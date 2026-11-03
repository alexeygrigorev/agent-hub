import type { SqlStorage, SqlStorageCursor, SqlStorageValue } from '@cloudflare/workers-types';

/**
 * In-memory per-table write generations (intake 8081a05a/e8a22f4c).
 *
 * The catalog caches used to check their freshness with a one-row read of a
 * trigger-maintained revision table on every hot call; at host poll rates
 * even that check, and the constant invalidation by unrelated writers, made
 * routes re-read thousands of rows per poll. A write-through cache does not
 * need a SQL freshness probe at all: the DO executes every statement itself,
 * so it can bump an in-memory generation when a statement writes a table.
 * A cache keyed on versions of the tables it reads is then
 * exactly as fresh as the database - data only changes through writes - and
 * checking it reads zero rows.
 *
 * Deliberate over-invalidation (never under): a rolled-back transaction also
 * bumps generations, and schema DDL bumps every version. Statement text
 * decides which tables bump, including trigger destinations observed at
 * trigger creation; column-level granularity is intentionally out of scope.
 */

const DDL_RE = /^\s*(CREATE|DROP|ALTER|VACUUM)\b/i;
const READ_RE = /^\s*(SELECT|WITH)\b/i;
const WRITE_TARGET_RE = /\b(?:INSERT\s+(?:OR\s+[A-Z]+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+[A-Z]+)?|DELETE\s+FROM)\s+(?:(?:main|temp)\s*\.\s*)?(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_$]*))/gi;
const TABLE_READ_RE = /\b(?:FROM|JOIN)\s+(?:(?:main|temp)\s*\.\s*)?(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_$]*))/gi;
const MAX_MEMO_ENTRIES = 512;
const MAX_MEMO_ROWS = 2_000;
const MAX_MEMO_ENTRY_BYTES = 256 * 1024;
const MAX_MEMO_BYTES = 2 * 1024 * 1024;

type MemoEntry = { tables: string[]; stamp: string; columns: string[]; rows: any[]; bytes: number };

function tableName(match: RegExpExecArray): string {
  return String(match[1] ?? match[2] ?? match[3] ?? match[4] ?? '').toLowerCase();
}

function referencedTables(query: string): string[] {
  const names = new Set<string>();
  TABLE_READ_RE.lastIndex = 0;
  for (let match = TABLE_READ_RE.exec(query); match; match = TABLE_READ_RE.exec(query)) {
    const name = tableName(match);
    if (name) names.add(name);
  }
  return [...names].sort();
}

/** A comma at the same SQL nesting level as a FROM starts another source.
 * TABLE_READ_RE covers FROM/JOIN, but it deliberately does not attempt to
 * parse comma joins; those statements stay on the ordinary SQL path so every
 * dependency is observed. String and quoted identifier contents are skipped. */
function hasCommaSource(query: string): boolean {
  let depth = 0;
  const fromDepth = new Set<number>();
  const endFrom = new Set(['WHERE', 'GROUP', 'ORDER', 'LIMIT', 'HAVING', 'WINDOW', 'UNION', 'EXCEPT', 'INTERSECT', 'RETURNING']);
  for (let i = 0; i < query.length;) {
    const ch = query[i];
    if (ch === "'") {
      i++;
      while (i < query.length) {
        if (query[i++] === "'" && query[i] !== "'") break;
        if (query[i - 1] === "'" && query[i] === "'") i++;
      }
      continue;
    }
    if (ch === '"' || ch === '`' || ch === '[') {
      const close = ch === '[' ? ']' : ch;
      i++;
      while (i < query.length) {
        if (query[i++] === close) break;
      }
      continue;
    }
    if (ch === '(') { depth++; i++; continue; }
    if (ch === ')') { fromDepth.delete(depth); depth = Math.max(0, depth - 1); i++; continue; }
    if (ch === ',' && fromDepth.has(depth)) return true;
    if (/[A-Za-z_]/.test(ch)) {
      const start = i++;
      while (i < query.length && /[A-Za-z0-9_$]/.test(query[i])) i++;
      const word = query.slice(start, i).toUpperCase();
      if (word === 'FROM') fromDepth.add(depth);
      else if (endFrom.has(word)) fromDepth.delete(depth);
      continue;
    }
    i++;
  }
  return false;
}

function writeTargets(query: string): string[] {
  const names = new Set<string>();
  WRITE_TARGET_RE.lastIndex = 0;
  for (let match = WRITE_TARGET_RE.exec(query); match; match = WRITE_TARGET_RE.exec(query)) {
    const name = tableName(match);
    if (name) names.add(name);
  }
  return [...names];
}

function cacheKey(query: string, bindings: any[]): string | null {
  const stable = bindings.map(value => {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return [typeof value, value];
    if (typeof value === 'number' && Number.isFinite(value)) return ['number', Object.is(value, -0) ? 0 : value];
    // ArrayBuffer bindings are uncommon and need a binary-safe key. Leave them
    // on the ordinary SQL path rather than risking collisions or large copies.
    return null;
  });
  if (stable.some(value => value === null)) return null;
  const key = JSON.stringify([query, stable]);
  return key.length <= 4096 ? key : null;
}

function memoSafe(query: string): boolean {
  if (!READ_RE.test(query)) return false;
  // Comments can split a FROM/JOIN from its source and defeat dependency
  // tracking. Multi-statement text is never sent through the memo path.
  if (/--|\/\*/.test(query) || query.includes(';') || hasCommaSource(query)) return false;
  // A cache must never freeze time, randomness, or connection-local SQL state.
  if (/\b(?:CURRENT_TIME|CURRENT_DATE|CURRENT_TIMESTAMP)\b|\b(?:random|randomblob|changes|total_changes|last_insert_rowid)\s*\(|\b(?:date|time|datetime|julianday|unixepoch|strftime)\s*\([^)]*['"]now['"]/i.test(query)) return false;
  // DML is not cacheable even when nested after a CTE. Words in comments or
  // string literals can only disable caching, which is a safe false positive.
  if (/\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM)\b/i.test(query)) return false;
  // These table-valued functions are pure over their SQL arguments. Other
  // FROM/JOIN functions can expose mutable or connection-local state, so do
  // not memoize them without an explicit dependency contract.
  const tableFunctions = /\b(?:FROM|JOIN)\s+(?:(?:main|temp)\s*\.\s*)?(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_$]*))\s*\(/gi;
  for (let match = tableFunctions.exec(query); match; match = tableFunctions.exec(query)) {
    const name = tableName(match);
    if (name !== 'json_each' && name !== 'json_tree') return false;
  }
  return true;
}

function copyValue(value: any): any {
  return value instanceof ArrayBuffer ? value.slice(0) : value;
}

function copyRow(row: any): any {
  const out: Record<string, any> = {};
  for (const [key, value] of Object.entries(row)) out[key] = copyValue(value);
  return out;
}

function rowBytes(row: any): number | null {
  let bytes = 32;
  for (const value of Object.values(row)) {
    if (value === null) bytes += 1;
    else if (typeof value === 'string') bytes += value.length * 2;
    else if (typeof value === 'number') bytes += 8;
    else if (value instanceof ArrayBuffer) bytes += value.byteLength;
    else return null;
  }
  return bytes;
}

/** Cursor backed by a completed in-memory read; row values are copied so a
 * caller mutating a response cannot alter the cached answer. */
class MemoCursor<T extends Record<string, SqlStorageValue>> {
  private at = 0;
  readonly rowsRead = 0;
  readonly rowsWritten = 0;
  readonly columnNames: string[];

  constructor(private rows: T[], columns: string[]) { this.columnNames = [...columns]; }

  next(): IteratorResult<T, undefined> {
    if (this.at >= this.rows.length) return { done: true, value: undefined };
    return { done: false, value: copyRow(this.rows[this.at++]) as T };
  }

  toArray(): T[] {
    const out = this.rows.slice(this.at).map(row => copyRow(row) as T);
    this.at = this.rows.length;
    return out;
  }

  one(): T {
    if (this.rows.length !== 1) throw new Error(`Expected exactly one row, got ${this.rows.length}`);
    this.at = this.rows.length;
    return copyRow(this.rows[0]) as T;
  }

  raw<U extends SqlStorageValue[]>(): IterableIterator<U> {
    const cursor = this;
    return {
      next(): IteratorResult<U, undefined> {
        const result = cursor.next();
        if (result.done) return { done: true, value: undefined };
        return { done: false, value: cursor.columnNames.map(name => copyValue((result.value as any)[name])) as U };
      },
      [Symbol.iterator]() { return this; },
    } as IterableIterator<U>;
  }

  [Symbol.iterator](): IterableIterator<T> { return this; }
}

/** Pass-through cursor that remembers a complete bounded read once consumed. */
class CaptureCursor<T extends Record<string, SqlStorageValue>> {
  private captured: T[] = [];
  private bytes = 0;
  private canMemo = true;
  private completed = false;
  readonly columnNames: string[];

  constructor(private cursor: any, private remember: (rows: T[], columns: string[], bytes: number) => void) {
    this.columnNames = [...(cursor.columnNames ?? [])];
    if (new Set(this.columnNames).size !== this.columnNames.length) this.canMemo = false;
  }

  private capture(row: T) {
    if (!this.canMemo) return;
    const size = rowBytes(row);
    if (size === null || this.captured.length >= MAX_MEMO_ROWS || this.bytes + size > MAX_MEMO_ENTRY_BYTES) {
      this.canMemo = false;
      this.captured = [];
      this.bytes = 0;
      return;
    }
    this.captured.push(copyRow(row) as T);
    this.bytes += size;
  }

  private finish() {
    if (this.completed) return;
    this.completed = true;
    if (this.canMemo) this.remember(this.captured, this.columnNames, this.bytes);
  }

  get rowsRead(): number { return Number(this.cursor.rowsRead) || 0; }
  get rowsWritten(): number { return Number(this.cursor.rowsWritten) || 0; }
  next(): IteratorResult<T, undefined> {
    const result = this.cursor.next();
    if (result.done) this.finish();
    else this.capture(result.value);
    return result;
  }
  toArray(): T[] {
    const rows = this.cursor.toArray();
    for (const row of rows) this.capture(row);
    this.finish();
    return rows;
  }
  one(): T { return this.cursor.one(); }
  raw<U extends SqlStorageValue[]>(): IterableIterator<U> {
    const source: IterableIterator<U> = this.cursor.raw();
    const columns = this.columnNames;
    const self = this;
    return {
      next(): IteratorResult<U, undefined> {
        const result = source.next();
        if (result.done) { self.finish(); return { done: true, value: undefined }; }
        const row: Record<string, any> = {};
        columns.forEach((name, index) => { row[name] = result.value[index]; });
        self.capture(row as T);
        return result;
      },
      [Symbol.iterator]() { return this; },
    } as IterableIterator<U>;
  }
  [Symbol.iterator](): IterableIterator<T> { return this; }
}

export class WriteWatch {
  private generations = new Map<string, number>();
  private schemaGeneration = 0;
  private triggerTargets = new Map<string, Set<string>>();
  private memo = new Map<string, MemoEntry>();
  private memoBytes = 0;

  /** Record one executed statement. Cheap no-op for reads. */
  note(query: string, rowsWritten?: number): void {
    if (DDL_RE.test(query)) {
      this.schemaGeneration++;
      if (/^\s*CREATE\s+(?:TEMP\s+)?TRIGGER\b/i.test(query)) this.noteTrigger(query);
      return;
    }
    // SqlStorageCursor reports the executed statement's affected-row count.
    // Conditional UPSERTs that change nothing must not invalidate otherwise
    // reusable read caches. An omitted count (e.g. isolated manual callers)
    // conservatively assumes the statement wrote.
    if (rowsWritten === 0) return;
    const targets = writeTargets(query);
    if (!targets.length) return;
    const pending = [...targets];
    const changed = new Set<string>();
    while (pending.length) {
      const table = pending.pop()!;
      if (changed.has(table)) continue;
      changed.add(table);
      for (const downstream of this.triggerTargets.get(table) ?? []) pending.push(downstream);
    }
    for (const table of changed) this.generations.set(table, (this.generations.get(table) ?? 0) + 1);
  }

  private noteTrigger(query: string): void {
    const sourceMatch = /\bON\s+(?:(?:main|temp)\s*\.\s*)?(?:\[([^\]]+)\]|"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_$]*))/i.exec(query);
    const source = sourceMatch ? tableName(sourceMatch) : '';
    if (!source) return;
    const begin = /\bBEGIN\b/i.exec(query);
    const body = begin ? query.slice(begin.index + begin[0].length) : '';
    const targets = writeTargets(body);
    let affected = this.triggerTargets.get(source);
    if (!affected) this.triggerTargets.set(source, affected = new Set());
    for (const target of targets) if (target !== source) affected.add(target);
  }

  /** Exact freshness stamp of the given tables. A string, so two different
   * write histories can never compare equal (a numeric fold could lose
   * precision at these magnitudes and miss an invalidation). */
  stamp(...tables: string[]): string {
    let out = '';
    for (const t of tables) out += (this.generations.get(t.toLowerCase()) ?? 0) + '.';
    return out + this.schemaGeneration;
  }

  /** Register a table whose generation may be manually bumped. SQL writes are
   * versioned automatically, including tables not registered in advance. */
  track(table: string): void {
    const key = table.toLowerCase();
    if (!this.generations.has(key)) this.generations.set(key, 0);
  }

  trackAll(tables: Iterable<string>): void {
    for (const t of tables) this.track(t);
  }

  /** Manually bump a (pseudo-)table generation for write-through paths that
   * patch an in-memory catalog instead of letting statement text decide -
   * e.g. census metadata changes versus activity-only refreshes. */
  bump(table: string): void {
    const key = table.toLowerCase();
    this.generations.set(key, (this.generations.get(key) ?? 0) + 1);
  }

  /** Proxy one SqlStorage so every executed statement is observed. */
  wrap(sql: SqlStorage): SqlStorage {
    const watch = this;
    return new Proxy(sql as any, {
      get(target, prop) {
        if (prop === 'exec') {
          return (query: string, ...bindings: any[]) => {
            const statement = String(query);
            if (memoSafe(statement)) {
              const key = cacheKey(statement, bindings);
              const tables = referencedTables(statement);
              if (key !== null) {
                watch.trackAll(tables);
                const stamp = watch.stamp(...tables);
                const hit = watch.memo.get(key);
                if (hit && hit.stamp === stamp) {
                  watch.memo.delete(key);
                  watch.memo.set(key, hit);
                  return new MemoCursor(hit.rows, hit.columns) as unknown as SqlStorageCursor<any>;
                }
                if (hit) watch.deleteMemo(key);
                const cursor = target.exec(query, ...bindings);
                return new CaptureCursor(cursor, (rows, columns, bytes) => watch.rememberMemo(key, tables, stamp, rows, columns, bytes));
              }
            }
            const cursor = target.exec(query, ...bindings);
            let rowsWritten: number | undefined;
            try {
              const count = Number(cursor.rowsWritten);
              if (Number.isFinite(count)) rowsWritten = count;
            } catch { /* preserve conservative invalidation */ }
            watch.note(statement, rowsWritten);
            return cursor;
          };
        }
        const value = target[prop];
        return typeof value === 'function' ? value.bind(target) : value;
      },
    }) as SqlStorage;
  }

  private deleteMemo(key: string): void {
    const entry = this.memo.get(key);
    if (!entry) return;
    this.memo.delete(key);
    this.memoBytes -= entry.bytes;
  }

  private rememberMemo(key: string, tables: string[], expectedStamp: string, rows: any[], columns: string[], bytes: number): void {
    // A write can run after exec() but before the cursor is fully consumed.
    // Do not attach an old result to the new generation in that case.
    if (this.stamp(...tables) !== expectedStamp) return;
    if (rows.length > MAX_MEMO_ROWS || bytes > MAX_MEMO_ENTRY_BYTES) return;
    this.deleteMemo(key);
    while (this.memo.size >= MAX_MEMO_ENTRIES || this.memoBytes + bytes > MAX_MEMO_BYTES) {
      const oldest = this.memo.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      this.deleteMemo(oldest);
    }
    const safeRows = rows.map(row => copyRow(row));
    const entry = { tables, stamp: expectedStamp, columns: [...columns], rows: safeRows, bytes };
    this.memo.set(key, entry);
    this.memoBytes += bytes;
  }
}

/** A small memo keyed by an exact write stamp. `get` serves a rebuilt value
 * without touching SQL while no observed table is written; any write to a
 * watched table rebuilds it on the next call. Building may write; the stamp
 * is re-read after the build so those writes are not lost. Entries are
 * bounded by a clear (over-invalidation only). */
export class StampCache<K, V> {
  private entries = new Map<K, { stamp: string; value: V }>();
  constructor(private watch: WriteWatch, private tables: (key: K) => string[], private maxEntries = 64) {}

  get(key: K, build: () => V): V {
    const tables = this.tables(key);
    const hit = this.entries.get(key);
    if (hit && hit.stamp === this.watch.stamp(...tables)) return hit.value;
    const value = build();
    if (this.entries.size >= this.maxEntries) this.entries.clear();
    this.entries.set(key, { stamp: this.watch.stamp(...tables), value });
    return value;
  }

  invalidate(): void { this.entries.clear(); }
}
