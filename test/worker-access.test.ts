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
 it.each(['headless','interactive'])('%s reads quota and own resource details without gaining fleet controls', mode=>setup(async({stub,session,task,other,worker,call}:any)=>{
  const api=(p:string,b?:any)=>call(p,b,worker.access_token);
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   const at=new Date().toISOString();
   for(const id of [session,other]) h.sql.exec(`INSERT INTO session_resource_usage
    (session_id,machine_id,native_id,project,engine,kind,started_at,state,observed_at)
    VALUES(?,'worker-host',?,'agent-hub','codex','goal',?,'running',?)`,id,id,at,at);
  });
  expect((await api('/api/intake/tasks/'+task)).status).toBe(200);
  expect((await api('/api/intake/tasks/'+task.slice(0,8))).status).toBe(200);
  expect((await api('/api/intake/tasks/'+other)).status).toBe(403);
  for(const path of ['/api/quota?view=fleet','/api/quota?view=fleet&provider=codex&account=codex-orin']) {
   const res=await api(path);expect(res.status,await res.clone().text()).toBe(200);
   expect(await res.json()).toHaveProperty('pools');
   expect((await api(path,{})).status).toBe(403);
  }
  for(const path of ['/api/fleet/resources','/api/fleet/resources/sessions']) {
   const res=await api(path);expect(res.status,await res.clone().text()).toBe(200);
   const data:any=await res.json();expect(data.sessions.map((s:any)=>s.session_id)).toEqual([session]);
   expect(JSON.stringify(data)).not.toContain(other);
   expect((await api(path,{})).status).toBe(403);
  }
  for(const path of ['/api/quota','/api/quota?view=fleet&view=host','/api/quota/server',
   '/api/quota/kill-switch','/api/fleet/resources/watchdog','/api/fleet/resources/config',
   '/api/fleet/resources/reaps','/api/fleet/resources/throttles','/api/fleet'])
   expect([403,404]).toContain((await api(path)).status);
  await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE worker_access SET expires_at=?',Date.now()-1));
  expect((await api('/api/quota?view=fleet')).status).toBe(403);
  expect((await api('/api/fleet/resources')).status).toBe(403);
 },mode));

 it('AC5 triage reviewer: only live requested intakes, fills missing AC only, access ends on its verdict or session end',()=>setup(async({stub,session,other,worker,call}:any)=>{
  const api=(p:string,b?:any)=>call(p,b,worker.access_token);
  const blank=uuid(), third=uuid(), later=uuid();
  await runInDurableObject<Hub, any>(stub,(h:any)=>{
   for (const [id,title] of [[blank,'missing AC'],[third,'not requested'],[later,'second requested']])
    h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',id,JSON.stringify({id,project:'agent-hub',status:'open',version:1,created_at:Date.now(),updated_at:Date.now(),title,scope:null,history:[],acceptance_criteria:null}));
   for (const id of [other,blank,later]) h.sql.exec("INSERT INTO intake_triage_requests(task_id,request_key,session_id,state,requested_at) VALUES(?,?,?,'requested',?)",id,'triage-k',session,Date.now()-1000);
  });
  expect((await api('/api/intake/tasks/'+blank)).status).toBe(200);
  expect((await api('/api/intake/tasks/'+blank.slice(0,8))).status).toBe(403); // exact ids only
  expect((await api('/api/intake/tasks/'+third)).status).toBe(403);
  // Existing AC is never replaced; missing AC may be filled, together with a project fix.
  expect((await api('/api/intake/tasks/'+other,{key:'t-over',expected_version:1,acceptance_criteria:'replaced'})).status).toBe(403);
  const ac=await api('/api/intake/tasks/'+blank,{key:'t-ac',expected_version:1,acceptance_criteria:'AC1 written by triage',project:'machine-management'});
  expect(ac.status,await ac.clone().text()).toBe(200);
  const v=(await ac.json() as any).task.version;
  // A lost reply retried with the same key replays the saved receipt; a new key may not overwrite.
  const replay=await api('/api/intake/tasks/'+blank,{key:'t-ac',expected_version:1,acceptance_criteria:'AC1 written by triage',project:'machine-management'});
  expect(replay.status,await replay.clone().text()).toBe(200);
  expect((await replay.json() as any).task.version).toBe(v);
  expect((await api('/api/intake/tasks/'+blank,{key:'t-ac2',expected_version:v,acceptance_criteria:'second fill'})).status).toBe(403);
  for (const bad of [{key:'t-st',expected_version:v,status:'done'},{key:'t-ti',expected_version:v,title:'renamed'},
    {key:'t-mix',expected_version:v,project:'aws-infra',status:'cancelled',reason:'x'}])
   expect((await api('/api/intake/tasks/'+blank,bad)).status).toBe(403);
  expect((await api('/api/intake/tasks/'+blank+'/events',{key:'t-dec',expected_version:v,event:{kind:'decision',text:'x'}})).status).toBe(403);
  expect((await api('/api/intake/tasks/'+third,{key:'t-3',expected_version:1,project:'aws-infra'})).status).toBe(403);
  // The verdict note itself revokes access immediately, without waiting for the hourly ledger update.
  const note=await api('/api/intake/tasks/'+blank+'/events',{key:'t-note',expected_version:v,event:{kind:'note',text:'TRIAGE ACTUAL — backlog triage (hourly, Claude Opus)'}});
  expect(note.status,await note.clone().text()).toBe(200);
  expect((await api('/api/intake/tasks/'+blank)).status).toBe(403);
  expect((await api('/api/intake/tasks/'+blank,{key:'t-after',expected_version:v+1,project:'relay'})).status).toBe(403);
  expect((await api('/api/intake/tasks/'+later)).status).toBe(200);
  // A reviewer whose session ended keeps no batch access.
  await runInDurableObject<Hub, any>(stub,(h:any)=>h.sql.exec("UPDATE session_control_sessions SET state='exited' WHERE id=?",session));
  expect((await api('/api/intake/tasks/'+later)).status).toBe(403);
 }));
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
 it('files follow-up intakes with provenance, reads/notes only its own creations, and names cross-intake denials (34d0271b)',()=>setup(async({stub,id,session,task,other,worker,call}:any)=>{
  const api=(p:string,b?:any)=>call(p,b,worker.access_token);
  const code=async(r:Response)=>((await r.json()) as any).error;
  const src={type:'unknown',reference:'test'};
  // Own intake: links/context are the same read view as show.
  for(const sfx of ['','/history','/links','/context']) expect((await api('/api/intake/tasks/'+task+sfx)).status).toBe(200);
  const made=await api('/api/intake/tasks',{key:uuid(),project:'dataops',title:'follow-up',text:'blocker found',source:src});
  expect(made.status,await made.clone().text()).toBe(200);
  const created:any=(await made.json() as any).task;
  expect(created.project).toBe('dataops');
  expect(created.created_by).toMatchObject({kind:'worker_session',hub_session_id:session,fleet_launch_id:id,parent_intake_id:task});
  for(const sfx of ['','/history','/links','/context']) expect((await api('/api/intake/tasks/'+created.id+sfx)).status).toBe(200);
  let version=1;
  for(const kind of ['note','report','blocker']) {
   const res=await api('/api/intake/tasks/'+created.id+'/events',{key:uuid(),expected_version:version,event:kind==='report'?{kind,reference:'r',source:src}:{kind,text:'worker '+kind,source:src}});
   expect(res.status,await res.clone().text()).toBe(200);version++;
  }
  expect((await api('/api/intake/tasks/'+created.id+'/events',{key:uuid(),expected_version:version,event:{kind:'decision',text:'no',source:src}})).status).toBe(403);
  // Status/AC updates and close stay denied on created and unrelated intakes, with a specific code.
  for(const target of [created.id,other]) {
   for(const body of [{status:'done'},{acceptance_criteria:'changed'},{title:'x'}]) {
    const res=await api('/api/intake/tasks/'+target,{key:uuid(),expected_version:version,...body});
    expect(res.status).toBe(403);expect(await code(res)).toBe('cross_intake_forbidden');
   }
   const close=await api('/api/intake/tasks/'+target+'/close',{key:uuid(),expected_version:version});
   expect(close.status).toBe(403);expect(await code(close)).toBe('cross_intake_forbidden');
   const del=await stub.fetch('https://hub.test/api/intake/tasks/'+target,{method:'DELETE',headers:{Authorization:'Bearer '+worker.access_token}});
   expect(del.status).toBe(403);
  }
  for(const sfx of ['','/history','/links']) {
   const res=await api('/api/intake/tasks/'+other+sfx);expect(res.status).toBe(403);expect(await code(res)).toBe('cross_intake_forbidden');
  }
  const noteOther=await api('/api/intake/tasks/'+other+'/events',{key:uuid(),expected_version:1,event:{kind:'note',text:'x',source:src}});
  expect(noteOther.status).toBe(403);expect(await code(noteOther)).toBe('cross_intake_forbidden');
  // Own intake status changes remain a scope denial, not a cross-intake one.
  const ownClose=await api('/api/intake/tasks/'+task,{key:uuid(),expected_version:1,status:'done'});
  expect(ownClose.status).toBe(403);expect(await code(ownClose)).toBe('worker_scope_forbidden');
  // Rules and grants stay operator-only.
  for(const [path,body] of [['/api/intake/rules',undefined],['/api/intake/grants',undefined],
   ['/api/intake/rules',{key:uuid(),id:'r',expected_version:0,text:'x',source:src,decision:'x'}],
   ['/api/intake/grants',{key:uuid(),credential_id:'c',scope:'all',access:'write',expires_at:Date.now()+1000,source:src}]] as any[]) {
   const res=await api(path,body);expect(res.status).toBe(403);expect(await code(res)).toBe('worker_scope_forbidden');
  }
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   const data=JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?',created.id).toArray()[0].data);
   expect(data.status).toBe('open');expect(data.acceptance_criteria??null).toBeNull();
   expect(JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?',other).toArray()[0].data).status).toBe('open');
  });
  // A different worker session never sees another session's creations.
  const id2=uuid(), session2=uuid(), now=new Date().toISOString();
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at) VALUES(?,'tester','human','s',?,'{}','worker-host','opencode','shared-muse','headless','/repo','task','starting',?,?)`,id2,'key-'+id2,now,now);
   h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at) VALUES(?,'tester',?,?,?,'worker-host','opencode','headless','starting',?,?)`,session2,'session-'+session2,JSON.stringify({intake_task_id:other,workspace:'/repo'}),id2,now,now);
  });
  const issued=await call('/api/fleet/launches/'+id2+'/worker-credential',{});expect(issued.status).toBe(200);
  const worker2:any=await issued.json();
  const peek=await call('/api/intake/tasks/'+created.id,undefined,worker2.access_token);
  expect(peek.status).toBe(403);expect(await code(peek)).toBe('cross_intake_forbidden');
  // Workers cannot author AC/verification (auto-pickup would turn it into launch
  // authority) or forge provenance; refused creates leave no ledger row.
  for(const extra of [{acceptance_criteria:'do things'},{verification:'required'},{created_by:{hub_session_id:session2}}]) {
   const res=await api('/api/intake/tasks',{key:uuid(),project:'agent-hub',title:'x',text:'x',source:src,...extra});
   expect([400,403]).toContain(res.status);
  }
  for(const extra of [{acceptance_criteria:'a'},{verification:'required'}])
   expect(await api('/api/intake/tasks',{key:uuid(),project:'agent-hub',title:'x',text:'x',source:src,...extra}).then(code)).toBe('worker_ac_forbidden');
  await runInDurableObject<Hub,any>(stub,(h:any)=>expect(h.sql.exec('SELECT COUNT(*) AS n FROM worker_created_intakes').toArray()[0].n).toBe(1));
  // Per-session creation cap.
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   for(let i=1;i<25;i++) h.sql.exec('INSERT INTO worker_created_intakes(task_id,session_id,launch_id,parent_task_id,created_at) VALUES(?,?,?,?,?)',uuid(),session,id,task,Date.now());
  });
  const capped=await api('/api/intake/tasks',{key:uuid(),project:'agent-hub',title:'one more',text:'x',source:src});
  expect(capped.status).toBe(429);expect(await code(capped)).toBe('worker_create_limit');
  // Access to created intakes ends with the worker grant.
  await runInDurableObject<Hub,any>(stub,(h:any)=>h.sql.exec('UPDATE worker_access SET expires_at=? WHERE launch_id=?',Date.now()-1,id));
  expect((await api('/api/intake/tasks/'+created.id)).status).toBe(403);
  expect((await api('/api/intake/tasks',{key:uuid(),project:'agent-hub',title:'late',text:'x',source:src})).status).toBe(403);
 }));
 it('extends read and note scope to a continuation only through a server-verified session link (34d0271b note 083c1588)',()=>setup(async({stub,session,task,worker,call}:any)=>{
  const api=(p:string,b?:any)=>call(p,b,worker.access_token);
  const code=async(r:Response)=>((await r.json()) as any).error;
  const src={type:'unknown',reference:'test'};
  const native=uuid(), [cont,other2,launched,later]=[uuid(),uuid(),uuid(),uuid()];
  const link=(extra:any)=>({id:uuid(),kind:'link',engine:'codex',tier:null,host:null,hub:null,routing_job_id:null,provenance:src,actor:'tester',captured_at:Date.now(),...extra});
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   h.sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify({native_id:native,tag:'hub-x',workspace:'/repo'}),session);
   const put=(id:string,history:any[])=>h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',id,JSON.stringify({id,project:'agent-hub',status:'open',version:1,created_at:Date.now(),updated_at:Date.now(),title:'continuation',scope:null,acceptance_criteria:'ac',history:[{id:uuid(),kind:'intake',text:'x',authority:'untrusted_task_data',actor:'tester',captured_at:Date.now()},...history]}));
   // Operator verify_live link to the native session; Hub launch link by session id; a client-supplied unverified link.
   put(cont,[link({native_id:native,hub_session_id:null,evidence:'external_live_verified',authority:'host_census_verified'})]);
   put(launched,[link({native_id:null,hub_session_id:session,evidence:'hub_launch_verified',authority:'hub_launch_verified'})]);
   put(other2,[link({native_id:native,hub_session_id:session,evidence:'external_unverified_not_launch_proof',authority:'untrusted_task_data'})]);
   // A verified link that arrives in a later write is indexed by the projection update trigger.
   put(later,[]);
   const row=JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?',later).one().data as string);
   expect(h.sql.exec('SELECT count(*) AS n FROM intake_session_links WHERE task_id=?',later).one().n).toBe(0);
   row.history.push(link({native_id:native,hub_session_id:null,evidence:'external_live_verified',authority:'host_census_verified'}));
   h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(row),later);
  });
  for(const id of [cont,launched,later]) {
   for(const sfx of ['','/history']) expect((await api('/api/intake/tasks/'+id+sfx)).status).toBe(200);
   const note=await api('/api/intake/tasks/'+id+'/events',{key:uuid(),expected_version:1,event:{kind:'note',text:'owner note on continuation',source:src}});
   expect(note.status,await note.clone().text()).toBe(200);
   for(const body of [{status:'done'},{acceptance_criteria:'changed'}]) {
    const res=await api('/api/intake/tasks/'+id,{key:uuid(),expected_version:2,...body});
    expect(res.status).toBe(403);expect(await code(res)).toBe('cross_intake_forbidden');
   }
   expect((await api('/api/intake/tasks/'+id+'/events',{key:uuid(),expected_version:2,event:{kind:'decision',text:'no',source:src}})).status).toBe(403);
  }
  const untrusted=await api('/api/intake/tasks/'+other2);
  expect(untrusted.status).toBe(403);expect(await code(untrusted)).toBe('cross_intake_forbidden');
  // A one-time backfill rebuilds the index from existing open intakes.
  await runInDurableObject<Hub,any>(stub,(h:any)=>{
   h.sql.exec('DROP TABLE intake_session_links');h.intakeService.init();
   const rows=h.sql.exec('SELECT task_id FROM intake_session_links WHERE session_key IN (?,?)',native,session).toArray().map((r:any)=>r.task_id);
   expect(new Set(rows)).toEqual(new Set([cont,launched,later]));
   // Upgrade from the first release: its full-history triggers on intake_tasks are replaced.
   for(const op of ['insert','update']) h.sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_session_links_${op} AFTER ${op.toUpperCase()} ON intake_tasks BEGIN SELECT 1; END`);
   h.intakeService.init();
   const triggers=h.sql.exec("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE 'intake_session_links%' ORDER BY name").toArray().map((r:any)=>r.name+'@'+r.tbl_name);
   expect(triggers).toEqual(['intake_session_links_delete@intake_tasks','intake_session_links_projection_insert@intake_task_projection','intake_session_links_projection_update@intake_task_projection']);
  });
  expect((await api('/api/intake/tasks/'+cont)).status).toBe(200);
  expect((await api('/api/intake/tasks/'+task)).status).toBe(200);
 }));
});
