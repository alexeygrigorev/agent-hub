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
 * dispatch saga stays the single launch-intent owner, and Cost Controller
 * gates fanout before this facade runs (budgetedBackgroundRequest in the
 * dispatch path). Quota is one resource input here, never a separate
 * authority; unknown or stale quota becomes a typed hold with a bounded
 * refresh, never fake zero and never a local probe.
 */

export type AdmissionRequest={operation_id:string;machine_id?:string;engine?:string;model?:string;effort?:string;override_reason?:string;interactive:boolean;with_goal?:boolean;required_capabilities?:string[]};
type RoutingCandidate={id:string;engine:string;model:string;effort?:string;account_id:string;weight:number;capabilities:string[];enabled:boolean};
type MachineView={machine_id:string;available:boolean;capacity:number;occupied:number;engines:string[];capabilities:string[];resources?:{fresh:boolean;observed_at:number}};
type AccountView={account:{enabled:number|boolean;capacity:number;occupied:number;min_remaining:number}|null;quota:{remaining:number;valid_until:number;captured_at?:number;reset_at?:number;source:string}|null;fresh:boolean;hosts:string[]};
export type AdmissionPlan={machine_id:string;engine:string;model:string;effort?:string;account_id:string;policy_id:string;policy_version:unknown;override_reason:string|null;weight:number;quota_remaining:number;required_capabilities:string[]};
export type HoldKind='quota_unknown'|'quota_stale'|'quota_exhausted'|'account_saturated'|'machine_unavailable'|'host_resources_stale'|'capability_unavailable'|'model_policy_unavailable'|'routing_policy_unavailable';
export type AdmissionOutcome={ok:true;plan:AdmissionPlan}|{ok:false;code:HoldKind;hold:{retry_at:number|null}};
type PolicyDocument={version:unknown;candidates:RoutingCandidate[]};
const internalActor=JSON.stringify({id:'hub2-resource-manager',kind:'operator',scopes:['operator:admin','routing:read']});
/** Bounded hold backoff while a triggered refresh runs; never a busy loop. */
const REFRESH_BACKOFF_MS=60_000;
/** Host resource snapshot older than this is unverified for admission. */
const HOST_RESOURCE_TTL_MS=120_000;

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
  if(!ids.length)return{ok:false,code:'machine_unavailable',hold:{retry_at:null}};
  const machineResults=await Promise.all(ids.map(async id=>{try{return await read(env.MACHINES,id,'/_internal/routing') as MachineView;}catch{return null;}}));
  const machines=machineResults.filter((m):m is MachineView=>!!m&&m.available&&ids.includes(m.machine_id));
  const accountIds=[...new Set(candidates.map(c=>c.account_id))];
  const accounts=new Map<string,AccountView|null>(await Promise.all(accountIds.map(async id=>{try{return [id,await read(env.ACCOUNTS,id,'/_internal/routing') as AccountView] as const;}catch{return [id,null] as const;}})));
  const options:AdmissionPlan[]=[];
  let worst:{code:HoldKind;retry_at:number|null}|null=null;
  const rank=(code:HoldKind)=>['quota_unknown','quota_stale','quota_exhausted','account_saturated','host_resources_stale','capability_unavailable','machine_unavailable'].indexOf(code);
  const merge=(code:HoldKind,retry_at:number|null):{code:HoldKind;retry_at:number|null}=>!worst||rank(code)<rank(worst.code)?{code,retry_at}:worst;
  for(const c of candidates){
    const a=accounts.get(c.account_id);
    if(!a?.account?.enabled){worst=merge('quota_unknown',null);continue;}
    const hold=quotaHold(a.account,a.quota,a.fresh,now);
    if(hold){worst=merge(hold.code,hold.retry_at);continue;}
    if(!Number.isFinite(c.weight)||c.weight<=0)throw new HttpError('routing_policy_invalid',503);
    for(const m of machines){
      if(!a.hosts.includes(m.machine_id)){worst=merge('machine_unavailable',null);continue;}
      if(m.resources&&(!m.resources.fresh||now-m.resources.observed_at>HOST_RESOURCE_TTL_MS)){worst=merge('host_resources_stale',now+REFRESH_BACKOFF_MS);continue;}
      if(!m.engines.includes(c.engine)||required.some(v=>!m.capabilities.includes(v)&&!m.capabilities.includes(v+':'+c.engine))||(c.effort&&!m.capabilities.includes('effort')&&!m.capabilities.includes('effort:'+c.engine))||required.filter(v=>!['headless','interactive'].includes(v)).some(v=>!c.capabilities.includes(v))){
        worst=merge('capability_unavailable',null);continue;}
      const weight=c.weight*((a.quota!.remaining-a.account.min_remaining)/100)*((a.account.capacity-a.account.occupied)/a.account.capacity)*((m.capacity-m.occupied)/m.capacity);
      if(!Number.isFinite(weight)||weight<=0)continue;
      options.push({machine_id:m.machine_id,engine:c.engine,model:c.model,...(c.effort?{effort:c.effort}:{}),account_id:c.account_id,policy_id:c.id,policy_version:policy.version,override_reason:override?input.override_reason!.trim():null,weight,quota_remaining:a.quota!.remaining,required_capabilities:required});
    }
  }
  if(!options.length){
    if(worst)return{ok:false,code:worst.code,hold:{retry_at:worst.retry_at}};
    throw new HttpError('machine_unavailable',409);
  }
  options.sort((a,b)=>(a.policy_id+'\0'+a.machine_id).localeCompare(b.policy_id+'\0'+b.machine_id));
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(input.operation_id));
  let draw=new DataView(digest).getUint32(0)/4294967296*options.reduce((sum,v)=>sum+v.weight,0);
  for(const option of options){draw-=option.weight;if(draw<0)return{ok:true,plan:option};}
  return{ok:true,plan:options[options.length-1]};
}
