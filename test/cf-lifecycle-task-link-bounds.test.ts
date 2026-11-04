import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {buildSessionInputs,collectCleanupEligible} from '../src/session_lifecycle_registry';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('link-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),meter=new RowsMeter(),sql=meter.wrap(h.sql);
 seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
 VALUES('selected','agent:fixture','selected','{}','host','codex','headless','stopped',?,?,?)`,JSON.stringify({native_id:'native',workspace:'/repo',tag:'test',native_created_at_ms:now}),at,at);
 seed.exec("UPDATE intake_link_health_meta SET complete=1 WHERE id=1");
 fn({h,state,seed,meter,sql,now});expect(h.rowBudget.status()).toMatchObject({limit_total:20000,stopped:false});
});
const read=(sql:any)=>buildSessionInputs(sql,new Set(['selected']));
function task(seed:any,id:string,links:any[]){seed.exec('INSERT INTO intake_task_projection VALUES(?,?,?)',id,JSON.stringify({id,status:'open'}),JSON.stringify(links));}
describe('selected lifecycle intake links',()=>{
 it('hydrates only indexed relevant tasks while retaining canonical link and report semantics',()=>fixture(({seed,meter,sql}:any)=>{
  for(let i=0;i<2100;i++)task(seed,'unrelated-'+i,[]);
  task(seed,'native-link',[{kind:'link',native_id:'native',routing_job_id:'routing'},{kind:'report',reference:'commit:abc'}]);
  task(seed,'unverified',[{kind:'link',native_id:'native'}]);
  task(seed,'hub-link',[{kind:'link',hub_session_id:'selected'}]);
  const result=meter.runSync('selected',()=>read(sql));const ref=result.refs.get('host/native')!;
  expect(ref).toMatchObject({task_refs:['native-link','hub-link'],report_refs:['commit:abc'],task_linkage_observation_pending:false});
  expect(meter.snapshot().selected.rows_read).toBeLessThan(200);expect(meter.snapshot().selected.rows_written).toBe(0);
  const query='SELECT ref,task_id FROM intake_projection_link_refs WHERE ref IN (SELECT value FROM json_each(?)) LIMIT 129';
  const plan=seed.exec('EXPLAIN QUERY PLAN '+query,JSON.stringify(['native','selected'])).toArray().map((r:any)=>r.detail).join(' | ');
  expect(plan).toMatch(/SEARCH.*PRIMARY KEY.*ref=/);expect(plan).not.toMatch(/SCAN intake_projection_link_refs|TEMP B-TREE/);
  seed.exec("UPDATE intake_task_projection SET links='[]' WHERE id IN ('native-link','hub-link')");
  expect(read(sql).refs.get('host/native')).toMatchObject({task_refs:[],task_linkage_observation_pending:false});
 }));
 it('invalidates real Status lifecycle caches when only health readiness changes and rolls back honestly',()=>fixture(({h,state,seed}:any)=>{
  const status=h.statusService as any,selected=new Set(['selected']);
  seed.exec('UPDATE intake_link_health_meta SET complete=0 WHERE id=1');
  expect(status.readLifecycleInputs(selected).refs.get('host/native')).toMatchObject({task_linkage_observation_pending:true,protected_session:true});
  seed.exec('UPDATE intake_link_health_meta SET complete=1 WHERE id=1');
  expect(status.readLifecycleInputs(selected).refs.get('host/native')).toMatchObject({task_linkage_observation_pending:false});
  expect(()=>state.storage.transactionSync(()=>{
   seed.exec('UPDATE intake_link_health_meta SET complete=0 WHERE id=1');
   expect(status.readLifecycleInputs(selected).refs.get('host/native')).toMatchObject({task_linkage_observation_pending:true});
   throw new Error('rollback-link-health-cache');
  })).toThrow('rollback-link-health-cache');
  expect(status.readLifecycleInputs(selected).refs.get('host/native')).toMatchObject({task_linkage_observation_pending:false});
 }));
 it('marks incomplete, oversized or overflowing task evidence protected before cleanup',()=>fixture(({seed,meter,sql,now}:any)=>{
  task(seed,'oversized',Array.from({length:129},()=>({kind:'link',hub_session_id:'selected'})));
  let result=meter.runSync('bad',()=>read(sql));
  expect(result.refs.get('host/native')).toMatchObject({task_linkage_observation_pending:true,protected_session:true});
  seed.exec("UPDATE intake_task_projection SET links='[]' WHERE id='oversized'");
  seed.exec('UPDATE intake_link_health_meta SET complete=0 WHERE id=1');
  expect(read(sql).refs.get('host/native')).toMatchObject({task_linkage_observation_pending:true,protected_session:true});
  seed.exec('UPDATE intake_link_health_meta SET complete=1 WHERE id=1');
  for(let i=0;i<129;i++)task(seed,'linked-'+i,[{kind:'link',hub_session_id:'selected'}]);
  result=meter.runSync('overflow',()=>read(sql));
  expect(result.refs.get('host/native')).toMatchObject({task_refs:[],task_linkage_observation_pending:true,protected_session:true});
  expect(meter.snapshot().overflow.rows_read).toBeLessThan(400);expect(meter.snapshot().overflow.rows_written).toBe(0);
  expect(collectCleanupEligible(sql,{host:'host'},'agent:fixture',now).eligible).toEqual([]);
 }));
});
