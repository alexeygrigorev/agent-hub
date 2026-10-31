"""Per-host engine sync status (intake 5814e109 AC9).

Hosts tick every heartbeat; the report goes to Hub only when it changes (or on a
slow refresh), carries exception type names only, and never fails the sync.
"""
import unittest

from agent_hub_host import provisioning as p


B1 = {'label': 'shared-codex', 'revision': '11111111-1111-1111-1111-111111111111'}
B2 = {'label': 'hub-claude', 'revision': '22222222-2222-2222-2222-222222222222'}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SyncReportTest(unittest.TestCase):
    def setUp(self):
        p._last_sync_report.update(body=None, at=0.0)
        self.calls = []
        self.clock = Clock()

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))

    def test_reports_on_change_and_slow_refresh_only(self):
        self.assertTrue(p.report_sync(self.request, 'synced', [B1], clock=self.clock))
        self.assertEqual(self.calls[-1], ('POST', '/api/engines/sync-report',
                                          {'state': 'synced', 'items': [dict(B1, state='installed')]}))
        self.clock.t += 60
        self.assertFalse(p.report_sync(self.request, 'synced', [B1], clock=self.clock))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(p.report_sync(self.request, 'synced', [B1, B2], clock=self.clock))
        self.clock.t += p.SYNC_REPORT_REFRESH_S
        self.assertTrue(p.report_sync(self.request, 'synced', [B1, B2], clock=self.clock))
        self.assertEqual(len(self.calls), 3)

    def test_failure_carries_exception_type_only(self):
        p.report_sync(self.request, 'synced', [B1], [B2], {B2['label']: OSError('/secret/path: token=abc')}, clock=self.clock)
        items = self.calls[-1][2]['items']
        self.assertEqual(items[1], dict(B2, state='failed', error='OSError'))
        self.assertNotIn('secret', repr(self.calls))
        p.report_sync(self.request, 'held', clock=self.clock)
        self.assertEqual(self.calls[-1][2], {'state': 'held', 'items': []})

    def test_unreachable_hub_never_raises_and_retries_next_tick(self):
        def down(*_):
            raise OSError('down')
        self.assertFalse(p.report_sync(down, 'synced', [B1], clock=self.clock))
        self.assertTrue(p.report_sync(self.request, 'synced', [B1], clock=self.clock))

    def test_sync_reports_failures_even_when_every_install_fails(self):
        from unittest import mock
        bundles = [dict(B1, provider='codex', version='0.162.1', files={}, env={}, args=[]),
                   dict(B2, provider='claude', version='2.1.304', files={}, env={}, args=[])]

        def hub(method, path, payload=None):
            self.calls.append((method, path, payload))
            return {'engines': bundles, 'logins': []} if path == '/api/engines/sync' else {}

        def broken(b, request=None):
            raise (OSError if b['label'] == B1['label'] else PermissionError)('/private/path')
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as managed, mock.patch.dict(os.environ, {'AGENT_HUB_MANAGED_DIR': managed}), \
                mock.patch.object(p, 'install', broken), mock.patch.object(p, 'profiles'):
            with self.assertRaises(ValueError):
                p.sync(hub)
        report = [c for c in self.calls if c[1] == '/api/engines/sync-report'][-1][2]
        self.assertEqual(report['state'], 'held')
        self.assertEqual([(i['label'], i['state'], i['error']) for i in report['items']],
                         [(B1['label'], 'failed', 'OSError'), (B2['label'], 'failed', 'PermissionError')])
        self.assertNotIn('/private/path', repr(self.calls))

    def test_status_view_is_compact(self):
        listing = {'sync_status': [{'machine_id': 'm1', 'machine': 'RMTHZ', 'online': True, 'host_state': 'synced',
                                    'last_sync_at': 5, 'accounts': [{'label': 'a', 'state': 'synced'},
                                                                    {'label': 'b', 'state': 'failed', 'error': 'OSError'}]}]}
        self.assertEqual(p.sync_status(listing), {'hosts': [{'machine': 'RMTHZ', 'online': True, 'host_state': 'synced',
                                                             'last_sync_at': 5, 'accounts': {'a': 'synced', 'b': 'failed:OSError'}}]})


if __name__ == '__main__':
    unittest.main()
