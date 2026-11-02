import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import auth, native_sessions as ns


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
