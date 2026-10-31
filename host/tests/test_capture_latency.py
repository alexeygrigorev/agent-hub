"""Synthetic deadline, privacy and command-loop responsiveness regressions."""
import argparse
import contextlib
import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from agent_hub_host import session_control as sc, session_host


def capture_args(*extra):
    parser = argparse.ArgumentParser()
    sc.add_commands(parser.add_subparsers(dest='cmd'))
    return parser.parse_args(['sessions', 'capture', 'owned', '--key', 'stable', *extra])


class CaptureLatency(unittest.TestCase):
    def test_deadline_includes_enqueue_and_caps_sleep(self):
        clock = [0.0]
        def transport(method, path, payload=None):
            if method == 'POST':
                clock[0] += 2
                return {'command_id': 'command-fixture'}
            return {'commands': [{'id': 'command-fixture', 'state': 'recorded'}]}
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with patch.object(sc, 'request', side_effect=transport), \
             patch.object(sc.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(sc.time, 'sleep', side_effect=sleep), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaisesRegex(sc.SessionInputError, 'capture_not_acked; command command-fixture'):
                sc.execute(capture_args('--timeout', '3', '--interval', '600'))
        self.assertEqual(clock[0], 3)
        self.assertEqual(sleeps, [1])
        self.assertIn('command-fixture', stderr.getvalue())
        self.assertIsNone(sc._request_deadline.get())

    def test_failed_and_empty_captures_never_succeed_or_echo_payloads(self):
        cases = [({'state': 'refused', 'ack': {'reason': 'session_not_bound'}}, 'session_not_bound'),
                 ({'state': 'uncertain', 'ack': {'reason': 'secret https://private.invalid'}}, 'capture_failed'),
                 ({'state': 'acked', 'ack': {'capture': ''}}, 'capture_empty'),
                 ({'state': 'acked', 'ack': {}}, 'capture_empty')]
        for entry, reason in cases:
            with self.subTest(reason=reason), \
                 patch.object(sc, 'request', return_value={'command_id': 'command-fixture'}), \
                 patch.object(sc, 'wait_for_command', return_value=entry), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(sc.SessionInputError, reason+'; command command-fixture') as error:
                    sc.execute(capture_args())
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn('https', str(error.exception))

    def test_transport_failure_names_command_and_reason(self):
        # A failed Hub poll says nothing about the session host (intake 93ff852f).
        with patch.object(sc, 'request', return_value={'command_id': 'command-fixture'}), \
             patch.object(sc, 'wait_for_command', side_effect=sc.SessionTransportError('private transport data')), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(sc.SessionInputError, 'hub_unreachable; command command-fixture'):
                sc.execute(capture_args())

    def test_invalid_wait_flags_refuse_before_enqueue(self):
        for flags in [('--timeout', 'nan'), ('--timeout', '0'), ('--interval', 'nan'), ('--interval', 'inf')]:
            with patch.object(sc, 'request') as request:
                with self.assertRaises(ValueError):
                    sc.execute(capture_args(*flags))
                request.assert_not_called()

    def test_http_timeout_uses_remaining_budget(self):
        token = sc._request_deadline.set(time.monotonic()+0.5)
        try:
            with patch.object(sc.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                 patch.object(sc.auth, 'get_access_token', return_value='synthetic'), \
                 patch.object(sc.api, 'safe_http_json', return_value={}) as http:
                sc.request('GET', '/api/sessions/owned/commands')
                self.assertGreater(http.call_args.kwargs['timeout'], 0)
                self.assertLessEqual(http.call_args.kwargs['timeout'], 0.5)
        finally:
            sc._request_deadline.reset(token)

    def test_cli_empty_capture_has_nonzero_exit_and_no_stdout(self):
        from agent_hub_host import cli
        with patch('sys.argv', ['hub','sessions','capture','owned']), \
             patch.object(sc, 'request', return_value={'command_id': 'command-fixture'}), \
             patch.object(sc, 'wait_for_command', return_value={'state':'acked','ack':{'capture':''}}), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_status:
                cli.main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('capture_empty; command command-fixture',stderr.getvalue())

    def test_blocked_maintenance_does_not_overlap_or_delay_commands(self):
        release, entered = threading.Event(), threading.Event()
        dispatcher = Mock()
        def block():
            entered.set()
            release.wait(5)
            return []
        dispatcher.once.side_effect = block
        quota = SimpleNamespace(last_collect_monotonic=0,step=lambda: None)
        thread = session_host.maybe_start_maintenance_thread(None, dispatcher, quota)
        self.addCleanup(lambda: (release.set(), thread.join(2)))
        self.assertTrue(entered.wait(1))
        self.assertIs(session_host.maybe_start_maintenance_thread(thread, dispatcher, quota), thread)
        # Run the real controller for two cycles while Fleet remains blocked.
        import tempfile
        from pathlib import Path
        patches = [patch.object(session_host.config,'load_config',return_value={'machine_id':'h','access_token':'synthetic'}),
                   patch.object(session_host.fleet,'load_settings',return_value={}),
                   patch.object(session_host,'qualify_sender_binding',return_value=SimpleNamespace(session_id='controller',workspace=str(Path.cwd()))),
                   patch.object(session_host.fleet,'Dispatcher',return_value=dispatcher),
                   patch.object(session_host,'maybe_start_maintenance_thread',return_value=thread)]
        patches.extend(patch.object(session_host,name,return_value=None) for name in (
            'maybe_start_status_thread','maybe_start_metrics_thread','maybe_start_disk_thread',
            'maybe_start_host_health_thread','maybe_start_role_consumer_thread'))
        with contextlib.ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            commands=stack.enter_context(patch.object(sc,'execute'))
            directory=stack.enter_context(tempfile.TemporaryDirectory())
            begin=time.monotonic()
            session_host.run('synthetic',directory,interval=1,cycles=2)
            self.assertLess(time.monotonic()-begin,3)
            self.assertEqual(commands.call_count,2)
        dispatcher.once.assert_called_once()
        self.assertFalse(release.is_set())

    def test_ack_route_accepts_64kib_capture_payload_while_other_routes_keep_32kib_limit(self):
        big_capture = ('line "quoted"\n' * 4682)[:65536]
        ack_payload = {
            'correlation': '00000000-0000-0000-0000-000000000001',
            'claim_token': 'tok-1234567890abcdef',
            'native': {'native_id': 's1', 'tag': 't1', 'workspace': '/tmp/w'},
            'outcome': 'delivered',
            'capture': big_capture,
        }
        with patch.object(sc.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
             patch.object(sc.auth, 'get_access_token', return_value='synthetic'), \
             patch.object(sc.api, 'safe_http_json', return_value={'state': 'acked'}) as http:
            res = sc.request('POST', '/api/session-commands/cmd-1/ack', ack_payload)
            self.assertEqual(res, {'state': 'acked'})
            req_obj = http.call_args.args[0]
            self.assertGreater(len(req_obj.data), sc.MAX_REQUEST)
            self.assertLessEqual(len(req_obj.data), sc.MAX_ACK_REQUEST)
            with self.assertRaisesRegex(sc.SessionInputError, 'Session request exceeds capacity'):
                sc.request('POST', '/api/sessions/owned/send', {'body': big_capture})
