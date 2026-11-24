// Regression for intake 37e34dcd: a fix worker could not read the sessions and intakes its own intake
// names as evidence (403 missing scope intake:read). A bound worker session now reads exactly those
// objects, read-only, in its own project, through the real router, Control route lookup and Project
// object; each grant is audited on the reader's intake, and every other read keeps a typed refusal
// that names the operator command. Nothing here widens writes, commands or cross-project reads.
import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';import {ProjectDO,evidenceReferences} from '../src/project-do';

const admin={id:'human:admin',kind:'operator',scopes:['operator:admin']};
const PROJECT='org/a',OTHER='org/b',MACHINE='host-a';
function storage(sql:DatabaseSync):any{return{storage:{sql:{exec(q:string,...a:any[]){const s=sql.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},
 transactionSync(fn:()=>unknown){sql.exec('BEGIN');try{const v=fn();sql.exec('COMMIT');return v;}catch(e){sql.exec('ROLLBACK');throw e;}},
 async getAlarm(){return null;},async setAlarm(){},async deleteAlarm(){},async get(){return undefined;},async put(){},async delete(){return false;}},
 id:{toString:()=>'synthetic'},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};}

function fixture(){
 const controlSql=new DatabaseSync(':memory:'),projectSqls=new Map<string,DatabaseSync>();
 const tokens=new Map<string,unknown>();let control:ControlDO;const projects=new Map<string,ProjectDO>();
 const machineCalls:{method:string;path:string;actor:any}[]=[];
 const req=(i:string|Request,init?:RequestInit)=>i instanceof Request?i:new Request(i,init);
 const stub={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({ok:true})})};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async(i:string|Request,init?:RequestInit)=>{const r=req(i,init);
   if(new URL(r.url).pathname==='/_verify'){const {token}=await r.clone().json() as {token:string};const actor=tokens.get(token);return actor?Response.json({actor}):Response.json({error:'unauthorized'},{status:401});}
   return control.fetch(r);}})},
  PROJECTS:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:(i:string|Request,init?:RequestInit)=>project(id).fetch(req(i,init))})},
  // Records exactly what the edge forwards to the Machine object (path and actor).
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async(i:string|Request,init?:RequestInit)=>{const r=req(i,init);
   machineCalls.push({method:r.method,path:new URL(r.url).pathname,actor:JSON.parse(r.headers.get('x-hub2-actor')||'null')});
   return Response.json({session:{id:new URL(r.url).pathname.split('/')[3]}});}})},
  ACCOUNTS:stub,MAILBOXES:stub,
 };
 const project=(id:string)=>{let p=projects.get(id);if(!p){const sql=new DatabaseSync(':memory:');projectSqls.set(id,sql);p=new ProjectDO(storage(sql),env);projects.set(id,p);}return p;};
 control=new ControlDO(storage(controlSql),env);
 const direct=(owner:string,path:string,m:string,b:unknown,a:unknown)=>project(owner).fetch(new Request('https://p.test'+path,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));
 const registerSession=async(id:string,projectId:string,intake:string)=>{
  const r=await control.fetch(new Request('https://control.internal/_routes/register',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({kind:'session',id,owner:MACHINE,resource_id:projectId,participants:[],bound_intake_id:intake})}));
  assert.equal(r.status,200,'register '+id);
 };
 const sql=(id:string)=>{project(id);return projectSqls.get(id)!;};
 return{env,tokens,direct,registerSession,machineCalls,sql,close:()=>{controlSql.close();for(const s of projectSqls.values())s.close();}};
}
const data=async(r:Response)=>await r.json() as any;
const workerActor=(session:string,intake:string,project=PROJECT)=>({id:'session:'+session,kind:'agent',resource_id:intake,generation:'worker:1:op',
 scopes:['intake:read','intake:append','intake:create','session:start','routing:read','claim:read','claim:write','message:read','message:send','mailbox:session:'+session,'worker_project:'+project]});

const S_CITED='4027e3ec-1111-4111-8111-111111111111',S_FULL='5e0a9c29-2222-4222-8222-222222222222';
const S_UNCITED='f24dba8c-3333-4333-8333-333333333333',S_FOREIGN='3c395070-4444-4444-8444-444444444444';
const WORKER='7eaf5745-5555-4555-8555-555555555555',CHILD_WORKER='8246f41e-6666-4666-8666-666666666666';

test('evidence references: full ids and 8-hex short ids only, bounded, never commit SHAs or longer hex',()=>{
 const refs=evidenceReferences(`sessions 4027E3EC, ${S_FULL} (intake 14c773e3) commit 87f7eb58e2a1 sha 0123456789abcdef0123456789abcdef01234567 tag hub-a8cbf216 x4027e3ec`);
 assert.deepEqual(refs.sort(),['14c773e3','4027e3ec',S_FULL,'a8cbf216'].sort());
 assert.equal(evidenceReferences(Array.from({length:100},(_,i)=>(0x10000000+i).toString(16)).join(' ')).length,64);
});

test('bound worker reads sessions and intakes its own intake cites, read-only and audited; all else is a typed refusal',async t=>{
 const f=fixture();t.after(f.close);
 const {default:worker}=await import('../src/worker');
 const create=async(owner:string,key:string,text:string,actor:unknown=admin)=>{const r=await f.direct(owner,'/api/intakes?project='+owner,'POST',{key,title:'T '+key,text,acceptance_criteria:['A']},actor);assert.equal(r.status,201,key);return(await data(r)).intake;};
 const evidence=await create(PROJECT,'evidence','prior run');
 const uncited=await create(PROJECT,'uncited','not mentioned');
 const foreign=await create(OTHER,'foreign','other project');
 const fix=await create(PROJECT,'fix',`Sessions 4027e3ec and ${S_FULL} on intake ${evidence.id.slice(0,8)} failed; also ${S_FOREIGN.slice(0,8)} and intake ${foreign.id.slice(0,8)}.`);
 await f.registerSession(S_CITED,PROJECT,evidence.id);await f.registerSession(S_FULL,PROJECT,evidence.id);
 await f.registerSession(S_UNCITED,PROJECT,uncited.id);await f.registerSession(S_FOREIGN,OTHER,foreign.id);
 const token='synthetic-fix-worker-'+'f'.repeat(32);f.tokens.set(token,workerActor(WORKER,fix.id));
 const call=(path:string,method='GET',b?:unknown,tok=token)=>worker.fetch(new Request('http://127.0.0.1'+path,{method,headers:{Authorization:'Bearer '+tok,'Content-Type':'application/json'},...(b===undefined?{}:{body:JSON.stringify(b)})}),f.env);

 // Cited sessions: show and events by short and full id reach the Machine with a one-object read actor.
 for(const [ref,full] of [[S_CITED.slice(0,8),S_CITED],[S_CITED,S_CITED],[S_FULL,S_FULL],[S_FULL.slice(0,8),S_FULL]]){
  for(const suffix of ['','/events']){
   f.machineCalls.length=0;
   const r=await call('/api/sessions/'+ref+suffix+'?threshold=60');assert.equal(r.status,200,ref+suffix);
   assert.equal(f.machineCalls.length,1);const sent=f.machineCalls[0];
   assert.equal(sent.path,'/api/sessions/'+full+suffix);assert.equal(sent.method,'GET');
   assert.deepEqual(sent.actor.scopes,['session:read']);assert.equal(sent.actor.resource_id,full);assert.equal(sent.actor.id,'session:'+WORKER);
  }
 }
 // Never a mutation, command, capture or wake on a cited session.
 f.machineCalls.length=0;
 for(const [path,method] of [['/send','POST'],['/stop','POST'],['/wake','POST'],['/capture','GET'],['/wakes','GET'],['/commands','GET']] as const){
  const r=await call('/api/sessions/'+S_CITED+path,method,method==='POST'?{key:'k',prompt:'x'}:undefined);
  assert.equal(r.status,403,path);assert.equal((await data(r)).missing_scope,'intake:read',path);
 }
 assert.equal(f.machineCalls.length,0,'no denied call reached the Machine');

 // Uncited same-project session: typed refusal naming the exact operator command.
 const refused=await call('/api/sessions/'+S_UNCITED.slice(0,8));assert.equal(refused.status,403);
 const rb=await data(refused);assert.equal(rb.error,'evidence_not_referenced');assert.equal(rb.reason,'not_referenced');
 assert.equal(rb.missing_scope,'intake:read');assert.equal(rb.operator_command,'hub sessions show '+S_UNCITED.slice(0,8));assert.doesNotMatch(JSON.stringify(rb),new RegExp(S_UNCITED),'uncited full id never disclosed');assert.match(rb.detail,/hub sessions show /);
 // Cited but in another project: Control's original denial, never an evidence grant.
 const crossed=await call('/api/sessions/'+S_FOREIGN.slice(0,8));assert.equal(crossed.status,403);
 assert.notEqual((await data(crossed)).error,'evidence_not_referenced');
 assert.equal(f.machineCalls.length,0);

 // Cited intake: show and history by short and full id; never append, edit or start.
 for(const ref of [evidence.id,evidence.id.slice(0,8)]){
  const shown=await call('/api/intake/tasks/'+ref);assert.equal(shown.status,200,'show '+ref);assert.equal((await data(shown)).intake.id,evidence.id);
  assert.equal((await call('/api/intakes/'+ref+'/history')).status,200,'history '+ref);
  for(const [path,method,body] of [['/events','POST',{key:'n-'+ref,note:'x'}],['/append','POST',{key:'a-'+ref,note:'x'}],['','PATCH',{key:'e-'+ref,title:'x'}],['/start','POST',{key:'s-'+ref}]] as const){
   const r=await call('/api/intakes/'+ref+path,method,body);assert.ok([403,404].includes(r.status),path+' '+r.status);
  }
 }
 // Other reads of a cited intake are not part of the evidence path.
 for(const path of ['/context','/audit','/merge-state','/links']){
  const r=await call('/api/intakes/'+evidence.id+path);assert.ok([403,404,405].includes(r.status),path+' '+r.status);
 }
 // History paging survives the evidence forward (after/limit are honoured, not reset to page one).
 for(let i=0;i<5;i++)assert.equal((await f.direct(PROJECT,'/api/intakes/'+evidence.id+'/append','POST',{key:'pg-'+i,note:'page '+i},admin)).status,201);
 const all=(await data(await call('/api/intakes/'+evidence.id+'/history'))).events;assert.ok(all.length>=6);
 const page1=await data(await call('/api/intakes/'+evidence.id.slice(0,8)+'/history?after=0&limit=2'));
 assert.equal(page1.events.length,2);assert.equal(page1.next,page1.events[1].seq);
 const page2=await data(await call('/api/intakes/'+evidence.id.slice(0,8)+'/history?after='+page1.next+'&limit=2'));
 assert.equal(page2.events.length,2);assert.ok(page2.events[0].seq>page1.next,'second page starts after the cursor');
 const uncitedIntake=await call('/api/intake/tasks/'+uncited.id.slice(0,8));assert.equal(uncitedIntake.status,403);
 const ub=await data(uncitedIntake);assert.equal(ub.error,'evidence_not_referenced');assert.equal(ub.operator_command,'hub intake show '+uncited.id.slice(0,8));assert.doesNotMatch(JSON.stringify(ub),new RegExp(uncited.id),'uncited full id never disclosed');
 const foreignIntake=await call('/api/intake/tasks/'+foreign.id.slice(0,8));assert.equal(foreignIntake.status,403);
 assert.notEqual((await data(foreignIntake)).error,'evidence_not_referenced');

 // Audited once per target on the reader's own intake, read-only, attributed to the worker session.
 const history=await data(await f.direct(PROJECT,'/api/intakes/'+fix.id+'/history','GET',undefined,admin));
 const audits=(history.events as any[]).filter(e=>e.kind==='evidence_read');
 // Exactly one audit record per target, however many reads.
 assert.deepEqual(audits.map(e=>(e.body??e.payload??e).target_id).sort(),[S_CITED,S_FULL,evidence.id].sort());
 const text=JSON.stringify(history);
 for(const id of [S_CITED,S_FULL,evidence.id])assert.match(text,new RegExp(id));
 assert.doesNotMatch(text,new RegExp(S_UNCITED));assert.match(text,new RegExp('session:'+WORKER));
 const unchanged=await data(await f.direct(PROJECT,'/api/intakes/'+evidence.id+'/history','GET',undefined,admin));
 assert.doesNotMatch(JSON.stringify(unchanged),/evidence_read/,'the read target is not written to');

 // A worker-authored intake grants nothing: a worker cannot mint evidence references for a child.
 const child=await create(PROJECT,'child',`read ${S_CITED.slice(0,8)} please`,workerActor(WORKER,fix.id));
 const childToken='synthetic-child-worker-'+'c'.repeat(32);f.tokens.set(childToken,workerActor(CHILD_WORKER,child.id));
 const minted=await call('/api/sessions/'+S_CITED.slice(0,8),'GET',undefined,childToken);assert.equal(minted.status,403);
 assert.equal((await data(minted)).reason,'intake_authored_by_worker');
});

test('a schedule occurrence of a worker-authored parent grants nothing even when an operator owns the schedule',async t=>{
 const f=fixture();t.after(f.close);
 const {default:worker}=await import('../src/worker');
 const create=async(key:string,text:string,actor:unknown=admin)=>{const r=await f.direct(PROJECT,'/api/intakes?project='+PROJECT,'POST',{key,title:'T '+key,text,acceptance_criteria:['A']},actor);assert.equal(r.status,201,key);return(await data(r)).intake;};
 const requester=await create('req','root');
 const planted=await create('planted',`cite ${S_CITED.slice(0,8)}`,workerActor(WORKER,requester.id));
 const evidence=await create('ev','x');await f.registerSession(S_CITED,PROJECT,evidence.id);
 const occurrence=await create('occ',planted.text);
 const token='synthetic-occ-worker-'+'o'.repeat(32);f.tokens.set(token,workerActor(CHILD_WORKER,occurrence.id));
 const read=()=>worker.fetch(new Request('http://127.0.0.1/api/sessions/'+S_CITED.slice(0,8),{headers:{Authorization:'Bearer '+token}}),f.env);
 // An operator-authored standalone copy is a requester intake.
 assert.equal((await read()).status,200);
 // The same row as the schedule runner writes it (operator owner, parent's text copied) is refused.
 f.sql(PROJECT).prepare('UPDATE intakes SET schedule_id=?,parent_intake_id=? WHERE id=?').run('schedule-1',planted.id,occurrence.id);
 const r=await read();assert.equal(r.status,403);assert.equal((await data(r)).reason,'intake_authored_by_worker');
});
