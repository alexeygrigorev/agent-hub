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

  // Intake 798f0f8c: the Tabo tablet serves only managed Codex bundles and
  // never posts a host-wide quse snapshot, yet reports every routed account
  // each fleet cycle. Those reports are machine quota coverage.
  const seedTwoMachines = (hub: Hub, freshAt: string) => {
    hub.fleetService.init();
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('rmthz', 'urgent-hetzner', 'RMTHZ', ?, ?)", ts(-86400000), freshAt);
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('tabo', 'tabo', 'Tabo', ?, ?)", ts(-86400000), freshAt);
    hub.sql.exec(`INSERT INTO host_quota_snapshots
      (host_id, schema_version, collector_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json)
      VALUES ('rmthz', '1.1', 'quota_snapshot/1.1', ?, ?, ?, ?, 'ok', NULL, '{}')`,
      freshAt, Date.parse(freshAt), freshAt, Date.parse(freshAt));
    hub.sql.exec("INSERT OR REPLACE INTO fleet_accounts VALUES ('shared-codex', 'codex', 4, 0, 1)");
    hub.sql.exec("INSERT OR REPLACE INTO fleet_accounts VALUES ('codex-orin', 'codex', 4, 0, 1)");
    for (const machine of ['rmthz', 'tabo']) {
      hub.sql.exec("INSERT INTO fleet_routes VALUES (?, 'codex', 'shared-codex', 'hub-shared-codex')", machine);
      hub.sql.exec("INSERT INTO fleet_routes VALUES (?, 'codex', 'codex-orin', 'hub-codex-orin')", machine);
    }
  };
  const report = (hub: Hub, machine: string, account: string, at: string) => {
    const quota = { status: 'ok', observed_at: at, details: { limit_reached: false },
      windows: { '7d': { present: true, percent_remaining: 41, reset_at: ts(86400000 * 5), rolling: false, error: null } } };
    hub.sql.exec("INSERT OR REPLACE INTO fleet_subscriptions VALUES (?, ?, 'codex', NULL, ?, ?)", machine, account, at, JSON.stringify(quota));
  };
  const view = (hub: Hub, filters: Record<string, string | null> = {}) => {
    const now = Date.now();
    const store = new QuotaStore(hub.sql, undefined, {} as any, () => now);
    return store.getFleetView({ host_id: null, provider: null, engine: null, account: null, ...filters } as any, now).body as any;
  };

  it('counts a machine without a host snapshot as covered when every routed account reports fresh', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      const freshAt = ts(-1000);
      seedTwoMachines(hub, freshAt);
      report(hub, 'tabo', 'shared-codex', freshAt);
      report(hub, 'tabo', 'codex-orin', freshAt);
      const body = view(hub);
      expect(body.coverage).toMatchObject({ machines_total: 2, fresh: ['rmthz', 'tabo'], stale: [], missing: [], route_reports: ['tabo'] });
      // Account-filtered view still counts it (only the filtered account must report).
      expect(view(hub, { account: 'codex-orin' }).coverage).toMatchObject({ fresh: ['rmthz', 'tabo'], missing: [] });
    });
  });

  it('keeps partial route reports stale and no reports missing', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      const freshAt = ts(-1000);
      seedTwoMachines(hub, freshAt);
      expect(view(hub).coverage).toMatchObject({ fresh: ['rmthz'], stale: [], missing: ['tabo'], route_reports: [] });
      report(hub, 'tabo', 'shared-codex', freshAt);
      expect(view(hub).coverage).toMatchObject({ fresh: ['rmthz'], stale: ['tabo'], missing: [] });
      // An old report is not coverage.
      report(hub, 'tabo', 'codex-orin', ts(-1_300_000));
      expect(view(hub).coverage).toMatchObject({ fresh: ['rmthz'], stale: ['tabo'], missing: [] });
      hub.sql.exec("DELETE FROM fleet_subscriptions WHERE machine_id = 'tabo' AND account = 'shared-codex'");
      expect(view(hub).coverage).toMatchObject({ fresh: ['rmthz'], missing: ['tabo'] });
    });
  });

  it('never counts a machine with no routes through route reports', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      const freshAt = ts(-1000);
      seedTwoMachines(hub, freshAt);
      hub.sql.exec("DELETE FROM fleet_routes WHERE machine_id = 'tabo'");
      report(hub, 'tabo', 'shared-codex', freshAt);
      expect(view(hub).coverage).toMatchObject({ fresh: ['rmthz'], missing: ['tabo'], stale: [], route_reports: [] });
    });
  });
});
