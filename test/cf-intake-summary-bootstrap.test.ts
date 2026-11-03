import { describe,it,expect } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { initIntakeSummary,advanceIntakeSummary } from '../src/intake-summary';
import { LivenessService } from '../src/liveness';
const fixture=(fn:(sql:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('summary-bounds-'+crypto.randomUUID())),async(_hub:any,state:any)=>fn(state.storage.sql));
function observe(raw:any){const calls:{query:string;cursor:any}[]=[];return{calls,sql:new Proxy(raw,{get(target,key){if(key==='exec')return(query:string,...args:any[])=>{const cursor=target.exec(query,...args);calls.push({query,cursor});return cursor;};const v=target[key];return typeof v==='function'?v.bind(target):v;}})};}
const reads=(calls:{cursor:any}[])=>calls.reduce((n,c)=>n+c.cursor.rowsRead,0);
const metadata=(call:{query:string})=>call.query==='SELECT name,type,sql FROM sqlite_master LIMIT 4097';
function boundedMetadata(calls:{query:string;cursor:any}[]){const probes=calls.filter(metadata);expect(probes).toHaveLength(1);expect(reads(probes)).toBeLessThanOrEqual(4097);return calls.filter(c=>!metadata(c));}
describe('retained intake and alert bootstrap uses deployed indexes',()=>{
 it('proves legacy trigger bootstrap without COUNT or rewriting 1270 summaries',()=>fixture(raw=>{
  initIntakeSummary(raw);while(!advanceIntakeSummary(raw)){}
  for(let i=0;i<1270;i++)raw.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)','legacy-summary-'+i,JSON.stringify({id:'legacy-summary-'+i,status:'done',title:'retained',history:[]}));
  raw.exec('DROP TABLE intake_summary_migration');
  const o=observe(raw);initIntakeSummary(o.sql);
  expect(o.calls.some(c=>/COUNT\(|INSERT INTO intake_status_summary\([^]*SELECT|DELETE FROM intake_status_summary WHERE id=\?/.test(c.query))).toBe(false);
  expect(raw.exec('SELECT phase FROM intake_summary_migration WHERE id=1').one().phase).toBe(2);
  expect(reads(boundedMetadata(o.calls))).toBeLessThan(100);
  o.calls.length=0;initIntakeSummary(o.sql);
  expect(o.calls.some(metadata)).toBe(false);
  expect(reads(o.calls)).toBeLessThan(100);
 }));
 it('repairs absent-trigger summaries with durable bounded pages and preserves concurrent writes',()=>fixture(raw=>{
  for(const op of ['insert','update','delete'])raw.exec('DROP TRIGGER IF EXISTS intake_status_summary_'+op);
  raw.exec('DROP TABLE intake_summary_migration');raw.exec('DELETE FROM intake_status_summary');
  for(let i=0;i<80;i++)raw.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)','page-summary-'+String(i).padStart(3,'0'),JSON.stringify({id:'page-summary-'+i,status:'done',history:[]}));
  raw.exec("INSERT INTO intake_status_summary(id,title) VALUES('orphan-summary','orphan')");
  const o=observe(raw);initIntakeSummary(o.sql);
  expect(o.calls.some(c=>/FROM intake_tasks/.test(c.query))).toBe(false);
  advanceIntakeSummary(o.sql);
  expect(reads(boundedMetadata(o.calls))).toBeLessThan(200);
  const repeat=observe(raw);initIntakeSummary(repeat.sql);
  expect(repeat.calls.some(metadata)).toBe(false); // pending receipt must not be promoted by its newly installed triggers
  expect(raw.exec('SELECT phase FROM intake_summary_migration WHERE id=1').one().phase).toBe(0);
  raw.exec("UPDATE intake_tasks SET data=json_set(data,'$.title','concurrent') WHERE id='page-summary-000'");
  let pages=0;while(!advanceIntakeSummary(raw)){expect(++pages).toBeLessThan(20);}
  expect(raw.exec("SELECT title FROM intake_status_summary WHERE id='page-summary-000'").one().title).toBe('concurrent');
  expect(raw.exec("SELECT id FROM intake_status_summary WHERE id='orphan-summary'").toArray()).toHaveLength(0);
  expect(raw.exec('SELECT COUNT(*) AS n FROM intake_status_summary').one().n).toBe(80);
 }));
 it('refuses an incomplete alert observation before retry and suppression filters can scan history',()=>fixture(raw=>{
  for(let i=0;i<1500;i++)raw.exec(`INSERT INTO liveness_alerts(subject_kind,subject,state,detail,opened_at) VALUES('agent',?,'stale','retained',?)`,'alert-bound-'+i,new Date().toISOString());
  const o=observe(raw),service=new LivenessService(o.sql);
  expect(()=>service.undelivered(3600)).toThrow('liveness_alert_observation_incomplete');
  expect(reads(o.calls)).toBeLessThanOrEqual(502);
  expect(o.calls).toHaveLength(1);
 }));
 it('keeps retry age and delivery suppression semantics on a complete bounded observation',()=>fixture(raw=>{
  const now=Date.now(),old=new Date(now-3600000).toISOString();
  for(const subject of ['suppressed','retry','never','recent','delivered'])raw.exec(`INSERT INTO liveness_alerts(subject_kind,subject,state,detail,opened_at,notified_at,notify_error) VALUES('agent',?,'stale','x',?,?,?)`,subject,subject==='recent'?new Date(now).toISOString():old,['retry','delivered'].includes(subject)?old:null,subject==='retry'?'failure':null);
  raw.exec("INSERT INTO alert_backfill_suppressed SELECT id,? FROM liveness_alerts WHERE subject='suppressed'",now);
  expect(new LivenessService(raw,undefined,()=>now).undelivered(300).map((r:any)=>r.subject)).toEqual(['retry','never']);
 }));
});
