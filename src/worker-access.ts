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
  const ownIntake = /^\/api\/intake\/tasks\/([a-zA-Z0-9._:-]+)(\/history|\/events)?$/.exec(path);
  if (ownIntake) {
    const id = ownIntake[1];
    // A triage reviewer may read, note and amend AC/project on its requested intakes.
    if (id !== worker.task_id && triageTargets(sql, worker.session_id).has(id))
      return request.method === 'GET' ? ownIntake[2] !== '/events' : request.method === 'POST' && ownIntake[2] !== '/history';
    if (id !== worker.task_id && !(id.length >= 8 && worker.task_id.startsWith(id))) return false;
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
  init() { this.sql.exec(WORKER_ACCESS_SCHEMA); }
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
