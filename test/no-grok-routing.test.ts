import { describe, expect, it } from "vitest";
import policy from "../config/model-routing.json";
import { modelCandidates } from "../src/model-routing";

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
  it("the native grok provider stays disabled", () => {
    expect(policy.rules.disabled_providers).toContain("grok");
  });
  it("no Grok candidate for any tier, alias or explicit grok model", () => {
    const grokModels = policy.models.filter((m: any) => isGrok(m.id) || isGrok(m.model));
    const now = Date.now();
    const quota = { windows: { w: { present: true, percent_remaining: 90, reset_at: new Date(now + 3600000).toISOString() } } };
    const tiers = [...Object.keys(policy.tiers), ...Object.keys(policy.aliases)];
    for (const m of grokModels as any[]) {
      const route = { provider: m.provider, engine: m.engine, profile: m.profile, account: "acct" };
      const ad = [{ engine: m.engine, profile: m.profile, model: m.model, effort: m.effort, account: "acct" }];
      for (const tier of tiers) expect(modelCandidates({ tier }, route, ad, quota, now).filter((c: any) => isGrok(c.id) || isGrok(c.model)), tier + "/" + m.id).toEqual([]);
      for (const model of [m.id, m.model]) expect(modelCandidates({ model }, route, ad, quota, now).filter((c: any) => isGrok(c.id) || isGrok(c.model)), model).toEqual([]);
    }
  });
});
