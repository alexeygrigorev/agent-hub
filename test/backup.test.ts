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
    expect(body.api_version).toBe(4);
    expect(Object.keys(body.row_counts).sort()).toEqual(expected);
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
