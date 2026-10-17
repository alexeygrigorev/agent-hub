#!/usr/bin/env python3
"""Cooperative receipt helper for an owned acceptance session, not a task-completion proof."""
import hashlib,json,subprocess,time,argparse
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--seconds',type=int,default=600);a=p.parse_args()
if not 1<=a.seconds<=1200:raise SystemExit('seconds must be 1..1200')
def native(*args):
 r=subprocess.run(['a',*args,'--json'],capture_output=True,text=True,timeout=15)
 if r.returncode:raise RuntimeError('native receiver command failed')
 return json.loads(r.stdout)
identity=native('whoami')
if not identity.get('id'):raise SystemExit('receiver requires genuine native session identity')
print('HUB_ACCEPTANCE_READY',flush=True)
end=time.monotonic()+a.seconds;seen=set()
while time.monotonic()<end:
 for item in native('message','inbox'):
  mid=item.get('id')
  if not mid or mid in seen:continue
  message=native('message','show',mid);proof=(message.get('data')or{}).get('hub_session_control')
  if not isinstance(proof,dict) or proof.get('body_digest')!=hashlib.sha256(message.get('body','').encode()).hexdigest():continue
  print('HUB_RECEIVED '+message['body'],flush=True)
  native('message','reply',mid,json.dumps({'correlation_id':proof['correlation_id'],'body_digest':proof['body_digest']}))
  native('message','ack',mid);seen.add(mid)
 time.sleep(2)
