import type { Env } from './env';
import type { SqlStorage } from '@cloudflare/workers-types';

/**
 * Independent off-Cloudflare backup export for the Hub Durable Object SQLite.
 *
 * Two read-only operator endpoints (wired in Hub.fetch via sessionControlAuth,
 * which already enforces mixed-authority refusal, cookie CSRF and the narrow
 * operator-token fallback):
 *
 * - GET /api/backup/export — consistent JSON dump of EVERY user table rows
 *   plus the full DDL schema (tables, indexes, triggers, views) inside one
 *   storage transaction, so the snapshot is internally consistent.
 * - GET /api/backup/status — table names + row counts only (cheap liveness
 *   proof for the restore drill without shipping the full dataset).
 *
 * Both require an admin caller (operator session, human credential or the
 * configured operator token) and are per-caller sliding-window rate-limited.
 * They never write business data; the only write is the rate-limit bucket row
 * (backup_rate, excluded from the export so no caller IP ever lands in a
 * backup file).
 *
 * Export size is bounded (per-table and total row caps); oversize exports
 * fail closed with 413 instead of risking the 128 MiB DO memory ceiling.
 */

const RATE_SCHEMA = `
CREATE TABLE IF NOT EXISTS backup_rate (
  bucket TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  expires INTEGER NOT NULL
);
`;

export interface BackupAuth {
  caller?: { isAdmin?: boolean; actor?: string };
  error?: Response;
}

const EXPORT_LIMIT_PER_HOUR = 30;
const STATUS_LIMIT_PER_HOUR = 120;
// Bounded chunked reads (single table slice): generous budget, since each
// call is capped to CHUNK_MAX_ROWS rows.
const CHUNK_LIMIT_PER_HOUR = 2000;
const CHUNK_MAX_ROWS = 5000;
const CHUNK_DEFAULT_ROWS = 500;

// Backup export API version. Bump on any BackupService behavior change so
// operators can tell which code a live (possibly long-lived) DO isolate is
// actually executing: it is reported by /api/backup/status.
// v5 (intake e27e0f9d): keyset chunk cursor (?after=<rowid>) ordered by
// rowid, ORDER BY rowid on offset reads, and status lists WITHOUT ROWID
// tables (those keep stable PK-ordered offset paging).
const BACKUP_API_VERSION = 5;

// DO memory is 128 MiB; materializing every table into JS objects peaks at
// ~2-3x the JSON size. Fail closed well below that ceiling.
const PER_TABLE_ROW_CAP = 200000;
const TOTAL_ROW_CAP = 500000;

// Ephemeral rate-limit state: never exported (would leak the caller/IP key).
const EXCLUDED_FROM_EXPORT = new Set(['backup_rate']);

// Cloudflare platform tables inside DO SQLite storage (e.g. _cf_METADATA)
// are not Hub data and are unreadable through the normal query path; like
// sqlite_% internals they must never enter the export. LIKE '_' is a
// wildcard, so the literal underscore is escaped.
const USER_TABLE_WHERE =
  "type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\'";
const SCHEMA_OBJECT_WHERE =
  "type IN ('table','index','trigger','view') AND name NOT LIKE 'sqlite_%'" +
  " AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\' AND sql IS NOT NULL";

export class ExportTooLarge extends Error {}

// Synthetic cursor column for keyset chunk reads; stripped from every row.
const ROWID_COL = '__hub_backup_rowid';

function isWithoutRowid(ddl: string): boolean {
  return /\bWITHOUT\s+ROWID\b/i.test(ddl);
}

export class ExportStageError extends Error {
  constructor(readonly stage: string, readonly table?: string) {
    super(`export_${stage}_failed${table ? ':' + table : ''}`);
  }
}

function quoteIdent(name: string): string {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error('unexpected_table_name');
  return '"' + name + '"';
}

export class BackupService {
  // env is reserved for future operator-config use; auth arrives per-request.
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private env: Env) {
    sql.exec(RATE_SCHEMA);
    void this.env;
  }

  init() {}

  /** User tables whose rows are exported (ephemeral state excluded). */
  private userTables(): { name: string; sql: string }[] {
    return (this.sql.exec(
      `SELECT name, sql FROM sqlite_master WHERE ${USER_TABLE_WHERE} ORDER BY name`
    ).toArray() as any[]).filter(t => !EXCLUDED_FROM_EXPORT.has(String(t.name)));
  }

  /** Full replayable DDL: tables + indexes + triggers + views (auto-indexes excluded). */
  private schemaObjects(): { name: string; sql: string }[] {
    return (this.sql.exec(
      `SELECT name, sql FROM sqlite_master WHERE ${SCHEMA_OBJECT_WHERE} ORDER BY name`
    ).toArray() as any[]).filter(t => !EXCLUDED_FROM_EXPORT.has(String(t.name)));
  }

  /**
   * JSON-safe sanitizer for export rows. SQLite/Workers values are almost
   * always JSON-native, but production data has contained values
   * JSON.stringify rejects outright. Everything converts explicitly:
   * BigInts and raw binary become documented lossless markers; anything
   * else unserializable (lazy-throwing accessors, unknown host objects,
   * over-deep or circular shapes) becomes an explicit unreadable/circular
   * marker instead of failing the whole backup. Conversions are counted so
   * the restore drill can verify them. Never throws. Encoding uses only web
   * primitives, no Node Buffer dependency inside the DO isolate.
   */
  private sanitizeValue(table: string, stats: { bigint: number; blob: number; unreadable: number }, depth: number, key: string, value: unknown, seen: WeakSet<object> = new WeakSet()): unknown {
    if (value === null || value === undefined) return null;
    const t = typeof value;
    if (t === 'bigint') {
      stats.bigint++;
      return { __hub_backup_bigint: (value as bigint).toString() };
    }
    if (t === 'string' || t === 'number' || t === 'boolean') return value;
    if (value instanceof ArrayBuffer) {
      stats.blob++;
      const bytes = new Uint8Array(value as ArrayBuffer);
      let binary = '';
      const CHUNK = 0x8000;
      for (let i = 0; i < bytes.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK) as any);
      }
      try {
        return { __hub_backup_blob_b64: btoa(binary) };
      } catch (err) {
        stats.unreadable++;
        return { __hub_backup_unreadable: key || true };
      }
    }
    if (depth > 50 || t !== 'object') {
      stats.unreadable++;
      return { __hub_backup_unreadable: key || t };
    }
    if (seen.has(value as object)) {
      stats.unreadable++;
      return { __hub_backup_circular: key || true };
    }
    seen.add(value as object);
    try {
      if (Array.isArray(value)) return (value as unknown[]).map((v, i) => this.sanitizeValue(table, stats, depth + 1, String(i), v, seen));
      const out: Record<string, unknown> = {};
      for (const k of Object.keys(value as Record<string, unknown>)) {
        let v: unknown;
        try {
          v = (value as Record<string, unknown>)[k];
        } catch (err) {
          stats.unreadable++;
          out[k] = { __hub_backup_unreadable: k };
          continue;
        }
        out[k] = this.sanitizeValue(table, stats, depth + 1, k, v, seen);
      }
      return out;
    } finally {
      seen.delete(value as object);
    }
  }

  /** Sanitize every row of one table into guaranteed JSON-safe values. */
  private sanitizeRows(table: string, rows: any[], stats: { bigint: number; blob: number; unreadable: number }): unknown[] {
    return rows.map((row, i) => {
      try {
        return this.sanitizeValue(table, stats, 0, String(i), row);
      } catch (err) {
        stats.unreadable++;
        return { __hub_backup_unreadable: i };
      }
    });
  }

  /** Chunked per-table read: bounded rows per request so giant tables (and
   * giant exports) never exceed DO memory. Each chunk is internally
   * consistent (single transaction).
   *
   * Cursor modes (intake e27e0f9d):
   * - keyset (`after` set, rowid tables only): rows with rowid > after in
   *   rowid order; `next_after` is the last rowid served. Concurrent inserts
   *   land after the cursor and deletes never shift unread rows, so a hot
   *   table can neither skip nor duplicate rows across chunks.
   * - offset (legacy): ORDER BY rowid for rowid tables (WITHOUT ROWID tables
   *   scan in primary-key order). A delete in an already-read range shifts
   *   later rows down and can skip one, which is why the backup script uses
   *   keyset whenever the table supports it and re-reads on drift. */
  private tableChunk(table: string, cursor: { offset: number } | { after: number }, limit: number): {
    exported_at: string;
    snapshot: string;
    table: string;
    offset?: number;
    after?: number;
    next_after?: number | null;
    cursor: 'offset' | 'rowid';
    limit: number;
    total: number;
    ddl: string;
    rows: unknown[];
    value_markers: { bigint: number; blob: number; unreadable: number };
  } {
    return this.tx(() => {
      const names = new Set(this.userTables().map(t => String(t.name)));
      if (!names.has(table)) throw new ExportStageError('table', table);
      const total = Number((this.sql.exec(`SELECT COUNT(*) AS n FROM ${quoteIdent(table)}`).one() as any).n);
      if (!Number.isInteger(limit) || limit < 1 || limit > CHUNK_MAX_ROWS) {
        throw new ExportStageError('range', table);
      }
      const ddlRow = (this.schemaObjects().filter(t => String(t.name) === table)[0] as any);
      const ddl = String(ddlRow?.sql || '');
      const withoutRowid = isWithoutRowid(ddl);
      const keyset = 'after' in cursor;
      if (keyset) {
        if (!Number.isSafeInteger(cursor.after)) throw new ExportStageError('range', table);
        if (withoutRowid) throw new ExportStageError('keyset', table);
      } else if (!Number.isInteger(cursor.offset) || cursor.offset < 0 || cursor.offset > total) {
        throw new ExportStageError('range', table);
      }
      const q = quoteIdent(table);
      let rows: any[];
      try {
        if (keyset) {
          rows = this.sql.exec(
            `SELECT _rowid_ AS ${ROWID_COL}, * FROM ${q} WHERE _rowid_ > ? ORDER BY _rowid_ LIMIT ?`,
            (cursor as { after: number }).after, limit).toArray() as any[];
        } else {
          const order = withoutRowid ? '' : ' ORDER BY _rowid_';
          rows = this.sql.exec(`SELECT * FROM ${q}${order} LIMIT ? OFFSET ?`,
            limit, (cursor as { offset: number }).offset).toArray() as any[];
        }
      } catch (err) {
        throw new ExportStageError('export', table);
      }
      let nextAfter: number | null = null;
      if (keyset) {
        for (const row of rows) {
          nextAfter = Number(row[ROWID_COL]);
          delete row[ROWID_COL];
        }
      }
      const stats = { bigint: 0, blob: 0, unreadable: 0 };
      const clean = this.sanitizeRows(table, rows, stats);
      const position = keyset
        ? { cursor: 'rowid' as const, after: (cursor as { after: number }).after, next_after: nextAfter }
        : { cursor: 'offset' as const, offset: (cursor as { offset: number }).offset };
      return {
        exported_at: new Date().toISOString(), snapshot: 'hub-do-sqlite',
        table, ...position, limit, total, ddl, rows: clean, value_markers: stats,
      };
    });
  }

  /** Full replayable schema (tables, indexes, triggers, views) without rows. */
  private schemaDump(): { exported_at: string; snapshot: string; schema: Record<string, string> } {
    return this.tx(() => {
      const schema: Record<string, string> = {};
      let objects: { name: string; sql: string }[];
      try {
        objects = this.schemaObjects();
      } catch (err) {
        throw new ExportStageError('schema');
      }
      for (const t of objects) schema[String(t.name)] = String(t.sql || '');
      return { exported_at: new Date().toISOString(), snapshot: 'hub-do-sqlite', schema };
    });
  }

  /**
   * Encode the export snapshot incrementally per table. A single
   * Response.json() over the whole snapshot cannot name the table whose
   * rows fail to serialize, so each table is stringified separately with a
   * marker-preserving replacer and failures carry the table name. The wire
   * shape matches a single-shot encoding plus a transparent value_markers
   * count (documented in docs/HUB-DB-BACKUP.md).
   */
  private encodeExport(payload: {
    exported_at: string;
    snapshot: string;
    schema: Record<string, string>;
    row_counts: Record<string, number>;
    tables: Record<string, any[]>;
  }): string {
    const stats = { bigint: 0, blob: 0, unreadable: 0 };
    const encodedTables: string[] = [];
    let first = true;
    for (const name of Object.keys(payload.tables)) {
      let sanitized: unknown;
      try {
        sanitized = this.sanitizeRows(name, payload.tables[name], stats);
      } catch (err) {
        if (err instanceof ExportStageError) throw err;
        throw new ExportStageError('serialize', name);
      }
      let encoded: string;
      try {
        encoded = JSON.stringify(sanitized);
      } catch (err) {
        throw new ExportStageError('serialize', name);
      }
      encodedTables.push((first ? '' : ',') + JSON.stringify(name) + ':' + encoded);
      first = false;
    }
    let body: string;
    try {
      body =
        '{"exported_at":' + JSON.stringify(payload.exported_at) +
        ',"snapshot":' + JSON.stringify(payload.snapshot) +
        ',"schema":' + JSON.stringify(payload.schema) +
        ',"row_counts":' + JSON.stringify(payload.row_counts) +
        ',"value_markers":' + JSON.stringify(stats) +
        ',"tables":{' + encodedTables.join('') + '}}';
    } catch (err) {
      throw new ExportStageError('serialize');
    }
    return body;
  }
  private callerKey(request: Request, auth: BackupAuth): string {
    const actor = typeof auth.caller?.actor === 'string' ? auth.caller.actor : '';
    if (/^[A-Za-z0-9:_.-]{1,160}$/.test(actor)) return 'actor:' + actor;
    const ip = request.headers.get('CF-Connecting-IP') || 'local';
    if (/^[A-Za-z0-9.:]{1,64}$/.test(ip)) return 'ip:' + ip;
    return 'ip:local';
  }

  /** Sliding-window per-caller rate limit; true when the request must be refused. */
  private rateLimited(request: Request, auth: BackupAuth, kind: string, limit: number): boolean {
    try {
      const now = Date.now();
      const bucket = `backup:${kind}:${this.callerKey(request, auth)}`;
      // .one() throws on zero rows, so read via toArray: a missing row is simply
      // an expired (never-used) window.
      const row = (this.sql.exec('SELECT count, expires FROM backup_rate WHERE bucket=?', bucket).toArray() as any[])[0] as any;
      if (!row || Number(row.expires) <= now) {
        this.sql.exec(
          'INSERT INTO backup_rate(bucket,count,expires) VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=1, expires=excluded.expires',
          bucket, now + 3600000
        );
        return false;
      }
      const next = Number(row.count) + 1;
      this.sql.exec('UPDATE backup_rate SET count=? WHERE bucket=?', next, bucket);
      return next > limit;
    } catch (err) {
      throw new ExportStageError('rate-limit');
    }
  }

  private snapshot(): { exported_at: string; tables: string[]; row_counts: Record<string, number>; without_rowid: string[]; api_version: number } {
    return this.tx(() => {
      const names: string[] = [];
      const counts: Record<string, number> = {};
      const withoutRowid: string[] = [];
      let tables: { name: string; sql: string }[];
      try {
        tables = this.userTables();
      } catch (err) {
        throw new ExportStageError('tables');
      }
      for (const t of tables) {
        const name = String(t.name);
        try {
          names.push(name);
          if (isWithoutRowid(String(t.sql || ''))) withoutRowid.push(name);
          counts[name] = Number((this.sql.exec(`SELECT COUNT(*) AS n FROM ${quoteIdent(name)}`).one() as any).n);
        } catch (err) {
          throw new ExportStageError('snapshot', name);
        }
      }
      return { exported_at: new Date().toISOString(), tables: names, row_counts: counts, without_rowid: withoutRowid, api_version: BACKUP_API_VERSION };
    });
  }

  private fullExport(): {
    exported_at: string;
    snapshot: string;
    schema: Record<string, string>;
    row_counts: Record<string, number>;
    tables: Record<string, any[]>;
  } {
    return this.tx(() => {
      const schema: Record<string, string> = {};
      let objects: { name: string; sql: string }[];
      try {
        objects = this.schemaObjects();
      } catch (err) {
        throw new ExportStageError('schema');
      }
      for (const t of objects) {
        schema[String(t.name)] = String(t.sql || '');
      }
      const row_counts: Record<string, number> = {};
      const data: Record<string, any[]> = {};
      let userTables: { name: string; sql: string }[];
      try {
        userTables = this.userTables();
      } catch (err) {
        throw new ExportStageError('tables');
      }
      let total = 0;
      for (const t of userTables) {
        const name = String(t.name);
        let rows: any[];
        try {
          rows = this.sql.exec(`SELECT * FROM ${quoteIdent(name)}`).toArray() as any[];
        } catch (err) {
          throw new ExportStageError('export', name);
        }
        if (rows.length > PER_TABLE_ROW_CAP) throw new ExportTooLarge(`table_too_large:${name}`);
        total += rows.length;
        if (total > TOTAL_ROW_CAP) throw new ExportTooLarge('export_too_large');
        data[name] = rows;
        row_counts[name] = rows.length;
      }
      return { exported_at: new Date().toISOString(), snapshot: 'hub-do-sqlite', schema, row_counts, tables: data };
    });
  }

  handle(request: Request, auth: BackupAuth): Response {
    if (auth.error) return auth.error;
    if (!auth.caller?.isAdmin) {
      return Response.json({ error: 'forbidden', reason: 'operator_required' }, { status: 403 });
    }
    const url = new URL(request.url);
    try {
      if (url.pathname === '/api/backup/status') {
        if (request.method !== 'GET') return Response.json({ error: 'method_not_allowed' }, { status: 405 });
        if (this.rateLimited(request, auth, 'status', STATUS_LIMIT_PER_HOUR)) {
          return Response.json({ error: 'rate_limited' }, { status: 429 });
        }
        return Response.json({ ...this.snapshot(), snapshot: 'hub-do-sqlite' },
          { headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' } });
      }
      if (url.pathname === '/api/backup/export') {
        if (request.method !== 'GET') return Response.json({ error: 'method_not_allowed' }, { status: 405 });
        // Chunked per-table reads (?table=&offset=&limit=) and the schema
        // dump (?schema=1) are bounded work per call: they share a generous
        // per-caller budget. The unbounded full dump keeps the tight budget.
        const tableParam = url.searchParams.get('table');
        const schemaOnly = url.searchParams.get('schema');
        if (tableParam !== null || schemaOnly !== null) {
          if (this.rateLimited(request, auth, 'chunk', CHUNK_LIMIT_PER_HOUR)) {
            return Response.json({ error: 'rate_limited' }, { status: 429 });
          }
          if (schemaOnly !== null) {
            if (schemaOnly !== '1') return Response.json({ error: 'invalid_schema_flag' }, { status: 400 });
            if (tableParam !== null) return Response.json({ error: 'invalid_combination' }, { status: 400 });
            return Response.json(this.schemaDump(),
              { headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' } });
          }
          const table = String(tableParam);
          const afterParam = url.searchParams.get('after');
          const offsetParam = url.searchParams.get('offset');
          const limit = Number(url.searchParams.get('limit') ?? String(CHUNK_DEFAULT_ROWS));
          if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(table)) {
            return Response.json({ error: 'invalid_table' }, { status: 400 });
          }
          if (afterParam !== null && offsetParam !== null) {
            return Response.json({ error: 'invalid_combination' }, { status: 400 });
          }
          if (afterParam !== null && !/^-?[0-9]{1,16}$/.test(afterParam)) {
            return Response.json({ error: 'invalid_range' }, { status: 400 });
          }
          const cursor = afterParam !== null
            ? { after: Number(afterParam) }
            : { offset: Number(offsetParam ?? '0') };
          try {
            return Response.json(this.tableChunk(table, cursor, limit),
              { headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' } });
          } catch (err) {
            if (err instanceof ExportStageError && err.stage === 'table') {
              return Response.json({ error: 'unknown_table' }, { status: 404 });
            }
            if (err instanceof ExportStageError && err.stage === 'range') {
              return Response.json({ error: 'invalid_range' }, { status: 400 });
            }
            if (err instanceof ExportStageError && err.stage === 'keyset') {
              return Response.json({ error: 'keyset_unsupported' }, { status: 400 });
            }
            throw err;
          }
        }
        if (this.rateLimited(request, auth, 'export', EXPORT_LIMIT_PER_HOUR)) {
          return Response.json({ error: 'rate_limited' }, { status: 429 });
        }
        const payload = this.fullExport();
        let body: string;
        try {
          body = this.encodeExport(payload);
        } catch (err) {
          if (err instanceof ExportStageError) throw err;
          throw new ExportStageError('serialize');
        }
        return new Response(body, {
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': 'private, no-store',
            'X-Content-Type-Options': 'nosniff',
          },
        });
      }
    } catch (err) {
      if (err instanceof ExportTooLarge) {
        return Response.json({ error: 'export_too_large' }, { status: 413 });
      }
      // Fail closed with the failing stage (and table, when per-table).
      // Table names are schema identifiers, never row content.
      if (err instanceof ExportStageError) {
        return Response.json(
          { error: 'export_failed', stage: err.stage, ...(err.table ? { table: err.table } : {}) },
          { status: 500 });
      }
      return Response.json({ error: 'export_failed' }, { status: 500 });
    }
    return Response.json({ error: 'not_found' }, { status: 404 });
  }
}
