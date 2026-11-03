// An Antigravity account (fleet provider gemini) must be placeable by naming the engine alone,
// although its models only sit in the deep/review tiers (intake 5814e109 AC i; overseer 8d006dc3 smoke).
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { freshCpu } from './cpu-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

describe('antigravity placement', () => {
  it('keeps plain daily work off Antigravity and allows reasoned deep placement', async () => {
    const now = Date.now();
    const iso = (delta = 0) => new Date(now + delta).toISOString();
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('agy-placement-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const machine = 'cBynxGDoKVPoGpGWxvCTvg';
      hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, machine, iso(), iso(), JSON.stringify({
          mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() },
        }), JSON.stringify({ engines: ['antigravity'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
      const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT', body: JSON.stringify({
          accounts: [{ label: 'hub-antigravity', provider: 'gemini', max_concurrency: 2, reserve_percent: 0, enabled: true }],
          routes: [{ machine_id: machine, engine: 'antigravity', account: 'hub-antigravity', profile: 'hub-hub-antigravity' }],
        }),
      }), { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] });
      expect(put.status, await put.clone().text()).toBe(200);
      const plain = fleet.plan({ engine: 'antigravity', machine, mode: 'headless', workspace: 'demo', prompt: 'plain implement', task_kind: 'implement' } as any);
      expect(plain.selected, JSON.stringify(plain)).toBeNull();
      expect(plain.rejected).toContainEqual({ machine, engine: 'antigravity', reason: 'model_unsupported' });

      const deep = fleet.plan({ engine: 'antigravity', tier: 'deep', engine_override_reason: 'This task explicitly requires deep reasoning on the Gemini route.', machine, mode: 'headless', workspace: 'demo', prompt: 'Return the proof marker' } as any);
      expect(deep.selected, JSON.stringify(deep.rejected)).toMatchObject({
        machine, engine: 'antigravity', account: 'hub-antigravity', selection: { engine: 'antigravity', provider: 'gemini', id: 'argon', model: 'gemini-4-argon-medium', effort: 'medium' },
      });
    });
  });
});
