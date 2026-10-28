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
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-overflow-' + crypto.randomUUID()));
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

const launch = (sql: any, machine: string) => sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'admin', 'human', NULL, 's', ?, '{}', ?, 'codex', 'shared-codex', NULL, 'headless', 'demo', 'goal', 'running', ?, ?)`,
  crypto.randomUUID(), crypto.randomUUID(), machine, iso(), iso());

// Intake a3227276 AC3: capacity-aware unpinned placement onto Tabo.
describe('Windows overflow placement (Tabo AC3)', () => {
  it('keeps unpinned Linux-shaped work on Hetzner while it has room, even when Tabo scores higher', async () => fixture(async (service, sql) => {
    for (let i = 0; i < 3; i++) launch(sql, 'hetzner');
    const plan: any = service.plan(intent());
    expect(plan.selected?.machine).toBe('hetzner');
    const tabo = plan.candidates.find((c: any) => c.machine === 'tabo');
    expect(tabo).toMatchObject({ overflow: true });
    expect(tabo.score).toBeGreaterThan(plan.selected.score);
  }));

  it('chooses Tabo when Hetzner memory is below the floor', async () => fixture(async (service, sql) => {
    const low = JSON.stringify({ mem_avail_mb: 600, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });
    sql.exec(`UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'`, low);
    const plan: any = service.plan(intent());
    expect(plan.selected?.machine).toBe('tabo');
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'hetzner', reason: 'launcher_memory_reserve' }));
  }));

  it('stops overflowing onto Tabo once its own memory reservation is used', async () => fixture(async (service, sql) => {
    const low = (mb: number) => JSON.stringify({ mem_avail_mb: mb, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });
    sql.exec(`UPDATE machines SET last_heartbeat = ? WHERE id = 'hetzner'`, low(600));
    sql.exec(`UPDATE machines SET last_heartbeat = ? WHERE id = 'tabo'`, low(2604));
    expect((service.plan(intent()) as any).selected?.machine).toBe('tabo');
    sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'admin', 'human', NULL, 's', ?, '{}', 'tabo', 'codex', 'shared-codex', NULL, 'headless', 'demo', 'goal', 'offered', ?, ?)`,
      crypto.randomUUID(), crypto.randomUUID(), iso(), iso());
    const plan: any = service.plan(intent());
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'tabo', reason: 'launcher_memory_reserve' }));
  }));

  it('does not demote Tabo for pinned or os:windows work', async () => fixture(async (service) => {
    const pinned: any = service.plan(intent({ machine: 'tabo' }));
    expect(pinned.selected?.machine).toBe('tabo');
    const win: any = service.plan(intent({ needs: ['os:windows'] }));
    expect(win.selected?.machine).toBe('tabo');
    expect(win.selected.overflow).toBeUndefined();
  }));
});
