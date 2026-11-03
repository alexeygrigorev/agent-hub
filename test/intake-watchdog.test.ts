import { describe, it, expect } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import { IntakeWatchdog as BaseIntakeWatchdog, queuePolicyFrom, WATCHDOG_INTERVAL, QUEUE_RETRY_BASE_MS, QUEUE_HOLD_TTL_MS, INTAKE_IDLE_GRACE } from '../src/intake-watchdog';
import { SessionControlService } from '../src/session_control';
// Forgotten-work dispatch cases opt in explicitly; the queue suite checks the disabled default.
class IntakeWatchdog extends BaseIntakeWatchdog {
  constructor(...args: ConstructorParameters<typeof BaseIntakeWatchdog>) {
    args[3] = {policy:()=>queuePolicyFrom({HUB_INTAKE_QUEUE_ENABLED:'true',HUB_INTAKE_QUEUE_PROJECTS:'*'}),...args[3]};
    super(...args);
  }
}
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const NOW = Date.parse('2026-10-26T12:00:00Z');
function task(id = 'synthetic-task', age = 3 * 3600000): any {
  return { id, title: 'Synthetic task', project: 'demo', created_at: NOW - age, updated_at: NOW,
    status: 'open', acceptance_criteria: 'Synthetic criterion', history: [] };
}
async function fixture(fn: (h: any) => any) {
  return inDO(stub(), async (h: any) => {
    for (const table of ['intake_queue_due_cursor', 'intake_queue_admissions', 'intake_queue_movement', 'intake_restart_queue', 'intake_watchdog_meta', 'session_control_sessions', 'fleet_sessions', 'claims', 'fleet_catalog_seen']) h.sql.exec('DELETE FROM ' + table);
    return fn(h);
  });
}
function session(h: any, id: string, t: any, state: string, extra: any = {}) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
    VALUES(?,?,?,?,?,'headless',?,?,?,?,?)`, id, 'fixture', id, JSON.stringify({ intake_task_id: t.id, workspace: 'fixture', ...(extra.request || {}) }), 'codex', state,
    new Date(NOW - 3600000).toISOString(), new Date(NOW - 1800000).toISOString(), extra.machine || null, extra.binding ? JSON.stringify(extra.binding) : null);
}
describe('forgotten intake watchdog', () => {
  it('restarts a registered project by repo identity instead of reusing a host path', async () => fixture(async h => {
    const t = task('repo-queue');
    t.project = 'agent-hub';
    session(h, 'old-path-session', t, 'failed', { request: { workspace: '/home/rmthz/git/agent-hub', cwd: '/home/rmthz/git/agent-hub/docs' } });
    h.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, capabilities) VALUES ('repo-host','repo-host','repo-host',?,?,?)",
      new Date(NOW).toISOString(), new Date(NOW).toISOString(), JSON.stringify({ repo_workspaces: true }));
    let started: any = null;
    const w = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => [t] } as any, f => f(), {
      repoForProject: project => project === 'agent-hub' ? 'alexeygrigorev/agent-hub' : null,
      start: async input => { started = input; return { status: 202, body: { id: 'repo-session', state: 'queued' } }; },
    }, () => NOW);
    w.init();
    await w.tick();
    expect(started).toMatchObject({ repo: 'alexeygrigorev/agent-hub', intake_task_id: t.id });
    expect(started.workspace).toBeUndefined();
    expect(started.cwd).toBeUndefined();
  }));

  it('examines overdue restarts outside the cursor with a bounded indexed lane', async () => fixture(h => {
    const normal=Array.from({length:64},(_,i)=>task('a-'+String(i).padStart(3,'0')));
    const overdue=Array.from({length:12},(_,i)=>task('z-'+String(i).padStart(3,'0')));
    const all=[...normal,...overdue];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>all} as any,(f:any)=>f(),{},()=>NOW);w.init();
    h.sql.exec("UPDATE intake_queue_cursor SET after_id='' WHERE id=1");
    for(const t of overdue)h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?,'queued','watchdog_global_capacity',?,?,?)",t.id,NOW-60000,NOW-60000,NOW);
    const page=(w as any).batch(NOW);
    expect(page.tasks).toHaveLength(72);
    expect(page.tasks.slice(64).map((t:any)=>t.id)).toEqual(overdue.slice(0,8).map(t=>t.id));
    expect(h.sql.exec('SELECT after_id FROM intake_queue_cursor WHERE id=1').one().after_id).toBe('a-063');
    const plan=h.sql.exec("EXPLAIN QUERY PLAN SELECT task_id FROM intake_restart_queue WHERE state='queued' AND next_at<=? ORDER BY next_at,task_id LIMIT 8",NOW).toArray();
    expect(JSON.stringify(plan)).toContain('intake_queue_due');
  }));
  it('dispatches the newest candidate first under the priority order without raising admission caps', async () => fixture(async h => {
    // Intake 68145244 AC3: the auto-queue candidate order is promoted >
    // priority high > normal > low, newest first within one priority. These
    // candidates tie on priority and age, so the stable id order picks fresh-0
    // ahead of the older queued restart; the per-project cap still admits one.
    const fresh=Array.from({length:9},(_,i)=>task('fresh-'+i)), overdue=task('queued-restart');
    const all=[...fresh,overdue],started:string[]=[];
    for(const t of all)session(h,'failed-'+t.id,t,'failed');
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>all} as any,(f:any)=>f(),{
      start:async input=>{started.push(input.intake_task_id);return {status:202,body:{id:'new-'+input.intake_task_id,state:'queued'}}}
    },()=>NOW);w.init();
    h.sql.exec("UPDATE intake_queue_cursor SET after_id='' WHERE id=1");
    h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?,'queued','watchdog_global_capacity',?,?,?)",overdue.id,NOW-60000,NOW-60000,NOW);
    await w.tick();
    expect(started[0]).toBe(fresh[0].id);
    expect(started).toEqual([fresh[0].id]); // The default per-project cap remains one.
    expect(h.sql.exec("SELECT reason FROM intake_restart_queue WHERE task_id=?",fresh[1].id).one().reason).toBe('watchdog_project_capacity');
  }));
  it('advances the indexed due lane past exempt holds outside the normal cursor', async () => fixture(h => {
    const normal=Array.from({length:192},(_,i)=>task('a-'+String(i).padStart(3,'0')));
    const exempt=Array.from({length:8},(_,i)=>task('z-exempt-'+i)), ready=task('z-ready');
    const all=[...normal,...exempt,ready];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>all} as any,(f:any)=>f(),{},()=>NOW);w.init();
    h.sql.exec("UPDATE intake_queue_cursor SET after_id='' WHERE id=1");
    for(const t of exempt)h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?,'queued','watchdog_project_not_enabled',?,?,?)",t.id,NOW-60000,NOW-60000,NOW);
    h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?,'queued','watchdog_global_capacity',?,?,?)",ready.id,NOW-30000,NOW-30000,NOW);
    expect((w as any).batch(NOW).tasks.slice(64).map((t:any)=>t.id)).toEqual(exempt.map(t=>t.id));
    // A new watchdog instance resumes the durable lane, despite unchanged holds.
    const resumed=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>all} as any,(f:any)=>f(),{},()=>NOW);resumed.init();
    expect((resumed as any).batch(NOW).tasks.slice(64).map((t:any)=>t.id)).toEqual([ready.id]);
    expect((resumed as any).batch(NOW).tasks.slice(64).map((t:any)=>t.id)).toEqual(exempt.map(t=>t.id));
    const plan=h.sql.exec("EXPLAIN QUERY PLAN SELECT next_at,task_id FROM intake_restart_queue WHERE state='queued' AND (next_at,task_id)>(?,?) AND next_at<=? ORDER BY next_at,task_id LIMIT 8",NOW-60000,exempt[7].id,NOW).toArray();
    expect(JSON.stringify(plan)).toContain('intake_queue_due');
    expect(JSON.stringify(plan)).not.toContain('TEMP B-TREE');
  }));
  it('does not renew the hold deadline when admission gates alternate', async () => fixture(async h => {
    const t=task('alternating-gates');let now=NOW;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{},()=>now);w.init();
    (w as any).hold(t,'watchdog_global_capacity',now,WATCHDOG_INTERVAL);
    now+=QUEUE_HOLD_TTL_MS-WATCHDOG_INTERVAL;
    (w as any).hold(t,'workspace_configuration_required',now,WATCHDOG_INTERVAL);
    expect(h.sql.exec('SELECT held_since FROM intake_restart_queue WHERE task_id=?',t.id).one().held_since).toBe(NOW);
    now+=WATCHDOG_INTERVAL;await w.tick();
    expect(w.queue([t])[0]).toMatchObject({state:'escalated',reason:'hold_ttl_workspace_configuration_required'});
  }));

  it('uses 25-minute no-session age, ignores note updates, omits terminal archives', async () => fixture(h => {
    const w = new IntakeWatchdog(h.sql, h.intakeService, (f: any) => f(), {}, () => NOW); w.init();
    const rows = w.project([task(), task('boundary',INTAKE_IDLE_GRACE-1), {...task('closed'),status:'done',archived_at:NOW}]);
    expect(rows).toHaveLength(2); expect(rows[0].forgotten).toBe(true); expect(rows[1].forgotten).toBe(false);
  }));
  it('flags only failed sessions immediately; pending intents suppress duplicates', async () => fixture(h => {
    const t = task('fresh', 60000), pending = task('pending');
    session(h,'failure',t,'failed'); session(h,'held',pending,'held');
    const w = new IntakeWatchdog(h.sql,h.intakeService,(f: any)=>f(),{},()=>NOW);w.init();
    const p=w.project([t,pending]);expect(p[0]).toMatchObject({forgotten:true,reason:'only_failed_sessions'});expect(p[1].forgotten).toBe(false);
  }));
  it('suppresses restart with fresh native work claim; stale evidence is unknown', async () => fixture(h => {
    const t = task();
    h.sql.exec("INSERT INTO claims(id,project,scope,holder,created_at,expires_at,work_key,claim_source) VALUES('claim','demo','@work/task','native:fixture:worker[12345678]',?,?,?,'native')",NOW-3600000,NOW+3600000,'task:'+t.id);
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('fixture','12345678-1234','worker','running',?,'fixture')",new Date(NOW).toISOString());
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'active'});
    h.sql.exec('DELETE FROM fleet_catalog_seen');
    expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'unknown'});
  }));
  it('requires fresh canonical evidence for manual native links, including an unbridged active goal', async () => fixture(h => {
    const t=task();t.history=[{kind:'link',native_id:'manual-native',host:'fixture'}];
    session(h,'old-failure',t,'failed');
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('fixture','manual-native','manual-goal','running',?,'fixture')",new Date(NOW).toISOString());
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'active'});
    h.sql.exec("DELETE FROM fleet_sessions WHERE native_id='manual-native'");
    expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'unknown'});
  }));
  it('holds unknown when retained intake links outlive their canonical session rows', async () => fixture(h => {
    const t=task();t.history=[{kind:'link',hub_session_id:'missing-canonical-session'}];
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'unknown',reason:'host_observation_unknown'});
  }));
  it.each([false, true])('77f492ed: failed ebbf4374 plus live aplexer 3261084c never starts 8ecfdacc (combined link %s)', async combined => fixture(async h => {
    const t = task('77f492ed');
    session(h, 'ebbf4374', t, 'failed');
    t.history = [{kind:'link',native_id:'3261084c-1f4f-4d9d-b88c-1eb74aa1810a',
      host:'fixture',evidence:'external_unverified_not_launch_proof', ...(combined ? {hub_session_id:'ebbf4374'} : {})}];
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('fixture',?,'autoreset','running',?,'isolated-autoreset-workspace')",t.history[0].native_id,new Date(NOW-3600000).toISOString());
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    let starts=0;
    const w = new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{id:'8ecfdacc'}}}},()=>NOW);w.init();
    await w.tick();
    expect(starts).toBe(0);
    expect(w.project([t])[0]).toMatchObject({state:'active',forgotten:false});
    h.sql.exec('DELETE FROM fleet_catalog_seen');
    expect(w.project([t])[0]).toMatchObject({state:'unknown',forgotten:false});
  }));
  it('77f492ed via the operator registration path: verify_live append plus failed headless session never spawns', async () => fixture(async h => {
    // Replay the incident through the supported registration flow instead of a
    // hand-written history entry: the watchdog must honor the stored verified
    // link contract (kind link, host, native_id) produced by intake append.
    const auth = async () => ({caller:{kind:'human',isAdmin:true,actor:'human:fixture'},scopes:['admin']});
    const call = (path: string, payload?: any) => h.intakeService.handle(new Request('https://hub.test/api/intake'+path,
      {method:payload?'POST':'GET',...(payload?{body:JSON.stringify(payload)}:{})}),auth);
    const created = await (await call('/tasks',{key:'iw-verified-create',project:'demo',title:'Verified external owner',
      text:'Synthetic task',source:{type:'unknown'},acceptance_criteria:'Synthetic criterion'})).json();
    const id = created.task.id;
    const native = '3261084c-1f4f-4d9d-b88c-1eb74aa1810a';
    // The host census must be fresh for the registration API's real wall clock,
    // so this test anchors its census rows and watchdog clock to Date.now();
    // the intake itself keeps the file's aged frozen timestamps (the incident
    // was an old intake), which only feeds the watchdog's aging logic.
    const LIVE = Date.now();
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('fixture',?,'autoreset','running',?,'isolated-autoreset-workspace')",native,new Date(LIVE).toISOString());
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(LIVE).toISOString());
    const append = await call('/tasks/'+id+'/events',{key:'register-live',expected_version:1,
      event:{kind:'session',native_id:native,host:'fixture',verify_live:true}});
    expect(append.status,await append.clone().text()).toBe(200);
    session(h, 'ebbf4374', {id}, 'failed');
    let starts=0;
    const w = new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{id:'8ecfdacc'}}}},()=>LIVE);w.init();
    await w.tick();
    expect(starts).toBe(0);
    const shown = await (await call('/tasks/'+id)).json();
    expect(shown.task.payload_data.history.at(-1)).toMatchObject({kind:'link',evidence:'external_live_verified',host:'fixture',native_id:native});
    expect(shown.task.assigned_sessions).toContainEqual(expect.objectContaining({native_id:native,machine:'fixture',liveness:'live',linkage:'external_session'}));
    // Project the same internal task shape the tick consumed (history lives on
    // the canonical row, not on the API view).
    const stored = h.intakeService.statusTaskById({admin:true},id);
    expect(w.project([stored])[0]).toMatchObject({state:'active',forgotten:false,reason:'live_session_or_pending_intent'});
  }));
  it('does not interpret an unreachable bound worker as dead', async () => fixture(h => {
    const t=task();session(h,'bound',t,'running',{machine:'fixture',binding:{native_id:'native'}});
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'unknown'});
  }));
  it('queues missing AC durably, audits once, and serializes concurrent ticks', async () => fixture(async h => {
    const t={...task(),acceptance_criteria:null}; let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:200,body:{}}}},()=>NOW);w.init();
    const results=await Promise.all([w.tick(),w.tick()]);expect(starts).toBe(0);expect(results.map(r=>r.state).sort()).toEqual(['already_checked','checked']);
    expect(h.sql.exec('SELECT state,reason FROM intake_restart_queue').toArray()).toEqual([{state:'queued',reason:'acceptance_criteria_required'}]);
    expect(h.sql.exec("SELECT kind FROM intake_audit WHERE task_id=? AND kind='restart_queued'",t.id).toArray()).toHaveLength(1);
  }));
  it.each([
    ['DONE commit 123abcd\nlive check: hub status\nresult: healthy', 'done_evidence_requires_operator_review'],
    ['DONE awaiting evidence', 'reported_completion_requires_operator_review'],
    ['ORIGINAL FINAL STATE (restored record, closed done v8): AC met.', 'reported_completion_requires_operator_review'],
  ])('queues completed-open claims without relaunching: %s', async (text, reason) => fixture(async h => {
    const t=task();t.history=[{kind:'note',text}];let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:200,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(0);expect(t.status).toBe('open');
    expect(w.project([t])[0]).toMatchObject({forgotten:true,restart:{state:'queued',reason}});
  }));
  it('context changes invalidate old completion claims and permit canonical admission', async () => fixture(async h => {
    const t=task();t.history=[{kind:'note',text:'DONE commit 123abcd\nlive check: hub status\nresult: healthy'},
      {kind:'update',changes:{acceptance_criteria:'New authorized criterion'}}];session(h,'old-failure',t,'failed');let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{id:'new',state:'queued'}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(1);expect(w.project([t])[0].restart.state).toBe('submitted');
  }));
  it('unresolved scope flag never holds a watchdog restart', async () => fixture(async h => {
    const t={...task(),scope:{flag:'needs_decision',policy_version:1,matched_terms:['dapier']}};session(h,'old-failure',t,'failed');let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{id:'new',state:'queued'}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(1);expect(JSON.stringify(w.project([t])[0])).not.toMatch(/scope_/);
  }));
  it('queues rather than duplicating an unlinked native worker when claim bridging is absent', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let starts=0;
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('fixture','unlinked','worker','running',?,'fixture')",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:200,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(0);expect(w.project([t])[0].restart.reason).toBe('unattributed_live_workspace_session');
  }));
  it('retries a lost admission reply with the same canonical key before considering fallback', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let now=NOW;const inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async input=>{
      inputs.push(input);if(inputs.length===1)throw new Error('lost response');
      return {status:202,body:{id:'new-session',state:'queued'}};
    }},()=>now);w.init();await w.tick();now+=QUEUE_RETRY_BASE_MS;await w.tick();
    expect(inputs).toHaveLength(2);expect(inputs[1]).toEqual(inputs[0]);
    // The failed codex attempt is recorded, so the recovery order continues
    // after it; a headless engine cannot require an interactive goal flag.
    expect(inputs[0]).toMatchObject({engine:'opencode',mode:'headless',intake_task_id:t.id,workspace:'fixture'});
    expect(w.project([t])[0].restart).toMatchObject({state:'submitted',session_id:'new-session'});
  }));
  it('canonical held session suppresses later duplicate restarts and preserves the queue reason', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{
      starts++;session(h,'canonical-held',t,'held');return {status:409,body:{id:'canonical-held',state:'held'}};
    }},()=>now);w.init();await w.tick();now+=WATCHDOG_INTERVAL;await w.tick();
    expect(starts).toBe(1);expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'active',restart:{state:'submitted',reason:'canonical_held'}});
  }));
  it('requeues a spent submission whose offered session expired and re-admits (0841a966 AC2)', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{
      starts++;const id='offer-'+starts;session(h,id,t,'offered');return {status:202,body:{id,state:'offered'}};
    }},()=>now);w.init();await w.tick();
    expect(starts).toBe(1);
    expect(w.project([t])[0].restart).toMatchObject({state:'submitted',reason:'canonical_offered'});
    h.sql.exec("UPDATE session_control_sessions SET state='cancelled' WHERE id='offer-1'");
    now+=WATCHDOG_INTERVAL;await w.tick();
    expect(starts).toBe(2);
    expect(h.sql.exec("SELECT reason FROM intake_queue_movement WHERE task_id=? AND reason='previous_session_cancelled'",t.id).toArray()).toHaveLength(1);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM intake_restart_queue').one().n).toBe(1);
  }));
  it('closes restart rows whose intake no longer accepts restarts (0841a966 AC1)', async () => fixture(async h => {
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[task('alive')]} as any,(f:any)=>f(),{},()=>NOW);w.init();
    h.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at)
      VALUES('gone-task','queued','workspace_configuration_required',?,?,?)`,NOW,NOW,NOW);
    await w.tick();
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM intake_restart_queue WHERE task_id=?','gone-task').one().n).toBe(0);
    expect(h.sql.exec("SELECT reason FROM intake_audit WHERE task_id=? AND kind='restart_closed'",'gone-task').toArray()[0].reason).toContain('intake_missing');
  }));
  it('escalates a hold that outlives the bounded TTL exactly once (0841a966 AC2)', async () => fixture(async h => {
    const t=task('ttl-hold');let now=NOW;const notes:string[]=[],alerts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      note:async(_t,text)=>{notes.push(text);return true},notify:async(...args)=>{alerts.push(args);return true}},()=>now);w.init();
    await w.tick();
    expect(w.queue([t])[0]).toMatchObject({state:'queued',reason:'workspace_configuration_required'});
    expect(alerts.some(a=>a[0]==='intake_human_hold')).toBe(true);
    now+=QUEUE_HOLD_TTL_MS+WATCHDOG_INTERVAL;await w.tick();
    expect(w.queue([t])[0]).toMatchObject({state:'escalated',reason:'hold_ttl_workspace_configuration_required'});
    expect(notes.some(n=>n.includes('hold_ttl_workspace_configuration_required'))).toBe(true);
    const alertKinds=alerts.filter(a=>a[0]==='intake_queue').map(a=>a[1]);
    expect(alertKinds).toContain(t.id);
    now+=QUEUE_RETRY_BASE_MS;await w.tick();
    expect(alerts.filter(a=>a[0]==='intake_queue'&&a[1]===t.id)).toHaveLength(1);
  }));
  it.each(['watchdog_project_not_enabled','reported_completion_requires_operator_review'])('keeps %s out of the hold TTL', async reason => fixture(async h => {
    const t=task('exempt');let now=NOW;const notes:string[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      note:async(_t,text)=>{notes.push(text);return true}},()=>now);w.init();
    h.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at)
      VALUES(?,'queued',?,?,?,?)`,t.id,reason,NOW-QUEUE_HOLD_TTL_MS-60000,NOW,NOW);
    await w.tick();
    expect(h.sql.exec('SELECT state FROM intake_restart_queue WHERE task_id=?',t.id).one().state).toBe('queued');
    expect(notes).toHaveLength(0);
  }));
  it('auto-configures a bare project label under an advertised absolute root (0841a966 AC2)', async () => fixture(async h => {
    const t={...task(),project:'subrepo'};let input:any=null;
    h.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,capabilities) VALUES('root-host','root-host','root-host','2026-10-26T12:00:00Z',?)",
      JSON.stringify({workspaces:['/home/alexey/git']}));
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async i=>{
      input=i;return {status:202,body:{id:'rooted-start',state:'queued'}}}},()=>NOW);w.init();await w.tick();
    expect(input).toMatchObject({intake_task_id:t.id,workspace:'subrepo',with_goal:true});
    expect(h.sql.exec('SELECT reason FROM intake_restart_queue').one().reason).toBe('canonical_queued');
  }));
  it('sends one daily digest and alerts on actual >30 minute idle; resets when count reaches zero', async () => fixture(async h => {
    let now=NOW,tasks=[task()];const notices:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{notify:async(...args)=>{notices.push(args)}},()=>now);w.init();
    await w.tick();expect(notices.filter(n=>n[0]==='intake_digest')).toHaveLength(1);
    // A 3h-old open intake with no session is a real >30 minute violation.
    expect(notices.some(n=>n[0]==='intake_watchdog'&&n[2]?.includes('>30 min'))).toBe(true);
    now+=WATCHDOG_INTERVAL;await w.tick();expect(notices.filter(n=>n[0]==='intake_digest')).toHaveLength(1);
    now+=86400001;await w.tick();expect(notices.some(n=>n[0]==='intake_watchdog' && n[2]?.includes('>24h'))).toBe(true);
    tasks=[];now+=WATCHDOG_INTERVAL;await w.tick();expect(h.sql.exec('SELECT nonzero_since FROM intake_watchdog_meta').one().nonzero_since).toBe(null);
  }));
  it('digest preserves IDs, all age buckets, and forgotten markers', async () => fixture(h => {
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    const items=w.project([task('day0',3600000),task('day2',2*86400000),task('day4',4*86400000),task('day8',8*86400000)]);
    const text=w.digest(items);for(const value of ['<1d (1)','1–3d (1)','3–7d (1)','>7d (1)','[FORGOTTEN] day8'])expect(text).toContain(value);
  }));
  it('resolves delivered informational digests while retaining failed target retries', async () => fixture(async h => {
    h.env={...h.env,HUB_ALERT_TO:'fixture-alerts'};
    h.sql.exec("INSERT OR IGNORE INTO agents(tag,project) VALUES('fixture-alerts',NULL)");
    const delivered=h.livenessService.setAlert('intake_digest','fixture-delivered',{state:'attention',detail:'Synthetic daily digest'});
    await h.notifyAlert(delivered);
    const done=h.sql.exec('SELECT * FROM liveness_alerts WHERE id=?',delivered.id).one();
    expect(done.resolved_at).not.toBeNull();expect(JSON.parse(done.delivered_to)).toContain('inbox:fixture-alerts');
    h.env={...h.env,HUB_ALERT_TO:'unregistered-fixture-alerts'};
    const failed=h.livenessService.setAlert('intake_digest','fixture-failed',{state:'attention',detail:'Synthetic failed digest'});
    await h.notifyAlert(failed);
    const pending=h.sql.exec('SELECT * FROM liveness_alerts WHERE id=?',failed.id).one();
    expect(pending.resolved_at).toBeNull();expect(pending.notify_error).toBeTruthy();
  }));
  it('stops stalled but alive native work through canonical control before replacement', async () => fixture(async h => {
    const t=task();const binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-3600000};
    session(h,'stalled',t,'running',{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture','native','worker','running',?,'fixture',?,?)",
      new Date(NOW).toISOString(),binding.native_created_at_ms,JSON.stringify({last_progress_at:new Date(NOW-3600000).toISOString()}));
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    let now=NOW;const stops:any[]=[],starts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      claudeRecoveryEnabled:true,
      stop:async(...args)=>{stops.push(args)},start:async input=>{starts.push(input);return {status:202,body:{id:'replacement',state:'queued'}}}
    },()=>now);w.init();await w.tick();expect(stops).toEqual([['stalled','iw-stall-stalled']]);expect(starts).toHaveLength(0);
    expect(w.project([t])[0]).toMatchObject({state:'recovering',reason:'stalled_native_exit_pending',forgotten:false});
    h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id='stalled'");
    h.sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id='native'");
    now+=WATCHDOG_INTERVAL;await w.tick();expect(starts).toHaveLength(1);expect(starts[0].engine).toBe('claude');
  }));
  it('quarantines three proven pre-launch failures on their host until an explicit successful probe', async () => fixture(async h => {
    for(let i=0;i<3;i++)h.sql.exec(`INSERT INTO fleet_launches(id,actor,request_key,requester_kind,summary,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,verification_status,exit_reason)
      VALUES(?,'fixture',?,'human','test','{}','fixture','antigravity','fixture','headless','fixture','test','failed',?,?,'not_started','launcher_command_failed')`,
      'quarantine-'+i,'quarantine-'+i,new Date(NOW-60000+i).toISOString(),new Date(NOW-60000+i).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[]} as any,(f:any)=>f(),{},()=>NOW);w.init();await w.tick();
    expect(h.sql.exec("SELECT status,reason FROM fleet_engine_health WHERE machine_id='fixture' AND engine='antigravity'").one()).toMatchObject({status:'unavailable',reason:'launch_failures_quarantined'});
    h.sql.exec("UPDATE fleet_engine_health SET status='ok',updated_at=? WHERE machine_id='fixture' AND engine='antigravity'",new Date(NOW).toISOString());
    const next=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[]} as any,(f:any)=>f(),{},()=>NOW+WATCHDOG_INTERVAL);next.init();await next.tick();
    expect(h.sql.exec("SELECT status FROM fleet_engine_health WHERE machine_id='fixture' AND engine='antigravity'").one().status).toBe('ok');
  }));
  it.each(['model_unsupported', 'goal_unsupported', 'account_unavailable','tier_unavailable','launcher_memory_reserve']
    .flatMap(reason=>[[reason,true],[reason,false]] as [string,boolean][]))('cancels a %s held intent with recorded engine=%s and selects another engine', async (reason,recordedEngine) => fixture(async h => {
    const t=task(),sessionId=crypto.randomUUID();session(h,sessionId,t,'held');
    h.sql.exec("UPDATE session_control_sessions SET hold_json=?,engine=?,mode='interactive',request_json=? WHERE id=?",JSON.stringify({reason}),recordedEngine?'codex':null,
      JSON.stringify({intake_task_id:t.id,workspace:'fixture',engine:'codex',mode:'interactive',with_goal:true}),sessionId);
    const starts:any[]=[],stopResults:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async(id,key)=>{
        const r=await h.sessionControlService.handle(new Request('https://hub.internal/api/sessions/'+id+'/stop',{method:'POST',body:JSON.stringify({key})}),
          {caller:{isAdmin:true,actor:'hub:service:intake-watchdog',kind:'human'},scopes:['admin']});
        stopResults.push({status:r.status,body:await r.json()});
      },start:async input=>{starts.push(input);return {status:202,body:{id:'fallback',state:'queued'}}}
    },()=>NOW);w.init();await w.tick();
    expect(stopResults).toMatchObject([{status:200}]);
    expect(h.sql.exec("SELECT state FROM session_control_sessions WHERE id=?",sessionId).one().state).toBe('cancelled');
    expect(starts).toHaveLength(1);expect(starts[0].engine).toBe('opencode');
  }));
  it('advances engine and request key after a definitive admission refusal without a session', async () => fixture(async h => {
    const t=task();session(h,'prior-codex',t,'failed');let now=NOW;const inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async input=>{
      inputs.push(input);return {status:400,body:{error:'model_unsupported'}};
    }},()=>now);w.init();await w.tick();now+=QUEUE_RETRY_BASE_MS;await w.tick();
    expect(inputs.map(i=>i.engine)).toEqual(['opencode','zcodex']);expect(inputs[0].key).not.toBe(inputs[1].key);
  }));
  it('keeps a retry unpinned when the first policy-routed admission is refused', async () => fixture(async h => {
    const t=task();let now=NOW;const inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async input=>{
      inputs.push(input);return {status:400,body:{error:'admission_refused'}};
    }},()=>now);w.init();await w.tick();now+=QUEUE_RETRY_BASE_MS;await w.tick();
    expect(inputs).toHaveLength(2);
    expect(inputs[0]).not.toHaveProperty('engine');
    expect(inputs[1]).not.toHaveProperty('engine');
    expect(inputs[0].key).not.toBe(inputs[1].key);
  }));
  it.each(['future-wake','different-incarnation'])('preserves %s instead of stopping it', async scenario => fixture(async h => {
    const t=task();const binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-3600000};
    session(h,'protected-native',t,'running',{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture','native','worker','running',?,'fixture',?,?)",
      new Date(NOW).toISOString(),binding.native_created_at_ms+(scenario==='different-incarnation'?1:0),JSON.stringify({last_progress_at:new Date(NOW-3600000).toISOString()}));
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    if(scenario==='future-wake')h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES('protected-wake','protected-native','fixture','protected',?,?,'continue','pending',?,?)`,NOW+60000,new Date(NOW+60000).toISOString(),new Date(NOW).toISOString(),new Date(NOW).toISOString());
    let stops=0,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async()=>{stops++},start:async()=>{starts++;return {status:202,body:{}}}
    },()=>NOW);w.init();await w.tick();expect(stops).toBe(0);expect(starts).toBe(0);expect(w.project([t])[0].state).toBe('active');
  }));
  it('alerts when a stalled native remains unconfirmed beyond the recovery deadline', async () => fixture(async h => {
    const t=task(),binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-7200000};
    session(h,'stalled-deadline',t,'running',{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture','native','worker','running',?,'fixture',?,?)",
      new Date(NOW).toISOString(),binding.native_created_at_ms,JSON.stringify({last_progress_at:new Date(NOW-7200000).toISOString()}));
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const notices:any[]=[];let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async()=>{},start:async()=>{starts++;return {status:202,body:{}}},notify:async(...args)=>{notices.push(args)}
    },()=>NOW);w.init();await w.tick();expect(starts).toBe(0);
    expect(notices.some(n=>n[0]==='intake_watchdog' && n[2]?.includes('>30 min') && n[2]?.includes(t.id) && n[2]?.includes('stalled_native_exit_pending'))).toBe(true);
  }));
  it('waits for native exit after a projected Codex reconnect failure', async () => fixture(async h => {
    const t=task(),binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-60000};
    session(h,'reconnect-old',t,'running',{machine:'fixture',binding});
    h.sql.exec("UPDATE session_control_sessions SET mode='interactive' WHERE id='reconnect-old'");
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture','native','worker','running','codex',?,'fixture',?,?)",
      new Date(NOW).toISOString(),binding.native_created_at_ms,JSON.stringify({last_progress_at:new Date(NOW).toISOString(),headless:false,terminal_error:'codex_reconnect_failed'}));
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    let stops=0,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async()=>{stops++},start:async()=>{starts++;return {status:202,body:{id:'replacement'}}}
    },()=>NOW);w.init();await w.tick();expect(stops).toBe(1);expect(starts).toBe(0);
    expect(w.project([t])[0]).toMatchObject({state:'recovering',forgotten:false});
  }));
  it('restarts from a fresh exact terminal census even before the canonical row catches up', async () => fixture(async h => {
    const t=task(),binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-60000};
    session(h,'lagging-old',t,'running',{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms) VALUES('fixture','native','worker','exited','codex',?,'fixture',?)",new Date(NOW).toISOString(),binding.native_created_at_ms);
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const starts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      start:async input=>{starts.push(input);return {status:202,body:{id:'replacement'}}}
    },()=>NOW);w.init();await w.tick();expect(starts).toHaveLength(1);expect(starts[0].engine).toBe('opencode');
  }));
  it('does not use a terminal row from a different native incarnation as exit proof', async () => fixture(h => {
    const t=task(),binding={native_id:'native',tag:'worker',workspace:'fixture',native_created_at_ms:NOW-60000};
    session(h,'old-incarnation',t,'running',{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms) VALUES('fixture','native','worker','exited','codex',?,'fixture',?)",new Date(NOW-3600000).toISOString(),binding.native_created_at_ms+1);
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    expect(w.project([t])[0]).toMatchObject({state:'unknown',forgotten:false});
  }));
  it('keeps a future registered wake as pending intent after native stop and restarts only when due', async () => fixture(async h => {
    const t=task();session(h,'future-stopped',t,'stopped');let now=NOW,starts=0;
    h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES('future-stopped-wake','future-stopped','fixture','future-stopped',?,?,'continue','pending',?,?)`,NOW+3600000,new Date(NOW+3600000).toISOString(),new Date(NOW).toISOString(),new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      start:async()=>{starts++;return {status:202,body:{id:'replacement'}}}
    },()=>now);w.init();await w.tick();expect(starts).toBe(0);
    expect(w.project([t])[0]).toMatchObject({state:'active',idle_ms:null,forgotten:false});
    // A due wake settles to missed through the canonical wake reconciler; the
    // recovery path then requeues the spent episode within its normal cadence.
    h.sql.exec("UPDATE session_control_wakes SET state='missed' WHERE id='future-stopped-wake'");
    now=NOW+3600001;await w.tick();expect(starts).toBe(1);
  }));
  it.each(['running','held'])('rechecks a future wake added during automatic %s stop admission', async state => fixture(async h => {
    const t=task(),id=crypto.randomUUID(),binding={native_id:crypto.randomUUID(),tag:'worker',workspace:'fixture',native_created_at_ms:NOW-7200000};
    session(h,id,t,state,state==='running'?{machine:'fixture',binding}:{});
    if(state==='held')h.sql.exec('UPDATE session_control_sessions SET hold_json=? WHERE id=?',JSON.stringify({reason:'model_unsupported'}),id);
    else{
      h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture',?,'worker','running','codex',?,'fixture',?,?)",binding.native_id,new Date(NOW).toISOString(),binding.native_created_at_ms,JSON.stringify({last_progress_at:new Date(NOW-7200000).toISOString()}));
      h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    }
    const service=new SessionControlService(h.sql,(f:any)=>f(),()=>NOW);service.init();
    const results:any[]=[];let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async(sessionId,key)=>{
        h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
          VALUES(?,?,'fixture',?, ?,?,'continue','pending',?,?)`,crypto.randomUUID(),id,id,NOW+60000,new Date(NOW+60000).toISOString(),new Date(NOW).toISOString(),new Date(NOW).toISOString());
        const r=await service.handle(new Request('https://hub.internal/api/sessions/'+sessionId+'/stop',{method:'POST',body:JSON.stringify({key})}),
          {caller:{isAdmin:true,actor:'hub:service:intake-watchdog',kind:'human'},scopes:['admin']});
        results.push({status:r.status,body:await r.json()});
      },start:async()=>{starts++;return {status:202,body:{}}}
    },()=>NOW);w.init();await w.tick();
    expect(results).toMatchObject([{status:409,body:{error:'session_waiting_until'}}]);expect(starts).toBe(0);
    expect(h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?',id).one().state).toBe(state);
    expect(h.sql.exec('SELECT id FROM session_control_commands WHERE session_id=?',id).toArray()).toHaveLength(0);
  }));
  it('does not let repeated recoverable admission holds reset the thirty-minute idle deadline', async () => fixture(async h => {
    const t=task('hold-loop',60000);let now=NOW;const notices:any[]=[];
    const first=crypto.randomUUID();session(h,first,t,'held');
    h.sql.exec('UPDATE session_control_sessions SET hold_json=? WHERE id=?',JSON.stringify({reason:'model_unsupported'}),first);
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      stop:async id=>{h.sql.exec("UPDATE session_control_sessions SET state='cancelled',updated_at=? WHERE id=?",new Date(now).toISOString(),id)},
      start:async input=>{
        const id=crypto.randomUUID();session(h,id,t,'held');
        h.sql.exec('UPDATE session_control_sessions SET engine=?,hold_json=?,created_at=?,updated_at=? WHERE id=?',input.engine,JSON.stringify({reason:'model_unsupported'}),new Date(now).toISOString(),new Date(now).toISOString(),id);
        return {status:409,body:{id,state:'held',reason:'model_unsupported'}};
      },notify:async(...args)=>{notices.push(args)}
    },()=>now);w.init();await w.tick();
    expect(notices.filter(n=>n[0]==='intake_watchdog'&&n[2]!==null)).toHaveLength(0);
    for(let i=0;i<34;i++){now+=WATCHDOG_INTERVAL;await w.tick();}
    expect(notices.some(n=>n[0]==='intake_watchdog'&&n[2]?.includes('>30 min')&&n[2]?.includes('model_unsupported'))).toBe(true);
    // The bounded retry cap names the stuck cycle instead of looping forever;
    // the idle deadline itself is never reset by the repeating holds.
    expect(w.project([t])[0]).toMatchObject({state:'escalated',reason:'only_failed_sessions'});
    expect(w.project([t])[0].idle_ms).toBeGreaterThan(1800000);
  }));
  it('replays an already-recorded automatic stop after a future wake is added', async () => fixture(async h => {
    const t=task(),id=crypto.randomUUID(),binding={native_id:crypto.randomUUID(),tag:'worker',workspace:'fixture',native_created_at_ms:NOW-60000};
    session(h,id,t,'running',{machine:'fixture',binding});
    const service=new SessionControlService(h.sql,(f:any)=>f(),()=>NOW);service.init();
    const send=()=>service.handle(new Request('https://hub.internal/api/sessions/'+id+'/stop',{method:'POST',body:JSON.stringify({key:'replay-stop'})}),
      {caller:{isAdmin:true,actor:'hub:service:intake-watchdog',kind:'human'},scopes:['admin']});
    const first=await send();expect(first.status).toBe(201);const original:any=await first.json();
    h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES(?,?,'fixture',?, ?,?,'continue','pending',?,?)`,crypto.randomUUID(),id,id,NOW+60000,new Date(NOW+60000).toISOString(),new Date(NOW).toISOString(),new Date(NOW).toISOString());
    const replay=await send();expect(replay.status).toBe(200);
    expect(await replay.json()).toMatchObject({command_id:original.command_id,replay:true});
    expect(h.sql.exec('SELECT id FROM session_control_commands WHERE session_id=?',id).toArray()).toHaveLength(1);
  }));
  it.each(['offered','starting','uncertain'])('requests canonical stop for a bound stalled %s session without starting a duplicate', async state => fixture(async h => {
    const t=task(),id=crypto.randomUUID(),binding={native_id:crypto.randomUUID(),tag:'worker',workspace:'fixture',native_created_at_ms:NOW-7200000,generation:1};
    session(h,id,t,state,{machine:'fixture',binding});
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms,progress_json) VALUES('fixture',?,'worker','running','codex',?,'fixture',?,?)",
      binding.native_id,new Date(NOW).toISOString(),binding.native_created_at_ms,JSON.stringify({last_progress_at:new Date(NOW-7200000).toISOString()}));
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",new Date(NOW).toISOString());
    const service=new SessionControlService(h.sql,(fn:any)=>fn(),()=>NOW);service.init();let starts=0;const stops:number[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(fn:any)=>fn(),{
      stop:async(sessionId,key)=>{const r=await service.handle(new Request('https://hub.internal/api/sessions/'+sessionId+'/stop',{method:'POST',body:JSON.stringify({key})}),
        {caller:{isAdmin:true,actor:'hub:service:intake-watchdog',kind:'human'},scopes:['admin']});stops.push(r.status);},
      start:async()=>{starts++;return {status:202,body:{}};}
    },()=>NOW);w.init();expect(w.project([t])[0].state).toBe('recovering');await w.tick();
    expect(stops).toEqual([201]);expect(starts).toBe(0);
    expect(h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?',id).one().state).toBe(state);
    expect(h.sql.exec("SELECT kind,state FROM session_control_commands WHERE session_id=?",id).one()).toMatchObject({kind:'stop',state:'recorded'});
  }));
  it.each(['codex','opencode','zcodex','antigravity'])('never pins to a failed engine when all engines have historical failures and records every rejected engine (after %s)', async previous => fixture(async h => {
    const t=task(),order=['codex','opencode','zcodex','antigravity'];
    const history=[...order.filter(engine=>engine!==previous),previous];
    history.forEach((engine,i)=>{
      const id='cycle-'+i;session(h,id,t,'failed');
      const at=new Date(NOW-10000+i*1000).toISOString();
      h.sql.exec('UPDATE session_control_sessions SET engine=?,created_at=?,updated_at=? WHERE id=?',engine,at,at,id);
    });
    const inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(fn:any)=>fn(),{
      start:async input=>{inputs.push(input);return {status:202,body:{id:'next-cycle',state:'queued'}};}
    },()=>NOW);w.init();
    // Engine choice must not select any recorded failed attempt; when none is eligible, record a held reason naming every rejected engine.
    h.sql.exec(`INSERT INTO intake_restart_queue(task_id,state,reason,request_key,request_json,session_id,attempts,next_at,updated_at)
      VALUES(?,'submitted','canonical_failed','old-key',?,'cycle-3',0,?,?)`,t.id,JSON.stringify({engine:previous,key:'old-key'}),NOW-1000,NOW-1000);
    await w.tick();expect(inputs).toHaveLength(0);
    const q:any=h.sql.exec('SELECT state,reason FROM intake_restart_queue WHERE task_id=?',t.id).one();
    expect(q.state).toBe('queued');
    for (const eng of order) expect(q.reason).toContain(eng);
  }));
  it('REST refuses unauthenticated readers', async () => {
    const res=await SELF.fetch('https://hub.test/api/intake/forgotten');expect([401,403]).toContain(res.status);
  });
});
