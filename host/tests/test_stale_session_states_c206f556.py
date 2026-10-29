"""Intake c206f556: stale Hub session states and transient stop/register 500s.

A host whose aplexer retained >500 dead session records had every native
census refused (400 invalid_sessions), so Hub never bound new starts
(offered forever), never saw exits (running forever) and never refreshed
progress. A dead aplexer worker also leaves containment_empty stale.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import api, intake, session_control, worktree_cli
from agent_hub_host.account_capacity import confirmed_empty, kernel_containment_empty
from agent_hub_host.fleet import CATALOG_LIMIT, Dispatcher, bounded_catalog, session_metadata

BOOT = 'edbec548-453f-4111-b38e-e7c16d12aa93'


def native(i, state, *, ms=1_000_000, alive=True):
    return {'id': f'00000000-0000-4000-8000-{i:012d}', 'tag': f'hub-{i}', 'engine': 'codex', 'profile': None,
            'workspace': f'/home/u/git/repo-wt-{i}', 'state': state, 'phase': 'running',
            'created_at_ms': ms, 'last_activity_ms': ms, 'worker_alive': alive}


class BoundedCatalogTests(unittest.TestCase):
    def test_oversized_host_keeps_every_live_native_within_hub_limit(self):
        # Production shape 2026-10-29: 281 broken + 223 exited + 2 failed
        # history rows listed before the live sessions.
        rows = ([native(i, 'broken', ms=1_000 + i, alive=False) for i in range(281)]
                + [native(300 + i, 'exited', ms=2_000 + i, alive=False) for i in range(223)]
                + [native(600 + i, 'failed', ms=3_000 + i, alive=False) for i in range(2)]
                + [native(900 + i, 'running', ms=5_000 + i) for i in range(9)])
        kept = bounded_catalog({'sessions': rows})
        self.assertEqual(len(kept), CATALOG_LIMIT)
        ids = {row['id'] for row in kept}
        self.assertTrue(all(native(900 + i, 'running')['id'] in ids for i in range(9)))
        self.assertEqual([row['state'] for row in kept[:9]], ['running'] * 9)
        # Non-terminal history outranks terminal; the oldest terminal rows drop.
        self.assertTrue(all(native(i, 'broken')['id'] in ids for i in range(281)))
        self.assertNotIn(native(300, 'exited')['id'], ids)
        self.assertEqual(len(session_metadata(kept, {'subscriptions': []}, collect_tokens=False)), CATALOG_LIMIT)

    def test_small_catalog_is_unchanged(self):
        rows = [native(1, 'exited'), native(2, 'running')]
        self.assertEqual(bounded_catalog(rows), rows)

    def test_census_put_never_exceeds_hub_limit_and_still_binds_new_start(self):
        rows = [native(i, 'broken', ms=1_000 + i, alive=False) for i in range(520)]
        fresh = native(999, 'running', ms=9_000)
        fresh.update(tag='hub-988b8d49-cacf-4a5e-900c-a214a6402808', id='9a6f3c1e-0000-4000-8000-000000000999')
        rows.append(fresh)
        dispatcher = Dispatcher.__new__(Dispatcher)
        dispatcher.settings = {'subscriptions': [], 'aplexer_bin': 'aplexer'}
        dispatcher._catalog_enrichment = {}
        dispatcher.runner = Mock(return_value={'sessions': rows})
        calls = []

        def request(method, path, payload=None):
            calls.append((method, path, payload))
            if path == '/api/sessions/host-bindings':
                return {'sessions': [{'id': '15acde15-2852-434e-9bdf-123c883e311c', 'engine': 'codex',
                                      'fleet_launch_id': '988b8d49-cacf-4a5e-900c-a214a6402808'}]}
            return {}
        dispatcher.request = request
        with patch('agent_hub_host.worktree_guard.protect_adoption', return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            dispatcher._report_native_bindings()
        put = [c for c in calls if c[1] == '/api/fleet/sessions'][0][2]['sessions']
        self.assertLessEqual(len(put), CATALOG_LIMIT)
        self.assertIn(fresh['id'], {row['native_id'] for row in put})
        binding = [c for c in calls if c[1].endswith('/binding')]
        self.assertEqual(binding[0][2]['native']['native_id'], fresh['id'])


class KernelContainmentReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/cgroup/'
        self.boot = os.path.join(self.tmp.name, 'boot_id')
        with open(self.boot, 'w') as handle:
            handle.write(BOOT + '\n')
        self.scope = self.root + 'app.slice/aplexer-workload-d5511769.scope'
        os.makedirs(self.scope)

    def row(self, **extra):
        return {'state': 'broken', 'worker_alive': False, 'containment_empty': False,
                'containment_cgroup': self.scope, 'containment_cgroup_identity': {'boot_id': BOOT},
                'worker_pid': 3549757, 'workload_pid': 3549765, **extra}

    def events(self, populated):
        with open(self.scope + '/cgroup.events', 'w') as handle:
            handle.write(f'populated {populated}\nfrozen 0\n')

    def receipt(self, row):
        return kernel_containment_empty(row, cgroup_root=self.root, boot_id_path=self.boot)

    def test_dead_worker_with_empty_same_boot_scope_is_exit_proof(self):
        self.events(0)
        self.assertTrue(self.receipt(self.row()))
        self.events(1)
        self.assertFalse(self.receipt(self.row()))

    def test_live_worker_report_stays_authoritative(self):
        self.events(0)
        self.assertFalse(self.receipt(self.row(worker_alive=True)))
        self.assertFalse(self.receipt(self.row(worker_alive=None)))

    def test_other_boot_or_foreign_path_is_not_proof(self):
        self.events(0)
        self.assertFalse(self.receipt(self.row(containment_cgroup_identity={'boot_id': 'other'})))
        self.assertFalse(self.receipt(self.row(containment_cgroup_identity=None)))
        self.assertFalse(self.receipt(self.row(containment_cgroup='/tmp/aplexer-workload-x.scope')))
        self.assertFalse(self.receipt(self.row(containment_cgroup=self.root + 'app.slice/user.slice')))

    def test_scope_removed_on_this_boot_is_proof(self):
        os.rmdir(self.scope)
        self.assertTrue(self.receipt(self.row()))

    def test_confirmed_empty_still_requires_dead_pids(self):
        self.events(0)
        row = self.row()
        with patch('agent_hub_host.account_capacity.kernel_containment_empty', return_value=True):
            with patch('os.kill', side_effect=ProcessLookupError):
                self.assertTrue(confirmed_empty(row))
            with patch('os.kill', return_value=None):
                self.assertFalse(confirmed_empty(row))


class KeyedReplayTests(unittest.TestCase):
    def test_replays_only_transient_failures(self):
        attempts = []

        def call():
            attempts.append(1)
            if len(attempts) < 3:
                raise session_control.SessionHTTPError(500, 'request_failed')
            return {'state': 'recorded'}
        sleep = Mock()
        self.assertEqual(api.replay_keyed(call, session_control.hub_unavailable, sleep=sleep), {'state': 'recorded'})
        self.assertEqual(len(attempts), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])

    def test_application_errors_and_exhaustion_raise(self):
        refused = session_control.SessionHTTPError(409, 'session_not_stoppable')
        with self.assertRaises(session_control.SessionHTTPError):
            api.replay_keyed(Mock(side_effect=refused), session_control.hub_unavailable, sleep=Mock())
        app500 = session_control.SessionHTTPError(500, 'task_capacity')
        self.assertFalse(session_control.hub_unavailable(app500))
        call = Mock(side_effect=session_control.SessionTransportError('timed out'))
        with self.assertRaises(session_control.SessionTransportError):
            api.replay_keyed(call, session_control.hub_unavailable, delays=(1, 1), sleep=Mock())
        self.assertEqual(call.call_count, 3)

    def test_stop_replays_same_key_through_hub_blip(self):
        failure = urllib.error.HTTPError('https://hub.test/api/sessions/x/stop', 500, 'err', {},
                                         io.BytesIO(b'Internal Server Error'))
        bodies = []

        def safe_http_json(req, timeout=10.0):
            bodies.append(json.loads(req.data))
            if len(bodies) == 1:
                raise failure
            return {'command_id': 'c1', 'state': 'recorded'}
        import argparse
        root = argparse.ArgumentParser()
        session_control.add_commands(root.add_subparsers(dest='cmd', required=True))
        args = root.parse_args(['sessions', 'stop', '0d5af39a-f8d1-484e-8056-ca95754b43ad', '--key', 'stop-k1', '--no-wait'])
        with patch.object(session_control.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.dict(os.environ, {'HUB_TOKEN': 'fixture-env-token'}), \
                patch.object(session_control.api, 'safe_http_json', side_effect=safe_http_json), \
                patch('agent_hub_host.api.KEYED_REPLAY_DELAYS', (0, 0, 0, 0, 0)), contextlib.redirect_stderr(io.StringIO()) as err:
            out = session_control.execute(args)
        self.assertEqual(out['command_id'], 'c1')
        self.assertEqual(bodies, [{'key': 'stop-k1'}, {'key': 'stop-k1'}])
        self.assertIn('replaying the same key', err.getvalue())

    def test_wait_budget_bounds_replay_and_capture_is_single_shot(self):
        token = session_control._request_deadline.set(__import__('time').monotonic() + 10)
        try:
            self.assertEqual(session_control.replay_delays(), (2, 4))
        finally:
            session_control._request_deadline.reset(token)
        self.assertEqual(session_control.replay_delays(), api.KEYED_REPLAY_DELAYS)
        import argparse
        root = argparse.ArgumentParser()
        session_control.add_commands(root.add_subparsers(dest='cmd', required=True))
        args = root.parse_args(['sessions', 'capture', '0d5af39a-f8d1-484e-8056-ca95754b43ad', '--key', 'cap-k1'])
        call = Mock(side_effect=session_control.SessionTransportError('offline'))
        with patch.object(session_control, 'request', call), patch('agent_hub_host.api.KEYED_REPLAY_DELAYS', (0, 0, 0, 0, 0)):
            with self.assertRaises(ValueError):
                session_control.execute(args)
        self.assertEqual(call.call_count, 1)

    def test_worktree_register_replays_same_key_through_hub_blip(self):
        calls = []

        def request(method, path, payload=None):
            calls.append(payload)
            if len(calls) == 1:
                raise intake.IntakeHTTPError(500, 'request_failed', payload['key'])
            return {'task_id': 't', 'worktree': {'path': payload['worktree']['path']}}
        args = SimpleNamespace(worktree_cmd='register', intake='786d94c0-d1c4-490c-b482-cd73f6103e9b',
                               key='wt-k1', path='/home/u/git/agent-hub-wt-tbm-driver-r4',
                               machine='DEFLf4-rahKf9wE4TQAZfA', branch=None, session=None)
        with patch.object(intake, 'request', side_effect=request), patch('agent_hub_host.api.KEYED_REPLAY_DELAYS', (0, 0, 0, 0, 0)), \
                contextlib.redirect_stderr(io.StringIO()):
            out = worktree_cli.execute(args)
        self.assertEqual(out['worktree']['path'], '/home/u/git/agent-hub-wt-tbm-driver-r4')
        self.assertEqual([c['key'] for c in calls], ['wt-k1', 'wt-k1'])
        refused = intake.IntakeHTTPError(409, 'worktree_owned_by_other_intake')
        self.assertFalse(worktree_cli.hub_unavailable(refused))


if __name__ == '__main__':
    unittest.main()
