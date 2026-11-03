/** Trigger-maintained intake summary; bootstrap never counts or rewrites history. */
import type { SqlStorage } from '@cloudflare/workers-types';
const columns = 'id,project,status,title,updated_at,created_at,archived_at,version';
const values = (p: string) => `${p}id,` + ['project','status','title','updated_at','created_at','archived_at','version']
  .map(k=>`json_extract(${p}data,'$.${k}')`).join(',');
const update = ['project','status','title','updated_at','created_at','archived_at','version']
  .map(k=>`${k}=excluded.${k}`).join(',');
const normalize = (sql: string) => sql.replace(/\s+/g,' ').trim().replace(/;$/,'');
const trigger = (op: 'INSERT'|'UPDATE'|'DELETE') => op === 'DELETE'
  ? 'CREATE TRIGGER IF NOT EXISTS intake_status_summary_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_status_summary WHERE id=OLD.id; END'
  : `CREATE TRIGGER IF NOT EXISTS intake_status_summary_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN INSERT INTO intake_status_summary(${columns}) VALUES(${values('NEW.')}) ON CONFLICT(id) DO UPDATE SET ${update}; END`;
export function initIntakeSummary(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS intake_status_summary (${columns.split(',').map((k,i)=>i===0?'id TEXT PRIMARY KEY':k).join(',')}) WITHOUT ROWID`);
  sql.exec('CREATE TABLE IF NOT EXISTS intake_summary_migration(id INTEGER PRIMARY KEY CHECK(id=1),after_id TEXT,phase INTEGER NOT NULL)');
  const receipt=sql.exec('SELECT phase FROM intake_summary_migration WHERE id=1').toArray()[0];
  if(!receipt && !sql.exec('SELECT id FROM intake_tasks ORDER BY id LIMIT 1').toArray().length &&
      !sql.exec('SELECT id FROM intake_status_summary ORDER BY id LIMIT 1').toArray().length) {
    // A fresh empty source and empty projection are a complete observation,
    // proved with two indexed heads. Construction never backfills history.
    sql.exec('INSERT INTO intake_summary_migration VALUES(1,NULL,2)');
  } else if(!receipt) {
    // The previous version committed full bootstrap and these exact triggers
    // atomically. Prove that legacy boundary ONCE; later cold starts use the
    // durable receipt instead of re-reading the entire SQLite catalog.
    // Bound the raw catalog before filtering names: WHERE name IN (...) has
    // no catalog index and does not make an unbounded metadata scan cheaper.
    const catalog=sql.exec('SELECT name,type,sql FROM sqlite_master LIMIT 4097').toArray() as {name:string;type:string;sql:string}[];
    if(catalog.length>4096)throw new Error('intake_summary_schema_observation_incomplete');
    const proven=['INSERT','UPDATE','DELETE'].every(op=>catalog.some(row=>row.type==='trigger' &&
      row.name==='intake_status_summary_'+op.toLowerCase() && normalize(row.sql)===normalize(trigger(op as any).replace('IF NOT EXISTS ',''))));
    sql.exec('INSERT INTO intake_summary_migration VALUES(1,NULL,?)',proven?2:0);
  }
  for(const op of ['INSERT','UPDATE','DELETE'] as const) sql.exec(trigger(op));
}
/** Explicit request only; caller wraps page+cursor in a transaction. Construction and alarms never advance. */
export function advanceIntakeSummary(sql: SqlStorage): boolean {
  const state = sql.exec('SELECT after_id,phase FROM intake_summary_migration WHERE id=1').one() as {after_id:string|null;phase:number};
  if(state.phase===2)return true;
  if(state.phase===0) {
    const rows=sql.exec('SELECT id FROM intake_tasks WHERE id>? ORDER BY id LIMIT 25',state.after_id??'').toArray() as {id:string}[];
    for(const row of rows)sql.exec(`INSERT INTO intake_status_summary(${columns}) SELECT ${values('')} FROM intake_tasks WHERE id=? ON CONFLICT(id) DO UPDATE SET ${update}`,row.id);
    sql.exec('UPDATE intake_summary_migration SET after_id=?,phase=? WHERE id=1',rows.length===25?rows.at(-1)!.id:null,rows.length===25?0:1);
    return false;
  }
  const rows=sql.exec('SELECT id FROM intake_status_summary WHERE id>? ORDER BY id LIMIT 25',state.after_id??'').toArray() as {id:string}[];
  for(const row of rows)sql.exec('DELETE FROM intake_status_summary WHERE id=? AND NOT EXISTS(SELECT 1 FROM intake_tasks WHERE id=?)',row.id,row.id);
  const done=rows.length<25;
  sql.exec('UPDATE intake_summary_migration SET after_id=?,phase=? WHERE id=1',rows.at(-1)?.id??state.after_id,done?2:1);
  return done;
}

/** Singleton readiness; never reads a retained source or advances its cursor. */
export function intakeSummaryReady(sql:SqlStorage):boolean {
  return Number((sql.exec('SELECT phase FROM intake_summary_migration WHERE id=1').toArray()[0] as any)?.phase)===2;
}
