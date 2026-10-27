"""Intake 488b860e: a dispatch timeout before/at claim must hold + retry,
never fail with dispatch_local_command_failed; the same-host re-claim is
idempotent (one claim, one native process)."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_hub_host.fleet import (
    CLAIM_ATTEMPTS,
    CLAIM_KEY_RE,
    ClaimTimeoutError,
    Dispatcher,
    _is_ambiguous_claim_error,
)
from agent_hub_host.dispatch_diagnostics import MESSAGES


def _valid_uuid4():
    import uuid
    return str(uuid.uuid4())


class FakeHub:
    """Minimal in-memory claim server with the production idempotency rule.

    offered -> starting exactly once per launch; a retry with the SAME key on
    an effect-free starting launch replays; any other key conflicts.
    """

    def __init__(self):
        self.state = {}  # launch_id -> 'offered' | 'starting'
        self.keys = {}  # launch_id -> claim_key that claimed it
        self.claim_calls = []  # claim_key per claim call, in order
        self.calls = []  # (method, path) for every request
        self.fail_claim_with = None  # exception factory or None

    def offer(self, launch_id):
        self.state[launch_id] = 'offered'

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        if path.endswith('/claim'):
            launch_id = path.split('/')[-2]
            key = (payload or {}).get('claim_key')
            self.claim_calls.append(key)
            if self.fail_claim_with is not None:
                raise self.fail_claim_with()
            if self.state.get(launch_id) != 'offered':
                if key is not None and self.keys.get(launch_id) == key:
                    return {'state': 'starting', 'replay': True}
                raise ValueError('HTTP request failed with status 409:already_claimed')
            self.state[launch_id] = 'starting'
            self.keys[launch_id] = key
            return {'state': 'starting'}
        if path.endswith('/reject'):
            return {'id': path.split('/')[-2], 'state': 'failed',
                    'reason': (payload or {}).get('reason')}
        if path.endswith('/result'):
            return {'id': path.split('/')[-2], 'state': (payload or {}).get('state')}
        return {}


class PreclaimRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chmod(self.tmp.name, 0o700)
        self.workspace = Path(self.tmp.name) / 'ws'
        self.workspace.mkdir(mode=0o700, parents=True)
        self.launcher_root = Path(self.tmp.name) / 'launcher'
        self.launcher_root.mkdir(mode=0o700, parents=True)
        self.launcher_cfg = Path(self.tmp.name) / 'launcher_cfg'
        self.launcher_cfg.mkdir(mode=0o700, parents=True)
        self.native_id = _valid_uuid4()

    def _settings(self):
        return {
            'version': 1,
            'workspaces': {'main': str(self.workspace)},
            'subscriptions': [
                {
                    'service': 'codex',
                    'account_label': 'pro-account',
                    'engine': 'codex',
                    'profile': 'hub-pro-account',
                    'launcher_provider': 'codex',
                    'launcher_model': 'gpt-6.1-sol',
                    'admission_argv': ['/bin/true'],
                }
            ],
            'launcher': {
                'argv': ['launcher-cli'],
                'root': str(self.launcher_root),
                'config_dir': str(self.launcher_cfg),
            },
        }

    def _admission(self):
        return {
            'launch_allowed': True,
            'profile_supported': True,
            'account_label': 'pro-account',
            'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def _offer(self, launch_id):
        return {
            'id': launch_id,
            'engine': 'codex',
            'profile': 'hub-pro-account',
            'account': 'pro-account',
            'mode': 'headless',
            'workspace': 'main',
            'prompt': 'trivial proof task',
        }

    def _runner(self, calls, offer_id):
        def fake_runner(argv, cwd=None, timeout=None, payload=None):
            calls.append(argv)
            cmd = ' '.join(argv)
            if '/bin/true' in cmd:
                return self._admission()
            if 'submit' in cmd:
                return {'id': 'hub-' + offer_id}
            if 'plan' in cmd:
                return {'eligible': [{'provider': 'codex', 'model': 'gpt-6.1-sol',
                                       'task_fit': 1.0, 'profile': 'hub-pro-account',
                                       'account_label': 'pro-account'}]}
            if 'run' in cmd:
                return {
                    'backend': 'aplexer',
                    'task_id': 'hub-' + offer_id,
                    'receipt': {
                        'session_id': self.native_id,
                        'state': 'running',
                        'first_action_verified': True,
                        'tag': 'task-hub-' + offer_id,
                        'workspace': str(self.workspace),
                        'engine': 'codex',
                        'profile': 'hub-pro-account',
                        'account_label': 'pro-account',
                        'phase': 'running',
                    },
                }
            return {}
        return fake_runner

    def test_preclaim_command_timeout_holds_then_next_cycle_starts_once(self):
        """AC(a): preparation timeout must not reject an unclaimed launch."""
        import subprocess
        from agent_hub_host.fleet import LocalCommandTimeout
        for phase in ('admission', 'submit', 'plan'):
            for error in (TimeoutError('slow status'), LocalCommandTimeout('deadline'),
                          subprocess.TimeoutExpired(['check'], 30)):
                with self.subTest(phase=phase, error=type(error).__name__):
                    hub = FakeHub()
                    launch_id = _valid_uuid4()
                    hub.offer(launch_id)
                    calls = []
                    normal = self._runner(calls, launch_id)
                    fail = [True]
                    def runner(argv, **kwargs):
                        selected = '/bin/true' in argv if phase == 'admission' else phase in argv
                        if selected and fail[0]:
                            raise error
                        return normal(argv, **kwargs)
                    dispatcher = Dispatcher(self._settings(), hub.request, runner,
                                            journal_dir=self.tmp.name)
                    results = []
                    dispatcher._dispatch_one(self._offer(launch_id), results)
                    self.assertEqual(results[0]['state'], 'held')
                    self.assertEqual(results[0]['reason'], 'claim_timeout')
                    self.assertEqual(hub.state[launch_id], 'offered')
                    self.assertFalse(any(p.endswith('/reject') for _, p in hub.calls))
                    self.assertEqual(hub.claim_calls, [])
                    self.assertFalse(any('run' in argv for argv in calls))
                    fail[0] = False
                    results = []
                    dispatcher._dispatch_one(self._offer(launch_id), results)
                    self.assertEqual(results[0]['state'], 'running')
                    self.assertEqual(len(hub.claim_calls), 1)
                    self.assertEqual(sum('run' in argv for argv in calls), 1)

    def test_preclaim_timeout_preserves_prior_ambiguous_claim_key(self):
        hub = FakeHub()
        launch_id = _valid_uuid4()
        hub.offer(launch_id)
        hub.fail_claim_with = lambda: TimeoutError('claim response lost')
        calls = []
        normal = self._runner(calls, launch_id)
        dispatcher = Dispatcher(self._settings(), hub.request, normal,
                                journal_dir=self.tmp.name)
        dispatcher._dispatch_one(self._offer(launch_id), [])
        key = hub.claim_calls[0]
        def slow_plan(argv, **kwargs):
            if 'plan' in argv:
                raise TimeoutError('status slow before claim replay')
            return normal(argv, **kwargs)
        dispatcher.runner = slow_plan
        results = []
        dispatcher._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual(dispatcher._pending_claim_key(launch_id), key)
        dispatcher.runner = normal
        hub.fail_claim_with = None
        results = []
        dispatcher._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'running')
        self.assertEqual(set(hub.claim_calls), {key})
        self.assertEqual(sum('run' in argv for argv in calls), 1)

    def test_definitive_preclaim_failure_is_not_retried_as_timeout(self):
        hub = FakeHub()
        launch_id = _valid_uuid4()
        hub.offer(launch_id)
        calls = []
        normal = self._runner(calls, launch_id)
        def denied(argv, **kwargs):
            if 'plan' in argv:
                raise RuntimeError('command refused')
            return normal(argv, **kwargs)
        dispatcher = Dispatcher(self._settings(), hub.request, denied,
                                journal_dir=self.tmp.name)
        results = []
        dispatcher._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'failed')
        self.assertEqual(results[0]['reason'], 'dispatch_local_command_failed')
        self.assertFalse(hub.claim_calls)
        self.assertFalse(any('run' in argv for argv in calls))

    def test_real_device_prepare_timeout_holds_before_claim(self):
        import subprocess
        from unittest.mock import patch
        from agent_hub_host import opencode_auth as auth
        settings = self._settings()
        settings['subscriptions'][0]['launcher_provider'] = 'opencode'
        settings['opencode_auth'] = dict(binary='/installed/opencode',
            source_data=str(Path(self.tmp.name) / 'source'),
            source_config=str(Path(self.tmp.name) / 'config'),
            target_data=str(Path(self.tmp.name) / 'runtime'))
        hub = FakeHub()
        launch_id = _valid_uuid4()
        hub.offer(launch_id)
        calls = []
        dispatcher = Dispatcher(settings, hub.request, self._runner(calls, launch_id),
                                journal_dir=self.tmp.name)
        def slow_native(*args):
            raise subprocess.TimeoutExpired(['native-auth'], 30)
        results = []
        with patch.object(auth, '_verified_recently', return_value=False), \
             patch.object(auth, '_verification_identity', return_value=None), \
             patch.object(auth, '_native', side_effect=slow_native):
            dispatcher._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual(results[0]['reason'], 'engine_auth_check_unavailable')
        self.assertEqual(hub.state[launch_id], 'offered')
        self.assertFalse(any(p.endswith('/claim') or p.endswith('/reject') for _, p in hub.calls))
        self.assertFalse(any('run' in argv for argv in calls))

    def test_replay_refusal_preserves_key_until_guard_recovers(self):
        for guard in ('quota_unavailable', 'provider_disabled', 'fresh_admission_required', 'cpu_pressure'):
            with self.subTest(guard=guard):
                hub = FakeHub()
                launch_id = _valid_uuid4()
                hub.offer(launch_id)
                phase = ['lost']
                def request(method, path, payload=None):
                    if path.endswith('/claim'):
                        if phase[0] == 'lost':
                            hub.request(method, path, payload)
                            raise TimeoutError('committed claim response lost')
                        if phase[0] == 'refused':
                            raise ValueError('HTTP request failed with status 409:' + guard)
                    return hub.request(method, path, payload)
                calls = []
                runner = self._runner(calls, launch_id)
                dispatcher = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
                dispatcher._dispatch_one(self._offer(launch_id), [])
                key = hub.keys[launch_id]
                resumed = dict(self._offer(launch_id), state='starting', claim_key=key)
                phase[0] = 'refused'
                dispatcher._dispatch_one(resumed, [])
                self.assertEqual(dispatcher._pending_claim_key(launch_id), key)
                self.assertFalse(any(p.endswith('/reject') for _, p in hub.calls))
                phase[0] = 'healthy'
                restarted = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
                results = []
                restarted._dispatch_one(resumed, results)
                self.assertEqual(results[0]['state'], 'running')
                self.assertEqual(set(hub.claim_calls), {key})
                self.assertEqual(sum('run' in argv for argv in calls), 1)

    def test_native_effect_fence_survives_lost_result_and_process_death(self):
        from unittest.mock import patch
        for mode in ('interactive', 'headless'):
            for failure in (TimeoutError, SystemExit):
                with self.subTest(mode=mode, failure=failure.__name__):
                    hub = FakeHub()
                    launch_id = _valid_uuid4()
                    hub.offer(launch_id)
                    phase = ['lost']
                    def request(method, path, payload=None):
                        if path.endswith('/claim') and phase[0] == 'lost':
                            hub.request(method, path, payload)
                            raise TimeoutError('committed claim response lost')
                        if path.endswith('/result'):
                            raise TimeoutError('uncertainty receipt lost')
                        return hub.request(method, path, payload)
                    calls = []
                    normal = self._runner(calls, launch_id)
                    starts = []
                    def runner(argv, **kwargs):
                        if ('start' if mode == 'interactive' else 'run') in argv:
                            starts.append(argv)
                            raise failure('native outcome unknown')
                        return normal(argv, **kwargs)
                    offer = dict(self._offer(launch_id), mode=mode,
                                 prompt='' if mode == 'interactive' else 'proof')
                    dispatcher = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
                    dispatcher._dispatch_one(offer, [])
                    resumed = dict(offer, state='starting', claim_key=hub.keys[launch_id])
                    phase[0] = 'healthy'
                    with patch.object(dispatcher, '_check_headless_health', return_value=None), \
                         patch('agent_hub_host.launcher_recovery.settle_unstarted', return_value=False), \
                         patch('agent_hub_host.launch_diagnostics.exception_diagnostic', return_value={
                             'reason': 'dispatch_local_command_failed', 'stage': 'dispatch_route',
                             'message': 'fixture', 'error_type': 'TimeoutError'}):
                        if failure is SystemExit:
                            with self.assertRaises(SystemExit):
                                dispatcher._dispatch_one(resumed, [])
                        else:
                            dispatcher._dispatch_one(resumed, [])
                    self.assertIsNone(dispatcher._pending_claim_key(launch_id))
                    restarted = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
                    results = []
                    restarted._dispatch_one(resumed, results)
                    self.assertEqual(results[0]['reason'], 'already_claimed')
                    self.assertEqual(len(starts), 1)

    def test_local_command_deadline_has_typed_timeout(self):
        import sys
        from agent_hub_host.fleet import LocalCommandTimeout, run_json
        with self.assertRaises(LocalCommandTimeout):
            run_json([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=.02)

    def test_ambiguous_claim_errors_cover_transport_blips_not_answers(self):
        import http.client
        import socket
        import urllib.error
        for exc in (TimeoutError('timed out'), socket.timeout('timed out'),
                    ConnectionRefusedError('refused'), ConnectionResetError('reset'),
                    OSError('transport'), urllib.error.URLError(TimeoutError('timed out')),
                    urllib.error.URLError(ConnectionRefusedError('refused')),
                    http.client.RemoteDisconnected('closed')):
            self.assertTrue(_is_ambiguous_claim_error(exc), type(exc).__name__)
        # Definitive Hub answers are never ambiguous: a 409 conflict holds
        # through the existing already_claimed path, refusals still reject.
        for exc in (ValueError('HTTP request failed with status 409:already_claimed'),
                    ValueError('Fresh admission refused'),
                    RuntimeError('boom'),
                    urllib.error.HTTPError('http://x/', 409, 'Conflict', {}, None)):
            self.assertFalse(_is_ambiguous_claim_error(exc), repr(exc)[:60])
        self.assertIn('claim_timeout', MESSAGES)

    def test_claim_timeout_holds_without_reject_and_retries_same_key(self):
        """AC(a): persistent claim timeouts hold, never fail the launch."""
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000001'
        hub.offer(launch_id)
        hub.fail_claim_with = lambda: TimeoutError('timed out')
        offer = self._offer(launch_id)
        calls = []
        dispatcher = Dispatcher(self._settings(), hub.request,
                                self._runner(calls, launch_id),
                                journal_dir=self.tmp.name)
        results = []
        dispatcher._dispatch_one(offer, results)
        self.assertEqual(len(results), 1)
        outcome = results[0]
        self.assertEqual(outcome['id'], launch_id)
        self.assertEqual(outcome['state'], 'held')
        self.assertEqual(outcome['reason'], 'claim_timeout')
        self.assertNotEqual(outcome.get('reason'), 'dispatch_local_command_failed')
        # Same-key bounded retries, then hold: no reject, no result posted.
        self.assertEqual(len(hub.claim_calls), CLAIM_ATTEMPTS)
        self.assertEqual(len(set(hub.claim_calls)), 1)
        self.assertFalse(any(p.endswith('/reject') for _, p in hub.calls))
        self.assertFalse(any(p.endswith('/result') for _, p in hub.calls))
        journal = json.loads((Path(self.tmp.name) / 'fleet-launches' / (launch_id + '.json')).read_text())
        self.assertEqual(journal['state'], 'claim_pending')
        self.assertEqual(journal['reason'], 'claim_timeout')
        self.assertFalse(journal['settled'])
        first_key = journal['claim_key']
        # The next cycle retries with the SAME attempt key and reaches running.
        hub.fail_claim_with = None
        runner_calls = []
        dispatcher2 = Dispatcher(self._settings(), hub.request,
                                 self._runner(runner_calls, launch_id),
                                 journal_dir=self.tmp.name)
        results2 = []
        dispatcher2._dispatch_one(offer, results2)
        self.assertEqual(results2[0]['state'], 'running')
        self.assertEqual(hub.claim_calls[-1], first_key)
        self.assertEqual(hub.state[launch_id], 'starting')
        runs = [c for c in runner_calls if 'run' in c]
        self.assertEqual(len(runs), 1)

    def test_late_landing_claim_plus_retry_is_exactly_once(self):
        """AC(b): timeout after the claim landed + same-key retry = one claim,
        one native process."""
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000002'
        hub.offer(launch_id)
        state = {'first': True}

        def flaky_request(method, path, payload=None):
            if path.endswith('/claim') and state['first']:
                state['first'] = False
                # Commit server-side, then lose the response: the client sees
                # a timeout for a claim that already landed.
                committed = hub.request(method, path, payload)
                self.assertEqual(committed.get('state'), 'starting')
                raise TimeoutError('timed out')
            return hub.request(method, path, payload)

        offer = self._offer(launch_id)
        runner_calls = []
        dispatcher = Dispatcher(self._settings(), flaky_request,
                                self._runner(runner_calls, launch_id),
                                journal_dir=self.tmp.name)
        res = dispatcher.dispatch(offer)
        self.assertEqual(res['state'], 'running')
        self.assertEqual(res['native_id'], self.native_id)
        # Exactly two claim calls with the same key, one server-side claim.
        self.assertEqual(len(hub.claim_calls), 2)
        self.assertEqual(len(set(hub.claim_calls)), 1)
        self.assertEqual(hub.keys[launch_id], hub.claim_calls[0])
        self.assertEqual(hub.state[launch_id], 'starting')
        # Exactly one native start, no reject round-trip.
        self.assertEqual(len([c for c in runner_calls if 'run' in c]), 1)
        self.assertFalse(any(p.endswith('/reject') for _, p in hub.calls))

    def test_sibling_key_conflict_still_holds_without_native_effect(self):
        """A different key (sibling loop) still conflicts and never starts."""
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000003'
        hub.offer(launch_id)
        hub.keys[launch_id] = 'other-attempt-key'
        hub.state[launch_id] = 'starting'
        offer = self._offer(launch_id)
        runner_calls = []
        dispatcher = Dispatcher(self._settings(), hub.request,
                                self._runner(runner_calls, launch_id),
                                journal_dir=self.tmp.name)
        results = []
        dispatcher._dispatch_one(offer, results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual(results[0]['reason'], 'already_claimed')
        self.assertEqual([c for c in runner_calls if 'run' in c], [])

    def test_preclaim_fault_flag_fires_once_then_retry_succeeds(self):
        """The opt-in fault flag reproduces the ambiguity for the live proof."""
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000004'
        hub.offer(launch_id)
        flag = Path(self.tmp.name) / 'fleet-fault-preclaim.json'
        flag.write_text(json.dumps({'mode': 'timeout_once',
                                    'workspaces': [str(self.workspace)]}))
        offer = self._offer(launch_id)
        runner_calls = []
        dispatcher = Dispatcher(self._settings(), hub.request,
                                self._runner(runner_calls, launch_id),
                                journal_dir=self.tmp.name)
        res = dispatcher.dispatch(offer)
        self.assertEqual(res['state'], 'running')
        self.assertEqual(len(hub.claim_calls), 2)
        self.assertEqual(len(set(hub.claim_calls)), 1)
        self.assertEqual(len([c for c in runner_calls if 'run' in c]), 1)

    def test_preclaim_fault_flag_off_by_default(self):
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000005'
        hub.offer(launch_id)
        offer = self._offer(launch_id)
        runner_calls = []
        dispatcher = Dispatcher(self._settings(), hub.request,
                                self._runner(runner_calls, launch_id),
                                journal_dir=self.tmp.name)
        res = dispatcher.dispatch(offer)
        self.assertEqual(res['state'], 'running')
        # No flag file: exactly one claim call.
        self.assertEqual(len(hub.claim_calls), 1)

    def test_claim_key_max_length_matches_server(self):
        # The server CLAIM_KEY allows at most 128 chars total; the host
        # validator must agree so a host-held key is never a server 400.
        self.assertTrue(CLAIM_KEY_RE.fullmatch('a' + 'b' * 127))
        self.assertFalse(CLAIM_KEY_RE.fullmatch('a' + 'b' * 128))
        self.assertTrue(CLAIM_KEY_RE.fullmatch('9' * 32))

    def _runner_with_incarnation(self, calls, offer_id, incarnation):
        base = self._runner(calls, offer_id)

        def runner(argv, cwd=None, timeout=None, payload=None):
            out = base(argv, cwd=cwd, timeout=timeout, payload=payload)
            if isinstance(out, dict) and isinstance(out.get('receipt'), dict):
                out = dict(out, receipt=dict(out['receipt'], created_at_ms=incarnation))
            return out

        return runner

    def test_exhausted_attempts_late_landing_resumes_next_cycle(self):
        """Follow-up: every claim attempt times out but one lands late.

        The launch is held with the attempt key; the server re-offers the
        effect-free starting launch with its stored key and the next cycle
        replays the claim exactly once, starting one native process.
        """
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000006'
        hub.offer(launch_id)

        def always_late_request(method, path, payload=None):
            if path.endswith('/claim'):
                committed = hub.request(method, path, payload)
                self.assertEqual(committed.get('state'), 'starting')
                raise TimeoutError('timed out')
            return hub.request(method, path, payload)

        offer = self._offer(launch_id)
        dispatcher = Dispatcher(self._settings(), always_late_request,
                                self._runner([], launch_id),
                                journal_dir=self.tmp.name)
        results = []
        dispatcher._dispatch_one(offer, results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual(results[0]['reason'], 'claim_timeout')
        self.assertEqual(len(hub.claim_calls), CLAIM_ATTEMPTS)
        self.assertEqual(len(set(hub.claim_calls)), 1)
        journal = json.loads((Path(self.tmp.name) / 'fleet-launches' / (launch_id + '.json')).read_text())
        self.assertEqual(journal['state'], 'claim_pending')
        key = journal['claim_key']
        self.assertEqual(hub.keys[launch_id], key)
        self.assertEqual(hub.state[launch_id], 'starting')
        # Next cycle: the server re-offers the effect-free starting launch
        # with its stored key; transport is healthy again.
        reoffer = dict(offer, state='starting', claim_key=key)
        runner_calls = []
        dispatcher2 = Dispatcher(self._settings(), hub.request,
                                 self._runner_with_incarnation(runner_calls, launch_id, 1793041501000),
                                 journal_dir=self.tmp.name)
        results2 = []
        dispatcher2._dispatch_one(reoffer, results2)
        self.assertEqual(results2[0]['state'], 'running')
        # Exactly one more claim call (the replay), same key, one server
        # claim total, exactly one native start, no reject round-trip.
        self.assertEqual(len(hub.claim_calls), CLAIM_ATTEMPTS + 1)
        self.assertEqual(len(set(hub.claim_calls)), 1)
        self.assertEqual(hub.state[launch_id], 'starting')
        self.assertEqual(len([c for c in runner_calls if 'run' in c]), 1)
        self.assertFalse(any(p.endswith('/reject') for _, p in hub.calls))
        resumed = json.loads((Path(self.tmp.name) / 'fleet-launches' / (launch_id + '.json')).read_text())
        self.assertEqual(resumed['state'], 'running')

    def test_sibling_key_reoffer_is_never_stolen(self):
        """A re-offered starting launch with a foreign key is skipped."""
        hub = FakeHub()
        launch_id = 'a1b2c3d4-0000-4000-8000-000000000007'
        hub.offer(launch_id)
        hub.keys[launch_id] = 'other-attempt-key-0000000000000001'
        hub.state[launch_id] = 'starting'
        offer = dict(self._offer(launch_id), state='starting',
                     claim_key='other-attempt-key-0000000000000001')
        runner_calls = []
        dispatcher = Dispatcher(self._settings(), hub.request,
                                self._runner(runner_calls, launch_id),
                                journal_dir=self.tmp.name)
        results = []
        dispatcher._dispatch_one(offer, results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertEqual(results[0]['reason'], 'already_claimed')
        # No claim attempt, no native effect, no journal record.
        self.assertEqual(hub.claim_calls, [])
        self.assertEqual(runner_calls, [])
        self.assertFalse((Path(self.tmp.name) / 'fleet-launches' / (launch_id + '.json')).exists())


if __name__ == '__main__':
    unittest.main()
