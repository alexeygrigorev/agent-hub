import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {fixtureSql} from './helpers/fixture-budget';
import {deployedUserSchema} from './fixtures/production-schema-t2609u';
import {MetadataCompat,MetadataCompatibilityError} from '../src/metadata-compat';
import {IntakeVerifier} from '../src/intake-verifier';
import {IntakeTriage} from '../src/intake-triage';
import {AskLedger} from '../src/asks';
import {RoutingService,effectiveRoutingObservedAt} from '../src/routing';
import {HostGoals} from '../src/host-goals';
const fixture=(fn:(x:any)=>Promise<void>|void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('metadata-compat-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),tx=(fn:any)=>state.storage.transactionSync(fn);
 await fn({h,state,seed,tx});expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
function oldTable(seed:any,name:string){seed.exec('DROP TABLE '+name);seed.exec(deployedUserSchema.find(row=>row.name===name)!.sql);}
const request=(path:string,data:any,method='POST')=>new Request('https://hub.test/api/routing/'+path,{method,body:JSON.stringify(data)});
function routingSetup(h:any,seed:any,tx:any){
 let now=Date.now();const at=new Date(now).toISOString();
 seed.exec(`INSERT INTO machines(id,name,hostname,roles,capabilities,registered_at,credential_id,approval_seq) VALUES('compat-host','compat-host','host','[]','{}',?,'compat-owner',7)`,at);
 seed.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,approval_seq)
 VALUES('compat-owner','host','compat-hash','compat-refresh','compat-family','host','compat-host','machine:compat-host','["host"]','fixture',?,?,7)`,at,new Date(now+86400000).toISOString());
 const auth={caller:{kind:'host',machineId:'compat-host',tokenId:'compat-owner',tokenHash:'compat-hash'},scopes:['host']};
 const service=new RoutingService(h.sql,h.fleetService,()=>now,tx,h.writes);service.init();
 return {service,auth,at,get now(){return now;},advance(ms:number){now+=ms;}};
}
describe('legacy optional metadata uses point-owned sidecars',()=>{
 it('does not ALTER legacy metadata, preserves flags and advances fair cursors across instances and rollback',()=>fixture(async({h,state,seed,tx}:any)=>{
  for(const name of ['intake_verifier_meta','intake_triage_meta','ask_meta','host_release_targets'])oldTable(seed,name);
  seed.exec("INSERT INTO intake_verifier_meta(id,enabled_at,last_tick,last_result) VALUES(1,17,NULL,'old-verifier')");
  seed.exec("INSERT INTO intake_triage_meta(id,last_tick,last_source,last_result,last_session,last_alert) VALUES(1,NULL,'operator','old-triage',NULL,NULL)");
  seed.exec("INSERT INTO ask_meta(id,gap_scope,last_tick,last_result,reconcile_intake,reconcile_session) VALUES(1,'global',11,'old-ask','kept-intake','kept-session')");
  let now=Date.now();const tasks=Array.from({length:130},(_,i)=>({id:'task-'+String(i).padStart(3,'0'),status:'done',history:[]}));
  const seen:string[]=[];const intake:any={statusAuthorizedTasks:()=>[],watchdogTasks:(after:string,limit:number)=>{seen.push(after);return tasks.filter(t=>t.id>after).slice(0,limit);}};
  const services=()=>({v:new IntakeVerifier(h.sql,intake,tx,{},()=>now),t:new IntakeTriage(h.sql,intake,{project:()=>[]} as any,tx,{},()=>now),a:new AskLedger(h.sql,h.intakeService,tx,{},()=>now)});
  let s=services();h.meter.runSync('legacy-metadata-init',()=>{s.v.init();s.t.init();s.a.init();new RoutingService(h.sql,h.fleetService,()=>now,tx,h.writes).init();});
  expect(h.meter.statementTop(4096).filter((r:any)=>r.scope==='legacy-metadata-init'&&/^ALTER /i.test(r.sql))).toEqual([]);
  for(const [name,missing] of [['intake_verifier_meta','task_cursor'],['intake_triage_meta','request_cursor'],['ask_meta','reconcile_cursor'],['host_release_targets','autostart_error']])expect(state.storage.sql.exec('PRAGMA table_info('+name+')').toArray().some((r:any)=>r.name===missing)).toBe(false);
  expect(state.storage.sql.exec('SELECT enabled_at,last_result FROM intake_verifier_meta').one()).toMatchObject({enabled_at:17,last_result:'old-verifier'});
  expect((s.a as any).meta()).toMatchObject({gap_scope:'global',last_tick:11,last_result:'old-ask',reconcile_intake:'kept-intake',reconcile_session:'kept-session'});
  await s.v.tick();await s.t.tick();const first=seen.slice();expect(first).toEqual(['','']);
  now+=7200000;s=services();s.v.init();s.t.init();s.a.init();await s.v.tick();await s.t.tick();expect(seen.slice(2).every(x=>x==='task-063')).toBe(true);
  (s.a as any).reconcileMeta(72,{last_reconcile_run:'kept',last_reconcile_at:now});(s.a as any).reconcileMeta(12);
  expect((s.a as any).meta()).toMatchObject({reconcile_cursor:72,last_reconcile_run:'kept'});
  expect(()=>tx(()=>{(s.a as any).reconcileMeta(99,{last_reconcile_run:'rolled back'});throw new Error('rollback-metadata');})).toThrow('rollback-metadata');
  // A separate reader/writer over initialized legacy schema retains the old
  // contract: it need not rerun init() or rewrite metadata to use cursors.
  const cold=services().a;expect((cold as any).meta()).toMatchObject({reconcile_cursor:72,last_reconcile_run:'kept'});
  (cold as any).reconcileMeta(73);expect((services().a as any).meta().reconcile_cursor).toBe(73);
  new MetadataCompat(h.sql,'ask_meta',['reconcile_cursor']);
  expect((services().a as any).meta()).toMatchObject({reconcile_cursor:73,last_reconcile_run:'kept'});
  new MetadataCompat(h.sql,'ask_meta',['reconcile_cursor','last_reconcile_run','last_reconcile_at'],'gap_scope');
  expect((services().a as any).meta()).toMatchObject({reconcile_cursor:73,last_reconcile_run:'kept'});
 }));
 it('preserves explicit null, caps encoded bytes before effects and keeps sidecar fields atomic with caller transactions',()=>fixture(({h,seed,tx}:any)=>{
  seed.exec('CREATE TABLE legacy_optional(id INTEGER PRIMARY KEY,old_flag TEXT)');seed.exec("INSERT INTO legacy_optional VALUES(1,'kept')");
  const meta=new MetadataCompat(h.sql,'legacy_optional',['cursor','error']);
  tx(()=>meta.patch(1,{cursor:3,error:null}));expect(meta.read(1)).toMatchObject({old_flag:'kept',cursor:3,error:null});
  expect(()=>tx(()=>meta.patch(1,{cursor:4,error:'x'.repeat(65537)}))).toThrow(MetadataCompatibilityError);expect(meta.read(1).cursor).toBe(3);
  expect(()=>tx(()=>{meta.patch(1,{cursor:5});throw new Error('rollback-sidecar');})).toThrow('rollback-sidecar');expect(new MetadataCompat(h.sql,'legacy_optional',['cursor','error']).read(1).cursor).toBe(3);
  seed.exec("UPDATE metadata_compat_fields SET value=? WHERE scope='legacy_optional' AND field='error'",'"'+'x'.repeat(65537)+'"');expect(()=>meta.read(1)).toThrow(MetadataCompatibilityError);
 }));
 it('coalesces capability reports with live freshness, checkpointing and actual outer-rollback invalidation',()=>fixture(async({h,state,seed,tx}:any)=>{
  const x=routingSetup(h,seed,tx),cap={needs:[],max_agents:1,runners:[]};
  expect((await x.service.handle(request('capabilities',cap,'PUT'),x.auth)).status).toBe(200);
  const durable=state.storage.sql.exec("SELECT observed_at FROM routing_machines WHERE machine_id='compat-host'").one().observed_at;
  for(let i=0;i<100;i++)expect((await h.meter.run('unchanged-capabilities',()=>x.service.handle(request('capabilities',cap,'PUT'),x.auth))).status).toBe(200);
  expect(h.meter.snapshot()['unchanged-capabilities'].rows_written).toBe(0);
  x.advance(181000);expect((await x.service.handle(request('capabilities',cap,'PUT'),x.auth)).status).toBe(200);
  expect(effectiveRoutingObservedAt(h.sql,'compat-host',durable)).toBe(new Date(x.now).toISOString());
  expect(()=>tx(()=>{seed.exec("UPDATE routing_machines SET max_agents=2 WHERE machine_id='compat-host'");throw new Error('rollback-capability');})).toThrow('rollback-capability');
  expect(effectiveRoutingObservedAt(h.sql,'compat-host',durable)).toBe(durable);
  x.advance(900000);expect((await x.service.handle(request('capabilities',cap,'PUT'),x.auth)).status).toBe(200);
  expect(state.storage.sql.exec("SELECT observed_at FROM routing_machines WHERE machine_id='compat-host'").one().observed_at).toBe(new Date(x.now).toISOString());
  expect((await x.service.handle(request('capabilities',{...cap,max_agents:2},'PUT'),x.auth)).status).toBe(200);
  expect(state.storage.sql.exec("SELECT max_agents FROM routing_machines WHERE machine_id='compat-host'").one().max_agents).toBe(2);
 }));
 it('coalesces unchanged legacy release reports and requires a fresh ACK after same-SHA goal pins',()=>fixture(async({h,state,seed,tx}:any)=>{
  oldTable(seed,'host_release_targets');const x=routingSetup(h,seed,tx),sha='a'.repeat(40),report={current_sha:sha,state:'current',error:null,autostart_state:'configured',autostart_error:'script_error'};
  seed.exec("UPDATE machines SET capabilities=? WHERE id='compat-host'",JSON.stringify({workspaces:['/repo']}));
  seed.exec("INSERT INTO engine_bundles VALUES('compat-engine','codex','1','revision','{}')");
  const goals=new HostGoals(h.sql,h.fleetService,()=>x.now);
  const goal=goals.set('compat-host',{engines:['compat-engine'],workspace:'/repo',sha});
  expect(goal.release_ready).toBe(false);
  expect((await x.service.handle(request('host-release/report',report),x.auth)).status).toBe(200);
  const launcher={current_sha:sha,state:'current',error:null,root:'/repo'};
  expect((await x.service.handle(request('launcher-release/report',launcher),x.auth)).status).toBe(200);
  for(let i=0;i<20;i++){await h.meter.run('release-noops',()=>x.service.handle(request('host-release/report',report),x.auth));await h.meter.run('release-noops',()=>x.service.handle(request('launcher-release/report',launcher),x.auth));}
  expect(h.meter.snapshot()['release-noops'].rows_written).toBe(0);
  expect(new MetadataCompat(h.sql,'host_release_targets',['autostart_state','autostart_error'],'machine_id').read('compat-host').autostart_error).toBe('script_error');
  expect(goals.view('compat-host')).toMatchObject({release_ready:true,autostart_error:'script_error'});
  x.advance(1000);const retry=goals.set('compat-host',{engines:['compat-engine'],workspace:'/repo',sha,retry:true});
  expect(retry.generation).not.toBe(goal.generation);expect(retry.release_ready).toBe(false);
  expect((await x.service.handle(request('host-release/report',report),x.auth)).status).toBe(200);
  expect(state.storage.sql.exec("SELECT reported_at FROM host_release_targets WHERE machine_id='compat-host'").one().reported_at).toBe(new Date(x.now).toISOString());
  expect(goals.view('compat-host')).toMatchObject({release_ready:true,autostart_error:'script_error'});
 }));
 it('denies post-body capability and release evidence after a dangling or foreign-family rebind',()=>fixture(async({h,state,seed,tx}:any)=>{
  const x=routingSetup(h,seed,tx),at=x.at;
  seed.exec("INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,approval_seq) VALUES('foreign-owner','host','foreign-hash','foreign-family','host','compat-host','machine:compat-host','[\"host\"]','fixture',?,?,7)",at,new Date(x.now+86400000).toISOString());
  seed.exec("INSERT INTO host_release_targets(machine_id,reported_at) VALUES('compat-host',?)",at);
  for(const mutation of ['dangling','foreign-owner','revoked'])for(const [path,data,method] of [['capabilities',{needs:[],max_agents:1,runners:[]},'PUT'],['host-release/report',{current_sha:'a'.repeat(40),state:'current'},'POST'],['launcher-release/report',{current_sha:'a'.repeat(40),state:'current',root:'/repo'},'POST']] as const){
   seed.exec("UPDATE machines SET credential_id='compat-owner' WHERE id='compat-host'");
   seed.exec("UPDATE credentials SET revoked_at=NULL WHERE id='compat-owner'");
   // Warm the exact authority SELECT, then mutate OUTSIDE WriteWatch while
   // request.body is suspended: a memoized permission read must not win.
   expect((await x.service.handle(request('host-release/report',{current_sha:'a'.repeat(40),state:'current'}),x.auth)).status).toBe(200);
   let controller!:ReadableStreamDefaultController<Uint8Array>;const stream=new ReadableStream<Uint8Array>({start(c){controller=c;}});
   const pending=h.meter.run('stale-report-authority',()=>x.service.handle(new Request('https://hub.test/api/routing/'+path,{method,body:stream,duplex:'half'} as any),x.auth));
   if(mutation==='revoked')state.storage.sql.exec("UPDATE credentials SET revoked_at=? WHERE id='compat-owner'",at);
   else state.storage.sql.exec("UPDATE machines SET credential_id=? WHERE id='compat-host'",mutation);
   controller.enqueue(new TextEncoder().encode(JSON.stringify(data)));controller.close();
   expect((await pending).status).toBe(403);
  }
  expect(state.storage.sql.exec("SELECT reported_at FROM host_release_targets WHERE machine_id='compat-host'").one().reported_at).toBe(at);
  expect(state.storage.sql.exec("SELECT 1 FROM routing_machines WHERE machine_id='compat-host'").toArray()).toEqual([]);
  expect(h.meter.snapshot()['stale-report-authority'].rows_written).toBe(0);
 }));
 it('refuses raw routing source overflow before status or host filters',()=>fixture(async({h,seed,tx}:any)=>{
  const x=routingSetup(h,seed,tx);
  for(let i=0;i<257;i++)seed.exec("INSERT INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES(?,?,'ok','fixture',?)",'engine-'+i,'foreign',x.at);
  const response=await h.meter.run('bounded-routing-view',()=>x.service.handle(new Request('https://hub.test/api/routing/machines'),{caller:{isAdmin:true},scopes:['admin']}));
  expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'routing_observation_limited',observation_pending:true});
  expect(h.meter.snapshot()['bounded-routing-view'].rows_read).toBeLessThan(600);expect(h.meter.snapshot()['bounded-routing-view'].rows_written).toBe(0);
 }));
});
