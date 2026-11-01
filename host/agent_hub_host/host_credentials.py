"""Hub-owned GitHub CLI token, shared git SSH key and commit identity (intake 54d5b703).

Hub holds the only copy of each credential and hands the current generation to
every host. The host materializes it non-interactively and reports gh/ssh
state back, so a machine where gh is logged out is repaired by the next sync
instead of by a person. Rules this module keeps:

* the token goes to ``gh auth login --with-token`` on stdin, never argv or logs;
* ``hosts.yml`` is never read; login state comes from ``gh auth status``;
* no browser or interactive login is ever started;
* existing SSH keys and config are never removed: the shared key is a new file
  and the ssh config change is one marked block;
* errors and reports carry fixed slugs (exception type), never output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

KEY_NAME = 'agent_hub_git_ed25519'
BLOCK_BEGIN = '# BEGIN agent-hub git key (managed; edits inside are overwritten)'
BLOCK_END = '# END agent-hub git key'
STATE_FILE = 'host-credentials-state.json'
POLL_S = 120
STATUS_REFRESH_S = 300
CMD_TIMEOUT_S = 30
# Published GitHub SSH host key (ED25519); `gh api meta` supplies the rest when reachable.
GITHUB_ED25519 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
_LOGIN_LINE = re.compile(r'Logged in to github\.com (?:account|as) ([A-Za-z0-9-]{1,39})')
_SCOPES_LINE = re.compile(r"Token scopes: (.+)")
_SCOPE = re.compile(r'[A-Za-z0-9_:.-]{1,64}')
_SSH_HI = re.compile(r"Hi ([A-Za-z0-9-]{1,39})! You've successfully authenticated")

_last = {'poll': 0.0, 'status': 0.0, 'body': None}


class HostCredentialError(RuntimeError):
    """Carries only a fixed slug."""


def ssh_dir() -> Path:
    return Path(os.environ.get('AGENT_HUB_SSH_DIR') or (Path.home() / '.ssh'))


def find_gh():
    found = shutil.which('gh')
    if found:
        return found
    for base in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        root = os.environ.get(base)
        if root:
            for rel in ('GitHub CLI/gh.exe', 'Programs/GitHub CLI/gh.exe'):
                candidate = Path(root) / rel
                if candidate.is_file():
                    return str(candidate)
    return None


def _env():
    # gh refuses to store a login while a token env var is in charge.
    env = {k: v for k, v in os.environ.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')}
    env.update(GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0', NO_COLOR='1')
    return env


def _run(argv, *, stdin=None, runner=subprocess.run):
    return runner(argv, input=stdin, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, env=_env())


def gh_status(runner=subprocess.run):
    """Logged-in state from `gh auth status` (never --show-token, never hosts.yml)."""
    gh = find_gh()
    if not gh:
        return {'logged_in': False, 'account': None, 'scopes': [], 'error': 'gh_not_installed'}
    try:
        done = _run([gh, 'auth', 'status', '--hostname', 'github.com'], runner=runner)
    except Exception as exc:
        return {'logged_in': False, 'account': None, 'scopes': [], 'error': type(exc).__name__[:60]}
    out = (done.stdout or '') + '\n' + (done.stderr or '')
    login = _LOGIN_LINE.search(out)
    if done.returncode != 0 or not login:
        return {'logged_in': False, 'account': None, 'scopes': [], 'error': None}
    scopes = []
    found = _SCOPES_LINE.search(out)
    if found:
        scopes = [s for s in (p.strip().strip("'\"") for p in found.group(1).split(',')) if _SCOPE.fullmatch(s)][:32]
    return {'logged_in': True, 'account': login.group(1), 'scopes': scopes, 'error': None}


def apply_gh_token(token, runner=subprocess.run):
    """Non-interactive login: token on stdin, then git credential helper and ssh protocol."""
    gh = find_gh()
    if not gh:
        raise HostCredentialError('gh_not_installed')
    done = _run([gh, 'auth', 'login', '--hostname', 'github.com', '--git-protocol', 'ssh', '--with-token'], stdin=token + '\n', runner=runner)
    if done.returncode != 0:
        raise HostCredentialError('gh_login_failed')
    # Best effort: the credential helper makes https remotes work with the same token.
    for argv in ([gh, 'auth', 'setup-git', '--hostname', 'github.com'], [gh, 'config', 'set', 'git_protocol', 'ssh', '--host', 'github.com']):
        try:
            _run(argv, runner=runner)
        except Exception:
            pass


def _restrict_windows(path):
    user = os.environ.get('USERNAME')
    if not user:
        raise HostCredentialError('windows_user_unknown')
    done = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', user + ':F'], capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
    if done.returncode != 0:
        raise HostCredentialError('icacls_failed')


def _write_private(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_name(path.name + '.' + os.urandom(6).hex() + '.tmp')
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data.encode())
        if os.name == 'nt':
            _restrict_windows(tmp)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _config_block(key_path: Path):
    return '\n'.join([BLOCK_BEGIN, 'Host github.com', '  HostName github.com', '  User git',
                      '  IdentityFile ' + json.dumps(key_path.as_posix()), '  IdentitiesOnly yes', BLOCK_END]) + '\n'


def merge_ssh_config(existing: str, block: str) -> str:
    """Replace our marked block in place, or put it first (ssh uses the first match). All other text is kept."""
    start = existing.find(BLOCK_BEGIN)
    end = existing.find(BLOCK_END)
    if start != -1 and end > start:
        tail = existing[end + len(BLOCK_END):].lstrip('\n')
        return existing[:start] + block + ('\n' + tail if tail else '')
    return block + ('\n' + existing if existing else '')


def merge_known_hosts(existing: str, keys):
    have = {line.split()[1] + ' ' + line.split()[2] for line in existing.splitlines()
            if len(line.split()) >= 3 and not line.startswith('#') and 'github.com' in line.split()[0].split(',')}
    add = [k for k in keys if ' '.join(k.split()[:2]) not in have]
    if not add:
        return existing
    sep = '' if not existing or existing.endswith('\n') else '\n'
    return existing + sep + ''.join('github.com ' + ' '.join(k.split()[:2]) + '\n' for k in add)


def github_host_keys(runner=subprocess.run):
    keys = [GITHUB_ED25519]
    gh = find_gh()
    if gh:
        try:
            done = _run([gh, 'api', 'meta', '--jq', '.ssh_keys[]'], runner=runner)
            if done.returncode == 0:
                keys += [line.strip() for line in done.stdout.splitlines() if re.fullmatch(r'(?:ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]{40,}', line.strip())]
        except Exception:
            pass
    return list(dict.fromkeys(keys))


def apply_ssh_key(payload, runner=subprocess.run):
    """Add the shared key as its own file plus one ssh config block and pinned host keys. Nothing is deleted."""
    directory = ssh_dir()
    key = directory / KEY_NAME
    private = payload['private_key'] if payload['private_key'].endswith('\n') else payload['private_key'] + '\n'
    _write_private(key, private)
    _write_private(directory / (KEY_NAME + '.pub'), payload['public_key'].strip() + '\n')
    config = directory / 'config'
    current = config.read_text(encoding='utf-8') if config.exists() else ''
    merged = merge_ssh_config(current, _config_block(key))
    if merged != current:
        _write_private(config, merged)
    known = directory / 'known_hosts'
    current = known.read_text(encoding='utf-8') if known.exists() else ''
    merged = merge_known_hosts(current, github_host_keys(runner))
    if merged != current:
        known.parent.mkdir(parents=True, exist_ok=True)
        with open(known, 'a', encoding='utf-8', newline='\n') as stream:
            stream.write(merged[len(current):])


def ssh_status(public_key=None, runner=subprocess.run):
    """`ssh -T git@github.com` with the shared key; the account comes from GitHub's greeting."""
    out = {'authenticated': False, 'account': None, 'key_fingerprint': None, 'error': None}
    key = ssh_dir() / KEY_NAME
    if not key.exists():
        out['error'] = 'key_not_installed'
        return out
    ssh = shutil.which('ssh')
    if not ssh:
        out['error'] = 'ssh_not_installed'
        return out
    argv = [ssh, '-T', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10']
    cfg = ssh_dir() / 'config'
    if cfg.exists():
        argv += ['-F', str(cfg)]
    argv.append('git@github.com')
    try:
        done = _run(argv, runner=runner)
    except Exception as exc:
        out['error'] = type(exc).__name__[:60]
        return out
    greeting = _SSH_HI.search((done.stderr or '') + (done.stdout or ''))
    if greeting:
        out.update(authenticated=True, account=greeting.group(1))
    else:
        out['error'] = 'ssh_denied'
    pub = ssh_dir() / (KEY_NAME + '.pub')
    try:
        fp = _run(['ssh-keygen', '-l', '-f', str(pub)], runner=runner)
        found = re.search(r'(SHA256:[A-Za-z0-9+/]{43})', fp.stdout or '')
        out['key_fingerprint'] = found.group(1) if found else None
    except Exception:
        pass
    return out


def apply_identity(payload, runner=subprocess.run):
    git = shutil.which('git')
    if not git:
        raise HostCredentialError('git_not_installed')
    for key, value in (('user.name', payload['name']), ('user.email', payload['email'])):
        cur = _run([git, 'config', '--global', '--get', key], runner=runner)
        if cur.returncode != 0 or cur.stdout.strip() != value:
            if _run([git, 'config', '--global', key, value], runner=runner).returncode != 0:
                raise HostCredentialError('git_config_failed')


def _state_path():
    from . import provisioning
    return provisioning.root() / STATE_FILE


def _load_state():
    from . import provisioning
    state = provisioning.read_json(_state_path(), {})
    return state if isinstance(state, dict) else {}


def _save_state(state):
    from . import provisioning
    provisioning.write_json(_state_path(), state)


def sync(request=None, runner=subprocess.run, clock=time.monotonic, force=False, _retry=True):
    """Fetch changed credentials from Hub, apply them, report status. Returns a slug-only summary."""
    from . import provisioning
    request = request or provisioning.host_request
    now = clock()
    if not force and now - _last['poll'] < POLL_S:
        return {'status': 'skipped'}
    _last['poll'] = now
    state = _load_state()
    gens = state.get('generations') if isinstance(state.get('generations'), dict) else {}
    have = ','.join(f'{k}:{v}' for k, v in sorted(gens.items()) if isinstance(v, int))
    response = request('GET', '/api/host-credentials/sync' + ('?have=' + have if have else ''))
    credentials = response.get('credentials') if isinstance(response, dict) else None
    if not isinstance(credentials, dict):
        raise ValueError('Invalid host credential response')
    errors = {}
    applied = []
    hub_gens = response.get('generations') if isinstance(response.get('generations'), dict) else {}
    # A previously failed kind stays at its old generation so the next poll retries it.
    appliers = {'git_ssh_key': apply_ssh_key, 'git_identity': apply_identity,
                'gh_token': lambda payload, runner: apply_gh_token(payload['token'], runner)}
    for kind, apply in appliers.items():
        item = credentials.get(kind)
        if not isinstance(item, dict) or not isinstance(item.get('payload'), dict) or not isinstance(item.get('generation'), int):
            continue
        try:
            apply(item['payload'], runner)
            gens[kind] = item['generation']
            applied.append(kind)
        except Exception as exc:
            errors[kind] = str(exc) if isinstance(exc, HostCredentialError) else type(exc).__name__
    state['generations'] = {k: v for k, v in gens.items() if k in hub_gens or k in applied}
    _save_state(state)
    reported = report_status(request, state['generations'], identity_applied=('git_identity' in state['generations']), runner=runner, clock=clock,
                             force=bool(applied) or force)
    if reported and reported.get('resync') and _retry and not applied:
        # Hub saw a missing login: ask for everything once, immediately.
        _last['poll'] = 0.0
        return sync(request, runner, clock, force=True, _retry=False)
    return {'status': 'synced', 'applied': applied, **({'errors': errors} if errors else {})}


def report_status(request, generations, *, identity_applied=False, runner=subprocess.run, clock=time.monotonic, force=False):
    now = clock()
    if not force and now - _last['status'] < STATUS_REFRESH_S:
        return None
    body = {'gh': gh_status(runner), 'ssh': ssh_status(runner=runner), 'identity_applied': bool(identity_applied), 'generations': generations}
    if not any(k in generations for k in ('git_ssh_key',)):
        body['ssh'] = {'authenticated': False, 'account': None, 'key_fingerprint': None, 'error': 'no_credential'}
    _last['status'] = now
    return request('POST', '/api/host-credentials/status', body)


def tick(request=None):
    """Heartbeat hook. Never raises; a failure leaves existing logins untouched."""
    try:
        return sync(request)
    except Exception as exc:
        return {'status': 'held', 'reason': type(exc).__name__}


# ---------- operator CLI: `hub host-credentials ...` (admin writes, no secret is ever printed) ----------

def add_parser(sub):
    parser = sub.add_parser('host-credentials', help='Hub-owned gh token, shared git SSH key and commit identity (never printed)')
    commands = parser.add_subparsers(dest='hostcred_cmd', required=True)
    commands.add_parser('status', help='Generations plus per-machine gh/ssh state (non-secret)')
    commands.add_parser('resync', help='Ask every host to re-fetch all credentials on its next poll')
    commands.add_parser('sync', help='Run one forced sync on this host now')
    token = commands.add_parser('set-gh-token', help='Store a gh token in Hub: from this host\'s gh login, a private file, or stdin')
    source = token.add_mutually_exclusive_group(required=True)
    source.add_argument('--from-gh', action='store_true', help='Pipe `gh auth token` straight to Hub (not displayed)')
    source.add_argument('--token-file', help='Private file holding the token')
    source.add_argument('--stdin', action='store_true')
    key = commands.add_parser('generate-ssh-key', help='Generate the ONE shared ed25519 git key, store it in Hub, discard the local copy')
    key.add_argument('--rotate', action='store_true', help='Replace an existing Hub key (hosts keep their old key files)')
    ident = commands.add_parser('set-identity', help='Commit identity every host applies to its global git config')
    ident.add_argument('--name', required=True)
    ident.add_argument('--email', required=True)
    commands.add_parser('register-key', help='Hub registers the shared public key on the gh token owner\'s GitHub account')
    return parser


def _read_token(args, runner=subprocess.run):
    if args.from_gh:
        gh = find_gh()
        if not gh:
            raise ValueError('gh is not installed on this host')
        done = runner([gh, 'auth', 'token', '--hostname', 'github.com'], capture_output=True, text=True, timeout=CMD_TIMEOUT_S, env=_env())
        if done.returncode != 0:
            raise ValueError('gh has no token on this host')
        return done.stdout.strip()
    if args.token_file:
        from . import private_fs
        return private_fs.read_bytes(Path(args.token_file).expanduser().absolute(), 4096).decode().strip()
    import sys
    return sys.stdin.readline().strip()


def generate_keypair(runner=subprocess.run):
    import tempfile
    keygen = shutil.which('ssh-keygen')
    if not keygen:
        raise ValueError('ssh-keygen is not installed on this host')
    with tempfile.TemporaryDirectory(prefix='hub-shared-key-') as tmp:
        path = Path(tmp) / 'key'
        done = runner([keygen, '-q', '-t', 'ed25519', '-N', '', '-C', 'agent-hub shared git key', '-f', str(path)], capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
        if done.returncode != 0:
            raise ValueError('ssh-keygen failed')
        return path.read_text(), (Path(str(path) + '.pub')).read_text().strip()


def command(args, request=None, runner=subprocess.run):
    from . import bus
    request = request or bus._do_request
    cmd = args.hostcred_cmd
    if cmd == 'status':
        return request('GET', '/api/host-credentials')
    if cmd == 'resync':
        return request('POST', '/api/host-credentials/resync', {})
    if cmd == 'sync':
        return sync(force=True)
    if cmd == 'register-key':
        return request('POST', '/api/host-credentials/register-key', {})
    if cmd == 'set-gh-token':
        return request('PUT', '/api/host-credentials/gh_token', {'payload': {'token': _read_token(args, runner)}})
    if cmd == 'set-identity':
        return request('PUT', '/api/host-credentials/git_identity', {'payload': {'name': args.name, 'email': args.email}})
    if cmd == 'generate-ssh-key':
        existing = [k for k in request('GET', '/api/host-credentials').get('kinds', []) if k.get('kind') == 'git_ssh_key']
        if existing and not args.rotate:
            raise ValueError('Hub already holds the shared git key; pass --rotate to replace it')
        private, public = generate_keypair(runner)
        return request('PUT', '/api/host-credentials/git_ssh_key', {'payload': {'private_key': private, 'public_key': public}})
    raise ValueError('unknown command')


def error_message(exc):
    """Fixed slug only: our own ValueError text, or the Hub's `error`/`status` slug from an HTTP refusal."""
    if isinstance(exc, ValueError):
        return 'host-credentials failed: ' + str(exc)
    reader = getattr(exc, 'read', None)
    if reader:
        try:
            body = json.loads(reader(4096))
            slug = body.get('error') or body.get('status')
            if isinstance(slug, str) and re.fullmatch(r'[a-z0-9_]{1,64}', slug):
                return 'host-credentials refused by Hub: ' + slug
        except Exception:
            pass
    return 'host-credentials failed: ' + type(exc).__name__
