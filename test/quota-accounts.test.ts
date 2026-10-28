import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import {
  QuotaStore,
  canonicalAccountLabel,
  canonicalHarness,
  canonicalQuotaProvider,
  canonicalQuotaProviders,
  dedupeAccountsByCanonical,
  fleetReadGrant,
  legacyRouteKeyFor,
  parseFleetQuotaFilters,
  snapshotRouteForAccount,
} from '../src/quota';

const ts = (deltaMs = 0) => new Date(Date.now() + deltaMs).toISOString();
function route(pct: number, at: string) {
  return {
    status: 'ok', error: null, observed_at: at,
    details: { limit_reached: false },
    windows: { '7d': { percent_remaining: pct, reset_at: ts(86400000), present: true, rolling: false, error: null } },
  };
}

describe('canonical harness/provider/account mapping (codex-orin, go, zai)', () => {
  it('canonicalizes legacy route keys, engines and account labels', () => {
    // codex-orin is the second Codex account, never a separate provider.
    expect(canonicalQuotaProvider('codex-orin')).toBe('codex');
    expect(canonicalQuotaProvider('codex')).toBe('codex');
    expect(canonicalQuotaProvider('zai')).toBe('zai');
    expect(canonicalQuotaProvider('go')).toBe('go');
    expect(legacyRouteKeyFor('codex')).toBe('codex-orin');
    expect(legacyRouteKeyFor('zai')).toBeNull();
    // Canonical provider choices never list the legacy route key.
    expect(canonicalQuotaProviders()).not.toContain('codex-orin');
    expect(canonicalQuotaProviders()).toEqual(expect.arrayContaining(['codex', 'zai', 'go']));
    // The native zcodex binary is a Codex harness for the Z.ai provider.
    expect(canonicalHarness('zcodex')).toBe('codex');
    expect(canonicalHarness('codex')).toBe('codex');
    expect(canonicalHarness('opencode')).toBe('opencode');
    // The Go pool account must not imply muse-only.
    expect(canonicalAccountLabel('urgent-muse')).toBe('shared-go');
    expect(canonicalAccountLabel('shared-go')).toBe('shared-go');
    expect(canonicalAccountLabel('codex-orin')).toBe('codex-orin');
  });

  it('routes host snapshot routes to the right account', () => {
    const snap = { codex: { marker: 'shared' }, 'codex-orin': { marker: 'orin' }, go: { marker: 'go' } };
    // The label-matching second-Codex-home route wins for the orin account.
    expect(snapshotRouteForAccount('codex-orin', 'codex', snap)).toMatchObject({ marker: 'orin' });
    // Legacy registry rows (provider `codex-orin`) resolve the same way.
    expect(snapshotRouteForAccount('codex-orin', 'codex-orin', snap)).toMatchObject({ marker: 'orin' });
    expect(snapshotRouteForAccount('shared-codex', 'codex', snap)).toMatchObject({ marker: 'shared' });
    expect(snapshotRouteForAccount('shared-go', 'go', snap)).toMatchObject({ marker: 'go' });
    // A distinct quota home never borrows the shared provider route: a host
    // with only the shared Codex home backs shared-codex, never orin.
    expect(snapshotRouteForAccount('codex-orin', 'codex', { codex: { marker: 'shared' } })).toBeNull();
    expect(snapshotRouteForAccount('other', 'grok', snap)).toBeNull();
    expect(snapshotRouteForAccount('shared-codex', 'codex', null)).toBeNull();
  });

  it('grants host credentials the fleet quota read; keeps strangers out', () => {
    expect(fleetReadGrant({ kind: 'host' }, ['host'])).toBe(true);
    expect(fleetReadGrant({ kind: 'agent' }, ['fleet:launch'])).toBe(true);
    expect(fleetReadGrant({ kind: 'routing-client' }, ['routing:submit'])).toBe(true);
    expect(fleetReadGrant({ kind: 'agent', isAdmin: false }, ['bus:send'])).toBe(false);
    expect(fleetReadGrant({ kind: 'host' }, ['bus:send'])).toBe(false);
    expect(fleetReadGrant(undefined, [])).toBe(false);
  });

  it('accepts legacy provider/account filters by canonicalizing them', () => {
    const view = (qs: string) => parseFleetQuotaFilters(new URL(`http://example.com/api/quota?view=fleet${qs}`));
    expect(view('&provider=codex-orin')).toMatchObject({ ok: true, filters: { provider: 'codex' } });
    expect(view('&account=urgent-muse')).toMatchObject({ ok: true, filters: { account: 'shared-go' } });
    expect(view('&provider=codex')).toMatchObject({ ok: true, filters: { provider: 'codex' } });
    expect(view('&provider=nope')).toMatchObject({ ok: false, error: 'invalid_provider' });
  });

  it('fleet view canonicalizes stored rows keyed by old names; groups two codex accounts', async () => {    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      const now = Date.now();
      hub.fleetService.init();
      const store = new QuotaStore(hub.sql, undefined, {} as any, () => now);
      const at = ts(-1000);
      // Legacy registry rows: provider `codex-orin`, muse-only `urgent-muse` label.
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',4,15,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('codex-orin','codex-orin',4,15,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('urgent-muse','go',1,0,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-zai','zai',2,0,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','codex','shared-codex','hub-shared-codex')");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','codex','codex-orin','hub-codex-orin')");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','opencode','urgent-muse',NULL)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','zcodex','shared-zai',NULL)");
      // A stale server row under the legacy label must not duplicate the
      // live canonical row: one pool per canonical identity.
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES (?,?,?,?)', 'urgent-muse', 'go', ts(-2000000), JSON.stringify(route(10, ts(-2000000))));
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES (?,?,?,?)', 'shared-go', 'go', at, JSON.stringify(route(83, at)));
      // Old host subscription reports (legacy service + legacy label).
      for (const [account, service, pct] of [['codex-orin', 'codex-orin', 72], ['urgent-muse', 'go', 61], ['shared-zai', 'zai', 50]] as const) {
        hub.sql.exec('INSERT INTO fleet_subscriptions VALUES (?,?,?,?,?,?)', 'm1', account, service, null, at, JSON.stringify(route(pct, at)));
      }
      const body = store.getFleetView({ host_id: null, provider: null, engine: null, account: null }, now).body as any;
      const byAccount = Object.fromEntries(body.pools.map((p: any) => [p.account, p]));
      // Grouping by provider shows two codex accounts.
      expect(body.pools.filter((p: any) => p.provider === 'codex').map((p: any) => p.account).sort()).toEqual(['codex-orin', 'shared-codex']);
      expect(body.pools.some((p: any) => p.provider === 'codex-orin')).toBe(false);
      // Every row exposes the harness alongside provider/account.
      expect(byAccount['codex-orin']).toMatchObject({ provider: 'codex', state: 'fresh', remaining_percent: 72 });
      expect(byAccount['codex-orin'].harness).toEqual(['codex']);
      // Exactly one Go pool: the fresh canonical server row wins over both
      // the stale legacy server row and the per-host subscription.
      const goPools = body.pools.filter((p: any) => p.provider === 'go');
      expect(goPools.map((p: any) => p.account)).toEqual(['shared-go']);
      expect(goPools[0]).toMatchObject({ state: 'fresh', source: 'server', remaining_percent: 83 });
      expect(goPools[0].harness).toEqual(['opencode']);
      expect(byAccount['shared-zai'].harness).toEqual(['codex']);
      // The accounts table reads canonical too: legacy registry labels never
      // surface in the fleet table.
      const goStatus = hub.fleetService.accountStatus({ label: 'urgent-muse', provider: 'go', max_concurrency: 1, reserve_percent: 5, enabled: 1 });
      expect(goStatus.label).toBe('shared-go');
      expect(goStatus.provider).toBe('go');
      // Legacy provider filter selects both Codex accounts.
      const filtered = store.getFleetView({ host_id: null, provider: 'codex', engine: null, account: null }, now).body as any;
      expect(filtered.pools.every((p: any) => p.provider === 'codex')).toBe(true);
      expect(filtered.pools.length).toBeGreaterThanOrEqual(2);
      expect(body.canonical).toMatchObject({ providers: { 'codex-orin': 'codex' }, engines: { zcodex: 'codex' }, accounts: { 'urgent-muse': 'shared-go' } });
    });
  });

  it('dedupes registry rows by canonical identity during a label rename', () => {
    // A rename transition can briefly hold both rows: prefer canonical.
    expect(dedupeAccountsByCanonical([
      { label: 'urgent-muse', provider: 'go' },
      { label: 'shared-go', provider: 'go' },
    ])).toEqual([{ label: 'shared-go', provider: 'go' }]);
    // Distinct pools are untouched.
    expect(dedupeAccountsByCanonical([
      { label: 'shared-codex', provider: 'codex' },
      { label: 'codex-orin', provider: 'codex-orin' },
    ]).map((r) => r.label).sort()).toEqual(['codex-orin', 'shared-codex']);
  });

  it('server snapshots and subscription reports accept legacy names and store canonical rows', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      hub.fleetService.init();
      hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('m1', 'm1', 'm1.local', ?)", ts(-7200000));
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('codex-orin','codex',4,15,1)");
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-go','go',1,0,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','codex','codex-orin','hub-codex-orin')");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','opencode','shared-go',NULL)");
      const host = { caller: { kind: 'host', machineId: 'm1' }, scopes: ['host'] };
      const at = ts(-1000);
      const sub = (service: string, account: string, pct: number) => ({
        service, account, plan: null,
        quota: { status: 'ok', observed_at: at, details: { limit_reached: false },
          windows: { weekly: { present: true, percent_remaining: pct, reset_at: ts(86400000) } } },
      });
      // Legacy host reports map onto the canonical registry in either update order.
      const legacy = await hub.fleetService.handle(new Request('http://example.com/api/fleet/subscriptions', {
        method: 'PUT', body: JSON.stringify({ subscriptions: [sub('codex-orin', 'codex-orin', 72), sub('go', 'urgent-muse', 61)] }),
      }), host);
      expect(legacy.status).toBe(200);
      const rows = hub.sql.exec('SELECT account, service FROM fleet_subscriptions WHERE machine_id = ? ORDER BY account', 'm1').toArray() as any[];
      expect(rows).toEqual([
        { account: 'codex-orin', service: 'codex' },
        { account: 'shared-go', service: 'go' },
      ]);
      // A contradictory double report for one canonical pool fails closed.
      const dup = await hub.fleetService.handle(new Request('http://example.com/api/fleet/subscriptions', {
        method: 'PUT', body: JSON.stringify({ subscriptions: [sub('go', 'urgent-muse', 61), sub('go', 'shared-go', 62)] }),
      }), host);
      expect(dup.status).toBe(400);
    });
  });

  it('one broken host probe does not hold the pool when another host reports a known 7d window', async () => {
    const e = env as unknown as Env;
    await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub) => {
      const now = Date.now();
      hub.fleetService.init();
      const store = new QuotaStore(hub.sql, undefined, {} as any, () => now);
      const at = ts(-1000);
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex','codex',4,0,1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m1','codex','shared-codex','hub-shared-codex')");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('m2','codex','shared-codex','hub-shared-codex')");
      const healthy = {
        status: 'ok', observed_at: at, details: { limit_reached: false },
        windows: {
          '5h': { present: false, percent_remaining: null, reset_at: null, rolling: false, error: null },
          '7d': { present: true, percent_remaining: 80, reset_at: ts(86400000), rolling: false, error: null },
          monthly: { present: false, percent_remaining: null, reset_at: null, rolling: false, error: null },
        },
      };
      const broken = { status: 'unknown', observed_at: at, details: { limit_reached: null }, windows: {} };
      hub.sql.exec('INSERT INTO fleet_subscriptions VALUES (?,?,?,?,?,?)', 'm1', 'shared-codex', 'codex', null, at, JSON.stringify(healthy));
      hub.sql.exec('INSERT INTO fleet_subscriptions VALUES (?,?,?,?,?,?)', 'm2', 'shared-codex', 'codex', null, at, JSON.stringify(broken));
      const body = store.getFleetView({ host_id: null, provider: null, engine: null, account: null }, now).body as any;
      const pool = body.pools.find((p: any) => p.account === 'shared-codex');
      expect(pool.state).toBe('fresh');
      expect(pool.remaining_percent).toBe(80);
      expect(pool.windows['7d']).toMatchObject({ present: true, percent_remaining: 80 });
      // The healthy observation backs the display even though a sibling probe failed.
      expect(pool.observed_by_hosts.sort()).toEqual(['m1', 'm2']);
      // Account health agrees: one healthy route is enough for ok.
      const status = hub.fleetService.accountStatus({ label: 'shared-codex', provider: 'codex', enabled: 1, reserve_percent: 0, max_concurrency: 4 });
      expect(status.status).toBe('ok');
      expect(status.remaining_percent).toBe(80);
    });
  });
});
