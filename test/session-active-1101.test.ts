import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, reconcileStaleSessions } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const get = (path: string) => new Request('https://hub.test' + path);

describe('shared active definition + stale reconciliation (hub-1101-outage-20261031)', () => {
  it('list, tree and active_count agree; reconcile ends only heartbeat-less rows without deleting', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('active-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now, {}, {});
      s.init();
      const old = new Date(now - 48 * 3600_000).toISOString();
      const fresh = new Date(now - 30_000).toISOString();
      const ins = (id: string, st: string, native: string | null, created: string) => hub.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
         VALUES (?, 'a', ?, '{}', 'm1', 'claude', 'headless', ?, ?, ?, ?)`, id, 'k' + id, st, native ? JSON.stringify({ native_id: native }) : null, created, created);
      const ids = ['00000000-0000-4000-8000-00000000000'];
      const u = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
      ins(u(1), 'running', 'n1', old);   // heartbeat fresh -> active
      ins(u(2), 'running', 'n2', old);   // no heartbeat -> stale
      ins(u(3), 'running', null, old);   // unbound -> stale
      ins(u(4), 'stopped', 'n4', old);   // terminal
      hub.sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, phase, observed_at, workspace) VALUES ('m1','n1','t','running',?,'w')`, fresh);
      const list = await (await s.handle(get('/api/sessions?active=1'), admin)).json() as any;
      expect(list.sessions.map((r: any) => r.id)).toEqual([u(1)]);
      expect(list.active_count).toBe(1);
      const tree = await (await s.handle(get('/api/sessions/tree'), admin)).json() as any;
      expect(tree.total).toBe(1);
      const r = reconcileStaleSessions(hub.sql, now);
      expect(r).toEqual({ reconciled: 2, remaining: 0 });
      const rows = hub.sql.exec('SELECT id, state FROM session_control_sessions ORDER BY id').toArray() as any[];
      expect(rows.map(x => x.state)).toEqual(['running', 'stopped', 'stopped', 'stopped']);
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM session_stale_reconciliations').toArray()[0]).toEqual({ n: 2 });
      void ids;
    });
  });
});
