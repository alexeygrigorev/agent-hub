import { AsyncLocalStorage } from 'node:async_hooks';
import type { SqlStorage } from '@cloudflare/workers-types';
import { rewriteSchemaProbe } from './sql-probes';

/**
 * Durable Object SQL rows-read/rows-written meter (intake 8081a05a).
 *
 * Cloudflare bills DO storage by rows read and rows written, and a cursor
 * reports both (`SqlStorageCursor.rowsRead/rowsWritten`) once consumed. The
 * meter wraps `ctx.storage.sql` so every `exec` cursor is attributed to the
 * current scope: an alarm step (`alarm:<step>`), an HTTP route
 * (`route:<METHOD> <normalized path>`) or `unscoped` (constructor/init and
 * anything outside a scope). Scopes nest; the innermost one owns the cursor.
 *
 * Totals are kept in memory and flushed once per alarm into a bounded ring
 * table (`rows_read_stats`, one row per hour slot and scope, 8 days of slots,
 * stale slots overwritten in place, nothing ever deleted). The flush itself
 * is metered as `meter:flush`.
 */

export type RowsTotals = { calls: number; rows_read: number; rows_written: number; max_read: number };

type Scope = { name: string; cursors: any[]; queries: string[] };

export const RING_HOURS = 192;
const MAX_SCOPES = 300;
const SLOW_SCOPE_LOG_ROWS = 50_000;
// Statements reading at least this many rows are logged with their SQL.
// Overridable for local replays via globalThis.__ROWS_METER_STATEMENT_LOG.
const SLOW_STATEMENT_LOG_ROWS = 5_000;
const statementLogRows = () => Number((globalThis as any).__ROWS_METER_STATEMENT_LOG) || SLOW_STATEMENT_LOG_ROWS;
export const DEFAULT_ROWS_READ_ALERT_PER_DAY = 20_000;
export const ROWS_READ_TARGET_PER_DAY = 5_000;

export const ROWS_STATS_SCHEMA = `
CREATE TABLE IF NOT EXISTS rows_read_stats (
  slot INTEGER NOT NULL,
  scope TEXT NOT NULL,
  hour INTEGER NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  rows_read INTEGER NOT NULL DEFAULT 0,
  rows_written INTEGER NOT NULL DEFAULT 0,
  max_read INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (slot, scope)
);
CREATE INDEX IF NOT EXISTS rows_read_stats_hour ON rows_read_stats(hour);
`;

/** `/api/sessions/3f2a.../send` -> `/api/sessions/:id/send`. Keeps scopes bounded. */
export function normalizeRoute(method: string, path: string): string {
  const parts = path.split('/').slice(0, 7).map(seg => {
    if (!seg) return seg;
    if (/^[0-9]+$/.test(seg)) return ':n';
    if (/^[0-9a-f]{8}-[0-9a-f-]{20,}$/i.test(seg)) return ':id';
    if (/[0-9]/.test(seg)) return ':id'; // ids, short ids, slugs: scopes stay a fixed set of route templates
    if (seg.length > 40 || /[^A-Za-z0-9_.-]/.test(seg)) return ':id';
    return seg;
  });
  return `${method.toUpperCase()} ${parts.join('/') || '/'}`;
}

export class RowsMeter {
  private als = new AsyncLocalStorage<Scope>();
  private unscoped: Scope = { name: 'unscoped', cursors: [], queries: [] };
  private totals = new Map<string, RowsTotals>();
  readonly startedAt: number;

  constructor(private now: () => number = Date.now) {
    this.startedAt = now();
  }

  wrap(sql: SqlStorage): SqlStorage {
    const meter = this;
    let probes = true;
    return new Proxy(sql as any, {
      get(target, prop) {
        if (prop === 'exec') {
          return (query: string, ...bindings: any[]) => {
            // Table-existence probes become indexed pragma lookups; should the
            // runtime ever refuse the pragma, fall back to the original once and for all.
            const probe = probes ? rewriteSchemaProbe(query) : null;
            let cursor: any;
            if (probe) {
              try { cursor = target.exec(probe, ...bindings); }
              catch (e) { probes = false; console.warn(JSON.stringify({ event: 'schema_probe_rewrite_disabled', error: String((e as any)?.message || e).slice(0, 200) })); }
            }
            cursor ??= target.exec(query, ...bindings);
            meter.track(cursor, query);
            return cursor;
          };
        }
        const value = target[prop];
        return typeof value === 'function' ? value.bind(target) : value;
      },
    }) as SqlStorage;
  }

  private track(cursor: any, query: string) {
    const scope = this.als.getStore() ?? this.unscoped;
    scope.cursors.push(cursor);
    scope.queries.push(query);
    // Long-lived unscoped work (constructor migrations) is settled eagerly
    // so the list never grows without bound between flushes.
    if (scope === this.unscoped && scope.cursors.length > 500) this.settle(scope);
  }

  private settle(scope: Scope) {
    let read = 0, written = 0;
    scope.cursors.forEach((c, i) => {
      let r = 0;
      try { r = Number(c.rowsRead) || 0; read += r; written += Number(c.rowsWritten) || 0; } catch {}
      // Name the statement behind a heavy read so the hot path is visible in logs.
      if (r >= statementLogRows()) console.warn(JSON.stringify({ event: 'do_rows_read_statement', scope: scope.name, rows_read: r, sql: String(scope.queries[i] ?? '').replace(/\s+/g, ' ').slice(0, 160) }));
    });
    const calls = scope.cursors.length;
    scope.cursors = [];
    scope.queries = [];
    if (!calls) return;
    this.add(scope.name, calls, read, written);
  }

  private add(name: string, calls: number, read: number, written: number) {
    let key = name;
    if (!this.totals.has(key) && this.totals.size >= MAX_SCOPES) key = 'other';
    const t = this.totals.get(key) ?? { calls: 0, rows_read: 0, rows_written: 0, max_read: 0 };
    t.calls += calls; t.rows_read += read; t.rows_written += written; t.max_read = Math.max(t.max_read, read);
    this.totals.set(key, t);
    if (read >= SLOW_SCOPE_LOG_ROWS) console.warn(JSON.stringify({ event: 'do_rows_read_heavy', scope: name, rows_read: read, calls }));
  }

  async run<T>(name: string, fn: () => Promise<T> | T): Promise<T> {
    const scope: Scope = { name, cursors: [], queries: [] };
    try { return await this.als.run(scope, fn); } finally { this.settle(scope); }
  }

  runSync<T>(name: string, fn: () => T): T {
    const scope: Scope = { name, cursors: [], queries: [] };
    try { return this.als.run(scope, fn); } finally { this.settle(scope); }
  }

  /** In-memory totals since the last flush (settles unscoped first). */
  snapshot(): Record<string, RowsTotals> {
    this.settle(this.unscoped);
    return Object.fromEntries([...this.totals].map(([k, v]) => [k, { ...v }]));
  }

  /** Upsert in-memory totals into the hour ring and reset them. */
  flush(sql: SqlStorage) {
    this.settle(this.unscoped);
    if (!this.totals.size) return;
    const pending = [...this.totals];
    this.totals.clear();
    this.runSync('meter:flush', () => {
      const hour = Math.floor(this.now() / 3_600_000);
      const slot = hour % RING_HOURS;
      let read = 0, written = 0, calls = 0;
      for (const [, t] of pending) { read += t.rows_read; written += t.rows_written; calls += t.calls; }
      for (const [scope, t] of [...pending, ['__total__', { calls, rows_read: read, rows_written: written, max_read: 0 }] as [string, RowsTotals]]) {
        sql.exec(`INSERT INTO rows_read_stats (slot, scope, hour, calls, rows_read, rows_written, max_read) VALUES (?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(slot, scope) DO UPDATE SET
            calls = CASE WHEN hour = excluded.hour THEN calls + excluded.calls ELSE excluded.calls END,
            rows_read = CASE WHEN hour = excluded.hour THEN rows_read + excluded.rows_read ELSE excluded.rows_read END,
            rows_written = CASE WHEN hour = excluded.hour THEN rows_written + excluded.rows_written ELSE excluded.rows_written END,
            max_read = CASE WHEN hour = excluded.hour THEN MAX(max_read, excluded.max_read) ELSE excluded.max_read END,
            hour = excluded.hour`,
          slot, scope, hour, t.calls, t.rows_read, t.rows_written, t.max_read);
      }
    });
  }
}

/** Bounded report over the last `hours` complete+current hours (index on hour). */
export function rowsReadReport(sql: SqlStorage, nowMs: number, hours = 24, pending: Record<string, RowsTotals> = {}) {
  const h = Math.max(1, Math.min(RING_HOURS - 1, Math.floor(hours)));
  const current = Math.floor(nowMs / 3_600_000);
  const rows = sql.exec('SELECT scope, hour, calls, rows_read, rows_written, max_read FROM rows_read_stats WHERE hour > ? AND hour <= ? LIMIT 20000', current - h, current).toArray() as any[];
  const byScope = new Map<string, RowsTotals>();
  const hourly = new Map<number, number>();
  for (const r of rows) {
    if (r.scope === '__total__') { hourly.set(r.hour, (hourly.get(r.hour) ?? 0) + Number(r.rows_read)); continue; }
    const t = byScope.get(r.scope) ?? { calls: 0, rows_read: 0, rows_written: 0, max_read: 0 };
    t.calls += Number(r.calls); t.rows_read += Number(r.rows_read); t.rows_written += Number(r.rows_written); t.max_read = Math.max(t.max_read, Number(r.max_read));
    byScope.set(r.scope, t);
  }
  let pendingRead = 0;
  for (const [scope, p] of Object.entries(pending)) {
    pendingRead += p.rows_read;
    const t = byScope.get(scope) ?? { calls: 0, rows_read: 0, rows_written: 0, max_read: 0 };
    t.calls += p.calls; t.rows_read += p.rows_read; t.rows_written += p.rows_written; t.max_read = Math.max(t.max_read, p.max_read);
    byScope.set(scope, t);
  }
  hourly.set(current, (hourly.get(current) ?? 0) + pendingRead);
  const total = [...hourly.values()].reduce((a, b) => a + b, 0);
  const lastFull = hourly.get(current - 1) ?? null;
  const scopes = [...byScope].map(([scope, t]) => ({ scope, ...t })).sort((a, b) => b.rows_read - a.rows_read).slice(0, 100);
  return {
    window_hours: h,
    rows_read: total,
    projected_rows_read_per_day: lastFull === null ? null : lastFull * 24,
    last_full_hour_rows_read: lastFull,
    hourly: [...hourly].sort((a, b) => a[0] - b[0]).map(([hour, rows_read]) => ({ hour_start: new Date(hour * 3_600_000).toISOString(), rows_read })),
    scopes,
  };
}

export function rowsReadAlertThreshold(env: any): number {
  const raw = Number(env?.HUB_ROWS_READ_ALERT_PER_DAY);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_ROWS_READ_ALERT_PER_DAY;
}
