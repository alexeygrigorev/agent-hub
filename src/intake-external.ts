/** External links are identity hints. Only the host's fresh census proves life. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { freshCatalogs } from './catalog-freshness';
import { nativeTerminalError } from './session_stall';
type Obj = Record<string, any>;

export function externalSession(sql: SqlStorage, link: Obj, now: number): Obj | null {
  if (!link.native_id) return null;
  const rows = sql.exec('SELECT * FROM fleet_sessions WHERE native_id=?', link.native_id).toArray() as Obj[];
  const matches = rows.filter(n => !link.host || n.machine_id === link.host);
  // Never guess between hosts or accept a short-id prefix collision.
  if (matches.length !== 1) return null;
  const row = matches[0];
  const fresh = freshCatalogs(sql, now).has(row.machine_id);
  const terminal = nativeTerminalError(row) || ['exited', 'stopped', 'failed'].includes(row.phase);
  return { ...row, live: fresh && !terminal && !['unknown', 'unobserved'].includes(row.phase),
    liveness: !fresh || ['unknown', 'unobserved'].includes(row.phase) ? 'unknown' : terminal ? 'terminal' : 'live' };
}
