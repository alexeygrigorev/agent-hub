"""`a task run --engine opencode --profile hub-<go>` must reach execute() unchanged (intake 5814e109)."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_hub_host import provisioning as p


class GoRunLauncher(unittest.TestCase):
    def test_engine_subcommand_before_the_label_is_moved_behind_it(self):
        self.assertEqual(p.normalize_engine_argv(['-c', 'run', 'shared-go', '--auto', 'goal']),
                         ['-c', 'shared-go', 'run', '--auto', 'goal'])
        # Interactive launches (no injected subcommand) are untouched.
        self.assertEqual(p.normalize_engine_argv(['-c', 'shared-go', '--auto']), ['-c', 'shared-go', '--auto'])
        self.assertEqual(p.normalize_engine_argv(['-c', 'shared-go', 'run', 'x']), ['-c', 'shared-go', 'run', 'x'])

    @unittest.skipIf(os.name == 'nt', 'POSIX sh launcher')
    def test_launcher_script_accepts_the_injected_subcommand_as_first_argument(self):
        tmp = Path(tempfile.mkdtemp())
        os.chmod(tmp, 0o700)
        with mock.patch.object(p, 'root', lambda: tmp):
            command = [sys.executable, '-c', 'import sys; print("|".join(sys.argv[1:]))', 'shared-go']
            launcher = p._posix_engine_launcher(command, 'shared-go')
            self.assertEqual(launcher[1], 'shared-go')
            self.assertTrue(os.access(launcher[0], os.X_OK))
            # aplexer builds [command[0], 'run', *command[1:], '--auto', prompt]
            out = subprocess.run([launcher[0], 'run', launcher[1], '--auto', 'reply OK'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), 'run|shared-go|--auto|reply OK')


class WindowsLauncherAndStalePin(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'private_fs needs a hub-created private directory on Windows')
    def test_windows_launcher_is_a_native_exe_stub_with_shebang_and_zipped_main(self):
        # aplexer refuses a .cmd as task child for a multi-line prompt, so Windows gets an exe stub.
        tmp = Path(tempfile.mkdtemp())
        os.chmod(tmp, 0o700)
        stub = tmp / 'stub.exe'
        stub.write_bytes(b'MZ-stub')
        python = 'C:/py/python.exe'
        command = [python, '-c', 'import sys; print(sys.argv)', 'shared-go']
        with mock.patch.object(p, 'root', lambda: tmp):
            launcher = p._windows_exe_launcher(command, 'shared-go', stub=stub)
        self.assertEqual(launcher[1], 'shared-go')
        self.assertTrue(launcher[0].endswith('shared-go.exe'))
        data = Path(launcher[0]).read_bytes()
        self.assertTrue(data.startswith(b'MZ-stub#!"' + python.encode() + b'"' + bytes((13, 10))))
        with zipfile.ZipFile(io.BytesIO(data[data.index(b'PK'):])) as archive:
            self.assertEqual(archive.read('__main__.py'), b'import sys; print(sys.argv)')

    def test_stale_release_pin_is_dropped_but_operator_pins_stay(self):
        stale = ("version = 1\n[engines.opencode]\n"
                 "command = ['C:/Users/a/AppData/Local/Programs/agent-hub/releases/56715fc/engines/node_modules/opencode-ai/bin/opencode.exe']\n"
                 "[engines.codex]\ncommand = ['x']\n")
        out = p._drop_stale_release_opencode_pin(stale)
        self.assertNotIn('engines.opencode', out)
        self.assertIn('[engines.codex]', out)
        self.assertIn('version = 1', out)
        bs = chr(92) * 2
        escaped = ('version = 1\n[engines.opencode]\ncommand = ["C:' + bs + 'Users' + bs + 'a' + bs + 'AppData' + bs + 'Local' + bs
                   + 'Programs' + bs + 'agent-hub' + bs + 'releases' + bs + 'x' + bs + 'opencode.exe"]\n')
        self.assertNotIn('engines.opencode', p._drop_stale_release_opencode_pin(escaped))
        operator = 'version = 1\n[engines.opencode]\ncommand = ["C:/ops/opencode.exe"]\n'
        self.assertEqual(p._drop_stale_release_opencode_pin(operator), operator)
        self.assertEqual(p._drop_stale_release_opencode_pin('version = 1\n'), 'version = 1\n')


if __name__ == '__main__':
    unittest.main()
