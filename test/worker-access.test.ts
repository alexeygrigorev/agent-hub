import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
const uuid=()=>crypto.randomUUID();
async function setup(fn: (x:any)=>Promise<void>, mode = 'headless') {
 const stub=(env as unknown as Env).HUB.get((env as unknown as Env).HUB.idFromName('worker-'+uuid()));
 const id=uuid(), task=uuid(), session=uuid(), other=uuid();
 const token='hubt_host_fixture_'+uuid(), hash=await sha256(token), now=new Date().toISOString();
 await runInDurableObject<Hub, any>(stub,async (h:any)=>{
  h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('worker-host','host','host',?)",now);
  h.sql.exec("INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES('worker-host-cred','host',?,'fixture-refresh','host-family','worker-host','worker-host','[\"host\"]','fixture',?,?)",hash,now,new Date(Date.now()+3600000).toISOString());
  h.sql.exec("UPDATE machines SET credential_id='worker-host-cred' WHERE id='worker-host'");
  for(const t of [task,other]) h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',t,JSON.stringify({id:t,project:'agent-hub',status:'open',version:1,created_at:Date.now(),updated_at:Date.now(),title:'fixture',scope:null,history:[{id:uuid(),kind:'intake',text:'fixture',authority:'untrusted_task_data',actor:'tester',captured_at:Date.now()}],acceptance_criteria:'read own task, append and forward'}));
  h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at) VALUES(?,'tester','human','s',?,'{}','worker-host','opencode','shared-muse',?,'/repo','task','starting',?,?)`,id,'key-'+id,mode,now,now);
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at) VALUES(?,'tester',?,?,?,'worker-host','opencode',?,'starting',?,?)`,session,'session-'+session,JSON.stringify({intake_task_id:task,workspace:'/repo'}),id,mode,now,now);
 });
 const call=(path:string,body?:any,bearer=token)=>stub.fetch('https://hub.test'+path,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+bearer,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const issue=await call('/api/fleet/launches/'+id+'/worker-credential',{});
 expect(issue.status,await issue.clone().text()).toBe(200);
 const worker:any=await issue.json();
 await fn({stub,id,task,session,other,worker,call});
}
describe('scoped durable worker access',()=>{
 it('reads only shared usage dimensions without granting other monitoring or writes',()=>setup(async({stub,worker,call}:any)=>{
  await runInDurableObject<Hub, any>(stub,(h:any)=>{
   const now=Date.now();
   h.hostHistoryService.record('quota_window','shared-codex','',now,{'5h':50,'7d':25},'fixture');
   h.hostHistoryService.record('tokens','codex / shared-codex / agent-hub','worker-host',now,{input_tokens:100},'fixture');
   h.hostHistoryService.record('host','private-host','private-host',now,{load1:5},'fixture');
   h.hostHistoryService.flush();
  });
  for (const path of ['/api/monitoring/history']) {
   const response=await call(path,undefined,worker.access_token);
   expect(response.status).toBe(200);
   const data:any=await response.json();
   expect(data.series.some((s:any)=>s.kind==='quota_window' && s.entity==='shared-codex')).toBe(true);
   expect(data.series.every((s:any)=>['quota_window','quota','tokens'].includes(s.kind))).toBe(true);
   for(const kind of ['quota_window','quota','tokens']) {
    const res=await call(path+'?kind='+kind,undefined,worker.access_token);
    expect(res.status).toBe(200);
    expect((await res.json() as any).series.every((s:any)=>s.kind===kind)).toBe(true);
   }
   expect((await call(path+'?kind=host',undefined,worker.access_token)).status).toBe(403);
   expect((await call(path+'?kind=quota_window&kind=host',undefined,worker.access_token)).status).toBe(403);
   expect((await call(path,{},worker.access_token)).status).toBe(403);
  }
  for(const path of ['/api/monitoring/tokens','/api/monitoring/health','/api/quota','/api/monitoring/history/other'])
   expect([403,404]).toContain((await call(path,undefined,worker.access_token)).status);
 }));

 it.each(['headless','interactive'])('%s reads only its intake and appends only notes/report/blocker, never decisions or close',(mode)=>setup(async({task,other,worker,call}:any)=>{
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
 }, mode));
 it.each(['headless','interactive'])('%s issuance refuses another host, a missing link, and an unbound running launch',(mode)=>setup(async({stub,id,call}:any)=>{
  const issue=()=>call('/api/fleet/launches/'+id+'/worker-credential',{});
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec("UPDATE fleet_launches SET machine_id='other-host' WHERE id=?",id));
  expect((await issue()).status).toBe(409);
  await runInDurableObject<Hub, any>(stub,(h:any)=>{
   h.sql.exec("UPDATE fleet_launches SET machine_id='worker-host' WHERE id=?",id);
   h.sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE fleet_launch_id=?",id);
  });
  expect((await issue()).status).toBe(409);
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec("UPDATE fleet_launches SET state='running' WHERE id=?",id));
  expect((await issue()).status).toBe(409);
 }, mode));
 it.each(['headless','interactive'])('%s host repairs a running exact native profile with the same intake scope', mode=>setup(async({stub,id,session,task,other,worker,call}:any)=>{
  const native=uuid(), tag=(mode==='headless'?'task-hub-':'hub-')+id;
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   h.sql.exec("UPDATE fleet_launches SET state='running',native_id=? WHERE id=?",native,id);
   h.sql.exec("UPDATE session_control_sessions SET state='running',binding_json=? WHERE id=?",JSON.stringify({native_id:native,tag,workspace:'/repo'}),session);
  });
  const issue=()=>call('/api/fleet/launches/'+id+'/worker-credential',{});
  expect((await call('/api/fleet/launches/'+id+'/worker-credential',{},worker.access_token)).status).toBe(403);
  const repaired=await issue(); expect(repaired.status,await repaired.clone().text()).toBe(200);
  const profile:any=await repaired.json(); expect(profile.worker_launch_id).toBe(id);
  expect((await call('/api/intake/tasks/'+task,undefined,profile.access_token)).status).toBe(200);
  expect((await call('/api/intake/tasks/'+other,undefined,profile.access_token)).status).toBe(403);
  for(const binding of [null, {native_id:uuid(),tag}, {native_id:native,tag:'hub-'+uuid()}]) {
   await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify(binding),session));
   expect((await issue()).status).toBe(409);
  }
  await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify({native_id:native,tag,workspace:'/repo'}),session));
  for(const state of ['stopped','failed','finished']) {
   await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE fleet_launches SET state=? WHERE id=?',state,id));
   expect((await issue()).status).toBe(409);
  }
 },mode));
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
 it('registers and reads its own wake without gaining self-stop or foreign access',()=>setup(async({stub,id,session,other,worker,call}:any)=>{
  await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec("UPDATE session_control_sessions SET state='running' WHERE id=?",session));
  const api=(path:string,body?:any)=>call(path,body,worker.access_token);
  const at=new Date(Date.now()+3600000).toISOString();
  const wake=await api('/api/sessions/'+session+'/wake',{key:'self-wait',at,prompt:'Resume own work'});
  expect(wake.status,await wake.clone().text()).toBe(201);
  expect(await (await api('/api/sessions/'+session)).json()).toMatchObject({state:'waiting_until',waiting_until:at});
  expect((await (await api('/api/sessions/'+session+'/wakes')).json() as any).wakes).toHaveLength(1);
  expect((await api('/api/sessions/'+session+'/commands')).status).toBe(200);
  expect((await api('/api/sessions/'+id+'/wake',{key:'launch-ref',at,prompt:'Own launch wake'})).status).toBe(201);
  expect((await api('/api/sessions/'+id)).status).toBe(200);
  expect((await api('/api/sessions/'+session+'/stop',{key:'no-self-stop'})).status).toBe(403);
  expect((await api('/api/sessions/'+other+'/wake',{key:'foreign',at,prompt:'no'})).status).toBe(403);
 },'interactive'));
 it('lets a watcher wake only its direct parent on the same intake',()=>setup(async({stub,session,task,other,worker,call}:any)=>{
  const parent=uuid(), sibling=uuid();
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   for(const id of [parent,sibling]) h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
    VALUES(?,'parent-creator',?,?,'interactive','running',?,?)`,id,id,JSON.stringify({intake_task_id:task,workspace:'/repo'}),new Date().toISOString(),new Date().toISOString());
   h.sql.exec('UPDATE session_control_sessions SET parent_id=? WHERE id=?',parent,session);
  });
  const api=(path:string,body?:any)=>call(path,body,worker.access_token);
  const at=new Date(Date.now()+3600000).toISOString();
  expect((await api('/api/sessions/'+parent+'/wake',{key:'watcher-result',at,prompt:'Watcher result'})).status).toBe(201);
  expect((await api('/api/sessions/'+sibling+'/wake',{key:'sibling',at,prompt:'no'})).status).toBe(403);
  expect((await api('/api/sessions/'+parent+'/stop',{key:'parent-stop'})).status).toBe(403);
  expect((await api('/api/sessions/'+parent)).status).toBe(403);
  await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE session_control_sessions SET request_json=? WHERE id=?',JSON.stringify({intake_task_id:other}),parent));
  expect((await api('/api/sessions/'+parent+'/wake',{key:'wrong-task',at,prompt:'no'})).status).toBe(403);
 },'interactive'));
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
