import { freshCpu } from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { FIXTURE_INTAKE_ID, seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 0b87eb29: claimed launches (launcher_task_id set) wedged in
// starting/offered forever. Covers (a) claimed-but-never-spawned and (b)
// completed-on-host-but-finish-lost, plus operator stop after the bound.

const admin = { caller: { isAdmin: true, actor: 'human:admin-token:fixture', kind: 'human' }, scopes: ['admin'] };
const START = Date.now();
const MIN = 60000;
const NATIVE_ID = '5d3a6e1c-0b87-4b29-8f65-e85b16b03000';
const MACHINE = 'wedge-0b87';
const host = { caller: { machineId: MACHINE, kind: 'host', actor: 'machine:' + MACHINE, tokenId: 'host-' + MACHINE }, scopes: ['host'] };
const foreign = { caller: { machineId: 'foreign-0b87', kind: 'host', actor: 'machine:foreign-0b87' }, scopes: ['host'] };

const fleetReq = (path: string, data?: any, method = 'POST') =>
  new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const sessReq = (path: string, data?: any, method = 'POST') =>
  new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });

type Ctx = { f: FleetService; s: SessionControlService; sql: any; clock: { now: number } };

async function fixture(fn: (ctx: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('wedged-claimed-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: START };
    const iso = (delta = 0) => new Date(clock.now + delta).toISOString();
    const tx = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const f = new FleetService(hub.sql, tx, () => clock.now);
    f.init();
    const s = new SessionControlService(hub.sql, tx, () => clock.now, { reserve: (input, auth) => f.reserve(input, auth) });
    s.init();
    const sql = hub.sql;
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at)
      VALUES (?, 'host', ?, ?, ?, 'test-host', ?, '["host"]', 'test', ?, ?)`, 'host-' + MACHINE, 'hash-' + MACHINE, 'refresh-' + MACHINE, 'family-' + MACHINE, MACHINE, iso(), iso(86400000));
    sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      MACHINE, MACHINE, MACHINE, 'host-' + MACHINE, iso(), iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      MACHINE, '1.0', iso(), clock.now, iso(), clock.now, 'ok', null,
      JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    seedFixtureIntake(sql);
    expect((await f.handle(fleetReq('/accounts', {
      accounts: [{ label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
      routes: [{ machine_id: MACHINE, engine: 'codex', account: 'shared-codex', profile: null }],
    }, 'PUT'), admin)).status).toBe(200);
    await fn({ f, s, sql, clock });
  });
}

/** Session -> placement -> host claim with a launcher task id: the exact
 * state of 9f65e85b / 15837247 / 16b0300e before they wedged. */
async function claimed(ctx: Ctx, key: string) {
  const { f, s, sql, clock } = ctx;
  const res = await s.handle(sessReq('/api/sessions', { key, workspace: 'demo', machine: MACHINE, mode: 'headless', brief: 'wedge probe' }), admin);
  const created: any = await res.json();
  expect(res.status, JSON.stringify(created)).toBe(201);
  const launchId = created.fleet_launch_id as string;
  const claim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: new Date(clock.now).toISOString(), account: 'shared-codex', profile_supported: true };
  expect((await f.handle(fleetReq('/launches/' + launchId + '/claim', claim), host)).status).toBe(200);
  sql.exec('UPDATE fleet_launches SET launcher_task_id = ? WHERE id = ?', 'hub-' + launchId, launchId);
  expect(sessionState(sql, created.id)).toBe('offered');
  return { id: created.id as string, launchId };
}
const launch = (sql: any, id: string) => sql.exec('SELECT state, exit_reason, verification_status, native_id, finished_at FROM fleet_launches WHERE id = ?', id).one();
const sessionState = (sql: any, id: string) => sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id).one().state;
const stop = async (ctx: Ctx, id: string, key: string) => {
  const res = await ctx.s.handle(sessReq(`/api/sessions/${id}/stop`, { key }), admin);
  return { status: res.status, body: await res.json() as any };
};
const result = async (ctx: Ctx, launchId: string, data: any, auth: any = host) => {
  const res = await ctx.f.handle(fleetReq(`/launches/${launchId}/result`, data), auth);
  return { status: res.status, body: await res.json() as any };
};
const abandon = (ctx: Ctx) => { ctx.clock.now = START + 16 * MIN; ctx.f.withMaintenance(() => {}); ctx.f.reconcileCapacity(); };

describe('wedged claimed launches (intake 0b87eb29)', () => {
  it('AC3: stop on a claimed-but-unspawned launch refuses before the bound and cancels after it, without a maintenance pass', () => fixture(async ctx => {
    const { sql, clock } = ctx;
    const w = await claimed(ctx, 'never-spawned');
    clock.now = START + 10 * MIN;
    const early = await stop(ctx, w.id, 'early');
    expect(early.status).toBe(409);
    expect(early.body.error).toBe('session_not_cancellable');
    expect(launch(sql, w.launchId).state).toBe('starting');

    clock.now = START + 16 * MIN; // no reconcileCapacity: stop applies the bound itself
    const stopped = await stop(ctx, w.id, 'after-bound');
    expect(stopped.status, JSON.stringify(stopped.body)).toBe(200);
    expect(stopped.body.state).toBe('cancelled');
    expect(stopped.body.cancellation).toMatchObject({ effect: 'no_native_start', launch: 'abandoned_unverified' });
    expect(launch(sql, w.launchId)).toMatchObject({ state: 'failed', exit_reason: 'worker_silent_start', verification_status: 'unverified', native_id: null });
    const replay = await stop(ctx, w.id, 'after-bound');
    expect(replay.status).toBe(200);
    expect(replay.body).toMatchObject({ replay: true, cancellation: { launch: 'abandoned_unverified' } });
    expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(0);
  }));

  it('AC2: an abandoned claim stays a reconciliation candidate for its owning host only, for a bounded window', () => fixture(async ctx => {
    const { f, clock } = ctx;
    const w = await claimed(ctx, 'candidate');
    abandon(ctx);
    const ids = async (auth: any) => ((await (await f.handle(fleetReq('/reconciliation', undefined, 'GET'), auth)).json()) as any).launches.map((r: any) => r.id);
    expect(await ids(host)).toContain(w.launchId);
    expect(await ids(foreign)).not.toContain(w.launchId);
    clock.now = START + 16 * MIN + 25 * 3600000;
    expect(await ids(host)).not.toContain(w.launchId);
  }));

  it('AC2a: the host fence proving no start upgrades the abandonment to not_started and stop then cancels', () => fixture(async ctx => {
    const { sql } = ctx;
    const w = await claimed(ctx, 'fenced');
    abandon(ctx);
    expect(sessionState(sql, w.id)).toBe('failed');
    const fenced = await result(ctx, w.launchId, { state: 'failed', confirmed_exit: true, launcher_task_id: 'hub-' + w.launchId, reason: 'launcher_not_started' });
    expect(fenced.status, JSON.stringify(fenced.body)).toBe(200);
    expect(launch(sql, w.launchId)).toMatchObject({ state: 'failed', exit_reason: 'launcher_not_started', verification_status: 'not_started' });
    expect(launch(sql, w.launchId).finished_at).toBeTruthy();
    // Lost acknowledgement: the same receipt replays without effects.
    expect((await result(ctx, w.launchId, { state: 'failed', confirmed_exit: true, launcher_task_id: 'hub-' + w.launchId, reason: 'launcher_not_started' })).body.replay).toBe(true);
    const stopped = await stop(ctx, w.id, 'fenced-stop');
    expect(stopped.status).toBe(200);
    expect(stopped.body.cancellation.effect).toBe('no_native_start');
    expect(stopped.body.cancellation.launch).toBeUndefined();
  }));

  it('AC2b: a completed run whose finish was lost is recorded as finish with its hub-results report', () => fixture(async ctx => {
    const { sql } = ctx;
    const w = await claimed(ctx, 'completed');
    expect((await result(ctx, w.launchId, { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId })).status).toBe(200);
    abandon(ctx);
    expect(launch(sql, w.launchId).state).toBe('failed');
    expect(sql.exec("SELECT reason FROM fleet_engine_health WHERE engine='codex' AND machine_id=?", MACHINE).toArray()).toHaveLength(1);
    const finish = { state: 'exited', confirmed_exit: true, native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId,
      report_status: 'done', report: 'Recovered 16b0300e completion.', artifacts: [] };
    const out = await result(ctx, w.launchId, finish);
    expect(out.status, JSON.stringify(out.body)).toBe(200);
    expect(launch(sql, w.launchId)).toMatchObject({ state: 'exited', exit_reason: null, verification_status: null, native_id: NATIVE_ID });
    expect(sessionState(sql, w.id)).toBe('stopped');
    const history = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).one().data).history;
    expect(JSON.stringify(history)).toContain('Recovered 16b0300e completion.');
    // The runtime was not silent: its own silent-start hold is lifted.
    expect(sql.exec("SELECT reason FROM fleet_engine_health WHERE engine='codex' AND machine_id=?", MACHINE).toArray()).toHaveLength(0);
    expect((await result(ctx, w.launchId, finish)).body.replay).toBe(true);
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).one().data).history).toHaveLength(history.length);
  }));

  it('AC3: a real late native start is never stranded behind an operator cancellation', () => fixture(async ctx => {
    const { sql } = ctx;
    const w = await claimed(ctx, 'late-start');
    abandon(ctx);
    expect((await stop(ctx, w.id, 'cancel-abandoned')).status).toBe(200);
    const late = await result(ctx, w.launchId, { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId });
    expect(late.status, JSON.stringify(late.body)).toBe(200);
    expect(launch(sql, w.launchId)).toMatchObject({ state: 'running', native_id: NATIVE_ID, exit_reason: null, finished_at: null });
    expect(sessionState(sql, w.id)).toBe('cancelled');
    expect(sql.exec("SELECT state FROM liveness_alerts WHERE subject_kind='launch' AND subject=? AND resolved_at IS NULL", w.launchId).one().state).toBe('late_native_start');
    // Its eventual exit still reaches Hub through the ordinary receipt.
    expect((await result(ctx, w.launchId, { state: 'exited', confirmed_exit: true, native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId })).status).toBe(200);
    expect(launch(sql, w.launchId).state).toBe('exited');
  }));

  it('AC3: a late native start without cancellation revives the session for host stop', () => fixture(async ctx => {
    const { sql } = ctx;
    const w = await claimed(ctx, 'late-revive');
    abandon(ctx);
    expect((await result(ctx, w.launchId, { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId })).status).toBe(200);
    expect(sessionState(sql, w.id)).toBe('starting');
    expect(launch(sql, w.launchId).state).toBe('running');
  }));

  it('refuses mismatched identities, foreign hosts and non-abandoned terminal rows', () => fixture(async ctx => {
    const { sql } = ctx;
    const w = await claimed(ctx, 'identity');
    expect((await result(ctx, w.launchId, { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId })).status).toBe(200);
    abandon(ctx);
    const other = '00000000-0000-4000-8000-000000000000';
    expect((await result(ctx, w.launchId, { state: 'exited', confirmed_exit: true, native_id: other, launcher_task_id: 'hub-' + w.launchId })).status).toBe(409);
    expect((await result(ctx, w.launchId, { state: 'exited', confirmed_exit: true, native_id: NATIVE_ID, launcher_task_id: 'hub-foreign' })).status).toBe(409);
    expect((await result(ctx, w.launchId, { state: 'exited', confirmed_exit: true, native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId }, foreign)).status).toBe(404);
    expect(launch(sql, w.launchId).state).toBe('failed');
    // A host-proven failure is final; only the synthetic abandonment is revisable.
    sql.exec("UPDATE fleet_launches SET verification_status='not_started', exit_reason='launcher_not_started' WHERE id=?", w.launchId);
    expect((await result(ctx, w.launchId, { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + w.launchId })).status).toBe(409);
  }));
});
