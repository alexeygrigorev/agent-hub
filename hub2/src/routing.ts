import { HttpError } from './http';
import type { Hub2Env } from './types';

export type RoutingRequest={operation_id:string;machine_id?:string;engine?:string;model?:string;effort?:string;override_reason?:string;interactive:boolean;with_goal?:boolean;required_capabilities?:string[]};
export type RoutingCandidate={id:string;engine:string;model:string;effort?:string;account_id:string;weight:number;capabilities:string[];enabled:boolean};
type Machine={machine_id:string;available:boolean;capacity:number;occupied:number;engines:string[];capabilities:string[]};
type Account={account:{enabled:number|boolean;capacity:number;occupied:number;min_remaining:number}|null;quota:{remaining:number;valid_until:number;source:string}|null;fresh:boolean;hosts:string[]};
export type RoutingDecision={machine_id:string;engine:string;model:string;effort?:string;account_id:string;policy_id:string;policy_version:unknown;override_reason:string|null;weight:number;quota_remaining:number;required_capabilities:string[]};
const internalActor=JSON.stringify({id:'hub2-routing',kind:'operator',scopes:['operator:admin','routing:read']});
async function read(namespace:DurableObjectNamespace,id:string,path:string){
  const response=await namespace.get(namespace.idFromName(id)).fetch('https://routing.internal'+path,{headers:{'x-hub2-actor':internalActor}});
  if(response.status===404&&path==='/_routing-policy')throw new HttpError('routing_policy_unconfigured',409);
  if(!response.ok)throw new HttpError('routing_evidence_unavailable',503);
  return await response.json() as any;
}

/** Advisory selection only: Account reservation and Machine CAS remain authoritative. */
export async function selectRoute(env:Hub2Env,input:RoutingRequest):Promise<RoutingDecision>{
  if(input.with_goal&&!input.interactive)throw new HttpError('goal_requires_interactive',400);
  const override=!!(input.machine_id||input.engine||input.model||input.effort);
  if(override&&(!input.override_reason?.trim()||input.override_reason.length>500))throw new HttpError('override_reason_required',400);
  const required=[...new Set([input.interactive?'interactive':'headless',...(input.with_goal?['goal']:[]),...(input.required_capabilities||[])])];
  if(required.length>32||required.some(v=>typeof v!=='string'||!v||v.length>80))throw new HttpError('invalid_required_capabilities',400);
  const policy=await read(env.CONTROL,'global','/_routing-policy');
  if(!Array.isArray(policy.candidates)||!policy.candidates.length)throw new HttpError('routing_policy_unconfigured',409);
  if(policy.candidates.length>32)throw new HttpError('routing_policy_bound_exceeded',503);
  const candidates=(policy.candidates as RoutingCandidate[]).filter(c=>c.enabled&&(!input.engine||input.engine===c.engine)&&(!input.model||input.model===c.model)&&(!input.effort||input.effort===c.effort));
  if(!candidates.length)throw new HttpError('model_policy_unavailable',409);
  const directory=await read(env.CONTROL,'global','/api/directory?kind=machines&limit=50');
  if(directory.next)throw new HttpError('routing_fleet_bound_exceeded',503);
  const ids=(directory.items as {id:string}[]).map(v=>v.id).filter(id=>!input.machine_id||input.machine_id===id);
  if(!ids.length)throw new HttpError('machine_unavailable',409);
  const machineResults=await Promise.all(ids.map(async id=>{try{return await read(env.MACHINES,id,'/_internal/routing') as Machine;}catch{return null;}}));
  const machines=machineResults.filter((m):m is Machine=>!!m&&m.available&&ids.includes(m.machine_id));
  const accountIds=[...new Set(candidates.map(c=>c.account_id))];
  const accounts=new Map<string,Account|null>(await Promise.all(accountIds.map(async id=>{try{return [id,await read(env.ACCOUNTS,id,'/_internal/routing') as Account] as const;}catch{return [id,null] as const;}})));
  const options:RoutingDecision[]=[];
  let quotaUnavailable=false,capabilityUnavailable=false;
  for(const c of candidates){
    const a=accounts.get(c.account_id),q=a?.quota;
    if(!a?.account?.enabled||!a.fresh||!q||q.valid_until<=Date.now()||!Number.isFinite(q.remaining)||q.remaining<=a.account.min_remaining||a.account.occupied>=a.account.capacity){quotaUnavailable=true;continue;}
    if(!Number.isFinite(c.weight)||c.weight<=0)throw new HttpError('routing_policy_invalid',503);
    for(const m of machines){
      if(!a.hosts.includes(m.machine_id))continue;
      if(!m.engines.includes(c.engine)||required.some(v=>!m.capabilities.includes(v)&&!m.capabilities.includes(v+':'+c.engine))||(c.effort&&!m.capabilities.includes('effort')&&!m.capabilities.includes('effort:'+c.engine))||required.filter(v=>!['headless','interactive'].includes(v)).some(v=>!c.capabilities.includes(v))){capabilityUnavailable=true;continue;}
      const weight=c.weight*((q.remaining-a.account.min_remaining)/100)*((a.account.capacity-a.account.occupied)/a.account.capacity)*((m.capacity-m.occupied)/m.capacity);
      if(!Number.isFinite(weight)||weight<=0)continue;
      options.push({machine_id:m.machine_id,engine:c.engine,model:c.model,...(c.effort?{effort:c.effort}:{}),account_id:c.account_id,policy_id:c.id,policy_version:policy.version,override_reason:override?input.override_reason!.trim():null,weight,quota_remaining:q.remaining,required_capabilities:required});
    }
  }
  if(!options.length)throw new HttpError(capabilityUnavailable?'capability_unavailable':quotaUnavailable?'quota_unavailable':'machine_unavailable',409);
  options.sort((a,b)=>(a.policy_id+'\0'+a.machine_id).localeCompare(b.policy_id+'\0'+b.machine_id));
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(input.operation_id));
  let draw=new DataView(digest).getUint32(0)/4294967296*options.reduce((sum,v)=>sum+v.weight,0);
  for(const option of options){draw-=option.weight;if(draw<0)return option;}
  return options[options.length-1];
}
