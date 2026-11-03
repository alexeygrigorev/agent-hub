import type { SqlStorage } from '@cloudflare/workers-types';

const MAX_RECONCILIATIONS_PER_CENSUS = 500;

/** Reconcile only positive catalog evidence for a claimed interactive launch.
 *
 * A complete catalog can confirm an exact live native or a native that
 * explicitly exited. A missing row alone is not launch failure evidence: the
 * host's fenced no-start receipt owns that transition. This keeps an uncertain
 * start protected while allowing every live row to advance to binding retry.
 */
export function reconcileInteractiveLaunchCatalog(
  sql: SqlStorage,
  machineId: string,
  now: number,
  censusComplete: boolean,
): { failed: number; pending: number } {
  if (!censusComplete) return { failed: 0, pending: 0 };
  const required = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session_control_sessions','fleet_launches','fleet_sessions')").toArray();
  if (required.length !== 3) return { failed: 0, pending: 0 };

  const at = new Date(now).toISOString();
  const ended = sql.exec(`SELECT l.id AS launch_id, l.native_id AS launch_native_id,
      s.id AS session_id, fs.native_id, fs.phase
    FROM fleet_launches l
    JOIN session_control_sessions s ON s.fleet_launch_id=l.id
    JOIN fleet_sessions fs ON fs.machine_id=l.machine_id
      AND fs.tag IN ('hub-' || l.id, 'task-hub-' || l.id)
      AND (l.native_id IS NULL OR l.native_id=fs.native_id)
      AND (l.engine IS NULL OR fs.engine IS NULL OR l.engine=fs.engine)
    WHERE l.machine_id=? AND l.mode='interactive'
      AND l.state IN ('starting','running','uncertain')
      AND s.machine_id=l.machine_id AND s.binding_json IS NULL
      AND s.state IN ('offered','starting','running','uncertain')
      AND fs.phase IN ('exited','dead')
      AND NOT EXISTS (SELECT 1 FROM fleet_sessions live
        WHERE live.machine_id=l.machine_id AND live.tag=fs.tag
          AND (l.engine IS NULL OR live.engine IS NULL OR l.engine=live.engine)
          AND live.phase IN ('starting','running'))
    ORDER BY l.rowid LIMIT ?`, machineId, MAX_RECONCILIATIONS_PER_CENSUS).toArray() as any[];

  let failed = 0;
  for (const row of ended) {
    const reason = 'native_exited_before_binding';
    const diagnostic = JSON.stringify({
      reason,
      stage: 'host_binding',
      message: 'The exact launch native has a confirmed terminal phase before Hub binding.',
    });
    const written = sql.exec(`UPDATE fleet_launches SET state='failed', native_id=COALESCE(native_id, ?),
        verification_status='verified', exit_reason=?, launch_error_json=?, finished_at=?, updated_at=?
      WHERE id=? AND machine_id=? AND state IN ('starting','running','uncertain')
        AND (native_id IS NULL OR native_id=?) RETURNING id`,
    row.native_id, reason, diagnostic, at, at, row.launch_id, machineId, row.native_id).toArray();
    if (!written.length) continue;
    sql.exec(`UPDATE session_control_sessions SET state='failed', updated_at=?
      WHERE id=? AND fleet_launch_id=? AND machine_id=? AND binding_json IS NULL
        AND state IN ('offered','starting','running','uncertain')`,
    at, row.session_id, row.launch_id, machineId);
    failed++;
  }

  // Live natives are handled separately from terminal evidence so an old
  // binding refusal cannot occupy the first rows and starve later binding
  // retries. The host feed rechecks the exact launch tag and native identity.
  const live = sql.exec(`SELECT l.id, s.id AS session_id FROM fleet_launches l
    JOIN session_control_sessions s ON s.fleet_launch_id=l.id
    WHERE l.machine_id=? AND l.mode='interactive'
      AND l.state IN ('starting','running','uncertain')
      AND l.launcher_task_id IS NULL
      AND s.machine_id=l.machine_id AND s.binding_json IS NULL
      AND s.state IN ('offered','starting','running')
      AND EXISTS (SELECT 1 FROM fleet_sessions fs WHERE fs.machine_id=l.machine_id
        AND fs.tag IN ('hub-' || l.id, 'task-hub-' || l.id)
        AND (l.native_id IS NULL OR l.native_id=fs.native_id)
        AND (l.engine IS NULL OR fs.engine IS NULL OR l.engine=fs.engine)
        AND fs.phase IN ('starting','running'))
    ORDER BY l.rowid LIMIT ?`, machineId, MAX_RECONCILIATIONS_PER_CENSUS).toArray() as any[];
  for (const row of live) {
    const diagnostic = JSON.stringify({
      reason: 'binding_pending_native_observed',
      stage: 'host_binding',
      message: 'A live native is present in the complete catalog but has not been bound; the host will retry its exact binding.',
    });
    sql.exec(`UPDATE fleet_launches SET launch_error_json=? WHERE id=?
      AND COALESCE(CASE WHEN json_valid(launch_error_json) THEN json_extract(launch_error_json,'$.reason') ELSE '' END,'') != 'binding_pending_native_observed'`, diagnostic, row.id);
    sql.exec(`UPDATE session_control_sessions SET state='uncertain', updated_at=?
      WHERE id=? AND binding_json IS NULL AND state IN ('offered','starting','running')`, at, row.session_id);
  }
  return { failed, pending: live.length };
}
