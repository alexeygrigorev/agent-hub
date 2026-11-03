"""Intake 78af9be8: credential failures keep bounded host-side evidence."""
import contextlib
import io
import json
import unittest

from agent_hub_host import bus, fleet


LAUNCH = '11111111-2222-4333-8444-555555555555'


class WorkerCredentialFailureTests(unittest.TestCase):
    def test_http_refusal_is_logged_without_response_bytes_or_replayed(self):
        dispatcher = fleet.Dispatcher.__new__(fleet.Dispatcher)
        dispatcher.request = lambda *args, **kwargs: {}
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
        lines = [line for line in out.getvalue().splitlines() if line.startswith('{')]
        events = [json.loads(line) for line in lines]
        failure = next(event for event in events if event.get('event') == 'worker_credential_failed')
        self.assertEqual(failure['status'], 409)
        self.assertEqual(failure['code'], 'worker_session_required')
        self.assertEqual(failure['error_type'], 'HttpRequestError')
        self.assertNotIn('hubt_never-log-this', out.getvalue())


if __name__ == '__main__':
    unittest.main()
