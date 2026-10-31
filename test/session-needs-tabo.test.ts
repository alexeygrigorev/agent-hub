import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';
import { seedFixtureIntake, withFixtureIntake, FIXTURE_INTAKE_ID } from './tbm-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'needs test', idempotency_key: crypto.randomUUID(), ...extra });

async function fixture(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-needs-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init();
    const sql = hub.sql;
    const hb = JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });
    // Hetzner Linux amd64, Tabo Windows arm64.
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      'hetzner', 'hetzner', 'hetzner', 'Linux', 'amd64', iso(), iso(), hb, 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      'tabo', 'tabo', 'tabo', 'Windows', 'arm64', iso(), iso(), hb, 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('shared-codex', 'codex', 4, 15, 1)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('hetzner', 'codex', 'shared-codex', NULL)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('tabo', 'codex', 'shared-codex', NULL)`);
    for (const m of ['hetzner', 'tabo']) {
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        m, '1.0', iso(), now, iso(), now, 'ok', null,
        JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    }
    await fn(service, sql);
  });
}

describe('session needs routing (Tabo AC4)', () => {
  it('rejects invalid needs and routes os:windows only to Tabo', async () => fixture(async (service) => {
    const bad = service.plan(intent({ needs: ['BAD'] }));
    expect((bad as any).error).toBe('invalid_launch_request');
    const dup: any = service.plan(intent({ needs: ['os:windows', 'os:windows'] }));
    // Duplicate needs are invalid (same CAP-uniqueness rule as routing).
    expect(dup.error).toBe('invalid_launch_request');

    const win = service.plan(intent({ needs: ['os:windows'] }));
    expect(win.selected?.machine).toBe('tabo');
    expect((win as any).rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'needs_unmet' }));
  }));

  it('holds with needs_unmet when no Windows host is fresh', async () => fixture(async (service, sql) => {
    sql.exec(`UPDATE machines SET last_checkin_at = ? WHERE id = 'tabo'`, new Date(now - 3600000).toISOString());
    const plan = service.plan(intent({ needs: ['os:windows'] }));
    expect(plan.selected).toBeNull();
    const rejected = (plan as any).rejected as any[];
    expect(rejected.length).toBeGreaterThan(0);
    expect(rejected.every((r: any) => r.reason === 'needs_unmet' || r.reason === 'machine_unavailable')).toBe(true);
    expect(rejected).toContainEqual(expect.objectContaining({ reason: 'needs_unmet' }));
  }));

  it('does not route on expired operator capability labels', async () => fixture(async (service, sql) => {
    sql.exec(`INSERT OR REPLACE INTO routing_machines (machine_id, needs_json, max_agents, observed_at, runners_json) VALUES ('tabo', ?, 2, ?, ?)`,
      JSON.stringify(['dotnet:10']), iso(-3600000), JSON.stringify([]));
    const plan = service.plan(intent({ needs: ['dotnet:10'] }));
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'tabo', reason: 'needs_unmet' }));
  }));

  it('matches synthesized arch labels and operator routing needs', async () => fixture(async (service, sql) => {
    sql.exec(`INSERT OR REPLACE INTO routing_machines (machine_id, needs_json, max_agents, observed_at, runners_json) VALUES ('tabo', ?, 2, ?, ?)`,
      JSON.stringify(['os:windows', 'arch:arm64', 'dotnet:10']), iso(), JSON.stringify([]));
    const plan = service.plan(intent({ needs: ['os:windows', 'arch:arm64'] }));
    expect(plan.selected?.machine).toBe('tabo');
    const dotnet = service.plan(intent({ needs: ['dotnet:10'] }));
    expect(dotnet.selected?.machine).toBe('tabo');
    const missing = service.plan(intent({ needs: ['os:windows', 'gpu:nvidia'] }));
    expect(missing.selected).toBeNull();
    expect(missing.rejected).toContainEqual(expect.objectContaining({ reason: 'needs_unmet' }));
  }));

  // Intake 6cb8890f AC2: Orin bank/desktop tasks target DESKTOP-FP4OP26 by
  // hostname and/or host-advertised custom capability tags.
  const desktopTags = ['browser:orin-chrome', 'phone:adb', 'tool:smscli', 'tool:agent-browser'];
  it('routes custom desktop capability tags and hostname --machine', async () => fixture(async (service, sql) => {
    sql.exec(`UPDATE machines SET hostname = 'DESKTOP-FP4OP26', name = 'DESKTOP-FP4OP26' WHERE id = 'tabo'`);
    sql.exec(`INSERT OR REPLACE INTO routing_machines (machine_id, needs_json, max_agents, observed_at, runners_json) VALUES ('tabo', ?, 2, ?, ?)`,
      JSON.stringify(desktopTags), iso(), JSON.stringify([]));
    const tagged = service.plan(intent({ mode: 'interactive', needs: ['os:windows', 'browser:orin-chrome', 'tool:agent-browser'] }));
    expect(tagged.selected?.machine).toBe('tabo');
    expect(tagged.rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'needs_unmet' }));
    for (const ref of ['DESKTOP-FP4OP26', 'desktop-fp4op26', 'tabo']) {
      const byName = service.plan(intent({ mode: 'interactive', machine: ref, needs: desktopTags }));
      expect(byName.selected?.machine).toBe('tabo');
      // The canonical intent keeps the caller's selector for idempotency.
      expect(byName.request.machine).toBe(ref);
    }
    const missing = service.plan(intent({ mode: 'interactive', machine: 'DESKTOP-FP4OP26', needs: ['tool:acy'] }));
    expect(missing.selected).toBeNull();
    expect(missing.rejected).toEqual([expect.objectContaining({ machine: 'tabo', reason: 'needs_unmet' })]);
  }));

  it('explains unknown or route-less --machine instead of an empty rejected[]', async () => fixture(async (service, sql) => {
    const unknown = service.plan(intent({ machine: 'DESKTOP-NOPE' }));
    expect(unknown.selected).toBeNull();
    expect(unknown.rejected).toEqual([{ machine: 'DESKTOP-NOPE', engine: '*', reason: 'machine_unknown' }]);
    sql.exec(`DELETE FROM fleet_routes WHERE machine_id = 'tabo'`);
    const noRoutes = service.plan(intent({ machine: 'tabo' }));
    expect(noRoutes.rejected).toEqual([{ machine: 'tabo', engine: '*', reason: 'machine_no_routes' }]);
  }));
});

describe('session start needs validation (Tabo AC4)', () => {
  const agentAuth = { caller: { kind: 'agent', actor: 'demo:assistant', tokenId: 'cred-1' }, scopes: ['fleet:launch', 'bus:send'] } as any;
  function sessionRequest(data: any) {
    return new Request('https://hub.test/api/sessions', { method: 'POST', body: JSON.stringify(withFixtureIntake('/api/sessions', data, 'POST')) });
  }
  async function sessionFixture(fn: (s: SessionControlService, sql: any, calls: any[]) => Promise<void>) {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('session-needs-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const calls: any[] = [];
      const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, {
        reserve: (input: any) => {
          calls.push(input);
          return { value: { id: 'launch-1', state: 'offered', machine: 'tabo', engine: 'codex' }, status: 201 };
        },
      });
      service.init();
      seedFixtureIntake(hub.sql);
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
      hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
        VALUES ('cred-1', 'agent', 'h1', 'r1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
      await fn(service, hub.sql, calls);
    });
  }
  it('accepts needs and passes them to fleet reserve', async () => sessionFixture(async (s, _sql, calls) => {
    const res = await s.handle(sessionRequest({ key: 'k-needs-1', workspace: 'demo', brief: 'goal', needs: ['os:windows', 'arch:arm64'] }), agentAuth);
    expect(res.status).toBe(201);
    expect(calls[0].needs).toEqual(['os:windows', 'arch:arm64']);
  }));
  it('stores the Hub machine id for a hostname --machine', async () => sessionFixture(async (s, sql, calls) => {
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, capabilities) VALUES ('desk-id', 'DESKTOP-FP4OP26', 'DESKTOP-FP4OP26', 'Windows', 'amd64', ?, '{}')`, new Date().toISOString());
    const res = await s.handle(sessionRequest({ key: 'k-needs-host', workspace: 'demo', brief: 'goal', machine: 'desktop-fp4op26', needs: ['tool:agent-browser'] }), agentAuth);
    expect(res.status).toBe(201);
    expect(calls[0].machine).toBe('desk-id');
    expect(calls[0].needs).toEqual(['tool:agent-browser']);
  }));
  it('rejects invalid and duplicate needs', async () => sessionFixture(async (s) => {
    const bad = await s.handle(sessionRequest({ key: 'k-needs-2', workspace: 'demo', brief: 'goal', needs: ['BAD'] }), agentAuth);
    expect(bad.status).toBe(400);
    const dup = await s.handle(sessionRequest({ key: 'k-needs-3', workspace: 'demo', brief: 'goal', needs: ['os:windows', 'os:windows'] }), agentAuth);
    expect(dup.status).toBe(400);
  }));
});
