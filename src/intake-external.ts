/** External links are identity hints. Only the host's fresh census proves life. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { freshCatalogs } from './catalog-freshness';
import { nativeTerminalError } from './session_stall';
type Obj = Record<string, any>;

export function externalSession(sql: SqlStorage, link: Obj, now: number): Obj | null {
  if (!link.native_id) return null;
  const rows = (link.host
    ? sql.exec('SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?', link.host, link.native_id)
    : sql.exec('SELECT * FROM fleet_sessions WHERE native_id=?', link.native_id)).toArray() as Obj[];
  return externalSessionInCensus(link, rows, freshCatalogs(sql, now));
}

/** Watchdog uses its existing consistent census rather than querying per link. */
export function externalSessionInCensus(link: Obj, rows: Obj[], catalogs: Map<string, number>): Obj | null {
  const matches = rows.filter(n => n.native_id === link.native_id && (!link.host || n.machine_id === link.host));
  // Never guess between hosts or accept a short-id prefix collision.
  if (matches.length !== 1) return null;
  const row = matches[0];
  const fresh = catalogs.has(row.machine_id);
  const terminal = nativeTerminalError(row) || ['exited', 'stopped', 'failed'].includes(row.phase);
  return { ...row, live: fresh && !terminal && !['unknown', 'unobserved'].includes(row.phase),
    liveness: !fresh || ['unknown', 'unobserved'].includes(row.phase) ? 'unknown' : terminal ? 'terminal' : 'live' };
}
