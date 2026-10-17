/** M15 session lifecycle registry (projection + plan seam, head-corrected).
 *
 * READONLY integration posture: this module reuses canonical M12 stores and
 * never duplicates session identity/provenance/control state. Canonical sources
 * (owned elsewhere):
 * - fleet_sessions / fleet_launches (src/fleet.ts FLEET_SCHEMA)
 * - intake link events {kind:link} (src/intake.ts)
 * - session_control sessions/commands (R3 candidate, head recovery in progress;
 *   server classifies the canonical record, host validates effects)
 *
 * Head-ACKed contract direction (NOT wired here; exact route glue waits M12 agreement):
 * - projection: additive `GET /api/sessions?view=lifecycle` (never a second `/api/lifecycle` registry)
 * - cleanup: `POST /api/sessions/cleanup` (dry-run default; apply is OPEN M15c/d)
 * - decisions: existing POST decision seam (durable server decisions are OPEN M15c/d)
 * - CLI: `hub sessions` (default grouped lifecycle listing) + `hub sessions cleanup`;
 *   existing `list/start/send/capture/stop` are preserved, no substitute `lifecycle-*` spelling.
 *
 * This module is pure except for the additive schema string:
 * - classifySession / projectSessions: readonly grouped projection (M15a)
 * - planCleanup / validateCleanupTarget / validateOwnershipTransfer: pure seams (M15b)
 * - toStatusDto / toHandoffDto: shared DTO seam for M14 status / M12 handoff
 *
 * Privacy: default projection carries allowlisted structured reason codes + provenance only.
 * Free-text snippets are opt-in (`note_text_opt_in`), bounded, redacted, and always data.
 * Dry-run planning performs no I/O. No fake apply success is implemented here; the host
 * validates effects and the server classifies the canonical record. Durable decisions,
 * server CLI/apply executor, and host acceptance are explicit OPEN M15c/d.
 */

export const SESSION_LIFECYCLE_SCHEMA = `
CREATE TABLE IF NOT EXISTS session_lifecycle_observations(session_key TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_lifecycle_decisions(session_key TEXT NOT NULL, decision_id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, engine TEXT, target TEXT, created_at TEXT NOT NULL);
`;

export type LifecycleGroup =
  | 'waiting_alexey'
  | 'limit_blocked'
  | 'finished_stale'
  | 'active'
  | 'unknown';

export type SessionInput = {
  machine_id: string;
  native_id: string;
  tag?: string | null;
  engine?: string | null;
  workspace?: string | null;
  starter?: string | null;
  owner?: string | null;
  owner_provenance?: 'authenticated_handoff' | 'starter' | 'unknown' | null;
  human_owned?: boolean;
  task_refs?: string[];
  last_activity_at?: string | null;
  observed_at?: string | null;
  phase?: string | null;
  agent_state?: string | null;
  /** Fresh-provenance observation flags (host-verified, not bare banners). */
  awaiting_alexey?: boolean;
  awaiting_alexey_at?: string | null;
  limit_hit?: boolean;
  limit_window?: string | null;
  limit_reset_at?: string | null;
  limit_zone?: string | null;
  report_written?: boolean;
  report_at?: string | null;
  screen_done?: boolean;
  /** Fresh explicit terminal evidence (completion/supersession record with provenance). */
  terminal_evidence?: 'explicit_completion' | 'supersession_record' | null;
  terminal_evidence_at?: string | null;
  /** Independent authoritative proof of nothing pending (server/host verified, not a banner). */
  authoritative_no_pending_proof?: boolean;
  pending_obligations?: boolean;
  has_active_children?: boolean;
  has_unsubmitted_draft?: boolean;
  has_unresolved_send_stop?: boolean;
  worktree_missing?: boolean;
  superseded_by?: string | null;
  observation_note?: string | null;
  /** Opt-in to include bounded redacted free text; default false (reason codes only). */
  note_text_opt_in?: boolean;
};

export type ClassifiedSession = {
  session_key: string;
  group: LifecycleGroup;
  state: string;
  reasons: string[];
  /** All simultaneous flags preserved even when grouping picks one primary. */
  flags: string[];
  /** Open obligations retained for status/handoff even when grouped finished. */
  obligations: string[];
  sanitized_note: string | null;
};

export type LifecycleConfig = { idle_hours?: number; now_ms?: number };

const ISO = (v: unknown): number | null => {
  if (typeof v !== 'string' || !v) return null;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : null;
};

/** Bounded redaction for opt-in text only. Regex alone is not a privacy guarantee:
 * callers must prefer reason codes; this is a backstop, not a promise. */
export function sanitizeObservationText(raw: unknown, max = 512): string | null {
  if (raw == null) return null;
  if (typeof raw !== 'string') return null;
  let text = raw.slice(0, max);
  text = text.replace(/[A-Za-z0-9_\-+/=]{20,}/g, '[redacted]');
  text = text.replace(/(token|secret|password|bearer)[^\n]{0,40}/gi, '[redacted]');
  return text;
}

function sessionKey(input: SessionInput): string {
  return `${input.machine_id}/${input.native_id}`;
}

function hoursSince(iso: string | null | undefined, now: number): number | null {
  const t = iso ? ISO(iso) : null;
  if (t == null) return null;
  return (now - t) / 3600000;
}

function collectFlags(input: SessionInput, now: number): { flags: string[]; obligations: string[] } {
  const flags: string[] = [];
  const obligations: string[] = [];
  if (input.worktree_missing) flags.push('missing_worktree');
  if (input.awaiting_alexey) {
    const at = input.awaiting_alexey_at ? ISO(input.awaiting_alexey_at) : null;
    flags.push(at != null && now >= at && now - at <= 24 * 3600000 ? 'waiting_alexey_fresh' : 'waiting_alexey_stale');
  }
  if (input.limit_hit) flags.push(input.limit_reset_at && ISO(input.limit_reset_at) != null ? 'limit_known_reset' : 'limit_unknown_reset');
  if (input.pending_obligations) obligations.push('pending_obligations');
  if (input.has_active_children) obligations.push('active_children');
  if (input.has_unsubmitted_draft) obligations.push('unsubmitted_draft');
  if (input.has_unresolved_send_stop) obligations.push('unresolved_send_stop');
  if (input.report_written) flags.push('report_present');
  if (input.screen_done) flags.push('screen_done');
  if (input.terminal_evidence) flags.push(`terminal_${input.terminal_evidence}`);
  if (input.superseded_by) flags.push('superseded_marker');
  return { flags, obligations };
}

/** Deterministic classification. Grouping picks one primary; flags+obligations preserve the rest. */
export function classifySession(input: SessionInput, config: LifecycleConfig = {}): ClassifiedSession {
  const now = config.now_ms ?? Date.now();
  const idleHours = config.idle_hours ?? 24;
  const key = sessionKey(input);
  const reasons: string[] = [];
  const { flags, obligations } = collectFlags(input, now);
  const note = input.note_text_opt_in ? sanitizeObservationText(input.observation_note ?? null) : null;

  if (input.worktree_missing) {
    reasons.push('worktree record present but host reports workspace absent');
    return { session_key: key, group: 'unknown', state: 'missing_worktree_unknown', reasons, flags, obligations, sanitized_note: note };
  }
  if (input.awaiting_alexey) {
    const at = input.awaiting_alexey_at ? ISO(input.awaiting_alexey_at) : null;
    if (at != null && now - at <= 24 * 3600000 && now >= at) {
      reasons.push('fresh question/approval pending for Alexey');
      return { session_key: key, group: 'waiting_alexey', state: 'waiting_alexey', reasons, flags, obligations, sanitized_note: note };
    }
    reasons.push('stale awaiting-Alexey evidence ignored');
  }
  if (input.limit_hit) {
    const window = typeof input.limit_window === 'string' && input.limit_window ? input.limit_window : 'unknown';
    const reset = input.limit_reset_at ? ISO(input.limit_reset_at) : null;
    reasons.push(`limit pattern matched window=${window} reset=${reset != null ? 'known' : 'unknown'}; unknown stays needs-action, never resumes from timestamp alone`);
    return {
      session_key: key,
      group: 'limit_blocked',
      state: reset != null ? 'limit_blocked' : 'limit_blocked_unknown_reset',
      reasons,
      flags,
      obligations,
      sanitized_note: note,
    };
  }
  const noObligations = !input.pending_obligations && !input.has_active_children &&
    !input.has_unsubmitted_draft && !input.has_unresolved_send_stop;
  const activity = input.last_activity_at ? ISO(input.last_activity_at) : null;
  const report = input.report_at ? ISO(input.report_at) : null;
  const terminalAt = input.terminal_evidence_at ? ISO(input.terminal_evidence_at) : null;
  const terminalFresh = input.terminal_evidence != null && terminalAt != null && now >= terminalAt && now - terminalAt <= 24 * 3600000;
  const reportFreshEnough = report != null && (activity == null || activity <= report);
  const finishedSignals = Boolean(input.report_written && input.screen_done);
  // Fresh explicit terminal evidence + independent authoritative no-pending proof can qualify
  // without BOTH report and screen; neither report nor screen alone ever suffices.
  if (input.superseded_by && noObligations && (finishedSignals || (terminalFresh && input.authoritative_no_pending_proof))) {
    reasons.push(`superseded by ${input.superseded_by} with authoritative no-pending proof`);
    return { session_key: key, group: 'finished_stale', state: 'superseded', reasons, flags, obligations, sanitized_note: note };
  }
  if (input.superseded_by && !noObligations) reasons.push('superseded marker present but obligations remain');
  if (noObligations && input.authoritative_no_pending_proof && (finishedSignals || terminalFresh) && (reportFreshEnough || terminalFresh)) {
    reasons.push('terminal completion with authoritative no-pending proof and no newer activity');
    return { session_key: key, group: 'finished_stale', state: 'finished', reasons, flags, obligations, sanitized_note: note };
  }
  if ((input.report_written || input.screen_done || input.terminal_evidence) && (!noObligations || (!reportFreshEnough && !terminalFresh))) {
    reasons.push('report/screen/terminal alone never qualifies without authoritative no-pending proof');
  }
  const idle = hoursSince(input.last_activity_at ?? null, now);
  if (idle != null && idle >= idleHours) {
    reasons.push(`idle ${idle.toFixed(1)}h >= ${idleHours}h threshold`);
    return { session_key: key, group: 'active', state: 'idle', reasons, flags, obligations, sanitized_note: note };
  }
  const phase = (input.phase ?? '').toLowerCase();
  if (phase === 'running' || idle == null || idle < idleHours) {
    if (idle == null) reasons.push('no activity timestamp; treating as unknown-fresh active');
    else reasons.push('fresh activity within idle threshold');
    return { session_key: key, group: 'active', state: phase || 'active', reasons, flags, obligations, sanitized_note: note };
  }
  reasons.push('insufficient fresh provenance');
  return { session_key: key, group: 'unknown', state: 'unknown', reasons, flags, obligations, sanitized_note: note };
}

const GROUP_RANK: Record<LifecycleGroup, number> = {
  waiting_alexey: 0,
  limit_blocked: 1,
  finished_stale: 2,
  active: 3,
  unknown: 4,
};

export type ProjectionScope = { owner?: string | null; task?: string | null; host?: string | null };

/** Readonly grouped projection: deterministic sort, bounded page, scoped to caller. Pure. */
export function projectSessions(
  inputs: SessionInput[],
  scope: ProjectionScope = {},
  config: LifecycleConfig & { limit?: number; cursor?: string | null } = {},
): { groups: Record<LifecycleGroup, ClassifiedSession[]>; next_cursor: string | null; total: number } {
  const limit = Math.min(Math.max(config.limit ?? 50, 1), 50);
  const filtered = inputs.filter((s) => {
    if (scope.host && s.machine_id !== scope.host) return false;
    if (scope.owner && (s.owner ?? null) !== scope.owner) return false;
    if (scope.task && !(s.task_refs ?? []).includes(scope.task)) return false;
    return true;
  });
  const classified = filtered.map((s) => classifySession(s, config));
  classified.sort((a, b) => {
    const r = GROUP_RANK[a.group] - GROUP_RANK[b.group];
    if (r !== 0) return r;
    return a.session_key < b.session_key ? -1 : a.session_key > b.session_key ? 1 : 0;
  });
  let start = 0;
  if (config.cursor) {
    const idx = classified.findIndex((c) => c.session_key === config.cursor);
    start = idx >= 0 ? idx + 1 : 0;
  }
  const page = classified.slice(start, start + limit);
  const groups: Record<LifecycleGroup, ClassifiedSession[]> = {
    waiting_alexey: [],
    limit_blocked: [],
    finished_stale: [],
    active: [],
    unknown: [],
  };
  for (const c of page) groups[c.group].push(c);
  const next_cursor = start + limit < classified.length ? page[page.length - 1].session_key : null;
  return { groups, next_cursor, total: classified.length };
}

export type CleanupCandidate = ClassifiedSession & {
  owner: string | null;
  human_owned: boolean;
  pending_obligations: boolean;
};

/** Pure cleanup plan (M15b seam). Dry-run by construction: no I/O, no fake apply.
 * Durable server decisions + apply executor + host acceptance are OPEN M15c/d. */
export function planCleanup(
  candidates: CleanupCandidate[],
  callerOwner: string,
): { eligible: CleanupCandidate[]; ineligible: { key: string; reasons: string[] }[] } {
  const eligible: CleanupCandidate[] = [];
  const ineligible: { key: string; reasons: string[] }[] = [];
  for (const c of candidates) {
    const blockers: string[] = [];
    if (c.human_owned) blockers.push('human/Alexey session permanently protected');
    if ((c.owner ?? null) !== callerOwner) blockers.push('not owned by caller (admin is not ownership)');
    if (c.owner == null || c.owner === 'unknown' || c.owner === 'legacy') {
      blockers.push('unknown/legacy owner cannot authorize cleanup');
    }
    if (c.group !== 'finished_stale') blockers.push(`group ${c.group} never eligible (idle/limit/waiting alone excluded)`);
    if (c.pending_obligations || c.obligations.length) blockers.push('pending obligations remain');
    if (!['finished', 'superseded'].includes(c.state)) blockers.push(`state ${c.state} not confirmed finished/superseded`);
    if (blockers.length) ineligible.push({ key: c.session_key, reasons: blockers });
    else eligible.push(c);
  }
  return { eligible, ineligible };
}

export type CleanupTargetCheck = {
  session_key: string;
  expected_machine_id: string;
  expected_native_id: string;
  expected_owner: string;
  observed_machine_id: string;
  observed_native_id: string;
  observed_owner: string;
  busy_or_draft_or_pending: boolean;
  contained: boolean;
};

/** Safe executor seam gate (pure). Host validates the effect; server classifies the record. */
export function validateCleanupTarget(check: CleanupTargetCheck): { ok: true } | { ok: false; error: string } {
  if (check.observed_machine_id !== check.expected_machine_id) return { ok: false, error: 'machine_mismatch' };
  if (check.observed_native_id !== check.expected_native_id) return { ok: false, error: 'incarnation_mismatch' };
  if (check.observed_owner !== check.expected_owner) return { ok: false, error: 'custodian_changed' };
  if (check.busy_or_draft_or_pending) return { ok: false, error: 'busy_draft_pending_guard' };
  if (!check.contained) return { ok: false, error: 'containment_failed' };
  return { ok: true };
}

export type OwnershipTransferRequest = {
  session_key: string;
  current_owner: string;
  current_epoch: number;
  expected_owner: string;
  expected_epoch: number;
  new_owner: string;
  actor: string;
  human_owned: boolean;
};

/** Dedicated ownership-transfer gate (pure seam). Generic intake text is NOT a transfer:
 * requires the authenticated current owner + epoch fence; starter stays immutable;
 * human protection can never be disabled; unknown owners never apply. */
export function validateOwnershipTransfer(req: OwnershipTransferRequest): { ok: true } | { ok: false; error: string } {
  if (req.human_owned) return { ok: false, error: 'human_protected' };
  if (!req.new_owner || req.new_owner === 'unknown' || req.new_owner === 'legacy') return { ok: false, error: 'unknown_owner_never_applies' };
  if (req.expected_owner !== req.current_owner) return { ok: false, error: 'stale_owner' };
  if (!Number.isInteger(req.expected_epoch) || !Number.isInteger(req.current_epoch) || req.expected_epoch !== req.current_epoch) {
    return { ok: false, error: 'epoch_fence_failed' };
  }
  if (!req.actor || req.actor !== req.current_owner) return { ok: false, error: 'only_current_owner_transfers' };
  if (req.new_owner === req.current_owner) return { ok: false, error: 'no_op_transfer' };
  return { ok: true };
}

/** Shared DTO seam for M14 status / M12 handoff. Same facts incl. flags+obligations, no second tracker. */
export function toStatusDto(c: ClassifiedSession): Record<string, unknown> {
  return { session_key: c.session_key, group: c.group, state: c.state, reasons: c.reasons, flags: c.flags, obligations: c.obligations, note: c.sanitized_note };
}

export function toHandoffDto(c: ClassifiedSession): Record<string, unknown> {
  return { ...toStatusDto(c), text_as_data: true };
}
