import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { BusService } from '../src/bus';
const fixture=(fn:(raw:any,state:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('bus-history-'+crypto.randomUUID())),async(_hub:any,state:any)=>fn(state.storage.sql,state));
function observe(raw:any){const calls:{query:string;cursor:any}[]=[];return{calls,sql:new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}})};}
function agents(raw:any){for(const [project,tag,host,role] of [['local','reader','host-a','worker'],['foreign','reader','host-b','worker'],['local','appointed','host-a','root']])raw.exec('INSERT INTO agents(project,tag,machine_id,role) VALUES(?,?,?,?)',project,tag,host,role);}
function message(raw:any,id:string,from:string,to:string,flags:[number,number]=[0,0]){
 raw.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,registry_unbound_from,registry_unbound_to)
 VALUES(?,?,?,'note','t',?,?,1,9999999999999,?,?)`,id,from,to,id,id,...flags);
}
const reads=(calls:{cursor:any}[])=>calls.reduce((n,c)=>n+c.cursor.rowsRead,0);
describe('bus history bounds and participant authority',()=>{
 it('pages20k retained source rows before filtering and retains a cursor on an empty scoped page',()=>fixture((raw,state)=>{
  agents(raw);
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<20000)
   INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
   SELECT printf('m%05d',i),'foreign:reader','foreign:recipient','note','t','foreign',printf('retained-%d',i),i,9999999999999 FROM n`);
  message(raw,'m19899local','foreign:reader','local:reader');
  const o=observe(raw),bus=new BusService(o.sql,fn=>state.storage.transactionSync(fn));o.calls.length=0;
  const caller={kind:'agent' as const,actor:'local:reader',machineId:'host-a'};
  const first=bus.getBusMessagesPage(caller);
  expect(first.rows).toEqual([]);expect(first.truncated).toBe(true);expect(first.next).toBeTruthy();expect(first.scanned).toBe(100);
  expect(reads(o.calls)).toBeLessThanOrEqual(250);
  expect(o.calls.filter(c=>/FROM messages/.test(c.query)).every(c=>/ORDER BY id DESC LIMIT \?/.test(c.query))).toBe(true);
  o.calls.length=0;const second=bus.getBusMessagesPage(caller,undefined,first.next);
  expect(second.rows.map((r:any)=>r.id)).toEqual(['m19899local']);
  expect(second.next! < first.next!).toBe(true);expect(reads(o.calls)).toBeLessThanOrEqual(250);
 }));
 it('restricts host history to its registered participants and honors unbound and root fences',()=>fixture((raw,state)=>{
  agents(raw);
  message(raw,'a-local-in','foreign:reader','local:reader');message(raw,'b-local-out','local:reader','foreign:reader');
  message(raw,'c-foreign','foreign:reader','foreign:recipient');message(raw,'d-machine','foreign:reader','machine:host-a');
  message(raw,'e-foreign-machine','foreign:reader','machine:host-b');message(raw,'f-root','role:root','local:reader');
  message(raw,'g-unbound','foreign:reader','local:reader',[0,1]);message(raw,'h-root-agent','foreign:reader','local:appointed');
  const bus=new BusService(raw,fn=>state.storage.transactionSync(fn));
  let page:any;for(let i=0;i<3;i++){try{page=bus.getBusMessagesPage({kind:'host',machineId:'host-a'});break;}catch(error:any){if(error.message!=='bus_registry_projection_incomplete')throw error;}}
  expect(page.rows.map((r:any)=>r.id)).toEqual(['d-machine','b-local-out','a-local-in']);
  expect(()=>bus.getBusMessagesPage({kind:'host',machineId:'host-a'},'foreign:reader')).toThrow('unauthorized');
  expect(()=>bus.getBusMessagesPage({kind:'host',machineId:'host-a',isRevoked:true})).toThrow('unauthorized');
  expect(()=>bus.getBusMessagesPage({isAdmin:true},'role:root')).toThrow('missing_m4_root_authority');
 }));
 it('preserves the array compatibility method while exposing honest source truncation separately',()=>fixture((raw,state)=>{
  agents(raw);message(raw,'a','foreign:reader','local:reader');
  const bus=new BusService(raw,fn=>state.storage.transactionSync(fn));
  const caller={actor:'local:reader'};const page=bus.getBusMessagesPage(caller);
  expect(page.truncated).toBe(false);expect(page.next).toBeNull();expect(page.order).toBe('id_desc');
  expect(bus.getBusMessages(caller)).toEqual(page.rows);
  expect(()=>bus.getBusMessagesPage(caller,undefined,'bad\nvalue')).toThrow('invalid_cursor');
 }));
});
