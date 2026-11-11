// Hub-owned task completion: process exit is not completion, unfinished work is
// continued (bounded) by Hub itself, and the outcome is delivered to the owner.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {NodeSqlStorage,NodeDurableStorage} from '../node/sqlite-storage.ts';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';
import {MachineDO} from '../src/machine-do';
import {CONTRACT_MARKER,CONTINUATION_MARKER,MAX_AUTO_CONTINUATIONS,basePrompt,completionClaim,completionContract,continuationPrompt} from '../src/task-completion';

const admin={id:'human:completion-owner',kind:'operator',scopes:['operator:admin']};
const candidates=[{id:'sol-route',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex-private',weight:1,capabilities:[],enabled:true}];
const AC=['Feature works for the requester','PR merged with green CI'];

function fixture(routes=candidates){
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
      if(path==='/_routing-policy')return Response.json({version:1,candidates:routes});
      if(path==='/_internal/session-cap/check'&&capDenials>0){capDenials--;return Response.json({error:'active_session_limit'},{status:429});}
      return Response.json({registered:true,accepted:true});
    }),
    MACHINES:namespace(async(id,r)=>{
      const path=new URL(r.url).pathname;
      if(path==='/_internal/routing')return Response.json({machine_id:id,connected:true,engines:['codex'],capabilities:['headless','interactive']});
      const body=await r.json() as any;
      if(path==='/_internal/reserve'){
        assert(new TextEncoder().encode(JSON.stringify(body.launch)).length<=12_000,'Machine launch limit');
        reservations.push(body);return Response.json({state:'staged',epoch:body.attempt+1});}
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
  const call=(path:string,method='GET',body?:unknown,actor:object=admin)=>project.fetch(new Request('https://project.test'+path,{method,
    headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},...(body===undefined?{}:{body:JSON.stringify(body)})}));
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
    assert(!launch.prompt.startsWith('-'),'a prompt starting with - is parsed as a CLI option by the engine');
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

test('a continuation never runs for a replaced attempt and keeps the original requester',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'superseded');
    assert.equal((await start(f,id,'first')).status,201);
    await finish(f,await run(f,id),0);
    // A requester restart clears the pending record entirely.
    assert.equal((await start(f,id,'manual')).status,201);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2);
    assert(!events(f,id).some(e=>e.kind==='session_start_requested'&&e.actor_id==='hub2-completion'));
    // A stale pending record that no longer matches the intake's attempt is never acted on.
    await finish(f,await run(f,id),0);
    const pending=intakeRow(f,id).final_evidence_json;
    f.db().run("UPDATE intakes SET session_id='00000000-0000-4000-8000-00000000beef' WHERE id=?",id);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,2,'no continuation for a replaced attempt');
    f.db().run('UPDATE intakes SET session_id=? WHERE id=?',sagas(f,id).at(-1).session_id,id);
    assert.equal(intakeRow(f,id).final_evidence_json,pending);
    await f.project.alarm();
    assert.equal(sagas(f,id).length,3);
    assert.equal(intakeRow(f,id).assigned_actor_id,admin.id,'continuation keeps the original requester for routes and delivery');
  }finally{f.close();}
});

test('claims bind to the writing session and to its start; host evidence cannot plant Hub actions',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'claim-binding');
    assert.equal((await start(f,id,'first')).status,201);
    const first=await run(f,id),acceptance=AC.map(()=>({status:'met',proof:'p'}));
    const other={id:'session:another',kind:'agent',resource_id:id,scopes:['intake:append']};
    assert.equal((await f.call('/api/intakes/'+id+'/events','POST',{key:'spoof',event:{kind:'task_complete',session_id:first.session_id,acceptance}},other)).status,403);
    const own={id:'session:'+first.session_id,kind:'agent',resource_id:id,scopes:['intake:append']};
    assert.equal((await f.call('/api/intakes/'+id+'/events','POST',{key:'own',event:{kind:'task_complete',session_id:first.session_id,acceptance}},own)).status,201);
    await finish(f,first,0);
    assert.equal(intakeRow(f,id).status,'completed-awaiting-review','the session itself may claim');

    const early=await createIntake(f,'claim-before-start');
    // A claim naming a future session (ids are predictable) does not count.
    const r=await start(f,early,'first');assert.equal(r.status,201);
    const saga=sagas(f,early)[0];
    f.db().run('DELETE FROM intake_events WHERE task_id=? AND kind=?',early,'worker_append');
    const startSeq=events(f,early).find(e=>e.event_key===`start:${saga.operation_id}:requested`).seq;
    f.db().run('UPDATE intake_events SET seq=seq+100 WHERE task_id=? AND seq>=?',early,startSeq);
    f.db().run("INSERT INTO intake_events(task_id,seq,event_key,actor_id,kind,body_json,at) VALUES(?,?,?,?,?,?,?)",early,startSeq,'pre-claim',admin.id,'worker_append',
      JSON.stringify({evidence:{kind:'task_complete',session_id:saga.session_id,acceptance}}),Date.now());
    await finish(f,await run(f,early),0);
    assert.equal(intakeRow(f,early).status,'failed','a claim written before the session started is ignored');

    // A pre-deploy (unmarked) saga keeps exit-code semantics and its evidence cannot schedule Hub actions.
    const legacy=await createIntake(f,'legacy');
    assert.equal((await start(f,legacy,'first')).status,201);
    const old=sagas(f,legacy)[0];
    f.db().run('UPDATE dispatch_sagas SET routing_json=json_remove(routing_json,\'$.completion_contract\') WHERE operation_id=?',old.operation_id);
    const binding=await run(f,legacy);
    const planted={controlled:true,session_id:binding.session_id,outcome:'unfinished',reason:'x',depth:0,continuation:{state:'pending',key:'evil',tries:0,next_action_at:0}};
    assert.equal((await f.call('/_internal/finish','POST',{...binding,exit_code:1,native_exit_confirmed:true,evidence:{result:'exit',completion:planted}})).status,200);
    assert.equal(completion(f,legacy),undefined,'host evidence completion is dropped');
    await f.project.alarm();
    assert.equal(sagas(f,legacy).length,1);assert.equal(f.mail.filter(m=>m.sender==='intake:'+legacy).length,0);
  }finally{f.close();}
});

test('quote-heavy maximal prompts stay within the Machine launch limit',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'quotes');
    const prompt=Array.from({length:8192},(_,i)=>i%3===0?'"':'a').join('');
    assert.equal(new TextEncoder().encode(prompt).length,8192);
    assert.equal((await start(f,id,'quotes',{prompt})).status,201,'a start that fits today still fits');
    const launch=f.reservations.at(-1).launch;
    assert(new TextEncoder().encode(JSON.stringify(launch)).length<=12_000);
    const routing=JSON.parse(sagas(f,id)[0].routing_json);
    assert.equal(routing.completion_contract,true,'launch budget never restores exit-code completion');
    assert.equal(routing.completion_contract_omitted==='launch_budget',!launch.prompt.includes(CONTRACT_MARKER));
    const quotes=await createIntake(f,'quote-criteria');
    f.db().run('UPDATE intakes SET acceptance_criteria_json=? WHERE id=?',JSON.stringify(Array.from({length:32},()=>'"\\'.repeat(330))),quotes);
    assert.equal((await start(f,quotes,'quote-criteria',{prompt:'x'.repeat(4000)})).status,201);
    assert(new TextEncoder().encode(JSON.stringify(f.reservations.at(-1).launch)).length<=12_000);
  }finally{f.close();}
});

test('completion helpers: claims bind to the session and criteria count; continuation prompts stay bounded',()=>{
  const ok=AC.map(()=>({status:'met',proof:'p'}));
  assert(completionClaim({kind:'task_complete',session_id:'s',acceptance:ok},'s',2));
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:ok},'other',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[...ok,{status:'met',proof:'p'}]},'s',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[{status:'met',proof:' '},ok[1]]},'s',2),undefined);
  assert.equal(completionClaim({kind:'task_complete',session_id:'s',acceptance:[{status:'partial',proof:'p'},ok[1]]},'s',2),undefined);
  const long=continuationPrompt('i',1,'s','exit','😀'.repeat(4096));
  assert(new TextEncoder().encode(long).length<=8192);assert(long.includes('full task: hub intake show i'));
  assert(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(long),'no split surrogate pair');
  assert.equal(basePrompt(continuationPrompt('i',2,'s','exit','Original')+CONTRACT_MARKER+'contract'),'Original');
  assert(!continuationPrompt('i',1,'s','exit','- bullet task').startsWith('-'));
  assert.equal(basePrompt('--- Hub continuation ---\nold preamble\n--- Original task ---\nOriginal'+CONTRACT_MARKER+'c'),'Original','legacy continuation prompts still unwrap');
});


test('pending continuation and delivery survive an outage longer than 25 hours',async()=>{
  const f=fixture();
  try{
    const unclaimed=await createIntake(f,'outage-continue');await start(f,unclaimed,'first');await finish(f,await run(f,unclaimed),0);
    const done=await createIntake(f,'outage-deliver');await start(f,done,'first');const binding=await run(f,done);await claim(f,done,binding.session_id);await finish(f,binding,0);
    f.db().run('UPDATE intakes SET updated_at=?',Date.now()-25*3600000);
    f.reopen();await f.project.alarm();
    assert.equal(sagas(f,unclaimed).length,2,'old unfinished attempt is continued');
    assert.equal(f.mail.length,1,'old terminal outcome is delivered');assert.equal(completion(f,done).delivery.state,'delivered');
  }finally{f.close();}
});

test('eligible current actions are filtered before LIMIT and alarm MIN; terminal continuation timestamps cannot hide delivery',async()=>{
  const f=fixture();
  try{
    const old=Date.now()-26*3600000;
    for(let n=0;n<9;n++){
      const id=await createIntake(f,'ineligible-'+n);await start(f,id,'first');const b=await run(f,id);await claim(f,id,b.session_id);await finish(f,b,0);
      const evidence=JSON.parse(intakeRow(f,id).final_evidence_json);evidence.completion.delivery.next_action_at=0;
      if(n%3===0)evidence.completion.controlled=false;
      else if(n%3===1)evidence.completion.session_id='replaced-session';
      else f.db().run("UPDATE dispatch_sagas SET routing_json=json_remove(routing_json,'$.completion_contract') WHERE intake_id=?",id);
      f.db().run('UPDATE intakes SET final_evidence_json=?,updated_at=? WHERE id=?',JSON.stringify(evidence),old+n,id);
    }
    const id=await createIntake(f,'eligible');await start(f,id,'first');const b=await run(f,id);await claim(f,id,b.session_id);await finish(f,b,0);
    const evidence=JSON.parse(intakeRow(f,id).final_evidence_json),due=Date.now()-100;
    evidence.completion.delivery.next_action_at=due;
    evidence.completion.continuation={state:'refused',key:'old-continuation',tries:3,next_action_at:Date.now()+3600000};
    f.db().run('UPDATE intakes SET final_evidence_json=?,updated_at=? WHERE id=?',JSON.stringify(evidence),old+100,id);
    assert.equal((f.project as any).completionNextDeadline(),due,'alarm ignores ineligible and nonpending action timestamps');
    f.reopen();await f.project.alarm();
    assert.equal(f.mail.length,1,'nine ineligible rows cannot starve the eligible ninth-plus action');assert.equal(completion(f,id).delivery.state,'delivered');
    assert.equal((f.project as any).completionNextDeadline(),undefined,'terminal or foreign records cannot spin the alarm');
  }finally{f.close();}
});

test('continuation uses the accepted absolute workspace instead of re-resolving a mutable label',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'bound-workspace');await start(f,id,'first',{workspace:'repo-label'});const b=await run(f,id);
    assert((await f.call('/_internal/started','POST',{...b,workspace_binding:{version:1,requested_workspace:'repo-label',resolved_workspace:'/srv/accepted-worktree'}})).ok);
    await finish(f,b,0);await f.project.alarm();
    assert.equal(JSON.parse(sagas(f,id).at(-1).launch_spec_json).workspace,'/srv/accepted-worktree');
  }finally{f.close();}
});

for(const failure of ['retry','refused','superseded','throw','outer-catch']){
  test('late '+failure+' continuation result cannot mutate a newer claimed manual attempt',async()=>{
    const f=fixture();
    try{
      const id=await createIntake(f,'late-'+failure);await start(f,id,'first');await finish(f,await run(f,id),0);
      const project=f.project as any,original=project.startIntake.bind(project);let replaced=false,snapshot:any;
      const replace=async()=>{
        replaced=true;
        const response=await original(new Request('https://project.internal/api/intakes/'+id+'/start',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(admin)},
          body:JSON.stringify({key:'manual-successor',workspace:'/srv/work/feature',prompt:'new requester attempt'})}),id);
        assert(response.ok,await response.text());const b=await run(f,id);await claim(f,id,b.session_id);await finish(f,b,0);snapshot=intakeRow(f,id);
      };
      if(failure==='outer-catch')project.continueTask=async()=>{await replace();throw new Error('test late outer failure');};
      else project.startIntake=async(request:Request,intakeId:string)=>{
        if(JSON.parse(request.headers.get('x-hub2-actor')!).id!=='hub2-completion'||replaced)return original(request,intakeId);
        await replace();
        if(failure==='throw')throw new Error('test late start failure');
        return Response.json({error:failure==='superseded'?'intake_already_assigned':'temporary_failure'},{status:failure==='retry'?503:failure==='superseded'?409:400});
      };
      await f.project.alarm();
      assert.equal(replaced,true);assert.equal(intakeRow(f,id).session_id,snapshot.session_id);
      assert.equal(intakeRow(f,id).status,'completed-awaiting-review');assert.equal(intakeRow(f,id).final_evidence_json,snapshot.final_evidence_json);
      assert.equal(intakeRow(f,id).updated_at,snapshot.updated_at);assert.equal(completion(f,id).outcome,'claimed');assert.equal(completion(f,id).continuation,undefined);
      await f.project.alarm();assert.equal(f.mail.length,1,'new claimed result still delivers normally');assert.equal(sagas(f,id).length,2);
    }finally{f.close();}
  });
}

test('valid quote-heavy prompt stays exact and outcome-controlled when the contract cannot fit',async()=>{
  const f=fixture();
  try{
    const prompt='"'.repeat(5700),id=await createIntake(f,'omitted-contract');assert.equal((await start(f,id,'first',{prompt})).status,201);
    const routing=JSON.parse(sagas(f,id)[0].routing_json);assert.equal(routing.completion_contract,true);assert.equal(routing.completion_contract_omitted,'launch_budget');
    assert.equal(f.reservations.at(-1).launch.prompt,prompt);await finish(f,await run(f,id),0);
    assert.equal(intakeRow(f,id).status,'failed');assert.equal(completion(f,id).outcome,'unfinished');assert.equal(completion(f,id).continuation,undefined);
    await f.project.alarm();assert.equal(sagas(f,id).length,1);assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/NOT finished/);
    const done=await createIntake(f,'omitted-claim');await start(f,done,'first',{prompt});const b=await run(f,done);await claim(f,done,b.session_id);await finish(f,b,0);
    assert.equal(intakeRow(f,done).status,'completed-awaiting-review');assert.equal(completion(f,done).outcome,'claimed','a valid explicit claim still counts');
  }finally{f.close();}
});

test('auth fallback preserves omitted-contract outcome control and original prompt',async()=>{
  const f=fixture([...candidates,{...candidates[0],id:'alternate-route'}]);
  try{
    const prompt='"'.repeat(5700),id=await createIntake(f,'omitted-auth');await start(f,id,'first',{prompt});const before=sagas(f,id)[0];
    const response=await f.call('/_internal/auth-fallback','POST',{operation_id:before.operation_id,attempt_operation_id:before.attempt_operation_id,session_id:before.session_id,machine_id:before.machine_id,epoch:before.epoch,reason:'provider_auth_failed'});
    assert.equal(response.status,200,await response.text());const after=sagas(f,id)[0],routing=JSON.parse(after.routing_json);
    assert.equal(after.attempt,1);assert.equal(routing.completion_contract,true);assert.equal(routing.completion_contract_omitted,'launch_budget');assert.equal(JSON.parse(after.launch_spec_json).prompt,prompt);
    await finish(f,await run(f,id),0);await f.project.alarm();assert.equal(sagas(f,id).length,1);assert.equal(completion(f,id).outcome,'unfinished');assert.equal(f.mail.length,1);
  }finally{f.close();}
});

/** Real Machine -> Project finish forwarding, including a lost response after commit. */
function machineFixture(f:F,loseFirstReply=false){
  const directory=mkdtempSync(join(resolve('.local/tmp'),'completion-machine-')),sql=new NodeSqlStorage(join(directory,'machine.sqlite'));
  const forwards:any[]=[];
  const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
  const env:any={
    CONTROL:{idFromName:(v:string)=>v,get:()=>({fetch:async()=>Response.json({authorized:true,revoked:true})})},
    PROJECTS:{idFromName:(v:string)=>v,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
      const request=input instanceof Request?input:new Request(input,init);forwards.push(await request.clone().json());
      const response=await f.project.fetch(request);
      if(loseFirstReply&&forwards.length===1){assert(response.ok);return Response.json({error:'lost_reply'},{status:503});}
      return response;
    }})},
  };
  const machine=new MachineDO({storage:new NodeDurableStorage(sql),blockConcurrencyWhile:(fn:any)=>Promise.resolve().then(fn),getWebSockets:()=>[]} as any,env);
  const call=(path:string,body:any,actor:any=machineActor)=>machine.fetch(new Request('https://machine.test'+path,{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},body:JSON.stringify(body)}));
  return {machine,call,forwards,db:()=>(machine as any).db,
    async running(id:string){
      assert((await call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]})).ok);
      const reservation=f.reservations.find(r=>r.intake_id===id);assert(reservation);
      const response=await call('/_internal/reserve',reservation),reserved=await json(response);assert(response.ok,JSON.stringify(reserved));
      f.db().run('UPDATE dispatch_sagas SET epoch=? WHERE intake_id=? AND session_id=?',reserved.epoch,id,reservation.session_id);
      const binding=await run(f,id),now=Date.now();
      (machine as any).db.run("UPDATE sessions SET state='running',native_id=? WHERE id=?",binding.native_id,binding.session_id);
      (machine as any).db.run('INSERT INTO launch_admissions VALUES(?,?,?,?)',binding.attempt_operation_id,binding.session_id,binding.epoch,now-100);
      const body={operation_id:binding.attempt_operation_id,epoch:binding.epoch,native_id:binding.native_id,exit_code:0,native_exit_confirmed:true,
        evidence:{source:'aplexer_status',containment_empty:true,native_identity:{id:binding.native_id,tag:'task-hub2-'+binding.session_id,workspace:'/srv/work/feature'},
          native_exit:{code:0,exited_at_ms:now},native_deadline:{deadline_ms:now-100+1800000,exceeded:false}}};
      return {binding,body};
    },
    close(){sql.close();rmSync(directory,{recursive:true});},
  };
}

test('Machine pending STOP intent prevents a child before ACK and freezes finish retries after a lost Project reply',async()=>{
  const f=fixture(),m=machineFixture(f,true);
  try{
    const id=await createIntake(f,'pending-stop-race');await start(f,id,'first');const {binding,body}=await m.running(id),base='/api/sessions/'+binding.session_id;
    const stopped=await m.call(base+'/commands',{kind:'stop',key:'operator-stop'},admin);assert.equal(stopped.status,201);const command=(await json(stopped)).command;
    assert.equal(m.db().one('SELECT state FROM session_commands WHERE id=?',command.id).state,'pending');
    const finished=await m.call(base+'/finish',body);assert.equal(finished.status,202,'Project committed but its first reply was lost');
    const outboxId='finish:'+binding.attempt_operation_id,frozen=m.db().one('SELECT payload_json FROM machine_outbox WHERE id=?',outboxId).payload_json;
    assert.equal(JSON.parse(frozen).stop_requested,true);assert.equal(completion(f,id).reason,'stop_requested');assert.equal(completion(f,id).continuation,undefined);
    assert.equal(m.db().one('SELECT state FROM session_commands WHERE id=?',command.id).state,'pending','finish must not manufacture a STOP ACK');
    await f.project.alarm();assert.equal(sagas(f,id).length,1);assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/NOT finished/);
    const late=await m.call(base+'/commands/'+command.id+'/ack',{epoch:binding.epoch,result:{stopped:true,evidence:'native_exit',native_id:binding.native_id}});
    assert.equal(late.status,409,'existing terminal-session ACK policy is preserved');
    const ack=await m.call(base+'/commands/'+command.id+'/ack',{epoch:binding.epoch,result:{stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed'}});
    assert.equal(ack.status,200);assert.equal(m.db().one('SELECT state FROM session_commands WHERE id=?',command.id).state,'failed');
    assert.equal((await m.call(base+'/finish',body)).status,200,'host replay stays idempotent after command state changes');
    assert.equal(await (m.machine as any).flushOne(outboxId),true,'retry must not poison the Project completion digest');
    assert.deepEqual(m.forwards[1],m.forwards[0]);assert.equal(m.db().one('SELECT payload_json FROM machine_outbox WHERE id=?',outboxId).payload_json,frozen);
    assert.equal(m.db().one('SELECT state FROM machine_outbox WHERE id=?',outboxId).state,'delivered');assert.equal(sagas(f,id).length,1);
  }finally{m.close();f.close();}
});

test('host-forged STOP intent cannot suppress an ordinary unfinished continuation',async()=>{
  const f=fixture(),m=machineFixture(f);
  try{
    const id=await createIntake(f,'forged-stop');await start(f,id,'first');const {binding,body}=await m.running(id);
    const response=await m.call('/api/sessions/'+binding.session_id+'/finish',{...body,stop_requested:true,evidence:{...body.evidence,stop_requested:true}});
    assert.equal(response.status,201);assert.equal(m.forwards[0].stop_requested,false);assert.equal(JSON.parse(intakeRow(f,id).final_evidence_json).stop_requested,false);
    await f.project.alarm();assert.equal(sagas(f,id).length,2,'only Machine-owned STOP intent can inhibit continuation');
  }finally{m.close();f.close();}
});


test('unexpected continuation errors exhaust bounded retries and durably deliver once after restart',async()=>{
  const f=fixture();
  try{
    const id=await createIntake(f,'unexpected-exhaustion');await start(f,id,'first');await finish(f,await run(f,id),0);
    (f.project as any).continueTask=async()=>{throw new Error('test unexpected continuation error');};
    for(let attempt=1;attempt<=3;attempt++){
      await f.project.alarm();assert.equal(completion(f,id).continuation.tries,attempt);
      if(attempt<3){
        assert.equal(completion(f,id).continuation.state,'pending');assert.equal(completion(f,id).delivery,undefined);
        f.db().run("UPDATE intakes SET final_evidence_json=json_set(final_evidence_json,'$.completion.continuation.next_action_at',0) WHERE id=?",id);
      }
    }
    assert.equal(completion(f,id).continuation.state,'refused');assert.equal(completion(f,id).delivery.state,'pending');assert.equal(sagas(f,id).length,1);
    f.failMail(1);f.reopen();await f.project.alarm();assert.equal(f.mail.length,0);assert.equal(completion(f,id).delivery.tries,1);
    f.db().run("UPDATE intakes SET final_evidence_json=json_set(final_evidence_json,'$.completion.delivery.next_action_at',0) WHERE id=?",id);
    f.reopen();await f.project.alarm();assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/NOT finished/);assert.match(f.mail[0].body,/internal_error/);
    assert.equal(completion(f,id).delivery.state,'delivered');f.reopen();await f.project.alarm();assert.equal(f.mail.length,1);assert.equal(sagas(f,id).length,1);
  }finally{f.close();}
});


for(const stopOutcome of ['unconfirmed','timeout']){
  test('accepted STOP intent survives '+stopOutcome+' before the first qualified finish',async()=>{
    const f=fixture(),m=machineFixture(f);
    try{
      const id=await createIntake(f,'stop-before-finish-'+stopOutcome);await start(f,id,'first');const {binding,body}=await m.running(id),base='/api/sessions/'+binding.session_id;
      const stopped=await m.call(base+'/commands',{kind:'stop',key:'accepted-stop'},admin);assert.equal(stopped.status,201);const command=(await json(stopped)).command;
      if(stopOutcome==='unconfirmed'){
        assert.equal((await m.call(base+'/commands/'+command.id+'/ack',{epoch:binding.epoch,result:{stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed'}})).status,200);
      }else{
        m.db().run('UPDATE session_commands SET created_at=? WHERE id=?',Date.now()-60001,command.id);await m.machine.alarm();
      }
      const before=m.db().one('SELECT state,result_json FROM session_commands WHERE id=?',command.id);
      assert.equal(before.state,'failed');assert.equal(JSON.parse(before.result_json).stopped,false);assert.equal(JSON.parse(before.result_json).error,'stop_unconfirmed');
      assert.equal(m.db().one('SELECT state FROM sessions WHERE id=?',binding.session_id).state,'running');
      const response=await m.call(base+'/finish',body);assert(response.ok,await response.text());
      assert.equal(m.forwards.at(-1).stop_requested,true);assert.equal(completion(f,id).continuation,undefined);
      assert.equal(m.db().one('SELECT result_json FROM session_commands WHERE id=?',command.id).result_json,before.result_json,'finish must not turn a failed STOP into confirmation');
      await f.project.alarm();assert.equal(sagas(f,id).length,1);assert.equal(f.mail.length,1);assert.match(f.mail[0].body,/NOT finished/);
    }finally{m.close();f.close();}
  });
}

test('a STOP accepted for another session does not inhibit the current attempt',async()=>{
  const f=fixture(),m=machineFixture(f);
  try{
    const target=await createIntake(f,'stop-other-target');await start(f,target,'first');const current=await m.running(target);
    const other=await createIntake(f,'stop-other-session');await start(f,other,'first');const prior=await m.running(other);
    const response=await m.call('/api/sessions/'+prior.binding.session_id+'/commands',{kind:'stop',key:'other-attempt-stop'},admin);assert.equal(response.status,201);
    const finished=await m.call('/api/sessions/'+current.binding.session_id+'/finish',current.body);assert(finished.ok,await finished.text());
    assert.equal(m.forwards.at(-1).stop_requested,false);await f.project.alarm();assert.equal(sagas(f,target).length,2);assert.equal(sagas(f,other).length,1);
  }finally{m.close();f.close();}
});

test('a historical STOP epoch does not inhibit the current attempt',async()=>{
  const f=fixture(),m=machineFixture(f);
  try{
    const id=await createIntake(f,'stop-old-epoch');await start(f,id,'first');
    assert((await m.call('/api/heartbeat',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]})).ok);
    m.db().run('UPDATE machines SET next_epoch=1');
    const {binding,body}=await m.running(id),base='/api/sessions/'+binding.session_id;assert.equal(binding.epoch,2);
    const response=await m.call(base+'/commands',{kind:'stop',key:'historical-stop'},admin);assert.equal(response.status,201);const command=(await json(response)).command;
    // Represent retained command history from the previous Machine epoch.
    m.db().run('UPDATE session_commands SET epoch=? WHERE id=?',binding.epoch-1,command.id);
    const finished=await m.call(base+'/finish',body);assert(finished.ok,await finished.text());assert.equal(m.forwards.at(-1).stop_requested,false);
    await f.project.alarm();assert.equal(sagas(f,id).length,2);
  }finally{m.close();f.close();}
});
