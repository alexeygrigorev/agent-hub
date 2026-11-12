import {credentialKey,sealCredential,openCredential} from './credential-crypto';
import { MeteredDb, initializeSchema } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';
import type { Actor,Hub2Env } from './types';
type AccountEnv=Partial<Hub2Env>&{HUB2_CREDENTIAL_KEY?:string;HUB2_PROVIDER_FETCH?:typeof fetch};
const PATHS:Record<string,string[]>={codex:['.codex/auth.json'],claude:['.claude/.credentials.json'],zai:['.config/goz/config.json'],gemini:['.gemini/oauth_creds.json'],antigravity:['.gemini/antigravity-cli/antigravity-oauth-token'],go:['.local/share/opencode-hub/device-auth.json']};
const SCHEMA = [
 'CREATE TABLE credential_rejections(host_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,reason TEXT NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE TABLE credential_candidates(id TEXT PRIMARY KEY,host_id TEXT NOT NULL,base_generation INTEGER NOT NULL,sealed TEXT NOT NULL,observed_at INTEGER NOT NULL,state TEXT NOT NULL) WITHOUT ROWID',

 'CREATE INDEX candidate_recovery ON credential_candidates(base_generation,state,id)',
 'CREATE TABLE probes(id INTEGER PRIMARY KEY CHECK(id=1),token TEXT NOT NULL,retry_at INTEGER NOT NULL DEFAULT 0)',
 'CREATE TABLE credentials(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,sealed TEXT NOT NULL,refresh_until INTEGER NOT NULL DEFAULT 0,refresh_owner TEXT,refresh_lease TEXT)',
 'CREATE TABLE approved_hosts(id TEXT PRIMARY KEY,ack_generation INTEGER,acked_at INTEGER)',
 'CREATE TABLE discovery(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,interval_ms INTEGER NOT NULL,next_at INTEGER NOT NULL,status TEXT NOT NULL)',
 'CREATE TABLE alerts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,grant_id TEXT,expires_at INTEGER,observed_at INTEGER NOT NULL,remaining_ms INTEGER)',

 `CREATE TABLE account(id INTEGER PRIMARY KEY CHECK(id=1),provider TEXT NOT NULL,enabled INTEGER NOT NULL,min_remaining REAL NOT NULL DEFAULT 0)`,
 `CREATE TABLE quota(id INTEGER PRIMARY KEY CHECK(id=1),remaining REAL NOT NULL,captured_at INTEGER NOT NULL,valid_until INTEGER NOT NULL,reset_at INTEGER,source TEXT NOT NULL)`,
 `CREATE TABLE reset_grants(id TEXT PRIMARY KEY,expires_at INTEGER NOT NULL,available INTEGER NOT NULL,source TEXT NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID`,
 'CREATE INDEX reset_expiry ON reset_grants(available,expires_at,id)',
 'CREATE INDEX reset_page ON reset_grants(available,id)',
];
type Account={provider:string;enabled:number;min_remaining:number};
type Quota={remaining:number;captured_at:number;valid_until:number;reset_at:number|null;source:string};
type RoutingCredentialStatus={credential_usable:boolean;credential_status:'ready'|'expired'|'refresh_failed'|'missing'|'unknown'};
const ROUTING_REFRESH_MINIMUM_MS=5*60_000;
// Proactive Claude renewal: one durable, secret-free row per credential
// generation. Created idempotently after schema init so a code rollback never
// meets a newer schema version.
const RENEWAL_TABLE='CREATE TABLE IF NOT EXISTS credential_renewal(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL,access_expires_at INTEGER,next_at INTEGER,attempts INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,reason TEXT,updated_at INTEGER NOT NULL)';
const RENEWAL_LEAD_MS=20*60_000;
const RENEWAL_HORIZON_MS=6*3600_000;
const RENEWAL_RETRY_BASE_MS=30_000;
const RENEWAL_RETRY_CAP_MS=5*60_000;
const RENEWAL_MAX_ATTEMPTS=6;
const RENEWAL_SCHEDULE_PASSES=4;
const RENEWAL_ACTIVE=['scheduled','retrying'];
type Renewal={generation:number;access_expires_at:number|null;next_at:number|null;attempts:number;status:string;reason:string|null;updated_at:number};
const DROP_SESSION_CAPACITY = ['DROP TABLE IF EXISTS reservations', 'ALTER TABLE account DROP COLUMN capacity', 'ALTER TABLE account DROP COLUMN occupied'];
/** Provider credentials and quota only; Hub session capacity is global and measured on hosts. */
export class AccountDO {
 private collecting?:Promise<Response>;
 private readonly db:MeteredDb;
 private readonly sqlReceipts:SqlReceiptMeter;
 private readonly env:AccountEnv;
 constructor(private readonly ctx:DurableObjectState,env:AccountEnv={}){this.db=new MeteredDb(ctx.storage.sql);this.sqlReceipts=new SqlReceiptMeter(this.db);this.env=wrapSqlNamespaces(env,this.sqlReceipts) as AccountEnv;initializeSchema(ctx,this.db,2,SCHEMA,[DROP_SESSION_CAPACITY],()=>this.initializeRenewal(),2);}
 async fetch(request:Request):Promise<Response>{
  return this.sqlReceipts.measure(()=>this.handleFetch(request),request.headers.get(SQL_PROBE_HEADER));
 }
 private async handleFetch(request:Request):Promise<Response>{
  const before=this.credentialGeneration();
  const response=await this.route(request);
  // Every maintained path that can publish, import or repair a generation
  // re-arms the expiry-driven renewal alarm; read-only views stay passive.
  if(request.method!=='GET'||this.credentialGeneration()!==before)await this.rearmRenewal();
  return response;
 }
 private async route(request:Request):Promise<Response>{
  try{
   const p=new URL(request.url).pathname,a=actorFrom(request),method=request.method;
   if(p==='/api/account/rotation'||p==='/api/account/refresh-lease'||p==='/api/account/credential-rejection'||p==='/api/account/reconcile'||p==='/api/account/refresh-outcome')return await this.rotationRoute(request,a,p);
   if(p==='/api/account/credentials')return await this.credentials(request,a);
   if(p==='/api/account/hosts')return await this.hosts(request,a);
   if(p==='/api/account/discovery')return await this.discovery(request,a);
   if(p==='/api/account/alerts'&&method==='GET')return this.alerts(request,a);
   if(p==='/api/account/maintain'&&method==='POST')return await this.maintain(a);
   if(p==='/api/account/renewal'&&method==='GET')return this.renewalView(a);
   if(p==='/_internal/routing-snapshot'&&method==='GET')return this.routingView(await this.routingCredentialStatus(true));
   if(p==='/_internal/routing'&&method==='GET'){
    const credential=await this.routingCredentialStatus();
    if(credential.credential_usable)await this.lazyQuota();
    return this.routingView(credential);
   }
   if(p==='/_metrics')return json({counts:this.db.snapshot()});
   if(p==='/api/account'&&method==='PATCH')return await this.configure(request,a);
   if(p==='/api/account/quota'&&method==='PUT')return await this.quota(request,a);
   if(p==='/api/account/resets')return await this.resets(request,a);
   if((p==='/api/account'||p==='/api/account/quota')&&method==='GET'){if(this.canReadQuota(a))await this.lazyQuota();return this.view(a);}
   return error('not_found',404);
  }catch(e){
    if(e instanceof HttpError)return error(e.code,e.status);
    const msg=e instanceof Error?e.message:'';
    if(/^[a-z_]{1,80}$/.test(msg))return error(msg,409);
    return error('internal_error',500);
  }
 }
 private async maintain(actor:Actor|undefined):Promise<Response>{
  if(!hasScope(actor,'operator:admin'))return error('forbidden',403);
  // Reuse the central refresh lease/CAS path; never export credential files.
  let credential=await this.routingCredentialStatus();
  let quota='skipped',reason:string|undefined;
  if(credential.credential_usable){
   const response=await this.collect(true,true);
   quota=response.ok?'observed':'failed';
   if(!response.ok){const body=await response.json() as {error?:string};reason=body.error;}
   credential=await this.routingCredentialStatus(true);
  }
  const generation=this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1')?.generation??null;
  // Explicit operator maintenance re-evaluates renewal from the current
  // generation, including a stopped refused/uncertain/exhausted state.
  this.db.run('DELETE FROM credential_renewal WHERE id=1');
  return json({generation,status:credential.credential_status,quota,...(reason?{reason}:{})});
 }
 private async routingCredentialStatus(passive=false):Promise<RoutingCredentialStatus>{
  const account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
  const row=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1');
  if(!account||!row){
   if(!passive)this.recordCredentialAlert('credential_missing',row?.generation??0);
   return{credential_usable:false,credential_status:'missing'};
  }
  if(account.provider==='zai'){
   if(passive){try{if(!this.validFiles('zai',await this.unseal(row.sealed,'zai',row.generation)))return{credential_usable:false,credential_status:'unknown'};}catch{return{credential_usable:false,credential_status:'unknown'};}}
   return{credential_usable:true,credential_status:'ready'};
  }
  if(!['codex','claude','go'].includes(account.provider)){
   if(!passive)this.recordCredentialAlert('credential_health_unknown',row.generation);
   return{credential_usable:false,credential_status:'unknown'};
  }
  try{
   let current=row;
   let files=await this.unseal(current.sealed,account.provider,current.generation) as Record<string,string>;
   let expires=this.accessExpiry(account.provider,files);
   if(!passive&&['codex','claude'].includes(account.provider)&&this.needsRefresh(account.provider,files,ROUTING_REFRESH_MINIMUM_MS)){
    try{
     await this.refresh(account.provider,files,current.generation);
     current=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1')!;
     files=await this.unseal(current.sealed,account.provider,current.generation) as Record<string,string>;
     expires=this.accessExpiry(account.provider,files);
    }catch{
     const expired=expires===null||expires<=Date.now();
     if(!passive)this.recordCredentialAlert(expired?'credential_expired':'credential_refresh_failed',current.generation);
     return{credential_usable:false,credential_status:expired?'expired':'refresh_failed'};
    }
   }
   if((['codex','claude'].includes(account.provider)&&(expires===null||expires<=Date.now()))
     ||(account.provider==='go'&&expires!==null&&expires<=Date.now())){
    if(!passive)this.recordCredentialAlert('credential_expired',current.generation);
    return{credential_usable:false,credential_status:'expired'};
   }
   return{credential_usable:true,credential_status:'ready'};
  }catch{
   if(!passive)this.recordCredentialAlert('credential_health_unknown',row.generation);
   return{credential_usable:false,credential_status:'unknown'};
  }
 }
 private recordCredentialAlert(kind:string,generation:number):void{
  this.db.run('INSERT OR IGNORE INTO alerts(id,kind,observed_at) VALUES(?,?,?)',`credential:${generation}:${kind}`,kind,Date.now());
 }
 private routingView(credential:RoutingCredentialStatus):Response{
  const account=this.db.one<Account>('SELECT * FROM account WHERE id=1'),quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
  const generationRow=this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1');
  const approved=this.db.all<{id:string}>('SELECT id FROM approved_hosts ORDER BY id LIMIT 51');
  if(approved.length>50)return error('host_observation_limit',503);
  const rejected=approved.length?this.db.all<{host_id:string}>('SELECT host_id FROM credential_rejections WHERE generation=? AND host_id IN ('+approved.map(()=>'?').join(',')+')',generationRow?.generation??-1,...approved.map(v=>v.id)):[];
  const hosts=approved.filter(v=>!rejected.some(r=>r.host_id===v.id));
  if(hosts.length>50)return error('host_observation_limit',503);
  return json({account:account||null,quota:quota||null,fresh:!!quota&&quota.valid_until>Date.now(),hosts:hosts.map(v=>v.id),credential_generation:generationRow?.generation??null,...credential});
 }
 private async configure(r:Request,a:Actor|undefined):Promise<Response>{
  if(!hasScope(a,'operator:admin'))return error('forbidden',403);
  const b=await bodyObject(r,4096);
  if(!boundedString(b.provider,80)||typeof b.enabled!=='boolean'||!percent(b.min_remaining??0))return error('invalid_account',400);
  if(['shared-zai','zcodex','zai-central'].includes(String(b.provider))||b.provider==='zai'&&(this.ctx.id?.name??r.headers.get('x-hub2-account-id'))!=='zai-central')return error('zai_central_account_required',400);
  const prior=this.db.one<{provider:string}>('SELECT provider FROM account WHERE id=1');if(prior&&prior.provider!==b.provider&&this.db.one('SELECT generation FROM credentials WHERE id=1'))return error('provider_change_requires_new_account',409);
  this.db.run(`INSERT INTO account(id,provider,enabled,min_remaining) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,enabled=excluded.enabled,min_remaining=excluded.min_remaining`,b.provider,Number(b.enabled),b.min_remaining??0);
  return this.view(a);
 }
 private async quota(r:Request,a:Actor|undefined):Promise<Response>{
  if(!hasScope(a,'quota:write')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
  const b=await bodyObject(r,4096),now=Date.now();
  if(!percent(b.remaining)||!boundedString(b.source,120)||!Number.isSafeInteger(b.captured_at)||Number(b.captured_at)>now+5000||!Number.isSafeInteger(b.valid_until)||Number(b.valid_until)<=now||Number(b.valid_until)>Number(b.captured_at)+3600000||(b.reset_at!=null&&!Number.isSafeInteger(b.reset_at)))return error('invalid_quota_evidence',400);
  const prior=this.db.one<Quota>('SELECT remaining,captured_at,valid_until,reset_at,source FROM quota WHERE id=1');
  if(prior&&Number(b.captured_at)<prior.captured_at)return error('stale_quota_evidence',409);
  this.db.run(`INSERT INTO quota(id,remaining,captured_at,valid_until,reset_at,source) VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET remaining=excluded.remaining,captured_at=excluded.captured_at,valid_until=excluded.valid_until,reset_at=excluded.reset_at,source=excluded.source`,b.remaining,b.captured_at,b.valid_until,b.reset_at??null,b.source);
  this.db.run('UPDATE probes SET token=? WHERE id=1',crypto.randomUUID());return json({recorded:true});
 }
 private canReadQuota(a:Actor|undefined):boolean{return hasScope(a,'quota:read')||hasScope(a,'operator:admin')||!!(a?.kind==='machine'&&a.resource_id&&this.db.one('SELECT id FROM approved_hosts WHERE id=?',a.resource_id));}
 private view(a:Actor|undefined):Response{
  if(!this.canReadQuota(a))return error('forbidden',403);
  const account=this.db.one<Account>('SELECT * FROM account WHERE id=1'),quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
  return json({account:account||null,quota:quota||null,fresh:!!quota&&quota.valid_until>Date.now()});
 }
 private async schedule():Promise<void>{
  this.db.alarmOperation();
  // The renewal read awaits an unseal. A concurrent import/rotation can publish
  // a newer generation (and rearm it) meanwhile, so a result computed for a
  // superseded generation is never published: recompute, bounded, and leave
  // the newer generation's alarm untouched if it keeps moving.
  for(let pass=0;pass<RENEWAL_SCHEDULE_PASSES;pass++){
   const due:number[]=[],startGeneration=this.credentialGeneration();
   const discovery=this.db.one<{next_at:number}>('SELECT next_at FROM discovery WHERE id=1 AND enabled=1');if(discovery)due.push(discovery.next_at);
   const grants=this.db.all<{id:string;expires_at:number}>('SELECT id,expires_at FROM reset_grants WHERE available=1 ORDER BY expires_at,id LIMIT 33');
   if(grants.length>32)throw new HttpError('reset_observation_limit',503);
   for(const g of grants){const key='reset:'+g.id+':'+g.expires_at+':24h';if(g.expires_at>Date.now()&&!this.db.one('SELECT id FROM alerts WHERE id=?',key))due.push(Math.max(Date.now()+100,g.expires_at-86400000));}
   const renewal=await this.renewalDue();
   if(renewal==='superseded'||this.credentialGeneration()!==startGeneration)continue;
   // Unknown is not "no work": keep a bounded retry instead of dropping the alarm.
   if(renewal==='unknown')due.push(Date.now()+RENEWAL_RETRY_CAP_MS);else if(renewal!==null)due.push(renewal);
   // Fenced: no await between the generation check above and the publish below.
   if(due.length)await this.ctx.storage.setAlarm(Math.max(Date.now()+100,Math.min(...due)));else await this.ctx.storage.deleteAlarm();
   return;
  }
 }
 async alarm():Promise<void>{
  this.resetAlerts();
  // Renew before any due discovery probe so it observes the fresh generation;
  // discovery keeps its own next_at and is never advanced by a renewal wake.
  await this.renewIfDue();
  const d=this.db.one<{next_at:number;interval_ms:number}>('SELECT next_at,interval_ms FROM discovery WHERE id=1 AND enabled=1');
  if(d&&d.next_at<=Date.now()){this.db.run('UPDATE discovery SET next_at=? WHERE id=1',Date.now()+d.interval_ms);await this.collect();}
  await this.schedule();
 }
 private async resets(r:Request,a:Actor|undefined):Promise<Response>{
  if(r.method==='PUT'){
   if(!hasScope(a,'quota:write')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
   const b=await bodyObject(r,4096);
   if(!boundedString(b.id,120)||!Number.isSafeInteger(b.expires_at)||typeof b.available!=='boolean'||!boundedString(b.source,120))return error('invalid_reset',400);
   this.db.run(`INSERT INTO reset_grants(id,expires_at,available,source,observed_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires_at=excluded.expires_at,available=excluded.available,source=excluded.source,observed_at=excluded.observed_at`,b.id,b.expires_at,Number(b.available),b.source,Date.now());
   this.resetAlerts();await this.schedule();return json({recorded:true,redemption:'explicit_approval_required'});
  }
  if(r.method!=='GET')return error('not_found',404);
  if(!hasScope(a,'quota:read')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
  const cursor=new URL(r.url).searchParams.get('cursor')||'';
  const items=this.db.all<{id:string}>('SELECT id,expires_at,available,source,observed_at FROM reset_grants WHERE available=1 AND id>? ORDER BY id LIMIT 51',cursor);
  return json({items:items.slice(0,50),next:items.length>50?items[49].id:null});
 }
 private async credentials(r:Request,a:Actor|undefined):Promise<Response>{
  const machine=a?.kind==='machine'?a.resource_id:undefined;
  if(r.method==='GET'){
   if(!machine||!this.db.one('SELECT id FROM approved_hosts WHERE id=?',machine))return error('host_not_approved',403);
   let row=this.db.one<{generation:number;sealed:string;refresh_owner?:string;refresh_until?:number;refresh_lease?:string|null}>('SELECT generation,sealed,refresh_owner,refresh_until,refresh_lease FROM credentials WHERE id=1');
   if(!row)return error('credentials_unavailable',503);
   const query=new URL(r.url).searchParams,requested=query.get('refresh_lease'),repairLease=!!requested&&row.refresh_owner===machine&&row.refresh_lease===requested;
   if(requested&&!repairLease)return error('refresh_permit_mismatch',409);
   const account=this.db.one<Account>('SELECT * FROM account WHERE id=1');if(!account)return error('account_unconfigured',409);
   if(query.get('metadata')==='1')return json({generation:row.generation,provider:account.provider,refresh_mode:account.provider==='zai'?'static':'central'});
   if(query.has('expected_generation')){
    // Exact-generation observation for a host release re-pin: synchronous,
    // never unseals, refreshes or returns material (no awaits below).
    const raw=query.get('expected_generation')||'';
    if(requested||[...query.keys()].length!==1||!/^[1-9][0-9]{0,15}$/.test(raw)||!Number.isSafeInteger(Number(raw)))return error('invalid_generation_check',400);
    if(account.provider!=='claude')return error('account_provider_mismatch',409);
    if(row.generation!==Number(raw))return error('generation_conflict',409);
    if(this.db.one('SELECT host_id FROM credential_rejections WHERE host_id=? AND generation=?',machine,row.generation))return error('credential_generation_rejected',409);
    if(Number(row.refresh_until||0)>Date.now()||row.refresh_owner||row.refresh_lease)return error('refresh_in_progress',409);
    if(this.hasUnresolvedRefresh(row.generation))return error('refresh_outcome_uncertain',503);
    return json({generation:row.generation,provider:'claude',status:'current'});
   }
   if(!repairLease&&this.db.one('SELECT host_id FROM credential_rejections WHERE host_id=? AND generation=?',machine,row.generation))return error('credential_generation_rejected',409);
   const minimum=Number(query.get('min_validity_ms')||0);if(!Number.isSafeInteger(minimum)||minimum<0||minimum>10800000)return error('invalid_access_validity',400);
   let files=await this.unseal(row.sealed,account.provider,row.generation) as Record<string,string>;
   if(!repairLease&&account&&['codex','claude'].includes(account.provider)&&this.needsRefresh(account.provider,files,minimum)){
    await this.refresh(account.provider,files,row.generation);row=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1')!;files=await this.unseal(row.sealed,account.provider,row.generation) as Record<string,string>;
   }
   if(!this.db.one('SELECT id FROM approved_hosts WHERE id=?',machine))return error('host_not_approved',403);
   const latest=this.db.one<{generation:number;refresh_owner:string|null;refresh_lease:string|null}>('SELECT generation,refresh_owner,refresh_lease FROM credentials WHERE id=1');if(latest?.generation!==row.generation)return error('generation_conflict',409);
   if(repairLease&&(latest.refresh_owner!==machine||latest.refresh_lease!==requested))return error('refresh_permit_mismatch',409);
   if(!repairLease&&this.db.one('SELECT host_id FROM credential_rejections WHERE host_id=? AND generation=?',machine,row.generation))return error('credential_generation_rejected',409);
   const expires=this.accessExpiry(account.provider,files);
   if(!repairLease&&account.provider!=='zai'&&minimum>0&&(expires===null||expires<=Date.now()+minimum))return error('access_lifetime_insufficient',409);
   if(!repairLease&&account.provider!=='zai'){
    const strip=(v:any):void=>{if(v&&typeof v==='object')for(const key of Object.keys(v)){if(key.toLowerCase().includes('refresh')&&typeof v[key]==='string')v[key]='';else strip(v[key]);}};
    files={...files};for(const path of Object.keys(files))if(path.endsWith('.json')||path.endsWith('antigravity-oauth-token')){const auth=JSON.parse(files[path]),lastRefresh=path==='.codex/auth.json'&&typeof auth.last_refresh==='string'&&Number.isFinite(Date.parse(auth.last_refresh))?auth.last_refresh:undefined;strip(auth);if(lastRefresh!==undefined)auth.last_refresh=lastRefresh;files[path]=JSON.stringify(auth);}
   }
   return json({generation:row.generation,provider:account.provider,files,access_expires_at:expires,refresh_material:repairLease,static_key:account.provider==='zai'});
  }
  if(r.method==='POST'){
   if(!machine)return error('forbidden',403);const b=await bodyObject(r,4096);
   const row=this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1');
   if(!row||row.generation!==b.generation||!this.db.one('SELECT id FROM approved_hosts WHERE id=?',machine))return error('generation_conflict',409);
   this.db.run('UPDATE approved_hosts SET ack_generation=?,acked_at=? WHERE id=?',b.generation,Date.now(),machine);return json({acknowledged:true});
  }
  if(r.method!=='PUT'||!hasScope(a,'operator:admin'))return error('forbidden',403);
  const b=await bodyObject(r,96000),account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
  if(!account)return error('account_unconfigured',409);
  if(b.provider!==undefined&&b.provider!==account.provider||account.provider==='zai'&&b.account_id!==undefined&&b.account_id!=='zai-central')return error('credential_metadata_mismatch',400);
  const files=b.files;
  if(!this.validFiles(account.provider,files))return error('invalid_credentials',400);
  if(!files||typeof files!=='object'||Array.isArray(files))return error('invalid_credentials',400);
  const entries=Object.entries(files);
  if(!entries.length||entries.length>8||!entries.every(([p,v])=>(PATHS[account.provider]||[]).includes(p)&&typeof v==='string'&&v.length>0&&v.length<=16000&&!v.includes('\0')))return error('invalid_credentials',400);
  for(const [p,v] of entries)if(p.endsWith('.json')){try{const value=JSON.parse(String(v));if(!value||typeof value!=='object'||Array.isArray(value)&&account.provider!=='go')return error('invalid_credentials',400);}catch{return error('invalid_credentials',400);}}
  if(['codex','claude'].includes(account.provider)){
   const auth=JSON.parse(String((files as Record<string,string>)[PATHS[account.provider][0]]));
   if(account.provider==='codex'&&(typeof auth?.tokens?.access_token!=='string'||typeof auth?.tokens?.refresh_token!=='string'))return error('invalid_credentials',400);
   if(account.provider==='claude'&&typeof auth?.claudeAiOauth?.accessToken!=='string')return error('invalid_credentials',400);
  }
  if(!Number.isSafeInteger(b.expected_generation)||Number(b.expected_generation)<0)return error('invalid_generation',400);
  if(b.native_import===true){
   if(account.provider!=='claude')return error('invalid_native_import',400);
   const prior=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1');
   if((prior?.generation||0)!==b.expected_generation)return error('generation_conflict',409);
   const incoming=this.freshness(account.provider,files as Record<string,string>);
   if(incoming===null||incoming<=Date.now())return error('invalid_native_import',400);
   if(prior){
    const previous=await this.unseal(prior.sealed,account.provider,prior.generation) as Record<string,string>;
    const existing=this.freshness(account.provider,previous);
    if(existing!==null&&incoming<=existing){
     const failed=this.db.one('SELECT host_id FROM credential_rejections WHERE generation=? LIMIT 1',prior.generation)
      ||this.db.one('SELECT id FROM alerts WHERE id IN (?,?)',`credential:${prior.generation}:credential_refresh_failed`,`credential:${prior.generation}:credential_expired`);
     if(!failed)return error('native_credential_not_newer',409);
     // A revoked token can expire later than a working native login. Recovery
     // requires current-generation failure evidence plus live native auth proof.
     // This is not an Opus health result and never clears an auth incident.
     try{
      const probe=await this.providerQuota('claude',JSON.parse(String((files as Record<string,string>)[PATHS.claude[0]])));
      if(!probe.ok)return error('native_credential_unverified',409);
      await this.boundedJson(probe);
     }catch{return error('native_credential_unverified',503);}
    }
   }
  }
  const sealed=await this.seal(files,account.provider,Number(b.expected_generation)+1);
  const generation=this.db.transaction(this.ctx,()=>{
   const current=this.db.one<Account>('SELECT * FROM account WHERE id=1');if(current?.provider!==account.provider)throw new HttpError('provider_changed',409);
   const row=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');
   if((row?.generation||0)!==b.expected_generation)throw new HttpError('generation_conflict',409);
   if(row&&row.refresh_until>Date.now())throw new HttpError('refresh_in_progress',409);
   this.db.run('UPDATE probes SET token=?,retry_at=0 WHERE id=1',crypto.randomUUID());
   const next=(row?.generation||0)+1;this.db.run('INSERT INTO credentials(id,generation,sealed) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,sealed=excluded.sealed',next,sealed);this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');return next;
  });return json({generation});
 }
 private async hosts(r:Request,a:Actor|undefined):Promise<Response>{
  if(!hasScope(a,'operator:admin'))return error('forbidden',403);
  if(r.method==='GET')return json({items:this.db.all('SELECT id,ack_generation,acked_at FROM approved_hosts ORDER BY id LIMIT 51')});
  if(r.method!=='PUT')return error('not_found',404);const b=await bodyObject(r,16000);
  if(!Array.isArray(b.hosts)||b.hosts.length>50||!b.hosts.every(v=>boundedString(v,120)))return error('invalid_hosts',400);
  this.db.transaction(this.ctx,()=>{const old=this.db.all<{id:string}>('SELECT id FROM approved_hosts LIMIT 51');if(old.length>50)throw new HttpError('host_observation_limit',503);for(const h of old)if(!(b.hosts as string[]).includes(h.id))this.db.run('DELETE FROM approved_hosts WHERE id=?',h.id);for(const h of new Set(b.hosts as string[]))this.db.run('INSERT OR IGNORE INTO approved_hosts(id) VALUES(?)',h);});
  return json({recorded:true});
 }
 private async discovery(r:Request,a:Actor|undefined):Promise<Response>{
  if(!hasScope(a,'quota:write')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
  if(r.method==='POST'){const result=await this.collect(true,true);await this.schedule();return result;}
  if(r.method==='GET')return json({discovery:this.db.one('SELECT enabled,interval_ms,next_at,status FROM discovery WHERE id=1')||null});
  if(r.method!=='PUT')return error('not_found',404);const b=await bodyObject(r,4096),interval=Number(b.interval_ms??21600000);
  if(typeof b.enabled!=='boolean'||!Number.isInteger(interval)||interval<3600000||interval>21600000)return error('invalid_discovery_schedule',400);
  this.db.run('INSERT INTO discovery(id,enabled,interval_ms,next_at,status) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,interval_ms=excluded.interval_ms,next_at=excluded.next_at',Number(b.enabled),interval,Date.now()+100,'unobserved');await this.schedule();return json({recorded:true});
 }
 private async lazyQuota():Promise<void>{
  const quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');if(quota&&quota.valid_until>Date.now())return;
  const account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
  if(account&&['codex','claude','go','zai'].includes(account.provider)&&this.db.one('SELECT generation FROM credentials WHERE id=1'))await this.collect(false);
 }
 private collect(force=true,supersede=false):Promise<Response>{
  if(this.collecting&&!supersede)return this.collecting;
  if(!force&&(this.db.one<{retry_at:number}>('SELECT retry_at FROM probes WHERE id=1')?.retry_at||0)>Date.now())return Promise.resolve(error('quota_probe_backoff',503));
  const pending=this.collectNow().finally(()=>{if(this.collecting===pending)this.collecting=undefined;});this.collecting=pending;return pending;
 }
 private async collectNow():Promise<Response>{
  const result=await this.collectProvider();
  return result;
 }
 private async collectProvider():Promise<Response>{
  const probe=crypto.randomUUID();this.db.run('INSERT INTO probes(id,token,retry_at) VALUES(1,?,0) ON CONFLICT(id) DO UPDATE SET token=excluded.token,retry_at=0',probe);
  const fail=(reason:string,invalidate=true)=>this.unknown(reason,probe,invalidate);let quotaProved=false;
  try{
   const account=this.db.one<Account>('SELECT * FROM account WHERE id=1'),row=this.db.one<{generation:number;sealed:string;refresh_until:number}>('SELECT generation,sealed,refresh_until FROM credentials WHERE id=1');
   if(!account||!row)return fail('credentials_unavailable');
   if(!['codex','claude','go','zai'].includes(account.provider))return fail('provider_collector_unavailable');
   const files=await this.unseal(row.sealed,account.provider,row.generation) as Record<string,string>;
   let expectedGeneration=row.generation;let auth=JSON.parse(files[PATHS[account.provider][0]]||'null');
   let response=await this.providerQuota(account.provider,auth);
   if(response.status===401&&['codex','claude'].includes(account.provider)){auth=await this.refresh(account.provider,files,row.generation);expectedGeneration++;response=await this.providerQuota(account.provider,auth);}
   if(!response.ok)return fail('quota_http_refused');const payload=await this.boundedJson(response);
   const limits=account.provider==='zai'?(payload?.data??payload)?.limits:null;
   if(account.provider==='zai'&&(!Array.isArray(limits)||limits.length>32))return fail('quota_invalid_response');
   const windows:any[]=account.provider==='zai'?limits.filter((v:any)=>v?.type==='TOKENS_LIMIT'&&[3,6].includes(v.unit)):account.provider==='codex'?[payload?.rate_limit?.primary_window,payload?.rate_limit?.secondary_window]:account.provider==='go'?[payload?.usage?.rolling,payload?.usage?.weekly,payload?.usage?.monthly]:[payload?.five_hour,payload?.seven_day];
   const available=windows.filter(v=>v&&typeof v==='object');
   const used=available.map(v=>account.provider==='zai'?v.percentage:account.provider==='codex'?v.used_percent:account.provider==='go'?(v.percent??v.usagePercent):v.utilization);
   if(!used.length||!used.every(percent))return fail('quota_invalid_response');
   const remaining=(payload?.rate_limit?.limit_reached===true||account.provider==='go'&&available.some(v=>['exhausted','limited','rate-limited'].includes(String(v.status).toLowerCase().replaceAll('_','-'))))?0:Math.min(...used.map(n=>100-n)),now=Date.now();
   const resets=available.map(v=>timestamp(account.provider==='zai'?v.nextResetTime:account.provider==='codex'?v.reset_at:account.provider==='go'?v.resetsAt:v.resets_at)).filter((v):v is number=>v!==null);const resetAt=resets.length?Math.min(...resets):null;
   if(this.db.one<{token:string}>('SELECT token FROM probes WHERE id=1')?.token!==probe)return error('probe_superseded',409);
   if(this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1')?.generation!==expectedGeneration)return fail('probe_generation_superseded');
   this.db.run('INSERT INTO quota(id,remaining,captured_at,valid_until,reset_at,source) VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET remaining=excluded.remaining,captured_at=excluded.captured_at,valid_until=excluded.valid_until,reset_at=excluded.reset_at,source=excluded.source',remaining,now,now+300000,resetAt,'provider:'+account.provider+':generation:'+expectedGeneration);quotaProved=true;
   if(account.provider==='codex'){
    const token=auth?.tokens?.access_token;
    const response=await this.provider('https://chatgpt.com/backend-api/wham/rate-limit-reset-credits',{headers:this.codexHeaders(auth)});
    if(!response.ok)return fail('reset_discovery_refused',false);const payload=await this.boundedJson(response);
    if(!Array.isArray(payload?.credits)||payload.credits.length>32)return fail('reset_discovery_invalid',false);
    const grants=[] as {id:string;expires:number;available:boolean}[];
    for(const g of payload.credits){const expires=timestamp(g.expires_at);if(expires===null)return fail('reset_discovery_invalid',false);const id=typeof g.id==='string'&&g.id.length<=120?g.id:await hash(JSON.stringify([g.title,expires]));grants.push({id,expires,available:g.status==='available'});}
    if(this.db.one<{token:string}>('SELECT token FROM probes WHERE id=1')?.token!==probe)return error('probe_superseded',409);
   if(this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1')?.generation!==expectedGeneration)return fail('probe_generation_superseded');
    this.db.transaction(this.ctx,()=>{
     const prior=this.db.all<{id:string}>('SELECT id FROM reset_grants WHERE available=1 ORDER BY id LIMIT 33');if(prior.length>32)throw new HttpError('reset_observation_limit',503);
     for(const g of prior)if(!grants.some(v=>v.id===g.id))this.db.run('UPDATE reset_grants SET available=0 WHERE id=?',g.id);
     for(const g of grants)this.db.run('INSERT INTO reset_grants(id,expires_at,available,source,observed_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires_at=excluded.expires_at,available=excluded.available,source=excluded.source,observed_at=excluded.observed_at',g.id,g.expires,Number(g.available),'provider:codex',now);
     this.resetAlerts();
    });
   }
   this.db.run('UPDATE discovery SET status=? WHERE id=1','observed');return json({observed:true,remaining});
  }catch(e){return fail('provider_probe_failed',!quotaProved);}
 }
 private unknown(reason:string,probe:string,invalidate=true):Response{
  if(this.db.one<{token:string}>('SELECT token FROM probes WHERE id=1')?.token!==probe)return error('probe_superseded',409);
  if(invalidate)this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');
  this.db.run('UPDATE probes SET retry_at=? WHERE id=1 AND token=?',Date.now()+60000,probe);
  this.db.run('UPDATE discovery SET status=? WHERE id=1',reason);
  const bucket=Math.floor(Date.now()/21600000);this.db.run('INSERT OR IGNORE INTO alerts(id,kind,observed_at) VALUES(?,?,?)','discovery:'+bucket+':'+reason,reason,Date.now());return error(reason,503);
 }
 private resetAlerts():void{
  const grants=this.db.all<{id:string;expires_at:number}>('SELECT id,expires_at FROM reset_grants WHERE available=1 ORDER BY expires_at,id LIMIT 33');if(grants.length>32)throw new HttpError('reset_observation_limit',503);
  for(const g of grants){const left=g.expires_at-Date.now(),stage=left>86400000?'36h':'24h';
   this.db.run('INSERT OR IGNORE INTO alerts(id,kind,grant_id,expires_at,observed_at,remaining_ms) VALUES(?,?,?,?,?,?)','reset:'+g.id+':'+g.expires_at+':'+stage,'reset_expiry_'+stage,g.id,g.expires_at,Date.now(),left);
  }
 }
 private alerts(r:Request,a:Actor|undefined):Response{
  if(!hasScope(a,'quota:read')&&!hasScope(a,'operator:admin'))return error('forbidden',403);const cursor=new URL(r.url).searchParams.get('cursor')||'';
  const rows=this.db.all<{id:string}>('SELECT * FROM alerts WHERE id>? ORDER BY id LIMIT 51',cursor);return json({items:rows.slice(0,50),next:rows.length>50?rows[49].id:null});
 }
 private accessExpiry(provider:string,files:Record<string,string>):number|null{
  try{const auth=JSON.parse(files[PATHS[provider][0]]);if(provider==='claude')return timestamp(auth?.claudeAiOauth?.expiresAt);
   const token=provider==='codex'?auth?.tokens?.access_token:provider==='go'&&Array.isArray(auth)?auth.find(v=>v?.active===true)?.value?.access:auth?.access_token??auth?.token?.access_token;
   if(typeof token==='string'){const part=token.split('.')[1],exp=JSON.parse(atob(part.replace(/-/g,'+').replace(/_/g,'/'))).exp;if(typeof exp==='number'&&Number.isFinite(exp))return exp*1000;}
   return timestamp(auth?.expiry_date??auth?.token?.expiry);
  }catch{return null;}
 }
 private needsRefresh(provider:string,files:Record<string,string>,minimum=0):boolean{
  try{const auth=JSON.parse(files[PATHS[provider][0]]),expires=this.accessExpiry(provider,files);
   if(provider==='claude')return typeof auth?.claudeAiOauth?.refreshToken==='string'&&(expires===null||expires<Date.now()+Math.max(minimum,300000));
   return provider==='codex'&&typeof auth?.tokens?.refresh_token==='string'&&(expires===null||expires<Date.now()+minimum||!Number.isFinite(Date.parse(auth.last_refresh))||Date.parse(auth.last_refresh)+691200000<Date.now()+minimum);
  }catch{return false;}
 }
 private async refresh(provider:string,files:Record<string,string>,generation:number):Promise<any>{
  return await this.refreshProvider(provider,files,generation);
 }
 private async refreshProvider(provider:string,files:Record<string,string>,generation:number):Promise<any>{
  const until=Date.now()+30000,attempt=crypto.randomUUID();
  this.db.transaction(this.ctx,()=>{const row=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');if(!row||row.generation!==generation||row.refresh_until>Date.now())throw new HttpError('refresh_in_progress',409);if(this.hasUnresolvedRefresh(generation))throw new HttpError('refresh_outcome_uncertain',503);this.db.run('UPDATE credentials SET refresh_until=?,refresh_owner=?,refresh_lease=NULL WHERE id=1',until,'central');});
  this.db.run('INSERT INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES(?,?,?,?,?,?)',attempt,'hub:central',generation,'',Date.now(),'issued');
  try{
   const path=PATHS[provider][0],auth=JSON.parse(files[path]),refresh=provider==='codex'?auth?.tokens?.refresh_token:auth?.claudeAiOauth?.refreshToken;
   if(typeof refresh!=='string')throw new HttpError('provider_refresh_missing',400);
   const response=await this.provider(provider==='codex'?'https://auth.openai.com/oauth/token':'https://platform.claude.com/v1/oauth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:refresh,client_id:provider==='codex'?'app_EMoamEEZ73f0CkXaXp7hrann':'9d1c250a-e61b-44d9-88ed-5944d1962f5e'})});
   if(!response.ok){
    let refusedCode='provider_refresh_refused';
    if([400,401,403].includes(response.status)){
     try{
      const failure=await this.boundedJson(response);
      if(['invalid_grant','invalid_client','access_denied','invalid_request'].includes(String(failure.error))){
       this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','refused',attempt);
       refusedCode='provider_refresh_'+failure.error;
      }
     }catch{}
    }
    throw new HttpError(refusedCode,409);
   }
   const b=await this.boundedJson(response);if(typeof b.access_token!=='string')throw new HttpError('provider_refresh_invalid',502);
   if(provider==='codex'){auth.tokens.access_token=b.access_token;if(typeof b.refresh_token==='string')auth.tokens.refresh_token=b.refresh_token;if(typeof b.id_token==='string')auth.tokens.id_token=b.id_token;auth.last_refresh=new Date().toISOString();}
   else{auth.claudeAiOauth.accessToken=b.access_token;if(typeof b.refresh_token==='string')auth.claudeAiOauth.refreshToken=b.refresh_token;auth.claudeAiOauth.expiresAt=Date.now()+Number(b.expires_in||3600)*1000;}
   files[path]=JSON.stringify(auth);const sealed=await this.seal(files,provider,generation+1);
   this.db.run('UPDATE credential_candidates SET sealed=?,state=? WHERE id=?',sealed,'pending',attempt);
   this.db.transaction(this.ctx,()=>{const row=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');if(row?.generation!==generation||row.refresh_until!==until)throw new HttpError('provider_refresh_superseded',409);this.db.run('UPDATE credentials SET sealed=?,generation=generation+1,refresh_until=0,refresh_owner=NULL,refresh_lease=NULL WHERE id=1',sealed);this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','published',attempt);});return auth;
  }catch(e){
   if(['codex','claude'].includes(provider)){
    this.recordCredentialAlert('credential_refresh_failed',generation);
    const expires=this.accessExpiry(provider,files);
    if(expires===null||expires<=Date.now())this.recordCredentialAlert('credential_expired',generation);
   }
   this.db.run('INSERT OR IGNORE INTO alerts(id,kind,observed_at) VALUES(?,?,?)','refresh:'+attempt,'refresh_publication_reconciliation_required',Date.now());throw e;
  }finally{this.db.run('UPDATE credentials SET refresh_until=0,refresh_owner=NULL,refresh_lease=NULL WHERE id=1 AND generation=? AND refresh_until=?',generation,until);}
 }
 private codexHeaders(auth:any):Record<string,string>{const token=auth?.tokens?.access_token;if(typeof token!=='string')throw new Error('credential_missing');return {Authorization:'Bearer '+token,Accept:'application/json','User-Agent':'codex_cli_rs/0.162.1',originator:'codex_cli_rs',...(typeof auth.tokens.account_id==='string'?{'ChatGPT-Account-Id':auth.tokens.account_id}:{})};}
 private providerQuota(provider:string,auth:any):Promise<Response>{
  if(provider==='zai'){if(typeof auth?.zai_token!=='string'||!auth.zai_token||/^enc:/i.test(auth.zai_token)||/\s/.test(auth.zai_token))throw new Error('credential_missing');return this.provider('https://api.z.ai/api/monitor/usage/quota/limit',{headers:{Authorization:'Bearer '+auth.zai_token,Accept:'application/json','Content-Type':'application/json','User-Agent':'agent-hub-quota/1'}});}
  if(provider==='go'){const token=Array.isArray(auth)?auth.find(v=>v&&v.active===true)?.value?.access:undefined;if(typeof token!=='string'||!token)throw new Error('credential_missing');return this.provider('https://opencode.ai/zen/go/v1/usage',{headers:{Authorization:'Bearer '+token,Accept:'application/json','User-Agent':'opencode/1.19'}});}
  const token=auth?.claudeAiOauth?.accessToken;if(provider==='claude'&&typeof token!=='string')throw new Error('credential_missing');
  return this.provider(provider==='codex'?'https://chatgpt.com/backend-api/wham/usage':'https://api.anthropic.com/api/oauth/usage',{headers:provider==='codex'?this.codexHeaders(auth):{Authorization:'Bearer '+String(token),Accept:'application/json','anthropic-beta':'oauth-2025-04-20','User-Agent':'Claude-Code/2.1.198'}});
 }
 private async provider(url:string,init:RequestInit,timeoutMs=15000):Promise<Response>{
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeoutMs);let consumed=false;
  try{
   const response=await (this.env.HUB2_PROVIDER_FETCH||fetch)(url,{...init,redirect:'manual',signal:controller.signal});
   const text=response.body?await this.boundedText(response):null;consumed=true;
   return new Response(text,{status:response.status,statusText:response.statusText,headers:response.headers});
  }finally{clearTimeout(timer);if(!consumed)controller.abort();}
 }
 private async boundedText(response:Response):Promise<string>{
  const reader=response.body?.getReader();if(!reader)throw new Error('empty_response');let size=0;const chunks:Uint8Array[]=[];
  try{
   while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>131072){await reader.cancel().catch(()=>{});throw new Error('oversized_response');}chunks.push(value);}
  }catch(e){await reader.cancel().catch(()=>{});throw e;}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}return new TextDecoder().decode(bytes);
 }
 private async boundedJson(response:Response):Promise<any>{return JSON.parse(await this.boundedText(response));}
 private async key():Promise<CryptoKey>{try{return await credentialKey(this.env.HUB2_CREDENTIAL_KEY||'');}catch{throw new HttpError('credential_key_unavailable',503);}}
 private async seal(value:unknown,provider:string,generation:number):Promise<string>{const sealed=await sealCredential(await this.key(),new TextEncoder().encode(JSON.stringify(value)),new TextEncoder().encode(JSON.stringify([this.ctx.id?.toString()||'test-account',provider,generation])));return JSON.stringify({iv:Array.from(sealed.iv),data:Array.from(sealed.data)});}
 private async unseal(value:string,provider:string,generation:number):Promise<unknown>{try{const b=JSON.parse(value),plain=await openCredential(await this.key(),new Uint8Array(b.iv),new Uint8Array(b.data),new TextEncoder().encode(JSON.stringify([this.ctx.id?.toString()||'test-account',provider,generation])));return JSON.parse(new TextDecoder().decode(plain));}catch{throw new HttpError('credential_decryption_failed',503);}}

 private async rotationRoute(r:Request,a:Actor|undefined,path:string):Promise<Response>{
  const admin=hasScope(a,'operator:admin'),host=a?.kind==='machine'?a.resource_id:undefined;
  if(!admin&&(!host||!this.db.one('SELECT id FROM approved_hosts WHERE id=?',host)))return error('forbidden',403);
  if(r.method!=='POST')return error('not_found',404);const b=await bodyObject(r,96000);
  if(host&&!admin&&!this.db.one('SELECT id FROM approved_hosts WHERE id=?',host))return error('host_not_approved',403);
  const current=this.db.one<{generation:number;sealed:string;refresh_until:number;refresh_owner:string|null;refresh_lease:string|null}>('SELECT * FROM credentials WHERE id=1'),account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
  if(!current||!account)return error('credentials_unavailable',503);
  if(path==='/api/account/credential-rejection'){
   if(!host||b.generation!==current.generation||!['provider_unauthorized','credential_rejected'].includes(String(b.reason)))return error('invalid_rejection',400);
   this.db.run('INSERT INTO credential_rejections(host_id,generation,reason,observed_at) VALUES(?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET generation=excluded.generation,reason=excluded.reason,observed_at=excluded.observed_at',host,b.generation,b.reason,Date.now());
   this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');return json({recorded:true});
  }
  if(path==='/api/account/refresh-lease'){
   if(account.provider==='zai')return error('static_key_has_no_oauth_refresh',409);
   if(!host||b.generation!==current.generation)return error('generation_conflict',409);
   if(current.refresh_until>Date.now())return current.refresh_owner===host?json({lease:current.refresh_lease,generation:current.generation,expires_at:current.refresh_until}):error('refresh_in_progress',409);
   if(this.hasUnresolvedRefresh(current.generation))return error('refresh_outcome_uncertain',503);
   const lease=crypto.randomUUID(),expires=Date.now()+120000;
   this.db.transaction(this.ctx,()=>{this.db.run('INSERT INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES(?,?,?,?,?,?)',lease,host,current.generation,'',Date.now(),'issued');this.db.run('UPDATE credentials SET refresh_owner=?,refresh_lease=?,refresh_until=? WHERE id=1',host,lease,expires);});return json({lease,generation:current.generation,expires_at:expires});
  }
  if(path==='/api/account/refresh-outcome'){
   const prior=this.db.one<{host_id:string;base_generation:number;state:string}>('SELECT host_id,base_generation,state FROM credential_candidates WHERE id=?',b.lease??'');
   if(host&&prior?.host_id===host&&prior.base_generation===b.generation&&prior.state===b.outcome&&['not-started','refused'].includes(String(b.outcome)))return json({recorded:true,replayed:true});
   if(!host||b.generation!==current.generation||b.lease!==current.refresh_lease||current.refresh_owner!==host||!['not-started','refused'].includes(String(b.outcome)))return error('refresh_outcome_mismatch',409);
   if(b.outcome==='refused'&&(![400,401,403].includes(Number(b.provider_status))||!['invalid_grant','invalid_client','access_denied','invalid_request'].includes(String(b.provider_error))))return error('definite_provider_refusal_required',400);
   this.db.transaction(this.ctx,()=>{this.db.run('UPDATE credential_candidates SET state=? WHERE id=? AND host_id=? AND base_generation=?',b.outcome==='not-started'?'not-started':'refused',b.lease,host,current.generation);this.db.run('UPDATE credentials SET refresh_until=0,refresh_owner=NULL,refresh_lease=NULL WHERE id=1');});return json({recorded:true});
  }
  if(path==='/api/account/reconcile'){
   if(!admin||!boundedString(b.candidate_id,120)||b.expected_generation!==current.generation)return error('reconciliation_forbidden',403);
   const candidate=this.db.one<{base_generation:number;sealed:string;state:string}>('SELECT base_generation,sealed,state FROM credential_candidates WHERE id=?',b.candidate_id);
   if(!candidate)return error('candidate_not_found',404);if(candidate.state==='issued'||!candidate.sealed)return error('candidate_outcome_uncertain',503);
   const files=await this.unseal(candidate.sealed,account.provider,candidate.base_generation+1) as Record<string,string>;
   if(['codex','claude','go','zai'].includes(account.provider)){const auth=JSON.parse(files[PATHS[account.provider][0]]);if(!(await this.providerQuota(account.provider,auth)).ok)return error('candidate_provider_refused',409);}
   return this.publishRotation(files,account.provider,current.generation,undefined,undefined,b.candidate_id);
  }
  if(!host||!Number.isSafeInteger(b.expected_generation)||Number(b.expected_generation)<1||!this.validFiles(account.provider,b.files))return error('invalid_rotation',400);
  const base=Number(b.expected_generation),files=b.files as Record<string,string>;
  const id=await hash(JSON.stringify([host,base,Object.entries(files).sort(([a],[b])=>a.localeCompare(b))]));
  const sealed=await this.seal(files,account.provider,base+1);
  if(!this.db.one('SELECT id FROM approved_hosts WHERE id=?',host))return error('host_not_approved',403);
  this.db.run('INSERT OR IGNORE INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES(?,?,?,?,?,?)',id,host,base,sealed,Date.now(),'pending');
  const result=await this.publishRotation(files,account.provider,base,host,typeof b.lease==='string'?b.lease:undefined,id);
  if(!result.ok){const code=(await result.json() as {error:string}).error;if(code==='rotation_not_newer')this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','rejected',id);return json({error:code,candidate_id:id},result.status);}return result;
 }
 private async publishRotation(files:Record<string,string>,provider:string,base:number,host?:string,lease?:string,candidate?:string):Promise<Response>{
  const prior=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1');if(!prior)return error('credentials_unavailable',503);
  const old=await this.unseal(prior.sealed,provider,prior.generation) as Record<string,string>;
  const ordered=(value:Record<string,string>)=>JSON.stringify(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)));
  if(ordered(old)===ordered(files))return json({generation:prior.generation,unchanged:true});
  const newer=this.freshness(provider,files),older=this.freshness(provider,old);
  if(['codex','claude'].includes(provider)&&(newer===null||older!==null&&newer<=older))return error('rotation_not_newer',409);
  const sealed=await this.seal(files,provider,base+1);
  const result=this.db.transaction(this.ctx,()=>{
   if(host&&!this.db.one('SELECT id FROM approved_hosts WHERE id=?',host))return error('host_not_approved',403);
   const row=this.db.one<{generation:number;refresh_until:number;refresh_owner:string|null;refresh_lease:string|null}>('SELECT * FROM credentials WHERE id=1');
   if(!row||row.generation!==base)return error('generation_conflict',409);
   if(host&&provider!=='zai'&&(row.refresh_owner||this.hasUnresolvedRefresh(base))&&(row.refresh_owner!==host||row.refresh_lease!==lease))return error('refresh_permit_mismatch',409);
   if(!host&&row.refresh_until>Date.now())return error('refresh_in_progress',409);
   this.db.run('UPDATE credentials SET generation=?,sealed=?,refresh_until=0,refresh_owner=NULL,refresh_lease=NULL WHERE id=1',base+1,sealed);if(row.refresh_lease)this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','published',row.refresh_lease);
   this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');if(candidate)this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','published',candidate);return json({generation:base+1});
  });return result;
 }
 private credentialGeneration():number|null{
  try{return this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1')?.generation??null;}catch{return null;}
 }
 private renewalApplicable():boolean{
  return this.db.one<Account>('SELECT * FROM account WHERE id=1')?.provider==='claude'&&this.credentialGeneration()!==null;
 }
 private renewalRow():Renewal|undefined{
  return this.db.one<Renewal>('SELECT generation,access_expires_at,next_at,attempts,status,reason,updated_at FROM credential_renewal WHERE id=1');
 }
 private async initializeRenewal():Promise<void>{
  this.db.run(RENEWAL_TABLE);
  // Existing and imported credentials arm on first construction (any fetch or
  // a restored alarm), never by exporting or re-importing material.
  try{if(this.renewalApplicable())await this.schedule();}catch{}
 }
 private async rearmRenewal():Promise<void>{
  try{if(this.renewalApplicable())await this.schedule();}catch{}
 }
 private renewalTarget(expires:number|null,now:number):number{
  if(expires===null)return now;
  return Math.min(now+RENEWAL_HORIZON_MS,Math.max(now,expires-RENEWAL_LEAD_MS));
 }
 private renewalCode(e:unknown):string{
  const code=e instanceof HttpError?e.code:e instanceof Error?e.message:'';
  return /^[a-z_]{1,80}$/.test(code)?code:'renewal_failed';
 }
 /** Next renewal wake for the current Claude generation; null only when there is authoritatively no work, 'superseded' when a newer generation appeared mid-read, 'unknown' on an unexpected failure. Never throws. */
 private async renewalDue():Promise<number|null|'superseded'|'unknown'>{
  try{
   const account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
   const cred=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1');
   if(account?.provider!=='claude'||!cred)return null;
   let row=this.renewalRow();
   if(!row||row.generation!==cred.generation){
    const now=Date.now();let expires:number|null=null,status='scheduled',next:number|null=now,reason:string|null=null;
    try{
     const files=await this.unseal(cred.sealed,'claude',cred.generation) as Record<string,string>;
     const auth=JSON.parse(files[PATHS.claude[0]]);expires=this.accessExpiry('claude',files);
     if(typeof auth?.claudeAiOauth?.refreshToken!=='string'||!auth.claudeAiOauth.refreshToken){status='no_refresh_token';next=null;}
     else next=this.renewalTarget(expires,now);
    }catch(e){reason=this.renewalCode(e);} // Due now: the attempt records a bounded retry.
    // Write only if no newer generation or concurrent row appeared while unsealing.
    if(this.credentialGeneration()!==cred.generation)return 'superseded';
    const latest=this.renewalRow();
    if(!latest||latest.generation!==cred.generation)
     this.db.run('INSERT INTO credential_renewal(id,generation,access_expires_at,next_at,attempts,status,reason,updated_at) VALUES(1,?,?,?,0,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,access_expires_at=excluded.access_expires_at,next_at=excluded.next_at,attempts=0,status=excluded.status,reason=excluded.reason,updated_at=excluded.updated_at',cred.generation,expires,next,status,reason,now);
    row=this.renewalRow();
   }else if(row.status==='uncertain'&&!this.hasUnresolvedRefresh(row.generation)){
    // The exact lease owner proved no refresh happened; resume bounded retries.
    const now=Date.now();
    this.db.run("UPDATE credential_renewal SET status='retrying',next_at=?,updated_at=? WHERE id=1 AND generation=?",now,now,row.generation);row=this.renewalRow();
   }
   if(this.credentialGeneration()!==cred.generation)return 'superseded';
   return row&&RENEWAL_ACTIVE.includes(row.status)&&row.next_at!==null?row.next_at:null;
  }catch{return 'unknown';}
 }
 /** Alarm work: renew the current Claude generation through the central lease/CAS path. */
 private async renewIfDue():Promise<void>{
  let generation:number;
  try{
   const row=this.renewalRow(),cred=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1');
   if(this.db.one<Account>('SELECT * FROM account WHERE id=1')?.provider!=='claude'||!cred||!row||row.generation!==cred.generation
    ||!RENEWAL_ACTIVE.includes(row.status)||row.next_at===null||row.next_at>Date.now())return;
   generation=cred.generation;
  }catch{return;}
  try{
   const cred=this.db.one<{sealed:string}>('SELECT sealed FROM credentials WHERE id=1 AND generation=?',generation);if(!cred)return;
   const files=await this.unseal(cred.sealed,'claude',generation) as Record<string,string>;
   if(!this.needsRefresh('claude',files,RENEWAL_LEAD_MS+1000)){
    const auth=JSON.parse(files[PATHS.claude[0]]),expires=this.accessExpiry('claude',files),now=Date.now();
    const usable=typeof auth?.claudeAiOauth?.refreshToken==='string'&&!!auth.claudeAiOauth.refreshToken;
    this.db.run('UPDATE credential_renewal SET access_expires_at=?,next_at=?,status=?,updated_at=? WHERE id=1 AND generation=?',expires,usable?this.renewalTarget(expires,now):null,usable?'scheduled':'no_refresh_token',now,generation);
    return;
   }
   await this.refresh('claude',files,generation);
   this.db.run("UPDATE credential_renewal SET status='renewed',next_at=NULL,reason=NULL,updated_at=? WHERE id=1 AND generation=?",Date.now(),generation);
  }catch(e){try{this.renewalFailed(generation,e);}catch{}}
 }
 private renewalFailed(generation:number,e:unknown):void{
  const code=this.renewalCode(e),now=Date.now();
  const current=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');
  const row=this.renewalRow();
  if(current?.generation!==generation||row?.generation!==generation)return; // A newer generation re-arms itself.
  let status:string,next:number|null=null;
  if(/^provider_refresh_(invalid_grant|invalid_client|access_denied|invalid_request)$/.test(code))status='refused';
  else if(code==='refresh_in_progress'&&current.refresh_until>now&&row.attempts+1<RENEWAL_MAX_ATTEMPTS){
   // A live host or central lease owns this generation: wait for it, bounded.
   status='retrying';next=Math.max(now+Math.min(RENEWAL_RETRY_CAP_MS,RENEWAL_RETRY_BASE_MS*2**row.attempts),current.refresh_until+1000);
  }
  else if(this.hasUnresolvedRefresh(generation))status='uncertain'; // Never replay a possibly-rotated refresh token.
  else if(row.attempts+1>=RENEWAL_MAX_ATTEMPTS)status='exhausted';
  else{status='retrying';next=now+Math.min(RENEWAL_RETRY_CAP_MS,RENEWAL_RETRY_BASE_MS*2**row.attempts);}
  this.db.run('UPDATE credential_renewal SET attempts=attempts+1,status=?,next_at=?,reason=?,updated_at=? WHERE id=1 AND generation=?',status,next,code,now,generation);
  if(status!=='retrying')this.recordCredentialAlert('credential_renewal_'+status,generation);
 }
 private renewalView(a:Actor|undefined):Response{
  if(!hasScope(a,'quota:read')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
  const provider=this.db.one<Account>('SELECT * FROM account WHERE id=1')?.provider??null,row=this.renewalRow();
  // Secret-free by construction: the row never holds token material.
  return json({provider,renewal:row&&provider==='claude'?{generation:row.generation,status:row.status,attempts:row.attempts,next_at:row.next_at,access_expires_at:row.access_expires_at,reason:row.reason,updated_at:row.updated_at}:null});
 }
 private hasUnresolvedRefresh(generation:number):boolean{
  return ['issued','pending'].some(state=>!!this.db.one('SELECT id FROM credential_candidates WHERE base_generation=? AND state=? ORDER BY id LIMIT 1',generation,state));
 }
 private validFiles(provider:string,value:unknown):value is Record<string,string>{
  if(!value||typeof value!=='object'||Array.isArray(value))return false;const entries=Object.entries(value);
  if(!entries.length||entries.length>8||!entries.every(([p,v])=>(PATHS[provider]||[]).includes(p)&&typeof v==='string'&&v.length>0&&v.length<=16000&&!v.includes('\0')))return false;
  try{for(const [p,v] of entries)if(p.endsWith('.json')){const parsed=JSON.parse(String(v));if(!parsed||typeof parsed!=='object'||Array.isArray(parsed)&&provider!=='go')return false;}
   if(provider==='go'){const parsed=JSON.parse(String((value as Record<string,string>)[PATHS[provider][0]]));const active=Array.isArray(parsed)?parsed.filter(v=>v&&v.active===true):[];return active.length===1&&typeof active[0]?.value?.access==='string'&&!!active[0].value.access;}
   if(provider==='zai'){const config=JSON.parse(String((value as Record<string,string>)[PATHS[provider][0]]));return typeof config.zai_token==='string'&&config.zai_token.length>0&&config.zai_token.length<=4096&&!/^enc:/i.test(config.zai_token)&&!(/\s/.test(config.zai_token))&&(config.zai_base_url===undefined||config.zai_base_url==='https://api.z.ai/api/anthropic');}
   if(provider==='gemini')return typeof JSON.parse(String((value as Record<string,string>)[PATHS[provider][0]]))?.access_token==='string';
   if(provider==='codex'){const auth=JSON.parse(String((value as Record<string,string>)[PATHS[provider][0]]));return typeof auth?.tokens?.access_token==='string'&&typeof auth?.tokens?.refresh_token==='string';}
   if(provider==='claude')return typeof JSON.parse(String((value as Record<string,string>)[PATHS[provider][0]]))?.claudeAiOauth?.accessToken==='string';return true;
  }catch{return false;}
 }
 private freshness(provider:string,files:Record<string,string>):number|null{
  try{const auth=JSON.parse(files[PATHS[provider][0]]);const t=provider==='codex'?Date.parse(auth.last_refresh):provider==='claude'?Number(auth?.claudeAiOauth?.expiresAt):NaN;return Number.isFinite(t)?t:null;}catch{return null;}
 }

}
function percent(v:unknown):v is number{return typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=100;}

async function hash(value:string):Promise<string>{const bytes=new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)));return [...bytes].map(v=>v.toString(16).padStart(2,'0')).join('');}
function timestamp(v:unknown):number|null{const n=typeof v==='number'?(v<100000000000?v*1000:v):typeof v==='string'?Date.parse(v):NaN;return Number.isFinite(n)&&n>0?n:null;}
