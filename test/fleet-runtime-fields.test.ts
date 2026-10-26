import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
async function fixture(fn: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-runtime-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init(); const sql = hub.sql;
    for (const machine of ['hetzner', 'win35']) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0, JSON.stringify({ engines: ['codex', 'claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', machine, '1.0', iso(), now, iso(), now, 'ok', null, JSON.stringify(Object.fromEntries(['codex', 'claude'].map(provider => [provider, { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } }]))));
    }
    await fn(service, sql);
  });
}
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'private test goal', idempotency_key: crypto.randomUUID(), ...extra });

describe('runtime diagnostic fields (intake a19cac9e AC1)', () => {
  it('accepts exit code, signal, wall time and sanitized tail', async () => fixture(async (s, sql) => {
    await s.handle(request('/accounts', { accounts: [
      { label: 'shared-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
    ], routes: [{ machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null }] }, 'PUT'), admin);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    const receipt = { state: 'failed', native_id: 'native-runtime', launcher_task_id: 'hub-' + l.id, confirmed_exit: true };
    await s.handle(request(path, { state: 'running', native_id: receipt.native_id, launcher_task_id: receipt.launcher_task_id }), host('hetzner'));
    const diagnostic = { reason: 'engine_exit_code', message: 'The running engine exited with code 1 (code=1, signal=none, wall_time_ms=2000000); no engine result was produced.', error_type: 'RuntimeError', stage: 'runtime', exit_code: 1, wall_time_ms: 2000000, tail: 'last log line' };
    expect((await s.handle(request(path, { ...receipt, diagnostic }), host('hetzner'))).status).toBe(200);
    const row: any = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', l.id).one();
    expect(JSON.parse(row.launch_error_json)).toEqual(diagnostic);
  }));
  it('rejects control chars, out-of-range signal and unknown keys', async () => fixture(async (s) => {
    await s.handle(request('/accounts', { accounts: [
      { label: 'shared-codex', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true },
    ], routes: [{ machine_id: 'hetzner', engine: 'codex', account: 'shared-codex', profile: null }] }, 'PUT'), admin);
    const l: any = await (await s.handle(request('/launches', intent({ machine: 'hetzner' })), admin)).json();
    await s.handle(request('/launches/' + l.id + '/claim', { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: iso(), account: 'shared-codex', profile_supported: true }), host('hetzner'));
    const path = '/launches/' + l.id + '/result';
    const receipt = { state: 'failed', native_id: 'native-runtime', launcher_task_id: 'hub-' + l.id, confirmed_exit: true };
    await s.handle(request(path, { state: 'running', native_id: receipt.native_id, launcher_task_id: receipt.launcher_task_id }), host('hetzner'));
    const base = { reason: 'engine_exit_code', message: 'ok', error_type: 'RuntimeError', stage: 'runtime' };
    expect((await s.handle(request(path, { ...receipt, diagnostic: { ...base, tail: 'bad\ntail' } }), host('hetzner'))).status).toBe(400);
    expect((await s.handle(request(path, { ...receipt, diagnostic: { ...base, signal: 999 } }), host('hetzner'))).status).toBe(400);
    expect((await s.handle(request(path, { ...receipt, diagnostic: { ...base, secret: 1 } }), host('hetzner'))).status).toBe(400);
  }));
});
