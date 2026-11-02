import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
const inDO: any = runInDurableObject;
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { IntakeWatchdog, queuePolicyFrom } from '../src/intake-watchdog';
import { freshCpu } from './cpu-fixture';

const admin:any={caller:{isAdmin:true,actor:'hub:service:intake-watchdog',kind:'human'},scopes:['admin']};
const policy=()=>queuePolicyFrom({HUB_INTAKE_QUEUE_ENABLED:'true',HUB_INTAKE_QUEUE_PROJECTS:'*'});
const req=(path:string,data:any,method='POST')=>new Request('https://hub.test'+path,{method,body:JSON.stringify(data)});

async function fixture(scenario:'quota'|'quarantine'|'retryable-health') {
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('recovery-placement-'+crypto.randomUUID()));
 await inDO(stub,async(h:any,state:any)=>{
  const now=Date.now(),at=new Date(now).toISOString(),old=new Date(now-3600000).toISOString();
  const tx=(f:any)=>state.storage.transactionSync(f);
  const fleet=new FleetService(h.sql,tx,()=>now);fleet.init();
  const sessions=new SessionControlService(h.sql,tx,()=>now,{reserve:(i:any,a:any)=>fleet.reserve(i,a)},{env:h.env});sessions.init();
  const accounts=[['shared-codex','codex','codex'],['shared-zai','zai','zcodex'],['shared-gemini','gemini','antigravity']];
  const caps={engines:accounts.map(a=>a[2]),fleet_modes:['headless','interactive'],workspaces:['demo'],cpu_count:8,
   fleet_goal_routes:accounts.map(([account,,engine])=>({engine,account,profile:null}))};
  h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES(?,?,?,?,?,?,0,?)',
   'fixture','fixture','fixture',at,at,JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[1],cpu_usage:freshCpu(at),gate:{launch_allowed:true,at}}),JSON.stringify(caps));
  const configured=await fleet.handle(req('/api/fleet/accounts',{
   accounts:accounts.map(([label,provider])=>({label,provider,max_concurrency:8,reserve_percent:15,enabled:true})),
   routes:accounts.map(([account,,engine])=>({machine_id:'fixture',engine,account,profile:null}))},'PUT'),admin);
  expect(configured.status).toBe(200);
  const quota=(pct:number)=>({status:'ok',observed_at:at,details:{limit_reached:pct===0},windows:{five_hour:{present:true,percent_remaining:pct,reset_at:new Date(now+18000000).toISOString()}}});
  const host:any={caller:{machineId:'fixture',kind:'host',actor:'machine:fixture'},scopes:['host']};
  const subscriptions=await fleet.handle(req('/api/fleet/subscriptions',{subscriptions:accounts.map(([account,service])=>({account,service,plan:'pro',quota:quota(scenario==='quota'&&service==='zai'?0:80)}))},'PUT'),host);
  expect(subscriptions.status).toBe(200);
  h.sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,routes_json) VALUES(?,?,?,?,?,?,?,?)',
   'fixture','1.0',at,now,at,now,'ok',JSON.stringify(Object.fromEntries(accounts.map(([,provider])=>[provider,quota(80)]))));
  const task={id:crypto.randomUUID(),title:'Recovery integration',project:'demo',status:'open',version:1,created_at:now-3600000,updated_at:now,
   acceptance_criteria:'Synthetic canonical recovery placement',history:[]};
  h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',task.id,JSON.stringify(task));
  const previous=crypto.randomUUID(),engine=scenario==='quota'?'zcodex':'antigravity';
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
   VALUES(?,'fixture',?,?,?,'headless',?,?,?)`,previous,previous,JSON.stringify({intake_task_id:task.id,workspace:'demo'}),engine,scenario==='quota'?'stopped':'failed',old,at);
  if(scenario==='quota'){
   const reapedLaunch=crypto.randomUUID();
   h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at,verification_status,exit_reason)
    VALUES(?,'fixture','human','quota reap',?,'{}','fixture','zcodex','shared-zai','headless','demo','test','exited','quota-reaped-native',?,?,'verified','quota_exhausted')`,reapedLaunch,reapedLaunch,old,at);
   h.sql.exec('UPDATE session_control_sessions SET fleet_launch_id=? WHERE id=?',reapedLaunch,previous);
  }
  if(scenario!=='quota')for(let i=0;i<3;i++){
   const id=crypto.randomUUID();h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,verification_status,exit_reason)
    VALUES(?,'fixture','human','test',?,'{}','fixture','antigravity','shared-gemini','headless','demo','test','failed',?,?,'not_started','launcher_command_failed')`,id,id,old,new Date(now-60000+i).toISOString());
  }
  if(scenario==='retryable-health')h.sql.exec("INSERT INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES('antigravity','fixture','unavailable','engine_auth_expired',?)",new Date(now-7200000).toISOString());
  const starts:any[]=[];
  const watchdog=new IntakeWatchdog(h.sql,h.intakeService,tx,{policy,start:async input=>{
   const r=await sessions.handle(req('/api/sessions',input),admin);const body:any=await r.json();starts.push({status:r.status,body});return {status:r.status,body};
  }},()=>now);watchdog.init();await watchdog.tick();
  expect(starts).toHaveLength(1);expect(starts[0].status,JSON.stringify(starts[0])).toBe(201);
  const replacement:any=h.sql.exec('SELECT s.engine,l.account FROM session_control_sessions s JOIN fleet_launches l ON l.id=s.fleet_launch_id WHERE s.id=?',starts[0].body.id).one();
  expect(replacement).toMatchObject({engine:'codex',account:'shared-codex'});
  if(scenario==='quota'){
   expect(replacement.account).not.toBe('shared-zai');
   expect(h.sql.exec("SELECT COUNT(*) AS n FROM fleet_engine_health WHERE engine='zcodex' AND status='unavailable'").one().n).toBe(0);
  }
  else{
   expect(h.sql.exec("SELECT status,reason FROM fleet_engine_health WHERE machine_id='fixture' AND engine='antigravity'").one()).toMatchObject({status:'unavailable',reason:'launch_failures_quarantined'});
   const plan=fleet.plan({workspace:'demo',engine:'antigravity',mode:'headless',prompt:'Test quarantine gate',idempotency_key:crypto.randomUUID()});
   expect(plan.error,JSON.stringify(plan)).toBeUndefined();expect(plan.selected).toBeNull();expect(plan.rejected).toContainEqual(expect.objectContaining({engine:'antigravity',reason:'launch_failures_quarantined'}));
  }
 });
}

it('restarts a terminal zcodex five-hour quota reap immediately on another account through canonical admission',async()=>fixture('quota'));
it('three antigravity launch failures quarantine that host and recover through another eligible engine',async()=>fixture('quarantine'));

it('replaces an expired retryable health hold with probe-only quarantine after three new launcher failures',async()=>fixture('retryable-health'));
