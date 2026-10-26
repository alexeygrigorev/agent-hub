import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {SessionControlService} from '../src/session_control';
import {MonitoringService} from '../src/monitoring';
import {freshCpu} from './cpu-fixture';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';
const now=Date.now(),iso=(d=0)=>new Date(now+d).toISOString();
const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
const req=(extra={})=>({workspace:'demo',project:'repo',task_kind:'implement',prompt:'Read the task',mode:'interactive',idempotency_key:crypto.randomUUID(),...extra});
async function fixture(fn:(f:FleetService,sql:any,beat:(busy:number,extra?:any)=>void)=>Promise<void>) {
 const bindings=env as unknown as Env;
 await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('cpu-'+crypto.randomUUID())),async(h:Hub,state:DurableObjectState)=>{
  const f=new FleetService(h.sql,cb=>state.storage.transactionSync(cb),()=>now);f.init();const sql=h.sql;
  const beat=(busy:number,extra={})=>sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?',JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[100,100,100],cpu_usage:freshCpu(iso(),8,busy),gate:{launch_allowed:true,at:iso()},...extra}),'host');
  sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES(?,?,?,?,?,?)','host','host','host',iso(),iso(),JSON.stringify({engines:['codex'],fleet_modes:['interactive','headless'],workspaces:['demo'],cpu_count:8}));beat(0);
  const quota={status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}};
  sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES(?,?,?,?,?,?,?,?,?)','host','1.0',iso(),now,iso(),now,'ok',null,JSON.stringify({codex:quota}));
  const configured=await f.handle(new Request('https://hub/api/fleet/accounts',{method:'PUT',body:JSON.stringify({accounts:[{label:'test-codex',provider:'codex',max_concurrency:20,reserve_percent:15,enabled:true}],routes:[{machine_id:'host',engine:'codex',account:'test-codex',profile:null}]})}),admin);
  expect(configured.status).toBe(200);await fn(f,sql,beat);
 });
}
function history(sql:any,id:string,project:string,kind:string,p95:number) {
 sql.exec(`INSERT INTO session_resource_usage(session_id,machine_id,native_id,project,engine,kind,started_at,finished_at,state,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)`,id,'host','native-'+id,project,'codex',kind,iso(-120000),iso(-60000),'exited',iso());
 sql.exec('INSERT INTO session_cpu_usage VALUES(?,?,?)',id,p95,iso());
}
describe('CPU predictive placement integration',()=>{
 it('uses repo+kind, repo, global and conservative no-history estimates',()=>fixture(async(f,sql)=>{
  expect(f.resources.cpuEstimate('repo','implement')).toEqual({expected_cpu_cores:1,expected_cpu_source:'default'});
  history(sql,'other','other','review',4);
  expect(f.resources.cpuEstimate('repo','implement')).toEqual({expected_cpu_cores:5,expected_cpu_source:'global'});
  history(sql,'repo','repo','review',2);
  expect(f.resources.cpuEstimate('repo','implement')).toEqual({expected_cpu_cores:2.5,expected_cpu_source:'repo'});
  history(sql,'kind','repo','implement',1);
  expect(f.resources.cpuEstimate('repo','implement')).toEqual({expected_cpu_cores:1.25,expected_cpu_source:'repo_kind'});
 }));
 it('admits idle CPU with history despite iowait-heavy load; busy+heavy queues then drains',()=>fixture(async(f,sql,beat)=>{
  history(sql,'heavy','repo','implement',2);beat(.5);
  expect(f.plan(req()).selected).toMatchObject({expected_cpu_cores:2.5,expected_cpu_source:'repo_kind'});
  beat(6);
  const queued=f.reserve(req(),admin);expect(queued.status).toBe(202);
  expect(queued.value.rejected[0]).toMatchObject({reason:'cpu_headroom',cpu:{current_cpu_cores:6,expected_cpu_cores:2.5,limit_cpu_cores:7.2}});
  beat(.5);expect(f.drainCapacityQueue().promoted).toBe(1);
  expect(f.resources.cpuStart(queued.value.id)).toMatchObject({expected_cpu_cores:2.5,expected_cpu_source:'repo_kind'});
 }));
 it('freezes start estimates, reserves startup remainder, and excludes stable/queued work',()=>fixture(async(f,sql)=>{
  const first=f.reserve(req(),admin);expect(first.status).toBe(201);
  f.resources.configure({cpu_default_cores:2});
  expect(f.resources.cpuHeadroom('host',req()).ramping_cpu_cores).toBe(1);
  expect(f.resources.cpuHeadroom('host',req(),first.value.id).ramping_cpu_cores).toBe(0);
  expect(f.resources.cpuHeadroom('host',req(),first.value.id).expected_cpu_cores).toBe(1);
  sql.exec('INSERT INTO resource_cpu_samples VALUES(?,?,?)',first.value.id,.6,iso());
  expect(f.resources.cpuHeadroom('host',req()).ramping_cpu_cores).toBeCloseTo(.4);
  sql.exec("UPDATE fleet_launches SET state='running',created_at=? WHERE id=?",iso(-181000),first.value.id);
  expect(f.resources.cpuHeadroom('host',req()).ramping_cpu_cores).toBe(0);
  sql.exec("UPDATE fleet_launches SET state='queued' WHERE id=?",first.value.id);
  expect(f.resources.cpuHeadroom('host',req()).ramping_cpu_cores).toBe(0);
 }));
 it('keeps memory floor, unknown telemetry and PSI cap as distinct reasons',()=>fixture(async(f,_sql,beat)=>{
  beat(0,{mem_avail_mb:2047});expect(f.plan(req()).rejected![0].reason).toBe('launcher_memory_reserve');
  beat(0,{cpu_usage:null});expect(f.reserve(req(),admin).value.rejected[0].reason).toBe('cpu_usage_unknown');
  beat(0,{cpu_usage:{...freshCpu(iso()),psi_avg10:51}});expect(f.plan(req()).rejected![0].reason).toBe('cpu_pressure');
 }));
 it('shows CPU source in session details and truthful status labels',()=>fixture(async(f,sql,beat)=>{
  const launch=f.reserve(req(),admin);expect(launch.status).toBe(201);
  const s=new SessionControlService(sql,cb=>cb(),()=>now);s.init();
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)`,'11111111-1111-4111-8111-111111111111','admin','session-key','{}',launch.value.id,'host','codex','interactive','offered',iso(),iso());
  const response=await s.handle(new Request('https://hub/api/sessions/11111111-1111-4111-8111-111111111111'),admin);
  expect(await response.json()).toMatchObject({expected_cpu_cores:1,expected_cpu_source:'default'});
  const binding={native_id:'native',tag:'agent',workspace:'/repo',native_created_at_ms:now-100000};
  sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify(binding),'11111111-1111-4111-8111-111111111111');
  f.resources.report('host',{samples:[{session_id:'11111111-1111-4111-8111-111111111111',native_id:'native',cpu_seconds:40,runtime_s:100,p95_cpu_cores:1.5,current_cpu_cores:.5,observed_at:iso()}],reaps:[]});
  expect(f.resources.usage()[0]).toMatchObject({workspace:'/repo',cpu_seconds:40,avg_cpu_cores:.4,p95_cpu_cores:1.5,engine:'codex'});
  beat(7);f.reserve(req(),admin);
  const monitoring=new MonitoringService(sql,cb=>cb(),undefined,undefined,undefined,()=>now);monitoring.init();
  sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','host',iso(),now,iso(),JSON.stringify({memory:{total_bytes:64000,available_bytes:32000,used_bytes:32000,percent:50,status:'ok'},load:{load1:1,load5:1,load15:1}}),'test');
  const status=monitoring.getStatusSummary();
  expect(JSON.stringify(status)).toContain('cpu_headroom');
  expect(JSON.stringify(status)).not.toContain('held queued for memory/capacity');
 }));
});
