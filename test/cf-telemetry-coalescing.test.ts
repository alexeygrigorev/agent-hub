import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RowsMeter } from '../src/rows-meter';
import { WriteWatch } from '../src/write-watch';
import { updateHeartbeat,effectiveHeartbeat,effectiveHost } from '../src/registry';
import { noteCatalogObservation,freshCatalogs,freshCompleteCatalogs } from '../src/catalog-freshness';

const fixture=(fn:(x:any)=>Promise<void>|void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('telemetry-'+crypto.randomUUID())),async(_h:any,state:any)=>{
  const raw=state.storage.sql;let now=Date.now();const iso=()=>new Date(now).toISOString();
  raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES('host','host','host',?,?,?,?)",iso(),iso(),JSON.stringify({mem_avail_mb:3000,mem_total_mb:64000,load:[1],launcher_resources:{observed_at:iso()}}),JSON.stringify({cpu_count:12}));
  raw.exec("INSERT INTO intake_tasks VALUES('task',?)",JSON.stringify({id:'task',project:'demo',status:'open',history:[],acceptance_criteria:'synthetic'}));
  raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
    VALUES('launch','test','human','test','key',?,'host','opencode','go','headless','/repo','test','starting','native',?,?)`,JSON.stringify({intake_task_id:'task'}),iso(),iso());
  raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('session','test','control',?,'launch','host','opencode','headless','running',?,?,?)`,JSON.stringify({intake_task_id:'task'}),JSON.stringify({native_id:'native',tag:'agent',workspace:'/repo'}),iso(),iso());
  raw.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('host','native','agent','opencode','running',?,'/repo')",iso());
  raw.exec("INSERT INTO resource_cpu_starts VALUES('launch',2,'fixture_commitment')");
  const meter=new RowsMeter(()=>now),watch=new WriteWatch(),sql=watch.wrap(meter.wrap(raw));
  const f=new FleetService(sql,fn=>state.storage.transactionSync(fn),()=>now,watch),r=f.resources;r.initCatalogCache();
  const sample=(extra:any={})=>({session_id:'session',native_id:'native',current_rss_mb:200,peak_rss_mb:200,peak_pss_mb:210,current_cpu_cores:1,cpu_seconds:20,runtime_s:100,observed_at:iso(),...extra});
  await fn({f,r,raw,sql,meter,sample,advance:(ms:number)=>{now+=ms;},iso});
});

describe('live telemetry persistence coalescing',()=>{
  it('uses freshest RSS/CPU for admission while changing fast reports write zero durable rows',()=>fixture(({r,raw,meter,sample,advance}:any)=>{
    r.report('host',{samples:[sample()],reaps:[]});
    expect(r.headroom('host').reserved_mb).toBe(312);
    expect(r.cpuHeadroom('host').ramping_cpu_cores).toBe(1);
    for(let i=0;i<60;i++) {
      advance(1000);
      meter.runSync('report-'+i,()=>r.report('host',{samples:[sample({current_rss_mb:100,current_cpu_cores:.5,cpu_seconds:30+i,runtime_s:110+i,peak_rss_mb:250+i})],reaps:[]}));
      expect(meter.snapshot()['report-'+i]?.rows_written??0).toBe(0);
    }
    console.log(JSON.stringify({evidence:'route_service_native_cost',route:'POST /api/fleet/resources/report',case:'warm changing telemetry',...meter.snapshot()['report-59'],excludes:'outer authentication, KV ledger and guard migration preflight'}));
    expect(r.headroom('host').reserved_mb).toBe(412);
    expect(r.cpuHeadroom('host').ramping_cpu_cores).toBe(1.5);
    expect(raw.exec("SELECT peak_rss_mb FROM session_resource_usage WHERE session_id='session'").one().peak_rss_mb).toBe(200);
  }));

  it('flushes merged cumulative measurements on terminal transition and on hourly requests',()=>fixture(({r,raw,sql,meter,sample,advance,iso}:any)=>{
    r.report('host',{samples:[sample()],reaps:[]});
    advance(45000);r.report('host',{samples:[sample({peak_rss_mb:800,cpu_seconds:100,runtime_s:200})],reaps:[]});
    advance(3600000);
    meter.runSync('hour',()=>r.report('host',{samples:[sample({peak_rss_mb:300,cpu_seconds:110,runtime_s:300})],reaps:[]}));
    expect(meter.snapshot().hour.rows_written).toBeGreaterThan(0);
    expect(raw.exec("SELECT peak_rss_mb,cpu_seconds FROM session_resource_usage WHERE session_id='session'").one()).toMatchObject({peak_rss_mb:800,cpu_seconds:110});
    sql.exec("UPDATE session_control_sessions SET state='stopped',updated_at=? WHERE id='session'",iso());
    sql.exec("UPDATE fleet_launches SET state='exited',finished_at=?,updated_at=? WHERE id='launch'",iso(),iso());
    meter.runSync('terminal',()=>r.report('host',{samples:[sample({cpu_seconds:150,runtime_s:350})],reaps:[]}));
    expect(meter.snapshot().terminal.rows_written).toBeGreaterThan(0);
    expect(raw.exec("SELECT peak_rss_mb,cpu_seconds,finished_at FROM session_resource_usage WHERE session_id='session'").one()).toMatchObject({peak_rss_mb:800,cpu_seconds:150,finished_at:iso()});
  }));
});

describe('coalesced catalog freshness',()=>{
  it('keeps active completeness fresh in memory and fails closed after a restart with stale durable evidence',()=>fixture(({raw,sql,advance,iso}:any)=>{
    let now=Date.parse(iso());
    expect(noteCatalogObservation(sql,'host',now,true)).toBe(true);
    raw.exec("INSERT INTO fleet_catalog_seen VALUES('host',?)",iso());
    raw.exec("INSERT INTO fleet_catalog_census VALUES('host',?,1)",iso());
    advance(240000);now=Date.parse(iso());
    expect(noteCatalogObservation(sql,'host',now,true)).toBe(false);
    expect(freshCompleteCatalogs(sql,now).get('host')).toBe(now);
    expect(freshCatalogs(sql,now).get('host')).toBe(now);
    const restarted={exec:(...args:any[])=>raw.exec(...args)} as any;
    expect(freshCompleteCatalogs(restarted,now).has('host')).toBe(false);
    expect(freshCatalogs(restarted,now).has('host')).toBe(false);
    advance(900000);now=Date.parse(iso());
    expect(freshCompleteCatalogs(sql,now).has('host')).toBe(false);
  }));

  it('persists incompleteness immediately and never lets an old complete verdict override it',()=>fixture(({raw,sql,advance,iso}:any)=>{
    let now=Date.parse(iso());noteCatalogObservation(sql,'host',now,true);
    raw.exec("INSERT INTO fleet_catalog_census VALUES('host',?,1)",iso());
    advance(45000);now=Date.parse(iso());
    expect(noteCatalogObservation(sql,'host',now,false)).toBe(true);
    expect(freshCompleteCatalogs(sql,now).has('host')).toBe(false);
    raw.exec("UPDATE fleet_catalog_census SET complete=0,observed_at=? WHERE machine_id='host'",iso());
    const restarted={exec:(...args:any[])=>raw.exec(...args)} as any;
    expect(freshCompleteCatalogs(restarted,now).has('host')).toBe(false);
    advance(45000);now=Date.parse(iso());
    expect(noteCatalogObservation(sql,'host',now,false)).toBe(false);
    expect(noteCatalogObservation(sql,'host',now,true)).toBe(true);
    expect(freshCompleteCatalogs(sql,now).get('host')).toBe(now);
  }));
});


describe('heartbeat coalescing with live admission overlays',()=>{
  it('keeps the latest pressure and host freshness without persisting sampling noise',()=>fixture(({r,raw,sql,meter,advance,iso}:any)=>{
    const heartbeat=(mem:number,swap:number)=>({type:'heartbeat',at:iso(),mem_avail_mb:mem,mem_total_mb:64000,swap_used_percent:swap,load:[1],gate:{launch_allowed:true,reason:'ok',at:iso()},launcher_resources:{active_mem_mb:1000,observed_at:iso()}});
    const first=iso();updateHeartbeat('host',heartbeat(3000,0),sql,Date.parse(iso()));
    advance(240000);
    meter.runSync('heartbeat',()=>updateHeartbeat('host',heartbeat(500,85),sql,Date.parse(iso())));
    expect(meter.snapshot().heartbeat).toBeUndefined(); // No SQL cursor was issued.
    const durable=raw.exec("SELECT * FROM machines WHERE id='host'").one();
    expect(durable.last_checkin_at).toBe(first);
    expect(JSON.parse(effectiveHeartbeat(sql,'host',durable.last_heartbeat)!)).toMatchObject({mem_avail_mb:500,swap_used_percent:85});
    expect(effectiveHost(sql,durable).last_checkin_at).toBe(iso());
    expect(r.headroom('host').mem_available_mb).toBe(500);
    expect(r.capacityCpuAdmitted('host')).toBe(false);
    const restarted={};
    expect(effectiveHost(restarted,durable).last_checkin_at).toBe(first);
    advance(660000);
    meter.runSync('heartbeat-due',()=>updateHeartbeat('host',heartbeat(500,85),sql,Date.parse(iso())));
    expect(meter.snapshot()['heartbeat-due'].rows_written).toBeGreaterThan(0);
  }));

  it('persists a real gate/control transition immediately inside the coalescing interval',()=>fixture(({raw,sql,meter,advance,iso}:any)=>{
    const hb=(allowed:boolean)=>({type:'heartbeat',at:iso(),gate:{launch_allowed:allowed,reason:allowed?'ok':'operator_hold',at:iso()}});
    updateHeartbeat('host',hb(true),sql,Date.parse(iso()));advance(1000);
    meter.runSync('control',()=>updateHeartbeat('host',hb(false),sql,Date.parse(iso())));
    expect(meter.snapshot().control.rows_written).toBeGreaterThan(0);
    expect(JSON.parse(raw.exec("SELECT last_heartbeat FROM machines WHERE id='host'").one().last_heartbeat).gate.launch_allowed).toBe(false);
  }));
});


describe('advisory telemetry and completeness transaction fences',()=>{
  it('drops admission credit after eviction and does not reuse an old-native measurement',()=>fixture(({r,raw,sql,sample}:any)=>{
    r.report('host',{samples:[sample()],reaps:[]});expect(r.headroom('host').reserved_mb).toBe(312);
    r.invalidateLiveTelemetry();expect(r.headroom('host').reserved_mb).toBe(512);
    expect(r.cpuHeadroom('host').ramping_cpu_cores).toBe(2);
    r.report('host',{samples:[sample()],reaps:[]});
    sql.exec("UPDATE fleet_launches SET native_id='replacement' WHERE id='launch'");
    expect(r.headroom('host').reserved_mb).toBe(512);
    expect(r.cpuHeadroom('host').ramping_cpu_cores).toBe(2);
  }));

  it('invalidates uncommitted telemetry and complete-census overlays after an SQL rollback',()=>fixture(({f,r,raw,sql,sample,iso}:any)=>{
    r.report('host',{samples:[sample()],reaps:[]});
    raw.exec("INSERT INTO fleet_catalog_census VALUES('host',?,0)",iso());
    expect(()=>(f as any).transaction(()=>{
      noteCatalogObservation(sql,'host',Date.parse(iso()),true);
      sql.exec("UPDATE fleet_catalog_census SET complete=1 WHERE machine_id='host'");
      r.report('host',{samples:[sample({current_rss_mb:500,current_cpu_cores:1.5})],reaps:[]});
      throw new Error('rollback');
    })).toThrow('rollback');
    expect(freshCompleteCatalogs(sql,Date.parse(iso())).has('host')).toBe(false);
    expect(r.headroom('host').reserved_mb).toBe(512);
    expect(r.cpuHeadroom('host').ramping_cpu_cores).toBe(2);
  }));
});


it('CPU admission refuses an oversized raw active prefix before retained history can be scanned',()=>fixture(({r,raw,meter,iso}:any)=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
    INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    SELECT 'active-'||i,'test','human','test','active-'||i,'{}','host','opencode','go','headless','/repo','test','running',?,? FROM n`,iso(),iso());
  const verdict=meter.runSync('cpu-prefix',()=>r.cpuHeadroom('host'));
  expect(verdict).toMatchObject({admitted:false,reason:'resource_snapshot_overflow',snapshot_truncated:true,ramping_cpu_cores:null});
  expect(meter.snapshot()['cpu-prefix'].rows_read).toBeLessThan(1000);
}));


describe('resource hotpaths ignore retained terminal history',()=>{
  it('finds late live natives and complete old protected aliases without reading 40k retained control/launch rows',()=>fixture(({r,raw,meter,sample,iso}:any)=>{
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<21000)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      SELECT 'history-'||i,'test','history-'||i,'{}','host','codex','interactive','stopped',json_object('native_id','history-'||i,'tag','old','workspace','/old'),?,? FROM n`,iso(),iso());
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<21000)
      INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      SELECT 'history-launch-'||i,'test','human','test','history-launch-'||i,'{}','host','codex','go','headless','/old','test','exited','history-'||i,?,? FROM n`,iso(),iso());
    raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES('protected-old','test','protected-old','{}','host','opencode','interactive','stopped',?,?,?)`,JSON.stringify({native_id:'native',tag:'old-alias',workspace:'/repo'}),iso(),iso());
    raw.exec("INSERT INTO session_control_protections VALUES('protected-old','human','keep',?)",iso());
    // Keep the eligible rows physically after the retained terminal prefix.
    raw.exec("UPDATE session_control_sessions SET rowid=30000 WHERE id='session'");
    raw.exec("UPDATE fleet_launches SET rowid=30000 WHERE id='launch'");
    const sessions=meter.runSync('bounded-watchdog',()=>r.sessions('host'));
    expect(sessions.find((row:any)=>row.native_id==='native')).toMatchObject({keep:true,managed:false,idle_reap_allowed:false,authority_observation_limited:true});
    expect(meter.snapshot()['bounded-watchdog'].rows_read).toBeLessThan(500);
    meter.runSync('bounded-report',()=>r.report('host',{samples:[sample()],reaps:[]}));
    expect(meter.snapshot()['bounded-report'].rows_read).toBeLessThan(500);
  }));

  it('refuses oversized native alias authority before any report write',()=>fixture(({r,raw,meter,sample,iso}:any)=>{
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<129)
      INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      SELECT 'alias-'||i,'test','alias-'||i,'{}','host','opencode','interactive','stopped',?, ?,? FROM n`,JSON.stringify({native_id:'native',tag:'old-alias',workspace:'/repo'}),iso(),iso());
    expect(()=>meter.runSync('alias-overflow',()=>r.report('host',{samples:[sample()],reaps:[]}))).toThrow('resource_catalog_observation_limit');
    expect(meter.snapshot()['alias-overflow'].rows_written).toBe(0);
    expect(meter.snapshot()['alias-overflow'].rows_read).toBeLessThan(300);
  }));
});

describe('bounded offer and dashboard source windows',()=>{
  it('pages past native-proven starting rows without scanning the whole population or reoffering positive effects',()=>fixture(async({f,raw,meter,iso}:any)=>{
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
      INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      SELECT 'started-'||i,'test','human','test','started-'||i,'{}','host','opencode','go','headless','/repo','test','starting','proof-'||i,?,? FROM n`,iso(),iso());
    raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,claim_key,created_at,updated_at)
      VALUES('late-retry','test','human','test','late-retry','{}','host','opencode','go','headless','/repo','test','starting','same-host-key',?,?)`,iso(),iso());
    // Freeze unrelated periodic queue planning; this test measures the host
    // candidate page, reconciliation and enrichment against retained rows.
    (f as any).maintenanceDirty=false;(f as any).pollMaintenanceAt=Date.parse(iso());
    let found=false;
    for(let i=0;i<34;i++) {
      const response=await meter.run('offer-page-'+i,()=>f.handle(new Request('https://hub.test/api/fleet/offers'),{caller:{kind:'host',machineId:'host',actor:'machine:host'},scopes:['host']}));
      expect(response.status).toBe(200);
      const body:any=await response.json();
      for(const offer of body.offers){expect(offer.id).toBe('late-retry');expect(offer.claim_key).toBe('same-host-key');found=true;}
      expect(meter.snapshot()['offer-page-'+i]?.rows_read??0,JSON.stringify(meter.statementTop(8))).toBeLessThan(1000);
    }
    expect(found).toBe(true);
  }));

  it('returns dashboard insertion pages with a continuation instead of sorting all retained launches',()=>fixture(async({f,raw,meter,iso}:any)=>{
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<21000)
      INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      SELECT 'dashboard-'||i,'test','human','test','dashboard-'||i,'{}','host','opencode','go','headless','/repo','test','exited',?,? FROM n`,iso(),iso());
    const admin={caller:{kind:'human',actor:'test',isAdmin:true},scopes:['admin']};
    const first=await meter.run('dashboard-first',()=>f.handle(new Request('https://hub.test/api/fleet?sections=launches'),admin));
    expect(first.status).toBe(200);const page:any=await first.json();
    expect(page.launches).toHaveLength(200);expect(page.launches_truncated).toBe(true);expect(page.launches_order).toBe('insertion_desc');
    expect(meter.snapshot()['dashboard-first'].rows_read).toBeLessThan(500);
    const next=await meter.run('dashboard-next',()=>f.handle(new Request('https://hub.test/api/fleet?sections=launches&launches_cursor='+page.launches_cursor),admin));
    const second:any=await next.json();const firstIds=new Set(page.launches.map((row:any)=>row.id));
    expect(second.launches).toHaveLength(200);expect(second.launches.some((row:any)=>firstIds.has(row.id))).toBe(false);
    expect(meter.snapshot()['dashboard-next'].rows_read).toBeLessThan(500);
  }));

  it('bounds sparse CPU history before the measurement filter and refuses unknown defaults',()=>fixture(({r,raw,meter,iso}:any)=>{
    raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
      INSERT INTO session_resource_usage(session_id,machine_id,native_id,started_at,finished_at,state,observed_at)
      SELECT 'sparse-'||i,'history','sparse-'||i,?,?,'exited',? FROM n`,iso(),iso(),iso());
    const view=meter.runSync('sparse-cpu',()=>r.cpuHeadroom('host'));
    expect(view).toMatchObject({admitted:false,reason:'cpu_history_observation_limited',expected_cpu_source:'default_observation_limited'});
    expect(meter.snapshot()['sparse-cpu'].rows_read).toBeLessThan(1600);
  }));
});

it('seeks the deployed runtime-failure partial index independently of neutral retained history',()=>fixture(({r,raw,sql,meter,iso}:any)=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
    INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,exit_reason,created_at,updated_at)
    SELECT 'runtime-noise-'||i,'test','human','test','runtime-noise-'||i,'{}','foreign','codex','acct','headless','/repo','test','failed','normal_exit',?,? FROM n`,iso(),iso());
  const first=meter.runSync('runtime-unknown',()=>r.cpuHeadroom('host'));
  expect(first).toMatchObject({admitted:true,reason:null,runtime_observation_limited:false});
  expect(meter.snapshot()['runtime-unknown'].rows_read).toBeLessThan(100);
  // Mutate through the same WriteWatch as the service: a raw write after a
  // measured read would intentionally bypass the memo generation fence.
  sql.exec("UPDATE fleet_launches SET state='failed',exit_reason='engine_runtime_timeout',updated_at=? WHERE id='launch'",new Date(Date.parse(iso())+1).toISOString());
  const second=meter.runSync('runtime-known',()=>r.cpuHeadroom('host'));
  expect(second).toMatchObject({admitted:false,reason:'host_runtime_choking',runtime_observation_limited:false});
  expect(meter.snapshot()['runtime-known'].rows_read).toBeLessThan(100);
}));

it('keeps fresh coalesced activity visible and expires or invalidates idle authority without scanning history',()=>fixture(({f,r,raw,sql,meter,iso,advance}:any)=>{
  raw.exec("UPDATE intake_tasks SET data=json_set(data,'$.status','done','$.archived_at',1,'$.history',json(?)) WHERE id='task'",JSON.stringify([{kind:'note',text:'DONE abc1234\nlive check: synthetic activity fixture\nresult: passed'}]));
  const native=(activity=iso())=>({native_id:'native',last_activity_at:activity,observed_at:iso()});
  r.noteCensus('host',[native()],false);
  expect(r.sessions('host')[0]).toMatchObject({last_activity_at:iso(),activity_observation_limited:false});
  advance(1000);r.noteCensus('host',[native()],false);
  meter.runSync('fresh-activity',()=>expect(r.sessions('host')[0]).toMatchObject({last_activity_at:iso(),activity_observation_limited:false}));
  expect(meter.snapshot()['fresh-activity']?.rows_read??0).toBeLessThan(20);
  expect(meter.snapshot()['fresh-activity']?.rows_written??0).toBe(0);
  advance(181000);
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
  r.noteCensus('host',[native()],false);
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:false,idle_reap_allowed:true});
  expect(()=>(f as any).transaction(()=>{r.noteCensus('host',[native()],false);throw new Error('rollback-live-activity');})).toThrow('rollback-live-activity');
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
  r.noteCensus('host',[native()],false);
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-01-01T00:00:00Z' WHERE machine_id='host' AND native_id='native'");
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
}));

it('never restores idle authority from an old durable row after live activity cache eviction',()=>fixture(({r,raw,iso,advance}:any)=>{
  raw.exec("UPDATE intake_tasks SET data=json_set(data,'$.status','done','$.archived_at',1,'$.history',json(?)) WHERE id='task'",JSON.stringify([{kind:'note',text:'DONE abc1234\nlive check: synthetic activity fixture\nresult: passed'}]));
  advance(181000); // a cold read cannot mistake an old15-minute checkpoint for fresh evidence
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
  r.noteCensus('host',[{native_id:'native',last_activity_at:iso(),observed_at:iso()}],false);
  expect(r.sessions('host')[0].activity_observation_limited).toBe(false);
  for(let batch=0;batch<5;batch++)r.noteCensus('host',Array.from({length:500},(_,i)=>({native_id:'cache-fill-'+batch+'-'+i,last_activity_at:iso(),observed_at:iso()})),false);
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
  r.noteCensus('host',[{native_id:'native',last_activity_at:iso(),observed_at:iso()}],false);
  expect(r.sessions('host')[0]).toMatchObject({activity_observation_limited:false,idle_reap_allowed:true});
}));

it('does not adopt a new host report stamp while retaining another hosts stale live activity',()=>fixture(({r,raw,sql,iso}:any)=>{
  raw.exec("UPDATE intake_tasks SET data=json_set(data,'$.status','done','$.archived_at',1,'$.history',json(?)) WHERE id='task'",JSON.stringify([{kind:'note',text:'DONE abc1234\nlive check: synthetic activity fixture\nresult: passed'}]));
  raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('other','other','other',?,?)",iso(),iso());
  raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
    SELECT 'other-launch',actor,requester_kind,summary,'other-key',request_json,'other',engine,account,mode,workspace,prompt,state,'other-native',created_at,updated_at FROM fleet_launches WHERE id='launch'`);
  raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    SELECT 'other-session',actor,'other-control',request_json,'other-launch','other',engine,mode,state,json_set(binding_json,'$.native_id','other-native'),created_at,updated_at FROM session_control_sessions WHERE id='session'`);
  raw.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,last_activity_at) VALUES('other','other-native','agent','opencode','running',?,'/repo',?)",iso(),iso());
  const report=(host:string,id:string)=>r.noteCensus(host,[{native_id:id,last_activity_at:iso(),observed_at:iso()}],false);
  report('host','native');report('other','other-native');
  expect(r.sessions('other')[0].activity_observation_limited).toBe(false);
  sql.exec("UPDATE fleet_sessions SET last_activity_at='2026-01-01T00:00:00Z' WHERE machine_id='other' AND native_id='other-native'");
  report('host','native');
  expect(r.sessions('other')[0]).toMatchObject({activity_observation_limited:true,idle_reap_allowed:false});
  report('other','other-native');
  expect(r.sessions('other')[0].activity_observation_limited).toBe(false);
}));

it('marks reconciliation pages partial and reaches all56 pending launches without freezing the first25 in a response cache',()=>fixture(async({f,raw,meter,iso}:any)=>{
 raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<56)
  INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
  SELECT 'reconcile-'||i,'test','human','test','reconcile-'||i,'{}','host','opencode','go','headless','/repo','test','uncertain',?,? FROM n`,iso(),iso());
 const seen=new Set<string>();
 for(let page=0;page<3;page++) {
  const response=await meter.run('reconcile-page-'+page,()=>f.handle(new Request('https://hub/api/fleet/reconciliation'),{caller:{kind:'host',machineId:'host'},scopes:['host']}));
  expect(response.status).toBe(200);const body=await response.json();
  expect(body.observation_scope).toBe('raw_state_page');expect(body.observation_pending).toBe(true);
  expect(body.launches.length).toBeLessThanOrEqual(25);expect(body.next_cursors).toHaveProperty('uncertain');
  for(const row of body.launches)if(row.id.startsWith('reconcile-'))seen.add(row.id);
  expect(meter.snapshot()['reconcile-page-'+page].rows_read).toBeLessThan(400);
 }
 expect(seen.size).toBe(56);
}));
