import { freshCpu } from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { stopHostOnline } from '../src/session_stop';
import { FIXTURE_INTAKE_ID, seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const START = Date.now();
const NATIVE_ID = '9bb2ae7c-82e3-42e2-a18f-6cb2d00ee188';
const PREDECESSOR_NATIVE = '48fe19dd-439b-41fa-82a6-2285cc7c8c4a';
const NATIVE_CREATED = 1793332119480;
const WORKSPACE = '/srv/hub/wt-cbcadaa1';

function fleetReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
function sessReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}
const registry = () => ({
  accounts: [{ label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
  routes: [{ machine_id: 'stale-1c8d', engine: 'codex', account: 'shared-codex', profile: null }],
});
const host = { caller: { machineId: 'stale-1c8d', kind: 'host', actor: 'machine:stale-1c8d', tokenId: 'host-stale-1c8d' }, scopes: ['host'] };

type Ctx = { f: FleetService; s: SessionControlService; sql: any; clock: { now: number } };

async function fixture(fn: (ctx: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('offered-stale-' + crypto.randomUUID()));
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
      VALUES ('host-stale-1c8d', 'host', 'hash-stale-1c8d', 'refresh-stale-1c8d', 'family-stale-1c8d', 'test-host', 'stale-1c8d', '["host"]', 'test', ?, ?)`, iso(), iso(86400000));
    sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'stale-1c8d', 'stale-1c8d', 'stale-1c8d', 'host-stale-1c8d', iso(), iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'stale-1c8d', '1.0', iso(), clock.now, iso(), clock.now, 'ok', null,
      JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    seedFixtureIntake(sql);
    expect((await f.handle(fleetReq('/accounts', registry(), 'PUT'), admin)).status).toBe(200);
    await fn({ f, s, sql, clock });
  });
}


async function startClaimed(ctx: Ctx, key: string, receipt: boolean) {
  const { f, s, sql, clock } = ctx;
  const res = await s.handle(sessReq('/api/sessions', { key, workspace: 'demo', machine: 'stale-1c8d', mode: 'headless', brief: 'silent start probe' }), admin);
  const created: any = await res.json();
  expect(res.status, JSON.stringify(created)).toBe(201);
  const launchId = created.fleet_launch_id as string;
  const claim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: new Date(clock.now).toISOString(), account: 'shared-codex', profile_supported: true };
  expect((await f.handle(fleetReq('/launches/' + launchId + '/claim', claim), host)).status).toBe(200);
  if (receipt) expect((await f.handle(fleetReq('/launches/' + launchId + '/result', { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + launchId }), host)).status).toBe(200);
  return { id: created.id as string, launchId, sql };
}
const launchRow = (sql: any, id: string) => sql.exec('SELECT state, exit_reason FROM fleet_launches WHERE id = ?', id).one();
const sessionState = (sql: any, id: string) => sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id).one().state;

describe('routing and silent workers (intake 6c2b2aa2)', () => {
  it('AC1: auto routing never places on an account_blocked engine, even after the retry cooldown', () => fixture(async ({ f, sql, clock }) => {
    sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('codex', '', 'unavailable', 'account_blocked', ?)", new Date(clock.now).toISOString());
    for (const advance of [0, 3600000]) {
      clock.now = START + advance;
      sql.exec("UPDATE fleet_engine_health SET updated_at = ?", new Date(START).toISOString());
      const plan: any = f.plan({ workspace: 'demo', mode: 'headless', prompt: 'auto routed' });
      expect(plan.selected).toBeNull();
      expect(plan.rejected).toContainEqual(expect.objectContaining({ engine: 'codex', reason: 'account_blocked' }));
    }
  }));

  it('AC2: a claimed headless launch with no output is failed with a slug, its session follows, and the engine is held', () => fixture(async ctx => {
    const { f, sql, clock } = ctx;
    const started = await startClaimed(ctx, 'silent-claimed', false);
    clock.now = START + 4 * 60000;
    f.reconcileCapacity();
    expect(launchRow(sql, started.launchId).state).toBe('starting');
    f.withMaintenance(() => {}); // fresh maintenance scope on the next call
    clock.now = START + 6 * 60000;
    f.reconcileCapacity();
    expect(launchRow(sql, started.launchId)).toMatchObject({ state: 'failed', exit_reason: 'worker_silent_start' });
    expect(sessionState(sql, started.id)).toBe('failed');
    const plan: any = f.plan({ workspace: 'demo', mode: 'headless', prompt: 'next' });
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ engine: 'codex', reason: 'worker_silent_start' }));
  }));

  it('AC2: a running receipt whose session never binds is failed as well', () => fixture(async ctx => {
    const { f, sql, clock } = ctx;
    const started = await startClaimed(ctx, 'silent-running', true);
    clock.now = START + 6 * 60000;
    f.reconcileCapacity();
    expect(launchRow(sql, started.launchId)).toMatchObject({ state: 'failed', exit_reason: 'worker_silent_start' });
    expect(sessionState(sql, started.id)).toBe('failed');
  }));

  it('AC3: a heartbeating host whose persisted check-in is minutes old is not unreachable', () => {
    const now = Date.now();
    const m = (ago: number) => ({ id: 'silent-6c2b-host', drained: 0, last_checkin_at: new Date(now - ago).toISOString() });
    expect(stopHostOnline(m(290000), now)).toBe(true);
    expect(stopHostOnline(m(400000), now)).toBe(true);
    expect(stopHostOnline(m(900000), now)).toBe(false);
    expect(stopHostOnline(m(900000), now, 'silent-6c2b-host')).toBe(true);
  });
});
