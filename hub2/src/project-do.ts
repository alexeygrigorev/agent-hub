import {listProjectIntakes} from './intake-list';
import {boundWorkspace,mergeWorkspaceBinding,type WorkspaceBinding} from './workspace-binding';
import { MeteredDb, initializeSchema } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';
import { advanceDispatch } from './dispatch';
import { pickCommittedModelRoute, type PickedRoute, type PickDiagnostics } from './model-picker';
import {buildStartPickerEvidence, normalizeStartPickerEvidence} from './start-picker-evidence';
import { sessionPurpose } from './session-purpose';
import { nativeReturncode } from './native-exit';
import { HOST_INVENTORY_LOST_MS } from './session-liveness';
import { PROJECT_WORK_SCHEMA, ProjectWork, type ProjectWorkHooks } from './project-work';
import type { Actor, Hub2Env, JsonRecord } from './types';

const SCHEMA = [
  `CREATE TABLE intakes (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
    text TEXT NOT NULL, acceptance_criteria_json TEXT NOT NULL,
    status TEXT NOT NULL, assigned_machine_id TEXT, assigned_actor_id TEXT, session_id TEXT,
    final_evidence_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    original_title TEXT,original_text TEXT,source_json TEXT NOT NULL DEFAULT '{"type":"user"}',priority INTEGER NOT NULL DEFAULT 2,owner_actor_id TEXT,route_registered INTEGER NOT NULL DEFAULT 0,route_next_action_at INTEGER NOT NULL DEFAULT 0,parent_intake_id TEXT,schedule_id TEXT,occurrence_id TEXT
  )`,
  'CREATE INDEX intake_route_pending ON intakes(route_registered,route_next_action_at,id) WHERE route_registered=0',
  'CREATE UNIQUE INDEX intakes_occurrence ON intakes(occurrence_id) WHERE occurrence_id IS NOT NULL',
  'CREATE INDEX intakes_schedule ON intakes(schedule_id,status,id) WHERE schedule_id IS NOT NULL',
  'CREATE INDEX intakes_status_updated ON intakes(status, updated_at, id)',
  'CREATE INDEX intakes_updated ON intakes(updated_at, id)',
  `CREATE TABLE intake_events (
    task_id TEXT NOT NULL, seq INTEGER NOT NULL, event_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL, kind TEXT NOT NULL, body_json TEXT NOT NULL, at INTEGER NOT NULL,
    PRIMARY KEY(task_id, seq)
  ) WITHOUT ROWID`,
  `CREATE TABLE idempotency (
    scope TEXT NOT NULL, request_key TEXT NOT NULL, request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
    PRIMARY KEY(scope, request_key)
  )`,
  'CREATE INDEX idempotency_expiry ON idempotency(expires_at, scope, request_key)',
  `CREATE TABLE dispatch_sagas (
    operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, request_key TEXT NOT NULL,
    intake_id TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE, machine_id TEXT NOT NULL, account_id TEXT,
    attempt_operation_id TEXT NOT NULL,
    launch_spec_json TEXT NOT NULL,routing_json TEXT,retry_count INTEGER NOT NULL DEFAULT 0,native_id TEXT,route_registered INTEGER NOT NULL DEFAULT 0,
    epoch INTEGER, state TEXT NOT NULL, attempt INTEGER NOT NULL, next_action_at INTEGER,due INTEGER GENERATED ALWAYS AS(next_action_at IS NOT NULL) VIRTUAL,
    last_reason TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX dispatch_saga_deadline ON dispatch_sagas(due,next_action_at, operation_id) WHERE due=1',
  ...PROJECT_WORK_SCHEMA,
];


type IntakeRow = {
  id: string; project: string; title: string; text: string; acceptance_criteria_json: string;
  status: string; assigned_machine_id: string | null; assigned_actor_id: string | null; session_id: string | null;
  final_evidence_json: string | null; created_at: number; updated_at: number;
  original_title?:string|null;original_text?:string|null;source_json?:string;priority?:number;owner_actor_id?: string | null; route_registered?: number; route_next_action_at?: number;
  parent_intake_id?: string | null; schedule_id?: string | null; occurrence_id?: string | null;
};
type SagaRow = {
  operation_id: string; request_digest: string; request_key: string; intake_id: string;
  session_id: string; machine_id: string; account_id: string | null; attempt_operation_id: string;
  launch_spec_json: string; epoch: number | null; state: string;
  attempt: number; retry_count: number; next_action_at: number | null; last_reason: string | null; native_id: string | null;
  routing_json: string | null;route_registered:number;created_at:number;
};
type IntakeEventRow = { seq: number; actor_id: string; kind: string; body_json: string; at: number };

/** Per-project intake, queue, idempotency and launch-saga owner. */
export class ProjectDO {
  protected readonly db: MeteredDb;
  private readonly sqlReceipts: SqlReceiptMeter;
  private readonly env: Hub2Env;
  private readonly work: ProjectWork;
  private readonly ready: Promise<void>;
  private constructorMs = 0;
  private readonly eventSequences=new Map<string,number>();
  private pendingSequences:Map<string,number>|undefined;
  private routingQueue:Promise<void>=Promise.resolve();

  constructor(private readonly ctx: DurableObjectState, env: Hub2Env) {
    const started = performance.now();
    this.db = new MeteredDb(ctx.storage.sql);
    this.sqlReceipts = new SqlReceiptMeter(this.db);
    this.env = wrapSqlNamespaces(env, this.sqlReceipts) as Hub2Env;
    this.work = new ProjectWork(ctx, this.db, {
      authorize: (actor, intakeId) => this.workAuthorized(actor, intakeId),
      active: scheduleId => this.scheduleHasActiveSession(scheduleId),
      occurrence: record => this.scheduleOccurrence(record),
    });
    this.ready = initializeSchema(ctx, this.db, 4, SCHEMA, [], async () => {
      await this.retryPendingIntakeRoutes();
      await this.scheduleNext();
      this.constructorMs = performance.now() - started;
    },4);
  }

  async fetch(request: Request): Promise<Response> {
    return this.sqlReceipts.measure(() => this.handleFetch(request), request.headers.get(SQL_PROBE_HEADER));
  }

  private async handleFetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      await this.ready;
      if (url.pathname === '/_internal/finish' && request.method === 'POST') return await this.finishInternal(request);
      if (url.pathname === '/_internal/started' && request.method === 'POST') return await this.startedInternal(request);
      if ((url.pathname === '/_internal/stopped' || url.pathname === '/_internal/reaped') && request.method === 'POST') return await this.stoppedInternal(request);
      if (url.pathname === '/_internal/auth-fallback' && request.method === 'POST') return await this.authFallbackInternal(request);
      if (url.pathname === '/_internal/requeue' && request.method === 'POST') return await this.requeueInternal(request);
      if (url.pathname === '/_internal/claim' && request.method === 'POST') return await this.claimInternal(request);
      if (url.pathname === '/_internal/admission' && request.method === 'POST') return await this.admissionInternal(request);
      if (url.pathname === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot(), constructor_ms: this.constructorMs });
      if(url.pathname==='/api/schedules'&&request.method==='POST'){
        const actor=actorFrom(request),body=await bodyObject(request.clone(),32768);
        const parent=typeof body.intake_id==='string'?this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',body.intake_id):undefined;
        const payload=body.payload as JsonRecord|undefined,launch=payload?.start as JsonRecord|undefined||payload;
        if(launch?.engine==='shell'&&!hasScope(actor,'operator:admin'))return error('forbidden',403);
        if(parent&&(['root','writer'].includes(String(body.kind))?!this.canProject(actor,parent.project,'session:start'):!hasScope(actor,'operator:admin')&&!hasScope(actor,'message:send')))return error('schedule_effect_forbidden',403);
      }
      const workResponse = await this.work.fetch(request);
      if (workResponse) {
        if (!['GET', 'HEAD'].includes(request.method) && workResponse.status < 400) await this.scheduleNext();
        return workResponse;
      }
      if (url.pathname === '/api/intakes' && request.method === 'POST') return await this.createIntake(request);
      if (url.pathname === '/api/intakes' && request.method === 'GET') return this.listIntakes(request);
      const match = url.pathname.match(/^\/api\/intakes\/([^/]+)(?:\/(start|history|append|events|context|audit|discard))?$/);
      if (!match) return error('not_found', 404);
      const intakeId = decodeURIComponent(match[1]);
      if (match[2] === 'start' && request.method === 'POST') return await this.startIntake(request, intakeId);
      if (match[2] === 'discard' && request.method === 'POST') return await this.discardUnused(request, intakeId);
      if (match[2] === 'history' && request.method === 'GET') return this.history(request, intakeId);
      if(match[2]==='append'&&request.method==='POST')return await this.appendIntake(request,intakeId);
      if(match[2]==='events'&&request.method==='POST')return await this.eventsCompat(request,intakeId);
      if(match[2]==='context'&&request.method==='GET')return this.contextView(request,intakeId);
      if(match[2]==='audit'&&request.method==='GET')return this.history(request,intakeId);
      if(!match[2]&&request.method==='POST')return await this.updateCompat(request,intakeId);
      if (!match[2] && request.method === 'GET') return this.showIntake(request, intakeId);
      if(!match[2]&&request.method==='PATCH')return await this.editIntake(request,intakeId);
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status, caught.detail);
      return error('internal_error', 500);
    }
  }

  async alarm(): Promise<void> {
    await this.ready;
    const now = Date.now();
    try {
      await this.work.alarm(now);
      await this.retryPendingIntakeRoutes(now);
      const due = this.db.all<{ operation_id: string }>(
        `SELECT operation_id FROM dispatch_sagas WHERE due=1 AND state IN ('pending_machine','staged') AND next_action_at <= ? ORDER BY next_action_at, operation_id LIMIT 20`, now,
      );
      for (const row of due) {
        if (!await this.dispatchSaga(row.operation_id)) break;
      }
    } catch (caught) {
      console.error('[hub2] project alarm failed:', caught);
    } finally {
      await this.scheduleNext();
    }
  }

  private async createIntake(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const project = new URL(request.url).searchParams.get('project') || '';
    if (!project || project.length > 180 || !this.canProject(actor, project, 'intake:write')) return error('forbidden', 403);
    const body = await bodyObject(request, 16_384);
    normalizeCriteria(body);
    if (typeof body.title !== 'string' || body.title.trim().length < 1 || body.title.length > 240) return error('invalid_title', 400);
    if (typeof body.text !== 'string' || body.text.trim().length < 1 || new TextEncoder().encode(body.text).byteLength > 8192) return error('invalid_text', 400);
    if (!Array.isArray(body.acceptance_criteria) || body.acceptance_criteria.length < 1 || body.acceptance_criteria.length > 32
      || !body.acceptance_criteria.every(item => typeof item === 'string' && item.trim().length > 0 && item.length <= 1000)) return error('acceptance_criteria_required', 400);
    const key = this.idempotencyKey(request, body);
    if (!key) return error('idempotency_key_required', 400);
    const normalized = { title: body.title.trim(), text: body.text, acceptance_criteria: body.acceptance_criteria.map(value => (value as string).trim()) };
    const digest = await sha256Hex(JSON.stringify(normalized));
    const prior = this.idempotent('intake:create', key, digest);
    if (prior instanceof Response) return prior;
    if (prior) {
      const existing = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', prior.id as string);
      if (!existing) return error('intake_not_found', 404);
      const routeReady = existing.route_registered === 1 || await this.registerIntakeRoute(existing);
      await this.scheduleNext();
      return routeReady ? this.showIntake(request, existing.id, 200) : error('route_registration_pending', 503);
    }
    const now = Date.now();
    const id = crypto.randomUUID();
    this.transaction(() => {
      this.db.run(`INSERT INTO intakes(id, project, title, text, acceptance_criteria_json, status, assigned_machine_id, assigned_actor_id, session_id, final_evidence_json, created_at, updated_at, owner_actor_id, route_registered, route_next_action_at,original_title,original_text,source_json,priority)
        VALUES (?, ?, ?, ?, ?, 'open', NULL, ?, NULL, NULL, ?, ?, ?, 0, ?,?,?,?,?)`, id, project, normalized.title, normalized.text, JSON.stringify(normalized.acceptance_criteria), actor?.id || 'operator', now, now, actor?.id || 'operator', now,normalized.title,normalized.text,JSON.stringify({type:'user'}),2);
      this.appendEvent(id, `intake:${id}:created`, actor?.id || 'operator', 'created', { title: normalized.title, acceptance_criteria: normalized.acceptance_criteria }, now);
      this.db.run('INSERT INTO idempotency(scope, request_key, request_digest, response_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        'intake:create', key, digest, JSON.stringify({ id }), now, now + 180 * 24 * 60 * 60_000);
    });
    const created = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', id);
    const routeReady = !!created && await this.registerIntakeRoute(created);
    await this.scheduleNext();
    return routeReady ? this.showIntake(request, id, 201) : error('route_registration_pending', 503);
  }

  private listIntakes(request: Request): Response {
    const actor = actorFrom(request), query = new URL(request.url).searchParams;
    const project = query.get('project') || '';
    if (!this.canProject(actor, project, 'intake:read')) return error('forbidden',403);
    return listProjectIntakes(this.db,query,project);
  }

  private showIntake(request: Request, intakeId: string, status = 200): Response {
    const actor = actorFrom(request);
    const row = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!row) return error('intake_not_found', 404);
    if (!this.canReadIntake(actor, row)) return error('forbidden', 403);
    return json({ intake: this.viewIntake(row), task: this.taskCompat(row) }, status);
  }

  /** Version handle for compare-and-set callers (CLI `intake close/update`): the row's updated_at. */
  private taskCompat(row: IntakeRow): JsonRecord {
    return { id: row.id, updated_at: row.updated_at, control: { version: row.updated_at } };
  }

  /** `hub intake context`: the intake plus its most recent events, bounded. */
  private contextView(request: Request, intakeId: string): Response {
    const actor = actorFrom(request);
    const row = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!row) return error('intake_not_found', 404);
    if (!this.canReadIntake(actor, row)) return error('forbidden', 403);
    const rows = this.db.all<IntakeEventRow>('SELECT seq, actor_id, kind, body_json, at FROM intake_events WHERE task_id = ? ORDER BY seq DESC LIMIT 50', intakeId).reverse();
    return json({ intake: this.viewIntake(row), task: this.taskCompat(row),
      events: rows.map(r => ({ seq: r.seq, actor_id: r.actor_id, kind: r.kind, at: r.at, body: this.parse(r.body_json) })) });
  }

  /** Hub1-shaped `POST /events` ({key,note}|{key,event}) mapped onto Hub2 append (note text or evidence object). */
  private async eventsCompat(request: Request, intakeId: string): Promise<Response> {
    const body = await bodyObject(request.clone(), 16384);
    const mapped: JsonRecord = { key: body.key };
    const event = body.event as JsonRecord | undefined;
    if (typeof body.note === 'string') mapped.note = body.note;
    else if (event && typeof event === 'object' && !Array.isArray(event)) {
      const text = typeof event.text === 'string' ? event.text : typeof event.note === 'string' ? event.note : undefined;
      if (event.kind === 'note' && text && Object.keys(event).every(k => ['kind', 'text', 'note'].includes(k))) mapped.note = text;
      else { mapped.evidence = event; }
    }
    return await this.appendIntake(new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify(mapped) }), intakeId);
  }

  /** Hub1-shaped `POST /tasks/:id` ({key,status,expected_version,reason,...}) mapped onto Hub2 edit; `done` closes. */
  private async updateCompat(request: Request, intakeId: string): Promise<Response> {
    const body = await bodyObject(request.clone(), 16384);
    const row = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!row) return error('intake_not_found', 404);
    const mapped: JsonRecord = { key: body.key };
    if (typeof body.expected_version === 'number') mapped.expected_updated_at = body.expected_version;
    for (const field of ['title', 'priority', 'acceptance_criteria']) if (body[field] !== undefined) mapped[field] = body[field];
    if (body.status !== undefined) {
      // A cancelled or superseded intake is archived (never deleted); the replacement id stays in the note.
      mapped.status = body.status === 'done' || body.status === 'closed'
        ? (row.status === 'completed-awaiting-review' ? 'accepted' : 'archived')
        : ['cancelled', 'canceled', 'superseded'].includes(String(body.status)) ? 'archived' : body.status;
    }
    if (body.superseded_by !== undefined && (typeof body.superseded_by !== 'string' || !/^[A-Za-z0-9._:-]{6,128}$/.test(body.superseded_by))) return error('invalid_superseded_by', 400);
    const why = [body.outcome, body.superseded_by ? `superseded by ${body.superseded_by}` : undefined, body.reason, body.note].filter(v => typeof v === 'string' && v).join(': ');
    if (why) mapped.note = why.slice(0, 4000);
    return await this.editIntake(new Request(request.url, { method: 'PATCH', headers: request.headers, body: JSON.stringify(mapped) }), intakeId);
  }

  private history(request: Request, intakeId: string): Response {
    const actor = actorFrom(request);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!intake) return error('intake_not_found', 404);
    if (!this.canReadIntake(actor, intake)) return error('forbidden', 403);
    const rawAfter = new URL(request.url).searchParams.get('after') || '0';
    const after = Number(rawAfter);
    if (!Number.isSafeInteger(after) || after < 0) return error('invalid_cursor', 400);
    const rows = this.db.all<IntakeEventRow>(
      'SELECT seq, actor_id, kind, body_json, at FROM intake_events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT 101', intakeId, after,
    );
    const hasMore = rows.length > 100;
    const page = rows.slice(0, 100).map(row => ({ seq: row.seq, actor_id: row.actor_id, kind: row.kind, at: row.at, body: this.parse(row.body_json) }));
    return json({ events: page, next: hasMore ? page[page.length - 1]?.seq : null });
  }

  /**
   * Archive an intake that never carried work. `hub sessions start` creates an intake and then starts it;
   * when that start is refused the intake would stay open forever. The fence is atomic and strict: only an
   * `open` intake with no session, no host, no dispatch saga and no event besides its creation is discarded.
   * Internal to the session-start path (the public router does not expose it).
   */
  private async discardUnused(request: Request, intakeId: string): Promise<Response> {
    const actor = actorFrom(request), body = await bodyObject(request, 4096), key = this.idempotencyKey(request, body);
    if (!key || typeof body.reason !== 'string' || !body.reason.trim() || body.reason.length > 200) return error('invalid_discard', 400);
    const reason = body.reason.trim(), eventKey = 'discard:' + intakeId + ':' + key;
    return this.transaction(() => {
      const row = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', intakeId);
      if (!row) return error('intake_not_found', 404);
      if (!this.canProject(actor, row.project, 'intake:write')) return error('forbidden', 403);
      if (this.db.one('SELECT seq FROM intake_events WHERE event_key=?', eventKey))
        return json({ discarded: row.status === 'archived', duplicate: true, intake: { id: row.id, status: row.status } });
      const used = row.status !== 'open' || row.session_id !== null || row.assigned_machine_id !== null
        || !!this.db.one('SELECT operation_id FROM dispatch_sagas WHERE intake_id=? LIMIT 1', row.id)
        || !!this.db.one("SELECT seq FROM intake_events WHERE task_id=? AND kind<>'created' LIMIT 1", row.id);
      if (used) return error('intake_in_use', 409);
      const at = Date.now();
      this.db.run("UPDATE intakes SET status='archived',updated_at=? WHERE id=? AND status='open' AND session_id IS NULL", at, row.id);
      this.appendEvent(row.id, eventKey, actor!.id, 'intake_updated', { status: 'archived', note: reason }, at);
      return json({ discarded: true, intake: { id: row.id, status: 'archived' } });
    });
  }

  private async appendIntake(request:Request,intakeId:string):Promise<Response>{
    const actor=actorFrom(request),body=await bodyObject(request,16384),key=this.idempotencyKey(request,body);
    if(!key||Object.keys(body).some(k=>!['key','note','evidence'].includes(k)))return error('invalid_append',400);
    if(body.note!==undefined&&(typeof body.note!=='string'||!body.note.trim()||body.note.length>4000)||body.evidence!==undefined&&(!body.evidence||typeof body.evidence!=='object'||Array.isArray(body.evidence)||!Object.keys(body.evidence).length)||body.note===undefined&&body.evidence===undefined)return error('invalid_append',400);
    const digest=await sha256Hex(JSON.stringify([body.note??null,body.evidence??null])),eventKey='append:'+intakeId+':'+key;
    return this.transaction(()=>{
      const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intakeId);if(!intake)return error('intake_not_found',404);
      if(!actor||!hasScope(actor,'operator:admin')&&!(actor.resource_id===intake.id&&hasScope(actor,'intake:append')))return error('forbidden',403);
      const previous=this.db.one<{body_json:string}>('SELECT body_json FROM intake_events WHERE event_key=?',eventKey);
      if(previous)return (JSON.parse(previous.body_json) as JsonRecord).request_digest===digest?json({appended:true,duplicate:true,intake_id:intakeId}):error('idempotency_conflict',409);
      this.appendEvent(intake.id,eventKey,actor.id,'worker_append',{...(body.note?{note:body.note}:{}),...(body.evidence?{evidence:body.evidence}:{}),request_digest:digest},Date.now());
      return json({appended:true,intake_id:intake.id},201);
    });
  }

  private async editIntake(request:Request,intakeId:string):Promise<Response>{
    const actor=actorFrom(request),body=await bodyObject(request,16384),key=this.idempotencyKey(request,body);normalizeCriteria(body);
    if(!key)return error('idempotency_key_required',400);
    const allowed=['key','expected_updated_at','title','text','acceptance_criteria','status','priority','note'];
    if(!['title','text','acceptance_criteria','status','priority','note'].some(k=>body[k]!==undefined)||Object.keys(body).some(k=>!allowed.includes(k)))return error('unknown_intake_field',400);
    if(body.title!==undefined&&(typeof body.title!=='string'||!body.title.trim()||body.title.length>240)||body.text!==undefined&&(typeof body.text!=='string'||!body.text.trim()||new TextEncoder().encode(body.text).length>8192)||body.note!==undefined&&(typeof body.note!=='string'||!body.note.trim()||body.note.length>4000)||body.priority!==undefined&&(!Number.isInteger(body.priority)||Number(body.priority)<0||Number(body.priority)>3)||body.status!==undefined&&!['open','archived','needs_attention','accepted'].includes(String(body.status)))return error('invalid_intake_edit',400);
    if(body.acceptance_criteria!==undefined&&(!Array.isArray(body.acceptance_criteria)||!body.acceptance_criteria.length||body.acceptance_criteria.length>32||!body.acceptance_criteria.every(v=>typeof v==='string'&&v.trim()&&v.length<=1000)))return error('acceptance_criteria_required',400);
    const digest=await sha256Hex(JSON.stringify(body)),scope='intake:edit:'+intakeId;
    const replay=this.idempotent(scope,key,digest);if(replay instanceof Response)return replay;
    const result=this.transaction(()=>{
      const row=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intakeId);if(!row)return error('intake_not_found',404);
      if(!this.canProject(actor,row.project,'intake:write'))return error('forbidden',403);
      if(replay)return json({intake:this.viewIntake(row),duplicate:true});
      if(body.expected_updated_at!==undefined&&body.expected_updated_at!==row.updated_at)return error('intake_version_conflict',409);
      const changesCore=body.title!==undefined||body.text!==undefined||body.acceptance_criteria!==undefined||body.status!==undefined;
      if(changesCore&&(['queued','running'].includes(row.status)||(row.session_id&&this.db.one<SagaRow>("SELECT * FROM dispatch_sagas WHERE session_id=? AND state<>'complete'",row.session_id))))return error('intake_active',409);
      if(body.status==='accepted'&&(row.status!=='completed-awaiting-review'||!hasScope(actor,'operator:admin')))return error('review_required',409);
      const at=Date.now(),next={title:typeof body.title==='string'?body.title.trim():row.title,text:body.text??row.text,acceptance_criteria_json:body.acceptance_criteria?JSON.stringify(body.acceptance_criteria):row.acceptance_criteria_json,status:body.status??row.status,priority:body.priority??row.priority??2};
      this.db.run('UPDATE intakes SET original_title=COALESCE(original_title,title),original_text=COALESCE(original_text,text),title=?,text=?,acceptance_criteria_json=?,status=?,priority=?,updated_at=? WHERE id=?',next.title,next.text,next.acceptance_criteria_json,next.status,next.priority,at,row.id);
      this.appendEvent(row.id,'edit:'+row.id+':'+key,actor!.id,'intake_updated',{...next,...(body.note?{note:body.note}:{})},at);
      this.db.run('INSERT INTO idempotency(scope,request_key,request_digest,response_json,created_at,expires_at) VALUES(?,?,?,?,?,?)',scope,key,digest,JSON.stringify({id:row.id}),at,at+180*86400000);
      return json({intake:this.viewIntake({...row,...next,updated_at:at} as IntakeRow)});
    });return result;
  }

  private async startIntake(request: Request, intakeId: string): Promise<Response> {
    const accepted=await this.serializeRouting(()=>this.prepareStartIntake(request,intakeId));
    if(accepted instanceof Response)return accepted;
    // Routing decisions are serialized only until their durable receipt exists;
    // dispatch and host launch do not hold the picker queue.
    await this.scheduleNext();
    await this.dispatchSaga(accepted);
    await this.scheduleNext();
    const saga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',accepted);
    return saga?this.startResponse(saga,['offered','claimed','running'].includes(saga.state)?201:202):error('dispatch_state_unavailable',503);
  }

  private async serializeRouting<T>(action:()=>Promise<T>):Promise<T>{
    const previous=this.routingQueue;
    let release!:()=>void;
    this.routingQueue=new Promise<void>(resolve=>{release=resolve;});
    await previous;
    try{return await action();}finally{release();}
  }

  private async prepareStartIntake(request: Request, intakeId: string): Promise<Response|string> {
    const actor = actorFrom(request);
    const body = await bodyObject(request, 16_384);
    const key = this.idempotencyKey(request, body);
    if (!key) return error('idempotency_key_required', 400);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!intake) return error('intake_not_found', 404);
    if (!this.canProject(actor, intake.project, 'session:start')) return error('forbidden', 403);
    if (intake.route_registered !== 1 && !await this.registerIntakeRoute(intake)) {
      await this.scheduleNext();
      return error('route_registration_pending', 503);
    }
    const criteria = this.parse(intake.acceptance_criteria_json);
    if (!Array.isArray(criteria) || criteria.length === 0) return error('acceptance_criteria_required', 409);
    const workspace = body.workspace;
    const machineInput = body.machine_id;
    const engineInput = body.engine;
    const modelInput = body.model;
    const effortInput = body.effort;
    const purpose = sessionPurpose(body.purpose);
    const reasonInput = body.override_reason === undefined ? body.reason : body.override_reason;
    const prompt = typeof body.prompt === 'string' ? body.prompt : intake.text;
    const interactive = body.interactive === undefined ? false : body.interactive;
    const timeoutSeconds = body.timeout_seconds === undefined ? 1800 : body.timeout_seconds;
    const absoluteWorkspace = typeof workspace === 'string' && (workspace.startsWith('/') || /^[A-Za-z]:\\/.test(workspace));
    const workspaceLabel = typeof workspace === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(workspace);
    if (typeof workspace !== 'string' || workspace.length > 1024
      || (!absoluteWorkspace && !workspaceLabel) || workspace.includes('\0')) return error('invalid_workspace', 400);
    if (machineInput !== undefined && (typeof machineInput !== 'string' || !/^[A-Za-z0-9_.-]{1,80}$/.test(machineInput))) return error('invalid_machine_id', 400);
    if (engineInput !== undefined && (typeof engineInput !== 'string' || !/^[A-Za-z0-9_.-]{1,48}$/.test(engineInput))) return error('invalid_engine', 400);
    if (modelInput !== undefined && (typeof modelInput !== 'string' || modelInput.length < 1 || modelInput.length > 128)) return error('invalid_model', 400);
    if (effortInput !== undefined && (typeof effortInput !== 'string' || !/^[A-Za-z0-9_.-]{1,48}$/.test(effortInput))) return error('invalid_effort', 400);
    if (reasonInput !== undefined && (typeof reasonInput !== 'string' || reasonInput.length > 500)) return error('invalid_override_reason', 400);
    if (typeof prompt !== 'string' || prompt.trim().length < 1 || new TextEncoder().encode(prompt).byteLength > 8192) return error('invalid_prompt', 400);
    if (typeof interactive !== 'boolean') return error('invalid_interactive', 400);
    if (!Number.isInteger(timeoutSeconds) || Number(timeoutSeconds) < 60 || Number(timeoutSeconds) > 7200) return error('invalid_timeout', 400);
    const withGoal = body.with_goal === undefined ? false : body.with_goal;
    if (typeof withGoal !== 'boolean') return error('invalid_with_goal', 400);
    if (withGoal&&typeof engineInput==='string'&&engineInput.toLowerCase()==='shell') return error('goal_unavailable', 409);
    const capabilities = body.required_capabilities === undefined ? [] : body.required_capabilities;
    if (!Array.isArray(capabilities) || capabilities.length > 32
      || capabilities.some(value => typeof value !== 'string' || value.length < 1 || value.length > 80)) return error('invalid_required_capabilities', 400);
    const requiredCapabilities = [...new Set(capabilities as string[])].sort();
    const overrideReason = typeof reasonInput === 'string' ? reasonInput.trim() : undefined;
    const requestedRoute = {
      machine_id: machineInput ?? null, engine: engineInput ?? null, model: modelInput ?? null,
      effort: effortInput ?? null, override_reason: overrideReason ?? null,
      required_capabilities: requiredCapabilities, with_goal: withGoal,
      ...(purpose !== 'implement' ? { purpose } : {}),
    };
    const digest = await sha256Hex(JSON.stringify({
      intake_id: intakeId, criteria, workspace, prompt, interactive,with_goal:withGoal, timeout_seconds: Number(timeoutSeconds), route: requestedRoute,
    }));
    const scope = `session:start:${intakeId}`;
    const prior = this.idempotent(scope, key, digest);
    if (prior instanceof Response) return prior;
    if (prior) {
      const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', prior.operation_id as string);
      return saga ? this.startResponse(saga,200) : error('dispatch_state_unavailable', 503);
    }

    const operationHash = await sha256Hex(JSON.stringify(['hub2-start-operation-v1',intake.project,intakeId,key]));
    const operationId = `${operationHash.slice(0,8)}-${operationHash.slice(8,12)}-5${operationHash.slice(13,16)}-a${operationHash.slice(17,20)}-${operationHash.slice(20,32)}`;
    const sessionHash = await sha256Hex(JSON.stringify(['hub2-start-session-v1',intake.project,intakeId,key]));
    const sessionId = `${sessionHash.slice(0,8)}-${sessionHash.slice(8,12)}-5${sessionHash.slice(13,16)}-a${sessionHash.slice(17,20)}-${sessionHash.slice(20,32)}`;
    let route: PickedRoute;
    let diagnostics:PickDiagnostics;
    if (typeof engineInput === 'string' && engineInput.toLowerCase() === 'shell') {
      if (!hasScope(actor, 'operator:admin')) return error('forbidden', 403);
      const hostIds = new Set<string>();
      if (typeof machineInput === 'string') hostIds.add(machineInput);
      else {
        let after = '';
        do {
          const response = await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch(
            'https://control.internal/api/directory?kind=machines&limit=50' + (after ? '&after=' + encodeURIComponent(after) : ''),
            { headers: { 'x-hub2-actor': JSON.stringify(actor) } });
          if (!response.ok) return error('model_picker_data_unavailable', 503);
          const page = await response.json() as { items?: Array<{ id: string }>; next?: string | null };
          for (const item of page.items || []) if (typeof item.id === 'string') hostIds.add(item.id);
          const next = page.next || '';
          if (next && next <= after) return error('machine_directory_invalid_cursor', 503);
          after = next;
        } while (after);
      }
      const hosts = await Promise.all([...hostIds].sort().map(async machine_id => {
        const response = await this.env.MACHINES.get(this.env.MACHINES.idFromName(machine_id)).fetch('https://machine.internal/_internal/routing', {
          headers: { 'x-hub2-actor': JSON.stringify(actor) },
        });
        if (!response.ok) throw new HttpError('model_picker_data_unavailable', 503);
        const host = await response.json() as { connected?: boolean; last_seen?: number };
        return { machine_id, connected: host.connected === true, last_seen: host.last_seen ?? null,
          age_ms: typeof host.last_seen === 'number' ? Math.max(0, Date.now() - host.last_seen) : null };
      }));
      const selected = hosts.find(host => host.connected);
      if (!selected) return error('no_connected_host', 503, JSON.stringify({ machines: hosts }));
      route = {
        machine_id: selected.machine_id, engine: 'shell', model: 'shell', account_id: '', policy_id: 'operator-shell',
        policy_version: 1, override_reason: overrideReason ?? null, weight: 1, quota_remaining: 0,
        required_capabilities: [interactive ? 'interactive' : 'headless', ...requiredCapabilities],
      };
      diagnostics={pool:[{policy_id:route.policy_id,engine:route.engine,model:route.model,
        effort:route.effort??null,weight:1,share:1}],excluded:[]};
    } else {
      const picked = await pickCommittedModelRoute(this.env, {
          ...(typeof machineInput === 'string' ? { machine_id: machineInput } : {}),
          ...(typeof engineInput === 'string' ? { engine: engineInput } : {}),
          ...(typeof modelInput === 'string' ? { model: modelInput } : {}),
          ...(typeof effortInput === 'string' ? { effort: effortInput } : {}),
          ...(typeof body.tier === 'string' ? { tier: body.tier } : {}),
          ...(overrideReason ? { override_reason: overrideReason } : {}),
          interactive, purpose, with_goal: withGoal, required_capabilities: requiredCapabilities, workspace,
        }, {project_id:intake.project,operation_id:operationId,attempt:0});
      route=picked.route;diagnostics=picked.diagnostics;
    }
    const picker=buildStartPickerEvidence(route,diagnostics);
    const raced = this.idempotent(scope, key, digest);
    if (raced instanceof Response) return raced;
    if (raced) {
      const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', raced.operation_id as string);
      return saga ? this.startResponse(saga,200) : error('dispatch_state_unavailable', 503);
    }
    const accountId = route.engine.toLowerCase() === 'shell' ? null : route.account_id;
    const launchSpec = {
      workspace, engine: route.engine, model: route.model, ...(route.effort ? { effort: route.effort } : {}),
      prompt, interactive, purpose, with_goal:withGoal, timeout_seconds: Number(timeoutSeconds), start_deadline_at: Date.now()+120_000,
      ...(route.engine.toLowerCase() === 'shell' ? { shell_reason: overrideReason } : {}),
    };
    const now = Date.now();
    const machineId = route.machine_id;
    const cap = await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
      body: JSON.stringify({ operation_id: operationId, session_id: sessionId }),
    });
    if (!cap.ok) return cap;
    try {
      this.transaction(() => {
        const current = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', intakeId);
        if (!current || (current.status !== 'open' && !(current.status === 'queued' && current.session_id === null)&&!this.retryableFailure(current)))
          throw new HttpError('intake_already_assigned', 409, !current ? undefined : ['queued','running'].includes(current.status)||current.status==='failed'
            ? `intake is ${current.status} with session ${current.session_id}; stop it first (hub sessions stop ${current.session_id}), then start again on this intake`
            : `intake is ${current.status}; reopen it first (hub intake update ${current.id} --status open), then start again on this intake`);
        this.db.run(`INSERT INTO dispatch_sagas(operation_id, request_digest, request_key, intake_id, session_id, machine_id, account_id, attempt_operation_id, launch_spec_json, routing_json, epoch, state, attempt, next_action_at, last_reason, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending_machine', 0, ?, NULL, ?, ?)`, operationId, digest, key, intakeId, sessionId, machineId, accountId, `${operationId}:0`, JSON.stringify(launchSpec), JSON.stringify({...route,attempted_policy_ids:[route.policy_id],picker,initial_picker:picker}), now + 30_000, now, now);
        this.db.run("UPDATE intakes SET status = 'queued', assigned_machine_id = ?, assigned_actor_id = ?, session_id = ?, final_evidence_json=NULL, updated_at = ? WHERE id = ?", machineId, actor?.id || 'operator', sessionId, now, intakeId);
        this.appendEvent(intakeId, `start:${operationId}:requested`, actor?.id || 'operator', 'session_start_requested', {
          operation_id: operationId, session_id: sessionId, machine_id: machineId, account_id: accountId, routing_decision: route,...(current.status==='failed'?{retry_of_session:current.session_id}:{}),
        }, now);
        this.db.run('INSERT INTO idempotency(scope, request_key, request_digest, response_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
          scope, key, digest, JSON.stringify({ operation_id: operationId }), now, now + 180 * 24 * 60 * 60_000);
      });
    } catch (caught) {
      await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operation_id: operationId, session_id: sessionId }),
      }).catch(() => undefined);
      if (caught instanceof HttpError) return error(caught.code, caught.status, caught.detail);
      return error('dispatch_persist_failed', 503);
    }
    return operationId;
  }

  private async dispatchSaga(operationId: string): Promise<boolean> {
    try {
      await this.dispatchSagaEffects(operationId, this.env);
      return true;
    } catch (caught) {
      if (!(caught instanceof HttpError)) console.error('[hub2] dispatch saga failed:', caught);
      const current = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
      if (current && ['pending_machine', 'staged'].includes(current.state)) await this.deferSaga(current, 'dispatch_unavailable');
      await this.scheduleNext();
      return false;
    }
  }

  private async dispatchSagaEffects(operationId: string, env: Hub2Env): Promise<void> {
    const initial = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
    if (!initial || !['pending_machine', 'staged'].includes(initial.state)) return;
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', initial.intake_id);
    if (!intake) {
      this.db.run("UPDATE dispatch_sagas SET state='needs_attention', next_action_at=NULL, last_reason='intake_missing', updated_at=? WHERE operation_id=?", Date.now(), operationId);
      return;
    }
    try {
      const participants=[intake.owner_actor_id,intake.assigned_actor_id].filter((v):v is string=>!!v);
      if (!initial.route_registered&&!await this.registerControlRoute('session', initial.session_id, initial.machine_id, intake.project, participants,intake.id, env)) {
        await this.deferSaga(initial, 'session_route_registration_pending');
        return;
      }
      if(!initial.route_registered)this.db.run('UPDATE dispatch_sagas SET route_registered=1 WHERE operation_id=?',initial.operation_id);
    } catch {
      await this.deferSaga(initial, 'session_route_registration_uncertain');
      return;
    }
    for (let step = 0; step < 3; step++) {
      const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
      if (!saga || !['pending_machine', 'staged'].includes(saga.state)) return;
      try {
        const outcome = await advanceDispatch(env, {
          state: saga.state as 'pending_machine' | 'staged',
          operation_id: saga.operation_id, attempt_operation_id: saga.attempt_operation_id,
          dispatch_id: saga.operation_id, attempt: saga.attempt,
          session_id: saga.session_id, machine_id: saga.machine_id,
          account_id: saga.account_id, epoch: saga.epoch,
          request_digest: saga.request_digest, project_id: intake.project, intake_id: intake.id,
          title: intake.title, acceptance_criteria: this.parse(intake.acceptance_criteria_json),
          launch: this.parse(saga.launch_spec_json),
          picker: normalizeStartPickerEvidence((this.parse(saga.routing_json) as JsonRecord|null)?.picker),
        });
        if (!outcome.ok) { await this.deferSaga(saga, outcome.reason); return; }
        const now = Date.now();
        if (outcome.state === 'staged') {
          this.transaction(() => {
            this.db.run("UPDATE dispatch_sagas SET state='staged', epoch=?, retry_count=0, next_action_at=?, last_reason=NULL, updated_at=? WHERE operation_id=? AND state='pending_machine'",
              outcome.epoch, now + 30_000, now, operationId);
            this.appendEvent(intake.id, `start:${operationId}:staged`, 'hub2-dispatch', 'session_staged', { session_id: saga.session_id, machine_id: saga.machine_id, epoch: outcome.epoch }, now);
          });
          continue;
        }
        this.transaction(() => {
          this.db.run("UPDATE dispatch_sagas SET state='offered', retry_count=0, next_action_at=NULL, last_reason=NULL, updated_at=? WHERE operation_id=? AND state='staged'", now, operationId);
          this.appendEvent(intake.id, `start:${operationId}:offered`, 'hub2-dispatch', 'session_offered', { session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch }, now);
        });
        return;
      } catch {
        await this.deferSaga(saga, 'dispatch_step_uncertain');
        return;
      }
    }
  }

  private async failDispatch(saga: SagaRow, reason: string): Promise<void> {
    const now=Date.now();
    const applied=this.transaction(()=>{
      const current=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
      if(!current||current.state==='complete'||current.native_id)return false;
      this.db.run("UPDATE dispatch_sagas SET state='complete',next_action_at=NULL,last_reason=?,updated_at=? WHERE operation_id=?",reason,now,saga.operation_id);
      this.db.run("UPDATE intakes SET status='failed',final_evidence_json=?,updated_at=? WHERE id=? AND session_id=?",
        JSON.stringify({launch_failed:true,native_id_absent:true,reason,session_id:saga.session_id,operation_id:saga.operation_id}),now,saga.intake_id,saga.session_id);
      this.appendEvent(saga.intake_id,`dispatch-failed:${saga.attempt_operation_id}`, 'hub2-dispatch','session_launch_failed',
        {session_id:saga.session_id,reason},now);
      return true;
    });
    if(!applied)return;
    await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:saga.operation_id,session_id:saga.session_id}),
    }).catch(()=>undefined);
    console.error('[hub2] DISPATCH_FAILED',JSON.stringify({session_id:saga.session_id,reason}));
  }

  private async deferSaga(saga: SagaRow, reason: string): Promise<void> {
    const now = Date.now();
    const retryCount = saga.retry_count + 1;
    if(retryCount>=10||now-saga.created_at>=120_000){
      await this.failDispatch(saga,now-saga.created_at>=120_000?'host_no_response':'dispatch_retries_exhausted');
      return;
    }
    const delayMs = Math.min(15 * 60_000, 1_000 * 2 ** Math.min(retryCount, 9));
    this.transaction(() => {
      this.db.run('UPDATE dispatch_sagas SET retry_count=?, next_action_at=?, last_reason=?, updated_at=? WHERE operation_id=? AND state=?',
        retryCount, Math.min(now + delayMs,saga.created_at+120_000), reason.slice(0, 100), now, saga.operation_id, saga.state);
    });
  }

  private async deferSagaUntil(operationId: string, retryAt: number, reason: string): Promise<void> {
    const now = Date.now();
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
    if (!saga || !['pending_machine', 'staged'].includes(saga.state)) return;
    this.transaction(() => {
      this.db.run('UPDATE dispatch_sagas SET next_action_at=?,last_reason=?,updated_at=? WHERE operation_id=? AND state=?',
        retryAt, reason.slice(0, 100), now, operationId, saga.state);
    });
  }


  private async requeueInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string'
      || typeof body.session_id !== 'string' || typeof body.machine_id !== 'string'
      || !Number.isSafeInteger(body.epoch) || !Number.isSafeInteger(body.offer_expires_at)
      || !Number.isSafeInteger(body.observed_at) || Number(body.observed_at) < Number(body.offer_expires_at)
      || body.proof_kind !== 'offer-expired-unclaimed') return error('invalid_unclaimed_offer_proof', 400);
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch || saga.state !== 'offered') {
      return error('stale_offer_expiry', 409);
    }
    await this.failDispatch(saga,'host_no_response');
    await this.scheduleNext();
    return json({ok:true,retry_scheduled:false,failed:true,reason:'host_no_response'});
  }

  private async claimInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 8192);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string'
      || typeof body.session_id !== 'string' || typeof body.machine_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_claim', 400);
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch) return error('stale_claim', 409);
    const binding=this.workspaceBinding(saga,body.workspace_binding);
    if (saga.state==='complete' && saga.last_reason==='host_inventory_stale') return json({claimed:false,lost:true,duplicate:true});
    if (saga.state === 'claimed' || saga.state === 'admitted' || saga.state === 'running') {
      this.transaction(()=>this.pinWorkspaceBinding(saga,binding));
      return json({ claimed: true, duplicate: true });
    }
    if (saga.state !== 'offered') return error('offer_not_claimable', 409);
    const now = Date.now();
    this.transaction(() => {
      this.pinWorkspaceBinding(saga,binding);
      this.db.run("UPDATE dispatch_sagas SET state='admitted', next_action_at=NULL, updated_at=? WHERE operation_id=? AND state='offered' AND attempt_operation_id=? AND epoch=?",
        now, saga.operation_id, saga.attempt_operation_id, saga.epoch);
      this.appendEvent(saga.intake_id, `claim:${saga.attempt_operation_id}:${saga.epoch}`, `machine:${saga.machine_id}`, 'session_claimed',
        { session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch }, now);
    });
    return json({ claimed: true });
  }

  private async legacyLaunchUnobserved(body:JsonRecord,reaped:boolean):Promise<Response>{
    if(!reaped||body.launch_failed!==true||body.native_outcome!=='unconfirmed'||body.failure_kind!=='legacy_census_absent'
      ||body.native_id!==null||body.native_exit_confirmed!==undefined||body.no_dispatch!==undefined||body.exit_code!==undefined
      ||typeof body.operation_id!=='string'||typeof body.attempt_operation_id!=='string'||typeof body.session_id!=='string'
      ||typeof body.machine_id!=='string'||!Number.isSafeInteger(body.epoch)
      ||typeof body.evidence_digest!=='string'||!/^[0-9a-f]{64}$/.test(body.evidence_digest))
      return error('invalid_legacy_launch_failure',400);
    const evidence=body.evidence;
    if(!evidence||typeof evidence!=='object'||Array.isArray(evidence)
      ||(evidence as JsonRecord).source!=='aplexer_all_registry_census'||(evidence as JsonRecord).complete!==true
      ||(evidence as JsonRecord).match_count!==0||(evidence as JsonRecord).dispatch_provenance!=='legacy_unknown')
      return error('invalid_legacy_census',400);
    const saga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',body.operation_id);
    if(!saga||saga.session_id!==body.session_id||saga.intake_id!==body.intake_id)return error('stale_legacy_launch_failure',409);
    const exact=saga.attempt_operation_id===body.attempt_operation_id&&saga.epoch===body.epoch&&saga.machine_id===body.machine_id;
    const attemptText=body.attempt_operation_id.startsWith(saga.operation_id+':')?body.attempt_operation_id.slice(saga.operation_id.length+1):'';
    const olderAttempt=!exact&&/^(0|[1-9][0-9]*)$/.test(attemptText)&&Number(attemptText)<saga.attempt;
    if(!exact&&!olderAttempt||exact&&saga.native_id!==null)return error('stale_legacy_launch_failure',409);
    const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',saga.intake_id);
    if(!intake)return error('intake_not_found',404);
    const terminalObservation=exact&&saga.state==='complete'
      &&['legacy_launch_unobserved','host_no_response','launch_timeout_no_native','host_launch_refused','stop_confirmed','dead','idle'].includes(String(saga.last_reason));
    const intakeMatches=intake.session_id===saga.session_id&&intake.assigned_machine_id===saga.machine_id;
    const historical=olderAttempt||!intakeMatches||terminalObservation;
    const key='legacy-unobserved:'+body.attempt_operation_id+':'+body.epoch;
    const accepted=this.db.one<{body_json:string}>('SELECT body_json FROM intake_events WHERE task_id=? AND event_key=?',saga.intake_id,key);
    const binding=exact?this.workspaceBinding(saga,body.workspace_binding):undefined;
    if(!accepted){
      const launch=this.parse(saga.launch_spec_json) as JsonRecord|null;
      if(!exact||!launch||!boundedString(launch.workspace,1024)||boundWorkspace(launch,binding)!==(evidence as JsonRecord).workspace)
        return error('legacy_launch_workspace_unverified',409);
    }
    const payload={session_id:body.session_id,machine_id:body.machine_id,attempt_operation_id:body.attempt_operation_id,
      epoch:body.epoch,reason:'legacy_launch_unobserved',launch_failed:true,native_outcome:'unconfirmed',
      failure_kind:'legacy_census_absent',native_id:null,evidence,evidence_digest:body.evidence_digest,historical};
    const encoded=JSON.stringify(payload),now=Date.now();
    this.transaction(()=>{
      const current=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
      if(!current||current.attempt_operation_id!==saga.attempt_operation_id||current.epoch!==saga.epoch||current.state!==saga.state
        ||current.machine_id!==saga.machine_id||current.native_id!==saga.native_id||current.launch_spec_json!==saga.launch_spec_json)
        throw new HttpError('stale_legacy_launch_failure',409);
      if(exact)this.pinWorkspaceBinding(saga,binding);
      const prior=this.db.one<{body_json:string}>('SELECT body_json FROM intake_events WHERE task_id=? AND event_key=?',saga.intake_id,key);
      if(prior){
        if(JSON.stringify({...this.parse(prior.body_json) as JsonRecord,historical})!==encoded)
          throw new HttpError('legacy_launch_failure_conflict',409);
        return;
      }
      if(exact){
        const active=['offered','claimed','admitted'].includes(current.state);
        if(!active&&!terminalObservation)throw new HttpError('stale_legacy_launch_failure',409);
        if(active){
          this.db.run("UPDATE dispatch_sagas SET state='complete',next_action_at=NULL,last_reason='legacy_launch_unobserved',updated_at=? WHERE operation_id=?",now,saga.operation_id);
        }
        if(!historical){
          const previous=this.parse(intake.final_evidence_json) as JsonRecord|null;
          this.db.run('UPDATE intakes SET status=?,final_evidence_json=?,updated_at=? WHERE id=?',active?'failed':intake.status,
            JSON.stringify({...previous,...payload,operation_id:saga.operation_id}),now,intake.id);
        }else if(terminalObservation&&intakeMatches){
          const previous=this.parse(intake.final_evidence_json) as JsonRecord|null;
          this.db.run('UPDATE intakes SET final_evidence_json=?,updated_at=? WHERE id=?',
            JSON.stringify({...previous,legacy_launch_recovery:payload}),now,intake.id);
        }
      }
      this.appendEvent(intake.id,key,'machine:'+body.machine_id,'session_launch_failed',payload,now);
    });
    if(exact){
      const released=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:saga.operation_id,session_id:saga.session_id})});
      if(!released.ok)return error('session_cap_release_pending',503);
    }
    await this.scheduleNext();
    return json({failed:true,native_outcome:'unconfirmed',historical,session_id:saga.session_id});
  }

  private async stoppedInternal(request: Request): Promise<Response> {
    const reaped=new URL(request.url).pathname.endsWith('/reaped');
    const body=await bodyObject(request,12_288);
    if(body.reason==='legacy_launch_unobserved')return this.legacyLaunchUnobserved(body,reaped);
    if(typeof body.operation_id!=='string'||typeof body.attempt_operation_id!=='string'||typeof body.session_id!=='string'
      ||typeof body.machine_id!=='string'||!Number.isSafeInteger(body.epoch)||!['dead','idle','stop_confirmed','launch_timeout_no_native','host_no_response','host_launch_refused','host_inventory_stale'].includes(String(body.reason)))
      return error('invalid_stopped_receipt',400);
    const launchTimedOut=['launch_timeout_no_native','host_no_response'].includes(String(body.reason));
    const launchRefused=body.reason==='host_launch_refused';
    const launchFailed=launchTimedOut||launchRefused;
    const lost=body.reason==='host_inventory_stale';
    if (lost && (!reaped || body.lost!==true || body.stopped!==undefined
      || typeof body.native_id!=='string' || body.native_id.length<8 || body.native_id.length>128
      || !Number.isSafeInteger(body.observed_at)
      || (body.inventory_observed_at!==null && (!Number.isSafeInteger(body.inventory_observed_at)
        || Number(body.observed_at)-Number(body.inventory_observed_at)<HOST_INVENTORY_LOST_MS)))) return error('invalid_lost_receipt',400);
    const saga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',body.operation_id);
    if(!saga||saga.session_id!==body.session_id||saga.machine_id!==body.machine_id)return error('stale_stopped_receipt',409);
    const exactAttempt=saga.attempt_operation_id===body.attempt_operation_id&&saga.epoch===body.epoch;
    const previousAttempt=saga.attempt>0&&body.attempt_operation_id===`${saga.operation_id}:${saga.attempt-1}`&&saga.epoch===body.epoch;
    const pendingRetryStop=!launchTimedOut&&body.reason==='stop_confirmed'&&body.native_id==null&&saga.native_id===null
      &&saga.state==='pending_machine'&&previousAttempt;
    const duplicateRetryStop=!launchTimedOut&&body.reason==='stop_confirmed'&&body.native_id==null&&saga.native_id===null
      &&saga.state==='complete'&&saga.last_reason==='stop_confirmed'&&previousAttempt;
    if(!exactAttempt&&!pendingRetryStop&&!duplicateRetryStop)return error('stale_stopped_receipt',409);
    if (lost && (!exactAttempt || (saga.native_id!==null && saga.native_id!==body.native_id))) return error('stale_lost_receipt',409);
    if (saga.state==='complete' && saga.last_reason==='host_inventory_stale') {
      if (!await this.releaseLostCapacity(saga)) return error('session_cap_release_pending',503);
      return json({lost:true,duplicate:true,session_id:saga.session_id});
    }
    if (lost && !['offered','claimed','admitted','running'].includes(saga.state)) return error('stale_lost_receipt',409);
    if(saga.state==='complete')return ['launch_timeout_no_native','host_no_response','host_launch_refused'].includes(String(saga.last_reason))
      ?json({failed:true,duplicate:true,session_id:saga.session_id,reason:saga.last_reason}):json({stopped:true,duplicate:true});
    if(launchTimedOut&&(!['staged','offered','claimed','admitted'].includes(saga.state)||saga.native_id!==null||body.native_id!==null||body.launch_failed!==true))return error('invalid_launch_timeout_receipt',409);
    if(launchRefused&&(!reaped||!['offered','claimed','admitted'].includes(saga.state)||saga.native_id!==null||body.native_id!==null||body.launch_failed!==true
      ||typeof body.failure_reason!=='string'||!/^[a-z_]{1,80}$/.test(body.failure_reason)))return error('invalid_launch_refusal_receipt',409);
    if(!['staged','offered','claimed','admitted','running'].includes(saga.state)&&!pendingRetryStop)return error('session_not_active',409);
    const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',saga.intake_id);
    if(!intake||intake.session_id!==saga.session_id||intake.assigned_machine_id!==saga.machine_id)return error('stopped_intake_binding_mismatch',409);
    const now=Date.now(),eventKey=`stopped:${saga.attempt_operation_id}:${saga.epoch}`;
    const applied=this.transaction(()=>{
      const current=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
      if(!current||current.state!==saga.state||current.attempt_operation_id!==saga.attempt_operation_id||current.epoch!==saga.epoch)return false;
      if(!['staged','offered','claimed','admitted','running'].includes(current.state)&&!pendingRetryStop)return false;
      this.db.run("UPDATE dispatch_sagas SET state='complete',native_id=COALESCE(native_id,?),next_action_at=NULL,last_reason=?,updated_at=? WHERE operation_id=? AND state=? AND attempt_operation_id=? AND epoch=?",
        typeof body.native_id==='string'?body.native_id:null,String(body.reason),now,saga.operation_id,saga.state,saga.attempt_operation_id,saga.epoch);
      this.db.run("UPDATE intakes SET status='failed',final_evidence_json=?,updated_at=? WHERE id=?",
        JSON.stringify({...(launchFailed?{launch_failed:true,native_id_absent:true}:lost?{lost:true,inventory_observed_at:body.inventory_observed_at}:{stopped:true}),...(launchRefused?{failure_reason:body.failure_reason}:{}),reaped,
          operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,
          session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,reason:body.reason,
          native_id:body.native_id??null,observed_at:body.observed_at}),now,intake.id);
      this.appendEvent(intake.id,eventKey,`machine:${saga.machine_id}`,launchFailed?'session_launch_failed':lost?'session_lost':reaped?'session_reaped':'session_stopped',
        {session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,reason:body.reason,native_id:body.native_id??null,
          ...(launchRefused?{failure_reason:body.failure_reason}:{})},now);
      return true;
    });
    if(!applied)return error('stale_stopped_receipt',409);
    if (lost) {
      await this.scheduleNext();
      if (!await this.releaseLostCapacity(saga)) return error('session_cap_release_pending',503);
      return json({lost:true,session_id:saga.session_id,reason:body.reason});
    }
    await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:saga.operation_id,session_id:saga.session_id}),
    }).catch(()=>undefined);
    await this.scheduleNext();
    return launchFailed?json({failed:true,reaped,session_id:saga.session_id,reason:body.reason,
      ...(launchRefused?{failure_reason:body.failure_reason}:{})}):json({stopped:true,reaped,session_id:saga.session_id});
  }


  private async releaseLostCapacity(saga: SagaRow): Promise<boolean> {
    try {
      const response = await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:saga.operation_id,session_id:saga.session_id}),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  private async authFallbackInternal(request:Request):Promise<Response>{
    const prepared=await this.serializeRouting(()=>this.prepareAuthFallback(request));
    if(prepared instanceof Response)return prepared;
    await this.scheduleNext();
    await this.dispatchSaga(prepared.operation_id);
    await this.scheduleNext();
    return json({fallback:true,session_id:prepared.session_id,engine:prepared.engine,model:prepared.model});
  }

  private async prepareAuthFallback(request:Request):Promise<Response|{operation_id:string;session_id:string;engine:string;model:string}>{
    const body=await bodyObject(request,4096);
    if(typeof body.operation_id!=='string'||typeof body.attempt_operation_id!=='string'||typeof body.session_id!=='string'
      ||typeof body.machine_id!=='string'||!Number.isSafeInteger(body.epoch)||typeof body.reason!=='string')return error('invalid_auth_fallback',400);
    const saga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',body.operation_id);
    if(!saga||saga.session_id!==body.session_id||saga.machine_id!==body.machine_id||saga.attempt_operation_id!==body.attempt_operation_id
      ||saga.epoch!==body.epoch||!['offered','claimed'].includes(saga.state)||saga.native_id)return error('stale_auth_fallback',409);
    const route=this.parse(saga.routing_json) as (PickedRoute&{attempted_policy_ids?:string[];picker?:unknown;initial_picker?:unknown})|null;
    const tried=[...new Set([...(route?.attempted_policy_ids||[]),route?.policy_id].filter((id):id is string=>typeof id==='string'))];
    const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',saga.intake_id);
    if(!intake||intake.session_id!==saga.session_id||intake.assigned_machine_id!==saga.machine_id)return error('fallback_intake_binding_mismatch',409);
    const fail=async(reason:string):Promise<Response>=>{
      const now=Date.now();
      const applied=this.transaction(()=>{
        if(!this.fallbackCurrent(saga,intake))return false;
        this.db.run("UPDATE dispatch_sagas SET state='complete',next_action_at=NULL,last_reason=?,updated_at=? WHERE operation_id=? AND state=? AND attempt_operation_id=? AND epoch=? AND native_id IS NULL",
          reason,now,saga.operation_id,saga.state,saga.attempt_operation_id,saga.epoch);
        this.db.run("UPDATE intakes SET status='failed',final_evidence_json=?,updated_at=? WHERE id=? AND session_id=?",
          JSON.stringify({error:'provider_auth_failed',reason,attempted_policy_ids:tried}),now,intake.id,saga.session_id);
        this.appendEvent(intake.id,`auth-fallback-exhausted:${saga.attempt_operation_id}:${saga.epoch}`,'hub2-dispatch','model_routes_auth_failed',
          {session_id:saga.session_id,attempted_policy_ids:tried,reason},now);
        return true;
      });
      if(!applied)return error('stale_auth_fallback',409);
      await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:saga.operation_id,session_id:saga.session_id}),
      }).catch(()=>undefined);
      console.error('[hub2] MODEL_ROUTE_AUTH_FAILED',JSON.stringify({session_id:saga.session_id,attempted_policy_ids:tried,reason}));
      await this.scheduleNext();
      return json({fallback:false,reason:'all_model_routes_auth_failed'});
    };
    if(saga.attempt>=1)return await fail('model_attempts_exhausted');
    if(!route||route.override_reason!==null)return await fail('explicit_route_auth_failed');
    let next:PickedRoute;
    let diagnostics:PickDiagnostics;
    try{
      const launch=this.parse(saga.launch_spec_json) as JsonRecord;
      const picked=await pickCommittedModelRoute(this.env,{interactive:launch.interactive===true,
        ...(typeof launch.workspace==='string'?{workspace:launch.workspace}:{}),
        purpose: sessionPurpose(launch.purpose),
        with_goal:launch.with_goal===true,required_capabilities:route.required_capabilities,exclude_policy_ids:tried},
        {project_id:intake.project,operation_id:saga.operation_id,attempt:1,previous_picker:route.picker});
      next=picked.route;diagnostics=picked.diagnostics;
    }catch(caught){
      if(caught instanceof HttpError&&caught.code==='model_policy_unavailable')return await fail('all_model_routes_auth_failed');
      if(caught instanceof HttpError)return error(caught.code,caught.status);
      return error('model_picker_data_unavailable',503);
    }
    const launch=this.parse(saga.launch_spec_json) as JsonRecord,nextAttempt=saga.attempt+1,now=Date.now();
    const launchSpec:JsonRecord={...launch,engine:next.engine,model:next.model,...(next.effort?{effort:next.effort}:{})};
    if(!next.effort)delete launchSpec.effort;
    const picker=buildStartPickerEvidence(next,diagnostics,1,route.picker);
    const routeState={...next,attempted_policy_ids:[...tried,next.policy_id],picker,
      initial_picker:normalizeStartPickerEvidence(route.initial_picker)??normalizeStartPickerEvidence(route.picker)};
    const applied=this.transaction(()=>{
      if(!this.fallbackCurrent(saga,intake))return false;
      this.db.run(`UPDATE dispatch_sagas SET state='pending_machine',attempt=?,attempt_operation_id=?,machine_id=?,account_id=?,
        launch_spec_json=?,routing_json=?,next_action_at=?,last_reason='provider_auth_fallback',updated_at=?
        WHERE operation_id=? AND state=? AND attempt_operation_id=? AND epoch=? AND native_id IS NULL`,
        nextAttempt,`${saga.operation_id}:${nextAttempt}`,next.machine_id,next.account_id,JSON.stringify(launchSpec),JSON.stringify(routeState),now,now,
        saga.operation_id,saga.state,saga.attempt_operation_id,saga.epoch);
      this.db.run('UPDATE intakes SET assigned_machine_id=?,updated_at=? WHERE id=? AND session_id=? AND assigned_machine_id=?',
        next.machine_id,now,intake.id,saga.session_id,saga.machine_id);
      this.appendEvent(intake.id,`auth-fallback:${saga.attempt_operation_id}:${saga.epoch}`,'hub2-dispatch','model_route_auth_fallback',
        {session_id:saga.session_id,failed_policy_id:route.policy_id,next_policy_id:next.policy_id,engine:next.engine,model:next.model},now);
      return true;
    });
    if(!applied)return error('stale_auth_fallback',409);
    return {operation_id:saga.operation_id,session_id:saga.session_id,engine:next.engine,model:next.model};
  }

  private fallbackCurrent(saga:SagaRow,intake:IntakeRow):boolean{
    // The actual picker awaits namespace reads. A claim/stop may commit during
    // that await; fence the receipt and intake again inside the write transaction.
    // No await separates this check from UPDATE, so its predicates remain true
    // until commit. MeteredDb.run's cumulative billing counters are not a CAS API.
    const current=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
    const assigned=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intake.id);
    return !!current&&current.state===saga.state&&current.epoch===saga.epoch
      &&current.attempt_operation_id===saga.attempt_operation_id&&current.machine_id===saga.machine_id
      &&current.session_id===saga.session_id&&current.native_id===null&&!!assigned
      &&assigned.session_id===saga.session_id&&assigned.assigned_machine_id===saga.machine_id;
  }

  private async admissionInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.session_id !== 'string'
      || typeof body.machine_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_admission', 400);
    const saga = this.sagaByAttempt(String(body.operation_id));
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id || saga.epoch !== body.epoch
      || !['offered', 'claimed', 'admitted', 'running'].includes(saga.state)) return error('stale_or_unoffered_admission', 409);
    const launch = this.parse(saga.launch_spec_json) as JsonRecord;
    const now = Date.now();
    // Only the current saga binding gates launch; quota and credential health
    // are handled by the model picker and host authentication path.
    const stillCurrent = this.transaction(() => {
      const current = this.sagaByAttempt(saga.attempt_operation_id);
      if (!current || current.operation_id !== saga.operation_id || current.session_id !== saga.session_id
        || current.machine_id !== saga.machine_id || current.epoch !== saga.epoch
        || !['offered', 'claimed', 'admitted', 'running'].includes(current.state)) return false;
      const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', current.intake_id);
      if (!intake || intake.session_id !== current.session_id || intake.assigned_machine_id !== current.machine_id) return false;
      if (current.state === 'running') return intake.status === 'running';
      if (!['offered', 'claimed', 'admitted'].includes(current.state) || intake.status !== 'queued') return false;
      if (current.state !== 'admitted') this.db.run("UPDATE dispatch_sagas SET state='admitted', next_action_at=NULL, updated_at=? WHERE operation_id=? AND state=? AND attempt_operation_id=? AND epoch=?",
        now, current.operation_id, current.state, current.attempt_operation_id, current.epoch);
      const admitted = this.sagaByAttempt(saga.attempt_operation_id);
      const queued = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', current.intake_id);
      return admitted?.operation_id === saga.operation_id && admitted.state === 'admitted'
        && admitted.session_id === saga.session_id && admitted.machine_id === saga.machine_id && admitted.epoch === saga.epoch
        && queued?.session_id === saga.session_id && queued.assigned_machine_id === saga.machine_id && queued.status === 'queued';
    });
    if (!stillCurrent) return error('stale_or_unoffered_admission', 409);
    return json({ launch_allowed: true, epoch: saga.epoch, engine: launch.engine, model: launch.model ?? null, observed_at: now });
  }

  private async startedInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 8192);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string'
      || typeof body.session_id !== 'string' || typeof body.machine_id !== 'string'
      || typeof body.native_id !== 'string' || body.native_id.length<8 || body.native_id.length>128
      || !Number.isSafeInteger(body.epoch)) return error('invalid_started_receipt', 400);
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch) return error('stale_started_receipt', 409);
    const binding=this.workspaceBinding(saga,body.workspace_binding);
    if (saga.state==='complete' && saga.last_reason==='host_inventory_stale' && saga.native_id===body.native_id) {
      return json({started:false,lost:true,duplicate:true});
    }
    if (!/^[0-9a-f-]{36}$/i.test(body.native_id)) return error('invalid_started_receipt',400);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', saga.intake_id);
    if (!intake || intake.session_id !== saga.session_id || intake.assigned_machine_id !== saga.machine_id) return error('started_intake_binding_mismatch', 409);
    if (saga.state === 'running') {
      if(saga.native_id!==body.native_id||intake.status!=='running')return error('native_identity_conflict',409);
      this.transaction(()=>this.pinWorkspaceBinding(saga,binding));
      return json({ started: true, duplicate: true });
    }
    if (saga.state !== 'admitted' || intake.status !== 'queued') return error('session_not_admitted', 409);
    const now = Date.now();
    this.transaction(() => {
      this.pinWorkspaceBinding(saga,binding);
      this.db.run("UPDATE dispatch_sagas SET state='running', native_id=?, updated_at=? WHERE operation_id=? AND state='admitted' AND attempt_operation_id=? AND epoch=?",
        body.native_id, now, saga.operation_id, saga.attempt_operation_id, saga.epoch);
      this.db.run("UPDATE intakes SET status='running', updated_at=? WHERE id=? AND session_id=? AND assigned_machine_id=? AND status='queued'",
        now, intake.id, saga.session_id, saga.machine_id);
      this.appendEvent(intake.id, `started:${saga.attempt_operation_id}:${saga.epoch}`, `machine:${saga.machine_id}`, 'native_session_started',
        { session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch, native_id: body.native_id }, now);
    });
    return json({ started: true, session_id: saga.session_id, state: 'running' }, 201);
  }

  private async finishInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 16_384);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string' || typeof body.session_id !== 'string'
      || typeof body.machine_id !== 'string' || typeof body.native_id !== 'string' || !Number.isSafeInteger(body.epoch)
      || !Number.isInteger(body.exit_code) || Number(body.exit_code) < -2147483648 || Number(body.exit_code) > 2147483647
      || body.native_exit_confirmed !== true) return error('invalid_completion', 400);
    if(body.launch_failed!==undefined&&typeof body.launch_failed!=='boolean')return error('invalid_completion',400);
    const launchFailed=body.launch_failed===true;
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', body.operation_id);
    const failureEvent=launchFailed&&saga?.state==='complete'&&saga.native_id===null
      ? this.db.one<{kind:string;body_json:string}>('SELECT kind,body_json FROM intake_events WHERE task_id=? AND event_key=?',
        saga.intake_id,`stopped:${saga.attempt_operation_id}:${saga.epoch}`)
        || this.db.one<{kind:string;body_json:string}>('SELECT kind,body_json FROM intake_events WHERE task_id=? AND event_key=?',
          saga.intake_id,`legacy-unobserved:${saga.attempt_operation_id}:${saga.epoch}`) : undefined;
    const failureReason=failureEvent ? (this.parse(failureEvent.body_json) as JsonRecord).reason : undefined;
    const lateLaunchFailure=failureEvent?.kind==='session_launch_failed'
      &&['launch_timeout_no_native','host_no_response','host_launch_refused','legacy_launch_unobserved'].includes(String(failureReason));
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch
      || (saga.native_id !== body.native_id&&!(launchFailed&&(saga.state==='admitted'||lateLaunchFailure)&&saga.native_id===null&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.native_id)))) return error('dispatch_not_found', 404);
    const binding=this.workspaceBinding(saga,body.workspace_binding);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', saga.intake_id);
    if (!intake) return error('intake_not_found', 404);
    const alreadyFinished = saga.state === 'complete'&&!lateLaunchFailure;
    if (!alreadyFinished&&!lateLaunchFailure && ((launchFailed?saga.state!=='admitted':saga.state!=='running')||intake.session_id!==saga.session_id||intake.assigned_machine_id!==saga.machine_id)) return error('dispatch_not_found', 404);
    const evidence = body.evidence && typeof body.evidence === 'object' && !Array.isArray(body.evidence) ? body.evidence as JsonRecord : undefined;
    if (!evidence) return error('native_exit_evidence_required', 400);
    const normalizedExit = evidence.native_exit === undefined ? undefined : nativeReturncode(evidence.native_exit);
    if (evidence.native_exit !== undefined && (normalizedExit === undefined || normalizedExit !== body.exit_code)) return error('native_exit_code_mismatch', 400);
    if(launchFailed){
      const identity=evidence.native_identity as JsonRecord|undefined,exit=evidence.native_exit as JsonRecord|undefined,launch=this.parse(saga.launch_spec_json) as JsonRecord;
      if(!['aplexer_status','aplexer_retirement'].includes(String(evidence.source))||evidence.containment_empty!==true
        ||identity?.id!==body.native_id||identity?.tag!=='task-hub2-'+saga.session_id||identity?.workspace!==boundWorkspace(launch,binding)||normalizedExit!==body.exit_code)return error('invalid_launch_failure_proof',400);
    }
    const completionDigest=await sha256Hex(JSON.stringify({operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:body.native_id,exit_code:body.exit_code,evidence,...(launchFailed?{launch_failed:true}:{})}));
    const replay=()=>{
      const event=this.db.one<{body_json:string}>('SELECT body_json FROM intake_events WHERE task_id=? AND event_key=?',intake.id,'finish:'+saga.operation_id);
      if(!event||(this.parse(event.body_json) as JsonRecord)?.completion_digest!==completionDigest)throw new HttpError('completion_proof_conflict',409);
    };
    if(alreadyFinished){
      replay();
      this.transaction(()=>this.pinWorkspaceBinding(saga,binding));
      await this.releaseSessionCap(saga.operation_id,saga.session_id);
      return json({ok:true,duplicate:true});
    }

    const now = Date.now();
    const nativeDeadline = evidence.native_deadline as JsonRecord | undefined;
    const deadlineExceeded = nativeDeadline?.exceeded === true;
    const finishedStatus = body.exit_code === 0 && !launchFailed && !deadlineExceeded ? 'completed-awaiting-review' : 'failed';
    const duplicate=this.transaction(() => {
      this.pinWorkspaceBinding(saga,binding);
      const currentSaga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
      if(currentSaga?.state==='complete'&&currentSaga.native_id!==null){replay();return true;}
      const current=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intake.id);
      if(lateLaunchFailure){
        if(currentSaga?.state!=='complete'||currentSaga.native_id!==null||currentSaga.attempt_operation_id!==saga.attempt_operation_id||currentSaga.epoch!==saga.epoch)
          throw new HttpError('completion_binding_changed',409);
        this.appendEvent(intake.id, `finish:${saga.operation_id}`, 'hub2-host', 'finish_evidence_received',
          {session_id:saga.session_id,native_id:body.native_id,exit_code:body.exit_code,evidence,completion_digest:completionDigest,launch_failed:true},now);
        this.db.run('UPDATE dispatch_sagas SET native_id=?,updated_at=? WHERE operation_id=?',body.native_id,now,saga.operation_id);
        if(current?.status==='failed'&&current.session_id===saga.session_id&&current.assigned_machine_id===saga.machine_id){
          const previous=this.parse(current.final_evidence_json??'{}') as JsonRecord;
          this.db.run('UPDATE intakes SET final_evidence_json=?,updated_at=? WHERE id=?',
            JSON.stringify({...previous,...evidence,native_id:body.native_id,exit_code:body.exit_code,native_exit_confirmed:true,
              ...(previous.native_outcome==='unconfirmed'?{native_outcome:'exit_confirmed'}:{}),launch_failed:true,native_id_absent:false}),now,intake.id);
        }
        return false;
      }
      if((launchFailed?currentSaga?.state!=='admitted':currentSaga?.state!=='running')||current?.session_id!==saga.session_id||current.assigned_machine_id!==saga.machine_id)throw new HttpError('completion_binding_changed',409);
      this.db.run('UPDATE intakes SET status=?, final_evidence_json=?, updated_at=? WHERE id=?', finishedStatus, JSON.stringify({ ...evidence, native_id: body.native_id, exit_code: body.exit_code,native_exit_confirmed:true,...(launchFailed?{launch_failed:true}:{}) }), now, intake.id);
      this.db.run("UPDATE dispatch_sagas SET state='complete', native_id=?, next_action_at=NULL, last_reason=NULL, updated_at=? WHERE operation_id=?", body.native_id, now, saga.operation_id);
      this.appendEvent(intake.id, `finish:${saga.operation_id}`, 'hub2-host', 'finish_evidence_received', { session_id: saga.session_id, native_id: body.native_id, exit_code: body.exit_code, evidence,completion_digest:completionDigest,...(launchFailed?{launch_failed:true}:{}) }, now);
      return false;
    });
    await this.scheduleNext();
    await this.releaseSessionCap(saga.operation_id,saga.session_id);
    return json({ ok: true, intake_id: intake.id,...(duplicate?{duplicate:true}:{}) });
  }

  private async releaseSessionCap(operationId:string,sessionId:string):Promise<void>{
    await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_internal/session-cap/release',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:operationId,session_id:sessionId}),
    }).catch(()=>undefined);
  }

  private retryableFailure(intake:IntakeRow):boolean{
    if(intake.status!=='failed')return false;
    // Keep the active-session fence, but do not require a special failure proof.
    const prior=intake.session_id?this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE session_id=?',intake.session_id):undefined;
    return !prior||prior.state==='complete';
  }

  private workAuthorized(actor:Actor,intakeId:string):boolean{
    const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intakeId);
    return !!intake&&(this.canProject(actor,intake.project,'intake:read')||this.canReadIntake(actor,intake));
  }
  private async registerControlRoute(kind:string,id:string,owner:string,project:string,participants:string[],intakeId?:string,env:Hub2Env=this.env):Promise<boolean>{
    const response=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://control.internal/_routes/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,id,owner,resource_id:project,participants:[...new Set(participants)],...(intakeId?{bound_intake_id:intakeId}:{})})});
    return response.ok;
  }
  private async registerIntakeRoute(intake:IntakeRow):Promise<boolean>{
    try{
      const participants=[intake.owner_actor_id,intake.assigned_actor_id].filter((v):v is string=>!!v);
      const registered=await this.registerControlRoute('intake',intake.id,intake.project,intake.project,participants);
      if(!registered){
        this.db.run('UPDATE intakes SET route_next_action_at=? WHERE route_registered=0 AND route_next_action_at<=?',Date.now()+30000,Date.now());
        return false;
      }
      this.db.run('UPDATE intakes SET route_registered=1 WHERE id=?',intake.id);return true;
    }catch{
      this.db.run('UPDATE intakes SET route_next_action_at=? WHERE id=? AND route_registered=0',Date.now()+30000,intake.id);return false;
    }
  }
  private async retryPendingIntakeRoutes(now=Date.now()):Promise<void>{
    for(const row of this.db.all<IntakeRow>('SELECT * FROM intakes WHERE route_registered=0 AND route_next_action_at<=? ORDER BY route_next_action_at,id LIMIT 8',now)){
      if(!await this.registerIntakeRoute(row))break;
    }
  }
  private async scheduleHasActiveSession(scheduleId:string):Promise<boolean>{
    return !!this.db.one("SELECT id FROM intakes WHERE schedule_id=? AND status IN ('queued','running') LIMIT 1",scheduleId);
  }
  private async scheduleOccurrence(record:Parameters<ProjectWorkHooks['occurrence']>[0]):Promise<{intake_id:string;session_id?:string}>{
    const parent=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',record.parent_intake_id);if(!parent)throw new Error('schedule_parent_missing');
    const actor:Actor={id:record.owner,kind:'agent',resource_id:parent.project,scopes:['session:start','intake:read','message:send','project:'+parent.project]};
    if(record.kind==='prompt'||record.kind==='wake'){
      const recipient=record.payload.recipient??(parent.session_id?'session:'+parent.session_id:null),body=record.payload.body??record.payload.prompt;
      if(typeof recipient!=='string'||typeof body!=='string'||!body.trim()||body.length>12000)throw new Error('invalid_scheduled_message');
      const response=await this.env.MAILBOXES.get(this.env.MAILBOXES.idFromName('global')).fetch('https://mailbox.internal/api/messages',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor),'x-hub2-mailbox':record.owner},body:JSON.stringify({key:record.id,recipient,body})});
      if(!response.ok)throw new Error('scheduled_message_pending');
      return {intake_id:parent.id,...(parent.session_id?{session_id:parent.session_id}:{})};
    }
    const id='scheduled:'+await sha256Hex(record.id),now=Date.now();
    this.transaction(()=>{
      this.db.run("INSERT OR IGNORE INTO intakes(id,project,title,text,acceptance_criteria_json,status,created_at,updated_at,owner_actor_id,assigned_actor_id,route_registered,route_next_action_at,parent_intake_id,schedule_id,occurrence_id,original_title,original_text,source_json) VALUES(?,?,?,?,?,'open',?,?,?,?,0,?,?,?,?,?,?,?)",id,parent.project,parent.title.slice(0,220)+' ['+record.kind+']',parent.text,parent.acceptance_criteria_json,now,now,record.owner,record.owner,now,parent.id,record.schedule_id,record.id,parent.title,parent.text,JSON.stringify({type:'schedule',schedule_id:record.schedule_id,occurrence_id:record.id,due_at:record.due_at,kind:record.kind,owner:record.owner}));
    });
    const child=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',id)!;
    if(!child.route_registered&&!await this.registerIntakeRoute(child))throw new Error('scheduled_route_pending');
    const launch=record.payload.start&&typeof record.payload.start==='object'?record.payload.start as JsonRecord:record.payload;
    if(launch.engine==='shell')actor.scopes.push('operator:admin'); // Shell schedules require operator authority at creation.
    const response=await this.startIntake(new Request('https://project.internal/api/intakes/'+encodeURIComponent(id)+'/start',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},body:JSON.stringify({...launch as object,key:record.id})}),id);
    if(!response.ok)throw new Error('scheduled_start_pending');
    const result=await response.json() as {session?:{id?:string}};if(!result.session?.id)throw new Error('scheduled_session_missing');
    return {intake_id:id,session_id:result.session.id};
  }
  private sagaByAttempt(attempt:string):SagaRow|undefined{
    const split=attempt.lastIndexOf(':');if(split<1)return;
    const row=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',attempt.slice(0,split));return row?.attempt_operation_id===attempt?row:undefined;
  }

  private async scheduleNext(): Promise<void> {
    const deadlines = [
      this.db.one<{ due: number }>('SELECT next_action_at AS due FROM dispatch_sagas WHERE due=1 ORDER BY next_action_at,operation_id LIMIT 1')?.due,
      this.db.one<{ due: number }>('SELECT route_next_action_at AS due FROM intakes WHERE route_registered=0 ORDER BY route_next_action_at,id LIMIT 1')?.due,
      this.work.nextDeadline() ?? undefined,
    ].filter((value): value is number => typeof value === 'number' && Number.isSafeInteger(value));
    const due = deadlines.length ? Math.min(...deadlines) : null;
    this.db.alarmOperation();
    const current = await this.ctx.storage.getAlarm();
    if (due !== null) {
      if (current !== due) { this.db.alarmOperation(); await this.ctx.storage.setAlarm(due); }
    } else if (current !== null) {
      this.db.alarmOperation();
      await this.ctx.storage.deleteAlarm();
    }
  }

  private transaction<T>(callback:()=>T):T{
    if(this.pendingSequences)throw new Error('nested_project_transaction');
    const pending=new Map<string,number>();this.pendingSequences=pending;
    try{
      const result=this.db.transaction(this.ctx,callback);
      for(const [id,seq] of pending){this.eventSequences.delete(id);this.eventSequences.set(id,seq);}
      while(this.eventSequences.size>256)this.eventSequences.delete(this.eventSequences.keys().next().value!);
      return result;
    }finally{this.pendingSequences=undefined;}
  }

  private appendEvent(taskId: string, eventKey: string, actorId: string, kind: string, body: JsonRecord, at: number) {
    const exists = this.db.one('SELECT event_key FROM intake_events WHERE event_key = ?', eventKey);
    if (exists) return;
    const prior=this.pendingSequences?.get(taskId)??this.eventSequences.get(taskId);
    const seq=prior!==undefined?prior+1:Number(this.db.one<{ next: number }>('SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM intake_events WHERE task_id = ?', taskId)?.next || 1);
    this.db.run('INSERT INTO intake_events(task_id, seq, event_key, actor_id, kind, body_json, at) VALUES (?, ?, ?, ?, ?, ?, ?)',
      taskId, seq, eventKey, actorId, kind, JSON.stringify(body), at);
    if(!this.pendingSequences)throw new Error('event_requires_transaction');
    this.pendingSequences.set(taskId,seq);
  }

  private idempotencyKey(request: Request, body: JsonRecord): string | undefined {
    const value = request.headers.get('idempotency-key') || body.key;
    return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : undefined;
  }

  private idempotent(scope: string, key: string, digest: string): JsonRecord | Response | undefined {
    const row = this.db.one<{ request_digest: string; response_json: string }>('SELECT request_digest, response_json FROM idempotency WHERE scope=? AND request_key=?', scope, key);
    if (!row) return undefined;
    if (row.request_digest !== digest) return error('idempotency_conflict', 409);
    try { return JSON.parse(row.response_json) as JsonRecord; } catch { return error('idempotency_result_unavailable', 503); }
  }

  private viewIntake(row: IntakeRow): JsonRecord {
    const saga = row.session_id ? this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE session_id = ?', row.session_id) : undefined;
    return {
      id: row.id, project: row.project, title: row.title, text: row.text,
      acceptance_criteria: this.parse(row.acceptance_criteria_json), status: row.status,
      assigned_machine_id: row.assigned_machine_id, session_id: row.session_id,
      dispatch: saga ? this.sagaView(saga) : undefined,
      priority:row.priority??2,original_ask:{title:row.original_title??row.title,text:row.original_text??row.text},provenance:this.parse(row.source_json),parent_intake_id:row.parent_intake_id??null,schedule_id:row.schedule_id??null,occurrence_id:row.occurrence_id??null,final_evidence: this.parse(row.final_evidence_json), created_at: row.created_at, updated_at: row.updated_at,
    };
  }

  private sagaView(saga: SagaRow): JsonRecord {
    const picker=normalizeStartPickerEvidence((this.parse(saga.routing_json) as JsonRecord|null)?.picker);
    return {
      id: saga.session_id, operation_id: saga.operation_id, attempt_operation_id: saga.attempt_operation_id, intake_id: saga.intake_id,
      machine_id: saga.machine_id, epoch: saga.epoch, state: saga.state, native_id: saga.native_id,
      attempt: saga.attempt, next_action_at: saga.next_action_at,
      picker,policy_id:picker?.policy_id??null,policy_version:picker?.policy_version??null,
      ...(() => {
        const launch = this.parse(saga.launch_spec_json) as JsonRecord | null;
        return launch ? { engine: launch.engine, model: launch.model, effort: launch.effort ?? null,
          workspace:boundWorkspace(launch,this.workspaceBinding(saga)),requested_workspace:launch.workspace,
          route: [launch.engine, launch.model, launch.effort ?? 'default'].join('/') } : {};
      })(),
    };
  }

  private startResponse(saga:SagaRow,status:number):Response{
    const session=this.sagaView(saga);
    return json({session,picker:session.picker},status);
  }

  private workspaceBinding(saga:SagaRow,incoming?:unknown):WorkspaceBinding|undefined {
    const launch=this.parse(saga.launch_spec_json) as JsonRecord;
    const route=this.parse(saga.routing_json) as JsonRecord|null;
    return mergeWorkspaceBinding(launch.workspace,route?.workspace_binding,incoming);
  }

  /** Existing receipt transactions pin metadata without rewriting the request. */
  private pinWorkspaceBinding(saga:SagaRow,binding:WorkspaceBinding|undefined):void {
    if(!binding)return;
    const current=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
    if(!current||current.session_id!==saga.session_id||current.machine_id!==saga.machine_id
      ||current.attempt_operation_id!==saga.attempt_operation_id||current.epoch!==saga.epoch)
      throw new HttpError('workspace_binding_fence_changed',409);
    const accepted=this.workspaceBinding(current,binding);
    const route=this.parse(current.routing_json) as JsonRecord|null;
    this.db.run('UPDATE dispatch_sagas SET routing_json=? WHERE operation_id=?',
      JSON.stringify({...route,workspace_binding:accepted}),saga.operation_id);
  }

  private parse(value: unknown): unknown {
    try { return JSON.parse(String(value ?? 'null')); } catch { return null; }
  }

  private canProject(actor: ReturnType<typeof actorFrom>, project: string, scope: string): boolean {
    if (!actor || !project) return false;
    if (hasScope(actor, 'operator:admin') || actor.scopes.includes('*')) return true;
    return hasScope(actor, scope) && (actor.resource_id === project || hasScope(actor, `project:${project}`));
  }

  private canReadIntake(actor: ReturnType<typeof actorFrom>, intake: IntakeRow): boolean {
    if (this.canProject(actor, intake.project, 'intake:read')) return true;
    return !!actor && actor.resource_id === intake.id && hasScope(actor, 'intake:read');
  }


}

/** CLI/MCP clients send criteria as one text block; store one criterion per non-empty line. */
function normalizeCriteria(body: Record<string, unknown>): void {
  if (typeof body.acceptance_criteria !== 'string') return;
  body.acceptance_criteria = body.acceptance_criteria
    .split('\n').map(line => line.replace(/^\s*(?:[-*]|\d+[.)])\s+/, '').trim()).filter(Boolean);
}
