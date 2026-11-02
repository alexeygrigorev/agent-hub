import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { finishBrief, MERGE_IDLE_MS, MERGE_MAX_REQUEUES, MergeGate, mergeGateTick } from '../src/merge-gate';

// Intake 2e00e01f: close gate, unmerged watcher, requeue. Synthetic Hub DOs only; never prod.
const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;
const AC = 'Synthetic AC: merged and deployed before close.';
const WT = '/home/alexey/git/demo-wt-bbbb2222';
const SHA = 'a'.repeat(40);
let seq = 0;
type Ctx = { hub: Hub };
async function fixture(fn: (c: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('merge-gate-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => { await fn({ hub }); });
}
async function intake(c: Ctx, path: string, payload?: any): Promise<{ status: number; body: any }> {
  const res = await c.hub.intakeService.handle(new Request('https://hub.test/api/intake' + path, payload
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), async () => admin);
  return { status: res.status, body: await res.json() };
}
async function createTask(c: Ctx) {
  const r = await intake(c, '/tasks', { key: 'create-' + ++seq, project: 'demo', title: 'Merge fixture', text: 'synthetic',
    source: { type: 'unknown', reference: null, timestamp: null }, acceptance_criteria: AC });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}
const version = async (c: Ctx, id: string) => (await intake(c, '/tasks/' + id)).body.task.version;
async function doneNote(c: Ctx, id: string) {
  const r = await intake(c, '/tasks/' + id + '/events', { key: 'note-' + ++seq, expected_version: await version(c, id),
    note: { text: `DONE ${SHA.slice(0, 7)} Live check: synthetic Result: ok` } });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
}
const close = async (c: Ctx, id: string, extra: any = {}) =>
  intake(c, '/tasks/' + id, { key: 'close-' + ++seq, expected_version: await version(c, id), status: 'done', ...extra });
const evidence = async (c: Ctx, id: string, body: any) => intake(c, '/tasks/' + id + '/merge-evidence', { key: 'ev-' + ++seq, ...body });
async function track(c: Ctx, id: string) {
  const r = await intake(c, '/tasks/' + id + '/worktrees', { key: 'reg-' + ++seq, worktree: { machine: 'hetzner', path: WT, branch: 'demo-wt-bbbb2222' } });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  // The worktree is merged and gone as far as the worktree gate is concerned; only the merge gate speaks.
  c.hub.sql.exec("UPDATE intake_worktrees SET state = 'removed' WHERE task_id = ?", id);
}

describe('AC1 close gate', () => {
  it('refuses close while commits are not in origin/main, naming them; deploy must be green', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id); await doneNote(c, t.id);
    const unknown = await close(c, t.id);
    expect(unknown.status).toBe(409); expect(unknown.body.error).toBe('merge_gate');
    expect((await evidence(c, t.id, { head: SHA, commits: [{ sha: SHA, in_main: false }], deploy: { state: 'pending' } })).status).toBe(200);
    const unmerged = await close(c, t.id);
    expect(unmerged.body.error).toBe('merge_gate');
    expect(unmerged.body.unmerged_commits).toEqual([SHA]);
    expect(unmerged.body.problems.join(' ')).toContain('aaaaaaaaaaaa');
    await evidence(c, t.id, { commits: [{ sha: SHA, in_main: true }], deploy: { state: 'failure', run: 'https://example.invalid/run/1' } });
    const red = await close(c, t.id);
    expect(red.body.error).toBe('merge_gate'); expect(red.body.problems.join(' ')).toContain('deploy not green: failure');
    await evidence(c, t.id, { commits: [{ sha: SHA, in_main: true }], deploy: { state: 'success', sha: SHA, run: 'https://example.invalid/run/2' } });
    const ok = await close(c, t.id);
    expect(ok.status, JSON.stringify(ok.body)).toBe(200); expect(ok.body.task.status).toBe('done');
  }));
  it('operator --force with a reason overrides and is recorded; closed_unverified is untouched', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id); await doneNote(c, t.id);
    expect((await close(c, t.id, { force: true })).body.error).toBe('force_reason_required');
    const forced = await close(c, t.id, { force: true, reason: 'synthetic override' });
    expect(forced.status, JSON.stringify(forced.body)).toBe(200);
    const history = (await intake(c, '/tasks/' + t.id)).body.task.payload_data.history;
    expect(history.find((e: any) => e.kind === 'close_gate_override').blocking[0].gate).toBe('merge');
  }));
  it('intakes that never tracked code close as before', async () => fixture(async c => {
    const t = await createTask(c); await doneNote(c, t.id);
    expect((await close(c, t.id)).status).toBe(200);
  }));
  it('rejects malformed evidence', async () => fixture(async c => {
    const t = await createTask(c);
    for (const bad of [{ commits: [{ sha: 'zz', in_main: true }] }, { deploy: { state: 'great' } }, { head: 'nope' }, { bogus: 1 }])
      expect((await evidence(c, t.id, bad)).status).toBe(400);
  }));
  it('shows merge_state on the intake and a global unmerged list', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id);
    await evidence(c, t.id, { commits: [{ sha: SHA, in_main: false }] });
    const shown = (await intake(c, '/tasks/' + t.id)).body.task;
    expect(shown.merge_state.state).toBe('tracking');
    expect(shown.merge_state.problems.length).toBeGreaterThan(0);
    expect((await intake(c, '/tasks/' + t.id + '/merge-state')).body.gate.ok).toBe(false);
    c.hub.sql.exec("UPDATE intake_merge_state SET state = 'unmerged' WHERE task_id = ?", t.id);
    const list = (await intake(c, '/merge-state')).body;
    expect(list.counts.total_unmerged).toBe(1); expect(list.unmerged[0].task_id).toBe(t.id);
  }));
});

function hooks(c: Ctx, over: any = {}) {
  const calls = { starts: [] as any[], notes: [] as string[], blockers: [] as string[], refresh: 0, alerts: [] as string[] };
  return { calls, hooks: {
    task: (id: string) => (c.hub.intakeService as any).get('tasks', id),
    sessionStates: () => [], sessionIds: () => [], refresh: () => { calls.refresh++; return true; },
    note: (_t: any, text: string) => { calls.notes.push(text); },
    start: async (input: any) => { calls.starts.push(input); return { status: 200, body: { id: crypto.randomUUID() } }; },
    blocker: async (_t: any, title: string) => { calls.blockers.push(title); return 'blocker-1'; },
    blockerOpen: () => true,
    alert: async (s: string) => { calls.alerts.push(s); },
    ...over } };
}
const age = (c: Ctx, id: string, ms: number) => {
  c.hub.sql.exec('UPDATE intake_merge_state SET activity_at = activity_at - ?, due_at = due_at - ?, evidence_at = evidence_at - ? WHERE task_id = ?', ms, ms, ms, id);
};

describe('AC4 reviewer-auth fallback', () => {
  it('requires canonical independent review, a distinct-provider retry, and a durable blocker on repeated auth failure', () => {
    const brief = finishBrief({ id: 'intake-1', title: 'Example' }, { path: WT, branch: 'demo-wt-bbbb2222', machine: 'hetzner' }, ['deploy pending']);
    expect(brief).toContain('hub sessions start --task-kind review --tier deep --headless');
    expect(brief).toContain('--intake-task-id intake-1 --intake-worktree detached');
    expect(brief).toContain('do not pin an engine or machine');
    expect(brief).toContain('immediately request one retry through canonical routing with a new request key');
    expect(brief).toContain('verify the retry uses a different provider engine/account');
    expect(brief).toContain('hub sessions show');
    expect(brief).toContain('Never reuse the failed provider/account');
    expect(brief).toContain('hub intake show intake-1');
    expect(brief).toContain('hub intake append intake-1 --version <current-version>');
    expect(brief).toContain('"kind":"blocker"');
    expect(brief).toContain('Run ONLY targeted unit tests for the files you changed locally');
    expect(brief).toContain('never run the full suite locally (it runs in CI)');
    expect(brief).toContain('gh pr merge --auto --rebase');
    expect(brief).toContain('Never push to main directly or approve deploy gates');
    expect(brief).toContain('after three unsuccessful finish-merge requeues the watcher creates a prerequisite and alert');
    expect(brief).toContain('hub intake blocker intake-1 --title');
  });
});

describe('AC2/AC3 watcher and requeue', () => {
  it('flags an idle unmerged intake once, requeues a finish-merge session, escalates to a blocker after the limit', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id);
    await evidence(c, t.id, { commits: [{ sha: SHA, in_main: false }] });
    const gate = c.hub.intakeService.mergeGate; const h = hooks(c);
    expect((await mergeGateTick(gate, h.hooks as any)).checked).toBe(0); // fresh activity: not due
    age(c, t.id, MERGE_IDLE_MS + 1000);
    const first = await mergeGateTick(gate, h.hooks as any);
    expect(first).toMatchObject({ flagged: 1, requeued: 1 });
    expect(h.calls.starts[0]).toMatchObject({ intake_task_id: t.id, cwd: WT, machine: 'hetzner' });
    expect(h.calls.starts[0].brief).toContain('gh pr merge --auto --rebase');
    expect(h.calls.starts[0].brief).not.toContain('Push to main');
    expect(gate.get(t.id)!.state).toBe('requeued');
    for (let i = 1; i < MERGE_MAX_REQUEUES; i++) { age(c, t.id, MERGE_IDLE_MS + 1000); await mergeGateTick(gate, h.hooks as any); }
    expect(gate.get(t.id)!.requeues).toBe(MERGE_MAX_REQUEUES);
    age(c, t.id, MERGE_IDLE_MS + 1000);
    expect((await mergeGateTick(gate, h.hooks as any)).blocked).toBe(1);
    expect(h.calls.blockers).toHaveLength(1); expect(gate.get(t.id)!.state).toBe('blocked'); expect(gate.get(t.id)!.blocker_id).toBe('blocker-1');
    expect(h.calls.alerts[0]).toContain(t.id);
    // Blocker resolved: tracking resumes with one more attempt.
    age(c, t.id, MERGE_IDLE_MS + 1000);
    await mergeGateTick(gate, hooks(c, { blockerOpen: () => false }).hooks as any);
    expect(gate.get(t.id)!.state).toBe('tracking');
  }));
  it('does not flag while a session is live, and unknown merge state is probed before failing closed', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id);
    const gate = c.hub.intakeService.mergeGate;
    age(c, t.id, MERGE_IDLE_MS + 1000);
    const live = hooks(c, { sessionStates: () => ['running'] });
    expect((await mergeGateTick(gate, live.hooks as any)).requeued).toBe(0);
    age(c, t.id, MERGE_IDLE_MS + 1000);
    const h = hooks(c);
    const probe = await mergeGateTick(gate, h.hooks as any);
    expect(probe.refreshed).toBe(1); expect(h.calls.starts).toHaveLength(0);
    age(c, t.id, MERGE_IDLE_MS);
    expect((await mergeGateTick(gate, h.hooks as any)).requeued).toBe(1);
  }));
  it('merged and deployed work is never flagged, and the close then goes through', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id); await doneNote(c, t.id);
    await evidence(c, t.id, { commits: [{ sha: SHA, in_main: true }], deploy: { state: 'success' } });
    expect(c.hub.intakeService.mergeGate.get(t.id)!.state).toBe('merged');
    age(c, t.id, MERGE_IDLE_MS * 3);
    expect((await mergeGateTick(c.hub.intakeService.mergeGate, hooks(c).hooks as any)).checked).toBe(0);
    expect((await close(c, t.id)).status).toBe(200);
  }));
  it('operator backfill evidence with idle:true is due immediately; workers cannot set it', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id);
    const r = await evidence(c, t.id, { commits: [{ sha: SHA, in_main: false }], idle: true });
    expect(r.status, JSON.stringify(r.body)).toBe(200);
    const h = hooks(c);
    expect(await mergeGateTick(c.hub.intakeService.mergeGate, h.hooks as any)).toMatchObject({ flagged: 1, requeued: 1 });
    expect((await evidence(c, t.id, { idle: 'yes' })).status).toBe(400);
  }));
  it('host worktree status feeds the tracked head and unmerged count', async () => fixture(async c => {
    const t = await createTask(c); await track(c, t.id);
    c.hub.sql.exec("UPDATE intake_worktrees SET state = 'active' WHERE task_id = ?", t.id);
    const gate: MergeGate = c.hub.intakeService.mergeGate;
    gate.noteStatus(t.id, { head: SHA, unmerged: 2, merged: false, branch: 'demo-wt-bbbb2222' });
    const v = gate.get(t.id)!;
    expect(v).toMatchObject({ head: SHA, unmerged: 2 });
    expect(v.problems.join(' ')).toContain('2 commit(s)');
  }));
});
