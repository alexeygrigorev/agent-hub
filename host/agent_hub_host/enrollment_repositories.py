"""Clone only Hub-approved repositories with short-lived, read-only credentials."""
import base64
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import urllib.error
from . import private_fs

ALIAS = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
ORIGIN = re.compile(r'https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\.git\Z')
RESERVED = {'con', 'prn', 'aux', 'nul', *(f'{p}{i}' for p in ('com', 'lpt') for i in range(1, 10))}


def sync(install_root, *, request=None, runner=None):
    from . import bootstrap, provisioning
    request = request or provisioning.host_request
    runner = runner or bootstrap.run
    try:
        listing = request('GET', '/api/host/repositories')
    except urllib.error.HTTPError as error:
        if error.code == 503:
            try:
                body = json.loads(error.read(2048))
            except (ValueError, OSError):
                # Keep the HTTP status so bootstrap can report an optional
                # repository outage and still install the controller.
                raise error from None
            if body == {'error': 'repository_access_not_configured'}:
                return None
        raise
    repositories = listing.get('repositories')
    if not isinstance(repositories, list) or len(repositories) > 64:
        raise ValueError('Invalid repository enrollment response')
    seen = set()
    for repo in repositories:
        if (not isinstance(repo, dict) or set(repo) != {'alias', 'origin'} or
                not isinstance(repo['alias'], str) or not ALIAS.fullmatch(repo['alias']) or
                repo['alias'] in RESERVED or repo['alias'] in seen or
                not isinstance(repo['origin'], str) or not ORIGIN.fullmatch(repo['origin']) or
                repo['origin'].rsplit('/', 1)[1] in ('.git', '..git', '...git')):
            raise ValueError('Invalid repository allowlist')
        seen.add(repo['alias'])
    root = Path(install_root) / 'workspaces'
    if root.is_symlink():
        raise ValueError('Linked repository root refused')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_COUNT='3', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
               GIT_CONFIG_KEY_1='http.followRedirects', GIT_CONFIG_VALUE_1='false',
               GIT_CONFIG_KEY_2='core.hooksPath', GIT_CONFIG_VALUE_2=os.devnull)
    paths = {}
    for repo in repositories:
        alias, origin = repo['alias'], repo['origin']
        digest = hashlib.sha256((alias + '\0' + origin).encode()).hexdigest()
        with private_fs.exclusive_lock(Path(install_root) / 'locks' / ('repository-' + digest + '.lock')):
            paths[alias] = _clone(root, alias, origin, digest, request, runner, env)
    return paths


def _clone(root, alias, origin, digest, request, runner, env):
    # A killed installer leaves a deterministic stage plus its private
    # ownership record. The per-repository kernel lock excludes any live
    # installer before recovery; unmarked/foreign scratch is preserved.
    staged = root / ('.clone-owned-' + digest)
    marker = root / (staged.name + '.json')
    ownership = {'version': 1, 'alias': alias, 'origin': origin}
    if marker.exists() or marker.is_symlink():
        saved = json.loads(private_fs.read_bytes(marker, 4096))
        if saved != ownership:
            raise ValueError('Repository staging ownership differs; files preserved')
        _clear_stage(staged)
        marker.unlink()
    elif staged.exists() or staged.is_symlink():
        raise ValueError('Unowned repository staging directory; files preserved')
    target = root / alias
    if target.is_symlink() or (target / '.git').is_symlink():
        raise ValueError('Linked repository refused')
    if target.exists():
        if not (target / '.git').is_dir():
            raise ValueError('Existing target is not a complete repository; files preserved')
        actual = runner(['git', '-C', target, 'remote', 'get-url', 'origin'], env=env, capture=True)
        if actual != origin:
            raise ValueError('Existing repository origin differs; files preserved')
        return str(target)
    grant = request('POST', '/api/host/repositories/token', {'repository': alias})
    token = grant.get('token')
    try:
        expires = dt.datetime.fromisoformat(grant['expires_at'].replace('Z', '+00:00'))
        lifetime = (expires - dt.datetime.now(dt.timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise ValueError('Invalid repository credential expiry') from None
    if (grant.get('origin') != origin or grant.get('username') != 'x-access-token' or
            not isinstance(token, str) or not re.fullmatch(r'[^\s\x00-\x1f\x7f]{12,16384}', token) or
            not 0 < lifetime <= 3660):
        raise ValueError('Invalid repository credential')
    # Git's environment-only config never writes credentials to argv,
    # .git/config, credential helpers or logs. The header is scoped to
    # this exact HTTPS repository; redirects and ambient tracing are off.
    clone_env = dict(env, GIT_CONFIG_COUNT='4',
                     GIT_CONFIG_KEY_3='http.' + origin + '.extraHeader',
                     GIT_CONFIG_VALUE_3='Authorization: Basic ' +
                     base64.b64encode(('x-access-token:' + token).encode()).decode())
    private_fs.atomic_write(marker, json.dumps(ownership).encode(), 4096)
    try:
        print('Cloning approved repository ' + alias + ' (up to 30 minutes).', flush=True)
        # Do not expose raw git output (which can echo credentials). The
        # alias-only progress message is safe for install transcripts.
        runner(['git', 'clone', '--', origin, staged], env=clone_env, capture=True, timeout=1800)
        if staged.is_symlink() or not (staged / '.git').is_dir() or (staged / '.git').is_symlink():
            raise ValueError('Incomplete repository clone')
        os.rename(staged, target)
    finally:
        clone_env.clear()
        _clear_stage(staged)
        marker.unlink()
    return str(target)


def _clear_stage(staged):
    if staged.is_symlink():
        raise ValueError('Linked repository staging directory; files preserved')
    if staged.exists():
        shutil.rmtree(staged)
