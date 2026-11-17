"""Actual Bridge command+journal, fake native guard only; no provider or host process."""
import copy
import hashlib
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


class StrictSessionWakeTests(unittest.TestCase):
    invoke = SessionWakeTests.invoke
    def setUp(self):
        SessionWakeTests.setUp(self)
        import types
        import sys
        self.command['wake']['transport']='strict-submit:v1'
        self.fake=Path(self.temp.name)/'fake_cli.py'
        self.mode=Path(self.temp.name)/'mode';self.mode.write_text('ok')
        self.fake.write_text('''import sys,json,hashlib
from pathlib import Path
root=Path(__file__).parent;args=sys.argv[1:]
if args==['send','--help']:
 print('--strict --enter --expect-tag --expect-workspace --expect-created-at-ms --idempotency-key --deadline-ms' if (root/'mode').read_text()!='unsupported' else '--enter');raise SystemExit(0)
if args[:2]==['message','send']:
 print('cross-workspace messaging requires an aplexer session identity',file=sys.stderr);raise SystemExit(2)
assert args[0]=='send' and '--strict' in args and '--enter' in args
assert '--from' not in args and '--workspace' not in args
field=lambda name:args[args.index(name)+1]
text=args[args.index('--')+1];mode=(root/'mode').read_text()
r={'version':1,'session_id':args[1],'operation_id':field('--idempotency-key'),'deadline_ms':int(field('--deadline-ms')),
   'request_sha256':hashlib.sha256(json.dumps([args[1],field('--idempotency-key'),field('--expect-tag'),field('--expect-workspace'),int(field('--expect-created-at-ms')),int(field('--deadline-ms')),text],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
   'status':'submitted_verified','mutation':True,'landed':True,'submitted':True,'consumed':None,'reason':'submission_observed'}
fence=root/'native-fence.json'
if fence.exists():
 r=json.loads(fence.read_text());r['replayed']=True
elif mode=='busy':r.update(status='refused',mutation=False,landed=False,submitted=False,reason='not_idle')
else:
 if mode=='uncertain':r.update(status='uncertain',submitted=False,reason='submission_uncertain')
 fence.write_text(json.dumps(r));(root/'effect-count').write_text('1')
if mode=='bad:duplicate':
 print(json.dumps(r)[:-1]+',\"version\":1}');raise SystemExit(0)
if mode.startswith('bad:'):
 key=mode[4:]
 if key=='extra':r['extra']='untrusted'
 elif key=='deadline_ms':r[key]+=1
 elif key=='version':r[key]=True
 else:r[key]='foreign'
print(json.dumps(r));raise SystemExit(0 if r['status']=='submitted_verified' else 4)
''')
        self.calls=[];self.bridge.executable=sys.executable
        self.bridge.native=types.MethodType(Bridge.native,self.bridge)
        def runner(argv,**kwargs):
            self.calls.append(tuple(argv[1:]))
            return subprocess.run([sys.executable,'-I','-S',str(self.fake),*argv[1:]],**kwargs)
        self.bridge.runner=runner

    def test_transport_capability_requires_selected_native_flags_and_caches_by_identity(self):
        self.assertTrue(self.bridge.strict_wake_supported())
        self.assertTrue(self.bridge.strict_wake_supported());self.assertEqual(self.calls,[('send','--help')])
        self.bridge._strict_wake_capability=None;self.mode.write_text('unsupported')
        self.assertFalse(self.bridge.strict_wake_supported())

    def test_missing_selected_executable_keeps_optional_transport_unknown_without_spawn(self):
        del self.bridge.executable
        self.assertFalse(self.bridge.strict_wake_supported())
        self.assertEqual(self.calls,[])

    def test_nonlinux_capability_observation_never_spawns_cli(self):
        with mock.patch('agent_hub_host.agent.sys.platform','win32'):
            self.assertFalse(self.bridge.strict_wake_supported())
        self.assertEqual(self.calls,[])

    def test_old_mailbox_service_context_causally_fails_without_forged_sender(self):
        self.command['wake'].pop('transport')
        with self.assertRaisesRegex(RuntimeError,'native_command_failed'):self.invoke()
        with self.assertRaisesRegex(RuntimeError,'wake_outcome_uncertain'):self.invoke()
        self.assertEqual(len(self.calls),1)
        self.assertFalse((Path(self.temp.name)/'effect-count').exists())

    def test_fixed_strict_argv_actual_process_receipt_done_and_replay_no_new_effect(self):
        result=self.invoke();b=self.command['wake'];args=self.calls[0]
        self.assertEqual(args,('send',NATIVE,'--enter','--strict','--expect-tag',b['tag'],'--expect-workspace',b['workspace'],
            '--expect-created-at-ms','1','--idempotency-key',self.command['id'],'--deadline-ms','2000000','--json','--',self.command['text']))
        self.assertEqual(result['wake_proof']['kind'],'strict_submit_v1');self.assertNotIn('native_message_id',result['wake_proof'])
        self.assertEqual(json.loads(self.bridge.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])['result'],result)
        self.assertEqual(self.invoke(),result);self.assertEqual(len(self.calls),1)

    def test_busy_refusal_can_retry_same_id_without_deadline_renewal(self):
        self.mode.write_text('busy');self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        self.assertFalse((Path(self.temp.name)/'native-fence.json').exists())
        self.mode.write_text('ok');self.assertEqual(self.invoke()['delivery'],'native_acknowledged')
        self.assertEqual(self.calls[0],self.calls[1])

    def test_worker_uncertain_fence_is_not_second_business_effect_or_ack(self):
        self.mode.write_text('uncertain');self.assertTrue(self.invoke()['wake_pending'])
        self.mode.write_text('ok');self.assertTrue(self.invoke()['wake_pending'])
        self.assertEqual((Path(self.temp.name)/'effect-count').read_text(),'1')
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertTrue(all('wake_proof' not in json.loads(r[0])['result'] for r in self.bridge.journal.db.execute('SELECT payload FROM outbox')))

    def test_local_crash_after_native_receipt_reconciles_original_once_id(self):
        finish=self.bridge.finish_command
        self.bridge.finish_command=mock.Mock(side_effect=RuntimeError('synthetic_crash'))
        with self.assertRaisesRegex(RuntimeError,'synthetic_crash'):self.invoke()
        self.bridge.finish_command=finish
        result=self.invoke();self.assertTrue(result['wake_proof']['receipt']['replayed'])
        self.assertEqual(self.calls[0],self.calls[1]);self.assertEqual((Path(self.temp.name)/'effect-count').read_text(),'1')

    def test_changed_receipt_binding_or_schema_never_acknowledges(self):
        for key in ('session_id','operation_id','request_sha256','deadline_ms','version','extra','duplicate'):
            with self.subTest(key=key):
                self.mode.write_text('bad:'+key)
                with self.assertRaisesRegex(RuntimeError,'wake_receipt_unverified'):self.invoke()
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')

    def test_late_receipt_and_changed_incarnation_never_qualify(self):
        runner=self.bridge.runner
        def late(*a,**k):
            result=runner(*a,**k);self.wall_clock.return_value=2001;return result
        self.bridge.runner=late
        with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.wall_clock.return_value=1000;self.status['created_at_ms']=2
        with self.assertRaisesRegex(ValueError,'wake_identity_unverified'):self.invoke()
        self.assertEqual(len(self.calls),1)

    def test_headless_changed_epoch_and_transport_refused_before_effect(self):
        original=copy.deepcopy(self.command)
        for key,value in [('epoch',2),('operation_id','foreign'),('transport','foreign')]:
            self.command=copy.deepcopy(original);self.command['wake'][key]=value
            with self.assertRaises(ValueError):self.invoke()
        self.command=original;self.bridge.journal.db.execute('UPDATE launches SET interactive=0');self.bridge.journal.db.commit()
        with self.assertRaisesRegex(ValueError,'headless_send_refused'):self.invoke()
        self.assertEqual(self.calls,[])

class StrictInboxWakeTests(unittest.TestCase):
    invoke = SessionWakeTests.invoke
    def setUp(self):
        SessionWakeTests.setUp(self)
        import types,sys
        self.command['wake']['transport']='strict-inbox:v1'
        self.environment=mock.patch.dict(os.environ,{'APLEXER_WORKSPACE':''});self.environment.start();self.addCleanup(self.environment.stop)
        self.mode=Path(self.temp.name)/'mode';self.mode.write_text('ok')
        self.fake=Path(self.temp.name)/'inbox_cli.py'
        self.fake.write_text(INBOX_CLI)
        self.calls=[];self.bridge.executable=sys.executable
        self.bridge.native=types.MethodType(Bridge.native,self.bridge)
        def runner(argv,**kwargs):
            self.calls.append((tuple(argv[1:]),kwargs.get('cwd')))
            return subprocess.run([sys.executable,'-I','-S',str(self.fake),*argv[1:]],**kwargs)
        self.bridge.runner=runner
    def creations(self):return [c for c in self.calls if c[0][:2]==('message','send')]
    def test_full_envelope_short_notice_same_command_replay_and_per_call_cwd(self):
        result=self.invoke();proof=result['wake_proof']
        self.assertEqual(proof['kind'],'strict_inbox_notice_v1');self.assertEqual(proof['envelope_id'],ENVELOPE)
        self.assertEqual(proof['body_sha256'],hashlib.sha256(self.command['text'].encode()).hexdigest())
        self.assertEqual(len(self.creations()),1);args,cwd=self.creations()[0]
        self.assertEqual(cwd,self.temp.name);self.assertNotIn('--workspace',args);self.assertNotIn('--from',args);self.assertNotIn('--pane',args)
        notice=[c for c in self.calls if c[0][0]=='send'][0][0]
        self.assertEqual(notice[-1],'Read inbox message '+ENVELOPE);self.assertEqual(len(notice[-1]),55)
        self.assertEqual(notice[notice.index('--idempotency-key')+1],self.command['id'])
        self.assertEqual(notice[notice.index('--deadline-ms')+1],'2000000')
        count=len(self.calls);self.assertEqual(self.invoke(),result);self.assertEqual(len(self.calls),count)
    def test_context_identity_stderr_and_workspace_override_refuse_before_creation(self):
        for mode in ['identity','who-error','who-output','who-stderr']:
            with self.subTest(mode=mode):
                self.mode.write_text(mode)
                with self.assertRaisesRegex(RuntimeError,'wake_local_context_unverified'):self.invoke()
        with mock.patch.dict(os.environ,{'APLEXER_WORKSPACE':'/foreign'}):
            with self.assertRaisesRegex(RuntimeError,'wake_local_context_unverified'):self.invoke()
        self.assertEqual(self.creations(),[])
        self.assertEqual(self.bridge.journal.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0],0)
    def test_unknown_create_never_creates_again_even_if_native_durable(self):
        self.mode.write_text('create-unknown')
        with self.assertRaisesRegex(RuntimeError,'native_command_failed'):self.invoke()
        self.assertTrue((Path(self.temp.name)/'envelope.json').exists())
        self.mode.write_text('ok')
        with self.assertRaisesRegex(RuntimeError,'wake_outcome_uncertain'):self.invoke()
        self.assertEqual(len(self.creations()),1)
    def test_busy_notice_reuses_real_envelope_and_original_native_once(self):
        self.mode.write_text('busy');self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-inbox:v1'})
        self.mode.write_text('ok');self.assertEqual(self.invoke()['wake_proof']['envelope_id'],ENVELOPE)
        self.assertEqual(len(self.creations()),1)
        notices=[c[0] for c in self.calls if c[0][0]=='send'];self.assertEqual(notices[0],notices[1])
    def test_early_ack_is_not_notice_proof_and_no_duplicate_business_envelope(self):
        self.mode.write_text('early-ack-busy');self.assertTrue(self.invoke()['wake_pending'])
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.mode.write_text('ok');self.assertEqual(self.invoke()['delivery'],'native_acknowledged');self.assertEqual(len(self.creations()),1)
    def test_committed_progress_rechecks_missing_or_changed_envelope_without_recreation(self):
        self.mode.write_text('busy');self.invoke()
        for mode in ['show-missing','show-changed']:
            self.mode.write_text(mode)
            with self.assertRaises(RuntimeError):self.invoke()
        self.assertEqual(len(self.creations()),1)
    def test_actual_envelope_schema_target_data_body_and_sender_are_not_borrowable(self):
        for field in ['id','to','body','data','workspace','from','delivery','extra','duplicate']:
            with self.subTest(field=field):
                # Each scenario owns a fresh journal; no clearing a consumed fence.
                self.bridge.journal.close_thread()
                self.bridge.journal=Journal(Path(self.temp.name)/('case-'+field+'.sqlite'))
                self.bridge.journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES('op','s',1,'d',?,'task-hub2-s',?,'running',1)",(self.temp.name,NATIVE));self.bridge.journal.db.commit()
                self.mode.write_text('bad-envelope:'+field)
                with self.assertRaises((RuntimeError,ValueError)):self.invoke()
        self.assertFalse(any(c[0][0]=='send' for c in self.calls))
    def test_late_after_durable_creation_retains_unknown_without_notice(self):
        runner=self.bridge.runner
        def late(argv,**kw):
            result=runner(argv,**kw)
            if argv[1:3]==['message','send']:self.wall_clock.return_value=2001
            return result
        self.bridge.runner=late
        with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.wall_clock.return_value=1000;self.bridge.runner=runner
        with self.assertRaisesRegex(RuntimeError,'wake_outcome_uncertain'):self.invoke()
        self.assertFalse(any(c[0][0]=='send' for c in self.calls))

INBOX_CLI = r'''import sys,json,hashlib,os
from pathlib import Path
root=Path(__file__).parent;args=sys.argv[1:];mode=(root/'mode').read_text()
if args==['whoami','--json']:
 if mode=='identity':print('{}');raise SystemExit(0)
 if mode=='who-output':print('{}')
 if mode in ['who-error','who-stderr']:print('synthetic',file=sys.stderr)
 raise SystemExit(1)
if args[:2]==['message','send']:
 assert '--workspace' not in args and '--from' not in args and '--pane' not in args
 text=args[args.index('--')+1];data=json.loads(args[args.index('--data')+1]);b=data['wake_binding']
 assert os.getcwd()==b['workspace']
 envelope={'schema_version':1,'id':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee','workspace':os.getcwd(),'created_at':1,'from':{'external':True},'to':{'tag':b['tag'],'session_id':b['native_id']},'kind':'hub-wake','body':text,'data':data,'delivery':'inbox'}
 (root/'envelope.json').write_text(json.dumps(envelope))
 if mode=='create-unknown':raise SystemExit(2)
 if mode.startswith('bad-envelope:'):
  field=mode.split(':')[1]
  if field=='extra':envelope['extra']=True
  elif field=='duplicate':print(json.dumps(envelope)[:-1]+',"id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}');raise SystemExit(0)
  else:envelope[field]='foreign'
 print(json.dumps(envelope));raise SystemExit(0)
if args[:2]==['message','show']:
 assert args[-1]=='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
 if mode=='show-missing':raise SystemExit(2)
 e=json.loads((root/'envelope.json').read_text())
 if mode=='show-changed':e['body']='foreign'
 print(json.dumps(e));raise SystemExit(0)
assert args[0]=='send' and '--strict' in args
field=lambda key:args[args.index(key)+1];text=args[args.index('--')+1]
assert text=='Read inbox message aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
r={'version':1,'session_id':args[1],'operation_id':field('--idempotency-key'),'deadline_ms':int(field('--deadline-ms')),'request_sha256':hashlib.sha256(json.dumps([args[1],field('--idempotency-key'),field('--expect-tag'),field('--expect-workspace'),int(field('--expect-created-at-ms')),int(field('--deadline-ms')),text],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),'status':'submitted_verified','mutation':True,'landed':True,'submitted':True,'consumed':None,'reason':'submission_observed'}
if mode in ['busy','early-ack-busy']:r.update(status='refused',mutation=False,landed=False,submitted=False,reason='not_idle')
print(json.dumps(r));raise SystemExit(0 if r['submitted'] else 4)
'''

if __name__=='__main__':
    import sys
    if sys.argv[1:] in (['--wire'],['--strict-wire'],['--inbox-wire']):
        case=(StrictInboxWakeTests() if '--inbox-wire' in sys.argv else StrictSessionWakeTests() if '--strict-wire' in sys.argv else SessionWakeTests());case.setUp();case.wall.stop()
        try:
            first=True
            for line in sys.stdin:
                command=json.loads(line);binding=command['wake'];case.command=command
                if first:
                    db=case.bridge.journal.db;db.execute('DELETE FROM launches')
                    db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES(?,?,?,'d',?,?,?,'running',1)",(binding['operation_id'],binding['session_id'],binding['epoch'],binding['workspace'],binding['tag'],binding['native_id']));db.commit();first=False
                    case.status.update(id=binding['native_id'],tag=binding['tag'],workspace=binding['workspace'],created_at_ms=binding['created_at_ms'])
                    if '--strict-wire' not in sys.argv and '--inbox-wire' not in sys.argv:
                        case.bridge.native=lambda *a,**k:subprocess.CompletedProcess(a,0,json.dumps({'id':ENVELOPE,'to':{'tag':binding['tag'],'session_id':binding['native_id']}} if a[1]=='send' else {'status':'submitted'}),'')
                print(json.dumps(case.invoke()),flush=True)
        finally:case.doCleanups()
    else:unittest.main()
