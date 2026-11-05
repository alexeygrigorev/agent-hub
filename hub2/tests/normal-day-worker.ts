import sourceWorker from '../src/worker';
import {providerFetch} from './normal-day-provider';
import {fixtureBudgetEnvelopes} from './budget-fixture';
// Test-only real five-DO fixture, native cursor profiles; never deploy.
import authFixture from './normal-day-auth-worker';
import fixture,{CoreControl as Control,CoreProject as Project,CoreMachine as Machine,CoreAccount as Account,CoreMailbox as Mailbox,CoreBudget as Budget} from './core-integration-worker';
// The test advances Date.now independently of workerd's physical alarm clock.
// Fixed translation persists across restart; due handlers are explicitly driven.
const FIXTURE_ALARM_SHIFT=7*86400000;
type Profile=Record<string,{calls:number;reads:number;writes:number}>;
function profiled(ctx:DurableObjectState,profile:Profile,storageCalls:Record<string,{attempted:number;succeeded:number;failed:number}>,kind:string):DurableObjectState{
 const sql=new Proxy(ctx.storage.sql,{get(target,key){if(key==='__fixtureOwner')return{kind,id:ctx.id.toString()};if(key==='exec')return(query:string,...bindings:unknown[])=>{const cursor=target.exec(query,...bindings);let counted=false;return new Proxy(cursor,{get(c,k){if(k==='toArray')return()=>{const rows=c.toArray();if(!counted){counted=true;const p=profile[query]??={calls:0,reads:0,writes:0};p.calls++;p.reads+=c.rowsRead;p.writes+=c.rowsWritten;}return rows;};const v=Reflect.get(c,k,c);return typeof v==='function'?v.bind(c):v;}});};const v=Reflect.get(target,key,target);return typeof v==='function'?v.bind(target):v;}});
 const storage=new Proxy(ctx.storage,{get(t,k){if(k==='sql')return sql;if(['getAlarm','setAlarm','deleteAlarm'].includes(String(k)))return async(...args:any[])=>{const c=storageCalls[String(k)]??={attempted:0,succeeded:0,failed:0};c.attempted++;try{const actualArgs=k==='setAlarm'?[Number(args[0])+FIXTURE_ALARM_SHIFT,...args.slice(1)]:args;const physical=await (t as any)[k](...actualArgs);const result=k==='getAlarm'&&physical!==null?Number(physical)-FIXTURE_ALARM_SHIFT:physical;c.succeeded++;console.log('HUB2_METER '+JSON.stringify({type:'storage',method:String(k),succeeded:true}));return result;}catch(error){c.failed++;console.log('HUB2_METER '+JSON.stringify({type:'storage',method:String(k),succeeded:false}));throw error;}};const v=Reflect.get(t,k,t);return typeof v==='function'?v.bind(t):v;}});
 return new Proxy(ctx,{get(t,k){if(k==='storage')return storage;const v=Reflect.get(t,k,t);return typeof v==='function'?v.bind(t):v;}});
}
function profileClass<T extends new(...args:any[])=>any>(Base:T){return class extends Base{private profile:Profile;private storageCalls:Record<string,{attempted:number;succeeded:number;failed:number}>;constructor(...args:any[]){const profile:Profile={},storageCalls={};super(profiled(args[0],profile,storageCalls,Base.name),args[1]);this.profile=profile;this.storageCalls=storageCalls;}fetch(r:Request){if(new URL(r.url).pathname==='/_fixture/state')return(async()=>{await(this as any).core.ready;return Response.json({counts:(this as any).core.db.snapshot(),alarm:await(this as any).ctx.storage.getAlarm()});})();if(new URL(r.url).pathname==='/_fixture/profile')return Promise.resolve(Response.json({profile:this.profile,storage:this.storageCalls,meter_id:(this as any).core.db.__fixtureMeterId,meter:(this as any).core.db.snapshot()}));return super.fetch(r);}};}
export const CoreControl=profileClass(Control),CoreMachine=profileClass(Machine),CoreMailbox=profileClass(Mailbox);
const ProfiledProject=profileClass(Project);
export class CoreProject extends ProfiledProject{
 private readonly fixtureToken:string;
 constructor(ctx:DurableObjectState,env:any){super(ctx,env);this.fixtureToken=env.HUB2_DEV_OPERATOR_TOKEN||'';}
 async fetch(request:Request){
  if(new URL(request.url).pathname==='/_fixture/align-schedules'){
   if(request.method!=='POST'||request.headers.get('Authorization')!=='Bearer '+this.fixtureToken)return Response.json({error:'unauthorized'},{status:401});
   const b=await request.json()as{root_id:string;root_due:number;writer_id:string;writer_due:number};
   if(typeof b.root_id!=='string'||typeof b.writer_id!=='string'||!Number.isSafeInteger(b.root_due)||!Number.isSafeInteger(b.writer_due))return Response.json({error:'invalid_fixture_schedule'},{status:400});
   const db=(this as any).core.db;db.run('UPDATE work_schedules SET next_due=? WHERE id=? AND state=?',b.root_due,b.root_id,'active');db.run('UPDATE work_schedules SET next_due=? WHERE id=? AND state=?',b.writer_due,b.writer_id,'active');
   await (this as any).ctx.storage.setAlarm(b.root_due);
   return Response.json({aligned:true,root_due:b.root_due,writer_due:b.writer_due});
  }
  return super.fetch(request);
 }
}
export class CoreAccount extends profileClass(Account){constructor(ctx:DurableObjectState,env:any){super(ctx,{...env,HUB2_BUDGET_ENVELOPES:env.HUB2_BUDGET_ENVELOPES??fixtureBudgetEnvelopes,HUB2_PROVIDER_FETCH:providerFetch});}}
const ProfiledBudget=profileClass(Budget);
export class CoreBudget extends ProfiledBudget{
 private readonly fixtureContext:DurableObjectState;
 constructor(ctx:DurableObjectState,env:any){super(ctx,env);this.fixtureContext=ctx;}
 async fetch(request:Request){
  if(new URL(request.url).pathname==='/_fixture/ledger'){
   await (this as any).core.fetch(new Request('https://budget.internal/_metrics'));
   const db=(this as any).core.db;
   return Response.json({object_id:this.fixtureContext.id.toString(),days:db.all('SELECT day,settled_pusd,reserved_pusd,units_json,costs_json,coverage_json,hard_stopped FROM budget_days ORDER BY day'),kinds:db.all('SELECT day,kind,state,COUNT(*) AS reservations,SUM(CAST(estimate_pusd AS INTEGER)) AS estimate_pusd,SUM(CAST(actual_pusd AS INTEGER)) AS actual_pusd FROM budget_reservations GROUP BY day,kind,state ORDER BY day,kind,state')});
  }
  if(new URL(request.url).pathname==='/_fixture/state'){
   await(this as any).core.ready;return Response.json({counts:(this as any).core.db.snapshot(),alarm:await this.fixtureContext.storage.getAlarm()});
  }
  return super.fetch(request);
 }
}
export default{async fetch(r:Request,env:any){const pathname=new URL(r.url).pathname;if(pathname==='/_fixture/alert-stats'){if(r.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return new Response(null,{status:401});return fetch('https://normal-day-alert.fixture/_stats');}if(pathname==='/_fixture/budget-cron'){if(r.method!=='POST'||r.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return new Response(null,{status:401});await sourceWorker.scheduled({} as ScheduledController,env);return Response.json({invoked:true});}const m=pathname.match(/^\/_fixture\/(control|project|machine|account|mailbox|budget)\/([^/]+)\/(profile|ledger)$/),align=pathname.match(/^\/_fixture\/project\/([^/]+)\/align-schedules$/);if(pathname==='/_fixture/barrier'){if(r.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return new Response(null,{status:401});const b=await r.json()as{id:string};console.log('HUB2_BARRIER '+b.id);return Response.json({observed:true});}if(pathname==='/_fixture/provider')return authFixture.fetch(r,env);if(align){if(r.method!=='POST'||r.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return Response.json({error:'unauthorized'},{status:401});return env.PROJECTS.get(env.PROJECTS.idFromName(decodeURIComponent(align[1]))).fetch(new Request('https://fixture.internal/_fixture/align-schedules',r));}if(!m){const u=new URL(r.url);return fixture.fetch(new Request('https://hub.fixture'+u.pathname+u.search,r),env);}if(r.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return Response.json({error:'unauthorized'},{status:401});const ns=({control:env.CONTROL,project:env.PROJECTS,machine:env.MACHINES,account:env.ACCOUNTS,mailbox:env.MAILBOXES,budget:env.BUDGET}as any)[m[1]];return ns.get(ns.idFromName(decodeURIComponent(m[2]))).fetch(new Request('https://fixture.internal/_fixture/'+m[3]));}};
