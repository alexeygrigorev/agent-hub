import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { RowBudget, RowBudgetExceeded } from '../src/row-budget';

const memoryLedger = () => {
  let data: any;
  return { async get<T>(): Promise<T | undefined> { return structuredClone(data); },
    async put<T>(_key: string, value: T) { data = structuredClone(value); } };
};

describe('Hub daily cost circuit breaker integration', () => {
  it('stops alarm, cron RPCs and requests without SQL, KV scans or rearming', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('cf-budget-event-stop-' + crypto.randomUUID()));
    await (runInDurableObject as any)(stub, async (hub: any, state: any) => {
      const budget = new RowBudget(memoryLedger(), () => 0, { read: 100, written: 50 }, { read: 20, written: 10 });
      await budget.begin();
      expect(() => budget.wrap({ exec() { return { rowsRead: 0, rowsWritten: 70 }; } } as any).exec('UPDATE bounded')).toThrow(RowBudgetExceeded);
      await budget.finish();
      const nativeDeleteAlarm = vi.spyOn(state.storage, 'deleteAlarm');
      const nativeSetAlarm = vi.spyOn(state.storage, 'setAlarm');
      const nativeGet = vi.spyOn(state.storage, 'get');
      const nativePut = vi.spyOn(state.storage, 'put');
      budget.guardStorage(state.storage);
      Object.defineProperty(hub, 'rowBudget', { value: budget, configurable: true });
      const before = hub.meter.snapshot();
      await hub.alarm();
      const tick = await hub.scheduledTick();
      const checks = await hub.runChecks();
      for (const path of ['/api/sessions', '/mcp', '/oauth/authorize', '/.well-known/oauth-authorization-server', '/api/conversations', '/api/status']) {
        const response = await hub.fetch(new Request('https://hub' + path));
        expect(response.status, path).toBe(503);
      }
      expect(tick.reason).toBe('do_daily_row_budget_exceeded');
      expect(checks).toEqual([]);
      expect(hub.meter.snapshot()).toEqual(before);
      expect(nativeSetAlarm).not.toHaveBeenCalled();
      expect(nativeGet).not.toHaveBeenCalled();
      expect(nativePut).not.toHaveBeenCalled();
      // All nested event handlers share a single idempotent emergency clear.
      expect(nativeDeleteAlarm).toHaveBeenCalledTimes(1);
      vi.restoreAllMocks();
    });
  });

  it('does not initialize schema/services if the durable ledger already stopped today', async () => {
    const id = (env as any).HUB.idFromName('cf-budget-init-stop-' + crypto.randomUUID());
    const stub = (env as any).HUB.get(id);
    await (runInDurableObject as any)(stub, async (hub: any, state: any) => {
      const ledger = memoryLedger();
      await ledger.put('cf_daily_row_budget_v1', { day: 0, read: 100, written: 50, stopped: true });
      const budget = new RowBudget(ledger, () => 0, { read: 100, written: 50 }, { read: 20, written: 10 });
      budget.guardStorage(state.storage);
      Object.defineProperty(hub, 'rowBudget', { value: budget, configurable: true });
      Object.defineProperty(hub, 'initializationComplete', { value: false, writable: true });
      const initialize = vi.spyOn(hub, 'initializeServices');
      const before = hub.meter.snapshot();
      for (const path of ['/api/sessions', '/mcp', '/oauth/authorize', '/.well-known/oauth-authorization-server', '/api/conversations', '/api/status']) {
        const response = await hub.fetch(new Request('https://hub' + path));
        expect(response.status, path).toBe(503);
      }
      expect(initialize).not.toHaveBeenCalled();
      expect(hub.meter.snapshot()).toEqual(before);
      vi.restoreAllMocks();
    });
  });
});
