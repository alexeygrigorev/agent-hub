import { describe, it, expect } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import { IntakeWatchdog, WATCHDOG_INTERVAL } from '../src/intake-watchdog';
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const NOW = Date.parse('2026-10-26T12:00:00Z');
function task(id = 'synthetic-task', age = 3 * 3600000): any {
  return { id, title: 'Synthetic task', project: 'demo', created_at: NOW - age, updated_at: NOW,
    status: 'open', acceptance_criteria: 'Synthetic criterion', history: [] };
}
async function fixture(fn: (h: any) => any) {
  return inDO(stub(), async (h: any) => {
    for (const table of ['intake_restart_queue', 'intake_watchdog_meta', 'session_control_sessions', 'fleet_sessions', 'claims', 'fleet_catalog_seen']) h.sql.exec('DELETE FROM ' + table);
    return fn(h);
  });
}
function session(h: any, id: string, t: any, state: string, extra: any = {}) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
    VALUES(?,?,?,?,?,'headless',?,?,?,?,?)`, id, 'fixture', id, JSON.stringify({ intake_task_id: t.id, workspace: 'fixture' }), 'codex', state,
    new Date(NOW - 3600000).toISOString(), new Date(NOW - 1800000).toISOString(), extra.machine || null, extra.binding ? JSON.stringify(extra.binding) : null);
}
describe('forgotten intake watchdog', () => {
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
  it('keeps unresolved scope in the visible queue without inventing operator approval', async () => fixture(async h => {
    const t={...task(),scope:{flag:'unknown',policy_version:1}};let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:200,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(0);expect(w.project([t])[0].restart.reason).toBe('scope_decision_required');
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
    }},()=>now);w.init();await w.tick();now+=WATCHDOG_INTERVAL;await w.tick();
    expect(inputs).toHaveLength(2);expect(inputs[1]).toEqual(inputs[0]);
    expect(inputs[0]).toMatchObject({engine:'codex',mode:'interactive',with_goal:true,intake_task_id:t.id,workspace:'fixture'});
    expect(w.project([t])[0].restart).toMatchObject({state:'submitted',session_id:'new-session'});
  }));
  it('canonical held session suppresses later duplicate restarts and preserves the queue reason', async () => fixture(async h => {
    const t=task();session(h,'old-failure',t,'failed');let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{
      starts++;session(h,'canonical-held',t,'held');return {status:409,body:{id:'canonical-held',state:'held'}};
    }},()=>now);w.init();await w.tick();now+=WATCHDOG_INTERVAL;await w.tick();
    expect(starts).toBe(1);expect(w.project([t])[0]).toMatchObject({forgotten:false,state:'active',restart:{state:'submitted',reason:'canonical_held'}});
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
