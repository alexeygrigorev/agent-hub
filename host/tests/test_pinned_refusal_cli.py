"""CLI diagnostics for the pinned refusal/hold DTO returned by sessions start."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent_hub_host import session_control


class PinnedRefusalDiagnosticsTest(unittest.TestCase):
    def payload(self, status, ramping=0):
        rejected = [
            {'machine': 'tabo', 'engine': 'codex', 'account': 'codex-orin',
             'reason': 'codex_quota_gate_quota_check_failed'},
            {'machine': 'tabo', 'engine': 'codex', 'account': 'shared-codex',
             'reason': 'workspace_unavailable'},
        ]
        if ramping:
            for route in rejected:
                route['memory'] = {'ramping': [
                    {'id': str(i), 'reserve_mb': 512, 'state': 'starting'}
                    for i in range(ramping)
                ]}
        body = {'id': '11111111-1111-4111-8111-111111111111',
                'state': 'refused' if status == 400 else 'held',
                'reason': rejected[0]['reason'], 'rejected': rejected}
        failure = {'error': 'no_eligible_route', 'rejected': rejected}
        if status == 400:
            body.update(failure)
        else:
            body['hold'] = failure
        return body

    def diagnostic(self, status, body):
        error = urllib.error.HTTPError('https://hub.test/api/sessions', status,
                                       'Refused', {}, io.BytesIO(json.dumps(body).encode()))
        return session_control.diagnostic(session_control.http_error(error))

    def test_pinned_start_reports_reason_and_all_route_reasons(self):
        for status in (400, 409):
            with self.subTest(status=status):
                message = self.diagnostic(status, self.payload(status))
                self.assertIn('reason: codex_quota_gate_quota_check_failed', message)
                self.assertIn('routes:', message)
                self.assertIn('workspace_unavailable', message)

    def test_rich_held_response_keeps_diagnostics_beyond_four_kib(self):
        body = self.payload(409, ramping=60)
        self.assertGreater(len(json.dumps(body).encode()), 4096)
        message = self.diagnostic(409, body)
        self.assertIn('reason: codex_quota_gate_quota_check_failed', message)
        self.assertIn('routes:', message)
        self.assertIn('workspace_unavailable', message)

    def test_oversized_response_reports_safe_fixed_reason(self):
        body = self.payload(409, ramping=60)
        body['private_field'] = 'DO_NOT_ECHO_PRIVATE_SERVER_DATA'
        with patch.object(session_control, 'MAX_ERROR_RESPONSE', 512):
            message = self.diagnostic(409, body)
        self.assertIn('reason: response_too_large', message)
        self.assertNotIn('DO_NOT_ECHO_PRIVATE_SERVER_DATA', message)


if __name__ == '__main__':
    unittest.main()
