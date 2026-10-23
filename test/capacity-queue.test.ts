import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, isCapacityOnlyHold } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const NOW = Date.now();
const iso = (delta = 0) => new Date(NOW + delta).toISOString();

function fleetReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
function sessReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}
function registry() {
  return { accounts: [
    { label: 'shared-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
    { label: 'desktop-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
  ], routes: [
    { machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null },
    { machine_id: 'win35', engine: 'codex', account: 'desktop-codex', profile: null },
  ] };
}
function seedMachines(sql: any) {
  for (const machine of ['hetzner', 'win35']) {
    sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', iso(), NOW, iso(), NOW, 'ok', null, JSON.stringify(Object.fromEntries(['codex', 'claude'].map(provider => [provider, { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } }]))));
  }
}
function seedAgent(sql: any) {
  const at = new Date(NOW).toISOString();
  const far = new Date(NOW + 30 * 86400000).toISOString();
  sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
  sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
    VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
}
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'capacity queue probe', idempotency_key: crypto.randomUUID(), ...extra });

async function fleetFixture(fn: (s: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('capacity-fleet-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => NOW);
    service.init();
    seedMachines(hub.sql);
    await fn(service, hub.sql);
  });
}
async function combinedFixture(fn: (f: FleetService, s: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('capacity-combined-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const tx = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const fleet = new FleetService(hub.sql, tx, () => NOW);
    fleet.init();
    const sessions = new SessionControlService(hub.sql, tx, () => NOW, { reserve: (input, auth) => fleet.reserve(input, auth) });
    sessions.init();
    seedMachines(hub.sql);
    seedFixtureIntake(hub.sql);
    seedAgent(hub.sql);
    await fn(fleet, sessions, hub.sql);
  });
}
async function configure(f: FleetService) {
  expect((await f.handle(fleetReq('/accounts', registry(), 'PUT'), admin)).status).toBe(200);
}
async function claimAndFinish(f: FleetService, launchId: string, machine: string, account: string) {
  const claim = { gate_allowed: true, observed_at: iso(), account, profile_supported: true };
  expect((await f.handle(fleetReq('/launches/' + launchId + '/claim', claim), host(machine))).status).toBe(200);
  const native = { state: 'running', native_id: 'native-' + launchId.slice(0, 8), launcher_task_id: 'hubtask1' };
  expect((await f.handle(fleetReq('/launches/' + launchId + '/result', native), host(machine))).status).toBe(200);
  const done = { state: 'exited', native_id: native.native_id, launcher_task_id: native.launcher_task_id, confirmed_exit: true };
  const res = await f.handle(fleetReq('/launches/' + launchId + '/result', done), host(machine));
  expect(res.status, JSON.stringify(await res.clone().json())).toBe(200);
}

describe('capacity-only classifier', () => {
  it('queues only on transient capacity, never on incidentals or other classes alone', () => {
    expect(isCapacityOnlyHold([])).toBe(false);
    expect(isCapacityOnlyHold(null)).toBe(false);
    expect(isCapacityOnlyHold([{ machine: 'h', engine: 'codex', reason: 'account_capacity' }])).toBe(true);
    expect(isCapacityOnlyHold([{ machine: 'h', engine: 'codex', reason: 'machine_capacity' }])).toBe(true);
    expect(isCapacityOnlyHold([
      { machine: 'h', engine: 'codex', reason: 'account_capacity' },
      { machine: 'w', engine: 'codex', reason: 'machine_unavailable' },
      { machine: 'x', engine: 'codex', reason: 'provider_disabled' },
    ])).toBe(true);
    expect(isCapacityOnlyHold([{ machine: 'w', engine: 'codex', reason: 'machine_unavailable' }])).toBe(false);
    expect(isCapacityOnlyHold([{ machine: 'h', engine: 'codex', reason: 'provider_disabled' }])).toBe(false);
    expect(isCapacityOnlyHold([{ machine: 'h', engine: 'gemini', reason: 'engine_unavailable' }])).toBe(false);
    expect(isCapacityOnlyHold([
      { machine: 'h', engine: 'codex', reason: 'account_capacity' },
      { machine: 'h', engine: 'gemini', reason: 'engine_unavailable' },
      { machine: 'w', engine: 'codex', reason: 'machine_unavailable' },
      { machine: 'x', engine: 'grok', reason: 'provider_disabled' },
    ])).toBe(true);
    for (const reason of ['capability_unavailable', 'admission_unknown', 'resource_limit', 'load_unknown', 'tier_unavailable', 'account_blocked', 'account_unknown', 'machine_account_quota_unknown']) {
      expect(isCapacityOnlyHold([
        { machine: 'h', engine: 'codex', reason: 'account_capacity' },
        { machine: 'w', engine: 'codex', reason },
      ]), reason).toBe(false);
    }
  });
});

describe('fleet reserve capacity queue', () => {
  it('queues all-capacity placement as 202 and auto-places FIFO when a slot frees', () => fleetFixture(async (f, sql) => {
    await configure(f);
    const first: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(first.state).toBe('offered');
    const second = await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-second' })), admin);
    expect(second.status).toBe(202);
    const parked: any = await second.json();
    expect(parked.state).toBe('queued');
    expect(parked.queued).toBe(true);
    expect(parked.queue_position).toBe(1);
    expect(parked.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'account_capacity' });
    // Same-key replay observes the same queued intent (no second reservation).
    const replay = await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-second' })), admin);
    expect(replay.status).toBe(202);
    const replayed: any = await replay.json();
    expect(replayed.id).toBe(parked.id);
    expect(replayed.replay).toBe(true);
    // A conflicting intent on the same key still conflicts.
    expect((await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-second', prompt: 'changed' })), admin)).status).toBe(409);
    const third: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-third' })), admin)).json();
    expect(third.state).toBe('queued');
    expect(third.queue_position).toBe(2);
    // Freeing the slot auto-places the oldest waiter; the later waiter stays queued.
    await claimAndFinish(f, first.id, 'hetzner', 'shared-codex');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', parked.id).one().state).toBe('offered');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', third.id).one().state).toBe('queued');
    // The promoted intent replays as offered; the still-queued one replays queued.
    const promoted: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-second' })), admin)).json();
    expect(promoted.state).toBe('offered');
    expect(promoted.replay).toBe(true);
    expect(promoted.machine).toBe('hetzner');
    const waiting: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner', idempotency_key: 'queue-third' })), admin)).json();
    expect(waiting.state).toBe('queued');
    expect(waiting.queue_position).toBe(1);
  }));

  it('keeps mixed capacity+capability failures as visible 409 rejects without queueing', () => fleetFixture(async (f, sql) => {
    await configure(f);
    const first: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(first.state).toBe('offered');
    sql.exec("UPDATE machines SET capabilities = '{}' WHERE id = 'win35'");
    const res = await f.handle(fleetReq('/launches', intent({ idempotency_key: 'mixed-reject' })), admin);
    expect(res.status).toBe(409);
    const body: any = await res.json();
    expect(body.error).toBe('no_eligible_route');
    expect(body.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'account_capacity' });
    expect(body.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'capability_unavailable' });
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches WHERE request_key = ?', 'mixed-reject').one().n).toBe(0);
  }));

  it('queues beside incidental stale routes but rejects incidentals alone', () => fleetFixture(async (f, sql) => {
    await configure(f);
    const first: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(first.state).toBe('offered');
    sql.exec('UPDATE machines SET drained = 1 WHERE id = ?', 'win35');
    const queued = await f.handle(fleetReq('/launches', intent({ idempotency_key: 'incidental-queue' })), admin);
    expect(queued.status).toBe(202);
    expect(((await queued.json()) as any).state).toBe('queued');
    const alone = await f.handle(fleetReq('/launches', intent({ machine: 'win35', idempotency_key: 'incidental-alone' })), admin);
    expect(alone.status).toBe(409);
    expect(((await alone.json()) as any).rejected).toEqual([{ machine: 'win35', engine: 'codex', reason: 'machine_unavailable' }]);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches WHERE request_key = ?', 'incidental-alone').one().n).toBe(0);
  }));

  it('marks engines no live host advertises as engine_unavailable and queues them with capacity', () => fleetFixture(async (f, sql) => {
    const withStale = registry();
    withStale.routes.push({ machine_id: 'hetzner', engine: 'gemini', account: 'shared-codex', profile: null });
    expect((await f.handle(fleetReq('/accounts', withStale, 'PUT'), admin)).status).toBe(200);
    // Fill both live codex slots so every advertised engine is capacity-blocked.
    for (const [machine, key] of [['hetzner', 'stale-first'], ['win35', 'stale-second']] as const) {
      const placed: any = await (await f.handle(fleetReq('/launches', intent({ machine, idempotency_key: key })), admin)).json();
      expect(placed.state).toBe('offered');
    }
    const res = await f.handle(fleetReq('/launches', intent({ idempotency_key: 'stale-queue' })), admin);
    expect(res.status).toBe(202);
    const body: any = await res.json();
    expect(body.state).toBe('queued');
    expect(body.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'account_capacity' });
    expect(body.rejected).toContainEqual({ machine: 'hetzner', engine: 'gemini', reason: 'engine_unavailable' });
    expect(body.rejected.some((r: any) => r.reason === 'capability_unavailable')).toBe(false);
    // A stale engine alone (no capacity in the mix) still hard-rejects.
    const alone = await f.handle(fleetReq('/launches', intent({ engine: 'gemini', idempotency_key: 'stale-alone' })), admin);
    expect(alone.status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_launches WHERE request_key = ?', 'stale-alone').one().n).toBe(0);
  }));
});

describe('fleet account status liveness', () => {
  it('reports drifted registry routes as zero placeable slots while the raw pool stays honest', () => fleetFixture(async (f, sql) => {
    await configure(f);
    const status = (label: string) => f.accountStatus(sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', label).one()) as any;
    const healthy: any = status('desktop-codex');
    expect(healthy.available_slots).toBe(1);
    expect(healthy.placeable_slots).toBe(1);
    expect(healthy.placeable_machines).toEqual(['win35']);
    expect(healthy.registry_drift).toBe(false);
    // A host release stops serving codex on win35 (subscription change) while
    // the registry route stays: raw pool unchanged, nothing placeable.
    sql.exec("UPDATE machines SET capabilities = ? WHERE id = 'win35'", JSON.stringify({ engines: ['claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    const drifted: any = status('desktop-codex');
    expect(drifted.available_slots).toBe(1);
    expect(drifted.placeable_slots).toBe(0);
    expect(drifted.placeable_machines).toEqual([]);
    expect(drifted.registry_drift).toBe(true);
    expect(drifted.status).toBe('ok');
    // Once no live machine advertises codex at all, plan() classifies every
    // codex route engine_unavailable (correct) — the lying half was fleet
    // status advertising phantom slots.
    sql.exec("UPDATE machines SET capabilities = ? WHERE id = 'hetzner'", JSON.stringify({ engines: ['claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    const all: any = status('shared-codex');
    expect(all.available_slots).toBe(1);
    expect(all.placeable_slots).toBe(0);
    expect(all.registry_drift).toBe(true);
    const res = await f.handle(fleetReq('/launches', intent({ idempotency_key: 'drifted-plan' })), admin);
    expect(res.status).toBe(409);
    const body: any = await res.json();
    expect(body.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'engine_unavailable' });
    expect(body.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'engine_unavailable' });
    // The admin fleet listing carries the liveness fields end to end.
    const listing: any = await (await f.handle(fleetReq('', undefined, 'GET'), admin)).json();
    const shared: any = listing.accounts.find((a: any) => a.label === 'desktop-codex');
    expect(shared.placeable_slots).toBe(0);
    expect(shared.registry_drift).toBe(true);
  }));
});

describe('sessions_start capacity queue', () => {
  it.each(['starting', 'running'])('reconciles a queued intent after Fleet already reached %s, without a client poll', state => combinedFixture(async (f, s, sql) => {
    await configure(f);
    for (const machine of ['hetzner', 'win35']) {
      sql.exec("INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'host', ?, ?, ?, 'test-host', ?, '[\"host\"]', 'test', ?, ?)", 'host-' + machine, 'hash-' + machine, 'refresh-' + machine, 'family-' + machine, machine, iso(), iso(86400000));
      sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', 'host-' + machine, machine);
    }
    const enrolled = (machine: string) => ({ ...host(machine), caller: { ...host(machine).caller, tokenId: 'host-' + machine } });
    const blocking: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    const parked: any = await (await s.handle(sessReq('/api/sessions', { key: 'fast-promotion', workspace: 'demo', machine: 'hetzner', brief: 'queue race probe' }), admin)).json();
    expect(parked.state).toBe('queued');
    await claimAndFinish(f, blocking.id, 'hetzner', 'shared-codex');
    const nativeId = '11111111-2222-4333-8444-555555555555';
    expect((await f.handle(fleetReq('/launches/' + parked.fleet_launch_id + '/claim', { gate_allowed: true, observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'))).status).toBe(200);
    if (state === 'running') expect((await f.handle(fleetReq('/launches/' + parked.fleet_launch_id + '/result', { state: 'running', native_id: nativeId, launcher_task_id: 'queue-race' }), host('hetzner'))).status).toBe(200);
    // A foreign host cannot adopt the assignment, and no native identity is invented.
    const foreign: any = await (await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), enrolled('win35'))).json();
    expect(foreign.sessions).toHaveLength(0);
    const stale: any = await (await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner'))).json();
    expect(stale.sessions).toHaveLength(0);
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', parked.id).one().state).toBe('queued');
    const pending: any = await (await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), enrolled('hetzner'))).json();
    expect(pending.sessions).toContainEqual({ id: parked.id, fleet_launch_id: parked.fleet_launch_id, engine: 'codex', native_id: state === 'running' ? nativeId : null });
    const adopted: any = await (await s.handle(sessReq('/api/sessions/' + parked.id, undefined, 'GET'), admin)).json();
    expect(adopted.state).toBe('starting');
    expect(adopted.machine).toBe('hetzner');
    expect(adopted.binding).toBeNull();
  }));
  it('parks capacity-only starts as waitable queued intents that place when slots free', () => combinedFixture(async (f, s, sql) => {
    await configure(f);
    const blocking: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(blocking.state).toBe('offered');
    const start = await s.handle(sessReq('/api/sessions', { key: 'cap-start', workspace: 'demo', machine: 'hetzner', brief: 'capacity queue probe' }), admin);
    expect(start.status).toBe(202);
    const parked: any = await start.json();
    expect(parked.state).toBe('queued');
    expect(parked.queued).toBe(true);
    expect(typeof parked.fleet_launch_id).toBe('string');
    expect(parked.rejected).toContainEqual({ machine: 'hetzner', engine: 'codex', reason: 'account_capacity' });
    // Same-key polling observes the same queued intent without duplicating it.
    const poll = await s.handle(sessReq('/api/sessions', { key: 'cap-start', workspace: 'demo', machine: 'hetzner', brief: 'capacity queue probe' }), admin);
    expect(poll.status).toBe(200);
    const polled: any = await poll.json();
    expect(polled.id).toBe(parked.id);
    expect(polled.replay).toBe(true);
    expect(polled.state).toBe('queued');
    expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions WHERE request_key = ?', 'cap-start').one().n).toBe(1);
    // Freeing the slot places the intent; the session poll adopts the offer.
    await claimAndFinish(f, blocking.id, 'hetzner', 'shared-codex');
    const placed: any = await (await s.handle(new Request('https://hub.test/api/sessions/' + parked.id, { method: 'GET' }), admin)).json();
    expect(placed.state).toBe('offered');
    expect(placed.machine).toBe('hetzner');
    expect(placed.fleet_launch_id).toBe(parked.fleet_launch_id);
  }));

  it('surfaces capability rejects as held with the slug visible and no fleet link', () => combinedFixture(async (f, s, sql) => {
    await configure(f);
    const blocking: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(blocking.state).toBe('offered');
    sql.exec("UPDATE machines SET capabilities = '{}' WHERE id = 'win35'");
    const res = await s.handle(sessReq('/api/sessions', { key: 'cap-mixed', workspace: 'demo', brief: 'mixed probe' }), admin);
    expect(res.status).toBe(409);
    const body: any = await res.json();
    expect(body.state).toBe('held');
    expect(body.hold.error).toBe('no_eligible_route');
    expect(body.hold.rejected).toContainEqual({ machine: 'win35', engine: 'codex', reason: 'capability_unavailable' });
    expect(body.fleet_launch_id).toBeNull();
  }));

  it('cancels a queued intent and releases its fleet waitlist row', () => combinedFixture(async (f, s, sql) => {
    await configure(f);
    const blocking: any = await (await f.handle(fleetReq('/launches', intent({ machine: 'hetzner' })), admin)).json();
    expect(blocking.state).toBe('offered');
    const start = await s.handle(sessReq('/api/sessions', { key: 'cap-cancel', workspace: 'demo', machine: 'hetzner', brief: 'cancel probe' }), agent());
    expect(start.status).toBe(202);
    const parked: any = await start.json();
    expect(parked.state).toBe('queued');
    const cancel = await s.handle(sessReq('/api/sessions/' + parked.id + '/cancel', { key: 'cancel-once' }, 'POST'), agent());
    expect(cancel.status, JSON.stringify(await cancel.clone().json())).toBe(200);
    const cancelled: any = await cancel.json();
    expect(cancelled.state).toBe('cancelled');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', parked.fleet_launch_id).one().state).toBe('cancelled');
  }));
});
