/**
 * AC1 TBM live quota ingestion (worker 09f7b606).
 *
 * Every accepted host/server quota collection fills tbm_quota_pools (per
 * canonical account/window, real reset + cycle) and tbm_banked_tranches.
 * Unknown token capacities stay unknown (capacity_known=0, percent-only);
 * tokens are never fabricated from percentages.
 *
 * Reuses host_quota_snapshots / server_quota_snapshots / fleet_accounts
 * storage and the fleet_accounts.label canonical name mapping (renames);
 * no duplicate tables.
 */
import type { SqlStorage } from "@cloudflare/workers-types";

export const TBM_POOL_ID_SEP = ":";

export function tbmPoolId(provider: string, account: string, window: string): string {
  return `${provider}${TBM_POOL_ID_SEP}${account}${TBM_POOL_ID_SEP}${window}`;
}

const HOUR = 3600000;
const DAY = 24 * HOUR;

/** Real cycle fallback per window name when no observed reset delta exists yet.
 *  Never a single hardcoded 7d: each window maps to its own nominal length,
 *  and observed reset deltas take precedence (see inferCycleMs). */
export const WINDOW_CYCLE_MS: Record<string, number> = {
  "5h": 5 * HOUR,
  five_hour: 5 * HOUR,
  "5hour": 5 * HOUR,
  hourly: HOUR,
  "1h": HOUR,
  daily: DAY,
  day: DAY,
  weekly: 7 * DAY,
  week: 7 * DAY,
  "7d": 7 * DAY,
  monthly: 30 * DAY,
  month: 30 * DAY,
  "30d": 30 * DAY,
};

export function inferCycleMs(
  windowName: string,
  prevResetMs: number | null,
  newResetMs: number | null
): number | null {
  if (
    typeof prevResetMs === "number" &&
    Number.isFinite(prevResetMs) &&
    typeof newResetMs === "number" &&
    Number.isFinite(newResetMs) &&
    newResetMs > prevResetMs
  ) {
    const delta = newResetMs - prevResetMs;
    if (delta > 0 && delta <= 366 * DAY) return Math.round(delta);
  }
  const mapped = WINDOW_CYCLE_MS[windowName];
  if (typeof mapped === "number" && Number.isFinite(mapped) && mapped > 0) return mapped;
  return null;
}

export interface TokenCapacity {
  known: boolean;
  total: number | null;
  remaining: number | null;
}

function finiteNonNeg(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0;
}

/**
 * Resolve real token capacity from a sanitized quota window (schema 1.1 native
 * fields). Known ONLY when unit === 'tokens' with explicit finite numbers:
 * - total from spend_limit, or spend_used + remaining when both finite
 * - remaining from credits_remaining, or spend_limit - spend_used
 * Any other unit (percent/credits/usd_cents/requests/null) stays unknown.
 * Never converts percentages into tokens.
 */
export function resolveTokenCapacity(window: any): TokenCapacity {
  const unknown: TokenCapacity = { known: false, total: null, remaining: null };
  if (!window || typeof window !== "object") return unknown;
  if (window.unit !== "tokens") return unknown;
  const credits = window.credits_remaining;
  const used = window.spend_used;
  const limit = window.spend_limit;
  const creditsOk = finiteNonNeg(credits);
  const usedOk = finiteNonNeg(used);
  const limitOk = finiteNonNeg(limit) && (limit as number) > 0;

  let remaining: number | null = null;
  if (creditsOk) remaining = credits as number;
  else if (limitOk && usedOk) remaining = Math.max(0, (limit as number) - (used as number));
  else return unknown;

  let total: number | null = null;
  if (limitOk) total = limit as number;
  else if (usedOk && creditsOk) total = (used as number) + (credits as number);
  else return { known: false, total: null, remaining };

  if (!Number.isFinite(total) || total <= 0) return { known: false, total: null, remaining };
  if (!Number.isFinite(remaining) || remaining < 0) return { known: false, total: null, remaining: null };
  if (remaining > total) remaining = total;
  return { known: true, total: Math.floor(total), remaining: Math.floor(remaining) };
}

/** Mirror of pacing hardFloorTokens (pacing.ts owned elsewhere): max(5% of total, 50k), capped by total. */
export function tbmHardFloorTokens(totalCapacityTokens: number): number {
  if (!Number.isFinite(totalCapacityTokens) || totalCapacityTokens <= 0) return 0;
  return Math.min(totalCapacityTokens, Math.max(totalCapacityTokens * 0.05, 50_000));
}

function tableExists(sql: SqlStorage, name: string): boolean {
  try {
    return (
      sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", name).toArray().length > 0
    );
  } catch {
    return false;
  }
}

function poolColumns(sql: SqlStorage): Set<string> {
  try {
    return new Set(
      sql.exec("PRAGMA table_info(tbm_quota_pools)").toArray().map((c: any) => c.name as string)
    );
  } catch {
    return new Set();
  }
}

/** Accounts (canonical fleet_accounts.label) serving this provider for host ingestion. */
export function accountsForHostProvider(sql: SqlStorage, hostId: string, provider: string): string[] {
  try {
    if (tableExists(sql, "fleet_routes") && tableExists(sql, "fleet_accounts")) {
      const rows = sql
        .exec(
          "SELECT a.label AS label FROM fleet_routes r JOIN fleet_accounts a ON r.account = a.label WHERE r.machine_id = ? AND a.provider = ? ORDER BY a.label",
          hostId,
          provider
        )
        .toArray() as any[];
      const labels = rows.map((r) => r.label).filter((l) => typeof l === "string" && l.length > 0);
      if (labels.length > 0) return [...new Set(labels)];
    }
  } catch {
    /* fall through to provider-wide lookup */
  }
  try {
    if (tableExists(sql, "fleet_accounts")) {
      const rows = sql
        .exec("SELECT label FROM fleet_accounts WHERE provider = ? ORDER BY label", provider)
        .toArray() as any[];
      const labels = rows.map((r) => r.label).filter((l) => typeof l === "string" && l.length > 0);
      if (labels.length > 0) return [...new Set(labels)];
    }
  } catch {
    /* fall through */
  }
  return [provider];
}

export interface TbmSyncCounts {
  pools: number;
  tranches: number;
}

function upsertPool(
  sql: SqlStorage,
  cols: Set<string>,
  args: {
    poolId: string;
    provider: string;
    account: string;
    total: number;
    remaining: number;
    headroom: number;
    resetAtMs: number;
    cycleMs: number | null;
    windowType: string;
    remainingPercent: number | null;
    capacityKnown: number;
    nowMs: number;
  }
): void {
  const hasCycle = cols.has("cycle_ms");
  const hasWindowType = cols.has("window_type");
  const hasRemainingPercent = cols.has("remaining_percent");
  const hasCapacityKnown = cols.has("capacity_known");
  const existing = sql
    .exec("SELECT pool_id FROM tbm_quota_pools WHERE pool_id = ?", args.poolId)
    .toArray()[0] as any;
  if (!existing) {
    const columns = [
      "pool_id",
      "provider",
      "account",
      "total_capacity_tokens",
      "remaining_tokens",
      "headroom_percentage",
      "reset_at_ms",
      "last_updated_ms",
    ];
    const values: any[] = [
      args.poolId,
      args.provider,
      args.account,
      args.total,
      args.remaining,
      args.headroom,
      args.resetAtMs,
      args.nowMs,
    ];
    if (hasCycle) {
      columns.push("cycle_ms");
      values.push(args.cycleMs);
    }
    if (hasWindowType) {
      columns.push("window_type");
      values.push(args.windowType);
    }
    if (hasRemainingPercent) {
      columns.push("remaining_percent");
      values.push(args.remainingPercent);
    }
    if (hasCapacityKnown) {
      columns.push("capacity_known");
      values.push(args.capacityKnown);
    }
    sql.exec(
      `INSERT INTO tbm_quota_pools (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})`,
      ...values
    );
    return;
  }
  const sets = [
    "provider = ?",
    "account = ?",
    "total_capacity_tokens = ?",
    "remaining_tokens = ?",
    "headroom_percentage = ?",
    "reset_at_ms = ?",
    "last_updated_ms = ?",
  ];
  const values: any[] = [
    args.provider,
    args.account,
    args.total,
    args.remaining,
    args.headroom,
    args.resetAtMs,
    args.nowMs,
  ];
  if (hasCycle) {
    sets.push("cycle_ms = ?");
    values.push(args.cycleMs);
  }
  if (hasWindowType) {
    sets.push("window_type = ?");
    values.push(args.windowType);
  }
  if (hasRemainingPercent) {
    sets.push("remaining_percent = ?");
    values.push(args.remainingPercent);
  }
  if (hasCapacityKnown) {
    sets.push("capacity_known = ?");
    values.push(args.capacityKnown);
  }
  values.push(args.poolId);
  sql.exec(`UPDATE tbm_quota_pools SET ${sets.join(", ")} WHERE pool_id = ?`, ...values);
}

function upsertBankedTranche(
  sql: SqlStorage,
  args: {
    trancheId: string;
    poolId: string;
    provider: string;
    burnable: number;
    grantedAtMs: number;
    expiresAtMs: number;
    nowMs: number;
  }
): void {
  const existing = sql
    .exec("SELECT tranche_id, status FROM tbm_banked_tranches WHERE tranche_id = ?", args.trancheId)
    .toArray()[0] as any;
  if (!existing) {
    sql.exec(
      `INSERT INTO tbm_banked_tranches
        (tranche_id, pool_id, provider, allocated_tokens, remaining_tokens, granted_at_ms, expires_at_ms, status, updated_at_ms)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)`,
      args.trancheId,
      args.poolId,
      args.provider,
      args.burnable,
      args.burnable,
      args.grantedAtMs,
      args.expiresAtMs,
      args.nowMs
    );
    return;
  }
  if (existing.status !== "active") return;
  sql.exec(
    `UPDATE tbm_banked_tranches SET pool_id = ?, provider = ?, allocated_tokens = ?, remaining_tokens = ?,
       granted_at_ms = ?, expires_at_ms = ?, updated_at_ms = ? WHERE tranche_id = ?`,
    args.poolId,
    args.provider,
    args.burnable,
    args.burnable,
    args.grantedAtMs,
    args.expiresAtMs,
    args.nowMs,
    args.trancheId
  );
}

interface WindowIngest {
  provider: string;
  account: string;
  windowName: string;
  window: any;
}

/** Core per-window upsert shared by host and server collections. */
export function syncWindowsIntoTbm(
  sql: SqlStorage,
  windows: WindowIngest[],
  nowMs: number
): TbmSyncCounts {
  const counts: TbmSyncCounts = { pools: 0, tranches: 0 };
  if (!tableExists(sql, "tbm_quota_pools") || !tableExists(sql, "tbm_banked_tranches")) return counts;
  const cols = poolColumns(sql);
  for (const item of windows) {
    const w = item.window;
    if (!w || typeof w !== "object") continue;
    if (w.present !== true) continue;
    if ((w as any).error === "unavailable") continue;
    const pct = (w as any).percent_remaining;
    const pctOk = typeof pct === "number" && Number.isFinite(pct) && pct >= 0 && pct <= 100;
    const resetRaw = (w as any).reset_at;
    if (typeof resetRaw !== "string") continue;
    const resetMs = Date.parse(resetRaw);
    if (!Number.isFinite(resetMs) || resetMs <= 0) continue;
    const cap = resolveTokenCapacity(w);
    const poolId = tbmPoolId(item.provider, item.account, item.windowName);
    let prevReset: number | null = null;
    try {
      const prev = sql
        .exec("SELECT reset_at_ms, cycle_ms FROM tbm_quota_pools WHERE pool_id = ?", poolId)
        .toArray()[0] as any;
      if (prev && Number.isFinite(Number(prev.reset_at_ms))) prevReset = Number(prev.reset_at_ms);
    } catch {
      prevReset = null;
    }
    const cycleMs = inferCycleMs(item.windowName, prevReset, resetMs);
    const total = cap.known && cap.total !== null ? cap.total : 0;
    const remaining = cap.remaining !== null ? cap.remaining : 0;
    const capacityKnown = cap.known ? 1 : 0;
    const headroom = pctOk ? (pct as number) : cap.remaining !== null && cap.total ? (cap.remaining / cap.total) * 100 : 0;
    upsertPool(sql, cols, {
      poolId,
      provider: item.provider,
      account: item.account,
      total,
      remaining,
      headroom,
      resetAtMs: resetMs,
      cycleMs,
      windowType: item.windowName,
      remainingPercent: pctOk ? (pct as number) : null,
      capacityKnown,
      nowMs,
    });
    counts.pools += 1;
    // Banked: only fixed (non-rolling) windows with known token capacity and a
    // future reset. Burnable surplus above R_hard expires at reset; percent-only
    // windows create no tranche (never fabricate tokens).
    const rolling = (w as any).rolling === true;
    if (!rolling && cap.known && resetMs > nowMs) {
      const hard = tbmHardFloorTokens(total);
      const burnable = Math.max(0, remaining - hard);
      if (burnable > 0) {
        upsertBankedTranche(sql, {
          trancheId: `${poolId}@${resetMs}`,
          poolId,
          provider: item.provider,
          burnable,
          grantedAtMs: nowMs,
          expiresAtMs: resetMs,
          nowMs,
        });
        counts.tranches += 1;
      }
    }
  }
  return counts;
}

/** Host collection hook: call after an accepted putSnapshot (inside its transaction). */
export function syncTbmFromHostSnapshot(
  sql: SqlStorage,
  hostId: string,
  routes: Record<string, any>,
  nowMs: number
): TbmSyncCounts {
  const windows: WindowIngest[] = [];
  for (const [provider, route] of Object.entries(routes || {})) {
    if (!route || typeof route !== "object") continue;
    if (route.status !== "ok") continue;
    const routeWindows = (route as any).windows;
    if (!routeWindows || typeof routeWindows !== "object") continue;
    const accounts = accountsForHostProvider(sql, hostId, provider);
    for (const [windowName, w] of Object.entries(routeWindows)) {
      for (const account of accounts) {
        windows.push({ provider, account, windowName, window: w });
      }
    }
  }
  return syncWindowsIntoTbm(sql, windows, nowMs);
}

/** Server collection hook: call after accepted putServerSnapshot rows. */
export function syncTbmFromServerAccounts(
  sql: SqlStorage,
  accounts: { account: string; provider: string; quota: any }[],
  nowMs: number
): TbmSyncCounts {
  const windows: WindowIngest[] = [];
  for (const row of accounts) {
    const routeWindows = (row.quota as any)?.windows;
    if (!routeWindows || typeof routeWindows !== "object") continue;
    if ((row.quota as any).status !== "ok") continue;
    for (const [windowName, w] of Object.entries(routeWindows)) {
      windows.push({ provider: row.provider, account: row.account, windowName, window: w });
    }
  }
  return syncWindowsIntoTbm(sql, windows, nowMs);
}
