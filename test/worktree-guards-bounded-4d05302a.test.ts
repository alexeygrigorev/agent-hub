import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { seedFixtureIntake } from './tbm-fixture';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 4d05302a: GET /api/sessions/worktree-guards returned every session of
// the machine (~4,300 rows) and rebuilt every session's inputs on each host
// poll. It must read a bounded, indexed row set whatever the history size.
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId: 'host-cred-1' }, scopes: ['host'] });
const get = (path: string) => new Request('https://hub.test' + path, { method: 'GET' });

function metered(sql: any) {
  const cursors: any[] = [];
  const proxy = new Proxy(sql, {
    get(target, prop) {
      if (prop === 'exec') return (q: string, ...args: any[]) => { const c = target.exec(q, ...args); cursors.push(c); return c; };
      const v = target[prop];
      return typeof v === 'function' ? v.bind(target) : v;
    },
  });
  const measure = async (fn: () => any) => {
    cursors.length = 0;
    const out = await fn();
        return { read: cursors.reduce((n, c) => n + c.rowsRead, 0), out };
  };
  return { proxy, measure };
}

async function fixture(fn: (s: SessionControlService, sql: any, measure: (fn: () => any) => Promise<{ read: number; out: any }>) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('wg-bounded-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const m = metered(hub.sql);
    const service = new SessionControlService(m.proxy, (cb) => state.storage.transactionSync(cb), () => Date.now(), { reserve: () => ({ value: null, status: 200 }) }, {});
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql, m.measure);
  });
}

const NATIVE = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`;

describe('worktree-guards bounded read (4d05302a)', () => {
  it('reads a bounded row set at production scale and still refuses foreign/live owners', async () => {
    await fixture(async (s, sql, measure) => {
      sql.exec(`WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c WHERE i < 50000)
        INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at)
        SELECT 'hist-' || i, 'demo:assistant', 'agent', 's', 'k-' || i, '{"workspace":"demo"}', 'hetzner', 'claude', 'default', 'headless', 'demo', 'p',
          'exited', '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z' FROM c`);
      sql.exec(`WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c WHERE i < 5000)
        INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at)
        SELECT printf('11111111-0000-4000-8000-%012d', i), 'demo:assistant', 'rk-' || i, '{"workspace":"demo"}', 'hist-' || i, 'hetzner', 'claude', 'headless', 'stopped',
          '2026-10-01T00:' || printf('%02d', (i / 60) % 60) || ':' || printf('%02d', i % 60) || '.000Z', '2026-10-01T00:00:00.000Z' FROM c`);
      // Live owner with an open intake, plus a foreign machine's session on the same intake id.
      const live = crypto.randomUUID();
      const foreign = crypto.randomUUID();
      const now = new Date().toISOString();
      const binding = JSON.stringify({ native_id: NATIVE(1), tag: 'hub-x', workspace: '/w', native_created_at_ms: 1, generation: 1 });
      sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES(?,'demo:assistant',?,?,'hetzner','claude','headless','running',?,?,?)`, live, live, JSON.stringify({ intake_task_id: 'task-live' }), binding, now, now);
      sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
        VALUES(?,'demo:assistant',?,?,'other-host','claude','headless','running',?,?)`, foreign, foreign, JSON.stringify({ intake_task_id: 'task-live' }), now, now);

      const call = async (qs: string) => {
        const m = await measure(async () => (await s.handle(get('/api/sessions/worktree-guards' + qs), host('hetzner') as any)).json());
        return { read: m.read, body: m.out as any };
      };
      const targeted = await call(`?session_id=${live}`);
      expect(targeted.body.sessions.map((f: any) => f.session_id)).toEqual([live]);
      expect(targeted.body.sessions[0]).toMatchObject({ terminal: false, native_id: NATIVE(1) });
      // A foreign machine's session is never reported to this host and a live
      // owner is never presented as releasable.
      expect((await call(`?session_id=${foreign}`)).body.sessions).toEqual([]);
      expect(targeted.body.sessions[0].lock_release_permitted).toBe(false);

      const page = await call('?limit=100');
      expect(page.body.sessions.length).toBe(100);
      expect(typeof page.body.next_cursor).toBe('string');
      const next = await call('?limit=100&cursor=' + encodeURIComponent(page.body.next_cursor));
      expect(next.body.sessions.length).toBe(100);
      expect(next.body.sessions[0].session_id).not.toBe(page.body.sessions[0].session_id);

      const legacy = await call('');
      expect(legacy.body.sessions.length).toBeLessThanOrEqual(200);
      expect(legacy.body.sessions.map((f: any) => f.session_id)).toContain(live);

      console.log('worktree-guards rows read', JSON.stringify({ targeted: targeted.read, page: page.read, next: next.read, legacy: legacy.read }));
      expect(targeted.read).toBeLessThanOrEqual(1000);
      expect(page.read).toBeLessThanOrEqual(6500);
      expect(next.read).toBeLessThanOrEqual(6500);
      expect(legacy.read).toBeLessThanOrEqual(6500);

      // History growth must not change the cost: add 2k more old sessions and re-measure.
      sql.exec(`WITH RECURSIVE c(i) AS (SELECT 5001 UNION ALL SELECT i + 1 FROM c WHERE i < 7000)
        INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at)
        SELECT printf('11111111-0000-4000-8000-%012d', i), 'demo:assistant', 'rk-' || i, '{"workspace":"demo"}', 'hist-' || i, 'hetzner', 'claude', 'headless', 'stopped',
          '2026-09-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z' FROM c`);
      const grown = await call(`?session_id=${live}`);
      expect(Math.abs(grown.read - targeted.read)).toBeLessThanOrEqual(20);
      // Every statement the endpoint can issue is indexed.
      const plan = (q: string, ...a: any[]) => (sql.exec('EXPLAIN QUERY PLAN ' + q, ...a).toArray() as any[]).map(r => r.detail).join('\n');
      // Keyset page with a row-value seek: a range seek on (created_at,id), no sort, covering (no table fetch).
      const seek = plan(`SELECT id, created_at FROM session_control_sessions INDEXED BY session_control_machine_created_state WHERE machine_id = ? AND state IN (SELECT value FROM json_each(?)) AND (created_at, id) > (?, ?) ORDER BY created_at, id LIMIT 101`, 'hetzner', '[]', 'x', 'y');
      console.log('worktree-guards page plan', seek.replace(/\n/g, ' | '));
      expect(seek).toContain('session_control_machine_created_state');
      expect(seek).toMatch(/\(created_at,id\)>\(\?,\?\)/);
      expect(seek).toContain('COVERING INDEX');
      expect(seek).not.toContain('TEMP B-TREE');
      expect(plan(`SELECT id FROM session_control_sessions INDEXED BY session_control_machine_state WHERE machine_id=? AND state IN (SELECT value FROM json_each(?)) LIMIT 100`, 'hetzner', '[]')).toContain('session_control_machine_state');
      // The link lookup is an indexed side-table probe, not a per-row json_each scan.
      const links = plan(`SELECT id, links FROM intake_task_projection WHERE id IN (SELECT task_id FROM intake_link_refs WHERE ref IN (SELECT value FROM json_each(?)))`, '[]');
      expect(links).toContain('intake_link_refs');
      expect(links).not.toMatch(/CORRELATED/);
    });
  });

  const insertSession = (sql: any, id: string, intake: string | null, state: string, at: string, machine = 'hetzner') =>
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
      VALUES(?,'demo:assistant',?,?,?,'claude','headless',?,?,?)`, id, id, JSON.stringify(intake ? { intake_task_id: intake } : {}), machine, state, at, at);
  const sid = (i: number) => `22222222-0000-4000-8000-${String(i).padStart(12, '0')}`;

  it('keyset paging over same-second timestamps skips and duplicates nothing', async () => {
    await fixture(async (s, sql) => {
      for (let i = 1; i <= 450; i++) insertSession(sql, sid(i), null, 'stopped', '2026-10-01T00:00:00.000Z');
      const seen: string[] = [];
      let cursor: string | null = null;
      for (let guard = 0; guard < 20; guard++) {
        const res: any = await (await s.handle(get('/api/sessions/worktree-guards?limit=100' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : '')), host('hetzner') as any)).json();
        seen.push(...res.sessions.map((f: any) => f.session_id));
        cursor = res.next_cursor;
        if (!cursor) break;
      }
      expect(seen.length).toBe(450);
      expect(new Set(seen).size).toBe(450);
    });
  });

  it('never unlocks when a live same-intake owner sits on the second sibling page', async () => {
    await fixture(async (s, sql) => {
      for (let i = 1; i <= 250; i++) insertSession(sql, sid(i), 'task-p2', 'stopped', '2026-10-01T00:00:00.000Z');
      insertSession(sql, sid(251), 'task-p2', 'running', '2026-10-02T00:00:00.000Z'); // 251st row: page 2 of 200
      const res: any = await (await s.handle(get(`/api/sessions/worktree-guards?session_id=${sid(1)}`), host('hetzner') as any)).json();
      expect(res.sessions.map((f: any) => f.session_id)).toEqual([sid(1)]);
      expect(res.sessions[0].lock_release_permitted).toBe(false);
    });
  });

  it('fails closed when the intake closure exceeds the cap (hidden live owner)', async () => {
    await fixture(async (s, sql) => {
      for (let i = 1; i <= 410; i++) insertSession(sql, sid(i), 'task-big', 'stopped', '2026-10-01T00:00:00.000Z');
      insertSession(sql, sid(411), 'task-big', 'running', '2026-10-02T00:00:00.000Z');
      for (const qs of [`session_id=${sid(1)}`, 'intake_task_id=task-big']) {
        const res: any = await (await s.handle(get('/api/sessions/worktree-guards?' + qs), host('hetzner') as any)).json();
        expect(res.sessions.length).toBeGreaterThan(0);
        for (const fact of res.sessions) expect(fact.lock_release_permitted).toBe(false);
      }
    });
  });
});
