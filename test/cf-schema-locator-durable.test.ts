import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {WriteWatch} from '../src/write-watch';
import {configureSchemaObjectHints,readSchemaObject} from '../src/schema-object-locator';
import {fixtureSql} from './helpers/fixture-budget';
import {deployedUserSchema} from './fixtures/production-schema-t2609u';
const fixture=(fn:(h:any,state:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('durable-schema-'+crypto.randomUUID())),fn);
const consumed=(h:any)=>h.rowBudget.status().reserved_total-h.rowBudget.status().available_total;
describe('durable schema locators survive eviction without definition authority',()=>{
 it('performs two complete real service-graph restarts on one strict day with the stale original message hint',()=>fixture(async(h,state)=>{
  const seed=fixtureSql(h,state);h.cancelBudgetRefund();
  const old=deployedUserSchema.find(row=>row.name==='messages_bind_agents')!;
  seed.exec('DROP TRIGGER messages_bind_agents');seed.exec(old.sql);
  // Keep a later schema row so replacing OLD cannot reuse its rowid.
  seed.exec('CREATE TABLE locator_eviction_sentinel(id INTEGER PRIMARY KEY)');
  seed.exec('DELETE FROM schema_object_locators');
  const names=['messages_bind_agents','agents_no_legacy_collision_insert','agents_no_legacy_collision_update','fleet_launch_runtime_exit',
   'resource_catalog_fleet_sessions_update','host_goal_running_insert','host_goal_running_update'];
  const hints=Object.fromEntries(names.map(name=>[name,seed.exec('SELECT rowid FROM sqlite_master WHERE name=?',name).one().rowid]));
  await h.rowBudget.begin(true);const day=h.rowBudget.status().day,before=consumed(h);
  const restart=async()=>{
   h.writes=new WriteWatch();h.meter=new RowsMeter();
   h.sql=h.meter.wrap(h.writes.wrap(h.rowBudget.wrap(state.storage.sql)));
   h.observedCatalog=new h.observedCatalog.constructor(h.sql,h.writes);
   h.env={...env,SCHEMA_OBJECT_ROWID_HINTS:JSON.stringify(hints)};
   h.initializeServices(h.env);h.backgroundRootStamp=null;h.backgroundAlarmStamp=null;
   await h.budgeted(async()=>undefined,undefined);
   h.cancelBudgetRefund();
   return h.meter.statementTop(4096);
  };
  const first=await restart();
  expect(first.some((row:any)=>/^DROP TRIGGER messages_bind_agents$/i.test(row.sql))).toBe(true);
  const actual=seed.exec("SELECT rowid FROM sqlite_master WHERE name='messages_bind_agents'").one().rowid;
  expect(actual).not.toBe(hints.messages_bind_agents);
  expect(seed.exec("SELECT object_rowid FROM schema_object_locators WHERE object_type='trigger' AND table_name='messages' AND name='messages_bind_agents'").one().object_rowid).toBe(actual);
  const second=await restart();
  expect(second.some((row:any)=>row.sql.includes('ORDER BY rowid LIMIT 1025'))).toBe(false);
  expect(second.some((row:any)=>/^DROP TRIGGER messages_bind_agents$/i.test(row.sql))).toBe(false);
  expect(second.some((row:any)=>/^(?:INSERT INTO|UPDATE) schema_object_locators/i.test(row.sql))).toBe(false);
  expect(h.conversationsService).toBeDefined();expect(h.jobsService).toBeDefined();
  expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  console.log(JSON.stringify({evidence:'durable_schema_locator_two_restarts',strict_day:day,combined_operations:consumed(h)-before,
   stale_original_hint:Number(hints.messages_bind_agents),current_message_rowid:Number(actual),second_schema_catalog_scans:0,second_locator_writes:0}));
 }));
 it('reconfigures after table-creation rollback and rejects a wrong persisted identity before fallback',()=>fixture(async(h,state)=>{
  const seed=fixtureSql(h,state);seed.exec('DROP TABLE schema_object_locators');
  await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
  const sql=h.meter.wrap(h.writes.wrap(h.rowBudget.wrap(state.storage.sql)));
  expect(()=>state.storage.transactionSync(()=>{configureSchemaObjectHints(sql,{});throw new Error('locator-rollback');})).toThrow('locator-rollback');
  expect(seed.exec("SELECT name FROM sqlite_master WHERE name='schema_object_locators'").toArray()).toHaveLength(0);
  configureSchemaObjectHints(sql,{});
  const identity={name:'fleet_launch_runtime_exit',type:'index' as const,table:'fleet_launches'};
  const wrong=seed.exec("SELECT rowid FROM sqlite_master WHERE name='fleet_sessions' AND type='table'").one().rowid;
  seed.exec('INSERT INTO schema_object_locators VALUES(?,?,?,?)',identity.type,identity.table,identity.name,wrong);
  const actual=seed.exec("SELECT rowid,sql FROM sqlite_master WHERE name='fleet_launch_runtime_exit'").one();
  expect(readSchemaObject(sql,identity)).toEqual(actual);
  expect(seed.exec('SELECT object_rowid FROM schema_object_locators WHERE object_type=? AND table_name=? AND name=?',identity.type,identity.table,identity.name).one().object_rowid).toBe(actual.rowid);
  expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
 }));
});
