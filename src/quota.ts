import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from "./env";
import { sha256 } from "./auth/crypto";
import { getSession } from "./auth/session";

export const QUOTA_SCHEMA_version = "1.0";
export const QUOTA_MAX_BODY_BYTES = 32_768;
export const QUOTA_STALE_MS = 1_200_000; // 1200 seconds (20 minutes) conservative boundary
export const MAX_WINDOWS_PER_PROVIDER = 32;
export const WINDOW_NAME_REGEX = /^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/;

export const ALLOWED_QUOTA_PROVIDERS = new Set<string>([
  "codex",
  "claude",
  "gemini",
  "grok",
  "zai",
  "go",
  "copilot",
]);

export const QUOTA_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS host_quota_snapshots (
  host_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  captured_at_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  received_at_ms INTEGER NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  routes_json TEXT NOT NULL
);
`;

const TOP_LEVEL_KEYS = new Set<string>([
  "schema_version",
  "captured_at",
  "host_id",
  "status",
  "error",
  "routes",
]);

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

const FORBIDDEN_PROTO_KEYS = new Set<string>([
  "__proto__",
  "constructor",
  "prototype",
]);

export interface QuotaWindowDTO {
  percent_remaining: number | null;
  reset_at: string | null;
  present: boolean;
  rolling: boolean;
  error?: "unavailable" | null;
}

export interface QuotaRouteDetailsDTO {
  limit_reached: boolean | null;
  has_grok_code_access?: boolean | null;
  stale?: boolean | null;
}

export interface QuotaRouteDTO {
  status: "ok" | "error" | "unknown";
  error: "unavailable" | null;
  observed_at: string | null;
  details: QuotaRouteDetailsDTO;
  windows: Record<string, QuotaWindowDTO>;
}

export interface QuotaSnapshotDTO {
  schema_version: "1.0";
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
    schema_version: "1.0";
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
    if (FORBIDDEN_PROTO_KEYS.has(k) || !TOP_LEVEL_KEYS.has(k)) {
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

  if (raw.schema_version !== "1.0") {
    return { ok: false, status: 400, error: "invalid_schema_version" };
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
        if (FORBIDDEN_PROTO_KEYS.has(wk) || !WINDOW_KEYS.has(wk)) {
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

      if (winVal.present === false && (pct !== null || resetAtIso !== null)) {
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
      schema_version: "1.0",
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
    const routesJson = JSON.stringify(snap.routes);

    return this.runInTransaction(() => {
      const existing = this.sql
        .exec(
          "SELECT captured_at_ms FROM host_quota_snapshots WHERE host_id = ?",
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

      this.sql.exec(
        `INSERT INTO host_quota_snapshots (
          host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(host_id) DO UPDATE SET
          schema_version = excluded.schema_version,
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
          schema_version: "1.0",
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
      return {
        status: 503,
        body: {
          schema_version: "1.0",
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

    return {
      status: 200,
      body: {
        schema_version: "1.0",
        captured_at: row.captured_at,
        received_at: row.received_at,
        host_id: row.host_id,
        stale: false,
        status: "ok",
        error: null,
        routes,
      },
    };
  }

  public async handleRequest(request: Request, url?: URL): Promise<Response> {
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

    // Reject any host-spoofing headers unconditionally
    if (
      request.headers.has("x-host-id") ||
      request.headers.has("x-machine-id") ||
      request.headers.has("x-forwarded-host")
    ) {
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

    const originHeader = request.headers.get("Origin");
    if (originHeader && originHeader !== reqUrl.origin) {
      return jsonResponse({ error: "forbidden", reason: "origin_mismatch" }, 403);
    }

    const secFetchSite = request.headers.get("Sec-Fetch-Site");
    if (secFetchSite && secFetchSite.toLowerCase() === "cross-site") {
      return jsonResponse({ error: "forbidden", reason: "cross_site_request_denied" }, 403);
    }

    const csrfHeader = request.headers.get("x-csrf-token");
    if (csrfHeader !== null && csrfHeader !== session.csrfToken) {
      return jsonResponse({ error: "forbidden", reason: "invalid_csrf_token" }, 403);
    }

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
  url?: URL
): Promise<Response | null> {
  if (path !== "/api/quota") return null;
  return quotaStore.handleRequest(request, url);
}
