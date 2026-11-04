import {budgetedBackgroundRequest,BudgetPause,budgetPauseResponse} from './budget-edge';
import {credentialKey,sealCredential,openCredential} from './credential-crypto';
import { MeteredDb, initializeSchema } from './db';
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

 `CREATE TABLE account(id INTEGER PRIMARY KEY CHECK(id=1),provider TEXT NOT NULL,enabled INTEGER NOT NULL,capacity INTEGER NOT NULL,occupied INTEGER NOT NULL DEFAULT 0,min_remaining REAL NOT NULL DEFAULT 0)`,
 `CREATE TABLE quota(id INTEGER PRIMARY KEY CHECK(id=1),remaining REAL NOT NULL,captured_at INTEGER NOT NULL,valid_until INTEGER NOT NULL,reset_at INTEGER,source TEXT NOT NULL)`,
 `CREATE TABLE reservations(operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,machine_id TEXT NOT NULL,epoch INTEGER NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,expires_at INTEGER NOT NULL,released_at INTEGER) WITHOUT ROWID`,
 'CREATE INDEX reservation_due ON reservations(state,expires_at,operation_id)',
 `CREATE TABLE reset_grants(id TEXT PRIMARY KEY,expires_at INTEGER NOT NULL,available INTEGER NOT NULL,source TEXT NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID`,
 'CREATE INDEX reset_expiry ON reset_grants(available,expires_at,id)',
 'CREATE INDEX reset_page ON reset_grants(available,id)',
];
type Account={provider:string;enabled:number;capacity:number;occupied:number;min_remaining:number};
type Quota={remaining:number;captured_at:number;valid_until:number;reset_at:number|null;source:string};
type Reservation={operation_id:string;session_id:string;machine_id:string;epoch:number;digest:string;state:string;expires_at:number};
/** One provider account owns its capacity. Expiry never frees confirmed native work. */
export class AccountDO {
 private collecting?:Promise<Response>;
 private readonly db:MeteredDb;
 constructor(private readonly ctx:DurableObjectState,private readonly env:AccountEnv={}){this.db=new MeteredDb(ctx.storage.sql);initializeSchema(ctx,this.db,1,SCHEMA);}
 async fetch(request:Request):Promise<Response>{
  try{
   const p=new URL(request.url).pathname,a=actorFrom(request),method=request.method;
   if(p==='/api/account/rotation'||p==='/api/account/refresh-lease'||p==='/api/account/credential-rejection'||p==='/api/account/reconcile'||p==='/api/account/refresh-outcome')return await this.rotationRoute(request,a,p);
   if(p==='/api/account/credentials')return await this.credentials(request,a);
   if(p==='/api/account/hosts')return await this.hosts(request,a);
   if(p==='/api/account/discovery')return await this.discovery(request,a);
   if(p==='/api/account/alerts'&&method==='GET')return this.alerts(request,a);
   if(p==='/_internal/routing'&&method==='GET'){await this.lazyQuota();return this.routingView();}
   if(p==='/_internal/release'&&method==='POST')return await this.transition(request,'released');
   if(p==='/api/account/release')return error('not_found',404);
   if(p==='/_metrics')return json({counts:this.db.snapshot()});
   if(p==='/api/account'&&method==='PATCH')return await this.configure(request,a);
   if(p==='/api/account/quota'&&method==='PUT')return await this.quota(request,a);
   if(p==='/api/account/resets')return await this.resets(request,a);
   if((p==='/api/account'||p==='/api/account/quota')&&method==='GET'){if(this.canReadQuota(a))await this.lazyQuota();return this.view(a);}
   if(!hasScope(a,'account:reserve')&&!hasScope(a,'operator:admin'))return error('forbidden',403);
   if(method!=='POST')return error('not_found',404);
   if(p==='/api/account/reserve')return await this.reserve(request);
   if(p==='/api/account/confirm')return await this.transition(request,'confirmed');
   if(p==='/api/account/start')return await this.transition(request,'running');
   
   return error('not_found',404);
  }catch(e){if(e instanceof BudgetPause)return budgetPauseResponse(e.retryAt);return e instanceof HttpError?error(e.code,e.status):error('internal_error',500);}
 }
 private routingView():Response{
  const account=this.db.one<Account>('SELECT * FROM account WHERE id=1'),quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
  const credential=this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1');
  const approved=this.db.all<{id:string}>('SELECT id FROM approved_hosts ORDER BY id LIMIT 51');
  if(approved.length>50)return error('host_observation_limit',503);
  const rejected=approved.length?this.db.all<{host_id:string}>('SELECT host_id FROM credential_rejections WHERE generation=? AND host_id IN ('+approved.map(()=>'?').join(',')+')',credential?.generation??-1,...approved.map(v=>v.id)):[];
  const hosts=approved.filter(v=>!rejected.some(r=>r.host_id===v.id));
  if(hosts.length>50)return error('host_observation_limit',503);
  return json({account:account||null,quota:quota||null,fresh:!!quota&&quota.valid_until>Date.now(),hosts:hosts.map(v=>v.id),credential_generation:credential?.generation??null});
 }
 private async configure(r:Request,a:Actor|undefined):Promise<Response>{
  if(!hasScope(a,'operator:admin'))return error('forbidden',403);
  const b=await bodyObject(r,4096);
  if(!boundedString(b.provider,80)||!Number.isInteger(b.capacity)||Number(b.capacity)<1||Number(b.capacity)>1000||typeof b.enabled!=='boolean'||!percent(b.min_remaining??0))return error('invalid_account',400);
  if(['shared-zai','zcodex','zai-central'].includes(String(b.provider))||b.provider==='zai'&&(this.ctx.id?.name??r.headers.get('x-hub2-account-id'))!=='zai-central')return error('zai_central_account_required',400);
  const prior=this.db.one<{provider:string}>('SELECT provider FROM account WHERE id=1');if(prior&&prior.provider!==b.provider&&this.db.one('SELECT generation FROM credentials WHERE id=1'))return error('provider_change_requires_new_account',409);
  this.db.run(`INSERT INTO account(id,provider,enabled,capacity,min_remaining) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,enabled=excluded.enabled,capacity=excluded.capacity,min_remaining=excluded.min_remaining`,b.provider,Number(b.enabled),b.capacity,b.min_remaining??0);
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
 private admissionHost(machine:string):void{
  const credential=this.db.one<{generation:number}>('SELECT generation FROM credentials WHERE id=1');
  if(!credential)return;
  if(!this.db.one('SELECT id FROM approved_hosts WHERE id=?',machine))throw new HttpError('host_not_approved',409);
  if(this.db.one('SELECT host_id FROM credential_rejections WHERE host_id=? AND generation=?',machine,credential.generation))throw new HttpError('credential_generation_rejected',409);
 }
 private async reserve(r:Request):Promise<Response>{
  const b=await bodyObject(r,4096),ttl=Number(b.ttl_ms??60000);
  if(!boundedString(b.operation_id,120)||!boundedString(b.session_id,120)||!boundedString(b.machine_id,120)||!boundedString(b.request_digest,128)||!Number.isSafeInteger(b.epoch)||Number(b.epoch)<1||!Number.isSafeInteger(ttl)||ttl<1000||ttl>600000)return error('invalid_reservation',400);
  const reservation=this.db.transaction(this.ctx,()=>{
   this.admissionHost(String(b.machine_id));
   this.expire(Date.now());
   const prior=this.db.one<Reservation>('SELECT * FROM reservations WHERE operation_id=?',b.operation_id);
   if(prior){if(prior.session_id!==b.session_id||prior.machine_id!==b.machine_id||prior.digest!==b.request_digest)throw new HttpError('idempotency_conflict',409);if(prior.epoch!==b.epoch)throw new HttpError('stale_epoch',409);return prior;}
   const a=this.db.one<Account>('SELECT * FROM account WHERE id=1'),q=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
   if(!a||!a.enabled)throw new HttpError('account_disabled',409);
   if(!q||q.valid_until<=Date.now())throw new HttpError('quota_evidence_unavailable',503);
   if(q.remaining<=a.min_remaining)throw new HttpError('quota_reserve',409);
   if(a.occupied>=a.capacity)throw new HttpError('account_capacity',409);
   const expires=Date.now()+ttl;
   this.db.run('INSERT INTO reservations(operation_id,session_id,machine_id,epoch,digest,state,expires_at) VALUES(?,?,?,?,?,?,?)',b.operation_id,b.session_id,b.machine_id,b.epoch,b.request_digest,'offered',expires);
   this.db.run('UPDATE account SET occupied=occupied+1 WHERE id=1');
   return this.db.one<Reservation>('SELECT * FROM reservations WHERE operation_id=?',b.operation_id)!;
  });
  await this.schedule();return json({reservation});
 }
 private async transition(r:Request,target:'confirmed'|'running'|'released'):Promise<Response>{
  const b=await bodyObject(r,4096);
  if(!boundedString(b.operation_id,120)||!Number.isSafeInteger(b.epoch))return error('invalid_reservation',400);
  if(target==='released'&&(!boundedString(b.session_id,120)||!boundedString(b.machine_id,120)))return error('release_binding_required',400);
  const reservation=this.db.transaction(this.ctx,()=>{
   const row=this.db.one<Reservation>('SELECT * FROM reservations WHERE operation_id=?',b.operation_id);
   if(!row)throw new HttpError('reservation_not_found',404);
   if(row.epoch!==b.epoch)throw new HttpError('stale_epoch',409);
   if(target==='released'&&(row.session_id!==b.session_id||row.machine_id!==b.machine_id))throw new HttpError('release_binding_mismatch',409);
   if(target!=='released')this.admissionHost(row.machine_id);
   if(row.state===target){if(target==='running'&&(row.session_id!==b.session_id||row.machine_id!==b.machine_id))throw new HttpError('admission_binding_mismatch',409);return row;}
   if(row.state==='released')throw new HttpError('reservation_terminal',409);
   if(target==='confirmed'){
    if(row.state!=='offered'||row.expires_at<=Date.now())throw new HttpError('offer_expired',409);
    this.db.run('UPDATE reservations SET state=? WHERE operation_id=?','confirmed',row.operation_id);
   }else if(target==='running'){
    if(row.session_id!==b.session_id||row.machine_id!==b.machine_id)throw new HttpError('admission_binding_mismatch',409);
    if(!['confirmed','awaiting-unclaimed-proof'].includes(row.state))throw new HttpError('reservation_not_confirmed',409);
    const quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1'),account=this.db.one<Account>('SELECT * FROM account WHERE id=1');
    if(!quota||quota.valid_until<=Date.now()||!account?.enabled)throw new HttpError('quota_evidence_unavailable',503);
    if(quota.remaining<=account.min_remaining)throw new HttpError('quota_reserve',409);
    this.db.run('UPDATE reservations SET state=?,expires_at=? WHERE operation_id=?','running',Date.now()+120000,row.operation_id);
   }else{
    const proof=b.unclaimed_proof as Record<string,unknown>|undefined;
    const unclaimed=['confirmed','awaiting-unclaimed-proof'].includes(row.state)&&proof&&['offer-expired-unclaimed','offer-never-activated'].includes(String(proof.kind))&&proof.operation_id===row.operation_id&&proof.session_id===row.session_id&&proof.machine_id===row.machine_id&&proof.epoch===row.epoch&&Number.isSafeInteger(proof.observed_at)&&Number(proof.observed_at)<=Date.now()+5000&&(proof.kind==='offer-never-activated'||Number.isSafeInteger(proof.offer_expires_at)&&Number(proof.observed_at)>=Number(proof.offer_expires_at));
    if(row.state!=='offered'&&b.native_exit_confirmed!==true&&!unclaimed)throw new HttpError('native_exit_or_unclaimed_proof_required',409);
    this.db.run('UPDATE reservations SET state=?,released_at=? WHERE operation_id=?','released',Date.now(),row.operation_id);
    this.db.run('UPDATE account SET occupied=occupied-1 WHERE id=1 AND occupied>0');
   }
   return {...row,state:target};
  });await this.schedule();return json({reservation});
 }
 private expire(now:number):void{
  for(const state of ['offered','confirmed','running']){
   const rows=this.db.all<Reservation>('SELECT * FROM reservations WHERE state=? AND expires_at<=? ORDER BY expires_at,operation_id LIMIT 32',state,now);
   for(const row of rows){this.db.run('UPDATE reservations SET state=? WHERE operation_id=?',state==='offered'?'released':state==='confirmed'?'awaiting-unclaimed-proof':'uncertain',row.operation_id);if(state==='offered')this.db.run('UPDATE account SET occupied=occupied-1 WHERE id=1 AND occupied>0');}
  }
 }
 private async schedule():Promise<void>{
  const due=['offered','confirmed','running'].map(s=>this.db.one<{expires_at:number}>('SELECT expires_at FROM reservations WHERE state=? ORDER BY expires_at,operation_id LIMIT 1',s)?.expires_at).filter((v):v is number=>v!==undefined);
  this.db.alarmOperation();const discovery=this.db.one<{next_at:number}>('SELECT next_at FROM discovery WHERE id=1 AND enabled=1');if(discovery)due.push(discovery.next_at);
  const grants=this.db.all<{id:string;expires_at:number}>('SELECT id,expires_at FROM reset_grants WHERE available=1 ORDER BY expires_at,id LIMIT 33');
  if(grants.length>32)throw new HttpError('reset_observation_limit',503);
  for(const g of grants){const key='reset:'+g.id+':'+g.expires_at+':24h';if(g.expires_at>Date.now()&&!this.db.one('SELECT id FROM alerts WHERE id=?',key))due.push(Math.max(Date.now()+100,g.expires_at-86400000));}
  if(due.length)await this.ctx.storage.setAlarm(Math.max(Date.now()+100,Math.min(...due)));else await this.ctx.storage.deleteAlarm();
 }
 async alarm():Promise<void>{
  this.db.transaction(this.ctx,()=>{this.expire(Date.now());this.resetAlerts();});
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
   if(!repairLease&&this.db.one('SELECT host_id FROM credential_rejections WHERE host_id=? AND generation=?',machine,row.generation))return error('credential_generation_rejected',409);
   const minimum=Number(query.get('min_validity_ms')||0);if(!Number.isSafeInteger(minimum)||minimum<0||minimum>10800000)return error('invalid_access_validity',400);
   let files=await this.unseal(row.sealed,account.provider,row.generation) as Record<string,string>;
   if(!repairLease&&account&&['codex','claude'].includes(account.provider)&&this.needsRefresh(account.provider,files,minimum)){
    await this.refresh(account.provider,files,row.generation);row=this.db.one<{generation:number;sealed:string}>('SELECT generation,sealed FROM credentials WHERE id=1')!;files=await this.unseal(row.sealed,account.provider,row.generation) as Record<string,string>;
   }
   if(!repairLease&&minimum>0){
    const source='provider:'+account.provider+':generation:'+row.generation;
    let quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
    if(!quota||quota.valid_until<=Date.now()||quota.source!==source){
     const collected=await this.collect(false);if(collected.status===429)return collected;
     quota=this.db.one<Quota>('SELECT * FROM quota WHERE id=1');
    }
    if(!quota||quota.valid_until<=Date.now()||quota.source!==source)return error('quota_unavailable',503);
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
  const result=await budgetedBackgroundRequest(this.env as Hub2Env,'provider_refresh',()=>this.collectProvider());
  if(result.status===429||result.status===503){
   const body=await result.clone().json().catch(()=>({})) as {error?:string;retry_at?:number};
   if(body.error?.startsWith('budget_')){
    const retry=Number.isSafeInteger(body.retry_at)?body.retry_at!:Date.now()+300000;
    this.db.run('INSERT INTO probes(id,token,retry_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET retry_at=excluded.retry_at',crypto.randomUUID(),retry);
    this.db.run('UPDATE discovery SET next_at=? WHERE id=1 AND enabled=1',retry);
   }
  }
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
  }catch(e){if(e instanceof BudgetPause){fail('budget_paused',!quotaProved);return budgetPauseResponse(e.retryAt);}return fail('provider_probe_failed',!quotaProved);}
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
  let value:any;
  const decision=await budgetedBackgroundRequest(this.env as Hub2Env,'provider_refresh',async()=>{
   value=await this.refreshProvider(provider,files,generation);
   return new Response(null,{status:204});
  });
  if(decision.status!==204){
   const denied=await decision.json().catch(()=>({})) as {error?:string;retry_at?:number};
   if(decision.status===429)throw new BudgetPause(Number(denied.retry_at)||Date.now()+86400000);
   throw new HttpError(denied.error||'budget_effect_outcome_uncertain',decision.status);
  }
  return value;
 }
 private async refreshProvider(provider:string,files:Record<string,string>,generation:number):Promise<any>{
  const until=Date.now()+30000,attempt=crypto.randomUUID();
  this.db.transaction(this.ctx,()=>{const row=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');if(!row||row.generation!==generation||row.refresh_until>Date.now())throw new HttpError('refresh_in_progress',409);if(this.hasUnresolvedRefresh(generation))throw new HttpError('refresh_outcome_uncertain',503);this.db.run('UPDATE credentials SET refresh_until=?,refresh_owner=?,refresh_lease=NULL WHERE id=1',until,'central');});
  this.db.run('INSERT INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES(?,?,?,?,?,?)',attempt,'hub:central',generation,'',Date.now(),'issued');
  try{
   const path=PATHS[provider][0],auth=JSON.parse(files[path]),refresh=provider==='codex'?auth?.tokens?.refresh_token:auth?.claudeAiOauth?.refreshToken;
   if(typeof refresh!=='string')throw new Error('refresh_missing');
   const response=await this.provider(provider==='codex'?'https://auth.openai.com/oauth/token':'https://platform.claude.com/v1/oauth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:refresh,client_id:provider==='codex'?'app_EMoamEEZ73f0CkXaXp7hrann':'9d1c250a-e61b-44d9-88ed-5944d1962f5e'})});
   if(!response.ok){if([400,401,403].includes(response.status)){try{const failure=await this.boundedJson(response);if(['invalid_grant','invalid_client','access_denied','invalid_request'].includes(String(failure.error)))this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','refused',attempt);}catch{}}throw new Error('refresh_refused');}const b=await this.boundedJson(response);if(typeof b.access_token!=='string')throw new Error('refresh_invalid');
   if(provider==='codex'){auth.tokens.access_token=b.access_token;if(typeof b.refresh_token==='string')auth.tokens.refresh_token=b.refresh_token;if(typeof b.id_token==='string')auth.tokens.id_token=b.id_token;auth.last_refresh=new Date().toISOString();}
   else{auth.claudeAiOauth.accessToken=b.access_token;if(typeof b.refresh_token==='string')auth.claudeAiOauth.refreshToken=b.refresh_token;auth.claudeAiOauth.expiresAt=Date.now()+Number(b.expires_in||3600)*1000;}
   files[path]=JSON.stringify(auth);const sealed=await this.seal(files,provider,generation+1);
   this.db.run('UPDATE credential_candidates SET sealed=?,state=? WHERE id=?',sealed,'pending',attempt);
   this.db.transaction(this.ctx,()=>{const row=this.db.one<{generation:number;refresh_until:number}>('SELECT generation,refresh_until FROM credentials WHERE id=1');if(row?.generation!==generation||row.refresh_until!==until)throw new Error('refresh_superseded');this.db.run('UPDATE credentials SET sealed=?,generation=generation+1,refresh_until=0,refresh_owner=NULL,refresh_lease=NULL WHERE id=1',sealed);this.db.run('UPDATE quota SET valid_until=0 WHERE id=1');this.db.run('UPDATE credential_candidates SET state=? WHERE id=?','published',attempt);});return auth;
  }catch(e){
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
 private provider(url:string,init:RequestInit):Promise<Response>{return (this.env.HUB2_PROVIDER_FETCH||fetch)(url,{...init,redirect:'manual',signal:AbortSignal.timeout(15000)});}
 private async boundedJson(response:Response):Promise<any>{
  const reader=response.body?.getReader();if(!reader)throw new Error('empty_response');let size=0;const chunks:Uint8Array[]=[];
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>131072){await reader.cancel();throw new Error('oversized_response');}chunks.push(value);}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}return JSON.parse(new TextDecoder().decode(bytes));
 }
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
