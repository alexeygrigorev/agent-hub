import policy from '../config/model-routing.json';
import { evaluateLaunchQuota } from './quota_admission';
import { canonicalAccountLabel } from './quota';
import { burnFactor, burnForecast, burnReleasesExplicitOnly, burnRules } from './burn';
import { grokDemotion, netHeadroom, pacingFor, providerPreference, resetUrgency, routeScore, type ProviderPacing, type RouteScoreFactors } from './pacing';
export { policy };

const DAY_MS = 86_400_000;

/** Parse an inclusive YYYY-MM-DD plan end into the next UTC midnight. */
export function planEndExpiryMs(value: unknown): number | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const start = Date.parse(`${value}T00:00:00.000Z`);
  if (!Number.isFinite(start) || new Date(start).toISOString().slice(0, 10) !== value) return null;
  return start + DAY_MS;
}

const accountPlanEnds = new Map<string, { date: string; expiresAtMs: number }>();
for (const entry of (((policy as any).accounts ?? []) as any[])) {
  const date = typeof entry?.plan_end === 'string' ? entry.plan_end : null;
  const expiresAtMs = planEndExpiryMs(date);
  if (typeof entry?.account === 'string' && date && expiresAtMs !== null) {
    accountPlanEnds.set(canonicalAccountLabel(entry.account), { date, expiresAtMs });
  }
}

export function accountPlanEndDate(label: string): string | null {
  return accountPlanEnds.get(canonicalAccountLabel(label))?.date ?? null;
}

export function accountPlanEndMs(label: string): number | null {
  return accountPlanEnds.get(canonicalAccountLabel(label))?.expiresAtMs ?? null;
}

export function accountPlanExpired(label: string, nowMs: number): boolean {
  const end = accountPlanEndMs(label);
  return end !== null && nowMs >= end;
}

/** Plan-end deadline for reset burning while the subscription is near expiry. */
export function planEndBurnMode(label: string, nowMs: number): number | null {
  const end = accountPlanEndMs(label);
  const horizonDays = Number((policy.rules as any).plan_end_reset_horizon_days);
  if (end === null || end <= nowMs || !Number.isFinite(horizonDays) || horizonDays <= 0) return null;
  return end - nowMs <= horizonDays * DAY_MS ? end : null;
}
/** Case-insensitive literal model-family matches, independent of provider. */
export function modelDisabledByPolicy(model: any): boolean {
  const values = typeof model === 'string' ? [model] : [model?.id, model?.model];
  return policy.rules.disabled_model_patterns.some(pattern => pattern.length > 0 &&
    values.some(value => typeof value === 'string' && value.toLowerCase().includes(pattern.toLowerCase())));
}
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
 * weight = fit * health * preference * headroom * urgency * pacing * grok * banked * burn.
 * `pacing` comes from tbm_* rows; omitted or empty keeps those factors neutral.
 * `burn` (src/burn.ts) favours quota forecast to expire unused before its reset,
 * and releases explicit-only providers for daily/deep work while that waste is large.
 */
export function modelCandidates(input: any, route: any, advertisement: any, quota: any, now: number, pacing?: Map<string, ProviderPacing>, unavailableEngines?: Set<string>) {
  // Paid routing is unsupported. Reject an incompatible policy rather than
  // advertising subscription-only behavior while silently accepting paid mode.
  if (policy.rules.allow_paid !== false || policy.rules.cost_mode !== 'existing-subscription-only') return [];
  const tier = taskTier(input); if (!tier) return [];
  const members: string[] = (policy.tiers as any)[tier];
  const rules = burnRules();
  const reserves = ((policy.rules as any).reserve_percent || {}) as Record<string, number>;
  const releaseTiers: string[] = (policy.rules as any).explicit_only_burn_release_tiers || [];
  const forecastFor = (provider: string) => burnForecast(quota, now, reserves[provider] ?? 0, rules);
  return policy.models.filter(m => {
    if (!members.includes(m.id) || (input.model && ![m.id, m.model].includes(input.model))) return false;
    if (modelDisabledByPolicy(m) || policy.rules.disabled_providers.includes(m.provider)) return false;
    if (unavailableEngines && unavailableEngines.has(m.engine)) return false;
    if (policy.rules.explicit_only_providers.includes(m.provider) && input.preferred_runner !== m.engine && ![m.id, m.model].includes(input.model)
      && !(releaseTiers.includes(tier) && burnReleasesExplicitOnly(forecastFor(m.provider), rules))) return false;
    const routeFam = route.provider === 'codex-orin' ? 'codex' : route.provider;
    const profile = route.profile === m.profile || (m.profile === null && route.profile === 'hub-' + canonicalAccountLabel(route.account));
    return m.provider === routeFam && m.engine === route.engine && profile &&
      advertisement.some((a: any) => a.engine === m.engine && a.profile === route.profile && a.model === m.model && a.effort === m.effort &&
        typeof a.account === 'string' && typeof route.account === 'string' && canonicalAccountLabel(a.account) === canonicalAccountLabel(route.account));
  }).map(m => {
    // Canonical fail-closed admission shared with dispatch/accountStatus/claim.
    // Policy data (reserves, ranking, preferences) is unchanged and still owned here.
    const admission = evaluateLaunchQuota({ provider: m.provider, quota, subscriptionObservedAt: null, nowMs: now, reserves: ((policy.rules as any).reserve_percent || {}) as Record<string, number> });
    if (!admission.ok) return null;
    const windows: any[] = Object.values(quota?.windows || {}).filter((w: any) => w.present !== false && Number.isFinite(w.percent_remaining) && typeof w.reset_at === 'string' && Date.parse(w.reset_at) > now);
    const unmetered = quota?.details?.unmetered === true ? (policy.rules as any).unmetered_headroom_percent : null;
    const remaining = (admission.remaining ?? unmetered ?? policy.rules.headroom_floor * 100) / 100;
    if (!Number.isFinite(remaining) || remaining <= 0 || remaining * 100 <= ((policy.rules.reserve_percent as any)[m.provider] ?? 0)) return null;
    const hours = windows.length ? Math.min(...windows.map(w => (Date.parse(w.reset_at) - now) / 3600000)) : Infinity;
    const paced = pacingFor(pacing, m.provider, route.account, route.provider);
    if (paced.halted) return null; // tbm kill switch or floor breach: no auto-routed burn on this route.
    const headroom = netHeadroom(remaining * 100, (policy.rules.reserve_percent as any)[m.provider] ?? 0);
    const health = typeof paced.efficiency === 'number' && Number.isFinite(paced.efficiency) && paced.efficiency > 0 ? paced.efficiency : 1;
    const factors: RouteScoreFactors = { fit: 1, health, preference: providerPreference(m.provider), headroom,
      urgency: resetUrgency(headroom, hours), pacing: paced.pacing, grok: grokDemotion(m.provider), banked: paced.banked };
    const burn = forecastFor(m.provider);
    factors.burn = burnFactor(burn);
    return { ...m, tier, headroom: Math.max(policy.rules.headroom_floor, remaining), weight: routeScore(factors), factors, burn, preempt: paced.preempt };
  }).filter((m): m is NonNullable<typeof m> => m !== null);
}
