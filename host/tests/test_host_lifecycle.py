import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, self_update


class HostLifecycleTest(unittest.TestCase):
    def test_release_provenance_comes_from_immutable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'host' / 'agent_hub_host'
            package.mkdir(parents=True)
            marker = package / 'self_update.py'
            marker.touch()
            manifest = Path(tmp) / 'release-manifest.json'
            manifest.write_text(json.dumps({'release_rev': 'a' * 40}))
            self.assertEqual(self_update.release_sha(marker), 'a' * 40)
            manifest.write_text(json.dumps({'release_rev': 'invalid'}))
            self.assertIsNone(self_update.release_sha(marker))

    def test_immutable_host_reports_sha_even_without_checkout(self):
        reports = []
        def request(method, path, body=None):
            if method == 'GET':
                return {'sha': 'b' * 40}
            reports.append(body)
        with patch.object(self_update.sys, 'platform', 'linux'), \
             patch.object(self_update, 'repo_root', return_value=None), \
             patch.object(self_update, 'release_sha', return_value='a' * 40):
            self.assertFalse(self_update.step_once(request, log=lambda *a, **k: None))
        self.assertEqual(reports[-1], {'current_sha': 'a' * 40, 'state': 'unsupported', 'error': 'immutable_layout_missing'})

    def test_capabilities_native_arch_replaces_stale_operator_label(self):
        dispatcher = object.__new__(fleet.Dispatcher)
        dispatcher.settings = {'capabilities': ['os:windows', 'arch:amd64', 'browser:tabo'], 'max_agents': 2, 'subscriptions': []}
        with patch('agent_hub_host.system.get_arch', return_value='arm64'):
            self.assertEqual(dispatcher.capabilities_put_payload()['needs'], ['os:windows', 'browser:tabo', 'arch:arm64'])
        self.assertEqual(dispatcher.settings['capabilities'][1], 'arch:amd64')

    def test_windows_autostart_installs_once_without_elevation(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / 'agent-hub' / 'run-session-host.ps1'
            runner.parent.mkdir()
            runner.touch()
            script = root / 'scripts' / 'windows-host-autostart.ps1'
            script.parent.mkdir()
            script.write_text('test fixture')
            calls = []
            def run(argv, **kw):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch.object(self_update.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': tmp}):
                self.assertEqual(self_update.ensure_windows_autostart(root, run), ('configured', None))
                self.assertEqual(self_update.ensure_windows_autostart(root, run), ('configured', None))
            self.assertEqual(len(calls), 1)
            self.assertIn('-NonInteractive', calls[0])
            self.assertNotIn('RunAs', calls[0])

    def test_windows_package_autostart_checks_the_controller_task(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / 'releases' / ('a' * 16 + '-' + 'b' * 12)
            release.mkdir(parents=True)
            runner = release / 'controller.ps1'
            runner.touch()
            calls = []

            def run(argv, **kw):
                calls.append(argv)
                self.assertIn(str(runner), argv[-1])
                self.assertIn('AgentHubController-', argv[-1])
                self.assertIn("MultipleInstances -ne 'IgnoreNew'", argv[-1])
                return subprocess.CompletedProcess(argv, 0, 'configured\n', '')

            with patch.object(self_update.sys, 'platform', 'win32'), \
                 patch('agent_hub_host.package_update.running_release', return_value=release):
                self.assertEqual(self_update.ensure_windows_autostart(None, run), ('configured', None))
            self.assertEqual(len(calls), 1)

    def test_windows_package_autostart_reports_missing_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / 'releases' / ('a' * 16 + '-' + 'b' * 12)
            release.mkdir(parents=True)
            with patch.object(self_update.sys, 'platform', 'win32'), \
                 patch('agent_hub_host.package_update.running_release', return_value=release):
                self.assertEqual(self_update.ensure_windows_autostart(None), ('wrapper_missing', None))

    def test_windows_autostart_failure_reports_a_bounded_secret_free_reason(self):
        # Intake c964dbd6 AC1: a failed registration must surface one of the
        # bounded autostart_error tokens, never free-form installer output.
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / 'agent-hub' / 'run-session-host.ps1'
            runner.parent.mkdir()
            runner.touch()
            script = root / 'scripts' / 'windows-host-autostart.ps1'
            script.parent.mkdir()
            script.write_text('test fixture')
            def failing_run(stdout, stderr='', code=1):
                def run(argv, **kw):
                    return subprocess.CompletedProcess(argv, code, stdout, stderr)
                return run
            cases = [
                ('AUTOSTART_ERROR=access_denied\nRegister-ScheduledTask : Access is denied.', 'access_denied'),
                ('AUTOSTART_ERROR:logon_type_denied', 'logon_type_denied'),
                ('AUTOSTART_ERROR=task_exists_other_user', 'task_exists_other_user'),
                ('AUTOSTART_ERROR=script_error', 'script_error'),
                # No marker: exit code alone still maps to the bounded fallback.
                ('Register-ScheduledTask : Access is denied. (0x80070005)', 'script_error'),
                ('', 'script_error'),
                # An unexpected token never reaches the Hub verbatim.
                ('AUTOSTART_ERROR=C:\\Users\\alexe\\secrets', 'script_error'),
            ]
            for output, expected in cases:
                with patch.object(self_update.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': tmp}):
                    marker = runner.parent / 'autostart-version.sha256'
                    if marker.exists():
                        marker.unlink()
                    state, error = self_update.ensure_windows_autostart(root, failing_run(output))
                    self.assertEqual(state, 'registration_failed', output)
                    self.assertEqual(error, expected, output)
                    self.assertNotIn('alexe', error or '')
                    self.assertNotIn('C:', error or '')

    def test_windows_autostart_report_clears_a_fixed_failure(self):
        # A machine that recovered must report autostart_error null so the Hub
        # clears the previously stored failure token.
        import os
        import subprocess
        reports = []
        def request(method, path, body=None):
            if method == 'GET':
                return {'sha': 'b' * 40}
            reports.append(body)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = root / 'agent-hub' / 'run-session-host.ps1'
            runner.parent.mkdir()
            runner.touch()
            script = root / 'scripts' / 'windows-host-autostart.ps1'
            script.parent.mkdir()
            script.write_text('test fixture')
            def run(argv, **kw):
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch.object(self_update.sys, 'platform', 'win32'), patch.dict(os.environ, {'LOCALAPPDATA': tmp}), \
                 patch.object(self_update, 'repo_root', return_value=root), \
                 patch.object(self_update, 'current_sha', return_value='b' * 40):
                self.assertFalse(self_update.step_once(request, root=root, run=run, log=lambda *a, **k: None))
        self.assertEqual(reports[-1]['autostart_state'], 'configured')
        self.assertIn('autostart_error', reports[-1])
        self.assertIsNone(reports[-1]['autostart_error'])
