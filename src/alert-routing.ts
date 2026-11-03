import type { SqlStorage } from '@cloudflare/workers-types';
import { registryAddress } from './agent_registry';
import { subscribedBusAgents } from './bus-agent-projection';
import { jobCredentialAllowed } from './auth/job-credential';
type ReaderSql={exec(query:string,...args:any[]):{toArray():any[]}};
const READER_CHECKPOINT_MS=60*60*1000;
const READER_LIMIT=128;
type LiveReader={recipient:string;credentialId:string;readAt:number;persistedAt:number};
const liveReaders=new WeakMap<object,Map<string,LiveReader>>();
export function invalidateReaderObservations(sql:object):void { liveReaders.delete(sql); }
function readerIdentity(sql:ReaderSql,actor:string):{actor:string;agent:any;unknown:boolean} {
  let rows:any[]=[];
  if(actor.startsWith('role:')) {
    const role=actor.slice(5);if(/^root/i.test(role))return{actor,agent:null,unknown:true};
    const ready=subscribedBusAgents(sql,'role',role,false);
    if(ready===null)return{actor,agent:null,unknown:true};rows=ready;
  } else {
    const address=registryAddress(actor);
    if(!address)return{actor,agent:null,unknown:false};
    rows=address.project
      ?sql.exec("SELECT tag,project,role,machine_id FROM agents INDEXED BY idx_agents_project_tag_unique WHERE COALESCE(project,'')=? AND tag=? LIMIT 2",address.project,address.tag).toArray()
      :sql.exec('SELECT tag,project,role,machine_id FROM agents WHERE tag=? LIMIT 2',address.tag).toArray();
  }
  if(rows.length!==1)return{actor,agent:null,unknown:rows.length>1||actor.startsWith('role:')};
  const row=rows[0],canonical=row.project?`${row.project}:${row.tag}`:row.tag;
  if(!registryAddress(canonical))return{actor,agent:null,unknown:true};
  return{actor:canonical,agent:row,unknown:false};
}
export function readerActor(sql:SqlStorage,actor:string):string { return readerIdentity(sql,actor).actor; }

/** Persist inside the inbox transaction; publish live proof only through the
 * returned callback AFTER that transaction commits successfully. */
export function prepareReaderObservation(sql:ReaderSql,recipient:string,credentialId:string,now:number):()=>void {
  const key=JSON.stringify([recipient,credentialId]);
  const prior=liveReaders.get(sql)?.get(key);
  let persistedAt=prior?.persistedAt;
  if(persistedAt===undefined)persistedAt=Number(sql.exec('SELECT last_read_at FROM bus_reader_activity WHERE recipient=? AND credential_id=?',recipient,credentialId).toArray()[0]?.last_read_at);
  if(!Number.isFinite(persistedAt)||now-persistedAt!>=READER_CHECKPOINT_MS) {
    sql.exec(`INSERT INTO bus_reader_activity(recipient,credential_id,last_read_at) VALUES(?,?,?)
      ON CONFLICT(recipient,credential_id) DO UPDATE SET last_read_at=excluded.last_read_at
      WHERE bus_reader_activity.last_read_at<=excluded.last_read_at-?`,recipient,credentialId,now,READER_CHECKPOINT_MS);
    persistedAt=now;
  }
  const checkpoint=persistedAt!;
  return()=>{
    let readers=liveReaders.get(sql);if(!readers){readers=new Map();liveReaders.set(sql,readers);}
    if(readers.size>=512&&!readers.has(key))readers.delete(readers.keys().next().value!);
    readers.set(key,{recipient,credentialId,readAt:now,persistedAt:checkpoint});
  };
}

// A credential is not evidence of a reader. Only successful recipient/host
// inbox polls count; operator inspections never renew this lease.
export const READER_FRESH_MS = 15 * 60 * 1000;
export const BUS_REFRESH_GRACE_MS = 2 * 60 * 1000;
export const ALERT_SUBJECT_COOLDOWN_MS = 24 * 60 * 60 * 1000;
export const DEFAULT_HUB_ALERT_WEBHOOK = 'https://dapier.dtcdev.click/hooks/webhook/dev';
export const ALERT_ROUTING_SCHEMA = `
CREATE TABLE IF NOT EXISTS bus_auth_pending (subject TEXT PRIMARY KEY, credential_id TEXT NOT NULL, first_failed_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS bus_auth_pending_due ON bus_auth_pending(first_failed_at);
CREATE TABLE IF NOT EXISTS bus_reader_activity (recipient TEXT NOT NULL, credential_id TEXT NOT NULL, last_read_at INTEGER NOT NULL, PRIMARY KEY(recipient, credential_id));
CREATE INDEX IF NOT EXISTS bus_reader_activity_age ON bus_reader_activity(last_read_at);
CREATE TABLE IF NOT EXISTS alert_delivery_receipts (subject_kind TEXT NOT NULL, subject TEXT NOT NULL, target TEXT NOT NULL, alert_id INTEGER NOT NULL, sent_at INTEGER NOT NULL, PRIMARY KEY(subject_kind, subject, target));
CREATE INDEX IF NOT EXISTS alert_delivery_receipts_age ON alert_delivery_receipts(sent_at);
`;
export function initAlertRouting(sql: { exec(query: string, ...values: any[]): unknown }) {
  for (const stmt of ALERT_ROUTING_SCHEMA.split(';')) if (stmt.trim()) sql.exec(stmt);
}

export function inboxReaderStatus(sql:SqlStorage,actor:string,now=Date.now()) {
  const identity=readerIdentity(sql,actor);actor=identity.actor;
  const result=(active:boolean,pending=false)=>({actor,active_reader:active,observation_pending:pending,
    reason:pending?'reader_observation_incomplete':active?'active_read_loop':'no_active_credential_read_loop'});
  if(identity.unknown)return result(false,true);
  if(typeof identity.agent?.role==='string' && /^root/i.test(identity.agent.role))return result(false);
  // Select the raw PK prefix before freshness/revocation filters: retained
  // stale readers must never turn a LIMIT into a historical table walk.
  const history=sql.exec('SELECT credential_id,last_read_at FROM bus_reader_activity WHERE recipient=? ORDER BY credential_id LIMIT 129',actor).toArray() as any[];
  if(history.length>READER_LIMIT)return result(false,true);
  const observations=new Map<string,number>(history.map(row=>[row.credential_id,row.last_read_at]));
  for(const live of liveReaders.get(sql)?.values()??[])if(live.recipient===actor)
    observations.set(live.credentialId,Math.max(observations.get(live.credentialId)??0,live.readAt));
  if(observations.size>READER_LIMIT)return result(false,true);
  for(const [id,at] of observations) {
    if(!Number.isFinite(at)||at<now-READER_FRESH_MS||at>now)continue;
    const c:any=sql.exec('SELECT * FROM credentials WHERE id=?',id).toArray()[0];
    if(!c||c.revoked_at!=null||!(Date.parse(c.expires_at)>now))continue;
    let scopes:any;try{scopes=JSON.parse(c.scopes);}catch{continue;}
    if(!Array.isArray(scopes)||!scopes.some(s=>['admin','bus:read','msg:read','host'].includes(s)))continue;
    if(c.family_id&&sql.exec('SELECT enabled FROM native_credential_authority WHERE family_id=?',c.family_id).toArray()[0]?.enabled===0)continue;
    if(!jobCredentialAllowed(sql,c,now))continue;
    if(c.kind==='agent') {
      const bound=typeof c.registry_actor==='string'?registryAddress(c.registry_actor):null;
      if(!bound||c.registry_actor!==actor||!identity.agent||(identity.agent.machine_id??null)!==(c.machine_id??null))continue;
      if(c.machine_id!=null&&!sql.exec('SELECT id FROM machines WHERE id=? AND registered_at IS NOT NULL',c.machine_id).toArray().length)continue;
      return result(true);
    }
    if(c.kind==='host') {
      if(!c.machine_id||!sql.exec('SELECT id FROM machines WHERE id=? AND registered_at IS NOT NULL',c.machine_id).toArray().length)continue;
      if(identity.agent?.machine_id===c.machine_id || actor==='machine:'+c.machine_id)return result(true);
    } else if(c.actor===actor)return result(true);
  }
  return result(false);
}

export function alertDestinations(env: { HUB_ALERT_TO?: string; HUB_ALERT_WEBHOOK?: string }) {
  const targets = [...new Set((env.HUB_ALERT_TO || '').split(',').map(t => t.trim()).filter(Boolean))];
  const actors = targets.filter(t => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(t));
  const invalidActors = actors.length !== targets.length;
  let webhook: string | null = null;
  let invalidWebhook = false;
  const webhookValue = env.HUB_ALERT_WEBHOOK === undefined ? DEFAULT_HUB_ALERT_WEBHOOK : env.HUB_ALERT_WEBHOOK;
  let webhookAuthRequired = false;
  if (webhookValue) {
    try {
      const url = new URL(webhookValue);
      if (url.protocol !== 'https:' || url.username || url.password || url.hash) invalidWebhook = true;
      else {
        webhook = url.toString();
        webhookAuthRequired = url.hostname === 'dapier.dtcdev.click' && url.pathname === '/hooks/webhook/dev';
      }
    } catch { invalidWebhook = true; }
  }
  return { actors, webhook, invalidWebhook, invalidActors, webhookAuthRequired };
}

/** Prevent a destination cutover from replaying an unbounded old alert queue. */
export function suppressOpenAlertBackfill(sql: SqlStorage, now = Date.now()): number {
  const count = Number(sql.exec(`SELECT COUNT(*) AS n FROM liveness_alerts a
    WHERE a.resolved_at IS NULL AND NOT EXISTS (SELECT 1 FROM alert_backfill_suppressed s WHERE s.alert_id=a.id)`).one().n);
  if (count) sql.exec(`INSERT OR IGNORE INTO alert_backfill_suppressed(alert_id, suppressed_at)
    SELECT id, ? FROM liveness_alerts WHERE resolved_at IS NULL`, now);
  return count;
}

export function pruneAlertRouting(sql: SqlStorage, now = Date.now()) {
  sql.exec('DELETE FROM bus_reader_activity WHERE last_read_at < ?', now - READER_FRESH_MS);
  sql.exec('DELETE FROM alert_delivery_receipts WHERE sent_at < ?', now - ALERT_SUBJECT_COOLDOWN_MS);
  sql.exec(`DELETE FROM alert_backfill_suppressed WHERE NOT EXISTS (
    SELECT 1 FROM liveness_alerts a WHERE a.id=alert_backfill_suppressed.alert_id AND a.resolved_at IS NULL)`);
}
