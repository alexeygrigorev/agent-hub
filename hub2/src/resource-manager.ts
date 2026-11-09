import { HttpError } from './http';
import type { Hub2Env } from './types';

/**
 * Resource Manager admission facade.
 *
 * One coherent component inside the Hub that owns availability and freshness
 * classification, typed holds, and account/model/machine selection policy for
 * admitted launches. It is pure policy over snapshots persisted by the
 * existing owners: provider quota/reset/credential health lives in AccountDO,
 * host engines/capabilities/resources live in MachineDO. It never reserves,
 * releases, writes quota evidence, or consults the monetary guard directly —
 * AccountDO/MachineDO owners stay authoritative for reserve/release, the
 * dispatch saga stays the single launch-intent owner. Quota is one resource
 * input here, never a separate authority; unknown or stale quota becomes a
 * typed hold with a bounded refresh, never fake zero and never a local probe.
 */

export type AdmissionRequest={operation_id:string;machine_id?:string;engine?:string;model?:string;effort?:string;override_reason?:string;interactive:boolean;with_goal?:boolean;required_capabilities?:string[]};
type RoutingCandidate={id:string;engine:string;model:string;effort?:string;account_id:string;weight:number;capabilities:string[];enabled:boolean};
/** MachineDO /_internal/routing wire contract: resources is the bounded
 * heartbeat snapshot {mem_available_mb,observed_at} exactly as persisted, or
 * absent on hosts that have not reported one yet. */
type MachineView={machine_id:string;available:boolean;capacity:number;occupied:number;engines:string[];capabilities:string[];resources?:{mem_available_mb:number;observed_at:number;cpu_load_pct?:number}};
type AccountView={account:{enabled:number|boolean;capacity:number;occupied:number;min_remaining:number}|null;quota:{remaining:number;valid_until:number;captured_at?:number;reset_at?:number;source:string}|null;fresh:boolean;hosts:string[]};
export type AdmissionPlan={machine_id:string;engine:string;model:string;effort?:string;account_id:string;policy_id:string;policy_version:unknown;override_reason:string|null;weight:number;quota_remaining:number;required_capabilities:string[]};
export type HoldKind='quota_unknown'|'quota_stale'|'quota_exhausted'|'account_saturated'|'machine_unavailable'|'host_resources_unknown'|'host_resources_stale'|'host_resources_saturated'|'capability_unavailable'|'model_policy_unavailable'|'routing_policy_unavailable';
export type AdmissionOutcome={ok:true;plan:AdmissionPlan}|{ok:false;code:HoldKind;hold:{retry_at:number|null;reason?:string}};
type PolicyDocument={version:unknown;candidates:RoutingCandidate[]};
const internalActor=JSON.stringify({id:'hub2-resource-manager',kind:'operator',scopes:['operator:admin','routing:read']});
/** Bounded hold backoff while a triggered refresh runs; never a busy loop. */
const REFRESH_BACKOFF_MS=60_000;
/** Host resource snapshot older than this is unverified for admission. */
const HOST_RESOURCE_TTL_MS=300_000;

/**
 * Host snapshot classification. Fail closed: a missing snapshot is unknown
 * (hosts must heartbeat resources before admission passes), a stale snapshot
 * is unverified, a non-positive reading is unusable, a malformed timestamp is
 * unknown, and a future-dated snapshot beyond small clock-skew tolerance is
 * unverified rather than trusted. A present-but-invalid CPU reading is
 * unknown; a fresh snapshot reporting CPU saturation refuses admission. Only
 * a fresh, positive, properly dated snapshot with bounded readings admits.
 */
const HOST_RESOURCE_FUTURE_TOLERANCE_MS=2_000;
const HOST_CPU_SATURATED_PCT=90;
function hostHold(resources:MachineView['resources'],now:number):{code:HoldKind;retry_at:number|null}|null{
  if(!resources)return{code:'host_resources_unknown',retry_at:now+REFRESH_BACKOFF_MS};
  if(!Number.isFinite(resources.mem_available_mb)||resources.mem_available_mb<=0)return{code:'host_resources_unknown',retry_at:now+REFRESH_BACKOFF_MS};
  if(!Number.isSafeInteger(resources.observed_at))return{code:'host_resources_unknown',retry_at:now+REFRESH_BACKOFF_MS};
  if(resources.observed_at>now+HOST_RESOURCE_FUTURE_TOLERANCE_MS||now-resources.observed_at>HOST_RESOURCE_TTL_MS)return{code:'host_resources_stale',retry_at:now+REFRESH_BACKOFF_MS};
  if(resources.cpu_load_pct!==undefined){
    if(!Number.isFinite(resources.cpu_load_pct)||resources.cpu_load_pct<0||resources.cpu_load_pct>100)return{code:'host_resources_unknown',retry_at:now+REFRESH_BACKOFF_MS};
    if(resources.cpu_load_pct>=HOST_CPU_SATURATED_PCT)return{code:'host_resources_saturated',retry_at:now+REFRESH_BACKOFF_MS};
  }
  return null;
}

async function read(namespace:DurableObjectNamespace,id:string,path:string){
  const response=await namespace.get(namespace.idFromName(id)).fetch('https://routing.internal'+path,{headers:{'x-hub2-actor':internalActor}});
  if(response.status===404&&path==='/_routing-policy')throw new HttpError('routing_policy_unconfigured',409);
  if(!response.ok)throw new HttpError('routing_evidence_unavailable',503);
  return await response.json() as any;
}

/**
 * Classify one account's quota evidence. Fresh, above-floor quota admits;
 * every other state is a typed hold with a bounded retry: unknown and stale
 * holds retry after the refresh backoff (the internal read already triggered
 * AccountDO's lazy refresh), exhausted holds retry at the known reset time,
 * and saturated accounts retry only when a running session releases.
 */
function quotaHold(account:Exclude<AccountView['account'],null>,quota:AccountView['quota'],fresh:boolean,now:number):{code:HoldKind;retry_at:number|null}|null{
  if(!quota)return{code:'quota_unknown',retry_at:now+REFRESH_BACKOFF_MS};
  if(!fresh||quota.valid_until<=now)return{code:'quota_stale',retry_at:now+REFRESH_BACKOFF_MS};
  if(!Number.isFinite(quota.remaining))return{code:'quota_unknown',retry_at:now+REFRESH_BACKOFF_MS};
  if(quota.remaining<=account.min_remaining)return{code:'quota_exhausted',retry_at:Number.isFinite(quota.reset_at)&&quota.reset_at&&quota.reset_at>now?quota.reset_at:now+REFRESH_BACKOFF_MS};
  if(account.occupied>=account.capacity)return{code:'account_saturated',retry_at:null};
  return null;
}

/** Resource Manager admission: typed outcome, authoritative owners unchanged. */
export async function admitLaunch(env:Hub2Env,input:AdmissionRequest):Promise<AdmissionOutcome>{
  if(input.with_goal&&!input.interactive)throw new HttpError('goal_requires_interactive',400);
  const override=!!(input.machine_id||input.engine||input.model||input.effort);
  if(override&&(!input.override_reason?.trim()||input.override_reason.length>500))throw new HttpError('override_reason_required',400);
  const required=[...new Set([input.interactive?'interactive':'headless',...(input.with_goal?['goal']:[]),...(input.required_capabilities||[])])];
  if(required.length>32||required.some(v=>typeof v!=='string'||!v||v.length>80))throw new HttpError('invalid_required_capabilities',400);
  const now=Date.now();
  const policy=await read(env.CONTROL,'global','/_routing-policy') as PolicyDocument;
  if(!Array.isArray(policy.candidates)||!policy.candidates.length)throw new HttpError('routing_policy_unconfigured',409);
  if(policy.candidates.length>32)throw new HttpError('routing_policy_bound_exceeded',503);
  const candidates=policy.candidates.filter(c=>c.enabled&&(!input.engine||input.engine===c.engine)&&(!input.model||input.model===c.model||input.model===c.id)&&(!input.effort||input.effort===c.effort));
  if(!candidates.length)throw new HttpError('model_policy_unavailable',409);
  const directory=await read(env.CONTROL,'global','/api/directory?kind=machines&limit=50');
  if(directory.next)throw new HttpError('routing_fleet_bound_exceeded',503);
  const ids=(directory.items as {id:string}[]).map(v=>v.id).filter(id=>!input.machine_id||input.machine_id===id);
  // A pinned machine missing from the credential directory was never paired or
  // was hard-revoked; say so instead of a bare machine_unavailable. An unpinned
  // start with an empty fleet stays bare: there is no single machine to name.
  if(!ids.length)return{ok:false,code:'machine_unavailable',hold:{retry_at:null,...(input.machine_id?{reason:'machine_not_enrolled'}:{})}};
  const machineResults=await Promise.all(ids.map(async id=>{try{return await read(env.MACHINES,id,'/_internal/routing') as MachineView;}catch{return null;}}));
  // Say why a host was skipped (busy/offline) instead of a bare machine_unavailable.
  const skipped=machineResults.filter((m):m is MachineView&{reason?:string}=>!!m&&!(m.available&&ids.includes(m.machine_id)));
  let machineReason:string|undefined=skipped.some(m=>m.reason==='machine_capacity')?'machine_capacity':skipped[0]?.reason||(machineResults.some(m=>!m)?'machine_unreadable':undefined);
  const machines=machineResults.filter((m):m is MachineView=>!!m&&m.available&&ids.includes(m.machine_id));
  const accountIds=[...new Set(candidates.map(c=>c.account_id))];
  const accounts=new Map<string,AccountView|null>(await Promise.all(accountIds.map(async id=>{try{return [id,await read(env.ACCOUNTS,id,'/_internal/routing') as AccountView] as const;}catch{return [id,null] as const;}})));
  const options:AdmissionPlan[]=[];
  let worst:{code:HoldKind;retry_at:number|null}|null=null;
  const rank=(code:HoldKind)=>['quota_unknown','quota_stale','quota_exhausted','account_saturated','host_resources_unknown','host_resources_stale','host_resources_saturated','capability_unavailable','machine_unavailable'].indexOf(code);
  const merge=(code:HoldKind,retry_at:number|null):{code:HoldKind;retry_at:number|null}=>!worst||rank(code)<rank(worst.code)?{code,retry_at}:worst;
  for(const c of candidates){
    const a=accounts.get(c.account_id);
    if(!a?.account?.enabled){worst=merge('quota_unknown',null);continue;}
    const hold=quotaHold(a.account,a.quota,a.fresh,now);
    if(hold){worst=merge(hold.code,hold.retry_at);continue;}
    if(!Number.isFinite(c.weight)||c.weight<=0)throw new HttpError('routing_policy_invalid',503);
    for(const m of machines){
      if(!a.hosts.includes(m.machine_id)){machineReason='account_host_not_approved';worst=merge('machine_unavailable',null);continue;}
      const resources=hostHold(m.resources,now);
      if(resources){worst=merge(resources.code,resources.retry_at);continue;}
      if(!m.engines.includes(c.engine)||required.some(v=>!m.capabilities.includes(v)&&!m.capabilities.includes(v+':'+c.engine))||(c.effort&&!m.capabilities.includes('effort')&&!m.capabilities.includes('effort:'+c.engine))||required.filter(v=>!['headless','interactive'].includes(v)).some(v=>!c.capabilities.includes(v))){
        worst=merge('capability_unavailable',null);continue;}
      if(!Number.isFinite(m.capacity)||m.capacity<=0||!Number.isFinite(m.occupied)||m.occupied<0||m.occupied>m.capacity){worst=merge('machine_unavailable',null);continue;}
      const weight=c.weight*((a.quota!.remaining-a.account.min_remaining)/100)*((a.account.capacity-a.account.occupied)/a.account.capacity)*((m.capacity-m.occupied)/m.capacity);
      if(!Number.isFinite(weight)||weight<=0)continue;
      options.push({machine_id:m.machine_id,engine:c.engine,model:c.model,...(c.effort?{effort:c.effort}:{}),account_id:c.account_id,policy_id:c.id,policy_version:policy.version,override_reason:override?input.override_reason!.trim():null,weight,quota_remaining:a.quota!.remaining,required_capabilities:required});
    }
  }
  if(!options.length){
    const reason=!worst||worst.code==='machine_unavailable'?machineReason:undefined;
    return{ok:false,code:worst?.code??'machine_unavailable',hold:{retry_at:worst?.retry_at??null,...(reason?{reason}:{})}};
  }
  options.sort((a,b)=>(a.policy_id+'\0'+a.machine_id).localeCompare(b.policy_id+'\0'+b.machine_id));
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(input.operation_id));
  let draw=new DataView(digest).getUint32(0)/4294967296*options.reduce((sum,v)=>sum+v.weight,0);
  for(const option of options){draw-=option.weight;if(draw<0)return{ok:true,plan:option};}
  return{ok:true,plan:options[options.length-1]};
}

/**
 * Reservation orchestration. The ResourceManager coordinates machine slot and
 * provider account capacity by calling the existing authoritative
 * MachineDO/AccountDO APIs with the same idempotent, epoch-fenced payloads as
 * before — it adds no durable state, DO, or authority of its own. ProjectDO
 * stays the durable saga intent and compensation journal: it journals the
 * returned step, schedules retries, and compensates partial holds through
 * releaseAccountCapacity. Uncertain native occupancy is never released here;
 * release requires the same qualified proof the account owner enforced before.
 */

export type DispatchIntent={
  state:'pending_machine'|'staged'|'account_confirmed';
  operation_id:string;attempt_operation_id:string;dispatch_id:string;attempt:number;
  session_id:string;machine_id:string;account_id:string|null;epoch:number|null;
  request_digest:string;project_id:string;intake_id:string;
  title:string;acceptance_criteria:unknown;launch:unknown;
};
export type AdvanceOutcome=
  |{ok:true;state:'staged';epoch:number}
  |{ok:true;state:'account_confirmed'|'offered'}
  |{ok:false;reason:string};

/** Wire-compatible refusal code for API paths that predate typed holds. */
export function holdWireCode(code:HoldKind):string{
  if(code==='quota_unknown'||code==='quota_stale'||code==='quota_exhausted'||code==='account_saturated')return'quota_unavailable';
  if(code==='host_resources_unknown'||code==='host_resources_stale'||code==='host_resources_saturated')return'machine_unavailable';
  return code;
}

const machineDispatchActor={'Content-Type':'application/json'} as const;

/** Advance one saga step through the authoritative capacity owners. */
export async function advanceDispatch(env:Hub2Env,saga:DispatchIntent):Promise<AdvanceOutcome>{
  const machine=env.MACHINES.get(env.MACHINES.idFromName(saga.machine_id));
  if(saga.state==='pending_machine'){
    const response=await machine.fetch('https://machine.hub2.internal/_internal/reserve',{
      method:'POST',headers:machineDispatchActor,
      body:JSON.stringify({
        operation_id:saga.attempt_operation_id,dispatch_id:saga.dispatch_id,attempt:saga.attempt,
        ...(saga.attempt>0?{retry_epoch:saga.epoch}:{}),
        session_id:saga.session_id,project_id:saga.project_id,
        intake_id:saga.intake_id,machine_id:saga.machine_id,request_digest:saga.request_digest,
        title:saga.title,acceptance_criteria:saga.acceptance_criteria,
        account_id:saga.account_id,launch:saga.launch,
      }),
    });
    const payload=await response.json() as {epoch?:number;state?:string;reason?:string;error?:string};
    const epoch=payload.epoch;
    if((response.status!==201&&response.status!==200)||typeof epoch!=='number'||!Number.isSafeInteger(epoch)||payload.state!=='staged')
      return{ok:false,reason:payload.reason||payload.error||'machine_unavailable'};
    return{ok:true,state:'staged',epoch};
  }
  if(saga.state==='staged'){
    if(saga.account_id){
      const account=env.ACCOUNTS.get(env.ACCOUNTS.idFromName(saga.account_id));
      const headers={'Content-Type':'application/json','x-hub2-actor':JSON.stringify({
        id:`hub2-dispatch:${saga.account_id}`,kind:'operator',resource_id:saga.account_id,
        generation:'dispatch',scopes:['operator:admin','account:reserve'],
      })};
      const reserved=await account.fetch('https://account.hub2.internal/api/account/reserve',{
        method:'POST',headers,
        body:JSON.stringify({operation_id:saga.attempt_operation_id,session_id:saga.session_id,machine_id:saga.machine_id,epoch:saga.epoch,
          request_digest:saga.request_digest,ttl_ms:600_000}),
      });
      if(!reserved.ok)return{ok:false,reason:'account_reservation_refused'};
      const confirmed=await account.fetch('https://account.hub2.internal/api/account/confirm',{
        method:'POST',headers,body:JSON.stringify({operation_id:saga.attempt_operation_id,epoch:saga.epoch}),
      });
      if(!confirmed.ok)return{ok:false,reason:'account_confirmation_refused'};
      const result=await confirmed.json() as {reservation?:{state?:string;epoch?:number}};
      if(result.reservation?.state!=='confirmed'||result.reservation.epoch!==saga.epoch)
        return{ok:false,reason:'account_confirmation_unverified'};
    }
    return{ok:true,state:'account_confirmed'};
  }
  const response=await machine.fetch('https://machine.hub2.internal/_internal/activate',{
    method:'POST',headers:machineDispatchActor,
    body:JSON.stringify({operation_id:saga.attempt_operation_id,session_id:saga.session_id,epoch:saga.epoch}),
  });
  const result=await response.json() as {state?:string;error?:string};
  if(!response.ok||!['offered','claimed','running'].includes(String(result.state)))
    return{ok:false,reason:result.error||'machine_activation_uncertain'};
  return{ok:true,state:'offered'};
}

export type CapacityTarget={account_id:string;attempt_operation_id:string;session_id:string;machine_id:string;epoch:number|null};
export type CapacityRelease={released:boolean};

/** Release the account reservation only against the owner's qualified proof. */
export async function releaseAccountCapacity(env:Hub2Env,target:CapacityTarget,proof:{native_exit_confirmed?:true;unclaimed_proof?:unknown}):Promise<CapacityRelease>{
  try{
    const account=env.ACCOUNTS.get(env.ACCOUNTS.idFromName(target.account_id));
    const result=await account.fetch('https://account.hub2.internal/_internal/release',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({operation_id:target.attempt_operation_id,session_id:target.session_id,
        machine_id:target.machine_id,epoch:target.epoch,...proof}),
    });
    if(!result.ok)return{released:false};
    const payload=await result.json() as {reservation?:{
      operation_id?:string;session_id?:string;machine_id?:string;epoch?:number;state?:string;
    }};
    const reservation=payload.reservation;
    return{released:reservation?.operation_id===target.attempt_operation_id&&reservation.session_id===target.session_id
      &&reservation.machine_id===target.machine_id&&reservation.epoch===target.epoch&&reservation.state==='released'};
  }catch{
    return{released:false};
  }
}
