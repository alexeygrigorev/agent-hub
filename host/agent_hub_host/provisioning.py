"""Managed engine installation. No credentials in argv, profile TOML, logs or receipts."""
from __future__ import annotations
import contextlib
import json
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from . import bus, config, private_fs

LABEL = re.compile(r'[a-z][a-z0-9_-]{0,47}\Z')
REV = re.compile(r'[a-f0-9-]{36}\Z')
PACKAGES = {
    'codex': ('@openai/codex', 'codex', 'codex'),
    'claude': ('@anthropic-ai/claude-code', 'claude', 'claude'),
    'gemini': ('@google/gemini-cli', 'gemini', 'gemini'),
    'go': ('opencode-ai', 'opencode', 'opencode'),
}
ENV = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
       'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENCODE_API_KEY'}
BEGIN = '# BEGIN HUB MANAGED ENGINES\n'
END = '# END HUB MANAGED ENGINES\n'


def root():
    return config.get_host_config_path().parent / 'managed-engines'


def read_json(path, fallback=None):
    try:
        return json.loads(private_fs.read_bytes(path, 1048576))
    except FileNotFoundError:
        return fallback


def write_json(path, value):
    private_fs.atomic_write(path, json.dumps(value).encode(), 1048576)


@contextlib.contextmanager
def lock():
    """One reconciler/exec reader; owner-only lock stays on disk across processes."""
    path = root() / 'reconcile.lock'
    with private_fs.parent(path, create=True) as anchor:
        fd = private_fs._open(anchor, path, os.O_CREAT | os.O_RDWR)
        try:
            if os.name == 'nt':
                import msvcrt
                os.write(fd, b'0'); os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def validate(bundle):
    if not isinstance(bundle, dict) or not LABEL.fullmatch(bundle.get('label', '')) or not REV.fullmatch(bundle.get('revision', '')) or bundle.get('provider') not in PACKAGES:
        raise ValueError('Invalid managed bundle')
    if bundle['provider'] == 'gemini' and (bundle.get('auth_method') != 'machine-login' or any(re.search('oauth|credential|token', k, re.I) for k in bundle.get('files', {})) or bundle.get('env')):
        raise ValueError('Gemini uses native per-machine login')
    if not isinstance(bundle.get('version'), str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', bundle['version']):
        raise ValueError('Invalid managed version')
    if not isinstance(bundle.get('files'), dict) or len(bundle['files']) > 24 or not isinstance(bundle.get('env'), dict):
        raise ValueError('Invalid managed credentials')
    for path, value in bundle['files'].items():
        if not isinstance(path, str) or len(path) > 200 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.', '..') for p in path.split('/')) or not isinstance(value, str) or len(value) > 16000:
            raise ValueError('Unsafe managed credential path')
    if any(k not in ENV or not isinstance(v, str) or len(v) > 8000 or '\0' in v for k, v in bundle['env'].items()):
        raise ValueError('Unsafe managed environment')
    args = bundle.get('args')
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) >= 1024 or '\0' in a for a in args):
        raise ValueError('Invalid engine arguments')
    if bundle['provider'] == 'go' and args not in (['--model', 'opencode-go/muse-spark-1.3-contributor'], ['--model', 'opencode-go/muse-spark-1.3-contributor', '--version']):
        raise ValueError('OpenCode requires subscribed Muse')


def profile_path():
    return Path(os.environ.get('APLEXER_CONFIG', Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'aplexer' / 'config.toml'))


def module_command(handler, label):
    prefix = ''
    if Path(sys.argv[0]).is_file() and zipfile.is_zipfile(sys.argv[0]):
        prefix = 'import sys; sys.path.insert(0, ' + repr(str(Path(sys.argv[0]).resolve())) + '); '
    return [sys.executable, '-c', prefix + 'from agent_hub_host.provisioning import ' + handler + '; ' + handler + '()', label]


def profiles(bundles):
    """Replace only our marked block; refuse a collision with operator profiles."""
    path = profile_path()
    # aplexer's ordinary TOML may originally be 0644. Tighten only this owned file
    # and its containing directory after no-follow ownership checks.
    with private_fs.parent(path, create=True, private=False) as anchor:
        if os.name != 'nt':
            if os.fstat(anchor).st_uid != os.getuid():
                raise PermissionError('Unowned aplexer directory')
            os.fchmod(anchor, 0o700)
        try:
            fd = private_fs._open(anchor, path, os.O_RDONLY, private=False)
        except FileNotFoundError:
            pass
        else:
            try:
                if os.name != 'nt':
                    if os.fstat(fd).st_uid != os.getuid():
                        raise PermissionError('Unowned aplexer configuration')
                    os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
    try:
        text = private_fs.read_bytes(path, 262144).decode()
    except FileNotFoundError:
        text = 'version = 1\n'
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('Invalid managed profile block')
    if BEGIN in text:
        before, rest = text.split(BEGIN)
        _, after = rest.split(END)
        text = before + after
    block = BEGIN
    for b in bundles:
        name = 'hub-' + b['label']
        if re.search(r'^\s*\[profiles\.' + re.escape(name) + r'(?:\]|\.)', text, re.M):
            raise ValueError('Operator profile collision')
        # Only public label and paths go into native registry command metadata.
        command = module_command('execute', b['label'])
        block += '\n[profiles.' + name + ']\nengine = ' + json.dumps(PACKAGES[b['provider']][2]) + '\ncommand = ' + json.dumps(command) + '\n'
    private_fs.atomic_write(path, (text.rstrip() + '\n\n' + block + END).encode(), 262144)


def executable_path(bundle, dest):
    """Use packaged native binaries directly; never run npm lifecycle scripts/stubs."""
    _, binary, _ = PACKAGES[bundle['provider']]
    modules = dest / 'engine/node_modules'
    if bundle['provider'] in ('claude', 'go'):
        system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win32'}.get(sys.platform)
        cpu = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
        if not system or not cpu:
            raise ValueError('Native engine platform unsupported')
        musl = '-musl' if system == 'linux' and platform.libc_ver()[0] == 'musl' else ''
        if bundle['provider'] == 'claude':
            candidates = [(modules / ('@anthropic-ai/claude-code-' + system + '-' + cpu + musl), binary + ('.exe' if system == 'win32' else ''))]
        else:
            target = ('windows' if system == 'win32' else system) + '-' + cpu
            variants = ['-baseline' + musl, musl] if cpu == 'x64' else [musl]
            candidates = [(modules / ('opencode-' + target + suffix), 'bin/' + binary + ('.exe' if system == 'win32' else '')) for suffix in variants]
        for package, relative in candidates:
            metadata = package / 'package.json'
            if metadata.is_file() and json.loads(metadata.read_text()).get('version') == bundle['version']:
                executable = package / relative
                if executable.is_file() and executable.resolve().is_relative_to(dest.resolve()):
                    return executable
        raise ValueError('Packaged native engine binary missing')
    if os.name == 'nt':
        # JS provider CLIs are invoked through Node, not a Windows .cmd shell stub.
        package = modules / PACKAGES[bundle['provider']][0]
        metadata = json.loads((package / 'package.json').read_text())
        relative = metadata['bin'][binary] if isinstance(metadata['bin'], dict) else metadata['bin']
        executable = package / relative
        if not executable.resolve().is_relative_to(package.resolve()):
            raise ValueError('Unsafe engine package binary')
        return executable
    return modules / '.bin' / binary


def engine_argv(bundle, dest):
    executable = executable_path(bundle, dest)
    if os.name == 'nt' and bundle['provider'] in ('codex', 'gemini'):
        node = shutil.which('node')
        if not node:
            raise ValueError('Node runtime missing')
        return [node, str(executable)]
    return [str(executable)]


def install(bundle):
    validate(bundle)
    dest = root() / (bundle['label'] + '-' + bundle['revision'])
    receipt = read_json(dest / 'bundle.json')
    if receipt == bundle:
        return
    pkg, binary, _ = PACKAGES[bundle['provider']]
    home = dest / 'home'
    # Credentials are delivered only after package installation; installers never see them.
    with private_fs.parent(dest / 'placeholder', create=True):
        pass
    clean_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')}
    clean_env.update(HOME=str(dest), npm_config_cache=str(root() / 'npm-cache'))
    try:
        subprocess.run(['npm', 'install', '--prefix', str(dest / 'engine'), '--ignore-scripts', '--no-audit', '--no-fund', '--', pkg + '@' + bundle['version']],
                       env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
    except (subprocess.SubprocessError, OSError):
        raise ValueError('Managed engine installation failed') from None
    executable = executable_path(bundle, dest)
    if not executable.is_file():
        raise ValueError('Managed engine binary missing')
    if bundle['provider'] in ('claude', 'go'):
        try:
            subprocess.run(engine_argv(bundle, dest) + ['--version'], env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        except (subprocess.SubprocessError, OSError):
            raise ValueError('Native engine verification failed') from None
    for relative, value in bundle['files'].items():
        private_fs.atomic_write(home / relative, value.encode(), 32768)
    with private_fs.parent(home / 'placeholder', create=True):
        pass
    write_json(dest / 'bundle.json', bundle)


def sync(request=None):
    """Called on startup and each machine heartbeat. Failure preserves no new route."""
    request = request or host_request
    response = request('GET', '/api/engines/sync')
    bundles = response.get('engines')
    if not isinstance(bundles, list) or len(bundles) > 32:
        raise ValueError('Invalid engine sync response')
    for b in bundles:
        validate(b)
    if len({b['label'] for b in bundles}) != len(bundles):
        raise ValueError('Duplicate managed label')
    with lock():
        previous = read_json(root() / 'state.json', [])
        # Withdraw removed/changed profiles before touching credentials or installing updates.
        retained = [b for b in bundles if any(p['label'] == b['label'] and p['revision'] == b['revision'] for p in previous)]
        profiles(retained)
        from . import engine_login
        engine_login.cancel_removed(bundles)
        keep = {b['label'] + '-' + b['revision'] for b in retained}
        for entry in root().iterdir():
            if re.fullmatch(r'[a-z][a-z0-9_-]{0,47}-[a-f0-9-]{36}', entry.name) and entry.name not in keep:
                # rmtree does not follow directory symlinks; reject them explicitly.
                if entry.is_symlink():
                    raise PermissionError('Unsafe managed directory')
                shutil.rmtree(entry)
        write_json(root() / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in retained])
        for b in bundles:
            install(b)
        profiles(bundles)
        write_json(root() / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in bundles])
        for b in bundles:
            authenticated = b['provider'] != 'gemini' or (root() / (b['label'] + '-' + b['revision']) / 'home/.gemini/oauth_creds.json').is_file()
            request('POST', '/api/engines/' + b['label'], {'revision': b['revision'], 'authenticated': authenticated})
        from . import engine_login
        engine_login.tick(response.get('logins', []), bundles, request)
    return {'status': 'synced', 'profiles': ['hub-' + b['label'] for b in bundles]}


def host_request(method, path, payload=None):
    """Always the host identity, even when a human profile is also logged in."""
    from . import api, auth
    import urllib.request
    cfg = config.load_host_config()
    base = cfg.get('base_url')
    api.check_url(base)
    req = urllib.request.Request(base.rstrip('/') + path, method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + auth.get_host_access_token(), 'Content-Type': 'application/json'})
    return api.safe_http_json(req)


def tick():
    try:
        return sync()
    except Exception:
        # Never stringify provider output, paths, request bodies or secret-bearing exceptions.
        return {'status': 'held', 'reason': 'managed_engine_sync_failed'}


def managed_subscriptions():
    state = read_json(root() / 'state.json', [])
    return [{'service': b['provider'], 'account_label': b['label'], 'engine': PACKAGES[b['provider']][2], 'profile': 'hub-' + b['label'],
             'admission_argv': module_command('admission', b['label'])} for b in state]


def engine_environment(bundle, home):
    env = {k: v for k, v in os.environ.items() if k not in ENV and k not in ('HUB_TOKEN', 'HUB_JOB_TOKEN')}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), XDG_DATA_HOME=str(home / '.local' / 'share'), XDG_STATE_HOME=str(home / '.local' / 'state'), CODEX_HOME=str(home / '.codex'), CLAUDE_CONFIG_DIR=str(home / '.claude'))
    env.update(bundle['env'])
    return env


def quotas():
    """For the dedicated server collector: dynamic isolated accounts, sanitized only."""
    from .fleet import quota_metadata
    accounts = []
    with lock():
        state = read_json(root() / 'state.json', [])
        for entry in state:
            raw = {}
            try:
                dest = root() / (entry['label'] + '-' + entry['revision'])
                b = read_json(dest / 'bundle.json')
                validate(b)
                with tempfile.TemporaryFile(dir=root()) as output:
                    result = subprocess.run(['quse', b['provider'], '--json'], env=engine_environment(b, dest / 'home'),
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=30)
                    if result.returncode == 0 and output.tell() <= 131072:
                        output.seek(0)
                        raw = json.load(output)
            except Exception:
                pass
            accounts.append({'account': entry['label'], 'provider': entry['provider'], 'quota': quota_metadata(raw, entry['provider'])})
    return {'accounts': accounts}


def execute():
    label = sys.argv[1]
    if not LABEL.fullmatch(label):
        raise ValueError('Invalid managed label')
    with lock():
        state = read_json(root() / 'state.json', [])
        record = next((b for b in state if b['label'] == label), None)
        if record is None:
            raise ValueError('Managed profile removed')
        dest = root() / (label + '-' + record['revision'])
        b = read_json(dest / 'bundle.json')
        validate(b)
        home = dest / 'home'
        _, binary, _ = PACKAGES[b['provider']]
        command = engine_argv(b, dest)
        env = engine_environment(b, home)
        argv = command + b['args'] + sys.argv[2:]
        os.execve(command[0], argv, env)


def admission():
    label = sys.argv[1]
    try:
        result = host_request('POST', '/api/engines/admission', {'label': label})
    except Exception:
        result = {'launch_allowed': False, 'profile_supported': False, 'account_label': label}
    print(json.dumps(result))


def add_commands(sub):
    parser = sub.add_parser('engines', help='Centrally add/remove engine accounts; secrets are read from a private file')
    commands = parser.add_subparsers(dest='engines_cmd', required=True)
    add = commands.add_parser('add')
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', help='Mode-600 JSON bundle (never put credentials in arguments)')
    source.add_argument('--provider', choices=sorted(PACKAGES), help='Import an existing private native auth file')
    add.add_argument('--label', help='Central account label for native auth import')
    add.add_argument('--token-file', help='Private Claude setup-token output (subscription token, not API billing)')
    add.add_argument('--auth-file', help='Private native auth file; contents never printed')
    add.add_argument('--config-file', help='Optional private native config file')
    add.add_argument('--version', help='Exact pinned engine version (defaults to tested version)')
    commands.add_parser('list')
    commands.add_parser('quota', help='Sanitized managed account observations for the server collector')
    remove = commands.add_parser('remove')
    remove.add_argument('label')
    commands.add_parser('sync', help='Reconcile this enrolled machine now')


def command(args):
    if args.engines_cmd == 'quota':
        return quotas()
    if args.engines_cmd == 'sync':
        return sync()
    if args.engines_cmd == 'list':
        return bus._do_request('GET', '/api/engines')
    if args.engines_cmd == 'remove':
        if not LABEL.fullmatch(args.label):
            raise ValueError('Invalid managed label')
        return bus._do_request('DELETE', '/api/engines/' + args.label)
    if args.file:
        bundle = json.loads(private_fs.read_bytes(Path(args.file).expanduser().absolute(), 32768))
    else:
        if not args.label or not LABEL.fullmatch(args.label) or (args.provider != 'gemini' and not (args.auth_file or getattr(args, 'token_file', None))):
            raise ValueError('Native import requires label and auth-file')
        paths = {'codex': ('.codex/auth.json', '.codex/config.toml', '0.163.0'),
                 'claude': ('.claude/.credentials.json', '.claude/settings.json', '2.1.304'),
                 'gemini': ('.gemini/oauth_creds.json', '.gemini/settings.json', '0.64.0'),
                 'go': ('.local/share/opencode/auth.json', '.config/opencode/opencode.json', '1.19.3')}
        auth_path, config_path, version = paths[args.provider]
        env = {}
        if args.provider == 'gemini':
            if args.auth_file or getattr(args, 'token_file', None):
                raise ValueError('Gemini credentials stay local; request native login in Hub')
            files = {}
        elif args.provider == 'claude':
            if not getattr(args, 'token_file', None) or args.auth_file:
                raise ValueError('Claude managed subscription import requires a private setup-token file')
            token = private_fs.read_bytes(Path(getattr(args, 'token_file', None)).expanduser().absolute(), 8000).decode().strip()
            if not token or any(c.isspace() for c in token):
                raise ValueError('Invalid subscription token file')
            env['CLAUDE_CODE_OAUTH_TOKEN'] = token
            files = {}
        else:
            if getattr(args, 'token_file', None) or not args.auth_file:
                raise ValueError('Native import requires auth-file')
            files = {auth_path: private_fs.read_bytes(Path(args.auth_file).expanduser().absolute(), 16000).decode()}
        if args.provider == 'go':
            native_auth = json.loads(files[auth_path])
            if not isinstance(native_auth, dict) or 'opencode-go' not in native_auth:
                raise ValueError('Native file has no OpenCode Go credential')
            files[auth_path] = json.dumps({'opencode-go': native_auth['opencode-go']})
        if args.config_file:
            files[config_path] = private_fs.read_bytes(Path(args.config_file).expanduser().absolute(), 16000).decode()
        bundle = {'label': args.label, 'provider': args.provider, 'version': args.version or version, 'files': files,
                  'env': env, 'args': ['--model', 'opencode-go/muse-spark-1.3-contributor'] if args.provider == 'go' else []}
    if not isinstance(bundle, dict) or not LABEL.fullmatch(bundle.get('label', '')):
        raise ValueError('Invalid managed bundle')
    if not args.file and args.provider == 'gemini':
        bundle['auth_method'] = 'machine-login'
    return bus._do_request('PUT', '/api/engines/' + bundle['label'], bundle)
