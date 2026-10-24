import type { SqlStorage } from '@cloudflare/workers-types';

/** Reconcile reservations from positive terminal evidence, never catalog absence.
 * Returns the proposed changes before applying so operators can save a backup.
 * No launch, session or task result is deleted or reported as successful. */
export function reconcileAccountCapacity(sql: SqlStorage, now: number, apply = true) {
  const controls = sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length > 0;
  const changes: { id: string; before: string; after: string; reason: string }[] = [];
  for (const launch of sql.exec("SELECT * FROM fleet_launches WHERE state IN ('starting','running','uncertain')").toArray() as any[]) {
    if (sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', launch.id).toArray().length) continue;
    const native: any = launch.native_id ? sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', launch.machine_id, launch.native_id).toArray()[0] : null;
    // A fresh, live native observation takes precedence over stale control state.
    if (native && ['starting', 'running', 'broken', 'unknown'].includes(native.phase)) continue;
    const session: any = controls ? sql.exec('SELECT state, binding_json FROM session_control_sessions WHERE fleet_launch_id = ? AND (machine_id IS NULL OR machine_id = ?)', launch.id, launch.machine_id).toArray()[0] : null;
    let binding: any = null;
    try { binding = JSON.parse(session?.binding_json || 'null'); } catch { /* unknown identity */ }
    const sameIncarnation = !session?.binding_json || (binding && native && binding.native_id === native.native_id && binding.tag === native.tag && binding.workspace === native.workspace && binding.native_created_at_ms === native.created_at_ms);
    let after: string | null = null;
    let reason = '';
    if (native && sameIncarnation && ['exited', 'dead'].includes(native.phase) && native.engine === launch.engine &&
        Number(native.created_at_ms) >= Date.parse(launch.created_at) - 60000 &&
        !native.workspace.endsWith(' (deleted)') && !(native.cwd || '').endsWith(' (deleted)')) {
      after = 'exited'; reason = 'native_terminal';
    }
    if (!after) continue;
    changes.push({ id: launch.id, before: launch.state, after, reason });
    if (!apply) continue;
    const at = new Date(now).toISOString();
    sql.exec('UPDATE fleet_launches SET state = ?, exit_reason = ?, verification_status = ?, finished_at = ?, updated_at = ? WHERE id = ?', after, reason, 'verified', at, at, launch.id);
    if (controls) sql.exec("UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE fleet_launch_id = ? AND state IN ('queued','held','offered','starting','running','uncertain')", after === 'failed' ? 'failed' : 'stopped', at, launch.id);
  }
  return changes;
}

/** Account holds that contradict observed workload counts need operator attention.
 * This is diagnostic only: uncertainty never authorizes an extra launch. */
export function accountCapacityAlerts(sql: SqlStorage, now: number) {
  const subjects: { kind: 'launch'; id: string; state: string; detail: string }[] = [];
  const cutoff = new Date(now - 600000).toISOString();
  for (const queued of sql.exec("SELECT id, request_json FROM fleet_launches WHERE state = 'queued' AND created_at <= ?", cutoff).toArray() as any[]) {
    let intent: any;
    try { intent = JSON.parse(queued.request_json); } catch { continue; }
    for (const account of sql.exec('SELECT * FROM fleet_accounts WHERE enabled = 1').toArray() as any[]) {
      const routes = sql.exec('SELECT * FROM fleet_routes WHERE account = ?', account.label).toArray() as any[];
      if (!routes.some(r => (!intent.machine || intent.machine === r.machine_id) && (!intent.engine || intent.engine === r.engine))) continue;
      const reserved = Number(sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE account = ? AND state IN ('offered','starting','running','uncertain')", account.label).one().n);
      const live = Number(sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE account = ? AND phase IN ('starting','running','broken','unknown') AND workspace NOT LIKE '% (deleted)' AND (cwd IS NULL OR cwd NOT LIKE '% (deleted)')", account.label).one().n);
      if (reserved < account.max_concurrency || live >= account.max_concurrency) continue;
      subjects.push({ kind: 'launch', id: queued.id, state: 'stuck', detail: `account_capacity queued >10m: account=${account.label}, reserved=${reserved}, observed_live=${live}, max_concurrency=${account.max_concurrency}` });
      break;
    }
  }
  return subjects;
}
