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
