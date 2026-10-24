"""Managed engine installation. No credentials in argv, profile TOML, logs or receipts."""
from __future__ import annotations
import contextlib
import datetime as dt
import json
import math
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
    'zai': ('codex-zcode', 'zcodex', 'codex'),
}
ENV = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
       'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENCODE_API_KEY',
       'ZAI_API_KEY', 'ZAI_BASE_URL', 'ZAI_TOKEN'}
BEGIN = '# BEGIN HUB MANAGED ENGINES\n'
END = '# END HUB MANAGED ENGINES\n'


def root():
    explicit = os.environ.get('AGENT_HUB_MANAGED_DIR') or os.environ.get('HUB_MANAGED_DIR')
    if explicit:
        return Path(explicit).resolve()
    return config.get_host_config_path().parent / 'managed-engines'


def read_json(path, fallback=None):
    try:
        return json.loads(private_fs.read_bytes(path, 1048576))
    except FileNotFoundError:
        return fallback


def write_json(path, value):
    private_fs.atomic_write(path, json.dumps(value).encode(), 1048576)


def normalize_git_identity(conf) -> dict[str, str]:
    if not isinstance(conf, dict):
        return {}
    res: dict[str, str] = {}
    for k, v in conf.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                full_key = f"{k}.{sub_k}".lower()
                res[full_key] = str(sub_v)
        else:
            k_lower = k.lower()
            if k_lower in ('name', 'user_name', 'user.name'):
                res['user.name'] = str(v)
            elif k_lower in ('email', 'user_email', 'user.email'):
                res['user.email'] = str(v)
            elif k_lower in ('signing_key', 'signingkey', 'user.signingkey'):
                res['user.signingkey'] = str(v)
            elif k_lower in ('gpg_sign', 'gpgsign', 'commit.gpgsign'):
                res['commit.gpgsign'] = 'true' if (v is True or str(v).lower() in ('true', '1', 'yes')) else str(v)
            elif k_lower in ('push_default', 'push.default'):
                res['push.default'] = str(v)
            elif k_lower in ('push_auto_setup_remote', 'push.autosetupremote'):
                res['push.autosetupremote'] = 'true' if (v is True or str(v).lower() in ('true', '1', 'yes')) else str(v)
            elif '.' in k:
                res[k] = str(v)
            else:
                res[f'user.{k}'] = str(v)
    return res


def render_gitconfig(git_config: dict[str, str]) -> str:
    sections: dict[str, dict[str, str]] = {}
    for full_key, val in git_config.items():
        if '.' in full_key:
            sec, k = full_key.split('.', 1)
        else:
            sec, k = 'user', full_key
        sections.setdefault(sec, {})[k] = str(val)
    lines = []
    sec_keys = list(sections.keys())
    if 'user' in sec_keys:
        sec_keys.remove('user')
        sec_keys.insert(0, 'user')
    for sec in sec_keys:
        lines.append(f'[{sec}]')
        for k, v in sorted(sections[sec].items()):
            lines.append(f'\t{k} = {v}')
    return '\n'.join(lines) + '\n'


def load_git_identity() -> dict[str, str]:
    """Resolve configured Git identity from host settings, host config, or environment."""
    # 1. Host settings: subscriptions.json (fleet.load_settings)
    try:
        from . import fleet
        settings = fleet.load_settings()
        git_conf = settings.get('git') if 'git' in settings else settings.get('git_identity')
        if git_conf:
            return normalize_git_identity(git_conf)
    except Exception:
        pass

    # 2. Host config: host.json (config.load_host_config)
    try:
        host_cfg = config.load_host_config()
        git_conf = host_cfg.get('git') if 'git' in host_cfg else host_cfg.get('git_identity')
        if git_conf:
            return normalize_git_identity(git_conf)
    except Exception:
        pass

    # 3. Environment variables fallback
    env_name = os.environ.get('HUB_GIT_NAME') or os.environ.get('GIT_AUTHOR_NAME')
    env_email = os.environ.get('HUB_GIT_EMAIL') or os.environ.get('GIT_AUTHOR_EMAIL')
    res = {}
    if env_name:
        res['user.name'] = env_name
    if env_email:
        res['user.email'] = env_email
    return res


def provision_managed_home(bundle=None, home=None, dest=None):
    """Shared provisioning hook point for managed engine homes.

    Provisions .gitconfig, credentials, and state hooks into home.
    Re-applies idempotently on re-bootstrap or home recreation.
    """
    if home is None:
        if dest is not None:
            home = Path(dest) / 'home'
        elif isinstance(bundle, dict) and 'label' in bundle and 'revision' in bundle:
            dest = root() / (bundle['label'] + '-' + bundle['revision'])
            home = dest / 'home'
        else:
            raise ValueError('home or dest/bundle must be provided')
    home = Path(home)
    with private_fs.parent(home / 'placeholder', create=True):
        pass

    # 1. Provision git identity
    git_ident = load_git_identity()
    if git_ident:
        content = render_gitconfig(git_ident)
        gitconfig = home / '.gitconfig'
        private_fs.atomic_write(gitconfig, content.encode('utf-8'), 32768)
        if os.name != 'nt':
            try:
                os.chmod(gitconfig, 0o600)
            except OSError:
                pass

    # 2. Bundle files (credentials, etc.)
    if isinstance(bundle, dict) and isinstance(bundle.get('files'), dict):
        for relative, value in bundle['files'].items():
            target = home / relative
            private_fs.atomic_write(target, value.encode(), 32768)
            if os.name != 'nt':
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
    return home


@contextlib.contextmanager
def lock():
    """One reconciler/exec reader; owner-only lock stays on disk across processes."""
    path = root() / 'reconcile.lock'
    with private_fs.parent(path, create=True) as anchor:
        fd = private_fs._open(anchor, path, os.O_CREAT | os.O_RDWR)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, 0)
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
    if bundle['provider'] == 'zai' and any('muse' in a.lower() or 'opencode-go' in a.lower() for a in args):
        raise ValueError('Z.ai cannot route OpenCode Muse')


def profile_path():
    return Path(os.environ.get('APLEXER_CONFIG', Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'aplexer' / 'config.toml'))


def module_command(handler, label, managed_dir=None):
    prefix = ''
    if Path(sys.argv[0]).is_file() and zipfile.is_zipfile(sys.argv[0]):
        prefix = 'import sys; sys.path.insert(0, ' + repr(str(Path(sys.argv[0]).resolve())) + '); '
    m_dir = str(managed_dir or root())
    prefix += 'import os; os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(m_dir) + '; '
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


def _copy_runtime_resources(src_res_dir, dst_res_dir):
    dst_res_dir.mkdir(parents=True, exist_ok=True)
    for name in ('glm', 'tools', 'model-providers', 'config'):
        src_entry = src_res_dir / name
        if src_entry.is_dir():
            dst_entry = dst_res_dir / name
            if dst_entry.is_dir():
                shutil.rmtree(dst_entry)
            shutil.copytree(src_entry, dst_entry)


def parse_deb_manifest(manifest_text):
    match = re.search(r'-\s*url:\s*(https?://[^\s]+\.deb)\s*\n\s*sha512:\s*([A-Za-z0-9+/=]+)', manifest_text)
    if not match:
        match = re.search(r'-\s*sha512:\s*([A-Za-z0-9+/=]+)\s*\n\s*url:\s*(https?://[^\s]+\.deb)', manifest_text)
        if match:
            deb_url, sha512 = match.group(2).strip(), match.group(1).strip()
        else:
            return None, None
    else:
        deb_url, sha512 = match.group(1).strip(), match.group(2).strip()
    try:
        import base64
        decoded = base64.b64decode(sha512, validate=True)
        if len(decoded) != 64:
            return deb_url, None
    except Exception:
        return deb_url, None
    return deb_url, sha512


def locate_zcode_runtime(dest=None):
    override = os.environ.get('ZCODE_CJS')
    if override:
        cand = Path(override)
        if cand.is_file() and os.access(cand, os.R_OK):
            return cand
    candidates = []
    if dest:
        candidates.append(dest / 'engine/runtime/glm/zcode.cjs')
        candidates.append(dest / 'engine/runtime/zcode.cjs')
    candidates.extend([
        Path('/opt/ZCode/resources/glm/zcode.cjs'),
        root() / 'zcode-runtime/glm/zcode.cjs',
        root() / 'zcode-runtime/zcode.cjs',
        Path.home() / '.local/share/zcode/zcode.cjs',
    ])
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.R_OK):
            return cand
    return None


def ensure_zcode_runtime(dest=None):
    runtime = locate_zcode_runtime(dest)
    if runtime:
        if dest and not runtime.resolve().is_relative_to(dest.resolve()):
            src_res = runtime.parent.parent if runtime.parent.name == 'glm' else runtime.parent
            if src_res.is_dir():
                _copy_runtime_resources(src_res, dest / 'engine/runtime')
                local_cjs = dest / 'engine/runtime/glm/zcode.cjs'
                if local_cjs.is_file():
                    return local_cjs
        return runtime

    system = sys.platform
    cpu = platform.machine().lower()
    if system != 'linux' or cpu not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        raise ValueError('Automatic ZCode runtime distribution unsupported on this platform')
    platform_name = 'linux-aarch64' if cpu in ('aarch64', 'arm64') else 'linux-x86_64'
    manifest_url = f'https://zcode.z.ai/api/v1/releases/electron/manifest?platform={platform_name}&channel=1'
    runtime_dir = root() / 'zcode-runtime'
    tmp_dir = dest / 'engine/tmp-zcode' if dest else root() / 'tmp-zcode'
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = tmp_dir / 'manifest.json'
        subprocess.run(['curl', '-fSL', manifest_url, '-o', str(manifest_path)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        manifest_text = manifest_path.read_text()
        deb_url, sha512_expected = parse_deb_manifest(manifest_text)
        if not deb_url:
            raise ValueError('ZCode deb asset not found in release manifest')
        if not sha512_expected:
            raise ValueError('ZCode deb release manifest missing valid SHA-512 checksum')
        deb_path = root() / 'zcode-cache' / f'zcode-latest-{platform_name}.deb'
        with private_fs.parent(deb_path, create=True):
            pass
        if not deb_path.is_file():
            subprocess.run(['curl', '-fSL', deb_url, '-o', str(deb_path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
        import base64, hashlib
        expected_hex = base64.b64decode(sha512_expected.strip()).hex()
        with open(deb_path, 'rb') as f:
            actual_hex = hashlib.sha512(f.read()).hexdigest()
        if actual_hex != expected_hex:
            raise ValueError('Official ZCode deb SHA-512 mismatch')
        tmp_unpack = tmp_dir / 'unpack'
        tmp_unpack.mkdir(parents=True, exist_ok=True)
        subprocess.run(['dpkg-deb', '-x', str(deb_path), str(tmp_unpack)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
        src_res = tmp_unpack / 'opt/ZCode/resources'
        if not (src_res / 'glm/zcode.cjs').is_file():
            raise ValueError('Extracted deb missing zcode.cjs')
        _copy_runtime_resources(src_res, runtime_dir)
        if dest:
            _copy_runtime_resources(src_res, dest / 'engine/runtime')
        return locate_zcode_runtime(dest)
    except Exception:
        raise ValueError('ZCode runtime missing (/opt/ZCode/resources/glm/zcode.cjs)') from None
    finally:
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def resolve_npm_command(is_windows=None):
    """Resolve npm command safely on Windows and POSIX without shell concatenation.

    On Windows, requires node + trusted npm-cli.js to avoid .cmd quoting/space issues;
    fails closed instead of falling back to batch execution.
    """
    if is_windows is None:
        is_windows = (os.name == 'nt')
    if is_windows:
        node = shutil.which('node')
        candidates = []
        override = os.environ.get('HUB_NPM_CLI_JS')
        if override:
            candidates.append(Path(override))
        if node:
            node_p = Path(node)
            candidates.append(node_p.parent / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js')
            candidates.append(node_p.parent / 'lib' / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js')
        npm_path = shutil.which('npm') or shutil.which('npm.cmd')
        if npm_path:
            npm_p = Path(npm_path)
            candidates.append(npm_p.parent / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js')
            candidates.append(npm_p.parent / 'lib' / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js')
        for cand in candidates:
            if cand.is_file():
                if not node:
                    node = shutil.which('node')
                if not node:
                    raise ValueError('Node runtime missing for npm execution')
                return [node, str(cand.resolve())]
        raise ValueError('Trusted npm-cli.js not found on Windows; refusing unsafe batch fallback')
    return ['npm']


def locate_native_zcodex(bundle=None, is_windows=None):
    if is_windows is None:
        is_windows = (os.name == 'nt')
    override = os.environ.get('ZCODEX_BIN')
    if override and Path(override).is_file() and (is_windows or os.access(override, os.X_OK)):
        return Path(override)
    which_bin = shutil.which('zcodex') or (shutil.which('zcodex.cmd') if is_windows else None)
    if which_bin:
        return Path(which_bin)
    cands = [Path.home() / '.local/bin/zcodex', Path('/usr/local/bin/zcodex'), Path('/usr/bin/zcodex')]
    if is_windows:
        cands.extend([Path.home() / '.local/bin/zcodex.cmd', Path.home() / '.local/bin/zcodex.bat', Path.home() / '.local/bin/zcodex.exe'])
    for cand in cands:
        if cand.is_file() and (is_windows or os.access(cand, os.X_OK)):
            return cand
    return None


def executable_path(bundle, dest, is_windows=None):
    """Use packaged native binaries directly; never run npm lifecycle scripts/stubs."""
    if is_windows is None:
        is_windows = (os.name == 'nt')
    _, binary, _ = PACKAGES[bundle['provider']]
    if bundle['provider'] == 'zai':
        wrapper = dest / ('engine/bin/zcodex.cmd' if is_windows else 'engine/bin/zcodex')
        if not wrapper.is_file():
            wrapper = dest / 'engine/bin/zcodex'
        if wrapper.is_file() and (is_windows or os.access(wrapper, os.X_OK)):
            return wrapper
        native = locate_native_zcodex(bundle, is_windows=is_windows)
        if native and Path(native).is_file():
            return Path(native)
        raise ValueError('Native zcodex binary missing')
    modules = dest / 'engine/node_modules'
    if bundle['provider'] in ('claude', 'go'):
        system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win32'}.get('win32' if is_windows else sys.platform)
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
    if is_windows:
        # JS provider CLIs are invoked through Node, not a Windows .cmd shell stub.
        package = modules / PACKAGES[bundle['provider']][0]
        metadata = json.loads((package / 'package.json').read_text())
        relative = metadata['bin'][binary] if isinstance(metadata['bin'], dict) else metadata['bin']
        executable = package / relative
        if not executable.resolve().is_relative_to(package.resolve()):
            raise ValueError('Unsafe engine package binary')
        return executable
    return modules / '.bin' / binary


def engine_argv(bundle, dest, is_windows=None):
    if is_windows is None:
        is_windows = (os.name == 'nt')
    # A host may require its protected quota wrapper for every Codex start.
    # This is operator environment, never a bundle/request supplied command.
    wrapper = os.environ.get('CODEX_LAUNCH_WRAPPER') if bundle['provider'] == 'codex' else None
    if wrapper:
        path = Path(wrapper)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError('Configured Codex launch wrapper unavailable')
        return [str(path)]
    executable = executable_path(bundle, dest, is_windows=is_windows)
    if is_windows and bundle['provider'] in ('codex', 'gemini'):
        node = shutil.which('node')
        if not node:
            raise ValueError('Node runtime missing')
        return [node, str(executable)]
    return [str(executable)]


def install(bundle):
    validate(bundle)
    dest = root() / (bundle['label'] + '-' + bundle['revision'])
    home = dest / 'home'
    receipt = read_json(dest / 'bundle.json')
    if receipt == bundle:
        provision_managed_home(bundle, home, dest)
        return
    pkg, binary, _ = PACKAGES[bundle['provider']]
    # Credentials are delivered only after package installation; installers never see them.
    with private_fs.parent(dest / 'placeholder', create=True):
        pass
    clean_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')}
    clean_env.update(
        HOME=str(dest),
        USERPROFILE=str(dest),
        APPDATA=str(dest / 'AppData' / 'Roaming'),
        LOCALAPPDATA=str(dest / 'AppData' / 'Local'),
        npm_config_cache=str(root() / 'npm-cache'),
    )
    if bundle['provider'] == 'zai':
        bin_name = 'engine/bin/zcodex.cmd' if os.name == 'nt' else 'engine/bin/zcodex'
        wrapper = dest / bin_name
        with private_fs.parent(wrapper, create=True):
            pass
        native = locate_native_zcodex(bundle)
        if native and Path(native).is_file():
            if os.name == 'nt':
                script = f'@echo off\r\n"{native}" %*\r\n'
            else:
                script = '#!/bin/sh\nexec "' + str(native) + '" "$@"\n'
            private_fs.atomic_write(wrapper, script.encode(), 4096)
            if hasattr(os, 'chmod'):
                try:
                    os.chmod(wrapper, 0o700)
                except OSError:
                    pass
        else:
            system = sys.platform
            cpu = platform.machine().lower()
            if system != 'linux' or cpu not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
                raise ValueError('Native zcodex platform unsupported')
            arch = 'amd64' if cpu in ('x86_64', 'amd64') else 'arm64'
            asset = f'zcodex-linux-{arch}.tar.zst'
            version = bundle['version']
            tag = f'zcode-3.14.5-codex-{version}' if not version.startswith('zcode-') else version
            url = f'https://github.com/alexeygrigorev/codex-zcode/releases/download/{tag}/{asset}'
            cache_file = root() / 'zcodex-cache' / asset
            tmp_download = dest / 'engine/tmp-zcodex'
            try:
                tmp_download.mkdir(parents=True, exist_ok=True)
                target_tar = cache_file if cache_file.is_file() else tmp_download / asset
                sums_url = f'https://github.com/alexeygrigorev/codex-zcode/releases/download/{tag}/SHA256SUMS'
                sums_file = tmp_download / 'SHA256SUMS'
                subprocess.run(['curl', '-fSL', sums_url, '-o', str(sums_file)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
                expected_sha = None
                for line in sums_file.read_text().splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].strip().lstrip('*') == asset:
                        expected_sha = parts[0].strip()
                        break
                    elif asset in line and len(parts) >= 1:
                        expected_sha = parts[0].strip()
                        break
                if not expected_sha or len(expected_sha) != 64 or not all(c in '0123456789abcdefABCDEF' for c in expected_sha):
                    raise ValueError(f'Missing or malformed SHA256 checksum for {asset}')
                if not target_tar.is_file():
                    subprocess.run(['curl', '-fSL', url, '-o', str(target_tar)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
                import hashlib
                with open(target_tar, 'rb') as f:
                    actual_sha = hashlib.sha256(f.read()).hexdigest()
                if actual_sha.lower() != expected_sha.lower():
                    raise ValueError(f'Checksum mismatch for {asset}')
                subprocess.run(['tar', '--zstd', '-xf', str(target_tar), '-C', str(tmp_download)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
                extracted = tmp_download / f'zcodex-linux-{arch}'
                if not extracted.is_file():
                    extracted = tmp_download / 'zcodex'
                if not extracted.is_file():
                    raise ValueError('Release asset does not contain zcodex binary')
                shutil.move(str(extracted), str(wrapper))
                os.chmod(wrapper, 0o700)
            except (subprocess.SubprocessError, OSError, ValueError):
                raise ValueError('Managed engine installation failed') from None
            finally:
                if tmp_download.is_dir():
                    shutil.rmtree(tmp_download, ignore_errors=True)
        runtime = ensure_zcode_runtime(dest)
        if not runtime:
            raise ValueError('ZCode runtime missing (/opt/ZCode/resources/glm/zcode.cjs)')
    else:
        npm_cmd = resolve_npm_command()
        try:
            subprocess.run(npm_cmd + ['install', '--prefix', str(dest / 'engine'), '--ignore-scripts', '--no-audit', '--no-fund', '--', pkg + '@' + bundle['version']],
                           env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=True)
        except (subprocess.SubprocessError, OSError):
            raise ValueError('Managed engine installation failed') from None
    executable = executable_path(bundle, dest)
    if not executable.is_file():
        raise ValueError('Managed engine binary missing')
    if bundle['provider'] in ('claude', 'go', 'zai'):
        try:
            subprocess.run(engine_argv(bundle, dest) + ['--version'], env=clean_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        except (subprocess.SubprocessError, OSError):
            raise ValueError('Native engine verification failed') from None
    provision_managed_home(bundle, home, dest)
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
        installed_bundles = []
        failed_bundles = []
        last_error = None
        for b in bundles:
            try:
                install(b)
                installed_bundles.append(b)
            except Exception as e:
                failed_bundles.append(b)
                last_error = e
        if bundles and not installed_bundles:
            if len(bundles) == 1 and last_error:
                raise last_error
            raise ValueError('Managed engine installation failed')
        profiles(installed_bundles)
        write_json(root() / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in installed_bundles])
        for b in installed_bundles:
            authenticated = b['provider'] != 'gemini' or (root() / (b['label'] + '-' + b['revision']) / 'home/.gemini/oauth_creds.json').is_file()
            request('POST', '/api/engines/' + b['label'], {'revision': b['revision'], 'authenticated': authenticated})
        from . import engine_login
        engine_login.tick(response.get('logins', []), installed_bundles, request)
    result = {'status': 'synced', 'profiles': ['hub-' + b['label'] for b in installed_bundles]}
    if failed_bundles:
        result['failed_labels'] = [b['label'] for b in failed_bundles]
    return result


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


def launcher_metadata(provider, bundle=None):
    args = (bundle or {}).get('args') or []
    model = None
    effort = None
    if '--model' in args:
        idx = args.index('--model')
        if idx + 1 < len(args):
            model = args[idx + 1]
    if '--effort' in args:
        idx = args.index('--effort')
        if idx + 1 < len(args):
            effort = args[idx + 1]
    if provider == 'go':
        return {
            'launcher_provider': 'opencode',
            'launcher_model': model or 'opencode-go/muse-spark-1.3-contributor',
            'launcher_effort': effort,
        }
    elif provider == 'zai':
        return {
            'launcher_provider': 'zai',
            'launcher_model': model or 'glm-5.3-flash',
            'launcher_effort': effort or 'high',
        }
    elif provider == 'codex':
        return {
            'launcher_provider': 'codex',
            'launcher_model': model or 'gpt-6.1-sol',
            'launcher_effort': effort or 'high',
        }
    elif provider == 'claude':
        return {
            'launcher_provider': 'claude',
            'launcher_model': model or 'sonnet',
            'launcher_effort': effort or 'high',
        }
    elif provider == 'gemini':
        return {
            'launcher_provider': 'gemini',
            'launcher_model': model or 'gemini-2.5-pro',
            'launcher_effort': effort or 'high',
        }
    return {}


def managed_subscriptions():
    state = read_json(root() / 'state.json', [])
    subs = []
    for b in state:
        bundle = None
        try:
            dest = root() / (b['label'] + '-' + b['revision'])
            bundle = read_json(dest / 'bundle.json')
        except Exception:
            pass
        meta = launcher_metadata(b['provider'], bundle)
        sub = {
            'service': ('codex-orin' if b['provider'] == 'codex' and
                        (bundle or {}).get('quota_provider') == 'codex-orin' else b['provider']),
            'account_label': b['label'],
            'engine': PACKAGES[b['provider']][2],
            'profile': 'hub-' + b['label'],
            'admission_argv': module_command('admission', b['label']),
        }
        sub.update(meta)
        subs.append(sub)
    return subs


def engine_environment(bundle, home, dest=None):
    dest = dest or Path(home).parent
    env = {k: v for k, v in os.environ.items() if k not in ENV and k not in ('HUB_TOKEN', 'HUB_JOB_TOKEN', 'HUB_CONFIG_FILE', 'AGENT_HUB_MANAGED_DIR', 'HUB_MANAGED_DIR')}
    env.update(
        HOME=str(home),
        GEMINI_CLI_HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / 'AppData' / 'Roaming'),
        LOCALAPPDATA=str(home / 'AppData' / 'Local'),
        XDG_CONFIG_HOME=str(home / '.config'),
        XDG_CACHE_HOME=str(home / '.cache'),
        XDG_DATA_HOME=str(home / '.local' / 'share'),
        XDG_STATE_HOME=str(home / '.local' / 'state'),
        CODEX_HOME=str(home / '.codex'),
        CLAUDE_CONFIG_DIR=str(home / '.claude'),
    )
    if bundle.get('provider') == 'zai':
        env['CODEX_HOME'] = str(home / '.zcodex')
        env['ZCODE_HOME'] = str(home / '.zcode')
        node = shutil.which('node')
        if not node:
            for cand in ('/usr/bin/node', '/usr/local/bin/node'):
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    node = cand
                    break
        if node:
            env['ZCODE_NODE'] = node
        runtime = locate_zcode_runtime(dest)
        if runtime:
            env['ZCODE_CJS'] = str(runtime)
    env.update(bundle.get('env') or {})
    git_ident = load_git_identity()
    name = git_ident.get('user.name') or git_ident.get('name')
    email = git_ident.get('user.email') or git_ident.get('email')
    if name:
        env['GIT_AUTHOR_NAME'] = name
        env['GIT_COMMITTER_NAME'] = name
    if email:
        env['GIT_AUTHOR_EMAIL'] = email
        env['GIT_COMMITTER_EMAIL'] = email
    if bundle.get('provider') == 'claude':
        for k in (
            'CLAUDE_CODE_USE_BEDROCK',
            'CLAUDE_CODE_USE_VERTEX',
            'CLAUDE_CODE_USE_FOUNDRY',
            'ANTHROPIC_API_KEY',
            'ANTHROPIC_AUTH_TOKEN',
            'ANTHROPIC_CUSTOM_HEADERS',
        ):
            env.pop(k, None)
    return env


def probe_native_gemini_quota(bundle, dest, model=None):
    """Authoritative native Google Code Assist quota probe using native_google_quota."""
    from . import native_google_quota
    return native_google_quota.probe(bundle, dest, model=model)


def probe_native_claude_quota(bundle, dest, model=None):
    """Authoritative native Claude quota probe using native_claude_quota."""
    from . import native_claude_quota
    return native_claude_quota.probe(bundle, dest, model=model)


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
                if b.get('provider') == 'gemini':
                    from . import native_google_quota
                    raw = native_google_quota.probe(b, dest)
                elif b.get('provider') == 'claude':
                    from . import native_claude_quota
                    raw = native_claude_quota.probe(b, dest)
                else:
                    with tempfile.TemporaryFile(dir=root()) as output:
                        result = subprocess.run(['quse', b['provider'], '--json'], env=engine_environment(b, dest / 'home', dest),
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=30)
                        if result.returncode == 0 and output.tell() <= 131072:
                            output.seek(0)
                            raw = json.load(output)
            except Exception:
                pass
            accounts.append({'account': entry['label'], 'provider': entry['provider'], 'quota': quota_metadata(raw, entry['provider'])})
    return {'accounts': accounts}


def check_codex_quota_gate(env):
    """Enforce the mandatory >=15% weekly quota gate for OpenAI Codex before execution.

    Fails closed with machine-safe reason codes; never leaks raw stdout/stderr.
    """
    try:
        proc = subprocess.run(['quse', 'codex', '--json'], env=env, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 and not proc.stdout:
            return False, 'quota_unavailable'
        text = proc.stdout.lstrip()
        idx = text.find('{')
        if idx == -1:
            return False, 'quota_unavailable'
        data = json.loads(text[idx:])
        codex_data = data.get('codex')
        if not isinstance(codex_data, dict) or codex_data.get('status') != 'ok':
            return False, 'quota_unavailable'
        details = codex_data.get('details')
        if not isinstance(details, dict) or details.get('limit_reached') is not False:
            return False, 'quota_unavailable'
        windows = codex_data.get('windows')
        if not isinstance(windows, dict):
            return False, 'quota_unavailable'

        now = dt.datetime.now(dt.timezone.utc)
        min_remaining = None
        has_weekly = False

        for win_name, win in windows.items():
            if not isinstance(win, dict) or win.get('present') is False:
                continue
            if (win_name in ('5h', 'monthly')
                    and win.get('percent_remaining') is None
                    and win.get('reset_at') is None):
                continue  # not applicable window for this plan

            if win_name in ('7d', 'weekly'):
                has_weekly = True

            perc = win.get('percent_remaining')
            if perc is None or isinstance(perc, bool) or not isinstance(perc, (int, float)) or not math.isfinite(perc):
                return False, 'quota_unavailable'
            reset_at = win.get('reset_at')
            if not isinstance(reset_at, str):
                return False, 'quota_unavailable'
            try:
                reset_text = reset_at[:-1] + '+00:00' if reset_at.endswith('Z') else reset_at
                reset_dt = dt.datetime.fromisoformat(reset_text)
                if reset_dt.tzinfo is None:
                    return False, 'quota_unavailable'
                if reset_dt <= now:
                    return False, 'stale_window_evidence'
            except Exception:
                return False, 'quota_unavailable'

            val = float(perc)
            if min_remaining is None or val < min_remaining:
                min_remaining = val

        if not has_weekly or min_remaining is None:
            return False, 'quota_unavailable'
        if min_remaining <= 15.0:
            return False, 'codex_reserve_15_percent_required'
        return True, 'ok'
    except Exception:
        return False, 'quota_check_failed'


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
        provision_managed_home(b, home, dest)
        _, binary, _ = PACKAGES[b['provider']]
        command = engine_argv(b, dest)
        env = engine_environment(b, home, dest)
        extra_argv = list(sys.argv[2:])
        if b.get('provider') == 'go' and extra_argv and extra_argv[0] == 'run':
            pinned_model = None
            args_copy = list(b.get('args') or [])
            for flag in ('--model', '-m'):
                if flag in args_copy:
                    idx = args_copy.index(flag)
                    if idx + 1 < len(args_copy):
                        pinned_model = args_copy[idx + 1]
                    del args_copy[idx:idx + 2]
                    break
            run_cmd = [extra_argv[0]]
            for flag in ('--model', '-m'):
                if flag in extra_argv[1:]:
                    idx = extra_argv[1:].index(flag) + 1
                    if idx + 1 < len(extra_argv):
                        req_model = extra_argv[idx + 1]
                        if pinned_model and req_model != pinned_model:
                            raise ValueError(f"Provider 'go' pinned model mismatch: {req_model} != {pinned_model}")
            if pinned_model:
                run_cmd.extend(['--model', pinned_model])
            run_cmd.extend(args_copy)
            i = 1
            while i < len(extra_argv):
                if extra_argv[i] in ('--model', '-m') and pinned_model:
                    i += 2
                    continue
                run_cmd.append(extra_argv[i])
                i += 1
            argv = command + run_cmd
        else:
            argv = command + b['args'] + extra_argv
        is_diagnostic = b['args'] + extra_argv in (["--version"], ["-v"], ["--help"], ["-h"])
        if b['provider'] == 'codex' and not is_diagnostic:
            allowed, reason = check_codex_quota_gate(env)
            if not allowed:
                print(json.dumps({'provider': 'codex', 'launch_allowed': False, 'reason': reason}), file=sys.stderr)
                sys.exit(75)
        for var in ('HUB_CONFIG_FILE', 'HUB_TOKEN', 'HUB_JOB_TOKEN', 'AGENT_HUB_MANAGED_DIR', 'HUB_MANAGED_DIR'):
            env.pop(var, None)
        if os.name == 'nt':
            res = subprocess.run(argv, env=env)
            sys.exit(res.returncode)
        else:
            os.execve(command[0], argv, env)


def admission():
    label = sys.argv[1]
    payload = {}
    try:
        if not sys.stdin.isatty():
            raw_in = sys.stdin.read()
            if raw_in and raw_in.strip():
                payload = json.loads(raw_in)
    except Exception:
        payload = {}
    try:
        result = host_request('POST', '/api/engines/admission', {'label': label})
    except Exception:
        result = {'launch_allowed': False, 'profile_supported': False, 'account_label': label}

    bundle = None
    try:
        with lock():
            state = read_json(root() / 'state.json', [])
            record = next((b for b in state if b['label'] == label), None)
            if record:
                dest = root() / (label + '-' + record['revision'])
                bundle = read_json(dest / 'bundle.json')
    except Exception:
        pass

    if bundle is None and result.get('launch_allowed'):
        result['launch_allowed'] = False
        result['profile_supported'] = False
        result['reason'] = 'local_bundle_missing'

    if isinstance(payload, dict) and bundle:
        offered_profile = payload.get('profile')
        expected_profile = 'hub-' + label
        if offered_profile is not None and offered_profile != expected_profile:
            result['launch_allowed'] = False
            result['profile_supported'] = False
            result['reason'] = 'profile_mismatch'
        offered_account = payload.get('account')
        if offered_account is not None and offered_account != label:
            result['launch_allowed'] = False
            result['reason'] = 'account_mismatch'
        selection = payload.get('selection')
        meta = launcher_metadata(bundle['provider'], bundle)
        if isinstance(selection, dict):
            sel_model = selection.get('model')
            sel_effort = selection.get('effort')
            if sel_model == meta.get('launcher_model') and sel_effort == meta.get('launcher_effort'):
                result['model_supported'] = True
                result['model'] = sel_model
                result['effort'] = sel_effort
            else:
                result['model_supported'] = False
                result['launch_allowed'] = False
                result['reason'] = 'model_mismatch'

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
                 'go': ('.local/share/opencode/auth.json', '.config/opencode/opencode.json', '1.19.3'),
                 'zai': ('.zcode/v2/credentials.json', '.zcodex/config.toml', '0.155.1')}
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
        elif args.provider == 'zai':
            files = {}
            if getattr(args, 'token_file', None):
                token = private_fs.read_bytes(Path(args.token_file).expanduser().absolute(), 8000).decode().strip()
                if not token or any(c.isspace() for c in token):
                    raise ValueError('Invalid subscription token file')
                env['ZAI_API_KEY'] = token
                files['.config/goz/config.json'] = json.dumps({'zai_token': token, 'zai_base_url': 'https://api.z.ai/api/anthropic'})
                files['.zcodex/config.toml'] = 'model = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
            elif args.auth_file:
                raw_auth = private_fs.read_bytes(Path(args.auth_file).expanduser().absolute(), 32768).decode()
                try:
                    auth_json = json.loads(raw_auth)
                except Exception:
                    token = raw_auth.strip()
                    if not token or any(c.isspace() for c in token):
                        raise ValueError('Invalid auth file content')
                    env['ZAI_API_KEY'] = token
                    files['.config/goz/config.json'] = json.dumps({'zai_token': token, 'zai_base_url': 'https://api.z.ai/api/anthropic'})
                    files['.zcodex/config.toml'] = 'model = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
                else:
                    if isinstance(auth_json, dict):
                        if 'oauth:zai:access_token' in auth_json or 'zcodejwttoken' in auth_json:
                            files['.zcode/v2/credentials.json'] = raw_auth
                            tok = auth_json.get('oauth:zai:access_token') or auth_json.get('zcodejwttoken')
                            if tok and isinstance(tok, str):
                                env['ZAI_API_KEY'] = tok
                                files['.config/goz/config.json'] = json.dumps({'zai_token': tok, 'zai_base_url': 'https://api.z.ai/api/anthropic'})
                        elif 'zai_token' in auth_json:
                            files['.config/goz/config.json'] = raw_auth
                            tok = auth_json.get('zai_token')
                            if tok and isinstance(tok, str):
                                env['ZAI_API_KEY'] = tok
                        elif 'zai-coding-plan' in auth_json and isinstance(auth_json['zai-coding-plan'], dict):
                            files['.local/share/opencode/auth.json'] = json.dumps({'zai-coding-plan': auth_json['zai-coding-plan']})
                            tok = auth_json['zai-coding-plan'].get('key')
                            if tok and isinstance(tok, str):
                                env['ZAI_API_KEY'] = tok
                                files['.config/goz/config.json'] = json.dumps({'zai_token': tok, 'zai_base_url': 'https://api.z.ai/api/anthropic'})
                        else:
                            raise ValueError('Native file has no Z.ai credential')
                        files['.zcodex/config.toml'] = 'model = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
                    else:
                        raise ValueError('Invalid auth file format')
            else:
                raise ValueError('Native import requires auth-file or token-file')
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
        default_args = ['--model', 'opencode-go/muse-spark-1.3-contributor'] if args.provider == 'go' else (['--model', 'glm-5.3-flash'] if args.provider == 'zai' else [])
        bundle = {'label': args.label, 'provider': args.provider, 'version': args.version or version, 'files': files,
                  'env': env, 'args': default_args}
    if not isinstance(bundle, dict) or not LABEL.fullmatch(bundle.get('label', '')):
        raise ValueError('Invalid managed bundle')
    if not args.file and args.provider == 'gemini':
        bundle['auth_method'] = 'machine-login'
    return bus._do_request('PUT', '/api/engines/' + bundle['label'], bundle)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('execute', 'admission', 'sync', 'quotas'):
        handler_name = sys.argv[1]
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        if handler_name == 'execute':
            execute()
        elif handler_name == 'admission':
            admission()
        elif handler_name == 'sync':
            print(json.dumps(sync()))
        elif handler_name == 'quotas':
            print(json.dumps(quotas()))

