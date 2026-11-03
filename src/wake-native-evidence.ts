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

    // An exact live catalog native means the host still has a chance to bind
    // it. Wake dispatch waits for that normal verified binding path.
    const tag = 'hub-' + launch.id;
    let live: any;
    try {
      live = launch.native_id
        ? sql.exec(`SELECT native_id FROM fleet_sessions WHERE machine_id=? AND native_id=? AND tag IN (?,?)
            AND phase NOT IN (${NON_LIVE_PHASES}) LIMIT 1`, machineId, launch.native_id, tag, 'task-' + tag).toArray()[0]
        : sql.exec(`SELECT native_id FROM fleet_sessions WHERE machine_id=? AND tag IN (?,?)
            AND phase NOT IN (${NON_LIVE_PHASES}) LIMIT 1`, machineId, tag, 'task-' + tag).toArray()[0];
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
    if (terminal) return `wake_session_unbound_native_${String(terminal.phase).replace(/[^a-z_]/g, '').slice(0, 24)}`;

    if (TERMINAL_LAUNCH_STATES.has(launch.state)) {
      return `wake_session_unbound_launch_${String(launch.state).replace(/[^a-z_]/g, '').slice(0, 24)}`;
    }
    if (WAITING_LAUNCH_STATES.has(launch.state)) return null;
    if (completeAfterLaunch && ['starting', 'running', 'uncertain'].includes(launch.state)) {
      return 'wake_session_unbound_native_not_observed';
    }
    return null;
  }

  if (typeof session.machine_id !== 'string' || !session.machine_id) return 'wake_native_machine_missing';
  const completeAfterNative = censusAfter(session.machine_id, Number(binding.native_created_at_ms));
  if (session.state !== 'running' || typeof binding.native_id !== 'string') return null;
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
  return null;
}
