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
            with urllib.request.urlopen(u, timeout=30) as r:
                return r.read(256).decode('ascii', 'replace')
    text = fetch(url).strip().split()
    sha = text[0].lower() if text else ''
    return sha if SHA.fullmatch(sha) else None


def task_script(base_url, log):
    """The one-shot task body: the public installer, nothing else."""
    base = base_url.rstrip('/')
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+(:[0-9]{1,5})?', base):
        raise ValueError('Package update needs an https Hub origin')
    log = "'" + str(log).replace("'", "''") + "'"
    return ("$ErrorActionPreference = 'Continue'\n"
            "$env:HUB_BASE_URL = '" + base + "'\n"
            "& { irm '" + base + "/host/install.ps1' | iex } *>> " + log + "\n")


def register_task(base_url, state_dir, run=None):
    import subprocess
    run = run or subprocess.run
    script = Path(state_dir) / 'package-update.ps1'
    script.write_text(task_script(base_url, Path(state_dir) / 'package-update.log'), encoding='utf-8')
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
    state = step_once(base_url, bootstrap.user_install_root())
    print(json.dumps({'event': 'package_update', 'state': state}), flush=True)
    return state
