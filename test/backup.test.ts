import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { sha256 } from '../src/auth/crypto';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Hub DB backup export (intake c1241bea): read-only, operator-auth only,
// rate-limited. Each test uses a fresh DO so the sliding-window rate budget
// never leaks between tests.

const stub = (name: string) => {
  const bindings = env as unknown as Env;
  return bindings.HUB.get(bindings.HUB.idFromName(name));
};

async function configureOperator(name: string, token: string) {
  // HUB_ADMIN_TOKEN_SHA256 is lowercase hex (see adminTokenHash); the
  // credentials table uses base64url sha256 instead. Copy the env object
  // instead of mutating the shared binding in place.
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
  const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  await runInDurableObject(stub(name), async (hub: Hub) => {
    hub.env = { ...hub.env, HUB_ADMIN_TOKEN_SHA256: hash } as Env;
  });
}

const call = (s: any, path: string, headers: Record<string, string> = {}, method = 'GET') =>
  s.fetch(new Request('https://hub.test' + path, { method, headers }));

async function listTables(name: string): Promise<string[]> {
  let names: string[] = [];
  await runInDurableObject(stub(name), async (hub: Hub) => {
    names = (hub.sql.exec(
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\' AND name != 'backup_rate' ORDER BY name"
    ).toArray() as any[]).map(r => String(r.name));
  });
  return names;
}

async function countAll(name: string): Promise<Record<string, number>> {
  let out: Record<string, number> = {};
  await runInDurableObject(stub(name), async (hub: Hub) => {
    const rows = hub.sql.exec(
      "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).toArray() as any[];
    for (const r of rows) {
      const t = String((r as any).name);
      out[t] = Number((hub.sql.exec(`SELECT COUNT(*) AS n FROM "${t}"`).one() as any).n);
    }
  });
  return out;
}

async function seedAgent(name: string): Promise<string> {
  const token = 'hubt_backup_fixture_' + name.replace(/[^A-Za-z0-9]/g, '').slice(-12);
  const hash = await sha256(token);
  await runInDurableObject(stub(name), async (hub: Hub) => {
    hub.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
    hub.sql.exec("INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES ('assistant','demo','fixture-host','2026-01-01T00:00:00.000Z')");
    hub.sql.exec(
      'INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      'cred-' + name, 'agent', hash, 'family-' + name, 'demo:assistant', 'fixture-host',
      'demo:assistant', '["bus:read","bus:send"]', 'fixture-operator',
      '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
  });
  return token;
}

describe('Hub DB backup export', () => {
  it('exports every table with schema, rows and matching row counts for the operator', async () => {
    const name = 'backup-export-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_token_0123456789';
    await configureOperator(name, token);
    const res = await call(stub(name), '/api/backup/export', { Authorization: 'Bearer ' + token });
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toContain('no-store');
    expect(res.headers.get('X-Content-Type-Options')).toBe('nosniff');
    const body = await res.json() as any;
    expect(body.snapshot).toBe('hub-do-sqlite');
    expect(typeof body.exported_at).toBe('string');
    // Non-JSON-native values become explicit lossless markers, counted
    // transparently (fixtures here are plain JSON: zero conversions).
    expect(body.value_markers).toEqual({ bigint: 0, blob: 0, unreadable: 0 });
    const expected = await listTables(name);
    expect(expected.length).toBeGreaterThan(10);
    expect(Object.keys(body.tables).sort()).toEqual(expected);
    expect(Object.keys(body.row_counts).sort()).toEqual(expected);
    // Schema carries tables PLUS indexes/triggers/views for a faithful
    // restore; ephemeral rate-limit state and platform internals
    // (sqlite_%, _cf_) are never exported.
    expect(expected.every(t => t in body.schema)).toBe(true);
    expect(Object.keys(body.schema).length).toBeGreaterThanOrEqual(expected.length);
    expect(body.schema).not.toHaveProperty('backup_rate');
    expect(body.tables).not.toHaveProperty('backup_rate');
    expect(body.row_counts).not.toHaveProperty('backup_rate');
    for (const key of [...Object.keys(body.tables), ...Object.keys(body.schema)]) {
      expect(key.startsWith('sqlite_')).toBe(false);
      expect(key.startsWith('_cf_')).toBe(false);
    }
    expect(String(body.schema['messages_bind_agents'] || '').toUpperCase()).toContain('CREATE TRIGGER');
    for (const table of expected) {
      expect(body.row_counts[table]).toBe(body.tables[table].length);
      expect(String(body.schema[table]).toUpperCase()).toContain('CREATE TABLE');
    }
    // Core Hub datasets are present: intake tasks, sessions, claims, audit.
    for (const table of ['intake_tasks', 'session_control_sessions', 'claims', 'audit']) {
      expect(body.tables, table).toHaveProperty(table);
    }
    // No plaintext operator token in the export body.
    expect(JSON.stringify(body)).not.toContain(token);
  });

  it('serves a cheap operator-only status with table names and row counts', async () => {
    const name = 'backup-status-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_status_0123456789';
    await configureOperator(name, token);
    const res = await call(stub(name), '/api/backup/status', { Authorization: 'Bearer ' + token });
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toContain('no-store');
    const body = await res.json() as any;
    const expected = await listTables(name);
    expect(body.tables).toEqual(expected);
    expect(body.api_version).toBe(5);
    expect(Object.keys(body.row_counts).sort()).toEqual(expected);
    // WITHOUT ROWID tables are listed so the script pages them by offset.
    expect(body.without_rowid).toContain('token_hours_posted');
    for (const t of body.without_rowid) expect(expected).toContain(t);
    expect(body).not.toHaveProperty('schema');
  });

  it('refuses anonymous, wrong-token, mixed-authority and non-admin callers', async () => {
    const name = 'backup-auth-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_auth_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    expect((await call(s, '/api/backup/export')).status).toBe(401);
    expect((await call(s, '/api/backup/export', { Authorization: 'Bearer wrong' })).status).toBe(401);
    expect((await call(s, '/api/backup/status')).status).toBe(401);
    expect((await call(s, '/api/backup/export',
      { Authorization: 'Bearer ' + token, Cookie: 'hub_session=invalid' })).status).toBe(403);
    const agentToken = await seedAgent(name);
    const agentRes = await call(s, '/api/backup/export', { Authorization: 'Bearer ' + agentToken });
    expect(agentRes.status).toBe(403);
    expect(((await agentRes.json()) as any).reason).toBe('operator_required');
    const agentStatus = await call(s, '/api/backup/status', { Authorization: 'Bearer ' + agentToken });
    expect(agentStatus.status).toBe(403);
  });

  it('is read-only: non-GET operator requests are refused and GET writes no business data', async () => {
    const name = 'backup-readonly-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_readonly_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    expect((await call(s, '/api/backup/export', { Authorization: 'Bearer ' + token }, 'POST')).status).toBe(405);
    expect((await call(s, '/api/backup/status', { Authorization: 'Bearer ' + token }, 'POST')).status).toBe(405);
    const before = await countAll(name);
    const res = await call(s, '/api/backup/export', { Authorization: 'Bearer ' + token });
    expect(res.status).toBe(200);
    await res.arrayBuffer();
    const after = await countAll(name);
    // The only permitted write is the rate-limit bucket row.
    for (const table of Object.keys(before)) {
      if (table === 'backup_rate') continue;
      expect(after[table]).toBe(before[table]);
    }
  });

  it('rate-limits the export per hour and still serves status afterwards', async () => {
    const name = 'backup-ratelimit-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_ratelimit_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    const headers = { Authorization: 'Bearer ' + token };
    for (let i = 0; i < 30; i++) {
      const res = await call(s, '/api/backup/export', headers);
      expect(res.status).toBe(200);
      await res.arrayBuffer();
    }
    const limited = await call(s, '/api/backup/export', headers);
    expect(limited.status).toBe(429);
    expect(((await limited.json()) as any).error).toBe('rate_limited');
    // Status has its own budget and must still serve after export is limited.
    const status = await call(s, '/api/backup/status', headers);
    expect(status.status).toBe(200);
    await status.arrayBuffer();
  });

  it('serves the schema dump without rows', async () => {
    const name = 'backup-schema-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_schema_0123456789';
    await configureOperator(name, token);
    const res = await call(stub(name), '/api/backup/export?schema=1', { Authorization: 'Bearer ' + token });
    expect(res.status).toBe(200);
    const body = await res.json() as any;
    expect(body.snapshot).toBe('hub-do-sqlite');
    expect(typeof body.exported_at).toBe('string');
    expect(Object.keys(body.schema).length).toBeGreaterThan(10);
    expect(String(body.schema['intake_tasks']).toUpperCase()).toContain('CREATE TABLE');
    expect(String(body.schema['messages_bind_agents']).toUpperCase()).toContain('CREATE TRIGGER');
    expect(body.schema).not.toHaveProperty('backup_rate');
    expect(body).not.toHaveProperty('tables');
    expect(body).not.toHaveProperty('row_counts');
  });

  it('serves paged per-table chunks that reassemble exactly', async () => {
    const name = 'backup-chunks-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_chunks_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    const headers = { Authorization: 'Bearer ' + token };
    // Seed enough rows to force multiple pages.
    await runInDurableObject(stub(name), async (hub: Hub) => {
      hub.sql.exec('CREATE TABLE IF NOT EXISTS chunk_probe (id INTEGER PRIMARY KEY, v TEXT NOT NULL)');
      for (let i = 0; i < 7; i++) hub.sql.exec('INSERT INTO chunk_probe(v) VALUES(?)', 'row-' + i);
    });
    const assembled: any[] = [];
    let total = -1;
    for (const offset of [0, 3, 6]) {
      const res = await call(s, '/api/backup/export?table=chunk_probe&offset=' + offset + '&limit=3', headers);
      expect(res.status).toBe(200);
      const body = await res.json() as any;
      expect(body.snapshot).toBe('hub-do-sqlite');
      expect(body.table).toBe('chunk_probe');
      expect(body.offset).toBe(offset);
      expect(body.limit).toBe(3);
      if (total < 0) total = body.total;
      else expect(body.total).toBe(total);
      expect(body.ddl.toUpperCase()).toContain('CREATE TABLE');
      expect(body.value_markers).toEqual({ bigint: 0, blob: 0, unreadable: 0 });
      assembled.push(...body.rows);
    }
    expect(total).toBe(7);
    expect(assembled.map((r: any) => r.v)).toEqual(
      ['row-0', 'row-1', 'row-2', 'row-3', 'row-4', 'row-5', 'row-6']);
    // Tail page may be short; paging past the end is refused.
    expect((await call(s, '/api/backup/export?table=chunk_probe&offset=99&limit=3', headers)).status).toBe(400);
    // Unknown tables 404 (not 500); bad shapes 400; POST stays 405.
    expect((await call(s, '/api/backup/export?table=no_such_table_xyz&offset=0&limit=3', headers)).status).toBe(404);
    expect((await call(s, '/api/backup/export?table=chunk_probe&offset=-1&limit=3', headers)).status).toBe(400);
    expect((await call(s, '/api/backup/export?table=chunk_probe&offset=0&limit=99999', headers)).status).toBe(400);
    expect((await call(s, '/api/backup/export?table=chunk_probe', headers)).status).toBe(200);
    expect((await call(s, '/api/backup/export?schema=1&table=chunk_probe', headers)).status).toBe(400);
    expect((await call(s, '/api/backup/export?schema=yes', headers)).status).toBe(400);
    expect((await call(s, '/api/backup/export?table=chunk_probe&offset=0&limit=3', headers, 'POST')).status).toBe(405);
  });

  // Regression (intake e27e0f9d, 2026-10-29 nightly failure): a hot table
  // mutating between chunk reads must neither skip nor duplicate rows.
  // Offset paging shifts unread rows down when already-read rows are
  // deleted (silent skip); the rowid keyset cursor does not.
  it('keyset cursor never skips or duplicates rows on a hot table', async () => {
    const name = 'backup-keyset-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_keyset_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    const headers = { Authorization: 'Bearer ' + token };
    const seed = async (table: string) => runInDurableObject(stub(name), async (hub: Hub) => {
      hub.sql.exec(`CREATE TABLE IF NOT EXISTS ${table} (id TEXT PRIMARY KEY, v TEXT NOT NULL)`);
      for (let i = 0; i < 7; i++) hub.sql.exec(`INSERT INTO ${table}(id, v) VALUES(?, ?)`, 'k' + i, 'row-' + i);
    });
    // Between page 1 and page 2: prune two already-read rows and append a burst.
    const churn = async (table: string) => runInDurableObject(stub(name), async (hub: Hub) => {
      hub.sql.exec(`DELETE FROM ${table} WHERE id IN ('k0', 'k1')`);
      for (let i = 7; i < 10; i++) hub.sql.exec(`INSERT INTO ${table}(id, v) VALUES(?, ?)`, 'k' + i, 'row-' + i);
    });

    await seed('hot_keyset');
    const got: string[] = [];
    let after = -9007199254740991;
    let pages = 0;
    for (;;) {
      const res = await call(s, `/api/backup/export?table=hot_keyset&after=${after}&limit=3`, headers);
      expect(res.status).toBe(200);
      const body = await res.json() as any;
      expect(body.cursor).toBe('rowid');
      expect(body.after).toBe(after);
      for (const row of body.rows) {
        expect(row).not.toHaveProperty('__hub_backup_rowid');
        got.push(row.v);
      }
      if (pages++ === 0) await churn('hot_keyset');
      if (body.rows.length < 3) break;
      expect(body.next_after).toBeGreaterThan(after);
      after = body.next_after;
    }
    // Every row that existed throughout plus every appended row, once each.
    expect(got).toEqual(['row-0', 'row-1', 'row-2', 'row-3', 'row-4', 'row-5', 'row-6', 'row-7', 'row-8', 'row-9']);

    // Same churn under legacy offset paging skips row-3 (the bug class the
    // drift bound used to paper over).
    await seed('hot_offset');
    const legacy: string[] = [];
    let offset = 0;
    for (let page = 0; ; page++) {
      const body = await (await call(s, `/api/backup/export?table=hot_offset&offset=${offset}&limit=3`, headers)).json() as any;
      expect(body.cursor).toBe('offset');
      legacy.push(...body.rows.map((r: any) => r.v));
      if (page === 0) await churn('hot_offset');
      if (body.rows.length < 3) break;
      offset += body.rows.length;
    }
    expect(legacy).not.toContain('row-3');

    // Keyset is refused (not silently mis-paged) on WITHOUT ROWID tables.
    const wr = await call(s, '/api/backup/export?table=token_hours_posted&after=0&limit=3', headers);
    expect(wr.status).toBe(400);
    expect(((await wr.json()) as any).error).toBe('keyset_unsupported');
    expect((await call(s, '/api/backup/export?table=hot_keyset&after=0&offset=0&limit=3', headers)).status).toBe(400);
    expect((await call(s, '/api/backup/export?table=hot_keyset&after=abc&limit=3', headers)).status).toBe(400);
  });

  it('keeps chunked reads on a separate generous budget', async () => {
    const name = 'backup-chunkbudget-' + crypto.randomUUID();
    const token = 'synthetic_operator_backup_chunkbudget_0123456789';
    await configureOperator(name, token);
    const s = stub(name);
    const headers = { Authorization: 'Bearer ' + token };
    // Exhaust the tight full-export budget: 30 OK, 31st limited.
    for (let i = 0; i < 30; i++) {
      const res = await call(s, '/api/backup/export', headers);
      expect(res.status).toBe(200);
      await res.arrayBuffer();
    }
    expect((await call(s, '/api/backup/export', headers)).status).toBe(429);
    // Chunked reads still serve after the full-export budget is exhausted.
    const chunk = await call(s, '/api/backup/export?table=intake_tasks&offset=0&limit=3', headers);
    expect(chunk.status).toBe(200);
    await chunk.arrayBuffer();
    const schema = await call(s, '/api/backup/export?schema=1', headers);
    expect(schema.status).toBe(200);
    await schema.arrayBuffer();
  });
});
