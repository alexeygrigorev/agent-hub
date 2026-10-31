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

/** Ids of intakes whose projection link/report entries satisfy `match`, in
 * projection scan order, at most `limit`. Candidates come from the indexed
 * intake_projection_link_refs table (intake 8081a05a), so a lookup reads a
 * few rows instead of json_each over every intake. `refs` must cover every
 * hub_session_id/native_id value `match` can accept. */
export function linkedTaskIds(sql: SqlStorage, refs: unknown[], match: (e: any) => boolean, limit: number): string[] {
  const keys = [...new Set(refs.filter((r): r is string => typeof r === 'string'))];
  if (!keys.length) return [];
  const rows = sql.exec(`SELECT i.id, i.links FROM intake_task_projection i WHERE i.id IN
    (SELECT task_id FROM intake_projection_link_refs WHERE ref IN (${keys.map(() => '?').join(',')})) ORDER BY i.rowid`, ...keys).toArray() as any[];
  const ids: string[] = [];
  for (const row of rows) {
    let links: any; try { links = JSON.parse(row.links); } catch { continue; }
    if (!Array.isArray(links) || !links.some(e => e && typeof e === 'object' && !Array.isArray(e) && match(e))) continue;
    ids.push(row.id);
    if (ids.length >= limit) break;
  }
  return ids;
}

/** json_extract(...)=? semantics: only a JSON string equals a text parameter. */
const textEq = (v: unknown, want: unknown) => typeof v === 'string' && v === want;
const absent = (v: unknown) => v === undefined || v === null;

/** The session/native link predicate shared by intake linkage and idle reaping. */
export function sessionLinkIds(sql: SqlStorage, session: any, binding: any, limit: number): string[] {
  const sid = session.id || '', native = binding.native_id || '', machine = session.machine_id || '';
  return linkedTaskIds(sql, [sid, native], e => textEq(e.hub_session_id, sid) ||
    (textEq(e.native_id, native) && (absent(e.host) || textEq(e.host, machine))), limit);
}

export function linkedIntake(sql: SqlStorage, session: any, inferHistory = false): any | null {
  const request = parseObject(session.request_json), binding = parseObject(session.binding_json);
  if (request.intake_task_id) {
    const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id=?', request.intake_task_id).toArray()[0];
    return row ? parseObject(row.data) : null;
  }
  if (!inferHistory) {
    const ids = sessionLinkIds(sql, session, binding, 2);
    if (ids.length !== 1) return null;
    const row: any = sql.exec('SELECT data FROM intake_tasks WHERE id=?', ids[0]).toArray()[0];
    return row ? parseObject(row.data) : null;
  }
  const matches = sql.exec('SELECT data FROM intake_tasks').toArray()
    .map((r: any) => parseObject(r.data)).filter(t => historyReferencesSession(t, session, binding));
  // Ambiguous notes require an explicit manual link; never pick an arbitrary task.
  return matches.length === 1 ? matches[0] : null;
}

/** Host-reported Codex goal states: the thread_goals store values, the screen's
 * `stalled`, and `unregistered` (a `/goal` launch Codex never turned into a goal). */
export const GOAL_STATUSES = ['blocked', 'paused', 'stalled', 'active', 'complete', 'usage_limited', 'budget_limited', 'unregistered'];

export function nativeGoalStatus(native: any): string | null {
  const p = parseObject(native?.progress_json);
  return GOAL_STATUSES.includes(p.goal_status) ? p.goal_status : null;
}

/** Optional scheduled-wake contract; no scheduler or second delivery path. */
export function sessionWakeState(sql: SqlStorage, sessionId: string, now: number) {
  const exists = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_wakes'").toArray().length;
  if (!exists) return { waiting_until: null as string | null, missed: false };
  const pending: any = sql.exec("SELECT MIN(wake_at_ms) AS at FROM session_control_wakes WHERE session_id=? AND state='pending' AND wake_at_ms>?", sessionId, now).toArray()[0];
  const missed = sql.exec("SELECT 1 FROM session_control_wakes WHERE session_id=? AND state='missed' LIMIT 1", sessionId).toArray().length > 0;
  return { waiting_until: Number.isFinite(pending?.at) ? new Date(pending.at).toISOString() : null, missed };
}

/** Read-only memo for one synchronous idle-reap projection over many
 * sessions: linked-intake facts by id and the idle-episode index. */
export type IdleReapCache = { intakes: Map<string, { done: boolean } | null>; episodes?: Set<string> | null };

// JSON letters decode only from literal characters or \u escapes, so data
// without either cannot hold a DONE note; skip parsing retained history.
const mayHoldDone = (raw: string) => /done|\\u/i.test(raw);

function cachedIntakeFacts(sql: SqlStorage, session: any, request: any, cache: IdleReapCache): { done: boolean } | null {
  let id: any = request.intake_task_id;
  if (!id) {
    const binding = parseObject(session.binding_json);
    const ids = sessionLinkIds(sql, session, binding, 2);
    if (ids.length !== 1) return null;
    id = ids[0];
  }
  const key = JSON.stringify([typeof id, String(id)]);
  if (cache.intakes.has(key)) return cache.intakes.get(key)!;
  // Only copy retained history into JS when it could hold a DONE note.
  const row: any = sql.exec(`SELECT CASE WHEN typeof(data)<>'text' OR data LIKE '%done%' OR instr(data, '\\u')>0
    THEN data END AS data FROM intake_tasks WHERE id=?`, id).toArray()[0];
  const facts = row ? { done: typeof row.data === 'string' && mayHoldDone(row.data) ? intakeHasDone(parseObject(row.data)) : false } : null;
  cache.intakes.set(key, facts);
  return facts;
}

export function openWorkPreventsIdleReap(sql: SqlStorage, session: any, now = Date.now(), cache?: IdleReapCache): boolean {
  try {
    const request = parseObject(session.request_json);
    const task = cache ? cachedIntakeFacts(sql, session, request, cache) : linkedIntake(sql, session);
    // A missing linked intake is unknown, never permission to destroy its executor.
    if (!task && !request.intake_task_id) {
      const binding = parseObject(session.binding_json);
      // Ambiguous retained links still establish an obligation even though they
      // cannot pick one intake for automatic recovery.
      return sessionLinkIds(sql, session, binding, 1).length > 0;
    }
    if (task && (cache ? (task as { done: boolean }).done : intakeHasDone(task))) return false;
    if (cache && cache.episodes === undefined) {
      const exists = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_idle_episodes'").toArray().length > 0;
      cache.episodes = exists ? new Set((sql.exec('SELECT session_id FROM session_idle_episodes WHERE replacement_id IS NOT NULL').toArray() as any[]).map(r => r.session_id)) : null;
    }
    // Without a replacement id the join below cannot match, so the cached
    // index only skips queries that would return no row.
    const episodes = cache ? cache.episodes !== null && cache.episodes!.has(session.id)
      : sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_idle_episodes'").toArray().length > 0;
    if (episodes) {
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
