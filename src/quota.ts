import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from "./env";
import { sha256 } from "./auth/crypto";
import { getSession, allowedCallbackUrls, verifyCsrf } from "./auth/session";

export const QUOTA_SCHEMA_version = "1.1";
export const QUOTA_SCHEMA_VERSIONS = new Set<string>(["1.0", "1.1"]);
export const QUOTA_MAX_BODY_BYTES = 32_768;
export const QUOTA_STALE_MS = 1_200_000; // 1200 seconds (20 minutes) conservative boundary
export const MAX_WINDOWS_PER_PROVIDER = 32;
export const WINDOW_NAME_REGEX = /^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/;
export const NATIVE_UNITS = new Set<string>(["percent", "credits", "usd_cents", "requests", "tokens"]);
export const PROFILE_LABEL_REGEX = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$/;
export const COLLECTOR_VERSION_REGEX = /^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$/;
export const MAX_CREDITS_VALUE = 1e12;
export const MAX_SPEND_VALUE = 1e15;

export const ALLOWED_QUOTA_PROVIDERS = new Set<string>([
  "codex",
  // Orin's second Codex account (own CODEX_HOME, own ChatGPT token).
  // Kept as a wire route key: one host snapshot carries both Codex homes and
  // provider-keyed routes cannot hold two `codex` entries. All presentation,
  // filtering and admission canonicalize it to `codex` (see below).
  "codex-orin",
  "claude",
  "gemini",
  "grok",
  "zai",
  "go",
  "copilot",
]);

/**
 * Canonical provider model (fleet Usage/Quota mapping, 2026-10-26).
 *
 * - `codex-orin` is NOT a separate provider: it is a second Codex account
 *   (model provider OpenAI) with its own account label. Canonical provider
 *   is `codex`; the two Codex accounts are distinguished by account label
 *   (`shared-codex` vs `codex-orin`).
 * - `zai` (GLM via Z.ai) runs on the Codex harness: canonical harness
 *   `codex`, provider `zai`, account `shared-zai`.
 * - `go` (OpenCode Go subscription) runs on the OpenCode harness and serves
 *   the whole OpenCode model pool, not just muse.
 *
 * Legacy wire route keys stay accepted on ingest (stored rows and host
 * snapshots keyed by old names keep working); every output path
 * canonicalizes via these helpers.
 */
export const LEGACY_QUOTA_ROUTE_PROVIDERS: Record<string, string> = {
  "codex-orin": "codex",
};

export function canonicalQuotaProvider(routeKey: string): string {
  return LEGACY_QUOTA_ROUTE_PROVIDERS[routeKey] ?? routeKey;
}

/** Canonical output providers: legacy wire route keys never appear here. */
export function canonicalQuotaProviders(): string[] {
  return [...ALLOWED_QUOTA_PROVIDERS].filter((p) => !(p in LEGACY_QUOTA_ROUTE_PROVIDERS));
}

/**
 * Canonical harness (engine) for display: the native `zcodex` binary is a
 * Codex harness serving the Z.ai model provider. Routing keeps the raw
 * route engine; quota rows expose this canonical harness alongside the
 * provider/account pair.
 */
export function canonicalHarness(engine: string): string {
  return engine === "zcodex" ? "codex" : engine;
}

/** Legacy account labels accepted on ingest, canonicalized on output. */
export const LEGACY_ACCOUNT_LABELS: Record<string, string> = {
  // The OpenCode Go subscription serves the whole Go model pool (muse, glm,
  // kimi and others); the old muse-only label must not imply muse-only.
  "urgent-muse": "shared-go",
};

export function canonicalAccountLabel(label: string): string {
  return LEGACY_ACCOUNT_LABELS[label] ?? label;
}

/** Reverse lookup: legacy wire route key for a canonical provider, if any. */
export function legacyRouteKeyFor(canonicalProvider: string): string | null {
  for (const [legacy, canonical] of Object.entries(LEGACY_QUOTA_ROUTE_PROVIDERS)) {
    if (canonical === canonicalProvider) return legacy;
  }
  return null;
}

/** Reverse lookup: legacy account labels canonicalizing to the given account. */
export function legacyLabelsFor(canonicalLabel: string): string[] {
  return Object.entries(LEGACY_ACCOUNT_LABELS)
    .filter(([, c]) => c === canonicalLabel)
    .map(([legacy]) => legacy);
}

/**
 * One account row per canonical identity. During a label rename transition
 * the registry can briefly hold both the legacy and the canonical row for
 * one pool: prefer the canonical label so every consumer surfaces the pool
 * exactly once. (Stored subscription rows self-canonicalize on every host
 * report, so observations keep joining through the transition.)
 */
export function dedupeAccountsByCanonical<T extends { label: string; provider: string }>(rows: T[]): T[] {
  const byIdentity = new Map<string, T>();
  for (const row of rows) {
    const key = canonicalAccountLabel(row.label) + " " + canonicalQuotaProvider(row.provider);
    const prev = byIdentity.get(key);
    if (!prev) {
      byIdentity.set(key, row);
    } else if (prev.label !== canonicalAccountLabel(prev.label) && row.label === canonicalAccountLabel(row.label)) {
      byIdentity.set(key, row);
    }
  }
  return [...byIdentity.values()];
}

/**
 * Pick the host-snapshot route that backs one fleet account. A snapshot is
 * keyed by quota route key, not by account:
 * - when the route key equals the account label and canonicalizes to the
 *   account's provider, that route wins (the Orin second-Codex-home case:
 *   label `codex-orin` -> provider `codex`);
 * - otherwise the canonical provider route backs the account — unless the
 *   label itself names a distinct quota home (a legacy route key with the
 *   same canonical provider, e.g. `codex-orin`), which must never borrow the
 *   shared provider route from a host that only has the other home.
 */
export function snapshotRouteForAccount(
  accountLabel: string,
  provider: string,
  snapRoutes: Record<string, any> | null | undefined,
): any | null {
  if (!snapRoutes || typeof snapRoutes !== "object") return null;
  const canonical = canonicalQuotaProvider(provider);
  const labelRoute = (snapRoutes as Record<string, any>)[accountLabel];
  if (labelRoute !== undefined) {
    return canonicalQuotaProvider(accountLabel) === canonical ? labelRoute : null;
  }
  if (canonicalQuotaProvider(accountLabel) === canonical && accountLabel !== canonical) return null;
  return (snapRoutes as Record<string, any>)[canonical] ?? null;
}

export const QUOTA_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS server_quota_snapshots (account TEXT PRIMARY KEY, provider TEXT NOT NULL, captured_at TEXT NOT NULL, quota_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_quota_snapshots (
  host_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  collector_version TEXT,
  captured_at TEXT NOT NULL,
  captured_at_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  received_at_ms INTEGER NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  routes_json TEXT NOT NULL
);
`;

export const TBM_QUOTA_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS tbm_quota_pools (
    pool_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT 'default',
    total_capacity_tokens INTEGER NOT NULL,
    remaining_tokens INTEGER NOT NULL,
    headroom_percentage REAL NOT NULL,
    reset_at_ms INTEGER NOT NULL,
    hard_floor_percent REAL NOT NULL DEFAULT 5.0,
    soft_reserve_percent REAL NOT NULL DEFAULT 20.0,
    convexity_exponent REAL NOT NULL DEFAULT 1.5,
    current_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,
    target_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,
    pacing_ratio REAL NOT NULL DEFAULT 1.0,
    kill_switch_active INTEGER NOT NULL DEFAULT 0,
    kill_switch_reason TEXT,
    kill_switch_updated_at TEXT,
    last_updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tbm_banked_tranches (
    tranche_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES tbm_quota_pools(pool_id),
    provider TEXT NOT NULL,
    label TEXT,
    allocated_tokens INTEGER NOT NULL,
    remaining_tokens INTEGER NOT NULL,
    granted_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    status TEXT CHECK(status IN ('active', 'exhausted', 'expired')) NOT NULL DEFAULT 'active',
    banked_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,
    target_banked_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,
    expired_unused_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tbm_reward_events (
    event_id TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL,
    pool_id TEXT NOT NULL,
    intake_task_id TEXT NOT NULL,
    active_mode TEXT CHECK(active_mode IN ('high', 'super-think', 'mini-swarm')) NOT NULL,
    swarm_concurrency INTEGER NOT NULL DEFAULT 1,
    useful_work_reward REAL NOT NULL,
    pacing_error_penalty REAL NOT NULL,
    floor_breach_penalty REAL NOT NULL,
    engine_mix_penalty REAL NOT NULL,
    waste_at_reset_penalty REAL NOT NULL,
    banked_waste_penalty REAL NOT NULL,
    net_reward REAL NOT NULL,
    token_efficiency_eta REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tbm_metrics_rollup (
    rollup_id TEXT PRIMARY KEY,
    window_type TEXT CHECK(window_type IN ('1m', '10m', '1h', 'reset_window', '24h')) NOT NULL,
    start_time_ms INTEGER NOT NULL,
    end_time_ms INTEGER NOT NULL,
    pool_id TEXT NOT NULL,
    avg_burn_rate_vs_target REAL NOT NULL,
    leftover_quota_at_reset INTEGER NOT NULL DEFAULT 0,
    soft_floor_breach_count INTEGER NOT NULL DEFAULT 0,
    hard_floor_breach_count INTEGER NOT NULL DEFAULT 0,
    total_tokens_burned INTEGER NOT NULL,
    useful_work_units REAL NOT NULL,
    token_efficiency_eta REAL NOT NULL,
    idle_wasted_quota INTEGER NOT NULL DEFAULT 0,
    engine_mix_free_share REAL NOT NULL,
    engine_mix_paid_share REAL NOT NULL,
    engine_mix_grok_share REAL NOT NULL,
    admission_rejection_rate REAL NOT NULL,
    no_eligible_route_rate REAL NOT NULL,
    avg_time_to_outcome_seconds REAL NOT NULL,
    banked_tokens_burned INTEGER NOT NULL DEFAULT 0,
    banked_tokens_expired_unused INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pool_provider ON tbm_quota_pools(provider);
CREATE INDEX IF NOT EXISTS idx_banked_edf ON tbm_banked_tranches(status, expires_at_ms ASC);
CREATE INDEX IF NOT EXISTS idx_reward_timestamp ON tbm_reward_events(timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_reward_intake ON tbm_reward_events(intake_task_id);
CREATE INDEX IF NOT EXISTS idx_rollup_query ON tbm_metrics_rollup(pool_id, window_type, end_time_ms DESC);
`;

/** Idempotent TBM migration: safe on fresh and already-migrated DBs. */
export function ensureTbmQuotaSchema(sql: SqlStorage): void {
  for (const statement of TBM_QUOTA_SCHEMA_SQL.split(";")) {
    const trimmed = statement.trim();
    if (trimmed) {
      sql.exec(trimmed);
    }
  }
}

const TOP_LEVEL_KEYS = new Set<string>([
  "schema_version",
  "captured_at",
  "host_id",
  "status",
  "error",
  "routes",
]);

const OPTIONAL_TOP_KEYS = new Set<string>(["collector_version"]);

const ROUTE_KEYS = new Set<string>([
  "status",
  "error",
  "observed_at",
  "details",
  "windows",
]);

const WINDOW_KEYS = new Set<string>([
  "percent_remaining",
  "reset_at",
  "present",
  "rolling",
  "error",
]);

// Schema 1.1 only: bounded provider-native usage fields. Unknown keys
// (tokens, cookies, emails, raw output) are never allowlisted here.
const OPTIONAL_WINDOW_KEYS = new Set<string>([
  "credits_remaining",
  "spend_used",
  "spend_limit",
  "unit",
  "profile",
]);

const OPTIONAL_DETAILS_KEYS = new Set<string>([
  "credits_remaining",
  "spend_used",
  "spend_limit",
  "unit",
  "profile",
  "cooldown_until",
]);

const FORBIDDEN_PROTO_KEYS = new Set<string>([
  "__proto__",
  "constructor",
  "prototype",
]);

export interface QuotaWindowNativeDTO {
  credits_remaining: number | null;
  spend_used: number | null;
  spend_limit: number | null;
  unit: "percent" | "credits" | "usd_cents" | "requests" | "tokens" | null;
  profile: string | null;
}

export interface QuotaWindowDTO {
  percent_remaining: number | null;
  reset_at: string | null;
  present: boolean;
  rolling: boolean;
  error?: "unavailable" | null;
  credits_remaining?: number | null;
  spend_used?: number | null;
  spend_limit?: number | null;
  unit?: QuotaWindowNativeDTO["unit"];
  profile?: string | null;
}

export interface QuotaRouteDetailsDTO {
  limit_reached: boolean | null;
  has_grok_code_access?: boolean | null;
  stale?: boolean | null;
  credits_remaining?: number | null;
  spend_used?: number | null;
  spend_limit?: number | null;
  unit?: QuotaWindowNativeDTO["unit"];
  profile?: string | null;
  /** Known provider backoff deadline (UTC ISO). Active holds block admission even when quota looks fine. */
  cooldown_until?: string | null;
}

export interface QuotaRouteDTO {
  status: "ok" | "error" | "unknown";
  error: "unavailable" | null;
  observed_at: string | null;
  details: QuotaRouteDetailsDTO;
  windows: Record<string, QuotaWindowDTO>;
}

export interface QuotaSnapshotDTO {
  schema_version: "1.0" | "1.1";
  collector_version?: string | null;
  captured_at: string | null;
  received_at?: string | null;
  host_id: string;
  stale?: boolean;
  status: "ok" | "error" | "unknown" | "stale";
  error: "unavailable" | null;
  routes: Record<string, QuotaRouteDTO>;
}

const PRIVATE_NO_STORE_HEADERS: Record<string, string> = {
  "Content-Type": "application/json",
  "Cache-Control": "private, no-store",
};

function jsonResponse(body: Record<string, any>, status: number = 200, extraHeaders?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...PRIVATE_NO_STORE_HEADERS,
      ...(extraHeaders || {}),
    },
  });
}

/** Canonical caller authority injected from Hub routing (hub.ts authenticateBusRequest). */
export interface QuotaFleetAuth {
  caller?: {
    kind?: string;
    isAdmin?: boolean;
    actor?: string;
    machineId?: string;
  };
  scopes?: string[];
  error?: Response;
}

export type CanonicalQuotaAuth = (request: Request) => Promise<QuotaFleetAuth>;

/** Fleet-wide usage reads: human admins, fleet-launch agents, routing clients, and hosts.
 * Hosts may read only this sanitized usage projection (percentages, native
 * amounts with unit labels, resets, freshness states); no tokens, cookies or
 * transcripts ever travel here. This keeps `hub quota` working for
 * host-credential operators (host kind, `host` scope). */
export function fleetReadGrant(caller: QuotaFleetAuth["caller"], scopes?: string[]): boolean {
  if (!caller) return false;
  if (caller.isAdmin === true) return true;
  if (caller.kind === "agent" && Array.isArray(scopes) && scopes.includes("fleet:launch")) return true;
  if (caller.kind === "routing-client" && Array.isArray(scopes) && scopes.includes("routing:submit")) return true;
  if (caller.kind === "host" && Array.isArray(scopes) && scopes.includes("host")) return true;
  return false;
}

function checkSessionReadIntegrity(request: Request, reqUrl: URL, csrfToken: string): Response | null {
  const originHeader = request.headers.get("Origin");
  if (originHeader && originHeader !== reqUrl.origin) {
    return jsonResponse({ error: "forbidden", reason: "origin_mismatch" }, 403);
  }

  const secFetchSite = request.headers.get("Sec-Fetch-Site");
  if (secFetchSite && secFetchSite.toLowerCase() === "cross-site") {
    return jsonResponse({ error: "forbidden", reason: "cross_site_request_denied" }, 403);
  }

  const csrfHeader = request.headers.get("x-csrf-token");
  if (csrfHeader !== null && csrfHeader !== csrfToken) {
    return jsonResponse({ error: "forbidden", reason: "invalid_csrf_token" }, 403);
  }
  return null;
}

const FLEET_LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const FLEET_HOST_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

export interface FleetQuotaFilters {
  host_id: string | null;
  provider: string | null;
  engine: string | null;
  account: string | null;
}

export function parseFleetQuotaFilters(reqUrl: URL): { ok: boolean; filters?: FleetQuotaFilters; error?: string } {
  const pick = (name: string): string | null => {
    const v = reqUrl.searchParams.get(name);
    if (v === null || v === "") return null;
    return v;
  };
  // Legacy names stay accepted (stored rows and configs keyed by old names
  // keep working) and canonicalize before validation: `codex-orin` filters
  // the two Codex accounts, `urgent-muse` the Go pool account.
  const rawProvider = pick("provider");
  const rawAccount = pick("account");
  const filters: FleetQuotaFilters = {
    host_id: pick("host_id"),
    provider: rawProvider === null ? null : canonicalQuotaProvider(rawProvider),
    engine: pick("engine"),
    account: rawAccount === null ? null : canonicalAccountLabel(rawAccount),
  };
  if (filters.host_id !== null && !FLEET_HOST_ID.test(filters.host_id)) return { ok: false, error: "invalid_host_id" };
  if (filters.provider !== null && !ALLOWED_QUOTA_PROVIDERS.has(filters.provider)) return { ok: false, error: "invalid_provider" };
  if (filters.engine !== null && !FLEET_LABEL.test(filters.engine)) return { ok: false, error: "invalid_engine" };
  if (filters.account !== null && !FLEET_LABEL.test(filters.account)) return { ok: false, error: "invalid_account" };
  return { ok: true, filters };
}

function isFreshTs(value: unknown, nowMs: number, ttlMs: number): boolean {
  if (typeof value !== "string") return false;
  const at = Date.parse(value);
  return Number.isFinite(at) && at <= nowMs && nowMs - at <= ttlMs;
}

function windowMinPercent(quota: any): number | null {
  let remaining: number | null = null;
  for (const w of Object.values((quota && quota.windows) || {}) as any[]) {
    if (w === null || typeof w !== "object" || w.present !== true || w.error === "unavailable") continue;
    if (typeof w.percent_remaining !== "number" || !Number.isFinite(w.percent_remaining)) continue;
    remaining = remaining === null ? w.percent_remaining : Math.min(remaining, w.percent_remaining);
  }
  return remaining;
}

function observationUnavailable(quota: any): boolean {
  if (!quota || typeof quota !== "object") return true;
  if (quota.status !== "ok" || quota.details?.stale === true) return true;
  for (const w of Object.values(quota.windows || {}) as any[]) {
    if (w !== null && typeof w === "object" && w.error === "unavailable") return true;
  }
  return windowMinPercent(quota) === null;
}

function isPlainObject(val: unknown): val is Record<string, any> {
  if (typeof val !== "object" || val === null || Array.isArray(val)) return false;
  const proto = Object.getPrototypeOf(val);
  return proto === Object.prototype || proto === null;
}

/**
 * Validates a strict UTC-aware ISO-8601 timestamp string (ending in Z or +00:00)
 * and normalizes it to canonical ISO-8601 UTC format (.toISOString()).
 * Rejects empty strings, naive timestamps without timezone, non-UTC offsets, or invalid calendar dates.
 */
export function parseUtcIso(raw: unknown): { iso: string; ms: number } | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 64) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|\+00:00)$/.exec(raw);
  if (!match) return null;

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);

  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;

  const ms = Date.parse(raw);
  if (!Number.isFinite(ms)) return null;

  const dt = new Date(ms);
  if (
    dt.getUTCFullYear() !== year ||
    dt.getUTCMonth() + 1 !== month ||
    dt.getUTCDate() !== day ||
    dt.getUTCHours() !== hour ||
    dt.getUTCMinutes() !== minute ||
    dt.getUTCSeconds() !== second
  ) {
    return null;
  }

  return { iso: dt.toISOString(), ms };
}

function parseNativeAmount(val: unknown, cap: number): { ok: boolean; value: number | null } {
  if (val === null || val === undefined) return { ok: true, value: null };
  if (typeof val === "boolean" || typeof val !== "number" || !Number.isFinite(val) || val < 0 || val > cap) {
    return { ok: false, value: null };
  }
  return { ok: true, value: val };
}

function parseNativeUnit(val: unknown): { ok: boolean; value: QuotaWindowNativeDTO["unit"] } {
  if (val === null || val === undefined) return { ok: true, value: null };
  if (typeof val !== "string" || !NATIVE_UNITS.has(val)) return { ok: false, value: null };
  return { ok: true, value: val as QuotaWindowNativeDTO["unit"] };
}

function parseNativeProfile(val: unknown): { ok: boolean; value: string | null } {
  if (val === null || val === undefined) return { ok: true, value: null };
  if (typeof val !== "string" || !PROFILE_LABEL_REGEX.test(val)) return { ok: false, value: null };
  return { ok: true, value: val };
}

function parseNativeFields(
  holder: Record<string, any>,
  allowed: Set<string>
): { ok: boolean; reason?: string; native?: QuotaWindowNativeDTO } {
  const native: QuotaWindowNativeDTO = {
    credits_remaining: null,
    spend_used: null,
    spend_limit: null,
    unit: null,
    profile: null,
  };
  if (allowed.has("credits_remaining")) {
    const r = parseNativeAmount(holder["credits_remaining"], MAX_CREDITS_VALUE);
    if (!r.ok) return { ok: false, reason: "invalid_credits_remaining" };
    native.credits_remaining = r.value;
  }
  if (allowed.has("spend_used")) {
    const r = parseNativeAmount(holder["spend_used"], MAX_SPEND_VALUE);
    if (!r.ok) return { ok: false, reason: "invalid_spend_used" };
    native.spend_used = r.value;
  }
  if (allowed.has("spend_limit")) {
    const r = parseNativeAmount(holder["spend_limit"], MAX_SPEND_VALUE);
    if (!r.ok) return { ok: false, reason: "invalid_spend_limit" };
    native.spend_limit = r.value;
  }
  if (allowed.has("unit")) {
    const r = parseNativeUnit(holder["unit"]);
    if (!r.ok) return { ok: false, reason: "invalid_unit" };
    native.unit = r.value;
  }
  if (allowed.has("profile")) {
    const r = parseNativeProfile(holder["profile"]);
    if (!r.ok) return { ok: false, reason: "invalid_profile" };
    native.profile = r.value;
  }
  return { ok: true, native };
}

async function readBoundedJsonBody(
  request: Request,
  maxBytes: number = QUOTA_MAX_BODY_BYTES
): Promise<{ data?: Record<string, any>; errorStatus?: number; errorCode?: string }> {
  const contentLengthHeader = request.headers.get("content-length");
  if (contentLengthHeader !== null && contentLengthHeader.trim() !== "") {
    const declared = Number(contentLengthHeader);
    if (!Number.isFinite(declared) || declared < 0) {
      return { errorStatus: 400, errorCode: "invalid_content_length" };
    }
    if (declared > maxBytes) {
      return { errorStatus: 413, errorCode: "body_too_large" };
    }
  }

  if (!request.body) {
    return { errorStatus: 400, errorCode: "missing_body" };
  }

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;

  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      if (part.value) {
        totalBytes += part.value.byteLength;
        if (totalBytes > maxBytes) {
          await reader.cancel();
          return { errorStatus: 413, errorCode: "body_too_large" };
        }
        chunks.push(part.value);
      }
    }
  } catch {
    return { errorStatus: 400, errorCode: "stream_read_error" };
  } finally {
    try {
      reader.releaseLock();
    } catch {}
  }

  if (totalBytes === 0) {
    return { errorStatus: 400, errorCode: "missing_body" };
  }

  const allBytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    allBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(allBytes);
  } catch {
    return { errorStatus: 400, errorCode: "invalid_utf8" };
  }

  // Guard against raw __proto__ key in JSON text before parse
  if (/"__proto__"\s*:/.test(text)) {
    return { errorStatus: 400, errorCode: "unknown_field" };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { errorStatus: 400, errorCode: "invalid_json" };
  }

  if (!isPlainObject(parsed)) {
    return { errorStatus: 400, errorCode: "invalid_json_object" };
  }

  return { data: parsed };
}

export interface ValidationResult {
  ok: boolean;
  status: number;
  error?: string;
  reason?: string;
  snapshot?: {
    schema_version: "1.0" | "1.1";
    collector_version: string | null;
    captured_at: string;
    captured_at_ms: number;
    host_id: string;
    status: "ok" | "error" | "unknown";
    error: "unavailable" | null;
    routes: Record<string, QuotaRouteDTO>;
  };
}

export function validateQuotaSnapshotPayload(
  raw: unknown,
  expectedHostId: string,
  nowMs: number
): ValidationResult {
  if (!isPlainObject(raw)) {
    return { ok: false, status: 400, error: "invalid_json_object" };
  }

  const rawKeys = Object.keys(raw);
  for (const k of rawKeys) {
    if (FORBIDDEN_PROTO_KEYS.has(k) || (!TOP_LEVEL_KEYS.has(k) && !OPTIONAL_TOP_KEYS.has(k))) {
      return { ok: false, status: 400, error: "unknown_field", reason: "unexpected_top_level_key" };
    }
  }

  for (const reqKey of TOP_LEVEL_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(raw, reqKey)) {
      return { ok: false, status: 400, error: "missing_field", reason: `missing_${reqKey}` };
    }
  }

  if (typeof raw.host_id !== "string" || raw.host_id.trim().length === 0 || raw.host_id.length > 128) {
    return { ok: false, status: 400, error: "invalid_host_id" };
  }

  if (raw.host_id !== expectedHostId) {
    return { ok: false, status: 403, error: "forbidden", reason: "host_id_mismatch" };
  }

  if (raw.schema_version !== "1.0" && raw.schema_version !== "1.1") {
    return { ok: false, status: 400, error: "invalid_schema_version" };
  }
  const isV11 = raw.schema_version === "1.1";

  let collectorVersion: string | null = null;
  if (Object.prototype.hasOwnProperty.call(raw, "collector_version")) {
    if (!isV11) {
      return { ok: false, status: 400, error: "unknown_field", reason: "collector_version_requires_1_1" };
    }
    const cv = (raw as Record<string, any>)["collector_version"];
    if (cv !== null) {
      if (typeof cv !== "string" || cv.length === 0 || cv.length > 64 || !COLLECTOR_VERSION_REGEX.test(cv)) {
        return { ok: false, status: 400, error: "invalid_collector_version" };
      }
      collectorVersion = cv;
    }
  }

  const capturedParsed = parseUtcIso(raw.captured_at);
  if (!capturedParsed) {
    return { ok: false, status: 400, error: "invalid_captured_at" };
  }

  if (capturedParsed.ms > nowMs) {
    return { ok: false, status: 400, error: "future_captured_at" };
  }

  if (raw.status !== "ok" && raw.status !== "error" && raw.status !== "unknown") {
    return { ok: false, status: 400, error: "invalid_status" };
  }

  if (raw.error !== null && raw.error !== "unavailable") {
    return { ok: false, status: 400, error: "invalid_error" };
  }

  if (raw.status === "ok" && raw.error !== null) {
    return { ok: false, status: 400, error: "inconsistent_top_level_status_error" };
  }

  if (!isPlainObject(raw.routes)) {
    return { ok: false, status: 400, error: "invalid_routes" };
  }

  const sanitizedRoutes: Record<string, QuotaRouteDTO> = Object.create(null);
  const routeEntries = Object.entries(raw.routes);

  for (const [provider, routeVal] of routeEntries) {
    if (FORBIDDEN_PROTO_KEYS.has(provider) || !ALLOWED_QUOTA_PROVIDERS.has(provider)) {
      return { ok: false, status: 400, error: "unknown_provider" };
    }

    if (!isPlainObject(routeVal)) {
      return { ok: false, status: 400, error: "invalid_route_object" };
    }

    for (const rk of Object.keys(routeVal)) {
      if (FORBIDDEN_PROTO_KEYS.has(rk) || !ROUTE_KEYS.has(rk)) {
        return { ok: false, status: 400, error: "unknown_route_field" };
      }
    }

    for (const reqRk of ROUTE_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(routeVal, reqRk)) {
        return { ok: false, status: 400, error: "missing_route_field", reason: `missing_${reqRk}` };
      }
    }

    if (routeVal.status !== "ok" && routeVal.status !== "error" && routeVal.status !== "unknown") {
      return { ok: false, status: 400, error: "invalid_route_status" };
    }

    if (routeVal.error !== null && routeVal.error !== "unavailable") {
      return { ok: false, status: 400, error: "invalid_route_error" };
    }

    let observedAtIso: string | null = null;
    if (routeVal.observed_at !== null) {
      const obsParsed = parseUtcIso(routeVal.observed_at);
      if (!obsParsed) {
        return { ok: false, status: 400, error: "invalid_observed_at" };
      }
      if (obsParsed.ms > nowMs || obsParsed.ms > capturedParsed.ms) {
        return { ok: false, status: 400, error: "future_observed_at" };
      }
      observedAtIso = obsParsed.iso;
    }

    if (!isPlainObject(routeVal.details)) {
      return { ok: false, status: 400, error: "invalid_route_details" };
    }

    const detailsVal = routeVal.details;
    for (const dk of Object.keys(detailsVal)) {
      if (FORBIDDEN_PROTO_KEYS.has(dk)) {
        return { ok: false, status: 400, error: "unknown_details_field" };
      }
      if (dk === "limit_reached" || dk === "stale") continue;
      if (dk === "has_grok_code_access" && provider === "grok") continue;
      if (isV11 && OPTIONAL_DETAILS_KEYS.has(dk)) continue;
      return { ok: false, status: 400, error: "unknown_details_field" };
    }

    if (!Object.prototype.hasOwnProperty.call(detailsVal, "limit_reached")) {
      return { ok: false, status: 400, error: "missing_limit_reached" };
    }
    if (detailsVal.limit_reached !== null && typeof detailsVal.limit_reached !== "boolean") {
      return { ok: false, status: 400, error: "invalid_limit_reached" };
    }

    if (provider === "grok") {
      if (
        Object.prototype.hasOwnProperty.call(detailsVal, "has_grok_code_access") &&
        detailsVal.has_grok_code_access !== null &&
        typeof detailsVal.has_grok_code_access !== "boolean"
      ) {
        return { ok: false, status: 400, error: "invalid_has_grok_code_access" };
      }
    }

    let detailsStale: boolean | null | undefined = undefined;
    if (Object.prototype.hasOwnProperty.call(detailsVal, "stale")) {
      if (detailsVal.stale !== null && typeof detailsVal.stale !== "boolean") {
        return { ok: false, status: 400, error: "invalid_details_stale" };
      }
      detailsStale = detailsVal.stale;
    }

    if (!isPlainObject(routeVal.windows)) {
      return { ok: false, status: 400, error: "invalid_route_windows" };
    }

    const windowEntries = Object.entries(routeVal.windows);
    if (windowEntries.length > MAX_WINDOWS_PER_PROVIDER) {
      return { ok: false, status: 400, error: "too_many_windows" };
    }

    const sanitizedWindows: Record<string, QuotaWindowDTO> = Object.create(null);
    let hasExplicitWindowError = false;

    for (const [winName, winVal] of windowEntries) {
      if (FORBIDDEN_PROTO_KEYS.has(winName) || !WINDOW_NAME_REGEX.test(winName)) {
        return { ok: false, status: 400, error: "invalid_window_name" };
      }

      if (!isPlainObject(winVal)) {
        return { ok: false, status: 400, error: "invalid_window_object" };
      }

      for (const wk of Object.keys(winVal)) {
        if (FORBIDDEN_PROTO_KEYS.has(wk)) {
          return { ok: false, status: 400, error: "unknown_window_field" };
        }
        if (!WINDOW_KEYS.has(wk) && !(isV11 && OPTIONAL_WINDOW_KEYS.has(wk))) {
          return { ok: false, status: 400, error: "unknown_window_field" };
        }
      }

      for (const reqWk of ["percent_remaining", "reset_at", "present", "rolling"]) {
        if (!Object.prototype.hasOwnProperty.call(winVal, reqWk)) {
          return { ok: false, status: 400, error: "missing_window_field", reason: `missing_${reqWk}` };
        }
      }

      if (typeof winVal.present !== "boolean") {
        return { ok: false, status: 400, error: "invalid_window_present" };
      }

      if (typeof winVal.rolling !== "boolean") {
        return { ok: false, status: 400, error: "invalid_window_rolling" };
      }

      const pct = winVal.percent_remaining;
      if (pct !== null) {
        if (typeof pct !== "number" || !Number.isFinite(pct) || pct < 0 || pct > 100) {
          return { ok: false, status: 400, error: "invalid_percent_remaining" };
        }
      }

      let resetAtIso: string | null = null;
      if (winVal.reset_at !== null) {
        const resetParsed = parseUtcIso(winVal.reset_at);
        if (!resetParsed) {
          return { ok: false, status: 400, error: "invalid_reset_at" };
        }
        resetAtIso = resetParsed.iso;
      }

      let hasNativeValue = false;
      if (isV11) {
        for (const nk of OPTIONAL_WINDOW_KEYS) {
          if (winVal[nk] !== null && winVal[nk] !== undefined) {
            hasNativeValue = true;
            break;
          }
        }
      }
      if (winVal.present === false && (pct !== null || resetAtIso !== null || hasNativeValue)) {
        return { ok: false, status: 400, error: "inconsistent_absent_window" };
      }

      let hasWinErrorProp = false;
      let winErrorVal: "unavailable" | null = null;
      if (Object.prototype.hasOwnProperty.call(winVal, "error")) {
        hasWinErrorProp = true;
        if (winVal.error !== null && winVal.error !== "unavailable") {
          return { ok: false, status: 400, error: "invalid_window_error" };
        }
        winErrorVal = winVal.error;
        if (winErrorVal === "unavailable") {
          hasExplicitWindowError = true;
        }
      }

      const sanitizedWin: QuotaWindowDTO = {
        percent_remaining: pct,
        reset_at: resetAtIso,
        present: winVal.present,
        rolling: winVal.rolling,
      };
      if (hasWinErrorProp) {
        sanitizedWin.error = winErrorVal;
      }
      if (isV11) {
        const nativeRes = parseNativeFields(winVal, OPTIONAL_WINDOW_KEYS);
        if (!nativeRes.ok) {
          return { ok: false, status: 400, error: "invalid_window_native", reason: nativeRes.reason };
        }
        Object.assign(sanitizedWin, nativeRes.native);
      }
      sanitizedWindows[winName] = sanitizedWin;
    }

    const isNativeStale = detailsStale === true;
    const isRouteUnavailable =
      routeVal.status === "error" ||
      routeVal.error === "unavailable" ||
      isNativeStale ||
      hasExplicitWindowError;

    const finalRouteStatus: "ok" | "error" | "unknown" = isRouteUnavailable
      ? "error"
      : routeVal.status;
    const finalRouteError: "unavailable" | null = isRouteUnavailable
      ? "unavailable"
      : routeVal.error;

    const sanitizedDetails: QuotaRouteDetailsDTO = {
      limit_reached: detailsVal.limit_reached,
    };
    if (provider === "grok") {
      sanitizedDetails.has_grok_code_access = Object.prototype.hasOwnProperty.call(detailsVal, "has_grok_code_access")
        ? detailsVal.has_grok_code_access
        : null;
    }
    if (detailsStale !== undefined) {
      sanitizedDetails.stale = detailsStale;
    }
    if (isV11) {
      const nativeRes = parseNativeFields(detailsVal, OPTIONAL_DETAILS_KEYS);
      if (!nativeRes.ok) {
        return { ok: false, status: 400, error: "invalid_details_native", reason: nativeRes.reason };
      }
      Object.assign(sanitizedDetails, nativeRes.native);
      if (Object.prototype.hasOwnProperty.call(detailsVal, "cooldown_until") && detailsVal.cooldown_until !== null) {
        const parsedCd = parseUtcIso(detailsVal.cooldown_until);
        if (!parsedCd) {
          return { ok: false, status: 400, error: "invalid_details_native", reason: "invalid_cooldown_until" };
        }
        sanitizedDetails.cooldown_until = parsedCd.iso;
      } else {
        sanitizedDetails.cooldown_until = null;
      }
    }

    sanitizedRoutes[provider] = {
      status: finalRouteStatus,
      error: finalRouteError,
      observed_at: observedAtIso,
      details: sanitizedDetails,
      windows: sanitizedWindows,
    };
  }

  const finalTopStatus: "ok" | "error" | "unknown" = raw.status;
  const finalTopError: "unavailable" | null =
    finalTopStatus === "error" ? "unavailable" : raw.error;

  return {
    ok: true,
    status: 200,
    snapshot: {
      schema_version: raw.schema_version,
      collector_version: collectorVersion,
      captured_at: capturedParsed.iso,
      captured_at_ms: capturedParsed.ms,
      host_id: expectedHostId,
      status: finalTopStatus,
      error: finalTopError,
      routes: sanitizedRoutes,
    },
  };
}

export class QuotaStore {
  private sql: SqlStorage;
  private ctx?: Pick<DurableObjectState, "storage">;
  private env: Env;
  private clockFn?: () => number;

  constructor(
    sql: SqlStorage,
    ctx?: Pick<DurableObjectState, "storage">,
    env?: Env,
    clockFn?: () => number
  ) {
    this.sql = sql;
    this.ctx = ctx;
    this.env = (env || {}) as Env;
    this.clockFn = clockFn;
    this.init();
  }

  public init(): void {
    for (const statement of QUOTA_SCHEMA_SQL.split(";")) {
      const trimmed = statement.trim();
      if (trimmed) {
        this.sql.exec(trimmed);
      }
    }
    ensureTbmQuotaSchema(this.sql);
    const cols = this.sql.exec("PRAGMA table_info(host_quota_snapshots)").toArray().map((c: any) => c.name);
    if (!cols.includes("collector_version")) {
      this.sql.exec("ALTER TABLE host_quota_snapshots ADD COLUMN collector_version TEXT");
    }
  }

  public setClock(fn?: () => number): void {
    this.clockFn = fn;
  }

  private now(): number {
    return this.clockFn ? this.clockFn() : Date.now();
  }

  private runInTransaction<T>(fn: () => T): T {
    const storageAny = this.ctx?.storage as any;
    if (storageAny && typeof storageAny.transactionSync === "function") {
      return storageAny.transactionSync(fn);
    }
    return fn();
  }

  private isRegisteredMachine(machineId: string): boolean {
    if (!machineId || typeof machineId !== "string") return false;
    const rows = this.sql.exec("SELECT id FROM machines WHERE id = ?", machineId).toArray();
    return rows.length > 0;
  }

  private async authenticateHostBearer(
    request: Request,
    nowMs: number
  ): Promise<{ machineId?: string; credId?: string; errorResponse?: Response }> {
    const authHeader = request.headers.get("Authorization");
    if (!authHeader || !authHeader.startsWith("Bearer ")) {
      return {
        errorResponse: jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        }),
      };
    }

    const token = authHeader.substring(7).trim();
    if (!token || !/^hubt_[A-Za-z0-9_-]+$/.test(token)) {
      return {
        errorResponse: jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        }),
      };
    }

    const tokenHash = await sha256(token);
    const cred = this.sql
      .exec("SELECT * FROM credentials WHERE token_hash = ?", tokenHash)
      .toArray()[0] as any;

    if (!cred || cred.revoked_at) {
      return {
        errorResponse: jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        }),
      };
    }

    const expiresMs = Date.parse(cred.expires_at);
    if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) {
      return {
        errorResponse: jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        }),
      };
    }

    if (cred.kind !== "host") {
      return {
        errorResponse: jsonResponse(
          { error: "forbidden", reason: "host_kind_required" },
          403
        ),
      };
    }

    let scopes: unknown;
    try {
      scopes = typeof cred.scopes === "string" ? JSON.parse(cred.scopes) : cred.scopes;
    } catch {
      return {
        errorResponse: jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        }),
      };
    }

    if (!Array.isArray(scopes) || scopes.includes("admin") || !scopes.includes("host")) {
      return {
        errorResponse: jsonResponse(
          { error: "forbidden", reason: "host_scope_required" },
          403
        ),
      };
    }

    if (typeof cred.machine_id !== "string" || !cred.machine_id.trim()) {
      return {
        errorResponse: jsonResponse(
          { error: "forbidden", reason: "machine_binding_required" },
          403
        ),
      };
    }

    if (!this.isRegisteredMachine(cred.machine_id)) {
      return {
        errorResponse: jsonResponse(
          { error: "forbidden", reason: "unregistered_machine" },
          403
        ),
      };
    }

    const lastUsedMs = cred.last_used_at ? Date.parse(cred.last_used_at) : 0;
    if (!lastUsedMs || isNaN(lastUsedMs) || nowMs - lastUsedMs >= 300_000) {
      this.sql.exec(
        "UPDATE credentials SET last_used_at = ? WHERE id = ?",
        new Date(nowMs).toISOString(),
        cred.id
      );
    }

    return { machineId: cred.machine_id, credId: cred.id };
  }

  public putSnapshot(
    hostId: string,
    rawPayload: unknown,
    nowMs: number = this.now()
  ): { ok: boolean; status: number; body: Record<string, any> } {
    const validation = validateQuotaSnapshotPayload(rawPayload, hostId, nowMs);
    if (!validation.ok || !validation.snapshot) {
      const errBody: Record<string, any> = { error: validation.error || "invalid_payload" };
      if (validation.reason) errBody.reason = validation.reason;
      return { ok: false, status: validation.status, body: errBody };
    }

    const snap = validation.snapshot;
    const receivedAtIso = new Date(nowMs).toISOString();

    return this.runInTransaction(() => {
      const existing = this.sql
        .exec(
          "SELECT captured_at_ms, routes_json FROM host_quota_snapshots WHERE host_id = ?",
          hostId
        )
        .toArray()[0] as any;

      if (existing && Number(existing.captured_at_ms) >= snap.captured_at_ms) {
        const isReplay = Number(existing.captured_at_ms) === snap.captured_at_ms;
        return {
          ok: false,
          status: 409,
          body: {
            error: "stale_or_replay_capture",
            reason: isReplay ? "replay_captured_at" : "out_of_order_captured_at",
          },
        };
      }

      // A newer observation that omits a cooldown deadline must not erase an
      // unexpired known hold: retain the stored deadline for that provider.
      if (existing?.routes_json) {
        let oldRoutes: Record<string, any> = {};
        try {
          oldRoutes = JSON.parse(existing.routes_json);
        } catch {
          oldRoutes = {};
        }
        for (const [provider, route] of Object.entries(snap.routes)) {
          const newCd = route.details?.cooldown_until ?? null;
          const oldCd = oldRoutes?.[provider]?.details?.cooldown_until ?? null;
          if ((newCd === null || newCd === undefined) && typeof oldCd === "string") {
            const oldMs = Date.parse(oldCd);
            if (Number.isFinite(oldMs) && oldMs > nowMs) {
              route.details.cooldown_until = oldCd;
            }
          }
        }
      }
      const routesJson = JSON.stringify(snap.routes);

      this.sql.exec(
        `INSERT INTO host_quota_snapshots (
          host_id, schema_version, collector_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_id) DO UPDATE SET
          schema_version = excluded.schema_version,
          collector_version = excluded.collector_version,
          captured_at = excluded.captured_at,
          captured_at_ms = excluded.captured_at_ms,
          received_at = excluded.received_at,
          received_at_ms = excluded.received_at_ms,
          status = excluded.status,
          error = excluded.error,
          routes_json = excluded.routes_json
        WHERE excluded.captured_at_ms > host_quota_snapshots.captured_at_ms`,
        hostId,
        snap.schema_version,
        snap.collector_version,
        snap.captured_at,
        snap.captured_at_ms,
        receivedAtIso,
        nowMs,
        snap.status,
        snap.error,
        routesJson
      );

      const ageMs = Math.max(0, nowMs - snap.captured_at_ms);
      const isStale = ageMs >= QUOTA_STALE_MS;

      return {
        ok: true,
        status: 200,
        body: {
          schema_version: snap.schema_version,
          ...(snap.schema_version === "1.1" ? { collector_version: snap.collector_version } : {}),
          captured_at: snap.captured_at,
          received_at: receivedAtIso,
          host_id: hostId,
          stale: isStale,
          status: isStale ? "stale" : snap.status,
          error: isStale ? "unavailable" : snap.error,
          routes: snap.routes,
        },
      };
    });
  }

  public getSnapshot(
    hostId: string,
    nowMs: number = this.now()
  ): { status: number; body: QuotaSnapshotDTO } {
    const row = this.sql
      .exec("SELECT * FROM host_quota_snapshots WHERE host_id = ?", hostId)
      .toArray()[0] as any;

    if (!row) {
      return {
        status: 404,
        body: {
          schema_version: "1.0",
          captured_at: null,
          received_at: null,
          host_id: hostId,
          stale: true,
          status: "unknown",
          error: "unavailable",
          routes: {},
        },
      };
    }

    let routes: Record<string, QuotaRouteDTO> = {};
    try {
      const parsed = typeof row.routes_json === "string" ? JSON.parse(row.routes_json) : row.routes_json;
      if (isPlainObject(parsed)) {
        routes = parsed as Record<string, QuotaRouteDTO>;
      }
    } catch {
      routes = {};
    }

    const capturedAtMs = Number(row.captured_at_ms);
    const receivedAtMs = Number(row.received_at_ms);
    const ageFromCapture = nowMs - capturedAtMs;
    const ageFromReceive = nowMs - receivedAtMs;
    const effectiveAgeMs = Math.max(ageFromCapture, ageFromReceive);

    const isStale =
      !Number.isFinite(effectiveAgeMs) ||
      effectiveAgeMs < 0 ||
      effectiveAgeMs >= QUOTA_STALE_MS;

    const isTopUnavailable = isStale || row.status !== "ok" || row.error === "unavailable";

    if (isTopUnavailable) {
      const degradedRoutes: Record<string, QuotaRouteDTO> = {};
      for (const [prov, r] of Object.entries(routes)) {
        degradedRoutes[prov] = {
          ...r,
          status: r.status === "unknown" ? "unknown" : "error",
          error: "unavailable",
        };
      }
      const storedVersion = row.schema_version === "1.1" ? "1.1" : "1.0";
      return {
        status: 503,
        body: {
          schema_version: storedVersion,
          ...(storedVersion === "1.1" ? { collector_version: row.collector_version ?? null } : {}),
          captured_at: row.captured_at,
          received_at: row.received_at,
          host_id: row.host_id,
          stale: isStale,
          status: isStale ? "stale" : row.status,
          error: "unavailable",
          routes: degradedRoutes,
        },
      };
    }

    {
      const storedVersion = row.schema_version === "1.1" ? "1.1" : "1.0";
      const normalizedRoutes: Record<string, QuotaRouteDTO> = {};
      for (const [prov, r] of Object.entries(routes)) {
        normalizedRoutes[prov] = {
          ...r,
          observed_at: r.observed_at || (r.status === "ok" ? row.captured_at : null),
        };
      }
      return {
        status: 200,
        body: {
          schema_version: storedVersion,
          ...(storedVersion === "1.1" ? { collector_version: row.collector_version ?? null } : {}),
          captured_at: row.captured_at,
          received_at: row.received_at,
          host_id: row.host_id,
          stale: false,
          status: "ok",
          error: null,
          routes: normalizedRoutes,
        },
      };
    }
  }

  public getFleetView(
    filters: FleetQuotaFilters,
    nowMs: number = this.now()
  ): { status: number; body: Record<string, any> } {
    const rawAccounts = dedupeAccountsByCanonical(
      this.sql.exec("SELECT * FROM fleet_accounts ORDER BY provider, label").toArray() as any[],
    );
    const serverRows = this.sql.exec("SELECT * FROM server_quota_snapshots ORDER BY account").toArray() as any[];
    // Stored rows keyed by old names (provider `codex-orin`, label
    // `urgent-muse`) canonicalize to the provider model: `codex-orin` is an
    // account under provider `codex`, the Go pool account is `shared-go`.
    for (const a of rawAccounts) a.provider = canonicalQuotaProvider(a.provider);
    for (const r of serverRows) { r.provider = canonicalQuotaProvider(r.provider); r.account = canonicalAccountLabel(r.account); }
    // One pool per canonical identity: a stale row under a legacy label must
    // never duplicate the live canonical row (registry wins ties; among
    // stored server rows a fresh captured_at beats a stale one, then newest
    // wins).
    const serverDeduped: any[] = [];
    {
      const byIdentity = new Map<string, any>();
      const rank = (row: any): [number, number] => [
        isFreshTs(row.captured_at, nowMs, QUOTA_STALE_MS) ? 1 : 0,
        Date.parse(row.captured_at),
      ];
      for (const row of serverRows) {
        const key = row.account + " " + row.provider;
        const prev = byIdentity.get(key);
        if (!prev) { byIdentity.set(key, row); continue; }
        const [pf, pt] = rank(prev);
        const [rf, rt] = rank(row);
        if (rf > pf || (rf === pf && rt > pt)) byIdentity.set(key, row);
      }
      serverDeduped.push(...byIdentity.values());
    }
    for (const row of serverDeduped) {
      if (!rawAccounts.some(a => canonicalAccountLabel(a.label) === row.account && canonicalQuotaProvider(a.provider) === row.provider)) {
        rawAccounts.push({ label: row.account, provider: row.provider });
      }
    }
    const knownProviders = new Set(rawAccounts.map((a: any) => a.provider));
    const knownLabels = new Set(rawAccounts.map((a: any) => a.label));
    const extraAccounts: any[] = [];
    const allRoutes = this.sql.exec("SELECT * FROM fleet_routes ORDER BY machine_id, engine").toArray() as any[];
    const machines = (this.sql.exec("SELECT id FROM machines ORDER BY id").toArray() as any[])
      .map((m: any) => m.id)
      .filter((id: string) => !filters.host_id || id === filters.host_id);

    for (const machineId of machines) {
      const snap: any = this.sql.exec("SELECT * FROM host_quota_snapshots WHERE host_id = ?", machineId).toArray()[0];
      if (!snap || !isFreshTs(snap.captured_at, nowMs, QUOTA_STALE_MS)) continue;
      let snapRoutes: any = null;
      try {
        snapRoutes = JSON.parse(snap.routes_json);
      } catch {
        snapRoutes = null;
      }
      if (!snapRoutes || typeof snapRoutes !== "object") continue;
      for (const prov of Object.keys(snapRoutes)) {
        if (!ALLOWED_QUOTA_PROVIDERS.has(prov)) continue;
        const canonical = canonicalQuotaProvider(prov);
        if (!knownProviders.has(canonical)) {
          knownProviders.add(canonical);
          if (!knownLabels.has(prov)) {
            knownLabels.add(prov);
            extraAccounts.push({ label: prov, provider: canonical, max_concurrency: 1, reserve_percent: 0, enabled: 1 });
          }
        } else if (!knownLabels.has(prov) && canonicalQuotaProvider(prov) !== prov) {
          // A legacy wire route key (e.g. `codex-orin`) whose canonical
          // provider is already registered still identifies an account the
          // registry has not named yet: surface it under its own label so
          // the second Codex home is never silently merged away.
          knownLabels.add(prov);
          extraAccounts.push({ label: prov, provider: canonical, max_concurrency: 1, reserve_percent: 0, enabled: 1 });
        }
      }
    }
    const accounts = [...rawAccounts, ...extraAccounts].filter(
      (a) => (!filters.provider || canonicalQuotaProvider(a.provider) === filters.provider) && (!filters.account || canonicalAccountLabel(a.label) === filters.account)
    );

    const pools: Record<string, any>[] = [];
    for (const account of accounts) {
      const routes = allRoutes.filter(
        (r) =>
          r.account === account.label &&
          (!filters.engine || r.engine === filters.engine) &&
          (!filters.host_id || r.machine_id === filters.host_id)
      );
      const freshObs: { machine: string; quota: any; kind: string }[] = [];
      let hasStale = false;
      for (const route of routes) {
        const sub: any = this.sql
          .exec("SELECT * FROM fleet_subscriptions WHERE machine_id = ? AND account = ?", route.machine_id, account.label)
          .toArray()[0];
        if (sub) {
          let quota: any = null;
          try {
            quota = JSON.parse(sub.quota_json);
          } catch {
            quota = null;
          }
          const quotaObs = quota?.observed_at || sub.observed_at;
          if (quota && isFreshTs(sub.observed_at, nowMs, QUOTA_STALE_MS) && isFreshTs(quotaObs, nowMs, QUOTA_STALE_MS)) {
            freshObs.push({ machine: route.machine_id, quota: quota.observed_at ? quota : { ...quota, observed_at: quotaObs }, kind: "subscription" });
          } else {
            hasStale = true;
          }
          continue;
        }
        // Single-account host snapshot fallback: same canonical rule as fleet observation().
        // A named profile that is not the provider itself cannot be read from
        // a default single-route CLI reading (it cannot address a named
        // profile or two accounts on one service).
        if (route.profile !== null && route.profile !== canonicalQuotaProvider(account.provider)) {
          hasStale = true;
          continue;
        }
        const countRow: any = this.sql
          .exec(
            "SELECT COUNT(DISTINCT r.account) AS n FROM fleet_routes r JOIN fleet_accounts a ON r.account = a.label WHERE r.machine_id = ? AND (a.provider = ? OR a.provider = ?)",
            route.machine_id,
            account.provider,
            legacyRouteKeyFor(account.provider) ?? account.provider,
          )
          .one();
        const snap: any = this.sql.exec("SELECT * FROM host_quota_snapshots WHERE host_id = ?", route.machine_id).toArray()[0];
        if (Number(countRow?.n) !== 1 || !snap || !isFreshTs(snap.captured_at, nowMs, QUOTA_STALE_MS)) {
          hasStale = true;
          continue;
        }
        let snapRoutes: any = null;
        try {
          snapRoutes = JSON.parse(snap.routes_json);
        } catch {
          snapRoutes = null;
        }
        const quota = snapshotRouteForAccount(account.label, account.provider, snapRoutes);
        const obsTs = quota?.observed_at || snap.captured_at;
        if (quota && isFreshTs(obsTs, nowMs, QUOTA_STALE_MS)) {
          freshObs.push({ machine: route.machine_id, quota: quota.observed_at ? quota : { ...quota, observed_at: obsTs }, kind: "snapshot" });
        } else {
          hasStale = true;
        }
      }

      if (freshObs.length === 0) {
        for (const machineId of machines) {
          const snap: any = this.sql.exec("SELECT * FROM host_quota_snapshots WHERE host_id = ?", machineId).toArray()[0];
          if (!snap || !isFreshTs(snap.captured_at, nowMs, QUOTA_STALE_MS)) continue;
          let snapRoutes: any = null;
          try {
            snapRoutes = JSON.parse(snap.routes_json);
          } catch {
            snapRoutes = null;
          }
          const quota = snapshotRouteForAccount(account.label, account.provider, snapRoutes);
          const obsTs = quota?.observed_at || snap.captured_at;
          if (quota && isFreshTs(obsTs, nowMs, QUOTA_STALE_MS)) {
            freshObs.push({ machine: machineId, quota: quota.observed_at ? quota : { ...quota, observed_at: obsTs }, kind: "snapshot" });
          }
        }
      }

      const central = serverDeduped.find(r => r.account === canonicalAccountLabel(account.label) && r.provider === canonicalQuotaProvider(account.provider));
      let source = "machine-fallback";
      if (central) {
        let quota: any = null;
        try { quota = JSON.parse(central.quota_json); } catch { /* unavailable */ }
        if (quota && isFreshTs(central.captured_at, nowMs, QUOTA_STALE_MS) &&
            (!quota.observed_at || isFreshTs(quota.observed_at, nowMs, QUOTA_STALE_MS))) {
          freshObs.splice(0, freshObs.length, { machine: "hub-server", quota, kind: "server" });
          source = "server";
        } else hasStale = true;
      }
      let state = "unknown";
      let remaining: number | null = null;
      let observedAt: string | null = null;
      let collectorVersion: string | null = null;
      if (freshObs.length) {
        state = "fresh";
        if (freshObs.some((o) => o.quota.details?.limit_reached === true)) {
          state = "exhausted";
        } else if (freshObs.some((o) => observationUnavailable(o.quota))) {
          state = "unavailable";
        } else {
          for (const o of freshObs) {
            const m = windowMinPercent(o.quota);
            if (m !== null) remaining = remaining === null ? m : Math.min(remaining, m);
          }
        }
        const times = freshObs.map((o) => o.quota.observed_at).filter((t: unknown) => typeof t === "string") as string[];
        if (times.length) observedAt = times.sort().pop()!;
        const cv = freshObs.map((o) => o.quota.collector_version).find((v: unknown) => typeof v === "string");
        if (typeof cv === "string") collectorVersion = cv;
      } else if (hasStale) {
        state = "stale";
      }
      const routeEngines = routes.length ? [...new Set(routes.map((r) => r.engine))].sort() : [canonicalQuotaProvider(account.provider)];
      pools.push({
        // Canonical provider model: the second Codex account surfaces as
        // provider `codex` (model provider OpenAI) with its own account, so
        // grouping by provider shows both Codex accounts together.
        provider: canonicalQuotaProvider(account.provider),
        account: canonicalAccountLabel(account.label),
        state,
        source,
        windows: freshObs[0]?.quota.windows || {},
        remaining_percent: remaining,
        observed_at: observedAt,
        collector_version: collectorVersion,
        observed_by_hosts: [...new Set(freshObs.map((o) => o.machine))].sort(),
        engines: routeEngines,
        // Every quota row exposes both the harness (engine) and the
        // provider/account pair: e.g. harness codex + provider codex +
        // account shared-codex/orin, harness codex + provider zai + account
        // shared-zai, harness opencode + provider go + account shared-go.
        harness: [...new Set(routeEngines.map((e) => canonicalHarness(e)))].sort(),
        profiles: routes.length ? [...new Set(routes.map((r) => r.profile).filter((p: unknown) => p !== null))].sort() : [],
      });
    }

    const hosts: Record<string, any>[] = [];
    const coverage = { machines_total: machines.length, fresh: [] as string[], stale: [] as string[], missing: [] as string[] };
    for (const machineId of machines) {
      const snap = this.getSnapshot(machineId, nowMs);
      const body = snap.body as Record<string, any>;
      if (filters.provider && body.routes) {
        body.routes = Object.fromEntries(Object.entries(body.routes).filter(([p]) => canonicalQuotaProvider(p) === filters.provider));
      }
      const subs = (this.sql.exec("SELECT * FROM fleet_subscriptions WHERE machine_id = ? ORDER BY service, account", machineId).toArray() as any[])
        .filter((s) => (!filters.provider || canonicalQuotaProvider(s.service) === filters.provider) && (!filters.account || canonicalAccountLabel(s.account) === filters.account))
        .map((s: any) => {
          let quota: any = null;
          try {
            quota = JSON.parse(s.quota_json);
          } catch {
            quota = null;
          }
          // Harness serving this subscription, from the machine's routes for
          // the account (canonicalized: native zcodex reads as harness codex).
          const subEngines = [...new Set(allRoutes
            .filter((r) => r.machine_id === s.machine_id && r.account === s.account)
            .map((r) => canonicalHarness(r.engine)))].sort();
          return { machine_id: s.machine_id, account: canonicalAccountLabel(s.account), service: canonicalQuotaProvider(s.service), plan: s.plan, observed_at: s.observed_at,
            stale: !isFreshTs(s.observed_at, nowMs, QUOTA_STALE_MS), quota, engines: subEngines };
        });
      hosts.push({ host_id: machineId, snapshot: body, subscriptions: subs });
      if (snap.status === 200 && body.status === "ok") coverage.fresh.push(machineId);
      else if (snap.status === 404) coverage.missing.push(machineId);
      else coverage.stale.push(machineId);
    }

    return {
      status: 200,
      body: {
        schema_version: "1.1",
        as_of: new Date(nowMs).toISOString(),
        filters,
        pools,
        hosts,
        coverage,
        // Canonical name mappings applied to this view: legacy wire route
        // keys and account labels (kept on ingest for backward compatibility
        // with stored rows and host snapshots) and their canonical
        // provider/harness/account names. UIs join per-window rows through it.
        canonical: {
          providers: { ...LEGACY_QUOTA_ROUTE_PROVIDERS },
          engines: { zcodex: "codex" },
          accounts: { ...LEGACY_ACCOUNT_LABELS },
        },
        account_coverage: { total: pools.length,
          fresh: pools.filter(p => p.state === "fresh" || p.state === "exhausted").map(p => p.account),
          stale: pools.filter(p => p.state === "stale").map(p => p.account),
          unavailable: pools.filter(p => p.state === "unavailable" || p.state === "unknown").map(p => p.account) },
      },
    };
  }

  /** Central account observations: no machine identity or host enrollment involved. */
  public async putServerSnapshot(request: Request): Promise<Response> {
    const session = await getSession(this.env, request);
    if (!session?.isAdmin) return jsonResponse({ error: "forbidden" }, 403);
    if (!verifyCsrf(request, session)) return jsonResponse({ error: "csrf_required" }, 403);
    if (request.method !== "POST") return jsonResponse({ error: "method_not_allowed" }, 405);
    const body = await readBoundedJsonBody(request, QUOTA_MAX_BODY_BYTES);
    if (body.errorStatus) return jsonResponse({ error: body.errorCode || "invalid_body" }, body.errorStatus);
    const data = body.data;
    if (!data || Object.keys(data).some(k => !["captured_at", "accounts"].includes(k)) ||
        !Array.isArray(data.accounts) || !data.accounts.length || data.accounts.length > 32) {
      return jsonResponse({ error: "invalid_accounts" }, 400);
    }
    const rows: { account: string; provider: string; quota: QuotaRouteDTO }[] = [];
    const seen = new Set<string>();
    for (const row of data.accounts) {
      if (!isPlainObject(row) || Object.keys(row).some(k => !["account", "provider", "quota"].includes(k)) ||
          typeof row.account !== "string" || !FLEET_LABEL.test(row.account)) {
        return jsonResponse({ error: "invalid_account" }, 400);
      }
      // Dedupe on the canonical account: `urgent-muse` and `shared-go` (or
      // a `codex-orin` route key under provider `codex`) are the same pool.
      const account = canonicalAccountLabel(row.account);
      if (seen.has(account)) return jsonResponse({ error: "invalid_account", reason: "duplicate_account" }, 400);
      seen.add(account);
      // Legacy names canonicalize on ingest: a `codex-orin` route key is the
      // second Codex account (provider `codex`), `urgent-muse` the Go pool
      // account (`shared-go`). Stored rows keyed by old names keep working.
      const provider = canonicalQuotaProvider(row.provider);
      const valid = validateQuotaSnapshotPayload({ schema_version: "1.1", collector_version: "hub-server/1.0",
        captured_at: data.captured_at, host_id: "hub-server", status: "ok", error: null,
        routes: { [provider]: row.quota } }, "hub-server", this.now());
      if (!valid.ok || !ALLOWED_QUOTA_PROVIDERS.has(row.provider)) return jsonResponse({ error: "invalid_quota" }, 400);
      const old: any = this.sql.exec("SELECT captured_at, quota_json FROM server_quota_snapshots WHERE account = ?", account).toArray()[0];
      if (old && Date.parse(old.captured_at) >= valid.snapshot!.captured_at_ms) return jsonResponse({ error: "older_snapshot" }, 409);
      const definition: any = this.sql.exec("SELECT provider FROM fleet_accounts WHERE label = ?", account).toArray()[0];
      if (definition && canonicalQuotaProvider(definition.provider) !== provider) return jsonResponse({ error: "provider_mismatch" }, 400);
      const quota = valid.snapshot!.routes[provider];
      if (old && quota.details.cooldown_until == null) {
        const deadline = JSON.parse(old.quota_json)?.details?.cooldown_until;
        if (typeof deadline === "string" && Date.parse(deadline) > this.now()) quota.details.cooldown_until = deadline;
      }
      rows.push({ account, provider, quota });
    }
    if (!this.ctx) return jsonResponse({ error: "storage_unavailable" }, 503);
    this.ctx.storage.transactionSync(() => {
      for (const row of rows) this.sql.exec("INSERT OR REPLACE INTO server_quota_snapshots VALUES (?, ?, ?, ?)",
        row.account, row.provider, data.captured_at, JSON.stringify(row.quota));
    });
    return jsonResponse({ status: "stored", accounts: rows.length });
  }

  public async handleRequest(request: Request, url?: URL, canonicalAuth?: CanonicalQuotaAuth): Promise<Response> {
    const reqUrl = url || new URL(request.url);
    const nowMs = this.now();

    if (request.method !== "GET" && request.method !== "POST") {
      return new Response(JSON.stringify({ error: "method_not_allowed" }), {
        status: 405,
        headers: {
          ...PRIVATE_NO_STORE_HEADERS,
          Allow: "GET, POST",
        },
      });
    }

    // Client-forged host identity headers are always denied. Allow proxy
    // X-Forwarded-Host only when it matches the request host or a configured
    // OIDC callback origin; reject spoofed host IDs or multi-host values.
    const fwdHost = request.headers.get("x-forwarded-host");
    let spoofedForwardedHost = false;
    if (fwdHost !== null) {
      const cleanFwd = fwdHost.trim().toLowerCase();
      if (!cleanFwd || cleanFwd.includes(",")) {
        spoofedForwardedHost = true;
      } else if (cleanFwd !== reqUrl.host.toLowerCase()) {
        spoofedForwardedHost = !allowedCallbackUrls(this.env).some((candidate) => {
          try {
            return new URL(candidate).host.toLowerCase() === cleanFwd;
          } catch {
            return false;
          }
        });
      }
    }
    if (request.headers.has("x-host-id") || request.headers.has("x-machine-id") || spoofedForwardedHost) {
      return jsonResponse(
        { error: "forbidden", reason: "header_spoof_denied" },
        403
      );
    }

    if (request.method === "POST") {
      const hasAuthHeader = request.headers.has("Authorization");
      if (!hasAuthHeader) {
        const session = await getSession(this.env, request);
        if (session) {
          return jsonResponse(
            { error: "forbidden", reason: "host_credential_required" },
            403
          );
        }
        return jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        });
      }

      const hostAuth = await this.authenticateHostBearer(request, nowMs);
      if (hostAuth.errorResponse) return hostAuth.errorResponse;
      const hostId = hostAuth.machineId!;

      if (reqUrl.searchParams.has("host_id") && reqUrl.searchParams.get("host_id") !== hostId) {
        return jsonResponse(
          { error: "forbidden", reason: "query_host_id_mismatch" },
          403
        );
      }

      const parsedBody = await readBoundedJsonBody(request, QUOTA_MAX_BODY_BYTES);
      if (parsedBody.errorStatus || !parsedBody.data) {
        return jsonResponse(
          { error: parsedBody.errorCode || "invalid_body" },
          parsedBody.errorStatus || 400
        );
      }

      const putRes = this.putSnapshot(hostId, parsedBody.data, nowMs);
      return jsonResponse(putRes.body, putRes.status);
    }

    // GET /api/quota
    const viewFleet = reqUrl.searchParams.get("view") === "fleet";
    if (viewFleet) {
      const parsed = parseFleetQuotaFilters(reqUrl);
      if (!parsed.ok || !parsed.filters) {
        return jsonResponse({ error: parsed.error || "invalid_filter" }, 400);
      }
      if (request.headers.has("Authorization")) {
        if (!canonicalAuth) {
          return jsonResponse({ error: "unauthorized" }, 401, {
            "WWW-Authenticate": "Bearer",
          });
        }
        const auth = await canonicalAuth(request);
        if (auth.error) return auth.error;
        if (!fleetReadGrant(auth.caller, auth.scopes)) {
          return jsonResponse({ error: "forbidden", reason: "fleet_view_denied" }, 403);
        }
        const fleetRes = this.getFleetView(parsed.filters, nowMs);
        return jsonResponse(fleetRes.body, fleetRes.status);
      }
      const fleetSession = await getSession(this.env, request);
      if (!fleetSession) {
        return jsonResponse({ error: "unauthorized" }, 401, {
          "WWW-Authenticate": "Bearer",
        });
      }
      if (!fleetSession.isAdmin) {
        return jsonResponse({ error: "forbidden" }, 403);
      }
      const fleetIntegrity = checkSessionReadIntegrity(request, reqUrl, fleetSession.csrfToken);
      if (fleetIntegrity) return fleetIntegrity;
      const fleetRes = this.getFleetView(parsed.filters, nowMs);
      return jsonResponse(fleetRes.body, fleetRes.status);
    }

    if (request.headers.has("Authorization")) {
      const hostAuth = await this.authenticateHostBearer(request, nowMs);
      if (hostAuth.errorResponse) return hostAuth.errorResponse;
      const hostId = hostAuth.machineId!;

      if (reqUrl.searchParams.has("host_id")) {
        const requestedHostId = reqUrl.searchParams.get("host_id");
        if (!requestedHostId || requestedHostId !== hostId) {
          return jsonResponse(
            { error: "forbidden", reason: "cross_machine_read_denied" },
            403
          );
        }
      }

      const getRes = this.getSnapshot(hostId, nowMs);
      return jsonResponse(getRes.body, getRes.status);
    }

    // Optional verified admin session with explicit ?host_id
    const session = await getSession(this.env, request);
    if (!session) {
      return jsonResponse({ error: "unauthorized" }, 401, {
        "WWW-Authenticate": "Bearer",
      });
    }
    if (!session.isAdmin) {
      return jsonResponse({ error: "forbidden" }, 403);
    }

    const integrity = checkSessionReadIntegrity(request, reqUrl, session.csrfToken);
    if (integrity) return integrity;

    const requestedHostId = reqUrl.searchParams.get("host_id");
    if (
      !requestedHostId ||
      !requestedHostId.trim() ||
      requestedHostId.length > 128 ||
      !/^[A-Za-z0-9_.:-]{1,128}$/.test(requestedHostId)
    ) {
      return jsonResponse(
        { error: "missing_host_id", reason: "explicit_host_id_required" },
        400
      );
    }

    if (!this.isRegisteredMachine(requestedHostId)) {
      return jsonResponse(
        {
          schema_version: "1.0",
          captured_at: null,
          received_at: null,
          host_id: requestedHostId,
          stale: true,
          status: "unknown",
          error: "unavailable",
          routes: {},
        },
        404
      );
    }

    const getRes = this.getSnapshot(requestedHostId, nowMs);
    return jsonResponse(getRes.body, getRes.status);
  }
}

export const TSQuotaStore = QuotaStore;

export async function handleQuotaRoutes(
  request: Request,
  path: string,
  quotaStore: QuotaStore,
  url?: URL,
  canonicalAuth?: CanonicalQuotaAuth
): Promise<Response | null> {
  if (path === "/api/quota/server") return quotaStore.putServerSnapshot(request);
  if (path !== "/api/quota") return null;
  return quotaStore.handleRequest(request, url, canonicalAuth);
}
