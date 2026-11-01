import { describe, it, expect } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import { IntakeWatchdog as BaseIntakeWatchdog, queuePolicyFrom, WATCHDOG_INTERVAL, QUEUE_RETRY_BASE_MS, QUEUE_HOLD_TTL_MS } from '../src/intake-watchdog';
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
    VALUES(?,?,?,?,?,'headless',?,?,?,?,?)`, id, 'fixture', id, JSON.stringify({ intake_task_id: t.id, workspace: 'fixture' }), 'codex', state,
    new Date(NOW - 3600000).toISOString(), new Date(NOW - 1800000).toISOString(), extra.machine || null, extra.binding ? JSON.stringify(extra.binding) : null);
}
describe('forgotten intake watchdog', () => {
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
  it('dispatches a queued restart before fresh pickup without raising admission caps', async () => fixture(async h => {
    const fresh=Array.from({length:9},(_,i)=>task('fresh-'+i)), overdue=task('queued-restart');
    const all=[...fresh,overdue],started:string[]=[];
    for(const t of all)session(h,'failed-'+t.id,t,'failed');
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>all} as any,(f:any)=>f(),{
      start:async input=>{started.push(input.intake_task_id);return {status:202,body:{id:'new-'+input.intake_task_id,state:'queued'}}}
    },()=>NOW);w.init();
    h.sql.exec("UPDATE intake_queue_cursor SET after_id='' WHERE id=1");
    h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,held_since,next_at,updated_at) VALUES(?,'queued','watchdog_global_capacity',?,?,?)",overdue.id,NOW-60000,NOW-60000,NOW);
    await w.tick();
    expect(started[0]).toBe(overdue.id);
    expect(started).toEqual([overdue.id]); // The default per-project cap remains one.
    expect(h.sql.exec("SELECT reason FROM intake_restart_queue WHERE task_id=?",fresh[0].id).one().reason).toBe('watchdog_project_capacity');
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

  it('uses >2h no-session age, ignores note updates, omits terminal archives', async () => fixture(h => {
    const w = new IntakeWatchdog(h.sql, h.intakeService, (f: any) => f(), {}, () => NOW); w.init();
    const rows = w.project([task(), task('boundary',7200000), {...task('closed'),status:'done',archived_at:NOW}]);
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
  it('retries a lost admission reply with the same canonical key and never starts without goal', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let now=NOW;const inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async input=>{
      inputs.push(input);if(inputs.length===1)throw new Error('lost response');
      return {status:202,body:{id:'new-session',state:'queued'}};
    }},()=>now);w.init();await w.tick();now+=QUEUE_RETRY_BASE_MS;await w.tick();
    expect(inputs).toHaveLength(2);expect(inputs[1]).toEqual(inputs[0]);expect(inputs[0]).not.toHaveProperty('engine');
    expect(inputs[0]).toMatchObject({mode:'interactive',with_goal:true,intake_task_id:t.id,workspace:'fixture'});
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
  it('sends one daily digest and alerts only after sustained >24h; resets when count reaches zero', async () => fixture(async h => {
    let now=NOW,tasks=[task()];const notices:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{notify:async(...args)=>{notices.push(args)}},()=>now);w.init();
    await w.tick();expect(notices.filter(n=>n[0]==='intake_digest')).toHaveLength(1);expect(notices[0][2]).toBe(null);
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
  it('REST refuses unauthenticated readers', async () => {
    const res=await SELF.fetch('https://hub.test/api/intake/forgotten');expect([401,403]).toContain(res.status);
  });
});
