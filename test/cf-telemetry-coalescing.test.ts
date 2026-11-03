import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { ResourceManagement } from '../src/resource-management';
import { RowsMeter } from '../src/rows-meter';
import { WriteWatch } from '../src/write-watch';
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
  const r=new ResourceManagement(sql,()=>now,watch);r.initCatalogCache();
  const sample=(extra:any={})=>({session_id:'session',native_id:'native',current_rss_mb:200,peak_rss_mb:200,peak_pss_mb:210,current_cpu_cores:1,cpu_seconds:20,runtime_s:100,observed_at:iso(),...extra});
  await fn({r,raw,sql,meter,sample,advance:(ms:number)=>{now+=ms;},iso});
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
