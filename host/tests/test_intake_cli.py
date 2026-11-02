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

    def test_show_points_to_archived_history_with_count_and_read_command(self):
        output = io.StringIO()
        result = {'task': {'id': 'fixture-task', 'history_archive': {'count': 23, 'latest_sequence': 99,
                  'href': '/api/intake/tasks/fixture-task/history?archived=1'}}}
        with patch('sys.argv', ['hub', 'intake', 'show', 'fixture-task']), patch('sys.stdout', output), patch.object(intake, 'request', return_value=result):
            cli.main()
        shown = json.loads(output.getvalue())['task']['history_archive']
        self.assertEqual(shown['count'], 23)
        self.assertEqual(shown['read_command'], 'hub intake history fixture-task --archived --after 98 --limit 1')

    def test_append_prints_server_archive_location(self):
        output = io.StringIO()
        response = {'appended_event': {'archived': True, 'sequence': 99}}
        with patch('sys.argv', ['hub', 'intake', 'append', 'fixture-task', '--version', '4', '--key', 'retry-1', '--event-file', 'event.json']), \
             patch('sys.stdout', output), patch.object(intake, 'json_file', return_value={'kind': 'note', 'text': 'fixture'}), \
             patch.object(intake, 'request', return_value=response):
            cli.main()
        self.assertEqual(json.loads(output.getvalue())['appended_event'], {'archived': True, 'sequence': 99})

    def test_failed_append_persist_error_exits_nonzero(self):
        output = io.StringIO()
        errors = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'append', 'fixture-task', '--version', '4', '--key', 'retry-1', '--event-file', '-']), \
             patch.object(intake, 'json_file', return_value={'kind': 'note', 'text': 'fixture'}), \
             patch('sys.stdout', output), patch('sys.stderr', errors), \
             patch.object(intake, 'request', side_effect=intake.IntakeHTTPError(500, 'internal_error')):
            with self.assertRaises(SystemExit) as raised:
                cli.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('Intake HTTP 500: internal_error', errors.getvalue())

    def test_invalid_archive_page_never_reaches_transport(self):
        for option, value in [('--limit', '0'), ('--limit', '101'), ('--after', '-1')]:
            with self.subTest(option=option, value=value), patch('sys.argv', ['hub', 'intake', 'history', 'fixture-task', '--archived', option, value]), patch('sys.stderr', io.StringIO()), patch.object(intake, 'request') as request:
                with self.assertRaises(SystemExit):
                    cli.main()
                request.assert_not_called()
