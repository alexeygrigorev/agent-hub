import policy from '../config/model-routing.json';
import { evaluateQuotaAdmission } from './quota_admission';
export { policy };
export function taskTier(input: any): string | null {
  if ((input.tier !== undefined && typeof input.tier !== 'string') || (input.difficulty !== undefined && (typeof input.difficulty !== 'string' || !Object.hasOwn(policy.rules.difficulty_tiers, input.difficulty)))) return null;
  const explicitModel = policy.models.find(m => [m.id, m.model].includes(input.model));
  const inferred = explicitModel ? Object.entries(policy.tiers).find(([, ids]) => ids.includes(explicitModel.id))?.[0] : null;
  const value = input.tier ?? inferred ?? (input.difficulty ? (policy.rules.difficulty_tiers as any)[input.difficulty] : policy.default_tier);
  const tier = (policy.aliases as any)[value] ?? value;
  return Object.hasOwn(policy.tiers, tier) ? tier : null;
}
/** Reuses launcher/ranking.py's bounded headroom/reset formula after hard admission gates. */
export function modelCandidates(input: any, route: any, advertisement: any, quota: any, now: number) {
  const tier = taskTier(input); if (!tier) return [];
  const members: string[] = (policy.tiers as any)[tier];
  return policy.models.filter(m => {
    if (!members.includes(m.id) || (input.model && ![m.id, m.model].includes(input.model))) return false;
    if (policy.rules.disabled_providers.includes(m.provider)) return false;
    if (policy.rules.explicit_only_providers.includes(m.provider) && input.preferred_runner !== m.engine && ![m.id, m.model].includes(input.model)) return false;
    return m.provider === route.provider && m.engine === route.engine && m.profile === route.profile &&
      advertisement.some((a: any) => a.engine === m.engine && a.profile === m.profile && a.model === m.model && a.effort === m.effort && a.account === route.account);
  }).map(m => {
    // Canonical fail-closed admission shared with dispatch/accountStatus/claim.
    // Policy data (reserves, ranking, preferences) is unchanged and still owned here.
    const admission = evaluateQuotaAdmission({ provider: m.provider, quota, subscriptionObservedAt: null, nowMs: now, reserves: ((policy.rules as any).reserve_percent || {}) as Record<string, number> });
    if (!admission.ok) return null;
    const windows: any[] = Object.values(quota?.windows || {}).filter((w: any) => w.present !== false && Number.isFinite(w.percent_remaining));
    const remaining = Math.min(...windows.map(w => w.percent_remaining)) / 100;
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining * 100 <= ((policy.rules.reserve_percent as any)[m.provider] ?? 0)) return null;
    const hours = Math.min(...windows.map(w => (Date.parse(w.reset_at) - now) / 3600000));
    const urgency = Number.isFinite(hours) ? remaining * Math.max(0, Math.min(1, (policy.rules.reset_horizon_hours - hours) / policy.rules.reset_horizon_hours)) : 0;
    const preference = policy.rules.preferred_providers.includes(m.provider) ? policy.rules.provider_preference : 1;
    return { ...m, tier, headroom: Math.max(policy.rules.headroom_floor, remaining), weight: preference * Math.max(policy.rules.headroom_floor, remaining) * (1 + policy.rules.reset_urgency_factor * urgency) };
  }).filter((m): m is NonNullable<typeof m> => m !== null);
}
