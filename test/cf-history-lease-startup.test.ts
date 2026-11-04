import {describe,it,expect,vi} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {JobsService} from '../src/jobs';
import {HostHistoryService} from '../src/host-history';
import {fixtureSql} from './helpers/fixture-budget';
const indices=['leases_active_due_ms','leases_active_name','leases_holder_job'];
const fixture=(fn:(x:any)=>void|Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('history-lease-startup-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now();
 await fn({h,state,seed,now});expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
const indexNames=(state:any)=>state.storage.sql.exec("SELECT name FROM pragma_index_list('leases','main') LIMIT 65").toArray().filter((r:any)=>indices.includes(r.name)).map((r:any)=>r.name);
function retained(seed:any){for(const name of indices)seed.exec('DROP INDEX '+name);seed.exec('INSERT OR IGNORE INTO leases(name,epoch) VALUES(\'writer:old\',3)');}
describe('history and lease startup does not index retained sources',()=>{
 it('preserves two released leases without constructor builds and prepares only explicit maintenance',()=>fixture(({h,state,seed}:any)=>{
  retained(seed);const jobs=h.meter.runSync('released-lease-init',()=>new JobsService(h.sql,state,{enableAlarms:false}));
  expect(indexNames(state)).toEqual([]);expect(jobs.getAlarmTime()).toBeNull();expect(jobs.nextBackgroundWork()).toBeNull();
  expect(state.storage.sql.exec("SELECT epoch FROM leases WHERE name='writer:old'").toArray()[0].epoch).toBe(3);
  expect(h.meter.statementTop(4096).filter((r:any)=>r.scope==='released-lease-init'&&indices.some(name=>r.sql.includes('CREATE INDEX IF NOT EXISTS '+name)))).toEqual([]);
  jobs.prepareLeaseWork();expect(indexNames(state)).toHaveLength(3);
  new JobsService(h.sql,state,{enableAlarms:false});expect(indexNames(state)).toHaveLength(3);
 }));
 it('keeps known active lease consumers PK-bound without building indices and refuses global heads',()=>fixture(({h,state,seed,now}:any)=>{
  retained(seed);const jobs=new JobsService(h.sql,state,{enableAlarms:false});
  seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,lease_epoch,started_at) VALUES('known','head-recovery','known','{\"project\":\"p\"}','running',?,?,'fixture',4,?)",new Date(now).toISOString(),new Date(now+60000).toISOString(),new Date(now).toISOString());
  seed.exec("INSERT INTO leases(name,epoch,holder_job_id,acquired_at,expires_at) VALUES('head:p',4,'known',?,?)",new Date(now).toISOString(),new Date(now-1).toISOString());
  expect(()=>jobs.getAlarmTime()).toThrow('lease_indexes_pending');expect(indexNames(state)).toEqual([]);
  h.meter.runSync('known-lease-consumer',()=>expect(jobs.processBackground('known')).toBeNull());
  expect(state.storage.sql.exec("SELECT status FROM jobs WHERE id='known'").toArray()[0].status).toBe('expired');
  expect(state.storage.sql.exec("SELECT holder_job_id FROM leases WHERE name='head:p'").toArray()[0].holder_job_id).toBeNull();
  expect(h.meter.snapshot()['known-lease-consumer'].rows_read).toBeLessThan(60);expect(indexNames(state)).toEqual([]);
 }));
 it('does not keep a readiness grant after physical index loss or rolled-back preparation',()=>fixture(({h,state,seed}:any)=>{
  retained(seed);const jobs=new JobsService(h.sql,state,{enableAlarms:false});
  expect(()=>state.storage.transactionSync(()=>{jobs.prepareLeaseWork();throw new Error('rollback');})).toThrow('rollback');expect(indexNames(state)).toEqual([]);
  jobs.prepareLeaseWork();seed.exec('DROP INDEX leases_active_due_ms');expect(indexNames(state)).toHaveLength(2);
  seed.exec("UPDATE leases SET holder_job_id='pending' WHERE name='root'");expect(()=>jobs.nextBackgroundWork()).toThrow('lease_indexes_pending');
 }));
 it('keeps falsy writer identities aligned across claim, deadline projection and queue',()=>fixture(async({h,state,seed,now}:any)=>{
  const at=new Date(now).toISOString();
  seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES('writer-host','writer-host','writer-host',?,?,?,0,'{}')",at,at,JSON.stringify({gate:{launch_allowed:true}}));
  const jobs=new JobsService(h.sql,state,{enableAlarms:false,now:()=>new Date(now)});
  for(const [i,date] of ['',0,false].entries()) {
   const id='falsy-'+i;
   seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at) VALUES(?,'writer-run',?,?,'offered',?,?,'fixture','writer-host',?)",id,id,JSON.stringify({date}),at,new Date(now+1200000).toISOString(),new Date(now+60000).toISOString());
   expect((await jobs.claimJob(id,'writer-host',true)).error).toBeUndefined();
   expect(state.storage.sql.exec('SELECT lease_name FROM job_claim_deadlines WHERE job_id=?',id).toArray()[0].lease_name).toBe('job:'+id);
   expect(state.storage.sql.exec('SELECT due_ms FROM hub_background_work WHERE id=?','job-deadline:'+id).toArray()[0].due_ms).toBe(now+300000);
  }
  seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at) VALUES('unsupported','writer-run','unsupported','{\"date\":true}','offered',?,?,'fixture','writer-host',?)",at,new Date(now+1200000).toISOString(),new Date(now+60000).toISOString());
  expect(await jobs.claimJob('unsupported','writer-host',true)).toMatchObject({error:'conflict',reason:'unsupported_lease_identity'});
  expect(state.storage.sql.exec("SELECT name FROM leases WHERE holder_job_id='unsupported' LIMIT 1").toArray()).toEqual([]);
 }));
 it('does not index 315 retained dimensions or compact during ordinary ingestion',()=>fixture(({h,state,seed,now}:any)=>{
  seed.exec('DROP INDEX resource_history_entities_seen');
  for(let i=0;i<315;i++)seed.exec("INSERT INTO resource_history_entities VALUES('fixture',?,'host',?)",String(i),now);
  const history=new HostHistoryService(h.sql,{hostResources:()=>[],cachedAccountStatus:()=>({})} as any,()=>now,h.writes,()=>state.storage.transactionSync(()=>{}));
  const compact=vi.spyOn(history,'compact');h.meter.runSync('retained-history-init',()=>history.init());
  expect(state.storage.sql.exec("SELECT name FROM pragma_index_list('resource_history_entities','main')").toArray().some((r:any)=>r.name==='resource_history_entities_seen')).toBe(false);
  history.sample();expect(compact).not.toHaveBeenCalled();
  h.meter.runSync('manual-history-compaction',()=>history.sample(true));expect(compact).toHaveBeenCalledTimes(1);
  expect(state.storage.sql.exec("SELECT name FROM pragma_index_list('resource_history_entities','main')").toArray().some((r:any)=>r.name==='resource_history_entities_seen')).toBe(true);
  expect(h.meter.snapshot()['retained-history-init'].rows_read).toBeLessThan(50);
 }));
 it('invalidates stale DDL facts before explicit history repair and refuses oversized external sources before flush',()=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('external-history-index-'+crypto.randomUUID())),async(h:any,state:any)=>{
  await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now();
  const history=new HostHistoryService(h.sql,{hostResources:()=>[]} as any,()=>now,h.writes,()=>state.storage.transactionSync(()=>{}));
  // Prime guarded native index facts, then model operator DDL outside Watch.
  h.sql.exec('CREATE INDEX IF NOT EXISTS resource_history_entities_seen ON resource_history_entities(last_seen_ms)');
  state.storage.sql.exec('DROP INDEX resource_history_entities_seen');
  for(let i=0;i<1001;i++)state.storage.sql.exec("INSERT INTO resource_history_entities VALUES('external',?,'host',?)",String(i),now);
  const flush=vi.spyOn(history,'flush');
  expect(()=>history.compact()).toThrow();expect(flush).not.toHaveBeenCalled();
  expect(state.storage.sql.exec("SELECT name FROM pragma_index_list('resource_history_entities','main')").toArray().some((r:any)=>r.name==='resource_history_entities_seen')).toBe(false);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM resource_history_entities').toArray()[0].n).toBe(1001);
  expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:true});
 }));
});
