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
