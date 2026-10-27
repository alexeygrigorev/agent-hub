import { jsonFields } from './sql-json-projection';
/** M15c live durable lifecycle registry (server authority).
 *
 * This module is the ONE durable/server authority for lifecycle state. It
 * reuses the canonical M12 stores and never duplicates session
 * identity/provenance/control state:
 * - session_control_sessions / session_control_commands (durable intents)
 * - fleet_launches / fleet_sessions (placement + host-observed truth)
 * - intake_tasks link/report events (task linkage; canonical links only)
 * - session_lifecycle_observations / session_lifecycle_decisions (additive
 *   tables created here; session identity is never duplicated, only keyed)
 *
 * Trust model (mirrors src/session_lifecycle.ts):
 * - starter/owner/human_owned come ONLY from authenticated canonical rows
 *   (fleet_launches.actor + requester_kind, session_control_sessions.actor).
 *   JSON owner/human flags in observation/decision payloads are untrusted
 *   input and are never read here (allowlist merge drops them).
 * - observations are host-verified facts (machine-scoped host auth) or
 *   operator records (admin). Agent writers cannot write observations.
 * - limit-block decisions are owner-only: the live session owner records
 *   resume_on_reset / reassign / drop. Admin privilege is NEVER another
 *   coordinator's ownership: admins cannot decide for, or clean up,
 *   sessions they do not own. Human/Alexey-owned records are protected
 *   (no decisions, no cleanup, no transfer).
 * - classification/planning stay pure in src/session_lifecycle.ts (single
 *   authority; no second classifier here). This module only joins inputs,
 *   persists observations/decisions, and shapes DTOs.
 * - cleanup dry-run performs ZERO writes. --apply always refuses
 *   explicitly (M15d owns the executor seam); no fake apply success.
 */

import type { SqlStorage } from '@cloudflare/workers-types';
import {
  SESSION_LIFECYCLE_SCHEMA,
  classifySession,
  projectSessions,
  planCleanup,
  sanitizeObservationText,
  toStatusDto,
  type ClassifiedSession,
  type LifecycleGroup,
  type SessionInput,
} from './session_lifecycle';

export type DecisionKind = 'resume_on_reset' | 'reassign' | 'drop';
export const DECISION_KINDS: ReadonlySet<string> = new Set(['resume_on_reset', 'reassign', 'drop']);

/** M15d shipped: native stop is enqueued as real M12 stop intents
 * (server custody) and executed by the host receiver (containment-confirmed);
 * owned worktree/branch removal runs host-side under the trusted
 * cleanup-root allowlist with clean-and-merged guards. Kept for history. */
export const APPLY_REFUSAL_NEXT_SEAM =
  'M15d executor seam (head-assigned next): host-side machine/native/owner ' +
  'revalidation + containment + owned worktree/branch clean-and-merged ' +
  'guards + live throwaway acceptance. No native/Git effect runs in M15c.';

export type LifecycleRefs = {
  session_id: string | null;
  fleet_launch_id: string | null;
  machine_id: string;
  native_id: string;
  engine: string | null;
  workspace: string | null;
  cwd: string | null;
  hub_state: string | null;
  fleet_state: string | null;
  starter: string | null;
  owner: string | null;
  human_owned: boolean;
  protected_session: boolean;
  task_refs: string[];
  report_refs: string[];
  has_brief: boolean;
};

export type LifecycleDecision = {
  session_key: string;
  decision_id: string;
  kind: DecisionKind;
  actor: string;
  reason: string;
  engine: string | null;
  target: string | null;
  created_at: string;
};

export type LifecycleEntry = ClassifiedSession & {
  refs: LifecycleRefs;
  decision: LifecycleEntryDecision;
};

type LifecycleEntryDecision = LifecycleDecision | null;

export type LifecycleScope = { owner?: string | null; task?: string | null; host?: string | null };

const parsed = (value: unknown, fallback: any = null): any => {
  try {
    return typeof value === 'string' ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
};

const isValidBindingId = (v: unknown): boolean => {
  if (typeof v !== 'string') return false;
  const t = v.trim().toLowerCase();
  return t.length > 0 && t !== 'unknown' && t !== 'legacy';
};

const isHumanStarter = (starter: string | null, requesterKind: string | null): boolean => {
  if (requesterKind === 'human') return true;
  if (!starter) return false;
  return starter === 'admin' || starter.startsWith('human:');
};

/** Additive table init. Called from SessionControlService.init only;
 * never from the global schema migration path. */
export function initLifecycleTables(sql: SqlStorage): void {
  for (const stmt of SESSION_LIFECYCLE_SCHEMA.split(';')) {
    if (stmt.trim()) sql.exec(stmt);
  }
}

type JoinedBase = {
  key: string;
  machine_id: string;
  native_id: string;
  sc: any | null;
  launch: any | null;
  fleet: any | null;
};

/** Request bodies are immutable between canonical request writes. Keep only
 * lifecycle fields, atomically maintained even when native telemetry changes. */
export function initLifecycleRequestProjection(sql: SqlStorage): void {
  sql.exec(`CREATE TABLE IF NOT EXISTS lifecycle_request_projection (
    id TEXT PRIMARY KEY,request_json TEXT NOT NULL,has_brief INTEGER NOT NULL)`);
  const brief = (column: string) => `COALESCE(json_type(CASE WHEN json_valid(${column}) THEN ${column} ELSE '{}' END,'$.brief')='text',0)`;
  const put = `INSERT OR REPLACE INTO lifecycle_request_projection VALUES
    (NEW.id,${jsonFields('NEW.request_json',['provenance','workspace','cwd'])},${brief('NEW.request_json')});`;
  sql.exec(`CREATE TRIGGER IF NOT EXISTS lifecycle_request_insert AFTER INSERT ON session_control_sessions BEGIN ${put} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS lifecycle_request_update AFTER UPDATE OF id,request_json ON session_control_sessions BEGIN
    DELETE FROM lifecycle_request_projection WHERE id=OLD.id; ${put} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS lifecycle_request_delete AFTER DELETE ON session_control_sessions BEGIN
    DELETE FROM lifecycle_request_projection WHERE id=OLD.id; END`);
  sql.exec(`INSERT INTO lifecycle_request_projection SELECT c.id,
    ${jsonFields('c.request_json',['provenance','workspace','cwd'])},${brief('c.request_json')}
    FROM session_control_sessions c WHERE NOT EXISTS(SELECT 1 FROM lifecycle_request_projection p WHERE p.id=c.id)`);
}

/** Canonical identity union. Only sessions with a native identity
 * (bound hub sessions, observed fleet sessions) are keyed; unbound
 * queued/held intents stay in the existing flat list view only. */
function joinedBases(sql: SqlStorage, sessionIds?: Set<string>): JoinedBase[] {
  const byKey = new Map<string, JoinedBase>();
  const get = (machine_id: string, native_id: string): JoinedBase => {
    const key = `${machine_id}/${native_id}`;
    let row = byKey.get(key);
    if (!row) {
      row = { key, machine_id, native_id, sc: null, launch: null, fleet: null };
      byKey.set(key, row);
    }
    return row;
  };
  // Status supplies only session ids referenced by its bounded task page.
  // Batch canonical joins rather than materializing the entire fleet archive.
  const selected = sessionIds ? [...sessionIds] : null;
  let projected = false;
  try { projected = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_request_projection'").toArray().length > 0; } catch {}
  const source = (table: string) => table === 'session_control_sessions' && projected
    ? `${table} c LEFT JOIN lifecycle_request_projection lp ON lp.id=c.id` : `${table} c`;
  const columns = (table: string) => {
    if (table === 'session_control_sessions') return `c.id,c.actor,c.machine_id,c.fleet_launch_id,c.binding_json,c.mode,c.session_type,
      c.owner_token_id,c.owner_family,c.state,c.updated_at,c.engine,
      ${projected ? `COALESCE(lp.request_json,${jsonFields('c.request_json',['provenance','workspace','cwd'])})` : jsonFields('c.request_json',['provenance','workspace','cwd'])} AS request_json,
      ${projected ? `CASE WHEN lp.id IS NOT NULL THEN lp.has_brief ELSE json_type(CASE WHEN json_valid(c.request_json) THEN c.request_json ELSE '{}' END,'$.brief')='text' END` : `json_type(CASE WHEN json_valid(c.request_json) THEN c.request_json ELSE '{}' END,'$.brief')='text'`} AS has_brief`;
    if (table === 'fleet_launches') return `c.id,c.machine_id,c.native_id,c.actor,c.requester_kind,c.mode,c.engine,c.workspace,c.state,
      typeof(c.prompt)='text' AS has_prompt`;
    return 'c.machine_id,c.native_id,c.engine,c.workspace,c.cwd,c.tag,c.last_activity_at,c.observed_at,c.phase,c.agent_state';
  };
  const read = (table: string): any[] => {
    const projection = columns(table);
    if (!selected) return sql.exec(`SELECT ${projection} FROM ${source(table)} ORDER BY c.rowid`).toArray() as any[];
    const rows = new Map<string, any>();
    for (let i = 0; i < selected.length; i += 1000) {
      const ids = selected.slice(i, i + 1000);
      const nativeId = `CASE WHEN json_valid(binding_json) THEN CASE WHEN json_type(binding_json, '$.native_id') = 'text' THEN json_extract(binding_json, '$.native_id') END END`;
      // Tuple-IN only seeks machine_id on SQLite's expression index. An
      // equality join with untyped RHS seeks the complete native identity.
      const targetNative = table === 'session_control_sessions'
        ? nativeId.replaceAll('binding_json', 'c.binding_json') : 'c.native_id';
      const prefix = `WITH selected AS (SELECT id,fleet_launch_id,machine_id,${nativeId} AS native_id
        FROM session_control_sessions WHERE id IN (SELECT value FROM json_each(?))) `;
      const nativeRows = `SELECT c.rowid AS matched_rowid FROM
        (SELECT DISTINCT machine_id,native_id FROM selected) s
        JOIN ${table} c ON c.machine_id=s.machine_id AND ${targetNative}=+s.native_id`;
      const matches = table === 'session_control_sessions'
        ? `SELECT c.rowid AS matched_rowid FROM ${table} c WHERE c.id IN (SELECT id FROM selected) UNION ${nativeRows}`
        : table === 'fleet_sessions' ? nativeRows
        : `SELECT c.rowid AS matched_rowid FROM ${table} c WHERE c.id IN
          (SELECT fleet_launch_id FROM selected) UNION ${nativeRows}`;
      // Deduplicate identity before projecting JSON: a selected bound control
      // row normally matches both branches, but its large request needs parsing
      // only once. Canonical rowid ordering still selects the oldest alias.
      const query = `SELECT c.rowid AS _join_order,${projection} FROM ${source(table)}
        WHERE c.rowid IN (${matches})`;
      for (const row of sql.exec(prefix + query, JSON.stringify(ids)).toArray() as any[]) rows.set(row.id ?? `${row.machine_id}/${row.native_id}`, row);
    }
    return [...rows.values()].sort((a, b) => a._join_order - b._join_order);
  };
  let scRows: any[] = [];
  try {
    scRows = read('session_control_sessions');
  } catch {
    scRows = [];
  }
  const launchesById = new Map<string, any>();
  try {
    for (const launch of read('fleet_launches')) {
      launchesById.set(launch.id, launch);
    }
  } catch { /* fleet tables predate; join degrades to session-control only */ }
  for (const sc of scRows) {
    const binding = sc.binding_json ? parsed(sc.binding_json) : null;
    if (!sc.machine_id || !binding?.native_id) continue;
    const base = get(sc.machine_id, binding.native_id);
    if (!base.sc) base.sc = sc;
  }
  let fleetRows: any[] = [];
  try {
    fleetRows = read('fleet_sessions');
  } catch {
    fleetRows = [];
  }
  for (const fleet of fleetRows) {
    if (!fleet.machine_id || !fleet.native_id) continue;
    if (sessionIds && !byKey.has(`${fleet.machine_id}/${fleet.native_id}`)) continue;
    const base = get(fleet.machine_id, fleet.native_id);
    if (!base.fleet) base.fleet = fleet;
  }
  const launchesByNative = new Map<string, any>();
  for (const launch of launchesById.values()) {
    const key = `${launch.machine_id}/${launch.native_id}`;
    if (!launchesByNative.has(key)) launchesByNative.set(key, launch);
  }
  for (const base of byKey.values()) {
    if (base.sc?.fleet_launch_id) base.launch = launchesById.get(base.sc.fleet_launch_id) ?? null;
    if (!base.launch) base.launch = launchesByNative.get(base.key) ?? null;
  }
  return [...byKey.values()];
}

type TaskLinks = { task_refs: string[]; report_refs: string[] };

/** Canonical intake links only: link events WITHOUT a routing_job_id are
 * external/unverified and never join — EXCEPT links carrying a
 * `hub_session_id` that exactly matches the joined canonical session row:
 * intake authorizes that reference at link time (operator or session owner),
 * so it is already an authenticated join endpoint. Report references ride
 * the linked tasks. Bounded; failures degrade to empty (never a hard error). */
/** Parse each history once per projection, not once per session. The maps
 * contain canonical link candidates; union in intake order preserves the
 * existing 25-task / 5-report bounds when a task links both identities. */
function taskLinkIndex(sql: SqlStorage): (native: string, hub: string | null) => TaskLinks {
  type LinkedTask = { order: number; id: string; reports: string[] };
  const natives = new Map<string, LinkedTask[]>();
  const hubs = new Map<string, LinkedTask[]>();
  try {
    const rows = sql.exec('SELECT id, links FROM intake_task_projection').toArray() as any[];
    for (const [order, row] of rows.entries()) {
      const task = { id: row.id, history: parsed(row.links, []) };
      if (!Array.isArray(task.history)) continue;
      const nativeIds = new Set<string>();
      const hubIds = new Set<string>();
      const reports: string[] = [];
      for (const event of task.history) {
        if (event?.kind === 'link') {
          if (typeof event.native_id === 'string' && event.routing_job_id) nativeIds.add(event.native_id);
          if (typeof event.hub_session_id === 'string') hubIds.add(event.hub_session_id);
        }
        if (event?.kind === 'report' && typeof event.reference === 'string' && event.reference && reports.length < 5) reports.push(event.reference);
      }
      const linked = { order, id: String(task.id ?? row.id), reports };
      for (const [index, ids] of [[natives, nativeIds], [hubs, hubIds]] as const) {
        for (const id of ids) {
          const list = index.get(id) ?? [];
          list.push(linked);
          index.set(id, list);
        }
      }
    }
  } catch { /* intake tables absent: unknown task linkage */ }
  return (native, hub) => {
    const tasks = [...new Set([...(natives.get(native) ?? []), ...(hub ? hubs.get(hub) ?? [] : [])])]
      .sort((a, b) => a.order - b.order).slice(0, 25);
    const report_refs: string[] = [];
    for (const task of tasks) for (const ref of task.reports) {
      if (report_refs.length < 5 && !report_refs.includes(ref)) report_refs.push(ref.slice(0, 512));
    }
    return { task_refs: tasks.map(t => t.id), report_refs };
  };
}

function observationsByKey(sql: SqlStorage, keys?: string[]): Map<string, any> {
  const map = new Map<string, any>();
  try {
    if (!keys) {
      for (const row of sql.exec('SELECT * FROM session_lifecycle_observations').toArray() as any[]) map.set(row.session_key, row);
    } else {
      for (let i=0;i<keys.length;i+=1000) {
        for (const row of sql.exec('SELECT * FROM session_lifecycle_observations WHERE session_key IN (SELECT value FROM json_each(?))',
          JSON.stringify(keys.slice(i,i+1000))).toArray() as any[]) map.set(row.session_key,row);
      }
    }
  } catch { /* pre-init: no observations yet */ }
  return map;
}

function decisionsByKey(sql: SqlStorage): Map<string, LifecycleDecision[]> {
  const map = new Map<string, LifecycleDecision[]>();
  try {
    for (const row of sql.exec('SELECT * FROM session_lifecycle_decisions ORDER BY created_at, decision_id').toArray() as any[]) {
      const list = map.get(row.session_key) ?? [];
      list.push({
        session_key: row.session_key,
        decision_id: row.decision_id,
        kind: row.kind,
        actor: row.actor,
        reason: row.reason,
        engine: row.engine ?? null,
        target: row.target ?? null,
        created_at: row.created_at,
      });
      map.set(row.session_key, list);
    }
  } catch { /* pre-init: no decisions yet */ }
  return map;
}

/** Observation payload fields the server accepts. owner/starter/human
 * flags are deliberately absent: JSON never authenticates ownership. */
const OBSERVABLE_FLAGS = new Set([
  'awaiting_alexey', 'awaiting_alexey_at',
  'waiting_confirmation', 'waiting_confirmation_at',
  'waiting_input', 'waiting_input_at',
  'limit_hit', 'limit_window', 'limit_reset_at', 'limit_zone',
  'report_written', 'report_at', 'screen_done',
  'terminal_evidence', 'terminal_evidence_at',
  'authoritative_no_pending_proof',
  'pending_obligations', 'has_active_children',
  'has_unsubmitted_draft', 'has_unresolved_send_stop',
  'worktree_missing', 'superseded_by',
]);

const BOOL_FLAGS = new Set([
  'awaiting_alexey', 'waiting_confirmation', 'waiting_input',
  'limit_hit', 'report_written', 'screen_done',
  'authoritative_no_pending_proof', 'pending_obligations',
  'has_active_children', 'has_unsubmitted_draft',
  'has_unresolved_send_stop', 'worktree_missing',
]);

const TEXT_FLAGS = new Set([
  'awaiting_alexey_at', 'waiting_confirmation_at', 'waiting_input_at',
  'limit_window', 'limit_reset_at', 'limit_zone',
  'report_at', 'terminal_evidence', 'terminal_evidence_at', 'superseded_by',
]);

/** Identity keys are silently dropped (never read, never stored): JSON
 * never authenticates ownership. Any other unknown field is rejected so
 * mistyped flags fail closed instead of vanishing. */
const DROPPED_IDENTITY_KEYS = new Set([
  'owner', 'starter', 'owner_provenance', 'human_owned',
  'machine_id', 'native_id', 'engine', 'actor',
]);

function checkObservationPayload(input: any): { ok: true; stored: Record<string, unknown> } | { ok: false; error: string } {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return { ok: false, error: 'invalid_observation' };
  const stored: Record<string, unknown> = {};
  for (const key of Object.keys(input)) {
    if (key === 'observation_note' || key === 'note_text_opt_in') continue;
    if (DROPPED_IDENTITY_KEYS.has(key)) continue;
    if (!OBSERVABLE_FLAGS.has(key)) return { ok: false, error: 'unknown_observation_field' };
  }
  for (const key of BOOL_FLAGS) {
    if (input[key] !== undefined && typeof input[key] !== 'boolean') return { ok: false, error: 'invalid_observation' };
    if (typeof input[key] === 'boolean') stored[key] = input[key];
  }
  for (const key of TEXT_FLAGS) {
    const value = input[key];
    if (value === undefined || value === null) continue;
    if (typeof value !== 'string' || !value || value.length > 512) return { ok: false, error: 'invalid_observation' };
    if (key === 'terminal_evidence' && value !== 'explicit_completion' && value !== 'supersession_record') {
      return { ok: false, error: 'invalid_observation' };
    }
    stored[key] = value;
  }
  if (input.note_text_opt_in === true) {
    if (typeof input.observation_note !== 'string') return { ok: false, error: 'invalid_observation' };
    stored.observation_note = input.observation_note;
    stored.note_text_opt_in = true;
  }
  return { ok: true, stored };
}

/** Build trusted classifier inputs from canonical joins + stored
 * observations. Owner/provenance/human/engine/task linkage always come
 * from canonical rows; observations supply host-verified flags only. */
export function buildSessionInputs(sql: SqlStorage, sessionIds?: Set<string>): { inputs: SessionInput[]; refs: Map<string, LifecycleRefs> } {
  const bases = joinedBases(sql, sessionIds);
  const observations = observationsByKey(sql, sessionIds ? bases.map(base=>base.key) : undefined);
  const taskLinks = taskLinkIndex(sql);
  const protectedSessions = new Set<string>();
  let protectionKnown = true;
  try {
    for (const row of sql.exec('SELECT session_id FROM session_control_protections').toArray() as any[]) protectedSessions.add(row.session_id);
  } catch { protectionKnown = false; }
  const inputs: SessionInput[] = [];
  const refs = new Map<string, LifecycleRefs>();
  // Creator provenance is immutable during this synchronous read. Many
  // sessions share one creator; never repeat its lookup for every session.
  // Keep this cache local so credential changes are visible on the next build.
  const creators = new Map<string, any>();
  for (const base of bases) {
    const request = base.sc?.request_json ? parsed(base.sc.request_json, {}) : {};
    const starter: string | null =
      (typeof base.launch?.actor === 'string' && base.launch.actor) ||
      (typeof base.sc?.actor === 'string' && base.sc.actor) ||
      null;
    const requesterKind: string | null = base.launch?.requester_kind ?? null;
    const human_owned = isHumanStarter(starter, requesterKind);
    // Human creation alone does not make a worker a human workload. Only
    // positively corroborated, Hub-managed headless work may cross this
    // boundary; interactive, observed, contradictory and legacy rows stay
    // protected. Neither observations nor caller-supplied labels can clear it.
    const binding = parsed(base.sc?.binding_json, null);
    let creator: any = null;
    if (base.sc?.owner_token_id) {
      const id = base.sc.owner_token_id;
      if (!creators.has(id)) {
        try { creator = sql.exec('SELECT kind, actor, subject, created_by_sub, family_id FROM credentials WHERE id = ?', id).toArray()[0]; } catch {}
        creators.set(id, creator);
      } else creator = creators.get(id);
    }
    const humanCreatorKnown = creator?.kind === 'human' && creator.actor === base.sc?.actor &&
      creator.actor?.startsWith('human:') && creator.created_by_sub === creator.actor.slice(6) &&
      typeof creator.subject === 'string' && !!creator.subject && typeof creator.family_id === 'string' && !!creator.family_id &&
      (!base.sc?.owner_family || base.sc.owner_family === creator.family_id);
    const humanHeadlessWorker = !!base.sc && !!base.launch &&
      humanCreatorKnown &&
      base.sc.mode === 'headless' && base.sc.session_type === 'headless' &&
      request.provenance !== 'observed_native' && base.launch.mode === 'headless' &&
      base.launch.requester_kind === 'human' && base.launch.actor === base.sc.actor &&
      starter === base.sc.actor && base.sc.fleet_launch_id === base.launch.id &&
      base.launch.machine_id === base.sc.machine_id && base.launch.native_id === base.native_id &&
      binding?.native_id === base.native_id && typeof binding.workspace === 'string' && !!binding.workspace &&
      Number.isFinite(binding.native_created_at_ms) && binding.native_created_at_ms > 0 &&
      (!base.fleet?.workspace || base.fleet.workspace === binding.workspace) &&
      (!base.fleet?.tag || base.fleet.tag === binding.tag);
    const protected_session = !protectionKnown || protectedSessions.has(base.sc?.id) ||
      (human_owned && !humanHeadlessWorker);
    const stored = base.key ? parsed(observations.get(base.key)?.payload_json, {}) ?? {} : {};
    const obs: Record<string, unknown> = {};
    for (const key of [...BOOL_FLAGS, ...TEXT_FLAGS]) {
      if (stored[key] !== undefined) obs[key] = stored[key];
    }
    if (stored.note_text_opt_in === true && typeof stored.observation_note === 'string') {
      obs.observation_note = stored.observation_note;
      obs.note_text_opt_in = true;
    }
    const links = taskLinks(base.native_id, base.sc?.id ?? null);
    const input: SessionInput = {
      machine_id: base.machine_id,
      native_id: base.native_id,
      engine: base.fleet?.engine ?? base.launch?.engine ?? base.sc?.engine ?? null,
      workspace: base.fleet?.workspace ?? base.launch?.workspace ?? request.workspace ?? null,
      starter,
      owner: starter,
      owner_provenance: starter ? 'starter' : null,
      human_owned,
      task_refs: links.task_refs,
      last_activity_at: base.fleet?.last_activity_at ?? null,
      observed_at: base.fleet?.observed_at ?? base.sc?.updated_at ?? null,
      phase: base.fleet?.phase ?? null,
      agent_state: base.fleet?.agent_state ?? null,
      ...obs,
    } as SessionInput;
    inputs.push(input);
    refs.set(base.key, {
      session_id: base.sc?.id ?? null,
      fleet_launch_id: base.launch?.id ?? base.sc?.fleet_launch_id ?? null,
      machine_id: base.machine_id,
      native_id: base.native_id,
      engine: input.engine ?? null,
      workspace: input.workspace ?? null,
      cwd: base.fleet?.cwd ?? (typeof request.cwd === 'string' ? request.cwd : null),
      hub_state: base.sc?.state ?? null,
      fleet_state: base.launch?.state ?? (base.fleet ? `observed:${base.fleet.phase}` : null),
      starter,
      owner: starter,
      human_owned,
      protected_session,
      task_refs: links.task_refs,
      report_refs: links.report_refs,
      has_brief: (Object.prototype.hasOwnProperty.call(request, 'brief')
        ? typeof request.brief === 'string' : base.sc?.has_brief === 1) || base.launch?.has_prompt === 1,
    });
  }
  return { inputs, refs };
}

/** Grouped needs-action projection. Decisions ride along: a durable
 * limit-block decision is appended to the reasons of its session so the
 * same classified facts feed status/handoff. */
export function buildLifecycleView(
  sql: SqlStorage,
  scope: LifecycleScope,
  options: { limit?: number; cursor?: string | null; now_ms?: number },
): { groups: Record<LifecycleGroup, Record<string, unknown>[]>; next_cursor: string | null; total: number } {
  const { inputs, refs } = buildSessionInputs(sql);
  const decisions = decisionsByKey(sql);
  const now = options.now_ms ?? Date.now();
  const projection = projectSessions(inputs, scope, { ...options, now_ms: now });
  const groups = {
    waiting_alexey: [],
    limit_blocked: [],
    finished_stale: [],
    active: [],
    unknown: [],
  } as Record<LifecycleGroup, Record<string, unknown>[]>;
  const enrich = (entry: ClassifiedSession): Record<string, unknown> => {
    const dto = toStatusDto(entry) as Record<string, unknown>;
    const ref = refs.get(entry.session_key) ?? null;
    const latest = decisions.get(entry.session_key)?.slice(-1)[0] ?? null;
    if (latest && entry.group === 'limit_blocked') {
      (dto.reasons as string[]).push(`decision:${latest.kind} by ${latest.actor} at ${latest.created_at}: ${latest.reason}`);
    }
    return { ...dto, refs: ref, decision: latest };
  };
  for (const group of Object.keys(projection.groups) as LifecycleGroup[]) {
    groups[group] = projection.groups[group].map(enrich);
  }
  return { groups, next_cursor: projection.next_cursor, total: projection.total };
}

/** Classify one joined key (used to gate decisions on live facts). */
export function classifyKey(sql: SqlStorage, session_key: string, now_ms?: number): (ClassifiedSession & { refs: LifecycleRefs }) | null {
  const { inputs, refs } = buildSessionInputs(sql);
  const input = inputs.find((candidate) => `${candidate.machine_id}/${candidate.native_id}` === session_key);
  if (!input) return null;
  return { ...classifySession(input, now_ms === undefined ? {} : { now_ms }), refs: refs.get(session_key)! };
}

export function recordObservation(
  sql: SqlStorage,
  now_ms: number,
  input: { machine_id: unknown; native_id: unknown; observed_at?: unknown; payload?: unknown },
): { ok: true; session_key: string; observed_at: string } | { ok: false; error: string; status: number } {
  if (!isValidBindingId(input.machine_id) || !isValidBindingId(input.native_id)) {
    return { ok: false, error: 'unbound_target', status: 400 };
  }
  const machine_id = input.machine_id as string;
  const native_id = input.native_id as string;
  let observed_at: string;
  if (input.observed_at === undefined || input.observed_at === null) {
    observed_at = new Date(now_ms).toISOString();
  } else {
    if (typeof input.observed_at !== 'string') return { ok: false, error: 'invalid_observation', status: 400 };
    const parsed_ms = Date.parse(input.observed_at);
    if (!Number.isFinite(parsed_ms)) return { ok: false, error: 'invalid_observation', status: 400 };
    if (parsed_ms > now_ms + 60000) return { ok: false, error: 'future_observation', status: 400 };
    observed_at = input.observed_at;
  }
  const checked = checkObservationPayload(input.payload ?? {});
  if (!checked.ok) return { ok: false, error: checked.error, status: 400 };
  const stored = { ...checked.stored };
  if (stored.note_text_opt_in === true) {
    const note = sanitizeObservationText(stored.observation_note, 512);
    if (note == null) {
      delete stored.observation_note;
      delete stored.note_text_opt_in;
    } else {
      stored.observation_note = note;
    }
  }
  const session_key = `${machine_id}/${native_id}`;
  sql.exec(
    'INSERT INTO session_lifecycle_observations (session_key, machine_id, native_id, observed_at, payload_json) VALUES (?, ?, ?, ?, ?) ' +
    'ON CONFLICT(session_key) DO UPDATE SET machine_id=excluded.machine_id, native_id=excluded.native_id, observed_at=excluded.observed_at, payload_json=excluded.payload_json',
    session_key, machine_id, native_id, observed_at, JSON.stringify(stored),
  );
  return { ok: true, session_key, observed_at };
}

export function recordDecision(
  sql: SqlStorage,
  now_ms: number,
  caller: { actor: string | undefined; isAdmin: boolean },
  ownerVerified: boolean,
  input: { session_key: unknown; kind: unknown; reason: unknown; engine?: unknown; target?: unknown },
): { ok: true; decision: LifecycleDecision } | { ok: false; error: string; status: number } {
  if (typeof input.session_key !== 'string' || !input.session_key || input.session_key.length > 512) {
    return { ok: false, error: 'invalid_decision', status: 400 };
  }
  if (typeof input.kind !== 'string' || !DECISION_KINDS.has(input.kind)) {
    return { ok: false, error: 'invalid_decision', status: 400 };
  }
  if (typeof input.reason !== 'string' || !input.reason.trim() || input.reason.length > 512) {
    return { ok: false, error: 'invalid_decision', status: 400 };
  }
  const kind = input.kind as DecisionKind;
  let engine: string | null = null;
  if (kind === 'resume_on_reset' || kind === 'reassign') {
    if (typeof input.engine !== 'string' || !input.engine.trim() || input.engine.length > 128) {
      return { ok: false, error: 'decision_engine_required', status: 400 };
    }
    engine = input.engine.trim();
  } else if (input.engine !== undefined && input.engine !== null) {
    return { ok: false, error: 'invalid_decision', status: 400 };
  }
  let target: string | null = null;
  if (input.target !== undefined && input.target !== null) {
    if (typeof input.target !== 'string' || !input.target.trim() || input.target.length > 512) {
      return { ok: false, error: 'invalid_decision', status: 400 };
    }
    target = input.target.trim();
  }
  if (kind === 'reassign' && !target) return { ok: false, error: 'decision_target_required', status: 400 };
  const live = classifyKey(sql, input.session_key, now_ms);
  if (!live) return { ok: false, error: 'not_found', status: 404 };
  if (live.refs.human_owned) return { ok: false, error: 'human_protected', status: 409 };
  if (live.group !== 'limit_blocked') return { ok: false, error: 'not_limit_blocked', status: 409 };
  // Ownership is authentication, never a payload flag: the service verifies
  // the live owner before calling here, and admins are never another
  // coordinator's owner.
  if (caller.isAdmin === true) return { ok: false, error: 'not_found', status: 404 };
  if (!caller.actor || live.refs.owner !== caller.actor || !ownerVerified) {
    return { ok: false, error: 'not_found', status: 404 };
  }
  const decision: LifecycleDecision = {
    session_key: input.session_key,
    decision_id: crypto.randomUUID(),
    kind,
    actor: caller.actor,
    reason: (input.reason as string).trim(),
    engine,
    target,
    created_at: new Date(now_ms).toISOString(),
  };
  sql.exec(
    'INSERT INTO session_lifecycle_decisions (session_key, decision_id, kind, actor, reason, engine, target, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
    decision.session_key, decision.decision_id, decision.kind, decision.actor,
    decision.reason, decision.engine, decision.target, decision.created_at,
  );
  return { ok: true, decision };
}

export type CleanupDryRun = {
  dry_run: true;
  apply: false;
  eligible: { session_key: string; state: string; owner: string | null; refs: LifecycleRefs }[];
  ineligible: { key: string; reasons: string[] }[];
  total: number;
};

/** Fresh eligible-target collection shared by dry-run and apply.
 *
 * Pure join + classification + planning over canonical stores, zero writes.
 * The apply path re-runs this INSIDE the mutation transaction (fresh
 * per-session re-evaluation) and then enqueues M12 stop intents through the
 * existing session-control custody — never a second classifier or store
 * here. Ownership in the returned candidates is TRUSTED canonical-server
 * context only; the service still verifies the live credential/family/
 * incarnation before any enqueue, and the host revalidates before any
 * native effect. */
export type CleanupApplyEligible = {
  session_key: string;
  state: string;
  owner: string | null;
  refs: LifecycleRefs;
};

export function collectCleanupEligible(
  sql: SqlStorage,
  scope: LifecycleScope,
  callerOwner: string,
  now_ms?: number,
): { eligible: CleanupApplyEligible[]; ineligible: { key: string; reasons: string[] }[]; total: number } {
  const { inputs, refs } = buildSessionInputs(sql);
  const now = now_ms ?? Date.now();
  const scoped = inputs.filter((candidate) => {
    if (scope.host && candidate.machine_id !== scope.host) return false;
    if (scope.owner && (candidate.owner ?? null) !== scope.owner) return false;
    if (scope.task && !(candidate.task_refs ?? []).includes(scope.task)) return false;
    return true;
  });
  const candidates = scoped.map((candidate) => {
    const classified = classifySession(candidate, { now_ms: now });
    return {
      ...classified,
      owner: candidate.owner ?? null,
      human_owned: Boolean(candidate.human_owned),
      protected_session: refs.get(classified.session_key)?.protected_session ?? true,
      pending_obligations: classified.obligations.length > 0,
    };
  });
  const plan = planCleanup(candidates, callerOwner);
  return {
    eligible: plan.eligible.map((entry) => ({
      session_key: entry.session_key,
      state: entry.state,
      owner: entry.owner,
      refs: refs.get(entry.session_key)!,
    })),
    ineligible: plan.ineligible,
    total: scoped.length,
  };
}

/** Readonly dry-run: pure classification + planning, zero writes. */
export function planCleanupDryRun(
  sql: SqlStorage,
  scope: LifecycleScope,
  callerOwner: string,
  now_ms?: number,
): CleanupDryRun {
  const collected = collectCleanupEligible(sql, scope, callerOwner, now_ms);
  return {
    dry_run: true,
    apply: false,
    eligible: collected.eligible,
    ineligible: collected.ineligible,
    total: collected.total,
  };
}

/** M15c refusal seam (retired in M15d: apply now enqueues real M12 stop
 * intents; kept exported so parallel workers importing it keep compiling). */
export function refuseCleanupApply(): { error: string; status: number; detail: string; next: string } {
  return {
    error: 'apply_not_available',
    status: 409,
    detail: 'Cleanup --apply refused: no safe execution path in M15c (dry-run planning only). Nothing was stopped, removed, or mutated.',
    next: APPLY_REFUSAL_NEXT_SEAM,
  };
}
