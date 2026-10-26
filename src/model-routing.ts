import policy from '../config/model-routing.json';
import { evaluateQuotaAdmission } from './quota_admission';
import { canonicalAccountLabel } from './quota';
import { grokDemotion, netHeadroom, pacingFor, providerPreference, resetUrgency, routeScore, type ProviderPacing, type RouteScoreFactors } from './pacing';
export { policy };
export function taskTier(input: any): string | null {
  if ((input.tier !== undefined && typeof input.tier !== 'string') || (input.difficulty !== undefined && (typeof input.difficulty !== 'string' || !Object.hasOwn(policy.rules.difficulty_tiers, input.difficulty)))) return null;
  const explicitModel = policy.models.find(m => [m.id, m.model].includes(input.model));
  const inferred = explicitModel ? Object.entries(policy.tiers).find(([, ids]) => ids.includes(explicitModel.id))?.[0] : null;
  const value = input.tier ?? inferred ?? (input.difficulty ? (policy.rules.difficulty_tiers as any)[input.difficulty] : policy.default_tier);
  const tier = (policy.aliases as any)[value] ?? value;
  return Object.hasOwn(policy.tiers, tier) ? tier : null;
}
/**
 * Hard admission gates first, then the TBM multi-factor score (src/pacing.ts):
 * weight = fit * health * preference * headroom * urgency * pacing * grok * banked.
 * `pacing` comes from tbm_* rows; omitted or empty keeps those factors neutral.
 */
export function modelCandidates(input: any, route: any, advertisement: any, quota: any, now: number, pacing?: Map<string, ProviderPacing>, unavailableEngines?: Set<string>) {
  const tier = taskTier(input); if (!tier) return [];
  const members: string[] = (policy.tiers as any)[tier];
  return policy.models.filter(m => {
    if (!members.includes(m.id) || (input.model && ![m.id, m.model].includes(input.model))) return false;
    if (policy.rules.disabled_providers.includes(m.provider)) return false;
    if (unavailableEngines && unavailableEngines.has(m.engine)) return false;
    if (policy.rules.explicit_only_providers.includes(m.provider) && input.preferred_runner !== m.engine && ![m.id, m.model].includes(input.model)) return false;
    const routeFam = route.provider === 'codex-orin' ? 'codex' : route.provider;
    return m.provider === routeFam && m.engine === route.engine && m.profile === route.profile &&
      advertisement.some((a: any) => a.engine === m.engine && a.profile === m.profile && a.model === m.model && a.effort === m.effort &&
        typeof a.account === 'string' && typeof route.account === 'string' && canonicalAccountLabel(a.account) === canonicalAccountLabel(route.account));
  }).map(m => {
    // Canonical fail-closed admission shared with dispatch/accountStatus/claim.
    // Policy data (reserves, ranking, preferences) is unchanged and still owned here.
    const admission = evaluateQuotaAdmission({ provider: m.provider, quota, subscriptionObservedAt: null, nowMs: now, reserves: ((policy.rules as any).reserve_percent || {}) as Record<string, number> });
    if (!admission.ok) return null;
    const windows: any[] = Object.values(quota?.windows || {}).filter((w: any) => w.present !== false && Number.isFinite(w.percent_remaining));
    const remaining = Math.min(...windows.map(w => w.percent_remaining)) / 100;
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining * 100 <= ((policy.rules.reserve_percent as any)[m.provider] ?? 0)) return null;
    const hours = Math.min(...windows.map(w => (Date.parse(w.reset_at) - now) / 3600000));
    const paced = pacingFor(pacing, m.provider, route.account, route.provider);
    if (paced.halted) return null; // tbm kill switch or floor breach: no auto-routed burn on this route.
    const headroom = netHeadroom(remaining * 100, (policy.rules.reserve_percent as any)[m.provider] ?? 0);
    const health = typeof paced.efficiency === 'number' && Number.isFinite(paced.efficiency) && paced.efficiency > 0 ? paced.efficiency : 1;
    const factors: RouteScoreFactors = { fit: 1, health, preference: providerPreference(m.provider), headroom,
      urgency: resetUrgency(headroom, hours), pacing: paced.pacing, grok: grokDemotion(m.provider), banked: paced.banked };
    return { ...m, tier, headroom: Math.max(policy.rules.headroom_floor, remaining), weight: routeScore(factors), factors, preempt: paced.preempt };
  }).filter((m): m is NonNullable<typeof m> => m !== null);
}
