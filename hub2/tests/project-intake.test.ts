import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ProjectDO} from '../src/project-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
function fixture(){const sql=new DatabaseSync(':memory:');let alarm:any=null;const storageValues=new Map<string,unknown>(),ctx:any={id:{toString:()=> 'synthetic-project'},storage:{sql:{exec(q:string,...a:any[]){const p=sql.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){sql.exec('BEGIN');try{const r=fn();sql.exec('COMMIT');return r;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;},async get(key:string){return storageValues.get(key);},async put(key:string,value:unknown){storageValues.set(key,value);},async delete(key:string){return storageValues.delete(key);}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};
 const routes:any[]=[],machineCalls:string[]=[];
 const requestOf=(input:string|Request,init?:RequestInit)=>input instanceof Request?input:new Request(input,init);
 const CONTROL={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init);routes.push(await request.json());return Response.json({registered:true});}})};
 const MACHINES={idFromName:(id:any)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{const request=requestOf(input,init);const path=new URL(request.url).pathname;machineCalls.push(path);return Response.json(path.endsWith('/reserve')?{state:'staged',epoch:1}:{state:'offered'});}})};
 const unused={idFromName:(id:any)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})};
 const env:any={CONTROL,PROJECTS:unused,MACHINES,ACCOUNTS:unused,MAILBOXES:unused};
 let project=new ProjectDO(ctx,env);const call=(p:string,m='GET',b?:unknown,a:any=admin)=>project.fetch(new Request('https://p.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{sql,get project(){return project;},restart:()=>project=new ProjectDO(ctx,env),routes,machineCalls,call,getAlarm:()=>alarm,storageValues};}
const body=async(r:Response)=>await r.json() as any;
async function intake(f:ReturnType<typeof fixture>,key='task'){const result=await f.call('/api/intakes?project=org/a','POST',{key,title:'Original task',text:'Original requested behavior',acceptance_criteria:['Actual result']});assert.equal(result.status,201);return(await body(result)).intake;}
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
test('old concurrent finish resuming after account release cannot overwrite a newly retried session',async()=>{
 const f=fixture(),own=await intake(f,'finish-race'),start={key:'first-race',workspace:'/tmp/synthetic-race',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 await f.call('/api/intakes/'+own.id+'/start','POST',start);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,project=f.project as any;
 project.db.run("UPDATE dispatch_sagas SET state='admitted' WHERE operation_id=?",first.operation_id);
 const binding={operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:'22222222-2222-4222-8222-222222222222'};
 assert.equal((await f.call('/_internal/started','POST',binding)).status,201);
 project.db.run('UPDATE dispatch_sagas SET account_id=? WHERE operation_id=?','synthetic-account',first.operation_id);
 let entered!:()=>void,release!:()=>void,calls=0;const started=new Promise<void>(r=>entered=r),gate=new Promise<void>(r=>release=r);
 project.releaseAccount=async()=>{if(++calls===1){entered();await gate;}return true;};
 const receipt={...binding,exit_code:1,native_exit_confirmed:true,evidence:{result:'failed'}},pending=f.call('/_internal/finish','POST',receipt);await started;
 assert.equal((await f.call('/_internal/finish','POST',receipt)).status,200);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'after-failure'})).status<300,true);
 const current=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.notEqual(current.session_id,first.session_id);
 release();const result=await pending;assert.equal(result.status,200);assert.equal((await body(result)).duplicate,true);
 const after=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any;assert.equal(after.session_id,current.session_id);assert.equal(after.status,'queued');assert.equal(after.final_evidence_json,null);
});
test('late AccountDO admission cannot authorize a completed attempt after the intake is retried',async()=>{
 const f=fixture(),own=await intake(f,'admission-race'),start={key:'first-admission-race',workspace:'/tmp/synthetic-admission-race',engine:'shell',machine_id:'h1',override_reason:'Synthetic semantic test'};
 await f.call('/api/intakes/'+own.id+'/start','POST',start);
 const first=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE intake_id=?').get(own.id) as any,project=f.project as any;
 project.db.run("UPDATE dispatch_sagas SET state='admitted',account_id=? WHERE operation_id=?",'synthetic-account',first.operation_id);
 let entered!:()=>void,release!:()=>void;const admissionStarted=new Promise<void>(resolve=>entered=resolve),accountGate=new Promise<void>(resolve=>release=resolve);
 const env=project.env;env.ACCOUNTS={idFromName:(id:string)=>id,get:()=>({fetch:async()=>{entered();await accountGate;return Response.json({reservation:{operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,state:'running'}});}})};
 const admission=f.call('/_internal/admission','POST',{operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch});await admissionStarted;
 project.releaseAccount=async()=>true;
 const native='66666666-6666-4666-8666-666666666666',failedProof={source:'aplexer_status',containment_empty:true,native_identity:{id:native,tag:'task-hub2-'+first.session_id,workspace:start.workspace},native_exit:{code:0}};
 assert.equal((await f.call('/_internal/finish','POST',{operation_id:first.operation_id,attempt_operation_id:first.attempt_operation_id,session_id:first.session_id,machine_id:first.machine_id,epoch:first.epoch,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence:failedProof})).status,200);
 assert.equal((await f.call('/api/intakes/'+own.id+'/start','POST',{...start,key:'retry-after-admission-race'})).status,201);
 const retried=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any,newSaga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE session_id=?').get(retried.session_id) as any;
 assert.notEqual(retried.session_id,first.session_id);assert.equal(retried.status,'queued');assert.notEqual(newSaga.operation_id,first.operation_id);
 release();const stale=await admission;assert.equal(stale.status,409);const staleBody=await body(stale);assert.equal(staleBody.error,'stale_or_unoffered_admission');
 const after=f.sql.prepare('SELECT * FROM intakes WHERE id=?').get(own.id) as any,oldSaga=f.sql.prepare('SELECT * FROM dispatch_sagas WHERE operation_id=?').get(first.operation_id) as any;
 assert.equal(after.session_id,retried.session_id);assert.equal(after.status,'queued');assert.equal(oldSaga.state,'complete');assert.equal(staleBody.launch_allowed,undefined);
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
