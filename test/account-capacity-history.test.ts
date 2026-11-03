import { fixtureStatements,registerFixtureStatements } from './helpers/fixture-statements';
import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ACCOUNT_CAPACITY_HISTORY_MAX_ROWS, readAccountCapacityHistory, sampleAccountCapacityHistory } from '../src/account-capacity-history';
import { RowsMeter } from '../src/rows-meter';
import { FleetService } from '../src/fleet';

const inDO: any = runInDurableObject;

it('records bounded rolling account-capacity observations and reads a half-open time window', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, (hub: any) => {
    registerFixtureStatements(hub);
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

it('refuses an incomplete retained population without deleting any Hub data', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, (hub: any) => {
    registerFixtureStatements(hub);
    const sql = hub.sql;
    const start = Date.UTC(2026, 10, 1, 4, 20);
    sampleAccountCapacityHistory(sql, start, () => []);
    for (let i = 0; i < ACCOUNT_CAPACITY_HISTORY_MAX_ROWS; i++) {
      fixtureStatements(sql).exec(`INSERT INTO fleet_account_capacity_history
        (account, observed_at_ms, active, reserved, native_live, concurrency_limit)
        VALUES ('capped-account', ?, 1, 1, 1, 4)`, start + i * 15 * 60_000);
    }
    const statements: string[] = [];
    const meter=new RowsMeter();
    const measured=meter.wrap(sql);
    const observedSql = new Proxy(measured, {
      get(target, key) {
        if (key !== 'exec') return Reflect.get(target, key);
        return (query: string, ...bindings: any[]) => {
          statements.push(query);
          return target.exec(query, ...bindings);
        };
      },
    });
    expect(()=>meter.runSync('refused-sampler',()=>sampleAccountCapacityHistory(observedSql, start + ACCOUNT_CAPACITY_HISTORY_MAX_ROWS * 15 * 60_000, () => [
      { label: 'capped-account', active: 2, reserved: 2, native_live: 2, max_concurrency: 4 },
    ]))).toThrow('account_capacity_history_observation_limited');
    expect(meter.snapshot()['refused-sampler'].rows_read).toBeLessThan(400);
    expect(statements.some(query => /^\s*DELETE\b/i.test(query))).toBe(false);
    expect(Number(fixtureStatements(sql).exec("SELECT COUNT(*) AS n FROM fleet_account_capacity_history WHERE account='capped-account'").one().n))
      .toBe(ACCOUNT_CAPACITY_HISTORY_MAX_ROWS);
  });
});

it('serves capacity history through the bounded admin-only account API', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, async (hub: any, state: any) => {
    registerFixtureStatements(hub);
    const start = Date.UTC(2026, 10, 1, 4, 20);
    const service = new FleetService(hub.sql, (_fn: any) => _fn(), () => start);
    service.init();
    fixtureStatements(hub.sql).exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('history-account','claude',13,0,1)");
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
      history_note: 'Capacity history starts at deployment; no historical backfill is available.',
      rows: [{ active: 7, reserved: 5, native_live: 4, limit: 13 }],
    });
    const forbidden = await service.handle(new Request(`https://hub.test${path}`), agent as any);
    expect(forbidden.status).toBe(403);
  });
});
