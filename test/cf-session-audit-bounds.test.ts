import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {SessionAuditService} from '../src/session-audit';
import {RowsMeter} from '../src/rows-meter';
const fixture=(fn:(raw:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('audit-bounds-'+crypto.randomUUID())),(_h:any,state:any)=>fn(state.storage.sql));
const at=new Date().toISOString();
function sessions(raw:any,count:number){raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
 INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
 SELECT 'audit-'||i,'fixture','audit-'||i,'{}','headless','running',?,? FROM n`,count,at,at);}
describe('manual audit source bounds',()=>{
 it('refuses150 active sessions before effects instead of exhausting the day',()=>fixture(async raw=>{
  sessions(raw,150);const meter=new RowsMeter(),service=new SessionAuditService(meter.wrap(raw));let effects=0;
  (service as any).hooks={request:async()=>{effects++;return {status:200,body:{}};}};
  const result=await meter.run('audit',()=>service.tick('manual'));
  expect(result).toMatchObject({state:'observation_pending',observation_pending:true});expect(effects).toBe(0);
  expect(meter.snapshot().audit.rows_read).toBeLessThan(150);expect(meter.snapshot().audit.rows_written).toBe(0);
 }));
 it('ignores2100 retained terminal natives while verifying a known terminal stop by PK',()=>fixture(async raw=>{
  raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('host','host','host',?,?)",at,at);
  raw.exec("INSERT INTO fleet_catalog_seen VALUES('host',?)",at);
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) SELECT 'host','old-'||i,'old','exited',?,'/repo' FROM n`,at);
  raw.exec("INSERT INTO session_audit_decisions(run_id,at,session_id,machine_id,native_id,rule,action,features_json) VALUES(0,?,'gone','host','old-2100','fixture','stop','{}')",at);
  const meter=new RowsMeter(),service=new SessionAuditService(meter.wrap(raw));
  const result=await meter.run('audit',()=>service.tick('manual'));
  expect(result.state).toBe('audited');expect(result.scanned).toBe(0);
  expect(raw.exec("SELECT outcome FROM session_audit_decisions WHERE session_id='gone'").one().outcome).toBe('stopped_verified');
  expect(meter.snapshot().audit.rows_read).toBeLessThan(350);
 }));
 it('refuses incomplete decision evidence and caps dataset views',()=>fixture(async raw=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO session_audit_decisions(run_id,at,rule,action,features_json) SELECT 0,?,'fixture','nudge','{}' FROM n`,at);
  const meter=new RowsMeter(),service=new SessionAuditService(meter.wrap(raw));
  expect(await meter.run('audit',()=>service.tick('manual'))).toMatchObject({state:'observation_pending'});
  expect(meter.snapshot().audit.rows_read).toBeLessThan(350);expect(meter.snapshot().audit.rows_written).toBe(0);
  const exported=meter.runSync('export',()=>service.exportRows(20000));expect(exported.decisions).toHaveLength(128);
  expect(meter.snapshot().export.rows_read).toBeLessThan(300);
 }));
});
