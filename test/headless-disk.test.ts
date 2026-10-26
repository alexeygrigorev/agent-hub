import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {SessionControlService} from '../src/session_control';
import {updateHeartbeat} from '../src/registry';
import {freshCpu} from './cpu-fixture';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';
const now=Date.now(),iso=(d=0)=>new Date(now+d).toISOString();
const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
const req=(extra={})=>({workspace:'demo',project:'repo',task_kind:'implement',prompt:'Read the task',mode:'headless',idempotency_key:crypto.randomUUID(),...extra});
async function fixture(fn:(f:FleetService,sql:any,beat:(busy:number,extra?:any)=>void,advance:(ms:number)=>void)=>Promise<void>) {
 const bindings=env as unknown as Env;
 await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('cpu-'+crypto.randomUUID())),async(h:Hub,state:DurableObjectState)=>{
  let clock=now;const f=new FleetService(h.sql,cb=>state.storage.transactionSync(cb),()=>clock);f.init();const sql=h.sql;
  const beat=(busy:number,extra={})=>sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?',JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[100,100,100],cpu_usage:freshCpu(iso(),8,busy),gate:{launch_allowed:true,at:iso()},...extra}),'host');
  sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES(?,?,?,?,?,?)','host','host','host',iso(),iso(),JSON.stringify({engines:['codex'],fleet_modes:['interactive','headless'],workspaces:['demo'],cpu_count:8}));beat(0);
  const quota={status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:80,reset_at:iso(86400000)}}};
  sql.exec('INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES(?,?,?,?,?,?,?,?,?)','host','1.0',iso(),now,iso(),now,'ok',null,JSON.stringify({codex:quota}));
  const configured=await f.handle(new Request('https://hub/api/fleet/accounts',{method:'PUT',body:JSON.stringify({accounts:[{label:'test-codex',provider:'codex',max_concurrency:20,reserve_percent:15,enabled:true}],routes:[{machine_id:'host',engine:'codex',account:'test-codex',profile:null}]})}),admin);
  expect(configured.status).toBe(200);await fn(f,sql,beat,ms=>{clock=now+ms});
 });
}

const http=(path:string,data:any)=>new Request('https://hub/api/fleet'+path,{method:'POST',body:JSON.stringify(data)});
const host={caller:{machineId:'host',kind:'host',actor:'machine:host'},scopes:['host']};
const ledger=(extra={})=>({active_mem_mb:0,active_disk_mb:19456,disk_floor_mb:20480,next_disk_mb:512,disk_aliases:{demo:"/srv/demo"},disk_task_ids:[],observed_at:iso(),...extra});
describe('headless disk admission and failure evidence',()=>{
 it('holds all eight burst requests at placement with real disk reason',()=>fixture(async(f,sql,beat)=>{
  beat(0,{disk_free_gb:35,launcher_resources:ledger()});
  const burst=await Promise.all(Array.from({length:8},async()=>f.reserve(req(),admin)));
  for(const r of burst){expect(r.status).toBe(202);expect(r.value.state).toBe('queued');expect(r.value.rejected).toContainEqual(expect.objectContaining({reason:'launcher_disk_reserve',disk:expect.objectContaining({reserved_disk_mb:19456,required_disk_gb:39.5})}));}
  expect(sql.exec("SELECT COUNT(*) n FROM fleet_launches WHERE state='failed'").one().n).toBe(0);
 }));
 it('admits eight when disk fits and accounts for offers between heartbeats',()=>fixture(async(f,sql,beat)=>{
  beat(0,{disk_free_gb:24,launcher_resources:ledger({active_disk_mb:0})});
  const burst=await Promise.all(Array.from({length:8},async()=>f.reserve(req(),admin)));
  expect(burst.map(r=>r.status)).toEqual(Array(8).fill(201));
  const ninth=f.reserve(req(),admin);expect(ninth.status).toBe(202);
  expect(ninth.value.rejected[0]).toMatchObject({reason:'launcher_disk_reserve',disk:{pending_disk_mb:4096,required_disk_gb:24.5}});
 }));
 it('does not charge launcher-covered offers twice and uses workspace filesystem',()=>fixture(async(f,sql,beat)=>{
  const first=f.reserve(req(),admin);expect(first.status).toBe(201);
  beat(0,{disk_free_gb:21,launcher_resources:ledger({active_disk_mb:512,disk_task_ids:['hub-'+first.value.id]})});
  expect(f.reserve(req(),admin).status).toBe(201);
  sql.exec('UPDATE machines SET capabilities=? WHERE id=?',JSON.stringify({engines:['codex'],fleet_modes:['headless','interactive'],workspaces:['/data/owned'],cpu_count:8}),'host');
  beat(0,{disk_free_gb:35,launcher_resources:ledger({disk_paths:[{path:'/data/owned',free_gb:190}]})});
  expect(f.plan(req({workspace:'/data/owned/review'})).selected).toBeTruthy();
  expect(f.plan(req({workspace:'/data/owned-other/review'})).selected).toBeNull();
 }));
 it('keeps absent and stale projection advisory, preserving the real disk floor',()=>fixture(async(f,sql,beat)=>{
  beat(0,{disk_free_gb:35});expect(f.plan(req()).selected).toBeTruthy();
  beat(0,{disk_free_gb:35,launcher_resources:ledger({observed_at:iso(-180001)})});expect(f.plan(req()).selected).toBeTruthy();
  beat(0,{disk_free_gb:2});expect(f.plan(req()).rejected![0].reason).toBe('disk_floor');
 }));
 it('enriches only an owning host legacy fenced terminal failure without retry',()=>fixture(async(f,sql)=>{
  const launch=f.reserve(req(),admin).value;
  expect((await f.handle(http('/launches/'+launch.id+'/claim',{gate_allowed:true,model:'gpt-6.1-sol',observed_at:iso(),account:'test-codex',profile_supported:true}),host)).status).toBe(200);
  const base={state:'failed',confirmed_exit:true,reason:'launcher_command_failed',launcher_task_id:'hub-'+launch.id,diagnostic:{reason:'launcher_command_failed',message:'The launcher command failed; unrecognized output was withheld.',error_type:'ValueError',stage:'dispatch_headless',exit_code:1}};
  expect((await f.handle(http('/launches/'+launch.id+'/result',base),host)).status).toBe(200);
  const diagnostic={...base.diagnostic,reason:'launcher_disk_reserve',message:'Host disk headroom after launcher reservations is below the maintained disk floor.'};
  expect((await f.handle(http('/launches/'+launch.id+'/result',{...base,reason:diagnostic.reason,diagnostic}),host)).status).toBe(200);
  expect(sql.exec('SELECT state,exit_reason FROM fleet_launches WHERE id=?',launch.id).one()).toMatchObject({state:'failed',exit_reason:'launcher_disk_reserve'});
  expect(sql.exec('SELECT COUNT(*) n FROM fleet_launches').one().n).toBe(1);
 }));
 it('preserves disk ValueError on queued session and waits for a newer fitting sample',()=>fixture(async(f,sql,beat,advance)=>{
  const sessions=new SessionControlService(sql,cb=>cb(),()=>now);sessions.init();
  beat(0,{disk_free_gb:80,launcher_resources:ledger({active_disk_mb:0})});
  const launch=f.reserve(req(),admin).value;
  const sid=crypto.randomUUID();
  sql.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,NULL,'headless','queued',?,?)",sid,'admin',sid,'{}',launch.id,iso(),iso());
  expect((await f.handle(http('/launches/'+launch.id+'/claim',{gate_allowed:true,model:'gpt-6.1-sol',observed_at:iso(),account:'test-codex',profile_supported:true}),host)).status).toBe(200);
  const diagnostic={reason:'launcher_disk_reserve',message:'Host disk headroom after launcher reservations is below the maintained disk floor.',error_type:'ValueError',exit_code:1,stage:'dispatch_headless'};
  const receipt={state:'failed',confirmed_exit:true,reason:diagnostic.reason,diagnostic,launcher_task_id:'hub-'+launch.id};
  beat(0,{disk_free_gb:35,launcher_resources:ledger()});
  const response=await f.handle(http('/launches/'+launch.id+'/result',receipt),host);
  expect(response.status).toBe(200);const queued:any=await response.json();expect(queued).toMatchObject({state:'queued',queued:true});expect(queued.id).not.toBe(launch.id);
  const view=()=> (sessions as any).view(sql.exec('SELECT * FROM session_control_sessions WHERE id=?',sid).one());
  expect(view()).toMatchObject({state:'queued',reason:'launcher_disk_reserve',launch_error:diagnostic});
  expect(await(await f.handle(http('/launches/'+launch.id+'/result',receipt),host)).json()).toMatchObject({id:queued.id,replay:true});
  f.drainCapacityQueue();expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',queued.id).one().state).toBe('queued');
  beat(0,{disk_free_gb:80,launcher_resources:ledger({active_disk_mb:0,observed_at:iso(1)})});
  advance(2);expect(f.drainCapacityQueue().promoted).toBe(1);
  expect(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id=?',queued.id).one().launch_error_json).toBeNull();
 }));
 it('maps named aliases and checks cwd plus workspace temp filesystem',()=>fixture(async(f,sql,beat)=>{
  sql.exec('UPDATE machines SET capabilities=? WHERE id=?',JSON.stringify({engines:['codex'],fleet_modes:['headless'],workspaces:['demo','/data/owned','/data/full'],cpu_count:8}),'host');
  beat(0,{disk_free_gb:35,launcher_resources:ledger({disk_aliases:{demo:'/data/owned'},disk_paths:[{path:'/data/owned',free_gb:190},{path:'/data/full',free_gb:19}]})});
  expect(f.plan(req()).selected).toBeTruthy();
  expect(f.plan(req({workspace:'/data/owned',cwd:'/data/full'})).rejected![0].reason).toBe('launcher_disk_reserve');
  beat(0,{disk_free_gb:80,launcher_resources:ledger({disk_paths:[{path:'/data/owned',free_gb:19},{path:'/data/full',free_gb:190}]})});
  expect(f.plan(req({workspace:'/data/owned',cwd:'/data/full'})).selected).toBeNull();
 }));
 it('requires newer real disk evidence after a proven no-start disk refusal',()=>fixture(async(f,sql,beat,advance)=>{
  beat(0,{disk_free_gb:80,launcher_resources:ledger({active_disk_mb:0})});const launch=f.reserve(req(),admin).value;
  await f.handle(http('/launches/'+launch.id+'/claim',{gate_allowed:true,model:'gpt-6.1-sol',observed_at:iso(),account:'test-codex',profile_supported:true}),host);
  const diagnostic={reason:'launcher_disk_reserve',message:'Host disk headroom after launcher reservations is below the maintained disk floor.',error_type:'ValueError',exit_code:1,stage:'dispatch_headless'};
  const q:any=await(await f.handle(http('/launches/'+launch.id+'/result',{state:'failed',confirmed_exit:true,reason:diagnostic.reason,diagnostic,launcher_task_id:'hub-'+launch.id}),host)).json();
  beat(8,{cpu_usage:{...freshCpu(iso(),8,8),psi_full_avg10:6}});expect(f.drainCapacityQueue().promoted).toBe(0);
  for(const extra of [{at:iso(1)},{launcher_resources:ledger({active_disk_mb:0,observed_at:iso(-180001)})},{launcher_resources:{active_mem_mb:0,observed_at:iso(1)}}]) {
   beat(0,extra);expect(f.drainCapacityQueue().promoted).toBe(0);expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',q.id).one().state).toBe('queued');
  }
  advance(2);
  beat(0,{launcher_resources:ledger({active_disk_mb:0,observed_at:iso(1),disk_paths:[{path:'/srv/demo',free_gb:80},{path:'/srv/demo/.local/tmp',free_gb:19}]})});
  expect(f.drainCapacityQueue().promoted).toBe(0);
  beat(0,{launcher_resources:ledger({active_disk_mb:0,observed_at:iso(1),disk_paths:[{path:'/srv/demo',free_gb:80},{path:'/srv/demo/.local/tmp',free_gb:80}]})});expect(f.drainCapacityQueue().promoted).toBe(1);
 }));
 it('sanitizes disk telemetry at ingestion, retaining no arbitrary fields',()=>fixture(async(f,sql)=>{
  const ingestId='disk-ingest-'+crypto.randomUUID();sql.exec('UPDATE machines SET id=? WHERE id=?',ingestId,'host');
  const paths=[{path:'/data/owned',free_gb:190,secret:'withheld'}];
  updateHeartbeat(ingestId,{type:'heartbeat',disk_free_gb:35,launcher_resources:ledger({disk_paths:paths,private:'withheld'})},sql);
  const hb=JSON.parse(sql.exec('SELECT last_heartbeat FROM machines WHERE id=?',ingestId).one().last_heartbeat);
  expect(hb.launcher_resources).toMatchObject({active_disk_mb:19456,disk_paths:[{path:'/data/owned',free_gb:190}]});
  expect(JSON.stringify(hb)).not.toContain('withheld');
 }));

});
