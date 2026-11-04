import {generateTokenPair} from '../src/auth/token';
import {describe,it,expect,vi} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {BackgroundWorkQueue} from '../src/background-work';
import {JobsService} from '../src/jobs';
import {fixtureSql} from './helpers/fixture-budget';
const names=['jobs_offered_host_created','jobs_offered_machine_created','jobs_offered_due_ms','jobs_root_offered_due_ms','jobs_nonroot_offered_due_ms','jobs_root_offered_deadline_ms'];
const fixture=(fn:(x:any)=>void|Promise<void>,stopped=false)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('lazy-offer-index-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString();
 for(const name of names)seed.exec('DROP INDEX '+name);
 seed.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
 for(let i=0;i<17;i++)seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor) VALUES(?,'root-run',?,'{}','failed',?,?,'fixture')",'retained-'+i,'retained-'+i,at,at);
 const service=()=>new JobsService(h.sql,state,{now:()=>new Date(now),enableAlarms:false});
 h.meter.runSync('retained-job-constructor',()=>{h.lazyJobs=service();});
 await fn({h,state,seed,now,at,jobs:h.lazyJobs,service});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped});
});
function countIndexes(state:any){const rows=state.storage.sql.exec("SELECT name FROM pragma_index_list('jobs','main')").toArray();return rows.filter((row:any)=>names.includes(row.name)).length;}
function offer(seed:any,id:string,at:string,now:number,kind='root-run'){
 seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,offer_host_id,offer_machine_id,offer_expires_at,actor) VALUES(?,?,?,'{}','offered',?,?,'host','host',?,'fixture')",id,kind,id,at,new Date(now+1500000).toISOString(),new Date(now+120000).toISOString());
}
describe('offered index migrations are owned by real job requests',()=>{
 it('does not build six indexes over 17 terminal jobs and keeps empty reads genuinely empty',()=>fixture(({h,state,jobs}:any)=>{
  expect(countIndexes(state)).toBe(0);
  expect(h.meter.statementTop(4096).filter((r:any)=>r.scope==='retained-job-constructor'&&names.some(name=>r.sql.includes('CREATE INDEX IF NOT EXISTS '+name)))).toEqual([]);
  h.meter.runSync('empty-offered-reads',()=>{
   for(let i=0;i<20;i++){
    expect(jobs.getAlarmTime()).toBeNull();expect(jobs.nextBackgroundWork()).toBeNull();expect(jobs.offeredForHost('host')).toEqual({rows:[],truncated:false});
   }
   h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');h.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
  });
  expect(countIndexes(state)).toBe(0);expect(h.meter.snapshot()['empty-offered-reads'].rows_read).toBeLessThan(20);
 }));
 it('preserves legacy root freeze/queue intent without optional indexes, then repairs only an offered host request',()=>fixture(({h,state,seed,now,at,jobs,service}:any)=>{
  offer(seed,'legacy-offer',at,now);
  expect(()=>jobs.getAlarmTime()).toThrow('job_offer_indexes_pending');expect(()=>jobs.nextBackgroundWork()).toThrow('job_offer_indexes_pending');
  expect(countIndexes(state)).toBe(0);
  // Flag changes address the complete bounded deployed offered-index prefix,
  // even while all expression/host indexes are physically absent.
  h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');h.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
  expect(countIndexes(state)).toBe(0);
  const rows=h.meter.runSync('offered-request-repair',()=>jobs.offeredForHost('host'));
  expect(rows.rows.map((row:any)=>row.id)).toEqual(['legacy-offer']);expect(countIndexes(state)).toBe(6);
  expect(h.meter.snapshot()['offered-request-repair'].rows_read).toBeLessThan(500);
  expect(jobs.getAlarmTime()).toBe(now+1500000);
  const restarted=service();h.meter.runSync('already-built-offer-read',()=>expect(restarted.offeredForHost('host').rows).toHaveLength(1));
  expect(h.meter.statementTop(4096).some((r:any)=>r.scope==='already-built-offer-read'&&/^CREATE INDEX/i.test(r.sql))).toBe(false);
  expect(restarted.nextBackgroundWork()).toEqual({id:'legacy-offer',dueAt:now+1500000});
 }));
 it('does not retain an index-ready grant across external DDL or a rolled-back request repair',()=>fixture(({h,state,seed,now,at,jobs}:any)=>{
  offer(seed,'rollback-offer',at,now);jobs.offeredForHost('host');expect(countIndexes(state)).toBe(6);
  state.storage.sql.exec('DROP INDEX jobs_offered_due_ms');
  expect(()=>jobs.getAlarmTime()).toThrow('job_offer_indexes_pending');expect(countIndexes(state)).toBe(5);
  for(const name of names)seed.exec('DROP INDEX IF EXISTS '+name);
  expect(()=>state.storage.transactionSync(()=>{jobs.offeredForHost('host');expect(countIndexes(state)).toBe(6);throw new Error('rollback-offer-repair');})).toThrow('rollback-offer-repair');
  expect(countIndexes(state)).toBe(0);expect(()=>jobs.getAlarmTime()).toThrow('job_offer_indexes_pending');
  expect(jobs.offeredForHost('host').rows).toHaveLength(1);expect(countIndexes(state)).toBe(6);
 }));
 it('claims through the queue-owned Hub without optional-index work after commit and processes its known deadline',()=>fixture(async({h,state,seed,now,at}:any)=>{
  const pair=await generateTokenPair();
  seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities,credential_id) VALUES('host','host','host',?,?,?,?, 'claim-host')",at,at,JSON.stringify({gate:{launch_allowed:true}}),JSON.stringify({cpu_count:8}));
  seed.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,last_used_at)
    VALUES('claim-host','host',?,?,'claim-family','host','host','machine:host','["host"]','fixture',?,?,?)`,pair.accessHash,pair.refreshHash,at,new Date(now+3600000).toISOString(),at);
  offer(seed,'direct-claim',at,now,'head-recovery');offer(seed,'unrelated-root',at,now);
  expect(()=>h.jobsService.getAlarmTime()).toThrow('job_offer_indexes_pending');
  const legacy=vi.spyOn(h.jobsService,'getAlarmTime').mockImplementation(()=>{throw new Error('legacy deadline scheduler was touched');});
  try{
   const response=await h.fetch(new Request('https://hub.test/api/jobs/direct-claim/claim',{method:'POST',headers:{Authorization:'Bearer '+pair.accessToken,'Content-Type':'application/json'},body:JSON.stringify({gate:true})}));
   expect(response.status,await response.clone().text()).toBe(200);const result=await response.json();expect(result.job_token).toMatch(/^hubt_/);
   const lease=state.storage.sql.exec("SELECT acquired_at,holder_job_id FROM leases WHERE name='head:default'").one();expect(lease.holder_job_id).toBe('direct-claim');
   const due=Date.parse(lease.acquired_at)+300000;
   expect(state.storage.sql.exec("SELECT due_ms FROM hub_background_work WHERE id='job-deadline:direct-claim'").one().due_ms).toBe(due);
   h.jobsService.setClock(()=>new Date(due+1));h.backgroundWork.now=()=>due+1;
   await h.alarm();
   expect(state.storage.sql.exec("SELECT status,outcome_summary FROM jobs WHERE id='direct-claim'").one()).toMatchObject({status:'failed',outcome_summary:'no first action'});
   expect(state.storage.sql.exec("SELECT holder_job_id FROM leases WHERE name='head:default'").one().holder_job_id).toBeNull();
   expect(state.storage.sql.exec("SELECT c.revoked_at FROM jobs j JOIN credentials c ON c.token_hash=j.claim_token_hash WHERE j.id='direct-claim'").one().revoked_at).not.toBeNull();
   expect(state.storage.sql.exec("SELECT status FROM jobs WHERE id='unrelated-root'").one().status).toBe('offered');
   expect(countIndexes(state)).toBe(0);expect(legacy).not.toHaveBeenCalled();
  }finally{legacy.mockRestore();}
 }));
 it('uses a new same-clock flag generation to restore an earlier due after a frozen consumer',()=>fixture(({h,state,seed,now,at,jobs}:any)=>{
  offer(seed,'frozen-offer',at,now);const queue=new BackgroundWorkQueue(h.sql,(fn:any)=>state.storage.transactionSync(fn),()=>now+180000);
  queue.cancel('root-slot');
  h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
  const first=state.storage.sql.exec('SELECT generation FROM job_background_root_generation WHERE id=1').one().generation;
  h.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
  jobs.setClock(()=>new Date(now+180000));
  const work=queue.takeDue();expect(work?.id).toBe('job-deadline:frozen-offer');
  const due=jobs.processBackground('frozen-offer');expect(due).toBe(now+1500000);queue.reschedule(work!,due!);
  expect(state.storage.sql.exec("SELECT due_ms FROM hub_background_work WHERE id='job-deadline:frozen-offer'").one().due_ms).toBe(now+1500000);
  h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
  const generation=state.storage.sql.exec('SELECT generation FROM job_background_root_generation WHERE id=1').one().generation;expect(generation).toBe(first+2);
  expect(state.storage.sql.exec("SELECT due_ms FROM hub_background_work WHERE id='job-deadline:frozen-offer'").one().due_ms).toBe(now+120000);
  h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
  expect(state.storage.sql.exec('SELECT generation FROM job_background_root_generation WHERE id=1').one().generation).toBe(generation);
  const expired=queue.takeDue();expect(expired?.id).toBe('job-deadline:frozen-offer');expect(jobs.processBackground('frozen-offer')).toBeNull();
  expect(state.storage.sql.exec("SELECT status FROM jobs WHERE id='frozen-offer'").one().status).toBe('expired');
  expect(countIndexes(state)).toBe(0);
 }));
 it('refuses a partial root-flag source before changing the flag or seeding a truncated cohort',()=>fixture(({h,state,seed,now,at}:any)=>{
  // Corrupt/legacy input larger than queue capacity is synthetic setup only.
  // Keep the real flag producer and its BEFORE source proof installed.
  seed.exec('DROP TRIGGER hub_background_job_insert');
  h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
  for(let i=0;i<129;i++)offer(seed,'oversized-offer-'+i,at,now,'writer-run');
  h.meter.runSync('oversized-root-disable',()=>h.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1').toArray());
  expect(h.meter.snapshot()['oversized-root-disable'].rows_read).toBeLessThan(10);
  const generation=state.storage.sql.exec('SELECT generation FROM job_background_root_generation WHERE id=1').one().generation;
  const before=state.storage.sql.exec('SELECT COUNT(*) AS n FROM hub_background_work').one().n;
  expect(()=>h.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1').toArray()).toThrow();
  expect(state.storage.sql.exec('SELECT enabled FROM root_scheduling WHERE id=1').one().enabled).toBe(0);
  expect(state.storage.sql.exec('SELECT generation FROM job_background_root_generation WHERE id=1').one().generation).toBe(generation);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM hub_background_work').one().n).toBe(before);
  // Failed native trigger statements expose no row counters: the real daily
  // guard stops rather than inventing zero cost or continuing background work.
  expect(h.rowBudget.status().stopped).toBe(true);
 },true));
});
