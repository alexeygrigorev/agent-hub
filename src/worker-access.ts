/** Host-issued, refreshable worker credentials restricted to one linked intake.
 * Uses the normal credential/registry lifecycle; no host or human bearer is
 * copied into the worker and no new signing secret is needed.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { CallerContext } from './bus';
import { generateTokenPair } from './auth/token';

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
const TRUSTED_LINK = `json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified')`;
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
    `SELECT json_extract(e.value,'$.${field}'), ${id} FROM ${from} WHERE json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.${field}')='text'`).join(' UNION ');
  if (!exists) sql.exec(`INSERT INTO intake_link_refs(ref,task_id) ${rows('p.id', 'intake_task_projection p, json_each(p.links) e')} ON CONFLICT DO NOTHING`);
  for (const op of ['INSERT', 'UPDATE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_link_refs_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN
    INSERT INTO intake_link_refs(ref,task_id) ${rows('NEW.id', "json_each(NEW.data,'$.history') e")} ON CONFLICT DO NOTHING; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS intake_link_refs_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_link_refs WHERE task_id=OLD.id; END');
}

export function workerAccessContext(sql: SqlStorage, tokenId: string | undefined, now = Date.now(), allowExpiredAccess = false): any | null {
  if (!tokenId || !sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='worker_access'").toArray().length) return null;
  const row: any = sql.exec(`SELECT w.*, c.actor, c.token_hash, c.expires_at AS credential_expires_at,
    c.revoked_at, c.kind, c.scopes, c.machine_id FROM worker_access w JOIN credentials c ON c.family_id=w.family_id WHERE c.id=?`, tokenId).toArray()[0];
  if (!row || row.kind !== 'agent' || row.revoked_at || !Number.isSafeInteger(row.expires_at) || row.expires_at <= now || (!Number.isFinite(Date.parse(row.credential_expires_at)) || (!allowExpiredAccess && (!allowExpiredAccess && Date.parse(row.credential_expires_at) <= now)))) return null;
  let scopes: any; try { scopes = JSON.parse(row.scopes); } catch { return null; }
  if (!Array.isArray(scopes) || !scopes.includes(WORKER_SCOPE) || scopes.includes('admin')) return null;
  const parent: any = sql.exec('SELECT fleet_launch_id,request_json,binding_json,state,engine,parent_id FROM session_control_sessions WHERE id=?',row.session_id).toArray()[0];
  if (!parent || parent.fleet_launch_id !== row.launch_id) return null;
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
const INTAKE_TASK_PATH = /^\/api\/intake\/tasks\/([a-zA-Z0-9._:-]+)(\/history|\/events|\/links|\/context)?$/;
/** Denial code for a worker request outside its capability. Intake reads or
 * writes aimed at another intake say so explicitly instead of a generic 403. */
export function workerDenialCode(sql: SqlStorage, request: Request, tokenId: string): string {
  const path = new URL(request.url).pathname;
  if (path !== '/api/intake/tasks' && !path.startsWith('/api/intake/tasks/')) return 'worker_scope_forbidden';
  const worker = workerAccessContext(sql, tokenId);
  const id = /^\/api\/intake\/tasks\/([a-zA-Z0-9._:-]+)/.exec(path)?.[1];
  if (worker && id && (id === worker.task_id || (id.length >= 8 && worker.task_id.startsWith(id)))) return 'worker_scope_forbidden';
  return 'cross_intake_forbidden';
}
export function workerRequestAllowed(sql: SqlStorage, request: Request, tokenId: string): boolean {
  const worker = workerAccessContext(sql, tokenId);
  if (!worker) return false;
  const url = new URL(request.url), path = url.pathname;
  // Sanitized admission data is needed by the worker itself. Keep telemetry
  // writes, raw host snapshots and operator controls outside this capability.
  if (path === '/api/quota') return request.method === 'GET' && url.searchParams.getAll('view').length === 1 && url.searchParams.get('view') === 'fleet';
  if (['/api/fleet/resources', '/api/fleet/resources/sessions'].includes(path)) return request.method === 'GET';
  // Usage evidence is shared with TBM. A worker may read only these three
  // sanitized dimensions; this grants no monitoring writes or host telemetry.
  if (path === '/api/monitoring/history') {
    const kinds = url.searchParams.getAll('kind');
    return request.method === 'GET' && kinds.every(kind => ['quota_window', 'quota', 'tokens'].includes(kind));
  }
  // Filing a follow-up intake; the intake service records provenance and caps volume.
  if (path === '/api/intake/tasks') return request.method === 'POST';
  const ownIntake = INTAKE_TASK_PATH.exec(path);
  if (ownIntake) {
    const id = ownIntake[1];
    // A triage reviewer may read, note and amend AC/project on its requested intakes.
    if (id !== worker.task_id && triageTargets(sql, worker.session_id).has(id))
      return request.method === 'GET' ? ownIntake[2] !== '/events' : request.method === 'POST' && (!ownIntake[2] || ownIntake[2] === '/events');
    const own = id === worker.task_id || (id.length >= 8 && worker.task_id.startsWith(id));
    if (!own && !workerCreatedTargets(sql, worker.session_id).has(id) && !workerLinkedTargets(sql, worker).has(id)) return false;
    return request.method === 'GET' ? ownIntake[2] !== '/events' : request.method === 'POST' && ownIntake[2] === '/events';
  }
  if (path === '/api/sessions') return ['GET', 'POST'].includes(request.method);
  const session = /^\/api\/sessions\/([0-9a-f-]{36})(?:\/(commands|capture|send|stop|wake|wakes))?$/.exec(path);
  if (session) {
    if ([worker.session_id,worker.launch_id,worker.native_binding?.native_id].includes(session[1])) return (request.method === 'GET' && (!session[2] || ['commands','wakes'].includes(session[2]))) ||
      (request.method === 'POST' && session[2] === 'wake');
    if (session[2] === 'wake' && request.method === 'POST' && workerWakeAllowed(sql,tokenId,session[1])) return true;
    const child: any = sql.exec('SELECT actor,parent_id FROM session_control_sessions WHERE id=?',session[1]).toArray()[0];
    return !!child && child.actor === worker.actor && child.parent_id === worker.session_id;
  }
  return false;
}

export class WorkerAccess {
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private hostAllowed: (caller: CallerContext) => boolean) {}
  init() { this.sql.exec(WORKER_ACCESS_SCHEMA); this.sql.exec(WORKER_CREATED_SCHEMA); this.sql.exec(WORKER_CREATED_INDEX); }
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
