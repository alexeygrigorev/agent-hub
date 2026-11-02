"""Intake 5814e109 A7: Hub pushes credential generations; hosts sync on a new epoch and ack."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agent_hub_host import provisioning as p


def reset():
    p._epoch.update(applied=None, thread=None, failed_at=-1e9, seen=None)


class NoteEpoch(unittest.TestCase):
    def setUp(self):
        reset()

    def test_new_epoch_syncs_once_and_is_remembered(self):
        calls = []
        tick = lambda: calls.append(1) or {'status': 'synced'}
        self.assertTrue(p.note_epoch('100', tick, spawn=False))
        self.assertFalse(p.note_epoch('100', tick, spawn=False))
        self.assertEqual(len(calls), 1)
        self.assertTrue(p.note_epoch('101', tick, spawn=False))
        self.assertEqual(len(calls), 2)

    def test_failed_sync_retries_only_after_backoff(self):
        now = [1000.0]
        clock = lambda: now[0]
        held = lambda: {'status': 'held'}
        self.assertTrue(p.note_epoch('7', held, clock=clock, spawn=False))
        p._epoch['failed_at'] = now[0]
        self.assertFalse(p.note_epoch('7', held, clock=clock, spawn=False))
        now[0] += p.EPOCH_RETRY_S + 1
        self.assertTrue(p.note_epoch('7', lambda: {'status': 'synced'}, clock=clock, spawn=False))
        self.assertEqual(p._epoch['applied'], '7')

    def test_exception_in_sync_is_contained(self):
        def boom():
            raise RuntimeError('provider output with /private/path')
        self.assertTrue(p.note_epoch('9', boom, spawn=False))
        self.assertIsNone(p._epoch['applied'])

    def test_missing_or_bad_epoch_ignored(self):
        for bad in (None, '', 5, 'x' * 40):
            self.assertFalse(p.note_epoch(bad, lambda: {'status': 'synced'}, spawn=False))

    def test_threaded_sync_runs_in_background(self):
        done = []
        self.assertTrue(p.note_epoch('55', lambda: done.append(1) or {'status': 'synced'}))
        for _ in range(100):
            if done:
                break
            time.sleep(0.01)
        self.assertEqual(done, [1])


class AckAndStatus(unittest.TestCase):
    def setUp(self):
        p._last_sync_report.update(body=None, at=0.0)

    def test_report_carries_installed_generation(self):
        sent = []
        req = lambda method, path, body=None: sent.append((method, path, body))
        bundles = [{'label': 'hub-antigravity', 'revision': 'r1', 'credential_generation': 4},
                   {'label': 'no-cred', 'revision': 'r2'},
                   {'label': 'bad', 'revision': 'r3', 'credential_generation': True}]
        self.assertTrue(p.report_sync(req, 'synced', bundles))
        items = sent[0][2]['items']
        self.assertEqual(items[0]['credential_generation'], 4)
        self.assertNotIn('credential_generation', items[1])
        self.assertNotIn('credential_generation', items[2])

    def test_new_generation_is_reported_immediately_despite_dedupe(self):
        sent = []
        req = lambda *a: sent.append(a)
        b = lambda g: [{'label': 'a', 'revision': 'r', 'credential_generation': g}]
        self.assertTrue(p.report_sync(req, 'synced', b(1)))
        self.assertFalse(p.report_sync(req, 'synced', b(1)))
        self.assertTrue(p.report_sync(req, 'synced', b(2)))

    def test_status_shows_acked_vs_current(self):
        listing = {'sync_status': [{'machine_id': 'm1', 'machine': 'DESKTOP', 'online': True, 'host_state': 'synced', 'last_sync_at': 1,
                                    'accounts': [{'label': 'hub-antigravity', 'state': 'synced', 'credential_generation': 3, 'acked_generation': 2},
                                                 {'label': 'plain', 'state': 'synced'}]}]}
        host = p.sync_status(listing)['hosts'][0]
        self.assertEqual(host['credentials'], {'hub-antigravity': {'current': 3, 'acked': 2}})


class OfferPollWiring(unittest.TestCase):
    def test_poll_passes_epoch_to_provisioning(self):
        from agent_hub_host import offer_claims
        import threading
        disp = mock.MagicMock()
        disp.stop_requested = threading.Event()
        disp.settings = {}
        disp._acquire_offers_lock.return_value = None
        disp._fetch_offers.return_value = {'offers': [], 'engine_epoch': '123'}
        with mock.patch.object(p, 'note_epoch') as note, mock.patch('agent_hub_host.fleet.dispatch_concurrency', return_value=1):
            offer_claims.poll(disp)
        note.assert_called_once_with('123')


if __name__ == '__main__':
    unittest.main()
