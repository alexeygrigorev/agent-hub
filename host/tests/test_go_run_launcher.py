"""`a task run --engine opencode --profile hub-<go>` must reach execute() unchanged (intake 5814e109)."""
import os
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == '__main__':
    unittest.main()
