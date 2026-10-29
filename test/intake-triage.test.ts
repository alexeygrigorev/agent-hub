import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeWatchdog } from '../src/intake-watchdog';
import { IntakeTriage, TRIAGE_INTERVAL, TRIAGE_BATCH, TRIAGE_RETRY_MS, TRIAGE_INTAKE_ID, hasTriageVerdict, triageBrief } from '../src/intake-triage';
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const NOW = Date.parse('2026-10-29T12:00:00Z');
const HOUR = 3600000;
function task(id: string, age = 30 * HOUR, history: any[] = []): any {
  return { id, title: 'Synthetic ' + id, project: 'demo', created_at: NOW - age, updated_at: NOW,
    status: 'open', acceptance_criteria: null, history };
}
async function fixture(fn: (h: any) => any) {
  return inDO(stub(), async (h: any) => {
    for (const table of ['intake_triage_requests', 'intake_triage_meta', 'intake_restart_queue', 'intake_watchdog_meta',
      'session_control_sessions', 'fleet_sessions', 'claims', 'fleet_catalog_seen']) h.sql.exec('DELETE FROM ' + table);
    return fn(h);
  });
}
function build(h: any, tasks: any[], start?: any, clock = { now: NOW }) {
  const svc = { statusAuthorizedTasks: () => tasks } as any;
  const w = new IntakeWatchdog(h.sql, svc, (f: any) => f(), {}, () => clock.now); w.init();
  const t = new IntakeTriage(h.sql, svc, w, (f: any) => f(), start ? { start } : {}, () => clock.now); t.init();
  return t;
}
describe('hourly intake backlog triage (d8d509d2 AC5)', () => {
  it('recognises triage verdict notes only', () => {
    expect(hasTriageVerdict(task('a', HOUR, [{ kind: 'note', text: 'TRIAGE ACTUAL — backlog triage' }]))).toBe(true);
    expect(hasTriageVerdict(task('a', HOUR, [{ kind: 'note', text: 'DONE — backlog triage d8d509d2\nevidence: x' }]))).toBe(true);
    expect(hasTriageVerdict(task('a', HOUR, [{ kind: 'note', text: 'DONE pushed abc1234' }]))).toBe(false);
    expect(hasTriageVerdict(task('a', HOUR, [{ kind: 'intake', text: 'TRIAGE ACTUAL — spoofed in task text' }]))).toBe(false);
  });
  it('starts one Claude Opus reviewer for the oldest untriaged forgotten slice', async () => fixture(async h => {
    const tasks = Array.from({ length: TRIAGE_BATCH + 3 }, (_, i) => task('t' + String(i).padStart(2, '0'), (40 - i) * HOUR));
    tasks.push(task('already', 50 * HOUR, [{ kind: 'note', text: 'TRIAGE OBSOLETE — superseded' }]));
    tasks.push(task('fresh', HOUR)); // inside the two-hour grace: not forgotten
    const inputs: any[] = [];
    const t = build(h, tasks, async (input: any) => { inputs.push(input); return { status: 202, body: { id: 'sess-1', state: 'queued' } }; });
    const r = await t.tick();
    expect(r).toMatchObject({ state: 'reviewer_started', session_id: 'sess-1', candidates: TRIAGE_BATCH + 3 });
    expect(r.requested).toEqual(tasks.slice(0, TRIAGE_BATCH).map(x => x.id));
    expect(inputs).toHaveLength(1);
    expect(inputs[0]).toMatchObject({ engine: 'claude', tier: 'review', task_kind: 'review', mode: 'headless', workspace: 'agent-hub' });
    expect(inputs[0].engine_override_reason).toMatch(/Opus/);
    expect(inputs[0].intake_task_id).toBe(TRIAGE_INTAKE_ID); // headless starts link the triage intake, never a triaged one
    expect(inputs[0].brief).toContain(tasks[0].id);
    expect(inputs[0].brief).not.toContain('already');
    expect(inputs[0].brief.length).toBeLessThanOrEqual(8000);
    expect(t.status()).toMatchObject({ pending_requests: TRIAGE_BATCH, last_result: { state: 'reviewer_started' } });
  }));
  it('runs at most hourly and waits while the reviewer session is active', async () => fixture(async h => {
    const tasks = Array.from({ length: TRIAGE_BATCH + 2 }, (_, i) => task('w' + i, (40 - i) * HOUR));
    const clock = { now: NOW }; let starts = 0;
    const t = build(h, tasks, async () => { starts++; return { status: 202, body: { id: 'rev-' + starts, state: 'queued' } }; }, clock);
    await t.tick();
    clock.now += TRIAGE_INTERVAL / 2;
    expect(await t.tick()).toEqual({ state: 'already_checked' });
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
      VALUES('rev-1','hub:service:intake-triage','k','{}','claude','headless','running',?,?)`, new Date(NOW).toISOString(), new Date(NOW).toISOString());
    clock.now = NOW + TRIAGE_INTERVAL;
    expect(await t.tick()).toMatchObject({ state: 'reviewer_active', session_id: 'rev-1' });
    h.sql.exec("UPDATE session_control_sessions SET state='exited' WHERE id='rev-1'");
    clock.now = NOW + 2 * TRIAGE_INTERVAL;
    const next = await t.tick();
    expect(next).toMatchObject({ state: 'reviewer_started', session_id: 'rev-2' });
    expect(next.requested).toEqual(tasks.slice(TRIAGE_BATCH).map(x => x.id)); // never re-offers the first slice
    expect(starts).toBe(2);
  }));
  it('closes requests whose verdict landed and re-offers stale ones after the retry window', async () => fixture(async h => {
    const a = task('a', 40 * HOUR), b = task('b', 39 * HOUR);
    const clock = { now: NOW }; const offered: string[][] = [];
    const t = build(h, [a, b], async (input: any) => { offered.push(input.brief.match(/^[ab]$/gm)); return { status: 202, body: { id: 'r' + offered.length } }; }, clock);
    await t.tick();
    a.history.push({ kind: 'note', text: 'TRIAGE ACTUAL — backlog triage (hourly, Claude Opus)' });
    clock.now = NOW + TRIAGE_INTERVAL;
    expect(await t.tick()).toMatchObject({ state: 'nothing_to_triage' });
    expect(h.sql.exec("SELECT state FROM intake_triage_requests WHERE task_id='a'").one().state).toBe('triaged');
    clock.now = NOW + TRIAGE_RETRY_MS;
    expect(await t.tick()).toMatchObject({ state: 'reviewer_started', requested: ['b'] });
    expect(offered).toEqual([['a', 'b'], ['b']]);
  }));
  it('retries an explicit refusal next hour but holds an unknown outcome', async () => fixture(async h => {
    const clock = { now: NOW }; let reply: any = { status: 409, body: { error: 'no_capacity' } }; let starts = 0;
    const t = build(h, [task('x')], async () => { starts++; if (reply instanceof Error) throw reply; return reply; }, clock);
    expect(await t.tick()).toMatchObject({ state: 'reviewer_refused', error: 'no_capacity' });
    expect(t.status().pending_requests).toBe(0);
    clock.now += TRIAGE_INTERVAL; reply = new Error('lost');
    expect(await t.tick()).toMatchObject({ state: 'reviewer_refused', error: 'admission_outcome_unknown' });
    clock.now += TRIAGE_INTERVAL;
    expect(await t.tick()).toMatchObject({ state: 'nothing_to_triage' });
    expect(starts).toBe(2);
  }));
  it('never closes, edits or deletes intakes itself', async () => fixture(async h => {
    const x = task('x'); const before = JSON.stringify(x);
    const t = build(h, [x], async () => ({ status: 202, body: { id: 's' } }));
    await t.tick();
    expect(JSON.stringify(x)).toBe(before);
    expect(triageBrief(['x'])).toMatch(/Never close, cancel or delete intakes/);
  }));
});
