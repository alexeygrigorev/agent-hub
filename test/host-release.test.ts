import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { RoutingService } from '../src/routing';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake a3227276: Hub-pinned host releases for checkout-installed hosts.
const now = Date.now();
const at = () => new Date(now).toISOString();
const admin = { caller: { isAdmin: true, actor: 'admin', kind: 'human' }, scopes: ['admin'] };
const client = { caller: { actor: 'client-a', kind: 'agent' }, scopes: ['routing:submit'] };
const host = (machineId: string) => ({ caller: { machineId, kind: 'host' }, scopes: ['host'] });
const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const SHA = 'a'.repeat(40);
const SHA2 = 'b'.repeat(40);

async function fixture(fn: (r: RoutingService) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('host-release-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now); f.init();
    const r = new RoutingService(hub.sql, f, () => now, cb => state.storage.transactionSync(cb)); r.init();
    for (const machine of ['tabo', 'hetzner']) {
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', machine, machine, machine, at(), at(), '{}', JSON.stringify({ engines: [], fleet_modes: [], workspaces: [] }));
      expect((await r.handle(req('/api/routing/capabilities', { needs: [], max_agents: 2, runners: [] }, 'PUT'), host(machine))).status).toBe(200);
    }
    await fn(r);
  });
}

describe('Hub-pinned host release', () => {
  it('lets only an operator pin a full SHA for a known machine; each host reads only its own pin', () => fixture(async r => {
    expect((await r.handle(req('/api/routing/host-release/tabo', { sha: SHA }, 'PUT'), client)).status).toBe(403);
    expect((await r.handle(req('/api/routing/host-release/tabo', { sha: SHA }, 'PUT'), host('tabo'))).status).toBe(403);
    expect((await r.handle(req('/api/routing/host-release/tabo', { sha: 'abc' }, 'PUT'), admin)).status).toBe(400);
    expect((await r.handle(req('/api/routing/host-release/tabo', { sha: SHA, extra: 1 }, 'PUT'), admin)).status).toBe(400);
    expect((await r.handle(req('/api/routing/host-release/nope', { sha: SHA }, 'PUT'), admin)).status).toBe(404);
    const set = await r.handle(req('/api/routing/host-release/tabo', { sha: SHA.toUpperCase() }, 'PUT'), admin);
    expect(set.status).toBe(200); expect(await set.json()).toEqual({ machine_id: 'tabo', sha: SHA });
    expect(await (await r.handle(req('/api/routing/host-release'), host('tabo'))).json()).toEqual({ sha: SHA });
    expect(await (await r.handle(req('/api/routing/host-release'), host('hetzner'))).json()).toEqual({ sha: null });
    expect((await r.handle(req('/api/routing/host-release'), client)).status).toBe(403);
    await r.handle(req('/api/routing/host-release/tabo', { sha: null }, 'PUT'), admin);
    expect(await (await r.handle(req('/api/routing/host-release'), host('tabo'))).json()).toEqual({ sha: null });
  }));

  it('records bounded host reports and shows desired vs current in routing machines', () => fixture(async r => {
    await r.handle(req('/api/routing/host-release/tabo', { sha: SHA2 }, 'PUT'), admin);
    expect((await r.handle(req('/api/routing/host-release/report', { current_sha: SHA, state: 'refused', error: 'dirty_checkout' }), host('tabo'))).status).toBe(200);
    for (const bad of [{ current_sha: SHA, state: 'pwned', error: null }, { current_sha: 'x', state: 'current', error: null }, { current_sha: SHA, state: 'failed', error: 'Free text with spaces' }, { current_sha: SHA, state: 'current', error: null, more: 1 }])
      expect((await r.handle(req('/api/routing/host-release/report', bad), host('tabo'))).status).toBe(400);
    expect((await r.handle(req('/api/routing/host-release/report', { current_sha: SHA, state: 'current', error: null }), admin)).status).toBe(403);
    // A later operator pin keeps the last host report.
    await r.handle(req('/api/routing/host-release/tabo', { sha: SHA }, 'PUT'), admin);
    const machines: any = await (await r.handle(req('/api/routing/machines'), admin)).json();
    const tabo = machines.machines.find((m: any) => m.id === 'tabo');
    expect(tabo.host_release).toMatchObject({ desired_sha: SHA, current_sha: SHA, state: 'refused', error: 'dirty_checkout' });
    expect(machines.machines.find((m: any) => m.id === 'hetzner').host_release).toBeNull();
  }));
});
