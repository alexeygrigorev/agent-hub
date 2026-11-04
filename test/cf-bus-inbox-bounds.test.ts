import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { BusService } from '../src/bus';
const fixture=(fn:(raw:any,state:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('bus-inbox-'+crypto.randomUUID())),async(_hub:any,state:any)=>fn(state.storage.sql,state));
function observe(raw:any){const calls:{query:string;cursor:any}[]=[];return{calls,sql:new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}})};}
const hooks={resolveRecipient:(actor:string)=>actor,afterInsert:()=>{}};
function setup(raw:any,state:any){raw.exec("INSERT INTO agents(project,tag,role,machine_id) VALUES('inbox','reader','reviewer','host-a')");const o=observe(raw),bus=new BusService(o.sql,fn=>state.storage.transactionSync(fn),hooks);bus.prepareActorRoles(['inbox:reader']);o.calls.length=0;return{...o,bus};}
function message(raw:any,id:string,to:string,seq:number|null,unbound=0){raw.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,recipient_seq,registry_unbound_to)
 VALUES(?,'sender:external',?,'note','thread',?,?,1,9999999999999,?,?)`,id,to,id,id,seq,unbound);}
const sum=(calls:{cursor:any}[],field:'rowsRead'|'rowsWritten')=>calls.reduce((n,c)=>n+c.cursor[field],0);
describe('indexed bounded recipient inbox pages',()=>{
 it('makes fair progress across2000read envelopes without idle poll writes and repeats undelivered eligible work',()=>fixture((raw,state)=>{
  const {bus,calls}=setup(raw,state);
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
   INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,recipient_seq,read_at,delivered_at)
   SELECT printf('old-%04d',i),'sender:external','inbox:reader','note','thread','old',printf('old-key-%d',i),1,9999999999999,i,1,1 FROM n`);
  message(raw,'late-unread','inbox:reader',2001);
  const caller={kind:'agent' as const,actor:'inbox:reader',tokenId:'inbox-token'};
  bus.inboxPage(caller,'inbox:reader',50,undefined,{unread:true}); // reader-presence initialization
  let found:any;let emptyPages=0;
  for(let i=0;i<45;i++){
   calls.length=0;const page=bus.inboxPage(caller,'inbox:reader',50,undefined,{unread:true});
   expect(sum(calls,'rowsRead')).toBeLessThanOrEqual(250);
   if(page.rows.length){found=page;break;}
   emptyPages++;expect(page.truncated).toBe(true);expect(page.next).toBeTruthy();expect(sum(calls,'rowsWritten')).toBe(0);
  }
  expect(emptyPages).toBeGreaterThan(30);expect(found.rows.map((r:any)=>r.id)).toEqual(['late-unread']);
  calls.length=0;expect(bus.inboxPage(caller,'inbox:reader',50,undefined,{unread:true}).rows.map((r:any)=>r.id)).toEqual(['late-unread']);
  expect(sum(calls,'rowsWritten')).toBe(0);
  raw.exec("UPDATE messages SET read_at=2 WHERE id='late-unread'");
  bus.inboxPage(caller,'inbox:reader',50,undefined,{unread:true});calls.length=0;
  for(let i=0;i<5;i++)expect(bus.inboxPage(caller,'inbox:reader',50,undefined,{unread:true}).rows).toEqual([]);
  expect(sum(calls,'rowsWritten')).toBe(0);
 }));
 it('retains every unreturned eligible alias stream when a merged page fills',()=>fixture((raw,state)=>{
  const {bus}=setup(raw,state);
  message(raw,'legacy-null','inbox:reader',null);message(raw,'canonical-1','inbox:reader',1);message(raw,'canonical-3','inbox:reader',3);
  message(raw,'role-2','role:reviewer',2);message(raw,'role-4','role:reviewer',4);
  const seen:string[]=[];let cursor:string|undefined;
  for(let i=0;i<5;i++){
   const page=bus.inboxPage({actor:'inbox:reader'},'inbox:reader',2,cursor);
   seen.push(...page.rows.map((r:any)=>r.id));if(!page.next)break;cursor=page.next;
  }
  expect(seen).toEqual(['legacy-null','canonical-1','role-2','canonical-3','role-4']);
  expect(new Set(seen).size).toBe(seen.length);
 }));
 it('uses the deployed recipient/sequence index without a temporary sort for legacy and sequenced streams',()=>fixture((raw,state)=>{
  const {bus,calls}=setup(raw,state);message(raw,'legacy','inbox:reader',null);message(raw,'sequenced','inbox:reader',1);
  raw.exec('DROP INDEX IF EXISTS idx_messages_to_actor');
  bus.inboxPage({actor:'inbox:reader'},'inbox:reader',2);
  const queries=calls.filter(c=>/^SELECT rowid AS inbox_rowid,\* FROM messages/.test(c.query)).map(c=>c.query);
  expect(queries).toHaveLength(4);
  for(const query of queries){const plan=raw.exec('EXPLAIN QUERY PLAN '+query,'inbox:reader',0,3).toArray().map((r:any)=>r.detail).join('\n');expect(plan).toContain('messages_recipient_seq');expect(plan).not.toMatch(/TEMP B-TREE|SCAN messages/i);}
 }));
 it('keeps admin and recipient resume positions separate for legacy unbound envelopes',()=>fixture((raw,state)=>{
  const {bus}=setup(raw,state);message(raw,'unbound','inbox:reader',1,1);
  expect(bus.inboxPage({actor:'inbox:reader'},'inbox:reader',2).rows).toEqual([]);
  expect(bus.inboxPage({isAdmin:true},'inbox:reader',2).rows.map((r:any)=>r.id)).toEqual(['unbound']);
  expect(()=>bus.inboxPage({actor:'foreign:reader'},'inbox:reader',2)).toThrow('unauthorized');
 }));
});
