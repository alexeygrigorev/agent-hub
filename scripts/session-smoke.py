#!/usr/bin/env python3
"""Exercise the deployed Hub through its public CLI; stop only the session created here."""
import argparse,json,os,pathlib,subprocess,sys,tempfile,uuid
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--machine',required=True);p.add_argument('--workspace',required=True)
p.add_argument('--engine',default='opencode');p.add_argument('--timeout',type=int,default=180)
a=p.parse_args();hub=pathlib.Path(__file__).resolve().with_name('hub');key='smoke-'+uuid.uuid4().hex
session=None;stopped=False

def call(*args):
 r=subprocess.run([str(hub),*args],capture_output=True,text=True,timeout=a.timeout+30)
 if r.returncode:raise RuntimeError('Hub CLI failed: '+args[1])
 return json.loads(r.stdout)

try:
 x=call('sessions','start','--machine',a.machine,'--workspace',a.workspace,'--engine',a.engine,'--interactive','--key',key,'--wait','--timeout',str(a.timeout),'--interval','1')
 session=x['id'];assert x['binding_wait']['state']=='running'
 print(json.dumps({'step':'start','session_id':session,'state':'running'}),flush=True)
 with tempfile.TemporaryDirectory(prefix='hub-smoke-') as directory:
  body=pathlib.Path(directory)/'message.txt'
  body.write_text('Hub acceptance nonce '+key+'. Reply to this native envelope echoing correlation_id and body_digest from data.hub_session_control exactly. Print HUB_NONCE_PROCESSED '+key+'.')
  x=call('sessions','send',session,'--body-file',str(body),'--key',key+'-send','--wait','--timeout',str(a.timeout),'--interval','1')
  assert x['command_wait']['state']=='acked' and x['command_wait']['ack']['outcome']=='delivered'
  print(json.dumps({'step':'send','state':'acked','proof':'correlated target reply'}),flush=True)
 x=call('sessions','capture',session,'--max-bytes','4096','--key',key+'-capture','--wait','--timeout',str(a.timeout),'--interval','1')
 assert x['command_wait']['state']=='acked'
 print(json.dumps({'step':'capture','state':'acked','characters':len(x['command_wait']['ack'].get('capture',''))}),flush=True)
 x=call('sessions','stop',session,'--key',key+'-stop','--wait','--timeout',str(a.timeout),'--interval','1')
 assert x['stop_wait']['state']=='stopped';stopped=True
 print(json.dumps({'step':'stop','state':'stopped'}),flush=True)
except Exception as error:
 print('Session smoke failed: '+type(error).__name__,file=sys.stderr)
 if session and not stopped:
  try:call('sessions','stop',session,'--key',key+'-cleanup','--wait','--timeout',str(a.timeout),'--interval','1')
  except Exception:print('Owned session needs reconciliation: '+session,file=sys.stderr)
 sys.exit(1)
