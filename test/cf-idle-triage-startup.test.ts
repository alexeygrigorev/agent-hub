import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {IntakeTriage} from '../src/intake-triage';
import {IdleWorkService} from '../src/idle-work';
import {fixtureSql} from './helpers/fixture-budget';
const indexes=['intake_triage_pending','intake_triage_request_key','intake_triage_session_state','intake_triage_sessions_recent'];
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('idle-triage-startup-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now();
 await fn({h,state,seed,now,tx:(fn:any)=>state.storage.transactionSync(fn)});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
const intake:any={statusAuthorizedTasks:()=>[],watchdogTasks:()=>[]};
const watchdog:any={project:()=>[]};
function retainedRequests(seed:any,now:number){
 for(const name of indexes)seed.exec('DROP INDEX '+name);
 seed.exec('UPDATE intake_triage_meta SET last_tick=NULL,last_session=NULL WHERE id=1');
 for(let i=0;i<356;i++)seed.exec("INSERT INTO intake_triage_requests VALUES(?,?,NULL,'triaged',?)",'old-'+String(i).padStart(3,'0'),'key-'+i,now);
}
const countIndexes=(state:any)=>state.storage.sql.exec("SELECT name FROM pragma_index_list('intake_triage_requests','main')").toArray().filter((r:any)=>indexes.includes(r.name)).length;
describe('retained idle/triage sources never build history indexes at construction',()=>{
 it('leaves356 requests untouched at init/status/not-due; only an explicit due tick prepares indexes',()=>fixture(async({h,state,seed,now,tx}:any)=>{
  retainedRequests(seed,now);const service=new IntakeTriage(h.sql,intake,watchdog,tx,{},()=>now);
  h.meter.runSync('retained-triage-init',()=>service.init());expect(countIndexes(state)).toBe(0);
  expect(h.meter.snapshot()['retained-triage-init'].rows_read).toBeLessThan(40);expect(h.meter.snapshot()['retained-triage-init'].rows_written).toBe(0);
  const status=h.meter.runSync('retained-triage-status',()=>service.status());expect(status).toMatchObject({pending_requests:null,pending_requests_lower_bound:0,pending_requests_truncated:true,observation_pending:true});
  expect(h.meter.snapshot()['retained-triage-status'].rows_read).toBeLessThan(300);expect(()=> (service as any).activeSession()).toThrow('triage_request_observation_pending');
  seed.exec('UPDATE intake_triage_meta SET last_tick=? WHERE id=1',now);
  expect(await h.meter.run('not-due-triage',()=>service.tick('manual'))).toMatchObject({state:'already_checked'});expect(countIndexes(state)).toBe(0);expect(h.meter.snapshot()['not-due-triage'].rows_written).toBe(0);
  seed.exec('UPDATE intake_triage_meta SET last_tick=NULL WHERE id=1');
  expect(await h.meter.run('explicit-due-triage',()=>service.tick('manual'))).toMatchObject({state:'nothing_to_triage'});
  expect(countIndexes(state)).toBe(4);expect(service.status()).toMatchObject({pending_requests:0,pending_requests_truncated:false});
  expect(h.meter.statementTop(4096).some((r:any)=>r.scope==='retained-triage-init'&&/^CREATE INDEX/i.test(r.sql))).toBe(false);
 }));
 it('handles a positively observed failed reviewer before the interval without consuming unrelated requests',()=>fixture(async({h,state,seed,now,tx}:any)=>{
  retainedRequests(seed,now);const at=new Date(now).toISOString(),alerts:any[]=[];
  seed.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,hold_json,created_at,updated_at) VALUES('refused-reviewer','fixture','refused-reviewer','{}','headless','refused','{\"reason\":\"blocked\"}',?,?)",at,at);
  seed.exec("UPDATE intake_triage_requests SET state='requested',session_id='refused-reviewer' WHERE task_id='old-000'");
  seed.exec("UPDATE intake_triage_meta SET last_tick=?,last_session='refused-reviewer' WHERE id=1",now);
  const service=new IntakeTriage(h.sql,intake,watchdog,tx,{alert:async(...args:any[])=>{alerts.push(args);}},()=>now);service.init();
  expect(await service.tick('manual')).toMatchObject({state:'already_checked'});expect(countIndexes(state)).toBe(4);
  expect(state.storage.sql.exec("SELECT task_id FROM intake_triage_requests WHERE task_id='old-000'").toArray()).toEqual([]);
  expect(state.storage.sql.exec("SELECT state FROM intake_triage_requests WHERE task_id='old-001'").one().state).toBe('triaged');
  expect(state.storage.sql.exec('SELECT last_tick,last_session,last_alert FROM intake_triage_meta WHERE id=1').one()).toMatchObject({last_tick:now,last_session:null,last_alert:'blocked'});expect(alerts).toHaveLength(1);
 }));
 it('rolls back request-owned index preparation before consuming any due cursor or emitting work',()=>fixture(async({h,state,seed,now,tx}:any)=>{
  retainedRequests(seed,now);let starts=0;
  const failTx=(fn:any)=>tx(()=>{const value=fn();if(countIndexes(state)===4)throw new Error('rollback-triage-indexes');return value;});
  const service=new IntakeTriage(h.sql,intake,watchdog,failTx,{start:async()=>{starts++;return{status:202,body:{id:'forbidden'}};}},()=>now);service.init();
  await expect(service.tick('manual')).rejects.toThrow('rollback-triage-indexes');expect(countIndexes(state)).toBe(0);expect(starts).toBe(0);
  expect(state.storage.sql.exec('SELECT last_tick,task_cursor,request_cursor FROM intake_triage_meta WHERE id=1').one()).toMatchObject({last_tick:null,task_cursor:'',request_cursor:''});
  const retry=new IntakeTriage(h.sql,intake,watchdog,tx,{},()=>now);retry.init();expect(await retry.tick('manual')).toMatchObject({state:'nothing_to_triage'});expect(countIndexes(state)).toBe(4);
 }));
 it('advances raw51 episode pages through755 inactive prefixes before closing only the late observed owner',()=>fixture(async({h,state,seed,now,tx}:any)=>{
  seed.exec('DROP INDEX IF EXISTS session_idle_episodes_active');
  for(let i=0;i<755;i++)seed.exec("INSERT INTO session_idle_episodes(session_id,episode,detected_at,active,nudges_json) VALUES(?,?,?,?,'[]')",'old-'+String(i).padStart(3,'0'),'episode-'+i,now,i===754?1:0);
  const sessions:any={attentionPage:()=>({items:[],evaluated:[],next:''}),attention:()=>{throw new Error('global attention forbidden');}};
  const service=new IdleWorkService(h.sql,tx,sessions,{},()=>now);h.meter.runSync('retained-idle-init',()=>service.init());
  expect(state.storage.sql.exec("SELECT name FROM pragma_index_list('session_idle_episodes','main') WHERE name='session_idle_episodes_active'").toArray()).toEqual([]);
  expect(h.meter.snapshot()['retained-idle-init'].rows_written).toBe(0);
  for(let i=0;i<16;i++){
   await h.meter.run('raw-idle-page-'+i,()=>service.tick());expect(h.meter.snapshot()['raw-idle-page-'+i].rows_read).toBeLessThan(160);
   expect(state.storage.sql.exec("SELECT active FROM session_idle_episodes WHERE session_id='old-754'").one().active).toBe(i<15?1:0);
  }
  expect(state.storage.sql.exec("SELECT value FROM session_idle_work_state WHERE key='episode'").one().value).toBe('');
  expect(h.meter.statementTop(4096).filter((r:any)=>r.sql.includes('FROM session_idle_episodes WHERE active=1'))).toEqual([]);
 }));
 it('never treats a bounded injected attention window as proof that a running owner recovered',()=>fixture(async({h,state,seed,now,tx}:any)=>{
  seed.exec('DROP INDEX IF EXISTS session_idle_episodes_active');
  seed.exec("INSERT INTO session_idle_episodes(session_id,episode,detected_at,active,nudges_json) VALUES('owner','episode',?,1,'[]')",now);
  const at=new Date(now).toISOString();seed.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at) VALUES('owner','fixture','owner','{}','headless','running',?,?)",at,at);
  let present=true;const calls:any[]=[];
  const sessions:any={attention:(ids?:string[])=>{calls.push(ids);return ids?(present?[{session_id:'owner'}]:[]):Array.from({length:100},(_,i)=>({session_id:i===99?'owner':'foreign-'+i}));}};
  const service=new IdleWorkService(h.sql,tx,sessions,{},()=>now);service.init();await service.tick();
  expect(state.storage.sql.exec("SELECT active FROM session_idle_episodes WHERE session_id='owner'").one().active).toBe(1);expect(calls.some(ids=>ids?.[0]==='owner')).toBe(true);
  present=false;await service.tick();expect(state.storage.sql.exec("SELECT active FROM session_idle_episodes WHERE session_id='owner'").one().active).toBe(0);
 }));
});
