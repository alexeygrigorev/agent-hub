import { describe, expect, it } from "vitest";
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import policy from "../config/model-routing.json";
import { modelCandidates, modelDisabledByPolicy } from "../src/model-routing";

// Alexey t1621u (2026-10-27): Grok is for bots only; no Grok route may be
// eligible for any Hub routing tier (intake 3c9eafa1).
const isGrok = (s: string) => /grok/i.test(s);

describe("Grok disabled in Hub routing (t1621u)", () => {
  it("no tier lists a Grok-family model", () => {
    const grokIds = new Set(policy.models.filter((m: any) => isGrok(m.id) || isGrok(m.model)).map((m: any) => m.id));
    for (const [tier, ids] of Object.entries(policy.tiers)) {
      expect((ids as string[]).filter(id => grokIds.has(id) || isGrok(id)), tier).toEqual([]);
    }
  });
  it("blocks model families regardless of provider or capitalization", () => {
    expect(policy.rules.disabled_model_patterns).toContain('grok');
    for (const value of ['grok', 'grok-4-7', 'opencode-go/grok-4.7', 'xai/GROK', 'muse/gRoK']) {
      expect(modelDisabledByPolicy({ id: 'innocent-alias', model: value })).toBe(true);
      expect(modelDisabledByPolicy({ id: value, model: 'innocent' })).toBe(true);
    }
    expect(modelDisabledByPolicy({ id: 'muse', model: 'opencode-go/muse-spark-1.3-contributor' })).toBe(false);
  });
  it("model rule still blocks a disguised Grok model even if tier membership is restored", () => {
    const model = policy.models.find(m => m.id === 'muse')!;
    const original = model.model;
    try {
      const now = Date.now();
      const quota = { status: 'ok', observed_at: new Date(now).toISOString(), details: { limit_reached: false, has_grok_code_access: true }, windows: { weekly: { present: true, percent_remaining: 90, reset_at: new Date(now + 3600000).toISOString() } } };
      const route = { ...model, account: 'acct' };
      expect(modelCandidates({ tier: 'daily', preferred_runner: model.engine }, route, [{ ...model, account: 'acct' }], quota, now).map(m => m.id)).toContain('muse');
      model.model = 'opencode-go/GrOk-4.7';
      const ad = [{ ...model, account: 'acct' }];
      expect(modelCandidates({ tier: 'daily' }, route, ad, quota, now)).toEqual([]);
      expect(modelCandidates({ model: model.id }, route, ad, quota, now)).toEqual([]);
    } finally { model.model = original; }
  });
  it("fleet rejects explicit Grok requests for all tiers, projects and start modes", async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('no-grok-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb));
      service.init();
      for (const engine of ['grok', 'grok-4-7', 'opencode-go/grok-4.7', 'xai/GROK', 'muse/gRoK']) {
        for (const tier of [...Object.keys(policy.tiers), ...Object.keys(policy.aliases)]) {
          for (const project of ['agent-hub', 'AISHL']) {
            for (const mode of ['headless', 'interactive']) {
              for (const withGoal of mode === 'interactive' ? [false, true] : [false]) {
                const input = { engine, tier, project, mode, workspace: 'demo', prompt: 'policy test', ...(withGoal ? { with_goal: true } : {}) };
                expect(service.plan(input)).toMatchObject({ error: 'model_disabled_by_policy' });
                if (tier === 'daily' && project === 'AISHL') {
                  const reserved = service.reserve({ ...input, idempotency_key: crypto.randomUUID(), access: { mode: 'read' } }, { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] });
                  expect(reserved.status).toBe(409);
                  expect(reserved.value.error).toBe('model_disabled_by_policy');
                }
              }
            }
          }
        }
      }
      expect(hub.sql.exec('SELECT COUNT(*) AS n FROM fleet_launches').one().n).toBe(0);
    });
  });
  it("the native grok provider stays disabled", () => {
    expect(policy.rules.disabled_providers).toContain("grok");
  });
  it("no Grok candidate for any tier, alias or explicit grok model", () => {
    const grokModels = policy.models.filter((m: any) => isGrok(m.id) || isGrok(m.model));
    const now = Date.now();
    const quota = { status: 'ok', observed_at: new Date(now).toISOString(), details: { limit_reached: false, has_grok_code_access: true }, windows: { w: { present: true, percent_remaining: 90, reset_at: new Date(now + 3600000).toISOString() } } };
    const tiers = [...Object.keys(policy.tiers), ...Object.keys(policy.aliases)];
    for (const m of grokModels as any[]) {
      const route = { provider: m.provider, engine: m.engine, profile: m.profile, account: "acct" };
      const ad = [{ engine: m.engine, profile: m.profile, model: m.model, effort: m.effort, account: "acct" }];
      for (const tier of tiers) expect(modelCandidates({ tier }, route, ad, quota, now).filter((c: any) => isGrok(c.id) || isGrok(c.model)), tier + "/" + m.id).toEqual([]);
      for (const model of [m.id, m.model]) expect(modelCandidates({ model }, route, ad, quota, now).filter((c: any) => isGrok(c.id) || isGrok(c.model)), model).toEqual([]);
    }
  });
});
