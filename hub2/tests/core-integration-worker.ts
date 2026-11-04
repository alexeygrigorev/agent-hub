// Local test entry only. Never referenced by a deployment configuration.
import worker from '../src/worker';
import { ControlDO } from '../src/control-do';
import { ProjectDO } from '../src/project-do';
import { MachineDO } from '../src/machine-do';
import { AccountDO } from '../src/account-do';
import { MailboxDO } from '../src/mailbox-do';
import { MeteredDb } from '../src/db';

const actualFixtureNow=Date.now;
type Core={fetch(request:Request):Promise<Response>;alarm?:()=>Promise<void>};
async function dispatch(request:Request,ctx:DurableObjectState,core:Core,kind:string):Promise<Response>{
 const path=new URL(request.url).pathname;
 if(path==='/_fixture/clock'&&kind==='control'&&request.method==='POST'){const b=await request.json()as{offset:number};if(!Number.isSafeInteger(b.offset)||b.offset<0||b.offset>86400000)return Response.json({error:'invalid_fixture_clock'},{status:400});Date.now=()=>actualFixtureNow()+b.offset;return Response.json({offset:b.offset});}
 if(path==='/_fixture/state'){
  const response=await core.fetch(new Request('https://fixture.internal/_metrics'));
  return Response.json({...(await response.json() as object),alarm:await ctx.storage.getAlarm()});
 }
 if(path==='/_fixture/meter-invalid'){const bad=new MeteredDb({exec(query:string,...bindings:unknown[]){const real=ctx.storage.sql.exec(query,...bindings);return {toArray:()=>real.toArray()};}} as any);bad.all('SELECT 1 AS native_fixture');return Response.json({counts:bad.snapshot()});}
 if(path==='/_fixture/alarm'&&request.method==='POST'){
  if(!core.alarm)return Response.json({error:'alarm_unavailable'},{status:400});
  await core.alarm();return Response.json({invoked:true});
 }
 if(path==='/_fixture/expire-offer'&&request.method==='POST'&&kind==='machine'){
  const b=await request.json() as {id:string};
  ctx.storage.sql.exec('UPDATE sessions SET offer_expires_at=? WHERE id=? AND state=?',Date.now()-1,b.id,'offered').toArray();
  (core as any).db.cache.clear(); // Test-only raw deadline mutation must invalidate instance data.
  await core.alarm!();return Response.json({forced_due:true});
 }
 if(path==='/_fixture/due-outbox'&&request.method==='POST'&&kind==='machine'){
  const b=await request.json() as {id:string};
  ctx.storage.sql.exec('UPDATE machine_outbox SET next_action_at=? WHERE id=? AND state=?',Date.now()-1,b.id,'pending').toArray();
  (core as any).db.cache.clear(); // Test-only raw deadline mutation must invalidate instance data.
  await core.alarm!();return Response.json({forced_due:true});
 }
 if(path==='/_fixture/expire-account'&&request.method==='POST'&&kind==='account'){
  const b=await request.json() as {operation_id:string};
  ctx.storage.sql.exec('UPDATE reservations SET expires_at=? WHERE operation_id=? AND state=?',Date.now()-1,b.operation_id,'running').toArray();
  (core as any).db.cache.clear(); // Test-only raw deadline mutation must invalidate instance data.
  await core.alarm!();return Response.json({forced_due:true});
 }
 if(path==='/_fixture/due-saga'&&request.method==='POST'&&kind==='project'){
  const b=await request.json() as {operation_id:string};
  ctx.storage.sql.exec('UPDATE dispatch_sagas SET next_action_at=? WHERE operation_id=? AND next_action_at IS NOT NULL',Date.now()-1,b.operation_id).toArray();
  (core as any).db.cache.clear(); // Test-only raw deadline mutation must invalidate instance data.
  await core.alarm!();return Response.json({forced_due:true});
 }
 return core.fetch(request);
}

function alarmFaultContext(ctx:DurableObjectState,fault:{armed:boolean}):DurableObjectState{
 const storage=new Proxy(ctx.storage,{get(target,key){if(key==='setAlarm')return async(at:number)=>{if(fault.armed){fault.armed=false;throw new Error('fixture_alarm_write_interrupted');}return target.setAlarm(at);};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
 return new Proxy(ctx,{get(target,key){if(key==='storage')return storage;const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
}
export class CoreControl{
 private core:ControlDO;private faults=new Set<string>();private cryptGate?:{entered:boolean;release?:()=>void};
 constructor(private ctx:DurableObjectState,env:any){this.core=new ControlDO(ctx,env);}
 async fetch(r:Request){const path=new URL(r.url).pathname;if(path==='/_fixture/hold-worker-crypt'){const workers=(this.core as any).workers,original=workers.crypt.bind(workers),gate:{entered:boolean;release?:()=>void}=this.cryptGate={entered:false};workers.crypt=async(...args:any[])=>{const value=await original(...args);if(!args[2]&&!gate.entered){gate.entered=true;await new Promise<void>(resolve=>gate.release=resolve);}return value;};return Response.json({armed:true});}if(path==='/_fixture/worker-crypt-state')return Response.json({entered:this.cryptGate?.entered||false});if(path==='/_fixture/release-worker-crypt'){this.cryptGate?.release?.();return Response.json({released:true});}if(path==='/_fixture/revoke-worker')return this.core.fetch(new Request('https://control.internal/_worker/revoke',r));if(path==='/_fixture/fault'&&r.method==='POST'){const b=await r.json()as{paths:string[]};if(!Array.isArray(b.paths)||b.paths.some(p=>!['/_worker/issue','/_worker/revoke'].includes(p)))return Response.json({error:'invalid_fixture_fault'},{status:400});this.faults=new Set(b.paths);return Response.json({armed:true});}const response=await dispatch(r,this.ctx,this.core,'control');if(response.ok&&this.faults.delete(path))return Response.json({error:'fixture_lost_worker_success'},{status:503});return response;}
 alarm(){return this.core.alarm();}
}
export class CoreProject{
 private core:ProjectDO;private fault={armed:false};private faults=new Set<string>();
 constructor(private ctx:DurableObjectState,env:any){this.core=new ProjectDO(alarmFaultContext(ctx,this.fault),env);}
 async fetch(r:Request){const path=new URL(r.url).pathname;
  if(path==='/_fixture/fail-next-alarm'){this.fault.armed=true;return Response.json({armed:true});}
  if(path==='/_fixture/fault'&&r.method==='POST'){const b=await r.json() as {paths:string[]};if(!Array.isArray(b.paths)||b.paths.some(p=>!['/_internal/started','/_internal/finish'].includes(p)))return Response.json({error:'invalid_fixture_fault'},{status:400});this.faults=new Set(b.paths);return Response.json({armed:true});}
  const response=await dispatch(r,this.ctx,this.core,'project');if(response.ok&&this.faults.delete(path))return Response.json({error:'fixture_lost_success_response'},{status:503});return response;
 }alarm(){return this.core.alarm();}
}
export class CoreAccount{private core:AccountDO;constructor(private ctx:DurableObjectState,env:any){this.core=new AccountDO(ctx,env);}fetch(r:Request){return dispatch(r,this.ctx,this.core,'account');}alarm(){return this.core.alarm();}}
export class CoreMailbox{private core:MailboxDO;constructor(private ctx:DurableObjectState,env:any){this.core=new MailboxDO(ctx,env);}fetch(r:Request){return dispatch(r,this.ctx,this.core,'mailbox');}alarm(){return this.core.alarm();}}
export class CoreMachine{
 private core:MachineDO;private faults=new Set<string>();constructor(private ctx:DurableObjectState,env:any){this.core=new MachineDO(ctx,env);}
 async fetch(r:Request){const path=new URL(r.url).pathname;if(path==='/_fixture/fault'&&r.method==='POST'){const b=await r.json() as {paths:string[]};if(!Array.isArray(b.paths)||b.paths.some(p=>!['/_internal/reserve','/_internal/activate'].includes(p)))return Response.json({error:'invalid_fixture_fault'},{status:400});this.faults=new Set(b.paths);return Response.json({armed:true});}const response=await dispatch(r,this.ctx,this.core,'machine');if(response.ok&&this.faults.delete(path))return Response.json({error:'fixture_lost_success_response'},{status:503});return response;}alarm(){return this.core.alarm();}
 webSocketMessage(s:WebSocket,m:string|ArrayBuffer){return this.core.webSocketMessage(s,m);}
 webSocketClose(s:WebSocket,c:number,r:string,w:boolean){return this.core.webSocketClose(s,c,r,w);}
 webSocketError(s:WebSocket,e:unknown){return this.core.webSocketError(s,e);}
}
export default {async fetch(request:Request,env:any):Promise<Response>{
 const u=new URL(request.url),m=u.pathname.match(/^\/_fixture\/(control|project|machine|account|mailbox)\/([^/]+)\/(state|meter-invalid|fault|fail-next-alarm|alarm|expire-offer|expire-account|due-saga|due-outbox|clock|hold-worker-crypt|worker-crypt-state|release-worker-crypt|revoke-worker)$/);
 if(!m)return worker.fetch(request,{...env,ASSETS:{fetch:async()=>new Response(env.HUB2_FIXTURE_ASSET_HTML,{headers:{'Content-Type':'text/html; charset=utf-8'}})}});
 if(request.headers.get('Authorization')!=='Bearer '+env.HUB2_DEV_OPERATOR_TOKEN)return Response.json({error:'unauthorized'},{status:401});
 const ns=({control:env.CONTROL,project:env.PROJECTS,machine:env.MACHINES,account:env.ACCOUNTS,mailbox:env.MAILBOXES} as any)[m[1]];
 return ns.get(ns.idFromName(decodeURIComponent(m[2]))).fetch(new Request('https://fixture.internal/_fixture/'+m[3],request));
}};
