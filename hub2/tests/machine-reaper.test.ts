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
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,headers:Record<string,string>={})=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{...header,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,machine,call,events,sockets,revocations,clearCache:()=>((machine as any).db.cache.clear()),get alarm(){return alarm;}};
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
  title:'test session',acceptance_criteria:['test'],account_id:'codex',launch:{workspace:'relay',engine:'codex',model:'gpt-6.1-sol',prompt:'test work',interactive:false}});
 assert.equal(reservation.status,201);const reserved=await reservation.json() as any;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-'+sessionId,session_id:sessionId,epoch:reserved.epoch})).status,201);
 return reserved.epoch as number;
}

test('host launch refusal terminally fails an unclaimed offer with a visible reason',async()=>{
 const f=fixture(),sessionId='workspace-label-refused',epoch=await startOffered(f,sessionId),frames:any[]=[];
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'socket'}),send:(value:string)=>frames.push(JSON.parse(value))};
 f.sockets.push(socket);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,stage:'host_launch',reason:'workspace_label_unavailable'}));
 const row=f.database.prepare('SELECT state,native_id FROM sessions WHERE id=?').get(sessionId) as any;
 assert.equal(row.state,'failed');assert.equal(row.native_id,null);
 assert.equal((f.database.prepare('SELECT reason FROM attention WHERE session_id=?').get(sessionId) as any).reason,'workspace_label_unavailable');
 assert.equal(f.events.at(-1)?.path,'/_internal/reaped');
 assert.equal(f.events.at(-1)?.body.epoch,epoch);assert.equal(f.events.at(-1)?.body.reason,'host_launch_refused');
 assert.equal(f.events.at(-1)?.body.failure_reason,'workspace_label_unavailable');
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
