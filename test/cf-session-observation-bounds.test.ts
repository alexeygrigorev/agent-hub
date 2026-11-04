import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {FleetService} from '../src/fleet';
import {SessionControlService} from '../src/session_control';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
import {observationIntakeRevision,observedIntakeCandidates,observedHubTagMatches} from '../src/session-observation';
const fixture=(fn:(x:any)=>Promise<void>|void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('observation-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const setup=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),meter=new RowsMeter(()=>now),sql=meter.wrap(h.sql);
 await fn({h,state,setup,now,at,meter,sql});
});
describe('census observation and first-run repair bounds',()=>{
 it('accepts the first real census without querying a missing projection table or tripping the financial latch',()=>fixture(async({h,state,setup,now,at,sql}:any)=>{
  for(const name of ['session_observation_intake_meta','session_observation_intake_schema','session_observation_intakes'])setup.exec('DROP TABLE IF EXISTS '+name);
  setup.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('host','host','host',?,?)",at,at);
  const fleet=new FleetService(sql,fn=>state.storage.transactionSync(fn),()=>now);
  const response=await fleet.handle(new Request('https://hub/api/fleet/sessions',{method:'PUT',body:JSON.stringify({sessions:[{
   native_id:'11111111-1111-4111-8111-111111111111',tag:'native-test',engine:'codex',workspace:'/repo',phase:'running',agent_state:null,profile:null,account:null,
  }]})}),{caller:{kind:'host',machineId:'host'},scopes:['host']});
  expect(response.status,await response.clone().text()).toBe(200);
  expect(h.rowBudget.status().stopped).toBe(false);
  expect(setup.exec("SELECT id FROM session_control_sessions WHERE id='11111111-1111-4111-8111-111111111111'").toArray()).toHaveLength(1);
  setup.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100) INSERT INTO session_observation_intakes SELECT 'retained-'||i,'[]' FROM n");
  const later=await fleet.handle(new Request('https://hub/api/fleet/sessions',{method:'PUT',body:JSON.stringify({census_complete:false,sessions:[{
   native_id:'22222222-2222-4222-8222-222222222222',tag:'second-native',engine:'codex',workspace:'/repo',phase:'running',agent_state:null,profile:null,account:null,
  }]})}),{caller:{kind:'host',machineId:'host'},scopes:['host']});
  expect(later.status,await later.clone().text()).toBe(200);
  expect(await later.json()).toMatchObject({observation_deferred:1});
  const native=setup.exec("SELECT state,request_json FROM session_control_sessions WHERE id='22222222-2222-4222-8222-222222222222'").one();
  expect(native.state).toBe('running');expect(JSON.parse(native.request_json).observation_link_revision).toBeUndefined();
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('reads a compact candidate prefix once and refuses unique linkage hidden behind retained evidence',()=>fixture(({h,setup,sql,meter}:any)=>{
  observationIntakeRevision(sql,'host');
  setup.exec('UPDATE session_observation_intake_meta SET complete=1 WHERE id=1');
  setup.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100) INSERT INTO session_observation_intakes SELECT 'old-'||i,'[]' FROM n");
  const result=meter.runSync('candidate-prefix',()=>observedIntakeCandidates(sql,'host',[{id:'native',binding_json:JSON.stringify({native_id:'native',tag:'tag',workspace:'/repo'})}]));
  expect(result.size).toBe(0);expect(result.observationLimited).toBe(true);
  expect(meter.snapshot()['candidate-prefix'].rows_read).toBeLessThan(350);
  expect(meter.snapshot()['candidate-prefix'].rows_written).toBe(0);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('bounds legacy aliases before JSON eligibility and refuses an incomplete alias universe',()=>fixture(({setup,at,sql,meter}:any)=>{
  setup.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
   SELECT 'history-'||i,'fixture','history-'||i,'{}','headless','exited',?,? FROM n`,at,at);
  const result=meter.runSync('alias-prefix',()=>observedHubTagMatches(sql,'aaaaaaaa',()=>true));
  expect(result).toHaveLength(0);expect(result.observationLimited).toBe(true);
  expect(meter.snapshot()['alias-prefix'].rows_read).toBeLessThan(350);
 }));
 it('keeps constructors idle and repairs only an authenticated manual raw created-index page',()=>fixture(async({h,state,setup,now,at,sql}:any)=>{
  setup.exec("DELETE FROM hub_init_stamps WHERE key='session_intake_links_t2609u'");
  setup.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
   SELECT 'repair-'||i,'fixture','repair-'||i,'{}','headless','exited',?,? FROM n`,at,at);
  const service=new SessionControlService(sql,fn=>state.storage.transactionSync(fn),()=>now);service.init();
  expect(setup.exec("SELECT * FROM session_intake_link_repair_cursor WHERE key='session_intake_links_t2609u'").toArray()).toEqual([]);
  const request=()=>new Request('https://hub/api/sessions/maintenance/repair-intake-links',{method:'POST'});
  expect((await service.handle(request(),{caller:{kind:'agent',actor:'worker'},scopes:['fleet:launch']})).status).toBe(403);
  expect(setup.exec("SELECT * FROM session_intake_link_repair_cursor WHERE key='session_intake_links_t2609u'").toArray()).toEqual([]);
  const repaired=await service.handle(request(),{caller:{kind:'human',isAdmin:true,actor:'admin'},scopes:['admin']});
  expect(repaired.status).toBe(200);expect(await repaired.json()).toMatchObject({complete:false,source_rows_scanned:16});
  expect(setup.exec("SELECT 1 FROM hub_init_stamps WHERE key='session_intake_links_t2609u'").toArray()).toEqual([]);
  const first=setup.exec("SELECT created_at,session_id FROM session_intake_link_repair_cursor WHERE key='session_intake_links_t2609u'").one();
  expect(first.created_at).toBe(at);expect(first.session_id).toBeTruthy();
  service.repairIntakeLinks();
  const next=setup.exec("SELECT session_id FROM session_intake_link_repair_cursor WHERE key='session_intake_links_t2609u'").one();
  expect(next.session_id>first.session_id).toBe(true);expect(h.rowBudget.status().stopped).toBe(false);
 }));
});
