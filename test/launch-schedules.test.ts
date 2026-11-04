import {fixtureSql,freshFixtureBudgetDay} from './helpers/fixture-budget';
import { describe, it, expect, beforeEach } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { briefFor, cronNext, LaunchSchedulesService, parseCron, validTimezone } from '../src/launch_schedules';
import { doFetch } from './m1-focus-setup';

const defaultEnv = env as unknown as Env;
const ADMIN_TOKEN = 'op-token-schedules-test';
const ADMIN_HEADER = { Authorization: `Bearer ${ADMIN_TOKEN}`, 'Content-Type': 'application/json' };

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

const CLEANUP_TABLES = ['launch_schedules', 'launch_schedule_runs', 'session_control_sessions',
  'audit', 'intake_tasks', 'intake_task_projection', 'intake_audit', 'intake_note_archive'];

async function sha(): Promise<string> {
  return sha256Hex(ADMIN_TOKEN);
}

// --------------------------------------------------------------- cron core

describe('launch schedule cron core', () => {
  const MIN = 60000;
  it('parses five-field expressions and rejects everything else', () => {
    expect(parseCron('*/5 * * * *')).toMatchObject({ minutes: [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55] });
    expect(parseCron('0 9 1-7/2 * 1')).toMatchObject({ minutes: [0], hours: [9], months: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] });
    expect(parseCron('0 9 * * 7')!.dows).toEqual([0]); // 7 normalizes to Sunday
    expect(parseCron('30 2 * * *')!.doms).toBeNull();
    expect(parseCron('* * * *')).toBeNull(); // four fields
    expect(parseCron('* * * * * *')).toBeNull(); // six fields
    expect(parseCron('60 * * * *')).toBeNull();
    expect(parseCron('* 24 * * *')).toBeNull();
    expect(parseCron('*/0 * * * *')).toBeNull();
    expect(parseCron('@hourly')).toBeNull();
    expect(parseCron('5-3 * * * *')).toBeNull();
    expect(parseCron('mon * * * *')).toBeNull();
    expect(parseCron('*-2 * * * *')).toBeNull();
    expect(parseCron('')).toBeNull();
  });

  it('computes next UTC occurrences on whole minutes strictly after the cursor', () => {
    const cron = parseCron('*/5 * * * *')!;
    expect(cronNext(cron, 'UTC', Date.parse('2026-10-30T10:02:30Z'))).toBe(Date.parse('2026-10-30T10:05:00Z'));
    expect(cronNext(cron, 'UTC', Date.parse('2026-10-30T10:05:00Z'))).toBe(Date.parse('2026-10-30T10:10:00Z'));
    const daily = parseCron('0 9 * * 1')!; // Mondays 09:00
    expect(cronNext(daily, 'UTC', Date.parse('2026-10-30T12:00:00Z'))).toBe(Date.parse('2026-11-02T09:00:00Z'));
  });

  it('evaluates in the schedule timezone including Europe/Berlin DST', () => {
    const morning = parseCron('0 9 * * *')!;
    // 2026-10-30 is already CET (UTC+1): next 09:00 local is 08:00Z next day.
    expect(cronNext(morning, 'Europe/Berlin', Date.parse('2026-10-30T12:00:00Z'))).toBe(Date.parse('2026-10-31T08:00:00Z'));
    // Spring forward: 02:30 local does not exist on 2026-03-29 (02:00->03:00);
    // the day contributes no tick, the next one is Monday 02:30 CEST.
    const night = parseCron('30 2 * * *')!;
    expect(cronNext(night, 'Europe/Berlin', Date.parse('2026-03-29T00:00:00Z'))).toBe(Date.parse('2026-03-30T00:30:00Z'));
    // Fall back: 02:30 local happens twice on 2026-10-25 (03:00->02:00); the
    // first instantiation (CEST) ticks once.
    const sundays = parseCron('30 2 * * 0')!;
    expect(cronNext(sundays, 'Europe/Berlin', Date.parse('2026-10-24T12:00:00Z'))).toBe(Date.parse('2026-10-25T00:30:00Z'));
  });

  it('applies the vixie OR-rule when dom and dow are both restricted', () => {
    // 13th OR any Friday: the first Friday after Nov 1 2026 is Nov 6.
    const friday13 = parseCron('0 12 13 * 5')!;
    expect(cronNext(friday13, 'UTC', Date.parse('2026-11-01T00:00:00Z'))).toBe(Date.parse('2026-11-06T12:00:00Z'));
    const domOnly = parseCron('0 12 13 * *')!;
    expect(cronNext(domOnly, 'UTC', Date.parse('2026-11-01T00:00:00Z'))).toBe(Date.parse('2026-11-13T12:00:00Z'));
    const either = parseCron('0 0 1 * 1')!; // 1st of month OR any Monday
    expect(cronNext(either, 'UTC', Date.parse('2026-11-01T12:00:00Z'))).toBe(Date.parse('2026-11-02T00:00:00Z'));
  });

  it('never fires impossible dates and validates timezones', () => {
    expect(cronNext(parseCron('0 0 30 2 *')!, 'UTC', Date.parse('2026-01-01T00:00:00Z'))).toBeNull();
    expect(validTimezone('Europe/Berlin')).toBe(true);
    expect(validTimezone('UTC')).toBe(true);
    expect(validTimezone('Mars/Olympus')).toBe(false);
  });

  it('builds a session brief inside the session-control cap', () => {
    const prompt = 'x'.repeat(7000);
    const brief = briefFor(prompt, 'task-123', 'agent-hub', 'nightly', '2026-10-31T10:05:00.000Z');
    expect(brief.startsWith(prompt)).toBe(true);
    expect(brief).toContain('task-123');
    expect(brief).toContain('hub intake show task-123');
    expect(new TextEncoder().encode(brief).length).toBeLessThanOrEqual(8000);
  });
});

// ------------------------------------------------------- admin API (real Hub)

describe('launch schedules admin API', () => {
  let stub: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      await freshFixtureBudgetDay(instance);
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha() } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { fixtureSql(instance).exec(trimmed); } catch (e) {} }
      }
      try { instance.launchSchedules.init(); } catch (e) {}
      for (const table of CLEANUP_TABLES) {
        try { fixtureSql(instance).exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });

  const admin = (method: string, path: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method, headers: ADMIN_HEADER, body: body === undefined ? undefined : JSON.stringify(body),
  }));

  async function createSchedule(overrides: Record<string, unknown> = {}) {
    const res = await admin('POST', '/api/schedules', {
      name: 'daily-writer', project: 'agent-hub', cron: '*/5 * * * *', tz: 'Europe/Berlin',
      prompt: 'Write the daily note', acceptance_criteria: 'The note exists in docs/',
      ...overrides,
    });
    expect(res.status).toBe(201);
    return res.json() as Promise<any>;
  }

  it('requires admin authority', async () => {
    const res = await doFetch(new Request('http://example.com/api/schedules', { method: 'GET' }));
    expect(res.status).toBe(401);
  });

  it('creates a schedule with defaults and flags missing acceptance criteria', async () => {
    const created = await createSchedule();
    expect(created.name).toBe('daily-writer');
    expect(created.overlap).toBe('skip');
    expect(created.enabled).toBe(true);
    expect(created.task_kind).toBe('implement');
    expect(created.next_due_at).toBeTruthy();
    expect(created.acceptance_criteria_warning).toBeUndefined();
    const bare: any = await (await admin('POST', '/api/schedules', {
      name: 'no-ac', project: 'agent-hub', cron: '* * * * *', prompt: 'p',
    })).json();
    expect(bare.acceptance_criteria_warning).toContain('acceptance_criteria_required');
    expect(bare.tz).toBe('UTC');
  });

  it('rejects invalid create input and duplicate names', async () => {
    expect((await admin('POST', '/api/schedules', { name: 'Bad Name', project: 'p', cron: '* * * * *', prompt: 'x' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'bad project!', cron: '* * * * *', prompt: 'x' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * *', prompt: 'x' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *', prompt: 'x', tz: 'Nowhere/Nothing' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *', prompt: 'x', overlap: 'maybe' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *', prompt: 'x', task_kind: 'party' })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *', prompt: 'x'.repeat(7001) })).status).toBe(400);
    expect((await admin('POST', '/api/schedules', { name: 'ok-name', project: 'p', cron: '* * * * *', prompt: 'x', acceptance_criteria: 'y'.repeat(8193) })).status).toBe(400);
    await createSchedule({ name: 'dup' });
    expect((await admin('POST', '/api/schedules', { name: 'dup', project: 'p', cron: '* * * * *', prompt: 'x' })).status).toBe(409);
  });

  it('lists, shows with runs, pauses, resumes and deletes', async () => {
    const created = await createSchedule();
    const list: any = await (await admin('GET', '/api/schedules')).json();
    expect(list.schedules.map((s: any) => s.name)).toEqual(['daily-writer']);
    const shown: any = await (await admin('GET', `/api/schedules/${created.id}`)).json();
    expect(shown.runs).toEqual([]);
    const byName: any = await (await admin('GET', '/api/schedules/daily-writer')).json();
    expect(byName.id).toBe(created.id);
    const paused: any = await (await admin('POST', `/api/schedules/${created.id}/pause`, {})).json();
    expect(paused.enabled).toBe(false);
    expect(paused.next_due_at).toBeNull();
    const resumed: any = await (await admin('POST', `/api/schedules/daily-writer/resume`, {})).json();
    expect(resumed.enabled).toBe(true);
    expect(resumed.next_due_at).toBeTruthy();
    await runInDurableObject(stub, async (instance: Hub) => {
      fixtureSql(instance).exec(`INSERT INTO launch_schedule_runs (id, schedule_id, schedule_name, tick_at, fired_at, outcome, created_at)
        VALUES ('run-1', ?, 'daily-writer', '2026-10-31T10:05:00.000Z', '2026-10-31T10:05:01.000Z', 'held', '2026-10-31T10:05:01.000Z')`, created.id);
    });
    const runs: any = await (await admin('GET', `/api/schedules/daily-writer/runs?limit=10`)).json();
    expect(runs.runs.map((r: any) => r.outcome)).toEqual(['held']);
    const ticked = await admin('POST', `/api/schedules/${created.id}/tick`, {});
    expect(ticked.status).toBe(200);
    const deleted: any = await (await admin('DELETE', `/api/schedules/${created.id}`)).json();
    expect(deleted.deleted).toBe(true);
    expect((await admin('GET', `/api/schedules/${created.id}`)).status).toBe(404);
    await runInDurableObject(stub, async (instance: Hub) => {
      const runs = fixtureSql(instance).exec("SELECT * FROM launch_schedule_runs WHERE id = 'run-1'").toArray();
      expect(runs).toHaveLength(1); // run history outlives the schedule
    });
    await runInDurableObject(stub, async (instance: Hub) => {
      const audits = fixtureSql(instance).exec("SELECT action FROM audit WHERE resource = 'schedule'").toArray() as any[];
      expect(audits.map(a => a.action)).toEqual(expect.arrayContaining(['schedule.create', 'schedule.pause', 'schedule.resume', 'schedule.delete']));
    });
  });
});

// ------------------------------------------------------ tick engine (injected)

const inDO: any = runInDurableObject;

describe('launch schedule ticks', () => {
  const NOW = Date.parse('2026-10-31T10:07:00Z');
  const stub = () => (defaultEnv as any).HUB.get((defaultEnv as any).HUB.idFromName('hub'));

  async function fixture(fn: (h: any) => any) {
    return inDO(stub(), async (h: any) => {
      for (const table of CLEANUP_TABLES) { try { h.sql.exec(`DELETE FROM ${table}`); } catch (e) {} }
      return fn(h);
    });
  }

  function build(h: any, hooks: any, clock = { now: NOW }) {
    const svc = new LaunchSchedulesService(h.sql, (f: any) => f(), () => clock.now, hooks);
    svc.init();
    return svc;
  }

  function seedSchedule(h: any, overrides: Record<string, any> = {}) {
    const id = overrides.id || crypto.randomUUID();
    const now = new Date(NOW).toISOString();
    h.sql.exec(
      `INSERT INTO launch_schedules (id, name, project, cron, tz, prompt, acceptance_criteria, engine, tier, task_kind, workspace, overlap, enabled, created_by, created_at, updated_at, last_tick_ms, next_due_ms)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'op', ?, ?, ?, ?)`,
      id, overrides.name || 'tick-me', overrides.project || 'agent-hub', overrides.cron || '*/5 * * * *',
      overrides.tz || 'UTC', overrides.prompt || 'Do the scheduled work', overrides.acceptance_criteria ?? 'Done with evidence',
      overrides.engine ?? null, overrides.tier ?? null, overrides.task_kind || 'implement', overrides.workspace ?? null,
      overrides.overlap || 'skip', overrides.enabled === undefined ? 1 : overrides.enabled, now, now,
      overrides.last_tick_ms ?? NOW - 6 * 60000, overrides.next_due_ms ?? null);
    return id;
  }

  function seedSession(h: any, id: string, state = 'running') {
    const now = new Date(NOW).toISOString();
    h.sql.exec(
      `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
       VALUES (?, 'human:op', 'k', '{}', 'headless', ?, ?, ?)`, id, state, now, now);
  }

  function okHooks(intakes: any[] = [], sessions: any[] = []): any {
    return {
      createIntake: async (payload: any) => { intakes.push(payload); return { status: 200, body: { task: { id: 'task-' + intakes.length } } }; },
      startSession: async (input: any) => { sessions.push(input); return { status: 202, body: { id: 'sess-' + sessions.length, state: 'queued' } }; },
    };
  }

  it('stays quiet before the due minute and caches the next occurrence', async () => fixture(async h => {
    const clock = { now: NOW };
    seedSchedule(h, { last_tick_ms: NOW }); // just ticked: next due in 5 minutes
    const svc = build(h, okHooks(), clock);
    const result = await (svc as any).tickSchedule(h.sql.exec('SELECT * FROM launch_schedules').toArray()[0], clock.now, 'cron');
    expect(result).toMatchObject({ state: 'not_due', next_due_at: new Date(NOW + 3 * 60000).toISOString() });
    const row: any = h.sql.exec('SELECT * FROM launch_schedules').toArray()[0];
    expect(row.next_due_ms).toBe(NOW + 3 * 60000);
  }));

  it('fires exactly once per occurrence with intake and session linkage', async () => fixture(async h => {
    const clock = { now: NOW };
    const intakes: any[] = []; const sessions: any[] = [];
    seedSchedule(h, { engine: 'codex', tier: 'daily' });
    const svc = build(h, { ...okHooks(intakes, sessions) }, clock);
    const first = await svc.tick('cron');
    expect(first.schedules[0]).toMatchObject({ state: 'started', outcome: 'started', source: 'cron' });
    expect(intakes).toHaveLength(1);
    expect(intakes[0]).toMatchObject({ project: 'agent-hub', source: { type: 'schedule', reference: 'tick-me', timestamp: new Date(NOW - 2 * 60000).toISOString() } });
    expect(intakes[0].acceptance_criteria).toBe('Done with evidence');
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({
      mode: 'headless', task_kind: 'implement', workspace: 'agent-hub',
      name: 'tick-me.20261031T1005', intake_task_id: 'task-1', engine: 'codex', tier: 'daily',
    });
    expect(sessions[0].engine_override_reason).toContain('tick-me');
    expect(sessions[0].brief).toContain('task-1');
    const runs: any[] = h.sql.exec('SELECT * FROM launch_schedule_runs').toArray();
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({ outcome: 'started', intake_id: 'task-1', session_id: 'sess-1', tick_at: new Date(NOW - 2 * 60000).toISOString() });
    // The cursor advanced past the occurrence: an immediate re-tick is quiet.
    const second = await svc.tick('cron');
    expect(second.schedules[0]).toMatchObject({ state: 'not_due' });
    expect(sessions).toHaveLength(1);
  }));

  it('collapses missed occurrences into a single fire per pass', async () => fixture(async h => {
    const clock = { now: NOW }; const sessions: any[] = [];
    seedSchedule(h, { last_tick_ms: NOW - 47 * 60000 }); // cursor several occurrences behind
    const svc = build(h, { ...okHooks([], sessions) }, clock);
    const result = await svc.tick('cron');
    // One pass fires the first occurrence after the cursor (09:25) once; the
    // skipped ones collapse because the cursor jumps to the pass time.
    expect(result.schedules[0]).toMatchObject({ state: 'started', tick_at: new Date(NOW - 42 * 60000).toISOString() });
    expect(sessions).toHaveLength(1);
  }));

  it('records held, refused and error outcomes honestly', async () => fixture(async h => {
    const clock = { now: NOW };
    seedSchedule(h, { id: 's-held', name: 'held-one', last_tick_ms: NOW - 6 * 60000 });
    seedSchedule(h, { id: 's-ref', name: 'refused-one', last_tick_ms: NOW - 6 * 60000 });
    seedSchedule(h, { id: 's-err', name: 'error-one', last_tick_ms: NOW - 6 * 60000 });
    const svc = build(h, {
      createIntake: async () => ({ status: 200, body: { task: { id: 'task-x' } } }),
      startSession: async (input: any) => {
        if (input.name.startsWith('held-one')) return { status: 200, body: { id: 'sess-held', state: 'held', reason: 'no_machine_available' } };
        if (input.name.startsWith('refused-one')) return { status: 400, body: { error: 'engine_not_configured' } };
        return { status: 500, body: { error: 'boom' } };
      },
    }, clock);
    const result = await svc.tick('manual');
    const byName: any = Object.fromEntries(result.schedules.map((r: any): [string, any] => [r.schedule, r]));
    expect(byName['held-one']).toMatchObject({ state: 'held', outcome: 'held', reason: 'no_machine_available', source: 'manual' });
    expect(byName['refused-one']).toMatchObject({ state: 'refused', outcome: 'refused', reason: 'engine_not_configured' });
    expect(byName['error-one']).toMatchObject({ state: 'error', outcome: 'error', reason: 'boom' });
    const rows: any[] = h.sql.exec('SELECT * FROM launch_schedule_runs ORDER BY schedule_name').toArray();
    expect(rows.map(r => [r.outcome, r.reason, r.session_id])).toEqual([
      ['error', 'boom', null],
      ['held', 'no_machine_available', 'sess-held'],
      ['refused', 'engine_not_configured', null],
    ]);
  }));

  it('records an unknown admission outcome as an error, never as started', async () => fixture(async h => {
    const clock = { now: NOW }; const sessions: any[] = [];
    seedSchedule(h, {});
    const svc = build(h, {
      createIntake: async () => { throw new Error('lost'); },
      startSession: async (input: any) => { sessions.push(input); return { status: 202, body: {} }; },
    }, clock);
    const result = await svc.tick('cron');
    expect(result.schedules[0]).toMatchObject({ state: 'error', outcome: 'error', reason: 'intake_502:intake_outcome_unknown' });
    expect(sessions).toHaveLength(0); // no intake, no session start
  }));

  it('lets the real start gate refuse an intake without acceptance criteria', async () => fixture(async h => {
    const clock = { now: NOW }; const intakes: any[] = []; const sessions: any[] = [];
    seedSchedule(h, { acceptance_criteria: null });
    const svc = build(h, {
      createIntake: async (payload: any) => { intakes.push(payload); return { status: 200, body: { task: { id: 'task-noac' } } }; },
      startSession: async (input: any) => { sessions.push(input); return { status: 400, body: { error: 'acceptance_criteria_required' } }; },
    }, clock);
    const result = await svc.tick('cron');
    expect(result.schedules[0]).toMatchObject({ state: 'refused', reason: 'acceptance_criteria_required' });
    expect(intakes).toHaveLength(1); // the tick intake is still recorded
    expect(sessions).toHaveLength(1); // the start was attempted through the normal path
  }));

  it('skips while the previous run is live (default overlap policy)', async () => fixture(async h => {
    const clock = { now: NOW }; const sessions: any[] = [];
    const id = seedSchedule(h, {});
    seedSession(h, 'sess-live', 'running');
    h.sql.exec(`INSERT INTO launch_schedule_runs (id, schedule_id, schedule_name, tick_at, fired_at, session_id, outcome, created_at)
      VALUES ('run-live', ?, 'tick-me', '2026-10-31T10:00:00.000Z', '2026-10-31T10:00:01.000Z', 'sess-live', 'started', '2026-10-31T10:00:01.000Z')`, id);
    const svc = build(h, { ...okHooks([], sessions) }, clock);
    const result = await svc.tick('cron');
    expect(result.schedules[0]).toMatchObject({ state: 'skipped', reason: 'overlap_previous_live:sess-live' });
    expect(sessions).toHaveLength(0);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM launch_schedule_runs').one().n).toBe(2);
  }));

  it('queues at most one tick while live and dispatches it when the runway clears', async () => fixture(async h => {
    const clock = { now: NOW }; const intakes: any[] = []; const sessions: any[] = [];
    const id = seedSchedule(h, { overlap: 'queue' });
    seedSession(h, 'sess-live', 'running');
    h.sql.exec(`INSERT INTO launch_schedule_runs (id, schedule_id, schedule_name, tick_at, fired_at, session_id, outcome, created_at)
      VALUES ('run-live', ?, 'tick-me', '2026-10-31T10:00:00.000Z', '2026-10-31T10:00:01.000Z', 'sess-live', 'started', '2026-10-31T10:00:01.000Z')`, id);
    const svc = build(h, { ...okHooks(intakes, sessions) }, clock);
    const first = await svc.tick('cron');
    expect(first.schedules[0]).toMatchObject({ state: 'queued' });
    const parked: any = h.sql.exec("SELECT * FROM launch_schedule_runs WHERE outcome = 'pending'").toArray()[0];
    expect(parked.tick_at).toBe(new Date(NOW - 2 * 60000).toISOString());
    // Still live at the next occurrence: the fresh tick is skipped, the single
    // parked run stays pending.
    clock.now = NOW + 5 * 60000;
    const second = await svc.tick('cron');
    expect(second.schedules[0]).toMatchObject({ state: 'skipped' });
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM launch_schedule_runs WHERE outcome = 'pending'").one().n).toBe(1);
    // Previous session exits; the parked run dispatches in place (no new row).
    h.sql.exec("UPDATE session_control_sessions SET state = 'exited' WHERE id = 'sess-live'");
    clock.now = NOW + 10 * 60000;
    const third = await svc.tick('cron');
    expect(third.schedules[0]).toMatchObject({ state: 'started' });
    const runs: any[] = h.sql.exec('SELECT * FROM launch_schedule_runs ORDER BY created_at').toArray();
    expect(runs).toHaveLength(3); // run-live + the skipped fresh tick + the dispatched pending row (updated in place)
    const dispatched = runs.find(r => r.id === parked.id);
    expect(dispatched.outcome).toBe('started');
    expect(dispatched.session_id).toBeTruthy();
    expect(dispatched.intake_id).toBeTruthy();
    expect(dispatched.tick_at).toBe(new Date(NOW - 2 * 60000).toISOString());
  }));

  it('fires concurrently under overlap allow even while the previous run is live', async () => fixture(async h => {
    const clock = { now: NOW }; const sessions: any[] = [];
    const id = seedSchedule(h, { overlap: 'allow' });
    seedSession(h, 'sess-live', 'running');
    h.sql.exec(`INSERT INTO launch_schedule_runs (id, schedule_id, schedule_name, tick_at, fired_at, session_id, outcome, created_at)
      VALUES ('run-live', ?, 'tick-me', '2026-10-31T10:00:00.000Z', '2026-10-31T10:00:01.000Z', 'sess-live', 'started', '2026-10-31T10:00:01.000Z')`, id);
    const svc = build(h, { ...okHooks([], sessions) }, clock);
    const result = await svc.tick('cron');
    expect(result.schedules[0]).toMatchObject({ state: 'started' });
    expect(sessions).toHaveLength(1);
  }));

  it('pauses a schedule whose stored cron no longer parses', async () => fixture(async h => {
    const clock = { now: NOW };
    seedSchedule(h, { cron: 'not a cron' });
    const svc = build(h, okHooks(), clock);
    const result = await svc.tick('cron');
    expect(result.schedules[0]).toMatchObject({ state: 'invalid_cron_paused' });
    expect(h.sql.exec('SELECT enabled FROM launch_schedules').one().enabled).toBe(0);
  }));
});

// ---------------------------------------------- end-to-end through the real Hub

describe('launch schedules end-to-end through real admission', () => {
  let stub: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      await freshFixtureBudgetDay(instance);
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha() } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { fixtureSql(instance).exec(trimmed); } catch (e) {} }
      }
      for (const table of CLEANUP_TABLES) {
        try { fixtureSql(instance).exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });

  const admin = (method: string, path: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method, headers: ADMIN_HEADER, body: body === undefined ? undefined : JSON.stringify(body),
  }));

  it('a due tick creates the intake and enqueues a session through the normal path', async () => {
    const created: any = await (await admin('POST', '/api/schedules', {
      name: 'e2e-tick', project: 'agent-hub', cron: '*/5 * * * *', tz: 'UTC',
      prompt: 'Check the fleet page renders', acceptance_criteria: 'A screenshot is attached as evidence',
      overlap: 'skip',
    })).json();
    // Simulate the */5 cron having come due two minutes ago.
    await runInDurableObject(stub, async (instance: Hub) => {
      fixtureSql(instance).exec('UPDATE launch_schedules SET last_tick_ms = ? WHERE id = ?', Date.now() - 7 * 60000, created.id);
      const result = await instance.launchSchedules.tick('cron');
      expect(result.schedules).toHaveLength(1);
      expect(['held', 'started', 'queued']).toContain(result.schedules[0].state);
    });
    const runs: any = await (await admin('GET', `/api/schedules/${created.id}/runs`)).json();
    expect(runs.runs).toHaveLength(1);
    const run = runs.runs[0];
    expect(run.intake_id).toBeTruthy();
    expect(run.session_id).toBeTruthy();
    expect(['held', 'started']).toContain(run.outcome);
    await runInDurableObject(stub, async (instance: Hub) => {
      const task: any = fixtureSql(instance).exec('SELECT * FROM intake_tasks WHERE id = ?', run.intake_id).toArray()[0];
      expect(task).toBeTruthy();
      const data = JSON.parse(task.data);
      // Schedule provenance lives on the intake history entry that carried the prompt.
      const entry = (data.history || []).find((e: any) => e.source?.type === 'schedule');
      expect(entry).toBeTruthy();
      expect(entry.source.reference).toBe('e2e-tick');
      expect(entry.source.timestamp).toBe(run.tick_at);
      expect(data.acceptance_criteria).toBe('A screenshot is attached as evidence');
      const session: any = fixtureSql(instance).exec('SELECT * FROM session_control_sessions WHERE id = ?', run.session_id).toArray()[0];
      expect(session).toBeTruthy();
      expect(session.mode).toBe('headless');
      const request = JSON.parse(session.request_json);
      expect(request.intake_task_id).toBe(run.intake_id);
      expect(request.name).toMatch(/^e2e-tick\.\d{8}T\d{4}$/);
    });
    // Overlap skip: the held/queued session is still live, so an immediately
    // due next occurrence is skipped with the live session named.
    await runInDurableObject(stub, async (instance: Hub) => {
      fixtureSql(instance).exec('UPDATE launch_schedules SET last_tick_ms = ? WHERE id = ?', Date.now() - 7 * 60000, created.id);
      const result = await instance.launchSchedules.tick('cron');
      expect(result.schedules[0]).toMatchObject({ state: 'skipped' });
      expect(String(result.schedules[0].reason)).toContain('overlap_previous_live');
    });
  });

  it('a schedule without acceptance criteria is refused by the real start gate', async () => {
    const created: any = await (await admin('POST', '/api/schedules', {
      name: 'e2e-noac', project: 'agent-hub', cron: '* * * * *', tz: 'UTC',
      prompt: 'Say hello', overlap: 'allow',
    })).json();
    await runInDurableObject(stub, async (instance: Hub) => {
      // Make an occurrence already due (create set the cursor to now).
      fixtureSql(instance).exec('UPDATE launch_schedules SET last_tick_ms = ? WHERE id = ?', Date.now() - 2 * 60000, created.id);
      const result = await instance.launchSchedules.tick('cron');
      expect(result.schedules[0]).toMatchObject({ state: 'refused', reason: 'acceptance_criteria_required' });
    });
    const runs: any = await (await admin('GET', `/api/schedules/${created.id}/runs`)).json();
    expect(runs.runs[0]).toMatchObject({ outcome: 'refused', reason: 'acceptance_criteria_required' });
    expect(runs.runs[0].intake_id).toBeTruthy(); // intake recorded, session refused
    expect(runs.runs[0].session_id).toBeNull();
  });
});
