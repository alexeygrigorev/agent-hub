import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MachineDO } from '../src/machine-do.ts';
import { HOST_INVENTORY_LOST_MS, LIVE_SNAPSHOT_TTL_MS, isLiveActiveSession } from '../src/session-liveness.ts';

const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
const header={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)};

function fixture(revokeFailures=0,projectFailures=0){
 const database=new DatabaseSync(':memory:'),events:Array<{path:string;body:any}>=[],sockets:any[]=[];
 const revocations:any[]=[];
 let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'machine-h1'},storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return{toArray:()=>rows,rowsRead,rowsWritten};
 }},async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(){return sockets;}};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
   const request=input instanceof Request?input:new Request(input,init);
   if(new URL(request.url).pathname==='/_worker/revoke'){
    revocations.push(await request.json());
    if(revokeFailures>0){revokeFailures--;return Response.json({error:'unavailable'},{status:503});}
    return Response.json({revoked:true});
   }
   return Response.json({authorized:true});
  }})},
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   const path=new URL(request.url).pathname;
   if(path==='/_internal/reaped'&&projectFailures>0){projectFailures--;return Response.json({error:'unavailable'},{status:503});}
   events.push({path,body:await request.json()});return Response.json({ok:true});
  }})},
 };
 let machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,headers:Record<string,string>={})=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{...header,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,get machine(){return machine;},restart:()=>machine=new MachineDO(ctx,env),call,events,sockets,revocations,clearCache:()=>((machine as any).db.cache.clear()),get alarm(){return alarm;}};
}

async function startClaimed(f:ReturnType<typeof fixture>,sessionId:string,nativeId:string|null){
 const beat=await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]});assert.equal(beat.status,200);
 const reservation=await f.call('/_internal/reserve','POST',{operation_id:'op-'+sessionId,dispatch_id:'dispatch-'+sessionId,attempt:0,
  session_id:sessionId,project_id:'project-test',intake_id:'intake-'+sessionId,machine_id:'h1',request_digest:'digest-'+sessionId,
  title:'test session',acceptance_criteria:['test'],account_id:'codex',launch:{workspace:'/tmp',engine:'codex',model:'gpt-6.1-sol',prompt:'test work',interactive:false}});
 assert.equal(reservation.status,201);const reserved=await reservation.json() as any;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-'+sessionId,session_id:sessionId,epoch:reserved.epoch})).status,201);
 f.database.prepare("UPDATE sessions SET state=?,native_id=?,updated_at=? WHERE id=?").run(nativeId?'running':'claimed',nativeId,Date.now(),sessionId);
 return reserved.epoch as number;
}

async function startOffered(f:ReturnType<typeof fixture>,sessionId:string){
 const beat=await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]});assert.equal(beat.status,200);
 const reservation=await f.call('/_internal/reserve','POST',{operation_id:'op-'+sessionId,dispatch_id:'dispatch-'+sessionId,attempt:0,
  session_id:sessionId,project_id:'project-test',intake_id:'intake-'+sessionId,machine_id:'h1',request_digest:'digest-'+sessionId,
  title:'test session',acceptance_criteria:['test'],account_id:'codex',launch:{workspace:'relay',engine:'codex',model:'gpt-6.1-sol',prompt:'test work',interactive:false,timeout_seconds:1800}});
 assert.equal(reservation.status,201);const reserved=await reservation.json() as any;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-'+sessionId,session_id:sessionId,epoch:reserved.epoch})).status,201);
 return reserved.epoch as number;
}

test('repo label ACK binds canonical cwd for public view and qualified exit without changing request',async()=>{
 const f=fixture(),session='repo-label-lifecycle',epoch=await startOffered(f,session),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'repo-label'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 const binding={version:1,requested_workspace:'relay',resolved_workspace:'/owned/enrolled/relay'};
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:session,epoch,workspace_binding:binding}));
 const native='11111111-2222-4333-8444-555555555555';
 assert.equal((await f.call('/api/sessions/'+session+'/started','POST',{operation_id:'op-'+session,epoch,native_id:native})).status,201);
 const admission=f.database.prepare('SELECT observed_at FROM launch_admissions WHERE operation_id=?').get('op-'+session) as any;
 const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
 const finish={operation_id:'op-'+session,epoch,native_id:native,exit_code:0,native_exit_confirmed:true,evidence:{
  source:'aplexer_status',native_identity:{id:native,tag:'task-hub2-'+session,workspace:binding.resolved_workspace},
  containment_empty:true,native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:admission.observed_at+1800_000,exceeded:false}}};
 for(const bad of [
  {...finish,epoch:epoch+1},
  {...finish,native_id:'99999999-2222-4333-8444-555555555555'},
  {...finish,evidence:{...finish.evidence,native_identity:{...finish.evidence.native_identity,tag:'foreign'}}},
  {...finish,evidence:{...finish.evidence,native_identity:{...finish.evidence.native_identity,workspace:'/foreign/relay'}}},
 ])assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',bad)).status,409);
 const result=await f.call('/api/sessions/'+session+'/finish','POST',finish);
 assert.equal(result.status,201,JSON.stringify(await result.json()));
 assert.equal(launch.workspace,'relay');
 assert.equal((f.database.prepare('SELECT request_digest FROM sessions WHERE id=?').get(session) as any).request_digest,'digest-'+session);
 const view=await (await f.call('/api/sessions/'+session,'GET',undefined,{'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})})).json() as any;
 assert.equal(view.session.workspace,binding.resolved_workspace);
 assert.equal(view.session.requested_workspace,'relay');
 assert.equal(f.events.find(event=>event.path==='/_internal/claim')?.body.workspace_binding.resolved_workspace,binding.resolved_workspace);
 assert.equal(f.events.find(event=>event.path==='/_internal/finish')?.body.workspace_binding.resolved_workspace,binding.resolved_workspace);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,200);
});

test('workspace ACK replay pins once, omission never clears, and wrong absolute/epoch cannot bind',async()=>{
 for(const resolved of ['/owned/enrolled/relay','C:\\Work\\relay']){
  const f=fixture(),session='binding-replay',epoch=await startOffered(f,session),frames:any[]=[];
  const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'binding-replay'}),send:(value:string)=>frames.push(JSON.parse(value))};
  f.sockets.push(socket);
  const ack={type:'offer_ack',session_id:session,epoch,workspace_binding:{version:1,requested_workspace:'relay',resolved_workspace:resolved}};
  await f.machine.webSocketMessage(socket as any,JSON.stringify({...ack,epoch:epoch+1}));
  assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'offered');
  await f.machine.webSocketMessage(socket as any,JSON.stringify(ack));
  await f.machine.webSocketMessage(socket as any,JSON.stringify(ack));
  await f.machine.webSocketMessage(socket as any,JSON.stringify({...ack,workspace_binding:undefined}));
  const prior=f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any;
  await f.machine.webSocketMessage(socket as any,JSON.stringify({...ack,workspace_binding:{...ack.workspace_binding,resolved_workspace:'/foreign/relay'}}));
  assert.equal(frames.at(-1).error,'workspace_binding_conflict');
  assert.equal((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json,prior.launch_json);
  const offers=await (await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})})).json() as any;
  assert.equal(offers.offers[0].launch.workspace,'relay');
  assert.equal(offers.offers[0].launch._hub_workspace_binding,undefined);
 }
 const f=fixture(),session='absolute-binding',epoch=await startClaimed(f,session,null),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'absolute'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 const prior=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:session,epoch,
  workspace_binding:{version:1,requested_workspace:'/tmp',resolved_workspace:'/foreign/tmp'}}));
 assert.equal(frames.at(-1).error,'invalid_workspace_binding');
 assert.deepEqual(f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session),prior);
});

test('legacy host label stays unbound and claimed binding upgrade uses a durable distinct receipt',async()=>{
 const f=fixture(),session='legacy-label-upgrade',epoch=await startOffered(f,session),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'upgrade'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 const ack={type:'offer_ack',session_id:session,epoch};
 await f.machine.webSocketMessage(socket as any,JSON.stringify(ack));
 const original=f.database.prepare('SELECT payload_json FROM machine_outbox WHERE id=?').get('claim:op-'+session) as any;
 assert.equal(JSON.parse(original.payload_json).workspace_binding,undefined);
 const workspace='/'+'\u754c'.repeat(1023);
 const receipt=legacyUnobserved(session,epoch);receipt.evidence.workspace=workspace;
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,409);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({...ack,workspace_binding:{version:1,requested_workspace:'relay',resolved_workspace:workspace}}));
 assert.equal((f.database.prepare('SELECT payload_json FROM machine_outbox WHERE id=?').get('claim:op-'+session) as any).payload_json,original.payload_json);
 assert(f.database.prepare('SELECT id FROM machine_outbox WHERE id=?').get('workspace-claim:op-'+session));
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,evidence:{...receipt.evidence,workspace:workspace+'x'}})).status,409);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,200);
 assert.equal(f.events.find(event=>event.path==='/_internal/reaped')?.body.workspace_binding.resolved_workspace,workspace);
 const old=fixture(),oldSession='unbound-absolute-unicode',oldEpoch=await startClaimed(old,oldSession,null);
 old.database.prepare("UPDATE sessions SET launch_json=json_set(launch_json,'$.workspace',?) WHERE id=?").run(workspace,oldSession);
 old.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+oldSession,oldSession,oldEpoch,Date.now());
 old.clearCache();
 const unbound=legacyUnobserved(oldSession,oldEpoch);unbound.evidence.workspace=workspace;
 assert.equal((await old.call('/api/sessions/'+oldSession+'/finish','POST',unbound)).status,409);
});

test('actual Python Unicode ACK frames fit the existing Machine UTF16 frame and carry all path bytes',async()=>{
 const {spawnSync}=await import('node:child_process'),{fileURLToPath}=await import('node:url');
 for(const workspace of ['/'+'\u754c'.repeat(1023),'C:\\'+'\u{1f642}'.repeat(510)]){
  const f=fixture(),session='unicode-frame',epoch=await startOffered(f,session),frames:any[]=[];
  const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'unicode'}),send:(value:string)=>frames.push(JSON.parse(value))};
  f.sockets.push(socket);
  const binding={version:1,requested_workspace:'relay',resolved_workspace:workspace};
  const child=spawnSync('python3',['-I','-c',
   'import sys,json;sys.path.insert(0,sys.argv[1]);from agent_hub_host.agent import offer_ack_frame;print(offer_ack_frame(sys.argv[2],int(sys.argv[3]),json.loads(sys.argv[4])))',
   fileURLToPath(new URL('../host',import.meta.url)),session,String(epoch),JSON.stringify(binding)],{encoding:'utf8',timeout:10000});
  assert.equal(child.status,0,child.stderr);
  const frame=child.stdout.trim();assert(frame.length<8192);assert(Buffer.byteLength(frame,'utf8')<8192);
  await f.machine.webSocketMessage(socket as any,frame);
  assert.equal(frames.at(-1).type,'ack');
  const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
  assert.deepEqual(launch._hub_workspace_binding,binding);
 }
});

test('running legacy label recovery stays reachable across ACK loss and accepts the same frozen exit',async()=>{
 const f=fixture(),session='legacy-running-upgrade',epoch=await startOffered(f,session),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'legacy-running'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 const ack={type:'offer_ack',session_id:session,epoch};
 await f.machine.webSocketMessage(socket as any,JSON.stringify(ack));
 const native='11111111-2222-4333-8444-555555555555';
 assert.equal((await f.call('/api/sessions/'+session+'/started','POST',{operation_id:'op-'+session,epoch,native_id:native})).status,201);
 const admitted=f.database.prepare('SELECT observed_at FROM launch_admissions WHERE operation_id=?').get('op-'+session) as any;
 const finish={operation_id:'op-'+session,epoch,native_id:native,exit_code:0,native_exit_confirmed:true,evidence:{source:'aplexer_status',
  native_identity:{id:native,tag:'task-hub2-'+session,workspace:'/owned/relay'},containment_empty:true,
  native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:admitted.observed_at+1800_000,exceeded:false}}};
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,409);
 const offers=async()=>((await (await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})})).json()) as any).offers;
 const recovery=(await offers())[0];
 assert.equal(recovery.claimed,true);assert.equal(recovery.recovery_native_id,native);
 assert.equal(recovery.launch.workspace,'relay');assert.equal(recovery.operation_id,'op-'+session);
 const binding={version:1,requested_workspace:'relay',resolved_workspace:'/owned/relay'};
 await f.machine.webSocketMessage(socket as any,JSON.stringify({...ack,workspace_binding:binding}));
 assert.deepEqual(frames.at(-1).workspace_binding,binding,'echo follows the durable binding transaction');
 const pinned=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session) as any;
 assert.equal(pinned.state,'running');assert.equal(pinned.native_id,native);assert.equal(pinned.epoch,epoch);
 assert.equal(JSON.parse(pinned.launch_json)._hub_workspace_repair_pending,true);
 // Simulate ACK loss/host crash before the local atomic requeue: the same
 // existing recovery wire remains available even though binding is now pinned.
 assert.equal((await offers())[0].recovery_native_id,native);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'resync'}));
 const frame=frames.filter(item=>item.type==='session.offer').at(-1);
 assert.equal(frame.offer.recovery_native_id,native);
 assert.equal(frame.offer.launch._hub_workspace_repair_pending,undefined);
 assert.equal(frame.offer.launch._hub_workspace_binding,undefined);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,201);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,200);
 assert.equal((await offers()).length,0);
 assert.equal(JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json)._hub_workspace_repair_pending,undefined);
});

test('historical label offers fill only spare capacity behind normal starts and exclude absolute paths',async()=>{
 const f=fixture();
 for(let i=0;i<12;i++){
  const session='historical-'+i;
  await startOffered(f,session);
  f.database.prepare("UPDATE sessions SET state='running',native_id=?,created_at=? WHERE id=?")
   .run('11111111-2222-4333-8444-'+String(i).padStart(12,'0'),i+1,session);
 }
 await startClaimed(f,'absolute-running','aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee');
 await startOffered(f,'fresh-one');await startOffered(f,'fresh-two');
 const response=await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})});
 const offers=(await response.json() as any).offers;
 assert.equal(offers.length,10);
 assert.deepEqual(offers.slice(0,2).map((offer:any)=>offer.session_id),['fresh-one','fresh-two']);
 assert(offers.slice(2).every((offer:any)=>offer.recovery_native_id&&offer.claimed===true));
 assert.equal(offers.some((offer:any)=>offer.session_id==='absolute-running'),false);
});

test('historical recovery rotates beyond ten pending rows and preserves progress across restart',async()=>{
 const f=fixture(),seen=new Set<string>(),frames:any[]=[];
 for(let i=0;i<21;i++){
  const session='rotation-'+String(i).padStart(2,'0');await startOffered(f,session);
  f.database.prepare("UPDATE sessions SET state='running',native_id=?,created_at=? WHERE id=?")
   .run('11111111-2222-4333-8444-'+String(i).padStart(12,'0'),i+1,session);
 }
 await startOffered(f,'fresh-rotation');
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'rotation'}),send:(value:string)=>frames.push(JSON.parse(value))};f.sockets.push(socket);
 const before=f.database.prepare('SELECT id,state,native_id,epoch,created_at,updated_at,request_digest FROM sessions ORDER BY id').all();
 for(let wave=0;wave<3;wave++){
  if(wave===1)f.restart();
  const response=await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})});
  const offers=(await response.json() as any).offers;
  assert.equal(offers.length,10);assert.equal(offers[0].session_id,'fresh-rotation');
  for(const offer of offers.slice(1)){
   seen.add(offer.session_id);
   assert.equal(offer.launch._hub_workspace_recovery_turn,undefined);
   await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:offer.session_id,epoch:offer.epoch,
    workspace_binding:{version:1,requested_workspace:'relay',resolved_workspace:'/owned/relay'}}));
  }
 }
 assert.equal(seen.size,21,'older live or unrecoverable pending rows must not starve an exited/deadletter row');
 assert.deepEqual(f.database.prepare('SELECT id,state,native_id,epoch,created_at,updated_at,request_digest FROM sessions ORDER BY id').all(),before);
});

test('recovery rotation normalizes malformed and exhausted turns without changing registry fields',async()=>{
 for(const exhausted of [false,true]){
  const f=fixture(),seen=new Set<string>();
  const invalid:any[]=['bad',-1,1.5,{},Number.MAX_SAFE_INTEGER+1];
  for(let i=0;i<11;i++){
   const session='counter-'+String(i).padStart(2,'0');await startOffered(f,session);
   const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
   launch._hub_workspace_recovery_turn=exhausted?Number.MAX_SAFE_INTEGER:invalid[i%invalid.length];
   f.database.prepare("UPDATE sessions SET state='running',native_id=?,created_at=?,launch_json=? WHERE id=?")
    .run('11111111-2222-4333-8444-'+String(i).padStart(12,'0'),i+1,JSON.stringify(launch),session);
  }
  await startOffered(f,'fresh-counter');
  const before=f.database.prepare('SELECT id,state,native_id,epoch,created_at,updated_at,request_digest FROM sessions ORDER BY id').all();
  for(let wave=0;wave<2;wave++){
   const offers=(await (await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})})).json() as any).offers;
   assert.equal(offers[0].session_id,'fresh-counter');assert.equal(offers.length,10);
   offers.slice(1).forEach((offer:any)=>seen.add(offer.session_id));
   if(wave===0)f.restart();
  }
  assert.equal(seen.size,11);
  for(const row of f.database.prepare("SELECT launch_json FROM sessions WHERE state='running'").all() as any[]){
   const turn=JSON.parse(row.launch_json)._hub_workspace_recovery_turn;
   assert(Number.isSafeInteger(turn)&&turn>=0&&turn<1000);
  }
  assert.deepEqual(f.database.prepare('SELECT id,state,native_id,epoch,created_at,updated_at,request_digest FROM sessions ORDER BY id').all(),before);
 }
});

test('rotation checkpoint failure is visible and never blocks normal offers; later retry advances',async()=>{
 const f=fixture(),seen=new Set<string>(),warnings:string[]=[];
 for(let i=0;i<11;i++){
  const session='checkpoint-'+String(i).padStart(2,'0');await startOffered(f,session);
  f.database.prepare("UPDATE sessions SET state='running',native_id=?,created_at=? WHERE id=?")
   .run('11111111-2222-4333-8444-'+String(i).padStart(12,'0'),i+1,session);
 }
 await startOffered(f,'fresh-checkpoint');
 f.database.exec("CREATE TRIGGER fail_rotation BEFORE UPDATE OF launch_json ON sessions BEGIN SELECT RAISE(ABORT,'synthetic checkpoint failure'); END");
 const warn=console.warn;console.warn=(message:any)=>warnings.push(String(message));
 try{
  const offers=(await (await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})})).json() as any).offers;
  assert.equal(offers[0].session_id,'fresh-checkpoint');assert.equal(offers.length,10);
 }finally{console.warn=warn;}
 assert.deepEqual(warnings,['[hub2] workspace recovery rotation checkpoint unavailable']);
 for(const row of f.database.prepare("SELECT launch_json FROM sessions WHERE state='running'").all() as any[])
  assert.equal(JSON.parse(row.launch_json)._hub_workspace_recovery_turn,undefined);
 f.database.exec('DROP TRIGGER fail_rotation');
 for(let wave=0;wave<2;wave++){
  const offers=(await (await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})})).json() as any).offers;
  assert.equal(offers[0].session_id,'fresh-checkpoint');
  offers.slice(1).forEach((offer:any)=>seen.add(offer.session_id));
 }
 assert.equal(seen.size,11);
});

test('malformed or overlong workspace ACK and oversized real frame never claim the offer',async()=>{
 const f=fixture(),session='binding-invalid',epoch=await startOffered(f,session),frames:any[]=[],closed:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'invalid'}),send:(value:string)=>frames.push(JSON.parse(value)),close:(...args:any[])=>closed.push(args)};
 f.sockets.push(socket);
 for(const binding of [
  {version:1,requested_workspace:'foreign',resolved_workspace:'/owned/relay'},
  {version:1,requested_workspace:'relay',resolved_workspace:'relative'},
  {version:1,requested_workspace:'relay',resolved_workspace:'/'+'x'.repeat(1024)},
  {version:1,requested_workspace:'relay',resolved_workspace:'/owned/relay',untrusted:true},
 ]){
  await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:session,epoch,workspace_binding:binding}));
  assert.equal(frames.at(-1).error,'invalid_workspace_binding');
 }
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:session,epoch,padding:'x'.repeat(8192)}));
 assert.equal(closed.at(-1)[0],1009);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'offered');
 assert.equal(f.database.prepare('SELECT * FROM launch_admissions').all().length,0);
 const injected=await f.call('/_internal/reserve','POST',{operation_id:'inject-op',dispatch_id:'inject-dispatch',attempt:0,
  session_id:'inject-session',project_id:'project-test',intake_id:'inject-intake',machine_id:'h1',request_digest:'inject-digest',
  title:'injected metadata',acceptance_criteria:['must refuse'],launch:{workspace:'relay',engine:'shell',model:'shell',prompt:'synthetic',interactive:false,
   _hub_workspace_binding:{version:1,requested_workspace:'relay',resolved_workspace:'/foreign'}}});
 assert.equal(injected.status,400);assert.equal((await injected.json() as any).error,'reserved_launch_field');
 assert.equal(f.database.prepare('SELECT id FROM sessions WHERE id=?').get('inject-session'),undefined);
 const reservedFlag=await f.call('/_internal/reserve','POST',{operation_id:'inject-op',dispatch_id:'inject-dispatch',attempt:0,
  session_id:'inject-session',project_id:'project-test',intake_id:'inject-intake',machine_id:'h1',request_digest:'inject-digest',
  title:'injected repair',acceptance_criteria:['must refuse'],launch:{workspace:'relay',engine:'shell',model:'shell',prompt:'synthetic',interactive:false,
   _hub_workspace_repair_pending:true}});
 assert.equal(reservedFlag.status,400);assert.equal((await reservedFlag.json() as any).error,'reserved_launch_field');
 const reservedTurn=await f.call('/_internal/reserve','POST',{operation_id:'inject-op',dispatch_id:'inject-dispatch',attempt:0,
  session_id:'inject-session',project_id:'project-test',intake_id:'inject-intake',machine_id:'h1',request_digest:'inject-digest',
  title:'injected turn',acceptance_criteria:['must refuse'],launch:{workspace:'relay',engine:'shell',model:'shell',prompt:'synthetic',interactive:false,
   _hub_workspace_recovery_turn:'malformed'}});
 assert.equal(reservedTurn.status,400);assert.equal((await reservedTurn.json() as any).error,'reserved_launch_field');
});

test('host launch refusal terminally fails an unclaimed offer with a visible reason',async()=>{
 const f=fixture(),sessionId='workspace-label-refused',epoch=await startOffered(f,sessionId),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,stage:'host_launch',reason:'launcher_capability_unavailable'}));
 const row=f.database.prepare('SELECT state,native_id FROM sessions WHERE id=?').get(sessionId) as any;
 assert.equal(row.state,'failed');assert.equal(row.native_id,null);
 assert.equal((f.database.prepare('SELECT reason FROM attention WHERE session_id=?').get(sessionId) as any).reason,'launcher_capability_unavailable');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');
 assert.equal(f.events.at(-1)?.body.epoch,epoch);assert.equal(f.events.at(-1)?.body.reason,'host_launch_refused');
 assert.equal(f.events.at(-1)?.body.failure_reason,'launcher_capability_unavailable');
 assert.equal(f.events.at(-1)?.body.launch_failed,true);
});

test('authentication refusal from an unclaimed offer routes to model fallback',async()=>{
 const f=fixture(),sessionId='auth-refused-offer';await startOffered(f,sessionId);
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:()=>{}};
 f.sockets.push(socket);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,reason:'provider_auth_failed'}));
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(sessionId) as any).state,'expired');
 assert.equal((f.database.prepare('SELECT state FROM machine_session_operations WHERE operation_id=?').get('op-'+sessionId) as any).state,'expired');
 assert.equal(f.events.at(-1)?.path,'/_internal/auth-fallback');
 assert.equal(f.events.at(-1)?.body.reason,'provider_auth_failed');
});

test('ambiguous workspace refusal routes to another-host placement fallback with a visible attention reason',async()=>{
 const f=fixture(),sessionId='workspace-label-ambiguous',epoch=await startOffered(f,sessionId);
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:()=>{}};
 f.sockets.push(socket);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,stage:'host_launch',reason:'workspace_label_ambiguous'}));
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(sessionId) as any).state,'expired');
 assert.equal((f.database.prepare('SELECT state FROM machine_session_operations WHERE operation_id=?').get('op-'+sessionId) as any).state,'expired');
 assert.equal((f.database.prepare('SELECT reason FROM attention WHERE session_id=?').get(sessionId) as any).reason,'workspace_label_ambiguous');
 assert.equal(f.events.at(-1)?.path,'/_internal/auth-fallback');
 assert.equal(f.events.at(-1)?.body.epoch,epoch);assert.equal(f.events.at(-1)?.body.reason,'workspace_label_ambiguous');
});

test('command and mailbox diagnostics never settle a matching launch attempt',async()=>{
 const f=fixture(),sessionId='command-error-on-claim',epoch=await startClaimed(f,sessionId,null);
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:()=>{}};
 f.sockets.push(socket);
 const before=f.events.length;
 for(const diagnostic of [
  {stage:'host_command',reason:'provider_auth_failed'},
  {message_id:'legacy-mail-id',reason:'workspace_label_unavailable'},
  {stage:'future_stage',reason:'host_operation_failed'},
 ]){
  await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
   operation_id:'op-'+sessionId,...diagnostic}));
  assert.deepEqual({...f.database.prepare('SELECT state,epoch,native_id FROM sessions WHERE id=?').get(sessionId)},
   {state:'claimed',epoch,native_id:null});
  assert.equal((f.database.prepare('SELECT reason FROM attention WHERE session_id=?').get(sessionId) as any).reason,diagnostic.reason);
  assert.equal(f.events.length,before);
 }
});

test('live reaper fails an expired claimed no-native launch without slot accounting',async()=>{
 const f=fixture(),epoch=await startClaimed(f,'orphan-session',null);
 f.database.prepare('UPDATE sessions SET created_at=?,updated_at=? WHERE id=?').run(Date.now()-3*60_000,Date.now()-3*60_000,'orphan-session');
 const dry=await f.call('/_internal/reap','POST',{dry_run:true});assert.equal(dry.status,200);
 const preview=await dry.json() as any;assert.equal(preview.reaped,0);assert.equal(preview.candidates[0].session_id,'orphan-session');
 assert.equal((f.database.prepare('SELECT state,epoch FROM sessions WHERE id=?').get('orphan-session') as any).state,'claimed');
 const live=await f.call('/_internal/reap','POST',{dry_run:false});const result=await live.json() as any;
 assert.equal(live.status,200);assert.equal(result.reaped,1);assert.equal(result.active_count,0);assert.equal(result.active_count_after,0);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('orphan-session') as any).state,'failed');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');assert.equal(f.events.at(-1)?.body.epoch,epoch);
 assert.equal(f.events.at(-1)?.body.reason,'launch_timeout_no_native');
 assert.equal(f.database.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='machine_active'").get(),undefined);
 assert.equal(f.database.prepare('PRAGMA table_info(machines)').all().some((row:any)=>row.name==='active_session_id'),false);
 assert.equal(f.database.prepare('PRAGMA table_info(machine_capabilities)').all().some((row:any)=>row.name==='resources_json'),false);
});

test('stale host snapshots still time out a claimed launch without a native identity visibly',async()=>{
 const f=fixture();await startClaimed(f,'orphan-on-stale-host',null);
 f.database.prepare('UPDATE sessions SET created_at=?,updated_at=? WHERE id=?').run(Date.now()-10*60_000,Date.now()-10*60_000,'orphan-on-stale-host');
 f.database.prepare('UPDATE machine_liveness SET observed_at=? WHERE singleton=1').run(Date.now()-LIVE_SNAPSHOT_TTL_MS-1);
 const response=await f.call('/_internal/reap','POST',{dry_run:false}),report=await response.json() as any;
 assert.equal(report.skipped,'host_liveness_stale');assert.equal(report.reaped,1);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('orphan-on-stale-host') as any).state,'failed');
 assert.equal((f.database.prepare('SELECT reason FROM attention WHERE session_id=?').get('orphan-on-stale-host') as any).reason,'launch_timeout_no_native');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');assert.equal(f.events.at(-1)?.body.reason,'launch_timeout_no_native');
 assert.equal((await f.call('/_internal/live-sessions')).status,200);
});

test('a claimed launch snapshot with no native id times out instead of queuing an unacknowledgeable stop',async()=>{
 const f=fixture();await startClaimed(f,'launching-without-native',null);
 const now=Date.now();assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[{
  session_id:'launching-without-native',native_id:null,phase:'launching',last_activity_at:now,exit_code:null,containment_empty:false,
 }]})).status,200);
 f.database.prepare("INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES('expired-capture','launching-without-native','h1',1,'capture-old','capture-digest','capture',NULL,'pending',NULL,?,?)").run(now-10_000,now-10_000);
 f.database.prepare("INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES('expired-stop','launching-without-native','h1',1,'stop-old','stop-digest','stop',NULL,'pending',NULL,?,?)").run(now-5_000,now-5_000);
 f.database.prepare('UPDATE sessions SET created_at=?,updated_at=? WHERE id=?').run(now-3*60_000,now-3*60_000,'launching-without-native');
 const response=await f.call('/_internal/reap','POST',{dry_run:false}),report=await response.json() as any;
 assert.equal(response.status,200);assert.equal(report.reaped,1);assert.equal(report.stop_requested,0);
 assert.equal(report.actions[0].action,'marked_failed');assert.equal(report.actions[0].reason,'launch_timeout_no_native');
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('launching-without-native') as any).state,'failed');
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get('expired-capture') as any).state,'failed');
 assert.equal((f.database.prepare("SELECT state FROM session_commands WHERE id=?").get('expired-stop') as any).state,'failed');
 assert.equal(JSON.parse((f.database.prepare('SELECT result_json FROM session_commands WHERE id=?').get('expired-stop') as any).result_json).error,'stop_unconfirmed');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');assert.equal(f.events.at(-1)?.body.launch_failed,true);
});

test('stop bypasses a pending fenced capture and settles it after terminal confirmation',async()=>{
 const f=fixture(),sessionId='claimed-stop',epoch=await startClaimed(f,sessionId,null),now=Date.now();
 const insert=f.database.prepare("INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,? ,NULL,'pending',NULL,?,?)");
 insert.run('pending-capture',sessionId,'h1',epoch,'capture-key','capture-digest','capture',now-1000,now-1000);
 const frames:any[]=[];f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:(value:string)=>frames.push(JSON.parse(value))});
 const operator={id:'human:operator',kind:'operator',scopes:['operator:admin']};
 const response=await f.call(`/api/sessions/${sessionId}/commands`,'POST',{kind:'stop',key:'operator-stop'},
  {'x-hub2-actor':JSON.stringify(operator)});
 assert.equal(response.status,201);
 assert.equal(frames.at(-1)?.type,'session.command');assert.equal(frames.at(-1)?.command?.kind,'stop');
 const stop=frames.at(-1).command;
 assert.equal((await f.call(`/api/sessions/${sessionId}/commands/${stop.id}/ack`,'POST',{epoch,result:{stopped:true,evidence:'dispatch_fenced'}})).status,200);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(sessionId) as any).state,'stopped');
 const replay=await f.call(`/api/sessions/${sessionId}/commands`,'POST',{kind:'stop',key:'operator-stop'},
  {'x-hub2-actor':JSON.stringify(operator)}),replayed=await replay.json() as any;
 assert.equal(replay.status,200);assert.equal(replayed.replayed,true);assert.equal(replayed.command.id,stop.id);assert.equal(frames.length,1);
 const capture=f.database.prepare('SELECT state,result_json FROM session_commands WHERE id=?').get('pending-capture') as any;
 assert.equal(capture.state,'failed');assert.equal(JSON.parse(capture.result_json).error,'session_ended_before_command_execution');
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(stop.id) as any).state,'acked');
 assert.equal(f.events.at(-1)?.path,'/_internal/stopped');assert.equal(f.events.at(-1)?.body.reason,'stop_confirmed');
});

test('a retry supersedes old commands and only sends stops fenced to the current epoch',async()=>{
 const f=fixture(),sessionId='stop-epoch',oldEpoch=await startClaimed(f,sessionId,null),oldNow=Date.now();
 const operator={id:'human:operator',kind:'operator',scopes:['operator:admin']};
 const oldStop=await f.call(`/api/sessions/${sessionId}/commands`,'POST',{kind:'stop',key:'old-stop'},
  {'x-hub2-actor':JSON.stringify(operator)});
 assert.equal(oldStop.status,201);const oldCommand=(await oldStop.json() as any).command;
 f.database.prepare('UPDATE session_commands SET created_at=?,updated_at=? WHERE id=?').run(oldNow-1000,oldNow-1000,oldCommand.id);
 f.database.prepare("UPDATE sessions SET state='expired' WHERE id=?").run(sessionId);
 f.clearCache();
 const prior=f.database.prepare('SELECT state,dispatch_id,request_digest,machine_id,attempt,epoch FROM sessions WHERE id=?').get(sessionId) as any;
 assert.equal(prior.state,'expired');assert.equal(prior.dispatch_id,'dispatch-'+sessionId);assert.equal(prior.request_digest,'digest-'+sessionId);
 assert.equal(prior.machine_id,'h1');assert.equal(prior.attempt+1,1);assert.equal(prior.epoch,oldEpoch);
 const retried=await f.call('/_internal/reserve','POST',{operation_id:'op-'+sessionId+':1',dispatch_id:'dispatch-'+sessionId,
  attempt:1,session_id:sessionId,project_id:'project-test',intake_id:'intake-'+sessionId,machine_id:'h1',
  request_digest:'digest-'+sessionId,title:'test session',acceptance_criteria:['test'],account_id:'codex',retry_epoch:oldEpoch,
  launch:{workspace:'/tmp',engine:'codex',model:'gpt-6.1-sol',prompt:'test work',interactive:false}});
 assert.equal(retried.status,200,JSON.stringify(await retried.clone().json()));const retryEpoch=(await retried.json() as any).epoch;
 assert(retryEpoch>oldEpoch);
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(oldCommand.id) as any).state,'failed');
 assert.equal(JSON.parse((f.database.prepare('SELECT result_json FROM session_commands WHERE id=?').get(oldCommand.id) as any).result_json).error,'session_epoch_superseded');
 f.database.prepare("UPDATE sessions SET state='claimed' WHERE id=?").run(sessionId);
 f.clearCache();
 const frames:any[]=[];f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:(value:string)=>frames.push(JSON.parse(value))});
 const reusedKey=await f.call(`/api/sessions/${sessionId}/commands`,'POST',{kind:'stop',key:'old-stop'},
  {'x-hub2-actor':JSON.stringify(operator)});
 assert.equal(reusedKey.status,409);assert.equal((await reusedKey.json() as any).error,'idempotency_conflict');
 const response=await f.call(`/api/sessions/${sessionId}/commands`,'POST',{kind:'stop',key:'current-epoch-stop'},
  {'x-hub2-actor':JSON.stringify(operator)});
 assert.equal(response.status,201);const created=(await response.json() as any).command;
 assert.equal(frames.at(-1)?.command?.id,created.id);assert.equal(frames.at(-1)?.command?.epoch,retryEpoch);
 assert.equal(frames.at(-1)?.command?.pre_native,true);
});

test('capture is refused before a claimed session has a native identity',async()=>{
 const f=fixture();await startClaimed(f,'claimed-capture',null);
 const operator={id:'human:operator',kind:'operator',scopes:['operator:admin']};
 const response=await f.call('/api/sessions/claimed-capture/commands','POST',{kind:'capture',key:'capture-too-early'},
  {'x-hub2-actor':JSON.stringify(operator)});
 assert.equal(response.status,409);assert.equal((await response.json() as any).error,'session_not_running');
 assert.equal((f.database.prepare('SELECT COUNT(*) AS count FROM session_commands WHERE session_id=?').get('claimed-capture') as any).count,0);
});

test('stop immediately settles unclaimed offered and expired sessions without a host command',async()=>{
 const operator={id:'human:operator',kind:'operator',scopes:['operator:admin']};
 for(const state of ['offered','expired']){
  const f=fixture(),sessionId=`${state}-stop`,epoch=await startClaimed(f,sessionId,null);
  f.database.prepare('UPDATE sessions SET state=?,native_id=NULL,offer_expires_at=NULL WHERE id=?').run(state,sessionId);
  f.clearCache();
  const path=`/api/sessions/${sessionId}/commands`,headers={'x-hub2-actor':JSON.stringify(operator)};
  const response=await f.call(path,'POST',{kind:'stop',key:'stop-before-native'},headers),data=await response.json() as any;
  assert.equal(response.status,201,JSON.stringify(data));assert.equal(data.command.kind,'stop');
  assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(sessionId) as any).state,'stopped');
  const command=f.database.prepare('SELECT state,result_json,epoch FROM session_commands WHERE session_id=?').get(sessionId) as any;
  assert.equal(command.state,'acked');assert.equal(command.epoch,epoch);assert.equal(JSON.parse(command.result_json).stopped,true);
  assert.equal((f.database.prepare('SELECT phase FROM native_sessions WHERE session_id=?').get(sessionId) as any).phase,'missing');
  assert.equal(f.events.at(-1)?.path,'/_internal/stopped');assert.equal(f.events.at(-1)?.body.reason,'stop_confirmed');
  assert.equal(f.events.filter(event=>event.path==='/_internal/stopped').length,1);
  const replay=await f.call(path,'POST',{kind:'stop',key:'stop-before-native'},headers);
  assert.equal(replay.status,200);assert.equal((await replay.json() as any).replayed,true);
  assert.equal((f.database.prepare('SELECT COUNT(*) AS count FROM session_commands WHERE session_id=?').get(sessionId) as any).count,1);
 }
});

test('a bounded fresh snapshot does not close an omitted running session',async()=>{
 const f=fixture();await startClaimed(f,'missing-native-session','native-missing');
 const response=await f.call('/_internal/reap','POST',{dry_run:false}),report=await response.json() as any;
 assert.equal(response.status,200);assert.equal(report.reaped,0);assert.equal(report.stop_requested,0,JSON.stringify(report));
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('missing-native-session') as any).state,'running');
 assert.equal((f.database.prepare("SELECT COUNT(*) AS count FROM session_commands WHERE session_id=? AND kind='stop'").get('missing-native-session') as any).count,0);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
});

test('a running attempt becomes lost only after five minutes without host inventory',async()=>{
 const f=fixture(1),session='lost-running',nativeId='native-lost',epoch=await startClaimed(f,session,nativeId);
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const started=Date.now()-HOST_INVENTORY_LOST_MS-100;
 const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
 f.database.prepare('UPDATE sessions SET updated_at=?,launch_json=? WHERE id=?').run(started,JSON.stringify({...launch,timeout_seconds:1800}),session);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,started);
 f.database.prepare('UPDATE machine_liveness SET observed_at=? WHERE singleton=1').run(started+15_000);
 f.clearCache();
 const before=await f.call('/_internal/reap','POST',{dry_run:false});
 assert.equal((await before.json() as any).lost,0);
 await f.machine.alarm();
 assert.equal(f.alarm,started+15_000+HOST_INVENTORY_LOST_MS);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'running');

 f.database.prepare('UPDATE machine_liveness SET observed_at=? WHERE singleton=1').run(started);
 f.clearCache();
 const pending=await f.call('/api/sessions/'+session+'/commands','POST',{key:'stop-before-loss',kind:'stop'},actor);
 assert.equal(pending.status,201);
 const dry=await f.call('/_internal/reap','POST',{dry_run:true});
 assert((await dry.json() as any).candidates.some((entry:any)=>entry.session_id===session&&entry.reason==='host_inventory_stale'));
 const result=await f.call('/_internal/reap','POST',{dry_run:false});
 assert.equal((await result.json() as any).lost,1);
 const view=await f.call('/api/sessions/'+session,'GET',undefined,actor);
 assert.equal((await view.json() as any).session.state,'lost');
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
 const replay=await f.call('/api/sessions/'+session+'/started','POST',{operation_id:'op-'+session,epoch,native_id:nativeId});
 assert.deepEqual(await replay.json(),{ok:true,duplicate:true,state:'lost'});
 const commands=await f.call('/api/sessions/'+session+'/commands','GET',undefined,actor);
 assert.equal((await commands.json() as any).commands[0].ack.confirmed_exit,false);
 assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[{
  session_id:session,native_id:nativeId,phase:'running',last_activity_at:Date.now(),exit_code:null,containment_empty:false,
 }]})).status,200);
 const snapshot=await f.call('/_internal/live-sessions');
 const nativeView=(await snapshot.json() as any).sessions[0];
 assert.equal(nativeView.phase,'running');
 assert.equal(nativeView.managed_state,'lost');
 assert.equal(isLiveActiveSession(nativeView),true);
 const frames:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),
  send:(value:string)=>frames.push(JSON.parse(value))});
 const stopUrl='/api/sessions/'+session+'/commands';
 const stopping=await f.call(stopUrl,'POST',{key:'stop-after-reconnect',kind:'stop'},actor);
 assert.equal(stopping.status,201);
 const {command}=await stopping.json() as any;
 assert(frames.some(frame=>frame.type==='session.command'&&frame.command.id===command.id));
 const stopAck=await f.call(stopUrl+'/'+command.id+'/ack','POST',{epoch,result:{stopped:true,evidence:'native_exit',native_id:nativeId}});
 assert.equal(stopAck.status,200);
 const confirmed=await f.call('/api/sessions/'+session,'GET',undefined,actor);
 assert.deepEqual((({state,stop_confirmed})=>({state,stop_confirmed}))((await confirmed.json() as any).session),
  {state:'lost',stop_confirmed:true});
 assert.equal(f.events.some(event=>event.path==='/_internal/stopped'),false);
 const finish={operation_id:'op-'+session,epoch,native_id:nativeId,exit_code:0,native_exit_confirmed:true,evidence:{
  source:'aplexer_retirement',containment_empty:true,native_identity:{id:nativeId,tag:'task-hub2-'+session,workspace:launch.workspace},
  native_deadline:{deadline_ms:started+1800_000,exceeded:false},native_exit:{code:0,exited_at_ms:Date.now()},
 }};
 f.database.prepare("UPDATE machine_outbox SET state='dead_letter',attempt=10 WHERE kind='lost'").run();
 f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,200);
 const revived=f.database.prepare("SELECT state,attempt FROM machine_outbox WHERE kind='lost'").get();
 assert.deepEqual({...revived},{state:'pending',attempt:0});
 assert.equal((await (await f.call('/api/sessions/'+session+'/finish','POST',finish)).json() as any).duplicate,true);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'lost');
 assert.equal(f.events.some(event=>event.path==='/_internal/finish'),false);
 assert.equal(f.revocations.length,1);
 f.database.prepare("UPDATE machine_outbox SET next_action_at=? WHERE kind='lost'").run(Date.now()-1);
 f.clearCache();
 await f.machine.alarm();
 assert.equal(f.revocations.length,2);
 assert.equal(f.revocations[1].reason,'host_inventory_stale');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');
 assert.equal(f.events.at(-1)?.body.lost,true);
 assert.equal(f.events.at(-1)?.body.stopped,undefined);
 assert.equal(f.events.at(-1)?.body.epoch,epoch);
});

test('dead native pane receives a stop command and confirmed exit releases state immediately',async()=>{
 const f=fixture(),epoch=await startClaimed(f,'dead-session','native-dead'),now=Date.now();
 assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[{
  session_id:'dead-session',native_id:'native-dead',phase:'dead',last_activity_at:now-1000,exit_code:1,containment_empty:false,
 }]})).status,200);
 const response=await f.call('/_internal/reap','POST',{dry_run:false});const report=await response.json() as any;
 assert.equal(report.stop_requested,1,JSON.stringify(report));assert.equal(report.reaped,0);
 const command=f.database.prepare("SELECT id,epoch FROM session_commands WHERE session_id=? AND kind='stop' AND state='pending'").get('dead-session') as any;
 assert(command);assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('dead-session') as any).state,'running');
 const ack=await f.call(`/api/sessions/dead-session/commands/${command.id}/ack`,'POST',{epoch, result:{stopped:true}});
 assert.equal(ack.status,200);assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('dead-session') as any).state,'stopped');
 assert.equal(f.events.at(-1)?.path,'/_internal/stopped');assert.equal(f.events.at(-1)?.body.reason,'stop_confirmed');
});


test('an unclaimed offer has one terminal acceptance deadline and never requeues',async()=>{
 const f=fixture(),session='silent-host';await startOffered(f,session);
 const accepted=Date.now()-120_001;
 f.database.prepare('UPDATE sessions SET created_at=?,offer_expires_at=? WHERE id=?').run(accepted,accepted+120_000,session);
 await f.machine.alarm();
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'failed');
 assert.equal(f.events.at(-1)?.body.reason,'host_no_response');
 assert.ok(!f.events.some(event=>event.path==='/_internal/requeue'));
});

test('claim timestamps cannot extend the original start deadline',async()=>{
 const f=fixture(),session='late-claim';await startClaimed(f,session,null);
 f.database.prepare('UPDATE sessions SET created_at=?,updated_at=? WHERE id=?').run(Date.now()-120_001,Date.now(),session);
 await f.machine.alarm();
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'failed');
 assert.equal(f.events.at(-1)?.body.reason,'launch_timeout_no_native');
});


test('pre-native stop accepts only qualified evidence and keeps an unconfirmed session live',async()=>{
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const cases=[
  {result:{stopped:true},state:'failed',sessionState:'claimed'},
  {result:{stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed',stage:'host_stop'},state:'failed',sessionState:'claimed'},
  {result:{stopped:true,evidence:'dispatch_fenced'},state:'acked',sessionState:'stopped'},
  {result:{stopped:true,evidence:'native_exit',native_id:'native-qualified'},state:'acked',sessionState:'stopped'},
 ];
 for (const [index,expected] of cases.entries()) {
  const f=fixture(),session='stop-proof-'+index,epoch=await startClaimed(f,session,null);
  const base='/api/sessions/'+session+'/commands';
  const queued=await f.call(base,'POST',{key:'stop-proof',kind:'stop'},actor);
  assert.equal(queued.status,201);
  const {command}=await queued.json() as any;
  const ackPath=base+'/'+command.id+'/ack';
  const body={epoch,result:expected.result};
  assert.equal((await f.call(ackPath,'POST',body)).status,200);
  assert.deepEqual(await (await f.call(ackPath,'POST',body)).json(),{acknowledged:true,duplicate:true});
  const list=await f.call(base,'GET',undefined,actor);
  const {commands}=await list.json() as any;
  assert.equal(commands[0].state,expected.state);
  assert.equal(commands[0].ack.confirmed_exit,expected.state==='acked');
  if (expected.state==='failed') assert.equal(commands[0].ack.reason,'stop_unconfirmed');
  assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,expected.sessionState);
  assert.equal(f.events.some(event=>event.body?.reason==='stop_confirmed'),expected.state==='acked');
 }
});

test('a legacy pre-native acknowledgment stays unconfirmed after the native ID binds',async()=>{
 const f=fixture(),session='legacy-stop-late-bind',epoch=await startClaimed(f,session,null);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,Date.now());
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const base='/api/sessions/'+session;
 const queued=await f.call(base+'/commands','POST',{key:'stop-late-bind',kind:'stop'},actor);
 assert.equal(queued.status,201);
 const {command}=await queued.json() as any;
 const started=await f.call(base+'/started','POST',{operation_id:'op-'+session,epoch,native_id:'native-after-delivery'});
 assert.equal(started.status,201);
 assert.equal((await f.call(base+'/commands/'+command.id+'/ack','POST',{epoch,result:{stopped:true}})).status,200);
 const listed=await f.call(base+'/commands','GET',undefined,actor);
 const {commands}=await listed.json() as any;
 assert.equal(commands[0].state,'failed');
 assert.equal(commands[0].ack.confirmed_exit,false);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'running');
});

test('a fresh snapshot missing the native process does not confirm a pending stop',async()=>{
 const f=fixture(),session='stop-absent-from-snapshot';await startClaimed(f,session,'native-missing');
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const base='/api/sessions/'+session;
 const queued=await f.call(base+'/commands','POST',{key:'stop-snapshot-omission',kind:'stop'},actor);
 assert.equal(queued.status,201);
 const {command}=await queued.json() as any;
 assert.equal((await f.call('/_internal/reap','POST',{dry_run:false})).status,200);
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(command.id) as any).state,'pending');
 f.database.prepare('UPDATE session_commands SET created_at=? WHERE id=?').run(Date.now()-60_001,command.id);
 await f.machine.alarm();
 const listed=await f.call(base+'/commands','GET',undefined,actor);
 const {commands}=await listed.json() as any;
 assert.equal(commands[0].state,'failed');
 assert.equal(commands[0].ack.reason,'stop_unconfirmed');
 assert.equal(commands[0].ack.confirmed_exit,false);
 assert.equal(f.database.prepare('SELECT phase FROM native_sessions WHERE session_id=?').get(session),undefined);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'running');
 assert.equal((await f.call('/_internal/reap','POST',{dry_run:false})).status,200);
 assert.equal(f.events.some(event=>event.path==='/_internal/stopped'||event.path==='/_internal/reaped'),false);
 const retry=await f.call(base+'/commands','POST',{key:'retry-snapshot-omission',kind:'stop'},actor);
 assert.equal(retry.status,201);
});

test('a formerly admitted expired attempt receives a host stop instead of immediate confirmation',async()=>{
 const f=fixture(),session='expired-admitted',epoch=await startClaimed(f,session,null);
 f.database.prepare("UPDATE sessions SET state='expired' WHERE id=?").run(session);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,Date.now());
 const messages:any[]=[];
 f.sockets.push({readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),
  send:(value:string)=>messages.push(JSON.parse(value))});
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const queued=await f.call('/api/sessions/'+session+'/commands','POST',{key:'stop-expired',kind:'stop'},actor);
 assert.equal(queued.status,201);
 const {command}=await queued.json() as any;
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(command.id) as any).state,'pending');
 assert(messages.some(message=>message.type==='session.command'&&message.command.pre_native===true&&message.command.id===command.id));
 const ack=await f.call('/api/sessions/'+session+'/commands/'+command.id+'/ack','POST',{epoch,result:{stopped:true,evidence:'dispatch_fenced'}});
 assert.equal(ack.status,200);
 assert.deepEqual(f.revocations.map(row=>[row.operation_id,row.epoch,row.reason]),
   [['op-'+session,epoch,'native_dispatch_not_started']]);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'stopped');
});

test('an authenticated no-dispatch receipt fails the exact admitted launch without native exit evidence',async()=>{
 const f=fixture(),session='no-native-dispatch',epoch=await startClaimed(f,session,null);
 const path='/api/sessions/'+session+'/finish';
 const receipt={operation_id:'op-'+session,epoch,native_id:null,launch_failed:true,no_dispatch:true};
 assert.equal((await f.call(path,'POST',receipt)).status,409);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now());
 f.clearCache();
 assert.equal((await f.call(path,'POST',{...receipt,epoch:epoch+1})).status,409);
 assert.equal((await f.call(path,'POST',{...receipt,native_exit_confirmed:true})).status,400);
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 const row=f.database.prepare('SELECT state,native_id FROM sessions WHERE id=?').get(session);
 assert.deepEqual({...row},{state:'failed',native_id:null});
 const events=f.events.filter(event=>event.path==='/_internal/reaped');
 assert.equal(events.length,1);
 assert.equal(events[0].body.failure_reason,'native_dispatch_not_started');
 assert.equal(events[0].body.no_dispatch,true);
 assert.equal(events[0].body.native_exit_confirmed,undefined);
});

test('a delayed no-dispatch receipt revokes a superseded admitted attempt without touching its successor',async()=>{
 const f=fixture(),session='no-dispatch-superseded',epoch=await startClaimed(f,session,null);
 const oldOperation='op-'+session;
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(oldOperation,session,epoch,Date.now());
 f.database.prepare("UPDATE machine_session_operations SET state='expired' WHERE operation_id=?").run(oldOperation);
 f.database.prepare("UPDATE sessions SET operation_id='successor',epoch=?,state='running',native_id='successor-native' WHERE id=?").run(epoch+1,session);
 f.clearCache();
 const path='/api/sessions/'+session+'/finish';
 const receipt={operation_id:oldOperation,epoch,native_id:null,launch_failed:true,no_dispatch:true};
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 assert.deepEqual(f.revocations.map(row=>[row.operation_id,row.epoch]),[[oldOperation,epoch]]);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
 assert.deepEqual({...f.database.prepare('SELECT state,native_id FROM sessions WHERE id=?').get(session)},
   {state:'running',native_id:'successor-native'});
});

test('a pending stop ends as stop_unconfirmed after sixty seconds',async()=>{
 const f=fixture(),session='stop-deadline';await startClaimed(f,session,'native-stop');
 const actor={'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})};
 const response=await f.call('/api/sessions/'+session+'/commands','POST',{key:'stop-deadline',kind:'stop'},actor);
 assert.equal(response.status,201);const receipt=await response.json() as any;
 f.database.prepare('UPDATE session_commands SET created_at=? WHERE id=?').run(Date.now()-60_001,receipt.command.id);
 await f.machine.alarm();
 const row=f.database.prepare('SELECT state,result_json FROM session_commands WHERE session_id=?').get(session) as any;
 assert.equal(row.state,'failed');assert.equal(JSON.parse(row.result_json).error,'stop_unconfirmed');
});


test('claim records admission so native started needs no separate admission request',async()=>{
 const f=fixture(),session='claim-is-admission',epoch=await startOffered(f,session);
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:()=>{}};
 f.sockets.push(socket);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'offer_ack',session_id:session,epoch}));
 const result=await f.call('/api/sessions/'+session+'/started','POST',{operation_id:'op-'+session,epoch,native_id:'native-new'});
 assert.equal(result.status,201,await result.clone().text());
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'running');
});

test('session projections carry host workspace route and timestamp columns for the CLI',async()=>{
 const f=fixture(),session='projection';await startClaimed(f,session,'native-projection');
 const response=await f.call('/api/sessions/'+session,'GET',undefined,{'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})});
 assert.equal(response.status,200);const view=(await response.json() as any).session;
 assert.equal(view.start_deadline_at,null,'legacy row must not fabricate a stored deadline');
 const storedDeadline=Date.now()+75_000;
 f.database.prepare("UPDATE sessions SET launch_json=json_set(launch_json,'$.start_deadline_at',?) WHERE id=?").run(storedDeadline,session);
 f.clearCache();
 const stored=await f.call('/api/sessions/'+session,'GET',undefined,{'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})});
 assert.equal((await stored.json() as any).session.start_deadline_at,storedDeadline);
 assert.equal(view.host,'h1');assert.equal(view.machine,'h1');assert.equal(view.workspace,'/tmp');
 assert.equal(view.account_id,'codex');assert.equal(view.account,'codex');assert.equal(view.mode,'headless');
 assert.equal(view.managed,true);assert.ok(Number.isFinite(Date.parse(view.created_at)));assert.ok(Number.isFinite(Date.parse(view.updated_at)));
});


test('explicit screen capture survives the durable command queue and remains idempotency bound',async()=>{
 const f=fixture();await startClaimed(f,'screen-session','11111111-1111-4111-8111-111111111111');
 const admin={'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})};
 const receipt=await f.call('/api/sessions/screen-session/capture','POST',{key:'screen-key',mode:'screen'},admin);
 assert.equal(receipt.status,201);const value=await receipt.json() as any;
 assert.equal(value.command.mode,'screen');
 const row=f.database.prepare('SELECT * FROM session_commands WHERE id=?').get(value.command_id) as any;
 assert.equal(row.capture_mode,'screen');assert.equal((f.machine as any).commandView(row).mode,'screen');
 assert.equal((await f.call('/api/sessions/screen-session/capture','POST',{key:'screen-key',mode:'screen'},admin)).status,200);
 assert.equal((await f.call('/api/sessions/screen-session/capture','POST',{key:'screen-key'},admin)).status,409);
 assert.equal((await f.call('/api/sessions/screen-session/capture','POST',{key:'bad-mode',mode:'file'},admin)).status,400);
 const tail=await f.call('/api/sessions/screen-session/capture','POST',{key:'tail-key'},admin);
 assert.equal(tail.status,201);assert.equal((await tail.json() as any).command.mode,undefined);
});

test('latest schema preserves pending version-six commands and adds capture mode',async()=>{
 const f=fixture();await startClaimed(f,'old-command','22222222-2222-4222-8222-222222222222');
 const admin={'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})};
 const response=await f.call('/api/sessions/old-command/capture','POST',{key:'before-upgrade'},admin);
 const created=await response.json() as any;
 f.database.exec('DROP TABLE native_commands; DROP TABLE native_inventory_meta; DROP TABLE native_inventory_rows; DROP TABLE native_inventory_upload; DROP TABLE native_inventory_chunks; ALTER TABLE session_commands DROP COLUMN capture_mode; UPDATE hub2_schema SET version=6');
 const prior=f.machine as any;
 const upgraded=new MachineDO(prior.ctx,prior.env);
 const listed=await upgraded.fetch(new Request('https://machine.test/api/sessions/old-command/commands',{headers:{...header,...admin}}));
 assert.equal(listed.status,200);assert.equal((await listed.json() as any).commands[0].id,created.command_id);
 const row=f.database.prepare('SELECT * FROM session_commands WHERE id=?').get(created.command_id) as any;
 assert.equal(row.capture_mode,null);assert.equal(row.state,'pending');
 assert.equal((f.database.prepare('SELECT version FROM hub2_schema').get() as any).version,9);
});


test('qualified exit after an admitted launch timeout records failure evidence and revokes before forwarding',async()=>{
 const f=fixture(1),session='late-admitted-exit',epoch=await startClaimed(f,session,null),started=Date.now()-120_000;
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,started);
 f.database.prepare("UPDATE sessions SET updated_at=?,created_at=? WHERE id=?").run(started,started,session);
 f.clearCache();
 await f.call('/_internal/reap','POST',{dry_run:false});
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'failed');
 const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
 const timeout=1800;
 f.database.prepare('UPDATE sessions SET launch_json=? WHERE id=?').run(JSON.stringify({...launch,timeout_seconds:timeout}),session);
 f.clearCache();
 const native='33333333-3333-4333-8333-333333333333';
 const evidence={source:'aplexer_status',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+session,workspace:'/tmp'},
   native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:started+timeout*1000,exceeded:false}};
 const receipt={operation_id:'op-'+session,epoch,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence};
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,evidence:{...evidence,containment_empty:false}})).status,409);
 const finishResponse=await f.call('/api/sessions/'+session+'/finish','POST',receipt);
 assert.equal(finishResponse.status,202,await finishResponse.text());
 assert.equal(f.events.filter(e=>e.path==='/_internal/finish').length,0);
 f.database.prepare("UPDATE machine_outbox SET next_action_at=? WHERE state='pending'").run(Date.now()-1);
 f.clearCache();
 await f.machine.alarm();
 assert.equal(f.events.filter(e=>e.path==='/_internal/finish').length,1);
 const row=f.database.prepare('SELECT state,native_id,exit_code FROM sessions WHERE id=?').get(session);
 assert.deepEqual({...row},{state:'failed',native_id:native,exit_code:0});
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,200);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,exit_code:1})).status,409);
});


test('a pending timeout notification cannot delay exact worker revocation after qualified native exit',async()=>{
 const f=fixture(0,3),session='late-exit-project-offline',epoch=await startClaimed(f,session,null),started=Date.now()-120_000;
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,started);
 const launch=JSON.parse((f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any).launch_json);
 f.database.prepare('UPDATE sessions SET updated_at=?,created_at=?,launch_json=? WHERE id=?')
   .run(started,started,JSON.stringify({...launch,timeout_seconds:1800}),session);
 f.clearCache();
 await f.call('/_internal/reap','POST',{dry_run:false});
 const native='33333333-3333-4333-8333-333333333333';
 const evidence={source:'aplexer_status',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+session,workspace:'/tmp'},
   native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:started+1800_000,exceeded:false}};
 const response=await f.call('/api/sessions/'+session+'/finish','POST',{
   operation_id:'op-'+session,epoch,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence});
 assert.equal(response.status,202,await response.text());
 assert.deepEqual(f.revocations.map(row=>[row.operation_id,row.epoch]),[['op-'+session,epoch]]);
 assert.equal(f.events.some(event=>event.path==='/_internal/finish'),false);
 const notification=f.database.prepare('SELECT state FROM machine_outbox WHERE id=?').get('finish:op-'+session) as any;
 assert.equal(notification.state,'pending');
});

function legacyUnobserved(session:string,epoch:number){
 const now=Date.now();
 return {operation_id:'op-'+session,epoch,native_id:null,launch_failed:true,failure_kind:'legacy_census_absent',
  native_outcome:'unconfirmed',evidence:{source:'aplexer_all_registry_census',started_at_ms:now-10,observed_at_ms:now,
   row_count:77,sha256:'a'.repeat(64),complete:true,match_count:0,workspace:'/tmp',
   tag:'task-hub2-'+session,dispatch_provenance:'legacy_unknown'}};
}

test('legacy census absence fails a bound attempt as unconfirmed and revokes before notifying Project',async()=>{
 const f=fixture(1),session='legacy-unobserved',epoch=await startClaimed(f,session,null),receipt=legacyUnobserved(session,epoch);
 receipt.evidence.workspace='/deleted/worktree';
 f.database.prepare("UPDATE sessions SET launch_json=json_set(launch_json,'$.workspace',?) WHERE id=?").run(receipt.evidence.workspace,session);
 f.clearCache();
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now()-100);
 const path='/api/sessions/'+session+'/finish';
 assert.equal((await f.call(path,'POST',receipt)).status,202);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
 const row=f.database.prepare('SELECT state,native_id,evidence_json FROM sessions WHERE id=?').get(session) as any;
 assert.equal(row.state,'failed');assert.equal(row.native_id,null);
 assert.equal(JSON.parse(row.evidence_json).native_outcome,'unconfirmed');
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 const event=f.events.find(event=>event.path==='/_internal/reaped')!;
 assert.equal(event.body.reason,'legacy_launch_unobserved');
 assert.equal(event.body.native_outcome,'unconfirmed');
 assert.equal(event.body.evidence.workspace,'/deleted/worktree');
 assert.equal(event.body.no_dispatch,undefined);assert.equal(event.body.native_exit_confirmed,undefined);
 assert.equal(f.revocations.at(-1).operation_id,receipt.operation_id);assert.equal(f.revocations.at(-1).epoch,epoch);
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 assert.equal(f.events.filter(event=>event.path==='/_internal/reaped').length,1);
 assert.equal((await f.call(path,'POST',{...receipt,evidence:{...receipt.evidence,sha256:'b'.repeat(64)}})).status,409);
 assert.equal(f.database.prepare('SELECT * FROM native_sessions WHERE session_id=?').get(session),undefined);
});

test('legacy absence receipt rejects invented exit, incomplete census and identity mismatches',async()=>{
 const f=fixture(),session='legacy-invalid',epoch=await startClaimed(f,session,null),receipt=legacyUnobserved(session,epoch);
 const path='/api/sessions/'+session+'/finish';
 assert.equal((await f.call(path,'POST',receipt)).status,409);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now()-100);
 f.clearCache();
 for(const invalid of [
  {...receipt,native_exit_confirmed:true},{...receipt,no_dispatch:true},{...receipt,exit_code:0},
  {...receipt,evidence:{...receipt.evidence,complete:false}},
  {...receipt,evidence:{...receipt.evidence,row_count:5001}},
  {...receipt,evidence:{...receipt.evidence,match_count:1}},
  {...receipt,evidence:{...receipt.evidence,tag:'other-session'}},
  {...receipt,evidence:{...receipt.evidence,secret:'must not persist'}},
 ]){
  assert.equal((await f.call(path,'POST',invalid)).status,400);
 }
 assert.equal((await f.call(path,'POST',{...receipt,epoch:epoch+1})).status,409);
 const before=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session);
 const operations=f.database.prepare('SELECT * FROM machine_session_operations WHERE session_id=?').all(session);
 const outboxes=f.database.prepare('SELECT * FROM machine_outbox').all();
 assert.equal((await f.call(path,'POST',{...receipt,evidence:{...receipt.evidence,workspace:'/wrong-worktree'}})).status,409);
 assert.deepEqual(f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session),before);
 assert.deepEqual(f.database.prepare('SELECT * FROM machine_session_operations WHERE session_id=?').all(session),operations);
 assert.deepEqual(f.database.prepare('SELECT * FROM machine_outbox').all(),outboxes);
 assert.equal(f.revocations.length,0);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
 f.database.prepare("UPDATE sessions SET launch_json='{}' WHERE id=?").run(session);
 f.clearCache();
 assert.equal((await f.call(path,'POST',receipt)).status,409);
 assert.equal(f.revocations.length,0);
 f.database.prepare("UPDATE sessions SET launch_json=?,state='running',native_id='bound-native' WHERE id=?").run((before as any).launch_json,session);
 f.clearCache();
 assert.equal((await f.call(path,'POST',receipt)).status,409);
 assert.equal(f.revocations.length,0);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
});

test('a first legacy receipt for a superseded attempt refuses unavailable launch metadata without effects',async()=>{
 const f=fixture(),session='legacy-superseded',epoch=await startClaimed(f,session,null),receipt=legacyUnobserved(session,epoch);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now()-100);
 f.database.prepare("UPDATE machine_session_operations SET state='expired' WHERE operation_id=?").run(receipt.operation_id);
 f.database.prepare("UPDATE sessions SET operation_id='successor',epoch=?,state='running',native_id='successor-native' WHERE id=?").run(epoch+1,session);
 f.clearCache();
 const before=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session);
 const outboxes=f.database.prepare('SELECT * FROM machine_outbox').all();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,409);
 assert.deepEqual(f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session),before);
 assert.deepEqual(f.database.prepare('SELECT * FROM machine_outbox').all(),outboxes);
 assert.equal(f.revocations.length,0);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
});

test('an already accepted legacy receipt remains replayable after its attempt is superseded',async()=>{
 const f=fixture(),session='legacy-replayed-successor',epoch=await startClaimed(f,session,null),receipt=legacyUnobserved(session,epoch);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now()-100);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,200);
 f.database.prepare("UPDATE sessions SET operation_id='successor',epoch=?,state='running',native_id='successor-native',launch_json=? WHERE id=?")
  .run(epoch+1,JSON.stringify({workspace:'/different-successor'}),session);
 f.clearCache();
 const before=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,200);
 assert.deepEqual(f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session),before);
 assert.deepEqual(f.revocations.map(value=>[value.operation_id,value.epoch]),[[receipt.operation_id,epoch]]);
 assert.equal(f.events.filter(event=>event.path==='/_internal/reaped').length,1);
});

test('an expired legacy attempt retains its verified history and revokes without notifying a successor Project',async()=>{
 const f=fixture(),session='legacy-expired-history',epoch=await startClaimed(f,session,null),receipt=legacyUnobserved(session,epoch);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(receipt.operation_id,session,epoch,Date.now()-100);
 f.database.prepare("UPDATE sessions SET state='expired' WHERE id=?").run(session);
 f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,200);
 const current=f.database.prepare('SELECT state,evidence_json FROM sessions WHERE id=?').get(session) as any;
 assert.equal(current.state,'expired');
 assert.equal(JSON.parse(current.evidence_json).legacy_launch_recovery.native_outcome,'unconfirmed');
 assert.deepEqual(f.revocations.map(value=>[value.operation_id,value.epoch]),[[receipt.operation_id,epoch]]);
 assert.equal(f.events.some(event=>event.path==='/_internal/reaped'),false);
 const outbox=f.database.prepare("SELECT state FROM machine_outbox WHERE kind='legacy-launch-failure'").get() as any;
 assert.equal(outbox.state,'delivered');
});

async function completedHeadless(f:ReturnType<typeof fixture>,session:string,native='11111111-2222-4333-8444-555555555555',complete=true,workspace='/work/retained'){
 const epoch=await startClaimed(f,session,native),started=Date.now()-100;
 const stored=f.database.prepare('SELECT launch_json FROM sessions WHERE id=?').get(session) as any;
 const launch={...JSON.parse(stored.launch_json),workspace,timeout_seconds:1800,interactive:false};
 f.database.prepare('UPDATE sessions SET launch_json=? WHERE id=?').run(JSON.stringify(launch),session);
 f.database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run('op-'+session,session,epoch,started);f.clearCache();
 const finish={operation_id:'op-'+session,epoch,native_id:native,exit_code:0,native_exit_confirmed:true,evidence:{source:'aplexer_retirement',
  native_identity:{id:native,tag:'task-hub2-'+session,workspace},containment_empty:true,
  native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:started+1800000,exceeded:false}}};
 if(complete){const response=await f.call('/api/sessions/'+session+'/finish','POST',finish);assert.equal(response.status,201,await response.clone().text());}
 return{epoch,native,finish};
}

test('qualified completed headless capture is retained-only and never requires a running session',async()=>{
 const f=fixture(),session='completed-retained-capture';await completedHeadless(f,session);
 const actor={'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})};
 const response=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'retained-once'},actor);
 assert.equal(response.status,201,await response.clone().text());
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'completed-awaiting-review');
});


const captureOperator={'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})};
test('failed legacy screen capture releases the machine queue without changing send or stop fences',async()=>{
 const f=fixture(),first='poison-screen',next='starved-capture';
 await completedHeadless(f,first,undefined,false);await completedHeadless(f,next,'22222222-2222-4222-8222-222222222222',false);
 const {socket,frames}=captureSocket(f);
 const enqueue=async(session:string,key:string)=>(await (await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key,mode:'screen'},captureOperator)).json() as any).command;
 const poison=await enqueue(first,'poison');
 const waiting=await enqueue(next,'waiting');
 f.database.prepare('UPDATE session_commands SET created_at=? WHERE id=?').run(1,poison.id);
 frames.length=0;await (f.machine as any).sendPendingCommands(socket,'h1');assert.equal(frames.at(-1).command.id,poison.id);
 const path='/api/sessions/'+first+'/commands/'+poison.id+'/ack';
 const receipt={epoch:poison.epoch,result:{error:'capture_outcome_uncertain',reason:'capture_outcome_uncertain'}};
 assert.equal((await f.call(path,'POST',receipt)).status,200);
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(poison.id) as any).state,'failed');
 assert.equal(frames.at(-1).command.id,waiting.id);
 assert.equal((await f.call(path,'POST',receipt)).status,200,'failed receipt replay is idempotent');
 assert.equal((await f.call(path,'POST',{...receipt,result:{error:'different'}})).status,409);
});

test('interrupted terminal capture accepts only exact failure binding and never reports output',async()=>{
 const f=fixture(),session='terminal-capture-interrupted';await completedHeadless(f,session);
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'interrupted'},captureOperator);
 const command=(await made.json() as any).command;
 const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
 const result={error:'capture_outcome_uncertain',reason:'capture_outcome_uncertain',capture_binding:command.capture_binding,capture_source:'unavailable'};
 assert.equal((await f.call(path,'POST',{epoch:command.epoch,result:{...result,output:'invented'}})).status,400);
 assert.equal((await f.call(path,'POST',{epoch:command.epoch,result:{...result,capture_binding:{...command.capture_binding,native_id:'foreign'}}})).status,409);
 assert.equal((await f.call(path,'POST',{epoch:command.epoch,result})).status,200);
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(command.id) as any).state,'failed');
});
function captureSocket(f:ReturnType<typeof fixture>){const frames:any[]=[];const socket={readyState:1,
 deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'capture-test'}),
 send:(raw:string)=>frames.push(JSON.parse(raw))};f.sockets.push(socket);return{socket,frames};}

test('completed retained capture dispatch and ACK bind exact op/native/digest and remain immutable',async()=>{
 const f=fixture(),session='retained-ack',meta=await completedHeadless(f,session),{frames}=captureSocket(f);
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'retained'},captureOperator);
 assert.equal(made.status,201);const wire=(await made.json() as any).command;
 assert.deepEqual(frames.at(-1).command,wire);
 assert.deepEqual(wire.capture_binding,{native_id:meta.native,operation_id:'op-'+session,
  request_digest:(f.database.prepare('SELECT request_digest FROM session_commands WHERE id=?').get(wire.id) as any).request_digest});
 const path='/api/sessions/'+session+'/commands/'+wire.id+'/ack';
 const body={epoch:meta.epoch,result:{output:'retained tool event',capture_binding:wire.capture_binding,capture_source:'retained_task_output'}};
 assert.equal((await f.call(path,'POST',body)).status,200);
 f.restart();assert.equal((await f.call(path,'POST',body)).status,200);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'completed-awaiting-review');
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(wire.id) as any).state,'acked');
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'retained'},captureOperator)).status,200);
});

test('headless capture queued running survives positive finish with the identical read-only envelope',async()=>{
 const f=fixture(),session='capture-finish-race',meta=await completedHeadless(f,session,undefined,false),{socket,frames}=captureSocket(f);
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'before-finish'},captureOperator);
 assert.equal(made.status,201);const before=frames.at(-1).command;
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',meta.finish)).status,201);
 await (f.machine as any).sendPendingCommands(socket,'h1');assert.deepEqual(frames.at(-1).command,before);
 const result={output:'retained after completion',capture_source:'retained_task_output',capture_binding:before.capture_binding};
 assert.equal((await f.call('/api/sessions/'+session+'/commands/'+before.id+'/ack','POST',{epoch:meta.epoch,result})).status,200);
});

test('terminal retained capture refuses screen, unqualified state/proof, and nonoperator authority',async()=>{
 const f=fixture(),session='capture-terminal-refusals';await completedHeadless(f,session);
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',mode:'screen',key:'screen'},captureOperator)).status,409);
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'spoof',capture_binding:{native_id:'foreign'}},captureOperator)).status,400);
 const agent={'x-hub2-actor':JSON.stringify({id:'agent',kind:'agent',resource_id:'project-test',scopes:['session:capture']})};
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'agent'},agent)).status,409);
 const original=f.database.prepare('SELECT * FROM sessions WHERE id=?').get(session) as any;
 for(const state of ['stopped','lost','failed']){
  f.database.prepare('UPDATE sessions SET state=? WHERE id=?').run(state,session);f.clearCache();
  assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:state},captureOperator)).status,409);
 }
 f.database.prepare("UPDATE sessions SET state='completed-awaiting-review',evidence_digest=NULL WHERE id=?").run(session);f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'no-proof'},captureOperator)).status,409);
 f.database.prepare('UPDATE sessions SET evidence_digest=?,evidence_json=? WHERE id=?').run(original.evidence_digest,JSON.stringify({...JSON.parse(original.evidence_json),native_id:'foreign'}),session);f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'foreign-proof'},captureOperator)).status,409);
});

test('terminal capture missing retained output is typed failed and repeats only its durable receipt',async()=>{
 const f=fixture(),session='capture-unavailable',meta=await completedHeadless(f,session);
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'missing'},captureOperator);const command=(await made.json() as any).command;
 const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
 const body={epoch:meta.epoch,result:{error:'task_output_unavailable',capture_source:'retained_task_output',capture_binding:command.capture_binding}};
 assert.equal((await f.call(path,'POST',body)).status,200);assert.equal((await f.call(path,'POST',body)).status,200);
 const row=f.database.prepare('SELECT state,result_json FROM session_commands WHERE id=?').get(command.id) as any;
 assert.equal(row.state,'failed');assert.equal(JSON.parse(row.result_json).reason,'task_output_unavailable');
});

test('terminal ACK never accepts a foreign binding or an unqualified native pane; running legacy remains compatible',async()=>{
 const f=fixture(),session='retained-fences',meta=await completedHeadless(f,session,undefined,false);
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'running-legacy'},captureOperator);const command=(await made.json() as any).command;
 const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
 assert.equal((await f.call(path,'POST',{epoch:meta.epoch,result:{output:'legacy running'}})).status,200);
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',meta.finish)).status,201);
 assert.equal((await f.call(path,'POST',{epoch:meta.epoch,result:{output:'legacy running'}})).status,200,'old running ACK replay stays idempotent after finish');
 const terminal=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'new-terminal'},captureOperator);const c=(await terminal.json() as any).command;
 for(const result of [{output:'wrong'}, {output:'pane',capture_source:'native_pane',capture_binding:c.capture_binding},
  {output:'wrong digest',capture_source:'retained_task_output',capture_binding:{...c.capture_binding,request_digest:'0'.repeat(64)}}]){
  const ack=await f.call('/api/sessions/'+session+'/commands/'+c.id+'/ack','POST',{epoch:meta.epoch,result});
  assert([400,409].includes(ack.status));
 }
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(c.id) as any).state,'pending');
});

test('real Python retained file ACK composes with qualified completed Machine capture without native calls',async()=>{
 const {spawnSync}=await import('node:child_process'),{mkdirSync,mkdtempSync,rmSync}=await import('node:fs'),{resolve,join}=await import('node:path');
 const base=resolve(import.meta.dirname,'../../.local/tmp');mkdirSync(base,{recursive:true});const directory=mkdtempSync(join(base,'terminal-capture-wire-'));
 const f=fixture(),session='terminal-wire-composition';
 try{
  const meta=await completedHeadless(f,session,undefined,true,directory);
  const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'composed'},captureOperator);assert.equal(made.status,201);
  const wire=(await made.json() as any).command;
  const script=`import json,sys,time
from pathlib import Path
from agent_hub_host.agent import Bridge,Journal
from agent_hub_host import task_output
command=json.loads(sys.stdin.read());root=Path(sys.argv[1]);journal=Journal(root/'private/journal.sqlite')
binding=command['capture_binding'];tag='task-hub2-'+command['session_id'];task='hub2-'+command['session_id']
journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES(?,?,?,?,?,?,?,'exited',0)",(binding['operation_id'],command['session_id'],command['epoch'],'immutable-launch',str(root),tag,binding['native_id']));journal.db.commit()
spec={'workspace':str(root),'operation_id':binding['operation_id'],'epoch':command['epoch']}
output=task_output.prepare_output(task,spec,'local-go','opencode','synthetic private prompt');outdir=root.joinpath(*output['parts']);created=int(time.time()*1000)-1000
record={'session_id':binding['native_id'],'workspace':str(root),'tag':tag,'created_at_ms':created,'task_output':output}
start={'schema_version':1,'parent_session':{'id':binding['native_id'],'workspace':str(root),'tag':tag},'cwd':str(root),'output_dir':str(outdir),'started_ms':created+1,'profile':'local-go','engine':'opencode','prompt_sha256':output['prompt_sha256']}
for path,value in ((root/'.local'/('launch-'+task+'.json'),record),(outdir/'START.json',start)):
 path.write_text(json.dumps(value));path.chmod(0o600)
text=json.dumps({'type':'tool_use','part':{'type':'tool','tool':'shell','state':{'status':'completed','output':'CANARY-OK:synthetic-retained'}}})
(outdir/'stdout.log').write_text(text);(outdir/'stdout.log').chmod(0o664)
def forbidden(*args,**kw):raise AssertionError('terminal capture must not invoke any native command')
bridge=Bridge(None,journal,[root],['go'],executable=sys.executable,runner=forbidden,machine_id='h1')
result=bridge.command(command);assert result['capture_source']=='retained_task_output' and result['output']==text
journal.db.close();journal=Journal(root/'private/journal.sqlite');bridge.journal=journal;bridge.launcher.journal=journal
assert bridge.command(command)==result
payload=json.loads(journal.db.execute('SELECT payload FROM outbox').fetchone()[0]);journal.db.close();print(json.dumps(payload))
`;
  const child=spawnSync('python3',['-c',script,directory],{input:JSON.stringify(wire),encoding:'utf8',timeout:10000,maxBuffer:16384,
   env:{...process.env,PYTHONPATH:resolve(import.meta.dirname,'../host')}});
  assert.equal(child.status,0,child.stderr);const body=JSON.parse(child.stdout);
  assert.equal((await f.call('/api/sessions/'+session+'/commands/'+wire.id+'/ack','POST',body)).status,200);
  const result=JSON.parse((f.database.prepare('SELECT result_json FROM session_commands WHERE id=?').get(wire.id) as any).result_json);
  assert.equal(JSON.parse(result.output).part.state.status,'completed');assert.equal(result.capture_source,'retained_task_output');
  assert(!JSON.stringify(result).includes('synthetic private prompt'));
 }finally{f.database.close();rmSync(directory,{recursive:true});}
});

test('managed Claude opt-in needs current socket heartbeat, not stale persisted capabilities',async()=>{
 const f=fixture(),frames:any[]=[];
 const cap='managed:hub-claude:opus:medium:v1';
 const makeSocket=(id:string)=>{
  let attachment:any={machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:Date.now(),connection_id:id};
  return{readyState:1,deserializeAttachment:()=>attachment,serializeAttachment:(next:any)=>{attachment=next;},
   send:(raw:string)=>frames.push(JSON.parse(raw)),close(){this.readyState=3;}};
 };
 const survivingOld=makeSocket('surviving-old');f.sockets.push(survivingOld);
 const beat=()=>f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['headless',cap]});
 assert.equal((await beat()).status,200);
 const reservation=(id:string,extra:any={})=>f.call('/_internal/reserve','POST',{
  operation_id:'op-'+id,dispatch_id:'dispatch-'+id,attempt:0,session_id:id,project_id:'project-test',
  intake_id:'intake-'+id,machine_id:'h1',request_digest:'digest-'+id,title:'managed proof',
  acceptance_criteria:['proof'],account_id:'claude',launch:{workspace:'/tmp',engine:'claude',model:'opus',
   effort:'medium',prompt:'do real work',interactive:false,managed_credential_label:'hub-claude',...extra}});
 // A new daemon's HTTP heartbeat must never bless the surviving old socket.
 assert.equal((await reservation('pre-handshake')).status,409);
 assert.notEqual(survivingOld.deserializeAttachment().managed_claude_ready,true);
 const first=makeSocket('managed-first');f.sockets.splice(0,1,first);
 const frame={type:'managed.claude.ready',protocol:'hub2-managed-claude-exec-v1',ready:true};
 await f.machine.webSocketMessage(survivingOld as any,JSON.stringify(frame));
 assert.equal((await reservation('stale-socket-frame')).status,409);
 await f.machine.webSocketMessage(first as any,JSON.stringify({...frame,ready:'true'}));
 assert.equal((await reservation('malformed-frame')).status,409);
 await f.machine.webSocketMessage(first as any,JSON.stringify(frame));frames.length=0;
 const accepted=await reservation('managed-one');assert.equal(accepted.status,201);
 const epoch=(await accepted.json() as any).epoch;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-managed-one',session_id:'managed-one',epoch})).status,201);
 assert.equal(frames.filter(frame=>frame.type==='session.offer').length,1);
 const poll=await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})});
 assert.deepEqual((await poll.json() as any).offers,[],'old REST pollers cannot receive the new selector');
 // Rollback/reconnect has no capability observation even though DB still has it.
 f.sockets.splice(0);const old=makeSocket('old-host');f.sockets.push(old);frames.length=0;
 const oldRouting=await (await f.call('/_internal/routing')).json() as any;
 assert(!oldRouting.capabilities.includes(cap));
 assert.equal((await reservation('managed-two')).status,409);
 await (f.machine as any).sendOffers(old,'h1');assert.equal(frames.length,0);
 await f.machine.webSocketMessage(old as any,JSON.stringify({type:'offer_ack',session_id:'managed-one',epoch}));
 assert.equal(frames.at(-1).error,'managed_exec_unavailable');
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('managed-one') as any).state,'offered');
 // Source upgrade + scoped repin are observed by ordinary heartbeat, no restart.
 assert.equal((await beat()).status,200);
 assert.equal((await reservation('managed-two')).status,409,'HTTP alone cannot enable this socket');
 await f.machine.webSocketMessage(old as any,JSON.stringify(frame));
 assert.equal((await reservation('managed-two')).status,201);
 frames.length=0;await (f.machine as any).sendOffers(old,'h1');
 assert(frames.some(frame=>frame.offer.launch.managed_credential_label==='hub-claude'));
 assert.equal((await reservation('bad-model',{model:'sonnet'})).status,400);
 assert.equal((await reservation('bad-mode',{interactive:true})).status,400);
 assert.equal((await reservation('bad-label',{managed_credential_label:'other'})).status,400);
 // Withdrawal is also a normal heartbeat and immediately stops opt-in dispatch.
 await f.call('/api/heartbeat','POST',{labels:[],engines:['claude'],capabilities:['headless']});
 assert.equal((await reservation('managed-three')).status,409);
 // More than one whole offer page may remain pending across a rollback.
 await beat();await f.machine.webSocketMessage(old as any,JSON.stringify(frame));
 for(let i=0;i<10;i++){
  const id='held-'+i,r=await reservation(id);assert.equal(r.status,201);
  const value=await r.json() as any;
  assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-'+id,session_id:id,epoch:value.epoch})).status,201);
 }
 f.sockets.splice(0);const legacy=makeSocket('legacy-after-rollback');f.sockets.push(legacy);frames.length=0;
 const normal=await f.call('/_internal/reserve','POST',{operation_id:'op-normal',dispatch_id:'dispatch-normal',attempt:0,
  session_id:'ordinary-after-managed',project_id:'project-test',intake_id:'ordinary-intake',machine_id:'h1',request_digest:'c'.repeat(64),
  title:'ordinary start',acceptance_criteria:['proof'],account_id:'codex',launch:{workspace:'/tmp',engine:'codex',model:'gpt-6.1-sol',
   prompt:'normal work',interactive:false}});
 assert.equal(normal.status,201);const normalEpoch=(await normal.json() as any).epoch;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-normal',session_id:'ordinary-after-managed',epoch:normalEpoch})).status,201);
 await (f.machine as any).sendOffers(legacy,'h1');
 assert(frames.some(value=>value.type==='session.offer'&&value.offer.session_id==='ordinary-after-managed'));
 assert(!frames.some(value=>value.type==='session.offer'&&value.offer.launch.managed_credential_label));
 const ordinaryPoll=await f.call('/api/offers','GET',undefined,{'x-hub2-actor':JSON.stringify({...machineActor,scopes:['machine:offer']})});
 assert.deepEqual((await ordinaryPoll.json() as any).offers.map((offer:any)=>offer.session_id),['ordinary-after-managed']);
 f.database.close();
});

test('64KiB retained capture survives HTTP escaping, exact replay and targeted receipt lookup',async()=>{
 const f=fixture(),session='capture-64k',meta=await completedHeadless(f,session);
 for(const [index,output] of ['x'.repeat(65536),'\0'.repeat(65536),'😀'.repeat(16384)].entries()){
  const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'large-'+index},captureOperator);
  const command=(await made.json() as any).command;
  const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
  const body={epoch:meta.epoch,result:{output,capture_binding:command.capture_binding,capture_source:'retained_task_output'}};
  const frozen=JSON.stringify(body);
  const replay=()=>f.machine.fetch(new Request('https://machine.test'+path,{method:'POST',headers:header,body:frozen}));
  assert.equal((await replay()).status,200,'first delivery of a previously unaccepted large frozen ACK');
  const accepted=f.database.prepare('SELECT result_json FROM session_commands WHERE id=?').get(command.id) as any;
  f.restart();const duplicate=await replay();assert.equal(duplicate.status,200);
  assert.equal((await duplicate.json() as any).duplicate,true,'lost-200 receipt retries exactly after restart');
  assert.equal((f.database.prepare('SELECT result_json FROM session_commands WHERE id=?').get(command.id) as any).result_json,accepted.result_json);
  assert.equal((await f.call(path,'POST',{...body,result:{...body.result,output:output.slice(1)}})).status,409);
  const read=await f.call('/api/sessions/'+session+'/commands?command_id='+command.id,'GET',undefined,captureOperator);
  const data=await read.json() as any;assert.equal(data.commands.length,1);
  assert.equal(data.commands[0].ack.capture,output);assert.equal(data.commands[0].ack.output,output);
 }
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'over-limit'},captureOperator);
 const command=(await made.json() as any).command;
 const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
 assert.equal((await f.call(path,'POST',{epoch:meta.epoch,result:{output:'x'.repeat(65537),capture_binding:command.capture_binding,capture_source:'retained_task_output'}})).status,400);
 assert.equal((await f.call(path,'POST',{epoch:meta.epoch,result:{output:'small',padding:'x'.repeat(401*1024)}})).status,413);
 assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(command.id) as any).state,'pending');
});

test('capture allowance never enlarges persisted SEND or STOP ACK budgets',async()=>{
 const f=fixture(),session='noncapture-bounds',meta=await completedHeadless(f,session);
 for(const kind of ['send','stop']){
  const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:kind+'-limits'},captureOperator);
  const command=(await made.json() as any).command;
  f.database.prepare('UPDATE session_commands SET kind=? WHERE id=?').run(kind,command.id);
  const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
  for(const output of ['x'.repeat(9000),'\0'.repeat(65536)]){
   assert.equal((await f.call(path,'POST',{epoch:meta.epoch,result:{output}})).status,413,kind+' old result/body budget');
  }
  assert.equal((f.database.prepare('SELECT state FROM session_commands WHERE id=?').get(command.id) as any).state,'pending');
 }
});

test('large legacy running screen capture uses same byte budget without retained metadata',async()=>{
 const f=fixture(),session='legacy-capture-64k',epoch=await startClaimed(f,session,'11111111-1111-4111-8111-111111111111');
 const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'screen',mode:'screen'},captureOperator);
 const command=(await made.json() as any).command;assert.equal(command.capture_binding,undefined);
 const path='/api/sessions/'+session+'/commands/'+command.id+'/ack';
 assert.equal((await f.call(path,'POST',{epoch,result:{output:'x'.repeat(65536)}})).status,200);
});

test('oversized unfiltered history refuses explicitly while selected receipts stay complete and scoped',async()=>{
 const f=fixture(),session='large-history',meta=await completedHeadless(f,session);let target='';
 for(let index=0;index<12;index++){
  const made=await f.call('/api/sessions/'+session+'/commands','POST',{kind:'capture',key:'history-'+index},captureOperator);
  const command=(await made.json() as any).command;target=command.id;
  assert.equal((await f.call('/api/sessions/'+session+'/commands/'+target+'/ack','POST',{
   epoch:meta.epoch,result:{output:'\0'.repeat(65536),capture_binding:command.capture_binding,capture_source:'retained_task_output'}})).status,200);
 }
 const history=await f.call('/api/sessions/'+session+'/commands','GET',undefined,captureOperator);
 assert.equal(history.status,413);assert.equal((await history.json() as any).error,'command_history_too_large');
 const exact=await (await f.call('/api/sessions/'+session+'/commands?command_id='+target,'GET',undefined,captureOperator)).json() as any;
 assert.equal(exact.commands.length,1);assert.equal(exact.commands[0].ack.capture.length,65536);
 for(const query of ['command_id=bad%2Fid','command_id=','command_id='+target+'&command_id='+target]){
  assert.equal((await f.call('/api/sessions/'+session+'/commands?'+query,'GET',undefined,captureOperator)).status,400);
 }
 const unknown=await (await f.call('/api/sessions/'+session+'/commands?command_id=missing','GET',undefined,captureOperator)).json() as any;
 assert.deepEqual(unknown,{commands:[],next_cursor:null});
 await completedHeadless(f,'other-history','99999999-2222-4333-8444-555555555555');
 const foreign=await (await f.call('/api/sessions/other-history/commands?command_id='+target,'GET',undefined,captureOperator)).json() as any;
 assert.deepEqual(foreign,{commands:[],next_cursor:null});
 const denied={'x-hub2-actor':JSON.stringify({id:'agent',kind:'agent',resource_id:'foreign',scopes:['session:read']})};
 assert.equal((await f.call('/api/sessions/'+session+'/commands?command_id='+target,'GET',undefined,denied)).status,404);
});
