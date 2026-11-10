// Actual public Worker enrollment/websocket regression on the Node runtime.
import assert from 'node:assert/strict';
import { WebSocket } from 'ws';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { createFixture, restartFixture, makeTempDir } from './node-fixture.mjs';
const root = resolve(import.meta.dirname, '../..');
const token = 'synthetic-socket-cap-operator-token-0000000000000';
const machine = 'socket-cap-machine';
const sha = 'a'.repeat(40);
const matching = '/api/connect?host_version='+sha+'&protocol_version=1';
const reported = '/api/connect?host_version='+sha+'&protocol_version=2';
const machineSource = resolve(root, 'hub2/src/machine-do.ts');
const hash = () => readFile(machineSource).then(b => createHash('sha256').update(b).digest('hex'));
const before = await hash();
const workerExports = await import('./socket-cap-worker.ts');
const [CountControl, SocketMachine, ] = [workerExports.CountControl, workerExports.SocketMachine, ];
const persist = await makeTempDir('socket-cap-');
const sockets = [];
const spec = {
  root,
  entry: workerExports.default,
  classes: { CONTROL: CountControl, MACHINES: SocketMachine, PROJECTS: workerExports.CoreProject, ACCOUNTS: workerExports.CoreAccount, MAILBOXES: workerExports.CoreMailbox },
  env: { HUB2_DEV_OPERATOR_TOKEN:token,HUB2_CREDENTIAL_KEY:'17'.repeat(32),HUB2_DEFAULT_ACCOUNT_ID:'synthetic-account' },
  dataDir: persist,
};
let fixture = await createFixture(spec);
let base;
try {
 base = fixture.origin;
async function api(path,method='GET',body,auth=token){
 const r=await fetch(base+path,{method,headers:{Authorization:'Bearer '+auth,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const data=await r.json();assert(r.ok,JSON.stringify(data));return data;
}
async function enroll(){const p=await api('/api/enrollments/pair','POST',{machine_id:machine,alias:'Socket test'});return(await api('/api/enrollments/complete','POST',{code:p.pairing_code},'')).token;}
async function socket(auth,path='/api/connect'){
 const ws=new WebSocket(base.replace('http:','ws:')+path,{headers:{Authorization:'Bearer '+auth}});
 ws.on('error',()=>{});ws.on('close',(code)=>{ws.closedCode=code;});sockets.push(ws);
 await new Promise((ok,bad)=>{const t=setTimeout(()=>bad(Error('connect timeout')),5000);ws.once('open',()=>{clearTimeout(t);ok();});ws.once('error',e=>{clearTimeout(t);bad(e);});});
 return ws;
}
async function until(fn){for(let i=0;i<100;i++){if(await fn())return;await new Promise(r=>setTimeout(r,10));}throw Error('condition timeout; states='+sockets.map(s=>s.readyState+':'+s.closedCode).join(','));}

 async function expectRelease(connected, version, protocol){
  const {machines}=await api('/api/fleet/routing/machines');
  const row=machines.find(item=>item.machine_id===machine);
  assert(row,'enrolled host appears in public routing');
  assert.equal(row.connected,connected);
  if(version===undefined){
   for(const key of ['host_version','protocol_version','version_mismatch'])assert(!Object.hasOwn(row,key));
  }else{
   assert.equal(row.host_version,version);
   assert.equal(row.protocol_version,protocol);
   assert.equal(row.version_mismatch,protocol===2);
  }
 }
 const old=await enroll();
 const selfPath='/api/fleet/routing/self';
 const selfConnection=auth=>api(selfPath,'GET',undefined,auth);
 const rejected=async(path,auth,method='GET')=>(await fetch(base+path,{method,headers:{Authorization:'Bearer '+auth}})).status;
 assert.equal(await rejected(selfPath,token),403);
 assert.equal(await rejected(selfPath,old,'POST'),405);
 assert.equal(await rejected(selfPath+'?machine_id=another-machine',old),400);
 assert.deepEqual(await selfConnection(old),{machine_id:machine,connected:false});
 await api('/api/heartbeat','POST',{os:'linux',arch:'x64',labels:[],engines:['shell'],capabilities:['headless'],host_version:sha,protocol_version:2},old);
 await expectRelease(false);
 await socket(old,matching);
 await expectRelease(true,sha,1);
 assert.deepEqual(await selfConnection(old),{
  machine_id:machine,connected:true,host_version:sha,protocol_version:1,version_mismatch:false,
 });
 const beforeRestart=await socket(old,reported);
 await expectRelease(true,sha,2);
 beforeRestart.terminate();
 await until(async()=>(await api('/_socket/state')).open===0);
 fixture=await restartFixture(fixture,spec);
 base=fixture.origin;
 await expectRelease(false);
 await api('/_socket/seed','POST',{});const pending=await api('/_socket/queue');
 let prior=await socket(old);
 await expectRelease(true);
 for(let i=0;i<12;i++){const next=await socket(old);await until(()=>prior.readyState!==WebSocket.OPEN);prior.terminate();prior=next;assert.equal((await api('/_socket/state')).open,1);}
 const legacy=await Promise.all(Array.from({length:8},()=>socket(token,'/_socket/legacy')));
 assert.equal((await api('/_socket/state')).open,9);
 const count=(await api('/_socket/count')).calls;
 await api('/_socket/routing');assert.equal((await api('/_socket/count')).calls-count,1,'routing authenticates singleton once');
 assert.equal((await api('/_socket/state')).open,1,'legacy sockets pruned before auth fanout');
 for(const ws of legacy)ws.terminate();
 prior=await socket(old,reported);
 await expectRelease(true,sha,2);
 for(const query of [
  '?host_version='+sha,
  '?host_version='+sha.toUpperCase()+'&protocol_version=2',
  '?host_version='+sha+'&protocol_version=02',
  '?host_version='+sha+'&protocol_version=9007199254740992',
  '?host_version='+sha+'&protocol_version=2&protocol_version=2',
  '?host_version='+sha+'&host_version='+sha+'&protocol_version=2',
 ]){
  prior=await socket(old,'/api/connect'+query);
  await expectRelease(true);
 }
 prior=await socket(old,reported);
 await expectRelease(true,sha,2);
 const fresh=await enroll();
 assert.equal(await rejected(selfPath,old),401);
 assert.deepEqual(await selfConnection(fresh),{machine_id:machine,connected:false});
 assert.equal(prior.readyState,WebSocket.OPEN,'retired generation remains open until routing authorization');
 await expectRelease(false);
 await until(()=>prior.readyState!==WebSocket.OPEN);prior.terminate();
 const current=await socket(fresh,reported);
 await expectRelease(true,sha,2);
 assert.equal((await api('/_socket/state')).open,1);
 await assert.rejects(()=>socket(old),/401|Unauthorized/,'retired generation cannot replace new socket');
 assert.equal(current.readyState,WebSocket.OPEN);assert.equal((await api('/_socket/state')).open,1);
 const concurrent=await Promise.all(Array.from({length:8},()=>socket(fresh)));
 await until(()=>concurrent.filter(s=>s.readyState===WebSocket.OPEN).length===1);
 assert.equal((await api('/_socket/state')).open,1);
 assert.deepEqual(await api('/_socket/queue'),pending,'pending intent unchanged across reconnect/rotation');
 const replacement=await socket(fresh,reported);
 await expectRelease(true,sha,2);
 const safe=await socket(fresh,reported+'&safety=operator_stop');
 await until(()=>replacement.readyState!==WebSocket.OPEN);
 assert.equal((await api('/_socket/state')).open,1,'safety socket replaces normal singleton');
 assert.equal(safe.readyState,WebSocket.OPEN);
 assert.deepEqual(await selfConnection(fresh),{machine_id:machine,connected:false});
 await expectRelease(false);
 const last=await socket(fresh,reported);
 await expectRelease(true,sha,2);
 last.terminate();
 await until(async()=>(await api('/_socket/state')).open===0);
 await expectRelease(false);
 assert.equal(await hash(),before,'Machine source stable');
 console.log(JSON.stringify({passed:true,reconnects:12,concurrent:8,reenrollment:true,routing_authorizations:1,pending_preserved:true,safety_reconnect:true,host_release_socket_only:true,machine_sha256:before}));
}finally{for(const ws of sockets)ws.terminate();await fixture.dispose();await rm(persist,{recursive:true,force:true});}
