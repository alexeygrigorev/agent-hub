import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {freshCpu} from './cpu-fixture';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';
const now=Date.now(),iso=(d=0)=>new Date(now+d).toISOString();
const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
const req=(extra={})=>({workspace:'demo',project:'repo',task_kind:'implement',prompt:'Read the task',mode:'headless',idempotency_key:crypto.randomUUID(),...extra});
async function fixture(fn:(f:FleetService,sql:any,beat:(busy:number,extra?:any)=>void)=>Promise<void>) {
 const bindings=env as unknown as Env;
 await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('admission-batch-'+crypto.randomUUID())),async(h:Hub,state:DurableObjectState)=>{
  const f=new FleetService(h.sql,cb=>state.storage.transactionSync(cb),()=>now);f.init();const sql=h.sql;
  const beat=(busy:number,extra={})=>sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?',JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[100,100,100],cpu_usage:freshCpu(iso(),8,busy),gate:{launch_allowed:true,at:iso()},...extra}),'host');
  sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES(?,?,?,?,?,?)','host','host','host',iso(),iso(),JSON.stringify({engines:['codex'],fleet_modes:['interactive','headless'],workspaces:['demo'],cpu_count:8}));beat(0);
  const quota={status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}};
  sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES(?,?,?,?,?,?,?,?,?)','host','1.0',iso(),now,iso(),now,'ok',null,JSON.stringify({codex:quota}));
  const configured=await f.handle(new Request('https://hub/api/fleet/accounts',{method:'PUT',body:JSON.stringify({accounts:[{label:'test-codex',provider:'codex',max_concurrency:20,reserve_percent:15,enabled:true}],routes:[{machine_id:'host',engine:'codex',account:'test-codex',profile:null}]})}),admin);
  expect(configured.status).toBe(200);await fn(f,sql,beat);
 });
}
describe('batch admission uses real choking evidence',()=>{
 it('names a configured concurrency limit without blaming healthy quota or CPU',()=>fixture(async(f,sql)=>{
  sql.exec('UPDATE fleet_accounts SET max_concurrency=1');
  expect(f.reserve(req(),admin).status).toBe(201);
  const held=f.reserve(req(),admin);
  expect(held.status).toBe(202);
  expect(held.value.rejected).toEqual([{
   machine:'host',engine:'codex',reason:'account_concurrency',account:'test-codex',
   concurrency:{active:1,limit:1,available:0},quota_status:'ok',
  }]);
  expect(f.resources.cpuHeadroom('host',req()).admitted).toBe(true);
 }));
 it('places ten starts followed by five despite ramping above the CPU target',()=>fixture(async(f,sql,beat)=>{
  // Reproduce current=5.18, startup commitments growing past a 12-core target.
  // The fixture has 8 cores, making the advisory target stricter still.
  beat(5.18);
  const placed=[];
  for(let i=0;i<15;i++) {
   const start=f.reserve(req(),admin);
   expect(start.status,`batch start ${i+1}`).toBe(201);
   expect(start.value.state).toBe('offered');
   placed.push(start.value.id);
   if(i===9) {
    expect(f.resources.cpuHeadroom('host',req())).toMatchObject({
     admitted:true,reason:null,predicted_over_target:true,ramping_cpu_cores:10,
    });
   }
  }
  expect(new Set(placed).size).toBe(15);
  expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE state='queued'").one().n).toBe(0);
 }));
 it('holds on real FULL pressure and automatically places held work when it clears',()=>fixture(async(f,sql,beat)=>{
  for(let i=0;i<10;i++) expect(f.reserve(req(),admin).status).toBe(201);
  beat(5.18,{cpu_usage:{...freshCpu(iso(),8,5.18),psi_full_avg10:6}});
  const held=f.reserve(req(),admin);
  expect(held.status).toBe(202);
  expect(held.value.rejected).toContainEqual(expect.objectContaining({reason:'cpu_pressure'}));
  expect(held.value.rejected.some((r:any)=>['account_capacity','account_concurrency'].includes(r.reason))).toBe(false);
  beat(5.18);
  expect(f.drainCapacityQueue().promoted).toBe(1);
  expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',held.value.id).one().state).toBe('offered');
 }));
});
