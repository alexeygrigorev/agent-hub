import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { JobsService } from '../src/jobs';
import { HostHistoryService } from '../src/host-history';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
const NOW = Date.parse('2026-10-24T15:00:00Z'), DAY = 86400000;
const admin = { caller: { isAdmin: true, actor: 'test-admin' } };
async function fixture(fn: (h: HostHistoryService, hub: Hub, clock: { t: number }) => any) {
  const e = env as unknown as Env;
  await runInDurableObject(e.HUB.get(e.HUB.idFromName('history-' + crypto.randomUUID())), async (hub: Hub) => {
    const clock = { t: NOW };
    const history = new HostHistoryService(hub.sql, hub.fleetService, () => clock.t);
    history.init(); await fn(history, hub, clock);
  });
}
describe('resource history', () => {
  it('backfills surviving disk evidence immediately, preserving timestamps, source records and missing values', () => fixture((h, hub) => {
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('host-a','host-a','host-a','2026-01-01T00:00:00Z')");
    const payload = JSON.stringify({ filesystems: [{ mount_point: '/', percent: 82 }, { mount_point: '/missing', error: 'unknown', percent: 0 }] });
    for (const age of [2 * DAY, 60000]) hub.sql.exec('INSERT INTO disk_observations VALUES(?,?,?,?,?,?,?)', String(age), 'host-a', new Date(NOW - age).toISOString(), NOW - age, new Date(NOW).toISOString(), payload, 'host:test');
    h.backfill(); h.backfill();
    const data = h.query('7d');
    expect(data.earliest_observation).toBe(new Date(NOW - 2 * DAY).toISOString());
    expect(data.series).toHaveLength(1);
    expect(data.series[0].points.filter((p: any) => p.value !== null)).toHaveLength(2);
    expect(data.series[0].points.some((p: any) => p.value === null)).toBe(true);
    expect(data.series[0].points.filter((p: any) => p.value === 0)).toHaveLength(0);
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM disk_observations').one().n).toBe(2);
  }));
  it('samples at most once a minute and persists across service recreation', () => fixture((h, hub, c) => {
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat) VALUES('a','a','a','2026-01-01T00:00:00Z',?,?)", new Date(NOW).toISOString(), JSON.stringify({ mem_avail_mb: 32768, launcher_resources: { active_mem_mb: 2048, observed_at: new Date(NOW).toISOString() } }));
    h.sample(); h.sample();
    const n = Number(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n);
    c.t += 60000; h.sample();
    expect(hub.sql.exec('SELECT payload_json FROM resource_history').toArray().flatMap((r:any)=>JSON.parse(r.payload_json)).filter((o:any)=>o.kind==='reservation').length).toBe(1); // cached heartbeat is not a new measurement
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n).toBe(n + 1); // fresh session-state read
    const restored = new HostHistoryService(hub.sql, hub.fleetService, () => c.t);
    expect(restored.query('1h').series.find(s => s.metric === 'reserved_gib').points.some((p: any) => p.value === 2)).toBe(true);
  }));
  it('compacts old observations, retains over 30 days and never alters source records', () => fixture((h, hub, c) => {
    for (const day of [0, 3, 8, 30, 31]) for (const minute of [0, 1, 2]) h.record('host', 'a', 'a', NOW - day * DAY - minute * 60000, { load1: minute }, 'test');
    h.compact();
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history WHERE bucket_ms < ?', NOW - 30 * DAY).one().n).toBeGreaterThan(0);
    expect(h.query('1h').series[0].points.filter((p: any) => p.value !== null)).toHaveLength(3);
    c.t += 34 * DAY; h.compact();
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n).toBe(0);
  }));
  it('validates ranges and machine filtering and never serves unauthenticated history', () => fixture(async (h) => {
    h.record('host', 'a', 'a', NOW - 60000, { load1: 1 }, 'test');
    h.record('host', 'b', 'b', NOW - 60000, { load1: 2 }, 'test');
    for (const range of ['1h','24h','7d','30d']) {
      const r = h.handle(new Request('https://hub/api/monitoring/history?range=' + range + '&machine=a'), admin);
      expect(r.status).toBe(200);
      const data: any = await r.json(); expect(data.series.every((s: any) => !s.machine_id || s.machine_id === 'a')).toBe(true);
      expect(r.headers.get('cache-control')).toBe('private, no-store');
    }
    expect(h.handle(new Request('https://hub/api/monitoring/history?range=forever'), admin).status).toBe(400);
    expect(h.handle(new Request('https://hub/api/monitoring/history?machine=../secret'), admin).status).toBe(400);
    expect(h.handle(new Request('https://hub/api/monitoring/history'), {}).status).toBe(401);
  }));
  it('keeps explicit zero, splits gaps, rejects invalid/future values and respects newest evidence per bucket', () => fixture((h) => {
    h.record('host', 'a', 'a', NOW - 61000, { load1: 0, load15: null }, 'test');
    h.record('host', 'a', 'a', NOW - 65000, { load1: 9 }, 'older');
    h.record('host', 'a', 'a', NOW + 120000, { load1: 8 }, 'future');
    const data = h.query('1h');
    const points = data.series.find(s => s.metric === 'load1').points;
    expect(points.filter((p: any) => p.value !== null).map((p: any) => p.value)).toEqual([0]);
    expect(data.series.find(s => s.metric === 'load15').points.every((p: any) => p.value === null)).toBe(true);
  }));
  it('counts running native observations rather than historical launch records and leaves stale-host running unknown', () => fixture((h, hub, c) => {
    const at = new Date(NOW).toISOString();
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('live','live','live',?,?)",at,at);
    hub.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('live','n1','worker','running',?,'/repo')",at);
    hub.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('live','n2','old','unobserved',?,'/repo')",at);
    h.sample();
    let s = h.query('1h').series.find(s => s.kind === 'sessions' && s.machine_id === 'live' && s.metric === 'running');
    expect(s.points.filter((p: any) => p.value !== null).map((p: any) => p.value)).toEqual([1]);
    c.t += 4 * 60000; h.sample();
    s = h.query('1h').series.find(s => s.kind === 'sessions' && s.machine_id === 'live' && s.metric === 'running');
    expect(s.points.at(-1).value).toBeNull();
  }));
  it('bounds history dimensions and range response, reporting omitted series explicitly', () => fixture((h, hub) => {
    for (let i=0;i<520;i++) h.record('disk','/mount-'+i,'a',NOW,{percent:1},'test');
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history_entities').one().n).toBe(512);
    expect(h.query('1h').dimension_limit).toBe(true);
    expect(h.query('1h').truncated).toBe(true);
    expect(h.query('1h').series.length).toBe(256);
  }));

  it('job rescheduling preserves an existing minute-sampler deadline', () => fixture((h, hub, c) => {
    h.sample();
    c.t += 30000;
    expect(h.nextSampleAt()).toBe(NOW + 60000);
    let alarm: number | null = null;
    const jobs = new JobsService(hub.sql, {storage:{setAlarm:(at:number)=>{alarm=at;}}} as any,
      {enableAlarms:true,alarmDeadline:()=>h.nextSampleAt()});
    (jobs as any).getAlarmTime = () => NOW + 1800000;
    (jobs as any).scheduleAlarm();
    expect(alarm).toBe(NOW + 60000);
    (jobs as any).getAlarmTime = () => NOW + 1000;
    (jobs as any).scheduleAlarm();
    expect(alarm).toBe(NOW + 1000);
  }));

  it('preserves explicit unknown/error source observations as gaps instead of collector fallback zeroes', () => fixture((h, hub) => {
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('a','a','a','2026-01-01T00:00:00Z')");
    const payload = {load:{load1:0,load15:0,status:'unknown'},memory:{used_bytes:0,available_bytes:0,total_bytes:0,status:'unknown',error:'meminfo_unavailable'}};
    hub.sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','a',new Date(NOW).toISOString(),NOW,new Date(NOW).toISOString(),JSON.stringify(payload),'host:test');
    h.backfill();
    const series = h.query('1h').series;
    expect(series.length).toBe(4);
    expect(series.every(s=>s.points.every((p:any)=>p.value===null))).toBe(true);
    // An explicit unknown arriving later in a bucket invalidates the earlier
    // value; it must not leave a known zero or an interpolated plateau.
    h.record('host','a','a',NOW,{load1:null},'unknown');
    expect(h.query('1h').series.find(s=>s.metric==='load1').points.at(-1).value).toBeNull();
  }));

  it('joins historical aliases into one physical-host chart while preserving original rows', () => fixture((h, hub) => {
    const at = new Date(NOW).toISOString(), old = new Date(NOW-3*DAY).toISOString();
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('old','old','SAME',?,?)",old,old);
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('live','live','same',?,?)",old,at);
    h.record('host','old','old',NOW-2*DAY,{load1:1},'test');
    h.record('host','live','live',NOW,{load1:2},'test');
    const d=h.query('7d','live');
    expect(d.machine_aliases.old).toBe('live');
    expect(d.series).toHaveLength(1);
    expect(d.series[0].machine_id).toBe('live');
    expect(d.series[0].points.filter((p:any)=>p.value!==null).map((p:any)=>p.value)).toEqual([1,2]);
    expect(hub.sql.exec('SELECT payload_json FROM resource_history').toArray().flatMap((r:any)=>JSON.parse(r.payload_json)).filter((o:any)=>o.machine_id==='old').length).toBe(1);
  }));

  it('batches many account/host observations into one durable row per minute', () => fixture((h, hub) => {
    for(let i=0;i<100;i++) h.record('account','account-'+i,'',NOW,{used:i,max:100},'test');
    h.flush();
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n).toBe(1);
    const packet=JSON.parse(String(hub.sql.exec('SELECT payload_json FROM resource_history').one().payload_json));
    expect(packet.length).toBe(100);
    expect(h.query('1h').series.length).toBe(200);
  }));

  it('does not invent a running-session zero from a fresh heartbeat without a fresh native census', () => fixture((h, hub) => {
    const at=new Date(NOW).toISOString();
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('new-host','new-host','new-host',?,?)",at,at);
    h.sample();
    const series=h.query('1h').series.find(s=>s.kind==='sessions' && s.metric==='running');
    expect(series.points.every((p:any)=>p.value===null)).toBe(true);
  }));

});
