import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ProjectDO} from '../src/project-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
function fixture(){const sql=new DatabaseSync(':memory:');let alarm:any=null;const ctx:any={id:{toString:()=> 'synthetic-project'},storage:{sql:{exec(q:string,...a:any[]){const p=sql.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){sql.exec('BEGIN');try{const r=fn();sql.exec('COMMIT');return r;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};
 const routes:any[]=[];const env:any={CONTROL:{idFromName:(id:any)=>id,get:()=>({fetch:async(_r:any,init:any)=>{routes.push(JSON.parse(init.body));return Response.json({registered:true});}})},MACHINES:{idFromName:(id:any)=>id,get:()=>({fetch:async(url:string)=>Response.json(url.endsWith('/reserve')?{state:'staged',epoch:1}:{state:'offered'})})}};
 let project=new ProjectDO(ctx,env);const call=(p:string,m='GET',b?:unknown,a:any=admin)=>project.fetch(new Request('https://p.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{sql,get project(){return project;},restart:()=>project=new ProjectDO(ctx,env),routes,call,getAlarm:()=>alarm};}
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
