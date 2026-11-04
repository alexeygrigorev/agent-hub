import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {HostGoals} from '../src/host-goals';
import {RepoRegistry,registeredRepoLabel} from '../src/repos';
import {initSessionLinks,initLinkRefs} from '../src/worker-access';
import {fixtureSql} from './helpers/fixture-budget';
import {deployedUserSchema} from './fixtures/production-schema-t2609u';
const fixture=(fn:(x:any)=>Promise<void>|void,stops=false)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('legacy-compat-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state);
 await fn({h,state,seed});expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:stops});
});
const admin={caller:{kind:'human',isAdmin:true},scopes:['admin']};
const get=()=>new Request('https://hub.test/api/repos');
function legacyRepos(seed:any,count:number){
 seed.exec('DROP TABLE repo_checkouts');seed.exec('DROP TABLE repo_registry');
 seed.exec('CREATE TABLE repo_registry(name TEXT PRIMARY KEY,machine TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL)');
 for(let i=0;i<count;i++){
  const name='legacy-'+String(i).padStart(3,'0'),at='2026-10-26T00:00:00Z';
  const repo={name,machine:'host',dir:'/repos/'+name,origin:'Example/'+name,status:'active',label:'Friendly '+i,aliases:['alias-'+i],purpose:'Keep human purpose '+i,flags:[],updated_at:at,last_commit:{sha:'a'.repeat(40),date:at}};
  seed.exec('INSERT INTO repo_registry VALUES(?,?,?,?)',name,'host',JSON.stringify(repo),at);
 }
}
describe('non-destructive retained schema compatibility',()=>{
 it('keeps the exact strong canonical host guards without DROP and refuses a changed body',()=>fixture(({h,state,seed}:any)=>{
  const goal=new HostGoals(h.sql,h.fleetService);
  h.meter.runSync('host-guards-repeat',()=>{goal.init();goal.init();});
  const cost=h.meter.snapshot()['host-guards-repeat'];expect(cost.rows_read).toBeLessThan(30);expect(cost.rows_written).toBe(0);
  expect(h.meter.statementTop(4096).filter((r:any)=>r.scope==='host-guards-repeat'&&/^DROP /i.test(r.sql))).toEqual([]);
  seed.exec('DROP TRIGGER host_goal_running_update');seed.exec("CREATE TRIGGER host_goal_running_update AFTER UPDATE ON fleet_sessions BEGIN SELECT 'unsafe'; END");
  expect(()=>goal.init()).toThrow('host_goal_trigger_upgrade_required');
  expect(state.storage.sql.exec("SELECT COUNT(*) AS n FROM host_goal_proofs").one().n).toBe(0);
 }));
 it('leaves 145 legacy repos untouched at construction, retains identity, then copies only request-owned pages',()=>fixture(async({h,state,seed}:any)=>{
  legacyRepos(seed,145);const original=state.storage.sql.exec('SELECT name,data FROM repo_registry ORDER BY name').toArray();
  const registry=new RepoRegistry(h.sql,(fn:any)=>state.storage.transactionSync(fn));
  h.meter.runSync('repo-legacy-init',()=>registry.init());
  const init=h.meter.snapshot()['repo-legacy-init'];expect(init.rows_read).toBeLessThan(60);expect(init.rows_written).toBeLessThan(20);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM repo_registry_v2').one().n).toBe(0);
  const legacy=registry.get('legacy-000');expect(legacy).toMatchObject({repo_id:'example/legacy-000',purpose:'Keep human purpose 0'});
  expect(legacy.checkouts[0]).toMatchObject({path:'/repos/legacy-000',updated_at:'2026-10-26T00:00:00Z'});
  expect(registry.resolveLaunchRepo({workspace:'legacy-000'}).input?.repo_context.repo_id).toBe('example/legacy-000');
  expect(registeredRepoLabel(h.sql,'example/legacy-000')).toBe(true);
  let total=0,done=false;
  for(let page=0;page<10;page++){
   const response=await h.meter.run('repo-page-'+page,()=>registry.handle(get(),admin));
   expect([200,503]).toContain(response.status);
   const next=state.storage.sql.exec('SELECT COUNT(*) AS n FROM repo_registry_v2').one().n;expect(next-total).toBeLessThanOrEqual(16);total=next;
   expect(h.meter.snapshot()['repo-page-'+page].rows_read).toBeLessThan(800);
   if(response.status===200){done=true;break;}
   expect(await response.json()).toMatchObject({error:'repo_registry_upgrade_pending',copied:16});
  }
  expect(done).toBe(true);expect(total).toBe(145);
  expect(state.storage.sql.exec('SELECT name,data FROM repo_registry ORDER BY name').toArray()).toEqual(original);
  expect(registry.get('legacy-000')).toMatchObject({repo_id:legacy.repo_id,purpose:legacy.purpose});
  expect(registeredRepoLabel(h.sql,'example/legacy-000')).toBe(true);
 }));
 it('publishes no partial registry on a 257-row source and refuses changed source generations',()=>fixture(async({h,state,seed}:any)=>{
  legacyRepos(seed,257);const registry=new RepoRegistry(h.sql,(fn:any)=>state.storage.transactionSync(fn));registry.init();
  const response=await registry.handle(get(),admin);expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'repo_registry_observation_limited'});
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM repo_registry_v2').one().n).toBe(0);
  expect(()=>registeredRepoLabel(h.sql,'alias-0')).toThrow('repo_registry_observation_limited');
 }));
 it('rejects an externally changed legacy generation after the first committed migration page',()=>fixture(async({h,state,seed}:any)=>{
  legacyRepos(seed,32);const registry=new RepoRegistry(h.sql,(fn:any)=>state.storage.transactionSync(fn));registry.init();
  expect((await registry.handle(get(),admin)).status).toBe(503);
  state.storage.sql.exec("UPDATE repo_registry SET data=json_set(data,'$.purpose','operator changed') WHERE name='legacy-000'");
  const response=await registry.handle(get(),admin);expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'repo_registry_upgrade_source_changed'});
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM repo_registry_v2').one().n).toBe(16);
  expect(()=>registeredRepoLabel(h.sql,'alias-0')).toThrow('repo_registry_upgrade_source_changed');
 }));
 it('refuses raw checkout overflow before parsing malformed JSON',()=>fixture(({h,seed}:any)=>{
  seed.exec("INSERT INTO repo_registry(repo_id,name,data,updated_at) VALUES('local/checkout-test','checkout-test',?,?)",JSON.stringify({name:'checkout-test',repo_id:'local/checkout-test',flags:[]}),new Date().toISOString());
  for(let i=0;i<65;i++)seed.exec("INSERT INTO repo_checkouts(repo_id,host_id,status,dirty_counts,updated_at) VALUES('local/checkout-test',?,'present','{',?)",'host-'+i,new Date().toISOString());
  expect(()=>h.repoRegistry.all()).toThrow('repo_registry_observation_limited');
 }));
 for(const table of ['intake_session_links','intake_link_refs','intake_projection_link_refs'])it('bounds old AFTER DELETE cleanup behind BEFORE proof for '+table,()=>fixture(({h,state,seed}:any)=>{
  for(const name of ['intake_session_links_delete','intake_link_refs_delete','intake_projection_link_refs_delete'])seed.exec(deployedUserSchema.find(row=>row.name===name)!.sql);
  h.meter.runSync('legacy-links-init',()=>{initSessionLinks(h.sql);initLinkRefs(h.sql);});
  expect(h.meter.snapshot()['legacy-links-init'].rows_read).toBeLessThan(60);
  expect(h.meter.statementTop(4096).some((row:any)=>row.scope==='legacy-links-init'&&/^DROP TRIGGER.*(?:intake_session_links_delete|intake_link_refs_delete|intake_projection_link_refs_delete)/.test(row.sql))).toBe(false);
  seed.exec("INSERT INTO intake_tasks(id,data) VALUES('protected',?)",JSON.stringify({id:'protected',project:'demo',title:'protected',status:'open',history:[],links:[]}));
  for(let i=0;i<2100;i++){
   if(table==='intake_session_links')seed.exec('INSERT INTO intake_session_links VALUES(?,?,?)','ref-'+i,'protected','hub_launch_verified');
   else seed.exec(`INSERT INTO ${table} VALUES(?,?)`,'ref-'+i,'protected');
  }
  // Native errors expose no cursor counters. The real guard therefore stops
  // this day rather than inventing zero cost; structural LIMIT129 proof and
  // unchanged rows establish safety independently of missing error counters.
  expect(()=>h.sql.exec("DELETE FROM intake_tasks WHERE id='protected'").toArray()).toThrow();
  expect(state.storage.sql.exec("SELECT id FROM intake_tasks WHERE id='protected'").toArray()).toHaveLength(1);
  expect(state.storage.sql.exec(`SELECT COUNT(*) AS n FROM ${table}`).one().n).toBe(2100);
  expect(h.rowBudget.status().stopped).toBe(true);
 },true));
});
