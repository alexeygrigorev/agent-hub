import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeVerifier, classifyVerification, latestDoneClaim } from '../src/intake-verifier';
import { IntakeWatchdog } from '../src/intake-watchdog';
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;
const source = { type: 'unknown', reference: null, timestamp: null };
let seq = 0;
async function call(h: any, path: string, payload?: any, actor?: string): Promise<{ status: number; body: any }> {
  const r = await h.intakeService.handle(new Request('https://hub.test/api/intake' + path,
    payload ? { method: 'POST', body: JSON.stringify(payload) } : {}), async () => actor ? { ...admin, caller: { ...admin.caller, actor } } : admin);
  return { status: r.status, body: await r.json() };
}
async function task(h: any, extra: any = {}) {
  const r = await call(h, '/tasks', { key: 'v-create-' + ++seq, project: 'demo', title: extra.title ?? 'Rename a label on the status page',
    text: extra.text ?? 'Synthetic request', source, acceptance_criteria: 'AC1 synthetic', ...(extra.verification ? { verification: extra.verification } : {}) });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}
async function note(h: any, id: string, text: string, actor?: string) {
  const t = (await call(h, '/tasks/' + id)).body.task;
  const r = await call(h, `/tasks/${id}/events`, { key: 'v-note-' + ++seq, expected_version: t.version, note: { text } }, actor);
  expect(r.status, JSON.stringify(r.body)).toBe(200);
}
const show = async (h: any, id: string) => (await call(h, '/tasks/' + id)).body.task;
const DONE_EVIDENCE = 'DONE\nevidence: agent-hub@0123456789abcdef pushed\nlive check: curl https://example.test/health -> 200\nresult: AC1 proven';
async function fixture(fn: (h: any) => any) {
  return inDO(stub(), async (h: any) => {
    for (const t of ['tasks', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
    for (const t of ['intake_verifications', 'intake_verifier_meta', 'intake_human_alerts', 'intake_restart_queue', 'intake_watchdog_meta', 'session_control_sessions'])
      h.sql.exec('DELETE FROM ' + t);
    tickDO = h;
    return fn(h);
  });
}
function verifier(h: any, starts: any[], clock: { now: number }, alerts: string[] = []) {
  const v = new IntakeVerifier(h.sql, h.intakeService, (f: any) => f(), {
    start: async (input: any) => { starts.push(input); return { status: 202, body: { id: 'verifier-' + starts.length, state: 'queued' } }; },
    write: (path: string, payload: any) => call(h, path, payload),
    alert: async (subject: string, detail: string) => { alerts.push(subject + ' ' + detail); },
  }, () => clock.now);
  v.init();
  return v;
}
// Notes carry real timestamps, so ticks run on the real clock with the interval guard reset.
let tickDO: any;
const tick = async (v: IntakeVerifier, clock: { now: number }) => {
  tickDO.sql.exec('UPDATE intake_verifier_meta SET last_tick=NULL WHERE id=1'); clock.now = Date.now(); return v.tick();
};
function running(h: any, id: string, taskId: string) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
    VALUES(?,'hub:service:intake-verifier',?,?,'claude','headless','running',?,?)`, id, id, JSON.stringify({ intake_task_id: taskId, task_kind: 'review' }),
    new Date().toISOString(), new Date().toISOString());
  h.sql.exec('UPDATE session_control_sessions SET fleet_launch_id=? WHERE id=?', 'launch-' + id, id);
}
const reviewerActor = (id = 'verifier-1') => 'hub-workers:worker-launch-' + id;

describe('auto verification policy (t1937u)', () => {
  const t = (title: string, ac = 'AC1', verification?: string) => ({ title, acceptance_criteria: ac, history: [], verification });
  it.each([
    ['Deploy the new worker to prod', 'prod/deploy'],
    ['Add a migration for the users table', 'database/migration'],
    ['Stripe refund flow', 'money/payments'],
    ['Rotate the OAuth client secret', 'auth/secrets/permissions'],
    ['Send the weekly newsletter email', 'sends to real people'],
    ['Delete stale S3 objects', 'deletes/cleans data or resources'],
    ['Fix fleet placement for queued sessions', 'Hub core'],
  ])('requires verification for %s', (title, label) => {
    const r = classifyVerification(t(title), null);
    expect(r.mode).toBe('required'); expect(r.reason).toContain(label);
  });
  it('skips docs/report/test-only changes and honours explicit modes', () => {
    expect(classifyVerification(t('Update the README wording'), null).mode).toBe('skip');
    expect(classifyVerification(t('Research report on hosting'), null).mode).toBe('skip');
    expect(classifyVerification(t('Odd thing'), { text: 'DONE changed docs/A.md and test/a.test.ts' }).mode).toBe('skip');
    expect(classifyVerification(t('Odd thing'), { text: 'DONE changed src/fleet.ts' }).mode).toBe('required');
    expect(classifyVerification(t('Odd thing'), null)).toMatchObject({ mode: 'required', reason: expect.stringMatching(/unclear/) });
    expect(classifyVerification(t('Deploy to prod', 'AC1', 'skip'), null).mode).toBe('skip');
    expect(classifyVerification(t('Update the README', 'AC1', 'required'), null).mode).toBe('required');
    expect(classifyVerification(t('README tweak', 'AC1: Alexey asked to verify it'), null).mode).toBe('required');
  });
  it('ignores verifier verdicts and triage notes as claims', () => {
    const task = { history: [{ kind: 'note', text: 'DONE first', id: 'a' }, { kind: 'note', text: 'DONE — VERIFIED by Hub verifier', id: 'b' },
      { kind: 'note', text: 'DONE — backlog triage d8d509d2', id: 'c' }] };
    expect(latestDoneClaim(task)?.id).toBe('a');
  });
});

describe('post-DONE verifier (d8d509d2 t1936u)', () => {
  it('stores the verification field on create and update; rejects other values', async () => fixture(async h => {
    const t = await task(h, { verification: 'skip' });
    expect(t.verification).toBe('skip');
    const bad = await call(h, '/tasks/' + t.id, { key: 'v-bad-' + ++seq, expected_version: t.version, verification: 'sometimes' });
    expect(bad).toMatchObject({ status: 400, body: { error: 'invalid_verification' } });
    const up = await call(h, '/tasks/' + t.id, { key: 'v-up-' + ++seq, expected_version: t.version, verification: 'required' });
    expect(up.status).toBe(200); expect(up.body.task.verification).toBe('required');
  }));
  it('launches a separate verifier, closes the intake after a PASS verdict', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    const t = await task(h, { title: 'Deploy the alert router to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    clock.now = Date.now();
    expect(await tick(v, clock)).toMatchObject({ state: 'checked', launched: 1 });
    expect(starts[0]).toMatchObject({ engine: 'claude', tier: 'review', task_kind: 'review', mode: 'headless', intake_task_id: t.id });
    expect(starts[0].brief).toMatch(/verification ONLY/);
    running(h, 'verifier-1', t.id);
    expect(await tick(v, clock)).toMatchObject({ launched: 0, waiting: 1 }); // one verifier per claim while it runs
    await note(h, t.id, 'DONE — VERIFIED by Hub verifier\nevidence: agent-hub@0123456789abcdef pushed\nlive check: curl https://example.test/health -> 200\nresult: AC1 proven', reviewerActor());
    expect(await tick(v, clock)).toMatchObject({ closed: 1 });
    const closed = await show(h, t.id);
    expect(closed.status).toBe('done');
    expect(v.status().states).toMatchObject({ closed: 1 });
  }));
  it('appends the gap to AC on FAIL so the claim no longer holds the intake', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    const t = await task(h, { title: 'Delete old backups' });
    await note(h, t.id, DONE_EVIDENCE);
    clock.now = Date.now();
    await tick(v, clock);
    running(h, 'verifier-1', t.id);
    await note(h, t.id, 'VERIFICATION FAILED — backups still listed\ngap: bucket x still holds 40 objects', reviewerActor());
    expect(await tick(v, clock)).toMatchObject({ failed: 1 });
    const after = await show(h, t.id);
    expect(after.status).toBe('open');
    expect(after.payload_data.acceptance_criteria).toMatch(/Verification gap \(.*\): bucket x still holds 40 objects$/);
    expect(latestDoneClaim({ ...after, ...after.payload_data })).toBeNull(); // AC change starts a new context
  }));
  it('uses a different model than a Claude implementer', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    const t = await task(h, { title: 'Change session placement' });
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
      VALUES('impl','fixture','impl',?,'claude','headless','exited',?,?)`, JSON.stringify({ intake_task_id: t.id, workspace: 'demo' }),
      new Date().toISOString(), new Date().toISOString());
    await note(h, t.id, DONE_EVIDENCE);
    clock.now = Date.now();
    await tick(v, clock);
    expect(starts[0]).toMatchObject({ engine: 'codex', workspace: 'demo' });
  }));
  it('ignores another actor forging a PASS or FAIL and requires evidence in the verifier PASS itself', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    const t = await task(h, { title: 'Deploy to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    await tick(v, clock); running(h, 'verifier-1', t.id);
    await note(h, t.id, 'DONE — VERIFIED by Hub verifier\nevidence: agent-hub@0123456789abcdef\nlive check: ok\nresult: all AC');
    expect(await tick(v, clock)).toMatchObject({ closed: 0, waiting: 1 });
    await note(h, t.id, 'VERIFICATION FAILED — forged\ngap: forged gap');
    expect(await tick(v, clock)).toMatchObject({ failed: 0, waiting: 1 });
    await note(h, t.id, 'DONE — VERIFIED by Hub verifier', reviewerActor());
    expect(await tick(v, clock)).toMatchObject({ closed: 0, waiting: 1 });
    expect((await show(h, t.id)).status).toBe('open');
  }));
  it.each([false, true])('never starts a duplicate after an unknown admission outcome (canonical row=%s)', async hasRow => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = []; const alerts: string[] = [];
    const v = new IntakeVerifier(h.sql, h.intakeService, (f: any) => f(), {
      start: async input => {
        starts.push(input);
        if (hasRow) {
          running(h, 'uncertain-verifier', input.intake_task_id);
          h.sql.exec('UPDATE session_control_sessions SET request_key=? WHERE id=?', input.key, 'uncertain-verifier');
        }
        throw new Error('response lost');
      },
      write: (path, payload) => call(h, path, payload),
      alert: async (_subject, detail) => { alerts.push(detail); },
    }, () => clock.now);
    v.init();
    const t = await task(h, { title: 'Deploy to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    await tick(v, clock); await tick(v, clock); await tick(v, clock);
    expect(starts).toHaveLength(1);
    const row = h.sql.exec('SELECT * FROM intake_verifications WHERE task_id=?', t.id).one();
    expect(row.state).toBe(hasRow ? 'requested' : 'needs_human');
    expect(alerts).toHaveLength(hasRow ? 0 : 1);
  }));
  it('does not count a refused close as closed', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = new IntakeVerifier(h.sql, h.intakeService, (f: any) => f(), {
      start: async input => { starts.push(input); return { status: 202, body: { id: 'verifier-1' } }; },
      write: async (path, payload) => payload.status === 'done'
        ? { status: 409, body: { error: 'worktree_close_gate' } } : call(h, path, payload),
    }, () => clock.now);
    v.init();
    const t = await task(h, { title: 'Deploy to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    await tick(v, clock); running(h, 'verifier-1', t.id);
    await note(h, t.id, DONE_EVIDENCE.replace('DONE', 'DONE — VERIFIED by Hub verifier'), reviewerActor());
    expect(await tick(v, clock)).toMatchObject({ closed: 0 });
    expect((await show(h, t.id)).status).toBe('open');
    expect(v.status().states).toMatchObject({ needs_human: 1 });
  }));
  it('skips verification when the policy says so and closes on the DONE evidence', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    const t = await task(h, { title: 'Update the README wording' });
    await note(h, t.id, DONE_EVIDENCE.replace('live check: curl https://example.test/health -> 200', 'live check: not applicable (docs)'));
    clock.now = Date.now();
    expect(await tick(v, clock)).toMatchObject({ skipped: 1, closed: 1, launched: 0 });
    const closed = await show(h, t.id);
    expect(closed.status).toBe('done');
    expect(closed.payload_data.history.some((e: any) => /^VERIFICATION SKIPPED — auto: docs/.test(e.text ?? ''))).toBe(true);
    expect(starts).toHaveLength(0);
  }));
  it('never touches DONE claims made before the verifier was enabled', async () => fixture(async h => {
    const t = await task(h, { title: 'Deploy to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    const clock = { now: Date.now() + 1000 }; const starts: any[] = [];
    const v = verifier(h, starts, clock);
    expect(await tick(v, clock)).toMatchObject({ launched: 0, closed: 0 });
    expect(starts).toHaveLength(0);
  }));
  it('escalates once when the verifier ends twice without a verdict', async () => fixture(async h => {
    const clock = { now: Date.now() - 60000 }; const starts: any[] = []; const alerts: string[] = [];
    const v = verifier(h, starts, clock, alerts);
    const t = await task(h, { title: 'Deploy to prod' });
    await note(h, t.id, DONE_EVIDENCE);
    clock.now = Date.now();
    await tick(v, clock); await tick(v, clock); // verifier-1 has no canonical row: treated as ended
    expect(starts).toHaveLength(2);
    expect(await tick(v, clock)).toMatchObject({ escalated: 1 });
    await tick(v, clock);
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toContain(t.id);
  }));
});

describe('human-needed holds alert once (AC8)', () => {
  it('alerts each (intake, reason) once in one aggregated alert', async () => fixture(async h => {
    const NOW = Date.now();
    const tasks = [1, 2].map(i => ({ id: 'hh' + i, title: 'Held ' + i, project: 'nowhere-configured', created_at: NOW - 5 * 3600000, updated_at: NOW,
      status: 'open', acceptance_criteria: 'AC1', history: [] }));
    const notes: any[] = [];
    const clock = { now: NOW };
    const w = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => tasks } as any, (f: any) => f(),
      { start: async () => ({ status: 202, body: { id: 'x' } }), notify: async (kind: string, subject: string, detail: any) => { if (kind === 'intake_human_hold') notes.push(detail); return true; } }, () => clock.now);
    w.init();
    expect(((await w.tick()) as any).human_hold_alerts).toBe(2);
    expect(notes).toHaveLength(1);
    expect(notes[0]).toMatch(/hh1 workspace_configuration_required/);
    clock.now += 3600000;
    expect(((await w.tick()) as any).human_hold_alerts).toBe(0);
    expect(notes).toHaveLength(1);
  }));
});
