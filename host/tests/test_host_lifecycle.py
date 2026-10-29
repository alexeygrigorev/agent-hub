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
        with patch.object(self_update, 'repo_root', return_value=None), patch.object(self_update, 'release_sha', return_value='a' * 40):
            self.assertFalse(self_update.step_once(request, log=lambda *a, **k: None))
        self.assertEqual(reports, [{'current_sha': 'a' * 40, 'state': 'unsupported', 'error': 'immutable_release'}])

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
                self.assertEqual(self_update.ensure_windows_autostart(root, run), 'configured')
                self.assertEqual(self_update.ensure_windows_autostart(root, run), 'configured')
            self.assertEqual(len(calls), 1)
            self.assertIn('-NonInteractive', calls[0])
            self.assertNotIn('RunAs', calls[0])
