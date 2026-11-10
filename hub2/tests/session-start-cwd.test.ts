import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/worker';

const token = 'synthetic-session-start-token-00000000000';

function fixture(startRefusal?: { status: number; error: string }) {
  const projectCalls: Array<{ path: string; project: string | null; body: any }> = [];
  const control = {
    idFromName: (name: string) => name,
    get: () => ({
      fetch: async (input: string | Request, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(input, init);
        const path = new URL(request.url).pathname;
        if (path === '/_actors/register') return Response.json({ registered: true });
        if (path === '/api/repos/' + encodeURIComponent('org/app') || path === '/api/repos/org%2Fapp')
          return Response.json({ repo: { canonical_name: 'org/app', status: 'active' } });
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
        if (url.pathname === '/api/intakes') return Response.json({ intake: { id: 'intake-0001' } }, { status: 201 });
        if (!url.pathname.endsWith('/start')) return Response.json({ intake: { id: 'intake-0001', status: 'archived' } });
        if (startRefusal) return Response.json({ error: startRefusal.error }, { status: startRefusal.status });
        return Response.json({ session: { id: 'session-0001', state: 'offered' } }, { status: 201 });
      },
    }),
  };
  const start = (body: Record<string, unknown>) => worker.fetch(new Request('https://hub.test/api/sessions', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', Origin: 'https://hub.test' },
    body: JSON.stringify({ key: 'start-key-00000001', mode: 'headless', brief: 'Do the task', ...body }),
  }), { CONTROL: control, PROJECTS: projects, HUB2_DEV_OPERATOR_TOKEN: token } as any);
  return { projectCalls, start };
}

test('--cwd is the launch workspace; the repository only names the project', async () => {
  const f = fixture();
  const response = await f.start({ repo: 'org/app', cwd: '/home/user/work/2026-11-10-task' });
  assert.equal(response.status, 201, JSON.stringify(await response.clone().json()));
  const started = f.projectCalls.find(call => call.path.endsWith('/start'))!;
  assert.equal(started.project, 'org/app');
  assert.equal(started.body.workspace, '/home/user/work/2026-11-10-task');
});

test('without --cwd the repository label stays the launch workspace', async () => {
  const f = fixture();
  assert.equal((await f.start({ repo: 'org/app' })).status, 201);
  assert.equal(f.projectCalls.find(call => call.path.endsWith('/start'))!.body.workspace, 'app');
});

test('--cwd wins over a workspace label and a relative --cwd is refused before any intake is created', async () => {
  const f = fixture();
  assert.equal((await f.start({ repo: 'org/app', workspace: 'app', cwd: 'C:\\work\\task' })).status, 201);
  assert.equal(f.projectCalls.find(call => call.path.endsWith('/start'))!.body.workspace, 'C:\\work\\task');
  const g = fixture();
  const refused = await g.start({ repo: 'org/app', cwd: 'work/task' });
  assert.equal(refused.status, 400);
  assert.equal(((await refused.json()) as any).error, 'invalid_cwd');
  assert.equal(g.projectCalls.length, 0);
});

test('a refused start archives the intake it created, keeps one it was given, and keeps it on a transient error', async () => {
  const refused = fixture({ status: 400, error: 'astra_not_for_implementation' });
  const response = await refused.start({ repo: 'org/app', cwd: '/home/user/work/task' });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: 'astra_not_for_implementation', stage: 'start', intake_id: 'intake-0001', intake_archived: true });
  const cleanup = refused.projectCalls.at(-1)!;
  assert.equal(cleanup.path, '/api/intakes/intake-0001');
  assert.deepEqual(cleanup.body, { key: 'start-key-00000001:refused', status: 'closed', reason: 'start refused: astra_not_for_implementation' });

  const given = fixture({ status: 409, error: 'intake_already_assigned' });
  const kept = await given.start({ repo: 'org/app', intake_task_id: 'intake-given' });
  assert.equal(kept.status, 409);
  assert.equal(((await kept.json()) as any).intake_archived, undefined);
  assert.deepEqual(given.projectCalls.map(call => call.path), ['/api/intakes/intake-given/start'], 'an existing intake is never archived by a refusal');

  const transient = fixture({ status: 503, error: 'no_connected_host' });
  assert.equal((await transient.start({ repo: 'org/app' })).status, 503);
  assert.ok(transient.projectCalls.at(-1)!.path.endsWith('/start'), 'a 5xx keeps the intake for a keyed retry');
});
