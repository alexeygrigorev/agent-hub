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

    def test_zai_central_account_is_published(self):
        calls = []
        def req(method, path, body=None):
            calls.append((method, path)); return {'account': {'id': 1}}
        quse = {'zai': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 76.0, 'reset_at': None}}}}
        self.assertEqual(qp.publish(quse=quse, request=req, now_ms=5)['published'], ['zai-central'])
        self.assertIn(('PUT', '/api/accounts/zai-central/api/account/quota'), calls)

    def test_unmeasured_is_skipped_and_all_unmeasured_fails_loud(self):
        self.assertIsNone(qp.evidence({'status': 'error', 'windows': {}}, 1))
        with self.assertRaises(ValueError):
            qp.publish(quse={'claude': {'status': 'error', 'error': 'boom'}}, request=lambda *a: {}, now_ms=1)

    def test_publish_registers_new_account_enabled_then_puts(self):
        calls = []
        def req(method, path, body=None):
            calls.append((method, path, body)); return {}
        quse = {'claude': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 95.0, 'reset_at': None}}}}
        out = qp.publish(quse=quse, request=req, now_ms=5)
        self.assertEqual(out['published'], ['claude'])
        self.assertEqual([c[0] for c in calls], ['GET', 'PATCH', 'PUT'])
        self.assertTrue(calls[1][2]['enabled'])
        self.assertEqual(calls[2][2]['remaining'], 95.0)

    def test_enable_flag_flips_existing_disabled_account(self):
        calls = []
        def req(method, path, body=None):
            calls.append(method)
            return {'account': {'enabled': 0, 'capacity': 8, 'min_remaining': 5}} if method == 'GET' else {}
        quse = {'claude': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 9.0, 'reset_at': None}}}}
        qp.publish(quse=quse, request=req, now_ms=5)
        self.assertEqual(calls, ['GET', 'PUT'])
        calls.clear(); qp.publish(quse=quse, request=req, now_ms=5, enable=True)
        self.assertEqual(calls, ['GET', 'PATCH', 'PUT'])

    def test_low_capacity_account_is_grown_without_enabling(self):
        calls = []
        def req(method, path, body=None):
            calls.append((method, body))
            return {'account': {'enabled': 0, 'capacity': 1, 'min_remaining': 5}} if method == 'GET' else {}
        quse = {'claude': {'status': 'ok', 'windows': {'7d': {'percent_remaining': 9.0, 'reset_at': None}}}}
        qp.publish(quse=quse, request=req, now_ms=5)
        patch = [b for m, b in calls if m == 'PATCH'][0]
        self.assertEqual((patch['capacity'], patch['enabled'], patch['min_remaining']), (qp.DEFAULT_CAPACITY, False, 5))


if __name__ == '__main__':
    unittest.main()
