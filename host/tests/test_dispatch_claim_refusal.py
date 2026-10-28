"""d13fe6d2: durable redacted claim causes, restart recovery and authority fencing."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from agent_hub_host import bus
from agent_hub_host.dispatch_diagnostics import ClaimRefusedError, classify
from agent_hub_host.fleet import Dispatcher
import test_preclaim_retry_488b860e as helpers
from test_preclaim_retry_488b860e import FakeHub, _valid_uuid4


class ClaimRefusalTests(unittest.TestCase):
    setUp = helpers.PreclaimRetry.setUp
    _settings = helpers.PreclaimRetry._settings
    _admission = helpers.PreclaimRetry._admission
    _offer = helpers.PreclaimRetry._offer
    _runner = helpers.PreclaimRetry._runner

    def test_real_transport_cause_is_bounded_and_redacted_in_journal_after_restart(self):
        launch_id = _valid_uuid4()
        secret = 'opaqueSecretWithoutPunctuation123'
        body = json.dumps({'error': 'quota_unavailable', 'reason': 'reserve',
                           'token': secret, 'prompt': secret}).encode()
        error = urllib.error.HTTPError('https://hub.test/', 409, secret, {}, io.BytesIO(body))
        calls = []
        def request(method, path, payload=None):
            if path.endswith('/claim'):
                with patch.object(bus.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                     patch.object(bus.auth, 'get_access_token', return_value=secret), \
                     patch('agent_hub_host.api.safe_http_json', side_effect=error):
                    return bus._do_request(method, path, payload)
            if path.endswith('/reject'):
                return {'id': launch_id, 'state': 'failed'}
            return {}
        dispatcher = Dispatcher(self._settings(), request, self._runner(calls, launch_id), journal_dir=self.tmp.name)
        dispatcher._dispatch_one(self._offer(launch_id), [])
        restarted = Dispatcher(self._settings(), request, self._runner(calls, launch_id), journal_dir=self.tmp.name)
        for directory in ('fleet-launches', 'claim-refusals'):
            raw = (Path(restarted.journal_dir) / directory / (launch_id + '.json')).read_text()
            self.assertNotIn(secret, raw)
            self.assertLess(len(raw), 4096)
            self.assertEqual(json.loads(raw)['diagnostic'], {
                'reason': 'dispatch_claim_refused', 'message': 'The Hub refused the dispatch claim before native start.',
                'stage': 'dispatch_route', 'error_type': 'ValueError', 'http_status': 409,
                'hub_error': 'quota_unavailable', 'hub_reason': 'reserve', 'retryable': False})
        self.assertFalse(any('run' in argv for argv in calls))

    def test_untrusted_slug_never_enters_diagnostic(self):
        for value in ('opaqueSecret123', 'Bearer abc', 'prompt\ntext', ['bad'], {'bad': 1}, 'x' * 200):
            with self.subTest(value=type(value).__name__):
                exc = bus.HttpRequestError(403, json.dumps({'error': value, 'reason': value}))
                diagnostic = classify(ClaimRefusedError(exc))
                self.assertEqual(diagnostic['http_status'], 403)
                self.assertNotIn('hub_error', diagnostic)
                self.assertNotIn('hub_reason', diagnostic)
                self.assertFalse(diagnostic['retryable'])

    def test_stale_claim_retries_with_fresh_admission_after_restart_once(self):
        launch_id = _valid_uuid4()
        hub = FakeHub(); hub.offer(launch_id)
        calls = []
        state = {'stale': True, 'admissions': 0}
        normal = self._runner(calls, launch_id)
        def runner(argv, **kwargs):
            if '/bin/true' in argv:
                state['admissions'] += 1
            return normal(argv, **kwargs)
        def request(method, path, payload=None):
            if path.endswith('/claim') and state['stale']:
                raise bus.HttpRequestError(409, b'{"error":"fresh_admission_required"}')
            if path.endswith('/reject') and state['stale']:
                self.assertTrue(payload['diagnostic']['retryable'])
                return {'id': launch_id, 'state': 'held', 'retryable': True}
            return hub.request(method, path, payload)
        dispatcher = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
        results = []; dispatcher._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'held')
        self.assertFalse(any('run' in argv for argv in calls))
        self.assertEqual(hub.state[launch_id], 'offered')
        state['stale'] = False
        restarted = Dispatcher(self._settings(), request, runner, journal_dir=self.tmp.name)
        results = []; restarted._dispatch_one(self._offer(launch_id), results)
        self.assertEqual(results[0]['state'], 'running')
        self.assertGreaterEqual(state['admissions'], 2)
        self.assertEqual(sum('run' in argv for argv in calls), 1)
        self.assertEqual(len(hub.claim_calls), 1)

    def test_authority_and_quota_refusals_are_never_retryable(self):
        for error in ('claim_inactive', 'claim_conflict', 'claim_authority_changed',
                      'claim_authority_unsupported', 'claim_scope_mismatch', 'quota_unavailable', 'provider_disabled'):
            for status in (403, 409):
                diagnostic = classify(ClaimRefusedError(bus.HttpRequestError(status, json.dumps({'error': error}))))
                self.assertFalse(diagnostic['retryable'])
                self.assertEqual(diagnostic['hub_error'], error)

    def test_ambiguous_prior_attempt_keeps_key_despite_definitive_refusal(self):
        launch_id = _valid_uuid4()
        hub = FakeHub(); hub.offer(launch_id)
        hub.fail_claim_with = lambda: TimeoutError('lost')
        dispatcher = Dispatcher(self._settings(), hub.request, self._runner([], launch_id), journal_dir=self.tmp.name)
        dispatcher._dispatch_one(self._offer(launch_id), [])
        key = dispatcher._pending_claim_key(launch_id)
        hub.fail_claim_with = lambda: bus.HttpRequestError(409, b'{"error":"fresh_admission_required"}')
        dispatcher._dispatch_one(self._offer(launch_id), [])
        self.assertEqual(dispatcher._pending_claim_key(launch_id), key)
        self.assertFalse(any(path.endswith('/reject') for _, path in hub.calls))

    def test_sibling_refusal_retains_sidecar_without_overwriting_effect_fence(self):
        launch_id = _valid_uuid4()
        dispatcher = Dispatcher(self._settings(), lambda *args: (_ for _ in ()).throw(
            bus.HttpRequestError(409, b'{"error":"already_claimed"}')), journal_dir=self.tmp.name)
        record = {'offer_id': launch_id, 'state': 'uncertain', 'native_id': 'effect', 'settled': False}
        dispatcher._persist_launch_record(record)
        with self.assertRaises(ClaimRefusedError):
            dispatcher._claim_launch('/api/fleet/launches/' + launch_id + '/claim', {'claim_key': 'a' * 32}, launch_id, self.workspace)
        self.assertEqual(json.loads((Path(self.tmp.name) / 'fleet-launches' / (launch_id + '.json')).read_text())['native_id'], 'effect')
        self.assertEqual(json.loads((Path(self.tmp.name) / 'claim-refusals' / (launch_id + '.json')).read_text())['diagnostic']['hub_error'], 'already_claimed')

    def test_timeout_then_refusal_in_same_attempt_preserves_pending_key(self):
        launch_id = _valid_uuid4()
        calls = []
        def request(method, path, payload=None):
            calls.append(path)
            if len(calls) == 1:
                raise TimeoutError('claim may have committed')
            raise bus.HttpRequestError(409, b'{"error":"fresh_admission_required"}')
        dispatcher = Dispatcher(self._settings(), request, journal_dir=self.tmp.name)
        from agent_hub_host.fleet import ClaimTimeoutError
        with self.assertRaises(ClaimTimeoutError) as caught:
            dispatcher._claim_launch('/api/fleet/launches/' + launch_id + '/claim', {'claim_key': 'b' * 32}, launch_id, self.workspace)
        self.assertEqual(caught.exception.claim_key, 'b' * 32)
        self.assertEqual(caught.exception.refusal_diagnostic['hub_error'], 'fresh_admission_required')
        self.assertFalse(caught.exception.refusal_diagnostic['retryable'])
