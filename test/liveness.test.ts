import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { LivenessService, thresholdsFrom, DEFAULT_THRESHOLDS } from '../src/liveness';
import { FleetService } from '../src/fleet';
import { updateHeartbeat } from '../src/registry';
import { sha256 } from '../src/auth/crypto';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

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
    const { subjects } = live.evaluate();
    expect(byId(subjects, 'session', 'hetzner/busy')).toBe('working');
    expect(byId(subjects, 'session', 'hetzner/hung')).toBe('stalled');
    expect(byId(subjects, 'session', 'hetzner/waiting')).toBe('idle');
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
    const { subjects } = live.evaluate();
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000001')).toBe('stuck');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000002')).toBe('offered');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000003')).toBe('uncertain');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000004')).toBe('missing');
    expect(byId(subjects, 'launch', 'a0000000-0000-4000-8000-000000000005')).toBe('working');
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

  it('accepts agent heartbeats only with the health:report scope', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('heartbeat-scope-' + crypto.randomUUID()));
    const tokens: Record<string, string> = { reader: 'reader-token-' + crypto.randomUUID(), reporter: 'reporter-token-' + crypto.randomUUID() };
    await runInDurableObject(stub, async (hub: Hub) => {
      for (const [name, scopes] of [['reader', ['bus:read']], ['reporter', ['bus:read', 'health:report']]] as const) {
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
