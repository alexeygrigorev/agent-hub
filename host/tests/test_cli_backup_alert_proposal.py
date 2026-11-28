"""Source-only partial hooks. Contract fixture is NOT Root's client extension."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
PRIVATE=ROOT.parent
BASE=PRIVATE/'base' if (PRIVATE/'pins.json').is_file() else None
_spec=importlib.util.spec_from_file_location('backup_frame_schema_only',ROOT/'scripts/hub_alert_client.py')
_schema=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_schema)

# This test-only session records partial claims without a broker or any counter.
# It deliberately implements a PROPOSED CLI, not a copied/forked client core.
SESSION='# TEST ONLY. No real client, broker, authority, counters or business spawning.\nimport argparse,json,os,re,sys,uuid\np=argparse.ArgumentParser();p.add_argument(\'operation\',choices=[\'source-session\'])\np.add_argument(\'--role\',choices=[\'backup_pull\',\'backup_notifier\'],required=True);p.add_argument(\'--socket\',required=True)\np.add_argument(\'--parent-source-claim\');p.add_argument(\'--lifetime-seconds\',type=int,default=900)\na=p.parse_args()\nassert 1<=a.lifetime_seconds<=900\nassert a.parent_source_claim is None or a.role==\'backup_notifier\' and re.fullmatch(\'[0-9a-f]{32}\',a.parent_source_claim)\nmode=os.environ.get(\'SESSION_MODE\',\'negative\');context=uuid.uuid4().hex\nlog=os.environ[\'SESSION_LOG\']\ndef append(value):\n with open(log,\'a\') as f:f.write(json.dumps(value)+\'\\n\')\ndef send(value):print(json.dumps(value,separators=(\',\',\':\')),flush=True)\nif mode==\'exit_before_ready\':raise SystemExit(2)\nif mode==\'bad_ready\':print(\'NOT_READY\',flush=True);raise SystemExit(2)\nappend({\'kind\':\'source_claim\',\'qualification\':\'none\',\'complete\':False,\'pid\':os.getpid(),\'context\':context,\'parent\':a.parent_source_claim,\'role\':a.role})\nready={\'kind\':\'hub_alert_source_session_v1\',\'state\':\'ready_unqualified\',\'role\':a.role,\'admitted\':False,\'source_claim_id\':context,\'parent_source_claim\':a.parent_source_claim}\nif mode==\'ready_reordered\':ready=dict(reversed(list(ready.items())))\nif mode==\'ready_admitted\':ready[\'admitted\']=True\nif mode==\'ready_unknown\':ready[\'extra\']=\'refuse\'\nif mode==\'ready_wrong_role\':ready[\'role\']=\'canary\'\nif mode==\'ready_hyphenated\':ready[\'source_claim_id\']=str(uuid.UUID(hex=context))\nif mode==\'ready_duplicate\':print(json.dumps(ready)[:-1]+\',"admitted":false}\',flush=True)\nelif mode==\'ready_nul\':sys.stdout.buffer.write(json.dumps(ready).encode().replace(b\'false\',b\'fa\\x00lse\')+b\'\\n\');sys.stdout.flush()\nelif mode==\'ready_invalid_utf8\':sys.stdout.buffer.write(b\'\\xff\\n\');sys.stdout.flush()\nelif mode==\'ready_oversized\':print(\'x\'*513,flush=True)\nelse:send(ready)\nseq=0;dirty=False\nfor line in sys.stdin:\n v=json.loads(line)\n assert set(v)<= {\'phase\',\'decision\',\'condition\',\'flags\',\'recipient_class\'}\n frame={\'v\':1,\'invocation_id\':context,\'parent_id\':a.parent_source_claim,\'role\':a.role,\'phase\':v[\'phase\'],\'seq\':seq,\'decision\':v.get(\'decision\'),\'condition\':v.get(\'condition\'),\'flags\':v.get(\'flags\',{}),\'recipient_class\':v.get(\'recipient_class\',\'none\'),\'observer_ok\':not dirty}\n append({\'kind\':\'partial_frame\',\'qualification\':\'none\',\'complete\':False,\'pid\':os.getpid(),\'frame\':frame})\n ack={\'kind\':\'hub_alert_source_session_ack_v1\',\'seq\':seq,\'local_ok\':True,\'broker_ok\':False,\'observer_ok\':False,\'admitted\':False}\n if mode==\'ack_bool_seq\':ack[\'seq\']=False\n if mode==\'ack_string_seq\':ack[\'seq\']=str(seq)\n if mode==\'ack_string_bool\':ack[\'local_ok\']=\'true\'\n if mode==\'ack_admitted\':ack[\'admitted\']=True\n if mode==\'ack_unknown\':ack[\'extra\']=False\n if mode==\'stale_ack\' and seq>0:ack[\'seq\']=seq-1\n if mode==\'healing_ack\' and seq>0:ack[\'broker_ok\']=True;ack[\'observer_ok\']=True\n seq+=1;dirty=True\n if mode==\'drop_decision_reply\' and v[\'phase\']==\'decision\':continue\n if mode==\'ack_reordered\':ack=dict(reversed(list(ack.items())))\n send(ack)\n if v[\'phase\']==\'terminal\' or mode==\'exit_after_begin\':break\n'
MTA='''#!/usr/bin/env python3
import json,os,sys
records=[]
try:
 with open(os.environ['SESSION_LOG']) as f:records=[json.loads(line) for line in f]
except FileNotFoundError:pass
prior=any(v.get('kind')=='partial_frame' and v['frame']['role']=='backup_notifier' and v['frame']['phase']=='decision' for v in records)
with open(os.environ['MAIL_LOG'],'a') as f:f.write(json.dumps({'body':sys.stdin.read(),'decision_before_mta':prior})+'\\n')
'''

class SourceProposalTests(unittest.TestCase):
 def setUp(self):
  # The real source loader refuses every world-writable ancestor, including
  # sticky /tmp. CI's disposable TMPDIR may live there; fixture source belongs
  # under this checkout, without relaxing the loader's custody checks.
  parent=ROOT/'.local/backup-alert-tests'
  parent.mkdir(parents=True,exist_ok=True)
  self.tmp=tempfile.TemporaryDirectory(prefix='backup-v2-',dir=parent);self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.bin=self.root/'bin';self.bin.mkdir()
  for name in ('sendmail','msmtp'):
   p=self.bin/name;p.write_text(MTA);p.chmod(0o700)
  for name in ('ssh','rsync'):
   p=self.bin/name;p.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name}" >> "$BUSINESS_LOG"\nexit "${{FAKE_{name.upper()}_RC:-0}}"\n');p.chmod(0o700)

 def run_source(self, *, tree='candidate',role='notifier',fresh=False,state=None,args=(),client='contract',**opts):
  run=self.root/('run-'+str(len(list(self.root.iterdir()))));run.mkdir();scripts=run/'scripts';scripts.mkdir()
  source=ROOT if tree=='candidate' else BASE
  for name in ('hub2-backup-pull.sh','hub-db-backup-staleness.sh'):
   p=scripts/name;p.write_bytes((source/'scripts'/name).read_bytes());p.chmod(0o700)
  p=scripts/'hub_alert_client.py'
  p.write_text(SESSION) if client=='contract' else p.write_bytes((ROOT/'scripts/hub_alert_client.py').read_bytes())
  p.chmod(0o600)
  mutation=opts.pop('CLIENT_MUTATION',None)
  if mutation=='group_write':p.chmod(0o620)
  elif mutation=='symlink':
   target=p.with_name('own-fixture-client.py');p.rename(target);p.symlink_to(target)
  elif mutation=='hardlink':os.link(p,p.with_name('own-fixture-hardlink.py'))
  for name in ('local','box','state'):(run/name).mkdir()
  if fresh:(run/'local/hub2-db-fixture.tar.gz').write_bytes(b'fixture')
  if state is not None:(run/'state/staleness.state').write_text(state)
  for name in ('sendmail','msmtp','ssh','rsync'):
   self.assertTrue(os.access(self.bin/name,os.X_OK),'fixture must fail closed before business command')
  env={'PATH':str(self.bin)+':/usr/bin:/bin','HOME':str(run),'TMPDIR':str(run),
   'HUB_DB_BACKUP_ENV_FILE':'/dev/null','BACKUP_DIR':str(run/'local'),'STORAGEBOX_DIR':str(run/'box'),
   'STATE_DIR':str(run/'state'),'BACKUP_GLOB':'hub2-db-*.tar.gz',
   'ALERT_TO':'dev@dtcdev.click','ALERT_OPS_TO':'dev@dtcdev.click',
   'MAIL_LOG':str(run/'mail.jsonl'),'SESSION_LOG':str(run/'sessions.jsonl'),'BUSINESS_LOG':str(run/'business.log'),
   'HUB_BACKUP_ALERT_BROKER_SOCKET':str(run/'unopened.sock'),
   'HUB_BACKUP_ALERT_CLIENT_SHA256':hashlib.sha256(p.read_bytes()).hexdigest()}
  private_fd=os.open(run,os.O_RDONLY|os.O_DIRECTORY)
  env['HUB_BACKUP_ALERT_BROKER_SOCKET']=f'/proc/{os.getpid()}/fd/{private_fd}/missing-observer.sock'
  env.update(opts)
  if role=='pull':
   f=run/'pull.env';f.write_text('\n'.join(['VM_HOST=fixture.invalid','SSH_KEY=/fixture-not-a-key',
    'BACKUP_DIR='+str(run/'local'),'STORAGEBOX_DIR='+str(run/'box'),'STATE_DIR='+str(run/'state'),
    'STALENESS_SCRIPT='+str(scripts/'hub-db-backup-staleness.sh')])+'\n')
   env['HUB2_BACKUP_PULL_ENV_FILE']=str(f)
  name='hub2-backup-pull.sh' if role=='pull' else 'hub-db-backup-staleness.sh'
  start=time.monotonic()
  try:r=subprocess.run(['bash',str(scripts/name),*args],env=env,capture_output=True,text=True,timeout=8)
  finally:os.close(private_fd)
  def read(name):
   p=run/name;return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
  return {'rc':r.returncode,'out':r.stdout,'err':r.stderr,'elapsed':time.monotonic()-start,
   'mail':read('mail.jsonl'),'events':read('sessions.jsonl'),'root':run,
   'business':(run/'business.log').read_text().splitlines() if (run/'business.log').exists() else [],
   'state':(run/'state/staleness.state').read_text() if (run/'state/staleness.state').exists() else None}

 def test_partial_logical_decisions_preserve_original_operational_results(self):
  old='WAS_STALE=1\nLAST_ALERT='+str(int(time.time()))+'\nDRILL=0\n'
  cases=[({},'alert',1,2),({'fresh':True},'observation',0,0),
   ({'state':old},'alert_dedup',0,2),({'state':old,'fresh':True},'recovery',1,0),
   ({'state':old,'fresh':True,'args':['--no-mail']},'recovery',0,0),
   ({'args':['--backup-failed','fixture']},'alert',1,0),
   ({'args':['--backup-failed','fixture','--no-mail']},'alert',0,0),
   ({'args':['--no-mail']},'alert',0,2),({'args':['--dry-run']},'alert',0,2),
   ({'args':['--backup-failed','[TEST] fixture']},'observation',0,0),
   ({'HUB_DB_STALE_DRILL':'1'},'observation',0,2),
   ({'state':old.replace('DRILL=0','DRILL=1'),'fresh':True},'observation',0,0),
   ({'state':old.replace('DRILL=0','DRILL=1'),'args':['--backup-failed','fixture']},'alert',1,0),
   ({'ALERT_TO':'other@invalid.test'},'observation',1,2)]
  for options,decision,mails,rc in cases:
   with self.subTest(options=options):
    r=self.run_source(**options)
    self.assertEqual((r['rc'],len(r['mail'])),(rc,mails),r)
    frames=[e['frame'] for e in r['events'] if e['kind']=='partial_frame']
    self.assertEqual([f['phase'] for f in frames],['begin','decision','terminal'],r)
    self.assertEqual(frames[1]['decision'],decision)
    self.assertEqual([f['seq'] for f in frames],[0,1,2])
    for frame in frames:
     self.assertEqual(_schema.checked(frame),frame)
     self.assertLess(len(json.dumps(frame).encode()),1024)
    if mails:self.assertTrue(r['mail'][0]['decision_before_mta'])
    self.assertTrue(all(e['complete'] is False and e['qualification']=='none' for e in r['events']))
    self.assertEqual(len({e['pid'] for e in r['events']}),1)
    if BASE:
     b=self.run_source(tree='base',**options)
     normalize=lambda text:re.sub(r'LAST_ALERT=[1-9][0-9]*','LAST_ALERT=positive',text or '')
     self.assertEqual((r['rc'],len(r['mail']),normalize(r['state'])),(b['rc'],len(b['mail']),normalize(b['state'])))

 def test_world_writable_tmpdir_does_not_disable_source_contract(self):
  unsafe=self.root/'world-writable-temp';unsafe.mkdir(mode=0o700);unsafe.chmod(0o1777)
  case=type(self)('test_reviewed_real_client_missing_broker_keeps_business_once')
  self.addCleanup(case.doCleanups)
  with mock.patch.dict(os.environ,{'TMPDIR':str(unsafe)}):
   case.setUp()
  self.assertFalse(case.root.is_relative_to(unsafe))
  r=case.run_source(args=['--backup-failed','fixture'])
  self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
  self.assertEqual([e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'],
   ['begin','decision','terminal'],r)

 def test_reviewed_real_client_missing_broker_keeps_business_once(self):
  r=self.run_source(client='accepted',args=['--backup-failed','fixture'])
  self.assertEqual((r['rc'],len(r['mail']),r['events']),(0,1,[]),r)

 def test_bad_hash_or_missing_opt_in_does_not_start_session(self):
  for options in ({'HUB_BACKUP_ALERT_CLIENT_SHA256':'0'*64},{'HUB_BACKUP_ALERT_BROKER_SOCKET':''}):
   r=self.run_source(args=['--backup-failed','fixture'],**options)
   self.assertEqual((r['rc'],len(r['mail']),r['events']),(0,1,[]),r)

 def test_observer_start_failure_never_reexecutes_business(self):
  for mode in ('exit_before_ready','bad_ready'):
   r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE=mode)
   self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
   self.assertLess(r['elapsed'],2)

 def test_missing_local_reply_is_bounded_and_keeps_terminal_dirty(self):
  r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE='drop_decision_reply')
  self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
  self.assertLess(r['elapsed'],2)
  self.assertEqual(r['events'][-1]['frame']['phase'],'decision')
  self.assertNotIn('terminal',[e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'])

 def test_observer_pipe_close_does_not_sigpipe_original_business(self):
  r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE='exit_after_begin')
  self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
  self.assertEqual([e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'],['begin'])

 def test_actual_nested_notifier_runs_once_with_distinct_stable_helper_claim(self):
  for ssh_rc in ('0','47'):
   r=self.run_source(role='pull',fresh=True,FAKE_SSH_RC=ssh_rc)
   self.assertEqual((r['rc'],len(r['mail'])),(int(ssh_rc),int(ssh_rc!='0')),r)
   self.assertEqual(r['business'],['ssh','rsync','rsync'] if ssh_rc=='0' else ['ssh'])
   claims=[e for e in r['events'] if e['kind']=='source_claim']
   self.assertEqual(len(claims),2,r)
   parent=next(c for c in claims if c['role']=='backup_pull')
   child=next(c for c in claims if c['role']=='backup_notifier')
   self.assertNotEqual(parent['pid'],child['pid']);self.assertNotEqual(parent['context'],child['context'])
   self.assertEqual(child['parent'],parent['context'])
   for claim in claims:
    frames=[e for e in r['events'] if e['kind']=='partial_frame' and e['frame']['invocation_id']==claim['context']]
    self.assertEqual([e['frame']['phase'] for e in frames],['begin','decision','terminal'])
    self.assertEqual({e['pid'] for e in frames},{claim['pid']})

 def test_setup_failure_does_not_invent_failure_mail(self):
  r=self.run_source(role='pull',RETENTION_DAYS='29')
  self.assertNotEqual(r['rc'],0);self.assertEqual(r['mail'],[])
  self.assertFalse(r['events'][-1]['frame']['flags']['decision_seen'])

 def test_ready_json_wrong_identity_types_or_fields_fail_open(self):
  modes=('ready_admitted','ready_unknown','ready_wrong_role','ready_hyphenated','ready_duplicate','ready_nul','ready_invalid_utf8','ready_oversized')
  for mode in modes:
   with self.subTest(mode=mode):
    r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE=mode)
    self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
    self.assertEqual([e for e in r['events'] if e['kind']=='partial_frame'],[])

 def test_ack_requires_typed_seq_and_booleans_not_admission(self):
  for mode in ('ack_bool_seq','ack_string_seq','ack_string_bool','ack_admitted','ack_unknown'):
   with self.subTest(mode=mode):
    r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE=mode)
    self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
    self.assertEqual([e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'],['begin'])

 def test_order_independent_json_ready_and_ack(self):
  for mode in ('ready_reordered','ack_reordered'):
   r=self.run_source(fresh=True,SESSION_MODE=mode)
   self.assertEqual((r['rc'],len(r['mail'])),(0,0),r)
   self.assertEqual([e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'],['begin','decision','terminal'])

 def test_stale_or_healing_ack_closes_only_observer(self):
  for mode in ('stale_ack','healing_ack'):
   r=self.run_source(args=['--backup-failed','fixture'],SESSION_MODE=mode)
   self.assertEqual((r['rc'],len(r['mail'])),(0,1),r)
   self.assertEqual([e['frame']['phase'] for e in r['events'] if e['kind']=='partial_frame'],['begin','decision'])

 def test_immutable_selection_rejects_unsafe_leaf_before_execution(self):
  for mutation in ('group_write','symlink','hardlink'):
   r=self.run_source(args=['--backup-failed','fixture'],CLIENT_MUTATION=mutation)
   self.assertEqual((r['rc'],len(r['mail']),r['events']),(0,1,[]),r)

 def test_cleanup_uses_one_coherent_job_snapshot_and_never_waits_on_transition(self):
  # Extract only the cleanup function; all jobs/wait are pure fakes. No real
  # helper, process signal, backup, broker or foreign PID is involved.
  for name in ('hub-db-backup-staleness.sh','hub2-backup-pull.sh'):
   text=(ROOT/'scripts'/name).read_text()
   begin=text.index('backup_alert_close() {')
   function=text[begin:text.index('\nbackup_alert_reply()',begin)]
   for scenario in ('running','stopped','stopped_then_running','running_then_stopped','completed','unknown','malformed'):
    with self.subTest(script=name,scenario=scenario):
     case=self.root/(name+'-'+scenario);case.mkdir()
     driver=r'''set -euo pipefail
_BACKUP_ALERT_WRITE=''; _BACKUP_ALERT_READ=''; _BACKUP_ALERT_PID=424242
jobs() {
  printf '%s\n' "$1" >> "$JOBS_LOG"
  case "$1" in
    -pr|-ps) return 0 ;; # The old split-snapshot transition can omit both.
    -p)
      case "$SCENARIO" in
        completed) return 0 ;;
        unknown) return 2 ;;
        malformed) printf '%s\n' not_a_pid ;;
        *) printf '11111\n424242\n22222\n' ;;
      esac ;;
    *) return 2 ;;
  esac
}
wait() { printf '%s\n' "$1" >> "$WAIT_LOG"; }
'''+function+'\nbackup_alert_close\n'
     env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'SCENARIO':scenario,
          'JOBS_LOG':str(case/'jobs'),'WAIT_LOG':str(case/'wait')}
     result=subprocess.run(['bash','-c',driver],env=env,capture_output=True,text=True,timeout=1)
     self.assertEqual(result.returncode,0,result.stderr)
     self.assertEqual((case/'jobs').read_text().splitlines(),['-p'])
     waited=(case/'wait').read_text().splitlines() if (case/'wait').exists() else []
     self.assertEqual(waited,['424242'] if scenario=='completed' else [])

if __name__=='__main__':unittest.main(verbosity=2)
