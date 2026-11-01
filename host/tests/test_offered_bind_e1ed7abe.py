"""Offered -> bound latency (intake e1ed7abe): offers poll on their own thread, early captures wait."""
import contextlib
import io
import json
import threading
import time
import urllib.error
import unittest
from unittest.mock import patch

from agent_hub_host import fleet, session_control as sc, session_host


class OffersLoop(unittest.TestCase):
    def test_offers_poll_independent_of_a_blocked_main_loop_body(self):
        # The main loop body (host-receive, credentials...) can take tens of seconds. The
        # offers loop is its own thread, so its cadence never includes that time.
        stop, polls = threading.Event(), []
        main_body_blocked = threading.Event()
        with patch.object(session_host, 'offers_step_once', side_effect=lambda d: polls.append(time.monotonic())):
            thread = threading.Thread(target=session_host.offers_loop, args=(object(), stop), kwargs={'poll_s': 0.02})
            thread.start()
            try:
                self.assertFalse(main_body_blocked.wait(0.4))  # the "main loop" never returns
                self.assertGreaterEqual(len(polls), 5)
            finally:
                stop.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_default_cadence_is_the_two_second_offers_poll(self):
        self.assertLessEqual(fleet.OFFERS_POLL_S, 2.0)

    def test_polling_flag_covers_exactly_the_poll(self):
        stop, polling, seen = threading.Event(), threading.Event(), []

        def step(_dispatcher):
            seen.append(polling.is_set())
            stop.set()
        with patch.object(session_host, 'offers_step_once', side_effect=step):
            session_host.offers_loop(object(), stop, polling, poll_s=0.01)
        self.assertEqual(seen, [True])
        self.assertFalse(polling.is_set())

    def test_a_poll_error_never_kills_the_loop(self):
        stop, calls = threading.Event(), []

        def step(_dispatcher):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('boom')
            stop.set()
        with patch.object(session_host, 'offers_step_once', side_effect=step), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            session_host.offers_loop(object(), stop, poll_s=0.01)
        self.assertEqual(len(calls), 2)
        self.assertIn('offers_loop_error', out.getvalue())


class StageTiming(unittest.TestCase):
    def test_stage_event_names_each_pre_claim_stage_without_secrets(self):
        stages = [('start', 10.0), ('admission', 10.5), ('workspace', 12.0), ('claim', 15.0)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fleet._emit_stage_timing('offer-1', stages)
        event = json.loads(out.getvalue())
        self.assertEqual(event['event'], 'offer_dispatch_stages')
        self.assertEqual(event['stages_s'], {'admission': 0.5, 'workspace': 1.5, 'claim': 3.0})
        self.assertEqual(event['total_s'], 5.0)


def pending_error(state='offered'):
    body = json.dumps({'error': 'session_not_bound', 'state': state, 'pending': True, 'retry_after_s': 5}).encode()
    return urllib.error.HTTPError('https://hub.test/x', 409, 'Conflict', {}, io.BytesIO(body))


class PendingCapture(unittest.TestCase):
    def args(self, *extra):
        import argparse
        parser = argparse.ArgumentParser()
        sc.add_commands(parser.add_subparsers(dest='cmd'))
        return parser.parse_args(['sessions', 'capture', 'owned', '--key', 'stable', *extra])

    def test_http_error_carries_the_pending_state(self):
        error = sc.http_error(pending_error('starting'), key='stable')
        self.assertTrue(error.pending)
        self.assertEqual((error.status, error.code, error.state), (409, 'session_not_bound', 'starting'))
        self.assertIn('not yet bound', str(error))

    def test_non_pending_409_is_unchanged(self):
        body = json.dumps({'error': 'session_not_bound', 'state': 'stopped', 'pending': False}).encode()
        error = sc.http_error(urllib.error.HTTPError('u', 409, 'c', {}, io.BytesIO(body)))
        self.assertFalse(error.pending)

    def test_capture_waits_for_the_bind_then_enqueues(self):
        states = iter(['offered', 'starting', 'running'])
        posts, keys = [], []

        def fake(method, path, payload=None):
            if method == 'POST':
                posts.append(path)
                keys.append(payload['key'])
                if len(posts) == 1:
                    raise sc.http_error(pending_error())
                return {'command_id': 'c1'}
            return {'state': next(states)}
        ack = {'state': 'acked', 'ack': {'capture': 'hello'}}
        with patch.object(sc, 'request', side_effect=fake), patch.object(sc.time, 'sleep'), \
                patch.object(sc, 'wait_for_command', return_value=ack), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = sc.execute(self.args())
        self.assertEqual(len(posts), 2)
        # The retry after the bind wait replays the same idempotency key: no duplicate capture.
        self.assertEqual(keys, ['stable', 'stable'])
        self.assertEqual(result['command_wait'], ack)
        self.assertIn('waiting up to', err.getvalue())

    def test_capture_gives_a_clear_error_when_the_bind_never_comes(self):
        def fake(method, path, payload=None):
            if method == 'POST':
                raise sc.http_error(pending_error())
            return {'state': 'offered'}
        clock = [0.0]
        with patch.object(sc, 'request', side_effect=fake), \
                patch.object(sc.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(sc.time, 'sleep', side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch.object(sc, 'BIND_WAIT_TIMEOUT', 6.0), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(sc.SessionInputError, 'session_not_bound; session owned did not reach running'):
                sc.execute(self.args())

    def test_capture_stops_waiting_when_the_session_is_terminal(self):
        def fake(method, path, payload=None):
            if method == 'POST':
                raise sc.http_error(pending_error())
            return {'state': 'failed'}
        with patch.object(sc, 'request', side_effect=fake), patch.object(sc.time, 'sleep') as sleep, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(sc.SessionInputError, 'session_not_bound'):
                sc.execute(self.args())
        sleep.assert_not_called()

    def test_no_wait_capture_returns_the_pending_error_immediately(self):
        def fake(method, path, payload=None):
            raise sc.http_error(pending_error())
        with patch.object(sc, 'request', side_effect=fake), patch.object(sc.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'not yet bound'):
                sc.execute(self.args('--no-wait'))
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
