import type {SqlStorage} from '@cloudflare/workers-types';
type HealthWatch={stamp(...tables:string[]):string;trackAll(tables:Iterable<string>):void};
type HealthResult={ready:boolean;complete:boolean};
const healthMemo=new WeakMap<object,{watch:HealthWatch;stamp:string;result:HealthResult}>();
const HEALTH_TABLES=['intake_task_projection','intake_link_health','intake_link_health_meta'];
const MAX_LINK_BYTES=2*1024*1024;
const invalid=(value:string)=>`CASE WHEN json_valid(${value}) THEN CASE WHEN json_type(${value})='array'
 AND json_array_length(${value})<=128 AND length(CAST(${value} AS BLOB))<=${MAX_LINK_BYTES} THEN 0 ELSE 1 END ELSE 1 END`;
/** Construction may prove an empty source with one indexed row; retained
 * projections are checked only by bounded request pages, never hydrated here. */
export function initIntakeLinkHealth(sql:SqlStorage,registerTriggers=false):void {
 const tables=['intake_link_health','intake_link_health_meta'];
 const present=tables.every(name=>sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?",name).toArray().length>0);
 const ready=present&&sql.exec('SELECT id FROM intake_link_health_meta WHERE id=1').toArray().length>0;
 if(ready&&!registerTriggers)return;
 sql.exec('CREATE TABLE IF NOT EXISTS intake_link_health(id TEXT PRIMARY KEY)');
 sql.exec('CREATE TABLE IF NOT EXISTS intake_link_health_meta(id INTEGER PRIMARY KEY CHECK(id=1),after_id TEXT NOT NULL,complete INTEGER NOT NULL)');
 for(const op of ['INSERT','UPDATE OF links'])sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_link_health_${op.split(' ')[0].toLowerCase()}
 AFTER ${op} ON intake_task_projection BEGIN
 INSERT INTO intake_link_health(id) SELECT NEW.id WHERE ${invalid('NEW.links')} ON CONFLICT(id) DO NOTHING;
 DELETE FROM intake_link_health WHERE id=NEW.id AND NOT(${invalid('NEW.links')}); END`);
 sql.exec('CREATE TRIGGER IF NOT EXISTS intake_link_health_delete AFTER DELETE ON intake_task_projection BEGIN DELETE FROM intake_link_health WHERE id=OLD.id; END');
 if(!ready) {
  // Triggers already cover every subsequent source mutation. A newly empty
  // source is complete now; an existing incomplete marker is never promoted.
  const empty=sql.exec('SELECT 1 FROM intake_task_projection LIMIT 1').toArray().length===0;
  sql.exec("INSERT INTO intake_link_health_meta VALUES(1,'',?) ON CONFLICT(id) DO UPDATE SET after_id='',complete=excluded.complete",empty?1:0);
 }
}
/** One bounded request page. Concurrent/source writes maintain their own exact
 * health entry, including inserts lexically before the durable cursor. */
export function intakeLinkHealth(sql:SqlStorage,advance=true,watch?:HealthWatch):HealthResult {
 // A trusted watcher is registered only for this exact SQL handle; other
 // handles keep their ordinary SQL path. No incomplete page is memoized.
 const previous=healthMemo.get(sql as object),owner=watch??previous?.watch;
 if(owner) {
  owner.trackAll(HEALTH_TABLES);
  if(previous?.watch===owner&&previous.stamp===owner.stamp(...HEALTH_TABLES)&&previous.result.complete)return {...previous.result};
 }
 if(!sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_task_projection'").toArray().length)return {ready:false,complete:false};
 initIntakeLinkHealth(sql);
 const meta:any=sql.exec('SELECT after_id,complete FROM intake_link_health_meta WHERE id=1').toArray()[0];
 if(!meta)return {ready:false,complete:false};
 let complete=meta.complete===1;
 if(!complete&&advance) {
  const rows=sql.exec('SELECT id,links FROM intake_task_projection WHERE id>? ORDER BY id LIMIT 33',meta.after_id).toArray();
  for(const row of rows.slice(0,32)) {
   let links:any;try{links=JSON.parse(String(row.links));}catch{}
   const bad=!Array.isArray(links)||links.length>128||new TextEncoder().encode(String(row.links)).byteLength>MAX_LINK_BYTES;
   if(bad)sql.exec('INSERT INTO intake_link_health VALUES(?) ON CONFLICT DO NOTHING',row.id);
   else sql.exec('DELETE FROM intake_link_health WHERE id=?',row.id);
  }
  const batch=rows.slice(0,32);complete=rows.length<=32;
  sql.exec('UPDATE intake_link_health_meta SET after_id=?,complete=? WHERE id=1',batch.at(-1)?.id??meta.after_id,complete?1:0);
 }
 // Keep the source in memo dependencies even for component wrappers that
 // did not observe trigger creation. The unused zero-row CTE reads no source.
 const bad=sql.exec('WITH health_source AS (SELECT id FROM intake_task_projection LIMIT 0) SELECT id FROM intake_link_health ORDER BY id LIMIT 1').toArray().length>0;
 const result={ready:complete&&!bad,complete};
 // Incomplete pages must continue advancing on later explicit requests.
 // Complete-invalid proofs remain authoritative until an exact source write.
 if(owner&&complete)healthMemo.set(sql as object,{watch:owner,stamp:owner.stamp(...HEALTH_TABLES),result:{...result}});
 return result;
}
