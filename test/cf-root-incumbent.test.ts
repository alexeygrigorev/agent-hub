import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import {JobsService} from '../src/jobs';
import {fixtureSql} from './helpers/fixture-budget';
import { initRootIncumbentProjection,advanceRootIncumbentProjection,externalRootBlocks } from '../src/root-incumbent';
const fixture=(fn:(raw:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('root-projection-'+crypto.randomUUID())),async(_hub:any,state:any)=>{
 const raw=state.storage.sql;
 for(const op of ['insert','update','delete'])raw.exec('DROP TRIGGER IF EXISTS root_incumbent_'+op);
 raw.exec('DROP TABLE IF EXISTS root_incumbent_candidates');raw.exec('DROP TABLE IF EXISTS root_incumbent_migration');fn(raw);
});
function observe(raw:any){const calls:{query:string;cursor:any}[]=[];return{calls,sql:new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}})};}
const insert=(sql:any,tag:string,state='active',machine:string|null=null,lastSeen:string|null=null)=>sql.exec('INSERT INTO agents(project,tag,role,state,health_state,machine_id,last_seen_at) VALUES(?,?,\'root\',?,?,?,?)','incumbent-fixture',tag,state,state,machine,lastSeen);
describe('external root authority without background registry scans',()=>{
 it('blocks until bounded durable rowid backfill has proved the retained source complete',()=>fixture(raw=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
    INSERT INTO agents(project,tag,role) SELECT 'incumbent-fixture','worker-'||i,'worker' FROM n`);
  const o=observe(raw);initRootIncumbentProjection(o.sql);
  expect(externalRootBlocks(o.sql,Date.now())).toBeNull();
  expect(o.calls.some(c=>/FROM agents/.test(c.query))).toBe(false);
  advanceRootIncumbentProjection(o.sql);
  expect(o.calls.filter(c=>/SELECT rowid AS id/.test(c.query))).toHaveLength(1);
  expect(o.calls.find(c=>/SELECT rowid AS id/.test(c.query))!.cursor.rowsRead).toBeLessThanOrEqual(101);
  let pages=0;
  while(!advanceRootIncumbentProjection(raw)){expect(++pages).toBeLessThan(25);}
  const read=observe(raw);expect(externalRootBlocks(read.sql,Date.now())).toBe(false);
  expect(read.calls.every(c=>! /FROM agents/.test(c.query))).toBe(true);
  expect(read.calls.reduce((n,c)=>n+c.cursor.rowsRead,0)).toBeLessThanOrEqual(4);
 }));
 it('maintains root candidates while migration is incomplete, including changed and deleted source rows',()=>fixture(raw=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<150)
    INSERT INTO agents(project,tag,role) SELECT 'incumbent-fixture','worker-'||i,'worker' FROM n`);
  initRootIncumbentProjection(raw);
  advanceRootIncumbentProjection(raw);
  raw.exec("UPDATE agents SET role='root',state='active' WHERE tag='worker-1'");
  insert(raw,'new-root','unknown');raw.exec("DELETE FROM agents WHERE tag='new-root'");
  while(!advanceRootIncumbentProjection(raw)){}
  expect(externalRootBlocks(raw,Date.now())).toBe(true);
  raw.exec("UPDATE agents SET state='retired' WHERE tag='worker-1'");
  expect(externalRootBlocks(raw,Date.now())).toBe(false);
 }));
 it('never hides another root behind a confirmed-dead head and resets cleared authority on source updates',()=>fixture(raw=>{
  const now=Date.now(),old=new Date(now-3600000).toISOString();
  raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('root-host','root-host','root-host',?,?)",old,old);
  insert(raw,'dead-root','dead','root-host',old);insert(raw,'unknown-root','unknown');
  initRootIncumbentProjection(raw);while(!advanceRootIncumbentProjection(raw)){}
  const o=observe(raw);expect(externalRootBlocks(o.sql,now)).toBeNull();
  expect(o.calls.filter(c=>/FROM agents/.test(c.query)).every(c=>/WHERE rowid=\?/.test(c.query))).toBe(true);
  expect(o.calls.reduce((n,c)=>n+c.cursor.rowsRead,0)).toBeLessThanOrEqual(12);
  expect(externalRootBlocks(raw,now)).toBe(true);
  raw.exec("DELETE FROM agents WHERE tag='unknown-root'");expect(externalRootBlocks(raw,now)).toBe(false);
  raw.exec("UPDATE agents SET state='active',health_state='active' WHERE tag='dead-root'");
  expect(externalRootBlocks(raw,now)).toBe(true);
 }));
 it('keeps contradictory fresh death and unknown host transport blocking',()=>fixture(raw=>{
  const now=Date.now();insert(raw,'contradictory','dead','missing',new Date(now).toISOString());
  initRootIncumbentProjection(raw);while(!advanceRootIncumbentProjection(raw)){}
  expect(externalRootBlocks(raw,now)).toBe(true);
  raw.exec("UPDATE agents SET last_seen_at=? WHERE tag='contradictory'",new Date(now-3600000).toISOString());
  expect(externalRootBlocks(raw,now)).toBe(true);
 }));
});

describe('root readiness request migration gates',()=>{
 it('advances only eligible current hosts, keeps same-slot progress, and stops on disable',()=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('root-ready-gates-'+crypto.randomUUID())),async(h:any,state:any)=>{
   const seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString();
   seed.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
    INSERT INTO agents(project,tag,role) SELECT 'root-ready-gates','retained-'||i,'worker' FROM n`);
   seed.exec('UPDATE root_incumbent_migration SET after_rowid=0,done=0 WHERE id=1');
   seed.exec('UPDATE root_scheduling SET enabled=1 WHERE id=1');
   const machine=(id:string,capable:boolean,allowed:boolean,revoked=false)=>seed.exec(`INSERT INTO machines
    (id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities,revoked_at) VALUES(?,?,?,?,?,?,?,?)`,
    id,id,id,at,at,JSON.stringify({gate:{launch_allowed:allowed}}),JSON.stringify(capable?{jobs:['root-run']}:{ }),revoked?at:null);
   machine('incapable',false,true);machine('blocked',true,false);machine('revoked',true,true,true);machine('ready',true,true);
   await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
   const jobs=new JobsService(h.sql,state,{now:()=>new Date(now)});
   const cursor=()=>seed.exec('SELECT after_rowid,done FROM root_incumbent_migration WHERE id=1').one();
   for(const host of ['incapable','blocked','revoked']){
    h.meter.runSync('root-unready-'+host,()=>jobs.markRootReady(host));
    expect(cursor()).toEqual({after_rowid:0,done:0});
    expect(h.meter.snapshot()['root-unready-'+host].rows_written).toBe(0);
   }
   h.meter.runSync('root-ready-first',()=>jobs.markRootReady('ready'));
   const first=cursor();expect(first.after_rowid).toBeGreaterThan(0);expect(first.done).toBe(0);
   expect(h.meter.snapshot()['root-ready-first'].rows_read).toBeLessThan(140);
   const readiness=seed.exec("SELECT slot_ms,host_id FROM root_background_ready WHERE host_id='ready' ORDER BY slot_ms").toArray();
   expect(readiness).toHaveLength(2);
   h.meter.runSync('root-ready-second',()=>jobs.markRootReady('ready'));
   expect(cursor().after_rowid).toBeGreaterThan(first.after_rowid);
   expect(seed.exec("SELECT slot_ms,host_id FROM root_background_ready WHERE host_id='ready' ORDER BY slot_ms").toArray()).toEqual(readiness);
   const beforeDisable=cursor();seed.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
   h.meter.runSync('root-disabled',()=>jobs.markRootReady('ready'));
   expect(cursor()).toEqual(beforeDisable);expect(h.meter.snapshot()['root-disabled'].rows_written).toBe(0);
   expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  }));
});
