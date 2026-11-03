import { jobLeaseNameSql } from './auth/job-credential';
/** Per-entity deadline intents: source writes address a known job/lease only.
 * Retained history is inspected once through bounded active index prefixes. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { expiryMsSql } from './jobs';
import type { BackgroundIntent } from './background-work';
function dueSql(j:string):string {
  const expiry=`COALESCE(${expiryMsSql('l.expires_at')},0)`;
  const claim=`CASE WHEN ${j}.status='claimed' AND ${j}.started_at IS NULL THEN COALESCE(${expiryMsSql('l.acquired_at')}+300000,${expiry}) ELSE ${expiry} END`;
  return `CAST(CASE WHEN ${j}.status='offered' THEN COALESCE(CASE WHEN ${j}.kind='root-run'
    AND (SELECT enabled FROM root_scheduling WHERE id=1)=0 THEN ${expiryMsSql(j+'.deadline_at')}
    ELSE ${expiryMsSql(j+'.offer_expires_at')} END,${expiryMsSql(j+'.created_at')},0)
    ELSE (SELECT MIN(${expiry},${claim}) FROM leases l WHERE l.name=${jobLeaseNameSql(j)}
      AND l.holder_job_id=${j}.id AND l.epoch=${j}.lease_epoch) END AS INTEGER)`;
}
function payloadSql(j:string):string {
  return `json_object('entity_id',${j}.id,'source_generation',json_array(${j}.status,${j}.lease_epoch,${j}.started_at,${j}.role_name,
    CASE WHEN ${j}.kind='root-run' THEN (SELECT enabled FROM root_scheduling WHERE id=1) END))`;
}
function refreshJob(id:string):string {
  const due=dueSql('j');
  return `DELETE FROM hub_background_work WHERE id='job-deadline:'||${id}
      AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=${id} AND j.status IN ('offered','claimed','running') AND (${due}) IS NOT NULL);
    INSERT INTO hub_background_work(id,kind,due_ms,payload,source_due_ms)
      SELECT 'job-deadline:'||j.id,'job-deadline',${due},${payloadSql('j')},${due} FROM jobs j
      WHERE j.id=${id} AND j.status IN ('offered','claimed','running') AND (${due}) IS NOT NULL
      ON CONFLICT(id) DO UPDATE SET due_ms=excluded.due_ms,source_due_ms=excluded.source_due_ms,payload=excluded.payload,token=NULL,attempts=0
      WHERE hub_background_work.token IS NULL AND (hub_background_work.source_due_ms IS NOT excluded.source_due_ms OR hub_background_work.payload IS NOT excluded.payload);`;
}
export function initJobBackgroundProducers(sql:SqlStorage,enqueue:(intent:BackgroundIntent)=>unknown):void {
  sql.exec('CREATE TABLE IF NOT EXISTS job_background_seed(id INTEGER PRIMARY KEY CHECK(id=1),done INTEGER NOT NULL)');
  for(const op of ['INSERT','UPDATE OF status,offer_expires_at,deadline_at,started_at,role_name,lease_epoch'])
    sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_job_${op==='INSERT'?'insert':'update'} AFTER ${op} ON jobs BEGIN ${refreshJob('NEW.id')} END`);
  sql.exec("CREATE TRIGGER IF NOT EXISTS hub_background_job_delete AFTER DELETE ON jobs BEGIN DELETE FROM hub_background_work WHERE id='job-deadline:'||OLD.id; END");
  sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_lease_insert AFTER INSERT ON leases BEGIN ${refreshJob('NEW.holder_job_id')} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_lease_update AFTER UPDATE OF holder_job_id,epoch,expires_at,acquired_at ON leases BEGIN
    ${refreshJob('OLD.holder_job_id')} ${refreshJob('NEW.holder_job_id')} END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_lease_delete AFTER DELETE ON leases BEGIN ${refreshJob('OLD.holder_job_id')} END`);
  // Flag updates are explicit user mutations. The active population was
  // proven <=128 by restoration; LIMIT129 still bounds corrupt legacy data.
  sql.exec(`CREATE TRIGGER IF NOT EXISTS hub_background_job_root_flag AFTER UPDATE OF enabled ON root_scheduling BEGIN
    INSERT INTO hub_background_work(id,kind,due_ms,payload,source_due_ms)
      SELECT 'job-deadline:'||j.id,'job-deadline',${dueSql('j')},${payloadSql('j')},${dueSql('j')}
      FROM jobs j INDEXED BY jobs_root_offered_due_ms WHERE j.status='offered' AND j.kind='root-run'
      ORDER BY ${expiryMsSql('j.offer_expires_at')} LIMIT 129
      ON CONFLICT(id) DO UPDATE SET due_ms=excluded.due_ms,source_due_ms=excluded.source_due_ms,payload=excluded.payload,token=NULL,attempts=0 WHERE hub_background_work.token IS NULL AND (hub_background_work.source_due_ms IS NOT excluded.source_due_ms OR hub_background_work.payload IS NOT excluded.payload); END`);
  if(sql.exec('SELECT done FROM job_background_seed WHERE id=1').toArray()[0]?.done)return;
  const ids=new Set<string>();
  const offers=sql.exec("SELECT id FROM jobs INDEXED BY jobs_offered WHERE status='offered' ORDER BY offer_expires_at LIMIT 129").toArray();
  const leases=sql.exec('SELECT holder_job_id FROM leases INDEXED BY leases_active_name WHERE holder_job_id IS NOT NULL ORDER BY name LIMIT 129').toArray();
  if(offers.length>128||leases.length>128)throw new Error('background_job_observation_limit');
  for(const row of offers)ids.add(String(row.id));for(const row of leases)ids.add(String(row.holder_job_id));
  if(ids.size>128)throw new Error('background_job_observation_limit');
  const intents:BackgroundIntent[]=[];
  for(const id of ids) {
    const row=sql.exec(`SELECT ${dueSql('j')} AS due,${payloadSql('j')} AS payload FROM jobs j WHERE j.id=? AND j.status IN ('offered','claimed','running')`,id).toArray()[0];
    if(typeof row?.due==='number')intents.push({id:'job-deadline:'+id,kind:'job-deadline',dueAt:Math.max(0,row.due),payload:JSON.parse(String(row.payload))});
  }
  // enqueue's O(1) counter rejects incomplete restoration atomically.
  for(const intent of intents)enqueue(intent);
  sql.exec('INSERT OR REPLACE INTO job_background_seed VALUES(1,1)');
}
