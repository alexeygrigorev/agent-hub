"""Managed engine installation. No credentials in argv, profile TOML, logs or receipts."""
from __future__ import annotations
import contextlib
import datetime as dt
import json
import uuid
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
import threading
import time
import tomllib
import zipfile

from . import bus, config, engine_versions, hook_command, hook_trust, private_fs, state_hook_commands

LABEL = re.compile(r'[a-z][a-z0-9_-]{0,47}\Z')
REV = re.compile(r'[a-f0-9-]{36}\Z')
PACKAGES = {
    'codex': ('@openai/codex', 'codex', 'codex'),
    'claude': ('@anthropic-ai/claude-code', 'claude', 'claude'),
    'gemini': ('@google/gemini-cli', 'gemini', 'gemini'),
    # OpenCode 2.x (`@opencode/cli`): the only line with `auth export|import`, i.e. the
    # Hub-central device login. opencode-ai 1.x cannot import it (no such subcommand).
    'go': ('@opencode/cli', 'opencode', 'opencode'),
    'zai': ('codex-zcode', 'zcodex', 'codex'),
    # Native binary from the reviewed release pin, never npm (intake 5814e109 AC8).
    'antigravity': ('antigravity-cli', 'agy', 'antigravity'),
}
GO_CLI_VERSION = '2.0.32'  # exact pin of @opencode/cli; the bundle version of a go account is ignored
ANTIGRAVITY_ONBOARDING = '.gemini/antigravity-cli/cache/onboarding.json'
ANTIGRAVITY_ONBOARDING_JSON = json.dumps({'consumerOnboardingComplete': True,
                                          'enterpriseOnboardingComplete': False,
                                          'onboardingComplete': True}, indent=2) + '\n'
ANTIGRAVITY_TOKEN = '.gemini/antigravity-cli/antigravity-oauth-token'
OPENCODE_DEVICE_EXPORT = '.local/share/opencode-hub/device-auth.json'
OPENCODE_QUSE_AUTH = '.local/share/opencode/auth.json'
ENV = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
       'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENCODE_API_KEY',
       'ZAI_API_KEY', 'ZAI_BASE_URL', 'ZAI_TOKEN'}
BEGIN = '# BEGIN HUB MANAGED ENGINES\n'
END = '# END HUB MANAGED ENGINES\n'


def root():
    explicit = os.environ.get('AGENT_HUB_MANAGED_DIR')
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


# Fixed relative credential sidecar next to a bundle receipt. The name is set
# by the host, never by bundle data, so a receipt cannot point it elsewhere
# (intake 935b266b AC2).
CREDENTIALS_FILE = 'credentials.json'


def load_bundle(dest):
    """Resolve a local receipt's fixed private credential reference in memory.

    Legacy receipts (full credential content, no reference) stay readable
    until their next successful sync migrates them. Never accept an arbitrary
    path from a receipt, and never include input in errors.
    """
    dest = Path(dest)
    receipt = read_json(dest / 'bundle.json')
    if not isinstance(receipt, dict) or 'credentials_file' not in receipt:
        return receipt
    if (receipt['credentials_file'] != CREDENTIALS_FILE
            or receipt.get('files') != {} or receipt.get('env') != {}):
        raise ValueError('Invalid managed credential reference')
    stored = read_json(dest / CREDENTIALS_FILE)
    if not isinstance(stored, dict) or set(stored) != {'files', 'env'}:
        raise ValueError('Invalid managed credential store')
    bundle = {k: v for k, v in receipt.items() if k != 'credentials_file'}
    bundle['files'] = stored['files']
    bundle['env'] = stored['env']
    validate(bundle)
    return bundle


def _profile_command_runs_this_release(label, path=None):
    """False only when the aplexer profile for ``label`` pins another host release.

    The profile command embeds the host release path (module_command). A release
    from before the split receipt cannot read it: its validate() sees empty
    files and every Hub-launched session dies before the engine starts with an
    empty stderr (intake 0e1b61c9, antigravity, 2026-11-03). Unknown or
    unreadable profile state is treated as current.
    """
    try:
        path = Path(path) if path else profile_path()
        text = path.read_text(encoding='utf-8')
        section = re.search(r'(?ms)^\[profiles\.' + re.escape('hub-' + label) + r'\]\s*$(.*?)(?=^\[|\Z)', text)
        if not section:
            return True
        command = re.search(r'(?m)^command\s*=\s*(\[.*\])\s*$', section.group(1))
        if not command:
            return True
        here = str(Path(__file__).resolve().parents[1])
        return any(repr(here) in part for part in json.loads(command.group(1)) if isinstance(part, str))
    except (OSError, ValueError, TypeError):
        return True


def save_bundle(dest, bundle):
    """Write credentials first; publish a secret-free receipt only after success.

    While the label's aplexer profile still runs an older host release, keep the
    legacy full receipt: that release cannot resolve ``credentials_file``. The
    next sync, after profiles() re-pins the command, migrates it.
    """
    dest = Path(dest)
    write_json(dest / CREDENTIALS_FILE, {'files': bundle['files'], 'env': bundle['env']})
    write_json(dest / 'bundle.json', _receipt_form(bundle))


def _receipt_form(bundle):
    """The receipt this host may publish now: split, unless an older release still runs the profile."""
    return _split_receipt(bundle) if _profile_command_runs_this_release(bundle['label']) else bundle


def _split_receipt(bundle):
    """Secret-free receipt form of a bundle: metadata plus the fixed reference."""
    receipt = {k: v for k, v in bundle.items() if k not in ('files', 'env')}
    receipt['files'] = {}
    receipt['env'] = {}
    receipt['credentials_file'] = CREDENTIALS_FILE
    return receipt


def native_antigravity_token_path():
    """The host's own agy login file (%USERPROFILE%\\.gemini\\... on Windows)."""
    return Path.home().joinpath(*ANTIGRAVITY_TOKEN.split('/'))


def sync_native_antigravity(bundle):
    """Install the central store's Antigravity token where the host's own `agy` reads it.

    Hosts never log in: the maintained launcher's admission probe and any native
    run use the default home, which has no token unless the store provides one
    (intake 5814e109: no token -> agy opened an OAuth browser tab every probe).
    A local file is replaced only when it is missing or still exactly what this
    sync wrote last; a token the engine rotated locally is never clobbered.
    Returns a non-secret status string.
    """
    if bundle.get('provider') != 'antigravity':
        return 'not_antigravity'
    token = (bundle.get('files') or {}).get(ANTIGRAVITY_TOKEN)
    if not isinstance(token, str) or not token:
        return 'no_store_credential'
    import hashlib
    raw = token.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    target = native_antigravity_token_path()
    marker = root() / 'native-antigravity.json'
    last = (read_json(marker, {}) or {}).get('sha256')
    try:
        current = target.read_bytes() if target.is_file() else None
    except OSError:
        current = None
    if current == raw:
        status = 'current'
    elif current is not None and hashlib.sha256(current).hexdigest() != last:
        return 'local_token_kept'
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + '.hub-%d.tmp' % os.getpid())
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
        fd = os.open(staging, flags, 0o600)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(raw)
            os.replace(staging, target)
        finally:
            if staging.exists():
                staging.unlink()
        status = 'installed'
    if last != digest:
        write_json(marker, {'sha256': digest})
    return status


def _opencode_device_export(text):
    """Accept only a native device-login export; legacy auth.json and API keys stay refused."""
    try:
        rows = json.loads(text)
    except ValueError:
        rows = None
    active = [r for r in rows if isinstance(r, dict) and r.get('active') is True and r.get('integrationID') == 'opencode'] if isinstance(rows, list) else []
    value = active[0].get('value') if len(active) == 1 and isinstance(active[0].get('value'), dict) else {}
    if not (value.get('type') == 'oauth' and value.get('methodID') == 'device' and value.get('access') and value.get('refresh')):
        raise ValueError('OpenCode imports only a native device login export (opencode auth export opencode); not API keys or legacy auth.json')
    return text


def opencode_device_access_token(bundle=None, home=None):
    """Active OpenCode device-login access token from the Hub-delivered export."""
    if isinstance(bundle, dict) and bundle.get('provider') != 'go':
        return None
    raw = (bundle.get('files') or {}).get(OPENCODE_DEVICE_EXPORT) if isinstance(bundle, dict) else None
    if (not isinstance(raw, str) or not raw) and home is not None:
        try:
            raw = private_fs.read_bytes(Path(home) / OPENCODE_DEVICE_EXPORT, 32768, private=False).decode('utf-8')
        except (OSError, UnicodeDecodeError, ValueError):
            raw = None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        rows = json.loads(raw)
    except ValueError:
        return None
    active = [r for r in rows if isinstance(r, dict) and r.get('active') is True
              and r.get('integrationID') == 'opencode'] if isinstance(rows, list) else []
    value = active[0].get('value') if len(active) == 1 and isinstance(active[0].get('value'), dict) else {}
    token = value.get('access')
    return token if isinstance(token, str) and token else None


def _ensure_opencode_quse_auth(bundle, home):
    """Materialize the isolated managed-home auth.json that `quse go --json` reads.

    OpenCode 2.x itself runs against the separate `opencode-device-<label>/runtime`
    SQLite store (`XDG_DATA_HOME` / `OPENCODE_DB`), so it never reads `home`'s
    `.local/share/opencode/auth.json`. The maintained launcher's managed-account
    admission (`fetch_managed_quse`) runs `quse go --json` with `HOME=dest/home`
    and `XDG_DATA_HOME` stripped, which otherwise fails with `no-credentials`.
    """
    token = opencode_device_access_token(bundle, home)
    if not token or home is None:
        return False
    target = Path(home) / OPENCODE_QUSE_AUTH
    expected = json.dumps({'opencode-go': {'type': 'api', 'key': token}})
    try:
        current = target.read_text(encoding='utf-8')
    except OSError:
        current = None
    if current == expected:
        return True
    home_path = Path(home)
    for directory in (home_path, home_path / '.local', home_path / '.local' / 'share', target.parent):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        else:
            with private_fs.parent(directory / '.private', create=True, owned=True):
                pass
    private_fs.atomic_write(target, expected.encode('utf-8'), 32768, engine_owned=True)
    if os.name != 'nt':
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return True


def opencode_device_default_path():
    return root() / 'opencode-device-default.json'


def sync_opencode_device(bundle, native=None):
    """Install the store's OpenCode device login into a Hub-owned per-account store.

    The stored `opencode auth export` JSON is imported with the native CLI into
    a Hub-owned source store (never edited by hand, never copied host to host);
    the default `opencode_auth` configuration pointing at it is recorded for
    `fleet.load_settings`, which uses it only where subscriptions.json sets no
    explicit one (intake 5814e109 AC3). Returns a non-secret status string.
    """
    if bundle.get('provider') != 'go':
        return 'not_go'
    export = (bundle.get('files') or {}).get(OPENCODE_DEVICE_EXPORT)
    if not isinstance(export, str) or not export:
        return 'no_store_credential'
    import hashlib
    from . import opencode_auth
    base = root() / ('opencode-device-' + bundle['label'])
    dest = root() / (bundle['label'] + '-' + bundle['revision'])
    cfg = {'binary': engine_argv(bundle, dest)[0], 'source_data': str(base / 'source'),
           'source_config': str(base / 'source-config'), 'target_data': str(base / 'runtime')}
    opencode_auth.validate(cfg)
    for directory in (base, *(Path(cfg[k]) for k in ('source_data', 'source_config', 'target_data'))):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            os.chmod(directory, 0o700)
        else:
            # Windows: mkdir inherits ambient ACEs, so the first private_fs read
            # of the receipt failed 'Private owner/protected DACL required' and
            # the central OpenCode login was never imported (laptop/Tabo/desktop:
            # engine_auth_expired 'device login configuration required').
            # owned=True re-protects the owner-only leaf, repairing old dirs too.
            with private_fs.parent(directory / '.private', create=True, owned=True):
                pass
    digest = hashlib.sha256(export.encode('utf-8')).hexdigest()
    receipt = base / 'store-receipt.json'
    if (read_json(receipt, {}) or {}).get('sha256') == digest and (Path(cfg['source_data']) / 'opencode' / 'opencode.db').is_file():
        status = 'current'
    else:
        rows = json.loads(export)
        if not isinstance(rows, list):
            raise ValueError('Invalid OpenCode device export')
        (native or opencode_auth._native)(cfg, ['auth', 'import'], opencode_auth.environment(cfg, source=True),
                                          json.dumps(rows).encode('utf-8'))
        write_json(receipt, {'sha256': digest})
        status = 'imported'
    write_json(opencode_device_default_path(), cfg)
    _ensure_opencode_quse_auth(bundle, dest / 'home')
    return status


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


def _windows_hooks():
    return sys.platform == 'win32'


def install_aplexer_state_hooks(bundle=None, home=None):
    """Install aplexer state-report hooks into managed engine home.

    Ensures .codex/hooks.json (and .zcodex/hooks.json for zai) exists with
    aplexer state-report working/idle hooks, matching ~/.codex/hooks.json.
    Installed for EVERY managed engine home (Codex at least) and re-applied
    idempotently on re-bootstrap, so interactive managed sessions report
    idle/waiting and Hub sends can be delivered from the aplexer inbox.
    """
    if home is None:
        return
    home = Path(home)
    provider = bundle.get('provider') if isinstance(bundle, dict) else None

    # State hooks for every managed home: .codex/hooks.json always, plus
    # .zcodex/hooks.json for zai. Other providers (claude/gemini/go) still
    # get .codex/hooks.json so any Codex-family interactive session in that
    # home reports state; installing it is harmless when unused.
    codex_home = home / '.codex'
    codex_hooks = codex_home / 'hooks.json'

    template_hooks = None
    user_codex_hooks = Path.home() / '.codex/hooks.json'
    # On Windows codex/zcodex run hooks through PowerShell, where the bash
    # `|| true` / `2>/dev/null` forms are a parse error (every prompt shows
    # 'Hook failed: hook exited with code 1'). Never copy a user template there.
    if not _windows_hooks() and user_codex_hooks.is_file():
        try:
            template_hooks = json.loads(user_codex_hooks.read_text(encoding='utf-8'))
        except Exception:
            template_hooks = None

    if not template_hooks or not isinstance(template_hooks, dict) or 'hooks' not in template_hooks:
        a_cmd = hook_command.tool_command('a')
        aplexer_cmd = hook_command.tool_command('aplexer')
        if _windows_hooks():
            _exe = lambda name: shutil.which(name) or str(Path.home() / '.local' / 'bin' / name)
            state_cmd = lambda st: state_hook_commands.hook_command(_exe('a'), 'state-report', st)
            ctx_cmd = state_hook_commands.hook_command(
                _exe('aplexer'), 'context', 'hook', '--engine', 'codex', quiet=True)
        else:
            state_cmd = lambda st: f"{a_cmd} state-report {st} || true"
            ctx_cmd = f"{aplexer_cmd} context hook --engine codex 2>/dev/null || true # aplexer-managed-awareness-hook-v1"
        template_hooks = {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "command": ctx_cmd,
                                "timeout": 30,
                                "type": "command"
                            }
                        ]
                    }
                ],
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "command": state_cmd('working'),
                                "type": "command"
                            }
                        ]
                    },
                    {
                        "hooks": [
                            {
                                "command": ctx_cmd,
                                "timeout": 30,
                                "type": "command"
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "command": state_cmd('idle'),
                                "type": "command"
                            }
                        ]
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "command": state_cmd('working'),
                                "type": "command"
                            }
                        ]
                    },
                    {
                        "hooks": [
                            {
                                "command": ctx_cmd,
                                "timeout": 30,
                                "type": "command"
                            }
                        ]
                    }
                ]
            }
        }

    if not _windows_hooks():
        template_hooks = hook_command.throttle_post_tool_use_hooks(template_hooks)
    content_bytes = json.dumps(template_hooks, indent=2).encode('utf-8')
    with private_fs.parent(codex_hooks, create=True):
        pass
    private_fs.atomic_write(codex_hooks, content_bytes, 65536)
    hook_trust.refresh(codex_home / "config.toml", codex_hooks, template_hooks)
    if os.name != 'nt':
        try:
            os.chmod(codex_hooks, 0o600)
        except OSError:
            pass

    if provider == 'zai':
        zcodex_home = home / '.zcodex'
        zcodex_hooks = zcodex_home / 'hooks.json'
        with private_fs.parent(zcodex_hooks, create=True):
            pass
        private_fs.atomic_write(zcodex_hooks, content_bytes, 65536)
        hook_trust.refresh(zcodex_home / "config.toml", zcodex_hooks, template_hooks)
        if os.name != 'nt':
            try:
                os.chmod(zcodex_hooks, 0o600)
            except OSError:
                pass


def set_toml_top_level_keys(content: str, keys: dict[str, str | bool]) -> str:
    """Set top-level TOML strings and booleans without disrupting existing tables."""
    content = repair_toml_duplicate_tables(content)
    sections = _toml_sections(content)
    header = sections[0][1]
    rest = ''.join(section for _, section in sections[1:])

    for key, val in keys.items():
        pattern = re.compile(r'^[ \t]*' + re.escape(key) + r'[ \t]*=.*$', re.M)
        if isinstance(val, bool):
            toml_value = 'true' if val else 'false'
        elif isinstance(val, str):
            toml_value = json.dumps(val, ensure_ascii=False)
        else:
            raise TypeError(f'unsupported TOML top-level value for {key}: {type(val).__name__}')
        new_entry = f'{key} = {toml_value}'
        header, replaced = _replace_toml_header_key(header, pattern, new_entry)
        if not replaced:
            if header and not header.endswith('\n'):
                header += '\n'
            header = header + new_entry + '\n'

    if rest:
        header = header.rstrip('\n') + '\n\n'
        return header + rest
    return header.rstrip('\n') + '\n'


def _toml_header_path(line: str):
    """Dotted key path of a ``[table]`` header in any TOML quoting style, else None."""
    stripped = line.strip()
    if not stripped.startswith('[') or stripped.startswith('[['):
        return None
    try:
        node = tomllib.loads(stripped + '\n')
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        (key, node), = node.items()
        path.append(key)
    return tuple(path) if not node else None


def _toml_array_table_header(line: str) -> bool:
    """Whether a line is a valid ``[[array-of-tables]]`` header."""
    stripped = line.strip()
    if not stripped.startswith('[['):
        return False
    try:
        node = tomllib.loads(stripped + '\n')
    except tomllib.TOMLDecodeError:
        return False
    while isinstance(node, dict) and len(node) == 1:
        (key, node), = node.items()
    return isinstance(node, list) and len(node) == 1 and isinstance(node[0], dict)


def _toml_sections(content: str):
    """Split content at table headers, ignoring header-like multiline-string text."""
    sections = [[None, '']]
    multiline: str | None = None
    for line in content.splitlines(keepends=True):
        path = _toml_header_path(line) if multiline is None else None
        array_header = path is None and multiline is None and _toml_array_table_header(line)
        if path is not None or array_header:
            sections.append([path, line])
        else:
            sections[-1][1] += line
        multiline = _toml_multiline_state(line, multiline)
    return sections


def _toml_multiline_state(line: str, state: str | None) -> str | None:
    """Track TOML multiline strings so their contents cannot look like headers."""
    i = 0
    while i < len(line):
        if state == 'basic':
            if line[i] == '\\':
                # Escaped quotes cannot close a multiline basic string.
                i += 2
            elif line.startswith('"""', i):
                state = None
                i += 3
            else:
                i += 1
            continue
        if state == 'literal':
            if line.startswith("'''", i):
                state = None
                i += 3
            else:
                i += 1
            continue

        char = line[i]
        if char == '#':
            break
        if char == '"':
            if line.startswith('"""', i):
                state = 'basic'
                i += 3
            else:
                # Skip a single-line basic string, honoring escapes so quote
                # characters inside it cannot start a multiline string.
                i += 1
                while i < len(line):
                    if line[i] == '\\':
                        i += 2
                    elif line[i] == '"':
                        i += 1
                        break
                    else:
                        i += 1
            continue
        if char == "'":
            if line.startswith("'''", i):
                state = 'literal'
                i += 3
            else:
                i += 1
                while i < len(line) and line[i] != "'":
                    i += 1
                i += 1
            continue
        i += 1
    return state


def _replace_toml_header_key(header: str, pattern: re.Pattern, new_entry: str) -> tuple[str, bool]:
    """Replace matching top-level assignments outside multiline strings."""
    lines = header.splitlines(keepends=True)
    state: str | None = None
    matches: list[int] = []
    for index, line in enumerate(lines):
        if state is None and pattern.match(line):
            matches.append(index)
        state = _toml_multiline_state(line, state)

    # Apply from the end so removing a replaced multiline value's continuation
    # lines cannot shift any earlier match.
    for index in reversed(matches):
        original = lines[index]
        state = _toml_multiline_state(original, None)
        end = index + 1
        while state is not None and end < len(lines):
            state = _toml_multiline_state(lines[end], state)
            end += 1
        if end > index + 1:
            del lines[index + 1:end]
        newline = '\n' if original.endswith('\n') else ''
        lines[index] = new_entry + newline
    return ''.join(lines), bool(matches)


def repair_toml_duplicate_tables(content: str, prefix: str | None = None) -> str:
    """Normalize EOLs and drop repeated TOML tables, keeping the first of each.

    Codex rewrites config.toml with CRLF and its own key quoting. Parsing each
    header makes duplicate detection independent of EOLs and quote style. If
    prefix is supplied, only matching table headers are collapsed.
    """
    content = content.replace('\r\n', '\n')
    seen: set[tuple] = set()
    out: list[str] = []
    for path, section in _toml_sections(content):
        header = section.splitlines()[0].strip() if section else ''
        if path is None or (prefix is not None and not header.startswith(prefix)):
            out.append(section)
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append(section)
    return ''.join(out)


def collapse_duplicate_toml_tables(content: str, prefix: str) -> str:
    """Compatibility wrapper for the TOML-aware table repair helper."""
    return repair_toml_duplicate_tables(content, prefix=prefix)


def ensure_toml_trusted_projects(content: str, paths: list[str]) -> str:
    """Ensure paths have [projects."<path>"] trust_level = "trusted" in TOML content."""
    res = repair_toml_duplicate_tables(content)
    for p in paths:
        if not p:
            continue
        try:
            p_str = str(Path(p).resolve())
        except Exception:
            p_str = str(p)
        want = ('projects', p_str)
        sections = _toml_sections(res)
        idx = next((i for i, (path, _) in enumerate(sections) if path == want), None)
        if idx is not None:
            # An existing table may explicitly be untrusted or lack the key.
            # Upgrade only this authorized path, retaining sibling settings.
            header, _, section = sections[idx][1].partition('\n')
            trust = re.compile(r'^[ \t]*trust_level[ \t]*=.*$', re.M)
            if trust.search(section):
                section = trust.sub('trust_level = "trusted"', section, count=1)
            else:
                section = 'trust_level = "trusted"\n' + section
            sections[idx][1] = header + '\n' + section
            res = ''.join(t for _, t in sections)
            continue
        if res and not res.endswith('\n'):
            res += '\n'
        res += f'\n[projects.{json.dumps(p_str)}]\ntrust_level = "trusted"\n'
    return res


def ensure_toml_trusted_hooks(content: str, hooks_file: Path) -> str:
    """Trust only handlers present in the actual generated hooks file."""
    try:
        hooks_file = Path(hooks_file).resolve()
        raw = read_engine_file(hooks_file)
        if raw is None:
            return content
        hooks_doc = json.loads(raw.decode('utf-8'))
    except Exception:
        return content
    return hook_trust.render(repair_toml_duplicate_tables(content), str(hooks_file), hooks_doc)


def read_engine_file(target, limit=131072):
    """Read a managed-home file the engine CLI may have rewritten itself.

    Codex replaces .codex/config.toml on launch: on Windows the new file
    inherits the owner-only ACE of its private parent instead of a protected
    DACL, on POSIX it gets the engine umask. Accept same-owner, owner-only
    files (POSIX mode re-tightened) instead of failing install, which used to
    withdraw the route on every launch (intake 6cbb862a). Returns None when
    absent; a foreign owner/ACE still raises.
    """
    try:
        if os.name == 'nt':
            try:
                return private_fs.read_bytes(target, limit)
            except PermissionError:
                return private_fs.read_bytes(target, limit, private=False)
        preserve_managed_file(target)
        return private_fs.read_bytes(target, limit)
    except FileNotFoundError:
        return None


def _engine_config(path, parse, default):
    """Current engine config; unreadable content becomes ``default``, unsafe files raise."""
    try:
        raw = read_engine_file(path)
    except ValueError:  # oversized: replaced, as before
        return None, default
    if raw is None:
        return None, default
    try:
        return raw, parse(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return raw, default


def _write_engine_config(path, raw, current):
    """Write only real changes, so an engine refresh never triggers a rewrite loop."""
    if raw == current:
        return
    private_fs.atomic_write(path, raw, 131072, engine_owned=True)
    if os.name != 'nt':
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def claude_project_keys(path):
    """Keys Claude Code may use for a project: native form and forward-slash form (Windows)."""
    native = str(path)
    return list(dict.fromkeys([native, native.replace('\\', '/')]))


def ensure_claude_trusted_projects(config_path, paths):
    """Mark paths trusted in a managed Claude config (projects[<path>].hasTrustDialogAccepted).

    The Claude equivalent of ensure_toml_trusted_projects (intake 7833bf13): a
    Hub-created work tree otherwise opens at the folder-trust dialog and the
    session never runs. The file also holds Claude's own account and session
    state, so it is only rewritten when it parsed as an object (or is absent)
    and something actually changes; oversized or unreadable content is kept.
    """
    try:
        raw = read_engine_file(config_path)
    except ValueError:
        return
    if raw is None:
        config = {}
    else:
        try:
            config = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(config, dict):
            return
    projects = config.setdefault('projects', {})
    if not isinstance(projects, dict):
        return
    changed = False
    # A managed home is never used by a person: a fresh or re-created config
    # must not open Claude at the first-run theme/onboarding screen either.
    if config.get('hasCompletedOnboarding') is not True:
        config['hasCompletedOnboarding'] = True
        changed = True
    if config.get('theme') != 'dark':
        config['theme'] = 'dark'
        changed = True
    for path in paths:
        for key in claude_project_keys(path):
            entry = projects.setdefault(key, {})
            if not isinstance(entry, dict):
                continue
            if entry.get('hasTrustDialogAccepted') is not True:
                entry['hasTrustDialogAccepted'] = True
                changed = True
    if changed:
        _write_engine_config(config_path, json.dumps(config, indent=2).encode('utf-8'), raw)


def install_managed_engine_permissions(bundle=None, home=None, trusted_paths=None):
    """Ensure managed engine home configuration has approval and sandbox prompts disabled.

    Reconciles on every bootstrap/re-bootstrap and execution to match operator config:
    - codex / zai: approval_policy = "never", sandbox_mode = "danger-full-access",
      check_for_update_on_startup = false in .codex/.zcodex config.toml
      plus [projects."<path>"] trust_level = "trusted" for workspaces and operator roots,
      and [hooks.state."..."] trusted_hash to bypass hook review prompts.
    - claude: skipDangerousModePermissionPrompt = True, autoUpdates = False
      in .claude/settings.json (plus DISABLE_AUTOUPDATER=1 in session env).
    - go (opencode): permission.* = "allow", autoupdate = False
      in .config/opencode/opencode.json.
    - antigravity/agy: pinned binary only moves with the reviewed pin; no
      self-update channel is enabled in managed homes.
    """
    if home is None:
        return
    home = Path(home)
    provider = bundle.get('provider') if isinstance(bundle, dict) else None

    # Determine default paths to trust for Codex / Zai
    paths_to_trust = [
        str(Path.home().resolve()),
        str((Path.home() / 'git').resolve()),
        str((Path.home() / 'tmp').resolve()),
        '/tmp',
        '/data/tmp',
    ]
    if trusted_paths:
        paths_to_trust.extend(trusted_paths)

    # Codex and Z.ai (zcodex)
    if provider in ('codex', 'zai') or provider is None:
        if provider == 'codex' or provider is None:
            codex_dir = home / '.codex'
            config_toml = codex_dir / 'config.toml'
            hooks_file = codex_dir / 'hooks.json'
            current, content = _engine_config(config_toml, str, '')
            updated = set_toml_top_level_keys(content, {
                'approval_policy': 'never',
                'sandbox_mode': 'danger-full-access',
                'check_for_update_on_startup': False,
            })
            updated = ensure_toml_trusted_projects(updated, paths_to_trust)
            updated = ensure_toml_trusted_hooks(updated, hooks_file)
            _write_engine_config(config_toml, updated.encode('utf-8'), current)

        if provider in ('zai', None):
            zcodex_dir = home / '.zcodex'
            zconfig_toml = zcodex_dir / 'config.toml'
            zhooks_file = zcodex_dir / 'hooks.json'
            current, content = _engine_config(zconfig_toml, str, '')
            updated = set_toml_top_level_keys(content, {
                'approval_policy': 'never',
                'sandbox_mode': 'danger-full-access',
                'check_for_update_on_startup': False,
            })
            updated = ensure_toml_trusted_projects(updated, paths_to_trust)
            updated = ensure_toml_trusted_hooks(updated, zhooks_file)
            _write_engine_config(zconfig_toml, updated.encode('utf-8'), current)

    # Claude Code
    if provider in ('claude', None):
        claude_dir = home / '.claude'
        settings_path = claude_dir / 'settings.json'
        current, settings = _engine_config(settings_path, json.loads, {})
        if not isinstance(settings, dict):
            settings = {}
        settings['skipDangerousModePermissionPrompt'] = True
        # In-engine self-update prompts must never block a session (AC4):
        # the session env also carries DISABLE_AUTOUPDATER=1.
        settings['autoUpdates'] = False
        perms = settings.setdefault('permissions', {})
        if isinstance(perms, dict):
            perms['defaultMode'] = 'bypassPermissions'
            perms.setdefault('deny', ['AskUserQuestion'])
        # Interactive claude sessions report semantic state through the same
        # hook contract as .codex/hooks.json: UserPromptSubmit/SessionStart ->
        # working, Stop -> durable idle. Claude never reads .codex/hooks.json,
        # so a managed-home claude session without these can never push the
        # fresh state report the aplexer deferred-deliver guard requires and
        # Hub sends deadlock (no delivery -> no turn -> no Stop). The set
        # deliberately has NO Notification entry: claude's idle-reminder
        # notification fires while the session rests and its expiring
        # `waiting` push would clobber Stop's durable `idle` (REPORTED_STATE_
        # STALE_MS is 8s), re-creating the same deadlock.
        # Claude runs hook commands through bash (Git Bash on Windows), so the
        # executable path must be bash-safe: forward slashes, quoted if needed.
        a_cmd = hook_command.tool_command('a')
        hooks = settings.setdefault('hooks', {})
        if isinstance(hooks, dict):
            working_hook = {"hooks": [{"command": hook_command.state_report_command(a_cmd, 'working'),
                                       "type": "command"}]}
            idle_hook = {"hooks": [{"command": hook_command.state_report_command(a_cmd, 'idle'),
                                    "type": "command"}]}
            hooks.setdefault('SessionStart', [dict(working_hook)])
            hooks.setdefault('UserPromptSubmit', [dict(working_hook)])
            hooks.setdefault('Stop', [dict(idle_hook)])
            # Re-bootstrap repairs state-report hooks we generated earlier with
            # a raw native path (backslashes eaten by bash on Windows).
            for groups in hooks.values():
                for group in groups if isinstance(groups, list) else []:
                    entries = group.get('hooks') if isinstance(group, dict) else None
                    for entry in entries if isinstance(entries, list) else []:
                        if isinstance(entry, dict) and 'command' in entry:
                            entry['command'] = hook_command.repair_state_report_command(
                                entry['command'], a_cmd)
        _write_engine_config(settings_path, json.dumps(settings, indent=2).encode('utf-8'), current)
        try:
            ensure_claude_trusted_projects(claude_dir / '.claude.json', paths_to_trust)
        except (OSError, ValueError) as exc:
            # Trust is a convenience: a refused write must not block the launch.
            print(json.dumps({'event': 'claude_trust_skipped', 'type': type(exc).__name__}), file=sys.stderr, flush=True)

    # OpenCode (go)
    if provider in ('go', None):
        opencode_dir = home / '.config' / 'opencode'
        opencode_path = opencode_dir / 'opencode.json'
        current, opencode_conf = _engine_config(opencode_path, json.loads, {})
        if not isinstance(opencode_conf, dict):
            opencode_conf = {}
        perm = opencode_conf.setdefault('permission', {})
        if isinstance(perm, dict):
            perm['*'] = 'allow'
            ext = perm.setdefault('external_directory', {})
            if isinstance(ext, dict):
                ext.setdefault('**', 'allow')
                ext.setdefault('~/git/**', 'allow')
                ext.setdefault('~/tmp/**', 'allow')
                ext.setdefault('/tmp/**', 'allow')
        # In-engine self-update prompts must never block a session (AC4).
        opencode_conf['autoupdate'] = False
        _write_engine_config(opencode_path, json.dumps(opencode_conf, indent=2).encode('utf-8'), current)


def preserve_managed_file(target):
    """Keep runtime refreshes, validating confinement before repairing mode.

    Provider CLIs can rewrite files with their default umask. Open the exact
    regular, singly-linked owner file without following symlinks before chmod;
    never let an existence check hide an unsafe file or parent.
    """
    try:
        if os.name == 'nt':
            return bool(read_engine_file(target))
        with private_fs.parent(target, create=True) as anchor:
            fd = private_fs._open(anchor, target, os.O_RDONLY, private=False)
            try:
                info = os.fstat(fd)
                if os.name != 'nt':
                    if info.st_uid != os.getuid():
                        raise PermissionError('Unsafe managed file owner')
                    os.fchmod(fd, 0o600)
                return info.st_size > 0
            finally:
                os.close(fd)
    except FileNotFoundError:
        return False


ZCODE_CLI_CONFIG = '.zcode/cli/config.json'
ZCODE_PROVIDER_CONFIG = '.zcode/v2/provider_config.json'
ZCODE_PROVIDER_ID = 'zai-coding-plan'
ZCODE_MODEL_ID = 'GLM-5.3-Flash'
ZCODE_CONTEXT_WINDOW = 1000000  # read from the working native home on the desktop (benign field)


def _zcode_goz(bundle):
    try:
        goz = json.loads((bundle.get('files') or {}).get('.config/goz/config.json') or '')
    except (TypeError, ValueError, AttributeError):
        return {}
    return goz if isinstance(goz, dict) else {}


def zcode_token(bundle):
    if not isinstance(bundle, dict) or bundle.get('provider') != 'zai':
        return None
    token = (bundle.get('env') or {}).get('ZAI_API_KEY') or _zcode_goz(bundle).get('zai_token')
    return token if isinstance(token, str) and token else None


def zcode_base_url(bundle):
    base = _zcode_goz(bundle).get('zai_base_url')
    return base if isinstance(base, str) and base else 'https://api.z.ai/api/anthropic'


def zcode_managed_configs(bundle):
    """ZCode's model selection files, generated from the key the store delivers.

    A managed home without them answers "Select a model before continuing"
    (CONFIGURATION_ERROR; intake 5814e109, desktop zai-central). The shapes
    mirror the working native home (schema only, never its values); the key is
    the bundle's own (ZAI_API_KEY env or .config/goz/config.json). Returns
    {relative_path: json_text}, or {} when the bundle holds no usable key.
    """
    token, base = zcode_token(bundle), zcode_base_url(bundle)
    if not token:
        return {}
    cli = {
        'model': 'zai/glm-5.3-flash',
        'provider': {
            ZCODE_PROVIDER_ID: {
                'name': 'Z.AI Coding Plan', 'kind': 'anthropic', 'source': 'custom',
                'options': {'apiKey': token, 'baseURL': base, 'apiKeyRequired': True},
                'models': {ZCODE_MODEL_ID: {'limit': {'context': ZCODE_CONTEXT_WINDOW},
                                            'modalities': {'input': ['text'], 'output': ['text']}}}},
            'zai': {'models': {'glm-5.3-flash': {'name': 'glm-5.3-flash'}}}}}
    provider = {
        'schemaVersion': 1,
        'config': {
            'providerConfigRules': {'providerRules': [{
                'providerId': ZCODE_PROVIDER_ID, 'providerName': 'Z.AI Coding Plan',
                'config': {'group': 'standard-personal', 'access': {'type': 'api-key', 'apiKey': token},
                           'api': {'type': 'anthropic-messages', 'baseUrl': base},
                           'personalModelIds': [ZCODE_MODEL_ID], 'modelOrder': [ZCODE_MODEL_ID]}}]},
            'modelConfigRules': {'providerModelRules': [{
                'modelId': ZCODE_MODEL_ID, 'providerId': ZCODE_PROVIDER_ID,
                'config': {'properties': {'contextWindow': ZCODE_CONTEXT_WINDOW}}}],
                'manualProviderModelRules': []},
            'defaultModelSelection': {'providerId': ZCODE_PROVIDER_ID, 'modelId': ZCODE_MODEL_ID}}}
    return {ZCODE_CLI_CONFIG: json.dumps(cli, indent=1), ZCODE_PROVIDER_CONFIG: json.dumps(provider, indent=1)}


def provision_managed_home(bundle=None, home=None, dest=None):
    """Shared provisioning hook point for managed engine homes.

    Provisions .gitconfig, credentials, state hooks, and permission bypass into home.
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
    from . import credentials
    hub_owned = set(credentials.managed_paths(bundle))
    if isinstance(bundle, dict) and isinstance(bundle.get('files'), dict):
        for relative, value in bundle['files'].items():
            if relative in hub_owned:
                continue
            target = home / relative
            # Same-revision sync/execute must not restore the original refresh
            # token over a rotated live token. A central import gets a new
            # revision/home; missing or truncated files are repaired normally.
            if preserve_managed_file(target):
                continue
            private_fs.atomic_write(target, value.encode(), 32768, engine_owned=True)
            if os.name != 'nt':
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass

    # 2b. ZCode's model selection, generated from the delivered key (never copied from a native home).
    # An existing file already carrying that key is the engine's own state: kept as is.
    for relative, text in zcode_managed_configs(bundle).items():
        target = home / relative
        token = zcode_token(bundle)
        try:
            current = target.read_text(encoding='utf-8')
        except OSError:
            current = None
        if current is not None and token in current:
            continue
        private_fs.atomic_write(target, text.encode(), 32768, engine_owned=True)
        if os.name != 'nt':
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

    # 2c. OpenCode Go quota-probe credentials for managed quse go --json.
    if isinstance(bundle, dict) and bundle.get('provider') == 'go':
        _ensure_opencode_quse_auth(bundle, home)

    # 3. Provision aplexer state hooks
    install_aplexer_state_hooks(bundle, home)

    # 4. Provision engine permissions (approvals disabled, full access)
    install_managed_engine_permissions(bundle, home)

    # 5. Antigravity first-run: the colour-scheme picker and the data-sharing
    # consent otherwise block every interactive agy start on a fresh home. The
    # marker below is what the CLI itself writes after the theme choice
    # (default 'terminal') and the Terms screen with the improve-the-product
    # data-sharing box left UNCHECKED; no consent is recorded or granted here.
    if isinstance(bundle, dict) and bundle.get('provider') == 'antigravity':
        marker = home / ANTIGRAVITY_ONBOARDING
        try:
            current = json.loads(marker.read_bytes())
        except (OSError, ValueError):
            current = None
        # The CLI itself writes onboardingComplete:false on a first headless start, and
        # then fails with 'plan model not specified' (desktop). Only a completed marker
        # is left alone.
        if not (isinstance(current, dict) and current.get('onboardingComplete') is True):
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(json.loads(ANTIGRAVITY_ONBOARDING_JSON))
            # Plain write: the CLI creates cache/ itself with inherited (non-private) ACEs on Windows,
            # which private_fs refuses; this marker is non-secret engine state.
            marker.parent.mkdir(parents=True, exist_ok=True)
            staged = marker.with_name(marker.name + '.' + uuid.uuid4().hex + '.tmp')
            staged.write_text(json.dumps(merged, indent=2) + chr(10), encoding='utf-8')
            os.replace(staged, marker)

    return home




WINDOWS_LOCK_WAIT_S = 900.0
# Under the 30 s fleet runner timeout for the admission subprocess.
ADMISSION_LOCK_WAIT_S = 10.0


def _windows_wait_lock(fd, timeout=None, sleep=None):
    """Block like flock(LOCK_EX) on Windows, bounded by WINDOWS_LOCK_WAIT_S.

    msvcrt LK_LOCK gives up after ten 1s retries, so `hub engines sync` failed
    whenever the session host's own reconcile held the lock through an npm
    install. Poll LK_NBLCK instead until the holder finishes.
    """
    import msvcrt
    import time
    timeout = WINDOWS_LOCK_WAIT_S if timeout is None else timeout
    sleep = sleep or time.sleep
    if os.fstat(fd).st_size == 0:
        os.write(fd, b'0')
    deadline = time.monotonic() + timeout
    while True:
        os.lseek(fd, 0, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            sleep(0.5)


@contextlib.contextmanager
def lock(timeout=None):
    """One reconciler/exec reader; owner-only lock stays on disk across processes."""
    path = root() / 'reconcile.lock'
    with private_fs.parent(path, create=True) as anchor:
        fd = private_fs._open(anchor, path, os.O_CREAT | os.O_RDWR)
        try:
            if os.name == 'nt':
                _windows_wait_lock(fd, timeout)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def validate(bundle):
    if not isinstance(bundle, dict) or not LABEL.fullmatch(bundle.get('label', '')) or not REV.fullmatch(bundle.get('revision', '')) or bundle.get('provider') not in PACKAGES:
        raise ValueError('Invalid managed bundle')
    if bundle['provider'] == 'gemini' and (bundle.get('env') or (
            any(re.search('oauth|credential|token', k, re.I) for k in bundle.get('files', {}))
            if bundle.get('auth_method') == 'machine-login' else
            bundle.get('auth_method') is not None or not isinstance(bundle.get('files', {}).get('.gemini/oauth_creds.json'), str))):
        raise ValueError('Gemini needs machine login or a Hub-held OAuth credential')
    if bundle['provider'] == 'go' and (bundle.get('env') or list(bundle.get('files') or {}) not in ([], [OPENCODE_DEVICE_EXPORT])):
        raise ValueError('OpenCode bundles carry no env and at most the Hub-held device-login export')
    if bundle['provider'] == 'antigravity' and (bundle.get('env') or 'auth_method' in bundle or
                                                list(bundle.get('files') or {}) != [ANTIGRAVITY_TOKEN]):
        raise ValueError('Antigravity needs exactly its Hub-held OAuth token')
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
    override = os.environ.get('APLEXER_CONFIG')
    if override:
        return Path(override)
    # Native aplexer reads %APPDATA% on Windows and XDG elsewhere.
    if sys.platform == 'win32' and os.environ.get('APPDATA'):
        base = Path(os.environ['APPDATA'])
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base / 'aplexer' / 'config.toml'


class LauncherUnavailable(OSError):
    """The managed Windows engine launcher could not be materialized (retryable)."""


def _windows_exe_launcher(command, label, stub=None):
    """Native exe launcher for `a task run --engine opencode --profile hub-<label>`.

    Never returns the bare python command: aplexer would run `python.exe run -c ...`
    ("can't open file '...\run'", exit 2 after 0 s). Proven race (intake: launcher fallback):
    MoveFileExW cannot replace an .exe that a running muse session holds open (ERROR_ACCESS_DENIED),
    so a profile sync during any active run used to fall back to the python form.
    The bytes are deterministic, an identical existing launcher is reused without any write, and when
    the canonical path is busy the new content goes to a content-addressed sibling that nothing can
    be running yet. The previous file is never removed first."""
    import hashlib
    import io
    import zipfile
    if stub is None:
        stub_name = 't64-arm.exe' if platform.machine().lower() in ('arm64', 'aarch64') else 't64.exe'
        candidates = [Path(sys.base_prefix) / 'Lib' / 'site-packages' / 'pip' / '_vendor' / 'distlib' / stub_name]
        try:
            import pip._vendor.distlib as _distlib
            candidates.append(Path(_distlib.__file__).parent / stub_name)
        except Exception:
            pass
        stub = next((c for c in candidates if c.is_file()), None)
    if stub is None:
        raise LauncherUnavailable('no launcher stub available for ' + label)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        # Fixed timestamp: identical code must give identical bytes so a sync can skip the write.
        info = zipfile.ZipInfo('__main__.py', date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, command[2])
    shebang = ('#!"' + command[0] + '"').encode('utf-8') + bytes((13, 10))
    data = stub.read_bytes() + shebang + archive.getvalue()
    path = root() / 'engine-launch' / (label + '.exe')
    try:
        if path.is_file() and path.read_bytes() == data:
            return [str(path), label]
    except OSError:
        pass
    last = None
    for attempt in range(3):
        try:
            with private_fs.parent(path, create=True):
                pass
            private_fs.atomic_write(path, data, 4 * 1024 * 1024)
            return [str(path), label]
        except (OSError, ValueError) as exc:
            last = exc
            time.sleep(0.2 * (attempt + 1))
    # Canonical path busy (a running session holds it open): publish under a new name instead.
    alt = root() / 'engine-launch' / (label + '-' + hashlib.sha256(data).hexdigest()[:12] + '.exe')
    try:
        if not (alt.is_file() and alt.read_bytes() == data):
            private_fs.atomic_write(alt, data, 4 * 1024 * 1024)
        _prune_launchers(alt.parent, label, keep=alt)
        return [str(alt), label]
    except (OSError, ValueError) as exc:
        raise LauncherUnavailable('cannot materialize launcher for %s: %s / %s' % (label, last, exc)) from exc


def _prune_launchers(directory, label, keep):
    """Best-effort removal of superseded content-addressed launchers; busy ones are left alone."""
    for old in Path(directory).glob(label + '-*.exe'):
        if old != keep:
            try:
                old.unlink()
            except OSError:
                pass


def _previous_profile_command(text, name):
    """Launcher argv of an existing `[profiles.<name>]` block, or None when absent or the bare python form."""
    header = '[profiles.' + name + ']'
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != header:
            continue
        for follow in lines[i + 1:]:
            if follow.lstrip().startswith('['):
                break
            key, _, raw = follow.partition('=')
            if key.strip() == 'command':
                try:
                    value = json.loads(raw.strip())
                except ValueError:
                    return None
                if (isinstance(value, list) and value and isinstance(value[0], str)
                        and not value[0].replace(chr(92), '/').rsplit('/', 1)[-1].lower().startswith('python')):
                    return value
                return None
    return None


def _posix_engine_launcher(command, label, windows=False):
    """Command whose first element tolerates the engine subcommand aplexer injects after it.

    `a task run --engine opencode --profile X` builds [command[0], 'run', *command[1:], ...]; with
    command[0] = python that is `python run -c ...` ("can't open file 'run'"). A tiny sh launcher takes
    that argument order and hands it to the same python -c; execute() puts `run` back after the label."""
    import shlex
    if windows:
        # aplexer (Rust) refuses a .cmd as the task child when an argument holds a newline or other
        # batch-unsafe characters, and every real prompt does. Use a native console-script style
        # launcher (pip's distlib stub + shebang + zipped __main__.py): it execs `python <exe> args...`.
        return _windows_exe_launcher(command, label)
    path = root() / 'engine-launch' / (label + '.sh')
    body = '#!/bin/sh' + chr(10) + 'exec ' + shlex.quote(command[0]) + ' -c ' + shlex.quote(command[2]) + ' "$@"' + chr(10)
    try:
        with private_fs.parent(path, create=True):
            pass
        private_fs.atomic_write(path, body.encode('utf-8'), 65536)
        os.chmod(path, 0o700)
    except (OSError, ValueError) as exc:
        raise LauncherUnavailable('cannot materialize launcher for %s: %s' % (label, exc)) from exc
    return [str(path), label]


def module_command(handler, label, managed_dir=None):
    # Profiles run outside the launcher environment, whose .cmd is what puts
    # <release>/package/host on PYTHONPATH (intake 608a2f9a): without an
    # embedded path the -c import dies with ModuleNotFoundError on Windows.
    prefix = 'import sys; sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) + '); '
    if Path(sys.argv[0]).is_file() and zipfile.is_zipfile(sys.argv[0]):
        # The frozen zipapp stays ahead of the checkout copy on sys.path.
        prefix += 'sys.path.insert(0, ' + repr(str(Path(sys.argv[0]).resolve())) + '); '
    m_dir = str(managed_dir or root())
    prefix += 'import os; os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(m_dir) + '; '
    return [sys.executable, '-c', prefix + 'from agent_hub_host.provisioning import ' + handler + '; ' + handler + '()', label]


def _opencode_engine_pin(bundles, is_windows=None):
    """argv pin for aplexer's [engines.opencode], or None to leave it alone.

    On Windows aplexer's built-in bare `opencode` resolves to the npm shim,
    which CreateProcess cannot exec (a task run --engine opencode exits 127,
    intake 608a2f9a); the managed 'go' install ships the real opencode.exe.
    The pin is recomputed on every sync because the revision dir changes per
    release. Linux engines resolve natively, so they are left untouched.
    """
    if is_windows is None:
        is_windows = (os.name == 'nt')
    if not is_windows:
        return None
    for b in bundles:
        if b.get('provider') != 'go':
            continue
        dest = root() / (b['label'] + '-' + b['revision'])
        try:
            executable = executable_path(b, dest, is_windows=True)
        except Exception:
            continue
        if executable.is_file():
            return [str(executable)]
    return None


def profiles(bundles, path=None, is_windows=None):
    """Replace only our marked block; refuse a collision with operator profiles."""
    path = Path(path) if path else profile_path()
    # aplexer's ordinary TOML may originally be 0644. Tighten only this owned file
    # and its containing directory after no-follow ownership checks. On Windows
    # the same applies to an ACL inherited from e.g. MSYS/Cygwin mkdir (Everyone,
    # NULL SID deny ACEs): without tightening, every sync died here before it
    # installed engines or reported host state (intake 8d006dc3).
    with private_fs.parent(path, create=True, private=False, owned=os.name == 'nt') as anchor:
        if os.name != 'nt':
            if os.fstat(anchor).st_uid != os.getuid():
                raise PermissionError('Unowned aplexer directory')
            os.fchmod(anchor, 0o700)
        else:
            from . import win32_fs
            try:
                win32_fs.restrict_file(path)
            except FileNotFoundError:
                pass
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
    old_text = text
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
        if b.get('provider') == 'go':
            try:
                command = _posix_engine_launcher(command, b['label'], windows=(os.name == 'nt' if is_windows is None else bool(is_windows)))
            except LauncherUnavailable:
                # Never publish the bare python form (aplexer runs `python run`, exit 2). Keep the
                # previous launcher entry; with none, fail this sync so the next one retries.
                command = _previous_profile_command(old_text, name)
                if command is None:
                    raise
        block += '\n[profiles.' + name + ']\nengine = ' + json.dumps(PACKAGES[b['provider']][2]) + '\ncommand = ' + json.dumps(command) + '\n'
    # After our block is stripped, any [engines.opencode] left in the file is
    # operator-owned and wins over the managed pin (duplicate tables would not
    # even parse for aplexer).
    text = _drop_stale_release_opencode_pin(text)
    if not re.search(r'(?m)^\s*\[engines\.opencode\]\s*$', text):
        pin = _opencode_engine_pin(bundles, is_windows=is_windows)
        if pin:
            block += '\n[engines.opencode]\ncommand = ' + json.dumps(pin) + '\n'
    private_fs.atomic_write(path, (text.rstrip() + '\n\n' + block + END).encode(), 262144)


_ENGINE_TABLE = re.compile(r'(?m)^\s*\[engines\.opencode\]\s*$')


def _drop_stale_release_opencode_pin(text):
    """Remove an [engines.opencode] table pinned to a Hub release directory.

    `a doctor --fix` (or older Hub code) pins the absolute opencode path found at that moment;
    on Windows that was <agent-hub>/releases/<old sha>/engines/node_modules/opencode-ai/..., which
    kept `a engines` and plain `--engine opencode` on the OLD release after every release switch
    (laptop: 56715fc...). A table whose command lives under an agent-hub releases directory is Hub
    state, not operator state: drop it so the managed pin (current engine) is written instead.
    Tables pointing anywhere else stay operator-owned and untouched."""
    match = _ENGINE_TABLE.search(text)
    if not match:
        return text
    rest = text[match.end():]
    nxt = re.search(r'(?m)^\s*\[', rest)
    body = rest[:nxt.start()] if nxt else rest
    command = re.search(r'(?m)^\s*command\s*=\s*(.*)$', body)
    if not command:
        return text
    normalized = command.group(1).replace('\\\\', '/').replace('\\', '/').lower()
    if '/agent-hub/releases/' not in normalized:
        return text
    end = match.end() + (nxt.start() if nxt else len(rest))
    return text[:match.start()] + text[end:].lstrip('\n')


def _ensure_builtin_provider(res_dir):
    """ZCode reads glm/provider/zcode-builtin.json ('Built-in Provider Config').

    The Linux deb ships that file only as config/provider/zcode-builtin.json (the
    Windows app has both), so a runtime taken from the deb failed with 'ZCode cannot
    locate its Built-in Provider Config' (Windows laptop)."""
    res_dir = Path(res_dir)
    target = res_dir / 'glm' / 'provider' / 'zcode-builtin.json'
    source = res_dir / 'config' / 'provider' / 'zcode-builtin.json'
    if (res_dir / 'glm').is_dir() and not target.is_file() and source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _copy_runtime_resources(src_res_dir, dst_res_dir):
    dst_res_dir.mkdir(parents=True, exist_ok=True)
    for name in ('glm', 'tools', 'model-providers', 'config'):
        src_entry = src_res_dir / name
        if src_entry.is_dir():
            dst_entry = dst_res_dir / name
            if dst_entry.is_dir():
                shutil.rmtree(dst_entry)
            shutil.copytree(src_entry, dst_entry)
    _ensure_builtin_provider(dst_res_dir)


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


def _extract_deb_resources(deb_path, target):
    """Pure-Python `dpkg-deb -x` for opt/ZCode/resources only (works on Windows)."""
    import tarfile
    prefix = 'opt/ZCode/resources/'
    with open(deb_path, 'rb') as f:
        if f.read(8) != b'!<arch>' + chr(10).encode():
            raise ValueError('Invalid ZCode deb')
        while True:
            header = f.read(60)
            if len(header) < 60:
                raise ValueError('ZCode deb has no data archive')
            name = header[:16].decode('ascii', 'replace').strip().rstrip('/')
            size = int(header[48:58].decode('ascii').strip())
            if name.startswith('data.tar'):
                if name not in ('data.tar', 'data.tar.xz', 'data.tar.gz'):
                    raise ValueError('Unsupported ZCode deb compression')
                f.seek(f.tell())
                start = f.tell()
                break
            f.seek(size + (size & 1), 1)
        import io
        buf = io.BytesIO(f.read(size))
    root_dir = Path(target).resolve()
    with tarfile.open(fileobj=buf, mode='r:*') as tar:
        for member in tar:
            rel = member.name[2:] if member.name.startswith('./') else member.name
            if not rel.startswith(prefix) or not (member.isfile() or member.isdir() or member.islnk() or member.issym()):
                continue
            out = (root_dir / rel).resolve()
            if not out.is_relative_to(root_dir / prefix):
                raise ValueError('Unsafe path in ZCode deb')
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with open(out, 'wb') as dst:
                shutil.copyfileobj(src, dst)


def ensure_zcode_runtime(dest=None):
    runtime = locate_zcode_runtime(dest)
    if runtime:
        for candidate in {runtime.parent.parent, runtime.parent}:
            try:
                _ensure_builtin_provider(candidate)
            except OSError:
                pass
        if dest and not runtime.resolve().is_relative_to(dest.resolve()):
            src_res = runtime.parent.parent if runtime.parent.name == 'glm' else runtime.parent
            if src_res.is_dir():
                _copy_runtime_resources(src_res, dest / 'engine/runtime')
                local_cjs = dest / 'engine/runtime/glm/zcode.cjs'
                if local_cjs.is_file():
                    return local_cjs
        return runtime

    cpu = platform.machine().lower()
    # zcode.cjs is plain JavaScript, byte-identical in the Windows installer and
    # the Linux package (sha256 verified), so every OS takes it from the Linux deb;
    # Windows hosts previously failed with 'distribution unsupported on this platform'.
    if cpu not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
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
        manifest_text = manifest_path.read_text(encoding='utf-8', errors='replace')
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
        _extract_deb_resources(deb_path, tmp_unpack)
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
    if bundle['provider'] == 'antigravity':
        return dest / 'engine/bin' / ('agy.exe' if is_windows else 'agy')
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
            candidates = [(modules / ('@opencode/cli-' + target + suffix), 'bin/' + binary + ('.exe' if system == 'win32' else '')) for suffix in variants]
        for package, relative in candidates:
            metadata = package / 'package.json'
            if metadata.is_file() and json.loads(metadata.read_text()).get('version') == (GO_CLI_VERSION if bundle['provider'] == 'go' else bundle['version']):
                executable = package / relative
                if not executable.is_file():
                    # A session-side updater can leave exactly one renamed
                    # leftover (<binary>.old.<epoch-ms>); restoring it is
                    # deterministic and version-checked above. Anything else
                    # stays a missing binary (fail closed).
                    stale = sorted(package.glob(Path(relative).name + '.old.*'))
                    if len(stale) == 1:
                        os.replace(stale[0], executable)
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


def _is_windows():
    # Seam for tests: pathlib.Path switches flavor with os.name on 3.12, so
    # tests patch this predicate instead of the os module.
    return os.name == 'nt'


def _fetch_checksummed_zcodex_asset(tag, asset, cache_dir, tmp_download):
    """Download one codex-zcode release asset (cache first) and return the
    verified local path. Any checksum or download failure becomes a generic
    install failure; details never reach output."""
    url = f'https://github.com/alexeygrigorev/codex-zcode/releases/download/{tag}/{asset}'
    sums_url = f'https://github.com/alexeygrigorev/codex-zcode/releases/download/{tag}/SHA256SUMS'
    cache_file = cache_dir / asset
    try:
        tmp_download.mkdir(parents=True, exist_ok=True)
        target = cache_file if cache_file.is_file() else tmp_download / asset
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
        if not target.is_file():
            subprocess.run(['curl', '-fSL', url, '-o', str(target)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
        import hashlib
        with open(target, 'rb') as f:
            actual_sha = hashlib.sha256(f.read()).hexdigest()
        if actual_sha.lower() != expected_sha.lower():
            raise ValueError(f'Checksum mismatch for {asset}')
        return target
    except (subprocess.SubprocessError, OSError, ValueError):
        raise ValueError('Managed engine installation failed') from None


def _go_engine_current(bundle, dest):
    """True when the installed OpenCode engine is the pinned 2.x `@opencode/cli`.

    Same-revision installs made while the pin was opencode-ai 1.x (no
    `auth import`) must be replaced in place, not skipped as already installed."""
    modules = Path(dest) / 'engine' / 'node_modules'
    try:
        # Only a leftover opencode-ai 1.x tree without the 2.x CLI is stale; an empty or
        # not-yet-installed tree is the ordinary install path's business.
        return not ((modules / 'opencode-ai').is_dir() and not (modules / '@opencode' / 'cli').is_dir())
    except OSError:
        return True


def install(bundle, request=None):
    from . import credentials
    validate(bundle)
    if bundle['provider'] == 'codex':
        # AC1 (intake 935b266b): the managed Codex version comes from the
        # reviewed host release pin, never from a per-account bundle, so a
        # sync from a stale account record cannot roll the engine back.
        bundle = dict(bundle, version=engine_versions.package_version('codex'))
        validate(bundle)
    dest = root() / (bundle['label'] + '-' + bundle['revision'])
    home = dest / 'home'
    raw = read_json(dest / 'bundle.json')
    receipt = load_bundle(dest)
    # A new Hub credential generation is not a new install: only the
    # immutable snapshot (engine, version, config) triggers reinstallation.
    paths = bundle.get('credential_paths') or []
    if (receipt is not None and credentials.install_snapshot(receipt, paths) == credentials.install_snapshot(bundle, paths)
            and (bundle['provider'] != 'go' or _go_engine_current(bundle, dest))):
        provision_managed_home(bundle, home, dest)
        credentials.reconcile(bundle['label'], bundle, home, dest, request or host_request)
        if bundle.get('provider') == 'go':
            _ensure_opencode_quse_auth(bundle, home)
        credentials.leased_refresh(bundle['label'], bundle, home, request or host_request)
        # A legacy receipt with identical content still migrates to the
        # secret-free form; unchanged split receipts are left untouched.
        if receipt != bundle or raw != _receipt_form(bundle):
            save_bundle(dest, bundle)
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
        bin_name = 'engine/bin/zcodex.cmd' if _is_windows() else 'engine/bin/zcodex'
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
            if system != 'linux' and not _is_windows():
                raise ValueError('Native zcodex platform unsupported')
            if _is_windows() and cpu not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
                raise ValueError('Native zcodex platform unsupported')
            if system == 'linux' and cpu not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
                raise ValueError('Native zcodex platform unsupported')
            version = bundle['version']
            tag = f'zcode-3.14.5-codex-{version}' if not version.startswith('zcode-') else version
            tmp_download = dest / 'engine/tmp-zcodex'
            try:
                if _is_windows():
                    # No npm/shell fallback: the checksummed release exe lands
                    # beside the wrapper (intake 608a2f9a, mirrors agy.exe).
                    # The codex-zcode release publishes only zcodex-windows-amd64.exe; Windows on ARM (Tabo)
                    # runs it under x64 emulation, so every Windows CPU takes the amd64 asset.
                    arch = 'amd64'
                    asset = f'zcodex-windows-{arch}.exe'
                    verified = _fetch_checksummed_zcodex_asset(tag, asset, root() / 'zcodex-cache', tmp_download)
                    binary = dest / 'engine' / 'bin' / 'zcodex.exe'
                    shutil.copyfile(verified, binary)
                    script = f'@echo off\r\n"{binary}" %*\r\n'
                    private_fs.atomic_write(wrapper, script.encode(), 4096)
                    os.chmod(wrapper, 0o700)
                else:
                    arch = 'amd64' if cpu in ('x86_64', 'amd64') else 'arm64'
                    asset = f'zcodex-linux-{arch}.tar.zst'
                    verified = _fetch_checksummed_zcodex_asset(tag, asset, root() / 'zcodex-cache', tmp_download)
                    subprocess.run(['tar', '--zstd', '-xf', str(verified), '-C', str(tmp_download)],
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
    elif bundle['provider'] == 'antigravity':
        install_antigravity(bundle, dest)
    else:
        npm_cmd = resolve_npm_command()
        try:
            subprocess.run(npm_cmd + ['install', '--prefix', str(dest / 'engine'), '--ignore-scripts', '--no-audit', '--no-fund', '--', pkg + '@' + (GO_CLI_VERSION if bundle['provider'] == 'go' else bundle['version'])],
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
    credentials.reconcile(bundle['label'], bundle, home, dest, request or host_request)
    if bundle.get('provider') == 'go':
        _ensure_opencode_quse_auth(bundle, home)
    credentials.leased_refresh(bundle['label'], bundle, home, request or host_request)
    save_bundle(dest, bundle)


def managed_model_allowed(provider, model, policy):
    """Antigravity is a harness on the Gemini policy family (see model_contract)."""
    family = 'gemini' if provider == 'antigravity' else provider
    return any(m['provider'] == family and m['model'] == model for m in policy)


def antigravity_pinned_version():
    from .bootstrap_pins import ANTIGRAVITY_RELEASES
    versions = {r.get('version') for r in ANTIGRAVITY_RELEASES.values()}
    if len(versions) != 1:
        raise ValueError('Antigravity release pins disagree on the version')
    return versions.pop()


def install_antigravity(bundle, dest):
    """The sha512-pinned Antigravity CLI release (bootstrap_pins), into this bundle only."""
    from . import bootstrap
    # Each host installs the release pinned in its own reviewed source, so a pin
    # bump never strands bundles imported earlier; the bundle version is the
    # import-time pin, kept for display only.
    antigravity_pinned_version()
    engine = dest / 'engine'
    for sub in ('bin', 'tmp'):
        (engine / sub).mkdir(parents=True, exist_ok=True)
    try:
        bootstrap.ensure_antigravity(engine)
    except (subprocess.SubprocessError, OSError, ValueError, RuntimeError, KeyError):
        raise ValueError('Managed engine installation failed') from None


def _rmtree_managed(entry: Path) -> None:
    """Remove a managed bundle directory even when Go/tool caches created 0555/0444 trees."""
    if entry.is_symlink():
        raise PermissionError('Unsafe managed directory')
    for dirpath, _dirnames, filenames in os.walk(entry, topdown=True, followlinks=False):
        dpath = Path(dirpath)
        if not dpath.is_symlink():
            try:
                os.chmod(dpath, 0o700)
            except OSError:
                pass
        for fname in filenames:
            fpath = dpath / fname
            if not fpath.is_symlink():
                try:
                    os.chmod(fpath, 0o600)
                except OSError:
                    pass

    def _on_rm_error(func, path, exc_info):
        p = Path(path)
        if p.is_symlink():
            raise exc_info[1]
        try:
            parent = p.parent
            if not parent.is_symlink():
                os.chmod(parent, 0o700)
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
        except OSError:
            pass
        func(path)

    shutil.rmtree(entry, onerror=_on_rm_error)


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
        retained = [
            b for b in bundles
            if any(p['label'] == b['label'] and p['revision'] == b['revision'] for p in previous)
            and (root() / (b['label'] + '-' + b['revision']) / 'bundle.json').is_file()
        ]
        profiles(retained)
        from . import engine_login
        engine_login.cancel_removed(bundles)
        # Keep every bundle dir the Hub still offers: a retry reinstalls in
        # place, while deleting it under a live session's engine (Windows
        # holds codex.exe open) half-removed the bundle (intake 6cbb862a).
        # AC2 (intake 8cc9e31a): a version update installs side by side and
        # never interrupts running sessions -- the previous revision dir of a
        # label with live sessions is kept until those sessions drain, while
        # new sessions resolve the profile to the new revision.
        keep = {b['label'] + '-' + b['revision'] for b in bundles}
        try:
            from . import engine_rollout
            for label in engine_rollout.deferred_labels():
                for old in root().glob(label + '-*'):
                    if old.is_dir() and re.fullmatch(r'[a-z][a-z0-9_-]{0,47}-[a-f0-9-]{36}', old.name):
                        keep.add(old.name)
        except Exception:
            pass
        for entry in root().iterdir():
            if re.fullmatch(r'[a-z][a-z0-9_-]{0,47}-[a-f0-9-]{36}', entry.name) and entry.name not in keep:
                # rmtree does not follow directory symlinks; reject them explicitly.
                if entry.is_symlink():
                    raise PermissionError('Unsafe managed directory')
                _rmtree_managed(entry)
        write_json(root() / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in retained])
        installed_bundles = []
        failed_bundles = []
        errors = {}
        last_error = None
        for b in bundles:
            try:
                install(b, request)
                installed_bundles.append(b)
            except Exception as e:
                failed_bundles.append(b)
                errors[b['label']] = last_error = e
                # Exception type only: messages may carry paths or provider output.
                print(json.dumps({'managed_install_failed': b['label'], 'error': type(e).__name__}), file=sys.stderr)
        if bundles and not installed_bundles:
            # Hub still learns which accounts failed, and why, before the sync is held.
            report_sync(request, 'held', (), failed_bundles, errors)
            if len(bundles) == 1 and last_error:
                raise last_error
            raise ValueError('Managed engine installation failed')
        profiles(installed_bundles)
        write_json(root() / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': b['provider']} for b in installed_bundles])
        for b in installed_bundles:
            try:
                sync_native_antigravity(b)
            except Exception as e:
                print(json.dumps({'native_antigravity_sync_failed': type(e).__name__}), file=sys.stderr)
            try:
                sync_opencode_device(b)
            except Exception as e:
                print(json.dumps({'opencode_device_sync_failed': type(e).__name__}), file=sys.stderr)
            authenticated = b['provider'] != 'gemini' or (root() / (b['label'] + '-' + b['revision']) / 'home/.gemini/oauth_creds.json').is_file()
            request('POST', '/api/engines/' + b['label'], {'revision': b['revision'], 'authenticated': authenticated})
        from . import engine_login
        engine_login.tick(response.get('logins', []), installed_bundles, request)
    report_sync(request, 'synced', installed_bundles, failed_bundles, errors)
    result = {'status': 'synced', 'profiles': ['hub-' + b['label'] for b in installed_bundles]}
    if failed_bundles:
        result['failed_labels'] = [b['label'] for b in failed_bundles]
    return result


# Sync reports go out on change, plus a slow refresh: hosts tick every heartbeat
# and Hub's Durable Object row writes are a daily Free-plan budget.
SYNC_REPORT_REFRESH_S = 1800
_last_sync_report = {'body': None, 'at': 0.0}


def report_sync(request, state, installed=(), failed=(), errors=None, clock=time.monotonic):
    """Best-effort per-host sync status for Hub (exception type only, never messages)."""
    errors = errors or {}
    items = [{'label': b['label'], 'revision': b['revision'], 'state': 'installed',
              # A7: acknowledge the Hub credential generation this host materialized.
              **({'credential_generation': b['credential_generation']}
                 if isinstance(b.get('credential_generation'), int) and not isinstance(b.get('credential_generation'), bool) else {})}
             for b in installed]
    items += [{'label': b['label'], 'revision': b['revision'], 'state': 'failed',
               'error': type(errors[b['label']]).__name__ if b['label'] in errors else 'InstallFailed'} for b in failed]
    body = {'state': state, 'items': items}
    now = clock()
    if body == _last_sync_report['body'] and now - _last_sync_report['at'] < SYNC_REPORT_REFRESH_S:
        return False
    try:
        request('POST', '/api/engines/sync-report', body)
    except Exception:
        return False
    _last_sync_report.update(body=body, at=now)
    return True


# A7: Hub pushes credential changes by bumping an epoch that rides on the 2 s
# offers poll. A host that sees an epoch it has not applied syncs at once (the
# same install path as the heartbeat pull, which stays as the fallback) and acks
# the generations it installed through report_sync.
EPOCH_RETRY_S = 30.0
_epoch = {'applied': None, 'thread': None, 'failed_at': -1e9, 'seen': None}
_epoch_lock = threading.Lock()


def _apply_epoch(epoch, tick_fn):
    try:
        result = tick_fn()
        ok = isinstance(result, dict) and result.get('status') == 'synced'
    except Exception:
        ok = False
    with _epoch_lock:
        if ok:
            _epoch['applied'] = epoch
        else:
            _epoch['failed_at'] = time.monotonic()
        _epoch['thread'] = None


def note_epoch(epoch, tick_fn=None, clock=time.monotonic, spawn=True):
    """Start one background sync when Hub announces an epoch not yet applied.

    Returns True when a sync was started. Failed syncs retry after
    EPOCH_RETRY_S, never in a tight loop.
    """
    if not isinstance(epoch, str) or not 1 <= len(epoch) <= 32:
        return False
    with _epoch_lock:
        _epoch['seen'] = epoch
        if _epoch['applied'] == epoch or (_epoch['thread'] is not None and _epoch['thread'].is_alive()):
            return False
        if clock() - _epoch['failed_at'] < EPOCH_RETRY_S:
            return False
        worker = threading.Thread(target=_apply_epoch, args=(epoch, tick_fn or tick), name='engine-epoch-sync', daemon=True)
        _epoch['thread'] = worker
    if spawn:
        worker.start()
    else:
        worker.run()
    return True


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
        report_sync(host_request, 'held')
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
            'launcher_effort': effort or 'medium',
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
    elif provider == 'antigravity':
        return {
            'launcher_provider': 'antigravity',
            'launcher_model': model or 'gemini-4-argon-high',
            'launcher_effort': effort,
        }
    return {}


def holds_login(bundle):
    """A Hub bundle with no credential and no host-login path cannot run work."""
    return (bundle['provider'] == 'go' or bundle.get('auth_method') == 'machine-login' or 'files' not in bundle
            or bool(bundle['files']) or bool(bundle.get('env')) or bool(bundle.get('credential_paths')))


def managed_subscriptions():
    state = read_json(root() / 'state.json', [])
    subs = []
    for b in state:
        bundle = None
        try:
            dest = root() / (b['label'] + '-' + b['revision'])
            bundle = load_bundle(dest)
        except Exception:
            pass
        # Installed for sync proofs (e.g. dummy-sync-5814), never advertised
        # for placement: no route means no session can land on it.
        if isinstance(bundle, dict) and not holds_login({**bundle, 'provider': b['provider']}):
            continue
        meta = launcher_metadata(b['provider'], bundle)
        sub = {
            # Canonical service model: the provider is the service. Orin's
            # second Codex home reports service `codex` (account `codex-orin`,
            # its label), never a separate `codex-orin` provider.
            # Antigravity spends the Google (Gemini) quota pool.
            'service': 'gemini' if b['provider'] == 'antigravity' else b['provider'],
            'account_label': b['label'],
            'engine': PACKAGES[b['provider']][2],
            'profile': 'hub-' + b['label'],
            'admission_argv': module_command('admission', b['label']),
        }
        sub.update(meta)
        subs.append(sub)
    return subs


def shared_dataops_dir():
    """Hub-owned DataOps credential directory shared by every managed session.

    Managed engines run with a private HOME, so the operator's own
    ~/.config/dataops is invisible to them (intake 3bae55cf). An operator
    provisions this directory once per host with
    `DATAOPS_CONFIG_DIR=<dir> dataops login --label "<agent>@<host>"`; it lives
    outside every repository and its contents are never printed.
    """
    try:
        return Path(config.get_config_path()).parent / 'shared' / 'dataops'
    except (OSError, RuntimeError, ValueError):
        return None


def engine_environment(bundle, home, dest=None):
    dest = dest or Path(home).parent
    env = {k: v for k, v in os.environ.items() if k not in ENV and k not in ('HUB_TOKEN', 'HUB_JOB_TOKEN', 'HUB_CONFIG_FILE', 'AGENT_HUB_MANAGED_DIR')}
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
        # Without a warm mode the Codex host policy auto-approves only the
        # 'safe' risk tier and denies every write/exec ("not auto-approved by
        # the warm ZCode bridge"): sessions read but never edit or commit.
        # RMTHZ goal routes run with yolo; keep an explicit operator override.
        env.setdefault('ZCODE_WARM', '1')
        env.setdefault('ZCODE_WARM_MODE', 'yolo')
    env['GH_CONFIG_DIR'] = os.environ.get('GH_CONFIG_DIR') or str(Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'gh')
    env.update(bundle.get('env') or {})
    shared = shared_dataops_dir()
    if shared is not None and 'DATAOPS_CONFIG_DIR' not in env and (shared / 'credentials.json').is_file():
        env['DATAOPS_CONFIG_DIR'] = str(shared)
    if bundle.get('provider') == 'go':
        from .fleet import load_settings
        from .opencode_auth import environment, prepare
        inherited = os.environ.get('AGENT_HUB_OPENCODE_AUTH')
        cfg = json.loads(inherited) if inherited else load_settings().get('opencode_auth')
        if not cfg:
            raise ValueError('engine_auth_expired: OpenCode device login configuration required')
        prepare(cfg)
        env = environment(cfg, base=env)
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
        # Managed engine installs are provisioned centrally; a session-side
        # Claude Code auto-update renames the shared native binary to
        # .old.<epoch-ms> and can leave the package with no engine at all
        # (Tabo, intake 61bba72f). Sessions never update the install.
        env['DISABLE_AUTOUPDATER'] = '1'
        env['CLAUDE_CODE_DISABLE_AUTOUPDATER'] = '1'
        for k in (
            'CLAUDE_CODE_USE_BEDROCK',
            'CLAUDE_CODE_USE_VERTEX',
            'CLAUDE_CODE_USE_FOUNDRY',
            'ANTHROPIC_API_KEY',
            'ANTHROPIC_AUTH_TOKEN',
            'ANTHROPIC_CUSTOM_HEADERS',
        ):
            env.pop(k, None)
    if bundle.get('provider') == 'codex':
        # Belt and suspenders with the config.toml key: never prompt.
        env['CODEX_DISABLE_UPDATE_CHECK'] = '1'
    if bundle.get('provider') == 'zai':
        env['CODEX_DISABLE_UPDATE_CHECK'] = '1'
        env['ZCODEX_DISABLE_UPDATE_CHECK'] = '1'
    if bundle.get('provider') == 'go':
        env['OPENCODE_DISABLE_AUTOUPDATE'] = '1'
    if bundle.get('provider') == 'antigravity':
        # The managed agy binary only moves with the reviewed pin.
        env['ANTIGRAVITY_DISABLE_AUTO_UPDATE'] = '1'
        env['AGY_DISABLE_AUTO_UPDATE'] = '1'
        # A user-level HTTP(S)_PROXY (desktop: 127.0.0.1:8899) breaks agy's Google OAuth
        # refresh: a valid Hub token then answers 'Authentication required' (same account,
        # identical token, OK without the proxy env on the same host).
        for key in list(env):
            if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                env.pop(key)
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
    """For the dedicated server collector: dynamic isolated accounts, sanitized only.

    Reads each provider with the Hub-materialized credential. Antigravity's
    read-only `/usage` command is used because its quota service is distinct
    from Gemini CLI's native Google Code Assist API. Hub itself also reads
    these accounts centrally; this is the backend fallback path.
    """
    from .fleet import quota_metadata
    from . import credentials
    accounts = []
    with lock():
        state = read_json(root() / 'state.json', [])
        for entry in state:
            raw = {}
            try:
                dest = root() / (entry['label'] + '-' + entry['revision'])
                b = load_bundle(dest)
                validate(b)
                if b.get('provider') == 'gemini':
                    from . import native_google_quota
                    raw = native_google_quota.probe(b, dest)
                elif b.get('provider') == 'antigravity':
                    from . import native_antigravity_quota
                    binary = dest / 'engine/bin' / ('agy.exe' if os.name == 'nt' else 'agy')
                    raw = native_antigravity_quota.probe(binary, dest / 'home')
                elif b.get('provider') == 'claude' and not credentials.managed_paths(b):
                    from . import native_claude_quota
                    raw = native_claude_quota.probe(b, dest)
                else:
                    raw = credentials.probe_quota(b['provider'], dest / 'home')
                    raw.pop('unauthorized', None)
            except Exception:
                pass
            if not raw:
                # No direct reading: report nothing rather than an error row
                # that would mask Hub's own fresh observation.
                continue
            quota_provider = 'gemini' if entry['provider'] == 'antigravity' else entry['provider']
            accounts.append({'account': entry['label'], 'provider': entry['provider'], 'quota': quota_metadata(raw, quota_provider)})
    return {'accounts': accounts}


# Hub admission reasons that must refuse a launch on this host.
HUB_REFUSALS = {'credential_invalid', 'credential_rejected', 'quota_exhausted'}


def hub_launch_gate(label, request=None):
    """Hub decides from its own quota and credential state; the host runs no probe.

    Returns (allowed, reason). Hub unreachable fails open with a warning: the
    offer that started this launch already passed Hub admission.
    """
    try:
        result = (request or host_request)('POST', '/api/engines/admission', {'label': label})
    except Exception:
        return True, 'quota_probe_warning:hub_unreachable'
    reason = result.get('reason') if isinstance(result, dict) else None
    if isinstance(result, dict) and result.get('launch_allowed') is False and reason in HUB_REFUSALS:
        return False, reason
    if isinstance(result, dict) and result.get('quota_warning'):
        return True, 'quota_probe_warning:' + str(result['quota_warning'])[:64]
    return True, 'ok'


def refresh_launch_credential(label, b, home, dest, request=None):
    """I3: materialize Hub's current credential generation right before exec."""
    from . import credentials
    if not credentials.managed_paths(b):
        return None
    request = request or host_request
    try:
        current = credentials.fetch_current(label, request)
        return credentials.reconcile(label, b, home, dest, request, hub_files=current['files'], hub_generation=current['generation'])
    except Exception:
        # Hub unreachable: launch with the copy materialized at the last sync.
        return {'status': 'hub_unreachable'}


def managed_codex_effort_args(bundle, extra_argv):
    """Apply the Hub effort after bundle defaults and before explicit caller argv."""
    if bundle['provider'] != 'codex':
        return []
    marker = '--hub-session-effort='
    session_efforts = [arg[len(marker):] for arg in extra_argv if arg.startswith(marker)]
    if len(set(session_efforts)) > 1 or any(e not in ('low', 'medium', 'high', 'max') for e in session_efforts):
        raise ValueError('invalid Hub session effort')
    args = [arg for arg in extra_argv if not arg.startswith(marker)]
    if args in (["--version"], ["-v"], ["--help"], ["-h"]):
        return []
    # Explicit caller config is last in the final argv and must remain authoritative.
    for i, arg in enumerate(args):
        if arg == '--':
            break
        value = None
        if arg in ('-c', '--config') and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith('--config='):
            value = arg[len('--config='):]
        elif arg.startswith('-c') and arg != '-c':
            value = arg[2:]
        if value and value.partition('=')[0].strip() == 'model_reasoning_effort':
            return []
        if arg == '--effort' or arg.startswith('--effort='):
            return []
    if session_efforts:
        effort = session_efforts[0]
    else:
        from .model_contract import native_model, policy_models
        model = native_model(args) or native_model(bundle['args']) or launcher_metadata('codex', bundle)['launcher_model']
        efforts = {m['effort'] for m in policy_models()
                   if m['provider'] == 'codex' and m['model'] == model}
        if len(efforts) != 1:
            return []
        effort = efforts.pop()
    # Hub calls the highest tier "max"; Codex's native spelling is "xhigh".
    effort = {'max': 'xhigh'}.get(effort, effort)
    return ['-c', 'model_reasoning_effort=' + json.dumps(effort)] if effort else []


def normalize_engine_argv(argv):
    """`prog run <label> ...` (engine subcommand injected before the label) -> `prog <label> run ...`."""
    argv = list(argv)
    if len(argv) > 2 and argv[1] == 'run' and LABEL.fullmatch(argv[2]):
        argv[1], argv[2] = argv[2], 'run'
    return argv


def execute():
    # `a task run --engine opencode --profile X` hands over `run <label> ...` (see _posix_engine_launcher).
    sys.argv[:] = normalize_engine_argv(sys.argv)
    label = sys.argv[1]
    if not LABEL.fullmatch(label):
        raise ValueError('Invalid managed label')
    with lock():
        state = read_json(root() / 'state.json', [])
        record = next((b for b in state if b['label'] == label), None)
        if record is None:
            raise ValueError('Managed profile removed')
        dest = root() / (label + '-' + record['revision'])
        b = load_bundle(dest)
        validate(b)
        home = dest / 'home'
        provision_managed_home(b, home, dest)
        refresh_launch_credential(label, b, home, dest)
        cwd = str(Path(os.getcwd()).resolve())
        p = Path(cwd)
        git_root = str(p)
        for parent in [p] + list(p.parents):
            if (parent / '.git').exists():
                git_root = str(parent)
                break
        install_managed_engine_permissions(b, home, trusted_paths=[cwd, git_root])
        _, binary, _ = PACKAGES[b['provider']]
        command = engine_argv(b, dest)
        env = engine_environment(b, home, dest)
        extra_argv = list(sys.argv[2:])
        # An admitted per-session model overrides bundle startup defaults.
        from .model_contract import native_model
        requested_model = native_model(extra_argv)
        if requested_model and b['provider'] != 'go':
            from .model_contract import replace_model_args, policy_models
            if not managed_model_allowed(b['provider'], requested_model, policy_models()):
                raise ValueError('model_unsupported: managed provider model')
            b = dict(b, args=replace_model_args(b['args'], requested_model)[:-2])
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
            # OpenCode 2.x `run` otherwise attaches to / starts the shared per-user background service
            # (fixed port), which is another store's server: 'port already in use' or
            # 'Database is not empty and has no session table'. A private server per store is isolated.
            if '--standalone' not in run_cmd:
                run_cmd.insert(1, '--standalone')
            argv = command + run_cmd
        else:
            is_diagnostic = b['args'] + extra_argv in (["--version"], ["-v"], ["--help"], ["-h"])
            bypass_flags = []
            if not is_diagnostic:
                if b['provider'] in ('codex', 'zai'):
                    if '--dangerously-bypass-approvals-and-sandbox' not in extra_argv and '--dangerously-bypass-approvals-and-sandbox' not in b.get('args', []):
                        # Also for `exec`: the flag is a top-level option placed before
                        # the subcommand. Without it headless zcodex/codex runs wait on
                        # approvals or deny every write (hung exec on DESKTOP-FP4OP26).
                        bypass_flags = ['--dangerously-bypass-approvals-and-sandbox']
                elif b['provider'] == 'claude':
                    if '--dangerously-skip-permissions' not in extra_argv and '--dangerously-skip-permissions' not in b.get('args', []):
                        bypass_flags = ['--dangerously-skip-permissions']
                elif b['provider'] in ('gemini', 'antigravity'):
                    if '--dangerously-skip-permissions' not in extra_argv and '--dangerously-skip-permissions' not in b.get('args', []):
                        bypass_flags = ['--dangerously-skip-permissions']
            from .codex_interactive import isolated_argv
            effort_args = managed_codex_effort_args(b, extra_argv)
            # This adapter-only marker is consumed above and must never reach Codex.
            extra_argv = [arg for arg in extra_argv if not arg.startswith('--hub-session-effort=')]
            # Bundle args carry startup defaults. Place Hub policy/session effort
            # after them, with explicit caller argv last so it can override policy.
            argv = command + isolated_argv(b['provider'], b['args'] + bypass_flags + effort_args + extra_argv)
        is_diagnostic = b['args'] + extra_argv in (["--version"], ["-v"], ["--help"], ["-h"])
        if not is_diagnostic:
            allowed, reason = hub_launch_gate(label)
            if allowed and reason != 'ok':
                print(json.dumps({'provider': b['provider'], 'quota_warning': reason}), file=sys.stderr)
            if not allowed:
                gate = 'codex_quota_gate_codex_reserve_required' if reason == 'quota_exhausted' and b['provider'] == 'codex' else reason
                print(json.dumps({'provider': b['provider'], 'launch_allowed': False, 'reason': gate}), file=sys.stderr)
                sys.exit(75)
        for var in ('HUB_CONFIG_FILE', 'HUB_TOKEN', 'HUB_JOB_TOKEN', 'AGENT_HUB_MANAGED_DIR'):
            env.pop(var, None)
    # Release the provisioning lock before any long-lived provider process.
    # Headless output must be retained before native automatic retirement;
    # native containment/exit receipts still determine the session outcome.
    if os.name != 'nt' and not is_diagnostic:
        from . import headless_output
        if headless_output.identity(env, PACKAGES[b['provider']][2]) is not None:
            headless_output.exit_like_child(headless_output.run(argv, env, PACKAGES[b['provider']][2]))
    if os.name == 'nt':
        res = subprocess.run(argv, env=env)
        sys.exit(res.returncode)
    else:
        os.execve(command[0], argv, env)


# Provider -> native credential file inside the managed home. Providers not
# listed have no reliable file probe and are not judged here.
LOGIN_FILES = {'codex': ('.codex/auth.json',)}


def profile_logged_in(bundle, dest):
    """True unless the managed profile provably has no credential."""
    paths = LOGIN_FILES.get(bundle.get('provider'))
    if not paths:
        return True
    files = bundle.get('files') if isinstance(bundle.get('files'), dict) else {}
    for rel in paths:
        if files.get(rel):
            return True
        try:
            if (Path(dest) / 'home' / rel).stat().st_size > 0:
                return True
        except OSError:
            pass
    return False


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
    def installed_bundle():
        state = read_json(root() / 'state.json', [])
        record = next((b for b in state if b['label'] == label), None)
        return load_bundle(root() / (label + '-' + record['revision'])) if record else None

    bundle = None
    lock_failed = False
    try:
        # Runs before Hub stamps its verdict, so a lock wait never ages the
        # verdict past the 30 s admission window. Bounded on Windows, where a
        # reconcile holds the lock through npm installs.
        with lock(timeout=ADMISSION_LOCK_WAIT_S):
            bundle = installed_bundle()
    except Exception:
        # state.json and bundle.json are replaced atomically, so an unlocked
        # read is consistent; a lock that cannot be opened or taken (Windows
        # ACL/msvcrt) must not read as a missing bundle and refuse every launch.
        lock_failed = True
        try:
            bundle = installed_bundle()
        except Exception:
            bundle = None
    if isinstance(bundle, dict) and bundle.get('provider') == 'go':
        try:
            _ensure_opencode_quse_auth(bundle, root() / f"{label}-{bundle.get('revision', '')}" / 'home')
        except Exception:
            pass
    # The managed profile is checked, and repaired under the lock, before Hub
    # stamps its verdict too: a repair waiting on a Windows reconcile after the
    # stamp aged the verdict past the 30 s window (admission_timestamp_stale).
    profile_error = None
    profile_missing = False
    if bundle is not None and profile_path().is_file():
        expected_header = f'[profiles.hub-{label}]'
        try:
            text = profile_path().read_text(encoding='utf-8')
        except OSError:
            text = ''
        if expected_header not in text:
            try:
                with lock(timeout=ADMISSION_LOCK_WAIT_S):
                    state = read_json(root() / 'state.json', [])
                    installed = []
                    for item in state:
                        b_path = root() / f"{item['label']}-{item['revision']}" / 'bundle.json'
                        loaded = load_bundle(b_path.parent)
                        # A partial bundle must not block profiles for the others.
                        if isinstance(loaded, dict) and loaded.get('provider') in PACKAGES and isinstance(loaded.get('label'), str):
                            installed.append(loaded)
                    if installed:
                        profiles(installed)
                text = profile_path().read_text(encoding='utf-8')
            except Exception as exc:
                # Credential-free: exception type only, never the message.
                profile_error = type(exc).__name__
                text = ''
            profile_missing = expected_header not in text

    try:
        result = host_request('POST', '/api/engines/admission', {'label': label})
    except Exception:
        result = {'launch_allowed': False, 'profile_supported': False, 'account_label': label, 'reason': 'hub_unreachable'}

    stamp_received = time.monotonic()
    try:
        server_at = dt.datetime.fromisoformat(result['server_observed_at'].replace('Z', '+00:00'))
        # Includes response transit; never forward server text or exception data.
        result['host_minus_hub_s'] = round((dt.datetime.now(dt.timezone.utc) - server_at).total_seconds(), 3)
    except (KeyError, TypeError, ValueError):
        pass

    # Login probe (intake 06d2a0ac AC2): a managed Codex profile without a
    # credential opens at the "Sign in with ChatGPT" screen and never runs a
    # turn. Refuse it as not_logged_in so Hub re-places on another route.
    if bundle is not None and result.get('launch_allowed') and not profile_logged_in(bundle, root() / (label + '-' + str(bundle.get('revision', '')))):
        result['launch_allowed'] = False
        result['reason'] = 'not_logged_in'
    if bundle is None and result.get('launch_allowed'):
        result['launch_allowed'] = False
        result['profile_supported'] = False
        result['reason'] = 'local_lock_unavailable' if lock_failed else 'local_bundle_missing'
    elif bundle is not None and result.get('launch_allowed') and profile_missing:
        if profile_error:
            result['profile_error'] = profile_error
        result['launch_allowed'] = False
        result['profile_supported'] = False
        result['reason'] = 'local_profile_missing'

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
            from .model_contract import configured_selection
            try:
                configured_selection(selection, payload.get('engine'), meta.get('launcher_provider'))
                supported = True
            except ValueError:
                supported = False
            if supported or (sel_model == meta.get('launcher_model') and sel_effort == meta.get('launcher_effort')):
                result['model_supported'] = True
                result['model'] = sel_model
                result['effort'] = sel_effort
            else:
                result['model_supported'] = False
                result['launch_allowed'] = False
                result['reason'] = 'model_mismatch'

    # Quota and credential admission is Hub's (intake 5814e109 I4): the
    # result above already carries Hub's verdict; the host runs no probe.
    # Map Hub's quota refusal onto the long-standing Codex gate slug.
    if result.get('reason') == 'quota_exhausted' and isinstance(bundle, dict) and bundle.get('provider') == 'codex':
        result['reason'] = 'codex_quota_gate_codex_reserve_required'

    # Lower bound on work after the Hub stamp: response transit is excluded.
    result['gate_post_stamp_s'] = round(time.monotonic() - stamp_received, 3)
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
    add.add_argument('--adopt', action='store_true', help='Attach the import to an existing unmanaged quota pool with this label (limits are preserved, never reset)')
    add.add_argument('--version', help='Exact pinned engine version (defaults to tested version)')
    list_cmd = commands.add_parser('list')
    list_cmd.add_argument('--view', choices=['worker'], default=None, help='Sanitized read-only verification view (worker sessions): engine, account, host, health, quota state')
    commands.add_parser('quota', help='Sanitized managed account observations for the server collector')
    remove = commands.add_parser('remove')
    remove.add_argument('label')
    commands.add_parser('sync', help='Reconcile this enrolled machine now')
    login = commands.add_parser('login', help='Hub-central provider login: Hub owns the token; no host ever logs in')
    login.add_argument('--provider', choices=['claude'], required=True)
    login.add_argument('--label', required=True)
    login.add_argument('--version', help='Exact engine version for a new account (default: tested version)')
    login.add_argument('--adopt', action='store_true', help='Attach to an existing unmanaged quota pool with this label')
    commands.add_parser('status', help='Operator: per-host sync state of every managed account')
    commands.add_parser('maintain', help='Operator: run central refresh + direct provider quota now')
    update = commands.add_parser('update', help='Hub-owned engine version rollout (intake 8cc9e31a): canary + smoke, never interrupts live sessions')
    update.add_argument('--engine', choices=['codex', 'claude', 'opencode', 'agy', 'zcodex'], default=None, help='Only this engine (default: all supported)')
    update.add_argument('--machine', default=None, help='Only this registered host (default: this host; use with Hub routing for fleet rollout)')
    update.add_argument('--canary', default=None, help='Canary host for fleet rollout (default: RMTHZ)')
    update.add_argument('--check-only', action='store_true', help='Report installed/desired/latest without installing or smoking')
    update.add_argument('--smoke', action='store_true', default=None, help='Run the real-prompt smoke after install (default: on unless --check-only)')
    update.add_argument('--no-smoke', dest='smoke', action='store_false', help='Skip the real-prompt smoke')
    update.set_defaults(smoke=True)
    cancel = commands.add_parser('cancel', help='Admin: cancel a pending rollout and restore its prior engine version')
    cancel.add_argument('rollout_id', help='Full rollout UUID')
    cancel.add_argument('--reason', default='operator_cancelled', help='Short nonsecret audit reason')
    versions = commands.add_parser('versions', help='Per-engine installed / desired / latest versions from a check run on this host (intake 8cc9e31a AC5)')
    versions.add_argument('--json', dest='json', action='store_true', help='Machine-readable output (default)')
    versions.add_argument('--fleet', action='store_true', help='Fleet-wide per-host versions from Hub version reports instead of this host')
    versions.add_argument('--machine', default=None, help='With --fleet: only this registered host')
    recommend = commands.add_parser('recommend', help='Recommend engine/account/model/host using Hub burn-rate decision logic without reserving')
    recommend.add_argument('--tier', default=None, help='Task tier (daily, deep, quick, review, ask)')
    recommend.add_argument('--workspace', default=None, help='Workspace name or path')
    recommend.add_argument('--machine', default=None, help='Preferred machine id')
    recommend.add_argument('--project', default=None, help='Project name')
    recommend.add_argument('--repo', default=None, help='Repository owner/name (like hub sessions start --repo): any host that clones its own work tree is eligible')
    recommend.add_argument('--needs', default=None, help='Comma-separated capability/OS labels')
    recommend.add_argument('--mode', choices=['headless', 'interactive'], default='headless')
    recommend.add_argument('--engine', default=None, help='Optional pinned engine (requires --reason)')
    recommend.add_argument('--reason', default=None, help='Required explanation when --engine is pinned')
    recommend.add_argument('--json', action='store_true', default=False, help='Raw JSON instead of the ranked table')


def central_login(args, prompt=None):
    """Start a Hub-owned login, show the provider URL, submit the pasted code.

    The code is read without echo and sent only to Hub; nothing is printed back.
    """
    import getpass
    if not LABEL.fullmatch(args.label):
        raise ValueError('Invalid managed label')
    body = {'provider': args.provider, 'label': args.label}
    if args.version:
        body['version'] = args.version
    if args.adopt:
        body['adopt'] = True
    started = bus._do_request('POST', '/api/engines/credential-logins', body)
    print('Open this URL, approve, then paste the code shown:\n' + started['url'], file=sys.stderr)
    print('This login expires at ' + login_expiry_text(started) + '; paste the code before then.', file=sys.stderr)
    code = (prompt or getpass.getpass)('Code: ').strip()
    path = '/api/engines/credential-logins/' + started['id']
    result = bus._do_request('POST', path, {'code': code, 'host_fallback': True})
    if isinstance(result, dict) and result.get('status') == 'host_exchange_required':
        # Hub's egress to the provider is blocked: exchange from this host and
        # upload the credential; Hub still owns the resulting token family.
        from . import credentials
        print('Hub could not reach the provider (provider HTTP %s, %s); exchanging the code from this host.'
              % (result.get('provider_status') or '-', result.get('reason') or 'unknown'), file=sys.stderr)
        files = credentials.exchange_login_code(code, result.get('exchange'))
        result = bus._do_request('POST', path, {'files': files})
    return result


def login_expiry_text(started, now=None):
    """Wall-clock expiry of a pending Hub-central login, from the server reply."""
    now = now or dt.datetime.now(dt.timezone.utc)
    expires = None
    raw = started.get('expires_at') if isinstance(started, dict) else None
    if isinstance(raw, str):
        try:
            expires = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            expires = None
    if expires is None:
        seconds = started.get('expires_in') if isinstance(started, dict) else None
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
            seconds = 600
        expires = now + dt.timedelta(seconds=seconds)
    minutes = max(0, round((expires - now).total_seconds() / 60))
    local = expires.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    return '%s local / %s UTC (in %d min)' % (local, expires.astimezone(dt.timezone.utc).strftime('%H:%M:%S'), minutes)


_SECRETISH = re.compile(r'[A-Za-z0-9_\-#.~/+=]{24,}')


def engine_error_message(exc, engines_cmd=None):
    """One sanitized line for a failed `hub engines` command (intake 8ff6b3d8).

    Shows the HTTP status plus the server's token-shaped error/reason slugs and
    the provider status; never response bodies, codes, verifiers or tokens.
    """
    rerun = 'rerun hub engines login'
    status = getattr(exc, 'http_status', None)
    if isinstance(status, int) and not isinstance(status, bool):
        code = getattr(exc, 'error_code', None) or getattr(exc, 'hub_error', None) or 'unknown'
        reason = getattr(exc, 'error_reason', None) or getattr(exc, 'hub_reason', None)
        pstatus = getattr(exc, 'provider_status', None)
        pstatus = pstatus if pstatus is not None else '-'
        if code == 'login_expired' or (engines_cmd == 'login' and code == 'not_found'):
            return 'login expired (%s, HTTP %d) -- %s' % (code, status, rerun)
        if code == 'login_code_rejected':
            return 'login code rejected (HTTP %d %s; provider HTTP %s, %s) -- %s' % (status, code, pstatus, reason or 'unknown', rerun)
        if code == 'login_exchange_blocked':
            return 'Hub cannot reach the provider (HTTP %d %s; provider HTTP %s, %s) -- %s' % (status, code, pstatus, reason or 'unknown', rerun)
        hint = ' -- check hub login and admin rights' if status in (401, 403) else ''
        return 'Hub engine operation failed: HTTP %d %s%s%s' % (status, code, ' (%s)' % reason if reason else '', hint)
    from . import credentials
    if isinstance(exc, credentials.LoginExchangeError):
        return 'host code exchange failed: %s -- %s' % (exc, rerun)
    import urllib.error
    if isinstance(exc, urllib.error.URLError):
        return 'Hub engine operation failed: Hub unreachable (%s)' % type(exc).__name__
    if isinstance(exc, (ValueError, OSError, KeyError)):
        text = _SECRETISH.sub('[redacted]', str(exc))[:200]
        return 'Hub engine operation failed: %s%s' % (type(exc).__name__, ': ' + text if text else '')
    return 'Hub engine operation failed: %s; check authentication, bundle permissions and host prerequisites.' % type(exc).__name__


def sync_status(listing):
    """Compact per-host view of the sanitized `GET /api/engines` sync_status."""
    hosts = []
    for h in listing.get('sync_status', []):
        hosts.append({'machine': h.get('machine') or h['machine_id'], 'machine_id': h['machine_id'], 'online': h['online'], 'host_state': h['host_state'],
                      'last_sync_at': h['last_sync_at'], 'accounts': {a['label']: a['state'] + (':' + a['error'] if a.get('error') else '') for a in h['accounts']}})
        # A7: credential generation Hub holds vs the one this host acknowledged.
        creds = {a['label']: {'current': a['credential_generation'], 'acked': a.get('acked_generation')}
                 for a in h['accounts'] if 'credential_generation' in a}
        if creds:
            hosts[-1]['credentials'] = creds
    return {'hosts': hosts}


def engine_update_command(args):
    """On-demand engine rollout (AC1-3): disable self-updates, sync, smoke, report.

    Fleet targeting (``--machine`` for another host) is routed through Hub
    (``POST /api/engines/update``); this host never SSHes anywhere. The local
    path converges via the normal managed ``sync`` (side-by-side installs,
    previous revision kept while live sessions use it) and runs one real
    smoke session per engine.
    """
    from . import engine_rollout
    only = getattr(args, 'engine', None)
    engines = [only] if only else list(engine_rollout.ENGINE_ORDER)
    machine = getattr(args, 'machine', None)
    if machine:
        try:
            import socket
            local_names = {socket.gethostname()}
        except Exception:
            local_names = set()
        try:
            from . import config as _config
            local_id = (_config.load_host_config() or {}).get('machine_id')
            if local_id:
                local_names.add(str(local_id))
        except Exception:
            pass
        if machine not in local_names:
            body: dict = {}
            if only:
                body['engine'] = only
            body['machine'] = machine
            if getattr(args, 'canary', None):
                body['canary'] = args.canary
            return bus._do_request('POST', '/api/engines/update', body)
    # Local path: self-update prompts off first so nothing blocks (AC4).
    disabled_files: list[str] = []
    try:
        state = read_json(root() / 'state.json', [])
        seen_homes = set()
        for entry in state:
            if not isinstance(entry, dict):
                continue
            for home in root().glob(entry.get('label', '') + '-*/home'):
                if home.is_dir() and str(home) not in seen_homes:
                    seen_homes.add(str(home))
                    try:
                        disabled_files.extend(engine_rollout.ensure_self_update_disabled(home, entry.get('provider')))
                    except Exception:
                        pass
    except Exception:
        pass
    check = engine_rollout.check_host()
    check['self_update_files_updated'] = sorted(set(disabled_files))
    if getattr(args, 'check_only', False):
        if only:
            check['engines'] = {only: check['engines'].get(only, {})}
        return check
    # Converge to Hub's current bundles (side-by-side; live sessions keep the
    # old revision until they drain). Best-effort: report sync errors sanitized.
    sync_result: dict = {}
    try:
        sync_result = sync()
    except Exception as exc:
        sync_result = {'status': 'held', 'error': type(exc).__name__}
    check['sync'] = sync_result
    # One real smoke session per engine (AC3): version + a real prompt.
    live = engine_rollout.live_engines()
    check['live_engines_deferred'] = sorted(live)
    smokes: dict[str, dict] = {}
    if getattr(args, 'smoke', True):
        for engine in engines:
            try:
                smoke = engine_rollout.smoke_engine(engine)
            except Exception as exc:
                smoke = {'engine': engine, 'state': 'failed', 'error': type(exc).__name__}
            engine_rollout.record_result(engine, {'state': smoke.get('state'),
                                                 'version': smoke.get('version'),
                                                 'error': smoke.get('error'),
                                                 'at': check.get('checked_at')})
            smokes[engine] = {'state': smoke.get('state'), 'version': smoke.get('version'),
                              'error': smoke.get('error'),
                              'deferred': engine in live}
        check['smokes'] = smokes
        # Canary gate: with --canary, decide fleet rollout vs rollback + 1 alert.
        canary_host = getattr(args, 'canary', None)
        if canary_host:
            rest = []
            try:
                fleet = bus._do_request('GET', '/api/fleet')
                rest = [m.get('id') for m in fleet.get('machines', [])
                        if isinstance(m, dict) and m.get('id') and m.get('id') != canary_host]
            except Exception:
                rest = []
            first = smokes.get(engines[0], {}) if len(engines) == 1 else {'state': 'green' if all(
                s.get('state') == 'green' for s in smokes.values()) else 'failed',
                'engine': ','.join(engines), 'error': 'smoke_failed'}
            gate = engine_rollout.decide_canary(first, rest)
            check['canary_gate'] = {'decision': gate['decision'], 'hosts': gate['hosts']}
            if gate['alert']:
                # Recorded locally and attached to the version report below;
                # Hub raises the single actionable alert from the report.
                check['alert'] = gate['alert']
    else:
        check['smokes'] = {}
    # Best-effort version report so `hub engines versions --fleet` reflects
    # this host's check run (AC5). Never fails the command.
    try:
        report = {'engines': {e: {'installed': (check['engines'].get(e) or {}).get('installed'),
                                  'latest': (check['engines'].get(e) or {}).get('latest'),
                                  'result': ((smokes.get(e) or {}).get('state')
                                             if e in smokes else ((check['engines'].get(e) or {}).get('last_result') or {}).get('state'))}
                              for e in engines}}
        if isinstance(check.get('alert'), dict):
            report['alert'] = check['alert']
        bus._do_request('POST', '/api/engines/version-report', report)
    except Exception:
        pass
    return check


def render_recommend(result):
    """Ranked candidates of `GET /api/fleet/recommend` as text: rank, normalized score, route, reason."""
    lines = []
    selected = result.get('selected') or {}
    candidates = result.get('candidates') or []
    if not candidates:
        lines.append('no eligible candidate')
    else:
        lines.append('tier=%s; ranked candidates (score 0-100 is relative to the best):' % (result.get('tier') or '-'))
    for i, c in enumerate(candidates, 1):
        norm = c.get('normalized_score')
        factors = (c.get('decision') or {}).get('factors') or {}
        lines.append('%2d. %5s  %s/%s (%s) on %s%s' % (
            i, ('%.1f' % norm) if isinstance(norm, (int, float)) else '-', c.get('engine'), c.get('account'),
            c.get('model_id') or c.get('model') or '-', c.get('machine'),
            '  <- selected' if selected and c.get('machine') == selected.get('machine') and c.get('engine') == selected.get('engine') and c.get('account') == selected.get('account') else ''))
        lines.append('      %s' % (c.get('decision_reason') or ''))
        if factors:
            lines.append('      rem=%s%% ratio=%s burn=%sx load=%sx host=%sx fail=%sx (recent failures %s)' % (
                factors.get('remaining_pct'), factors.get('burn_ratio'), factors.get('burn_factor'), factors.get('load_factor'),
                factors.get('host_factor'), factors.get('failure_factor'), factors.get('recent_failures')))
    rejected = result.get('rejected') or []
    if rejected:
        counts = {}
        for r in rejected:
            counts[r.get('reason')] = counts.get(r.get('reason'), 0) + 1
        lines.append('excluded: ' + ', '.join('%s x%d' % (k, v) for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))))
    return '\n'.join(lines)


def command(args):
    if args.engines_cmd == 'recommend':
        import urllib.parse
        if getattr(args, 'engine', None) and not (getattr(args, 'reason', None) and str(args.reason).strip()):
            raise ValueError('engine_reason_required: --engine requires --reason')
        q = {}
        for k in ('tier', 'workspace', 'machine', 'project', 'repo', 'needs', 'mode', 'engine', 'reason'):
            v = getattr(args, k, None)
            if v is not None and str(v).strip():
                q[k] = str(v).strip()
        qs = ('?' + urllib.parse.urlencode(q)) if q else ''
        return bus._do_request('GET', '/api/fleet/recommend' + qs)
    if args.engines_cmd == 'quota':
        return quotas()
    if args.engines_cmd == 'login':
        return central_login(args)
    if args.engines_cmd == 'maintain':
        return bus._do_request('POST', '/api/engines/maintain', {})
    if args.engines_cmd == 'sync':
        return sync()
    if args.engines_cmd == 'versions':
        from . import engine_rollout
        if getattr(args, 'fleet', False) or getattr(args, 'machine', None):
            params = ''
            if getattr(args, 'machine', None):
                import urllib.parse
                params = '?machine=' + urllib.parse.quote(str(args.machine), safe='')
            try:
                return bus._do_request('GET', '/api/engines/versions' + params)
            except Exception:
                # Fleet unreachable: fall back to the local check run.
                return engine_rollout.check_host()
        return engine_rollout.check_host()
    if args.engines_cmd == 'cancel':
        if not REV.fullmatch(args.rollout_id):
            raise ValueError('Invalid rollout id')
        reason = args.reason
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240 or re.search(r'[\x00-\x1f\x7f]', reason):
            raise ValueError('Invalid cancellation reason')
        return bus._do_request('POST', '/api/engines/rollouts/' + args.rollout_id + '/cancel', {'reason': reason.strip()})
    if args.engines_cmd == 'update':
        from . import engine_rollout
        return engine_update_command(args)
    if args.engines_cmd == 'list':
        if getattr(args, 'view', None) == 'worker':
            return bus._do_request('GET', '/api/engines?view=worker')
        return bus._do_request('GET', '/api/engines')
    if args.engines_cmd == 'status':
        listing = sync_status(bus._do_request('GET', '/api/engines'))
        # AC5: per host per engine installed / latest / last result, taken
        # from a check run on that host. Best-effort: status stays usable
        # when version reports are unavailable.
        try:
            listing['engine_versions'] = bus._do_request('GET', '/api/engines/versions')
        except Exception:
            pass
        return listing
    if args.engines_cmd == 'remove':
        if not LABEL.fullmatch(args.label):
            raise ValueError('Invalid managed label')
        return bus._do_request('DELETE', '/api/engines/' + args.label)
    if args.file:
        bundle = json.loads(private_fs.read_bytes(Path(args.file).expanduser().absolute(), 32768))
    else:
        if not args.label or not LABEL.fullmatch(args.label) or (args.provider not in ('gemini', 'go') and not (args.auth_file or getattr(args, 'token_file', None))):
            raise ValueError('Native import requires label and auth-file')
        paths = {'codex': ('.codex/auth.json', '.codex/config.toml', engine_versions.package_version('codex')),
                 'claude': ('.claude/.credentials.json', '.claude/settings.json', engine_versions.package_version('claude')),
                 'gemini': ('.gemini/oauth_creds.json', '.gemini/settings.json', engine_versions.package_version('gemini')),
                 'go': ('.local/share/opencode/auth.json', '.config/opencode/opencode.json', '1.19.3'),
                 'zai': ('.zcode/v2/credentials.json', '.zcodex/config.toml', '0.155.1'),
                 'antigravity': (ANTIGRAVITY_TOKEN, None, antigravity_pinned_version())}
        auth_path, config_path, version = paths[args.provider]
        env = {}
        if args.provider == 'go':
            if getattr(args, 'token_file', None) or args.config_file:
                raise ValueError('OpenCode imports only its `opencode auth export opencode` file (--auth-file)')
            # The device-login export (private file, never printed) goes to the
            # central store; hosts import it into their own Hub-owned device store.
            files = {OPENCODE_DEVICE_EXPORT: _opencode_device_export(private_fs.read_bytes(Path(args.auth_file).expanduser().absolute(), 16000).decode())} if args.auth_file else {}
        elif args.provider == 'gemini':
            if getattr(args, 'token_file', None):
                raise ValueError('Gemini imports its private OAuth cache with --auth-file')
            # Hub-held Gemini account (intake 5814e109): the OAuth cache is
            # stored centrally; without it, hosts use native machine login.
            files = {auth_path: private_fs.read_bytes(Path(args.auth_file).expanduser().absolute(), 16000).decode()} if args.auth_file else {}
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
                files['.zcodex/config.toml'] = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nmodel = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
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
                    files['.zcodex/config.toml'] = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nmodel = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
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
                        files['.zcodex/config.toml'] = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\nmodel = "glm-5.3-flash"\nmodel_provider = "zcode"\n\n[model_providers.zcode]\nname = "zcode"\nbase_url = "https://api.z.ai/api/anthropic"\nwire_api = "zcode"\n'
                    else:
                        raise ValueError('Invalid auth file format')
            else:
                raise ValueError('Native import requires auth-file or token-file')
        else:
            if getattr(args, 'token_file', None) or not args.auth_file:
                raise ValueError('Native import requires auth-file')
            files = {auth_path: private_fs.read_bytes(Path(args.auth_file).expanduser().absolute(), 16000).decode()}
        if args.config_file and not config_path:
            raise ValueError('Antigravity imports only its OAuth token file')
        if args.config_file:
            files[config_path] = private_fs.read_bytes(Path(args.config_file).expanduser().absolute(), 16000).decode()
        elif args.provider == 'codex':
            files[config_path] = 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        default_args = ['--model', 'opencode-go/muse-spark-1.3-contributor'] if args.provider == 'go' else (['--model', 'glm-5.3-flash'] if args.provider == 'zai' else [])
        bundle = {'label': args.label, 'provider': args.provider, 'version': args.version or version, 'files': files,
                  'env': env, 'args': default_args}
    if not isinstance(bundle, dict) or not LABEL.fullmatch(bundle.get('label', '')):
        raise ValueError('Invalid managed bundle')
    if bundle.get('provider') == 'go' and (bundle.get('env') or list(bundle.get('files') or {}) not in ([], [OPENCODE_DEVICE_EXPORT])):
        raise ValueError('OpenCode bundles carry no env and at most the Hub-held device-login export')
    if not args.file and ((args.provider == 'go' and not args.auth_file) or (args.provider == 'gemini' and not args.auth_file)):
        bundle['auth_method'] = 'machine-login'
    return bus._do_request('PUT', '/api/engines/' + bundle['label'] + ('?adopt=true' if getattr(args, 'adopt', False) else ''), bundle)


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

