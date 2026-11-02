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

  it('keeps unchanged running sessions active via fleet_catalog_seen and recovers false stale_no_heartbeat stops', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('active-catalog-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const s = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now, {}, {});
      s.init();
      const old = new Date(now - 48 * 3600_000).toISOString();
      const fresh = new Date(now - 30_000).toISOString();
      const id1 = '00000000-0000-4000-8000-000000000011';
      const id2 = '00000000-0000-4000-8000-000000000012';
      // id1: unchanged row (observed_at > 10m ago) on a machine with a fresh fleet_catalog_seen census
      hub.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
         VALUES (?, 'a', 'k11', '{}', 'm1', 'shell', 'interactive', 'running', ?, ?, ?)`,
        id1, JSON.stringify({ native_id: id1, tag: 't11', workspace: '/w', native_created_at_ms: now - 48 * 3600_000, provenance: 'observed_native' }), old, old,
      );
      hub.sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, observed_at, workspace, created_at_ms) VALUES ('m1', ?, 't11', 'shell', 'running', ?, '/w', ?)`, id1, old, now - 48 * 3600_000);
      hub.sql.exec(`INSERT INTO fleet_catalog_seen (machine_id, observed_at) VALUES ('m1', ?)`, fresh);

      const list = await (await s.handle(get('/api/sessions?live=1'), admin)).json() as any;
      expect(list.sessions.map((r: any) => r.id)).toEqual([id1]);
      expect(reconcileStaleSessions(hub.sql, now)).toEqual({ reconciled: 0, remaining: 0 });

      // id2: falsely marked stopped by an earlier stale_no_heartbeat reconciliation, still running in host census
      const createdMs = now - 72 * 3600_000;
      hub.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
         VALUES (?, 'a', 'k12', '{"provenance":"observed_native"}', 'm1', 'opus', 'interactive', 'stopped', ?, ?, ?)`,
        id2, JSON.stringify({ native_id: id2, tag: 'orchestrator-main', workspace: '/w', native_created_at_ms: createdMs, generation: 1, provenance: 'observed_native' }), old, old,
      );
      hub.sql.exec(`INSERT INTO session_stale_reconciliations (session_id, previous_state, reason, at) VALUES (?, 'running', 'stale_no_heartbeat', ?)`, id2, old);
      const { adoptObservedNativeSessions } = await import('../src/session_control');
      adoptObservedNativeSessions(hub.sql, 'm1', [{
        native_id: id2, tag: 'orchestrator-main', engine: 'opus', phase: 'running', workspace: '/w', created_at_ms: createdMs,
      }], now);
      const recovered = hub.sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id2).toArray()[0] as any;
      expect(recovered.state).toBe('running');
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM session_stale_reconciliations WHERE session_id = ?', id2).toArray()[0]).toEqual({ n: 0 });

      // Prefix lookup resolves fleet_launch_id and bound native_id prefixes (e.g. 988b8d49).
      const id3 = '15acde15-2852-434e-9bdf-123c883e311c';
      const launch3 = '988b8d49-cacf-4a5e-900c-a214a6402808';
      const native3 = '5a613cb2-f06d-4cc7-9435-af49e67107c5';
      hub.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, binding_json, created_at, updated_at)
         VALUES (?, 'a', 'k13', '{}', ?, 'm1', 'zcodex', 'interactive', 'running', ?, ?, ?)`,
        id3, launch3, JSON.stringify({ native_id: native3, tag: 'hub-' + launch3, workspace: '/w' }), fresh, fresh,
      );
      const byLaunchPrefix = await (await s.handle(get('/api/sessions/988b8d49'), admin)).json() as any;
      expect(byLaunchPrefix.id).toBe(id3);
      const byNativePrefix = await (await s.handle(get('/api/sessions/5a613cb2'), admin)).json() as any;
      expect(byNativePrefix.id).toBe(id3);
    });
  });
});
