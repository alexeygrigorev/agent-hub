/** Hub work-claim protocol helpers (Phase 1, minimal v1).
 *
 * Implements the wire-level pieces of the work-claim & messaging protocol
 * design (`hub.work-claim.v1`, `work.claim|complete|abandon|steal|handoff`
 * lifecycle) WITHOUT inventing a competing protocol:
 *
 * - strict validation of the `hub.work-claim.v1` `--data` payload,
 * - `work_key` <-> Hub claim scope mapping (`@work/` namespace),
 * - host-attested synthetic actor naming (`native:<machine>:<tag>[<short>]`),
 * - deterministic secret redaction before any payload touches Hub SQLite.
 *
 * The single mandatory worker write path stays local-first (`aplexer`
 * `a work join` + `work.*` mailbox events); the host daemon bridges
 * redacted native state here via `POST /api/bus/native-sync`.
 */

export const WORK_CLAIM_SCHEMA = 'hub.work-claim.v1' as const;

export const WORK_CLAIM_EVENTS = [
  'claim',
  'heartbeat',
  'complete',
  'abandon',
  'steal',
  'handoff',
] as const;

export type WorkClaimEvent = (typeof WORK_CLAIM_EVENTS)[number];

/** Native mailbox kinds bridged by the host daemon (design section 9). */
export const WORK_MESSAGE_KINDS = [
  'work.claim',
  'work.heartbeat',
  'work.complete',
  'work.abandon',
  'work.steal',
  'work.handoff',
] as const;

export function isWorkMessageKind(kind: unknown): boolean {
  return typeof kind === 'string' && (WORK_MESSAGE_KINDS as readonly string[]).includes(kind);
}

const CLAIM_ID_RE = /^clm_[0-9A-HJKMNP-TV-Z]{10,26}$/;
const WORK_KEY_RE = /^(task|intake|ci|pr|issue|path):[A-Za-z0-9._:/-]{1,250}$/;
const ALIAS_KEY_RE = /^(task|intake|ci|pr|issue|path|mail):[A-Za-z0-9._:/-]{1,250}$/;
const TASK_WORK_KEY_RE = /^task:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/;
const COMMIT_SHA_RE = /^[0-9a-f]{7,40}$/;
const MODE_RE = /^(edit|read|review|coord)$/;

/** Host-attested synthetic actor for bridged native sessions (design 3.3). */
const NATIVE_ACTOR_RE = /^native:([A-Za-z0-9][A-Za-z0-9._-]{0,63}):([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?:\[([0-9a-fA-F-]{4,64})\])?$/;

export function formatNativeActor(machineId: string, tag: string, sessionId?: string): string {
  const short = (sessionId || '').replace(/-/g, '').slice(0, 8);
  return short ? `native:${machineId}:${tag}[${short}]` : `native:${machineId}:${tag}`;
}

export function parseNativeActor(actor: unknown): { machineId: string; tag: string; sessionShort: string | null } | null {
  if (typeof actor !== 'string') return null;
  const m = NATIVE_ACTOR_RE.exec(actor);
  if (!m) return null;
  return { machineId: m[1], tag: m[2], sessionShort: m[3] ?? null };
}

/** `project:<project>` broadcast address for mirrored `work.*` envelopes (design 3.3). */
export function projectBroadcastActor(project: string): string {
  return `project:${project}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

function strLen(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max;
}

/**
 * Map a canonical `work_key` into a Hub claim scope under the reserved
 * `@work/` namespace (design 4.1): `:` becomes `/` so the existing
 * path-segment prefix matcher works without modification.
 */
export function workKeyToScope(workKey: string): string | null {
  if (!WORK_KEY_RE.test(workKey)) return null;
  return `@work/${workKey.replace(/:/g, '/')}`;
}

/** Extract the Hub task UUID from a `task:<uuid>` work_key for UI linking. */
export function taskIdFromWorkKey(workKey: unknown): string | null {
  if (typeof workKey !== 'string') return null;
  const m = TASK_WORK_KEY_RE.exec(workKey);
  return m ? m[1].toLowerCase() : null;
}

/**
 * Parse an `a work join --task` string per the design 9.1 convention:
 * `<work_key> [hub_task=<uuid>] [claim=<claim_id>] [ttl=<sec>s] | <summary>`.
 * Lenient: returns the raw string as summary when no work_key prefix matches.
 */
export function parseWorkTaskString(task: unknown): {
  workKey: string | null;
  claimId: string | null;
  ttlSec: number | null;
  summary: string;
} {
  if (typeof task !== 'string' || !task) return { workKey: null, claimId: null, ttlSec: null, summary: '' };
  const pipe = task.indexOf('|');
  const head = (pipe >= 0 ? task.slice(0, pipe) : task).trim();
  const summary = (pipe >= 0 ? task.slice(pipe + 1) : '').trim().slice(0, 280);
  const parts = head.split(/\s+/).filter(Boolean);
  const workKey = parts.length > 0 && WORK_KEY_RE.test(parts[0]) ? parts[0] : null;
  let claimId: string | null = null;
  let ttlSec: number | null = null;
  for (const part of parts.slice(1)) {
    const m = /^\[([^\]]+)\]$/.exec(part);
    if (!m) continue;
    const inner = m[1];
    if (inner.startsWith('claim=')) {
      const id = inner.slice('claim='.length);
      if (CLAIM_ID_RE.test(id)) claimId = id;
    } else if (inner.startsWith('ttl=')) {
      const t = /^(\d+)s$/.exec(inner.slice('ttl='.length));
      if (t) {
        const n = parseInt(t[1], 10);
        if (Number.isSafeInteger(n) && n >= 60 && n <= 14400) ttlSec = n;
      }
    }
  }
  return { workKey, claimId, ttlSec, summary };
}

function validateHolder(holder: unknown): string | null {
  if (!isRecord(holder)) return 'holder must be an object';
  const allowed = ['machine_id', 'session_id', 'tag', 'engine', 'hub_agent', 'hub_session_id'];
  for (const k of Object.keys(holder)) {
    if (!allowed.includes(k)) return `holder has forbidden field ${k}`;
  }
  if (!strLen(holder.machine_id, 64)) return 'holder.machine_id invalid';
  if (!strLen(holder.session_id, 64)) return 'holder.session_id invalid';
  if (!strLen(holder.tag, 128)) return 'holder.tag invalid';
  if (holder.engine !== undefined && !strLen(holder.engine, 64)) return 'holder.engine invalid';
  if (holder.hub_agent !== undefined && !strLen(holder.hub_agent, 128)) return 'holder.hub_agent invalid';
  if (holder.hub_session_id !== undefined && !strLen(holder.hub_session_id, 64)) return 'holder.hub_session_id invalid';
  return null;
}

function validateLineage(lineage: unknown): string | null {
  if (lineage === undefined) return null;
  if (!isRecord(lineage)) return 'lineage must be an object';
  const allowed = [
    'task_id', 'parent_claim_id', 'parent_session_id', 'root_work_key',
    'replaces_hub_session_id', 'stolen_from_claim_id', 'stolen_from_session_id',
    'stolen_reason', 'prior_worktree_state', 'handoff_to',
  ];
  for (const k of Object.keys(lineage)) {
    if (!allowed.includes(k)) return `lineage has forbidden field ${k}`;
  }
  const strFields: Array<[string, number]> = [
    ['task_id', 64], ['parent_claim_id', 64], ['parent_session_id', 64],
    ['root_work_key', 256], ['replaces_hub_session_id', 64],
    ['stolen_from_claim_id', 64], ['stolen_from_session_id', 64], ['handoff_to', 128],
  ];
  for (const [field, max] of strFields) {
    const v = lineage[field];
    if (v !== undefined && !strLen(v, max)) return `lineage.${field} invalid`;
  }
  const stolen = lineage.stolen_reason;
  if (stolen !== undefined && !['process_exited', 'ttl_expired', 'host_offline', 'operator_override'].includes(stolen as string)) {
    return 'lineage.stolen_reason invalid';
  }
  const prior = lineage.prior_worktree_state;
  if (prior !== undefined && !['clean', 'dirty_preserved', 'not_found', 'unknown'].includes(prior as string)) {
    return 'lineage.prior_worktree_state invalid';
  }
  return null;
}

function validateOutcome(outcome: unknown): string | null {
  if (outcome === undefined) return null;
  if (!isRecord(outcome)) return 'outcome must be an object';
  const allowed = ['status', 'reason', 'fix_commit_sha', 'branch', 'report_path', 'dedup_cooldown_sec'];
  for (const k of Object.keys(outcome)) {
    if (!allowed.includes(k)) return `outcome has forbidden field ${k}`;
  }
  if (outcome.status !== undefined && !['completed', 'abandoned', 'handed_off', 'stolen'].includes(outcome.status as string)) {
    return 'outcome.status invalid';
  }
  if (outcome.reason !== undefined && !strLen(outcome.reason, 280)) return 'outcome.reason invalid';
  if (outcome.fix_commit_sha !== undefined && (typeof outcome.fix_commit_sha !== 'string' || !COMMIT_SHA_RE.test(outcome.fix_commit_sha))) {
    return 'outcome.fix_commit_sha invalid';
  }
  if (outcome.branch !== undefined && !strLen(outcome.branch, 128)) return 'outcome.branch invalid';
  if (outcome.report_path !== undefined && !strLen(outcome.report_path, 512)) return 'outcome.report_path invalid';
  if (outcome.dedup_cooldown_sec !== undefined && (!isInt(outcome.dedup_cooldown_sec) || outcome.dedup_cooldown_sec < 0 || outcome.dedup_cooldown_sec > 86400)) {
    return 'outcome.dedup_cooldown_sec invalid';
  }
  return null;
}

function validateScopes(scopes: unknown): string | null {
  if (scopes === undefined) return null;
  if (!Array.isArray(scopes) || scopes.length > 64) return 'scopes must be an array of at most 64';
  for (const s of scopes) {
    if (typeof s !== 'string' || !s || s.length > 512) return 'scope entry too long or empty';
    if (s.includes('..') || s.includes('\\') || s.startsWith('/')) return `scope entry not workspace-relative: ${s.slice(0, 64)}`;
  }
  return null;
}

/**
 * Strict validation of a `hub.work-claim.v1` payload (design 9.2).
 * Returns null when valid, otherwise a short machine-readable reason.
 * Unknown top-level fields are forbidden (schema allowlist, design 7.10).
 */
export function validateWorkClaimData(data: unknown): string | null {
  if (!isRecord(data)) return 'payload must be an object';
  const allowed = [
    'schema', 'event', 'claim_id', 'seq', 'work_key', 'alias_keys',
    'project', 'workspace', 'worktree', 'mode', 'scopes', 'holder',
    'lineage', 'lease_ttl_sec', 'outcome', 'summary',
  ];
  for (const k of Object.keys(data)) {
    if (!allowed.includes(k)) return `forbidden field ${k}`;
  }
  if (data.schema !== WORK_CLAIM_SCHEMA) return 'schema must be hub.work-claim.v1';
  if (!WORK_CLAIM_EVENTS.includes(data.event as WorkClaimEvent)) return 'event invalid';
  if (typeof data.claim_id !== 'string' || !CLAIM_ID_RE.test(data.claim_id)) return 'claim_id invalid';
  if (!isInt(data.seq) || data.seq < 1) return 'seq invalid';
  if (typeof data.work_key !== 'string' || !WORK_KEY_RE.test(data.work_key)) return 'work_key invalid';
  if (data.alias_keys !== undefined) {
    if (!Array.isArray(data.alias_keys) || data.alias_keys.length > 16) return 'alias_keys invalid';
    for (const a of data.alias_keys) {
      if (typeof a !== 'string' || !ALIAS_KEY_RE.test(a)) return 'alias_keys entry invalid';
    }
  }
  if (!strLen(data.project, 64)) return 'project invalid';
  if (typeof data.workspace !== 'string' || !data.workspace || data.workspace.length > 1024) return 'workspace invalid';
  if (data.worktree !== undefined && (typeof data.worktree !== 'string' || !data.worktree || data.worktree.length > 1024)) {
    return 'worktree invalid';
  }
  if (typeof data.mode !== 'string' || !MODE_RE.test(data.mode)) return 'mode invalid';
  const scopesErr = validateScopes(data.scopes);
  if (scopesErr) return scopesErr;
  const holderErr = validateHolder(data.holder);
  if (holderErr) return holderErr;
  const lineageErr = validateLineage(data.lineage);
  if (lineageErr) return lineageErr;
  if (!isInt(data.lease_ttl_sec) || data.lease_ttl_sec < 60 || data.lease_ttl_sec > 14400) return 'lease_ttl_sec invalid';
  const outcomeErr = validateOutcome(data.outcome);
  if (outcomeErr) return outcomeErr;
  if (data.summary !== undefined && (typeof data.summary !== 'string' || data.summary.length > 280)) return 'summary invalid';
  return null;
}

// ---------------------------------------------------------------------------
// Secret redaction (design 7.10). Deterministic allowlist scrubbing applied
// to every string that crosses into Hub SQLite. Ordered so URL query
// redaction runs before the generic high-entropy heuristic.
// ---------------------------------------------------------------------------

const TOKEN_RE = /(hubt_[A-Za-z0-9_-]+|hubr_[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[A-Za-z0-9-]+)/g;
// Value stops at `&`/`#` as well as whitespace so URL neighbors survive;
// the URL pass runs first and this stays idempotent on `[REDACTED]`.
const BEARER_ENV_RE = /(authorization\s*[:=]\s*bearer\s+|token\s*[:=]\s*|password\s*[:=]\s*|secret\s*[:=]\s*|api[_-]?key\s*[:=]\s*)([^\s"',;&#]+)/gi;
const AWS_KEY_RE = /(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})/g;
const PEM_RE = /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g;
const URL_USERINFO_RE = /(https?:\/\/)[^/\s@]+@/gi;
const URL_QUERY_RE = /([?&](?:token|key|sig|signature|secret|auth|password|code|state|x-amz-[^=&#\s]+)=)[^&#\s]*/gi;
// NOTE: `/` and `=` are deliberately excluded from the run: paths and
// `key=value` pairs would otherwise glue into false positives (URL query
// secrets are handled by URL_QUERY_RE; padding `=` never starts a token).
const HIGH_ENTROPY_RE = /[A-Za-z0-9+_-]{40,}/g;
const COMMIT_SHA40_RE = /^[0-9a-f]{40}$/;

/** Redact one free-text string per design 7.10. Non-strings pass through. */
export function redactForBus(value: unknown): unknown {
  if (typeof value !== 'string' || !value) return value;
  let out = value;
  out = out.replace(PEM_RE, '[REDACTED_PRIVATE_KEY]');
  out = out.replace(TOKEN_RE, '[REDACTED_TOKEN]');
  out = out.replace(URL_USERINFO_RE, '$1');
  out = out.replace(URL_QUERY_RE, '$1[REDACTED]');
  out = out.replace(BEARER_ENV_RE, '$1[REDACTED]');
  out = out.replace(AWS_KEY_RE, '[REDACTED_AWS_KEY]');
  out = out.replace(HIGH_ENTROPY_RE, (token) => {
    // Recognized non-secret identifiers survive the heuristic: a lowercase
    // hex commit SHA, or a token that is only digits (counts, timestamps).
    if (COMMIT_SHA40_RE.test(token)) return token;
    if (/^[0-9]+$/.test(token)) return token;
    return '[REDACTED_HIGH_ENTROPY]';
  });
  return out;
}

/** Deep-redact every string value inside a validated claim payload. */
export function redactClaimData<T>(data: T): T {
  if (typeof data === 'string') return redactForBus(data) as unknown as T;
  if (Array.isArray(data)) return data.map((v) => redactClaimData(v)) as unknown as T;
  if (isRecord(data)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(data)) out[k] = redactClaimData(v);
    return out as unknown as T;
  }
  return data;
}

/** Columns added to the existing `claims` table for host-attested native rows. */
export const NATIVE_CLAIM_COLUMNS: Record<string, string> = {
  work_key: 'TEXT',
  claim_mode: 'TEXT',
  claim_source: 'TEXT',
  worktree: 'TEXT',
  session_state: 'TEXT',
  native_claim_id: 'TEXT',
};

/** A native declaration carries no exclusive weight (design edge case 9). */
export function isExclusiveClaimMode(mode: unknown): boolean {
  return mode === 'edit' || mode === 'coord' || mode === undefined || mode === null;
}
