import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, reconcileStaleSessions } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = { caller: { isAdmin: false, actor: 'agent-a', kind: 'agent' }, scopes: ['fleet:launch'] };
const get = (path: string) => new Request('https://hub.test' + path);
const u = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

// 700 rows: 1..560 stopped (history), 561..600 running+fresh heartbeat (agent-a),
// 601..640 running without heartbeat (stale, old), 641..700 queued (agent-b).
async function seed(hub: Hub, now: number) {
  const old = (n: number) => new Date(now - 48 * 3600_000 + n * 1000).toISOString();
  const fresh = new Date(now - 30_000).toISOString();
  for (let n = 1; n <= 700; n++) {
    const st = n <= 560 ? 'stopped' : n <= 600 ? 'running' : n <= 640 ? 'running' : 'queued';
    const actor = n >= 641 ? 'agent-b' : 'agent-a';
    const native = n > 560 && n <= 600 ? `n${n}` : n > 600 && n <= 640 ? `x${n}` : null;
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, ?, ?, '{}', 'm1', 'claude', 'headless', ?, ?, ?, ?)`, u(n), actor, 'k' + n, st, native ? JSON.stringify({ native_id: native }) : null, old(n), old(n));
    if (n > 560 && n <= 600) hub.sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, phase, observed_at, workspace) VALUES ('m1',?,'t','running',?,'w')`, `n${n}`, fresh);
  }
}

describe('live/active reads are index-served and bounded (intake b2236d2c)', () => {
  it('LIMIT/cursor, tree caps, non-admin, reconcile batch and query plans over 700 rows', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('bounds-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now, {}, {});
      s.init();
      await seed(hub, now);
      // Credential-family liveness is covered elsewhere; here only the actor-scoped SQL path matters.
      (s as any).ownerOk = (row: any, c: any) => c.isAdmin === true || row.actor === c.actor;
      const json = async (p: string, a: any = admin) => await (await s.handle(get(p), a)).json() as any;

      // list: live=1 pages through 40 fresh + 60 queued = 100 rows with a cursor
      const p1 = await json('/api/sessions?live=1&limit=30');
      expect(p1.sessions).toHaveLength(30);
      expect(p1.next_cursor).toBeTruthy();
      const p2 = await json('/api/sessions?live=1&limit=30&cursor=' + encodeURIComponent(p1.next_cursor));
      expect(p2.sessions).toHaveLength(30);
      expect(new Set([...p1.sessions, ...p2.sessions].map((r: any) => r.id)).size).toBe(60);
      expect(p1.active_count).toBe(100);
      expect(p1.active_count_capped).toBe(false);
      // non-live default list also bounded (limit max 100)
      expect((await json('/api/sessions?limit=500')).sessions).toHaveLength(100);

      // non-admin: only own live rows (40 fresh running), count likewise
      const na = await json('/api/sessions?live=1&limit=100', agent);
      expect(na.sessions).toHaveLength(40);
      expect(na.sessions.every((r: any) => r.id >= u(561) && r.id <= u(600))).toBe(true);
      expect(na.active_count).toBe(40);

      // tree: default limit 200, TREE_LIMIT_MAX 500, include=all capped & truncated
      const t = await json('/api/sessions/tree');
      expect(t.total).toBe(100);
      expect(t.truncated).toBe(false);
      const small = await json('/api/sessions/tree?limit=10');
      expect(small.total).toBe(10);
      expect(small.truncated).toBe(true);
      const all = await json('/api/sessions/tree?include=all&limit=100000');
      expect(all.total).toBe(500);
      expect(all.truncated).toBe(true);
      expect((await json('/api/sessions/tree', agent)).total).toBe(40);

      // reconcile: batch > 200 with remaining > 0, converges, live rows untouched
      for (let n = 1000; n < 1300; n++) {
        const t0 = new Date(now - 48 * 3600_000).toISOString();
        hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
          VALUES (?, 'agent-a', ?, '{}', 'm1', 'claude', 'headless', 'running', NULL, ?, ?)`, u(n), 'k' + n, t0, t0);
      }
      const r1 = reconcileStaleSessions(hub.sql, now, 250);
      expect(r1.reconciled).toBe(250);
      expect(r1.remaining).toBeGreaterThan(0);
      const r2 = reconcileStaleSessions(hub.sql, now);
      expect(r2.reconciled).toBe(90);
      expect(r2.remaining).toBe(0);
      expect((hub.sql.exec(`SELECT COUNT(*) AS n FROM session_control_sessions WHERE state = 'running'`).toArray()[0] as any).n).toBe(40);
      expect((hub.sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').toArray()[0] as any).n).toBe(1000);

      // EXPLAIN QUERY PLAN: no base-table SCAN without an index, no temp b-tree
      const live = `state IN ('queued','held','offered','starting','running','uncertain')`;
      const plans: string[] = [
        `SELECT * FROM session_control_sessions INDEXED BY session_control_live_created WHERE 1 = 1 AND ${live} AND created_at < ? ORDER BY created_at DESC, id DESC LIMIT 31`,
        `SELECT * FROM session_control_sessions INDEXED BY session_control_live_actor_created WHERE actor = ? AND ${live} ORDER BY created_at DESC, id DESC LIMIT 31`,
        `SELECT COUNT(*) FROM (SELECT 1 FROM session_control_sessions INDEXED BY session_control_live_created WHERE ${live} LIMIT 1000)`,
        `SELECT 1 FROM session_control_sessions WHERE state IN ('starting','running','uncertain','offered') AND updated_at < ? LIMIT 1`,
      ];
      for (const q of plans) {
        const args = q.includes('?') ? ['z'] : [];
        const detail = (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...args).toArray() as any[]).map(x => String(x.detail)).join('\n');
        expect(detail).not.toMatch(/SCAN session_control_sessions(?! USING)/);
        expect(detail).not.toMatch(/TEMP B-TREE/);
        expect(detail).toMatch(/USING (COVERING )?INDEX session_control_(live_|state)/);
      }
    });
  });
});
