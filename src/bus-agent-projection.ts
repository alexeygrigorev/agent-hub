/** Request-owned sparse identity subscriptions. Never backfill from alarms or
 * construction, and never infer authority from a partial or overflowed view. */
import { registryRowSql, registryAddress } from './agent_registry';
interface Sql { exec(query:string,...args:any[]):{toArray():any[]}; }
const PAGE=100,CAP=128;
export function initBusAgentProjection(sql:Sql):void {
  sql.exec(`CREATE TABLE IF NOT EXISTS bus_agent_subscriptions(key TEXT PRIMARY KEY,kind TEXT NOT NULL,value TEXT NOT NULL,
    after_rowid INTEGER NOT NULL DEFAULT 0,done INTEGER NOT NULL DEFAULT 0,overflow INTEGER NOT NULL DEFAULT 0,pending INTEGER NOT NULL DEFAULT 0)`);
  sql.exec('CREATE TABLE IF NOT EXISTS bus_agent_members(key TEXT NOT NULL,agent_rowid INTEGER NOT NULL,PRIMARY KEY(key,agent_rowid))');
  sql.exec(`CREATE TRIGGER IF NOT EXISTS bus_agent_member_capacity BEFORE INSERT ON bus_agent_members
    WHEN NOT EXISTS(SELECT 1 FROM bus_agent_members WHERE key=NEW.key AND agent_rowid=NEW.agent_rowid)
      AND (SELECT pending FROM bus_agent_subscriptions WHERE key=NEW.key)>=${CAP}
    BEGIN UPDATE bus_agent_subscriptions SET overflow=1 WHERE key=NEW.key; SELECT RAISE(IGNORE); END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS bus_agent_member_insert AFTER INSERT ON bus_agent_members BEGIN UPDATE bus_agent_subscriptions SET pending=pending+1 WHERE key=NEW.key; END');
  sql.exec('CREATE TRIGGER IF NOT EXISTS bus_agent_member_delete AFTER DELETE ON bus_agent_members BEGIN UPDATE bus_agent_subscriptions SET pending=pending-1 WHERE key=OLD.key; END');
  const remove=`DELETE FROM bus_agent_members WHERE key='role:'||OLD.role AND agent_rowid=OLD.rowid;
    DELETE FROM bus_agent_members WHERE key='host:'||OLD.machine_id AND agent_rowid=OLD.rowid;`;
  const add=`INSERT OR IGNORE INTO bus_agent_members SELECT key,NEW.rowid FROM bus_agent_subscriptions
    WHERE key='role:'||NEW.role AND ${registryRowSql('NEW.')};
    INSERT OR IGNORE INTO bus_agent_members SELECT key,NEW.rowid FROM bus_agent_subscriptions
    WHERE key='host:'||NEW.machine_id AND ${registryRowSql('NEW.')} AND COALESCE(NEW.role,'')!='root';`;
  sql.exec(`CREATE TRIGGER IF NOT EXISTS bus_agent_subscription_insert AFTER INSERT ON agents BEGIN ${add} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS bus_agent_subscription_update AFTER UPDATE OF tag,project,role,machine_id ON agents BEGIN ${remove} ${add} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS bus_agent_subscription_delete AFTER DELETE ON agents BEGIN ${remove} END`);
}
/** One source PK prefix per explicit request. Completion remains durable;
 * subsequent requests read only subscribed members and exact source PKs. */
export function subscribedBusAgents(sql:Sql,kind:'role'|'host',value:string,advance=true):any[]|null {
  const key=kind+':'+value;
  if(advance)sql.exec('INSERT OR IGNORE INTO bus_agent_subscriptions(key,kind,value) VALUES(?,?,?)',key,kind,value);
  let state=sql.exec('SELECT after_rowid,done,overflow FROM bus_agent_subscriptions WHERE key=?',key).toArray()[0];
  if(!state||state.overflow)return null;
  if(!state.done && advance) {
    const rows=sql.exec(`SELECT rowid AS source_rowid,tag,project,role,machine_id
      FROM agents WHERE rowid>? ORDER BY rowid LIMIT ${PAGE}`,state.after_rowid).toArray();
    for(const row of rows) if(registryAddress(row.project?`${row.project}:${row.tag}`:row.tag) && (kind==='role'?row.role===value:row.machine_id===value&&row.role!=='root'))
      sql.exec('INSERT OR IGNORE INTO bus_agent_members VALUES(?,?)',key,row.source_rowid);
    sql.exec('UPDATE bus_agent_subscriptions SET after_rowid=?,done=? WHERE key=?',rows.at(-1)?.source_rowid??state.after_rowid,rows.length<PAGE?1:0,key);
    state=sql.exec('SELECT done,overflow FROM bus_agent_subscriptions WHERE key=?',key).toArray()[0];
  }
  if(!state?.done||state.overflow)return null;
  const members=sql.exec('SELECT agent_rowid FROM bus_agent_members WHERE key=? ORDER BY agent_rowid LIMIT ?',key,kind==='role'?2:CAP+1).toArray();
  if(members.length>CAP)return null;
  const agents=[];
  for(const member of members) {
    const row=sql.exec('SELECT tag,project,role,machine_id FROM agents WHERE rowid=?',member.agent_rowid).toArray()[0];
    // Trigger integrity failures refuse the entire observation.
    if(!row || (kind==='role'?row.role!==value:row.machine_id!==value||row.role==='root'))return null;
    agents.push(row);
  }
  return agents;
}
