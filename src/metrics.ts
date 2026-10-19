import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from "./env";
import { sha256 } from "./auth/crypto";
import { getSession, verifyCsrf } from "./auth/session";

export const METRICS_SCHEMA_VERSION = "1.0";
export const METRICS_MAX_BODY_BYTES = 32_768;
export const METRICS_STALE_MS = 7_200_000;
export const METRICS_PER_HOST_ROW_CAP = 8;
export const METRICS_GLOBAL_ROW_CAP = 64;
export const METRICS_GLOBAL_LATEST_ROW_CAP = 64;
export const METRICS_PER_HOST_BYTE_CAP = 262_144;
export const METRICS_GLOBAL_BYTE_CAP = 2_097_152;
export const METRICS_PAGE_MAX = 8;
export const HOUR_MS = 3_600_000;
export const WINDOW_HOURS = 24;
export const REQUEST_KEY_RE = /^[A-Za-z0-9._:-]{8,128}$/;

/** Live host-observation gate injected by Hub (M9 integration).
 * Bound to SessionControlService.hostObservationAllowed: fresh machine
 * family + approval authority, never JSON owner flags. Undefined fails closed. */
export type MetricsHostGate = (
  machineId: unknown,
  caller: { kind?: string; machineId?: string; tokenId?: string }
) => boolean;

export const CANONICAL_PROJECT_IDS = [
  "agent-branches",
  "agent-dashboard",
  "quota-launcher",
  "agent-coordination",
] as const;

export const OPTIONAL_PROJECT_IDS = new Set<string>(["unattributed"]);
export const ALLOWED_PROJECT_IDS = new Set<string>([...CANONICAL_PROJECT_IDS, "unattributed"]);

export const COVERAGE_ENUM = new Set(["observed", "unknown", "missing", "unsupported", "partial"]);
export const GAP_CODES = new Set([
  "missing",
  "unsupported_project",
  "unattributed",
  "partial_coverage",
  "producer_unknown",
  "empty_window",
]);

export const METRICS_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS host_metrics_latest (
  host_id TEXT PRIMARY KEY,
  as_of TEXT NOT NULL,
  as_of_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  received_at_ms INTEGER NOT NULL,
  request_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  credential_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS host_metrics_idempotency (
  host_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  as_of_ms INTEGER NOT NULL,
  received_at_ms INTEGER NOT NULL,
  received_at TEXT,
  credential_id TEXT NOT NULL,
  payload_bytes INTEGER NOT NULL,
  payload_json TEXT,
  PRIMARY KEY (host_id, request_key)
);
CREATE INDEX IF NOT EXISTS host_metrics_idempotency_received
  ON host_metrics_idempotency (received_at_ms, host_id);
`;

const TOP_LEVEL_KEYS = new Set([
  "schema_version",
  "as_of",
  "host_id",
  "request_key",
  "coverage",
  "gaps",
  "utilization",
  "usage",
  "accepted",
]);

const COVERAGE_KEYS = new Set(["utilization", "usage", "accepted"]);
const GAP_KEYS = new Set(["code"]);
const UTIL_KEYS = new Set(["unknown", "projects"]);
const UTIL_PROJECT_KEYS = new Set([
  "unknown",
  "unique_agents",
  "total_agent_hours",
  "coverage",
  "unattributed_agent_hours",
  "hourly_buckets",
]);
const BUCKET_KEYS = new Set(["bucket_start", "bucket_end", "active_agents", "agent_hours"]);
const USAGE_KEYS = new Set(["unknown", "projects"]);
const USAGE_PROJECT_KEYS = new Set([
  "unknown",
  "total_input_tokens",
  "total_output_tokens",
  "total_cache_read_tokens",
  "total_cache_creation_tokens",
  "total_reasoning_tokens",
  "total_noncache_input_tokens",
  "has_unknown_tokens",
  "has_unknown_cache",
  "has_unknown_reasoning",
  "cache_coverage",
  "reasoning_coverage",
  "event_count",
]);
const ACCEPTED_KEYS = new Set([
  "unknown",
  "accepted_outcomes",
  "task_count",
  "commit_count",
  "review_count",
  "projects",
]);
const ACCEPTED_PROJECT_KEYS = new Set([
  "unknown",
  "accepted_outcomes",
  "task_count",
  "commit_count",
  "review_count",
]);

const PRIVATE_KEY_RE =
  /^(path|paths|prompt|prompts|text|content|summary|feature_summary|user_content|session_id|session_uuid|conversation_id|credential|credentials|token|account|accounts|address|addresses|email|raw|transcript|transcripts|quota|percent_remaining|cost|money|file|filename|source_path|owner_tag|reviewer|pr_url|commit|sha|total_quota_delta_percent|total_cost|quota_delta|shared_agent_ids|__proto__|constructor|prototype)$/i;

const PRIVATE_NO_STORE_HEADERS: Record<string, string> = {
  "Content-Type": "application/json",
  "Cache-Control": "private, no-store",
};

function jsonResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: PRIVATE_NO_STORE_HEADERS });
}

function isPlainObject(val: unknown): val is Record<string, unknown> {
  if (typeof val !== "object" || val === null || Array.isArray(val)) return false;
  const proto = Object.getPrototypeOf(val);
  return proto === Object.prototype || proto === null;
}

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
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59) return null;
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

function finiteNonNegative(val: unknown, integer: boolean): number | null | "bad" {
  if (val === null) return null;
  if (typeof val === "boolean") return "bad";
  if (typeof val !== "number" || !Number.isFinite(val) || val < 0) return "bad";
  if (integer && !Number.isInteger(val)) return "bad";
  return val;
}

function finiteUnitInterval(val: unknown): number | null | "bad" {
  if (val === null) return null;
  if (typeof val === "boolean") return "bad";
  if (typeof val !== "number" || !Number.isFinite(val) || val < 0 || val > 1) return "bad";
  return val;
}

function strictBool(val: unknown): boolean | "bad" {
  return typeof val === "boolean" ? val : "bad";
}

function scanPrivateKeys(val: unknown): boolean {
  if (Array.isArray(val)) return val.some(scanPrivateKeys);
  if (!isPlainObject(val)) return false;
  for (const [k, v] of Object.entries(val)) {
    if (PRIVATE_KEY_RE.test(k) || k === "__proto__" || k === "constructor" || k === "prototype") return true;
    if (scanPrivateKeys(v)) return true;
  }
  return false;
}

function unknownKeys(obj: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(obj).some((k) => !allowed.has(k));
}

async function readBoundedJsonBody(
  request: Request,
  maxBytes: number = METRICS_MAX_BODY_BYTES
): Promise<{ data?: Record<string, unknown>; errorStatus?: number; errorCode?: string }> {
  const contentLengthHeader = request.headers.get("content-length");
  if (contentLengthHeader !== null && contentLengthHeader.trim() !== "") {
    const declared = Number(contentLengthHeader);
    if (!Number.isFinite(declared) || declared < 0) return { errorStatus: 400, errorCode: "invalid_content_length" };
    if (declared > maxBytes) return { errorStatus: 413, errorCode: "body_too_large" };
  }
  if (!request.body) return { errorStatus: 400, errorCode: "missing_body" };
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
  if (totalBytes === 0) return { errorStatus: 400, errorCode: "missing_body" };
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
  if (/"__proto__"\s*:/.test(text)) return { errorStatus: 400, errorCode: "unknown_field" };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { errorStatus: 400, errorCode: "invalid_json" };
  }
  if (!isPlainObject(parsed)) return { errorStatus: 400, errorCode: "invalid_json_object" };
  return { data: parsed };
}

export interface MetricsSnapshot {
  schema_version: "1.0";
  as_of: string;
  as_of_ms: number;
  host_id: string;
  request_key: string;
  coverage: { utilization: string; usage: string; accepted: string };
  gaps: Array<{ code: string }>;
  utilization: Record<string, unknown>;
  usage: Record<string, unknown>;
  accepted: Record<string, unknown>;
}

function unknownProject(kind: "utilization" | "usage" | "accepted"): Record<string, unknown> {
  if (kind === "utilization") {
    return {
      unknown: true,
      unique_agents: null,
      total_agent_hours: null,
      coverage: null,
      unattributed_agent_hours: null,
      hourly_buckets: [],
    };
  }
  if (kind === "usage") {
    return {
      unknown: true,
      total_input_tokens: null,
      total_output_tokens: null,
      total_cache_read_tokens: null,
      total_cache_creation_tokens: null,
      total_reasoning_tokens: null,
      total_noncache_input_tokens: null,
      has_unknown_tokens: true,
      has_unknown_cache: true,
      has_unknown_reasoning: true,
      cache_coverage: null,
      reasoning_coverage: null,
      event_count: 0,
    };
  }
  return {
    unknown: true,
    accepted_outcomes: null,
    task_count: null,
    commit_count: null,
    review_count: null,
  };
}

function emptyBuckets(asOfMs: number): Array<Record<string, unknown>> {
  const start = asOfMs - WINDOW_HOURS * HOUR_MS;
  const buckets: Array<Record<string, unknown>> = [];
  for (let i = 0; i < WINDOW_HOURS; i++) {
    const bStart = start + i * HOUR_MS;
    buckets.push({
      bucket_start: new Date(bStart).toISOString(),
      bucket_end: new Date(bStart + HOUR_MS).toISOString(),
      active_agents: null,
      agent_hours: null,
    });
  }
  return buckets;
}

function validateBuckets(raw: unknown, asOfMs: number, unknown: boolean): Array<Record<string, unknown>> | null {
  if (!Array.isArray(raw) || raw.length !== WINDOW_HOURS) return null;
  const windowStart = asOfMs - WINDOW_HOURS * HOUR_MS;
  const out: Array<Record<string, unknown>> = [];
  for (let i = 0; i < WINDOW_HOURS; i++) {
    const row = raw[i];
    if (!isPlainObject(row) || unknownKeys(row, BUCKET_KEYS) || scanPrivateKeys(row)) return null;
    const start = parseUtcIso(row.bucket_start);
    const end = parseUtcIso(row.bucket_end);
    if (!start || !end) return null;
    if (start.ms !== windowStart + i * HOUR_MS) return null;
    if (end.ms !== start.ms + HOUR_MS) return null;
    if (i === WINDOW_HOURS - 1 && end.ms !== asOfMs) return null;
    const active = finiteNonNegative(row.active_agents, true);
    const hours = finiteNonNegative(row.agent_hours, false);
    if (active === "bad" || hours === "bad") return null;
    if (unknown && (active !== null || hours !== null)) return null;
    if (!unknown && (active === null || hours === null)) return null;
    out.push({
      bucket_start: start.iso,
      bucket_end: end.iso,
      active_agents: active,
      agent_hours: hours,
    });
  }
  return out;
}

function validateUtilProject(raw: unknown, asOfMs: number): Record<string, unknown> | null {
  if (!isPlainObject(raw) || unknownKeys(raw, UTIL_PROJECT_KEYS) || scanPrivateKeys(raw)) return null;
  const unknown = strictBool(raw.unknown);
  if (unknown === "bad") return null;
  const unique = finiteNonNegative(raw.unique_agents, true);
  const hours = finiteNonNegative(raw.total_agent_hours, false);
  const coverage = finiteUnitInterval(raw.coverage);
  const unattr = finiteNonNegative(raw.unattributed_agent_hours, false);
  if (unique === "bad" || hours === "bad" || coverage === "bad" || unattr === "bad") return null;
  if (unknown) {
    if (unique !== null || hours !== null || coverage !== null || unattr !== null) return null;
    const buckets = Array.isArray(raw.hourly_buckets) && raw.hourly_buckets.length
      ? validateBuckets(raw.hourly_buckets, asOfMs, true)
      : emptyBuckets(asOfMs);
    if (!buckets) return null;
    return {
      unknown: true,
      unique_agents: null,
      total_agent_hours: null,
      coverage: null,
      unattributed_agent_hours: null,
      hourly_buckets: buckets,
    };
  }
  if (unique === null || hours === null || coverage === null || unattr === null) return null;
  const buckets = validateBuckets(raw.hourly_buckets, asOfMs, false);
  if (!buckets) return null;
  return {
    unknown: false,
    unique_agents: unique,
    total_agent_hours: hours,
    coverage,
    unattributed_agent_hours: unattr,
    hourly_buckets: buckets,
  };
}

function validateUsageProject(raw: unknown): Record<string, unknown> | null {
  if (!isPlainObject(raw) || unknownKeys(raw, USAGE_PROJECT_KEYS) || scanPrivateKeys(raw)) return null;
  const unknown = strictBool(raw.unknown);
  if (unknown === "bad") return null;
  const ints: Record<string, number | null> = {};
  for (const key of [
    "total_input_tokens",
    "total_output_tokens",
    "total_cache_read_tokens",
    "total_cache_creation_tokens",
    "total_reasoning_tokens",
    "total_noncache_input_tokens",
    "event_count",
  ]) {
    const v = finiteNonNegative(raw[key], true);
    if (v === "bad") return null;
    ints[key] = v;
  }
  const flags: Record<string, boolean> = {};
  for (const key of ["has_unknown_tokens", "has_unknown_cache", "has_unknown_reasoning"]) {
    const v = strictBool(raw[key]);
    if (v === "bad") return null;
    flags[key] = v;
  }
  const cacheCov = finiteUnitInterval(raw.cache_coverage);
  const reasonCov = finiteUnitInterval(raw.reasoning_coverage);
  if (cacheCov === "bad" || reasonCov === "bad") return null;
  if (unknown) {
    if (
      ints.total_input_tokens !== null ||
      ints.total_output_tokens !== null ||
      ints.total_cache_read_tokens !== null ||
      ints.total_cache_creation_tokens !== null ||
      ints.total_reasoning_tokens !== null ||
      ints.total_noncache_input_tokens !== null
    ) {
      return null;
    }
  }
  return {
    unknown,
    ...ints,
    ...flags,
    cache_coverage: cacheCov,
    reasoning_coverage: reasonCov,
  };
}

function validateAcceptedProject(raw: unknown): Record<string, unknown> | null {
  if (!isPlainObject(raw) || unknownKeys(raw, ACCEPTED_PROJECT_KEYS) || scanPrivateKeys(raw)) return null;
  const unknown = strictBool(raw.unknown);
  if (unknown === "bad") return null;
  const accepted = finiteNonNegative(raw.accepted_outcomes, true);
  const tasks = finiteNonNegative(raw.task_count, true);
  const commits = finiteNonNegative(raw.commit_count, true);
  const reviews = finiteNonNegative(raw.review_count, true);
  if (accepted === "bad" || tasks === "bad" || commits === "bad" || reviews === "bad") return null;
  if (unknown && (accepted !== null || tasks !== null || commits !== null || reviews !== null)) return null;
  // F3: unknown=false keeps known outcomes/tasks/commits; review_count may stay
  // null when no agreed review-event identity exists. Do not force reviews non-null.
  if (!unknown && (accepted === null || tasks === null || commits === null)) return null;
  return {
    unknown,
    accepted_outcomes: accepted,
    task_count: tasks,
    commit_count: commits,
    review_count: reviews,
  };
}

function validateProjectMap(
  raw: unknown,
  asOfMs: number,
  kind: "utilization" | "usage" | "accepted"
): Record<string, Record<string, unknown>> | null {
  if (!isPlainObject(raw) || scanPrivateKeys(raw)) return null;
  const out: Record<string, Record<string, unknown>> = {};
  for (const [proj, value] of Object.entries(raw)) {
    if (!ALLOWED_PROJECT_IDS.has(proj)) return null;
    let parsed: Record<string, unknown> | null = null;
    if (kind === "utilization") parsed = validateUtilProject(value, asOfMs);
    else if (kind === "usage") parsed = validateUsageProject(value);
    else parsed = validateAcceptedProject(value);
    if (!parsed) return null;
    out[proj] = parsed;
  }
  for (const id of CANONICAL_PROJECT_IDS) {
    if (!out[id]) {
      const filled = unknownProject(kind);
      if (kind === "utilization") filled.hourly_buckets = emptyBuckets(asOfMs);
      out[id] = filled;
    }
  }
  return out;
}

export function validateMetricsSnapshotPayload(
  rawPayload: unknown,
  expectedHostId: string,
  nowMs: number
): { ok: boolean; status: number; error?: string; reason?: string; snapshot?: MetricsSnapshot } {
  if (!isPlainObject(rawPayload)) return { ok: false, status: 400, error: "invalid_json_object" };
  if (scanPrivateKeys(rawPayload) || unknownKeys(rawPayload, TOP_LEVEL_KEYS)) {
    return { ok: false, status: 400, error: "unknown_field" };
  }
  if (rawPayload.schema_version !== METRICS_SCHEMA_VERSION) {
    return { ok: false, status: 400, error: "invalid_schema_version" };
  }
  if (typeof rawPayload.request_key !== "string" || !REQUEST_KEY_RE.test(rawPayload.request_key)) {
    return { ok: false, status: 400, error: "invalid_request_key" };
  }
  if (rawPayload.host_id !== undefined && rawPayload.host_id !== expectedHostId) {
    return { ok: false, status: 403, error: "forbidden", reason: "host_id_mismatch" };
  }
  const asOf = parseUtcIso(rawPayload.as_of);
  if (!asOf) return { ok: false, status: 400, error: "invalid_as_of" };
  if (asOf.ms > nowMs) return { ok: false, status: 400, error: "future_as_of" };

  if (!isPlainObject(rawPayload.coverage) || unknownKeys(rawPayload.coverage, COVERAGE_KEYS) || scanPrivateKeys(rawPayload.coverage)) {
    return { ok: false, status: 400, error: "invalid_coverage" };
  }
  const utilizationCov = rawPayload.coverage.utilization;
  const usageCov = rawPayload.coverage.usage;
  const acceptedCov = rawPayload.coverage.accepted;
  if (
    typeof utilizationCov !== "string" ||
    typeof usageCov !== "string" ||
    typeof acceptedCov !== "string" ||
    !COVERAGE_ENUM.has(utilizationCov) ||
    !COVERAGE_ENUM.has(usageCov) ||
    !COVERAGE_ENUM.has(acceptedCov)
  ) {
    return { ok: false, status: 400, error: "invalid_coverage" };
  }
  const coverage = {
    utilization: utilizationCov,
    usage: usageCov,
    accepted: acceptedCov,
  };

  if (!Array.isArray(rawPayload.gaps) || rawPayload.gaps.length > 16) {
    return { ok: false, status: 400, error: "invalid_gaps" };
  }
  const gaps: Array<{ code: string }> = [];
  for (const g of rawPayload.gaps) {
    if (!isPlainObject(g) || unknownKeys(g, GAP_KEYS) || scanPrivateKeys(g)) {
      return { ok: false, status: 400, error: "invalid_gaps" };
    }
    if (typeof g.code !== "string" || !GAP_CODES.has(g.code)) {
      return { ok: false, status: 400, error: "invalid_gaps" };
    }
    gaps.push({ code: g.code });
  }

  if (!isPlainObject(rawPayload.utilization) || unknownKeys(rawPayload.utilization, UTIL_KEYS) || scanPrivateKeys(rawPayload.utilization)) {
    return { ok: false, status: 400, error: "invalid_utilization" };
  }
  const utilUnknown = strictBool(rawPayload.utilization.unknown);
  if (utilUnknown === "bad") return { ok: false, status: 400, error: "invalid_utilization" };
  const utilProjects = validateProjectMap(rawPayload.utilization.projects, asOf.ms, "utilization");
  if (!utilProjects) return { ok: false, status: 400, error: "invalid_utilization" };

  if (!isPlainObject(rawPayload.usage) || unknownKeys(rawPayload.usage, USAGE_KEYS) || scanPrivateKeys(rawPayload.usage)) {
    return { ok: false, status: 400, error: "invalid_usage" };
  }
  const usageUnknown = strictBool(rawPayload.usage.unknown);
  if (usageUnknown === "bad") return { ok: false, status: 400, error: "invalid_usage" };
  const usageProjects = validateProjectMap(rawPayload.usage.projects, asOf.ms, "usage");
  if (!usageProjects) return { ok: false, status: 400, error: "invalid_usage" };

  if (!isPlainObject(rawPayload.accepted) || unknownKeys(rawPayload.accepted, ACCEPTED_KEYS) || scanPrivateKeys(rawPayload.accepted)) {
    return { ok: false, status: 400, error: "invalid_accepted" };
  }
  const accUnknown = strictBool(rawPayload.accepted.unknown);
  if (accUnknown === "bad") return { ok: false, status: 400, error: "invalid_accepted" };
  const accOutcomes = finiteNonNegative(rawPayload.accepted.accepted_outcomes, true);
  const accTasks = finiteNonNegative(rawPayload.accepted.task_count, true);
  const accCommits = finiteNonNegative(rawPayload.accepted.commit_count, true);
  const accReviews = finiteNonNegative(rawPayload.accepted.review_count, true);
  if (accOutcomes === "bad" || accTasks === "bad" || accCommits === "bad" || accReviews === "bad") {
    return { ok: false, status: 400, error: "invalid_accepted" };
  }
  if (accUnknown && (accOutcomes !== null || accTasks !== null || accCommits !== null || accReviews !== null)) {
    return { ok: false, status: 400, error: "invalid_accepted" };
  }
  const acceptedProjects = validateProjectMap(rawPayload.accepted.projects ?? {}, asOf.ms, "accepted");
  if (!acceptedProjects) return { ok: false, status: 400, error: "invalid_accepted" };

  return {
    ok: true,
    status: 200,
    snapshot: {
      schema_version: "1.0",
      as_of: asOf.iso,
      as_of_ms: asOf.ms,
      host_id: expectedHostId,
      request_key: rawPayload.request_key,
      coverage,
      gaps,
      utilization: { unknown: utilUnknown, projects: utilProjects },
      usage: { unknown: usageUnknown, projects: usageProjects },
      accepted: {
        unknown: accUnknown,
        accepted_outcomes: accOutcomes,
        task_count: accTasks,
        commit_count: accCommits,
        review_count: accReviews,
        projects: acceptedProjects,
      },
    },
  };
}

export function publicSnapshotBody(
  snap: MetricsSnapshot,
  receivedAt: string,
  nowMs: number,
  asOfMs: number,
  receivedAtMs: number
): Record<string, unknown> {
  const age = Math.max(nowMs - asOfMs, nowMs - receivedAtMs);
  const stale = !Number.isFinite(age) || age < 0 || age >= METRICS_STALE_MS;
  return {
    schema_version: "1.0",
    host_id: snap.host_id,
    as_of: snap.as_of,
    received_at: receivedAt,
    request_key: snap.request_key,
    stale,
    status: stale ? "stale" : "ok",
    error: stale ? "unavailable" : null,
    coverage: snap.coverage,
    gaps: snap.gaps,
    utilization: snap.utilization,
    usage: snap.usage,
    accepted: snap.accepted,
  };
}

export class MetricsStore {
  private sql: SqlStorage;
  private ctx?: Pick<DurableObjectState, "storage">;
  private env: Env;
  private clockFn?: () => number;
  private gate?: MetricsHostGate;

  constructor(sql: SqlStorage, ctx?: Pick<DurableObjectState, "storage">, env?: Env, clockFn?: () => number) {
    this.sql = sql;
    this.ctx = ctx;
    this.env = (env || {}) as Env;
    this.clockFn = clockFn;
    this.init();
  }

  public init(): void {
    for (const statement of METRICS_SCHEMA_SQL.split(";")) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }
    const cols = new Set(
      (this.sql.exec("PRAGMA table_info(host_metrics_idempotency)").toArray() as Array<{ name?: string }>).map((c) =>
        String(c.name || "")
      )
    );
    if (cols.size > 0 && !cols.has("payload_json")) {
      this.sql.exec("ALTER TABLE host_metrics_idempotency ADD COLUMN payload_json TEXT");
    }
    if (cols.size > 0 && !cols.has("received_at")) {
      this.sql.exec("ALTER TABLE host_metrics_idempotency ADD COLUMN received_at TEXT");
    }
  }

  public setClock(fn?: () => number): void {
    this.clockFn = fn;
  }

  /** Hub injects live SessionControlService.hostObservationAllowed here
   * (or undefined while absent). Never duplicated, never trusted blindly.
   * Write/read host transport fails closed while absent. */
  public setObservationGate(gate: MetricsHostGate | undefined): void {
    this.gate = gate;
  }

  private now(): number {
    return this.clockFn ? this.clockFn() : Date.now();
  }

  private runInTransaction<T>(fn: () => T): T {
    const storageAny = this.ctx?.storage as { transactionSync?: <R>(fn: () => R) => R } | undefined;
    if (storageAny && typeof storageAny.transactionSync === "function") {
      return storageAny.transactionSync(fn);
    }
    return fn();
  }

  private liveMachine(machineId: string, credentialId: string): boolean {
    const row = this.sql
      .exec("SELECT id, credential_id FROM machines WHERE id = ?", machineId)
      .toArray()[0] as { id?: string; credential_id?: string } | undefined;
    if (!row || row.id !== machineId) return false;
    if (typeof row.credential_id !== "string" || !row.credential_id) return false;
    return row.credential_id === credentialId;
  }

  private authorizeCurrentHostCredential(
    credentialId: string,
    machineId: string,
    nowMs: number
  ): { ok: boolean; status: number; error: string; reason?: string } {
    const cred = this.sql.exec("SELECT * FROM credentials WHERE id = ?", credentialId).toArray()[0] as any;
    if (!cred) return { ok: false, status: 401, error: "unauthorized", reason: "credential_missing" };
    if (cred.revoked_at) return { ok: false, status: 401, error: "unauthorized", reason: "revoked" };
    const expiresMs = Date.parse(cred.expires_at);
    if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) {
      return { ok: false, status: 401, error: "unauthorized", reason: "expired" };
    }
    if (cred.kind !== "host") {
      return { ok: false, status: 403, error: "forbidden", reason: "host_kind_required" };
    }
    let scopes: unknown;
    try {
      scopes = typeof cred.scopes === "string" ? JSON.parse(cred.scopes) : cred.scopes;
    } catch {
      return { ok: false, status: 401, error: "unauthorized", reason: "invalid_scopes" };
    }
    if (!Array.isArray(scopes) || scopes.includes("admin") || !scopes.includes("host")) {
      return { ok: false, status: 403, error: "forbidden", reason: "host_scope_required" };
    }
    if (typeof cred.machine_id !== "string" || cred.machine_id !== machineId) {
      return { ok: false, status: 403, error: "forbidden", reason: "machine_binding_required" };
    }
    if (!this.liveMachine(machineId, credentialId)) {
      return { ok: false, status: 403, error: "forbidden", reason: "machine_credential_mismatch" };
    }
    return { ok: true, status: 200, error: "ok" };
  }

  private storedCredentialHonored(hostId: string, credentialId: string, nowMs: number): boolean {
    return this.authorizeCurrentHostCredential(credentialId, hostId, nowMs).ok;
  }

  /** Bound-owner exemption mirroring register (registry.ts): the currently
   * bound owner keeps transport while a later approval is still pending.
   * Approval witness and bound owner are distinct until a new valid issuance
   * actually replaces the binding. Family continuity is still required:
   * a superseded pre-bind family never qualifies. Server-side rows only;
   * JSON flags never authorize. */
  private boundOwnerWithLiveFamily(hostId: string, credentialId: string, nowMs: number): boolean {
    try {
      const m: any = this.sql.exec(
        'SELECT m.credential_id, c.family_id AS machine_family FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?',
        hostId
      ).toArray()[0];
      if (!m || m.credential_id !== credentialId) return false;
      const presenter: any = this.sql.exec('SELECT family_id, revoked_at, expires_at FROM credentials WHERE id = ?', credentialId).toArray()[0];
      if (!presenter || presenter.revoked_at) return false;
      const expires = Date.parse(presenter.expires_at);
      if (!Number.isFinite(expires) || expires <= nowMs) return false;
      const family = typeof presenter.family_id === 'string' && presenter.family_id ? presenter.family_id : null;
      if (!family) return false;
      if (m.machine_family && m.machine_family !== family) return false;
      return true;
    } catch {
      return false;
    }
  }

  /** Live gate check: injected hostObservationAllowed first; on denial the
   * bound owner with live family keeps transport (pending-approval case).
   * Anything else fails closed with scoped retirement by the caller. */
  private gateAllows(hostId: string, credentialId: string, nowMs: number): boolean {
    if (typeof this.gate !== 'function') return false;
    try {
      if (this.gate(hostId, { kind: 'host', machineId: hostId, tokenId: credentialId })) return true;
    } catch {
      return false;
    }
    return this.boundOwnerWithLiveFamily(hostId, credentialId, nowMs);
  }

  private purgeHost(hostId: string): void {
    this.sql.exec("DELETE FROM host_metrics_latest WHERE host_id = ?", hostId);
    this.sql.exec("DELETE FROM host_metrics_idempotency WHERE host_id = ?", hostId);
  }

  // R4 F1a: a denied stale caller is never permission to purge a valid
  // successor's rows. Retire only rows still bound to the caller's own
  // invalid/displaced credential; rows bound to another (current) credential
  // are left untouched. Status/expiry/kind/scope/revocation/family checks
  // are unchanged; only the deletion scope is constrained.
  private retireCallerBinding(hostId: string, callerCredentialId: string, nowMs: number): void {
    if (this.storedCredentialHonored(hostId, callerCredentialId, nowMs)) return;
    this.sql.exec("DELETE FROM host_metrics_latest WHERE host_id = ? AND credential_id = ?", hostId, callerCredentialId);
    this.sql.exec(
      "DELETE FROM host_metrics_idempotency WHERE host_id = ? AND credential_id = ?",
      hostId,
      callerCredentialId
    );
  }

  // R4 F1a: retire only stored rows whose own binding is invalid/displaced.
  // A host holding a valid current latest keeps it even when older receipts
  // bound to superseded credentials are retired.
  private retireInvalidStoredRows(hostId: string, nowMs: number): void {
    const latest = this.sql
      .exec("SELECT credential_id FROM host_metrics_latest WHERE host_id = ?", hostId)
      .toArray()[0] as { credential_id?: unknown } | undefined;
    if (latest && typeof latest.credential_id === "string" && !this.storedCredentialHonored(hostId, latest.credential_id, nowMs)) {
      this.sql.exec("DELETE FROM host_metrics_latest WHERE host_id = ? AND credential_id = ?", hostId, latest.credential_id);
    }
    const receipts = this.sql
      .exec("SELECT DISTINCT credential_id FROM host_metrics_idempotency WHERE host_id = ?", hostId)
      .toArray() as Array<{ credential_id?: unknown }>;
    for (const row of receipts) {
      if (typeof row.credential_id !== "string") continue;
      if (!this.storedCredentialHonored(hostId, row.credential_id, nowMs)) {
        this.sql.exec(
          "DELETE FROM host_metrics_idempotency WHERE host_id = ? AND credential_id = ?",
          hostId,
          row.credential_id
        );
      }
    }
  }

  private purgeStaleBindings(nowMs: number = this.now()): void {
    const hosts = this.sql
      .exec("SELECT host_id FROM host_metrics_latest UNION SELECT host_id FROM host_metrics_idempotency")
      .toArray() as Array<{
      host_id: string;
    }>;
    for (const h of hosts) {
      this.retireInvalidStoredRows(h.host_id, nowMs);
    }
  }

  private countRows(table: "host_metrics_latest" | "host_metrics_idempotency", hostId?: string): number {
    const row = (
      hostId
        ? this.sql.exec(`SELECT COUNT(*) AS n FROM ${table} WHERE host_id = ?`, hostId)
        : this.sql.exec(`SELECT COUNT(*) AS n FROM ${table}`)
    ).toArray()[0] as { n: number };
    return Number(row?.n) || 0;
  }

  private sumPayloadBytes(table: "host_metrics_latest" | "host_metrics_idempotency", hostId?: string): number {
    const row = (
      hostId
        ? this.sql.exec(`SELECT COALESCE(SUM(payload_bytes), 0) AS n FROM ${table} WHERE host_id = ?`, hostId)
        : this.sql.exec(`SELECT COALESCE(SUM(payload_bytes), 0) AS n FROM ${table}`)
    ).toArray()[0] as { n: number };
    return Number(row?.n) || 0;
  }

  private retainedBytes(): { latest: number; retry: number; total: number } {
    const latest = this.sumPayloadBytes("host_metrics_latest");
    const retry = this.sumPayloadBytes("host_metrics_idempotency");
    return { latest, retry, total: latest + retry };
  }

  private latestKeySet(): Set<string> {
    return new Set(
      (
        this.sql.exec("SELECT host_id, request_key FROM host_metrics_latest").toArray() as Array<{
          host_id: string;
          request_key: string;
        }>
      ).map((r) => `${r.host_id}\0${r.request_key}`)
    );
  }

  private capsHold(): boolean {
    const bytes = this.retainedBytes();
    if (this.countRows("host_metrics_idempotency") > METRICS_GLOBAL_ROW_CAP) return false;
    if (this.countRows("host_metrics_latest") > METRICS_GLOBAL_LATEST_ROW_CAP) return false;
    if (bytes.retry > METRICS_GLOBAL_BYTE_CAP) return false;
    if (bytes.latest > METRICS_GLOBAL_BYTE_CAP) return false;
    if (bytes.total > METRICS_GLOBAL_BYTE_CAP) return false;
    return true;
  }

  private hostCapsHold(hostId: string): boolean {
    if (this.countRows("host_metrics_idempotency", hostId) > METRICS_PER_HOST_ROW_CAP) return false;
    const hostBytes =
      this.sumPayloadBytes("host_metrics_latest", hostId) + this.sumPayloadBytes("host_metrics_idempotency", hostId);
    return hostBytes <= METRICS_PER_HOST_BYTE_CAP;
  }

  private evictOldestNonLatestRetry(hostId?: string): boolean {
    const latestKeys = this.latestKeySet();
    const ordered = (
      hostId
        ? this.sql.exec(
            "SELECT host_id, request_key FROM host_metrics_idempotency WHERE host_id = ? ORDER BY received_at_ms ASC, request_key ASC",
            hostId
          )
        : this.sql.exec(
            "SELECT host_id, request_key FROM host_metrics_idempotency ORDER BY received_at_ms ASC, host_id ASC, request_key ASC"
          )
    ).toArray() as Array<{ host_id: string; request_key: string }>;
    for (const row of ordered) {
      if (latestKeys.has(`${row.host_id}\0${row.request_key}`)) continue;
      this.sql.exec("DELETE FROM host_metrics_idempotency WHERE host_id = ? AND request_key = ?", row.host_id, row.request_key);
      return true;
    }
    return false;
  }

  private evictOldestStaleLatest(nowMs: number, protectHostId?: string): boolean {
    const rows = this.sql
      .exec("SELECT host_id, as_of_ms, received_at_ms FROM host_metrics_latest ORDER BY received_at_ms ASC, host_id ASC")
      .toArray() as Array<{ host_id: string; as_of_ms: number; received_at_ms: number }>;
    for (const row of rows) {
      if (protectHostId && row.host_id === protectHostId) continue;
      const age = Math.max(nowMs - Number(row.as_of_ms), nowMs - Number(row.received_at_ms));
      if (!Number.isFinite(age) || age >= METRICS_STALE_MS) {
        this.purgeHost(row.host_id);
        return true;
      }
    }
    return false;
  }

  private makeRoomFor(
    hostId: string,
    extraLatestRows: number,
    extraRetryRows: number,
    extraLatestBytes: number,
    extraRetryBytes: number,
    extraHostRetry: number,
    extraHostBytes: number,
    nowMs: number
  ): boolean {
    const fits = (): boolean => {
      const latestRows = this.countRows("host_metrics_latest") + extraLatestRows;
      const retryRows = this.countRows("host_metrics_idempotency") + extraRetryRows;
      const latestBytes = this.sumPayloadBytes("host_metrics_latest") + extraLatestBytes;
      const retryBytes = this.sumPayloadBytes("host_metrics_idempotency") + extraRetryBytes;
      const hostRetry = this.countRows("host_metrics_idempotency", hostId) + extraHostRetry;
      const hostBytes =
        this.sumPayloadBytes("host_metrics_latest", hostId) +
        this.sumPayloadBytes("host_metrics_idempotency", hostId) +
        extraHostBytes;
      return (
        latestRows <= METRICS_GLOBAL_LATEST_ROW_CAP &&
        retryRows <= METRICS_GLOBAL_ROW_CAP &&
        latestBytes <= METRICS_GLOBAL_BYTE_CAP &&
        retryBytes <= METRICS_GLOBAL_BYTE_CAP &&
        latestBytes + retryBytes <= METRICS_GLOBAL_BYTE_CAP &&
        hostRetry <= METRICS_PER_HOST_ROW_CAP &&
        hostBytes <= METRICS_PER_HOST_BYTE_CAP
      );
    };
    for (let i = 0; i < 256; i++) {
      if (fits()) return true;
      const hostRetry = this.countRows("host_metrics_idempotency", hostId) + extraHostRetry;
      const hostBytes =
        this.sumPayloadBytes("host_metrics_latest", hostId) +
        this.sumPayloadBytes("host_metrics_idempotency", hostId) +
        extraHostBytes;
      if (
        (hostRetry > METRICS_PER_HOST_ROW_CAP || hostBytes > METRICS_PER_HOST_BYTE_CAP) &&
        this.evictOldestNonLatestRetry(hostId)
      ) {
        continue;
      }
      if (
        (this.countRows("host_metrics_idempotency") + extraRetryRows > METRICS_GLOBAL_ROW_CAP ||
          this.sumPayloadBytes("host_metrics_idempotency") + extraRetryBytes > METRICS_GLOBAL_BYTE_CAP) &&
        this.evictOldestNonLatestRetry()
      ) {
        continue;
      }
      if (this.evictOldestStaleLatest(nowMs, hostId)) continue;
      return false;
    }
    return fits();
  }

  private enforceCaps(hostId: string, latestKey: string, nowMs: number): boolean {
    void latestKey;
    return this.makeRoomFor(hostId, 0, 0, 0, 0, 0, 0, nowMs);
  }

  private async authenticateHostBearer(
    request: Request,
    nowMs: number
  ): Promise<{ machineId?: string; credId?: string; errorResponse?: Response }> {
    const authHeader = request.headers.get("Authorization");
    if (!authHeader || !authHeader.startsWith("Bearer ")) {
      return { errorResponse: jsonResponse({ error: "unauthorized" }, 401) };
    }
    const token = authHeader.substring(7).trim();
    if (!token || !/^hubt_[A-Za-z0-9_-]+$/.test(token)) {
      return { errorResponse: jsonResponse({ error: "unauthorized" }, 401) };
    }
    const tokenHash = await sha256(token);
    const cred = this.sql.exec("SELECT * FROM credentials WHERE token_hash = ?", tokenHash).toArray()[0] as any;
    if (!cred || cred.revoked_at) return { errorResponse: jsonResponse({ error: "unauthorized" }, 401) };
    const expiresMs = Date.parse(cred.expires_at);
    if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) {
      return { errorResponse: jsonResponse({ error: "unauthorized" }, 401) };
    }
    if (cred.kind !== "host") {
      return { errorResponse: jsonResponse({ error: "forbidden", reason: "host_kind_required" }, 403) };
    }
    let scopes: unknown;
    try {
      scopes = typeof cred.scopes === "string" ? JSON.parse(cred.scopes) : cred.scopes;
    } catch {
      return { errorResponse: jsonResponse({ error: "unauthorized" }, 401) };
    }
    if (!Array.isArray(scopes) || scopes.includes("admin") || !scopes.includes("host")) {
      return { errorResponse: jsonResponse({ error: "forbidden", reason: "host_scope_required" }, 403) };
    }
    if (typeof cred.machine_id !== "string" || !cred.machine_id.trim()) {
      return { errorResponse: jsonResponse({ error: "forbidden", reason: "machine_binding_required" }, 403) };
    }
    if (!this.liveMachine(cred.machine_id, cred.id)) {
      this.retireCallerBinding(cred.machine_id, cred.id, nowMs);
      return { errorResponse: jsonResponse({ error: "forbidden", reason: "machine_credential_mismatch" }, 403) };
    }
    // last_used bookkeeping happens inside the mutation transaction only,
    // after fresh re-auth + live gate, never on pre-body transport auth.
    return { machineId: cred.machine_id, credId: cred.id };
  }

  public putSnapshot(
    hostId: string,
    credentialId: string,
    rawPayload: unknown,
    digest: string,
    nowMs: number = this.now()
  ): { ok: boolean; status: number; body: Record<string, unknown> } {
    const validation = validateMetricsSnapshotPayload(rawPayload, hostId, nowMs);
    if (!validation.ok || !validation.snapshot) {
      const errBody: Record<string, unknown> = { error: validation.error || "invalid_payload" };
      if (validation.reason) errBody.reason = validation.reason;
      return { ok: false, status: validation.status, body: errBody };
    }
    const snap = validation.snapshot;
    const receivedAtIso = new Date(nowMs).toISOString();
    const payloadJson = JSON.stringify({
      schema_version: snap.schema_version,
      as_of: snap.as_of,
      host_id: snap.host_id,
      request_key: snap.request_key,
      coverage: snap.coverage,
      gaps: snap.gaps,
      utilization: snap.utilization,
      usage: snap.usage,
      accepted: snap.accepted,
    });
    const payloadBytes = new TextEncoder().encode(payloadJson).byteLength;
    if (payloadBytes > METRICS_MAX_BODY_BYTES) {
      return { ok: false, status: 413, body: { error: "body_too_large" } };
    }

    return this.runInTransaction(() => {
      const auth = this.authorizeCurrentHostCredential(credentialId, hostId, nowMs);
      if (!auth.ok) {
        this.retireCallerBinding(hostId, credentialId, nowMs);
        return { ok: false, status: auth.status, body: { error: auth.error, reason: auth.reason } };
      }
      // Live family/approval gate (M9 integration): reuses existing
      // SessionControlService.hostObservationAllowed with the register
      // bound-owner exemption (pending approval never blocks the live
      // bound owner; takeover still denies via bound-owner mismatch).
      // Fails closed while absent; JSON flags never authorize.
      // Scoped retire only on denial.
      if (typeof this.gate !== "function") {
        this.retireCallerBinding(hostId, credentialId, nowMs);
        return { ok: false, status: 503, body: { error: "observation_auth_unavailable", reason: "gate_missing" } };
      }
      if (!this.gateAllows(hostId, credentialId, nowMs)) {
        this.retireCallerBinding(hostId, credentialId, nowMs);
        return { ok: false, status: 403, body: { error: "forbidden", reason: "host_observation_denied" } };
      }
      try {
        this.sql.exec("UPDATE credentials SET last_used_at = ? WHERE id = ?", new Date(nowMs).toISOString(), credentialId);
      } catch { /* bookkeeping only */ }
      this.purgeStaleBindings(nowMs);

      const existingKey = this.sql
        .exec(
          `SELECT request_digest, as_of_ms, received_at_ms, received_at, payload_json
           FROM host_metrics_idempotency WHERE host_id = ? AND request_key = ?`,
          hostId,
          snap.request_key
        )
        .toArray()[0] as
        | {
            request_digest: string;
            as_of_ms: number;
            received_at_ms: number;
            received_at?: string;
            payload_json?: string;
          }
        | undefined;
      if (existingKey) {
        if (existingKey.request_digest !== digest) {
          return { ok: false, status: 409, body: { error: "idempotency_conflict" } };
        }
        let stored: MetricsSnapshot | null = null;
        if (typeof existingKey.payload_json === "string" && existingKey.payload_json) {
          try {
            stored = JSON.parse(existingKey.payload_json) as MetricsSnapshot;
          } catch {
            stored = null;
          }
        }
        if (!stored) {
          const latestSame = this.sql.exec("SELECT * FROM host_metrics_latest WHERE host_id = ?", hostId).toArray()[0] as any;
          if (latestSame && latestSame.request_key === snap.request_key && latestSame.request_digest === digest) {
            try {
              stored = JSON.parse(latestSame.payload_json) as MetricsSnapshot;
              existingKey.received_at = latestSame.received_at;
              existingKey.received_at_ms = Number(latestSame.received_at_ms);
              existingKey.as_of_ms = Number(latestSame.as_of_ms);
            } catch {
              stored = null;
            }
          }
        }
        if (!stored) {
          return { ok: false, status: 409, body: { error: "idempotency_conflict" } };
        }
        stored.host_id = hostId;
        stored.request_key = snap.request_key;
        stored.as_of_ms = Number(existingKey.as_of_ms);
        const receivedAt = existingKey.received_at || new Date(Number(existingKey.received_at_ms)).toISOString();
        return {
          ok: true,
          status: 200,
          body: publicSnapshotBody(stored, receivedAt, nowMs, Number(existingKey.as_of_ms), Number(existingKey.received_at_ms)),
        };
      }

      const latest = this.sql.exec("SELECT as_of_ms, request_key, payload_bytes FROM host_metrics_latest WHERE host_id = ?", hostId)
        .toArray()[0] as { as_of_ms: number; request_key: string; payload_bytes: number } | undefined;
      if (latest && Number(latest.as_of_ms) > snap.as_of_ms) {
        return { ok: false, status: 409, body: { error: "out_of_order_as_of" } };
      }
      if (latest && Number(latest.as_of_ms) === snap.as_of_ms && latest.request_key !== snap.request_key) {
        return { ok: false, status: 409, body: { error: "out_of_order_as_of", reason: "same_as_of_different_key" } };
      }

      const extraLatestRows = latest ? 0 : 1;
      const extraRetryRows = 1;
      const extraLatestBytes = payloadBytes - (latest ? Number(latest.payload_bytes) || 0 : 0);
      const extraRetryBytes = payloadBytes;
      const extraHostRetry = 1;
      const extraHostBytes = extraLatestBytes + extraRetryBytes;
      if (
        !this.makeRoomFor(
          hostId,
          extraLatestRows,
          extraRetryRows,
          extraLatestBytes,
          extraRetryBytes,
          extraHostRetry,
          extraHostBytes,
          nowMs
        )
      ) {
        return { ok: false, status: 507, body: { error: "capacity_exceeded" } };
      }

      this.sql.exec(
        `INSERT INTO host_metrics_idempotency (
          host_id, request_key, request_digest, as_of_ms, received_at_ms, received_at, credential_id, payload_bytes, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_id, request_key) DO UPDATE SET
          request_digest = excluded.request_digest,
          as_of_ms = excluded.as_of_ms,
          received_at_ms = excluded.received_at_ms,
          received_at = excluded.received_at,
          credential_id = excluded.credential_id,
          payload_bytes = excluded.payload_bytes,
          payload_json = excluded.payload_json`,
        hostId,
        snap.request_key,
        digest,
        snap.as_of_ms,
        nowMs,
        receivedAtIso,
        credentialId,
        payloadBytes,
        payloadJson
      );

      this.sql.exec(
        `INSERT INTO host_metrics_latest (
          host_id, as_of, as_of_ms, received_at, received_at_ms, request_key, request_digest,
          credential_id, schema_version, payload_json, payload_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_id) DO UPDATE SET
          as_of = excluded.as_of,
          as_of_ms = excluded.as_of_ms,
          received_at = excluded.received_at,
          received_at_ms = excluded.received_at_ms,
          request_key = excluded.request_key,
          request_digest = excluded.request_digest,
          credential_id = excluded.credential_id,
          schema_version = excluded.schema_version,
          payload_json = excluded.payload_json,
          payload_bytes = excluded.payload_bytes
        WHERE excluded.as_of_ms >= host_metrics_latest.as_of_ms`,
        hostId,
        snap.as_of,
        snap.as_of_ms,
        receivedAtIso,
        nowMs,
        snap.request_key,
        digest,
        credentialId,
        snap.schema_version,
        payloadJson,
        payloadBytes
      );

      if (!this.enforceCaps(hostId, snap.request_key, nowMs) || !this.capsHold() || !this.hostCapsHold(hostId)) {
        throw new Error("metrics_capacity_postcondition");
      }
      return {
        ok: true,
        status: 200,
        body: publicSnapshotBody(snap, receivedAtIso, nowMs, snap.as_of_ms, nowMs),
      };
    });
  }

  public getSnapshot(hostId: string, nowMs: number = this.now()): { status: number; body: Record<string, unknown> } {
    this.purgeStaleBindings(nowMs);
    const row = this.sql.exec("SELECT * FROM host_metrics_latest WHERE host_id = ?", hostId).toArray()[0] as any;
    if (!row || !this.storedCredentialHonored(hostId, row.credential_id, nowMs)) {
      if (row) this.retireInvalidStoredRows(hostId, nowMs);
      return {
        status: 404,
        body: {
          schema_version: "1.0",
          host_id: hostId,
          as_of: null,
          received_at: null,
          stale: true,
          status: "unknown",
          error: "unavailable",
          coverage: { utilization: "unknown", usage: "unknown", accepted: "unknown" },
          gaps: [{ code: "missing" }],
          utilization: { unknown: true, projects: {} },
          usage: { unknown: true, projects: {} },
          accepted: { unknown: true, accepted_outcomes: null, task_count: null, commit_count: null, review_count: null, projects: {} },
        },
      };
    }
    let snap: MetricsSnapshot;
    try {
      snap = JSON.parse(row.payload_json) as MetricsSnapshot;
    } catch {
      return { status: 404, body: { schema_version: "1.0", host_id: hostId, status: "unknown", error: "unavailable" } };
    }
    snap.host_id = hostId;
    snap.request_key = row.request_key;
    snap.as_of_ms = Number(row.as_of_ms);
    return {
      status: 200,
      body: publicSnapshotBody(snap, row.received_at, nowMs, Number(row.as_of_ms), Number(row.received_at_ms)),
    };
  }

  public listLatest(limit: number, after: string | null, nowMs: number = this.now()): Record<string, unknown> {
    this.purgeStaleBindings(nowMs);
    const capped = Math.max(1, Math.min(METRICS_PAGE_MAX, Math.floor(limit) || METRICS_PAGE_MAX));
    const rows = (
      after
        ? this.sql.exec(
            "SELECT host_id, as_of, received_at, as_of_ms, received_at_ms, payload_json FROM host_metrics_latest WHERE host_id > ? ORDER BY host_id ASC LIMIT ?",
            after,
            capped + 1
          )
        : this.sql.exec(
            "SELECT host_id, as_of, received_at, as_of_ms, received_at_ms, payload_json FROM host_metrics_latest ORDER BY host_id ASC LIMIT ?",
            capped + 1
          )
    ).toArray() as any[];
    const page = rows.slice(0, capped);
    const items = page.map((row) => {
      let coverage = { utilization: "unknown", usage: "unknown", accepted: "unknown" };
      try {
        const parsed = JSON.parse(row.payload_json);
        if (parsed && parsed.coverage) coverage = parsed.coverage;
      } catch {}
      const age = Math.max(nowMs - Number(row.as_of_ms), nowMs - Number(row.received_at_ms));
      const stale = !Number.isFinite(age) || age < 0 || age >= METRICS_STALE_MS;
      return {
        host_id: row.host_id,
        as_of: row.as_of,
        received_at: row.received_at,
        stale,
        status: stale ? "stale" : "ok",
        coverage,
      };
    });
    return {
      schema_version: "1.0",
      items,
      next_cursor: rows.length > capped ? page[page.length - 1].host_id : null,
    };
  }

  public async handleRequest(request: Request, url?: URL): Promise<Response> {
    const reqUrl = url || new URL(request.url);
    const nowMs = this.now();
    if (request.method !== "GET" && request.method !== "POST") {
      return new Response(JSON.stringify({ error: "method_not_allowed" }), {
        status: 405,
        headers: { ...PRIVATE_NO_STORE_HEADERS, Allow: "GET, POST" },
      });
    }
    // Client-forged host identity headers are always denied. Do not treat
    // X-Forwarded-Host as spoof: Cloudflare injects it on every browser request,
    // and denying it blocked admin cookie Metrics GETs (Access denied on
    // hub.dtcdev.click). Host binding still comes only from the bearer credential.
    if (request.headers.has("x-host-id") || request.headers.has("x-machine-id")) {
      return jsonResponse({ error: "forbidden", reason: "header_spoof_denied" }, 403);
    }

    if (request.method === "POST") {
      const hasAuthHeader = request.headers.has("Authorization");
      if (!hasAuthHeader) {
        const session = await getSession(this.env, request);
        if (session) return jsonResponse({ error: "forbidden", reason: "host_credential_required" }, 403);
        return jsonResponse({ error: "unauthorized" }, 401);
      }
      const hostAuth = await this.authenticateHostBearer(request, nowMs);
      if (hostAuth.errorResponse) return hostAuth.errorResponse;
      const hostId = hostAuth.machineId!;
      if (reqUrl.searchParams.has("host_id") && reqUrl.searchParams.get("host_id") !== hostId) {
        return jsonResponse({ error: "forbidden", reason: "query_host_id_mismatch" }, 403);
      }
      const parsedBody = await readBoundedJsonBody(request);
      if (parsedBody.errorStatus || !parsedBody.data) {
        return jsonResponse({ error: parsedBody.errorCode || "invalid_body" }, parsedBody.errorStatus || 400);
      }
      const canonical = JSON.stringify(parsedBody.data);
      const digest = await sha256(canonical);
      const writeNow = this.now();
      const reauth = this.authorizeCurrentHostCredential(hostAuth.credId!, hostId, writeNow);
      if (!reauth.ok) {
        this.retireCallerBinding(hostId, hostAuth.credId!, writeNow);
        return jsonResponse({ error: reauth.error, reason: reauth.reason }, reauth.status);
      }
      if (typeof this.gate !== "function") {
        this.retireCallerBinding(hostId, hostAuth.credId!, writeNow);
        return jsonResponse({ error: "observation_auth_unavailable", reason: "gate_missing" }, 503);
      }
      if (!this.gateAllows(hostId, hostAuth.credId!, writeNow)) {
        this.retireCallerBinding(hostId, hostAuth.credId!, writeNow);
        return jsonResponse({ error: "forbidden", reason: "host_observation_denied" }, 403);
      }
      const putRes = this.putSnapshot(hostId, hostAuth.credId!, parsedBody.data, digest, writeNow);
      return jsonResponse(putRes.body, putRes.status);
    }

    if (request.headers.has("Authorization")) {
      const hostAuth = await this.authenticateHostBearer(request, nowMs);
      if (hostAuth.errorResponse) return hostAuth.errorResponse;
      const hostId = hostAuth.machineId!;
      const readNow = this.now();
      const reauth = this.authorizeCurrentHostCredential(hostAuth.credId!, hostId, readNow);
      if (!reauth.ok) {
        this.retireCallerBinding(hostId, hostAuth.credId!, readNow);
        return jsonResponse({ error: reauth.error, reason: reauth.reason }, reauth.status);
      }
      if (typeof this.gate === "function") {
        if (!this.gateAllows(hostId, hostAuth.credId!, readNow)) {
          this.retireCallerBinding(hostId, hostAuth.credId!, readNow);
          return jsonResponse({ error: "forbidden", reason: "host_observation_denied" }, 403);
        }
      } else {
        this.retireCallerBinding(hostId, hostAuth.credId!, readNow);
        return jsonResponse({ error: "observation_auth_unavailable", reason: "gate_missing" }, 503);
      }
      if (reqUrl.searchParams.has("host_id")) {
        const requested = reqUrl.searchParams.get("host_id");
        if (!requested || requested !== hostId) {
          return jsonResponse({ error: "forbidden", reason: "cross_machine_read_denied" }, 403);
        }
      }
      const getRes = this.getSnapshot(hostId, readNow);
      return jsonResponse(getRes.body, getRes.status);
    }

    const session = await getSession(this.env, request);
    if (!session) return jsonResponse({ error: "unauthorized" }, 401);
    if (!session.isAdmin) return jsonResponse({ error: "forbidden" }, 403);
    const originHeader = request.headers.get("Origin");
    if (originHeader && originHeader !== reqUrl.origin) {
      return jsonResponse({ error: "forbidden", reason: "origin_mismatch" }, 403);
    }
    const secFetchSite = request.headers.get("Sec-Fetch-Site");
    if (secFetchSite && secFetchSite.toLowerCase() === "cross-site") {
      return jsonResponse({ error: "forbidden", reason: "cross_site_request_denied" }, 403);
    }
    if (!verifyCsrf(request, session)) {
      const csrfHeader = request.headers.get("x-csrf-token");
      if (csrfHeader !== null && csrfHeader !== session.csrfToken) {
        return jsonResponse({ error: "forbidden", reason: "invalid_csrf_token" }, 403);
      }
      if (request.method !== "GET" && request.method !== "HEAD") {
        return jsonResponse({ error: "forbidden", reason: "invalid_csrf_token" }, 403);
      }
    }

    const readNow = this.now();
    const requestedHostId = reqUrl.searchParams.get("host_id");
    if (requestedHostId) {
      if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(requestedHostId)) {
        return jsonResponse({ error: "invalid_host_id" }, 400);
      }
      const getRes = this.getSnapshot(requestedHostId, readNow);
      return jsonResponse(getRes.body, getRes.status);
    }

    const limitRaw = reqUrl.searchParams.get("limit");
    const limit = limitRaw ? Number(limitRaw) : METRICS_PAGE_MAX;
    if (limitRaw && (!Number.isInteger(limit) || limit < 1 || limit > METRICS_PAGE_MAX)) {
      return jsonResponse({ error: "invalid_limit" }, 400);
    }
    const after = reqUrl.searchParams.get("after");
    if (after && !/^[A-Za-z0-9_.:-]{1,128}$/.test(after)) {
      return jsonResponse({ error: "invalid_cursor" }, 400);
    }
    return jsonResponse(this.listLatest(limit, after, readNow), 200);
  }
}

export async function handleMetricsRoutes(
  request: Request,
  path: string,
  metricsStore: MetricsStore,
  url?: URL
): Promise<Response | null> {
  if (path !== "/api/metrics") return null;
  return metricsStore.handleRequest(request, url);
}
