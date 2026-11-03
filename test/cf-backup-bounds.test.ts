import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { BackupService } from '../src/backup';
const fixture=(fn:(h:any,raw:any,state:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('backup-bounds-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const raw=state.storage.sql;
 raw.exec('CREATE TABLE aaa_backup_large(id INTEGER PRIMARY KEY,value TEXT)');
 raw.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000) INSERT INTO aaa_backup_large SELECT i,'retained-'||i FROM n");
 await fn(h,raw,state);
});
function observe(native:any){const calls:{query:string;args:any[];cursor:any}[]=[];return{calls,sql:new Proxy(native,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,args,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}})};}
const reads=(calls:{cursor:any}[])=>calls.reduce((n,c)=>n+c.cursor.rowsRead,0);
const admin={caller:{isAdmin:true,actor:'human:backup-fixture'}};
function service(h:any,state:any){const o=observe(h.sql),backup=new BackupService(o.sql,fn=>state.storage.transactionSync(fn),h.env);o.calls.length=0;return{...o,backup};}
describe('backup reads bounded retained source prefixes',()=>{
 it('caps keyset chunks at128 and exposes unknown totals without scanning2000rows',()=>fixture(async(h,_raw,state)=>{
  const {backup,calls}=service(h,state);await h.rowBudget.begin();
  const first=backup.handle(new Request('https://hub/api/backup/export?table=aaa_backup_large&after=first&limit=5000'),admin);
  expect(first.status).toBe(200);const body:any=await first.json();
  expect(body.limit).toBe(128);expect(body.rows).toHaveLength(128);expect(body.total).toBeNull();expect(body.total_exact).toBe(false);expect(body.next_after).toBe('128');
  expect(reads(calls)).toBeLessThanOrEqual(3100);
  expect(calls.some(c=>/COUNT\s*\(/i.test(c.query))).toBe(false);
  calls.length=0;await h.rowBudget.begin();
  const second=backup.handle(new Request('https://hub/api/backup/export?table=aaa_backup_large&after='+body.next_after),admin);
  expect(second.status).toBe(200);const next:any=await second.json();
  expect(next.rows[0].id).toBe(129);expect(next.next_after).toBe('256');expect(reads(calls)).toBeLessThanOrEqual(3100);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('reports limited status counts explicitly rather than exactCOUNT over retained tables',()=>fixture(async(h,_raw,state)=>{
  const {backup,calls}=service(h,state);await h.rowBudget.begin();
  const response=backup.handle(new Request('https://hub/api/backup/status'),admin);
  expect(response.status).toBe(200);const body:any=await response.json();
  expect(body.row_counts.aaa_backup_large).toBeNull();expect(body.counts_observation_limited).toBe(true);
  expect(body.api_version).toBeGreaterThanOrEqual(6);expect(reads(calls)).toBeLessThanOrEqual(3100);
  expect(calls.some(c=>/COUNT\s*\(/i.test(c.query))).toBe(false);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('refuses a full2000row dump after atmost1001rawdata rows',()=>fixture(async(h,_raw,state)=>{
  const {backup,calls}=service(h,state);await h.rowBudget.begin();
  const response=backup.handle(new Request('https://hub/api/backup/export'),admin);
  expect(response.status).toBe(413);expect(await response.json()).toEqual({error:'export_too_large'});
  // Max1025schema rows twice +1001data rows +bounded rate-limit overhead.
  expect(reads(calls)).toBeLessThanOrEqual(3100);
  const data=calls.filter(c=>/SELECT \* FROM "aaa_backup_large"/.test(c.query));
  expect(data).toHaveLength(1);expect(data[0].args).toEqual([1001]);expect(data[0].cursor.rowsRead).toBeLessThanOrEqual(1001);
  expect(h.rowBudget.status().stopped).toBe(false);
 }));
 it('refuses an unbounded offset before issuing a data slice',()=>fixture(async(h,_raw,state)=>{
  const {backup,calls}=service(h,state);await h.rowBudget.begin();
  const response=backup.handle(new Request('https://hub/api/backup/export?table=aaa_backup_large&offset=129&limit=128'),admin);
  expect(response.status).toBe(400);expect(await response.json()).toEqual({error:'invalid_range'});
  expect(calls.some(c=>/OFFSET/.test(c.query))).toBe(false);expect(reads(calls)).toBeLessThanOrEqual(3100);
 }));
});
