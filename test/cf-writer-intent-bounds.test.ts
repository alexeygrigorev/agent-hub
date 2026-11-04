import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {JobsService} from '../src/jobs';
import {fixtureSql} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('writer-intent-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString();
 seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES('writer-host','writer-host','writer-host',?,?,?,0,'{}')",at,at,JSON.stringify({gate:{launch_allowed:true}}));
 const jobs=new JobsService(h.sql,state,{enableAlarms:false,now:()=>new Date(now)});
 const add=(id:string,payload='{}',kind='root-run',effect:any=null,status='offered')=>seed.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at,role_effect)
  VALUES(?,?,?,?,?,?,?,'fixture','writer-host',?,?)`,id,kind,id,payload,status,at,new Date(now+1200000).toISOString(),new Date(now+60000).toISOString(),effect);
 await fn({h,state,seed,now,at,jobs,add});expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
describe('writer intent completeness before claim effects',()=>{
 for(const source of ['terminal','unmatched-intent'])it('refuses1001 '+source+' rows before JSON or persistence',()=>fixture(async({h,state,seed,jobs,add}:any)=>{
  for(let i=0;i<1001;i++)add('history-'+i,source==='terminal'?'{':JSON.stringify({date:'2000-01-01'}),source==='terminal'?'root-run':'writer-run',source==='terminal'?null:'{}','failed');
  add('current',JSON.stringify({date:'2026-11-04'}),'writer-run');
  await expect(h.meter.run('writer-incomplete',()=>jobs.claimJob('current','writer-host',true))).rejects.toMatchObject({status:503,code:'writer_intent_observation_pending'});
  const cost=h.meter.snapshot()['writer-incomplete'];expect(cost.rows_read).toBeLessThan(170);expect(cost.rows_written).toBe(0);
  expect(state.storage.sql.exec("SELECT status,claim_token_hash FROM jobs WHERE id='current'").toArray()[0]).toMatchObject({status:'offered',claim_token_hash:null});
  expect(state.storage.sql.exec("SELECT name FROM leases WHERE holder_job_id='current' LIMIT 1").toArray()).toEqual([]);
  expect(h.meter.statementTop(4096).filter((r:any)=>r.scope==='writer-incomplete').some((r:any)=>r.sql.includes('json_extract'))).toBe(false);
 }));
 it('preserves complete17-job legacy positive, current exclusion, and actual claim fence',()=>fixture(async({h,state,seed,jobs,add}:any)=>{
  add('current',JSON.stringify({date:'2026-11-04'}),'writer-run');
  add('legacy',JSON.stringify({date:'2026-11-04',project:'foreign-project'}),'writer-run','{}');
  for(let i=0;i<15;i++)add('noise-'+i,'{','root-run',null,'failed');
  const job=state.storage.sql.exec("SELECT * FROM jobs WHERE id='current'").toArray()[0];
  expect(h.meter.runSync('writer-known-positive',()=>(jobs as any).hasUnresolvedWriterIntent(job))).toBe(true);
  expect(await h.meter.run('writer-real-fence',()=>jobs.claimJob('current','writer-host',true))).toMatchObject({error:'conflict',reason:'writer_date_busy'});
  expect(h.meter.snapshot()['writer-real-fence'].rows_written).toBe(0);
  expect(state.storage.sql.exec("SELECT role_name FROM jobs WHERE id='legacy'").toArray()[0].role_name).toBeNull();
  // Only the candidate job's own intent remains: current-id exclusion still
  // permits the lease acquisition instead of self-deadlocking forever.
  seed.exec("UPDATE jobs SET role_finish='{}' WHERE id='legacy'");seed.exec("UPDATE jobs SET role_effect='{}' WHERE id='current'");
  expect((jobs as any).hasUnresolvedWriterIntent(job)).toBe(false);
  expect((await jobs.claimJob('current','writer-host',true)).error).toBeUndefined();
  expect(state.storage.sql.exec("SELECT holder_job_id FROM leases WHERE name='writer:2026-11-04'").toArray()[0].holder_job_id).toBe('current');
  const plan=state.storage.sql.exec("EXPLAIN QUERY PLAN SELECT j.id FROM json_each(?) raw CROSS JOIN jobs j ON j.id=raw.value WHERE j.kind='writer-run' AND j.id!=? AND j.role_effect IS NOT NULL AND j.role_finish IS NULL AND COALESCE(NULLIF(CASE WHEN json_valid(j.payload) THEN json_extract(j.payload,'$.date') END,''),'')=? LIMIT 1",JSON.stringify(['current','legacy']),'current','2026-11-04').toArray().map((r:any)=>r.detail).join('\n');
  expect(plan).toMatch(/SEARCH j USING INDEX .*\(id=\?\)/);expect(plan).not.toMatch(/SCAN j\b/);
 }));
});
