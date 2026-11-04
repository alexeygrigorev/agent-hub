// Actual Worker and BudgetDO; synthetic financial bounds/auth/receiver, never production sizing.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {resolve} from 'node:path';
const root=resolve(import.meta.dirname,'../..'),hub=resolve(root,'hub2');
const names=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
const units=Object.fromEntries(names.map(k=>[k,0]));
Object.assign(units,{worker_requests:1,worker_cpu_ms:100,do_requests:12,do_gb_ms:1000,sqlite_rows_read:1000,sqlite_rows_written:30000,sqlite_byte_days:1});
const safety={...units,sqlite_rows_written:100,sqlite_rows_read:100,do_gb_ms:100};
const plan={version:1,normal:{units,max_do_calls:8,max_wall_ms:10000,provenance:'Synthetic financial threshold fixture, not production sizing'},safety:{units:safety,max_do_calls:8,max_wall_ms:10000,provenance:'Synthetic recovery financial fixture'}};
const entry=`
import worker from './src/worker';
import {BudgetDO} from './src/budget-do';
import {dispatchBudgetAlerts} from './src/budget-edge';
export {BudgetDO};
export class Probe {
 constructor(ctx){this.sql=ctx.storage.sql;this.sql.exec('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,effects INTEGER NOT NULL,verifies INTEGER NOT NULL)');this.sql.exec('INSERT OR IGNORE INTO calls VALUES(1,0,0)');}
 async fetch(r){const p=new URL(r.url).pathname;if(p==='/_stats')return Response.json(this.sql.exec('SELECT * FROM calls').toArray()[0]);
 if(p==='/_verify'){this.sql.exec('UPDATE calls SET verifies=verifies+1');const b=await r.json();return b.token==='synthetic-fixture-token-0000000000000000'?Response.json({actor:{id:'human:fixture',kind:'operator',scopes:['*']}}):Response.json({error:'unauthorized'},{status:401});}
 if(r.method==='POST')this.sql.exec('UPDATE calls SET effects=effects+1');return Response.json({items:[],effect:true});}
}
export default {async fetch(r,env){const u=new URL(r.url);
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
 if(u.pathname==='/_fixture/fanout'){
  const p=JSON.parse(env.HUB2_BUDGET_ENVELOPES);p.normal.max_do_calls=1;const original=env.BUDGET;
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
const bundles=await Promise.all([entry,sink].map(contents=>build({stdin:{contents,resolveDir:hub,loader:'ts'},bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']})));
await mkdir(resolve(root,'.local/tmp'),{recursive:true});const persist=await mkdtemp(resolve(root,'.local/tmp/hub2-public-budget-'));
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[
{name:'public-budget-fixture',script:bundles[0].outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',outboundService:'alert-sink',durableObjects:{BUDGET:{className:'BudgetDO',useSQLite:true},CONTROL:{className:'Probe',useSQLite:true},PROJECTS:{className:'Probe',useSQLite:true},MACHINES:{className:'Probe',useSQLite:true},ACCOUNTS:{className:'Probe',useSQLite:true},MAILBOXES:{className:'Probe',useSQLite:true}},bindings:{HUB2_BUDGET_ENVELOPES:JSON.stringify(plan),HUB_ALERT_WEBHOOK:'https://fixture-alert.test/deliver',HUB_ALERT_WEBHOOK_TOKEN:'synthetic-alert-secret'}},
{name:'alert-sink',script:bundles[1].outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',durableObjects:{SINK:{className:'Sink',useSQLite:true}}}]});
let mf=new Miniflare(options),base='';
const call=async(path,method='GET',body)=>{const r=await fetch(base+path,{method,headers:{'Content-Type':'application/json',Authorization:'Bearer synthetic-fixture-token-0000000000000000'},...(body===undefined?{}:{body:JSON.stringify(body)})});let data;try{data=await r.json();}catch{data={};}return {status:r.status,data,headers:r.headers};};
try{
 base=(await mf.ready).origin;
 assert.equal((await call('/api/repos','POST',{name:'fixture-a'})).status,200);
 assert.equal((await call('/api/repos','POST',{name:'fixture-b'})).status,200);
 const before=(await call('/_fixture/stats')).data;
 const blocked=await call('/api/repos','POST',{name:'must-not-run'});assert.equal(blocked.status,429);assert.equal(blocked.data.error,'budget_paused');assert(Number(blocked.headers.get('Retry-After'))>0);
 assert.deepEqual((await call('/_fixture/stats')).data,before,'ceiling denies before authentication fanout');
 const unknown=await call('/_fixture/unknown','POST',{});assert.equal(unknown.status,503);assert.equal(unknown.data.error,'budget_metrics_unknown');
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
 const proof={passed:true,gate:'actual public Worker / BudgetDO / idempotent receiver restart',tests:16,public_overrun_recovery_atomic:true,unknown_recovery_refused:true,scheduled_empty_invocation_priced:true,public_ceiling_persisted:true,safety_stop_available:true,unknown_metrics_fail_closed:true,alert_physical_ids:after.map(v=>v.id),delivery_attempts:after.map(v=>v.attempts),provider_billing_verified:false,production_data_mutated:false,note:'Synthetic financial bounds/auth/receiver; actual Worker, BudgetDO and restart. Not production sizing or live external delivery proof.'};
 await writeFile(resolve(root,'.local/tmp/hub2-public-budget-workerd.json'),JSON.stringify(proof,null,2));console.log(JSON.stringify(proof));
}finally{await mf.dispose();await rm(persist,{recursive:true,force:true});}
