import type { SqlStorage } from '@cloudflare/workers-types';

/** A session row's observed_at is its last change, not the last full census.
 * Separate bounded evidence lets unchanged live sessions remain observable.
 */
export function freshCatalogs(sql: SqlStorage, now: number): Map<string, number> {
  try {
  return new Map((sql.exec('SELECT machine_id,observed_at FROM fleet_catalog_seen WHERE observed_at>=? AND observed_at<=?',
    new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString()).toArray() as any[])
    .map(row => [String(row.machine_id), Date.parse(row.observed_at)]));
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
    return new Map((sql.exec('SELECT machine_id, observed_at FROM fleet_catalog_census WHERE complete = 1 AND observed_at >= ? AND observed_at <= ?',
      new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString()).toArray() as any[])
      .map(row => [String(row.machine_id), Date.parse(row.observed_at)]));
  } catch { return new Map(); }
}
