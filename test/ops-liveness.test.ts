import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { OpsLiveness, OPS_WINDOW_MS } from '../src/ops-liveness';
import { IntakeWatchdog, queuePolicyFrom } from '../src/intake-watchdog';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 500fd60b: Hub-side orchestrator/backlog liveness that does not depend on the
// orchestrator session or the host watchdog.
const NOW = Date.parse('2026-10-29T02:16:00Z');
const MIN = 60000;
const isoAt = (ms: number) => new Date(ms).toISOString();

async function fixture(fn: (h: Hub, clock: { t: number }) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('ops-liveness-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (h: Hub) => fn(h, { t: NOW }));
}
function intake(h: Hub, id: string, ac = 'Do the thing', status = 'open', project = 'demo', created = NOW - 86400000) {
  const data = { id, project, title: id, status, version: 1, created_at: created, updated_at: created, acceptance_criteria: ac, history: [] };
  h.sql.exec('INSERT OR REPLACE INTO intake_task_projection(id,data,links) VALUES(?,?,?)', id, JSON.stringify(data), '[]');
  return data;
}
function session(h: Hub, id: string, taskId: string, state: string) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id)
    VALUES(?,'admin',?,?,'claude','interactive',?,?,?,'hetzner')`, id, id, JSON.stringify({ intake_task_id: taskId }), state, isoAt(NOW), isoAt(NOW));
}
function service(h: Hub, clock: { t: number }, hooks: any = {}) {
  const alerts: [string, string | null][] = [];
  const s = new OpsLiveness(h.sql, { alert: async (subject: string, detail: string | null) => { alerts.push([subject, detail]); return true; }, ...hooks }, () => clock.t);
  s.init();
  return { s, alerts, open: () => alerts.filter(([, d]) => d !== null) };
}
const tick = async (s: OpsLiveness, clock: { t: number }, minutes: number) => { clock.t += minutes * MIN; return s.tick(); };

describe('ops liveness (intake 500fd60b)', () => {
  it('validates reports and never trusts a future relayed tick time', () => fixture((h, clock) => {
    const { s } = service(h, clock);
    expect(s.record({ role: 'orchestrator', kind: 'tick', summary: 'tick 1' })).toEqual({ ok: true });
    expect(s.record({ role: 'root', kind: 'tick' })).toEqual({ error: 'invalid_role' });
    expect(s.record({ role: 'orchestrator', kind: 'beat' })).toEqual({ error: 'invalid_kind' });
    expect(s.record({ role: 'orchestrator', kind: 'watchdog', state: 'fine' })).toEqual({ error: 'invalid_state' });
    expect(s.record({ role: 'orchestrator', kind: 'watchdog', recovery_failures: -1 })).toEqual({ error: 'invalid_recovery_failures' });
    expect(s.record({ role: 'orchestrator', kind: 'watchdog', token: 'x' })).toEqual({ error: 'invalid_ops_heartbeat' });
    expect(s.record({ role: 'orchestrator', kind: 'watchdog', tick_at: isoAt(NOW + 3600000) })).toEqual({ error: 'invalid_tick_at' });
    expect(s.record({ role: 'orchestrator', kind: 'tick', summary: 'bad\nline' })).toEqual({ error: 'invalid_summary' });
    expect(s.status().roles.orchestrator).toMatchObject({ state: 'watchdog_stale', tick_age_s: 0, tick_summary: 'tick 1' });
    expect(s.status().roles.oncall).toMatchObject({ state: 'never_reported' });
  }));

  it('reproduces 29 Oct: a live watchdog saying OK while no tick arrives is tick_stale, not ok', () => fixture(async (h, clock) => {
    const { s, open } = service(h, clock);
    s.record({ role: 'orchestrator', kind: 'tick' });
    for (let i = 0; i < 6; i++) { clock.t += 10 * MIN; s.record({ role: 'orchestrator', kind: 'watchdog', state: 'ok', tick_at: isoAt(NOW) }); await s.tick(); }
    expect(s.status().roles.orchestrator).toMatchObject({ state: 'tick_stale', tick_age_s: 3600, watchdog_state: 'ok' });
    // Stale ticks alone are the watchdog's job to recover; no alert while it still can.
    expect(open()).toEqual([]);
  }));

  it('alerts only after two consecutive failed recoveries and resolves on proven recovery', () => fixture(async (h, clock) => {
    const { s, alerts, open } = service(h, clock);
    s.record({ role: 'orchestrator', kind: 'watchdog', state: 'proof_failed', recovery_failures: 1 });
    await tick(s, clock, 1);
    expect(open()).toEqual([]);
    s.record({ role: 'orchestrator', kind: 'watchdog', state: 'proof_failed', recovery_failures: 2 });
    await tick(s, clock, 1);
    expect(open()).toHaveLength(1);
    expect(open()[0][0]).toBe('ops-orchestrator');
    expect(open()[0][1]).toContain('failed 2 times in a row');
    s.record({ role: 'orchestrator', kind: 'tick' });
    s.record({ role: 'orchestrator', kind: 'watchdog', state: 'ok', recovery_failures: 0 });
    await tick(s, clock, 1);
    expect(alerts.at(-1)).toEqual(['ops-orchestrator', null]);
  }));

  it('detects a dead watchdog independently and alerts after two missed windows', () => fixture(async (h, clock) => {
    const { s, open } = service(h, clock);
    s.record({ role: 'orchestrator', kind: 'watchdog', state: 'ok' });
    s.record({ role: 'orchestrator', kind: 'tick' });
    await tick(s, clock, 31);
    expect(s.status().roles.orchestrator.state).toBe('tick_and_watchdog_stale');
    expect(open()).toEqual([]);
    await tick(s, clock, 30);
    expect(open()).toHaveLength(1);
    expect(open()[0][1]).toContain('ops-watchdog silent for 3660s');
  }));

  it('backlog idle 30 min: records why, starts work itself, alerts after two failed starts, resolves when work runs', () => fixture(async (h, clock) => {
    intake(h, 'with-ac'); intake(h, 'no-ac', '   '); intake(h, 'blocked', 'x', 'blocked');
    const kicks: any[] = [];
    const { s, alerts, open } = service(h, clock, { kick: async (why: any) => { kicks.push(why); return { task_id: 'with-ac', reason: 'queued:canonical_held' }; },
      holdReasons: () => ({ watchdog_project_not_enabled: 640 }) });
    s.record({ role: 'orchestrator', kind: 'watchdog', state: 'ok' });
    session(h, 'done-before', 'with-ac', 'exited');
    expect((await s.tick()).idle).toMatchObject({ state: 'idle_started', open_with_ac: 1, active_sessions: 0 });
    expect((await tick(s, clock, 29)).idle.state).toBe('idle');
    expect(kicks).toHaveLength(0);
    expect((await tick(s, clock, 1)).idle).toMatchObject({ state: 'kicked', failed_kicks: 0 });
    expect(kicks[0]).toMatchObject({ open_with_ac: 1, active_sessions: 0, orchestrator: 'tick_stale', queue_holds: { watchdog_project_not_enabled: 640 } });
    const idle = s.status().idle;
    expect(idle).toMatchObject({ kicks: 1, idle_for_s: 1800, why_idle: { orchestrator: 'tick_stale' }, last_kick: { task_id: 'with-ac' } });
    expect((await tick(s, clock, 30)).idle).toMatchObject({ state: 'kicked', failed_kicks: 1 });
    expect(open()).toEqual([]);
    expect((await tick(s, clock, 30)).idle).toMatchObject({ state: 'kicked', failed_kicks: 2 });
    expect(open().map(a => a[0])).toContain('ops-backlog-idle');
    expect(open().find(a => a[0] === 'ops-backlog-idle')![1]).toContain('2 automatic starts in a row');
    session(h, 'kicked-session', 'with-ac', 'running');
    expect((await tick(s, clock, 1)).idle.state).toBe('working');
    expect(alerts.at(-1)).toEqual(['ops-backlog-idle', null]);
    expect(s.status().idle).toBeNull();
    expect(s.status().recent_idle[0]).toMatchObject({ kicks: 3, failed_kicks: 2, resolved_at: expect.any(String) });
  }));

  it('a working intake session or an empty backlog is never an idle episode', () => fixture(async (h, clock) => {
    const { s } = service(h, clock, { kick: async () => { throw new Error('must not kick'); } });
    expect((await s.tick()).idle.state).toBe('no_backlog');
    intake(h, 'busy'); session(h, 'busy-session', 'busy', 'starting');
    for (let i = 0; i < 4; i++) expect((await tick(s, clock, 30)).idle.state).toBe('working');
  }));

  it('idle kick starts a real backlog intake through canonical admission when its project is not enabled', () => fixture(async (h, clock) => {
    h.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,capabilities) VALUES('ops-host','ops-host','ops-host',?,?)", isoAt(NOW), JSON.stringify({ workspaces: ['demo'] }));
    const older = intake(h, 'z-older', 'AC', 'open', 'demo', NOW - 2 * 86400000), newer = intake(h, 'a-newer');
    const inputs: any[] = [];
    const proofOnly = () => queuePolicyFrom({ HUB_INTAKE_QUEUE_ENABLED: 'true', HUB_INTAKE_QUEUE_PROJECTS: 'agent-hub-queue-proof' });
    const make = (policy: () => any) => {
      const w = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => [older, newer] } as any, (f: any) => f(), { policy,
        start: async (input: any) => { inputs.push(input); session(h, 'kick-1', input.intake_task_id, 'queued'); return { status: 202, body: { id: 'kick-1', state: 'queued' } }; } }, () => clock.t);
      w.init(); return w;
    };
    await make(proofOnly).tick();
    expect(inputs).toHaveLength(0); // continuous pickup stays scoped to the proof project
    const result = await make(proofOnly).idleKick();
    expect(inputs).toHaveLength(1);
    expect(inputs[0]).toMatchObject({ intake_task_id: 'z-older', workspace: 'demo', with_goal: true });
    expect(result).toEqual({ task_id: 'z-older', reason: 'submitted:canonical_queued' });
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM intake_audit WHERE task_id='z-older' AND kind='idle_kick'").one().n).toBe(1);
    // An operator's explicit disable wins over the idle kick.
    expect(await make(() => queuePolicyFrom({ HUB_INTAKE_QUEUE_ENABLED: 'false' })).idleKick()).toEqual({ task_id: null, reason: 'watchdog_disabled' });
    expect(inputs).toHaveLength(1);
  }));

  it('serves heartbeat and status over HTTP for admins only, and the minute alarm evaluates it', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('ops-liveness-http-' + crypto.randomUUID()));
    let cookie = '';
    await runInDurableObject(stub, async (hub: Hub) => {
      cookie = (await createSessionCookie(hub.env, { sub: 'ops-admin', email: 'admin@fake.local', isAdmin: true, csrfToken: 'ops-csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
    });
    const post = (body: any, headers: any = { Cookie: cookie, 'x-csrf-token': 'ops-csrf' }) => stub.fetch(new Request('https://hub.test/api/ops/heartbeat',
      { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) }));
    expect((await post({ role: 'orchestrator', kind: 'tick' }, {})).status).toBe(401);
    const ok = await post({ role: 'orchestrator', kind: 'watchdog', state: 'ok', tick_at: new Date().toISOString() });
    expect(ok.status).toBe(200);
    expect((await ok.json() as any).ops.roles.orchestrator.state).toBe('ok');
    expect((await post({ role: 'orchestrator', kind: 'nope' })).status).toBe(400);
    const status = await stub.fetch(new Request('https://hub.test/api/ops/liveness', { headers: { Cookie: cookie } }));
    expect(status.status).toBe(200);
    expect((await status.json() as any).window_s).toBe(OPS_WINDOW_MS / 1000);
    await runInDurableObject(stub, async (hub: Hub) => {
      await hub.alarm();
      expect(hub.sql.exec('SELECT last_eval FROM ops_liveness_meta WHERE id=1').one().last_eval).toEqual(expect.any(Number));
    });
  });
});
