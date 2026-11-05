import type { SqlStorage } from '@cloudflare/workers-types';
import { freshCompleteCatalogs } from './catalog-freshness';

const NON_LIVE_PHASES = "'exited','dead','pruned','forgotten','unobserved'";
const TERMINAL_LAUNCH_STATES = new Set(['failed', 'cancelled', 'exited', 'stopped', 'dead', 'forgotten', 'pruned']);
const WAITING_LAUNCH_STATES = new Set(['queued', 'held', 'offered']);

/** Explain why a due wake cannot reach a native, using only positive evidence.
 * An incomplete or stale host census never proves that a native is absent. */
export function wakeNoNativeReason(
  sql: SqlStorage,
  session: any,
  binding: any,
  now: number,
): string | null {
  if (!session) return 'wake_session_gone';
  const censusTime = (machineId: string) => freshCompleteCatalogs(sql, now).get(machineId);
  const censusAfter = (machineId: string, timestamp: number) => {
    const seenAt = censusTime(machineId);
    return seenAt !== undefined && Number.isFinite(seenAt) && Number.isFinite(timestamp) && seenAt >= timestamp;
  };

  if (!binding) {
    let launch: any = null;
    try {
      launch = session.fleet_launch_id
        ? sql.exec('SELECT id, state, native_id, machine_id, updated_at FROM fleet_launches WHERE id=?', session.fleet_launch_id).toArray()[0]
        : null;
    } catch { return null; }
    if (!launch) return 'wake_session_unbound_no_launch';
    const machineId = session.machine_id ?? launch.machine_id;
    if (typeof machineId !== 'string' || !machineId) return null;
    if (launch.machine_id !== machineId) return 'wake_session_unbound_launch_machine_mismatch';
    const completeAfterLaunch = censusAfter(machineId, Date.parse(launch.updated_at));

    // A starting/running catalog row is positive liveness; other nonterminal
    // labels (unknown, broken, uncertain) do not authorize a native send.
    const tag = 'hub-' + launch.id;
    let live: any;
    try {
      live = launch.native_id
        ? sql.exec(`SELECT native_id FROM fleet_sessions WHERE machine_id=? AND native_id=? AND tag IN (?,?)
            AND phase IN ('starting','running') LIMIT 1`, machineId, launch.native_id, tag, 'task-' + tag).toArray()[0]
        : sql.exec(`SELECT native_id FROM fleet_sessions WHERE machine_id=? AND tag IN (?,?)
            AND phase IN ('starting','running') LIMIT 1`, machineId, tag, 'task-' + tag).toArray()[0];
    } catch { return null; }
    if (live) return null;
    let terminal: any;
    try {
      terminal = launch.native_id
        ? sql.exec(`SELECT phase FROM fleet_sessions WHERE machine_id=? AND native_id=? AND tag IN (?,?)
            AND phase IN (${NON_LIVE_PHASES}) LIMIT 1`, machineId, launch.native_id, tag, 'task-' + tag).toArray()[0]
        : sql.exec(`SELECT phase FROM fleet_sessions WHERE machine_id=? AND tag IN (?,?)
            AND phase IN (${NON_LIVE_PHASES}) LIMIT 1`, machineId, tag, 'task-' + tag).toArray()[0];
    } catch { return null; }
    if (terminal && (terminal.phase !== 'unobserved' || completeAfterLaunch)) {
      return `wake_session_unbound_native_${String(terminal.phase).replace(/[^a-z_]/g, '').slice(0, 24)}`;
    }
    let observed: any;
    try {
      observed = launch.native_id
        ? sql.exec(`SELECT phase FROM fleet_sessions WHERE machine_id=? AND native_id=? AND tag IN (?,?) LIMIT 1`,
            machineId, launch.native_id, tag, 'task-' + tag).toArray()[0]
        : sql.exec(`SELECT phase FROM fleet_sessions WHERE machine_id=? AND tag IN (?,?) LIMIT 1`,
            machineId, tag, 'task-' + tag).toArray()[0];
    } catch { return null; }
    if (observed && completeAfterLaunch && !['starting', 'running'].includes(String(observed.phase))) {
      return `wake_session_unbound_native_${String(observed.phase).replace(/[^a-z_]/g, '').slice(0, 24)}`;
    }

    if (TERMINAL_LAUNCH_STATES.has(launch.state)) {
      return `wake_session_unbound_launch_${String(launch.state).replace(/[^a-z_]/g, '').slice(0, 24)}`;
    }
    if (WAITING_LAUNCH_STATES.has(launch.state)) return null;
    // A missing row cannot prove that a claimed start never happened. When
    // launch evidence already names a native, a fresh complete census after
    // that receipt can prove the known native is no longer present.
    if (launch.native_id && completeAfterLaunch && ['starting', 'running', 'uncertain'].includes(launch.state)) {
      return 'wake_session_unbound_native_not_observed';
    }
    return null;
  }

  if (typeof session.machine_id !== 'string' || !session.machine_id) return 'wake_native_machine_missing';
  const completeAfterNative = censusAfter(session.machine_id, Number(binding.native_created_at_ms));
  if (typeof binding.native_id !== 'string' || !binding.native_id) {
    return completeAfterNative ? 'wake_native_binding_invalid' : null;
  }
  if (session.fleet_launch_id) {
    let launch: any;
    try {
      launch = sql.exec('SELECT machine_id, state, native_id FROM fleet_launches WHERE id=?', session.fleet_launch_id).toArray()[0];
    } catch { return null; }
    if (launch && (launch.machine_id !== session.machine_id ||
        (launch.native_id && launch.native_id !== binding.native_id))) {
      return completeAfterNative ? 'wake_native_launch_identity_mismatch' : null;
    }
  }
  let native: any;
  try {
    native = sql.exec(`SELECT native_id, tag, created_at_ms, phase FROM fleet_sessions
      WHERE machine_id=? AND native_id=?`, session.machine_id, binding.native_id).toArray()[0];
  } catch { return null; }
  if (!native) return completeAfterNative ? 'wake_native_not_observed' : null;
  if (native.tag !== binding.tag || native.created_at_ms !== binding.native_created_at_ms) {
    return completeAfterNative ? 'wake_native_identity_mismatch' : null;
  }
  if (['exited', 'dead'].includes(native.phase)) return `wake_native_${native.phase}`;
  if (['pruned', 'forgotten', 'unobserved'].includes(native.phase)) {
    return completeAfterNative ? `wake_native_${native.phase}` : null;
  }
  if (!['starting', 'running'].includes(native.phase)) {
    return completeAfterNative ? 'wake_native_not_live' : null;
  }
  return null;
}

/** A due wake may be queued only when a fresh complete census contains the
 * exact bound native incarnation in a live phase. Unknown or stale catalog
 * state waits for the receipt deadline instead of creating a doomed send. */
export function wakeNativeCanReceive(
  sql: SqlStorage,
  session: any,
  binding: any,
  now: number,
): boolean {
  if (!session || typeof session.machine_id !== 'string' || !session.machine_id ||
      !binding || typeof binding.native_id !== 'string' ||
      typeof binding.tag !== 'string' || typeof binding.workspace !== 'string' ||
      !Number.isSafeInteger(binding.native_created_at_ms)) return false;
  const censusAt = freshCompleteCatalogs(sql, now).get(session.machine_id);
  if (!Number.isFinite(censusAt) || censusAt! < binding.native_created_at_ms) return false;
  try {
    const native: any = sql.exec(`SELECT native_id, tag, engine, phase, workspace, created_at_ms
      FROM fleet_sessions WHERE machine_id=? AND native_id=?`, session.machine_id, binding.native_id).toArray()[0];
    if (!native || native.tag !== binding.tag || native.workspace !== binding.workspace ||
        native.created_at_ms !== binding.native_created_at_ms ||
        !['starting', 'running'].includes(String(native.phase)) ||
        (session.engine && native.engine !== session.engine)) return false;
    if (session.fleet_launch_id) {
      const launch: any = sql.exec('SELECT machine_id, engine, state, native_id FROM fleet_launches WHERE id=?',
        session.fleet_launch_id).toArray()[0];
      if (!launch || launch.machine_id !== session.machine_id ||
          !['starting', 'running', 'uncertain'].includes(String(launch.state)) ||
          (launch.native_id && launch.native_id !== binding.native_id) ||
          (launch.engine && native.engine && launch.engine !== native.engine)) return false;
    }
    return true;
  } catch { return false; }
}
