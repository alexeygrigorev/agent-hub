"""Standalone helper against disposable loopback HTTP, never production Hub."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/apply-model-policy.py'
spec=importlib.util.spec_from_file_location('policy_recovery_helper',SCRIPT)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
SHA='a'*40
CANDIDATE=dict(id='sol',engine='codex',model='gpt-6.1-sol',effort='medium',account_id='codex',weight=1,capabilities=[],enabled=True)


class RecoveryTests(unittest.TestCase):
 def setUp(self):
  parent=ROOT/'.local/tmp';parent.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(prefix='policy-recovery-',dir=parent);self.addCleanup(self.temp.cleanup)
  self.directory=Path(self.temp.name);self.receipt=self.directory/'receipt.json';self.config=self.directory/'policy.json'
  self.config.write_text(json.dumps({'candidates':[CANDIDATE]}))
  self.current={'version':0,'candidates':[]};self.calls=[];self.request_bytes=[];self.drop=False;self.compete=False;self.drop_rollback=False;self.operator_after_rollback_loss=False;self.conflicts=0;self.old_unconfigured=False;self.get_response=None;self.legacy_recovery=False;self.bad_preflight=False;self.lock=threading.Lock()
  outer=self
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args): pass
   def reply(self,status,value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
   def do_GET(self):
    with outer.lock:
     outer.calls.append(('GET',None))
     if outer.get_response is not None:
      self.reply(*outer.get_response);return
     if outer.current['candidates']:self.reply(200,outer.current)
     else:self.reply(404,{'error':'model_policy_unconfigured'} if outer.old_unconfigured else dict(error='model_policy_unconfigured',version=outer.current['version']))
   def read_mutation(self):
    raw=self.rfile.read(int(self.headers['Content-Length']))
    outer.request_bytes.append(raw)
    if len(raw)>98_304:
     self.reply(413,{'error':'body_too_large'});return None
    return json.loads(raw)
   def do_POST(self):
    value=self.read_mutation()
    if value is None:return
    with outer.lock:
     outer.calls.append(('POST',value))
     if outer.legacy_recovery:
      self.reply(403,{'error':'forbidden'});return
     action=value['action']
     assert value['protocol']==helper.RECOVERY_PROTOCOL
     if value['expected_version']!=outer.current['version']:
      self.reply(409,{'error':'model_policy_version_conflict'});return
     prior=helper.candidates(outer.current['candidates'],empty=True)
     if any('astra' in row['model'] and 'review-only' not in row['capabilities'] for row in prior):
      self.reply(409,{'error':'model_policy_prior_unrestorable'});return
     marker=dict(protocol=helper.RECOVERY_PROTOCOL,action=action)
     if action=='validate_recovery':
      if outer.bad_preflight:
       prior=[dict(CANDIDATE,weight=73)]
      self.reply(200,dict(marker,version=outer.current['version'],candidates=prior,
                          changed=prior!=value['candidates']))
      return
     assert action=='apply_recoverable'
     self.mutate(value,marker,prior)
   def do_PUT(self):
    value=self.read_mutation()
    if value is None:return
    with outer.lock:
     outer.calls.append(('PUT',value))
     self.mutate(value,{},outer.current['candidates'])
   def mutate(self,value,marker,prior):
    on_disk=json.loads(outer.receipt.read_text()) if outer.receipt.exists() else None
    if on_disk is not None:assert on_disk['phase'] in ('applying','rolling_back')
    if outer.compete:
     outer.compete=False;outer.current={'version':outer.current['version']+1,'candidates':value['candidates']}
     assert value['expected_version']!=outer.current['version'];outer.conflicts+=1
     self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
    if 'expected_version' in value and value['expected_version']!=outer.current['version']:
     self.reply(409,{'error':'model_policy_version_conflict'});return
    if not value['candidates'] and value.get('restore_unconfigured') is not True:
     self.reply(400,{'error':'invalid_model_policy'});return
    unchanged=value['candidates']==prior
    if not unchanged:
     outer.current={'version':outer.current['version']+1,'candidates':value['candidates']}
    if outer.drop or outer.drop_rollback and on_disk and on_disk['phase']=='rolling_back':
     outer.drop=False;outer.drop_rollback=False
     if outer.operator_after_rollback_loss:
      outer.current={'version':outer.current['version']+1,'candidates':[dict(CANDIDATE,weight=19)]}
     self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
    self.reply(200,dict(marker,version=outer.current['version'],candidates=value['candidates'],unchanged=unchanged))
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
  self.url='http://127.0.0.1:'+str(self.server.server_port)

 def run_helper(self,action,extra=(),receipt=True):
  args=[sys.executable,str(SCRIPT),action,'--url',self.url,'--file',str(self.config)]
  if receipt:args+=['--receipt',str(self.receipt),'--source-sha',SHA]
  result=subprocess.run(args+list(extra),capture_output=True,text=True,timeout=5,env={**os.environ,'HUB2_TOKEN':'synthetic-secret-token-'+'x'*40})
  self.assertEqual(result.stderr,'',result.stderr);self.assertNotIn('synthetic-secret-token',result.stdout)
  return result.returncode,json.loads(result.stdout)

 def mutation_count(self):
  return sum(method=='PUT' or method=='POST' and value['action']=='apply_recoverable'
             for method,value in self.calls)

 def test_commit_seals_receipt_without_policy_write_and_refuses_late_rollback(self):
  self.assertEqual(self.run_helper('prepare')[0],0)
  self.assertEqual(self.run_helper('apply')[0],0)
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('commit')[0],0)
  self.assertEqual(helper.saved(self.receipt)['phase'],'committed')
  self.assertEqual(self.run_helper('commit')[0],0)
  self.assertEqual(self.run_helper('rollback')[0],1)
  self.assertEqual(self.run_helper('apply')[0],1)
  self.assertEqual(self.mutation_count(),writes)

 def test_commit_refuses_prepared_or_superseded_policy_without_writing(self):
  self.assertEqual(self.run_helper('prepare')[0],0)
  self.assertEqual(self.run_helper('commit')[0],1)
  self.assertEqual(self.mutation_count(),0)
  self.assertEqual(self.run_helper('apply')[0],0)
  self.current['version']+=1
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('commit')[0],1)
  self.assertEqual(helper.saved(self.receipt)['phase'],'applied')
  self.assertEqual(self.mutation_count(),writes)

 def test_optional_transaction_binding_preserves_old_receipts_and_refuses_other_token(self):
  a=('--transaction-token','a'*32);b=('--transaction-token','b'*32)
  self.assertEqual(self.run_helper('prepare',a)[0],0)
  self.assertEqual(helper.saved(self.receipt)['transaction_token'],'a'*32)
  for action in ('prepare','apply','rollback','commit'):
   for extra in (b,()):
    self.assertEqual(self.run_helper(action,extra)[1]['error'],'recovery_transaction_mismatch')
  self.assertEqual(self.mutation_count(),0)
  self.assertEqual(self.run_helper('apply',a)[0],0)
  self.assertEqual(self.run_helper('commit',a)[0],0)
  self.assertEqual(self.run_helper('rollback',a)[0],1)

 def test_large_unicode_policies_round_trip_and_keep_the_applied_receipt_readable(self):
  prior=helper.candidates([
   dict(CANDIDATE,id=f'sol-{i}',capabilities=['🌐'*19+f'{j:02}' for j in range(32)])
   for i in range(32)
  ])
  desired=[dict(row,weight=2) for row in prior]
  self.current={'version':4,'candidates':prior}
  self.config.write_bytes(helper.compact_utf8({'candidates':desired}))
  rollback={'candidates':prior,'expected_version':5}
  self.assertGreater(len(helper.canonical(rollback)),128000)
  self.assertLessEqual(len(helper.compact_utf8(rollback)),98_304)
  self.assertEqual(self.run_helper('prepare')[0],0)
  self.assertEqual(self.run_helper('apply')[0],0)
  saved=helper.saved(self.receipt)
  self.assertEqual(saved['phase'],'applied')
  self.assertEqual(saved['source_digest'],helper.hashlib.sha256(helper.canonical(desired).encode()).hexdigest())
  self.assertGreater(len(helper.canonical(saved)),helper.LIMIT)
  self.assertEqual(self.run_helper('rollback')[0],0)
  self.assertEqual(self.current,{'version':6,'candidates':prior})
  self.assertIn('🌐'.encode(),self.request_bytes[-1])
  self.assertEqual(self.request_bytes[-1],helper.compact_utf8(rollback))

 def test_oversized_rollback_refuses_new_and_legacy_prepared_receipts(self):
  prior=[dict(CANDIDATE,id=f'sol-{i}',capabilities=[f'cap-{j}' for j in range(32)]) for i in range(32)]
  remaining=98_304-len(helper.compact_utf8({'candidates':prior}))
  for row in prior:
   for j in range(len(row['capabilities'])):
    escapes=min(70,remaining//6)
    suffix=(remaining-escapes*6)%6
    row['capabilities'][j]+='\0'*escapes+'x'*suffix
    remaining-=escapes*6+suffix
  prior=helper.candidates(prior)
  self.assertEqual(len(helper.compact_utf8({'candidates':prior})),98_304)
  self.current={'version':4,'candidates':prior}
  self.assertEqual(self.run_helper('prepare')[1]['error'],'rollback_policy_body_too_large')
  self.assertFalse(self.receipt.exists())
  # An older helper could save this before checking the actual rollback envelope.
  old_receipt=dict(format=1,source_sha=SHA,url=self.url,desired=[CANDIDATE],before=self.current,
                   source_digest=helper.hashlib.sha256(helper.canonical([CANDIDATE]).encode()).hexdigest(),
                   phase='prepared',applied=None,changed=True)
  self.receipt.write_text(helper.canonical(old_receipt))
  self.receipt.chmod(0o600)
  for action in ('prepare','apply'):
   self.assertEqual(self.run_helper(action)[1]['error'],'rollback_policy_body_too_large')
   self.assertEqual(helper.saved(self.receipt),old_receipt)
  self.assertEqual(self.request_bytes,[])
  self.assertEqual(self.current['version'],4)

 def test_wire_encoder_rejects_unpaired_surrogates(self):
  with self.assertRaisesRegex(helper.Problem,'invalid_wire_json'):
   helper.wire_body({'candidates':['\ud800']})

 def test_prepare_refuses_invalid_legacy_prior_without_saving_receipt_or_mutating(self):
  self.current={'version':4,'candidates':[dict(CANDIDATE,id='astra',model='gpt-6.1-astra')]}
  code,result=self.run_helper('prepare')
  self.assertEqual(code,1)
  self.assertEqual(result['error'],'rollback_policy_unrestorable')
  self.assertFalse(self.receipt.exists())
  self.assertEqual(self.mutation_count(),0)
  self.assertEqual(self.current['version'],4)

 def test_legacy_server_cannot_prepare_or_apply_a_recovery_receipt(self):
  self.legacy_recovery=True
  self.assertEqual(self.run_helper('prepare')[0],1)
  self.assertFalse(self.receipt.exists())
  self.assertEqual(self.mutation_count(),0)
  self.legacy_recovery=False
  self.assertEqual(self.run_helper('prepare')[0],0)
  self.legacy_recovery=True
  self.assertEqual(self.run_helper('apply')[0],1)
  self.assertEqual(self.current,{'version':0,'candidates':[]})
  self.assertFalse(any(method=='PUT' for method,_ in self.calls))
  self.assertIsNone(json.loads(self.receipt.read_text())['applied'])

 def test_prepare_requires_the_same_normalized_baseline_at_the_same_version(self):
  self.current={'version':4,'candidates':[CANDIDATE]}
  self.bad_preflight=True
  self.assertEqual(self.run_helper('prepare')[0],1)
  self.assertFalse(self.receipt.exists())
  self.assertEqual(self.mutation_count(),0)

 def test_initial_apply_restore_reapply_and_replays_have_no_aba(self):
  self.assertEqual(self.run_helper('prepare')[0],0);self.assertEqual(self.receipt.stat().st_mode&0o777,0o600)
  self.assertNotIn('synthetic-secret-token',self.receipt.read_text())
  self.assertEqual(self.run_helper('apply')[0],0);self.assertEqual(self.current['version'],1)
  self.assertEqual(self.run_helper('apply')[0],0);self.assertEqual(self.current['version'],1)
  self.assertEqual(self.run_helper('rollback')[0],0);self.assertEqual(self.current,{'version':2,'candidates':[]})
  self.assertEqual(self.run_helper('rollback')[0],0);self.assertEqual(self.current['version'],2)
  self.receipt=self.directory/'second.json'
  self.assertEqual(self.run_helper('prepare')[0],0);self.assertEqual(self.run_helper('apply')[0],0)
  self.assertEqual(self.current['version'],3)

 def test_unchanged_candidate_never_rolls_back_an_operator_change(self):
  self.current={'version':5,'candidates':[CANDIDATE]}
  self.run_helper('prepare');self.assertEqual(self.run_helper('apply')[0],0)
  self.current={'version':6,'candidates':[dict(CANDIDATE,weight=7)]}
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('rollback')[0],0)
  self.assertEqual(self.current['version'],6);self.assertEqual(self.mutation_count(),writes)

 def test_operator_change_after_apply_is_preserved(self):
  self.run_helper('prepare');self.run_helper('apply')
  self.current={'version':2,'candidates':[dict(CANDIDATE,weight=9)]}
  self.assertEqual(self.run_helper('rollback')[0],1);self.assertEqual(self.current['candidates'][0]['weight'],9)
  self.assertEqual(json.loads(self.receipt.read_text())['phase'],'applied')

 def test_lost_apply_response_observes_without_acquiring_write_ownership(self):
  self.run_helper('prepare');self.drop=True
  rc,result=self.run_helper('apply');self.assertEqual(rc,1);self.assertTrue(result['manual_reconciliation_required'])
  self.assertEqual(self.current['version'],1);self.assertEqual(json.loads(self.receipt.read_text())['phase'],'uncertain')
  self.assertEqual(self.run_helper('rollback')[0],1);self.assertEqual(self.current['version'],1)

 def test_competing_identical_write_and_lost_conflict_never_grants_ownership(self):
  self.current={'version':1,'candidates':[dict(CANDIDATE,weight=2)]}
  self.run_helper('prepare');self.compete=True
  self.assertEqual(self.run_helper('apply')[0],1)
  self.assertEqual(self.current,{'version':2,'candidates':[CANDIDATE]})
  self.assertEqual(self.run_helper('rollback')[0],1)
  self.assertEqual(self.current['version'],2);self.assertEqual(self.conflicts,1)

 def test_lost_rollback_response_is_resolved_without_another_write(self):
  self.run_helper('prepare');self.run_helper('apply');self.drop_rollback=True
  self.assertEqual(self.run_helper('rollback')[0],0)
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('rollback')[0],0)
  self.assertEqual(self.mutation_count(),writes)
  self.assertEqual(self.current,{'version':2,'candidates':[]})

 def test_lost_rollback_followed_by_operator_write_never_reissues_restore(self):
  self.run_helper('prepare');self.run_helper('apply');self.drop_rollback=True;self.operator_after_rollback_loss=True
  self.assertEqual(self.run_helper('rollback')[0],1)
  self.assertEqual(self.current,{'version':3,'candidates':[dict(CANDIDATE,weight=19)]})
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('rollback')[0],1)
  self.assertEqual(self.mutation_count(),writes)
  self.assertEqual(self.current['candidates'][0]['weight'],19)

 def test_receipt_write_failure_before_put_prevents_mutation(self):
  self.run_helper('prepare')
  args=SimpleNamespace(action='apply',receipt=self.receipt,source_sha=SHA,url=self.url,file=self.config)
  with mock.patch.object(helper,'save',side_effect=OSError('synthetic disk failure')):
   with self.assertRaises(OSError):helper.recovery(args,'synthetic-token-'+'x'*40)
  self.assertEqual(self.mutation_count(),0)
  self.assertEqual(self.current['version'],0)

 def test_crash_after_success_before_receipt_persistence_cannot_claim_ownership(self):
  self.run_helper('prepare');save=helper.save
  def fault(path,data):
   if data['phase']=='applied':raise OSError('synthetic lost durable receipt')
   return save(path,data)
  args=SimpleNamespace(action='apply',receipt=self.receipt,source_sha=SHA,url=self.url,file=self.config)
  with mock.patch.object(helper,'save',side_effect=fault):
   with self.assertRaises(OSError):helper.recovery(args,'synthetic-token-'+'x'*40)
  self.assertEqual(self.current['version'],1)
  self.assertEqual(self.run_helper('apply')[0],1)
  self.assertEqual(self.run_helper('rollback')[0],1)
  self.assertEqual(self.current['version'],1)

 def test_crash_replays_before_mutation_and_after_confirmed_response(self):
  self.run_helper('prepare');record=json.loads(self.receipt.read_text());record['phase']='applying';self.receipt.write_text(json.dumps(record))
  self.assertEqual(self.run_helper('apply')[0],0)
  record=json.loads(self.receipt.read_text());record['phase']='rolling_back';self.receipt.write_text(json.dumps(record))
  self.current={'version':2,'candidates':[]}
  writes=self.mutation_count()
  self.assertEqual(self.run_helper('rollback')[0],0);self.assertEqual(self.mutation_count(),writes)

 def test_identity_corruption_and_unsafe_receipts_refuse_before_put(self):
  self.run_helper('prepare');original=self.receipt.read_text()
  for extras in (['--source-sha','b'*40],['--url','http://127.0.0.1:1']):
   self.assertEqual(self.run_helper('apply',extras)[0],1)
  self.config.write_text(json.dumps({'candidates':[dict(CANDIDATE,weight=4)]}))
  self.assertEqual(self.run_helper('apply')[0],1)
  self.receipt.write_text('{bad');self.assertEqual(self.run_helper('rollback')[0],1)
  self.receipt.unlink();outside=self.directory/'untouched';outside.write_text(original);self.receipt.symlink_to(outside)
  self.assertEqual(self.run_helper('prepare')[0],1);self.assertEqual(outside.read_text(),original)
  self.receipt.unlink();os.mkfifo(self.receipt,0o600)
  self.assertEqual(self.run_helper('prepare')[0],1)
  self.assertFalse(any(method=='PUT' for method,_ in self.calls))

 def test_concurrent_helper_invocations_do_not_duplicate_apply(self):
  self.run_helper('prepare')
  results=[]
  threads=[threading.Thread(target=lambda:results.append(self.run_helper('apply')[0])) for _ in range(2)]
  for thread in threads:thread.start()
  for thread in threads:thread.join(timeout=5);self.assertFalse(thread.is_alive())
  self.assertEqual(results,[0,0]);self.assertEqual(self.current['version'],1)
  self.assertEqual(self.mutation_count(),1)

 def test_prepare_http_errors_refuse_before_success_payload_validation(self):
  for status,error in ((401,'unauthorized'),(403,'forbidden'),(503,'unavailable')):
   with self.subTest(status=status):
    self.calls.clear();self.get_response=(status,{'error':error})
    rc,result=self.run_helper('prepare')
    self.assertEqual(rc,1)
    self.assertEqual(result,{'error':f'policy_read_failed:{status}','manual_reconciliation_required':True})
    self.assertEqual(self.calls,[('GET',None)])
    self.assertEqual(self.request_bytes,[])
    self.assertFalse(self.receipt.exists())
    self.assertEqual(self.current,{'version':0,'candidates':[]})

 def test_prepare_success_with_malformed_version_still_refuses_without_receipt(self):
  for bad in (None,True,-1,'1'):
   with self.subTest(version=bad):
    self.calls.clear();self.get_response=(200,{'version':bad,'candidates':[CANDIDATE]})
    rc,result=self.run_helper('prepare')
    self.assertEqual(rc,1)
    self.assertEqual(result['error'],'invalid_version')
    self.assertEqual(self.calls,[('GET',None)])
    self.assertEqual(self.request_bytes,[])
    self.assertFalse(self.receipt.exists())
    self.assertEqual(self.current,{'version':0,'candidates':[]})

 def test_only_exact_unconfigured_404_retains_versioned_empty_snapshot(self):
  self.assertEqual(helper.snapshot(404,{'error':'model_policy_unconfigured','version':3}),
                   {'version':3,'candidates':[]})
  for status,payload in ((401,{'error':'model_policy_unconfigured','version':0}),
                         (404,{'error':'unauthorized'}),(503,None)):
   with self.subTest(status=status,payload=payload),self.assertRaisesRegex(helper.Problem,f'^policy_read_failed:{status}$'):
    helper.snapshot(status,payload)

 def test_old_unconfigured_response_without_version_cannot_prepare_recovery(self):
  self.old_unconfigured=True
  rc,result=self.run_helper('prepare')
  self.assertEqual(rc,1);self.assertTrue(result['manual_reconciliation_required'])
  self.assertFalse(self.receipt.exists())
  self.assertFalse(any(method=='PUT' for method,_ in self.calls))
  self.assertEqual(self.run_helper('show',receipt=False)[0],1)
  self.assertEqual(self.run_helper('apply',receipt=False)[0],0)

 def test_legacy_show_apply_and_url_validation(self):
  self.assertEqual(self.run_helper('apply',receipt=False)[0],0)
  self.assertEqual(self.run_helper('show',receipt=False)[0],0)
  for value in ('https://token@hub.example','https://hub.example/path','https://hub.example?token=x','http://hub.example'):
   with self.assertRaises(helper.Problem):helper.url(value)


if __name__=='__main__':unittest.main()
