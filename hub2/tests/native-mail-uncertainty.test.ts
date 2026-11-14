import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MachineDO } from '../src/machine-do.ts';
import Worker from '../src/worker.ts';

const native='11111111-2222-4333-8444-555555555555';
function fixture(){
 const database=new DatabaseSync(':memory:'),sockets:any[]=[],checks:string[]=[];
 let alarm:number|null=null,authorized=true,delivery=true,afterAuth:undefined|(()=>void);
 const ctx:any={storage:{sql:{exec(query:string,...args:any[]){
  const q=database.prepare(query);if(q.columns().length){const rows=q.all(...args);return{toArray:()=>rows,rowsRead:rows.length,rowsWritten:0};}
  const result=q.run(...args);return{toArray:()=>[],rowsRead:0,rowsWritten:Number(result.changes)};
 }},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const v=fn();database.exec('COMMIT');return v;}catch(e){database.exec('ROLLBACK');throw e;}},
 async getAlarm(){return alarm;},async setAlarm(v:number){alarm=v;},async deleteAlarm(){alarm=null;}},
 blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(tag:string){return sockets.filter(s=>s.tags.includes(tag));}};
 const env:any={CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
  const request=input instanceof Request?input:new Request(input,init),path=new URL(request.url).pathname;
  if(path==='/_authorize'){
   const b=await request.json() as any;checks.push(b.scope);
   if(b.scope==='message:delivery'&&afterAuth){const fn=afterAuth;afterAuth=undefined;fn();}
   return Response.json({authorized:true},{status:authorized&&(b.scope!=='message:delivery'||delivery)?200:403});
  }
  return Response.json({authorized:true});
 }})},PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})}};
 let machine=new MachineDO(ctx,env);
 const actor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
 const call=(path:string,body?:unknown)=>machine.fetch(new Request('https://machine.test'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 const clear=()=>((machine as any).db.cache.clear());
 function socket(extra:any={},rank=1){const frames:any[]=[],value:any={tags:['hub2:h1'],readyState:1,
  deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connection_id:String(rank),connected_at:rank,...extra}),
  send:(raw:string)=>frames.push(JSON.parse(raw)),close(){this.readyState=3;}};sockets.push(value);return{value,frames};}
 return{database,call,checks,sockets,socket,clear,env,get machine(){return machine;},restart(){machine=new MachineDO(ctx,env);},
  revoke(){authorized=false;},denyDelivery(){delivery=false;},race(fn:()=>void){afterAuth=fn;}};
}
type F=ReturnType<typeof fixture>;
async function seed(f:F){
 assert.equal((await f.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless']})).status,200);
 const r=await f.call('/_internal/reserve',{operation_id:'launch-s',dispatch_id:'dispatch-s',attempt:0,session_id:'s',project_id:'p',intake_id:'i',machine_id:'h1',request_digest:'d',title:'synthetic',acceptance_criteria:['test'],launch:{workspace:'/owned/fixture',engine:'codex',model:'gpt-6.1-sol',prompt:'synthetic',interactive:false}});
 assert.equal(r.status,201);const {epoch}=await r.json() as any;
 f.database.prepare("UPDATE sessions SET state='running',native_id=? WHERE id='s'").run(native);f.clear();
 return epoch;
}
const mail=(id='first')=>({message_id:id,epoch:7,operation_id:'native:'+id+':7',session_id:'s',recipient:'agent:target',body:'synthetic '+id});
const uncertainty=(changes:any={})=>({type:'host.error',stage:'host_mailbox',reason:'native_mail_outcome_uncertain',session_id:'s',message_id:'first',operation_id:'native:first:7',message_epoch:7,native_id:native,...changes});
const row=(f:F,id='first')=>f.database.prepare('SELECT * FROM native_mail WHERE message_id=?').get(id) as any;
const report=(f:F,socket:any,changes:any={})=>f.machine.webSocketMessage(socket,JSON.stringify(uncertainty(changes)));
const queued=(frames:any[])=>frames.filter(v=>v.type==='mailbox.deliver').map(v=>v.message.id);
async function enqueue(f:F,id='first'){assert.equal((await f.call('/_internal/mail',mail(id))).status,200);}

test('native mail quarantine is durable, idempotent and unblocks later pending without receipt',async()=>{
 const f=fixture();try{
  await seed(f);await enqueue(f);const peer=f.socket();
  await report(f,peer.value);assert.equal(row(f).state,'outcome_uncertain');assert.equal(row(f).receipt_digest,null);
  const first={...row(f)};await report(f,peer.value);assert.deepEqual({...row(f)},first);
  await enqueue(f);assert.equal(row(f).state,'outcome_uncertain');
  assert.equal((await f.call('/_internal/mail',{...mail(),body:'changed'})).status,409);
  await enqueue(f,'later');assert.deepEqual(queued(peer.frames),['later']);
  f.restart();const reconnect=f.socket({},2);
  await f.machine.webSocketMessage(reconnect.value,JSON.stringify({type:'resync'}));
  assert.deepEqual(queued(reconnect.frames),['later']);assert.deepEqual({...row(f)},first);
  f.database.prepare("UPDATE native_mail SET created_at=0 WHERE message_id='first'").run();f.clear();
  await f.machine.alarm();assert.equal(row(f).state,'outcome_uncertain');assert.equal(row(f).receipt_digest,null);
 }finally{f.database.close();}
});

test('native mail uncertainty requires exact typed route epoch, stage, reason, native and tuple',async()=>{
 const changes:any[]=[{message_epoch:undefined},{message_epoch:true},{message_epoch:0},{message_epoch:-1},{message_epoch:1.5},{message_epoch:'7'},
  {message_epoch:1},{message_id:'foreign'},{operation_id:'foreign'},{session_id:'foreign'},
  {native_id:undefined},{native_id:'foreign'},{stage:undefined},{stage:'host_command'},{stage:'host_launch'},{reason:'native_mail_effect_unconfirmed'}];
 for(const change of changes){const f=fixture();try{const launchEpoch=await seed(f);assert.notEqual(launchEpoch,7);await enqueue(f);const peer=f.socket();
  await report(f,peer.value,change);assert.equal(row(f).state,'pending',JSON.stringify(change));assert.equal(row(f).receipt_digest,null);
 }finally{f.database.close();}}
});

test('native mail quarantine refuses revoked, scope-denied, stale, foreign and stop-only sockets',async()=>{
 for(const kind of ['revoked','scope','stale','foreign','stop','revoked-during','superseded-during']){const f=fixture();try{
  await seed(f);await enqueue(f);const peer=f.socket(kind==='foreign'?{machine_id:'h2'}:kind==='stop'?{stop_only:true}:{});
  if(kind==='revoked')f.revoke();if(kind==='scope')f.denyDelivery();if(kind==='stale')f.socket({},2);
  if(kind==='revoked-during')f.race(()=>f.revoke());if(kind==='superseded-during')f.race(()=>{f.socket({},2);});
  await report(f,peer.value);assert.equal(row(f).state,'pending',kind);assert.equal(row(f).receipt_digest,null);
 }finally{f.database.close();}}
});

test('native mail quarantine rechecks session incarnation, state and mail after delivery authorization awaits',async()=>{
 for(const mutation of ["UPDATE sessions SET native_id='different' WHERE id='s'","UPDATE sessions SET state='lost' WHERE id='s'",
  "UPDATE sessions SET epoch=epoch+1 WHERE id='s'","UPDATE sessions SET operation_id='different' WHERE id='s'",
  "UPDATE native_mail SET state='cancelled' WHERE message_id='first'"]){const f=fixture();try{
  await seed(f);await enqueue(f);const peer=f.socket();f.race(()=>{f.database.exec(mutation);f.clear();});
  await report(f,peer.value);assert.notEqual(row(f).state,'outcome_uncertain',mutation);
 }finally{f.database.close();}}
});

test('native held mail authorizes only receipt, never a fresh claim, and valid late proof/cancel fences remain',async()=>{
 const f=fixture();try{
  await seed(f);await enqueue(f);const peer=f.socket();await report(f,peer.value);
  const authorize={...mail(),machine_id:'h1'};
  for(const phase of [undefined,'claim','other'])assert.equal((await f.call('/_internal/mail-authorize',{...authorize,phase})).status,404);
  assert.equal((await f.call('/_internal/mail-authorize',{...authorize,phase:'receipt'})).status,200);
  const proof={provider:'aplexer',message_id:'22222222-3333-4444-8555-666666666666',session_id:'s',recipient:'agent:target'};
  const late={message_id:'first',epoch:7,operation_id:mail().operation_id,native_receipt:proof};
  assert.equal((await f.call('/_internal/mail-ack',late)).status,200);const settled={...row(f)};
  assert.equal(settled.state,'acked');assert.ok(settled.receipt_digest);
  assert.equal((await f.call('/_internal/mail-ack',late)).status,200);
  assert.equal((await f.call('/_internal/mail-ack',{...late,native_receipt:{...proof,message_id:'foreign'}})).status,409);
  assert.equal((await f.call('/_internal/mail-cancel',mail())).status,409);
  await report(f,peer.value);assert.deepEqual({...row(f)},settled);
  await enqueue(f,'other');assert.equal((await f.call('/_internal/mail-cancel',mail('other'))).status,200);
  await report(f,peer.value,{message_id:'other',operation_id:mail('other').operation_id});assert.equal(row(f,'other').state,'cancelled');
  assert.equal((await f.call('/_internal/mail',mail('other'))).status,409);
  assert.equal((await f.call('/_internal/mail-ack',{...late,message_id:'other',operation_id:mail('other').operation_id})).status,409);
 }finally{f.database.close();}
});

test('native held receipt authorization survives session exit without granting a claim',async()=>{
 const f=fixture();try{
  await seed(f);await enqueue(f);await report(f,f.socket().value);
  f.database.exec("UPDATE sessions SET state='completed-awaiting-review',native_id=NULL WHERE id='s'");f.clear();
  const request={...mail(),machine_id:'h1'};
  assert.equal((await f.call('/_internal/mail-authorize',{...request,phase:'claim'})).status,404);
  assert.equal((await f.call('/_internal/mail-authorize',{...request,phase:'receipt'})).status,200);
  assert.equal((await f.call('/_internal/mail-ack',{message_id:'first',epoch:7,operation_id:mail().operation_id,
   native_receipt:{provider:'aplexer',message_id:'22222222-3333-4444-8555-666666666666',session_id:'s',recipient:'agent:target'}})).status,200);
  assert.equal(row(f).state,'acked');
 }finally{f.database.close();}
});

test('Worker forwards actual delivery phase so held claim is denied before Mailbox',async()=>{
 const f=fixture();try{
  await seed(f);await enqueue(f);await report(f,f.socket().value);assert.equal(row(f).state,'outcome_uncertain');
  const forwarded:any[]=[];
  const env:any={...f.env,CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
   const request=input instanceof Request?input:new Request(input,init);
   assert.equal(new URL(request.url).pathname,'/_verify');
   return Response.json({actor:{id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['message:delivery']}});
  }})},MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:(input:string|Request,init?:RequestInit)=>f.machine.fetch(input instanceof Request?input:new Request(input,init))})},
  MAILBOXES:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{forwarded.push(await request.json());assert.equal(request.headers.get('x-hub2-delivery-session'),'s');return Response.json({forwarded:true});}})}};
  for(const phase of [undefined,'claim','receipt']){
   const response=await Worker.fetch(new Request('https://hub.test/api/messages/first/delivered?mailbox=agent%3Atarget',{
    method:'POST',headers:{Authorization:'Bearer '+'a'.repeat(32),'Content-Type':'application/json'},
    body:JSON.stringify({phase,epoch:7,operation_id:mail().operation_id})}),env);
   assert.equal(response.status,phase==='receipt'?200:404,String(phase));
  }
  assert.equal(forwarded.length,1);assert.equal(forwarded[0].phase,'receipt');
  assert.equal(row(f).state,'outcome_uncertain');assert.equal(row(f).receipt_digest,null);
  // Stub Mailbox tests only forwarding: unchanged actual Mailbox claim/proof
  // rejection/reuse/readdress cases run separately; no proof is fabricated here.
 }finally{f.database.close();}
});
