import {METRICS, type Units, type Coverage} from './budget-do';
import {bodyObject, error, HttpError} from './http';
import type {Actor, Hub2Env} from './types';
import {nativeReturncode} from './native-exit';

type Plan={units:Units;max_do_calls:number;max_wall_ms:number;provenance:string};
export type BudgetKind='public_request'|'background_effect'|'provider_refresh'|'scheduled_start'|'native_delivery'|'retry'|'operator_stop'|'native_exit'|'capacity_reconcile'|'auth_revoke'|'alert_delivery'|'budget_recovery';
const SAFE=new Set<BudgetKind>(['operator_stop','native_exit','capacity_reconcile','auth_revoke','alert_delivery','budget_recovery']);
type ResolveActor=(request:Request,env:Hub2Env)=>Promise<Actor|undefined>;
const NAMES=['CONTROL','PROJECTS','MACHINES','ACCOUNTS','MAILBOXES'] as const;
const COVERAGE=Object.fromEntries(METRICS.map(k=>[k,'upper_bound'])) as Coverage;
const internal='https://budget.hub2.internal';
const admin=(a:Actor)=>a.scopes.includes('*')||a.scopes.includes('operator:admin');
const scoped=(a:Actor,s:string)=>admin(a)||a.scopes.includes(s);
const nextDay=()=>{const now=new Date();return Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate()+1);};

function planFor(env:Hub2Env,safety:boolean):Plan|undefined{
 try{
  const all=JSON.parse(env.HUB2_BUDGET_ENVELOPES||'null'),p=all?.[safety?'safety':'normal'];
  if(all?.version!==1||!p||typeof p.provenance!=='string'||p.provenance.length<8||p.provenance.length>500)return;
  if(!Number.isSafeInteger(p.max_do_calls)||p.max_do_calls<1||p.max_do_calls>64||!Number.isSafeInteger(p.max_wall_ms)||p.max_wall_ms<100||p.max_wall_ms>60000)return;
  if(!p.units||Object.keys(p.units).length!==METRICS.length||METRICS.some(k=>!Number.isSafeInteger(p.units[k])||p.units[k]<0))return;
  // Never call missing CPU, duration, SQLite or persistent-storage coverage zero.
  if(p.units.worker_requests<1||p.units.worker_cpu_ms<1||p.units.do_requests<p.max_do_calls+3||p.units.do_gb_ms<1||p.units.sqlite_rows_read<1||p.units.sqlite_rows_written<1||p.units.sqlite_byte_days<1)return;
  // Hub2 has no KV binding; a future one needs an explicitly priced envelope.
  return p;
 }catch{return;}
}
async function safetyKind(r:Request,env:Hub2Env,resolve:ResolveActor):Promise<BudgetKind|undefined>{
 const path=new URL(r.url).pathname,method=r.method;
 const candidate=path==='/api/budget'||path==='/api/budget/override'
  ||path==='/api/token/revoke'||/^\/api\/sessions\/[^/]+\/(finish|started|commands(?:\/[^/]+\/ack)?)$/.test(path)
  ||path==='/api/connect'&&new URL(r.url).searchParams.get('safety')==='operator_stop';
 if(!candidate)return;
 const actor=await resolve(r,env);if(!actor)return;
 if(path==='/api/budget'&&method==='GET'&&scoped(actor,'budget:read'))return 'budget_recovery';
 if(path==='/api/budget/override'&&method==='POST'&&scoped(actor,'budget:override'))return 'budget_recovery';
 if(path==='/api/token/revoke'&&method==='POST'&&actor.kind==='operator')return 'auth_revoke';
 if(actor.kind==='machine'&&actor.resource_id&&path==='/api/connect'&&method==='GET'&&new URL(r.url).searchParams.get('safety')==='operator_stop'&&scoped(actor,'machine:offer'))return 'operator_stop';
 if(method!=='POST')return;
 const body=await bodyObject(r.clone(),12288);
 if(/\/commands$/.test(path)&&body.kind==='stop'&&scoped(actor,'session:stop'))return 'operator_stop';
 if(/\/commands\/[^/]+\/ack$/.test(path)&&actor.kind==='machine'&&actor.resource_id&&scoped(actor,'session:report')&&new URL(r.url).searchParams.get('safety')==='operator_stop')return 'operator_stop';
 if(/\/started$/.test(path)&&actor.kind==='machine'&&actor.resource_id&&scoped(actor,'session:report')
  &&typeof body.operation_id==='string'&&body.operation_id.length>0&&body.operation_id.length<=160
  &&Number.isSafeInteger(body.epoch)&&Number(body.epoch)>0&&typeof body.native_id==='string'
  &&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.native_id))return 'capacity_reconcile';
 if(/\/finish$/.test(path)&&actor.kind==='machine'&&actor.resource_id&&scoped(actor,'session:report')){
  const evidence=body.evidence as any,identity=evidence?.native_identity,exit=nativeReturncode(evidence?.native_exit);
  const session=path.split('/')[3];
  if(Number.isSafeInteger(body.epoch)&&Number(body.epoch)>0&&exit!==undefined&&exit===body.exit_code
   &&['aplexer_status','aplexer_retirement'].includes(evidence?.source)&&evidence?.containment_empty===true
   &&identity?.id===body.native_id&&identity?.tag==='task-hub2-'+session&&typeof identity?.workspace==='string')return 'native_exit';
 }
}
export class BudgetPause extends HttpError {constructor(readonly retryAt:number){super('budget_paused',429);}}
export function budgetPauseResponse(retryAt=nextDay()):Response{
 const seconds=Math.min(86400,Math.max(1,Math.ceil((retryAt-Date.now())/1000)));
 return Response.json({error:'budget_paused',retry_at:retryAt},{status:429,headers:{'Retry-After':String(seconds)}});
}
async function ledger(env:Hub2Env,path:string,body:unknown):Promise<Response>{
 if(!env.BUDGET)throw new HttpError('budget_unavailable',503);
 return env.BUDGET.get(env.BUDGET.idFromName('global')).fetch(internal+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}

/** A request is durably priced before any normal auth or application fanout.
 * Bounds come only from server configuration, never request headers/body. */
export async function budgetedPublicRequest(
 request:Request,env:Hub2Env,resolve:ResolveActor,run:(guarded:Hub2Env)=>Promise<Response>,
):Promise<Response>{
 let kind:BudgetKind='public_request';
 try{kind=await safetyKind(request,env,resolve)||kind;}catch(e){return e instanceof HttpError?error(e.code,e.status):error('budget_safety_auth_unavailable',503);}
 // The authenticated override funds itself atomically under its proposed ceiling.
 if(kind==='budget_recovery'&&request.method==='POST'&&new URL(request.url).pathname==='/api/budget/override'){
  if(!planFor(env,true))return error('budget_metrics_unknown',503);
  return run(env); // Router still enforces CSRF and fresh authentication before the dedicated adapter.
 }
 return budgetedBackgroundRequest(env,kind,run);
}

export async function budgetedOperatorOverride(request:Request,env:Hub2Env,actor:Actor):Promise<Response>{
 if(!scoped(actor,'budget:override'))return error('forbidden',403);
 const plan=planFor(env,true);if(!plan)return error('budget_metrics_unknown',503);
 const proposal=await bodyObject(request,4096),id='recovery:'+crypto.randomUUID();
 const stub=env.BUDGET.get(env.BUDGET.idFromName('global'));
 const reserved=await stub.fetch(internal+'/_internal/reserve',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},body:JSON.stringify({id,kind:'budget_recovery',emergency:true,override:proposal,estimated_units:plan.units,coverage:COVERAGE})});
 if(!reserved.ok)return reserved;
 const settled=await ledger(env,'/_internal/settle',{id,actual_units:plan.units,coverage:COVERAGE});
 if(!settled.ok)return error('budget_settlement_uncertain',503);
 return stub.fetch(internal+'/api/budget',{headers:{'x-hub2-actor':JSON.stringify(actor)}});
}

/** Namespace-only callers use this before alarms, retries or provider effects. */
export async function budgetedBackgroundRequest(
 env:Hub2Env,kind:BudgetKind,run:(guarded:Hub2Env)=>Promise<Response>,backgroundInvocation=false,
):Promise<Response>{
 const safety=SAFE.has(kind),plan=planFor(env,safety);
 if(!plan)return error('budget_metrics_unknown',503);
 const units={...plan.units,background_invocations:backgroundInvocation?Math.max(1,plan.units.background_invocations):plan.units.background_invocations};
 const id='edge:'+crypto.randomUUID(),started=performance.now();
 let reservation:Response;
 try{reservation=await ledger(env,'/_internal/reserve',{id,kind,emergency:safety,estimated_units:units,coverage:COVERAGE});}
 catch{return error('budget_unavailable',503);}
 if(reservation.status===429){const denied=await reservation.json() as {retry_at?:number};return budgetPauseResponse(denied.retry_at);}
 if(!reservation.ok)return error('budget_unavailable',503);
 const reserved=await reservation.json() as {allowed?:boolean;retry_at?:number};
 if(reserved.allowed!==true)return budgetPauseResponse(reserved.retry_at);
 let calls=0;
 const guarded={...env} as Hub2Env;
 for(const name of NAMES){
  const ns=env[name];
  if(!ns)continue;
  guarded[name]=new Proxy(ns,{get(target,key){
   if(key==='get')return(...args:any[])=>{
    const stub=(target.get as any)(...args);
    return new Proxy(stub,{get(t,k){
     if(k==='fetch')return async(...fetchArgs:any[])=>{
      if(++calls>plan.max_do_calls||performance.now()-started>plan.max_wall_ms)throw new HttpError('budget_effect_limit',429);
      const remaining=plan.max_wall_ms-(performance.now()-started),controller=new AbortController();
      const timer=setTimeout(()=>controller.abort(),Math.max(1,remaining));
      try{return await t.fetch(new Request(fetchArgs[0],{...fetchArgs[1],signal:controller.signal}));}
      finally{clearTimeout(timer);}
     };
     const value=Reflect.get(t,k,t);return typeof value==='function'?value.bind(t):value;
    }});
   };
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }}) as any;
 }
 try{
  const response=await run(guarded);
  if(performance.now()-started>plan.max_wall_ms)return error('budget_effect_outcome_uncertain',503);
  // Conservatively debit the entire envelope, including controller overhead.
  // Coverage stays upper_bound; these are not claimed runtime CPU observations.
  const settled=await ledger(env,'/_internal/settle',{id,actual_units:units,coverage:COVERAGE});
  if(!settled.ok)return error('budget_settlement_uncertain',503);
  return response;
 }catch(e){
  // Any started/ambiguous effect retains its full reservation across restart.
  return e instanceof HttpError?(e.status===429?budgetPauseResponse():error(e.code,e.status)):error('budget_effect_outcome_uncertain',503);
 }
}

export function budgetAlertFields(id:string,payload:Record<string,unknown>):Record<string,unknown>{
 const text=JSON.stringify({service:'hub2',...payload},null,2);
 return {from:'Agent Hub <alerts@dtcdev.click>',to:'dev@dtcdev.click',
  subject:'[agent-hub] '+(payload.transport_test?'TRANSPORT TEST ':'')+'daily cost '+String(payload.day||'unknown'),
  snippet:text.replace(/\s+/g,' ').slice(0,200),text,date:new Date().toISOString(),
  message_id:'hub-alert-'+id,attachments:[],
  alert:{id,subject_kind:'cost',subject:'hub:'+String(payload.day||'unknown'),state:String(payload.kind||'threshold'),detail:text,opened_at:payload.observed_at,...payload}};
}

/** At-least-once delivery with a stable idempotency key; receiver must dedupe it. */
export async function dispatchBudgetAlerts(env:Hub2Env):Promise<{delivered:number;pending:number}>{
 const url=env.HUB_ALERT_WEBHOOK||'https://dapier.dtcdev.click/hooks/webhook/dev',key=env.HUB_ALERT_WEBHOOK_TOKEN;
 if(!url||!key||new URL(url).protocol!=='https:')throw new Error('budget_alert_channel_unconfigured');
 const stub=env.BUDGET.get(env.BUDGET.idFromName('global'));
 const pending=await stub.fetch(internal+'/_internal/alerts?limit=10');
 if(!pending.ok)throw new Error('budget_alert_queue_unavailable');
 const {items}=await pending.json() as {items:{id:string}[]};let delivered=0;
 for(const item of items){
  const outcome=await budgetedBackgroundRequest(env,'alert_delivery',async()=>{
   const claim=await ledger(env,'/_internal/alerts/'+encodeURIComponent(item.id)+'/claim',{worker_id:'budget-dispatcher'});
   if(!claim.ok)return claim;
   const {alert,claim_token}=await claim.json() as {alert:{payload:Record<string,unknown>};claim_token:string};
   try{
    const sent=await fetch(url,{method:'POST',redirect:'manual',signal:AbortSignal.timeout(10000),headers:{'Content-Type':'application/json','Authorization':'Bearer '+key,'Idempotency-Key':'hub-alert-'+item.id},body:JSON.stringify(budgetAlertFields(item.id,alert.payload))});
    if(!sent.ok)throw new Error('delivery_refused');
    const ack=await ledger(env,'/_internal/alerts/'+encodeURIComponent(item.id)+'/ack',{claim_token});
    if(!ack.ok)throw new Error('delivery_ack_uncertain');
    return new Response(null,{status:204});
   }catch{
    await ledger(env,'/_internal/alerts/'+encodeURIComponent(item.id)+'/fail',{claim_token});
    return new Response(null,{status:503});
   }
  });
  if(outcome.status===204)delivered++;
 }
 return {delivered,pending:items.length-delivered};
}
