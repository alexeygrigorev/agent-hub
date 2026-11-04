import {describe,it,expect,vi} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {WebhookService} from '../src/webhooks';
import {fixtureSql} from './helpers/fixture-budget';
const indexes=['session_deliveries_queued_order','session_deliveries_active_source'];
const fixture=(fn:(h:any,state:any,seed:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('delivery-retained-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 h.sql.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');h.backgroundWork.cancel('root-slot');
 await fn(h,state,fixtureSql(h,state));
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
function delivery(sql:any,id:string,status='delivered',command:string|null=null){const at=new Date().toISOString();sql.exec(`INSERT INTO session_deliveries(id,source,source_id,delivery_key,target_label,body,status,command_id,created_at,updated_at,next_attempt_at) VALUES(?,'webhook','gone',?,'gone','body',?,?,?,?,?)`,id,id,status,command,at,at,new Date(Date.now()+600000).toISOString());}
function command(sql:any,id:string){const at=new Date().toISOString();sql.exec("INSERT OR IGNORE INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at) VALUES('owner','test','owner','{}','headless','running',?,?)",at,at);sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at) VALUES(?,'owner','send','test',?,'{}','queued',1,?,?,?)`,id,id,id,at,at);}
const names=(state:any)=>state.storage.sql.exec("SELECT name FROM pragma_index_list('session_deliveries','main')").toArray().map((r:any)=>r.name);
describe('retained deliveries use point-owned work without startup indexes',()=>{
 it('skips two history builds over8 terminal deliveries and leaves idle probes empty',()=>fixture(async(h,state,seed)=>{
  for(const name of indexes)seed.exec('DROP INDEX '+name);
  for(let i=0;i<8;i++)delivery(seed,'old-'+i);
  const svc=new WebhookService(h.sql,(fn:any)=>state.storage.transactionSync(fn),Date.now,()=>null,null);
  h.meter.runSync('retained-delivery-init',()=>svc.init());
  expect(indexes.some(name=>names(state).includes(name))).toBe(false);
  expect(h.meter.snapshot()['retained-delivery-init'].rows_written).toBe(0);
  expect(h.meter.snapshot()['retained-delivery-init'].rows_read).toBeLessThan(80);
  expect(h.meter.runSync('retained-delivery-head',()=>svc.nextBackgroundWork())).toBe(null);
  expect(h.meter.snapshot()['retained-delivery-head'].rows_read).toBeLessThan(5);
 }));
 it('ACKs only the current command owner among1001 terminal rows without history indexes',()=>fixture(async(h,state,seed)=>{
  for(const name of indexes)seed.exec('DROP INDEX '+name);
  for(let i=0;i<1001;i++)delivery(seed,'old-'+i);
  command(seed,'ack-old');command(seed,'ack-current');delivery(seed,'target','queued','ack-old');
  h.sql.exec("UPDATE session_deliveries SET command_id='ack-current' WHERE id='target'");
  const before=state.storage.sql.exec("SELECT next_attempt_at FROM session_deliveries WHERE id='target'").one().next_attempt_at;
  h.meter.runSync('old-command-ack',()=>h.sql.exec("UPDATE session_control_commands SET ack_json='{}' WHERE id='ack-old'"));
  expect(state.storage.sql.exec("SELECT next_attempt_at FROM session_deliveries WHERE id='target'").one().next_attempt_at).toBe(before);
  h.meter.runSync('current-command-ack',()=>h.sql.exec(`UPDATE session_control_commands SET ack_json='{"outcome":"delivered"}' WHERE id='ack-current'`));
  expect(state.storage.sql.exec("SELECT next_attempt_at FROM session_deliveries WHERE id='target'").one().next_attempt_at).not.toBe(before);
  expect(h.meter.snapshot()['current-command-ack'].rows_read).toBeLessThan(40);
  await h.meter.run('known-ack-consumer',()=>h.webhookService.processBackground('target'));
  expect(state.storage.sql.exec("SELECT status FROM session_deliveries WHERE id='target'").one().status).toBe('delivered');
  expect(h.meter.snapshot()['known-ack-consumer'].rows_read).toBeLessThan(40);
  expect(indexes.some(name=>names(state).includes(name))).toBe(false);
 }));
 it('prepares only explicit global work and cannot retain readiness after external DDL',()=>fixture(async(h,state,seed)=>{
  for(const name of indexes)seed.exec('DROP INDEX '+name);delivery(seed,'old');
  await h.webhookService.pump();expect(indexes.every(name=>names(state).includes(name))).toBe(true);
  for(const name of indexes)seed.exec('DROP INDEX '+name);
  await h.webhookService.pump();expect(indexes.every(name=>names(state).includes(name))).toBe(true);
 }));
 it('coalesces concurrent known-entity consumers without a duplicate send pass',()=>fixture(async(h)=>{
  let release!:()=>void;const pending=new Promise<void>(resolve=>{release=resolve;});
  const spy=vi.spyOn(h.webhookService as any,'pumpOnce').mockImplementation(async()=>{await pending;return undefined;});
  const first=h.webhookService.processBackground('one');
  expect(await h.webhookService.processBackground('two')).toBeGreaterThanOrEqual(Date.now()+14000);
  release();expect(await first).toBe(null);expect(spy).toHaveBeenCalledTimes(1);spy.mockRestore();
 }));
});
