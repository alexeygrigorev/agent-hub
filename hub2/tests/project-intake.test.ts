import {ControlDO} from '../src/control-do';
import {MachineDO} from '../src/machine-do';
import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ProjectDO} from '../src/project-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
function fixture(options:{modelCandidates?:any[];accountHosts?:Record<string,string[]>;connected?:boolean;lostReleaseFailures?:number;machineFetch?:(request:Request)=>Promise<Response>;lostDrawResponse?:boolean;schemaMode?:string;denyCapacity?:boolean;lostCapResponse?:boolean}={}){const sql=new DatabaseSync(':memory:');let alarm:any=null;const storageValues=new Map<string,unknown>(),ctx:any={id:{toString:()=> 'synthetic-project'},storage:{sql:{exec(q:string,...a:any[]){const p=sql.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){sql.exec('BEGIN');try{const r=fn();sql.exec('COMMIT');return r;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;},async get(key:string){return storageValues.get(key);},async put(key:string,value:unknown){storageValues.set(key,value);},async delete(key:string){return storageValues.delete(key);}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};
 const collectorSql=new DatabaseSync(':memory:');
 const collectorContext:any={...ctx,storage:{...ctx.storage,sql:{exec(q:string,...a:any[]){const statement=collectorSql.prepare(q);const rows=statement.columns().length?statement.all(...a):(statement.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){collectorSql.exec('BEGIN');try{const result=fn();collectorSql.exec('COMMIT');return result;}catch(error){collectorSql.exec('ROLLBACK');throw error;}}}};
 let control:ControlDO;let loseDraw=options.lostDrawResponse,loseCap=options.lostCapResponse;
 const routes:any[]=[],machineCalls:string[]=[],accountCalls:string[]=[],capReleases:any[]=[];
 let remainingReleaseFailures=options.lostReleaseFailures??0;
 const requestOf=(input:string|Request,init?:RequestInit)=>input instanceof Request?input:new Request(input,init);
 const CONTROL={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init),path=new URL(request.url).pathname;const value=request.method==='GET'?{}:await request.clone().json();routes.push(value);if(path==='/_internal/session-cap/check'&&options.lostCapResponse){const response=await control.fetch(request);if(loseCap&&response.ok){loseCap=false;throw new Error('synthetic_crash_after_capacity_reservation');}return response;}if(path==='/_internal/session-cap/check'&&options.denyCapacity)return Response.json({error:'active_session_limit'},{status:429});if(path==='/_internal/picker/draw'){const response=await control.fetch(request);if(loseDraw&&response.ok){loseDraw=false;throw new Error('synthetic_lost_draw_response');}return response;}if(path==='/_internal/session-cap/release'){capReleases.push(value);if(remainingReleaseFailures>0){remainingReleaseFailures--;return Response.json({error:'unavailable'},{status:503});}}if(path==='/_routing-policy')return Response.json({version:1,candidates:options.modelCandidates||[]});if(path==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});return Response.json({registered:true,accepted:true});}})};
 const MACHINES={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init);const path=new URL(request.url).pathname;machineCalls.push(path);if(options.machineFetch&&path!=='/_internal/routing')return options.machineFetch(request);if(path==='/_internal/routing')return Response.json({machine_id:'h1',connected:options.connected??true,engines:['codex','claude'],capabilities:['headless','interactive']});return Response.json(path.endsWith('/reserve')?{state:'staged',epoch:1}:{state:'offered'});}})};
 const unused={idFromName:(id:any)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})};
 const ACCOUNTS={idFromName:(id:any)=>id,get:(id:string)=>({fetch:async(input:string|Request)=>{const path=new URL(input instanceof Request?input.url:input).pathname;accountCalls.push(path);return Response.json({account:{enabled:true},quota:{remaining:id==='codex'?90:70},hosts:options.accountHosts?.[id]||['h1'],credential_usable:false});}})};
 const env:any={CONTROL,PROJECTS:unused,MACHINES,ACCOUNTS,MAILBOXES:unused,HUB2_SCHEMA_MIGRATIONS:options.schemaMode};
 control=new ControlDO(collectorContext,env);
 let project=new ProjectDO(ctx,env);const call=(p:string,m='GET',b?:unknown,a:any=admin)=>project.fetch(new Request('https://p.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{sql,collectorSql,get project(){return project;},restart:()=>project=new ProjectDO(ctx,env),routes,machineCalls,accountCalls,capReleases,call,getAlarm:()=>alarm,storageValues};}
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
test('an exact lost receipt closes capacity and records loss without claiming stop',async()=>{
 const f=fixture({lostReleaseFailures:1}),task=await intake(f,'host-inventory-loss');
 const start={key:'start-before-loss',workspace:'/tmp/synthetic-loss',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+task.id+'/start','POST',start)).status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(task.id) as any;
 f.sql.prepare("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?").run(saga.operation_id);
 const nativeId='native-historical-id';
 const receipt={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:nativeId,
  reason:'host_inventory_stale',lost:true,observed_at:Date.now(),inventory_observed_at:Date.now()-301_000};
 assert.equal((await f.call('/_internal/reaped','POST',{...receipt,epoch:receipt.epoch+1})).status,409);
 assert.equal((await f.call('/_internal/reaped','POST',{...receipt,inventory_observed_at:Date.now()})).status,400);
 const result=await f.call('/_internal/reaped','POST',receipt);
 assert.equal(result.status,503);
 assert.equal((await body(result)).error,'session_cap_release_pending');
 f.restart();
 const duplicate=await f.call('/_internal/reaped','POST',receipt);
 assert.equal(duplicate.status,200);
 assert.deepEqual(await body(duplicate),{lost:true,duplicate:true,session_id:saga.session_id});
 const delayedStart=await f.call('/_internal/started','POST',receipt);
 assert.deepEqual(await body(delayedStart),{started:false,lost:true,duplicate:true});
 const delayedClaim=await f.call('/_internal/claim','POST',receipt);
 assert.deepEqual(await body(delayedClaim),{claimed:false,lost:true,duplicate:true});
 const finished=f.sql.prepare('SELECT status,final_evidence_json FROM intakes WHERE id=?').get(task.id) as any;
 assert.equal(finished.status,'failed');
 assert.equal(JSON.parse(finished.final_evidence_json).lost,true);
 assert.equal(JSON.parse(finished.final_evidence_json).stopped,undefined);
 const history=await body(await f.call('/api/intakes/'+task.id+'/history'));
 assert.equal(history.events.filter((event:any)=>event.kind==='session_lost').length,1);
 assert.deepEqual(f.capReleases,Array.from({length:2},()=>({operation_id:saga.operation_id,session_id:saga.session_id})));
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
 assert.equal(retry.status,201,JSON.stringify(await retry.clone().json()));
 assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any).n,2);
});
test('an expired unclaimed offer terminates instead of scheduling an unbounded retry',async()=>{
 const f=fixture(),own=await intake(f,'expired-offer-stop'),start={key:'expired-offer-stop',workspace:'/tmp/synthetic-expired-stop',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 const expiredAt=Date.now()-1000,proof={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,
  session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,proof_kind:'offer-expired-unclaimed',
  offer_expires_at:expiredAt,observed_at:expiredAt+1};
 const result=await f.call('/_internal/requeue','POST',proof);
 assert.equal(result.status,200);assert.equal((await body(result)).retry_scheduled,false);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any;
 assert.equal(saga.state,'complete');assert.equal(saga.attempt,0);assert.equal(saga.last_reason,'host_no_response');
 const intakeRow=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
 assert.equal(intakeRow.status,'failed');assert.equal(JSON.parse(intakeRow.final_evidence_json).reason,'host_no_response');
 const calls=f.machineCalls.length;
 await f.project.alarm();assert.equal(f.machineCalls.length,calls);assert.equal(f.getAlarm(),null);
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
test('failed intake can retry without qualified failure proof once prior saga is complete',async()=>{
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
 f.sql.prepare('UPDATE intakes SET final_evidence_json=? WHERE id=?').run('{}',own.id);
 f.sql.prepare("UPDATE dispatch_sagas SET state='needs_attention' WHERE operation_id=?").run(prior.operation_id);
 const activeRetry=await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'incomplete-prior'});
 assert.equal(activeRetry.status,409);assert.equal((await body(activeRetry)).error,'intake_already_assigned');
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

test('operator shell start needs no reason but requires a connected host',async()=>{
 for(const connected of [true,false])for(const explicitMachine of [true,false]){
  const f=fixture({connected}),own=await intake(f,'shell-connected');
  const result=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'shell-connected',workspace:'relay',engine:'shell',...(explicitMachine?{machine_id:'h1'}:{})});
  assert.equal(result.status,connected?201:503,JSON.stringify(await result.clone().json()));
  if(!connected){const resultBody=await body(result);assert.equal(resultBody.error,'no_connected_host');assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get().n,0);}
 }
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
test('a superseded intake closes with its replacement and note; a busy intake names the session to stop',async()=>{
 const f=fixture(),old=await intake(f,'superseded-old');
 const closed=await f.call('/api/intakes/'+old.id,'POST',{key:'supersede-1',status:'cancelled',superseded_by:'abf8e6d2-00f5-49b8-89df-d4eeaef444cf',reason:'start failed, work moved',note:'worktree kept'});
 assert.equal(closed.status,200,JSON.stringify(await closed.clone().json()));
 assert.equal((await body(await f.call('/api/intakes/'+old.id))).intake.status,'archived');
 const history=await body(await f.call('/api/intakes/'+old.id+'/history'));
 assert(history.events.some((e:any)=>e.body.note==='superseded by abf8e6d2-00f5-49b8-89df-d4eeaef444cf: start failed, work moved: worktree kept'));
 assert.equal((await f.call('/api/intakes/'+old.id,'POST',{key:'supersede-bad',status:'cancelled',superseded_by:'../x'})).status,400);
 const busy=await intake(f,'busy-intake'),start={key:'busy-first',workspace:'/tmp/synthetic-busy',engine:'shell',machine_id:'h1'};
 assert.equal((await f.call('/api/intakes/'+busy.id+'/start','POST',start)).status,201);
 const saga=f.sql.prepare('SELECT session_id FROM dispatch_sagas WHERE intake_id=?').get(busy.id) as any;
 const again=await f.call('/api/intakes/'+busy.id+'/start','POST',{...start,key:'busy-second'}),refusal=await body(again);
 assert.equal(again.status,409);assert.equal(refusal.error,'intake_already_assigned');
 assert.match(refusal.detail,new RegExp('hub sessions stop '+saga.session_id));
});
test('project intake pages clamp to 50 rows and follow the cursor under either spelling',async()=>{
 const f=fixture();for(let i=0;i<53;i++)await intake(f,'page-'+String(i).padStart(2,'0'));
 const first=await body(await f.call('/api/intakes?project=org/a&limit=100'));
 assert.equal(first.items.length,50);assert.ok(first.next);
 const viaCursor=await body(await f.call('/api/intakes?project=org/a&limit=100&cursor='+encodeURIComponent(first.next)));
 const viaAfter=await body(await f.call('/api/intakes?project=org/a&limit=100&after='+encodeURIComponent(first.next)));
 assert.equal(viaCursor.items.length,3);assert.equal(viaCursor.next,null);
 assert.deepEqual(viaAfter.items.map((i:any)=>i.id),viaCursor.items.map((i:any)=>i.id));
 assert.equal(new Set([...first.items,...viaCursor.items].map((i:any)=>i.id)).size,53);
 assert.equal((await f.call('/api/intakes?project=org/a&limit=101')).status,400);
});
test('discard archives only an untouched open intake and is fenced against any work',async()=>{
 const f=fixture(),history=async(id:string)=>(await body(await f.call('/api/intakes/'+id+'/history'))).events,status=async(id:string)=>(await body(await f.call('/api/intakes/'+id))).intake.status;
 const unused=await intake(f,'discard-unused');
 const done=await f.call('/api/intakes/'+unused.id+'/discard','POST',{key:'discard-1',reason:'start refused: invalid_model'});
 assert.equal(done.status,200);assert.deepEqual(await body(done),{discarded:true,intake:{id:unused.id,status:'archived'}});
 assert.equal(await status(unused.id),'archived');
 assert((await history(unused.id)).some((e:any)=>e.kind==='intake_updated'&&e.body.note==='start refused: invalid_model'&&e.body.status==='archived'));
 const replay=await body(await f.call('/api/intakes/'+unused.id+'/discard','POST',{key:'discard-1',reason:'start refused: invalid_model'}));
 assert.equal(replay.discarded,true);assert.equal(replay.duplicate,true);
 assert.equal((await history(unused.id)).filter((e:any)=>e.kind==='intake_updated').length,1);
 assert.equal((await f.call('/api/intakes/'+unused.id+'/discard','POST',{key:'discard-2',reason:'again'})).status,409,'an archived intake is not open');

 const started=await intake(f,'discard-started'),start={key:'discard-start',workspace:'/tmp/synthetic-discard',engine:'shell',machine_id:'h1'};
 assert.equal((await f.call('/api/intakes/'+started.id+'/start','POST',start)).status,201);
 const busy=await f.call('/api/intakes/'+started.id+'/discard','POST',{key:'discard-busy',reason:'start refused: x'});
 assert.equal(busy.status,409);assert.equal((await body(busy)).error,'intake_in_use');assert.equal(await status(started.id),'queued');

 const reviewed=await intake(f,'discard-reviewed');
 f.sql.prepare("UPDATE intakes SET status='completed-awaiting-review' WHERE id=?").run(reviewed.id);
 assert.equal((await f.call('/api/intakes/'+reviewed.id+'/discard','POST',{key:'discard-reviewed',reason:'start refused: x'})).status,409);
 assert.equal(await status(reviewed.id),'completed-awaiting-review','finished work is never accepted or archived by a discard');

 const failed=await intake(f,'discard-failed');
 f.sql.prepare("UPDATE intakes SET status='failed',session_id='old-session',assigned_machine_id='h1' WHERE id=?").run(failed.id);
 assert.equal((await f.call('/api/intakes/'+failed.id+'/discard','POST',{key:'discard-failed',reason:'start refused: x'})).status,409);
 assert.equal(await status(failed.id),'failed');

 const reopened=await intake(f,'discard-reopened');
 f.sql.prepare('UPDATE intakes SET session_id=? WHERE id=?').run('earlier-session',reopened.id);
 assert.equal((await f.call('/api/intakes/'+reopened.id+'/discard','POST',{key:'discard-reopened',reason:'start refused: x'})).status,409,'open again after earlier work');

 const noted=await intake(f,'discard-noted');
 assert.equal((await f.call('/api/intakes/'+noted.id+'/append','POST',{key:'note-1',note:'operator context'})).status,201);
 assert.equal((await f.call('/api/intakes/'+noted.id+'/discard','POST',{key:'discard-noted',reason:'start refused: x'})).status,409);
 assert.equal(await status(noted.id),'open');

 const fresh=await intake(f,'discard-authz');
 assert.equal((await f.call('/api/intakes/'+fresh.id+'/discard','POST',{key:'discard-foreign',reason:'x'},{id:'agent:x',kind:'agent',resource_id:'other',scopes:['intake:write']})).status,403);
 assert.equal((await f.call('/api/intakes/'+fresh.id+'/discard','POST',{key:'discard-empty',reason:' '})).status,400);
 assert.equal((await f.call('/api/intakes/missing/discard','POST',{key:'discard-missing',reason:'x'})).status,404);
 assert.equal(await status(fresh.id),'open');
});
test('a start racing a discard leaves exactly one outcome: used intakes stay, discarded intakes refuse the start',async()=>{
 const f=fixture(),start={workspace:'/tmp/synthetic-race',engine:'shell',machine_id:'h1'};
 const first=await intake(f,'race-start-first');
 assert.equal((await f.call('/api/intakes/'+first.id+'/start','POST',{...start,key:'race-start-1'})).status,201);
 assert.equal((await f.call('/api/intakes/'+first.id+'/discard','POST',{key:'race-discard-1',reason:'start refused: x'})).status,409);
 assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas WHERE intake_id=?').get(first.id) as any).n,1);
 const second=await intake(f,'race-discard-first');
 assert.equal((await f.call('/api/intakes/'+second.id+'/discard','POST',{key:'race-discard-2',reason:'start refused: x'})).status,200);
 const late=await f.call('/api/intakes/'+second.id+'/start','POST',{...start,key:'race-start-2'});
 assert.equal(late.status,409);assert.equal((await body(late)).error,'intake_already_assigned');
 assert.equal((f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas WHERE intake_id=?').get(second.id) as any).n,0);
});


test('late qualified exit preserves timeout failure and cannot overwrite a retry intake',async()=>{
 for(const retryBeforeReceipt of [false,true]){
  const f=fixture(),own=await intake(f,'late-abort-'+retryBeforeReceipt);
  const start={key:'late-abort',workspace:'/tmp/synthetic-late-abort',engine:'shell',machine_id:'h1'};
  await f.call('/api/intakes/'+own.id+'/start','POST',start);
  const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
  (f.project as any).db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
  const binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch};
  assert.equal((await f.call('/_internal/reaped','POST',{...binding,native_id:null,reason:'launch_timeout_no_native',launch_failed:true,observed_at:Date.now()})).status,200);
  if(retryBeforeReceipt)assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'late-abort-retry'})).status,201);
  const native='33333333-3333-4333-8333-333333333333';
  const evidence={source:'aplexer_status',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+first.session_id,workspace:start.workspace},native_exit:{code:0}};
  const receipt={...binding,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence};
  assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
  assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
  const current=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
  assert.equal(current.status,retryBeforeReceipt?'queued':'failed');
  if(!retryBeforeReceipt){
   const proof=JSON.parse(current.final_evidence_json);
   assert.equal(proof.reason,'launch_timeout_no_native');assert.equal(proof.exit_code,0);
   assert.equal(proof.native_exit_confirmed,true);assert.equal(proof.native_id_absent,false);
  }else assert.notEqual(current.session_id,first.session_id);
  assert.equal((await f.call('/_internal/finish','POST',{...receipt,evidence:{...evidence,changed:true}})).status,409);
 }
});


test('late exit forwards the timeout before completion when the first timeout transport fails',async()=>{
 const database=new DatabaseSync(':memory:');
 let alarm:number|null=null;
 const machineContext:any={
  id:{toString:()=> 'h1'},
  storage:{
   sql:{exec(query:string,...args:any[]){
    const statement=database.prepare(query);
    const rows=statement.columns().length?statement.all(...args):(statement.run(...args),[]);
    return {toArray:()=>rows};
   }},
   transactionSync(callback:()=>unknown){
    database.exec('BEGIN');
    try{const result=callback();database.exec('COMMIT');return result;}
    catch(error){database.exec('ROLLBACK');throw error;}
   },
   async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;},
  },
  blockConcurrencyWhile:(callback:()=>unknown)=>Promise.resolve(callback()),getWebSockets:()=>[],
 };
 let machine:MachineDO;
 const f=fixture({machineFetch:request=>machine.fetch(request)});
 let timeoutFailures=1;
 const delivered:string[]=[];
 const environment:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true,revoked:true})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   const path=new URL(request.url).pathname;
   if(path==='/_internal/reaped'&&timeoutFailures-->0){delivered.push('timeout-failed');return Response.json({error:'unavailable'},{status:503});}
   delivered.push(path);
   return f.project.fetch(request);
  }})},
 };
 machine=new MachineDO(machineContext,environment);
 const own=await intake(f,'late-exit-ordering');
 await f.call('/api/intakes/'+own.id+'/start','POST',{key:'late-exit-ordering',workspace:'/tmp/synthetic-late-order',engine:'shell',machine_id:'h1'});
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 assert(saga);
 const started=Date.now()-120_000;
 const machineDb=(machine as any).db;
 machineDb.run("UPDATE sessions SET state='claimed',updated_at=?,created_at=? WHERE id=?",started,started,saga.session_id);
 machineDb.run('INSERT INTO launch_admissions VALUES(?,?,?,?)',saga.attempt_operation_id,saga.session_id,saga.epoch,started);
 (f.project as any).db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",saga.operation_id);
 const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['session:report']};
 const call=(path:string,body:unknown)=>machine.fetch(new Request('https://machine.test'+path,{
  method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)},body:JSON.stringify(body)}));
 await call('/_internal/reap',{dry_run:false});
 assert(delivered.includes('timeout-failed'));
 const launch=JSON.parse(saga.launch_spec_json),native='33333333-3333-4333-8333-333333333333';
 const evidence={source:'aplexer_status',containment_empty:true,
  native_identity:{id:native,tag:'task-hub2-'+saga.session_id,workspace:launch.workspace},
  native_exit:{code:0,exited_at_ms:Date.now()},native_deadline:{deadline_ms:started+launch.timeout_seconds*1000,exceeded:false}};
 const receipt={operation_id:saga.attempt_operation_id,epoch:saga.epoch,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence};
 const response=await call('/api/sessions/'+saga.session_id+'/finish',receipt);
 assert.equal(response.status,201,await response.text());
 const timeoutIndex=delivered.indexOf('/_internal/reaped'),finishIndex=delivered.indexOf('/_internal/finish');
 assert(timeoutIndex>=0&&finishIndex>timeoutIndex);
 const result=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
 assert.equal(result.status,'failed');
 const proof=JSON.parse(result.final_evidence_json);
 assert.equal(proof.reason,'launch_timeout_no_native');
 assert.equal(proof.native_exit_confirmed,true);assert.equal(proof.native_id,native);assert.equal(proof.exit_code,0);
});

test('legacy census failure retains native uncertainty and exact replay through Project completion',async()=>{
 const f=fixture({lostReleaseFailures:1}),own=await intake(f,'legacy-census');
 const response=await f.call('/api/intakes/'+own.id+'/start','POST',{key:'legacy-start',workspace:'/deleted',engine:'shell',machine_id:'h1'});
 assert.equal(response.status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 const receipt={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,
  machine_id:saga.machine_id,intake_id:saga.intake_id,epoch:saga.epoch,native_id:null,reason:'legacy_launch_unobserved',
  failure_kind:'legacy_census_absent',launch_failed:true,native_outcome:'unconfirmed',evidence_digest:'a'.repeat(64),
  evidence:{source:'aplexer_all_registry_census',complete:true,match_count:0,dispatch_provenance:'legacy_unknown',workspace:'/deleted'}};
 assert.equal((await f.call('/_internal/reaped','POST',{...receipt,native_exit_confirmed:true})).status,400);
 const before=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(saga.operation_id);
 const beforeIntake=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id);
 const beforeEvents=f.sql.prepare('SELECT * FROM intake_events WHERE task_id=?').all(own.id);
 assert.equal((await f.call('/_internal/reaped','POST',{...receipt,evidence:{...receipt.evidence,workspace:'/wrong-worktree'}})).status,409);
 assert.deepEqual(f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(saga.operation_id),before);
 assert.deepEqual(f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id),beforeIntake);
 assert.deepEqual(f.sql.prepare('SELECT * FROM intake_events WHERE task_id=?').all(own.id),beforeEvents);
 assert.equal(f.capReleases.length,0);
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,503);
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 const closed=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
 assert.equal(closed.status,'failed');
 const evidence=JSON.parse(closed.final_evidence_json);
 assert.equal(evidence.native_outcome,'unconfirmed');assert.equal(evidence.native_exit_confirmed,undefined);
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 assert.equal(f.sql.prepare("SELECT COUNT(*) AS n FROM intake_events WHERE kind='session_launch_failed' AND task_id=?").get(own.id).n,1);
 assert.equal((await f.call('/_internal/reaped','POST',{...receipt,evidence_digest:'b'.repeat(64)})).status,409);
});

test('a first legacy failure for an older attempt refuses unavailable Project launch metadata',async()=>{
 const f=fixture(),own=await intake(f,'legacy-history');
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'legacy-start',workspace:'/deleted',engine:'shell',machine_id:'h1'})).status,201);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 f.sql.prepare("UPDATE dispatch_sagas SET attempt=1,attempt_operation_id=?,state='running',native_id='new-native',machine_id='h2' WHERE operation_id=?")
  .run(first.operation_id+':1',first.operation_id);
 const before=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id);
 const receipt={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,
  machine_id:first.machine_id,intake_id:first.intake_id,epoch:first.epoch,native_id:null,reason:'legacy_launch_unobserved',
  failure_kind:'legacy_census_absent',launch_failed:true,native_outcome:'unconfirmed',evidence_digest:'a'.repeat(64),
  evidence:{source:'aplexer_all_registry_census',complete:true,match_count:0,dispatch_provenance:'legacy_unknown',workspace:'/deleted'}};
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,409);
 assert.deepEqual(f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id),before);
 assert.equal(f.capReleases.length,0);
 assert.equal(f.sql.prepare("SELECT body_json FROM intake_events WHERE kind='session_launch_failed' AND task_id=?").get(own.id),undefined);
});

test('legacy uncertainty enriches an already timed-out exact saga without reopening or losing old failure evidence',async()=>{
 const f=fixture(),own=await intake(f,'legacy-late');
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'legacy-late',workspace:'/deleted',engine:'shell',machine_id:'h1'})).status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 const binding={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,intake_id:saga.intake_id};
 assert.equal((await f.call('/_internal/reaped','POST',{...binding,native_id:null,reason:'launch_timeout_no_native',launch_failed:true,observed_at:Date.now()})).status,200);
 const receipt={...binding,native_id:null,reason:'legacy_launch_unobserved',failure_kind:'legacy_census_absent',
  launch_failed:true,native_outcome:'unconfirmed',evidence_digest:'a'.repeat(64),
  evidence:{source:'aplexer_all_registry_census',complete:true,match_count:0,dispatch_provenance:'legacy_unknown',workspace:'/deleted'}};
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 const current=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(saga.operation_id) as any;
 assert.equal(current.state,'complete');assert.equal(current.last_reason,'launch_timeout_no_native');
 const task=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
 assert.equal(task.status,'failed');assert.equal(JSON.parse(task.final_evidence_json).legacy_launch_recovery.native_outcome,'unconfirmed');
 const events=f.sql.prepare("SELECT body_json FROM intake_events WHERE task_id=? AND kind='session_launch_failed'").all(own.id) as any[];
 assert.equal(events.length,2);
 assert(events.some(event=>JSON.parse(event.body_json).reason==='launch_timeout_no_native'));
});

test('late legacy failure cannot overwrite a retry intake and remains replayable after the retry',async()=>{
 const f=fixture(),own=await intake(f,'legacy-retry');
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'legacy-retry',workspace:'/deleted',engine:'shell',machine_id:'h1'})).status,201);
 const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
 const receipt={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
  session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,intake_id:saga.intake_id,
  native_id:null,reason:'legacy_launch_unobserved',failure_kind:'legacy_census_absent',
  launch_failed:true,native_outcome:'unconfirmed',evidence_digest:'a'.repeat(64),
  evidence:{source:'aplexer_all_registry_census',complete:true,match_count:0,dispatch_provenance:'legacy_unknown',workspace:'/deleted'}};
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 f.sql.prepare("UPDATE intakes SET session_id='new-session',assigned_machine_id='h2',status='running',final_evidence_json=? WHERE id=?")
  .run(JSON.stringify({new_evidence:'preserve me'}),own.id);
 const newer=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id);
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 assert.deepEqual(f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id),newer);

 // First delivery can also arrive only after the manual retry.
 const key='legacy-unobserved:'+saga.attempt_operation_id+':'+saga.epoch;
 f.sql.prepare('DELETE FROM intake_events WHERE task_id=? AND event_key=?').run(own.id,key);
 assert.equal((await f.call('/_internal/reaped','POST',receipt)).status,200);
 assert.deepEqual(f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id),newer);
 const event=f.sql.prepare('SELECT body_json FROM intake_events WHERE task_id=? AND event_key=?').get(own.id,key) as any;
 assert.equal(JSON.parse(event.body_json).historical,true);
});

test('real Machine to Project legacy reports enrich stopped null-ID attempts without replacing terminal evidence',async()=>{
 for(const reason of ['stop_confirmed','dead','idle']){
  const database=new DatabaseSync(':memory:');
  let alarm:number|null=null;
  const context:any={storage:{
   sql:{exec(query:string,...args:any[]){
    const statement=database.prepare(query);
    return {toArray:()=>statement.columns().length?statement.all(...args):(statement.run(...args),[])};
   }},
   transactionSync(callback:()=>unknown){
    database.exec('BEGIN');
    try{const value=callback();database.exec('COMMIT');return value;}catch(error){database.exec('ROLLBACK');throw error;}
   },
   async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},
  },blockConcurrencyWhile:(callback:()=>unknown)=>Promise.resolve(callback()),getWebSockets:()=>[]};
  let machine:MachineDO;
  const f=fixture({machineFetch:request=>machine.fetch(request)});
  const revoked:any[]=[];
  machine=new MachineDO(context,{
   CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
    const request=input instanceof Request?input:new Request(input,init);
    if(new URL(request.url).pathname==='/_worker/revoke')revoked.push(await request.json());
    return Response.json({authorized:true,revoked:true});
   }})},
   PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:(request:Request)=>f.project.fetch(request)})},
  } as any);
  const own=await intake(f,'legacy-stopped-'+reason);
  assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'legacy-start-'+reason,workspace:'/deleted/worktree',engine:'shell',machine_id:'h1'})).status,201);
  const saga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any;
  const stopped={operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,
   machine_id:saga.machine_id,epoch:saga.epoch,native_id:null,reason,observed_at:Date.now()};
  assert.equal((await f.call(reason==='stop_confirmed'?'/_internal/stopped':'/_internal/reaped','POST',stopped)).status,200);
  const previousSaga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(saga.operation_id);
  const previousIntake=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
  database.prepare("UPDATE sessions SET state='stopped',evidence_json=? WHERE id=?")
   .run(JSON.stringify({stopped:true,reason,previous_proof:'retain'}),saga.session_id);
  database.prepare("UPDATE machine_session_operations SET state='stopped' WHERE operation_id=?").run(saga.attempt_operation_id);
  const now=Date.now();
  database.prepare('INSERT INTO launch_admissions VALUES(?,?,?,?)').run(saga.attempt_operation_id,saga.session_id,saga.epoch,now-100);
  (machine as any).db.cache.clear();
  const receipt={operation_id:saga.attempt_operation_id,epoch:saga.epoch,native_id:null,launch_failed:true,
   failure_kind:'legacy_census_absent',native_outcome:'unconfirmed',evidence:{source:'aplexer_all_registry_census',
    started_at_ms:now-10,observed_at_ms:now,row_count:77,sha256:'a'.repeat(64),complete:true,match_count:0,
    workspace:'/deleted/worktree',tag:'task-hub2-'+saga.session_id,dispatch_provenance:'legacy_unknown'}};
  const call=()=>machine.fetch(new Request('https://machine.test/api/sessions/'+saga.session_id+'/finish',{
   method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify({
    id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['session:report']})},body:JSON.stringify(receipt)}));
  assert.equal((await call()).status,200);
  assert.equal((await call()).status,200);
  assert.equal(revoked.length,1);
  const stored=database.prepare('SELECT state,evidence_json FROM sessions WHERE id=?').get(saga.session_id) as any;
  assert.equal(stored.state,'stopped');
  const machineEvidence=JSON.parse(stored.evidence_json);
  assert.equal(machineEvidence.previous_proof,'retain');
  assert.equal(machineEvidence.legacy_launch_recovery.native_outcome,'unconfirmed');
  assert.equal(machineEvidence.native_exit_confirmed,undefined);
  assert.deepEqual(f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(saga.operation_id),previousSaga);
  const current=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;
  assert.equal(current.status,previousIntake.status);
  const {legacy_launch_recovery,...preserved}=JSON.parse(current.final_evidence_json);
  assert.deepEqual(preserved,JSON.parse(previousIntake.final_evidence_json));
  assert.equal(legacy_launch_recovery.native_outcome,'unconfirmed');
  assert.equal(legacy_launch_recovery.historical,true);
  const outbox=database.prepare("SELECT state FROM machine_outbox WHERE kind='legacy-launch-failure'").get() as any;
  assert.equal(outbox.state,'delivered');
 }
});

test('a lost central draw response replays the same draw before creating a saga',async()=>{
 const candidates=[{id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
 const f=fixture({modelCandidates:candidates,lostDrawResponse:true}),own=await intake(f,'lost-draw');
 const start={key:'lost-draw',workspace:'/tmp/synthetic-picker'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,500);
 assert.equal(f.collectorSql.prepare('SELECT COUNT(*) AS n FROM picker_draws').get()!.n,1);
 assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get()!.n,0);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,200);
 assert.equal(f.collectorSql.prepare('SELECT total_picks FROM picker_alert_counters').get()!.total_picks,1);
 assert.equal(f.sql.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='picker_outbox'").get()!.n,0);
 assert.equal(f.sql.prepare('SELECT version FROM hub2_schema').get()!.version,4);
});


test('a real centrally committed draw stays counted when downstream capacity refuses the start',async()=>{
 const candidates=[{id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
 const f=fixture({modelCandidates:candidates,denyCapacity:true}),own=await intake(f,'capacity-refusal');
 const start={key:'capacity-refusal',workspace:'/tmp/synthetic-picker'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,429);
 assert.equal(f.collectorSql.prepare('SELECT total_picks FROM picker_alert_counters').get()!.total_picks,1);
 assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get()!.n,0);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,429);
 assert.equal(f.collectorSql.prepare('SELECT total_picks FROM picker_alert_counters').get()!.total_picks,1);
});


test('crash after actual capacity acceptance reuses stable operation and session identities on retry',async()=>{
 const candidates=[{id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
 const f=fixture({modelCandidates:candidates,lostCapResponse:true}),own=await intake(f,'lost-capacity');
 const start={key:'lost-capacity',workspace:'/tmp/synthetic-capacity'};
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,500);
 const reservation=f.collectorSql.prepare('SELECT operation_id,session_id FROM session_start_reservations').get()!;
 assert(reservation.operation_id);assert(reservation.session_id);assert.notEqual(reservation.operation_id,reservation.session_id);
 assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get()!.n,0);
 f.restart();
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',start)).status,201);
 const saga=f.sql.prepare('SELECT operation_id,session_id FROM dispatch_sagas').get()!;
 assert.deepEqual({...saga},{...reservation});
 assert.equal(f.collectorSql.prepare('SELECT COUNT(*) AS n FROM session_start_reservations').get()!.n,1);
 assert.equal(f.collectorSql.prepare('SELECT total_picks FROM picker_alert_counters').get()!.total_picks,1);
});

test('a collector SQL failure does not refuse a normally routable Project start',async()=>{
 const candidates=[{id:'codex-sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
 const f=fixture({modelCandidates:candidates}),own=await intake(f,'observer-failure');
 f.collectorSql.exec("CREATE TRIGGER reject_collector BEFORE INSERT ON picker_draws BEGIN SELECT RAISE(ABORT,'synthetic collector write failure'); END");
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{key:'observer-failure',workspace:'/tmp/synthetic-picker'})).status,201);
 assert.equal(f.sql.prepare('SELECT COUNT(*) AS n FROM dispatch_sagas').get()!.n,1);
 assert.equal(f.collectorSql.prepare('SELECT COUNT(*) AS n FROM picker_draws').get()!.n,0);
});
