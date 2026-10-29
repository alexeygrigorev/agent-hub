/**
 * Token Burning Machine Phase 3: pacing controller, banked-tranche EDF arbiter and
 * multi-factor routing score (tbm-coord.md §2.1 items 1-4, §8 item 3).
 *
 * Supports both token-capacity pools and percentage-only pools (capacity_known=0 /
 * quota_unit='percent') without fabricating token counts, plus deadline-only banked
 * grants (quantity_known=0) and persisted PID controller ticks.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import policy from '../config/model-routing.json';
import {
  TBM_FLEET_POOL_ID,
  evaluateTbmAdmission,
  isPercentOnlyTbmPool,
  normalizeProviderFamily,
  resolveTbmPoolCycleMs,
  withPolicyTbmFloors,
} from './quota_admission';

const MINUTE = 60000;
const HOUR = 3600000;

/** Phase 1 + AC1 tbm_quota_pools columns this module reads or writes. */
export interface TbmPoolRow {
  pool_id: string; provider: string; account: string;
  total_capacity_tokens: number; remaining_tokens: number; headroom_percentage: number;
  reset_at_ms: number; hard_floor_percent: number; soft_reserve_percent: number; convexity_exponent: number;
  current_burn_rate_tpm: number; target_burn_rate_tpm: number; pacing_ratio: number;
  kill_switch_active: number;
  cycle_ms?: number | null;
  window_type?: string | null;
  remaining_percent?: number | null;
  capacity_known?: number | boolean | null;
  quota_unit?: string | null;
  kill_switch_reason?: string | null;
  last_updated_ms?: number | null;
}
/** Phase 1 + AC1 tbm_banked_tranches columns this module reads or writes. */
export interface TbmTrancheRow {
  tranche_id: string; pool_id: string; provider: string;
  remaining_tokens: number; expires_at_ms: number; status: 'active' | 'exhausted' | 'expired';
  banked_burn_rate_tpm: number; target_banked_burn_rate_tpm: number; expired_unused_tokens: number;
  allocated_tokens?: number;
  quantity_known?: number | boolean | null;
  account?: string | null;
}

export function isPercentPool(pool: Partial<TbmPoolRow>): boolean {
  return isPercentOnlyTbmPool(pool);
}

export function isDeadlineOnlyTranche(t: Partial<TbmTrancheRow>): boolean {
  return t.quantity_known === 0 || t.quantity_known === false;
}

export function poolEffectiveTotal(pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'capacity_known' | 'quota_unit' | 'remaining_tokens' | 'remaining_percent' | 'headroom_percentage'>): number {
  return isPercentPool(pool) ? 100 : pool.total_capacity_tokens;
}

export function poolEffectiveRemaining(pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'capacity_known' | 'quota_unit' | 'remaining_tokens' | 'remaining_percent' | 'headroom_percentage'>): number {
  if (isPercentPool(pool)) {
    const pct = typeof pool.remaining_percent === 'number' && Number.isFinite(pool.remaining_percent)
      ? pool.remaining_percent
      : (typeof pool.headroom_percentage === 'number' && Number.isFinite(pool.headroom_percentage) ? pool.headroom_percentage : 0);
    return Math.max(0, Math.min(100, pct));
  }
  return pool.remaining_tokens;
}

// ---------------------------------------------------------------------------
// Safety floors (§2.1 item 2)
// ---------------------------------------------------------------------------

export const FLOOR_DEFAULTS = { hardAbsTokens: 50_000, softAbsTokens: 200_000, windowMs: 7 * 24 * HOUR };

/**
 * R_hard = max(total * hard%, R_hard_abs), never above the pool itself.
 * For percentage-only pools (capacity_known=0), returns hard_floor_percent (default 5%)
 * in percentage points without applying the 50k token floor.
 * An explicit stored 0 (or any finite value in [0,100]) is honored — mirroring
 * tbmHardFloor in quota_admission — so a configured zero floor disables the
 * percentage floor AND the absolute token minimum, leaving only depletion.
 */
export function hardFloorTokens(
  pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'hard_floor_percent'> & Partial<TbmPoolRow>,
  hardAbs = FLOOR_DEFAULTS.hardAbsTokens
): number {
  const stored = pool.hard_floor_percent;
  const hardPct = typeof stored === 'number' && Number.isFinite(stored) && stored >= 0 && stored <= 100
    ? stored
    : 5;
  if (isPercentPool(pool)) {
    return Math.min(100, Math.max(0, hardPct));
  }
  if (hardPct === 0) return 0;
  return Math.min(pool.total_capacity_tokens, Math.max(pool.total_capacity_tokens * hardPct / 100, hardAbs));
}

/** R_soft(t) = R_hard + (R_soft_init - R_hard) * ((T_reset - t) / T_window)^kappa, decaying to R_hard at reset.
 * An explicit stored 0 soft_reserve_percent is honored (mirroring tbmSoftReserve): the initial
 * reserve collapses to R_hard instead of the 20% default / 200k token minimum. */
export function softReserveTokens(
  pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'hard_floor_percent' | 'soft_reserve_percent' | 'convexity_exponent' | 'reset_at_ms'> & Partial<TbmPoolRow>,
  nowMs: number,
  opts: Partial<typeof FLOOR_DEFAULTS> = {}
): number {
  const inferredWindowMs = resolveTbmPoolCycleMs({
    pool_id: pool.pool_id ?? '',
    cycle_ms: pool.cycle_ms,
    window_type: pool.window_type,
  });
  const windowMs = opts.windowMs ?? inferredWindowMs;
  const storedSoft = pool.soft_reserve_percent;
  const softPct = typeof storedSoft === 'number' && Number.isFinite(storedSoft) && storedSoft >= 0 && storedSoft <= 100
    ? storedSoft
    : 20;
  if (isPercentPool(pool)) {
    const hard = hardFloorTokens(pool, 0);
    const init = Math.min(100, Math.max(softPct, hard));
    const fraction = windowMs == null ? 1 : Math.max(0, Math.min(1, (pool.reset_at_ms - nowMs) / Math.max(1, windowMs)));
    return hard + (init - hard) * Math.pow(fraction, Math.max(1, pool.convexity_exponent || 1.5));
  }
  const o = { ...FLOOR_DEFAULTS, ...opts, windowMs };
  const hard = hardFloorTokens(pool, o.hardAbsTokens);
  const init = softPct === 0
    ? hard
    : Math.min(pool.total_capacity_tokens, Math.max(pool.total_capacity_tokens * softPct / 100, o.softAbsTokens, hard));
  const fraction = windowMs == null ? 1 : Math.max(0, Math.min(1, (pool.reset_at_ms - nowMs) / Math.max(1, windowMs)));
  return hard + (init - hard) * Math.pow(fraction, Math.max(1, pool.convexity_exponent || 1.5));
}

// ---------------------------------------------------------------------------
// Continuous target burn with PID smoothing (§2.1 item 1, tbm-pacing.md §3.1, §3.3)
// ---------------------------------------------------------------------------

/** Rates are tokens/minute (or %/minute for percentage-only pools); Kp acts on cumulative deficit e(t) = S_planned - S_actual. */
export interface PidGains { kp: number; ki: number; kd: number }
export const PID_DEFAULTS: PidGains = { kp: 1 / 60, ki: 1 / (60 * 240), kd: 0.1 };
/** Controller memory per pool (persisted in tbm_controller_state by tickTbm). */
export interface PidState { deficitTokens: number; integral: number; lastRateError: number; lastAtMs: number | null }
export const newPidState = (): PidState => ({ deficitTokens: 0, integral: 0, lastRateError: 0, lastAtMs: null });

const EPSILON_MS = 10000;

/** Base term of the AC formula: max(0, Q_rem - R_soft) / max(eps, T_reset - t), in units/minute. */
export function baseTargetTpm(pool: TbmPoolRow, nowMs: number, opts: Partial<typeof FLOOR_DEFAULTS> = {}): number {
  const rem = poolEffectiveRemaining(pool);
  const burnable = Math.max(0, rem - softReserveTokens(pool, nowMs, opts));
  return burnable / (Math.max(EPSILON_MS, pool.reset_at_ms - nowMs) / MINUTE);
}

export interface PacingOptions extends Partial<typeof FLOOR_DEFAULTS> {
  gains?: PidGains;
  /** Actuator ceiling: min(provider TPM ceiling, swarm cap * worker TPM). */
  maxBurnTpm?: number;
  /** Provider cooldown/backoff hold (`cooldown_until > now`) freezes the integrator. */
  heldUntilMs?: number | null;
}
export interface PacingResult {
  baseTpm: number; smoothingTpm: number; targetTpm: number;
  saturated: boolean; held: boolean; pacingRatio: number;
  softReserveTokens: number; hardFloorTokens: number; antiCliff: boolean; state: PidState;
}

/**
 * Q_target(t) = max(0, Q_rem - R_soft) / dt_rem + Phi_PID, clamped to [0, maxBurn].
 * The deficit integrates (base target - observed burn) between steps; the integral term
 * freezes while saturated or held (anti-windup). Returns a new state, never mutates.
 */
export function pacingStep(pool: TbmPoolRow, prev: PidState, nowMs: number, opts: PacingOptions = {}): PacingResult {
  const gains = opts.gains ?? PID_DEFAULTS;
  const maxBurn = opts.maxBurnTpm ?? Infinity;
  const held = opts.heldUntilMs != null && opts.heldUntilMs > nowMs;
  const base = baseTargetTpm(pool, nowMs, opts);
  const rateError = base - pool.current_burn_rate_tpm;
  const dtMin = prev.lastAtMs == null ? 0 : Math.max(0, (nowMs - prev.lastAtMs) / MINUTE);
  const deficit = prev.deficitTokens + rateError * dtMin;
  const derivative = dtMin > 0 ? (rateError - prev.lastRateError) / dtMin : 0;
  const tentativeIntegral = prev.integral + deficit * dtMin;
  const raw = (b: number, integral: number) => b + gains.kp * deficit + gains.ki * integral + gains.kd * derivative;
  const saturated = base >= maxBurn || raw(base, tentativeIntegral) >= maxBurn;
  const integral = saturated || held ? prev.integral : tentativeIntegral;
  const unclamped = raw(base, integral);
  const target = held ? 0 : Math.max(0, Math.min(maxBurn, unclamped));
  const hard = hardFloorTokens(pool, opts.hardAbsTokens);
  const rem = poolEffectiveRemaining(pool);
  const minutesLeft = Math.max(EPSILON_MS, pool.reset_at_ms - nowMs) / MINUTE;
  return {
    baseTpm: base, smoothingTpm: target - base, targetTpm: target, saturated, held,
    pacingRatio: target > 0 ? pool.current_burn_rate_tpm / target : 1,
    softReserveTokens: softReserveTokens(pool, nowMs, opts), hardFloorTokens: hard,
    // Anti-cliff: surplus above R_hard needs > 80% of max feasible burn to clear before reset.
    antiCliff: Number.isFinite(maxBurn) && (rem - hard) / minutesLeft > 0.8 * maxBurn,
    state: { deficitTokens: deficit, integral, lastRateError: rateError, lastAtMs: nowMs },
  };
}

// ---------------------------------------------------------------------------
// Banked tranches: Earliest-Deadline-First arbiter (§2.1 item 4, tbm-pacing.md §4.2)
// ---------------------------------------------------------------------------

export const EDF_DEFAULTS = { urgentHours: 24, criticalHours: 4, dispatchHours: 6 };
export const MAX_BANKED_URGENCY = 4;

/** Active, non-empty (or deadline-only quantity_known=0), unexpired tranches by ascending expiry; ties by remaining desc, then id. */
export function edfOrder<T extends Pick<TbmTrancheRow, 'tranche_id' | 'remaining_tokens' | 'expires_at_ms' | 'status'> & Partial<TbmTrancheRow>>(tranches: T[], nowMs: number): T[] {
  return tranches.filter(t => t.status === 'active' && (t.remaining_tokens > 0 || isDeadlineOnlyTranche(t)) && t.expires_at_ms > nowMs)
    .sort((a, b) => a.expires_at_ms - b.expires_at_ms || b.remaining_tokens - a.remaining_tokens || a.tranche_id.localeCompare(b.tranche_id));
}

/** Omega = 1 + 3 * ((24 - dtau_h) / 24)^2 inside the 24h urgent window, else 1. */
export function bankedUrgencyMultiplier(expiresAtMs: number, nowMs: number, urgentHours = EDF_DEFAULTS.urgentHours): number {
  const hours = (expiresAtMs - nowMs) / HOUR;
  if (hours <= 0 || hours > urgentHours) return 1;
  return 1 + (MAX_BANKED_URGENCY - 1) * Math.pow((urgentHours - hours) / urgentHours, 2);
}

/** mu_i = B_rem / max(eps, T_exp - t), tokens/minute. Deadline-only grants (quantity_known=0) never invent a token burn rate (0). */
export const trancheUrgencyTpm = (t: Pick<TbmTrancheRow, 'remaining_tokens' | 'expires_at_ms'> & Partial<TbmTrancheRow>, nowMs: number) => {
  if (isDeadlineOnlyTranche(t)) return 0;
  const denomMin = Math.max(EPSILON_MS, t.expires_at_ms - nowMs) / MINUTE;
  return Math.max(0, t.remaining_tokens) / denomMin;
};
/** mu_primary = max(0, Q_rem - R_hard) / max(eps, T_reset - t), units/minute. */
export const primaryUrgencyTpm = (pool: TbmPoolRow, nowMs: number, hardAbs?: number) =>
  Math.max(0, poolEffectiveRemaining(pool) - hardFloorTokens(pool, hardAbs)) / (Math.max(EPSILON_MS, pool.reset_at_ms - nowMs) / MINUTE);

export interface BurnSource { source: 'banked' | 'primary'; tranche: TbmTrancheRow | null; critical: boolean; omega: number }

/**
 * Dispatch rule: burn the EDF head tranche when it expires within dispatchHours or when any
 * tranche is more urgent than the primary pool; otherwise burn primary along the pacing curve.
 * Banked never substitutes for primary reserves: a pool at/below R_hard or with the kill switch
 * on is not a burn source at all (null).
 */
export function selectBurnSource(pool: TbmPoolRow | null, tranches: TbmTrancheRow[], nowMs: number, opts: Partial<typeof EDF_DEFAULTS> & { hardAbsTokens?: number } = {}): BurnSource | null {
  const o = { ...EDF_DEFAULTS, ...opts };
  if (pool && (pool.kill_switch_active || poolEffectiveRemaining(pool) <= hardFloorTokens(pool, o.hardAbsTokens))) return null;
  const ordered = edfOrder(tranches, nowMs);
  const head = ordered[0];
  if (head) {
    const muPrimary = pool ? primaryUrgencyTpm(pool, nowMs, o.hardAbsTokens) : 0;
    const hours = (head.expires_at_ms - nowMs) / HOUR;
    const mostUrgent = ordered.reduce((best, t) => trancheUrgencyTpm(t, nowMs) > trancheUrgencyTpm(best, nowMs) ? t : best, head);
    const deadlineUrgent = ordered.some(t => isDeadlineOnlyTranche(t) && (t.expires_at_ms - nowMs) / HOUR <= o.urgentHours);
    if (hours <= o.dispatchHours || deadlineUrgent || trancheUrgencyTpm(mostUrgent, nowMs) > muPrimary) {
      // Dispatch is always to the EDF head; urgency only decides banked vs primary.
      // Unknown-size grants get the existing bounded maximum throughout their urgent window.
      // Do not invent token quantities/rates: once quantity_known changes, the real remaining
      // amount participates in the normal rate comparison above and deadline multiplier below.
      const omega = deadlineUrgent ? MAX_BANKED_URGENCY : bankedUrgencyMultiplier(head.expires_at_ms, nowMs, o.urgentHours);
      return { source: 'banked', tranche: head, critical: hours <= o.criticalHours, omega };
    }
  }
  return pool ? { source: 'primary', tranche: null, critical: false, omega: 1 } : null;
}

// ---------------------------------------------------------------------------
// Multi-factor routing score (§2.1 item 3, tbm-routing.md §2.4)
// ---------------------------------------------------------------------------

export const FREE_POOL_PROVIDERS = policy.rules.preferred_providers;
export const ROUTING_DEFAULTS = { codexPreference: 0.9, grokDemotion: 0.2, urgencyGamma: 1.5 };

/** S(m) factors; the score is their product. */
export interface RouteScoreFactors { fit: number; health: number; preference: number; headroom: number; urgency: number; pacing: number; grok: number; banked: number }

/** F_fit: exact tier 1.0, deep model on daily task 0.5, anything else (up-tiering) 0. */
export function taskFit(modelTier: string, taskTier: string): number {
  if (modelTier === taskTier) return 1;
  return modelTier === 'deep' && taskTier === 'daily' ? 0.5 : 0;
}
/** P_pref: Free Models Pool 1.3x, Codex 0.9x, others 1.0. Grok is demoted once, by G_grok. */
export function providerPreference(provider: string): number {
  if (policy.rules.preferred_providers.includes(provider)) return policy.rules.provider_preference;
  return provider === 'codex' ? ROUTING_DEFAULTS.codexPreference : 1;
}
/** G_grok: fixed 0.2x Grok-minimal demotion. */
export const grokDemotion = (provider: string) => provider === 'grok' ? ROUTING_DEFAULTS.grokDemotion : 1;
/** N = max(0.05, (Q_rem% - R_floor%) / (100 - R_floor%)). */
export function netHeadroom(remainingPercent: number, floorPercent: number): number {
  const span = 100 - floorPercent;
  return Math.max(policy.rules.headroom_floor, span > 0 ? (remainingPercent - floorPercent) / span : 0);
}
/** U = 1 + 2.5 * N * ((24 - min(24, dt_h)) / 24)^1.5; N <= 0.05 at the floor suppresses panic burn. */
export function resetUrgency(headroom: number, hoursToReset: number): number {
  const h = policy.rules.reset_horizon_hours;
  if (!Number.isFinite(hoursToReset)) return 1;
  const closeness = Math.max(0, (h - Math.min(h, Math.max(0, hoursToReset))) / h);
  return 1 + policy.rules.reset_urgency_factor * headroom * Math.pow(closeness, ROUTING_DEFAULTS.urgencyGamma);
}
/** A_pacing: lagging (< 0.85 target) boosts up to 2.5x; ahead (> 1.15 target) dampens down to 0.2x. */
export function pacingAlignment(actualTpm: number, targetTpm: number): number {
  if (!(targetTpm > 0) || !Number.isFinite(actualTpm)) return 1;
  if (actualTpm < 0.85 * targetTpm) return 1 + Math.min(1.5, (targetTpm - actualTpm) / targetTpm);
  if (actualTpm > 1.15 * targetTpm) return Math.max(0.2, targetTpm / actualTpm);
  return 1;
}
export const routeScore = (f: RouteScoreFactors) => f.fit * f.health * f.preference * f.headroom * f.urgency * f.pacing * f.grok * f.banked;

/** Per-provider / per-account pacing context for routing, derived from tbm_* rows. */
export interface ProviderPacing {
  pacing: number;
  banked: number;
  preempt: boolean;
  halted: boolean;
  efficiency?: number;
}
const NEUTRAL: ProviderPacing = { pacing: 1, banked: 1, preempt: false, halted: false };

function computeGroupPacing(
  groupPools: TbmPoolRow[],
  groupTranches: TbmTrancheRow[],
  nowMs: number,
  globalHalted: boolean,
  enforceFloors: boolean,
  efficiency?: number
): ProviderPacing {
  let floorBlocked = false;
  if (enforceFloors && groupPools.length > 0) {
    const adm = evaluateTbmAdmission({
      pools: groupPools,
      provider: groupPools[0].provider,
      nowMs,
    });
    if (!adm.ok) floorBlocked = true;
  }
  const halted = globalHalted || floorBlocked || groupPools.some(p => Boolean(p.kill_switch_active));
  let banked = 1;
  let preempt = false;
  for (const pool of groupPools.length ? groupPools : [null]) {
    const own = groupTranches.filter(t => !pool || t.pool_id === pool.pool_id || t.pool_id === '*' || t.pool_id === pool.provider);
    const choice = selectBurnSource(pool, own, nowMs);
    if (choice?.source === 'banked') {
      banked = Math.max(banked, choice.omega);
      preempt ||= choice.critical;
    }
  }
  const pacing = groupPools.length
    ? Math.max(...groupPools.map(p => {
        const target = p.target_burn_rate_tpm > 0 ? p.target_burn_rate_tpm : baseTargetTpm(p, nowMs);
        return pacingAlignment(p.current_burn_rate_tpm, target);
      }))
    : 1;
  const out: ProviderPacing = { pacing, banked: halted ? 1 : banked, preempt: preempt && !halted, halted };
  if (typeof efficiency === 'number' && Number.isFinite(efficiency) && efficiency !== 1) {
    out.efficiency = efficiency;
  }
  return out;
}

export function providerPacing(
  pools: TbmPoolRow[],
  tranches: TbmTrancheRow[],
  nowMs: number,
  opts: { enforceFloors?: boolean; efficiencies?: Map<string, number> } = {}
): Map<string, ProviderPacing> {
  const out = new Map<string, ProviderPacing>();
  const globalHalted = pools.some(p => p.pool_id === TBM_FLEET_POOL_ID && Boolean(p.kill_switch_active));
  if (globalHalted) {
    out.set(TBM_FLEET_POOL_ID, { pacing: 1, banked: 1, preempt: false, halted: true });
  }
  const realPools = pools.filter(p => p.pool_id !== TBM_FLEET_POOL_ID && p.provider !== TBM_FLEET_POOL_ID);
  const providers = new Set([...realPools.map(p => p.provider), ...tranches.map(t => t.provider).filter(p => p !== TBM_FLEET_POOL_ID)]);
  for (const provider of providers) {
    const providerPools = realPools.filter(p => p.provider === provider);
    const providerTranches = tranches.filter(t => t.provider === provider);
    const eff = opts.efficiencies?.get(provider);
    // Account-level entries so per-account pacing/banked/floor state steers routing per account.
    const accounts = new Set(providerPools.map(p => p.account).filter((a): a is string => typeof a === 'string' && a.length > 0 && a !== '*'));
    const eligibleAccounts = new Set<string>();
    for (const account of accounts) {
      const acctPools = providerPools.filter(p => p.account === account);
      const acctPoolIds = new Set(acctPools.map(p => p.pool_id));
      const acctTranches = providerTranches.filter(t => acctPoolIds.has(t.pool_id) || t.account === account);
      const acctEff = opts.efficiencies?.get(`${provider}:${account}`) ?? eff;
      const acctPacing = computeGroupPacing(acctPools, acctTranches, nowMs, globalHalted, Boolean(opts.enforceFloors), acctEff);
      out.set(`${provider}:${account}`, acctPacing);
      const fam = normalizeProviderFamily(provider);
      if (fam && fam !== provider && !out.has(`${fam}:${account}`)) {
        out.set(`${fam}:${account}`, acctPacing);
      }
      if (!acctPacing.halted) eligibleAccounts.add(account);
    }
    let aggregatePools = providerPools;
    let aggregateTranches = providerTranches;
    // A floor-blocked account must neither lend urgency to nor suppress critical
    // work on an eligible sibling account. Preserve global/provider kill gates.
    // Only filter when an account was actually excluded: with every account
    // eligible the base aggregate must pass through unchanged, including
    // pool-matched grants whose tranche row names an account outside the pool
    // account set (accepted row shape, no cross-field constraint).
    if (opts.enforceFloors && accounts.size > 1 && eligibleAccounts.size > 0 && eligibleAccounts.size < accounts.size && !globalHalted && !providerPools.some(p => p.kill_switch_active)) {
      aggregatePools = providerPools.filter(p => !p.account || p.account === '*' || eligibleAccounts.has(p.account));
      const poolIds = new Set(aggregatePools.map(p => p.pool_id));
      // Ownership is consistent: an explicit account that maps to a known pool
      // account decides; any other tranche follows its pool_id. Tranches owned
      // by a halted account are excluded; foreign-account rows ride their pool.
      aggregateTranches = providerTranches.filter(t => t.account && t.account !== '*' && accounts.has(t.account)
        ? eligibleAccounts.has(t.account)
        : poolIds.has(t.pool_id) || t.pool_id === '*' || t.pool_id === provider);
    }
    const provPacing = computeGroupPacing(aggregatePools, aggregateTranches, nowMs, globalHalted, Boolean(opts.enforceFloors), eff);
    out.set(provider, provPacing);
    const fam = normalizeProviderFamily(provider);
    if (fam && fam !== provider && !out.has(fam)) {
      out.set(fam, provPacing);
    }
  }
  return out;
}

export function pacingFor(
  map: Map<string, ProviderPacing> | undefined,
  provider: string,
  account?: string | null,
  routeProvider?: string | null
): ProviderPacing {
  if (!map) return NEUTRAL;
  const globalHalted = map.get(TBM_FLEET_POOL_ID)?.halted === true;
  const candidates: string[] = [];
  if (account) {
    if (routeProvider) candidates.push(`${routeProvider}:${account}`);
    candidates.push(`${provider}:${account}`);
  }
  if (routeProvider) candidates.push(routeProvider);
  candidates.push(provider);
  for (const key of candidates) {
    const found = map.get(key);
    if (found) {
      return globalHalted && !found.halted ? { ...found, halted: true, preempt: false } : found;
    }
  }
  return globalHalted ? { ...NEUTRAL, halted: true } : NEUTRAL;
}

// ---------------------------------------------------------------------------
// Phase 1 schema I/O
// ---------------------------------------------------------------------------

const hasTable = (sql: SqlStorage, name: string) =>
  sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", name).toArray().length > 0;

export function loadPools(sql: SqlStorage, includeSentinel = false): TbmPoolRow[] {
  if (!hasTable(sql, 'tbm_quota_pools')) return [];
  const query = includeSentinel
    ? 'SELECT * FROM tbm_quota_pools'
    : "SELECT * FROM tbm_quota_pools WHERE pool_id != '*'";
  return sql.exec(query).toArray() as unknown as TbmPoolRow[];
}

export function loadActiveTranches(sql: SqlStorage, nowMs: number): TbmTrancheRow[] {
  return hasTable(sql, 'tbm_banked_tranches')
    ? sql.exec("SELECT * FROM tbm_banked_tranches WHERE status = 'active' AND expires_at_ms > ? ORDER BY expires_at_ms ASC", nowMs).toArray() as unknown as TbmTrancheRow[]
    : [];
}

/** Compute bounded routing efficiency multipliers from measured native-token reward events. */
export function loadProviderEfficiencies(sql: SqlStorage, nowMs: number): Map<string, number> {
  const out = new Map<string, number>();
  if (!hasTable(sql, 'tbm_reward_events')) return out;
  try {
    const cols = new Set(sql.exec('PRAGMA table_info(tbm_reward_events)').toArray().map((c: any) => String(c.name)));
    const knownFilter = cols.has('tokens_known') ? 'AND tokens_known = 1' : 'AND token_efficiency_eta > 0';
    const sinceMs = nowMs - 24 * HOUR;
    const rows = sql.exec(
      `SELECT pool_id, SUM(token_efficiency_eta * tokens_burned) / SUM(tokens_burned) AS avg_eta, COUNT(*) AS n
       FROM tbm_reward_events
       WHERE timestamp_ms >= ? ${knownFilter} AND tokens_burned > 0 AND event_kind = 'session'
       GROUP BY pool_id`,
      sinceMs
    ).toArray() as any[];
    const byProvider = new Map<string, { sum: number; n: number }>();
    for (const r of rows) {
      const eta = Number(r.avg_eta);
      const count = Number(r.n) || 1;
      if (!Number.isFinite(eta) || eta < 0) continue;
      // Smooth efficiency factor in [0.75, 1.25] centered at 1.0 for 50 VU/MTok nominal benchmark.
      const mult = Math.max(0.75, Math.min(1.25, 0.75 + 0.5 * (eta / (eta + 50))));
      const parts = String(r.pool_id || '').split(':');
      const prov = parts[0];
      const acct = parts[1];
      if (prov && acct) out.set(`${prov}:${acct}`, mult);
      if (prov) {
        const cur = byProvider.get(prov) ?? { sum: 0, n: 0 };
        cur.sum += eta * count;
        cur.n += count;
        byProvider.set(prov, cur);
      }
    }
    for (const [prov, agg] of byProvider.entries()) {
      const avgEta = agg.sum / Math.max(1, agg.n);
      const mult = Math.max(0.75, Math.min(1.25, 0.75 + 0.5 * (avgEta / (avgEta + 50))));
      out.set(prov, mult);
    }
  } catch {
    /* ignore if schema not yet migrated */
  }
  return out;
}

/** Routing context from stored rows; empty tables keep routing neutral.
 * Operator floor overrides (rules.tbm_floor_percent) are applied here — the same
 * override evaluateTbmAdmission applies — so burn selection and pacing factors
 * see the floors admission sees. Stored rows keep the pre-override defaults
 * (5/20) for pools whose provider family has no override. */
export const loadProviderPacing = (sql: SqlStorage, nowMs: number) =>
  providerPacing(loadPools(sql, true).map(p => withPolicyTbmFloors(p)), loadActiveTranches(sql, nowMs), nowMs, {
    enforceFloors: true,
    efficiencies: loadProviderEfficiencies(sql, nowMs),
  });

/**
 * One controller tick: settle expired/exhausted tranches, write their EDF targets, and write each
 * pool's target_burn_rate_tpm / pacing_ratio. PID memory lives in `states` (keyed by pool_id).
 */
export function refreshPacing(sql: SqlStorage, nowMs: number, states: Map<string, PidState>, opts: PacingOptions = {}): PacingResult[] {
  if (!hasTable(sql, 'tbm_quota_pools') || !hasTable(sql, 'tbm_banked_tranches')) return [];
  const trancheCols = new Set(sql.exec('PRAGMA table_info(tbm_banked_tranches)').toArray().map((c: any) => String(c.name)));
  const exhaustCond = trancheCols.has('quantity_known')
    ? "status = 'active' AND remaining_tokens <= 0 AND COALESCE(quantity_known, 1) != 0"
    : "status = 'active' AND remaining_tokens <= 0";
  sql.exec("UPDATE tbm_banked_tranches SET status = 'expired', expired_unused_tokens = expired_unused_tokens + remaining_tokens, remaining_tokens = 0, target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE status = 'active' AND expires_at_ms <= ?", nowMs, nowMs);
  sql.exec(`UPDATE tbm_banked_tranches SET status = 'exhausted', target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE ${exhaustCond}`, nowMs);
  for (const t of loadActiveTranches(sql, nowMs)) {
    sql.exec('UPDATE tbm_banked_tranches SET target_banked_burn_rate_tpm = ?, updated_at_ms = ? WHERE tranche_id = ?', trancheUrgencyTpm(t, nowMs), nowMs, t.tranche_id);
  }
  return loadPools(sql, false).map(p => withPolicyTbmFloors(p)).map(pool => {
    const result = pacingStep(pool, states.get(pool.pool_id) ?? newPidState(), nowMs, opts);
    states.set(pool.pool_id, result.state);
    sql.exec('UPDATE tbm_quota_pools SET target_burn_rate_tpm = ?, pacing_ratio = ? WHERE pool_id = ?', result.targetTpm, result.pacingRatio, pool.pool_id);
    return result;
  });
}
