// Actual Worker and BudgetDO; synthetic financial bounds/auth/receiver, never production sizing.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {resolve} from 'node:path';
import {canonicalSizingEnvelopes} from './canonical-cost-envelope.mjs';
const root=resolve(import.meta.dirname,'../..'),hub=resolve(root,'hub2');
const splitTestEnvelope=canonicalSizingEnvelopes();
const names=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
const units=Object.fromEntries(names.map(k=>[k,0]));
Object.assign(units,{worker_requests:1,worker_cpu_ms:100,do_requests:12,do_gb_ms:1000,sqlite_rows_read:1000,sqlite_rows_written:40000,sqlite_byte_days:1});
const safety={...units,sqlite_rows_written:100,sqlite_rows_read:100,do_gb_ms:100};
const plan={version:1,normal:{units,max_do_calls:8,max_wall_ms:10000,provenance:'Synthetic financial threshold fixture, not production sizing'},safety:{units:safety,max_do_calls:8,max_wall_ms:10000,provenance:'Synthetic recovery financial fixture'}};
const entry=`
import worker from './src/worker';
import {BudgetDO as ActualBudgetDO} from './src/budget-do';
import {SqlReceiptMeter,SQL_PROBE_HEADER,wrapSqlNamespaces} from './src/sql-receipt';
import {budgetedBackgroundRequest,budgetedPublicRequest,dispatchBudgetAlerts} from './src/budget-edge';
export class BudgetDO extends ActualBudgetDO{
 async fetch(r){if(new URL(r.url).pathname==='/_fixture/reservations'){
  await super.fetch(new Request('https://budget/_metrics'));
  return Response.json(this.db.all('SELECT id,state,estimate_pusd,actual_pusd FROM budget_reservations ORDER BY id'));
 }return super.fetch(r);}
}
export class Probe {
 constructor(ctx){this.sql=ctx.storage.sql;this.rowsRead=0;this.rowsWritten=0;this.exec('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,effects INTEGER NOT NULL,verifies INTEGER NOT NULL)');this.exec('CREATE TABLE IF NOT EXISTS padding(id INTEGER PRIMARY KEY)');this.exec('INSERT OR IGNORE INTO calls VALUES(1,0,0)');this.receipts=new SqlReceiptMeter({observeRows:(f,g)=>{this.rowObserver=f;this.rowGuard=g;},snapshot:()=>({rows_read:this.rowsRead,rows_written:this.rowsWritten,alarm_operations:0,measurement_valid:true})});}
 exec(query,...bindings){this.rowGuard?.();const cursor=this.sql.exec(query,...bindings),rows=cursor.toArray();this.rowsRead+=cursor.rowsRead;this.rowsWritten+=cursor.rowsWritten;this.rowObserver?.(cursor.rowsRead,cursor.rowsWritten,true);return rows;}
 fetch(r){return this.receipts.measure(()=>this.handle(r),r.headers.get(SQL_PROBE_HEADER));}
 async handle(r){const p=new URL(r.url).pathname;if(p==='/_concurrent'){let reads=0,writes=0;const execute=()=>{this.rowGuard?.();const c=this.sql.exec('UPDATE calls SET effects=effects+1 WHERE id=1');c.toArray();reads+=c.rowsRead;writes+=c.rowsWritten;this.rowObserver(c.rowsRead,c.rowsWritten,true);};execute();await new Promise(resolve=>setTimeout(resolve,Number(new URL(r.url).searchParams.get('delay')||0)));execute();return Response.json({reads,writes});}
 if(p==='/_stats')return Response.json(this.exec('SELECT * FROM calls')[0]);
 if(p==='/_verify'){this.exec('UPDATE calls SET verifies=verifies+1');const b=await r.json();return b.token==='synthetic-fixture-token-0000000000000000'?Response.json({actor:{id:'human:fixture',kind:'operator',scopes:['*']}}):Response.json({error:'unauthorized'},{status:401});}
 if(r.method==='POST'){this.exec('UPDATE calls SET effects=effects+1');const body=await r.clone().json().catch(()=>({}));if(body?.name==='fixture-a'||body?.name==='fixture-b'){const base=body.name==='fixture-a'?0:30000;for(let offset=0;offset<30000;offset+=1000){const rows=Array.from({length:1000},(_,i)=>'('+(base+offset+i+1)+')').join(',');this.exec('INSERT INTO padding(id) VALUES '+rows);}}}return Response.json({items:[],effect:true});}
}
export default {async fetch(r,env){const u=new URL(r.url);
 if(u.pathname==='/_fixture/concurrent'){const stub=env.CONTROL.get(env.CONTROL.idFromName('concurrent'));await stub.fetch('https://probe/_stats');const results=await Promise.all([40,0].map(async delay=>{const r=await stub.fetch('https://probe/_concurrent?delay='+delay),receipt=JSON.parse(r.headers.get('x-hub2-sql-receipt')),body=await r.json();return{receipt,body};}));return Response.json({results});}
 if(u.pathname==='/_fixture/overrun'){
  const stub=env.BUDGET.get(env.BUDGET.idFromName('global')),names=${JSON.stringify(names)},units=Object.fromEntries(names.map(k=>[k,0])),coverage=Object.fromEntries(names.map(k=>[k,'exact']));
  units.worker_requests=1;
  const post=(path,body)=>stub.fetch('https://budget'+path,{method:'POST',body:JSON.stringify(body)});
  await post('/_internal/reserve',{id:'fixture-overrun',kind:'public_request',estimated_units:units,coverage});
  units.sqlite_rows_written=150000;
  return post('/_internal/settle',{id:'fixture-overrun',actual_units:units,coverage});
 }
 if(u.pathname==='/_fixture/unknown-health')return env.BUDGET.get(env.BUDGET.idFromName('global')).fetch('https://budget/_internal/reserve',{method:'POST',body:JSON.stringify({id:'unknown-fixture',kind:'public_request',estimated_units:{},coverage:{}})});
 if(u.pathname==='/_fixture/stats')return env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://probe/_stats');
 if(u.pathname==='/_fixture/sink')return fetch('https://fixture-alert.test/_stats');
 if(u.pathname==='/_fixture/dispatch')return Response.json(await dispatchBudgetAlerts(env));
 if(u.pathname==='/_fixture/scheduled'){await worker.scheduled({},env);return Response.json({invoked:true});}
 if(u.pathname==='/_fixture/unknown')return worker.fetch(new Request('https://public/api/repos',r),{...env,HUB2_BUDGET_ENVELOPES:'{}'});
 if(u.pathname==='/_fixture/split-envelope'){
  const raw=env.HUB2_SPLIT_TEST_ENVELOPE,mid=Math.floor(raw.length/2),original=env.BUDGET;
  const encoder=new TextEncoder();if(encoder.encode(raw.slice(0,mid)).length>5000||encoder.encode(raw.slice(mid)).length>5000)throw Error('budget envelope chunks exceed 5KB');
  const make=(tail)=>({...env,HUB2_BUDGET_ENVELOPES:raw.slice(0,mid),...(tail?{HUB2_BUDGET_ENVELOPES_PART2:raw.slice(mid)}:{}),BUDGET:{idFromName:()=>original.idFromName('split-envelope'),get:()=>original.get(original.idFromName('split-envelope'))}});
  let handled=false;
  const valid=await budgetedPublicRequest(new Request('https://public/api/repos'),make(true),async()=>undefined,async()=>{handled=true;return Response.json({ok:true});});
  let missingHandled=false;
  const missing=await budgetedPublicRequest(new Request('https://public/api/repos'),make(false),async()=>undefined,async()=>{missingHandled=true;return Response.json({ok:true});});
  return Response.json({valid:valid.status,handled,missing:missing.status,missing_error:await missing.json().then(v=>v.error),missing_handled:missingHandled});
 }
 if(u.pathname==='/_fixture/invalid-kind-plan'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES),base={...p.normal};p.normal.by_kind={not_a_budget_kind:base};
  return worker.fetch(new Request('https://public/api/repos',r),{...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p)});
 }
 if(u.pathname==='/_fixture/placement-kind'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES);p.normal.by_kind={public_request:{...p.normal,max_do_calls:1},placement_request:{...p.normal,max_do_calls:2}};
  const original=env.BUDGET,fixtureEnv={...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p),BUDGET:{idFromName:()=>original.idFromName('placement-kind'),get:()=>original.get(original.idFromName('placement-kind'))}};
  return budgetedPublicRequest(new Request('https://public/api/intakes/fixture-id/start',{method:'POST'}),fixtureEnv,async()=>undefined,async guarded=>{
   await guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/one');
   await guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/two');
   return Response.json({placed:true});
  });
 }
 if(u.pathname==='/_fixture/connection-kind'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES);p.normal.by_kind={public_request:{...p.normal,max_do_calls:1},connection_request:{...p.normal,max_do_calls:2}};
  const original=env.BUDGET,fixtureEnv={...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p),BUDGET:{idFromName:()=>original.idFromName('connection-kind'),get:()=>original.get(original.idFromName('connection-kind'))}};
  return budgetedPublicRequest(new Request('https://public/api/connect',{method:'GET'}),fixtureEnv,async()=>undefined,async guarded=>{
   await guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/one');
   await guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/two');
   return Response.json({connected:true});
  });
 }
 if(u.pathname==='/_fixture/safety-prelude'){
  const budget=env.BUDGET.get(env.BUDGET.idFromName('safety-prelude')),control=env.CONTROL.get(env.CONTROL.idFromName('safety-prelude'));
  let preauth,reserve;
  const fixtureEnv={...env,CONTROL:{idFromName:()=>null,get:()=>control},BUDGET:{idFromName:()=>null,get:()=>({fetch:async(...args)=>{const reserveCall=String(args[0]?.url||args[0]).endsWith('/_internal/reserve');const response=await budget.fetch(...args);if(reserveCall)reserve=JSON.parse(response.headers.get('x-hub2-sql-receipt'));return response;}})}};
  const response=await budgetedPublicRequest(new Request('https://public/api/budget'),fixtureEnv,async(request,guarded)=>{
   const proof=await guarded.CONTROL.get(null).fetch('https://probe/_verify',{method:'POST',body:JSON.stringify({token:'synthetic-fixture-token-0000000000000000'})});
   preauth=JSON.parse(proof.headers.get('x-hub2-sql-receipt'));return(await proof.json()).actor;
  },async()=>Response.json({done:true}));
  const status=await budget.fetch('https://budget/api/budget',{headers:{'x-hub2-actor':JSON.stringify({id:'human:fixture',kind:'operator',scopes:['*']})}});
  return Response.json({status:response.status,body:await response.json(),preauth,reserve,budget:await status.json()});
 }
 if(u.pathname==='/_fixture/selected-kind'){
  const b=await r.json(),p=JSON.parse(env.HUB2_BUDGET_ENVELOPES),base={...p.normal};delete base.by_kind;
  const kinds=['auth_request','public_read','public_write','resource_heartbeat','fleet_read','project_request','claim_request','message_request','account_request','provider_refresh'];
  const by_kind=Object.fromEntries(kinds.map(k=>[k,{...base,max_do_calls:k===b.expected?2:1}]));
  p.normal={...base,max_do_calls:1,by_kind};const original=env.BUDGET,id='selected-kind-'+b.expected;
  const fixtureEnv={...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p),BUDGET:{idFromName:()=>original.idFromName(id),get:()=>original.get(original.idFromName(id))}};
  return budgetedPublicRequest(new Request('https://public'+b.path,{method:b.method}),fixtureEnv,async()=>undefined,async guarded=>{
   for(let i=0;i<2;i++){const response=await guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/'+i);if(!response.ok)return response;}
   return Response.json({selected:b.expected});
  });
 }
 if(u.pathname==='/_fixture/origin-cost'){
  const mode=u.searchParams.get('mode'),original=env.BUDGET,id='origin-'+mode,raw={...env,BUDGET:{idFromName:()=>original.idFromName(id),get:()=>original.get(original.idFromName(id))}};
  const scoped=mode==='nested'?wrapSqlNamespaces(raw,new SqlReceiptMeter()):raw;
  const run=async()=>Response.json({done:true});
  const response=mode==='worker'?await budgetedPublicRequest(new Request('https://public/health'),scoped,async()=>undefined,run):await budgetedBackgroundRequest(scoped,'background_effect',run,mode==='cron');
  const budget=await(await original.get(original.idFromName(id)).fetch('https://budget/api/budget',{headers:{'x-hub2-actor':JSON.stringify({id:'operator:fixture',kind:'operator',scopes:['budget:read']})}})).json();
  return Response.json({status:response.status,units:budget.units});
 }
 if(u.pathname==='/_fixture/sql-accounting'){
  const original=env.BUDGET,id='sql-accounting';
  const fixtureEnv={...env,BUDGET:{idFromName:()=>original.idFromName(id),get:()=>original.get(original.idFromName(id))}};
  const response=await budgetedPublicRequest(new Request('https://public/health'),fixtureEnv,async()=>undefined,async guarded=>
   guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/_stats'));
  const statusResponse=await original.get(original.idFromName(id)).fetch('https://budget/api/budget',{headers:{'x-hub2-actor':JSON.stringify({id:'operator:fixture',kind:'operator',scopes:['budget:read']})}});
  return Response.json({status:response.status,receipt_leaked:response.headers.has('x-hub2-sql-receipt'),probe_leaked:response.headers.has('x-hub2-sql-operation'),budget:await statusResponse.json()});
 }
 if(u.pathname==='/_fixture/sql-missing'){
  const original=env.BUDGET,status=u.searchParams.get('status')==='403'?403:200,id='sql-missing-'+status,meter=new SqlReceiptMeter();let accountCalls=0;
  const account=meter.account.bind(meter);meter.account=(...args)=>{accountCalls++;return account(...args);};
  const fixtureEnv=wrapSqlNamespaces({...env,BUDGET:{idFromName:()=>original.idFromName(id),get:()=>original.get(original.idFromName(id))},CONTROL:{idFromName:()=> 'missing',get:()=>({fetch:async()=>Response.json({unmetered:true},{status})})}},meter);
  const response=await budgetedPublicRequest(new Request('https://public/health'),fixtureEnv,async()=>undefined,async guarded=>
   guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/missing'));
  const stub=original.get(original.idFromName(id));
  const statusResponse=await stub.fetch('https://budget/api/budget',{headers:{'x-hub2-actor':JSON.stringify({id:'operator:fixture',kind:'operator',scopes:['budget:read']})}});
  const rows=await(await stub.fetch('https://budget/_fixture/reservations')).json();
  return Response.json({status:response.status,body:await response.json(),budget:await statusResponse.json(),reservations:rows,account_calls:accountCalls});
 }
 if(u.pathname==='/_fixture/sql-settle-failure'){
  const original=env.BUDGET,id='sql-settle-failure',meter=new SqlReceiptMeter();let accountCalls=0;
  const account=meter.account.bind(meter);meter.account=(...args)=>{accountCalls++;return account(...args);};
  const budget={idFromName:()=>original.idFromName(id),get:()=>{const stub=original.get(original.idFromName(id));return new Proxy(stub,{get(target,key){if(key==='fetch')return async(input,init)=>{const url=new URL(typeof input==='string'?input:input.url);if(url.pathname==='/_internal/settle'){if(u.searchParams.get('timeout'))return new Promise((_resolve,reject)=>{const signal=input instanceof Request?input.signal:init?.signal;if(signal?.aborted)reject(new Error('aborted'));else signal?.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});});return new Response(null,{status:503});}return target.fetch(input,init);};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});}};
  const fixtureEnv=wrapSqlNamespaces({...env,BUDGET:budget},meter);
  const response=await budgetedBackgroundRequest(fixtureEnv,'public_read',async guarded=>guarded.CONTROL.get(guarded.CONTROL.idFromName('global')).fetch('https://probe/_stats'));
  const statusResponse=await original.get(original.idFromName(id)).fetch('https://budget/api/budget',{headers:{'x-hub2-actor':JSON.stringify({id:'operator:fixture',kind:'operator',scopes:['budget:read']})}});
  return Response.json({status:response.status,account_calls:accountCalls,budget:await statusResponse.json()});
 }
 if(u.pathname==='/_fixture/reservation-latency'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES),base={...p.normal};delete base.by_kind;
  p.normal={...base,by_kind:{public_read:{...base,max_wall_ms:250}}};
  const original=env.BUDGET,id='reservation-latency',raw=original.get(original.idFromName(id));
  const delayed= new Proxy(raw,{get(target,key){
   if(key==='fetch')return async(input,init)=>{
    const url=new URL(typeof input==='string'?input:input.url);
    if(url.pathname==='/_internal/reserve')await new Promise(resolve=>setTimeout(resolve,350));
    return target.fetch(input,init);
   };
   const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
  }});
  const fixtureEnv={...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p),BUDGET:{idFromName:()=>original.idFromName(id),get:()=>delayed}};
  const response=await budgetedPublicRequest(new Request('https://public/health'),fixtureEnv,async()=>undefined,async()=>Response.json({bounded:true}));
  return Response.json({status:response.status,body:await response.json()});
 }
 if(u.pathname==='/_fixture/deny-before-fanout'){
  const original=env.BUDGET,id='deny-before-fanout',stub=original.get(original.idFromName(id));
  const reserveUnits=Object.fromEntries(${JSON.stringify(names)}.map(k=>[k,0])),coverage=Object.fromEntries(${JSON.stringify(names)}.map(k=>[k,'exact']));
  reserveUnits.worker_requests=1;
  const post=(path,body)=>stub.fetch('https://budget'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  await post('/_internal/reserve',{id:'synthetic-overrun',kind:'test',estimated_units:reserveUnits,coverage});
  const actual={...reserveUnits,sqlite_rows_written:150000};
  await post('/_internal/settle',{id:'synthetic-overrun',actual_units:actual,coverage});
  const blockedEnv={...env,BUDGET:{idFromName:()=>original.idFromName(id),get:()=>original.get(original.idFromName(id))}};
  const before=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://probe/_stats');
  const blocked=await worker.fetch(new Request('https://public/api/repos',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"name":"must-not-run"}'}),blockedEnv);
  const after=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://probe/_stats');
  return Response.json({status:blocked.status,body:await blocked.json(),retry_after:blocked.headers.get('Retry-After'),before:await before.json(),after:await after.json()});
 }
 if(u.pathname==='/_fixture/fanout'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES);p.normal.by_kind={public_request:{...p.normal,max_do_calls:1},public_write:{...p.normal,max_do_calls:1}};const original=env.BUDGET;
  return worker.fetch(new Request('https://public/api/repos',r),{...env,HUB2_BUDGET_ENVELOPES:JSON.stringify(p),BUDGET:{idFromName:original.idFromName.bind(original),get:()=>original.get(original.idFromName('fanout'))}});
 }
 return worker.fetch(r,env);
}};
`;
const sink=`
export class Sink {
 constructor(ctx){this.sql=ctx.storage.sql;this.sql.exec('CREATE TABLE IF NOT EXISTS delivered(id TEXT PRIMARY KEY,attempts INTEGER NOT NULL)');}
 async fetch(r){if(new URL(r.url).pathname==='/_stats')return Response.json({items:this.sql.exec('SELECT * FROM delivered ORDER BY id').toArray()});
 if(r.headers.get('Authorization')!=='Bearer synthetic-alert-secret')return new Response(null,{status:403});
 const id=r.headers.get('Idempotency-Key'),b=await r.json();if(!id||b.message_id!==id||!b.alert?.id||!b.from||!b.to||!b.text)return new Response(null,{status:400});
 const prior=this.sql.exec('SELECT * FROM delivered WHERE id=?',id).toArray()[0];
 if(!prior){this.sql.exec('INSERT INTO delivered VALUES(?,1)',id);return new Response(null,{status:503});}
 this.sql.exec('UPDATE delivered SET attempts=attempts+1 WHERE id=?',id);return Response.json({delivered:true,id});
 }
}
export default {fetch(r,env){return env.SINK.get(env.SINK.idFromName('global')).fetch(r);}};
`;
const bundles=await Promise.all([entry,sink].map(contents=>build({stdin:{contents,resolveDir:hub,loader:'ts'},bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers','node:async_hooks']})));
await mkdir(resolve(root,'.local/tmp'),{recursive:true});const persist=await mkdtemp(resolve(root,'.local/tmp/hub2-public-budget-'));
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[
{name:'public-budget-fixture',script:bundles[0].outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_als'],outboundService:'alert-sink',durableObjects:{BUDGET:{className:'BudgetDO',useSQLite:true},CONTROL:{className:'Probe',useSQLite:true},PROJECTS:{className:'Probe',useSQLite:true},MACHINES:{className:'Probe',useSQLite:true},ACCOUNTS:{className:'Probe',useSQLite:true},MAILBOXES:{className:'Probe',useSQLite:true}},bindings:{HUB2_BUDGET_ENVELOPES:JSON.stringify(plan),HUB2_SPLIT_TEST_ENVELOPE:splitTestEnvelope,HUB_ALERT_WEBHOOK:'https://fixture-alert.test/deliver',HUB_ALERT_WEBHOOK_TOKEN:'synthetic-alert-secret'}},
{name:'alert-sink',script:bundles[1].outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_als'],durableObjects:{SINK:{className:'Sink',useSQLite:true}}}]});
let mf=new Miniflare(options),base='';
const call=async(path,method='GET',body)=>{const r=await fetch(base+path,{method,headers:{'Content-Type':'application/json',Authorization:'Bearer synthetic-fixture-token-0000000000000000'},...(body===undefined?{}:{body:JSON.stringify(body)})});let data;try{data=await r.json();}catch{data={};}return {status:r.status,data,headers:r.headers};};
try{
 base=(await mf.ready).origin;
 const delayedReservation=await call('/_fixture/reservation-latency');
 assert.equal(delayedReservation.status,200);assert.equal(delayedReservation.data.status,200,'pre-effect reservation latency must not consume the bounded effect wall window');
 assert.equal(delayedReservation.data.body.bounded,true);
 const testCeiling=await call('/api/budget/override','POST',{day:new Date().toISOString().slice(0,10),ceiling_pusd:'100000000000',reason:'Synthetic lower ceiling for cutoff and recovery tests'});
 assert.equal(testCeiling.status,200,JSON.stringify(testCeiling.data));
 const concurrent=await call('/_fixture/concurrent');
 assert.equal(concurrent.status,200);
 for(const {receipt,body}of concurrent.data.results){assert.equal(receipt.valid,true);assert.equal(receipt.reads,body.reads);assert.equal(receipt.writes,body.writes);}

 assert.equal((await call('/api/repos','POST',{name:'fixture-a'})).status,200);
 assert.equal((await call('/api/repos','POST',{name:'fixture-b'})).status,200);
 const blocked=await call('/_fixture/deny-before-fanout');assert.equal(blocked.status,200);assert.equal(blocked.data.status,429);assert.equal(blocked.data.body.error,'budget_paused');assert(Number(blocked.data.retry_after)>0);
 assert.deepEqual(blocked.data.after,blocked.data.before,'ceiling denies before authentication fanout');
 const unknown=await call('/_fixture/unknown','POST',{});assert.equal(unknown.status,503);assert.equal(unknown.data.error,'budget_metrics_unknown');
 const split=await call('/_fixture/split-envelope','POST',{});assert.equal(split.status,200);assert.equal(split.data.valid,200);assert.equal(split.data.handled,true);assert.equal(split.data.missing,503);assert.equal(split.data.missing_error,'budget_metrics_unknown');assert.equal(split.data.missing_handled,false,'missing envelope chunk refuses before effect');
 const beforeInvalid=(await call('/_fixture/stats')).data;
 const invalidKind=await call('/_fixture/invalid-kind-plan','POST',{});assert.equal(invalidKind.status,503);assert.equal(invalidKind.data.error,'budget_metrics_unknown','unknown per-kind envelope fails closed');
 assert.deepEqual((await call('/_fixture/stats')).data,beforeInvalid,'invalid per-kind envelope refuses before auth fanout');
 const placement=await call('/_fixture/placement-kind','POST',{});assert.equal(placement.status,200,JSON.stringify(placement.data));assert.equal(placement.data.placed,true,'session creation uses its higher fan-out envelope');
 const connected=await call('/_fixture/connection-kind','POST',{});assert.equal(connected.status,200,JSON.stringify(connected.data));assert.equal(connected.data.connected,true,'connection establishment uses its bounded handshake envelope');
 const safetyPrelude=await call('/_fixture/safety-prelude');assert.equal(safetyPrelude.data.status,200,JSON.stringify(safetyPrelude.data));
 assert(safetyPrelude.data.preauth.writes>0,'cold safety auth must execute real SQLite');
 assert.equal(Number(safetyPrelude.data.budget.units.sqlite_rows_read),safetyPrelude.data.preauth.reads+safetyPrelude.data.reserve.reads+64,'cold preauth+reserve+settle reads charged exactly once');
 assert.equal(Number(safetyPrelude.data.budget.units.sqlite_rows_written),safetyPrelude.data.preauth.writes+safetyPrelude.data.reserve.writes+8,'cold preauth+reserve+settle writes charged exactly once');
 const routeKinds=[['auth_request','GET','/device'],['auth_request','GET','/auth/login'],['auth_request','GET','/auth/callback'],['auth_request','POST','/api/device/code'],['public_read','GET','/health'],['public_write','POST','/api/repos'],['resource_heartbeat','POST','/api/heartbeat'],['fleet_read','GET','/api/fleet'],['project_request','GET','/api/intakes/t?project=p'],['claim_request','POST','/api/claims?project=p'],['message_request','POST','/api/messages'],['account_request','GET','/api/accounts/a/api/account/credentials'],['provider_refresh','GET','/api/accounts/a/api/account/quota']];
 for(const[expected,method,path]of routeKinds){const selected=await call('/_fixture/selected-kind','POST',{expected,method,path});assert.equal(selected.status,200,`${expected} must select its own request envelope: ${JSON.stringify(selected.data)}`);assert.equal(selected.data.selected,expected);}
 for(const mode of['worker','cron','nested']){
  const origin=await call('/_fixture/origin-cost?mode='+mode);assert.equal(origin.status,200);assert.equal(origin.data.status,200);
  assert.equal(Number(origin.data.units.worker_requests),mode==='worker'?1:0);
  assert.equal(Number(origin.data.units.worker_cpu_ms)>0,mode!=='nested');
  assert(Number(origin.data.units.do_requests)>0,'actual DO/SQLite dimensions remain priced in every origin');
  assert(Number(origin.data.units.sqlite_rows_written)>0);
 }
 const measuredSql=await call('/_fixture/sql-accounting');assert.equal(measuredSql.status,200);assert.equal(measuredSql.data.status,200);assert.equal(measuredSql.data.receipt_leaked,false);assert.equal(measuredSql.data.probe_leaked,false);
 assert(BigInt(measuredSql.data.budget.units.sqlite_rows_read)>0n&&BigInt(measuredSql.data.budget.units.sqlite_rows_read)<BigInt(units.sqlite_rows_read),'valid receipts replace the broad SQL read reserve with measured reads plus the settle allowance');
 assert(BigInt(measuredSql.data.budget.units.sqlite_rows_written)>0n&&BigInt(measuredSql.data.budget.units.sqlite_rows_written)<BigInt(units.sqlite_rows_written),'valid receipts replace the broad SQL write reserve with measured writes plus the settle allowance');
 assert(Number(measuredSql.data.budget.units.do_requests)<Number(units.do_requests),'completed nonce-bound namespace counts release unused request reserve');
 assert.equal(measuredSql.data.budget.coverage.sqlite_rows_read,'upper_bound');assert.equal(measuredSql.data.budget.coverage.sqlite_rows_written,'upper_bound');
 for(const callbackStatus of[200,403]){
  const missingSql=await call('/_fixture/sql-missing?status='+callbackStatus);
  assert.equal(missingSql.status,200);assert.equal(missingSql.data.status,callbackStatus,'funded uncertainty preserves the completed domain response');
  assert.equal(missingSql.data.body.unmetered,true);assert.equal(missingSql.data.account_calls,0);
  assert.equal(missingSql.data.reservations.length,1);
  const held=missingSql.data.reservations[0];assert.equal(held.state,'reserved');assert.equal(held.actual_pusd,null);
  assert.equal(BigInt(missingSql.data.budget.reserved_usd.replace('.','')),BigInt(held.estimate_pusd),'the entire original estimate remains held');
  assert.equal(missingSql.data.budget.settled_usd,'0.000000000000');
  assert.equal(missingSql.data.budget.coverage.sqlite_rows_read,'upper_bound');assert.equal(missingSql.data.budget.coverage.sqlite_rows_written,'upper_bound');
  assert.equal(missingSql.data.budget.health_events.length,0,'attribution ambiguity does not poison validated funding coverage');
 }

 const failedSettle=await call('/_fixture/sql-settle-failure');assert.equal(failedSettle.status,200);assert.equal(failedSettle.data.status,503);assert.equal(failedSettle.data.account_calls,0,'no parent SQL is funded before the durable settlement acknowledgement');assert(Number(failedSettle.data.budget.reserved_usd)>0,'failed settlement retains the full reservation');
 const timedSettle=await call('/_fixture/sql-settle-failure?timeout=1');assert.equal(timedSettle.status,200);assert.equal(timedSettle.data.status,503);assert.equal(timedSettle.data.account_calls,0);assert(Number(timedSettle.data.budget.reserved_usd)>0,'settle response timeout retains the full reservation with no credit');

 assert.equal((await call('/api/sessions/s1/commands?machine_id=h1','POST',42)).status,400,'malformed safety body keeps typed refusal');
 assert.equal((await call('/api/sessions/s1/commands?machine_id=h1','POST',{kind:'send',key:'fake-safety',text:'blocked'})).status,429);
 assert.equal((await call('/api/sessions/s1/commands?machine_id=h1','POST',{kind:'stop',key:'operator-stop'})).status,200);
 assert.equal((await call('/api/budget')).status,200);
 assert.equal((await call('/_fixture/fanout','POST',{})).status,429);
 const first=await call('/_fixture/dispatch');assert.equal(first.status,200);assert.equal(first.data.delivered,0);
 const sinkBefore=(await call('/_fixture/sink')).data.items;assert(sinkBefore.length>=1);assert(sinkBefore.every(v=>v.attempts===1));
 await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
 assert.equal((await call('/api/repos','POST',{})).status,429,'ceiling survives restart');
 await new Promise(r=>setTimeout(r,16000)); // Real persisted retry deadline, no raw-SQL clock bypass.
 const retried=await call('/_fixture/dispatch');assert.equal(retried.data.delivered,sinkBefore.length);
 const after=(await call('/_fixture/sink')).data.items;assert.equal(after.length,sinkBefore.length);assert(after.every(v=>v.attempts===2));
 assert.equal((await call('/_fixture/dispatch')).data.delivered,0);
 const status=(await call('/api/budget')).data;assert.equal(status.alerts.filter(a=>a.kind==='threshold').length,1);assert(status.alerts.every(a=>a.state==='delivered'));assert.equal(status.coverage.worker_cpu_ms,'upper_bound');assert.equal(status.provider.state,'unknown');
 const beforeBackground=BigInt(status.units.background_invocations);
 assert.equal((await call('/_fixture/scheduled')).status,200);
 const afterScheduled=(await call('/api/budget')).data;
 assert.equal(BigInt(afterScheduled.units.background_invocations),beforeBackground+1n);
 assert.equal((await call('/_fixture/sink')).data.items.length,after.length);
 assert.equal((await call('/_fixture/overrun','POST',{})).status,200);
 assert.equal((await call('/api/repos','POST',{})).status,429);
 const proposal={day:new Date().toISOString().slice(0,10),ceiling_pusd:'500000000000',reason:'Synthetic overrun reviewed and profile corrected',clear_hard_stop:true};
 const recovered=await call('/api/budget/override','POST',proposal);
 assert.equal(recovered.status,200,JSON.stringify(recovered.data));
 assert.equal((await call('/api/repos','POST',{})).status,200,'public audited recovery funded under new ceiling');
 await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
 assert.equal((await call('/api/repos','POST',{})).status,200,'breaker recovery survives restart');
 assert.equal((await call('/_fixture/unknown-health','POST',{})).status,503);
 assert.equal((await call('/api/budget/override','POST',proposal)).status,503,'unknown metrics cannot be cleared');
 const proof={passed:true,gate:'actual public Worker / BudgetDO / idempotent receiver restart',tests:37,namespace_request_refund_verified:true,settlement_timeout_retains_funding:true,source_origin_billing_dimensions_verified:true,concurrent_native_receipts_isolated:true,invalid_sql_attribution_retains_full_funding:true,domain_status_preserved_under_full_hold:true,per_kind_envelope_selected:true,route_kinds:routeKinds.map(([kind])=>kind),placement_uses_bounded_high_fanout_envelope:true,connection_uses_bounded_handshake_envelope:true,invalid_per_kind_envelope_refused:true,public_overrun_recovery_atomic:true,unknown_recovery_refused:true,scheduled_empty_invocation_priced:true,public_ceiling_persisted:true,safety_stop_available:true,unknown_metrics_fail_closed:true,alert_physical_ids:after.map(v=>v.id),delivery_attempts:after.map(v=>v.attempts),provider_billing_verified:false,production_data_mutated:false,note:'Synthetic financial bounds/auth/receiver; actual Worker, BudgetDO and restart. Not production sizing or live external delivery proof.'};
 await writeFile(resolve(root,'.local/tmp/hub2-public-budget-workerd.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
}finally{await mf.dispose();await rm(persist,{recursive:true,force:true});}
