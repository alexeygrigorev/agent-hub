/** Intake 056a4131 AC2/AC4: routing views and the dispatch pre-filter must
 * answer with plan()'s engine-health predicate — machine-scoped rows plus
 * global '' rows, mode-aware (engine_tool_permission_refused blocks headless
 * only) — exposing reason + affected mode per runner. */
import {freshCpu} from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import { blockedEngines, blockingHealthRow, runnerHealth } from '../src/engine-health';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'admin', kind: 'human' }, scopes: ['admin'] };
const client = { caller: { actor: 'client-a', kind: 'agent' }, scopes: ['routing:submit'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host' }, scopes: ['host'] });
const now = Date.now();
const at = (delta = 0) => new Date(now + delta).toISOString();
const quota = { status: 'ok', observed_at: at(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: at(86400000) } } };

const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const planReq = (extra = {}) => ({ workspace: 'tasks', prompt: 'health parity probe', idempotency_key: crypto.randomUUID(), ...extra });

async function fixture(fn: (r: RoutingService, f: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('engine-health-parity-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now); f.init();
    const r = new RoutingService(hub.sql, f, () => now, cb => state.storage.transactionSync(cb)); r.init();
    const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at()), gate: { launch_allowed: true, at: at() } }), JSON.stringify({ engines: ['zcodex'], fleet_modes: ['headless', 'interactive'], workspaces: ['tasks'], cpu_count: 8 }));
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', at(), now, at(), now, 'ok', null, JSON.stringify({ zai: quota }));
    }
    expect((await f.handle(req('/api/fleet/accounts', { accounts: [{ label: 'shared-zai', provider: 'zai', max_concurrency: 3, reserve_percent: 0, enabled: true }], routes: ['hetzner', 'win35'].map(machine_id => ({ machine_id, engine: 'zcodex', account: 'shared-zai', profile: null })) }, 'PUT'), admin)).status).toBe(200);
    for (const machine of ['hetzner', 'win35']) {
      expect((await f.handle(req('/api/fleet/subscriptions', { subscriptions: [{ service: 'zai', account: 'shared-zai', plan: null, quota }] }, 'PUT'), host(machine))).status).toBe(200);
      expect((await r.handle(req('/api/routing/capabilities', { needs: [], max_agents: 2, runners: [{ engine: 'zcodex', profile: null, account: 'shared-zai', model: 'glm-5.3-flash', effort: 'high' }] }, 'PUT'), host(machine))).status).toBe(200);
    }
    await fn(r, f, sql);
  });
}
const seedHold = (sql: any, engine: string, machineId: string, reason: string) =>
  sql.exec('INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, ?, ?, ?)', engine, machineId, 'unavailable', reason, at());
const machinesView = async (r: RoutingService) => (await (await r.handle(req('/api/routing/machines'), admin)).json() as any).machines;
const runnerOf = (machines: any[], machine: string) => machines.find(m => m.id === machine).runners[0];

describe('engine-health predicate parity (intake 056a4131)', () => {
  it('unit: mode-aware row selection and runner verdicts', () => {
    const refusal = { status: 'unavailable', reason: 'engine_tool_permission_refused', updated_at: at() };
    const auth = { status: 'unavailable', reason: 'engine_auth_expired', updated_at: at() };
    expect(blockingHealthRow([refusal], 'headless', now)).toBe(refusal);
    expect(blockingHealthRow([refusal], 'interactive', now)).toBeUndefined();
    expect(blockingHealthRow([refusal, auth], 'interactive', now)).toBe(auth);
    expect(blockingHealthRow([auth], 'interactive', now)).toBe(auth);
    expect(runnerHealth([], now)).toEqual({ available: true, health: 1, reason: null, mode: null });
    expect(runnerHealth([refusal], now)).toEqual({ available: true, health: 1, reason: 'engine_tool_permission_refused', mode: 'headless' });
    expect(runnerHealth([auth], now)).toEqual({ available: false, health: 0, reason: 'engine_auth_expired', mode: 'all' });
    // Retriable holds pass through after the retry cooldown, exactly like plan().
    const aged = { status: 'unavailable', reason: 'engine_auth_expired', updated_at: at(-601000) };
    expect(blockingHealthRow([aged], 'headless', now)).toBeUndefined();
    expect(runnerHealth([aged], now)).toEqual({ available: true, health: 1, reason: null, mode: null });
    const missing = { status: 'unavailable', reason: 'engine_auth_expired', updated_at: '' };
    expect(blockingHealthRow([missing], 'headless', now)).toBe(missing); // no cooldown pass without updated_at
    // Machine scoping: global rows apply everywhere, machine rows only there.
    const rows = [{ engine: 'zcodex', machine_id: 'hetzner', reason: 'engine_auth_expired', updated_at: at() }];
    expect(blockedEngines(rows, 'hetzner', 'headless', now)).toEqual(new Set(['zcodex']));
    expect(blockedEngines(rows, 'win35', 'headless', now)).toEqual(new Set());
    expect(blockedEngines([{ engine: 'zcodex', machine_id: '', reason: 'engine_auth_expired', updated_at: at() }], 'win35', 'headless', now)).toEqual(new Set(['zcodex']));
    expect(blockedEngines(rows, 'win35', 'interactive', now)).toEqual(new Set());
  });

  it('global engine_tool_permission_refused holds headless plans but not interactive, shown as mode headless', () => fixture(async (r, f, sql) => {
    seedHold(sql, 'zcodex', '', 'engine_tool_permission_refused');
    const headless: any = await (await f.handle(req('/api/fleet/plan', planReq({ machine: 'hetzner' }), 'POST'), admin)).json();
    expect(headless.rejected).toContainEqual({ machine: 'hetzner', engine: 'zcodex', reason: 'engine_tool_permission_refused' });
    const interactive: any = await (await f.handle(req('/api/fleet/plan', planReq({ machine: 'hetzner', mode: 'interactive' }), 'POST'), admin)).json();
    expect(interactive.selected.engine).toBe('zcodex');
    expect(interactive.rejected.filter((x: any) => x.reason === 'engine_tool_permission_refused')).toEqual([]);
    const runner = runnerOf(await machinesView(r), 'hetzner');
    expect(runner).toMatchObject({ available: true, health: 1, reason: 'engine_tool_permission_refused', mode: 'headless' });
  }));

  it('machine-scoped hold blocks only its machine; runner view matches plan() reason per machine', () => fixture(async (r, f, sql) => {
    seedHold(sql, 'zcodex', 'hetzner', 'engine_auth_expired');
    const hetzner: any = await (await f.handle(req('/api/fleet/plan', planReq({ machine: 'hetzner' }), 'POST'), admin)).json();
    expect(hetzner.rejected).toContainEqual({ machine: 'hetzner', engine: 'zcodex', reason: 'engine_auth_expired' });
    expect(hetzner.selected).toBeNull();
    const win35: any = await (await f.handle(req('/api/fleet/plan', planReq({ machine: 'win35' }), 'POST'), admin)).json();
    expect(win35.selected.engine).toBe('zcodex');
    const view = await machinesView(r);
    expect(runnerOf(view, 'hetzner')).toMatchObject({ available: false, health: 0, reason: 'engine_auth_expired', mode: 'all' });
    expect(runnerOf(view, 'win35')).toMatchObject({ available: true, health: 1, reason: null, mode: null });
  }));

  it('aged retriable hold passes cooldown in plan() and the view alike', () => fixture(async (r, f, sql) => {
    // One retrial may pass after ENGINE_HEALTH_RETRY_MS: plan() admits, so
    // the view must not show a health 0 for it (review finding, exact parity).
    sql.exec('INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, ?, ?, ?)', 'zcodex', 'hetzner', 'unavailable', 'engine_auth_expired', at(-601000));
    const hetzner: any = await (await f.handle(req('/api/fleet/plan', planReq({ machine: 'hetzner' }), 'POST'), admin)).json();
    expect(hetzner.rejected.filter((x: any) => x.reason === 'engine_auth_expired')).toEqual([]);
    expect(hetzner.selected.engine).toBe('zcodex');
    const runner = runnerOf(await machinesView(r), 'hetzner');
    expect(runner).toMatchObject({ available: true, health: 1, reason: null, mode: null });
  }));

  it('dispatch pre-filter never shadow-bans an engine fleet-wide from one machine hold', () => fixture(async (r, f, sql) => {
    seedHold(sql, 'zcodex', 'hetzner', 'engine_auth_expired');
    // Old pre-filter (DISTINCT engine over all machines) queued this job
    // forever: the hetzner-only row banned zcodex candidates everywhere.
    // Submit dispatches synchronously, so placement lands in the reply.
    const job: any = await (await r.handle(req('/api/tasks', { prompt: 'Route around the held machine', approved_action: 'Read only', needs: [], workspace: 'tasks', idempotency_key: crypto.randomUUID() }), client)).json();
    expect(job.machine).toBe('win35');
    expect(job.runner).toBe('zcodex');
  }));
});
