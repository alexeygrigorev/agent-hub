import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {fixtureSql} from './helpers/fixture-budget';
import {intakeLinkHealth} from '../src/intake-link-health';
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('resource-batches-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString();
 const r=h.fleetService.resources,original=r.sql,queries:{sql:string;args:any[]}[]=[];
 r.sql=new Proxy(original,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{queries.push({sql:query,args});return target.exec(query,...args);};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
 try{fn({h,state,r,seed,now,at,queries});}finally{r.sql=original;}
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
function launch(seed:any,id:string,state:string,at:string,native:string|null=null){seed.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
 VALUES(?,'fixture','agent','test',?,'{}','host','codex','account','headless','/repo','test',?,?,?,?)`,id,id,state,native,at,at);}
function control(seed:any,id:string,state:string,at:string,native:string){seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
 VALUES(?,'fixture',?,'{}','host','codex','headless',?,?,?,?)`,id,id,state,JSON.stringify({native_id:native,workspace:'/repo'}),at,at);}
describe('shared bounded resource observations',()=>{
 it('batches disjoint phase and active-state sources without losing unknown phases or raw sentinels',()=>fixture(({h,r,seed,at,queries}:any)=>{
  for(let i=0;i<2100;i++){
   seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('host',?,'old','exited',?,'/repo')",'old-'+i,at);
   launch(seed,'old-launch-'+i,'stopped',at,'old-'+i);control(seed,'old-control-'+i,'stopped',at,'old-'+i);
  }
  for(const [i,phase]of ['custom_busy','paused','running','zz_future'].entries())seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('host',?,'active',?,?,'/repo')",'native-'+i,phase,at);
  for(const [i,state]of ['offered','starting','running','uncertain'].entries())launch(seed,'live-'+i,state,at,i===0?'native-0':null);
  for(const [i,state]of ['queued','held','offered','starting','running','uncertain'].entries())control(seed,'control-'+i,state,at,'native-'+i%4);
  const count=h.meter.runSync('physical',()=>r.hostSessionCount('host'));
  expect(count).toBe(7);expect(h.meter.snapshot().physical.calls).toBe(2);expect(h.meter.snapshot().physical.rows_read).toBeLessThan(50);
  expect(r.hostSessionCount('host','live-3')).toBe(6);
  const rows=h.meter.runSync('canonical',()=>r.buildCanonicalSessions('host'));
  expect(rows.map((row:any)=>row.session_id)).toEqual(expect.arrayContaining(['control-0','control-1','control-2','control-3','control-4','control-5']));
  const sourceQueries=h.meter.statementTop(1500).filter((q:any)=>q.scope==='canonical'&&q.sql.startsWith('SELECT rowid AS source_rowid'));
  expect(sourceQueries).toHaveLength(2);
  expect(sourceQueries.every((q:any)=>q.calls===1)).toBe(true);
  const plans=queries.filter((q:any)=>q.sql.startsWith('SELECT session_key FROM (')||q.sql.startsWith('SELECT rowid AS source_rowid'));
  expect(plans.length).toBeGreaterThanOrEqual(3);
  for(const query of plans){const plan=seed.exec('EXPLAIN QUERY PLAN '+query.sql,...query.args).toArray().map((row:any)=>row.detail).join(' | ');
   expect(plan).toMatch(/SEARCH.*USING INDEX (fleet_session_machine_phase|session_control_machine_state|fleet_launch_machine_state)/);
   expect(plan).not.toMatch(/TEMP B-TREE|SCAN (?:fleet_sessions|session_control_sessions|fleet_launches)\b/);
  }

  for(let i=0;i<129;i++)seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('host',?,'extra','custom_busy',?,'/repo')",'overflow-'+i,at);
  expect(r.hostSessionCount('host')).toBeNull();
 }));
 it('batches CPU commitment facts, reuses only exact generations and invalidates absence/rollback',()=>fixture(({h,state,r,seed}:any)=>{
  const ids=Array.from({length:100},(_,i)=>'launch-'+i);
  for(const id of ids)seed.exec('INSERT INTO resource_cpu_starts VALUES(?,?,?)',id,2,'fixture');
  const batch=h.meter.runSync('cpu-batch',()=>r.cpuStarts(ids));
  expect(batch.size).toBe(100);expect(batch.get('launch-0')).toEqual({expected_cpu_cores:2,expected_cpu_source:'fixture'});
  expect(h.meter.snapshot()['cpu-batch'].calls).toBe(1);
  h.meter.runSync('cpu-warm',()=>{for(const id of ids)expect(r.cpuStart(id).expected_cpu_cores).toBe(2);});
  expect(h.meter.snapshot()['cpu-warm']?.calls??0).toBe(0);
  seed.exec("UPDATE resource_cpu_starts SET expected_cpu_cores=3 WHERE launch_id='launch-0'");expect(r.cpuStart('launch-0').expected_cpu_cores).toBe(3);
  expect(()=>state.storage.transactionSync(()=>{seed.exec("UPDATE resource_cpu_starts SET expected_cpu_cores=4 WHERE launch_id='launch-0'");expect(r.cpuStart('launch-0').expected_cpu_cores).toBe(4);throw new Error('rollback-cpu-fact');})).toThrow('rollback-cpu-fact');
  expect(r.cpuStart('launch-0').expected_cpu_cores).toBe(3);
  expect(r.cpuStart('future')).toBeNull();seed.exec("INSERT INTO resource_cpu_starts VALUES('future',5,'fixture')");expect(r.cpuStart('future').expected_cpu_cores).toBe(5);
  expect(()=>r.cpuStarts(Array(129).fill('launch-0'))).toThrow();
 }));
 it('memoizes complete health by exact watched sources while incomplete pages continue and rollback invalidates',()=>fixture(({h,state,seed}:any)=>{
  const health=(advance=true)=>intakeLinkHealth(h.sql,advance,h.writes);
  expect(health()).toEqual({ready:true,complete:true});
  h.meter.runSync('health-warm',()=>{for(let i=0;i<10;i++)expect(intakeLinkHealth(h.sql)).toEqual({ready:true,complete:true});});
  expect(h.meter.snapshot()['health-warm']?.calls??0).toBe(0);
  seed.exec("INSERT INTO intake_task_projection VALUES('bad','{}','broken')");expect(health()).toEqual({ready:false,complete:true});
  h.meter.runSync('invalid-warm',()=>health());expect(h.meter.snapshot()['invalid-warm']?.calls??0).toBe(0);
  expect(()=>state.storage.transactionSync(()=>{seed.exec("UPDATE intake_task_projection SET links='[]' WHERE id='bad'");expect(health()).toEqual({ready:true,complete:true});throw new Error('rollback-health-fact');})).toThrow('rollback-health-fact');
  expect(health()).toEqual({ready:false,complete:true});
  seed.exec("UPDATE intake_task_projection SET links='[]' WHERE id='bad'");
  for(let i=0;i<70;i++)seed.exec('INSERT INTO intake_task_projection VALUES(?,?,?)','task-'+i,'{}','[]');
  seed.exec("UPDATE intake_link_health_meta SET complete=0,after_id='' WHERE id=1");
  expect(health()).toEqual({ready:false,complete:false});const first=seed.exec('SELECT after_id FROM intake_link_health_meta WHERE id=1').one().after_id;
  expect(health()).toEqual({ready:false,complete:false});expect(seed.exec('SELECT after_id FROM intake_link_health_meta WHERE id=1').one().after_id).not.toBe(first);
  expect(health()).toEqual({ready:true,complete:true});
 }));
});
