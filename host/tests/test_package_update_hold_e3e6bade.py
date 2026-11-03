"""Bounded, reversible Windows package-update hold (intake e3e6bade).

DESKTOP-FP4OP26 was rolled back to ff295a5 because the served build (680642,
then 799cec4) was Host-canary red. The self-updater had no supported way to
pause: only its 12 h throttle stood between the host and reinstalling a red
build, and moving that marker would fake a timestamp. A hold file now pauses
scheduling for a stated reason, at most 72 h, and is released by deleting it.
"""
import json
import os
import time
from pathlib import Path

import pytest

from agent_hub_host import package_update as u


def make_release(tmp_path, sha):
    release = tmp_path / 'releases' / (sha[:16] + '-' + 'c' * 12)
    release.mkdir(parents=True)
    (release / 'runtime-ready.json').write_text(json.dumps({'archive_sha256': sha}))
    return release


def step(tmp_path, now, registered):
    release = make_release(tmp_path, 'd' * 64) if not (tmp_path / 'releases').exists() else next((tmp_path / 'releases').iterdir())
    return u.step_once('https://hub.example', tmp_path, release=release, now=now,
                       fetch=lambda url: 'e' * 64 + '\n', register=lambda *a: registered.append(a), free_gb=500)


def test_active_hold_blocks_scheduling_and_leaves_throttle_marker_untouched(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 24, 'canary-red-680642', now=now)
    registered = []
    assert step(tmp_path, now + 60, registered) == 'held'
    assert registered == []
    assert not (tmp_path / 'package-update.json').exists()
    assert u.active_hold(tmp_path, now + 60)['reason'] == 'canary-red-680642'


def test_hold_expires_and_release_resumes_normal_scheduling(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 1, 'canary-red', now=now)
    registered = []
    assert step(tmp_path, now + 3600 + 1, registered) == 'scheduled'  # expired: normal path
    assert len(registered) == 1
    u.set_hold(tmp_path, 1, 'canary-red', now=now)
    assert u.clear_hold(tmp_path) is True and u.clear_hold(tmp_path) is False
    assert u.active_hold(tmp_path, now) is None


def test_current_build_is_reported_current_even_while_held(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 1, 'x', now=now)
    release = make_release(tmp_path, 'd' * 64)
    assert u.step_once('https://hub.example', tmp_path, release=release, now=now,
                       fetch=lambda url: 'd' * 64, register=lambda *a: None) == 'current'


def test_hold_is_bounded_and_invalid_holds_are_ignored(tmp_path):
    now = time.time()
    for hours in (0, -1, 72.01, 1000):
        try:
            u.set_hold(tmp_path, hours, 'x', now=now)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{hours} h accepted')
    for reason in ('', 'Has Spaces', 'x' * 65):
        try:
            u.set_hold(tmp_path, 1, reason, now=now)
        except ValueError:
            pass
        else:
            raise AssertionError(f'reason {reason!r} accepted')
    path = tmp_path / u.HOLD_FILE
    bad = [
        'not json',
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00Z'}),                       # no until
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-10T00:00:00Z'}),  # > 72 h
        json.dumps({'reason': 'BAD REASON', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-01T01:00:00Z'}),
        json.dumps({'reason': 'x', 'set_at': '2099-01-01T00:00:00Z', 'until': '2099-01-02T00:00:00Z'}),  # set in future
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00+00:00', 'until': '2026-11-01T01:00:00+00:00'}),  # non-Z
    ]
    import datetime as dt
    t = dt.datetime(2026, 11, 1, 0, 40, tzinfo=dt.timezone.utc).timestamp()
    for text in bad:
        path.write_text(text)
        assert u.active_hold(tmp_path, t) is None, text
    path.write_text(json.dumps({'reason': 'ok', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-01T01:00:00Z'}))
    assert u.active_hold(tmp_path, t) == {'until': '2026-11-01T01:00:00Z', 'reason': 'ok'}


def test_cli_hold_show_release(tmp_path, monkeypatch, capsys):
    from agent_hub_host import bootstrap
    monkeypatch.setattr(bootstrap, 'user_install_root', lambda: tmp_path)
    assert u.main(['hold', '12', 'canary-red']) == 0
    assert json.loads(capsys.readouterr().out)['hold']['reason'] == 'canary-red'
    assert u.main(['show']) == 0
    assert json.loads(capsys.readouterr().out)['hold']['reason'] == 'canary-red'
    assert u.main(['release']) == 0
    assert json.loads(capsys.readouterr().out) == {'released': True}


# --- e3e v4 review: pending task, task preflight, atomic write -------------

def test_task_script_proceeds_only_on_preflight_exit_zero():
    script = u.task_script('https://hub.example', r'C:\l\package-update.log',
                           preflight_python=r'C:\r\venv\Scripts\python.exe', package_dir=r'C:\r\package\host')
    lines = script.splitlines()
    guard = lines.index(r"if (Test-Path -LiteralPath 'C:\r\venv\Scripts\python.exe' -PathType Leaf) {")
    pre = next(i for i, l in enumerate(lines) if 'agent_hub_host.package_update preflight' in l)
    gate = lines.index('if ($preflight -ne 0) {')
    inst = next(i for i, l in enumerate(lines) if '/host/install.ps1' in l)
    assert lines.index('$preflight = 1') < guard < pre < gate < inst
    assert '  $global:LASTEXITCODE = 1' in lines  # no stale exit code can read as success
    assert r"$env:PYTHONPATH = 'C:\r\package\host'" in lines
    assert 'package_update_preflight_skip' in script


def _run_task(tmp_path, preflight_python):
    """Execute the real task body in Windows PowerShell; report whether the installer line ran."""
    import subprocess
    log = tmp_path / 'package-update.log'
    # The installer origin is unroutable, so reaching it only leaves an irm error in the log.
    body = u.task_script('https://127.0.0.1:9', log, preflight_python=preflight_python, package_dir=tmp_path)
    marker = tmp_path / 'installer-reached'
    marker.unlink(missing_ok=True)
    body = body.replace("& { irm 'https://127.0.0.1:9/host/install.ps1' | iex }",
                        "& { Set-Content -LiteralPath '" + str(marker).replace("'", "''") + "' -Value 'reached' }")
    script = tmp_path / 'task.ps1'
    script.write_text(body, encoding='utf-8')
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                   capture_output=True, text=True, timeout=60)
    text = log.read_bytes().decode('utf-8', 'replace').replace('\x00', '') if log.exists() else ''
    return marker.exists(), text


@pytest.mark.skipif(os.name != 'nt', reason='executes the real Windows PowerShell task body')
def test_real_task_body_gates_installer_on_preflight_result(tmp_path):
    def fake(code):
        cmd = tmp_path / f'pre{code}.cmd'
        cmd.write_text(f'@exit /b {code}\r\n', encoding='ascii')
        return str(cmd)
    reached, _ = _run_task(tmp_path, fake(0))
    assert reached, 'exit 0 must proceed to the installer'
    for code in (u.PREFLIGHT_HELD, 1, 3):
        (tmp_path / 'package-update.log').unlink(missing_ok=True)
        reached, text = _run_task(tmp_path, fake(code))
        assert not reached and '"code":%d' % code in text, (code, text)
    (tmp_path / 'package-update.log').unlink(missing_ok=True)
    reached, text = _run_task(tmp_path, str(tmp_path / 'missing' / 'python.exe'))
    assert not reached and '"code":1' in text  # missing interpreter: skipped, not installed


def test_registered_task_carries_the_preflight(tmp_path):
    class R:
        returncode = 0
    u.register_task('https://hub.example', tmp_path, run=lambda *a, **k: R())
    body = (tmp_path / 'package-update.ps1').read_text(encoding='utf-8')
    assert body.index('package_update preflight') < body.index('/host/install.ps1')


def test_preflight_exit_codes(tmp_path, capsys):
    now = time.time()
    assert u.preflight(tmp_path, now) == 0
    u.set_hold(tmp_path, 2, 'canary-red', now=now)
    assert u.preflight(tmp_path, now + 1) == u.PREFLIGHT_HELD
    assert '"package_update_preflight"' in capsys.readouterr().out


def _ps(stdout, returncode=0, seen=None):
    def run(argv, **kw):
        if seen is not None:
            seen.append(argv[-1])
        return type('R', (), {'returncode': returncode, 'stdout': stdout})()
    return run


def test_disable_pending_task_never_touches_a_running_task():
    seen = []
    assert u.disable_pending_task(run=_ps('disabled_now\n', seen=seen)) == 'disabled_now'
    assert u.disable_pending_task(run=_ps('running\n')) == 'running'
    assert u.disable_pending_task(run=_ps('absent\n')) == 'absent'
    assert u.disable_pending_task(run=_ps('garbage\n')) == 'error'
    assert u.disable_pending_task(run=_ps('disabled_now\n', returncode=1)) == 'error'
    assert u.disable_pending_task(run=lambda *a, **k: (_ for _ in ()).throw(OSError('no ps'))) == 'error'
    script = seen[0]
    assert "elseif ($t.State -eq 'Running') { 'running' }" in script
    assert 'Disable-ScheduledTask' in script
    assert 'Stop-ScheduledTask' not in script and 'Unregister-ScheduledTask' not in script


def test_hold_cli_reports_pending_task_state(tmp_path, monkeypatch, capsys):
    from agent_hub_host import bootstrap
    monkeypatch.setattr(bootstrap, 'user_install_root', lambda: tmp_path)
    monkeypatch.setattr(u, 'disable_pending_task', lambda: 'disabled_now')
    assert u.main(['hold', '6', 'canary-red']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['pending_task'] == 'disabled_now' and out['hold']['reason'] == 'canary-red'
    assert u.main(['preflight']) == u.PREFLIGHT_HELD


def test_hold_write_is_atomic_and_leaves_no_temp(tmp_path, monkeypatch):
    import os
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, 'replace', lambda a, b: replaced.append((str(a), str(b))) or real(a, b))
    u.set_hold(tmp_path, 1, 'x')
    [(src, dst)] = replaced
    assert dst.endswith(u.HOLD_FILE) and src.endswith('.tmp')
    assert not list(tmp_path.glob(u.HOLD_FILE + '.*.tmp'))


def test_automatic_updates_keep_12h_spacing_while_operator_request_bypasses_once(tmp_path):
    release = make_release(tmp_path, 'd' * 64)
    registered = []
    call = lambda archive, now, **kw: u.step_once(
        'https://hub.example', tmp_path, release=release, now=now,
        fetch=lambda url: archive + '\n', register=lambda *a: registered.append(a), free_gb=500, **kw)
    t0 = 10_000.0
    assert call('e' * 64, t0) == 'scheduled'
    assert len(registered) == 1
    # Automatic check 1 h later sees a newer archive ('f' * 64) but still waits for the 12 h window.
    assert call('f' * 64, t0 + 3600) == 'pending'
    assert len(registered) == 1
    # After 12 h, automatic check schedules the new archive.
    t1 = t0 + u.MIN_INTERVAL_S + 1
    assert call('f' * 64, t1) == 'scheduled'
    assert len(registered) == 2
    # Another critical fix ('a' * 64) arrives 30 min later: automatic check waits, operator request bypasses once.
    assert call('a' * 64, t1 + 1800) == 'pending'
    assert call('a' * 64, t1 + 1800, requested_sha='1' * 40) == 'scheduled'
    assert len(registered) == 3
    # The same operator request does not bypass the 12 h window a second time.
    assert call('b' * 64, t1 + 2400, requested_sha='1' * 40) == 'pending'
    # Bounded retry policy: even a new operator request cannot re-trigger the same failing archive inside RETRY_S.
    assert call('a' * 64, t1 + 2400, requested_sha='2' * 40) == 'pending'
    assert len(registered) == 3


def test_host_release_pin_triggers_package_update_on_windows_package_install(tmp_path, monkeypatch):
    from agent_hub_host import config, self_update
    release = make_release(tmp_path, 'd' * 64)
    old_sha = '1' * 40
    new_sha = '2' * 40
    monkeypatch.setattr(u, 'running_release', lambda: release)
    monkeypatch.setattr(self_update, 'repo_root', lambda run=None: None)
    monkeypatch.setattr(self_update, 'release_sha', lambda package=None: old_sha)
    monkeypatch.setattr(self_update, 'ensure_windows_autostart', lambda root, run=None: ('configured', None))
    monkeypatch.setattr(config, 'load_host_config', lambda: {'base_url': 'https://hub.example'})
    monkeypatch.setattr(u, 'served_archive_sha', lambda base_url, fetch=None: 'e' * 64)
    registered = []
    monkeypatch.setattr(u, 'register_task', lambda base_url, state_dir: registered.append((base_url, Path(state_dir))))
    monkeypatch.setattr('shutil.disk_usage', lambda path: type('U', (), {'free': 200 * (1024 ** 3)})())
    # Seed a recent automatic update 1 h ago so we also verify the host-release pin bypasses the 12 h spacing once.
    now = time.time()
    (tmp_path / 'package-update.json').write_text(json.dumps({'sha': 'c' * 64, 'from': 'b' * 64, 'at': now - 3600}))
    reports = []
    desired = {'sha': new_sha}

    def request(method, path, body=None):
        if method == 'GET' and path == '/api/fleet/routing/host-release':
            return dict(desired)
        if method == 'POST' and path == '/api/fleet/routing/host-release/report':
            reports.append(body)
            return {'status': 'recorded'}
        raise AssertionError((method, path))

    assert self_update.step_once(request, root=None, log=lambda *a, **k: None) is False
    assert len(registered) == 1 and registered[0] == ('https://hub.example', tmp_path)
    assert reports[-1] == {'current_sha': old_sha, 'state': 'updating', 'error': None,
                           'autostart_state': 'configured', 'autostart_error': None}
    # Next poll while the background installer task is still pending stays in 'updating' without re-registering.
    assert self_update.step_once(request, root=None, log=lambda *a, **k: None) is False
    assert len(registered) == 1
    assert reports[-1]['state'] == 'updating' and reports[-1]['error'] is None
    # Active hold refuses cleanly with a fixed reason.
    u.set_hold(tmp_path, 2, 'canary-red', now=now)
    assert self_update.step_once(request, root=None, log=lambda *a, **k: None) is False
    assert reports[-1] == {'current_sha': old_sha, 'state': 'refused', 'error': 'package_update_held',
                           'autostart_state': 'configured', 'autostart_error': None}
    u.clear_hold(tmp_path)
    # Once the new release is running, step_once reports current.
    monkeypatch.setattr(self_update, 'release_sha', lambda package=None: new_sha)
    assert self_update.step_once(request, root=None, log=lambda *a, **k: None) is False
    assert reports[-1] == {'current_sha': new_sha, 'state': 'current', 'error': None,
                           'autostart_state': 'configured', 'autostart_error': None}
