/** Intake linkage is evidence, never authority to control a native worker. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { nativeObservationFresh } from './session_stall';

export const parseObject = (raw: any): any => { try { return JSON.parse(raw || '{}'); } catch { return {}; } };
export const OPEN_INTAKES = new Set(['open', 'in_progress', 'blocked', 'pending_approval']);

export function intakeHasDone(task: any): boolean {
  return (task?.history || []).some((e: any) => e.kind === 'note' && /^DONE\b/i.test(String(e.text || '').trim()));
}

export function hasOpenWork(task: any): boolean {
  return !!task && OPEN_INTAKES.has(task.status) && !task.archived_at && !intakeHasDone(task);
}

/** Only explicit links and START notes count; an RCA mentioning another
 * session must never silently adopt that session as its own executor. */
export function historyReferencesSession(task: any, session: any, binding: any): boolean {
  const native = String(binding?.native_id || '');
  const tag = String(binding?.tag || '');
  return (task?.history || []).some((e: any) => {
    if ((e.hub_session_id && e.hub_session_id === session.id) || (native && e.native_id === native && (!e.host || e.host === session.machine_id))) return true;
    if (e.kind !== 'note' || !/^START(?:ED)?\b/i.test(String(e.text || '').trim())) return false;
    const tokens = String(e.text).split(/[^A-Za-z0-9_.:/~-]+/);
    return (native && tokens.includes(native)) || (tag && tokens.some(t => t === tag || t.endsWith(':' + tag)));
  });
}

export function linkedIntake(sql: SqlStorage, session: any, inferHistory = false): any | null {
  const request = parseObject(session.request_json), binding = parseObject(session.binding_json);
  if (request.intake_task_id) {
    const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id=?', request.intake_task_id).toArray()[0];
    return row ? parseObject(row.data) : null;
  }
  if (!inferHistory) {
    const ids: any[] = sql.exec(`SELECT DISTINCT i.id FROM intake_task_projection i, json_each(i.links) e
      WHERE json_extract(e.value,'$.hub_session_id')=? OR
      (json_extract(e.value,'$.native_id')=? AND (json_extract(e.value,'$.host') IS NULL OR json_extract(e.value,'$.host')=?)) LIMIT 2`,
      session.id || '', binding.native_id || '', session.machine_id || '').toArray();
    if (ids.length !== 1) return null;
    const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id=?', ids[0].id).toArray()[0];
    return row ? parseObject(row.data) : null;
  }
  const matches = sql.exec('SELECT data FROM intake_tasks').toArray()
    .map((r: any) => parseObject(r.data)).filter(t => historyReferencesSession(t, session, binding));
  // Ambiguous notes require an explicit manual link; never pick an arbitrary task.
  return matches.length === 1 ? matches[0] : null;
}

export function nativeGoalStatus(native: any): string | null {
  const p = parseObject(native?.progress_json);
  return ['blocked', 'paused', 'stalled', 'active', 'complete'].includes(p.goal_status) ? p.goal_status : null;
}

/** Optional scheduled-wake contract; no scheduler or second delivery path. */
export function sessionWakeState(sql: SqlStorage, sessionId: string, now: number) {
  const exists = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_wakes'").toArray().length;
  if (!exists) return { waiting_until: null as string | null, missed: false };
  const pending: any = sql.exec("SELECT MIN(wake_at_ms) AS at FROM session_control_wakes WHERE session_id=? AND state='pending' AND wake_at_ms>?", sessionId, now).toArray()[0];
  const missed = sql.exec("SELECT 1 FROM session_control_wakes WHERE session_id=? AND state='missed' LIMIT 1", sessionId).toArray().length > 0;
  return { waiting_until: Number.isFinite(pending?.at) ? new Date(pending.at).toISOString() : null, missed };
}

export function openWorkPreventsIdleReap(sql: SqlStorage, session: any, now = Date.now()): boolean {
  try {
    const request = parseObject(session.request_json);
    const task = linkedIntake(sql, session);
    // A missing linked intake is unknown, never permission to destroy its executor.
    if (!task && !request.intake_task_id) {
      const binding = parseObject(session.binding_json);
      // Ambiguous retained links still establish an obligation even though they
      // cannot pick one intake for automatic recovery.
      return sql.exec(`SELECT 1 FROM intake_task_projection i,json_each(i.links) e WHERE
        json_extract(e.value,'$.hub_session_id')=? OR
        (json_extract(e.value,'$.native_id')=? AND (json_extract(e.value,'$.host') IS NULL OR json_extract(e.value,'$.host')=?)) LIMIT 1`,
        session.id || '',binding.native_id || '',session.machine_id || '').toArray().length > 0;
    }
    if (task && intakeHasDone(task)) return false;
    const table = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_idle_episodes'").toArray();
    if (table.length) {
      const replacement: any = sql.exec(`SELECT s.* FROM session_idle_episodes e JOIN session_control_sessions s
        ON s.id=e.replacement_id WHERE e.session_id=?`, session.id).toArray()[0];
      const binding = parseObject(replacement?.binding_json), old = parseObject(session.binding_json);
      const native: any = binding.native_id && replacement && sql.exec('SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?', replacement.machine_id, binding.native_id).toArray()[0];
      if (replacement?.state === 'running' && binding.native_id !== old.native_id && native?.phase === 'running' &&
          native.created_at_ms === binding.native_created_at_ms && native.tag === binding.tag && native.workspace === binding.workspace &&
          nativeObservationFresh(sql, native, now)) return false;
    }
    return true;
  } catch { return true; }
}
