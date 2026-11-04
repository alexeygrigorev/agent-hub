import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { AskLedger } from '../src/asks';
const fixture=(fn:(h:any,raw:any,state:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('asks-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const raw=state.storage.sql;
 raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
  INSERT INTO ask_messages(id,agent_id,t_id,seq,ts,text,text_hash,source,received_at,split_state,conflict_count)
  SELECT 'msg-'||i,'bulk','t'||i,i,2001-i,'retained','hash','fixture',i,'split',1 FROM n`);
 raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
  INSERT INTO asks(id,message_id,part,text,state,created_at,updated_at)
  SELECT 'ask-'||i,'msg-'||i,0,'retained','verified',i,i FROM n`);
 raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
  INSERT INTO ask_gaps(scope,from_seq,to_seq,kind,detected_at) SELECT 'fixture',i,i,'missing',i FROM n`);
 await fn(h,raw,state);
});
function service(h:any,state:any){const calls:{query:string;args:any[];cursor:any}[]=[];const sql=new Proxy(h.sql,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,args,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}});return{calls,ledger:new AskLedger(sql,h.intakeService,fn=>state.storage.transactionSync(fn))};}
const reads=(calls:{cursor:any}[])=>calls.reduce((n,c)=>n+c.cursor.rowsRead,0);
const admin={admin:true,actor:'human:fixture'};
describe('ask status, digest and history observations are bounded',()=>{
 it('does not aggregate2000open gaps/conflicts and preserves positive lower bounds in the digest',()=>fixture(async(h,_raw,state)=>{
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const counts=ledger.counts();
  expect(counts).toMatchObject({messages:2000,verified:2000,open_gaps:null,conflicts:null,open_gaps_lower_bound:129,conflicts_lower_bound:129,counts_observation_limited:true});
  expect(reads(calls)).toBeLessThanOrEqual(300);expect(calls.some(c=>/COUNT\s*\(/i.test(c.query))).toBe(false);
  for(const name of ['ask_gaps_open','ask_messages_conflicts'])expect(calls.some(c=>c.query.includes('INDEXED BY '+name))).toBe(true);
  expect(ledger.summaryLine()).toContain('at least 129 (count limited) unrecorded range(s)');
  expect(ledger.summaryLine()).toContain('at least 129 (count limited) message(s) re-posted');
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('keeps exact small cohort counts after a bounded source observation',()=>fixture(async(h,raw,state)=>{
  raw.exec('UPDATE ask_gaps SET resolved_at=1 WHERE from_seq>3');raw.exec('UPDATE ask_messages SET conflict_count=0 WHERE seq>2');
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  expect(ledger.counts()).toMatchObject({open_gaps:3,conflicts:2,messages:2000,counts_observation_limited:false});
  expect(reads(calls)).toBeLessThanOrEqual(50);
 }));
 it('advances an empty filtered ask page without joining or sorting2000retained rows',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE asks SET state='pending' WHERE id='ask-1871'");
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const first=ledger.list(new URLSearchParams({state:'pending'}));
  expect(first.asks).toEqual([]);expect(first.observation_pending).toBe(true);expect(first.next_cursor).toBeTruthy();expect(first.order).toBe('ask_arrival_desc');
  expect(reads(calls)).toBeLessThanOrEqual(1000);
  expect(calls.some(c=>/JOIN ask_messages/.test(c.query))).toBe(false);
  calls.length=0;await h.rowBudget.begin();
  const next=ledger.list(new URLSearchParams({state:'pending',cursor:first.next_cursor}));
  expect(next.asks.map((a:any)=>a.id)).toEqual(['ask-1871']);expect(reads(calls)).toBeLessThanOrEqual(1000);
 }));
 it('bounds nested message-state observations globally and publishes unknown rather than a partial aggregate',()=>fixture(async(h,raw,state)=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
   INSERT INTO asks(id,message_id,part,text,state,created_at,updated_at) SELECT 'extra-'||i,'msg-2000',i,'retained','pending',i,i FROM n`);
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const page=ledger.messages(new URLSearchParams());
  expect(page.messages[0]).toMatchObject({msg_id:'t2000',states:null,states_observation_limited:true});
  expect(page.next_cursor).toBeTruthy();expect(page.observation_pending).toBe(true);
  expect(calls.filter(c=>/SELECT state FROM asks/.test(c.query))).toHaveLength(1);
  expect(reads(calls)).toBeLessThanOrEqual(1000);
 }));
 it('bounds summary agent/source/gap populations before sorting retained history',()=>fixture(async(h,raw,state)=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
   INSERT INTO ask_agents(agent_id) SELECT 'agent-'||i FROM n`);
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
   INSERT INTO ask_sources(machine_id,source,last_ingest_at) SELECT 'host-'||i,'fixture',i FROM n`);
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const summary=ledger.summary();
  expect(summary.sources).toHaveLength(128);expect(summary.agents).toHaveLength(128);expect(summary.gaps).toHaveLength(100);
  expect(summary.observation_pending).toBe(true);expect(summary.summary_observation_limited).toEqual({sources:true,agents:true,gaps:true});
  expect(reads(calls)).toBeLessThanOrEqual(2000);
 }));
 it('refuses large missing-counter bootstrap before creating an apparently complete zero counter',()=>fixture(async(h,raw,state)=>{
  h.writes.wrap(raw).exec('DROP TABLE ask_counts');const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  expect(()=>ledger.init()).toThrow('ask_counter_migration_observation_limited');
  expect(raw.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='ask_counts'").toArray()).toEqual([]);
  expect(calls.some(c=>/COUNT\s*\(/i.test(c.query))).toBe(false);
  const probes=calls.filter(c=>/SELECT (state FROM asks|role FROM ask_messages) ORDER BY rowid LIMIT 129/.test(c.query));
  expect(probes).toHaveLength(2);expect(reads(probes)).toBeLessThanOrEqual(260);
 }));
 it('keeps a global hole unresolved when129raw user entries cannot prove2000sequence coverage',()=>fixture(async(h,raw,state)=>{
  raw.exec('DELETE FROM ask_gaps');raw.exec("INSERT INTO ask_gaps(scope,from_seq,to_seq,kind,detected_at) VALUES('*',1,2000,'hole',1)");
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  expect(()=>(ledger as any).resolveFilledGaps('fixture',2000,Date.now())).toThrow('ask_gap_coverage_observation_limited');
  expect(raw.exec("SELECT resolved_at FROM ask_gaps WHERE scope='*'").one().resolved_at).toBeNull();
  // A non-covering index can bill both the129indexentries and129table rows.
  expect(reads(calls)).toBeLessThanOrEqual(300);expect(calls.some(c=>/COUNT\s*\(/i.test(c.query))).toBe(false);
 }));
 it('refuses reconciler launch on an incomplete pending cohort before any start effect',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE asks SET state='pending'");raw.exec("UPDATE ask_meta SET reconcile_intake='fixture' WHERE id=1");
  const o=service(h,state);let starts=0;
  const ledger=new AskLedger((o.ledger as any).sql,h.intakeService,fn=>state.storage.transactionSync(fn),{start:async()=>{starts++;return{status:202,body:{}};}} as any);
  await h.rowBudget.begin();await expect(ledger.reconcileDue()).rejects.toMatchObject({code:'ask_reconcile_waiting_observation_limited'});
  expect(starts).toBe(0);expect(reads(o.calls)).toBeLessThanOrEqual(300);
 }));
 it('does not declare a filtered latest-user head absent after scanning only a backfill prefix',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE ask_messages SET source='backfill'");raw.exec("UPDATE ask_meta SET reconcile_intake='fixture' WHERE id=1");
  const o=service(h,state);const ledger=new AskLedger((o.ledger as any).sql,h.intakeService,fn=>state.storage.transactionSync(fn),{start:async()=>({status:202,body:{}})} as any);
  await h.rowBudget.begin();await expect(ledger.reconcileDue()).rejects.toMatchObject({code:'ask_reconcile_cursor_observation_limited'});
  expect(reads(o.calls)).toBeLessThanOrEqual(160);
 }));
 it('manual checking skips2000settled messages/asks without refusing the empty active cohorts',()=>fixture(async(h,raw,state)=>{
  raw.exec('DELETE FROM ask_gaps');raw.exec('UPDATE ask_messages SET conflict_count=0');
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const result=await ledger.tick();expect(result.state).toBe('checked');expect(result.observation_pending).toBe(false);
  expect(result.counts.verified).toBe(2000);expect(reads(calls)).toBeLessThanOrEqual(100);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('manual reconciliation commits raw cursor progress across an assistant-only page before finding a later user',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE ask_messages SET role='assistant',conflict_count=0");raw.exec("UPDATE ask_messages SET role='user' WHERE id='msg-130'");
  raw.exec("UPDATE ask_counts SET n=1 WHERE k='user_messages'");
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const first=await ledger.runReconcilePass();expect(first.state).toBe('observation_pending');expect(first.to_cursor).toBe(128);
  expect(raw.exec('SELECT reconcile_cursor FROM ask_meta WHERE id=1').one().reconcile_cursor).toBe(128);expect(reads(calls)).toBeLessThanOrEqual(500);
  calls.length=0;await h.rowBudget.begin();const next=await ledger.runReconcilePass();
  expect(next.state).toBe('reconciled');expect(next.scanned_messages).toBe(1);expect(next.to_cursor).toBe(256);
  expect(reads(calls)).toBeLessThanOrEqual(1000);expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('does not approve intake closure from an incomplete2000ask intake prefix',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE asks SET intake_id='target'");const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  expect(ledger.closeGate({id:'target',status:'done',history:[]},false)).toMatchObject({code:'ask_close_observation_limited'});
  expect(reads(calls)).toBeLessThanOrEqual(300);
 }));
 it('publishes JSONL progress headers even when a sparse text filter yields an empty first source page',()=>fixture(async(h,raw,state)=>{
  raw.exec("UPDATE ask_messages SET text='needle' WHERE id='msg-130'");
  const {ledger,calls}=service(h,state);await h.rowBudget.begin();
  const first=await ledger.handle(new Request('https://hub/api/asks/messages.jsonl?q=needle'),admin);
  expect(first.status).toBe(200);expect(await first.text()).toBe('');expect(first.headers.get('X-Hub-Asks-Observation-Pending')).toBe('true');
  const cursor=first.headers.get('X-Hub-Asks-Cursor');expect(cursor).toBeTruthy();expect(reads(calls)).toBeLessThanOrEqual(150);
  calls.length=0;await h.rowBudget.begin();
  const next=await ledger.handle(new Request('https://hub/api/asks/messages.jsonl?q=needle&after='+cursor),admin);
  expect(next.status).toBe(200);const rows=(await next.text()).trim().split('\n').map(line=>JSON.parse(line));
  expect(rows.map(r=>r.msg_id)).toEqual(['t130']);expect(reads(calls)).toBeLessThanOrEqual(150);
 }));
});
