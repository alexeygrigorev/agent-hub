import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import auth, fleet, native_sessions as ns


class RuntimePathTests(unittest.TestCase):
    def test_windows_venv_sibling_bin_is_prepended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bin').mkdir()
            (root / 'bin/aplexer.exe').write_bytes(b'')
            (root / 'venv/Scripts').mkdir(parents=True)
            python = root / 'venv/Scripts/python.exe'
            python.write_bytes(b'')
            with patch.object(ns.sys, 'platform', 'win32'), patch.object(ns.sys, 'executable', str(python)), \
                    patch.dict(os.environ, {'PATH': 'C:\\Windows'}):
                ns.ensure_runtime_bin_on_path()
                self.assertEqual(os.environ['PATH'].split(os.pathsep)[0], str((root / 'bin').resolve()))
                ns.ensure_runtime_bin_on_path()
                self.assertEqual(os.environ['PATH'].count('aplexer'), 0)
                self.assertEqual(os.environ['PATH'].split(os.pathsep).count(str((root / 'bin').resolve())), 1)

    def test_session_start_uses_the_hub_release_binary_over_a_stale_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary_dir = root / 'bin'
            binary_dir.mkdir()
            binary = binary_dir / 'aplexer.exe'
            binary.write_bytes(b'hub release')
            python = root / 'venv/Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'python')
            with patch.object(ns.sys, 'platform', 'win32'), \
                    patch.object(ns.sys, 'executable', str(python)):
                self.assertEqual(ns.session_aplexer_executable('old-aplexer-0.1.10.exe'), str(binary))

    def test_managed_windows_runtime_refuses_to_fall_back_when_binary_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / 'venv/Scripts/python.exe'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'python')
            with patch.object(ns.sys, 'platform', 'win32'), \
                    patch.object(ns.sys, 'executable', str(python)):
                with self.assertRaisesRegex(ns.SessionControlError, 'Hub-managed aplexer executable is missing'):
                    ns.session_aplexer_executable('old-aplexer-0.1.10.exe')

    def test_windows_fleet_start_has_explicit_120_second_startup_timeout(self):
        with patch.object(fleet, 'is_windows_host', return_value=True):
            argv = fleet.build_native_start_argv(
                'C:/agent-hub/bin/aplexer.exe', 'C:/workspace', 'hub-offer', 'codex'
            )
        self.assertEqual(
            argv[argv.index('--startup-timeout-ms') + 1],
            '120000',
        )
        self.assertEqual(argv[0], 'C:/agent-hub/bin/aplexer.exe')
        self.assertEqual(argv[1], 'start')

    def test_non_windows_fleet_start_keeps_platform_default(self):
        with patch.object(fleet, 'is_windows_host', return_value=False):
            argv = fleet.build_native_start_argv('/hub/bin/aplexer', '/workspace', 'hub-offer', 'codex')
        self.assertNotIn('--startup-timeout-ms', argv)

    def test_noop_off_windows(self):
        with patch.dict(os.environ, {'PATH': 'x'}):
            if sys.platform != 'win32':
                ns.ensure_runtime_bin_on_path()
                self.assertEqual(os.environ['PATH'], 'x')


class DevicePromptTests(unittest.TestCase):
    def test_code_goes_to_stdout_stderr_and_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HOME': tmp, 'LOCALAPPDATA': tmp}), \
                patch('sys.stderr') as err, patch('builtins.print') as out:
            auth._publish_device_prompt('Confirm this code: ABCD-1234\n')
            out.assert_called()
            self.assertIn('ABCD-1234', ''.join(c.args[0] for c in err.write.call_args_list))
            found = list(Path(tmp).rglob('device-code.txt'))
            self.assertEqual(len(found), 1)
            self.assertIn('ABCD-1234', found[0].read_text())


if __name__ == '__main__':
    unittest.main()
