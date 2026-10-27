import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
import { QuotaStore } from '../src/quota';
const ts = (deltaMs = 0) => new Date(Date.now() + deltaMs).toISOString();

describe('quota machine coverage counts canonical machines only', () => {
  it('excludes a stale alias registration from coverage, hosts and totals', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      hub.fleetService.init();
      const freshAt = ts(-1000);
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('live-host', 'urgent-hetzner', 'RMTHZ', ?, ?)",
        ts(-86400000), freshAt);
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('stale-alias', 'hetzner', 'rmthz', ?, ?)",
        ts(-86400000 * 30), ts(-86400000 * 11));
      const routes = JSON.stringify({});
      hub.sql.exec(`INSERT INTO host_quota_snapshots
        (host_id, schema_version, collector_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json)
        VALUES ('live-host', '1.1', 'quota_snapshot/1.1', ?, ?, ?, ?, 'ok', NULL, ?)`,
        freshAt, Date.parse(freshAt), freshAt, Date.parse(freshAt), routes);
      // Clock reads after every insert: a snapshot timestamped after the
      // observation instant reads stale (negative age), never fresh.
      const now = Date.now();
      const store = new QuotaStore(hub.sql, undefined, {} as any, () => now);
      const body = store.getFleetView({ host_id: null, provider: null, engine: null, account: null }, now).body as any;
      expect(body.coverage).toMatchObject({ machines_total: 1, fresh: ['live-host'], stale: [], missing: [] });
      expect(body.hosts.map((h: any) => h.host_id)).toEqual(['live-host']);
    });
  });

  it('resolves a host_id filter naming an alias to its canonical machine', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      hub.fleetService.init();
      const freshAt = ts(-1000);
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('live-host', 'urgent-hetzner', 'RMTHZ', ?, ?)",
        ts(-86400000), freshAt);
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('stale-alias', 'hetzner', 'rmthz', ?, ?)",
        ts(-86400000 * 30), ts(-86400000 * 11));
      const routes = JSON.stringify({});
      hub.sql.exec(`INSERT INTO host_quota_snapshots
        (host_id, schema_version, collector_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json)
        VALUES ('live-host', '1.1', 'quota_snapshot/1.1', ?, ?, ?, ?, 'ok', NULL, ?)`,
        freshAt, Date.parse(freshAt), freshAt, Date.parse(freshAt), routes);
      const now = Date.now();
      const store = new QuotaStore(hub.sql, undefined, {} as any, () => now);
      const filtered = store.getFleetView({ host_id: 'stale-alias', provider: null, engine: null, account: null }, now).body as any;
      expect(filtered.coverage).toMatchObject({ machines_total: 1, fresh: ['live-host'] });
      expect(filtered.hosts.map((h: any) => h.host_id)).toEqual(['live-host']);
    });
  });
});
