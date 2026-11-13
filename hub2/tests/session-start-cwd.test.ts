import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/worker';

const token = 'synthetic-session-start-token-00000000000';

function fixture(options: { refusal?: { status: number; error: string }; createStatus?: number; discard?: { status: number; body: unknown };
  repoAlias?: string; canonicalUrl?: string | null } = {}) {
  const projectCalls: Array<{ path: string; project: string | null; body: any }> = [];
  const control = {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: async (input: string | Request, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(input, init);
        const path = new URL(request.url).pathname;
        if (path === '/_actors/register') return Response.json({ registered: true });
        if (path === '/api/repos/' + encodeURIComponent('org/app') || path === '/api/repos/org%2Fapp'
            || (options.repoAlias && path === '/api/repos/' + encodeURIComponent(options.repoAlias)))
          return Response.json({ repo: { canonical_name: 'org/app',
            ...(options.canonicalUrl === null ? {} : { canonical_url: options.canonicalUrl ?? 'https://github.com/org/app.git' }), status: 'active' } });
        if (path.startsWith('/api/routes/intake/')) return Response.json({ route: { id: 'intake-given', owner: 'org/app' } });
        return Response.json({ error: 'not_found' }, { status: 404 });
      },
    }),
  };
  const projects = {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: async (request: Request) => {
        const url = new URL(request.url);
        const body = await request.json();
        projectCalls.push({ path: url.pathname, project: url.searchParams.get('project'), body });
        if (url.pathname === '/api/intakes') return Response.json({ intake: { id: 'intake-0001' } }, { status: options.createStatus ?? 201 });
        if (url.pathname.endsWith('/discard')) {
          const discard = options.discard ?? { status: 200, body: { discarded: true, intake: { id: 'intake-0001', status: 'archived' } } };
          return Response.json(discard.body, { status: discard.status });
        }
        if (options.refusal) return Response.json({ error: options.refusal.error }, { status: options.refusal.status });
        return Response.json({ session: { id: 'session-0001', state: 'offered' } }, { status: 201 });
      },
    }),
  };
  const env = { CONTROL: control, PROJECTS: projects, HUB2_DEV_OPERATOR_TOKEN: token } as any;
  const post = (path: string, body: Record<string, unknown>) => worker.fetch(new Request('https://hub.test' + path, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', Origin: 'https://hub.test' },
    body: JSON.stringify(body),
  }), env);
  const start = (body: Record<string, unknown>) => post('/api/sessions', { key: 'start-key-00000001', mode: 'headless', brief: 'Do the task', ...body });
  return { projectCalls, start, post };
}

test('--cwd is the launch workspace; the repository only names the project', async () => {
  const f = fixture();
  const response = await f.start({ repo: 'org/app', cwd: '/home/user/work/2026-11-10-task' });
  assert.equal(response.status, 201, JSON.stringify(await response.clone().json()));
  const started = f.projectCalls.find(call => call.path.endsWith('/start'))!;
  assert.equal(started.project, 'org/app');
  assert.equal(started.body.workspace, '/home/user/work/2026-11-10-task');
});

test('repo-only launch workspace comes from the canonical registry origin', async () => {
  const f = fixture();
  assert.equal((await f.start({ repo: 'org/app' })).status, 201);
  const started = f.projectCalls.find(call => call.path.endsWith('/start'))!;
  assert.equal(started.body.workspace, 'repo-189a31e2ed69f2eda847d62bb76543970dc623261734e6072fc5236932e');
  assert.deepEqual(started.body.required_capabilities, ['repo-identity:v1']);
});

test('registry aliases use the canonical repository identity after resolution', async () => {
  const f = fixture({ repoAlias: 'legacy-app', canonicalUrl: 'https://github.com/CanonicalOwner/CanonicalApp.git' });
  assert.equal((await f.start({ repo: 'legacy-app', needs: ['browser:orin-chrome'] })).status, 201);
  const started = f.projectCalls.find(call => call.path.endsWith('/start'))!;
  assert.equal(started.project, 'org/app');
  assert.equal(started.body.workspace, 'repo-dbcdada8bd9f6a134f3440a9fb120d904ee226139a2e0589420486bf638');
  assert.deepEqual(started.body.required_capabilities, ['browser:orin-chrome', 'repo-identity:v1']);
});

test('repo-only start fails closed without a canonical registry URL while explicit workspace remains usable', async () => {
  const missing = fixture({ canonicalUrl: null });
  const refused = await missing.start({ repo: 'org/app' });
  assert.equal(refused.status, 409);
  assert.equal(((await refused.json()) as any).error, 'repository_identity_unavailable');
  assert.equal(missing.projectCalls.length, 0);

  const explicit = fixture({ canonicalUrl: null });
  assert.equal((await explicit.start({ repo: 'org/app', workspace: 'app' })).status, 201);
  assert.equal(explicit.projectCalls.find(call => call.path.endsWith('/start'))!.body.workspace, 'app');
});

test('--cwd wins over a workspace label and a relative --cwd is refused before any intake is created', async () => {
  const f = fixture();
  assert.equal((await f.start({ repo: 'org/app', workspace: 'app' })).status, 201);
  assert.equal(f.projectCalls.find(call => call.path.endsWith('/start'))!.body.workspace, 'app');
  f.projectCalls.length = 0;
  assert.equal((await f.start({ repo: 'org/app', workspace: 'app', cwd: 'C:\\work\\task' })).status, 201);
  assert.equal(f.projectCalls.find(call => call.path.endsWith('/start'))!.body.workspace, 'C:\\work\\task');
  const g = fixture();
  const refused = await g.start({ repo: 'org/app', cwd: 'work/task' });
  assert.equal(refused.status, 400);
  assert.equal(((await refused.json()) as any).error, 'invalid_cwd');
  assert.equal(g.projectCalls.length, 0);
});

test('a definitively refused start discards the intake this request created', async () => {
  const f = fixture({ refusal: { status: 400, error: 'astra_not_for_implementation' } });
  const response = await f.start({ repo: 'org/app', cwd: '/home/user/work/task' });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: 'astra_not_for_implementation', stage: 'start', intake_id: 'intake-0001', intake_archived: true });
  const cleanup = f.projectCalls.at(-1)!;
  assert.equal(cleanup.path, '/api/intakes/intake-0001/discard');
  assert.deepEqual(cleanup.body, { key: 'start-key-00000001:refused', reason: 'start refused: astra_not_for_implementation' });
});

test('no discard for a replayed or caller-supplied intake, nor for a retryable refusal', async () => {
  const replayed = fixture({ createStatus: 200, refusal: { status: 400, error: 'invalid_model' } });
  const replayBody = (await (await replayed.start({ repo: 'org/app' })).json()) as any;
  assert.equal(replayBody.intake_archived, undefined);
  assert.ok(replayed.projectCalls.at(-1)!.path.endsWith('/start'), 'an idempotent replay returned an existing intake');

  const given = fixture({ refusal: { status: 400, error: 'invalid_model' } });
  assert.equal((await given.start({ repo: 'org/app', intake_task_id: 'intake-given' })).status, 400);
  assert.deepEqual(given.projectCalls.map(call => call.path), ['/api/intakes/intake-given/start']);

  for (const status of [408, 409, 429, 500, 503]) {
    const retryable = fixture({ refusal: { status, error: 'try_again' } });
    const response = await retryable.start({ repo: 'org/app' });
    assert.equal(response.status, status);
    assert.equal(((await response.json()) as any).intake_archived, undefined);
    assert.ok(retryable.projectCalls.at(-1)!.path.endsWith('/start'), `HTTP ${status} keeps the intake for a keyed retry`);
  }
});

test('intake_archived is reported only when the project confirms the archived state', async () => {
  for (const discard of [
    { status: 409, body: { error: 'intake_in_use' } },
    { status: 500, body: { error: 'internal_error' } },
    { status: 200, body: { discarded: false, duplicate: true, intake: { id: 'intake-0001', status: 'open' } } },
    { status: 200, body: { discarded: true, intake: { id: 'intake-0001', status: 'accepted' } } },
  ]) {
    const f = fixture({ refusal: { status: 400, error: 'invalid_model' }, discard });
    const response = await f.start({ repo: 'org/app' });
    assert.equal(response.status, 400, 'the refusal itself is still reported');
    const body = (await response.json()) as any;
    assert.equal(body.error, 'invalid_model');
    assert.equal(body.intake_archived, undefined, JSON.stringify(discard));
  }
});

test('the discard route is not reachable through the public API', async () => {
  const f = fixture();
  for (const path of ['/api/intakes/intake-0001/discard?project=org/app', '/api/intake/tasks/intake-0001/discard?project=org/app']) {
    const response = await f.post(path, { key: 'outside-key-0001', reason: 'start refused: x' });
    assert.ok(response.status >= 400, path);
  }
  assert.equal(f.projectCalls.length, 0);
});
