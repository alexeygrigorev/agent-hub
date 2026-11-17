"""Actual Bridge command+journal, fake native guard only; no provider or host process."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from agent_hub_host.agent import Bridge, Journal
from agent_hub_host import wake_send

NATIVE='11111111-2222-4333-8444-555555555555'
ENVELOPE='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
class SessionWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.temp.cleanup)
        self.bridge=Bridge.__new__(Bridge);self.bridge.journal=Journal(Path(self.temp.name)/'journal.sqlite')
        self.addCleanup(self.bridge.journal.close_thread)
        db=self.bridge.journal.db
        db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES('op','s',1,'d',?,'task-hub2-s',?,'running',1)",(self.temp.name,NATIVE));db.commit()
        self.status={'id':NATIVE,'tag':'task-hub2-s','workspace':self.temp.name,'phase':'running','created_at_ms':1}
        self.bridge.lookup=mock.Mock(side_effect=lambda *_a,**_k:copy.deepcopy(self.status))
        self.calls=[];self.guard='submitted';self.send_error=False
        def native(*args,**kwargs):
            self.calls.append(args)
            if args[1]=='send':
                if self.send_error:raise RuntimeError('synthetic_ambiguous_send')
                return subprocess.CompletedProcess(args,0,json.dumps({'id':ENVELOPE,'to':{'tag':'task-hub2-s','session_id':NATIVE}}),'')
            return subprocess.CompletedProcess(args,0,json.dumps({'status':self.guard}),'')
        self.bridge.native=native
        self.command={'id':'22222222-3333-4444-8555-666666666666','session_id':'s','epoch':1,'kind':'send','text':'--not-a-flag',
                      'wake':{'id':'a'*64,'session_id':'s','operation_id':'op','epoch':1,'native_id':NATIVE,'tag':'task-hub2-s','workspace':self.temp.name,'created_at_ms':1,'deadline_ms':2_000_000}}
        self.wall=mock.patch.object(wake_send.time,'time',return_value=1000);self.wall_clock=self.wall.start();self.addCleanup(self.wall.stop)
    def invoke(self):return self.bridge.command(copy.deepcopy(self.command))
    def authorize(self):
        self.assertEqual(self.invoke(),{'wake_pending':True,'native_message_id':ENVELOPE})
        self.command['wake'].update(native_message_id=ENVELOPE,ack_deadline_ms=2_100_000)

    def test_actual_command_guard_proof_is_journaled_before_outbox_and_replays_without_native_effect(self):
        self.authorize();result=self.invoke();self.assertEqual(result['wake_proof']['kind'],'native_delivery');self.assertEqual(result['wake_proof']['status'],'submitted')
        self.assertEqual(self.calls[0][-2: ],('--','--not-a-flag'))
        row=self.bridge.journal.db.execute('SELECT * FROM commands').fetchone();self.assertEqual(row['state'],'done')
        self.assertEqual(json.loads(self.bridge.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])['result'],result)
        before=len(self.calls);self.assertEqual(self.invoke(),result);self.assertEqual(len(self.calls),before)
    def test_busy_guard_is_not_ack_and_reconciliation_reuses_original_envelope(self):
        self.guard='busy';self.authorize();self.assertEqual(self.invoke(),{'wake_pending':True,'native_message_id':ENVELOPE});self.guard='already-submitted';result=self.invoke()
        self.assertEqual(result['wake_proof']['status'],'already-submitted');self.assertEqual(sum(c[1]=='send' for c in self.calls),1)
        self.assertTrue(all(c[-1]==ENVELOPE for c in self.calls if c[1]=='deliver'))
    def test_recipient_cursor_guard_is_accepted_without_human_or_model_reply(self):
        self.guard='recipient-acked';self.authorize();self.assertEqual(self.invoke()['wake_proof']['status'],'recipient-acked')
    def test_inbox_only_unknown_status_cannot_ack(self):
        self.authorize()
        for status in ('recorded','unknown','unsupported','not-ready'):
            self.guard=status;self.assertEqual(self.invoke(),{'wake_pending':True,'native_message_id':ENVELOPE})
        self.assertEqual(sum(c[1]=='send' for c in self.calls),1)
    def test_old_reader_internal_wake_kind_cannot_degrade_to_an_ordinary_send(self):
        self.command.pop('wake');self.command['kind']='wake_send'
        with self.assertRaisesRegex(ValueError,'command_not_allowed'):self.invoke()
        self.assertEqual(self.calls,[])

    def test_unknown_creation_never_replays_the_native_send(self):
        self.send_error=True
        with self.assertRaisesRegex(RuntimeError,'synthetic_ambiguous_send'):self.invoke()
        self.send_error=False
        with self.assertRaisesRegex(RuntimeError,'wake_outcome_uncertain'):self.invoke()
        self.assertEqual(len(self.calls),1)
    def test_changed_attempt_native_birth_and_headless_refuse_before_effect(self):
        original=copy.deepcopy(self.command)
        for key,value in [('epoch',2),('operation_id','foreign'),('native_id','99999999-2222-4333-8444-555555555555'),('created_at_ms',2)]:
            self.command=copy.deepcopy(original);self.command['wake'][key]=value
            with self.assertRaises((ValueError,RuntimeError)):self.invoke()
        self.command=original;self.bridge.journal.db.execute('UPDATE launches SET interactive=0');self.bridge.journal.db.commit()
        with self.assertRaisesRegex(ValueError,'headless_send_refused'):self.invoke()
        self.assertEqual(self.calls,[])
    def test_retarget_after_envelope_creation_never_delivers_it(self):
        original=self.bridge.native
        def swap(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1]=='send':self.status['created_at_ms']=2
            return result
        self.bridge.native=swap
        with self.assertRaisesRegex(ValueError,'wake_identity_unverified'):self.invoke()
        self.assertEqual(sum(c[1]=='send' for c in self.calls),1);self.assertEqual(sum(c[1]=='deliver' for c in self.calls),0)
    def test_changed_command_digest_cannot_borrow_pending_envelope(self):
        self.guard='busy';self.invoke();self.command['text']='changed'
        with self.assertRaisesRegex(ValueError,'command_id_conflict'):self.invoke()
        self.assertEqual(sum(c[1]=='send' for c in self.calls),1)
    def test_late_guard_success_is_not_returned_as_a_timely_ack(self):
        self.authorize()
        original=self.bridge.native
        def late(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1]=='deliver':self.wall_clock.return_value=2100
            return result
        self.bridge.native=late
        with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertNotIn('wake_proof',json.loads(self.bridge.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])['result'])
    def test_foreign_envelope_receipt_never_proceeds_to_delivery(self):
        self.bridge.native=lambda *a,**k:subprocess.CompletedProcess(a,0,json.dumps({'id':ENVELOPE,'to':{'tag':'foreign','session_id':NATIVE}}),'')
        with self.assertRaisesRegex(RuntimeError,'wake_receipt_unverified'):self.invoke()
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
    def test_first_receipt_does_not_physically_submit_before_server_deadline_authority(self):
        self.assertEqual(self.invoke(),{'wake_pending':True,'native_message_id':ENVELOPE})
        self.assertEqual(sum(c[1]=='deliver' for c in self.calls),0)
        self.assertEqual(self.invoke(),{'wake_pending':True,'native_message_id':ENVELOPE})
        self.assertEqual(sum(c[1]=='send' for c in self.calls),1)

    def test_ack_deadline_cannot_renew_after_the_one_authorized_phase_transition(self):
        self.authorize();self.guard='busy';self.invoke();self.command['wake']['ack_deadline_ms']+=1
        with self.assertRaisesRegex(ValueError,'command_id_conflict'):self.invoke()
        self.assertEqual(sum(c[1]=='send' for c in self.calls),1)

if __name__=='__main__':
    import sys
    if sys.argv[1:] == ['--wire']:
        case=SessionWakeTests();case.setUp();case.wall.stop()
        try:
            first=True
            for line in sys.stdin:
                command=json.loads(line);binding=command['wake'];case.command=command
                if first:
                    db=case.bridge.journal.db;db.execute('DELETE FROM launches')
                    db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES(?,?,?,'d',?,?,?,'running',1)",(binding['operation_id'],binding['session_id'],binding['epoch'],binding['workspace'],binding['tag'],binding['native_id']));db.commit();first=False
                    case.status.update(id=binding['native_id'],tag=binding['tag'],workspace=binding['workspace'],created_at_ms=binding['created_at_ms'])
                    case.bridge.native=lambda *a,**k:subprocess.CompletedProcess(a,0,json.dumps({'id':ENVELOPE,'to':{'tag':binding['tag'],'session_id':binding['native_id']}} if a[1]=='send' else {'status':'submitted'}),'')
                print(json.dumps(case.invoke()),flush=True)
        finally:case.doCleanups()
    else:unittest.main()
