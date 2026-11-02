import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';

// Intake dcac7cd4: a needs-carrying start held while no host advertising the
// needs is online AND routed must say so at the top level (hold_reason
// no_routed_host_matches_needs) and name the enrolled machines whose labels
// satisfy the needs with their online/route state. rejected[] only lists
// machines that have routes, so the Windows request held while the Windows
// host had no route advertisement looked like a broken auto-restart.

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'needs hold test', idempotency_key: crypto.randomUUID(), ...extra });

function machineRow(id: string, os: string, arch: string, hb: string, checkin = iso()) {
  return [id, id, id, os, arch, iso(), checkin, hb, 0,
    JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 })];
}

const healthyHb = () => JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });

/** Hetzner (Linux, routed) + Tabo (Windows, enrolled but WITHOUT routes):
 * the 2026-10-31 15:59Z fleet shape that held session 62833613. */
async function fleetFixture(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('needs-hold-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init();
    const sql = hub.sql;
    const hb = healthyHb();
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ...machineRow('hetzner', 'Linux', 'amd64', hb));
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ...machineRow('tabo', 'Windows', 'arm64', hb));
    sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('shared-codex', 'codex', 4, 15, 1)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('hetzner', 'codex', 'shared-codex', NULL)`);
    for (const m of ['hetzner', 'tabo']) {
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        m, '1.0', iso(), now, iso(), now, 'ok', null,
        JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    }
    await fn(service, sql);
  });
}

const addTaboRoute = (sql: any) => sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('tabo', 'codex', 'shared-codex', NULL)`);

describe('needs hold diagnosis (intake dcac7cd4, fleet plan)', () => {
  it('held os:windows start with an unrouted Windows host fires the top-level reason and names it (AC1/AC3)', async () => fleetFixture(async (service) => {
    const plan: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected).toBeNull();
    expect(plan.hold_reason).toBe('no_routed_host_matches_needs');
    expect(plan.needs_machines).toEqual([expect.objectContaining({
      machine: 'tabo', hostname: 'tabo', online: true, routed: false, last_checkin_at: iso(),
    })]);
    expect(plan.needs_machines[0].routes).toEqual([]);
    // AC2: per-route rejected[] entries unchanged.
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'needs_unmet' }));
  }));

  it('an offline Windows host WITH routes still fires with online=false, routed=true (AC1)', async () => fleetFixture(async (service, sql) => {
    addTaboRoute(sql);
    sql.exec(`UPDATE machines SET last_checkin_at = ? WHERE id = 'tabo'`, iso(-3600000));
    const plan: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected).toBeNull();
    expect(plan.hold_reason).toBe('no_routed_host_matches_needs');
    expect(plan.needs_machines[0]).toMatchObject({ machine: 'tabo', online: false, routed: true });
  }));

  it('does not fire when a satisfying host is online, routed and held for another reason (AC2)', async () => fleetFixture(async (service, sql) => {
    addTaboRoute(sql);
    sql.exec('UPDATE machines SET last_heartbeat = ? WHERE id = ?', JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 1, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 'tabo');
    const plan: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected).toBeNull();
    expect(plan.hold_reason).toBeUndefined();
    expect(plan.needs_machines).toBeUndefined();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'tabo', reason: 'disk_floor' }));
  }));

  it('places on the Windows host once it gains routes, without hold fields (AC3)', async () => fleetFixture(async (service, sql) => {
    addTaboRoute(sql);
    const plan: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected?.machine).toBe('tabo');
    expect(plan.hold_reason).toBeUndefined();
    expect(plan.needs_machines).toBeUndefined();
  }));

  it('bounds needs_machines at 64 machines (AC2)', async () => fleetFixture(async (service, sql) => {
    const hb = healthyHb();
    for (let i = 0; i < 70; i++) {
      sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        ...machineRow(`win-${String(i).padStart(2, '0')}`, 'Windows', 'amd64', hb));
    }
    const plan: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected).toBeNull();
    expect(plan.hold_reason).toBe('no_routed_host_matches_needs');
    expect(plan.needs_machines.length).toBe(64);
    expect(plan.needs_machines.every((m: any) => m.online && !m.routed)).toBe(true);
  }));

  it('leaves needs-free plans untouched', async () => fleetFixture(async (service) => {
    const plan: any = service.plan(intent());
    expect(plan.selected?.machine).toBe('hetzner');
    expect(plan.hold_reason).toBeUndefined();
    expect(plan.needs_machines).toBeUndefined();
  }));
});

describe('needs hold diagnosis (intake dcac7cd4, sessions start/show)', () => {
  function sessionRequest(path: string, data?: any, method = 'POST') {
    return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
  }
  async function sessionFixture(fn: (s: SessionControlService, fleet: FleetService, sql: any) => Promise<void>) {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('needs-hold-session-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb));
      fleet.init();
      const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, {
        reserve: (input: any, auth: any) => fleet.reserve(input, auth),
      });
      service.init();
      seedFixtureIntake(hub.sql);
      const hb = healthyHb();
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        ...machineRow('hetzner', 'Linux', 'amd64', hb));
      hub.sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        ...machineRow('tabo', 'Windows', 'arm64', hb));
      hub.sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('shared-codex', 'codex', 4, 15, 1)`);
      hub.sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('hetzner', 'codex', 'shared-codex', NULL)`);
      for (const m of ['hetzner', 'tabo']) {
        hub.sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
          m, '1.0', iso(), now, iso(), now, 'ok', null,
          JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
      }
      await fn(service, fleet, hub.sql);
    });
  }

  it('hold response and sessions show carry the top-level reason + machines (AC1)', async () => sessionFixture(async (s, _fleet, sql) => {
    const res = await s.handle(sessionRequest('/api/sessions', { key: 'needs-hold-1', workspace: 'demo', brief: 'windows evidence run', needs: ['os:windows'] }), admin);
    expect(res.status).toBe(409);
    const body: any = await res.json();
    expect(body.state).toBe('held');
    expect(body.hold_reason).toBe('no_routed_host_matches_needs');
    expect(body.needs_machines).toEqual([expect.objectContaining({ machine: 'tabo', online: true, routed: false })]);
    expect(body.hold.error).toBe('no_eligible_route');
    expect(body.hold.hold_reason).toBe('no_routed_host_matches_needs');
    expect(body.hold.rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'needs_unmet' }));

    const show: any = await (await s.handle(sessionRequest(`/api/sessions/${body.id}`, undefined, 'GET'), admin)).json();
    expect(show.state).toBe('held');
    expect(show.reason).toBe('needs_unmet');
    expect(show.hold_reason).toBe('no_routed_host_matches_needs');
    expect(show.needs_machines).toEqual([expect.objectContaining({ machine: 'tabo', online: true, routed: false })]);
    expect(show.rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'needs_unmet' }));
    void sql;
  }));

  it('the held intent places on the Windows host once it gains routes (AC3)', async () => sessionFixture(async (s, _fleet, sql) => {
    const res = await s.handle(sessionRequest('/api/sessions', { key: 'needs-hold-2', workspace: 'demo', brief: 'windows evidence run', needs: ['os:windows'] }), admin);
    const body: any = await res.json();
    expect(body.state).toBe('held');
    addTaboRoute(sql);
    const reconcile = s.reconcileHeldSessions();
    expect(reconcile.promoted).toBe(1);
    const show: any = await (await s.handle(sessionRequest(`/api/sessions/${body.id}`, undefined, 'GET'), admin)).json();
    expect(show.state).toBe('offered');
    expect(show.machine).toBe('tabo');
    expect(show.hold_reason).toBeUndefined();
    expect(show.needs_machines).toBeUndefined();
  }));
});
