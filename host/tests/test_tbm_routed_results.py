"""Routed outcomes survive host restart and reach the reward-writing task API."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.fleet import Dispatcher

OFFER = '11111111-2222-4333-8444-555555555555'
JOB = '22222222-3333-4444-8555-666666666666'
NATIVE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class RoutedResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / 'workspace'
        self.ws.mkdir()
        self.journal = Path(self.tmp.name) / 'journal'
        self.settings = {'subscriptions': [], 'launcher': {'argv': ['launcher'],
                         'root': str(self.ws), 'config_dir': str(self.ws / 'config')}}
        self.result = self.ws / '.local' / 'hub-results' / (OFFER + '.json')
        self.result.parent.mkdir(parents=True)
        managed = patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[])
        managed.start()
        self.addCleanup(managed.stop)
        self.request = Mock(return_value={'id': JOB, 'launch_id': OFFER, 'status': 'done'})
        self.runner = Mock(return_value={'state': 'completed-awaiting-review',
                                        'native_id': NATIVE, 'resources_reserved': False})
        self.d = self.dispatcher()
        self.d._record_running_launch(OFFER, 'hub-' + OFFER, self.ws, 'opencode', 'shared-go',
                                      NATIVE, self.result,
                                      {'tag': 'task-hub-' + OFFER, 'created_at_ms': 1700000000000},
                                      routing_task_id=JOB)
        self.result.write_text(json.dumps({'status': 'done', 'report': 'Verified real reset invariant.',
                                          'artifacts': [{'label': 'Useful evidence', 'text': 'Actual functions and tests.'}]}))

    def dispatcher(self):
        return Dispatcher(self.settings, self.request, self.runner, journal_dir=str(self.journal))

    def reconcile(self, dispatcher=None, retired=True):
        with patch('agent_hub_host.fleet.verify_native_retirement', return_value=(retired, {}, {})), \
             patch.object(Dispatcher, '_maybe_cleanup_worktree') as cleanup:
            result = (dispatcher or self.d).reconcile_launches()
        return result, cleanup

    def test_real_dispatch_journals_routed_task_before_returning_running(self):
        # This Codex execution is a distinct launch from the seeded OpenCode
        # task. Reusing a launch UUID across native identities is forbidden.
        offer = '33333333-4444-4555-8666-777777777777'
        native = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff'
        seeded_path = self.journal / 'fleet-launches' / (OFFER + '.json')
        seeded = seeded_path.read_bytes()
        settings = dict(self.settings, capabilities=[], workspaces={'demo': str(self.ws)}, subscriptions=[{
            'service': 'codex', 'account_label': 'shared-codex', 'engine': 'codex', 'profile': None,
            'admission_argv': ['maintained-gate'], 'launcher_provider': 'codex',
            'launcher_model': 'gpt-6.1-sol', 'launcher_effort': 'high'}])

        def runner(argv, **kwargs):
            if argv[0] == 'maintained-gate':
                from agent_hub_host.fleet import utcnow
                return {'launch_allowed': True, 'profile_supported': True,
                        'account_label': 'shared-codex', 'observed_at': utcnow(),
                        'model_supported': True, 'model': 'gpt-6.1-sol', 'effort': 'high'}
            if 'plan' in argv:
                return {'eligible': [{'provider': 'codex', 'model': 'gpt-6.1-sol', 'task_fit': 1.0}]}
            if 'run' in argv:
                return {'receipt': {'session_id': native, 'state': 'running', 'first_action_verified': True,
                                    'tag': 'task-hub-' + offer, 'workspace': str(self.ws),
                                    'engine': 'codex', 'created_at_ms': 1700000000000,
                                    'model': 'gpt-6.1-sol', 'model_verified': True}}
            return {}

        d = Dispatcher(settings, Mock(return_value={}), runner, journal_dir=str(self.journal))
        result = d.dispatch({'id': offer, 'engine': 'codex', 'account': 'shared-codex', 'profile': None,
                             'workspace': 'demo', 'prompt': 'Read committed source.', 'mode': 'headless',
                             'task_id': JOB, 'needs': [], 'selection': {'id': 'sol', 'model': 'gpt-6.1-sol', 'effort': 'high'}})
        self.assertEqual(result['state'], 'running')
        self.assertEqual(d._active_launches[offer]['routing_task_id'], JOB)
        self.assertEqual(self.dispatcher()._active_launches[offer]['routing_task_id'], JOB)

        self.assertEqual(seeded_path.read_bytes(), seeded)

    def test_restart_delivers_task_result_and_settles_once(self):
        restarted = self.dispatcher()
        self.assertEqual(restarted._active_launches[OFFER]['routing_task_id'], JOB)
        result, cleanup = self.reconcile(restarted)
        self.assertEqual(result, [{'id': OFFER, 'state': 'done', 'reconciled': True}])
        method, path, payload = self.request.call_args.args
        self.assertEqual((method, path), ('POST', '/api/fleet/tasks/' + JOB + '/result'))
        self.assertEqual(payload.pop('runtime'), {'exit_code': None, 'exit_signal': None, 'wall_time_ms': None,
            'log_tail': '', 'engine_outcome': None, 'observed_commits': [], 'dirty_paths': [], 'outcome': 'reported'})
        self.assertEqual(payload, {'status': 'done', 'report': 'Verified real reset invariant.',
                                  'confirmed_exit': True,
                                  'artifacts': [{'name': 'evidence-1', 'text': 'Useful evidence\nActual functions and tests.'}]})
        cleanup.assert_called_once()
        self.request.reset_mock()
        self.assertEqual(self.dispatcher().reconcile_launches(), [])
        self.request.assert_not_called()

    def test_live_native_or_unreleased_resource_never_reports_done(self):
        self.assertEqual(self.reconcile(retired=False)[0], [])
        self.request.assert_not_called()
        self.runner.return_value['resources_reserved'] = True
        self.assertEqual(self.reconcile()[0], [])
        self.request.assert_not_called()
        self.assertFalse(self.d._active_launches[OFFER]['settled'])

    def test_lost_ack_recovers_only_same_job_launch_and_status(self):
        self.request.side_effect = [TimeoutError(), {'id': JOB, 'launch_id': OFFER, 'status': 'done'}]
        self.assertEqual(self.reconcile()[0][0]['state'], 'done')
        self.assertEqual(self.request.call_args.args, ('GET', '/api/fleet/tasks/' + JOB))

    def test_mismatched_ack_keeps_restart_journal(self):
        self.request.side_effect = [TimeoutError(), {'id': JOB, 'launch_id': JOB, 'status': 'done'}]
        self.assertEqual(self.reconcile()[0], [])
        self.assertFalse(self.dispatcher()._active_launches[OFFER]['settled'])

    def test_missing_or_failed_native_result_never_earns_done(self):
        self.result.unlink()
        self.request.return_value['status'] = 'failed'
        self.assertEqual(self.reconcile()[0][0]['state'], 'failed')
        self.assertEqual(self.request.call_args.args[2]['status'], 'failed')

    def test_failed_native_overrides_worker_done(self):
        self.runner.return_value['state'] = 'failed'
        self.request.return_value['status'] = 'failed'
        self.assertEqual(self.reconcile()[0][0]['state'], 'failed')

    def test_human_wait_preserves_workspace_and_server_reservation(self):
        self.result.write_text(json.dumps({'status': 'waiting-on-human', 'report': 'Need actual operator input.'}))
        self.request.return_value['status'] = 'waiting-on-human'
        result, cleanup = self.reconcile()
        self.assertEqual(result[0]['state'], 'waiting-on-human')
        cleanup.assert_not_called()
        self.assertNotIn('state', self.request.call_args.args[2])

    def test_invalid_routing_identity_is_not_loaded(self):
        p = self.journal / 'fleet-launches' / (OFFER + '.json')
        d = json.loads(p.read_text())
        d['routing_task_id'] = '../another-job'
        p.write_text(json.dumps(d))
        self.assertNotIn(OFFER, self.dispatcher()._active_launches)
