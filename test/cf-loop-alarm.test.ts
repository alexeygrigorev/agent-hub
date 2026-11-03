import { describe, expect, it } from 'vitest';
import worker from '../src/worker';
import { env, createExecutionContext, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import { expiryMsSql } from '../src/jobs';

const withHub = (fn: (hub: Hub, state: DurableObjectState) => Promise<void>) =>
  runInDurableObject((env as any).HUB.get((env as any).HUB.idFromName('cf-loop-' + crypto.randomUUID())), fn);

const plan = (hub: Hub, q: string, ...args: any[]) => (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...args).toArray() as any[])
  .map(row => String(row.detail)).join('\n');

describe('CF runaway alarm regression', () => {
  it('bounds host offer polls and advances past expired prefixes without starving an older live offer', () => withHub(async (hub,state) => {
    const sql=state.storage.sql, now=Date.now();
    for(let i=0;i<221;i++) sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_machine_id,offer_expires_at)
      VALUES(?,'root-run',?,'{}','offered',?,?,'test','a','a',?)`,
      'poll-'+i,'poll-'+i,new Date(now-i*1000).toISOString(),new Date(now+3600000).toISOString(),new Date(now+(i===220?3600000:-3600000)).toISOString());
    const pages:any[]=[];
    for(let i=0;i<3;i++) pages.push(hub.meter.runSync('probe:offers',()=>hub.jobsService.offeredForHost('a')));
    expect(pages[0]).toMatchObject({rows:[],truncated:true});
    expect(pages[1]).toMatchObject({rows:[],truncated:true});
    expect(pages[2].rows.map((r:any)=>r.id)).toEqual(['poll-220']);
    expect(pages[2].truncated).toBe(false);
    expect(hub.meter.snapshot()['probe:offers'].rows_read).toBeLessThan(1500);
    for(const column of ['offer_host_id','offer_machine_id']) expect(plan(hub,
      `SELECT * FROM jobs WHERE status='offered' AND ${column}=? ORDER BY created_at DESC,id DESC LIMIT 101`,'a'))
      .not.toMatch(/TEMP B-TREE/);
  }));
  it('constructs the exported conversation Hub through the real Worker fetch path', async () => {
    const id = (env as any).HUB.idFromName('cf-worker-construction-' + crypto.randomUUID());
    const bindings = { ...(env as any), HUB: {
      idFromName: () => id,
      get: (objectId: any) => (env as any).HUB.get(objectId),
    } };
    const response = await worker.fetch(new Request('https://hub.test/api/conversations'), bindings as any, createExecutionContext());
    expect([401, 403]).toContain(response.status);
    await (runInDurableObject as any)((env as any).HUB.get(id), async (hub: any) => {
      expect(hub.conversationsService).toBeDefined();
      expect(hub.mcpOAuth).toBeDefined();
      expect(hub.mcpUsage).toBeDefined();
      expect(hub.rowBudget.status().stopped).toBe(false);
    });
  });

  it('does not re-arm an empty Hub, and overdue blocked work cannot re-arm at 1ms', () => withHub(async (hub, state) => {
    const schedule = (hub as any).scheduleDueAlarm.bind(hub);
    await state.storage.deleteAlarm();
    await schedule([], false);
    expect(await state.storage.getAlarm()).toBe(null);
    const before = Date.now();
    await schedule([before - 60000]);
    expect((await state.storage.getAlarm())!).toBeGreaterThanOrEqual(before + 15000);
    await state.storage.deleteAlarm();
    await schedule([]);
    expect(await state.storage.getAlarm()).toBe(null);
  }));

  it('expires non-root offers once without a repeating past alarm or changing another role holder', () => withHub(async (hub, state) => {
    const now = Date.now(), at = new Date(now).toISOString(), past = new Date(now - 60000).toISOString();
    const future = new Date(now + 60000).toISOString(), leaseFuture = new Date(now + 3600000).toISOString();
    const raw = state.storage.sql;
    raw.exec("INSERT INTO leases(name,holder_job_id,epoch,acquired_at,expires_at) VALUES('automation:demo:writer','other-holder',7,?,?)", at, leaseFuture);
    for (const kind of ['writer-run','principal-wake','head-recovery']) raw.exec(`INSERT INTO jobs
      (id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_expires_at,role_name,role_generation)
      VALUES(?,?,?,'{}','offered',?,?,'test',?,'automation:demo:writer',3)`, 'expired-' + kind, kind, 'expired-' + kind, at, leaseFuture, past);
    raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_expires_at)
      VALUES('future-writer','writer-run','future-writer','{}','offered',?,?,'test',?)`, at, leaseFuture, future);
    expect(hub.jobsService.getAlarmTime()).toBe(now - 60000);
    for (let i = 0; i < 3; i++) expect(hub.jobsService.processAlarms()).toBe(now + 60000);
    for (const kind of ['writer-run','principal-wake','head-recovery']) expect(raw.exec('SELECT status FROM jobs WHERE id=?', 'expired-' + kind).toArray()[0].status).toBe('expired');
    expect(raw.exec("SELECT COUNT(*) AS n FROM health_events WHERE detail='offer expired'").toArray()[0].n).toBe(3);
    const lease: any = raw.exec("SELECT holder_job_id,epoch FROM leases WHERE name='automation:demo:writer'").toArray()[0];
    expect(lease).toEqual({ holder_job_id: 'other-holder', epoch: 7 });
    hub.sql.exec("UPDATE jobs SET status='cancelled' WHERE id='future-writer'");
    hub.sql.exec("UPDATE leases SET holder_job_id=NULL WHERE name='automation:demo:writer'");
    expect(hub.jobsService.processAlarms()).toBe(null);
    await hub.alarm();
    expect(await state.storage.getAlarm()).toBe(null);
    expect(plan(hub, `SELECT id FROM jobs WHERE status='offered' AND kind<>'root-run' AND ${expiryMsSql('offer_expires_at')}<=? ORDER BY ${expiryMsSql('offer_expires_at')} LIMIT 100`, now))
      .toContain('jobs_nonroot_offered_due_ms');
  }));

  it('preserves expiry formats while seeking indexed due jobs and leases', () => withHub(async hub => {
    const ms = expiryMsSql('offer_expires_at');
    const now = Date.now();
    for (const value of [new Date(now).toISOString(), String(now), String(Math.floor(now / 1000))]) {
      const row: any = hub.sql.exec(`WITH deadline(offer_expires_at) AS (VALUES (?)) SELECT ${ms} AS due FROM deadline`, value).toArray()[0];
      expect(Math.abs(row.due - now)).toBeLessThan(1000);
    }
    const p = plan(hub, `SELECT * FROM jobs WHERE kind='root-run' AND status='offered' AND ${ms}<=? ORDER BY ${ms} LIMIT ?`, now, 100);
    expect(p).toContain('jobs_root_offered_due_ms');
    expect(p).not.toMatch(/TEMP B-TREE/);
    const leaseMs = `COALESCE(${expiryMsSql('expires_at')},0)`;
    expect(plan(hub, `SELECT * FROM leases WHERE holder_job_id IS NOT NULL AND ${leaseMs}<=? ORDER BY ${leaseMs} LIMIT ?`, now, 100))
      .toContain('leases_active_due_ms');
  }));

  it('pending-work probes do not read retained job or delivery history', () => withHub(async (hub, state) => {
    const at = new Date().toISOString();
    // Fixture seeding uses native SQL intentionally; billed application work
    // is measured only after setup, and no application cache was primed.
    const fixture = state.storage.sql;
    for (let i = 0; i < 500; i++) {
      fixture.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor) VALUES(?,'root-run',?,'{}','succeeded',?,?,'test')", 'done-' + i, 'done-' + i, at, at);
      fixture.exec("INSERT INTO session_deliveries(id,source,source_id,delivery_key,target_label,body,status,created_at,updated_at) VALUES(?,'webhook','gone',?,'gone','x','delivered',?,?)", 'done-' + i, 'done-' + i, at, at);
    }
    const before = hub.meter.snapshot()['probe:pending']?.rows_read ?? 0;
    await hub.meter.run('probe:pending', async () => {
      expect(hub.jobsService.getAlarmTime()).toBe(null);
      expect(await hub.webhookService.pump()).toBe(undefined);
      expect(hub.sessionControlService.nextWakeTime()).toBe(null);
    });
    const rows = (hub.meter.snapshot()['probe:pending']?.rows_read ?? 0) - before;
    console.log('cf-loop idle probes with 500 terminal jobs/deliveries rows_read=' + rows);
    expect(rows).toBeLessThan(40);
  }));

  it('claim deadline probes and expiry never scan thousands of running leases', () => withHub(async (hub, state) => {
    const now = Date.now(), at = new Date(now).toISOString();
    const acquired = new Date(now - 600000).toISOString(), future = new Date(now + 3600000).toISOString();
    const raw = state.storage.sql;
    for (let i = 0; i < 2000; i++) {
      const id = 'running-' + i;
      raw.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,started_at) VALUES(?,'role-run',?,'{}','running',?,?,'test',?)", id, id, at, future, at);
      raw.exec('INSERT INTO leases(name,holder_job_id,acquired_at,expires_at) VALUES(?,?,?,?)', id, id, acquired, future);
    }
    raw.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor) VALUES('claim-last','role-run','claim-last','{}','claimed',?,?,'test')", at, future);
    raw.exec("INSERT INTO leases(name,holder_job_id,acquired_at,expires_at) VALUES('zz-last','claim-last',?,?)", acquired, future);
    const before = hub.meter.snapshot()['probe:claims']?.rows_read ?? 0;
    await hub.meter.run('probe:claims', async () => {
      expect(hub.jobsService.getAlarmTime()).toBe(now - 300000);
      hub.jobsService.processAlarms();
    });
    const rows = (hub.meter.snapshot()['probe:claims']?.rows_read ?? 0) - before;
    console.log('cf-loop claim seek/process with 2000 running leases rows_read=' + rows);
    expect(rows).toBeLessThan(100);
    expect(raw.exec("SELECT status FROM jobs WHERE id='claim-last'").toArray()[0].status).toBe('failed');
    expect(raw.exec('SELECT lease_name FROM job_claim_deadlines').toArray()).toHaveLength(0);
    expect(plan(hub, 'SELECT due_ms FROM job_claim_deadlines ORDER BY due_ms,lease_name LIMIT 1')).toContain('job_claim_deadlines_due');
    expect(plan(hub, "UPDATE credentials SET revoked_at=? WHERE subject=? AND kind='job' AND revoked_at IS NULL", at, 'claim-last')).toContain('credentials_job_subject_live');
    // Reproduce an upgrade with pre-existing leases, including the claim
    // after all running rows. Migration pages must progress, not starve it.
    raw.exec("UPDATE jobs SET status='claimed',finished_at=NULL WHERE id='claim-last'");
    raw.exec("UPDATE leases SET holder_job_id='claim-last',acquired_at=?,expires_at=? WHERE name='zz-last'", acquired, future);
    hub.sql.exec('DELETE FROM job_claim_deadlines');
    hub.sql.exec("UPDATE job_claim_backfill SET after_name='',done=0 WHERE id=1");
    for (let i = 0; i < 21; i++) {
      const scope = 'probe:claim-migration-' + i;
      hub.meter.runSync(scope, () => hub.jobsService.processAlarms());
      expect(hub.meter.snapshot()[scope]?.rows_read ?? 0).toBeLessThan(500);
    }
    expect(raw.exec('SELECT done FROM job_claim_backfill WHERE id=1').toArray()[0].done).toBe(1);
    expect(raw.exec("SELECT status FROM jobs WHERE id='claim-last'").toArray()[0].status).toBe('failed');
  }));

  it('wake queue capacity counts stop at the configured threshold', () => withHub(async (hub, state) => {
    const now = Date.now(), at = new Date(now).toISOString(), raw = state.storage.sql;
    raw.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at) VALUES('wake-session','test','wake-session','{}','codex','interactive','running',?,?)", at, at);
    for (let i = 0; i < 5000; i++) raw.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
      VALUES(?,'wake-session','send','test',?,'{}','recorded',0,'test',?,?)`, 'queue-' + i, 'queue-' + i, at, at);
    raw.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
      VALUES('due-wake','wake-session','test','due-wake',?,?,'resume','pending',?,?)`, now, at, at, at);
    (hub.sessionControlService as any).maxPending = 20;
    const before = hub.meter.snapshot()['probe:wake-cap']?.rows_read ?? 0;
    hub.meter.runSync('probe:wake-cap', () => hub.sessionControlService.processDueWakes());
    const rows = (hub.meter.snapshot()['probe:wake-cap']?.rows_read ?? 0) - before;
    console.log('cf-loop wake cap20 with 5000 queued commands rows_read=' + rows);
    expect(rows).toBeLessThan(80);
    expect(raw.exec("SELECT state FROM session_control_wakes WHERE id='due-wake'").toArray()[0].state).toBe('pending');
  }));

  it('pages idle recovery without clearing episodes outside the evaluated page', () => withHub(async (hub, state) => {
    const at = new Date().toISOString();
    const fixture = state.storage.sql;
    for (let i = 0; i < 120; i++) {
      const id = 'page-' + String(i).padStart(3, '0');
      fixture.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at) VALUES(?,'test',?,'{}','codex','interactive','running',?,?)", id, id, at, at);
      fixture.exec("INSERT INTO session_idle_episodes(session_id,episode,detected_at,active,nudges_json) VALUES(?,?,?,1,'[]')", id, id, Date.now());
    }
    const seen: string[][] = [];
    hub.sessionControlService.attention = (ids?: string[]) => { seen.push(ids ?? []); return []; };
    await (hub as any).idleWork.tick();
    expect(seen[0]).toHaveLength(50);
    expect(fixture.exec("SELECT active FROM session_idle_episodes WHERE session_id='page-049'").toArray()[0].active).toBe(0);
    expect(fixture.exec("SELECT active FROM session_idle_episodes WHERE session_id='page-050'").toArray()[0].active).toBe(1);
    await (hub as any).idleWork.tick();
    expect(seen[1][0]).toBe('page-050');
    expect(seen[1]).toHaveLength(50);
    await (hub as any).idleWork.tick();
    expect(seen[2]).toHaveLength(20);
    expect(fixture.exec("SELECT active FROM session_idle_episodes WHERE session_id='page-119'").toArray()[0].active).toBe(0);
  }));

  it('delivery queue ordering and next ping seek use covering indexes', () => withHub(async hub => {
    expect(plan(hub, "SELECT * FROM session_deliveries WHERE status='queued' ORDER BY updated_at LIMIT 50"))
      .toContain('session_deliveries_queued_order');
    expect(plan(hub, "SELECT next_attempt_at FROM session_deliveries WHERE status='pending' ORDER BY next_attempt_at LIMIT 1"))
      .toContain('session_deliveries_due');
    expect(plan(hub, "SELECT next_at_ms FROM session_control_pings WHERE state='active' ORDER BY next_at_ms LIMIT 1"))
      .toContain('session_control_pings_due');
  }));
});
