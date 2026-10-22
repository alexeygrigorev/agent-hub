/**
 * Token Burning Machine Phase 3: pacing controller, banked-tranche EDF arbiter and
 * multi-factor routing score (tbm-coord.md §2.1 items 1-4, §8 item 3).
 *
 * Everything here is pure arithmetic over the Phase 1 tbm_* rows; the only SQL is
 * reading those tables and writing their existing pacing columns. No new columns.
 */
import type { SqlStorage } from '@cloudflare/workers-types';

const MINUTE = 60000;
const HOUR = 3600000;

/** Exactly the Phase 1 tbm_quota_pools columns this module reads or writes. */
export interface TbmPoolRow {
  pool_id: string; provider: string; account: string;
  total_capacity_tokens: number; remaining_tokens: number; headroom_percentage: number;
  reset_at_ms: number; hard_floor_percent: number; soft_reserve_percent: number; convexity_exponent: number;
  current_burn_rate_tpm: number; target_burn_rate_tpm: number; pacing_ratio: number;
  kill_switch_active: number;
}
/** Exactly the Phase 1 tbm_banked_tranches columns this module reads or writes. */
export interface TbmTrancheRow {
  tranche_id: string; pool_id: string; provider: string;
  remaining_tokens: number; expires_at_ms: number; status: 'active' | 'exhausted' | 'expired';
  banked_burn_rate_tpm: number; target_banked_burn_rate_tpm: number; expired_unused_tokens: number;
}

// ---------------------------------------------------------------------------
// Safety floors (§2.1 item 2)
// ---------------------------------------------------------------------------

export const FLOOR_DEFAULTS = { hardAbsTokens: 50_000, softAbsTokens: 200_000, windowMs: 7 * 24 * HOUR };

/** R_hard = max(total * hard%, R_hard_abs), never above the pool itself. */
export function hardFloorTokens(pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'hard_floor_percent'>, hardAbs = FLOOR_DEFAULTS.hardAbsTokens): number {
  return Math.min(pool.total_capacity_tokens, Math.max(pool.total_capacity_tokens * pool.hard_floor_percent / 100, hardAbs));
}

/** R_soft(t) = R_hard + (R_soft_init - R_hard) * ((T_reset - t) / T_window)^kappa, decaying to R_hard at reset. */
export function softReserveTokens(pool: Pick<TbmPoolRow, 'total_capacity_tokens' | 'hard_floor_percent' | 'soft_reserve_percent' | 'convexity_exponent' | 'reset_at_ms'>,
  nowMs: number, opts: Partial<typeof FLOOR_DEFAULTS> = {}): number {
  const o = { ...FLOOR_DEFAULTS, ...opts };
  const hard = hardFloorTokens(pool, o.hardAbsTokens);
  const init = Math.min(pool.total_capacity_tokens, Math.max(pool.total_capacity_tokens * pool.soft_reserve_percent / 100, o.softAbsTokens, hard));
  const fraction = Math.max(0, Math.min(1, (pool.reset_at_ms - nowMs) / o.windowMs));
  return hard + (init - hard) * Math.pow(fraction, Math.max(1, pool.convexity_exponent || 1.5));
}

// ---------------------------------------------------------------------------
// Continuous target burn with PID smoothing (§2.1 item 1, tbm-pacing.md §3.1, §3.3)
// ---------------------------------------------------------------------------

/** Rates are tokens/minute; Kp acts on the cumulative token deficit e(t) = S_planned - S_actual. */
export interface PidGains { kp: number; ki: number; kd: number }
export const PID_DEFAULTS: PidGains = { kp: 1 / 60, ki: 1 / (60 * 240), kd: 0.1 };
/** Controller memory. Not persisted: tbm_quota_pools has no PID columns, so callers keep it per pool. */
export interface PidState { deficitTokens: number; integral: number; lastRateError: number; lastAtMs: number | null }
export const newPidState = (): PidState => ({ deficitTokens: 0, integral: 0, lastRateError: 0, lastAtMs: null });

const EPSILON_MS = 10000;

/** Base term of the AC formula: max(0, Q_rem - R_soft) / max(eps, T_reset - t), in tokens/minute. */
export function baseTargetTpm(pool: TbmPoolRow, nowMs: number, opts: Partial<typeof FLOOR_DEFAULTS> = {}): number {
  const burnable = Math.max(0, pool.remaining_tokens - softReserveTokens(pool, nowMs, opts));
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
  const minutesLeft = Math.max(EPSILON_MS, pool.reset_at_ms - nowMs) / MINUTE;
  return {
    baseTpm: base, smoothingTpm: target - base, targetTpm: target, saturated, held,
    pacingRatio: target > 0 ? pool.current_burn_rate_tpm / target : 1,
    softReserveTokens: softReserveTokens(pool, nowMs, opts), hardFloorTokens: hard,
    // Anti-cliff: surplus above R_hard needs > 80% of max feasible burn to clear before reset.
    antiCliff: Number.isFinite(maxBurn) && (pool.remaining_tokens - hard) / minutesLeft > 0.8 * maxBurn,
    state: { deficitTokens: deficit, integral, lastRateError: rateError, lastAtMs: nowMs },
  };
}

// ---------------------------------------------------------------------------
// Banked tranches: Earliest-Deadline-First arbiter (§2.1 item 4, tbm-pacing.md §4.2)
// ---------------------------------------------------------------------------

export const EDF_DEFAULTS = { urgentHours: 24, criticalHours: 4, dispatchHours: 6 };

/** Active, non-empty, unexpired tranches by ascending expiry; ties by remaining desc, then id. */
export function edfOrder<T extends Pick<TbmTrancheRow, 'tranche_id' | 'remaining_tokens' | 'expires_at_ms' | 'status'>>(tranches: T[], nowMs: number): T[] {
  return tranches.filter(t => t.status === 'active' && t.remaining_tokens > 0 && t.expires_at_ms > nowMs)
    .sort((a, b) => a.expires_at_ms - b.expires_at_ms || b.remaining_tokens - a.remaining_tokens || a.tranche_id.localeCompare(b.tranche_id));
}

/** Omega = 1 + 3 * ((24 - dtau_h) / 24)^2 inside the 24h urgent window, else 1. */
export function bankedUrgencyMultiplier(expiresAtMs: number, nowMs: number, urgentHours = EDF_DEFAULTS.urgentHours): number {
  const hours = (expiresAtMs - nowMs) / HOUR;
  if (hours <= 0 || hours > urgentHours) return 1;
  return 1 + 3 * Math.pow((urgentHours - hours) / urgentHours, 2);
}

/** mu_i = B_rem / max(eps, T_exp - t), tokens/minute. */
export const trancheUrgencyTpm = (t: Pick<TbmTrancheRow, 'remaining_tokens' | 'expires_at_ms'>, nowMs: number) =>
  Math.max(0, t.remaining_tokens) / (Math.max(EPSILON_MS, t.expires_at_ms - nowMs) / MINUTE);
/** mu_primary = max(0, Q_rem - R_hard) / max(eps, T_reset - t), tokens/minute. */
export const primaryUrgencyTpm = (pool: TbmPoolRow, nowMs: number, hardAbs?: number) =>
  Math.max(0, pool.remaining_tokens - hardFloorTokens(pool, hardAbs)) / (Math.max(EPSILON_MS, pool.reset_at_ms - nowMs) / MINUTE);

export interface BurnSource { source: 'banked' | 'primary'; tranche: TbmTrancheRow | null; critical: boolean; omega: number }

/**
 * Dispatch rule: burn the EDF head tranche when it expires within dispatchHours or when any
 * tranche is more urgent than the primary pool; otherwise burn primary along the pacing curve.
 * Banked never substitutes for primary reserves: a pool at/below R_hard or with the kill switch
 * on is not a burn source at all (null).
 */
export function selectBurnSource(pool: TbmPoolRow | null, tranches: TbmTrancheRow[], nowMs: number, opts: Partial<typeof EDF_DEFAULTS> & { hardAbsTokens?: number } = {}): BurnSource | null {
  const o = { ...EDF_DEFAULTS, ...opts };
  if (pool && (pool.kill_switch_active || pool.remaining_tokens <= hardFloorTokens(pool, o.hardAbsTokens))) return null;
  const ordered = edfOrder(tranches, nowMs);
  const head = ordered[0];
  if (head) {
    const muPrimary = pool ? primaryUrgencyTpm(pool, nowMs, o.hardAbsTokens) : 0;
    const hours = (head.expires_at_ms - nowMs) / HOUR;
    const mostUrgent = ordered.reduce((best, t) => trancheUrgencyTpm(t, nowMs) > trancheUrgencyTpm(best, nowMs) ? t : best, head);
    if (hours <= o.dispatchHours || trancheUrgencyTpm(mostUrgent, nowMs) > muPrimary) {
      // Dispatch is always to the EDF head; urgency only decides banked vs primary.
      return { source: 'banked', tranche: head, critical: hours <= o.criticalHours, omega: bankedUrgencyMultiplier(head.expires_at_ms, nowMs, o.urgentHours) };
    }
  }
  return pool ? { source: 'primary', tranche: null, critical: false, omega: 1 } : null;
}

// ---------------------------------------------------------------------------
// Multi-factor routing score (§2.1 item 3, tbm-routing.md §2.4)
// ---------------------------------------------------------------------------

export const FREE_POOL_PROVIDERS = ['zai', 'go'];
export const ROUTING_DEFAULTS = { freePoolBoost: 1.3, codexPreference: 0.9, grokDemotion: 0.2, headroomFloor: 0.05,
  urgencyHorizonHours: 24, urgencyKappa: 2.5, urgencyGamma: 1.5 };

/** S(m) factors; the score is their product. */
export interface RouteScoreFactors { fit: number; health: number; preference: number; headroom: number; urgency: number; pacing: number; grok: number; banked: number }

/** F_fit: exact tier 1.0, deep model on daily task 0.5, anything else (up-tiering) 0. */
export function taskFit(modelTier: string, taskTier: string): number {
  if (modelTier === taskTier) return 1;
  return modelTier === 'deep' && taskTier === 'daily' ? 0.5 : 0;
}
/** P_pref: Free Models Pool 1.3x, Codex 0.9x, others 1.0. Grok is demoted once, by G_grok. */
export function providerPreference(provider: string): number {
  if (FREE_POOL_PROVIDERS.includes(provider)) return ROUTING_DEFAULTS.freePoolBoost;
  return provider === 'codex' ? ROUTING_DEFAULTS.codexPreference : 1;
}
/** G_grok: fixed 0.2x Grok-minimal demotion. */
export const grokDemotion = (provider: string) => provider === 'grok' ? ROUTING_DEFAULTS.grokDemotion : 1;
/** N = max(0.05, (Q_rem% - R_floor%) / (100 - R_floor%)). */
export function netHeadroom(remainingPercent: number, floorPercent: number): number {
  const span = 100 - floorPercent;
  return Math.max(ROUTING_DEFAULTS.headroomFloor, span > 0 ? (remainingPercent - floorPercent) / span : 0);
}
/** U = 1 + 2.5 * N * ((24 - min(24, dt_h)) / 24)^1.5; N <= 0.05 at the floor suppresses panic burn. */
export function resetUrgency(headroom: number, hoursToReset: number): number {
  const h = ROUTING_DEFAULTS.urgencyHorizonHours;
  if (!Number.isFinite(hoursToReset)) return 1;
  const closeness = Math.max(0, (h - Math.min(h, Math.max(0, hoursToReset))) / h);
  return 1 + ROUTING_DEFAULTS.urgencyKappa * headroom * Math.pow(closeness, ROUTING_DEFAULTS.urgencyGamma);
}
/** A_pacing: lagging (< 0.85 target) boosts up to 2.5x; ahead (> 1.15 target) dampens down to 0.2x. */
export function pacingAlignment(actualTpm: number, targetTpm: number): number {
  if (!(targetTpm > 0) || !Number.isFinite(actualTpm)) return 1;
  if (actualTpm < 0.85 * targetTpm) return 1 + Math.min(1.5, (targetTpm - actualTpm) / targetTpm);
  if (actualTpm > 1.15 * targetTpm) return Math.max(0.2, targetTpm / actualTpm);
  return 1;
}
export const routeScore = (f: RouteScoreFactors) => f.fit * f.health * f.preference * f.headroom * f.urgency * f.pacing * f.grok * f.banked;

/** Per-provider pacing context for routing, derived from tbm_* rows. */
export interface ProviderPacing { pacing: number; banked: number; preempt: boolean; halted: boolean }
const NEUTRAL: ProviderPacing = { pacing: 1, banked: 1, preempt: false, halted: false };

export function providerPacing(pools: TbmPoolRow[], tranches: TbmTrancheRow[], nowMs: number): Map<string, ProviderPacing> {
  const out = new Map<string, ProviderPacing>();
  for (const provider of new Set([...pools.map(p => p.provider), ...tranches.map(t => t.provider)])) {
    const providerPools = pools.filter(p => p.provider === provider);
    const halted = providerPools.some(p => p.kill_switch_active);
    // Tranches debit their own pool, so pair each tranche with that pool's floors.
    let banked = 1; let preempt = false;
    for (const pool of providerPools.length ? providerPools : [null]) {
      const own = tranches.filter(t => t.provider === provider && (!pool || t.pool_id === pool.pool_id));
      const choice = selectBurnSource(pool, own, nowMs);
      if (choice?.source === 'banked') { banked = Math.max(banked, choice.omega); preempt ||= choice.critical; }
    }
    // The most lagging account decides: routing should feed whichever pool is behind its curve.
    const pacing = providerPools.length ? Math.max(...providerPools.map(p => pacingAlignment(p.current_burn_rate_tpm, p.target_burn_rate_tpm))) : 1;
    out.set(provider, { pacing, banked, preempt: preempt && !halted, halted });
  }
  return out;
}
export const pacingFor = (map: Map<string, ProviderPacing> | undefined, provider: string) => map?.get(provider) ?? NEUTRAL;

// ---------------------------------------------------------------------------
// Phase 1 schema I/O
// ---------------------------------------------------------------------------

const hasTable = (sql: SqlStorage, name: string) =>
  sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", name).toArray().length > 0;

export function loadPools(sql: SqlStorage): TbmPoolRow[] {
  return hasTable(sql, 'tbm_quota_pools') ? sql.exec('SELECT * FROM tbm_quota_pools').toArray() as unknown as TbmPoolRow[] : [];
}
export function loadActiveTranches(sql: SqlStorage, nowMs: number): TbmTrancheRow[] {
  return hasTable(sql, 'tbm_banked_tranches') ? sql.exec("SELECT * FROM tbm_banked_tranches WHERE status = 'active' AND expires_at_ms > ? ORDER BY expires_at_ms ASC", nowMs).toArray() as unknown as TbmTrancheRow[] : [];
}
/** Routing context from stored rows; empty tables keep routing neutral. */
export const loadProviderPacing = (sql: SqlStorage, nowMs: number) => providerPacing(loadPools(sql), loadActiveTranches(sql, nowMs), nowMs);

/**
 * One controller tick: settle expired/exhausted tranches, write their EDF targets, and write each
 * pool's target_burn_rate_tpm / pacing_ratio. PID memory lives in `states` (keyed by pool_id).
 */
export function refreshPacing(sql: SqlStorage, nowMs: number, states: Map<string, PidState>, opts: PacingOptions = {}): PacingResult[] {
  if (!hasTable(sql, 'tbm_quota_pools') || !hasTable(sql, 'tbm_banked_tranches')) return [];
  sql.exec("UPDATE tbm_banked_tranches SET status = 'expired', expired_unused_tokens = expired_unused_tokens + remaining_tokens, remaining_tokens = 0, target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE status = 'active' AND expires_at_ms <= ?", nowMs, nowMs);
  sql.exec("UPDATE tbm_banked_tranches SET status = 'exhausted', target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE status = 'active' AND remaining_tokens <= 0", nowMs);
  for (const t of loadActiveTranches(sql, nowMs)) {
    sql.exec('UPDATE tbm_banked_tranches SET target_banked_burn_rate_tpm = ?, updated_at_ms = ? WHERE tranche_id = ?', trancheUrgencyTpm(t, nowMs), nowMs, t.tranche_id);
  }
  return loadPools(sql).map(pool => {
    const result = pacingStep(pool, states.get(pool.pool_id) ?? newPidState(), nowMs, opts);
    states.set(pool.pool_id, result.state);
    sql.exec('UPDATE tbm_quota_pools SET target_burn_rate_tpm = ?, pacing_ratio = ? WHERE pool_id = ?', result.targetTpm, result.pacingRatio, pool.pool_id);
    return result;
  });
}
