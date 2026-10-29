import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_hub_host import state_hook_commands as hooks


class StateHookCommandsTests(unittest.TestCase):
    def test_posix_shell_preserves_profile_path_and_arguments_without_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "user's $HOME `echo unsafe` path"
            executable.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o700)
            arguments = ['state-report', 'working', '$(echo unsafe)', '; exit 7', "a'b"]
            with patch.object(hooks.sys, 'platform', 'linux'):
                command = hooks.hook_command(executable, *arguments)
            result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), arguments)

    def test_failed_posix_hook_does_not_block_engine_and_quiet_suppresses_stderr(self):
        with patch.object(hooks.sys, 'platform', 'darwin'):
            command = hooks.hook_command('/missing user path/aplexer', 'state-report', 'idle', quiet=True)
        result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')

    def test_windows_outer_command_is_shell_neutral_and_keeps_path_literal(self):
        path = "C:\\Users\\O'Brien $HOME `user` & other\\aplexer.exe"
        with patch.object(hooks.sys, 'platform', 'win32'):
            command = hooks.hook_command(path, 'state-report', 'idle')
        words = shlex.split(command)
        self.assertEqual(words[:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertEqual(len(words), 5)
        self.assertNotIn(path, command)
        script = base64.b64decode(words[4], validate=True).decode('utf-16-le')
        self.assertIn("& 'C:\\Users\\O''Brien $HOME `user` & other\\aplexer.exe' 'state-report' 'idle'", script)
        self.assertTrue(script.endswith('catch {}; exit 0'))
        self.assertNotIn('RunAs', command + script)

    def test_windows_awareness_hook_preserves_stdout_and_quiets_only_stderr(self):
        with patch.object(hooks.sys, 'platform', 'win32'):
            command = hooks.hook_command('aplexer.exe', 'context', 'hook', '--engine', 'codex', quiet=True)
        script = base64.b64decode(command.split()[-1]).decode('utf-16-le')
        self.assertIn("'context' 'hook' '--engine' 'codex' 2>$null", script)
        self.assertNotIn('Out-Null', script)

    def test_control_characters_are_refused(self):
        for platform in ['linux', 'win32']:
            with patch.object(hooks.sys, 'platform', platform):
                for invalid in ['line\nbreak', 'line\rbreak', 'zero\0byte']:
                    with self.assertRaises(ValueError):
                        hooks.hook_command('aplexer', invalid)


if __name__ == '__main__':
    unittest.main()
