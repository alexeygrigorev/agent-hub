"""Private provider homes materialized from Hub account credentials.

This module is the minimal adapter moved into the Hub repository from the
maintained Agent Quota Launcher's ``launcher.provider_home`` (the external
package remains untouched for its other consumers). It materializes and
verifies per-session private provider homes from AccountDO credential bundles
so the Hub2 host can launch native engines directly without the external
launcher runtime. The final pre-spawn check verifies the current session
binding only; quota and credential health are handled by model choice and
authentication fallback.

No tokens are logged; failures raise typed ``ValueError`` slugs only.
"""
import base64
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path

PATHS = {'codex': '.codex/auth.json', 'claude': '.claude/.credentials.json',
         'go': '.local/share/opencode-hub/device-auth.json', 'zai': '.config/goz/config.json'}
SDK_VERSIONS = {'codex': '0.166.0', 'claude': '2.1.322', 'go': '2.0.32'}
CLAUDE_PACKAGE = '@anthropic-ai/claude-code'
CLAUDE_MIN_VERSION = (2, 1, 312)
ZAI_BASE_URL = 'https://api.z.ai/api/anthropic'


def make_private(path):
    if os.name == 'nt':
        user = subprocess.check_output(['whoami'], text=True).strip()
        permission = '(OI)(CI)F' if Path(path).is_dir() else 'F'
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', user + ':' + permission],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    else:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)


_BROAD_NT_ACCOUNTS = {'everyone', 'users', 'authenticated users', 'all application packages',
                      'all restricted application packages', 'creator owner', 'guests', 'restricted'}
_TRUSTED_NT_ACCOUNTS = {'system', 'administrators'}


def _nt_acl_allows_only_current_user(path):
    """Minimal Windows ACL check: no inherited grants, no broad principals, and
    the current user must hold the only other access. ACE accounts are matched
    by their last name segment so the icacls path header sharing the line, or
    account casing, cannot break the check; anything unrecognized fails closed."""
    raw = subprocess.run(['icacls', str(path)], text=True, capture_output=True, timeout=10, check=False)
    if raw.returncode:
        raise ValueError('provider_file_not_private')
    user = subprocess.check_output(['whoami'], text=True).strip().lower()
    user_tail = user.rsplit('\\', 1)[-1]
    entries = re.findall(r'([^:()]+):\(([^)]*)\)', raw.stdout)
    if not entries:
        raise ValueError('provider_file_not_private')
    found_user = False
    for account, grant in entries:
        if '(I)' in grant:
            raise ValueError('provider_file_not_private')
        tail = account.strip().lower().rsplit('\\', 1)[-1]
        if tail in _BROAD_NT_ACCOUNTS:
            raise ValueError('provider_file_not_private')
        if account.strip().lower() == user or tail == user_tail:
            found_user = True
        elif tail not in _TRUSTED_NT_ACCOUNTS:
            raise ValueError('provider_file_not_private')
    if not found_user:
        raise ValueError('provider_file_not_private')


def private_read(path, limit=131072):
    path = Path(path)
    if os.name == 'nt':
        _nt_acl_allows_only_current_user(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('provider_file_invalid')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('provider_file_not_private')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('provider_file_too_large')
    return data


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def access_only(provider, value):
    """Preserve actual expiry; ordinary SDKs never receive a rotatable token.

    Installed Codex 0.166 refreshes at last_refresh + eight days. Claude
    2.1.322 and Go 2.0.32 refresh within five minutes of access expiry.
    """
    value = json.loads(json.dumps(value))
    proactive = None
    if provider == 'codex':
        token = value['tokens']['access_token']
        claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        expires = claims['exp'] * 1000
        refreshed = datetime.datetime.fromisoformat(value['last_refresh'].replace('Z', '+00:00'))
        if refreshed.tzinfo is None:
            raise ValueError('provider_refresh_time_unknown')
        if refreshed.timestamp() * 1000 > time.time() * 1000 + 5000:
            raise ValueError('provider_refresh_time_invalid')
        proactive = refreshed.timestamp() * 1000 + 8 * 86400000
    elif provider == 'claude':
        expires = value['claudeAiOauth']['expiresAt']
    else:
        active = [row for row in value if row.get('active') is True]
        if len(active) != 1:
            raise ValueError('provider_active_identity_ambiguous')
        expires = active[0]['value']['expires']
    if type(expires) not in (int, float) or not 0 < expires < 10**14:
        raise ValueError('provider_access_expiry_unknown')
    if proactive is None:
        proactive = expires - 300000
    def strip(item):
        if isinstance(item, list):
            return [strip(entry) for entry in item]
        if isinstance(item, dict):
            return {key: ('' if key in {'refresh', 'refresh_token', 'refreshToken', 'idpRefreshToken'} else strip(entry)) for key, entry in item.items()}
        return item
    stripped = strip(value)
    if provider == 'codex':
        stripped['tokens']['refresh_token'] = ''
    elif provider == 'go':
        for row in stripped:
            row['value']['refresh'] = ''
    return stripped, expires, proactive


def require_lifetime(data, timeout):
    if data['provider'] == 'zai':
        return
    # Sixty seconds is the explicit controller clock/dispatch safety margin,
    # in addition to the SDK's independently recorded proactive cutoff.
    if type(timeout) not in (int, float) or not 1 <= timeout <= 7200 or min(data['access_expires_at'], data['proactive_refresh_at']) <= time.time() * 1000 + (timeout + 60) * 1000:
        raise ValueError('provider_access_lifetime_insufficient')


def token_for(provider, value):
    if provider == 'codex':
        token = value['tokens']['access_token']
    elif provider == 'claude':
        token = value['claudeAiOauth']['accessToken']
    elif provider == 'go':
        active = [row for row in value if row.get('active') is True]
        if len(active) != 1:
            raise ValueError('provider_active_identity_ambiguous')
        token = active[0]['value']['access']
    else:
        token = value['zai_token']
        if value.get('zai_base_url', ZAI_BASE_URL) != ZAI_BASE_URL:
            raise ValueError('provider_origin_untrusted')
    if not isinstance(token, str) or not token or len(token) > 16000 or token.startswith('enc:') or any(c.isspace() for c in token):
        raise ValueError('provider_token_invalid')
    return token


def zcode_files(token, model):
    # SDK schema identifiers are not Hub account labels. The native model comes
    # from the configured policy, never a fallback/default model.
    provider = 'zai-coding-plan'
    config = {'schemaVersion': 1, 'config': {
        'providerConfigRules': {'providerRules': [{'providerId': provider, 'providerName': 'Z.AI Coding Plan',
            'config': {'group': 'standard-personal', 'access': {'type': 'api-key', 'apiKey': token},
                       'api': {'type': 'anthropic-messages', 'baseUrl': ZAI_BASE_URL},
                       'personalModelIds': [model], 'modelOrder': [model]}}]},
        'modelConfigRules': {'providerModelRules': [{'modelId': model, 'providerId': provider,
            'config': {'properties': {'contextWindow': 200000}}}], 'manualProviderModelRules': []},
        'defaultModelSelection': {'providerId': provider, 'modelId': model}}}
    cli = {'model': 'zai/' + model, 'provider': {provider: {'name': 'Z.AI Coding Plan', 'kind': 'anthropic',
        'source': 'custom', 'options': {'apiKey': token, 'baseURL': ZAI_BASE_URL, 'apiKeyRequired': True},
        'models': {model: {'limit': {'context': 200000}, 'modalities': {'input': ['text'], 'output': ['text']}}}},
        'zai': {'models': {model: {'name': model}}}}}
    harness = ('approval_policy="never"\nsandbox_mode="danger-full-access"\ncheck_for_update_on_startup=false\n'
               'model=' + json.dumps(model) + '\nmodel_provider="zcode"\n'
               '[model_providers.zcode]\nname="zcode"\nbase_url="' + ZAI_BASE_URL + '"\nwire_api="zcode"\n')
    return {'.zcode/v2/provider_config.json': json.dumps(config), '.zcode/cli/config.json': json.dumps(cli),
            '.zcodex/config.toml': harness}


def selected_token(provider, home, model):
    source = PATHS[provider]
    if provider == 'zai':
        harness=tomllib.loads(private_read(home / '.zcodex/config.toml').decode('utf8'))
        route=harness.get('model_providers',{}).get('zcode',{})
        if harness.get('model_provider')!='zcode' or harness.get('model')!=model or route.get('wire_api')!='zcode' or route.get('base_url')!=ZAI_BASE_URL:
            raise ValueError('provider_harness_route_mismatch')
        source = '.zcode/v2/provider_config.json'
        data = json.loads(private_read(home / source))['config']
        selection = data['defaultModelSelection']
        rules = data['providerConfigRules']['providerRules']
        matches = [row for row in rules if row['providerId'] == selection['providerId']]
        if len(matches) != 1 or selection['modelId'].lower() != model.lower():
            raise ValueError('provider_sdk_selection_mismatch')
        rule = matches[0]['config']
        if rule['api']['baseUrl'] != ZAI_BASE_URL:
            raise ValueError('provider_sdk_origin_mismatch')
        token = rule['access']['apiKey']
    elif provider == 'go':
        source = '.local/share/opencode/opencode.db'
        exported = go_sdk(home, ['auth', 'export', 'opencode'])
        if exported != access_only(provider, exported)[0]:
            raise ValueError('provider_refresh_material_forbidden')
        token = token_for(provider, exported)
    else:
        token = token_for(provider, json.loads(private_read(home / source)))
    return source, fingerprint(token)


def _version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('provider_sdk_version_invalid')
    return tuple(int(part) for part in value.split('.'))


def claude_engine_root():
    """Persistent private root for Hub-managed, side-by-side Claude binaries."""
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / '.local' / 'share'
    return base / 'agent-hub2' / 'engines'


def resolve_npm_command():
    """Resolve npm without invoking Windows batch files through a shell."""
    if os.name == 'nt':
        node = shutil.which('node')
        candidates = []
        if node:
            node_path = Path(node)
            candidates.extend((node_path.parent / 'node_modules/npm/bin/npm-cli.js',
                              node_path.parent / 'lib/node_modules/npm/bin/npm-cli.js'))
        npm = shutil.which('npm') or shutil.which('npm.cmd')
        if npm:
            npm_path = Path(npm)
            candidates.extend((npm_path.parent / 'node_modules/npm/bin/npm-cli.js',
                              npm_path.parent / 'lib/node_modules/npm/bin/npm-cli.js'))
        npm_cli = next((candidate for candidate in candidates if candidate.is_file()), None)
        if not node or not npm_cli:
            raise ValueError('provider_npm_unavailable')
        return [node, str(npm_cli.resolve())]
    npm = shutil.which('npm')
    if not npm:
        raise ValueError('provider_npm_unavailable')
    return [npm]


def _claude_native_path(engine_root, version):
    system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win32'}.get(sys.platform)
    cpu = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if not system or not cpu:
        raise ValueError('provider_sdk_platform_unsupported')
    musl = '-musl' if system == 'linux' and platform.libc_ver()[0] == 'musl' else ''
    native = Path(engine_root) / 'node_modules' / '@anthropic-ai' / f'claude-code-{system}-{cpu}{musl}'
    metadata = native / 'package.json'
    binary = native / ('claude.exe' if system == 'win32' else 'claude')
    try:
        package = json.loads(metadata.read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise ValueError('provider_sdk_native_binary_missing') from None
    if package.get('version') != version or not binary.is_file() or binary.is_symlink():
        raise ValueError('provider_sdk_native_binary_missing')
    if system != 'win32' and not os.access(binary, os.X_OK):
        raise ValueError('provider_sdk_native_binary_not_executable')
    resolved = binary.resolve()
    if resolved != binary.absolute() or not resolved.is_relative_to(Path(engine_root).resolve()):
        raise ValueError('provider_sdk_native_binary_untrusted')
    return resolved


def verify_managed_claude_binary(binary, expected_version=None):
    binary = Path(binary)
    root = claude_engine_root().resolve()
    if not binary.is_absolute() or binary.resolve() != binary or not binary.resolve().is_relative_to(root):
        raise ValueError('provider_sdk_native_binary_untrusted')
    try:
        version_dir = binary.parents[3]
    except IndexError:
        raise ValueError('provider_sdk_native_binary_untrusted') from None
    match = re.fullmatch(r'claude-(\d+\.\d+\.\d+)', version_dir.name)
    if not match:
        raise ValueError('provider_sdk_native_binary_untrusted')
    version = match.group(1)
    _version_tuple(version)
    if expected_version is not None and version != expected_version:
        raise ValueError('provider_sdk_native_binary_untrusted')
    if version_dir.parent.resolve() != root:
        raise ValueError('provider_sdk_native_binary_untrusted')
    package_path = version_dir / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'package.json'
    try:
        package = json.loads(package_path.read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise ValueError('provider_sdk_native_binary_untrusted') from None
    if package.get('version') != version or _claude_native_path(version_dir, version) != binary:
        raise ValueError('provider_sdk_native_binary_untrusted')
    _verify_claude_binary(binary, version)
    return version


def _claude_binary_digest(binary):
    digest = hashlib.sha256()
    with Path(binary).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def claude_fable_smoke_is_current(binary):
    try:
        version = verify_managed_claude_binary(binary)
        marker = Path(binary).parents[3] / '.hub2-fable-smoke.json'
        if marker.is_symlink():
            return False
        record = json.loads(private_read(marker, limit=4096))
        return (record.get('version') == version and record.get('model') == 'claude-fable-5-2'
                and record.get('binary_sha256') == _claude_binary_digest(binary))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _record_claude_fable_smoke(binary, version):
    marker = Path(binary).parents[3] / '.hub2-fable-smoke.json'
    payload = json.dumps({'version': version, 'model': 'claude-fable-5-2',
                          'binary_sha256': _claude_binary_digest(binary),
                          'checked_at': int(time.time() * 1000)}).encode('utf8')
    fd, temporary = tempfile.mkstemp(prefix='.fable-smoke-', dir=marker.parent)
    try:
        make_private(temporary)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        make_private(marker)
    finally:
        if fd is not None:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def _verify_claude_binary(binary, expected_version):
    binary = Path(binary)
    if not binary.is_absolute() or binary.is_symlink() or not binary.is_file():
        raise ValueError('provider_sdk_native_binary_missing')
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR'}}
    try:
        result = subprocess.run([str(binary), '--version'], text=True, capture_output=True,
                                timeout=10, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('provider_sdk_refresh_policy_unverified') from None
    stdout = result.stdout if isinstance(result.stdout, str) else ''
    stderr = result.stderr if isinstance(result.stderr, str) else ''
    output = stdout + '\n' + stderr
    observed = re.search(r'(?<![0-9.])' + re.escape(expected_version) + r'(?![0-9.])', output)
    if result.returncode or len(output) > 4096 or not observed:
        raise ValueError('provider_sdk_refresh_policy_unverified')
    return expected_version


def install_managed_claude(version, root=None):
    """Install and verify an immutable native Claude version under Hub2's home.

    Each version gets its own directory, so installing a canary never changes
    the executable used by already running sessions.
    """
    parsed = _version_tuple(version)
    if parsed < CLAUDE_MIN_VERSION:
        raise ValueError('provider_sdk_version_too_old')
    base = Path(root) if root is not None else claude_engine_root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.is_symlink():
        raise ValueError('provider_sdk_install_root_untrusted')
    base = base.resolve()
    make_private(base)
    target = base / ('claude-' + version)
    if target.exists():
        if target.is_symlink():
            raise ValueError('provider_sdk_install_root_untrusted')
        package_file = target / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'package.json'
        try:
            package = json.loads(package_file.read_text(encoding='utf8'))
        except (OSError, ValueError):
            raise ValueError('provider_sdk_install_corrupt') from None
        if package.get('version') != version:
            raise ValueError('provider_sdk_install_corrupt')
        binary = _claude_native_path(target, version)
        _verify_claude_binary(binary, version)
        return str(binary)

    staging = base / ('.claude-' + version + '-' + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    make_private(staging)
    cache = base / '.npm-cache'
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(cache)
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR'}}
    env.update({'HOME': str(base), 'USERPROFILE': str(base), 'npm_config_cache': str(cache),
                'npm_config_registry': 'https://registry.npmjs.org/', 'npm_config_ignore_scripts': 'true',
                'npm_config_update_notifier': 'false', 'npm_config_fund': 'false', 'npm_config_audit': 'false'})
    try:
        command = resolve_npm_command()
        result = subprocess.run(command + ['install', '--prefix', str(staging), '--ignore-scripts',
                                  '--no-audit', '--no-fund', '--no-update-notifier', '--no-progress',
                                  '--package-lock=false', '--', CLAUDE_PACKAGE + '@' + version],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, timeout=600, check=False)
        if result.returncode:
            raise ValueError('provider_sdk_install_failed')
        package_file = staging / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'package.json'
        try:
            package = json.loads(package_file.read_text(encoding='utf8'))
        except (OSError, ValueError):
            raise ValueError('provider_sdk_install_corrupt') from None
        if package.get('version') != version:
            raise ValueError('provider_sdk_install_corrupt')
        binary = _claude_native_path(staging, version)
        _verify_claude_binary(binary, version)
        make_private(staging)
        os.rename(staging, target)
        final_binary = _claude_native_path(target, version)
        _verify_claude_binary(final_binary, version)
        return str(final_binary)
    except ValueError:
        raise
    except (OSError, subprocess.SubprocessError):
        if target.is_dir() and not target.is_symlink():
            try:
                package_file = target / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'package.json'
                package = json.loads(package_file.read_text(encoding='utf8'))
                if package.get('version') == version:
                    binary = _claude_native_path(target, version)
                    _verify_claude_binary(binary, version)
                    return str(binary)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        raise ValueError('provider_sdk_install_failed') from None
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def managed_claude_installed():
    root = claude_engine_root()
    try:
        for target in root.glob('claude-*'):
            match = re.fullmatch(r'claude-(\d+\.\d+\.\d+)', target.name)
            if match and target.is_dir() and not target.is_symlink():
                try:
                    _claude_native_path(target, match.group(1))
                    return True
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    return False


def claude_install_available():
    if managed_claude_installed():
        return True
    try:
        resolve_npm_command()
        return True
    except ValueError:
        return False


def sdk_version(provider, binary=None, expected=None):
    binary = binary or shutil.which('opencode' if provider == 'go' else provider)
    if not binary:
        raise ValueError('provider_sdk_unavailable')
    expected = expected or SDK_VERSIONS[provider]
    try:
        result = subprocess.run([str(binary), '--version'], text=True, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('provider_sdk_refresh_policy_unverified') from None
    stdout = result.stdout if isinstance(result.stdout, str) else ''
    stderr = result.stderr if isinstance(result.stderr, str) else ''
    output = stdout + '\n' + stderr
    observed = re.search(r'(?<![0-9.])' + re.escape(expected) + r'(?![0-9.])', output)
    if result.returncode or len(output) > 4096 or not observed:
        raise ValueError('provider_sdk_refresh_policy_unverified')
    return expected


def materialize(root, account, provider, generation, files, model, timeout_seconds=7200, engine_binary=None):
    if provider not in PATHS or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', account):
        raise ValueError('provider_identity_invalid')
    if provider == 'zai' and account != 'zai-central':
        raise ValueError('zai_central_required')
    if type(generation) is not int or generation < 1 or set(files) != {PATHS[provider]}:
        raise ValueError('provider_generation_or_schema_invalid')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,120}', model):
        raise ValueError('provider_model_required')
    text = files[PATHS[provider]]
    if not isinstance(text, str) or not 0 < len(text) <= 16000:
        raise ValueError('provider_bundle_invalid')
    token = token_for(provider, json.loads(text))
    bounds = {}
    if provider != 'zai':
        native, expires, proactive = access_only(provider, json.loads(text))
        if provider == 'claude':
            if not isinstance(engine_binary, str):
                raise ValueError('provider_sdk_native_binary_missing')
            binary = Path(engine_binary)
            # The requested version is encoded in the versioned parent path and
            # verified against the native executable below.
            engine_version = verify_managed_claude_binary(binary)
            sdk = engine_version
            bounds.update({'engine_binary_path': str(binary), 'engine_version': engine_version, 'sdk_version': sdk})
        else:
            bounds['sdk_version'] = sdk_version(provider)
        bounds.update({'access_expires_at': expires, 'proactive_refresh_at': proactive, 'access_only': True})
        require_lifetime({'provider': provider, **bounds}, timeout_seconds)
        files = {PATHS[provider]: json.dumps(native)}
        text = files[PATHS[provider]]
    digest = fingerprint(token)
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    make_private(root)
    route_identity = model + ('\0' + engine_binary if provider == 'claude' and engine_binary else '')
    destination = root / (account + '-' + str(generation) + '-' + fingerprint(route_identity)[:12])
    if destination.exists():
        result = verify(destination / 'manifest.json')
        if result['credential_hash'] != digest or result['model'] != model:
            raise ValueError('provider_generation_conflict')
        if provider == 'claude' and result.get('engine_binary_path') != engine_binary:
            raise ValueError('provider_generation_conflict')
        require_lifetime(result, timeout_seconds)
        return str(destination / 'manifest.json')
    staging = Path(tempfile.mkdtemp(prefix='.provider-', dir=root))
    make_private(staging)
    try:
        content = dict(files)
        if provider == 'zai':
            content.update(zcode_files(token, model))
        for relative, value in content.items():
            path = staging / 'home' / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for directory in [path.parent, *path.parent.parents]:
                if directory == staging:
                    break
                make_private(directory)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8', newline='') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            make_private(path)
        if provider == 'go':
            go_sdk(staging / 'home', ['auth', 'import'], text)
        source, actual = selected_token(provider, staging / 'home', model)
        if actual != digest:
            raise ValueError('provider_sdk_credential_mismatch')
        metadata = {'version': 1, 'account_id': account, 'provider': provider, 'generation': generation,
                    'model': model, 'source': source, 'credential_hash': digest,
                    'files': {name: fingerprint(value) for name, value in content.items()}, **bounds}
        fd = os.open(staging / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(metadata, stream)
            stream.flush()
            os.fsync(stream.fileno())
        make_private(staging / 'manifest.json')
        if os.name != 'nt':
            for directory in sorted([p for p in staging.rglob('*') if p.is_dir()], reverse=True) + [staging]:
                fd = os.open(directory, os.O_RDONLY)
                os.fsync(fd)
                os.close(fd)
        os.rename(staging, destination)
        if os.name != 'nt':
            fd = os.open(root, os.O_RDONLY)
            os.fsync(fd)
            os.close(fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return str(destination / 'manifest.json')


def verify(manifest):
    path = Path(manifest)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError('provider_manifest_untrusted')
    data = json.loads(private_read(path))
    provider = data.get('provider')
    if data.get('version') != 1 or provider not in PATHS or type(data.get('generation')) is not int or data['generation'] < 1:
        raise ValueError('provider_manifest_invalid')
    if provider == 'zai' and data['account_id'] != 'zai-central':
        raise ValueError('zai_central_required')
    expected = {PATHS[provider]} | ({'.zcode/v2/provider_config.json', '.zcode/cli/config.json', '.zcodex/config.toml'} if provider == 'zai' else set())
    if set(data['files']) != expected:
        raise ValueError('provider_manifest_schema_invalid')
    home = path.parent / 'home'
    if home.is_symlink():
        raise ValueError('provider_home_untrusted')
    for relative, digest in data['files'].items():
        target = home / relative
        if not target.resolve().is_relative_to(home.resolve()) or fingerprint(private_read(target).decode('utf8')) != digest:
            raise ValueError('provider_file_changed')
    source, digest = selected_token(provider, home, data['model'])
    if source != data['source'] or digest != data['credential_hash']:
        raise ValueError('provider_sdk_credential_mismatch')
    if provider != 'zai':
        native = json.loads(private_read(home / PATHS[provider]))
        stripped, expires, proactive = access_only(provider, native)
        if native != stripped or data.get('access_only') is not True or expires != data.get('access_expires_at') or proactive != data.get('proactive_refresh_at'):
            raise ValueError('provider_refresh_material_forbidden')
        if provider == 'claude':
            binary_text = data.get('engine_binary_path')
            version = data.get('engine_version')
            if not isinstance(binary_text, str) or not binary_text or not isinstance(version, str):
                raise ValueError('provider_sdk_native_binary_missing')
            if verify_managed_claude_binary(binary_text, version) != data.get('sdk_version'):
                raise ValueError('provider_sdk_refresh_policy_unverified')
        elif data.get('sdk_version') != sdk_version(provider):
            raise ValueError('provider_sdk_refresh_policy_unverified')
    return data


def home_environment(home, provider):
    env = {'HOME': str(home), 'USERPROFILE': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
           'XDG_DATA_HOME': str(home / '.local/share'), 'XDG_CACHE_HOME': str(home / '.cache'),
           'XDG_STATE_HOME': str(home / '.local/state'), 'APPDATA': str(home / 'AppData/Roaming'),
           'LOCALAPPDATA': str(home / 'AppData/Local'), 'CLAUDE_CONFIG_DIR': str(home / '.claude'),
           'CODEX_HOME': str(home / ('.zcodex' if provider == 'zai' else '.codex'))}
    if provider == 'zai':
        env['ZCODE_HOME'] = str(home / '.zcode')
    if provider == 'go':
        env['OPENCODE_DB'] = str(home / '.local/share/opencode/opencode.db')
    return env


def clean_provider_environment(source=None):
    # AWS_ covers ambient cloud credentials that would otherwise leak into the
    # native SDK environment alongside the provider keys.
    prefixes = ('HUB', 'AGENT_HUB_', 'OPENCODE_', 'ANTHROPIC_', 'OPENAI_', 'CODEX_', 'CLAUDE_', 'ZAI_', 'GO_API_', 'AWS_')
    return {key: value for key, value in (os.environ if source is None else source).items() if not key.startswith(prefixes)}


def go_sdk(home, arguments, text=None):
    env = clean_provider_environment()
    env.update(home_environment(home, 'go'))
    binary = shutil.which('opencode')
    if not binary:
        raise ValueError('provider_sdk_unavailable')
    result = subprocess.run([binary, *arguments, '--standalone'], input=text, text=True, capture_output=True,
                            env=env, timeout=20, check=False)
    if result.returncode or len(result.stdout) > 131072:
        raise ValueError('provider_sdk_auth_unverified')
    database = home / '.local/share/opencode/opencode.db'
    if database.exists():
        if database.is_symlink() or not database.resolve().is_relative_to(home.resolve()):
            raise ValueError('provider_sdk_store_untrusted')
        make_private(database)
    return json.loads(result.stdout) if text is None else None


def native_environment(manifest):
    data = verify(manifest)
    return home_environment(Path(manifest).parent / 'home', data['provider'])


def session_binding(manifest, client, session_id, operation_id, epoch):
    """Recheck that this accepted session is still the one being launched."""
    data = verify(manifest)
    binding = {'session_id': session_id, 'operation_id': operation_id, 'epoch': epoch,
               'account_id': data['account_id']}
    result = client.request('POST', '/api/sessions/' + session_id + '/admission',
                            {'operation_id': operation_id, 'epoch': epoch})
    if any(result.get(key) != value for key, value in binding.items()):
        raise ValueError('central_provider_binding_mismatch')
    now = time.time() * 1000
    if result.get('launch_allowed') is not True or result.get('model') != data['model']:
        raise ValueError('central_provider_binding_mismatch')
    deadline = result.get('runtime_deadline')
    if type(deadline) is not int or not now < deadline <= now + 7200000:
        raise ValueError('central_runtime_deadline_unverified')
    data['runtime_deadline'] = deadline
    return data


def claude_fable_smoke(manifest, timeout=120):
    """Run a bounded Fable prompt against the exact Hub-managed binary."""
    data = verify(manifest)
    if data.get('provider') != 'claude' or data.get('engine_version') is None:
        raise ValueError('provider_sdk_native_binary_missing')
    binary = data['engine_binary_path']
    env = clean_provider_environment()
    env.update(native_environment(manifest))
    env['DISABLE_AUTOUPDATER'] = '1'
    env['CLAUDE_CODE_DISABLE_AUTOUPDATER'] = '1'
    try:
        result = subprocess.run([binary, '--dangerously-skip-permissions', '-p', '--model',
                                 'claude-fable-5-2', '--effort', 'medium', 'say ok'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError('provider_fable_smoke_timeout') from None
    except (OSError, subprocess.SubprocessError):
        raise ValueError('provider_fable_smoke_failed') from None
    output = result.stdout or ''
    if result.returncode or len(output) > 65536 or not re.search(r'(?i)\bok\b', output):
        raise ValueError('provider_fable_smoke_failed')
    _record_claude_fable_smoke(binary, data['engine_version'])
    return True
