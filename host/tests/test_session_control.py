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


if __name__ == '__main__':
    unittest.main()
