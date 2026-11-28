"""`hub host self-update` inside its real scheduled task (intake 56a34448).

DESKTOP-FP4OP26 stayed on fdcd5505 while the daily AgentHub2HostUpdate task
reported result 0 every morning and install.log had no entry: the installer
was started detached, its output went to DEVNULL and the launcher returned
success without waiting for it.

The launcher runs here as the action of a per-user scheduled task registered
the way host-install.ps1 registers AgentHub2HostUpdate (Interactive, Limited,
IgnoreNew), so the handoff is exercised against Task Scheduler itself. Only the
network is replaced: the installer is a local script that logs like the real one.
"""
import ctypes
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
    flags = 0x00000008 | 0x00000200 | (0x01000000 if scenario['mode'] == 'breakaway' else 0)
    try:
        child = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(script)], env={**os.environ, 'HUB2_INSTALL_MODE': 'update'},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=flags)
    except OSError as error:
        Path(scenario['state'], 'spawn-error.txt').write_text(repr(error), encoding='utf-8')
        return 0
    Path(scenario['state'], 'child-pid.txt').write_text(str(child.pid), encoding='utf-8')
    if scenario['mode'] == 'legacy_wait':
        time.sleep(scenario['launcher_seconds'])
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
  Add-Content -LiteralPath '{log}' -Value $line
}
Write-Step 'Hub2 host install from https://hub.example (mode=update)'
Start-Sleep -Seconds {seconds}
{tail}
"""


def powershell(script, check=True):
    done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-Command', script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
    if check and done.returncode != 0:
        raise AssertionError(f'powershell exited {done.returncode}: {done.stdout} {done.stderr}')
    return (done.stdout + done.stderr).strip()


def alive(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel32.CloseHandle(handle)


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
        self.publish(INSTALLER.format(log=self.state / 'install.log', seconds=2, tail=''))
        self.task = 'AgentHub2HostUpdateTest-' + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: powershell(
            f"Unregister-ScheduledTask -TaskName '{self.task}' -Confirm:$false -ErrorAction SilentlyContinue", False))
        (self.directory / 'driver.py').write_text(DRIVER, encoding='utf-8')

    def publish(self, installer):
        """Serve the installer locally with the manifest that makes it the verified release."""
        data = installer.encode('utf-8')
        manifest = {'host_version': PUBLISHED, 'artifacts': {
            'host-install.ps1': {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}}}
        (self.files / 'host-release.json').write_text(json.dumps(manifest), encoding='utf-8')
        (self.files / 'host-install.ps1').write_bytes(data)

    def run_task(self, mode, **extra):
        scenario = {'package': str(HOST), 'files': str(self.files), 'state': str(self.state), 'mode': mode,
                    'current': CURRENT, 'installed': PUBLISHED, 'launcher_seconds': 8, **extra}
        path = self.directory / 'scenario.json'
        path.write_text(json.dumps(scenario), encoding='utf-8')
        powershell(f'''
            $ErrorActionPreference = 'Stop'
            $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
            $action = New-ScheduledTaskAction -Execute '{sys.executable}' -Argument '-I "{self.directory / "driver.py"}" "{path}"'
            Register-ScheduledTask -TaskName '{self.task}' -Action $action -Settings $settings -Principal $principal -Description 'Hub2 host verified self-update (test)' -Force | Out-Null
            Start-ScheduledTask -TaskName '{self.task}'
        ''')
        state = lambda: powershell(f"(Get-ScheduledTask -TaskName '{self.task}').State")
        deadline = time.monotonic() + 60
        while state() != 'Running' and time.monotonic() < deadline:
            time.sleep(0.5)
        running = state() == 'Running'
        deadline = time.monotonic() + 180
        while state() == 'Running' and time.monotonic() < deadline:
            time.sleep(0.5)
        time.sleep(3)  # a detached installer that outlived the task would log by now
        evidence = {'mode': mode, 'task_started': running, 'task_state': state(),
                    'last_task_result': int(powershell(
                        f"(Get-ScheduledTaskInfo -TaskName '{self.task}').LastTaskResult")),
                    'session_id': powershell('(Get-Process -Id $PID).SessionId'),
                    'install_log': (self.state / 'install.log').read_text() if (self.state / 'install.log').exists() else None}
        child = self.state / 'child-pid.txt'
        if child.exists():
            evidence['installer_pid_alive_after_task'] = alive(child.read_text())
        error = self.state / 'spawn-error.txt'
        if error.exists():
            evidence['spawn_error'] = error.read_text()
        handoff = self.state / 'self-update.log'
        if handoff.exists():
            evidence['self_update_log'] = handoff.read_text(errors='replace')
        print(json.dumps(evidence, indent=2))
        return evidence

    def test_foreground_handoff_installs_and_the_task_reports_success(self):
        evidence = self.run_task('foreground')
        self.assertTrue(evidence['task_started'], 'the scheduled task never ran')
        self.assertEqual(evidence['last_task_result'], 0)
        self.assertIn('installed host_version=' + PUBLISHED, (self.state / 'install.log').read_text())
        log = evidence['self_update_log']
        self.assertIn('self-update start from=' + CURRENT + ' host_version=' + PUBLISHED, log)
        self.assertIn('Hub2 host install from https://hub.example (mode=update)', log)  # installer output kept
        self.assertIn('self-update exit code=0 reason=installed', log)

    def test_foreground_handoff_fails_the_task_with_the_installers_exit_code(self):
        self.publish(INSTALLER.format(log=self.state / 'install.log', seconds=0,
                                      tail="Write-Step 'install failed: pip install failed; see install.log'\nexit 3"))
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
        self.assertFalse(evidence['installer_pid_alive_after_task'])

    def test_the_same_detached_installer_runs_while_its_launcher_stays_alive(self):
        # Control: identical task, flags and installer; only the launcher lives 8 s.
        evidence = self.run_task('legacy_wait')
        self.assertTrue(evidence['task_started'], 'the scheduled task never ran')
        self.assertIn('installed host_version=' + PUBLISHED, evidence['install_log'] or '')


if __name__ == '__main__':
    unittest.main()
