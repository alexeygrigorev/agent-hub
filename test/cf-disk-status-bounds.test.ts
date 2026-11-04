import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {MonitoringService} from '../src/monitoring';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
const NOW=Date.now();
const fixture=(fn:(x:any)=>Promise<void>|void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('disk-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,meter=new RowsMeter(()=>NOW),sql=meter.wrap(h.sql),seed=fixtureSql(h,state);
 const service=new MonitoringService(sql,f=>state.storage.transactionSync(f),undefined,undefined,undefined,()=>NOW);
 await fn({h,seed,meter,service});expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
function observation(seed:any,id:string,host:string,at:number,primary=false,mounts=1){
 const filesystems=Array.from({length:mounts},(_,n)=>({mount_point:'/disk-'+n,is_primary:primary&&n===0,used_bytes:Math.floor(at/60000),total_bytes:1000000000000,percent:10}));
 seed.exec('INSERT INTO disk_observations VALUES(?,?,?,?,?,?,?)',id,host,new Date(at).toISOString(),at,new Date(at).toISOString(),JSON.stringify({machine_id:host,observed_at:new Date(at).toISOString(),filesystems}),'host');
}
describe('bounded disk status observations',()=>{
 it('seeks distinct host keys without walking1000 retained observations',()=>fixture(({seed,meter,service}:any)=>{
  for(let i=0;i<1000;i++)observation(seed,'history-'+i,'host',NOW-i*60000);
  const result=meter.runSync('status',()=>service.getDiskStatus(NOW));
  expect(result.machines).toHaveLength(1);expect(result.observation_pending).toBe(false);
  expect(meter.snapshot().status.rows_read).toBeLessThan(20);expect(meter.snapshot().status.rows_written).toBe(0);
 }));
 it('preserves configured200-observation,3-mount history and refuses excess before sample expansion',()=>fixture(({seed,meter,service}:any)=>{
  for(let i=0;i<200;i++)observation(seed,'sample-'+i,'host',NOW-(199-i)*60000,true,3);
  const known=meter.runSync('known',()=>service.getDiskStatus(NOW,'host'));
  expect(known.observation_pending).toBe(false);expect(known.machines[0].filesystems[0].trend).toMatchObject({state:'calculated',sample_count:200});
  expect(meter.snapshot().known.rows_read).toBeLessThan(1800);
  for(let i=0;i<2100;i++)observation(seed,'legacy-'+i,'host',NOW-(i+200)*60000,true);
  const unknown=meter.runSync('legacy',()=>service.getDiskStatus(NOW,'host'));
  expect(unknown.observation_pending).toBe(true);expect(unknown.machines[0].filesystems[0].trend.state).toBe('insufficient_history');
  expect(meter.snapshot().legacy.rows_read).toBeLessThan(300);expect(meter.snapshot().legacy.rows_written).toBe(0);
  expect(service.getStatusSummary(NOW)).toMatchObject({observation_pending:true,categories_entry:{coverage:'partial',observation_pending:true,reason:'disk_observation_limited'}});
 }));
 it('marks host and open-episode overflow explicitly without a false complete list',()=>fixture(({seed,meter,service}:any)=>{
  for(let i=0;i<65;i++)observation(seed,'host-'+i,'host-'+i,NOW);
  const hosts=meter.runSync('hosts',()=>service.getDiskStatus(NOW));
  expect(hosts.machines).toHaveLength(64);expect(hosts.machine_list_observation_pending).toBe(true);
  expect(service.getStatusSummary(NOW).categories_entry).toMatchObject({coverage:'partial',observation_pending:true,reason:'disk_observation_limited'});
  expect(meter.snapshot().hosts.rows_read).toBeLessThan(400);
  for(let i=0;i<2100;i++)seed.exec("INSERT INTO disk_episodes(episode_key,machine_id,mount_point,state,opened_at,last_updated_at) VALUES(?,'host-0','/','warn',?,?)",'episode-'+i,new Date(NOW).toISOString(),new Date(NOW).toISOString());
  const episodes=meter.runSync('episodes',()=>service.getDiskStatus(NOW,'host-0'));
  expect(episodes.observation_pending).toBe(true);expect(episodes.machines[0].open_episodes).toHaveLength(128);
  expect(episodes.machines[0].open_episodes_observation_pending).toBe(true);
  const summary=service.getStatusSummary(NOW);
  expect(summary).toMatchObject({observation_pending:true,categories_entry:{coverage:'partial',observation_pending:true,reason:'disk_observation_limited'}});
  expect(summary.filesystems.find((fs:any)=>fs.machine_id==='host-0')).toMatchObject({observation_pending:true});
  expect(meter.snapshot().episodes.rows_read).toBeLessThan(400);expect(meter.snapshot().episodes.rows_written).toBe(0);
 }));
 it('leaves retained legacy trends uncertified without constructor history repair',()=>fixture(({h,seed,meter,service}:any)=>{
  for(let i=0;i<1000;i++)observation(seed,'legacy-init-'+i,'host',NOW-i*60000,true);
  seed.exec('DELETE FROM disk_trend_projection_meta');seed.exec('DELETE FROM disk_trend_samples');
  const before=seed.exec("SELECT id,payload_json FROM disk_observations WHERE id='legacy-init-0'").one();
  meter.runSync('legacy-init',()=>service.init());
  expect(meter.snapshot()['legacy-init'].rows_read).toBeLessThan(3000);
  expect(meter.snapshot()['legacy-init'].rows_written).toBe(0);
  expect(seed.exec('SELECT id FROM disk_trend_projection_meta').toArray()).toEqual([]);
  expect(seed.exec('SELECT id FROM disk_trend_samples').toArray()).toEqual([]);
  expect(seed.exec('SELECT count(*) AS n FROM disk_observations').one().n).toBe(1000);
  expect(seed.exec("SELECT id,payload_json FROM disk_observations WHERE id='legacy-init-0'").one()).toEqual(before);
  const shapes=meter.statementTop(1500).filter((q:any)=>q.scope==='legacy-init').map((q:any)=>q.sql);
  expect(shapes.some((q:string)=>/INSERT OR REPLACE INTO disk_trend_samples|SELECT DISTINCT machine_id FROM disk_observations/i.test(q))).toBe(false);
  const status=service.getDiskStatus(NOW,'host');
  expect(status.observation_pending).toBe(true);expect(status.machines[0].filesystems[0].trend.state).toBe('insufficient_history');
  observation(seed,'new-after-legacy','host',NOW+1,true);
  expect(service.getDiskStatus(NOW+1,'host').observation_pending).toBe(true);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('bounds global host-health payloads before parsing and propagates incomplete coverage',()=>fixture(({seed,meter,service}:any)=>{
  for(let i=0;i<2100;i++)seed.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','health-'+i,new Date(NOW).toISOString(),NOW,new Date(NOW).toISOString(),JSON.stringify({load:{},memory:{status:'unknown'},services:[]}),'host');
  const health=meter.runSync('health',()=>service.getHostHealthStatus(NOW));
  expect(health.machines).toHaveLength(64);expect(health.observation_pending).toBe(true);
  expect(meter.snapshot().health.rows_read).toBeLessThan(700);expect(meter.snapshot().health.rows_written).toBe(0);
  const known=meter.runSync('known-health',()=>service.getHostHealthStatus(NOW,'health-2099'));
  expect(known.machines).toHaveLength(1);expect(known.observation_pending).toBe(false);
  expect(meter.snapshot()['known-health'].rows_read).toBeLessThan(50);
  expect(service.getStatusSummary(NOW)).toMatchObject({observation_pending:true,categories_entry:{coverage:'partial',reason:'host_health_observation_limited'}});
 }));

});
