import { effectiveCheckin } from './registry';

/** Stop delivery is bounded independently of the native execution lease. */
export const STOP_DELIVERY_MS = 8000;
export const REACHABLE_STOP_DELIVERY_MS = 60000;
/** Storage persists an unchanged heartbeat only every 300 s (registry.ts), so a
 * Durable Object that lost its in-memory check-in sees a persisted time up to
 * that old for a host that is heartbeating right now. Freshness must exceed the
 * persist interval or such a host is falsely reported stop_host_unreachable
 * (intake 6c2b2aa2 AC3). */
const HOST_FRESH_CHECKIN_MS = 300000 + 120000;

/** Intake 1c8d915e: `machines.last_checkin_at` is persisted only on a
 * changed heartbeat or every five minutes, so freshness must use the
 * in-memory check-in (effectiveCheckin). The host polling right now is
 * reachable by definition. stop_host_unreachable is reserved for a host that
 * is not checking in; an online host that leaves a stop unclaimed past the
 * reachable budget reports stop_not_claimed instead. */
export function stopHostOnline(machine: { id?: unknown; drained?: unknown; last_checkin_at?: unknown } | null | undefined, now: number, pollingMachine?: string): boolean {
  if (!machine || typeof machine.id !== 'string' || machine.drained) return false;
  if (pollingMachine && machine.id === pollingMachine) return true;
  const seen = Date.parse(effectiveCheckin(machine.id, machine.last_checkin_at) || '');
  return Number.isFinite(seen) && now - seen <= HOST_FRESH_CHECKIN_MS;
}

export function expireUnclaimedStops(sql: SqlStorage, now: number, sessionId?: string, machineId?: string): void {
  const at = new Date(now).toISOString();
  const offlineCutoff = new Date(now - STOP_DELIVERY_MS).toISOString();
  const reachableCutoff = new Date(now - REACHABLE_STOP_DELIVERY_MS).toISOString();
  // A machine-scoped sweep runs inside that host's own offers poll. Candidates
  // come from the partial index session_control_unclaimed_stops (few rows);
  // the machine filter rides on the joined session, never a per-poll subquery
  // scan. Unary + keeps it from driving the join: Durable Object SQLite has no
  // ANALYZE statistics, so the planner would otherwise prefer machine_id.
  const rows = sql.exec(`SELECT c.id, c.created_at, s.state AS session_state,
      m.id AS machine_id, m.drained, m.last_checkin_at
    FROM session_control_commands c
    LEFT JOIN session_control_sessions s ON s.id = c.session_id
    LEFT JOIN machines m ON m.id = s.machine_id
    WHERE c.kind = 'stop' AND c.state IN ('recorded','transported') AND c.claim_token IS NULL AND c.created_at <= ?
    ${sessionId ? 'AND c.session_id = ?' : ''}
    ${machineId ? 'AND +s.machine_id = ?' : ''}`,
    offlineCutoff, ...(sessionId ? [sessionId] : []), ...(machineId ? [machineId] : [])).toArray() as any[];
  for (const row of rows) {
    const online = stopHostOnline(row.machine_id ? { id: row.machine_id, drained: row.drained, last_checkin_at: row.last_checkin_at } : null, now, machineId);
    if ((online || row.session_state === 'uncertain') && row.created_at > reachableCutoff) continue;
    sql.exec(`UPDATE session_control_commands SET state = 'failed', ack_json = ?, updated_at = ?
      WHERE id = ? AND kind = 'stop' AND state IN ('recorded','transported') AND claim_token IS NULL`,
      JSON.stringify({ outcome: 'failed', reason: online ? 'stop_not_claimed' : 'stop_host_unreachable' }), at, row.id);
  }
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
