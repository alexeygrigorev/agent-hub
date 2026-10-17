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
        bad = self.parse('host-receive', '--once', '--machine-id', 'hetzner1', '--limit', '99')
        with self.assertRaises(ValueError):
            session_control.execute(bad)


if __name__ == '__main__':
    unittest.main()
