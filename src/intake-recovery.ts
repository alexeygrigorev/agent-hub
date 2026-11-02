/** Recovery decisions never substitute a stall observation for native exit proof. */
import type { SqlStorage } from '@cloudflare/workers-types';

/** A second canonical row must not bypass protection of the same native worker. */
export function recoveryProtected(sql: SqlStorage, session: { id: string; machine_id?: string; binding_json?: string }): boolean {
  if (sql.exec('SELECT session_id FROM session_control_protections WHERE session_id=?',session.id).toArray().length) return true;
  let binding: any;
  try { binding = JSON.parse(session.binding_json || 'null'); } catch { return false; }
  if (!session.machine_id || !binding?.native_id) return false;
  return sql.exec(`SELECT s.id FROM session_control_sessions s JOIN session_control_protections p ON p.session_id=s.id
    WHERE s.machine_id=? AND json_extract(s.binding_json,'$.native_id')=? LIMIT 1`, session.machine_id,binding.native_id).toArray().length > 0;
}

/** Waiting belongs to the physical worker too, even through an unlinked alias. */
export function recoveryWakePending(sql: SqlStorage, session: { id: string; machine_id?: string; binding_json?: string }, now: number): boolean {
  if (sql.exec("SELECT id FROM session_control_wakes WHERE session_id=? AND state='pending' AND wake_at_ms>? LIMIT 1",session.id,now).toArray().length) return true;
  let binding: any;
  try { binding = JSON.parse(session.binding_json || 'null'); } catch { return false; }
  if (!session.machine_id || !binding?.native_id) return false;
  return sql.exec(`SELECT w.id FROM session_control_sessions s JOIN session_control_wakes w ON w.session_id=s.id
    WHERE s.machine_id=? AND json_extract(s.binding_json,'$.native_id')=? AND w.state='pending' AND w.wake_at_ms>? LIMIT 1`,
    session.machine_id,binding.native_id,now).toArray().length > 0;
}

export type RecoverySession = {
  id: string;
  state: string;
  lifecycle_state?: string;
  stalled?: boolean;
  stall_reason?: string | null;
  waiting_until?: string | null;
};

export function recoveryAction(session: RecoverySession, now: number): 'stop' | 'restart' | 'wait' {
  // A scheduled wake is deliberate waiting, even when an older stall is retained.
  const wake = Date.parse(session.waiting_until ?? '');
  if (Number.isFinite(wake) && wake > now) return 'wait';
  const lifecycle = session.lifecycle_state ?? session.state;
  if (['failed', 'refused', 'cancelled', 'stopped', 'engine_failed', 'engine_oom_killed'].includes(lifecycle)) return 'restart';
  if (lifecycle === 'running' && (session.stalled || session.state === 'stalled') &&
      ['no_native_progress', 'agent_exited_children_running', 'agent_exited'].includes(session.stall_reason ?? '')) return 'stop';
  return 'wait';
}

export type LaunchOutcome = {
  id: string; machine_id: string; engine: string; state: string;
  updated_at: string; created_at?: string; native_id?: string | null; verification_status?: string | null;
  exit_reason?: string | null;
};

/** Count definite launcher failures on one host/engine after its last successful
 * explicit probe. Order attempts by creation, so an older healthy process
 * exiting late cannot erase newer failures. Uncertain dispatch and quota reaps
 * cannot prove a broken launcher. Deterministic ties use the launch id. */
export function launchFailureStreak(outcomes: LaunchOutcome[], machine: string, engine: string,
  probeSucceededAt: string | null = null): number {
  const probe = probeSucceededAt === null ? -Infinity : Date.parse(probeSucceededAt);
  if (Number.isNaN(probe)) return 0;
  const recent = outcomes.filter(o => o.machine_id === machine && o.engine === engine && Date.parse(o.updated_at) > probe)
    .sort((a, b) => Date.parse(b.created_at ?? b.updated_at) - Date.parse(a.created_at ?? a.updated_at) || b.id.localeCompare(a.id));
  let streak = 0;
  for (const o of recent) {
    if (o.native_id && ['running', 'exited'].includes(o.state)) break;
    if (o.state !== 'failed' || !['not_started','verified'].includes(o.verification_status ?? '')) continue;
    if (!['launcher_command_failed', 'dispatch_local_command_failed', 'dispatch_claim_refused'].includes(o.exit_reason ?? '')) break;
    streak++;
  }
  return streak;
}

/** Eligibility is supplied by canonical admission, including operator engine
 * policy and account quota gates. Ordering alone never grants launch authority. */
export function recoveryEngineOrder(eligible: string[], previous?: string): string[] {
  const preferred = ['claude', 'codex', 'opencode', 'zcodex', 'antigravity'];
  const unique = [...new Set(eligible)];
  const rank = (engine: string) => {
    const index = preferred.indexOf(engine);
    return index < 0 ? preferred.length : index;
  };
  return unique.sort((a, b) => Number(a === previous) - Number(b === previous) || rank(a) - rank(b) || a.localeCompare(b));
}
