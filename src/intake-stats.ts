/** Read-only backfill from canonical records. Missing telemetry is never estimated. */
import type { SqlStorage } from '@cloudflare/workers-types';
type Obj = Record<string, any>;
export const STATS_WINDOWS = { '24h': 86400000, '7d': 7 * 86400000, '30d': 30 * 86400000, all: Infinity };
const parse = (v: any): Obj => { try { return typeof v === 'string' ? JSON.parse(v) : v ?? {}; } catch { return {}; } };
export function statsTime(v: any): number | null {
  if (typeof v === 'number') return Number.isSafeInteger(v) && v > 0 ? v : null;
  if (typeof v !== 'string' || !/T.*(?:Z|[+-]\d\d:\d\d)$/i.test(v)) return null;
  const n = Date.parse(v); return Number.isFinite(n) && n > 0 ? n : null;
}
function linkMatchesIncarnation(link: Obj, start: any): boolean {
  const linkedAt = statsTime(link.verified_at) ?? statsTime(link.captured_at);
  const createdAt = statsTime(start);
  return linkedAt === null || createdAt === null || createdAt <= linkedAt;
}
const terminal = new Set(['stopped', 'failed', 'refused', 'cancelled', 'finished', 'exited', 'dead']);
export function projectIntakeStats(task: Obj, sources: { sessions: Obj[]; launches: Obj[]; natives: Obj[]; dependencies?: Obj[]; audit?: Obj[] }, now: number): Obj {
  const history: Obj[] = task.history ?? [];
  const links = history.filter(e => ['session', 'link'].includes(e.kind));
  const sessions = sources.sessions.filter(s => parse(s.request_json).intake_task_id === task.id || links.some(l => l.hub_session_id === s.id || (l.native_id && l.native_id === parse(s.binding_json).native_id && (!l.host || l.host === s.machine_id) && linkMatchesIncarnation(l, parse(s.binding_json).native_created_at_ms))));
  const launches = sources.launches.filter(l => parse(l.request_json).intake_task_id === task.id || sessions.some(s => s.fleet_launch_id === l.id) || links.some(e => e.routing_job_id === l.id));
  const launchIds = new Set(launches.map(l => l.id));
  for (const launch of launches) {
    for (const related of sources.launches.filter(l => l.retried_launch_id === launch.id || launch.retried_launch_id === l.id)) {
      if (!launchIds.has(related.id)) { launchIds.add(related.id); launches.push(related); }
    }
  }
  const attempts: Obj[] = [];
  const seenNative = new Set<string>(), seenLaunch = new Set<string>(), seenRecords = new Set<string>();
  function add(s: Obj | null, launch: Obj | null, native: Obj | null, link?: Obj) {
    const recordId = s?.id ?? link?.hub_session_id ?? launch?.id ?? link?.routing_job_id;
    if (recordId && seenRecords.has(recordId)) return;
    if (recordId) seenRecords.add(recordId);
    const b = parse(s?.binding_json);
    const machine = s?.machine_id ?? launch?.machine_id ?? native?.machine_id ?? link?.host;
    const nativeId = b.native_id ?? launch?.native_id ?? native?.native_id ?? link?.native_id ?? null;
    const incarnation = statsTime(b.native_created_at_ms) ?? statsTime(native?.created_at_ms);
    const key = nativeId && machine ? machine + '/' + nativeId + '/' + (incarnation ?? 'unknown') : null;
    if (key && seenNative.has(key)) { if (launch) seenLaunch.add(launch.id); return; }
    if (key) seenNative.add(key);
    if (launch) seenLaunch.add(launch.id);
    const start = incarnation;
    const state = s?.state ?? launch?.state ?? native?.phase ?? 'unknown';
    const end = statsTime(b.retirement?.finished_at_ms) ?? statsTime(launch?.finished_at);
    const endBasis = statsTime(b.retirement?.finished_at_ms) !== null ? 'native_retirement' : end !== null ? 'hub_terminal_observation' : null;
    const wall = start !== null && end !== null && end >= start ? end - start : null;
    attempts.push({ id: s?.id ?? launch?.id ?? link?.hub_session_id ?? key ?? link?.id, native_id: nativeId,
      state, created_at: statsTime(s?.created_at ?? launch?.created_at) ?? incarnation, created_at_basis: s?.created_at || launch?.created_at ? 'hub_attempt_intent' : 'native_creation', started_at: start, finished_at: end,
      finished_at_basis: endBasis, wall_time_basis: wall !== null ? endBasis : null,
      wall_time_ms: wall, active_time_ms: null,
      engine: s?.engine ?? launch?.engine ?? native?.engine ?? null,
      account: launch?.account ?? native?.account ?? null,
      restarted: launch ? sources.launches.some(l => l.retried_launch_id === launch.id) : null,
      unknown_reasons: { ...(start === null ? { started_at: 'native_start_not_retained' } : {}),
        ...(end === null ? { finished_at: terminal.has(state) ? 'terminal_timestamp_not_retained' : 'session_not_finished' } : {}),
        ...(wall === null ? { wall_time_ms: 'complete_start_end_pair_unavailable' } : {}),
        ...(!launch ? { restarted: 'retry_history_not_retained' } : {}), active_time_ms: 'active_intervals_not_recorded' } });
  }
  function nativeFor(machine: any, id: any): Obj | null {
    const matches = sources.natives.filter(n => n.native_id === id && (!machine || machine === n.machine_id));
    return matches.length === 1 ? matches[0] : null;
  }
  for (const s of sessions) {
    const l = sources.launches.find(l => l.id === s.fleet_launch_id) ?? null;
    const b = parse(s.binding_json);
    const linked = links.find(e => e.hub_session_id === s.id);
    const n = nativeFor(s.machine_id ?? linked?.host, b.native_id ?? l?.native_id ?? linked?.native_id);
    // A later census incarnation cannot supply the old binding's account/start.
    const matchingNative = n && (statsTime(b.native_created_at_ms) === null || statsTime(b.native_created_at_ms) === statsTime(n.created_at_ms)) ? n : null;
    add(s, l, matchingNative, linked);
  }
  for (const l of launches) if (!seenLaunch.has(l.id)) add(null, l, nativeFor(l.machine_id, l.native_id));
  for (const l of links) {
    if (l.hub_session_id && sessions.some(s => s.id === l.hub_session_id)) continue;
    if (l.routing_job_id && seenLaunch.has(l.routing_job_id)) continue;
    if (l.native_id || l.hub_session_id || l.routing_job_id) {
      const n = nativeFor(l.host, l.native_id);
      add(null, null, n && linkMatchesIncarnation(l,n.created_at_ms) ? n : null, l);
    }
  }
  const created = statsTime(task.created_at);
  const doneEvents = history.filter(e => (e.kind === 'note' && /^DONE\b/m.test(e.text ?? '')) || (e.kind === 'update' && e.changes?.status === 'done'));
  const auditDone = (sources.audit ?? []).filter(e => e.task_id === task.id && e.to_status === 'done');
  const doneTimes = [...doneEvents, ...auditDone].map(e => statsTime(e.captured_at)).filter((n): n is number => n !== null && n <= now);
  const done = doneTimes.length ? Math.min(...doneTimes) : null;
  const starts = attempts.map(s => s.started_at).filter((n): n is number => n !== null && n <= now);
  const unknownStarts = attempts.some(a => a.started_at === null && (a.native_id !== null || a.state === 'unknown'));
  const unknownOutcomes = attempts.some(a => a.state === 'unknown');
  const unknownRetries = attempts.some(a => a.restarted === null);
  const first = starts.length && !unknownStarts ? Math.min(...starts) : null;
  const knownCounts = { sessions: attempts.filter(a => a.started_at !== null).length,
    failed: attempts.filter(a => ['failed','refused'].includes(a.state)).length,
    stopped: attempts.filter(a => ['stopped','cancelled'].includes(a.state)).length,
    restarted: attempts.filter(a => a.restarted === true).length };
  const diff = (a: number | null, b: number | null) => a !== null && b !== null && b >= a ? b - a : null;
  const sum = (field: string) => attempts.every(s => s[field] !== null) ? attempts.reduce((n, s) => n + s[field], 0) : null;
  const wall = sum('wall_time_ms'), active = sum('active_time_ms');
  const blockers = new Set(history.filter(e => e.kind === 'blocker').map(e => e.id));
  const dependencies = (sources.dependencies ?? []).filter(d => d.task_id === task.id);
  return { id: task.id, title: task.title, project: task.project, status: task.status, as_of: now,
    created_at: created, first_session_started_at: first, done_at: done,
    done_recorded: task.status === 'done' || doneEvents.length > 0 || auditDone.length > 0,
    lead_time_ms: diff(created, done), cycle_time_ms: diff(first, done),
    total_session_wall_time_ms: wall, total_session_active_time_ms: active,
    session_count: unknownStarts ? null : knownCounts.sessions, attempt_count: attempts.length,
    attempt_count_basis: 'retained_attempt_identities_including_unproven_links', known_counts: knownCounts,
    count_basis: 'Retained attempts include linked identities whose execution is unproven. Started sessions require native creation evidence. Outcome counts use retained states as of this snapshot; unknown histories leave totals null.',
    wall_time_basis: 'Native creation to retained native retirement, or to Hub terminal observation where native end is unavailable. This is recorded lifecycle wall time, not active time.',
    failed_attempts: unknownOutcomes ? null : knownCounts.failed,
    stopped_attempts: unknownOutcomes ? null : knownCounts.stopped,
    restarted_attempts: unknownRetries ? null : knownCounts.restarted,
    engines: [...new Set(attempts.map(s => s.engine).filter(Boolean))].sort(),
    accounts: [...new Set(attempts.map(s => s.account).filter(Boolean))].sort(),
    blocker_count: blockers.size + dependencies.length, blocker_event_count: blockers.size, blocker_link_count: dependencies.length,
    attempts, unknown_reasons: { ...(created === null ? { created_at: 'creation_timestamp_not_retained' } : {}),
      ...(first === null ? { first_session_started_at: 'no_proven_native_start' } : {}),
      ...(unknownStarts ? { session_count: 'one_or_more_linked_starts_unproven' } : {}),
      ...(unknownOutcomes ? { failed_attempts: 'one_or_more_attempt_outcomes_unknown', stopped_attempts: 'one_or_more_attempt_outcomes_unknown' } : {}),
      ...(unknownRetries ? { restarted_attempts: 'one_or_more_retry_histories_not_retained' } : {}),
      ...(done === null ? { done_at: task.status === 'done' ? 'done_timestamp_not_retained' : 'no_done_event' } : {}),
      ...(diff(created, done) === null ? { lead_time_ms: 'complete_created_done_pair_unavailable' } : {}),
      ...(diff(first, done) === null ? { cycle_time_ms: 'complete_start_done_pair_unavailable' } : {}),
      ...(wall === null ? { total_session_wall_time_ms: 'one_or_more_session_durations_unknown' } : {}),
      ...(active === null ? { total_session_active_time_ms: 'active_intervals_not_recorded' } : {}),
      ...(attempts.some(s => !s.account) ? { accounts: 'one_or_more_accounts_not_retained' } : {}),
      ...(attempts.some(s => !s.engine) ? { engines: 'one_or_more_engines_not_retained' } : {}) },
    provenance: 'retained_session_history_read_only_backfill' };
}
export function readStatsSources(sql: SqlStorage) {
  const exists = new Set(sql.exec("SELECT name FROM sqlite_master WHERE type='table'").toArray().map((r: any) => r.name));
  const rows = (name: string, fields: string[]): Obj[] => {
    if (!exists.has(name)) return [];
    const columns = new Set(sql.exec('PRAGMA table_info(' + name + ')').toArray().map((r: any) => r.name));
    const selected = fields.filter(f => columns.has(f)).map(f => f === 'request_json'
      ? "json_object('intake_task_id',json_extract(request_json,'$.intake_task_id')) AS request_json" : f);
    return sql.exec('SELECT ' + selected.join(',') + ' FROM ' + name).toArray();
  };
  return {
    sessions: rows('session_control_sessions', ['id','actor','machine_id','engine','state','created_at','binding_json','fleet_launch_id','request_json']),
    launches: rows('fleet_launches', ['id','actor','machine_id','native_id','engine','account','state','created_at','finished_at','retried_launch_id','request_json']),
    natives: rows('fleet_sessions', ['machine_id','native_id','engine','account','phase','created_at_ms']),
    dependencies: rows('intake_dependencies', ['task_id','dependency_id']),
    audit: rows('intake_audit', ['task_id','to_status','captured_at'])
  };
}
export function aggregateIntakeStats(items: Obj[], window: keyof typeof STATS_WINDOWS, now: number): Obj {
  const since = now - STATS_WINDOWS[window];
  const knownDone = items.filter(t => t.done_recorded || t.status === 'done' || t.done_at != null);
  const undatedDone = knownDone.filter(t => t.done_at == null || t.done_at > now);
  const datedDone = knownDone.filter(t => t.done_at != null && t.done_at >= since && t.done_at <= now);
  const done = window === 'all' ? knownDone : datedDone;
  const unknownCohort = window !== 'all' && undatedDone.length > 0;
  const undatedAttempts = items.reduce((n,t) => n + t.attempts.filter((a: Obj) => a.created_at == null || a.created_at > now).length,0);
  const leads = done.map(t => t.lead_time_ms).filter(n => n != null).sort((a, b) => a - b);
  const mid = Math.floor(leads.length / 2);
  const median = leads.length ? (leads.length % 2 ? leads[mid] : (leads[mid - 1] + leads[mid]) / 2) : null;
  const top = items.map(t => ({ id: t.id, title: t.title, attempt_count: t.attempts.filter((a: Obj) => window === 'all' || (a.created_at !== null && a.created_at >= since && a.created_at <= now)).length }))
    .filter(t => t.attempt_count > 0).sort((a, b) => b.attempt_count - a.attempt_count || a.id.localeCompare(b.id)).slice(0, 20);
  return { window, as_of: now, done_count: unknownCohort ? null : done.length,
    dated_done_count: datedDone.length, undated_done_count: undatedDone.length,
    undated_attempt_count: undatedAttempts, top_ranking_complete: window === 'all' || undatedAttempts === 0,
    measured_lead_count: leads.length, median_lead_time_ms: median,
    attempts_per_done: !unknownCohort && done.length ? done.reduce((n, t) => n + t.attempt_count, 0) / done.length : null,
    top_intakes_by_attempts: top, window_basis: 'DONE cohort for lead/attempts-per-DONE; attempt creation for top intakes. Attempts count retained identities, including linked attempts with unproven execution.',
    unknown_reasons: {
      ...(median === null ? { median_lead_time_ms: 'no_measured_done_lead_times' } : leads.length < done.length || unknownCohort ? { median_lead_time_ms: 'measured_subset_only_missing_completion_or_lead_times' } : {}),
      ...(unknownCohort ? { done_count: 'undated_done_intakes_cannot_be_assigned_to_window', attempts_per_done: 'undated_done_intakes_cannot_be_assigned_to_window' } : done.length ? {} : { attempts_per_done: 'no_done_intakes_in_window' }),
      ...(window !== 'all' && undatedAttempts ? { top_intakes_by_attempts: 'partial_ranking_undated_attempts_excluded' } : {}) } };
}

/** Caller supplies only tasks visible under current intake grants. */
export function intakeStatsTable(sql: SqlStorage, tasks: Obj[], url: URL, now: number, principal: Obj): Obj {
  const window = url.searchParams.get('window') ?? 'all';
  const sort = url.searchParams.get('sort') ?? 'attempt_count';
  const order = url.searchParams.get('order') ?? 'desc';
  if (!Object.hasOwn(STATS_WINDOWS, window)) throw new Error('invalid_stats_window');
  if (!['created_at', 'done_at', 'lead_time_ms', 'cycle_time_ms', 'attempt_count', 'blocker_count'].includes(sort)) throw new Error('invalid_stats_sort');
  if (!['asc', 'desc'].includes(order)) throw new Error('invalid_stats_order');
  const sourcesForTask = indexStatsSources(readStatsSources(sql));
  const items = tasks.filter(t => !url.searchParams.has('project') || t.project === url.searchParams.get('project')).map(t => projectVisibleIntakeStats(t, sourcesForTask(t), now, principal));
  items.sort((a, b) => a[sort] === null ? (b[sort] === null ? a.id.localeCompare(b.id) : 1) : b[sort] === null ? -1 : (a[sort] - b[sort]) * (order === 'asc' ? 1 : -1) || a.id.localeCompare(b.id));
  return { items: items.map(({attempts, ...s}) => s), total: items.length, sort, order,
    aggregates: aggregateIntakeStats(items, window as keyof typeof STATS_WINDOWS, now), as_of: now,
    authorized_scope: 'current_intake_grants', completeness: 'All authorized retained intakes; unknown historical measurements remain null.' };
}

/** Intake read grants do not grant access to another actor's session telemetry. */
export function projectVisibleIntakeStats(task: Obj, sources: ReturnType<typeof readStatsSources>, now: number, principal: Obj): Obj {
  if (principal.admin) return projectIntakeStats(task, sources, now);
  const links = (task.history ?? []).filter((e: Obj) => ['link','session'].includes(e.kind));
  const ownedSessions = sources.sessions.filter(s => s.actor === principal.actor);
  const hidden = sources.sessions.filter(s => s.actor !== principal.actor && links.some((l: Obj) => l.hub_session_id === s.id));
  const launches = sources.launches.filter(l => l.actor === principal.actor);
  const natives = sources.natives.filter(n => ownedSessions.some(s => s.machine_id === n.machine_id && parse(s.binding_json).native_id === n.native_id)
    || launches.some(l => l.machine_id === n.machine_id && l.native_id === n.native_id));
  const result = projectIntakeStats(task, { ...sources, sessions: [...ownedSessions, ...hidden.map(s => ({id:s.id}))], launches, natives }, now);
  if (hidden.length) {
    for (const field of ['first_session_started_at','cycle_time_ms','total_session_wall_time_ms','total_session_active_time_ms','session_count','failed_attempts','stopped_attempts','restarted_attempts']) {
      result[field] = null; result.unknown_reasons[field] = 'session_owner_required';
    }
    result.unknown_reasons.engines = 'one_or_more_sessions_not_visible';
    result.unknown_reasons.accounts = 'one_or_more_sessions_not_visible';
  }
  return result;
}

/** Build joins once for a table request; avoid re-parsing every session per intake. */
export function indexStatsSources(sources: ReturnType<typeof readStatsSources>) {
  const group = (rows: Obj[], key: (r: Obj) => any) => {
    const map = new Map<any, Obj[]>();
    for (const row of rows) { const k = key(row); if (k == null) continue; const bucket = map.get(k); if (bucket) bucket.push(row); else map.set(k,[row]); }
    return map;
  };
  const sessionsByTask = group(sources.sessions, s => parse(s.request_json).intake_task_id);
  const sessionsById = new Map(sources.sessions.map(s => [s.id,s]));
  const sessionsByNative = group(sources.sessions, s => parse(s.binding_json).native_id);
  const launchesByTask = group(sources.launches, l => parse(l.request_json).intake_task_id);
  const launchesById = new Map(sources.launches.map(l => [l.id,l]));
  const retryParents = group(sources.launches, l => l.retried_launch_id);
  const nativesById = group(sources.natives, n => n.native_id);
  const dependenciesByTask = group(sources.dependencies, d => d.task_id);
  const auditByTask = group(sources.audit, e => e.task_id);
  return (task: Obj) => {
    const sessions = new Set(sessionsByTask.get(task.id) ?? []);
    const launches = new Set(launchesByTask.get(task.id) ?? []);
    const nativeIds = new Set<string>();
    const addLaunch = (id: any) => { const l = launchesById.get(id); if (l) launches.add(l); };
    for (const l of task.history ?? []) {
      if (!['link','session'].includes(l.kind)) continue;
      const s = sessionsById.get(l.hub_session_id); if (s) sessions.add(s);
      if (l.native_id) {
        nativeIds.add(l.native_id);
        for (const s of sessionsByNative.get(l.native_id) ?? []) if (!l.host || l.host === s.machine_id) sessions.add(s);
      }
      addLaunch(l.routing_job_id);
    }
    for (const s of sessions) { addLaunch(s.fleet_launch_id); const n = parse(s.binding_json).native_id; if (n) nativeIds.add(n); }
    const pending = [...launches];
    for (let i=0;i<pending.length;i++) {
      const l = pending[i];
      const related = [...(retryParents.get(l.id) ?? []), launchesById.get(l.retried_launch_id)].filter(Boolean) as Obj[];
      for (const other of related) if (!launches.has(other)) { launches.add(other); pending.push(other); }
      if (l.native_id) nativeIds.add(l.native_id);
    }
    return { sessions:[...sessions], launches:[...launches], natives:[...nativeIds].flatMap(id => nativesById.get(id) ?? []),
      dependencies: dependenciesByTask.get(task.id) ?? [], audit: auditByTask.get(task.id) ?? [] };
  };
}
