import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
import { QuotaStore } from '../src/quota';

const ts = (deltaMs = 0) => new Date(Date.now() + deltaMs).toISOString();

describe('fleet quota view is Hub-server only (intake 58ebeb9f)', () => {
  it('does not expose or use host snapshots or per-machine subscription reports', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      hub.fleetService.init();
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex', 'codex', 4, 0, 1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('machine', 'codex', 'shared-codex', 'hub-shared-codex')");
      const observed = ts(-1000);
      const quota = { status: 'ok', observed_at: observed, details: { limit_reached: false },
        windows: { '7d': { present: true, percent_remaining: 95, reset_at: ts(86400000) } } };
      hub.sql.exec(`INSERT INTO host_quota_snapshots
        (host_id, schema_version, collector_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json)
        VALUES ('machine', '1.1', 'test', ?, ?, ?, ?, 'ok', NULL, ?)`,
        observed, Date.parse(observed), observed, Date.parse(observed), JSON.stringify({ codex: quota }));
      hub.sql.exec("INSERT INTO fleet_subscriptions VALUES ('machine', 'shared-codex', 'codex', NULL, ?, ?)", observed, JSON.stringify(quota));

      const now = Date.now();
      const body = new QuotaStore(hub.sql, undefined, {} as any, () => now)
        .getFleetView({ host_id: null, provider: null, engine: null, account: null }, now).body as any;
      expect(body.pools).toHaveLength(1);
      expect(body.pools[0]).toMatchObject({ state: 'unknown', source: 'server', remaining_percent: null, observed_by_hosts: [] });
      expect(body.hosts).toBeUndefined();
      expect(body.coverage).toBeUndefined();
    });
  });

});
