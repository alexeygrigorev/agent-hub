import type { SqlStorage } from '@cloudflare/workers-types';

/** A headless launch that never leaves `starting` (host claimed, no running
 * receipt, no item after turn.started) is failed after this bound (intake 6c2b2aa2 AC2). */
export const SILENT_START_MS = 15 * 60 * 1000;
export const SILENT_START_REASON = 'worker_silent_start';
/** Abandoned claims stay visible to owning-host reconciliation this long, so a
 * late fenced no-start, a completed hub-results report or a late native start
 * still reaches Hub (intake 0b87eb29). */
export const ABANDONED_RECOVERY_MS = 24 * 3600 * 1000;

/** Fail headless launches that produced no bound native within SILENT_START_MS.
 * This is an abandonment, never exit proof: verification stays `unverified` and
 * the owning host's later exact receipt supersedes it (see Fleet result).
 * Kept outside FleetService so session stop can apply the same bound to one
 * launch without a circular import. */
export function expireSilentStartRows(sql: SqlStorage, nowMs: number, launchId?: string): number {
  const at = new Date(nowMs).toISOString();
  const cutoff = new Date(nowMs - SILENT_START_MS).toISOString();
  const controlStore = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
  const unbound = controlStore
    ? `EXISTS (SELECT 1 FROM session_control_sessions s WHERE s.fleet_launch_id=l.id AND s.binding_json IS NULL AND s.state IN ('offered','starting'))`
    : '0=1';
  const only = launchId === undefined ? '' : 'AND l.id = ?';
  const rows = sql.exec(`SELECT l.id, l.machine_id, l.engine FROM fleet_launches l
    WHERE l.state IN ('starting','running') AND l.mode='headless' AND l.updated_at < ? ${only}
      AND NOT EXISTS (SELECT 1 FROM routing_jobs j WHERE j.launch_id=l.id) AND ${unbound}
    ORDER BY l.updated_at,l.id LIMIT 20`, cutoff, ...(launchId === undefined ? [] : [launchId])).toArray() as any[];
  const diagnostic = JSON.stringify({reason: SILENT_START_REASON,
    message: 'The headless worker produced no output within 15 minutes of starting; the launch was failed.',
    error_type: 'TimeoutError', stage: 'runtime'});
  for (const row of rows) {
    sql.exec(`UPDATE fleet_launches SET state='failed', verification_status='unverified', exit_reason=?,
      launch_error_json=?, finished_at=?, updated_at=? WHERE id=? AND state IN ('starting','running')`, SILENT_START_REASON, diagnostic, at, at, row.id);
    if (row.engine && row.machine_id) {
      // Never overwrite an existing unavailable hold (account_blocked,
      // admission_unconfigured, ...) with this retriable one.
      const held = sql.exec("SELECT 1 FROM fleet_engine_health WHERE engine=? AND machine_id=? AND status='unavailable'", row.engine, row.machine_id).toArray().length;
      if (!held) sql.exec("INSERT OR REPLACE INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES (?, ?, 'unavailable', ?, ?)",
        row.engine, row.machine_id, SILENT_START_REASON, at);
    }
  }
  return rows.length;
}

/** True for a launch Hub abandoned on the silent-start bound with no exit proof. */
export function abandonedLaunch(row: any): boolean {
  return !!row && row.mode === 'headless' && row.state === 'failed'
    && row.exit_reason === SILENT_START_REASON && row.verification_status === 'unverified';
}
