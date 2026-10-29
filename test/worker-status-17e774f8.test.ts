import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { IntakeService } from '../src/intake';
import { SessionControlService } from '../src/session_control';
import { cleanReportedExit } from '../src/worker-outcome';
import sessionsJs from '../public/sessions.js?raw';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 17e774f8: a headless session that finished cleanly with a worker
// report surfaces that outcome (worker_status) while lifecycle stays stopped.
// Failed, crashed, contradictory or report-less launches never claim one.

const host = { caller: { machineId: 'tabo', kind: 'host', actor: 'machine:tabo' }, scopes: ['host'] } as any;
const admin = { caller: { isAdmin: true, kind: 'human', actor: 'tester' }, scopes: ['admin'] } as any;
const now = Date.now();
const iso = () => new Date(now).toISOString();
const uuid = () => crypto.randomUUID();
const NATIVE = 'cc4714fa-330b-4e22-9899-d5116a937d12';

async function setup(fn: (fleet: FleetService, sessions: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('worker-status-' + uuid()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const tx = <T>(cb: () => T) => state.storage.transactionSync(cb);
    const sessions = new SessionControlService(hub.sql, tx, () => now); sessions.init();
    const fleet = new FleetService(hub.sql, tx, () => now); fleet.init();
    new IntakeService(hub.sql, tx).init();
    await fn(fleet, sessions, hub.sql);
  });
}

/** Unbound: the receipt-less retired dispatch (no running receipt ever
 * bound the session). Bound: an ordinary running session. */
function seed(sql: any, bound = false) {
  const launchId = uuid(), sessionId = uuid();
  sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json,
    machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'tester', 'agent', NULL, 's', ?, '{}', 'tabo', 'claude', 'claude-max', NULL,
    'headless', 'demo', 'prompt', ?, ?, ?, ?, ?)`, launchId, 'key-' + launchId,
    bound ? 'running' : 'starting', bound ? NATIVE : null, bound ? 'hub-' + launchId : null, iso(), iso());
  sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id,
    machine_id, engine, mode, state, binding_json, created_at, updated_at)
    VALUES (?, 'tester', ?, '{}', ?, 'tabo', 'claude', 'headless', 'running', ?, ?, ?)`,
    sessionId, 'sk-' + sessionId, launchId,
    bound ? JSON.stringify({ native_id: NATIVE, tag: 'task-hub-' + launchId, workspace: 'C:\\repos\\agent-hub', native_created_at_ms: now - 120000 }) : null,
    iso(), iso());
  return { launchId, sessionId };
}

const receipt = (launchId: string, extra: any) => ({
  state: 'exited', confirmed_exit: true, native_id: NATIVE, launcher_task_id: 'hub-' + launchId, ...extra,
});
const clean = { exit_code: 0, exit_signal: null, wall_time_ms: 110000, log_tail: '', engine_outcome: null,
  observed_commits: [], dirty_paths: [], outcome: 'reported' };
const done = (runtime: any = clean) => ({ report_status: 'done', report: 'Posted the note.', runtime });

async function post(fleet: FleetService, launchId: string, body: any) {
  return fleet.handle(new Request('https://hub.test/api/fleet/launches/' + launchId + '/result',
    { method: 'POST', body: JSON.stringify(body) }), host);
}
async function show(sessions: SessionControlService, id: string) {
  return (await sessions.handle(new Request('https://hub.test/api/sessions/' + id), admin)).json() as Promise<any>;
}
const stored = (sql: any, launchId: string) => sql.exec('SELECT worker_status FROM fleet_launches WHERE id=?', launchId).one().worker_status;

describe('session worker outcome surface (intake 17e774f8)', () => {
  for (const bound of [false, true]) {
    it(`clean exit with a done report shows done, lifecycle stopped (${bound ? 'bound' : 'unbound receipt-less'})`, () => setup(async (fleet, sessions, sql) => {
      const { launchId, sessionId } = seed(sql, bound);
      const res = await post(fleet, launchId, receipt(launchId, done()));
      expect(res.status, await res.clone().text()).toBe(200);
      expect(stored(sql, launchId)).toBe('done');
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?', sessionId).one().state).toBe('stopped');
      expect(await show(sessions, sessionId)).toMatchObject({ state: 'stopped', lifecycle_state: 'stopped', worker_status: 'done' });
      const list = await (await sessions.handle(new Request('https://hub.test/api/sessions'), admin)).json() as any;
      const row = list.sessions.find((s: any) => s.id === sessionId);
      expect(row).toBeDefined();
      expect(row).toMatchObject({ state: 'stopped', worker_status: 'done' });
    }));
  }

  it('carries other reported worker outcomes without claiming done', () => setup(async (fleet, sessions, sql) => {
    for (const status of ['failed', 'stuck', 'waiting-on-human']) {
      const { launchId, sessionId } = seed(sql);
      expect((await post(fleet, launchId, receipt(launchId, { ...done(), report_status: status }))).status).toBe(200);
      expect((await show(sessions, sessionId)).worker_status).toBe(status);
    }
  }));

  it('rejects an exited receipt whose runtime contradicts a clean completion', () => setup(async (fleet, sessions, sql) => {
    for (const runtime of [{ ...clean, exit_code: 137, exit_signal: 9 }, { ...clean, exit_signal: 9 },
                           { ...clean, exit_code: null }, { ...clean, engine_outcome: 'failed' },
                           { ...clean, engine_outcome: 'cancelled' }]) {
      const { launchId, sessionId } = seed(sql);
      expect((await post(fleet, launchId, receipt(launchId, done(runtime)))).status).toBe(200);
      expect(stored(sql, launchId)).toBeNull();
      expect((await show(sessions, sessionId)).worker_status).toBeUndefined();
    }
  }));

  it('never marks a failed or signal-killed launch, even with a done report', () => setup(async (fleet, sessions, sql) => {
    const { launchId, sessionId } = seed(sql);
    const killed = { ...clean, exit_code: 137, exit_signal: 9 };
    expect((await post(fleet, launchId, receipt(launchId, { ...done(killed), state: 'failed' }))).status).toBe(200);
    expect(stored(sql, launchId)).toBeNull();
    const view = await show(sessions, sessionId);
    expect(view.worker_status).toBeUndefined();
    expect(view.state).toBe('failed');
  }));

  it('never marks an exit without a real worker report', () => setup(async (fleet, sessions, sql) => {
    for (const body of [{}, { runtime: { ...clean, outcome: 'failed-without-report' } },
                        { report_status: 'done', report: 'partial', runtime: { ...clean, outcome: 'failed-with-partial-work' } },
                        { report_status: 'done', report: 'no runtime' }]) {
      const { launchId, sessionId } = seed(sql);
      expect((await post(fleet, launchId, receipt(launchId, body))).status).toBe(200);
      expect(stored(sql, launchId)).toBeNull();
      expect((await show(sessions, sessionId)).worker_status).toBeUndefined();
    }
  }));

  it('a late report replay sets the outcome once; later replays never rewrite it', () => setup(async (fleet, sessions, sql) => {
    const { launchId, sessionId } = seed(sql);
    expect((await post(fleet, launchId, receipt(launchId, {}))).status).toBe(200);
    expect(stored(sql, launchId)).toBeNull();
    const late = receipt(launchId, done());
    expect((await post(fleet, launchId, late)).status).toBe(200);
    expect(stored(sql, launchId)).toBe('done');
    expect((await post(fleet, launchId, { ...late, report_status: 'stuck' })).status).toBe(200);
    expect(stored(sql, launchId)).toBe('done');
    expect((await show(sessions, sessionId)).worker_status).toBe('done');
  }));

  it('a later contradictory runtime replay suppresses done in the view', () => setup(async (fleet, sessions, sql) => {
    const { launchId, sessionId } = seed(sql);
    expect((await post(fleet, launchId, receipt(launchId, done()))).status).toBe(200);
    expect((await show(sessions, sessionId)).worker_status).toBe('done');
    expect((await post(fleet, launchId, receipt(launchId, done({ ...clean, exit_code: 137, exit_signal: 9 })))).status).toBe(200);
    expect(stored(sql, launchId)).toBe('done');
    expect((await show(sessions, sessionId)).worker_status).toBeUndefined();
  }));

  it('older fleet tables without the column keep the session view working', () => setup(async (fleet, sessions, sql) => {
    const { launchId, sessionId } = seed(sql);
    expect((await post(fleet, launchId, receipt(launchId, done()))).status).toBe(200);
    sql.exec('ALTER TABLE fleet_launches DROP COLUMN worker_status');
    const view = await show(sessions, sessionId);
    expect(view).toMatchObject({ state: 'stopped', fleet_launch_id: launchId });
    expect(view.worker_status).toBeUndefined();
  }));
});

describe('clean reported exit predicate', () => {
  it('accepts only integer 0, no signal, no failed engine outcome, reported', () => {
    expect(cleanReportedExit(clean)).toBe(true);
    expect(cleanReportedExit({ ...clean, exit_signal: 0 })).toBe(true);
    for (const bad of [null, undefined, [], {}, { ...clean, exit_code: false }, { ...clean, exit_code: '0' },
                       { ...clean, exit_code: 0.5 }, { ...clean, exit_signal: false }, { ...clean, outcome: 'failed-without-report' }]) {
      expect(cleanReportedExit(bad)).toBe(false);
    }
  });
});

describe('dashboard badge (public/sessions.js)', () => {
  const source = (sessionsJs as string).match(/function sessionDisplayState\(s\) \{[\s\S]*?\n\}/)?.[0];
  const display = new Function(source + '\nreturn sessionDisplayState;')() as (s: any) => string;
  it('renders a cleanly stopped done session as done (success), every other stop unchanged', () => {
    expect(source).toBeDefined();
    expect(display({ state: 'stopped', worker_status: 'done' })).toBe('done');
    expect(display({ state: 'stopped' })).toBe('stopped');
    expect(display({ state: 'stopped', worker_status: 'stuck' })).toBe('stopped');
    expect(display({ state: 'failed', worker_status: 'done' })).toBe('failed');
    expect(display({ state: 'running', agent_state: 'idle' })).toBe('idle');
    expect(sessionsJs).toMatch(/\['running', 'active', 'working', 'acked', 'delivered', 'done'\]\.includes\(s\)\) color = 'dk-badge--success'/);
    expect(sessionsJs).toMatch(/session\.state === 'stopped' && session\.worker_status === 'done' \? 'done'/);
  });
});
