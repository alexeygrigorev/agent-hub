import {AsyncLocalStorage} from 'node:async_hooks';
import type {MeteredDb} from './db';

export const SQL_PROBE_HEADER='x-hub2-sql-operation';
export const SQL_RECEIPT_HEADER='x-hub2-sql-receipt';
export type SqlReceipt={v:1;valid:boolean;reads:number;writes:number;operation_id?:string;do_requests?:number;do_gb_ms?:number};
type Scope={reads:number;writes:number;fundedReads:number;fundedWrites:number;fault:number;pending:number;closed:boolean;requests:number;duration:number;fundedRequests:number;fundedDuration:number;requestsValid:boolean;durationValid:boolean;started:number;prelude:number;own:boolean};
type Point={scope:Scope;reads:number;writes:number;fundedReads:number;fundedWrites:number;fault:number;requests:number;duration:number;fundedRequests:number;fundedDuration:number};
const scope=():Scope=>({reads:0,writes:0,fundedReads:0,fundedWrites:0,fault:0,pending:0,closed:false,requests:0,duration:0,fundedRequests:0,fundedDuration:0,requestsValid:true,durationValid:true,started:performance.now(),prelude:0,own:false});
const names=['CONTROL','PROJECTS','MACHINES','ACCOUNTS','MAILBOXES'] as const;
const finite=(n:unknown):n is number=>Number.isSafeInteger(n)&&Number(n)>=0;
export function parseSqlReceipt(raw:string|null,operationId?:string):SqlReceipt|undefined{
 if(!raw||raw.length>384)return;
 try{const r=JSON.parse(raw);if(r.v===1&&r.valid===true&&finite(r.reads)&&finite(r.writes)&&(!operationId||r.operation_id===operationId))return r;}catch{}
}

/** Native cursor deltas follow their async request, including overlapping HTTP
 * and WebSocket handlers. Unawaited children invalidate only their own caller.
 * Every alarm operation retains one conservative additional billed write. */
export class SqlReceiptMeter{
 private readonly local=new AsyncLocalStorage<Scope>();
 private unassigned=scope();
 private readonly observable:boolean;
 private readonly points=new WeakSet<object>();
 private readonly credits=new WeakMap<object,{reads:number;writes:number;do_requests?:number;do_gb_ms?:number}>();
 private readonly constructed=performance.now();private initializationMs:number|undefined;private initializationTaken=false;
 constructor(private readonly db?:MeteredDb,private readonly options:{hibernates_after_handler?:boolean;cpu_upper_ms?:number}={}){
  this.observable=!db||typeof db.observeRows==='function';
  const initial=db?.snapshot();
  if(initial){this.unassigned.reads=initial.rows_read;this.unassigned.writes=initial.rows_written+initial.alarm_operations;}
  if(db&&!db.observeRows)this.unassigned.fault++;
  db?.observeRows?.((reads,writes,valid)=>{const s=this.current();s.reads+=reads;s.writes+=writes;if(!valid||!finite(s.reads)||!finite(s.writes))s.fault++;},()=>this.assertOpen(),()=>{this.initializationMs=performance.now()-this.constructed;});
 }
 private assertOpen():void{if(this.local.getStore()?.closed)throw new Error('sql_scope_closed');}
 private current():Scope{return this.local.getStore()??this.unassigned;}
 private elapsed(s:Scope):number{return s.own?Math.ceil((performance.now()-s.started+s.prelude+(this.options.cpu_upper_ms||0))*0.25):0;}
 private take(http=false):Scope{
  const s=this.unassigned;this.unassigned=scope();s.started=performance.now();s.own=!!this.db;
  if(s.own){s.requests++;s.durationValid=http&&!!this.options.hibernates_after_handler&&finite(this.options.cpu_upper_ms)&&this.options.cpu_upper_ms!>0&&this.options.cpu_upper_ms!<=300000&&this.initializationMs!==undefined;if(this.initializationMs!==undefined&&!this.initializationTaken){s.prelude=this.initializationMs+(this.options.cpu_upper_ms||0);this.initializationTaken=true;}}
  return s;
 }
 async withScope<T>(run:()=>Promise<T>):Promise<T>{
  const existing=this.local.getStore();
  if(existing&&!existing.closed)return run();
  const s=this.take();
  return this.local.run(s,async()=>{try{return await run();}finally{s.closed=true;}});
 }
 checkpoint():Point{
  const s=this.current(),p={scope:s,reads:s.reads,writes:s.writes,fundedReads:s.fundedReads,fundedWrites:s.fundedWrites,fault:s.fault,requests:s.requests,duration:s.duration+this.elapsed(s),fundedRequests:s.fundedRequests,fundedDuration:s.fundedDuration};
  this.points.add(p);return p;
 }
 checkpointUnassigned():Point{
  const p=this.checkpoint();p.reads=0;p.writes=0;p.fundedReads=0;p.fundedWrites=0;p.requests=0;p.duration=0;p.fundedRequests=0;p.fundedDuration=0;return p;
 }
 finish(p:Point):SqlReceipt{
  const s=p.scope,reads=s.reads-p.reads-(s.fundedReads-p.fundedReads),writes=s.writes-p.writes-(s.fundedWrites-p.fundedWrites);
  return{v:1,valid:this.observable&&this.points.has(p)&&this.current()===s&&!s.closed&&s.pending===0&&s.fault===p.fault&&(this.db?.snapshot().measurement_valid??true)&&finite(reads)&&finite(writes),reads,writes};
 }
 usage(p:Point):{do_requests?:number;do_gb_ms?:number}{
  if(!this.finish(p).valid)return{};
  const s=p.scope,requests=s.requests-p.requests-(s.fundedRequests-p.fundedRequests),duration=s.duration+this.elapsed(s)-p.duration-(s.fundedDuration-p.fundedDuration);
  return{...(s.requestsValid&&finite(requests)?{do_requests:requests}:{}),...(s.durationValid&&finite(duration)?{do_gb_ms:duration}:{})};
 }
 /** Invoke only after durable settlement ACK, never after a lost/failed ACK. */
 account(p:Point,paid:{reads:number;writes:number;do_requests?:number;do_gb_ms?:number}):boolean{
  const prior=this.credits.get(p);
  if(prior){if(prior.reads===paid.reads&&prior.writes===paid.writes&&prior.do_requests===paid.do_requests&&prior.do_gb_ms===paid.do_gb_ms)return true;this.current().fault++;return false;}
  const r=this.finish(p),usage=this.usage(p);
  if(!r.valid||!finite(paid.reads)||!finite(paid.writes)||r.reads>paid.reads||r.writes>paid.writes||paid.do_requests!==undefined&&(!finite(paid.do_requests)||usage.do_requests===undefined||usage.do_requests>paid.do_requests)||paid.do_gb_ms!==undefined&&(!finite(paid.do_gb_ms)||usage.do_gb_ms===undefined||usage.do_gb_ms>paid.do_gb_ms)){this.current().fault++;return false;}
  this.credits.set(p,{...paid});p.scope.fundedReads+=r.reads;p.scope.fundedWrites+=r.writes;if(paid.do_requests!==undefined)p.scope.fundedRequests+=usage.do_requests!;if(paid.do_gb_ms!==undefined)p.scope.fundedDuration+=usage.do_gb_ms!;return true;
 }
 observe(response:Response,operationId?:string,target=this.current()):void{
  const r=parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER),operationId);
  if(!r){target.fault++;return;}
  target.reads+=r.reads;target.writes+=r.writes;
  if(finite(r.do_requests))target.requests+=r.do_requests-1;else target.requestsValid=false;
  if(finite(r.do_gb_ms))target.duration+=r.do_gb_ms;else target.durationValid=false;
  if(!finite(target.reads)||!finite(target.writes))target.fault++;
 }
 beginChild():Scope{this.assertOpen();const s=this.current();s.pending++;s.requests++;return s;}
 endChild(s=this.current()):void{s.pending--;}
 failed(s=this.current()):void{s.fault++;}
 async measure(run:()=>Promise<Response>,operationId?:string|null):Promise<Response>{
  const s=this.take(true);
  return this.local.run(s,async()=>{
   const p=this.checkpointUnassigned();
   try{
    const response=await run(),receipt={...this.finish(p),...this.usage(p)};
    receipt.operation_id=typeof operationId==='string'&&operationId.length<=80?operationId:crypto.randomUUID();
    const headers=new Headers(response.headers);headers.set(SQL_RECEIPT_HEADER,JSON.stringify(receipt));
    return new Response(response.body,{status:response.status,statusText:response.statusText,headers,webSocket:(response as any).webSocket} as ResponseInit);
   }catch(error){this.failed();throw error;}finally{s.closed=true;}
  });
 }
}

export function wrapSqlNamespaces<T extends object>(env:T,meter:SqlReceiptMeter):T&{SQL_RECEIPT_METER:SqlReceiptMeter}{
 const cache=new WeakMap<object,unknown>();
 const wrap=(ns:any)=>{
  const prior=cache.get(ns);if(prior)return prior;
  const wrapped=new Proxy(ns,{get(target,key){
   if(key==='get')return(...args:any[])=>{
    const stub=target.get(...args);
    return new Proxy(stub,{get(t,k){
     if(k==='fetch')return async(...fetchArgs:any[])=>{
      const child=meter.beginChild();
      try{
       const request=new Request(fetchArgs[0],fetchArgs[1]),headers=new Headers(request.headers),operationId=crypto.randomUUID();
       headers.set(SQL_PROBE_HEADER,operationId);
       const response=await t.fetch(new Request(request,{headers}));meter.observe(response,operationId,child);return response;
      }catch(error){meter.failed(child);throw error;}finally{meter.endChild(child);}
     };
     const value=Reflect.get(t,k,t);return typeof value==='function'?value.bind(t):value;
    }});
   };
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  cache.set(ns,wrapped);return wrapped;
 };
 // Preserve live bindings and key rotation rather than snapshotting the env.
 // A fresh proxy target avoids invariants of non-configurable host bindings.
 return new Proxy({},{get(_target,key){
  if(key==='SQL_RECEIPT_METER')return meter;
  const value=Reflect.get(env,key);
  return names.includes(key as any)&&value?wrap(value):value;
 },set(_target,key,value){return key!=='SQL_RECEIPT_METER'&&Reflect.set(env,key,value);},
 defineProperty(){return false;},preventExtensions(){return false;},
 deleteProperty(_target,key){return key!=='SQL_RECEIPT_METER'&&Reflect.deleteProperty(env,key);},
 has(_target,key){return key==='SQL_RECEIPT_METER'||key in env;},
 ownKeys(){return [...new Set([...Reflect.ownKeys(env),'SQL_RECEIPT_METER'])];},
 getOwnPropertyDescriptor(_target,key){
  if(key==='SQL_RECEIPT_METER')return{configurable:true,enumerable:true,value:meter};
  const d=Reflect.getOwnPropertyDescriptor(env,key);return d?{configurable:true,enumerable:d.enumerable,value:Reflect.get(env,key),writable:true}:undefined;
 }}) as T&{SQL_RECEIPT_METER:SqlReceiptMeter};
}
