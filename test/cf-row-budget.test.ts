import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { DAILY_ROW_LIMITS, RowBudget, RowBudgetExceeded } from '../src/row-budget';

class MemoryStore {
  data = new Map<string, unknown>();
  reads = 0;
  writes = 0;
  async get<T>(key: string): Promise<T | undefined> { this.reads++; return structuredClone(this.data.get(key)) as T | undefined; }
  async put<T>(key: string, value: T): Promise<void> { this.writes++; this.data.set(key, structuredClone(value)); }
}

/** Cursor counters grow as a real SQL cursor advances, rather than all being
 * available at exec time. This catches wrappers that only sample exec(). */
function nativeSql(rows: number, readPerRow = 1, written = 0) {
  const exec = vi.fn(() => {
    let index = 0, read = 0;
    const cursor: any = {
      get rowsRead() { return read; }, rowsWritten: written,
      next() { if (index >= rows) return { done: true, value: undefined }; read += readPerRow; return { done: false, value: { id: index++ } }; },
      [Symbol.iterator]() { return this; },
      raw() { return { next: () => { const row = cursor.next(); return { ...row, value: row.value && [row.value.id] }; }, [Symbol.iterator]() { return this; } }; },
      toArray() { return Array.from(this); },
      one() { const first = this.next(); if (this.next().done && !first.done) return first.value; throw new Error('not one'); },
    };
    return cursor;
  });
  return { exec };
}

const LIMITS = { read: 100, written: 50 };
const CHUNK = { read: 20, written: 10 };

// These tests are pure unit tests; CI's normal Vitest discovery includes them.
describe('durable daily SQL row circuit breaker', () => {
  it('fails closed before any SQL without a durable reservation', () => {
    const native = nativeSql(1);
    const budget = new RowBudget(new MemoryStore());
    expect(() => budget.wrap(native as any).exec('SELECT 1')).toThrow(RowBudgetExceeded);
    expect(native.exec).not.toHaveBeenCalled();
  });

  it('accounts lazy cursor reads and refuses further SQL even after a caught error', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await budget.begin();
    const native = nativeSql(100);
    const sql = budget.wrap(native as any);
    expect(() => sql.exec('SELECT id FROM bounded_pending').toArray()).toThrow(RowBudgetExceeded);
    expect(native.exec).toHaveBeenCalledTimes(1);
    expect(() => sql.exec('SELECT expensive_work')).toThrow(RowBudgetExceeded);
    expect(native.exec).toHaveBeenCalledTimes(1);
    await budget.finish();
    // No writes per row / iteration or on a normal event finish.
    expect(store.writes).toBe(1);
  });

  it.each(['iterator', 'raw', 'next', 'one'] as const)('accounts %s consumption', async method => {
    const budget = new RowBudget(new MemoryStore(), () => 0, LIMITS, CHUNK);
    await budget.begin();
    const cursor: any = budget.wrap(nativeSql(3, 8) as any).exec('SELECT bounded');
    expect(() => {
      if (method === 'iterator') [...cursor];
      if (method === 'raw') [...cursor.raw()];
      if (method === 'next') { cursor.next(); cursor.next(); cursor.next(); }
      if (method === 'one') { try { cursor.one(); } catch {} cursor.next(); }
    }).toThrow(RowBudgetExceeded);
    await budget.finish();
  });

  it('charges writes reported at exec, then persists excess outside rollbackable SQL', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await budget.begin();
    const native = nativeSql(0, 0, 70);
    expect(() => budget.wrap(native as any).exec('UPDATE bounded')).toThrow(RowBudgetExceeded);
    await budget.finish();
    expect(budget.status().reserved!.written).toBeGreaterThan(LIMITS.written);
    const restarted = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await expect(restarted.begin()).rejects.toThrow(RowBudgetExceeded);
    expect(native.exec).toHaveBeenCalledTimes(1);
  });

  it('does not reissue reservations on eviction or refund rolled-back usage', async () => {
    const store = new MemoryStore();
    for (let i = 0; i < 4; i++) {
      const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
      await budget.begin();
      // Lose the instance with unused prepaid credit. Storage survives.
    }
    const exhausted = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await expect(exhausted.begin()).rejects.toThrow(RowBudgetExceeded);
    expect(exhausted.status().reserved!.written).toBeLessThanOrEqual(LIMITS.written);
  });

  it('serializes concurrent reservation attempts without issuing duplicate credit', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await Promise.all([budget.begin(), budget.begin(), budget.begin()]);
    expect(store.reads).toBe(1);
    expect(store.writes).toBe(1);
    expect(budget.status().available).toEqual(CHUNK);
  });

  it('isolates many concurrent handled denials from the reservation queue and recovers next UTC day', async () => {
    const store = new MemoryStore();
    let now = 0;
    const budget = new RowBudget(store, () => now, LIMITS, CHUNK);
    await budget.begin();
    expect(() => budget.wrap(nativeSql(0, 0, 70) as any).exec('UPDATE bounded')).toThrow(RowBudgetExceeded);
    await budget.finish();
    const handled = Array.from({ length: 25 }, async () => {
      try { await budget.begin(); return false; }
      catch (error) { expect(error).toBeInstanceOf(RowBudgetExceeded); return true; }
    });
    expect((await Promise.all(handled)).every(Boolean)).toBe(true);
    now = 86_400_000;
    await budget.begin();
    expect(budget.status().stopped).toBe(false);
    // Any intermediate unhandled promise also fails this test through the
    // native CI runtime's normal unhandled-rejection capture.
  });

  it('keeps repeated idle preflights and event completions at zero storage I/O', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await budget.begin();
    for (let i = 0; i < 100; i++) { await budget.begin(); await budget.finish(); }
    expect(store.reads).toBe(1);
    expect(store.writes).toBe(1);
  });

  it('resets only at the next UTC day and refuses time moving backwards', async () => {
    const store = new MemoryStore();
    let now = 0;
    const budget = new RowBudget(store, () => now, LIMITS, CHUNK);
    await budget.begin();
    expect(() => budget.wrap(nativeSql(0, 0, 70) as any).exec('UPDATE bounded')).toThrow(RowBudgetExceeded);
    await budget.finish();
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
    now = 86_400_000;
    await budget.begin();
    expect(budget.status().stopped).toBe(false);
    now = 0;
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
  });

  it('persists a daily stop when SQL fails without a cursor, even if the caller catches it', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await budget.begin();
    const native = { exec: vi.fn(() => { throw new Error('runtime interrupted after scanning'); }) };
    expect(() => budget.wrap(native as any).exec('SELECT expensive')).toThrow(RowBudgetExceeded);
    await budget.finish();
    await expect(new RowBudget(store, () => 0, LIMITS, CHUNK).begin()).rejects.toThrow(RowBudgetExceeded);
    expect(native.exec).toHaveBeenCalledTimes(1);
  });

  it('charges failed cursor stepping and persists a daily stop even if the caller catches it', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await budget.begin();
    let scanned = 0;
    const native = { exec() { return {
      get rowsRead() { return scanned; }, rowsWritten: 0,
      next() { scanned += 70; throw new Error('malformed JSON after scan'); },
      [Symbol.iterator]() { return this; },
    }; } };
    const sql = budget.wrap(native as any);
    expect(() => sql.exec('SELECT failing_step').toArray()).toThrow(RowBudgetExceeded);
    await budget.finish();
    expect(budget.status().reserved!.read).toBeGreaterThanOrEqual(71);
    await expect(new RowBudget(store, () => 0, LIMITS, CHUNK).begin()).rejects.toThrow(RowBudgetExceeded);
  });

  it('does not issue SQL allowance if a ledger write fails', async () => {
    const store = new MemoryStore();
    store.put = vi.fn(async () => { throw new Error('storage unavailable'); });
    const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
    await expect(budget.begin()).rejects.toThrow('storage unavailable');
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
    await budget.finish();
    expect(store.put).toHaveBeenCalledTimes(1);
    const native = nativeSql(1);
    expect(() => budget.wrap(native as any).exec('SELECT 1')).toThrow(RowBudgetExceeded);
    expect(native.exec).not.toHaveBeenCalled();
  });

  it('charges KV/alarm writes and permits only one emergency alarm clear after a trip', async () => {
    const storage = Object.assign(new MemoryStore(), {
      setAlarm: vi.fn(async () => {}), getAlarm: vi.fn(async () => null),
      deleteAlarm: vi.fn(async () => {}), list: vi.fn(async () => new Map()),
    });
    const nativeSetAlarm = storage.setAlarm;
    const nativeDeleteAlarm = storage.deleteAlarm;
    const nativeList = storage.list;
    const budget = new RowBudget(storage, () => 0, LIMITS, CHUNK);
    budget.guardStorage(storage);
    await budget.begin(); // native ledger calls do not recurse through guard
    for (let i = 0; i < 9; i++) await storage.setAlarm();
    expect(() => storage.setAlarm()).toThrow(RowBudgetExceeded);
    expect(nativeSetAlarm).toHaveBeenCalledTimes(9);
    await storage.deleteAlarm();
    await storage.deleteAlarm();
    expect(nativeDeleteAlarm).toHaveBeenCalledTimes(1);
    await budget.finish();
    await budget.begin();
    await expect((storage.list as any)()).rejects.toThrow(RowBudgetExceeded);
    expect(nativeList).not.toHaveBeenCalled();
  });

  it('refunds only idle unused credit so repeated orderly cold starts retain actual daily spending', async () => {
    const store = new MemoryStore();
    for (let i=0;i<10;i++) {
      const budget = new RowBudget(store,()=>0,LIMITS,CHUNK);
      await budget.begin();
      budget.wrap(nativeSql(0,0,1) as any).exec('UPDATE bounded');
      expect(await budget.releaseUnused()).toBe(true);
      expect(budget.status().available).toEqual({read:0,written:0});
      // SQL requires a new durable preflight after the release.
      expect(()=>budget.wrap(nativeSql(0) as any).exec('SELECT 1')).toThrow(RowBudgetExceeded);
    }
    const last = [...store.data.values()][0] as any;
    expect(last.written).toBe(30); // per start: reservation + actual write + refund
    expect(last.read).toBe(10); // one ledger load per start; unused reads refunded
    expect(last.stopped).toBe(false);
  });

  it('keeps actual SQL spending despite rollback and never reopens a stopped day', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store,()=>0,LIMITS,CHUNK);
    await budget.begin();
    try { budget.wrap(nativeSql(0,0,4) as any).exec('UPDATE rollback'); throw new Error('application rollback'); } catch {}
    await budget.releaseUnused();
    expect((([...store.data.values()][0]) as any).written).toBe(6);
    await budget.begin();
    expect(()=>budget.wrap(nativeSql(0,0,70) as any).exec('UPDATE overshoot')).toThrow(RowBudgetExceeded);
    await budget.finish();
    await budget.releaseUnused();
    await expect(new RowBudget(store,()=>0,LIMITS,CHUNK).begin()).rejects.toThrow(RowBudgetExceeded);
  });

  it('rechecks quiet eligibility after queued operations so new activity cancels a pending refund', async () => {
    const store = new MemoryStore();
    let quiet = true;
    const budget = new RowBudget(store,()=>0,LIMITS,CHUNK);
    const begin = budget.begin();
    const refund = budget.releaseUnused(()=>quiet);
    quiet = false;
    await begin;
    expect(await refund).toBe(false);
    expect(store.writes).toBe(1);
    expect(budget.status().available).toEqual(CHUNK);
  });

  it('retains the old reservation and fails closed if an idle checkpoint cannot persist', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store,()=>0,LIMITS,CHUNK);
    await budget.begin();
    const before = budget.status().reserved;
    store.put = vi.fn(async()=>{throw new Error('checkpoint unavailable');});
    await expect(budget.releaseUnused()).rejects.toThrow('checkpoint unavailable');
    expect(budget.status().reserved).toEqual(before);
    expect(budget.status().available).toEqual(CHUNK);
    expect(await budget.releaseUnused()).toBe(false);
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
    expect(store.put).toHaveBeenCalledTimes(1);
  });

  it('refuses corrupt persisted counters and clamps configured limits to free-plan safety ceilings', async () => {
    const store = new MemoryStore();
    store.data.set('cf_daily_row_budget_v1', { day: 0, read: -1, written: 0, stopped: false });
    await expect(new RowBudget(store, () => 0).begin()).rejects.toThrow(RowBudgetExceeded);
    const budget = new RowBudget(new MemoryStore(), () => 0, { read: Infinity, written: DAILY_ROW_LIMITS.written + 1 });
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
  });
});


describe('native SQLite budget accounting', () => {
  it('meters lazy native cursor reads before a caller can continue scanning', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('cf-native-budget-' + crypto.randomUUID()));
    await (runInDurableObject as any)(stub, async (_hub: any, state: any) => {
      const raw = state.storage.sql;
      raw.exec('CREATE TABLE cf_budget_fixture(id INTEGER PRIMARY KEY)');
      for (let i = 0; i < 40; i++) raw.exec('INSERT INTO cf_budget_fixture VALUES(?)', i);
      const budget = new RowBudget(new MemoryStore(), () => 0, LIMITS, CHUNK);
      await budget.begin();
      const sql = budget.wrap(raw);
      expect(() => sql.exec('SELECT id FROM cf_budget_fixture ORDER BY id').toArray()).toThrow(RowBudgetExceeded);
      expect(() => sql.exec('SELECT id FROM cf_budget_fixture')).toThrow(RowBudgetExceeded);
      await budget.finish();
    });
  });

  it('keeps the prepaid ledger after the application SQL transaction rolls back', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('cf-rollback-budget-' + crypto.randomUUID()));
    await (runInDurableObject as any)(stub, async (_hub: any, state: any) => {
      const raw = state.storage.sql;
      raw.exec('CREATE TABLE cf_budget_rollback(id INTEGER PRIMARY KEY)');
      // Prefix only this fixture ledger so the Hub's own guard remains isolated.
      const store = {
        get: <T>(key: string) => state.storage.get('fixture_' + key) as Promise<T | undefined>,
        put: <T>(key: string, value: T) => state.storage.put('fixture_' + key, value) as Promise<void>,
      };
      const budget = new RowBudget(store, () => 0, LIMITS, CHUNK);
      await budget.begin();
      const sql = budget.wrap(raw);
      expect(() => state.storage.transactionSync(() => {
        for (let i = 0; i < 100; i++) sql.exec('INSERT INTO cf_budget_rollback VALUES(?)', i);
      })).toThrow(RowBudgetExceeded);
      await budget.finish();
      expect(raw.exec('SELECT COUNT(*) AS n FROM cf_budget_rollback').one().n).toBe(0);
      const restarted = new RowBudget(store, () => 0, LIMITS, CHUNK);
      await restarted.begin();
      // Previous reservation is never refunded by transactionSync rollback.
      expect(restarted.status().reserved!.written).toBeGreaterThan(CHUNK.written * 2);
    });
  });
});
