import ctypes
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import resource_admission as r
from agent_hub_host.agent import Journal


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        # Only this fixture's new directory; never chmod shared ancestors.
        parent = Path(__file__).resolve().parents[3] / '.local' / 'tmp'
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = Journal(self.root / 'journal.sqlite')
        self.addCleanup(self.journal.close_thread)
        self.db = self.journal.db

    def row(self, operation='op', session='sid', state='launching', native=None, marker=0):
        with self.db:
            self.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,native_dispatch_started) VALUES(?,?,1,?,?,?,?,?,?)',
                (operation, session, 'digest', str(self.root), 'task-hub2-' + session, native, state, marker))
            value = self.db.execute('SELECT * FROM launches WHERE operation_id=?', (operation,)).fetchone()
            self.db.execute('UPDATE launches SET admission_json=? WHERE operation_id=?', (r.starting(value), operation))
        return self.db.execute('SELECT * FROM launches WHERE operation_id=?', (operation,)).fetchone()

    def test_exact_floor_and_one_byte_below_preserve_marker(self):
        self.row()
        with patch.object(r, 'available_bytes', return_value=r.FLOOR_BYTES + 1500*r.MIB-1):
            with self.assertRaises(r.Refused):r.admit(self.db, 'op','sid',1)
        self.assertEqual(self.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],0)
        with patch.object(r, 'available_bytes', return_value=r.FLOOR_BYTES + 1500*r.MIB):
            r.admit(self.db, 'op','sid',1).check()
        self.assertEqual(self.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],1)

    def test_pending_queue_holds_zero_and_current_estimate_is_once(self):
        self.row();self.row('queued','queued','pending_admission',marker=None)
        with patch.object(r, 'available_bytes', return_value=r.FLOOR_BYTES+1500*r.MIB):
            r.admit(self.db,'op','sid',1)

    def test_marker_zero_other_preparation_is_reserved(self):
        self.row();self.row('other','other')
        with patch.object(r,'available_bytes',return_value=r.FLOOR_BYTES+2999*r.MIB):
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)

    def test_stop_and_replay_never_second_dispatch(self):
        self.row()
        with self.db:self.db.execute('INSERT INTO stop_fences VALUES(?,?)',('sid',1))
        with patch.object(r,'available_bytes',return_value=100*r.FLOOR_BYTES):
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)
        with self.db:self.db.execute('DELETE FROM stop_fences')
        with patch.object(r,'available_bytes',return_value=100*r.FLOOR_BYTES):
            r.admit(self.db,'op','sid',1)
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)

    def test_concurrent_threads_serialize_one_budget(self):
        self.row('a','a');self.row('b','b')
        # Two initial preparations reserve each other: 3GB admits one; its
        # marker1 remains a full reserve so lowering by1byte refuses the next.
        barrier=threading.Barrier(2);results=[];samples=iter([r.FLOOR_BYTES+3000*r.MIB,r.FLOOR_BYTES+3000*r.MIB-1])
        def run(op):
            barrier.wait()
            try:r.admit(self.journal.db,op,op,1);results.append('admitted')
            except r.Refused:results.append('refused')
            finally:self.journal.close_thread()
        with patch.object(r,'available_bytes',side_effect=lambda:next(samples)):
            threads=[threading.Thread(target=run,args=(x,))for x in ['a','b']]
            for t in threads:t.start()
            for t in threads:t.join(5);self.assertFalse(t.is_alive())
        self.assertCountEqual(results,['admitted','refused'])

    def test_legacy_null_and_terminal_spelling_are_not_release(self):
        row=self.row(state='expired_unconfirmed')
        with self.assertRaises(r.Refused):r.reserve(row,r.clock(),r.boot())
        with self.db:self.db.execute('UPDATE launches SET admission_json=NULL')
        row=self.db.execute('SELECT * FROM launches').fetchone()
        with self.assertRaises(r.Refused):r.reserve(row,r.clock(),r.boot())

    def test_long_preparation_does_not_age_resident_ramp(self):
        row=self.row()
        with patch.object(r,'clock',return_value=900),patch.object(r,'boot',return_value='boot'),patch.object(r,'available_bytes',return_value=100*r.FLOOR_BYTES):
            r.admit(self.db,'op','sid',1)
        with self.db:self.db.execute("UPDATE launches SET state='running',native_id='native'")
        row=self.db.execute('SELECT * FROM launches').fetchone()
        native={'id':'native','tag':row['tag'],'workspace':row['workspace'],'phase':'running','created_at_ms':100,'engine':'codex','containment_empty':False}
        with self.db,patch.object(r,'clock',return_value=901),patch.object(r,'boot',return_value='boot'):r.bind(self.db,row,native)
        row=self.db.execute('SELECT * FROM launches').fetchone()
        with patch.object(r,'anonymous_mb',return_value=100):
            self.assertEqual(r.reserve(row,901,'boot'),1400)
            self.assertEqual(r.reserve(row,1080,'boot'),0)
            self.assertEqual(r.reserve(row,1080,'different-boot'),1500)
        with patch.object(r,'anonymous_mb',return_value=0):self.assertEqual(r.reserve(row,2000,'boot'),1500)

    def test_slow_commit_and_final_permit_keep_marker_one(self):
        self.row();times=iter([0.,0.,2.])
        with patch.object(r,'clock',side_effect=lambda:next(times)),patch.object(r,'available_bytes',return_value=100*r.FLOOR_BYTES):
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)
        self.assertEqual(self.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],1)
        with patch.object(r,'clock',return_value=5):
            with self.assertRaises(r.Refused):r.Permit(('op','sid',1),0).check()

    def test_linux_metrics_and_caps_are_closed(self):
        self.assertEqual(r.linux_available('MemTotal: 12 kB\nMemAvailable: 10 kB\n'),10240)
        for raw in ['MemFree: 10 kB','MemTotal: 12 kB\nMemAvailable: 10 MB','MemTotal: 12 kB\nMemAvailable: 13 kB','MemTotal: 12 kB\nMemAvailable: 1 kB\nMemAvailable: 1 kB']:
            with self.subTest(raw=raw),self.assertRaises(r.Refused):r.linux_available(raw)
        for memory,pids in [(True,256),(1501,256),(1500,257),(0,1)]:
            with self.subTest(memory=memory,pids=pids),self.assertRaises(r.Refused):r.caps(memory,pids)

    def test_windows_abi_false_and_sane_physical_fields(self):
        self.assertEqual(ctypes.sizeof(r.MemoryStatus),64)
        class Function:
            def __call__(self,p):
                p._obj.total_phys=20*1024**3;p._obj.avail_phys=12*1024**3
                return 1
        class Kernel:GlobalMemoryStatusEx=Function()
        self.assertEqual(r.windows_available(Kernel()),12*1024**3)
        class Failed:
            def __call__(self,p):return 0
        class Bad:GlobalMemoryStatusEx=Failed()
        with self.assertRaises(r.Refused):r.windows_available(Bad())

    def test_real_synthetic_cgroup_credit_excludes_cache_and_foreign_pid(self):
        root=self.root/'cgroup';proc=self.root/'proc';path=root/'aplexer-workload-native.scope';path.mkdir(parents=True)
        pid=proc/'42';pid.mkdir(parents=True)
        (pid/'cgroup').write_text('0::/aplexer-workload-native.scope\n')
        (pid/'stat').write_text('42 (fixture process) '+' '.join(['S']+['0']*18+['123'])+'\n')
        for name,value in {'memory.max':str(1500*r.MIB),'memory.current':str(700*r.MIB),'memory.stat':'anon '+str(100*r.MIB)+'\nfile '+str(600*r.MIB),'cgroup.events':'populated 1','cgroup.procs':'42'}.items():(path/name).write_text(value)
        identity={'id':'native','workload_pid':42,'workload_cgroup':'/aplexer-workload-native.scope','containment_cgroup':str(path),'containment_cgroup_identity':{'boot':'fake'}}
        with patch.object(r,'_kernel_identity',return_value={'boot':'fake'}):
            self.assertEqual(r.anonymous_mb(identity,root,proc),100)
            (path/'cgroup.procs').write_text('99')
            self.assertEqual(r.anonymous_mb(identity,root,proc),0)

    def test_binding_new_incarnation_cannot_reuse_or_adopt_old_age(self):
        row=self.row(state='running',native='native',marker=1)
        native={'id':'native','tag':row['tag'],'workspace':row['workspace'],'phase':'running','created_at_ms':100,'containment_empty':False}
        with patch.object(r,'clock',return_value=100),patch.object(r,'boot',return_value='boot'):
            with self.db:r.bind(self.db,row,native)
        row=self.db.execute('SELECT * FROM launches').fetchone()
        with self.db:r.bind(self.db,row,{**native,'created_at_ms':200})
        row=self.db.execute('SELECT * FROM launches').fetchone()
        self.assertIsNone(json.loads(row['admission_json'])['identity'])
        with self.db:r.bind(self.db,row,{**native,'created_at_ms':200})
        row=self.db.execute('SELECT * FROM launches').fetchone()
        with patch.object(r,'anonymous_mb',return_value=1500):self.assertEqual(r.reserve(row,1000,'boot'),1500)

    def test_duplicate_private_record_is_unknown_not_last_field_wins(self):
        row=self.row()
        with self.db:self.db.execute('UPDATE launches SET admission_json=?', ('{"version":0,'+row['admission_json'][1:],))
        with self.assertRaises(r.Refused):r.metadata(self.db.execute('SELECT * FROM launches').fetchone())

    def test_backwards_measurement_clock_refuses_before_marker(self):
        self.row()
        times=iter([10.,9.])
        with patch.object(r,'clock',side_effect=lambda:next(times)),patch.object(r,'available_bytes',return_value=100*r.FLOOR_BYTES):
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)
        self.assertEqual(self.db.execute('SELECT native_dispatch_started FROM launches').fetchone()[0],0)


class LegacyAccountingEvidenceTests(unittest.TestCase):
    setUp=AdmissionTests.setUp
    row=AdmissionTests.row
    native='11111111-2222-4333-8444-555555555555'

    def legacy(self,state='exited',marker=1,native=None):
        self.row(state=state,native=native,marker=marker)
        with self.db:self.db.execute('UPDATE launches SET admission_json=NULL,runtime_deadline=100')
        return self.db.execute('SELECT * FROM launches').fetchone()

    def save(self,key,payload,path='/api/sessions/sid/finish'):
        with self.db:self.journal.enqueue(key,path,payload)

    def exit_payload(self,row):
        return {'operation_id':'op','epoch':1,'native_id':row['native_id'],'exit_code':0,'native_exit_confirmed':True,
            'evidence':{'source':'aplexer_retirement','containment_empty':True,
                'native_identity':{'id':row['native_id'],'tag':row['tag'],'workspace':row['workspace']},
                'native_exit':{'code':0,'signal':None}}}

    def absence_payload(self,row):
        return {'operation_id':'op','epoch':1,'native_id':row['native_id'],'launch_expired':True,
            'native_outcome':'unconfirmed','runtime_deadline':100,'evidence':{'source':'aplexer_all_registry_census',
                'complete':True,'match_count':0,'row_count':1,'sha256':'a'*64,'workspace':row['workspace'],
                'tag':row['tag'],'dispatch_provenance':'host_journal','started_at_ms':150,'observed_at_ms':160}}

    def test_real_persisted_exit_recovers_private_accounting_only(self):
        row=self.legacy(native=self.native);payload=self.exit_payload(row);self.save('finish:op',payload)
        before=self.db.execute('SELECT * FROM outbox').fetchone()['payload']
        with self.db:self.assertTrue(r.recover_release(self.db,row))
        current=self.db.execute('SELECT * FROM launches').fetchone()
        self.assertEqual(current['state'],'exited');self.assertEqual(current['native_dispatch_started'],1)
        self.assertTrue(json.loads(current['admission_json'])['released'])
        self.assertEqual(self.db.execute('SELECT payload FROM outbox').fetchone()[0],before)
        self.assertFalse(r.recover_release(self.db,current))

    def test_exact_no_dispatch_receipt_and_marker_zero_recover(self):
        row=self.legacy('stopped_before_dispatch',marker=0)
        self.save('no-dispatch:op:1',{'operation_id':'op','epoch':1,'native_id':None,'launch_failed':True,'no_dispatch':True})
        with self.db:self.assertTrue(r.recover_release(self.db,row))
        self.assertEqual(self.db.execute('SELECT state FROM launches').fetchone()[0],'stopped_before_dispatch')

    def test_persisted_postdeadline_absence_recovers_not_exit(self):
        row=self.legacy('expired_unconfirmed',native=self.native)
        self.save('launch-expired:op:1',self.absence_payload(row))
        with self.db:self.assertTrue(r.recover_release(self.db,row))
        value=json.loads(self.db.execute('SELECT admission_json FROM launches').fetchone()[0])
        self.assertEqual(value['release_reason'],'qualified_absence')
        self.assertEqual(self.db.execute('SELECT state FROM launches').fetchone()[0],'expired_unconfirmed')

    def test_persisted_legacy_absence_column_is_bound_before_reporting(self):
        row=self.legacy('launch_failed_reported',marker=None)
        payload=self.absence_payload(row);payload.pop('launch_expired');payload.pop('runtime_deadline')
        payload.update(launch_failed=True,failure_kind='legacy_census_absent');payload['evidence']['dispatch_provenance']='legacy_unknown'
        with self.db:self.db.execute('UPDATE launches SET legacy_failure_json=?',(json.dumps(payload),))
        row=self.db.execute('SELECT * FROM launches').fetchone()
        with self.db:self.assertTrue(r.recover_release(self.db,row))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_wrong_epoch_native_and_forged_receipt_never_release(self):
        row=self.legacy(native=self.native)
        for change in [{'epoch':2},{'epoch':True},{'operation_id':'other'},{'native_id':'foreign'},
                       {'evidence':{'containment_empty':True,'source':'forged'}},{'native_exit_confirmed':False}]:
            with self.subTest(change=change):
                self.save('finish:op',{**self.exit_payload(row),**change})
                with self.db:self.assertFalse(r.recover_release(self.db,row))
                self.assertIsNone(self.db.execute('SELECT admission_json FROM launches').fetchone()[0])

    def test_wrong_source_key_path_and_no_receipt_are_unproven(self):
        row=self.legacy(native=self.native)
        self.assertFalse(r.recover_release(self.db,row))
        self.save('foreign',self.exit_payload(row));self.save('finish:op',self.exit_payload(row),'/api/sessions/foreign/finish')
        with self.db:self.assertFalse(r.recover_release(self.db,row))
        self.assertIsNone(self.db.execute('SELECT admission_json FROM launches').fetchone()[0])

    def test_absence_guard_rejects_wrong_workspace_and_predeadline(self):
        row=self.legacy('expired_unconfirmed',native=self.native)
        for change in [{'workspace':'/foreign'},{'tag':'foreign'},{'started_at_ms':99},{'complete':False},{'match_count':1}]:
            with self.subTest(change=change):
                payload=self.absence_payload(row);payload['evidence'].update(change)
                self.save('launch-expired:op:1',payload)
                with self.db:self.assertFalse(r.recover_release(self.db,row))

    def test_accounting_rollback_preserves_lifecycle_and_outbox(self):
        row=self.legacy(native=self.native);self.save('finish:op',self.exit_payload(row))
        self.db.execute('BEGIN IMMEDIATE');self.assertTrue(r.recover_release(self.db,row));self.db.rollback()
        current=self.db.execute('SELECT * FROM launches').fetchone()
        self.assertIsNone(current['admission_json']);self.assertEqual(current['state'],'exited')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_stale_running_observation_cannot_overwrite_terminal_release(self):
        old=self.row(state='running',native=self.native,marker=1)
        with self.db:
            self.db.execute("UPDATE launches SET state='exited'")
            r.release(self.db,old,'qualified_exit')
        released=self.db.execute('SELECT admission_json FROM launches').fetchone()[0]
        native={'id':self.native,'tag':old['tag'],'workspace':old['workspace'],'phase':'running',
                'created_at_ms':100,'containment_empty':False}
        with self.db:r.bind(self.db,old,native)
        self.assertEqual(self.db.execute('SELECT admission_json FROM launches').fetchone()[0],released)

    def test_old_observation_cannot_release_a_replaced_attempt(self):
        row=self.legacy(native=self.native);self.save('finish:op',self.exit_payload(row))
        with self.db:self.db.execute('UPDATE launches SET epoch=2')
        with self.db:self.assertFalse(r.recover_release(self.db,row))
        self.assertIsNone(self.db.execute('SELECT admission_json FROM launches').fetchone()[0])

    def old_exit_and_new_launch(self):
        old=self.row('old','old','exited',native=self.native,marker=1)
        with self.db:self.db.execute('UPDATE launches SET admission_json=NULL WHERE operation_id=?',('old',))
        payload=self.exit_payload(old);payload['operation_id']='old'
        self.save('finish:old',payload,'/api/sessions/old/finish')
        self.row()

    def test_actual_admission_reuses_exact_persisted_exit_without_native_query(self):
        self.old_exit_and_new_launch()
        with patch.object(r,'available_bytes',return_value=r.FLOOR_BYTES+1500*r.MIB):r.admit(self.db,'op','sid',1)
        self.assertEqual(self.db.execute("SELECT native_dispatch_started FROM launches WHERE operation_id='op'").fetchone()[0],1)
        self.assertTrue(json.loads(self.db.execute("SELECT admission_json FROM launches WHERE operation_id='old'").fetchone()[0])['released'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_admission_refusal_rolls_back_legacy_accounting_refinement(self):
        self.old_exit_and_new_launch()
        with patch.object(r,'available_bytes',return_value=3*1024**3):
            with self.assertRaises(r.Refused):r.admit(self.db,'op','sid',1)
        self.assertIsNone(self.db.execute("SELECT admission_json FROM launches WHERE operation_id='old'").fetchone()[0])
        self.assertEqual(self.db.execute("SELECT native_dispatch_started FROM launches WHERE operation_id='op'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
