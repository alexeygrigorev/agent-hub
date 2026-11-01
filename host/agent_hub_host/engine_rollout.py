"""Hub-managed engine version rollout (intake 8cc9e31a).

Hub owns engine updates for codex, claude, opencode, agy/antigravity and
zcodex on every registered host. This module reuses the reviewed pins in
:mod:`agent_hub_host.bootstrap_pins` and the managed installer in
:mod:`agent_hub_host.provisioning` -- it never builds a parallel updater.

Covered acceptance criteria:
- AC1: on-demand ``hub engines update [--engine X]`` plus a daily check
  (``check_host`` is called from the host loop / a daily timer; the Hub
  Worker also runs a daily ``tickEngineRollout``).
- AC2: updates never interrupt running sessions. Managed installs are
  side by side (``<label>-<revision>``); :func:`live_engines` reports which
  engines have live sessions and :func:`deferred_labels` keeps the previous
  revision directory until those sessions drain. New sessions resolve the
  profile to the new revision.
- AC3: canary helper :func:`decide_canary` -- roll out to the rest only on
  a green canary smoke, otherwise keep/rollback and raise one alert.
- AC4: :func:`ensure_self_update_disabled` disables in-engine self-update
  prompts on Hub-provisioned homes so sessions never block on an update menu.
- AC5: :func:`check_host` returns per-engine installed / desired / latest /
  last-result rows taken from a check run on that host.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path

ENGINE_ORDER = ('codex', 'claude', 'opencode', 'agy', 'zcodex')
PROVIDER_FOR = {
    'codex': 'codex',
    'claude': 'claude',
    'opencode': 'go',
    'agy': 'antigravity',
    'zcodex': 'zai',
}
ENGINE_FOR_PROVIDER = {v: k for k, v in PROVIDER_FOR.items()}

VERSION_RE = re.compile(r'(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)')
CANARY_DEFAULT = 'RMTHZ'
SMOKE_MARKER = 'SMOKE_OK'
SMOKE_PROMPT = 'Reply with the exact string SMOKE_OK and nothing else.'
ROLLOUT_STATE_FILE = 'engine-rollout.json'


def desired_versions() -> dict[str, str]:
    """Reviewed desired versions. Single source: bootstrap_pins."""
    from . import bootstrap_pins
    from .provisioning import antigravity_pinned_version

    def _pkg_version(spec: str) -> str:
        return spec.rsplit('@', 1)[1]

    codex_version = _pkg_version(bootstrap_pins.ENGINE_PACKAGES['codex'])
    return {
        'codex': codex_version,
        'claude': _pkg_version(bootstrap_pins.ENGINE_PACKAGES['claude']),
        'opencode': _pkg_version(bootstrap_pins.ENGINE_PACKAGES['opencode']),
        # zcodex tracks the reviewed codex CLI version: the release tag is
        # ``zcode-<runtime>-codex-<version>`` (see provisioning.install).
        'zcodex': codex_version,
        'agy': antigravity_pinned_version(),
    }


def _managed_root() -> Path:
    from . import provisioning
    return provisioning.root()


def rollout_state_path() -> Path:
    return _managed_root() / ROLLOUT_STATE_FILE


def read_rollout_state() -> dict:
    try:
        from . import provisioning
        state = provisioning.read_json(rollout_state_path(), {})
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def write_rollout_state(state: dict) -> None:
    from . import provisioning
    with_time = dict(state)
    with_time['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    provisioning.write_json(rollout_state_path(), with_time)


def record_result(engine: str, result: dict) -> dict:
    """Persist the last update result for one engine; returns the state."""
    state = read_rollout_state()
    engines = state.get('engines')
    if not isinstance(engines, dict):
        engines = {}
    engines[engine] = result
    state['engines'] = engines
    write_rollout_state(state)
    return state


def last_result(engine: str) -> dict | None:
    state = read_rollout_state()
    engines = state.get('engines')
    if isinstance(engines, dict) and isinstance(engines.get(engine), dict):
        return engines[engine]
    return None


def parse_version(text: str) -> str | None:
    if not text:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def installed_version(engine: str, timeout: int = 30) -> str | None:
    """Best-effort ``--version`` of the managed install on this host."""
    if engine not in PROVIDER_FOR:
        raise ValueError('Unknown engine')
    provider = PROVIDER_FOR[engine]
    try:
        from . import provisioning
        state = provisioning.read_json(provisioning.root() / 'state.json', [])
        labels = [b['label'] for b in state if isinstance(b, dict) and b.get('provider') == provider]
    except Exception:
        labels = []
    for label in labels:
        try:
            from . import provisioning
            dest = provisioning.root() / label
            candidates = list(provisioning.root().glob(label + '-*/bundle.json'))
            if not candidates:
                continue
            bundle = provisioning.read_json(candidates[0])
            if not isinstance(bundle, dict):
                continue
            dest = candidates[0].parent
            argv = provisioning.engine_argv(bundle, dest) + ['--version']
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=timeout)
            out = (proc.stdout or '') + '\n' + (proc.stderr or '')
            version = parse_version(out)
            if version:
                return version
        except Exception:
            continue
    return None


def latest_available(engine: str, timeout: int = 15) -> str | None:
    """Latest upstream version, best-effort. Never raises; falls back to desired."""
    desired = desired_versions().get(engine)
    if engine not in PROVIDER_FOR:
        raise ValueError('Unknown engine')
    if engine == 'agy':
        # Antigravity moves only through the reviewed sha512 pin.
        return desired
    if engine == 'zcodex':
        # zcodex tracks the reviewed codex pin (GitHub release needs review).
        return desired
    try:
        from . import bootstrap_pins
        pkg_map = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code',
                   'opencode': 'opencode-ai'}
        pkg = pkg_map[engine]
        _ = bootstrap_pins  # keep pin module as the version source of record
        proc = subprocess.run(['npm', 'view', pkg, 'version'], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=timeout)
        version = parse_version(proc.stdout or '')
        if version and re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', version):
            return version
    except Exception:
        pass
    return desired


def ensure_self_update_disabled(home=None, provider: str | None = None) -> list[str]:
    """Disable in-engine self-update prompts in Hub-provisioned homes.

    - codex/zcodex: ``check_for_update_on_startup = false`` in config.toml.
    - claude: ``DISABLE_AUTOUPDATER`` is set in the session env (see
      provisioning.engine_environment); here also persist
      ``autoUpdates = false`` so interactive logins never prompt.
    - opencode: ``autoupdate = false`` in opencode.json.
    - antigravity/agy: marker env documented in engine_environment; the
      binary itself only moves with the reviewed pin, never self-updates.
    Returns the list of files changed.
    """
    from . import provisioning

    if home is None:
        return []
    home = Path(home)
    changed: list[str] = []
    providers = [provider] if provider else ['codex', 'zai', 'claude', 'go', 'antigravity']

    def _toml_disable(path: Path) -> None:
        try:
            current = provisioning.read_engine_file(path)
        except Exception:
            return
        text = '' if current is None else current.decode('utf-8', 'replace')
        updated = provisioning.set_toml_top_level_keys(text, {'check_for_update_on_startup': False})
        if current is None or updated.encode() != current:
            provisioning.private_fs.atomic_write(path, updated.encode('utf-8'), 131072, engine_owned=True)
            changed.append(str(path))

    if 'codex' in providers:
        _toml_disable(home / '.codex' / 'config.toml')
    if 'zai' in providers:
        _toml_disable(home / '.zcodex' / 'config.toml')
    if 'claude' in providers:
        settings_path = home / '.claude' / 'settings.json'
        try:
            raw = provisioning.read_engine_file(settings_path)
            settings = json.loads(raw.decode('utf-8')) if raw else {}
            if not isinstance(settings, dict):
                settings = {}
            if settings.get('autoUpdates') is not False:
                settings['autoUpdates'] = False
                provisioning.private_fs.atomic_write(
                    settings_path, json.dumps(settings, indent=2).encode('utf-8'), 131072, engine_owned=True)
                changed.append(str(settings_path))
        except Exception:
            pass
    if 'go' in providers:
        conf_path = home / '.config' / 'opencode' / 'opencode.json'
        try:
            raw = provisioning.read_engine_file(conf_path)
            conf = json.loads(raw.decode('utf-8')) if raw else {}
            if not isinstance(conf, dict):
                conf = {}
            if conf.get('autoupdate') is not False:
                conf['autoupdate'] = False
                provisioning.private_fs.atomic_write(
                    conf_path, json.dumps(conf, indent=2).encode('utf-8'), 131072, engine_owned=True)
                changed.append(str(conf_path))
        except Exception:
            pass
    return changed


def self_update_disabled_ok(home, provider: str) -> bool:
    """Verify self-update prompts are disabled for one managed home."""
    home = Path(home)
    try:
        if provider == 'codex':
            config = tomllib.loads((home / '.codex' / 'config.toml').read_text(encoding='utf-8'))
            return config.get('check_for_update_on_startup') is False
        if provider == 'zai':
            config = tomllib.loads((home / '.zcodex' / 'config.toml').read_text(encoding='utf-8'))
            return config.get('check_for_update_on_startup') is False
        if provider == 'claude':
            raw = open(home / '.claude' / 'settings.json', 'rb').read().decode('utf-8', 'replace')
            return '"autoUpdates"' in raw and 'false' in raw.lower()
        if provider == 'go':
            raw = open(home / '.config' / 'opencode' / 'opencode.json', 'rb').read().decode('utf-8', 'replace')
            return '"autoupdate"' in raw and 'false' in raw.lower()
        if provider == 'antigravity':
            # Pinned binary: no self-update channel to disable in config.
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return False


def live_engines(list_rows=None) -> set[str]:
    """Engines with at least one live native session on this host."""
    try:
        if list_rows is None:
            from . import native_sessions
            rows = native_sessions.list_sessions()
        else:
            rows = list_rows
    except Exception:
        return set()
    live: set[str] = set()
    items = rows if isinstance(rows, list) else rows.get('sessions', []) if isinstance(rows, dict) else []
    for row in items if isinstance(items, list) else []:
        if not isinstance(row, dict):
            continue
        phase = str(row.get('phase') or row.get('state') or '').lower()
        if phase in ('exited', 'dead', 'failed', 'done'):
            continue
        engine = str(row.get('engine') or '').lower()
        mapped = {'codex': 'codex', 'zcodex': 'zcodex', 'claude': 'claude',
                  'opencode': 'opencode', 'agy': 'agy', 'antigravity': 'agy',
                  'gemini': 'agy'}.get(engine)
        if mapped:
            live.add(mapped)
    return live


def deferred_labels(live: set[str] | None = None) -> set[str]:
    """Managed labels whose old revision must be kept (live sessions).

    Reads the sync state plus any installed bundle dir, so a label with live
    sessions is never pruned even when the state file is mid-write.
    """
    live = live if live is not None else live_engines()
    providers = {PROVIDER_FOR[e] for e in live if e in PROVIDER_FOR}
    if not providers:
        return set()
    keep: set[str] = set()
    try:
        from . import provisioning
        state = provisioning.read_json(provisioning.root() / 'state.json', [])
        for b in state:
            if isinstance(b, dict) and b.get('provider') in providers and isinstance(b.get('label'), str):
                keep.add(b['label'])
        for bundle_path in provisioning.root().glob('*/bundle.json'):
            try:
                bundle = provisioning.read_json(bundle_path)
            except Exception:
                continue
            if isinstance(bundle, dict) and bundle.get('provider') in providers and isinstance(bundle.get('label'), str):
                keep.add(bundle['label'])
    except Exception:
        pass
    return keep


def smoke_argv(engine: str, managed_argv: list[str], provider: str) -> list[str] | None:
    """Bounded real-prompt smoke argv for one engine, or None when unsupported."""
    base = list(managed_argv)
    if engine == 'codex' or engine == 'zcodex':
        flag = '--dangerously-bypass-approvals-and-sandbox'
        if flag not in base:
            base.append(flag)
        return base + ['exec', SMOKE_PROMPT]
    if engine == 'claude':
        flag = '--dangerously-skip-permissions'
        if flag not in base:
            base.append(flag)
        return base + ['-p', SMOKE_PROMPT]
    if engine == 'opencode':
        return base + ['run', SMOKE_PROMPT]
    if engine == 'agy':
        return base + ['-p', SMOKE_PROMPT]
    return None


def smoke_engine(engine: str, timeout: int = 180) -> dict:
    """Run ``--version`` plus one real prompt. Bounded; output never stored raw."""
    if engine not in PROVIDER_FOR:
        raise ValueError('Unknown engine')
    provider = PROVIDER_FOR[engine]
    result: dict = {'engine': engine, 'version': None, 'version_ok': False,
                    'prompt_ok': False, 'state': 'failed', 'error': None}
    try:
        from . import provisioning
        state = provisioning.read_json(provisioning.root() / 'state.json', [])
        bundle = None
        dest = None
        for entry in state:
            if isinstance(entry, dict) and entry.get('provider') == provider:
                cands = list(provisioning.root().glob(entry['label'] + '-*/bundle.json'))
                if cands:
                    bundle = provisioning.read_json(cands[0])
                    dest = cands[0].parent
                    break
        if not isinstance(bundle, dict) or dest is None:
            result['error'] = 'not_installed'
            return result
        clean_env = {k: v for k, v in os.environ.items() if k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR')}
        argv = provisioning.engine_argv(bundle, dest) + ['--version']
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=30, env=clean_env)
        version = parse_version((proc.stdout or '') + '\n' + (proc.stderr or ''))
        result['version'] = version
        result['version_ok'] = bool(version) and proc.returncode == 0
        s_argv = smoke_argv(engine, provisioning.engine_argv(bundle, dest), provider)
        if s_argv is None:
            result['error'] = 'smoke_unsupported'
            return result
        proc2 = subprocess.run(s_argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=timeout, env=clean_env)
        out = (proc2.stdout or '') + '\n' + (proc2.stderr or '')
        result['prompt_ok'] = SMOKE_MARKER in out and proc2.returncode == 0
        result['state'] = 'green' if (result['version_ok'] and result['prompt_ok']) else 'failed'
        if result['state'] != 'green' and not result.get('error'):
            result['error'] = 'smoke_failed'
        return result
    except subprocess.TimeoutExpired:
        result['error'] = 'smoke_timeout'
        return result
    except Exception as exc:
        result['error'] = type(exc).__name__
        return result


def decide_canary(canary_smoke: dict, rest_hosts: list[str]) -> dict:
    """Canary gate (AC3): green canary rolls out; failure keeps/rolls back + one alert."""
    green = isinstance(canary_smoke, dict) and canary_smoke.get('state') == 'green'
    if green:
        return {'decision': 'rollout', 'hosts': list(rest_hosts), 'alert': None}
    engine = (canary_smoke or {}).get('engine', 'unknown')
    error = (canary_smoke or {}).get('error', 'smoke_failed')
    alert = {
        'kind': 'engine_rollout',
        'subject': f'canary-{engine}',
        'state': 'attention',
        'detail': (f'Engine rollout canary FAILED on the canary host for {engine} '
                   f'({error}). Kept the previous version; rollout to '
                   f'{len(rest_hosts)} host(s) is held. Inspect the canary smoke, '
                   f'then rerun `hub engines update --engine {engine}`.'),
    }
    return {'decision': 'rollback', 'hosts': [], 'alert': alert}


def check_host() -> dict:
    """Full per-engine check run on this host (AC5 source of truth)."""
    desired = desired_versions()
    try:
        import socket
        machine = socket.gethostname()
    except Exception:
        machine = 'unknown'
    engines: dict[str, dict] = {}
    for engine in ENGINE_ORDER:
        provider = PROVIDER_FOR[engine]
        installed = None
        try:
            installed = installed_version(engine)
        except Exception:
            installed = None
        try:
            latest = latest_available(engine)
        except Exception:
            latest = desired.get(engine)
        # Self-update check across managed homes of this provider.
        disabled = True
        try:
            from . import provisioning
            state = provisioning.read_json(provisioning.root() / 'state.json', [])
            homes = []
            for entry in state:
                if isinstance(entry, dict) and entry.get('provider') == provider:
                    for cand in provisioning.root().glob(entry['label'] + '-*/home'):
                        if cand.is_dir():
                            homes.append(cand)
            if homes:
                disabled = all(self_update_disabled_ok(h, provider) for h in homes)
            else:
                disabled = installed is None
        except Exception:
            disabled = False
        prev = last_result(engine)
        engines[engine] = {
            'installed': installed,
            'desired': desired.get(engine),
            'latest': latest,
            'self_update_disabled': bool(disabled),
            'last_result': prev,
        }
    return {'machine': machine, 'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'desired': desired, 'engines': engines}
