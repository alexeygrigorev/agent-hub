import { MeteredDb, initializeSchema } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, error, hasScope, HttpError, json } from './http';
import { admitLaunch, advanceDispatch, holdWireCode, releaseAccountCapacity, type AdmissionOutcome } from './resource-manager';
import type { RoutingDecision } from './routing';
import { nativeReturncode } from './native-exit';
import { budgetedBackgroundRequest, type BudgetKind } from './budget-edge';
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
  routing_json: string | null;route_registered:number;
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
      if (url.pathname === '/_internal/requeue' && request.method === 'POST') return await this.requeueInternal(request);
      if (url.pathname === '/_internal/claim' && request.method === 'POST') return await this.claimInternal(request);
      if (url.pathname === '/_internal/admission' && request.method === 'POST') return await this.admissionInternal(request);
      if (url.pathname === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot(), constructor_ms: this.constructorMs });
      if(url.pathname==='/api/schedules'&&request.method==='POST'){
        const actor=actorFrom(request),body=await bodyObject(request.clone(),32768);
        const parent=typeof body.intake_id==='string'?this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',body.intake_id):undefined;
        const payload=body.payload as JsonRecord|undefined,launch=payload?.start as JsonRecord|undefined||payload;
        if(launch?.engine==='shell'&&!hasScope(actor,'operator:admin'))return error('shell_reason_required',403);
        if(parent&&(['root','writer'].includes(String(body.kind))?!this.canProject(actor,parent.project,'session:start'):!hasScope(actor,'operator:admin')&&!hasScope(actor,'message:send')))return error('schedule_effect_forbidden',403);
      }
      const workResponse = await this.work.fetch(request);
      if (workResponse) {
        if (!['GET', 'HEAD'].includes(request.method) && workResponse.status < 400) await this.scheduleNext();
        return workResponse;
      }
      if (url.pathname === '/api/intakes' && request.method === 'POST') return await this.createIntake(request);
      if (url.pathname === '/api/intakes' && request.method === 'GET') return this.listIntakes(request);
      const match = url.pathname.match(/^\/api\/intakes\/([^/]+)(?:\/(start|history|append))?$/);
      if (!match) return error('not_found', 404);
      const intakeId = decodeURIComponent(match[1]);
      if (match[2] === 'start' && request.method === 'POST') return await this.startIntake(request, intakeId);
      if (match[2] === 'history' && request.method === 'GET') return this.history(request, intakeId);
      if(match[2]==='append'&&request.method==='POST')return await this.appendIntake(request,intakeId);
      if (!match[2] && request.method === 'GET') return this.showIntake(request, intakeId);
      if(!match[2]&&request.method==='PATCH')return await this.editIntake(request,intakeId);
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('internal_error', 500);
    }
  }

  async alarm(): Promise<void> {
    await this.ready;
    const now = Date.now();
    const pause = await this.ctx.storage.get<number>('hub2:budget_pause_until');
    if (Number.isSafeInteger(pause) && Number(pause) > now) {
      if (await this.ctx.storage.getAlarm() !== Number(pause)) { this.db.alarmOperation(); await this.ctx.storage.setAlarm(Number(pause)); }
      return;
    }
    if (pause !== undefined) await this.ctx.storage.delete('hub2:budget_pause_until');
    const guarded = await budgetedBackgroundRequest(this.env, 'background_effect', async () => {
      await this.work.alarm(now);
      await this.retryPendingIntakeRoutes(now);
      const due = this.db.all<{ operation_id: string }>(
        `SELECT operation_id FROM dispatch_sagas WHERE due=1 AND state IN ('pending_machine','staged','account_confirmed') AND next_action_at <= ? ORDER BY next_action_at, operation_id LIMIT 20`, now,
      );
      for (const row of due) {
        if (!await this.dispatchSaga(row.operation_id, 'retry')) break;
      }
      await this.scheduleNext();
      return new Response(null, { status: 204 });
    });
    if (!guarded.ok) {
      const retryAt = await this.pauseForBudget(guarded);
      if (retryAt !== undefined) await this.scheduleNext();
    }
  }

  private async createIntake(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const project = new URL(request.url).searchParams.get('project') || '';
    if (!project || project.length > 180 || !this.canProject(actor, project, 'intake:write')) return error('forbidden', 403);
    const body = await bodyObject(request, 16_384);
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
    const actor = actorFrom(request);
    const query = new URL(request.url).searchParams;
    const project = query.get('project') || '';
    if (!this.canProject(actor, project, 'intake:read')) return error('forbidden', 403);
    const limit = Number(query.get('limit') || 25);
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) return error('invalid_limit', 400);
    const status = query.get('status');
    if (status && !['open', 'queued', 'running', 'completed-awaiting-review', 'failed', 'needs_attention', 'archived','accepted'].includes(status)) return error('invalid_status', 400);
    let cursor: [number, string] = [Number.MAX_SAFE_INTEGER, '\uffff'];
    const rawCursor = query.get('cursor');
    if (rawCursor) {
      try { cursor = JSON.parse(rawCursor) as [number, string]; } catch { return error('invalid_cursor', 400); }
      if (!Array.isArray(cursor) || cursor.length !== 2 || !Number.isSafeInteger(cursor[0]) || typeof cursor[1] !== 'string' || cursor[1].length > 80) return error('invalid_cursor', 400);
    }
    const search=query.get('q')||'';if(search.length>128)return error('invalid_search',400);
    const selected = 'id, project, title, status, assigned_machine_id, assigned_actor_id, session_id, created_at, updated_at,priority'+(search?',text,original_title,original_text':'');
    const rows = status
      ? this.db.all<IntakeRow>(`SELECT ${selected} FROM intakes WHERE project=? AND status=? AND (updated_at,id)<(?,?) ORDER BY updated_at DESC,id DESC LIMIT ?`, project, status, ...cursor, limit + 1)
      : this.db.all<IntakeRow>(`SELECT ${selected} FROM intakes WHERE project=? AND (updated_at,id)<(?,?) ORDER BY updated_at DESC,id DESC LIMIT ?`, project, ...cursor, limit + 1);
    const scanned=rows.slice(0,limit),needle=search.toLowerCase();
    const shown=search?scanned.filter(v=>[v.title,v.text,v.original_title,v.original_text].some(t=>t?.toLowerCase().includes(needle))):scanned;
    const last = scanned[scanned.length - 1];
    return json({ items: shown.map(row => ({ id: row.id, project: row.project, title: row.title, status: row.status,
      assigned_machine_id: row.assigned_machine_id, assigned_actor_id: row.assigned_actor_id, session_id: row.session_id,
      created_at: row.created_at, updated_at: row.updated_at,priority:row.priority })),filtered_page:!!search, next: rows.length > limit && last ? JSON.stringify([last.updated_at, last.id]) : null });
  }

  private showIntake(request: Request, intakeId: string, status = 200): Response {
    const actor = actorFrom(request);
    const row = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!row) return error('intake_not_found', 404);
    if (!this.canReadIntake(actor, row)) return error('forbidden', 403);
    return json({ intake: this.viewIntake(row) }, status);
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
    const actor=actorFrom(request),body=await bodyObject(request,16384),key=this.idempotencyKey(request,body);
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
    const actor = actorFrom(request);
    const body = await bodyObject(request, 16_384);
    const key = this.idempotencyKey(request, body);
    if (!key) return error('idempotency_key_required', 400);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', intakeId);
    if (!intake) return error('intake_not_found', 404);
    if (!this.canProject(actor, intake.project, 'session:start')) return error('forbidden', 403);
    if(intake.status==='failed'&&!this.qualifiedFailure(intake))return error('retry_requires_qualified_failure',409);
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
    const reasonInput = body.override_reason === undefined ? body.reason : body.override_reason;
    const prompt = typeof body.prompt === 'string' ? body.prompt : intake.text;
    const interactive = body.interactive === undefined ? false : body.interactive;
    const timeoutSeconds = body.timeout_seconds === undefined ? 1800 : body.timeout_seconds;
    if (typeof workspace !== 'string' || workspace.length > 1024
      || !(workspace.startsWith('/') || /^[A-Za-z]:\\/.test(workspace)) || workspace.includes('\0')) return error('invalid_workspace', 400);
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
    };
    const digest = await sha256Hex(JSON.stringify({
      intake_id: intakeId, criteria, workspace, prompt, interactive,with_goal:withGoal, timeout_seconds: Number(timeoutSeconds), route: requestedRoute,
    }));
    const scope = `session:start:${intakeId}`;
    const prior = this.idempotent(scope, key, digest);
    if (prior instanceof Response) return prior;
    if (prior) {
      const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', prior.operation_id as string);
      return saga ? json({ session: this.sagaView(saga) }, 200) : error('dispatch_state_unavailable', 503);
    }

    const operationId = crypto.randomUUID();
    let route: RoutingDecision;
    if (typeof engineInput === 'string' && engineInput.toLowerCase() === 'shell') {
      if (typeof machineInput !== 'string' || !hasScope(actor, 'operator:admin') || !overrideReason || overrideReason.length < 8) {
        return error('shell_reason_required', 403);
      }
      route = {
        machine_id: machineInput, engine: 'shell', model: 'shell', account_id: '', policy_id: 'operator-shell',
        policy_version: 1, override_reason: overrideReason, weight: 1, quota_remaining: 0,
        required_capabilities: [interactive ? 'interactive' : 'headless', ...requiredCapabilities],
      };
    } else {
      const selected = await budgetedBackgroundRequest(this.env, 'scheduled_start', async guardedEnv => {
      const decision = await admitLaunch(guardedEnv, {
          operation_id: operationId,
          ...(typeof machineInput === 'string' ? { machine_id: machineInput } : {}),
          ...(typeof engineInput === 'string' ? { engine: engineInput } : {}),
          ...(typeof modelInput === 'string' ? { model: modelInput } : {}),
          ...(typeof effortInput === 'string' ? { effort: effortInput } : {}),
          ...(overrideReason ? { override_reason: overrideReason } : {}),
          interactive, with_goal: withGoal, required_capabilities: requiredCapabilities,
        });
        return Response.json(decision);
      });
      if (!selected.ok) {
        await this.pauseForBudget(selected);
        return selected;
      }
      const decision = await selected.json() as AdmissionOutcome;
      if (!decision.ok) {
        // Typed Resource Manager hold before any saga exists: refuse with the
        // wire-compatible code; the next start attempt re-runs selection after
        // the bounded refresh/backoff carried by the hold kind.
        return error(holdWireCode(decision.code), 409);
      }
      route = decision.plan;
    }
    const raced = this.idempotent(scope, key, digest);
    if (raced instanceof Response) return raced;
    if (raced) {
      const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', raced.operation_id as string);
      return saga ? json({ session: this.sagaView(saga) }, 200) : error('dispatch_state_unavailable', 503);
    }
    const accountId = route.engine.toLowerCase() === 'shell' ? null : route.account_id;
    const launchSpec = {
      workspace, engine: route.engine, model: route.model, ...(route.effort ? { effort: route.effort } : {}),
      prompt, interactive,with_goal:withGoal, timeout_seconds: Number(timeoutSeconds),
      ...(route.engine.toLowerCase() === 'shell' ? { shell_reason: overrideReason } : {}),
    };
    const now = Date.now();
    const sessionId = crypto.randomUUID();
    const machineId = route.machine_id;
    try {
      this.transaction(() => {
        const current = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', intakeId);
        if (!current || (current.status !== 'open' && !(current.status === 'queued' && current.session_id === null)&&!this.qualifiedFailure(current))) throw new HttpError('intake_already_assigned', 409);
        this.db.run(`INSERT INTO dispatch_sagas(operation_id, request_digest, request_key, intake_id, session_id, machine_id, account_id, attempt_operation_id, launch_spec_json, routing_json, epoch, state, attempt, next_action_at, last_reason, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending_machine', 0, ?, NULL, ?, ?)`, operationId, digest, key, intakeId, sessionId, machineId, accountId, `${operationId}:0`, JSON.stringify(launchSpec), JSON.stringify(route), now + 30_000, now, now);
        this.db.run("UPDATE intakes SET status = 'queued', assigned_machine_id = ?, assigned_actor_id = ?, session_id = ?, final_evidence_json=NULL, updated_at = ? WHERE id = ?", machineId, actor?.id || 'operator', sessionId, now, intakeId);
        this.appendEvent(intakeId, `start:${operationId}:requested`, actor?.id || 'operator', 'session_start_requested', {
          operation_id: operationId, session_id: sessionId, machine_id: machineId, account_id: accountId, routing_decision: route,...(current.status==='failed'?{retry_of_session:current.session_id}:{}),
        }, now);
        this.db.run('INSERT INTO idempotency(scope, request_key, request_digest, response_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
          scope, key, digest, JSON.stringify({ operation_id: operationId }), now, now + 180 * 24 * 60 * 60_000);
      });
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('dispatch_persist_failed', 503);
    }
    await this.scheduleNext();
    await this.dispatchSaga(operationId);
    await this.scheduleNext();
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', operationId);
    return saga ? json({ session: this.sagaView(saga) }, ['offered', 'claimed', 'running'].includes(saga.state) ? 201 : 202) : error('dispatch_state_unavailable', 503);
  }

  private async dispatchSaga(operationId: string, kind: BudgetKind = 'scheduled_start'): Promise<boolean> {
    const result = await budgetedBackgroundRequest(this.env, kind, async guardedEnv => {
      await this.dispatchSagaEffects(operationId, guardedEnv);
      return new Response(null, { status: 204 });
    });
    if (!result.ok) {
      const retryAt = await this.pauseForBudget(result);
      if (retryAt !== undefined) await this.deferSagaUntil(operationId, retryAt, 'budget_paused');
      else {
        const current = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
        if (current && ['pending_machine', 'staged', 'account_confirmed'].includes(current.state)) await this.deferSaga(current, 'budget_dispatch_unavailable');
      }
      await this.scheduleNext();
      return false;
    }
    return true;
  }

  private async dispatchSagaEffects(operationId: string, env: Hub2Env): Promise<void> {
    const initial = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
    if (!initial || !['pending_machine', 'staged', 'account_confirmed'].includes(initial.state)) return;
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
      if (!saga || !['pending_machine', 'staged', 'account_confirmed'].includes(saga.state)) return;
      try {
        // The Resource Manager orchestrates capacity against the authoritative
        // MachineDO/AccountDO APIs; this saga only journals the returned step.
        const outcome = await advanceDispatch(env, {
          state: saga.state as 'pending_machine' | 'staged' | 'account_confirmed',
          operation_id: saga.operation_id, attempt_operation_id: saga.attempt_operation_id,
          dispatch_id: saga.operation_id, attempt: saga.attempt,
          session_id: saga.session_id, machine_id: saga.machine_id,
          account_id: saga.account_id, epoch: saga.epoch,
          request_digest: saga.request_digest, project_id: intake.project, intake_id: intake.id,
          title: intake.title, acceptance_criteria: this.parse(intake.acceptance_criteria_json),
          launch: this.parse(saga.launch_spec_json),
        });
        if (!outcome.ok) { await this.deferSaga(saga, outcome.reason); return; }
        const now = Date.now();
        if (outcome.state === 'staged') {
          this.transaction(() => {
            this.db.run("UPDATE dispatch_sagas SET state='staged', epoch=?, retry_count=0, next_action_at=?, last_reason=NULL, updated_at=? WHERE operation_id=? AND state='pending_machine'",
              outcome.epoch, now + 30_000, now, operationId);
            this.appendEvent(intake.id, `start:${operationId}:staged`, 'hub2-dispatch', 'machine_slot_staged', { session_id: saga.session_id, machine_id: saga.machine_id, epoch: outcome.epoch }, now);
          });
          continue;
        }
        if (outcome.state === 'account_confirmed') {
          this.transaction(() => {
            this.db.run("UPDATE dispatch_sagas SET state='account_confirmed', retry_count=0, next_action_at=?, last_reason=NULL, updated_at=? WHERE operation_id=? AND state='staged'",
              now + 30_000, now, operationId);
          });
          continue;
        }
        this.transaction(() => {
          this.db.run("UPDATE dispatch_sagas SET state='offered', retry_count=0, next_action_at=NULL, last_reason=NULL, updated_at=? WHERE operation_id=? AND state='account_confirmed'", now, operationId);
          this.appendEvent(intake.id, `start:${operationId}:offered`, 'hub2-dispatch', 'session_offered', { session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch }, now);
        });
        return;
      } catch {
        await this.deferSaga(saga, 'dispatch_step_uncertain');
        return;
      }
    }
  }

  private async deferSaga(saga: SagaRow, reason: string): Promise<void> {
    const now = Date.now();
    const retryCount = saga.retry_count + 1;
    const delayMs = Math.min(15 * 60_000, 1_000 * 2 ** Math.min(retryCount, 9));
    this.transaction(() => {
      this.db.run('UPDATE dispatch_sagas SET retry_count=?, next_action_at=?, last_reason=?, updated_at=? WHERE operation_id=? AND state=?',
        retryCount, now + delayMs, reason.slice(0, 100), now, saga.operation_id, saga.state);
    });
  }

  private async deferSagaUntil(operationId: string, retryAt: number, reason: string): Promise<void> {
    const now = Date.now();
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', operationId);
    if (!saga || !['pending_machine', 'staged', 'account_confirmed'].includes(saga.state)) return;
    this.transaction(() => {
      this.db.run('UPDATE dispatch_sagas SET next_action_at=?,last_reason=?,updated_at=? WHERE operation_id=? AND state=?',
        retryAt, reason.slice(0, 100), now, operationId, saga.state);
    });
  }

  private async pauseForBudget(response: Response): Promise<number | undefined> {
    let payload: { error?: unknown; retry_at?: unknown };
    try { payload = await response.clone().json() as { error?: unknown; retry_at?: unknown }; }
    catch { return undefined; }
    if (payload.error !== 'budget_paused' && !(typeof payload.error === 'string' && payload.error.startsWith('budget_'))) return undefined;
    const now = Date.now();
    const tomorrow = Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth(), new Date(now).getUTCDate() + 1);
    const proposed = payload.error === 'budget_paused' && Number.isSafeInteger(payload.retry_at)
      && Number(payload.retry_at) > now && Number(payload.retry_at) <= now + 48 * 60 * 60_000
      ? Number(payload.retry_at) : tomorrow;
    const prior = await this.ctx.storage.get<number>('hub2:budget_pause_until');
    const retryAt = Number.isSafeInteger(prior) && Number(prior) > now ? Math.max(Number(prior), proposed) : proposed;
    await this.ctx.storage.put('hub2:budget_pause_until', retryAt);
    if (await this.ctx.storage.getAlarm() !== retryAt) {
      this.db.alarmOperation();
      await this.ctx.storage.setAlarm(retryAt);
    }
    return retryAt;
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
    if (saga.account_id) {
      const proof = { kind: body.proof_kind, operation_id: saga.attempt_operation_id, session_id: saga.session_id,
        machine_id: saga.machine_id, epoch: body.epoch, observed_at: body.observed_at, offer_expires_at: body.offer_expires_at };
      if (!await this.releaseAccount(saga, { unclaimed_proof: proof })) return error('account_unclaimed_release_pending', 503);
    }
    const now = Date.now();
    this.transaction(() => {
      const current = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
      if (!current || current.state !== 'offered' || current.attempt_operation_id !== body.attempt_operation_id || current.epoch !== body.epoch) return;
      const nextAttempt = current.attempt + 1;
      this.db.run(`UPDATE dispatch_sagas SET state='pending_machine', attempt=?, retry_count=0, attempt_operation_id=?, next_action_at=?, last_reason=?, updated_at=?
        WHERE operation_id=? AND state='offered'`, nextAttempt, `${current.operation_id}:${nextAttempt}`, now, 'offer_expired_unclaimed', now, current.operation_id);
    });
    await this.scheduleNext();
    return json({ ok: true, retry_scheduled: true });
  }

  private async claimInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string'
      || typeof body.session_id !== 'string' || typeof body.machine_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_claim', 400);
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch) return error('stale_claim', 409);
    if (saga.state === 'claimed' || saga.state === 'admitted' || saga.state === 'running') return json({ claimed: true, duplicate: true });
    if (saga.state !== 'offered') return error('offer_not_claimable', 409);
    const now = Date.now();
    this.transaction(() => {
      this.db.run("UPDATE dispatch_sagas SET state='claimed', next_action_at=NULL, updated_at=? WHERE operation_id=? AND state='offered' AND attempt_operation_id=? AND epoch=?",
        now, saga.operation_id, saga.attempt_operation_id, saga.epoch);
      this.appendEvent(saga.intake_id, `claim:${saga.attempt_operation_id}:${saga.epoch}`, `machine:${saga.machine_id}`, 'session_claimed',
        { session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch }, now);
    });
    return json({ claimed: true });
  }

  private async admissionInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.session_id !== 'string'
      || typeof body.machine_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_admission', 400);
    const saga = this.sagaByAttempt(String(body.operation_id));
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id || saga.epoch !== body.epoch
      || !['offered', 'claimed', 'admitted', 'running'].includes(saga.state)) return error('stale_or_unoffered_admission', 409);
    const launch = this.parse(saga.launch_spec_json) as JsonRecord;
    if (saga.account_id) {
      const headers = { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify({
        id: `hub2-machine:${saga.machine_id}`, kind: 'machine', resource_id: saga.machine_id,
        generation: 'internal', scopes: ['account:reserve'],
      }) };
      const response = await budgetedBackgroundRequest(this.env, 'scheduled_start', async guardedEnv => {
        const account = guardedEnv.ACCOUNTS.get(guardedEnv.ACCOUNTS.idFromName(saga.account_id!));
        return account.fetch('https://account.hub2.internal/api/account/start', {
          method: 'POST', headers,
          body: JSON.stringify({ operation_id: saga.attempt_operation_id, session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch }),
        });
      });
      if (!response.ok && await this.pauseForBudget(response) !== undefined) return response;
      if (!response.ok) return error('account_admission_refused', response.status === 503 ? 503 : 409);
      const result = await response.json() as { reservation?: { operation_id?: string; session_id?: string; state?: string; epoch?: number; machine_id?: string } };
      if (result.reservation?.state !== 'running' || result.reservation.epoch !== saga.epoch
        || result.reservation.operation_id !== saga.attempt_operation_id
        || result.reservation.session_id !== saga.session_id
        || result.reservation.machine_id !== saga.machine_id) return error('account_admission_unverified', 503);
    }
    const now = Date.now();
    // Account admission is an awaited cross-DO call. The saga or intake can be
    // completed/retried while it is pending, so the earlier snapshot is not a
    // launch permit. Re-read both bindings and transition atomically after the
    // await; a stale AccountDO success must never authorize an old attempt.
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
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string'
      || typeof body.session_id !== 'string' || typeof body.machine_id !== 'string'
      || typeof body.native_id !== 'string' || !/^[0-9a-f-]{36}$/i.test(body.native_id)
      || !Number.isSafeInteger(body.epoch)) return error('invalid_started_receipt', 400);
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch) return error('stale_started_receipt', 409);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?', saga.intake_id);
    if (!intake || intake.session_id !== saga.session_id || intake.assigned_machine_id !== saga.machine_id) return error('started_intake_binding_mismatch', 409);
    if (saga.state === 'running') {
      return saga.native_id === body.native_id && intake.status === 'running'
        ? json({ started: true, duplicate: true }) : error('native_identity_conflict', 409);
    }
    if (saga.state !== 'admitted' || intake.status !== 'queued') return error('session_not_admitted', 409);
    const now = Date.now();
    this.transaction(() => {
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
    const body = await bodyObject(request, 12_288);
    if (typeof body.operation_id !== 'string' || typeof body.attempt_operation_id !== 'string' || typeof body.session_id !== 'string'
      || typeof body.machine_id !== 'string' || typeof body.native_id !== 'string' || !Number.isSafeInteger(body.epoch)
      || !Number.isInteger(body.exit_code) || Number(body.exit_code) < -2147483648 || Number(body.exit_code) > 2147483647
      || body.native_exit_confirmed !== true) return error('invalid_completion', 400);
    if(body.launch_failed!==undefined&&typeof body.launch_failed!=='boolean')return error('invalid_completion',400);
    const launchFailed=body.launch_failed===true;
    const saga = this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id = ?', body.operation_id);
    if (!saga || saga.session_id !== body.session_id || saga.machine_id !== body.machine_id
      || saga.attempt_operation_id !== body.attempt_operation_id || saga.epoch !== body.epoch
      || (saga.native_id !== body.native_id&&!(launchFailed&&saga.state==='admitted'&&saga.native_id===null&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.native_id)))) return error('dispatch_not_found', 404);
    const intake = this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id = ?', saga.intake_id);
    if (!intake) return error('intake_not_found', 404);
    const alreadyFinished = saga.state === 'complete';
    if (!alreadyFinished && ((launchFailed?saga.state!=='admitted':saga.state!=='running')||intake.session_id!==saga.session_id||intake.assigned_machine_id!==saga.machine_id)) return error('dispatch_not_found', 404);
    const evidence = body.evidence && typeof body.evidence === 'object' && !Array.isArray(body.evidence) ? body.evidence as JsonRecord : undefined;
    if (!evidence) return error('native_exit_evidence_required', 400);
    const normalizedExit = evidence.native_exit === undefined ? undefined : nativeReturncode(evidence.native_exit);
    if (evidence.native_exit !== undefined && (normalizedExit === undefined || normalizedExit !== body.exit_code)) return error('native_exit_code_mismatch', 400);
    if(launchFailed){
      const identity=evidence.native_identity as JsonRecord|undefined,exit=evidence.native_exit as JsonRecord|undefined,launch=this.parse(saga.launch_spec_json) as JsonRecord;
      if(!['aplexer_status','aplexer_retirement'].includes(String(evidence.source))||evidence.containment_empty!==true
        ||identity?.id!==body.native_id||identity?.tag!=='task-hub2-'+saga.session_id||identity?.workspace!==launch.workspace||normalizedExit!==body.exit_code)return error('invalid_launch_failure_proof',400);
    }
    const completionDigest=await sha256Hex(JSON.stringify({operation_id:saga.operation_id,attempt_operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,native_id:body.native_id,exit_code:body.exit_code,evidence,...(launchFailed?{launch_failed:true}:{})}));
    const replay=()=>{
      const event=this.db.one<{body_json:string}>('SELECT body_json FROM intake_events WHERE task_id=? AND event_key=?',intake.id,'finish:'+saga.operation_id);
      if(!event||(this.parse(event.body_json) as JsonRecord)?.completion_digest!==completionDigest)throw new HttpError('completion_proof_conflict',409);
    };
    if(alreadyFinished){replay();return json({ok:true,duplicate:true});}

    if (saga.account_id && !await this.releaseAccount(saga, { native_exit_confirmed: true })) return error('account_release_pending', 503);
    const now = Date.now();
    const nativeDeadline = evidence.native_deadline as JsonRecord | undefined;
    const deadlineExceeded = nativeDeadline?.exceeded === true;
    const finishedStatus = body.exit_code === 0 && !launchFailed && !deadlineExceeded ? 'completed-awaiting-review' : 'failed';
    const duplicate=this.transaction(() => {
      const currentSaga=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE operation_id=?',saga.operation_id);
      if(currentSaga?.state==='complete'){replay();return true;}
      const current=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intake.id);
      if((launchFailed?currentSaga?.state!=='admitted':currentSaga?.state!=='running')||current?.session_id!==saga.session_id||current.assigned_machine_id!==saga.machine_id)throw new HttpError('completion_binding_changed',409);
      this.db.run('UPDATE intakes SET status=?, final_evidence_json=?, updated_at=? WHERE id=?', finishedStatus, JSON.stringify({ ...evidence, native_id: body.native_id, exit_code: body.exit_code,native_exit_confirmed:true,account_released:true,...(launchFailed?{launch_failed:true}:{}) }), now, intake.id);
      this.db.run("UPDATE dispatch_sagas SET state='complete', native_id=?, next_action_at=NULL, last_reason=NULL, updated_at=? WHERE operation_id=?", body.native_id, now, saga.operation_id);
      this.appendEvent(intake.id, `finish:${saga.operation_id}`, 'hub2-host', 'finish_evidence_received', { session_id: saga.session_id, native_id: body.native_id, exit_code: body.exit_code, evidence,completion_digest:completionDigest,...(launchFailed?{launch_failed:true}:{}) }, now);
      return false;
    });
    await this.scheduleNext();
    return json({ ok: true, intake_id: intake.id,...(duplicate?{duplicate:true}:{}) });
  }

  private qualifiedFailure(intake:IntakeRow):boolean{
    if(intake.status!=='failed'||!intake.session_id)return false;
    const prior=this.db.one<SagaRow>('SELECT * FROM dispatch_sagas WHERE session_id=?',intake.session_id),proof=this.parse(intake.final_evidence_json) as JsonRecord|null;
    return !!prior&&prior.state==='complete'&&!!prior.native_id&&proof?.native_id===prior.native_id&&Number.isSafeInteger(proof.exit_code)&&(proof.exit_code!==0||proof.launch_failed===true||(proof.native_deadline as JsonRecord|undefined)?.exceeded===true)&&proof.native_exit_confirmed===true&&proof.account_released===true;
  }

  private async releaseAccount(saga: SagaRow, proof: { native_exit_confirmed?: true; unclaimed_proof?: JsonRecord }): Promise<boolean> {
    if (!saga.account_id) return true;
    // The Resource Manager coordinates the release against the authoritative
    // AccountDO API with the same qualified-proof enforcement as before.
    const outcome = await releaseAccountCapacity(this.env, {
      account_id: saga.account_id, attempt_operation_id: saga.attempt_operation_id,
      session_id: saga.session_id, machine_id: saga.machine_id, epoch: saga.epoch,
    }, proof);
    if (outcome.budgetRefusal) await this.pauseForBudget(outcome.budgetRefusal);
    return outcome.released;
  }


  private workAuthorized(actor:Actor,intakeId:string):boolean{
    const intake=this.db.one<IntakeRow>('SELECT * FROM intakes WHERE id=?',intakeId);
    return !!intake&&(this.canProject(actor,intake.project,'intake:read')||this.canReadIntake(actor,intake));
  }
  private async registerControlRoute(kind:string,id:string,owner:string,project:string,participants:string[],intakeId?:string,env:Hub2Env=this.env):Promise<boolean>{
    const response=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://control.internal/_routes/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,id,owner,resource_id:project,participants:[...new Set(participants)],...(intakeId?{bound_intake_id:intakeId}:{})})});
    return response.ok;
  }
  private async registerIntakeRoute(intake:IntakeRow,kind:BudgetKind='public_request'):Promise<boolean>{
    try{
      const participants=[intake.owner_actor_id,intake.assigned_actor_id].filter((v):v is string=>!!v);
      const response=await budgetedBackgroundRequest(this.env,kind,async guardedEnv=>new Response(null,{status:
        await this.registerControlRoute('intake',intake.id,intake.project,intake.project,participants,undefined,guardedEnv)?204:503}));
      if(!response.ok){
        const paused=await this.pauseForBudget(response);
        const next=paused??Date.now()+30000;
        this.db.run('UPDATE intakes SET route_next_action_at=? WHERE route_registered=0 AND route_next_action_at<=?',next,Date.now());
        return false;
      }
      this.db.run('UPDATE intakes SET route_registered=1 WHERE id=?',intake.id);return true;
    }catch{
      this.db.run('UPDATE intakes SET route_next_action_at=? WHERE id=? AND route_registered=0',Date.now()+30000,intake.id);return false;
    }
  }
  private async retryPendingIntakeRoutes(now=Date.now()):Promise<void>{
    for(const row of this.db.all<IntakeRow>('SELECT * FROM intakes WHERE route_registered=0 AND route_next_action_at<=? ORDER BY route_next_action_at,id LIMIT 8',now)){
      if(!await this.registerIntakeRoute(row,'retry'))break;
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
      const response=await budgetedBackgroundRequest(this.env,'native_delivery',guardedEnv=>guardedEnv.MAILBOXES.get(guardedEnv.MAILBOXES.idFromName('global')).fetch('https://mailbox.internal/api/messages',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor),'x-hub2-mailbox':record.owner},body:JSON.stringify({key:record.id,recipient,body})}));
      if(!response.ok){await this.pauseForBudget(response);throw new Error('scheduled_message_pending');}
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
    const budgetPause=await this.ctx.storage.get<number>('hub2:budget_pause_until');
    if(Number.isSafeInteger(budgetPause)&&Number(budgetPause)>Date.now()){
      const current=await this.ctx.storage.getAlarm();
      if(current!==Number(budgetPause)){this.db.alarmOperation();await this.ctx.storage.setAlarm(Number(budgetPause));}
      return;
    }
    if(budgetPause!==undefined)await this.ctx.storage.delete('hub2:budget_pause_until');
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
    return {
      id: saga.session_id, operation_id: saga.operation_id, attempt_operation_id: saga.attempt_operation_id, intake_id: saga.intake_id,
      machine_id: saga.machine_id, epoch: saga.epoch, state: saga.state, native_id: saga.native_id,
      attempt: saga.attempt, next_action_at: saga.next_action_at, held_reason: saga.last_reason,
    };
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
