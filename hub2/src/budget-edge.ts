import {METRICS, type Units, type Coverage} from './budget-do';
import {bodyObject, error, HttpError} from './http';
import type {Actor, Hub2Env} from './types';
import {nativeReturncode} from './native-exit';
import {SqlReceiptMeter, wrapSqlNamespaces, type SqlReceipt} from './sql-receipt';

type Plan={units:Units;max_do_calls:number;max_wall_ms:number;provenance:string};
export type BudgetKind='auth_request'|'public_request'|'public_read'|'public_write'|'resource_heartbeat'|'placement_request'|'connection_request'|'fleet_read'|'project_request'|'claim_request'|'message_request'|'account_request'|'background_effect'|'provider_refresh'|'scheduled_start'|'native_delivery'|'retry'|'operator_stop'|'native_exit'|'capacity_reconcile'|'auth_revoke'|'alert_delivery'|'budget_recovery';
type PlanSet=Plan&{by_kind?:Partial<Record<BudgetKind,Plan>>};
const KINDS=new Set<BudgetKind>(['auth_request','public_request','public_read','public_write','resource_heartbeat','placement_request','connection_request','fleet_read','project_request','claim_request','message_request','account_request','background_effect','provider_refresh','scheduled_start','native_delivery','retry','operator_stop','native_exit','capacity_reconcile','auth_revoke','alert_delivery','budget_recovery']);
const SAFE=new Set<BudgetKind>(['operator_stop','native_exit','capacity_reconcile','auth_revoke','alert_delivery','budget_recovery']);
type ResolveActor=(request:Request,env:Hub2Env)=>Promise<Actor|undefined>;
const NAMES=['CONTROL','PROJECTS','MACHINES','ACCOUNTS','MAILBOXES'] as const;
// Source-bound native Workerd profile: settlement performed 4 reads/5 writes
// in all tested warm, cold-alert, and restart cases. Keep a 64R/8W upper bound
// for this settle RPC; the reserve RPC is charged from its authenticated receipt.
const SETTLE_SQL_ALLOWANCE={reads:64,writes:8} as const;
const COVERAGE=Object.fromEntries(METRICS.map(k=>[k,'upper_bound'])) as Coverage;
const internal='https://budget.hub2.internal';
const admin=(a:Actor)=>a.scopes.includes('*')||a.scopes.includes('operator:admin');
const scoped=(a:Actor,s:string)=>admin(a)||a.scopes.includes(s);
const nextDay=()=>{const now=new Date();return Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate()+1);};

function validPlan(p:any):p is Plan{
 if(!p||typeof p.provenance!=='string'||p.provenance.length<8||p.provenance.length>500)return false;
 if(!Number.isSafeInteger(p.max_do_calls)||p.max_do_calls<1||p.max_do_calls>64||!Number.isSafeInteger(p.max_wall_ms)||p.max_wall_ms<100||p.max_wall_ms>60000)return false;
 if(!p.units||Object.keys(p.units).length!==METRICS.length||METRICS.some(k=>!Number.isSafeInteger(p.units[k])||p.units[k]<0))return false;
 // Never call missing CPU, duration, SQLite or persistent-storage coverage zero.
 return p.units.worker_requests>=1&&p.units.worker_cpu_ms>=1&&p.units.do_requests>=p.max_do_calls+3&&p.units.do_gb_ms>=1&&p.units.sqlite_rows_read>=1&&p.units.sqlite_rows_written>=1&&p.units.sqlite_byte_days>=1;
}
function planFor(env:Hub2Env,safety:boolean,kind:BudgetKind):Plan|undefined{
 try{
  const all=JSON.parse((env.HUB2_BUDGET_ENVELOPES||'')+(env.HUB2_BUDGET_ENVELOPES_PART2||'')||'null'),set=all?.[safety?'safety':'normal'] as PlanSet|undefined;
  if(all?.version!==1||!set)return;
  let p:Plan|undefined=set;
  if(set.by_kind!==undefined){
   if(!set.by_kind||typeof set.by_kind!=='object'||Array.isArray(set.by_kind))return;
   for(const [key,override]of Object.entries(set.by_kind))if(!KINDS.has(key as BudgetKind)||!validPlan(override))return;
   p=set.by_kind[kind]||set;
  }
  if(!validPlan(p))return;
  // Hub2 has no KV binding; a future one needs an explicitly priced envelope.
  return p;
 }catch{return;}
}
type ReceiptEnv=Hub2Env&{SQL_RECEIPT_METER?:SqlReceiptMeter};
function receiptContext(env:Hub2Env):{env:Hub2Env;meter:SqlReceiptMeter;parent:boolean}{
 const existing=(env as ReceiptEnv).SQL_RECEIPT_METER;
 if(existing&&typeof existing.checkpoint==='function'&&typeof existing.finish==='function'&&typeof existing.account==='function')
  return{env,meter:existing,parent:true};
 const meter=new SqlReceiptMeter();
 return{env:wrapSqlNamespaces(env,meter) as Hub2Env,meter,parent:false};
}
function sqlActual(plan:Units,receipt:SqlReceipt):Units|undefined{
 const reads=receipt.reads+SETTLE_SQL_ALLOWANCE.reads,writes=receipt.writes+SETTLE_SQL_ALLOWANCE.writes;
 if(!receipt.valid||!Number.isSafeInteger(reads)||!Number.isSafeInteger(writes))return;
 // Other dimensions remain configured upper bounds until authoritative provider
 // usage APIs supply actual CPU, duration, storage and KV observations.
 return{...plan,sqlite_rows_read:reads,sqlite_rows_written:writes};
}
function stripSqlReceiptHeaders(response:Response):Response{
 if(!response.headers.has('x-hub2-sql-receipt')&&!response.headers.has('x-hub2-sql-operation'))return response;
 const headers=new Headers(response.headers);headers.delete('x-hub2-sql-receipt');headers.delete('x-hub2-sql-operation');
 return new Response(response.body,{status:response.status,statusText:response.statusText,headers,webSocket:(response as any).webSocket} as ResponseInit);
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
 const controller=new AbortController(),timer=path==='/_internal/settle'?setTimeout(()=>controller.abort(),500):undefined;
 try{return await env.BUDGET.get(env.BUDGET.idFromName('global')).fetch(internal+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});}
 finally{if(timer!==undefined)clearTimeout(timer);}
}

/** A request is durably priced before any normal auth or application fanout.
 * Bounds come only from server configuration, never request headers/body. */
export async function budgetedPublicRequest(
 request:Request,env:Hub2Env,resolve:ResolveActor,run:(guarded:Hub2Env)=>Promise<Response>,
):Promise<Response>{
 const path=new URL(request.url).pathname,method=request.method;
 const origin=(env as ReceiptEnv).SQL_RECEIPT_METER?'nested':'worker';
 env=receiptContext(env).env; // Include bounded safety authentication in the charged prelude.
 let kind:BudgetKind;
 if(path==='/device'||path.startsWith('/auth/')||path.startsWith('/api/device/')||path.startsWith('/api/token/'))kind='auth_request';
 else if(method==='POST'&&path==='/api/heartbeat')kind='resource_heartbeat';
 else if(method==='GET'&&path==='/api/connect')kind='connection_request';
 else if(method==='POST'&&(path==='/api/sessions'||/^\/api\/intakes\/[^/]+\/start$/.test(path)))kind='placement_request';
 else if(method==='GET'&&['/api/fleet','/api/fleet/machines','/api/sessions','/api/status'].includes(path))kind='fleet_read';
 else if(path==='/api/messages'||/^\/api\/messages\/[^/]+(?:\/(?:read|ack|delivered|accept|complete|readdress|status))?$/.test(path))kind='message_request';
 else if(path==='/api/claims'||/^\/api\/claims\/[^/]+(?:\/(?:renew|release))?$/.test(path))kind='claim_request';
 else if(path==='/api/intakes'||/^\/api\/(?:intakes|intake\/tasks)(?:\/[^/]+(?:\/(?:start|history|append))?)?$/.test(path)||path==='/api/schedules'||/^\/api\/schedules\/[^/]+(?:\/(?:pause|resume|cancel|occurrences))?$/.test(path))kind='project_request';
 else if(/^\/api\/(?:accounts\/[^/]+\/)?(?:api\/)?account\/(?:quota|discovery|refresh)(?:\/|$)/.test(path))kind='provider_refresh';
 else if(path.startsWith('/api/accounts/')||path==='/api/account'||path.startsWith('/api/account/'))kind='account_request';
 else if(method==='GET')kind='public_read';
 else if(['POST','PUT','PATCH','DELETE'].includes(method))kind='public_write';
 else kind='public_request';
 try{kind=await safetyKind(request,env,resolve)||kind;}catch(e){return e instanceof HttpError?error(e.code,e.status):error('budget_safety_auth_unavailable',503);}
 // The authenticated override funds itself atomically under its proposed ceiling.
 if(kind==='budget_recovery'&&request.method==='POST'&&new URL(request.url).pathname==='/api/budget/override'){
  if(!planFor(env,true,'budget_recovery'))return error('budget_metrics_unknown',503);
  return stripSqlReceiptHeaders(await run(env)); // Router still enforces CSRF and fresh authentication before the dedicated adapter.
 }
 return stripSqlReceiptHeaders(await budgetedBackgroundRequest(env,kind,run,false,origin));
}

export async function budgetedOperatorOverride(request:Request,env:Hub2Env,actor:Actor):Promise<Response>{
 if(!scoped(actor,'budget:override'))return error('forbidden',403);
 const plan=planFor(env,true,'budget_recovery');if(!plan)return error('budget_metrics_unknown',503);
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
 env:Hub2Env,kind:BudgetKind,run:(guarded:Hub2Env)=>Promise<Response>,backgroundInvocation=false,origin:'worker'|'cron'|'nested'=(env as ReceiptEnv).SQL_RECEIPT_METER?'nested':'cron',
):Promise<Response>{
 const context=receiptContext(env);
 return context.meter.withScope(()=>budgetedScopedRequest(context.env,kind,run,backgroundInvocation,origin));
}
async function budgetedScopedRequest(env:Hub2Env,kind:BudgetKind,run:(guarded:Hub2Env)=>Promise<Response>,backgroundInvocation:boolean,origin:'worker'|'cron'|'nested'):Promise<Response>{
 const safety=SAFE.has(kind),plan=planFor(env,safety,kind);
 if(!plan)return error('budget_metrics_unknown',503);
 // Source-proven N/A: internal DO/subrequests are not Worker ingress or
 // Worker CPU. Cron has CPU but no billed inbound Worker request. Outer
 // Worker guards retain CPU bounds for any nested Worker-side helpers.
 const units={...plan.units,worker_requests:origin==='worker'?plan.units.worker_requests:0,
  worker_cpu_ms:origin==='nested'?0:plan.units.worker_cpu_ms,
  background_invocations:backgroundInvocation?Math.max(1,plan.units.background_invocations):plan.units.background_invocations};
 const id='edge:'+crypto.randomUUID();
 const receipt=receiptContext(env),receiptPoint=receipt.parent
  ?receipt.meter.checkpointUnassigned()
  :receipt.meter.checkpoint();
 let reservation:Response;
 try{reservation=await ledger(receipt.env,'/_internal/reserve',{id,kind,emergency:safety,estimated_units:units,coverage:COVERAGE});}
 catch{return error('budget_unavailable',503);}
 if(reservation.status===429){const denied=await reservation.json() as {retry_at?:number};return budgetPauseResponse(denied.retry_at);}
 if(!reservation.ok)return error('budget_unavailable',503);
 const reserved=await reservation.json() as {allowed?:boolean;retry_at?:number};
 if(reserved.allowed!==true)return budgetPauseResponse(reserved.retry_at);
 // The wall bound limits effect execution, not the preceding durable budget
 // reservation. Controller latency is funded separately in the request
 // envelope and must not turn a successful bounded effect into an ambiguous
 // result after the effect has run.
 let calls=0,started=performance.now();
 const guarded={...receipt.env} as Hub2Env;
 for(const name of NAMES){
  const ns=receipt.env[name];
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
  const measured=receipt.meter.finish(receiptPoint),actual=sqlActual(units,measured);
  if(!actual){
   // Attribution may overlap a concurrent operation. Its complete validated
   // envelope is still durably reserved: do not refund, settle or credit it.
   return response;
  }
  const usage=receipt.meter.usage(receiptPoint);
  const actualUnits={...actual,
   do_requests:usage.do_requests===undefined?units.do_requests:usage.do_requests+1,
   do_gb_ms:usage.do_gb_ms===undefined?units.do_gb_ms:usage.do_gb_ms+512};

  // Coverage remains upper_bound: cursor receipts and alarm-operation writes
  // are conservative source-bound SQL counts, not provider invoices.
  const settled=await ledger(receipt.env,'/_internal/settle',{id,actual_units:actualUnits,coverage:COVERAGE});
  if(!settled.ok)return error('budget_settlement_uncertain',503);
  if(receipt.parent&&!receipt.meter.account(receiptPoint,{reads:actualUnits.sqlite_rows_read,writes:actualUnits.sqlite_rows_written,...(usage.do_requests===undefined?{}:{do_requests:actualUnits.do_requests}),...(usage.do_gb_ms===undefined?{}:{do_gb_ms:actualUnits.do_gb_ms})}))
   return error('budget_sql_parent_accounting_unverified',503);
  return response;
 }catch(e){
  // Any started/ambiguous effect retains its full reservation across restart.
  return e instanceof HttpError?(e instanceof BudgetPause?budgetPauseResponse(e.retryAt):e.status===429?budgetPauseResponse():error(e.code,e.status)):error('budget_effect_outcome_uncertain',503);
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
