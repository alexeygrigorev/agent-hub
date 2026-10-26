import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
const uuid=()=>crypto.randomUUID();
async function setup(fn: (x:any)=>Promise<void>) {
 const stub=(env as unknown as Env).HUB.get((env as unknown as Env).HUB.idFromName('worker-'+uuid()));
 const id=uuid(), task=uuid(), session=uuid(), other=uuid();
 const token='hubt_host_fixture_'+uuid(), hash=await sha256(token), now=new Date().toISOString();
 await runInDurableObject<Hub, any>(stub,async (h:any)=>{
  h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('worker-host','host','host',?)",now);
  h.sql.exec("INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES('worker-host-cred','host',?,'fixture-refresh','host-family','worker-host','worker-host','[\"host\"]','fixture',?,?)",hash,now,new Date(Date.now()+3600000).toISOString());
  h.sql.exec("UPDATE machines SET credential_id='worker-host-cred' WHERE id='worker-host'");
  for(const t of [task,other]) h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',t,JSON.stringify({id:t,project:'agent-hub',status:'open',version:1,created_at:Date.now(),updated_at:Date.now(),title:'fixture',scope:null,history:[{id:uuid(),kind:'intake',text:'fixture',authority:'untrusted_task_data',actor:'tester',captured_at:Date.now()}],acceptance_criteria:'read own task, append and forward'}));
  h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at) VALUES(?,'tester','human','s',?,'{}','worker-host','opencode','shared-muse','headless','/repo','task','starting',?,?)`,id,'key-'+id,now,now);
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at) VALUES(?,'tester',?,?,?,'worker-host','opencode','headless','starting',?,?)`,session,'session-'+session,JSON.stringify({intake_task_id:task,workspace:'/repo'}),id,now,now);
 });
 const call=(path:string,body?:any,bearer=token)=>stub.fetch('https://hub.test'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+bearer,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const issue=await call('/api/fleet/launches/'+id+'/worker-credential',{});
 expect(issue.status,await issue.clone().text()).toBe(200);
 const worker:any=await issue.json();
 await fn({stub,id,task,session,other,worker,call});
}
describe('scoped durable worker access',()=>{
 it('reads only its intake and appends only notes/report/blocker, never decisions or close',()=>setup(async({task,other,worker,call}:any)=>{
  const api=(p:string,b?:any)=>call(p,b,worker.access_token);
  expect((await api('/api/intake/tasks/'+task)).status).toBe(200);
  expect((await api('/api/intake/tasks/'+task+'/history')).status).toBe(200);
  expect((await api('/api/intake/tasks/'+other)).status).toBe(403);
  expect((await api('/api/intake/tasks')).status).toBe(403);
  let version=1;
  for(const kind of ['note','report','blocker']) {
   const res=await api('/api/intake/tasks/'+task+'/events',{key:uuid(),expected_version:version,event:kind==='report'?{kind,reference:'worker report',source:{type:'unknown',reference:'test'}}:{kind,text:'worker '+kind,source:{type:'unknown',reference:'test'}}});
   expect(res.status,await res.clone().text()).toBe(200);version++;
  }
  for(const kind of ['scope_decision','approval_decision','done_note','resolution']) expect((await api('/api/intake/tasks/'+task+'/events',{key:uuid(),expected_version:version,event:{kind,text:'forbidden'}})).status).toBe(403);
  expect((await api('/api/intake/tasks/'+task+'/close',{key:uuid(),expected_version:version})).status).toBe(403);
  expect((await api('/api/fleet/launches',{})).status).toBe(403);
  expect((await api('/api/bus/claims')).status).toBe(403);
 }));
 it('retains binding through normal refresh and fails closed after scope expiry',()=>setup(async({stub,task,other,worker,call}:any)=>{
  const refreshed=await stub.fetch('https://hub.test/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:worker.refresh_token})});
  expect(refreshed.status,await refreshed.clone().text()).toBe(200);
  const pair:any=await refreshed.json();
  expect((await call('/api/intake/tasks/'+task,undefined,pair.access_token)).status).toBe(200);
  expect((await call('/api/intake/tasks/'+other,undefined,pair.access_token)).status).toBe(403);
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec('UPDATE worker_access SET expires_at=?',Date.now()-1));
  expect((await call('/api/intake/tasks/'+task,undefined,pair.access_token)).status).toBe(403);
 }));
 it('renews expired access only within its hard worker grant and rejects replay',()=>setup(async({stub,task,worker,call}:any)=>{
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec("UPDATE credentials SET expires_at=? WHERE kind='agent'",new Date(Date.now()-1000).toISOString()));
  expect((await call('/api/intake/tasks/'+task,undefined,worker.access_token)).status).toBe(401);
  const refresh=(token:string)=>stub.fetch('https://hub.test/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:token})});
  const res=await refresh(worker.refresh_token);expect(res.status,await res.clone().text()).toBe(200);
  const pair:any=await res.json();expect((await call('/api/intake/tasks/'+task,undefined,pair.access_token)).status).toBe(200);
  expect((await refresh(worker.refresh_token)).status).toBe(400);
  expect((await call('/api/intake/tasks/'+task,undefined,pair.access_token)).status).toBe(401);
 }));
 it('denies renewal after hard expiry even with an otherwise valid rotating token',()=>setup(async({stub,worker}:any)=>{
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec('UPDATE worker_access SET expires_at=?',Date.now()-1));
  const res=await stub.fetch('https://hub.test/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:worker.refresh_token})});
  expect(res.status).toBe(400);
 }));
 it('restricts chained sessions to policy routed headless children on the same intake/workspace',()=>setup(async({stub,session,task,other,worker,call}:any)=>{
  const start=(body:any)=>call('/api/sessions',{key:uuid(),workspace:'/repo',intake_task_id:task,brief:'small real followup',...body},worker.access_token);
  for(const bad of [{workspace:'/else'},{intake_task_id:other},{mode:'interactive'},{parent_id:uuid()},{engine:'opencode',engine_override_reason:'no'},{machine:'worker-host'}]) expect([400,403]).toContain((await start(bad)).status);
  const res=await start({}); const data:any=await res.json();
  // No route is advertised by this fixture, so normal routing should hold the
  // legitimate child, after recording its real lineage, rather than bypass it.
  expect(res.status,JSON.stringify(data)).toBe(409);
  await runInDurableObject<Hub, any>(stub,(h:any)=>{
   const child=h.sql.exec('SELECT parent_id,request_json FROM session_control_sessions WHERE id!=?',session).toArray()[0];
   expect(child.parent_id).toBe(session);expect(JSON.parse(child.request_json).intake_task_id).toBe(task);
  });
 }));
});
