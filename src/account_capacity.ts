import type { SqlStorage } from '@cloudflare/workers-types';
import { accountPlanExpired } from './model-routing';

const NO_START_REFUSAL_MESSAGES: Record<string, string> = {
  launcher_memory_reserve: 'Host memory headroom after launcher reservations is below the configured floor.',
  launcher_disk_reserve: 'Host disk headroom after launcher reservations is below the maintained disk floor.',
};

function verifiedUnboundNoStart(diagnostic: any): string | null {
  if (!diagnostic || typeof diagnostic !== 'object' || Array.isArray(diagnostic)) return null;
  const reason = diagnostic.reason;
  if (typeof reason !== 'string' || NO_START_REFUSAL_MESSAGES[reason] !== diagnostic.message ||
      diagnostic.error_type !== 'ValueError' || diagnostic.stage !== 'dispatch_headless' || diagnostic.exit_code !== 1) return null;
  return reason;
}

/** Reconcile reservations from positive terminal evidence, never catalog absence.
 * Returns the proposed changes before applying so operators can save a backup.
 * No launch, session or task result is deleted or reported as successful. */
export function reconcileAccountCapacity(sql: SqlStorage, now: number, apply = true) {
  const controls = sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length > 0;
  const changes: { id: string; before: string; after: string; reason: string }[] = [];
  // Only positive terminal native evidence can release a reservation. Filter
  // candidates in SQLite instead of probing every live launch from JS.
  const binding = controls ? `(SELECT s.binding_json FROM session_control_sessions s
    WHERE s.fleet_launch_id=l.id AND (s.machine_id IS NULL OR s.machine_id=l.machine_id) LIMIT 1)` : 'NULL';
  for (const launch of sql.exec(`SELECT l.id,l.machine_id,l.native_id,l.state,l.engine,l.created_at,
    n.tag,n.workspace,n.cwd,n.created_at_ms,n.phase,n.engine AS native_engine,${binding} AS binding_json
    FROM fleet_launches l JOIN fleet_sessions n
    ON n.machine_id=l.machine_id AND n.native_id=l.native_id AND n.phase IN ('exited','dead')
    WHERE l.state IN ('starting','running','uncertain')
      AND NOT EXISTS(SELECT 1 FROM routing_jobs j WHERE j.launch_id=l.id)`).toArray() as any[]) {
    // The joined native is already the exact current identity. Read its small
    // proof fields and the first matching control binding in the same query.
    const native = launch.native_id ? { ...launch, engine: launch.native_engine } : null;
    // A fresh, live native observation takes precedence over stale control state.
    if (native && ['starting', 'running', 'broken', 'unknown'].includes(native.phase)) continue;
    let binding: any = null;
    try { binding = JSON.parse(launch.binding_json || 'null'); } catch { /* unknown identity */ }
    const sameIncarnation = !launch.binding_json || (binding && native && binding.native_id === native.native_id && binding.tag === native.tag && binding.workspace === native.workspace && binding.native_created_at_ms === native.created_at_ms);
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

/** Settle unbound launches whose stored receipt positively proves dispatch
 * stopped before any native start. Only exact launcher memory/disk refusal
 * receipts (ValueError, dispatch_headless, exit 1, fixed message) qualify,
 * and only while the launch has no native id, no launcher task id, no
 * routing job, no session-control binding and no native row under its unique
 * managed tag. Bound, observed and ambiguous failures stay reserved, and no
 * record is deleted. Host launcher-ledger recovery uses POSIX-only locking
 * and fails closed on Windows, so without this these rows pin account slots
 * until the 48h stale cutoff while no session exists on any host.
 *
 * Runs on the host-census path (sessions PUT) and the manual /reconcile
 * endpoint, never inside claim/accountStatus maintenance: the candidate scan
 * walks retained history and would burn the status-perf rows-read budget on
 * the hot admission path. Settlement lag is bounded by host poll cadence,
 * and the next census drains freed capacity to waiters. */
export function reconcileVerifiedNoStarts(sql: SqlStorage, now: number, apply = true) {
  const controls = sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length > 0;
  const changes: { id: string; before: string; after: string; reason: string }[] = [];
  const noBinding = controls ? `AND NOT EXISTS (SELECT 1 FROM session_control_sessions c
    WHERE c.fleet_launch_id=l.id AND c.binding_json IS NOT NULL AND c.binding_json != '')` : '';
  const noNativeTag = `AND NOT EXISTS (SELECT 1 FROM fleet_sessions s
    WHERE s.machine_id=l.machine_id AND s.tag='task-hub-' || l.id)`;
  // Scan only rows that can actually pin account slots (the population the
  // capacity count reads): running rows of any age, plus in-flight rows
  // touched in the last 48h. The fleet_launch_nostart_receipt partial index
  // keeps this lookup independent of retained launch history.
  const pinCutoff = new Date(now - 48 * 3600000).toISOString();
  const noStartCandidates = sql.exec(`SELECT l.id,l.state,l.launch_error_json FROM fleet_launches l
    WHERE l.mode='headless' AND l.state IN ('starting','running','uncertain')
      AND (l.state='running' OR l.updated_at > ?)
      AND l.launch_error_json IS NOT NULL AND l.native_id IS NULL AND l.launcher_task_id IS NULL
      AND NOT EXISTS(SELECT 1 FROM routing_jobs j WHERE j.launch_id=l.id)
      ${noBinding} ${noNativeTag}`, pinCutoff).toArray() as any[];
  for (const launch of noStartCandidates) {
    let diagnostic: any = null;
    try { diagnostic = JSON.parse(launch.launch_error_json || 'null'); } catch { /* unknown evidence */ }
    const reason = verifiedUnboundNoStart(diagnostic);
    if (!reason) continue;
    const at = new Date(now).toISOString();
    if (!apply) {
      changes.push({ id: launch.id, before: launch.state, after: 'failed', reason });
      continue;
    }
    const settled = sql.exec(`UPDATE fleet_launches SET state='failed', exit_reason=?, verification_status='not_started',
      finished_at=?, updated_at=? WHERE id=? AND state IN ('starting','running','uncertain')
      AND native_id IS NULL AND launcher_task_id IS NULL RETURNING id`, reason, at, at, launch.id).toArray();
    if (!settled.length) continue;
    if (controls) sql.exec(`UPDATE session_control_sessions SET state='failed', updated_at=?
      WHERE fleet_launch_id=? AND state IN ('queued','held','offered','starting','running','uncertain')
        AND (binding_json IS NULL OR binding_json='')`, at, launch.id);
    changes.push({ id: launch.id, before: launch.state, after: 'failed', reason });
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
      if (accountPlanExpired(String(account.label), now)) continue;
      const routes = sql.exec('SELECT * FROM fleet_routes WHERE account = ?', account.label).toArray() as any[];
      if (!routes.some(r => (!intent.machine || intent.machine === r.machine_id) && (!intent.engine || intent.engine === r.engine))) continue;
      const reserved = Number(sql.exec("SELECT COUNT(*) AS n FROM fleet_launches WHERE account = ? AND state IN ('offered','starting','running','uncertain')", account.label).one().n);
      const live = Number(sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions WHERE account = ? AND phase IN ('starting','running','broken','unknown') AND workspace NOT LIKE '% (deleted)' AND (cwd IS NULL OR cwd NOT LIKE '% (deleted)')", account.label).one().n);
      if (reserved < account.max_concurrency || live >= account.max_concurrency) continue;
      subjects.push({ kind: 'launch', id: 'account-capacity/' + queued.id, state: 'stuck', detail: `account_capacity queued >10m: account=${account.label}, reserved=${reserved}, observed_live=${live}, max_concurrency=${account.max_concurrency}` });
      break;
    }
  }
  return subjects;
}

/** The server quota collector stopped refreshing: every stored server observation is older than
 * 30 minutes, so `hub quota` would silently show old numbers. Raised through the liveness alert path
 * (agent-kind stale subject) and resolved by the next stored snapshot. Empty storage raises nothing. */
export const QUOTA_PROBE_STALE_MS = 30 * 60 * 1000;
export function quotaProbeStaleAlerts(sql: SqlStorage, now: number) {
  let rows: any[] = [];
  try { rows = sql.exec('SELECT captured_at FROM server_quota_snapshots').toArray() as any[]; } catch { return []; }
  const times = rows.map(r => Date.parse(String(r.captured_at))).filter(Number.isFinite);
  if (!times.length) return [];
  const newest = Math.max(...times);
  if (now - newest <= QUOTA_PROBE_STALE_MS) return [];
  const minutes = Math.floor((now - newest) / 60000);
  return [{ kind: 'agent' as const, id: 'quota-server', state: 'stale',
    detail: `quota probe stale: all ${rows.length} server quota observations are older than 30m (newest ${minutes}m ago); check agent-hub-quota-server.timer and its release drop-in on the Hub backend` }];
}
