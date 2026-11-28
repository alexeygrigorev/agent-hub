"""Actual Bridge command+journal, fake native guard only; no provider or host process."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from agent_hub_host.agent import Bridge, Journal
from agent_hub_host import wake_send

NATIVE='11111111-2222-4333-8444-555555555555'
ENVELOPE='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
CLOSED_DIAG_EVENTS=frozenset(('attempt_start','validation_refused','identity_refused','context_refused','deadline_exceeded','outcome_uncertain','receipt_unverified','command_intent_committed','envelope_stored','envelope_reread','submit_attempted','submit_pending','submit_verified','done_replay','unrecognized'))
CLOSED_DIAG_PHASES=frozenset(('entry','validated','identity','context','envelope','submit','receipt','terminal'))
CLOSED_DIAG_STATUSES=frozenset(('submitted_verified','refused','uncertain','unrecognized','absent'))
CLOSED_DIAG_REASONS=frozenset(('invalid_wake_command','command_id_conflict','wake_fence_conflict','headless_send_refused','wake_identity_unverified','wake_local_context_unverified','wake_deadline_exceeded','wake_outcome_uncertain','wake_receipt_unverified','duplicate_receipt_field','submission_observed','preflight_refused','input_busy','invalid_text','dispatch_fenced','prior_dispatch_uncertain','receipt_unreadable','deadline_expired','identity_mismatch','not_idle','input_client_attached','input_not_settled','unknown_layout','unsafe_composer','readiness_changed','paste_uncertain','landing_uncertain','landing_changed','enter_uncertain','unrecognized','absent'))
CLOSED_DIAG_FLAGS=frozenset(('true','false','unknown','absent','unrecognized'))
_DIAG_KEYS={'event','phase','command','attempt','status','reason','mutation','landed','submitted','replayed'}
_HEX16=re.compile(r'[0-9a-f]{16}\Z');_HEX12=re.compile(r'[0-9a-f]{12}\Z')
_UUID=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
class StrictDiagTools:
    'Closed strict-attempt diagnostic capture; events() validates and settles the bounded queue.'
    def capture(self):
        records=[]
        class Sink(logging.Handler):
            def emit(self,record):records.append(record.getMessage())
        sink=Sink();logger=logging.getLogger(wake_send.STRICT_DIAG_LOGGER)
        logger.addHandler(sink);self.addCleanup(lambda:logger.removeHandler(sink))
        def drain(limit=10.0):
            end=time.perf_counter()+limit
            while not wake_send._STRICT_DIAG_QUEUE.empty() and time.perf_counter()<end:time.sleep(0.02)
        self.addCleanup(drain)
        return records
    def settle(self,records,first=2.0,quiet=0.25):
        'Bounded wait for the async logging worker: until the first record or first, then quiet.'
        prefix='wake_strict_diag ';count=0;deadline=time.perf_counter()+first;quiet_until=None
        while True:
            now=len([m for m in records if isinstance(m,str) and m.startswith(prefix)])
            if now>count:count=now;quiet_until=time.perf_counter()+quiet
            if count==0 and time.perf_counter()>=deadline:break
            if count and quiet_until is not None and time.perf_counter()>=quiet_until:break
            time.sleep(0.01)
        return count
    def events(self,records,first=2.0,quiet=0.25):
        'Parsed records with per-field closed vocabularies; attempts become A1/A2/... by first appearance.'
        prefix='wake_strict_diag '
        self.settle(records,first=first,quiet=quiet)
        parsed=[json.loads(message[len(prefix):]) for message in records if isinstance(message,str) and message.startswith(prefix)]
        tokens={}
        for record in parsed:
            self.assertEqual(set(record),_DIAG_KEYS)
            for value in record.values():self.assertIsInstance(value,str)
            self.assertIn(record['event'],CLOSED_DIAG_EVENTS)
            self.assertIn(record['phase'],CLOSED_DIAG_PHASES)
            self.assertIn(record['status'],CLOSED_DIAG_STATUSES)
            self.assertIn(record['reason'],CLOSED_DIAG_REASONS)
            for key in ('mutation','landed','submitted','replayed'):self.assertIn(record[key],CLOSED_DIAG_FLAGS)
            command=record['command']
            self.assertTrue(command=='unknown' or _HEX16.fullmatch(command) or _UUID.fullmatch(command),command)
            attempt=record['attempt']
            if attempt=='unknown':tokens[attempt]='unknown'
            else:
                self.assertTrue(_HEX12.fullmatch(attempt),attempt)
                if attempt not in tokens:tokens[attempt]='A%d'%(len(tokens)+1)
            record['attempt']=tokens[attempt]
        return parsed
    def diag(self,event,phase,attempt='A1',command=None,**fields):
        return {'event':event,'phase':phase,'command':self.command['id'] if command is None else command,'attempt':attempt,
                'status':fields.get('status','absent'),'reason':fields.get('reason','absent'),
                'mutation':fields.get('mutation','absent'),'landed':fields.get('landed','absent'),
                'submitted':fields.get('submitted','absent'),'replayed':fields.get('replayed','absent')}
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


class StrictSessionWakeTests(StrictDiagTools, unittest.TestCase):
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
 if r['status']!='submitted_verified':r['reason']='prior_dispatch_uncertain'
elif mode=='busy':r.update(status='refused',mutation=False,landed=False,submitted=False,reason='not_idle')
elif mode=='layout':r.update(status='refused',mutation=False,landed=False,submitted=False,reason='unknown_layout')
elif mode=='alien':r.update(status='alien_status',mutation=False,landed=False,submitted=False,reason='alien_reason')
elif mode.startswith('flag:'):
	key=mode[5:]
	if key=='status-true':r['status']=True
	elif key=='reason-false':r['reason']=False
	elif key=='mutation-null':r['mutation']=None
	elif key=='replayed-null':r['replayed']=None
	elif key=='replayed-text':r['replayed']='yes'
else:
 if mode=='uncertain':r.update(status='uncertain',mutation=True,landed=False,submitted=False,reason='dispatch_fenced')
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

    def test_verified_submit_diagnostic_is_closed_and_done_replay_stays_effect_free(self):
        records=self.capture();result=self.invoke()
        self.assertEqual(result['wake_proof']['kind'],'strict_submit_v1')
        events=self.events(records)
        self.assertEqual(events[0],self.diag('attempt_start',phase='entry'))
        self.assertTrue(any(e['event']=='command_intent_committed' for e in events))
        self.assertTrue(any(e['event']=='submit_attempted' for e in events))
        self.assertEqual(events[-1],self.diag('submit_verified',phase='terminal',status='submitted_verified',
            reason='submission_observed',mutation='true',landed='true',submitted='true'))
        before=len(self.calls);records=self.capture();self.assertEqual(self.invoke(),result)
        self.assertEqual(self.events(records)[-1],self.diag('done_replay',phase='terminal'))
        self.assertEqual(len(self.calls),before)
    def test_pending_diagnostics_distinguish_not_idle_unknown_layout_uncertain_and_replay(self):
        records=self.capture()
        refusal={'phase':'receipt','status':'refused','mutation':'false','landed':'false','submitted':'false','replayed':'absent'}
        for mode in ('busy','layout'):
            self.mode.write_text(mode);self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        self.mode.write_text('uncertain')
        for replayed in ('absent','true'):
            self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        pending=[e for e in self.events(records) if e['event']=='submit_pending']
        self.assertEqual(pending,[self.diag('submit_pending',attempt='A1',reason='not_idle',**refusal),
                                  self.diag('submit_pending',attempt='A2',reason='unknown_layout',**refusal),
                                  self.diag('submit_pending',attempt='A3',status='uncertain',reason='dispatch_fenced',phase='receipt',
                                            mutation='true',landed='false',submitted='false',replayed='absent'),
                                  self.diag('submit_pending',attempt='A4',status='uncertain',reason='prior_dispatch_uncertain',phase='receipt',
                                            mutation='true',landed='false',submitted='false',replayed='true')])
        self.assertEqual((Path(self.temp.name)/'effect-count').read_text(),'1')
    def test_unknown_receipt_and_unknown_failure_stay_unrecognized(self):
        records=self.capture();self.mode.write_text('alien')
        self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        pending=[e for e in self.events(records) if e['event']=='submit_pending']
        self.assertEqual(pending,[self.diag('submit_pending',phase='receipt',status='unrecognized',reason='unrecognized',
                                            mutation='false',landed='false',submitted='false')])
        records=self.capture();runner=self.bridge.runner
        def ambiguous(argv,**kwargs):
            if argv[1]=='send':raise RuntimeError('synthetic_ambiguous_send')
            return runner(argv,**kwargs)
        self.bridge.runner=ambiguous
        with self.assertRaisesRegex(RuntimeError,'synthetic_ambiguous_send'):self.invoke()
        events=self.events(records)
        self.assertEqual(events[-1],self.diag('unrecognized',phase='submit',reason='unrecognized'))
        self.assertTrue(all(e['event']!='command_intent_committed' for e in events))
    def test_deadline_exceeded_diagnostic_is_closed(self):
        records=self.capture();runner=self.bridge.runner
        def late(*args,**kwargs):
            result=runner(*args,**kwargs);self.wall_clock.return_value=2001;return result
        self.bridge.runner=late
        with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        deadlines=[e for e in self.events(records) if e['event']=='deadline_exceeded']
        self.assertEqual(deadlines,[self.diag('deadline_exceeded',phase='submit',reason='wake_deadline_exceeded')])
    def test_headless_refusal_diagnostic_is_closed(self):
        records=self.capture()
        self.bridge.journal.db.execute('UPDATE launches SET interactive=0');self.bridge.journal.db.commit()
        with self.assertRaisesRegex(ValueError,'headless_send_refused'):self.invoke()
        self.assertEqual(self.events(records)[-1],self.diag('identity_refused',phase='identity',reason='headless_send_refused'))
        self.assertEqual(self.calls,[])
    def test_invalid_transport_diagnostic_is_closed_validation_refusal(self):
        records=self.capture();self.command['wake']['transport']='foreign'
        with self.assertRaisesRegex(ValueError,'invalid_wake_command'):self.invoke()
        self.assertEqual(self.events(records)[-1],self.diag('validation_refused',phase='entry',reason='invalid_wake_command'))
    def test_untrusted_receipt_schema_diagnostic_is_closed_without_ack(self):
        records=self.capture();self.mode.write_text('bad:extra')
        with self.assertRaisesRegex(RuntimeError,'wake_receipt_unverified'):self.invoke()
        self.assertEqual(self.events(records)[-1],self.diag('receipt_unverified',phase='submit',reason='wake_receipt_unverified'))
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
    def test_diagnostics_never_leak_raw_command_envelope_or_path_values(self):
        records=self.capture()
        self.command['wake']['transport']='foreign'
        with self.assertRaises(ValueError):self.invoke()
        self.command['wake']['transport']='strict-submit:v1'
        self.mode.write_text('busy');self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        self.mode.write_text('bad:extra')
        with self.assertRaises(RuntimeError):self.invoke()
        self.bridge.journal.db.execute('UPDATE launches SET interactive=0');self.bridge.journal.db.commit()
        with self.assertRaises(ValueError):self.invoke()
        forbidden=(self.command['text'],'--not-a-flag','--strict',NATIVE,ENVELOPE,self.temp.name,'foreign','untrusted','strict-submit:v1')
        for message in records:
            for raw in forbidden:self.assertNotIn(raw,message)
        parsed=self.events(records)  # Validates the exact 10-key schema and per-field closed vocabularies.
        self.assertTrue(len(parsed)>=4)
        events={record['event'] for record in parsed}
        self.assertTrue({'validation_refused','submit_pending','receipt_unverified','identity_refused'}<=events)
    def test_broken_diagnostic_sink_never_changes_outcome(self):
        with mock.patch.object(logging.getLogger(wake_send.STRICT_DIAG_LOGGER),'info',side_effect=RuntimeError('sink_broken')):
            self.mode.write_text('busy')
            self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})

    def test_records_bind_one_attempt_to_one_bounded_command_correlation(self):
        records=self.capture();self.mode.write_text('busy')
        self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        events=self.events(records)
        self.assertTrue(len(events)>=3)
        for record in events:
            self.assertEqual(record['command'],self.command['id'])
            self.assertLessEqual(len(record['command']),36)
            self.assertEqual(record['attempt'],'A1')
    def test_malformed_command_ids_fall_back_to_stable_closed_digest(self):
        base={'id':'22222222-3333-4444-8555-666666666666','kind':'send','wake':{'session_id':'s','operation_id':'op','epoch':1,
              'native_id':NATIVE,'tag':'task-hub2-s','workspace':'/w','created_at_ms':1,'deadline_ms':2,'transport':'strict-submit:v1'}}
        self.assertEqual(wake_send._strict_diag_correlation(base),base['id'])
        leak='raw command id and text leak'
        digest=wake_send._strict_diag_correlation(dict(base,id=leak))
        self.assertEqual(len(digest),16);self.assertTrue(all(c in '0123456789abcdef' for c in digest))
        self.assertNotIn('leak',digest)
        self.assertEqual(digest,wake_send._strict_diag_correlation(dict(base,id=leak)))
        self.assertNotEqual(digest,wake_send._strict_diag_correlation(dict(base,id='other leak')))
        self.assertEqual(wake_send._strict_diag_correlation({'id':object(),'wake':base['wake']}),'unknown')
        nondict=wake_send._strict_diag_correlation('nondict')
        self.assertEqual(len(nondict),16);self.assertTrue(all(c in '0123456789abcdef' for c in nondict))
        fallback=wake_send._strict_diag_correlation({'wake':'notadict'})
        self.assertEqual(len(fallback),16)
        records=self.capture();self.command['id']='not-a-canonical-uuid leak'
        with self.assertRaisesRegex(ValueError,'invalid_command_id'):self.invoke()
        self.assertEqual(self.events(records),[])
        self.assertEqual(self.calls,[])
        binding=self.command['wake']
        direct={'id':'not-a-canonical-uuid leak','session_id':'s','kind':'send','text':'--not-a-flag',
                'wake':dict(binding,transport='foreign')}
        expected=hashlib.sha256(json.dumps([direct['id'],direct['kind'],binding['session_id'],binding['operation_id'],
            binding['epoch'],binding['native_id'],binding['tag'],binding['workspace'],binding['created_at_ms'],
            binding['deadline_ms'],'foreign'],ensure_ascii=True,separators=(',',':')).encode()).hexdigest()[:16]
        records=self.capture()
        with self.assertRaises(ValueError):wake_send.execute_strict(self.bridge,copy.deepcopy(direct),'d')
        last=self.events(records)[-1]
        self.assertEqual(last['event'],'validation_refused');self.assertEqual(last['phase'],'entry')
        self.assertEqual(last['command'],expected)
        records=self.capture()
        with self.assertRaises(ValueError):wake_send.execute_strict(self.bridge,copy.deepcopy(direct),'d')
        self.assertEqual(self.events(records)[-1]['command'],expected)
    def test_finalization_stays_bound_to_original_budget_after_queued_diagnostics(self):
        records=self.capture();clock={'t':0.0}
        runner=self.bridge.runner
        def burn(argv,**kwargs):
            result=runner(argv,**kwargs);clock['t']+=35.0;return result
        self.bridge.runner=burn
        with mock.patch.object(wake_send.time,'monotonic',lambda:clock['t']):
            self.mode.write_text('busy')
            with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertEqual(self.bridge.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        last=self.events(records)[-1]
        self.assertEqual(last,self.diag('deadline_exceeded',phase='submit',reason='wake_deadline_exceeded'))
    def test_positive_finish_rechecks_original_budget_after_terminal_diagnostic(self):
        records=self.capture();clock={'t':0.0}
        original=wake_send._strict_diag_record
        def burn_at_final(correlation,attempt,category,phase,**fields):
            if category=='submit_verified':clock['t']+=35.0  # Deterministic advance of only the final diagnostic preparation event.
            return original(correlation,attempt,category,phase,**fields)
        with mock.patch.object(wake_send.time,'monotonic',lambda:clock['t']), \
                mock.patch.object(wake_send,'_strict_diag_record',side_effect=burn_at_final):
            with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.assertEqual(self.calls[0][self.calls[0].index('--idempotency-key')+1],self.command['id'])
        self.assertEqual(len(self.calls),1)  # The expired final boundary sent no second native input.
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertEqual(self.bridge.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)  # No done result or positive ACK.
        last=self.events(records)[-1]
        self.assertEqual(last,self.diag('deadline_exceeded',phase='terminal',reason='wake_deadline_exceeded'))
    def test_retry_of_same_command_keeps_command_but_rotates_attempt(self):
        records=self.capture();self.mode.write_text('busy')
        first=self.invoke();second=self.invoke()
        self.assertEqual(first,second)
        self.assertEqual(first,{'wake_pending':True,'transport':'strict-submit:v1'})
        pending=[e for e in self.events(records) if e['event']=='submit_pending']
        self.assertEqual(len(pending),2)
        self.assertEqual(pending[0],self.diag('submit_pending',attempt='A1',phase='receipt',status='refused',reason='not_idle',
                                              mutation='false',landed='false',submitted='false'))
        self.assertEqual(pending[1],self.diag('submit_pending',attempt='A2',phase='receipt',status='refused',reason='not_idle',
                                              mutation='false',landed='false',submitted='false'))
        self.assertEqual(len({e['command'] for e in pending}),1)
        starts=[e for e in self.events(records) if e['event']=='attempt_start']
        self.assertEqual([e['attempt'] for e in starts],['A1','A2'])
        self.assertEqual(self.calls[0],self.calls[1])
        self.assertEqual(self.calls[0][self.calls[0].index('--deadline-ms')+1],'2000000')
    def test_slow_sink_never_blocks_control_path_or_effects(self):
        records=self.capture();delays=[1.0,1.0]
        class Slow(logging.Handler):
            def emit(self,record):
                if delays:time.sleep(delays.pop(0))
        logger=logging.getLogger(wake_send.STRICT_DIAG_LOGGER);slow=Slow();logger.addHandler(slow)
        self.addCleanup(lambda:logger.removeHandler(slow))
        self.mode.write_text('busy')
        began=time.perf_counter()
        self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        self.assertLess(time.perf_counter()-began,1.5)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        events=self.events(records,first=8.0,quiet=2.5)  # Quiet must exceed the slow handler's per-record delay.
        self.assertTrue(len(events)>=3)
        self.assertEqual(len(self.calls),1)  # The drained worker added no late native effect or ACK.
    def test_full_queue_drops_diagnostics_without_blocking_or_effects(self):
        records=self.capture()
        filler='wake-filler-not-a-diag'
        queued=0
        for _ in range(2000):
            try:wake_send._STRICT_DIAG_QUEUE.put_nowait(filler);queued+=1
            except queue.Full:break
        self.assertGreater(queued,0)
        self.assertLessEqual(wake_send._STRICT_DIAG_QUEUE.qsize(),wake_send._STRICT_DIAG_QUEUE.maxsize)
        self.mode.write_text('busy')
        began=time.perf_counter()
        self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        self.assertLess(time.perf_counter()-began,1.5)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertEqual(len([t for t in threading.enumerate() if t.name=='wake-strict-diag']),1)
    def test_first_worker_start_failure_keeps_attempt_results_and_singleton_recoverable(self):
        attempts=[]
        class Refusing:
            def __init__(self,*args,**kwargs):attempts.append(1)
            def start(self):raise RuntimeError('synthetic_thread_start_refused')
        fresh=[]
        self.mode.write_text('busy')
        with mock.patch.object(wake_send,'_STRICT_DIAG_WORKER_THREAD',fresh), \
                mock.patch.object(wake_send,'_STRICT_DIAG_WORKER_LOCK',threading.Lock()):
            with mock.patch.object(wake_send.threading,'Thread',Refusing):
                began=time.perf_counter()
                self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
                self.assertLess(time.perf_counter()-began,1.5)  # A refused first start never fails, blocks or changes the attempt.
            self.assertTrue(attempts)  # A first start was genuinely attempted, not skipped.
            self.assertEqual(fresh,[])  # The singleton marker is committed only after a successful start.
            started=[]
            class Recorded:
                def __init__(self,*args,**kwargs):started.append(self)
                def start(self):pass
                def is_alive(self):return True
            with mock.patch.object(wake_send.threading,'Thread',Recorded):
                wake_send._strict_diag_record('unknown','00000000000f','attempt_start','entry')  # A later emission retries startup safely.
            self.assertEqual(started,[fresh[0]])  # Recovery commits exactly one started worker marker, with no extra OS thread.
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
    def test_emission_never_waits_behind_startup_owner_or_hostile_emitter(self):
        held=threading.Lock();held.acquire();self.addCleanup(held.release)
        fresh=[]
        with mock.patch.object(wake_send,'_STRICT_DIAG_WORKER_THREAD',fresh), \
                mock.patch.object(wake_send,'_STRICT_DIAG_WORKER_LOCK',held):
            began=time.perf_counter()
            wake_send._strict_diag_record('unknown','00000000000e','attempt_start','entry')
            self.assertLess(time.perf_counter()-began,0.5)  # Drops instead of waiting behind the active startup owner.
            self.assertEqual(fresh,[])  # No worker is spawned while another emission owns startup.
        with mock.patch.object(wake_send,'_strict_diag_emit',side_effect=RuntimeError('emit_hostile')):
            self.assertIsNone(wake_send._strict_diag_record('unknown','00000000000d','attempt_start','entry'))
    def test_concurrent_commands_are_attributed_with_distinct_attempts(self):
        records=self.capture();self.mode.write_text('busy')
        errors={};results={};identities={0:'33333333-3333-4444-8555-666666666666',1:'44444444-3333-4444-8555-666666666666'}
        def run(index):
            bridge=None
            try:
                sub=Path(self.temp.name)/('conc-%d'%index)
                bridge=Bridge.__new__(Bridge)
                bridge.journal=Journal(sub/'journal.sqlite')
                db=bridge.journal.db
                db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES('op','s',1,'d',?,'task-hub2-s',?,'running',1)",(self.temp.name,NATIVE));db.commit()
                bridge.lookup=mock.Mock(side_effect=lambda *a,**k:copy.deepcopy({'id':NATIVE,'tag':'task-hub2-s','workspace':str(sub),'phase':'running','created_at_ms':1}))
                bridge.executable=sys.executable
                bridge.native=types.MethodType(Bridge.native,bridge)
                def runner(argv,**kwargs):
                    self.calls.append(tuple(argv[1:]))
                    return subprocess.run([sys.executable,'-I','-S',str(self.fake),*argv[1:]],**kwargs)
                bridge.runner=runner
                command=copy.deepcopy(self.command);command['id']=identities[index]
                results[index]=bridge.command(command)
            except Exception as error:
                errors[index]=error
            finally:
                if bridge is not None:bridge.journal.close_thread()
        threads=[threading.Thread(target=run,args=(index,)) for index in (0,1)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors,{})
        self.assertEqual(results,{0:{'wake_pending':True,'transport':'strict-submit:v1'},1:{'wake_pending':True,'transport':'strict-submit:v1'}})
        events=self.events(records,first=8.0,quiet=0.5)
        by_command={}
        for record in events:by_command.setdefault(record['command'],set()).add(record['attempt'])
        self.assertEqual(set(by_command),set(identities.values()))
        for command,attempts in by_command.items():
            self.assertEqual(len(attempts),1,command)
        joined=set().union(*by_command.values())
        self.assertEqual(len(joined),2)
        self.assertEqual(len([t for t in threading.enumerate() if t.name=='wake-strict-diag']),1)
    def test_receipt_flag_normalization_is_field_typed_and_public_result_exact(self):
        cases=[('flag:status-true',{'status':'unrecognized','reason':'submission_observed','mutation':'true','landed':'true','submitted':'true'}),
               ('flag:reason-false',{'status':'submitted_verified','reason':'unrecognized','mutation':'true','landed':'true','submitted':'true'}),
               ('flag:mutation-null',{'status':'submitted_verified','reason':'submission_observed','mutation':'unknown','landed':'true','submitted':'true'})]
        for mode,expected in cases:
            with self.subTest(mode=mode):
                records=self.capture();self.mode.write_text(mode)
                self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
                pending=[e for e in self.events(records) if e['event']=='submit_pending']
                self.assertEqual(len(pending),1)
                self.assertEqual(pending[0],self.diag('submit_pending',phase='receipt',**expected))
                self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        records=self.capture();self.mode.write_text('flag:replayed-null')
        with self.assertRaisesRegex(RuntimeError,'wake_receipt_unverified'):self.invoke()
        last=self.events(records)[-1]
        self.assertEqual(last,self.diag('receipt_unverified',phase='submit',reason='wake_receipt_unverified'))
        self.assertEqual(last['replayed'],'absent')
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
    def test_raising_str_exception_preserves_exact_original_object_and_state(self):
        class HostileError(Exception):
            def __str__(self):raise RuntimeError('str_must_never_be_called')
        hostile=HostileError('secret-payload-text')
        runner=self.bridge.runner
        def thrower(argv,**kwargs):
            self.calls.append(tuple(argv[1:]))
            if argv[1]=='send':raise hostile
            return runner(argv,**kwargs)
        self.bridge.runner=thrower
        records=self.capture()
        with self.assertRaises(HostileError) as caught:self.invoke()
        self.assertIs(caught.exception,hostile)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        last=self.events(records)[-1]
        self.assertEqual(last,self.diag('unrecognized',phase='submit',reason='unrecognized'))
        self.assertNotIn('secret-payload-text',json.dumps(last))
        records=self.capture();self.mode.write_text('busy')
        self.bridge.runner=runner
        with mock.patch.object(wake_send,'_strict_diag_correlation',side_effect=RuntimeError('construction_broken')):
            self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-submit:v1'})
        events=self.events(records)
        self.assertTrue(events)
        self.assertTrue(all(record['command']=='unknown' for record in events))
        self.assertEqual(len(self.calls),2)

class StrictInboxWakeTests(StrictDiagTools, unittest.TestCase):
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

    def test_inbox_custody_and_notice_diagnostics_are_closed(self):
        records=self.capture();result=self.invoke()
        events=self.events(records)
        self.assertEqual([e['event'] for e in events],['attempt_start','command_intent_committed','envelope_stored',
            'envelope_reread','submit_attempted','envelope_reread','submit_verified'])
        self.assertTrue(all(e['command']==self.command['id'] for e in events))
        self.assertEqual(events[-1],self.diag('submit_verified',phase='terminal',status='submitted_verified',
            reason='submission_observed',mutation='true',landed='true',submitted='true'))
        self.assertEqual(result['wake_proof']['kind'],'strict_inbox_notice_v1')
        row=self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()
        self.assertEqual(row[0],'done')
    def test_inbox_pending_journal_keeps_exact_envelope_progress_shape(self):
        records=self.capture();self.mode.write_text('busy')
        self.assertEqual(self.invoke(),{'wake_pending':True,'transport':'strict-inbox:v1'})
        row=self.bridge.journal.db.execute('SELECT state,result FROM commands').fetchone()
        self.assertEqual(row[0],'executing');self.assertEqual(set(json.loads(row[1])),{'inbox_envelope'})
        self.assertEqual(set(json.loads(row[1])['inbox_envelope']),
            {'schema_version','id','workspace','created_at','from','to','kind','body','data','delivery'})
        pending=[e for e in self.events(records) if e['event']=='submit_pending']
        self.assertEqual(pending,[self.diag('submit_pending',phase='receipt',status='refused',reason='not_idle',
                                            mutation='false',landed='false',submitted='false')])
    def test_inbox_context_unknown_creation_and_uncertain_retry_diagnostics_stay_closed(self):
        records=self.capture()
        self.mode.write_text('identity')
        with self.assertRaisesRegex(RuntimeError,'wake_local_context_unverified'):self.invoke()
        self.assertEqual(self.events(records)[-1],self.diag('context_refused',phase='context',reason='wake_local_context_unverified'))
        self.assertEqual(self.bridge.journal.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0],0)
        self.mode.write_text('create-unknown')
        with self.assertRaisesRegex(RuntimeError,'native_command_failed'):self.invoke()
        self.assertTrue((Path(self.temp.name)/'envelope.json').exists())
        events=self.events(records)
        self.assertEqual(events[-1],self.diag('unrecognized',phase='envelope',reason='unrecognized',attempt='A2'))
        self.assertTrue(any(e['event']=='command_intent_committed' for e in events))
        self.mode.write_text('ok')
        with self.assertRaisesRegex(RuntimeError,'wake_outcome_uncertain'):self.invoke()
        self.assertEqual(self.events(records)[-1],self.diag('outcome_uncertain',phase='context',reason='wake_outcome_uncertain',attempt='A3'))
        self.assertEqual(len(self.creations()),1)
    def test_inbox_positive_finish_rechecks_original_budget_after_terminal_diagnostic(self):
        records=self.capture();clock={'t':0.0}
        original=wake_send._strict_diag_record
        def burn_at_final(correlation,attempt,category,phase,**fields):
            if category=='submit_verified':clock['t']+=35.0  # Deterministic advance of only the final diagnostic preparation event.
            return original(correlation,attempt,category,phase,**fields)
        with mock.patch.object(wake_send.time,'monotonic',lambda:clock['t']), \
                mock.patch.object(wake_send,'_strict_diag_record',side_effect=burn_at_final):
            with self.assertRaisesRegex(RuntimeError,'wake_deadline_exceeded'):self.invoke()
        self.assertEqual(len(self.creations()),1)  # The one original business envelope; never a second creation.
        self.assertEqual(len([c for c in self.calls if c[0][0]=='send' and '--strict' in c[0]]),1)  # The original notice effect stays at most once.
        self.assertEqual(self.bridge.journal.db.execute('SELECT state FROM commands').fetchone()[0],'executing')
        self.assertEqual(self.bridge.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)  # No done result or positive ACK.
        self.assertEqual(self.events(records)[-1],self.diag('deadline_exceeded',phase='terminal',reason='wake_deadline_exceeded'))

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
