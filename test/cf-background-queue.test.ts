import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject, createExecutionContext } from 'cloudflare:test';
import worker from '../src/worker';
import { BackgroundWorkQueue, BackgroundQueueFull, BACKGROUND_QUEUE_CAPACITY, BACKGROUND_MAX_ATTEMPTS } from '../src/background-work';
import { JobsService } from '../src/jobs';
import { advanceRootIncumbentProjection } from '../src/root-incumbent';

const withHub = (fn: (hub:any,state:any) => Promise<void>|void) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('strict-background-' + crypto.randomUUID())), async (hub:any,state:any) => {
    hub.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
    hub.backgroundWork.cancel('root-slot');
    await hub.refreshBackgroundJobs();
    await state.storage.deleteAlarm();
    await fn(hub,state);
  });
const intent = (id:string,dueAt:number) => ({id,kind:'job-deadline' as const,dueAt,payload:{entity_id:id}});

describe('strict indexed background queue', () => {
  it('pops one due row, preserves an active lease, and fences stale completion', () => withHub((hub,state) => {
    let now=Date.now();
    const queue=new BackgroundWorkQueue(hub.sql,(fn:any)=>state.storage.transactionSync(fn),()=>now);queue.init();
    queue.enqueue(intent('first',now));queue.enqueue(intent('second',now+100000));
    const work=queue.takeDue()!;
    expect(work.id).toBe('first');expect(queue.takeDue()).toBe(null);
    expect(queue.enqueue(intent('first',now-1))).toBe(false); // Existing HTTP consumer keeps its token.
    expect(queue.complete(work)).toBe(true);
    queue.enqueue(intent('first',now));
    const stale=queue.takeDue()!;
    queue.enqueue({...intent('first',now),payload:{entity_id:'replacement'}});
    expect(queue.complete(stale)).toBe(false);
    expect(queue.cancel('first')).toBe(true);
    expect(queue.nextAt()).toBe(now+100000);
  }));

  it('normal waiting survives more than three polls but real failures have a finite retry budget', () => withHub((hub,state) => {
    let now=Date.now();
    const queue=new BackgroundWorkQueue(hub.sql,(fn:any)=>state.storage.transactionSync(fn),()=>now);queue.init();
    queue.enqueue(intent('waiting',now));
    for(let i=0;i<6;i++) {
      const work=queue.takeDue()!;expect(work.exhausted).toBe(false);expect(work.attempts).toBe(1);
      queue.reschedule(work,now+15000);now+=15000;
    }
    queue.cancel('waiting');queue.enqueue(intent('failing',now));
    for(let i=0;i<BACKGROUND_MAX_ATTEMPTS;i++) {
      const work=queue.takeDue()!;expect(work.exhausted).toBe(false);
      queue.reschedule(work,now+15000,true);now+=15000;
    }
    expect(queue.takeDue()?.exhausted).toBe(true);
    expect(queue.nextAt()).toBe(null);
    expect(state.storage.sql.exec('SELECT pending FROM hub_background_count WHERE id=1').one().pending).toBe(0);
  }));

  it('refuses capacity explicitly without a count scan or budget failure', () => withHub((hub,state) => {
    const queue=new BackgroundWorkQueue(hub.sql,(fn:any)=>state.storage.transactionSync(fn));queue.init();
    for(let i=0;i<BACKGROUND_QUEUE_CAPACITY;i++)queue.enqueue(intent('capacity-'+i,Date.now()+100000));
    expect(()=>queue.enqueue(intent('overflow',Date.now()))).toThrow(BackgroundQueueFull);
    expect(hub.rowBudget.status().stopped).toBe(false);
    queue.cancel('capacity-0');expect(queue.enqueue(intent('replacement',Date.now()))).toBe(true);
    const plan=state.storage.sql.exec('EXPLAIN QUERY PLAN SELECT id FROM hub_background_work INDEXED BY hub_background_due WHERE due_ms<=? ORDER BY due_ms,id LIMIT 1',Date.now()).toArray().map((row:any)=>row.detail).join(' | ');
    expect(plan).toContain('hub_background_due');expect(plan).not.toMatch(/TEMP B-TREE/);
  }));

  it('an empty alarm never invokes legacy maintenance services, and cron never opens the DO', () => withHub(async (hub,state) => {
    const legacy=[vi.spyOn(hub.jobsService,'processAlarms'),vi.spyOn(hub.webhookService,'pump'),vi.spyOn(hub.sessionControlService,'processDueWakes'),vi.spyOn(hub,'runChecks')];
    await hub.alarm();
    for(const spy of legacy)expect(spy).not.toHaveBeenCalled();
    expect(await state.storage.getAlarm()).toBe(null);
    const get=vi.fn(()=>{throw new Error('cron must not access the DO');});
    await worker.scheduled({cron:'*/5 * * * *'} as any,{HUB:{get,idFromName:()=>''}} as any,createExecutionContext());
    expect(get).not.toHaveBeenCalled();vi.restoreAllMocks();
  }));

  it('per-entity producers defer a blocked wake without starving the next due wake', () => withHub(async (hub,state) => {
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString();
    for(const id of ['blocked','ready'])raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
      VALUES(?,'test',?,'{}','host','codex','interactive','offered',?,?)`,id,id,at,at);
    for(const id of ['blocked','ready'])raw.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES(?,?,'test',?,?,?,'resume','pending',?,?)`,id,id,id,now-1000,new Date(now-1000).toISOString(),at,at);
    const processed=vi.spyOn(hub.sessionControlService,'processBackground');
    await hub.alarm();await hub.alarm();
    expect(processed.mock.calls.map((call:any[])=>call[0])).toEqual(['blocked','ready']);
    const queued=raw.exec('SELECT id,attempts,due_ms FROM hub_background_work WHERE kind=? ORDER BY id','session-wake').toArray();
    expect(queued).toHaveLength(2);expect(queued.every((row:any)=>row.attempts===0&&row.due_ms>now)).toBe(true);
    vi.restoreAllMocks();
  }));

  it('exhausting one per-job deadline cannot hide another or resurrect on restart seeding', () => withHub(async (hub,state) => {
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString();
    for(let i=0;i<2000;i++)raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor)
      VALUES(?,'writer-run',?,'{}','expired',?,?,'test')`,'retained-'+i,'retained-'+i,at,at);
    for(const [id,offset] of [['failing',2000],['following',1000]] as const)raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,offer_expires_at,actor)
      VALUES(?,'writer-run',?,'{}','offered',?,?,?,'test')`,id,id,at,at,new Date(now-offset).toISOString());
    const offerProbe=raw.exec("SELECT id FROM jobs INDEXED BY jobs_offered WHERE status='offered' ORDER BY offer_expires_at LIMIT 129");
    expect(offerProbe.toArray()).toHaveLength(2);expect(offerProbe.rowsRead).toBeLessThan(8);
    raw.exec("UPDATE hub_background_work SET attempts=3 WHERE id='job-deadline:failing'");
    await hub.alarm(); // quarantine only the repeatedly failing entity
    expect(raw.exec("SELECT status FROM jobs WHERE id='failing'").one().status).toBe('offered');
    await hub.alarm();
    expect(raw.exec("SELECT status FROM jobs WHERE id='following'").one().status).toBe('expired');
    expect(raw.exec("SELECT id FROM hub_background_work WHERE kind='job-deadline'").toArray()).toEqual([]);
    raw.exec("UPDATE jobs SET status=status,offer_expires_at=offer_expires_at WHERE id='failing'");
    raw.exec('UPDATE root_scheduling SET enabled=enabled WHERE id=1');
    hub.backgroundWork.initEntityProducers(); // restoration is once-only, not a retry loop
    expect(raw.exec("SELECT id FROM hub_background_work WHERE kind='job-deadline'").toArray()).toEqual([]);
    raw.exec("UPDATE jobs SET offer_expires_at=? WHERE id='failing'",new Date(now+60000).toISOString());
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='job-deadline:failing'").toArray()).toHaveLength(1);
  }));

  it('cold restoration does not resurrect an exhausted wake without a new source mutation', () => withHub(async (hub,state) => {
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString();
    raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
      VALUES('wake-owner','test','wake-owner','{}','host','codex','interactive','offered',?,?)`,at,at);
    raw.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES('failed-wake','wake-owner','test','failed-wake',?,?,'resume','pending',?,?)`,now-1000,at,at,at);
    raw.exec("UPDATE hub_background_work SET attempts=3 WHERE id='session-wake:failed-wake'");
    await hub.alarm();
    raw.exec("UPDATE session_control_wakes SET state=state,wake_at_ms=wake_at_ms WHERE id='failed-wake'");
    hub.backgroundWork.initEntityProducers();
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='session-wake:failed-wake'").toArray()).toEqual([]);
    raw.exec("UPDATE session_control_wakes SET wake_at_ms=? WHERE id='failed-wake'",now+60000);
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='session-wake:failed-wake'").toArray()).toHaveLength(1);
  }));

  it('failed root deadlines stay fenced across no-op root flag writes and cold restoration', () => withHub(async(hub,state) => {
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString(),future=new Date(now+600000).toISOString();
    raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,offer_expires_at,actor)
      VALUES('fenced-root','root-run','fenced-root','{}','offered',?,?,?,'test')`,at,future,at);
    raw.exec("UPDATE hub_background_work SET attempts=3,due_ms=? WHERE id='job-deadline:fenced-root'",now-1000);
    await hub.alarm();
    raw.exec('UPDATE root_scheduling SET enabled=enabled WHERE id=1');
    raw.exec("UPDATE jobs SET status=status,deadline_at=deadline_at WHERE id='fenced-root'");
    hub.backgroundWork.initEntityProducers();
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='job-deadline:fenced-root'").toArray()).toEqual([]);
    raw.exec("UPDATE jobs SET deadline_at=? WHERE id='fenced-root'",new Date(now+1200000).toISOString());
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='job-deadline:fenced-root'").toArray()).toHaveLength(1);
  }));

  it('targeted deadlines preserve legacy writer, head, root and generic lease names', () => withHub((hub,state)=>{
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString(),future=new Date(now+600000).toISOString(),past=new Date(now-600000).toISOString();
    for(const [id,kind,payload,name] of [['old-writer','writer-run',{date:'2026-11-04'},'writer:2026-11-04'],['old-head','head-recovery',{project:'project'},'head:project'],['old-root','root-run',{},'root'],['old-job','custom',{},'job:old-job']] as const){
      raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,lease_epoch)
        VALUES(?,?,?,?,'claimed',?,?,'test',1)`,id,kind,id,JSON.stringify(payload),at,future);
      raw.exec(`INSERT INTO leases(name,epoch,holder_job_id,expires_at,acquired_at) VALUES(?,1,?,?,?)
        ON CONFLICT(name) DO UPDATE SET epoch=1,holder_job_id=excluded.holder_job_id,expires_at=excluded.expires_at,acquired_at=excluded.acquired_at`,name,id,future,past);
      expect(raw.exec('SELECT due_ms FROM hub_background_work WHERE id=?','job-deadline:'+id).one().due_ms).toBe(Date.parse(past)+300000);
      expect(hub.jobsService.processBackground(id)).toBe(null);
      expect(raw.exec('SELECT status FROM jobs WHERE id=?',id).one().status).toBe('failed');
      expect(raw.exec('SELECT holder_job_id FROM leases WHERE name=?',name).one().holder_job_id).toBe(null);
    }
  }));

  it('known job and lease producers maintain claim deadlines and frozen root offers', () => withHub((hub,state) => {
    const raw=state.storage.sql,now=Date.now(),at=new Date(now).toISOString(),future=new Date(now+600000).toISOString(),past=new Date(now-600000).toISOString();
    raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,offer_expires_at,actor)
      VALUES('root-timer','root-run','root-timer','{}','offered',?,?,?,'test')`,at,future,past);
    const due=()=>raw.exec("SELECT due_ms FROM hub_background_work WHERE id='job-deadline:root-timer'").one().due_ms;
    expect(due()).toBe(Date.parse(future));
    raw.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');expect(due()).toBe(Date.parse(past));
    raw.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');expect(due()).toBe(Date.parse(future));
    raw.exec("UPDATE jobs SET status='claimed',lease_epoch=1 WHERE id='root-timer'");
    raw.exec("UPDATE leases SET epoch=1,holder_job_id='root-timer',expires_at=?,acquired_at=? WHERE name='root'",future,past);
    expect(due()).toBe(Date.parse(past)+300000);
    const changed=raw.exec("UPDATE jobs SET status='running',started_at=? WHERE id='root-timer'",at);changed.toArray();
    expect(changed.rowsRead).toBeLessThan(100);expect(due()).toBe(Date.parse(future));
    raw.exec("UPDATE jobs SET status='done' WHERE id='root-timer'");
    expect(raw.exec("SELECT id FROM hub_background_work WHERE id='job-deadline:root-timer'").toArray()).toEqual([]);
  }));

  it('root slot selects a single ready host and never calls registry or machine censuses', () => withHub((hub,state) => {
    const now=Math.floor(Date.now()/1800000)*1800000+60000,at=new Date(now).toISOString(),slot=new Date(now-60000).toISOString();
    hub.sql.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
    const jobs=new JobsService(hub.sql,state,{now:()=>new Date(now)});
    state.storage.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES('root-ready','root-ready','root-ready',?,?,?,?)",at,at,JSON.stringify({gate:{launch_allowed:true}}),JSON.stringify({jobs:['root-run']}));
    const census=vi.spyOn(jobs,'evaluateRootIncumbent').mockImplementation(()=>{throw new Error('root registry census forbidden');});
    state.storage.transactionSync(()=>advanceRootIncumbentProjection(hub.sql));
    jobs.markRootReady('root-ready');expect(jobs.processRootSlotBackground(slot)).toBe(null);
    const job=state.storage.sql.exec('SELECT status,offer_host_id FROM jobs WHERE idempotency_key=?','root-run:'+slot).one();
    expect(job.status).toBe('offered');expect(job.offer_host_id).toBe('root-ready');expect(census).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  }));
});
