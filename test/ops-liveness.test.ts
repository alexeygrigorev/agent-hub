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
function session(h: Hub, id: string, taskId: string, state: string, at = NOW, native: string | null = null) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
    VALUES(?,'admin',?,?,'claude','interactive',?,?,?,'hetzner',?)`, id, id, JSON.stringify({ intake_task_id: taskId }), state, isoAt(at), isoAt(at),
    native ? JSON.stringify({ native_id: native }) : null);
}
/** Host census observing a live native session at `at`, whose last sampled progress was `progress`. */
function observe(h: Hub, native: string, at: number, progress: number | null = at) {
  h.sql.exec("INSERT OR REPLACE INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,progress_json) VALUES('hetzner',?,?,'running',?,'demo',?)",
    native, native, isoAt(at), JSON.stringify(progress === null ? {} : { last_progress_at: isoAt(progress) }));
  h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('hetzner',?)", isoAt(at));
}
function service(h: Hub, clock: { t: number }, hooks: any = {}) {
  // Mirrors LivenessService.setAlert: record only open/resolve transitions per subject.
  const alerts: [string, string | null][] = [], current = new Map<string, boolean>();
  h.sql.exec('DELETE FROM ops_liveness_meta');  // the Hub constructor seeded it with the real clock
  const s = new OpsLiveness(h.sql, { alert: async (subject: string, detail: string | null) => {
    const opened = detail !== null;
    if ((current.get(subject) ?? false) === opened) return false;
    current.set(subject, opened); alerts.push([subject, detail]); return opened;
  }, ...hooks }, () => clock.t);
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
    expect(s.status().roles.orchestrator).toMatchObject({ state: 'ok', tick_age_s: 0, tick_summary: 'tick 1', watchdog_at: null });
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

  it('an expected orchestrator that never reports alerts after two windows', () => fixture(async (h, clock) => {
    const { s, open } = service(h, clock);
    await s.tick();
    expect(s.status().roles.orchestrator.state).toBe('never_reported_ok');
    await tick(s, clock, 31);
    expect(s.status().roles.orchestrator.state).toBe('never_reported_tick_and_watchdog_stale');
    expect(open()).toEqual([]);
    await tick(s, clock, 30);
    expect(open()).toHaveLength(1);
    expect(open()[0][1]).toContain('ops-watchdog never reported for 3660s');
  }));

  it('tick-only reports (watchdog never installed) still alert after two windows', () => fixture(async (h, clock) => {
    const { s, open } = service(h, clock);
    for (let i = 0; i < 8; i++) { s.record({ role: 'orchestrator', kind: 'tick' }); await tick(s, clock, 10); }
    expect(s.status().roles.orchestrator).toMatchObject({ state: 'watchdog_stale', watchdog_at: null });
    expect(open()).toHaveLength(1);
    expect(open()[0][1]).toContain('ops-watchdog never reported');
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
    expect(kicks[0]).toMatchObject({ open_with_ac: 1, active_sessions: 0, orchestrator: expect.any(String), orchestrator_tick_age_s: null, queue_holds: { watchdog_project_not_enabled: 640 } });
    const idle = s.status().idle;
    expect(idle).toMatchObject({ kicks: 1, idle_for_s: 1800, why_idle: { orchestrator_tick_age_s: null }, last_kick: { task_id: 'with-ac' } });
    expect((await tick(s, clock, 30)).idle).toMatchObject({ state: 'kicked', failed_kicks: 1 });
    expect(open()).toEqual([]);
    expect((await tick(s, clock, 30)).idle).toMatchObject({ state: 'kicked', failed_kicks: 2 });
    expect(open().map(a => a[0])).toContain('ops-backlog-idle');
    expect(open().find(a => a[0] === 'ops-backlog-idle')![1]).toContain('2 automatic starts in a row');
    session(h, 'kicked-session', 'with-ac', 'running', clock.t, 'native-kicked');
    expect((await tick(s, clock, 1)).idle.state).toBe('idle');  // running row without live native evidence is not work
    observe(h, 'native-kicked', clock.t);
    expect((await tick(s, clock, 1)).idle.state).toBe('working');
    expect(alerts.at(-1)).toEqual(['ops-backlog-idle', null]);
    expect(s.status().idle).toBeNull();
    expect(s.status().recent_idle[0]).toMatchObject({ kicks: 3, failed_kicks: 2, resolved_at: expect.any(String) });
  }));

  it('retained rows are not work: stale offered, running without a live native, rows for closed intakes', () => fixture(async (h, clock) => {
    intake(h, 'waiting'); intake(h, 'closed', 'AC', 'done');
    const kicks: any[] = [];
    const { s } = service(h, clock, { kick: async () => { kicks.push(1); return { task_id: null, reason: 'no_ready_intake' }; } });
    session(h, 'old-offer', 'waiting', 'offered', NOW - 31 * MIN);
    session(h, 'ghost-running', 'waiting', 'running', NOW, 'ghost-native');
    observe(h, 'ghost-native', NOW - 11 * MIN);  // host census stopped seeing it
    session(h, 'closed-live', 'closed', 'running', NOW, 'closed-native'); observe(h, 'closed-native', NOW);
    // The stale offer is the last evidence of work (31 min ago): already proven idle, kick now.
    expect((await s.tick()).idle).toMatchObject({ state: 'kicked', active_sessions: 0 });
    expect(kicks).toHaveLength(1);
    session(h, 'fresh-offer', 'waiting', 'offered', clock.t);
    expect((await tick(s, clock, 1)).idle.state).toBe('working');
  }));

  it('a freshly observed but idle process is not work; the idle clock starts at its last progress', () => fixture(async (h, clock) => {
    intake(h, 'stuck');
    const kicks: number[] = [];
    const { s } = service(h, clock, { kick: async () => { kicks.push(clock.t); return { task_id: 'stuck', reason: 'queued:held' }; } });
    session(h, 'idle-run', 'stuck', 'running', NOW - 6 * 3600000, 'idle-native');
    for (let m = 0; m <= 120; m++) {   // census every minute; progress stays 4-6 h old
      observe(h, 'idle-native', clock.t, NOW - 4 * 3600000);
      const r = await s.tick();
      expect(r.idle.active_sessions).toBe(0);
      clock.t += MIN;
    }
    // Last work 4 h ago: the first evaluation kicks, then one kick per 30 min, never 60 min late.
    expect(kicks[0]).toBe(NOW);
    expect(kicks.map(k => (k - NOW) / MIN)).toEqual([0, 30, 60, 90, 120]);
    expect(s.status().idle.why_idle.last_work_at).toBe(isoAt(NOW - 4 * 3600000));
  }));

  it('pickup happens about 30 min after the last progress, not 60 (no second window)', () => fixture(async (h, clock) => {
    intake(h, 'recent');
    const kicks: number[] = [];
    const { s } = service(h, clock, { kick: async () => { kicks.push(clock.t); return { task_id: 'recent', reason: 'submitted:canonical_queued' }; } });
    const last = NOW - 10 * MIN;
    session(h, 'recent-run', 'recent', 'running', NOW - 3600000, 'recent-native');
    for (let m = 0; m <= 25; m++) {   // census every minute, progress frozen at `last`
      observe(h, 'recent-native', clock.t, last);
      const r = await s.tick();
      if (clock.t - last <= 30 * MIN) expect(r.idle.state).toBe('working');
      clock.t += MIN;
    }
    expect(kicks).toHaveLength(1);
    expect((kicks[0] - last) / MIN).toBe(31);  // first minute tick after progress turned 30 min old
  }));

  it('a live working intake session or an empty backlog is never an idle episode', () => fixture(async (h, clock) => {
    const { s } = service(h, clock, { kick: async () => { throw new Error('must not kick'); } });
    expect((await s.tick()).idle.state).toBe('no_backlog');
    intake(h, 'busy'); session(h, 'busy-session', 'busy', 'running', NOW, 'busy-native');
    for (let i = 0; i < 4; i++) { await tick(s, clock, 30); observe(h, 'busy-native', clock.t); clock.t += 1; expect((await tick(s, clock, 1)).idle.state).toBe('working'); }
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
    // Unknown work (live-looking session, stale host observation) is never duplicated.
    const unknownIntake = intake(h, 'b-unknown', 'AC', 'open', 'demo', NOW - 3 * 86400000);
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
      VALUES('unknown-run','hub:service:intake-watchdog','unknown-run',?,'codex','interactive','running',?,?,'ops-host',?)`,
      JSON.stringify({ intake_task_id: 'b-unknown', workspace: 'demo', with_goal: true }), isoAt(NOW), isoAt(NOW), JSON.stringify({ native_id: 'unknown-native', tag: 'native', workspace: 'demo', native_created_at_ms: NOW - 3600000 }));
    const before = inputs.length;
    const w2 = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => [unknownIntake] } as any, (f: any) => f(), { policy: proofOnly,
      start: async (input: any) => { inputs.push(input); return { status: 202, body: { id: 'dup', state: 'queued' } }; } }, () => clock.t);
    w2.init();
    expect(await w2.idleKick()).toEqual({ task_id: null, reason: 'no_ready_intake' });
    expect(inputs.length).toBe(before);
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
