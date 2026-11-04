import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {ConversationsService} from '../src/conversations';
import {RowsMeter} from '../src/rows-meter';
const fixture=(fn:(raw:any,state:any)=>Promise<void>|void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('conversation-visibility-'+crypto.randomUUID())),(_hub:any,state:any)=>fn(state.storage.sql,state));
const auth={caller:{actor:'human:reader',kind:'human' as const,isAdmin:true},scopes:['bus:read','bus:send'],authType:'session' as const,csrfVerified:true};
function seed(raw:any,count:number){
 const at=new Date().toISOString();
 raw.exec("INSERT INTO conversations(id,kind,title,created_by,created_at,updated_at,last_seq) VALUES('conv','group','fixture','human:reader',?,?,?)",at,at,count);
 raw.exec("INSERT INTO conversation_members(conversation_id,actor,kind,role,added_at,added_by,last_read_seq) VALUES('conv','human:reader','human','owner',?,'human:reader',0)",at);
 if(count)raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
  INSERT INTO conversation_messages(id,conversation_id,seq,sender_actor,sender_kind,kind,body,idempotency_key,request_json,created_at)
  SELECT 'm-'||i,'conv',i,'human:writer','human','note','body','key-'||i,'{}',? FROM n`,count,at);
}
describe('bounded retained conversation visibility',()=>{
 it('refuses a large retained history with covering129 reads and rolls back read-watermark changes',()=>fixture(async(raw,state)=>{
  seed(raw,2100);const meter=new RowsMeter(),svc=new ConversationsService(meter.wrap(raw),fn=>state.storage.transactionSync(fn));
  expect(()=>meter.runSync('visibility',()=>(svc as any).getRetainedVisibility('conv',2100,0))).toThrow('bounded observation');
  expect(meter.snapshot().visibility.rows_read).toBeLessThan(140);
  const response=await svc.handle(new Request('https://hub.test/api/conversations/conv'),auth);
  expect(response.status).toBe(503);expect(await response.json()).toMatchObject({error:'conversation_visibility_observation_limited',observation_pending:true});
  expect(()=>svc.markRead(auth,'conv',{seq:2000})).toThrow('bounded observation');
  const withoutTx=new ConversationsService(meter.wrap(raw));
  expect(()=>withoutTx.markRead(auth,'conv',{seq:2000})).toThrow('bounded observation');
  expect(raw.exec("SELECT last_read_seq,last_read_at FROM conversation_members WHERE conversation_id='conv'").one()).toMatchObject({last_read_seq:0,last_read_at:null});
  const denied=await svc.handle(new Request('https://hub.test/api/conversations/conv'),{...auth,caller:{...auth.caller,actor:'human:foreign'}});
  expect(denied.status).toBe(403);
 }));
 it('preserves exact retained/unread counts across pruning holes and fully-read history',()=>fixture((raw,state)=>{
  seed(raw,12);raw.exec("DELETE FROM conversation_messages WHERE seq NOT IN(3,7,9)");
  const svc=new ConversationsService(raw,fn=>state.storage.transactionSync(fn));
  expect((svc as any).getRetainedVisibility('conv',12,7)).toEqual({oldestRetainedSeq:3,newestRetainedSeq:9,prunedThroughSeq:2,retainedCount:3,unreadCount:1});
  expect((svc as any).getRetainedVisibility('conv',12,12).unreadCount).toBe(0);
  raw.exec('DELETE FROM conversation_messages');
  expect((svc as any).getRetainedVisibility('conv',12,0)).toEqual({oldestRetainedSeq:null,newestRetainedSeq:null,prunedThroughSeq:12,retainedCount:0,unreadCount:0});
 }));
});
