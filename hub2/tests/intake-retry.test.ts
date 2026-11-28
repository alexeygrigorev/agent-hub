// Failed intakes whose sessions carry no completion contract (operator shells
// and legacy attempts) are retried by Hub itself: bounded attempts, growing
// backoff, machine/route fallback, escalation only when the bound is spent or
// a human blocker says a fresh session cannot clear the cause.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {NodeSqlStorage,NodeDurableStorage} from '../node/sqlite-storage.ts';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';

const admin={id:'human:retry-owner',kind:'operator',scopes:['operator:admin']};
const candidates=[
  {id:'sol-route',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex-private',weight:1,capabilities:[],enabled:true},
  {id:'terra-route',engine:'codex',model:'gpt-6.1-terra',effort:'medium',account_id:'codex-private',weight:1,capabilities:[],enabled:true},
];
const AC=['The task is finished with recorded proof'];

function fixture(){
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  const directory=mkdtempSync(join(parent,'intake-retry-')),file=join(directory,'project.sqlite');
  const controlSql=new NodeSqlStorage(join(directory,'control.sqlite'));
  let control:ControlDO,sql:NodeSqlStorage,project:ProjectDO;
  const reservations:any[]=[],mail:any[]=[],pickerDraws:any[]=[];
  const directoryMachines=['m1','m2'],connectedMachines=new Set(['m1','m2']);
  let capDenials=0;
  const namespace=(handle:(id:string,request:Request)=>Promise<Response>)=>({idFromName:(id:string)=>id,
    get:(id:string)=>({fetch:(input:string|Request,init?:RequestInit)=>handle(id,input instanceof Request?input:new Request(input,init))})});
  const env:any={
    CONTROL:namespace(async(_id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/api/directory')return Response.json({items:directoryMachines.map(id=>({id})),next:null});
      if(path==='/_routing-policy')return Response.json({version:1,candidates});
      if(path==='/_internal/session-cap/check'&&capDenials>0){capDenials--;return Response.json({error:'active_session_limit'},{status:429});}
      if(path==='/_internal/picker/draw'){pickerDraws.push(await r.clone().json());return control.fetch(r);}
      return Response.json({registered:true,accepted:true});
    }),
    MACHINES:namespace(async(id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/routing')return Response.json({machine_id:id,connected:connectedMachines.has(String(id)),engines:['codex'],capabilities:['headless','interactive']});
      const body=await r.json() as any;
      if(path==='/_internal/reserve'){
        assert(new TextEncoder().encode(JSON.stringify(body.launch)).length<=12_000,'Machine launch limit');
        reservations.push(body);return Response.json({state:'staged',epoch:body.attempt+1});}
      return Response.json({state:'offered'});
    }),
    ACCOUNTS:namespace(async()=>Response.json({account:{enabled:true,min_remaining:0},credential_usable:true,credential_status:'ready',quota:{remaining:80,captured_at:Date.now(),valid_until:Date.now()+900000},fresh:true,hosts:['h1']})),
    MAILBOXES:namespace(async(_id,r)=>{
      const body=await r.json() as any,actor=JSON.parse(r.headers.get('x-hub2-actor')!);
      mail.push({...body,sender:r.headers.get('x-hub2-mailbox'),actor:actor.id});
      return Response.json({message:{id:'message-'+mail.length}},{status:201});
    }),
  };
  control=new ControlDO({storage:new NodeDurableStorage(controlSql),blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  const open=()=>{sql=new NodeSqlStorage(file);project=new ProjectDO({id:{toString:()=> 'PROJECTS/org/retry'},storage:new NodeDurableStorage(sql),
    blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);};
  open();
  const call=(path:string,method='GET',body?:unknown,actor:object=admin)=>project.fetch(new Request('https://project.test'+path,{method,
    headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
  const db=()=>(project as any).db;
  return {call,reservations,mail,pickerDraws,db,get project(){return project;},
    setConnected(id:string,on:boolean){if(on)connectedMachines.add(id);else connectedMachines.delete(id);},
    denyCapacity(n:number){capDenials=n;},
    reopen(){sql.close();open();},close(){sql.close();controlSql.close();rmSync(directory,{recursive:true});}};
}
type F=ReturnType<typeof fixture>;
const json=async(r:Response)=>await r.json() as any;

async function createIntake(f:F,key:string){
  const r=await f.call('/api/intakes?project=org/retry','POST',{key,title:'Retry the failed work',text:'Finish the recorded task end to end.',acceptance_criteria:AC,code_task:false});
  assert.equal(r.status,201,JSON.stringify(await r.clone().json()));return (await json(r)).intake.id as string;
}
const intakeRow=(f:F,id:string)=>f.db().one('SELECT * FROM intakes WHERE id=?',id);
const sagas=(f:F,id:string)=>f.db().all('SELECT * FROM dispatch_sagas WHERE intake_id=? ORDER BY created_at,operation_id',id) as any[];
const activeSaga=(f:F,id:string)=>f.db().one('SELECT * FROM dispatch_sagas WHERE intake_id=? AND session_id=?',id,intakeRow(f,id)?.session_id);
const events=(f:F,id:string)=>(f.db().all('SELECT * FROM intake_events WHERE task_id=? ORDER BY seq',id) as any[]).map(e=>({...e,body:JSON.parse(e.body_json)}));
const evidence=(f:F,id:string)=>JSON.parse(intakeRow(f,id)?.final_evidence_json??'{}');
/** Pretend the failure happened longer ago so the retry pass is due: the
 * completed saga and any absolute backoff marker move together. */
const age=(f:F,id:string,ms:number)=>{
  f.db().run('UPDATE dispatch_sagas SET updated_at=updated_at-? WHERE intake_id=?',ms,id);
  f.db().run("UPDATE intakes SET final_evidence_json=json_set(final_evidence_json,'$.auto_retry.next_action_at',json_extract(final_evidence_json,'$.auto_retry.next_action_at')-?),updated_at=updated_at-? WHERE id=? AND json_extract(final_evidence_json,'$.auto_retry.next_action_at') IS NOT NULL",ms,ms,id);
};

/** Drive the newest saga of an intake to running, like an admitted host launch. */
async function run(f:F,id:string){
  const saga=activeSaga(f,id)??sagas(f,id).at(-1);
  const native=crypto.randomUUID();
  f.db().run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",saga.operation_id);
  const binding={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:native};
  const started=await f.call('/_internal/started','POST',binding);
  assert.equal(started.status,201,JSON.stringify({response:await started.clone().json(),saga_state:saga.state,intake_status:intakeRow(f,id)?.status}));
  return binding;
}
async function finish(f:F,binding:any,exit:number,extra:Record<string,unknown>={}){
  const r=await f.call('/_internal/finish','POST',{...binding,exit_code:exit,native_exit_confirmed:true,evidence:{result:'exit'},...extra});
  assert.equal(r.status,200,JSON.stringify(await r.clone().json()));
}
async function appendBlocker(f:F,id:string,sessionId:string){
  const actor={id:'session:'+sessionId,kind:'agent',resource_id:id,scopes:['intake:append']};
  const r=await f.call('/api/intakes/'+id+'/events','POST',{key:'blocked-'+sessionId,event:{kind:'task_blocked',session_id:sessionId,
    cause:'external',detail:'the deploy is owned by another session',next_step:'the deploy owner must act first'}},actor);
  assert.equal(r.status,201,JSON.stringify(await r.clone().json()));
}
async function startShell(f:F,id:string,key:string){
  return await f.call('/api/intakes/'+id+'/start','POST',{key,workspace:'retry-ws-'+key,engine:'shell',override_reason:'operator shell task',purpose:'implement',timeout_seconds:600});
}
async function startManaged(f:F,id:string,key:string,engine='codex',model='gpt-6.1-sol'){
  return await f.call('/api/intakes/'+id+'/start','POST',{key,workspace:'retry-ws-'+key,engine,model,effort:'medium'});
}

test('a failed shell session is retried on another machine, bounded, then escalates loudly',async()=>{
  const f=fixture();
  const alarmErrors:string[]=[],originalConsoleError=console.error;
  console.error=(...parts:unknown[])=>{const line=parts.map(String).join(' ');alarmErrors.push(line);originalConsoleError(...parts);};
  try{
    const id=await createIntake(f,'shell-retry-bound');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201,JSON.stringify(await started.clone().json()));
    const b1=await run(f,id);
    assert.equal(activeSaga(f,id).machine_id,'m1');
    await finish(f,b1,2);
    assert.equal(intakeRow(f,id).status,'failed');
    assert.equal(evidence(f,id).completion,undefined);
    assert.equal(typeof evidence(f,id).auto_retry?.next_action_at,'number');

    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'first retry waits for its backoff step');
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'the retry pass starts attempt 1 once due');
    const retry1=activeSaga(f,id),e1=events(f,id).find(e=>e.kind==='task_retry_started');
    assert.equal(e1?.actor_id,'hub2-retry');
    assert.equal(e1?.body.attempt,1);
    assert.equal(retry1.machine_id,'m2','the failed machine is excluded from the retry');
    const launch1=JSON.parse(retry1.launch_spec_json);
    assert.equal(launch1.engine,'shell');
    assert.match(launch1.prompt,/Hub retry 1 of at most 2/);
    assert.match(launch1.prompt,/Finish the recorded task end to end/);
    const b2=await run(f,id);
    await finish(f,b2,3);

    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'the second backoff step is longer than the first');
    age(f,id,180_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,3,'attempt 2 starts after the grown backoff');
    assert.equal(activeSaga(f,id).machine_id,'m1','the fallback moves back to the only other host');
    const e2=events(f,id).filter(e=>e.kind==='task_retry_started').at(-1);
    assert.equal(e2?.body.attempt,2);
    const b3=await run(f,id);
    await finish(f,b3,4);

    await f.project.alarm();
    assert.equal(sagas(f,id).length,3,'no third retry: the bound is two attempts');
    const marker=evidence(f,id).auto_retry;
    assert.equal(marker.state,'terminal');
    assert.equal(marker.reason,'retry_exhausted');
    const alerts=events(f,id).filter(e=>e.kind==='developer_alert_raised'&&e.body.kind==='retry_exhausted');
    assert.equal(alerts.length,1,'the escalation is raised exactly once');
    assert.equal(alerts[0].body.session_id,b3.session_id);
    const owner=events(f,id).filter(e=>e.kind==='intake_context_updated').at(-1);
    assert.equal(owner?.body.owner,'dev@dtcdev.click','the escalation names the developer owner');
    assert(alarmErrors.every(line=>!line.includes('project alarm failed')),'the alarm never throws during retries');
    assert(events(f,id).some(e=>e.kind==='task_retry_terminal'&&e.body.state==='retry_exhausted'));

    f.reopen();
    await f.project.alarm();
    assert.equal(sagas(f,id).length,3,'the terminal marker survives a restart');
  }finally{console.error=originalConsoleError;f.close();}
});

test('an operator stop is never retried by the retry pass',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-stop-kept');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    await finish(f,b,1,{stop_requested:true});
    assert.equal(intakeRow(f,id).status,'failed');
    age(f,id,86_400_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'a stop the operator asked for stays stopped');
  }finally{f.close();}
});

test('a stop receipt marks the attempt terminal without a retry',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-stop-receipt');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    f.db().run("UPDATE dispatch_sagas SET state='running' WHERE operation_id=?",b.operation_id);
    // An operator stop receipt for the running attempt (uncontrolled attempt).
    const stopped=await f.call('/_internal/stopped','POST',{...b,reason:'stop_confirmed',native_id:null,observed_at:Date.now(),stopped:true});
    assert.equal(stopped.status,200,JSON.stringify(await stopped.clone().json()));
    assert.equal(intakeRow(f,id).status,'failed');
    age(f,id,86_400_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'stop_confirmed receipts are never retried');
  }finally{f.close();}
});

test('a hard blocker stops retries and alerts the developer with the cause',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-blocked');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    await appendBlocker(f,id,b.session_id);
    await finish(f,b,1);
    assert.equal(intakeRow(f,id).status,'failed');
    age(f,id,86_400_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'no session is started into a hard blocker');
    const marker=evidence(f,id).auto_retry;
    assert.equal(marker.state,'terminal');
    assert.equal(marker.reason,'blocked_external');
    const alerts=events(f,id).filter(e=>e.kind==='developer_alert_raised'&&e.body.kind==='blocked');
    assert.equal(alerts.length,1);
    assert.equal(alerts[0].body.blocker?.cause,'external');
  }finally{f.close();}
});

test('a refused retry start escalates bounded instead of spinning',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-refused');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    await finish(f,b,2);
    // Break the start preconditions durably: the retry pass must treat the
    // refusal as terminal (alerted), not retry it on every alarm tick.
    f.db().run('UPDATE intakes SET acceptance_criteria_json=? WHERE id=?','[]',id);
    age(f,id,61_000);
    await f.project.alarm();
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'the refused start does not produce a session');
    const marker=evidence(f,id).auto_retry;
    assert.equal(marker.state,'terminal');
    assert.equal(marker.reason,'retry_start_refused');
    assert.equal(events(f,id).filter(e=>e.kind==='developer_alert_raised'&&e.body.kind==='retry_start_refused').length,1);
  }finally{f.close();}
});

test('controlled failures stay owned by the completion loop, not the retry pass',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'controlled-untouched');
    const started=await startManaged(f,id,'k1');
    assert.equal(started.status,201,JSON.stringify(await started.clone().json()));
    const b=await run(f,id);
    await finish(f,b,1);
    assert.equal(evidence(f,id).completion?.controlled,true);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'the completion loop starts its own continuation');
    const retry=events(f,id).filter(e=>e.kind==='task_retry_started');
    assert.equal(retry.length,0,'the retry pass does not double-start controlled failures');
    assert(events(f,id).some(e=>e.kind==='task_continuation_started'));
  }finally{f.close();}
});

test('a legacy uncontrolled failure retries, then falls back to another routing policy',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'legacy-route-fallback');
    const started=await startManaged(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    await finish(f,b,1);
    // Turn the finished attempt into a legacy one: no completion contract on
    // the saga, no completion record in the evidence. The due time falls back
    // to the saga's completion time plus one first backoff step.
    f.db().run("UPDATE dispatch_sagas SET routing_json=json_set(routing_json,'$.completion_contract',json('false')) WHERE intake_id=?",id);
    f.db().run("UPDATE intakes SET final_evidence_json=json_remove(final_evidence_json,'$.completion') WHERE id=?",id);
    const policy1=JSON.parse(sagas(f,id)[0].routing_json).policy_id as string;
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2);
    const draw1=f.pickerDraws.at(-1);
    assert.equal(draw1.input.machine_id,undefined,'the retry does not pin the failed machine');
    assert.equal(draw1.input.engine,'codex');
    assert.equal(draw1.input.exclude_policy_ids,undefined,'the first retry keeps the routing policy');
    const r2=activeSaga(f,id);
    assert.equal(JSON.parse(r2.launch_spec_json).engine,'codex');
    const b2=await run(f,id);
    await finish(f,b2,1);
    f.db().run("UPDATE dispatch_sagas SET routing_json=json_set(routing_json,'$.completion_contract',json('false')) WHERE intake_id=?",id);
    f.db().run("UPDATE intakes SET final_evidence_json=json_remove(final_evidence_json,'$.completion') WHERE id=?",id);
    age(f,id,241_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,3,'attempt 2 starts after the grown backoff');
    const draw2=f.pickerDraws.at(-1);
    assert.deepEqual(draw2.input.exclude_policy_ids,[policy1],'the second retry falls back to another policy');
    const e2=events(f,id).filter(e=>e.kind==='task_retry_started').at(-1);
    assert.equal(e2?.body.attempt,2);
    assert.equal(e2?.body.route_fallback,true);
    assert.equal(draw2.input.machine_id,undefined,'the fallback attempt stays free to move machines');
  }finally{f.close();}
});

test('a start that exits 75 within seconds fails over to another route on the first retry, loudly',async()=>{
  const f=fixture();
  const errors:string[]=[],original=console.error;
  console.error=(...args:unknown[])=>{errors.push(args.map(String).join(' '));};
  try{
    const id=await createIntake(f,'exit75-route-failover');
    assert.equal((await startManaged(f,id,'k1')).status,201);
    const b=await run(f,id);
    // The wrapper quota gate refused a dead credential: exit 75 in ~2 seconds.
    await finish(f,b,75);
    f.db().run("UPDATE dispatch_sagas SET routing_json=json_set(routing_json,'$.completion_contract',json('false')) WHERE intake_id=?",id);
    f.db().run("UPDATE intakes SET final_evidence_json=json_remove(final_evidence_json,'$.completion') WHERE id=?",id);
    const policy1=JSON.parse(sagas(f,id)[0].routing_json).policy_id as string;
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2);
    const draw=f.pickerDraws.at(-1);
    assert.deepEqual(draw.input.exclude_policy_ids,[policy1],'the first retry already excludes the failed route');
    assert.equal(draw.input.engine,undefined,'the failed engine/model is not re-pinned');
    assert.notEqual(JSON.parse(activeSaga(f,id).routing_json).policy_id,policy1);
    const started=events(f,id).filter(e=>e.kind==='task_retry_started').at(-1);
    assert.equal(started?.body.route_fallback,true);
    assert.equal(started?.body.fast_failure,true);
    assert.equal(started?.body.failed_policy_id,policy1);
    assert.ok(errors.some(line=>line.includes('INTAKE_ROUTE_FAILOVER')&&line.includes(policy1)),errors.join('\n'));
  }finally{console.error=original;f.close();}
});

test('uncertain retry starts back off and terminate bounded with an alert',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-cap-denied');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    await finish(f,b,2);
    f.denyCapacity(3);
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'a 429 start is deferred, not refused');
    assert.equal(evidence(f,id).auto_retry.tries,1);
    assert.equal(evidence(f,id).auto_retry.last_reason,'active_session_limit');
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(evidence(f,id).auto_retry.tries,2);
    age(f,id,241_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,1,'no session ever started');
    const marker=evidence(f,id).auto_retry;
    assert.equal(marker.state,'terminal');
    assert.equal(marker.reason,'retry_start_failed');
    assert.equal(events(f,id).filter(e=>e.kind==='developer_alert_raised'&&e.body.kind==='retry_start_failed').length,1);
    const owner=events(f,id).filter(e=>e.kind==='intake_context_updated').at(-1);
    assert.equal(owner?.body.owner,'dev@dtcdev.click');
  }finally{f.close();}
});

test('a shell retry reuses the failed machine when no other host is connected',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'shell-single-host');
    const started=await startShell(f,id,'k1');
    assert.equal(started.status,201);
    const b=await run(f,id);
    assert.equal(activeSaga(f,id).machine_id,'m1');
    await finish(f,b,2);
    f.setConnected('m2',false);
    age(f,id,61_000);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'the retry still starts');
    assert.equal(activeSaga(f,id).machine_id,'m1','the only connected host is reused despite the exclusion');
  }finally{f.close();}
});
