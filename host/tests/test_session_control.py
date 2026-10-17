"""Synthetic public CLI/HTTP coverage for hub sessions; never executes prompts."""
import argparse
import io
import json
import unittest
from unittest.mock import patch
from agent_hub_host import session_control


def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root


class TestSessionControlCli(unittest.TestCase):
    def parse(self, *args):
        return parser().parse_args(['sessions', *args])

    def test_stop_wait_does_not_accept_running(self):
        with patch.object(session_control, 'request', side_effect=[{'state': 'running'}, {'state': 'stopped'}]) as fetch, patch.object(session_control.time, 'sleep'):
            result = session_control.wait_for_session('s', 10, 1, stopping=True)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(fetch.call_count, 2)

    def test_list_show_commands_paths(self):
        with patch.object(session_control, 'request', return_value={'sessions': []}) as transport:
            result = session_control.execute(self.parse('list', '--limit', '10'))
        self.assertEqual(result, {'sessions': []})
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions?limit=10'))
        with patch.object(session_control, 'request', return_value={'id': 's'}) as transport:
            session_control.execute(self.parse('show', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s'))
        with patch.object(session_control, 'request', return_value={'commands': []}) as transport:
            session_control.execute(self.parse('commands', 's'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s/commands'))

    def test_start_headless_brief_and_interactive_empty(self):
        with patch.object(session_control, 'read_file', return_value='synthetic goal'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--engine', 'codex', '--brief-file', 'goal.txt', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'synthetic goal')
        self.assertEqual(payload['mode'], 'headless')
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--interactive', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['mode'], 'interactive')
        self.assertNotIn('brief', transport.call_args.args[2])
        conflicting = self.parse('start', '--workspace', 'demo', '--interactive', '--key', 'k')
        conflicting.brief_file = 'goal.txt'
        with patch.object(session_control, 'read_file', return_value='x'):
            with self.assertRaises(ValueError):
                session_control.execute(conflicting)

    def test_send_capture_stop_payloads(self):
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('send', 'sid', '--body-file', 'p.txt', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/send'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'body': 'prompt'})
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('capture', 'sid', '--max-bytes', '1024', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2], {'key': 'k', 'max_bytes': 1024})
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('stop', 'sid', '--key', 'k'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sid/stop'))

    def test_request_errors_hide_server_bodies(self):
        import os
        import urllib.error
        failure = urllib.error.HTTPError('https://hub.test/api/sessions', 500, 'boom', {}, io.BytesIO(b'secret'))
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                session_control.request('GET', '/api/sessions')
        self.assertNotIn('secret', str(raised.exception))
        self.assertNotIn('boom', str(raised.exception))

    def test_inputs_are_bounded(self):
        with self.assertRaises(ValueError):
            session_control.read_file(__file__, limit=10)

    def test_wait_flag_validation(self):
        args = self.parse('start', '--workspace', 'demo', '--wait', '--timeout', '10', '--interval', '2')
        self.assertEqual(session_control.check_wait_args(args), (10.0, 2.0))
        bad = self.parse('start', '--workspace', 'demo', '--wait', '--timeout', '0')
        with self.assertRaises(ValueError):
            session_control.check_wait_args(bad)
        bad_interval = self.parse('send', 'sid', '--body-file', 'p.txt', '--wait', '--interval', '0.1')
        with self.assertRaises(ValueError):
            session_control.check_wait_args(bad_interval)

    def test_start_wait_polls_binding_to_running(self):
        args = self.parse('start', '--workspace', 'demo', '--key', 'k1', '--wait', '--timeout', '30', '--interval', '1')
        views = [{'id': 's1', 'state': 'offered'}, {'id': 's1', 'state': 'running'}]
        with patch.object(session_control, 'request', side_effect=[{'id': 's1', 'state': 'offered'}, *views]) as transport, \
                patch.object(session_control.time, 'sleep', return_value=None):
            result = session_control.execute(args)
        self.assertEqual(result['client_key'], 'k1')
        self.assertEqual(result['binding_wait']['state'], 'running')
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/s1'))

    def test_send_wait_ignores_transport_until_correlated_ack(self):
        args = self.parse('send', 's1', '--body-file', 'p.txt', '--key', 'k2', '--wait', '--timeout', '30', '--interval', '1')
        listing = lambda state: {'commands': [{'id': 'c1', 'state': state}]}
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', side_effect=[
                    {'command_id': 'c1', 'state': 'recorded'},
                    listing('recorded'), listing('transported'), listing('acked'),
                ]), \
                patch.object(session_control.time, 'sleep', return_value=None):
            result = session_control.execute(args)
        # Transported alone never completes the wait; only the correlated ack does.
        self.assertEqual(result['command_wait']['state'], 'acked')

    def test_wait_timeout_names_key_for_reconciliation(self):
        args = self.parse('stop', 's1', '--key', 'k3', '--wait', '--timeout', '2', '--interval', '1')
        with patch.object(session_control, 'request', side_effect=[
                    {'command_id': 'c9', 'state': 'recorded'},
                    {'id': 's1', 'state': 'offered'},
                    {'id': 's1', 'state': 'offered'},
                    {'id': 's1', 'state': 'offered'},
                ]), \
                patch.object(session_control.time, 'sleep', return_value=None), \
                patch.object(session_control.time, 'monotonic', side_effect=[0, 0, 0, 100]):
            with self.assertRaises(ValueError) as raised:
                session_control.execute(args)
        self.assertIn('timed out', str(raised.exception))

    def test_lost_response_keeps_key_for_safe_retry(self):
        args = self.parse('send', 's1', '--body-file', 'p.txt', '--key', 'k4')
        with patch.object(session_control, 'read_file', return_value='prompt'), \
                patch.object(session_control, 'request', side_effect=ValueError('Session HTTP request failed with status 502')):
            with self.assertRaises(ValueError) as raised:
                session_control.execute(args)
        self.assertIn('k4', str(raised.exception))
        self.assertNotIn('prompt', str(raised.exception))

    def test_list_state_filters_without_echo(self):
        args = self.parse('list', '--state', 'running')
        payload = {'sessions': [{'id': 'a', 'state': 'running'}, {'id': 'b', 'state': 'offered'}], 'next_cursor': None}
        with patch.object(session_control, 'request', return_value=payload):
            result = session_control.execute(args)
        self.assertEqual([s['id'] for s in result['sessions']], ['a'])

    def test_start_prompt_inline_with_brief_boundaries(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'do the thing', '--key', 'k'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['brief'], 'do the thing')
        self.assertEqual(payload['mode'], 'headless')
        # Mutually exclusive with --brief-file and --interactive.
        with self.assertRaises(SystemExit):
            self.parse('start', '--workspace', 'demo', '--prompt', 'x', '--brief-file', 'goal.txt')
        with self.assertRaises(SystemExit):
            self.parse('start', '--workspace', 'demo', '--prompt', 'x', '--interactive')
        # Same 8 KiB UTF-8 boundaries as --brief-file, never executed locally.
        with patch.object(session_control, 'request', return_value={}):
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', '   ', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'é' * 5000, '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'x' * 8001, '--key', 'k'))

    def test_start_name_stored_and_validated(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--prompt', 'goal', '--name', 'friendly', '--key', 'k'))
        self.assertEqual(transport.call_args.args[2]['name'], 'friendly')
        with patch.object(session_control, 'request', return_value={}):
            session_control.execute(self.parse('start', '--workspace', 'demo', '--interactive', '--name', 'empty-ok', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--name', '../escape', '--key', 'k'))
            with self.assertRaises(ValueError):
                session_control.execute(self.parse('start', '--workspace', 'demo', '--name', '', '--key', 'k'))

    def test_host_receive_requires_once_and_machine(self):
        args = self.parse('host-receive', '--once', '--machine-id', 'hetzner1')
        self.assertTrue(args.once)
        self.assertEqual(args.machine_id, 'hetzner1')
        self.assertEqual(args.limit, 20)
        with patch('agent_hub_host.session_receiver.receive_once', return_value={'offers_seen': 0}) as recv:
            result = session_control.execute(args)
        self.assertEqual(result, {'offers_seen': 0})
        self.assertTrue(recv.called)
        # Bounded limit enforced.
    def test_qualified_alias_routing_for_show_send_capture_stop(self):
        with patch.object(session_control, 'request', return_value={'id': 'native-1', 'qualified_alias': 'dataops:batching'}) as transport:
            session_control.execute(self.parse('show', 'dataops:batching'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/dataops:batching'))

        with patch.object(session_control, 'request', return_value={'id': 'native-2', 'qualified_alias': 'git:crm-redesign'}) as transport:
            session_control.execute(self.parse('show', 'git:crm-redesign'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/git:crm-redesign'))

        with patch.object(session_control, 'read_file', return_value='payload'), \
                patch.object(session_control, 'request', return_value={'command_id': 'c1', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('send', 'dataops:batching', '--body-file', 'p.txt', '--key', 'k1'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/dataops:batching/send'))

        with patch.object(session_control, 'request', return_value={'command_id': 'c2', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('capture', 'git:crm-redesign', '--key', 'k2'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/git:crm-redesign/capture'))

        with patch.object(session_control, 'request', return_value={'command_id': 'c3', 'state': 'recorded'}) as transport:
            session_control.execute(self.parse('stop', 'dataops:batching', '--key', 'k3'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/dataops:batching/stop'))

        with patch.object(session_control, 'request', return_value={'commands': []}) as transport:
            session_control.execute(self.parse('commands', 'dataops:batching'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/dataops:batching/commands'))

        # Friendly name check strictly excludes colon (separate qualified alias resolver preserving friendly name validation)
        with self.assertRaises(ValueError):
            session_control.check_session_name('dataops:batching')

    def test_start_lineage_parent_and_headless_flags(self):
        with patch.object(session_control, 'request', return_value={'id': 's-child'}) as transport:
            session_control.execute(self.parse('start', '--workspace', 'demo', '--parent', 'parent-123', '--headless', '--key', 'k-lineage'))
        payload = transport.call_args.args[2]
        self.assertEqual(payload['parent_id'], 'parent-123')
        self.assertEqual(payload['mode'], 'headless')

    def test_sessions_tree_routing(self):
        with patch.object(session_control, 'request', return_value={'tree': []}) as transport:
            session_control.execute(self.parse('tree'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/tree'))

        with patch.object(session_control, 'request', return_value={'tree': []}) as transport:
            session_control.execute(self.parse('list', '--tree'))
        self.assertEqual(transport.call_args.args[:2], ('GET', '/api/sessions/tree'))

    def test_render_table_and_tree_formatters(self):
        sample_sessions = [
            {
                'id': 'uuid-1',
                'session_type': 'aplexer',
                'qualified_alias': 'dataops:batching',
                'engine': 'gemini',
                'state': 'running',
                'workspace': '/home/alexey/git/dataops',
                'updated_at': '2026-10-17T19:40:00.000Z'
            },
            {
                'id': 'uuid-2',
                'session_type': 'headless',
                'qualified_alias': None,
                'name': 'worker-1',
                'engine': 'codex',
                'state': 'running',
                'workspace': '/home/alexey/git/agent-hub',
                'updated_at': '2026-10-17T19:42:00.000Z'
            }
        ]
        table = session_control.render_table(sample_sessions)
        self.assertIn('TYPE', table)
        self.assertIn('NAME', table)
        self.assertIn('dataops:batching', table)
        self.assertIn('worker-1', table)
        self.assertIn('aplexer', table)
        self.assertIn('headless', table)

        sample_tree = {
            'tree': [
                {
                    'id': 'uuid-1',
                    'session_type': 'aplexer',
                    'qualified_alias': 'dataops:batching',
                    'state': 'running',
                    'workspace': '/home/alexey/git/dataops',
                    'children': [
                        {
                            'id': 'uuid-2',
                            'session_type': 'headless',
                            'name': 'worker-1',
                            'state': 'running',
                            'workspace': '/home/alexey/git/agent-hub',
                            'children': []
                        }
                    ]
                }
            ]
        }
        tree = session_control.render_tree(sample_tree)
        self.assertIn('Sessions Lineage Tree:', tree)
        self.assertIn('[aplexer] dataops:batching (running)', tree)
        self.assertIn('[headless] worker-1 (running)', tree)
        self.assertIn('└── ', tree)

        # render_output with --json flag returns raw data
        args_json = self.parse('list', '--json')
        out_json = session_control.render_output(args_json, {'sessions': sample_sessions})
        self.assertIsInstance(out_json, dict)

        # render_output without --json returns formatted string
        args_plain = self.parse('list')
        out_plain = session_control.render_output(args_plain, {'sessions': sample_sessions})
        self.assertIsInstance(out_plain, str)
        self.assertIn('dataops:batching', out_plain)


if __name__ == '__main__':
    unittest.main()

