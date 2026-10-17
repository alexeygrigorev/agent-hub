"""M13 collector/admission contract: native units, collector_version, window error.

Offline fixtures only: no subprocess, no network, no real credentials.
"""
import argparse
import datetime
import json
import unittest

from agent_hub_host import fleet
from agent_hub_host import quota_snapshot
from agent_hub_host import quota as quota_cli


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class CollectorNativeTests(unittest.TestCase):
    def test_collector_stamps_version_and_passes_native_fields(self):
        now = utcnow()
        raw = {'codex': {'status': 'ok', 'error': None,
                         'observed_at': quota_snapshot.format_iso_utc(now),
                         'details': {'limit_reached': False},
                         'windows': {'7d': {'percent_remaining': 78.5,
                                            'reset_at': '2026-10-20T00:00:00Z',
                                            'present': True, 'rolling': False,
                                            'credits_remaining': 12.5, 'unit': 'credits',
                                            'profile': 'muse-spark-1.3'}}}}
        snap = quota_snapshot.create_quota_snapshot(raw, 'mach1', now)
        self.assertEqual(snap['schema_version'], '1.1')
        self.assertEqual(snap['collector_version'], 'quota_snapshot/1.1')
        win = snap['routes']['codex']['windows']['7d']
        self.assertEqual(win['credits_remaining'], 12.5)
        self.assertEqual(win['unit'], 'credits')
        self.assertEqual(win['profile'], 'muse-spark-1.3')
        self.assertIsNone(win['spend_used'])

    def test_live_quse_shape_stays_percent_only_without_synthesis(self):
        now = utcnow()
        raw = {'go': {'status': 'ok', 'error': None,
                      'observed_at': quota_snapshot.format_iso_utc(now),
                      'details': {'limit_reached': False, 'max_used_percent': 38.0},
                      'windows': {'5h': {'percent_remaining': 91.0,
                                         'reset_at': '2026-10-17T16:21:20Z',
                                         'rolling': True}}}}
        snap = quota_snapshot.create_quota_snapshot(raw, 'mach1', now)
        win = snap['routes']['go']['windows']['5h']
        self.assertEqual(win['percent_remaining'], 91.0)
        # Unknown extra detail keys are dropped, never converted into money.
        self.assertNotIn('max_used_percent', snap['routes']['go']['details'])
        for key in ('credits_remaining', 'spend_used', 'spend_limit', 'unit', 'profile'):
            self.assertIsNone(win[key])
        details = snap['routes']['go']['details']
        for key in ('credits_remaining', 'spend_used', 'spend_limit', 'unit', 'profile'):
            self.assertIsNone(details[key])

    def test_invalid_natives_become_unknown_and_secrets_dropped(self):
        now = utcnow()
        raw = {'codex': {'status': 'ok', 'error': None,
                         'observed_at': quota_snapshot.format_iso_utc(now),
                         'details': {'limit_reached': False, 'access_token': 'secret',
                                     'email': 'someone@example.com'},
                         'windows': {'7d': {'percent_remaining': 80.0,
                                            'reset_at': '2026-10-20T00:00:00Z',
                                            'present': True, 'rolling': False,
                                            'credits_remaining': 'lots',
                                            'spend_used': float('nan'),
                                            'unit': 'dollars',
                                            'profile': 'has spaces'}}}}
        snap = quota_snapshot.create_quota_snapshot(raw, 'mach1', now)
        win = snap['routes']['codex']['windows']['7d']
        self.assertEqual(win['percent_remaining'], 80.0)
        for key in ('credits_remaining', 'spend_used', 'unit', 'profile'):
            self.assertIsNone(win[key])
        dumped = json.dumps(snap)
        self.assertNotIn('secret', dumped)
        self.assertNotIn('someone@example.com', dumped)

    def test_absent_window_carries_no_natives(self):
        now = utcnow()
        raw = {'codex': {'status': 'ok', 'error': None,
                         'observed_at': quota_snapshot.format_iso_utc(now),
                         'details': {'limit_reached': False},
                         'windows': {'5h': {'percent_remaining': None, 'reset_at': None,
                                            'present': False, 'rolling': True,
                                            'credits_remaining': 3.0}}}}
        snap = quota_snapshot.create_quota_snapshot(raw, 'mach1', now)
        win = snap['routes']['codex']['windows']['5h']
        self.assertFalse(win['present'])
        self.assertIsNone(win['credits_remaining'])

    def test_snapshot_size_bound_holds_with_natives(self):
        now = utcnow()
        raw = {prov: {'status': 'ok', 'error': None,
                      'observed_at': quota_snapshot.format_iso_utc(now),
                      'details': {'limit_reached': False, 'spend_used': 10.0,
                                  'spend_limit': 100.0, 'unit': 'usd_cents',
                                  'profile': 'muse-spark-1.3'},
                      'windows': {'w%d' % i: {'percent_remaining': 50.0,
                                             'reset_at': '2026-10-20T00:00:00Z',
                                             'present': True, 'rolling': False,
                                             'credits_remaining': 1.0, 'unit': 'credits'}
                                  for i in range(8)}}
               for prov in quota_snapshot.ALLOWED_PROVIDERS}
        snap = quota_snapshot.create_quota_snapshot(raw, 'mach1', now)
        self.assertLessEqual(len(json.dumps(snap).encode('utf-8')), quota_snapshot.MAX_SNAPSHOT_BYTES)


class FleetMetadataTests(unittest.TestCase):
    def test_quota_metadata_preserves_error_natives_and_collector_version(self):
        now = utcnow()
        raw = {'codex': {'status': 'error', 'error': 'unavailable',
                         'observed_at': quota_snapshot.format_iso_utc(now),
                         'details': {'limit_reached': False},
                         'windows': {'5h': {'percent_remaining': None, 'reset_at': None,
                                            'present': False, 'rolling': False,
                                            'error': 'unavailable'}}},
               'collector_version': 'quota_snapshot/1.1'}
        meta = fleet.quota_metadata(raw, 'codex')
        self.assertEqual(meta['windows']['5h']['error'], 'unavailable')
        self.assertEqual(meta['collector_version'], 'quota_snapshot/1.1')

    def test_quota_metadata_drops_unknown_and_rejects_bad_collector_version(self):
        now = utcnow()
        raw = {'codex': {'status': 'ok', 'error': None,
                         'observed_at': quota_snapshot.format_iso_utc(now),
                         'details': {'limit_reached': False, 'cookie': 'secret'},
                         'windows': {'7d': {'percent_remaining': 80.0,
                                            'reset_at': '2026-10-20T00:00:00Z',
                                            'present': True, 'rolling': False,
                                            'raw_stdout': 'secret'}}},
               'collector_version': 'evil; rm'}
        meta = fleet.quota_metadata(raw, 'codex')
        self.assertNotIn('cookie', meta['details'])
        self.assertNotIn('raw_stdout', meta['windows']['7d'])
        self.assertIsNone(meta['collector_version'])


class QuotaCliTests(unittest.TestCase):
    def test_execute_builds_filtered_fleet_query(self):
        calls = []
        import agent_hub_host.bus as bus
        real = bus._do_request
        bus._do_request = lambda method, path: calls.append((method, path)) or {'pools': []}
        try:
            args = argparse.Namespace(host='m1', provider='codex', engine='codex',
                                      account='main', json=True)
            quota_cli.execute(args)
        finally:
            bus._do_request = real
        self.assertEqual(len(calls), 1)
        method, path = calls[0]
        self.assertEqual(method, 'GET')
        self.assertIn('view=fleet', path)
        self.assertIn('host_id=m1', path)
        self.assertIn('provider=codex', path)

    def test_execute_rejects_bad_filters_locally(self):
        with self.assertRaises(ValueError):
            quota_cli.fetch_quota(engine='bad engine!')
        with self.assertRaises(ValueError):
            quota_cli.fetch_quota(provider='openai')

    def test_render_text_never_zero_fills_unknown(self):
        text = quota_cli.render_text({'pools': [
            {'provider': 'codex', 'account': 'main', 'state': 'unknown',
             'remaining_percent': None, 'observed_at': None, 'observed_by_hosts': []}],
            'coverage': {'fresh': [], 'stale': [], 'missing': ['m1'], 'machines_total': 1}})
        self.assertIn('unknown', text)
        self.assertNotIn('0%', text)


if __name__ == '__main__':
    unittest.main()
