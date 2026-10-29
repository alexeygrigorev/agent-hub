import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { hostReconcileFact,validReconcileNative } from '../src/host-reconcile';

const native={native_id:'11111111-1111-4111-8111-111111111111',tag:'task-hub-launch',workspace:'/srv/repo',native_created_at_ms:1000,engine:'codex'};
async function fixture(fn:(sql:any,now:number)=>void|Promise<void>, control=true) {
 const bindings=env as unknown as Env;
 await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('reconcile-'+crypto.randomUUID())),async(h:Hub)=>{
  const sql=h.sql,now=Date.now(),at=new Date(now).toISOString();
  sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,created_at_ms)
    VALUES(?,?,?,?,?,?,?,?)`,'host',native.native_id,native.tag,native.engine,'running',at,native.workspace,1000);
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
    VALUES('launch','agent:test','agent','test','test','{}','host','codex','test','headless','/srv/repo','','running',?,?,?)`,native.native_id,at,at);
  if(control) sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('session','agent:test','session','{}','launch','host','codex','headless','finished',?,?,?)`,JSON.stringify(native),at,at);
  await fn(sql,now);
 });
}
describe('retaining host reconciliation authority',()=>{
 it('authorizes the exact managed terminal incarnation even while its native remains alive',()=>fixture((sql,now)=>{
  expect(hostReconcileFact(sql,'host',native,now)).toMatchObject({...native,managed:true,mode:'headless',protected:false,state:'finished'});
 }));
 it('refuses a live Hub alias, interactive mode, keep flag and explicit protection',()=>fixture((sql,now)=>{
  sql.exec("UPDATE session_control_sessions SET state='running'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  sql.exec("UPDATE session_control_sessions SET state='finished',mode='interactive'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  sql.exec("UPDATE session_control_sessions SET mode='headless',request_json=?",JSON.stringify({keep:true}));
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  sql.exec("UPDATE session_control_sessions SET request_json='{}'");
  sql.exec("INSERT INTO session_control_protections VALUES('session','human:operator','protect',?)",new Date(now).toISOString());
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
 }));
 it('requires a fresh host catalog and all native identity fields',()=>fixture((sql,now)=>{
  for(const change of [{tag:'replacement'},{workspace:'/different'},{engine:'shell'},{native_created_at_ms:2000}])
   expect(hostReconcileFact(sql,'host',{...native,...change},now)).toBeNull();
  expect(hostReconcileFact(sql,'foreign-host',native,now)).toBeNull();
  expect(hostReconcileFact(sql,'host',native,now+180001)).toBeNull();
 }));
 it('rejects malformed or extended authority inputs',()=>{
  expect(validReconcileNative(native)).toBe(true);
  for(const value of [null,{}, {...native,native_created_at_ms:true},{...native,private:'data'},{...native,tag:'bad\nline'}])
   expect(Boolean(validReconcileNative(value))).toBe(false);
 });
 it('authorizes a known managed headless native whose Hub session is missing',()=>fixture((sql,now)=>{
  expect(hostReconcileFact(sql,'host',native,now)).toMatchObject({state:'missing',managed:true});
 },false));
 it('does not infer cleanup authority for a human-owned missing session',()=>fixture((sql,now)=>{
  sql.exec("UPDATE fleet_launches SET requester_kind='human',actor='human:alexey'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
 },false));
 it('does not let a terminal alias bypass another live alias',()=>fixture((sql,now)=>{
  const at=new Date(now).toISOString();
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('alias','agent:test','alias','{}','launch','host','codex','headless','running',?,?,?)`,JSON.stringify(native),at,at);
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
 }));
});
