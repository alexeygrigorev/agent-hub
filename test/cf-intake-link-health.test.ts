import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
import {initIntakeLinkHealth,intakeLinkHealth} from '../src/intake-link-health';
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('link-health-'+crypto.randomUUID())),(h:any,state:any)=>{
 const setup=fixtureSql(h,state),meter=new RowsMeter(),sql=meter.wrap(h.sql);
 fn({h,state,setup,meter,sql});
});
describe('complete bounded intake-link health',()=>{
 it('keeps cold initialization empty and advances32request rows while representing writes behind the cursor',()=>fixture(({setup,meter,sql}:any)=>{
  for(const name of ['intake_link_health_insert','intake_link_health_update','intake_link_health_delete'])setup.exec('DROP TRIGGER IF EXISTS '+name);
  setup.exec('DROP TABLE intake_link_health');setup.exec('DROP TABLE intake_link_health_meta');
  setup.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<130) INSERT INTO intake_task_projection SELECT 'old-'||i,'{}','[]' FROM n");
  initIntakeLinkHealth(sql,true);
  expect(setup.exec('SELECT after_id,complete FROM intake_link_health_meta WHERE id=1').one()).toEqual({after_id:'',complete:0});
  const first=meter.runSync('first',()=>intakeLinkHealth(sql));
  expect(first).toEqual({ready:false,complete:false});expect(meter.snapshot().first.rows_read).toBeLessThan(150);
  setup.exec("INSERT INTO intake_task_projection VALUES('000-before','{}','invalid')");
  let result=first;for(let i=0;i<5&&!result.complete;i++)result=intakeLinkHealth(sql);
  expect(result).toEqual({ready:false,complete:true});
  setup.exec("UPDATE intake_task_projection SET links='[]' WHERE id='000-before'");
  expect(intakeLinkHealth(sql)).toEqual({ready:true,complete:true});
  meter.runSync('steady',()=>{expect(intakeLinkHealth(sql)).toEqual({ready:true,complete:true});});
  expect(meter.snapshot().steady?.rows_written??0).toBe(0);
 }));
 it('treats malformed, non-array, overlong arrays and oversized link payloads as unknown until repaired',()=>fixture(({setup,sql}:any)=>{
  for(const[id,links]of [['bad','invalid'],['object','{}'],['array',JSON.stringify(Array(129).fill({}))],['bytes',JSON.stringify([{pad:'x'.repeat(2100000)}])]])
   setup.exec('INSERT INTO intake_task_projection VALUES(?,?,?)',id,'{}',links);
  expect(intakeLinkHealth(sql)).toEqual({ready:false,complete:true});
  for(const id of ['bad','object','array','bytes'])setup.exec("UPDATE intake_task_projection SET links='[]' WHERE id=?",id);
  expect(intakeLinkHealth(sql)).toEqual({ready:true,complete:true});
 }));
 it('does not retain a validity observation or backfill progress across transaction rollback',()=>fixture(({setup,sql,state}:any)=>{
  setup.exec("INSERT INTO intake_task_projection VALUES('task','{}','[]')");
  expect(intakeLinkHealth(sql)).toEqual({ready:true,complete:true});
  expect(()=>state.storage.transactionSync(()=>{
   sql.exec("UPDATE intake_task_projection SET links='invalid' WHERE id='task'");
   expect(intakeLinkHealth(sql)).toEqual({ready:false,complete:true});
   throw new Error('rollback-health');
  })).toThrow('rollback-health');
  expect(intakeLinkHealth(sql)).toEqual({ready:true,complete:true});
 }));
});
