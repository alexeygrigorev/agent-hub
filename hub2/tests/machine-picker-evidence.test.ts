import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {MachineDO} from '../src/machine-do';
import worker from '../src/worker';
import {advanceDispatch} from '../src/dispatch';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const admin={id:'human:evidence',kind:'operator',scopes:['operator:admin']};
const host={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','machine:offer','session:report']};
function fixture(){
 const database=new DatabaseSync(':memory:');let alarm:number|null=null;
 const ctx:any={storage:{sql:{exec(q:string,...args:any[]){const stmt=database.prepare(q);const rows=stmt.columns().length?stmt.all(...args):[];const writes=stmt.columns().length?0:Number(stmt.run(...args).changes);return{toArray:()=>rows,rowsRead:rows.length,rowsWritten:writes};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const r=fn();database.exec('COMMIT');return r;}catch(e){database.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(v:number){alarm=v;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>[]};
 const namespace={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true})})};
 const env:any={CONTROL:namespace,PROJECTS:namespace,ACCOUNTS:namespace,MAILBOXES:namespace};
 let machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,actor:any=admin)=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,call,reopen(){machine=new MachineDO(ctx,env);},env:{...env,MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:(input:Request|string,init?:RequestInit)=>machine.fetch(input instanceof Request?input:new Request(input,init))})}}};
}
function evidence(size=3){
 const pool=Array.from({length:size},(_,i)=>({policy_id:i===0?'sol':`candidate-${i}-${'x'.repeat(100)}`,engine:'codex',model:i===0?'gpt-6.1-sol':`synthetic-${i}-${'y'.repeat(100)}`,effort:'medium',weight:1,share:1/size}));
 const selected={policy_id:'sol',policy_version:7,machine_id:'h1',attempt:0,engine:'codex',model:'gpt-6.1-sol',effort:'medium'};
 return{...selected,pool,history:[selected]};
}
function reservation(picker:any=evidence(),prompt='task'){
 return{operation_id:'op-evidence',dispatch_id:'dispatch-evidence',attempt:0,session_id:'session-evidence',project_id:'project-test',intake_id:'intake-test',machine_id:'h1',request_digest:'user-request-digest',title:'evidence task',acceptance_criteria:['produce output'],account_id:'codex',picker,launch:{workspace:'/synthetic',engine:'codex',model:'gpt-6.1-sol',effort:'medium',interactive:false,prompt}};
}
async function beat(f:ReturnType<typeof fixture>){assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]},host)).status,200);}

test('dispatch preserves a full actual pool beside a maximum launch and hides diagnostics from host offers',async()=>{
 const f=fixture();try{
  await beat(f);const input=reservation({...evidence(32),credential:'never-persist-this',excluded:[{account_id:'private-account'}]},'p'.repeat(11_500));
  assert(Buffer.byteLength(JSON.stringify(input))>12_288);
  assert(Buffer.byteLength(JSON.stringify(input.launch))<12_000);
  const dispatch=await advanceDispatch(f.env,{...input,state:'pending_machine',attempt_operation_id:input.operation_id,epoch:null} as any);
  assert.equal(dispatch.ok,true);assert.equal((dispatch as any).state,'staged');
  const stored=f.database.prepare('SELECT launch_json FROM sessions').get() as any;
  assert(!stored.launch_json.includes('never-persist-this'));assert(!stored.launch_json.includes('private-account'));
  const detail=await f.call('/api/sessions/session-evidence');assert.equal(detail.status,200);
  const session=(await detail.json() as any).session;
  assert.deepEqual(session.picker,evidence(32));assert.equal(session.policy_id,'sol');assert.equal(session.policy_version,7);
  const epoch=(dispatch as any).epoch;
  assert.equal((await f.call('/_internal/activate','POST',{operation_id:input.operation_id,session_id:input.session_id,epoch})).status,201);
  const offers=(await (await f.call('/api/offers','GET',undefined,host)).json() as any).offers;
  assert.equal(offers.length,1);assert.deepEqual(offers[0].launch,input.launch);
  assert(!JSON.stringify(offers).includes('_hub_picker'));assert(!JSON.stringify(offers).includes('candidate-'));
  // A rollback to an older Machine reader can emit the extra JSON property.
  // The real host hashes its validated whitelist, so replay remains identical.
  const hostProof=spawnSync('python3',['-c',`
import hashlib,json,sys
from pathlib import Path
from types import SimpleNamespace
from agent_hub_host.agent import Bridge
from agent_hub_host.native_launch import canonical
offer=json.load(sys.stdin)
bridge=Bridge.__new__(Bridge)
bridge.engines={'codex'}
bridge.resolve_workspace=lambda value:Path(value)
bridge.launcher=SimpleNamespace(capabilities=lambda:{'codex':{'headless','effort'}})
before=hashlib.sha256(canonical(bridge.validate_launch(offer)).encode()).hexdigest()
offer['launch']['_hub_picker']={'pool':[{'policy_id':'synthetic'}]}
after=hashlib.sha256(canonical(bridge.validate_launch(offer)).encode()).hexdigest()
assert before==after
`],{input:JSON.stringify(offers[0]),encoding:'utf8',timeout:10_000,
    env:{...process.env,PYTHONPATH:fileURLToPath(new URL('../host/',import.meta.url))}});
  assert.equal(hostProof.status,0,hostProof.stderr);
  f.reopen();const restored=(await (await f.call('/api/sessions/session-evidence')).json() as any).session;
  assert.deepEqual(restored.picker,evidence(32));
 }finally{f.database.close();}
});

test('reservation replay never overwrites evidence and old reservations still work',async()=>{
 const f=fixture();try{
  await beat(f);const input=reservation();assert.equal((await f.call('/_internal/reserve','POST',input)).status,201);
  const different=evidence();different.policy_version=8;different.history[0].policy_version=8;
  const replay=await f.call('/_internal/reserve','POST',{...input,picker:different});assert.equal(replay.status,200);
  assert.deepEqual((await (await f.call('/api/sessions/session-evidence')).json() as any).session.picker,evidence());
  const legacy:any=reservation();delete legacy.picker;legacy.session_id='legacy';legacy.operation_id='legacy-op';legacy.dispatch_id='legacy-dispatch';
  assert.equal((await f.call('/_internal/reserve','POST',legacy)).status,201);
  assert.equal((await (await f.call('/api/sessions/legacy')).json() as any).session.picker,undefined);
 }finally{f.database.close();}
});

test('separate evidence allowance cannot bypass launch size, identity binding or reserved metadata protection',async()=>{
 const f=fixture();try{
  await beat(f);
  for(const modify of [
   (input:any)=>{input.picker.machine_id='foreign';},
   (input:any)=>{input.picker.attempt=1;},
   (input:any)=>{input.picker.model='different';},
   (input:any)=>{input.launch._hub_picker=evidence();},
  ]){const input=reservation();modify(input);assert.equal((await f.call('/_internal/reserve','POST',input)).status,400);}
  assert.equal((await f.call('/_internal/reserve','POST',reservation(evidence(),'x'.repeat(12_000)))).status,413);
  const huge:any=reservation();huge.picker.extra='x'.repeat(65_536);assert.equal((await f.call('/_internal/reserve','POST',huge)).status,413);
  assert.equal(f.database.prepare('SELECT * FROM sessions').all().length,0);
 }finally{f.database.close();}
});


test('public live session pages filter terminal rows before pagination, for one machine and the fleet',async()=>{
 const f=fixture();try{
  await beat(f);
  const states=[['a','failed'],['b','running'],['c','expired'],['d','offered'],['e','stopped']];
  for(const [id,state] of states){
   const input={...reservation(),session_id:id,operation_id:'op-'+id,dispatch_id:'dispatch-'+id};
   assert.equal((await f.call('/_internal/reserve','POST',input)).status,201);
   f.database.prepare('UPDATE sessions SET state=? WHERE id=?').run(state,id);
  }
  const env={...f.env,CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   const path=new URL(request.url).pathname;
   if(path==='/_verify')return Response.json({actor:admin});
   if(path==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});
   throw new Error('unexpected control route '+path);
  }})}};
  const page=async(query:URLSearchParams)=>{
   const response=await worker.fetch(new Request('https://hub.test/api/sessions?'+query,{
    headers:{Authorization:'Bearer '+'test-token'.repeat(4)},
   }),env);
   assert.equal(response.status,200);
   return await response.json() as {sessions:{id:string;host:string;workspace:string}[];next_cursor:string|null;complete:boolean};
  };
  for(const machine of ['', 'h1']){
   const query=new URLSearchParams({live:'1',limit:'1'});
   if(machine)query.set('machine_id',machine);
   const first=await page(query);
   assert.deepEqual(first.sessions.map(row=>[row.id,row.host,row.workspace]),[['b','h1','/synthetic']]);
   assert.equal(first.complete,false);
   assert(first.next_cursor);
   query.set('after',first.next_cursor);
   const last=await page(query);
   assert.deepEqual(last.sessions.map(row=>row.id),['d']);
   assert.equal(last.complete,true);
   assert.equal(last.next_cursor,null);
   query.delete('after');query.delete('live');query.set('limit','10');
   assert.deepEqual((await page(query)).sessions.map(row=>row.id),states.map(([id])=>id));
  }
 }finally{f.database.close();}
});
