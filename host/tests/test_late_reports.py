import json
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
