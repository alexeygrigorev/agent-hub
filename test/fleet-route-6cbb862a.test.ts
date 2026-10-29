// Intake 6cbb862a: Hub offered Tabo a shared-codex route its host no longer
// advertised, and every start failed dispatch_route_unconfigured on the host
// with no hint of which account was offered.
import { freshCpu } from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, advertisedRoutes, hostAdvertisesRoute, isCapacityOnlyHold } from '../src/fleet';
import { MAX_ADVERTISED_ROUTES, updateHeartbeat } from '../src/registry';
import { SessionControlService } from '../src/session_control';
import { seedFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const BASE_CAPS = { engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 };

async function fleet(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-route-6cbb862a-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init(); const sql = hub.sql;
    sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      'tabo', 'tabo', 'tabo', iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify(BASE_CAPS));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'tabo', '1.0', iso(), now, iso(), now, 'ok', null, JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    const registry = { accounts: [
      { label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true },
      { label: 'codex-orin', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true },
    ], routes: [
      { machine_id: 'tabo', engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' },
      { machine_id: 'tabo', engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' },
    ] };
    const put = await service.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(registry) }), admin);
    expect(put.status).toBe(200);
    await fn(service, sql);
  });
}
const HB = { mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso() } };
// Latest heartbeat as the registry stores it: fleet_routes plus its own freshness.
const advertise = (sql: any, routes: any[] | undefined, at = iso()) =>
  sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'tabo'", JSON.stringify({ ...HB, cpu_usage: freshCpu(iso()),
    ...(routes === undefined ? {} : { fleet_routes: routes, fleet_routes_at: at }) }));
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'private test goal', machine: 'tabo', engine: 'codex', idempotency_key: crypto.randomUUID(), ...extra });

describe('placement offers only host-advertised routes (6cbb862a AC a)', () => {
  it('never offers a route missing from the latest host capabilities', async () => fleet(async (s, sql) => {
    advertise(sql, [{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' }]);
    for (let i = 0; i < 4; i++) {
      const plan: any = s.plan(intent());
      expect(plan.selected?.account).toBe('codex-orin');
      expect(plan.candidates.map((c: any) => c.account)).toEqual(['codex-orin']);
      expect(plan.rejected).toContainEqual({ machine: 'tabo', engine: 'codex', reason: 'route_not_advertised', account: 'shared-codex', profile: 'hub-shared-codex' });
    }
  }));

  it('holds with rejected[] naming the missing route when the pinned account is not advertised', async () => fleet(async (s, sql) => {
    advertise(sql, [{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' }]);
    const plan: any = s.plan(intent({ account: 'shared-codex' }));
    expect(plan.selected).toBeFalsy();
    expect(plan.rejected).toEqual([{ machine: 'tabo', engine: 'codex', reason: 'route_not_advertised', account: 'shared-codex', profile: 'hub-shared-codex' }]);
    expect(isCapacityOnlyHold(plan.rejected)).toBe(true);
    // The host re-advertises the route after its managed sync recovers.
    advertise(sql, [{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' }, { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' }]);
    expect((s.plan(intent({ account: 'shared-codex' })) as any).selected?.account).toBe('shared-codex');
  }));

  it('holds every route when the latest advertisement is stale, never trusting old capabilities', async () => fleet(async (s, sql) => {
    advertise(sql, [{ engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' }], iso(-181000));
    const plan: any = s.plan(intent({ account: 'shared-codex' }));
    expect(plan.selected).toBeFalsy();
    expect(plan.rejected).toEqual([{ machine: 'tabo', engine: 'codex', reason: 'route_not_advertised', account: 'shared-codex', profile: 'hub-shared-codex' }]);
  }));

  it('judges pre-release hosts only by fresh exact runner tuples, holding unknown routes', async () => fleet(async (s, sql) => {
    advertise(sql, undefined);
    // A host with no route evidence at all keeps registry-only placement.
    expect((s.plan(intent({ account: 'shared-codex' })) as any).selected?.account).toBe('shared-codex');
    sql.exec('INSERT OR REPLACE INTO routing_machines VALUES (?, ?, ?, ?, ?)', 'tabo', '[]', 6, iso(),
      JSON.stringify([{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin', model: 'm', effort: 'high' }]));
    expect((s.plan(intent()) as any).selected?.account).toBe('codex-orin');
    expect((s.plan(intent({ account: 'shared-codex' })) as any).rejected[0].reason).toBe('route_not_advertised');
    sql.exec("UPDATE routing_machines SET observed_at = ? WHERE machine_id = 'tabo'", iso(-181000));
    expect((s.plan(intent({ account: 'codex-orin' })) as any).rejected[0].reason).toBe('route_not_advertised');
  }));

  it('matches engine, canonical account and profile exactly', () => {
    const route = { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' };
    expect(hostAdvertisesRoute(undefined, route)).toBe(false);
    expect(hostAdvertisesRoute([], route)).toBe(false);
    expect(hostAdvertisesRoute([{ ...route, profile: null }], route)).toBe(false);
    expect(hostAdvertisesRoute([{ ...route, engine: 'zcodex' }], route)).toBe(false);
    expect(hostAdvertisesRoute([null, 'x', route], route)).toBe(true);
    expect(hostAdvertisesRoute([{ engine: 'codex', account: 'x', profile: null }], { engine: 'codex', account: 'x' })).toBe(true);
    expect(advertisedRoutes({ fleet_routes: [route] }, null, now)).toEqual([]); // no freshness proof
    expect(advertisedRoutes({ fleet_routes: [route], fleet_routes_at: iso() }, null, now)).toEqual([route]);
    expect(advertisedRoutes({}, { runners_json: 'not json', observed_at: iso() }, now)).toEqual([]);
    expect(advertisedRoutes({}, null, now)).toBeNull();
  });
});

describe('heartbeat route advertisement storage (6cbb862a AC a)', () => {
  it('sanitizes fleet_routes, stamps freshness, and keeps the original time across heartbeats without it', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hb-route-6cbb862a-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      const id = 'm-' + crypto.randomUUID().slice(0, 8);
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', id, id, id, iso());
      const stored = () => JSON.parse(String((hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', id).toArray()[0] as any).last_heartbeat));
      const route = { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' };
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes: [route, { engine: 'codex', account: 'codex-orin' }] }, hub.sql);
      const first = stored();
      expect(first.fleet_routes).toEqual([route, { engine: 'codex', account: 'codex-orin', profile: null }]);
      expect(typeof first.fleet_routes_at).toBe('string');
      updateHeartbeat(id, { type: 'heartbeat', mem_avail_mb: 1 }, hub.sql);
      expect(stored().fleet_routes_at).toBe(first.fleet_routes_at);
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes: [{ engine: 'codex', account: 'bad account', profile: null }] }, hub.sql);
      expect(stored().fleet_routes_at).toBe(first.fleet_routes_at); // malformed list dropped, never partially kept
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes: [{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' }] }, hub.sql);
      expect(stored().fleet_routes).toEqual([{ engine: 'codex', account: 'codex-orin', profile: 'hub-codex-orin' }]);
    });
  });
});

// Review 89766b23: the host config supports 128 routes, so an advertisement is
// never truncated; past the cap the host sends an explicit overflow count.
const fillers = (n: number) => Array.from({ length: n }, (_, i) => ({ engine: 'codex', account: `filler-${i}`, profile: `hub-filler-${i}` }));
const SHARED = { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' };

describe('route advertisement size bound (6cbb862a review)', () => {
  it('stores 64, 65 and 128 advertised routes completely and drops an oversize list', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hb-route-size-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      const id = 'm-' + crypto.randomUUID().slice(0, 8);
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', id, id, id, iso());
      const stored = () => JSON.parse(String((hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', id).toArray()[0] as any).last_heartbeat));
      expect(MAX_ADVERTISED_ROUTES).toBe(128);
      for (const n of [64, 65, 128]) {
        const routes = [...fillers(n - 1), SHARED];
        updateHeartbeat(id, { type: 'heartbeat', fleet_routes: routes }, hub.sql);
        expect(stored().fleet_routes).toEqual(routes);
      }
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes: [...fillers(128), SHARED] }, hub.sql);
      expect(stored().fleet_routes).toHaveLength(128); // oversize list dropped whole, never truncated
      expect(stored().fleet_routes_overflow).toBeUndefined();
    });
  });

  it('clears the advertisement on an explicit overflow instead of holding valid routes', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hb-route-overflow-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      const id = 'm-' + crypto.randomUUID().slice(0, 8);
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', id, id, id, iso());
      const stored = () => JSON.parse(String((hub.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', id).toArray()[0] as any).last_heartbeat));
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes: [SHARED] }, hub.sql);
      updateHeartbeat(id, { type: 'heartbeat', fleet_routes_overflow: 129 }, hub.sql);
      const hb = stored();
      expect(hb.fleet_routes_overflow).toBe(129);
      expect(hb.fleet_routes).toBeUndefined();
      expect(hb.fleet_routes_at).toBeUndefined();
      expect(advertisedRoutes(hb, null, now)).toBeNull(); // no route evidence: registry placement
    });
  });

  for (const n of [64, 65, 128]) {
    it(`places the last of ${n} advertised routes`, async () => fleet(async (s, sql) => {
      advertise(sql, [...fillers(n - 1), SHARED]);
      expect((s.plan(intent({ account: 'shared-codex' })) as any).selected?.account).toBe('shared-codex');
    }));
  }

  it('places a host that reports overflow by registry routes, not route_not_advertised', async () => fleet(async (s, sql) => {
    sql.exec("UPDATE machines SET last_heartbeat = ? WHERE id = 'tabo'", JSON.stringify({ ...HB, cpu_usage: freshCpu(iso()), fleet_routes_overflow: 129 }));
    const plan: any = s.plan(intent({ account: 'shared-codex' }));
    expect(plan.selected?.account).toBe('shared-codex');
    expect(plan.rejected.filter((r: any) => r.reason === 'route_not_advertised')).toEqual([]);
  }));
});

describe('sessions show names the offered route on dispatch errors (6cbb862a AC c)', () => {
  async function show(account: string, profile: string | null, launchError: any) {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('session-route-6cbb862a-' + crypto.randomUUID()));
    let view: any;
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, {}, {});
      s.init(); seedFixtureIntake(hub.sql);
      const at = new Date().toISOString();
      hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, exit_reason, launch_error_json, created_at, updated_at)
        VALUES ('launch-6cbb', 'test-admin', 'human', NULL, 'fixture', 'fleet-launch-6cbb', '{"workspace":"demo"}', 'tabo', 'codex', ?, ?, 'headless', 'demo', 'goal', 'failed', ?, ?, ?, ?)`,
        account, profile, launchError.reason, JSON.stringify(launchError), at, at);
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at)
        VALUES ('6cbb862a-0000-4000-8000-000000000001', 'test-admin', 'key-6cbb', '{}', 'launch-6cbb', 'tabo', 'codex', 'headless', 'failed', ?, ?)`, at, at);
      const res = await s.handle(new Request('https://hub.test/api/sessions/6cbb862a-0000-4000-8000-000000000001', { method: 'GET' }), admin);
      expect(res.status).toBe(200);
      view = await res.json();
    });
    return view;
  }
  const routeError = { reason: 'dispatch_route_unconfigured', message: 'The offered account, engine and profile do not match a local route.', stage: 'dispatch_route', error_type: 'ValueError' };

  it('includes the offered account/profile and the failing route', async () => {
    const view = await show('shared-codex', 'hub-shared-codex', routeError);
    const route = { machine: 'tabo', engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex' };
    expect(view.reason).toBe('dispatch_route_unconfigured');
    expect(view.offered_route).toEqual(route);
    expect(view.launch_error).toEqual({ ...routeError, route });
  });

  it('sanitizes route labels and leaves runtime errors unannotated', async () => {
    const view = await show('secret=/private/token', 'hub-shared-codex', routeError);
    expect(view.offered_route).toEqual({ machine: 'tabo', engine: 'codex', account: null, profile: 'hub-shared-codex' });
    expect(JSON.stringify(view)).not.toContain('/private/token');
    const runtime = await show('shared-codex', 'hub-shared-codex', { reason: 'engine_exited', message: 'exited', stage: 'runtime' });
    expect(runtime.launch_error.route).toBeUndefined();
    expect(runtime.offered_route.account).toBe('shared-codex');
  });
});
