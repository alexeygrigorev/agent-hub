import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {fixtureSql} from './helpers/fixture-budget';

const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('supervisor-coalescing-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now(),outer=now-240000;
 const service={service_id:'supervision',worker:'alive',status_file_age_s:700,observed_at:new Date(outer-700000).toISOString(),reason:'stale_status_file'};
 const source=(svc:any=service,observed=outer)=>seed.exec(`INSERT INTO host_health_observations(machine_id,observed_at,observed_at_ms,received_at,payload_json,reporter)
  VALUES('host',?,?,?,?, 'host') ON CONFLICT(machine_id) DO UPDATE SET observed_at=excluded.observed_at,observed_at_ms=excluded.observed_at_ms,payload_json=excluded.payload_json`,
  new Date(observed).toISOString(),observed,new Date(now).toISOString(),JSON.stringify({machine_id:'host',services:[svc]}));
 source();fn({h,state,seed,now,outer,service,source,read:(at:number)=>h.monitoringService.getHostHealthStatus(at,'host')});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});

describe('supervisor episode status-read coalescing',()=>{
 it('keeps unchanged stale-service polls write-free while source and actual state changes persist',()=>fixture(({h,seed,now,outer,service,source,read}:any)=>{
  h.meter.runSync('initial',()=>read(now));
  expect(h.meter.snapshot().initial.rows_written).toBeGreaterThan(0);
  const initial=seed.exec("SELECT * FROM supervisor_episodes WHERE episode_key='supervisor:host:supervision'").one();
  const status=h.meter.runSync('unchanged',()=>{let last:any;for(let i=1;i<=30;i++)last=read(now+i*45000);return last;});
  expect(h.meter.snapshot().unchanged.rows_written).toBe(0);
  expect(status.machines[0].services[0].status_file_age_s).toBeGreaterThan(700);
  expect(seed.exec("SELECT last_updated_at,details_json FROM supervisor_episodes WHERE episode_key='supervisor:host:supervision'").one()).toEqual({last_updated_at:initial.last_updated_at,details_json:initial.details_json});
  source({...service,observed_at:new Date(outer-600000).toISOString()},outer+1000);
  h.meter.runSync('source-change',()=>read(now));
  expect(h.meter.snapshot()['source-change'].rows_written).toBeGreaterThan(0);
  source({...service,worker:'broken',reason:'worker_broken'});
  h.meter.runSync('state-change',()=>read(now));
  expect(h.meter.snapshot()['state-change'].rows_written).toBeGreaterThan(0);
  expect(seed.exec("SELECT state FROM supervisor_episodes WHERE episode_key='supervisor:host:supervision'").one().state).toBe('broken');
  h.meter.runSync('broken-repeat',()=>read(now+45000));
  expect(h.meter.snapshot()['broken-repeat'].rows_written).toBe(0);
 }));
 it('retains derived reason transitions and never caches a rolled-back episode update',()=>fixture(({h,state,seed,now,outer,service,source,read}:any)=>{
  source({...service,status_file_age_s:100,reason:'ok'});
  read(now);
  h.meter.runSync('threshold',()=>read(now+400000));
  expect(h.meter.snapshot().threshold.rows_written).toBeGreaterThan(0);
  h.meter.runSync('threshold-repeat',()=>read(now+445000));
  expect(h.meter.snapshot()['threshold-repeat'].rows_written).toBe(0);
  source({...service,worker:'broken',reason:'worker_broken'});
  expect(()=>state.storage.transactionSync(()=>{
   read(now+450000);throw new Error('rollback-supervisor-episode');
  })).toThrow('rollback-supervisor-episode');
  expect(seed.exec("SELECT state FROM supervisor_episodes WHERE episode_key='supervisor:host:supervision'").one().state).toBe('stale');
  h.meter.runSync('after-rollback',()=>read(now+450000));
  expect(h.meter.snapshot()['after-rollback'].rows_written).toBeGreaterThan(0);
  expect(seed.exec("SELECT state FROM supervisor_episodes WHERE episode_key='supervisor:host:supervision'").one().state).toBe('broken');
 }));
});
