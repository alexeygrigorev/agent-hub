import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
import {boundedRecentHoleInput,boundedBatchSubjects,batchCompleteStillOpen,boundedReconcilePage} from '../src/ask-bounded-observation';
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('ask-maintenance-'+crypto.randomUUID())),(hub:any,state:any)=>{
 const setup=fixtureSql(hub,state),now=Date.now(),meter=new RowsMeter(()=>now),sql=meter.wrap(hub.sql);
 const messages=(n:number,extra:any={})=>setup.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
  INSERT INTO ask_messages(id,agent_id,t_id,seq,role,text_hash,source,received_at,split_state)
  SELECT 'msg-'||i,'agent','t'||i,i,?,'hash','live',?,'split' FROM n`,n,extra.role??'user',extra.at??now);
 const asks=(n:number,extra:any={})=>setup.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
  INSERT INTO asks(id,message_id,part,text,state,created_at,updated_at,alert_subject)
  SELECT 'ask-'||i,?,i,'synthetic',?,0,0,? FROM n`,n,extra.message??'msg-1',extra.state??'verified',extra.subject??'resolved:old');
 fn({setup,now,meter,sql,messages,asks});
});
describe('bounded manual Ask maintenance inputs',()=>{
 it('does not gate small active observations on1500closed old records',()=>fixture(({messages,asks,now,meter,sql}:any)=>{
  messages(1500,{at:now-30*86400000});asks(1500);
  const observed=meter.runSync('old-history',()=>({holes:boundedRecentHoleInput(sql,now-14*86400000),subjects:boundedBatchSubjects(sql,'asks_unmapped')}));
  expect(observed).toEqual({holes:{rows:[],limited:false},subjects:{subjects:[],limited:false}});
  expect(meter.snapshot()['old-history'].rows_read).toBeLessThan(40);
 }));
 it('never invents holes from a partial recent message cohort',()=>fixture(({messages,now,meter,sql}:any)=>{
  messages(2100);
  expect(meter.runSync('recent-overflow',()=>boundedRecentHoleInput(sql,now-14*86400000))).toEqual({rows:[],limited:true});
  expect(meter.snapshot()['recent-overflow'].rows_read).toBeLessThan(300);
 }));
 it('bounds duplicate subjects before distinct and never resolves an unknown batch',()=>fixture(({messages,asks,setup,meter,sql}:any)=>{
  messages(1);asks(2100,{subject:'asks_unmapped-123'});
  expect(meter.runSync('subjects',()=>boundedBatchSubjects(sql,'asks_unmapped'))).toEqual({subjects:['asks_unmapped-123'],limited:true});
  expect(meter.snapshot().subjects.rows_read).toBeLessThan(300);
  expect(meter.runSync('batch',()=>batchCompleteStillOpen(sql,'asks_unmapped','asks_unmapped-123'))).toEqual({stillOpen:null,limited:true});
  expect(meter.snapshot().batch.rows_read).toBeLessThan(300);
  setup.exec("UPDATE asks SET state='unmapped' WHERE id='ask-1'");
  expect(batchCompleteStillOpen(sql,'asks_unmapped','asks_unmapped-123')).toEqual({stillOpen:true,limited:true});
 }));
 it('advances an empty raw page before role filters and reaches later eligible messages',()=>fixture(({messages,setup,meter,sql}:any)=>{
  messages(2100,{role:'assistant'});setup.exec("UPDATE ask_messages SET role='user' WHERE id='msg-130'");
  const first=meter.runSync('first',()=>boundedReconcilePage(sql,0,false));
  expect(first).toMatchObject({messages:[],asks:[],toCursor:128,sourceRowsScanned:128,observationPending:true,limited:false});
  expect(meter.snapshot().first.rows_read).toBeLessThan(300);
  const next=meter.runSync('next',()=>boundedReconcilePage(sql,first.toCursor,false));
  expect(next.messages.map((row:any)=>row.id)).toEqual(['msg-130']);expect(next.toCursor).toBe(256);expect(next.limited).toBe(false);
  expect(meter.snapshot().next.rows_read).toBeLessThan(350);
 }));
 it('refuses an oversized raw part cohort before filtering and leaves the original cursor unadvanced',()=>fixture(({messages,asks,meter,sql}:any)=>{
  messages(1);asks(2100,{state:'not_an_ask'});
  const page=meter.runSync('parts',()=>boundedReconcilePage(sql,0,false));
  expect(page).toMatchObject({messages:[],asks:[],toCursor:0,limited:true,observationPending:true});
  expect(meter.snapshot().parts.rows_read).toBeLessThan(350);
 }));
});
