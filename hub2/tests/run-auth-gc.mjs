// Actual public Worker + Control/Budget DOs; isolated synthetic auth GC stress.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {canonicalSizingEnvelopes} from './canonical-cost-envelope.mjs';
const root=resolve(import.meta.dirname,'../..'),hub=resolve(root,'hub2');
const candidate=process.env.HUB2_AUTH_GC_CANDIDATE;
const metrics=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
const plan=JSON.parse(candidate?await readFile(candidate,'utf8'):canonicalSizingEnvelopes(metrics));
// Auth network latency and non-SQL profiles are not verified by this SQL test.
for(const side of ['normal','safety']){plan[side].max_wall_ms=10000;for(const p of Object.values(plan[side].by_kind||{}))p.max_wall_ms=10000;}
const sources=['budget-edge','control-do','control-auth','auth','db','sql-receipt','budget-do'];
const hashes=async()=>Object.fromEntries(await Promise.all(sources.map(async n=>[n,createHash('sha256').update(await readFile(resolve(hub,'src',n+'.ts'))).digest('hex')])));
const before=await hashes();
const built=await build({entryPoints:[resolve(hub,'tests/auth-gc-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers','node:async_hooks']});
await mkdir(resolve(root,'.local/tmp'),{recursive:true});const persist=await mkdtemp(resolve(root,'.local/tmp/auth-gc-'));
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{name:'isolated-auth-gc',script:built.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_als'],durableObjects:{CONTROL:{className:'AuthGcControl',useSQLite:true},BUDGET:{className:'AuthGcBudget',useSQLite:true}},bindings:{HUB2_BUDGET_ENVELOPES:JSON.stringify(plan),HUB_ADMINS:'fixture@example.test'}}]});
let mf=new Miniflare(options);
const results=[];
try{
 let base=(await mf.ready).origin;
 for(const id of ['device','revoke']){
  const token='synthetic-auth-gc-'+id+'-000000000000000000000000';
  const seed=await fetch(base+'/_fixture/seed?case='+id,{method:'POST',body:JSON.stringify({token})});assert.equal(seed.status,200);assert.equal((await seed.json()).seeded,75);
  await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
  const path=id==='device'?'/device':'/api/token/revoke';
  const response=await fetch(base+path+'?case='+id,{method:id==='device'?'GET':'POST',headers:{Authorization:'Bearer '+token},redirect:'manual'});
  assert.equal(response.status,id==='device'?303:200,await response.text());
  const remaining=await(await fetch(base+'/_fixture/remaining?case='+id)).json();assert.deepEqual(remaining.remaining,[0,0,0]);
  const reservations=await(await fetch(base+'/_fixture/reservations?case='+id)).json();assert.equal(reservations.length,1);
  const row=reservations[0],estimate=JSON.parse(row.estimate_json),actual=JSON.parse(row.actual_json).units;
  assert.equal(row.kind,id==='device'?'auth_request':'auth_revoke');assert.equal(row.state,'settled');
  assert.equal(estimate.sqlite_rows_read,256);assert.equal(estimate.sqlite_rows_written,384);
  assert(actual.sqlite_rows_read<=256,JSON.stringify({id,actual}));assert(actual.sqlite_rows_written<=384,JSON.stringify({id,actual}));assert(actual.sqlite_rows_written>=75,'all 75 deletes must be counted');
  results.push({case:id,status:response.status,kind:row.kind,seeded:75,remaining:0,actual_reads:actual.sqlite_rows_read,actual_writes:actual.sqlite_rows_written});
 }
 const after=await hashes();assert.deepEqual(after,before,'production source changed during run');
 const report={passed:true,tests:2,source_stable:true,source_hashes:before,results,synthetic_fixture:true,production_data_mutated:false,non_sql_bounds_verified:false};
 await writeFile(resolve(root,'.local/tmp/hub2-auth-gc-workerd.json'),JSON.stringify(report,null,2),{mode:0o600});console.log(JSON.stringify(report));
}finally{await mf.dispose();await rm(persist,{recursive:true,force:true});}
