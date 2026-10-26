import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { ResourceManagement, resourceConfig, memoryVerdict, percentile } from '../src/resource-management';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
const now=Date.now(), iso=(d=0)=>new Date(now+d).toISOString();
async function fixture(fn:(r:ResourceManagement,sql:any,f:FleetService)=>Promise<void>) {
 const bindings=env as unknown as Env;
 await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('resource-'+crypto.randomUUID())),async(hub:Hub,state:DurableObjectState)=>{
  const f=new FleetService(hub.sql,cb=>state.storage.transactionSync(cb),()=>now);f.init();
  const sql=hub.sql,r=f.resources;
  sql.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES(?,?,?,?,?,?,?,?)','host','host','host',iso(),iso(),JSON.stringify({mem_avail_mb:3000,mem_total_mb:64000,load:[1,2,3],launcher_resources:{active_mem_mb:40000,observed_at:iso()}}),0,JSON.stringify({cpu_count:12}));
  const intake={id:'task',project:'demo',status:'open',acceptance_criteria:'Synthetic resource task acceptance criteria.',version:1,updated_at:now,history:[]};
  sql.exec('INSERT INTO intake_tasks VALUES(?,?)','task',JSON.stringify(intake));
  const binding={native_id:'native',tag:'agent',workspace:'/repo',native_created_at_ms:now-600000};
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,'launch','admin','human','work','key',JSON.stringify({intake_task_id:'task'}),'host','opencode','go','headless','/repo','goal','running','native',iso(-600000),iso());
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,'session','admin','control',JSON.stringify({intake_task_id:'task'}),'launch','host','opencode','headless','running',JSON.stringify(binding),iso(-600000),iso());
  sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,last_activity_at) VALUES(?,?,?,?,?,?,?,?)`,'host','native','agent','opencode','running',iso(),'/repo',iso(-3600001));
  await fn(r,sql,f);
 });
}
const sample=(extra={})=>({session_id:'session',native_id:'native',current_rss_mb:200,peak_rss_mb:200,peak_pss_mb:210,cpu_seconds:20,runtime_s:100,observed_at:iso(),...extra});
describe('resource management',()=>{
 it('keeps the configured host floor at the exact boundary and fails closed on invalid math',()=>{
  const cfg=resourceConfig();expect(memoryVerdict(2560,0,512,cfg)).toBe(true);expect(memoryVerdict(2559,0,512,cfg)).toBe(false);
  expect(memoryVerdict(2047,0,0,cfg)).toBe(false);expect(memoryVerdict(NaN,0,512,cfg)).toBe(false);expect(memoryVerdict(8000,-1000,512,cfg)).toBe(false);
  expect(()=>resourceConfig({memory_floor_mb:-1})).toThrow();expect(()=>resourceConfig({cpu_load_factor:Infinity})).toThrow();
  expect(percentile([100,200,300,400,500,600,700,800,900,1000],90)).toBe(900);
 });
 it('does not subtract resident caps but reserves unstarted ramps, crediting measured resident usage',async()=>fixture(async(r,sql)=>{
  expect(r.headroom('host')).toMatchObject({reserved_mb:0,requested_mb:512});
  sql.exec("UPDATE fleet_launches SET state='starting' WHERE id='launch'");
  expect(r.headroom('host').reserved_mb).toBe(512);
  r.report('host',{samples:[sample()],reaps:[]});
  expect(r.headroom('host').reserved_mb).toBe(312);
  r.report('host',{samples:[sample({current_rss_mb:100,peak_rss_mb:200})],reaps:[]});
  expect(r.headroom('host').reserved_mb).toBe(412); // peaks cannot masquerade as current resident credit
  expect(r.headroom('host').mem_available_mb).toBe(3000);
 }));
 it('persists monotonic peaks, CPU/runtime and metadata with exact native token join',async()=>fixture(async(r,sql)=>{
  r.report('host',{samples:[sample()],reaps:[]});
  r.report('host',{samples:[sample({peak_rss_mb:100,cpu_seconds:5,runtime_s:50})],reaps:[]});
  expect(r.usage()[0]).toMatchObject({session_id:'session',peak_rss_mb:200,peak_pss_mb:210,cpu_seconds:20,runtime_s:100,avg_cpu_cores:.2,project:'demo',engine:'opencode',token_join_key:'host:native'});
  sql.exec("UPDATE fleet_launches SET state='exited',finished_at=? WHERE id='launch'",iso());
  r.report('host',{samples:[sample()],reaps:[]});
  expect(r.estimate('demo','opencode','goal')).toBe(250);
  expect(r.estimate('other','opencode','review')).toBe(250);
  expect(r.estimate('/repo','codex','goal')).toBe(512);
  expect(()=>r.report('other',{samples:[sample()],reaps:[]})).toThrow('resource_binding_mismatch');
  expect(()=>r.report('host',{samples:[sample({native_id:'replacement'})],reaps:[]})).toThrow('resource_binding_mismatch');
 }));
 it('saves evidence and an intake restart note before accepting an idempotent confirmed reap',async()=>fixture(async(r,sql)=>{
  r.report('host',{samples:[sample()],reaps:[]});
  const evidence={id:'saved-output',session_id:'session',native_id:'native',reason:'idle',output:'Final report saved.'};
  expect(r.evidence('host',evidence)).toEqual({saved:true,evidence_id:'saved-output'});r.evidence('host',evidence);
  const reap={id:'reap-1',session_id:'session',native_id:'native',reason:'idle',freed_mb:200,at:iso(),evidence_id:'saved-output',stopped:true};
  expect(()=>r.report('host',{samples:[],reaps:[{...reap,evidence_id:'missing'}]})).toThrow('invalid_reap');
  r.report('host',{samples:[],reaps:[reap]});r.report('host',{samples:[],reaps:[reap]});
  expect(r.reaps()).toHaveLength(1);
  const task=JSON.parse(sql.exec("SELECT data FROM intake_tasks WHERE id='task'").one().data);
  expect(task.status).toBe('open');expect(task.history).toHaveLength(2);expect(task.history[1].text).toContain('restart');
  expect(r.usage()[0]).toMatchObject({state:'stopped',finished_at:iso()});
 }));
 it('settles a native binding once across changed attempts and preserves its completion state',async()=>fixture(async(r,sql)=>{
  sql.exec("UPDATE fleet_launches SET state='exited',finished_at=? WHERE id='launch'",iso());
  sql.exec("UPDATE session_control_sessions SET state='exited' WHERE id='session'");
  r.report('host',{samples:[sample()],reaps:[]});
  const evidence={id:'terminal-output',session_id:'session',native_id:'native',reason:'terminal',output:'saved'};
  r.evidence('host',evidence);
  const reap={id:'first-stop',session_id:'session',native_id:'native',reason:'terminal',freed_mb:null,at:iso(),evidence_id:evidence.id,stopped:true};
  r.report('host',{samples:[],reaps:[reap,{...reap,id:'changed-attempt'}]});
  expect(r.reaps()).toHaveLength(1);
  expect(r.sessions('host')[0].reaped).toBe(true);
  expect(r.usage()[0].state).toBe('exited');
  expect(sql.exec("SELECT state FROM session_control_sessions WHERE id='session'").one().state).toBe('exited');
  expect(()=>r.evidence('host',{...evidence,id:'new-evidence'})).toThrow('session_already_reaped');
  expect(r.evidence('host',evidence)).toEqual({saved:true,evidence_id:evidence.id});
  r.report('host',{samples:[],reaps:[{...reap,id:'later-stop'}]});
  expect(r.reaps()).toHaveLength(1);
  const history=JSON.parse(sql.exec("SELECT data FROM intake_tasks WHERE id='task'").one().data).history;
  expect(history).toHaveLength(2);
 }));
 it('never exposes protected or externally interactive sessions as managed reap targets',async()=>fixture(async(r,sql)=>{
  sql.exec('INSERT INTO session_control_protections VALUES(?,?,?,?)','session','human','keep',iso());
  expect(r.sessions('host')[0].keep).toBe(true);
  expect(()=>r.evidence('host',{id:'e',session_id:'session',native_id:'native',reason:'idle',output:'x'})).toThrow('session_not_reapable');
  sql.exec("DELETE FROM session_control_protections");
  sql.exec("UPDATE session_control_sessions SET mode='interactive',request_json=?",JSON.stringify({intake_task_id:'task',with_goal:true}));
  expect(r.sessions('host')[0].managed).toBe(false); // even interactive goals cannot prove they are not Alexey's own session
  sql.exec("UPDATE session_control_sessions SET fleet_launch_id=NULL,mode='interactive'");
  expect(r.sessions('host')[0].managed).toBe(false);
 }));
 it('preserves task kind and intake project through canonical start and replay',async()=>fixture(async(r,sql)=>{
  const calls:any[]=[];
  const service=new SessionControlService(sql,cb=>cb(),()=>now,{reserve:(input:any)=>{calls.push(input);return {status:201,value:{id:'metadata-launch',state:'offered',machine:'host',engine:'opencode'}};}});
  const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
  const start={key:'metadata',workspace:'/repo',brief:'Review the work',task_kind:'review',intake_task_id:'task'};
  const req=(input:any)=>new Request('https://hub/api/sessions',{method:'POST',body:JSON.stringify(input)});
  const response=await service.handle(req(start),admin);
  expect(response.status).toBe(201);
  expect(calls[0]).toMatchObject({project:'demo',task_kind:'review'});
  const body:any=await response.json();
  expect(JSON.parse(sql.exec('SELECT request_json FROM session_control_sessions WHERE id=?',body.id).one().request_json).task_kind).toBe('review');
  expect((await service.handle(req({...start,task_kind:'implement'}),admin)).status).toBe(409);
  expect((await service.handle(req({...start,key:'invalid',task_kind:'shell'}),admin)).status).toBe(400);
 }));
 it('authenticates own-host reporting and operator reads/configuration',async()=>fixture(async(r,sql,f)=>{
  const req=(path:string,method='GET',body?:any)=>new Request('https://hub/api/fleet/resources'+path,{method,body:body?JSON.stringify(body):undefined});
  expect((await f.handle(req(''),{caller:{kind:'host',machineId:'host'},scopes:['host']})).status).toBe(403);
  expect((await f.handle(req('/report','POST',{samples:[],reaps:[]}),{caller:{kind:'agent',actor:'agent'},scopes:['fleet:launch']})).status).toBe(403);
  expect((await f.handle(req('/watchdog'),{caller:{kind:'host',machineId:'host'},scopes:['host']})).status).toBe(200);
  const admin={caller:{isAdmin:true,actor:'admin',kind:'human'},scopes:['admin']};
  expect((await f.handle(req('/config','PUT',{memory_floor_mb:4096}),admin)).status).toBe(200);
  expect(r.headroom('host').floor_mb).toBe(4096);
 }));
});
