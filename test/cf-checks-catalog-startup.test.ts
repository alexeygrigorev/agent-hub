import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {ChecksService,ChecksObservationLimit} from '../src/checks';
import {SessionControlService,FreshCatalogObservationLimit,reconcileStaleSessions} from '../src/session_control';
import {fixtureSql} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('checks-catalog-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state);let clock=Date.now();
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 await fn({h,state,seed,get now(){return clock;},advance(ms:number){clock+=ms;}});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
const admin={caller:{kind:'human',actor:'human:fixture',isAdmin:true},scopes:['admin']};
function checksSetup(h:any,seed:any,now:number){
 seed.exec('DROP INDEX IF EXISTS hub_checks_enabled_due');seed.exec('DROP INDEX IF EXISTS hub_check_slots_retry_due');
 seed.exec('DELETE FROM hub_check_slots');seed.exec('DELETE FROM hub_checks');
 const check=(i:number,params='{"min_percent":20}')=>seed.exec(`INSERT INTO hub_checks(name,kind,interval_s,params_json,action_json,enabled,next_due_at)
  VALUES(?,'quota',300,?,'{"type":"alert","on":"fail"}',1,?)`,'fixture-check-'+i,params,new Date(now+(i<2?-3000+i*1000:86400000)).toISOString());
 for(let i=0;i<4;i++){
  check(i);seed.exec(`INSERT INTO hub_check_slots(name,slot_key,next_at,check_json,state,token,lease_until)
   VALUES(?,?,0,?,?,'fixture',0)`,'fixture-check-'+i,'slot-'+i,JSON.stringify({name:'fixture-check-'+i,kind:'quota',interval_s:300,params:{min_percent:20},action:{type:'alert',on:'fail'}}),i===3?'evaluating':'done');
 }
 const service=new ChecksService(h.sql,()=>now),evaluated:string[]=[];const original=(service as any).evaluate.bind(service);
 (service as any).evaluate=(definition:any,deps:any)=>{evaluated.push(definition.name);return original(definition,deps);};
 const deps={liveness:h.livenessService,notify:async()=>{},message:async()=>'',launch:async()=>'',fetch:async()=>new Response('unused'),evaluateQuota:()=>[]};
 return {service,evaluated,deps,check};
}
describe('retained checks and catalog need no constructor history indexes',()=>{
 it('keeps4check/4slot sources unindexed at init and preserves retry/due order without warm no-op writes',()=>fixture(async({h,seed,now}:any)=>{
  const x=checksSetup(h,seed,now);
  h.meter.runSync('checks-retained-init',()=>x.service.init());
  expect(h.meter.statementTop(1500).some((row:any)=>row.scope==='checks-retained-init'&&/CREATE INDEX/.test(row.sql)&&/hub_checks_enabled_due|hub_check_slots_retry_due/.test(row.sql))).toBe(false);
  expect(x.service.list()).toHaveLength(4);
  const results=await h.meter.run('checks-known',()=>x.service.runDue(x.deps));
  expect(results.map((row:any)=>row.name)).toEqual(['fixture-check-3','fixture-check-0','fixture-check-1']);
  expect(x.evaluated).toEqual(['fixture-check-3','fixture-check-0','fixture-check-1']);
  expect(await h.meter.run('checks-idle',()=>x.service.runDue(x.deps))).toEqual([]);
  expect(h.meter.snapshot()['checks-idle'].rows_written).toBe(0);
  expect(seed.exec("SELECT name FROM sqlite_master WHERE name IN ('hub_checks_enabled_due','hub_check_slots_retry_due')").toArray()).toEqual([]);
 }));
 for(const source of ['checks','slots'])it('refuses101raw '+source+' before JSON, evaluator or slot/audit effects',()=>fixture(async({h,seed,now}:any)=>{
  const x=checksSetup(h,seed,now);x.service.init();
  if(source==='checks')for(let i=4;i<101;i++)x.check(i,'{');
  else for(let i=4;i<101;i++)seed.exec("INSERT INTO hub_check_slots(name,slot_key,next_at,check_json,state,token,lease_until) VALUES(?,?,0,'{}','done','fixture',0)",'noise-'+i,'noise-slot-'+i);
  await expect(h.meter.run('checks-overflow',()=>x.service.runDue(x.deps))).rejects.toThrow(ChecksObservationLimit);
  expect(x.evaluated).toEqual([]);expect(h.meter.snapshot()['checks-overflow'].rows_written).toBe(0);
  expect(h.meter.snapshot()['checks-overflow'].rows_read).toBeLessThan(220);
  if(source==='checks')expect(()=>x.service.list()).toThrow(ChecksObservationLimit);
  // Explicit operator-owned work is a known PK, not a global census.
  expect(x.service.has('fixture-check-0')).toBe(true);
  expect((await x.service.runDue(x.deps,'fixture-check-0')).map((row:any)=>row.name)).toEqual(['fixture-check-0']);
 }));
 it('forces existing scheduling indexes before filtering1000ineligible retained rows',()=>fixture(async({h,seed,now}:any)=>{
  const x=checksSetup(h,seed,now);seed.exec('UPDATE hub_checks SET next_due_at=?',new Date(now+86400000).toISOString());seed.exec("UPDATE hub_check_slots SET state='done'");
  for(let i=4;i<1004;i++){
   x.check(i,'{');seed.exec("UPDATE hub_checks SET enabled=0 WHERE name=?",'fixture-check-'+i);
   seed.exec("INSERT INTO hub_check_slots(name,slot_key,next_at,check_json,state,token,lease_until) VALUES(?,?,0,'{}','done','fixture',0)",'noise-'+i,'slot-'+i);
  }
  seed.exec('CREATE INDEX hub_checks_enabled_due ON hub_checks(enabled,next_due_at,name)');
  seed.exec("CREATE INDEX hub_check_slots_retry_due ON hub_check_slots(lease_until,name) WHERE state!='done'");
  expect(await h.meter.run('checks-existing-indexes',()=>x.service.runDue(x.deps))).toEqual([]);
  expect(h.meter.snapshot()['checks-existing-indexes'].rows_read).toBeLessThan(20);expect(h.meter.snapshot()['checks-existing-indexes'].rows_written).toBe(0);
  const statements=h.meter.statementTop(1500).filter((row:any)=>row.scope==='checks-existing-indexes');
  expect(statements.some((row:any)=>row.sql.includes('INDEXED BY hub_checks_enabled_due'))).toBe(true);
  expect(statements.some((row:any)=>row.sql.includes('INDEXED BY hub_check_slots_retry_due'))).toBe(true);
 }));
});
function catalogSetup(h:any,state:any,seed:any,clock:()=>number){
 seed.exec('DROP INDEX IF EXISTS fleet_catalog_seen_fresh');seed.exec('DELETE FROM fleet_catalog_seen');
 const now=clock(),at=new Date(now-2*3600000).toISOString();
 for(let i=0;i<5;i++)seed.exec('INSERT INTO fleet_catalog_seen VALUES(?,?)','host-'+i,new Date(now).toISOString());
 seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('host-0','native','native','codex','running',?,'/repo')`,at);
 seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,engine,machine_id,binding_json,created_at,updated_at)
  VALUES('catalog-session','human:fixture','catalog-session','{}','headless','running','codex','host-0','{"native_id":"native"}',?,?)`,at,at);
 return new SessionControlService(h.sql,f=>state.storage.transactionSync(f),clock);
}
describe('active-session catalog observations are complete and clock-live',()=>{
 it('counts a fresh5host catalog then expires the same memoized facts with a later clock',()=>fixture(async(x:any)=>{
  const {h,state,seed}=x,service=catalogSetup(h,state,seed,()=>x.now);
  const get=()=>service.handle(new Request('https://hub/api/sessions?active=1'),admin);
  const initial=await h.meter.run('catalog-fresh',get);expect(initial.status).toBe(200);expect(await initial.json()).toMatchObject({active_count:1});
  x.advance(11*60000);
  const expired=await h.meter.run('catalog-expired',get);expect(expired.status).toBe(200);expect(await expired.json()).toMatchObject({active_count:0,sessions:[]});
  expect(seed.exec("SELECT name FROM sqlite_master WHERE name='fleet_catalog_seen_fresh'").toArray()).toEqual([]);
  expect(h.meter.statementTop(1500).some((row:any)=>row.scope.startsWith('catalog-')&&/group_concat/i.test(row.sql))).toBe(false);
 }));
 it('refuses1001stale running candidates before an unbounded search for fresh matches',()=>fixture(async(x:any)=>{
  const {h,state,seed}=x,service=catalogSetup(h,state,seed,()=>x.now),at=new Date(x.now-2*3600000).toISOString();
  for(let i=0;i<1000;i++)seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,machine_id,binding_json,created_at,updated_at)
    VALUES(?,'human:fixture',?,'{}','headless','running','host-0','{"native_id":"missing"}',?,?)`,'stale-'+i,'stale-'+i,at,at);
  const response=await h.meter.run('active-prefix-overflow',()=>service.handle(new Request('https://hub/api/sessions?active=1'),admin));
  expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'active_session_observation_limited',observation_pending:true});
  expect(h.meter.snapshot()['active-prefix-overflow'].rows_read).toBeLessThan(2100);expect(h.meter.snapshot()['active-prefix-overflow'].rows_written).toBe(0);
  expect(()=>reconcileStaleSessions(h.sql,x.now)).toThrow(FreshCatalogObservationLimit);
  expect(seed.exec("SELECT 1 FROM session_control_sessions WHERE state='stopped' LIMIT 1").toArray()).toEqual([]);
 }));
 for(const invalid of ['overflow','delimiter'])it('refuses '+invalid+' catalog authority before declaring or reconciling inactivity',()=>fixture(async(x:any)=>{
  const {h,state,seed}=x,service=catalogSetup(h,state,seed,()=>x.now);
  const at=new Date(x.now-2*3600000).toISOString();
  seed.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
    VALUES('maintenance-launch','human:fixture','human','fixture','maintenance-launch','{}','host-0','codex','fixture','headless','/repo','','starting','receipt-native',?,?)`,at,at);
  seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,machine_id,fleet_launch_id,created_at,updated_at)
    VALUES('maintenance-offered','human:fixture','maintenance-offered','{}','headless','offered','host-0','maintenance-launch',?,?)`,at,at);
  if(invalid==='overflow')for(let i=5;i<65;i++)seed.exec('INSERT INTO fleet_catalog_seen VALUES(?,?)','host-'+i,new Date(x.now).toISOString());
  else seed.exec('INSERT INTO fleet_catalog_seen VALUES(?,?)','host|other',new Date(x.now).toISOString());
  const response=await h.meter.run('catalog-unknown',()=>service.handle(new Request('https://hub/api/sessions?active=1'),admin));
  expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'fresh_catalog_observation_limited',observation_pending:true});
  expect(h.meter.snapshot()['catalog-unknown'].rows_written).toBe(0);expect(h.meter.snapshot()['catalog-unknown'].rows_read).toBeLessThanOrEqual(65);
  expect(()=>reconcileStaleSessions(h.sql,x.now)).toThrow(FreshCatalogObservationLimit);
  expect(seed.exec("SELECT state FROM session_control_sessions WHERE id='catalog-session'").one().state).toBe('running');
  expect(seed.exec("SELECT state FROM session_control_sessions WHERE id='maintenance-offered'").one().state).toBe('offered');
  // Positive control: with complete catalog authority, the same real GET
  // performs the pre-existing receipt promotion under the same daily guard.
  if(invalid==='overflow')for(let i=5;i<65;i++)seed.exec('DELETE FROM fleet_catalog_seen WHERE machine_id=?','host-'+i);
  else seed.exec("DELETE FROM fleet_catalog_seen WHERE machine_id='host|other'");
  expect((await service.handle(new Request('https://hub/api/sessions?limit=1'),admin)).status).toBe(200);
  expect(seed.exec("SELECT state FROM session_control_sessions WHERE id='maintenance-offered'").one().state).toBe('starting');
 }));
});
