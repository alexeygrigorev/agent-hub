import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {SessionControlService} from '../src/session_control';
import {MonitoringService} from '../src/monitoring';
import {LivenessService,DEFAULT_THRESHOLDS} from '../src/liveness';
import {updateHeartbeat} from '../src/registry';
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
 it('admits idle CPU with history despite iowait-heavy load; busy+heavy admits until actual full pressure queues then drains',()=>fixture(async(f,sql,beat)=>{
  history(sql,'heavy','repo','implement',2);beat(.5);
  expect(f.plan(req()).selected).toMatchObject({expected_cpu_cores:2.5,expected_cpu_source:'repo_kind'});
  beat(8);expect(f.plan(req()).selected).toBeTruthy();
  beat(8,{cpu_usage:{...freshCpu(iso(),8,8),psi_full_avg10:6}});
  const queued=f.reserve(req(),admin);expect(queued.status).toBe(202);
  expect(queued.value.rejected[0]).toMatchObject({reason:'cpu_pressure',cpu:{current_cpu_cores:8,expected_cpu_cores:2.5,limit_cpu_cores:8}});
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
 it('credits fresh session CPU across real dispatcher heartbeat ordering and timestamp precision',()=>fixture(async(f,sql,beat)=>{
  const launch=f.reserve(req(),admin);expect(launch.status).toBe(201);
  const heartbeat={...freshCpu(iso(-60000)),observed_at:iso(-60000).replace('Z','456Z')};
  beat(0,{cpu_usage:heartbeat});
  // A session report follows a separate heartbeat poll, outside its 200ms window.
  sql.exec('INSERT INTO resource_cpu_samples VALUES(?,?,?)',launch.value.id,.6,iso(-59000));
  expect(f.resources.cpuHeadroom('host').ramping_cpu_cores).toBeCloseTo(.4);
  sql.exec('UPDATE resource_cpu_samples SET observed_at=?',iso(-181000));
  expect(f.resources.cpuHeadroom('host').ramping_cpu_cores).toBe(1);
  sql.exec('UPDATE resource_cpu_samples SET observed_at=?',iso(31000));
  expect(f.resources.cpuHeadroom('host').ramping_cpu_cores).toBe(1);
 }));
 it('freezes only real terminal CPU p95 while allowing live decreases and late final evidence',()=>fixture(async(f,sql)=>{
  const sid='cpu-final',binding={native_id:'native-final',tag:'agent',workspace:'/repo'};
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?)`,sid,'admin',sid,'{}','host','codex','interactive','running',JSON.stringify(binding),iso(-100000),iso());
  const report=(offset:number,extra:any={})=>f.resources.report('host',{samples:[{session_id:sid,native_id:'native-final',observed_at:iso(offset),...extra}],reaps:[]});
  const p95=()=>sql.exec('SELECT p95_cpu_cores FROM session_cpu_usage WHERE session_id=?',sid).one().p95_cpu_cores;
  report(-5000,{p95_cpu_cores:2,cpu_seconds:100,runtime_s:100});
  report(-4000,{p95_cpu_cores:1});expect(p95()).toBe(1);
  sql.exec("UPDATE session_control_sessions SET state='exited' WHERE id=?",sid);
  report(-3000,{peak_rss_mb:50});
  expect(sql.exec('SELECT * FROM session_cpu_finalized').toArray()).toHaveLength(0);
  report(-6000,{p95_cpu_cores:.1});expect(p95()).toBe(1); // out-of-order live sample cannot finalize
  expect(sql.exec('SELECT * FROM session_cpu_finalized').toArray()).toHaveLength(0);
  report(-2000,{p95_cpu_cores:1.5,cpu_seconds:120,runtime_s:110});expect(p95()).toBe(1.5);
  expect(sql.exec('SELECT * FROM session_cpu_finalized').toArray()).toHaveLength(1);
  report(-1000,{p95_cpu_cores:0,peak_rss_mb:80});expect(p95()).toBe(1.5);
  expect(f.resources.usage()[0]).toMatchObject({cpu_seconds:120,runtime_s:110,peak_rss_mb:80,workspace:'/repo'});
 }));
 it('keeps memory floor, unknown telemetry and PSI cap as distinct reasons',()=>fixture(async(f,_sql,beat)=>{
  beat(0,{mem_avail_mb:2047});expect(f.plan(req()).rejected![0].reason).toBe('launcher_memory_reserve');
  beat(0,{cpu_usage:null});expect(f.resources.cpuHeadroom('host',req())).toMatchObject({admitted:true,telemetry_state:'unknown'});
  beat(0,{cpu_usage:{...freshCpu(iso()),psi_full_avg10:6}});expect(f.plan(req()).rejected![0].reason).toBe('cpu_pressure');
 }));
 it('retains fresh OOM evidence through clear/unknown samples, then recovers after cooldown',()=>fixture(async(f,sql,beat)=>{
  beat(8,{cpu_usage:{...freshCpu(iso(),8,8),memory_psi_full_avg10:2}});
  expect(f.reserve(req(),admin).value.rejected[0].reason).toBe('memory_pressure');
  sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities,last_heartbeat) SELECT 'oom-host',name,hostname,registered_at,last_checkin_at,capabilities,last_heartbeat FROM machines WHERE id='host'");
  updateHeartbeat('oom-host',{type:'heartbeat',cpu_usage:{...freshCpu(iso()),oom_kills_delta:1}},sql);
  sql.exec("UPDATE machines SET last_heartbeat=? WHERE id='oom-host'",JSON.stringify({cpu_usage:freshCpu(iso())}));expect(f.resources.cpuHeadroom('oom-host').reason).toBe('host_oom');
  sql.exec("UPDATE machines SET last_heartbeat='{}' WHERE id='oom-host'");expect(f.resources.cpuHeadroom('oom-host').reason).toBe('host_oom');
  sql.exec('UPDATE resource_choking_events SET observed_at=?',iso(-301000));
  expect(f.resources.cpuHeadroom('oom-host')).toMatchObject({admitted:true,telemetry_state:'unknown'});
 }));
 it('holds on verified session runtime failures or fresh service timeout/crash observations',()=>fixture(async(f,sql,beat)=>{
  const start=f.reserve(req(),admin);expect(start.status,JSON.stringify(start.value)).toBe(201);
  sql.exec("UPDATE fleet_launches SET state='failed',exit_reason='engine_runtime_timeout' WHERE id=?",start.value.id);
  expect(f.resources.cpuHeadroom('host').reason).toBe('host_runtime_choking');
  sql.exec('UPDATE fleet_launches SET updated_at=? WHERE id=?',iso(-301000),start.value.id);
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
  const monitoring=new MonitoringService(sql,cb=>cb(),undefined,undefined,undefined,()=>now);monitoring.init();
  const health=(worker:string,reason:string)=>JSON.stringify({services:[{service_id:'controller',worker,reason,observed_at:null,status_file_age_s:null}]});
  sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','host',iso(),now,iso(),health('unknown','aplexer_timeout'),'test');
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
  sql.exec('UPDATE host_health_observations SET payload_json=?',health('alive','stale_status_file'));
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
  // Actual collect_service_status DTO: native worker broken, no status source.
  sql.exec('UPDATE host_health_observations SET payload_json=?',health('broken','worker_broken'));
  expect(f.resources.cpuHeadroom('host').reason).toBe('host_runtime_choking');
  sql.exec('UPDATE host_health_observations SET payload_json=?',health('broken','stale_status_file'));
  expect(f.resources.cpuHeadroom('host').reason).toBe('host_runtime_choking');
  sql.exec('UPDATE host_health_observations SET observed_at=?',iso(-301000));
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
 }));
 it('retains authenticated service failure evidence across healthy snapshots only for the cooldown',()=>fixture(async(f,sql)=>{
  const monitoring=new MonitoringService(sql,cb=>cb(),undefined,undefined,undefined,()=>now);monitoring.init();
  const post=(worker:string,observed=iso(),source:any=null)=>monitoring.handleHostHealthObservation(new Request('https://hub/api/monitoring/health',{method:'POST',body:JSON.stringify({
   machine_id:'host',observed_at:observed,load:{load1:1,load5:1,load15:1},
   memory:{total_bytes:64000,available_bytes:32000,used_bytes:32000,percent:50,status:'ok'},
   services:[{service_id:'controller',worker,reason:worker==='broken'?'worker_broken':'worker_unknown',observed_at:source}],
  })}),{kind:'host',machine_id:'host',tokenId:'host-token'});
  expect((await post('broken',iso(),iso(120000))).status).toBe(200);
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true); // future source degrades to unknown
  expect((await post('unknown')).status).toBe(200);
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
  expect((await post('broken')).status).toBe(200); // positively observed worker, no status file
  expect(f.resources.cpuHeadroom('host').resource_choking).toEqual({reason:'host_runtime_choking',observed_at:iso()});
  expect((await post('alive')).status).toBe(200);
  expect(f.resources.cpuHeadroom('host').reason).toBe('host_runtime_choking');
  expect((await post('unknown')).status).toBe(200);
  expect(f.resources.cpuHeadroom('host').reason).toBe('host_runtime_choking');
  sql.exec('UPDATE resource_choking_events SET observed_at=?',iso(-301000));
  expect(f.resources.cpuHeadroom('host').admitted).toBe(true);
 }));
 it('preserves bounded local refusal math through a fenced no-start retry',()=>fixture(async(f,sql)=>{
  const start=f.reserve(req(),admin);expect(start.status).toBe(201);
  sql.exec("UPDATE fleet_launches SET state='starting' WHERE id=?",start.value.id);
  const cpu={current_cpu_cores:8,expected_cpu_cores:1,ramping_cpu_cores:2,limit_cpu_cores:8,target_utilization:1,psi_full_avg10:7,observed_at:iso()};
  const diagnostic={reason:'cpu_pressure',message:'Host CPU pressure exceeds the safety cap.',error_type:'RuntimeError',stage:'launcher_run',cpu};
  const post=(data:any)=>f.handle(new Request('https://hub/api/fleet/launches/'+start.value.id+'/result',{method:'POST',body:JSON.stringify(data)}),{caller:{kind:'host',machineId:'host'},scopes:['host']});
  const body={state:'failed',confirmed_exit:true,launcher_task_id:'hub-'+start.value.id,reason:'cpu_pressure',diagnostic};
  expect((await post({...body,diagnostic:{...diagnostic,cpu:{...cpu,secret:'private'}}})).status).toBe(400);
  const result=await post(body);expect(result.status).toBe(200);
  const queued:any=await result.json();expect(queued.state).toBe('queued');
  const hold:any=sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?',queued.id).one();
  expect(JSON.parse(hold.rejected_json)[0]).toMatchObject({reason:'cpu_pressure',cpu:{...cpu,expected_cpu_source:'default'}});
 }));
 it('samples old active sessions ahead of more than 1000 recent terminal sessions',()=>fixture(async(f,sql)=>{
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('old-live','admin','old-live','{}','host','codex','interactive','running',?,?,?)`,JSON.stringify({native_id:'old-native',tag:'old',workspace:'/repo',native_created_at_ms:now-100000}),iso(-86400000),iso(-86400000));
  sql.exec(`WITH RECURSIVE n(x) AS(SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1001)
    INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    SELECT 'dead-'||x,'admin','dead-'||x,'{}','host','codex','interactive','stopped',json_object('native_id','dead-'||x,'tag','dead','workspace','/repo'),?,? FROM n`,iso(),iso());
  expect(f.resources.sessions('host')[0]).toMatchObject({session_id:'old-live',started_at:iso(-100000)});
 }));
 it('shows CPU source in session details and truthful status labels',()=>fixture(async(f,sql,beat)=>{
  const launch=f.reserve(req(),admin);expect(launch.status,JSON.stringify(launch.value)).toBe(201);
  const s=new SessionControlService(sql,cb=>cb(),()=>now);s.init();
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)`,'11111111-1111-4111-8111-111111111111','admin','session-key','{}',launch.value.id,'host','codex','interactive','offered',iso(),iso());
  const response=await s.handle(new Request('https://hub/api/sessions/11111111-1111-4111-8111-111111111111'),admin);
  expect(await response.json()).toMatchObject({expected_cpu_cores:1,expected_cpu_source:'default'});
  const binding={native_id:'native',tag:'agent',workspace:'/repo',native_created_at_ms:now-100000};
  sql.exec('UPDATE session_control_sessions SET binding_json=? WHERE id=?',JSON.stringify(binding),'11111111-1111-4111-8111-111111111111');
  f.resources.report('host',{samples:[{session_id:'11111111-1111-4111-8111-111111111111',native_id:'native',cpu_seconds:40,runtime_s:100,p95_cpu_cores:1.5,current_cpu_cores:.5,observed_at:iso()}],reaps:[]});
  expect(f.resources.usage()[0]).toMatchObject({workspace:'/repo',cpu_seconds:40,avg_cpu_cores:.4,p95_cpu_cores:1.5,engine:'codex'});
  beat(7,{cpu_usage:{...freshCpu(iso(),8,7),psi_full_avg10:6}});f.reserve(req(),admin);
  const monitoring=new MonitoringService(sql,cb=>cb(),undefined,undefined,undefined,()=>now);monitoring.init();
  sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','host',iso(),now,iso(),JSON.stringify({memory:{total_bytes:64000,available_bytes:32000,used_bytes:32000,percent:50,status:'ok'},load:{load1:1,load5:1,load15:1}}),'test');
  const status=monitoring.getStatusSummary();
  expect(JSON.stringify(status)).toContain('cpu_pressure');
  expect(JSON.stringify(status)).not.toContain('held queued for memory/capacity');
  sql.exec('UPDATE fleet_memory_holds SET since=?',iso(-301000));
  const live=new LivenessService(sql,DEFAULT_THRESHOLDS,()=>now);live.init();
  expect(live.evaluate().subjects.some(s=>s.state==='cpu_pressure'&&s.detail.includes('cpu_pressure hold'))).toBe(true);
 }));
 it('explains expected CPU for an intent queued by memory before placement',()=>fixture(async(f,sql,beat)=>{
  beat(0,{mem_avail_mb:2047});const queued=f.reserve(req(),admin);expect(queued.status).toBe(202);
  const s=new SessionControlService(sql,cb=>cb(),()=>now);s.init();
  const id='22222222-2222-4222-8222-222222222222';
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,mode,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)`,id,'admin','queued-cpu','{}',queued.value.id,'interactive','queued',iso(),iso());
  const response=await s.handle(new Request('https://hub/api/sessions/'+id),admin);
  expect(await response.json()).toMatchObject({expected_cpu_cores:1,expected_cpu_source:'default',reason:'launcher_memory_reserve'});
 }));
});
