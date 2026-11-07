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

 const old=await enroll();await api('/api/heartbeat','POST',{os:'linux',arch:'x64',labels:[],engines:['shell'],capabilities:['headless']},old);
 await api('/_socket/seed','POST',{});const pending=await api('/_socket/queue');
 let prior=await socket(old);
 for(let i=0;i<12;i++){const next=await socket(old);await until(()=>prior.readyState!==WebSocket.OPEN);prior.terminate();prior=next;assert.equal((await api('/_socket/state')).open,1);}
 const legacy=await Promise.all(Array.from({length:8},()=>socket(token,'/_socket/legacy')));
 assert.equal((await api('/_socket/state')).open,9);
 const count=(await api('/_socket/count')).calls;
 await api('/_socket/routing');assert.equal((await api('/_socket/count')).calls-count,1,'routing authenticates singleton once');
 assert.equal((await api('/_socket/state')).open,1,'legacy sockets pruned before auth fanout');
 for(const ws of legacy)ws.terminate();
 const fresh=await enroll();const current=await socket(fresh);
 await until(()=>prior.readyState!==WebSocket.OPEN);prior.terminate();assert.equal((await api('/_socket/state')).open,1);
 await assert.rejects(()=>socket(old),/401|Unauthorized/,'retired generation cannot replace new socket');
 assert.equal(current.readyState,WebSocket.OPEN);assert.equal((await api('/_socket/state')).open,1);
 const concurrent=await Promise.all(Array.from({length:8},()=>socket(fresh)));
 await until(()=>concurrent.filter(s=>s.readyState===WebSocket.OPEN).length===1);
 assert.equal((await api('/_socket/state')).open,1);
 assert.deepEqual(await api('/_socket/queue'),pending,'pending intent unchanged across reconnect/rotation');
 const safe=await socket(fresh,'/api/connect?safety=operator_stop');
 assert.equal((await api('/_socket/state')).open,1,'safety socket replaces normal singleton');
 assert.equal(safe.readyState,WebSocket.OPEN);
 assert.equal(await hash(),before,'Machine source stable');
 console.log(JSON.stringify({passed:true,reconnects:12,concurrent:8,reenrollment:true,routing_authorizations:1,pending_preserved:true,safety_reconnect:true,machine_sha256:before}));
}finally{for(const ws of sockets)ws.terminate();await fixture.dispose();await rm(persist,{recursive:true,force:true});}
