"""hub alerts CLI wiring: onward-relay receipts (intake 1429bf88 AC2-AC3)."""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class AlertReceiptsCli(unittest.TestCase):
    def _run(self, argv, response=None):
        from agent_hub_host import cli, session_control
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return response if response is not None else {}

        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['hub'] + argv), \
                patch.object(session_control, 'request', side_effect=request), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit:
                pass
        return calls, out.getvalue(), err.getvalue()

    def test_receipts_view_hits_bounded_operator_endpoint(self):
        response = {'alerts': [{'id': 7, 'state': 'stalled', 'relay_state': 'unconfirmed',
                                'subject_kind': 'session', 'subject': 'host/shell',
                                'inbox': [{'read_at': '2026-11-04T10:00:00.000Z', 'acked_at': None}]}],
                    'unconfirmed': 1}
        calls, out, _ = self._run(['alerts', 'receipts', '--limit', '50'], response)
        self.assertEqual(calls[0][0:2], ('GET', '/api/alert-receipts?limit=50'))
        self.assertIn('unconfirmed', out)
        self.assertIn('7', out)
        self.assertIn('session:host/shell', out)
        # The table view summarizes read/ack without inventing relay delivery.
        self.assertIn('relay_ref', out.splitlines()[0])
        calls, out, _ = self._run(['alerts', 'receipts', '--json'], response)
        self.assertEqual(calls[0][1], '/api/alert-receipts')
        self.assertIn('"unconfirmed": 1', out)

    def test_relay_reports_forward_receipt_id_only(self):
        calls, _, _ = self._run(['alerts', 'relay', '7', '--ref', 'fwd-abc123', '--at', '2026-11-04T10:00:00Z'])
        self.assertEqual(calls[0], ('POST', '/api/alert-receipts/7/relay',
                                     {'relay_ref': 'fwd-abc123', 'relayed_at': '2026-11-04T10:00:00Z'}))

    def test_relay_requires_ref_and_rejects_bad_id(self):
        calls, _, err = self._run(['alerts', 'relay', '7'])
        self.assertEqual(calls, [])
        self.assertIn('--ref', err)
        calls, _, _ = self._run(['alerts', 'relay', 'not-an-id', '--ref', 'x'])
        self.assertEqual(calls, [])
