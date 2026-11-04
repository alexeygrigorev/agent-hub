import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowBudget,RowBudgetExceeded,INDEX_BUILD_SOURCE_LIMIT} from '../src/row-budget';
import {fixtureSql} from './helpers/fixture-budget';

const fixture=(fn:(x:any)=>Promise<void>|void)=>(runInDurableObject as any)((env as any).HUB.get(
 (env as any).HUB.idFromName('index-facts-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state),calls:{query:string;cursor:any}[]=[];
 const native=new Proxy(state.storage.sql,{get(target,key){if(key==='exec')return(query:string,...bindings:any[])=>{
  const cursor=target.exec(query,...bindings);calls.push({query,cursor});return cursor;};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 await fn({h,state,seed,calls,sql:h.rowBudget.wrap(native),day});
 expect(h.rowBudget.status().limit_total).toBe(20000);expect(h.rowBudget.status().day).toBe(day);
});
function source(seed:any,name:string,count=3){
 seed.exec('CREATE TABLE '+name+'(id INTEGER PRIMARY KEY,value INTEGER)');
 seed.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
  INSERT INTO ${name} SELECT i,i FROM n`,count);
}
const metadata=(calls:any[])=>calls.filter(row=>row.query.includes('pragma_index_list'));
const exactMetadata=(calls:any[])=>calls.filter(row=>row.query.includes('pragma_index_info'));

describe('bounded index metadata facts',()=>{
 it('charges bounded live exact-name observations for existing indexes without scanning index lists',()=>fixture(({h,seed,calls,sql}:any)=>{
  source(seed,'fact_existing',2100);
  for(let i=0;i<24;i++)seed.exec('CREATE INDEX fact_index_'+i+' ON fact_existing(value)');
  calls.length=0;const before=h.rowBudget.status();
  for(let i=0;i<40;i++)sql.exec('CREATE INDEX IF NOT EXISTS fact_index_'+i%24+' ON fact_existing(value)').toArray();
  expect(metadata(calls)).toHaveLength(0);expect(exactMetadata(calls)).toHaveLength(40);expect(calls.some((row:any)=>/^SELECT 1 FROM/.test(row.query))).toBe(false);
  const after=h.rowBudget.status(),charged=(after.reserved_total-after.available_total)-(before.reserved_total-before.available_total);
  console.log(JSON.stringify({evidence:'index_metadata_fact_cost',existing_indexes:24,repeated_noop_builds:40,
   metadata_calls:exactMetadata(calls).length,guard_consumed_total:charged,metadata_rows_read:exactMetadata(calls).reduce((sum:number,row:any)=>sum+Number(row.cursor.rowsRead),0)}));
  expect(charged).toBeLessThan(100);expect(h.rowBudget.status().stopped).toBe(false);
  sql.exec('CREATE TRIGGER fact_noop AFTER INSERT ON fact_existing BEGIN SELECT 1; END').toArray();
  sql.exec('CREATE INDEX IF NOT EXISTS fact_index_0 ON fact_existing(value)').toArray();
  expect(metadata(calls)).toHaveLength(0);expect(exactMetadata(calls)).toHaveLength(41);
 }));
 it('records successful builds and conservatively clears facts at every native transaction boundary',()=>fixture(({h,state,seed,calls,sql}:any)=>{
  source(seed,'fact_small');calls.length=0;
  sql.exec('CREATE INDEX fact_built ON fact_small(value)').toArray();
  for(let i=0;i<10;i++)sql.exec('CREATE INDEX IF NOT EXISTS fact_built ON fact_small(value)').toArray();
  expect(metadata(calls)).toHaveLength(1);
  state.storage.transactionSync(()=>42);
  sql.exec('CREATE INDEX IF NOT EXISTS fact_built ON fact_small(value)').toArray();
  expect(metadata(calls)).toHaveLength(1);expect(exactMetadata(calls)).toHaveLength(2);expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('cannot use a rolled-back index name to bypass a later full-source build guard',()=>fixture(({h,state,seed,calls,sql}:any)=>{
  source(seed,'fact_rollback',INDEX_BUILD_SOURCE_LIMIT+5);calls.length=0;
  expect(()=>state.storage.transactionSync(()=>{
   sql.exec('CREATE INDEX fact_rolled_back ON fact_rollback(value) WHERE value<0').toArray();
   sql.exec('CREATE INDEX IF NOT EXISTS fact_rolled_back ON fact_rollback(value) WHERE value<0').toArray();
   throw new Error('rollback-index-fact');
  })).toThrow('rollback-index-fact');
  expect(h.rowBudget.status().stopped).toBe(false);
  const before=calls.length;
  expect(()=>sql.exec('CREATE INDEX IF NOT EXISTS fact_rolled_back ON fact_rollback(value)').toArray()).toThrow(RowBudgetExceeded);
  expect(calls.slice(before).some((row:any)=>/CREATE INDEX/.test(row.query))).toBe(false);
  expect(calls.slice(before).some((row:any)=>row.query.includes('pragma_index_list'))).toBe(true);
  expect(h.rowBudget.status().stopped).toBe(true);
 }));
 it.each(['guarded','fixture'])('invalidates a dropped index from %s DDL before another large build',kind=>fixture(({h,seed,calls,sql}:any)=>{
  source(seed,'fact_drop',INDEX_BUILD_SOURCE_LIMIT+5);seed.exec('CREATE INDEX fact_deleted ON fact_drop(value)');
  sql.exec('CREATE INDEX IF NOT EXISTS fact_deleted ON fact_drop(value)').toArray();
  if(kind==='guarded')sql.exec('DROP INDEX fact_deleted').toArray();
  else seed.exec('DROP INDEX fact_deleted'); // Explicit fixture invalidation hook.
  const before=calls.length;
  expect(()=>sql.exec('CREATE INDEX IF NOT EXISTS fact_deleted ON fact_drop(value)').toArray()).toThrow(RowBudgetExceeded);
  expect(calls.slice(before).some((row:any)=>/CREATE INDEX/.test(row.query))).toBe(false);
  expect(h.rowBudget.status().stopped).toBe(true);
 }));
 it('expires facts before a new event observes external native schema changes',()=>fixture(async({h,state,seed,calls,sql}:any)=>{
  source(seed,'fact_event',INDEX_BUILD_SOURCE_LIMIT+5);seed.exec('CREATE INDEX fact_event_index ON fact_event(value)');
  sql.exec('CREATE INDEX IF NOT EXISTS fact_event_index ON fact_event(value)').toArray();
  // Models out-of-band operator DDL, not an application permission bypass.
  state.storage.sql.exec('DROP INDEX fact_event_index');
  await h.rowBudget.begin();const before=calls.length;
  expect(()=>sql.exec('CREATE INDEX IF NOT EXISTS fact_event_index ON fact_event(value)').toArray()).toThrow(RowBudgetExceeded);
  expect(calls.slice(before).some((row:any)=>/CREATE INDEX/.test(row.query))).toBe(false);
  expect(h.rowBudget.status().stopped).toBe(true);
 }));
 it('leaves standalone wrappers uncached when their raw storage transaction owner is unknown',()=>fixture(async({state,seed}:any)=>{
  source(seed,'fact_standalone',INDEX_BUILD_SOURCE_LIMIT+5);seed.exec('CREATE INDEX fact_external ON fact_standalone(value)');
  const store={async get(){return undefined;},async put(){}};
  const budget=new RowBudget(store);budget.guardStorage(store);await budget.begin(true);const sql=budget.wrap(state.storage.sql);
  sql.exec('CREATE INDEX IF NOT EXISTS fact_external ON fact_standalone(value)').toArray();
  state.storage.sql.exec('DROP INDEX fact_external');
  expect(()=>sql.exec('CREATE INDEX IF NOT EXISTS fact_external ON fact_standalone(value)').toArray()).toThrow(RowBudgetExceeded);
  expect(budget.status().stopped).toBe(true);
 }));
});
