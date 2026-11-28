"""`hub host self-update` inside its real scheduled task (intake 56a34448).

DESKTOP-FP4OP26 stayed on fdcd5505 while the daily AgentHub2HostUpdate task
reported result 0 every morning and install.log had no entry. `hub host
self-update` started the installer with DETACHED_PROCESS, and Windows PowerShell
started that way exits 0 within a tenth of a second without running the script;
its output went to DEVNULL and the launcher returned success at once.

The launcher runs here as the action of a per-user scheduled task registered
the way host-install.ps1 registers AgentHub2HostUpdate (Interactive, Limited,
IgnoreNew), so the handoff is exercised against Task Scheduler itself. Only the
network is replaced: the installer is a local script that logs like the real one.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

WINDOWS = sys.platform == 'win32'
HOST = Path(__file__).resolve().parents[1]
CURRENT, PUBLISHED = 'c' * 40, 'a' * 40

# The launcher, run by the scheduled task exactly like `python -I -m ... self-update`.
DRIVER = '''
import io, json, os, subprocess, sys, time
from pathlib import Path
from unittest.mock import patch
scenario = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
sys.path.insert(0, scenario['package'])
from agent_hub_host import windows_host

class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False

def opener(url, timeout):
    return Response(Path(scenario['files'], url.rsplit('/', 1)[1]).read_bytes(), url)

def launcher_before_the_fix():
    # Start the verified installer detached, discard its output, report success.
    version, data = windows_host.verified_installer('https://hub.example', opener)
    script = Path(scenario['state'], 'tmp', 'host-install.ps1')
    script.write_bytes(data)
    detached, new_group, no_window, breakaway = 0x00000008, 0x00000200, 0x08000000, 0x01000000
    flags = {'legacy': detached | new_group, 'legacy_wait': detached | new_group,
             'legacy_files': detached | new_group, 'no_window': no_window,
             'breakaway': detached | new_group | breakaway}[scenario['mode']]
    if scenario['mode'] == 'legacy_files':
        handles = {'stdout': open(Path(scenario['state'], 'installer-output.txt'), 'wb'), 'stderr': subprocess.STDOUT}
    else:
        handles = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    try:
        child = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(script)], env={**os.environ, 'HUB2_INSTALL_MODE': 'update'},
                                 stdin=subprocess.DEVNULL, creationflags=flags, **handles)
    except OSError as error:
        Path(scenario['state'], 'spawn-error.txt').write_text(repr(error), encoding='utf-8')
        return 0
    Path(scenario['state'], 'child-pid.txt').write_text(str(child.pid), encoding='utf-8')
    watched = scenario['launcher_seconds'] if scenario['mode'] in ('legacy_wait', 'legacy_files') else 0
    started = time.monotonic()
    outcome = []
    while time.monotonic() - started < watched:
        time.sleep(0.05)
        if child.poll() is not None:
            outcome = [{'exited_after_s': round(time.monotonic() - started, 2), 'exit_code': child.returncode}]
            break
    Path(scenario['state'], 'child-exit.json').write_text(json.dumps({'launcher_alive_s': watched, 'outcome': outcome}))
    return 0

with patch.object(windows_host, 'state_root', return_value=Path(scenario['state'])), \\
        patch.object(windows_host, 'host_release', return_value=(scenario['current'], 1)), \\
        patch.object(windows_host, 'installed_host_version', return_value=scenario['installed']):
    if scenario['mode'] == 'foreground':
        sys.exit(windows_host.windows_self_update('https://hub.example', opener=opener))
    sys.exit(launcher_before_the_fix())
'''

# Local stand-in for the verified installer: the same first statements and
# logging as deploy/host-install.ps1, then success or a reported failure.
INSTALLER = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
function Write-Step([string]$Message) {
  $line = (Get-Date).ToString('o') + ' ' + $Message
  Write-Host $line
  Add-Content -LiteralPath '@LOG@' -Value $line
}
Write-Step 'Hub2 host install from https://hub.example (mode=update)'
Start-Sleep -Seconds @SECONDS@
@TAIL@
"""
SUCCESS_TAIL = "Write-Step 'installed host_version=" + PUBLISHED + "'"
DETACHED, NEW_PROCESS_GROUP, NO_WINDOW, BREAKAWAY = 0x00000008, 0x00000200, 0x08000000, 0x01000000
POWERSHELL = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File']


def powershell(script, check=True):
    done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-Command', script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    if check and done.returncode != 0:
        raise AssertionError(f'powershell exited {done.returncode}: {done.stdout} {done.stderr}')
    return (done.stdout + done.stderr).strip()


@unittest.skipUnless(WINDOWS, 'needs the Windows Task Scheduler')
class SelfUpdateInsideScheduledTaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='hub2-self-update-task-'))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.state = self.directory / 'state'
        self.files = self.directory / 'files'
        self.state.mkdir()
        self.files.mkdir()
        (self.state / 'tmp').mkdir()
        self.publish(INSTALLER.replace('@TAIL@', SUCCESS_TAIL))
        self.task = 'AgentHub2HostUpdateTest-' + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: powershell(
            f"Unregister-ScheduledTask -TaskName '{self.task}' -Confirm:$false -ErrorAction SilentlyContinue", False))
        (self.directory / 'driver.py').write_text(DRIVER, encoding='utf-8')

    def publish(self, installer):
        """Serve the installer locally with the manifest that makes it the verified release."""
        data = (installer.replace('@LOG@', str(self.state / 'install.log')).replace('@SECONDS@', '2')
                .replace('@TAIL@', '').encode('utf-8'))
        manifest = {'host_version': PUBLISHED, 'artifacts': {
            'host-install.ps1': {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}}}
        (self.files / 'host-release.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.files / 'host-install.ps1').write_bytes(data)

    def run_task(self, mode, **extra):
        scenario = {'package': str(HOST), 'files': str(self.files), 'state': str(self.state), 'mode': mode,
                    'current': CURRENT, 'installed': PUBLISHED, 'launcher_seconds': 8, **extra}
        path = self.directory / 'scenario.json'
        path.write_text(json.dumps(scenario), encoding='utf-8')
        for leftover in self.state.rglob('*'):
            if leftover.is_file():
                leftover.unlink()
        powershell(f'''
            $ErrorActionPreference = 'Stop'
            $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
            $action = New-ScheduledTaskAction -Execute '{sys.executable}' -Argument '-I "{self.directory / "driver.py"}" "{path}"'
            Register-ScheduledTask -TaskName '{self.task}' -Action $action -Settings $settings -Principal $principal -Description 'Hub2 host verified self-update (test)' -Force | Out-Null
            Start-ScheduledTask -TaskName '{self.task}'
        ''')
        def state():
            return powershell(f"(Get-ScheduledTask -TaskName '{self.task}').State")
        deadline = time.monotonic() + 240
        while state() == 'Running' and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(3)  # a detached installer that outlived its task would log by now
        info = powershell(f"$i = Get-ScheduledTaskInfo -TaskName '{self.task}'; "
                          '"{0}|{1}|{2}" -f $i.LastTaskResult, $i.LastRunTime.ToString("o"), $i.NumberOfMissedRuns')
        result, last_run, missed = info.split('|')
        evidence = {'mode': mode, 'task_state': state(), 'last_task_result': int(result), 'last_run_time': last_run,
                    'missed_runs': int(missed), 'session_id': powershell('(Get-Process -Id $PID).SessionId'),
                    'install_log': (self.state / 'install.log').read_text() if (self.state / 'install.log').exists() else None}
        # 267011 is SCHED_S_TASK_HAS_NOT_RUN: a task shorter than one poll never shows as Running.
        evidence['task_started'] = evidence['last_task_result'] != 267011
        for name, key in (('child-exit.json', 'child_exit'), ('spawn-error.txt', 'spawn_error'),
                          ('installer-output.txt', 'installer_output')):
            if (self.state / name).exists():
                evidence[key] = (self.state / name).read_text(errors='replace')
        handoff = self.state / 'self-update.log'
        if handoff.exists():
            evidence['self_update_log'] = handoff.read_text(errors='replace')
        print(json.dumps(evidence, indent=2))
        return evidence

    def test_foreground_handoff_installs_and_the_task_reports_success(self):
        evidence = self.run_task('foreground')
        self.assertTrue(evidence['task_started'], 'the scheduled task never ran')
        self.assertEqual(evidence['last_task_result'], 0)
        self.assertIn('installed host_version=' + PUBLISHED, evidence['install_log'])
        log = evidence['self_update_log']
        self.assertIn('self-update start from=' + CURRENT + ' host_version=' + PUBLISHED, log)
        self.assertIn('Hub2 host install from https://hub.example (mode=update)', log)  # installer output kept
        self.assertIn('self-update exit code=0 reason=installed', log)

    def test_foreground_handoff_fails_the_task_with_the_installers_exit_code(self):
        self.publish(INSTALLER.replace('@SECONDS@', '0').replace(
            '@TAIL@', "Write-Step 'install failed: pip install failed; see install.log'\nexit 3"))
        evidence = self.run_task('foreground')
        self.assertTrue(evidence['task_started'], 'the scheduled task never ran')
        self.assertEqual(evidence['last_task_result'], 3)
        self.assertIn('install failed: pip install failed; see install.log', evidence['install_log'])
        self.assertIn('self-update exit code=3 reason=installer_failed', evidence['self_update_log'])

    def test_detached_installer_dies_with_the_task_action_and_the_task_still_reports_success(self):
        # The failure from DESKTOP-FP4OP26, reproduced: result 0, no install.log line.
        evidence = self.run_task('legacy')
        self.assertTrue(evidence['task_started'], 'the scheduled task never ran')
        self.assertEqual(evidence['last_task_result'], 0)
        self.assertIsNone(evidence['install_log'], 'the detached installer survived its task')

    def spawn_detached_here(self):
        """The launcher's old spawn, outside any scheduled task."""
        script = self.state / 'tmp' / 'host-install.ps1'
        script.write_bytes((self.files / 'host-install.ps1').read_bytes())
        started = time.monotonic()
        child = subprocess.Popen(POWERSHELL + [str(script)], env={**os.environ, 'HUB2_INSTALL_MODE': 'update'},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=DETACHED | NEW_PROCESS_GROUP)
        code = child.wait(timeout=60)
        log = self.state / 'install.log'
        return {'exited_after_s': round(time.monotonic() - started, 2), 'exit_code': code,
                'install_log': log.read_text() if log.exists() else None}

    def test_detached_powershell_reports_success_without_running_the_script(self):
        # Cause of the Desktop failure, outside Task Scheduler: no job, no task,
        # no launcher exit, and PowerShell still exits 0 without a log line.
        finding = self.spawn_detached_here()
        print(json.dumps({'mode': 'detached_direct', **finding}, indent=2))
        self.assertEqual(finding['exit_code'], 0)
        self.assertLess(finding['exited_after_s'], 3)
        self.assertIsNone(finding['install_log'])

    def test_why_the_detached_installer_never_runs_inside_the_task(self):
        # Same task and installer each time; one variable per run.
        findings = {mode: self.run_task(mode) for mode in ('legacy_wait', 'legacy_files', 'no_window', 'breakaway')}
        self.assertTrue(all(finding['task_started'] for finding in findings.values()), 'a scheduled task never ran')
        # Not the launcher's exit: PowerShell exits 0 at once while it is still alive.
        self.assertIsNone(findings['legacy_wait']['install_log'])
        self.assertEqual(json.loads(findings['legacy_wait']['child_exit'])['outcome'][0]['exit_code'], 0)
        # Not the discarded output either: kept in a file, it stays empty.
        kept = findings['legacy_files']
        self.assertEqual(json.loads(kept['child_exit'])['outcome'][0]['exit_code'], 0)
        self.assertIsNone(kept['install_log'])
        self.assertEqual(kept['installer_output'], '')
        # Given a console instead of none, the same script runs.
        self.assertIn('Hub2 host install from https://hub.example (mode=update)', findings['no_window']['install_log'])
        # A task's job cannot be left, so waiting in the foreground is the only way.
        self.assertIn('Access is denied', findings['breakaway']['spawn_error'])

if __name__ == '__main__':
    unittest.main()
