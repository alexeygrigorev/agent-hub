import unittest
import datetime as dt
from agent_hub_host import quota_publish as qp


class QuotaPublishTests(unittest.TestCase):
    def test_invalid_windows_are_not_partially_published(self):
        for invalid in (True, '42', float('nan'), float('inf'), -1, 101, 10**1000):
            with self.subTest(invalid=invalid):
                self.assertIsNone(qp.evidence({'status': 'ok', 'windows': {
                    'valid': {'percent_remaining': 80}, 'invalid': {'percent_remaining': invalid}}}, 1))
        for windows in ([], 'invalid', {'window': None}, {'window': 12}):
            with self.subTest(windows=windows):
                self.assertIsNone(qp.evidence({'status': 'ok', 'windows': windows}, 1))

    def test_stale_or_failed_observations_are_not_retimestamped(self):
        for fields in ({'status': 'stale'}, {'status': 'error'}, {'status': 'ok', 'error': 'failed refresh'}):
            entry = {'status': 'ok', 'windows': {'7d': {'percent_remaining': 80}}, **fields}
            self.assertIsNone(qp.evidence(entry, 1000))

    def test_reset_must_be_aware_future_and_bounds_validity(self):
        now = 1794340000000
        def entry(reset):
            return {'status': 'ok', 'windows': {'7d': {'percent_remaining': 80, 'reset_at': reset}}}
        for reset in ('invalid', '2026-11-10T19:46:40', '2020-01-01T00:00:00Z', 123):
            with self.subTest(reset=reset):
                self.assertIsNone(qp.evidence(entry(reset), now))
        reset = dt.datetime.fromtimestamp((now+60000)/1000, dt.timezone.utc).isoformat()
        self.assertEqual(qp.evidence(entry(reset), now)['valid_until'], now+60000)

    def test_limit_reached_is_never_published_as_available_headroom(self):
        ev = qp.evidence({'status': 'ok', 'details': {'limit_reached': True},
                          'windows': {'7d': {'percent_remaining': 80}}}, 1000)
        self.assertEqual(ev['remaining'], 0)
        for windows in (None, {}, {'7d': {'percent_remaining': None}}):
            with self.subTest(windows=windows):
                ev = qp.evidence({'status': 'ok', 'details': {'limit_reached': True}, 'windows': windows}, 1000)
                self.assertEqual(ev['remaining'], 0)
                self.assertEqual(ev['valid_until'], 1000+qp.VALID_MS)
        self.assertIsNone(qp.evidence({'status': 'stale', 'details': {'limit_reached': True}}, 1000))

    def test_partial_publish_keeps_skipped_errors_private_and_includes_gemini(self):
        calls = []
        data = {'claude': {'status': 'stale', 'error': 'PRIVATE TOKEN'},
                'codex': {'status': 'ok', 'windows': {'7d': {'percent_remaining': float('nan')}}},
                'gemini': {'status': 'ok', 'windows': {'5h': {'percent_remaining': 60}}}}
        def request(method, path, body=None):
            calls.append((method, path, body))
            return {'account': {'enabled': 1, 'capacity': 8}}
        result = qp.publish(quse=data, request=request, now_ms=1000)
        self.assertEqual(result['published'], ['gemini'])
        self.assertEqual(result['skipped']['claude'], 'quota_stale')
        self.assertNotIn('PRIVATE TOKEN', str(result))
        self.assertTrue(all('/gemini/' in path for _,path,_ in calls))

    def test_invalid_snapshot_clock_and_shape_fail_before_requests(self):
        for data, now in (([], 1), ({}, True), ({}, -1), ({}, 0.5)):
            with self.subTest(data=data, now=now), self.assertRaises(ValueError):
                qp.publish(quse=data, now_ms=now, request=lambda *a: self.fail('no request'))

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
