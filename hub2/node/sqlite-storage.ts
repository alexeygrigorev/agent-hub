// Durable Object storage on plain SQLite files: the SqlStorage cursor surface
// used by MeteredDb, the key-value storage API, alarms and the transaction
// helper, all backed by one DatabaseSync per object instance.

import { DatabaseSync } from 'node:sqlite';

export interface SqlCursor {
  toArray(): Record<string, unknown>[];
  rowsRead: number;
  rowsWritten: number;
}

/** One SQLite file per Durable Object instance. WAL keeps concurrent readers
 * (backup VACUUM INTO, drills) safe against the live worker. */
export class NodeSqlStorage {
  private readonly db: DatabaseSync;

  constructor(file: string) {
    this.db = new DatabaseSync(file);
    this.db.exec('PRAGMA journal_mode=WAL');
    this.db.exec('PRAGMA busy_timeout=5000');
    this.db.exec('PRAGMA synchronous=NORMAL');
    this.db.exec('PRAGMA foreign_keys=ON');
    this.db.exec('CREATE TABLE IF NOT EXISTS hub2_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID');
    this.db.exec('CREATE TABLE IF NOT EXISTS hub2_alarm (singleton INTEGER PRIMARY KEY CHECK(singleton=1), due INTEGER)');
  }

  exec(query: string, ...bindings: unknown[]): SqlCursor {
    const statement = this.db.prepare(query);
    const returnsRows = statement.columns().length > 0;
    if (returnsRows) {
      const rows = statement.all(...bindings.map(v => v as never)) as Record<string, unknown>[];
      return { toArray: () => rows, rowsRead: rows.length, rowsWritten: 0 };
    }
    const result = statement.run(...bindings.map(v => v as never));
    const written = Number(result.changes);
    return { toArray: () => [], rowsRead: written, rowsWritten: written };
  }

  /** Closes the underlying handle (runtime dispose / tests). */
  close(): void {
    this.db.close();
  }
}

const ALARM_LIMIT_MS = 5 * 365 * 24 * 60 * 60 * 1000;

/** The storage surface DOs use: KV get/put/delete/list, alarms, KV-durable
 * transactionSync (same shape the semantic-test fixtures provide). Values are
 * JSON-encoded; hub2 only stores JSON-shaped values. */
export class NodeDurableStorage {
  readonly sql: NodeSqlStorage;
  private transactionDepth = 0;

  constructor(sql: NodeSqlStorage) {
    this.sql = sql;
  }

  async get<T = unknown>(key: string): Promise<T | undefined> {
    const rows = this.sql.exec('SELECT v FROM hub2_kv WHERE k=?', key);
    const row = rows.toArray()[0];
    return row === undefined ? undefined : JSON.parse(String(row.v)) as T;
  }

  async put(key: string, value: unknown): Promise<void> {
    this.sql.exec('INSERT OR REPLACE INTO hub2_kv (k, v) VALUES (?, ?)', key, JSON.stringify(value ?? null));
  }

  async delete(key: string): Promise<boolean> {
    const cursor = this.sql.exec('DELETE FROM hub2_kv WHERE k=?', key);
    return cursor.rowsWritten > 0;
  }

  async deleteAll(): Promise<void> {
    this.sql.exec('DELETE FROM hub2_kv');
  }

  async list<T = unknown>(options: { prefix?: string; limit?: number; startAfter?: string } = {}): Promise<Map<string, T>> {
    const prefix = options.prefix ?? '';
    const startAfter = options.startAfter;
    const limit = options.limit ?? 1000;
    const rows = this.sql.exec(
      'SELECT k, v FROM hub2_kv WHERE k>=? AND substr(k,1,length(?))=? ORDER BY k LIMIT ?',
      startAfter === undefined ? prefix : startAfter + '\u0000',
      prefix, prefix, limit,
    );
    const out = new Map<string, T>();
    for (const row of rows.toArray()) out.set(String(row.k), JSON.parse(String(row.v)) as T);
    return out;
  }

  transactionSync<T>(fn: () => T): T {
    if (this.transactionDepth > 0) throw new Error('nested_transaction_unsupported');
    this.transactionDepth = 1;
    this.sql.exec('BEGIN IMMEDIATE');
    try {
      const result = fn();
      this.sql.exec('COMMIT');
      return result;
    } catch (error) {
      this.sql.exec('ROLLBACK');
      throw error;
    } finally {
      this.transactionDepth = 0;
    }
  }

  async getAlarm(): Promise<number | null> {
    const rows = this.sql.exec('SELECT due FROM hub2_alarm WHERE singleton=1');
    const row = rows.toArray()[0];
    return row === undefined || row.due === null ? null : Number(row.due);
  }

  async setAlarm(due: number): Promise<void> {
    if (!Number.isSafeInteger(due)) throw new TypeError('alarm_due_not_safe_integer');
    if (due < 0 || due > Date.now() + ALARM_LIMIT_MS) throw new RangeError('alarm_due_out_of_range');
    this.sql.exec('INSERT OR REPLACE INTO hub2_alarm (singleton, due) VALUES (1, ?)', due);
  }

  async deleteAlarm(): Promise<void> {
    this.sql.exec('DELETE FROM hub2_alarm WHERE singleton=1');
  }
}
