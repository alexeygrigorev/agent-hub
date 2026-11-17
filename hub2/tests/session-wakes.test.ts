import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {MachineDO} from '../src/machine-do';
import worker from '../src/worker';
import {ProjectDO} from '../src/project-do';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {once} from 'node:events';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {sha256Hex} from '../src/crypto';
import {HttpError, HttpError as ExpectedHttpError} from '../src/http';
import type {JsonRecord} from '../src/types';
import type {WakeRecord,WakeSession} from '../src/session-wakes';
const admin={id:'operator:wake-test',kind:'operator',scopes:['operator:admin']};
const host={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','machine:offer','session:report']};
const native='11111111-2222-4333-8444-555555555555';
function fixture(workspace='/owned/work'){
 const db=new DatabaseSync(':memory:'),kv=new Map<string,unknown>(),events:any[]=[],sockets:any[]=[];let projectFailure=false;let alarm:number|null=null;
 const ctx:any={storage:{sql:{exec(q:string,...args:any[]){const p=db.prepare(q);let rows:any[]=[],written=0;if(p.columns().length)rows=p.all(...args);else written=Number(p.run(...args).changes);return{toArray:()=>rows,rowsRead:rows.length,rowsWritten:written};}},transactionSync(fn:()=>unknown){db.exec('BEGIN');try{const out=fn();db.exec('COMMIT');return out;}catch(e){db.exec('ROLLBACK');throw e;}},async get(k:string){return structuredClone(kv.get(k));},async put(k:string,v:unknown){kv.set(k,structuredClone(v));},async delete(k:string){return kv.delete(k);},async list(o:any={}){return new Map([...kv].filter(([k])=>k.startsWith(o.prefix??'')&&(!o.startAfter||k>o.startAfter)).sort(([a],[b])=>a.localeCompare(b)).slice(0,o.limit??1000));},async getAlarm(){return alarm;},async setAlarm(v:number){alarm=v;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn()),getWebSockets:()=>sockets};
 const env:any={HUB2_SCHEMA_MIGRATIONS:'apply',CONTROL:{idFromName:(x:string)=>x,get:()=>({fetch:async(input:any)=>{const p=new URL(input.url??input).pathname;if(p==='/_verify')return Response.json({actor:admin});if(p.startsWith('/api/routes/session/'))return Response.json({route:{id:'s1',owner:'h1'}});return Response.json({authorized:true});}})},PROJECTS:{idFromName:(x:string)=>x,get:()=>({fetch:async(input:any,init?:any)=>{const req=input instanceof Request?input:new Request(input,init);events.push({path:new URL(req.url).pathname,body:await req.json()});return projectFailure?Response.json({error:'synthetic_unavailable'},{status:503}):Response.json({ok:true});}})}};
 let machine=new MachineDO(ctx,env);env.MACHINES={idFromName:(x:string)=>x,get:()=>({fetch:(r:Request)=>machine.fetch(r)})};
 const call=(p:string,method='GET',body?:any,actor:any=admin)=>machine.fetch(new Request('https://machine.test'+p,{method,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 async function boot(interactive=true,wakeSupport=true){await call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive',...(wakeSupport?['session-wakes:v1']:[])],native_sessions:[]},host);const response=await call('/_internal/reserve','POST',{operation_id:'op1:0',dispatch_id:'op1',attempt:0,session_id:'s1',project_id:'p1',intake_id:'i1',machine_id:'h1',request_digest:'d1',title:'wake',acceptance_criteria:['wake'],account_id:'claude',launch:{workspace,engine:'claude',model:'sonnet',prompt:'test',interactive}});assert.equal(response.status,201);const r=await response.json() as any;await call('/_internal/activate','POST',{operation_id:'op1:0',session_id:'s1',epoch:r.epoch});db.prepare("UPDATE sessions SET state='running',native_id=? WHERE id='s1'").run(native);return r.epoch;}
 const publicCall=(p:string,body?:any)=>worker.fetch(new Request('https://hub.test'+p,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+'x'.repeat(40),'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})}),env);
 return{workspace,db,kv,events,sockets,failProject(value=true){projectFailure=value;},edit(q:string){db.exec(q);(machine as any).db.cache.clear();},storage:ctx.storage,boot,call,publicCall,get machine(){return machine;},restart(){machine=new MachineDO(ctx,env);},get alarm(){return alarm;}};
}
test('public session wake records a durable one-shot for the existing interactive session',async()=>{
 const f=fixture();await f.boot();const response=await f.publicCall('/api/sessions/s1/wake',{key:'once',at:new Date(Date.now()+60_000).toISOString(),prompt:'Check the external result'});assert.equal(response.status,201,await response.text());
});

async function publish(f:ReturnType<typeof fixture>,now=Date.now(),birth=1){
 const rows=[{id:native,tag:'task-hub2-s1',workspace:f.workspace,engine:'claude',agent:null,profile:null,agent_profile:null,phase:'running',state:'running',worker_alive:true,cwd:'/owned/work',created_at_ms:birth,last_activity_ms:null}];
 const data=JSON.stringify(rows),meta={snapshot_id:crypto.randomUUID(),started_at_ms:now,observed_at_ms:now,row_count:1,chunk_count:1,sha256:await sha256Hex(data)};
 assert.equal((await f.call('/api/native-inventory','POST',{...meta,chunk_index:0,data},host)).status,200);
}
const create=(f:ReturnType<typeof fixture>,key='once',at:any='now',prompt='Wake and check')=>f.publicCall('/api/sessions/s1/wake',{key,at,prompt});
const command=(f:ReturnType<typeof fixture>)=>f.db.prepare("SELECT * FROM session_commands WHERE kind='wake_send'").get() as any;
function proof(c:any,status='submitted'){const b=JSON.parse(c.result_json).wake_binding;return{delivery:'native_acknowledged',delivered_at_ms:Date.now(),wake_proof:{kind:'native_delivery',status,native_message_id:'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee',sender_native_id:native,sender_tag:b.tag,sender_workspace:b.workspace,sender_generation:c.epoch,sender_created_at_ms:b.created_at_ms}};}
const ack=(f:ReturnType<typeof fixture>,c:any,r:any,actor:any=host)=>f.call('/api/sessions/s1/commands/'+c.id+'/ack','POST',{epoch:c.epoch,result:r},actor);
async function authorize(f:ReturnType<typeof fixture>,c:any){assert.equal((await ack(f,c,{wake_pending:true,native_message_id:'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'})).status,200);Object.assign(c,command(f));}
async function history(f:ReturnType<typeof fixture>){return await(await f.publicCall('/api/sessions/s1/wakes')).json() as any;}
test('future wake is durable, private and replay of now never renews the original due time',async()=>{
 const f=fixture();await f.boot();const at=new Date(Date.now()+60_000).toISOString();const first=await(await create(f,'future',at)).json() as any;
 assert.equal(command(f),undefined);assert.equal((await(await f.call('/api/sessions/s1')).json() as any).session.waiting_until,at);
 f.restart();assert.equal((await create(f,'future',at)).status,200);assert.equal((await create(f,'future',at,'changed')).status,409);
 const now=await(await create(f,'now','now')).json() as any;const replay=await(await create(f,'now','now')).json() as any;assert.equal(now.wake_at,replay.wake_at);assert.equal(replay.replay,true);
 const h=await history(f);assert.equal(h.wakes.length,2);assert.ok(h.wakes.every((w:any)=>!('prompt'in w)));assert.equal(first.wake_state,'pending');
});
test('due wake uses one exact command, qualified guard proof and immutable replay after restart',async()=>{
 const f=legacyFixture();await f.boot();await publish(f);assert.equal((await create(f)).status,201);const c=command(f);assert.ok(c);const b=JSON.parse(c.result_json).wake_binding;
 assert.equal(c.kind,'wake_send');assert.equal(b.operation_id,'op1:0');assert.equal(b.native_id,native);assert.equal(b.created_at_ms,1);assert.equal((await ack(f,c,{delivery:'native_acknowledged'})).status,400);
 assert.equal((await ack(f,c,{wake_pending:true})).status,400);await authorize(f,c);assert.equal(command(f).state,'wake_pending');
 const r=proof(c);assert.equal((await ack(f,c,r)).status,200);assert.equal((await ack(f,c,r)).status,200);
 f.restart();const replay=await(await create(f)).json() as any;assert.equal(replay.replay,true);assert.equal(replay.wake_state,'acked');assert.equal(f.db.prepare('SELECT COUNT(*) AS n FROM session_commands').get()!.n,1);
 assert.equal((await history(f)).wakes[0].wake_state,'acked');
});
test('wake actors, malformed payloads and service-command keys remain closed',async()=>{
 const f=fixture();await f.boot();
 for(const a of [{id:'foreign',kind:'agent',resource_id:'elsewhere',scopes:['session:send']},host,{id:'reader',kind:'agent',resource_id:'s1',scopes:['session:read']}])assert.equal((await f.call('/api/sessions/s1/wake','POST',{key:'x',at:'now',prompt:'x'},a)).status,404);
 const self={id:'session:s1',kind:'agent',resource_id:'s1',scopes:['session:send','session:read']};assert.equal((await f.call('/api/sessions/s1/wake','POST',{key:'x',at:'now',prompt:'x'},self)).status,201);
 for(const body of [{key:'b',at:'tomorrow',prompt:'x'},{key:'c',at:'now',prompt:''},{key:'d',at:'now',prompt:'x',wake:{}}])assert.equal((await f.publicCall('/api/sessions/s1/wake',body)).status,400);
 assert.equal((await f.call('/api/sessions/s1/commands','POST',{kind:'send',key:'hub-wake:forged',text:'x'})).status,400);
});
test('unknown or foreign native proof cannot satisfy a wake and host authority is required',async()=>{
 const f=legacyFixture();await f.boot();await publish(f);await create(f);const c=command(f);await authorize(f,c);const r=proof(c);
 for(const mutation of [{sender_native_id:'99999999-2222-4333-8444-555555555555'},{sender_generation:c.epoch+1},{sender_created_at_ms:2},{sender_workspace:'/foreign'},{status:'queued'},{extra:'untrusted'}])assert.equal((await ack(f,c,{...r,wake_proof:{...r.wake_proof,...mutation}})).status,409);
 assert.equal((await ack(f,c,r,admin)).status,403);assert.equal((await ack(f,{...c,epoch:c.epoch+1},r)).status,409);
 assert.equal((await ack(f,c,{...r,exception:'secret'})).status,400);assert.equal(command(f).state,'wake_pending');
});
test('headless due wake is a truthful missed handoff and never weakens send refusal',async()=>{
 const f=fixture();await f.boot(false);await publish(f);assert.equal((await create(f)).status,201);assert.equal(command(f),undefined);
 const h=await history(f);assert.equal(h.wakes[0].wake_state,'missed');assert.equal(h.wakes[0].reason,'headless_send_refused');
 assert.equal((await f.call('/api/sessions/s1/commands','POST',{kind:'send',key:'normal',text:'x'})).status,409);
 assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM machine_outbox WHERE kind='wake' AND json_extract(payload_json,'$.missed_at') IS NOT NULL").get()!.n,1);
 assert.equal((await(await f.call('/api/sessions/s1')).json() as any).session.state,'running');
});
test('due-time expiry and late qualified ACK preserve missed evidence without a new effect',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=legacyFixture();await f.boot();await publish(f,now);await create(f);const c=command(f);await authorize(f,c);now+=600001;await (f.machine as any).wakes.sweep();
 assert.equal((await history(f)).wakes[0].wake_state,'missed');assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'running');
 assert.equal((await ack(f,c,proof(c))).status,200);const h=await history(f);assert.equal(h.wakes[0].wake_state,'acked');assert.ok(h.wakes[0].missed_at);assert.equal(h.wakes[0].reason,'wake_not_acked_within_10m');assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM attention WHERE reason='missed_wake'").get()!.n,0);assert.equal(f.db.prepare('SELECT COUNT(*) AS n FROM session_commands').get()!.n,1);
 }finally{Date.now=original;}
});
test('terminal or replacement attempts do not inherit pending wake effects',async()=>{
 const f=fixture();await f.boot();await publish(f);await create(f,'future',new Date(Date.now()+1000).toISOString());f.edit("UPDATE sessions SET state='lost' WHERE id='s1'");await f.machine.alarm();assert.equal(command(f),undefined);assert.equal((await history(f)).wakes[0].wake_state,'missed');
 f.edit("UPDATE sessions SET state='running',operation_id='replacement',epoch=epoch+1 WHERE id='s1'");assert.equal((await create(f,'future','now')).status,200);assert.equal(command(f),undefined);
});
test('partial creation durability failure is not acknowledged and exact-key retry repairs without duplication',async()=>{
 const f=fixture();await f.boot();const put=f.storage.put;let once=true;f.storage.put=async(k:string,v:any)=>{await put(k,v);if(once&&k.startsWith('session-wake-active/')){once=false;throw new Error('synthetic_after_commit');}};
 assert.equal((await create(f,'partial',new Date(Date.now()+60000).toISOString())).status,500);f.storage.put=put;
 const stored=[...f.kv.values()].find((v:any)=>v.request_key==='partial') as any;assert.ok(stored);
 f.restart();const response=await create(f,'partial',new Date(stored.wake_at_ms).toISOString());assert.equal(response.status,200);assert.equal((await response.json() as any).wake_id,stored.id);assert.equal(command(f),undefined);
});
test('KV await race cannot dispatch after the original incarnation changes',async()=>{
 const f=fixture();await f.boot();await publish(f);const put=f.storage.put;let once=true;f.storage.put=async(k:string,v:any)=>{await put(k,v);if(once&&k.startsWith('session-wake-active/')){once=false;f.edit("UPDATE sessions SET epoch=epoch+1 WHERE id='s1'");}};
 await create(f);assert.equal(command(f),undefined);assert.equal((await history(f)).wakes[0].wake_state,'missed');
});

test('actual host Bridge guard receipt crosses the unchanged command ACK boundary',async()=>{
 const f=legacyFixture();await f.boot();await publish(f);const frames:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))});
 await create(f);const c=command(f),packet=frames.find(x=>x.type==='session.command').command;assert.equal(packet.kind,'send');assert.ok(packet.wake);
 const hostRoot=fileURLToPath(new URL('../host',import.meta.url)),script=fileURLToPath(new URL('../host/tests/test_session_wakes.py',import.meta.url));
 const parent=process.env.TMPDIR||fileURLToPath(new URL('../../.local/tmp',import.meta.url));
 mkdirSync(parent,{recursive:true,mode:0o700});const owned=mkdtempSync(join(parent,'wake-bridge-'));
 try{
 const child=spawn('/usr/bin/python3',['-I','-S','-c','import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")',hostRoot,script,'--wire'],{stdio:['pipe','pipe','pipe'],env:{...process.env,HOME:owned,TMPDIR:owned}});
 const lines=createInterface({input:child.stdout}),iterator=lines[Symbol.asyncIterator](),closed=once(child,'close');let error='';child.stderr.on('data',b=>error=(error+String(b)).slice(-8192));
 const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
 try{
  child.stdin.write(JSON.stringify(packet)+'\n');const first=await iterator.next();assert.equal(first.done,false,error);const inbox=JSON.parse(first.value!);assert.equal(inbox.wake_pending,true);assert.equal((await ack(f,c,inbox)).status,200);
  const updated=frames.filter(x=>x.type==='session.command').at(-1)!.command;assert.ok(updated.wake.ack_deadline_ms);assert.equal(updated.id,packet.id);child.stdin.write(JSON.stringify(updated)+'\n');
  const second=await iterator.next();assert.equal(second.done,false,error);const result=JSON.parse(second.value!);assert.ok(result.wake_proof);assert.equal((await ack(f,c,result)).status,200);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 }finally{child.stdin.end();await closed;clearTimeout(timer);lines.close();}
 assert.equal(child.exitCode,0,error);
 }finally{rmSync(owned,{recursive:true,force:true});}
});
test('linked intake waiting and missed handoff are durable, scoped and idempotent without completion',async()=>{
 const db=new DatabaseSync(':memory:');let alarm:any=null;
 const ctx:any={storage:{sql:{exec(q:string,...a:any[]){const p=db.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){db.exec('BEGIN');try{const r=fn();db.exec('COMMIT');return r;}catch(e){db.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(v:any){alarm=v;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};
 const stub={idFromName:(x:string)=>x,get:()=>({fetch:async()=>Response.json({ok:true,registered:true})})};
 const project=new ProjectDO(ctx,{CONTROL:stub,MACHINES:stub,ACCOUNTS:stub,MAILBOXES:stub} as any);
 const call=(p:string,b?:any,a:any=admin)=>project.fetch(new Request('https://project.test'+p,{method:b?'POST':'GET',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b?{body:JSON.stringify(b)}:{})}));
 const created=await call('/api/intakes?project=p1',{key:'fixture',title:'Read source',text:'Read-only source audit',acceptance_criteria:['Report'],code_task:false});assert.equal(created.status,201,await created.clone().text());
 const row=db.prepare('SELECT id FROM intakes').get() as any;db.prepare("UPDATE intakes SET session_id='s1' WHERE id=?").run(row.id);
 db.prepare("INSERT INTO dispatch_sagas(operation_id,request_digest,request_key,intake_id,session_id,machine_id,attempt_operation_id,launch_spec_json,epoch,state,attempt,created_at,updated_at) VALUES('op1','d','k',?,'s1','h1','op1:0','{}',1,'running',0,?,?)").run(row.id,Date.now(),Date.now());(project as any).db.cache.clear();
 const w={wake_id:'a'.repeat(64),session_id:'s1',attempt_operation_id:'op1:0',epoch:1,intake_id:row.id,state:'pending',wake_at_ms:Date.now()+60000,missed_at:null};
 assert.equal((await call('/_internal/session-wake',w)).status,200);const view=await(await call('/api/intakes/'+row.id+'/context')).json() as any;assert.ok(view.waiting_until);
 assert.equal((await call('/_internal/session-wake',{...w,epoch:2})).status,409);
 assert.equal((await call('/_internal/session-wake',{...w,state:'delivered'})).status,200);
 const missed={...w,state:'missed',missed_at:Date.now()};assert.equal((await call('/_internal/session-wake',missed)).status,200);assert.equal((await call('/_internal/session-wake',missed)).status,200);
 assert.equal(db.prepare("SELECT COUNT(*) AS n FROM intake_events WHERE kind='developer_alert_raised'").get()!.n,1);
 const end=await(await call('/api/intakes/'+row.id+'/context')).json() as any;assert.equal(end.waiting_until,undefined);assert.notEqual(end.intake.status,'done');
 assert.equal((await call('/_internal/session-wake',w)).status,200);assert.equal((await(await call('/api/intakes/'+row.id+'/context')).json() as any).waiting_until,undefined);
});
test('archive failure cannot forget a wake or duplicate its proven effect',async()=>{
 const f=legacyFixture();await f.boot();await publish(f);await create(f);const c=command(f);await authorize(f,c);const result=proof(c),put=f.storage.put;let once=true;
 f.storage.put=async(k:string,v:any)=>{if(once&&k.startsWith('session-wake-history/')){once=false;throw new Error('synthetic_archive_failure');}return put(k,v);};
 assert.equal((await ack(f,c,result)).status,500);f.storage.put=put;f.restart();await(f.machine as any).wakes.sweep();
 assert.equal((await history(f)).wakes[0].wake_state,'acked');assert.equal(f.db.prepare('SELECT COUNT(*) AS n FROM session_commands').get()!.n,1);
});

test('older hosts never receive a wake annotation as an unsafe ordinary PTY send',async()=>{
 const f=fixture();await f.boot(true,false);await publish(f);const frames:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'old-host'}),send:(s:string)=>frames.push(JSON.parse(s))});
 await create(f);assert.ok(command(f));assert.equal(frames.filter(x=>x.type==='session.command').length,0);
 assert.equal((await f.call('/api/sessions/s1/commands','POST',{kind:'send',key:'normal',text:'ordinary preserved'})).status,201);
 assert.equal(frames.filter(x=>x.type==='session.command').length,1);assert.equal(frames.find(x=>x.type==='session.command').command.wake,undefined);
});

test('restart drains stale archived active keys across pages without truncating a pending receipt',async()=>{
 const f=fixture();await f.boot();await create(f,'pending',new Date(Date.now()+60000).toISOString());
 const original=[...f.kv.values()].find((w:any)=>w.request_key==='pending') as any;
 for(let i=0;i<250;i++){const id=i.toString(16).padStart(64,'0'),old={...original,id,state:'acked',notified:true};f.kv.set('session-wake-active/s1/'+id,old);f.kv.set('session-wake-history/s1/'+id,old);}
 f.restart();await f.call('/api/sessions/s1');assert.equal([...f.kv.keys()].filter(k=>k.startsWith('session-wake-active/')).length,1);
 const replay=await(await create(f,'pending',new Date(original.wake_at_ms).toISOString())).json() as any;assert.equal(replay.wake_id,original.id);assert.equal(replay.replay,true);assert.equal(command(f),undefined);
});

test('future pending wake holds only idle reaping of its exact fresh living incarnation',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=fixture();await f.boot();await create(f,'future',new Date(now+8*3600_000).toISOString());now+=6*3600_000+1000;
 const beat=(phase:string)=>f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:v1'],native_sessions:[{session_id:'s1',native_id:native,phase,last_activity_at:now-6*3600_000-1000,exit_code:null,containment_empty:false}]},host);
 assert.equal((await beat('running')).status,200);await publish(f,now);await f.machine.alarm();assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM session_commands WHERE kind='stop'").get()!.n,0);assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'running');
 await beat('exited');await f.machine.alarm();assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM session_commands WHERE kind='stop'").get()!.n,1);assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'running');
 }finally{Date.now=original;}
});
test('private wake states cannot FIFO-block ordinary commands on current or old readers',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=fixture();await f.boot();await publish(f);await create(f);const c=command(f);assert.equal(c.state,'wake_pending');
 const ordinary=await f.call('/api/sessions/s1/commands','POST',{kind:'capture',key:'ordinary'});assert.equal(ordinary.status,201);
 assert.equal(f.db.prepare("SELECT kind FROM session_commands WHERE machine_id='h1' AND state='pending' ORDER BY CASE WHEN kind='stop' THEN 0 ELSE 1 END,created_at,id LIMIT 1").get()!.kind,'capture');
 now+=600001;await(f.machine as any).wakes.sweep();assert.equal(command(f).state,'wake_failed');
 assert.equal(f.db.prepare("SELECT kind FROM session_commands WHERE machine_id='h1' AND state='pending' ORDER BY created_at,id LIMIT 1").get()!.kind,'capture');
 }finally{Date.now=original;}
});
test('late initial inbox receipt gets one immutable separate ACK window and no renewal',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=legacyFixture();await f.boot();await publish(f);await create(f);const c=command(f);now+=590000;await authorize(f,c);
 const first=JSON.parse(c.result_json);assert.equal(first.wake_delivered_at_ms,now);assert.equal(first.wake_binding.ack_deadline_ms,now+600000);
 now+=20000;await(f.machine as any).wakes.sweep();assert.equal((await history(f)).wakes[0].wake_state,'delivered');
 await authorize(f,c);const repeated=JSON.parse(c.result_json);assert.equal(repeated.wake_delivered_at_ms,first.wake_delivered_at_ms);assert.equal(repeated.wake_binding.ack_deadline_ms,first.wake_binding.ack_deadline_ms);
 now=first.wake_binding.ack_deadline_ms+1;await(f.machine as any).wakes.sweep();assert.equal((await history(f)).wakes[0].reason,'wake_not_acked_within_10m');
 }finally{Date.now=original;}
});
test('initial inbox receipt after the due delivery deadline cannot mint a fresh ACK window',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=legacyFixture();await f.boot();await publish(f);await create(f);const c=command(f);now+=600001;
 const response=await ack(f,c,{wake_pending:true,native_message_id:'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'});assert.equal(response.status,200);assert.equal((await response.json() as any).expired,true);
 assert.equal(JSON.parse(command(f).result_json).wake_binding.ack_deadline_ms,undefined);assert.equal(command(f).state,'wake_failed');assert.equal((await history(f)).wakes[0].wake_state,'missed');
 }finally{Date.now=original;}
});

test('slow overlapping receipt bodies cannot renew the first committed ACK deadline',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=legacyFixture();await f.boot();await publish(f);await create(f);const c=command(f);let release:()=>void=()=>{};
 const body=new ReadableStream({start(controller){release=()=>{controller.enqueue(new TextEncoder().encode(JSON.stringify({epoch:c.epoch,result:{wake_pending:true,native_message_id:'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'}})));controller.close();};}});
 const request=new Request('https://machine.test/api/sessions/s1/commands/'+c.id+'/ack',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(host)},body,duplex:'half'} as any);
 const slow=f.machine.fetch(request);await Promise.resolve();await authorize(f,c);const first=JSON.parse(command(f).result_json).wake_binding.ack_deadline_ms;now+=5000;release();assert.equal((await slow).status,200);assert.equal(JSON.parse(command(f).result_json).wake_binding.ack_deadline_ms,first);
 }finally{Date.now=original;}
});

test('wake notification backlog is private to the new reader without consuming old-reader delivery slots',async()=>{
 const f=fixture();await f.boot();f.failProject();assert.equal((await create(f,'notify',new Date(Date.now()+60000).toISOString())).status,201);
 assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM machine_outbox WHERE kind='wake' AND state='pending'").get()!.n,0);
 assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM machine_outbox WHERE kind='wake' AND state='wake_pending'").get()!.n,1);
 const row=f.db.prepare("SELECT id FROM machine_outbox WHERE kind='wake'").get() as any;f.failProject(false);await(f.machine as any).flushOutbox(Date.now()+600000);assert.equal(f.db.prepare('SELECT state FROM machine_outbox WHERE id=?').get(row.id)!.state,'delivered');assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'running');
});

// The original activity observation deliberately remains old: a qualified wake
// is an idle-only bounded hold, never fabricated provider/PTY progress.
test('future wake crosses due and timely ACK without idle STOP until its immutable window ends',async()=>{
 const original=Date.now;let now=original();Date.now=()=>now;
 try{const f=legacyFixture();await f.boot();const activity=now-7*3600_000,due=now+1000;
 const fresh=async()=>{assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:v1'],native_sessions:[{session_id:'s1',native_id:native,phase:'running',last_activity_at:activity,exit_code:null,containment_empty:false}]},host)).status,200);await publish(f,now);};
 await create(f,'crossing',new Date(due).toISOString());await fresh();await f.machine.alarm();
 const stops=()=>Number(f.db.prepare("SELECT COUNT(*) AS n FROM session_commands WHERE kind='stop'").get()!.n);
 assert.equal(stops(),0);now=due;await fresh();await f.machine.alarm();const c=command(f);assert.ok(c);assert.equal(stops(),0);assert.equal((await history(f)).waiting_until,undefined);
 now+=590000;await fresh();await authorize(f,c);const deadline=JSON.parse(c.result_json).wake_binding.ack_deadline_ms;await f.machine.alarm();assert.equal(stops(),0);assert.equal((await history(f)).wakes[0].wake_state,'delivered');
 now+=1000;assert.equal((await ack(f,c,proof(c))).status,200);f.restart();await fresh();await f.machine.alarm();assert.equal(stops(),0);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 now=deadline-1;await fresh();await f.machine.alarm();assert.equal(stops(),0);
 assert.equal(f.db.prepare("SELECT last_activity_at FROM native_sessions WHERE session_id='s1'").get()!.last_activity_at,activity);
 now=deadline;await fresh();await f.machine.alarm();assert.equal(stops(),1);assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'running');
 }finally{Date.now=original;}
});
test('qualified STOP and replacement immediately invalidate future wake scheduling and waiting',async()=>{
 const original=Date.now;const now=original();Date.now=()=>now;
 try{for(const replacement of [false,true]){const f=fixture();await f.boot();await publish(f);await create(f,'future-terminal',new Date(now+8*3600_000).toISOString());
 if(replacement){f.edit("UPDATE sessions SET operation_id='replacement',epoch=epoch+1 WHERE id='s1'");await(f.machine as any).scheduleNext();}
 else{const response=await f.call('/api/sessions/s1/commands','POST',{kind:'stop',key:'normal-qualified-stop'});assert.equal(response.status,201);const c=(await response.json() as any).command;assert.equal((await f.call('/api/sessions/s1/commands/'+c.id+'/ack','POST',{epoch:c.epoch,result:{stopped:true,evidence:'native_exit',native_id:native}},host)).status,200);assert.equal(f.db.prepare('SELECT state FROM sessions').get()!.state,'stopped');}
 assert.ok(f.alarm!==null&&f.alarm<=now+1000);assert.equal((await history(f)).waiting_until,undefined);await f.machine.alarm();assert.equal(command(f),undefined);assert.equal((await history(f)).wakes[0].reason,'wake_session_gone');assert.equal(Number(f.db.prepare("SELECT COUNT(*) AS n FROM session_commands WHERE kind='stop'").get()!.n),replacement?0:1);
 }}finally{Date.now=original;}
});


/** Explicit existing-inflight compatibility fixture: the immutable 7c server
 * producer writes a legacy binding BEFORE delivery, rather than rewriting any
 * newly-created strict record. All consumers are the current MachineDO.
 */
type CommandRow={session_id:string;epoch:number};
type LegacyWakeProducer={
 wakeSession(id:string):WakeSession|undefined;
 db:{one<T>(sql:string,...values:unknown[]):T|undefined;transaction(ctx:unknown,fn:()=>void):void;run(sql:string,...values:unknown[]):unknown};
 nativeInventory:{status():{fresh?:boolean}};ctx:unknown;
};
function legacyFixture(){
 const f=fixture();
 function install(){(f.machine as any).enqueueWake=function(this:LegacyWakeProducer,w:WakeRecord):boolean{
    const current=this.wakeSession(w.session_id);
    if(!current||current.operation_id!==w.operation_id||current.epoch!==w.epoch||current.native_id!==w.native_id||current.state!=='running'||!current.interactive)return false;
    const prior=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?',w.command_id);
    if(prior)return prior.session_id===w.session_id&&prior.epoch===w.epoch;
    const native=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',w.native_id);
    const observed=native?JSON.parse(native.row_json) as JsonRecord:null;
    if(this.nativeInventory.status().fresh!==true||!observed||!Number.isSafeInteger(observed.created_at_ms)||Number(observed.created_at_ms)<=0||observed.tag!==`task-hub2-${w.session_id}`||observed.workspace!==w.workspace)return false;
    w.native_birth=Number(observed.created_at_ms);
    const binding={id:w.id,session_id:w.session_id,operation_id:w.operation_id,epoch:w.epoch,native_id:w.native_id,tag:observed.tag,workspace:w.workspace,created_at_ms:w.native_birth,deadline_ms:w.wake_at_ms+600000};
    this.db.transaction(this.ctx,()=>{
      const row=this.wakeSession(w.session_id);if(!row||row.operation_id!==w.operation_id||row.epoch!==w.epoch||row.native_id!==w.native_id||row.state!=='running')throw new HttpError('stale_wake',409);
      this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'wake_send',?,'wake_pending',?,?,?)`,w.command_id,w.session_id,w.machine_id,w.epoch,'hub-wake:'+w.id,w.id,w.prompt,JSON.stringify({wake_binding:binding}),Date.now(),Date.now());
    });return true;
  };}
 const restart=f.restart;f.restart=()=>{restart();install();};install();return f;
}

/** Immutable 035a direct-strict producer: existing inflight records stay direct. */
function directFixture(){
 const f=fixture();
 function install(){(f.machine as any).enqueueWake=function(this:LegacyWakeProducer,w:WakeRecord):boolean{
    const current=this.wakeSession(w.session_id);
    if(!current||current.operation_id!==w.operation_id||current.epoch!==w.epoch||current.native_id!==w.native_id||current.state!=='running'||!current.interactive)return false;
    const prior=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?',w.command_id);
    if(prior)return prior.session_id===w.session_id&&prior.epoch===w.epoch;
    const native=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',w.native_id);
    const observed=native?JSON.parse(native.row_json) as JsonRecord:null;
    if(this.nativeInventory.status().fresh!==true||!observed||!Number.isSafeInteger(observed.created_at_ms)||Number(observed.created_at_ms)<=0||observed.tag!==`task-hub2-${w.session_id}`||observed.workspace!==w.workspace)return false;
    w.native_birth=Number(observed.created_at_ms);
    const binding={id:w.id,session_id:w.session_id,operation_id:w.operation_id,epoch:w.epoch,native_id:w.native_id,tag:observed.tag,workspace:w.workspace,created_at_ms:w.native_birth,deadline_ms:w.wake_at_ms+600000,transport:'strict-submit:v1'};
    this.db.transaction(this.ctx,()=>{
      const row=this.wakeSession(w.session_id);if(!row||row.operation_id!==w.operation_id||row.epoch!==w.epoch||row.native_id!==w.native_id||row.state!=='running')throw new HttpError('stale_wake',409);
      this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'wake_send',?,'wake_pending',?,?,?)`,w.command_id,w.session_id,w.machine_id,w.epoch,'hub-wake:'+w.id,w.id,w.prompt,JSON.stringify({wake_binding:binding}),Date.now(),Date.now());
    });return true;
  };}
 const restart=f.restart;f.restart=()=>{restart();install();};install();return f;
}

async function strictFixture(){
 const f=directFixture();await f.boot();await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:strict-submit:v1'],native_sessions:[]},host);await publish(f);return f;
}
async function strictProof(c:any){const b=JSON.parse(c.result_json).wake_binding;return{delivery:'native_acknowledged',delivered_at_ms:Date.now(),wake_proof:{kind:'strict_submit_v1',epoch:b.epoch,operation_id:b.operation_id,receipt:{version:1,session_id:b.native_id,operation_id:c.id,request_sha256:await sha256Hex(JSON.stringify([b.native_id,c.id,b.tag,b.workspace,b.created_at_ms,b.deadline_ms,c.text])),deadline_ms:b.deadline_ms,status:'submitted_verified',mutation:true,landed:true,submitted:true,consumed:null,reason:'submission_observed'}}};}
test('strict wake capability selects durable explicit transport without a mailbox ID',async()=>{
 const f=await strictFixture();await create(f);const c=command(f),b=JSON.parse(c.result_json).wake_binding;
 assert.equal(b.transport,'strict-submit:v1');assert.equal(b.native_message_id,undefined);const result=await strictProof(c);
 assert.equal((await ack(f,c,result)).status,200);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 assert.equal((await ack(f,c,result)).status,200);assert.equal(f.db.prepare('SELECT COUNT(*) AS n FROM session_commands').get()!.n,1);
});
test('strict pending never manufactures an inbox receipt or renews original deadline',async()=>{
 const f=await strictFixture();await create(f);const c=command(f),binding=JSON.parse(c.result_json).wake_binding;
 assert.equal((await ack(f,c,{wake_pending:true,transport:'strict-submit:v1'})).status,200);
 assert.deepEqual(JSON.parse(command(f).result_json).wake_binding,binding);assert.equal(JSON.parse(command(f).result_json).wake_delivered_at_ms,undefined);
 assert.equal((await ack(f,c,{wake_pending:true,native_message_id:crypto.randomUUID()})).status,400);
});
test('strict receipt binds payload command attempt epoch and every native guard',async()=>{
 const f=await strictFixture();await create(f);const c=command(f),good=await strictProof(c);
 for(const change of [{session_id:crypto.randomUUID()},{operation_id:crypto.randomUUID()},{request_sha256:'0'.repeat(64)},{deadline_ms:0},{status:'uncertain'},{mutation:false},{landed:false},{submitted:false},{consumed:true},{reason:'raw error'},{extra:'secret'},{version:true}]){
  const r=structuredClone(good);Object.assign(r.wake_proof.receipt,change);assert.equal((await ack(f,c,r)).status,409);
 }
 for(const change of [{epoch:c.epoch+1},{operation_id:'foreign'},{extra:true}]){const r=structuredClone(good);Object.assign(r.wake_proof,change);assert.equal((await ack(f,c,r)).status,409);}
 const wrong=structuredClone(c);wrong.text='changed';assert.equal((await ack(f,c,await strictProof(wrong))).status,409);
 assert.equal((await ack(f,c,{...good,wake_proof:proof(c).wake_proof})).status,409);
 assert.equal(command(f).state,'wake_pending');
});
test('strict transport is withheld from rolled-back host while ordinary commands still flow',async()=>{
 const f=await strictFixture();await create(f);const c=command(f);await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:v1'],native_sessions:[]},host);
 const frames:any[]=[];const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))};f.sockets.push(socket);
 await (f.machine as any).sendPendingCommands(socket,'h1');assert.ok(!frames.some(x=>x.command?.id===c.id));
 await f.call('/api/sessions/s1/commands','POST',{kind:'capture',key:'normal-after-wake'});assert.ok(frames.some(x=>x.command?.kind==='capture'));
});
test('strict expiry and incarnation replacement never turn an uncertain native result into ACK',async()=>{
 const f=await strictFixture();await create(f);const c=command(f),result=await strictProof(c);result.delivered_at_ms=JSON.parse(c.result_json).wake_binding.deadline_ms+1;
 const clock=Date.now;Date.now=()=>result.delivered_at_ms+1;
 try{assert.equal((await ack(f,c,result)).status,409);}finally{Date.now=clock;}
 f.edit("UPDATE sessions SET epoch=epoch+1 WHERE id='s1'");assert.equal((await ack(f,c,await strictProof(c))).status,409);
});

test('actual service-context Bridge strict receipt crosses MachineDO ACK with no sender impersonation',async()=>{
 const f=await strictFixture();const frames:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))});
 await create(f);const c=command(f),packet=frames.find(x=>x.type==='session.command').command;assert.equal(packet.kind,'send');assert.ok(packet.wake);
 const hostRoot=fileURLToPath(new URL('../host',import.meta.url)),script=fileURLToPath(new URL('../host/tests/test_session_wakes.py',import.meta.url));
 const parent=process.env.TMPDIR||fileURLToPath(new URL('../../.local/tmp',import.meta.url));
 mkdirSync(parent,{recursive:true,mode:0o700});const owned=mkdtempSync(join(parent,'wake-bridge-'));
 try{
 const child=spawn('/usr/bin/python3',['-I','-S','-c','import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")',hostRoot,script,'--strict-wire'],{stdio:['pipe','pipe','pipe'],env:{...process.env,HOME:owned,TMPDIR:owned}});
 const lines=createInterface({input:child.stdout}),iterator=lines[Symbol.asyncIterator](),closed=once(child,'close');let error='';child.stderr.on('data',b=>error=(error+String(b)).slice(-8192));
 const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
 try{
  child.stdin.write(JSON.stringify(packet)+'\n');const first=await iterator.next();assert.equal(first.done,false,error);const result=JSON.parse(first.value!);assert.equal(result.wake_proof.kind,'strict_submit_v1');assert.equal((await ack(f,c,result)).status,200);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 }finally{child.stdin.end();await closed;clearTimeout(timer);lines.close();}
 assert.equal(child.exitCode,0,error);
 }finally{rmSync(owned,{recursive:true,force:true});}
});


test('new wake on old-marker host is strictly held and resumes same command key and original deadline',async()=>{
 const f=fixture();await f.boot();await publish(f);const frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))};f.sockets.push(socket);
 const response=await create(f,'held-strict');assert.equal(response.status,201);const created=await response.json() as any,c=command(f),binding=JSON.parse(c.result_json).wake_binding;
 assert.equal(binding.transport,'strict-inbox:v1');assert.equal(binding.native_message_id,undefined);assert.equal(c.state,'wake_pending');
 assert.equal(frames.filter(x=>x.type==='session.command').length,0);
 assert.equal((await f.call('/api/sessions/s1/commands','POST',{kind:'capture',key:'ordinary-while-held'})).status,201);
 const ordinary=frames.find(x=>x.type==='session.command');assert.equal(ordinary.command.kind,'capture');assert.equal(ordinary.command.wake,undefined);
 await f.call('/api/sessions/s1/commands/'+ordinary.command.id+'/ack','POST',{epoch:ordinary.command.epoch,result:{output:'synthetic-readonly'}},host);
 f.restart();assert.equal((await create(f,'held-strict')).status,200);assert.equal(command(f).id,c.id);assert.deepEqual(JSON.parse(command(f).result_json).wake_binding,binding);
 await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:strict-inbox:v1'],native_sessions:[]},host);
 await (f.machine as any).sendPendingCommands(socket,'h1');const resumed=frames.filter(x=>x.type==='session.command'&&x.command.wake).at(-1)!.command;
 assert.equal(resumed.id,c.id);assert.equal(resumed.wake.deadline_ms,binding.deadline_ms);assert.deepEqual(resumed.wake,binding);
 const again=await(await create(f,'held-strict')).json() as any;assert.equal(again.wake_id,created.wake_id);assert.equal(command(f).id,c.id);
 assert.equal((await ack(f,c,await inboxProof(c))).status,200);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 assert.equal(f.db.prepare("SELECT COUNT(*) AS n FROM session_commands WHERE kind='wake_send'").get()!.n,1);
});


test('explicit historical producer stale transaction guard raises typed409 without a command',async()=>{
 const f=legacyFixture();await f.boot();await publish(f);await create(f,'legacy-stale',new Date(Date.now()+60000).toISOString());
 const wake=[...f.kv.values()].find((v:any)=>v?.request_key==='legacy-stale') as WakeRecord;assert.ok(wake);
 const machine=f.machine as any,original=machine.wakeSession.bind(machine);let reads=0;
 machine.wakeSession=(id:string)=>{const row=original(id);return ++reads===2?{...row,epoch:row.epoch+1}:row;};
 try{assert.throws(()=>machine.enqueueWake(structuredClone(wake)),(error:any)=>error instanceof ExpectedHttpError&&error.code==='stale_wake'&&error.status===409);}
 finally{machine.wakeSession=original;}
 assert.equal(reads,2);assert.equal(command(f),undefined);assert.equal((await history(f)).wakes[0].wake_state,'pending');
});

async function inboxFixture(workspace='/owned/work'){const f=fixture(workspace);await f.boot();await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:strict-inbox:v1'],native_sessions:[]},host);await publish(f);return f;}
async function inboxProof(c:any){const result=await strictProof(c),b=JSON.parse(c.result_json).wake_binding,id='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';Object.assign(result.wake_proof,{kind:'strict_inbox_notice_v1',envelope_id:id,body_sha256:await sha256Hex(c.text)});result.wake_proof.receipt.request_sha256=await sha256Hex(JSON.stringify([b.native_id,c.id,b.tag,b.workspace,b.created_at_ms,b.deadline_ms,'Read inbox message '+id]));return result;}
test('inbox notice proof binds full body and real envelope separately from short strict submission',async()=>{
 const f=await inboxFixture();await create(f);const c=command(f),good=await inboxProof(c);assert.equal(JSON.parse(c.result_json).wake_binding.transport,'strict-inbox:v1');
 for(const change of [{body_sha256:'0'.repeat(64)},{envelope_id:crypto.randomUUID()},{kind:'strict_submit_v1'},{extra:true}]){const r=structuredClone(good);Object.assign(r.wake_proof,change);assert.equal((await ack(f,c,r)).status,409);}
 assert.equal((await ack(f,c,await strictProof(c))).status,409);
 assert.equal((await ack(f,c,good)).status,200);assert.equal((await ack(f,c,good)).status,200);
 assert.equal((await history(f)).wakes[0].wake_state,'acked');assert.equal(good.wake_proof.receipt.consumed,null);
});
test('new inbox transport does not leak into historical direct host while normal commands flow',async()=>{
 const f=await inboxFixture();await create(f);const c=command(f);await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:strict-submit:v1'],native_sessions:[]},host);
 const frames:any[]=[];const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))};f.sockets.push(socket);
 await(f.machine as any).sendPendingCommands(socket,'h1');assert.equal(frames.length,0);
 await f.call('/api/sessions/s1/commands','POST',{kind:'capture',key:'ordinary-inbox-held'});assert.equal(frames.at(-1).command.kind,'capture');assert.equal(command(f).id,c.id);
});

test('actual service-context Bridge inbox notice receipt crosses MachineDO ACK with no sender impersonation',async()=>{
 const parent=process.env.TMPDIR||fileURLToPath(new URL('../../.local/tmp',import.meta.url));
 mkdirSync(parent,{recursive:true,mode:0o700});const owned=mkdtempSync(join(parent,'wake-inbox-bridge-'));
 try{
 const f=await inboxFixture(owned);const frames:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))});
 await create(f);const c=command(f),packet=frames.find(x=>x.type==='session.command').command;assert.equal(packet.kind,'send');assert.ok(packet.wake);
 const hostRoot=fileURLToPath(new URL('../host',import.meta.url)),script=fileURLToPath(new URL('../host/tests/test_session_wakes.py',import.meta.url));

 const child=spawn('/usr/bin/python3',['-I','-S','-c','import runpy,sys;sys.path.insert(0,sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name="__main__")',hostRoot,script,'--inbox-wire'],{stdio:['pipe','pipe','pipe'],env:{...process.env,HOME:owned,TMPDIR:owned}});
 const lines=createInterface({input:child.stdout}),iterator=lines[Symbol.asyncIterator](),closed=once(child,'close');let error='';child.stderr.on('data',b=>error=(error+String(b)).slice(-8192));
 const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
 try{
  child.stdin.write(JSON.stringify(packet)+'\n');const first=await iterator.next();assert.equal(first.done,false,error);const result=JSON.parse(first.value!);assert.equal(result.wake_proof.kind,'strict_inbox_notice_v1');assert.equal((await ack(f,c,result)).status,200);assert.equal((await history(f)).wakes[0].wake_state,'acked');
 }finally{child.stdin.end();await closed;clearTimeout(timer);lines.close();}
 assert.equal(child.exitCode,0,error);
 }finally{rmSync(owned,{recursive:true,force:true});}
});

test('new host retains existing direct strict command eligibility without changing its transport',async()=>{
 const f=await strictFixture();await create(f);const c=command(f),b=JSON.parse(c.result_json).wake_binding;
 const frames:any[]=[];const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:host.id,generation:'1',connected_at:Date.now(),connection_id:'qualified-host'}),send:(s:string)=>frames.push(JSON.parse(s))};f.sockets.push(socket);
 await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['interactive','session-wakes:strict-inbox:v1'],native_sessions:[]},host);
 await(f.machine as any).sendPendingCommands(socket,'h1');assert.equal(frames.at(-1).command.id,c.id);assert.deepEqual(frames.at(-1).command.wake,b);
 assert.equal(b.transport,'strict-submit:v1');assert.equal((await ack(f,c,await strictProof(c))).status,200);
});
test('different inbox command cannot reuse another completed envelope UUID',async()=>{
 const f=await inboxFixture();await create(f,'first');const first=command(f);assert.equal((await ack(f,first,await inboxProof(first))).status,200);
 await create(f,'second');const second=f.db.prepare("SELECT * FROM session_commands WHERE kind='wake_send' AND id<>?").get(first.id) as any;
 assert.ok(second);assert.equal((await ack(f,second,await inboxProof(second))).status,409);assert.equal(second.state,'wake_pending');
});
