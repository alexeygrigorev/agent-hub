import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {initSessionLinks,initLinkRefs,initProjectionLinkRefs} from '../src/worker-access';
import {linkedTaskIds} from '../src/intake-session';
const fixture=(fn:(raw:any)=>void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('link-seed-'+crypto.randomUUID())),(_h:any,state:any)=>fn(state.storage.sql));
const initializers=[['intake_session_links',initSessionLinks],['intake_link_refs',initLinkRefs],['intake_projection_link_refs',initProjectionLinkRefs]] as const;
describe('bounded intake link startup and deletion',()=>{
 for(const[table,init]of initializers)it('refuses incomplete source before creating '+table,()=>fixture(raw=>{
  raw.exec(`WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2100)
   INSERT INTO intake_task_projection(id,data,links) SELECT 'old-'||i,'{}','[]' FROM n`);
  raw.exec('DROP TABLE '+table);
  const calls:{query:string;cursor:any}[]=[];
  const sql=new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
  expect(()=>init(sql)).toThrow('intake_link_seed_observation_limited');
  const probe=calls.find(call=>call.query==='SELECT id FROM intake_task_projection ORDER BY id LIMIT 129');
  expect(probe).toBeDefined();expect(probe!.cursor.rowsRead).toBeLessThanOrEqual(129);
  expect(raw.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?",table).toArray()).toEqual([]);
 }));
 it('seeds complete small projections and retains harmless stale candidates without deletion census',()=>fixture(raw=>{
  raw.exec("INSERT INTO intake_task_projection VALUES('linked',?,?)",JSON.stringify({status:'open'}),JSON.stringify([{kind:'link',authority:'host_census_verified',hub_session_id:'known',native_id:'native'}]));
  for(const[table]of initializers)raw.exec('DROP TABLE '+table);
  initSessionLinks(raw);initLinkRefs(raw);
  expect(raw.exec("SELECT task_id FROM intake_session_links WHERE session_key='known'").toArray()).toEqual([{task_id:'linked'}]);
  const deletion=raw.exec("DELETE FROM intake_task_projection WHERE id='linked'");deletion.toArray();
  expect(deletion.rowsRead).toBeLessThan(20);
  expect(raw.exec("SELECT task_id FROM intake_projection_link_refs WHERE ref='known'").toArray()).toEqual([{task_id:'linked'}]);
  expect(linkedTaskIds(raw,['known'],()=>true,2)).toEqual([]);
 }));
 it('refuses a large authority-ledger delete before its unindexed task filter and preserves data',()=>fixture(raw=>{
  initSessionLinks(raw);initLinkRefs(raw);
  raw.exec("INSERT INTO intake_tasks(id,data) VALUES('protected',?)",JSON.stringify({id:'protected',project:'fixture',title:'protected',status:'open',version:1,created_at:Date.now(),updated_at:Date.now(),acceptance_criteria:'retain authority',history:[]}));
  raw.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000) INSERT INTO intake_session_links SELECT 'session-'||i,'protected','host_census_verified' FROM n");
  const probe=raw.exec('SELECT COUNT(*) AS n FROM(SELECT 1 FROM intake_session_links LIMIT 129)');expect(probe.one().n).toBe(129);expect(probe.rowsRead).toBeLessThanOrEqual(130);
  // A failed native exec exposes no cursor counters. The identical standalone
  // preflight above proves the source bound; no write-count claim is fabricated.
  expect(()=>raw.exec("DELETE FROM intake_tasks WHERE id='protected'")).toThrow('intake_link_delete_observation_limited');
  expect(raw.exec("SELECT id FROM intake_tasks WHERE id='protected'").toArray()).toEqual([{id:'protected'}]);
  expect(raw.exec('SELECT COUNT(*) AS n FROM intake_session_links').one().n).toBe(2000);
 }));
});
