import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_hub_host.account_capacity import activity_ms, reconcile_unstarted, reconcile_retired, reconcile_launcher_retired, confirmed_empty


class AccountCapacityTests(unittest.TestCase):
    def _real_failed_dispatch(self, tmp, cwd=None):
        import datetime
        from agent_hub_host.fleet import Dispatcher
        offer_id = '11111111-2222-4333-8444-555555555555'
        sub = {'service': 'go', 'account_label': 'muse', 'engine': 'opencode',
               'launcher_provider': 'opencode', 'launcher_model': 'opencode-go/muse-spark-1.3-contributor',
               'admission_argv': ['gate']}
        settings = {'version': 1, 'subscriptions': [sub], 'workspaces': {'main': tmp},
                    'journal_dir': str(Path(tmp) / 'journal'),
                    'launcher': {'argv': ['launcher'], 'root': tmp, 'config_dir': str(Path(tmp) / 'launcher')}}
        def runner(argv, **kwargs):
            if argv[0] == 'gate':
                return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'muse',
                        'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            if 'plan' in argv:
                return {'eligible': [{'provider': 'opencode', 'model': sub['launcher_model'], 'task_fit': 1.0}]}
            if 'run' in argv:
                raise RuntimeError('first-action handshake failed')
            return {}
        d = Dispatcher(settings, runner=runner, request=Mock(return_value={}))
        offer = {'id': offer_id, 'engine': 'opencode', 'account': 'muse',
                 'mode': 'headless', 'workspace': 'main', 'prompt': 'inspect one source file'}
        if cwd is not None:
            offer['cwd'] = cwd
            (Path(tmp) / cwd).mkdir(parents=True, exist_ok=True)
        with patch('agent_hub_host.launcher_recovery.settle_unstarted', return_value=False), \
             patch.object(d, '_check_headless_health', return_value=None):
            self.assertEqual(d.dispatch(offer)['state'], 'uncertain')
        entry = Path(settings['journal_dir']) / 'fleet-launches' / (offer_id + '.json')
        record = json.loads(entry.read_text())
        self.assertEqual(record['state'], 'not_started')
        self.assertEqual(record['reason'], 'launcher_command_failed')
        self.assertFalse({'native_id', 'tag', 'incarnation', 'engine'} & record.keys())
        return settings, offer_id, record

    def test_persisted_first_action_failure_promotes_then_replays_after_restart_and_wrong_ack(self):
        self._assert_persisted_failure_recovers()

    def test_nested_cwd_failed_dispatch_recovers_through_both_entry_paths_after_restart(self):
        for entry in ['launcher', 'server']:
            with self.subTest(entry=entry):
                self._assert_persisted_failure_recovers(cwd='nested', entry=entry)

    def _assert_persisted_failure_recovers(self, cwd=None, entry='launcher'):
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            settings, offer, original = self._real_failed_dispatch(tmp, cwd)
            native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
            binding = dict(task_id='hub-' + offer, native_id=native, tag='task-hub-' + offer,
                           workspace=tmp, incarnation=1000, engine='opencode')
            status = {'state': 'failed', 'payload': {'owner': 'hub-fleet', 'cwd': original['target_cwd']}, 'native_binding': binding}
            row = {'id': offer, 'engine': 'opencode', 'mode': 'headless', 'workspace': 'main', 'cwd': cwd}
            if entry == 'server':
                row.update(native_id=native, binding={'native_id': native, 'tag': binding['tag'],
                    'workspace': tmp, 'native_created_at_ms': 1000})
            def recover(dispatcher):
                return (reconcile_retired(dispatcher, row) if entry == 'server' else
                        reconcile_launcher_retired(dispatcher, row, Path(original['target_cwd'])))
            d = Dispatcher(settings, request=Mock(), runner=Mock(side_effect=lambda argv, **kwargs:
                status if 'status' in argv else {'state': 'running', 'native_id': native, 'resources_reserved': True}))
            self.assertEqual(d._active_launches[offer], original)
            path = Path(tmp) / '.local/hub-results' / (offer + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'status': 'failed', 'report': 'Real partial worker report'}))
            tomb = {'finished_at_ms': 2300, 'exit': {'code': 0, 'signal': None}}
            runtime_failure = dict(reason='engine_exit_code', message='Runtime detail',
                                   error_type='RuntimeError', stage='runtime', exit_code=0)
            with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, tomb, 'verified')), \
                 patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, tomb, 'verified')), \
                 patch('agent_hub_host.runtime_diagnostics.runtime_diagnostic', return_value=runtime_failure), \
                 patch.object(Dispatcher, '_check_headless_health', return_value=None), \
                 patch.object(Dispatcher, '_maybe_cleanup_worktree') as cleanup:
                # Promotion is durable even while the launcher still reserves
                # resources; no terminal POST or cleanup can bypass that fence.
                self.assertFalse(recover(d))
                d.request.assert_not_called()
                cleanup.assert_not_called()
                d = Dispatcher(settings)
                self.assertEqual(d._active_launches[offer]['state'], 'running')
                self.assertEqual(d._active_launches[offer]['result_path'], str(path))
                posts = []
                def request(method, endpoint, payload=None):
                    posts.append(payload)
                    return {'id': offer, 'state': 'exited' if len(posts) == 1 else payload['state']}
                def released(argv, **kwargs):
                    return status if 'status' in argv else {
                        'state': 'failed', 'native_id': native, 'resources_reserved': False}
                d.runner, d.request = released, request
                self.assertFalse(recover(d))
                self.assertFalse(d._active_launches[offer]['settled'])
                cleanup.assert_not_called()
                d = Dispatcher(settings, runner=released, request=request)
                self.assertEqual(d._active_launches[offer]['dispatch_diagnostic'], original['diagnostic'])
                self.assertTrue(recover(d))
                self.assertEqual(posts[0], posts[1])
                self.assertEqual(posts[1]['state'], 'failed')
                self.assertEqual(posts[1]['diagnostic'], original['diagnostic'])
                self.assertEqual(posts[1]['report_status'], 'failed')
                self.assertEqual(posts[1]['report'], 'Real partial worker report')
                self.assertEqual(posts[1]['runtime']['exit_code'], 0)
                self.assertEqual(posts[1]['runtime']['wall_time_ms'], 1300)
                self.assertTrue(d._active_launches[offer]['settled'])
                self.assertEqual(d._active_launches[offer]['reason'], original['reason'])
                self.assertEqual(d._active_launches[offer]['target_cwd'], original['target_cwd'])
                cleanup.assert_called_once()
                self.assertEqual(d.reconcile_launches(), [])
                self.assertEqual(Dispatcher(settings, runner=released, request=request).reconcile_launches(), [])
                self.assertEqual(len(posts), 2)

    def test_failed_dispatch_promotion_refuses_conflicting_established_identity(self):
        from agent_hub_host.fleet import Dispatcher
        with tempfile.TemporaryDirectory() as tmp:
            settings, offer, original = self._real_failed_dispatch(tmp)
            native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
            binding = dict(task_id='hub-' + offer, native_id=native, tag='task-hub-' + offer,
                           workspace=tmp, incarnation=1000, engine='opencode')
            status = {'state': 'failed', 'payload': {'owner': 'hub-fleet', 'cwd': original['target_cwd']}, 'native_binding': binding}
            row = {'id': offer, 'engine': 'opencode', 'mode': 'headless'}
            for field, value in [('native_id', 'bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
                                 ('tag', 'task-hub-other'), ('incarnation', 1001), ('engine', 'codex'),
                                 ('workspace', tmp + '/other'), ('target_cwd', tmp + '/other'),
                                 ('task_id', 'hub-other'), ('settled', True)]:
                with self.subTest(field=field):
                    d = Dispatcher(settings, runner=Mock(return_value=status), request=Mock())
                    record = {**original, field: value}
                    d._active_launches[offer] = record
                    # Deliberately inject a foreign/corrupt durable fixture.
                    # The production writer now refuses established-identity
                    # replacement before this recovery fence can be exercised.
                    from agent_hub_host import private_fs
                    private_fs.atomic_write(d._journal_launches_dir() / (offer + '.json'),
                                            json.dumps(record).encode(), 131072)
                    before = dict(record)
                    with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {}, 'verified')):
                        self.assertFalse(reconcile_launcher_retired(d, row, Path(tmp)))
                    d.request.assert_not_called()
                    self.assertEqual(record, before)
                    self.assertEqual(d.runner.call_count, 1)  # status only; never release

    def test_nested_cwd_recovery_refuses_genuinely_conflicting_paths(self):
        from agent_hub_host.fleet import Dispatcher
        for entry in ['launcher', 'server']:
            cases = ['journal_cwd', 'offer_cwd', 'native_workspace']
            if entry == 'launcher':
                cases += ['payload_cwd', 'argument_cwd']
            for conflict in cases:
                with self.subTest(entry=entry, conflict=conflict), tempfile.TemporaryDirectory() as tmp:
                    settings, offer, original = self._real_failed_dispatch(tmp, 'nested')
                    native = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
                    binding = dict(task_id='hub-' + offer, native_id=native,
                        tag='task-hub-' + offer, workspace=tmp, incarnation=1000, engine='opencode')
                    status = {'state': 'failed', 'payload': {'owner': 'hub-fleet',
                        'cwd': original['target_cwd']}, 'native_binding': binding}
                    row = {'id': offer, 'engine': 'opencode', 'mode': 'headless',
                        'workspace': 'main', 'cwd': 'nested', 'native_id': native,
                        'binding': {'native_id': native, 'tag': binding['tag'],
                            'workspace': tmp, 'native_created_at_ms': 1000}}
                    d = Dispatcher(settings, runner=Mock(return_value=status), request=Mock())
                    argument = Path(original['target_cwd'])
                    other = str(Path(tmp) / 'other')
                    if conflict == 'journal_cwd':
                        d._active_launches[offer]['target_cwd'] = other
                    elif conflict == 'offer_cwd':
                        row['cwd'] = 'other'
                    elif conflict == 'native_workspace':
                        binding['workspace'] = original['target_cwd']
                        row['binding']['workspace'] = original['target_cwd']
                    elif conflict == 'payload_cwd':
                        status['payload']['cwd'] = other
                    elif conflict == 'argument_cwd':
                        argument = Path(other)
                    before = dict(d._active_launches[offer])
                    with patch('agent_hub_host.retired_native.verify_native_retirement', return_value=(True, {}, 'verified')):
                        result = (reconcile_retired(d, row) if entry == 'server' else
                                  reconcile_launcher_retired(d, row, argument))
                    self.assertFalse(result)
                    d.request.assert_not_called()
                    self.assertEqual(d._active_launches[offer], before)
                    self.assertFalse(any('reconcile' in call.args[0] for call in d.runner.call_args_list))

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
