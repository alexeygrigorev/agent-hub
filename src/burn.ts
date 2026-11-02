/**
 * Burn-aware routing (intake ebfe9d85, t1822u): subscription quota is
 * use-it-or-lose-it. For each account the long quota window (weekly, monthly)
 * that resets first decides how much quota will expire unused. The forecast
 * extrapolates the account's own average burn rate in the current cycle up to
 * that reset; pressure is the projected unused fraction weighted by how close
 * the reset is inside `reset_horizon_hours` (>= 7 days). Routing multiplies
 * candidate scores by `1 + burn_urgency_factor * pressure`, so a 95%-left
 * account that resets in ~28 h outranks a static provider preference.
 *
 * Pure functions over the canonical quota shape; unknown evidence never
 * creates capacity (admission gates run before this factor) and never applies
 * banked resets.
 */
import policy from '../config/model-routing.json';
import { evaluateQuotaAdmission } from './quota_admission';

const HOUR = 3600000;

/** Nominal cycle length per quota window name, in hours. */
const CYCLE_HOURS: Record<string, number> = {
  '1h': 1, hourly: 1, '5h': 5, five_hour: 5, '5hour': 5,
  daily: 24, day: 24, '24h': 24,
  weekly: 168, week: 168, '7d': 168,
  monthly: 720, month: 720, '30d': 720,
};

export interface BurnRules {
  horizonHours: number; kappa: number; minCycleHours: number; minElapsedHours: number;
  alertPercent: number; releasePercent: number;
}

const num = (v: unknown, fallback: number) => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : fallback;

export function burnRules(rules: any = (policy as any).rules): BurnRules {
  return {
    horizonHours: Math.max(168, num(rules?.reset_horizon_hours, 168)),
    kappa: num(rules?.burn_urgency_factor, 3),
    minCycleHours: num(rules?.burn_min_cycle_hours, 24),
    minElapsedHours: num(rules?.burn_min_elapsed_hours, 12),
    alertPercent: num(rules?.burn_waste_alert_percent, 30),
    releasePercent: num(rules?.explicit_only_burn_release_percent, 30),
  };
}

export interface BurnForecast {
  window: string;
  remaining_percent: number;
  reset_at: string;
  hours_to_reset: number;
  cycle_hours: number;
  /** Average % of the window used per hour so far this cycle; null before burn_min_elapsed_hours. */
  burn_rate_percent_per_hour: number | null;
  /** Remaining % above reserve expected to expire unused at reset. */
  projected_unused_percent: number;
  pressure: number;
  factor: number;
  /** projected unused > burn_waste_alert_percent with a measured burn rate. */
  waste_alert: boolean;
}

const round = (v: number, digits = 2) => Math.round(v * 10 ** digits) / 10 ** digits;

/**
 * Forecast for one account quota observation. Returns null when no present,
 * timed window exists. Long windows win over short admission windows.
 */
export function burnForecast(quota: any, nowMs: number, reservePercent = 0, rules: BurnRules = burnRules()): BurnForecast | null {
  // Only current, healthy observations support a forecast or explicit-only release.
  // Claude validation also permits the documented inactive five-hour window
  // shape shared by Claude/ZAI; it changes no provider's launch admission.
  if (!evaluateQuotaAdmission({ provider: 'claude', quota, subscriptionObservedAt: null, nowMs, reserves: {} }).ok) return null;
  const windows = quota && typeof quota === 'object' && quota.windows && typeof quota.windows === 'object' ? quota.windows : null;
  if (!windows) return null;
  let best: { name: string; pct: number; resetMs: number; cycle: number; rolling: boolean } | null = null;
  for (const [name, w] of Object.entries(windows) as [string, any][]) {
    if (!w || w.present === false || w.error) continue;
    const cycle = CYCLE_HOURS[name];
    if (!cycle) continue;
    const pct = w.percent_remaining;
    if (typeof pct !== 'number' || !Number.isFinite(pct) || pct < 0 || pct > 100) continue;
    const resetMs = typeof w.reset_at === 'string' ? Date.parse(w.reset_at) : NaN;
    if (!Number.isFinite(resetMs) || resetMs <= nowMs) continue;
    // Prefer long-term pools; subscriptions with only short windows (Go/Gemini)
    // still participate rather than silently getting a neutral multiplier.
    const isLong = cycle >= rules.minCycleHours;
    const bestLong = best !== null && best.cycle >= rules.minCycleHours;
    if (!best || (isLong && !bestLong) || (isLong === bestLong && resetMs < best.resetMs)) best = { name, pct, resetMs, cycle, rolling: w.rolling === true };
  }
  if (!best) return null;
  const hoursLeft = (best.resetMs - nowMs) / HOUR;
  if (hoursLeft > best.cycle || !Number.isFinite(reservePercent) || reservePercent < 0 || reservePercent > 100) return null;
  const elapsed = best.cycle - hoursLeft;
  const rate = !best.rolling && elapsed > 0 && elapsed >= Math.min(rules.minElapsedHours, best.cycle / 2) ? Math.max(0, 100 - best.pct) / elapsed : null;
  const burnable = Math.max(0, best.pct - Math.max(0, reservePercent));
  // An early-cycle estimate is provisional and cannot release explicit-only routes.
  const projected = Math.max(0, burnable - (rate ?? 0) * hoursLeft);
  const h = rules.horizonHours;
  const horizon = Math.min(h, best.cycle);
  const closeness = Math.max(0, Math.min(1, (horizon - hoursLeft) / horizon));
  const pressure = (projected / 100) * closeness;
  return {
    window: best.name,
    remaining_percent: best.pct,
    reset_at: new Date(best.resetMs).toISOString(),
    hours_to_reset: round(hoursLeft, 1),
    cycle_hours: best.cycle,
    burn_rate_percent_per_hour: rate === null ? null : round(rate, 3),
    projected_unused_percent: round(projected, 1),
    pressure: round(pressure, 4),
    factor: round(1 + rules.kappa * pressure, 4),
    waste_alert: rate !== null && projected > rules.alertPercent,
  };
}

/** Score multiplier (>= 1); neutral without a forecast. */
export const burnFactor = (f: BurnForecast | null) => f ? f.factor : 1;

/**
 * Explicit-only providers (Codex) are released for tier-only daily/deep work
 * while their quota is forecast to expire unused above the release threshold.
 */
export const burnReleasesExplicitOnly = (f: BurnForecast | null, rules: BurnRules = burnRules()) =>
  !!f && f.burn_rate_percent_per_hour !== null && f.projected_unused_percent >= rules.releasePercent;
