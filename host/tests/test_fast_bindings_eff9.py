"""Binding observation must survive a blocked launcher and command receiver."""
import contextlib
import io
import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import session_host


class ClockStop:
    def __init__(self, dispatcher, *, until=45, on_wait=None):
        self.now = 0
        self.until = until
        self.dispatcher = dispatcher
        self.on_wait = on_wait

    def is_set(self):
        return self.now >= self.until

    def wait(self, seconds):
        self.now += seconds
        if self.on_wait:
            self.on_wait(self)


class FastBindingsTests(unittest.TestCase):
    def test_idle_controller_does_not_add_hub_requests(self):
        dispatcher = SimpleNamespace(_inflight=0, _active_launches={},
                                     report_catalog_bindings=Mock())
        stop = ClockStop(dispatcher)
        session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
        dispatcher.report_catalog_bindings.assert_not_called()

    def test_late_native_is_adopted_after_launcher_returns_then_observer_goes_idle(self):
        dispatcher = SimpleNamespace(_inflight=1, _active_launches={},
                                     report_catalog_bindings=Mock())
        stop = ClockStop(dispatcher, on_wait=lambda _: setattr(dispatcher, '_inflight', 0))
        samples = []
        dispatcher.report_catalog_bindings.side_effect = lambda: samples.append(stop.now)
        session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
        # A native row first visible ten seconds after dispatch returned still
        # gets a fresh census; recurring idle HTTP does not continue forever.
        self.assertIn(10, samples)
        self.assertLess(max(samples), 30)
        self.assertLessEqual(len(samples), 6)

    def test_recovered_journal_gets_an_initial_adoption_window(self):
        dispatcher = SimpleNamespace(_inflight=0, _active_launches={'launch': {'state': 'starting'}},
                                     report_catalog_bindings=Mock())
        stop = ClockStop(dispatcher)
        session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
        self.assertGreater(dispatcher.report_catalog_bindings.call_count, 0)
        self.assertLessEqual(dispatcher.report_catalog_bindings.call_count, 6)

    def test_slow_old_census_does_not_consume_late_native_followup(self):
        dispatcher = SimpleNamespace(_inflight=1, _active_launches={},
                                     report_catalog_bindings=Mock())
        stop = ClockStop(dispatcher, until=45)
        samples = []

        def report():
            samples.append(stop.now)
            if len(samples) == 1:
                # The native appears after this census sampled its catalog,
                # while its HTTP report stalls and the launcher returns.
                stop.now += 35
                dispatcher._inflight = 0

        dispatcher.report_catalog_bindings.side_effect = report
        session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
        self.assertIn(35, samples)

    def test_telemetry_400_is_logged_without_body_and_next_report_still_runs(self):
        dispatcher = SimpleNamespace(_inflight=1, _active_launches={},
                                     report_catalog_bindings=Mock())
        stop = ClockStop(dispatcher, until=10)
        dispatcher.report_catalog_bindings.side_effect = [
            ValueError('HTTP request failed with status 400: synthetic-private-body'), None]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['event'], 'bindings_error')
        self.assertEqual(rows[0]['http_status'], 400)
        self.assertEqual(rows[1]['event'], 'bindings_cycle')
        self.assertNotIn('synthetic-private-body', output.getvalue())

    def test_run_reports_while_launcher_and_command_receiver_are_waiting(self):
        observed = threading.Event()
        launcher_done = threading.Event()
        dispatcher = SimpleNamespace(_inflight=1, _active_launches={},
                                     report_catalog_bindings=Mock(),
                                     stop_requested=threading.Event())
        threads = []
        real_start = session_host.maybe_start_named_thread

        def report():
            self.assertFalse(launcher_done.is_set())
            observed.set()

        def start(current, target, args, name):
            if name != 'fleet-bindings':
                return None
            thread = real_start(current, target, args, name)
            threads.append(thread)
            return thread

        def receive(_):
            # The command loop itself can wait on a Hub response. A separate
            # binding observer must run before either waiter completes.
            self.assertTrue(observed.wait(2))
            launcher_done.set()
            dispatcher._inflight = 0

        dispatcher.report_catalog_bindings.side_effect = report
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(session_host.config, 'load_config', return_value={
                'machine_id': 'synthetic-machine', 'access_token': 'synthetic'}))
            stack.enter_context(patch.object(session_host.fleet, 'load_settings', return_value={}))
            stack.enter_context(patch.object(session_host, 'qualify_sender_binding', return_value=
                SimpleNamespace(session_id='synthetic-controller', workspace=str(pathlib.Path.cwd()))))
            stack.enter_context(patch.object(session_host.fleet, 'Dispatcher', return_value=dispatcher))
            stack.enter_context(patch.object(session_host, 'maybe_start_named_thread', side_effect=start))
            stack.enter_context(patch.object(session_host.session_control, 'execute', side_effect=receive))
            for helper in ('maintenance', 'status', 'metrics', 'disk', 'host_health', 'role_consumer', 'token'):
                stack.enter_context(patch.object(session_host, 'maybe_start_' + helper + '_thread', return_value=None))
            session_host.run('synthetic-settings', directory, cycles=1)
        self.assertTrue(observed.is_set())
        self.assertEqual(len(threads), 1)
        self.assertFalse(threads[0].is_alive())


class CensusIsolationTests(unittest.TestCase):
    def dispatcher(self, directory, rows, requests):
        from agent_hub_host import fleet
        from host.tests.test_fleet import settings

        def request(method, path, body=None):
            requests.append((method, path, body))
            if path == '/api/sessions/host-bindings':
                return {'sessions': [{'id': 'hub-session', 'fleet_launch_id': 'new-launch',
                                      'engine': 'opencode'}]}
            return {}

        with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
            dispatcher = fleet.Dispatcher(settings(pathlib.Path(directory)), request,
                                          lambda *args: list(rows), journal_dir=directory)
        dispatcher._session_progress = SimpleNamespace(sample=lambda *args: {})
        return dispatcher

    @staticmethod
    def row(native='native-old', tag='hub-old-launch', created=1000):
        return {'id': native, 'tag': tag, 'workspace': '/synthetic/workspace',
                'cwd': '/synthetic/workspace', 'engine': 'opencode',
                'profile': None, 'state': 'running', 'created_at_ms': created}

    def test_fast_binding_bypasses_blocked_sampling_and_old_sampler_relists(self):
        entered, release, fast_done = threading.Event(), threading.Event(), threading.Event()
        rows, requests, errors = [self.row()], [], []
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = self.dispatcher(directory, rows, requests)

            def sample(*args):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test sampler was not released')
                return {}

            def run(method, done=None):
                try:
                    method()
                except Exception as error:
                    errors.append(error)
                finally:
                    if done:
                        done.set()

            dispatcher._session_progress.sample = sample
            slow = threading.Thread(target=run, args=(dispatcher.report_catalog_bindings,))
            fast = threading.Thread(target=run, args=(dispatcher.report_native_bindings, fast_done))
            with patch('agent_hub_host.token_usage.native_session_tokens', return_value=None):
                slow.start()
                try:
                    self.assertTrue(entered.wait(2))
                    rows.append(self.row('native-new', 'task-hub-new-launch', 2000))
                    fast.start()
                    self.assertTrue(fast_done.wait(2), 'binding waited for slow sampling')
                    self.assertTrue(any(path.endswith('/hub-session/binding')
                                        for _, path, _ in requests))
                finally:
                    release.set()
                    slow.join(5)
                    if fast.ident is not None:
                        fast.join(5)
            self.assertFalse(slow.is_alive())
            self.assertFalse(fast.is_alive())
            self.assertEqual(errors, [])
            snapshots = [body['sessions'] for method, path, body in requests
                         if method == 'PUT' and path == '/api/fleet/sessions']
            self.assertEqual(len(snapshots), 2)
            for snapshot in snapshots:
                self.assertEqual({r['native_id'] for r in snapshot}, {'native-old', 'native-new'})

    def test_complete_fast_census_preserves_proven_enrichment_only_for_exact_lifecycle(self):
        rows, requests = [self.row(), self.row('other-native', 'other-tag', 3000)], []
        progress = {'state': 'working', 'observed_at': '2026-10-26T22:00:00Z'}
        tokens = {'input': 7, 'output': 3}
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = self.dispatcher(directory, rows, requests)
            dispatcher._session_progress.sample = lambda *args: {'native-old': progress}
            with patch('agent_hub_host.token_usage.native_session_tokens', return_value=tokens):
                dispatcher.report_catalog_bindings()
            requests.clear()
            with patch('agent_hub_host.token_usage.native_session_tokens',
                       side_effect=AssertionError('fast census read token files')):
                dispatcher.report_native_bindings()
                payload = next(body['sessions'] for method, path, body in requests if method == 'PUT')
                self.assertEqual(len(payload), 2)
                self.assertEqual(payload[0]['progress'], progress)
                self.assertEqual(payload[0]['tokens'], tokens)
                # A reused UUID, retag, or lifecycle change cannot inherit old
                # sampled progress or accounting. Identity always comes fresh.
                original = dict(rows[0])
                for change in ({'created_at_ms': 2000}, {'tag': 'hub-different'}, {'state': 'uncertain'}):
                    rows[0] = dict(original, **change)
                    requests.clear()
                    dispatcher.report_native_bindings()
                    payload = next(body['sessions'] for method, path, body in requests if method == 'PUT')
                    self.assertNotIn('progress', payload[0])
                    self.assertNotIn('tokens', payload[0])

    def test_one_sampler_remains_bounded_while_fast_observer_keeps_reporting(self):
        entered, release = threading.Event(), threading.Event()
        slow, fast = Mock(), Mock()

        def sample():
            entered.set()
            release.wait(5)

        slow.side_effect = sample
        dispatcher = SimpleNamespace(_inflight=1, _active_launches={},
                                     report_catalog_bindings=slow, report_native_bindings=fast)
        stop = ClockStop(dispatcher, until=20, on_wait=lambda _: entered.wait(2))
        threads = []
        real_thread = threading.Thread

        def thread(*args, **kwargs):
            result = real_thread(*args, **kwargs)
            threads.append(result)
            return result

        with patch.object(session_host.threading, 'Thread', side_effect=thread):
            try:
                session_host.bindings_loop(dispatcher, stop, clock=lambda: stop.now)
                self.assertEqual(slow.call_count, 1)
                self.assertEqual(fast.call_count, 4)
                self.assertTrue(entered.is_set())
            finally:
                release.set()
                for sampler in threads:
                    sampler.join(5)
                    self.assertFalse(sampler.is_alive())


if __name__ == '__main__':
    unittest.main()
