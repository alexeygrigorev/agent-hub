/** Host-issued, refreshable worker credentials restricted to one linked intake.
 * Uses the normal credential/registry lifecycle; no host or human bearer is
 * copied into the worker and no new signing secret is needed.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { CallerContext } from './bus';
import { generateTokenPair } from './auth/token';
import { DRIVER_ACCESS_SCHEMA, driverLaunchRegistered, issueDriverAccess } from './driver-access';
import { formatNativeActor } from './work_claim';

export const WORKER_SCOPE = 'worker:own-intake';
export const WORKER_ACCESS_SCHEMA = `CREATE TABLE IF NOT EXISTS worker_access (
  family_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
  task_id TEXT NOT NULL, workspace TEXT NOT NULL, expires_at INTEGER NOT NULL)`;
/** Follow-up intakes a worker session filed (intake 34d0271b). The creating
 * session may read them and append note/report/blocker; nothing else. */
export const WORKER_CREATED_SCHEMA = `CREATE TABLE IF NOT EXISTS worker_created_intakes (
  task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, launch_id TEXT NOT NULL,
  parent_task_id TEXT NOT NULL, created_at INTEGER NOT NULL)`;
export const WORKER_CREATED_INDEX = 'CREATE INDEX IF NOT EXISTS worker_created_intakes_session ON worker_created_intakes(session_id, task_id)';
/** Per-session cap on follow-up intakes; also bounds the per-request lookup. */
export const WORKER_CREATE_LIMIT = 25;
/** Server-verified session links (operator verify_live or Hub launch), indexed
 * by Hub session id or native id. Maintained by intake_task_projection triggers. */
export const SESSION_LINKS_SCHEMA = `CREATE TABLE IF NOT EXISTS intake_session_links (
  session_key TEXT NOT NULL, task_id TEXT NOT NULL, authority TEXT NOT NULL, PRIMARY KEY(session_key, task_id))`;
const TRUSTED_LINK = `e.type='object' AND json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified')`;
const linkRows = (id: string, from: string, filter = '1') => ['hub_session_id', 'native_id'].map(field =>
  `SELECT json_extract(e.value,'$.${field}'), ${id}, json_extract(e.value,'$.authority') FROM ${from}
   WHERE ${filter} AND ${TRUSTED_LINK} AND json_type(e.value,'$.${field}')='text'`).join(' UNION ');
/** Create the index, backfill it once, and keep it current on every task write.
 * The backfill reads the small link/report projection of open intakes only. */
export function initSessionLinks(sql: SqlStorage) {
  const exists = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_session_links'").toArray().length > 0;
  sql.exec(SESSION_LINKS_SCHEMA);
  // Upsert, not INSERT OR IGNORE: the firing statement's conflict policy would override OR IGNORE.
  if (!exists) sql.exec(`INSERT INTO intake_session_links(session_key,task_id,authority)
    ${linkRows('p.id', 'intake_task_projection p, json_each(p.links) e', "json_extract(p.data,'$.status') NOT IN ('done','cancelled')")}
    ON CONFLICT DO NOTHING`);
  // Driven by the projection's small link/report column, and only when it changes,
  // so ordinary note writes do not rescan the whole history. A malformed links
  // value is skipped, never thrown: readers already fail closed on it. The first release
  // put full-history triggers on intake_tasks under the old names; drop them.
  for (const op of ['insert', 'update']) sql.exec(`DROP TRIGGER IF EXISTS intake_session_links_${op}`);
  for (const [op, when] of [['INSERT', 'WHEN json_valid(NEW.links)'], ['UPDATE OF links', 'WHEN NEW.links IS NOT OLD.links AND json_valid(NEW.links)']]) sql.exec(
    `CREATE TRIGGER IF NOT EXISTS intake_session_links_projection_${op.split(' ')[0].toLowerCase()} AFTER ${op} ON intake_task_projection ${when} BEGIN
    INSERT INTO intake_session_links(session_key,task_id,authority) ${linkRows('NEW.id', 'json_each(NEW.links) e')}
    ON CONFLICT DO NOTHING; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS intake_session_links_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_session_links WHERE task_id=OLD.id; END');
}

/** Indexed (ref -> task) side table of every link event's hub_session_id and
 * native_id, maintained by an intake_tasks trigger; stale refs only widen the
 * candidate set because the exact link events are re-read from the projection. */
export function initLinkRefs(sql: SqlStorage) {
  const exists = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_link_refs'").toArray().length > 0;
  sql.exec('CREATE TABLE IF NOT EXISTS intake_link_refs (ref TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(ref, task_id)) WITHOUT ROWID');
  const rows = (id: string, from: string) => ['hub_session_id', 'native_id'].map(field =>
    `SELECT json_extract(e.value,'$.${field}'), ${id} FROM ${from} WHERE e.type='object' AND json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.${field}')='text'`).join(' UNION ');
  if (!exists) sql.exec(`INSERT INTO intake_link_refs(ref,task_id) ${rows('p.id', 'intake_task_projection p, json_each(p.links) e')} ON CONFLICT DO NOTHING`);
  for (const op of ['INSERT', 'UPDATE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_link_refs_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN
    INSERT INTO intake_link_refs(ref,task_id) ${rows('NEW.id', "json_each(NEW.data,'$.history') e")} ON CONFLICT DO NOTHING; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS intake_link_refs_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_link_refs WHERE task_id=OLD.id; END');
  initProjectionLinkRefs(sql);
}

/** Indexed (ref -> task) side table over every entry of the projection's
 * link/report column (any kind), keyed by hub_session_id and native_id text
 * values (intake 8081a05a). Session-to-intake lookups read candidate tasks by
 * ref instead of running json_each over the whole projection per session.
 * Insert-only like intake_link_refs: stale refs only widen the candidate set,
 * and callers re-check the exact link entries. */
export function initProjectionLinkRefs(sql: SqlStorage) {
  const exists = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_projection_link_refs'").toArray().length > 0;
  sql.exec('CREATE TABLE IF NOT EXISTS intake_projection_link_refs (ref TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(ref, task_id)) WITHOUT ROWID');
  const rows = (id: string, from: string) => ['hub_session_id', 'native_id'].map(field =>
    `SELECT json_extract(e.value,'$.${field}'), ${id} FROM ${from} WHERE e.type='object' AND json_type(e.value,'$.${field}')='text'`).join(' UNION ');
  if (!exists) sql.exec(`INSERT INTO intake_projection_link_refs(ref,task_id) ${rows('p.id', 'intake_task_projection p, json_each(p.links) e')} ON CONFLICT DO NOTHING`);
  for (const [op, when] of [['INSERT', 'WHEN json_valid(NEW.links)'], ['UPDATE OF links', 'WHEN NEW.links IS NOT OLD.links AND json_valid(NEW.links)']]) sql.exec(
    `CREATE TRIGGER IF NOT EXISTS intake_projection_link_refs_${op.split(' ')[0].toLowerCase()} AFTER ${op} ON intake_task_projection ${when} BEGIN
    INSERT INTO intake_projection_link_refs(ref,task_id) ${rows('NEW.id', 'json_each(NEW.links) e')} ON CONFLICT DO NOTHING; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS intake_projection_link_refs_delete AFTER DELETE ON intake_task_projection BEGIN DELETE FROM intake_projection_link_refs WHERE task_id=OLD.id; END');
}

export function workerAccessContext(sql: SqlStorage, tokenId: string | undefined, now = Date.now(), allowExpiredAccess = false): any | null {
  if (!tokenId || !sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_access'").toArray().length) return null;
  const row: any = sql.exec(`SELECT w.*, c.actor, c.token_hash, c.expires_at AS credential_expires_at,
    c.revoked_at, c.kind, c.scopes, c.machine_id, c.registry_actor FROM worker_access w JOIN credentials c ON c.family_id=w.family_id WHERE c.id=?`, tokenId).toArray()[0];
  if (!row || row.kind !== 'agent' || row.revoked_at || !Number.isSafeInteger(row.expires_at) || row.expires_at <= now || (!Number.isFinite(Date.parse(row.credential_expires_at)) || (!allowExpiredAccess && (!allowExpiredAccess && Date.parse(row.credential_expires_at) <= now)))) return null;
  let scopes: any; try { scopes = JSON.parse(row.scopes); } catch { return null; }
  if (!Array.isArray(scopes) || !scopes.includes(WORKER_SCOPE) || scopes.includes('admin')) return null;
  const parent: any = sql.exec('SELECT fleet_launch_id,request_json,binding_json,state,engine,parent_id,machine_id FROM session_control_sessions WHERE id=?',row.session_id).toArray()[0];
  if (!parent) return null;
  // Native-session credentials (observed sessions with no fleet launch, e.g. drain
  // briefs started through aplexer): bound to the session itself, valid only
  // while it runs on the machine the credential was minted for.
  if (parent.fleet_launch_id ? parent.fleet_launch_id !== row.launch_id
    : row.launch_id !== row.session_id || parent.state !== 'running' || parent.machine_id !== row.machine_id) return null;
  if(!parent.fleet_launch_id) {
    const authority:any=sql.exec('SELECT enabled FROM native_credential_authority WHERE family_id=?',row.family_id).toArray()[0];
    if(authority && (!authority.enabled || row.registry_actor!==NATIVE_WORKER_PROJECT+':native-'+row.session_id))return null;
  }
  let binding: any; try { binding=JSON.parse(parent.binding_json); } catch {}
  let task: any; try { task=JSON.parse((sql.exec('SELECT data FROM intake_tasks WHERE id=?',row.task_id).toArray()[0] as any)?.data); } catch {}
  const native = binding && typeof binding === 'object' && !Array.isArray(binding) ? binding : null;
  return {...row, task_project: typeof task?.project === 'string' ? task.project : null,
    native_binding: native, session_state: parent.state, parent_id: parent.parent_id,
    native_workspace: typeof native?.workspace === 'string' ? native.workspace : null};
}

/** Refresh proves the opaque refresh token separately; job scope retains its hard deadline. */
export function workerRefreshContext(sql: SqlStorage, tokenId: string, now = Date.now(), allowExpiredAccess = false): any | null {
  return workerAccessContext(sql, tokenId, now, true);
}

/** Narrow wake delegation: self, or the direct parent on the same intake.
 * This never grants send/stop/cleanup authority over the parent. */
export function workerWakeAllowed(sql: SqlStorage, tokenId: string | undefined, sessionId: string, now = Date.now()): boolean {
  const worker = workerAccessContext(sql,tokenId,now);
  if (!worker) return false;
  if (worker.session_id === sessionId) return true;
  if (worker.parent_id !== sessionId) return false;
  const parent: any = sql.exec('SELECT request_json FROM session_control_sessions WHERE id=?',sessionId).toArray()[0];
  try { return JSON.parse(parent?.request_json).intake_task_id === worker.task_id; } catch { return false; }
}

/** Route containment runs in addition to existing registry/family checks. */
/** A triage verdict: a note whose first line starts with TRIAGE <outcome>, or a
 * DONE note written by a backlog triage (shared with src/intake-triage.ts). */
export const TRIAGE_VERDICT = /^(?:TRIAGE (?:ACTUAL|DONE|OBSOLETE|DUPLICATE)\b|DONE\b[^\n]*backlog triage)/;
/** The outcome of an intake's newest triage verdict note (ACTUAL, DONE, OBSOLETE,
 * DUPLICATE), or null when it was never triaged. A `DONE … backlog triage` note is DONE. */
export function latestTriageVerdict(task: { history?: any[] }): 'ACTUAL' | 'DONE' | 'OBSOLETE' | 'DUPLICATE' | null {
  const history = task.history || [];
  for (let i = history.length - 1; i >= 0; i--) {
    const e = history[i], text = String(e?.text ?? '');
    if (e?.kind !== 'note' || !TRIAGE_VERDICT.test(text)) continue;
    return (/^TRIAGE (ACTUAL|DONE|OBSOLETE|DUPLICATE)\b/.exec(text)?.[1] ?? 'DONE') as any;
  }
  return null;
}
const TRIAGE_SESSION_ACTIVE = new Set(['queued', 'held', 'offered', 'starting', 'running', 'uncertain']);
/** AC5 backlog triage (intake d8d509d2): the intakes this worker's live session
 * was asked to triage, from the server-written request ledger. Access to an
 * intake ends as soon as a verdict note lands on it after the request, when
 * the request leaves 'requested', or when the reviewer session ends. Exact ids only. */
export function triageTargets(sql: SqlStorage, sessionId: string | null | undefined): Set<string> {
  const out = new Set<string>();
  if (!sessionId) return out;
  try {
    const session: any = sql.exec('SELECT state FROM session_control_sessions WHERE id=?', sessionId).toArray()[0];
    if (!session || !TRIAGE_SESSION_ACTIVE.has(session.state)) return out;
    for (const r of sql.exec(`SELECT r.task_id, r.requested_at, t.data FROM intake_triage_requests r JOIN intake_tasks t ON t.id=r.task_id
      WHERE r.session_id=? AND r.state='requested'`, sessionId).toArray() as any[]) {
      let task: any; try { task = JSON.parse(r.data); } catch { continue; }
      const verdict = (task.history || []).some((e: any) => e.kind === 'note' && (e.captured_at ?? 0) >= r.requested_at && TRIAGE_VERDICT.test(String(e.text ?? '')));
      if (!verdict && !['done', 'cancelled'].includes(task.status)) out.add(r.task_id);
    }
  } catch { /* ledger absent outside the Hub composition */ }
  return out;
}
/** Intakes a server-verified link ties to this worker's Hub or native session
 * (e.g. a continuation intake, note 083c1588). Exact ids only, bounded. */
export function workerLinkedTargets(sql: SqlStorage, worker: any): Set<string> {
  const keys = [worker?.session_id, worker?.native_binding?.native_id].filter((k: any) => typeof k === 'string' && k);
  if (!keys.length || !sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_session_links'").toArray().length) return new Set();
  return new Set(sql.exec(`SELECT DISTINCT task_id FROM intake_session_links WHERE session_key IN (${keys.map(() => '?').join(',')}) ORDER BY task_id LIMIT ?`,
    ...keys, WORKER_CREATE_LIMIT).toArray().map((r: any) => r.task_id as string));
}
/** Intakes this worker session created, exact ids only. */
export function workerCreatedTargets(sql: SqlStorage, sessionId: string | null | undefined): Set<string> {
  if (!sessionId || !sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_created_intakes'").toArray().length) return new Set();
  return new Set(sql.exec('SELECT task_id FROM worker_created_intakes WHERE session_id=? ORDER BY task_id LIMIT ?', sessionId, WORKER_CREATE_LIMIT)
    .toArray().map((r: any) => r.task_id as string));
}
const INTAKE_TASK_PATH = /^\/api\/intake\/tasks\/([a-zA-Z0-9._:-]+)(\/history|\/events|\/links|\/context|\/depend|\/blocker|\/decompose|\/stats|\/merge-evidence)?$/;
/** Intake 08658ee8 AC3: unique short-id prefixes (>= 8 chars, like sessions)
 * resolve to the full task id for the worker's own, created and linked intakes.
 * Triage requests stay exact-id (intake d8d509d2), so the caller passes the raw
 * reference there. Ambiguous or unknown prefixes stay unresolved. */
export function resolveTaskRef(sql: SqlStorage, id: string): string | null {
  if (!/^[a-zA-Z0-9._:-]{1,128}$/.test(id)) return null;
  const exact: any = sql.exec('SELECT id FROM intake_tasks WHERE id=?', id).toArray()[0];
  if (exact) return exact.id;
  if (id.length < 8) return null;
  const matches: any[] = sql.exec('SELECT id FROM intake_tasks WHERE substr(id,1,?)=? LIMIT 2', id.length, id).toArray();
  return matches.length === 1 ? matches[0].id : null;
}
/** Denial code for a worker request outside its capability. Intake reads or
 * writes aimed at another intake say so explicitly instead of a generic 403. */
export function workerDenialCode(sql: SqlStorage, request: Request, tokenId: string): string {
  const path = new URL(request.url).pathname;
  if (path !== '/api/intake/tasks' && !path.startsWith('/api/intake/tasks/')) return 'worker_scope_forbidden';
  const worker = workerAccessContext(sql, tokenId);
  const id = /^\/api\/intake\/tasks\/([a-zA-Z0-9._:-]+)/.exec(path)?.[1];
  const resolved = id ? resolveTaskRef(sql, id) : null;
  if (worker && id && (resolved === worker.task_id || (id.length >= 8 && worker.task_id.startsWith(id)))) return 'worker_scope_forbidden';
  return 'cross_intake_forbidden';
}
export function workerRequestAllowed(sql: SqlStorage, request: Request, tokenId: string): boolean {
  const worker = workerAccessContext(sql, tokenId);
  if (!worker) return false;
  const url = new URL(request.url), path = url.pathname;
  // Sanitized admission data is needed by the worker itself. Keep telemetry
  // writes, raw host snapshots and operator controls outside this capability.
  if (path === '/api/quota') return request.method === 'GET' && url.searchParams.getAll('view').length === 1 && url.searchParams.get('view') === 'fleet';
  // Sanitized engine verification (intake f28929eb): read-only five-field rows
  // (engine, account, host, health, quota state). The operator engines surface
  // (bundles, logins, credentials) stays outside this capability.
  if (path === '/api/engines') return request.method === 'GET' && url.searchParams.getAll('view').length === 1 && url.searchParams.get('view') === 'worker';
  if (['/api/fleet/resources', '/api/fleet/resources/sessions'].includes(path)) return request.method === 'GET';
  // Usage evidence is shared with TBM. A worker may read only these three
  // sanitized dimensions; this grants no monitoring writes or host telemetry.
  if (path === '/api/monitoring/history') {
    const kinds = url.searchParams.getAll('kind');
    return request.method === 'GET' && kinds.every(kind => ['quota_window', 'quota', 'tokens'].includes(kind));
  }
  // Filing a follow-up intake; the intake service records provenance and caps volume.
  if (path === '/api/intake/tasks') return request.method === 'POST';
  // The handler limits this read-only aggregate to tasks visible under the worker grant.
  if (path === '/api/intake/stats') return request.method === 'GET';
  // Only the release handler opens: it rechecks the exact claim owner under
  // the effect transaction. This grants no claim listing or creation scope.
  if (/^\/api\/bus\/claims\/[^/]+\/release$/.test(path)) return request.method === 'POST';
  const ownIntake = INTAKE_TASK_PATH.exec(path);
  if (ownIntake) {
    const requested = ownIntake[1];
    // Intake 08658ee8 AC3: created and linked intakes are matched through the
    // unique-prefix resolution, so workers can address them by short id too.
    const id = resolveTaskRef(sql, requested) ?? requested;
    // Every worker token is bounded to the project of its server-linked intake,
    // including legacy created/linked/triage grants. Resolve prefixes fail-closed.
    if (typeof worker.task_project !== 'string') return false;
    let matches: any[] = [];
    try {
      matches = sql.exec('SELECT data FROM intake_tasks WHERE id=?', id).toArray() as any[];
      if (!matches.length) matches = sql.exec('SELECT data FROM intake_tasks WHERE substr(id,1,?)=? ORDER BY id LIMIT 2', id.length, id).toArray() as any[];
    } catch { return false; }
    if (matches.length !== 1) return false;
    let target: any;
    try { target = JSON.parse(matches[0].data); } catch { return false; }
    if (target?.project !== worker.task_project) return false;
    // Triage requests remain exact-id scoped even though ordinary task refs accept prefixes.
    if (requested !== worker.task_id && triageTargets(sql, worker.session_id).has(requested))
      return request.method === 'GET' ? !['/events', '/depend', '/blocker', '/decompose'].includes(ownIntake[2] ?? '') : request.method === 'POST' && (!ownIntake[2] || ownIntake[2] === '/events');
    const own = id === worker.task_id || (id.length >= 8 && worker.task_id.startsWith(id));
    const writeTarget = own || workerCreatedTargets(sql, worker.session_id).has(id) || workerLinkedTargets(sql, worker).has(id);
    // Self-report: the session's own token may file a blocker follow-up, link prerequisites and request decomposition.
    const suffix = ownIntake[2] ?? '';
    const write = ['/events', '/depend', '/blocker', '/decompose', '/merge-evidence'].includes(suffix);
    // Merge evidence is accepted only for the worker's own linked intake.
    // The intake handler validates and normalizes the evidence payload.
    const writeAllowed = suffix === '/merge-evidence' ? own : writeTarget;
    return request.method === 'GET' ? !write : request.method === 'POST' && write && writeAllowed;
  }
  if (path === '/api/sessions') return ['GET', 'POST'].includes(request.method);
  const link = /^\/api\/sessions\/([^/]{8,128})\/link$/.exec(path);
  if (link) {
    if (request.method !== 'POST') return false;
    let ref: string;
    try { ref = decodeURIComponent(link[1]); } catch { return false; }
    if (!/^[0-9a-f-]{8,36}$/i.test(ref)) return false;
    const targets = sql.exec(`SELECT id,machine_id,request_json,binding_json FROM session_control_sessions
      WHERE id LIKE ? ORDER BY id LIMIT 101`, ref.toLowerCase() + '%').toArray() as any[];
    return targets.some(row => {
      let intent: any = {}, binding: any = {};
      try { intent = JSON.parse(row.request_json || '{}'); } catch { return false; }
      try { binding = JSON.parse(row.binding_json || '{}'); } catch { return false; }
      const workspace = typeof binding.workspace === 'string' ? binding.workspace : intent.workspace;
      const observed = intent.provenance === 'observed_native' && binding.provenance === 'observed_native';
      const own = row.id === worker.session_id || row.id === worker.launch_id ||
        (typeof binding.native_id === 'string' && binding.native_id === worker.native_binding?.native_id);
      // The endpoint rechecks the exact intake id under its write transaction.
      // This front-door gate limits the target to the worker's machine/workspace
      // and either that worker's own session or an observed native session.
      return row.machine_id === worker.machine_id && workspace === worker.workspace && (own || observed);
    });
  }
  const session = /^\/api\/sessions\/([0-9a-f-]{36})(?:\/(commands|capture|send|stop|wake|wakes|ping|ping-clear|pings))?$/.exec(path);
  if (session) {
    if ([worker.session_id,worker.launch_id,worker.native_binding?.native_id].includes(session[1])) return (request.method === 'GET' && (!session[2] || ['commands','wakes','pings'].includes(session[2]))) ||
      (request.method === 'POST' && ['wake','ping','ping-clear'].includes(session[2]));
    if (['wake','ping','ping-clear'].includes(session[2]) && request.method === 'POST' && workerWakeAllowed(sql,tokenId,session[1])) return true;
    const child: any = sql.exec('SELECT actor,parent_id FROM session_control_sessions WHERE id=?',session[1]).toArray()[0];
    return !!child && child.actor === worker.actor && child.parent_id === worker.session_id;
  }
  // Work claims: the route handler confines them to this intake's project and
  // the worker's own holder (src/worker-claims.ts, intake 6022786b).
  if (workerClaimRoute(path, request.method)) return true;
  return false;
}

export const NATIVE_WORKER_PROJECT = 'hub-native-workers';
const NATIVE_LIST_LIMIT = 10;
/** Open intakes a server-verified link (operator verify_live, never a START
 * note or tag guess) ties to this running native session, oldest id first.
 * Empty when the session has no verified open intake. */
const NATIVE_SCAN_BATCH = 64;
const NATIVE_FAMILY_LIMIT = 128;
export class NativeCredentialObservationLimit extends Error {
  constructor() { super('native_credential_observation_limit'); this.name='NativeCredentialObservationLimit'; }
}
export function nativeSessionIntakes(sql: SqlStorage, sessionId: string, nativeId: string): string[] {
  const keys = [...new Set([sessionId,nativeId].filter(k=>typeof k==='string' && k))];
  if (!keys.length || !sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_session_links'").toArray().length) return [];
  const ids = new Set<string>();
  for (const key of keys) {
    const refs=sql.exec('SELECT task_id FROM intake_session_links WHERE session_key=? ORDER BY task_id LIMIT ?',key,WORKER_CREATE_LIMIT+1).toArray() as any[];
    if(refs.length>WORKER_CREATE_LIMIT)throw new NativeCredentialObservationLimit();
    for(const row of refs)ids.add(String(row.task_id));
  }
  if(ids.size>WORKER_CREATE_LIMIT)throw new NativeCredentialObservationLimit();
  const out:string[]=[];
  for(const id of [...ids].sort()) {
    let task:any;try{task=JSON.parse((sql.exec('SELECT data FROM intake_tasks WHERE id=?',id).toArray()[0] as any)?.data);}catch{continue;}
    if(task && !['done','cancelled'].includes(task.status))out.push(id);
  }
  return out;
}
export function credentialActorRows(sql:SqlStorage,actor:string):any[] {
  const rows=sql.exec('SELECT id,family_id,revoked_at,kind,machine_id FROM credentials INDEXED BY credentials_registry_actor WHERE registry_actor=? LIMIT ?',actor,NATIVE_FAMILY_LIMIT+1).toArray() as any[];
  if(rows.length>NATIVE_FAMILY_LIMIT)throw new NativeCredentialObservationLimit();
  return rows;
}
function nativeFamilyCredentials(sql:SqlStorage,sessionId:string,family:string):any[] {
  // Native grants have one stable actor and launch_id=session_id. The actor
  // index is already deployed; family_id has no index in the live database.
  return credentialActorRows(sql,NATIVE_WORKER_PROJECT+':native-'+sessionId)
    .filter(row=>row.family_id===family && row.revoked_at===null);
}
function revokeNativeFamily(sql:SqlStorage,sessionId:string,family:string,now:number):void {
  // The PK fence revokes every opaque legacy member too; old issuance did
  // not populate registry_actor, so an actor-index walk cannot enumerate it.
  sql.exec('INSERT INTO native_credential_authority VALUES(?,0) ON CONFLICT(family_id) DO UPDATE SET enabled=0',family);
  const at=new Date(now).toISOString();
  for(const credential of nativeFamilyCredentials(sql,sessionId,family))
    sql.exec('UPDATE credentials SET revoked_at=? WHERE id=? AND family_id=? AND revoked_at IS NULL',at,credential.id,family);
}
export function nativeSessionsNeedingIntakeCredentialPage(sql:SqlStorage,machineId:string,after=0):{sessions:any[];next:number} {
  // Bound raw running-machine rows before binding/link/credential filtering.
  // This uses the deployed (machine_id,state,implicit rowid) index and does
  // not sort or anti-join the entire retained session/credential population.
  const raw=sql.exec(`SELECT rowid AS scan_id,id,binding_json,fleet_launch_id FROM session_control_sessions
    INDEXED BY session_control_machine_state WHERE machine_id=? AND state='running' AND rowid>?
    ORDER BY rowid LIMIT ?`,machineId,after,NATIVE_SCAN_BATCH+1).toArray() as any[];
  const out:any[]=[];
  let last=after;
  for(const row of raw.slice(0,NATIVE_SCAN_BATCH)) {
    last=Number(row.scan_id);
    if(row.fleet_launch_id || row.binding_json===null)continue;
    let binding:any;try{binding=JSON.parse(row.binding_json);}catch{continue;}
    if(!binding?.native_id || !binding?.workspace)continue;
    const access:any=sql.exec('SELECT family_id,session_id FROM worker_access WHERE launch_id=?',row.id).toArray()[0];
    if(access && access.session_id===row.id) {
      const authority:any=sql.exec('SELECT enabled FROM native_credential_authority WHERE family_id=?',access.family_id).toArray()[0];
      if(authority?.enabled!==0 && nativeFamilyCredentials(sql,row.id,String(access.family_id)).length)continue;
    }
    const intake=nativeSessionIntakes(sql,String(row.id),String(binding.native_id))[0];
    if(!intake)continue;
    out.push({session_id:String(row.id),native_id:String(binding.native_id),workspace:String(binding.workspace),intake_task_id:intake});
    if(out.length>=NATIVE_LIST_LIMIT)return {sessions:out,next:last};
  }
  return {sessions:out,next:raw.length>NATIVE_SCAN_BATCH?last:0};
}
/** Compatibility entry for bounded direct callers. Hosts use the durable page. */
export function nativeSessionsNeedingIntakeCredential(sql:SqlStorage,machineId:string):any[] {
  return nativeSessionsNeedingIntakeCredentialPage(sql,machineId).sessions;
}
export function revokeNativeWorkerCredentials(sql:SqlStorage,sessionId:string,now:number):void {
  if(!sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_access'").toArray().length)return;
  const access:any=sql.exec('SELECT family_id,session_id FROM worker_access WHERE launch_id=?',sessionId).toArray()[0];
  if(access?.session_id===sessionId)revokeNativeFamily(sql,sessionId,String(access.family_id),now);
}
/** Fair bounded cleanup. Unknown credential coverage refuses the call; it can
 * never silently bless an incomplete family revocation as successful. */
export function revokeEndedNativeWorkerCredentials(sql:SqlStorage,now:number):void {
  if(!sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_access'").toArray().length)return;
  const old=String(sql.exec("SELECT cursor FROM native_credential_scan WHERE key='ended'").toArray()[0]?.cursor??'');
  const rows=sql.exec('SELECT family_id,launch_id,session_id FROM worker_access WHERE family_id>? ORDER BY family_id LIMIT ?',old,NATIVE_SCAN_BATCH+1).toArray() as any[];
  for(const row of rows.slice(0,NATIVE_SCAN_BATCH)) {
    if(row.launch_id!==row.session_id)continue;
    const session:any=sql.exec('SELECT state FROM session_control_sessions WHERE id=?',row.session_id).toArray()[0];
    if(!session || session.state!=='running')revokeNativeFamily(sql,String(row.session_id),String(row.family_id),now);
  }
  const next=rows.length>NATIVE_SCAN_BATCH?String(rows[NATIVE_SCAN_BATCH-1].family_id):'';
  if(next!==old)sql.exec("INSERT INTO native_credential_scan VALUES('ended',?) ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor",next);
}

/** Write-stamp gate for the ended-session backstop sweeps (intake e8a22f4c).
 * `changed` is non-null when a source table was written since the sweep last
 * ran; the caller runs the sweep, then `done` records the post-sweep stamp
 * in the same synchronous transaction. */
export type CredentialSweepGate = {
  changed: (key: string, ...tables: string[]) => string | null;
  done: (key: string, ...tables: string[]) => void;
};

export class WorkerAccess {
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private hostAllowed: (caller: CallerContext) => boolean,
    private sweep?: CredentialSweepGate) {}
  init() {
    this.sql.exec(WORKER_ACCESS_SCHEMA);
    this.sql.exec('CREATE TABLE IF NOT EXISTS native_credential_scan(key TEXT PRIMARY KEY,cursor TEXT NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS native_credential_authority(family_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL)');
    this.sql.exec(DRIVER_ACCESS_SCHEMA); this.sql.exec(WORKER_CREATED_SCHEMA); this.sql.exec(WORKER_CREATED_INDEX);
  }
  releaseClaim(caller: CallerContext, target: string): Response {
    return this.tx(() => {
      const worker = workerAccessContext(this.sql, caller.tokenId);
      const deny = () => Response.json({ error: 'worker_claim_forbidden' }, { status: 403 });
      if (!worker || worker.actor !== caller.actor || worker.token_hash !== caller.tokenHash) return deny();
      let claim: any;
      if (target.includes(':')) {
        const [project, ...parts] = target.split(':');
        const scope = parts.join(':');
        if (scope.includes('..')) return deny();
        const normalized = scope.split('/').filter(p => p && p !== '.').join('/');
        if (!normalized) return deny();
        claim = this.sql.exec('SELECT * FROM claims WHERE project=? AND scope=? AND released_at IS NULL ORDER BY created_at DESC LIMIT 1', project, normalized).toArray()[0];
      } else claim = this.sql.exec('SELECT * FROM claims WHERE id=?', target).toArray()[0];
      const binding = worker.native_binding;
      const ownNative = claim?.claim_source === 'native' && binding?.native_id && binding?.tag &&
        claim.native_session_id === binding.native_id &&
        binding.workspace === worker.workspace && claim.worktree === worker.workspace &&
        claim.holder === formatNativeActor(worker.machine_id, binding.tag, binding.native_id);
      if (!claim || claim.project !== worker.task_project || (claim.holder !== worker.actor && !ownNative)) return deny();
      if (!claim.released_at) this.sql.exec('UPDATE claims SET released_at=? WHERE id=?', Date.now(), claim.id);
      return Response.json({ status: 'ok' });
    });
  }
  /** Host lists running native sessions that need an intake credential. The
   * ended-session revoke sweep is a backstop (transitions revoke directly);
   * it runs only after a write to the tables it reads, never on every poll. */
  private nativePages=new Map<string,any>();
  private nativeCyclePending=new Map<string,boolean>();
  private endedScanPending=true;
  listNative(caller: CallerContext | undefined, scopes: string[] | undefined): Response {
    if(caller?.kind!=='host' || !caller.machineId || !scopes?.includes('host') || !this.hostAllowed(caller))return Response.json({error:'forbidden'},{status:403});
    try { return this.tx(()=>{
      // Credential last_used_at writes cannot invalidate a lifecycle cleanup.
      // Authentication remains authoritative on every request independently.
      const lifecycle=['worker_access','session_control_sessions'];
      const changed=this.sweep?this.sweep.changed('native-credential',...lifecycle):'run';
      if(changed!==null || this.endedScanPending) {
        revokeEndedNativeWorkerCredentials(this.sql,Date.now());
        this.endedScanPending=String(this.sql.exec("SELECT cursor FROM native_credential_scan WHERE key='ended'").toArray()[0]?.cursor??'')!=='';
        this.sweep?.done('native-credential',...lifecycle);
      }
      const key='native-list:'+caller.machineId;
      const dependencies=['worker_access','native_credential_authority','session_control_sessions','credentials','intake_session_links','intake_tasks'];
      const cached=this.nativePages.get(caller.machineId!);
      if(cached && this.sweep?.changed(key,...dependencies)===null)return Response.json(cached,{headers:{'Cache-Control':'no-store'}});
      const cursorKey='machine:'+caller.machineId;
      const old=Number(this.sql.exec('SELECT cursor FROM native_credential_scan WHERE key=?',cursorKey).toArray()[0]?.cursor??0);
      if(!old)this.nativeCyclePending.set(caller.machineId!,false);
      else if(!this.nativeCyclePending.has(caller.machineId!))this.nativeCyclePending.set(caller.machineId!,true);
      const page=nativeSessionsNeedingIntakeCredentialPage(this.sql,caller.machineId!,old);
      if(page.sessions.length)this.nativeCyclePending.set(caller.machineId!,true);
      if(page.next!==old)this.sql.exec('INSERT INTO native_credential_scan VALUES(?,?) ON CONFLICT(key) DO UPDATE SET cursor=excluded.cursor',cursorKey,String(page.next));
      const payload={sessions:page.sessions,...(page.next?{observation_scope:'cursor_batch',observation_pending:true}:{})};
      // Nonempty work must be offered again until issuance changes authority.
      // Continue unfinished scans on later polls even without a source write.
      if(!page.next && !this.nativeCyclePending.get(caller.machineId!)) {this.nativePages.set(caller.machineId!,payload);this.sweep?.done(key,...dependencies);}
      else this.nativePages.delete(caller.machineId!);
      if(!page.next)this.nativeCyclePending.delete(caller.machineId!);
      return Response.json(payload,{headers:{'Cache-Control':'no-store'}});
    }); } catch(error) {
      if(!(error instanceof NativeCredentialObservationLimit))throw error;
      return Response.json({error:'native_credential_observation_limit',observation_pending:true},{status:503,headers:{'Retry-After':'60','Cache-Control':'no-store'}});
    }
  }
  /** Mint the intake-scoped credential of a running native session. Authority is
   * the verified link, never a request field; scope is `worker:own-intake`
   * only (no fleet:launch, no admin). Expires with the session. */
  async issueNative(request: Request, caller: CallerContext | undefined, scopes: string[] | undefined): Promise<Response> {
    const id = /^\/api\/sessions\/([0-9a-f-]{36})\/intake-credential$/.exec(new URL(request.url).pathname)?.[1];
    const reply = (data: any, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'no-store' } });
    if (request.method !== 'POST' || !id || caller?.kind !== 'host' || !caller.machineId || !scopes?.includes('host')) return reply({ error: 'forbidden' }, 403);
    const tokens = await generateTokenPair();
    return this.tx(() => {
      if (!this.hostAllowed(caller)) return reply({ error: 'host_credential_changed' }, 403);
      const session: any = this.sql.exec('SELECT id, machine_id, binding_json, fleet_launch_id, state FROM session_control_sessions WHERE id=?', id).toArray()[0];
      if (!session || session.state !== 'running' || session.machine_id !== caller.machineId || session.fleet_launch_id) return reply({ error: 'not_found' }, 404);
      let binding: any; try { binding = JSON.parse(session.binding_json); } catch {}
      if (!binding?.native_id || !binding?.workspace) return reply({ error: 'not_found' }, 404);
      const intake = nativeSessionIntakes(this.sql, id, String(binding.native_id))[0];
      if (!intake) return reply({ error: 'intake_link_required' }, 409);
      const now = Date.now(), at = new Date(now).toISOString(), expires = now + 7 * 86400000;
      const tag = 'native-' + id, actor = NATIVE_WORKER_PROJECT + ':' + tag;
      const prior: any = this.sql.exec('SELECT family_id FROM worker_access WHERE launch_id=?', id).toArray()[0];
      const family = prior?.family_id || crypto.randomUUID();
      if (!prior) this.sql.exec('INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES(?,?,?,?,?)', tag, NATIVE_WORKER_PROJECT, 'worker', caller.machineId!, at);
      revokeNativeFamily(this.sql,id,family,now);
      const credId = crypto.randomUUID();
      this.sql.exec('INSERT INTO native_credential_authority VALUES(?,1) ON CONFLICT(family_id) DO UPDATE SET enabled=1',family);
      this.sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,registry_actor)
        VALUES(?,'agent',?,?,?,?,?,?,?,?,?,?,?)`, credId, tokens.accessHash, tokens.refreshHash, family, actor, caller.machineId!, actor,
        JSON.stringify(['bus:read', WORKER_SCOPE]), 'host:' + caller.machineId, at, new Date(now + 3600000).toISOString(),actor);
      this.sql.exec(`INSERT INTO worker_access(family_id,launch_id,session_id,task_id,workspace,expires_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(family_id) DO UPDATE SET task_id=excluded.task_id, expires_at=excluded.expires_at`, family, id, id, intake, String(binding.workspace), expires);
      return reply({ access_token: tokens.accessToken, refresh_token: tokens.refreshToken, client_kind: 'agent',
        worker_launch_id: id, base_url: new URL(request.url).origin, worker_expires_at: expires, intake_task_id: intake });
    });
  }
  async issue(request: Request, caller: CallerContext | undefined, scopes: string[] | undefined): Promise<Response> {
    const id = /^\/api\/fleet\/launches\/([0-9a-f-]{36})\/worker-credential$/.exec(new URL(request.url).pathname)?.[1];
    const reply = (data: any, status=200) => Response.json(data,{status,headers:{'Cache-Control':'no-store'}});
    if (request.method !== 'POST' || !id || caller?.kind !== 'host' || !caller.machineId || !scopes?.includes('host')) return reply({error:'forbidden'},403);
    const tokens = await generateTokenPair();
    return this.tx(() => {
      // Token generation awaited above: host registration, presenting family
      // and token freshness must be checked again inside the effect transaction.
      if (!this.hostAllowed(caller)) return reply({error:'host_credential_changed'},403);
      const launch: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id=? AND machine_id=?',id,caller.machineId!).toArray()[0];
      if (!launch || !['headless','interactive'].includes(launch.mode) || !['starting','running'].includes(launch.state)) return reply({error:'worker_launch_not_starting'},409);
      const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE fleet_launch_id=?',id).toArray()[0];
      if (!session) return reply({error:'worker_session_required'},409);
      if (driverLaunchRegistered(this.sql, id)) {
        const profile = issueDriverAccess(this.sql, id, caller.machineId!, tokens, new URL(request.url).origin);
        return profile ? reply(profile) : reply({ error: 'driver_credential_refused' }, 409);
      }
      // The assigned host may restore a lost profile after launch. Keep the
      // original intake binding; this does not restart or adopt a session.
      // The host verifies native process liveness before requesting repair.
      if (launch.state === 'running') {
        let binding: any; try { binding = JSON.parse(session.binding_json); } catch {}
        if (session.state !== 'running' || session.machine_id !== caller.machineId
            || !launch.native_id || binding?.native_id !== launch.native_id
            || binding?.tag !== (launch.mode === 'headless' ? 'task-hub-' : 'hub-') + id) {
          return reply({error:'worker_launch_not_live'},409);
        }
      }
      let intakeId: any; try { intakeId=JSON.parse(session.request_json).intake_task_id; } catch { return reply({error:'worker_intake_required'},409); }
      if (typeof intakeId !== 'string' || !/^[a-zA-Z0-9-]{8,128}$/.test(intakeId)) return reply({error:'worker_intake_required'},409);
      const tasks: any[] = this.sql.exec("SELECT id,data FROM intake_tasks WHERE id=? OR substr(id,1,length(?))=? LIMIT 2",intakeId,intakeId,intakeId).toArray();
      if (tasks.length !== 1) return reply({error:'worker_intake_required'},409);
      const task = JSON.parse(tasks[0].data);
      if (['done','cancelled'].includes(task.status)) return reply({error:'worker_intake_closed'},409);
      const now=Date.now(), at=new Date(now).toISOString(), expires=now+7*86400000;
      const tag='worker-'+id, project='hub-workers', actor=project+':'+tag;
      // One stable registration and family per launch. A lost issuance response
      // may rotate the pre-start credential without creating unbounded grants.
      let prior: any = this.sql.exec('SELECT family_id FROM worker_access WHERE launch_id=?',id).toArray()[0];
      const family=prior?.family_id || crypto.randomUUID();
      if (!prior) this.sql.exec('INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES(?,?,?,?,?)',tag,project,'worker',caller.machineId!,at);
      const credId=crypto.randomUUID();
      this.sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at)
        VALUES(?,'agent',?,?,?,?,?,?,?,?,?,?)`,credId,tokens.accessHash,tokens.refreshHash,family,actor,caller.machineId!,actor,
        JSON.stringify(['bus:read','fleet:launch',WORKER_SCOPE]),'host:'+caller.machineId,at,new Date(now+3600000).toISOString());
      this.sql.exec(`INSERT INTO worker_access(family_id,launch_id,session_id,task_id,workspace,expires_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(family_id) DO UPDATE SET expires_at=excluded.expires_at`,family,id,session.id,tasks[0].id,launch.workspace,expires);
      return reply({access_token:tokens.accessToken,refresh_token:tokens.refreshToken,client_kind:'agent',
        worker_launch_id:id,base_url:new URL(request.url).origin,worker_expires_at:expires});
    });
  }
}

/** Claim routes a worker may reach; src/worker-claims.ts confines them further. */
export function workerClaimRoute(path: string, method: string): boolean {
  if (path === '/api/bus/claims') return method === 'GET' || method === 'POST';
  return method === 'POST' && /^\/api\/bus\/claims\/[^/]+\/release$/.test(path);
}
