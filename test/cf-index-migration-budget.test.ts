import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { INDEX_BUILD_SOURCE_LIMIT, RowBudget, RowBudgetExceeded } from '../src/row-budget';

class LedgerStore {
  data = new Map<string, unknown>();
  async get<T>(key: string): Promise<T | undefined> { return structuredClone(this.data.get(key)) as T | undefined; }
  async put<T>(key: string, value: T): Promise<void> { this.data.set(key, structuredClone(value)); }
}
const nativeCase = (fn: (raw: any) => Promise<void>) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('index-preflight-' + crypto.randomUUID())),
  async (_hub: any, state: any) => fn(state.storage.sql));
function fixture(raw: any, table: string, count: number) {
  const name = '"' + table.replaceAll('"', '""') + '"';
  raw.exec(`CREATE TABLE ${name}(id INTEGER PRIMARY KEY,value INTEGER)`);
  raw.exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<?) INSERT INTO ${name} SELECT i,i FROM n`, count);
}
function observe(raw: any) {
  const calls: { query: string; cursor: any }[] = [];
  const sql = new Proxy(raw, { get(target, key) {
    if (key !== 'exec') { const value = target[key]; return typeof value === 'function' ? value.bind(target) : value; }
    return (query: string, ...bindings: any[]) => {
      const cursor = target.exec(query, ...bindings);
      calls.push({ query, cursor });
      return cursor;
    };
  } });
  return { sql, calls };
}

describe('index migration cost preflight', () => {
  it('stops a large new index before native CREATE INDEX and durably refuses retries', () => nativeCase(async raw => {
    fixture(raw, 'large_source', INDEX_BUILD_SOURCE_LIMIT + 5);
    const store = new LedgerStore(), budget = new RowBudget(store);
    await budget.begin(true);
    const observed = observe(raw), sql = budget.wrap(observed.sql);
    const before = budget.status().available.read;
    expect(() => sql.exec('/* migration */ CREATE UNIQUE INDEX IF NOT EXISTS [new_large_index] ON "large_source"(value)'))
      .toThrow(RowBudgetExceeded);
    expect(observed.calls.some(call => /CREATE\s+UNIQUE\s+INDEX/i.test(call.query))).toBe(false);
    const probe = observed.calls.find(call => /^SELECT 1 FROM/.test(call.query))!;
    expect(probe).toBeDefined();
    expect(Number(probe.cursor.rowsRead)).toBeGreaterThanOrEqual(INDEX_BUILD_SOURCE_LIMIT);
    expect(Number(probe.cursor.rowsRead)).toBeLessThanOrEqual(INDEX_BUILD_SOURCE_LIMIT + 1);
    expect(before - budget.status().available.read).toBeGreaterThanOrEqual(Number(probe.cursor.rowsRead));
    expect(raw.exec("SELECT name FROM pragma_index_list('large_source') WHERE name='new_large_index'").toArray()).toHaveLength(0);
    await budget.finish();
    await expect(new RowBudget(store).begin()).rejects.toThrow(RowBudgetExceeded);
    console.log('index refused before build; source probe rows_read=' + probe.cursor.rowsRead);
  }));

  it('bounds source reads and matching writes separately for a sparse partial index', () => nativeCase(async raw => {
    fixture(raw,'sparse_source',5000);
    const budget=new RowBudget(new LedgerStore());await budget.begin(true);
    const observed=observe(raw),sql=budget.wrap(observed.sql),before=budget.status().available;
    sql.exec('CREATE INDEX sparse_live ON sparse_source(value) WHERE value<=5 /* internal */ AND id>0; -- trailing comment').toArray();
    const after=budget.status().available;
    expect(before.read-after.read).toBe(observed.calls.reduce((n,c)=>n+Number(c.cursor.rowsRead),0));
    expect(before.written-after.written).toBe(observed.calls.reduce((n,c)=>n+Number(c.cursor.rowsWritten),0));
    expect(before.read-after.read).toBeLessThan(22000);
    expect(before.written-after.written).toBeLessThan(20);
    expect(budget.status().stopped).toBe(false);
    console.log('sparse partial index: source=5000 entries=5 rows_read='+(before.read-after.read)+' rows_written='+(before.written-after.written));
  }));

  it('refuses a partial index whose matching write set exceeds the hard build cap', () => nativeCase(async raw => {
    fixture(raw,'wide_partial',2000);
    const budget=new RowBudget(new LedgerStore());await budget.begin(true);
    const observed=observe(raw),sql=budget.wrap(observed.sql);
    expect(()=>sql.exec('CREATE INDEX excessive_entries ON wide_partial(value) WHERE value<=1500')).toThrow(RowBudgetExceeded);
    expect(observed.calls.some(c=>/CREATE INDEX/.test(c.query))).toBe(false);
    expect(raw.exec("SELECT name FROM pragma_index_list('wide_partial') WHERE name='excessive_entries'").toArray()).toHaveLength(0);
  }));

  it('allows an existing index on a large source without probing that source', () => nativeCase(async raw => {
    fixture(raw, 'existing_source', INDEX_BUILD_SOURCE_LIMIT + 5);
    raw.exec('CREATE INDEX already_present ON existing_source(value)'); // Fixture represents the deployed schema.
    const budget = new RowBudget(new LedgerStore());
    await budget.begin(true);
    const observed = observe(raw), sql = budget.wrap(observed.sql);
    sql.exec('CREATE INDEX IF NOT EXISTS already_present ON existing_source(value)').toArray();
    expect(observed.calls.some(call => /^SELECT 1 FROM/.test(call.query))).toBe(false);
    expect(observed.calls.filter(call => /CREATE INDEX/.test(call.query))).toHaveLength(1);
    expect(observed.calls.some(call => /pragma_index_list/.test(call.query))).toBe(true);
    expect(budget.status().stopped).toBe(false);
  }));

  it('charges metadata, source probe and actual small index counters, with quoted identifiers', () => nativeCase(async raw => {
    fixture(raw, 'quoted"source', 3);
    const budget = new RowBudget(new LedgerStore());
    await budget.begin(true);
    const observed = observe(raw), sql = budget.wrap(observed.sql), before = budget.status().available;
    sql.exec('CREATE INDEX IF NOT EXISTS "quoted""index" ON "quoted""source"(value)').toArray();
    const after = budget.status().available;
    expect(before.read - after.read).toBe(observed.calls.reduce((sum, call) => sum + Number(call.cursor.rowsRead), 0));
    expect(before.written - after.written).toBe(observed.calls.reduce((sum, call) => sum + Number(call.cursor.rowsWritten), 0));
    expect(raw.exec("SELECT name FROM pragma_index_list(?)", 'quoted"source').toArray().some((row: any) => row.name === 'quoted"index')).toBe(true);
    expect(budget.status().stopped).toBe(false);
  }));

  it('reduces the source cap to current prepaid credit and rejects mixed batches', () => nativeCase(async raw => {
    fixture(raw, 'credit_source', 25);
    const observed = observe(raw);
    const budget = new RowBudget(new LedgerStore(), Date.now, { read: 10000, written: 1000 }, { read: 200, written: 40 });
    await budget.begin();
    expect(() => budget.wrap(observed.sql).exec('CREATE INDEX credit_index ON credit_source(value)')).toThrow(RowBudgetExceeded);
    expect(observed.calls.some(call => /CREATE INDEX/.test(call.query))).toBe(false);
    const separate = new RowBudget(new LedgerStore());
    await separate.begin(true);
    expect(() => separate.wrap(observed.sql).exec('CREATE TABLE batch_source(value); CREATE INDEX batch_index ON batch_source(value)')).toThrow(RowBudgetExceeded);
    expect(observed.calls.some(call => /batch_source/.test(call.query))).toBe(false);
  }));
});
