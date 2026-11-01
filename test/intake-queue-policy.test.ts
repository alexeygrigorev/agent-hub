import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { liveQueuePolicy } from '../src/intake-queue-policy';
import { providerPreference } from '../src/pacing';
import { IntakeWatchdog, queuePolicyFrom } from '../src/intake-watchdog';

const NOW = Date.parse('2026-11-01T10:00:00Z');
const settings = { HUB_INTAKE_QUEUE_ENABLED: 'true', HUB_INTAKE_QUEUE_PROJECTS: '*',
  HUB_INTAKE_QUEUE_MAX_INFLIGHT: 'auto', HUB_INTAKE_QUEUE_MAX_PER_PROJECT: 'auto' };
async function fixture(fn: (h: any, fleet: any, account: any) => void | Promise<void>) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('queue-capacity-policy'));
  await (runInDurableObject as any)(stub, async (h: any) => {
    for (const table of ['intake_queue_admissions', 'intake_restart_queue', 'intake_watchdog_meta', 'intake_queue_movement', 'machines', 'routing_machines', 'fleet_accounts', 'session_control_sessions', 'fleet_sessions', 'fleet_launches']) h.sql.exec('DELETE FROM ' + table);
    h.sql.exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('zai-main','zai',26,15,1)");
    h.sql.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained)
      VALUES('online','online','online',?,?,?,0)`, new Date(NOW).toISOString(), new Date(NOW).toISOString(), JSON.stringify({disk_free_gb: 100}));
    const account = { provider: 'zai', enabled: true, supports_goal: true, status: 'ok', placeable_machines: ['online'], placeable_slots: 26 };
    const fleet = { withMaintenance: (fn: any) => fn(), accountStatus: () => account, resources: {
      config: () => ({ disk_floor_gb: 15 }), cpuHeadroom: () => ({ admitted: true }),
      headroom: () => ({ mem_available_mb: 20480, reserved_mb: 1024, floor_mb: 10240, requested_mb: 512 }) } };
    await fn(h, fleet, account);
  });
}
describe('live autonomous queue policy', () => {
  it('prefers admitted Z.ai and Gemini routes over Claude at equal scoring inputs', () => {
    expect(providerPreference('zai')).toBeGreaterThan(providerPreference('claude'));
    expect(providerPreference('gemini')).toBeGreaterThan(providerPreference('claude'));
  });
  it('enables real projects with a capacity budget above the proof-only 2/1 limits', () => fixture((h, fleet) => {
    expect(liveQueuePolicy(settings, h.sql, fleet, NOW)).toMatchObject({ enabled: true, valid: true,
      projects: null, max_inflight: 18, max_per_project: 18,
      capacity: { active: 0, machine_slots: 18, account_slots: 26, available_slots: 18 } });
  }));
  it('dispatches real-project intakes past 2/1 while stopping at live account capacity', () => fixture(async (h, fleet, account) => {
    const tasks = Array.from({length:4}, (_, i) => ({id:'real-' + i, project:'real-project', title:'Real work',
      status:'open', version:1, created_at:NOW, updated_at:NOW, acceptance_criteria:'Verify actual outcome', history:[]}));
    h.sql.exec('UPDATE machines SET capabilities=?', JSON.stringify({workspaces:['real-project']}));
    let starts = 0, policyReads = 0;
    account.placeable_slots = 3;
    const w = new IntakeWatchdog(h.sql, {statusAuthorizedTasks:()=>tasks} as any, (fn:any)=>fn(), {
      policy: () => { policyReads++; return liveQueuePolicy(settings, h.sql, fleet, NOW); },
      start: async input => {
        const id = 'automatic-' + starts++;
        account.placeable_slots--;
        h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
          VALUES(?,'hub:service:intake-watchdog',?,?,'interactive','offered',?,?)`, id, input.key, JSON.stringify(input),
          new Date(NOW).toISOString(), new Date(NOW).toISOString());
        return {status:202,body:{id,state:'offered'}};
      }
    }, () => NOW);
    w.init(); await w.tick();
    expect(starts).toBe(3);
    expect(w.status().automation).toMatchObject({projects:null,max_inflight:3,max_per_project:3,capacity:{available_slots:0}});
    await Promise.resolve();
    policyReads = 0;
    expect(w.queue(tasks).find(t=>t.id==='real-3')).toMatchObject({reason:'watchdog_global_capacity'});
    expect(policyReads).toBe(1);
  }));
  it.each(['OBSOLETE','DUPLICATE','DONE'])('holds current TRIAGE %s claims but invalidates old verdicts after AC updates', verdict => fixture(async (h, fleet) => {
    const negative = {id:'negative',project:'real-project',title:'Old work',status:'open',created_at:NOW,
      acceptance_criteria:'Verify outcome',history:[{kind:'note',text:'TRIAGE ' + verdict + ' — reviewed'}]};
    const changed = {...negative,id:'changed',history:[...negative.history,
      {kind:'update',changes:{acceptance_criteria:'New authorized requirement'}}]};
    const tasks = [negative,changed];
    h.sql.exec('UPDATE machines SET capabilities=?', JSON.stringify({workspaces:['real-project']}));
    const started:string[] = [];
    const w = new IntakeWatchdog(h.sql, {statusAuthorizedTasks:()=>tasks} as any, (fn:any)=>fn(), {
      policy: () => liveQueuePolicy(settings,h.sql,fleet,NOW),
      start: async input => { started.push(input.intake_task_id); return {status:409,body:{error:'test_refusal'}}; }
    }, () => NOW);
    w.init(); await w.tick();
    expect(started).toEqual(['changed']);
    expect(w.queue(tasks).find(t=>t.id==='negative')).toMatchObject({reason:'watchdog_triage_' + verdict.toLowerCase()});
  }));
  it('skips disabled and explicit-only accounts before expensive status maintenance', () => fixture((h, fleet) => {
    h.sql.exec("UPDATE fleet_accounts SET provider='codex'");
    let reads = 0;
    fleet.accountStatus = () => { reads++; throw new Error('must not probe explicit-only account'); };
    expect(liveQueuePolicy(settings,h.sql,fleet,NOW).max_inflight).toBe(0);
    expect(reads).toBe(0);
  }));
  it('honors the existing machine ceiling without raising it', () => fixture((h, fleet) => {
    h.sql.exec("INSERT OR REPLACE INTO routing_machines(machine_id,needs_json,max_agents,observed_at) VALUES('online','[]',1,?)", new Date(NOW).toISOString());
    expect(liveQueuePolicy(settings, h.sql, fleet, NOW)).toMatchObject({max_inflight:1,max_per_project:1});
    expect(h.sql.exec("SELECT max_agents FROM routing_machines WHERE machine_id='online'").one().max_agents).toBe(1);
  }));
  it('counts shared accounts once across hosts and honors their remaining slots', () => fixture((h, fleet, account) => {
    h.sql.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained)
      SELECT 'second','second','second',registered_at,last_checkin_at,last_heartbeat,drained FROM machines WHERE id='online'`);
    account.placeable_machines = ['online', 'second']; account.placeable_slots = 3;
    expect(liveQueuePolicy(settings, h.sql, fleet, NOW)).toMatchObject({ max_inflight: 3, max_per_project: 3 });
  }));
  it.each(['offline', 'drained', 'revoked', 'disk', 'memory', 'cpu', 'quota', 'unknown_quota', 'unreachable', 'explicit_only', 'disabled_provider'])('creates no slots from %s capacity', reason => fixture((h, fleet, account) => {
    if (reason === 'offline') h.sql.exec('UPDATE machines SET last_checkin_at=?', new Date(NOW - 180001).toISOString());
    if (reason === 'drained') h.sql.exec('UPDATE machines SET drained=1');
    if (reason === 'revoked') h.sql.exec('UPDATE machines SET revoked_at=?', new Date(NOW).toISOString());
    if (reason === 'disk') h.sql.exec('UPDATE machines SET last_heartbeat=?', JSON.stringify({ disk_free_gb: 14 }));
    if (reason === 'memory') fleet.resources.headroom = () => ({ mem_available_mb: null });
    if (reason === 'cpu') fleet.resources.cpuHeadroom = () => ({ admitted: false });
    if (reason === 'quota') account.status = 'blocked';
    if (reason === 'unknown_quota') account.status = 'unknown';
    if (reason === 'explicit_only') account.provider = 'codex';
    if (reason === 'disabled_provider') account.provider = 'grok';
    if (reason === 'unreachable') account.placeable_machines = ['offline-tabo'];
    expect(liveQueuePolicy(settings, h.sql, fleet, NOW)).toMatchObject({ valid: true, max_inflight: 0, max_per_project: 0 });
  }));
  it('does not lose occupied automatic slots when free capacity shrinks', () => fixture((h, fleet, account) => {
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
      VALUES('owned','hub:service:intake-watchdog','owned','{"with_goal":true}','zai','headless','running',?,?)`, new Date(NOW).toISOString(), new Date(NOW).toISOString());
    account.placeable_slots = 0;
    expect(liveQueuePolicy(settings, h.sql, fleet, NOW)).toMatchObject({ max_inflight: 1, capacity: { active: 1, available_slots: 0 } });
  }));
  it('keeps the bounded observation ceiling and fails closed without capacity evidence', () => {
    expect(queuePolicyFrom(settings, 1000)).toMatchObject({ valid: true, max_inflight: 32, max_per_project: 32 });
    expect(queuePolicyFrom(settings)).toMatchObject({ valid: true, max_inflight: 0, max_per_project: 0 });
    expect(queuePolicyFrom({ ...settings, HUB_INTAKE_QUEUE_MAX_INFLIGHT: 'bogus' })).toMatchObject({ valid: false, enabled: false });
  });
  it('preserves explicit rollback caps without querying capacity', () => {
    expect(liveQueuePolicy({ ...settings, HUB_INTAKE_QUEUE_MAX_INFLIGHT: '2', HUB_INTAKE_QUEUE_MAX_PER_PROJECT: '1' }, {} as any, {} as any, NOW))
      .toMatchObject({ valid: true, max_inflight: 2, max_per_project: 1 });
  });
});
