import {intakeLinkHealth} from '../src/intake-link-health';
import {buildSessionInputs,classifyKey,LifecycleObservationLimit} from '../src/session_lifecycle_registry';
import {fixtureSql} from './helpers/fixture-budget';
import { describe,expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { reservedLaunchPage,parseReservedLaunchCursor } from '../src/account-reserved-launches';
import { SILENT_START_REASON } from '../src/silent-start';
import { RowsMeter } from '../src/rows-meter';
import { reconcileVerifiedNoStarts } from '../src/account_capacity';

const NOW=Date.parse('2026-11-03T21:00:00Z'),AT=new Date(NOW).toISOString();
const fixture=(fn:(hub:any,raw:any)=>Promise<void>)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('fleet-existing-'+crypto.randomUUID())),async(hub:any,state:any)=>fn(hub,state.storage.sql));
function measured(raw:any) {
  let cursors:any[]=[];const queries:string[]=[];
  const native=new Proxy(raw,{get(target,key){
    if(key==='exec')return(q:string,...b:any[])=>{queries.push(q);const c=target.exec(q,...b);cursors.push(c);return c;};
    const v=target[key];return typeof v==='function'?v.bind(target):v;
  }});
  return {sql:new RowsMeter(()=>NOW).wrap(native),reset(){cursors=[];queries.length=0;},rows(){return cursors.reduce((n,c)=>n+Number(c.rowsRead),0);},queries};
}
function launch(raw:any,id:string,state:string,reason:string|null=null,updated=AT) {
  raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,exit_reason,verification_status,finished_at)
    VALUES(?,'test','human','test',?,'{}','host','codex','account','headless','/repo','x',?,?,?,?,?,?)`,
    id,id,state,updated,updated,reason,'unverified',AT);
}

describe('Fleet recovery and paging use deployed indexes',()=>{
  it('reconciliation advances bounded raw state pages past sparse retained failures',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1000;i++)launch(raw,'old-'+i,'failed','unrelated');
    launch(raw,'recover-me','failed',SILENT_START_REASON);
    const m=measured(raw),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    let found=false;
    for(let page=0;page<42;page++) {
      m.reset();
      const rows=(fleet as any).reconciliationCandidatesUncached('host',false);
      expect(m.rows()).toBeLessThan(100);
      if(rows.some((row:any)=>row.id==='recover-me')) {found=true;break;}
    }
    expect(found).toBe(true);
    expect(m.queries.some(q=>q.includes('ORDER BY rowid LIMIT 26'))).toBe(true);
  }));

  it('batches reconciliation identity probes without reusing stale freshness or phase verdicts',()=>fixture(async(hub,raw)=>{
    const seed=fixtureSql(hub),stale=new Date(NOW-180001).toISOString();
    for(let i=0;i<2100;i++) {
      launch(seed,'batch-history-'+i,'stopped');
      seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
        VALUES('host',?,?,'codex','exited',?,'/repo')`,'history-native-'+i,'history-'+i,AT);
    }
    for(const state of ['starting','running','uncertain','failed'])for(let i=0;i<25;i++) {
      const id=state+'-'+i;
      launch(seed,id,state,state==='failed'?SILENT_START_REASON:null);
      if(state!=='failed') {
        seed.exec('UPDATE fleet_launches SET native_id=? WHERE id=?','native-'+id,id);
        seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
          VALUES('host',?, ?,?,'running',?,'/repo')`,'native-'+id,id,state==='uncertain'?'other':'codex',state==='running'?stale:AT);
      }
    }
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    let clock=NOW;
    const m=measured(hub.sql),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>clock,hub.writes);
    const read=(fresh:boolean)=>{m.reset();const result=(fleet as any).reconciliationCandidatesUncached('host',fresh);
      expect(m.queries.length).toBeLessThanOrEqual(7);expect(m.rows()).toBeLessThan(650);return result;};
    expect(read(false)).toHaveLength(75);
    // A complete fresh census supplies positive proof for the previously
    // stale native rows; engine mismatch and missing native remain visible.
    expect(read(true)).toHaveLength(50);
    seed.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id='native-starting-0'");
    expect(read(true).some((row:any)=>row.id==='starting-0')).toBe(true);
    expect(m.queries.some(q=>q.includes('CROSS JOIN fleet_sessions n'))).toBe(true);
    clock=NOW+180001;
    // Time alone retires positive freshness. Raw facts remain generation
    // cached; no native SQL runs to recompute the changed verdict.
    expect(read(false)).toHaveLength(100);
    expect(m.queries.some(q=>q.includes('CROSS JOIN fleet_sessions n'))).toBe(false);
    expect(m.rows()).toBe(0);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('shares only policy source rows inside a synchronous maintenance scope and invalidates writes and rollback',()=>fixture(async(hub,raw)=>{
    const seed=fixtureSql(hub);seed.exec("INSERT INTO fleet_claim_policy VALUES('/snapshot','project',0)");
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    const m=measured(hub.sql),fleet=new FleetService(m.sql as any,(fn:any)=>hub.ctx.storage.transactionSync(fn),()=>NOW,hub.writes);
    m.reset();fleet.withMaintenance(()=>{
      const first=fleet.admission.policy('/snapshot');first.require_claim=true;first.project='forged';
      expect(fleet.admission.policy('/snapshot')).toEqual({project:'project',require_claim:false});
      for(let i=0;i<100;i++)expect(fleet.admission.policy('/snapshot').require_claim).toBe(false);
      expect(m.queries).toHaveLength(1);
      seed.exec("UPDATE fleet_claim_policy SET require_claim=1 WHERE workspace='/snapshot'");
      expect(fleet.admission.policy('/snapshot').require_claim).toBe(true);
      expect(m.queries).toHaveLength(2);
      expect(()=>hub.ctx.storage.transactionSync(()=>{
        seed.exec("UPDATE fleet_claim_policy SET require_claim=0 WHERE workspace='/snapshot'");
        expect(fleet.admission.policy('/snapshot').require_claim).toBe(false);
        throw new Error('fixture rollback');
      })).toThrow('fixture rollback');
      expect(fleet.admission.policy('/snapshot').require_claim).toBe(true);
    });
    m.reset();expect(fleet.admission.policy('/snapshot').require_claim).toBe(true);
    expect(m.queries).toHaveLength(1); // no cache survives finally
    // A standalone store without an exact write generation never reuses a
    // source row, even inside the same synchronous operation.
    const plain=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    m.reset();plain.withMaintenance(()=>{plain.admission.policy('/snapshot');plain.admission.policy('/snapshot');});
    expect(m.queries).toHaveLength(2);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('hydrates account reservations in one bounded PK cohort and refuses incomplete CLI account families',()=>fixture(async(hub,raw)=>{
    const seed=fixtureSql(hub);
    seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('host','host','host',?,?,?)",AT,AT,JSON.stringify({engines:['codex']}));
    seed.exec("INSERT INTO fleet_accounts VALUES('account','codex',100,15,1)");
    seed.exec("INSERT INTO fleet_routes VALUES('host','codex','account',NULL)");
    for(let i=0;i<80;i++)launch(seed,'batch-reservation-'+i,'offered');
    for(let i=0;i<2100;i++)launch(seed,'batch-reservation-history-'+i,'stopped');
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    const m=measured(hub.sql),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    (fleet as any).reconcileCapacity=()=>{};
    m.reset();const result=fleet.accountStatus({label:'account',provider:'codex',max_concurrency:100,reserve_percent:15,enabled:1});
    expect(result).toMatchObject({active:80,reserved:80,capacity_observation_limited:false});
    expect(m.queries.filter(q=>q.includes('CROSS JOIN fleet_launches l ON l.id=requested.value'))).toHaveLength(1);
    expect(m.queries.some(q=>q.includes('WHERE l.id=?'))).toBe(false);
    expect(m.rows()).toBeLessThan(1000);
    // Even unrelated provider routes count against the raw observation cap;
    // they cannot be filtered away to manufacture unique-account proof.
    for(let i=0;i<129;i++)seed.exec('INSERT INTO fleet_routes VALUES(?,?,?,NULL)','host','engine-'+i,'missing-'+i);
    m.reset();expect(fleet.observationProvenance({label:'account',provider:'codex'},{machine_id:'host',profile:null})).toBeNull();
    expect(m.rows()).toBeLessThan(150);
    expect(m.queries.some(q=>q.includes('COUNT(DISTINCT'))).toBe(false);
    expect(m.queries.some(q=>q.includes('FROM host_quota_snapshots'))).toBe(false);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('reserved pages seek account/state/rowid before filtering old reservations',()=>fixture(async(_hub,raw)=>{
    const old=new Date(NOW-86400000).toISOString();
    for(let i=0;i<1000;i++)launch(raw,'stale-'+i,'offered',null,old);
    launch(raw,'eligible','offered');
    const context={live_ids:['host'],reservation_cutoff:new Date(NOW-900000).toISOString(),complete_catalogs:new Map<string,number>()};
    const m=measured(raw);let cursor=null;let found=false;
    for(let page=0;page<12;page++) {
      m.reset();const result=reservedLaunchPage(m.sql,'account',context,100,cursor);
      expect(m.rows()).toBeLessThan(230);
      if(result.rows.some(row=>row.id==='eligible'))found=true;
      if(!result.next_cursor)break;
      expect(result.next_cursor).toMatch(/^v2:/);
      cursor=parseReservedLaunchCursor(result.next_cursor);
    }
    expect(found).toBe(true);
    expect(m.queries.some(q=>q.includes('ORDER BY rowid LIMIT ?'))).toBe(true);
  }));

  it('no-start reconciliation advances bounded state pages and preserves positive receipt fences',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1000;i++)launch(raw,'no-start-history-'+i,'running');
    launch(raw,'receipt-proven','running');
    const diagnostic={reason:'launcher_memory_reserve',message:'Host memory headroom after launcher reservations is below the configured floor.',
      error_type:'ValueError',stage:'dispatch_headless',exit_code:1};
    raw.exec('UPDATE fleet_launches SET launch_error_json=? WHERE id=?',JSON.stringify(diagnostic),'receipt-proven');
    const m=measured(raw);let settled=false;
    for(let page=0;page<42;page++) {
      m.reset();const changes=reconcileVerifiedNoStarts(m.sql as any,NOW);
      expect(m.rows()).toBeLessThan(150);
      if(changes.some(row=>row.id==='receipt-proven')) {settled=true;break;}
      expect(m.queries.some(query=>/^UPDATE|^INSERT|^DELETE/.test(query))).toBe(false);
    }
    expect(settled).toBe(true);
    expect(raw.exec("SELECT state,verification_status FROM fleet_launches WHERE id='receipt-proven'").one())
      .toMatchObject({state:'failed',verification_status:'not_started'});
  }));

  it('account capacity bounds indexed active prefixes before foreign-host filtering',()=>fixture(async(hub,raw)=>{
    // One real strict budget day covers every measured accounting probe;
    // only synthetic retained-history setup uses the native watched handle.
    await hub.rowBudget.begin(true);
    const initialDay=hub.rowBudget.status().day,seed=fixtureSql(hub);
    seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('host','host','host',?,?,?)",AT,AT,JSON.stringify({engines:['codex']}));
    // Same account and same machine: a machine-only plan reads all retained
    // terminal history even when there are just two live natives to count.
    for(let i=0;i<2100;i++) {
      seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account,workspace)
        VALUES('host',?,?,'codex','exited',?,'account','/repo')`,'old-'+i,'old-'+i,AT);
      launch(seed,'terminal-'+i,'stopped');
    }
    for(const phase of ['running','unknown']) seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account,workspace)
      VALUES('host',?,?,'codex',?,?,'account','/repo')`,phase,phase,phase,AT);
    const m=measured(hub.sql),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    const definition={label:'account',provider:'codex',max_concurrency:10,reserve_percent:15,enabled:1};
    // Skip unrelated maintenance only in this component query-cost test;
    // the real account-status implementation and all accounting SQL run.
    (fleet as any).reconcileCapacity=()=>{};
    m.reset();const known=fleet.accountStatus(definition);
    expect(known).toMatchObject({active:2,available_slots:8,capacity_observation_limited:false});
    expect(m.rows()).toBeLessThan(150);
    console.log(JSON.stringify({evidence:'route_service_native_cost',operation:'FleetService.accountStatus',case:'2100terminal history per table,2live natives',rows_read:m.rows(),excludes:'outer authentication, KV ledger,maintenance and guard migration preflight'}));
    for(const query of m.queries.filter(q=>q.includes('INDEXED BY fleet_session_account_phase')||q.includes('INDEXED BY fleet_launch_account_state'))) {
      const detail=raw.exec('EXPLAIN QUERY PLAN '+query,'account').toArray().map((r:any)=>r.detail).join(' | ');
      expect(detail).toMatch(/SEARCH.*USING INDEX fleet_(session_account_phase|launch_account_state)/);
      expect(detail).not.toMatch(/TEMP B-TREE|^SCAN /);
    }
    // Raw foreign-host occupancy must refuse, rather than scanning until it
    // finds enough eligible local rows or declaring the local account free.
    for(let i=0;i<257;i++) seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account,workspace)
      VALUES('foreign',?,?,'codex','running',?,'account','/repo')`,'foreign-'+i,'foreign-'+i,AT);
    m.reset();const limited=fleet.accountStatus(definition);
    expect(limited).toMatchObject({status:'blocked',available_slots:0,placeable_slots:0,native_live:null,
      capacity_observation_limited:true,active_is_lower_bound:true});
    expect(limited.placement_reasons).toContain('capacity_observation_limited');
    expect(m.rows()).toBeLessThan(700);
    expect(m.queries.some(q=>q.includes('COUNT(*)'))).toBe(false);
    seed.exec("DELETE FROM fleet_sessions WHERE machine_id='foreign'");
    for(let i=0;i<257;i++)launch(seed,'stale-offer-'+i,'offered',null,new Date(NOW-86400000).toISOString());
    m.reset();const stalePrefix=fleet.accountStatus(definition);
    expect(stalePrefix).toMatchObject({status:'blocked',available_slots:0,capacity_observation_limited:true});
    expect(m.rows()).toBeLessThan(700);
    expect(m.queries.some(q=>q.includes('WHERE l.id=?'))).toBe(false);
    expect(hub.rowBudget.status()).toMatchObject({day:initialDay,limit_total:20000,stopped:false});
  }));

  it('correlates a unique in-flight native without scanning other accounts and preserves ambiguity fences',()=>fixture(async(hub,raw)=>{
    // One real strict budget day covers every measured accounting probe;
    // only synthetic retained-history setup uses the native watched handle.
    await hub.rowBudget.begin(true);
    const initialDay=hub.rowBudget.status().day,seed=fixtureSql(hub);
    seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES('host','host','host',?,?,?)",AT,AT,JSON.stringify({engines:['codex']}));
    seed.exec("INSERT INTO fleet_catalog_census(machine_id,observed_at,complete) VALUES('host',?,1)",AT);
    for(let i=0;i<2100;i++)seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account,workspace)
      VALUES('host',?,?,'codex','running',?,'other','/repo')`,'foreign-active-'+i,'noise-'+i,AT);
    launch(seed,'inflight-start','starting');
    const old=new Date(NOW-86400000).toISOString();
    const spawn=(id:string)=>seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,account,workspace,last_activity_at)
      VALUES('host',?,'task-hub-inflight-start','codex','running',?,'account','/repo',?)`,id,AT,old);
    spawn('target-native');
    const m=measured(hub.sql),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    (fleet as any).reconcileCapacity=()=>{};
    const definition={label:'account',provider:'codex',max_concurrency:10,reserve_percent:15,enabled:1};
    m.reset();
    expect(fleet.accountStatus(definition)).toMatchObject({active:1,reserved:0,observed_sessions:1,native_live:1,capacity_observation_limited:false});
    expect(m.rows()).toBeLessThan(150);
    expect(m.queries.some(q=>q.includes('fleet_sessions spawned'))).toBe(false);
    const boundQuery=m.queries.find(q=>q.includes('INDEXED BY fleet_launch_native_state'))!;
    expect(boundQuery).toBeTruthy();
    const detail=raw.exec('EXPLAIN QUERY PLAN '+boundQuery,'host','target-native').toArray().map((row:any)=>row.detail).join(' | ');
    expect(detail).toMatch(/SEARCH.*COVERING INDEX fleet_launch_native_state/);
    expect(detail).not.toMatch(/TEMP B-TREE|^SCAN /);
    console.log(JSON.stringify({evidence:'route_service_native_cost',operation:'FleetService.accountStatus',case:'2100 other-account active natives, one unique in-flight target',rows_read:m.rows(),excludes:'outer authentication, KV ledger, maintenance and guard migration preflight'}));

    m.reset();
    expect(reservedLaunchPage(m.sql,'account',{live_ids:['host'],reservation_cutoff:new Date(NOW-900000).toISOString(),complete_catalogs:new Map([['host',NOW]])},10).rows).toEqual([]);
    expect(m.rows()).toBeLessThan(150);
    spawn('duplicate-target');
    m.reset();
    expect(fleet.accountStatus(definition)).toMatchObject({reserved:1,observed_sessions:0,native_live:2});
    expect(m.rows()).toBeLessThan(150);
    seed.exec("DELETE FROM fleet_sessions WHERE native_id='duplicate-target'");
    launch(seed,'already-bound-elsewhere','running');
    seed.exec("UPDATE fleet_launches SET account='other',native_id='target-native' WHERE id='already-bound-elsewhere'");
    m.reset();
    // Existing direct-native linkage wins even when its account is not in
    // the account-specific source: never deduplicate through a second launch.
    expect(fleet.accountStatus(definition)).toMatchObject({reserved:1,observed_sessions:0,native_live:1});
    expect(m.rows()).toBeLessThan(150);
    expect(hub.rowBudget.status()).toMatchObject({day:initialDay,limit_total:20000,stopped:false});
  }));

  it('status agent counts seek active sources, preserve orphan hosts and refuse incomplete observations',()=>fixture(async(hub,raw)=>{
    const setup=fixtureSql(hub);
    for(let i=0;i<2100;i++) {
      setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
        VALUES('orphan',?,?,'codex','exited',?,'/repo')`,'old-'+i,'old-'+i,AT);
      setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
        VALUES(?,'test',?,'{}','orphan','codex','headless','stopped',?,?)`,'old-control-'+i,'old-control-'+i,AT,AT);
      setup.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)','orphan/old-'+i,'orphan','old-'+i,AT,'{}');
    }
    setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
      VALUES('orphan','current','current','codex','running',?,'/repo'),('orphan','noise','PING-current','codex','running',?,'/repo')`,AT,AT);
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    let sequence=0;
    const read=()=>{
      const scope='test:bounded-agent-counts-'+sequence++;
      const value=hub.meter.runSync(scope,()=>hub.statusService.computeAgentCounts(NOW));
      const cost=hub.meter.snapshot()[scope];
      expect(cost.rows_read).toBeLessThan(800);
      return value;
    };
    expect(read()).toEqual({running:1,idle:0,waiting:0,waiting_confirmation:0});
    setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,session_type,binding_json,created_at,updated_at)
      VALUES('probe-alias','test','probe-alias','{}','orphan','codex','headless','stopped','probe','{"native_id":"current"}',?,?)`,AT,AT);
    expect(read()).toEqual({running:0,idle:0,waiting:0,waiting_confirmation:0});
    setup.exec("UPDATE session_control_sessions SET session_type='headless' WHERE id='probe-alias'");
    setup.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)','orphan/current','orphan','current',AT,'{"waiting_confirmation":true}');
    expect(read()).toEqual({running:0,idle:1,waiting:1,waiting_confirmation:1});
    setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES('duplicate','test','duplicate','{}','orphan','codex','headless','stopped','{"native_id":"current"}',?,?)`,AT,AT);
    expect(read()).toMatchObject({running:null,observation_pending:true,observation_reason:'native_control_ambiguous'});
    setup.exec("DELETE FROM session_control_sessions WHERE id='duplicate'");
    for(let i=0;i<257;i++)setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
      VALUES('orphan',?,?,'codex','running',?,'/repo')`,'overflow-'+i,'overflow-'+i,AT);
    expect(read()).toMatchObject({running:null,observation_pending:true,observation_reason:'native_source_limit'});
    setup.exec("DELETE FROM fleet_sessions WHERE native_id LIKE 'overflow-%'");
    for(let i=0;i<257;i++)setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,created_at,updated_at)
      VALUES(?,'test',?,'{}','orphan','codex','headless','running',?,?)`,'active-control-'+i,'active-control-'+i,AT,AT);
    expect(read()).toMatchObject({running:null,observation_pending:true,observation_reason:'control_source_limit'});
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
    const plan=raw.exec(`EXPLAIN QUERY PLAN SELECT machine_id FROM fleet_sessions INDEXED BY fleet_session_machine_phase
      WHERE machine_id>? ORDER BY machine_id LIMIT 1`,'orphan').toArray().map((r:any)=>r.detail).join(' | ');
    expect(plan).toMatch(/SEARCH.*COVERING INDEX fleet_session_machine_phase/);
    expect(plan).not.toMatch(/TEMP B-TREE/);
  }));

  it('status host coverage refuses a bounded revoked prefix instead of declaring all hosts fresh',()=>fixture(async(hub)=>{
    const setup=fixtureSql(hub);
    for(let i=0;i<65;i++)setup.exec('INSERT INTO machines(id,name,hostname,registered_at,revoked_at) VALUES(?,?,?,?,?)',
      'revoked-'+i,'revoked','revoked',AT,AT);
    await hub.rowBudget.begin(true);
    const built=hub.statusService.buildItems({admin:true},[],null,null,NOW,AT);
    expect(built).toMatchObject({sessionCoverage:'partial',hostObservationPending:true});
    const coverage=hub.statusService.coverageDto(built.missingHosts,built.expectedOffline,built.sessionCoverage,NOW,built);
    expect(coverage).toMatchObject({host_observation_pending:true,reason:'host_source_limit',sessions:'partial'});
    expect(hub.statusService.categoriesDto(AT,[],built.sessionCoverage,false,false,{}).tasks_sessions.state).toBe('partial');
    expect(hub.rowBudget.status().stopped).toBe(false);
  }));

  it('existing status callers do no global baseline capacity read across retained legacy rows',()=>fixture(async(hub)=>{
    const setup=fixtureSql(hub),callerKey='human:baseline'+'||'+['','','',''].join('|');
    for(let i=0;i<5100;i++)setup.exec('INSERT INTO status_baselines VALUES(?,?,?,?)','old-'+i,'{}',0,AT);
    // A malformed payload is still an existing row, not a new caller.
    setup.exec('INSERT INTO status_baselines VALUES(?,?,?,?)',callerKey,'broken{',0,AT);
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    const response=await hub.meter.run('test:existing-status-baseline',()=>hub.statusService.handlePost(
      new Request('https://hub/api/status',{method:'POST',body:'{}'}),
      async()=>({caller:{kind:'human',isAdmin:true,actor:'human:baseline'},scopes:['admin']})));
    expect(response.status).toBe(200);
    const work=hub.meter.snapshot()['test:existing-status-baseline'];
    expect(work.rows_read).toBeLessThan(300);
    const queries=hub.meter.statementTop(1500).filter((statement:any)=>statement.scope==='test:existing-status-baseline').map((statement:any)=>statement.sql);
    expect(queries.some((query:string)=>query.includes('COUNT(*) AS n FROM status_baselines') || query.includes('SELECT 1 FROM status_baselines LIMIT'))).toBe(false);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('lifecycle linkage drives bounded selected identities through existing indexes and keeps oldest aliases',()=>fixture(async(hub,raw)=>{
    const setup=fixtureSql(hub),ids=new Set<string>();
    const control=(id:string,native:string,explicit:string|null=null,mode='headless')=>setup.exec(`INSERT INTO session_control_sessions
      (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,fleet_launch_id,created_at,updated_at)
      VALUES(?,'test',?,'{}','host','codex',?,'stopped',?,?,?,?)`,id,id,mode,JSON.stringify({native_id:native,workspace:'/repo'}),explicit,AT,AT);
    for(let i=0;i<2100;i++) {
      control('retained-control-'+i,'retained-native-'+i);
      launch(setup,'retained-launch-'+i,'stopped');
      setup.exec("UPDATE fleet_launches SET native_id=? WHERE id=?",'retained-native-'+i,'retained-launch-'+i);
      setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
        VALUES('host',?,?,'codex','exited',?,'/repo')`,'retained-native-'+i,'retained-tag-'+i,AT);
    }
    launch(setup,'preferred-launch','stopped');
    control('oldest-alias','selected-native-0','preferred-launch','interactive');
    for(let i=0;i<384;i++) {
      const id='selected-control-'+i;ids.add(id);
      control(id,'selected-native-'+i,i===0?'preferred-launch':null);
      launch(setup,'selected-launch-'+i,'stopped');
      setup.exec('UPDATE fleet_launches SET native_id=? WHERE id=?','selected-native-'+i,'selected-launch-'+i);
      setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
        VALUES('host',?,?,'codex','exited',?,'/repo')`,'selected-native-'+i,'selected-tag-'+i,AT);
    }
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    const value=hub.meter.runSync('test:selected-lifecycle-linkage',()=>buildSessionInputs(hub.sql,ids,{taskLinks:false}));
    expect(value.inputs).toHaveLength(384);
    expect(value.refs.get('host/selected-native-0')).toMatchObject({session_id:'oldest-alias',fleet_launch_id:'preferred-launch',protected_session:true});
    const work=hub.meter.snapshot()['test:selected-lifecycle-linkage'];
    console.log(JSON.stringify({evidence:'route_service_native_cost',operation:'buildSessionInputs selected384',history_per_table:2100,
      rows_read:work.rows_read,rows_written:work.rows_written,excludes:'outer authentication, KVledger and fixture seeding'}));
    expect(work.rows_read).toBeLessThan(10000);expect(work.rows_written).toBe(0);
    expect(()=>buildSessionInputs(hub.sql,undefined,{taskLinks:false})).toThrow(LifecycleObservationLimit);
    expect(()=>buildSessionInputs(hub.sql,new Set(Array.from({length:513},(_,i)=>'too-many-'+i)),{taskLinks:false})).toThrow(LifecycleObservationLimit);
    for(let i=0;i<1025;i++)control('duplicate-alias-'+i,'selected-native-0');
    hub.meter.runSync('test:lifecycle-alias-overflow',()=>{
      expect(()=>buildSessionInputs(hub.sql,new Set(['selected-control-0']),{taskLinks:false})).toThrow(LifecycleObservationLimit);
    });
    expect(hub.meter.snapshot()['test:lifecycle-alias-overflow'].rows_written).toBe(0);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
    const nativeQuery=hub.meter.statementTop(1500).find((statement:any)=>statement.scope==='test:selected-lifecycle-linkage' && statement.sql.includes('WITH identities AS MATERIALIZED'));
    expect(nativeQuery).toBeTruthy();
    const plan=raw.exec(`EXPLAIN QUERY PLAN WITH identities AS MATERIALIZED (
      SELECT json_extract(value,'$[0]') AS machine_id,json_extract(value,'$[1]') AS native_id FROM json_each(?))
      SELECT c.rowid FROM identities k CROSS JOIN session_control_sessions c INDEXED BY session_control_native
      ON c.machine_id=k.machine_id AND CASE WHEN json_valid(c.binding_json) THEN CASE WHEN json_type(c.binding_json,'$.native_id')='text'
        THEN json_extract(c.binding_json,'$.native_id') END END=+k.native_id LIMIT 1025`,JSON.stringify([['host','selected-native-0']]))
      .toArray().map((r:any)=>r.detail).join(' | ');
    expect(plan).toContain('machine_id=? AND <expr>=?');expect(plan).not.toMatch(/TEMP B-TREE/);
  }));

  it('classifies exact native targets independently of retained archives and refuses ambiguous keys or alias overflow',()=>fixture(async(hub)=>{
    const setup=fixtureSql(hub);
    for(let i=0;i<2100;i++) {
      setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES(?,'agent:owner',?,'{}','host','codex','headless','stopped',?,?,?)`,
        'history-'+i,'history-'+i,JSON.stringify({native_id:'history-native-'+i}),AT,AT);
      launch(setup,'history-launch-'+i,'stopped');
      setup.exec('UPDATE fleet_launches SET native_id=? WHERE id=?','history-native-'+i,'history-launch-'+i);
      setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
        VALUES('host',?,?,'codex','exited',?,'/repo')`,'history-native-'+i,'history-tag-'+i,AT);
    }
    setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
      VALUES('host','target-native','target','codex','exited',?,'/repo')`,AT);
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    expect(intakeLinkHealth(hub.sql,true).ready).toBe(true);
    let probe=0;const costs:any[]=[];
    const read=()=>{
      const scope='test:targeted-lifecycle-'+probe++;
      const value=hub.meter.runSync(scope,()=>classifyKey(hub.sql,'host/target-native',NOW,{machine_id:'host',native_id:'target-native'}));
      const work=hub.meter.snapshot()[scope];
      expect(work.rows_read).toBeLessThan(300);expect(work.rows_written).toBe(0);
      costs.push({probe:scope,rows_read:work.rows_read,rows_written:work.rows_written});
      return value;
    };
    expect(read()?.refs).toMatchObject({native_id:'target-native',session_id:null,protected_session:false});
    launch(setup,'native-target-launch','stopped');
    setup.exec("UPDATE fleet_launches SET native_id='target-native',requester_kind='agent' WHERE id='native-target-launch'");
    launch(setup,'preferred-target-launch','stopped');
    const alias=(id:string)=>setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,fleet_launch_id,created_at,updated_at)
      VALUES(?,'agent:owner',?,'{}','host','codex','headless','stopped',?,'preferred-target-launch',?,?)`,
      id,id,JSON.stringify({native_id:'target-native',workspace:'/repo'}),AT,AT);
    alias('oldest-target-alias');alias('newer-target-alias');
    expect(read()?.refs).toMatchObject({session_id:'oldest-target-alias',fleet_launch_id:'preferred-target-launch',human_owned:true,protected_session:true});
    setup.exec("UPDATE fleet_launches SET requester_kind='agent',actor='agent:owner' WHERE id='preferred-target-launch'");
    expect(read()?.refs).toMatchObject({human_owned:false,protected_session:false});
    setup.exec("INSERT INTO session_control_protections(session_id,actor,request_key,created_at) VALUES('oldest-target-alias','agent:owner','keep',?)",AT);
    expect(read()?.refs.protected_session).toBe(true);
    expect(classifyKey(hub.sql,'host/target-native',NOW)?.refs.session_id).toBe('oldest-target-alias');
    expect(classifyKey(hub.sql,'missing/native',NOW)).toBeNull();
    expect(classifyKey(hub.sql,'host/target-native',NOW,{machine_id:'other',native_id:'target-native'})).toBeNull();
    setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
      VALUES('split/a','b','split1','codex','exited',?,'/repo'),('split','a/b','split2','codex','exited',?,'/repo')`,AT,AT);
    expect(()=>classifyKey(hub.sql,'split/a/b',NOW)).toThrow(LifecycleObservationLimit);
    expect(()=>classifyKey(hub.sql,'a/'.repeat(66)+'b',NOW)).toThrow(LifecycleObservationLimit);
    for(let i=0;i<127;i++)alias('overflow-target-'+i);
    hub.meter.runSync('test:targeted-lifecycle-overflow',()=>expect(()=>classifyKey(hub.sql,'host/target-native',NOW,
      {machine_id:'host',native_id:'target-native'})).toThrow(LifecycleObservationLimit));
    const overflow=hub.meter.snapshot()['test:targeted-lifecycle-overflow'];
    expect(overflow.rows_read).toBeLessThan(300);expect(overflow.rows_written).toBe(0);
    console.log(JSON.stringify({evidence:'route_service_native_cost',operation:'classifyKey exactnative',history_per_table:2100,probes:costs,overflow,excludes:'outerauth, KVledger and fixtureseeding'}));
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('constructors do not repair retained wake/audit history and exact wake receipt upgrades touch only their entity',()=>fixture(async(hub)=>{
    const setup=fixtureSql(hub),now=Date.now(),created=new Date(now-1000).toISOString(),receipt=new Date(now).toISOString();
    setup.exec('ALTER TABLE audit ADD COLUMN ts TEXT');
    for(let i=0;i<2100;i++) {
      setup.exec("INSERT INTO audit(actor,action,at,ts) VALUES('fixture','retained',?,?)",created,created);
      setup.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at,acked_at)
        VALUES(?,'session-target','actor',?,?,?,'fixture','acked',?,?,?)`,'history-wake-'+i,'history-wake-'+i,now-1000,created,created,created,created);
    }
    const wake=(id:string,command:string)=>setup.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,command_id,created_at,updated_at,delivered_at)
      VALUES(?,'session-target','actor',?,?,?,'fixture','delivered',?,?,?,?)`,id,id,now-1000,created,command,created,created,created);
    const command=(id:string,wakeId:string,status:string|null,session='session-target')=>setup.exec(`INSERT INTO session_control_commands
      (id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at,host_status)
      VALUES(?,?,'send','actor',?,'{"body":"fixture"}','recorded',1,?,?,?,?)`,
      id,session,`wake-${wakeId}-${id}`,'cmd:'+id+':gen:1',created,receipt,status);
    wake('receipt-target','receipt-command');command('receipt-command','receipt-target','waiting in inbox for recipient');
    wake('queue-target','queue-command');command('queue-command','queue-target',null);
    wake('foreign-target','foreign-command');command('foreign-command','foreign-target','waiting in inbox for recipient','other-session');
    setup.exec("CREATE TRIGGER fixture_no_ctor_wake_repair BEFORE UPDATE ON session_control_wakes BEGIN SELECT RAISE(ABORT,'constructor_wake_repair'); END");
    setup.exec("CREATE TRIGGER fixture_no_ctor_audit_repair BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'constructor_audit_repair'); END");
    await hub.rowBudget.begin(true);const day=hub.rowBudget.status().day;
    hub.meter.runSync('test:no-constructor-history-repair',()=>{hub.initDb();hub.sessionControlService.init();});
    const sql=hub.meter.statementTop(1500).filter((statement:any)=>statement.scope==='test:no-constructor-history-repair').map((statement:any)=>statement.sql);
    expect(sql.some((query:string)=>/^UPDATE (?:audit|session_control_wakes)\b/i.test(query.trim()))).toBe(false);
    expect(setup.exec("SELECT delivered_at FROM session_control_wakes WHERE id='receipt-target'").one().delivered_at).toBe(created);
    expect(setup.exec("SELECT COUNT(*) AS n FROM audit WHERE at=? AND ts=?",created,created).one().n).toBe(2100);
    setup.exec('DROP TRIGGER fixture_no_ctor_wake_repair');
    const service=hub.sessionControlService;
    hub.meter.runSync('test:exact-wake-upgrade',()=>service.upgradeWakeNativeReceipt('receipt-target'));
    expect(setup.exec("SELECT state,delivered_at FROM session_control_wakes WHERE id='receipt-target'").one())
      .toMatchObject({state:'delivered',delivered_at:receipt});
    expect(setup.exec("SELECT delivered_at FROM session_control_wakes WHERE id='queue-target'").one().delivered_at).toBe(created);
    service.processDueWakes('queue-target');
    expect(setup.exec("SELECT state,delivered_at FROM session_control_wakes WHERE id='queue-target'").one())
      .toMatchObject({state:'queued',delivered_at:null});
    hub.meter.runSync('test:foreign-wake-upgrade',()=>expect(()=>service.upgradeWakeNativeReceipt('foreign-target')).toThrow('wake_receipt_binding_invalid'));
    expect(hub.meter.snapshot()['test:foreign-wake-upgrade'].rows_written).toBe(0);
    expect(setup.exec("SELECT state,delivered_at FROM session_control_wakes WHERE id='foreign-target'").one())
      .toMatchObject({state:'delivered',delivered_at:created});
    wake('cas-target','cas-command');command('cas-command','cas-target','waiting in inbox for recipient');
    setup.exec("CREATE TRIGGER fixture_ignore_wake_upgrade BEFORE UPDATE ON session_control_wakes WHEN OLD.id='cas-target' BEGIN SELECT RAISE(IGNORE); END");
    const ignored=hub.meter.runSync('test:ignored-wake-upgrade',()=>service.upgradeWakeNativeReceipt('cas-target'));
    expect(ignored.delivered_at).toBe(created);
    expect(hub.meter.snapshot()['test:ignored-wake-upgrade'].rows_written).toBe(0);
    expect(setup.exec("SELECT delivered_at FROM session_control_wakes WHERE id='cas-target'").one().delivered_at).toBe(created);
    setup.exec('DROP TRIGGER fixture_ignore_wake_upgrade');
    expect(service.upgradeWakeNativeReceipt('cas-target').delivered_at).toBe(receipt);
    expect(setup.exec("SELECT COUNT(*) AS n FROM session_control_wakes WHERE state='acked'").one().n).toBe(2100);
    expect(hub.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));

  it('maps legacy three-part session cursors to the existing primary-key order',()=>fixture(async(_hub,raw)=>{
    for(let i=0;i<1200;i++)raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace)
      VALUES('host',?,?,'exited',?,'/repo')`,'native-'+String(i).padStart(4,'0'),'tag-'+(1200-i),AT);
    const m=measured(raw),fleet=new FleetService(m.sql as any,(fn:any)=>fn(),()=>NOW);
    const encoded=btoa(JSON.stringify(['host','old-tag','native-0500'])).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
    const response=await fleet.handle(new Request('https://hub/api/fleet?sections=sessions&sessions_limit=10&sessions_cursor='+encoded),
      {caller:{kind:'human',actor:'test',isAdmin:true},scopes:['admin']} as any);
    const body:any=await response.json();
    expect(body.sessions[0].native_id).toBe('native-0501');
    expect(body.sessions_order).toEqual(['machine_id','native_id']);
    expect(body.sessions_total).toBeNull();
    expect(m.rows()).toBeLessThan(100);
    const cursor=JSON.parse(atob(body.sessions_cursor_next.replace(/-/g,'+').replace(/_/g,'/')));
    expect(cursor).toHaveLength(2);
  }));
});
