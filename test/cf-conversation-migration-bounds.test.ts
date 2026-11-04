import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {ConversationsService} from '../src/conversations';
const fixture=(fn:(raw:any,state:any)=>void)=>(runInDurableObject as any)(
 (env as any).HUB.get((env as any).HUB.idFromName('conv-migration-bound-'+crypto.randomUUID())),(_h:any,state:any)=>fn(state.storage.sql,state));
describe('legacy conversation accounting migration bounds',()=>{
 for(const table of ['conversations','conversation_messages','conversation_members'])it('refuses large '+table+' before accounting repair writes',()=>fixture((raw,state)=>{
  const at=new Date().toISOString();
  raw.exec("INSERT INTO conversations(id,kind,title,created_by,created_at,updated_at) VALUES('base','group','base','human:test',?,?)",at,at);
  if(table==='conversations')raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO conversations(id,kind,title,created_by,created_at,updated_at) SELECT 'c-'||i,'group','fixture','human:test',?,? FROM n`,at,at);
  if(table==='conversation_messages')raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO conversation_messages(id,conversation_id,seq,sender_actor,sender_kind,kind,body,idempotency_key,request_json,created_at)
   SELECT 'm-'||i,'base',i,'human:test','human','note','body','key-'||i,'{}',? FROM n`,at);
  if(table==='conversation_members')raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO conversation_members(conversation_id,actor,kind,role,added_at,added_by)
   SELECT 'base','human:m-'||i,'human','member',?,'human:test' FROM n`,at);
  raw.exec('UPDATE conversation_storage_usage SET schema_version=1 WHERE singleton=1');
  const calls:{query:string;cursor:any}[]=[];
  const sql=new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
  const service=new ConversationsService(sql,fn=>state.storage.transactionSync(fn));
  expect(()=>service.init()).toThrow('conversation_migration_observation_limited');
  const probe=calls.find(call=>call.query===`SELECT 1 FROM ${table} LIMIT 129`);
  expect(probe).toBeDefined();expect(probe!.cursor.rowsRead).toBeLessThanOrEqual(129);
  expect(calls.some(call=>/^(DELETE FROM|UPDATE) (conversations|conversation_messages|conversation_members)\b/.test(call.query.trim()))).toBe(false);
  expect(raw.exec('SELECT schema_version FROM conversation_storage_usage WHERE singleton=1').one().schema_version).toBe(1);
 }));
});
