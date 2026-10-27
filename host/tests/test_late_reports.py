import json
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent_hub_host.fleet import Dispatcher
from agent_hub_host import late_reports

OFFER='11111111-2222-4333-8444-555555555555'
NATIVE='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class LateReports(unittest.TestCase):
    def failed_payload(self, record):
        return dict(state='failed', confirmed_exit=True, native_id=NATIVE,
                    launcher_task_id=record['task_id'], report_status='failed',
                    report='Failed with partial work; no final JSON.', artifacts=[],
                    diagnostic=dict(reason='engine_exit_code', message='Exited without result.',
                                    error_type='RuntimeError', stage='runtime', exit_code=0),
                    runtime=copy.deepcopy(record['terminal_runtime']))

    def test_stop_receipt_enrichment_is_durable_before_retry_and_preserves_failed_report(self):
        from agent_hub_host.terminal_reports import read_record
        with tempfile.TemporaryDirectory() as tmp:
            d, _, record = self.fixture(tmp)
            record.update(state='running', settled=False)
            payload = self.failed_payload(record)
            posts = []
            def request(method, path, value):
                posts.append(copy.deepcopy(value))
                self.assertEqual(read_record(d, OFFER)['terminal_payload'], value)
                if value['state'] == 'failed':
                    raise ValueError('HTTP request failed with status 409: invalid_transition')
                self.assertNotIn('diagnostic', value)
                return dict(id=OFFER, state='exited', replay=True)
            d.request = request
            with patch.object(d, '_maybe_cleanup_worktree') as cleanup:
                self.assertTrue(late_reports.acknowledge(d, record, payload, False))
                cleanup.assert_not_called()  # Missing report still gets its late-file window.
            self.assertEqual([p['state'] for p in posts], ['failed', 'exited'])
            self.assertEqual(posts[1]['report_status'], 'failed')
            self.assertEqual(posts[1]['runtime'], payload['runtime'])
            self.assertEqual(payload['state'], 'failed')
            self.assertTrue(record['settled'])
            self.assertEqual(record['state'], 'exited')
            self.assertNotIn('terminal_payload', record)

    def test_real_reconciler_builds_failed_diagnostics_after_an_already_acknowledged_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, cfg, record = self.fixture(tmp)
            cfg['launcher'] = dict(argv=['launcher'], root=tmp, config_dir=str(Path(tmp)/'launcher'))
            record.update(state='running', settled=False)
            d._active_launches[OFFER] = record
            d._persist_launch_record(record)
            d.runner = Mock(return_value=dict(state='failed', native_id=NATIVE, resources_reserved=False))
            posts = []
            def server(method, path, payload):
                posts.append(copy.deepcopy(payload))
                if payload['state'] == 'failed':
                    raise ValueError('HTTP request failed with status 409: invalid_transition')
                return dict(id=OFFER, state='exited', replay=True)
            d.request = server
            tomb = dict(finished_at_ms=2234, exit=dict(code=0, signal=None, exited_at_ms=2234))
            with patch.object(d, '_maybe_cleanup_worktree'), \
                 patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, tomb, 'exact')):
                result = d.reconcile_launches(terminal_only=True)
            self.assertEqual(len(result), 1)
            self.assertEqual([p['state'] for p in posts], ['failed', 'exited'])
            self.assertEqual(posts[1]['report_status'], 'failed')
            self.assertEqual(posts[1]['runtime']['exit_code'], 0)
            self.assertEqual(posts[1]['runtime']['wall_time_ms'], 1234)
            self.assertEqual(posts[1]['runtime']['outcome'], 'failed-without-report')
            self.assertIn('without a valid structured report', posts[1]['report'])
            self.assertTrue(d._active_launches[OFFER]['settled'])
            self.assertEqual(d._active_launches[OFFER]['state'], 'exited')

    def test_enrichment_lost_ack_restart_requalifies_and_replays_exact_intent(self):
        from agent_hub_host.terminal_reports import read_record
        with tempfile.TemporaryDirectory() as tmp:
            d, cfg, record = self.fixture(tmp)
            cfg['launcher'] = dict(argv=['launcher'], root=tmp, config_dir=str(Path(tmp)/'launcher'))
            record.update(state='running', settled=False)
            # Original dispatch diagnostics would otherwise be reattached after restart.
            record['dispatch_diagnostic'] = dict(reason='engine_auth_expired',
                message='Original dispatch error.', stage='launcher_run', error_type='AuthenticationError')
            posts = []
            def lost(method, path, value):
                posts.append(copy.deepcopy(value))
                if value['state'] == 'failed':
                    raise ValueError('HTTP request failed with status 409: invalid_transition')
                raise TimeoutError('reply lost')
            d.request = lost
            self.assertFalse(late_reports.acknowledge(d, record, self.failed_payload(record), False))
            durable = read_record(d, OFFER)
            self.assertEqual(durable['terminal_payload']['state'], 'exited')
            self.assertEqual(len(posts), 2)
            restarted = Dispatcher(cfg)
            restarted.runner = Mock(return_value=dict(state='failed', native_id=NATIVE, resources_reserved=False))
            restarted.request = Mock(return_value=dict(id=OFFER, state='exited', replay=True))
            with patch.object(restarted, '_maybe_cleanup_worktree'), \
                 patch('agent_hub_host.fleet.verify_native_retirement', return_value=(False, None, 'unknown')):
                self.assertEqual(restarted.reconcile_launches(terminal_only=True), [])
            restarted.request.assert_not_called()
            with patch.object(restarted, '_maybe_cleanup_worktree'), \
                 patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, dict(finished_at_ms=2234), 'exact')):
                self.assertEqual(len(restarted.reconcile_launches(terminal_only=True)), 1)
            replay = restarted.request.call_args.args[2]
            self.assertEqual(replay, posts[1])
            self.assertTrue(read_record(restarted, OFFER)['settled'])

    def test_only_exact_confirmed_transition_conflict_allows_one_enrichment_attempt(self):
        errors = [OSError('ambiguous'), TimeoutError('lost reply'), ValueError('unknown'),
                  ValueError('HTTP request failed with status 409: other_error'),
                  ValueError('HTTP request failed with status 401: invalid_transition')]
        for error in errors:
            with self.subTest(error=type(error).__name__, message=str(error)), tempfile.TemporaryDirectory() as tmp:
                d, _, record = self.fixture(tmp)
                record.update(state='running', settled=False)
                d.request = Mock(side_effect=error)
                self.assertFalse(late_reports.acknowledge(d, record, self.failed_payload(record), False))
                self.assertEqual(d.request.call_count, 1)
                self.assertEqual(record['terminal_payload']['state'], 'failed')
                self.assertFalse(record['settled'])

    def test_enrichment_bad_ack_and_non_headless_or_routed_contract_stay_pending(self):
        for change in [{}, {'mode': 'interactive'}, {'routing_task_id': OFFER}]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                d, _, record = self.fixture(tmp)
                record.update(state='running', settled=False, **change)
                d.request = Mock(side_effect=[ValueError('HTTP request failed with status 409: invalid_transition'),
                                             dict(id='another', state='exited')])
                self.assertFalse(late_reports.acknowledge(d, record, self.failed_payload(record), False))
                self.assertFalse(record['settled'])
                self.assertEqual(d.request.call_count, 2 if not change else 1)

    def fixture(self, tmp):
        root=Path(tmp)
        settings={'version':1,'workspaces':{'main':tmp},'subscriptions':[],
                  'journal_dir':str(root/'journal')}
        record=dict(offer_id=OFFER,task_id='hub-'+OFFER,mode='headless',workspace=tmp,
                    native_id=NATIVE,tag='task-hub-'+OFFER,incarnation=1000,engine='opencode',
                    state='failed',settled=True,result_path=str(root/'.local/hub-results'/(OFFER+'.json')))
        late_reports.arm(record,False,{'exit_code':0,'exit_signal':None,'wall_time_ms':1234,
                                      'log_tail':'partial work','engine_outcome':'succeeded',
                                      'observed_commits':[],'dirty_paths':[],'outcome':'failed-with-partial-work'})
        return Dispatcher(settings),settings,record

    def test_restart_forwards_late_worker_status_once_without_reopening_or_proving_exit(self):
        for status in ['done','failed','waiting-on-human','stuck']:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as tmp:
                d,settings,record=self.fixture(tmp)
                d._persist_launch_record(record)
                restarted=Dispatcher(settings)
                self.assertIn(OFFER,restarted._active_launches)
                path=Path(record['result_path']);path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps({'status':status,'report':'Late worker report','artifacts':[]}))
                restarted.request=Mock(return_value={'id':OFFER,'state':'failed','replay':True})
                with patch('agent_hub_host.fleet.verify_native_retirement') as retire, \
                     patch.object(restarted,'_maybe_cleanup_worktree'):
                    result=restarted.reconcile_launches()
                    restarted.reconcile_launches()
                retire.assert_not_called()
                self.assertTrue(result[0]['late_report_delivered'])
                restarted.request.assert_called_once()
                payload=restarted.request.call_args.args[2]
                self.assertEqual(payload['state'],'failed')
                self.assertEqual(payload['report_status'],status)
                self.assertEqual(payload['runtime']['outcome'],'reported')
                self.assertTrue(restarted._active_launches[OFFER]['worker_report_received'])
                self.assertNotIn(OFFER,Dispatcher(settings)._active_launches)

    def test_bad_ack_or_missing_file_does_not_record_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,_,record=self.fixture(tmp)
            d.request=Mock(return_value={'id':'different','state':'failed'})
            self.assertFalse(late_reports.deliver(d,record))
            d.request.assert_not_called()
            path=Path(record['result_path']);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'status':'done','report':'Late file'}))
            self.assertFalse(late_reports.deliver(d,record))
            self.assertFalse(record['worker_report_received'])

    def test_retry_window_is_bounded_and_does_not_recover_unproven_or_old_settlements(self):
        now=int(time.time()*1000)
        base={'settled':True,'state':'failed','worker_report_received':False}
        for deadline in [None,True,0,now-1,now+late_reports.WINDOW_MS+1]:
            self.assertFalse(late_reports.pending(dict(base,late_report_deadline_ms=deadline),now))
        self.assertTrue(late_reports.pending(dict(base,late_report_deadline_ms=now+1000),now))
        self.assertFalse(late_reports.pending(dict(base,settled=False,late_report_deadline_ms=now+1000),now))
        self.assertFalse(late_reports.pending(dict(base,routing_task_id=OFFER,late_report_deadline_ms=now+1000),now))


    def sync_dispatch(self, tmp, ack_ok=True):
        from agent_hub_host.fleet import utcnow
        root=Path(tmp)
        cfg={'version':1,'workspaces':{'main':tmp},'capabilities':[],
             'journal_dir':str(root/'journal'),'subscriptions':[{'service':'codex',
             'account_label':'fixture','engine':'codex','profile':'hub-fixture',
             'launcher_provider':'codex','launcher_model':'gpt-6.1-sol',
             'admission_argv':['maintained-gate']}],
             'launcher':{'argv':['launcher'],'root':tmp,'config_dir':str(root/'launcher')}}
        def runner(argv,**kwargs):
            if argv[0]=='maintained-gate':
                return {'launch_allowed':True,'profile_supported':True,'account_label':'fixture','observed_at':utcnow()}
            if 'plan' in argv:
                return {'eligible':[{'provider':'codex','model':'gpt-6.1-sol','task_fit':1,
                                     'profile':'hub-fixture','account_label':'fixture'}]}
            if 'run' in argv:
                return {'receipt':{'session_id':NATIVE,'exit_code':0,'tag':'task-hub-'+OFFER,
                                   'workspace':tmp,'engine':'codex','profile':'hub-fixture',
                                   'account_label':'fixture','created_at_ms':1000}}
            return {}
        def request(method,path,payload=None):
            if path.endswith('/result'):
                return {'id':OFFER if ack_ok else 'wrong','state':payload['state']}
            return {'id':OFFER,'state':'starting'}
        d=Dispatcher(cfg,request=request,runner=runner)
        task={'id':OFFER,'engine':'codex','profile':'hub-fixture','account':'fixture',
              'mode':'headless','workspace':'main','prompt':'Small fixture task'}
        with patch.object(d,'_maybe_cleanup_worktree') as cleanup:
            result=d.dispatch(task)
            self.assertEqual(cleanup.call_count,0)
        return d,cfg,result

    def test_synchronous_terminal_ack_arms_restart_late_report_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,cfg,result=self.sync_dispatch(tmp)
            self.assertEqual(result['state'],'failed')
            self.assertTrue(d._active_launches[OFFER]['settled'])
            rp=Path(d._active_launches[OFFER]['result_path'])
            rp.write_text(json.dumps({'status':'done','report':'Late synchronous report'}))
            restarted=Dispatcher(cfg)
            restarted.request=Mock(return_value={'id':OFFER,'state':'failed'})
            with patch.object(restarted,'_maybe_cleanup_worktree') as cleanup:
                self.assertTrue(restarted.reconcile_launches()[0]['late_report_delivered'])
                cleanup.assert_called_once()
            self.assertEqual(restarted.request.call_args.args[2]['report_status'],'done')

    def test_synchronous_bad_ack_keeps_terminal_delivery_for_qualified_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,cfg,result=self.sync_dispatch(tmp,False)
            self.assertEqual(result['state'],'uncertain')
            self.assertFalse(d._active_launches[OFFER]['settled'])
            self.assertEqual(d._active_launches[OFFER]['terminal_payload']['state'],'failed')
            Path(d._active_launches[OFFER]['result_path']).write_text(json.dumps({'status':'done','report':'Recovered file'}))
            restarted=Dispatcher(cfg)
            restarted.request=Mock(return_value={'id':OFFER,'state':'failed'})
            restarted.runner=Mock(return_value={'state':'completed-awaiting-review','native_id':NATIVE,'resources_reserved':False})
            with patch.object(restarted,'_check_headless_health',return_value=None), \
                 patch('agent_hub_host.fleet.verify_native_retirement',return_value=(True,{'finished_at_ms':3100},'verified')), \
                 patch.object(restarted,'_maybe_cleanup_worktree'):
                result=restarted.reconcile_launches()
            self.assertEqual(result[0]['state'],'failed')
            self.assertEqual(restarted.request.call_args.args[2]['report_status'],'done')
            self.assertNotIn('terminal_payload',restarted._active_launches[OFFER])

    def test_expired_restart_recovers_cleanup_without_reopening_report_or_lifecycle(self):
        for received in [False,True]:
            with self.subTest(received=received),tempfile.TemporaryDirectory() as tmp:
                d,cfg,record=self.fixture(tmp)
                record.update(late_report_deadline_ms=int(time.time()*1000)-1,
                              worker_report_received=received,cleanup_pending=True)
                d._persist_launch_record(record)
                restarted=Dispatcher(cfg)
                self.assertIn(OFFER,restarted._active_launches)
                restarted.request=Mock(side_effect=AssertionError('expired recovery must not send'))
                with patch.object(restarted,'_maybe_cleanup_worktree') as cleanup:
                    self.assertEqual(restarted.reconcile_launches(),[])
                    cleanup.assert_called_once()
                restarted.request.assert_not_called()
                self.assertFalse(restarted._active_launches[OFFER]['cleanup_pending'])
                self.assertNotIn(OFFER,Dispatcher(cfg)._active_launches)


    def test_terminal_delivery_rejects_cross_session_receipt_before_persist_or_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,_,record=self.fixture(tmp)
            record.update(state='running',settled=False)
            payload=dict(state='failed',confirmed_exit=True,native_id=NATIVE,
                         launcher_task_id=record['task_id'],report_status='failed',
                         report='Partial work',artifacts=[])
            d.request=Mock(side_effect=AssertionError('wrong identity must not post'))
            with patch.object(d,'_persist_launch_record') as persist, \
                 patch.object(d,'_maybe_cleanup_worktree') as cleanup:
                for mismatch in [dict(native_id=OFFER),dict(launcher_task_id='another-task'),
                                 dict(confirmed_exit=False),dict(state='running')]:
                    with self.subTest(mismatch=mismatch):
                        self.assertFalse(late_reports.acknowledge(d,record,dict(payload,**mismatch),False))
                persist.assert_not_called()
                cleanup.assert_not_called()
            d.request.assert_not_called()
            self.assertNotIn('terminal_payload',record)


    def test_lost_response_restart_preserves_original_authentication_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,cfg,record=self.fixture(tmp)
            record.update(state='running',settled=False)
            cfg['launcher']=dict(argv=['launcher'],root=tmp,config_dir=str(Path(tmp)/'launcher'))
            diagnostic=dict(reason='engine_auth_expired',message='Engine authentication expired.',
                            error_type='AuthenticationError',stage='launcher_run')
            payload=dict(state='failed',confirmed_exit=True,native_id=NATIVE,
                         launcher_task_id=record['task_id'],diagnostic=diagnostic,
                         report_status='failed',report='Authentication failed before completion',artifacts=[])
            d.request=Mock(side_effect=TimeoutError('response lost after server commit'))
            self.assertFalse(late_reports.acknowledge(d,record,payload,False))
            restarted=Dispatcher(cfg)
            restarted.request=Mock(return_value=dict(id=OFFER,state='failed'))
            restarted.runner=Mock(return_value=dict(state='completed-awaiting-review',native_id=NATIVE,resources_reserved=False))
            with patch.object(restarted,'_check_headless_health',return_value=None), \
                 patch('agent_hub_host.fleet.verify_native_retirement',return_value=(True,dict(finished_at_ms=3100),'verified')), \
                 patch.object(restarted,'_maybe_cleanup_worktree'):
                results=restarted.reconcile_launches()
            self.assertEqual(results[0]['state'],'failed')
            self.assertEqual(restarted.request.call_args.args[2]['diagnostic'],diagnostic)
            self.assertTrue(restarted._active_launches[OFFER]['settled'])
