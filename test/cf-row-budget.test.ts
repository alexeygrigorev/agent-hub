import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { DAILY_COMBINED_ROW_LIMIT, DAILY_ROW_LIMITS, RowBudget, RowBudgetExceeded } from '../src/row-budget';

class MemoryStore {
  data = new Map<string, unknown>();
  reads = 0;
  writes = 0;
  failWrites: string | null = null;
  async get<T>(key: string): Promise<T | undefined> { this.reads++; return structuredClone(this.data.get(key)) as T | undefined; }
  async put<T>(key: string, value: T): Promise<void> { this.writes++; if (this.failWrites) throw new Error(this.failWrites); this.data.set(key, structuredClone(value)); }
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
    store.failWrites = 'checkpoint unavailable';
    await expect(budget.releaseUnused()).rejects.toThrow('checkpoint unavailable');
    expect(budget.status().reserved).toEqual(before);
    expect(budget.status().available).toEqual(CHUNK);
    expect(await budget.releaseUnused()).toBe(false);
    await expect(budget.begin()).rejects.toThrow(RowBudgetExceeded);
    expect(store.writes).toBe(2); // initial reservation plus the failed checkpoint
  });

  it('enforces the 20k combined ceiling when each direction alone is below 20k', async () => {
    const store = new MemoryStore();
    const budget = new RowBudget(store,()=>0);
    await budget.begin(true);
    expect(budget.status().limit_total).toBe(DAILY_COMBINED_ROW_LIMIT);
    expect(budget.status().reserved_total!).toBeLessThan(20_000);
    const native = { exec() { return { rowsRead:12_000,rowsWritten:9_000 }; } };
    expect(()=>budget.wrap(native as any).exec('SELECT bounded_work')).toThrow(RowBudgetExceeded);
    await budget.finish();
    await budget.releaseUnused();
    const persisted = [...store.data.values()][0] as any;
    expect(persisted.read).toBeLessThan(20_000);
    expect(persisted.written).toBeLessThan(20_000);
    expect(persisted.read+persisted.written).toBeGreaterThan(20_000);
    expect(persisted.stopped).toBe(true);
    await expect(new RowBudget(store,()=>0).begin()).rejects.toThrow(RowBudgetExceeded);
  });

  it('charges a cold current-day KV lookup even when it loads yesterday’s stopped ledger', async () => {
    const store=new MemoryStore();
    store.data.set('cf_daily_row_budget_v1',{day:0,read:19000,written:999,stopped:true});
    const budget=new RowBudget(store,()=>86400001);
    await budget.begin();await budget.releaseUnused();
    const persisted=[...store.data.values()][0] as any;
    expect(persisted.day).toBe(1);expect(persisted.stopped).toBe(false);
    expect(persisted.read).toBe(1);expect(persisted.written).toBe(2);
    expect(store.reads).toBe(1);
  });

  it('completes a bounded500-row request within one normal prepaid block', async () => {
    const store=new MemoryStore(),budget=new RowBudget(store,()=>0);
    await budget.begin();
    expect(budget.status().available_total).toBe(4096);
    const sql=budget.wrap(nativeSql(500,2) as any);
    expect(sql.exec('SELECT bounded_indexed_page').toArray()).toHaveLength(500);
    budget.wrap(nativeSql(0,0,100) as any).exec('UPDATE bounded_followup');
    expect(budget.status().stopped).toBe(false);
    expect(store.writes).toBe(1);
    await budget.releaseUnused();
    const persisted=[...store.data.values()][0] as any;
    expect(persisted.read+persisted.written).toBe(1103);
    expect(persisted.read+persisted.written).toBeLessThan(20000);
  });

  it('borrows unused prepaid directions for constructor work without raising the combined reservation', async () => {
    const store=new MemoryStore(),budget=new RowBudget(store,()=>0);
    await budget.begin(true);
    const original=budget.status().reserved_total;
    const credit=budget.status().available_total;
    budget.wrap(nativeSql(0,0,5000) as any).exec('UPDATE bounded_constructor');
    budget.wrap(nativeSql(1,10000,0) as any).exec('SELECT bounded_constructor').toArray();
    expect(budget.status().stopped).toBe(false);
    expect(budget.status().reserved_total).toBe(original);
    expect(budget.status().available_total).toBe(credit-15000);
    await budget.releaseUnused();
    const persisted=[...store.data.values()][0] as any;
    expect(persisted.read).toBe(10001);
    expect(persisted.written).toBe(5002);
    expect(persisted.read+persisted.written).toBeLessThan(20000);
    const restart=new RowBudget(store,()=>0);await restart.begin();
    expect(restart.status().stopped).toBe(false);
  });

  it('keeps fungible bulk credit across a directional workload without another checkpoint', async () => {
    const store=new MemoryStore(),budget=new RowBudget(store,()=>0);
    await budget.begin(true);
    const credit=budget.status().available_total;
    budget.wrap(nativeSql(0,0,5000) as any).exec('UPDATE bounded_bootstrap');
    await budget.begin();
    await budget.begin(true);
    expect(budget.status().available_total).toBe(credit-5000);
    expect(budget.status().reserved_total!).toBeLessThan(20000);
    expect(store.writes).toBe(1);
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
  it('upgrades a bounded bulk event past4096 operations without cheap-event checkpoint churn or a higher daily cap', async () => {
    const stub=(env as any).HUB.get((env as any).HUB.idFromName('cf-large-prepay-'+crypto.randomUUID()));
    await (runInDurableObject as any)(stub,async(_hub:any,state:any)=>{
      const raw=state.storage.sql;
      raw.exec('CREATE TABLE cf_bulk_source(id INTEGER PRIMARY KEY)');
      for(let i=0;i<500;i++)raw.exec('INSERT INTO cf_bulk_source VALUES(?)',i);
      const store=new MemoryStore(),budget=new RowBudget(store,()=>0);
      await budget.begin();
      expect(budget.status().available_total).toBe(4096);
      await budget.begin(true);
      const sql=budget.wrap(raw);
      let nativeReads=0;
      for(let i=0;i<10;i++) {
        const cursor=sql.exec('SELECT id FROM cf_bulk_source ORDER BY id LIMIT 500');
        expect(cursor.toArray()).toHaveLength(500);nativeReads+=cursor.rowsRead;
      }
      expect(nativeReads).toBeGreaterThan(4096);
      expect(budget.status().stopped).toBe(false);
      const writes=store.writes;
      for(let i=0;i<100;i++) {
        await budget.begin(true);
        expect(sql.exec('SELECT id FROM cf_bulk_source WHERE id=?',i).toArray()).toHaveLength(1);
        await budget.finish();
      }
      expect(store.writes).toBe(writes);
      expect(budget.status().reserved_total).toBeLessThan(20000);
      expect(()=>{for(let i=0;i<40;i++)sql.exec('SELECT id FROM cf_bulk_source ORDER BY id LIMIT 500').toArray();}).toThrow(RowBudgetExceeded);
      await budget.finish();
      const persisted=[...store.data.values()][0] as any;
      expect(persisted.read+persisted.written).toBeGreaterThanOrEqual(20000);
      await expect(new RowBudget(store,()=>0).begin(true)).rejects.toThrow(RowBudgetExceeded);
      expect(()=>sql.exec('SELECT 1')).toThrow(RowBudgetExceeded);
    });
  });

  it('preflights a small real index using the same combined pool after write credit is borrowed', async () => {
    const stub=(env as any).HUB.get((env as any).HUB.idFromName('cf-pooled-ddl-'+crypto.randomUUID()));
    await (runInDurableObject as any)(stub,async(_hub:any,state:any)=>{
      const raw=state.storage.sql;
      raw.exec('CREATE TABLE pooled_ddl_source(id INTEGER PRIMARY KEY,value TEXT)');
      raw.exec("INSERT INTO pooled_ddl_source VALUES(1,'a'),(2,'b')");
      const budget=new RowBudget(new MemoryStore(),()=>0);await budget.begin(true);
      budget.wrap(nativeSql(0,0,5000) as any).exec('UPDATE bounded_constructor');
      expect(budget.status().available.written).toBe(0);
      budget.wrap(raw).exec('CREATE INDEX pooled_ddl_value ON pooled_ddl_source(value)');
      expect(budget.status().stopped).toBe(false);
      await budget.finish();
      expect(budget.status().reserved_total).toBeLessThan(20000);
    });
  });

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
