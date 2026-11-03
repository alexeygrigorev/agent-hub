import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { IntakeVerifier } from '../src/intake-verifier';

// Intake ccc50cca: Hub starts failed with dispatch_worktree_refused.
// Read-only verifier/triage sessions must never resume, create or lock the
// intake worktree held by a live implementer, nor reuse an implementer's
// -wt- worktree path (or nested subdirectory) as their workspace.
const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;
const source = { type: 'unknown', reference: null, timestamp: null };
const HOST = 'hetzner';
let seq = 0;

type Ctx = { hub: Hub; state: DurableObjectState };
async function fixture(fn: (c: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('ccc50cca-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => { await fn({ hub, state }); });
}
async function intake(c: Ctx, path: string, payload?: any): Promise<{ status: number; body: any }> {
  const res = await c.hub.intakeService.handle(new Request('https://hub.test/api/intake' + path, payload
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), async () => admin);
  return { status: res.status, body: await res.json() };
}
async function plan(c: Ctx, launchId: string): Promise<any> {
  const res = await c.hub.intakeService.handleWorktreeHost(new Request('https://hub.test/api/worktree-host/plan',
    { method: 'POST', body: JSON.stringify({ launch_id: launchId }) }), HOST, 'machine:' + HOST);
  return ((await res.json()) as any).plan;
}
async function createTask(c: Ctx, extra: any = {}): Promise<any> {
  const r = await intake(c, '/tasks', { key: 'ccc-create-' + ++seq, project: 'demo', title: 'Worktree refusal fixture',
    text: 'synthetic', source, acceptance_criteria: 'AC1 synthetic', ...extra });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}
function seedLaunch(c: Ctx, request: any, opts: { machine?: string | null; state?: string; launchState?: string } = {}) {
  const launchId = crypto.randomUUID(); const sessionId = crypto.randomUUID(); const at = new Date().toISOString();
  const machine = opts.machine === undefined ? HOST : opts.machine;
  const requestJson = JSON.stringify({ workspace: 'demo', cwd: null, ...request });
  c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
    profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
    'acct', 'p', 'headless', 'demo', '', ?, ?, ?)`, launchId, 'k-' + launchId, requestJson, machine ?? '', opts.launchState ?? 'offered', at, at);
  c.hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode,
    state, created_at, updated_at) VALUES (?, 'human:operator', ?, ?, ?, ?, 'claude', 'headless', ?, ?, ?)`,
    sessionId, 'sk-' + sessionId, requestJson, launchId, machine, opts.state ?? 'offered', at, at);
  return { launchId, sessionId, requestJson, at };
}

describe('read-only detached review sessions never take the intake worktree (ccc50cca)', () => {
  it('plans mode none for a detached review session even when the intake has an active worktree', async () => fixture(async c => {
    const t = await createTask(c);
    const worker = seedLaunch(c, { intake_task_id: t.id });
    const first = await plan(c, worker.launchId);
    expect(first.mode).toBe('create');
    const path = '/home/alexey/git/demo-' + first.suffix;
    const observed = await c.hub.intakeService.handleWorktreeHost(new Request('https://hub.test/api/worktree-host/observe',
      { method: 'POST', body: JSON.stringify({ launch_id: worker.launchId, action: 'created', path, branch: 'demo-' + first.suffix }) }),
      HOST, 'machine:' + HOST);
    expect(observed.status).toBe(200);
    // The implementer's own replacement still resumes in the recorded worktree.
    const replacement = seedLaunch(c, { intake_task_id: t.id });
    expect(await plan(c, replacement.launchId)).toMatchObject({ mode: 'resume', path });
    // A detached verifier/triage session for the same intake runs outside it.
    const review = seedLaunch(c, { intake_task_id: t.id, task_kind: 'review', intake_worktree: 'detached' });
    expect(await plan(c, review.launchId)).toEqual({ mode: 'none', task_id: t.id, detached: true });
    // And when no worktree exists yet, a detached review session does not create one.
    const untouched = await createTask(c);
    const review2 = seedLaunch(c, { intake_task_id: untouched.id, task_kind: 'review', intake_worktree: 'detached' });
    expect(await plan(c, review2.launchId)).toEqual({ mode: 'none', task_id: untouched.id, detached: true });
  }));

  it('never starts the verifier inside an implementer worktree or nested worktree subdirectory', async () => fixture(async c => {
    const t = await createTask(c, { verification: 'required' });
    const at = new Date(Date.now() - 120000).toISOString();
    for (const [id, workspace] of [
      ['impl-label-', 'demo'],
      ['impl-wt-', '/home/alexey/git/demo-wt-' + t.id.slice(0, 8)],
      ['impl-nested-', '/home/alexey/git/demo-wt-' + t.id.slice(0, 8) + '/src'],
    ]) {
      c.hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
        VALUES(?,'fixture',?,?,'codex','interactive','running',?,?)`, id + t.id, id + t.id,
        JSON.stringify({ intake_task_id: t.id, workspace }), id === 'impl-label-' ? at : new Date().toISOString(), at);
    }
    const starts: any[] = [];
    const v = new IntakeVerifier(c.hub.sql, c.hub.intakeService, (f: any) => f(), {
      start: async (input: any) => { starts.push(input); return { status: 202, body: { id: 'verifier-' + starts.length, state: 'queued' } }; },
      write: (path: string, payload: any) => intake(c, path, payload),
      alert: async () => {},
    }, () => Date.now());
    v.init();
    const task = (await intake(c, '/tasks/' + t.id)).body.task;
    const noted = await intake(c, '/tasks/' + t.id + '/events', { key: 'ccc-note-' + ++seq, expected_version: task.version,
      note: { text: 'DONE\nevidence: demo@0123456789abcdef pushed\nlive check: curl https://example.test/health -> 200\nresult: AC1 proven' } });
    expect(noted.status, JSON.stringify(noted.body)).toBe(200);
    c.hub.sql.exec('UPDATE intake_verifier_meta SET last_tick=NULL WHERE id=1');
    const result = await v.tick();
    expect(starts, JSON.stringify(result)).toHaveLength(1);
    expect(starts[0]).toMatchObject({ task_kind: 'review', intake_task_id: t.id, intake_worktree: 'detached', workspace: 'demo' });
    expect(starts[0].workspace).not.toMatch(/-wt-/);
  }));
});
