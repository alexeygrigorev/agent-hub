"""CLI diagnostics for the global live-session limit."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent_hub_host import session_control


class SessionCapDiagnosticsTest(unittest.TestCase):
    def diagnostic(self, status, body):
        error = urllib.error.HTTPError('https://hub.test/api/sessions', status,
                                       'Session limit', {}, io.BytesIO(json.dumps(body).encode()))
        return str(session_control.http_error(error))

    def test_429_reports_current_live_count_and_limit(self):
        message = self.diagnostic(429, {
            'error': 'active_session_limit',
            'detail': 'Hub allows 50 live active sessions; 50 are active and 0 starts are in progress.',
            'active_count': 50,
            'starting_count': 0,
            'limit': 50,
        })
        self.assertIn('active_session_limit', message)
        self.assertIn('50 live active sessions of 50', message)
        self.assertIn('stop an active session and retry', message)

    def test_oversized_error_body_has_a_safe_fixed_diagnostic(self):
        body = {'error': 'active_session_limit', 'active_count': 50, 'limit': 50,
                'private_field': 'DO_NOT_ECHO_PRIVATE_SERVER_DATA'}
        with patch.object(session_control, 'MAX_ERROR_RESPONSE', 32):
            message = self.diagnostic(429, body)
        self.assertIn('response_too_large', message)
        self.assertNotIn('DO_NOT_ECHO_PRIVATE_SERVER_DATA', message)


if __name__ == '__main__':
    unittest.main()
