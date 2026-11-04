/** Trigger-maintained intake summary; bootstrap never counts or rewrites history. */
import {readSchemaObject} from './schema-object-locator';
import {schemaSqlEquivalent} from './schema-sql-equivalent';
import type { SqlStorage } from '@cloudflare/workers-types';
const columns = 'id,project,status,title,updated_at,created_at,archived_at,version';
const values = (p: string) => `${p}id,` + ['project','status','title','updated_at','created_at','archived_at','version']
  .map(k=>`json_extract(${p}data,'$.${k}')`).join(',');
const update = ['project','status','title','updated_at','created_at','archived_at','version']
  .map(k=>`${k}=excluded.${k}`).join(',');
const trigger = (op: 'INSERT'|'UPDATE'|'DELETE') => op === 'DELETE'
  ? 'CREATE TRIGGER IF NOT EXISTS intake_status_summary_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_status_summary WHERE id=OLD.id; END'
  : `CREATE TRIGGER IF NOT EXISTS intake_status_summary_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN INSERT INTO intake_status_summary(${columns}) VALUES(${values('NEW.')}) ON CONFLICT(id) DO UPDATE SET ${update}; END`;
export function initIntakeSummary(sql: SqlStorage) {
  const existed=sql.exec("SELECT name FROM pragma_table_list('intake_status_summary') WHERE schema='main' AND type='table'").toArray().length>0;
  sql.exec(`CREATE TABLE IF NOT EXISTS intake_status_summary (${columns.split(',').map((k,i)=>i===0?'id TEXT PRIMARY KEY':k).join(',')}) WITHOUT ROWID`);
  sql.exec('CREATE TABLE IF NOT EXISTS intake_summary_migration(id INTEGER PRIMARY KEY CHECK(id=1),after_id TEXT,phase INTEGER NOT NULL)');
  if(!existed)sql.exec('DELETE FROM intake_summary_migration WHERE id=1');
  const receipt=sql.exec('SELECT phase FROM intake_summary_migration WHERE id=1').toArray()[0];
  if(!receipt && !sql.exec('SELECT id FROM intake_tasks ORDER BY id LIMIT 1').toArray().length &&
      !sql.exec('SELECT id FROM intake_status_summary ORDER BY id LIMIT 1').toArray().length) {
    // A fresh empty source and empty projection are a complete observation,
    // proved with two indexed heads. Construction never backfills history.
    sql.exec('INSERT INTO intake_summary_migration VALUES(1,NULL,2)');
  } else if(!receipt) {
    // Only a pre-existing summary can inherit the old atomic-bootstrap
    // boundary. A newly created projection has no legacy proof to inspect:
    // retained source data leaves it pending, without any catalog/history scan.
    // Existing projections require the exact LIVE definitions of all guards;
    // deployment rowid hints locate objects but never prove their contents.
    const proven=existed && (['INSERT','UPDATE','DELETE'] as const).every(op=>{
      const actual=readSchemaObject(sql,{name:'intake_status_summary_'+op.toLowerCase(),type:'trigger',table:'intake_tasks'});
      return !!actual&&schemaSqlEquivalent(actual.sql,trigger(op));
    });
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
