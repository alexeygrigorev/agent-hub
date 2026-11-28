// Regression for intake bca192ab: a worker session started for a child intake reads and appends to its
// parent intake (full and short id) through the real router, Control route lookup and Project object;
// every denial is a named 403 missing_scope, never a bare 404 route_not_found.
import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';import {ProjectDO} from '../src/project-do';

const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
const PROJECT='org/a',OTHER='org/b';
function storage(sql:DatabaseSync):any{return{storage:{sql:{exec(q:string,...a:any[]){const s=sql.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},
 transactionSync(fn:()=>unknown){sql.exec('BEGIN');try{const v=fn();sql.exec('COMMIT');return v;}catch(e){sql.exec('ROLLBACK');throw e;}},
 async getAlarm(){return null;},async setAlarm(){},async deleteAlarm(){},async get(){return undefined;},async put(){},async delete(){return false;}},
 id:{toString:()=>'synthetic'},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};}

function fixture(){
 const controlSql=new DatabaseSync(':memory:'),projectSqls=new Map<string,DatabaseSync>();
 const tokens=new Map<string,unknown>();let control:ControlDO;const projects=new Map<string,ProjectDO>();
 const req=(i:string|Request,init?:RequestInit)=>i instanceof Request?i:new Request(i,init);
 const stub={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(i:string|Request,init?:RequestInit)=>{const r=req(i,init);
   if(new URL(r.url).pathname==='/_verify'){const {token}=await r.clone().json() as {token:string};const actor=tokens.get(token);return actor?Response.json({actor}):Response.json({error:'unauthorized'},{status:401});}
   return control.fetch(r);}})},
  PROJECTS:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:(i:string|Request,init?:RequestInit)=>project(id).fetch(req(i,init))})},
  MACHINES:stub,ACCOUNTS:stub,MAILBOXES:stub,
 };
 const project=(id:string)=>{let p=projects.get(id);if(!p){const sql=new DatabaseSync(':memory:');projectSqls.set(id,sql);p=new ProjectDO(storage(sql),env);projects.set(id,p);}return p;};
 control=new ControlDO(storage(controlSql),env);
 const direct=(owner:string,path:string,m:string,b:unknown,a:unknown)=>project(owner).fetch(new Request('https://p.test'+path,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));
 return{env,tokens,direct,close:()=>{controlSql.close();for(const s of projectSqls.values())s.close();}};
}
const data=async(r:Response)=>await r.json() as any;
/** The exact scope set WorkerCredentials issues to a native worker session. */
const workerActor=(session:string,intake:string,project=PROJECT)=>({id:'session:'+session,kind:'agent',resource_id:intake,generation:'worker:1:op',
 scopes:['intake:read','intake:append','intake:create','session:start','routing:read','claim:read','claim:write','message:read','message:send','mailbox:session:'+session,'worker_project:'+project]});

test('child-intake worker session shows and appends to its parent intake by full and short id; denials are named 403s',async t=>{
 const f=fixture();t.after(f.close);
 const {default:worker}=await import('../src/worker');
 const create=async(owner:string,key:string,actor:unknown=admin)=>{const r=await f.direct(owner,'/api/intakes?project='+owner,'POST',{key,title:'T '+key,text:'X',acceptance_criteria:['A']},actor);assert.equal(r.status,201,key);return(await data(r)).intake;};
 const parent=await create(PROJECT,'parent'),unrelated=await create(PROJECT,'unrelated'),foreign=await create(OTHER,'foreign');
 // The parent's own worker session creates the child intake, so the child records parent_intake_id.
 const parentSession='aaaaaaaa-1111-4111-8111-111111111111',childSession='bbbbbbbb-2222-4222-8222-222222222222';
 const child=await create(PROJECT,'child',workerActor(parentSession,parent.id));
 assert.equal(child.parent_intake_id,parent.id);
 const childToken='synthetic-child-worker-'+'c'.repeat(32),limitedToken='synthetic-child-limited-'+'d'.repeat(32);
 f.tokens.set(childToken,workerActor(childSession,child.id));
 f.tokens.set(limitedToken,{...workerActor(childSession,child.id),scopes:['intake:read','worker_project:'+PROJECT]});
 const call=(path:string,token=childToken,method='GET',b?:unknown)=>worker.fetch(new Request('http://127.0.0.1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(b===undefined?{}:{body:JSON.stringify(b)})}),f.env);

 for(const ref of [parent.id,parent.id.slice(0,8)]){
  const shown=await call('/api/intake/tasks/'+ref);
  assert.equal(shown.status,200,'show '+ref);assert.equal((await data(shown)).intake.id,parent.id);
  assert.equal((await call('/api/intakes/'+ref+'/history')).status,200,'history '+ref);
  // `hub intake append --note` posts Hub1-shaped /events; native append posts /append.
  const events=await call('/api/intake/tasks/'+ref+'/events',childToken,'POST',{key:'note-'+ref,note:'child progress via '+ref});
  assert.equal(events.status,201,'events '+ref);assert.equal((await data(events)).intake_id,parent.id);
  const appended=await call('/api/intakes/'+ref+'/append',childToken,'POST',{key:'append-'+ref,note:'child append via '+ref});
  assert.equal(appended.status,201,'append '+ref);
 }
 // A child cannot claim the parent's completion.
 const claim=await call('/api/intakes/'+parent.id+'/append',childToken,'POST',{key:'claim',evidence:{kind:'session_finished',session_id:parentSession}});
 assert.equal(claim.status,403);assert.equal((await data(claim)).missing_scope,'operator:admin');
 // Nor write records the parent's merge gate, verifier or blocker handling reads.
 for(const kind of ['merge_gate','task_blocked','acceptance_verification']){
  const r=await call('/api/intakes/'+parent.id+'/append',childToken,'POST',{key:'gate-'+kind,evidence:{kind,session_id:parentSession}});
  assert.equal(r.status,403,kind);assert.equal((await data(r)).missing_scope,'operator:admin',kind);
 }
 assert.equal((await call('/api/intakes/'+parent.id+'/merge-state')).status,200);

 const named=async(r:Response,scope:string,label:string)=>{assert.equal(r.status,403,label);const b=await data(r);assert.notEqual(b.error,'route_not_found',label);assert.equal(b.missing_scope,scope,label);assert.match(String(b.detail),new RegExp(scope),label);};
 // Sibling intake in the same project, by full and short id: named intake:read refusal.
 await named(await call('/api/intake/tasks/'+unrelated.id),'intake:read','sibling full');
 await named(await call('/api/intake/tasks/'+unrelated.id.slice(0,8)),'intake:read','sibling short');
 await named(await call('/api/intake/tasks/'+unrelated.id+'/events',childToken,'POST',{key:'x',note:'nope'}),'intake:append','sibling append');
 // Intake in another project: Control's named refusal, not a bare 404.
 await named(await call('/api/intake/tasks/'+foreign.id),'intake:read','foreign full');
 await named(await call('/api/intake/tasks/'+foreign.id.slice(0,8)),'intake:read','foreign short');
 // Missing intake:append on an otherwise linked worker.
 await named(await call('/api/intake/tasks/'+parent.id+'/events',limitedToken,'POST',{key:'y',note:'nope'}),'intake:append','no append scope');
 // A truly nonexistent id stays a 404.
 assert.equal((await call('/api/intake/tasks/ffffffff-ffff-4fff-8fff-ffffffffffff')).status,404);

 const history=await data(await f.direct(PROJECT,'/api/intakes/'+parent.id+'/history','GET',undefined,admin));
 const notes=JSON.stringify(history);
 assert.match(notes,/child progress via /);assert.match(notes,/child append via /);assert.match(notes,new RegExp('session:'+childSession));
});

test('operator can set parent_intake_id on an existing intake (short or full id) and its worker session gains parent read/append',async t=>{
 const f=fixture();t.after(f.close);
 const {default:worker}=await import('../src/worker');
 const create=async(key:string)=>{const r=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key,title:'T '+key,text:'X',acceptance_criteria:['A']},admin);assert.equal(r.status,201,key);return(await data(r)).intake;};
 const parent=await create('parent2'),standalone=await create('standalone');
 assert.equal(standalone.parent_intake_id,null);
 const adminToken='synthetic-admin-'+'a'.repeat(32),projectWriterToken='synthetic-writer-'+'w'.repeat(32);
 const childSession='cccccccc-3333-4333-8333-333333333333',childToken='synthetic-standalone-worker-'+'e'.repeat(32);
 f.tokens.set(adminToken,admin);
 f.tokens.set(projectWriterToken,{id:'human:writer',kind:'operator',scopes:['intake:read','intake:write','project:'+PROJECT]});
 f.tokens.set(childToken,workerActor(childSession,standalone.id));
 const call=(path:string,token:string,method='GET',b?:unknown)=>worker.fetch(new Request('http://127.0.0.1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(b===undefined?{}:{body:JSON.stringify(b)})}),f.env);

 // Non-operator (even project writer) and worker session cannot set parent_intake_id.
 const deniedWriter=await f.direct(PROJECT,'/api/intakes/'+standalone.id,'PATCH',{key:'p-deny-w',parent_intake_id:parent.id},{id:'human:writer',kind:'operator',scopes:['intake:read','intake:write','project:'+PROJECT]});
 assert.equal(deniedWriter.status,403);assert.equal((await data(deniedWriter)).missing_scope,'operator:admin');
 const deniedWorker=await call('/api/intakes/'+standalone.id,childToken,'PATCH',{key:'p-deny-s',parent_intake_id:parent.id});
 assert.equal(deniedWorker.status,403);

 // Self, cycle, and missing parent checks.
 assert.equal((await f.direct(PROJECT,'/api/intakes/'+standalone.id,'PATCH',{key:'p-self',parent_intake_id:standalone.id},admin)).status,409);
 assert.equal((await f.direct(PROJECT,'/api/intakes/'+standalone.id,'PATCH',{key:'p-miss',parent_intake_id:'deadbeef'},admin)).status,404);

 // Operator sets parent using short IDs for both child and parent.
 const linked=await f.direct(PROJECT,'/api/intakes/'+standalone.id.slice(0,8)+'?project='+PROJECT,'PATCH',{key:'p-set',parent_intake_id:parent.id.slice(0,8)},admin);
 assert.equal(linked.status,200);assert.equal((await data(linked)).intake.parent_intake_id,parent.id);

 // Cycle check: parent cannot become a child of its own child.
 assert.equal((await f.direct(PROJECT,'/api/intakes/'+parent.id,'PATCH',{key:'p-cycle',parent_intake_id:standalone.id},admin)).status,409);

 // Worker session bound to the updated child now reads and appends to the parent by short and full id.
 for(const ref of [parent.id,parent.id.slice(0,8)]){
  const shown=await call('/api/intake/tasks/'+ref,childToken);
  assert.equal(shown.status,200,'show '+ref);assert.equal((await data(shown)).intake.id,parent.id);
  const appended=await call('/api/intake/tasks/'+ref+'/events',childToken,'POST',{key:'linked-note-'+ref,note:'linked child note '+ref});
  assert.equal(appended.status,201,'append '+ref);
 }

 // Operator can clear parent_intake_id back to null.
 const cleared=await f.direct(PROJECT,'/api/intakes/'+standalone.id,'PATCH',{key:'p-clear',parent_intake_id:null},admin);
 assert.equal(cleared.status,200);assert.equal((await data(cleared)).intake.parent_intake_id,null);
});

test('compat POST /api/intake/tasks and /api/intake/tasks/:id support parent_intake_id for operators',async t=>{
 const f=fixture();t.after(f.close);
 const {default:worker}=await import('../src/worker');
 const adminToken='synthetic-admin-compat-'+'a'.repeat(32),writerToken='synthetic-writer-compat-'+'w'.repeat(32);
 const writer={id:'human:writer',kind:'operator',scopes:['intake:read','intake:write','project:'+PROJECT]};
 f.tokens.set(adminToken,admin);
 f.tokens.set(writerToken,writer);
 const call=(path:string,token:string,method='GET',b?:unknown)=>worker.fetch(new Request('http://127.0.0.1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(b===undefined?{}:{body:JSON.stringify(b)})}),f.env);

 const parentRes=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key:'compat-parent',title:'Parent',text:'P',acceptance_criteria:['A']},admin);
 assert.equal(parentRes.status,201);const parent=(await data(parentRes)).intake;

 // Non-operator cannot set parent_intake_id on create or compat update.
 const deniedCreate=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key:'compat-deny-create',title:'C',text:'X',acceptance_criteria:['A'],parent_intake_id:parent.id},writer);
 assert.equal(deniedCreate.status,403);assert.equal((await data(deniedCreate)).missing_scope,'operator:admin');

 // Operator creates a child directly via --parent (short ID).
 const createdChildRes=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key:'compat-child-create',title:'Child',text:'C',acceptance_criteria:['A'],parent_intake_id:parent.id.slice(0,8)},admin);
 assert.equal(createdChildRes.status,201);const createdChild=(await data(createdChildRes)).intake;
 assert.equal(createdChild.parent_intake_id,parent.id);

 // Operator links and clears parent_intake_id via compat POST /api/intake/tasks/:id (used by `hub intake update --parent`).
 const standaloneRes=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key:'compat-standalone',title:'Standalone',text:'S',acceptance_criteria:['A']},admin);
 assert.equal(standaloneRes.status,201);const standalone=(await data(standaloneRes)).intake;
 const deniedUpdate=await f.direct(PROJECT,'/api/intakes/'+standalone.id,'POST',{key:'compat-deny-upd',parent_intake_id:parent.id},writer);
 assert.equal(deniedUpdate.status,403);assert.equal((await data(deniedUpdate)).missing_scope,'operator:admin');
 const linkedRes=await f.direct(PROJECT,'/api/intakes/'+standalone.id,'POST',{key:'compat-link',parent_intake_id:parent.id.slice(0,8),note:'link via compat'},admin);
 assert.equal(linkedRes.status,200);assert.equal((await data(linkedRes)).intake.parent_intake_id,parent.id);
 const clearedRes=await f.direct(PROJECT,'/api/intakes/'+standalone.id.slice(0,8),'POST',{key:'compat-clear',parent_intake_id:null},admin);
 assert.equal(clearedRes.status,200);assert.equal((await data(clearedRes)).intake.parent_intake_id,null);
});
