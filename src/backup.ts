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
   * JSON replacer for export rows. SQLite/Workers values are almost always
   * JSON-native, but production data has contained values JSON.stringify
   * rejects outright (e.g. BigInts). Those are converted to explicit,
   * documented, lossless markers instead of failing the whole backup;
   * conversions are counted so the restore drill can verify them.
   * Raw binary stays explicit base64 rather than V8's lossy '{}'.
   */
  private exportReplacer(table: string, stats: { bigint: number; blob: number }) {
    return function (_key: string, value: unknown) {
      if (typeof value === 'bigint') {
        stats.bigint++;
        return { __hub_backup_bigint: (value as bigint).toString() };
      }
      if (value instanceof ArrayBuffer) {
        stats.blob++;
        let b64: string;
        try {
          b64 = Buffer.from(value as ArrayBuffer).toString('base64');
        } catch (err) {
          throw new ExportStageError('serialize', table);
        }
        return { __hub_backup_blob_b64: b64 };
      }
      return value as any;
    };
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
    const stats = { bigint: 0, blob: 0 };
    const encodedTables: string[] = [];
    let first = true;
    for (const name of Object.keys(payload.tables)) {
      let encoded: string;
      try {
        encoded = JSON.stringify(payload.tables[name], this.exportReplacer(name, stats));
      } catch (err) {
        if (err instanceof ExportStageError) throw err;
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

  private snapshot(): { exported_at: string; tables: string[]; row_counts: Record<string, number> } {
    return this.tx(() => {
      const names: string[] = [];
      const counts: Record<string, number> = {};
      for (const t of this.userTables()) {
        const name = String(t.name);
        try {
          names.push(name);
          counts[name] = Number((this.sql.exec(`SELECT COUNT(*) AS n FROM ${quoteIdent(name)}`).one() as any).n);
        } catch (err) {
          throw new ExportStageError('snapshot', name);
        }
      }
      return { exported_at: new Date().toISOString(), tables: names, row_counts: counts };
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
