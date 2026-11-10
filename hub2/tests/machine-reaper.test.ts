import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MachineDO } from '../src/machine-do.ts';
import { LIVE_SNAPSHOT_TTL_MS } from '../src/session-liveness.ts';

const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
const header={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)};

function fixture(){
 const database=new DatabaseSync(':memory:'),events:Array<{path:string;body:any}>=[];let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'machine-h1'},storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return{toArray:()=>rows,rowsRead,rowsWritten};
 }},async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(){return[];}};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true})})},
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{events.push({path:new URL(request.url).pathname,body:await request.json()});return Response.json({ok:true});}})},
 };
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,headers:Record<string,string>={})=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{...header,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,machine,call,events,get alarm(){return alarm;}};
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

test('dry-run reports an orphan and the live reaper marks it stopped without slot accounting',async()=>{
 const f=fixture(),epoch=await startClaimed(f,'orphan-session',null);
 f.database.prepare('UPDATE sessions SET updated_at=? WHERE id=?').run(Date.now()-3*60_000,'orphan-session');
 const dry=await f.call('/_internal/reap','POST',{dry_run:true});assert.equal(dry.status,200);
 const preview=await dry.json() as any;assert.equal(preview.reaped,0);assert.equal(preview.candidates[0].session_id,'orphan-session');
 assert.equal((f.database.prepare('SELECT state,epoch FROM sessions WHERE id=?').get('orphan-session') as any).state,'claimed');
 const live=await f.call('/_internal/reap','POST',{dry_run:false});const result=await live.json() as any;
 assert.equal(live.status,200);assert.equal(result.reaped,1);assert.equal(result.active_count,0);assert.equal(result.active_count_after,0);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('orphan-session') as any).state,'stopped');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');assert.equal(f.events.at(-1)?.body.epoch,epoch);
 assert.equal(f.database.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='machine_active'").get(),undefined);
 assert.equal(f.database.prepare('PRAGMA table_info(machines)').all().some((row:any)=>row.name==='active_session_id'),false);
 assert.equal(f.database.prepare('PRAGMA table_info(machine_capabilities)').all().some((row:any)=>row.name==='resources_json'),false);
});

test('stale host snapshots never claim an orphan was killed',async()=>{
 const f=fixture();await startClaimed(f,'orphan-on-stale-host',null);
 f.database.prepare('UPDATE sessions SET updated_at=? WHERE id=?').run(Date.now()-10*60_000,'orphan-on-stale-host');
 f.database.prepare('UPDATE machine_liveness SET observed_at=? WHERE singleton=1').run(Date.now()-LIVE_SNAPSHOT_TTL_MS-1);
 const response=await f.call('/_internal/reap','POST',{dry_run:false}),report=await response.json() as any;
 assert.equal(report.skipped,'host_liveness_stale');assert.equal(report.reaped,0);
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('orphan-on-stale-host') as any).state,'claimed');
 assert.equal((await f.call('/_internal/live-sessions')).status,200);
});

test('a native process absent from a fresh host snapshot is marked stopped without a pending stop command',async()=>{
 const f=fixture();await startClaimed(f,'missing-native-session','native-missing');
 const response=await f.call('/_internal/reap','POST',{dry_run:false}),report=await response.json() as any;
 assert.equal(response.status,200);assert.equal(report.reaped,1);assert.equal(report.stop_requested,0,JSON.stringify(report));
 assert.equal((f.database.prepare('SELECT state FROM sessions WHERE id=?').get('missing-native-session') as any).state,'stopped');
 assert.equal((f.database.prepare("SELECT COUNT(*) AS count FROM session_commands WHERE session_id=? AND kind='stop'").get('missing-native-session') as any).count,0);
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');
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
