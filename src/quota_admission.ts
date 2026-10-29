/**
 * Single canonical quota admission evaluator (M13-IG3).
 *
 * Used by FleetService.accountStatus/plan/claim and RoutingService.dispatch.
 * Reserve thresholds come from the caller's policy config
 * (config/model-routing.json rules.reserve_percent) — never a hardcoded
 * second list here. modelCandidates() in src/model-routing.ts is owned
 * elsewhere; dispatch filters its shortlist through this evaluator.
 */
import policy from "../config/model-routing.json";

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
  let timedWindows = 0;

  for (const [name, w] of Object.entries(quota.windows)) {
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
    // Claude and Z.ai's unused five-hour windows are explicitly 0% utilized with
    // no active reset. Preserve that fact without inventing a reset timestamp.
    // A separate active, timed window is still required (normally weekly).
    const inactiveFiveHour = ['claude', 'zai'].includes(provider) && ['5h', 'five_hour'].includes(name) && pct === 100 && w.reset_at === null;
    if (!inactiveFiveHour) {
      if (typeof w.reset_at !== "string") return fail("malformed");
      const resetMs = Date.parse(w.reset_at);
      if (!Number.isFinite(resetMs)) return fail("malformed");
      // A passed reset is not recovery: the observation must be post-reset.
      if (resetMs <= nowMs && obsMs < resetMs) return fail("reset_passed_since_observation");
      timedWindows++;
    }
    rawMin = rawMin === null ? pct : Math.min(rawMin, pct);
    if (pct <= reserve) return { ok: false, reason: "reserve", remaining: rawMin };
    available++;
    remaining = remaining === null ? pct : Math.min(remaining, pct);
  }

  if (!available || !timedWindows || remaining === null) return fail("no_available_window");
  return { ok: true, reason: "eligible", remaining };
}

/** Placement tolerates probe failure, while positive exhaustion/backoff still holds.
 * Unknown stays null: callers must use a ranking floor, never invent a balance.
 */
export function evaluateLaunchQuota(input: QuotaAdmissionInput): QuotaAdmissionResult {
  const strict = evaluateQuotaAdmission(input);
  if (strict.ok) return strict;
  const q = input.quota;
  if (q?.details?.limit_reached === true) return { ok: false, reason: 'limit_reached', remaining: null };
  const until = Date.parse(q?.details?.cooldown_until);
  if (Number.isFinite(until) && until > input.nowMs) return { ok: false, reason: 'cooldown_active', remaining: null };
  if (input.provider === 'grok') return strict; // entitlement is positive evidence
  const reserve = input.reserves[input.provider] ?? 0;
  let remaining: number | null = null;
  for (const w of Object.values(q?.windows || {}) as any[]) {
    if (w?.present === false || typeof w?.percent_remaining !== 'number' || !Number.isFinite(w.percent_remaining)
        || w.percent_remaining < 0 || w.percent_remaining > 100) continue;
    remaining = remaining === null ? w.percent_remaining : Math.min(remaining, w.percent_remaining);
  }
  if (remaining !== null && remaining <= reserve) return { ok: false, reason: 'reserve', remaining };
  return { ok: true, reason: 'quota_probe_warning:' + strict.reason, remaining };
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

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const WINDOW_NOMINAL_CYCLE_MS: Record<string, number> = {
  "1h": HOUR_MS,
  hourly: HOUR_MS,
  "5h": 5 * HOUR_MS,
  five_hour: 5 * HOUR_MS,
  "5hour": 5 * HOUR_MS,
  daily: DAY_MS,
  day: DAY_MS,
  "24h": DAY_MS,
  weekly: 7 * DAY_MS,
  week: 7 * DAY_MS,
  "7d": 7 * DAY_MS,
  monthly: 30 * DAY_MS,
  month: 30 * DAY_MS,
  "30d": 30 * DAY_MS,
};

/** Engine -> quota provider (matches config/model-routing.json). */
const ENGINE_PROVIDER: Record<string, string> = {
  claude: "claude", codex: "codex", grok: "grok", gemini: "gemini", antigravity: "gemini",
  opencode: "go", zcodex: "zai",
};
export function engineProvider(engine: string): string | null {
  return ENGINE_PROVIDER[engine] ?? null;
}

export function normalizeProviderFamily(provider: string | null | undefined): string | null {
  if (!provider) return null;
  return provider === "codex-orin" ? "codex" : provider;
}

export interface TbmPoolRow {
  pool_id: string;
  provider: string;
  account?: string | null;
  total_capacity_tokens: number;
  remaining_tokens: number;
  headroom_percentage?: number | null;
  remaining_percent?: number | null;
  capacity_known?: number | boolean | null;
  quota_unit?: string | null;
  reset_at_ms: number;
  cycle_ms?: number | null;
  window_type?: string | null;
  hard_floor_percent?: number | null;
  soft_reserve_percent?: number | null;
  convexity_exponent?: number | null;
  kill_switch_active: number;
  kill_switch_reason?: string | null;
  last_updated_ms?: number | null;
}

function finiteNonNeg(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0;
}

function finitePercent(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 100;
}

/** True when the pool represents a percentage-only window with unknown token capacity. */
export function isPercentOnlyTbmPool(p: Partial<TbmPoolRow>): boolean {
  if (p.capacity_known === 0 || p.capacity_known === false || p.quota_unit === "percent") return true;
  if (p.total_capacity_tokens === 0 && p.remaining_tokens === 0 && (finitePercent(p.remaining_percent) || finitePercent(p.headroom_percentage))) {
    return true;
  }
  return false;
}

export function resolveTbmPoolCycleMs(
  p: Pick<TbmPoolRow, "pool_id" | "cycle_ms" | "window_type">,
  fallbackCycleMs?: number | null
): number | null {
  if (typeof p.cycle_ms === "number" && Number.isFinite(p.cycle_ms) && p.cycle_ms > 0) {
    return p.cycle_ms;
  }
  if (fallbackCycleMs !== undefined) return fallbackCycleMs;
  const win = typeof p.window_type === "string" && p.window_type
    ? p.window_type
    : (typeof p.pool_id === "string" && p.pool_id.split(":").length >= 3 ? p.pool_id.split(":").slice(2).join(":") : "");
  if (win && WINDOW_NOMINAL_CYCLE_MS[win]) return WINDOW_NOMINAL_CYCLE_MS[win];
  return null;
}

/** R_hard = max(5% of Q_total, 50k tokens); an explicit 0% disables the floor. */
export function tbmHardFloor(totalTokens: number, hardFloorPercent = TBM_HARD_FLOOR_FRACTION * 100): number {
  if (hardFloorPercent === 0) return 0;
  const frac = finitePercent(hardFloorPercent) && hardFloorPercent > 0 ? hardFloorPercent / 100 : TBM_HARD_FLOOR_FRACTION;
  return Math.max(totalTokens * frac, TBM_HARD_FLOOR_MIN_TOKENS);
}

/**
 * R_soft(t) = R_hard + (R_soft_init - R_hard) * ((T_reset - t) / T_cycle)^kappa.
 * The cycle length comes from `cycleMs` (ingested per pool); when the window is
 * unknown the reserve stays at R_soft_init (conservative). Clamped to [R_hard, R_soft_init].
 */
export function tbmSoftReserve(
  totalTokens: number,
  nowMs: number,
  resetAtMs: number,
  cycleMs: number | null,
  kappa = TBM_DEFAULT_KAPPA,
  hardFloorPercent = TBM_HARD_FLOOR_FRACTION * 100,
  softReservePercent = TBM_SOFT_INIT_FRACTION * 100
): number {
  const hard = tbmHardFloor(totalTokens, hardFloorPercent);
  const softFrac = finitePercent(softReservePercent) ? softReservePercent / 100 : TBM_SOFT_INIT_FRACTION;
  const init = softFrac === 0 ? hard : Math.max(hard, Math.max(totalTokens * softFrac, TBM_SOFT_INIT_MIN_TOKENS));
  if (cycleMs === null || !(cycleMs > 0) || !Number.isFinite(resetAtMs)) return init;
  const k = Number.isFinite(kappa) && kappa > 0 ? kappa : TBM_DEFAULT_KAPPA;
  const frac = Math.min(1, Math.max(0, (resetAtMs - nowMs) / cycleMs));
  return hard + (init - hard) * Math.pow(frac, k);
}

/** Percentage-only soft reserve decay (20% -> 5% over cycleMs without 50k/200k token floor). */
export function tbmSoftReservePercent(
  nowMs: number,
  resetAtMs: number,
  cycleMs: number | null,
  kappa = TBM_DEFAULT_KAPPA,
  hardFloorPercent = TBM_HARD_FLOOR_FRACTION * 100,
  softReservePercent = TBM_SOFT_INIT_FRACTION * 100
): number {
  const hard = finitePercent(hardFloorPercent) ? hardFloorPercent : TBM_HARD_FLOOR_FRACTION * 100;
  const init = Math.max(hard, finitePercent(softReservePercent) ? softReservePercent : TBM_SOFT_INIT_FRACTION * 100);
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
  /** Optional account filter when evaluating a specific route. */
  account?: string | null;
  nowMs: number;
  /** Fallback cycle length when a pool row has no cycle_ms/window_type; null keeps R_soft at its initial level. */
  cycleMs?: number | null;
}

/**
 * Operator floor overrides per provider family (config/model-routing.json
 * rules.tbm_floor_percent, e.g. { codex: { hard: 0, soft: 0 } }). They replace
 * the stored pool floors; 0 disables a floor, so only depletion (remaining 0)
 * still blocks. Invalid entries are ignored and the stored floors apply.
 * Generic over the row type so extended row shapes (e.g. pacing's TbmPoolRow)
 * pass through with their extra fields.
 */
export function withPolicyTbmFloors<P extends TbmPoolRow>(p: P, overrides: unknown = (policy.rules as any).tbm_floor_percent): P {
  const entry = overrides && typeof overrides === "object" ? (overrides as any)[normalizeProviderFamily(p.provider) ?? ""] : null;
  if (!entry || typeof entry !== "object") return p;
  const out = { ...p };
  if (finitePercent(entry.hard)) out.hard_floor_percent = entry.hard;
  if (finitePercent(entry.soft)) out.soft_reserve_percent = entry.soft;
  return out;
}

function providerMatches(poolProvider: string, targetProvider: string): boolean {
  if (poolProvider === targetProvider) return true;
  return normalizeProviderFamily(poolProvider) === normalizeProviderFamily(targetProvider);
}

function accountMatches(pool: TbmPoolRow, account: string | null | undefined): boolean {
  if (!account) return true;
  if (!pool.account || pool.account === "default" || pool.account === "*") return true;
  return pool.account === account;
}

/**
 * Autonomous admission: kill-switch (fleet pool "*" or the provider's pools),
 * then hard floor, then dynamic soft reserve. Floors are checked only for
 * pools of the target provider; with no known provider only kill-switches apply
 * (the router enforces floors per route). Percentage-only pools evaluate
 * strictly in percent (5% hard, 20%->5% soft) without a 50k token floor.
 * Malformed rows fail closed.
 */
export function evaluateTbmAdmission(input: TbmAdmissionInput): TbmAdmissionResult {
  const { pools, provider, account, nowMs } = input;
  for (const p of pools) {
    if (p.kill_switch_active !== 0 && p.kill_switch_active !== 1) {
      return { ok: false, reason: "malformed_pool", pool_id: p.pool_id, detail: "kill_switch_active" };
    }
    const scoped = p.pool_id === TBM_FLEET_POOL_ID ||
      (provider !== null && providerMatches(p.provider, provider) && accountMatches(p, account));
    if (scoped && p.kill_switch_active === 1) {
      return { ok: false, reason: "kill_switch_active", pool_id: p.pool_id, detail: p.kill_switch_reason ?? undefined };
    }
  }
  if (provider === null) return { ok: true, reason: "ok", pool_id: null };
  for (const row of pools) {
    if (row.pool_id === TBM_FLEET_POOL_ID || !providerMatches(row.provider, provider) || !accountMatches(row, account)) continue;
    const p = withPolicyTbmFloors(row);
    const cycleMs = resolveTbmPoolCycleMs(p, input.cycleMs);
    const kappa = p.convexity_exponent ?? TBM_DEFAULT_KAPPA;
    if (isPercentOnlyTbmPool(p)) {
      const remPct = finitePercent(p.remaining_percent)
        ? p.remaining_percent
        : (finitePercent(p.headroom_percentage) ? p.headroom_percentage : null);
      if (remPct === null) {
        return { ok: false, reason: "malformed_pool", pool_id: p.pool_id, detail: "percent" };
      }
      const hardPct = finitePercent(p.hard_floor_percent) ? p.hard_floor_percent : TBM_HARD_FLOOR_FRACTION * 100;
      const softPct = tbmSoftReservePercent(nowMs, p.reset_at_ms, cycleMs, kappa, hardPct, p.soft_reserve_percent ?? TBM_SOFT_INIT_FRACTION * 100);
      const base = { pool_id: p.pool_id, r_hard: hardPct, r_soft: softPct, remaining: remPct };
      if (remPct <= hardPct) return { ok: false, reason: "hard_floor", ...base };
      if (remPct <= softPct) return { ok: false, reason: "soft_reserve", ...base };
      continue;
    }
    if (!finiteNonNeg(p.total_capacity_tokens) || !finiteNonNeg(p.remaining_tokens) || p.total_capacity_tokens <= 0) {
      return { ok: false, reason: "malformed_pool", pool_id: p.pool_id, detail: "capacity" };
    }
    const hard = tbmHardFloor(p.total_capacity_tokens, p.hard_floor_percent ?? TBM_HARD_FLOOR_FRACTION * 100);
    const soft = tbmSoftReserve(
      p.total_capacity_tokens,
      nowMs,
      p.reset_at_ms,
      cycleMs,
      kappa,
      p.hard_floor_percent ?? TBM_HARD_FLOOR_FRACTION * 100,
      p.soft_reserve_percent ?? TBM_SOFT_INIT_FRACTION * 100
    );
    const base = { pool_id: p.pool_id, r_hard: hard, r_soft: soft, remaining: p.remaining_tokens };
    if (p.remaining_tokens <= hard) return { ok: false, reason: "hard_floor", ...base };
    if (p.remaining_tokens <= soft) return { ok: false, reason: "soft_reserve", ...base };
  }
  return { ok: true, reason: "ok", pool_id: null };
}
