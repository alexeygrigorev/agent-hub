"""Real scheduled-task registration for scripts/windows-host-autostart.ps1
(intake c964dbd6): registering an S4U principal is denied to non-elevated
users on locked-down Windows hosts, which left Tabo's autostart_state at
registration_failed forever. The script must therefore install a per-user
Limited task without elevation, falling back to an Interactive logon type,
and record the installed task name next to the runner.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'windows-host-autostart.ps1'


@unittest.skipUnless(os.name == 'nt' and SCRIPT.is_file(),
                     'Real scheduled-task registration evidence')
class RealAutostartRegistrationTests(unittest.TestCase):
    TASK = f'AgentHubSessionHost-Selftest{os.getpid()}'

    def setUp(self):
        self._unregister()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = pathlib.Path(self.directory.name) / 'run-session-host.ps1'
        self.runner.write_text('exit 0\n', encoding='ascii')

    def tearDown(self):
        self._unregister()

    def _unregister(self):
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        f'Unregister-ScheduledTask -TaskName "{self.TASK}" '
                        '-Confirm:$false -ErrorAction SilentlyContinue'],
                       capture_output=True, timeout=120)

    def _probe(self):
        """logon|user|runlevel|interval|expected user of the registered task."""
        out = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                              '$t = Get-ScheduledTask -TaskName "' + self.TASK + '" '
                              '-ErrorAction SilentlyContinue; '
                              'if ($t) { "{0}|{1}|{2}|{3}|{4}" -f $t.Principal.LogonType, '
                              '$t.Principal.UserId, $t.Principal.RunLevel, '
                              '$t.Triggers[0].Repetition.Interval, '
                              '[System.Security.Principal.WindowsIdentity]::GetCurrent().Name }'],
                             capture_output=True, text=True, timeout=120)
        return out.stdout.strip()

    def test_registers_a_per_user_limited_task_without_elevation(self):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
                                 '-Runner', str(self.runner), '-TaskName', self.TASK],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        logon, user, run_level, interval, expected_user = self._probe().split('|')
        # S4U where policy allows it; Interactive is the documented fallback.
        self.assertIn(logon, ('S4U', 'Interactive'))
        # Task Scheduler stores local accounts without the machine prefix.
        self.assertIn(user, (expected_user, expected_user.split('\\')[-1]))
        self.assertEqual(run_level, 'Limited')
        self.assertEqual(interval, 'PT1M')  # durable one-minute timer
        record = pathlib.Path(self.directory.name) / 'autostart-task-name.txt'
        self.assertEqual(record.read_text(encoding='ascii').strip(), self.TASK)
        # No console window at all, not even a flash (intake 446227dc).
        act = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                              '$a=(Get-ScheduledTask -TaskName "' + self.TASK + '").Actions[0]; '
                              '"$($a.Execute)|$($a.Arguments)"'],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        self.assertTrue(act.lower().split('|')[0].endswith('conhost.exe'), act)
        self.assertIn('--headless', act)
        self.assertIn('-WindowStyle Hidden', act)

    def test_task_name_pick_upgrades_a_user_owned_legacy_task(self):
        """The pick must compare Task Scheduler's bare account form
        (alexe) against the prefixed identity name (Tabo\\alexe): without
        normalization a user-owned legacy task is not upgraded in place and
        a second per-user task fights the running supervisor for the host
        journal lock (intake c964dbd6 follow-up)."""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT),
                                 '-Runner', str(self.runner), '-ResolveTaskNameOnly'],
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        picked = result.stdout.strip()
        identity = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                   '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                                  capture_output=True, text=True, timeout=120).stdout.strip()
        legacy = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 '$t = Get-ScheduledTask -TaskName "AgentHubSessionHost" '
                                 '-ErrorAction SilentlyContinue; '
                                 'if ($t) { "$($t.Principal.UserId)|$([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)" }'],
                                capture_output=True, text=True, timeout=120).stdout.strip()
        owns_legacy = False
        if legacy:
            legacy_user, identity_name = legacy.split('|')
            account = identity_name.split('\\')[-1]
            owns_legacy = legacy_user in (identity_name, account, identity)
        if owns_legacy:
            self.assertEqual(picked, 'AgentHubSessionHost')
        else:
            self.assertEqual(picked, f'AgentHubSessionHost-User-{identity}')


if __name__ == '__main__':
    unittest.main()
