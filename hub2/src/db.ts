import type { SqlStorage } from '@cloudflare/workers-types';
import { assertCacheSchema, ReadCache } from './read-cache';

export interface RowCounts {
  sql_calls: number;
  rows_read: number;
  rows_written: number;
  schema_calls: number;
  measurement_valid: boolean;
  measurement_errors: number;
  transactions: number;
  alarm_operations: number;
}

/** Materializes every cursor before sampling rowsRead/rowsWritten. The values
 * include constructor, authorization, alarm, and route SQL for this instance. */
export class MeteredDb {
  private readonly cache = new ReadCache();
  private totals: RowCounts = {
    sql_calls: 0, rows_read: 0, rows_written: 0, schema_calls: 0,
    measurement_valid: true, measurement_errors: 0, transactions: 0, alarm_operations: 0,
  };

  constructor(private readonly sql: SqlStorage) {}

  all<T = Record<string, unknown>>(query: string, ...bindings: unknown[]): T[] {
    const cached = this.cache.get(query, bindings);
    if (cached !== undefined) return cached as T[];
    this.cache.before(query);
    let cursor: ReturnType<SqlStorage['exec']>;
    let rows: T[];
    try {
      cursor = this.execute(query, bindings);
      rows = cursor.toArray() as T[];
    } catch (error) {
      this.recordFailure(query);
      throw error;
    }
    this.record(query, cursor);
    if (this.totals.measurement_valid) this.cache.put(query, bindings, rows);
    return rows;
  }

  one<T = Record<string, unknown>>(query: string, ...bindings: unknown[]): T | undefined {
    return this.all<T>(query, ...bindings)[0];
  }

  run(query: string, ...bindings: unknown[]): RowCounts {
    this.cache.before(query);
    let cursor: ReturnType<SqlStorage['exec']>;
    try {
      cursor = this.execute(query, bindings);
      cursor.toArray();
    } catch (error) {
      this.recordFailure(query);
      throw error;
    }
    this.record(query, cursor);
    return { ...this.totals };
  }

  snapshot(): RowCounts & { read_cache: ReturnType<ReadCache['metrics']> } {
    return { ...this.totals, read_cache: this.cache.metrics() };
  }

  transaction<T>(state: DurableObjectState, callback: () => T): T {
    this.totals.transactions++;
    try {
      return state.storage.transactionSync(callback);
    } catch (error) {
      this.cache.clear();
      throw error;
    }
  }

  alarmOperation(): void { this.totals.alarm_operations++; }

  private execute(query: string, bindings: unknown[]): ReturnType<SqlStorage['exec']> {
    const cursor = this.sql.exec(query, ...bindings);
    return cursor;
  }

  private record(query: string, cursor: { rowsRead?: number; rowsWritten?: number }) {
    this.totals.sql_calls++;
    if (/^\s*(CREATE|DROP|ALTER|PRAGMA)\b/i.test(query)) this.totals.schema_calls++;
    let read: number | undefined;
    let written: number | undefined;
    try {
      read = cursor.rowsRead;
      written = cursor.rowsWritten;
    } catch {
      this.invalidateMeasurement();
      return;
    }
    if (typeof read !== 'number' || !Number.isFinite(read) || read < 0
      || typeof written !== 'number' || !Number.isFinite(written) || written < 0) {
      this.invalidateMeasurement();
      return;
    }
    this.totals.rows_read += read;
    this.totals.rows_written += written;
  }

  private recordFailure(query: string): void {
    this.totals.sql_calls++;
    if (/^\s*(CREATE|DROP|ALTER|PRAGMA)\b/i.test(query)) this.totals.schema_calls++;
    this.invalidateMeasurement();
  }

  private invalidateMeasurement(): void {
    this.totals.measurement_valid = false;
    this.totals.measurement_errors++;
    this.cache.clear();
  }
}

export function initializeSchema(
  state: DurableObjectState,
  db: MeteredDb,
  version: number,
  statements: string[],
  migrations: string[][] = [],
  afterInitialize?: () => void | Promise<void>,
  initialVersion = 1,
): Promise<void> {
  if (!Number.isSafeInteger(initialVersion) || initialVersion < 1 || initialVersion > version) {
    return Promise.reject(new Error('invalid_initial_schema_version'));
  }
  return state.blockConcurrencyWhile(async () => {
    assertCacheSchema([...statements, ...migrations.flat()]);
    db.transaction(state, () => {
      db.run('CREATE TABLE IF NOT EXISTS hub2_schema (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), version INTEGER NOT NULL)');
      const row = db.one<{ version: number }>('SELECT version FROM hub2_schema WHERE singleton = 1');
      if (row && row.version > version) throw new Error('schema_version_newer_than_code');
      let applied = row?.version || 0;
      if (applied === 0) {
        for (const statement of statements) db.run(statement);
        db.run('INSERT INTO hub2_schema(singleton, version) VALUES (1, ?)', initialVersion);
        applied = initialVersion;
      }
      while (applied < version) {
        const migration = migrations[applied - 1];
        if (!migration) throw new Error(`schema_migration_missing:${applied}->${applied + 1}`);
        for (const statement of migration) db.run(statement);
        applied++;
        db.run('UPDATE hub2_schema SET version=? WHERE singleton=1', applied);
      }
    });
    await afterInitialize?.();
  });
}
