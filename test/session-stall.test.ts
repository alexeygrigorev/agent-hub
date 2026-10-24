import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { LivenessService } from '../src/liveness';
import { FleetService } from '../src/fleet';
import { classifySessionStall, stallThreshold } from '../src/session_stall';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const id = '11111111-1111-4111-8111-111111111111';
const start = Date.parse('2026-10-24T12:00:00Z');
const admin = { caller: { isAdmin: true, kind: 'human', actor: 'test-admin' }, scopes: ['admin'] };
async function fixture(fn: (s: SessionControlService, live: LivenessService, sql: any, clock: { now: number }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-stall-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: start };
    const s = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), () => clock.now);
    s.init();
    const live = new LivenessService(hub.sql, undefined, () => clock.now); live.init();
    const at = new Date(start).toISOString(), created = new Date(start - 11000000).toISOString();
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('test-host', 'test-host', 'test-host', ?, ?)", created, at);
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'test', '{}', 'test-host', 'codex', 'headless', 'running', ?, ?, ?)`, id,
      JSON.stringify({ native_id: id, tag: 'test', workspace: '/test', native_created_at_ms: start - 11000000 }), created, at);
    hub.sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, last_activity_at, created_at_ms)
      VALUES ('test-host', ?, 'test', 'codex', 'running', 'idle', ?, '/test', ?, ?)`, id, at, new Date(start - 1800000).toISOString(), start - 11000000);
    await fn(s, live, hub.sql, clock);
  });
}
async function view(s: SessionControlService, path = '/' + id) {
  return await (await s.handle(new Request('https://hub.test/api/sessions' + path), admin)).json() as any;
}
function native(sql: any) { return sql.exec('SELECT * FROM fleet_sessions').toArray()[0]; }

describe('stalled running sessions', () => {
  it('round trips bounded host progress and rejects arbitrary process or file payloads', async () => fixture(async (s, live, sql, clock) => {
    const fleet = new FleetService(sql, cb => cb(), () => clock.now); fleet.init();
    const row = { native_id: id, tag: 'test', workspace: '/test', engine: 'codex', phase: 'running', agent_state: 'idle', profile: null, account: null,
      created_at_ms: start - 11000000, last_activity_at: new Date(start - 1800000).toISOString(),
      progress: { last_progress_at: new Date(start).toISOString(), headless: true, agent_alive: true, orphaned: false, cleanup: 'none' } };
    const report = (data: any, auth: any = { caller: { machineId: 'test-host', kind: 'host', actor: 'machine:test-host' }, scopes: ['host'] }) =>
      fleet.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions: [data] }) }), auth);
    expect((await report(row, {})).status).toBe(401);
    expect((await report({ ...row, progress: { ...row.progress, argv: 'untrusted' } })).status).toBe(400);
    expect((await report({ ...row, progress: { ...row.progress, agent_alive: 'false' } })).status).toBe(400);
    expect((await report(row)).status).toBe(200);
    expect(JSON.parse(native(sql).progress_json)).toEqual(row.progress);
    expect((await view(s)).state).toBe('running');
  }));
  it('flags idle headless and interactive sessions after the exact configurable threshold without releasing lifecycle/capacity', async () => fixture(async (s, live, sql, clock) => {
    expect((await view(s)).state).toBe('running');
    clock.now += 1;
    expect(await view(s)).toMatchObject({ state: 'stalled', stalled: true, lifecycle_state: 'running', stall_reason: 'no_native_progress', stall_threshold: 1800 });
    expect((await view(s, '')).sessions[0].state).toBe('stalled');
    sql.exec("UPDATE session_control_sessions SET mode = 'interactive'");
    expect((await view(s)).state).toBe('stalled');
    expect(sql.exec('SELECT state FROM session_control_sessions').toArray()[0].state).toBe('running');
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toEqual([]);
    expect(live.evaluate().subjects.find(x => x.kind === 'session')?.state).toBe('stalled');
    expect(stallThreshold({ HUB_AGENT_STALL_S: '90' })).toBe(90);
    for (const value of ['NaN', '0', '-1', '604801']) expect(stallThreshold({ HUB_AGENT_STALL_S: value })).toBe(1800);
  }));

  it('opens one existing-channel alert per stall and requires sustained recovery before a new alert', async () => fixture(async (s, live, sql, clock) => {
    clock.now += 1;
    let subjects = live.evaluate().subjects;
    expect(live.reconcileAlerts(subjects)).toHaveLength(1);
    expect(live.reconcileAlerts(subjects)).toHaveLength(0);
    sql.exec('UPDATE fleet_sessions SET last_activity_at = ?', new Date(clock.now).toISOString());
    subjects = live.evaluate().subjects;
    expect(subjects.find(x => x.kind === 'session')?.state).toBe('stalled');
    expect(live.reconcileAlerts(subjects)).toHaveLength(0);
    clock.now += 60000;
    sql.exec('UPDATE fleet_sessions SET last_activity_at = ?', new Date(clock.now).toISOString());
    subjects = live.evaluate().subjects;
    expect(subjects.find(x => x.kind === 'session')?.state).toBe('idle');
    live.reconcileAlerts(subjects);
    expect(live.alerts()[0].resolved_at).not.toBeNull();
    clock.now += 1800001;
    sql.exec('UPDATE machines SET last_checkin_at = ?', new Date(clock.now).toISOString());
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(1);
  }));

  it('reports gone headless agents immediately even with recently active orphan children and uncertain native phase', async () => fixture(async (s, live, sql) => {
    sql.exec("UPDATE fleet_sessions SET phase = 'uncertain', progress_json = ?", JSON.stringify({ headless: true, agent_alive: false, orphaned: true, cleanup: 'uncertain', last_progress_at: new Date(start).toISOString() }));
    expect(await view(s)).toMatchObject({ state: 'stalled', stall_reason: 'agent_exited_children_running' });
    expect(live.evaluate().subjects.find(x => x.kind === 'session')?.state).toBe('stalled');
    expect(sql.exec('SELECT state FROM session_control_sessions').toArray()[0].state).toBe('running');
    // Successful reaping retains a visible failure and one alert even when
    // it completes between scheduled liveness checks.
    sql.exec("UPDATE fleet_sessions SET phase = 'exited', progress_json = ?", JSON.stringify({ headless: true, agent_alive: false, orphaned: true, cleanup: 'reaped', last_progress_at: new Date(start).toISOString() }));
    sql.exec("UPDATE session_control_sessions SET state = 'stopped'");
    expect(await view(s)).toMatchObject({ state: 'failed', lifecycle_state: 'stopped' });
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(1);
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(0);
  }));

  it('uses latest CPU/file/output progress, ignores stale observations and resets on new incarnation or proven exit', async () => fixture(async (s, live, sql, clock) => {
    sql.exec('UPDATE fleet_sessions SET progress_json = ?', JSON.stringify({ last_progress_at: new Date(start).toISOString() }));
    clock.now += 1;
    expect((await view(s)).state).toBe('running');
    sql.exec('UPDATE fleet_sessions SET progress_json = NULL');
    expect((await view(s)).state).toBe('stalled');
    expect(classifySessionStall(sql, native(sql), clock.now, 1800, false).stalled).toBe(true);
    sql.exec('UPDATE fleet_sessions SET created_at_ms = ?, last_activity_at = ?', clock.now, new Date(clock.now).toISOString());
    // Matching UUID alone cannot transfer new-incarnation activity to the
    // original Hub binding. Classifier itself resets for the new native.
    expect(classifySessionStall(sql, native(sql), clock.now).stalled).toBe(false);
    sql.exec('UPDATE session_control_sessions SET binding_json = ?', JSON.stringify({ native_id: id, tag: 'test', workspace: '/test', native_created_at_ms: clock.now }));
    expect((await view(s)).state).toBe('running');
    sql.exec("UPDATE fleet_sessions SET phase = 'exited'");
    expect(classifySessionStall(sql, native(sql), clock.now).stalled).toBe(false);
    sql.exec("UPDATE fleet_sessions SET phase = 'running', last_activity_at = ?", new Date(start - 2000000).toISOString());
    sql.exec('UPDATE machines SET last_checkin_at = ?', new Date(start - 900000).toISOString());
    sql.exec('UPDATE fleet_sessions SET observed_at = ?', new Date(start - 900000).toISOString());
    expect((await view(s)).stalled).toBe(false);
  }));

  it('stalls missing/unobserved bindings on a fresh catalog without fabricating exit', async () => fixture(async (s, live, sql, clock) => {
    clock.now += 1;
    sql.exec("UPDATE fleet_sessions SET phase = 'unobserved'");
    expect((await view(s)).state).toBe('stalled');
    expect(live.evaluate().subjects.find(x => x.kind === 'session')?.state).toBe('stalled');
    sql.exec('DELETE FROM fleet_sessions');
    expect((await view(s)).state).toBe('stalled');
    expect(sql.exec('SELECT state FROM session_control_sessions').toArray()[0].state).toBe('running');
  }));

  it('reconciles the three-hour warning with mismatched native phases and preserves its snooze independently of stall alerts', async () => fixture(async (s, live, sql) => {
    sql.exec("UPDATE fleet_sessions SET phase = 'uncertain'");
    expect(await view(s)).toMatchObject({ long_running: true, warning_active: true, runtime: 11000 });
    expect((await view(s, '/warnings')).warnings).toHaveLength(1);
    sql.exec('UPDATE fleet_sessions SET phase = ?', 'running');
    sql.exec('UPDATE fleet_sessions SET last_activity_at = ?', new Date(start - 2000000).toISOString());
    await s.handle(new Request('https://hub.test/api/sessions/' + id + '/ack', { method: 'POST', body: '{}' }), admin);
    expect(await view(s)).toMatchObject({ state: 'stalled', long_running: true, warning_active: false });
    expect(live.reconcileAlerts(live.evaluate().subjects)).toHaveLength(1);
  }));
});
