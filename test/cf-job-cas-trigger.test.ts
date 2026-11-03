import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {JobsService} from '../src/jobs';
const fixture=(fn:(hub:any,raw:any,state:any)=>Promise<void>)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('job-cas-trigger-'+crypto.randomUUID())),(hub:any,state:any)=>fn(hub,state.storage.sql,state));
function host(sql:any,id:string,now:number){const at=new Date(now).toISOString();sql.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities)
  VALUES(?,?,?,?,?,?,0,?)`,id,id,id,at,at,JSON.stringify({gate:{launch_allowed:true}}),JSON.stringify({jobs:['root-run']}));}
function offer(sql:any,id:string,machine:string,now:number){sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_machine_id,offer_expires_at)
  VALUES(?,'root-run',?,'{}','offered',?,?,'system',?,?,?)`,id,id,new Date(now).toISOString(),new Date(now+1500000).toISOString(),machine,machine,new Date(now+120000).toISOString());}
function amplify(sql:any){sql.exec('CREATE TABLE cf_job_cas_effects(name TEXT)');sql.exec(`CREATE TRIGGER cf_job_cas_effect AFTER UPDATE ON leases BEGIN
  INSERT INTO cf_job_cas_effects VALUES(NEW.name);INSERT INTO cf_job_cas_effects VALUES(NEW.name);END`);}
describe('job CAS cardinality with billed trigger writes',()=>{
  it('claim, renew and finish use returned lease rows while stale epochs remain fenced',()=>fixture(async(_hub,raw,state)=>{
    let now=Date.now();const jobs=new JobsService(raw,state,{now:()=>new Date(now)});
    host(raw,'host',now);offer(raw,'job','host',now);amplify(raw);
    const claimed=await jobs.claimJob('job','host',true);expect(claimed.error).toBeUndefined();
    expect(claimed.lease_epoch).toBe(1);
    expect((await jobs.startJob('job',claimed.job_token!,1,'sha256:0123456789abcdef','native','task')).error).toBeUndefined();
    now+=60000;
    expect((await jobs.renewJob('job',claimed.job_token!,2)).error).toBe('unauthorized_or_stale');
    expect((await jobs.renewJob('job',claimed.job_token!,1)).error).toBeUndefined();
    expect((await jobs.finishJob('job',claimed.job_token!,1,'succeeded','done',[])).error).toBeUndefined();
    expect(raw.exec("SELECT holder_job_id FROM leases WHERE name='root'").one().holder_job_id).toBe(null);
    expect(Number(raw.exec('SELECT COUNT(*) AS n FROM cf_job_cas_effects').one().n)).toBeGreaterThanOrEqual(6);
  }));

  it('takeover succeeds with amplified writes but the original expected epoch cannot win twice',()=>fixture(async(_hub,raw,state)=>{
    let now=Date.now();const jobs=new JobsService(raw,state,{now:()=>new Date(now)});
    host(raw,'incumbent',now);offer(raw,'old-job','incumbent',now);amplify(raw);
    const claimed=await jobs.claimJob('old-job','incumbent',true);expect(claimed.error).toBeUndefined();
    expect((await jobs.startJob('old-job',claimed.job_token!,claimed.lease_epoch!,'sha256:0123456789abcdef','native','task')).error).toBeUndefined();
    now+=28*60000;host(raw,'replacement',now);offer(raw,'new-job','replacement',now);
    raw.exec("UPDATE machines SET last_checkin_at=? WHERE id='incumbent'",new Date(now-240000).toISOString());
    const takeover=await jobs.takeoverRoot('new-job','replacement',claimed.lease_epoch!);
    expect(takeover.success).toBe(true);expect(takeover.lease_epoch).toBe(claimed.lease_epoch!+1);
    expect((await jobs.takeoverRoot('new-job','replacement',claimed.lease_epoch!)).error).toBe('cas_failed');
    expect(raw.exec("SELECT status FROM jobs WHERE id='old-job'").one().status).toBe('fenced');
  }));
});
