"""Windows local login and native observation contracts (intake 41ef7048)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from agent_hub_host.agent import Bridge
from agent_hub_host.native_launch import NativeLauncher, native_state_directory, windows_codex_command


class WindowsLaunchObservationTests(unittest.TestCase):
    def test_windows_npm_codex_runs_node_without_shell_and_preserves_literal_arguments(self):
        with tempfile.TemporaryDirectory() as temp, patch('sys.platform', 'win32'):
            root = Path(temp)
            script = root / 'node_modules/@openai/codex/bin/codex.js'
            script.parent.mkdir(parents=True)
            script.write_text('// fixture', encoding='utf-8')
            shim, node = str(root / 'codex.cmd'), str(root / 'node.exe')
            args = ['codex', 'exec', 'literal & echo %PATH% | < > "value"']
            with patch('agent_hub_host.native_launch.shutil.which', side_effect=lambda name: shim if name == 'codex' else node):
                self.assertEqual(windows_codex_command(args), [node, str(script), *args[1:]])
                script.unlink()
                with self.assertRaisesRegex(RuntimeError, 'native_codex_program_unavailable'):
                    windows_codex_command(args)

    def test_windows_native_codex_headless_does_not_use_the_task_runner(self):
        with tempfile.TemporaryDirectory() as temp, patch('sys.platform', 'win32'):
            calls = []
            def runner(argv, **options):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps({
                    'id': '71ffde7c-f089-471a-93bd-9acf29c31c51', 'tag': 'task-fixture',
                    'workspace': temp, 'command': argv[argv.index('--') + 1:],
                }), '')
            launcher = NativeLauncher({}, runner=runner)
            launcher.profile_command = Mock(side_effect=AssertionError('task runner must not be used'))
            with patch('agent_hub_host.native_launch.windows_codex_command', side_effect=lambda argv: ['node.exe', 'codex.js', *argv[1:]]):
                result = launcher.launch('fixture', {'workspace': temp, 'engine': 'codex', 'model': 'gpt-6.1-sol',
                    'prompt': 'Verify', 'effort': 'medium', 'timeout_seconds': 600})
            self.assertEqual(result['session_id'], '71ffde7c-f089-471a-93bd-9acf29c31c51')
            child = calls[0][calls[0].index('--') + 1:]
            self.assertEqual(child[:3], ['node.exe', 'codex.js', 'exec'])
            self.assertIn('model_reasoning_effort="medium"', child)

    def test_running_registry_record_with_dead_worker_is_reported_missing(self):
        bridge = Bridge.__new__(Bridge)
        row = {'session_id': 'fixture', 'state': 'running', 'workspace': 'fixture'}
        bridge.journal = SimpleNamespace(db=SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: [row])))
        bridge._inflight = set()
        bridge._workspace_is_available = lambda _: True
        bridge.resolve_launch_native = lambda _: ('native', {'phase': 'running', 'worker_alive': False})
        self.assertEqual(bridge.live_sessions()[0]['phase'], 'missing')

    def test_local_windows_codex_uses_native_login_and_preserves_explicit_profiles(self):
        with patch('sys.platform', 'win32'):
            launcher = NativeLauncher({'routes': {'codex': {'provider': 'codex'}}})
            self.assertEqual(launcher.config['routes']['codex'], {'provider': 'codex', 'engine': 'codex'})
            explicit = NativeLauncher({'routes': {'codex': {'provider': 'codex', 'profile': 'operator-choice'}}})
            self.assertEqual(explicit.config['routes']['codex']['profile'], 'operator-choice')
            central = NativeLauncher({'private_provider_homes': True})
            self.assertEqual(central.config['routes']['codex'], {'provider': 'codex'})

    def test_both_native_readers_decode_utf8_without_losing_pty_output(self):
        # Includes UTF-8 0x9d, which crashes Windows' default cp1252 reader.
        script = "import os; os.write(1, bytes.fromhex('e2809dc3a9')); os.write(2, b'\\xff')"
        launcher = NativeLauncher({}, aplexer=sys.executable)
        bridge = Bridge.__new__(Bridge)
        bridge.executable, bridge.runner = sys.executable, subprocess.run
        for reader in (launcher.native, bridge.native):
            with self.subTest(reader=reader.__qualname__):
                receipt = reader('-c', script)
                self.assertEqual(receipt.stdout, '\u201dé')
                self.assertEqual(receipt.stderr, '\ufffd')

    def test_windows_retirement_uses_native_state_default_and_exit_identity(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'LOCALAPPDATA': temp}):
            with patch.dict(os.environ):
                os.environ.pop('APLEXER_STATE_DIR', None)
                with patch('sys.platform', 'win32'):
                    base = Path(temp) / 'aplexer' / 'state'
                    self.assertEqual(native_state_directory(), base.resolve())
                    native_id = '71ffde7c-f089-471a-93bd-9acf29c31c51'
                    workspace = Path(temp) / 'workspace'
                    workspace.mkdir()
                    directory = base / 'retired-sessions' / native_id
                    directory.mkdir(parents=True)
                    (directory / 'tombstone.json').write_text(json.dumps({
                        'id': native_id, 'tag': 'task-fixture', 'workspace': str(workspace),
                        'cause': 'finished', 'containment_empty': True,
                        'exit': {'code': 1, 'signal': None, 'exited_at_ms': 100},
                    }), encoding='utf-8')
                    launcher = NativeLauncher({})
                    row = launcher.retired(native_id, {'tag': 'task-fixture', 'workspace': str(workspace)})
                    self.assertEqual(row['phase'], 'exited')
                    self.assertEqual(row['exit']['code'], 1)
                    self.assertIsNone(launcher.retired(native_id, {'tag': 'other', 'workspace': str(workspace)}))
                    override = str(Path(temp) / 'override')
                    with patch.dict(os.environ, {'APLEXER_STATE_DIR': override}):
                        self.assertEqual(native_state_directory(), Path(override).resolve())
                        self.assertEqual(native_state_directory(str(base)), base.resolve())


if __name__ == '__main__':
    unittest.main()
