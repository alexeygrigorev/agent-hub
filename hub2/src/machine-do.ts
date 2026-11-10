import { nativeReturncode } from './native-exit';
import { normalizeStartPickerEvidence } from './start-picker-evidence';
import { MeteredDb, initializeSchema } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';
import { CLAIMED_WITHOUT_NATIVE_GRACE_MS, inactiveReason, isLiveActiveSession, LIVE_SNAPSHOT_TTL_MS, SESSION_REAP_INTERVAL_MS, type LiveSession } from './session-liveness';
import type { Actor, Hub2Env, JsonRecord } from './types';

const SCHEMA = [
  'CREATE TABLE IF NOT EXISTS machine_capabilities(machine_id TEXT PRIMARY KEY,engines_json TEXT NOT NULL,capabilities_json TEXT NOT NULL,resources_json TEXT) WITHOUT ROWID',
  'CREATE TABLE IF NOT EXISTS machine_liveness(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at INTEGER NOT NULL)',
  'CREATE TABLE IF NOT EXISTS native_sessions(session_id TEXT PRIMARY KEY,native_id TEXT,phase TEXT NOT NULL,last_activity_at INTEGER,exit_code INTEGER,containment_empty INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
  `CREATE TABLE machines (
    machine_id TEXT PRIMARY KEY, alias TEXT NOT NULL, os TEXT NOT NULL, arch TEXT NOT NULL,
    labels_json TEXT NOT NULL, last_seen INTEGER NOT NULL, next_epoch INTEGER NOT NULL DEFAULT 0,
    active_session_id TEXT
  )`,
  `CREATE TABLE sessions (
    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
    dispatch_id TEXT NOT NULL, attempt INTEGER NOT NULL, account_id TEXT,
    project_id TEXT NOT NULL, intake_id TEXT NOT NULL, machine_id TEXT NOT NULL,
    epoch INTEGER NOT NULL, title TEXT NOT NULL, acceptance_criteria_json TEXT NOT NULL,
    launch_json TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT UNIQUE, offer_expires_at INTEGER,
    evidence_json TEXT, evidence_digest TEXT, exit_code INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX machine_session_offer ON sessions(machine_id, state, created_at, id)',
  'CREATE INDEX machine_session_deadline ON sessions(offer_expires_at, id) WHERE state = \'offered\' AND offer_expires_at IS NOT NULL',
  'CREATE INDEX machine_session_intake ON sessions(intake_id, created_at, id)',
  `CREATE TABLE machine_session_operations (
    operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    request_digest TEXT NOT NULL, state TEXT NOT NULL
  )`,
  'CREATE TABLE launch_admissions(operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,epoch INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
  `CREATE TABLE session_commands (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    request_key TEXT NOT NULL, request_digest TEXT NOT NULL, kind TEXT NOT NULL, text TEXT,
    state TEXT NOT NULL, result_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(session_id, request_key)
  )`,
  'CREATE INDEX command_pending_machine ON session_commands(machine_id, state, created_at, id)',
  `CREATE TABLE native_mail (
    message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,machine_id TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,
    digest TEXT NOT NULL,state TEXT NOT NULL,receipt_digest TEXT,created_at INTEGER NOT NULL,
    PRIMARY KEY(message_id,epoch)
  )`,
  'CREATE INDEX mail_pending_machine ON native_mail(machine_id,state,created_at,message_id,epoch)',
  'CREATE TABLE native_mail_fences(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
  'CREATE TABLE attention(session_id TEXT PRIMARY KEY,reason TEXT NOT NULL,operation_id TEXT,message_id TEXT,observed_at INTEGER NOT NULL)',
  'CREATE INDEX attention_recent ON attention(observed_at DESC,session_id)',
  `CREATE TABLE machine_outbox (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, operation_id TEXT NOT NULL,
    project_id TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
    attempt INTEGER NOT NULL, next_action_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX machine_outbox_due ON machine_outbox(state, next_action_at, id)',
];
// These identifiers are fixed source names; preserve every row and index on upgrade.
const COMPACT_KEYS=['machine_outbox','machine_session_operations','sessions'].flatMap(table=>{
  const create=SCHEMA.find(sql=>sql.startsWith(`CREATE TABLE ${table} (`))!;
  return [create.replace(`TABLE ${table}`,`TABLE ${table}_compact`)+' WITHOUT ROWID',
    `INSERT INTO ${table}_compact SELECT * FROM ${table}`,`DROP TABLE ${table}`,
    `ALTER TABLE ${table}_compact RENAME TO ${table}`,
    ...SCHEMA.filter(sql=>sql.startsWith('CREATE INDEX ')&&sql.includes(` ON ${table}(`))];
});
// Historical migration for slot accounting; migration 6 removes the obsolete table.
const MACHINE_ACTIVE_MIGRATION=[
  'CREATE TABLE IF NOT EXISTS machine_active(session_id TEXT PRIMARY KEY,machine_id TEXT NOT NULL) WITHOUT ROWID',
  'CREATE INDEX IF NOT EXISTS machine_active_machine ON machine_active(machine_id)',
  'INSERT OR IGNORE INTO machine_active(session_id,machine_id) SELECT active_session_id,machine_id FROM machines WHERE active_session_id IS NOT NULL',
  'UPDATE machines SET active_session_id=NULL',
];

const LATEST_SCHEMA=SCHEMA.map(sql=>{
  const latest=sql.replace(',resources_json TEXT','').replace(',\n    active_session_id TEXT','');
  return ['machine_outbox','machine_session_operations','sessions'].some(table=>latest.startsWith(`CREATE TABLE ${table} (`))?latest+' WITHOUT ROWID':latest;
});

type MachineRow = { machine_id: string; alias: string; os: string; arch: string; labels_json: string; last_seen: number; next_epoch: number };
type SessionRow = {
  id: string; operation_id: string; request_digest: string; dispatch_id: string; attempt: number; account_id: string | null;
  project_id: string; intake_id: string;
  machine_id: string; epoch: number; title: string; acceptance_criteria_json: string;
  launch_json: string;
  state: string; native_id: string | null; offer_expires_at: number | null;
  evidence_json: string | null; evidence_digest: string | null; exit_code: number | null;
  created_at: number; updated_at: number;
};
// The server speaks version 1 of the host WebSocket protocol.
const HOST_PROTOCOL_VERSION = 1;

type HostRelease = { host_version: string; protocol_version: number };
type SocketAttachment = { machine_id: string; actor_id: string; generation: string } & Partial<HostRelease>;

function hostRelease(hostVersion: unknown, protocolVersion: unknown): HostRelease | undefined {
  if (typeof hostVersion !== 'string' || hostVersion.length !== 40 || !/^[0-9a-f]+$/.test(hostVersion)
      || typeof protocolVersion !== 'number' || !Number.isSafeInteger(protocolVersion) || protocolVersion < 1) return undefined;
  return { host_version: hostVersion, protocol_version: protocolVersion };
}

function reportedHostRelease(query: URLSearchParams): HostRelease | undefined {
  const versions = query.getAll('host_version');
  const protocols = query.getAll('protocol_version');
  if (versions.length !== 1 || protocols.length !== 1 || !/^[1-9][0-9]{0,15}$/.test(protocols[0])) return undefined;
  return hostRelease(versions[0], Number(protocols[0]));
}

type OutboxRow = { id: string; kind: string; operation_id: string; project_id: string; payload_json: string; attempt: number; next_action_at: number };
type CommandRow = { id: string; session_id: string; machine_id: string; epoch: number; request_key: string; request_digest: string; kind: string; text: string | null; capture_mode: string | null; state: string; result_json: string | null; created_at: number; updated_at: number };
type NativeMail = {message_id:string;epoch:number;operation_id:string;session_id:string;machine_id:string;recipient:string;body:string;digest:string;state:string;receipt_digest:string|null};
const AUTH_FALLBACK_REASONS=new Set(['provider_auth_failed','provider_unauthorized','provider_route_mismatch','provider_account_required',
  'provider_materialization_refused','provider_generation_ack_unverified','credential_generation_rejected','credential_generation_conflict',
  'credentials_unavailable','account_unconfigured','access_lifetime_insufficient','provider_access_lifetime_insufficient',
  'child_credential_unavailable','child_credential_unverified','credential_key_unavailable','credential_decryption_failed',
  'credential_missing','refresh_refused','refresh_invalid','refresh_in_progress','refresh_outcome_uncertain','generation_conflict',
  'host_not_approved']);

/** Per-host presence, native-session evidence and durable completion outbox. */
export class MachineDO {
  private alarmQueue:Promise<void>=Promise.resolve();
  private connectQueue:Promise<void>=Promise.resolve();
  private readonly db: MeteredDb;
  private readonly sqlReceipts: SqlReceiptMeter;
  private readonly env: Hub2Env;
  private readonly constructorMs: number;

  constructor(private readonly ctx: DurableObjectState, env: Hub2Env) {
    const started = performance.now();
    this.db = new MeteredDb(ctx.storage.sql);
    this.sqlReceipts = new SqlReceiptMeter(this.db);
    this.env = wrapSqlNamespaces(env, this.sqlReceipts) as Hub2Env;
    initializeSchema(ctx, this.db, 7, LATEST_SCHEMA, [[SCHEMA[0]],COMPACT_KEYS,['ALTER TABLE machine_capabilities ADD COLUMN resources_json TEXT'],MACHINE_ACTIVE_MIGRATION,[
      'CREATE TABLE IF NOT EXISTS machine_liveness(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at INTEGER NOT NULL)',
      'CREATE TABLE IF NOT EXISTS native_sessions(session_id TEXT PRIMARY KEY,native_id TEXT,phase TEXT NOT NULL,last_activity_at INTEGER,exit_code INTEGER,containment_empty INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
      'DROP TABLE IF EXISTS machine_active','ALTER TABLE machine_capabilities DROP COLUMN resources_json','ALTER TABLE machines DROP COLUMN active_session_id',
    ],['ALTER TABLE session_commands ADD COLUMN capture_mode TEXT']],()=>this.scheduleNext(),6);
    this.constructorMs = performance.now() - started;
  }

  async fetch(request: Request): Promise<Response> {
    return this.sqlReceipts.measure(() => this.handleFetch(request), request.headers.get(SQL_PROBE_HEADER));
  }

  private async handleFetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/_internal/reserve' && request.method === 'POST') return await this.reserveInternal(request);
      if (url.pathname === '/_internal/activate' && request.method === 'POST') return await this.activateInternal(request);
      if (url.pathname === '/_internal/mail' && request.method === 'POST') return await this.queueMail(request);
      if (url.pathname === '/_internal/mail-ack' && request.method === 'POST') return await this.settleMail(request, false);
      if (url.pathname === '/_internal/mail-cancel' && request.method === 'POST') return await this.settleMail(request, true);
      if (url.pathname === '/_internal/mail-authorize' && request.method === 'POST') {
        const b=await bodyObject(request,4096);
        if(!boundedString(b.message_id,180)||!Number.isSafeInteger(b.epoch)||!boundedString(b.operation_id,180)||!boundedString(b.recipient,180)||!boundedString(b.machine_id,180))return error('invalid_delivery_binding',400);
        const row=this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?',b.message_id,b.epoch);
        if(!row||!['pending','acked'].includes(row.state)||row.machine_id!==b.machine_id||row.operation_id!==b.operation_id||row.recipient!==b.recipient)return error('delivery_binding_not_found',404);
        return json({authorized:true,message_id:row.message_id,epoch:row.epoch,operation_id:row.operation_id,recipient:row.recipient,machine_id:row.machine_id,session_id:row.session_id});
      }
      if (url.pathname === '/_internal/status' && request.method === 'GET') return this.machineView();
      if (url.pathname === '/_internal/routing-snapshot' && request.method === 'GET') return await this.routingView(true);
      if (url.pathname === '/_internal/routing' && request.method === 'GET') return await this.routingView();
      if (url.pathname === '/_internal/live-sessions' && request.method === 'GET') return this.liveSessionView();
      if (url.pathname === '/_internal/reap' && request.method === 'POST') return await this.reap(request);
      if (url.pathname === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot(), constructor_ms: this.constructorMs });
      if (url.pathname === '/api/connect' && request.method === 'GET') return await this.connect(request);
      if (url.pathname === '/api/heartbeat' && request.method === 'POST') return await this.heartbeat(request);
      if (url.pathname === '/api/offers' && request.method === 'GET') return this.offers(request);
      if (url.pathname === '/api/sessions' && request.method === 'GET') return this.sessionList(request);
      const sessionView = url.pathname.match(/^\/api\/sessions\/([^/]+)$/);
      if (sessionView && request.method === 'GET') return this.sessionDetail(request, decodeURIComponent(sessionView[1]));
      const commandAck = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands\/([^/]+)\/ack$/);
      if (commandAck && request.method === 'POST') return await this.ackCommand(request, decodeURIComponent(commandAck[1]), decodeURIComponent(commandAck[2]));
      const legacy = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(stop|send|capture)$/);
      if (legacy && request.method === 'POST') return await this.legacyCommand(request, decodeURIComponent(legacy[1]), legacy[2]);
      const commandList = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands$/);
      if (commandList && request.method === 'GET') return this.commandListing(request, decodeURIComponent(commandList[1]));
      const commandSend = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands$/);
      if (commandSend && request.method === 'POST') return await this.createCommand(request, decodeURIComponent(commandSend[1]));
      const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(started|finish|admission|credentials)$/);
      if (match && request.method === 'POST') {
        const sessionId = decodeURIComponent(match[1]);
        if (match[2] === 'started') return await this.started(request, sessionId);
        if (match[2] === 'admission') return await this.admission(request, sessionId);
        if (match[2] === 'credentials') return await this.childCredentials(request,sessionId);
        return await this.finish(request, sessionId);
      }
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('internal_error', 500);
    }
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const expired = this.db.all<SessionRow>(
      "SELECT * FROM sessions WHERE state IN ('staged','offered','claimed') AND native_id IS NULL AND created_at<=? ORDER BY created_at,id LIMIT 100",
      now-CLAIMED_WITHOUT_NATIVE_GRACE_MS);
    for (const session of expired) await this.recordLaunchTimeout(session, now);
    this.db.run("UPDATE session_commands SET state='failed',result_json=?,updated_at=? WHERE kind='stop' AND state='pending' AND created_at<=?",
      JSON.stringify({error:'stop_unconfirmed',stage:'host_stop'}),now,now-60_000);
    await this.reapInactive(false);
    await this.scheduleNext();
    await this.flushOutbox(now);
    await this.scheduleNext();
  }

  private async heartbeat(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:heartbeat');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 65_536);
    const os = typeof body.os === 'string' && body.os.length <= 40 ? body.os : 'unknown';
    const arch = typeof body.arch === 'string' && body.arch.length <= 40 ? body.arch : 'unknown';
    const labels = Array.isArray(body.labels) ? body.labels : [];
    const engines=body.engines??[],capabilities=body.capabilities??[];
    if(![engines,capabilities].every(values=>Array.isArray(values)&&values.length<=32&&values.every(v=>boundedString(v,80))))return error('invalid_capabilities',400);
    const enginesJson=JSON.stringify([...new Set(engines as string[])].sort()),capabilitiesJson=JSON.stringify([...new Set(capabilities as string[])].sort());
    if (labels.length > 64 || !labels.every(value => typeof value === 'string' && value.length > 0 && value.length <= 80)) return error('invalid_capabilities', 400);
    const now = Date.now();
    const existing = this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?', machineId);
    if (!existing) {
      this.db.run('INSERT INTO machines(machine_id, alias, os, arch, labels_json, last_seen, next_epoch) VALUES (?, ?, ?, ?, ?, ?, 0)',
        machineId, machineId, os, arch, JSON.stringify([...new Set(labels)]), now);
    } else if (now - existing.last_seen >= 60_000 || existing.os !== os || existing.arch !== arch || existing.labels_json !== JSON.stringify([...new Set(labels)])) {
      this.db.run('UPDATE machines SET os=?, arch=?, labels_json=?, last_seen=? WHERE machine_id=?', os, arch, JSON.stringify([...new Set(labels)]), now, machineId);
    }
    const prior=this.db.one<{engines_json:string;capabilities_json:string}>('SELECT engines_json,capabilities_json FROM machine_capabilities WHERE machine_id=?',machineId);
    if(!prior||prior.engines_json!==enginesJson||prior.capabilities_json!==capabilitiesJson)this.db.run('INSERT INTO machine_capabilities(machine_id,engines_json,capabilities_json) VALUES(?,?,?) ON CONFLICT(machine_id) DO UPDATE SET engines_json=excluded.engines_json,capabilities_json=excluded.capabilities_json',machineId,enginesJson,capabilitiesJson);
    const nativeSessions=body.native_sessions;
    if(nativeSessions!==undefined&&(!Array.isArray(nativeSessions)||nativeSessions.length>100||nativeSessions.some(value=>{
      if(!value||typeof value!=='object'||Array.isArray(value))return true;
      const row=value as JsonRecord;
      return !boundedString(row.session_id,180)||(row.native_id!==null&&!boundedString(row.native_id,180))
        ||!boundedString(row.phase,40)||(row.last_activity_at!==null&&!Number.isSafeInteger(row.last_activity_at))
        ||(row.exit_code!==null&&(!Number.isInteger(row.exit_code)||Number(row.exit_code)<-2147483648||Number(row.exit_code)>2147483647))
        ||typeof row.containment_empty!=='boolean';
    })))return error('invalid_native_session_snapshot',400);
    if(Array.isArray(nativeSessions))this.db.transaction(this.ctx,()=>{
      this.db.run('DELETE FROM native_sessions');
      for(const value of nativeSessions as JsonRecord[]){
        this.db.run('INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at) VALUES(?,?,?,?,?,?,?)',
          value.session_id,value.native_id,value.phase,value.last_activity_at,value.exit_code,Number(value.containment_empty),now);
      }
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
    });
    await this.scheduleNext();
    return json({ ok: true, machine_id: machineId, last_seen: now });
  }

  private async routingView(passive=false):Promise<Response>{
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    if(!machine)return json({available:false,reason:'machine_not_enrolled'});
    let connected=false;
    let release:HostRelease|undefined;
    for(const socket of this.machineSockets(machine.machine_id,!passive)){
      if(passive){try{if(socket.deserializeAttachment()?.machine_id!==machine.machine_id)continue;}catch{continue;}}
      if(socket.readyState!==1||this.stopOnly(socket))continue;
      const attachment=await this.authorizedAttachment(socket,'machine:offer',!passive,this.env,!passive);
      if(attachment?.machine_id!==machine.machine_id)continue;
      connected=true;
      // The running daemon reports these values. They are not an attestation.
      release=hostRelease(attachment.host_version,attachment.protocol_version);
      break;
    }
    const capabilities=this.db.one<{engines_json:string;capabilities_json:string}>('SELECT engines_json,capabilities_json FROM machine_capabilities WHERE machine_id=?',machine.machine_id);
    // Report protocol skew for operators without changing socket admission or dispatch.
    const versionMismatch = release && release.protocol_version !== HOST_PROTOCOL_VERSION;
    return json({machine_id:machine.machine_id,connected,...release,version_mismatch:versionMismatch,
      last_seen:machine.last_seen,os:machine.os,arch:machine.arch,labels:JSON.parse(machine.labels_json),
      engines:capabilities?JSON.parse(capabilities.engines_json):[],capabilities:capabilities?JSON.parse(capabilities.capabilities_json):[]});
  }

  private offers(request: Request): Response {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:offer');
    if (!machineId) return error('forbidden', 403);
    const now = Date.now();
    const rows = this.db.all<SessionRow>(
      `SELECT * FROM sessions WHERE machine_id=? AND ((state='offered' AND offer_expires_at > ?) OR state='claimed') ORDER BY created_at, id LIMIT 10`, machineId, now,
    );
    return json({ offers: rows.map(row => this.offerView(row)) });
  }

  private async reserveInternal(request: Request): Promise<Response> {
    // Diagnostics travel separately, so a full policy pool never consumes the
    // user launch-spec allowance. Both fields retain their own bounded checks.
    const body = await bodyObject(request, 65_536);
    const required = ['operation_id', 'dispatch_id', 'session_id', 'project_id', 'intake_id', 'machine_id', 'request_digest', 'title'];
    if (required.some(key => typeof body[key] !== 'string') || !Number.isSafeInteger(body.attempt) || Number(body.attempt) < 0) return error('invalid_reservation', 400);
    if (typeof body.machine_id !== 'string' || typeof body.operation_id !== 'string' || typeof body.session_id !== 'string') return error('invalid_reservation', 400);
    if (!Array.isArray(body.acceptance_criteria) || body.acceptance_criteria.length < 1) return error('acceptance_criteria_required', 409);
    const launch = body.launch;
    if (!launch || typeof launch !== 'object' || Array.isArray(launch)
      || typeof (launch as JsonRecord).workspace !== 'string' || typeof (launch as JsonRecord).engine !== 'string'
      || typeof (launch as JsonRecord).prompt !== 'string' || typeof (launch as JsonRecord).interactive !== 'boolean') return error('invalid_launch_spec', 400);
    if (new TextEncoder().encode(JSON.stringify(launch)).byteLength > 12_000) return error('launch_spec_too_large', 413);
    if (Object.hasOwn(launch, '_hub_picker')) return error('reserved_launch_field', 400);
    const picker = normalizeStartPickerEvidence(body.picker);
    if (body.picker !== undefined && (!picker || picker.machine_id !== body.machine_id || picker.attempt !== body.attempt
      || picker.engine !== (launch as JsonRecord).engine || picker.model !== (launch as JsonRecord).model
      || picker.effort !== ((launch as JsonRecord).effort ?? null))) return error('invalid_picker_evidence', 400);
    // Use an existing JSON column; keep evidence out of the native launch wire.
    const storedLaunch = { ...(launch as JsonRecord), ...(picker ? { _hub_picker: picker } : {}) };
    const now = Date.now();
    const deadline=(launch as JsonRecord).start_deadline_at;
    if(deadline!==undefined&&(!Number.isSafeInteger(deadline)||Number(deadline)>now+120_000))return error('invalid_start_deadline',400);
    if(deadline!==undefined&&Number(deadline)<=now)return error('host_no_response',409);
    const acceptedAt=deadline===undefined?now:Number(deadline)-120_000;
    const priorOperation = this.db.one<{ session_id: string; epoch: number; request_digest: string; state: string }>(
      'SELECT session_id,epoch,request_digest,state FROM machine_session_operations WHERE operation_id=?', body.operation_id,
    );
    const prior = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', body.session_id);
    if (priorOperation && priorOperation.session_id === body.session_id && priorOperation.state !== 'current') {
      return json({ session_id: body.session_id, epoch: priorOperation.epoch, state: priorOperation.state, duplicate: true }, 200);
    }
    if (prior && prior.operation_id === body.operation_id) {
      if (prior.request_digest !== body.request_digest || prior.dispatch_id !== body.dispatch_id || prior.machine_id !== body.machine_id
        || prior.attempt !== body.attempt || prior.account_id !== (typeof body.account_id === 'string' ? body.account_id : null)) return error('idempotency_conflict', 409);
      return json({ session_id: prior.id, epoch: prior.epoch, state: prior.state, duplicate: true }, 200);
    }
    let machine = this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?', body.machine_id);
    if(!machine){
      this.db.run('INSERT INTO machines(machine_id,alias,os,arch,labels_json,last_seen,next_epoch) VALUES(?,?,?,?,?,?,0)',
        body.machine_id,body.machine_id,'unknown','unknown','[]',now);
      machine=this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?',body.machine_id);
    }
    const retrying = prior !== undefined;
    if (retrying && (prior!.state !== 'expired' || prior!.dispatch_id !== body.dispatch_id
      || prior!.request_digest !== body.request_digest || prior!.machine_id !== body.machine_id
      || prior!.attempt + 1 !== body.attempt || body.retry_epoch !== prior!.epoch)) return error('retry_fence_conflict', 409);
    if (!retrying && (Number(body.attempt) !== 0 || body.retry_epoch !== undefined)) return error('retry_without_expired_offer', 409);
    let epoch = 0;
    try {
      this.db.transaction(this.ctx, () => {
        const fresh=this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?',body.machine_id);
        if (!fresh) throw new HttpError('machine_route_unavailable',503);
        epoch=fresh.next_epoch+1;
        this.db.run('UPDATE machines SET next_epoch=? WHERE machine_id=?', epoch, body.machine_id);
        if (retrying) {
          this.db.run("UPDATE session_commands SET state='failed',result_json=?,updated_at=? WHERE session_id=? AND machine_id=? AND epoch=? AND state='pending'",
            JSON.stringify({error:'session_epoch_superseded'}),now,body.session_id,body.machine_id,prior!.epoch);
          this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=?",
            prior!.operation_id, prior!.id, prior!.epoch);
          this.db.run(`UPDATE sessions SET operation_id=?,attempt=?,epoch=?,account_id=?,launch_json=?,state='staged',offer_expires_at=NULL,
            native_id=NULL,evidence_json=NULL,evidence_digest=NULL,exit_code=NULL,updated_at=? WHERE id=? AND state='expired' AND epoch=?`,
            body.operation_id, body.attempt, epoch, typeof body.account_id === 'string' ? body.account_id : null, JSON.stringify(storedLaunch), now, body.session_id, prior!.epoch);
        } else {
          this.db.run(`INSERT INTO sessions(id, operation_id, request_digest, dispatch_id, attempt, account_id, project_id, intake_id, machine_id, epoch, title,
            acceptance_criteria_json, launch_json, state, native_id, offer_expires_at, evidence_json, evidence_digest, exit_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', NULL, NULL, NULL, NULL, NULL, ?, ?)`,
            body.session_id, body.operation_id, body.request_digest, body.dispatch_id, body.attempt, typeof body.account_id === 'string' ? body.account_id : null,
            body.project_id, body.intake_id, body.machine_id, epoch, body.title,
            JSON.stringify(body.acceptance_criteria), JSON.stringify(storedLaunch), acceptedAt, now);
        }
        this.db.run("INSERT INTO machine_session_operations(operation_id,session_id,epoch,request_digest,state) VALUES(?,?,?,?, 'current')",
          body.operation_id, body.session_id, epoch, body.request_digest);
      });
    } catch (caught) {
      return error('reservation_failed', 503);
    }
    return json({ session_id: body.session_id, epoch, state: 'staged' }, retrying ? 200 : 201);
  }

  private async activateInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.session_id !== 'string' || !Number.isInteger(body.epoch)) return error('invalid_activation', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE operation_id=? AND id=?', body.operation_id, body.session_id);
    if (!row || row.epoch !== body.epoch) return error('reservation_not_found', 404);
    if (row.state === 'offered' || row.state === 'claimed' || row.state === 'running') return json({ session_id: row.id, epoch: row.epoch, state: row.state }, 200);
    if (row.state !== 'staged') return error('reservation_not_activatable', 409);
    const now = Date.now();
    const expires = row.created_at + CLAIMED_WITHOUT_NATIVE_GRACE_MS;
    if(expires<=now){await this.recordLaunchTimeout(row,now);return error('host_no_response',409);}
    this.db.run("UPDATE sessions SET state='offered', offer_expires_at=?, updated_at=? WHERE id=? AND state='staged'", expires, now, row.id);
    await this.ensureDeadline(expires);
    await this.notifyOffer(row.id);
    return json({ session_id: row.id, epoch: row.epoch, state: 'offered' }, 201);
  }

  private async connect(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:offer');
    if (!machineId) return error('forbidden', 403);
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return error('websocket_upgrade_required', 426);
    const stopOnly=request.headers.get('x-hub2-safety-mode')==='operator_stop';
    const query=new URL(request.url).searchParams;
    if(query.has('safety')&&!stopOnly)return error('untrusted_safety_mode',403);
    let client!:WebSocket,server!:WebSocket;
    const accepted=this.connectQueue.then(async()=>{
      const control=this.env.CONTROL.get(this.env.CONTROL.idFromName('global'));
      const current=await control.fetch('https://control.hub2.internal/_authorize',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({actor_id:actor!.id,generation:actor!.generation,scope:'machine:offer'}),
      });
      if(!current.ok)return false;
      const pair=new WebSocketPair();[client,server]=Object.values(pair);
      for(const old of this.ctx.getWebSockets('hub2:'+machineId)){
        try{old.close(1000,'connection superseded');}catch{}
      }
      server.serializeAttachment({machine_id:machineId,actor_id:actor!.id,generation:actor!.generation,
        stop_only:stopOnly,connection_id:crypto.randomUUID(),connected_at:Date.now(),
        ...(!stopOnly?reportedHostRelease(query):undefined)});
      this.ctx.acceptWebSocket(server,['hub2:'+machineId]);
      return true;
    });
    this.connectQueue=accepted.then(()=>{},()=>{});
    if(!await accepted)return error('credential_revoked',401);
    try { server.send(JSON.stringify({ type: 'resync', reason: 'connected' })); } catch {}
    await this.sendOffers(server, machineId);
    await this.sendPendingCommands(server, machineId);
    await this.sendPendingMail(server, machineId);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(socket:WebSocket,message:string|ArrayBuffer):Promise<void>{
    if(!this.currentSocket(socket))return;
    await this.webSocketMessageRaw(socket, message, this.env);
  }

  private async webSocketMessageRaw(socket: WebSocket, message: string | ArrayBuffer, env:Hub2Env): Promise<void> {
    if (typeof message !== 'string' || message.length > 8192) {
      try { socket.close(1009, 'message too large'); } catch {}
      return;
    }
    let body: JsonRecord;
    try { body = JSON.parse(message) as JsonRecord; } catch { return; }
    if(this.stopOnly(socket)&&body.type!=='resync'){try{socket.send(JSON.stringify({type:'error',error:'safety_socket_stop_only'}));}catch{}return;}
    if (body.type === 'ping') {
      try { socket.send(JSON.stringify({ type: 'pong', nonce: typeof body.nonce === 'string' ? body.nonce.slice(0, 80) : undefined })); } catch {}
      return;
    }
    if (body.type === 'resync') {
      try {
        const attachment = await this.authorizedAttachment(socket, 'machine:offer',true,env);
        if (attachment?.machine_id) { await this.sendOffers(socket, attachment.machine_id,env); await this.sendPendingCommands(socket, attachment.machine_id,env); await this.sendPendingMail(socket, attachment.machine_id,env); }
      } catch {}
      return;
    }
    if (body.type === 'offer_ack') {
      await this.ackOffer(socket, body,env);
      return;
    }
    if (body.type === 'host.error') { await this.recordAttention(socket, body,env); return; }
    try { socket.send(JSON.stringify({ type: 'error', error: 'unsupported_message' })); } catch {}
  }

  webSocketClose(socket:WebSocket,code:number,reason:string,_wasClean:boolean):void{
    try{socket.close(code===1005||code===1006?1000:code,reason);}catch{}
  }

  webSocketError(_socket: WebSocket, _error: unknown): void {}

  private async ackOffer(socket: WebSocket, body: JsonRecord,env:Hub2Env): Promise<void> {
    if (typeof body.session_id !== 'string' || !Number.isInteger(body.epoch)) return;
    try {
      const attachment = await this.authorizedAttachment(socket, 'machine:offer',true,env);
      if (!attachment?.machine_id) return;
      const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', body.session_id, attachment.machine_id);
      if (!row || row.epoch !== body.epoch) {
        socket.send(JSON.stringify({ type: 'error', error: 'stale_offer' }));
        return;
      }
      if (row.state === 'claimed' || row.state === 'running') {
        socket.send(JSON.stringify({ type: 'ack', session_id: row.id, epoch: row.epoch, duplicate: true }));
        return;
      }
      if (row.state !== 'offered' || row.offer_expires_at === null || row.offer_expires_at <= Date.now()) {
        socket.send(JSON.stringify({ type: 'error', error: 'offer_unavailable' }));
        return;
      }
      const now = Date.now();
      this.db.transaction(this.ctx, () => {
        this.db.run("UPDATE sessions SET state='claimed', offer_expires_at=NULL, updated_at=? WHERE id=? AND state='offered' AND epoch=?", now, row.id, row.epoch);
        this.db.run('INSERT OR IGNORE INTO launch_admissions VALUES(?,?,?,?)', row.operation_id,row.id,row.epoch,now);
        this.insertOutbox(`claim:${row.operation_id}`, 'claim', row.operation_id, row.project_id,
          { operation_id: row.dispatch_id, attempt_operation_id: row.operation_id, session_id: row.id,
            machine_id: row.machine_id, epoch: row.epoch }, now);
      });
      await this.ensureDeadline(now+30_000);
      await this.flushOne(`claim:${row.operation_id}`);
      await this.scheduleNext();
      await this.sendPendingCommands(socket, attachment.machine_id,env);
      socket.send(JSON.stringify({ type: 'ack', session_id: row.id, epoch: row.epoch }));
    } catch {}
  }

  private async sendOffers(socket: WebSocket, machineId: string,env:Hub2Env=this.env): Promise<void> {
    if(this.stopOnly(socket))return;
    if(!this.currentSocket(socket))return;
    const now = Date.now();
    const rows = this.db.all<SessionRow>(
      `SELECT * FROM sessions WHERE machine_id=? AND ((state='offered' AND offer_expires_at > ?) OR state='claimed') ORDER BY created_at, id LIMIT 10`, machineId, now,
    );
    for (const row of rows) {
      try { socket.send(JSON.stringify({ type: 'session.offer', offer: this.offerView(row) })); } catch { return; }
    }
  }

  private async notifyOffer(sessionId: string): Promise<void> {
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND state=\'offered\'', sessionId);
    if (!row) return;
    const frame = JSON.stringify({ type: 'session.offer', offer: this.offerView(row) });
    for (const socket of this.machineSockets(`${row.machine_id}`)) {
      try {
        if(this.stopOnly(socket))continue;
        if(await this.authorizedAttachment(socket,'machine:offer',true,this.env))socket.send(frame);
      } catch {}
    }
  }

  private offerView(row: SessionRow): JsonRecord {
    const { _hub_picker: _picker, ...launch } = this.parse(row.launch_json) as JsonRecord;
    return {
      session_id: row.id, operation_id: row.operation_id, intake_id: row.intake_id,
      project_id: row.project_id, account_id: row.account_id, epoch: row.epoch, title: row.title,
      acceptance_criteria: this.parse(row.acceptance_criteria_json),
      launch, expires_at: row.offer_expires_at, claimed: row.state === 'claimed',
    };
  }

  private async admission(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:admit');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_admission', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row || row.operation_id !== body.operation_id || row.epoch !== body.epoch || !['claimed', 'running'].includes(row.state)) return error('stale_or_unclaimed_admission', 409);
    const stub = this.env.PROJECTS.get(this.env.PROJECTS.idFromName(row.project_id));
    const response = await stub.fetch('https://project.internal/_internal/admission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation_id: row.operation_id, session_id: row.id, machine_id: machineId, epoch: row.epoch }),
    });
    if (!response.ok) return error('launch_admission_refused', response.status === 503 ? 503 : 409);
    const result = await response.json() as JsonRecord;
    const launch = this.parse(row.launch_json) as JsonRecord;
    if (result.launch_allowed !== true || result.epoch !== row.epoch || result.engine !== launch.engine
      || (result.model ?? null) !== (launch.model ?? null) || !Number.isSafeInteger(result.observed_at)) return error('launch_admission_unverified', 503);
    const recorded = this.db.transaction(this.ctx, () => {
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',row.id);
      if (!current || current.operation_id!==row.operation_id || current.epoch!==row.epoch || !['claimed','running'].includes(current.state)) return false;
      const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=?',row.operation_id);
      if (!admission) this.db.run('INSERT INTO launch_admissions VALUES(?,?,?,?)',row.operation_id,row.id,row.epoch,result.observed_at);
      return admission?.observed_at ?? Number(result.observed_at);
    });
    if (!recorded) return error('stale_admission_result',409);
    const runtimeDeadline=Number(recorded)+Number(launch.timeout_seconds)*1000;
    if(!Number.isSafeInteger(runtimeDeadline)||runtimeDeadline<=Date.now()||runtimeDeadline>Date.now()+7_200_000)return error('runtime_deadline_unavailable',409);
    return json({...result,session_id:row.id,operation_id:row.operation_id,machine_id:machineId,
      account_id:row.account_id??null,runtime_deadline:runtimeDeadline});
  }

  private async childCredentials(request:Request,sessionId:string):Promise<Response>{
    const actor=actorFrom(request),machine=this.machineActor(actor,'session:admit');
    if(!machine||!actor?.generation)return error('forbidden',403);
    const body=await bodyObject(request,4096);
    const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machine);
    if(!row||row.state!=='claimed'||row.operation_id!==body.operation_id||row.epoch!==body.epoch)return error('child_credential_fence',409);
    const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    const timeout=(this.parse(row.launch_json) as JsonRecord).timeout_seconds;
    if(!admission||!Number.isSafeInteger(timeout)||Number(timeout)<1||Number(timeout)>86400)return error('launch_admission_required',409);
    const deadline=admission.observed_at+Number(timeout)*1000;
    if(deadline<=Date.now())return error('child_credential_deadline_passed',409);
    const response=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_worker/issue',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:row.id,operation_id:row.operation_id,
        epoch:row.epoch,machine_id:row.machine_id,machine_generation:actor.generation,intake_id:row.intake_id,project_id:row.project_id,deadline})});
    if(!response.ok)return error('child_credential_unavailable',response.status===409?409:503);
    const result=await response.json() as JsonRecord;
    if(result.session_id!==row.id||result.operation_id!==row.operation_id||result.epoch!==row.epoch||result.machine_id!==row.machine_id||result.intake_id!==row.intake_id||result.project_id!==row.project_id||result.actor!=='session:'+row.id||typeof result.token!=='string'||!Number.isSafeInteger(result.expires_at)||Number(result.expires_at)>deadline||Number(result.expires_at)<=Date.now())return error('child_credential_unverified',503);
    if(!this.db.one('SELECT id FROM sessions WHERE id=? AND operation_id=? AND epoch=? AND state=\'claimed\'',row.id,row.operation_id,row.epoch))return error('child_credential_fence',409);
    return json(result,response.status);
  }

  private async started(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.native_id !== 'string' || body.native_id.length < 8 || body.native_id.length > 128 || !Number.isSafeInteger(body.epoch)) return error('invalid_native_receipt', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row) return error('session_not_found', 404);
    if (row.state === 'running' && row.operation_id === body.operation_id && row.native_id === body.native_id && row.epoch === body.epoch) {
      const sent=this.db.one<{state:string}>('SELECT state FROM machine_outbox WHERE id=?',`started:${row.operation_id}`);
      const forwarded=sent?.state==='delivered'||await this.flushOne(`started:${row.operation_id}`);
      await this.scheduleNext();
      return json({ok:true,duplicate:true,forwarded});
    }
    if (row.state !== 'claimed' || row.operation_id !== body.operation_id || row.epoch !== body.epoch) return error('stale_or_unoffered_session', 409);
    if (!this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch)) return error('launch_admission_required',409);
    const now=Date.now();
    this.db.transaction(this.ctx,()=>{
      this.db.run("UPDATE sessions SET state='running', native_id=?, offer_expires_at=NULL, updated_at=? WHERE id=? AND state='claimed'",
        body.native_id,now,sessionId);
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,'running',?,NULL,0,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase='running',last_activity_at=excluded.last_activity_at,exit_code=NULL,containment_empty=0,observed_at=excluded.observed_at`,
        sessionId,body.native_id,now,now);
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
      this.insertOutbox(`started:${row.operation_id}`,'started',row.operation_id,row.project_id,
        {operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,machine_id:row.machine_id,epoch:row.epoch,native_id:body.native_id},now);
    });
    await this.ensureDeadline(now+30_000);
    const forwarded=await this.flushOne(`started:${row.operation_id}`);
    await this.scheduleNext();
    return json({ ok: true, session_id: sessionId, state: 'running',forwarded }, forwarded?201:202);
  }

  private async finish(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 12_288);
    const launchFailed=body.launch_failed===true;
    if(body.launch_failed!==undefined&&typeof body.launch_failed!=='boolean')return error('invalid_launch_failure',400);
    if (typeof body.operation_id !== 'string' || !Number.isSafeInteger(body.epoch) || typeof body.exit_code !== 'number'
      || !Number.isInteger(body.exit_code) || body.exit_code < -2147483648 || body.exit_code > 2147483647
      || typeof body.native_id !== 'string' || body.native_exit_confirmed !== true) return error('invalid_finish_evidence', 400);
    const evidence = body.evidence && typeof body.evidence === 'object' && !Array.isArray(body.evidence) ? body.evidence as JsonRecord : {};
    const evidenceJson = JSON.stringify(evidence);
    if (new TextEncoder().encode(evidenceJson).byteLength > 8192) return error('evidence_too_large', 413);
    const evidenceDigest = await sha256Hex(JSON.stringify({ epoch: body.epoch, native_id: body.native_id, exit_code: body.exit_code, evidence,
      ...(launchFailed?{launch_failed:true}:{}) }));
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row) return error('session_not_found', 404);
    if(row.operation_id!==body.operation_id||row.epoch!==body.epoch)return error('stale_or_unmatched_finish',409);
    const launch = this.parse(row.launch_json) as JsonRecord;
    const identity = evidence.native_identity as JsonRecord | undefined;
    const nativeExit = evidence.native_exit as JsonRecord | undefined;
    if (!['aplexer_status', 'aplexer_retirement'].includes(String(evidence.source)) || evidence.containment_empty !== true
      || !identity || identity.id !== body.native_id || identity.tag !== `task-hub2-${row.id}`
      || identity.workspace !== launch.workspace || !nativeExit || nativeReturncode(nativeExit) !== body.exit_code) return error('unqualified_native_exit_evidence', 409);
    if (row.state === 'completed-awaiting-review' || row.state === 'failed') {
      return row.evidence_digest === evidenceDigest ? json({ ok: true, duplicate: true, state: row.state }) : error('finish_conflict', 409);
    }
    const nativeDeadline=evidence.native_deadline as JsonRecord|undefined;
    const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    if(!nativeDeadline||!Number.isSafeInteger(nativeDeadline.deadline_ms)||typeof nativeDeadline.exceeded!=='boolean'||!admission
      ||nativeDeadline.deadline_ms!==admission.observed_at+Number(launch.timeout_seconds)*1000
      ||!Number.isSafeInteger(nativeExit.exited_at_ms)||Number(nativeExit.exited_at_ms)<admission.observed_at||Number(nativeExit.exited_at_ms)>Date.now()+5000
      ||(Number(nativeExit.exited_at_ms)>=Number(nativeDeadline.deadline_ms)&&nativeDeadline.exceeded!==true)
      ||(nativeDeadline.exceeded&&Number(nativeDeadline.deadline_ms)>Date.now()))return error('native_deadline_unverified',409);
    const admittedAbort=launchFailed&&row.state==='claimed'&&row.native_id===null
      &&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.native_id)
      &&this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    if (!admittedAbort&&(launchFailed||row.state!=='running'||row.native_id!==body.native_id))return error('stale_or_unmatched_finish',409);
    const now = Date.now();
    const state = !launchFailed&&nativeDeadline?.exceeded!==true&&Number(body.exit_code) === 0 ? 'completed-awaiting-review' : 'failed';
    const payload = { operation_id: row.dispatch_id, attempt_operation_id: row.operation_id, session_id: row.id,
      machine_id: row.machine_id,intake_id:row.intake_id, epoch: row.epoch, native_id: body.native_id,
      ...(launchFailed?{launch_failed:true}:{}),
      exit_code: Number(body.exit_code), native_exit_confirmed: true, evidence, evidence_digest: evidenceDigest };
    this.db.transaction(this.ctx, () => {
      this.db.run('UPDATE sessions SET state=?, evidence_json=?, evidence_digest=?, exit_code=?, updated_at=?,native_id=? WHERE id=? AND state=?',
        state, JSON.stringify(payload), evidenceDigest, Number(body.exit_code), now, body.native_id,row.id, row.state);
      this.db.run("UPDATE native_sessions SET phase='exited',last_activity_at=?,exit_code=?,containment_empty=1,observed_at=? WHERE session_id=?",
        now,Number(body.exit_code),now,row.id);
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
      this.insertOutbox(`finish:${row.operation_id}`, 'finish', row.operation_id, row.project_id, payload, now);
    });
    await this.ensureDeadline(now+30_000);
    const forwarded = await this.flushOne(`finish:${row.operation_id}`);
    await this.scheduleNext();
    return json({ ok: true, session_id: sessionId, state, forwarded }, forwarded ? 201 : 202);
  }

  private async flushOutbox(now: number): Promise<void> {
    const rows = this.db.all<OutboxRow>(
      `SELECT * FROM machine_outbox WHERE state='pending' AND next_action_at <= ? ORDER BY next_action_at, id LIMIT 20`, now,
    );
    for (const row of rows) await this.flushOne(row.id);
  }

  private async flushOne(id:string):Promise<boolean>{
    return this.flushOneRaw(id,this.env);
  }

  private async flushOneRaw(id: string, env:Hub2Env): Promise<boolean> {
    const row = this.db.one<OutboxRow>('SELECT * FROM machine_outbox WHERE id=? AND state=\'pending\'', id);
    if (!row) return false;
    const payload = this.parse(row.payload_json) as JsonRecord;
    if(row.kind==='finish'&&!payload.intake_id)payload.intake_id=this.db.one<{intake_id:string}>('SELECT intake_id FROM sessions WHERE id=? AND operation_id=?',payload.session_id,payload.attempt_operation_id)?.intake_id;
    // Every finish outbox row was created only after verified public finish evidence.
    if (row.kind === 'finish') payload.native_exit_confirmed = true;
    try {
      if(row.kind==='expiry'){
        const session=this.db.one<SessionRow>("SELECT * FROM sessions WHERE id=? AND operation_id=? AND epoch=? AND state='offered' AND offer_expires_at<=?",
          payload.session_id,payload.operation_id,payload.epoch,Date.now());
        if(session){
          const now=Date.now();
          this.db.transaction(this.ctx, () => {
            this.db.run("UPDATE sessions SET state='expired', offer_expires_at=NULL, updated_at=? WHERE id=? AND state='offered'", now, session.id);
            this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",
              session.operation_id, session.id, session.epoch);
            this.insertOutbox(`requeue:${session.operation_id}`, 'requeue', session.operation_id, session.project_id,
              { operation_id: session.dispatch_id, attempt_operation_id: session.operation_id, session_id: session.id,
                machine_id: session.machine_id, epoch: session.epoch, proof_kind: 'offer-expired-unclaimed',
                offer_expires_at: session.offer_expires_at, observed_at: now }, now);
          });
        }
        this.db.run("UPDATE machine_outbox SET state='delivered',updated_at=? WHERE id=? AND state='pending'",Date.now(),row.id);
        return true;
      }
      if(row.kind==='finish'){
        const revoked=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://control.internal/_worker/revoke',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:payload.session_id,
            operation_id:payload.attempt_operation_id,epoch:payload.epoch,machine_id:payload.machine_id,
            intake_id:payload.intake_id,project_id:row.project_id,reason:'native_exit_confirmed'})});
        if(!revoked.ok||(await revoked.json() as JsonRecord).revoked!==true)throw new Error('child_revocation_pending');
      }
      const stub = env.PROJECTS.get(env.PROJECTS.idFromName(row.project_id));
      const path = row.kind === 'finish' ? '/_internal/finish' : row.kind === 'started' ? '/_internal/started'
        : row.kind === 'auth-fallback' ? '/_internal/auth-fallback'
        : row.kind === 'stopped' ? '/_internal/stopped' : row.kind === 'reaped' ? '/_internal/reaped'
        : row.kind === 'claim' ? '/_internal/claim' : '/_internal/requeue';
      const response = await stub.fetch(`https://project.internal${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error('project_forward_failed');
      this.db.run("UPDATE machine_outbox SET state='delivered', updated_at=? WHERE id=? AND state='pending'", Date.now(), row.id);
      return true;
    } catch {
      const now = Date.now();
      const attempt = row.attempt + 1;
      if(attempt>=10){
        this.db.run("UPDATE machine_outbox SET state='dead_letter',attempt=?,updated_at=? WHERE id=? AND state='pending'",attempt,now,row.id);
        if(typeof payload.session_id==='string')this.db.run('INSERT OR IGNORE INTO attention VALUES(?,?,?,?,?)',payload.session_id,'outbox_retries_exhausted',row.operation_id,null,now);
        console.error('[hub2] MACHINE_OUTBOX_DEAD_LETTER',JSON.stringify({id:row.id,kind:row.kind,operation_id:row.operation_id,attempt}));
        return false;
      }
      const delay = Math.min(15 * 60_000, 1_000 * 2 ** Math.min(attempt, 9));
      this.db.run('UPDATE machine_outbox SET attempt=?, next_action_at=?, updated_at=? WHERE id=? AND state=\'pending\'', attempt, now + delay, now, row.id);
      return false;
    }
  }

  private insertOutbox(id: string, kind: string, operationId: string, projectId: string, payload: JsonRecord, now: number) {
    this.db.run(`INSERT OR IGNORE INTO machine_outbox(id, kind, operation_id, project_id, payload_json, state, attempt, next_action_at, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)`, id, kind, operationId, projectId, JSON.stringify(payload), now+30_000, now, now);
  }

  private async scheduleNext(): Promise<void> {
    const pending=this.alarmQueue.catch(()=>{}).then(async()=>{
    this.db.alarmOperation();
    const current = await this.ctx.storage.getAlarm();
    const next = this.db.one<{ due: number | null }>(`SELECT MIN(due) AS due FROM (
      SELECT MIN(offer_expires_at) AS due FROM sessions WHERE state='offered' AND offer_expires_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM machine_outbox b WHERE b.id='expiry:'||sessions.id||':'||sessions.epoch AND b.state='pending')
      UNION ALL SELECT MIN(created_at+?) AS due FROM sessions WHERE state IN ('staged','offered','claimed') AND native_id IS NULL
      UNION ALL SELECT MIN(created_at+60000) AS due FROM session_commands WHERE kind='stop' AND state='pending'
      UNION ALL SELECT MIN(next_action_at) AS due FROM machine_outbox WHERE state='pending'
      UNION ALL SELECT ? AS due WHERE EXISTS(SELECT 1 FROM sessions WHERE state IN ('claimed','running'))
    )`,CLAIMED_WITHOUT_NATIVE_GRACE_MS,Date.now()+SESSION_REAP_INTERVAL_MS);
    if (next?.due !== null && next?.due !== undefined) {
      if (current !== next.due) { this.db.alarmOperation(); await this.ctx.storage.setAlarm(next.due); }
    } else if (current !== null) {
      this.db.alarmOperation();
      await this.ctx.storage.deleteAlarm();
    }
    });
    this.alarmQueue=pending;
    await pending;
  }

  private async ensureDeadline(due:number):Promise<void>{
    const pending=this.alarmQueue.catch(()=>{}).then(async()=>{
    this.db.alarmOperation();
    const current=await this.ctx.storage.getAlarm();
    if(current===null||current>due){this.db.alarmOperation();await this.ctx.storage.setAlarm(due);}
    });
    this.alarmQueue=pending;
    await pending;
  }

  private async createCommand(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    if (!actor || actor.kind === 'machine') return error('forbidden', 403);
    const session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
    if (!session) return error('session_not_found', 404);
    const body = await bodyObject(request, 12_288);
    const kind = body.kind;
    if (!['send', 'capture', 'stop'].includes(String(kind))) return error('invalid_command_kind', 400);
    const scope = kind === 'send' ? 'session:send' : kind === 'capture' ? 'session:capture' : 'session:stop';
    if (!hasScope(actor, 'operator:admin') && (!hasScope(actor, scope)
      || (actor.resource_id !== sessionId && actor.resource_id !== session.project_id))) return error('forbidden', 403);
    if (kind === 'send' && (this.parse(session.launch_json) as JsonRecord).interactive !== true) return error('headless_send_refused', 409);
    const captureMode = kind === 'capture' ? body.mode : undefined;
    if (captureMode !== undefined && captureMode !== 'screen') return error('invalid_capture_mode', 400);
    if (kind !== 'capture' && body.mode !== undefined) return error('unexpected_capture_mode', 400);
    const commandText = body.text;
    if (kind === 'send' && (typeof commandText !== 'string' || !commandText.trim() || new TextEncoder().encode(commandText).byteLength > 8192)) return error('invalid_command_text', 400);
    if (kind !== 'send' && commandText !== undefined) return error('unexpected_command_text', 400);
    const key = request.headers.get('Idempotency-Key') || body.key;
    if (typeof key !== 'string' || !/^[A-Za-z0-9_.:-]{1,120}$/.test(key)) return error('idempotency_key_required', 400);
    const digest = await sha256Hex(JSON.stringify({ kind, text: commandText ?? null, epoch: session.epoch, ...(captureMode ? {mode:captureMode} : {}) }));
    const immediateStop = kind === 'stop' && !session.native_id && ['offered', 'expired'].includes(session.state);
    const prior = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
    if (prior) {
      if (prior.request_digest !== digest) return error('idempotency_conflict', 409);
      if (!immediateStop || prior.state !== 'pending') return json({ command: this.commandView(prior), replayed: true });
    }
    if (kind === 'stop' ? !immediateStop && !['running', 'claimed'].includes(session.state)
      : session.state !== 'running' || !session.native_id) return error('session_not_running', 409);
    const now = Date.now(), id = crypto.randomUUID();
    if (immediateStop) {
      const applied = await this.recordStopped(session, 'stop_confirmed', now, prior ? undefined : {
        id, request_key: key, request_digest: digest,
      });
      if (!applied) {
        const raced = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
        if (raced?.request_digest === digest) return json({ command: this.commandView(raced), replayed: true });
        return error('session_not_running', 409);
      }
      await this.scheduleNext();
      const command = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
      return command ? json({ command: this.commandView(command), ...(prior ? { replayed: true } : {}) }, prior ? 200 : 201)
        : error('command_persist_failed', 503);
    }
    try {
      this.db.transaction(this.ctx, () => {
        const current = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
        if (!current || current.epoch !== session.epoch || !(kind==='stop'
          ? ['running','claimed'].includes(current.state)
          : current.state==='running'&&!!current.native_id)) throw new HttpError('session_not_running', 409);
        this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,capture_mode,state,result_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,?,?)`, id, sessionId, session.machine_id, session.epoch, key, digest, kind, commandText ?? null, captureMode ?? null, now, now);
      });
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('command_persist_failed', 503);
    }
    await this.notifyPendingCommands(session.machine_id);
    const command = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?', id);
    return command ? json({ command: this.commandView(command) }, 201) : error('command_persist_failed', 503);
  }

  private async ackCommand(request: Request, sessionId: string, commandId: string): Promise<Response> {
    const actor = actorFrom(request), machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const safety=request.headers.get('x-hub2-safety-mode')==='operator_stop';
    if(new URL(request.url).searchParams.has('safety')&&!safety)return error('untrusted_safety_mode',403);
    const body = await bodyObject(request, 12_288);
    if (!Number.isSafeInteger(body.epoch) || !body.result || typeof body.result !== 'object' || Array.isArray(body.result)) return error('invalid_command_ack', 400);
    const resultJson = JSON.stringify(body.result);
    if (new TextEncoder().encode(resultJson).byteLength > 8192) return error('command_ack_too_large', 413);
    const row = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=? AND session_id=? AND machine_id=?', commandId, sessionId, machineId);
    if (!row || row.epoch !== body.epoch) return error('stale_command', 409);
    if(safety&&row.kind!=='stop')return error('safety_command_refused',403);
    if (row.state === 'acked') return row.result_json === resultJson ? json({ acknowledged: true, duplicate: true }) : error('command_ack_conflict', 409);
    if (row.state !== 'pending') return error('command_not_pending', 409);
    const now=Date.now();
    this.db.run("UPDATE session_commands SET state='acked',result_json=?,updated_at=? WHERE id=? AND state='pending' AND epoch=?",
      resultJson, now, commandId, row.epoch);
    if(row.kind==='stop'&&(body.result as JsonRecord).stopped===true){
      const session=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=? AND epoch=?',sessionId,machineId,row.epoch);
      if(session)await this.recordStopped(session,'stop_confirmed',now);
    }
    await this.notifyPendingCommands(machineId);
    return json({ acknowledged: true });
  }

  private async sendPendingCommands(socket:WebSocket,machineId:string,env:Hub2Env=this.env):Promise<void>{
    const stop=this.stopOnly(socket);
    const command=this.db.one<CommandRow>("SELECT c.* FROM session_commands c JOIN sessions s ON s.id=c.session_id AND s.machine_id=c.machine_id AND c.epoch=s.epoch WHERE c.machine_id=? AND s.state IN ('claimed','running') AND c.state='pending' "+(stop?"AND c.kind='stop' ":"")+"ORDER BY CASE WHEN c.kind='stop' THEN 0 ELSE 1 END,c.created_at,c.id LIMIT 1",machineId);
    if(!command)return;
    if(!await this.authorizedAttachment(socket,'machine:offer',true,env))return;
    try{
      const view=this.commandView(command);
      const session=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',command.session_id,machineId);
      if(session?.state==='claimed'&&!session.native_id){
        const launch=this.parse(session.launch_json) as JsonRecord;
        view.pre_native=true;view.workspace=launch.workspace;view.tag=`task-hub2-${session.id}`;
      }
      socket.send(JSON.stringify({type:'session.command',command:view}));
    }catch{}
  }

  private async notifyPendingCommands(machineId: string): Promise<void> {
    for (const socket of this.machineSockets(`${machineId}`)) {
      try {
        await this.sendPendingCommands(socket, machineId);
      } catch {}
    }
  }

  private async queueMail(request: Request): Promise<Response> {
    const body = await bodyObject(request);
    if (!['message_id','operation_id','recipient','session_id','body'].every(key => boundedString(body[key], key === 'body' ? 12000 : 180))
      || !Number.isSafeInteger(body.epoch) || Number(body.epoch) < 1
      || (body.idempotency_key !== undefined && body.idempotency_key !== body.operation_id)) return error('invalid_native_mail', 400);
    const session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', body.session_id);
    if (!session || session.state !== 'running' || !session.native_id) return error('native_mail_target_unavailable', 409);
    const digest = await sha256Hex(JSON.stringify([body.message_id,body.epoch,body.operation_id,body.session_id,body.recipient,body.body]));
    const row = this.db.transaction(this.ctx, () => {
      if (this.db.one('SELECT operation_id FROM native_mail_fences WHERE message_id=? AND epoch=?', body.message_id, body.epoch)) throw new HttpError('native_mail_fenced', 409);
      const prior = this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?', body.message_id, body.epoch);
      if (prior) {
        if (prior.digest !== digest) throw new HttpError('native_mail_conflict', 409);
        return prior;
      }
      this.db.run("INSERT INTO native_mail(message_id,epoch,operation_id,session_id,machine_id,recipient,body,digest,state,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',?)",
        body.message_id,body.epoch,body.operation_id,session.id,session.machine_id,body.recipient,body.body,digest,Date.now());
      return this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?', body.message_id, body.epoch)!;
    });
    await this.notifyPendingMail(session.machine_id);
    return json({queued:true,state:row.state,message_id:row.message_id,epoch:row.epoch,operation_id:row.operation_id});
  }

  private async settleMail(request: Request, cancel: boolean): Promise<Response> {
    const body = await bodyObject(request, 8192);
    if (!boundedString(body.message_id,180) || !boundedString(body.operation_id,180)
      || !Number.isSafeInteger(body.epoch) || Number(body.epoch)<1) return error('invalid_native_mail_ack',400);
    const proof = body.native_receipt as JsonRecord | undefined;
    if (!cancel && (!proof || proof.provider!=='aplexer' || !boundedString(proof.message_id,180)
      || !boundedString(proof.session_id,180) || !boundedString(proof.recipient,180))) return error('native_mail_receipt_required',400);
    const digest = cancel ? null : await sha256Hex(JSON.stringify([proof!.provider,proof!.message_id,proof!.session_id,proof!.recipient]));
    let machine: string | undefined;
    this.db.transaction(this.ctx, () => {
      const row = this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?',body.message_id,body.epoch);
      if (row && row.operation_id !== body.operation_id) throw new HttpError('native_mail_operation_conflict',409);
      if (cancel) {
        if (row?.state==='acked') throw new HttpError('cannot_cancel_delivered',409);
        const fence = this.db.one<{operation_id:string}>('SELECT operation_id FROM native_mail_fences WHERE message_id=? AND epoch=?',body.message_id,body.epoch);
        if (fence && fence.operation_id!==body.operation_id) throw new HttpError('native_mail_operation_conflict',409);
        if (!fence) this.db.run('INSERT INTO native_mail_fences VALUES(?,?,?)',body.message_id,body.epoch,body.operation_id);
        if (row && row.state!=='cancelled') this.db.run("UPDATE native_mail SET state='cancelled' WHERE message_id=? AND epoch=?",body.message_id,body.epoch);
      } else {
        if (!row) throw new HttpError('native_mail_not_found',404);
        if (row.state==='cancelled' || proof!.session_id!==row.session_id || proof!.recipient!==row.recipient) throw new HttpError('native_mail_fenced',409);
        if (row.receipt_digest && row.receipt_digest!==digest) throw new HttpError('native_mail_receipt_conflict',409);
        if (row.state!=='acked') this.db.run("UPDATE native_mail SET state='acked',receipt_digest=? WHERE message_id=? AND epoch=?",digest,body.message_id,body.epoch);
      }
      machine=row?.machine_id;
    });
    if (machine) await this.notifyPendingMail(machine);
    return json({acknowledged:true,acked:!cancel,cancelled:cancel,state:cancel?'cancelled':'acked',
      message_id:body.message_id,epoch:body.epoch,operation_id:body.operation_id});
  }

  private async sendPendingMail(socket: WebSocket, machine: string,env:Hub2Env=this.env): Promise<void> {
    if(this.stopOnly(socket))return;
    if (!await this.authorizedAttachment(socket,'message:delivery',false,env)) return;
    const row = this.db.one<NativeMail>("SELECT m.* FROM native_mail m JOIN sessions s ON s.id=m.session_id AND s.machine_id=m.machine_id WHERE m.machine_id=? AND s.state='running' AND s.native_id IS NOT NULL AND m.state='pending' ORDER BY m.created_at,m.message_id,m.epoch LIMIT 1",machine);
    if (row) socket.send(JSON.stringify({type:'mailbox.deliver',message:{id:row.message_id,epoch:row.epoch,
      recipient:row.recipient,session_id:row.session_id,operation_id:row.operation_id,body:row.body}}));
  }

  private async notifyPendingMail(machine: string): Promise<void> {
    for (const socket of this.machineSockets(`${machine}`)) {
      try { await this.sendPendingMail(socket,machine); } catch {}
    }
  }

  private async recordAttention(socket: WebSocket, body: JsonRecord,env:Hub2Env): Promise<void> {
    if (!boundedString(body.session_id,120) || typeof body.reason!=='string' || !/^[a-z_]{1,80}$/.test(body.reason)) return;
    const attachment = await this.authorizedAttachment(socket,'machine:offer',true,env);
    if (!attachment) return;
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',body.session_id,attachment.machine_id);
    if (!row) return;
    const operation=boundedString(body.operation_id,180)?body.operation_id:null,message=boundedString(body.message_id,180)?body.message_id:null;
    const prior=this.db.one<{reason:string;operation_id:string|null;message_id:string|null}>('SELECT reason,operation_id,message_id FROM attention WHERE session_id=?',row.id);
    if (!prior || prior.reason!==body.reason || prior.operation_id!==operation || prior.message_id!==message) {
      this.db.run('INSERT INTO attention VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=excluded.message_id,observed_at=excluded.observed_at',
        row.id,body.reason,operation,message,Date.now());
      console.error('[hub2] MACHINE_SESSION_ATTENTION',JSON.stringify({session_id:row.id,machine_id:row.machine_id,reason:body.reason,operation_id:operation}));
    }
    // Command and mailbox failures are diagnostics, even if they carry the
    // same operation ID. Older daemons omit stage but include the command or
    // message ID; their launch refusals carry neither.
    if((body.stage!=null&&body.stage!=='host_launch')||body.message_id!=null)return;
    if(AUTH_FALLBACK_REASONS.has(body.reason)&&['offered','claimed'].includes(row.state)&&!row.native_id&&operation===row.operation_id){
      const now=Date.now(),payload={operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,
        machine_id:row.machine_id,epoch:row.epoch,reason:body.reason};
      this.db.transaction(this.ctx,()=>{
        this.db.run("UPDATE sessions SET state='expired',updated_at=? WHERE id=? AND operation_id=? AND epoch=? AND state IN ('offered','claimed')",now,row.id,row.operation_id,row.epoch);
        this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",row.operation_id,row.id,row.epoch);
        this.insertOutbox(`auth-fallback:${row.operation_id}:${row.epoch}`,'auth-fallback',row.operation_id,row.project_id,payload,now);
      });
      await this.ensureDeadline(now+1000);
      await this.flushOne(`auth-fallback:${row.operation_id}:${row.epoch}`);
      await this.scheduleNext();
    } else if(['offered','claimed'].includes(row.state)&&!row.native_id&&operation===row.operation_id){
      await this.recordLaunchRefusal(row,body.reason);
    }
  }

  private async recordLaunchRefusal(session:SessionRow,failureReason:string):Promise<void>{
    const now=Date.now(),reason='host_launch_refused',payload={operation_id:session.dispatch_id,
      attempt_operation_id:session.operation_id,session_id:session.id,intake_id:session.intake_id,
      machine_id:session.machine_id,epoch:session.epoch,native_id:null,reason,failure_reason:failureReason,
      launch_failed:true,observed_at:now};
    const key=`launch-refused:${session.operation_id}:${session.epoch}`;
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||!['offered','claimed'].includes(current.state)||current.native_id!==null||current.operation_id!==session.operation_id||current.epoch!==session.epoch)return false;
      this.db.run("UPDATE sessions SET state='failed',updated_at=? WHERE id=? AND operation_id=? AND epoch=? AND state IN ('offered','claimed')",
        now,session.id,session.operation_id,session.epoch);
      this.db.run("UPDATE machine_session_operations SET state='failed' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",
        session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(current,now,reason,false);
      this.insertOutbox(key,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(!applied)return;
    await this.flushOne(key);
    await this.scheduleNext();
    console.error('[hub2] SESSION_LAUNCH_REFUSED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,
      intake_id:session.intake_id,epoch:session.epoch,reason:failureReason}));
  }

  private machineView(): Response {
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const sessions=machine?this.db.all("SELECT id,state,project_id,intake_id,native_id,updated_at FROM sessions WHERE machine_id=? AND state IN ('staged','offered','claimed','running') LIMIT 101",machine.machine_id):[];
    return json({machine:machine?{id:machine.machine_id,alias:machine.alias,os:machine.os,arch:machine.arch,
      labels:this.parse(machine.labels_json),last_seen:machine.last_seen,connected:this.machineSockets(`${machine.machine_id}`).length>0}:null,
      sessions,live_active_count:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,
      attention:this.db.all('SELECT * FROM attention ORDER BY observed_at DESC,session_id LIMIT 20'),
      pending_notifications:this.db.all("SELECT id,kind,attempt,next_action_at FROM machine_outbox WHERE state='pending' ORDER BY next_action_at,id LIMIT 21")});
  }

  private liveSessions():LiveSession[]{
    return this.db.all<Omit<LiveSession,'containment_empty'>&{containment_empty:number}>(
      'SELECT session_id,native_id,phase,last_activity_at,observed_at,containment_empty FROM native_sessions ORDER BY session_id LIMIT 100')
      .map(row=>({...row,containment_empty:row.containment_empty===1}));
  }

  private liveSessionView():Response{
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const snapshot=this.db.one<{observed_at:number}>('SELECT observed_at FROM machine_liveness WHERE singleton=1');
    return json({machine_id:machine?.machine_id??null,observed_at:snapshot?.observed_at??null,sessions:this.liveSessions()});
  }

  private async reap(request:Request):Promise<Response>{
    const body=await bodyObject(request,4096);
    if(body.dry_run!==undefined&&typeof body.dry_run!=='boolean')return error('invalid_reap_request',400);
    return json(await this.reapInactive(body.dry_run===true));
  }

  private async reapInactive(dryRun:boolean):Promise<JsonRecord>{
    const now=Date.now(),machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const inventory=this.db.one<{observed_at:number}>('SELECT observed_at FROM machine_liveness WHERE singleton=1');
    const sessions=this.liveSessions(),active=sessions.filter(row=>isLiveActiveSession(row,now)).length;
    const rows=this.db.all<SessionRow>("SELECT * FROM sessions WHERE state IN ('claimed','running') ORDER BY updated_at,id LIMIT 200");
    const confirmedStops=rows.filter(session=>this.stopConfirmed(session));
    const timedOutClaims=rows.filter(session=>session.state==='claimed'&&!session.native_id
      &&now-session.updated_at>=CLAIMED_WITHOUT_NATIVE_GRACE_MS&&!this.stopConfirmed(session));
    if(!machine||!inventory||inventory.observed_at>now+5_000||now-inventory.observed_at>LIVE_SNAPSHOT_TTL_MS){
      if(dryRun)return {machine_id:machine?.machine_id??null,dry_run:true,active_count:active,reaped:0,stop_requested:0,
        skipped:'host_liveness_stale',candidates:[...confirmedStops.map(session=>({session_id:session.id,native_id:session.native_id,reason:'stop_confirmed'})),
          ...timedOutClaims.map(session=>({session_id:session.id,native_id:null,reason:'launch_timeout_no_native'}))]};
      let reaped=0;
      for(const session of confirmedStops)if(await this.recordStopped(session,'stop_confirmed',now))reaped++;
      for(const session of rows){
        if(session.state!=='claimed'||session.native_id||this.stopConfirmed(session)
          ||now-session.updated_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)continue;
        if(await this.recordLaunchTimeout(session,now))reaped++;
      }
      return {machine_id:machine?.machine_id??null,dry_run:false,active_count:active,active_count_after:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,
        reaped,stop_requested:0,skipped:'host_liveness_stale'};
    }
    const bySession=new Map(sessions.map(row=>[row.session_id,row]));
    const candidates:Array<{session:SessionRow;reason:'dead'|'idle';live:LiveSession|null}> = [];
    for(const session of rows){
      if(this.stopConfirmed(session))continue;
      const live=bySession.get(session.id)||null;
      if(!session.native_id){
        const starting=live&&['launching','starting'].includes(live.phase);
        if(starting&&now-session.updated_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)continue;
        if(now-session.updated_at>=CLAIMED_WITHOUT_NATIVE_GRACE_MS)candidates.push({session,reason:'dead',live});
        continue;
      }
      if(session.native_id&&!live){candidates.push({session,reason:'dead',live:null});continue;}
      if(live){
        const reason=inactiveReason({...live,last_activity_at:live.last_activity_at??session.created_at},now);
        if(reason)candidates.push({session,reason,live});
      }
    }
    if(dryRun)return {machine_id:machine.machine_id,dry_run:true,active_count:active,reaped:0,stop_requested:0,
      candidates:[...confirmedStops.map(session=>({session_id:session.id,native_id:session.native_id,reason:'stop_confirmed'})),
        ...candidates.map(({session,reason,live})=>({session_id:session.id,native_id:session.native_id,reason,
          last_activity_at:live?.last_activity_at??null,observed_at:live?.observed_at??inventory.observed_at}))]};
    let reaped=0,stopRequested=0;const actions:JsonRecord[]=[];
    for(const session of confirmedStops){
      if(await this.recordStopped(session,'stop_confirmed',now)){reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason:'stop_confirmed'});}
    }
    for(const candidate of candidates){
      const {session,reason,live}=candidate;
      if(session.state==='claimed'&&!session.native_id&&!live?.native_id){
        if(await this.recordLaunchTimeout(session,live?.observed_at??inventory.observed_at)){
          reaped++;actions.push({session_id:session.id,action:'marked_failed',reason:'launch_timeout_no_native'});
        }
        continue;
      }
      if(!live){
        if(await this.recordReaped(session,reason,inventory.observed_at)){
          reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason});
        }
        continue;
      }
      if(live&&['exited','missing','dead','stopped'].includes(live.phase)&&live.containment_empty===true){
        if(await this.recordReaped(session,reason,live.observed_at)){reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason});}
        continue;
      }
      if(session.native_id||live?.native_id||!!(live&&['launching','starting'].includes(live.phase))){
        const pending=this.db.one<{id:string}>("SELECT id FROM session_commands WHERE session_id=? AND machine_id=? AND epoch=? AND kind='stop' AND state='pending' ORDER BY created_at DESC LIMIT 1",
          session.id,session.machine_id,session.epoch);
        if(pending)continue;
        const actor={id:'hub2-session-reaper',kind:'operator',scopes:['operator:admin']};
        const command=await this.createCommand(new Request(`https://machine.internal/api/sessions/${session.id}/commands`,{
          method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},
          body:JSON.stringify({kind:'stop',key:`reap:${session.id}:${session.epoch}:${Math.floor(now/SESSION_REAP_INTERVAL_MS)}`}),
        }),session.id);
        if(command.status===201){
          stopRequested++;actions.push({session_id:session.id,action:'stop_requested',reason});
          console.info('[hub2] SESSION_REAP_STOP_REQUESTED',JSON.stringify({session_id:session.id,machine_id:machine.machine_id,reason}));
        }else if(!command.ok){
          console.error('[hub2] SESSION_REAP_STOP_FAILED',JSON.stringify({session_id:session.id,machine_id:machine.machine_id,reason,status:command.status}));
        }
        continue;
      }
      if(await this.recordReaped(session,reason,live?.observed_at??inventory.observed_at)){reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason});}
    }
    return {machine_id:machine.machine_id,dry_run:false,active_count:active,
      active_count_after:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,reaped,stop_requested:stopRequested,actions};
  }

  private stopConfirmed(session:SessionRow):boolean{
    const row=this.db.one<{result_json:string|null}>("SELECT result_json FROM session_commands WHERE session_id=? AND machine_id=? AND epoch=? AND kind='stop' AND state='acked' ORDER BY created_at DESC LIMIT 1",
      session.id,session.machine_id,session.epoch);
    try{return !!row?.result_json&&(JSON.parse(row.result_json) as JsonRecord).stopped===true;}catch{return false;}
  }

  private async recordStopped(session:SessionRow,reason:string,observedAt:number,
    stopCommand?:{id:string;request_key:string;request_digest:string}):Promise<boolean>{
    const now=Date.now(),payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:session.native_id,
      reason,observed_at:observedAt};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      const unclaimed=current&&['offered','expired'].includes(current.state)&&current.native_id===null;
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id||current.native_id!==session.native_id
        ||(!['claimed','running'].includes(current.state)&&!unclaimed))return false;
      if(stopCommand)this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?, 'stop',NULL,'pending',NULL,?,?)`,stopCommand.id,session.id,session.machine_id,session.epoch,
        stopCommand.request_key,stopCommand.request_digest,now,now);
      this.db.run("UPDATE sessions SET state='stopped',updated_at=? WHERE id=?",now,session.id);
      this.db.run("UPDATE machine_session_operations SET state='stopped' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(session,now,'stop_confirmed',true);
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,?, ?,NULL,1,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase=excluded.phase,last_activity_at=excluded.last_activity_at,containment_empty=1,observed_at=excluded.observed_at`,
        session.id,session.native_id,session.native_id?'exited':'missing',observedAt,now);
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
      this.insertOutbox(`stopped:${session.operation_id}:${session.epoch}`,'stopped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(applied)await this.flushOne(`stopped:${session.operation_id}:${session.epoch}`);
    if(applied)console.info('[hub2] SESSION_REAPED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,reason:'stop_confirmed'}));
    return applied;
  }

  private async recordReaped(session:SessionRow,reason:'dead'|'idle',observedAt:number):Promise<boolean>{
    const now=Date.now(),payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:session.native_id,
      reason,observed_at:observedAt,stopped:true,reaped:true};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id||!['claimed','running'].includes(current.state))return false;
      this.db.run("UPDATE sessions SET state='stopped',updated_at=? WHERE id=?",now,session.id);
      this.db.run("UPDATE machine_session_operations SET state='reaped' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(session,now,reason,true);
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,'missing',?,NULL,1,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase='missing',last_activity_at=excluded.last_activity_at,containment_empty=1,observed_at=excluded.observed_at`,
        session.id,session.native_id,observedAt,now);
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
      this.insertOutbox(`reaped:${session.operation_id}:${session.epoch}`,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(applied)await this.flushOne(`reaped:${session.operation_id}:${session.epoch}`);
    if(applied)console.info('[hub2] SESSION_REAPED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,reason}));
    return applied;
  }

  private async recordLaunchTimeout(session:SessionRow,observedAt:number):Promise<boolean>{
    const now=Date.now(),reason=session.state==='claimed'?'launch_timeout_no_native':'host_no_response';
    const previousAttention=this.db.one<{reason:string}>('SELECT reason FROM attention WHERE session_id=?',session.id)?.reason??null;
    const payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:null,
      reason,launch_failed:true,observed_at:observedAt};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id
        ||!['staged','offered','claimed'].includes(current.state)||current.native_id!==null||Date.now()-current.created_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)return false;
      this.db.run("UPDATE sessions SET state='failed',updated_at=? WHERE id=? AND state IN ('staged','offered','claimed') AND native_id IS NULL",now,session.id);
      this.db.run("UPDATE machine_session_operations SET state='failed' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(session,now,reason,false);
      this.db.run('INSERT INTO attention(session_id,reason,operation_id,message_id,observed_at) VALUES(?,?,?,NULL,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=NULL,observed_at=excluded.observed_at',
        session.id,reason,session.operation_id,now);
      this.insertOutbox(`reaped:${session.operation_id}:${session.epoch}`,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(applied)await this.flushOne(`reaped:${session.operation_id}:${session.epoch}`);
    if(applied)console.error('[hub2] SESSION_LAUNCH_TIMEOUT',JSON.stringify({session_id:session.id,machine_id:session.machine_id,
      intake_id:session.intake_id,epoch:session.epoch,reason,previous_attention:previousAttention}));
    return applied;
  }

  private settlePendingCommands(session:SessionRow,now:number,reason:string,stopConfirmed:boolean):void{
    const stopResult=JSON.stringify({stopped:true,reason});
    const unconfirmedStopResult=JSON.stringify({error:'stop_unconfirmed',reason});
    const failedResult=JSON.stringify({error:'session_ended_before_command_execution',reason});
    this.db.run(`UPDATE session_commands SET state=CASE WHEN kind='stop' AND ? THEN 'acked' ELSE 'failed' END,
      result_json=CASE WHEN kind='stop' AND ? THEN ? WHEN kind='stop' THEN ? ELSE ? END,updated_at=?
      WHERE session_id=? AND machine_id=? AND epoch=? AND state='pending'`,
      stopConfirmed?1:0,stopConfirmed?1:0,stopResult,unconfirmedStopResult,failedResult,now,session.id,session.machine_id,session.epoch);
  }

  private sessionList(request: Request): Response {
    const actor=actorFrom(request);
    if (!hasScope(actor,'operator:admin')) return error('forbidden',403);
    const query=new URL(request.url).searchParams,after=query.get('after')||'',limit=Number(query.get('limit')||25);
    if (!Number.isInteger(limit)||limit<1||limit>100||after.length>120) return error('invalid_page',400);
    const rows=this.db.all<SessionRow>(query.get('live')==='1'
      ? "SELECT * FROM sessions WHERE id>? AND state IN ('staged','offered','claimed','running') ORDER BY id LIMIT ?"
      : 'SELECT * FROM sessions WHERE id>? ORDER BY id LIMIT ?',after,limit+1);
    return json({items:rows.slice(0,limit).map(row=>this.sessionView(row)),next:rows.length>limit?rows[limit-1].id:null});
  }

  /** Hub1-shaped `POST /api/sessions/:id/(stop|send|capture)` mapped onto the Hub2 command queue, so the CLI and MCP keep working. */
  private async legacyCommand(request: Request, sessionId: string, kind: string): Promise<Response> {
    const body = await bodyObject(request, 12_288);
    const mapped: JsonRecord = { kind, key: body.key };
    if (kind === 'send') mapped.text = body.body;
    if (kind === 'capture' && body.mode !== undefined) mapped.mode = body.mode;
    const response = await this.createCommand(new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify(mapped) }), sessionId);
    if (!response.ok) return response;
    const data = await response.json() as { command: { id: string } };
    const command=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?',data.command.id);
    const result=this.parse(command?.result_json) as JsonRecord|null;
    const stopped=kind==='stop'&&result?.stopped===true;
    return json({ ...data, command_id: data.command.id,
      state: stopped?'stopped':command?.state==='acked'?'acked':'queued',
      delivery: stopped?'confirmed':command?.state==='acked'?'delivered':'queued' }, response.status);
  }

  /** Command receipts in the shape `sessions send/capture/stop --wait` polls: ack.capture, ack.outcome, ack.confirmed_exit. */
  private commandListing(request: Request, sessionId: string): Response {
    const actor = actorFrom(request), session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
    if (!session || (!hasScope(actor, 'operator:admin') && !(hasScope(actor, 'session:read') && [session.id, session.project_id].includes(actor?.resource_id || '')))) return error('session_not_found', 404);
    const rows = this.db.all<CommandRow>('SELECT * FROM session_commands WHERE session_id=? ORDER BY created_at DESC,id LIMIT 100', sessionId);
    return json({ commands: rows.map(row => {
      const result = row.result_json ? JSON.parse(row.result_json) as JsonRecord : null;
      const ack = result === null ? null : {
        ...result,
        ...(row.kind === 'capture' && typeof result.output === 'string' ? { capture: result.output } : {}),
        ...(row.kind === 'stop' ? { outcome: 'delivered', confirmed_exit: result.stopped === true } : {}),
      };
      return { id: row.id, session_id: row.session_id, kind: row.kind, state: row.state, created_at: row.created_at, ack };
    }), next_cursor: null });
  }

  private sessionDetail(request: Request, id: string): Response {
    const actor=actorFrom(request),row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',id);
    if (!row || (!hasScope(actor,'operator:admin') && !(hasScope(actor,'session:read') && [row.id,row.project_id].includes(actor?.resource_id||'')))) return error('session_not_found',404);
    const stopped = this.db.one<{result_json: string | null}>("SELECT result_json FROM session_commands WHERE session_id=? AND kind='stop' AND state='acked' ORDER BY created_at DESC LIMIT 1", row.id);
    return json({session:{...this.sessionView(row), ...(stopped?.result_json && (JSON.parse(stopped.result_json) as JsonRecord).stopped === true ? { stop_confirmed: true } : {})}});
  }

  private sessionView(row: SessionRow): JsonRecord {
    const launch=this.parse(row.launch_json) as JsonRecord;
    const picker=normalizeStartPickerEvidence(launch._hub_picker);
    return {id:row.id,state:row.state,intake_id:row.intake_id,project_id:row.project_id,machine_id:row.machine_id,
      epoch:row.epoch,operation_id:row.operation_id,native_id:row.native_id,engine:launch.engine,model:launch.model,
      managed:true,host:row.machine_id,machine:row.machine_id,workspace:launch.workspace,
      start_deadline_at:launch.start_deadline_at??null,
      account_id:row.account_id,account:row.account_id,effort:launch.effort??null,
      ...(picker ? { picker, policy_id: picker.policy_id, policy_version: picker.policy_version } : {}),
      mode:launch.interactive===true?'interactive':'headless',
      created_at:new Date(row.created_at).toISOString(),updated_at:new Date(row.updated_at).toISOString(),
      last_activity_at:this.db.one<{last_activity_at:number|null}>('SELECT last_activity_at FROM native_sessions WHERE session_id=?',row.id)?.last_activity_at??null,
      interactive:launch.interactive,evidence:this.parse(row.evidence_json),attention:this.db.one('SELECT reason,observed_at FROM attention WHERE session_id=?',row.id)||null};
  }

  /** Hibernation attachments retain selection across eviction; prune legacy duplicates before RPCs. */
  private machineSockets(machineId:string,prune=true):WebSocket[]{
    const sockets=this.ctx.getWebSockets('hub2:'+machineId).filter(socket=>socket.readyState===1);
    const rank=(socket:WebSocket):string=>{
      try{const a=socket.deserializeAttachment();return String(a?.connected_at??0).padStart(16,'0')+':'+String(a?.connection_id??'');}
      catch{return '';}
    };
    sockets.sort((a,b)=>rank(b).localeCompare(rank(a)));
    if(prune)for(const old of sockets.slice(1)){try{old.close(1000,'connection superseded');}catch{}}
    return sockets.slice(0,1);
  }

  private currentSocket(socket:WebSocket,prune=true):boolean{
    try{const machine=socket.deserializeAttachment()?.machine_id;
      return typeof machine==='string'&&this.machineSockets(machine,prune)[0]===socket;
    }catch{return false;}
  }

  private stopOnly(socket:WebSocket):boolean{try{return socket.deserializeAttachment()?.stop_only===true;}catch{return false;}}

  private async authorizedAttachment(socket: WebSocket, scope: string, closeOnRefusal = true,env:Hub2Env=this.env,prune=true): Promise<SocketAttachment | undefined> {
    if(!this.currentSocket(socket,prune))return undefined;
    let attachment: Partial<SocketAttachment> | null;
    try { attachment = socket.deserializeAttachment(); } catch { attachment = null; }
    if (!attachment?.machine_id || !attachment.actor_id || !attachment.generation) return undefined;
    const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
    const response = await control.fetch('https://control.hub2.internal/_authorize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor_id: attachment.actor_id, generation: attachment.generation, scope }),
    });
    if (!response.ok) {
      if (closeOnRefusal) { try { socket.close(1008, 'credential revoked'); } catch {} }
      return undefined;
    }
    if(!this.currentSocket(socket,prune))return undefined;
    return attachment as SocketAttachment;
  }

  private commandView(row: CommandRow): JsonRecord {
    return { id: row.id, session_id: row.session_id, epoch: row.epoch, kind: row.kind,
      ...(row.capture_mode ? {mode:row.capture_mode} : {}),
      ...(row.text === null ? {} : { text: row.text }) };
  }

  private machineActor(actor: Actor | undefined, scope: string): string | undefined {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id || !actor.scopes.includes(scope)) return undefined;
    return actor.resource_id;
  }

  private parse(value: unknown): unknown {
    try { return JSON.parse(String(value ?? 'null')); } catch { return null; }
  }
}
