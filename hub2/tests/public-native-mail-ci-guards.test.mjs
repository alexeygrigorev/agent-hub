import {test} from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {bareEnv,pythonPath,checkProbe,checkResult,runChild,failureMetadata,PINS,SOURCES} from './public-native-mail-ci-helper.mjs';

function fake() {
  const child=new EventEmitter();child.pid=1234;child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin=new EventEmitter();
  const times=[],signals=[],calls=[];child.stdin.end=value=>{calls.push(['stdin',value]);};child.kill=sig=>{signals.push(sig);return true;};
  const deps={spawn:(...args)=>{calls.push(['spawn',...args]);return child;},now:()=>0,
    timer:(fn,ms)=>{const t={fn,ms,cleared:false};times.push(t);return t;},clear:t=>{if(t)t.cleared=true;}};
  const receipt=(out='',err='',changes={})=>({schema:1,supervision:{owned_subtree_reaped:true,unknown:false,residual_children:false,capture_complete:true,
    leader_status:0,signals:['INT','KILL'],leader_exit_code:0,success:true,...changes},stdout_b64:Buffer.from(out).toString('base64'),stderr_b64:Buffer.from(err).toString('base64')});
  const publish=(out='',err='',changes={},code=0)=>{child.stdout.emit('data',Buffer.from(JSON.stringify(receipt(out,err,changes))));child.emit('exit',code,null);child.emit('close',code,null);};
  return {child,times,signals,calls,deps,receipt,publish,run:opts=>runChild('/absolute/test',['fixed'],{cwd:'/owned',env:{},input:'private',deadline:1000,python:'/absolute/python',supervisor:'/owned/supervisor.py',...opts},deps)};
}
test('CI guard: child-only environment excludes selector/proxy/profile values',()=>{
  assert.deepEqual(bareEnv('/owned',{HTTP_PROXY:'secret',HUB_CONFIG_FILE:'secret',APLEXER_SESSION_ID:'secret',PATH:'bad',AGENT_HUB_HEAVY_TEST_HELD:'1'}),
    {PATH:'/usr/bin:/bin',HOME:'/owned',TMPDIR:'/owned',LC_ALL:'C',PYTHONDONTWRITEBYTECODE:'1',PYTHONNOUSERSITE:'1',AGENT_HUB_HEAVY_TEST_HELD:'1'});
});
test('CI guard: Python resolution is absolute, explicit or existing python3.12 only',()=>{
  assert.equal(pythonPath({PATH:'relative:/good'},p=>p==='/good/python3.12'),'/good/python3.12');
  assert.equal(pythonPath({HUB_PUBLIC_MAIL_TEST_PYTHON:'/owned/python'},()=>true),'/owned/python');
  assert.throws(()=>pythonPath({HUB_PUBLIC_MAIL_TEST_PYTHON:'relative'},()=>true));
  assert.throws(()=>pythonPath({PATH:'/good'},()=>false));
});
test('CI guard: exact Python3.12 and websockets16 required, missing never skips',()=>{
  checkProbe('{"python":[3,12],"websockets":"16.0"}');
  for(const raw of ['{}','{"python":[3,14],"websockets":"16.0"}','{"python":[3,12],"websockets":"15.0"}','not json'])assert.throws(()=>checkProbe(raw));
});
test('CI guard: only public progression with distinct IDs and owned evidence qualifies',()=>{
  const r={status:'observed_public_progression',first:'34dd6cad-9f2e-4289-a7da-bc8ed97841c6',later:'2dbc7f38-f604-4638-931d-e4b9cd9e54a5',native_effect_fake:true,full63_retirement:false,complete:false,evidence_directory:'/owned/public-native-mail-AB12'};
  checkResult(JSON.stringify(r),'/owned');
  for(const mutation of [{status:'FAILED_OR_UNKNOWN'},{later:r.first},{complete:true},{native_effect_fake:false},{evidence_directory:'/other/public-native-mail-AB12'}])assert.throws(()=>checkResult(JSON.stringify({...r,...mutation}),'/owned'));
  assert.throws(()=>checkResult(JSON.stringify(r)+'\n'+JSON.stringify(r),'/owned'));
});
test('CI guard: fixed component pins and all current 13 sources are explicit',()=>{
  assert.equal(SOURCES.length,13);assert.equal(new Set(SOURCES).size,13);
  for(const hash of Object.values(PINS))assert.match(hash,/^[a-f0-9]{64}$/);
  assert.ok(SOURCES.includes('hub2/host/agent_hub_host/agent.py'));
});
test('CI guard: strict subreaper receipt closes owned subtree and decoded captures',async()=>{
  const f=fake(),p=f.run();f.publish('actual');assert.equal((await p).stdout,'actual');assert.deepEqual(f.signals,[]);
  assert.equal(f.calls[0][1],'/absolute/python');assert.equal(f.calls[0][3].detached,false);
  assert.ok(f.calls[0][2].includes('/owned/supervisor.py'));assert.ok(f.times.every(t=>t.cleared));
});
test('CI guard: nonzero leader status fails with genuine closed subtree retained',async()=>{
  const f=fake(),retained=[],p=f.run({retain:v=>retained.push(v)});
  f.publish('failure evidence','',{leader_exit_code:1,leader_status:256,success:false},1);
  await assert.rejects(p,/child_failed/);assert.equal(retained[0].supervision.owned_subtree_reaped,true);
  assert.equal(retained[0].stdout.toString(),'failure evidence');
});
test('CI guard: diagnostics publish only fixed phase/failure codes and strict supervision facts',()=>{
  const f=fake(),proof=f.receipt().supervision;
  const input={closed:true,fault:'private fault',exit_code:1,signal:'private signal',supervision:proof,
    stdout:Buffer.from(JSON.stringify({status:'FAILED_OR_UNKNOWN',phase:'later_cli',reason:'actual_cli_failed',
      first:'private ID',evidence_directory:'/private/path',token:'private token'})),
    stderr:Buffer.from('private traceback\nModuleNotFoundError: No module named \'websockets\'\n')};
  const scenario=failureMetadata('scenario',input),probe=failureMetadata('probe',input);
  assert.deepEqual(scenario.scenario_failure,{status:'FAILED_OR_UNKNOWN',phase:'later_cli',reason:'actual_cli_failed'});
  assert.equal(probe.probe_failure,'python_transport_missing');assert.deepEqual(probe.supervision,proof);
  assert(!JSON.stringify([probe,scenario]).includes('private'));
  for(const changes of [{phase:'private phase'},{reason:'private reason'},{status:'private status'}]){
    const value={...input,stdout:Buffer.from(JSON.stringify({status:'FAILED_OR_UNKNOWN',phase:'later_cli',reason:'actual_cli_failed',...changes}))};
    assert.equal(failureMetadata('scenario',value).scenario_failure,undefined);
  }
  assert.equal(failureMetadata('probe',{...input,supervision:{...proof,secret:'private'}}).supervision,null);
  assert.throws(()=>failureMetadata('private phase',input),/diagnostic_phase/);
});
test('CI guard: an abnormal supervisor exit never becomes an ordinary child failure',async()=>{
  const f=fake(),p=f.run();
  f.child.stdout.emit('data',Buffer.from(JSON.stringify(f.receipt('','',{success:false}))));
  f.child.emit('exit',2,null);f.child.emit('close',2,null);
  await assert.rejects(p,/supervisor_failed_unknown/);
});
test('CI guard: leader/stdio close alone never credits subtree closure',async()=>{
  const f=fake(),p=f.run();f.child.emit('close',0,null);await assert.rejects(p,/owned_subtree_unknown/);
  assert.deepEqual(f.signals,[]);
});
test('CI guard: zero leader exit with residual children or uncertain probe fails',async()=>{
  for(const mutation of [{residual_children:true},{unknown:true},{owned_subtree_reaped:false},{capture_complete:false}]){
    const f=fake(),p=f.run();f.publish('','',mutation);await assert.rejects(p,/residual_child_cleanup_failed|owned_subtree_unknown|capture_incomplete_unknown/);
  }
});
test('CI guard: outer capture cap requests only owned supervisor handle stop',async()=>{
  const f=fake(),p=f.run();f.child.stdout.emit('data',Buffer.alloc(114689));
  assert.deepEqual(f.signals,['SIGTERM']);f.child.emit('close',1,null);await assert.rejects(p,/owned_subtree_unknown/);
});
test('CI guard: actual stderr16KiB remains bounded inside supervisor envelope',async()=>{
  const f=fake(),p=f.run();f.publish('',Buffer.alloc(16385));await assert.rejects(p,/owned_subtree_unknown/);
});
test('CI guard: absolute deadline escalates handle once then retains unknown',async()=>{
  const f=fake(),retained=[],p=f.run({retain:v=>retained.push(v)});f.times[0].fn();assert.deepEqual(f.signals,['SIGTERM']);
  f.times[1].fn();assert.deepEqual(f.signals,['SIGTERM','SIGKILL']);
  f.times[2].fn();await assert.rejects(p,/owned_subtree_unknown/);
  assert.equal(retained.length,1);assert.equal(retained[0].closed,false);assert.equal(retained[0].supervision,null);
  f.child.emit('close',null,'SIGKILL');assert.equal(retained.length,1);assert.equal(f.signals.length,2);
});
test('CI guard: expired budget spawns nothing',()=>{
  const f=fake();assert.throws(()=>f.run({deadline:0}),/child_deadline_or_supervisor/);assert.equal(f.calls.length,0);
});
test('CI guard: exited supervisor is never signalled by recycled numeric PID',async()=>{
  const f=fake(),p=f.run();f.child.emit('exit',1,null);f.times[0].fn();f.times[1].fn();
  assert.deepEqual(f.signals,[]);f.times[2].fn();await assert.rejects(p,/owned_subtree_unknown/);
});
test('CI guard: stdin/spawn errors never invent closure or retry scenario',async()=>{
  for(const kind of ['stdin','child']){
    const f=fake(),p=f.run();(kind==='stdin'?f.child.stdin:f.child).emit('error',new Error('private'));
    f.child.emit('close',1,null);await assert.rejects(p,/owned_subtree_unknown/);assert.equal(f.calls.filter(c=>c[0]==='spawn').length,1);
  }
});
