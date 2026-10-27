import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { RoutingService } from '../src/routing';
import { modelCandidates, policy } from '../src/model-routing';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

describe('Grok policy control', () => {
  it('keeps every stored preference subordinate to the global model deny', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('grok-preference-fence-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const now = Date.now();
      const service = new RoutingService(hub.sql, null as any, () => now, cb => state.storage.transactionSync(cb));
      const path = 'https://hub.test/api/fleet/routing/policy/grok';
      const admin = { caller: { isAdmin: true, actor: 'operator-test' }, scopes: ['admin'] };
      const before: any = await (await service.handle(new Request(path), admin)).json();
      expect(before).toMatchObject({ mode: 'disabled', effective_mode: 'disabled', factor: 0, enabled: false });
      const grok = policy.models.find(m => m.id === 'grok-4-7')!;
      const muse = policy.models.find(m => m.id === 'muse')!;
      const quota = { status: 'ok', observed_at: new Date(now).toISOString(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 90, reset_at: new Date(now + 3600000).toISOString() } } };
      const route = { provider: 'go', engine: 'opencode', profile: null, account: 'test-account' };
      const ad = [grok, muse].map(m => ({ ...m, account: route.account }));
      // Even an accidental tier restoration cannot turn preference into authority.
      const original = [...policy.tiers.daily];
      try {
        policy.tiers.daily.push(grok.id);
        for (const mode of ['normal', 'minimal', 'disabled']) {
          const saved = await service.handle(new Request(path, { method: 'POST', body: JSON.stringify({ mode, reason: 'preference proof' }) }), admin);
          expect(saved.status).toBe(200);
          expect(await saved.json()).toMatchObject({ mode, effective_mode: 'disabled', factor: 0, enabled: false, coding_allowed: false, ask_last_resort: false, policy_reason: 'model_disabled_by_policy' });
          expect(modelCandidates({ tier: 'daily', model: grok.id }, route, ad, quota, now)).toEqual([]);
          expect(modelCandidates({ tier: 'daily', model: muse.id }, route, ad, quota, now).map(m => m.id)).toContain('muse');
        }
      } finally { policy.tiers.daily.splice(0, policy.tiers.daily.length, ...original); }
      // Legacy stored preferences also remain effectively disabled after restart.
      hub.sql.exec("UPDATE grok_policy SET mode='minimal' WHERE singleton=1");
      const restarted = new RoutingService(hub.sql, null as any);
      expect(await (await restarted.handle(new Request(path), admin)).json()).toMatchObject({ mode: 'minimal', effective_mode: 'disabled', factor: 0, enabled: false });
    });
  });
  it('requires operator authority, validates and persists an audited set/revert', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('grok-policy-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const service = new RoutingService(hub.sql, null as any, () => 1793083000000, cb => state.storage.transactionSync(cb));
      const path = 'https://hub.test/api/fleet/routing/policy/grok';
      const request = (body?: any) => new Request(path, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
      const admin = { caller: { isAdmin: true, actor: 'operator-test' }, scopes: ['admin'] };
      const agent = { caller: { kind: 'agent', actor: 'agent-test' }, scopes: ['routing:submit'] };
      const before = await (await service.handle(request(), admin)).json() as any;
      expect((await service.handle(request({ mode: 'normal', reason: 'test' }), agent)).status).toBe(403);
      for (const body of [{ mode: 'oops', reason: 'test' }, { mode: 'normal', reason: '' }, { mode: 'normal', reason: 'test', extra: true }]) {
        expect((await service.handle(request(body), admin)).status).toBe(400);
      }
      expect((await service.handle(request({ mode: 'disabled', reason: 'bounded proof' }), admin)).status).toBe(200);
      expect((await (await service.handle(request(), admin)).json() as any).mode).toBe('disabled');
      expect((await service.handle(request({ mode: before.mode, reason: 'revert proof' }), admin)).status).toBe(200);
      const audit: any[] = hub.sql.exec('SELECT * FROM grok_policy_audit ORDER BY rowid').toArray();
      expect(audit).toHaveLength(2);
      expect(audit[0]).toMatchObject({ previous_mode: before.mode, mode: 'disabled', actor: 'operator-test' });
      expect(audit[1]).toMatchObject({ previous_mode: 'disabled', mode: before.mode, reason: 'revert proof' });
      const restarted = new RoutingService(hub.sql, null as any);
      expect((await (await restarted.handle(request(), admin)).json() as any).mode).toBe(before.mode);
      // A state-write failure must roll back its audit entry as well.
      hub.sql.exec("CREATE TRIGGER reject_grok_write BEFORE INSERT ON grok_policy BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END");
      await expect(service.handle(request({ mode: 'normal', reason: 'must roll back' }), admin)).rejects.toThrow('synthetic failure');
      expect(hub.sql.exec('SELECT * FROM grok_policy_audit').toArray()).toHaveLength(2);
      expect((await (await service.handle(request(), admin)).json() as any).mode).toBe(before.mode);
    });
  });
});
