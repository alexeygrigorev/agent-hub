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

it('AC1: intake recovery skips engines that already failed for the intake, account_blocked, or zcodex_session_failed and never pins to a single dead engine when none is eligible', async () => {
 const stub = (env as any).HUB.get((env as any).HUB.idFromName('recovery-ac1-' + crypto.randomUUID()));
 await inDO(stub, async (h: any, state: any) => {
  const now = Date.now(), at = new Date(now).toISOString(), old = new Date(now - 3600000).toISOString();
  const tx = (f: any) => state.storage.transactionSync(f);
  const fleet = new FleetService(h.sql, tx, () => now); fleet.init();
  const sessions = new SessionControlService(h.sql, tx, () => now, { reserve: (i: any, a: any) => fleet.reserve(i, a) }, { env: h.env }); sessions.init();
  const accounts: [string, string, string, boolean][] = [
   ['shared-codex', 'codex', 'codex', true],
   ['shared-go', 'go', 'opencode', false], // account_blocked
   ['shared-zai', 'zai', 'zcodex', true],
   ['shared-gemini', 'gemini', 'antigravity', true],
  ];
  const caps = {
   engines: accounts.map(a => a[2]),
   fleet_modes: ['headless', 'interactive'],
   workspaces: ['demo'],
   cpu_count: 8,
   fleet_goal_routes: accounts.map(([account, , engine]) => ({ engine, account, profile: null })),
  };
  h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES(?,?,?,?,?,?,0,?)',
   'fixture', 'fixture', 'fixture', at, at, JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at), gate: { launch_allowed: true, at } }), JSON.stringify(caps));
  const configured = await fleet.handle(req('/api/fleet/accounts', {
   accounts: accounts.map(([label, provider, , enabled]) => ({ label, provider, max_concurrency: 8, reserve_percent: 15, enabled })),
   routes: accounts.map(([account, , engine]) => ({ machine_id: 'fixture', engine, account, profile: null })),
  }, 'PUT'), admin);
  expect(configured.status).toBe(200);
  const quota = (pct: number) => ({ status: 'ok', observed_at: at, details: { limit_reached: pct === 0 }, windows: { five_hour: { present: true, percent_remaining: pct, reset_at: new Date(now + 18000000).toISOString() } } });
  const host: any = { caller: { machineId: 'fixture', kind: 'host', actor: 'machine:fixture' }, scopes: ['host'] };
  await fleet.handle(req('/api/fleet/subscriptions', { subscriptions: accounts.map(([account, service]) => ({ account, service, plan: 'pro', quota: quota(80) })) }, 'PUT'), host);
  h.sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,routes_json) VALUES(?,?,?,?,?,?,?,?)',
   'fixture', '1.0', at, now, at, now, 'ok', JSON.stringify(Object.fromEntries(accounts.map(([, provider]) => [provider, quota(80)]))));
  // zcodex is unavailable with zcodex_session_failed
  h.sql.exec("INSERT INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES('zcodex','fixture','unavailable','zcodex_session_failed',?)", at);

  const task = { id: crypto.randomUUID(), title: 'AC1 skip ineligible engines', project: 'demo', status: 'open', version: 1, created_at: now - 3600000, updated_at: now,
   acceptance_criteria: 'Never pin recovery session to ineligible engine', history: [] };
  h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', task.id, JSON.stringify(task));
  // codex already failed for this intake
  const failedCodex = crypto.randomUUID();
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
   VALUES(?,'fixture',?,?, 'codex','interactive','failed',?,?)`, failedCodex, failedCodex, JSON.stringify({ intake_task_id: task.id, workspace: 'demo', engine: 'codex' }), old, at);

  const starts: any[] = [];
  const watchdog = new IntakeWatchdog(h.sql, h.intakeService, tx, {
   policy,
   start: async input => {
    const r = await sessions.handle(req('/api/sessions', input), admin);
    const body: any = await r.json();
    starts.push({ status: r.status, body, input });
    return { status: r.status, body };
   },
  }, () => now);
  watchdog.init();
  await watchdog.tick();

  // Canonical order is codex -> opencode -> zcodex -> antigravity.
  // codex failed for intake, opencode is account_blocked, zcodex is zcodex_session_failed -> must place on antigravity!
  expect(starts).toHaveLength(1);
  expect(starts[0].status, JSON.stringify(starts[0])).toBe(201);
  expect(starts[0].body).toMatchObject({ state: 'offered', engine: 'antigravity', mode: 'headless' });
  expect(h.sql.exec("SELECT COUNT(*) AS n FROM session_control_sessions WHERE state = 'held'").one().n).toBe(0);

  // Now test when NO engine is eligible: antigravity also fails/quarantines for a second intake
  const taskAllDead = { id: crypto.randomUUID(), title: 'AC1 all engines ineligible', project: 'demo', status: 'open', version: 1, created_at: now - 3600000, updated_at: now,
   acceptance_criteria: 'Record every rejected engine when none is eligible', history: [] };
  h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', taskAllDead.id, JSON.stringify(taskAllDead));
  for (const [eng, idx] of [['codex', 0], ['antigravity', 1]] as const) {
   const sid = crypto.randomUUID();
   h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
    VALUES(?,'fixture',?,?,?,'headless','failed',?,?)`, sid, sid, JSON.stringify({ intake_task_id: taskAllDead.id, workspace: 'demo', engine: eng }), eng, old, new Date(now - 10000 + idx * 1000).toISOString());
  }
  // Direct POST /api/sessions recovery call pinned to opencode when codex & antigravity failed for intake, opencode is account_blocked, zcodex is zcodex_session_failed:
  const directRes = await sessions.handle(req('/api/sessions', {
   key: 'iw-direct-' + crypto.randomUUID(),
   intake_task_id: taskAllDead.id,
   workspace: 'demo',
   mode: 'headless',
   engine: 'opencode',
   engine_override_reason: 'Intake recovery: next engine after terminal attempts; canonical policy and quota gates remain authoritative',
   brief: 'Continue intake',
  }), admin);
  expect(directRes.status).toBe(409);
  const directBody: any = await directRes.json();
  const heldRow: any = h.sql.exec('SELECT engine, hold_json FROM session_control_sessions WHERE id = ?', directBody.id).one();
  expect(heldRow.engine).toBeNull();
  const holdData = JSON.parse(heldRow.hold_json);
  for (const eng of ['codex', 'opencode', 'zcodex', 'antigravity']) {
   expect(holdData.reason).toContain(eng);
   expect(holdData.rejected.map((r: any) => r.engine)).toContain(eng);
  }
 });
});

it('AC2: offers loop re-places or supersedes already-held recovery sessions with zcodex_session_failed or account_blocked within one cycle', async () => {
 const stub = (env as any).HUB.get((env as any).HUB.idFromName('recovery-ac2-' + crypto.randomUUID()));
 await inDO(stub, async (h: any, state: any) => {
  const now = Date.now(), at = new Date(now).toISOString(), old = new Date(now - 600000).toISOString();
  const tx = (f: any) => state.storage.transactionSync(f);
  const fleet = new FleetService(h.sql, tx, () => now); fleet.init();
  const sessions = new SessionControlService(h.sql, tx, () => now, { reserve: (i: any, a: any) => fleet.reserve(i, a) }, { env: h.env }); sessions.init();
  const accounts: [string, string, string, boolean][] = [
   ['codex-orin', 'codex', 'codex', true],
   ['shared-go', 'go', 'opencode', false], // account_blocked
   ['shared-zai', 'zai', 'zcodex', true],
   ['hub-antigravity', 'gemini', 'antigravity', true],
  ];
  const caps = {
   engines: accounts.map(a => a[2]),
   fleet_modes: ['headless', 'interactive'],
   workspaces: ['demo'],
   cpu_count: 32,
   fleet_goal_routes: accounts.map(([account, , engine]) => ({ engine, account, profile: null })),
  };
  h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES(?,?,?,?,?,?,0,?)',
   'fixture', 'fixture', 'fixture', at, at, JSON.stringify({ mem_avail_mb: 64000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at, 32), gate: { launch_allowed: true, at } }), JSON.stringify(caps));
  await fleet.handle(req('/api/fleet/accounts', {
   accounts: accounts.map(([label, provider, , enabled]) => ({ label, provider, max_concurrency: 20, reserve_percent: 15, enabled })),
   routes: accounts.map(([account, , engine]) => ({ machine_id: 'fixture', engine, account, profile: null })),
  }, 'PUT'), admin);
  const quota = (pct: number) => ({ status: 'ok', observed_at: at, details: { limit_reached: false }, windows: { five_hour: { present: true, percent_remaining: pct, reset_at: new Date(now + 18000000).toISOString() } } });
  const host: any = { caller: { machineId: 'fixture', kind: 'host', actor: 'machine:fixture' }, scopes: ['host'] };
  await fleet.handle(req('/api/fleet/subscriptions', { subscriptions: accounts.map(([account, service]) => ({ account, service, plan: 'pro', quota: quota(80) })) }, 'PUT'), host);
  h.sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,routes_json) VALUES(?,?,?,?,?,?,?,?)',
   'fixture', '1.0', at, now, at, now, 'ok', JSON.stringify(Object.fromEntries(accounts.map(([, provider]) => [provider, quota(80)]))));
  h.sql.exec("INSERT INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES('zcodex','fixture','unavailable','zcodex_session_failed',?)", at);

  const heldIds: string[] = [];
  const supersededIds: string[] = [];
  // Create 18 held recovery sessions: 9 zcodex_session_failed and 9 account_blocked
  for (let i = 0; i < 18; i++) {
   const isZcodex = i < 9;
   const isSuperseded = i === 0 || i === 9;
   const taskId = crypto.randomUUID();
   const liveId = crypto.randomUUID();
   const heldId = crypto.randomUUID();
   const task = {
    id: taskId, title: 'Recovery held ' + i, project: 'demo', status: 'open', version: 1, created_at: now - 3600000, updated_at: now,
    acceptance_criteria: 'Re-place held recovery sessions within one cycle',
    history: isSuperseded ? [{ kind: 'session', hub_session_id: heldId }, { kind: 'session', hub_session_id: liveId }] : [{ kind: 'session', hub_session_id: heldId }],
   };
   h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', taskId, JSON.stringify(task));
   if (isZcodex) {
    // Prior failed codex session so zcodex was chosen next, and zcodex also failed for this intake
    const priorFailed = crypto.randomUUID();
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
     VALUES(?,'hub:service:intake-watchdog',?,?, 'codex','interactive','failed',?,?)`,
     priorFailed, priorFailed, JSON.stringify({ intake_task_id: taskId, workspace: 'demo', engine: 'codex' }), old, old);
   }
   const eng = isZcodex ? 'zcodex' : 'opencode';
   const mode = isZcodex ? 'interactive' : 'headless';
   const reason = isZcodex ? 'zcodex_session_failed' : 'account_blocked';
   const reqPayload = {
    key: 'iw-' + heldId,
    intake_task_id: taskId,
    workspace: 'demo',
    engine: eng,
    engine_override_reason: 'Intake recovery: next engine after terminal attempts; canonical policy and quota gates remain authoritative',
    mode,
    ...(isZcodex ? { with_goal: true } : {}),
    brief: 'Continue Hub intake ' + taskId,
   };
   const holdPayload = {
    error: 'no_eligible_route',
    reason,
    rejected: [{ machine: 'fixture', engine: eng, reason }],
   };
   h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,session_type,state,hold_json,created_at,updated_at)
    VALUES(?,'hub:service:intake-watchdog',?,?,?,?,?,'held',?,?,?)`,
    heldId, reqPayload.key, JSON.stringify(reqPayload), eng, mode, mode === 'interactive' ? 'aplexer' : 'headless', JSON.stringify(holdPayload), old, old);
   if (isSuperseded) {
    supersededIds.push(heldId);
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
     VALUES(?,'hub:service:intake-watchdog',?,?, 'antigravity','headless','running',?,?)`,
     liveId, liveId, JSON.stringify({ intake_task_id: taskId, workspace: 'demo', engine: 'antigravity' }), at, at);
   } else {
    heldIds.push(heldId);
   }
  }

  // Simulate a recent reconcileHeldSessions call so the 10s throttle would normally block untargeted passes
  sessions.reconcileHeldSessions(0);

  // One cycle of the offers loop
  const offersRes = await fleet.handle(new Request('https://hub.test/api/fleet/offers', { method: 'GET' }), host);
  expect(offersRes.status).toBe(200);
  const offersBody: any = await offersRes.json();
  expect(offersBody.offers.length).toBeGreaterThan(0);

  // Every one of the 16 non-superseded recovery sessions must be offered on an eligible engine in one cycle
  for (const id of heldIds) {
   const r: any = h.sql.exec('SELECT state, engine, mode, fleet_launch_id, hold_json FROM session_control_sessions WHERE id = ?', id).one();
   expect(r.state, JSON.stringify(r)).toBe('offered');
   expect(['codex', 'antigravity']).toContain(r.engine);
   expect(r.fleet_launch_id).toBeTruthy();
   expect(r.hold_json).toBeNull();
  }
  // The 2 superseded sessions must be cancelled with superseded_by_newer_session
  for (const id of supersededIds) {
   const r: any = h.sql.exec('SELECT state, hold_json FROM session_control_sessions WHERE id = ?', id).one();
   expect(r.state).toBe('cancelled');
   expect(JSON.parse(r.hold_json).reason).toBe('superseded_by_newer_session');
  }
 });
});
