import { jsonFields, initResourceRequestProjection } from './sql-json-projection';
import { sanitizeExitEvidence, redactExitText } from './exit-evidence';
import { sanitizeCommandFields } from './command-diagnostics';
import {sanitizeCpuAdmission,sanitizeCpuUsage} from './cpu-headroom';
import { launcherDiskHeadroom } from './launcher-disk';
/** Generic account registry and placement. Provider credentials never enter this service. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { RoutingService } from './routing';
import { ClaimAdmission, sameIntent, sameEditCaller, type EditAuthority, type AdmissionCaller } from './claim_admission';
import { effectiveCheckin } from './registry';
import { taskTier, policy } from './model-routing';
import { QUOTA_SCHEMA_SQL, QUOTA_STALE_MS, LEGACY_ACCOUNT_LABELS, canonicalAccountLabel, canonicalHarness, canonicalQuotaProvider, dedupeAccountsByCanonical, ensureTbmQuotaSchema, legacyRouteKeyFor, snapshotRouteForAccount } from './quota';
import { evaluateLaunchQuota } from './quota_admission';
import { adoptObservedNativeSessions, reconcileUnboundFleetSessions } from './session_control';
import { isValidMachineId } from './machine-id';
import { LivenessService } from './liveness';
import { reconcileAccountCapacity } from './account_capacity';
import { resolveMachineAliases, projectAliasSessions } from './machine-alias';
import { freshCatalogs } from './catalog-freshness';
import { syncTbmFromServerAccounts } from './tbm-ingestion';
import { recordSessionTokens } from './tbm-controller';
import { loadProviderPacing, loadPools, netHeadroom, pacingFor, resetUrgency } from './pacing';
// Host telemetry freshness windows (intake 4398ce2a). The host posts a fast
// heartbeat every TELEMETRY_REFRESH_S (host/agent_hub_host/fleet.py, 45 s);
// keep LAUNCHER_TELEMETRY_STALE_MS >= 3x that (host regression test parses it).
export const LAUNCHER_TELEMETRY_STALE_MS = 180000;
export const GATE_STALE_MS = 300000;
/** Engine holds that expire into a single retrial after this cooldown. */
export const ENGINE_HEALTH_RETRY_MS = 600000;
export const RETRIABLE_ENGINE_HOLDS = new Set(['engine_auth_expired', 'engine_tool_permission_refused']);

export const FLEET_SCHEMA = `
CREATE TABLE IF NOT EXISTS fleet_catalog_seen (machine_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_queue_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_rowid INTEGER NOT NULL, high_water INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_memory_holds (launch_id TEXT PRIMARY KEY, since TEXT NOT NULL, rejected_json TEXT NOT NULL, refused INTEGER NOT NULL DEFAULT 0, retry_id TEXT, evidence_json TEXT);
CREATE TABLE IF NOT EXISTS fleet_accounts (label TEXT PRIMARY KEY, provider TEXT NOT NULL, max_concurrency INTEGER NOT NULL, reserve_percent REAL NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_routes (machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, PRIMARY KEY(machine_id, engine, account));
CREATE TABLE IF NOT EXISTS fleet_subscriptions (machine_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL, plan TEXT, observed_at TEXT NOT NULL, quota_json TEXT NOT NULL, PRIMARY KEY(machine_id, account));
CREATE TABLE IF NOT EXISTS fleet_sessions (machine_id TEXT NOT NULL, native_id TEXT NOT NULL, tag TEXT NOT NULL, engine TEXT, phase TEXT NOT NULL, agent_state TEXT, observed_at TEXT NOT NULL, workspace TEXT NOT NULL, profile TEXT, account TEXT, last_activity_at TEXT, cwd TEXT, PRIMARY KEY(machine_id, native_id));
CREATE TABLE IF NOT EXISTS fleet_launches (id TEXT PRIMARY KEY, actor TEXT NOT NULL, requester_kind TEXT NOT NULL, parent_id TEXT, summary TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, mode TEXT NOT NULL, workspace TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT, launcher_task_id TEXT, runtime_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
CREATE TABLE IF NOT EXISTS fleet_engine_health (engine TEXT NOT NULL, machine_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL, diagnostics_json TEXT, PRIMARY KEY(engine, machine_id));
CREATE TABLE IF NOT EXISTS operator_notices (notice_key TEXT PRIMARY KEY, engine TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS placement_rejections (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, created_at_ms INTEGER NOT NULL, actor TEXT NOT NULL, reasons_json TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_placement_rejections_recent ON placement_rejections(created_at_ms);
`;
type Auth = { caller?: AdmissionCaller; scopes?: string[]; error?: Response };
const LABEL = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
// Idempotency key for same-host claim retries (intake 488b860e): per launch
// plus host attempt, so a late-landing original claim plus a retry replays
// instead of double-claiming. UUID hex from the host fits with headroom.
const CLAIM_KEY = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
// Wire-compatible provider/service namespace: `codex-orin` is accepted as a
// quota route key and subscription service for hosts that have not updated
// yet, but it canonicalizes to provider `codex` (the second Codex account)
// on every read path. Never register new accounts under it.
const PROVIDERS = new Set(['codex', 'codex-orin', 'claude', 'grok', 'gemini', 'zai', 'go', 'copilot']);
const ACTIVE = "('offered','starting','running','uncertain')";
// A native catalog can observe spawn before the launcher posts its receipt.
// Correlate that narrow in-flight window without trusting a tag alone.
const NATIVE_IN_FLIGHT = `(
  l.native_id IS NULL AND l.state IN ('starting','uncertain') AND
  l.engine = s.engine AND l.account = s.account AND l.profile IS s.profile AND
  -- Native start runs in the validated workspace-relative cwd, which can
  -- differ from the project root stored on the launch. Match that exact
  -- intended directory; never correlate by a task tag or path suffix alone.
  CASE WHEN json_valid(l.request_json) THEN
    CASE WHEN json_extract(l.request_json, '$.cwd') IS NULL OR json_extract(l.request_json, '$.cwd') = '.'
      THEN l.workspace
      WHEN json_extract(l.request_json, '$.cwd') LIKE '/%' OR json_extract(l.request_json, '$.cwd') GLOB '[A-Za-z]:[/\\]*'
      THEN json_extract(l.request_json, '$.cwd')
      ELSE rtrim(l.workspace, '/') || '/' || json_extract(l.request_json, '$.cwd') END
    END = s.workspace AND
  (SELECT COUNT(*) FROM fleet_sessions spawned WHERE spawned.machine_id = s.machine_id
   AND spawned.tag = s.tag AND spawned.engine = s.engine AND spawned.account = s.account
   AND spawned.profile IS s.profile AND spawned.workspace = s.workspace
   AND spawned.phase IN ('starting','running','broken','unknown','uncertain')) = 1 AND
  ((l.mode = 'headless' AND s.tag = 'task-hub-' || l.id) OR
   (l.mode = 'interactive' AND s.tag = 'hub-' || l.id)))`;
// Separate exact native identity from the in-flight predicate so SQLite can
// seek the native index instead of scanning every active reservation per row.
// The managed tag narrows the second lookup to one primary key; the complete
// incarnation predicate still proves the correlation (a tag alone cannot).
const NATIVE_UNRESERVED = `NOT EXISTS (SELECT 1 FROM fleet_launches l
  WHERE l.machine_id=s.machine_id AND l.native_id=s.native_id AND l.state IN ${ACTIVE})
  AND NOT EXISTS (SELECT 1 FROM fleet_launches l
  WHERE l.id = CASE WHEN substr(s.tag,1,9)='task-hub-' THEN substr(s.tag,10)
    WHEN substr(s.tag,1,4)='hub-' THEN substr(s.tag,5) END
  AND l.machine_id=s.machine_id AND ${NATIVE_IN_FLIGHT})`;

/** Registry reserves cannot fall below the configured admission reserve. */
const HEADROOM: Record<string, number> = { ...policy.rules.reserve_percent,
  'codex-orin': policy.rules.reserve_percent.codex };
/** The native ZCode adapter and managed Codex profile serve the same ZAI model. */
function matchesModelRoute(model: any, route: any, family: unknown): boolean {
  const nativeZai = family === 'zai' && route.engine === 'zcodex' && route.profile === null;
  return model.provider === family &&
    (model.engine === route.engine || (nativeZai && model.engine === 'codex' && model.profile === 'zcodex')) &&
    (model.profile === route.profile || route.profile === 'hub-' + route.account ||
      (model.profile === null && route.profile !== null) || (nativeZai && model.profile === 'zcodex'));
}
/** Engines whose providers the canonical model policy disables. New grok sessions hold here. */
function disabledEngines(): Set<string> {
  const rules = ((policy as any).rules || {}) as any;
  const disabled = new Set<string>(Array.isArray(rules.disabled_providers) ? rules.disabled_providers : []);
  return new Set<string>((((policy as any).models || []) as any[]).filter(m => m && disabled.has(m.provider)).map(m => String(m.engine)));
}
/** Reject reasons that mean "no free slot right now" (transient capacity). */
const CAPACITY_REASONS = new Set(['disk_floor', 'launcher_disk_reserve', 'account_capacity', 'account_concurrency', 'machine_capacity', 'launcher_memory_reserve', 'cpu_headroom', 'cpu_pressure', 'memory_pressure', 'host_oom', 'host_runtime_choking']);
/** Launcher memory gate inputs, mirrored here for dashboard visibility only.
 * The placement verdict stays in plan(); this module never changes the math,
 * it only projects the same inputs (MemAvailable, reserved total, floor for
 * the next slot) so operators see why starts hold. Owned by the launcher
 * reservation fixer; visibility reuses its accounting. */
import { MEMORY_FLOOR_MB, NEXT_SLOT_MB, ResourceManagement, memoryVerdict } from "./resource-management";
export { MEMORY_FLOOR_MB, NEXT_SLOT_MB };
/** Placement-pressure reasons that degrade host health and raise operator
 * alerts: any launch rejected or held for memory/capacity. */
export const PLACEMENT_PRESSURE_REASONS = new Set(['launcher_memory_reserve', 'account_capacity', 'account_concurrency', 'machine_capacity', 'cpu_headroom', 'cpu_pressure', 'memory_pressure', 'host_oom', 'host_runtime_choking']);
/** Incidental per-route reasons that may accompany capacity but never queue alone
 * (stale/drained check-ins, policy-disabled engines, engines no live host
 * advertises anymore — e.g. registry routes for engines the current host
 * release stopped running). */
const DISPATCH_MESSAGES: Record<string, string> = {
  cpu_headroom: 'Host CPU usage plus expected session demand exceeds the CPU target.',
  cpu_pressure: 'Host CPU pressure exceeds the safety cap.',
  cpu_usage_unknown: 'Fresh real CPU usage is unavailable.',
  memory_pressure: 'Host memory full pressure exceeds the safety cap.',
  host_oom: 'A recent host OOM kill requires a placement cooldown.',
  host_runtime_choking: 'Recent session or service failures require a placement cooldown.',
  "workspace_unavailable": "The requested workspace is not available on this host.",
  "dispatch_route_unconfigured": "The offered account, engine and profile do not match a local route.",
  "goal_startup_unconfigured": "This account has no trusted interactive goal startup command.",
  "dispatch_startup_invalid": "The configured trusted startup command is invalid.",
  "dispatch_cwd_invalid": "The requested working directory is not confined to the workspace.",
  "dispatch_goal_invalid": "Goal work requires an interactive Codex-family route and nonempty text.",
  "dispatch_prompt_unsupported": "Interactive prompt injection requires an explicit goal start.",
  "dispatch_offer_invalid": "The offered launch or task identifier is invalid.",
  "dispatch_capability_unavailable": "The required task capabilities are unavailable on this host.",
  "dispatch_local_command_failed": "A local dispatch prerequisite command failed before native start.",
  "dispatch_internal_error": "An unexpected host dispatch error occurred before native start.",
  "dispatch_claim_refused": "The Hub refused the dispatch claim before native start.",
  "model_unsupported": "The selected model or effort is unsupported by this local route.",
  "admission_unconfigured": "This local route has no maintained fresh admission adapter.",
  "admission_refused": "Fresh admission refused this account or could not verify its timestamp and model.",
  "engine_auth_expired": "The engine authentication is unavailable or expired.",
  "codex_quota_gate_quota_unavailable": "The managed Codex account quota gate refused launch before execution.",
  "codex_quota_gate_codex_reserve_required": "The managed Codex account is below the configured weekly reserve.",
  "codex_quota_gate_stale_window_evidence": "The managed Codex quota evidence is stale.",
  "codex_quota_gate_quota_check_failed": "The managed Codex quota check failed."
};
const CLAIM_HTTP_SLUGS = new Set<string>(["account_capacity", "account_concurrency", "account_unavailable", "already_claimed", "claim_actor_ambiguous", "claim_authority_changed", "claim_authority_unsupported", "claim_conflict", "claim_holder_mismatch", "claim_inactive", "claim_project_mismatch", "claim_required", "claim_scope_mismatch", "claim_workspace_unconfigured", "cooldown_active", "cpu_headroom", "cpu_pressure", "cpu_usage_unknown", "forbidden", "fresh_admission_required", "future_observation", "grok_entitlement", "host_oom", "host_runtime_choking", "invalid_claim", "invalid_observed_at", "limit_reached", "machine_capacity", "malformed", "memory_pressure", "model_unsupported", "no_available_window", "no_quota_evidence", "not_found", "provider_disabled", "quota_exhausted", "quota_stale", "quota_unavailable", "quota_unknown", "reserve", "reserve_required", "reset_passed_since_observation", "route_not_ok", "route_stale", "stale_observation", "stale_window_evidence", "unauthorized", "window_unavailable", "worker_scope_forbidden"]);
const RETRYABLE_CLAIM_ERRORS = new Set<string>(["account_capacity", "account_concurrency", "cpu_headroom", "cpu_pressure", "fresh_admission_required", "host_oom", "host_runtime_choking", "machine_capacity", "memory_pressure"]);
function claimFieldsValid(d: any): boolean {
  const fields = ['http_status', 'hub_error', 'hub_reason', 'retryable'];
  if (d.reason !== 'dispatch_claim_refused') return fields.every(k => d[k] === undefined);
  if (d.http_status !== undefined && (!Number.isInteger(d.http_status) || d.http_status < 400 || d.http_status > 599)) return false;
  if (['hub_error', 'hub_reason'].some(k => d[k] !== undefined && !CLAIM_HTTP_SLUGS.has(d[k]))) return false;
  return d.retryable === undefined || (typeof d.retryable === 'boolean' && (!d.retryable || (d.http_status === 409 && RETRYABLE_CLAIM_ERRORS.has(d.hub_error))));
}
// model_unsupported: the route cannot serve this intent's model/tier (plan()
// tier exclusion or a recorded route refusal); it never frees up, so it must not
// turn a capacity wait into a hard 409.
const INCIDENTAL_REASONS = new Set(['model_unsupported', 'machine_unavailable', 'provider_disabled', 'engine_unavailable', 'tier_unavailable', 'goal_unsupported', 'engine_auth_expired', 'engine_tool_permission_refused', 'admission_unconfigured']);
/** Capacity-only placement failure: every rejected route reports transient
 * capacity (account/machine), optionally alongside incidental stale/disabled
 * routes (drained/stale check-ins, policy-disabled engines, registry routes
 * for engines no live host advertises) — but incidentals alone never queue.
 * Routes excluded by the requested tier or goal capability cannot serve this
 * intent even after capacity frees, so they are incidental too. Their slugs
 * remain visible; without a capacity rejection they never queue alone.
 * Any other class (workspace/mode capability, admission, resources, quota
 * evidence) stays a hard reject with its slug visible. */
export function isCapacityOnlyHold(rejected: unknown): rejected is { machine: string; engine: string; reason: string }[] {
  if (!Array.isArray(rejected) || rejected.length === 0) return false;
  let capacity = false;
  for (const entry of rejected) {
    const reason = (entry as any)?.reason;
    if (CAPACITY_REASONS.has(reason)) { capacity = true; continue; }
    if (INCIDENTAL_REASONS.has(reason)) continue;
    return false;
  }
  return capacity;
}
const HEADERS = { 'Cache-Control': 'private, no-store' };
export function overflowMachine(machine: any, needs: string[]): boolean {
  return String(machine?.os ?? '').trim().toLowerCase() === 'windows' && !needs.includes('os:windows');
}
function response(value: unknown, status = 200) { return Response.json(value, { status, headers: HEADERS }); }
function label(value: unknown): value is string { return typeof value === 'string' && LABEL.test(value); }
/** Absolute POSIX workspace identifiers, accepted ALONGSIDE existing labels.
 *
 * An absolute workspace is never a free-form path: it must be a bounded
 * canonical path (no NUL/control chars, no backslashes, no `//`, no trailing
 * slash, no `.`/`..` segments, segments confined to `[A-Za-z0-9._-]{1,64}`).
 * Acceptance additionally requires an explicit trusted mapping on BOTH ends:
 * the host must advertise the exact string in its capabilities `workspaces`
 * (from its operator-configured settings) and the launch route must match a
 * configured route. Unadvertised or unmapped paths are refused before any
 * claim or native effect. Host realpath/CWD confinement stays authoritative. */
const WORKSPACE_SEG = /^[A-Za-z0-9._-]{1,64}$/;
const NEEDS_LABEL = /^[a-z0-9][a-z0-9:_-]{0,95}$/;
const WINDOWS_RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
  'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
  'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
]);
function absoluteWorkspaceOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 512) return false;
  if (/[\x00-\x1f\x7f]/.test(value)) return false;
  if (value.startsWith('/')) {
    if (value.includes('\\')) return false;
    const parts = value.split('/');
    if (parts[0] !== '' || parts.length < 2) return false;
    return parts.slice(1).every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && WORKSPACE_SEG.test(seg));
  }
  if (/^[A-Za-z]:[\\/]/.test(value)) {
    if (value.includes('\\\\') || value.includes('//') || value.includes('\\/') || value.includes('/\\')) return false;
    if (value.endsWith('\\') || value.endsWith('/')) return false;
    const tail = value.slice(3);
    if (!tail) return false;
    const parts = tail.split(/[\\/]/);
    for (const seg of parts) {
      if (!seg || seg === '.' || seg === '..' || seg.endsWith(' ') || seg.endsWith('.')) return false;
      if (/[\x00-\x1f\x7f<>"|?*:]/.test(seg)) return false;
      const base = seg.split('.')[0].toUpperCase();
      if (WINDOWS_RESERVED.has(base)) return false;
    }
    return true;
  }
  return false;
}
/** A workspace identifier is either a configured label or an explicitly
 * allowed absolute POSIX path. Labels keep their exact legacy meaning. */
function workspaceOk(value: unknown): value is string {
  return label(value) || absoluteWorkspaceOk(value);
}
/** Hub-side shape check for a requested working directory relative to the
 * workspace root: relative, confined (no traversal), bounded segments. The
 * host resolves it against allowlisted canonical roots with symlink
 * confinement before any native effect. */
function relativeCwdOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 256) return false;
  if (value.startsWith('/') || value.includes('\\') || value.includes(':') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value === '.' || value.split('/').every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
}
function offerCwdOk(value: unknown): value is string {
  if (relativeCwdOk(value)) return true;
  if (typeof value !== 'string' || !absoluteWorkspaceOk(value)) return false;
  return value.startsWith('/') ? value.split('/').length >= 3 : value.slice(3).split(/[\\/]/).length >= 2;
}
function object(value: any, keys: string[]): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => keys.includes(k));
}
function parsed(value: any, fallback: any = {}) { try { return JSON.parse(value || '{}'); } catch { return fallback; } }
const NATIVE_AMOUNTS: Record<string, number> = { credits_remaining: 1e12, spend_used: 1e15, spend_limit: 1e15 };
const NATIVE_UNITS = new Set(['percent', 'credits', 'usd_cents', 'requests', 'tokens']);
const PROFILE_LABEL = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$/;
const COLLECTOR_VERSION = /^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$/;
const UTC_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$/;
function nativeAmount(value: any, cap: number): boolean {
  return value === null || value === undefined || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= cap);
}
function sanitizedQuota(raw: any): any {
  const topKeys = ['status', 'observed_at', 'details', 'windows'];
  const hasCv = !!raw && typeof raw === 'object' && !Array.isArray(raw) && (raw as any).collector_version !== undefined;
  if (!object(raw, hasCv ? [...topKeys, 'collector_version'] : topKeys) || !['ok', 'unknown', 'error'].includes(raw.status) || !object(raw.details, Object.keys(raw.details)) || !object(raw.windows, Object.keys(raw.windows || {})) || Object.keys(raw.windows).length > 32) return null;
  if (raw.collector_version !== undefined && raw.collector_version !== null && (typeof raw.collector_version !== 'string' || !COLLECTOR_VERSION.test(raw.collector_version))) return null;
  const details: any = {};
  for (const [key, value] of Object.entries(raw.details)) {
    if (['limit_reached', 'has_grok_code_access', 'stale'].includes(key)) {
      if (value !== null && typeof value !== 'boolean') return null;
    } else if (key in NATIVE_AMOUNTS) {
      if (!nativeAmount(value, NATIVE_AMOUNTS[key])) return null;
    } else if (key === 'unit') {
      if (value !== null && (typeof value !== 'string' || !NATIVE_UNITS.has(value))) return null;
    } else if (key === 'profile') {
      if (value !== null && (typeof value !== 'string' || !PROFILE_LABEL.test(value))) return null;
    } else if (key === 'cooldown_until') {
      if (value !== null && (typeof value !== 'string' || value.length > 64 || !UTC_ISO.test(value) || !Number.isFinite(Date.parse(value)))) return null;
    } else if (key === 'banked_resets') {
      if (!Array.isArray(value) || value.length > 32 || value.some(g =>
        !object(g, ['expires_at', 'available', 'label']) || typeof g.available !== 'boolean' ||
        typeof g.expires_at !== 'string' || !UTC_ISO.test(g.expires_at) || !Number.isFinite(Date.parse(g.expires_at)) ||
        typeof g.label !== 'string' || !/^[A-Za-z0-9 ._-]{0,80}$/.test(g.label))) return null;
    } else return null;
    details[key] = value;
  }
  const windows: any = {};
  for (const [key, w] of Object.entries(raw.windows) as any) {
    if (!label(key) || !object(w, Object.keys(w)) || typeof w.present !== 'boolean' || (w.percent_remaining !== null && (typeof w.percent_remaining !== 'number' || !Number.isFinite(w.percent_remaining) || w.percent_remaining < 0 || w.percent_remaining > 100)) || (w.reset_at !== null && (typeof w.reset_at !== 'string' || !Number.isFinite(Date.parse(w.reset_at)))) || (w.rolling !== undefined && typeof w.rolling !== 'boolean') || (w.error !== undefined && w.error !== null && w.error !== 'unavailable')) return null;
    for (const nk of Object.keys(NATIVE_AMOUNTS)) if (!nativeAmount(w[nk], NATIVE_AMOUNTS[nk])) return null;
    if (w.unit !== undefined && w.unit !== null && (typeof w.unit !== 'string' || !NATIVE_UNITS.has(w.unit))) return null;
    if (w.profile !== undefined && w.profile !== null && (typeof w.profile !== 'string' || !PROFILE_LABEL.test(w.profile))) return null;
    for (const k of Object.keys(w)) if (!['present', 'percent_remaining', 'reset_at', 'rolling', 'error', ...Object.keys(NATIVE_AMOUNTS), 'unit', 'profile'].includes(k)) return null;
    const nativesNull = w.credits_remaining == null && w.spend_used == null && w.spend_limit == null && w.unit == null && w.profile == null;
    if (!w.present && (w.percent_remaining !== null || w.reset_at !== null || !nativesNull)) return null;
    windows[key] = { present: w.present, percent_remaining: w.percent_remaining, reset_at: w.reset_at, rolling: w.rolling === true, error: w.error === 'unavailable' ? 'unavailable' : null,
      credits_remaining: w.credits_remaining ?? null, spend_used: w.spend_used ?? null, spend_limit: w.spend_limit ?? null, unit: w.unit ?? null, profile: w.profile ?? null };
  }
  if (raw.observed_at !== null && (typeof raw.observed_at !== 'string' || !Number.isFinite(Date.parse(raw.observed_at)))) return null;
  return { status: raw.status, observed_at: raw.observed_at, details, windows, collector_version: raw.collector_version ?? null };
}
function fresh(value: any, now: number, ttl: number) {
  const at = typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? Date.parse(value) : NaN;
  return Number.isFinite(at) && at <= now && now - at <= ttl;
}
async function body(request: Request, maxBytes = 32768): Promise<any> {
  if (!request.body) return null;
  const reader = request.body.getReader(); let size = 0; const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.byteLength;
      if (size > maxBytes) { await reader.cancel(); return null; }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return null; } finally { reader.releaseLock(); }
}


/** Exact workspace id or boundary-safe descendant of an advertised root. */
function workspaceCapable(capsWorkspaces: any, request: any): boolean {
  if (!Array.isArray(capsWorkspaces) || typeof request !== 'string' || !request) return false;
  for (const w of capsWorkspaces) {
    if (typeof w !== 'string' || !w) continue;
    if (w === request) return true;
    // Windows roots use backslash separators; prefix matching must follow
    // the advertised separator style, never a POSIX-only assumption.
    const sep = w.includes('\\') ? '\\' : '/';
    const prefix = w.endsWith('/') || w.endsWith('\\') ? w : w + sep;
    if (request.startsWith(prefix)) return true;
  }
  return false;
}

/** Resolve a bare workspace label via an advertised absolute root.
 *
 * Hosts advertise `workspace_roots` as absolute prefixes (e.g.
 * `/home/alexey/git`) while callers use bare labels (e.g.
 * `dataops-knowledge`). Exact/prefix matching alone 409s that class with
 * `capability_unavailable` and pressures operators into per-repo
 * `subscriptions.json` map edits — the footgun. Instead, when the request
 * is a bare label that is not already capable, rewrite it to
 * `{advertised_absolute_root}/{label}` using the first advertised absolute
 * entry (in advertised order) whose candidate is a bounded canonical
 * absolute that passes `workspaceCapable`. Absolute requests are never
 * rewritten. When no absolute root yields a capable candidate, the request
 * is returned unchanged so placement still fails closed with
 * `capability_unavailable`.
 *
 * The rewritten absolute is what the host `resolve_workspace_path`
 * accepts (identity mapping under configured roots); the host's
 * realpath/CWD confinement stays authoritative. */
export function resolveLabelUnderRoots(capsWorkspaces: any, request: any): string {
  if (typeof request !== 'string' || !request) return request;
  if (!LABEL.test(request)) return request;
  if (workspaceCapable(capsWorkspaces, request)) return request;
  if (!Array.isArray(capsWorkspaces)) return request;
  for (const w of capsWorkspaces) {
    if (typeof w !== 'string' || !w) continue;
    // Advertised roots may carry trailing separators (operator map values
    // only require absoluteness, unlike workspace_roots): normalize before
    // the canonical check so a trailing slash never blocks label
    // resolution. The candidate itself is always built canonical.
    const stripped = w.length > 1 ? w.replace(/[/\\]+$/, '') : w;
    if (!stripped || !absoluteWorkspaceOk(stripped)) continue;
    const sep = stripped.includes('\\') ? '\\' : '/';
    const candidate = stripped + sep + request;
    if (candidate.length > 512) continue;
    if (!absoluteWorkspaceOk(candidate)) continue;
    if (workspaceCapable(capsWorkspaces, candidate)) return candidate;
  }
  return request;
}

export class FleetService {
  public routing: RoutingService;
  public resources: ResourceManagement;
  public admission: ClaimAdmission;
  private maintenance: { reconciled: boolean; drained: boolean; queueLimit:number } | null = null;
  // Intake 488b860e: rotation point for re-offered effect-free starting
  // launches, so no row starves behind others regardless of age order.
  // The cursor is a value in the ORDER BY key (updated_at, id), never a
  // row position, so concurrent inserts, removals and wraps stay exact.
  private reofferCursors = new Map<string, [string, string]>();

  /** Synchronous operation scope: nested routing/reserve/plan calls share work,
   * never capacity snapshots. No await may cross this scope. */
  withMaintenance<T>(fn: () => T, queueLimit=2): T {
    if (this.maintenance) return fn();
    this.maintenance = { reconciled: false, drained: false, queueLimit };
    try { return fn(); } finally { this.maintenance = null; }
  }

  reconcileCapacity() {
    return this.withMaintenance(() => {
      if (this.maintenance!.reconciled) return;
      this.admission.reconcileOffers();
      reconcileAccountCapacity(this.sql, this.now());
      reconcileUnboundFleetSessions(this.sql, this.now());
      this.maintenance!.reconciled = true;
    });
  }

  constructor(private sql: SqlStorage, private transaction: <T>(fn: () => T) => T, private now: () => number = Date.now) {
    this.resources = new ResourceManagement(sql, now);
    this.routing = new RoutingService(sql, this, now, transaction);
    this.admission = new ClaimAdmission(sql, transaction, now);
  }
  init() {
    this.resources.init();
    for (const stmt of QUOTA_SCHEMA_SQL.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    ensureTbmQuotaSchema(this.sql);
    this.routing.init();
    this.admission.init();
    for (const stmt of FLEET_SCHEMA.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    const holdColumns = this.sql.exec('PRAGMA table_info(fleet_memory_holds)').toArray().map((c: any) => c.name);
    if (!holdColumns.includes('evidence_json')) this.sql.exec('ALTER TABLE fleet_memory_holds ADD COLUMN evidence_json TEXT');
    initResourceRequestProjection(this.sql, 'fleet_launches');
    const launchColumns = this.sql.exec('PRAGMA table_info(fleet_launches)').toArray().map((c: any) => c.name);
    for (const name of ['verification_status', 'exit_reason', 'finished_at', 'launch_error_json', 'model_selection_json', 'retried_launch_id', 'claim_key', 'runtime_json']) {
      if (!launchColumns.includes(name)) this.sql.exec(`ALTER TABLE fleet_launches ADD COLUMN ${name} TEXT`);
    }
    this.sql.exec("CREATE INDEX IF NOT EXISTS fleet_offered ON fleet_launches(id) WHERE state = 'offered'");
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_state ON fleet_launches(state)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_machine_state ON fleet_launches(machine_id,state)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_expiry ON fleet_launches(state,created_at)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_account_state ON fleet_launches(account,state)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_session_account_phase ON fleet_sessions(account,phase)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_native_state ON fleet_launches(machine_id,native_id,state)');
    this.sql.exec("CREATE INDEX IF NOT EXISTS fleet_launch_inflight_identity ON fleet_launches(machine_id,engine,account,profile) WHERE native_id IS NULL AND state IN ('starting','uncertain')");
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_session_incarnation ON fleet_sessions(machine_id,tag,engine,account,profile,workspace,phase)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_route_account ON fleet_routes(account)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS routing_launch ON routing_jobs(launch_id)');
    const columns = this.sql.exec('PRAGMA table_info(fleet_sessions)').toArray().map((c: any) => c.name);
    if (!columns.includes('last_activity_at')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN last_activity_at TEXT');
    if (!columns.includes('cwd')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN cwd TEXT');
    if (!columns.includes('created_at_ms')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN created_at_ms INTEGER');
    if (!columns.includes('progress_json')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN progress_json TEXT');
  }

  public observationProvenance(account: any, route: any): { quota: any; subscriptionObservedAt: string | null } | null {
    const provider = canonicalQuotaProvider(account.provider);
    const label = canonicalAccountLabel(account.label);
    const legacyLabel = Object.entries(LEGACY_ACCOUNT_LABELS).find(([, c]) => c === label)?.[0] ?? label;
    const central: any = this.sql.exec('SELECT * FROM server_quota_snapshots WHERE (account = ? OR account = ?) AND (provider = ? OR provider = ?)', label, legacyLabel, provider, legacyRouteKeyFor(provider) ?? provider).toArray()[0];
    if (central && fresh(central.captured_at, this.now(), QUOTA_STALE_MS - 1)) {
      const quota = parsed(central.quota_json);
      if (!quota.observed_at || fresh(quota.observed_at, this.now(), QUOTA_STALE_MS - 1)) {
        return { quota, subscriptionObservedAt: central.captured_at };
      }
    }
    const subscription: any = this.sql.exec('SELECT * FROM fleet_subscriptions WHERE machine_id = ? AND account = ?', route.machine_id, account.label).toArray()[0];
    if (subscription) {
      if (canonicalQuotaProvider(subscription.service) !== provider || !fresh(subscription.observed_at, this.now(), 1200000)) return null;
      const quota = parsed(subscription.quota_json);
      const quotaObs = quota?.observed_at || subscription.observed_at;
      return fresh(quotaObs, this.now(), 1200000) ? { quota: quota?.observed_at ? quota : { ...quota, observed_at: quotaObs }, subscriptionObservedAt: subscription.observed_at } : null;
    }
    // A default CLI reading cannot describe a named profile or two accounts on one service.
    if (route.profile !== null && route.profile !== provider) return null;
    const count = Number(this.sql.exec('SELECT COUNT(DISTINCT r.account) AS n FROM fleet_routes r JOIN fleet_accounts a ON r.account = a.label WHERE r.machine_id = ? AND (a.provider = ? OR a.provider = ?)', route.machine_id, provider, legacyRouteKeyFor(provider) ?? provider).one().n);
    const snapshot: any = this.sql.exec('SELECT * FROM host_quota_snapshots WHERE host_id = ?', route.machine_id).toArray()[0];
    if (count !== 1 || !snapshot || !fresh(snapshot.captured_at, this.now(), 1200000)) return null;
    const quota = snapshotRouteForAccount(account.label, provider, parsed(snapshot.routes_json));
    const obsTs = quota?.observed_at || snapshot.captured_at;
    if (!quota || !fresh(obsTs, this.now(), 1200000)) return null;
    const quotaWithTs = quota.observed_at ? quota : { ...quota, observed_at: obsTs };
    return { quota: snapshot.status === 'ok' ? quotaWithTs : { ...quotaWithTs, status: 'unknown' }, subscriptionObservedAt: snapshot.captured_at };
  }

  private observation(account: any, route: any) {
    return this.observationProvenance(account, route)?.quota ?? null;
  }

  private reconciliationCandidates(machine: string) {
    // Hosts report their complete catalog before polling offers. Avoid one
    // redundant native retirement probe per fresh live workload each cycle;
    // stale, absent and ambiguous observations still need positive proof.
    const cutoff = new Date(this.now() - 180000).toISOString();
    const catalogFresh = freshCatalogs(this.sql, this.now()).has(machine);
    // Fleet-only stores need no control table. Probe once, not once per row.
    const controlStore = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
    const bindingProjection = controlStore
      ? '(SELECT binding_json FROM session_control_sessions c WHERE c.fleet_launch_id=l.id AND c.machine_id=l.machine_id LIMIT 1)'
      : 'NULL';
    return this.sql.exec(`SELECT l.id, l.workspace, ${jsonFields('l.request_json',['cwd'])} AS request_json, l.launch_error_json, l.native_id, l.engine, l.mode, l.launcher_task_id, ${bindingProjection} AS binding_json
      FROM fleet_launches l WHERE l.machine_id = ? AND l.state IN ('starting','running','uncertain')
      AND (l.native_id IS NULL OR NOT EXISTS (SELECT 1 FROM fleet_sessions s
        WHERE s.machine_id = l.machine_id AND s.native_id = l.native_id AND s.engine = l.engine
        AND s.phase IN ('starting','running') AND (s.observed_at >= ? OR ?)))
      ORDER BY l.updated_at LIMIT 100`, machine, cutoff, catalogFresh ? 1 : 0).toArray().map((r: any) => {
      const binding = controlStore ? parsed(r.binding_json) : null;
      return { id: r.id, workspace: r.workspace, cwd: parsed(r.request_json).cwd ?? null, diagnostic: parsed(r.launch_error_json), native_id: r.native_id, engine: r.engine, mode: r.mode, launcher_task_id: r.launcher_task_id, binding };
    });
  }

  accountStatus(account: any) {
    return this.withMaintenance(() => this.accountStatusWithinMaintenance(account));
  }

  private accountStatusWithinMaintenance(account: any) {
    this.reconcileCapacity();
    // Canonical provider model: `codex-orin` is an account under provider
    // `codex`, never a separate provider. Stored rows keyed by old names
    // canonicalize here so grouping by provider shows both Codex accounts.
    const provider = canonicalQuotaProvider(account.provider);
    const routes = this.sql.exec('SELECT machine_id, engine, profile FROM fleet_routes WHERE account = ?', account.label).toArray();
    let remaining: number | null = null; let observed = 0; let unknown = false; let blocked = !account.enabled; let healthy = false;
    const reserves = (((policy as any).rules as any).reserve_percent || {}) as Record<string, number>;
    for (const route of routes) {
      const prov = this.observationProvenance(account, route);
      if (!prov) continue;
      observed++;
      const adm = evaluateLaunchQuota({ provider, quota: prov.quota, subscriptionObservedAt: prov.subscriptionObservedAt, nowMs: this.now(), reserves });
      if (!adm.ok) {
        if (['limit_reached', 'cooldown_active'].includes(adm.reason)) blocked = true;
        else if (adm.reason === 'reserve') {
          if (adm.remaining !== null) remaining = remaining === null ? adm.remaining : Math.min(remaining, adm.remaining);
        } else unknown = true;
        continue;
      }
      if (adm.remaining !== null) {
        remaining = remaining === null ? adm.remaining : Math.min(remaining, adm.remaining);
        healthy = true;
      } else unknown = true;
    }
    if (remaining !== null && remaining <= account.reserve_percent) blocked = true;
    // Capacity counts only live machines (Hetzner urgent live, legacy stale):
    // sessions and reservations on drained or stale-checkin machines must not
    // block the shared pool for the live host. Stale offered/starting/uncertain
    // reservations (no update for 48h) never had a confirmed native start and
    // must not hold quota forever; running launches may be long-lived and stay.
    // Both bound and orphan sessions idle past the configured threshold
    // release admission slots; parked PTYs must not pin account capacity.
    const liveIds = new Set<string>();
    const liveEnginesByMachine = new Map<string, Set<string>>();
    try {
      for (const m of this.sql.exec('SELECT id, last_checkin_at, drained, capabilities FROM machines').toArray() as any[]) {
        if (m.drained) continue;
        if (!fresh(effectiveCheckin(m.id, m.last_checkin_at), this.now(), 180000)) continue;
        liveIds.add(String(m.id));
        // Same advertised-engine rule as plan(): a route stays placeable only
        // while its machine's fresh capabilities still list the engine. A host
        // release that drops a subscription stops serving the engine, and its
        // leftover registry routes must not read as placeable capacity.
        const engines = parsed(m.capabilities)?.engines;
        if (Array.isArray(engines)) liveEnginesByMachine.set(String(m.id), new Set(engines.filter((e: unknown) => typeof e === 'string')));
      }
    } catch { /* fail closed below when the live set is empty */ }
    const liveList = [...liveIds];
    const staleCutoff = new Date(this.now() - 48 * 3600000).toISOString();
    const idleCutoff = this.now() - this.resources.config().idle_minutes * 60000;
    const catalogs = freshCatalogs(this.sql, this.now());
    const isIdle = (row: any) => row.phase === 'running' && catalogs.has(String(row.machine_id))
      && typeof row.last_activity_at === 'string' && Number.isFinite(Date.parse(row.last_activity_at))
      && Date.parse(row.last_activity_at) <= idleCutoff;
    // Native activity, rather than a launch receipt's update timestamp,
    // determines whether a bound running session still occupies admission.
    // Stale catalogs, unknown native state/activity and in-flight starts
    // retain their reservations, even while independent heartbeats are fresh.
    const reserved = liveList.length
      ? (this.sql.exec(`SELECT l.state, l.machine_id, s.phase, s.last_activity_at FROM fleet_launches l
          LEFT JOIN fleet_sessions s ON s.machine_id=l.machine_id AND s.native_id=l.native_id
          WHERE l.account = ? AND l.state IN ${ACTIVE} AND l.machine_id IN (${liveList.map(() => '?').join(',')})
          AND (l.state = 'running' OR l.updated_at > ?)`, account.label, ...liveList, staleCutoff).toArray() as any[])
          .filter(row => row.state !== 'running' || !isIdle(row)).length
      : 0;
    const observedSessions = (() => {
      if (!liveList.length) return 0;
      const orphans = this.sql.exec(`SELECT s.machine_id, s.phase, s.last_activity_at FROM fleet_sessions s WHERE s.account = ? AND s.phase IN ('starting','running','broken','unknown','uncertain') AND s.machine_id IN (${liveList.map(() => '?').join(',')}) AND ${NATIVE_UNRESERVED}`, account.label, ...liveList).toArray() as any[];
      let counted = 0;
      for (const row of orphans) {
        if (!isIdle(row)) counted++;
      }
      return counted;
    })();
    const active = reserved + observedSessions;
    // Visibility breakdown (no accounting change): counted slots split into
    // launch reservations vs orphan observed sessions, alongside live native
    // processes verified by the host catalog for this account. The sibling
    // capacity fixer owns the counting; this only projects it.
    let nativeLive: number | null = null;
    try {
      const seen = freshCatalogs(this.sql, this.now());
      if (liveList.length && liveList.every(id => seen.has(id))) nativeLive = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_sessions s WHERE s.account = ? AND s.phase = 'running' AND s.machine_id IN (${liveList.map(() => '?').join(',')})`, account.label, ...liveList).one().n);
    } catch { nativeLive = null; }
    const availableSlots = Math.max(0, Number(account.max_concurrency) - active);
    const placeableMachines = new Set<string>();
    const placementReasons = new Set<string>();
    if (!routes.length) placementReasons.add('no_routes');
    if (!availableSlots) placementReasons.add('account_concurrency');
    let placeableRoutes = 0;
    for (const route of routes) {
      if (liveEnginesByMachine.get(String(route.machine_id))?.has(String(route.engine))) {
        placeableRoutes++;
        placeableMachines.add(String(route.machine_id));
      } else placementReasons.add(liveIds.has(String(route.machine_id)) ? 'engine_unavailable' : 'machine_unavailable');
    }
    return { label: canonicalAccountLabel(account.label), provider, max_concurrency: account.max_concurrency, reserve_percent: account.reserve_percent, enabled: !!account.enabled, supports_goal: ['codex', 'zai'].includes(provider),
      remaining_percent: remaining, status: blocked ? 'blocked' : (!observed || remaining === null || (!healthy && unknown)) ? 'unknown' : 'ok', active, available_slots: availableSlots,
      // Counted vs live: active splits into launch reservations + orphan
      // observed sessions; native_live is the host-catalog verified live
      // process count for this account (includes launch-linked sessions).
      reserved, observed_sessions: observedSessions, native_live: nativeLive,
      // available_slots is the raw pool and ignores route liveness: a registry
      // whose routes all point at engines no live host serves anymore (e.g.
      // after a host subscription change) used to advertise phantom free
      // slots. placeable_slots counts only capacity reachable through a live
      // route; status stays quota health — placement classes live in plan().
      placeable_slots: placeableMachines.size ? availableSlots : 0, placeable_machines: [...placeableMachines].sort(),
      placement_reasons: [...placementReasons].sort(),
      registry_drift: routes.length > 0 && placeableRoutes < routes.length,
      machines: routes.map(r => r.machine_id) };
  }

  /** Per-host launcher memory reservation projection for the fleet dashboard.
   *
   * Read-only visibility over the sibling fixer's accounting: MemAvailable
   * (real, from host heartbeat), reserved total
   * (launcher_resources.active_mem_mb), the 10 GiB floor plus one next slot,
   * and the resulting headroom. The gate mirrors plan(): headroom after the
   * next slot below the floor refuses with launcher_memory_reserve. Stale
   * launcher telemetry reads fail closed (stale_telemetry) exactly like
   * placement does. */
  hostResources(machineFilter?: string): any[] {
    return this.resources.withReadSnapshot(()=>this.hostResourcesWithinSnapshot(machineFilter));
  }
  private hostResourcesWithinSnapshot(machineFilter?: string): any[] {
    let machines: any[] = [];
    try {
      machines = this.sql.exec(`SELECT id, last_checkin_at, drained, capabilities, last_heartbeat FROM machines ${machineFilter ? 'WHERE id=?' : ''} ORDER BY id`,...(machineFilter ? [machineFilter] : [])).toArray() as any[];
    } catch { return []; }
    const out: any[] = [];
    for (const m of machines) {
      const id = String(m.id);
      if (machineFilter && id !== machineFilter) continue;
      let hb: any = {};
      try { hb = JSON.parse(m.last_heartbeat || '{}'); } catch { hb = {}; }
      const memAvailMb = typeof hb.mem_avail_mb === 'number' && Number.isFinite(hb.mem_avail_mb) ? hb.mem_avail_mb : null;
      const launcher = hb.launcher_resources && typeof hb.launcher_resources === 'object' ? hb.launcher_resources : null;
      const reservedMb = launcher && typeof launcher.active_mem_mb === 'number' && Number.isFinite(launcher.active_mem_mb) && launcher.active_mem_mb >= 0 ? launcher.active_mem_mb : null;
      const observedAt = launcher && typeof launcher.observed_at === 'string' ? launcher.observed_at : null;
      let caps: any = {};
      try { caps = JSON.parse(m.capabilities || '{}'); } catch { caps = {}; }
      const launcherManaged = caps.launcher_resources === true;
      const telemetryFresh = launcherManaged
        ? (reservedMb !== null && observedAt !== null && fresh(observedAt, this.now(), 180000))
        : true;
      const memory = this.memoryHeadroom(id);
      const headroomMb = memory.mem_available_mb !== null && reservedMb !== null && telemetryFresh
        ? memory.mem_available_mb - memory.reserved_mb - memory.requested_mb
        : null;
      out.push({
        machine_id: id,
        mem_avail_mb: memAvailMb,
        reserved_mb: telemetryFresh && reservedMb !== null ? memory.reserved_mb : null,
        floor_mb: memory.floor_mb,
        next_slot_mb: memory.requested_mb,
        headroom_mb: headroomMb,
        headroom_ok: headroomMb === null ? null : headroomMb >= memory.floor_mb,
        launcher_managed: launcherManaged,
        stale_telemetry: launcherManaged ? !telemetryFresh : false,
        observed_at: observedAt || m.last_checkin_at || null,
      });
    }
    return out;
  }

  /** Recent placement-pressure evidence for dashboards and alerts.
   *
   * Counts queued holds, hard rejections recorded at reserve time, and
   * recent terminal failures with memory/capacity reasons. Read-only; the
   * accounting verdicts stay with plan()/accountStatus(). */
  placementOverview(): { queued_held: number; queued_total: number; recent_rejects: number; recent_failed: number } {
    const nowMs = this.now();
    let queuedTotal = 0, queuedHeld = 0, recentRejects = 0, recentFailed = 0;
    try {
      queuedTotal = Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state = 'queued'").one().n);
      const heldBefore = new Date(nowMs - 5 * 60000).toISOString();
      queuedHeld = Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state = 'queued' AND created_at <= ?", heldBefore).one().n);
    } catch { /* fail closed: pressure unknown reads as none here; liveness re-checks */ }
    try {
      const dayAgo = nowMs - 24 * 3600000;
      recentRejects = Number(this.sql.exec('SELECT COUNT(*) AS n FROM placement_rejections WHERE created_at_ms >= ?', dayAgo).one().n);
    } catch { recentRejects = 0; }
    try {
      const dayAgoIso = new Date(nowMs - 24 * 3600000).toISOString();
      const rows = this.sql.exec("SELECT exit_reason, launch_error_json FROM fleet_launches WHERE state = 'failed' AND updated_at >= ?", dayAgoIso).toArray() as any[];
      for (const row of rows) {
        const reasons: string[] = [];
        if (typeof row.exit_reason === 'string') reasons.push(row.exit_reason);
        try {
          const diag = JSON.parse(row.launch_error_json || 'null');
          if (diag && typeof diag.reason === 'string') reasons.push(diag.reason);
        } catch { /* ignore unparseable diagnostics */ }
        if (reasons.some((r) => PLACEMENT_PRESSURE_REASONS.has(r))) recentFailed++;
      }
    } catch { recentFailed = 0; }
    return { queued_held: queuedHeld, queued_total: queuedTotal, recent_rejects: recentRejects, recent_failed: recentFailed };
  }

  /** Record a hard placement rejection for operator alerting.
   *
   * Only called when reserve() refuses with memory/capacity reasons in the
   * rejected set. Best-effort observability row; never affects placement. */
  private recordPlacementRejection(actor: string, rejected: { machine: string; engine: string; reason: string }[]): void {
    try {
      const reasons = [...new Set(rejected.map((r) => String(r.reason)))].sort();
      const pressure = reasons.filter((r) => PLACEMENT_PRESSURE_REASONS.has(r));
      if (!pressure.length) return;
      const nowMs = this.now();
      const at = new Date(nowMs).toISOString();
      const machines = pressure.includes('launcher_memory_reserve') ? this.hostResources() : [];
      let psiNote = '';
      try {
        const placeholders = machines.map(() => '?').join(',');
        const heartbeats = machines.length
          ? this.sql.exec(`SELECT id, last_heartbeat FROM machines WHERE id IN (${placeholders})`, ...machines.map((h) => h.machine_id)).toArray() as any[]
          : [];
        const parts = heartbeats.map((m) => {
          const usage = sanitizeCpuUsage(parsed(m.last_heartbeat).cpu_usage);
          return `${m.id}: psi_cpu_full=${usage?.psi_full_avg10 ?? 'unknown'}% psi_memory_full=${usage?.memory_psi_full_avg10 ?? 'unknown'}% psi_io_full=${usage?.io_psi_full_avg10 ?? 'unknown'}% observed_at=${usage?.observed_at ?? 'unknown'}`;
        });
        if (parts.length) psiNote = ' ' + parts.join('; ');
      } catch { /* PSI evidence is best-effort */ }
      const memNote = machines.length
        ? ' host ' + machines.map((h) => `${h.machine_id}: mem_avail_mb=${h.mem_avail_mb ?? 'unknown'} reserved_mb=${h.reserved_mb ?? 'unknown'} floor_mb=${MEMORY_FLOOR_MB} next_slot_mb=${NEXT_SLOT_MB} headroom_mb=${h.headroom_mb ?? 'unknown'}`).join('; ') + psiNote
        : '';
      const detail = `rejected [${pressure.join(', ')}] (all routes: ${reasons.join(', ')})${memNote}`;
      this.sql.exec('INSERT OR IGNORE INTO placement_rejections (id, created_at, created_at_ms, actor, reasons_json, detail) VALUES (?, ?, ?, ?, ?, ?)',
        crypto.randomUUID(), at, nowMs, String(actor).slice(0, 128), JSON.stringify(reasons).slice(0, 1024), detail.slice(0, 2048));
    } catch { /* alerting never breaks placement */ }
  }

  plan(input: any) {
    return this.withMaintenance(() => this.planWithinMaintenance(input));
  }

  private planWithinMaintenance(input: any):
    { error: string; request?: undefined; selected?: undefined; candidates?: undefined; rejected?: undefined } |
    { error?: undefined; request: any; selected: any; candidates: any[]; rejected: { machine: string; engine: string; reason: string; memory?: any }[]; evidence: Record<string, any> } {
    if (!object(input, ['mode', 'machine', 'engine', 'engine_override_reason', 'tier', 'account', 'workspace', 'cwd', 'prompt', 'idempotency_key', 'parent_id', 'summary', 'access', 'approved_action', 'with_goal', 'task_kind', 'project', 'needs']) ||
      (input.project !== undefined && (typeof input.project !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(input.project))) ||
      (input.needs !== undefined && (!Array.isArray(input.needs) || input.needs.length > 64 || input.needs.some((n: any) => typeof n !== 'string' || !NEEDS_LABEL.test(n)) || new Set(input.needs).size !== input.needs.length)) ||
      (input.task_kind !== undefined && !['implement','review','ask','goal'].includes(input.task_kind)) ||
      !workspaceOk(input.workspace) || (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) ||
      (input.account !== undefined && !label(input.account)) || (input.machine !== undefined && !isValidMachineId(input.machine)) || (input.engine !== undefined && !label(input.engine)) ||
      // Optional tier/cwd arrive as null (not just undefined) from the
      // session-control reserve handoff when the client omitted them; null
      // means absent, never a malformed value.
      (input.tier !== undefined && input.tier !== null && (typeof input.tier !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(input.tier))) ||
      (input.cwd !== undefined && input.cwd !== null && !offerCwdOk(input.cwd)) ||
      (input.with_goal !== undefined && (input.with_goal !== true || input.mode !== 'interactive' || (typeof input.prompt !== 'string' || !input.prompt.trim()))) ||
      (input.parent_id !== undefined && input.parent_id !== null && (typeof input.parent_id !== 'string' || !/^[a-f0-9-]{36}$/.test(input.parent_id))) ||
      (input.summary !== undefined && (typeof input.summary !== 'string' || input.summary.length > 240 || /[\x00-\x1f\x7f]/.test(input.summary))) ||
      (input.prompt !== undefined && (typeof input.prompt !== 'string' || input.prompt.length > 16000)) ||
      (input.approved_action !== undefined && (typeof input.approved_action !== 'string' || !input.approved_action.trim() || input.approved_action.length > 12000)) ||
      (input.mode !== 'interactive' && (typeof input.prompt !== 'string' || !input.prompt.trim()))) return { error: 'invalid_launch_request' };
    // Tier resolves through the EXISTING model-routing table (aliases +
    // tier membership), never new ranking: candidates are filtered to routes
    // whose (engine, profile) pair is a configured member of the tier, while
    // the existing score order still decides placement. An explicit engine
    // outside the tier simply matches nothing (honest no_eligible_route).
    const pacing = loadProviderPacing(this.sql, this.now());
    const pools = loadPools(this.sql);
    let tierModels: typeof policy.models | null = null;
    if (input.tier != null) {
      const tier = taskTier({ tier: input.tier });
      if (!tier) return { error: 'invalid_launch_request' };
      const members: string[] = (policy.tiers as any)[tier] || [];
      tierModels = policy.models.filter(m => members.includes(m.id));
    }
    // Model ids are policy selectors, not native executable names. Keep the
    // caller's selector in the canonical intent for idempotency.
    const overrideModels = policy.models.filter(m => m.id === input.engine);
    const request: any = { mode: input.mode || 'headless', machine: input.machine || null, engine: input.engine || null, workspace: input.workspace, cwd: input.cwd ?? null, prompt: input.prompt || '', parent_id: input.parent_id || null, summary: input.summary || (input.mode === 'interactive' ? 'Interactive session' : 'Agent task') };
    if (input.project !== undefined) request.project = input.project;
    if (input.task_kind !== undefined) request.task_kind = input.task_kind;
    if (input.with_goal === true) request.with_goal = true;
    if (input.account !== undefined) request.account = input.account;
    if (input.approved_action !== undefined) request.approved_action = input.approved_action;
    if (input.tier !== undefined) request.tier = input.tier;
    const needs: string[] = Array.isArray(input.needs) ? [...input.needs] : [];
    if (needs.length) request.needs = needs;
    const rejected: { machine: string; engine: string; reason: string; memory?: any }[] = []; const candidates: any[] = [];
    this.reconcileCapacity();
    // Planning below is read-only after reconciliation. Drop the snapshot
    // before reserve/promotion writes so the next plan sees every commitment.
    return this.resources.withReadSnapshot(() => {
    const accounts = new Map<any,ReturnType<FleetService['accountStatus']>>();
    const definitions = new Map(this.sql.exec('SELECT * FROM fleet_accounts').toArray().map(a => [a.label, a]));
    // Route alternatives within this synchronous read-only plan share the
    // same host commitments. Never retain these values across a reservation.
    const memoryByRoute=new Map<string,ReturnType<ResourceManagement['headroom']>>();
    const cpuByMachine=new Map<string,ReturnType<ResourceManagement['cpuHeadroom']>>();
    const diskByWorkspace=new Map<string,ReturnType<FleetService['diskHeadroom']>>();
    // Rejection-time host telemetry (MemAvailable + PSI cpu/memory/io full),
    // captured from the same heartbeat that drove the refusal and persisted
    // with the rejected entry. Status warnings show these values, never a
    // later observation (intake 1bc3add1 AC1).
    const evidenceByMachine=new Map<string,any>();
    // Engines advertised by at least one live (non-drained, fresh-checkin)
    // host. A registry route for an engine no live host runs anymore is
    // stale registry debt, not a workspace/mode defect: it must not block
    // capacity queueing for the engines the live fleet actually runs.
    const liveEngines = new Set<string>();
    try {
      for (const m of this.sql.exec('SELECT id, last_checkin_at, drained, capabilities FROM machines').toArray() as any[]) {
        if (m.drained) continue;
        if (!fresh(effectiveCheckin(m.id, m.last_checkin_at), this.now(), 180000)) continue;
        const engines = parsed(m.capabilities)?.engines;
        if (Array.isArray(engines)) for (const e of engines) if (typeof e === 'string') liveEngines.add(e);
      }
    } catch { /* fail closed below: empty set keeps capability_unavailable */ }
    for (const route of this.sql.exec('SELECT * FROM fleet_routes ORDER BY machine_id, engine').toArray() as any[]) {
      if ((request.account && route.account !== request.account) || (request.machine && route.machine_id !== request.machine) || (request.engine && !overrideModels.length && route.engine !== request.engine)) continue;
      if (overrideModels.length) {
        const provider = definitions.get(route.account)?.provider;
        const family = canonicalQuotaProvider(provider as string);
        if (!overrideModels.some(m => matchesModelRoute(m, route, family))) continue;
      }
      if (tierModels) {
        const provider = definitions.get(route.account)?.provider;
        // Named profiles share policy membership with their registered provider,
        // never merely with an executable (Z.ai also uses the Codex engine).
        const family = canonicalQuotaProvider(provider as string);
        if (!tierModels.some(m => (!overrideModels.length || overrideModels.some(o => o.id === m.id)) && matchesModelRoute(m, route, family))) {
          rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'tier_unavailable' });
          continue;
        }
      }
      if (request.with_goal && !['codex', 'zcodex'].includes(route.engine)) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'goal_unsupported' }); continue; }
      // A disabled execution engine holds all new candidate planning, whatever the account label.
      if (disabledEngines().has(route.engine)) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'provider_disabled' }); continue; }
      // Auth/permission holds are only cleared by a successful launch, which a
      // held engine can never produce. After a cooldown one retrial may pass;
      // a failure re-marks the engine (fresh updated_at) and requeues the launch.
      const engineHealth: any = this.sql.exec("SELECT status, reason, updated_at FROM fleet_engine_health WHERE engine = ? AND (machine_id = ? OR machine_id = '' OR machine_id IS NULL) AND status = 'unavailable'", route.engine, route.machine_id).toArray().find((h: any) => (request.mode === 'headless' || h.reason !== 'engine_tool_permission_refused') &&
        !(RETRIABLE_ENGINE_HOLDS.has(h.reason) && this.now() - Date.parse(h.updated_at) >= ENGINE_HEALTH_RETRY_MS));
      if (engineHealth && engineHealth.status === 'unavailable') {
        rejected.push({ machine: route.machine_id, engine: route.engine, reason: engineHealth.reason || 'engine_auth_expired' });
        continue;
      }
      const machine: any = this.sql.exec('SELECT * FROM machines WHERE id = ?', route.machine_id).toArray()[0];
      const definition=definitions.get(route.account);
      if(definition && !accounts.has(route.account)) accounts.set(route.account,this.accountStatus(definition));
      const account = accounts.get(route.account); const caps = parsed(machine?.capabilities); const hb = parsed(machine?.last_heartbeat);
      // Label filter (Tabo intake AC4): requested needs must be satisfied by
      // the machine's advertised routing needs plus synthesized os:/arch:
      // labels from registration (os=Windows/Linux, arch=arm64/amd64).
      // A start requiring os:windows never lands on Hetzner; when no Windows
      // host is online the intent holds with rejected[] reason needs_unmet.
      if (needs.length) {
        let routingNeeds: unknown = [];
        try {
          const rn: any = this.sql.exec('SELECT needs_json, observed_at FROM routing_machines WHERE machine_id = ?', route.machine_id).toArray()[0];
          if (rn && fresh(rn.observed_at, this.now(), 180000)) routingNeeds = JSON.parse(rn.needs_json);
        } catch { routingNeeds = []; }
        const labels = new Set<string>(Array.isArray(routingNeeds) ? (routingNeeds as unknown[]).filter((n): n is string => typeof n === 'string') : []);
        if (typeof machine?.os === 'string' && machine.os.trim()) labels.add('os:' + machine.os.trim().toLowerCase());
        const archRaw = typeof (machine as any)?.arch === 'string' ? (machine as any).arch.trim().toLowerCase() : '';
        if (archRaw) labels.add('arch:' + archRaw);
        if (!needs.every(n => labels.has(n))) {
          rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'needs_unmet' });
          continue;
        }
      }
      // Bare labels resolve per-route via that machine's advertised absolute
      // roots (`{root}/{label}`); absolutes and already-capable labels pass
      // through unchanged. Each candidate records its effective workspace so
      // the winner's normalized absolute is what gets stored and offered.
      if (request.with_goal && (!Array.isArray(caps.fleet_goal_routes) || !caps.fleet_goal_routes.some((r: any) =>
          r.engine === route.engine && r.account === route.account && (r.profile ?? null) === route.profile))) {
        rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'goal_unsupported' }); continue;
      }
      const effectiveWorkspace = resolveLabelUnderRoots(caps.workspaces, request.workspace);
      const absoluteCwd = typeof request.cwd === 'string' && (request.cwd.startsWith('/') || /^[A-Za-z]:[\\/]/.test(request.cwd)) ? request.cwd : null;
      // Host admission probes the credential actually used for execution.
      // A healthy sibling account must not mask a refusal for this route.
      const gateProjection = Array.isArray(caps.fleet_route_rejections) && caps.fleet_route_rejections.find((r: any) =>
        r.engine === route.engine && r.account === route.account && (r.profile ?? null) === route.profile
        && r.mode === request.mode && /^codex_quota_gate_[a-z0-9_]{1,64}$/.test(r.reason)
        && fresh(r.observed_at, this.now(), 300000));
      const gateRefusal = gateProjection && !['codex_quota_gate_quota_unavailable', 'codex_quota_gate_quota_check_failed', 'codex_quota_gate_stale_window_evidence'].includes(gateProjection.reason) ? gateProjection : null;
      const routeProv = definition ? this.observationProvenance(definition, route) : null;
      const routeQuota = evaluateLaunchQuota({ provider: String(definition?.provider ?? ''), quota: routeProv?.quota ?? null,
        subscriptionObservedAt: routeProv?.subscriptionObservedAt ?? null, nowMs: this.now(),
        reserves: (policy.rules.reserve_percent || {}) as Record<string, number> });
      const memory = () => {
        const key=JSON.stringify([route.machine_id,route.engine]);
        if(!memoryByRoute.has(key)) memoryByRoute.set(key,this.resources.headroom(route.machine_id,request,route.engine));
        return memoryByRoute.get(key)!;
      };
      const cpu = () => {
        if(!cpuByMachine.has(route.machine_id)) cpuByMachine.set(route.machine_id,this.resources.cpuHeadroom(route.machine_id,request));
        return cpuByMachine.get(route.machine_id)!;
      };
      const disk = () => {
        const key=JSON.stringify([route.machine_id,effectiveWorkspace]);
        if(!diskByWorkspace.has(key)) diskByWorkspace.set(key,this.diskHeadroom(route.machine_id,effectiveWorkspace,undefined,request.cwd));
        return diskByWorkspace.get(key);
      };
      const evidence = () => {
        if(!evidenceByMachine.has(route.machine_id)) {
          const usage=sanitizeCpuUsage(hb.cpu_usage);
          evidenceByMachine.set(route.machine_id,{
            mem_avail_mb: Number.isFinite(hb.mem_avail_mb) ? hb.mem_avail_mb : null,
            psi_cpu_full_avg10: usage?.psi_full_avg10 ?? null,
            psi_memory_full_avg10: usage?.memory_psi_full_avg10 ?? null,
            psi_io_full_avg10: usage?.io_psi_full_avg10 ?? null,
            observed_at: usage?.observed_at ?? (typeof hb.at === 'string' ? hb.at : null),
          });
        }
        return evidenceByMachine.get(route.machine_id);
      };
      let reason = '';
      if (!machine || machine.drained || !fresh(effectiveCheckin(machine.id, machine.last_checkin_at), this.now(), 180000)) reason = 'machine_unavailable';
      else if (gateRefusal) reason = gateRefusal.reason;
      else if (!Array.isArray(caps.engines) || !caps.engines.includes(route.engine) || !Array.isArray(caps.fleet_modes) || !caps.fleet_modes.includes(request.mode) || !workspaceCapable(caps.workspaces, effectiveWorkspace) || (absoluteCwd !== null && !workspaceCapable(caps.workspaces, absoluteCwd))) {
        const engineMissing = !Array.isArray(caps.engines) || !caps.engines.includes(route.engine);
        const modeMissing = !Array.isArray(caps.fleet_modes) || !caps.fleet_modes.includes(request.mode);
        const workspaceMissing = !workspaceCapable(caps.workspaces, effectiveWorkspace)
          || (absoluteCwd !== null && !workspaceCapable(caps.workspaces, absoluteCwd));
        // A route for an engine no live host advertises anymore is stale
        // registry debt, not a workspace/mode defect — but only when the live
        // fleet actually advertises engines. When nothing live advertises
        // anything (broken/empty registration), keep capability_unavailable.
        reason = (engineMissing && liveEngines.size > 0 && !liveEngines.has(route.engine)) ? 'engine_unavailable'
          : !engineMissing && !modeMissing && workspaceMissing ? 'workspace_unavailable' : 'capability_unavailable';
      }
      // Intake 4398ce2a (Alexey t1523u): the aggregate host gate and launcher
      // telemetry are projections, not refusals. Missing, stale, failed or
      // timed-out projections fail OPEN; per-route quota-gate refusals
      // (fleet_route_rejections above) and real MemAvailable/PSI/OOM/disk
      // signals below still hold. The host re-runs its real admission gate
      // before every claim, and the claim requires a <30 s observation.
      else if (Number.isFinite(hb.mem_avail_mb) && !memoryVerdict(hb.mem_avail_mb,
        memory().reserved_mb,
        memory().requested_mb, this.resources.config())) reason = 'launcher_memory_reserve';
      // Missing mem/disk telemetry is not a choking signal: fail OPEN (intake
      // 4398ce2a, Alexey t1523u). Only a present, real reading below a floor holds.
      // Root-disk floor is configurable (resource_settings.disk_floor_gb, default
      // 15 GiB). Docker and /tmp live on /data, so / only needs modest headroom.
      else if (Number.isFinite(hb.disk_free_gb) && hb.disk_free_gb < this.resources.config().disk_floor_gb) reason = 'disk_floor';
      else if (request.mode === 'headless' && disk()?.admitted === false) reason = 'launcher_disk_reserve';
      else if (!cpu().admitted) reason = cpu().reason!;
      else if (!account || account.status === 'blocked') reason = 'account_' + (account?.status || 'unmapped');
      else if (!routeQuota.ok) reason = 'quota_' + routeQuota.reason;
      else if (account.available_slots < 1) reason = 'account_concurrency';
      if (reason) { evidence(); rejected.push({ machine: route.machine_id, engine: route.engine, reason, ...(reason === 'account_concurrency' ? { account: route.account, concurrency: { active: account!.active, limit: account!.max_concurrency, available: account!.available_slots }, quota_status: account!.status } : {}), ...(reason === 'launcher_memory_reserve' ? { memory: memory() } : {}), ...(reason === 'launcher_disk_reserve' ? { disk: disk() } : {}), ...(reason === 'disk_floor' ? { disk_free_gb: hb.disk_free_gb, disk_floor_gb: this.resources.config().disk_floor_gb } : {}), ...(['cpu_headroom','cpu_pressure','memory_pressure','host_oom','host_runtime_choking'].includes(reason) ? {cpu:cpu()} : {}) }); continue; }
      const family = canonicalQuotaProvider(definitions.get(route.account)?.provider as string);
      const tier = taskTier({ ...(input.tier != null ? { tier: input.tier } : {}), model: overrideModels[0]?.id });
      const members: string[] = (policy.tiers as any)[tier || policy.default_tier];
      const model = policy.models.find(m => members.includes(m.id) && (!overrideModels.length || m.id === input.engine) && matchesModelRoute(m, route, family));
      if (!model) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'model_unsupported' }); continue; }
      // Host adapter refusals are scoped to the exact route, mode and model
      // within the engine health retry window so transient historical launch
      // rows cannot permanently poison a route after host/launcher updates.
      const unsupported = this.sql.exec("SELECT id FROM fleet_launches WHERE machine_id = ? AND engine = ? AND account = ? AND profile IS ? AND mode = ? AND exit_reason = 'model_unsupported' AND COALESCE(finished_at, updated_at) > ? AND json_extract(model_selection_json, '$.model') = ? AND json_extract(model_selection_json, '$.effort') IS ? LIMIT 1",
        route.machine_id, route.engine, route.account, route.profile, request.mode, new Date(this.now() - ENGINE_HEALTH_RETRY_MS).toISOString(), model.model, model.effort).toArray().length;
      const advertisedRefusal = Array.isArray(caps.fleet_route_rejections) && caps.fleet_route_rejections.some((r: any) =>
        r.engine === route.engine && r.account === route.account && (r.profile ?? null) === route.profile && r.mode === request.mode && r.reason === 'model_unsupported');
      if (unsupported || advertisedRefusal) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'model_unsupported' }); continue; }
      const pending = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_launches WHERE machine_id = ? AND state IN ${ACTIVE}`, route.machine_id).one().n);
      // Machine admission uses the resource verdicts above, not catalog counts.
      // Idle/stalled sessions are observations, not launch reservations.

      const tbm = pacingFor(pacing, account!.provider, route.account, account!.provider);
      if (request.mode !== 'interactive' && tbm.halted) {
        rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'tbm_admission_denied' }); continue;
      }
      const ownPools = pools.filter(p => p.account === canonicalAccountLabel(route.account) && canonicalQuotaProvider(p.provider) === family && p.reset_at_ms > this.now());
      const urgency = ownPools.length ? Math.max(...ownPools.map(p => resetUrgency(netHeadroom(p.headroom_percentage, p.hard_floor_percent),
        (p.reset_at_ms - this.now()) / 3600000))) : 1;
      const tbm_weight = request.mode === 'interactive' ? 1 : tbm.pacing * tbm.banked * (tbm.efficiency ?? 1) * urgency;
      const score = ((routeQuota.remaining ?? 5) / 100) / (1 + (hb.cpu_usage?.busy_cores ?? caps.cpu_count) / caps.cpu_count + pending) * tbm_weight;
      // Windows hosts are overflow for work that does not ask for Windows:
      // agent tooling assumes Linux, so they win only when every Linux route
      // is held (loaded, capped or quota-limited). Pinned and os:windows
      // requests are unaffected (intake a3227276 AC3).
      const overflow = overflowMachine(machine, needs);
      candidates.push({ machine: route.machine_id, engine: route.engine, account: route.account, profile: route.profile,
        ...this.resources.cpuEstimate(request.project || request.workspace,request.task_kind || 'goal'), selection: model, score, ...(routeQuota.reason !== 'eligible' || (gateProjection && !gateRefusal) ? { quota_warning: 'quota_probe_unavailable' } : {}), workspace: effectiveWorkspace, tbm_weight, tbm: { ...tbm, urgency }, ...(overflow ? { overflow: true } : {}) });
    }
    candidates.sort((a, b) => Number(!!a.overflow) - Number(!!b.overflow) || b.score - a.score || a.machine.localeCompare(b.machine) || a.engine.localeCompare(b.engine));
    // The stored intent carries the winner's normalized workspace: a bare
    // label that resolved under the selected host's root is persisted and
    // offered as the absolute path the host accepts. Labels that were
    // already capable keep their exact legacy meaning (host map keys).
    if (candidates[0] && typeof candidates[0].workspace === 'string' && candidates[0].workspace !== request.workspace) {
      request.workspace = candidates[0].workspace;
    }
    const codexSelector = input.engine === 'codex' || overrideModels.some(m => m.provider === 'codex');
    if (!candidates.length && codexSelector && !input.account && rejected.some(r =>
      r.reason.startsWith('codex_quota_gate_') || ['account_blocked', 'account_unknown', 'engine_auth_expired'].includes(r.reason))) {
      const fallback: any = this.plan({ ...input, engine: undefined, tier: input.tier ?? taskTier({ model: overrideModels[0]?.id }) });
      if (!fallback.error) return { ...fallback, request: { ...fallback.request, engine: request.engine }, rejected: [...rejected, ...fallback.rejected], fallback_from: input.engine };
    }
    return { request, selected: candidates[0] || null, candidates, rejected, evidence: Object.fromEntries(evidenceByMachine) };
    });
  }

  private diskHeadroom(machine: string, workspace?: string, excludeId?: string, cwd?: string) {
    const row: any = this.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', machine).toArray()[0];
    const hb = parsed(row?.last_heartbeat);
    if (!fresh(hb.launcher_resources?.observed_at, this.now(), LAUNCHER_TELEMETRY_STALE_MS)) return null;
    const pending = this.sql.exec("SELECT id FROM fleet_launches WHERE machine_id = ? AND mode = 'headless' AND state IN ('offered','starting','running','uncertain')", machine).toArray()
      .map((r: any) => String(r.id)).filter(id => id !== excludeId);
    return launcherDiskHeadroom(hb, pending, this.resources.config().disk_floor_gb, workspace, cwd);
  }

  private memoryHeadroom(machine: string) {
    return this.resources.headroom(machine);
  }

  /** Persist a placement hold with the rejection-time evidence map
   * (MemAvailable + PSI cpu/memory/io full per machine, captured from the
   * heartbeat that drove the refusal — intake 1bc3add1 AC1). Status
   * warnings read these values, never a later observation. */
  private memoryHold(id: string, rejected: any[], refused = false, evidence: Record<string, any> | null = null) {
    if (!rejected.some(r => CAPACITY_REASONS.has(r.reason))) return;
    this.sql.exec('INSERT INTO fleet_memory_holds (launch_id, since, rejected_json, refused, evidence_json) VALUES (?, ?, ?, ?, ?) ON CONFLICT(launch_id) DO UPDATE SET rejected_json = excluded.rejected_json, refused = MAX(refused, excluded.refused), evidence_json = COALESCE(excluded.evidence_json, evidence_json)',
      id, new Date(this.now()).toISOString(), JSON.stringify(rejected), refused ? 1 : 0, evidence ? JSON.stringify(evidence).slice(0, 8192) : null);
  }

  private memoryRejections(id: string): any[] {
    const row: any = this.sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id = ?', id).toArray()[0];
    return row ? parsed(row.rejected_json, []) : [];
  }

  private queuePosition(id: string): number {
    try {
      return Number(this.sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state = 'queued' AND rowid <= (SELECT rowid FROM fleet_launches WHERE id = ?)", id).one().n);
    } catch { return 1; }
  }

  /** Promote capacity-queued intents in bounded circular order; expire stale parks.
   *
   * Runs inside the ambient transaction on every reserve (new + replay) and
   * on every capacity-freeing write (terminal results, session catalog
   * reports) plus host offer polls, so a queued intent places without the
   * operator retrying blindly. Bounded per call for the CPU budget.
   * Admission and parent liveness are re-checked at promotion: a deny keeps
   * the row queued (never launches under dead authority). */
  drainCapacityQueue(limit = 2): { promoted: number; expired: number } {
    return this.withMaintenance(() => {
      if (this.maintenance!.drained) return { promoted: 0, expired: 0 };
      this.maintenance!.drained = true;
      return this.transaction(() => this.drainCapacitySlice(Math.min(limit,this.maintenance!.queueLimit)));
    });
  }

  private drainCapacitySlice(limit: number): { promoted: number; expired: number } {
    let promoted = 0, expired = 0;
    try {
      const nowIso = new Date(this.now()).toISOString();
      const staleBefore = new Date(this.now() - 48 * 3600000).toISOString();
      const stale = this.sql.exec("SELECT id FROM fleet_launches WHERE state = 'queued' AND created_at < ? ORDER BY created_at LIMIT 5", staleBefore).toArray() as any[];
      for (const row of stale) {
        this.sql.exec("UPDATE fleet_launches SET state = 'expired', updated_at = ? WHERE id = ? AND state = 'queued'", nowIso, row.id);
        expired++;
      }
      // A durable circular scan visits at most two candidates per operation. A fixed
      // high-water mark forces a wrap even while new queued work keeps arriving.
      // Host polls and capacity-changing writes drive the next bounded slice.
      const cursor: any = this.sql.exec('SELECT after_rowid,high_water FROM fleet_queue_cursor WHERE singleton=1').toArray()[0];
      let after = Number(cursor?.after_rowid || 0), high = Number(cursor?.high_water || 0);
      let persistedAfter = cursor?.after_rowid, persistedHigh = cursor?.high_water;
      const persistCursor = () => {
        if (persistedAfter === after && persistedHigh === high) return;
        this.sql.exec('INSERT INTO fleet_queue_cursor VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET after_rowid=excluded.after_rowid,high_water=excluded.high_water', after, high);
        persistedAfter = after; persistedHigh = high;
      };
      if (after >= high) {
        after = 0;
        high = Number(this.sql.exec("SELECT COALESCE(MAX(rowid),0) AS n FROM fleet_launches WHERE state='queued'").one().n);
      }
      // Bounded expiry cannot leave old work eligible for promotion.
      const rows = this.sql.exec("SELECT rowid AS queue_rowid,* FROM fleet_launches WHERE state='queued' AND rowid>? AND rowid<=? AND created_at>=? ORDER BY rowid LIMIT ?", after, high, staleBefore, Math.max(0,Math.min(2,Math.floor(limit)))).toArray() as any[];
      if (!rows.length) after = high;
      for (const row of rows) {
        if (promoted >= Math.min(2, limit)) break;
        after = Number(row.queue_rowid);
        // Persist only examined progress, in the promotion transaction. Even
        // malformed or held rows must not strand eligible work behind them.
        persistCursor();
        let bound: any = null;
        try { bound = JSON.parse(row.request_json); } catch { continue; }
        if (!bound || typeof bound !== 'object') continue;
        const input: any = { mode: bound.mode || 'headless', workspace: bound.workspace, prompt: bound.prompt ?? '', idempotency_key: 'drain-' + row.id };
        for (const key of ['machine', 'engine', 'account', 'tier', 'cwd', 'parent_id', 'summary', 'approved_action', 'access', 'with_goal', 'task_kind', 'project', 'needs']) {
          if (bound[key] !== undefined && bound[key] !== null) input[key] = bound[key];
        }
        let plan: any;
        try { plan = this.plan(input); } catch { continue; }
        if (!plan || (plan as any).error) continue;
        if (!plan.selected) {
          const rejected = plan.rejected || [];
          this.memoryHold(row.id, rejected, false, plan.evidence);
          // Honest parked status: when a re-plan now fails for a non-capacity
          // reason (e.g. model_unsupported after memory cleared), record the
          // live reasons so status stops showing the last memory sample forever.
          if (!isCapacityOnlyHold(rejected) && rejected.length) {
            this.sql.exec('INSERT INTO fleet_memory_holds (launch_id, since, rejected_json, refused, evidence_json) VALUES (?, ?, ?, 0, ?) ON CONFLICT(launch_id) DO UPDATE SET rejected_json = excluded.rejected_json, evidence_json = excluded.evidence_json', row.id, nowIso, JSON.stringify(rejected), plan.evidence && Object.keys(plan.evidence).length ? JSON.stringify(plan.evidence).slice(0, 8192) : null);
          }
          continue;
        }
        const memoryHold: any = this.sql.exec('SELECT since, refused FROM fleet_memory_holds WHERE launch_id = ?', row.id).toArray()[0];
        // Keep known disk evidence sticky if a later plan reports another
        // temporary hold. A generic heartbeat cannot clear that refusal.
        const diskHeld = parsed(row.launch_error_json)?.reason === 'launcher_disk_reserve';
        if (memoryHold?.refused && !diskHeld && (this.memoryHeadroom(plan.selected.machine).observed_at || '') <= memoryHold.since) continue;
        const diskOrigin: any = diskHeld ? this.sql.exec('SELECT l.machine_id FROM fleet_launches l JOIN fleet_memory_holds h ON h.launch_id=l.id WHERE h.retry_id=?', row.id).toArray()[0] : null;
        if (memoryHold?.refused && diskHeld && (!diskOrigin || diskOrigin.machine_id === plan.selected.machine)) {
          const machine: any = this.sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', plan.selected.machine).toArray()[0];
          const disk = parsed(machine?.last_heartbeat).launcher_resources;
          const verdict = this.diskHeadroom(plan.selected.machine, plan.request.workspace, undefined, plan.request.cwd);
          if (!disk || disk.observed_at <= memoryHold.since || !verdict?.admitted) continue;
        }
        if (bound.parent_id) {
          const parent: any = this.sql.exec('SELECT actor, state FROM fleet_launches WHERE id = ?', bound.parent_id).toArray()[0];
          if (!parent || parent.actor !== row.actor || !['starting', 'running'].includes(parent.state)) continue;
        }
        // A pre-fix queued row may carry a bare label; the re-plan above
        // normalizes it to the winner's absolute path. Re-check admission
        // against the normalized workspace (what the host will execute),
        // never the stale alias alone.
        const normalizedWorkspace = (plan as any).request?.workspace ?? bound.workspace;
        try {
          const hold = this.admission.heldReason({ request_json: row.request_json, workspace: normalizedWorkspace, id: row.id });
          if (hold) continue;
        } catch { continue; }
        const selected = (plan as any).selected;
        // Persist the normalized workspace alongside placement so the offer
        // carries the absolute path the host accepts (pre-fix bare-label
        // parks promote with the rewritten workspace, not the stale alias).
        let patchedJson: string = row.request_json;
        try {
          const patched = { ...bound, workspace: normalizedWorkspace };
          patchedJson = JSON.stringify(patched);
        } catch { /* keep the stored intent on stringify failure */ }
        const written: any = this.sql.exec("UPDATE fleet_launches SET machine_id = ?, engine = ?, account = ?, profile = ?, workspace = ?, request_json = ?, model_selection_json = ?, state = 'offered', launch_error_json = CASE WHEN json_extract(launch_error_json, '$.reason') = 'launcher_disk_reserve' THEN NULL ELSE launch_error_json END, updated_at = ? WHERE id = ? AND state = 'queued' RETURNING id",
          selected.machine, selected.engine, selected.account, selected.profile ?? null, normalizedWorkspace, patchedJson, JSON.stringify(selected.selection), new Date(this.now()).toISOString(), row.id).toArray()[0];
        if (written) {
          this.resources.freezeCpuStart(String(row.id),selected);
          promoted++;
        }
      }
      persistCursor();
    } catch { /* drain is best-effort inside the ambient txn */ }
    return { promoted, expired };
  }

  /** All final placement gates, authority reads and capacity writes share this transaction. */
  reserve(input: any, auth: Auth, expectedAuthority?: EditAuthority, preview = false): { value: any; status: number } {
    return this.withMaintenance(() => this.reserveWithinMaintenance(input, auth, expectedAuthority, preview));
  }

  private reserveWithinMaintenance(input: any, auth: Auth, expectedAuthority?: EditAuthority, preview = false): { value: any; status: number } {
    const result = (value: any, status = 200) => ({ value, status });
    const caller = auth.caller;
    if (!caller || !(caller.isAdmin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch')))) return result({ error: 'forbidden' }, 403);
    return this.transaction(() => {
      this.reconcileCapacity();
      // Give the retained waitlist a bounded turn before a new reservation;
      // within each circular slice, older rows are considered first.
      try { this.drainCapacityQueue(); } catch { /* best-effort */ }
      const plan = this.plan(input);
      if ('error' in plan) return result(plan, 400);
      const parsed = this.admission.parseDeclaration(input?.access);
      if ('error' in parsed) return result({ error: parsed.error }, 400);
      if (parsed.access.mode === 'edit' && !input.approved_action?.trim()) return result({ error: 'approved_action_required' }, 400);
      const bound = { ...plan.request, access: parsed.access };
      // Bare-label alias admission: the normalized absolute is what the
      // host executes and always gates. When the operator additionally
      // configured a claim policy for the requested alias itself, that
      // alias check must also pass (fail closed against policy bypass).
      // Unmapped aliases impose nothing beyond the normalized absolute.
      const requestedWorkspace = typeof input?.workspace === 'string' ? input.workspace : plan.request.workspace;
      const rewritten = requestedWorkspace !== plan.request.workspace;
      const aliasPolicy = rewritten ? this.admission.policy(requestedWorkspace) : null;
      const aliasConfigured = !!aliasPolicy && (!!aliasPolicy.project || aliasPolicy.require_claim);
      if (preview) {
        const preview = this.admission.evaluate(caller, plan.request.workspace, parsed.access, 'enqueue');
        if (!preview.ok) return result({ error: preview.error, access: parsed.access.mode }, preview.status);
        if (aliasConfigured) {
          const alias = this.admission.evaluate(caller, requestedWorkspace, parsed.access, 'enqueue');
          if (!alias.ok) return result({ error: alias.error, access: parsed.access.mode }, alias.status);
        }
        return result({ ...plan, access: parsed.access.mode });
      }
      if (!label(input.idempotency_key)) return result({ error: 'idempotency_key_required' }, 400);
      const actor = caller.actor || caller.machineId || 'admin'; const canonical = JSON.stringify(bound);
      const prior: any = this.sql.exec('SELECT * FROM fleet_launches WHERE actor = ? AND request_key = ?', actor, input.idempotency_key).toArray()[0];
      if (prior) {
        if (!sameIntent(prior.request_json, canonical) || !sameEditCaller(prior.request_json, caller)) return result({ error: 'idempotency_conflict' }, 409);
        if (prior.state === 'queued') {
          return result({ id: prior.id, state: 'queued', queued: true, queue_position: this.queuePosition(prior.id), rejected: this.memoryRejections(prior.id), machine: null, engine: null, account: null, mode: prior.mode, access: JSON.parse(prior.request_json).access?.mode || 'legacy_unspecified', replay: true }, 202);
        }
        return result({ id: prior.id, state: prior.state, machine: prior.machine_id, engine: prior.engine, account: prior.account, mode: prior.mode, access: JSON.parse(prior.request_json).access?.mode || 'legacy_unspecified', replay: true });
      }
      // A disabled execution engine refuses every new reservation, after valid replay.
      const overrideModels = policy.models.filter(m => m.id === plan.request.engine);
      const disabled = disabledEngines();
      if (plan.request.engine && (disabled.has(plan.request.engine) ||
          (overrideModels.length > 0 && overrideModels.every(m => disabled.has(m.engine))))) {
        return result({ error: 'provider_disabled', detail: 'The requested provider is disabled by model policy. Choose an enabled engine/model or ask the policy owner to enable it and register a fresh host route with verified quota.', access: parsed.access.mode }, 409);
      }
      if (plan.request.parent_id) {
        const parent: any = this.sql.exec('SELECT actor, state FROM fleet_launches WHERE id = ?', plan.request.parent_id).toArray()[0];
        if (!parent) return result({ error: 'parent_not_found' }, 400);
        if (parent.actor !== actor) return result({ error: 'parent_not_owned' }, 403);
        if (!['starting', 'running'].includes(parent.state)) return result({ error: 'parent_not_active' }, 409);
      }
      const admitted = this.admission.evaluate(caller, plan.request.workspace, parsed.access, 'reserve', { authority: expectedAuthority });
      if (!admitted.ok) return result({ error: admitted.error, access: parsed.access.mode }, admitted.status);
      if (aliasConfigured) {
        const alias = this.admission.evaluate(caller, requestedWorkspace, parsed.access, 'reserve', { authority: expectedAuthority });
        if (!alias.ok) return result({ error: alias.error, access: parsed.access.mode }, alias.status);
        if (JSON.stringify((alias as any).authority ?? null) !== JSON.stringify((admitted as any).authority ?? null)) {
          return result({ error: 'claim_authority_changed', access: parsed.access.mode }, 409);
        }
      }
      if (!plan.selected) {
        // Capacity-only failure parks a waitable queued intent (202) that the
        // drain auto-places when a slot frees. Any other reject class stays a
        // hard 409 with its slugs visible. Never raise max_concurrency here.
        // A hard 409 that still names memory/capacity pressure is recorded
        // for the operator alert (same liveness-alerts channel); queued holds
        // and terminal failures surface via the placement overview instead.
        if (isCapacityOnlyHold(plan.rejected)) {
          const id = crypto.randomUUID(); const at = new Date(this.now()).toISOString();
          this.sql.exec('INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', id, actor, caller.kind || 'unknown', plan.request.parent_id, plan.request.summary, input.idempotency_key, JSON.stringify({ ...bound, ...(admitted.authority ? { edit_authority: admitted.authority } : {}) }), '', '', '', null, plan.request.mode, plan.request.workspace, plan.request.prompt, 'queued', at, at);
          this.memoryHold(id, plan.rejected, false, plan.evidence);
          return result({ id, state: 'queued', queued: true, queue_position: this.queuePosition(id), rejected: plan.rejected, access: admitted.access.mode }, 202);
        }
        try { this.recordPlacementRejection(actor, plan.rejected); } catch { /* alerting never breaks placement */ }
        return result({ error: 'no_eligible_route', rejected: plan.rejected, access: parsed.access.mode }, 409);
      }
      const id = crypto.randomUUID(); const at = new Date(this.now()).toISOString(); const selected = plan.selected;
      this.sql.exec('INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', id, actor, caller.kind || 'unknown', plan.request.parent_id, plan.request.summary, input.idempotency_key, JSON.stringify({ ...bound, ...(admitted.authority ? { edit_authority: admitted.authority } : {}) }), selected.machine, selected.engine, selected.account, selected.profile, plan.request.mode, plan.request.workspace, plan.request.prompt, 'offered', at, at);
      this.resources.freezeCpuStart(id,selected);
      this.sql.exec('UPDATE fleet_launches SET model_selection_json = ? WHERE id = ?', JSON.stringify(selected.selection), id);
      return result({ id, selection: selected.selection, state: 'offered', machine: selected.machine, engine: selected.engine, account: selected.account, mode: plan.request.mode, access: admitted.access.mode }, 201);
    });
  }

  async handle(request: Request, auth: Auth): Promise<Response> {
    if (auth.error) return auth.error;
    const path = new URL(request.url).pathname; const caller = auth.caller;
    if (path === '/api/fleet/tasks' || path.startsWith('/api/fleet/tasks/') || path.startsWith('/api/fleet/routing/')) return this.routing.handle(request, auth);
    if (!caller) return response({ error: 'unauthorized' }, 401);
    const admin = !!caller.isAdmin; const host = caller.kind === 'host' && !!caller.machineId && auth.scopes?.includes('host');
    const writer = admin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch'));
    if (path === '/api/fleet/resources' || path.startsWith('/api/fleet/resources/')) {
      const suffix = path.slice('/api/fleet/resources'.length);
      if (request.method === 'GET') {
        if (suffix === '/watchdog') {
          if (!host) return response({error:'forbidden'},403);
          return response({config:this.resources.config(), sessions:this.resources.sessions(caller.machineId!), choking:this.resources.resourceChoking(caller.machineId!)});
        }
        if (!admin) return response({error:'forbidden'},403);
        if (!suffix) return response(this.resources.overview());
        if (suffix === '/sessions') return response({sessions:this.resources.usage(Object.fromEntries(new URL(request.url).searchParams))});
        if (suffix === '/reaps') return response({reaps:this.resources.reaps()});
        if (suffix === '/throttles') return response({throttles:this.resources.throttles()});
      }
      if (suffix === '/config' && request.method === 'PUT' && admin) {
        try { const data=await body(request); return response({config:this.resources.configure(data)}); }
        catch { return response({error:'invalid_resource_config'},400); }
      }
      if ((suffix === '/report' || suffix === '/evidence') && request.method === 'POST') {
        if (!host) return response({error:'forbidden'},403);
        try {
          const data=await body(request);
          return this.transaction(() => response(suffix === '/report' ? this.resources.report(caller.machineId!,data) : this.resources.evidence(caller.machineId!,data)));
        } catch (error) {
          const allowed=new Set(['invalid_resource_report','resource_binding_mismatch','invalid_resource_sample','invalid_resource_timestamp','invalid_reap','reap_conflict','invalid_throttle','throttle_conflict','session_not_reapable','invalid_evidence','evidence_conflict','intake_required','intake_not_found','intake_history_capacity','intake_capacity']);
          const code=error instanceof Error && allowed.has(error.message) ? error.message : 'resource_report_failed';
          return response({error:code},400);
        }
      }
      return response({error:'not_found'},404);
    }
    if (path === '/api/fleet/reconciliation' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      // Legacy failed dispatches predate the host journal. The host must fence
      // the maintained launcher before sending the existing no-start receipt.
      return response({ launches: this.reconciliationCandidates(caller.machineId!) });
    }
    if (path === '/api/fleet/reconcile' && request.method === 'POST') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!object(input, ['apply']) || typeof input.apply !== 'boolean') return response({ error: 'invalid_reconcile' }, 400);
      return this.transaction(() => {
        const changes = reconcileAccountCapacity(this.sql, this.now(), input.apply);
        if (input.apply) this.drainCapacityQueue();
        return response({ applied: input.apply, changes });
      });
    }
    if (path === '/api/fleet' && request.method === 'GET') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      // Harness per (machine, account) for the subscriptions table: every
      // quota row exposes both the harness and the provider/account pair.
      const routeHarness = new Map<string, Set<string>>();
      try {
        for (const r of this.sql.exec('SELECT machine_id, account, engine FROM fleet_routes').toArray() as any[]) {
          const key = String(r.machine_id) + ' ' + String(r.account);
          if (!routeHarness.has(key)) routeHarness.set(key, new Set());
          routeHarness.get(key)!.add(canonicalHarness(String(r.engine)));
        }
      } catch { /* harness reads as unknown below */ }
      const controlIds = new Map<string, string>();      try {
        for (const row of this.sql.exec('SELECT id, fleet_launch_id FROM session_control_sessions WHERE fleet_launch_id IN (SELECT id FROM fleet_launches ORDER BY created_at DESC LIMIT 200)').toArray() as any[]) controlIds.set(row.fleet_launch_id, row.id);
      } catch { /* optional session-control store */ }
      // Machine aliasing (mark, never delete): records sharing a normalized
      // hostname resolve to the live canonical host; sessions on a stale
      // alias resolve to the canonical host for display and liveness.
      let machineAliases: Record<string, string> = {};
      try {
        const aliasRows = this.sql.exec('SELECT id, hostname, drained, last_checkin_at FROM machines').toArray() as any[];
        const resolution = resolveMachineAliases(aliasRows.map((m: any) => {
          const persisted = typeof m.last_checkin_at === 'string' ? Date.parse(m.last_checkin_at) : NaN;
          return { id: String(m.id), hostname: m.hostname, drained: m.drained, last_seen_ms: Number.isFinite(persisted) ? persisted : null };
        }));
        machineAliases = Object.fromEntries(resolution.aliasOf.entries());
      } catch { machineAliases = {}; }
      const machines = this.sql.exec('SELECT id, name, hostname, last_checkin_at, drained FROM machines ORDER BY id').toArray().map((m: any) => {
        const canonical = machineAliases[String(m.id)] ?? null;
        return { ...m, last_checkin_at: effectiveCheckin(m.id, m.last_checkin_at), alias_of: canonical, canonical_id: canonical ?? String(m.id) };
      });
      const sessions = projectAliasSessions(this.sql.exec('SELECT * FROM fleet_sessions ORDER BY machine_id, tag').toArray(), new Map(Object.entries(machineAliases)), this.now(), freshCatalogs(this.sql, this.now()));
      return response({ accounts: dedupeAccountsByCanonical(this.sql.exec('SELECT * FROM fleet_accounts ORDER BY label').toArray() as any[]).map(a => this.accountStatus(a)), routes: this.sql.exec('SELECT * FROM fleet_routes ORDER BY machine_id, engine').toArray().map(r => ({ ...r, supports_goal: (() => { const caps = parsed(this.sql.exec('SELECT capabilities FROM machines WHERE id = ?', r.machine_id).toArray()[0]?.capabilities); return Array.isArray(caps?.fleet_goal_routes) && caps.fleet_goal_routes.some((g: any) => g.engine === r.engine && g.account === r.account && (g.profile ?? null) === r.profile); })() })),
        machines, machine_aliases: machineAliases,
        agents: this.sql.exec('SELECT tag, project, role, machine_id, engine, session_id, health_state, health_at FROM agents').toArray(),
        sessions,
        host_resources: this.hostResources(),
        resource_management: this.resources.overview(),
        placement: this.placementOverview(),
        subscriptions: this.sql.exec('SELECT * FROM fleet_subscriptions ORDER BY machine_id, service, account').toArray().map((s: any) => ({ machine_id: s.machine_id, account: canonicalAccountLabel(s.account), service: canonicalQuotaProvider(s.service), plan: s.plan, observed_at: s.observed_at, stale: !fresh(s.observed_at, this.now(), 1200000), quota: parsed(s.quota_json),
          harness: [...(routeHarness.get(String(s.machine_id) + ' ' + String(s.account)) ?? [])].sort() })),
        launches: this.sql.exec('SELECT * FROM fleet_launches ORDER BY created_at DESC LIMIT 200').toArray().map((row: any) => { const { request_json, request_key, claim_key, prompt, model_selection_json, ...view } = row; const selection = parsed(model_selection_json); return { ...view, rejected: row.state === 'queued' ? this.memoryRejections(row.id) : [], model_id: selection?.id ?? null, model: selection?.model ?? null, hub_session_id: controlIds.get(row.id) ?? null, access: parsed(request_json).access?.mode || 'legacy_unspecified', admission_hold: ['offered', 'starting', 'running', 'uncertain'].includes(row.state) ? this.admission.heldReason(row) : null }; }) });
    }
    if (path === '/api/fleet/claim-policy' && request.method === 'GET') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      return response({ workspaces: this.admission.listPolicy() });
    }
    if (path === '/api/fleet/claim-policy' && request.method === 'PUT') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['workspaces']) || !Array.isArray(data.workspaces) || data.workspaces.length > 128) return response({ error: 'invalid_claim_policy' }, 400);
      const names = new Set<string>();
      const rows: { workspace: string; project: string | null; require_claim: boolean }[] = [];
      for (const item of data.workspaces) {
        if (!object(item, ['workspace', 'project', 'require_claim']) || !workspaceOk(item.workspace) || (item.project !== null && !label(item.project)) || typeof item.require_claim !== 'boolean' || names.has(item.workspace)) return response({ error: 'invalid_claim_policy' }, 400);
        names.add(item.workspace);
        rows.push({ workspace: item.workspace, project: item.project, require_claim: item.require_claim });
      }
      return this.transaction(() => { this.admission.replacePolicy(rows); return response({ status: 'configured', workspaces: this.admission.listPolicy() }); });
    }
    // Cap raises and reserve lowerings do not need a lossy registry round trip.
    // In particular shared-go may have both canonical and legacy alias rows.
    if (path === '/api/fleet/accounts' && request.method === 'PATCH') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['accounts']) || !Array.isArray(data.accounts) || !data.accounts.length || data.accounts.length > 128)
        return response({ error: 'invalid_accounts' }, 400);
      return this.transaction(() => {
        const updates: {label: string; max: number; reserve: number}[] = [];
        const seen = new Set<string>();
        for (const a of data.accounts) {
          if (!object(a, ['label', 'max_concurrency', 'reserve_percent']) || !label(a.label) || seen.has(a.label) ||
              (a.max_concurrency === undefined && a.reserve_percent === undefined) ||
              (a.max_concurrency !== undefined && (!Number.isInteger(a.max_concurrency) || a.max_concurrency < 1 || a.max_concurrency > 100)) ||
              (a.reserve_percent !== undefined && (!Number.isFinite(a.reserve_percent) || a.reserve_percent < 0 || a.reserve_percent > 100)))
            return response({ error: 'invalid_account' }, 400);
          seen.add(a.label);
          const rows = this.sql.exec('SELECT max_concurrency, reserve_percent, provider FROM fleet_accounts WHERE label=?', a.label).toArray();
          if (!rows.length || (a.max_concurrency !== undefined && a.max_concurrency < Number(rows[0].max_concurrency))) return response({ error: 'account_raise_required' }, 409);
          if (a.reserve_percent !== undefined) {
            if (a.reserve_percent < (HEADROOM[String(rows[0].provider)] ?? 0)) return response({ error: 'invalid_account' }, 400);
            if (a.reserve_percent > Number(rows[0].reserve_percent)) return response({ error: 'account_reserve_lowering_required' }, 409);
          }
          updates.push({label: a.label, max: a.max_concurrency ?? Number(rows[0].max_concurrency),
            reserve: a.reserve_percent ?? Number(rows[0].reserve_percent)});
        }
        for (const a of updates) this.sql.exec('UPDATE fleet_accounts SET max_concurrency=?, reserve_percent=? WHERE label=?', a.max, a.reserve, a.label);
        return response({status: 'configured', updated_accounts: updates.length});
      });
    }
    if (path === '/api/fleet/accounts' && request.method === 'PUT') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['accounts', 'routes']) || !Array.isArray(data.accounts) || !Array.isArray(data.routes) || data.accounts.length > 128 || data.routes.length > 512) return response({ error: 'invalid_registry' }, 400);
      const labels = new Set(); const keys = new Set();
      for (const a of data.accounts) {
        if (!object(a, ['label', 'provider', 'max_concurrency', 'reserve_percent', 'enabled']) || !label(a.label) || !PROVIDERS.has(a.provider) || !Number.isInteger(a.max_concurrency) || a.max_concurrency < 1 || a.max_concurrency > 100 || !Number.isFinite(a.reserve_percent) || a.reserve_percent < (HEADROOM[a.provider] || 0) || a.reserve_percent > 100 || typeof a.enabled !== 'boolean' || labels.has(a.label)) return response({ error: 'invalid_account' }, 400);
        labels.add(a.label);
      }
      for (const r of data.routes) {
        if (!object(r, ['machine_id', 'engine', 'account', 'profile'])) return response({ error: 'invalid_route' }, 400);
        const key = r.machine_id + ':' + r.engine + ':' + r.account;
        if (!object(r, ['machine_id', 'engine', 'account', 'profile']) || !isValidMachineId(r.machine_id) || !label(r.engine) || !labels.has(r.account) || (r.profile !== null && !label(r.profile)) || keys.has(key) || !this.sql.exec('SELECT id FROM machines WHERE id = ?', r.machine_id).toArray().length) return response({ error: 'invalid_route' }, 400);
        keys.add(key);
      }
      return this.transaction(() => {
        this.admission.reconcileOffers();
        if (Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_launches WHERE state IN ${ACTIVE}`).one().n)) {
          // Add engine routes to the same quota pool without replacing live
          // account policy, subscriptions, routes or execution reservations.
          const accounts = this.sql.exec('SELECT * FROM fleet_accounts').toArray() as any[];
          const routes = this.sql.exec('SELECT * FROM fleet_routes').toArray() as any[];
          const sameAccountFields = (a: any, n: any) =>
            n.label === a.label && n.provider === a.provider &&
            n.reserve_percent === a.reserve_percent && Number(n.enabled) === a.enabled;
          // Pure concurrency raises (same label/provider/reserve/enabled,
          // new max >= old max) are safe under live launches: they only widen
          // the pool, never revoke in-flight reservations. Decreases and any
          // other field change still hold the registry with 409.
          const raisesAllowed = accounts.every(a => data.accounts.some((n: any) =>
            sameAccountFields(a, n) && n.max_concurrency >= a.max_concurrency));
          const raises = (data.accounts as any[]).filter(n => {
            const old = accounts.find(a => a.label === n.label);
            return old && sameAccountFields(old, n) && n.max_concurrency > old.max_concurrency;
          });
          const newLabels = (data.accounts as any[]).filter(n => !accounts.some(a => a.label === n.label));
          const sameRoute = (a: any, b: any) => a.machine_id === b.machine_id && a.engine === b.engine && a.account === b.account && a.profile === b.profile;
          const removed = routes.filter(r => !data.routes.some((n: any) => sameRoute(r, n)));
          // Prune obsolete routes without resetting subscriptions or pools.
          // Never remove a route with an outstanding execution reservation,
          // or replace a currently advertised route's profile under live work.
          const safeRemoval = removed.every(r => {
            const active = this.sql.exec(`SELECT id FROM fleet_launches WHERE machine_id = ? AND engine = ? AND account = ? AND state IN ${ACTIVE} LIMIT 1`, r.machine_id, r.engine, r.account).toArray().length;
            const m: any = this.sql.exec('SELECT * FROM machines WHERE id = ?', r.machine_id).toArray()[0];
            const engines = parsed(m?.capabilities).engines;
            const live = m && !m.drained && fresh(effectiveCheckin(m.id, m.last_checkin_at), this.now(), 180000);
            return !active && (!live || (Array.isArray(engines) && !engines.includes(r.engine)));
          });
          const added = data.routes.filter((n: any) => !routes.some(r => sameRoute(r, n)));
          if (!raisesAllowed || !safeRemoval || (!added.length && !removed.length && !raises.length && !newLabels.length)) return response({ error: 'active_launches_hold_registry' }, 409);
          for (const a of data.accounts) if (!accounts.some(old => old.label === a.label)) this.sql.exec('INSERT INTO fleet_accounts VALUES (?, ?, ?, ?, ?)', a.label, a.provider, a.max_concurrency, a.reserve_percent, Number(a.enabled));
          for (const r of raises) this.sql.exec('UPDATE fleet_accounts SET max_concurrency = ? WHERE label = ?', r.max_concurrency, r.label);
          for (const r of added) this.sql.exec('INSERT INTO fleet_routes VALUES (?, ?, ?, ?)', r.machine_id, r.engine, r.account, r.profile);
          for (const r of removed) this.sql.exec('DELETE FROM fleet_routes WHERE machine_id = ? AND engine = ? AND account = ?', r.machine_id, r.engine, r.account);
          return response({ status: 'configured', added_routes: added.length, ...(removed.length ? { removed_routes: removed.length } : {}) });
        }
        this.sql.exec('DELETE FROM fleet_subscriptions');
        this.sql.exec('DELETE FROM fleet_routes'); this.sql.exec('DELETE FROM fleet_accounts');
        for (const a of data.accounts) this.sql.exec('INSERT INTO fleet_accounts VALUES (?, ?, ?, ?, ?)', a.label, a.provider, a.max_concurrency, a.reserve_percent, Number(a.enabled));
        for (const r of data.routes) this.sql.exec('INSERT INTO fleet_routes VALUES (?, ?, ?, ?)', r.machine_id, r.engine, r.account, r.profile);
        return response({ status: 'configured' });
      });
    }
    if (['/api/fleet/plan', '/api/fleet/launches'].includes(path) && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      const reserved = this.reserve(input, auth, undefined, path.endsWith('/plan'));
      return response(reserved.value, reserved.status);
    }

    if (path === '/api/fleet/offers' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      return this.withMaintenance(() => this.transaction(() => {
        this.routing.dispatchNow();
        this.reconcileCapacity();
        // One re-plan per host poll; durable cursors preserve backlog progress.
        try { this.drainCapacityQueue(1); } catch { /* best-effort */ };
        const valid: any[] = [];
        for (const row of this.sql.exec("SELECT * FROM fleet_launches WHERE machine_id = ? AND state = 'offered' ORDER BY created_at, id LIMIT 100", caller.machineId!)) {
          if (this.admission.retireInvalidOffer(row)) continue;
          valid.push(row);
          if (valid.length === 20) break;
        }
        // Intake 488b860e follow-up: an all-attempts claim timeout can land
        // late server-side (state starting, attempt key stored, no native
        // effect). Offered-only polling would never resurface it, so the
        // same host could never replay the claim. Re-offer effect-free
        // starting launches to the owning host with their stored key; the
        // host resumes only on a journal key match, so a sibling loop's
        // claim is never stolen. Rotation (not age order) guarantees no row
        // starves: the retry window is the cycle right after the timeout,
        // and a flood of newer rows or stale foreign rows can never pin a
        // resumable launch out of the bounded window forever. The cursor is
        // a value in the ORDER BY key with a wrap query, so rows past any
        // fetch bound and rows inserted behind the cursor are still reached.
        const resumeBase = "FROM fleet_launches WHERE machine_id = ? AND state = 'starting' AND native_id IS NULL AND launcher_task_id IS NULL";
        const [cat, cid] = this.reofferCursors.get(caller.machineId!) ?? ['', ''];
        const resumeRows = this.sql.exec(`SELECT * ${resumeBase} AND (updated_at > ? OR (updated_at = ? AND id > ?)) ORDER BY updated_at, id LIMIT 100`, caller.machineId!, cat, cat, cid).toArray() as any[];
        if (resumeRows.length < 100) {
          resumeRows.push(...this.sql.exec(`SELECT * ${resumeBase} AND NOT (updated_at > ? OR (updated_at = ? AND id > ?)) ORDER BY updated_at, id LIMIT ?`, caller.machineId!, cat, cat, cid, 100 - resumeRows.length).toArray() as any[]);
        }
        for (const row of resumeRows) {
          valid.push(row);
          this.reofferCursors.set(caller.machineId!, [row.updated_at, row.id]);
          if (valid.length === 30) break;
        }
        const sessionTable = this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
        const scopedWorkers = sessionTable && valid.length ? new Set(this.sql.exec(
          `SELECT fleet_launch_id FROM session_control_sessions WHERE fleet_launch_id IN (${valid.map(() => '?').join(',')}) AND json_extract(request_json,'$.intake_task_id') IS NOT NULL`,
          ...valid.map(row => String(row.id))).toArray().map((row: any) => String(row.fleet_launch_id))) : new Set<string>();
        // Worktree-guard (AC1): the host locks the session worktree on start
        // with reason "hub-session <id> intake <intake-id|none>". The offer
        // carries both ids so the host never invents them.
        const hubLinks = new Map<string, { hub_session_id: string; intake_task_id: string | null }>();
        if (sessionTable && valid.length) {
          try {
            for (const row of this.sql.exec(
              `SELECT id, fleet_launch_id, request_json FROM session_control_sessions WHERE fleet_launch_id IN (${valid.map(() => '?').join(',')})`,
              ...valid.map(row => String(row.id))).toArray() as any[]) {
              if (!row.fleet_launch_id || hubLinks.has(String(row.fleet_launch_id))) continue;
              let intake: string | null = null;
              try {
                const request = row.request_json ? JSON.parse(row.request_json) : {};
                if (typeof request.intake_task_id === 'string' && request.intake_task_id) intake = request.intake_task_id;
              } catch { /* unparseable request: intake stays null */ }
              hubLinks.set(String(row.fleet_launch_id), { hub_session_id: String(row.id), intake_task_id: intake });
            }
          } catch { /* additive enrichment is best-effort */ }
        }
        const offers = this.resources.withReadSnapshot(() => {
          // Offer metadata needs host failure evidence, not a hypothetical
          // admission estimate for each launch. Claim admission remains live.
          const choking = valid.length ? this.resources.resourceChoking(caller.machineId!) : null;
          return valid.map(row => {
            const request = parsed(row.request_json);
            const link = hubLinks.get(String(row.id));
            return { ...(scopedWorkers.has(String(row.id)) ? { worker_credential_required: true } : {}),
              // AC1 lock identity (additive; unknown keys are ignored by hosts).
              ...(link ? { hub_session_id: link.hub_session_id, ...(link.intake_task_id ? { intake_task_id: link.intake_task_id } : {}) } : {}),
              ...this.resources.cpuStart(String(row.id)), resource_choking: choking, resource_management: this.resources.config(),
              memory_estimate_mb: this.resources.estimate(request.project || String(row.workspace), String(row.engine), request.task_kind || 'goal'),
              selection: parsed(row.model_selection_json) ?? {}, id: row.id, engine: row.engine, account: row.account, profile: row.profile,
              mode: row.mode, workspace: row.workspace, cwd: request.cwd ?? null, prompt: row.prompt,
              ...(request.with_goal === true ? { with_goal: true } : {}), access: request.access?.mode || 'legacy_unspecified',
              ...(row.state !== 'offered' ? { state: row.state, claim_key: row.claim_key ?? null } : {}) };
          });
        });
        return response({ offers: this.routing.enrichOffers(offers), reconciliation: this.reconciliationCandidates(caller.machineId!) });
      }),1);
    }
    if (path === '/api/fleet/sessions' && request.method === 'PUT') {
      if (!host) return response({ error: 'forbidden' }, 403);
      // A complete bounded native catalog can exceed the generic 32 KiB
      // mutation limit even with fewer than 100 sessions. Preserve the 500
      // row and field limits below; allow their maximum serialized footprint.
      const data = await body(request, 3 * 1024 * 1024);
      if (!object(data, ['sessions']) || !Array.isArray(data.sessions) || data.sessions.length > 500) return response({ error: 'invalid_sessions' }, 400);
      const ids = new Set();
      for (const s of data.sessions) {
        if (!object(s, ['native_id', 'tag', 'engine', 'phase', 'agent_state', 'workspace', 'cwd', 'profile', 'account', 'last_activity_at', 'created_at_ms', 'confirmed_exit', 'progress', 'tokens']) ||
            (s.tokens !== undefined && (!object(s.tokens, ['input_tokens', 'output_tokens']) ||
              !['input_tokens', 'output_tokens'].every(k => Number.isInteger(s.tokens[k]) && s.tokens[k] >= 0 && s.tokens[k] <= 1e12))) ||
            (s.progress !== undefined && (!object(s.progress, ['last_progress_at', 'headless', 'agent_alive', 'orphaned', 'cleanup', 'terminal_error', 'goal_status', 'sampled_at']) ||
              (s.progress.sampled_at !== undefined && (typeof s.progress.sampled_at !== 'string' || !fresh(s.progress.sampled_at, this.now() + 60000, 366 * 86400000))) ||
              (s.progress.goal_status !== undefined && !['blocked', 'paused', 'stalled', 'active', 'complete'].includes(s.progress.goal_status)) ||
              (s.progress.terminal_error !== undefined && (s.progress.terminal_error !== 'codex_reconnect_failed' || s.engine !== 'codex' || s.progress.headless !== false)) ||
              typeof s.progress.last_progress_at !== 'string' || !fresh(s.progress.last_progress_at, this.now() + 60000, 366 * 86400000) ||
              typeof s.progress.headless !== 'boolean' || ![true, false, null].includes(s.progress.agent_alive) ||
              typeof s.progress.orphaned !== 'boolean' || !['none', 'reaped', 'uncertain'].includes(s.progress.cleanup))) ||
            (s.last_activity_at !== undefined && s.last_activity_at !== null && (typeof s.last_activity_at !== 'string' || !fresh(s.last_activity_at, this.now() + 60000, 366 * 86400000))) ||
            (s.confirmed_exit !== undefined && typeof s.confirmed_exit !== 'boolean') ||
            (s.created_at_ms !== undefined && s.created_at_ms !== null && (!Number.isInteger(s.created_at_ms) || s.created_at_ms <= 0)) ||
            typeof s.native_id !== 'string' || !/^[a-zA-Z0-9-]{1,160}$/.test(s.native_id) || typeof s.tag !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$/.test(s.tag) || (s.engine !== null && !label(s.engine)) || !label(s.phase) || (s.agent_state !== null && !label(s.agent_state)) || typeof s.workspace !== 'string' || s.workspace.length > 2048 || /[\x00-\x1f\x7f]/.test(s.workspace) || (s.cwd !== undefined && s.cwd !== null && (typeof s.cwd !== 'string' || s.cwd.length > 2048 || /[\x00-\x1f\x7f]/.test(s.cwd))) || (s.profile !== null && !label(s.profile)) || (s.account !== null && !label(s.account)) || ids.has(s.native_id)) return response({ error: 'invalid_session' }, 400);
        ids.add(s.native_id);
      }
      return this.transaction(() => {
        // Write only rows that changed: hosts report every minute and Durable Object row writes
        // are metered. observed_at therefore means "last changed"; host freshness comes from heartbeats.
        const fields = ['tag', 'engine', 'phase', 'agent_state', 'workspace', 'cwd', 'profile', 'account', 'last_activity_at', 'created_at_ms', 'progress_json'];
        const existing = new Map((this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ?', caller.machineId!).toArray() as any[]).map(r => [r.native_id, r]));

        // Stopped same-incarnation native sessions cannot resurrect from late live-catalog snapshots:
        // do not insert or resurrect in fleet_sessions or adoptObservedNativeSessions if already canonically stopped for the same incarnation.
        const stoppedSessions = new Map<string, number | null>();
        try {
          const stoppedRows = this.sql.exec(
            "SELECT id, binding_json FROM session_control_sessions WHERE machine_id = ? AND state = 'stopped'",
            caller.machineId!
          ).toArray() as any[];
          for (const r of stoppedRows) {
            try {
              const b = JSON.parse(r.binding_json || '{}');
              stoppedSessions.set(r.id, b.native_created_at_ms ?? null);
            } catch {
              stoppedSessions.set(r.id, null);
            }
          }
        } catch {}

        let written = 0;
        const cleanSessions: any[] = [];
        for (const s of data.sessions) {
          if (s.tokens) recordSessionTokens(this.sql, { session_id: `${caller.machineId}:${s.native_id}`,
            native_id: s.native_id, tag: s.tag, engine: s.engine, account: s.account, ...s.tokens }, this.now());
          if (stoppedSessions.has(s.native_id)) {
            const stoppedCreated = stoppedSessions.get(s.native_id);
            if (stoppedCreated === null || stoppedCreated === undefined || s.created_at_ms === null || s.created_at_ms === undefined || s.created_at_ms <= stoppedCreated) {
              continue;
            }
          }
          // Terminal catalog labels and deleted paths are not exit receipts.
          if (['exited', 'dead', 'pruned', 'forgotten'].includes(s.phase) && s.confirmed_exit !== true) {
            s.phase = 'uncertain';
          }
          cleanSessions.push(s);
          const prior = existing.get(s.native_id); existing.delete(s.native_id);
          const row = { ...s, last_activity_at: s.last_activity_at ?? null, cwd: s.cwd ?? null, created_at_ms: s.created_at_ms ?? null, progress_json: s.progress ? JSON.stringify(s.progress) : null };
          if (prior && fields.every(f => (prior[f] ?? null) === (row[f] ?? null))) continue;
          this.sql.exec('INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at, created_at_ms, progress_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', caller.machineId!, s.native_id, s.tag, s.engine, s.phase, s.agent_state, new Date(this.now()).toISOString(), s.workspace, row.cwd, s.profile, s.account, row.last_activity_at, row.created_at_ms, row.progress_json);
          written++;
        }
        // Preserve historical observations. A missing catalog row is not exit
        // proof, but an orphan no longer observed is not a live workload.
        for (const gone of existing.keys()) {
          this.sql.exec("UPDATE fleet_sessions SET phase = 'unobserved' WHERE machine_id = ? AND native_id = ? AND phase IN ('starting','running','broken','unknown','uncertain')", caller.machineId!, gone); written++;
        }
        // Exact positive terminal evidence releases physical concurrency even
        // if a headless launch journal was lost. Absence is never exit proof.
        // Routed jobs retain their separate task-result contract.
        for (const native of cleanSessions) {
          if (!['exited', 'dead'].includes(native.phase) || !native.created_at_ms) continue;
          // Deleted cwd/workspace is a capacity projection above, not positive
          // native exit evidence. It must never settle a launch reservation.
          if (native.workspace.endsWith(' (deleted)') || native.cwd?.endsWith(' (deleted)')) continue;
          const managed = this.sql.exec("SELECT * FROM session_control_sessions WHERE machine_id = ? AND fleet_launch_id IS NOT NULL AND binding_json IS NOT NULL", caller.machineId!).toArray() as any[];
          for (const session of managed) {
            const binding = parsed(session.binding_json);
            if (binding.native_id !== native.native_id || binding.tag !== native.tag ||
                binding.workspace !== native.workspace || binding.native_created_at_ms !== native.created_at_ms ||
                session.engine !== native.engine) continue;
            if (this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', session.fleet_launch_id).toArray().length) continue;
            const at = new Date(this.now()).toISOString();
            this.sql.exec("UPDATE fleet_launches SET state = 'exited', finished_at = ?, updated_at = ?, verification_status = 'verified' WHERE id = ? AND machine_id = ? AND native_id = ? AND state IN ('starting','running','uncertain')", at, at, session.fleet_launch_id, caller.machineId!, native.native_id);
            this.sql.exec("UPDATE session_control_sessions SET state = 'stopped', updated_at = ? WHERE id = ? AND state IN ('starting','running','uncertain')", at, session.id);
          }
        }
        adoptObservedNativeSessions(this.sql, caller.machineId!, cleanSessions, this.now());
        reconcileAccountCapacity(this.sql, this.now());
        // A refreshed catalog can free machine_capacity: promote waiters.
        try { this.drainCapacityQueue(); } catch { /* best-effort */ }
        // unchanged rows retain last-change timestamps; the complete census
        // has separate freshness evidence, bounded to one host write/minute.
        this.sql.exec('INSERT INTO fleet_catalog_seen VALUES (?,?) ON CONFLICT(machine_id) DO UPDATE SET observed_at=excluded.observed_at WHERE fleet_catalog_seen.observed_at <= ?', caller.machineId!, new Date(this.now()).toISOString(), new Date(this.now() - 60000).toISOString());
        return response({ status: 'reported', written });
      });
    }
    if (path === '/api/fleet/subscriptions' && request.method === 'PUT') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['subscriptions']) || !Array.isArray(data.subscriptions) || data.subscriptions.length > 128) return response({ error: 'invalid_subscriptions' }, 400);
      const clean: any[] = []; const labels = new Set();
      for (const sub of data.subscriptions) {
        if (!object(sub, ['service', 'account', 'plan', 'quota']) || !label(sub.account) || !PROVIDERS.has(sub.service) || (sub.plan !== null && (typeof sub.plan !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9 ._+-]{0,63}$/.test(sub.plan))) || labels.has(sub.account)) return response({ error: 'invalid_subscription' }, 400);
        labels.add(sub.account);
        // Canonical provider AND account match: a legacy `codex-orin` service
        // report maps to an account registered under provider `codex`, and a
        // legacy `urgent-muse` account report maps to `shared-go` — in either
        // update order — so a half-updated fleet never fails the whole
        // per-machine report with unmapped.
        const canonicalService = canonicalQuotaProvider(sub.service);
        const canonicalSubAccount = canonicalAccountLabel(sub.account);
        const legacySubAccount = Object.entries(LEGACY_ACCOUNT_LABELS).find(([, c]) => c === canonicalSubAccount)?.[0];
        if ([...labels].some((l) => canonicalAccountLabel(l as string) === canonicalSubAccount && l !== sub.account)) return response({ error: 'invalid_subscription', reason: 'duplicate_account' }, 400);
        const mapped = this.sql.exec('SELECT a.label, a.provider FROM fleet_accounts a JOIN fleet_routes r ON a.label = r.account WHERE r.machine_id = ? AND (a.label = ? OR a.label = ? OR a.label = ?)', caller.machineId!, sub.account, canonicalSubAccount, legacySubAccount ?? sub.account).toArray()
          .filter((row: any) => canonicalQuotaProvider(row.provider) === canonicalService);
        const quota = sanitizedQuota(sub.quota);
        if (!mapped.length || !quota || (quota.observed_at !== null && Date.parse(quota.observed_at) > this.now()) || (quota.status === 'ok' && !fresh(quota.observed_at, this.now(), 1200000))) return response({ error: 'subscription_unmapped_or_invalid', reason: !mapped.length ? 'unmapped' : !quota ? 'invalid_quota' : 'invalid_timestamp' }, 400);
        // Store canonical names so subscription reads join the canonical
        // account registry regardless of which legacy name a host reported.
        clean.push({ ...sub, account: canonicalSubAccount, service: canonicalService, quota });
      }
      return this.transaction(() => {
        const prior = new Map((this.sql.exec('SELECT account, quota_json FROM fleet_subscriptions WHERE machine_id = ?', caller.machineId!).toArray() as any[]).map(r => [r.account, r.quota_json]));
        this.sql.exec('DELETE FROM fleet_subscriptions WHERE machine_id = ?', caller.machineId!);
        for (const s of clean) {
          // A newer report that omits a cooldown deadline must not erase an unexpired known hold.
          if (s.quota.details?.cooldown_until == null) {
            try {
              const oldQ = JSON.parse(prior.get(s.account) || 'null');
              const oldCd = oldQ?.details?.cooldown_until;
              if (typeof oldCd === 'string' && Number.isFinite(Date.parse(oldCd)) && Date.parse(oldCd) > this.now()) {
                s.quota.details.cooldown_until = oldCd;
              }
            } catch { /* keep the field absent */ }
          }
          this.sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', caller.machineId!, s.account, s.service, s.plan, new Date(this.now()).toISOString(), JSON.stringify(s.quota));
          syncTbmFromServerAccounts(this.sql, [{ account: s.account, provider: s.service, quota: s.quota }],
            Date.parse(s.quota.observed_at));
        }
        return response({ status: 'reported' });
      });
    }
    if (path === '/api/fleet/engine-health' && request.method === 'GET') {
      if (!admin && !caller) return response({ error: 'forbidden' }, 403);
      const health = this.sql.exec("SELECT * FROM fleet_engine_health").toArray();
      return response({ health, engines: health });
    }
    if (path === '/api/fleet/probe' && request.method === 'POST') {
      if (!admin && !host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!data || typeof data !== 'object' || !label(data.engine)) {
        return response({ error: 'invalid_probe' }, 400);
      }
      if (data.status !== undefined && !['ok', 'success', 'unavailable', 'failed'].includes(data.status)) {
        return response({ error: 'invalid_probe_status' }, 400);
      }
      if (data.reason !== undefined && (typeof data.reason !== 'string' || !/^[a-z0-9_-]{1,64}$/.test(data.reason))) {
        return response({ error: 'invalid_probe_reason' }, 400);
      }
      if (data.machine_id !== undefined && !isValidMachineId(data.machine_id)) {
        return response({ error: 'invalid_machine_id' }, 400);
      }
      if (data.probe_id !== undefined && (typeof data.probe_id !== 'string' || data.probe_id.length > 128)) {
        return response({ error: 'invalid_probe_id' }, 400);
      }

      let targetMachine = '';
      if (!admin) {
        if (data.machine_id && data.machine_id !== caller!.machineId) {
          return response({ error: 'forbidden_machine_id' }, 403);
        }
        targetMachine = caller!.machineId!;
      } else {
        targetMachine = data.machine_id || '';
      }

      const isSuccess = data.status === 'ok' || data.status === 'success' || data.success === true;
      const nowIso = new Date(this.now()).toISOString();

      return this.transaction(() => {
        if (isSuccess) {
          // The host's periodic configuration probe can retire only its own
          // missing-adapter hold. Authentication failures still require the
          // explicit recovery probe; a healthy sibling account cannot clear them.
          if (data.reason === 'admission_configured') {
            if (!targetMachine) return response({ error: 'machine_required' }, 400);
            this.sql.exec("UPDATE fleet_engine_health SET status = 'ok', updated_at = ? WHERE engine = ? AND machine_id = ? AND reason = 'admission_unconfigured'", nowIso, data.engine, targetMachine);
            return response({ status: 'configuration_probed', engine: data.engine, machine_id: targetMachine });
          }
          if (data.reason === 'device_auth_verified') {
            if (!targetMachine || data.engine !== 'opencode') return response({ error: 'machine_required' }, 400);
            this.sql.exec("UPDATE fleet_engine_health SET status = 'ok', updated_at = ? WHERE engine = ? AND machine_id = ? AND reason = 'engine_auth_expired'", nowIso, data.engine, targetMachine);
            const remaining: any = this.sql.exec("SELECT COUNT(*) as count FROM fleet_engine_health WHERE engine = ? AND status = 'unavailable'", data.engine).one();
            if (remaining.count === 0) {
              this.sql.exec('DELETE FROM operator_notices WHERE engine = ?', data.engine);
              if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'liveness_alerts'").toArray().length) {
                this.sql.exec("UPDATE liveness_alerts SET resolved_at = ? WHERE resolved_at IS NULL AND subject_kind = 'engine_auth_expired' AND subject = ?", nowIso, data.engine);
              }
            }
            return response({ status: 'device_auth_probed', engine: data.engine, machine_id: targetMachine });
          }
          if (targetMachine) {
            this.sql.exec("UPDATE fleet_engine_health SET status = 'ok', updated_at = ? WHERE engine = ? AND machine_id = ?", nowIso, data.engine, targetMachine);
          } else {
            this.sql.exec("UPDATE fleet_engine_health SET status = 'ok', updated_at = ? WHERE engine = ?", nowIso, data.engine);
          }

          const remaining: any = this.sql.exec("SELECT COUNT(*) as count FROM fleet_engine_health WHERE engine = ? AND status = 'unavailable'", data.engine).toArray()[0];
          if (!remaining || remaining.count === 0) {
            this.sql.exec("DELETE FROM operator_notices WHERE engine = ?", data.engine);
            try {
              if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'liveness_alerts'").toArray().length) {
                this.sql.exec("UPDATE liveness_alerts SET resolved_at = ? WHERE resolved_at IS NULL AND subject_kind = 'engine_auth_expired' AND subject = ?",
                  nowIso, data.engine);
              }
            } catch {}
          }

          return response({ status: 're_enabled', engine: data.engine, machine_id: targetMachine || null });
        } else {
          const reason = data.reason || 'engine_auth_expired';
          this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, 'unavailable', ?, ?)",
            data.engine, targetMachine, reason, nowIso);
          return response({ status: 'marked_unavailable', engine: data.engine, machine_id: targetMachine || null, reason });
        }
      });
    }
    const match = /^\/api\/fleet\/launches\/([a-f0-9-]{36})\/(claim|result|reject)$/.exec(path);
    if (match && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      // A bounded report plus runtime evidence may expand under Python's
      // JSON Unicode escaping. Keep other mutations on the smaller budget.
      const data = await body(request, match[2] === 'result' ? 131072 : 32768);
      if (!data || typeof data !== 'object' || Array.isArray(data)) return response({ error: 'invalid_launch_result' }, 400);
      const runtime = data.runtime === undefined ? undefined : sanitizeExitEvidence(data.runtime);
      if (data?.runtime !== undefined && (!runtime || !['failed', 'exited'].includes(data.state) || data.confirmed_exit !== true)) return response({ error: 'invalid_exit_evidence' }, 400);
      return this.transaction(() => {
        const row: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ? AND machine_id = ?', match[1], caller.machineId!).toArray()[0];
        if (!row) return response({ error: 'not_found' }, 404);
        if (match[2] === 'reject') {
          if (!object(data, ['reason', 'diagnostic']) || (data.reason !== 'dispatch_refused' && !Object.hasOwn(DISPATCH_MESSAGES, data.reason))) return response({ error: 'invalid_rejection' }, 400);
          const diagnostic = data.diagnostic ?? { reason: data.reason, message: DISPATCH_MESSAGES[data.reason] || 'The host refused dispatch before native start.', stage: 'dispatch_route', error_type: 'Error' };
          if (!object(diagnostic, ['reason', 'message', 'stage', 'error_type', 'command', 'exit_code', 'stdout_tail', 'stderr_tail', 'http_status', 'hub_error', 'hub_reason', 'retryable']) || !claimFieldsValid(diagnostic) || diagnostic.reason !== data.reason || diagnostic.stage !== 'dispatch_route' ||
              diagnostic.message !== (DISPATCH_MESSAGES[data.reason] || 'The host refused dispatch before native start.') ||
              typeof diagnostic.error_type !== 'string' || !/^[A-Za-z]{1,64}$/.test(diagnostic.error_type) ||
              !sanitizeCommandFields(diagnostic)) return response({ error: 'invalid_rejection' }, 400);
          // Only an unclaimed offer proves that no start was authorized. The
          // transaction fences a racing claim; never reject possibly-live work.
          if (row.state !== 'offered' || row.native_id || row.launcher_task_id) return response({ error: 'already_claimed' }, 409);
          if (this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) return response({ error: 'task_report_required' }, 409);
          this.sql.exec('UPDATE fleet_launches SET launch_error_json = ? WHERE id = ?', JSON.stringify(diagnostic), row.id);
          const at = new Date(this.now()).toISOString();
          if (diagnostic.reason === 'dispatch_claim_refused' && diagnostic.retryable === true) {
            // The offered-state fence above proves no native start was authorized.
            // Retain the same intent; next offer repeats every admission/authority gate.
            return response({ id: row.id, state: 'held', reason: data.reason, diagnostic, retryable: true });
          }
          if (data.reason === 'model_unsupported' || data.reason === 'engine_auth_expired' || ['cpu_headroom','cpu_pressure','memory_pressure','host_oom','host_runtime_choking'].includes(data.reason)) {
            if (data.reason === 'engine_auth_expired') {
              this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, 'unavailable', ?, ?)", row.engine, row.machine_id, data.reason, at);
            }
            // The fenced pre-claim refusal proves no native start. Retain the
            // failed attempt as route evidence and use a new launcher identity.
            const id = crypto.randomUUID();
            const rejected = [{ machine: row.machine_id, engine: row.engine, reason: data.reason, ...(['cpu_headroom','cpu_pressure','memory_pressure','host_oom','host_runtime_choking'].includes(data.reason) ? {cpu:this.resources.cpuHeadroom(row.machine_id,parsed(row.request_json),row.id)} : {}) }];
            this.sql.exec("UPDATE fleet_launches SET request_key = ?, state = 'failed', verification_status = 'not_started', exit_reason = ?, finished_at = ?, updated_at = ? WHERE id = ?", 'model-retry-' + row.id, data.reason, at, at, row.id);
            this.sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', NULL, ?, ?, ?, 'queued', ?, ?)", id, row.actor, row.requester_kind, row.parent_id, row.summary, row.request_key, row.request_json, row.mode, row.workspace, row.prompt, row.created_at, at);
            this.sql.exec('INSERT INTO fleet_memory_holds (launch_id, since, rejected_json) VALUES (?, ?, ?)', id, at, JSON.stringify(rejected));
            if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
              this.sql.exec("UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = NULL, state = 'queued', updated_at = ? WHERE fleet_launch_id = ? AND binding_json IS NULL AND state IN ('offered','queued','held')", id, at, row.id);
            }
            this.drainCapacityQueue();
            const next: any = this.sql.exec('SELECT state FROM fleet_launches WHERE id = ?', id).one();
            return response({ id, state: next.state, queued: next.state === 'queued', rejected });
          }
          this.sql.exec("UPDATE fleet_launches SET state = 'failed', verification_status = 'not_started', exit_reason = ?, finished_at = ?, updated_at = ? WHERE id = ? AND state = 'offered'", data.reason, at, at, row.id);
          if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
            this.sql.exec("UPDATE session_control_sessions SET state = 'failed', updated_at = ? WHERE fleet_launch_id = ? AND machine_id = ? AND binding_json IS NULL AND state IN ('offered','queued','held')", at, row.id, caller.machineId!);
          }
          if (data.reason === 'admission_unconfigured') {
            // A proven pre-claim refusal is execution-health evidence, even when
            // quota is healthy. Do not repeatedly offer this broken host engine.
            this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, 'unavailable', ?, ?)", row.engine, row.machine_id, data.reason, at);
          }
          try { this.drainCapacityQueue(); } catch { /* best-effort */ }
          return response({ id: row.id, state: 'failed', reason: data.reason, diagnostic });
        }
        if (match[2] === 'claim') {
          // Intake 488b860e: a timeout before/at claim leaves the outcome
          // unknown. The host retries with the SAME per-attempt claim_key; a
          // stored key match on a still-effect-free starting launch replays
          // the claim instead of double-claiming. A different key (sibling
          // loop or another attempt) stays a 409 conflict.
          const refuse = (body: { error: string; reason?: string; [key: string]: unknown }, status: number) => {
            const diagnostic = { reason: 'dispatch_claim_refused', message: DISPATCH_MESSAGES.dispatch_claim_refused,
              stage: 'dispatch_route', error_type: 'ValueError', http_status: status,
              ...(CLAIM_HTTP_SLUGS.has(body.error) ? { hub_error: body.error } : {}),
              ...(body.reason && CLAIM_HTTP_SLUGS.has(body.reason) ? { hub_reason: body.reason } : {}),
              retryable: row.state === 'offered' && !row.native_id && !row.launcher_task_id && status === 409 && RETRYABLE_CLAIM_ERRORS.has(body.error) };
            // A racing post-start attempt must not overwrite native failure proof.
            if (!row.native_id && !row.launcher_task_id) {
              this.sql.exec('UPDATE fleet_launches SET launch_error_json = ? WHERE id = ?', JSON.stringify(diagnostic), row.id);
            }
            return response(body, status);
          };
          const claimKey = (data as any)?.claim_key;
          if (claimKey !== undefined && (typeof claimKey !== 'string' || !CLAIM_KEY.test(claimKey))) return refuse({ error: 'invalid_claim' }, 400);
          const replay = typeof claimKey === 'string' && row.claim_key === claimKey && row.state === 'starting' && !row.native_id && !row.launcher_task_id;
          if (row.state !== 'offered' && !replay) return refuse({ error: 'already_claimed' }, 409);
          // A delayed first start must still have current edit authority,
          // fresh admission and enabled providers. Invalid replay holds the
          // existing reservation: it cannot prove absence of native effects.
          const cpu=this.resources.cpuHeadroom(row.machine_id,parsed(row.request_json),row.id);
          if(!cpu.admitted) return refuse({error:cpu.reason,cpu},409);
          // Pre-hold offers on a now-disabled engine are held, never launched.
          if (disabledEngines().has(row.engine)) return refuse({ error: 'provider_disabled' }, 409);
          const held = replay ? this.admission.heldReason(row) : this.admission.retireInvalidOffer(row);
          if (held) return refuse({ error: held }, ['claim_inactive', 'claim_conflict', 'claim_authority_changed'].includes(held) ? 409 : 403);
          if (!object(data, ['gate_allowed', 'observed_at', 'account', 'profile_supported', 'model', 'claim_key']) || data.gate_allowed !== true || data.profile_supported !== true || data.account !== row.account || !fresh(data.observed_at, this.now(), 30000)) return refuse({ error: 'fresh_admission_required' }, 409);
          const selectedModel = parsed(row.model_selection_json)?.model;
          if (typeof selectedModel !== 'string' || data.model !== selectedModel) return refuse({ error: 'model_unsupported', detail: 'Host must confirm the resolved model before native launch' }, 409);
          const account: any = this.sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', row.account).toArray()[0];
          if (!account || this.accountStatus(account).status === 'blocked') return refuse({ error: 'account_unavailable' }, 409);
          // Re-verify the exact launch route quota inside the atomic claim: no manual override bypass.
          const claimRoute: any = this.sql.exec('SELECT machine_id, profile FROM fleet_routes WHERE machine_id = ? AND account = ?', row.machine_id, row.account).toArray()[0];
          const claimProv = claimRoute ? this.observationProvenance(account, claimRoute) : null;
          const claimAdm = evaluateLaunchQuota({ provider: account.provider, quota: claimProv?.quota ?? null, subscriptionObservedAt: claimProv?.subscriptionObservedAt ?? null, nowMs: this.now(), reserves: ((((policy as any).rules as any).reserve_percent || {}) as Record<string, number>) });
          if (!claimAdm || !claimAdm.ok) return refuse({ error: 'quota_unavailable', reason: claimAdm ? claimAdm.reason : 'no_quota_evidence' }, 409);
          // Capacity reads reconcile other offers and time may advance during synchronous checks.
          // Never revive a row retired by that sweep; check authority again at the final write.
          const current: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ?', row.id).one();
          if (replay) {
            if (current.state !== 'starting' || current.claim_key !== claimKey || current.native_id || current.launcher_task_id) return refuse({ error: 'already_claimed' }, 409);
            const replayHold = this.admission.heldReason(current);
            if (replayHold) return refuse({ error: replayHold }, 409);
            return response({ ...current, state: 'starting', replay: true });
          }
          const finalHold = current.state === 'offered' ? this.admission.retireInvalidOffer(current) : current.exit_reason || 'already_claimed';
          if (finalHold) return refuse({ error: finalHold }, ['claim_inactive', 'claim_conflict', 'claim_authority_changed', 'already_claimed'].includes(finalHold) ? 409 : 403);
          this.sql.exec("UPDATE fleet_launches SET state = 'starting', claim_key = COALESCE(?, claim_key), updated_at = ? WHERE id = ? AND state = 'offered'", typeof claimKey === 'string' ? claimKey : null, new Date(this.now()).toISOString(), row.id);
          this.sql.exec('INSERT INTO resource_ramps VALUES(?,?) ON CONFLICT(launch_id) DO UPDATE SET started_at=excluded.started_at', row.id, new Date(this.now()).toISOString());
          return response({ ...row, state: 'starting', ...(typeof claimKey === 'string' ? { claim_key: claimKey } : {}) });
        }
        // Replay an identical committed normal-headless terminal receipt without effects.
        if (row.mode === 'headless' && ['exited', 'failed'].includes(row.state) &&
            object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit', 'report_status', 'report', 'artifacts', 'runtime']) &&
            data.state === row.state && data.confirmed_exit === true &&
            typeof data.native_id === 'string' && data.native_id.length > 0 && data.native_id.length <= 160 &&
            data.native_id === row.native_id && label(data.launcher_task_id) &&
            data.launcher_task_id === row.launcher_task_id &&
            !this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) {
          if (runtime) this.sql.exec('UPDATE fleet_launches SET runtime_json = ? WHERE id = ?', JSON.stringify(runtime), row.id);
          this.ingestFinishReport(row.id, data);
          return response({ id: row.id, state: row.state, replay: true });
        }
        // Host emits fixed, sanitized diagnostics; validate bounds again at storage.
        // AC a19cac9e: runtime diagnostics may carry signal, wall_time_ms and
        // a sanitized tail alongside exit_code; all bounded, no control chars.
        const diagnostic = data.diagnostic;
        if (diagnostic !== undefined && (!object(diagnostic, ['reason', 'message', 'error_type', 'exit_code', 'signal', 'wall_time_ms', 'tail', 'stage', 'cpu', 'command', 'stdout_tail', 'stderr_tail']) ||
            typeof diagnostic.reason !== 'string' || !/^(?:(?:launcher|engine)_[a-z_]{1,64}|cpu_headroom|cpu_pressure|memory_pressure|host_oom|host_runtime_choking)$/.test(diagnostic.reason) ||
            typeof diagnostic.message !== 'string' || diagnostic.message.length > 300 || /[\x00-\x1f\x7f]/.test(diagnostic.message) ||
            typeof diagnostic.error_type !== 'string' || !/^[A-Za-z]{1,64}$/.test(diagnostic.error_type) ||
            !['launcher_run', 'dispatch_headless', 'dispatch_interactive', 'dispatch_route', 'runtime'].includes(diagnostic.stage) ||
            (diagnostic.exit_code !== undefined && !Number.isSafeInteger(diagnostic.exit_code)) ||
            (diagnostic.signal !== undefined && (!Number.isSafeInteger(diagnostic.signal) || diagnostic.signal <= 0 || diagnostic.signal > 128)) ||
            (diagnostic.wall_time_ms !== undefined && (!Number.isSafeInteger(diagnostic.wall_time_ms) || diagnostic.wall_time_ms < 0)) ||
            (diagnostic.tail !== undefined && (typeof diagnostic.tail !== 'string' || diagnostic.tail.length > 1024 || /[\x00-\x1f\x7f]/.test(diagnostic.tail))) ||
            (diagnostic.cpu !== undefined && !sanitizeCpuAdmission(diagnostic.cpu)) ||
            !sanitizeCommandFields(diagnostic))) return response({ error: 'invalid_launch_diagnostic' }, 400);
        if (diagnostic) {
          diagnostic.message = redactExitText(diagnostic.message).slice(0, 300);
          if (diagnostic.tail !== undefined) diagnostic.tail = redactExitText(diagnostic.tail).slice(0, 1024);
        }
        // Replay the original diagnostic after an ambiguous delivery. Journal
        // serialization may reorder object keys; compare JSON values, not bytes.
        const sameDiagnostic = diagnostic !== undefined && !!row.launch_error_json &&
          sameJsonValue(parsed(row.launch_error_json), diagnostic);
        if (row.mode === 'headless' && ['exited', 'failed'].includes(row.state) &&
            data.state === row.state && data.confirmed_exit === true && sameDiagnostic &&
            object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit', 'diagnostic', 'report_status', 'report', 'artifacts', 'runtime']) &&
            !!row.native_id && data.native_id === row.native_id &&
            !!row.launcher_task_id && data.launcher_task_id === row.launcher_task_id &&
            !this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) {
          if (runtime) this.sql.exec('UPDATE fleet_launches SET runtime_json = ? WHERE id = ?', JSON.stringify(runtime), row.id);
          this.ingestFinishReport(row.id, data);
          return response({ id: row.id, state: row.state, replay: true });
        }
        // An owning host may enrich a legacy failed receipt after collecting
        // runtime evidence. Identity/state stay fixed; no capacity/lifecycle effect.
        if (row.mode === 'headless' && row.state === 'failed' && data.state === 'failed' &&
            diagnostic?.stage === 'runtime' && data.confirmed_exit === true &&
            object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit', 'diagnostic', 'report_status', 'report', 'artifacts', 'runtime']) &&
            !!row.native_id && data.native_id === row.native_id &&
            !!row.launcher_task_id && data.launcher_task_id === row.launcher_task_id &&
            (!row.launch_error_json || sameDiagnostic) &&
            !this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) {
          this.sql.exec('UPDATE fleet_launches SET launch_error_json = ?, exit_reason = ? WHERE id = ?', JSON.stringify(diagnostic), diagnostic.reason, row.id);
          if (runtime) this.sql.exec('UPDATE fleet_launches SET runtime_json = ? WHERE id = ?', JSON.stringify(runtime), row.id);
          this.ingestFinishReport(row.id, data);
          return response({ id: row.id, state: row.state, replay: true });
        }
        const noStart = object(data, ['state', 'launcher_task_id', 'confirmed_exit', 'reason', 'diagnostic']) &&
          data.state === 'failed' && data.confirmed_exit === true &&
          (data.reason === 'launcher_not_started' || (diagnostic && data.reason === diagnostic.reason)) &&
          data.launcher_task_id === 'hub-' + row.id && !row.native_id;
        // Owning-host enrichment of the exact legacy no-start receipt. Keep
        // its terminal lifecycle and history; never restart the original job.
        if (noStart && row.mode === 'headless' && row.state === 'failed' &&
            row.verification_status === 'not_started' && row.exit_reason === 'launcher_command_failed' &&
            row.launcher_task_id === data.launcher_task_id && data.reason === 'launcher_disk_reserve' &&
            diagnostic?.error_type === 'ValueError' && diagnostic?.stage === 'dispatch_headless' &&
            diagnostic?.message === 'Host disk headroom after launcher reservations is below the maintained disk floor.') {
          this.sql.exec('UPDATE fleet_launches SET launch_error_json = ?, exit_reason = ? WHERE id = ?', JSON.stringify(diagnostic), diagnostic.reason, row.id);
          return response({ id: row.id, state: row.state, replay: true });
        }
        if (noStart && ['launcher_disk_reserve','launcher_memory_reserve','cpu_headroom','cpu_pressure','memory_pressure','host_oom','host_runtime_choking'].includes(data.reason)) {
          const prior: any = this.sql.exec('SELECT retry_id FROM fleet_memory_holds WHERE launch_id = ?', row.id).toArray()[0];
          if (prior?.retry_id) return response({ id: prior.retry_id, state: 'queued', replay: true });
          if (row.state === 'starting' && !this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) {
            // Fenced no-start proof: keep the user's session queued, with a new
            // launcher task identity. Never reset/reuse the failed local task.
            const id = crypto.randomUUID(), at = new Date(this.now()).toISOString();
            const rejected = [{ machine: row.machine_id, engine: row.engine, reason: data.reason, ...(data.reason==='launcher_disk_reserve' ? {disk:this.diskHeadroom(row.machine_id,row.workspace,row.id)} : data.reason==='launcher_memory_reserve' ? {memory:this.memoryHeadroom(row.machine_id)} : {cpu:{...this.resources.cpuHeadroom(row.machine_id,parsed(row.request_json),row.id),...(diagnostic?.cpu || {})}}) }];
            this.sql.exec("UPDATE fleet_launches SET request_key = ?, state = 'exited', exit_reason = ?, launch_error_json = ?, verification_status = 'not_started', finished_at = ?, updated_at = ? WHERE id = ?", 'memory-retry-' + row.id, data.reason, JSON.stringify(diagnostic), at, at, row.id);
            this.sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', NULL, ?, ?, ?, 'queued', ?, ?)", id, row.actor, row.requester_kind, row.parent_id, row.summary, row.request_key, row.request_json, row.mode, row.workspace, row.prompt, row.created_at, at);
            this.memoryHold(row.id, rejected, true); this.memoryHold(id, rejected, true);
            if (data.reason === 'launcher_disk_reserve') this.sql.exec('UPDATE fleet_launches SET launch_error_json = ? WHERE id = ?', JSON.stringify(diagnostic), id);
            // Durable alert exists even if a later heartbeat promotes before cron.
            const alerts = new LivenessService(this.sql); alerts.init();
            alerts.setAlert('launch', row.id, { state: data.reason, detail: JSON.stringify(rejected) });
            this.sql.exec('UPDATE fleet_memory_holds SET retry_id = ? WHERE launch_id = ?', id, row.id);
            if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
              this.sql.exec("UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = NULL, state = 'queued', updated_at = ? WHERE fleet_launch_id = ? AND binding_json IS NULL", id, at, row.id);
            }
            return response({ id, state: 'queued', queued: true, rejected });
          }
        }
        if (noStart && row.state === 'failed' && row.exit_reason === data.reason && row.launcher_task_id === data.launcher_task_id) return response({ id: row.id, state: row.state, replay: true });
        if ((!noStart && !object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit', 'diagnostic', 'report_status', 'report', 'artifacts', 'runtime'])) || !['running', 'uncertain', 'failed', 'exited'].includes(data.state) || !['starting', 'running', 'uncertain'].includes(row.state)) return response({ error: 'invalid_transition' }, 409);
        if (['exited', 'failed'].includes(data.state) && this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) return response({ error: 'task_report_required' }, 409);
        if (['exited', 'failed'].includes(data.state) && data.confirmed_exit !== true) return response({ error: 'confirmed_exit_required' }, 409);
        if (data.state === 'running' && (typeof data.native_id !== 'string' || data.native_id.length < 1 || data.native_id.length > 160 || (row.mode === 'headless' && !label(data.launcher_task_id)))) return response({ error: 'native_receipt_required' }, 409);
        if ((data.native_id !== undefined && (typeof data.native_id !== 'string' || data.native_id.length > 160)) || (data.launcher_task_id !== undefined && !label(data.launcher_task_id))) return response({ error: 'invalid_receipt' }, 400);
        // Hub finish-report fields (intake 88b3260d): optional, bounded, sanitized host-side.
        // Accepted on terminal (and replay) receipts; running/uncertain receipts must not carry them.
        if (data.report_status !== undefined && !['done', 'failed', 'waiting-on-human', 'stuck'].includes(data.report_status)) return response({ error: 'invalid_finish_report' }, 400);
        if (data.report !== undefined && (typeof data.report !== 'string' || data.report.length > 4096 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(data.report))) return response({ error: 'invalid_finish_report' }, 400);
        if (data.artifacts !== undefined && (!Array.isArray(data.artifacts) || data.artifacts.length > 8 || data.artifacts.some((a: any) =>
          a === null || typeof a !== 'object' || Array.isArray(a) ||
          typeof (a as any).label !== 'string' || (a as any).label.length > 120 ||
          typeof (a as any).text !== 'string' || (a as any).text.length > 1024))) return response({ error: 'invalid_finish_report' }, 400);
        if (!['failed', 'exited'].includes(data.state) && (data.report_status !== undefined || data.report !== undefined || data.artifacts !== undefined)) return response({ error: 'invalid_finish_report' }, 400);
        this.sql.exec('UPDATE fleet_launches SET state = ?, native_id = COALESCE(?, native_id), launcher_task_id = COALESCE(?, launcher_task_id), updated_at = ? WHERE id = ?', data.state, data.native_id || null, data.launcher_task_id || null, new Date(this.now()).toISOString(), row.id);
        if (runtime) this.sql.exec('UPDATE fleet_launches SET runtime_json = ? WHERE id = ?', JSON.stringify(runtime), row.id);
        if (diagnostic) this.sql.exec("UPDATE fleet_launches SET launch_error_json = ?, exit_reason = ? WHERE id = ?", JSON.stringify(diagnostic), diagnostic.reason, row.id);
        if (noStart) this.sql.exec("UPDATE fleet_launches SET exit_reason = ?, verification_status = 'not_started', finished_at = ? WHERE id = ?", data.reason, new Date(this.now()).toISOString(), row.id);
        if (diagnostic?.reason === 'engine_tool_permission_refused' && row.mode === 'headless') {
          this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at, diagnostics_json) VALUES (?, ?, 'unavailable', ?, ?, ?)", row.engine, row.machine_id, diagnostic.reason, new Date(this.now()).toISOString(), JSON.stringify(diagnostic));
        }
        if (diagnostic && diagnostic.reason === 'engine_auth_expired') {
          this.sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at, diagnostics_json) VALUES (?, ?, 'unavailable', 'engine_auth_expired', ?, ?)", row.engine, row.machine_id, new Date(this.now()).toISOString(), JSON.stringify(diagnostic));
          const noticeKey = 'engine_auth_expired:' + row.engine;
          const existingNotice = this.sql.exec("SELECT notice_key FROM operator_notices WHERE notice_key = ?", noticeKey).toArray()[0];
          if (!existingNotice) {
            const noticeMsg = row.engine === 'opencode'
              ? 'OpenCode device login is unavailable. Run opencode auth login opencode --method device on the affected host, then verify a launch. No API key is required.'
              : `Authentication for engine '${row.engine}' is unavailable. Renew its native provider login and verify a launch.`;
            this.sql.exec("INSERT INTO operator_notices (notice_key, engine, message, created_at) VALUES (?, ?, ?, ?)", noticeKey, row.engine, noticeMsg, new Date(this.now()).toISOString());
            try {
              if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'liveness_alerts'").toArray().length) {
                const existingAlert = this.sql.exec("SELECT id FROM liveness_alerts WHERE resolved_at IS NULL AND subject_kind = 'engine_auth_expired' AND subject = ?", row.engine).toArray();
                if (existingAlert.length === 0) {
                  this.sql.exec("INSERT INTO liveness_alerts (subject_kind, subject, state, detail, opened_at) VALUES ('engine_auth_expired', ?, 'failing', ?, ?)",
                    row.engine, noticeMsg, new Date(this.now()).toISOString());
                }
              }
            } catch {}
          }
        }
        // An attempted start with uncertain effects is no longer an offer.
        // Keep its reservation and missing binding, but expose the real state.
        if (data.state === 'uncertain' && this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
          this.sql.exec("UPDATE session_control_sessions SET state = 'uncertain', updated_at = ? WHERE fleet_launch_id = ? AND machine_id = ? AND state IN ('queued', 'held', 'offered', 'starting', 'running')", new Date(this.now()).toISOString(), row.id, caller.machineId!);
        }
        let retried = false;
        const isAlreadyRetry = (typeof row.request_key === 'string' && row.request_key.startsWith('retry-')) ||
          this.sql.exec("SELECT id FROM fleet_launches WHERE retried_launch_id = ?", row.id).toArray().length > 0;
        const origLaunchReq = parsed(row.request_json, {});
        const hasExplicitEngine = Boolean(origLaunchReq.engine && typeof origLaunchReq.engine === 'string');
        if (data.state === 'failed' && data.confirmed_exit === true &&
            (data.reason === 'engine_auth_expired' || ['engine_auth_expired', 'engine_tool_permission_refused'].includes(diagnostic?.reason)) &&
            !row.retried_launch_id && !isAlreadyRetry && !hasExplicitEngine) {
          const hasSessionTable = this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length > 0;
          const sessionRow: any = hasSessionTable ? this.sql.exec("SELECT * FROM session_control_sessions WHERE fleet_launch_id = ?", row.id).toArray()[0] : null;
          if (sessionRow && !sessionRow.retried_launch_id) {
            const sessionReq = parsed(sessionRow.request_json, {});
            const retryInput = {
              mode: sessionRow.mode,
              workspace: sessionReq.workspace || sessionRow.workspace,
              tier: sessionReq.tier || origLaunchReq.tier,
              prompt: sessionReq.brief || sessionReq.prompt || '',
              summary: sessionReq.summary,
              cwd: sessionReq.cwd ?? null,
              parent_id: sessionRow.parent_id ?? sessionReq.parent_id ?? null,
              access: sessionReq.access || origLaunchReq.access,
              approved_action: sessionReq.approved_action || origLaunchReq.approved_action,
              ...(sessionReq.with_goal === true || origLaunchReq.with_goal === true ? { with_goal: true } : {}),
              idempotency_key: 'retry-' + row.id,
            };

            let retryAuth: Auth;
            if (row.requester_kind === 'human' || sessionRow.actor.startsWith('human:') || sessionRow.actor === 'admin') {
              retryAuth = { caller: { kind: 'human', actor: sessionRow.actor, isAdmin: true }, scopes: ['fleet:launch', 'admin'] };
            } else {
              retryAuth = {
                caller: {
                  kind: 'agent',
                  actor: sessionRow.actor,
                  tokenId: sessionRow.owner_token_id || undefined,
                },
                scopes: ['fleet:launch', 'bus:send'],
              };
            }

            let ownerRevoked = false;
            if (retryAuth.caller?.kind === 'agent' && retryAuth.caller?.tokenId) {
              const cred: any = this.sql.exec('SELECT revoked_at, expires_at FROM credentials WHERE id = ?', retryAuth.caller.tokenId).toArray()[0];
              if (!cred || cred.revoked_at != null || (cred.expires_at && Date.parse(cred.expires_at) <= this.now())) {
                ownerRevoked = true;
              }
            }

            const wasRunning = sessionRow.state === 'running' || sessionRow.binding_json != null || Boolean(row.native_id);
            if (!ownerRevoked) {
              const reserved = this.reserve(retryInput, retryAuth);
              if (reserved.status < 300) {
                const nowIso = new Date(this.now()).toISOString();
                this.sql.exec("UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = ?, engine = ?, state = 'offered', updated_at = ? WHERE id = ?",
                  reserved.value.id, reserved.value.machine, reserved.value.engine, nowIso, sessionRow.id);
                if (diagnostic?.reason === 'engine_tool_permission_refused') {
                  // Retry has a new native identity (and may use a different host).
                  // Preserve the failed launch history, retire commands for its binding.
                  this.sql.exec("UPDATE session_control_sessions SET binding_json = NULL, host_family = NULL WHERE id = ?", sessionRow.id);
                  this.sql.exec("UPDATE session_control_commands SET state = 'failed', ack_json = ?, poll_at = NULL, updated_at = ? WHERE session_id = ? AND state IN ('recorded','transported','claimed','uncertain')", JSON.stringify({ reason: 'engine_tool_permission_refused', message: 'Original execution failed; command cannot follow the replacement binding.' }), nowIso, sessionRow.id);
                }
                this.sql.exec("UPDATE fleet_launches SET retried_launch_id = ? WHERE id = ?", reserved.value.id, row.id);
                retried = true;
              } else {
                const nowIso = new Date(this.now()).toISOString();
                const termState = wasRunning ? 'failed' : 'held';
                this.sql.exec("UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?", termState, nowIso, sessionRow.id);
              }
            } else {
              const nowIso = new Date(this.now()).toISOString();
              const termState = wasRunning ? 'failed' : 'held';
              this.sql.exec("UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?", termState, nowIso, sessionRow.id);
            }
          } else if (!sessionRow) {
            const origReq = parsed(row.request_json, {});
            const retryInput = {
              mode: row.mode,
              workspace: row.workspace,
              tier: origReq.tier,
              prompt: row.prompt,
              summary: row.summary,
              cwd: origReq.cwd ?? null,
              parent_id: row.parent_id ?? origReq.parent_id ?? null,
              access: origReq.access,
              approved_action: origReq.approved_action,
              ...(origReq.with_goal === true ? { with_goal: true } : {}),
              idempotency_key: 'retry-' + row.id,
            };

            let retryAuth: Auth;
            if (row.requester_kind === 'human' || row.actor === 'admin') {
              retryAuth = { caller: { kind: 'human', actor: row.actor, isAdmin: true }, scopes: ['fleet:launch', 'admin'] };
            } else {
              const credId = origReq.edit_authority?.credential_id ||
                (this.sql.exec("SELECT id FROM credentials WHERE actor = ? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", row.actor).toArray()[0] as any)?.id;
              retryAuth = {
                caller: {
                  kind: 'agent',
                  actor: row.actor,
                  tokenId: credId || undefined,
                },
                scopes: ['fleet:launch', 'bus:send'],
              };
            }

            let ownerRevoked = false;
            if (retryAuth.caller?.kind === 'agent' && retryAuth.caller?.tokenId) {
              const cred: any = this.sql.exec('SELECT revoked_at, expires_at FROM credentials WHERE id = ?', retryAuth.caller.tokenId).toArray()[0];
              if (!cred || cred.revoked_at != null || (cred.expires_at && Date.parse(cred.expires_at) <= this.now())) {
                ownerRevoked = true;
              }
            }

            if (!ownerRevoked) {
              const reserved = this.reserve(retryInput, retryAuth, origReq.edit_authority);
              if (reserved.status < 300) {
                this.sql.exec("UPDATE fleet_launches SET retried_launch_id = ? WHERE id = ?", reserved.value.id, row.id);
                retried = true;
              }
            }
          }
        }
        // A confirmed terminal host result must also close its canonical Hub
        // session. Otherwise intake keeps an active assignment forever after
        // a failed start, preventing recovery despite released Fleet capacity.
        // Fleet can run without the optional session-control tables in tests.
        if (!retried && ['failed', 'exited'].includes(data.state) && this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
          if (noStart) {
            // A held session still linked to this failed launch cannot retry:
            // Fleet idempotency replays the failure forever. Close the attempt
            // so intake can recover with a new start key. This also repairs
            // waitlisted sessions whose machine assignment is still null.
            reconcileUnboundFleetSessions(this.sql, this.now());
          } else {
            this.sql.exec("UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE fleet_launch_id = ? AND machine_id = ? AND state IN ('queued', 'offered', 'starting', 'running', 'uncertain')", data.state === 'failed' ? 'failed' : 'stopped', new Date(this.now()).toISOString(), row.id, caller.machineId!);
          }
        }
        // A terminal result frees account/machine capacity: promote waiters.
        if (['failed', 'exited'].includes(data.state)) {
          this.sql.exec("UPDATE fleet_launches SET finished_at = COALESCE(finished_at, ?), updated_at = ? WHERE id = ?", new Date(this.now()).toISOString(), new Date(this.now()).toISOString(), row.id);
          this.ingestFinishReport(row.id, data);
          try { this.drainCapacityQueue(); } catch { /* best-effort */ }
        }
        return response({ id: row.id, state: data.state });
      });
    }
    return response({ error: 'not_found' }, 404);
  }

  /** Append the Hub-launched session's final result to its linked intake.
   *
   * Intake 88b3260d AC1: every Hub-launched headless session's finish report
   * (.local/hub-results/<fleet_launch_id>.json forwarded by the host as
   * report_status/report/artifacts) lands on the linked intake as a
   * Hub-authored report entry within the SAME transaction as the terminal
   * Fleet receipt (done, waiting-on-human, stuck and failed). No worker auth
   * required. Never closes the intake, never records scope decisions.
   * Idempotent: a second terminal receipt (replay) finds the existing
   * sourceRef and skips. Absent intake link or full task: no-op.
   *
   * Inline SQL on purpose: this already runs inside the outer Fleet result
   * transaction, and a nested transactionSync (e.g. via an intake service
   * call) throws in workerd, which would silently drop every report. Bounds
   * mirror IntakeService.serviceRecordEvent (events 128 with hub:service
   * eviction, taskBytes 131072). */
  private ingestFinishReport(launchId: string, data: any): void {
    try {
      if (!['failed', 'exited'].includes(data?.state)) return;
      if (data?.report_status !== undefined && !['done', 'failed', 'waiting-on-human', 'stuck'].includes(data.report_status)) return;
      if (data?.report !== undefined && (typeof data.report !== 'string' || data.report.length > 4096)) return;
      if (data?.artifacts !== undefined && (!Array.isArray(data.artifacts) || data.artifacts.length > 8 || data.artifacts.some((a: any) =>
        a === null || typeof a !== 'object' || Array.isArray(a) ||
        typeof (a as any).label !== 'string' || (a as any).label.length > 120 ||
        typeof (a as any).text !== 'string' || (a as any).text.length > 1024))) return;
      if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length === 0) return;
      if (this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'intake_tasks'").toArray().length === 0) return;
      const session: any = this.sql.exec('SELECT id, request_json FROM session_control_sessions WHERE fleet_launch_id = ?', launchId).toArray()[0];
      if (!session) return;
      let intakeTaskId: string | null = null;
      try {
        const req = JSON.parse(session.request_json || '{}');
        if (typeof req?.intake_task_id === 'string' && req.intake_task_id) intakeTaskId = req.intake_task_id;
      } catch { return; }
      if (!intakeTaskId) return;
      const sourceRef = 'fleet-finish:' + launchId;
      let taskRows: any[] = this.sql.exec('SELECT id,data FROM intake_tasks WHERE id=?',intakeTaskId).toArray();
      if (!taskRows.length && intakeTaskId.length >= 6) taskRows = this.sql.exec('SELECT id,data FROM intake_tasks WHERE substr(id,1,?)=? LIMIT 2',intakeTaskId.length,intakeTaskId).toArray();
      if (taskRows.length !== 1) return;
      const taskRow = taskRows[0];
      intakeTaskId = taskRow.id;
      const task = JSON.parse(taskRow.data);
      if (!task || !Array.isArray(task.history)) return;
      const canonical = task.history.filter((e:any) => e?.kind === 'report' && e.actor === 'hub:service:fleet-finish' &&
        e.authority === 'hub_launch_verified' && e.source?.reference === sourceRef);
      const received = (e:any) => e.worker_report_received === true || (e.worker_report_received === undefined &&
        e.text?.includes(' Worker report: ') && !e.text.includes('Worker report: Agent exited without a valid structured report'));
      const incomingWorkerReport = typeof data.report === 'string' && !!data.report.trim() &&
        (!data.runtime || data.runtime.outcome === 'reported');
      if (canonical.some(received) || (canonical.length && !incomingWorkerReport)) return;
      // Preserve the original receipt. A late worker report is an additional
      // immutable history entry, accepted once from the same owning host.

      const rawStatus = typeof data?.report_status === 'string' ? data.report_status : null;
      const workerStatus = ['done', 'failed', 'waiting-on-human', 'stuck'].includes(rawStatus || '') ? rawStatus : null;
      const fleetState = data.state === 'exited' ? 'exited' : 'failed';
      let reportText = typeof data?.report === 'string' ? redactExitText(data.report.trim()) : '';
      if (reportText.length > 2000) reportText = reportText.slice(0,2000).replace(/[\uD800-\uDBFF]$/, '') + '…';
      let artifactLine = '';
      if (Array.isArray(data?.artifacts) && data.artifacts.length) {
        const labels = (data.artifacts as any[])
          .map((a: any) => redactExitText(a.label).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '').slice(0, 120))
          .filter((l: string) => l);
        if (labels.length) artifactLine = `\nArtifacts: ${labels.join(', ').slice(0, 500)}`;
      }
      const text = `Hub session ${session.id} finished (${fleetState}${workerStatus ? '/' + workerStatus : ''}, launch ${launchId}).` +
        (reportText ? ` Worker report: ${reportText}` : ' No worker report file was forwarded by the host.') + artifactLine;
      if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) return;
      const at = new Date(this.now()).toISOString();
      const event = {
        id: crypto.randomUUID(), kind: 'report', actor: 'hub:service:fleet-finish',
        captured_at: Date.now(), text: text.slice(0, 8192),
        source: { type: 'unknown', reference: sourceRef, timestamp: at },
        authority: 'hub_launch_verified', worker_report_received: incomingWorkerReport,
      };
      if (task.history.length >= 128) return; // Preserve every prior event; durable capacity repair is separate.
      task.history.push(event);
      task.version = (typeof task.version === 'number' ? task.version : 1) + 1;
      task.updated_at = Date.now();
      if (JSON.stringify(task).length > 131072) return;
      this.sql.exec('UPDATE intake_tasks SET data = ? WHERE id = ?', JSON.stringify(task), intakeTaskId);
      this.sql.exec('UPDATE intake_meta SET revision = revision + 1 WHERE id = 1');
    } catch { /* best-effort: a finish-report append must never fail the terminal receipt */ }
  }
}

function sameJsonValue(a: any, b: any): boolean {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b, k) && sameJsonValue(a[k], b[k]));
}
