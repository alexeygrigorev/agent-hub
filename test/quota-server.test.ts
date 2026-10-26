import { beforeEach, describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { defaultEnv, doFetch } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';

const stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
let cookie: string;
const csrf = 'quota-server-test';
const timestamp = (delta = 0) => new Date(Date.now() + delta).toISOString();
function observation(percent = 70) {
  return { status: 'ok', error: null, observed_at: timestamp(-100), details: { limit_reached: false },
    windows: { '7d': { present: true, percent_remaining: percent, reset_at: timestamp(86400000), rolling: false, error: null } } };
}
function post(accounts: any[], extraHeaders: Record<string, string> = {}, at = timestamp(-50)) {
  return doFetch(new Request('http://example.com/api/quota/server', { method: 'POST',
    headers: { Cookie: cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...extraHeaders },
    body: JSON.stringify({ captured_at: at, accounts }) }));
}
async function view() {
  return (await (await doFetch(new Request('http://example.com/api/quota?view=fleet', { headers: { Cookie: cookie } }))).json()) as any;
}
beforeEach(async () => {
  cookie = (await createSessionCookie(defaultEnv, { sub: 'quota-server-admin', email: 'admin@fake.local',
    isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await runInDurableObject(stub, (hub: Hub) => {
    hub.sql.exec('DELETE FROM server_quota_snapshots');
    hub.sql.exec('DELETE FROM fleet_launches');
    hub.sql.exec('DELETE FROM fleet_routes');
    hub.sql.exec('DELETE FROM fleet_accounts');
  });
});

describe('Hub server account quota', () => {
  it('fresh account quota and routing do not depend on any machine report', async () => {
    // The second Codex home posts under its legacy wire route key; the pool
    // surfaces as canonical provider `codex` with its own account.
    expect((await post([{ account: 'main', provider: 'codex', quota: observation(62) },
      { account: 'orin', provider: 'codex-orin', quota: observation(99) }])).status).toBe(200);
    const q = await view();
    expect(q.pools.find((p: any) => p.account === 'main')).toMatchObject({ provider: 'codex', state: 'fresh', source: 'server', remaining_percent: 62 });
    expect(q.pools.find((p: any) => p.account === 'orin')).toMatchObject({ provider: 'codex', state: 'fresh', source: 'server', remaining_percent: 99 });
    expect(q.pools.some((p: any) => p.provider === 'codex-orin')).toBe(false);
    expect(q.account_coverage).toMatchObject({ total: 2, fresh: ['main', 'orin'], unavailable: [] });
    await runInDurableObject(stub, (hub: Hub) => {
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('main', 'codex', 4, 15, 1)");
      // Deliberately no machine quota or subscription rows.
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('offline-machine', 'codex', 'main', NULL)");
      expect(hub.fleetService.accountStatus({ label: 'main', provider: 'codex', enabled: 1, reserve_percent: 15, max_concurrency: 4 })).toMatchObject({ status: 'ok', remaining_percent: 62 });
    });
  });

  it('adds account definitions while retaining live launches and existing account policy', async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('main', 'codex', 4, 15, 1)");
      hub.sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at) VALUES ('live', 'actor', 'agent', 'running', 'key', '{}', 'machine', 'codex', 'main', 'headless', 'tasks', 'work', 'running', ?, ?)", timestamp(), timestamp());
      const accounts = [{ label: 'main', provider: 'codex', max_concurrency: 4, reserve_percent: 15, enabled: true },
        { label: 'orin', provider: 'codex-orin', max_concurrency: 4, reserve_percent: 15, enabled: true }];
      const update = (data: any) => hub.fleetService.handle(new Request('http://example.com/api/fleet/accounts', {
        method: 'PUT', body: JSON.stringify({ accounts: data, routes: [] }) }), { caller: { isAdmin: true }, scopes: ['admin'] });
      expect((await update(accounts)).status).toBe(200);
      expect(hub.sql.exec("SELECT state FROM fleet_launches WHERE id = 'live'").one().state).toBe('running');
      // A pure max_concurrency raise widens the pool under live launches.
      expect((await update([{ ...accounts[0], max_concurrency: 5 }, accounts[1]])).status).toBe(200);
      expect(hub.sql.exec("SELECT max_concurrency FROM fleet_accounts WHERE label = 'main'").one().max_concurrency).toBe(5);
      // A decrease still holds the registry.
      expect((await update([{ ...accounts[0], max_concurrency: 4 }, accounts[1]])).status).toBe(409);
      expect(hub.sql.exec("SELECT max_concurrency FROM fleet_accounts WHERE label = 'main'").one().max_concurrency).toBe(5);
    });
  });

  it('requires an admin session and CSRF; rejects unknown/private fields atomically', async () => {
    const row = { account: 'main', provider: 'codex', quota: observation() };
    expect((await post([row], { Cookie: '' })).status).toBe(403);
    expect((await post([row], { 'X-CSRF-Token': '' })).status).toBe(403);
    expect((await post([row, { account: 'bad', provider: 'go', quota: { ...observation(), token: 'private' } }])).status).toBe(400);
    expect((await view()).pools).toHaveLength(0);
    expect((await post([row, row])).status).toBe(400);
  });

  it('does not turn a failed probe into fresh capacity and rejects replays/future observations', async () => {
    const at = timestamp(-100);
    expect((await post([{ account: 'go', provider: 'go', quota: { status: 'error', error: 'unavailable', observed_at: null,
      details: { limit_reached: null }, windows: {} } }], {}, at)).status).toBe(200);
    expect((await view()).pools[0]).toMatchObject({ state: 'unavailable', source: 'server', remaining_percent: null });
    expect((await post([{ account: 'go', provider: 'go', quota: { ...observation(), observed_at: at } }], {}, at)).status).toBe(409);
    expect((await post([{ account: 'future', provider: 'go', quota: observation() }], {}, timestamp(60000))).status).toBe(400);
  });

  it('prefers server failures over apparently healthy host data; expires stale server evidence', async () => {
    await runInDurableObject(stub, (hub: Hub) => {
      const at = timestamp(-100);
      const bad = { status: 'error', error: 'unavailable', observed_at: at, details: { limit_reached: null }, windows: {} };
      hub.sql.exec("INSERT INTO fleet_accounts VALUES ('main', 'codex', 4, 15, 1)");
      hub.sql.exec("INSERT INTO fleet_routes VALUES ('machine', 'codex', 'main', NULL)");
      hub.sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', 'machine', 'main', 'codex', null, at, JSON.stringify(observation(95)));
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES (?, ?, ?, ?)', 'main', 'codex', at, JSON.stringify(bad));
      expect(hub.fleetService.accountStatus({ label: 'main', provider: 'codex', enabled: 1, reserve_percent: 15, max_concurrency: 4 }).status).toBe('unknown');
      hub.sql.exec('UPDATE server_quota_snapshots SET captured_at = ?', timestamp(-1200001));
      expect(hub.fleetService.accountStatus({ label: 'main', provider: 'codex', enabled: 1, reserve_percent: 15, max_concurrency: 4 }).remaining_percent).toBe(95);
    });
  });
});
