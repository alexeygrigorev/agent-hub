import type { JsonRecord } from './types';

export type StatsIntakeRow = {
  id: string;
  project: string;
  title: string;
  status: string;
  created_at: number;
  updated_at: number;
  source_json?: string;
};

export type StatsSagaRow = {
  operation_id: string;
  session_id: string;
  machine_id: string;
  account_id: string | null;
  launch_spec_json: string;
  routing_json: string | null;
  retry_count: number;
  native_id: string | null;
  state: string;
  attempt: number;
  last_reason: string | null;
  created_at: number;
  updated_at: number;
};

export type StatsEventRow = {
  seq: number;
  actor_id: string;
  kind: string;
  body_json: string;
  at: number;
};

const parseJson = (raw: string | null | undefined): JsonRecord | null => {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : null;
  } catch {
    return null;
  }
};

const validTime = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;

const diffMs = (start: number | null, end: number | null): number | null =>
  start !== null && end !== null && end >= start ? end - start : null;

/** Read-only Hub2 projection for `GET /api/intakes/:id/stats` and `/api/intake/tasks/:id/stats`. */
export function buildIntakeStats(
  row: StatsIntakeRow,
  sagas: StatsSagaRow[],
  events: StatsEventRow[],
  dependencyCount: number,
  now = Date.now(),
): JsonRecord {
  const source = parseJson(row.source_json);
  const promotedAt = validTime(source?.promoted_at);
  const startedBySession = new Map<string, number>();
  const finishedBySession = new Map<string, { at: number; failed: boolean; stopped: boolean }>();
  const restartedSessions = new Set<string>();
  const doneTimes: number[] = [];
  let blockerEventCount = 0;

  for (const ev of events) {
    const body = parseJson(ev.body_json);
    const sid = typeof body?.session_id === 'string' ? body.session_id : null;
    const at = validTime(ev.at);
    if (ev.kind === 'native_session_started' && sid && at !== null) {
      const prior = startedBySession.get(sid);
      if (prior === undefined || at < prior) startedBySession.set(sid, at);
    } else if (
      ['finish_evidence_received', 'session_stopped', 'session_reaped', 'session_lost', 'session_launch_failed'].includes(ev.kind) &&
      sid &&
      at !== null
    ) {
      const failed = ev.kind === 'session_launch_failed' || ev.kind === 'session_lost' || body?.launch_failed === true;
      const stopped = ev.kind === 'session_stopped' || ev.kind === 'session_reaped';
      const prior = finishedBySession.get(sid);
      if (!prior || at >= prior.at) {
        finishedBySession.set(sid, { at, failed: failed || (prior?.failed ?? false), stopped: stopped || (prior?.stopped ?? false) });
      }
    } else if ((ev.kind === 'task_retry_started' || ev.kind === 'task_continuation_started') && sid) {
      restartedSessions.add(sid);
    } else if (ev.kind === 'worker_append') {
      const evidence = body?.evidence as JsonRecord | undefined;
      if (evidence && typeof evidence === 'object' && evidence.kind === 'task_blocked') blockerEventCount++;
    } else if (ev.kind === 'task_acceptance_verified' && at !== null) {
      doneTimes.push(at);
    } else if (ev.kind === 'intake_updated' && at !== null && ['accepted', 'archived'].includes(String(body?.status ?? ''))) {
      doneTimes.push(at);
    }
  }

  const attempts = sagas.map(saga => {
    const launch = parseJson(saga.launch_spec_json);
    const routing = parseJson(saga.routing_json);
    const picker = (routing?.picker && typeof routing.picker === 'object' ? routing.picker : null) as JsonRecord | null;
    const engine = typeof launch?.engine === 'string' ? launch.engine : typeof picker?.engine === 'string' ? picker.engine : null;
    const model = typeof launch?.model === 'string' ? launch.model : typeof picker?.model === 'string' ? picker.model : null;
    const account = saga.account_id ?? (typeof picker?.account_id === 'string' ? picker.account_id : null);
    const purpose = typeof launch?.purpose === 'string' ? launch.purpose : null;
    const startedAt = startedBySession.get(saga.session_id) ?? null;
    const finish = finishedBySession.get(saga.session_id);
    const finishedAt = finish?.at ?? (saga.state === 'complete' ? validTime(saga.updated_at) : null);
    const wallTimeMs = diffMs(startedAt, finishedAt);
    const restarted = saga.retry_count > 0 || restartedSessions.has(saga.session_id);
    const state = finish?.failed ? 'failed' : finish?.stopped ? 'stopped' : saga.state;
    const unknownReasons: Record<string, string> = { active_time_ms: 'active_intervals_not_recorded' };
    if (startedAt === null) unknownReasons.started_at = 'native_start_not_recorded';
    if (finishedAt === null) unknownReasons.finished_at = 'session_not_finished';
    if (wallTimeMs === null) unknownReasons.wall_time_ms = 'complete_start_end_pair_unavailable';
    return {
      id: saga.session_id,
      operation_id: saga.operation_id,
      native_id: saga.native_id,
      machine_id: saga.machine_id,
      state,
      created_at: validTime(saga.created_at),
      started_at: startedAt,
      finished_at: finishedAt,
      wall_time_ms: wallTimeMs,
      active_time_ms: null,
      engine,
      model,
      account,
      purpose,
      attempt: saga.attempt,
      retry_count: saga.retry_count,
      restarted,
      last_reason: saga.last_reason,
      unknown_reasons: unknownReasons,
    };
  });

  const createdAt = validTime(row.created_at);
  const startedTimes = attempts.map(a => a.started_at).filter((t): t is number => t !== null && t <= now);
  const firstSessionStartedAt = startedTimes.length ? Math.min(...startedTimes) : null;
  const doneRecorded = ['accepted', 'archived'].includes(row.status) || doneTimes.length > 0;
  const doneAt = doneTimes.length ? Math.min(...doneTimes) : null;
  const totalWallTimeMs = attempts.length > 0 && attempts.every(a => a.wall_time_ms !== null)
    ? attempts.reduce((sum, a) => sum + (a.wall_time_ms ?? 0), 0)
    : attempts.length === 0 ? 0 : null;
  const engines = [...new Set(attempts.map(a => a.engine).filter((e): e is string => !!e))].sort();
  const accounts = [...new Set(attempts.map(a => a.account).filter((a): a is string => !!a))].sort();
  const leadTimeMs = diffMs(createdAt, doneAt);
  const cycleTimeMs = diffMs(firstSessionStartedAt, doneAt);

  const unknownReasons: Record<string, string> = {
    total_session_active_time_ms: 'active_intervals_not_recorded',
  };
  if (createdAt === null) unknownReasons.created_at = 'creation_timestamp_not_retained';
  if (firstSessionStartedAt === null) unknownReasons.first_session_started_at = 'no_proven_native_start';
  if (doneAt === null) unknownReasons.done_at = doneRecorded ? 'done_timestamp_not_retained' : 'no_done_event';
  if (leadTimeMs === null) unknownReasons.lead_time_ms = 'complete_created_done_pair_unavailable';
  if (cycleTimeMs === null) unknownReasons.cycle_time_ms = 'complete_start_done_pair_unavailable';
  if (totalWallTimeMs === null) unknownReasons.total_session_wall_time_ms = 'one_or_more_session_durations_unknown';

  return {
    id: row.id,
    title: row.title,
    project: row.project,
    status: row.status,
    as_of: now,
    created_at: createdAt,
    updated_at: validTime(row.updated_at),
    promoted_at: promotedAt,
    first_session_started_at: firstSessionStartedAt,
    done_at: doneAt,
    done_recorded: doneRecorded,
    lead_time_ms: leadTimeMs,
    cycle_time_ms: cycleTimeMs,
    total_session_wall_time_ms: totalWallTimeMs,
    total_session_active_time_ms: null,
    session_count: attempts.filter(a => a.started_at !== null).length,
    attempt_count: attempts.length,
    failed_attempts: attempts.filter(a => a.state === 'failed').length,
    stopped_attempts: attempts.filter(a => a.state === 'stopped').length,
    restarted_attempts: attempts.filter(a => a.restarted).length,
    engines,
    accounts,
    blocker_count: blockerEventCount + dependencyCount,
    blocker_event_count: blockerEventCount,
    blocker_link_count: dependencyCount,
    attempts,
    unknown_reasons: unknownReasons,
    provenance: 'hub2_project_do_projection',
  };
}
