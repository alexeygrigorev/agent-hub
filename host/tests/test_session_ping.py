"""CLI coverage for hub sessions ping / hub loop; never executes prompts."""
import argparse
import os
import unittest
from unittest.mock import patch
from agent_hub_host import session_control


def parse(*args):
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest='cmd', required=True)
    session_control.add_commands(sub)
    return root.parse_args(list(args))


class TestSessionPingCli(unittest.TestCase):
    def test_create_posts_interval_seconds_and_prompt(self):
        with patch.object(session_control, 'request', return_value={'ping': {}}) as transport:
            session_control.execute(parse('sessions', 'ping', 'create', 'sess-1', '--every', '15m', '--prompt', 'status?'))
        self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/sess-1/ping'))
        self.assertEqual(transport.call_args.args[2], {'every_seconds': 900, 'prompt': 'status?'})

    def test_interval_bounds(self):
        with patch.object(session_control, 'request') as transport:
            for bad in ('30s', '8d', 'abc'):
                with self.assertRaises(Exception):
                    session_control.execute(parse('sessions', 'ping', 'create', 'sess-1', '--every', bad, '--prompt', 'x'))
            transport.assert_not_called()

    def test_disable_and_show_routes(self):
        with patch.object(session_control, 'request', return_value={}) as transport:
            session_control.execute(parse('sessions', 'ping', 'disable', 'sess-1', '--reason', 'done'))
            self.assertEqual(transport.call_args.args, ('POST', '/api/sessions/sess-1/ping-clear', {'reason': 'done'}))
            session_control.execute(parse('sessions', 'ping', 'show', 'sess-1'))
            self.assertEqual(transport.call_args.args, ('GET', '/api/sessions/sess-1/pings'))

    def test_loop_alias_targets_current_session(self):
        with patch.dict(os.environ, {'HUB_SESSION_ID': 'self-id-1'}), \
                patch.object(session_control, 'request', return_value={}) as transport:
            args = parse('loop', 'set', '--every', '1h', '--prompt', 'keep going')
            self.assertEqual(args.sessions_cmd, 'ping')
            session_control.execute(args)
            self.assertEqual(transport.call_args.args, ('POST', '/api/sessions/self-id-1/ping', {'every_seconds': 3600, 'prompt': 'keep going'}))
            session_control.execute(parse('loop', 'clear'))
            self.assertEqual(transport.call_args.args[:2], ('POST', '/api/sessions/self-id-1/ping-clear'))

    def test_invalid_session_ref_refused(self):
        with patch.object(session_control, 'request') as transport:
            with self.assertRaises(ValueError):
                session_control.execute(parse('sessions', 'ping', 'show', '../x'))
            transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
