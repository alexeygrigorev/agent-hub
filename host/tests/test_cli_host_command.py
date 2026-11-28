"""`hub host ...` is the only host-daemon entry point; there is no hub2 CLI."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import cli, host_command


class HostCommandTests(unittest.TestCase):
    def release(self, root, *, windows=False):
        directory = Path(root)
        python = directory / ('Scripts/python.exe' if windows else 'bin/python')
        python.parent.mkdir(parents=True)
        python.write_text('')
        return directory, python

    def test_resolves_release_next_to_zipapp_then_current_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            release, python = self.release(Path(temporary) / 'rel')
            archive = release / 'hub.pyz'
            archive.write_text('')
            self.assertEqual(host_command.daemon_python(str(archive), {}), python)
            self.assertEqual(host_command.daemon_python('', {'HUB_HOST_RELEASE': str(release)}), python)
            home = Path(temporary) / 'home'
            current, expected = self.release(home / '.local/share/agent-hub2/current')
            with patch.object(Path, 'home', return_value=home):
                self.assertEqual(host_command.daemon_python('', {}), expected)

    def test_missing_release_is_a_clear_failure(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(Path, 'home', return_value=Path(temporary)):
            with patch('sys.stderr') as stderr:
                self.assertEqual(host_command.main(['serve']), 1)
            self.assertTrue(stderr.write.called)

    def test_main_execs_isolated_daemon_module_with_all_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, python = self.release(temporary)
            with patch.object(host_command, 'daemon_python', return_value=python), \
                 patch.object(host_command.os, 'name', 'posix'), \
                 patch.object(host_command.os, 'execv', side_effect=SystemExit(0)) as execv:
                with self.assertRaises(SystemExit):
                    host_command.main(['--config', '/c.json', 'serve', '--aplexer', 'a'])
            execv.assert_called_once_with(str(python), [str(python), '-I', '-m', 'agent_hub_host.cli',
                                                        '--config', '/c.json', 'serve', '--aplexer', 'a'])

    def test_hub_dispatches_host_before_building_the_operator_parser(self):
        with patch.object(host_command, 'main', return_value=7) as dispatched, \
             patch('sys.argv', ['hub', 'host', 'heartbeat', '--aplexer', 'a']):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(raised.exception.code, 7)
        dispatched.assert_called_once_with(['heartbeat', '--aplexer', 'a'])

    def test_host_is_listed_in_operator_help(self):
        with patch('sys.argv', ['hub', '--help']), patch('sys.stdout') as out:
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertIn('host', ''.join(call.args[0] for call in out.write.call_args_list))


if __name__ == '__main__':
    unittest.main()
