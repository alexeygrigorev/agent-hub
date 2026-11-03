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
      expect(meter.snapshot()['report-'+i].rows_written).toBe(0);
    }
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
    expect(meter.snapshot().heartbeat.rows_written).toBe(0);
    expect(meter.snapshot().heartbeat.rows_read).toBe(0);
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
