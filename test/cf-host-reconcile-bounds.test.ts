import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {hostReconcileFact} from '../src/host-reconcile';
import {intakeLinkHealth} from '../src/intake-link-health';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';

const native={native_id:'11111111-1111-4111-8111-111111111111',tag:'task-hub-launch',workspace:'/srv/repo',native_created_at_ms:1000,engine:'codex'};
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('host-reconcile-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),meter=new RowsMeter(()=>now),queries:string[]=[];
 const tracked=new Proxy(h.sql,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{queries.push(query);return target.exec(query,...args);};const value=Reflect.get(target,key);return typeof value==='function'?value.bind(target):value;}});
 const sql=meter.wrap(tracked);
 const launch=(id:string,nativeId:string=native.native_id)=>seed.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
  VALUES(?,'agent:test','agent','test',?,'{}','host','codex','test','headless','/srv/repo','','running',?,?,?)`,id,id,nativeId,at,at);
 const alias=(id:string,nativeId:string=native.native_id,launchId='launch',mode='headless')=>seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
  VALUES(?,'agent:test',?,'{}',?,'host','codex',?,'finished',?,?,?)`,id,id,launchId,mode,JSON.stringify({...native,native_id:nativeId}),at,at);
 seed.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,created_at_ms) VALUES('host',?,?,?,'running',?,?,?)`,native.native_id,native.tag,native.engine,at,native.workspace,1000);
 launch('launch');
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 expect(intakeLinkHealth(sql).ready).toBe(true);
 fn({h,seed,sql,meter,queries,now,at,launch,alias});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});

describe('bounded exact-incarnation host reconciliation',()=>{
 it('retains terminal, protected and missing-session authority with2100 unrelated same-host rows',()=>fixture(({seed,sql,meter,now,at,launch,alias}:any)=>{
  for(let i=0;i<2100;i++){launch('retained-launch-'+i,'retained-native-'+i);alias('retained-alias-'+i,'retained-native-'+i,'retained-launch-'+i);}
  alias('session');
  expect(meter.runSync('managed',()=>hostReconcileFact(sql,'host',native,now))).toMatchObject({state:'finished',managed:true,protected:false});
  expect(meter.snapshot().managed.rows_read).toBeLessThan(250);expect(meter.snapshot().managed.rows_written).toBe(0);
  seed.exec("INSERT INTO session_control_protections VALUES('session','human:operator','protect',?)",at);
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("DELETE FROM session_control_protections WHERE session_id='session'");
  seed.exec("DELETE FROM session_control_sessions WHERE id='session'");
  expect(meter.runSync('orphan',()=>hostReconcileFact(sql,'host',native,now))).toMatchObject({state:'missing',managed:true});
  expect(meter.snapshot().orphan.rows_read).toBeLessThan(250);expect(meter.snapshot().orphan.rows_written).toBe(0);
  seed.exec("UPDATE fleet_launches SET requester_kind='human',actor='human:alexey' WHERE id='launch'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  const plan=seed.exec(`EXPLAIN QUERY PLAN SELECT rowid FROM session_control_sessions INDEXED BY intake_queue_session_native
   WHERE machine_id=? AND json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_id')=? LIMIT 129`,'host',native.native_id).toArray().map((row:any)=>row.detail).join(' | ');
  expect(plan).toContain('machine_id=? AND <expr>=?');expect(plan).not.toMatch(/TEMP B-TREE/);
 }));
 it('cannot hide newer human launch, alias or creator custody behind older agent rows',()=>fixture(({seed,sql,now,launch,alias,at}:any)=>{
  alias('oldest-agent');
  expect(hostReconcileFact(sql,'host',native,now)).toMatchObject({state:'finished',protected:false});
  alias('newer-human');
  seed.exec("UPDATE session_control_sessions SET actor='human:alexey' WHERE id='newer-human'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("DELETE FROM session_control_sessions WHERE id='newer-human'");
  launch('newer-human-launch');
  seed.exec("UPDATE fleet_launches SET actor='human:alexey',requester_kind='human' WHERE id='newer-human-launch'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("DELETE FROM fleet_launches WHERE id='newer-human-launch'");
  alias('newer-creator');
  seed.exec("UPDATE session_control_sessions SET owner_token_id='human-creator' WHERE id='newer-creator'");
  // Missing/deleted custody evidence never authorizes a host reaper.
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("INSERT INTO credentials(id,kind,actor,token_hash,family_id,subject,scopes,created_by_sub,created_at,expires_at) VALUES('human-creator','human','human:alexey','fixture-hash','fixture-family','alexey','[]','alexey',?,?)",at,at);
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("UPDATE session_control_sessions SET owner_token_id='' WHERE id='newer-creator'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("UPDATE session_control_sessions SET owner_token_id='human-creator' WHERE id='newer-creator'");
  seed.exec("UPDATE credentials SET kind='legacy_unknown',actor='agent:fixture' WHERE id='human-creator'");
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("UPDATE credentials SET kind='human',actor='human:alexey' WHERE id='human-creator'");
  seed.exec("UPDATE session_control_sessions SET owner_token_id=NULL,request_json=? WHERE id='newer-creator'",JSON.stringify({provenance:{creator_token_id:'human-creator'}}));
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
  seed.exec("DELETE FROM session_control_sessions WHERE id='newer-creator'");
  launch('creator-launch');
  seed.exec("UPDATE fleet_launches SET request_json=? WHERE id='creator-launch'",JSON.stringify({provenance:{creator_token_id:'human-creator'}}));
  expect(hostReconcileFact(sql,'host',native,now)).toBeNull();
 }));
 it('refuses launch overflow before request hydration or classification',()=>fixture(({sql,meter,queries,now,launch}:any)=>{
  for(let i=0;i<128;i++)launch('duplicate-launch-'+i);
  queries.length=0;expect(meter.runSync('overflow',()=>hostReconcileFact(sql,'host',native,now))).toBeNull();
  expect(meter.snapshot().overflow.rows_read).toBeLessThan(300);expect(meter.snapshot().overflow.rows_written).toBe(0);
  expect(queries.some((q:string)=>q.includes('SELECT l.*'))).toBe(false);
 }));
 it('refuses both native-alias and launch-linked alias overflow before binding JSON hydration',()=>fixture(({seed,sql,meter,queries,now,alias}:any)=>{
  for(let i=0;i<129;i++)alias('duplicate-alias-'+i);
  queries.length=0;expect(meter.runSync('native-overflow',()=>hostReconcileFact(sql,'host',native,now))).toBeNull();
  expect(meter.snapshot()['native-overflow'].rows_read).toBeLessThan(300);
  expect(queries.some((q:string)=>q.includes('SELECT s.*'))).toBe(false);
  seed.exec("DELETE FROM session_control_sessions WHERE id LIKE 'duplicate-alias-%'");
  for(let i=0;i<129;i++)alias('launch-alias-'+i,'different-native-'+i);
  queries.length=0;expect(meter.runSync('launch-overflow',()=>hostReconcileFact(sql,'host',native,now))).toBeNull();
  expect(meter.snapshot()['launch-overflow'].rows_read).toBeLessThan(300);expect(meter.snapshot()['launch-overflow'].rows_written).toBe(0);
  expect(queries.some((q:string)=>q.includes('SELECT s.*'))).toBe(false);
  seed.exec("DELETE FROM session_control_sessions WHERE id LIKE 'launch-alias-%'");
  for(let i=0;i<70;i++){alias('native-range-'+i,native.native_id,'unrelated-launch');alias('launch-range-'+i,'foreign-native-'+i);}
  queries.length=0;expect(meter.runSync('union-overflow',()=>hostReconcileFact(sql,'host',native,now))).toBeNull();
  expect(meter.snapshot()['union-overflow'].rows_read).toBeLessThan(300);expect(meter.snapshot()['union-overflow'].rows_written).toBe(0);
  expect(queries.some((q:string)=>q.includes('SELECT s.*'))).toBe(false);
 }));
});
