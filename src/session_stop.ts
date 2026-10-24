/** Stop delivery is bounded independently of the native execution lease. */
export const STOP_DELIVERY_MS = 8000;

export function expireUnclaimedStops(sql: SqlStorage, now: number, sessionId?: string, machineId?: string): void {
  const at = new Date(now).toISOString();
  const cutoff = new Date(now - STOP_DELIVERY_MS).toISOString();
  sql.exec(`UPDATE session_control_commands SET state = 'failed', ack_json = ?, updated_at = ?
    WHERE kind = 'stop' AND state IN ('recorded','transported') AND claim_token IS NULL
    AND created_at <= ? ${sessionId ? 'AND session_id = ?' : ''}
    ${machineId ? 'AND session_id IN (SELECT id FROM session_control_sessions WHERE machine_id = ?)' : ''}`,
    JSON.stringify({outcome:'failed', reason:'stop_host_unreachable'}), at, cutoff,
    ...(sessionId ? [sessionId] : []), ...(machineId ? [machineId] : []));
}

/** A current host observation can renew custody for the SAME adopted tuple.
 * It cannot rebind another incarnation or transfer a Hub-launched session. */
export function refreshObservedHostFamily(sql: SqlStorage, machineId: string, existing: any,
  observed: {native_id: string; tag: string; workspace: string; created_at_ms?: number},
  family: string | null | undefined): void {
  if (!family || existing.machine_id !== machineId || existing.fleet_launch_id) return;
  let binding: any;
  try { binding = JSON.parse(existing.binding_json); } catch { return; }
  if (binding?.provenance !== 'observed_native' || binding.native_id !== observed.native_id ||
      binding.tag !== observed.tag || binding.workspace !== observed.workspace ||
      binding.native_created_at_ms !== observed.created_at_ms) return;
  const machine: any = sql.exec(`SELECT c.family_id FROM machines m JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?`, machineId).toArray()[0];
  if (machine?.family_id !== family) return;
  sql.exec('UPDATE session_control_sessions SET host_family = ? WHERE id = ?', family, existing.id);
}

export function visibleSessionPrefixMatches(sql: SqlStorage, prefix: string, owns: (row:any)=>boolean): any[] {
  if (!/^[a-f0-9][a-f0-9-]{7,34}$/i.test(prefix)) return [];
  return (sql.exec('SELECT * FROM session_control_sessions WHERE id LIKE ? ORDER BY id', prefix.toLowerCase()+'%').toArray() as any[]).filter(owns);
}
