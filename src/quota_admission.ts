/**
 * Single canonical quota admission evaluator (M13-IG3).
 *
 * Used by FleetService.accountStatus/plan/claim and RoutingService.dispatch.
 * Reserve thresholds come from the caller's policy config
 * (config/model-routing.json rules.reserve_percent) — never a hardcoded
 * second list here. modelCandidates() in src/model-routing.ts is owned
 * elsewhere; dispatch filters its shortlist through this evaluator.
 */
export const QUOTA_OBSERVATION_TTL_MS = 1_200_000; // 20 minutes, matches QUOTA_STALE_MS

export interface QuotaAdmissionInput {
  provider: string;
  quota: any;
  /** fleet_subscriptions.observed_at, or host snapshot captured_at for snapshot-backed reads. Null skips the subscription leg. */
  subscriptionObservedAt: string | null;
  nowMs: number;
  /** e.g. policy.rules.reserve_percent: { codex: 15, grok: 5 } */
  reserves: Record<string, number>;
}

export interface QuotaAdmissionResult {
  ok: boolean;
  reason: string;
  /** Minimum percent across valid available windows; null when inadmissible. */
  remaining: number | null;
}

function validObservedAt(value: unknown, nowMs: number): boolean {
  if (typeof value !== "string") return false;
  const at = Date.parse(value);
  return Number.isFinite(at) && at <= nowMs && nowMs - at <= QUOTA_OBSERVATION_TTL_MS;
}

function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function evaluateQuotaAdmission(input: QuotaAdmissionInput): QuotaAdmissionResult {
  const fail = (reason: string): QuotaAdmissionResult => ({ ok: false, reason, remaining: null });
  const { provider, quota, subscriptionObservedAt, nowMs, reserves } = input;
  if (typeof provider !== "string" || !provider) return fail("malformed");
  if (!isRecord(quota)) return fail("malformed");
  if (typeof nowMs !== "number" || !Number.isFinite(nowMs)) return fail("malformed");

  if (subscriptionObservedAt !== null && !validObservedAt(subscriptionObservedAt, nowMs)) {
    const at = typeof subscriptionObservedAt === "string" ? Date.parse(subscriptionObservedAt) : NaN;
    if (!Number.isFinite(at)) return fail("invalid_observed_at");
    if (at > nowMs) return fail("future_observation");
    return fail("stale_observation");
  }
  if (!validObservedAt(quota.observed_at, nowMs)) {
    const at = typeof quota.observed_at === "string" ? Date.parse(quota.observed_at) : NaN;
    if (!Number.isFinite(at)) return fail("invalid_observed_at");
    if (at > nowMs) return fail("future_observation");
    return fail("stale_observation");
  }
  const obsMs = Date.parse(quota.observed_at as string);

  if (quota.status !== "ok") return fail("route_not_ok");
  if (quota.error !== null && quota.error !== undefined) return fail("route_not_ok");
  if (!isRecord(quota.details)) return fail("malformed");
  if (quota.details.limit_reached !== false) {
    return fail(quota.details.limit_reached === true ? "limit_reached" : "malformed");
  }
  if (quota.details.stale === true) return fail("route_stale");
  // A known active provider backoff hold blocks admission even when quota looks fine.
  // Expiry alone never promotes stale/unknown quota: freshness was already enforced above.
  const cooldownUntil = (quota.details as Record<string, any>).cooldown_until;
  if (cooldownUntil !== null && cooldownUntil !== undefined) {
    if (typeof cooldownUntil !== "string" || !Number.isFinite(Date.parse(cooldownUntil))) return fail("malformed");
    if (Date.parse(cooldownUntil) > nowMs) return fail("cooldown_active");
  }
  if (provider === "grok" && quota.details.has_grok_code_access !== true) return fail("grok_entitlement");
  if (!isRecord(quota.windows)) return fail("malformed");

  const reserve = typeof reserves[provider] === "number" && Number.isFinite(reserves[provider]) ? reserves[provider] : 0;
  let remaining: number | null = null;
  let rawMin: number | null = null;
  let available = 0;

  for (const w of Object.values(quota.windows)) {
    if (!isRecord(w) || typeof w.present !== "boolean") return fail("malformed");
    if (w.present === false) {
      // Benign absence: unsupported window with no data and no error.
      // A genuine unavailable/error sibling fails closed even when other windows look healthy.
      if (w.error === "unavailable") return fail("window_unavailable");
      if (w.percent_remaining !== null && w.percent_remaining !== undefined) return fail("malformed");
      if (w.reset_at !== null && w.reset_at !== undefined) return fail("malformed");
      continue;
    }
    if (w.error !== null && w.error !== undefined) return fail("window_unavailable");
    const pct = w.percent_remaining;
    if (typeof pct === "boolean" || typeof pct !== "number" || !Number.isFinite(pct) || pct < 0 || pct > 100) {
      return fail("malformed");
    }
    if (typeof w.reset_at !== "string") return fail("malformed");
    const resetMs = Date.parse(w.reset_at);
    if (!Number.isFinite(resetMs)) return fail("malformed");
    // A passed reset is not recovery: the observation must be post-reset.
    if (resetMs <= nowMs && obsMs < resetMs) return fail("reset_passed_since_observation");
    rawMin = rawMin === null ? pct : Math.min(rawMin, pct);
    if (pct <= reserve) return { ok: false, reason: "reserve", remaining: rawMin };
    available++;
    remaining = remaining === null ? pct : Math.min(remaining, pct);
  }

  if (!available || remaining === null) return fail("no_available_window");
  return { ok: true, reason: "eligible", remaining };
}

/* ------------------------------------------------------------------ */
/* TBM Phase 2: floors + kill-switch for autonomous (non-interactive)   */
/* admission. Pure functions over tbm_quota_pools rows; fail closed.    */
/* ------------------------------------------------------------------ */

/** Fleet-wide kill-switch is stored as a tbm_quota_pools row with this pool_id. */
export const TBM_FLEET_POOL_ID = "*";
export const TBM_HARD_FLOOR_FRACTION = 0.05;
export const TBM_HARD_FLOOR_MIN_TOKENS = 50_000;
export const TBM_SOFT_INIT_FRACTION = 0.2;
export const TBM_SOFT_INIT_MIN_TOKENS = 200_000;
export const TBM_DEFAULT_KAPPA = 1.5;

/** Engine -> quota provider (matches config/model-routing.json). */
const ENGINE_PROVIDER: Record<string, string> = {
  claude: "claude", codex: "codex", grok: "grok", gemini: "gemini", antigravity: "gemini",
  opencode: "go", zcodex: "zai",
};
export function engineProvider(engine: string): string | null {
  return ENGINE_PROVIDER[engine] ?? null;
}

export interface TbmPoolRow {
  pool_id: string;
  provider: string;
  total_capacity_tokens: number;
  remaining_tokens: number;
  reset_at_ms: number;
  convexity_exponent?: number | null;
  kill_switch_active: number;
  kill_switch_reason?: string | null;
  last_updated_ms?: number | null;
}

/** R_hard = max(5% of Q_total, 50k tokens). */
export function tbmHardFloor(totalTokens: number): number {
  return Math.max(totalTokens * TBM_HARD_FLOOR_FRACTION, TBM_HARD_FLOOR_MIN_TOKENS);
}

/**
 * R_soft(t) = R_hard + (R_soft_init - R_hard) * ((T_reset - t) / T_cycle)^kappa.
 * The cycle length is not stored in tbm_quota_pools, so `cycleMs` is supplied by
 * the caller; when the window is unknown the reserve stays at R_soft_init
 * (conservative). Clamped to [R_hard, R_soft_init].
 */
export function tbmSoftReserve(totalTokens: number, nowMs: number, resetAtMs: number, cycleMs: number | null, kappa = TBM_DEFAULT_KAPPA): number {
  const hard = tbmHardFloor(totalTokens);
  const init = Math.max(hard, Math.max(totalTokens * TBM_SOFT_INIT_FRACTION, TBM_SOFT_INIT_MIN_TOKENS));
  if (cycleMs === null || !(cycleMs > 0) || !Number.isFinite(resetAtMs)) return init;
  const k = Number.isFinite(kappa) && kappa > 0 ? kappa : TBM_DEFAULT_KAPPA;
  const frac = Math.min(1, Math.max(0, (resetAtMs - nowMs) / cycleMs));
  return hard + (init - hard) * Math.pow(frac, k);
}

export type TbmAdmissionReason =
  | "ok" | "kill_switch_active" | "hard_floor" | "soft_reserve" | "malformed_pool";

export interface TbmAdmissionResult {
  ok: boolean;
  reason: TbmAdmissionReason;
  pool_id: string | null;
  detail?: string;
  r_hard?: number;
  r_soft?: number;
  remaining?: number;
}

export interface TbmAdmissionInput {
  /** Rows from tbm_quota_pools (all pools; the evaluator picks the relevant ones). */
  pools: TbmPoolRow[];
  /** Provider the session will burn, when known (explicit engine); null = unknown. */
  provider: string | null;
  nowMs: number;
  /** Assumed provider cycle length for R_soft decay; null keeps R_soft at its initial level. */
  cycleMs?: number | null;
}

function finiteNonNeg(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0;
}

/**
 * Autonomous admission: kill-switch (fleet pool "*" or the provider's pools),
 * then hard floor, then dynamic soft reserve. Floors are checked only for
 * pools of the target provider; with no known provider only kill-switches apply
 * (the router enforces floors per route in a later phase). Malformed rows fail closed.
 */
export function evaluateTbmAdmission(input: TbmAdmissionInput): TbmAdmissionResult {
  const { pools, provider, nowMs } = input;
  const cycleMs = input.cycleMs ?? null;
  for (const p of pools) {
    if (p.kill_switch_active !== 0 && p.kill_switch_active !== 1) {
      return { ok: false, reason: "malformed_pool", pool_id: p.pool_id, detail: "kill_switch_active" };
    }
    const scoped = p.pool_id === TBM_FLEET_POOL_ID || (provider !== null && p.provider === provider);
    if (scoped && p.kill_switch_active === 1) {
      return { ok: false, reason: "kill_switch_active", pool_id: p.pool_id, detail: p.kill_switch_reason ?? undefined };
    }
  }
  if (provider === null) return { ok: true, reason: "ok", pool_id: null };
  for (const p of pools) {
    if (p.pool_id === TBM_FLEET_POOL_ID || p.provider !== provider) continue;
    if (!finiteNonNeg(p.total_capacity_tokens) || !finiteNonNeg(p.remaining_tokens) || p.total_capacity_tokens <= 0) {
      return { ok: false, reason: "malformed_pool", pool_id: p.pool_id, detail: "capacity" };
    }
    const hard = tbmHardFloor(p.total_capacity_tokens);
    const soft = tbmSoftReserve(p.total_capacity_tokens, nowMs, p.reset_at_ms, cycleMs, p.convexity_exponent ?? TBM_DEFAULT_KAPPA);
    const base = { pool_id: p.pool_id, r_hard: hard, r_soft: soft, remaining: p.remaining_tokens };
    if (p.remaining_tokens <= hard) return { ok: false, reason: "hard_floor", ...base };
    if (p.remaining_tokens <= soft) return { ok: false, reason: "soft_reserve", ...base };
  }
  return { ok: true, reason: "ok", pool_id: null };
}
