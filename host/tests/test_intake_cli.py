"""Archive paging uses the same authorized intake transport and bounded inputs."""
import io
import json
import unittest
from unittest.mock import patch
from agent_hub_host import cli, intake

class TestArchiveCli(unittest.TestCase):
    def test_archived_history_cursor_and_limit(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'history', 'fixture-task', '--archived', '--after', '7', '--limit', '2']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'items': [], 'next_after': None}) as request:
            cli.main()
        request.assert_called_once_with('GET', '/api/intake/tasks/fixture-task/history?archived=1&limit=2&after=7')
        self.assertEqual(json.loads(output.getvalue())['items'], [])

    def test_invalid_archive_page_never_reaches_transport(self):
        for option, value in [('--limit', '0'), ('--limit', '101'), ('--after', '-1')]:
            with self.subTest(option=option, value=value), patch('sys.argv', ['hub', 'intake', 'history', 'fixture-task', '--archived', option, value]), patch('sys.stderr', io.StringIO()), patch.object(intake, 'request') as request:
                with self.assertRaises(SystemExit):
                    cli.main()
                request.assert_not_called()
