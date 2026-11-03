import { describe, expect, it } from 'vitest';
import { deepModelAllowedForTier, heavyModelEffortAllowed, isDeepModel, isHeavyModel, modelCandidates, policy, taskTier } from '../src/model-routing';

// Intake abd728b2: heavy models stay at medium, and deep models stay out of daily.
const NOW = Date.parse('2026-11-03T00:00:00Z');
const HEAVY_MODEL_IDS = ['sol', 'astra', 'argon', 'argon-gemini'];
const REQUIRED_HEAVY_MODEL_IDS = ['sol', 'astra', 'argon', 'argon-gemini'];
const DEEP_MODEL_IDS = ['argon', 'argon-gemini', 'astra'];
const GEMINI4_ARGON_MODELS = (policy.models as any[])
  .filter(model => String(model.model).startsWith('gemini-4-argon-'));
const ARGON_ROUTE_IDS = (policy.models as any[])
  .filter(model => model.provider === 'gemini' && String(model.model).startsWith('gemini-4-argon-'))
  .map(model => model.id);

const quota = {
  status: 'ok',
  observed_at: new Date(NOW - 1_000).toISOString(),
  details: { limit_reached: false },
  windows: {
    '7d': {
      present: true,
      percent_remaining: 100,
      reset_at: new Date(NOW + 24 * 60 * 60_000).toISOString(),
    },
  },
};

function routeFor(model: any) {
  const account = (policy.accounts as any[]).find(candidate => candidate.provider === model.provider);
  if (!account) throw new Error(`missing configured account for ${model.provider}`);
  return {
    provider: model.provider,
    engine: model.engine,
    profile: model.profile ?? `hub-${account.account}`,
    account: account.account,
  };
}

describe('heavy model routing defaults', () => {
  const models = new Map((policy.models as any[]).map(model => [model.id, model]));
  const configuredDeepModelIds = DEEP_MODEL_IDS.filter(id => models.has(id));

  it('pins Claude route effort to Sonnet medium even when a caller overrides effort', () => {
    const sonnet = models.get('sonnet');
    expect(sonnet).toBeDefined();
    expect(heavyModelEffortAllowed(sonnet, 'medium')).toBe(true);
    for (const effort of ['low', 'high', 'max', 'xhigh', undefined]) {
      expect(heavyModelEffortAllowed(sonnet, effort), String(effort)).toBe(false);
    }
  });

  it('keeps every heavy model at medium effort and uses the non-high Argon model id', () => {
    for (const id of REQUIRED_HEAVY_MODEL_IDS) {
      const model = models.get(id);
      expect(model, `missing heavy model ${id}`).toBeDefined();
      expect(isHeavyModel(model), id).toBe(true);
      expect(model.effort, id).toBe('medium');
      expect(heavyModelEffortAllowed(model, 'medium'), id).toBe(true);
      expect(heavyModelEffortAllowed(model, 'high'), id).toBe(false);
      expect(heavyModelEffortAllowed(model, 'HIGH'), id).toBe(false);
      expect(heavyModelEffortAllowed(model, 'max'), id).toBe(false);
      expect(heavyModelEffortAllowed(model, 'xhigh'), id).toBe(false);
      expect(heavyModelEffortAllowed(model, undefined), id).toBe(false);
    }
    for (const id of HEAVY_MODEL_IDS) {
      expect(isHeavyModel(id), id).toBe(true);
      expect(heavyModelEffortAllowed(id, 'high'), id).toBe(false);
      expect(heavyModelEffortAllowed(id, undefined), id).toBe(false);
      const model = models.get(id);
      if (model) expect(model.effort, id).toBe('medium');
    }
    // Claude Opus is not a configured route; Claude policy uses Sonnet only.
    expect(models.has('opus')).toBe(false);
    expect(isHeavyModel('gemini-4-argon-high')).toBe(true);
    expect(heavyModelEffortAllowed('gemini-4-argon-high', 'high')).toBe(false);
    expect(isDeepModel('gemini-4-argon-medium')).toBe(true);
    expect(isHeavyModel('gpt-6-astra')).toBe(true);
    expect(heavyModelEffortAllowed('gpt-6-astra', 'high')).toBe(false);
    expect(isDeepModel('gpt-6-astra')).toBe(true);
    expect(isHeavyModel('gpt-6.1-sol')).toBe(true);
    expect(heavyModelEffortAllowed('gpt-6.1-sol', 'high')).toBe(false);
    expect(isHeavyModel('opus')).toBe(false);
    expect(isDeepModel('opus')).toBe(false);
    expect(isHeavyModel('gemini-4-argon-contributor')).toBe(false);
    expect(isDeepModel('gemini-4-argon-contributor')).toBe(false);
    for (const model of policy.models as any[]) {
      if (isHeavyModel(model)) expect(model.effort, model.id).toBe('medium');
    }

    for (const id of ['argon', 'argon-gemini']) {
      expect(models.get(id).model, id).toBe('gemini-4-argon-medium');
    }

    for (const model of GEMINI4_ARGON_MODELS) {
      expect(model.model, model.id).not.toBe('gemini-4-argon-high');
      expect(model.effort, model.id).not.toBe('high');
    }

    for (const id of ARGON_ROUTE_IDS) {
      expect(models.get(id).model, id).toBe('gemini-4-argon-medium');
      expect(models.get(id).effort, id).toBe('medium');
    }
  });

  it('keeps heavy deep-model selectors out of the daily default tier', () => {
    expect(policy.default_tier).toBe('daily');
    expect(taskTier({})).toBe('daily');
    expect(taskTier({ task_kind: 'implement' })).toBe('daily');
    for (const id of DEEP_MODEL_IDS) {
      expect(policy.tiers.daily, id).not.toContain(id);
    }
    for (const model of policy.models as any[]) {
      if (isDeepModel(model)) expect(policy.tiers.daily, model.id).not.toContain(model.id);
    }

    const contributor = models.get('gemini-4-argon-contributor');
    expect(contributor, 'daily OpenCode Argon Contributor remains a distinct model').toBeDefined();
    expect(policy.tiers.daily).toContain('gemini-4-argon-contributor');
    expect(isHeavyModel(contributor)).toBe(false);
    expect(isDeepModel(contributor)).toBe(false);
  });

  it('does not resolve heavy models at high effort for plain implement starts', () => {
    const configuredHeavyModelIds = (policy.models as any[]).filter(isHeavyModel).map(model => model.id);
    for (const id of new Set([...configuredHeavyModelIds, ...ARGON_ROUTE_IDS])) {
      const model = models.get(id);
      expect(model, `missing heavy model ${id}`).toBeDefined();
      const route = routeFor(model);
      const advertisement = [{
        engine: model.engine,
        profile: route.profile,
        model: model.model,
        effort: model.effort,
        account: route.account,
      }];

      for (const input of [{}, { task_kind: 'implement' }, { tier: 'daily', task_kind: 'implement' }] as const) {
        const resolved = modelCandidates(input, route, advertisement, quota, NOW);
        expect(resolved.every(candidate => candidate.effort !== 'high'), id).toBe(true);
        if (DEEP_MODEL_IDS.includes(id)) {
          expect(resolved.map(candidate => candidate.id), id).not.toContain(id);
        }
      }
    }
  });

  it('resolves deep candidates at medium only when explicitly requested', () => {
    for (const id of configuredDeepModelIds) {
      const model = models.get(id);
      expect(model, `missing deep model ${id}`).toBeDefined();
      expect(isDeepModel(model), id).toBe(true);
      expect(deepModelAllowedForTier(model, 'daily', false), id).toBe(false);
      expect(deepModelAllowedForTier(model, 'daily', true), id).toBe(true);
      expect(deepModelAllowedForTier(model, 'deep', false), id).toBe(false);
      expect(deepModelAllowedForTier(model, 'deep', true), id).toBe(true);
      const route = routeFor(model);
      const advertisement = [{
        engine: model.engine,
        profile: route.profile,
        model: model.model,
        effort: model.effort,
        account: route.account,
      }];

      for (const input of [{}, { task_kind: 'implement' }, { tier: 'daily', task_kind: 'implement' }] as const) {
        expect(modelCandidates(input, route, advertisement, quota, NOW).map(candidate => candidate.id), id).not.toContain(id);
      }

      const unreasonedDailyPin = modelCandidates(
        { tier: 'daily', model: id }, route, advertisement, quota, NOW,
      );
      expect(unreasonedDailyPin.map(candidate => candidate.id), id).not.toContain(id);

      const unreasonedDeepPin = modelCandidates(
        { tier: 'deep', model: id }, route, advertisement, quota, NOW,
      );
      expect(unreasonedDeepPin.map(candidate => candidate.id), id).not.toContain(id);

      const [resolved] = modelCandidates(
        { tier: 'deep', model: id, engine_override_reason: 'Explicit deep-model routing regression check.' },
        route, advertisement, quota, NOW,
      );
      expect(resolved, id).toMatchObject({ id, effort: 'medium', tier: 'deep' });

      const [explicitDailyPin] = modelCandidates(
        { tier: 'daily', model: id, engine_override_reason: 'Explicit deep-model routing regression check.' },
        route, advertisement, quota, NOW,
      );
      expect(explicitDailyPin, id).toMatchObject({ id, effort: 'medium', tier: 'daily' });
    }
  });
});
