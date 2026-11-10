import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { MachineDO } from '../src/machine-do.ts';
import { LIVE_SNAPSHOT_TTL_MS } from '../src/session-liveness.ts';

const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
const header={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)};

function fixture(){
 const database=new DatabaseSync(':memory:'),events:Array<{path:string;body:any}>=[],sockets:any[]=[];let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'machine-h1'},storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return{toArray:()=>rows,rowsRead,rowsWritten};
 }},async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(){return sockets;}};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true})})},
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{events.push({path:new URL(request.url).pathname,body:await request.json()});return Response.json({ok:true});}})},
 };
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,headers:Record<string,string>={})=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{...header,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,machine,call,events,sockets,clearCache:()=>((machine as any).db.cache.clear()),get alarm(){return alarm;}};
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
  operation_id:'op-'+sessionId,reason:'workspace_label_unavailable'}));
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

test('live reaper fails an expired claimed no-native launch without slot accounting',async()=>{
 const f=fixture(),epoch=await startClaimed(f,'orphan-session',null);
 f.database.prepare('UPDATE sessions SET updated_at=? WHERE id=?').run(Date.now()-3*60_000,'orphan-session');
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
 f.database.prepare('UPDATE sessions SET updated_at=? WHERE id=?').run(Date.now()-10*60_000,'orphan-on-stale-host');
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
 f.database.prepare('UPDATE sessions SET updated_at=? WHERE id=?').run(now-3*60_000,'launching-without-native');
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
 assert.equal((await f.call(`/api/sessions/${sessionId}/commands/${stop.id}/ack`,'POST',{epoch,result:{stopped:true}})).status,200);
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


test('session projections carry host workspace route and timestamp columns for the CLI',async()=>{
 const f=fixture(),session='projection';await startClaimed(f,session,'native-projection');
 const response=await f.call('/api/sessions/'+session,'GET',undefined,{'x-hub2-actor':JSON.stringify({id:'admin',kind:'operator',scopes:['operator:admin']})});
 assert.equal(response.status,200);const view=(await response.json() as any).session;
 assert.equal(view.host,'h1');assert.equal(view.machine,'h1');assert.equal(view.workspace,'/tmp');
 assert.equal(view.account_id,'codex');assert.equal(view.account,'codex');assert.equal(view.mode,'headless');
 assert.equal(view.managed,true);assert.ok(Number.isFinite(Date.parse(view.created_at)));assert.ok(Number.isFinite(Date.parse(view.updated_at)));
});
