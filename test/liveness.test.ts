import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { LivenessService, thresholdsFrom, DEFAULT_THRESHOLDS } from '../src/liveness';
import { FleetService } from '../src/fleet';
import { updateHeartbeat } from '../src/registry';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const now = Date.parse('2026-10-15T12:00:00Z');
const iso = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();

async function fixture(fn: (live: LivenessService, sql: any, clock: { t: number }) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('liveness-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const clock = { t: now };
    new FleetService(hub.sql, fn => fn(), () => clock.t).init();
    const live = new LivenessService(hub.sql, DEFAULT_THRESHOLDS, () => clock.t); live.init();
    await fn(live, hub.sql, clock);
  });
}
function machine(sql: any, id: string, checkin: string | null, drained = 0) {
  sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?, ?, ?, ?, ?, ?)', id, id, id, iso(86400), checkin, drained);
}
function session(sql: any, machineId: string, tag: string, phase: string, state: string | null, activityAgo: number | null) {
  sql.exec('INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
    machineId, 'n-' + tag, tag, 'codex', phase, state, iso(30), '/repo', null, null, activityAgo === null ? null : iso(activityAgo));
}
function launch(sql: any, id: string, machineId: string, state: string, updatedAgo: number, mode = 'headless', nativeId: string | null = null) {
  sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES (?, 'admin', 'human', NULL, 'test task', ?, '{}', ?, 'codex', 'acct', NULL, ?, 'demo', 'goal', ?, ?, ?, ?)",
    id, id, machineId, mode, state, nativeId, iso(updatedAgo), iso(updatedAgo));
}
const byId = (subjects: any[], kind: string, id: string) => subjects.find(s => s.kind === kind && s.id === id)?.state;

describe('liveness states', () => {
  it('opens one work alert after ten minutes offered despite fresh host cycles, then resolves it', () => fixture((live, sql, clock) => {
    machine(sql, 'hetzner', iso(0));
    launch(sql, 'never-claimed', 'hetzner', 'offered', 600);
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    clock.t += 1000;
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', new Date(clock.t).toISOString(), 'hetzner');
    const opened = live.reconcileAlerts(live.evaluate().subjects);
    expect(opened).toHaveLength(1);
    expect(opened[0]).toMatchObject({ subject_kind: 'launch', subject: 'never-claimed', state: 'stuck' });
    expect(opened[0].detail).toContain('offered for 601s');
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    sql.exec("UPDATE fleet_launches SET state='failed', exit_reason='admission_unconfigured' WHERE id='never-claimed'");
    live.reconcileAlerts(live.evaluate().subjects);
    expect(live.alerts()[0].resolved_at).not.toBeNull();
  }));
  it('classifies machines by heartbeat age and drain state', () => fixture((live, sql) => {
    machine(sql, 'fresh', iso(60)); machine(sql, 'late', iso(300)); machine(sql, 'gone', iso(3600)); machine(sql, 'new', null); machine(sql, 'parked', iso(3600), 1);
    const { subjects } = live.evaluate();
    expect(byId(subjects, 'machine', 'fresh')).toBe('online');
    expect(byId(subjects, 'machine', 'late')).toBe('stale');
    expect(byId(subjects, 'machine', 'gone')).toBe('offline');
    expect(byId(subjects, 'machine', 'new')).toBe('never');
    expect(byId(subjects, 'machine', 'parked')).toBe('drained');
  }));

  it('detects stalled, working, idle and exited sessions, and inherits unknown from a silent host', () => fixture((live, sql) => {
    machine(sql, 'hetzner', iso(30)); machine(sql, 'laptop', iso(3600));
    session(sql, 'hetzner', 'busy', 'running', 'working', 60);
    session(sql, 'hetzner', 'hung', 'running', 'working', 4000);
    session(sql, 'hetzner', 'waiting', 'running', 'idle', 4000);
    session(sql, 'hetzner', 'done', 'exited', null, null);
    session(sql, 'laptop', 'remote', 'running', 'working', 10);
    sql.exec("INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, last_activity_at) VALUES ('hetzner', 'n-sh', 'quiet-shell', 'shell', 'running', 'working', ?, '/repo', ?)", iso(30), iso(9000));
    const { subjects } = live.evaluate();
    expect(byId(subjects, 'session', 'hetzner/quiet-shell')).toBe('stalled');
    expect(byId(subjects, 'session', 'hetzner/busy')).toBe('working');
    expect(byId(subjects, 'session', 'hetzner/hung')).toBe('stalled');
    expect(byId(subjects, 'session', 'hetzner/waiting')).toBe('stalled');
    expect(byId(subjects, 'session', 'hetzner/done')).toBe('exited');
    expect(byId(subjects, 'session', 'laptop/remote')).toBe('unknown');
  }));

  it('flags stuck offers, uncertain launches and interactive sessions that vanished', () => fixture((live, sql) => {
    machine(sql, 'hetzner', iso(30));
    launch(sql, 'a0000000-0000-4000-8000-000000000001', 'hetzner', 'offered', 1200);
    launch(sql, 'a0000000-0000-4000-8000-000000000002', 'hetzner', 'offered', 30);
    launch(sql, 'a0000000-0000-4000-8000-000000000003', 'hetzner', 'uncertain', 30);
    launch(sql, 'a0000000-0000-4000-8000-000000000004', 'hetzner', 'running', 30, 'interactive', 'n-gone');
    session(sql, 'hetzner', 'kept', 'running', 'working', 10);
    launch(sql, 'a0000000-0000-4000-8000-000000000005', 'hetzner', 'running', 30, 'interactive', 'n-kept');
    launch(sql, 'a0000000-0000-4000-8000-000000000006', 'hetzner', 'running', 30, 'headless', 'n-headless-gone');
    const { subjects } = live.evaluate();
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000001')).toBe('stuck');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000002')).toBe('offered');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000003')).toBe('uncertain');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000004')).toBe('missing');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000005')).toBe('working');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000006')).toBe('missing');
  }));

  it('records self-reported agent heartbeats and marks them stale after the threshold', () => fixture((live, sql, clock) => {
    expect(live.recordHeartbeat('grok-bot', null, { status: 'working', summary: 'triaging inbox' })).toEqual({ ok: true });
    expect(byId(live.evaluate().subjects, 'agent', 'grok-bot')).toBe('working');
    clock.t += (DEFAULT_THRESHOLDS.heartbeatStaleS + 1) * 1000;
    expect(byId(live.evaluate().subjects, 'agent', 'grok-bot')).toBe('stale');
    for (const bad of [{ status: 'stale' }, { status: 'Working!' }, { summary: 'x'.repeat(241) }, { summary: 'line\nbreak' }, { token: 'nope' }, null, []]) {
      expect('error' in live.recordHeartbeat('grok-bot', null, bad)).toBe(true);
    }
  }));

  it('uses the in-memory check-in when storage lags behind steady heartbeats', () => fixture((live, sql) => {
    const origNow = Date.now;
    try {
      machine(sql, 'steady', null);
      const beat = { type: 'heartbeat', mem_avail_mb: 4096, disk_free_gb: 100, load: [0.1], gate: { launch_allowed: true, reason: 'ok' }, running_jobs: [], sessions_summary: {} };
      Date.now = () => now - 240000; updateHeartbeat('steady', beat, sql);
      Date.now = () => now - 10000; updateHeartbeat('steady', beat, sql);
      // Unchanged telemetry is not persisted again within five minutes...
      expect(Date.parse(sql.exec("SELECT last_checkin_at FROM machines WHERE id = 'steady'").one().last_checkin_at)).toBe(now - 240000);
      // ...but liveness still sees the beat from ten seconds ago.
      const subject = live.evaluate().subjects.find(s => s.id === 'steady')!;
      expect([subject.state, subject.age_s]).toEqual(['online', 10]);
    } finally { Date.now = origNow; }
  }));

  it('reads thresholds from vars and ignores invalid values', () => {
    expect(thresholdsFrom({ HUB_MACHINE_STALE_S: '60', HUB_AGENT_STALL_S: 'soon', HUB_MACHINE_OFFLINE_S: '30' })).toEqual({ ...DEFAULT_THRESHOLDS, machineStaleS: 60, machineOfflineS: 60 });
  });
});

describe('stale alerts', () => {
  it('opens one alert per problem, updates it in place and resolves it on recovery', () => fixture((live, sql) => {
    machine(sql, 'hetzner', iso(300));
    let opened = live.reconcileAlerts(live.evaluate().subjects);
    expect(opened.map(a => [a.subject_kind, a.subject, a.state])).toEqual([['machine', 'hetzner', 'stale']]);
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', iso(3600), 'hetzner');
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    expect(live.alerts()[0].state).toBe('offline');
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', iso(10), 'hetzner');
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    expect(live.alerts()[0].resolved_at).not.toBeNull();
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', iso(400), 'hetzner');
    opened = live.reconcileAlerts(live.evaluate().subjects);
    expect(opened).toHaveLength(1);
  }));

  it('offers failed or untargeted notifications for retry after a delay', () => fixture((live, sql, clock) => {
    machine(sql, 'hetzner', iso(300));
    const [alert] = live.reconcileAlerts(live.evaluate().subjects);
    live.markNotified(alert.id, 'webhook: HTTP 500');
    expect(live.undelivered(900)).toEqual([]);
    clock.t += 901000;
    expect(live.undelivered(900).map((a: any) => a.id)).toEqual([alert.id]);
    live.markNotified(alert.id, null);
    clock.t += 901000;
    expect(live.undelivered(900)).toEqual([]);
  }));

  it('retries an alert whose notification was never attempted', () => fixture((live, sql, clock) => {
    machine(sql, 'hetzner', iso(300));
    const [alert] = live.reconcileAlerts(live.evaluate().subjects);
    expect(live.undelivered(900)).toEqual([]);
    clock.t += 901000;
    expect(live.undelivered(900).map((a: any) => a.id)).toEqual([alert.id]);
  }));

  it('delivers each target once and retries only the failed ones', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('notify-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      hub.env = { ...hub.env, HUB_ALERT_TO: 'nobody-registered', HUB_ALERT_WEBHOOK: 'https://hooks.example/alert' } as Env;
      const calls: string[] = []; const original = globalThis.fetch;
      globalThis.fetch = (async (url: any) => { calls.push(String(url)); return new Response('ok'); }) as any;
      try {
        hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('m', 'm', 'm', ?, ?, 0)", new Date().toISOString(), new Date(Date.now() - 3600000).toISOString());
        await hub.runChecks('liveness');
        let [alert]: any[] = hub.livenessService.alerts();
        expect(calls).toEqual(['https://hooks.example/alert']);
        expect(alert.notify_error).toContain('nobody-registered');
        expect(JSON.parse(alert.delivered_to)).toEqual(['webhook']);
        hub.sql.exec('UPDATE liveness_alerts SET notified_at = ?', new Date(Date.now() - 3600000).toISOString());
        await hub.runChecks('liveness');
        expect(calls).toHaveLength(1);
      } finally { globalThis.fetch = original; }
    });
  });

  it('delivers alerts to the grok-bot inbox by default when HUB_ALERT_TO is unset', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('notify-default-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      delete (hub.env as any).HUB_ALERT_TO;
      hub.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('grok-bot', NULL, NULL, NULL)");
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('m', 'm', 'm', ?, ?, 0)", new Date().toISOString(), new Date(Date.now() - 3600000).toISOString());
      await hub.runChecks('liveness');
      const [alert]: any[] = hub.livenessService.alerts();
      expect(alert.notify_error).toBeNull();
      expect(JSON.parse(alert.delivered_to)).toEqual(['inbox:grok-bot']);
    });
  });

  it('accepts agent heartbeats only with the health:report scope', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('heartbeat-scope-' + crypto.randomUUID()));
    const tokens: Record<string, string> = { reader: 'reader-token-' + crypto.randomUUID(), reporter: 'reporter-token-' + crypto.randomUUID() };
    await runInDurableObject(stub, async (hub: Hub) => {
      for (const [name, scopes] of [['reader', ['bus:read']], ['reporter', ['bus:read', 'health:report']]] as const) {
        hub.sql.exec('INSERT INTO agents(tag,registered_at) VALUES (?,?)',name,new Date().toISOString());
        hub.sql.exec('INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
          name, 'agent', await sha256(tokens[name]), name, name, name, JSON.stringify(scopes), 'test', new Date().toISOString(), new Date(Date.now() + 3600000).toISOString());
      }
    });
    const beat = (name: string) => stub.fetch(new Request('https://hub.test/api/heartbeat', { method: 'POST', headers: { Authorization: 'Bearer ' + tokens[name] }, body: JSON.stringify({ status: 'working' }) }));
    expect((await beat('reader')).status).toBe(403);
    expect((await beat('reporter')).status).toBe(200);
  });

  it('requires authentication for liveness and heartbeat endpoints', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hub'));
    expect((await stub.fetch(new Request('https://hub.test/api/liveness'))).status).toBe(401);
    expect((await stub.fetch(new Request('https://hub.test/api/heartbeat', { method: 'POST', body: '{}' }))).status).toBe(401);
    expect((await stub.fetch(new Request('https://hub.test/api/checks'))).status).toBe(401);
    expect((await stub.fetch(new Request('https://hub.test/api/checks/liveness/run', { method: 'POST' }))).status).toBe(401);
  });
});


describe('memory pressure alerts', () => {
  it('retains a real admission-created memory hold until fresh capacity promotes the same request', () => fixture((live, sql, clock) => {
    machine(sql, 'capacity-probe', iso(0));
    const fleet = new FleetService(sql, fn => fn(), () => clock.t);
    const admin = {caller:{isAdmin:true, actor:'memory-probe', kind:'human'}, scopes:['admin']};
    sql.exec('UPDATE machines SET capabilities=? WHERE id=?', JSON.stringify({engines:['codex'],fleet_modes:['headless'],workspaces:['demo'],cpu_count:8}), 'capacity-probe');
    const beat = (available: number) => {
      const at = new Date(clock.t).toISOString();
      sql.exec('UPDATE machines SET last_checkin_at=?, last_heartbeat=? WHERE id=?', at, JSON.stringify({mem_avail_mb:available, disk_free_gb:80, cpu_usage:freshCpu(at), gate:{launch_allowed:true,at}}), 'capacity-probe');
    };
    beat(2400);
    sql.exec("INSERT INTO fleet_accounts VALUES ('probe-account','codex',8,15,1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('capacity-probe','codex','probe-account',NULL)");
    sql.exec('INSERT INTO host_quota_snapshots (host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES (?,?,?,?,?,?,?,?,?)', 'capacity-probe','1.0',iso(0),now,iso(0),now,'ok',null,
      JSON.stringify({codex:{status:'ok',observed_at:iso(0),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:new Date(now+86400000).toISOString()}}}}));
    const input = {machine:'capacity-probe',workspace:'demo',mode:'headless',prompt:'fixture only',idempotency_key:'genuine-memory-hold'};
    const held = fleet.reserve(input, admin);
    expect(held.status).toBe(202);
    expect(held.value.state).toBe('queued');
    expect(held.value.rejected).toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
    clock.t += 301000;
    beat(2400);
    const subjects = live.evaluate().subjects;
    expect(subjects.find(s => s.id === held.value.id)).toMatchObject({state:'launcher_memory_reserve',memory_event:{classification:'active'}});
    const opened = live.reconcileAlerts(subjects);
    expect(opened).toHaveLength(1);
    expect(opened[0].resolved_at).toBeNull();
    fleet.drainCapacityQueue();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',held.value.id).one().state).toBe('queued');
    beat(32000);
    fleet.drainCapacityQueue();
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',held.value.id).one().state).toBe('offered');
    live.reconcileAlerts(live.evaluate().subjects);
    expect(live.alerts().find(a => a.id === opened[0].id)?.resolved_at).toEqual(expect.any(String));
    expect(fleet.resources.config().memory_floor_mb).toBe(2048);
  }));

  it('reads exact retained alert history behind more than one hundred newer open alerts without mutation', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('memory-history-' + crypto.randomUUID()));
    let cookie = '';
    let original: any;
    await runInDurableObject(stub, async (hub: Hub) => {
      cookie = (await createSessionCookie(hub.env, {sub:'history-admin', email:'admin@fake.local', isAdmin:true, csrfToken:'history-csrf', exp:Math.floor(Date.now()/1000)+3600})).split(';')[0];
      const live = hub.livenessService;
      original = live.setAlert('launch', 'old-memory-launch', {state:'launcher_memory_reserve', detail:'original failure'});
      live.setAlert('launch', 'old-memory-launch', null);
      for (let i = 0; i < 101; i++) live.setAlert('session', `new-session-${i}`, {state:'stalled', detail:'newer problem'});
      expect(live.alerts().some(a => a.subject === 'old-memory-launch')).toBe(false);
    });
    const url = 'https://hub.test/api/liveness?alert_subject=old-memory-launch';
    const get = () => stub.fetch(new Request(url, {headers:{Cookie:cookie}}));
    const response = await get();
    expect(response.status).toBe(200);
    const result: any = await response.json();
    expect(result.alerts).toHaveLength(1);
    expect(result.alerts[0]).toMatchObject({...original, resolved_at: expect.any(String)});
    expect(await (await get()).json()).toEqual(result);
    expect((await stub.fetch(new Request(url))).status).toBe(401);
    for (const value of ['', 'x'.repeat(129), 'bad\nsubject']) {
      expect((await stub.fetch(new Request('https://hub.test/api/liveness?alert_subject='+encodeURIComponent(value), {headers:{Cookie:cookie}}))).status).toBe(400);
    }
    await runInDurableObject(stub, (hub: Hub) => {
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM liveness_alerts').one().n).toBe(102);
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM liveness_alerts WHERE resolved_at IS NULL').one().n).toBe(101);
    });
  });

  it('alerts once after five minutes with numbers and resolves after promotion', () => fixture((live, sql, clock) => {
    launch(sql, 'memory-held', '', 'queued', 0);
    const rejected = [{reason: 'launcher_memory_reserve', memory: {mem_available_mb: 12000, reserved_mb: 2000, floor_mb: 10240}}];
    sql.exec('INSERT INTO fleet_memory_holds (launch_id,since,rejected_json) VALUES (?,?,?)', 'memory-held', iso(0), JSON.stringify(rejected));
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(0);
    clock.t += 301000;
    const opened = live.reconcileAlerts(live.evaluate().subjects);
    expect(opened).toHaveLength(1);
    expect(opened[0].detail).toContain('12000');
    expect(opened[0].detail).toContain('10240');
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(0);
    sql.exec("UPDATE fleet_launches SET state='offered' WHERE id='memory-held'");
    live.reconcileAlerts(live.evaluate().subjects);
    expect(sql.exec('SELECT resolved_at FROM liveness_alerts WHERE id=?', opened[0].id).one().resolved_at).not.toBeNull();
  }));
  it('alerts immediately for a queued fenced refusal while retaining terminal failures as history', () => fixture((live, sql) => {
    launch(sql, 'memory-retry', '', 'queued', 0);
    sql.exec("INSERT INTO fleet_memory_holds (launch_id,since,rejected_json,refused) VALUES (?,?,?,1)", 'memory-retry', iso(0), '[{"reason":"launcher_memory_reserve","memory":{"floor_mb":10240}}]');
    launch(sql, 'memory-failed', '', 'failed', 0);
    sql.exec("UPDATE fleet_launches SET exit_reason='launcher_memory_reserve' WHERE id='memory-failed'");
    const subjects = live.evaluate().subjects;
    expect(subjects.find(s => s.id === 'memory-failed')).toMatchObject({ state: 'historical', memory_event: { classification: 'historical' } });
    expect(live.reconcileAlerts(subjects).map(a => a.subject)).toEqual(['memory-retry']);
  }));

  it('keeps all twenty terminal refusals and their alerts while removing the false active projection', () => fixture((live, sql) => {
    machine(sql, 'recovered', iso(0));
    sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?', JSON.stringify({mem_avail_mb: 40000, launcher_resources: {active_mem_mb: 3000}}), 'recovered');
    for (let i = 0; i < 20; i++) {
      const id = `original-${i}`;
      launch(sql, id, 'recovered', 'failed', 3600);
      sql.exec("UPDATE fleet_launches SET exit_reason='launcher_memory_reserve', verification_status='not_started', finished_at=? WHERE id=?", iso(3500), id);
      live.setAlert('launch', id, {state: 'launcher_memory_reserve', detail: 'original refusal evidence'});
    }
    const before = sql.exec('SELECT * FROM fleet_launches ORDER BY id').toArray();
    const alerts = live.alerts();
    const subjects = live.evaluate().subjects;
    const historical = subjects.filter(s => s.memory_event?.classification === 'historical');
    expect(historical).toHaveLength(20);
    expect(historical.every(s => s.state === 'historical' && s.memory_event?.native_id === null)).toBe(true);
    expect(historical.every(s => !s.detail.includes('40000') && !s.detail.includes('3000'))).toBe(true);
    expect(live.reconcileAlerts(subjects)).toEqual([]);
    const after = live.alerts();
    expect(after).toHaveLength(20);
    for (const original of alerts) {
      expect(after.find(a => a.id === original.id)).toEqual({...original, resolved_at: new Date(now).toISOString()});
    }
    expect(sql.exec('SELECT * FROM fleet_launches ORDER BY id').toArray()).toEqual(before);
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
  }));

  it('reports a superseded refusal separately from a genuine queued retry, then retires only the hold on promotion', () => fixture((live, sql) => {
    launch(sql, 'old-refusal', '', 'exited', 600);
    launch(sql, 'current-retry', '', 'queued', 0);
    const evidence = JSON.stringify([{reason:'launcher_memory_reserve', memory:{mem_available_mb: 2200, reserved_mb: 512, requested_mb: 512, floor_mb: 2048}}]);
    sql.exec('INSERT INTO fleet_memory_holds (launch_id,since,rejected_json,refused,retry_id) VALUES (?,?,?,?,?)', 'old-refusal', iso(600), evidence, 1, 'current-retry');
    sql.exec('INSERT INTO fleet_memory_holds (launch_id,since,rejected_json,refused) VALUES (?,?,?,1)', 'current-retry', iso(0), evidence);
    live.setAlert('launch', 'old-refusal', {state: 'launcher_memory_reserve', detail: 'original refusal'});
    const subjects = live.evaluate().subjects;
    expect(subjects.find(s => s.id === 'old-refusal')).toMatchObject({state:'historical', memory_event:{classification:'superseded', retry_id:'current-retry', retry_state:'queued'}});
    expect(subjects.find(s => s.id === 'current-retry')).toMatchObject({state:'launcher_memory_reserve', memory_event:{classification:'active'}});
    expect(live.reconcileAlerts(subjects).map(a => a.subject)).toEqual(['current-retry']);
    expect(live.alerts().find(a => a.subject === 'old-refusal')?.resolved_at).toEqual(expect.any(String));
    expect(live.alerts().find(a => a.subject === 'current-retry')?.resolved_at).toBeNull();
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    sql.exec("UPDATE fleet_launches SET state='offered' WHERE id='current-retry'");
    live.reconcileAlerts(live.evaluate().subjects);
    expect(live.alerts().every(a => a.resolved_at !== null)).toBe(true);
    expect(sql.exec('SELECT * FROM fleet_memory_holds').toArray()).toHaveLength(2);
  }));

  it('does not infer a successful retry when a superseded attempt has a missing retry record', () => fixture((live, sql) => {
    launch(sql, 'superseded', '', 'exited', 600);
    sql.exec('INSERT INTO fleet_memory_holds (launch_id,since,rejected_json,refused,retry_id) VALUES (?,?,?,1,?)', 'superseded', iso(600), '[{"reason":"launcher_memory_reserve"}]', 'not-observed');
    expect(live.evaluate().subjects.find(s => s.id === 'superseded')).toMatchObject({state:'historical', memory_event:{classification:'superseded', retry_state:'unknown'}});
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
  }));
});
