import argparse
import os
import unittest
from unittest.mock import patch
from agent_hub_host import session_control as sc


def parse(*args):
    parser = argparse.ArgumentParser()
    sc.add_commands(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(['sessions', *args])


class LongRunningTests(unittest.TestCase):
    def test_threshold_and_snooze(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sc, 'request', return_value={}) as request:
            sc.execute(parse('ack', 'session'))
            request.assert_called_with('POST', '/api/sessions/session/ack', {})
            sc.execute(parse('ack', 'session', '--snooze', '3h', '--threshold', '2h'))
            request.assert_called_with('POST', '/api/sessions/session/ack?threshold=7200', {'snooze': 10800})
            sc.execute(parse('show', 'session', '--threshold', '1h'))
            request.assert_called_with('GET', '/api/sessions/session?threshold=3600')
        with patch.dict(os.environ, {'HUB_SESSION_WARNING_THRESHOLD': '4h'}), patch.object(sc, 'request', return_value={}) as request:
            sc.execute(parse('list'))
            self.assertIn('threshold=14400', request.call_args.args[1])
        for bad in ('0h', '-1h', 'nan', '1.5h', '366d', '3h;kill'):
            with self.assertRaises(ValueError):
                sc.duration_seconds(bad)

    def test_warnings_tail_and_exact_snooze_no_stop(self):
        warning = {'id': 'session', 'qualified_name': 'tag', 'engine': 'codex', 'intake_id': 'intake',
                   'runtime': 14400, 'idle_for': 4000, 'last_activity_at': '2026-10-24T12:00:00Z',
                   'activity_state': 'idle', 'snooze_command': 'hub sessions ack session --snooze 10800s'}
        with patch.object(sc, 'request', side_effect=[{'warnings': [warning]}, {'command_id': 'capture-id'}]) as request, \
             patch.object(sc, 'wait_for_command', return_value={'state': 'acked', 'ack': {'capture': '0\n1\n2\n3\n4\n5'}}):
            result = sc.execute(parse('warnings'))
        self.assertEqual(result['warnings'][0]['output_tail'], ['1', '2', '3', '4', '5'])
        self.assertEqual([c.args[1] for c in request.call_args_list], ['/api/sessions/warnings', '/api/sessions/session/capture'])
        text = sc.render_output(parse('warnings'), result)
        for value in ('session', 'tag', 'codex', 'intake', '4h00m00s', 'long and idle', warning['snooze_command']):
            self.assertIn(value, text)
        self.assertIs(sc.render_output(parse('warnings', '--json'), result), result)

    def test_capture_failure_keeps_warning_visible_and_table_shows_flag(self):
        with patch.object(sc, 'request', side_effect=[{'warnings': [{'id': 's'}]}, sc.SessionTransportError('offline')]):
            result = sc.execute(parse('warnings'))
        self.assertEqual(result['warnings'][0]['output_tail'], [])
        self.assertIn('unavailable', result['warnings'][0]['output_status'])
        table = sc.render_table([{'id': 's', 'long_running': True, 'runtime': 14400, 'idle_for': 20, 'activity_state': 'active'}])
        for value in ('LONG_RUNNING', 'true', 'RUNTIME', '4h00m00s', 'IDLE_FOR', 'active'):
            self.assertIn(value, table)
