import {PickerAlerts,PICKER_ALERT_SCHEMA} from './picker-alerts';
import {pickModelRouteDetailed,type PickInput} from './model-picker';
import {buildStartPickerEvidence} from './start-picker-evidence';
import {SharedMemory,SHARED_MEMORY_SCHEMA} from './shared-memory';
import {WorkerCredentials,WORKER_CREDENTIAL_SCHEMA} from './worker-credentials';
import {ControlAuth,CONTROL_AUTH_SCHEMA} from './control-auth';
import {verifyKnownCredential} from './auth';
import { MeteredDb, initializeSchema, schemaMigrationCeiling } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { randomToken, safeEqual, sha256Hex } from './crypto';
import { credentialKey, openCredential, sealCredential } from './credential-crypto';
import { actorFrom, bodyObject, boundedString, boundedText, error, hasScope, HttpError, HUB2_FORWARD_BODY_LIMIT, json } from './http';
import { modelPolicyCandidateViolation, type ModelPolicyRoute } from './model-policy';
import { isLiveActiveSession, MAX_ACTIVE_SESSIONS } from './session-liveness';
import { CLAUDE_OAUTH, claudeAuthorization, claudeExchange } from './claude-oauth';
import type { Actor, Hub2Env, JsonRecord } from './types';

type CredentialActor=Actor&{family_id?:string;client_id?:string};
const PICKER_COVERAGE_EPOCH = crypto.randomUUID();

function canonicalDraw(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonicalDraw).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    const object=value as Record<string,unknown>;
    return '{'+Object.keys(object).sort().map(key=>JSON.stringify(key)+':'+canonicalDraw(object[key])).join(',')+'}';
  }
  return JSON.stringify(value);
}
const SCHEMA = [
 'CREATE TABLE mailbox_wakes(owner TEXT PRIMARY KEY,revision INTEGER NOT NULL,after_id TEXT NOT NULL,next_attempt INTEGER NOT NULL,attempt INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID',
 'CREATE INDEX mailbox_wake_due ON mailbox_wakes(next_attempt,owner)',
 ...CONTROL_AUTH_SCHEMA,...WORKER_CREDENTIAL_SCHEMA,
 'CREATE TABLE model_policy(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,candidates_json TEXT NOT NULL,digest TEXT NOT NULL)',
 'CREATE TABLE actor_directory(id TEXT PRIMARY KEY,kind TEXT NOT NULL,enabled INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE TABLE named_agents(id TEXT PRIMARY KEY,display_name TEXT NOT NULL,forward_session TEXT,enabled INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID',
 'CREATE INDEX active_named_directory ON named_agents(enabled,id)',
 'CREATE TABLE agent_grants(agent_id TEXT NOT NULL,family_id TEXT NOT NULL,client_id TEXT NOT NULL,can_send INTEGER NOT NULL,can_read INTEGER NOT NULL,enabled INTEGER NOT NULL,PRIMARY KEY(agent_id,family_id,client_id)) WITHOUT ROWID',
 'CREATE INDEX grant_subject ON agent_grants(family_id,client_id,enabled,agent_id)',
 'CREATE TABLE object_routes(kind TEXT NOT NULL,id TEXT NOT NULL,owner TEXT NOT NULL,resource_id TEXT NOT NULL,bound_intake_id TEXT,epoch INTEGER NOT NULL,digest TEXT NOT NULL,participants_json TEXT NOT NULL,PRIMARY KEY(kind,id)) WITHOUT ROWID',
 'CREATE INDEX route_intake ON object_routes(bound_intake_id,kind,id) WHERE bound_intake_id IS NOT NULL',
 'CREATE INDEX route_resource ON object_routes(resource_id,kind,id)',
 'CREATE TABLE route_acl(actor_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(actor_id,kind,id)) WITHOUT ROWID',
 'CREATE TABLE session_start_reservations(operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,expires_at INTEGER NOT NULL) WITHOUT ROWID',

  `CREATE TABLE credentials (
    actor_id TEXT PRIMARY KEY, kind TEXT NOT NULL, resource_id TEXT, generation TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, scopes_json TEXT NOT NULL,
    family_id TEXT NOT NULL,client_id TEXT NOT NULL DEFAULT '',expires_at INTEGER,principal_email TEXT,refresh_hash TEXT,csrf_token TEXT,created_at INTEGER NOT NULL, revoked_at INTEGER
  )`,
  'CREATE UNIQUE INDEX credentials_refresh ON credentials(refresh_hash) WHERE refresh_hash IS NOT NULL',
  'CREATE INDEX credentials_family_generation ON credentials(resource_id,generation,revoked_at)',
  'CREATE INDEX credentials_resource ON credentials(kind, resource_id, revoked_at)',
  "CREATE INDEX active_machine_directory ON credentials(resource_id) WHERE kind='machine' AND revoked_at IS NULL",
  `CREATE TABLE device_pairings (
    id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, alias TEXT NOT NULL,
    code_hash TEXT NOT NULL UNIQUE, scopes_json TEXT NOT NULL,
    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER
  )`,
  'CREATE INDEX device_pairings_expiry ON device_pairings(expires_at, id)',
  `CREATE TABLE repos (
    canonical_name TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','archived','duplicate')),
    duplicate_of TEXT, domains_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX repos_status_name ON repos(status, canonical_name)',
  'CREATE INDEX repos_duplicate ON repos(duplicate_of, canonical_name)',
  `CREATE TABLE repo_aliases (
    alias TEXT PRIMARY KEY, canonical_name TEXT NOT NULL REFERENCES repos(canonical_name)
  )`,
  'CREATE INDEX repo_aliases_owner_alias ON repo_aliases(canonical_name, alias)',
];

const ONCALL_SCHEMA = [
 'CREATE TABLE oncall_alert_config(singleton INTEGER PRIMARY KEY CHECK(singleton=1),ingest_token_hash TEXT NOT NULL,poll_token_hash TEXT NOT NULL,created_at INTEGER NOT NULL)',
 'CREATE TABLE oncall_alert_deliveries(id TEXT PRIMARY KEY,delivery_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,payload_digest TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN (\'pending\',\'leased\',\'saved\')),attempts INTEGER NOT NULL DEFAULT 0,available_at INTEGER NOT NULL,lease_token_hash TEXT,lease_until INTEGER,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,saved_at INTEGER)',
 'CREATE INDEX oncall_alerts_due ON oncall_alert_deliveries(status,available_at,lease_until,created_at)',
];

const ENGINE_SCHEMA = [
 'CREATE TABLE IF NOT EXISTS hub2_engine_credential_logins(id TEXT PRIMARY KEY,label TEXT NOT NULL,provider TEXT NOT NULL,sealed TEXT NOT NULL,expires_at INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE INDEX IF NOT EXISTS hub2_engine_login_expiry ON hub2_engine_credential_logins(expires_at,id)',
 'CREATE TABLE IF NOT EXISTS hub2_engine_rollout(engine TEXT PRIMARY KEY,desired_version TEXT NOT NULL,latest TEXT NOT NULL,rollout_id TEXT,target_version TEXT,previous_version TEXT,canary_machine TEXT,state TEXT NOT NULL,updated_at INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE TABLE IF NOT EXISTS hub2_engine_version_reports(machine_id TEXT NOT NULL,engine TEXT NOT NULL,installed TEXT,latest TEXT,result TEXT,updated_at INTEGER NOT NULL,PRIMARY KEY(machine_id,engine)) WITHOUT ROWID',
 'CREATE TABLE IF NOT EXISTS hub2_engine_sync_reports(machine_id TEXT NOT NULL,label TEXT NOT NULL,revision TEXT NOT NULL,state TEXT NOT NULL,error TEXT,credential_generation INTEGER,updated_at INTEGER NOT NULL,PRIMARY KEY(machine_id,label)) WITHOUT ROWID',
];

const CLAUDE_ENGINE_LABEL = 'hub-claude';
const CLAUDE_ACCOUNT_ID = 'claude';
const CLAUDE_BASELINE_VERSION = '2.1.312';
const CLAUDE_LATEST_VERSION = '2.1.322';
const ENGINE_LOGIN_TTL_MS = 10 * 60_000;

type PairingRow = { id: string; machine_id: string; alias: string; code_hash: string; scopes_json: string; expires_at: number; used_at: number | null };
type CredentialRow = { actor_id: string; kind: Actor['kind']; resource_id: string | null; generation: string; scopes_json: string;family_id:string;client_id:string;expires_at:number|null;principal_email:string|null;revoked_at:number|null };

/** Small global identity, pairing and canonical-repository directory. */
export class ControlDO {
  private readonly db: MeteredDb;
  private readonly sqlReceipts: SqlReceiptMeter;
  private readonly env: Hub2Env;
  private readonly auth:ControlAuth;
  private readonly workers:WorkerCredentials;
  private readonly memory:SharedMemory;
  private readonly pickerAlerts:PickerAlerts;
  private readonly ready:Promise<void>;
  private pickerQueue:Promise<void> = Promise.resolve();
  private pickerObserverDirty = false;
  private pickerObserverEpoch = PICKER_COVERAGE_EPOCH;

  constructor(private readonly ctx: DurableObjectState, env: Hub2Env) {
    this.db = new MeteredDb(ctx.storage.sql);
    this.sqlReceipts = new SqlReceiptMeter(this.db);
    this.env = wrapSqlNamespaces(env, this.sqlReceipts) as Hub2Env;
    this.pickerAlerts=new PickerAlerts(this.db,ctx);
    this.ready=initializeSchema(ctx, this.db, 6, SCHEMA,[ONCALL_SCHEMA,['CREATE TABLE IF NOT EXISTS session_start_reservations(operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,expires_at INTEGER NOT NULL) WITHOUT ROWID', ...ENGINE_SCHEMA],ENGINE_SCHEMA,SHARED_MEMORY_SCHEMA,PICKER_ALERT_SCHEMA],async()=>{
      this.seedEngineRollout();
      if(this.db.schemaVersion>=6){
        try{this.pickerAlerts.beginCoverage(PICKER_COVERAGE_EPOCH);}
        catch{this.invalidatePickerCoverage();}
      }
      await this.scheduleWake();
    },1,schemaMigrationCeiling(env.HUB2_SCHEMA_MIGRATIONS,4,6));
    this.memory=new SharedMemory(ctx,this.db,this.env);
    this.auth=new ControlAuth(ctx,this.db,this.env);this.workers=new WorkerCredentials(ctx,this.db,this.env);
  }

  async fetch(request: Request): Promise<Response> {
    return this.sqlReceipts.measure(() => this.handleFetch(request), request.headers.get(SQL_PROBE_HEADER));
  }

  private async handleFetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      await this.ready;
      if(url.pathname==='/_internal/picker/draw'&&request.method==='POST')return await this.committedPickerDraw(request);
      if(url.pathname.startsWith('/api/picker-alerts'))return await this.pickerAlertRequest(request,url);
      const workerResponse=await this.workers.fetch(request);if(workerResponse)return workerResponse;
      const authResponse=await this.auth.fetch(request);if(authResponse)return authResponse;
      const memoryResponse=await this.memory.fetch(request);if(memoryResponse)return memoryResponse;
      if(url.pathname==='/_internal/session-cap/check'&&request.method==='POST')return await this.checkSessionCap(request);
      if(url.pathname==='/_internal/session-cap/release'&&request.method==='POST')return await this.releaseSessionStart(request);
      if (url.pathname === '/_oncall/config' && request.method === 'POST') return await this.configureOncall(request);
      if (url.pathname === '/_oncall/ingest' && request.method === 'POST') return await this.ingestOncall(request);
      if (url.pathname === '/_oncall/offers' && request.method === 'GET') return await this.oncallOffers(request);
      const oncallDelivery = url.pathname.match(/^\/_oncall\/deliveries\/([0-9a-f-]{36})\/(ack|nack)$/i);
      if (oncallDelivery && request.method === 'POST')
        return await this.finishOncallDelivery(request, oncallDelivery[1], oncallDelivery[2] as 'ack' | 'nack');
      if (url.pathname === '/_actors/register' && request.method === 'POST') return await this.registerActor(request);
      if(url.pathname==='/api/model-policy'||url.pathname==='/_routing-policy')return await this.modelPolicy(request);
      if(url.pathname==='/api/engines'||url.pathname.startsWith('/api/engines/'))return await this.engines(request,url.pathname);
      if (url.pathname === '/_routes/register' && request.method === 'POST') return await this.registerRoute(request);
      if (url.pathname === '/_delegate' && request.method === 'POST') return await this.delegate(request);
      if (url.pathname === '/_mailboxes' && request.method === 'POST') return this.delegatedMailboxes(request);
      if (url.pathname === '/_recipient' && request.method === 'POST') return await this.recipient(request);
      if (url.pathname.startsWith('/api/routes/') && request.method === 'GET') return this.lookupRoute(request);
      if (url.pathname === '/api/directory' && request.method === 'GET') return this.directory(request);
      if (url.pathname === '/api/named-agents') return await this.namedAgents(request);
      const grantMatch=url.pathname.match(/^\/api\/named-agents\/([^/]+)\/grants$/);
      if(grantMatch) return await this.agentGrants(request,decodeURIComponent(grantMatch[1]));
      if (url.pathname === '/_verify' && request.method === 'POST') return await this.verify(request);
      if (url.pathname === '/_authorize' && request.method === 'POST') return await this.authorizeCurrent(request);
      if (url.pathname === '/api/enrollments/pair' && request.method === 'POST') return await this.createPairing(request);
      if (url.pathname === '/api/enrollments/complete' && request.method === 'POST') return await this.completePairing(request);
      if (url.pathname === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot() });
      if (url.pathname === '/api/repos' && request.method === 'GET') return this.listRepos(request);
      const repoMatch = url.pathname.match(/^\/api\/repos\/([^/]+)$/);
      if (repoMatch && request.method === 'GET') return this.showRepo(request, decodeURIComponent(repoMatch[1]));
      if (repoMatch && request.method === 'PATCH') return await this.editRepo(request, decodeURIComponent(repoMatch[1]));
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('internal_error', 500);
    }
  }

  private seedEngineRollout(): void {
    this.db.run(
      "INSERT OR IGNORE INTO hub2_engine_rollout(engine,desired_version,latest,state,updated_at) VALUES('claude',?,?, 'stable',?)",
      CLAUDE_BASELINE_VERSION, CLAUDE_LATEST_VERSION, Date.now(),
    );
  }

  private async committedPickerDraw(request:Request):Promise<Response> {
    const body=await bodyObject(request,32768);
    if(!boundedString(body.project_id,180)||!boundedString(body.operation_id,180)
      ||(body.attempt!==0&&body.attempt!==1)||!body.input||typeof body.input!=='object'||Array.isArray(body.input))
      return error('invalid_picker_draw',400);
    const previous=this.pickerQueue;
    let release!:()=>void;
    this.pickerQueue=new Promise<void>(resolve=>{release=resolve;});
    await previous;
    try {
      const digest=await sha256Hex(canonicalDraw({input:body.input,previous_picker:body.previous_picker??null}));
      let collect=this.db.schemaVersion>=6;
      if(collect){
        try{
          const replay=this.pickerAlerts.replayDraw(body.project_id as string,body.operation_id as string,body.attempt,digest);
          if(replay!==undefined)return json(replay);
        }catch(caught){
          if(caught instanceof HttpError&&caught.code==='picker_draw_conflict')throw caught;
          this.invalidatePickerCoverage();
          collect=false;
        }
      }
      const picked=await pickModelRouteDetailed(this.env,body.input as PickInput);
      const picker=buildStartPickerEvidence(picked.route,picked.diagnostics,body.attempt,body.previous_picker);
      const response={...picked,picker,coverage:{available:false} as Record<string,unknown>};
      if(collect){
        try{
          if(this.pickerObserverDirty)this.pickerAlerts.beginCoverage(PICKER_COVERAGE_EPOCH,Date.now(),true,this.pickerObserverEpoch);
          response.coverage=this.pickerAlerts.coverage();
          this.pickerAlerts.commitDraw({project_id:body.project_id,operation_id:body.operation_id,
            attempt:body.attempt,committed_at:Date.now(),picker},digest,response);
          this.pickerObserverDirty=false;
        }catch{
          this.invalidatePickerCoverage();
          response.coverage={available:false,reason:'picker_observer_unavailable'};
        }
      }else if(this.pickerObserverDirty){
        response.coverage={available:false,reason:'picker_observer_unavailable'};
      }
      return json(response);
    }catch(caught){
      if(caught instanceof HttpError)return error(caught.code,caught.status,caught.detail);
      throw caught;
    }finally{release();}
  }

  private invalidatePickerCoverage():void{
    if(!this.pickerObserverDirty){
      this.pickerObserverEpoch=crypto.randomUUID();
      console.warn('[hub2] PICKER_OBSERVER_UNAVAILABLE');
    }
    this.pickerObserverDirty=true;
  }

  private async accountRequest(path: string, method: string, actor: Actor, body?: unknown): Promise<Response> {
    const url = 'https://account.hub2.internal' + path;
    const headers = new Headers({ 'x-hub2-actor': JSON.stringify(actor), 'x-hub2-account-id': CLAUDE_ACCOUNT_ID });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    return this.env.ACCOUNTS.get(this.env.ACCOUNTS.idFromName(CLAUDE_ACCOUNT_ID)).fetch(new Request(url, {
      method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }));
  }

  private async engineCredentialKey(): Promise<CryptoKey> {
    try { return await credentialKey(this.env.HUB2_CREDENTIAL_KEY || ''); }
    catch { throw new HttpError('credential_key_unavailable', 503); }
  }

  private async sealEngineLogin(id: string, value: unknown): Promise<string> {
    const sealed = await sealCredential(await this.engineCredentialKey(),
      new TextEncoder().encode(JSON.stringify(value)), new TextEncoder().encode('hub2-engine-login:' + id));
    return JSON.stringify({ iv: Array.from(sealed.iv), data: Array.from(sealed.data) });
  }

  private async openEngineLogin(id: string, value: string): Promise<any> {
    try {
      const sealed = JSON.parse(value);
      const plain = await openCredential(await this.engineCredentialKey(), new Uint8Array(sealed.iv),
        new Uint8Array(sealed.data), new TextEncoder().encode('hub2-engine-login:' + id));
      return JSON.parse(new TextDecoder().decode(plain));
    } catch (caught) {
      if (caught instanceof HttpError) throw caught;
      throw new HttpError('credential_decryption_failed', 503);
    }
  }

  private async engineRevision(version: string): Promise<string> {
    const digest = await sha256Hex(CLAUDE_ENGINE_LABEL + ':' + version);
    return `${digest.slice(0, 8)}-${digest.slice(8, 12)}-${digest.slice(12, 16)}-${digest.slice(16, 20)}-${digest.slice(20, 32)}`;
  }

  private engineRollout(): any {
    return this.db.one<any>("SELECT * FROM hub2_engine_rollout WHERE engine='claude'") || {
      engine: 'claude', desired_version: CLAUDE_BASELINE_VERSION, latest: CLAUDE_LATEST_VERSION,
      rollout_id: null, target_version: null, previous_version: null, canary_machine: null,
      state: 'stable', updated_at: Date.now(),
    };
  }

  private engineVersionFor(machineId?: string): string {
    const rollout = this.engineRollout();
    if (rollout.state === 'canary_pending' && machineId === rollout.canary_machine && rollout.target_version)
      return rollout.target_version;
    return rollout.desired_version;
  }

  private async engines(request: Request, path: string): Promise<Response> {
    const actor = actorFrom(request);
    if (path === '/api/engines' && request.method === 'GET') return await this.listManagedEngines(actor);
    if (path === '/api/engines/sync' && request.method === 'GET') return await this.syncManagedEngines(actor);
    if (path === '/api/engines/admission' && request.method === 'POST') return await this.engineAdmission(request, actor);
    if (path === '/api/engines/credential-logins' || /^\/api\/engines\/credential-logins\/[0-9a-f-]{36}$/i.test(path))
      return await this.engineCredentialLogin(request, path, actor);
    if (path === '/api/engines/update' && request.method === 'POST') return await this.startClaudeRollout(request, actor);
    if (path === '/api/engines/versions' && request.method === 'GET') return await this.engineVersions(request, actor);
    if (path === '/api/engines/version-report' && request.method === 'POST') return await this.reportEngineVersions(request, actor);
    if (path === '/api/engines/sync-report' && request.method === 'POST') return await this.reportEngineSync(request, actor);
    if (path === '/api/engines/' + CLAUDE_ENGINE_LABEL && request.method === 'POST') return await this.acknowledgeEngine(request, actor);
    const credentialRoute = path.match(/^\/api\/engines\/hub-claude\/(credential|rejection)$/);
    if (credentialRoute) return await this.engineCredential(request, credentialRoute[1] as 'credential' | 'rejection', actor);
    return error('not_found', 404);
  }

  private async listManagedEngines(actor?: Actor): Promise<Response> {
    if (!hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const account = await this.accountRequest('/_internal/routing', 'GET', actor!);
    if (!account.ok) return account;
    const routing = await account.json() as any;
    const hostsResponse = await this.accountRequest('/api/account/hosts', 'GET', actor!);
    if (!hostsResponse.ok) return hostsResponse;
    const hostsBody = await hostsResponse.json() as any;
    const hosts = Array.isArray(hostsBody.items) ? hostsBody.items : [];
    const rollout = this.engineRollout();
    const reports = this.db.all<any>('SELECT * FROM hub2_engine_sync_reports ORDER BY machine_id,label');
    const byHost = new Map(reports.map(row => [row.machine_id, row]));
    const version = rollout.desired_version;
    const revision = await this.engineRevision(version);
    return json({
      logins: this.db.all<any>('SELECT id,label,provider,expires_at FROM hub2_engine_credential_logins WHERE expires_at>? ORDER BY expires_at', Date.now()),
      engines: [{ label: CLAUDE_ENGINE_LABEL, provider: 'claude', version, revision, status: routing.credential_status || 'unknown', supports_goal: true }],
      installations: hosts.map((host: any) => ({ machine_id: host.id, label: CLAUDE_ENGINE_LABEL,
        revision: this.engineVersionFor(host.id) === version ? revision : null })),
      sync_status: hosts.map((host: any) => {
        const report = byHost.get(host.id);
        return { machine_id: host.id, machine: host.id, online: null, host_state: report?.state || 'unknown',
          last_sync_at: report?.updated_at ? new Date(report.updated_at).toISOString() : null,
          accounts: [{ label: CLAUDE_ENGINE_LABEL, state: report?.state || 'unknown',
            ...(report?.error ? { error: report.error } : {}), credential_generation: routing.credential_generation,
            acked_generation: host.ack_generation ?? null }] };
      }),
      credentials: [{ label: CLAUDE_ENGINE_LABEL, provider: 'claude', generation: routing.credential_generation ?? null,
        status: routing.credential_status || 'unknown', hosts: hosts.map((host: any) => host.id) }],
      credential_rejections: [], account_migrations: [],
    });
  }

  private async syncManagedEngines(actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const credentialResponse = await this.accountRequest('/api/account/credentials?min_validity_ms=10800000', 'GET', actor);
    if (!credentialResponse.ok) return credentialResponse;
    const credential = await credentialResponse.json() as any;
    if (credential.provider !== 'claude' || !Number.isSafeInteger(credential.generation)
      || !credential.files || typeof credential.files !== 'object' || Array.isArray(credential.files))
      return error('managed_credential_unavailable', 503);
    const version = this.engineVersionFor(actor.resource_id);
    const revision = await this.engineRevision(version);
    return json({ engines: [{ label: CLAUDE_ENGINE_LABEL, provider: 'claude', version, revision,
      files: credential.files, env: {}, args: [], credential_generation: credential.generation,
      credential_paths: ['.claude/.credentials.json'] }], logins: [], latest_version: this.engineRollout().latest });
  }

  private async engineAdmission(request: Request, actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (body.label !== CLAUDE_ENGINE_LABEL || body.native !== true) return error('invalid_label', 400);
    const response = await this.accountRequest('/_internal/routing', 'GET', actor);
    if (!response.ok) return response;
    const routing = await response.json() as any;
    const account = routing.account;
    const eligible = Array.isArray(routing.hosts) && routing.hosts.includes(actor.resource_id);
    let reason: string | null = null;
    const accountMapped = account?.provider === 'claude' && (account.enabled === true || account.enabled === 1);
    if (!accountMapped) reason = 'account_unmapped';
    else if (!eligible) reason = 'host_not_approved';
    else if (routing.credential_usable !== true) reason = typeof routing.credential_status === 'string'
      ? 'credential_' + routing.credential_status : 'credential_unavailable';
    const quota = routing.quota;
    const remaining = quota && typeof quota.remaining === 'number' && Number.isFinite(quota.remaining)
      ? quota.remaining : null;
    const minimum = typeof account?.min_remaining === 'number' && Number.isFinite(account.min_remaining)
      ? account.min_remaining : 0;
    if (!reason && routing.fresh === true && remaining !== null && remaining <= minimum) reason = 'quota_exhausted';
    return json({ launch_allowed: reason === null, profile_supported: accountMapped,
      ...(reason ? { reason } : {}), ...(routing.fresh === true ? {} : { quota_warning: 'quota_probe_unavailable' }),
      ...(Number.isSafeInteger(routing.credential_generation) ? { credential_generation: routing.credential_generation } : {}),
      account_label: CLAUDE_ENGINE_LABEL, observed_at: new Date(Date.now() - 5000).toISOString(),
      server_observed_at: new Date().toISOString() });
  }

  private async engineCredential(request: Request, route: 'credential' | 'rejection', actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    if (route === 'credential' && request.method === 'GET') {
      const current = await this.accountRequest('/api/account/credentials?min_validity_ms=0', 'GET', actor);
      if (!current.ok) return current;
      const credential = await current.json() as any;
      return json({ label: CLAUDE_ENGINE_LABEL, generation: credential.generation, status: 'ready', files: credential.files });
    }
    if (request.method !== 'POST') return error('method_not_allowed', 405);
    const body = await bodyObject(request, 98_304);
    if (route === 'rejection') {
      if (!Number.isSafeInteger(body.generation) || !['provider_unauthorized', 'credential_rejected'].includes(String(body.reason)))
        return error('invalid_rejection', 400);
      const routingResponse = await this.accountRequest('/_internal/routing', 'GET', actor);
      if (!routingResponse.ok) return routingResponse;
      const routing = await routingResponse.json() as any;
      if (body.generation !== routing.credential_generation)
        return json({ status: 'superseded', generation: routing.credential_generation });
      return await this.accountRequest('/api/account/credential-rejection', 'POST', actor, body);
    }
    const baseGeneration = Number.isSafeInteger(body.base_generation) ? Number(body.base_generation) : null;
    if (baseGeneration === null || !body.files || typeof body.files !== 'object'
      || Array.isArray(body.files) || Object.keys(body.files).length > 8
      || !Object.entries(body.files).every(([path, value]) => path === '.claude/.credentials.json'
        && typeof value === 'string' && value.length > 0 && value.length <= 16_000)) return error('invalid_credential', 400);
    const currentResponse = await this.accountRequest('/api/account/credentials?min_validity_ms=0', 'GET', actor);
    if (!currentResponse.ok) return currentResponse;
    const current = await currentResponse.json() as any;
    if (baseGeneration !== current.generation || baseGeneration < 1)
      return json({ label: CLAUDE_ENGINE_LABEL, generation: current.generation, status: 'ready', files: current.files, accepted: false });
    const published = await this.accountRequest('/api/account/rotation', 'POST', actor, {
      expected_generation: baseGeneration, files: body.files,
    });
    if (!published.ok) {
      if (published.status !== 409) return published;
      const latestResponse = await this.accountRequest('/api/account/credentials?min_validity_ms=0', 'GET', actor);
      if (!latestResponse.ok) return latestResponse;
      const latest = await latestResponse.json() as any;
      return json({ label: CLAUDE_ENGINE_LABEL, generation: latest.generation, status: 'ready', files: latest.files, accepted: false });
    }
    const result = await published.json() as any;
    return json({ label: CLAUDE_ENGINE_LABEL, generation: result.generation, status: 'ready',
      files: body.files, accepted: true });
  }

  private async engineCredentialLogin(request: Request, path: string, actor?: Actor): Promise<Response> {
    if (!hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    if (request.method !== 'POST') return error('method_not_allowed', 405);
    const idMatch = path.match(/^\/api\/engines\/credential-logins\/([0-9a-f-]{36})$/i);
    const body = await bodyObject(request, 98_304);
    if (!idMatch) {
      if (body.provider !== 'claude' || body.label !== CLAUDE_ENGINE_LABEL
        || (body.version !== undefined && (typeof body.version !== 'string' || !/^\d+\.\d+\.\d+$/.test(body.version))))
        return error('invalid_login', 400);
      const accountResponse = await this.accountRequest('/_internal/routing', 'GET', actor!);
      if (!accountResponse.ok) return accountResponse;
      const routing = await accountResponse.json() as any;
      if (routing.account?.provider !== 'claude') return error('account_provider_mismatch', 409);
      this.db.run('DELETE FROM hub2_engine_credential_logins WHERE expires_at<=?', Date.now());
      if (Number(this.db.one<{ total: number }>('SELECT COUNT(*) AS total FROM hub2_engine_credential_logins')?.total || 0) >= 32)
        return error('login_capacity', 409);
      const id = crypto.randomUUID();
      const auth = await claudeAuthorization();
      const sealed = await this.sealEngineLogin(id, { verifier: auth.verifier, state: auth.state });
      const expiresAt = Date.now() + ENGINE_LOGIN_TTL_MS;
      this.db.run('INSERT INTO hub2_engine_credential_logins(id,label,provider,sealed,expires_at) VALUES(?,?,?,?,?)',
        id, CLAUDE_ENGINE_LABEL, 'claude', sealed, expiresAt);
      return json({ id, url: auth.url, expires_in: ENGINE_LOGIN_TTL_MS / 1000,
        expires_at: new Date(expiresAt).toISOString() }, 201);
    }

    const row = this.db.one<{ id: string; label: string; provider: string; sealed: string; expires_at: number }>(
      'SELECT * FROM hub2_engine_credential_logins WHERE id=?', idMatch[1]);
    if (!row || row.expires_at <= Date.now()) {
      if (row) this.db.run('DELETE FROM hub2_engine_credential_logins WHERE id=?', row.id);
      return error('login_expired', 410);
    }
    const login = await this.openEngineLogin(row.id, row.sealed);
    const publish = async (files: unknown): Promise<Response> => {
      const currentResponse = await this.accountRequest('/_internal/routing', 'GET', actor!);
      if (!currentResponse.ok) return currentResponse;
      const current = await currentResponse.json() as any;
      const expected = Number.isSafeInteger(current.credential_generation) ? current.credential_generation : 0;
      const saved = await this.accountRequest('/api/account/credentials', 'PUT', actor!, {
        provider: 'claude', expected_generation: expected, files,
      });
      if (!saved.ok) return saved;
      const result = await saved.json() as any;
      this.db.run('DELETE FROM hub2_engine_credential_logins WHERE id=?', row.id);
      return json({ status: 'configured', label: CLAUDE_ENGINE_LABEL, credential_generation: result.generation });
    };
    if (body.files !== undefined) return await publish(body.files);
    if (typeof body.code !== 'string' || body.code.length > 4096 || !/^[A-Za-z0-9_\-#.~]+$/.test(body.code.trim()))
      return error('invalid_code', 400);
    const result = await claudeExchange(body.code, login.verifier, login.state, (input, init) => fetch(input, init), Date.now());
    if (result.ok) return await publish(result.files);
    if (result.blocked && body.host_fallback === true) {
      return json({ status: 'host_exchange_required', provider_status: result.providerStatus ?? null,
        reason: result.detail ?? 'egress_blocked', expires_at: new Date(row.expires_at).toISOString(),
        exchange: { token_url: CLAUDE_OAUTH.token, profile_url: CLAUDE_OAUTH.profile,
          client_id: CLAUDE_OAUTH.client, redirect_uri: CLAUDE_OAUTH.redirect,
          scopes: CLAUDE_OAUTH.scopes, state: login.state, code_verifier: login.verifier } }, 202);
    }
    if (!result.blocked) this.db.run('DELETE FROM hub2_engine_credential_logins WHERE id=?', row.id);
    return json({ error: result.blocked ? 'login_exchange_blocked' : result.reason,
      provider_status: result.providerStatus ?? null, reason: result.detail ?? null }, result.blocked ? 502 : 400);
  }

  private async startClaudeRollout(request: Request, actor?: Actor): Promise<Response> {
    const body = await bodyObject(request, 16_384);
    if (body.engine !== 'claude') return error('invalid_engine', 400);
    const canary = typeof body.canary === 'string' && /^[A-Za-z0-9_.-]{1,120}$/.test(body.canary)
      ? body.canary : typeof body.machine === 'string' && /^[A-Za-z0-9_.-]{1,120}$/.test(body.machine) ? body.machine : 'RMTHZ';
    const admin = hasScope(actor, 'operator:admin');
    const canaryHost = actor?.kind === 'machine' && actor.resource_id === canary;
    if (!admin && !canaryHost) return error('forbidden', 403);
    if (admin) {
      const hostsResponse = await this.accountRequest('/api/account/hosts', 'GET', actor!);
      if (!hostsResponse.ok) return hostsResponse;
      const hostsBody = await hostsResponse.json() as any;
      const enrolled = Array.isArray(hostsBody.items) && hostsBody.items.some((host: any) => host?.id === canary);
      if (!enrolled) return error('unknown_canary', 400);
    }
    const current = this.engineRollout();
    if (current.state === 'canary_pending') {
      if (current.target_version === CLAUDE_LATEST_VERSION && current.canary_machine === canary)
        return json({ status: current.state, engine: 'claude', target_version: current.target_version,
          canary: canary, rollout_id: current.rollout_id });
      return error('rollout_in_progress', 409);
    }
    if (current.state === 'rolling') return error('rollout_in_progress', 409);
    if (current.desired_version === CLAUDE_LATEST_VERSION && current.state === 'stable')
      return json({ status: 'already_current', engine: 'claude', desired_version: current.desired_version });
    const id = crypto.randomUUID();
    const now = Date.now();
    this.db.run("UPDATE hub2_engine_rollout SET rollout_id=?,target_version=?,previous_version=desired_version,canary_machine=?,state='canary_pending',latest=?,updated_at=? WHERE engine='claude'",
      id, CLAUDE_LATEST_VERSION, canary, CLAUDE_LATEST_VERSION, now);
    return json({ status: 'canary_pending', engine: 'claude', target_version: CLAUDE_LATEST_VERSION,
      canary, rollout_id: id }, 202);
  }

  private async engineVersions(request: Request, actor?: Actor): Promise<Response> {
    if (!actor || (!hasScope(actor, 'operator:admin') && actor.kind !== 'machine')) return error('forbidden', 403);
    const machine = hasScope(actor, 'operator:admin') ? new URL(request.url).searchParams.get('machine') : actor.resource_id;
    const rollout = this.engineRollout();
    const reports = machine
      ? this.db.all<any>('SELECT * FROM hub2_engine_version_reports WHERE machine_id=? ORDER BY engine', machine)
      : this.db.all<any>('SELECT * FROM hub2_engine_version_reports ORDER BY machine_id,engine');
    return json({ desired: { claude: { version: rollout.desired_version, latest: rollout.latest, updated_at: rollout.updated_at } },
      reports, rollouts: [{ id: rollout.rollout_id, engine: 'claude', target_version: rollout.target_version,
        canary_machine: rollout.canary_machine, state: rollout.state, updated_at: rollout.updated_at,
        detail: { previous_version: rollout.previous_version } }] });
  }

  private async reportEngineVersions(request: Request, actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const body = await bodyObject(request, 16_384);
    const engines = body.engines;
    if (!engines || typeof engines !== 'object' || Array.isArray(engines) || Object.keys(engines).length > 8)
      return error('invalid_report', 400);
    const row = (engines as Record<string, any>).claude;
    if (!row || typeof row !== 'object' || Array.isArray(row)) return json({ status: 'recorded', engines: [] });
    const semver = (value: unknown) => typeof value === 'string' && /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(value) ? value : null;
    const installed = semver(row.installed);
    const latest = semver(row.latest);
    const result = typeof row.result === 'string' && /^[A-Za-z0-9_]{1,32}$/.test(row.result) ? row.result : null;
    const now = Date.now();
    this.db.run('INSERT OR REPLACE INTO hub2_engine_version_reports(machine_id,engine,installed,latest,result,updated_at) VALUES(?,?,?,?,?,?)',
      actor.resource_id, 'claude', installed, latest, result, now);
    const rollout = this.engineRollout();
    if (rollout.state === 'canary_pending' && rollout.canary_machine === actor.resource_id) {
      if (result === 'green' && installed === rollout.target_version) {
        this.db.run("UPDATE hub2_engine_rollout SET desired_version=target_version,state='rolling',updated_at=? WHERE engine='claude'", now);
      } else if (result !== null) {
        this.db.run("UPDATE hub2_engine_rollout SET state='rolled_back',target_version=NULL,updated_at=? WHERE engine='claude'", now);
      }
    } else if (rollout.state === 'rolling' && latest === rollout.desired_version && result === 'failed') {
      this.db.run("UPDATE hub2_engine_rollout SET desired_version=COALESCE(previous_version,desired_version),state='rolled_back',target_version=NULL,updated_at=? WHERE engine='claude'", now);
    } else if (rollout.state === 'rolling' && installed === rollout.desired_version && result === 'green') {
      const hostsResponse = await this.accountRequest('/api/account/hosts', 'GET', { id: 'hub2-engine-rollout', kind: 'operator', scopes: ['operator:admin'] });
      if (hostsResponse.ok) {
        const hostsBody = await hostsResponse.json() as any;
        const hosts = Array.isArray(hostsBody.items) ? hostsBody.items.map((host: any) => host.id).filter((id: unknown) => typeof id === 'string') : [];
        const ready = hosts.length > 0 && hosts.every((host: string) => {
          const report = this.db.one<any>('SELECT installed,result FROM hub2_engine_version_reports WHERE machine_id=? AND engine=?', host, 'claude');
          return report?.installed === rollout.desired_version && report?.result === 'green';
        });
        if (ready) this.db.run("UPDATE hub2_engine_rollout SET state='stable',target_version=NULL,previous_version=NULL,canary_machine=NULL,updated_at=? WHERE engine='claude'", now);
      }
    }
    return json({ status: 'recorded', engines: ['claude'] });
  }

  private async reportEngineSync(request: Request, actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const body = await bodyObject(request, 16_384);
    if (!['synced', 'held'].includes(String(body.state)) || !Array.isArray(body.items) || body.items.length > 32)
      return error('invalid_report', 400);
    const item = body.items.find((value: any) => value?.label === CLAUDE_ENGINE_LABEL);
    if (!item || typeof item.revision !== 'string' || !/^[a-f0-9-]{36}$/.test(item.revision)
      || !['installed', 'failed'].includes(item.state)) return json({ status: 'recorded' });
    const currentRevision = await this.engineRevision(this.engineVersionFor(actor.resource_id));
    if (item.revision !== currentRevision) return error('revision_changed', 409);
    const errorCode = item.state === 'failed' && typeof item.error === 'string' && /^[A-Za-z0-9_]{1,64}$/.test(item.error) ? item.error : null;
    const generation = Number.isSafeInteger(item.credential_generation) ? Number(item.credential_generation) : null;
    if (item.state === 'installed' && generation !== null) {
      const ack = await this.accountRequest('/api/account/credentials', 'POST', actor, { generation });
      if (!ack.ok) return ack;
    }
    this.db.run('INSERT OR REPLACE INTO hub2_engine_sync_reports(machine_id,label,revision,state,error,credential_generation,updated_at) VALUES(?,?,?,?,?,?,?)',
      actor.resource_id, CLAUDE_ENGINE_LABEL, item.revision, item.state, errorCode, generation, Date.now());
    return json({ status: 'recorded' });
  }

  private async acknowledgeEngine(request: Request, actor?: Actor): Promise<Response> {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    const current = await this.engineRevision(this.engineVersionFor(actor.resource_id));
    if (body.revision !== current) return error('revision_changed', 409);
    return json({ status: 'installed', label: CLAUDE_ENGINE_LABEL });
  }

  private bearer(request: Request): string | undefined {
    const match = (request.headers.get('Authorization') || '').match(/^Bearer ([A-Za-z0-9_-]{32,256})$/);
    return match?.[1];
  }

  private async configureOncall(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const bootstrapToken = this.bearer(request);
    const bootstrap = this.env.HUB2_RELEASE_MODE === 'bootstrap' && !!this.env.HUB2_BOOTSTRAP_TOKEN &&
      !!bootstrapToken && safeEqual(bootstrapToken, this.env.HUB2_BOOTSTRAP_TOKEN);
    if (!bootstrap && !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (body.rotate !== undefined && typeof body.rotate !== 'boolean') return error('invalid_request', 400);

    const ingestToken = randomToken(32);
    const pollToken = randomToken(32);
    const ingestHash = await sha256Hex(ingestToken);
    const pollHash = await sha256Hex(pollToken);
    const now = Date.now();
    const configured = this.db.transaction(this.ctx, () => {
      const prior = this.db.one<{ singleton: number }>('SELECT singleton FROM oncall_alert_config WHERE singleton=1');
      if (prior && body.rotate !== true) return false;
      this.db.run(
        'INSERT INTO oncall_alert_config(singleton,ingest_token_hash,poll_token_hash,created_at) VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET ingest_token_hash=excluded.ingest_token_hash,poll_token_hash=excluded.poll_token_hash,created_at=excluded.created_at',
        ingestHash, pollHash, now,
      );
      return true;
    });
    if (!configured) return error('already_configured', 409);
    return json({ configured: true, ingest_path: '/webhooks/oncall', offers_path: '/api/oncall-alerts/offers',
      ingest_token: ingestToken, poll_token: pollToken,
      secret_note: 'shown once; store in Dapier and the private host config' }, 201);
  }

  private async oncallAuthorized(request: Request, field: 'ingest_token_hash' | 'poll_token_hash'): Promise<boolean> {
    const token = this.bearer(request);
    if (!token) return false;
    const row = this.db.one<Record<string, unknown>>(`SELECT ${field} FROM oncall_alert_config WHERE singleton=1`);
    if (!row || typeof row[field] !== 'string') return false;
    return safeEqual(await sha256Hex(token), String(row[field]));
  }

  private async ingestOncall(request: Request): Promise<Response> {
    if (!(await this.oncallAuthorized(request, 'ingest_token_hash'))) return error('unauthorized', 401);
    const deliveryKey = request.headers.get('X-Delivery-Id') || '';
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(deliveryKey)) return error('invalid_delivery_id', 400);
    const raw = await boundedText(request, 65_536);
    let payload: unknown;
    try { payload = JSON.parse(raw); } catch { return error('invalid_json', 400); }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return error('invalid_json_object', 400);
    const canonicalPayload = JSON.stringify(payload);
    const digest = await sha256Hex(canonicalPayload);
    const id = crypto.randomUUID();
    const now = Date.now();
    const result = this.db.transaction(this.ctx, () => {
      const prior = this.db.one<{ id: string; status: string; payload_digest: string }>(
        'SELECT id,status,payload_digest FROM oncall_alert_deliveries WHERE delivery_key=?', deliveryKey);
      if (prior) return safeEqual(prior.payload_digest, digest)
        ? { delivery_id: prior.id, status: prior.status, replay: true }
        : { conflict: true };
      const pending = this.db.one<{ total: number }>("SELECT COUNT(*) AS total FROM oncall_alert_deliveries WHERE status<>'saved'");
      if (Number(pending?.total || 0) >= 5000) throw new HttpError('oncall_queue_full', 503);
      this.db.run('INSERT INTO oncall_alert_deliveries(id,delivery_key,payload_json,payload_digest,status,attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,\'pending\',0,?,?,?)',
        id, deliveryKey, canonicalPayload, digest, now, now, now);
      return { delivery_id: id, status: 'pending', replay: false };
    });
    if ('conflict' in result) return error('delivery_id_conflict', 409);
    return json(result, 202);
  }

  private async oncallOffers(request: Request): Promise<Response> {
    if (!(await this.oncallAuthorized(request, 'poll_token_hash'))) return error('unauthorized', 401);
    const now = Date.now();
    const rows = this.db.all<{ id: string; payload_json: string; attempts: number }>(
      "SELECT id,payload_json,attempts FROM oncall_alert_deliveries WHERE (status='pending' AND available_at<=?) OR (status='leased' AND lease_until<=?) ORDER BY created_at,id LIMIT 10",
      now, now);
    const staged = rows.map(row => ({ row, token: randomToken(32) }));
    const tokenHashes = await Promise.all(staged.map(item => sha256Hex(item.token)));
    const offers: Array<Record<string, unknown>> = [];
    this.db.transaction(this.ctx, () => {
      for (let index = 0; index < staged.length; index++) {
        const { row, token } = staged[index];
        const leaseHash = tokenHashes[index];
        const leaseUntil = now + 10 * 60_000;
        this.db.run(
          "UPDATE oncall_alert_deliveries SET status='leased',attempts=attempts+1,lease_token_hash=?,lease_until=?,updated_at=? WHERE id=? AND ((status='pending' AND available_at<=?) OR (status='leased' AND lease_until<=?))",
          leaseHash, leaseUntil, now, row.id, now, now,
        );
        const current = this.db.one<{ lease_token_hash: string | null }>(
          'SELECT lease_token_hash FROM oncall_alert_deliveries WHERE id=?', row.id);
        if (!current?.lease_token_hash || !safeEqual(current.lease_token_hash, leaseHash)) continue;
        let payload: unknown;
        try { payload = JSON.parse(row.payload_json); } catch { payload = {}; }
        offers.push({ delivery_id: row.id, payload, lease_token: token, lease_until: leaseUntil,
          attempt: Number(row.attempts) + 1 });
      }
    });
    return json({ offers });
  }

  private async finishOncallDelivery(request: Request, id: string, action: 'ack' | 'nack'): Promise<Response> {
    if (!(await this.oncallAuthorized(request, 'poll_token_hash'))) return error('unauthorized', 401);
    const body = await bodyObject(request, 4096);
    if (!boundedString(body.lease_token, 256)) return error('invalid_lease', 400);
    const leaseHash = await sha256Hex(body.lease_token);
    const row = this.db.one<{ status: string; lease_token_hash: string | null; attempts: number }>(
      'SELECT status,lease_token_hash,attempts FROM oncall_alert_deliveries WHERE id=?', id);
    if (!row) return error('delivery_not_found', 404);
    if (action === 'ack' && row.status === 'saved') return json({ saved: true, replay: true });
    if (row.status !== 'leased' || !row.lease_token_hash || !safeEqual(row.lease_token_hash, leaseHash))
      return error('lease_conflict', 409);
    const now = Date.now();
    if (action === 'ack') {
      this.db.transaction(this.ctx, () => this.db.run(
        "UPDATE oncall_alert_deliveries SET status='saved',payload_json='{}',lease_token_hash=NULL,lease_until=NULL,saved_at=?,updated_at=? WHERE id=? AND status='leased' AND lease_token_hash=?",
        now, now, id, leaseHash));
      return json({ saved: true, replay: false });
    }
    const delay = Math.min(5 * 60_000, 15_000 * (2 ** Math.min(Number(row.attempts || 1) - 1, 4)));
    this.db.transaction(this.ctx, () => this.db.run(
      "UPDATE oncall_alert_deliveries SET status='pending',lease_token_hash=NULL,lease_until=NULL,available_at=?,updated_at=? WHERE id=? AND status='leased' AND lease_token_hash=?",
      now + delay, now, id, leaseHash));
    return json({ retry: true, retry_after_ms: delay });
  }

  private async verify(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.token !== 'string' || body.token.length < 32 || body.token.length > 256) return error('invalid_credentials', 401);
    const tokenHash = await sha256Hex(body.token);
    const row = this.db.one<CredentialRow>(
      'SELECT actor_id,kind,resource_id,generation,scopes_json,family_id,client_id,expires_at,principal_email,revoked_at FROM credentials WHERE token_hash = ? AND revoked_at IS NULL', tokenHash,
    );
    if (!row){const actor=this.workers.verifyHash(tokenHash);return actor?json({actor}):error('invalid_credentials',401);}
    if(row.kind==='operator'){const actor=verifyKnownCredential(row,this.env);return actor?json({actor}):error('invalid_credentials',401);}
    if(row.kind!=='machine')return error('invalid_credentials',401);
    let scopes: unknown;
    try { scopes = JSON.parse(row.scopes_json); } catch { return error('credential_corrupt', 503); }
    if (!Array.isArray(scopes) || !scopes.every(value => typeof value === 'string')) return error('credential_corrupt', 503);
    return json({ actor: { id: row.actor_id, kind: row.kind, resource_id: row.resource_id || undefined, generation:row.generation,family_id:row.family_id,client_id:row.client_id,scopes } });
  }

  private async authorizeCurrent(request: Request): Promise<Response> {
    const body = await bodyObject(request.clone(), 4096);
    if(typeof body.actor_id==='string'&&body.actor_id.startsWith('session:'))return this.workers.authorize(body);
    if(typeof body.actor_id==='string'&&body.actor_id.startsWith('human:'))return (await this.auth.fetch(new Request('https://control.internal/_auth/authorize',{method:'POST',headers:request.headers,body:JSON.stringify(body)})))||error('forbidden',403);
    if (typeof body.actor_id !== 'string' || typeof body.generation !== 'string' || typeof body.scope !== 'string') return error('invalid_authorization_check', 400);
    const row = this.db.one<{ generation: string; scopes_json: string }>(
      'SELECT generation, scopes_json FROM credentials WHERE actor_id=? AND revoked_at IS NULL', body.actor_id,
    );
    if (!row || row.generation !== body.generation) return error('credential_revoked', 401);
    const scopes = this.parseArray(row.scopes_json);
    return scopes.includes('*') || scopes.includes(body.scope) ? json({ current: true }) : error('forbidden', 403);
  }

  private async createPairing(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    if (!hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const body = await bodyObject(request);
    const machineId = body.machine_id;
    const alias = body.alias;
    if (typeof machineId !== 'string' || !/^[A-Za-z0-9_.-]{1,80}$/.test(machineId)) return error('invalid_machine_id', 400);
    if (typeof alias !== 'string' || alias.trim().length < 1 || alias.length > 120) return error('invalid_alias', 400);
    const pairingCode = randomToken(16);
    const codeHash = await sha256Hex(pairingCode);
    const now = Date.now();
    const id = crypto.randomUUID();
    const scopes = ['machine:heartbeat', 'machine:offer', 'session:report', 'session:admit','message:delivery'];
    this.db.transaction(this.ctx, () => {
      this.db.run('INSERT INTO device_pairings(id, machine_id, alias, code_hash, scopes_json, created_at, expires_at, used_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
        id, machineId, alias.trim(), codeHash, JSON.stringify(scopes), now, now + 10 * 60_000);
    });
    return json({ pairing_id: id, machine_id: machineId, pairing_code: pairingCode, expires_at: new Date(now + 10 * 60_000).toISOString() }, 201);
  }

  private async pickerAlertRequest(request:Request,url:URL):Promise<Response>{
    const actor=actorFrom(request);
    if(!actor||!hasScope(actor,'operator:admin'))return error('forbidden',403);
    if(this.db.schemaVersion<6)return error('schema_migration_deferred',503);
    if(url.pathname==='/api/picker-alerts/counters'&&request.method==='GET'){
      try{
        return json({recipient:'dev@dtcdev.click',...this.pickerAlerts.counters(),coverage:this.pickerObserverDirty
          ?{available:false,reason:'picker_observer_unavailable'}:this.pickerAlerts.coverage()});
      }catch{
        this.invalidatePickerCoverage();
        return json({recipient:'dev@dtcdev.click',coverage:{available:false,reason:'picker_observer_unavailable'}});
      }
    }
    if(url.pathname==='/api/picker-alerts/offers'&&request.method==='GET')
      return json({offers:await this.pickerAlerts.offers(Date.now())});
    const delivery=url.pathname.match(/^\/api\/picker-alerts\/([1-9][0-9]*)\/(ack|nack)$/);
    if(delivery&&request.method==='POST'){
      const body=await bodyObject(request,2048);
      return json(await this.pickerAlerts.settle(Number(delivery[1]),body.lease_token as string,delivery[2] as 'ack'|'nack',Date.now()));
    }
    return error('not_found',404);
  }

  private async completePairing(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.code !== 'string' || body.code.length !== 32) return error('invalid_pairing_code', 400);
    const codeHash = await sha256Hex(body.code);
    const now = Date.now();
    const row = this.db.one<PairingRow>(
      'SELECT id, machine_id, alias, code_hash, scopes_json, expires_at, used_at FROM device_pairings WHERE code_hash = ?', codeHash,
    );
    if (!row || row.used_at !== null || row.expires_at <= now) return error('pairing_unavailable', 410);
    const token = randomToken(32);
    const actorId = `machine:${row.machine_id}`;
    const family=crypto.randomUUID();
    const tokenHash = await sha256Hex(token);
    const generation = randomToken(12);
    this.db.transaction(this.ctx, () => {
      const current = this.db.one<PairingRow>('SELECT id, machine_id, alias, code_hash, scopes_json, expires_at, used_at FROM device_pairings WHERE id = ?', row.id);
      if (!current || current.used_at !== null || current.expires_at <= Date.now()) throw new HttpError('pairing_unavailable', 410);
      this.workers.revokeMachine(row.machine_id);
      this.db.run('UPDATE credentials SET revoked_at = ? WHERE kind = ? AND resource_id = ? AND revoked_at IS NULL', now, 'machine', row.machine_id);
      this.db.run('UPDATE device_pairings SET used_at = ? WHERE id = ? AND used_at IS NULL', now, row.id);
      this.db.run(`INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,created_at,revoked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(actor_id) DO UPDATE SET kind=excluded.kind, resource_id=excluded.resource_id,
          generation=excluded.generation, token_hash=excluded.token_hash, scopes_json=excluded.scopes_json,family_id=excluded.family_id,created_at=excluded.created_at,revoked_at=NULL`,
        actorId,'machine',row.machine_id,generation,tokenHash,current.scopes_json,family,now);
    });
    return json({ machine_id: row.machine_id, alias: row.alias, token, scopes: JSON.parse(row.scopes_json) }, 201);
  }

  private listRepos(request: Request): Response {
    const actor = actorFrom(request);
    if (!hasScope(actor, 'repos:read') && !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const status = new URL(request.url).searchParams.get('status') || 'active';
    if (!['active', 'archived', 'duplicate'].includes(status)) return error('invalid_status', 400);
    const after = new URL(request.url).searchParams.get('after') || '';
    if (after.length > 200) return error('invalid_cursor', 400);
    const limit = Number(new URL(request.url).searchParams.get('limit') || 50);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) return error('invalid_limit', 400);
    const rows = this.db.all<Record<string, unknown>>(
      'SELECT canonical_name, canonical_url, purpose, status, duplicate_of, domains_json, metadata_json, updated_at FROM repos WHERE status = ? AND canonical_name > ? ORDER BY canonical_name LIMIT ?',
      status, after, limit + 1,
    );
    const hasMore = rows.length > limit;
    const page = rows.slice(0, limit);
    const next = hasMore ? String(page[page.length - 1]?.canonical_name || '') : null;
    return json({ items: page.map(row => this.repoView(row)), next });
  }

  private showRepo(request: Request, name: string): Response {
    const actor = actorFrom(request);
    if (!hasScope(actor, 'repos:read') && !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const canonical = this.resolveRepoName(name);
    if (!canonical) return error('repo_not_found', 404);
    const row = this.db.one<Record<string, unknown>>(
      'SELECT canonical_name, canonical_url, purpose, status, duplicate_of, domains_json, metadata_json, updated_at FROM repos WHERE canonical_name = ?', canonical,
    );
    return row ? json({ repo: this.repoView(row) }) : error('repo_not_found', 404);
  }

  private async editRepo(request: Request, name: string): Promise<Response> {
    const actor = actorFrom(request);
    if (!hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    if (!/^[A-Za-z0-9_.-]{1,80}\/[A-Za-z0-9_.-]{1,100}$/.test(name)) return error('invalid_repo_name', 400);
    const body = await bodyObject(request, 8192);
    const existing = this.db.one<Record<string, unknown>>(
      'SELECT canonical_name, canonical_url, purpose, status, duplicate_of, domains_json, metadata_json FROM repos WHERE canonical_name = ?', name,
    );
    const canonicalUrl = typeof body.canonical_url === 'string' ? body.canonical_url : existing?.canonical_url;
    const purpose = typeof body.purpose === 'string' ? body.purpose : existing?.purpose;
    const status = typeof body.status === 'string' ? body.status : existing?.status || 'active';
    const duplicateOf = status==='duplicate'?(typeof body.duplicate_of==='string'?body.duplicate_of:existing?.duplicate_of||null):null;
    const domains = Array.isArray(body.domains) ? body.domains : this.parseArray(existing?.domains_json);
    const aliases = Array.isArray(body.aliases) ? body.aliases : this.db.all<{ alias: string }>('SELECT alias FROM repo_aliases WHERE canonical_name = ?', name).map(row => row.alias);
    const metadata = body.metadata && typeof body.metadata === 'object' && !Array.isArray(body.metadata)
      ? body.metadata : this.parseObject(existing?.metadata_json);
    if (typeof canonicalUrl !== 'string' || canonicalUrl.length > 2048 || !/^https:\/\//.test(canonicalUrl)
      || typeof purpose !== 'string' || purpose.length > 2000) return error('invalid_repo', 400);
    if (!['active', 'archived', 'duplicate'].includes(String(status))) return error('invalid_status', 400);
    if(status!=='active'&&this.db.one('SELECT canonical_name FROM repos WHERE duplicate_of=? ORDER BY canonical_name LIMIT 1',name))return error('canonical_repo_in_use',409);
    if (status === 'duplicate' && (typeof duplicateOf !== 'string' || duplicateOf === name || !this.db.one('SELECT canonical_name FROM repos WHERE canonical_name=? AND status=? AND duplicate_of IS NULL',duplicateOf,'active'))) return error('invalid_duplicate_target', 400);
    if (domains.length > 32 || aliases.length > 64
      || !domains.every(value => typeof value === 'string' && value.length <= 253 && /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/.test(value))
      || !aliases.every(value => typeof value === 'string' && value.length <= 180 && /^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)?$/.test(value))) return error('invalid_repo_metadata', 400);
    if (new TextEncoder().encode(JSON.stringify(metadata)).byteLength > 4096) return error('repo_metadata_too_large', 400);
    if (this.db.one('SELECT canonical_name FROM repo_aliases WHERE alias = ? AND canonical_name != ?', name, name)) return error('repo_alias_conflict', 409);
    if (aliases.some(alias => alias !== name && !!this.db.one('SELECT canonical_name FROM repos WHERE canonical_name = ?', alias))) return error('repo_alias_conflict', 409);
    const now = Date.now();
    try {
      this.db.transaction(this.ctx, () => {
        this.db.run(`INSERT INTO repos(canonical_name, canonical_url, purpose, status, duplicate_of, domains_json, metadata_json, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(canonical_name) DO UPDATE SET canonical_url=excluded.canonical_url, purpose=excluded.purpose,
            status=excluded.status, duplicate_of=excluded.duplicate_of, domains_json=excluded.domains_json,
            metadata_json=excluded.metadata_json, updated_at=excluded.updated_at`,
          name, canonicalUrl, purpose, status, duplicateOf, JSON.stringify(domains), JSON.stringify(metadata), now);
        this.db.run('DELETE FROM repo_aliases WHERE canonical_name = ?', name);
        for (const alias of new Set(aliases)) this.db.run('INSERT INTO repo_aliases(alias, canonical_name) VALUES (?, ?)', alias, name);
      });
    } catch { return error('repo_conflict', 409); }
    return this.showRepo(new Request(request.url, { headers: request.headers }), name);
  }

  private resolveRepoName(value: string): string | undefined {
    return this.db.one<{ canonical_name: string }>('SELECT canonical_name FROM repos WHERE canonical_name = ?', value)?.canonical_name
      || this.db.one<{ canonical_name: string }>('SELECT canonical_name FROM repo_aliases WHERE alias = ?', value)?.canonical_name;
  }

  private repoView(row: Record<string, unknown>): JsonRecord {
    const name = String(row.canonical_name);
    return {
      canonical_name: name,
      canonical_url: row.canonical_url,
      purpose: row.purpose,
      status: row.status,
      duplicate_of: row.duplicate_of,
      domains: this.parseArray(row.domains_json),
      aliases: this.db.all<{ alias: string }>('SELECT alias FROM repo_aliases WHERE canonical_name = ? ORDER BY alias LIMIT 65', name).map(item => item.alias),
      metadata: this.parseObject(row.metadata_json),
      updated_at: row.updated_at,
    };
  }

  private parseArray(value: unknown): unknown[] {
    try { const parsed = JSON.parse(String(value || '[]')); return Array.isArray(parsed) ? parsed : []; } catch { return []; }
  }

  private parseObject(value: unknown): JsonRecord {
    try { const parsed = JSON.parse(String(value || '{}')); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}; } catch { return {}; }
  }
  private async namedAgents(request:Request):Promise<Response>{
    const actor=actorFrom(request);if(!hasScope(actor,'operator:admin'))return error('forbidden',403);
    if(request.method==='GET'){
      const after=new URL(request.url).searchParams.get('after')||'';
      const rows=this.db.all<{id:string}>('SELECT id,display_name,forward_session,enabled FROM named_agents WHERE id>? ORDER BY id LIMIT 51',after);
      return json({items:rows.slice(0,50),next:rows.length>50?rows[49].id:null});
    }
    if(request.method!=='POST')return error('not_found',404);const b=await bodyObject(request,4096);
    const name=typeof b.name==='string'?b.name.replace(/^agent:/,''):'';
    if(!/^[a-z][a-z0-9_-]{0,63}$/.test(name)||!boundedString(b.display_name,120)||(b.enabled!==undefined&&typeof b.enabled!=='boolean'))return error('invalid_named_agent',400);
    const forward=b.forward_session??null;
    if(forward!==null&&(!boundedString(forward,120)||!this.db.one('SELECT id FROM object_routes WHERE kind=? AND id=?','session',forward)))return error('forward_session_not_found',400);
    const id='agent:'+name;
    this.db.transaction(this.ctx,()=>{
      const prior=this.db.one<{forward_session:string|null;enabled:number}>('SELECT forward_session,enabled FROM named_agents WHERE id=?',id);
      this.db.run('INSERT INTO named_agents(id,display_name,forward_session,enabled) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,forward_session=excluded.forward_session,enabled=excluded.enabled',id,b.display_name,forward,Number(b.enabled!==false));
      if(forward&&b.enabled!==false&&(!prior||prior.forward_session!==forward||!prior.enabled))this.db.run("INSERT INTO mailbox_wakes(owner,revision,after_id,next_attempt) VALUES(?,1,'',?) ON CONFLICT(owner) DO UPDATE SET revision=revision+1,after_id='',next_attempt=excluded.next_attempt,attempt=0",id,Date.now());
    });
    await this.scheduleWake();
    return json({agent:{id,display_name:b.display_name,forward_session:forward,enabled:b.enabled!==false}});
  }
  private async agentGrants(request:Request,name:string):Promise<Response>{
    const actor=actorFrom(request);if(!hasScope(actor,'operator:admin'))return error('forbidden',403);
    const id=name.startsWith('agent:')?name:'agent:'+name;
    if(!this.db.one('SELECT id FROM named_agents WHERE id=?',id))return error('named_agent_not_found',404);
    if(request.method==='GET')return json({items:this.db.all('SELECT family_id,client_id,can_send,can_read,enabled FROM agent_grants WHERE agent_id=? ORDER BY family_id,client_id LIMIT 101',id)});
    if(request.method!=='PUT')return error('not_found',404);const b=await bodyObject(request,4096),client=b.client_id??'*';
    if(!boundedString(b.family_id,180)||typeof client!=='string'||client.length>180||typeof b.can_send!=='boolean'||typeof b.can_read!=='boolean'||typeof b.enabled!=='boolean')return error('invalid_agent_grant',400);
    const prior=this.db.one('SELECT agent_id FROM agent_grants WHERE agent_id=? AND family_id=? AND client_id=?',id,b.family_id,client);
    if(!prior&&this.db.all('SELECT family_id FROM agent_grants WHERE agent_id=? LIMIT 101',id).length>=100)return error('grant_capacity',409);
    this.db.run('INSERT INTO agent_grants(agent_id,family_id,client_id,can_send,can_read,enabled) VALUES(?,?,?,?,?,?) ON CONFLICT(agent_id,family_id,client_id) DO UPDATE SET can_send=excluded.can_send,can_read=excluded.can_read,enabled=excluded.enabled',id,b.family_id,client,Number(b.can_send),Number(b.can_read),Number(b.enabled));return json({recorded:true});
  }
  private grant(actor:CredentialActor,id:string):{can_send:number;can_read:number}|undefined{
    if(!actor.family_id)return undefined;
    const rows=[...new Set([actor.client_id||'','*'])].map(client=>this.db.one<{can_send:number;can_read:number}>('SELECT can_send,can_read FROM agent_grants WHERE agent_id=? AND family_id=? AND client_id=? AND enabled=1',id,actor.family_id,client)).filter((v):v is {can_send:number;can_read:number}=>!!v);
    if(!rows.length)return undefined;return {can_send:Number(rows.some(v=>v.can_send)),can_read:Number(rows.some(v=>v.can_read))};
  }
  private async delegate(request:Request):Promise<Response>{
    const actor=actorFrom(request) as CredentialActor|undefined;if(!actor)return error('forbidden',403);
    const b=await bodyObject(request,4096),target=b.target??b.as;
    if(!boundedString(target,80)||!['send','read'].includes(String(b.action)))return error('invalid_delegation',400);
    if((b.actor_id!==undefined&&b.actor_id!==actor.id)||(b.family_id!==undefined&&b.family_id!==actor.family_id)||(b.client_id!==undefined&&b.client_id!==(actor.client_id||'')))return error('subject_mismatch',403);
    const id=target.startsWith('agent:')?target:'agent:'+target;
    const agent=this.db.one<{forward_session:string|null}>('SELECT forward_session FROM named_agents WHERE id=? AND enabled=1',id);
    if(!agent)return error('named_agent_not_found',404);
    const grant=this.grant(actor,id);if(!grant||(b.action==='send'?!grant.can_send:!grant.can_read))return error('delegation_not_granted',403);
    const scopes=[`mailbox:${id}`,b.action==='send'?'message:send':'message:read'];
    if(b.requested_scopes!==undefined&&(!Array.isArray(b.requested_scopes)||b.requested_scopes.length>8||!b.requested_scopes.every(v=>typeof v==='string'&&scopes.includes(v))))return error('delegation_scope_escalation',403);
    return json({authorized:true,target:id,action:b.action,scopes,subject:{actor_id:actor.id,family_id:actor.family_id,client_id:actor.client_id||''},forward_session:agent.forward_session});
  }
  private delegatedMailboxes(request:Request):Response{
    const actor=actorFrom(request) as CredentialActor|undefined;if(!actor)return error('forbidden',403);
    const owners=[actor.id];if(!actor.family_id)return json({owners});
    const rows=this.db.all<{agent_id:string}>('SELECT DISTINCT g.agent_id FROM agent_grants g JOIN named_agents n ON n.id=g.agent_id WHERE g.family_id=? AND g.client_id IN (?,?) AND g.enabled=1 AND g.can_read=1 AND n.enabled=1 ORDER BY g.agent_id LIMIT 101',actor.family_id,actor.client_id||'','*');
    if(rows.length>100)return error('delegation_observation_limit',503);return json({owners:[...owners,...rows.map(v=>v.agent_id)]});
  }
  private async recipient(request:Request):Promise<Response>{
    const b=await bodyObject(request,4096);if(!boundedString(b.recipient,180))return error('invalid_recipient',400);
    const row=this.db.one<{id:string;forward_session:string|null}>('SELECT id,forward_session FROM named_agents WHERE id=? AND enabled=1',b.recipient);
    if(row)return json({mailbox:row.id,forward_session:row.forward_session});
    if(b.recipient.startsWith('agent:'))return error('recipient_unknown',404);
    const known=this.db.one<{id:string}>('SELECT id FROM actor_directory WHERE id=? AND enabled=1',b.recipient);
    return known?json({mailbox:known.id,forward_session:null}):error('recipient_unknown',404);
  }
  private async registerRoute(request:Request):Promise<Response>{
    const b=await bodyObject(request,8192);
    if(!['intake','session','message','schedule'].includes(String(b.kind))||!boundedString(b.id,180)||!boundedString(b.owner,180)||!boundedString(b.resource_id,180)||!Array.isArray(b.participants)||b.participants.length>32||!b.participants.every(v=>boundedString(v,180)))return error('invalid_route',400);
    if(b.bound_intake_id!==undefined&&(b.kind!=='session'||!boundedString(b.bound_intake_id,180)))return error('invalid_route_intake',400);
    const epoch=Number(b.epoch??1);if(!Number.isSafeInteger(epoch)||epoch<1)return error('invalid_route_epoch',400);
    const participants=[...new Set(b.participants as string[])].sort();
    const digest=await sha256Hex(JSON.stringify([b.kind,b.id,b.owner,b.resource_id,b.bound_intake_id??null,participants]));
    this.db.transaction(this.ctx,()=>{
      const prior=this.db.one<{owner:string;resource_id:string;epoch:number;digest:string;participants_json:string;bound_intake_id:string|null}>('SELECT owner,resource_id,bound_intake_id,epoch,digest,participants_json FROM object_routes WHERE kind=? AND id=?',b.kind,b.id);
      if(prior&&(prior.owner!==b.owner||prior.resource_id!==b.resource_id||prior.bound_intake_id!==(b.bound_intake_id??null)))throw new HttpError('route_conflict',409);
      if(prior&&(epoch<prior.epoch||(epoch===prior.epoch&&digest!==prior.digest)))throw new HttpError('route_epoch_conflict',409);
      if(prior&&epoch===prior.epoch)return;
      let old:string[]=[];if(prior){try{old=JSON.parse(prior.participants_json);}catch{throw new HttpError('route_acl_corrupt',503);}if(!Array.isArray(old)||old.length>32||!old.every(v=>boundedString(v,180)))throw new HttpError('route_acl_corrupt',503);}
      this.db.run('INSERT INTO object_routes(kind,id,owner,resource_id,bound_intake_id,epoch,digest,participants_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET epoch=excluded.epoch,digest=excluded.digest,participants_json=excluded.participants_json',b.kind,b.id,b.owner,b.resource_id,b.bound_intake_id??null,epoch,digest,JSON.stringify(participants));
      for(const actor of old)this.db.run('DELETE FROM route_acl WHERE actor_id=? AND kind=? AND id=?',actor,b.kind,b.id);
      for(const actor of participants)this.db.run('INSERT INTO route_acl(actor_id,kind,id) VALUES(?,?,?)',actor,b.kind,b.id);
      if(b.kind==='session')this.db.run('INSERT OR IGNORE INTO actor_directory(id,kind,enabled) VALUES(?,?,1)','session:'+b.id,'session');
    });return json({registered:true});
  }
  private lookupRoute(request:Request):Response{
    const actor=actorFrom(request);if(!actor)return error('forbidden',403);
    const match=new URL(request.url).pathname.match(/^\/api\/routes\/(intake|session|message|schedule)\/([^/]+)$/);if(!match)return error('not_found',404);
    const kind=match[1],value=decodeURIComponent(match[2]);if(!boundedString(value,180))return error('invalid_route_id',400);
    type Route={id:string;owner:string;resource_id:string;bound_intake_id:string|null};
    const columns='id,owner,resource_id,bound_intake_id',admin=hasScope(actor,'operator:admin')||hasScope(actor,'routing:read');
    const bound=(r:Route)=>kind!=='message'&&hasScope(actor,'intake:read')&&((kind==='intake'&&actor.resource_id===r.id)||(kind==='session'&&!!r.bound_intake_id&&actor.resource_id===r.bound_intake_id)||hasScope(actor,'project:'+r.resource_id));
    const exact=this.db.one<Route>('SELECT '+columns+' FROM object_routes WHERE kind=? AND id=?',kind,value);
    if(exact){if(!admin&&!bound(exact)&&!this.db.one('SELECT id FROM route_acl WHERE actor_id=? AND kind=? AND id=?',actor.id,kind,exact.id))return error('route_not_found',404);return json({route:{kind,...exact}});}
    if(value.length<6)return error('prefix_too_short',400);
    const rows:Route[]=[],add=(items:Route[])=>{for(const item of items)if(!rows.some(v=>v.id===item.id))rows.push(item);};
    if(admin)add(this.db.all('SELECT '+columns+' FROM object_routes WHERE kind=? AND id>=? AND id<? ORDER BY id LIMIT 2',kind,value,value+'\uffff'));
    else{
      add(this.db.all('SELECT r.id,r.owner,r.resource_id,r.bound_intake_id FROM route_acl a JOIN object_routes r ON r.kind=a.kind AND r.id=a.id WHERE a.actor_id=? AND a.kind=? AND a.id>=? AND a.id<? ORDER BY a.id LIMIT 2',actor.id,kind,value,value+'\uffff'));
      if(kind!=='message'&&hasScope(actor,'intake:read')){
        if(kind==='intake'&&actor.resource_id?.startsWith(value)){const r=this.db.one<Route>('SELECT '+columns+' FROM object_routes WHERE kind=? AND id=?',kind,actor.resource_id);if(r)add([r]);}
        if(kind==='session'&&actor.resource_id)add(this.db.all('SELECT '+columns+' FROM object_routes WHERE bound_intake_id=? AND kind=? AND id>=? AND id<? ORDER BY id LIMIT 2',actor.resource_id,kind,value,value+'\uffff'));
        const projects=actor.scopes.filter(v=>v.startsWith('project:')).map(v=>v.slice(8));if(projects.length>32)return error('project_scope_observation_limit',503);
        for(const project of projects)add(this.db.all('SELECT '+columns+' FROM object_routes WHERE resource_id=? AND kind=? AND id>=? AND id<? ORDER BY id LIMIT 2',project,kind,value,value+'\uffff'));
      }
    }
    if(rows.length>1)return error('ambiguous_prefix',409);return rows.length?json({route:{kind,...rows[0]}}):error('route_not_found',404);
  }

  private directory(request:Request):Response{
    const actor=actorFrom(request);if(!hasScope(actor,'operator:admin')&&!hasScope(actor,'routing:read')&&!hasScope(actor,'directory:read'))return error('forbidden',403);
    const q=new URL(request.url).searchParams,kind=q.get('kind')||'agents',after=q.get('after')||'',limit=Number(q.get('limit')||25);
    if(!['agents','machines'].includes(kind)||!Number.isInteger(limit)||limit<1||limit>50||after.length>180)return error('invalid_directory_query',400);
    const rows=kind==='agents'?this.db.all<{id:string}>('SELECT id,display_name FROM named_agents WHERE enabled=1 AND id>? ORDER BY id LIMIT ?',after,limit+1)
      :this.db.all<{id:string}>("SELECT resource_id AS id FROM credentials WHERE kind='machine' AND revoked_at IS NULL AND resource_id>? ORDER BY resource_id LIMIT ?",after,limit+1).map(v=>({...v,credential_active:true,presence:'requires_machine_probe'}));
    return json({kind,items:rows.slice(0,limit),next:rows.length>limit?rows[limit-1].id:null});
  }

  private async checkSessionCap(request:Request):Promise<Response>{
    const actor=actorFrom(request);
    if(!hasScope(actor,'operator:admin')&&!hasScope(actor,'session:start'))return error('forbidden',403);
    const body=await bodyObject(request,4096);
    if(!boundedString(body.operation_id,180)||!boundedString(body.session_id,180))return error('invalid_session_start',400);
    const operationId=body.operation_id,sessionId=body.session_id;
    const machines:{id:string}[]=[];
    let after='';
    while(true){
      const page=this.db.all<{id:string}>("SELECT resource_id AS id FROM credentials WHERE kind='machine' AND revoked_at IS NULL AND resource_id>? ORDER BY resource_id LIMIT 51",after);
      machines.push(...page.slice(0,50));
      if(page.length<=50)break;
      after=page[49].id;
    }
    const snapshots=await Promise.all(machines.map(async row=>{
      try{
        const response=await this.env.MACHINES.get(this.env.MACHINES.idFromName(row.id)).fetch('https://machine.internal/_internal/live-sessions');
        if(!response.ok)return null;
        return await response.json() as {observed_at?:number;sessions?:Array<{session_id:string;native_id:string|null;phase:string;last_activity_at:number|null;observed_at:number}>};
      }catch{return null;}
    }));
    const now=Date.now(),live=[...new Map(snapshots.flatMap(snapshot=>snapshot?.sessions||[])
      .filter(session=>isLiveActiveSession(session,now)).map(session=>[session.session_id,session])).values()];
    const liveIds=new Set(live.map(session=>session.session_id));
    const reservation=this.db.transaction(this.ctx,()=>{
      this.db.run('DELETE FROM session_start_reservations WHERE expires_at<=?',now);
      const prior=this.db.one<{session_id:string}>('SELECT session_id FROM session_start_reservations WHERE operation_id=?',operationId);
      if(prior)return prior.session_id===sessionId
        ?{duplicate:true,starting_count:this.db.one<{n:number}>('SELECT COUNT(*) AS n FROM session_start_reservations')?.n||0}
        :{conflict:true};
      for(const row of this.db.all<{operation_id:string;session_id:string}>('SELECT operation_id,session_id FROM session_start_reservations'))
        if(liveIds.has(row.session_id))this.db.run('DELETE FROM session_start_reservations WHERE operation_id=?',row.operation_id);
      const starting=this.db.one<{n:number}>('SELECT COUNT(*) AS n FROM session_start_reservations')?.n||0;
      if(liveIds.has(sessionId))return {duplicate:true,starting_count:starting};
      if(live.length+starting>=MAX_ACTIVE_SESSIONS)return {full:true,starting_count:starting};
      this.db.run('INSERT INTO session_start_reservations(operation_id,session_id,expires_at) VALUES(?,?,?)',operationId,sessionId,now+120_000);
      return {starting_count:starting+1};
    });
    if('conflict'in reservation)return error('session_start_idempotency_conflict',409);
    if('full'in reservation){
      const detail=`Hub allows ${MAX_ACTIVE_SESSIONS} live active sessions; ${live.length} are active and ${reservation.starting_count} starts are in progress.`;
      console.error('[hub2] ACTIVE_SESSION_LIMIT',JSON.stringify({active_count:live.length,starting_count:reservation.starting_count,limit:MAX_ACTIVE_SESSIONS}));
      return json({error:'active_session_limit',detail,active_count:live.length,starting_count:reservation.starting_count,limit:MAX_ACTIVE_SESSIONS},429);
    }
    return json({accepted:true,duplicate:reservation.duplicate===true,active_count:live.length,starting_count:reservation.starting_count,limit:MAX_ACTIVE_SESSIONS});
  }

  private async releaseSessionStart(request:Request):Promise<Response>{
    const body=await bodyObject(request,4096);
    if(!boundedString(body.operation_id,180)||!boundedString(body.session_id,180))return error('invalid_session_start',400);
    this.db.run('DELETE FROM session_start_reservations WHERE operation_id=? AND session_id=?',body.operation_id,body.session_id);
    return json({released:true});
  }

  private async registerActor(request:Request):Promise<Response>{
    const b=await bodyObject(request,4096);
    if(!boundedString(b.id,180)||!['human','session'].includes(String(b.kind))||!b.id.startsWith(String(b.kind)+':')||typeof b.enabled!=='boolean')return error('invalid_actor_registration',400);
    if(b.kind==='session'&&!this.db.one('SELECT id FROM object_routes WHERE kind=? AND id=?','session',b.id.slice(8)))return error('session_not_found',404);
    const prior=this.db.one<{kind:string;enabled:number}>('SELECT kind,enabled FROM actor_directory WHERE id=?',b.id);
    if(prior&&prior.kind===b.kind&&prior.enabled===Number(b.enabled))return json({registered:true,duplicate:true});
    this.db.run('INSERT INTO actor_directory(id,kind,enabled) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled',b.id,b.kind,Number(b.enabled));return json({registered:true});
  }

  private normalizeModelPolicy(input:unknown,allowEmpty=false):JsonRecord[]{
    if(!Array.isArray(input)||input.length>32||!input.length&&!allowEmpty)throw new HttpError('invalid_model_policy',400);
    const ids=new Set<string>(),candidates:JsonRecord[]=[];
    for(const candidate of input){
      if(!candidate||typeof candidate!=='object'||Array.isArray(candidate))throw new HttpError('invalid_model_candidate',400);
      const c=candidate as JsonRecord;
      if(Object.keys(c).some(k=>!['id','engine','model','effort','account_id','weight','capabilities','enabled'].includes(k)))throw new HttpError('unknown_model_candidate_field',400);
      if(!['id','engine','model','account_id'].every(k=>boundedString(c[k],120)&&/^[A-Za-z0-9_.:/-]+$/.test(String(c[k])))||typeof c.weight!=='number'||!Number.isFinite(c.weight)||c.weight<=0||c.weight>1000000||typeof c.enabled!=='boolean'||!Array.isArray(c.capabilities)||c.capabilities.length>32||!c.capabilities.every(v=>boundedString(v,80))||(c.effort!==undefined&&!['low','medium','max'].includes(String(c.effort))))throw new HttpError('invalid_model_candidate',400);
      if(['shared-zai','zcodex'].includes(String(c.account_id))||['zcodex','goz','zai'].includes(String(c.engine))&&c.account_id!=='zai-central')throw new HttpError('zai_central_account_required',400);
      const policyViolation=modelPolicyCandidateViolation(c as unknown as ModelPolicyRoute);
      if(policyViolation)throw new HttpError(policyViolation,400);
      if(ids.has(String(c.id)))throw new HttpError('duplicate_model_candidate',400);ids.add(String(c.id));candidates.push({...c,capabilities:[...new Set(c.capabilities)].sort()});
    }
    return candidates;
  }

  private checkPolicyRecovery(prior:{version:number;candidates_json:string}|undefined,expected:unknown,desired:JsonRecord[]){
    const version=prior?.version??0;
    if(expected!==version)throw new HttpError('model_policy_version_conflict',409);
    let candidates:JsonRecord[];
    try{
      candidates=this.normalizeModelPolicy(prior?JSON.parse(prior.candidates_json):[],true);
    }catch{
      throw new HttpError('model_policy_prior_unrestorable',409);
    }
    // Ignore object key order and compare the same normalized capabilities
    // that PUT would restore. Candidate array order remains significant.
    const keys=['id','engine','model','effort','account_id','weight','capabilities','enabled'];
    const changed=JSON.stringify(candidates,keys)!==JSON.stringify(desired,keys);
    if(changed){
      if(version>Number.MAX_SAFE_INTEGER-2)throw new HttpError('model_policy_recovery_version_exhausted',409);
      const rollback={candidates,expected_version:version+1,...(!candidates.length?{restore_unconfigured:true}:{})};
      if(new TextEncoder().encode(JSON.stringify(rollback)).byteLength>HUB2_FORWARD_BODY_LIMIT)throw new HttpError('model_policy_rollback_body_too_large',409);
    }
    return {version,candidates,changed};
  }

  private async modelPolicy(request:Request):Promise<Response>{
    const path=new URL(request.url).pathname,actor=actorFrom(request);
    if(path!=='/_routing-policy'&&!hasScope(actor,'operator:admin')&&!hasScope(actor,'model-policy:read')&&!hasScope(actor,'routing:read'))return error('forbidden',403);
    const readPolicy=()=>this.db.one<{version:number;candidates_json:string;digest:string}>('SELECT version,candidates_json,digest FROM model_policy WHERE id=1');
    if(request.method==='GET'){
      const current=readPolicy(),candidates=current?JSON.parse(current.candidates_json):[];
      return candidates.length?json({version:current!.version,candidates}):json({error:'model_policy_unconfigured',version:current?.version??0},404);
    }
    if(path==='/_routing-policy'||!['PUT','POST'].includes(request.method)||!hasScope(actor,'operator:admin'))return error('forbidden',403);
    const b=await bodyObject(request,128000);
    const recovery=request.method==='POST';
    const protocol='model-policy-recovery-v1';
    if(recovery&&(b.protocol!==protocol||(b.action!=='validate_recovery'&&b.action!=='apply_recoverable')||Object.keys(b).some(key=>!['protocol','action','expected_version','candidates'].includes(key))))return error('invalid_model_policy_recovery',400);
    const restore=b.restore_unconfigured===true;
    if(b.restore_unconfigured!==undefined&&(!restore||recovery))return error('invalid_model_policy_restore',400);
    if((recovery||b.expected_version!==undefined)&&(!Number.isSafeInteger(b.expected_version)||Number(b.expected_version)<0))return error('invalid_model_policy_version',400);
    if(restore&&(!Number.isSafeInteger(b.expected_version)||!Array.isArray(b.candidates)||b.candidates.length!==0))return error('invalid_model_policy_restore',400);
    const candidates=this.normalizeModelPolicy(b.candidates,restore);
    if(recovery&&b.action==='validate_recovery'){
      return json({protocol,action:b.action,...this.checkPolicyRecovery(readPolicy(),b.expected_version,candidates)});
    }
    const encoded=JSON.stringify(candidates),digest=await sha256Hex(encoded);
    return this.db.transaction(this.ctx,()=>{
      const prior=readPolicy(),priorVersion=prior?.version??0;
      const marker=recovery?{protocol,action:b.action}:{};
      if(recovery){
        const check=this.checkPolicyRecovery(prior,b.expected_version,candidates);
        if(!check.changed)return json({...marker,version:priorVersion,candidates,unchanged:true});
      }else{
        if(b.expected_version!==undefined&&b.expected_version!==priorVersion)return error('model_policy_version_conflict',409);
        if(prior?.digest===digest)return json({version:prior.version,candidates,unchanged:true});
        if(priorVersion>=Number.MAX_SAFE_INTEGER)return error('model_policy_version_exhausted',409);
      }
      const version=priorVersion+1;
      this.db.run('INSERT INTO model_policy(id,version,candidates_json,digest) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,candidates_json=excluded.candidates_json,digest=excluded.digest',version,encoded,digest);
      return json({...marker,version,candidates});
    });
  }

  private async scheduleWake():Promise<void>{
    const row=this.db.one<{next_attempt:number}>('SELECT next_attempt FROM mailbox_wakes ORDER BY next_attempt,owner LIMIT 1');
    this.db.alarmOperation();if(row)await this.ctx.storage.setAlarm(Math.max(Date.now()+1,row.next_attempt));else await this.ctx.storage.deleteAlarm();
  }
  async alarm():Promise<void>{
    await this.ready;
    for(const row of this.db.all<{owner:string;revision:number;after_id:string;attempt:number}>('SELECT * FROM mailbox_wakes WHERE next_attempt<=? ORDER BY next_attempt,owner LIMIT 8',Date.now())){
      try{
        const effect=await this.env.MAILBOXES.get(this.env.MAILBOXES.idFromName('global')).fetch(new Request('https://mailbox.internal/_resume-native',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owner:row.owner,after:row.after_id})}));
        if(!effect.ok)throw new Error('wake_refused');
        const result=await effect.json() as {next:unknown;resumed:unknown};
        if(!Number.isInteger(result.resumed)||Number(result.resumed)<0||Number(result.resumed)>16||(result.next!==null&&(!boundedString(result.next,180)||result.next<=row.after_id)))throw new Error('invalid_wake_result');
        if(result.next===null)this.db.run('DELETE FROM mailbox_wakes WHERE owner=? AND revision=?',row.owner,row.revision);
        else this.db.run('UPDATE mailbox_wakes SET after_id=?,next_attempt=?,attempt=0 WHERE owner=? AND revision=?',result.next,Date.now()+1,row.owner,row.revision);
      }catch{this.db.run('UPDATE mailbox_wakes SET attempt=attempt+1,next_attempt=? WHERE owner=? AND revision=?',Date.now()+Math.min(300000,1000*2**Math.min(row.attempt,8)),row.owner,row.revision);}
    }
    await this.scheduleWake();
  }

}
