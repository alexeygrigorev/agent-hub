/** Hub cron launch schedules (intake a839dfb6): operator-configured cron
 * expressions in a named timezone that launch a fresh session per tick through
 * the ordinary admission path. Every due tick first creates a Hub intake
 * (source_type `schedule`) and then starts a headless session linked to it via
 * the canonical SessionControlService start — the same path every other Hub
 * service uses — so placement, capacity gates, holds and intake AC gates all
 * apply unchanged. A tick never bypasses admission: refused or held starts are
 * recorded as run outcomes with their reason.
 *
 * Each schedule carries an overlap policy evaluated against its own previous
 * run while that session is still live (queued/held/offered/starting/running/
 * uncertain): `skip` (default) records a skipped tick, `queue` parks at most
 * one pending tick that dispatches once the previous run is no longer live,
 * and `allow` fires concurrently. Run history (tick time, intake id, session
 * id, outcome) is durable and served per schedule for the dashboard.
 *
 * Cron evaluation uses minute-resolution next-fire arithmetic over the
 * schedule's IANA timezone (Intl), including DST: a wall-clock time skipped by
 * a spring-forward transition yields no tick that day; a fall-back repeat
 * ticks once. Missed occurrences (Hub down) collapse into the next due one.
 */
import type { SqlStorage } from '@cloudflare/workers-types';

export const LAUNCH_SCHEDULE_SCHEMA = `
CREATE TABLE IF NOT EXISTS launch_schedules (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  project TEXT NOT NULL,
  cron TEXT NOT NULL,
  tz TEXT NOT NULL,
  prompt TEXT NOT NULL,
  acceptance_criteria TEXT,
  engine TEXT,
  tier TEXT,
  task_kind TEXT NOT NULL DEFAULT 'implement',
  workspace TEXT,
  overlap TEXT NOT NULL DEFAULT 'skip',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_tick_ms INTEGER,
  next_due_ms INTEGER
);
CREATE TABLE IF NOT EXISTS launch_schedule_runs (
  id TEXT PRIMARY KEY,
  schedule_id TEXT NOT NULL,
  schedule_name TEXT NOT NULL,
  tick_at TEXT NOT NULL,
  fired_at TEXT NOT NULL,
  intake_id TEXT,
  session_id TEXT,
  outcome TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS launch_schedule_runs_recent ON launch_schedule_runs(schedule_id, created_at DESC);
`;

// ------------------------------------------------------------------ cron core

const WEEKDAYS: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
const FIELD_DOMAINS: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];

export interface CronFields {
  minutes: number[];
  hours: number[];
  months: number[];
  /** null means unrestricted (`*`); standard vixie OR-rule applies when both
   * day-of-month and day-of-week are restricted. */
  doms: number[] | null;
  dows: number[] | null;
}

function parseField(raw: string, index: number): { values: number[]; restricted: boolean } | null {
  const [lo, hi] = FIELD_DOMAINS[index];
  if (!raw.trim()) return null;
  const values = new Set<number>();
  let restricted = false;
  for (const part of raw.split(',')) {
    const match = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/.exec(part);
    if (!match) return null;
    const step = match[3] ? Number(match[3]) : 1;
    if (!Number.isInteger(step) || step < 1 || step > hi - lo + 1) return null;
    let start: number, end: number;
    if (match[1] === '*') {
      if (match[2] !== undefined) return null;
      start = lo; end = hi;
    } else {
      start = Number(match[1]);
      end = match[2] !== undefined ? Number(match[2]) : (match[3] !== undefined ? hi : start);
      if (match[2] === undefined && match[3] !== undefined) end = hi; // `N/step` = N-max/step
    }
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < lo || end > hi || start > end) return null;
    if (match[1] !== '*' || match[2] !== undefined) restricted = true;
    for (let v = start; v <= end; v += step) values.add(index === 4 && v === 7 ? 0 : v);
  }
  return { values: [...values].sort((a, b) => a - b), restricted };
}

/** Strict five-field cron (minute hour day-of-month month day-of-week).
 * Numbers only, `*`, lists, ranges and ` /step`; no names or macros. */
export function parseCron(expr: string): CronFields | null {
  if (typeof expr !== 'string') return null;
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const parsed: (ReturnType<typeof parseField>)[] = [];
  for (let i = 0; i < 5; i++) {
    const field = parseField(fields[i], i);
    if (!field) return null;
    parsed.push(field);
  }
  return {
    minutes: parsed[0]!.values,
    hours: parsed[1]!.values,
    doms: parsed[2]!.restricted ? parsed[2]!.values : null,
    months: parsed[3]!.values,
    dows: parsed[4]!.restricted ? parsed[4]!.values : null,
  };
}

function dayMatches(cron: CronFields, y: number, m: number, d: number, weekday: number): boolean {
  if (!cron.months.includes(m)) return false;
  const domHit = cron.doms !== null && cron.doms.includes(d);
  const dowHit = cron.dows !== null && cron.dows.includes(weekday);
  if (cron.doms !== null && cron.dows !== null) return domHit || dowHit;
  if (cron.doms !== null) return domHit;
  if (cron.dows !== null) return dowHit;
  return true;
}

interface LocalParts { y: number; m: number; d: number; h: number; min: number; weekday: number }

function localFormatter(tz: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23', weekday: 'short',
  });
}

function localParts(format: Intl.DateTimeFormat, ms: number): LocalParts {
  const parts: Record<string, string> = {};
  for (const part of format.formatToParts(ms)) parts[part.type] = part.value;
  return {
    y: Number(parts.year), m: Number(parts.month), d: Number(parts.day),
    h: Number(parts.hour), min: Number(parts.minute), weekday: WEEKDAYS[parts.weekday] ?? -1,
  };
}

function asUtc(p: LocalParts): number {
  return Date.UTC(p.y, p.m - 1, p.d, p.h, p.min);
}

/** Offset (milliseconds east of UTC) of `tz` at instant `ms`. */
function tzOffsetMs(format: Intl.DateTimeFormat, ms: number): number {
  return asUtc(localParts(format, ms)) - ms;
}

/** UTC instant whose local wall-clock time in `tz` is exactly
 * y-m-d h:min, or null when that wall time does not exist (DST gap). */
function utcFromLocal(format: Intl.DateTimeFormat, y: number, m: number, d: number, h: number, min: number): number | null {
  const naive = Date.UTC(y, m - 1, d, h, min);
  let guess = naive - tzOffsetMs(format, naive);
  for (let i = 0; i < 3; i++) {
    const refined = naive - tzOffsetMs(format, guess);
    if (refined === guess) break;
    guess = refined;
  }
  const check = localParts(format, guess);
  if (!(check.y === y && check.m === m && check.d === d && check.h === h && check.min === min)) return null;
  // A fall-back repeat maps two instants to the same wall clock; the fixed
  // point above lands on the later one, so prefer the earlier instantiation
  // when it shows the same wall time (conventional cron behavior).
  const earlier = guess - 3600000;
  const prev = localParts(format, earlier);
  if (prev.y === y && prev.m === m && prev.d === d && prev.h === h && prev.min === min) return earlier;
  return guess;
}

/** First instant strictly after `afterMs` matching the cron in `tz`, scanning
 * forward at most 1490 local days (covers the four-year leap cycle). */
export function cronNext(cron: CronFields, tz: string, afterMs: number, format?: Intl.DateTimeFormat): number | null {
  const fmt = format ?? localFormatter(tz);
  const start = Math.floor(afterMs / 60000) * 60000 + 60000; // first whole minute strictly after
  const first = localParts(fmt, start);
  // Step local days by UTC noon: noon-to-noon always crosses exactly one local
  // midnight, so the local date advances by exactly one day per step.
  let noon = Date.UTC(first.y, first.m - 1, first.d, 12);
  for (let day = 0; day < 1490; day++, noon += 86400000) {
    const dayParts = day === 0 ? first : localParts(fmt, noon);
    if (!dayMatches(cron, dayParts.y, dayParts.m, dayParts.d, dayParts.weekday)) continue;
    for (const h of cron.hours) {
      for (const min of cron.minutes) {
        // Physical verification decides, never wall-clock ordering: a spring
        // gap yields null here, a fall-back repeat resolves to its first
        // instantiation, and on DST days wall order can disagree with instant
        // order, so only `candidate > afterMs` may accept a fire.
        const candidate = utcFromLocal(fmt, dayParts.y, dayParts.m, dayParts.d, h, min);
        if (candidate !== null && candidate > afterMs) return candidate;
      }
    }
  }
  return null;
}

export function validTimezone(tz: string): boolean {
  try { localFormatter(tz); return true; } catch { return false; }
}

// ------------------------------------------------------------------- service

const NAME_RE = /^[a-z0-9][a-z0-9-]{1,39}$/;
const PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const OVERLAPS = new Set(['skip', 'queue', 'allow']);
const TASK_KINDS = new Set(['implement', 'review', 'ask', 'goal']);
const PROMPT_MAX_BYTES = 7000;
const AC_MAX_BYTES = 8192;
const BRIEF_LIMIT_BYTES = 8000; // session-control brief cap; footer must fit alongside the prompt
const REASON_MAX = 256;
const LIVE_STATES = new Set(['queued', 'held', 'offered', 'starting', 'running', 'uncertain']);
const STARTED_STATES = new Set(['offered', 'starting', 'running', 'queued']);
const RUNS_PAGE_MAX = 100;

const HEADERS = { 'Cache-Control': 'private, no-store' } as Record<string, string>;

type Obj = Record<string, any>;
type Auth = { caller?: { isAdmin?: boolean; actor?: string }; scopes?: string[]; error?: Response };

function json(data: any, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', ...HEADERS } });
}

function bytes(text: string): number { return new TextEncoder().encode(text).length; }

export function briefFor(prompt: string, intakeId: string, project: string, name: string, tickAt: string): string {
  return `${prompt}\n\nHub launch schedule "${name}" (tick ${tickAt}). This run is linked to Hub intake ${intakeId} (project ${project}). Read it with hub intake show ${intakeId}, keep it open, and append a DONE note with evidence once the scheduled prompt's work is complete and its acceptance criteria are met.`;
}

export type LaunchScheduleHooks = {
  createIntake: (payload: Obj) => Promise<{ status: number; body: Obj }>;
  startSession: (input: Obj) => Promise<{ status: number; body: Obj }>;
};

export class LaunchSchedulesService {
  private sql: SqlStorage;
  private txn: (fn: () => any) => any;
  private now: () => number;
  private hooks: LaunchScheduleHooks;

  constructor(sql: SqlStorage, txn: (fn: () => any) => any, now: () => number, hooks: LaunchScheduleHooks) {
    this.sql = sql;
    this.txn = txn;
    this.now = now;
    this.hooks = hooks;
  }

  init() {
    for (const statement of LAUNCH_SCHEDULE_SCHEMA.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }
  }

  private at(): string { return new Date(this.now()).toISOString(); }

  private audit(action: string, actor: string, target: string, detail: Obj) {
    try {
      this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
        actor, action, 'schedule', target, JSON.stringify(detail), this.at());
    } catch { /* audit is best-effort; schedule rows stay authoritative */ }
  }

  // ------------------------------------------------------------- admin API

  async handle(request: Request, auth: Auth): Promise<Response> {
    const caller = auth.caller;
    if (!caller?.isAdmin) return auth.error || json({ error: 'forbidden' }, 403);
    const url = new URL(request.url);
    const path = url.pathname;
    const actor = caller.actor || 'admin';

    if (path === '/api/schedules' && request.method === 'POST') {
      let input: any;
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return this.create(input, actor);
    }
    if (path === '/api/schedules' && request.method === 'GET') {
      const rows = this.sql.exec('SELECT * FROM launch_schedules ORDER BY created_at DESC, id LIMIT 200').toArray() as any[];
      return json({ schedules: rows.map(row => this.publicSchedule(row)) });
    }
    const match = /^\/api\/schedules\/([^/]+)(\/(runs|pause|resume|tick))?$/.exec(path);
    if (!match) return json({ error: 'not_found' }, 404);
    const schedule: any = this.sql.exec('SELECT * FROM launch_schedules WHERE id = ? OR name = ?', match[1], match[1]).toArray()[0];
    if (!schedule) return json({ error: 'not_found' }, 404);
    if (match[3] === 'runs' && request.method === 'GET') {
      const limit = Math.min(RUNS_PAGE_MAX, Math.max(1, Number(url.searchParams.get('limit')) || 50));
      const rows = this.sql.exec(
        'SELECT * FROM launch_schedule_runs WHERE schedule_id = ? ORDER BY created_at DESC, id DESC LIMIT ?', schedule.id, limit).toArray() as any[];
      return json({ runs: rows.map(row => this.publicRun(row)) });
    }
    if (match[3] === 'pause' && request.method === 'POST') {
      if (schedule.enabled) {
        this.txn(() => {
          // A paused schedule has no meaningful next occurrence; clear it so
          // the dashboard reads "paused" instead of a stale time.
          this.sql.exec('UPDATE launch_schedules SET enabled = 0, next_due_ms = NULL, updated_at = ? WHERE id = ?', this.at(), schedule.id);
          this.audit('schedule.pause', actor, schedule.name, { schedule_id: schedule.id });
        });
      }
      return json(this.publicSchedule(this.row(schedule.id)!));
    }
    if (match[3] === 'resume' && request.method === 'POST') {
      if (!schedule.enabled) {
        // Resume resets the cursor: a paused schedule never backfills the
        // occurrences that passed while it was disabled.
        const cron = parseCron(schedule.cron);
        const nextDue = cron ? cronNext(cron, schedule.tz, this.now()) : null;
        this.txn(() => {
          this.sql.exec('UPDATE launch_schedules SET enabled = 1, last_tick_ms = ?, next_due_ms = ?, updated_at = ? WHERE id = ?',
            this.now(), nextDue, this.at(), schedule.id);
          this.audit('schedule.resume', actor, schedule.name, { schedule_id: schedule.id });
        });
      }
      return json(this.publicSchedule(this.row(schedule.id)!));
    }
    if (match[3] === 'tick' && request.method === 'POST') {
      const result = await this.tickSchedule(this.row(schedule.id)!, this.now(), 'manual');
      return json(result);
    }
    if (request.method === 'GET') {
      const runs = this.sql.exec(
        'SELECT * FROM launch_schedule_runs WHERE schedule_id = ? ORDER BY created_at DESC, id DESC LIMIT 20', schedule.id).toArray() as any[];
      return json({ ...this.publicSchedule(schedule), runs: runs.map(row => this.publicRun(row)) });
    }
    if (request.method === 'DELETE') {
      return this.txn(() => {
        this.sql.exec('DELETE FROM launch_schedules WHERE id = ?', schedule.id);
        this.audit('schedule.delete', actor, schedule.name, { schedule_id: schedule.id });
        // Run history is kept: deleted schedules stay auditable through their runs.
        return json({ deleted: true, schedule_id: schedule.id });
      });
    }
    return json({ error: 'method_not_allowed' }, 405);
  }

  private row(id: string): Obj | undefined {
    return this.sql.exec('SELECT * FROM launch_schedules WHERE id = ?', id).toArray()[0] as Obj | undefined;
  }

  private iso(ms: number | null | undefined): string | null {
    return typeof ms === 'number' && Number.isFinite(ms) ? new Date(ms).toISOString() : null;
  }

  private publicSchedule(row: any): Obj {
    return {
      id: row.id, name: row.name, project: row.project, cron: row.cron, tz: row.tz,
      prompt: row.prompt, acceptance_criteria: row.acceptance_criteria ?? null,
      engine: row.engine ?? null, tier: row.tier ?? null, task_kind: row.task_kind,
      workspace: row.workspace ?? null, overlap: row.overlap, enabled: Boolean(row.enabled),
      created_by: row.created_by, created_at: row.created_at, updated_at: row.updated_at,
      last_tick_at: this.iso(row.last_tick_ms), next_due_at: this.iso(row.next_due_ms),
    };
  }

  private publicRun(row: any): Obj {
    return {
      id: row.id, schedule_id: row.schedule_id, schedule_name: row.schedule_name,
      tick_at: row.tick_at, fired_at: row.fired_at, intake_id: row.intake_id ?? null,
      session_id: row.session_id ?? null, outcome: row.outcome, reason: row.reason ?? null,
      created_at: row.created_at,
    };
  }

  private async create(input: any, actor: string): Promise<Response> {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return json({ error: 'invalid_request' }, 400);
    const name = typeof input.name === 'string' ? input.name : '';
    const project = typeof input.project === 'string' ? input.project : '';
    if (!NAME_RE.test(name)) return json({ error: 'invalid_name', detail: 'lowercase slug, 2-40 chars' }, 400);
    if (!PROJECT_RE.test(project)) return json({ error: 'invalid_project' }, 400);
    const cron = parseCron(typeof input.cron === 'string' ? input.cron : '');
    if (!cron) return json({ error: 'invalid_cron', detail: 'five-field cron: minute hour dom month dow' }, 400);
    const tz = input.tz === undefined || input.tz === null || input.tz === '' ? 'UTC' : input.tz;
    if (typeof tz !== 'string' || tz.length > 64 || !validTimezone(tz)) return json({ error: 'invalid_tz' }, 400);
    const prompt = typeof input.prompt === 'string' ? input.prompt : '';
    if (!prompt.trim()) return json({ error: 'invalid_prompt', detail: 'prompt is required' }, 400);
    if (bytes(prompt) > PROMPT_MAX_BYTES) return json({ error: 'invalid_prompt', detail: `prompt exceeds ${PROMPT_MAX_BYTES} bytes` }, 400);
    const acceptance = input.acceptance_criteria == null ? null : input.acceptance_criteria;
    if (acceptance !== null && (typeof acceptance !== 'string' || bytes(acceptance) > AC_MAX_BYTES)) {
      return json({ error: 'invalid_acceptance_criteria' }, 400);
    }
    const engine = input.engine == null || input.engine === '' ? null : input.engine;
    if (engine !== null && (typeof engine !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(engine))) return json({ error: 'invalid_engine' }, 400);
    const tier = input.tier == null || input.tier === '' ? null : input.tier;
    if (tier !== null && (typeof tier !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,63}$/i.test(tier))) return json({ error: 'invalid_tier' }, 400);
    const taskKind = input.task_kind === undefined ? 'implement' : input.task_kind;
    if (!TASK_KINDS.has(taskKind)) return json({ error: 'invalid_task_kind' }, 400);
    const workspace = input.workspace == null || input.workspace === '' ? null : input.workspace;
    if (workspace !== null && (typeof workspace !== 'string' || workspace.length > 128)) return json({ error: 'invalid_workspace' }, 400);
    const overlap = input.overlap === undefined ? 'skip' : input.overlap;
    if (!OVERLAPS.has(overlap)) return json({ error: 'invalid_overlap', detail: 'skip | queue | allow' }, 400);
    const enabled = input.enabled === undefined ? true : Boolean(input.enabled);

    const id = crypto.randomUUID();
    return this.txn(() => {
      if (this.sql.exec('SELECT id FROM launch_schedules WHERE name = ?', name).toArray().length) return json({ error: 'name_taken' }, 409);
      const at = this.at();
      const nowMs = this.now();
      const nextDue = enabled ? cronNext(cron, tz, nowMs) : null;
      this.sql.exec(
        `INSERT INTO launch_schedules (id, name, project, cron, tz, prompt, acceptance_criteria, engine, tier, task_kind, workspace, overlap, enabled, created_by, created_at, updated_at, last_tick_ms, next_due_ms)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        id, name, project, input.cron.trim(), tz, prompt, acceptance, engine, tier, taskKind, workspace, overlap, enabled ? 1 : 0, actor, at, at, nowMs, nextDue);
      this.audit('schedule.create', actor, name, { schedule_id: id, cron: input.cron.trim(), tz, project, overlap });
      const body: Obj = { ...this.publicSchedule(this.row(id)!), url_path: '/api/schedules/' + id };
      // The canonical start gate refuses headless sessions linked to an intake
      // without acceptance criteria; say so up front instead of failing at tick.
      if (!acceptance || !String(acceptance).trim()) {
        body.acceptance_criteria_warning = 'no acceptance criteria: ticks record the intake, but the session start refuses with acceptance_criteria_required until the schedule carries criteria';
      }
      return json(body, 201);
    });
  }

  // ------------------------------------------------------------------ ticks

  status(): Obj {
    const rows = this.sql.exec('SELECT COUNT(*) AS n FROM launch_schedules WHERE enabled = 1').toArray() as any[];
    return { enabled_schedules: Number(rows[0]?.n || 0) };
  }

  /** One evaluation pass over every enabled schedule. Each schedule advances a
   * durable cursor (last_tick_ms) to the pass time, so a cron occurrence fires
   * exactly once even across DO restarts; missed occurrences collapse. */
  async tick(source = 'cron'): Promise<Obj> {
    const nowMs = this.now();
    const schedules = this.sql.exec('SELECT * FROM launch_schedules WHERE enabled = 1 ORDER BY created_at, id').toArray() as any[];
    const results: Obj[] = [];
    for (const row of schedules) {
      let result: Obj;
      try {
        result = await this.tickSchedule(row, nowMs, source);
      } catch (e: any) {
        result = { schedule: row.name, state: 'error', error: String(e?.message || e).slice(0, 200) };
      }
      results.push(result);
    }
    return { source, at: this.at(), schedules: results };
  }

  /** Evaluate one schedule at `nowMs`: fire when a cron occurrence is due,
   * otherwise advance the cursor and cache the next due time. */
  private async tickSchedule(schedule: Obj, nowMs: number, source: string): Promise<Obj> {
    const cron = parseCron(schedule.cron);
    if (!cron) {
      // A stored cron can no longer parse only through manual tampering; keep
      // the schedule paused instead of firing on broken input.
      this.txn(() => this.sql.exec('UPDATE launch_schedules SET enabled = 0, updated_at = ? WHERE id = ?', this.at(), schedule.id));
      return { schedule: schedule.name, state: 'invalid_cron_paused' };
    }
    const cursor = typeof schedule.last_tick_ms === 'number' ? schedule.last_tick_ms : Date.parse(schedule.created_at);
    const due = cronNext(cron, schedule.tz, Number.isFinite(cursor) ? cursor : nowMs);
    const advance = () => {
      const nextDue = cronNext(cron, schedule.tz, nowMs);
      this.txn(() => this.sql.exec('UPDATE launch_schedules SET last_tick_ms = ?, next_due_ms = ?, updated_at = ? WHERE id = ?',
        nowMs, nextDue, this.at(), schedule.id));
      return nextDue;
    };
    if (due === null || due > nowMs) {
      // Nothing due: cache the next occurrence so the dashboard shows it.
      this.txn(() => this.sql.exec('UPDATE launch_schedules SET next_due_ms = ?, updated_at = ? WHERE id = ?', due, this.at(), schedule.id));
      return { schedule: schedule.name, state: 'not_due', next_due_at: this.iso(due) };
    }
    const occurrence = new Date(due).toISOString();
    const live = this.livePreviousRun(schedule.id);
    if (live && schedule.overlap === 'skip') {
      advance();
      const reason = `overlap_previous_live:${live.session_id}`;
      this.recordRun(schedule, occurrence, 'skipped', reason);
      return { schedule: schedule.name, state: 'skipped', reason, tick_at: occurrence };
    }
    if (live && schedule.overlap === 'queue') {
      const pending = this.pendingRun(schedule.id);
      if (pending) {
        advance();
        const reason = `overlap_queue_pending:${pending.id}`;
        this.recordRun(schedule, occurrence, 'skipped', reason);
        return { schedule: schedule.name, state: 'skipped', reason, tick_at: occurrence };
      }
      advance();
      const runId = crypto.randomUUID();
      this.writeRun(schedule, runId, occurrence, 'pending', 'overlap_queued_previous_live');
      return { schedule: schedule.name, state: 'queued', run_id: runId, tick_at: occurrence };
    }
    // Overlap clear (or `allow`): a parked tick dispatches before a fresh one.
    const pending = live ? null : this.pendingRun(schedule.id);
    const fireResult = await this.fire(schedule, pending
      ? { id: String(pending.id), tick_at: String(pending.tick_at), dispatch: true }
      : { id: crypto.randomUUID(), tick_at: occurrence }, nowMs);
    advance();
    return { schedule: schedule.name, state: fireResult.outcome, ...fireResult, tick_at: pending ? pending.tick_at : occurrence, source };
  }

  /** Newest run whose session is still live, else null. */
  private livePreviousRun(scheduleId: string): { session_id: string; state: string } | null {
    const rows = this.sql.exec(
      `SELECT r.session_id, s.state FROM launch_schedule_runs r
       JOIN session_control_sessions s ON s.id = r.session_id
       WHERE r.schedule_id = ? AND r.session_id IS NOT NULL
       ORDER BY r.created_at DESC, r.id DESC LIMIT 10`, scheduleId).toArray() as any[];
    for (const row of rows) {
      if (LIVE_STATES.has(String(row.state))) return { session_id: row.session_id, state: row.state };
    }
    return null;
  }

  private pendingRun(scheduleId: string): Obj | null {
    return (this.sql.exec(
      "SELECT * FROM launch_schedule_runs WHERE schedule_id = ? AND outcome = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1", scheduleId).toArray()[0] as Obj) || null;
  }

  /** Insert (or, for a dispatched pending tick, update) one run row. */
  private writeRun(schedule: Obj, runId: string, tickAt: string, outcome: string, reason: string | null,
                   intakeId: string | null = null, sessionId: string | null = null, update = false): void {
    const at = this.at();
    const boundedReason = reason ? String(reason).slice(0, REASON_MAX) : null;
    this.txn(() => {
      if (update) {
        this.sql.exec('UPDATE launch_schedule_runs SET outcome = ?, reason = ?, intake_id = ?, session_id = ?, fired_at = ? WHERE id = ?',
          outcome, boundedReason, intakeId, sessionId, at, runId);
      } else {
        this.sql.exec(
          `INSERT INTO launch_schedule_runs (id, schedule_id, schedule_name, tick_at, fired_at, intake_id, session_id, outcome, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          runId, schedule.id, schedule.name, tickAt, at, intakeId, sessionId, outcome, boundedReason, at);
      }
    });
  }

  private recordRun(schedule: Obj, tickAt: string, outcome: string, reason: string | null): string {
    const runId = crypto.randomUUID();
    this.writeRun(schedule, runId, tickAt, outcome, reason);
    return runId;
  }

  /** Create the tick intake and start the linked session through the canonical
   * admission path; record exactly one run row with the honest outcome. A run
   * row with `dispatch` set already exists (overlap `queue` parking) and is
   * updated in place instead of duplicated. */
  private async fire(schedule: Obj, run: { id: string; tick_at: string; dispatch?: boolean }, nowMs: number): Promise<Obj> {
    const runId = run.id;
    const intakePayload: Obj = {
      key: 'sched-' + runId,
      project: schedule.project,
      title: `${schedule.name} schedule run ${String(run.tick_at).replace(/[-:]/g, '').slice(0, 13)}`,
      text: schedule.prompt,
      acceptance_criteria: schedule.acceptance_criteria ?? null,
      source: { type: 'schedule', reference: schedule.name, timestamp: run.tick_at },
    };
    let intake: { status: number; body: Obj };
    try {
      intake = await this.hooks.createIntake(intakePayload);
    } catch {
      intake = { status: 502, body: { error: 'intake_outcome_unknown' } };
    }
    const intakeId = typeof intake.body?.task?.id === 'string' ? intake.body.task.id : null;
    if (intake.status >= 400 || !intakeId) {
      const reason = `intake_${intake.status}:${String(intake.body?.error || 'intake_failed')}`;
      const outcome = intake.status >= 500 ? 'error' : 'refused';
      this.writeRun(schedule, runId, run.tick_at, outcome, reason, null, null, run.dispatch === true);
      return { run_id: runId, outcome, reason };
    }

    const workspace = schedule.workspace || schedule.project;
    const brief = briefFor(String(schedule.prompt), intakeId, String(schedule.project), String(schedule.name), run.tick_at);
    if (bytes(brief) > BRIEF_LIMIT_BYTES) {
      // Create-time validation bounds the prompt so this cannot trigger for a
      // well-formed schedule; guard honestly anyway.
      this.writeRun(schedule, runId, run.tick_at, 'refused', 'prompt_too_long_for_brief', intakeId, null, run.dispatch === true);
      return { run_id: runId, outcome: 'refused', reason: 'prompt_too_long_for_brief', intake_id: intakeId };
    }
    const input: Obj = {
      key: 'schedrun-' + runId,
      mode: 'headless',
      task_kind: schedule.task_kind || 'implement',
      workspace,
      name: `${schedule.name}.${String(run.tick_at).replace(/[-:]/g, '').slice(0, 13)}`,
      brief,
      intake_task_id: intakeId,
      ...(schedule.engine ? {
        engine: schedule.engine,
        engine_override_reason: `hub launch schedule "${schedule.name}": operator-pinned engine`,
      } : {}),
      ...(schedule.tier ? { tier: schedule.tier } : {}),
    };
    let result: { status: number; body: Obj };
    try {
      result = await this.hooks.startSession(input);
    } catch {
      result = { status: 502, body: { error: 'admission_outcome_unknown' } };
    }
    // Admission status semantics: 409 with a session row means HELD (waiting
    // for placement), 202 queued, 2xx placed, 400 with state "refused" is an
    // explicit refusal. Outcome follows the returned session state, so a hold
    // is recorded as held — never as a refusal.
    const sessionId = typeof result.body?.id === 'string' && result.body.id ? result.body.id : null;
    let outcome: string;
    let reason: string | null = null;
    if (sessionId) {
      const state = String(result.body?.state || '');
      if (state === 'held') {
        outcome = 'held';
        reason = String(result.body?.reason || result.body?.hold?.reason || 'placement_hold');
      } else if (STARTED_STATES.has(state)) {
        outcome = 'started';
      } else if (state === 'refused') {
        outcome = 'refused';
        reason = String(result.body?.reason || result.body?.error || 'admission_refused');
      } else {
        outcome = state || 'unknown';
      }
    } else if (result.status >= 500) {
      outcome = 'error';
      reason = String(result.body?.error || 'admission_outcome_unknown');
    } else {
      outcome = 'refused';
      reason = String(result.body?.error || `start_${result.status}`);
    }
    this.writeRun(schedule, runId, run.tick_at, outcome, reason, intakeId, sessionId, run.dispatch === true);
    return { run_id: runId, outcome, reason, intake_id: intakeId, session_id: sessionId };
  }
}
