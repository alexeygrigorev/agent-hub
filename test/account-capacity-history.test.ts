import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { readAccountCapacityHistory, sampleAccountCapacityHistory } from '../src/account-capacity-history';
import { FleetService } from '../src/fleet';

it('records bounded rolling account-capacity observations and reads a half-open time window', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await runInDurableObject(stub, (hub: any) => {
    const sql = hub.sql;
    const account = { label: 'history-account', active: 7, reserved: 5, native_live: 4, max_concurrency: 13 };
    const start = Date.UTC(2026, 10, 1, 4, 20);
    expect(sampleAccountCapacityHistory(sql, start, () => [account])).toBe(1);
    expect(sampleAccountCapacityHistory(sql, start + 14 * 60_000, () => {
      throw new Error('sample interval should avoid recomputing account status');
    })).toBe(0);
    expect(sampleAccountCapacityHistory(sql, start + 15 * 60_000, () => [
      { ...account, active: 8, reserved: 6, native_live: null },
    ])).toBe(1);

    const window = readAccountCapacityHistory(sql, 'history-account', start, start + 30 * 60_000, 1);
    expect(window).toEqual({
      rows: [{ observed_at: new Date(start).toISOString(), active: 7, reserved: 5, native_live: 4, limit: 13 }],
      next_cursor: start,
    });
    const next = readAccountCapacityHistory(sql, 'history-account', start, start + 30 * 60_000, 100, window.next_cursor);
    expect(next.rows).toEqual([{ observed_at: new Date(start + 15 * 60_000).toISOString(), active: 8, reserved: 6, native_live: null, limit: 13 }]);
    expect(next.next_cursor).toBeNull();
    expect(() => readAccountCapacityHistory(sql, 'history-account', start, start + 32 * 24 * 60 * 60_000, 100)).toThrow('invalid_capacity_history_query');
  });
});

it('serves capacity history through the bounded admin-only account API', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: any, state: DurableObjectState) => {
    const start = Date.UTC(2026, 10, 1, 4, 20);
    const service = new FleetService(hub.sql, (fn: () => unknown) => state.storage.transactionSync(fn), () => start);
    service.init();
    hub.sql.exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('history-account','claude',13,0,1)");
    sampleAccountCapacityHistory(hub.sql, start, () => [
      { label: 'history-account', active: 7, reserved: 5, native_live: 4, max_concurrency: 13 },
    ]);
    const query = `?from=${encodeURIComponent(new Date(start).toISOString())}&to=${encodeURIComponent(new Date(start + 60_000).toISOString())}&limit=1`;
    const path = `/api/fleet/accounts/history-account/capacity-history${query}`;
    const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
    const agent = { caller: { actor: 'test-agent', kind: 'agent' }, scopes: ['fleet:launch'] };
    const allowed = await service.handle(new Request(`https://hub.test${path}`), admin as any);
    expect(allowed.status).toBe(200);
    expect(await allowed.json()).toMatchObject({
      account: 'history-account',
      rows: [{ active: 7, reserved: 5, native_live: 4, limit: 13 }],
    });
    const forbidden = await service.handle(new Request(`https://hub.test${path}`), agent as any);
    expect(forbidden.status).toBe(403);
  });
});
