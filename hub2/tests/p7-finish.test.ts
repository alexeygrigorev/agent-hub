import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import {terminalReceipt} from '../src/terminal-receipts.ts';
import {MeteredDb} from '../src/db.ts';
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


async function stoppedFixture(){
 const f=fixture(),session='stopped-probe',native='11111111-2222-4333-8444-555555555555';
 const epoch=await startClaimed(f,session,native),now=Date.now();
 f.database.prepare("INSERT INTO launch_admissions VALUES(?,?,?,?)").run('op-'+session,session,epoch,now-1000);
 f.database.prepare("UPDATE sessions SET launch_json=?,state='stopped' WHERE id=?").run(JSON.stringify({workspace:'/tmp',timeout_seconds:60}),session);
 f.database.prepare("INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,state,result_json,created_at,updated_at) VALUES(?,?,'h1',?,'stop','digest','stop','acked',?,?,?)").run('stop',session,epoch,JSON.stringify({stopped:true,evidence:'native_exit',native_id:native}),now,now);
 const finish={operation_id:'op-'+session,epoch,native_id:native,exit_code:129,native_exit_confirmed:true,evidence:{source:'aplexer_status',containment_empty:true,
  native_identity:{id:native,tag:'task-hub2-'+session,workspace:'/tmp'},native_exit:{code:129,exited_at_ms:now},native_deadline:{deadline_ms:now-1000+60000,exceeded:false}}};
 return {f,session,native,epoch,finish};
}

test('qualified SIGHUP finish after confirmed stop returns duplicate and preserves stop',async()=>{
 const {f,session,finish}=await stoppedFixture();
 for(let i=0;i<2;i++){
  const response=await f.call('/api/sessions/'+session+'/finish','POST',finish);
  assert.equal(response.status,200,JSON.stringify(await response.clone().json()));
  assert.deepEqual(await response.json(),{ok:true,duplicate:true,state:'stopped'});
 }
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'stopped');
 assert.equal(f.events.filter(e=>e.path==='/_internal/finish').length,0);
});

test('stop duplicate still requires exact native identity, epoch, deadline, and confirmed stop',async()=>{
 const {f,session,finish}=await stoppedFixture();
 for(const bad of [
  {...finish,epoch:finish.epoch+1}, {...finish,operation_id:'foreign'}, {...finish,native_id:'foreign'},
  {...finish,evidence:{...finish.evidence,containment_empty:false}},
  {...finish,evidence:{...finish.evidence,native_identity:{...finish.evidence.native_identity,workspace:'/foreign'}}},
  {...finish,evidence:{...finish.evidence,native_deadline:{...finish.evidence.native_deadline,deadline_ms:1}}},
 ])assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',bad)).status,409);
 f.database.prepare("UPDATE session_commands SET state='failed'").run();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',finish)).status,409);
});

test('expired unadmitted no-dispatch receipt closes an offer, but early and foreign receipts do not',async()=>{
 const f=fixture(),session='admission-expired',epoch=await startClaimed(f,session,null);
 const receipt={operation_id:'op-'+session,epoch,native_id:null,launch_failed:true,no_dispatch:true,admission_expired:true};
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',receipt)).status,409);
 f.database.prepare('UPDATE sessions SET created_at=?,offer_expires_at=? WHERE id=?').run(Date.now()-120000,Date.now()-1000,session);
 f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,epoch:epoch+1})).status,409);
 const accepted=await f.call('/api/sessions/'+session+'/finish','POST',receipt);
 assert.equal(accepted.status,200,JSON.stringify(await accepted.json()));
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'failed');
});

test('rejected real finish raises one durable developer alert with pinned intake id',async()=>{
 const {f,session,finish}=await stoppedFixture();
 for(let i=0;i<3;i++)assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...finish,epoch:finish.epoch+1})).status,409);
 const alerts=f.events.filter(e=>e.path==='/_internal/finish-dead-letter');
 assert.equal(alerts.length,1);assert.equal(alerts[0].body.intake_id,'intake-'+session);
 assert.equal(alerts[0].body.recipient,'dev@dtcdev.click');assert.equal(alerts[0].body.reason,'stale_or_unmatched_finish');
 f.restart();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...finish,epoch:finish.epoch+1})).status,409);
 assert.equal(f.events.filter(e=>e.path==='/_internal/finish-dead-letter').length,1);
});

test('canary classification uses persisted launch and title, never finish payload',async()=>{
 const {f,session,finish}=await stoppedFixture();
 f.database.prepare('UPDATE sessions SET title=?,launch_json=? WHERE id=?').run('hub-canary-shell-run',JSON.stringify({workspace:'/owned/hub-canary',timeout_seconds:60}),session);
 f.clearCache();
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...finish,epoch:finish.epoch+1})).status,409);
 assert.equal(f.events.filter(e=>e.path==='/_internal/finish-dead-letter').length,0);
});

test('expired absent native launch is reported without declaring an exit or changing lifecycle',async()=>{
 const {f,session,finish}=await stoppedFixture(),now=Date.now(),admitted=now-120000,deadline=admitted+60000;
 f.database.prepare("UPDATE sessions SET state='running' WHERE id=?").run(session);
 f.database.prepare('UPDATE launch_admissions SET observed_at=?').run(admitted);f.clearCache();
 const receipt={operation_id:finish.operation_id,epoch:finish.epoch,native_id:finish.native_id,launch_expired:true,
  native_outcome:'unconfirmed',runtime_deadline:deadline,evidence:{source:'aplexer_all_registry_census',dispatch_provenance:'host_journal',
  complete:true,match_count:0,workspace:'/tmp',tag:'task-hub2-'+session,started_at_ms:now-100,observed_at_ms:now,row_count:0,sha256:'a'.repeat(64)}};
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,runtime_deadline:deadline+1})).status,409);
 // A census that began before the runtime deadline can never qualify, however fresh.
 assert.equal((await f.call('/api/sessions/'+session+'/finish','POST',{...receipt,evidence:{...receipt.evidence,started_at_ms:deadline-1,observed_at_ms:deadline+10}})).status,409);
 const response=await f.call('/api/sessions/'+session+'/finish','POST',receipt);
 assert.equal(response.status,200,JSON.stringify(await response.clone().json()));
 assert.deepEqual(await response.json(),{ok:true,native_outcome:'unconfirmed',terminal_report:true});
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get(session) as any).state,'running');
 const alert=f.events.find(e=>e.body.kind==='launch_expired_unconfirmed');
 assert.equal(alert?.body.intake_id,'intake-'+session);assert.equal(alert?.body.evidence.complete,true);
});

test('Project terminal receipt helper raises exactly one developer alert and retains stopped salvage evidence',async()=>{
 const database=new DatabaseSync(':memory:');
 database.exec(`CREATE TABLE dispatch_sagas(session_id TEXT,intake_id TEXT,machine_id TEXT,operation_id TEXT,attempt_operation_id TEXT,epoch INTEGER,native_id TEXT);
 CREATE TABLE intakes(id TEXT,final_evidence_json TEXT,updated_at INTEGER);
 CREATE TABLE intake_events(task_id TEXT,seq INTEGER,event_key TEXT UNIQUE,actor_id TEXT,kind TEXT,body_json TEXT,at INTEGER);
 INSERT INTO dispatch_sagas VALUES('s','i','h1','dispatch','attempt',1,'native');
 INSERT INTO intakes VALUES('i','{"stopped":true}',0);`);
 const db=new MeteredDb({exec(query:string,...args:unknown[]){
  const statement=database.prepare(query),rows=statement.columns().length?statement.all(...args as any):(statement.run(...args as any),[]);
  return {toArray:()=>rows,rowsRead:rows.length,rowsWritten:0};
 }} as any);
 const transaction=<T>(fn:()=>T)=>{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(e){database.exec('ROLLBACK');throw e;}};
 const append=(id:string,key:string,actor:string,kind:string,body:any,at:number)=>db.run('INSERT INTO intake_events VALUES(?,?,?,?,?,?,?)',id,
  Number((database.prepare('SELECT COUNT(*) AS n FROM intake_events').get() as any).n)+1,key,actor,kind,JSON.stringify(body),at);
 const send=(path:string,body:any)=>terminalReceipt(new Request('https://project.internal'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),db,transaction,append,()=>({owner:'worker'}));
 const alert={session_id:'s',intake_id:'i',machine_id:'h1',operation_id:'dispatch',attempt_operation_id:'attempt',epoch:1,
  kind:'finish_dead_letter',reason:'stale_or_unmatched_finish',status:409,recipient:'dev@dtcdev.click'};
 for(let i=0;i<3;i++)assert.equal((await send('/_internal/finish-dead-letter',alert)).status,200);
 assert.equal((database.prepare("SELECT COUNT(*) AS n FROM intake_events WHERE kind='developer_alert_raised'").get() as any).n,1);
 assert.equal(JSON.parse((database.prepare("SELECT body_json FROM intake_events WHERE kind='intake_context_updated'").get() as any).body_json).owner,'dev@dtcdev.click');
 assert.equal((await send('/_internal/finish-dead-letter',{...alert,intake_id:'foreign'})).status,409);
 assert.equal((await send('/_internal/finish-dead-letter',{...alert,machine_id:'old-host',attempt_operation_id:'old-attempt',epoch:0})).status,200);
 assert.equal((database.prepare("SELECT COUNT(*) AS n FROM intake_events WHERE kind='developer_alert_raised'").get() as any).n,2);
 assert.equal((await send('/_internal/stopped-finish',{...alert,machine_id:'old-host',state:'stopped',native_id:'native',native_exit_confirmed:true,evidence_digest:'b'.repeat(64)})).status,409);
 const evidence={git_salvage:{branch:'fix/work',commit:'a'.repeat(40),pushed:true}};
 assert.equal((await send('/_internal/stopped-finish',{...alert,state:'stopped',native_id:'native',native_exit_confirmed:true,evidence_digest:'b'.repeat(64),evidence})).status,200);
 const final=JSON.parse((database.prepare("SELECT final_evidence_json FROM intakes WHERE id='i'").get() as any).final_evidence_json);
 assert.equal(final.stopped,true);assert.deepEqual(final.finish_after_stop.evidence,evidence);
 database.close();
});
