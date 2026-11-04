import { advanceFixtureBudgetDay, fixtureSql } from './helpers/fixture-budget';
import { beforeEach, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { IntakeDependencies } from '../src/intake-dependencies';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
let cookie: string;
async function api(path: string, payload?: any) {
  const response = await SELF.fetch('https://hub.test/api' + path, {
    method: payload ? 'POST' : 'GET', headers: { Cookie: cookie, 'x-csrf-token': 'csrf' },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
  return { status: response.status, data: await response.json() as any };
}
async function create(project = 'demo') {
  const result = await api('/intake/tasks', { key: crypto.randomUUID(), project, title: 'Tablet installation',
    text: 'Install on tablet', source: { type: 'unknown' }, acceptance_criteria: 'Installation succeeds.' });
  expect(result.status, JSON.stringify(result.data)).toBe(200);
  return result.data.task;
}

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true,
    csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
  await inDO(stub(), async (h: any,state:any) => {
    await advanceFixtureBudgetDay(h);
    const seedSql=fixtureSql(h,state);
    const graph = new IntakeDependencies(h.sql); graph.init();
    seedSql.exec('DELETE FROM intake_dependencies');
    for (const id of ['dep-a', 'dep-b', 'dep-c']) seedSql.exec('INSERT OR REPLACE INTO intake_tasks VALUES(?,?)', id,
      JSON.stringify({ id, project: 'demo', title: id, status: 'open', history: [] }));
  });
});

it('atomically reports a blocker, refuses original starts and releases on DONE without closing either intake', async () => {
  const original = await create();
  const payload = { key: 'tablet-blocker', expected_version: original.version, title: 'Build ARM binary',
    text: 'Required binary is missing', acceptance_criteria: 'ARM binary launches on tablet.' };
  const blocked = await api(`/intake/tasks/${original.id}/blocker`, payload);
  expect(blocked.status, JSON.stringify(blocked.data)).toBe(200);
  const followup = blocked.data.followup;
  expect(blocked.data.task.status).toBe('blocked');
  expect(blocked.data.task.blocked_by).toEqual([followup.id]);
  expect((await api(`/intake/tasks/${original.id}/blocker`, payload)).data.followup.id).toBe(followup.id);
  const start = { key: 'tablet-start', workspace: 'demo', intake_task_id: original.id, brief: 'Continue installation' };
  const refused = await api('/sessions', start);
  expect(refused.status, JSON.stringify(refused.data)).toBe(409);
  expect(refused.data.error).toBe('intake_dependencies_unresolved');
  expect(refused.data.blocked_by).toEqual([followup.id]);
  const assigned = await api(`/intake/tasks/${original.id}/assign`, { key: 'tablet-assign', expected_version: blocked.data.task.version,
    session: { workspace: 'demo', brief: 'Continue installation' }, provenance: { type: 'unknown' } });
  expect(assigned.status).toBe(409);
  expect(assigned.data.error).toBe('intake_dependencies_unresolved');
  const note = await api(`/intake/tasks/${followup.id}/events`, { key: 'tablet-done', expected_version: followup.version,
    note: 'DONE abc1234\nLive check: ARM fixture\nResult: launches' });
  expect(note.status, JSON.stringify(note.data)).toBe(200);
  // DONE on the prerequisite releases its parents immediately (reverse-index probe); the watchdog sweep is idempotent.
  const resumed = (await api(`/intake/tasks/${original.id}`)).data.task;
  expect(resumed.blocked_by).toEqual([]);
  expect(resumed.status).toBe('open');
  expect(await inDO(stub(), (h: any) => h.intakeService.reconcileDependencies())).toEqual([]);
  expect(await inDO(stub(), (h: any) => h.sql.exec('SELECT state,reason FROM intake_restart_queue WHERE task_id=?', original.id).toArray()[0]))
    .toMatchObject({ state: 'queued', reason: 'prereqs_done' });
  expect((await api(`/intake/tasks/${followup.id}`)).data.task.status).toBe('open');
});

it('rolls back bad blocker creation and refuses cycles and cross-project links', async () => {
  const a = await create(), b = await create(), foreign = await create('other');
  const bad = await api(`/intake/tasks/${a.id}/blocker`, { key: 'empty-ac', expected_version: a.version,
    title: 'Invalid followup', text: 'No AC', acceptance_criteria: ' ' });
  expect(bad.status).toBe(400);
  expect((await api(`/intake/tasks/${a.id}`)).data.task.version).toBe(a.version);
  const add = await api(`/intake/tasks/${a.id}/depend`, { key: 'a-on-b', expected_version: a.version, on: b.id });
  expect(add.status, JSON.stringify(add.data)).toBe(200);
  const cycle = await api(`/intake/tasks/${b.id}/depend`, { key: 'b-on-a', expected_version: b.version, on: a.id });
  expect(cycle.data.error).toBe('dependency_cycle');
  expect(cycle.status).toBe(409);
  const cross = await api(`/intake/tasks/${b.id}/depend`, { key: 'b-on-foreign', expected_version: b.version, on: foreign.id });
  expect(cross.status).toBe(404);
  const force = await api('/sessions', { key: 'force-tablet', workspace: 'demo', intake_task_id: a.id,
    brief: 'Inspect prerequisite', force: true });
  expect(force.data.error).not.toBe('intake_dependencies_unresolved');
  const current = (await api(`/intake/tasks/${a.id}`)).data.task;
  const moved = await api(`/intake/tasks/${a.id}`, { key: 'move-linked', expected_version: current.version, project: 'other' });
  expect(moved.status).toBe(409);
  expect(moved.data.error).toBe('dependency_project_conflict');
});

it('requests decomposition through canonical assignment with deep routing and durable child instructions', async () => {
  const original = await create();
  const captured = await inDO(stub(), async (h: any) => {
    const prior = h.intakeService.hooks.startSession;
    let input: any;
    const sessionId = crypto.randomUUID(), now = new Date().toISOString();
    h.intakeService.hooks.startSession = async (request: any) => {
      input = request;
      h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
        VALUES(?,'fixture',?,'{}','headless','running',?,?)`, sessionId, request.key, now, now);
      return { status: 201, body: { id: sessionId, state: 'running' } };
    };
    try {
      const response = await h.intakeService.handle(new Request(`https://hub.test/api/intake/tasks/${original.id}/decompose`, {
        method: 'POST', body: JSON.stringify({ key: 'deep-decompose', expected_version: original.version }) }),
        async () => ({ caller: { kind: 'human', isAdmin: true, actor: 'fixture' }, scopes: ['admin'] }));
      return { input, status: response.status, data: await response.json(),
        marker: h.sql.exec('SELECT task_id,project,child_count FROM intake_decomposition_sessions WHERE session_id=?', sessionId).toArray()[0] };
    } finally { h.intakeService.hooks.startSession = prior; }
  });
  expect(captured.input.tier).toBe('deep');
  expect(captured.input.intake_task_id).toBe(original.id);
  expect(captured.input.brief).toContain('child intakes');
  expect(captured.input.brief).toContain('hub intake depend');
  expect(captured.status).toBe(200);
  expect(captured.marker).toEqual({ task_id: original.id, project: 'demo', child_count: 0 });
});

it('supports shared prerequisites, deduplicates edges and rejects indirect cycles', async () => {
  await inDO(stub(), (h: any) => {
    const graph = new IntakeDependencies(h.sql);
    graph.add('dep-a', 'dep-b', 'operator');
    graph.add('dep-a', 'dep-b', 'operator');
    graph.add('dep-c', 'dep-b', 'operator');
    graph.add('dep-a', 'dep-c', 'operator');
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b', 'dep-c']);
    expect(() => graph.add('dep-b', 'dep-a', 'operator')).toThrow('dependency_cycle');
    expect(() => graph.add('dep-a', 'dep-a', 'operator')).toThrow('dependency_cycle');
    graph.remove('dep-a', 'dep-c');
    expect(graph.view('dep-a').depends_on).toEqual(['dep-b']);
    expect(graph.view('dep-c').depends_on).toEqual(['dep-b']);
  });
});

it('requires current DONE evidence and fails closed for missing or cancelled prerequisites', async () => {
  await inDO(stub(), (h: any) => {
    const graph = new IntakeDependencies(h.sql);
    graph.add('dep-a', 'dep-b', 'operator');
    const set = (status: string, history: any[]) => h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',
      JSON.stringify({ id: 'dep-b', project: 'demo', title: 'dep-b', status, history }), 'dep-b');
    set('cancelled', []);
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b']);
    set('open', [{ kind: 'note', text: 'DONE' }]);
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b']);
    const done = { kind: 'note', text: 'DONE abc1234\nLive check: fixture\nResult: passed' };
    set('cancelled', [done]);
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b']);
    set('open', [done]);
    expect(graph.view('dep-a').blocked_by).toEqual([]);
    set('open', [done, { kind: 'update', changes: { acceptance_criteria: 'New requirement' } }]);
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b']);
    h.sql.exec('DELETE FROM intake_tasks WHERE id=?', 'dep-b');
    expect(graph.view('dep-a').dependencies[0].status).toBe('missing');
    expect(graph.view('dep-a').blocked_by).toEqual(['dep-b']);
  });
});


it('preserves an independent blocked status after its prerequisite completes', async () => {
  const original = await create(), prerequisite = await create();
  const held = await api(`/intake/tasks/${original.id}`, { key: 'independent-hold', expected_version: original.version, status: 'blocked' });
  expect(held.status).toBe(200);
  const linked = await api(`/intake/tasks/${original.id}/depend`, { key: 'held-dependency', expected_version: held.data.task.version, on: prerequisite.id });
  expect(linked.status).toBe(200);
  await api(`/intake/tasks/${prerequisite.id}/events`, { key: 'held-done', expected_version: prerequisite.version,
    note: 'DONE abc1234\nLive check: fixture\nResult: passed' });
  await inDO(stub(), (h: any) => h.intakeService.reconcileDependencies());
  expect((await api(`/intake/tasks/${original.id}`)).data.task.status).toBe('blocked');
});


it('invalidates original DONE on a new blocker but preserves fresh DONE during resolution', async () => {
  const original = await create();
  const done = await api(`/intake/tasks/${original.id}/events`, { key: 'original-old-done', expected_version: original.version,
    note: 'DONE abc1234\nLive check: original fixture\nResult: passed' });
  await inDO(stub(), (h: any) => h.intakeService.dependencies.add('dep-a', original.id, 'fixture'));
  const blocked = await api(`/intake/tasks/${original.id}/blocker`, { key: 'new-requirement', expected_version: done.data.task.version,
    title: 'New prerequisite', text: 'New unmet requirement', acceptance_criteria: 'Requirement met' });
  expect(blocked.status).toBe(200);
  expect(await inDO(stub(), (h: any) => h.intakeService.dependencies.view('dep-a').dependencies[0].resolved)).toBe(false);
  const followup = blocked.data.followup;
  await api(`/intake/tasks/${followup.id}/events`, { key: 'new-requirement-done', expected_version: followup.version,
    note: 'DONE abc1234\nLive check: prerequisite fixture\nResult: passed' });
  // The prerequisite's DONE releases the parent immediately (recording the release), so re-read its version.
  const current = (await api(`/intake/tasks/${original.id}`)).data.task;
  const fresh = await api(`/intake/tasks/${original.id}/events`, { key: 'fresh-original-done', expected_version: current.version,
    note: 'DONE def5678\nLive check: updated fixture\nResult: passed' });
  expect(fresh.status).toBe(200);
  const resolved = await inDO(stub(), (h: any) => {
    h.intakeService.reconcileDependencies();
    return h.intakeService.dependencies.view('dep-a').dependencies[0].resolved;
  });
  expect(resolved).toBe(true);
});


it.each(['blocked', 'pending_approval'])('preserves explicit %s applied after dependency linking', async status => {
  const original = await create(), prerequisite = await create();
  const linked = await api(`/intake/tasks/${original.id}/depend`, { key: 'link-before-hold-' + status, expected_version: original.version, on: prerequisite.id });
  expect(linked.status, JSON.stringify(linked.data)).toBe(200);
  const held = await api(`/intake/tasks/${original.id}`, { key: 'later-hold-' + status, expected_version: linked.data.task.version, status });
  expect(held.status).toBe(200);
  await api(`/intake/tasks/${prerequisite.id}/events`, { key: 'later-hold-done-' + status, expected_version: prerequisite.version,
    note: 'DONE abc1234\nLive check: prerequisite fixture\nResult: passed' });
  await inDO(stub(), (h: any) => h.intakeService.reconcileDependencies());
  expect((await api(`/intake/tasks/${original.id}`)).data.task.status).toBe(status);
});
it('shares one prerequisite per blocker_key across parents and ranks it ahead of the parent in the shared queue', async () => {
  const a = await create(), b = await create();
  const body = (t: any, key: string) => ({ key, expected_version: t.version, title: 'Install zcodex', text: 'engine missing',
    acceptance_criteria: 'zcodex runs.', blocker_key: 'missing_engine:zcodex' });
  const first = await api(`/intake/tasks/${a.id}/blocker`, body(a, 'blk-a'));
  const second = await api(`/intake/tasks/${b.id}/blocker`, body(b, 'blk-b'));
  expect(first.status, JSON.stringify(first.data)).toBe(200);
  expect(second.data.followup.id).toBe(first.data.followup.id);
  expect(second.data.deduplicated).toBe(true);
  expect((await api(`/intake/tasks/${a.id}`)).data.task.blocked_by).toEqual([first.data.followup.id]);
  expect((await api(`/intake/tasks/${b.id}`)).data.task.blocked_by).toEqual([first.data.followup.id]);
  const row = await inDO(stub(), (h: any) => h.sql.exec('SELECT state,reason,updated_at FROM intake_restart_queue WHERE task_id=?', first.data.followup.id).toArray()[0]);
  expect(row).toMatchObject({ state: 'queued' });
  expect(row.reason).toMatch(/^prerequisite_of:/);
  // The web list of waiting parents names their blockers.
  const waiting = await inDO(stub(), (h: any) => h.intakeService.dependencies.waiting());
  expect(waiting.map((w: any) => w.id).sort()).toEqual([a.id, b.id].sort());
  expect(waiting[0].blocked_by).toEqual([first.data.followup.id]);
});

it('reads dependency graphs in batches for list views (no per-task query)', async () => {
  const parents = [] as any[];
  for (let i = 0; i < 12; i++) parents.push(await create());
  for (const p of parents) await api(`/intake/tasks/${p.id}/depend`, { key: 'd-' + p.id, expected_version: p.version, on: 'dep-a' }).then(r => { if (r.status !== 200) throw new Error(JSON.stringify(r.data)); });
  const seen = await inDO(stub(), async (h: any) => {
    // Count dependency-table reads through a proxy on the module's own handle (never patch the shared sql object).
    const deps = h.intakeService.dependencies; const real = deps.sql; let n = 0;
    deps.sql = new Proxy(real, { get(t: any, prop: string) {
      if (prop === 'exec') return (q: string, ...a: any[]) => { if (/FROM intake_dependencies/.test(q)) n++; return t.exec(q, ...a); };
      const v = t[prop]; return typeof v === 'function' ? v.bind(t) : v;
    } });
    try {
      await h.intakeService.handle(new Request('https://hub.internal/api/intake/tasks?limit=100'), async () => ({ caller: { isAdmin: true, actor: 'human:test', kind: 'human' }, scopes: ['admin'] }));
    } finally { deps.sql = real; }
    return n;
  });
  expect(seen).toBeLessThanOrEqual(2);
});

it('fails closed: an unreadable dependency graph refuses a start instead of allowing it', async () => {
  const original = await create();
  await inDO(stub(), (h: any) => fixtureSql(h).exec('ALTER TABLE intake_dependencies RENAME TO intake_dependencies_hold'));
  try {
    const refused = await api('/sessions', { key: 'fail-closed', workspace: 'demo', intake_task_id: original.id, brief: 'Go' });
    expect(refused.status, JSON.stringify(refused.data)).toBe(503);
    expect(refused.data.error).toBe('dependency_check_failed');
  } finally {
    await inDO(stub(), (h: any) => fixtureSql(h).exec('ALTER TABLE intake_dependencies_hold RENAME TO intake_dependencies'));
  }
});

it('files a shared blocker prerequisite instead of escalating when a held session hits the retry cap', async () => {
  const original = await create();
  const { IntakeWatchdog, queuePolicyFrom } = await import('../src/intake-watchdog');
  const hold = { error: 'no_eligible_route', rejected: [{ machine: 'M1', engine: 'claude', reason: 'goal_unsupported' }] };
  const now = Date.now() + 3 * 3600_000; const stops: string[] = [];
  const out = await inDO(stub(), async (h: any) => {
    h.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,capabilities) VALUES('dep-host','dep-host','dep-host','2026-10-31T10:00:00Z',?)", JSON.stringify({ workspaces: ['demo'] }));
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,hold_json)
      VALUES('held-dep','hub:service:intake-watchdog','held-dep',?,'claude','interactive','held',?,?,?)`,
      JSON.stringify({ intake_task_id: original.id, workspace: 'demo', with_goal: true }), new Date().toISOString(), new Date().toISOString(), JSON.stringify(hold));
    const w = new IntakeWatchdog(h.sql, h.intakeService, (f: any) => f(), { policy: () => queuePolicyFrom({ HUB_INTAKE_QUEUE_ENABLED: 'true', HUB_INTAKE_QUEUE_PROJECTS: '*' }),
      command: async (id: string, action: string) => { stops.push(id + ':' + action); return { status: 202, body: {} }; } }, () => now);
    w.init();
    h.sql.exec("INSERT OR REPLACE INTO intake_restart_queue(task_id,state,reason,attempts,next_at,updated_at) VALUES(?,'queued','rechecking_admission',3,?,?)", original.id, now - 1, now - 1);
    await w.tick();
    return h.intakeService.dependencies.view(original.id);
  });
  expect(out.blocked_by).toHaveLength(1);
  expect(stops).toEqual(['held-dep:stop']);
  const shown = (await api(`/intake/tasks/${original.id}`)).data.task;
  expect(shown.status).toBe('blocked');
  expect(shown.dependencies[0].title).toContain('no_eligible_route');
});
