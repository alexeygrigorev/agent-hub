// Hub-owned task completion: process exit is not completion, unfinished work is
// continued (bounded) by Hub itself, and the outcome is delivered to the owner.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {NodeSqlStorage,NodeDurableStorage} from '../node/sqlite-storage.ts';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';
import {CONTRACT_MARKER,CONTINUATION_MARKER,MAX_AUTO_CONTINUATIONS,basePrompt,completionClaim,completionContract,continuationPrompt} from '../src/task-completion';

const admin={id:'human:completion-owner',kind:'operator',scopes:['operator:admin']};
const candidates=[{id:'sol-route',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex-private',weight:1,capabilities:[],enabled:true}];
const AC=['Feature works for the requester','PR merged with green CI'];

function fixture(){
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  const directory=mkdtempSync(join(parent,'task-completion-')),file=join(directory,'project.sqlite');
  const controlSql=new NodeSqlStorage(join(directory,'control.sqlite'));
  let control:ControlDO,sql:NodeSqlStorage,project:ProjectDO;
  const reservations:any[]=[],mail:any[]=[];
  let capDenials=0,mailFailures=0;
  const namespace=(handle:(id:string,request:Request)=>Promise<Response>)=>({idFromName:(id:string)=>id,
    get:(id:string)=>({fetch:(input:string|Request,init?:RequestInit)=>handle(id,input instanceof Request?input:new Request(input,init))})});
  const env:any={
    CONTROL:namespace(async(_id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/picker/draw')return control.fetch(r);
      if(path==='/_routing-policy')return Response.json({version:1,candidates});
      if(path==='/_internal/session-cap/check'&&capDenials>0){capDenials--;return Response.json({error:'active_session_limit'},{status:429});}
      return Response.json({registered:true,accepted:true});
    }),
    MACHINES:namespace(async(id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/routing')return Response.json({machine_id:id,connected:true,engines:['codex'],capabilities:['headless','interactive']});
      const body=await r.json() as any;
      if(path==='/_internal/reserve'){reservations.push(body);return Response.json({state:'staged',epoch:body.attempt+1});}
      return Response.json({state:'offered'});
    }),
    ACCOUNTS:namespace(async()=>Response.json({account:{enabled:true,min_remaining:0},credential_usable:true,credential_status:'ready',quota:{remaining:80},hosts:['h1']})),
    MAILBOXES:namespace(async(_id,r)=>{
      if(mailFailures>0){mailFailures--;return Response.json({error:'unavailable'},{status:503});}
      const body=await r.json() as any,actor=JSON.parse(r.headers.get('x-hub2-actor')!);
      mail.push({...body,sender:r.headers.get('x-hub2-mailbox'),actor:actor.id});
      return Response.json({message:{id:'message-'+mail.length}},{status:201});
    }),
  };
  control=new ControlDO({storage:new NodeDurableStorage(controlSql),blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);
  const open=()=>{sql=new NodeSqlStorage(file);project=new ProjectDO({id:{toString:()=> 'PROJECTS/org/completion'},storage:new NodeDurableStorage(sql),
    blockConcurrencyWhile:(callback:any)=>Promise.resolve().then(callback)} as any,env);};
  open();
  const call=(path:string,method='GET',body?:unknown)=>project.fetch(new Request('https://project.test'+path,{method,
    headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
  const db=()=>(project as any).db;
  return {call,reservations,mail,db,get project(){return project;},
    denyCapacity(n:number){capDenials=n;},failMail(n:number){mailFailures=n;},
    reopen(){sql.close();open();},close(){sql.close();controlSql.close();rmSync(directory,{recursive:true});}};
}
type F=ReturnType<typeof fixture>;
const json=async(r:Response)=>await r.json() as any;

async function createIntake(f:F,key:string){
  const r=await f.call('/api/intakes?project=org/completion','POST',{key,title:'Ship the requested feature',text:'Implement the requested feature end to end.',acceptance_criteria:AC});
  assert.equal(r.status,201);return (await json(r)).intake.id as string;
}
const intakeRow=(f:F,id:string)=>f.db().one('SELECT * FROM intakes WHERE id=?',id);
const sagas=(f:F,id:string)=>f.db().all('SELECT * FROM dispatch_sagas WHERE intake_id=? ORDER BY created_at,operation_id',id) as any[];
const events=(f:F,id:string)=>(f.db().all('SELECT * FROM intake_events WHERE task_id=? ORDER BY seq',id) as any[]).map(e=>({...e,body:JSON.parse(e.body_json)}));
const completion=(f:F,id:string)=>JSON.parse(intakeRow(f,id).final_evidence_json??'{}').completion;

/** Drive the newest saga of an intake to running, like an admitted host launch. */
async function run(f:F,id:string){
  const saga=sagas(f,id).at(-1),native=crypto.randomUUID();
  f.db().run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",saga.operation_id);
  const binding={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:native};
  assert.equal((await f.call('/_internal/started','POST',binding)).status,201);
  return binding;
}
async function finish(f:F,binding:any,exit:number,deadline=false){
  const r=await f.call('/_internal/finish','POST',{...binding,exit_code:exit,native_exit_confirmed:true,evidence:{result:'exit',native_deadline:{exceeded:deadline}}});
  assert.equal(r.status,200,JSON.stringify(await r.clone().json()));
}
async function claim(f:F,id:string,sessionId:string,acceptance=AC.map(()=>({status:'met',proof:'commit abc123, CI run 42 green, live check ok'})),key='claim-'+sessionId){
  const r=await f.call('/api/intakes/'+id+'/events','POST',{key,event:{kind:'task_complete',session_id:sessionId,summary:'Feature shipped and verified live.',acceptance}});
  assert.equal(r.status,201);
}
const start=(f:F,id:string,key:string,extra:object={})=>f.call('/api/intakes/'+id+'/start','POST',{key,workspace:'/srv/work/feature',prompt:'Implement the requested feature end to end.',...extra});

test('task sessions receive the completion contract with intake, session and criteria within the launch limit',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'contract');
    assert.equal((await start(f,id,'first')).status,201);
    const saga=sagas(f,id)[0],launch=f.reservations.at(-1).launch;
    assert(launch.prompt.startsWith('Implement the requested feature end to end.'+CONTRACT_MARKER));
    for(const text of [id,saga.session_id,'1. '+AC[0],'2. '+AC[1],'"kind":"task_complete"','hub intake append '+id])assert(launch.prompt.includes(text),text);
    assert.equal(JSON.parse(saga.routing_json).completion_contract,true);
    assert.equal(basePrompt(launch.prompt),'Implement the requested feature end to end.');
    assert(new TextEncoder().encode(JSON.stringify(launch)).length<=12_000);
    // Huge criteria never push the launch past the Machine limit.
    const big=completionContract('i','s',Array.from({length:32},(_,n)=>'criterion '+n+' '+'x'.repeat(990)),4000);
    assert(new TextEncoder().encode(big).length<=4000);assert(big.includes('hub intake show i'));
  }finally{f.close();}
});

test('false completion: exit 0 without a completion claim keeps the task unfinished and Hub continues it itself',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'false-completion');
    assert.equal((await start(f,id,'first')).status,201);
    const first=await run(f,id);
    // A NOT-DONE note is not a claim; neither is a claim for another session or a partial claim.
    await f.call('/api/intakes/'+id+'/events','POST',{key:'handoff',note:'HANDOFF: NOT DONE, PR still open'});
    await claim(f,id,'00000000-0000-4000-8000-000000000000',undefined,'foreign');
    await claim(f,id,first.session_id,[{status:'met',proof:'only one'}],'partial');
    await finish(f,first,0);
    let row=intakeRow(f,id);
    assert.equal(row.status,'failed','exit 0 is not task completion');
    assert.deepEqual({outcome:completion(f,id).outcome,reason:completion(f,id).reason,state:completion(f,id).continuation.state},
      {outcome:'unfinished',reason:'exit_without_completion_claim',state:'pending'});
    assert(events(f,id).some(e=>e.kind==='task_unfinished'&&e.body.next==='continue'));
    assert.equal(f.mail.length,0,'no terminal delivery while Hub is still continuing');
    await f.project.alarm();
    row=intakeRow(f,id);
    const all=sagas(f,id),next=all.at(-1);
    assert.equal(all.length,2);assert.equal(row.session_id,next.session_id);assert(['queued','running'].includes(row.status));
    const launch=JSON.parse(next.launch_spec_json);
    assert(launch.prompt.startsWith(CONTINUATION_MARKER));
    assert(launch.prompt.includes(first.session_id)&&launch.prompt.includes('hub intake history '+id));
    assert(launch.prompt.includes('Implement the requested feature end to end.'+CONTRACT_MARKER),'original task and a fresh contract');
    assert(launch.prompt.includes(next.session_id),'contract binds the continuation session');
    assert.equal(launch.timeout_seconds,1800);assert.equal(next.machine_id,first.machine_id);assert.equal(launch.workspace,'/srv/work/feature');
    const started=events(f,id).filter(e=>e.kind==='session_start_requested');
    assert.equal(started.at(-1).actor_id,'hub2-completion');
    assert(events(f,id).some(e=>e.kind==='task_continuation_started'&&e.body.previous_session_id===first.session_id&&e.body.depth===1));
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'one continuation per unfinished exit');
  }finally{f.close();}
});

test('a claimed task is finished even when its process times out, and the outcome is delivered once to the owner',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'claimed-timeout');
    assert.equal((await start(f,id,'first')).status,201);
    const first=await run(f,id);
    await claim(f,id,first.session_id);
    await finish(f,first,124,true);
    assert.equal(intakeRow(f,id).status,'completed-awaiting-review','completion claim, not exit code, decides');
    assert.equal(completion(f,id).outcome,'claimed');
    assert(events(f,id).some(e=>e.kind==='task_completion_claimed'&&e.body.criteria_met===2));
    f.failMail(1);
    await f.project.alarm();
    assert.equal(f.mail.length,0);assert.equal(completion(f,id).delivery.state,'pending');assert.equal(completion(f,id).delivery.tries,1);
    assert(completion(f,id).delivery.next_action_at>Date.now(),'transient delivery failure backs off');
    f.db().run("UPDATE intakes SET final_evidence_json=json_set(final_evidence_json,'$.completion.delivery.next_action_at',0) WHERE id=?",id);
    await f.project.alarm();
    assert.equal(f.mail.length,1);
    const message=f.mail[0];
    assert.equal(message.recipient,admin.id);assert.equal(message.sender,'intake:'+id);assert.equal(message.key,'terminal:'+first.operation_id);
    assert.match(message.body,/Task finished, awaiting review/);assert(message.body.includes(id)&&message.body.includes('Feature shipped and verified live.'));
    assert.equal(completion(f,id).delivery.state,'delivered');
    assert(events(f,id).some(e=>e.kind==='task_outcome_delivered'&&e.body.message_id==='message-1'));
    f.reopen();await f.project.alarm();
    assert.equal(f.mail.length,1,'delivery is durable and never repeated');
    assert.equal(sagas(f,id).length,1,'a finished task is never continued');
  }finally{f.close();}
});

test('continuations are bounded per requester start; deadline exits get more time; exhaustion is delivered',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'bounded');
    assert.equal((await start(f,id,'first')).status,201);
    for(let attempt=0;attempt<=MAX_AUTO_CONTINUATIONS;attempt++){
      const binding=await run(f,id);
      await finish(f,binding,124,true);
      await f.project.alarm();
    }
    const all=sagas(f,id);
    assert.equal(all.length,1+MAX_AUTO_CONTINUATIONS);
    assert.deepEqual(all.map(s=>JSON.parse(s.launch_spec_json).timeout_seconds),[1800,3600,7200]);
    assert.equal(intakeRow(f,id).status,'failed');
    assert.equal(completion(f,id).continuation,undefined);assert.equal(completion(f,id).depth,MAX_AUTO_CONTINUATIONS);
    assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/Task NOT finished/);assert.match(f.mail[0].body,/continued it 2 times/);
    await f.project.alarm();assert.equal(sagas(f,id).length,3,'no unbounded restart loop');
    // A new requester start opens a new bounded budget.
    assert.equal((await start(f,id,'human-restart')).status,201);
    await finish(f,await run(f,id),0);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,5);
  }finally{f.close();}
});

test('a refused continuation start retries with backoff, then reports the unfinished task',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'refused');
    assert.equal((await start(f,id,'first')).status,201);
    await finish(f,await run(f,id),1);
    f.denyCapacity(10);
    for(let i=0;i<3;i++){
      await f.project.alarm();
      f.db().run("UPDATE intakes SET final_evidence_json=json_set(final_evidence_json,'$.completion.continuation.next_action_at',0) WHERE id=? AND json_extract(final_evidence_json,'$.completion.continuation.state')='pending'",id);
    }
    assert.equal(sagas(f,id).length,1);
    assert.equal(completion(f,id).continuation.state,'refused');assert.equal(completion(f,id).continuation.tries,3);
    assert(events(f,id).some(e=>e.kind==='task_continuation_refused'));
    await f.project.alarm();
    assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/could not start a continuation/);
  }finally{f.close();}
});

test('an operator stop and a launch failure are reported, not continued; a dead session is continued',async()=>{
  const f=fixture();
  try{
    const stopped=await createIntake(f,'stopped');
    assert.equal((await start(f,stopped,'first')).status,201);
    const binding=await run(f,stopped);
    const receipt={operation_id:binding.operation_id,attempt_operation_id:binding.attempt_operation_id,session_id:binding.session_id,machine_id:binding.machine_id,epoch:binding.epoch,native_id:binding.native_id,observed_at:Date.now()};
    assert.equal((await f.call('/_internal/stopped','POST',{...receipt,reason:'stop_confirmed'})).status,200);
    await f.project.alarm();
    assert.equal(sagas(f,stopped).length,1);assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/session_stop_confirmed/);

    const dead=await createIntake(f,'dead');
    assert.equal((await start(f,dead,'first')).status,201);
    const deadBinding=await run(f,dead);
    assert.equal((await f.call('/_internal/reaped','POST',{operation_id:deadBinding.operation_id,attempt_operation_id:deadBinding.attempt_operation_id,session_id:deadBinding.session_id,
      machine_id:deadBinding.machine_id,epoch:deadBinding.epoch,native_id:deadBinding.native_id,observed_at:Date.now(),reason:'dead'})).status,200);
    await f.project.alarm();
    assert.equal(sagas(f,dead).length,2,'an exited unclaimed session is continued');

    const refused=await createIntake(f,'refused-launch');
    assert.equal((await start(f,refused,'first')).status,201);
    const saga=sagas(f,refused)[0];
    f.db().run("UPDATE dispatch_sagas SET state='offered' WHERE operation_id=?",saga.operation_id);
    assert.equal((await f.call('/_internal/reaped','POST',{operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,
      epoch:saga.epoch,native_id:null,launch_failed:true,failure_reason:'provider_refresh_invalid_grant',observed_at:Date.now(),reason:'host_launch_refused'})).status,200);
    await f.project.alarm();
    assert.equal(sagas(f,refused).length,1,'launch refusals are not retried in a loop');
    const report=f.mail.find(m=>m.key==='terminal:'+saga.operation_id);
    assert(report);assert.match(report.body,/launch_failed/);
  }finally{f.close();}
});

test('a requester restart before the continuation supersedes it without a duplicate owner',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'superseded');
    assert.equal((await start(f,id,'first')).status,201);
    const first=await run(f,id);
    await finish(f,first,0);
    assert.equal((await start(f,id,'manual')).status,201);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'only the requester start exists');
    assert(!events(f,id).some(e=>e.kind==='session_start_requested'&&e.actor_id==='hub2-completion'));
  }finally{f.close();}
});

test('completion helpers: claims bind to the session and criteria count; continuation prompts stay bounded',()=>{
  const ok=AC.map(()=>({status:'met',proof:'p'}));
  assert(completionClaim({kind:'task_complete',session_id:'s',acceptance:ok},'s',2));
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:ok},'other',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[...ok,{status:'met',proof:'p'}]},'s',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[{status:'met',proof:' '},ok[1]]},'s',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[{status:'partial',proof:'p'},ok[1]]},'s',2),undefined);
  const long=continuationPrompt('i',1,'s','exit','é'.repeat(8192));
  assert(new TextEncoder().encode(long).length<=8192);
  assert.equal(basePrompt(continuationPrompt('i',2,'s','exit','Original')+CONTRACT_MARKER+'contract'),'Original');
});
