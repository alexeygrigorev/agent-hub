import { fixtureStatements,registerFixtureStatements,registerFixtureStatementAlias } from './helpers/fixture-statements';
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { CAPACITY_MACHINES_SQL } from '../src/fleet-capacity-snapshot';
import { liveQueuePolicy } from '../src/intake-queue-policy';
import { freshCpu } from './cpu-fixture';

const now = Date.parse('2026-11-01T10:00:00Z');
const iso = (delta = 0) => new Date(now + delta).toISOString();
const settings = { HUB_INTAKE_QUEUE_ENABLED:'true', HUB_INTAKE_QUEUE_PROJECTS:'*',
  HUB_INTAKE_QUEUE_MAX_INFLIGHT:'auto', HUB_INTAKE_QUEUE_MAX_PER_PROJECT:'auto' };
async function fixture(fn: (s: FleetService, sql: any, queries: {query:string,args:any[]}[]) => void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('bounded-capacity-' + crypto.randomUUID()));
  await (runInDurableObject as any)(stub, (h: any, state: DurableObjectState) => {
    registerFixtureStatements(h);
    const queries: {query:string,args:any[]}[] = [];
    const sql = {exec(query:string,...args:any[]) {
      expect(args.length).toBeLessThanOrEqual(100);
      queries.push({query,args}); return h.sql.exec(query,...args);
    }};
    registerFixtureStatementAlias(sql,h,state);
    const s = new FleetService(sql as any, fn=>state.storage.transactionSync(fn), ()=>now); s.init();
    fixtureStatements(sql).exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('zai-main','zai',10,15,1)");
    machine(sql,'online');
    fixtureStatements(sql).exec("INSERT INTO fleet_routes(machine_id,engine,account,profile) VALUES('online','zcodex','zai-main',NULL)");
    fixtureStatements(sql).exec(`INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,routes_json)
      VALUES('online','1.0',?,?,?,?,'ok',?)`,iso(),now,iso(),now,JSON.stringify({zai:{status:'ok',observed_at:iso(),
      details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}}}));
    fixtureStatements(sql).exec("INSERT INTO fleet_catalog_seen VALUES('online',?)",iso());
    fixtureStatements(sql).exec("INSERT INTO fleet_catalog_census(machine_id,observed_at,complete) VALUES('online',?,1)",iso());
    queries.length=0; fn(s,sql,queries);
  });
}
function machine(sql:any,id:string,drained=0) {
  fixtureStatements(sql).exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities)
    VALUES(?,?,?,?,?,?,?,?)`,id,id,id,iso(),iso(),JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,cpu_usage:freshCpu(iso())}),drained,
    JSON.stringify({engines:['zcodex'],cpu_count:8,workspaces:['real-project']}));
}
function launch(sql:any,id:string,state='offered',native:string|null=null) {
  fixtureStatements(sql).exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,native_id)
    VALUES(?,'test','human','test',?,'{}','online','zcodex','zai-main','headless','real-project','',?,?,?,?)`,id,id,state,iso(),iso(),native);
}
function native(sql:any,id:string,activity=iso()) {
  fixtureStatements(sql).exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,account,last_activity_at)
    VALUES('online',?,?,'zcodex','running',?,'real-project','zai-main',?)`,id,'external-'+id,iso(),activity);
}
function absentBoundLaunch(sql:any,state='uncertain',nativeId:string|null=null) {
  launch(sql,'absent-launch',state,nativeId);
  native(sql,'absent-native');
  fixtureStatements(sql).exec("UPDATE fleet_sessions SET phase='unobserved',created_at_ms=? WHERE native_id='absent-native'",now);
  controlBinding(sql,'exact-binding');
}
function controlBinding(sql:any,id:string) {
  fixtureStatements(sql).exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES(?,'fixture',?,'{}','absent-launch','online','zcodex','headless','uncertain',?,?,?)`,
    id,id,JSON.stringify({native_id:'absent-native',native_created_at_ms:now}),iso(),iso());
}
describe('bounded real Fleet autonomous capacity snapshot',()=>{
  it('uses real quota/account/resource admission without global maintenance or writes',()=>fixture((s,sql,queries)=>{
    (s as any).reconcileCapacity=()=>{throw new Error('global maintenance forbidden');};
    const result=liveQueuePolicy(settings,sql,s,now);
    expect(result.max_inflight).toBe(10);
    expect((result as any).capacity).toMatchObject({account_slots:10,available_slots:10});
    expect(queries.every(q=>/^\s*(SELECT|WITH)/i.test(q.query))).toBe(true);
    expect(queries.some(q=>q.query.includes('FROM machines WHERE revoked_at'))).toBe(false);
    expect(queries.some(q=>q.query.includes('session_cpu_usage'))).toBe(false);
  }));
  it('preserves exact native reservation deduplication and idle catalog accounting',()=>fixture((s,sql)=>{
    launch(sql,'bound','running','native'); native(sql,'native');
    native(sql,'orphan'); native(sql,'idle',iso(-3600000)); launch(sql,'pending');
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    const normal=s.accountStatus(a);
    expect(bounded).toMatchObject({active:3,reserved:1,observed_sessions:2,native_live:3});
    expect(bounded).toEqual(normal);
    // A fresh legacy receipt alone is not complete-census evidence.
    fixtureStatements(sql).exec('DELETE FROM fleet_catalog_census');
    expect(s.accountStatus(a,s.queueCapacitySnapshot(now))).toMatchObject({active:4,reserved:1,observed_sessions:3,native_live:null});
  }));
  it('releases a running reservation whose exact native is absent from a fresh complete census',()=>fixture((s,sql)=>{
    launch(sql,'stale-bound','running','stale-bound-native');
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    expect(bounded).toMatchObject({active:0,available_slots:10,reserved:0,observed_sessions:0,native_live:0});
    expect(bounded).toEqual(s.accountStatus(a));
    // Capacity inspection is read-only: the launch history remains for reconciliation.
    expect(fixtureStatements(sql).exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE phase='running'").one().n).toBe(0);
    expect(fixtureStatements(sql).exec("SELECT state FROM fleet_launches WHERE id='stale-bound'").one().state).toBe('running');
  }));
  it.each(['missing','incomplete','stale','future'])('retains a missing-native reservation without usable census evidence: %s',kind=>fixture((s,sql)=>{
    launch(sql,'unproven-bound','running','unproven-native');
    if(kind==='missing') fixtureStatements(sql).exec('DELETE FROM fleet_catalog_census');
    if(kind==='incomplete') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET complete=0');
    if(kind==='stale') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET observed_at=?',iso(-180001));
    if(kind==='future') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET observed_at=?',iso(60001));
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    expect(s.accountStatus(a,s.queueCapacitySnapshot(now))).toMatchObject({active:1,reserved:1,native_live:null});
    expect(fixtureStatements(sql).exec("SELECT state FROM fleet_launches WHERE id='unproven-bound'").one().state).toBe('running');
  }));
  it.each(['missing','incomplete','stale','future'])('keeps idle reservations without a usable complete census: %s',kind=>fixture((s,sql)=>{
    launch(sql,'idle-bound','running','idle-native'); native(sql,'idle-native',iso(-3600000));
    native(sql,'idle-orphan',iso(-3600000));
    if(kind==='missing') fixtureStatements(sql).exec('DELETE FROM fleet_catalog_census');
    if(kind==='incomplete') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET complete=0');
    if(kind==='stale') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET observed_at=?',iso(-180001));
    if(kind==='future') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET observed_at=?',iso(60001));
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    expect(bounded).toMatchObject({active:2,reserved:0,observed_sessions:2,native_live:null});
    expect(bounded).toEqual(s.accountStatus(a));
  }));
  it('expires unmatched reservations after the bounded reservation TTL while keeping observed live sessions active',()=>fixture((s,sql)=>{
    launch(sql,'stale-offered','offered',null);
    launch(sql,'stale-starting','starting',null);
    launch(sql,'stale-uncertain','uncertain',null);
    fixtureStatements(sql).exec("UPDATE fleet_launches SET updated_at=? WHERE id IN ('stale-offered','stale-starting','stale-uncertain')",iso(-15*60000-1));
    launch(sql,'fresh-starting','starting',null);
    launch(sql,'old-bound','running','live-native');
    fixtureStatements(sql).exec("UPDATE fleet_launches SET updated_at=? WHERE id='old-bound'",iso(-2*3600000));
    native(sql,'live-native');
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    expect(bounded).toMatchObject({active:2,reserved:1,observed_sessions:1,native_live:1});
    expect(bounded).toEqual(s.accountStatus(a));
  }));
  it.each(['running','uncertain'])('releases only physical absence with complete census for %s launches',state=>fixture((s,sql)=>{
    absentBoundLaunch(sql,state,state==='running'?'absent-native':null);
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    expect(bounded).toMatchObject({active:0,reserved:0,observed_sessions:0,native_live:0});
    expect(bounded).toEqual(s.accountStatus(a));
    expect(fixtureStatements(sql).exec("SELECT state FROM fleet_launches WHERE id='absent-launch'").one().state).toBe(state);
  }));
  it.each(['incomplete','ambiguous','missing','engine','account','incarnation','malformed','direct-uncertain'])('retains uncertain reservations when absence proof is %s',kind=>fixture((s,sql)=>{
    absentBoundLaunch(sql);
    if(kind==='incomplete') fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET complete=0');
    if(kind==='ambiguous') controlBinding(sql,'duplicate-binding');
    if(kind==='missing') fixtureStatements(sql).exec('DELETE FROM session_control_sessions');
    if(kind==='engine') fixtureStatements(sql).exec("UPDATE fleet_sessions SET engine='claude' WHERE native_id='absent-native'");
    if(kind==='account') fixtureStatements(sql).exec("UPDATE fleet_sessions SET account='foreign' WHERE native_id='absent-native'");
    if(kind==='incarnation') fixtureStatements(sql).exec("UPDATE fleet_sessions SET created_at_ms=? WHERE native_id='absent-native'",now-1);
    if(kind==='malformed') fixtureStatements(sql).exec("UPDATE session_control_sessions SET binding_json='invalid'");
    if(kind==='direct-uncertain') fixtureStatements(sql).exec("UPDATE fleet_launches SET native_id='absent-native' WHERE id='absent-launch'");
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    const bounded=s.accountStatus(a,s.queueCapacitySnapshot(now));
    expect(bounded).toMatchObject({active:1,reserved:1,observed_sessions:0});
    expect(bounded).toEqual(s.accountStatus(a));
    expect(fixtureStatements(sql).exec("SELECT state FROM fleet_launches WHERE id='absent-launch'").one().state).toBe('uncertain');
  }));
  it.each([null,''])('preserves a one-agent machine ceiling while releasing a proven absent binding with native_id=%s',nativeId=>fixture((s,sql)=>{
    absentBoundLaunch(sql,'uncertain',nativeId);
    fixtureStatements(sql).exec("INSERT INTO routing_machines(machine_id,needs_json,max_agents,observed_at) VALUES('online','[]',1,?)",iso());
    expect(liveQueuePolicy(settings,sql,s,now)).toMatchObject({max_inflight:1,capacity:{machine_slots:1,available_slots:1}});
    fixtureStatements(sql).exec('UPDATE fleet_catalog_census SET complete=0');
    expect(liveQueuePolicy(settings,sql,s,now)).toMatchObject({max_inflight:0,capacity:{machine_slots:0,available_slots:0}});
    expect(fixtureStatements(sql).exec("SELECT max_agents FROM routing_machines WHERE machine_id='online'").one().max_agents).toBe(1);
  }));
  it('bounds duplicate binding reads despite retained terminal controls',()=>fixture((s,sql)=>{
    absentBoundLaunch(sql);
    for(let i=0;i<400;i++) controlBinding(sql,'history-'+i);
    fixtureStatements(sql).exec("UPDATE session_control_sessions SET state='exited'");
    for(let i=0;i<200;i++) controlBinding(sql,'duplicate-'+i);
    const measured:any[]=[]; const exec=sql.exec.bind(sql);
    sql.exec=(query:string,...args:any[])=>{
      const cursor=exec(query,...args);
      if(query.includes('FROM fleet_launches l') && query.includes('FROM session_control_sessions c')) measured.push({cursor,query,args});
      return cursor;
    };
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    expect(s.accountStatus(a,s.queueCapacitySnapshot(now))).toMatchObject({active:1,reserved:1});
    expect(measured).toHaveLength(1);
    expect(Number(measured[0].cursor.rowsRead)).toBeLessThan(40);
    const plan=exec('EXPLAIN QUERY PLAN '+measured[0].query,...measured[0].args).toArray().map((r:any)=>r.detail).join('\n');
    expect(plan).toMatch(/SEARCH c USING INDEX session_control_capacity_binding/);
  }));
  it('fails closed on 99 live hosts before account status and keeps every binding batch below 100',()=>fixture((s,sql,queries)=>{
    for(let i=0;i<98;i++) machine(sql,'live-'+i);
    const a=fixtureStatements(sql).exec("SELECT * FROM fleet_accounts WHERE label='zai-main'").one();
    // Ordinary Fleet status also needs safe binding chunks with 99 live hosts.
    expect(s.accountStatus(a).active).toBe(0);
    let calls=0; s.accountStatus=()=>{calls++;throw new Error('must not reach accounts');};
    queries.length=0;
    expect(liveQueuePolicy(settings,sql,s,now)).toMatchObject({max_inflight:0,capacity:{available_slots:0,observation_limited:true}});
    expect(calls).toBe(0);
    expect(queries.filter(q=>q.query.includes('fleet_accounts'))).toHaveLength(0);
  }));
  it.each(['routes','launches','natives'])('fails closed before account projection on overflowing %s',kind=>fixture((s,sql)=>{
    for(let i=0;i<257;i++) {
      if(kind==='routes') fixtureStatements(sql).exec("INSERT INTO fleet_routes(machine_id,engine,account,profile) VALUES(?,'zcodex','zai-main',NULL)",'historical-'+i);
      if(kind==='launches') launch(sql,'launch-'+i);
      if(kind==='natives') native(sql,'native-'+i);
    }
    let calls=0; s.accountStatus=()=>{calls++;throw new Error('must not reach accounts');};
    expect(liveQueuePolicy(settings,sql,s,now).max_inflight).toBe(0); expect(calls).toBe(0);
  }));
  it.each(['accounts','engines'])('rejects overflowing %s before account projection',kind=>fixture((s,sql)=>{
    if(kind==='accounts') for(let i=0;i<128;i++) fixtureStatements(sql).exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES(?,'zai',1,15,1)",'account-'+i);
    if(kind==='engines') for(let i=0;i<17;i++) {launch(sql,'engine-'+i);fixtureStatements(sql).exec('UPDATE fleet_launches SET engine=? WHERE id=?','engine-'+i,'engine-'+i);}
    let calls=0; s.accountStatus=()=>{calls++;throw new Error('must not reach accounts');};
    expect(liveQueuePolicy(settings,sql,s,now)).toMatchObject({max_inflight:0,capacity:{observation_limited:true}});
    expect(calls).toBe(0);
  }));
  it('uses range/index probes despite retained drained machines and terminal history',()=>fixture((s,sql,queries)=>{
    for(let i=0;i<300;i++) { machine(sql,'drained-'+i,1); launch(sql,'old-'+i,'exited'); }
    const plan=fixtureStatements(sql).exec('EXPLAIN QUERY PLAN '+CAPACITY_MACHINES_SQL,iso(-360000),iso(60000)).toArray();
    const detail=plan.map((r:any)=>r.detail).join('\n');
    expect(detail).toMatch(/SEARCH m USING INDEX fleet_queue_live_machines/);
    expect(detail).not.toMatch(/SCAN m/);
    queries.length=0;
    expect(liveQueuePolicy(settings,sql,s,now).max_inflight).toBe(10);
    for(const q of queries.filter(q=>q.query.includes('FROM fleet_launches l LEFT JOIN fleet_sessions'))) {
      const p=fixtureStatements(sql).exec('EXPLAIN QUERY PLAN '+q.query,...q.args).toArray().map((r:any)=>r.detail).join('\n');
      expect(p).toMatch(/SEARCH l USING INDEX fleet_launch_machine_state/);
    }
  }));
  it('atomically upgrades the legacy runtime index while preserving its lookup identity and bounded reads',()=>fixture((s,sql,queries)=>{
    // Isolate this index-upgrade test from the per-host live-session cap:
    // the 400 running scale rows below are query-plan scaffolding, not a
    // real 400-session host. The cap itself is covered in cpu-headroom tests.
    // (The setting validates to [1,512]; the 400 scale rows fit under 500.)
    s.resources.configure({host_max_sessions:500});
    fixtureStatements(sql).exec('DROP INDEX fleet_launch_runtime_exit');
    fixtureStatements(sql).exec("CREATE INDEX fleet_launch_runtime_exit ON fleet_launches(machine_id,updated_at) WHERE exit_reason IN ('engine_runtime_timeout','engine_signal_exit')");
    fixtureStatements(sql).exec("CREATE INDEX IF NOT EXISTS fleet_queue_runtime_exit ON fleet_launches(machine_id,updated_at DESC) WHERE state IN ('failed','exited') AND exit_reason IN ('engine_runtime_timeout','engine_signal_exit')");
    s.init();
    const schema=String(fixtureStatements(sql).exec("SELECT sql FROM sqlite_master WHERE name='fleet_launch_runtime_exit'").one().sql);
    expect(schema).toContain("WHERE state IN ('failed','exited') AND exit_reason");
    expect(fixtureStatements(sql).exec("SELECT name FROM sqlite_master WHERE name='fleet_queue_runtime_exit'").toArray()).toEqual([]);
    for(let i=0;i<400;i++) launch(sql,'runtime-'+i,'running');
    fixtureStatements(sql).exec("UPDATE fleet_launches SET exit_reason='engine_runtime_timeout',updated_at=?",iso());
    fixtureStatements(sql).exec("UPDATE fleet_launches SET state='failed',updated_at=? WHERE id='runtime-399'",iso(-1));
    const query=`SELECT exit_reason,updated_at FROM fleet_launches WHERE machine_id=? AND updated_at>=?
      AND state IN ('failed','exited') AND exit_reason IN ('engine_runtime_timeout','engine_signal_exit')
      ORDER BY updated_at DESC LIMIT 1`;
    const plan=fixtureStatements(sql).exec('EXPLAIN QUERY PLAN '+query,'online',iso(-60000)).toArray().map((r:any)=>r.detail).join('\n');
    expect(plan).toContain('fleet_launch_runtime_exit');
    expect(plan).not.toMatch(/SCAN fleet_launches|TEMP B-TREE/);
    const cursor=fixtureStatements(sql).exec(query,'online',iso(-60000));
    expect(cursor.toArray()).toEqual([{exit_reason:'engine_runtime_timeout',updated_at:iso(-1)}]);
    expect(Number(cursor.rowsRead)).toBeLessThanOrEqual(8);
    expect(s.resources.capacityCpuAdmitted('online')).toBe(false);
    fixtureStatements(sql).exec("UPDATE fleet_launches SET state='running' WHERE id='runtime-399'");
    expect(s.resources.capacityCpuAdmitted('online')).toBe(true);
    queries.length=0; s.init();
    expect(queries.some(q=>q.query==='DROP INDEX fleet_launch_runtime_exit')).toBe(false);
    fixtureStatements(sql).exec('DROP INDEX fleet_launch_runtime_exit');
    fixtureStatements(sql).exec("CREATE INDEX fleet_launch_runtime_exit ON fleet_launches(updated_at,machine_id) WHERE state IN ('failed','exited') AND exit_reason IN ('engine_runtime_timeout','engine_signal_exit')");
    s.init();
    const repairedPlan=fixtureStatements(sql).exec('EXPLAIN QUERY PLAN '+query,'online',iso(-60000)).toArray().map((r:any)=>r.detail).join('\n');
    expect(repairedPlan).toMatch(/machine_id=\?.*updated_at>\?/);
  }));
  it('retains hard PSI/OOM admission while ordinary busy CPU remains advisory',()=>fixture((s,sql)=>{
    const update=(cpu:any)=>fixtureStatements(sql).exec("UPDATE machines SET last_heartbeat=? WHERE id='online'",JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,cpu_usage:cpu}));
    update(freshCpu(iso(),8,8));
    expect(s.resources.capacityCpuAdmitted('online')).toBe(s.resources.cpuHeadroom('online').admitted);
    expect(liveQueuePolicy(settings,sql,s,now).max_inflight).toBe(10);
    update({...freshCpu(iso()),psi_full_avg10:100});
    expect(s.resources.capacityCpuAdmitted('online')).toBe(false);
    expect(liveQueuePolicy(settings,sql,s,now).max_inflight).toBe(0);
    update({...freshCpu(iso()),host_oom_kills_delta:1});
    expect(s.resources.capacityCpuAdmitted('online')).toBe(false);
    expect(liveQueuePolicy(settings,sql,s,now).max_inflight).toBe(0);
  }));
});
