"""Self-update for Windows package installs (intake bf0863c4).

A Windows host installed by `irm .../host/install.ps1 | iex` runs from an
immutable release tree, not a git checkout, so the host-release poll reports
not_a_git_checkout and fixes reached the machine only when a human reran the
installer. When the served hub.pyz differs from the archive the current
release was built from, register a one-shot per-user scheduled task that runs
the same public installer (no admin, no UAC). The installer verifies the
archive checksum, builds a new release beside the old one and hands the
controller over, exactly as a manual rerun does.
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

CHECK_S = 900
RETRY_S = 6 * 3600
# Each update is a full new release (node, git, engines, venv) and old ones
# are kept for owner-led cleanup, so updates are spaced out and need room.
MIN_INTERVAL_S = 12 * 3600
DISK_FLOOR_GB = 50
SHA = re.compile(r'[a-f0-9]{64}')
# Bounded, reversible operator hold (intake e3e6bade): while a valid hold is
# active no update task is scheduled. A hold is never permanent: it must name
# its reason and expire within MAX_HOLD_S of being set; malformed, future-set,
# over-long or expired holds are ignored. Deleting the file releases it.
HOLD_FILE = 'package-update-hold.json'
MAX_HOLD_S = 72 * 3600
REASON = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')


def _iso(value):
    import datetime as dt
    if not isinstance(value, str) or not value.endswith('Z'):
        return None
    try:
        return dt.datetime.fromisoformat(value[:-1] + '+00:00').timestamp()
    except ValueError:
        return None


def active_hold(state_dir, now=None):
    """The active hold {until, reason} or None. Never raises."""
    now = time.time() if now is None else now
    try:
        hold = json.loads((Path(state_dir) / HOLD_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(hold, dict) or not isinstance(hold.get('reason'), str) or not REASON.fullmatch(hold['reason']):
        return None
    set_at, until = _iso(hold.get('set_at')), _iso(hold.get('until'))
    if set_at is None or until is None or set_at > now + 60 or until <= now or until - set_at > MAX_HOLD_S:
        return None
    return {'until': hold['until'], 'reason': hold['reason']}


def set_hold(state_dir, hours, reason, now=None):
    """Write a bounded hold (at most MAX_HOLD_S) and return it."""
    import datetime as dt
    now = time.time() if now is None else now
    if not REASON.fullmatch(reason or ''):
        raise ValueError('hold reason must be a short lowercase slug')
    seconds = float(hours) * 3600
    if not 0 < seconds <= MAX_HOLD_S:
        raise ValueError('hold must be positive and at most 72 hours')
    stamp = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    hold = {'set_at': stamp(now), 'until': stamp(now + seconds), 'reason': reason}
    path = Path(state_dir) / HOLD_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import uuid
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(hold), encoding='utf-8')
        os.replace(tmp, path)  # atomic within the directory
    finally:
        if tmp.exists():
            tmp.unlink()
    return hold


# Exit code the task preflight uses to tell the update task "held: skip the installer".
PREFLIGHT_HELD = 75


def preflight(state_dir, now=None):
    """Installer-task preflight: PREFLIGHT_HELD while a valid hold is active, else 0."""
    hold = active_hold(state_dir, now)
    print(json.dumps({'event': 'package_update_preflight', 'hold': hold}), flush=True)
    return PREFLIGHT_HELD if hold is not None else 0


def disable_pending_task(run=None):
    """Disable this user's pending AgentHubUpdate task; never touch a running one.

    Reversible: the next scheduling re-registers it enabled (Register-ScheduledTask
    -Force). Returns absent | running | disabled | disabled_now | unsupported | error.
    """
    if sys.platform != 'win32' and run is None:
        return 'unsupported'
    import subprocess
    run = run or subprocess.run
    ps = (" $id=[Security.Principal.WindowsIdentity]::GetCurrent();"
          " $name='AgentHubUpdate-'+$id.User.Value;"
          " $t=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;"
          " if (-not $t) { 'absent' } elseif ($t.State -eq 'Running') { 'running' }"
          " elseif ($t.State -eq 'Disabled') { 'disabled' }"
          " else { Disable-ScheduledTask -TaskName $name | Out-Null; 'disabled_now' }")
    try:
        res = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop';" + ps],
                  capture_output=True, text=True, timeout=60)
    except Exception:
        return 'error'
    state = (res.stdout or '').strip().splitlines()[-1:] or ['']
    return state[0] if res.returncode == 0 and state[0] in ('absent', 'running', 'disabled', 'disabled_now') else 'error'


def clear_hold(state_dir):
    try:
        (Path(state_dir) / HOLD_FILE).unlink()
        return True
    except FileNotFoundError:
        return False


def running_release():
    """The immutable Windows release this module runs from, if any.

    package/host/agent_hub_host/<this file> inside <root>/releases/<16hex>-<12hex>.
    """
    release = Path(__file__).resolve().parents[3]
    if release.parent.name == 'releases' and re.fullmatch(r'[a-f0-9]{16}-[a-f0-9]{12}', release.name):
        return release
    return None


def current_archive_sha(release=None):
    """Archive sha256 of the running Windows release, or None if unknown."""
    release = release or running_release()
    if not release:
        return None
    try:
        marker = json.loads((Path(release) / 'runtime-ready.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    sha = marker.get('archive_sha256') if isinstance(marker, dict) else None
    if not (isinstance(sha, str) and SHA.fullmatch(sha) and Path(release).name.startswith(sha[:16])):
        return None
    return sha


def served_archive_sha(base_url, fetch=None):
    url = base_url.rstrip('/') + '/cli/hub.pyz.sha256'
    if fetch is None:
        def fetch(u):
            # Cloudflare answers urllib's default "Python-urllib/x.y" with 403, which
            # kept every Windows package host on its install-day release.
            from .api import USER_AGENT
            req = urllib.request.Request(u, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(256).decode('ascii', 'replace')
    text = fetch(url).strip().split()
    sha = text[0].lower() if text else ''
    return sha if SHA.fullmatch(sha) else None


def task_script(base_url, log, preflight_python=None, package_dir=None):
    """The one-shot task body: an optional hold preflight, then the public installer.

    With preflight_python/package_dir the task first runs
    `python -m agent_hub_host.package_update preflight` and exits without
    running the installer when it reports a valid hold (exit PREFLIGHT_HELD), so
    a task scheduled before a hold was set cannot reinstall (intake e3e6bade).
    """
    base = base_url.rstrip('/')
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(:[0-9]{1,5})?', base):
        raise ValueError('Package update needs an https Hub origin')
    quote = lambda v: "'" + str(v).replace("'", "''") + "'"
    log = quote(log)
    script = ("$ErrorActionPreference = 'Continue'\n"
              "$env:HUB_BASE_URL = '" + base + "'\n")
    if preflight_python and package_dir:
        script += ("$env:PYTHONPATH = " + quote(package_dir) + "\n"
                   "& " + quote(preflight_python) + " -m agent_hub_host.package_update preflight *>> " + log + "\n"
                   "if ($LASTEXITCODE -eq " + str(PREFLIGHT_HELD) + ") { exit 0 }\n")
    return script + "& { irm '" + base + "/host/install.ps1' | iex } *>> " + log + "\n"


def register_task(base_url, state_dir, run=None):
    import subprocess
    run = run or subprocess.run
    script = Path(state_dir) / 'package-update.ps1'
    script.write_text(task_script(base_url, Path(state_dir) / 'package-update.log',
                                  preflight_python=sys.executable,
                                  package_dir=Path(__file__).resolve().parents[1]), encoding='utf-8')
    quote = lambda v: "'" + str(v).replace("'", "''") + "'"
    # Per-user task like the controller's: no password, RunAs or UAC.
    ps = (" $id=[Security.Principal.WindowsIdentity]::GetCurrent();"
          " $name='AgentHubUpdate-'+$id.User.Value;"
          " $a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
          + quote('-NoProfile -ExecutionPolicy Bypass -File "' + str(script) + '"') + ";"
          " $t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(15);"
          " $p=New-ScheduledTaskPrincipal -UserId $id.Name -LogonType Interactive -RunLevel Limited;"
          " $s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew"
          " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable;"
          " Register-ScheduledTask -TaskName $name -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null;")
    res = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop';" + ps],
              capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError('update task registration failed')


def step_once(base_url, state_dir, *, now=None, fetch=None, register=None, release=None, free_gb=None):
    """One check. Returns a fixed state slug; never raises for expected cases."""
    now = time.time() if now is None else now
    current = current_archive_sha(release)
    if current is None:
        return 'unsupported'
    served = served_archive_sha(base_url, fetch)
    if served is None:
        return 'served_unknown'
    if served == current:
        return 'current'
    if active_hold(state_dir, now) is not None:
        return 'held'
    marker = Path(state_dir) / 'package-update.json'
    try:
        last = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        last = {}
    # One attempt per served archive per RETRY_S: a failing installer is not
    # rerun in a loop, and a newer deploy is picked up on the next check.
    if not isinstance(last, dict):
        last = {}
    try:
        age = now - float(last['at'])
    except (KeyError, TypeError, ValueError):
        age = None  # no earlier attempt
    if age is not None and ((last.get('sha') == served and age < RETRY_S) or age < MIN_INTERVAL_S):
        return 'pending'
    if free_gb is None:
        import shutil
        free_gb = shutil.disk_usage(state_dir).free / (1024 ** 3)
    if free_gb < DISK_FLOOR_GB:
        return 'disk_low'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'sha': served, 'from': current, 'at': now}), encoding='utf-8')
    (register or register_task)(base_url, state_dir)
    return 'scheduled'


def maybe_update(clock=time.monotonic, _last=[None]):
    """Rate-limited check from the host-release loop; Windows package installs only."""
    if sys.platform != 'win32' or running_release() is None:
        return None
    if _last[0] is not None and clock() - _last[0] < CHECK_S:
        return None
    _last[0] = clock()
    from . import bootstrap, config
    base_url = config.load_host_config().get('base_url')
    if not base_url:
        return None
    state_dir = bootstrap.user_install_root()
    state = step_once(base_url, state_dir)
    event = {'event': 'package_update', 'state': state}
    if state == 'held':
        event['hold'] = active_hold(state_dir)
    print(json.dumps(event), flush=True)
    return state


def main(argv=None):
    """Operator CLI: python -m agent_hub_host.package_update hold HOURS REASON | release | show"""
    import argparse
    from . import bootstrap
    parser = argparse.ArgumentParser(prog='package_update')
    sub = parser.add_subparsers(dest='cmd', required=True)
    hold = sub.add_parser('hold')
    hold.add_argument('hours', type=float)
    hold.add_argument('reason')
    sub.add_parser('release')
    sub.add_parser('show')
    sub.add_parser('preflight')
    args = parser.parse_args(argv)
    state_dir = bootstrap.user_install_root()
    if args.cmd == 'preflight':
        return preflight(state_dir)
    if args.cmd == 'hold':
        result = {'hold': set_hold(state_dir, args.hours, args.reason), 'pending_task': disable_pending_task()}
    elif args.cmd == 'release':
        result = {'released': clear_hold(state_dir)}
    else:
        result = {'hold': active_hold(state_dir)}
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
