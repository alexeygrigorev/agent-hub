import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ProjectDO} from '../src/project-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
function fixture(options:{modelCandidates?:any[];accountHosts?:Record<string,string[]>}={}){const sql=new DatabaseSync(':memory:');let alarm:any=null;const storageValues=new Map<string,unknown>(),ctx:any={id:{toString:()=> 'synthetic-project'},storage:{sql:{exec(q:string,...a:any[]){const p=sql.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){sql.exec('BEGIN');try{const r=fn();sql.exec('COMMIT');return r;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;},async get(key:string){return storageValues.get(key);},async put(key:string,value:unknown){storageValues.set(key,value);},async delete(key:string){return storageValues.delete(key);}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};
 const routes:any[]=[],machineCalls:string[]=[],accountCalls:string[]=[];
 const requestOf=(input:string|Request,init?:RequestInit)=>input instanceof Request?input:new Request(input,init);
 const CONTROL={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init),path=new URL(request.url).pathname;const value=request.method==='GET'?{}:await request.json();routes.push(value);if(path==='/_routing-policy')return Response.json({version:1,candidates:options.modelCandidates||[]});if(path==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});return Response.json({registered:true,accepted:true});}})};
 const MACHINES={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init);const path=new URL(request.url).pathname;machineCalls.push(path);if(path==='/_internal/routing')return Response.json({machine_id:'h1',engines:['codex','claude'],capabilities:['headless','interactive']});return Response.json(path.endsWith('/reserve')?{state:'staged',epoch:1}:{state:'offered'});}})};
 const unused={idFromName:(id:any)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})};
 const ACCOUNTS={idFromName:(id:any)=>id,get:(id:string)=>({fetch:async(input:string|Request)=>{const path=new URL(input instanceof Request?input.url:input).pathname;accountCalls.push(path);return Response.json({account:{enabled:true},quota:{remaining:id==='codex'?90:70},hosts:options.accountHosts?.[id]||['h1'],credential_usable:false});}})};
 const env:any={CONTROL,PROJECTS:unused,MACHINES,ACCOUNTS,MAILBOXES:unused};
 let project=new ProjectDO(ctx,env);const call=(p:string,m='GET',b?:unknown,a:any=admin)=>project.fetch(new Request('https://p.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{sql,get project(){return project;},restart:()=>project=new ProjectDO(ctx,env),routes,machineCalls,accountCalls,call,getAlarm:()=>alarm,storageValues};}
const body=async(r:Response)=>await r.json() as any;
async function intake(f:ReturnType<typeof fixture>,key='task'){const result=await f.call('/api/intakes?project=org/a','POST',{key,title:'Original task',text:'Original requested behavior',acceptance_criteria:['Actual result']});assert.equal(result.status,201);return(await body(result)).intake;}
test('model-less start falls back to another allowed model after an auth failure',async()=>{
 const candidates=[
  {id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
  {id:'claude-sonnet',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 ];
 const f=fixture({modelCandidates:candidates,accountHosts:{codex:['h1'],claude:['h2']}}),originalRandom=Math.random;Math.random=()=>0;
 try{
  const own=await intake(f,'auth-fallback'),start={key:'auth-fallback',workspace:'/tmp/synthetic-auth-fallback',purpose:'think'};
  const accepted=await f.call('/api/intakes/'+own.id+'/start','POST',start);assert.equal(accepted.status,201);
  const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
  assert.equal(JSON.parse(first.launch_spec_json).engine,'codex');
  assert.equal(JSON.parse(first.launch_spec_json).purpose,'think');
  assert.equal(first.state,'offered');
  const fallback=await f.call('/_internal/auth-fallback','POST',{operation_id:first.operation_id,
   attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,
   epoch:first.epoch,reason:'provider_auth_failed'});
  const fallbackBody=await body(fallback);assert.equal(fallback.status,200,JSON.stringify(fallbackBody));assert.equal(fallbackBody.fallback,true);
  const next=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any;
  assert.equal(next.attempt,1);assert.equal(next.state,'offered');
  assert.equal(next.machine_id,'h2');
  assert.equal((f.sql.prepare('SELECT assigned_machine_id FROM intakes WHERE id=?').get(own.id) as any).assigned_machine_id,'h2');
  assert.equal(JSON.parse(next.launch_spec_json).purpose,'think');
  assert.equal(JSON.parse(next.launch_spec_json).engine,'claude');assert.equal(JSON.parse(next.launch_spec_json).model,'sonnet');
 }finally{Math.random=originalRandom;}
});
test('intake edits preserve original ask and notes/history; archive search paginates retained data',async()=>{
 const f=fixture(),initial=await intake(f);
 const edited=await f.call('/api/intakes/'+initial.id,'PATCH',{key:'edit',expected_updated_at:initial.updated_at,title:'Revised title',text:'Revised behavior',priority:0,note:'Operator clarification',status:'archived'});assert.equal(edited.status,200);
 const current=(await body(await f.call('/api/intakes/'+initial.id))).intake;assert.equal(current.original_ask.text,'Original requested behavior');assert.equal(current.title,'Revised title');assert.equal(current.priority,0);assert.equal(current.status,'archived');
 const history=await body(await f.call('/api/intakes/'+initial.id+'/history'));assert(history.events.some((e:any)=>e.body.note==='Operator clarification'));
 const search=await body(await f.call('/api/intakes?project=org/a&q=Original'));assert.equal(search.items[0].id,initial.id);assert.equal(search.filtered_page,true);
 assert.equal((await f.call('/api/intakes/'+initial.id,'PATCH',{key:'foreign',note:'bad'},{id:'agent:x',kind:'agent',resource_id:'other',scopes:['intake:write']})).status,403);
 assert.equal((await f.call('/api/intakes/'+initial.id,'PATCH',{key:'stale',expected_updated_at:-1,priority:1})).status,409);
});
test('schedule adapter creates distinct child provenance and actual launch saga, replay creates no duplicate',async()=>{
 const f=fixture(),parent=await intake(f);
 const created=await body(await f.call('/api/schedules','POST',{key:'writer',intake_id:parent.id,kind:'writer',interval_ms:60000,overlap:'allow',payload:{start:{workspace:'/tmp/synthetic-workspace',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test only'}}}));
 (f.project as any).db.run('UPDATE work_schedules SET next_due=? WHERE id=?',Date.now()-1,created.schedule.id);
 await f.project.alarm();
 const rows=f.sql.prepare('SELECT * FROM intakes WHERE schedule_id=?').all(created.schedule.id) as any[];assert.equal(rows.length,1);assert.equal(rows[0].parent_intake_id,parent.id);assert.equal(rows[0].status,'queued');assert.equal(JSON.parse(rows[0].source_json).type,'schedule');assert(rows[0].session_id);
 assert(f.routes.some(r=>r.kind==='session'&&r.owner==='h1'&&r.resource_id==='org/a'&&r.bound_intake_id===rows[0].id));
 const before=f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get() as any;
 (f.project as any).db.run("UPDATE schedule_occurrences SET state='pending',next_attempt=?,result_json=NULL",Date.now()-1);await f.project.alarm();assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get() as any).n,before.n);assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM intakes WHERE schedule_id=?').get(created.schedule.id) as any).n,1);
 assert.equal((await f.call('/api/schedules','POST',{key:'bad-shell',intake_id:parent.id,kind:'root',interval_ms:60000,payload:{engine:'shell'}},{id:'agent:writer',kind:'agent',resource_id:'org/a',scopes:['schedule:write','intake:read','session:start']})).status,403);
});


test('worker append is exact-intake scoped, immutable key fenced and cannot alter acceptance or task state',async()=>{
 const f=fixture(),own=await intake(f,'own'),foreign=await intake(f,'foreign'),worker={id:'session:worker',kind:'agent',resource_id:own.id,scopes:['intake:read','intake:append','claim:write','claim:read']},append={key:'worker-note',note:'Bound progress',evidence:{result:'synthetic semantic proof'}};
 assert.equal((await f.call('/api/intakes/'+own.id+'/append','POST',append,worker)).status,201);
 assert.equal((await f.call('/api/intakes/'+own.id+'/append','POST',append,worker)).status,200);
 assert.equal((await f.call('/api/intakes/'+own.id+'/append','POST',{...append,note:'changed'},worker)).status,409);
 assert.equal((await f.call('/api/intakes/'+foreign.id+'/append','POST',append,worker)).status,403);
 assert.equal((await f.call('/api/intakes/'+own.id+'/append','POST',{...append,acceptance_criteria:['injected']},worker)).status,400);
 assert.equal((await f.call('/api/intakes/'+own.id,'PATCH',{key:'change-ac',acceptance_criteria:['injected']},worker)).status,403);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'privilege-escalation'},worker)).status,403);
 const after=(await body(await f.call('/api/intakes/'+own.id))).intake;assert.deepEqual(after.acceptance_criteria,['Actual result']);assert.equal(after.status,'open');
 const history=await body(await f.call('/api/intakes/'+own.id+'/history'));assert.equal(history.events.filter((e:any)=>e.kind==='worker_append').length,1);
});
test('event sequence cache publishes only committed state and reloads correctly after same-version restart',async()=>{
 const f=fixture(),own=await intake(f),project=f.project as any;
 assert.throws(()=>project.transaction(()=>{project.appendEvent(own.id,'rolled-back','human:admin','note',{note:'must disappear'},Date.now());throw Error('abort');}));
 await f.call('/api/intakes/'+own.id,'PATCH',{key:'after-rollback',note:'committed'});
 const history=await body(await f.call('/api/intakes/'+own.id+'/history'));assert.deepEqual(history.events.map((e:any)=>e.seq),[1,2]);assert.equal(history.events.some((e:any)=>e.body.note==='must disappear'),false);
 f.restart();await f.call('/api/intakes/'+own.id,'PATCH',{key:'after-restart',note:'persisted restart'});const resumed=await body(await f.call('/api/intakes/'+own.id+'/history'));assert.deepEqual(resumed.events.map((e:any)=>e.seq),[1,2,3]);
});
test('failed task retry requires native exit and release evidence, preserves old key and creates a new immutable session',async()=>{
 const f=fixture(),own=await intake(f,'retry'),start={key:'first',workspace:'/tmp/synthetic-retry',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status<300,true);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,db=(f.project as any).db,native='11111111-1111-4111-8111-111111111111';
 // Machine admission is synthetic in this unit fixture; actual public native gates cover the host seam.
 db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
 const binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:native};
 assert.equal((await f.call('/_internal/started','POST',binding)).status,201);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,exit_code:1,native_exit_confirmed:false,evidence:{result:'failed'}})).status,400);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'premature'})).status,409);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,exit_code:1,native_exit_confirmed:true,evidence:{result:'failed'}})).status,200);
 const replay=await f.call('/api/intakes/'+own.id+'/start','POST',start);assert.equal(replay.status<300,true);assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get() as any).n,1);
 const retried=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'second'});assert.equal(retried.status<300,true);
 const current=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.notEqual(current.session_id,first.session_id);assert.equal(current.status,'queued');assert.equal(current.final_evidence_json,null);
 assert.equal((f.sql.prepare('SELECT state FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any).state,'complete');
 assert.equal((await f.call('/_internal/finish','POST',{...binding,exit_code:1,native_exit_confirmed:true,evidence:{result:'failed'}})).status,200);
 assert.equal((f.sql.prepare('SELECT session_id FROM intakes WHERE id=?').get(own.id) as any).session_id,current.session_id);
});
test('terminal stop releases an intake for retry and no-native launch timeout is a visible failure',async()=>{
 const f=fixture(),stoppedIntake=await intake(f,'stop-retry'),start={key:'stop-first',workspace:'/tmp/synthetic-stop-retry',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+stoppedIntake.id+'/start','POST',start)).status,201);
 let saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(stoppedIntake.id) as any;
 f.sql.prepare("UPDATE dispatch_sagas SET state='claimed' WHERE operation_id=?").run(saga.operation_id);
 let result=await f.call('/_internal/stopped','POST',{operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,reason:'stop_confirmed',observed_at:Date.now()});
 assert.equal(result.status,200);assert.equal((await body(result)).stopped,true);
 let intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(stoppedIntake.id) as any;
 assert.equal(intakeRow.status,'failed');assert.equal(JSON.parse(intakeRow.final_evidence_json).stopped,true);
 assert.equal((await f.call('/api/intakes/'+stoppedIntake.id+'/start','POST',{...start,key:'stop-retry-again'})).status,201);
 const timeoutIntake=await intake(f,'launch-timeout'),timeoutStart={key:'timeout-first',workspace:'/tmp/synthetic-launch-timeout',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+timeoutIntake.id+'/start','POST',timeoutStart)).status,201);
 saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(timeoutIntake.id) as any;
 f.sql.prepare("UPDATE dispatch_sagas SET state='claimed' WHERE operation_id=?").run(saga.operation_id);
 result=await f.call('/_internal/reaped','POST',{operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:null,reason:'launch_timeout_no_native',launch_failed:true,observed_at:Date.now()});
 assert.equal(result.status,200);assert.equal((await body(result)).failed,true);
 intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(timeoutIntake.id) as any;
 const proof=JSON.parse(intakeRow.final_evidence_json);
 assert.equal(intakeRow.status,'failed');assert.equal(proof.launch_failed,true);assert.equal(proof.native_id_absent,true);
 assert.equal(proof.reaped,true);assert.equal(proof.session_id,saga.session_id);assert.equal(proof.reason,'launch_timeout_no_native');
 assert.equal(proof.native_id,null);assert.equal(Number.isSafeInteger(proof.observed_at),true);assert.equal(proof.stopped,undefined);
 const history=await body(await f.call('/api/intakes/'+timeoutIntake.id+'/history'));
 assert(history.events.some((event:any)=>event.kind==='session_launch_failed'&&event.body.reason==='launch_timeout_no_native'));
});
test('pre-claim host launch refusal fails clearly and is not silently re-offered',async()=>{
 const f=fixture(),own=await intake(f,'host-launch-refused'),start={key:'host-refused-first',workspace:'relay',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 assert.equal(saga.state,'offered');assert.equal(saga.native_id,null);
 const response=await f.call('/_internal/reaped','POST',{operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:null,reason:'host_launch_refused',
  failure_reason:'workspace_label_unavailable',launch_failed:true,observed_at:Date.now()});
 assert.equal(response.status,200);const receipt=await body(response);
 assert.equal(receipt.failed,true);assert.equal(receipt.reason,'host_launch_refused');
 const intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any,proof=JSON.parse(intakeRow.final_evidence_json);
 assert.equal(intakeRow.status,'failed');assert.equal(proof.launch_failed,true);assert.equal(proof.native_id_absent,true);
 assert.equal(proof.failure_reason,'workspace_label_unavailable');assert.equal(proof.session_id,saga.session_id);
 const history=await body(await f.call('/api/intakes/'+own.id+'/history'));
 assert(history.events.some((event:any)=>event.kind==='session_launch_failed'&&event.body.failure_reason==='workspace_label_unavailable'));
 const retry=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'host-refused-retry'});
 assert.equal(retry.status,409);assert.equal((await body(retry)).error,'retry_requires_qualified_failure');
 assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any).n,1);
});
test('stop receipt for an expired offer cancels its already-requeued retry',async()=>{
 const f=fixture(),own=await intake(f,'expired-offer-stop'),start={key:'expired-offer-stop',workspace:'/tmp/synthetic-expired-stop',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 assert.equal(first.state,'offered');assert.equal(first.attempt,0);
 const expiredAt=Date.now()-1000,observedAt=expiredAt+1;
 const requeued=await f.call('/_internal/requeue','POST',{operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,
  session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,proof_kind:'offer-expired-unclaimed',
  offer_expires_at:expiredAt,observed_at:observedAt});
 assert.equal(requeued.status,200,JSON.stringify(await requeued.clone().json()));
 let saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any;
 assert.equal(saga.state,'pending_machine');assert.equal(saga.attempt,1);assert.equal(saga.attempt_operation_id,`${first.operation_id}:1`);
 assert.equal(saga.epoch,first.epoch);
 const machineCallsBeforeStop=f.machineCalls.length;
 const stop={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,
  machine_id:first.machine_id,epoch:first.epoch,native_id:null,reason:'stop_confirmed',observed_at:Date.now()};
 const response=await f.call('/_internal/stopped','POST',stop),result=await body(response);
 assert.equal(response.status,200,JSON.stringify(result));assert.equal(result.stopped,true);
 saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any;
 assert.equal(saga.state,'complete');assert.equal(saga.last_reason,'stop_confirmed');
 const intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
 assert.equal(intakeRow.status,'failed');assert.equal(JSON.parse(intakeRow.final_evidence_json).stopped,true);
 const replay=await f.call('/_internal/stopped','POST',stop);
 assert.equal(replay.status,200);assert.equal((await body(replay)).duplicate,true);
 await f.project.alarm();
 assert.equal(f.machineCalls.length,machineCallsBeforeStop);
 assert.equal(f.getAlarm(),null);
});
test('session admission checks the current saga without quota or account-capacity reads',async()=>{
 const f=fixture(),own=await intake(f,'admission-binding'),start={key:'admission-binding',workspace:'/tmp/synthetic-admission',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert([201,202].includes((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status));
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 const admission=await f.call('/_internal/admission','POST',{operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch});
 assert.equal(admission.status,200);const view=await body(admission);assert.equal(view.launch_allowed,true);assert.equal(view.model,'shell');
 assert.deepEqual(f.accountCalls,[],'launch admission must not read quota or reserve account capacity');
 assert.equal((await f.call('/_internal/admission','POST',{operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch+1})).status,409);
});
test('qualified admitted launch failure binds actual native exit without fabricating started or a nonzero exit',async()=>{
 const f=fixture(),own=await intake(f,'launch-failed'),start={key:'failed-launch',workspace:'/tmp/synthetic-abort',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 await f.call('/api/intakes/'+own.id+'/start','POST',start);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,project=f.project as any;
 project.db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
 const binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:'33333333-3333-4333-8333-333333333333',exit_code:0,native_exit_confirmed:true,launch_failed:true};
 const evidence={source:'aplexer_status',containment_empty:true,native_identity:{id:binding.native_id,tag:'task-hub2-'+first.session_id,workspace:start.workspace},native_exit:{code:0}};
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence:{...evidence,containment_empty:false}})).status,400);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence:{...evidence,native_identity:{...evidence.native_identity,workspace:'/foreign'}}})).status,400);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,launch_failed:false,evidence})).status,404);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence})).status,200);
 const failed=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.equal(failed.status,'failed');assert.equal(JSON.parse(failed.final_evidence_json).exit_code,0);assert.equal(JSON.parse(failed.final_evidence_json).launch_failed,true);
 assert.equal((f.sql.prepare("SELECT COUNT(*) AS n FROM intake_events WHERE task_id=? AND kind='native_session_started'").get(own.id) as any).n,0);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence})).status,200);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence:{...evidence,extra:'changed'}})).status,409);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,launch_failed:false,evidence})).status,409);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'after-abort'})).status<300,true);
 assert.equal((await f.call('/_internal/finish','POST',{...binding,evidence})).status,200);
 assert.equal((f.sql.prepare('SELECT status FROM intakes WHERE id=?').get(own.id) as any).status,'queued');
});
test('late successful native exit is failed, replay-bound, and eligible for retry',async()=>{
 const f=fixture(),own=await intake(f,'late-zero-exit'),start={key:'deadline-start',workspace:'/tmp/synthetic-deadline',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 await f.call('/api/intakes/'+own.id+'/start','POST',start);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,project=f.project as any;
 project.db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
 const native='44444444-4444-4444-8444-444444444444',binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:native};
 assert.equal((await f.call('/_internal/started','POST',binding)).status,201);
 const evidence={source:'aplexer_status',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+first.session_id,workspace:start.workspace},native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:Date.now()-1,exceeded:true}};
 const receipt={...binding,exit_code:0,native_exit_confirmed:true,evidence};
 assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
 const failed=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.equal(failed.status,'failed');assert.equal(JSON.parse(failed.final_evidence_json).exit_code,0);assert.equal(JSON.parse(failed.final_evidence_json).native_deadline.exceeded,true);
 assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
 assert.equal((await f.call('/_internal/finish','POST',{...receipt,evidence:{...evidence,native_deadline:{...evidence.native_deadline,exceeded:false}}})).status,409);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'after-deadline-failure'})).status<300,true);
 const retried=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.notEqual(retried.session_id,first.session_id);assert.equal(retried.status,'queued');assert.equal(retried.final_evidence_json,null);
});
test('signal-only native exit preserves raw tombstone and derives a negative failed exit code',async()=>{
 const f=fixture(),own=await intake(f,'signal-exit'),start={key:'signal-start',workspace:'/tmp/synthetic-signal',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 await f.call('/api/intakes/'+own.id+'/start','POST',start);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,project=f.project as any;
 project.db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
 const native='55555555-5555-4555-8555-555555555555',binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:native};
 assert.equal((await f.call('/_internal/started','POST',binding)).status,201);
 const now=Date.now(),evidence={source:'aplexer_retirement',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+first.session_id,workspace:start.workspace},native_deadline:{deadline_ms:now+60_000,exceeded:false},native_exit:{code:null,signal:1,exited_at_ms:now+1}};
 const receipt={...binding,exit_code:-1,native_exit_confirmed:true,evidence};
 assert.equal((await f.call('/_internal/finish','POST',{...receipt,exit_code:0})).status,400,'signal evidence cannot become exit zero');
 assert.equal((await f.call('/_internal/finish','POST',{...receipt,evidence:{...evidence,native_exit:{...evidence.native_exit,signal:0}}})).status,400,'invalid signal cannot normalize');
 assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
 const failed=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any,proof=JSON.parse(failed.final_evidence_json);
 assert.equal(failed.status,'failed');assert.equal(proof.exit_code,-1);assert.deepEqual(proof.native_exit,evidence.native_exit);
 assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200,'exact signal receipt replay');
 assert.equal((await f.call('/_internal/finish','POST',{...receipt,evidence:{...evidence,native_exit:{...evidence.native_exit,signal:2}}})).status,400,'changed raw signal cannot reuse normalized code');
});
test('no-native launch timeout permits retry only with a proof bound to the completed prior saga',async()=>{
 const f=fixture(),own=await intake(f,'timeout-retry-proof'),start={key:'timeout-proof-first',workspace:'/tmp/synthetic-timeout-proof',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const prior=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 f.sql.prepare("UPDATE dispatch_sagas SET state='claimed' WHERE operation_id=?").run(prior.operation_id);
 const failed=await f.call('/_internal/reaped','POST',{operation_id:prior.operation_id,attempt_operation_id:prior.attempt_operation_id,
  session_id:prior.session_id,machine_id:prior.machine_id,epoch:prior.epoch,native_id:null,reason:'launch_timeout_no_native',
  launch_failed:true,observed_at:Date.now()});
 assert.equal(failed.status,200,JSON.stringify(await failed.clone().json()));
 const intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any,proof=JSON.parse(intakeRow.final_evidence_json);
 assert.equal(intakeRow.status,'failed');assert.equal(prior.native_id,null);
 for(const [field,value] of Object.entries({operation_id:'foreign-operation',attempt_operation_id:'foreign-attempt',session_id:'foreign-session',
  machine_id:'foreign-machine',epoch:prior.epoch+1,reason:'dead',launch_failed:false,native_id_absent:false,reaped:false,native_id:'foreign-native'})){
  f.sql.prepare('UPDATE intakes SET final_evidence_json=? WHERE id=?').run(JSON.stringify({...proof,[field]:value}),own.id);
  const refused=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:`bad-timeout-proof-${field}`});
  const refusedBody=await body(refused);assert.equal(refused.status,409,`${field}: ${JSON.stringify(refusedBody)}`);
  assert.equal(refusedBody.error,'retry_requires_qualified_failure');
 }
 f.sql.prepare("UPDATE dispatch_sagas SET state='needs_attention' WHERE operation_id=?").run(prior.operation_id);
 f.sql.prepare('UPDATE intakes SET final_evidence_json=? WHERE id=?').run(JSON.stringify(proof),own.id);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'incomplete-timeout-saga'})).status,409);
 f.sql.prepare("UPDATE dispatch_sagas SET state='complete',last_reason='retry_bookkeeping' WHERE operation_id=?").run(prior.operation_id);
 const retried=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'timeout-proof-retry'});
 assert.equal(retried.status,201,JSON.stringify(await retried.clone().json()));
 const rows=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=? ORDER BY created_at,operation_id').all(own.id) as any[];
 const priorAfterRetry=rows.find(row=>row.operation_id===prior.operation_id),retrySaga=rows.find(row=>row.operation_id!==prior.operation_id);
 assert.equal(rows.length,2);assert.equal(priorAfterRetry.state,'complete');
 assert.equal(priorAfterRetry.last_reason,'retry_bookkeeping');assert.equal(priorAfterRetry.native_id,null);assert.notEqual(retrySaga.session_id,prior.session_id);
 const history=await body(await f.call('/api/intakes/'+own.id+'/history'));
 assert(history.events.some((event:any)=>event.kind==='session_launch_failed'&&event.body.reason==='launch_timeout_no_native'));
});


test('start purpose is validated and bound to launch and idempotency',async()=>{
 const f=fixture(),own=await intake(f,'purpose'),start={key:'purpose-start',workspace:'/tmp/synthetic-purpose',engine:'shell',machine_id:'h1',override_reason:'Synthetic purpose test'};
 for(const purpose of [null,'Review',false]){
  const refused=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,purpose});
  assert.equal(refused.status,400);assert.equal((await body(refused)).error,'invalid_purpose');
 }
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 assert.equal(JSON.parse(saga.launch_spec_json).purpose,'implement');
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,purpose:'implement'})).status,200);
 const conflict=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,purpose:'review'});
 assert.equal(conflict.status,409);assert.equal((await body(conflict)).error,'idempotency_conflict');
});

test('project starts enforce Astra purpose even through non-Astra policy ids',async()=>{
 const astra={id:'review-route',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex',weight:1,capabilities:['review-only'],enabled:true};
 for(const purpose of [undefined,'fix','implement','review','think']){
  const f=fixture({modelCandidates:[astra]}),own=await intake(f,'astra-purpose');
  const response=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'astra-purpose',workspace:'/tmp/synthetic-purpose',model:'review-route',override_reason:'Synthetic review test',purpose});
  if(purpose==='review'||purpose==='think'){
   assert.equal(response.status,201,JSON.stringify(await body(response)));
   const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
   assert.equal(JSON.parse(saga.launch_spec_json).purpose,purpose);
  }else{
   assert.equal(response.status,400);assert.equal((await body(response)).error,'astra_not_for_implementation');
  }
 }
});
