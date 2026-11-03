import type { SqlStorage } from '@cloudflare/workers-types';

/** Resolve claimed interactive starts from one complete host census.
 *
 * A claim is only a dispatch intent; it is not proof that a native started.
 * The catalog PUT that invokes this function is the reconcile boundary: exact
 * live natives remain pending for binding, while launches with no live native
 * become failed with a bounded diagnostic. Incomplete censuses cannot prove
 * absence and are ignored.
 */
const TERMINAL_PHASE_SQL = "'exited','dead','pruned','forgotten','unobserved'";
const MAX_SETTLEMENTS_PER_CENSUS = 500;

export function settleUnstartedInteractiveLaunches(
  sql: SqlStorage,
  machineId: string,
  now: number,
  censusComplete: boolean,
): { failed: number; pending: number } {
  if (!censusComplete) return { failed: 0, pending: 0 };
  const required = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session_control_sessions','fleet_launches','fleet_sessions')").toArray();
  if (required.length !== 3) return { failed: 0, pending: 0 };

  const at = new Date(now).toISOString();
  const candidates = sql.exec(`SELECT l.id,
      EXISTS (SELECT 1 FROM fleet_sessions fs WHERE fs.machine_id=l.machine_id
        AND fs.tag='hub-' || l.id AND fs.phase IN ('exited','dead','pruned','forgotten')) AS terminal_native,
      EXISTS (SELECT 1 FROM fleet_sessions fs WHERE fs.machine_id=l.machine_id
        AND fs.tag='hub-' || l.id AND fs.phase='unobserved') AS unobserved_native
    FROM fleet_launches l
    WHERE l.machine_id=? AND l.mode='interactive'
      AND l.state IN ('starting','uncertain') AND l.native_id IS NULL
      AND l.launcher_task_id IS NULL AND l.updated_at <= ?
      AND EXISTS (SELECT 1 FROM session_control_sessions s WHERE s.fleet_launch_id=l.id
        AND s.binding_json IS NULL AND s.state IN ('offered','starting','uncertain'))
      AND NOT EXISTS (SELECT 1 FROM fleet_sessions fs WHERE fs.machine_id=l.machine_id
        AND fs.tag='hub-' || l.id AND fs.phase NOT IN (${TERMINAL_PHASE_SQL}))
    ORDER BY l.rowid LIMIT ?`, machineId, at, MAX_SETTLEMENTS_PER_CENSUS).toArray() as any[];

  let failed = 0;
  for (const launch of candidates) {
    const reason = launch.terminal_native
      ? 'native_exited_before_binding'
      : launch.unobserved_native ? 'native_unobserved_before_binding' : 'native_start_not_observed';
    const diagnostic = JSON.stringify({
      reason,
      stage: 'dispatch_interactive',
      message: launch.terminal_native
        ? 'The native ended before the host bound it.'
        : launch.unobserved_native
          ? 'A prior native observation is now absent from a complete host census.'
          : 'A complete host census did not observe a native for this claimed launch.',
    });
    const written = sql.exec(`UPDATE fleet_launches SET state='failed', verification_status='unverified',
        exit_reason=?, launch_error_json=?, finished_at=?, updated_at=?
      WHERE id=? AND state IN ('starting','uncertain') AND native_id IS NULL
        AND launcher_task_id IS NULL RETURNING id`, reason, diagnostic, at, at, launch.id).toArray();
    if (!written.length) continue;
    sql.exec(`UPDATE session_control_sessions SET state='failed', updated_at=?
      WHERE fleet_launch_id=? AND binding_json IS NULL
        AND state IN ('offered','starting','uncertain')`, at, launch.id);
    failed++;
  }

  // Live natives are handled separately from the settlement batch so an old
  // binding refusal cannot occupy the first rows forever and starve launches
  // that have no native. Moving an offer to uncertain is a one-time transition;
  // the host-bindings feed continues retrying its exact identity binding.
  const live = sql.exec(`SELECT l.id, s.id AS session_id FROM fleet_launches l
    JOIN session_control_sessions s ON s.fleet_launch_id=l.id
    WHERE l.machine_id=? AND l.mode='interactive' AND l.state IN ('starting','uncertain')
      AND l.native_id IS NULL AND l.launcher_task_id IS NULL AND l.updated_at <= ?
      AND s.binding_json IS NULL AND s.state='offered'
      AND EXISTS (SELECT 1 FROM fleet_sessions fs WHERE fs.machine_id=l.machine_id
        AND fs.tag='hub-' || l.id AND fs.phase NOT IN (${TERMINAL_PHASE_SQL}))
    ORDER BY l.rowid LIMIT ?`, machineId, at, MAX_SETTLEMENTS_PER_CENSUS).toArray() as any[];
  for (const row of live) {
    const diagnostic = JSON.stringify({
      reason: 'binding_pending_native_observed',
      stage: 'host_binding',
      message: 'A live native is present in the complete catalog but has not been bound; the host will retry its exact binding.',
    });
    sql.exec(`UPDATE fleet_launches SET launch_error_json=? WHERE id=?
      AND COALESCE(CASE WHEN json_valid(launch_error_json) THEN json_extract(launch_error_json,'$.reason') ELSE '' END,'') != 'binding_pending_native_observed'`, diagnostic, row.id);
    sql.exec(`UPDATE session_control_sessions SET state='uncertain', updated_at=?
      WHERE id=? AND binding_json IS NULL AND state='offered'`, at, row.session_id);
  }
  return { failed, pending: live.length };
}
