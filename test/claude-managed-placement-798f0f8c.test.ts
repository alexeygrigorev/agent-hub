// The central Claude account must place headless work on either installed host.
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { freshCpu } from './cpu-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

describe('managed Claude placement (798f0f8c)', () => {
  it('keeps deep/Opus and daily/Sonnet starts headless on both hosts, including mixed legacy routes', async () => {
    const now = Date.now();
    const iso = (delta = 0) => new Date(now + delta).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('managed-claude-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machines = ['DEFLf4-rahKf9wE4TQAZfA', 'cBynxGDoKVPoGpGWxvCTvg'];
      for (const machine of machines) {
        hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
          machine, machine, machine, iso(), iso(), JSON.stringify({
            mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
          }), JSON.stringify({ engines: ['claude'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      }
      const response = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT', body: JSON.stringify({
          accounts: [{ label: 'hub-claude', provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true }],
          routes: machines.map(machine_id => ({ machine_id, engine: 'claude', account: 'hub-claude', profile: 'hub-hub-claude' })),
        }),
      }), { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] });
      expect(response.status).toBe(200);
      for (const machine of machines) {
        const reported = await fleet.handle(new Request('https://hub.test/api/fleet/subscriptions', {
          method: 'PUT', body: JSON.stringify({ subscriptions: [{ service: 'claude', account: 'hub-claude', plan: null, quota: {
            status: 'ok', observed_at: iso(), details: { limit_reached: false },
            windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } },
          } }] }),
        }), { caller: { machineId: machine, actor: 'machine:' + machine, kind: 'host' }, scopes: ['host'] });
        expect(reported.status).toBe(200);
        for (const [tier, model] of [['deep', 'opus'], ['daily', 'sonnet']]) {
          const plan = fleet.plan({ engine: 'claude', tier, machine, mode: 'headless', workspace: 'demo', prompt: 'Return the proof marker' });
          expect(plan.selected, JSON.stringify(plan.rejected)).toMatchObject({
            machine, engine: 'claude', account: 'hub-claude', profile: 'hub-hub-claude',
            selection: { model, engine: 'claude', provider: 'claude' },
          });
          expect(plan.selected?.mode).toBeUndefined();
          expect(plan.selected?.with_goal).toBeUndefined();
          expect(plan.rejected).not.toContainEqual(expect.objectContaining({ machine, reason: 'model_unsupported' }));
        }
      }

      // A healthy host-home account used to trigger the unbounded interactive
      // fallback. Its higher quota must not displace a managed headless route.
      const machine = machines[0];
      const legacy = { engine: 'claude', account: 'shared-claude', profile: null };
      hub.sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({
        engines: ['claude'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8,
        fleet_goal_routes: [legacy],
        fleet_route_rejections: [{ ...legacy, mode: 'headless', reason: 'model_unsupported' }],
      }), machine);
      const mixed = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT', body: JSON.stringify({
          accounts: ['hub-claude', 'shared-claude'].map(label => ({ label, provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true })),
          routes: [
            ...machines.map(machine_id => ({ machine_id, engine: 'claude', account: 'hub-claude', profile: 'hub-hub-claude' })),
            { machine_id: machine, ...legacy },
          ],
        }),
      }), { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] });
      expect(mixed.status).toBe(200);
      const reported = await fleet.handle(new Request('https://hub.test/api/fleet/subscriptions', {
        method: 'PUT', body: JSON.stringify({ subscriptions: ['hub-claude', 'shared-claude'].map(account => ({ service: 'claude', account, plan: null, quota: {
          status: 'ok', observed_at: iso(), details: { limit_reached: false },
          windows: { weekly: { present: true, percent_remaining: account === 'hub-claude' ? 80 : 99, reset_at: iso(86400000) } },
        } })) }),
      }), { caller: { machineId: machine, actor: 'machine:' + machine, kind: 'host' }, scopes: ['host'] });
      expect(reported.status).toBe(200);
      const intent = { engine: 'claude', tier: 'deep', machine, mode: 'headless', workspace: 'demo', prompt: 'Return the proof marker' };
      const plan = fleet.plan(intent);
      expect(plan.selected, JSON.stringify(plan.rejected)).toMatchObject({ account: 'hub-claude', selection: { model: 'opus' } });
      expect(plan.selected?.mode).toBeUndefined();
      expect(plan.request.mode).toBe('headless');

      // /goal has Codex semantics. Merely advertising a Claude goal route
      // must not make an unpinned deep/review goal become a plain prompt.
      for (const tier of ['deep', 'review']) {
        for (const engine of [undefined, 'claude']) {
          const goal = fleet.plan({ ...intent, engine, tier, mode: 'interactive', with_goal: true });
          expect(goal.selected, JSON.stringify(goal.rejected)).toBeNull();
          expect(goal.rejected).toContainEqual(expect.objectContaining({ machine, engine: 'claude', reason: 'goal_unsupported' }));
        }
      }

      const reserved = fleet.reserve({ ...intent, idempotency_key: crypto.randomUUID(), access: { mode: 'read' } },
        { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] });
      expect(reserved.status, JSON.stringify(reserved.value)).toBe(201);
      expect(reserved.value).toMatchObject({ account: 'hub-claude', mode: 'headless' });
      const launch = hub.sql.exec('SELECT mode, request_json FROM fleet_launches WHERE id = ?', reserved.value.id).one();
      expect(launch.mode).toBe('headless');
      expect(JSON.parse(String(launch.request_json)).with_goal).toBeUndefined();
    });
  });
});
