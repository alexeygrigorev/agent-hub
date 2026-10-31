import { describe, expect, it } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { SessionControlService } from '../src/session_control';
import { worktreePathAllowed } from '../src/intake-worktrees';

// Intake 4b161918: worktree registry, resume plan, close gate, host commands.
const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;
const AC = 'Synthetic AC: worktree merged and removed before close.';
const HOST = 'hetzner';
const OTHER = 'win35';
const WT = '/home/alexey/git/demo-wt-aaaa1111';
let seq = 0;

type Ctx = { hub: Hub; state: DurableObjectState };
async function fixture(fn: (c: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('intake-worktrees-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => { await fn({ hub, state }); });
}
async function intake(c: Ctx, path: string, payload?: any): Promise<{ status: number; body: any }> {
  const res = await c.hub.intakeService.handle(new Request('https://hub.test/api/intake' + path, payload
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), async () => admin);
  return { status: res.status, body: await res.json() };
}
async function host(c: Ctx, machine: string, path: string, payload?: any): Promise<{ status: number; body: any }> {
  const res = await c.hub.intakeService.handleWorktreeHost(new Request('https://hub.test/api/worktree-host' + path, payload
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), machine, 'machine:' + machine);
  return { status: res.status, body: await res.json() };
}
async function createTask(c: Ctx): Promise<any> {
  const r = await intake(c, '/tasks', { key: 'create-' + ++seq, project: 'demo', title: 'Worktree fixture', text: 'synthetic',
    source: { type: 'unknown', reference: null, timestamp: null }, acceptance_criteria: AC });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}
async function doneNote(c: Ctx, id: string) {
  const t = (await intake(c, '/tasks/' + id)).body.task;
  const r = await intake(c, '/tasks/' + id + '/events', { key: 'note-' + ++seq, expected_version: t.version,
    note: { text: 'DONE abc1234 Live check: synthetic Result: ok' } });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
}
async function close(c: Ctx, id: string, extra: any = {}) {
  const t = (await intake(c, '/tasks/' + id)).body.task;
  return intake(c, '/tasks/' + id, { key: 'close-' + ++seq, expected_version: t.version, status: 'done', ...extra });
}
async function register(c: Ctx, id: string, path = WT, machine = HOST, extra: any = {}) {
  return intake(c, '/tasks/' + id + '/worktrees', { key: 'reg-' + ++seq, worktree: { machine, path, branch: path.split('/').pop(), ...extra } });
}
/** A placed launch for an intake session on `machine` (synthetic rows only). */
function seedLaunch(c: Ctx, taskId: string | null, machine = HOST, request: any = {}): { launchId: string; sessionId: string } {
  const launchId = crypto.randomUUID(); const sessionId = crypto.randomUUID(); const at = new Date().toISOString();
  c.hub.sql.exec("INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, '{}', ?, 'codex', 'acct', 'p', 'headless', 'demo', '', 'offered', ?, ?)",
    launchId, 'k-' + launchId, machine, at, at);
  c.hub.sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at) VALUES (?, 'human:operator', ?, ?, ?, ?, 'codex', 'headless', 'offered', ?, ?)",
    sessionId, 'sk-' + sessionId, JSON.stringify({ workspace: 'demo', cwd: null, intake_task_id: taskId, ...request }), launchId, machine, at, at);
  return { launchId, sessionId };
}
const endSession = (c: Ctx, id: string, state = 'stopped') => c.hub.sql.exec('UPDATE session_control_sessions SET state = ? WHERE id = ?', state, id);
async function hostRun(c: Ctx, machine: string, result: (cmd: any) => any) {
  const offers = (await host(c, machine, '/commands')).body.commands;
  for (const cmd of offers) {
    const r = await host(c, machine, '/commands/' + cmd.id + '/result', result(cmd));
    expect(r.status, JSON.stringify(r.body)).toBe(200);
  }
  return offers;
}
const status = (over: any = {}) => ({ present: true, dirty: false, dirty_count: 0, unpushed: 0, unmerged: 0, merged: true,
  head: 'a'.repeat(40), branch: 'demo-wt-aaaa1111', checked_at: new Date().toISOString(), error: null, ...over });

describe('path guard (Hub side)', () => {
  it.each([
    ['/home/alexey/git/agent-hub-wt-close-gate', true],
    ['/home/u/git/x-wt-1', true],
    ['/home/alexey/git/agent-hub', false],
    ['/home/alexey/git/sub/agent-hub-wt-x', false],
    ['/home/alexey/src/agent-hub-wt-x', false],
    ['/home/alexey/git/prod-wt-x', false],
    ['/srv/prod/git/a-wt-b', false],
    ['/home/alexey/git/../git/a-wt-b', false],
    ['home/alexey/git/a-wt-b', false],
    ['/home/alexey/git//a-wt-b', false],
    ['/home/alexey/git/a-wt-b/', false],
  ])('%s -> %s', (path, allowed) => { expect(worktreePathAllowed(path)).toBe(allowed); });
});

describe('intake worktree registry', () => {
  it('records per-intake worktrees, shows them on intake show, and never shares one between intakes', async () => fixture(async c => {
    const a = await createTask(c); const b = await createTask(c);
    const reg = await register(c, a.id);
    expect(reg.status, JSON.stringify(reg.body)).toBe(200);
    expect(reg.body.worktree).toMatchObject({ machine: HOST, path: WT, branch: 'demo-wt-aaaa1111', state: 'active', origin: 'registered', status: null });
    const shown = (await intake(c, '/tasks/' + a.id)).body.task;
    expect(shown.worktrees).toHaveLength(1);
    expect(shown.close_gate.ok).toBe(false);
    expect(shown.payload_data.history.some((e: any) => e.kind === 'worktree' && e.action === 'registered')).toBe(true);
    // Same path, other intake: refused with a clear reason (server-side, plus unique index).
    const clash = await register(c, b.id);
    expect(clash.status).toBe(409);
    expect(clash.body).toMatchObject({ error: 'worktree_owned_by_other_intake', owner_task_id: a.id, path: WT });
    // Same path on a different host is a different worktree.
    expect((await register(c, b.id, WT, OTHER)).status).toBe(200);
    // Non-worktree paths (main checkout, /prod) are never recorded.
    expect((await register(c, b.id, '/home/alexey/git/demo')).body.error).toBe('worktree_path_refused');
    expect((await register(c, b.id, '/home/alexey/git/prod-wt-x')).body.error).toBe('worktree_path_refused');
    // Storage enforces one active owner too.
    expect(() => c.hub.sql.exec("INSERT INTO intake_worktrees (id, task_id, machine, path, created_at, updated_at, origin, state) VALUES ('x', ?, ?, ?, 0, 0, 'registered', 'active')", b.id, HOST, WT)).toThrow();
  }));

  it('session start refuses another intake\'s worktree and pins a follow-up start to the recorded host', async () => fixture(async c => {
    const a = await createTask(c); const b = await createTask(c);
    await register(c, a.id);
    const calls: any[] = [];
    const service = new SessionControlService(c.hub.sql, cb => c.state.storage.transactionSync(cb), Date.now, { reserve: (input: any) => {
      calls.push(input); return { value: { id: crypto.randomUUID(), state: 'offered', machine: input.machine || HOST, engine: 'codex' }, status: 201 };
    } });
    const start = async (body: any) => {
      const res = await service.handle(new Request('https://hub.test/api/sessions', { method: 'POST', body: JSON.stringify(body) }), admin);
      return { status: res.status, body: await res.json() as any };
    };
    const shared = await start({ key: 'share-1', workspace: 'demo', cwd: WT, mode: 'headless', brief: 'x', intake_task_id: b.id });
    expect(shared.status).toBe(409);
    expect(shared.body).toMatchObject({ error: 'worktree_owned_by_other_intake', owner_task_id: a.id });
    expect(calls).toHaveLength(0);
    const resumed = await start({ key: 'resume-1', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: a.id });
    expect(resumed.status, JSON.stringify(resumed.body)).toBe(201);
    expect(calls.at(-1).machine).toBe(HOST);
    // An explicit machine is kept (and the plan for that host creates a fresh worktree there).
    await start({ key: 'explicit-1', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: a.id, machine: OTHER });
    expect(calls.at(-1).machine).toBe(OTHER);
    // Intake without worktrees: placement untouched.
    await start({ key: 'free-1', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: b.id });
    expect(calls.at(-1).machine).toBeUndefined();
  }));
});

describe('detached intake-linked starts (intake 79458f2d)', () => {
  it('a triage/verifier start never resumes or pins the linked intake worktree a live owner holds', async () => fixture(async c => {
    const t = await createTask(c);
    await register(c, t.id);
    // A live owner session holds (and on the host has git-locked) the intake worktree.
    const owner = seedLaunch(c, t.id, HOST, { cwd: WT });
    c.hub.sql.exec("UPDATE session_control_sessions SET state = 'running' WHERE id = ?", owner.sessionId);
    const calls: any[] = [];
    const service = new SessionControlService(c.hub.sql, cb => c.state.storage.transactionSync(cb), Date.now, { reserve: (input: any) => {
      calls.push(input); return { value: { id: crypto.randomUUID(), state: 'offered', machine: input.machine || HOST, engine: 'claude' }, status: 201 };
    } });
    const start = async (body: any) => {
      const res = await service.handle(new Request('https://hub.test/api/sessions', { method: 'POST', body: JSON.stringify(body) }), admin);
      return { status: res.status, body: await res.json() as any };
    };
    const planFor = async (sessionId: string) => {
      const row = c.hub.sql.exec('SELECT request_json FROM session_control_sessions WHERE id = ?', sessionId).one() as any;
      const launch = seedLaunch(c, t.id, HOST, JSON.parse(row.request_json));
      return (await host(c, HOST, '/plan', { launch_id: launch.launchId })).body.plan;
    };
    // Reproduction: an ordinary linked start is pinned to the owner's host and
    // planned into the locked worktree, which the host refuses as dispatch_worktree_refused.
    const coupled = await start({ key: 'coupled-1', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: t.id, task_kind: 'review' });
    expect(coupled.status, JSON.stringify(coupled.body)).toBe(201);
    expect(calls.at(-1).machine).toBe(HOST);
    expect(await planFor(coupled.body.id)).toMatchObject({ mode: 'resume', path: WT });
    // Fix: the detached start keeps its intake link but runs in the workspace root.
    const detached = await start({ key: 'detached-1', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: t.id,
      task_kind: 'review', intake_worktree: 'detached' });
    expect(detached.status, JSON.stringify(detached.body)).toBe(201);
    expect(calls.at(-1).machine).toBeUndefined();
    const stored = JSON.parse((c.hub.sql.exec('SELECT request_json FROM session_control_sessions WHERE id = ?', detached.body.id).one() as any).request_json);
    expect(stored).toMatchObject({ intake_task_id: t.id, intake_worktree: 'detached', cwd: null });
    expect(await planFor(detached.body.id)).toEqual({ mode: 'none', task_id: t.id, detached: true });
    // Live detached sessions are never counted as bound to the intake worktree.
    c.hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE json_extract(request_json, '$.intake_worktree') IS NOT 'detached'");
    c.hub.sql.exec("UPDATE session_control_sessions SET state = 'running' WHERE json_extract(request_json, '$.intake_worktree') = 'detached'");
    const rm = await intake(c, '/tasks/' + t.id + '/worktrees/remove', { key: 'rm-' + ++seq });
    expect(rm.body.refused ?? [], JSON.stringify(rm.body)).toEqual([]);
    expect(rm.body.commands).toHaveLength(1);
    // Detached starts may not name a worktree themselves, and only 'detached' is a valid value.
    expect((await start({ key: 'detached-2', workspace: 'demo', cwd: WT, mode: 'headless', brief: 'x', intake_task_id: t.id, intake_worktree: 'detached' })).body)
      .toMatchObject({ error: 'invalid_session_request', field: 'intake_worktree', detail: 'detached_start_names_worktree' });
    // Nor any path inside or naming a worktree (review r1).
    for (const cwd of [WT + '/sub', 'demo-wt-aaaa1111', 'nested/x-wt-y']) {
      expect((await start({ key: 'detached-sub-' + ++seq, workspace: 'demo', cwd, mode: 'headless', brief: 'x', intake_task_id: t.id, intake_worktree: 'detached' })).body)
        .toMatchObject({ field: 'intake_worktree', detail: 'detached_start_names_worktree' });
    }
    expect((await start({ key: 'detached-3', workspace: 'demo', mode: 'headless', brief: 'x', intake_task_id: t.id, intake_worktree: 'shared' })).body)
      .toMatchObject({ error: 'invalid_session_request', field: 'intake_worktree' });
  }));
});

describe('resume plan + observe (host channel)', () => {
  it('creates once, then resumes in the recorded worktree; a missing one is recreated at the same path', async () => fixture(async c => {
    const t = await createTask(c);
    const first = seedLaunch(c, t.id);
    const plan1 = (await host(c, HOST, '/plan', { launch_id: first.launchId })).body.plan;
    expect(plan1).toEqual({ mode: 'create', task_id: t.id, suffix: 'wt-' + t.id.replace(/-/g, '').slice(0, 8) });
    const path = '/home/alexey/git/demo-' + plan1.suffix;
    const obs = await host(c, HOST, '/observe', { launch_id: first.launchId, action: 'created', path, branch: 'demo-' + plan1.suffix });
    expect(obs.body).toMatchObject({ recorded: true, worktree: { path, machine: HOST, origin: 'hub_created', session_ids: [first.sessionId] } });
    endSession(c, first.sessionId, 'stopped'); // interrupted
    const second = seedLaunch(c, t.id);
    const plan2 = (await host(c, HOST, '/plan', { launch_id: second.launchId })).body.plan;
    expect(plan2).toMatchObject({ mode: 'resume', path, branch: 'demo-' + plan1.suffix, worktree_id: obs.body.worktree.id });
    await host(c, HOST, '/observe', { launch_id: second.launchId, action: 'recreated', path, branch: 'demo-' + plan1.suffix });
    const rows = (await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees;
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ path, origin: 'hub_recreated', session_ids: [first.sessionId, second.sessionId], status: null });
    const history = (await intake(c, '/tasks/' + t.id)).body.task.payload_data.history.filter((e: any) => e.kind === 'worktree').map((e: any) => e.action);
    expect(history).toEqual(['created', 'recreated']);
    // Another host has no recorded worktree: it creates its own.
    const third = seedLaunch(c, t.id, OTHER);
    expect((await host(c, OTHER, '/plan', { launch_id: third.launchId })).body.plan.mode).toBe('create');
    // A host never plans or observes another host's launch.
    expect((await host(c, OTHER, '/plan', { launch_id: second.launchId })).status).toBe(404);
  }));

  it('records an explicit worktree cwd/workspace and leaves unlinked or relative-cwd launches alone', async () => fixture(async c => {
    const t = await createTask(c); const other = await createTask(c);
    const explicit = seedLaunch(c, t.id, HOST, { cwd: WT });
    expect((await host(c, HOST, '/plan', { launch_id: explicit.launchId })).body.plan).toMatchObject({ mode: 'none', recorded: WT });
    expect((await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees[0]).toMatchObject({ path: WT, origin: 'session_cwd' });
    const stolen = seedLaunch(c, other.id, HOST, { workspace: WT });
    expect((await host(c, HOST, '/plan', { launch_id: stolen.launchId })).body.plan).toMatchObject({ mode: 'none', refused: 'worktree_owned_by_other_intake' });
    expect((await host(c, HOST, '/plan', { launch_id: seedLaunch(c, null).launchId })).body.plan).toEqual({ mode: 'none' });
    expect((await host(c, HOST, '/plan', { launch_id: seedLaunch(c, other.id, HOST, { cwd: 'sub/dir' }).launchId })).body.plan).toMatchObject({ mode: 'none' });
    const failed = seedLaunch(c, other.id);
    expect((await host(c, HOST, '/observe', { launch_id: failed.launchId, action: 'failed', error: 'not a git repository' })).body.recorded).toBe(false);
    const ev = (await intake(c, '/tasks/' + other.id)).body.task.payload_data.history.find((e: any) => e.kind === 'worktree');
    expect(ev).toMatchObject({ action: 'create_failed', error: 'not a git repository' });
    // A guard-failing path reported by a host is refused, never recorded.
    const bad = seedLaunch(c, other.id);
    expect((await host(c, HOST, '/observe', { launch_id: bad.launchId, action: 'created', path: '/srv/prod/git/x-wt-y' })).body).toMatchObject({ recorded: false, error: 'worktree_path_refused' });
  }));
});

describe('close gate', () => {
  it('refuses close while a worktree is present, unmerged or unknown, listing each worktree; passes after removal', async () => fixture(async c => {
    const t = await createTask(c);
    await register(c, t.id);
    await doneNote(c, t.id);
    const unknown = await close(c, t.id);
    expect(unknown.status).toBe(409);
    expect(unknown.body.error).toBe('worktree_close_gate');
    expect(unknown.body.worktrees).toEqual([expect.objectContaining({ path: WT, machine: HOST, problems: ['status unknown (run hub worktree refresh)'] })]);
    // Refresh: host reports present + dirty + unmerged.
    const refresh = await intake(c, '/tasks/' + t.id + '/worktrees/refresh', { key: 'refresh-' + ++seq });
    expect(refresh.body.commands).toHaveLength(1);
    await hostRun(c, HOST, () => ({ outcome: 'ok', status: status({ dirty: true, dirty_count: 2, unmerged: 3, unpushed: 1, merged: false }) }));
    const present = await close(c, t.id);
    expect(present.status).toBe(409);
    expect(present.body.worktrees[0].problems).toEqual(['still present', 'dirty (2 uncommitted changes)', 'unpushed commits 1', 'unmerged commits 3']);
    // Remove via Hub: host reports ok -> state removed -> close succeeds.
    const remove = await intake(c, '/tasks/' + t.id + '/worktrees/remove', { key: 'rm-' + ++seq });
    expect(remove.body).toMatchObject({ refused: [], commands: [expect.objectContaining({ kind: 'remove', state: 'queued' })] });
    const ran = await hostRun(c, HOST, () => ({ outcome: 'ok', removed: true, branch_deleted: true, status: status({ present: false }) }));
    expect(ran[0]).toMatchObject({ kind: 'remove', path: WT, branch: 'demo-wt-aaaa1111' });
    const after = (await intake(c, '/tasks/' + t.id + '/worktrees')).body;
    expect(after.worktrees[0]).toMatchObject({ state: 'removed', last_remove: { result: 'ok', branch_deleted: true } });
    expect(after.close_gate).toEqual({ ok: true, blocking: [] });
    await doneNote(c, t.id);
    const closed = await close(c, t.id);
    expect(closed.status, JSON.stringify(closed.body)).toBe(200);
    expect(closed.body.task.status).toBe('done');
  }));

  it('a host status of absent + merged marks the worktree gone and lets close pass', async () => fixture(async c => {
    const t = await createTask(c);
    await register(c, t.id);
    await intake(c, '/tasks/' + t.id + '/worktrees/refresh', { key: 'refresh-' + ++seq });
    await hostRun(c, HOST, () => ({ outcome: 'ok', status: status({ present: false, merged: true }) }));
    expect((await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees[0].state).toBe('gone');
    await doneNote(c, t.id);
    expect((await close(c, t.id)).status).toBe(200);
  }));

  it('--force needs a reason; the override is recorded on history and the audit close row', async () => fixture(async c => {
    const t = await createTask(c);
    await register(c, t.id);
    await doneNote(c, t.id);
    expect((await close(c, t.id, { force: true })).body.error).toBe('force_reason_required');
    expect((await close(c, t.id, { force: true, reason: '   ' })).body.error).toBe('force_reason_required');
    const forced = await close(c, t.id, { force: true, reason: 'synthetic: host decommissioned' });
    expect(forced.status, JSON.stringify(forced.body)).toBe(200);
    const history = forced.body.task.payload_data.history;
    expect(history.find((e: any) => e.kind === 'close_gate_override')).toMatchObject({ reason: 'synthetic: host decommissioned', actor: 'human:operator',
      blocking: [expect.objectContaining({ path: WT })] });
    const audit = (await intake(c, '/tasks/' + t.id + '/audit')).body.items;
    expect(audit.find((e: any) => e.kind === 'close')).toMatchObject({ reason: 'force: synthetic: host decommissioned' });
  }));

  it('intakes without worktrees close exactly as before', async () => fixture(async c => {
    const t = await createTask(c);
    await doneNote(c, t.id);
    expect((await close(c, t.id)).status).toBe(200);
  }));
});

describe('remove action', () => {
  it('refuses on Hub when a live/held session is bound and records the refusal on the intake', async () => fixture(async c => {
    const t = await createTask(c);
    const live = seedLaunch(c, t.id, HOST, { cwd: WT });
    await host(c, HOST, '/plan', { launch_id: live.launchId });
    for (const state of ['running', 'held']) {
      endSession(c, live.sessionId, state);
      const r = await intake(c, '/tasks/' + t.id + '/worktrees/remove', { key: 'rm-' + ++seq });
      expect(r.status).toBe(200);
      expect(r.body.commands).toHaveLength(0);
      expect(r.body.refused[0]).toMatchObject({ result: 'refused', reason: `live session bound: ${live.sessionId} (${state})` });
    }
    const rows = (await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees;
    expect(rows[0].last_remove).toMatchObject({ result: 'refused', by: 'hub' });
    const history = (await intake(c, '/tasks/' + t.id)).body.task.payload_data.history.filter((e: any) => e.kind === 'worktree' && e.action === 'remove');
    expect(history).toHaveLength(2);
    // Stopped session: queued for the owning host only; by-session remove resolves the same worktree.
    endSession(c, live.sessionId, 'stopped');
    const bySession = await intake(c, '/worktrees/remove', { key: 'rm-' + ++seq, session_id: live.sessionId });
    expect(bySession.body).toMatchObject({ task_id: t.id, refused: [], commands: [expect.objectContaining({ machine: HOST, kind: 'remove' })] });
    expect((await host(c, OTHER, '/commands')).body.commands).toHaveLength(0);
    // Host refusal reason is recorded verbatim; the worktree stays active.
    await hostRun(c, HOST, () => ({ outcome: 'refused', reason: 'dirty: 1 uncommitted changes', status: status({ dirty: true, dirty_count: 1 }) }));
    const after = (await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees[0];
    expect(after).toMatchObject({ state: 'active', last_remove: { result: 'refused', reason: 'dirty: 1 uncommitted changes', by: 'host' } });
    const cmd = (await intake(c, '/worktree-commands/' + bySession.body.commands[0].id)).body.command;
    expect(cmd).toMatchObject({ state: 'done', result: { outcome: 'refused' } });
  }));

  it('dedupes pending commands, re-offers stale claims and rejects foreign result posts', async () => fixture(async c => {
    const t = await createTask(c);
    await register(c, t.id);
    const one = await intake(c, '/tasks/' + t.id + '/worktrees/refresh', { key: 'r1' });
    const two = await intake(c, '/tasks/' + t.id + '/worktrees/refresh', { key: 'r2' });
    expect(two.body.commands[0].id).toBe(one.body.commands[0].id);
    const offered = (await host(c, HOST, '/commands')).body.commands;
    expect(offered).toHaveLength(1);
    expect((await host(c, HOST, '/commands')).body.commands).toHaveLength(0); // claimed
    expect((await host(c, OTHER, '/commands/' + offered[0].id + '/result', { outcome: 'ok' })).status).toBe(404);
    c.hub.sql.exec('UPDATE intake_worktree_commands SET claimed_at = ? WHERE id = ?', Date.now() - 16 * 60000, offered[0].id);
    expect((await host(c, HOST, '/commands')).body.commands).toHaveLength(1);
    // Unknown fields in a host status are dropped; unknown stays null.
    await host(c, HOST, '/commands/' + offered[0].id + '/result', { outcome: 'ok', status: { present: true, merged: 'yes', unmerged: -1, secret: 'x' } });
    const s = (await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees[0].status;
    expect(s).toMatchObject({ present: true, merged: null, unmerged: null, dirty: null });
    expect(s.secret).toBeUndefined();
  }));
});

describe('HTTP mount', () => {
  it('the host channel refuses non-host callers', async () => {
    const res = await SELF.fetch('https://hub.test/api/worktree-host/commands');
    expect([401, 403]).toContain(res.status);
  });
});
