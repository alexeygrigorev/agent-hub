"""Offline workflow ordering/correlation tests; never invokes a provider."""
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub_host import codex_reset_credit as native
from agent_hub_host import reset_credit_workflow as flow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.before = {'accountId': 'provider-a', 'observedAt': self.now, 'ordinaryUsageAllowed': False,
            'availableCount': 2, 'credits': [{'id': 'credit-a', 'expiresAt': self.now + 3600,
                'status': 'available', 'resetType': 'codexRateLimits'}],
            'windows': [{'usedPercent': 90, 'resetsAt': self.now + 86400}]}
        self.event = {'event_id': 'earned-reset:event-a', 'consumption_ready': True, 'decision': {
            'account': 'shared-codex', 'provider_account_id': 'provider-a', 'credit_id': 'credit-a',
            'expires_at_ms': (self.now + 3600) * 1000, 'captured_at_ms': self.now * 1000,
            'idempotency_key': 'server-key'}}
        self.calls = []

    def hub(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {'events': [copy.deepcopy(self.event)]} if method == 'GET' else copy.deepcopy(self.event)

    def receipt(self):
        after = copy.deepcopy(self.before)
        after.update(availableCount=1, credits=[])
        return {'accountId': 'provider-a', 'creditId': 'credit-a', 'idempotencyKey': 'server-key',
                'outcome': 'reset', 'before': self.before, 'after': after}

    def test_prepare_failure_cannot_reach_redemption(self):
        with patch.object(native, 'snapshot', return_value=self.before), patch.object(native, 'redeem') as redeem:
            def offline(*_):
                raise RuntimeError('transport')
            with self.assertRaises(RuntimeError):
                flow.prepare(unittest.mock.Mock(), offline, 'shared-codex', 'credit-a')
            redeem.assert_not_called()

    def test_prepare_requires_exact_hub_to_provider_mapping(self):
        self.event['decision']['provider_account_id'] = 'other'
        with patch.object(native, 'snapshot', return_value=self.before):
            with self.assertRaisesRegex(native.CreditError, 'identity_mismatch'):
                flow.prepare(unittest.mock.Mock(), self.hub, 'shared-codex', 'credit-a')

    def test_consume_fetches_decision_and_passes_remote_key_before_record(self):
        with patch.object(native, 'redeem', return_value=self.receipt()) as redeem:
            result = flow.consume(unittest.mock.Mock(), self.hub, self.event['event_id'], journal=Path('unused'))
        self.assertEqual(redeem.call_args.kwargs['idempotency_key'], 'server-key')
        self.assertEqual(self.calls[0][0], 'GET')
        self.assertEqual(self.calls[-1][2]['action'], 'receipt')
        self.assertEqual(result['event_id'], self.event['event_id'])

    def test_stale_decision_does_not_consume(self):
        self.event['decision']['captured_at_ms'] -= 301000
        with patch.object(native, 'redeem') as redeem:
            with self.assertRaisesRegex(native.CreditError, 'decision_expired'):
                flow.consume(unittest.mock.Mock(), self.hub, self.event['event_id'])
            redeem.assert_not_called()

    def test_current_hub_hold_prevents_provider_mutation(self):
        self.event['consumption_ready'] = False
        with patch.object(native, 'redeem') as redeem:
            with self.assertRaisesRegex(native.CreditError, 'not_ready'):
                flow.consume(unittest.mock.Mock(), self.hub, self.event['event_id'])
            redeem.assert_not_called()

    def test_receipt_mismatch_does_not_post_or_consume(self):
        receipt = self.receipt()
        receipt['creditId'] = 'wrong'
        with self.assertRaisesRegex(native.CreditError, 'identity_mismatch'):
            flow.record(self.hub, self.event['event_id'], receipt)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_record_after_hub_outage_never_repeats_provider_mutation(self):
        with patch.object(native, 'redeem') as redeem:
            flow.record(self.hub, self.event['event_id'], self.receipt())
            flow.record(self.hub, self.event['event_id'], self.receipt())
            redeem.assert_not_called()
        reports = [body['receipt'] for method, _, body in self.calls if method == 'POST']
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]['selected_credit_state'], 'absent')
        self.assertEqual(len(reports[0]['sha256']), 64)

    def test_noop_record_does_not_fabricate_after_quota(self):
        receipt = self.receipt()
        receipt.update(outcome='nothingToReset', after=None)
        flow.record(self.hub, self.event['event_id'], receipt)
        report = self.calls[-1][2]['receipt']
        self.assertIsNone(report['after_count'])
        self.assertIsNone(report['after_at_ms'])
        self.assertEqual(report['selected_credit_state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
