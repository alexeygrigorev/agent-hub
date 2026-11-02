/** One native-progress classifier shared by session control and existing liveness alerts.
 * Lifecycle state remains intact: a stall is recoverable and never releases capacity.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { effectiveCheckin } from './registry';

/** Host clocks may be reused only within one synchronous projection. Native
 * fallback clocks remain per row when no host clock is available. */
export type NativeObservationCache = Map<string, { at: string | null }>;

export function nativeObservationFresh(sql: SqlStorage, native: any, now: number, cache?: NativeObservationCache): boolean {
  if (!native) return false;
  let host = cache?.get(native.machine_id);
  if (!host) {
    const machine: any = sql.exec('SELECT last_checkin_at FROM machines WHERE id = ?', native.machine_id).toArray()[0];
    const seen: any = sql.exec('SELECT observed_at FROM fleet_catalog_seen WHERE machine_id = ?', native.machine_id).toArray()[0];
    host = { at: seen?.observed_at ?? effectiveCheckin(native.machine_id, machine?.last_checkin_at) ?? null };
    cache?.set(native.machine_id, host);
  }
  const at = Date.parse(host.at ?? native.observed_at ?? '');
  return Number.isFinite(at) && now - at <= 180000 && at <= now + 60000;
}

export function initSessionStalls(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS session_stalls (
    machine_id TEXT NOT NULL, native_id TEXT NOT NULL, incarnation INTEGER NOT NULL,
    since_ms INTEGER NOT NULL, recovery_ms INTEGER, PRIMARY KEY(machine_id, native_id))`);
}

export function stallThreshold(env?: any): number {
  const n = Number(env?.HUB_AGENT_STALL_S);
  return Number.isFinite(n) && n > 0 && n <= 604800 ? n : 1800;
}

/** Intake 167c6178: an active tool call must not trip the 30-minute
 * no_native_progress stall. It gets its own, longer hung-call bound
 * (default two hours) instead. */
export function toolCallStallThreshold(env?: any): number {
  const n = Number(env?.HUB_AGENT_TOOLCALL_STALL_S);
  return Number.isFinite(n) && n > 0 && n <= 604800 ? n : 7200;
}

export function nativeProgress(native: any): number | null {
  let progress: any = null;
  try { progress = JSON.parse(native?.progress_json ?? 'null'); } catch { /* unknown */ }
  // The host's progress observation is authoritative. PTY output can be
  // housekeeping, retries or hook chatter long after the agent stopped.
  // Retain the legacy fallback only for hosts without a progress sampler.
  const at = progress ? progress.last_progress_at : native?.last_activity_at;
  const time = typeof at === 'string' ? Date.parse(at) : NaN;
  if (Number.isFinite(time)) return time;
  return Number.isFinite(native?.created_at_ms) ? native.created_at_ms : null;
}

export function orphanFailure(native: any): boolean {
  try {
    const p = JSON.parse(native?.progress_json ?? 'null');
    return p?.headless === true && p?.orphaned === true;
  } catch { return false; }
}

export function nativeTerminalError(native: any): string | null {
  try {
    const p = JSON.parse(native?.progress_json ?? 'null');
    // Match the phases Fleet invalidates on an absent complete census.
    if (native?.engine === 'zcodex') {
      // A dead zcodex leader leaves its scope in 'exiting' with live children.
      return ['running', 'starting', 'broken', 'exiting'].includes(native?.phase)
        && p?.headless === false && ['zcodex_agent_exited', 'zcodex_reconnect_failed'].includes(p?.terminal_error)
        ? p.terminal_error : null;
    }
    return ['running', 'starting', 'broken'].includes(native?.phase)
      && native?.engine === 'codex' && p?.headless === false && p?.terminal_error === 'codex_reconnect_failed'
      ? p.terminal_error : null;
  } catch { return null; }
}

/** Projection only: the native process and Fleet lease still require exit proof. */
export function sessionTerminalError(row: any, native: any): string | null {
  try {
    const binding = JSON.parse(row?.binding_json ?? 'null');
    return row?.state === 'running' && row?.mode === 'interactive' && (row?.engine === 'codex' || row?.engine === 'zcodex')
      && native?.machine_id === row.machine_id && native?.native_id === binding?.native_id
      && native?.tag === binding?.tag && native?.workspace === binding?.workspace
      && Number.isInteger(binding?.native_created_at_ms) && binding.native_created_at_ms > 0
      && native?.created_at_ms === binding.native_created_at_ms ? nativeTerminalError(native) : null;
  } catch { return null; }
}

/** An active tool call: the engine's Working state or a live headless leader
 * (live child process). `progress.last_cpu_at` — host-published time of the
 * leader's last CPU advance — separates a genuinely hung call (no output and
 * no CPU) from a long-running one that only lacks transcript writes. */
export function activeToolCall(native: any, progress: any): boolean {
  return native?.agent_state === 'working' ||
    (progress?.headless === true && progress?.agent_alive === true);
}

export function classifySessionStall(sql: SqlStorage, native: any, now: number,
  threshold = 1800, hostFresh = true, toolCallThreshold = 7200): { stalled: boolean; reason: string | null; since: string | null; idle_for: number | null } {
  if (!native) return { stalled: false, reason: null, since: null, idle_for: null };
  let progress: any = null;
  try { progress = JSON.parse(native.progress_json ?? 'null'); } catch { /* unknown */ }
  const last = nativeProgress(native);
  const idle = last === null ? null : Math.max(0, (now - last) / 1000);
  const terminal = ['exited', 'dead', 'pruned', 'forgotten'].includes(native.phase);
  const orphan = progress?.agent_alive === false && progress?.headless === true && !terminal;
  const toolCall = activeToolCall(native, progress);
  // A hung tool call needs both bounds: no output and no observed CPU advance
  // beyond the long threshold. Hosts without CPU evidence keep the output-only
  // long bound. CPU is never progress, so it cannot clear the output stall.
  const cpuAt = typeof progress?.last_cpu_at === 'string' ? Date.parse(progress.last_cpu_at) : NaN;
  const cpuIdle = Number.isFinite(cpuAt) ? Math.max(0, (now - cpuAt) / 1000) : null;
  // Unknown/stale observations cannot establish recovery or erase an open stall.
  const observable = hostFresh && !['unknown', 'unobserved'].includes(native.phase);
  // A fresh complete catalog that no longer observes this native binding is
  // lack of progress, not exit proof. It must not leave Hub running forever.
  const idleCandidate = hostFresh && idle !== null &&
    (toolCall ? idle > toolCallThreshold && (cpuIdle === null || cpuIdle > toolCallThreshold)
              : idle > threshold);
  const candidate = !terminal && (orphan || idleCandidate);
  let saved: any = sql.exec('SELECT * FROM session_stalls WHERE machine_id = ? AND native_id = ?', native.machine_id, native.native_id).toArray()[0];
  const incarnation = native.created_at_ms ?? 0;
  if (saved && saved.incarnation !== incarnation) {
    sql.exec('DELETE FROM session_stalls WHERE machine_id = ? AND native_id = ?', native.machine_id, native.native_id);
    saved = null;
  }
  if (candidate && (!saved || saved.recovery_ms !== null)) {
    const since = saved?.since_ms ?? now;
    sql.exec('INSERT OR REPLACE INTO session_stalls VALUES (?, ?, ?, ?, NULL)', native.machine_id, native.native_id, incarnation, since);
    saved = { since_ms: since, recovery_ms: null };
  } else if (saved && observable && !candidate) {
    // Require a full minute of fresh progress after a stall; repeated old
    // snapshots and a single output byte cannot flap notifications.
    if (terminal || (saved.recovery_ms !== null && now - saved.recovery_ms >= 60000 && last !== null && last >= saved.since_ms)) {
      sql.exec('DELETE FROM session_stalls WHERE machine_id = ? AND native_id = ?', native.machine_id, native.native_id);
      saved = null;
    } else if (saved.recovery_ms === null) {
      sql.exec('UPDATE session_stalls SET recovery_ms = ? WHERE machine_id = ? AND native_id = ?', now, native.machine_id, native.native_id);
      saved.recovery_ms = now;
    }
  }
  return { stalled: !!saved, reason: saved ? (orphan ? (progress?.orphaned ? 'agent_exited_children_running' : 'agent_exited')
    : toolCall ? 'hung_tool_call' : 'no_native_progress') : null,
    since: saved ? new Date(saved.since_ms).toISOString() : null, idle_for: idle };
}
