import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_hub_host.account_capacity import activity_ms, reconcile_unstarted, reconcile_retired, reconcile_launcher_retired, confirmed_empty


class AccountCapacityTests(unittest.TestCase):
    def test_failed_first_action_recovers_actual_native_instead_of_claiming_no_start(self):
        native = '11111111-1111-4111-8111-111111111111'
        binding = {'task_id': 'hub-launch', 'native_id': native, 'tag': 'task-hub-launch',
                   'workspace': '/repo', 'engine': 'codex', 'incarnation': 1234}
        dispatcher = Mock(settings={'launcher': {'argv': ['launcher'], 'config_dir': '/cfg', 'root': '/launcher'}})
        status = {'state': 'failed', 'payload': {'owner': 'hub-fleet'}, 'native_binding': binding}
        dispatcher.runner.side_effect = [status, {'native_id': native, 'resources_reserved': False, 'state': 'failed'}]
        dispatcher.request.return_value = {'state': 'failed'}
        with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {}, 'verified')):
            self.assertTrue(reconcile_launcher_retired(dispatcher, {'id': 'launch', 'engine': 'codex'}, Path('/repo')))
        payload = dispatcher.request.call_args.args[2]
        self.assertEqual(payload['native_id'], native)
        self.assertTrue(payload['confirmed_exit'])
        self.assertNotIn('reason', payload)
        dispatcher.request.reset_mock()
        dispatcher.runner.side_effect = [status]
        with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(False, None, 'live')):
            self.assertFalse(reconcile_launcher_retired(dispatcher, {'id': 'launch', 'engine': 'codex'}, Path('/repo')))
        dispatcher.request.assert_not_called()

    def test_lost_journal_releases_only_exact_retired_binding(self):
        dispatcher = Mock(settings={})
        row = {'id': 'launch', 'native_id': '11111111-1111-4111-8111-111111111111', 'engine': 'codex',
               'binding': {'native_id': '11111111-1111-4111-8111-111111111111', 'tag': 'task-hub-launch',
                           'workspace': '/repo', 'native_created_at_ms': 1234}}
        dispatcher.request.return_value = {'state': 'exited'}
        with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(False, None, 'unknown')):
            self.assertFalse(reconcile_retired(dispatcher, row))
            dispatcher.request.assert_not_called()
        with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {}, 'verified')):
            self.assertTrue(reconcile_retired(dispatcher, row, dry_run=True))
            dispatcher.request.assert_not_called()
            self.assertTrue(reconcile_retired(dispatcher, row))
        self.assertTrue(dispatcher.request.call_args.args[2]['confirmed_exit'])

    def test_deleted_marker_normalization_never_confers_exit_credit(self):
        from agent_hub_host.fleet import session_metadata
        row = {'id': 'native', 'tag': 'task', 'engine': 'codex', 'workspace': '/repo (deleted)',
               'cwd': '/repo (deleted)', 'phase': 'running', 'containment_empty': True, 'worker_pid': 123}
        settings = {'subscriptions': []}
        with patch('os.kill', side_effect=ProcessLookupError):
            result = session_metadata([row], settings)[0]
            self.assertEqual((result['phase'], result['workspace'], result['cwd']), ('exited', '/repo', '/repo'))
            self.assertTrue(result['confirmed_exit'])
        with patch('os.kill', return_value=None):
            result = session_metadata([row], settings)[0]
            self.assertEqual((result['phase'], result['workspace'], result['cwd']), ('running', '/repo', '/repo'))
            self.assertFalse(result['confirmed_exit'])

    def test_empty_containment_with_dead_pids_proves_exit_and_unknown_does_not(self):
        row = {'containment_empty': True, 'worker_pid': 123, 'workload_pid': 456}
        with patch('os.kill', side_effect=ProcessLookupError):
            self.assertTrue(confirmed_empty(row))
            self.assertFalse(confirmed_empty({**row, 'containment_empty': None}))
        with patch('os.kill', return_value=None):
            self.assertFalse(confirmed_empty(row))
        with patch('os.kill', side_effect=PermissionError):
            self.assertFalse(confirmed_empty(row))

    def test_observation_does_not_refresh_real_progress(self):
        self.assertEqual(activity_ms({'reported_state_event_ms': 100, 'last_activity_ms': 900, 'updated_at_ms': 999}), 100)
        self.assertEqual(activity_ms({'last_activity_ms': 900, 'updated_at_ms': 999}), 900)
        self.assertIsNone(activity_ms({'updated_at_ms': 999}))

    def test_legacy_failure_requires_fence_and_dry_run_has_no_effect(self):
        with tempfile.TemporaryDirectory() as root:
            diagnostic = {'reason': 'launcher_command_failed', 'stage': 'dispatch_headless'}
            dispatcher = Mock(settings={'workspaces': {'demo': root}, 'subscriptions': [], 'launcher': {}})
            dispatcher.request.return_value = {'launches': [{'id': 'launch', 'workspace': 'demo', 'diagnostic': diagnostic}]}
            with patch('agent_hub_host.launcher_recovery.settle_unstarted', return_value=False) as fence:
                self.assertEqual(reconcile_unstarted(dispatcher, dry_run=True), [{'id': 'launch', 'state': 'probe_required'}])
                fence.assert_not_called()
                reconcile_unstarted(dispatcher)
                fence.assert_called_once_with({}, 'hub-launch', Path(root), Path(root), diagnostic)
                self.assertEqual(dispatcher.request.call_count, 2)
            dispatcher.request.side_effect = [{'launches': [{'id': 'launch', 'workspace': 'demo', 'diagnostic': diagnostic}]}, {'id': 'launch', 'state': 'failed'}]
            with patch('agent_hub_host.launcher_recovery.settle_unstarted', return_value=True):
                self.assertEqual(reconcile_unstarted(dispatcher), [{'id': 'launch', 'state': 'failed'}])
            self.assertTrue(dispatcher.request.call_args.args[2]['confirmed_exit'])
