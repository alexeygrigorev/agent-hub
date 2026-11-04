import { RowBudgetExceeded } from './row-budget';
import { jsonFields } from './sql-json-projection';
import { backfillOnce } from './sql-probes';
import { intakeHasDoneEvidence } from './intake';
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
  task_linkage_observation_pending?: boolean;
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
  backfillOnce(sql, 'lifecycle_request_projection', () => sql.exec(`INSERT INTO lifecycle_request_projection SELECT c.id,
    ${jsonFields('c.request_json',['provenance','workspace','cwd'])},${brief('c.request_json')}
    FROM session_control_sessions c WHERE NOT EXISTS(SELECT 1 FROM lifecycle_request_projection p WHERE p.id=c.id)`), false, ['session_control_sessions']);
}

/** Canonical identity union. Only sessions with a native identity
 * (bound hub sessions, observed fleet sessions) are keyed; unbound
 * queued/held intents stay in the existing flat list view only. */
export class LifecycleObservationLimit extends Error {
  readonly code='lifecycle_observation_limit';
  readonly status=503;
  constructor(){super('lifecycle_observation_limit');}
}

export type LifecycleNativeIdentity={machine_id:string;native_id:string};

function joinedBases(sql: SqlStorage, sessionIds?: Set<string>, providedIdentities?:readonly LifecycleNativeIdentity[]): JoinedBase[] {
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
  const targeted=providedIdentities!==undefined;
  const selected=sessionIds?[...sessionIds]:targeted?[]:null;
  if(targeted) {
    if(providedIdentities.length>64)throw new LifecycleObservationLimit();
    const composite=new Map<string,string>();
    for(const pair of providedIdentities) {
      if(typeof pair?.machine_id!=='string'||!pair.machine_id||typeof pair.native_id!=='string'||!pair.native_id
        ||pair.machine_id.length+pair.native_id.length+1>512)throw new LifecycleObservationLimit();
      const key=pair.machine_id+'/'+pair.native_id,exact=JSON.stringify([pair.machine_id,pair.native_id]);
      if(composite.has(key)&&composite.get(key)!==exact)throw new LifecycleObservationLimit();
      composite.set(key,exact);
    }
  }
  let projected = false;
  try { projected = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_request_projection'").toArray().length > 0; } catch(error) {if(error instanceof RowBudgetExceeded)throw error;}
  const columns = (table: string) => {
    if (table === 'session_control_sessions') return `c.id,c.actor,c.machine_id,c.fleet_launch_id,c.binding_json,c.mode,c.session_type,
      c.owner_token_id,c.owner_family,c.state,c.updated_at,c.engine,
      ${projected ? `COALESCE(lp.request_json,${jsonFields('c.request_json',['provenance','workspace','cwd'])})` : jsonFields('c.request_json',['provenance','workspace','cwd'])} AS request_json,
      ${projected ? `CASE WHEN lp.id IS NOT NULL THEN lp.has_brief ELSE json_type(CASE WHEN json_valid(c.request_json) THEN c.request_json ELSE '{}' END,'$.brief')='text' END` : `json_type(CASE WHEN json_valid(c.request_json) THEN c.request_json ELSE '{}' END,'$.brief')='text'`} AS has_brief`;
    if (table === 'fleet_launches') return `c.id,c.machine_id,c.native_id,c.actor,c.requester_kind,c.mode,c.engine,c.workspace,c.state,
      typeof(c.prompt)='text' AS has_prompt`;
    return 'c.machine_id,c.native_id,c.engine,c.workspace,c.cwd,c.tag,c.last_activity_at,c.observed_at,c.phase,c.agent_state';
  };
  if(selected && selected.length>512)throw new LifecycleObservationLimit();
  const aliasLimit=1024;
  const joinProjection=(table:string)=>table==='session_control_sessions' && projected
    ? ' LEFT JOIN lifecycle_request_projection lp ON lp.id=c.id' : '';
  let selectedControls:any[]|undefined;
  const controls=():any[]=>{
    if(selectedControls)return selectedControls;
    if(!selected?.length)return selectedControls=[];
    // Drive from the caller's bounded IDs, never from the retained table.
    return selectedControls=sql.exec(`SELECT c.rowid AS _join_order,${columns('session_control_sessions')}
      FROM json_each(?) ids CROSS JOIN session_control_sessions c ON c.id=ids.value
      ${joinProjection('session_control_sessions')}`,JSON.stringify(selected)).toArray() as any[];
  };
  let nativeIdentities:string|undefined;
  const identities=():string=>{
    if(nativeIdentities!==undefined)return nativeIdentities;
    const unique=new Map<string,[string,string]>();
    for(const row of controls()) {
      const binding=parsed(row.binding_json,null);
      if(typeof row.machine_id!=='string' || typeof binding?.native_id!=='string')continue;
      const pair:[string,string]=[row.machine_id,binding.native_id];
      unique.set(JSON.stringify(pair),pair);
    }
    for(const pair of providedIdentities??[]) {
      const tuple:[string,string]=[pair.machine_id,pair.native_id];
      unique.set(JSON.stringify(tuple),tuple);
    }
    return nativeIdentities=JSON.stringify([...unique.values()]);
  };
  const read = (table: string): any[] => {
    const projection=columns(table);
    if(!selected) {
      const raw=sql.exec(`SELECT rowid AS matched_rowid FROM ${table} ORDER BY rowid LIMIT 1025`).toArray() as any[];
      if(raw.length>aliasLimit)throw new LifecycleObservationLimit();
      if(!raw.length)return [];
      return (sql.exec(`SELECT c.rowid AS _join_order,${projection} FROM json_each(?) ids
        CROSS JOIN ${table} c ON c.rowid=ids.value ${joinProjection(table)}`,
        JSON.stringify(raw.map(row=>row.matched_rowid))).toArray() as any[]).sort((a,b)=>a._join_order-b._join_order);
    }
    const direct=controls();
    const rows=new Map<number,any>();
    const ids=new Set<number>();
    if(table==='session_control_sessions')for(const row of direct){rows.set(row._join_order,row);ids.add(row._join_order);}
    if(table==='fleet_launches') {
      const launches=[...new Set((targeted?scRows:direct).map(row=>row.fleet_launch_id).filter(id=>typeof id==='string'))];
      if(launches.length)for(const row of sql.exec(`SELECT c.rowid AS matched_rowid FROM json_each(?) ids
        CROSS JOIN fleet_launches c ON c.id=ids.value`,JSON.stringify(launches)).toArray() as any[])ids.add(row.matched_rowid);
    }
    const nativeId=`CASE WHEN json_valid(c.binding_json) THEN CASE WHEN json_type(c.binding_json,'$.native_id')='text' THEN json_extract(c.binding_json,'$.native_id') END END`;
    const hint=table==='session_control_sessions'?' INDEXED BY session_control_native'
      :table==='fleet_launches'?' INDEXED BY fleet_launch_native_state':'';
    const target=table==='session_control_sessions'?nativeId:'c.native_id';
    // Materialize only identity keys. The exact deployed index prefixes seek
    // aliases, and the GLOBAL raw sentinel precedes any request JSON hydration.
    const aliases:any[]=[];
    if(targeted) {
      for(const [machine,native] of JSON.parse(identities()) as [string,string][]) {
        const raw=sql.exec(`SELECT c.rowid AS matched_rowid FROM ${table} c${hint}
          WHERE c.machine_id=? AND ${target}=? LIMIT 129`,machine,native).toArray() as any[];
        if(raw.length>128)throw new LifecycleObservationLimit();
        aliases.push(...raw);
        if(aliases.length>aliasLimit)throw new LifecycleObservationLimit();
      }
    } else aliases.push(...sql.exec(`WITH identities AS MATERIALIZED (
      SELECT json_extract(value,'$[0]') AS machine_id,json_extract(value,'$[1]') AS native_id FROM json_each(?))
      SELECT c.rowid AS matched_rowid FROM identities k CROSS JOIN ${table} c${hint}
      ON c.machine_id=k.machine_id AND ${target}=+k.native_id LIMIT 1025`,identities()).toArray() as any[]);
    if(aliases.length>aliasLimit)throw new LifecycleObservationLimit();
    for(const row of aliases)ids.add(row.matched_rowid);
    if(ids.size>(targeted?128:aliasLimit))throw new LifecycleObservationLimit();
    const missing=[...ids].filter(id=>!rows.has(id));
    if(missing.length)for(const row of sql.exec(`SELECT c.rowid AS _join_order,${projection}
      FROM json_each(?) ids CROSS JOIN ${table} c ON c.rowid=ids.value ${joinProjection(table)}`,
      JSON.stringify(missing)).toArray() as any[])rows.set(row._join_order,row);
    return [...rows.values()].sort((a,b)=>a._join_order-b._join_order);
  };
  let scRows: any[] = [];
  try {
    scRows = read('session_control_sessions');
  } catch(error) {
    if(error instanceof LifecycleObservationLimit || error instanceof RowBudgetExceeded)throw error;
    scRows = [];
  }
  const launchesById = new Map<string, any>();
  try {
    for (const launch of read('fleet_launches')) {
      launchesById.set(launch.id, launch);
    }
  } catch(error) { if(error instanceof LifecycleObservationLimit || error instanceof RowBudgetExceeded)throw error; /* fleet tables predate; join degrades to session-control only */ }
  for (const sc of scRows) {
    const binding = sc.binding_json ? parsed(sc.binding_json) : null;
    if (!sc.machine_id || !binding?.native_id) continue;
    const base = get(sc.machine_id, binding.native_id);
    if (!base.sc) base.sc = sc;
  }
  let fleetRows: any[] = [];
  try {
    fleetRows = read('fleet_sessions');
  } catch(error) {
    if(error instanceof LifecycleObservationLimit || error instanceof RowBudgetExceeded)throw error;
    fleetRows = [];
  }
  for (const fleet of fleetRows) {
    if (!fleet.machine_id || !fleet.native_id) continue;
    if (sessionIds && !targeted && !byKey.has(`${fleet.machine_id}/${fleet.native_id}`)) continue;
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

type TaskLinks = { task_refs: string[]; report_refs: string[]; observation_pending?:boolean };

/** Canonical intake links only: link events WITHOUT a routing_job_id are
 * external/unverified and never join — EXCEPT links carrying a
 * `hub_session_id` that exactly matches the joined canonical session row:
 * intake authorizes that reference at link time (operator or session owner),
 * so it is already an authenticated join endpoint. Report references ride
 * the linked tasks. Bounded; failures degrade to empty (never a hard error). */
/** Parse each history once per projection, not once per session. The maps
 * contain canonical link candidates; union in intake order preserves the
 * existing 25-task / 5-report bounds when a task links both identities. */
function taskLinkIndex(sql: SqlStorage, selected:Set<string>): (native: string, hub: string | null) => TaskLinks {
  type LinkedTask = { order: number; id: string; reports: string[] };
  const natives = new Map<string, LinkedTask[]>();
  const hubs = new Map<string, LinkedTask[]>();
  const unknown=()=>({task_refs:[],report_refs:[],observation_pending:true});
  if(!selected.size)return ()=>({task_refs:[],report_refs:[]});
  if(selected.size>2048)return unknown;
  try {
    // Normalized refs are initialized atomically by IntakeService and kept
    // insert-only by triggers. Global health proves malformed/unfinished
    // legacy input cannot hide a link behind an empty candidate result.
    const stores=sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('intake_projection_link_refs','intake_link_health','intake_link_health_meta')").toArray();
    if(stores.length!==3)return unknown;
    if((sql.exec('SELECT complete FROM intake_link_health_meta WHERE id=1').toArray()[0] as any)?.complete!==1
      ||sql.exec('SELECT id FROM intake_link_health ORDER BY id LIMIT 1').toArray().length)return unknown;
    const refs=sql.exec('SELECT ref,task_id FROM intake_projection_link_refs WHERE ref IN (SELECT value FROM json_each(?)) LIMIT 129',JSON.stringify([...selected])).toArray() as any[];
    if(refs.length>128)return unknown;
    const ids=[...new Set(refs.map(r=>String(r.task_id)))];
    const rows=ids.length?sql.exec('SELECT id,links FROM intake_task_projection WHERE id IN (SELECT value FROM json_each(?)) ORDER BY rowid',JSON.stringify(ids)).toArray() as any[]:[];
    for (const [order, row] of rows.entries()) {
      const task = { id: row.id, history: parsed(row.links, []) };
      if (!Array.isArray(task.history)||task.history.length>128||new TextEncoder().encode(String(row.links)).byteLength>2097152)return unknown;
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
  } catch { return unknown; /* never interpret unreadable linkage as absence */ }
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

function observationsByKey(sql:SqlStorage,keys:string[]):Map<string,any> {
  const map=new Map<string,any>();
  try {
    for(let i=0;i<keys.length;i+=512)for(const row of sql.exec(
      'SELECT * FROM session_lifecycle_observations WHERE session_key IN (SELECT value FROM json_each(?))',
      JSON.stringify(keys.slice(i,i+512))).toArray() as any[])map.set(row.session_key,row);
  } catch(error) {if(error instanceof RowBudgetExceeded)throw error; /* absent additive schema: unknown */}
  return map;
}

function decisionsByKey(sql:SqlStorage,keys:string[]):Map<string,LifecycleDecision[]> {
  const map=new Map<string,LifecycleDecision[]>();
  try {
    for(let i=0;i<keys.length;i+=512)for(const row of sql.exec(`SELECT d.* FROM json_each(?) ids
      CROSS JOIN session_lifecycle_decisions d ON d.decision_id=(
        SELECT decision_id FROM session_lifecycle_decisions INDEXED BY lifecycle_decision_latest
        WHERE session_key=ids.value ORDER BY created_at DESC,decision_id DESC LIMIT 1)`,
      JSON.stringify(keys.slice(i,i+512))).toArray() as any[]) {
      map.set(row.session_key,[{session_key:row.session_key,decision_id:row.decision_id,kind:row.kind,
        actor:row.actor,reason:row.reason,engine:row.engine??null,target:row.target??null,created_at:row.created_at}]);
    }
  } catch(error) {if(error instanceof RowBudgetExceeded)throw error; /* absent additive schema: unknown */}
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
  'worktree_missing', 'superseded_by', 'cwd_path_recreated',
]);

const BOOL_FLAGS = new Set([
  'awaiting_alexey', 'waiting_confirmation', 'waiting_input',
  'limit_hit', 'report_written', 'screen_done',
  'authoritative_no_pending_proof', 'pending_obligations',
  'has_active_children', 'has_unsubmitted_draft',
  'has_unresolved_send_stop', 'worktree_missing', 'cwd_path_recreated',
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
export function buildSessionInputs(sql: SqlStorage, sessionIds?: Set<string>, options: { taskLinks?: boolean;nativeIdentities?:readonly LifecycleNativeIdentity[] } = {}): { inputs: SessionInput[]; refs: Map<string, LifecycleRefs> } {
  const bases = joinedBases(sql, sessionIds,options.nativeIdentities);
  const observations = observationsByKey(sql,bases.map(base=>base.key));
  // Callers that read only protection/ownership refs (worktree-guards) skip the
  // selected intake links; task_refs/report_refs are then deliberately skipped.
  const taskLinks:(native:string,hub:string|null)=>TaskLinks = options.taskLinks === false ? () => ({ task_refs: [], report_refs: [] })
    :taskLinkIndex(sql,new Set(bases.flatMap(base=>[base.native_id,base.sc?.id].filter((id):id is string=>typeof id==='string'))));
  const protectedSessions=new Set<string>();
  let protectionKnown=true;
  try {
    const all=[...new Set([...(sessionIds??[]),...bases.map(base=>base.sc?.id).filter((id):id is string=>typeof id==='string')])];
    for(let i=0;i<all.length;i+=512)for(const row of sql.exec(
      'SELECT session_id FROM session_control_protections WHERE session_id IN (SELECT value FROM json_each(?))',
      JSON.stringify(all.slice(i,i+512))).toArray() as any[])protectedSessions.add(row.session_id);
  } catch(error) {if(error instanceof RowBudgetExceeded)throw error;protectionKnown=false;}
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
      protected_session:protected_session||links.observation_pending===true,
      task_linkage_observation_pending:links.observation_pending===true,
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
  const now = options.now_ms ?? Date.now();
  const projection = projectSessions(inputs, scope, { ...options, now_ms: now });
  const visibleEntries=Object.values(projection.groups).flat();
  const decisions=decisionsByKey(sql,visibleEntries.map(entry=>entry.session_key));
  const groups = {
    waiting_alexey: [],
    limit_blocked: [],
    finished_stale: [],
    active: [],
    unknown: [],
  } as Record<LifecycleGroup, Record<string, unknown>[]>;
  const wakes=new Map<string,string>();
  try {
    const visible=new Set(visibleEntries.map(entry=>refs.get(entry.session_key)?.session_id)
      .filter((id):id is string=>typeof id==='string'));
    for(const id of visible) {
      const wake=sql.exec(`SELECT wake_at_ms FROM session_control_wakes INDEXED BY intake_queue_wakes_session
        WHERE session_id=? AND state='pending' AND wake_at_ms>? ORDER BY wake_at_ms LIMIT 1`,id,now).toArray()[0] as any;
      if(wake)wakes.set(id,new Date(wake.wake_at_ms).toISOString());
    }
  } catch(error) {if(error instanceof RowBudgetExceeded)throw error; /* absent optional wake schema */}
  const enrich = (entry: ClassifiedSession): Record<string, unknown> => {
    const dto = toStatusDto(entry) as Record<string, unknown>;
    const ref = refs.get(entry.session_key) ?? null;
    const latest = decisions.get(entry.session_key)?.slice(-1)[0] ?? null;
    if (latest && entry.group === 'limit_blocked') {
      (dto.reasons as string[]).push(`decision:${latest.kind} by ${latest.actor} at ${latest.created_at}: ${latest.reason}`);
    }
    const waitingUntil = ref?.hub_state === 'running' && ref.session_id ? wakes.get(ref.session_id) : null;
    return { ...dto, ...(waitingUntil ? { state: 'waiting_until', waiting_until: waitingUntil } : {}), refs: ref, decision: latest };
  };
  for (const group of Object.keys(projection.groups) as LifecycleGroup[]) {
    groups[group] = projection.groups[group].map(enrich);
  }
  return { groups, next_cursor: projection.next_cursor, total: projection.total };
}

/** Classify one joined key (used to gate decisions on live facts). */
export function classifyKey(sql:SqlStorage,session_key:string,now_ms?:number,trustedIdentity?:LifecycleNativeIdentity):
  (ClassifiedSession & {refs:LifecycleRefs})|null {
  let identity=trustedIdentity;
  if(identity) {
    if(session_key!==identity.machine_id+'/'+identity.native_id)return null;
  } else {
    if(typeof session_key!=='string'||session_key.length>512)return null;
    const candidates:LifecycleNativeIdentity[]=[];
    for(let at=session_key.indexOf('/');at>=0;at=session_key.indexOf('/',at+1)) {
      if(at>0&&at<session_key.length-1)candidates.push({machine_id:session_key.slice(0,at),native_id:session_key.slice(at+1)});
      if(candidates.length>64)throw new LifecycleObservationLimit();
    }
    for(const candidate of candidates) {
      // Only controls or observed natives establish a joined base. A launch
      // alone must not invent a native or establish cleanup authority.
      const present=sql.exec('SELECT 1 FROM fleet_sessions WHERE machine_id=? AND native_id=? LIMIT 1',
        candidate.machine_id,candidate.native_id).toArray().length || sql.exec(`SELECT 1 FROM session_control_sessions
        INDEXED BY session_control_native WHERE machine_id=? AND CASE WHEN json_valid(binding_json)
          THEN CASE WHEN json_type(binding_json,'$.native_id')='text' THEN json_extract(binding_json,'$.native_id') END END=? LIMIT 1`,
        candidate.machine_id,candidate.native_id).toArray().length;
      if(!present)continue;
      if(identity)throw new LifecycleObservationLimit();
      identity=candidate;
    }
    if(!identity)return null;
  }
  const {inputs,refs}=buildSessionInputs(sql,undefined,{nativeIdentities:[identity]});
  const input=inputs.find(candidate=>candidate.machine_id===identity.machine_id&&candidate.native_id===identity.native_id);
  if(!input)return null;
  return {...classifySession(input,now_ms===undefined?{}:{now_ms}),refs:refs.get(session_key)!};
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
  // Worktree-guard (AC2): finished sessions linked to an OPEN intake stay
  // ineligible even when every other cleanup condition holds. The lock is
  // released only after a DONE note / archive; never while work is open.
  const eligible: CleanupApplyEligible[] = [];
  const ineligible = [...plan.ineligible];
  for (const entry of plan.eligible) {
    const openId = linkedOpenIntake(sql, refs.get(entry.session_key)?.task_refs ?? []);
    if (openId) {
      ineligible.push({ key: entry.session_key, reasons: [`linked open intake ${openId} (worktree guard: keep while work is open)`] });
      continue;
    }
    eligible.push({
      session_key: entry.session_key,
      state: entry.state,
      owner: entry.owner,
      refs: refs.get(entry.session_key)!,
    });
  }
  return { eligible, ineligible, total: scoped.length };
}

/** Worktree-guard (intake a0f01448 AC2): a session linked to an open intake
 * is never cleanup-eligible, even when finished. An intake is open unless
 * its ledger row is archived or carries a qualifying
 * DONE note (live proof). Unknown rows and parse failures fail closed
 * (treated as open) so a missing ledger never authorizes a removal. */
export function isIntakeTaskOpen(sql: SqlStorage, taskId: unknown): boolean {
  try {
    if (typeof taskId !== 'string' || !taskId) return true;
    const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0];
    if (!row) return true;
    const task = typeof row.data === 'string' ? JSON.parse(row.data) : row.data;
    if (!task || typeof task !== 'object') return true;
    if (task.archived_at != null) return false;
    if (intakeHasDoneEvidence(task)) return false;
    return true;
  } catch {
    return true;
  }
}

/** First linked open intake id, or null when none is open. */
export function linkedOpenIntake(sql: SqlStorage, taskRefs: unknown): string | null {
  if (!Array.isArray(taskRefs)) return null;
  for (const id of taskRefs) {
    if (typeof id === 'string' && id && isIntakeTaskOpen(sql, id)) return id;
  }
  return null;
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
