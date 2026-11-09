import unittest
from agent_hub_host import quota_publish as qp


class QuotaPublishTests(unittest.TestCase):
    def test_binding_window_is_lowest_remaining(self):
        entry = {'status': 'ok', 'windows': {
            '5h': {'percent_remaining': 100.0, 'reset_at': None},
            '7d': {'percent_remaining': 8.0, 'reset_at': '2026-11-12T12:58:00Z'},
            'monthly': {'percent_remaining': None, 'reset_at': None}}}
        ev = qp.evidence(entry, 1000)
        self.assertEqual(ev['remaining'], 8.0)
        self.assertEqual(ev['reset_at'], 1794488280000)
        self.assertEqual(ev['valid_until'], 1000 + qp.VALID_MS)

    def test_unmeasured_is_skipped_and_all_unmeasured_fails_loud(self):
        self.assertIsNone(qp.evidence({'status': 'error', 'windows': {}}, 1))
        with self.assertRaises(ValueError):
            qp.publish(quse={'claude': {'status': 'error', 'error': 'boom'}}, request=lambda *a: {}, now_ms=1)

    def test_publish_registers_new_account_disabled_then_puts(self):
        calls = []
        def req(method, path, body=None):
            calls.append((method, path, body)); return {}
        quse = {'claude': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 95.0, 'reset_at': None}}}}
        out = qp.publish(quse=quse, request=req, now_ms=5)
        self.assertEqual(out['published'], ['claude'])
        self.assertEqual([c[0] for c in calls], ['GET', 'PATCH', 'PUT'])
        self.assertFalse(calls[1][2]['enabled'])
        self.assertEqual(calls[2][2]['remaining'], 95.0)


if __name__ == '__main__':
    unittest.main()
