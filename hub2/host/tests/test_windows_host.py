"""Intake 41ef7048: the Windows Hub2 host refused launches silently.

serve.log stayed empty, the refusal reason was a bare `forbidden`, and the host
had no supported install/update path or reported release identity.
"""
import contextlib
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import windows_host
from agent_hub_host.agent import Bridge
from agent_hub_host.cli import main
from agent_hub_host.client import ApiError

ENROLLED = {'server': 'https://hub.example', 'token': 'x' * 40, 'machine_id': 'DESKTOP-FP4OP26',
            'roots': ['C:\\Users\\User\\work'], 'engines': ['codex', 'claude'],
            'launcher': {'private_provider_homes': True, 'aplexer': 'C:\\a.exe'}}


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def opener_for(files):
    def opener(url, timeout):
        name = url.rsplit('/', 1)[1]
        return Response(files[name], url)
    return opener


def release_files(installer=b'# installer\n', record=None):
    version = 'a' * 40
    manifest = {'host_version': version, 'artifacts': {
        'host-install.ps1': record or {'sha256': hashlib.sha256(installer).hexdigest(), 'size': len(installer)}}}
    return version, {'host-release.json': json.dumps(manifest).encode(), 'host-install.ps1': installer}


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.saved = (self.root.handlers[:], self.root.level)
        self.root.handlers = []

    def tearDown(self):
        for handler in self.root.handlers:
            handler.close()
        self.root.handlers, level = self.saved
        self.root.setLevel(level)

    def test_serve_log_file_receives_errors_and_stderr_too(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested' / 'serve.log'
            self.assertEqual(windows_host.configure_logging(path, stream=stream), path)
            windows_host.configure_logging(path, stream=stream)  # idempotent stderr handler
            logging.getLogger('agent_hub_host.agent').error('LAUNCH REFUSED session=s1 reason=host_not_approved')
            for handler in self.root.handlers:
                handler.flush()
            self.assertIn('LAUNCH REFUSED session=s1 reason=host_not_approved', path.read_text())
            for handler in [h for h in self.root.handlers if isinstance(h, logging.FileHandler)]:
                handler.close()  # Windows cannot delete an open log file
                self.root.removeHandler(handler)
        self.assertEqual(stream.getvalue().count('LAUNCH REFUSED'), 1)

    def test_default_log_file_only_on_windows(self):
        windows = SimpleNamespace(name='nt', environ={'LOCALAPPDATA': '/tmp/local'})
        with patch.object(windows_host, 'os', windows):
            self.assertEqual(windows_host.default_log_file(), Path('/tmp/local/agent-hub2/serve.log'))
        with patch.object(windows_host, 'os', SimpleNamespace(name='posix', environ={})):
            self.assertIsNone(windows_host.default_log_file())


class LaunchRefusalLogTests(unittest.TestCase):
    def test_refusal_names_session_stage_and_typed_reason(self):
        bridge = object.__new__(Bridge)
        bridge.offer = Mock(side_effect=ApiError('Hub HTTP 403', status=403, code='host_not_approved'))
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE launches(operation_id,session_id,epoch,state,native_dispatch_started)')
        bridge.journal = SimpleNamespace(db=db)
        socket = Mock()
        offer = {'session_id': '4a6cdeea', 'operation_id': 'op-1', 'epoch': 3}
        with self.assertLogs('agent_hub_host.agent', level='ERROR') as logs:
            bridge.dispatch({'type': 'session.offer', 'offer': offer}, socket, None)
        refusal = [line for line in logs.output if 'LAUNCH REFUSED' in line]
        self.assertEqual(len(refusal), 1)
        self.assertIn('session=4a6cdeea', refusal[0])
        self.assertIn('stage=host_launch reason=host_not_approved', refusal[0])
        frame = json.loads(socket.send.call_args.args[0])
        self.assertEqual((frame['reason'], frame['stage'], frame['epoch']), ('host_not_approved', 'host_launch', 3))


class CredentialModeTests(unittest.TestCase):
    def test_modes_round_trip_without_touching_other_launcher_settings(self):
        local = windows_host.set_credential_mode(ENROLLED, 'local')
        self.assertEqual(windows_host.credential_mode(local), 'local')
        self.assertEqual(local['launcher'], {'aplexer': 'C:\\a.exe'})
        central = windows_host.set_credential_mode(local, 'central')
        self.assertEqual(central['launcher'], {'aplexer': 'C:\\a.exe', 'private_provider_homes': True})
        bare = windows_host.set_credential_mode({**ENROLLED, 'launcher': {'private_provider_homes': True}}, 'local')
        self.assertNotIn('launcher', bare)
        with self.assertRaises(ValueError):
            windows_host.set_credential_mode({}, 'local')
        with self.assertRaises(ValueError):
            windows_host.set_credential_mode(ENROLLED, 'shared')

    def test_cli_credential_mode_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'v2.json'
            config.write_text(json.dumps(ENROLLED))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['--config', str(config), 'credential-mode']), 0)
                self.assertEqual(main(['--config', str(config), 'credential-mode', 'local']), 0)
                with patch.object(windows_host, 'host_release', return_value=('b' * 40, 1)):
                    self.assertEqual(main(['--config', str(config), 'version']), 0)
            lines = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(lines[0], {'credential_mode': 'central', 'restart_required': False})
            self.assertEqual(lines[1], {'credential_mode': 'local', 'restart_required': True})
            self.assertEqual(lines[2], {'host_version': 'b' * 40, 'protocol_version': 1,
                                        'machine_id': 'DESKTOP-FP4OP26', 'credential_mode': 'local'})
            saved = json.loads(config.read_text())
            self.assertEqual(saved['token'], ENROLLED['token'])
            self.assertNotIn('private_provider_homes', saved.get('launcher', {}))


class WindowsSelfUpdateTests(unittest.TestCase):
    def test_verified_installer_checks_manifest_digest_and_size(self):
        version, files = release_files()
        self.assertEqual(windows_host.verified_installer('https://hub.example', opener_for(files)),
                         (version, files['host-install.ps1']))
        tampered = dict(files, **{'host-install.ps1': b'# evil!!!!!\n'})
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            windows_host.verified_installer('https://hub.example', opener_for(tampered))
        _, old = release_files()
        old['host-release.json'] = json.dumps({'host_version': version, 'artifacts': {}}).encode()
        with self.assertRaisesRegex(ValueError, 'no Windows host installer'):
            windows_host.verified_installer('https://hub.example', opener_for(old))
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            windows_host.verified_installer('http://hub.example', opener_for(files))

    def test_self_update_starts_detached_installer_in_update_mode(self):
        version, files = release_files()
        spawn = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(windows_host, 'state_root', return_value=Path(directory)), \
                patch.object(windows_host, 'host_release', return_value=('c' * 40, 1)):
            out = io.StringIO()
            self.assertEqual(windows_host.windows_self_update('https://hub.example/', opener=opener_for(files),
                                                              spawn=spawn, out=out), 0)
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[:2], ['powershell.exe', '-NoProfile'])
            script = Path(argv[argv.index('-File') + 1])
            self.assertEqual(script.read_bytes(), files['host-install.ps1'])
            env = spawn.call_args.kwargs['env']
            self.assertEqual((env['HUB2_INSTALL_MODE'], env['HUB_BASE_URL'], env['HUB2_EXPECTED_HOST_VERSION']),
                             ('update', 'https://hub.example', version))
            self.assertEqual(json.loads(out.getvalue())['host_version'], version)

    def test_self_update_is_a_no_op_when_current(self):
        version, files = release_files()
        spawn = Mock()
        with patch.object(windows_host, 'host_release', return_value=(version, 1)):
            out = io.StringIO()
            self.assertEqual(windows_host.windows_self_update('https://hub.example', opener=opener_for(files),
                                                              spawn=spawn, out=out), 0)
        spawn.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())['reason'], 'already_current')


class InstallerScriptTests(unittest.TestCase):
    """Static guards for deploy/host-install.ps1 (parsed by PowerShell on the Windows CI runner)."""
    SCRIPT = Path(__file__).resolve().parents[3] / 'deploy/host-install.ps1'

    def test_installer_keeps_serve_running_and_never_targets_worker_processes(self):
        text = self.SCRIPT.read_text(encoding='utf-8')
        for needle in ("$TaskName = 'AgentHub2Host'", "$UpdateTaskName = 'AgentHub2HostUpdate'",
                       '-ExecutionTimeLimit ([TimeSpan]::Zero)', 'serve --aplexer',
                       '--log-file', 'self-update', "AgentHubController-*", "'retired-by-hub2'",
                       "Get-Verified 'host-install.ps1'", '/api/fleet/routing/self'):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r'(?im)^\s*param\s*\(', 'must run under irm | iex')
        self.assertNotRegex(text, r'(?i)Stop-Process[^\n]*(codex|claude)')
        self.assertNotRegex(text, r'(?i)Unregister-ScheduledTask|Remove-Item')
        self.assertIn("$tag -like 'hub-controller-*' -and $engine -eq 'shell'", text)


if __name__ == '__main__':
    unittest.main()
