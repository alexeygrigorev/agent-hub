"""Update a verified Linux host release without changing a development checkout.

The Git checkout supplies objects only. The running release supplies the trusted
builder/switcher; activation preserves the current user service configuration.
The normal supervisor restart marker loads the new unit after the report lands.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .self_update import SHA, UpdateRefused, _git, _run

SERVICE = 'agent-hub-session-host.service'
MARKER = '# Managed by Agent Hub desired host release.\n'
DROPIN = 'zz~~hub-desired-release.conf'
ORIGINS = {'https://github.com/alexeygrigorev/agent-hub.git',
           'https://github.com/alexeygrigorev/agent-hub',
           'git@github.com:alexeygrigorev/agent-hub.git'}


def manifest_sha(root):
    try:
        data = json.loads((Path(root) / 'release-manifest.json').read_text(encoding='utf-8'))
        sha = data.get('release_rev') if isinstance(data, dict) else None
        return sha.lower() if isinstance(sha, str) and SHA.fullmatch(sha.lower()) else None
    except (OSError, ValueError):
        return None


def _require(ok, reason):
    if not ok:
        raise UpdateRefused(reason)


def apply(target, before, request, *, root, source=None, run=subprocess.run,
          python=None, disk_usage=shutil.disk_usage):
    """Return (state, error, running_sha); updated asks the supervisor to restart."""
    try:
        return _apply(target, before, request, root=Path(root), source=source,
                      run=run, python=python or sys.executable, disk_usage=disk_usage)
    except UpdateRefused as exc:
        return 'refused', exc.reason, before
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'failed', 'immutable_update_failed', before


def _apply(target, before, request, *, root, source, run, python, disk_usage):
    if not sys.platform.startswith('linux'):
        return 'unsupported', 'immutable_platform_unsupported', before
    _require(isinstance(target, str) and SHA.fullmatch(target), 'invalid_release_target')
    if not before or manifest_sha(root) != before or root.name != before:
        return 'unsupported', 'immutable_layout_missing', before
    if before == target:
        return 'current', None, before
    scripts = root / 'scripts'
    if not all((scripts / name).is_file() for name in (
            'build-host-release.py', 'verify-host-release.py', 'switch-host-release.py')):
        return 'unsupported', 'release_update_kit_missing', before
    source = Path(source or os.environ.get('AGENT_HUB_SOURCE_REPO') or
                  Path.home() / 'git' / 'agent-hub').resolve()
    _require((source / '.git').exists(), 'source_repository_missing')
    code, origin = _git(run, source, 'remote', 'get-url', 'origin', timeout=30)
    _require(code == 0 and origin in ORIGINS, 'untrusted_release_origin')
    observation = request('GET', '/api/fleet/resources/watchdog') or {}
    config = observation.get('config', {}) if isinstance(observation, dict) else {}
    floor = config.get('disk_floor_gb') if isinstance(config, dict) else None
    _require(type(floor) in (int, float) and 1 <= floor <= 1024,
             'release_disk_config_unavailable')
    _require(disk_usage(root.parent).free / (1024 ** 3) >= floor + 1,
             'release_disk_headroom')

    unit_dir = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd' / 'user' / (SERVICE + '.d')
    unit_dir = unit_dir.resolve()
    _require(unit_dir.is_relative_to(Path.home().resolve()), 'unsafe_release_unit_directory')
    code, paths = _run(run, ['systemctl', '--user', 'show', SERVICE,
                            '--property=DropInPaths', '--value'], root, 30)
    _require(code == 0, 'release_service_unavailable')
    dropins = [Path(p) for p in shlex.split(paths)]
    candidates = []
    for path in dropins:
        if path.parent.resolve() != unit_dir or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_uid == os.getuid() and str(root) in path.read_text(encoding='utf-8'):
            candidates.append(path)
    _require(bool(candidates), 'release_service_template_missing')
    destination = unit_dir / DROPIN
    _require(all(p.name <= DROPIN for p in dropins), 'release_dropin_precedence')
    if destination.exists() or destination.is_symlink():
        _require(not destination.is_symlink() and destination.is_file() and
                 destination.stat().st_uid == os.getuid() and
                 destination.read_text(encoding='utf-8').startswith(MARKER),
                 'release_dropin_conflict')
    template = sorted(candidates, key=lambda p: p.name)[-1].read_text(encoding='utf-8')

    _require(_git(run, source, 'fetch', '--quiet', 'origin', 'main')[0] == 0, 'fetch_failed')
    _require(_git(run, source, 'merge-base', '--is-ancestor', target, 'origin/main')[0] == 0,
             'not_on_origin_main')
    _require(_git(run, source, 'merge-base', '--is-ancestor', before, target)[0] == 0,
             'not_fast_forward')
    code, _ = _run(run, [python, str(scripts / 'build-host-release.py'), '--rev', target,
                         '--source-repo', str(source), '--releases-dir', str(root.parent)],
                   root, 900, umask=0o077)
    if code:
        return 'failed', 'release_build_failed', before
    release = root.parent / target
    _require(release.is_dir() and not release.is_symlink() and manifest_sha(release) == target,
             'release_build_provenance')
    # A superseded goal must not activate after a long build.
    _require((request('GET', '/api/fleet/routing/host-release') or {}).get('sha') == target,
             'release_target_changed')
    temporary = root / '.local' / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    _require(temporary.resolve().is_relative_to(root.resolve()), 'unsafe_release_tmp')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=temporary,
                                     prefix='host-goal-', suffix='.conf', delete=False) as handle:
        path = Path(handle.name)
        handle.write(template if template.startswith(MARKER) else MARKER + template)
    try:
        code, _ = _run(run, [python, str(scripts / 'switch-host-release.py'),
                             '--release', str(release), '--template', str(path),
                             '--dropin', str(destination)], root, 120, umask=0o077)
    finally:
        path.unlink()
    if code:
        return 'failed', 'release_switch_failed', before
    # The currently running module is still the old release until the normal
    # restart marker drains the controller and the supervisor restarts the unit.
    return 'updated', None, before
