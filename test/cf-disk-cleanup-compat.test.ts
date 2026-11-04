import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {MonitoringService} from '../src/monitoring';
import {fixtureSql} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('disk-cleanup-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state),now=Date.now();
 seed.exec('DROP INDEX IF EXISTS idx_disk_obs_time');
 const queries:string[]=[],sql=new Proxy(h.sql,{get(target,key){if(key==='exec')return(q:string,...args:any[])=>{queries.push(q);return target.exec(q,...args);};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
 const service=new MonitoringService(sql,f=>state.storage.transactionSync(f),undefined,undefined,undefined,()=>now,h.writes);
 const insert=(id:string,host='other',time=now)=>seed.exec('INSERT INTO disk_observations VALUES(?,?,?,?,?,?,?)',id,host,new Date(time).toISOString(),time,new Date(now).toISOString(),'{"filesystems":[]}','fixture');
 const post=(host='host')=>service.handleObservation(new Request('https://hub/api/disk/observe',{method:'POST',body:JSON.stringify({machine_id:host,observed_at:new Date(now).toISOString(),filesystems:[{mount_point:'/',device_id:'root',is_primary:true,total_bytes:1000000,used_bytes:100000,free_bytes:900000,percent:10}]})}),{kind:'admin',actor:'fixture'});
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 await fn({h,state,seed,now,queries,service,insert,post});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
const snapshots=(queries:string[])=>queries.filter(q=>q.includes('rowid AS source_rowid,id,machine_id,observed_at_ms'));
describe('disk cleanup uses complete bounded keys without a global index build',()=>{
 it('does no legacy time-index build at init, pays one200-row snapshot, and reuses only committed watched keys',()=>fixture(async({h,state,seed,queries,service,insert,post}:any)=>{
  for(let i=0;i<200;i++)insert('disk-'+i);
  h.meter.runSync('disk-init',()=>service.init());
  expect(queries.some((q:string)=>/CREATE INDEX.*idx_disk_obs_time/i.test(q))).toBe(false);
  expect(seed.exec("SELECT name FROM sqlite_master WHERE name='idx_disk_obs_time'").toArray()).toHaveLength(0);
  expect((await h.meter.run('disk-first',()=>post())).status).toBe(200);
  expect(h.meter.snapshot()['disk-first'].rows_read).toBeLessThan(300);expect(snapshots(queries)).toHaveLength(1);
  expect((await h.meter.run('disk-warm',()=>post())).status).toBe(200);
  expect(snapshots(queries)).toHaveLength(1);expect(h.meter.snapshot()['disk-warm'].rows_read).toBeLessThan(20);
  insert('foreign-mutation');expect((await post()).status).toBe(200);expect(snapshots(queries)).toHaveLength(2);
  const before=seed.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n,transaction=state.storage.transactionSync;
  state.storage.transactionSync=(fn:any)=>transaction.call(state.storage,()=>{fn();throw new Error('disk-rollback');});
  try{expect((await post()).status).toBe(500);}finally{state.storage.transactionSync=transaction;}
  expect(seed.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n).toBe(before);
  expect((await post()).status).toBe(200);expect(snapshots(queries)).toHaveLength(3);
  expect(seed.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n).toBe(before+1);
 }));
 it('preserves exact global, per-machine and retention bounds with only selected PK deletes',()=>fixture(async({h,seed,now,queries,insert,post}:any)=>{
  for(let i=0;i<1000;i++)insert('history-'+i,'host-'+Math.floor(i/250),now-(1000-i)*1000);
  expect((await post('new-host')).status).toBe(200);
  expect(seed.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n).toBe(1000);
  expect(seed.exec("SELECT id FROM disk_observations WHERE id='history-0'").toArray()).toHaveLength(0);
  expect((await post('host-0')).status).toBe(200);
  expect(seed.exec("SELECT COUNT(*) AS n FROM disk_observations WHERE machine_id='host-0'").one().n).toBe(200);
  insert('expired-a','old-host',now-15*86400000);insert('expired-b','old-host',now-15*86400000);
  expect((await post('new-host')).status).toBe(200);
  expect(seed.exec("SELECT id FROM disk_observations WHERE machine_id='old-host'").toArray()).toHaveLength(0);
  expect(queries.filter((q:string)=>/^DELETE FROM disk_observations/i.test(q)).every((q:string)=>q.includes('WHERE id IN (')&&!q.includes('SELECT'))).toBe(true);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('refuses2100 retained observations before ingestion or cleanup effects',()=>fixture(async({h,seed,queries,insert,post}:any)=>{
  for(let i=0;i<2100;i++)insert('overflow-'+i);
  const response=await h.meter.run('disk-overflow',()=>post());expect(response.status).toBe(503);
  expect(await response.json()).toMatchObject({error:'disk_cleanup_observation_limited'});
  expect(h.meter.snapshot()['disk-overflow'].rows_read).toBeLessThanOrEqual(1001);expect(h.meter.snapshot()['disk-overflow'].rows_written).toBe(0);
  expect(queries.some((q:string)=>/^INSERT INTO disk_observations|^DELETE FROM disk_observations/i.test(q))).toBe(false);
  expect(seed.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n).toBe(2100);
 }));
});
