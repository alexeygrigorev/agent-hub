"""Intake 78af9be8: credential failures keep bounded host-side evidence."""
import contextlib
import io
import json
import unittest

from agent_hub_host import bus, fleet


LAUNCH = '11111111-2222-4333-8444-555555555555'


class WorkerCredentialFailureTests(unittest.TestCase):
    def _dispatcher(self):
        dispatcher = fleet.Dispatcher.__new__(fleet.Dispatcher)
        dispatcher.request = lambda *args, **kwargs: {}
        return dispatcher

    def _failure_event(self, out):
        lines = [line for line in out.getvalue().splitlines() if line.startswith('{')]
        events = [json.loads(line) for line in lines]
        return next(event for event in events if event.get('event') == 'worker_credential_failed')

    def test_http_refusal_is_logged_without_response_bytes_or_replayed(self):
        dispatcher = self._dispatcher()
        body = json.dumps({
            'error': 'worker_session_required',
            'access_token': 'hubt_never-log-this',
        }).encode()
        refusal = bus.HttpRequestError(409, body)
        calls = []

        def provision(workspace, offer_id, request):
            calls.append((workspace, offer_id))
            raise refusal

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(
                ValueError, '^Worker Hub credential could not be prepared$'):
            dispatcher._provision_worker_credential(provision, '/workspace', LAUNCH)

        self.assertEqual(calls, [('/workspace', LAUNCH)])
        failure = self._failure_event(out)
        self.assertEqual(failure['status'], 409)
        self.assertEqual(failure['code'], 'worker_session_required')
        self.assertEqual(failure['error_type'], 'HttpRequestError')
        self.assertNotIn('hubt_never-log-this', out.getvalue())

    def test_transport_timeout_is_logged_without_replay(self):
        # Intake 78af9be8 root cause: the Oct 30 outage was Hub API
        # saturation (10 s transport timeouts / 500s, intake 5551f943), not
        # a credential refusal. A timeout carries no HTTP status or code,
        # but must still leave a bounded journal event and never be replayed
        # (a timeout can follow a committed issuance).
        dispatcher = self._dispatcher()
        calls = []

        def provision(workspace, offer_id, request):
            calls.append((workspace, offer_id))
            raise TimeoutError('timed out')

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(
                ValueError, '^Worker Hub credential could not be prepared$'):
            dispatcher._provision_worker_credential(provision, '/workspace', LAUNCH)

        self.assertEqual(calls, [('/workspace', LAUNCH)])
        failure = self._failure_event(out)
        self.assertIsNone(failure['status'])
        self.assertIsNone(failure['code'])
        self.assertEqual(failure['error_type'], 'TimeoutError')
        self.assertEqual(failure['offer_id'], LAUNCH)

    def test_local_validation_failure_keeps_fixed_diagnostic(self):
        # A malformed issuance response (bad token shape) fails local
        # validation with a plain ValueError: no status, no code, and the
        # offending value must never reach the journal.
        dispatcher = self._dispatcher()

        def provision(workspace, offer_id, request):
            raise ValueError('Invalid scoped worker credential response: hubt_leaked-value')

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(
                ValueError, '^Worker Hub credential could not be prepared$'):
            dispatcher._provision_worker_credential(provision, '/workspace', LAUNCH)

        failure = self._failure_event(out)
        self.assertIsNone(failure['status'])
        self.assertIsNone(failure['code'])
        self.assertEqual(failure['error_type'], 'ValueError')
        self.assertNotIn('hubt_leaked-value', out.getvalue())

    def test_malformed_error_code_is_dropped(self):
        # Only lowercase slug codes are journal-safe; anything else must
        # degrade to null rather than leak free-form server text.
        dispatcher = self._dispatcher()
        refusal = bus.HttpRequestError(500, b'Internal Server Error')
        refusal.error_code = 'Worker Boom (pii?)'

        def provision(workspace, offer_id, request):
            raise refusal

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaisesRegex(
                ValueError, '^Worker Hub credential could not be prepared$'):
            dispatcher._provision_worker_credential(provision, '/workspace', LAUNCH)

        failure = self._failure_event(out)
        self.assertEqual(failure['status'], 500)
        self.assertIsNone(failure['code'])
        self.assertNotIn('Boom', out.getvalue())


if __name__ == '__main__':
    unittest.main()
