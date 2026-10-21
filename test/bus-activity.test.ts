import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { SCHEMA_SQL } from '../src/schema';
import { sha256 } from '../src/auth/crypto';
import { doFetch } from './m1-focus-setup';

const defaultEnv = env as unknown as Env;

// Read-only visibility slice: GET /api/bus/activity aggregates the existing
// claims table and the existing messages table. It creates no store, writes
// nothing, and invents no protocol — it only shows what already exists so an
// operator can see that someone is already on a task/workspace.
describe('bus activity visibility (read-only aggregation)', () => {
  let stub: any;

  async function seedCred(sql: any, id: string, token: string, kind: string, machineId: string, actor: string | null, scopes: string[]) {
    const now = new Date().toISOString();
    const future = new Date(Date.now() + 86400000).toISOString();
    sql.exec(
      "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      id, kind, await sha256(token), `fam_${id}`, actor || machineId, actor, machineId, JSON.stringify(scopes), 'admin-1', now, future
    );
  }

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      for (const schema of [SCHEMA_SQL, BUS_SCHEMA_SQL]) {
        for (const statement of schema.split(';')) {
          const trimmed = statement.trim();
          if (trimmed) {
            try { instance.sql.exec(trimmed); } catch (e) {}
          }
        }
      }
      for (const table of ['messages', 'claims', 'agents', 'machines', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
      try { instance.sql.exec('DELETE FROM fleet_claim_policy'); } catch (e) {}
      const now = new Date().toISOString();
      for (const m of ['mach-A', 'mach-B']) {
        instance.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', m, m, m, now);
      }
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'mach-A')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-2', 'projB', 'worker', 'mach-B')");
      await seedCred(instance.sql, 'c_w1', 'tok_w1', 'agent', 'mach-A', 'worker-1', ['bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_w1_send', 'tok_w1_send', 'agent', 'mach-A', 'worker-1', ['bus:send']);
      await seedCred(instance.sql, 'c_w2q', 'tok_w2q', 'agent', 'mach-B', 'projB:worker-2', ['bus:send', 'bus:read']);
    });
  });

  const http = (method: string, path: string, token?: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method,
    headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));

  it('requires authentication and the bus:read scope', async () => {
    expect((await http('GET', '/api/bus/activity?project=projA')).status).toBe(401);
    expect((await http('GET', '/api/bus/activity?project=projA', 'tok_w1_send')).status).toBe(403);
  });

  it('rejects unknown params and out-of-range limits', async () => {
    expect((await http('GET', '/api/bus/activity?project=projA&foo=1', 'tok_w1')).status).toBe(400);
    expect((await http('GET', '/api/bus/activity?project=projA&limit=0', 'tok_w1')).status).toBe(400);
    expect((await http('GET', '/api/bus/activity?project=projA&limit=101', 'tok_w1')).status).toBe(400);
    expect((await http('GET', '/api/bus/activity?project=projA&limit=many', 'tok_w1')).status).toBe(400);
  });

  it('shows active claims and recent bus messages for a project', async () => {
    const claim = await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    });
    expect(claim.status).toBe(200);
    const sent = await http('POST', '/api/bus/send', 'tok_w1', {
      from_actor: 'worker-1', to_actor: 'worker-2', kind: 'note',
      body: 'starting the CI fix', idempotency_key: 'activity-key-1',
    });
    expect(sent.status).toBe(200);

    const res = await http('GET', '/api/bus/activity?project=projA', 'tok_w1');
    expect(res.status).toBe(200);
    const data = await res.json() as any;
    expect(data.project).toBe('projA');
    expect(data.workspace).toBeNull();
    expect(data.claim_policy).toBeNull();
    expect(data.claims).toHaveLength(1);
    expect(data.claims[0]).toMatchObject({ project: 'projA', scope: 'src/worker', holder: 'projA:worker-1' });
    expect(data.recent_messages).toHaveLength(1);
    expect(data.recent_messages[0]).toMatchObject({ body: 'starting the CI fix' });
    expect(data.notice).toContain('1 active work declaration');
    expect(data.notice).toContain('someone may already be on this');
  });

  it('resolves a workspace to its claim project through the existing policy table', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('CREATE TABLE IF NOT EXISTS fleet_claim_policy (workspace TEXT PRIMARY KEY, project TEXT, require_claim INTEGER NOT NULL)');
      instance.sql.exec('INSERT INTO fleet_claim_policy VALUES (?, ?, ?)', '/repo', 'projA', 1);
    });
    await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    });

    const res = await http('GET', '/api/bus/activity?workspace=%2Frepo', 'tok_w1');
    expect(res.status).toBe(200);
    const data = await res.json() as any;
    expect(data.workspace).toBe('/repo');
    expect(data.project).toBe('projA');
    expect(data.claim_policy).toEqual({ project: 'projA', require_claim: true });
    expect(data.claims).toHaveLength(1);
  });

  it('reports an empty scope honestly when nothing is declared', async () => {
    const res = await http('GET', '/api/bus/activity?project=projA', 'tok_w1');
    expect(res.status).toBe(200);
    const data = await res.json() as any;
    expect(data.claims).toEqual([]);
    expect(data.recent_messages).toEqual([]);
    expect(data.notice).toContain('no active work declarations');
  });

  it('keeps existing project scoping: a foreign project is refused', async () => {
    const res = await http('GET', '/api/bus/activity?project=projB', 'tok_w1');
    expect(res.status).toBe(403);
  });

  it('does not scope claims to a workspace with no policy mapping', async () => {
    await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    });
    const res = await http('GET', '/api/bus/activity?workspace=%2Funmapped', 'tok_w1');
    expect(res.status).toBe(200);
    const data = await res.json() as any;
    expect(data.project).toBeNull();
    expect(data.claim_policy).toBeNull();
    expect(data.claims).toEqual([]);
    expect(data.notice).toContain('no claim policy maps this workspace');
  });

  it('omits released claims by default and includes them with all=true', async () => {
    const created = await (await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    })).json() as any;
    expect((await http('POST', `/api/bus/claims/${created.id}/release`, 'tok_w1', {})).status).toBe(200);

    const def = await (await http('GET', '/api/bus/activity?project=projA', 'tok_w1')).json() as any;
    expect(def.claims).toEqual([]);
    const all = await (await http('GET', '/api/bus/activity?project=projA&all=true', 'tok_w1')).json() as any;
    expect(all.claims).toHaveLength(1);
    expect(all.claims[0].released_at).not.toBeNull();
  });
});
