import {test}from'node:test';
import assert from'node:assert/strict';
import {SqlReceiptMeter,parseSqlReceipt,SQL_RECEIPT_HEADER,SQL_PROBE_HEADER,wrapSqlNamespaces}from'../src/sql-receipt';
const make=(options:{hibernates_after_handler?:boolean;cpu_upper_ms?:number}={})=>{let observer:(r:number,w:number,v:boolean)=>void=()=>{},guard:()=>void=()=>{};const values={rows_read:0,rows_written:0,alarm_operations:0,measurement_valid:true};
 const totals=new Proxy(values,{set(t,k,v){guard();const old=(t as any)[k];(t as any)[k]=v;if(k==='rows_read')observer(v-old,0,true);if(k==='rows_written'||k==='alarm_operations')observer(0,v-old,true);if(k==='measurement_valid'&&!v)observer(0,0,false);return true;}});
 return{totals,meter:new SqlReceiptMeter({snapshot:()=>({...values}),observeRows:(f:any,g:any,ready:any)=>{observer=f;guard=g;ready?.();}}as any,options)};};

test('trusted receipts count constructor, own and nested work without inventing zero',async()=>{
 const{totals,meter}=make();totals.rows_read=3;totals.rows_written=2;
 const r=await meter.measure(async()=>{totals.rows_read+=4;meter.observe(new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:7,writes:5})}}));return Response.json({ok:true});});
 assert.deepEqual({...parseSqlReceipt(r.headers.get(SQL_RECEIPT_HEADER)),operation_id:undefined},{v:1,valid:true,reads:14,writes:7,operation_id:undefined});
 const next=await meter.measure(async()=>{totals.rows_written++;return Response.json({ok:true});});
 assert.deepEqual({...parseSqlReceipt(next.headers.get(SQL_RECEIPT_HEADER)),operation_id:undefined},{v:1,valid:true,reads:0,writes:1,operation_id:undefined,do_requests:1});
});
test('missing malformed observations fail closed while independent overlaps remain isolated',async()=>{
 for(const raw of [null,'{}','{"v":1,"valid":true,"reads":-1,"writes":0}','{"v":1,"valid":true,"reads":1e99,"writes":0}']){
  const{meter}=make(),p=meter.checkpoint();meter.observe(new Response(null,{headers:raw?{[SQL_RECEIPT_HEADER]:raw}:{}}));assert.equal(meter.finish(p).valid,false);
 }
 const{meter}=make();let release!:()=>void;const gate=new Promise<void>(r=>release=r);
 const first=meter.measure(async()=>{await gate;return new Response();});
 const second=await meter.measure(async()=>new Response());release();const done=await first;
 assert.equal(parseSqlReceipt(second.headers.get(SQL_RECEIPT_HEADER))?.valid,true);assert.equal(parseSqlReceipt(done.headers.get(SQL_RECEIPT_HEADER))?.valid,true);
 const failed=await meter.measure(async()=>new Response(null,{status:503}));assert.equal(parseSqlReceipt(failed.headers.get(SQL_RECEIPT_HEADER))?.valid,true,'a domain HTTP error does not invalidate measured cursor rows');
});
test('namespace adapters use trusted response receipts and preserve invocation binding',async()=>{
 const{meter}=make();const ns={idFromName:(x:string)=>x,get(){return{fetch:async(r:Request)=>new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:2,writes:3,operation_id:r.headers.get(SQL_PROBE_HEADER)})}})}}};
 const env=wrapSqlNamespaces({CONTROL:ns},meter),p=meter.checkpoint();await env.CONTROL.get().fetch(new Request('https://control/'));
 assert.deepEqual(meter.finish(p),{v:1,valid:true,reads:2,writes:3});
});

test('stale request nonce never certifies current SQL',()=>{
 const {meter}=make(),p=meter.checkpoint();
 meter.observe(new Response(null,{headers:{[SQL_RECEIPT_HEADER]:'{"v":1,"valid":true,"reads":0,"writes":0,"operation_id":"stale"}'}}),'current');
 assert.equal(meter.finish(p).valid,false);
});
test('unawaited pending child cannot certify unused SQL before its receipt arrives',async()=>{
 const{meter}=make();let release!:(r:Response)=>void;
 const pending=new Promise<Response>(r=>release=r);
 let operationId='';const env=wrapSqlNamespaces({CONTROL:{get(){return{fetch:async(r:Request)=>{operationId=r.headers.get(SQL_PROBE_HEADER)!;return pending;}}}}},meter);
 let child!:Promise<Response>;
 const response=await meter.measure(async()=>{child=env.CONTROL.get().fetch(new Request('https://control/'));return new Response();});
 assert.equal(parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER)),undefined);
 const checkpoint=meter.checkpoint();
 release(new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:4,writes:5,operation_id:operationId})}}));await child;
 assert.deepEqual(meter.finish(checkpoint),{v:1,valid:true,reads:0,writes:0});
});

test('alarm operations retain a conservative write allowance beyond cursor rows',async()=>{
 const{totals,meter}=make();
 const response=await meter.measure(async()=>{totals.rows_written=2;totals.alarm_operations=3;return new Response();});
 assert.equal(parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER))!.writes,5);
});

test('only proved durably funded nested rows are excluded from a parent interval',async()=>{
 const{totals,meter}=make();const outer=meter.checkpoint();
 totals.rows_read=2;totals.rows_written=1;const child=meter.checkpoint();
 totals.rows_read+=8;totals.rows_written+=5;
 assert.equal(meter.account(child,{reads:8,writes:5}),true);
 assert.deepEqual(meter.finish(outer),{v:1,valid:true,reads:2,writes:1});
 assert.equal(meter.account(child,{reads:8,writes:5}),true);
 assert.deepEqual(meter.finish(outer),{v:1,valid:true,reads:2,writes:1});
 const response=await meter.measure(async()=>new Response());
 const receipt=parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER))!;
 assert.equal(receipt.reads,2);assert.equal(receipt.writes,1);
});
test('underfunded or ambiguous nested accounting cannot release parent funding',()=>{
 const{totals,meter}=make(),outer=meter.checkpoint(),child=meter.checkpoint();
 totals.rows_written=6;
 assert.equal(meter.account(child,{reads:0,writes:5}),false);
 assert.equal(meter.finish(outer).valid,false);
 const other=make(),pending=other.meter.checkpoint();other.meter.failed();
 assert.equal(other.meter.account(pending,{reads:100,writes:100}),false);
});

test('replaying a funded checkpoint cannot credit new unrelated SQL or cross meters',()=>{
 const{totals,meter}=make(),outer=meter.checkpoint(),child=meter.checkpoint();
 totals.rows_read=8;totals.rows_written=5;
 assert.equal(meter.account(child,{reads:8,writes:5}),true);
 totals.rows_read++;totals.rows_written++;
 assert.equal(meter.account(child,{reads:8,writes:5}),true);
 assert.deepEqual(meter.finish(outer),{v:1,valid:true,reads:1,writes:1});
 const other=make();
 assert.equal(other.meter.account(child,{reads:8,writes:5}),false);
 assert.equal(meter.account(child,{reads:9,writes:6}),false);
});

test('wrapping preserves live scalar rotation and late/replaced namespace bindings',async()=>{
 const{meter}=make();const original:any={KEY:'before'},env:any=wrapSqlNamespaces(original,meter);
 original.KEY='after';assert.equal(env.KEY,'after');assert.equal({...env}.KEY,'after');
 let operation='';original.CONTROL={get(){return{fetch:async(r:Request)=>{operation=r.headers.get(SQL_PROBE_HEADER)!;return new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,operation_id:operation,reads:3,writes:1})}});}}}};
 const p=meter.checkpoint();await env.CONTROL.get().fetch('https://control/');
 assert.equal(meter.finish(p).reads,3);assert(operation);
 assert.equal({...env}.SQL_RECEIPT_METER,meter);
});

test('structural proxy mutation fails without poisoning future live bindings',()=>{
 const{meter}=make(),source:any={KEY:1},env:any=wrapSqlNamespaces(source,meter);
 assert.throws(()=>Object.defineProperty(env,'EXTRA',{value:1}),TypeError);
 assert.equal(Reflect.preventExtensions(env),false);
 assert.equal({...env}.KEY,1);
 assert.equal(delete env.KEY,true);assert.equal('KEY' in source,false);
 source.NEXT=2;assert.equal({...env}.NEXT,2);
 assert.equal(Reflect.set(env,'SQL_RECEIPT_METER',null),false);
});

test('an alarm scope includes unassigned prelude and subtracts only funded nested work',async()=>{
 const{totals,meter}=make();
 await meter.measure(async()=>{totals.rows_read=2;totals.rows_written=1;return new Response();});
 totals.rows_written+=4;const alarm=meter.checkpointUnassigned();
 totals.rows_read+=3;totals.rows_written+=2;
 assert.deepEqual(meter.finish(alarm),{v:1,valid:true,reads:3,writes:6});
 assert.equal(meter.account(alarm,{reads:3,writes:6}),true);
 totals.rows_written++;
 const next=meter.checkpointUnassigned();
 assert.deepEqual(meter.finish(next),{v:1,valid:true,reads:0,writes:1});
});

test('overlapping HTTP and WebSocket async scopes own only their native rows',async()=>{
 const{totals,meter}=make();let release!:()=>void;
 const gate=new Promise<void>(r=>release=r);
 const first=meter.measure(async()=>{totals.rows_read+=3;await gate;totals.rows_written+=5;return new Response();});
 const second=await meter.measure(async()=>{totals.rows_read+=7;await Promise.resolve();totals.rows_written+=11;return new Response();});
 release();const done=await first;
 assert.deepEqual({...parseSqlReceipt(done.headers.get(SQL_RECEIPT_HEADER)),operation_id:undefined},{v:1,valid:true,reads:3,writes:5,operation_id:undefined,do_requests:1});
 assert.deepEqual({...parseSqlReceipt(second.headers.get(SQL_RECEIPT_HEADER)),operation_id:undefined},{v:1,valid:true,reads:7,writes:11,operation_id:undefined,do_requests:1});
});
test('nested settled work credits its own scope without subtracting another request',async()=>{
 const{totals,meter}=make();let release!:()=>void;const gate=new Promise<void>(r=>release=r);
 const first=meter.measure(async()=>{const p=meter.checkpoint();totals.rows_written+=5;await gate;assert(meter.account(p,{reads:0,writes:5}));return new Response();});
 const second=await meter.measure(async()=>{totals.rows_written+=9;return new Response();});release();
 assert.equal(parseSqlReceipt((await first).headers.get(SQL_RECEIPT_HEADER))!.writes,0);
 assert.equal(parseSqlReceipt(second.headers.get(SQL_RECEIPT_HEADER))!.writes,9);
});

test('detached native SQL is refused before effect after its request closes',async()=>{
 const{totals,meter}=make();let release!:()=>void;
 const gate=new Promise<void>(r=>release=r);let detached!:Promise<void>;
 const first=await meter.measure(async()=>{detached=(async()=>{await gate;totals.rows_written+=17;})();return new Response();});
 assert.equal(parseSqlReceipt(first.headers.get(SQL_RECEIPT_HEADER))!.writes,0);
 release();await assert.rejects(detached,/sql_scope_closed/);assert.equal(totals.rows_written,0);
 const next=await meter.measure(async()=>{totals.rows_written++;return new Response();});
 assert.equal(parseSqlReceipt(next.headers.get(SQL_RECEIPT_HEADER))!.writes,1);
});
test('a live checkpoint cannot be settled by another active request scope',async()=>{
 const{totals,meter}=make();let release!:()=>void,point:any;
 const gate=new Promise<void>(r=>release=r);
 const first=meter.measure(async()=>{point=meter.checkpoint();totals.rows_written+=5;await gate;return new Response();});
 const second=await meter.measure(async()=>{assert.equal(meter.account(point,{reads:0,writes:5}),false);return new Response();});
 assert.equal(parseSqlReceipt(second.headers.get(SQL_RECEIPT_HEADER)),undefined);
 release();assert.equal(parseSqlReceipt((await first).headers.get(SQL_RECEIPT_HEADER))!.writes,5);
});

test('fresh namespace receipts include target plus downstream calls without double charging target',async()=>{
 const{meter}=make({hibernates_after_handler:true,cpu_upper_ms:500});
 const env=wrapSqlNamespaces({CONTROL:{get(){return{fetch:async(r:Request)=>new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:0,writes:0,do_requests:3,do_gb_ms:12,operation_id:r.headers.get(SQL_PROBE_HEADER)})}})}}}},meter);
 const response=await meter.measure(async()=>{await env.CONTROL.get().fetch(new Request('https://control/'));return new Response();});
 const receipt=parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER))!;
 assert.equal(receipt.do_requests,4,'one own target + three child/downstream requests');
 assert(receipt.do_gb_ms!>=12,'own wall interval and child duration remain funded');
});
test('unknown duration keeps its envelope while independently known request counts remain usable',async()=>{
 const{meter}=make();
 const response=await meter.measure(async()=>new Response());
 const receipt=parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER))!;
 assert.equal(receipt.do_requests,1);assert.equal(receipt.do_gb_ms,undefined);
});
test('duration and request credits require same-scope durable bounds and replay cannot hide later calls',async()=>{
 const meter=new SqlReceiptMeter();
 await meter.withScope(async()=>{
  const outer=meter.checkpoint(),point=meter.checkpoint();
  meter.beginChild();meter.observe(new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:2,writes:3,do_requests:2,do_gb_ms:12})}}));meter.endChild();
  assert(meter.account(point,{reads:2,writes:3,do_requests:2,do_gb_ms:12}));
  meter.beginChild();meter.observe(new Response(null,{headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:true,reads:1,writes:1,do_requests:1,do_gb_ms:4})}}));meter.endChild();
  assert(meter.account(point,{reads:2,writes:3,do_requests:2,do_gb_ms:12}));
  assert.deepEqual(meter.usage(outer),{do_requests:1,do_gb_ms:4});
  assert.equal(meter.account(point,{reads:2,writes:3,do_requests:3,do_gb_ms:12}),false);
 });
});

test('clock-only duration cannot replace an envelope without an enforced CPU upper bound',async()=>{
 const{meter}=make({hibernates_after_handler:true});
 const response=await meter.measure(async()=>new Response());
 assert.equal(parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER))!.do_gb_ms,undefined);
});
test('frozen I/O clocks still fund constructor and handler CPU tails separately',async()=>{
 const original=globalThis.performance;
 try{
  Object.defineProperty(globalThis,'performance',{configurable:true,value:{now:()=>0}});
  const{meter}=make({hibernates_after_handler:true,cpu_upper_ms:500});
  const first=parseSqlReceipt((await meter.measure(async()=>new Response())).headers.get(SQL_RECEIPT_HEADER))!;
  const next=parseSqlReceipt((await meter.measure(async()=>new Response())).headers.get(SQL_RECEIPT_HEADER))!;
  assert.equal(first.do_gb_ms,250);assert.equal(next.do_gb_ms,125);
 }finally{Object.defineProperty(globalThis,'performance',{configurable:true,value:original});}
});

test('fresh alarm and WS scopes retain configured duration even with an audited HTTP CPU bound',async()=>{
 const{meter}=make({hibernates_after_handler:true,cpu_upper_ms:500});
 await meter.withScope(async()=>{const point=meter.checkpointUnassigned();assert.equal(meter.usage(point).do_gb_ms,undefined);assert.equal(meter.usage(point).do_requests,1);});
});
