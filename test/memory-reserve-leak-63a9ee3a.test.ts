import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {IN_FLIGHT_RESERVE_SECONDS} from '../src/resource-management';
import {freshCpu} from './cpu-fixture';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';

// Intake 63a9ee3a (orig 3d0c8852): launches stuck in offered/starting/uncertain
// kept a full pending ramp reservation forever. On 28-29 Oct 11 uncertain
// rows (1.8-15 h old) held ~21 GiB on the only routing host, so every new
// start was refused with launcher_memory_reserve.
const now=Date.now(),iso=(d=0)=>new Date(now+d).toISOString();
const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
const req=(extra={})=>({workspace:'demo',project:'repo',task_kind:'implement',prompt:'Read the task',mode:'interactive',idempotency_key:crypto.randomUUID(),...extra});
async function fixture(fn:(f:FleetService,sql:any,launch:(id:string,state:string,ageMs:number,ramp?:boolean)=>void)=>Promise<void>) {
  const bindings=env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('memres-'+crypto.randomUUID())),async(h:Hub,state:DurableObjectState)=>{
    const f=new FleetService(h.sql,cb=>state.storage.transactionSync(cb),()=>now);f.init();const sql=h.sql;
    sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES(?,?,?,?,?,?,?)','host','host','host',iso(),iso(),
      JSON.stringify({mem_avail_mb:12000,disk_free_gb:80,load:[1,1,1],cpu_usage:freshCpu(iso(),12,1),gate:{launch_allowed:true,at:iso()}}),
      JSON.stringify({engines:['codex'],fleet_modes:['interactive','headless'],workspaces:['demo'],cpu_count:12}));
    const quota={status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}};
    sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES(?,?,?,?,?,?,?,?,?)','host','1.0',iso(),now,iso(),now,'ok',null,JSON.stringify({codex:quota}));
    const configured=await f.handle(new Request('https://hub/api/fleet/accounts',{method:'PUT',body:JSON.stringify({accounts:[{label:'test-codex',provider:'codex',max_concurrency:50,reserve_percent:15,enabled:true}],routes:[{machine_id:'host',engine:'codex',account:'test-codex',profile:null}]})}),admin);
    expect(configured.status).toBe(200);
    const launch=(id:string,st:string,ageMs:number,ramp=st!=='offered')=>{
      sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES(?,'fixture','agent','fixture',?,?,'host','codex','test-codex','interactive','demo','fixture',?,?,?)`,id,id,JSON.stringify({project:'repo',task_kind:'implement'}),st,iso(-ageMs),iso(-ageMs));
      if (ramp) sql.exec('INSERT INTO resource_ramps VALUES(?,?)',id,iso(-ageMs));
    };
    await fn(f,sql,launch);
  });
}

describe('launcher memory reservation leak (63a9ee3a)',()=>{
  it('stale uncertain/starting/offered launches no longer hold memory and starve placement',()=>fixture(async(f,_sql,launch)=>{
    const est=f.resources.estimate('repo','codex','implement');
    // Reproduce the live shape: hours-old uncertain launches plus one stuck
    // starting and one never-claimed offer, all far past any ramp window.
    for (let i=0;i<16;i++) launch('stale-uncertain-'+i,'uncertain',(2+i)*3600000);
    launch('stale-starting','starting',6*3600000);
    launch('stale-offered','offered',6*3600000);
    const memory=f.resources.headroom('host',req(),'codex');
    expect(memory.reserved_mb).toBe(0);
    const plan=f.plan(req());
    expect(plan.rejected??[]).not.toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
    expect(plan.selected).toMatchObject({machine:'host'});
    // The same stale rows no longer pin CPU ramp commitments either.
    expect(f.resources.cpuHeadroom('host',req()).ramping_cpu_cores).toBe(0);
    expect(est).toBeGreaterThan(0);
  }));

  it('fresh in-flight launches still reserve until the bounded window passes, and real pressure still refuses',()=>fixture(async(f,_sql,launch)=>{
    const est=f.resources.estimate('repo','codex','implement');
    const inside=(IN_FLIGHT_RESERVE_SECONDS-60)*1000, outside=(IN_FLIGHT_RESERVE_SECONDS+60)*1000;
    launch('fresh-offered','offered',inside);
    launch('fresh-starting','starting',inside);
    launch('fresh-uncertain','uncertain',inside);
    launch('old-uncertain','uncertain',outside);
    launch('old-offered','offered',outside);
    // Running rows keep the shorter ramp_seconds window.
    launch('ramping-running','running',1000);
    launch('settled-running','running',outside);
    expect(f.resources.headroom('host',req(),'codex').reserved_mb).toBe(4*est);
    // Enough fresh commitments still refuse with launcher_memory_reserve.
    for (let i=0;i<30;i++) launch('burst-'+i,'starting',1000);
    expect(f.plan(req()).rejected).toContainEqual(expect.objectContaining({reason:'launcher_memory_reserve'}));
  }));
});
