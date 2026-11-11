"""Bounded, read-only observations of the canonical Linux Claude login.

Only absent access AND refresh credentials, with no known alternate source
for an exact checked workspace, are negative. Everything else is unverified: this module never proves login,
checks expiry, invokes an engine, refreshes credentials, or logs file contents.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tomllib
import time

UNKNOWN = 'unknown'
MISSING = 'missing'
FILE_LIMIT = 262144
ALTERNATE_ENV = ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE',
                 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL')


def _alternate_environment(environment):
    # Unknown provider overrides may select a backend this observer cannot
    # inspect. They never turn into a false missing-login assertion.
    return any(value for name, value in environment.items()
               if name in ALTERNATE_ENV or name.startswith(('CLAUDE_', 'ANTHROPIC_')))


def _read(path, *, credentials=False):
    """Pin directory components and refuse links, FIFOs and unbounded input."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('unknown_auth_location')
    fds = []
    try:
        fds.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY))
        for part in path.parts[1:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            fds.append(fd)
            info = os.fstat(fd)
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o002:
                raise ValueError('untrusted_auth_location')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
        fds.append(fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_nlink != 1 or info.st_size > FILE_LIMIT or info.st_mode & 0o002
                or (credentials and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise ValueError('untrusted_auth_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise ValueError('auth_file_too_large')
        return raw
    except FileNotFoundError:
        return None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_auth_field')
        result[key] = value
    return result


def _decode_json(raw):
    if raw is None:
        return {}
    def invalid_constant(_value):
        raise ValueError('invalid_auth_json')
    value = json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('unknown_auth_shape')
    return value


def _json(path, *, credentials=False):
    return _decode_json(_read(path, credentials=credentials))


def _alternative(data, keys):
    for key in keys:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError('unknown_auth_configuration')
        if value:
            return True
    return False


def _native_backend(binary):
    # A wrapper can change HOME/config or supply auth after this observation.
    # Only the supported native Linux CLI backend has a known default here.
    path = Path(binary).resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        return (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                and not info.st_mode & 0o002 and os.read(fd, 4) == b'\x7fELF')
    finally:
        os.close(fd)


def _default_claude_auth_state(environment=None, *, platform=None):
    """Return only missing/unknown for the actual default local execution source.

    Overrides and platform keychain backends are deliberately unknown. The
    caller must supply the same sanitized environment used for native spawn.
    """
    try:
        environment = os.environ if environment is None else environment
        if (sys.platform if platform is None else platform) != 'linux':
            return UNKNOWN
        if _alternate_environment(environment):
            return UNKNOWN
        home = Path(environment.get('HOME', ''))
        if not home.is_absolute():
            return UNKNOWN
        config_home = Path(environment.get('XDG_CONFIG_HOME') or home / '.config')
        config_path = Path(environment.get('APLEXER_CONFIG') or config_home / 'aplexer/config.toml')
        # A custom caller configuration can select a different profile/backend.
        if config_home != home / '.config' or config_path != home / '.config/aplexer/config.toml':
            return UNKNOWN
        raw = _read(config_path)
        config = tomllib.loads(raw.decode('utf8')) if raw is not None else {}
        if type(config.get('version', 1)) is not int or config.get('version', 1) != 1 or 'default_profile' in config:
            return UNKNOWN
        # Both local Claude launch modes use literal `a start -- <child>`.
        # Its outer default engine still merges env/env_unset before the child;
        # inspecting only engines.claude would miss a shell HOME/config override.
        # Restrict negative observations to the understood, unchanged shell
        # environment. Custom defaults/profiles remain unknown, never invalid.
        if (config.get('default_engine', 'shell') != 'shell' or 'shell' in config
                or environment.get('APLEXER_SHELL')):
            return UNKNOWN
        outer = config.get('engines', {}).get('shell', {})
        if (not isinstance(outer, dict) or not isinstance(outer.get('env', {}), dict)
                or not isinstance(outer.get('env_unset', []), list)
                or outer.get('env') or outer.get('env_unset')):
            return UNKNOWN
        engine = config.get('engines', {}).get('claude', {})
        if (not isinstance(engine, dict) or not isinstance(engine.get('env', {}), dict)
                or not isinstance(engine.get('env_unset', []), list)
                or engine.get('env') or engine.get('env_unset')
                or engine.get('task_argv') not in (None, [], ['-p'])
                or engine.get('skip_permissions_argv') not in (None, [], ['--dangerously-skip-permissions'])):
            return UNKNOWN
        command = engine.get('command', ['claude'])
        binary = shutil.which('claude', path=environment.get('PATH', ''))
        if (not isinstance(command, list) or len(command) != 1 or not isinstance(command[0], str)
                or not binary or shutil.which(command[0], path=environment.get('PATH', '')) != binary
                or not _native_backend(binary)):
            return UNKNOWN
        directory = home / '.claude'
        for path in (home / '.claude.json', directory / 'settings.json', directory / 'settings.local.json'):
            if not _settings_clear(_json(path)):
                return UNKNOWN
        data = _json(directory / '.credentials.json', credentials=True)
        if _alternative(data, ('apiKey', 'primaryApiKey', 'anthropicApiKey')):
            return UNKNOWN
        oauth = data.get('claudeAiOauth', {})
        if not isinstance(oauth, dict):
            return UNKNOWN
        material = [oauth.get(key) for key in ('accessToken', 'refreshToken')]
        if any(value is not None and not isinstance(value, str) for value in material):
            return UNKNOWN
        return UNKNOWN if any(value and value.strip() for value in material) else MISSING
    except Exception:
        # Observation failure is never a launch/heartbeat gate or a secret log.
        return UNKNOWN


def _settings_clear(settings):
    if _alternative(settings, ('apiKeyHelper', 'primaryApiKey', 'apiKey', 'anthropicApiKey')):
        return False
    extra = settings.get('env', {})
    return (isinstance(extra, dict) and all(isinstance(value, str) for value in extra.values())
            and not _alternate_environment(extra)
            and not any(extra.get(name) for name in ('HOME', 'USERPROFILE', 'XDG_CONFIG_HOME')))


def local_claude_missing_contexts(workspaces, modes, environment=None, *, platform=None,
                                  now=None, filesystem_root=Path('/')):
    """Observe at most four exact absolute workspaces; never assert global auth.

    A fresh marker describes only this checked cwd and canonical local backend.
    Project/ancestor or managed settings, profiles, refreshable credentials and
    any unreadable/unsafe configuration remain unknown. No helpers are executed.
    filesystem_root is an offline test seam; production always checks through /.
    """
    try:
        if _default_claude_auth_state(environment, platform=platform) != MISSING:
            return []
        root = Path(filesystem_root)
        if not root.is_absolute() or root.resolve(strict=True) != root:
            return []
        observed = int(time.time() if now is None else now)
        if observed < 1 or observed > 9999999999:
            return []
        cache = {}
        budget = 1024 * 1024
        def clear(path):
            nonlocal budget
            if path in cache:
                return cache[path]
            raw = _read(path)
            budget -= len(raw) if raw is not None else 0
            if budget < 0:
                raise ValueError('observation_budget')
            cache[path] = _settings_clear(_decode_json(raw))
            return cache[path]
        # Managed policy can provide auth outside user/project configuration.
        if not clear(root / 'etc/claude-code/managed-settings.json'):
            return []
        # Additional managed fragments are unknown; do not scan their contents.
        try:
            (root / 'etc/claude-code/managed-settings.d').lstat()
        except FileNotFoundError:
            pass
        else:
            return []
        result = []
        seen = set()
        if not isinstance(workspaces, (list, tuple)):
            return []
        for value in workspaces[:4]:
            try:
                if not isinstance(value, str) or not value.startswith('/') or len(value.encode('utf8')) > 4096:
                    continue
                path = Path(value)
                if value != str(path) or path in seen or path.resolve(strict=True) != path or not path.is_dir() or not path.is_relative_to(root):
                    continue
                seen.add(path)
                current = path
                for _ in range(16):
                    if not all(clear(current / '.claude' / name) for name in ('settings.json', 'settings.local.json')):
                        break
                    if current == root:
                        digest = base64.urlsafe_b64encode(hashlib.sha256(json.dumps(
                            ['hub2-local-auth-context-v1', value], ensure_ascii=False,
                            separators=(',', ':')).encode('utf8')).digest()).decode('ascii').rstrip('=')
                        result.extend('auth:missing:l:' + mode + ':claude:' + digest + ':' + str(observed)
                                      for mode in ('h', 'i') if mode in modes)
                        break
                    current = current.parent
            except Exception:
                continue  # A failed context remains unknown, including budget exhaustion.
        return result
    except Exception:
        return []
