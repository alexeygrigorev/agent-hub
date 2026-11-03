import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {freshCpu} from './cpu-fixture';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';

// Intake 189f6cd6: the 32 GiB laptop (LAPTOP-PHA6TCNC) queued its second
// light codex start with launcher_memory_reserve although ~5.1 GiB were
// really available and the running codex session used ~125 MiB. Reservations
// must come from measured per-engine p90 RSS over live MemAvailable with the
// host-proportional floor, and stale/leaked reservation rows (63a9ee3a) must
// never count: a host with ~1.6 GiB free admits light headless sessions until
// MemAvailable actually drops under the floor.

const now=Date.now(),iso=(d=0)=>new Date(now+d).toISOString();
const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
const req=(extra={})=>({workspace:'demo',project:'repo',engine:'codex',task_kind:'implement',mode:'headless',prompt:'Read the task',idempotency_key:crypto.randomUUID(),...extra});
const CODEX_TYPICAL=[80,90,100,105,110,115,120,122,125,130];    // p90 = 125 MiB
const CLAUDE_TYPICAL=[700,750,800,850,880,900,920,935,943,960]; // p90 = 943 MiB

async function fixture(fn:(f:FleetService,sql:any)=>Promise<void>,memAvail=1600) {
  const bindings=env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('lapmem-'+crypto.randomUUID())),async(h:Hub,state:DurableObjectState)=>{
    const f=new FleetService(h.sql,cb=>state.storage.transactionSync(cb),()=>now);f.init();const sql=h.sql;
    sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES(?,?,?,?,?,?,?)','host','host','host',iso(),iso(),
      JSON.stringify({mem_avail_mb:memAvail,mem_total_mb:32279,disk_free_gb:80,load:[1,1,1],cpu_usage:freshCpu(iso(),12,1),gate:{launch_allowed:true,at:iso()}}),
      JSON.stringify({engines:['codex','claude'],fleet_modes:['interactive','headless'],workspaces:['demo'],cpu_count:12}));
    const quota={status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}};
    sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES(?,?,?,?,?,?,?,?,?)',
      'host','1.0',iso(),now,iso(),now,'ok',null,JSON.stringify({codex:quota,claude:quota}));
    const configured=await f.handle(new Request('https://hub/api/fleet/accounts',{method:'PUT',body:JSON.stringify({
      accounts:[{label:'test-codex',provider:'codex',max_concurrency:50,reserve_percent:15,enabled:true},
                {label:'test-claude',provider:'claude',max_concurrency:50,reserve_percent:15,enabled:true}],
      routes:[{machine_id:'host',engine:'codex',account:'test-codex',profile:null},
              {machine_id:'host',engine:'claude',account:'test-claude',profile:null}]})}),admin);
    expect(configured.status).toBe(200);
    // Finished measured sessions feed the per-engine estimate cohorts.
    const seed=(engine:string,values:number[])=>{
      values.forEach((typical,i)=>{
        const id=`done-${engine}-${i}`;
        sql.exec(`INSERT INTO session_resource_usage(session_id,machine_id,native_id,project,engine,kind,started_at,finished_at,state,peak_rss_mb,observed_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)`,id,'host','native-'+id,'repo',engine,'implement',iso(-86400000),iso(-3600000),'exited',typical*2,iso(-3600000));
        sql.exec('INSERT INTO session_rss_median VALUES(?,?,?)',id,typical,iso(-3600000));
      });
    };
    seed('codex',CODEX_TYPICAL);
    seed('claude',CLAUDE_TYPICAL);
    const launch=(id:string,engine:string,st:string,ageMs:number)=>{
      sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES(?,'fixture','agent','fixture',?,?,'host',?,'test-'+?,'headless','demo','fixture',?,?,?)`,
        id,id,JSON.stringify({project:'repo',task_kind:'implement'}),engine,engine,st,iso(-ageMs),iso(-ageMs));
      if (st!=='offered') sql.exec('INSERT INTO resource_ramps VALUES(?,?)',id,iso(-ageMs));
    };
    await fn(f,{sql,launch});
  });
}

describe('light-host memory admission (189f6cd6)',()=>{
 it('sizes new sessions from the measured per-engine p90',()=>fixture(async(f)=>{
  expect(f.resources.estimate('repo','codex','implement')).toBe(Math.ceil(125*1.25));
  // A heavy cohort is capped so one engine can never reserve gigabytes.
  expect(f.resources.estimate('repo','claude','implement')).toBe(1024);
 }));

 it('admits the measured Claude p90 on a 32 GiB host with ~1.6 GiB available',()=>fixture(async(f)=>{
  const planned=f.plan(req({engine:'claude'}));
  expect(planned.rejected??[]).not.toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
  expect(planned.selected).toMatchObject({machine:'host'});
 }));

 it('a host with ~1.6 GiB free admits light codex sessions until the floor, leaked rows never count',()=>fixture(async(f,{launch})=>{
  const est=f.resources.estimate('repo','codex','implement');
  // Two codex sessions already admitted and ramping, plus stale leaked
  // offered, starting and uncertain rows (63a9ee3a) that must not count.
  launch('inflight-1','codex','offered',60000);
  launch('inflight-2','codex','starting',60000);
  launch('leaked-offered','codex','offered',4*3600000);
  launch('leaked-starting','codex','starting',4*3600000);
  launch('leaked-uncertain','codex','uncertain',4*3600000);
  const memory=f.resources.headroom('host',req(),'codex');
  expect(memory.floor_mb).toBe(512);          // 1.5% of RAM, with the 512 MiB minimum
  expect(memory.reserved_mb).toBe(2*est);     // leaked row ignored
  // Four more light Codex starts fit (six total including the active ramps).
  for (let i=3;i<=6;i++) {
    const planned=f.plan(req());
    expect(planned.rejected??[]).not.toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
    expect(planned.selected).toMatchObject({machine:'host'});
    launch(`inflight-${i}`,'codex',i%2?'offered':'starting',30000);
  }
  // The seventh start would leave less than the 512 MiB floor.
  const seventh=f.plan(req());
  expect(seventh.rejected).toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
  expect(seventh.selected).toBeNull();
 }));
});
