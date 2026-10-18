#!/usr/bin/env python3
"""Opt-in acceptance of CLI and MCP against ONE newly created headless session.
Credentials, commands and captures are private; no other session can be selected.
"""
import argparse, hashlib, importlib.util, json, os, subprocess, sys, time, uuid
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--cli',required=True);p.add_argument('--token-file',required=True);p.add_argument('--workspace',required=True);p.add_argument('--engine',required=True,choices=['zcodex','antigravity','opencode']);p.add_argument('--evidence',required=True);p.add_argument('--lifecycle',action='store_true',required=True);a=p.parse_args()
sp=importlib.util.spec_from_file_location('mcp_live',str(Path(a.source)/'scripts/test-mcp-live.py'));m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
secret=Path(a.token_file);assert secret.stat().st_mode&0o077==0
access=json.loads(secret.read_text())['access_token'];client=m.MCPClient('https://hub.dtcdev.click/mcp',access)
def private_write(path, text):
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600)
 os.fchmod(fd,0o600)
 with os.fdopen(fd,'w') as f:f.write(text)
out=Path(a.evidence);out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
profile=out.with_suffix('.profile.json');private_write(profile,json.dumps({'client_kind':'human','base_url':'https://hub.dtcdev.click','access_token':access}))
env={**os.environ,'HUB_CONFIG_FILE':str(profile)}
for name in ['HUB_TOKEN','HUB_JOB_TOKEN','HUB_URL','HUB_BASE_URL']:env.pop(name,None)
prefix=[sys.executable,a.cli];nonce='dual-mcp-'+str(uuid.uuid4());keys={k:nonce+'-'+k for k in ['start','send','capture','stop']};proof={'nonce':nonce,'keys':keys,'commands':[],'passed':False};sid=None
brief='This is an authorized bounded Hub CLI/MCP lifecycle test. Take the maintained Launcher first tool action immediately, writing its genuine full whoami receipt. Then print MCP_START_OK. Do not edit product files, launch agents, change services or control sessions. For up to 8 minutes, poll ONLY your own a message inbox --json every 3 seconds. Ignore unrelated messages. For the body beginning Hub MCP test follow-up, read its actual envelope with a message show ID --json. Its data.hub_session_control contains correlation_id and body_digest. Reply only to that exact ID with body MCP_SEND_OK and --data JSON echoing the exact correlation_id and body_digest from that real envelope. This test reply is the sole authorized outgoing message. Print MCP_SEND_OK after replying, then continue sleeping until your controller captures and stops you; finish naturally after 8 minutes.'
def save():private_write(out,json.dumps(proof,indent=2))
def cli(*args):
 command=prefix+['sessions',*args];r=subprocess.run(command,env=env,capture_output=True,text=True,timeout=90);row={'argv':command,'returncode':r.returncode};proof['commands'].append(row)
 try:row['result']=json.loads(r.stdout)
 except:row['stderr']=r.stderr;save();raise RuntimeError('CLI failed; see private evidence')
 save()
 if r.returncode:raise RuntimeError('CLI failure')
 return row['result']
def show():return client.call('sessions_show',{'id':sid})['data']
def waited_command(kind,extra,cli_args):
 queued=cli(kind,sid,'--key',keys[kind],*cli_args);proof[kind+'_cli']=queued;save()
 reply=client.call('sessions_'+kind,{'id':sid,'payload':{'key':keys[kind],**extra}})['data'];assert reply['command_id']==queued['command_id'];proof[kind+'_mcp']=reply;save()
 def check():
  native=client.call('sessions_commands',{'id':sid,'query':{'limit':100}})['data'];local=cli('commands',sid)
  remote=next((x for x in native['commands'] if x['id']==queued['command_id']),{});other=next((x for x in local['commands'] if x['id']==queued['command_id']),{})
  return remote if other.get('state')==remote.get('state') else {}
 done=m.wait_for(check,lambda r:r.get('state') in ['acked','refused','uncertain'],600);proof[kind+'_ack']=done;save();assert done['state']=='acked';return done
try:
 client.rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'hub-dual-acceptance','version':'1'}})
 q=subprocess.run(['quse','--json'],capture_output=True,text=True,timeout=120);assert q.returncode==0;proof['fresh_quse']=True
 payload={'key':keys['start'],'workspace':a.workspace,'engine':a.engine,'mode':'headless','name':nonce,'brief':brief}
 started=client.call('sessions_start',{'payload':payload},allow_error=True);proof['start_mcp']=started;sid=started.get('data',{}).get('id');proof['session_id']=sid;save();assert started['status']<400 and sid,'Admission refused; see evidence'
 duplicate=cli('start','--workspace',a.workspace,'--engine',a.engine,'--headless','--name',nonce,'--key',keys['start'],'--prompt',brief);proof['start_cli']=duplicate;assert duplicate['id']==sid
 running=m.wait_for(show,lambda r:r.get('state') in ['running','failed','held','uncertain','stopped'],600);proof['bound']=running;save();assert running['state']=='running'
 body='Hub MCP test follow-up. Reply MCP_SEND_OK using the exact received correlation proof; do not edit files or take other actions.'
 body_file=out.with_suffix('.body.txt');private_write(body_file,body)
 sent=waited_command('send',{'body':body},['--body-file',str(body_file)]);assert sent.get('ack',{}).get('proof',{}).get('reply_digest')==hashlib.sha256(b'MCP_SEND_OK').hexdigest()
 captured=waited_command('capture',{'max_bytes':16384},['--max-bytes','16384']);assert captured.get('ack',{}).get('capture');assert captured['ack']['native']['native_id']==running['binding']['native_id']
 waited_command('stop',{},[])
 final=m.wait_for(show,lambda r:r.get('state')=='stopped',600);proof['final_mcp']=final;proof['final_cli']=cli('show',sid);assert proof['final_cli']['state']=='stopped';proof['passed']=True;print('PASS: both CLI and MCP start/send/capture/confirmed stop of one owned session')
finally:
 if sid and not proof['passed']:
  try:proof['cleanup_stop']=client.call('sessions_stop',{'id':sid,'payload':{'key':keys['stop']}},allow_error=True)
  except Exception:proof['cleanup_stop']='reconciliation_required'
 proof['finished_at']=time.time();save()
