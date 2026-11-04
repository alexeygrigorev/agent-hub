import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {initIntakeSummary,intakeSummaryReady} from '../src/intake-summary';
import {configureSchemaObjectHints} from '../src/schema-object-locator';
import {fixtureSql} from './helpers/fixture-budget';

const names=['intake_status_summary_insert','intake_status_summary_update','intake_status_summary_delete'];
const fixture=(mode:'missing'|'legacy'|'invalid',fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('summary-proof-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),at=new Date().toISOString();
 if(mode==='missing'){
  for(const name of names)seed.exec('DROP TRIGGER '+name);
  seed.exec('DROP TABLE intake_status_summary');
 }
 for(let i=0;i<2100;i++){
  const id='summary-source-'+i;
  seed.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',id,JSON.stringify({id,project:'demo',title:'retained',status:'done',created_at:at,updated_at:at,version:1,links:[],history:[]}));
 }
 seed.exec('DROP TABLE intake_summary_migration');
 if(mode==='invalid'){
  seed.exec('DROP TRIGGER intake_status_summary_update');
  seed.exec('CREATE TRIGGER intake_status_summary_update AFTER UPDATE ON intake_tasks BEGIN SELECT 1; END');
 }
 const queries:string[]=[],sql=new Proxy(h.sql,{get(target,key){if(key==='exec')return(q:string,...args:any[])=>{queries.push(q);return target.exec(q,...args);};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
 // Synthetic metadata discovery provides UNTRUSTED locators only, before the
 // measured service call. Every hinted identity/definition is verified live.
 if(mode!=='missing'){
  const rows=state.storage.sql.exec("SELECT rowid,name,type,tbl_name FROM sqlite_master WHERE name IN ('intake_status_summary_insert','intake_status_summary_update','intake_status_summary_delete')").toArray();
  expect(rows).toHaveLength(3);configureSchemaObjectHints(sql,rows);
 }
 h.meter.runSync('summary-proof',()=>initIntakeSummary(sql));
 fn({h,state,sql,queries});
 const cost=h.meter.snapshot()['summary-proof'];
 console.log(JSON.stringify({evidence:'bounded_intake_summary_bootstrap',mode,native_sql:cost,ledger:h.rowBudget.status()}));
 expect(cost.rows_read).toBeLessThan(50);expect(cost.rows_written).toBeLessThan(20);
 expect(queries.some(q=>/FROM sqlite_master/i.test(q)&&!q.includes('WHERE rowid=?'))).toBe(false);
 expect(queries.some(q=>/^UPDATE intake_tasks|^DELETE FROM intake_status_summary|^INSERT INTO intake_status_summary/i.test(q))).toBe(false);
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});

describe('summary bootstrap never scans retained source or global schema to infer readiness',()=>{
 it('leaves a new projection pending beside 2100 retained tasks without catalog proof or backfill',()=>fixture('missing',({state,sql,queries}:any)=>{
  expect(intakeSummaryReady(sql)).toBe(false);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').one().n).toBe(2100);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM intake_status_summary').one().n).toBe(0);
  expect(queries.some((q:string)=>q.includes('sqlite_master'))).toBe(false);
 }));
 it('inherits an existing atomic legacy boundary only through three live exact definitions',()=>fixture('legacy',({state,sql,queries}:any)=>{
  expect(intakeSummaryReady(sql)).toBe(true);
  expect(state.storage.sql.exec('SELECT COUNT(*) AS n FROM intake_status_summary').one().n).toBe(2100);
  expect(queries.filter((q:string)=>q.includes('FROM sqlite_master')&&q.includes('WHERE rowid=?'))).toHaveLength(3);
 }));
 it('refuses the inherited boundary when a correctly named trigger has the wrong live body',()=>fixture('invalid',({state,sql,queries}:any)=>{
  expect(intakeSummaryReady(sql)).toBe(false);
  expect(state.storage.sql.exec('SELECT phase FROM intake_summary_migration WHERE id=1').one().phase).toBe(0);
  expect(queries.filter((q:string)=>q.includes('FROM sqlite_master')&&q.includes('WHERE rowid=?'))).toHaveLength(2);
 }));
});
