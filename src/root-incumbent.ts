/** Registered roots remain authoritative without an alarm-time agents census.
 * All retained-source backfill runs in bounded request-owned pages. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { registryNameSql } from './agent_registry';
import { effectiveHost } from './registry';
const candidate = (p:string) => `(${p}role='root' OR (COALESCE(${p}project,'')='' AND ${p}tag IN ('role:root','root')))
  AND (${p}state IS NULL OR ${p}state!='retired') AND (${p}health_state IS NULL OR ${p}health_state!='retired')
  AND (COALESCE(${p}project,'')='' OR ${registryNameSql(p+'project')})
  AND ${p}tag NOT LIKE '%win35%' AND ${p}tag NOT LIKE '%retired%'`;
export function initRootIncumbentProjection(sql:SqlStorage):void {
  sql.exec('CREATE TABLE IF NOT EXISTS root_incumbent_candidates(agent_rowid INTEGER PRIMARY KEY,blocking INTEGER NOT NULL CHECK(blocking IN(0,1)))');
  sql.exec('CREATE INDEX IF NOT EXISTS root_incumbent_blocking ON root_incumbent_candidates(blocking,agent_rowid)');
  sql.exec('CREATE TABLE IF NOT EXISTS root_incumbent_migration(id INTEGER PRIMARY KEY CHECK(id=1),after_rowid INTEGER NOT NULL,done INTEGER NOT NULL)');
  sql.exec('INSERT OR IGNORE INTO root_incumbent_migration VALUES(1,0,0)');
  sql.exec(`CREATE TRIGGER IF NOT EXISTS root_incumbent_insert AFTER INSERT ON agents WHEN ${candidate('NEW.')} BEGIN
    INSERT INTO root_incumbent_candidates VALUES(NEW.rowid,1) ON CONFLICT(agent_rowid) DO UPDATE SET blocking=1; END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS root_incumbent_update AFTER UPDATE ON agents BEGIN
    DELETE FROM root_incumbent_candidates WHERE agent_rowid=OLD.rowid;
    INSERT INTO root_incumbent_candidates SELECT NEW.rowid,1 WHERE ${candidate('NEW.')}; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS root_incumbent_delete AFTER DELETE ON agents BEGIN DELETE FROM root_incumbent_candidates WHERE agent_rowid=OLD.rowid; END');
}
/** Call only from an explicit request, inside a transaction. Never from construction or alarms. */
export function advanceRootIncumbentProjection(sql:SqlStorage):boolean {
  const state=sql.exec('SELECT after_rowid,done FROM root_incumbent_migration WHERE id=1').one() as any;
  if(state.done)return true;
  const rows=sql.exec(`SELECT rowid AS id,CASE WHEN ${candidate('')} THEN 1 ELSE 0 END AS potential
    FROM agents WHERE rowid>? ORDER BY rowid LIMIT 100`,state.after_rowid).toArray() as any[];
  for(const row of rows) if(row.potential)sql.exec('INSERT OR IGNORE INTO root_incumbent_candidates VALUES(?,1)',row.id);
  const done=rows.length<100;
  sql.exec('UPDATE root_incumbent_migration SET after_rowid=?,done=? WHERE id=1',rows.at(-1)?.id??state.after_rowid,done?1:0);
  return done;
}
const dead=new Set(['retired','dead','defunct','failed','stale','stopped','offline','terminal','broken','error','expired','fenced']);
function timestamp(value:unknown):number {
  if(typeof value==='number'||typeof value==='string'&&/^\d+(?:\.\d+)?$/.test(value)) {
    const n=Number(value);return n<1e12?n*1000:n;
  }
  return typeof value==='string'?Date.parse(value):NaN;
}
/** Background-safe: singleton metadata, one indexed head and exact PK reads.
 * null means no complete negative observation yet; callers MUST refuse launch.
 * Clearing a confirmed-dead head returns null, never a false empty verdict
 * while another registered root could remain behind it. */
export function externalRootBlocks(sql:SqlStorage,now:number):boolean|null {
  const state=sql.exec('SELECT done FROM root_incumbent_migration WHERE id=1').toArray()[0] as any;
  if(!state?.done)return null;
  const head=sql.exec('SELECT agent_rowid FROM root_incumbent_candidates INDEXED BY root_incumbent_blocking WHERE blocking=1 ORDER BY agent_rowid LIMIT 1').toArray()[0] as any;
  if(!head)return false;
  const agent=sql.exec('SELECT state,health_state,last_seen_at,health_at,machine_id FROM agents WHERE rowid=?',head.agent_rowid).toArray()[0] as any;
  if(!agent)return true; // An inconsistent projection is never evidence of death.
  const stateName=String(agent.state??'').toLowerCase(),health=String(agent.health_state??'').toLowerCase();
  if(stateName==='unknown'||health==='unknown')return true;
  const explicitDead=(!!stateName&&dead.has(stateName)&&(!health||dead.has(health)))||(!stateName&&!!health&&dead.has(health));
  if(!explicitDead)return true;
  const seen=timestamp(agent.last_seen_at||agent.health_at);
  const host=agent.machine_id?effectiveHost(sql,sql.exec('SELECT id,last_checkin_at,last_heartbeat FROM machines WHERE id=?',agent.machine_id).toArray()[0] as any):null;
  const checkin=timestamp(host?.last_checkin_at);
  if(!Number.isFinite(seen)||seen>now-180000||!Number.isFinite(checkin)||checkin>now)return true;
  sql.exec('UPDATE root_incumbent_candidates SET blocking=0 WHERE agent_rowid=?',head.agent_rowid);
  return null;
}
