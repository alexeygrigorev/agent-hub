import type { SqlStorage } from '@cloudflare/workers-types';

const liveCatalogs=new WeakMap<object,Map<string,{at:number;complete:boolean;persistedAt:number}>>();
/** Verified host reports refresh memory immediately. Durable freshness is a
 * conservative restart fallback, not a per-minute heartbeat log. */
export function noteCatalogObservation(sql:SqlStorage,machine:string,now:number,complete:boolean):boolean {
  let hosts=liveCatalogs.get(sql as object);
  if(!hosts)liveCatalogs.set(sql as object,hosts=new Map());
  const old=hosts.get(machine);
  const persist=!old || old.complete!==complete || now-old.persistedAt>=900000;
  hosts.set(machine,{at:now,complete,persistedAt:persist?now:old.persistedAt});
  return persist;
}
export function invalidateCatalogObservations(sql:object):void { liveCatalogs.delete(sql); }
function overlay(sql:SqlStorage,rows:Map<string,number>,now:number,completeOnly:boolean):Map<string,number> {
  for(const [machine,live] of liveCatalogs.get(sql as object) ?? []) {
    rows.delete(machine);
    if(live.at>=now-180000 && live.at<=now+60000 && (!completeOnly || live.complete))rows.set(machine,live.at);
  }
  return rows;
}

/** A session row's observed_at is its last change, not the last full census.
 * Separate bounded evidence lets unchanged live sessions remain observable.
 */
export function freshCatalogs(sql: SqlStorage, now: number): Map<string, number> {
  try {
  return overlay(sql,new Map((sql.exec('SELECT machine_id,observed_at FROM fleet_catalog_seen WHERE observed_at>=? AND observed_at<=?',
    new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString()).toArray() as any[])
    .map(row => [String(row.machine_id), Date.parse(row.observed_at)])),now,false);
  } catch { return new Map(); }
}

/** Latest census freshness that is safe for capacity decisions.
 *
 * fleet_catalog_seen tracks any received report. This separate latest-result
 * row records whether that report covered every live or ambiguous native; an
 * incomplete newer census must invalidate an older complete observation.
 */
export function freshCompleteCatalogs(sql: SqlStorage, now: number): Map<string, number> {
  try {
    return overlay(sql,new Map((sql.exec('SELECT machine_id, observed_at FROM fleet_catalog_census WHERE complete = 1 AND observed_at >= ? AND observed_at <= ?',
      new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString()).toArray() as any[])
      .map(row => [String(row.machine_id), Date.parse(row.observed_at)])),now,true);
  } catch { return new Map(); }
}
