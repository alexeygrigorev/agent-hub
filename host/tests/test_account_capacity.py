import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_hub_host.account_capacity import activity_ms, reconcile_unstarted, reconcile_retired, reconcile_launcher_retired, confirmed_empty


class AccountCapacityTests(unittest.TestCase):
    def test_headless_capacity_recovery_forwards_all_worker_statuses_and_runtime(self):
        from agent_hub_host.fleet import Dispatcher
        offer = '11111111-2222-4333-8444-555555555555'
        native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        for status in ['done', 'failed', 'waiting-on-human', 'stuck', None]:
            for journal_present in [False, True]:
                with self.subTest(status=status, journal_present=journal_present), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    settings = {'version': 1, 'subscriptions': [], 'workspaces': {'main': tmp},
                                'journal_dir': str(root / 'journal'),
                                'launcher': {'argv': ['launcher'], 'root': tmp, 'config_dir': str(root / 'launcher')}}
                    posts = []
                    def request(method, path, payload=None):
                        posts.append((method, path, payload))
                        return {'id': offer, 'state': payload['state']}
                    d = Dispatcher(settings, request=request, runner=Mock(return_value={
                        'state': 'completed-awaiting-review', 'native_id': native, 'resources_reserved': False}))
                    row = {'id': offer, 'native_id': native, 'engine': 'opencode', 'mode': 'headless',
                           'launcher_task_id': 'hub-' + offer,
                           'binding': {'native_id': native, 'tag': 'task-hub-' + offer,
                                       'workspace': tmp, 'native_created_at_ms': 1000}}
                    path = root / '.local/hub-results' / (offer + '.json')
                    path.parent.mkdir(parents=True)
                    if status:
                        path.write_text(json.dumps({'status': status, 'report': 'Real worker result hubt_synthetic-private-value'}))
                    if journal_present:
                        d._active_launches[offer] = dict(offer_id=offer, native_id=native,
                            task_id='hub-' + offer, tag='task-hub-' + offer, workspace=tmp,
                            incarnation=1000, engine='opencode', mode='headless', state='running',
                            settled=False, result_path='')  # repair an empty legacy path
                    other = '99999999-2222-4333-8444-555555555555'
                    d._active_launches[other] = dict(offer_id=other, native_id=native,
                        task_id='hub-' + other, tag='task-hub-' + other, workspace=tmp,
                        incarnation=1000, engine='opencode', mode='headless', state='running',
                        settled=False, result_path=str(root / '.local/hub-results' / (other + '.json')))
                    tomb = {'finished_at_ms': 2300, 'exit': {'code': 0, 'signal': None}}
                    with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, tomb, 'verified')), \
                         patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, tomb, 'verified')), \
                         patch.object(d, '_check_headless_health', return_value=None), \
                         patch.object(d, '_maybe_cleanup_worktree'):
                        self.assertTrue(reconcile_retired(d, row))
                    self.assertEqual(len(posts), 1)
                    payload = posts[0][2]
                    self.assertEqual(payload['report_status'], status or 'failed')
                    self.assertEqual(payload['runtime']['exit_code'], 0)
                    self.assertEqual(payload['runtime']['wall_time_ms'], 1300)
                    self.assertEqual(payload['runtime']['outcome'], 'reported' if status else 'failed-without-report')
                    self.assertNotIn('synthetic-private-value', json.dumps(payload))
                    self.assertTrue(d._active_launches[offer]['settled'])
                    self.assertEqual(d._active_launches[offer]['result_path'], str(path))
                    self.assertFalse(d._active_launches[other]['settled'])

    def test_headless_capacity_recovery_never_bypasses_live_launcher_reservation(self):
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            offer = '11111111-2222-4333-8444-555555555555'
            native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
            d = Dispatcher({'version': 1, 'subscriptions': [], 'workspaces': {'main': tmp},
                            'journal_dir': str(Path(tmp) / 'journal'),
                            'launcher': {'argv': ['launcher'], 'root': tmp, 'config_dir': tmp}},
                           request=Mock(), runner=Mock(return_value={
                               'state': 'running', 'native_id': native, 'resources_reserved': True}))
            row = {'id': offer, 'native_id': native, 'mode': 'headless', 'engine': 'opencode',
                   'binding': {'native_id': native, 'tag': 'task-hub-' + offer,
                               'workspace': tmp, 'native_created_at_ms': 1000}}
            d._active_launches[offer] = dict(offer_id=offer, native_id=native, task_id='hub-' + offer,
                tag='task-hub-' + offer, workspace=tmp, incarnation=1000, engine='opencode',
                mode='headless', state='running', settled=False, result_path='')
            with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {}, 'verified')), \
                 patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, {}, 'verified')), \
                 patch.object(d, '_check_headless_health', return_value=None):
                self.assertFalse(reconcile_retired(d, row))
            d.request.assert_not_called()
            self.assertFalse(d._active_launches[offer]['settled'])
            restarted = Dispatcher(d.settings)
            self.assertEqual(restarted._active_launches[offer]['result_path'],
                             str(Path(tmp) / '.local/hub-results' / (offer + '.json')))

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
