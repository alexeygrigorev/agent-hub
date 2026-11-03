"""Archive paging uses the same authorized intake transport and bounded inputs."""
import io
import json
import statistics
import sys
import time
import unittest
from unittest.mock import patch
from agent_hub_host import __main__ as entrypoint
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

    def test_summary_local_dispatch_overhead_under_10ms(self):
        """Measure local parse/dispatch/JSON work; HTTP and process startup are excluded."""
        payload = {
            'counts': {'total': 1000, 'open': 120, 'active': 45, 'done': 835},
            'recent': [{'id': f'task-{i}', 'status': 'active', 'updated_at': i,
                        'title': f'task {i}', 'current_state': 'working'} for i in range(5)],
            'as_of': 1234567890,
        }
        elapsed = []
        output = io.StringIO()
        with patch.object(sys, 'argv', ['hub', 'intake', 'summary']), \
                patch('sys.stdout', output), \
                patch.object(intake, 'request', return_value=payload) as request:
            for _ in range(50):
                output.seek(0)
                output.truncate(0)
                started = time.perf_counter()
                entrypoint.main()
                elapsed.append(time.perf_counter() - started)
        self.assertEqual(request.call_args.args, ('GET', '/api/intake/summary?'))
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertLess(statistics.median(elapsed), 0.010)

    def test_https_get_json_decompresses_gzip_and_reuses_keepalive_pool(self):
        import gzip
        from unittest.mock import MagicMock
        compressed = gzip.compress(b'{"items": [{"id": "task-1"}], "next": null, "total": 1}')
        fake_res = MagicMock()
        fake_res.status = 200
        fake_res.headers = {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}
        fake_res.read.return_value = compressed
        fake_res.will_close = False
        fake_res.isclosed.return_value = True
        fake_conn = MagicMock()
        fake_conn.sock = object()
        fake_conn.getresponse.return_value = fake_res
        intake._https_conn_pool.clear()
        intake._https_conn_pool[('localhost', 8443)] = fake_conn
        try:
            data = intake._https_get_json('https://localhost:8443', '/api/intake/tasks?limit=100', 'token-1')
            self.assertEqual(data, {'items': [{'id': 'task-1'}], 'next': None, 'total': 1})
            self.assertIs(intake._https_conn_pool.get(('localhost', 8443)), fake_conn)
        finally:
            intake._https_conn_pool.clear()


class TestQueuePriorityCli(unittest.TestCase):
    def test_promote_posts_cas_payload(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'promote', 'fixture-task', '--version', '3', '--key', 'promo-key']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'task': {'id': 'fixture-task'}}) as request:
            cli.main()
        request.assert_called_once_with('POST', '/api/intake/tasks/fixture-task/promote', {'key': 'promo-key', 'expected_version': 3})
        self.assertEqual(json.loads(output.getvalue())['task']['id'], 'fixture-task')

    def test_promote_fetches_version_and_generates_key_when_omitted(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'promote', 'fixture-task']), patch('sys.stdout', output), patch.object(intake, 'request', side_effect=[{'task': {'control': {'version': 7}}}, {'task': {'id': 'fixture-task'}}]) as request:
            cli.main()
        self.assertEqual(request.call_args_list[0].args, ('GET', '/api/intake/tasks/fixture-task'))
        post = request.call_args_list[1].args
        self.assertEqual((post[0], post[1]), ('POST', '/api/intake/tasks/fixture-task/promote'))
        self.assertEqual(post[2]['expected_version'], 7)
        self.assertTrue(post[2]['key'].startswith('promote-'))

    def test_create_and_update_carry_priority(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'create', '--project', 'demo', '--title', 'T', '--text', 'x', '--priority', 'high', '--key', 'k']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={}) as request:
            cli.main()
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ('POST', '/api/intake/tasks'))
        self.assertEqual(body['priority'], 'high')
        with patch('sys.argv', ['hub', 'intake', 'update', 'fixture-task', '--version', '2', '--key', 'k2', '--priority', 'low']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={}) as request:
            cli.main()
        method, path, body = request.call_args.args
        self.assertEqual((method, path), ('POST', '/api/intake/tasks/fixture-task'))
        self.assertEqual(body['priority'], 'low')

    def test_queue_lists_the_auto_queue(self):
        output = io.StringIO()
        with patch('sys.argv', ['hub', 'intake', 'queue', '--project', 'demo', '--limit', '10']), patch('sys.stdout', output), patch.object(intake, 'request', return_value={'items': [{'id': 'q-1', 'queue_position': 1}]}) as request:
            cli.main()
        request.assert_called_once_with('GET', '/api/intake/queue?project=demo&limit=10')
        self.assertEqual(json.loads(output.getvalue())['items'][0]['id'], 'q-1')
