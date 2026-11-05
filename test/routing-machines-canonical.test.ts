/** Intake 0ea34d42 AC1: hub routing-machines lists every canonical registry
 * machine, including one that never advertised capabilities (offline during
 * setup). It appears with advertised:false, empty runners and its planned
 * engine installations instead of silently vanishing. Alias records
 * (re-enrollments of the same host) resolve to their canonical machine like
 * the fleet view. Quota-gated runners keep the 056a4131 reason display. */
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'admin', kind: 'human' }, scopes: ['admin'] };
const client = { caller: { actor: 'client-a', kind: 'agent' }, scopes: ['routing:submit'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host' }, scopes: ['host'] });
const now = Date.now();
const at = (delta = 0) => new Date(now + delta).toISOString();

const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'PUT') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });

async function fixture(fn: (r: RoutingService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('routing-canonical-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const f = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now); f.init();
    const r = new RoutingService(hub.sql, f, () => now, cb => state.storage.transactionSync(cb)); r.init();
    const sql = hub.sql;
    sql.exec(`CREATE TABLE IF NOT EXISTS engine_installations (machine_id TEXT NOT NULL, label TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(machine_id,label))`);
    // live-host advertises; silent-host never did; ghost-host is an old
    // re-enrollment of live-host's physical host (same hostname, stale).
    for (const [id, hostname, checkin] of [
      ['live-host', 'SHARED-HOST', at()],
      ['silent-host', 'LAPTOP-QUIET', null],
      ['ghost-host', 'SHARED-HOST', at(-600000)],
    ] as const) {
      sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?)', id, id, hostname, at(), checkin, JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80 }), JSON.stringify({ engines: [], fleet_modes: ['interactive'], workspaces: ['tasks'], cpu_count: 4 }));
    }
    sql.exec('INSERT INTO engine_installations VALUES (?, ?, ?)', 'silent-host', 'codex-orin', crypto.randomUUID());
    sql.exec('INSERT INTO engine_installations VALUES (?, ?, ?)', 'silent-host', 'shared-codex', crypto.randomUUID());
    expect((await r.handle(req('/api/routing/capabilities', { needs: [], max_agents: 2, runners: [{ engine: 'codex', profile: 'hub-codex-orin', account: 'codex-orin', model: 'gpt-6.1-sol', effort: 'high' }] }), host('live-host'))).status).toBe(200);
    await fn(r, sql);
  });
}

const machinesView = async (r: RoutingService, auth: any = admin) =>
  (await (await r.handle(req('/api/routing/machines'), auth)).json() as any).machines;

describe('routing-machines canonical visibility (intake 0ea34d42)', () => {
  it('lists never-advertised canonical machines with planned installations and hides aliases', async () => {
    await fixture(async (r) => {
      const machines = await machinesView(r);
      expect(machines.map((m: any) => m.id).sort()).toEqual(['live-host', 'silent-host']);
      const silent = machines.find((m: any) => m.id === 'silent-host');
      expect(silent.advertised).toBe(false);
      expect(silent.runners).toEqual([]);
      expect(silent.needs).toEqual([]);
      expect(silent.max_agents).toBeNull();
      expect(silent.installations.sort()).toEqual(['codex-orin', 'shared-codex']);
      expect(silent.online).toBe(false);
      expect(silent.resources.mem_avail_mb).toBe(32000);
      const live = machines.find((m: any) => m.id === 'live-host');
      expect(live.advertised).toBe(true);
      expect(live.runners.map((rn: any) => rn.account)).toEqual(['codex-orin']);
      expect(live.runners[0].profile).toBe('hub-codex-orin');
      expect(live.online).toBe(true);
    });
  });

  it('keeps the client caller allowed and the host caller forbidden (unchanged auth)', async () => {
    await fixture(async (r) => {
      expect((await machinesView(r, client)).length).toBe(2);
      const refused = await r.handle(req('/api/routing/machines'), host('live-host'));
      expect(refused.status).toBe(403);
    });
  });
});
