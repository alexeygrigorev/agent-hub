import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { ControlDO } from '../src/control-do';
import { ProjectDO } from '../src/project-do';

const PROJECT = 'org/a';
const adminActor = { id: 'human:admin', kind: 'operator', scopes: ['operator:admin'] };
const readerActor = { id: 'human:reader', kind: 'operator', scopes: ['repos:read', 'intake:read', 'project:' + PROJECT] };

function makeStorage(sql: DatabaseSync): any {
  return {
    storage: {
      sql: {
        exec(query: string, ...args: any[]) {
          const stmt = sql.prepare(query);
          const rows = stmt.columns().length ? stmt.all(...args) : (stmt.run(...args), []);
          return { toArray: () => rows };
        },
      },
      transactionSync(fn: () => unknown) {
        sql.exec('BEGIN');
        try {
          const result = fn();
          sql.exec('COMMIT');
          return result;
        } catch (err) {
          sql.exec('ROLLBACK');
          throw err;
        }
      },
      async getAlarm() { return null; },
      async setAlarm() {},
      async deleteAlarm() {},
      async get() { return undefined; },
      async put() {},
      async delete() { return false; },
    },
    id: { toString: () => 'synthetic' },
    blockConcurrencyWhile: (fn: () => unknown) => Promise.resolve(fn()),
  };
}

function makeFixture() {
  const controlSql = new DatabaseSync(':memory:');
  const projectSqls = new Map<string, DatabaseSync>();
  const projects = new Map<string, ProjectDO>();
  const tokens = new Map<string, unknown>();
  const req = (i: string | Request, init?: RequestInit) => (i instanceof Request ? i : new Request(i, init));
  const stub = { idFromName: (id: string) => id, get: () => ({ fetch: async () => Response.json({ ok: true }) }) };
  let control: ControlDO;
  const project = (id: string) => {
    let p = projects.get(id);
    if (!p) {
      let sql = projectSqls.get(id);
      if (!sql) {
        sql = new DatabaseSync(':memory:');
        projectSqls.set(id, sql);
      }
      p = new ProjectDO(makeStorage(sql), env);
      projects.set(id, p);
    }
    return p;
  };
  const env: any = {
    CONTROL: {
      idFromName: (id: string) => id,
      get: () => ({
        fetch: async (i: string | Request, init?: RequestInit) => {
          const r = req(i, init);
          if (new URL(r.url).pathname === '/_verify') {
            const { token } = (await r.clone().json()) as { token: string };
            const actor = tokens.get(token);
            return actor ? Response.json({ actor }) : Response.json({ error: 'unauthorized' }, { status: 401 });
          }
          return control.fetch(r);
        },
      }),
    },
    PROJECTS: {
      idFromName: (id: string) => id,
      get: (id: string) => ({ fetch: (i: string | Request, init?: RequestInit) => project(id).fetch(req(i, init)) }),
    },
    MACHINES: stub,
    ACCOUNTS: stub,
    MAILBOXES: stub,
  };
  control = new ControlDO(makeStorage(controlSql), env);
  const restartProject = (id: string) => {
    projects.delete(id);
    return project(id);
  };
  return {
    env,
    control,
    tokens,
    restartProject,
    close: () => {
      controlSql.close();
      for (const s of projectSqls.values()) s.close();
    },
  };
}

const jsonBody = async (r: Response) => (await r.json()) as any;

test('intake stats and promote work for short and full ids through the router and enforce auth and CSRF', async t => {
  const f = makeFixture();
  t.after(f.close);
  const { default: worker } = await import('../src/worker');

  const adminToken = 'synthetic-admin-' + 'a'.repeat(32);
  const readerToken = 'synthetic-reader-' + 'b'.repeat(32);
  f.tokens.set(adminToken, adminActor);
  f.tokens.set(readerToken, readerActor);

  const call = (path: string, method = 'GET', body?: unknown, token: string | null = adminToken, extraHeaders: Record<string, string> = {}) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json', ...extraHeaders };
    if (token !== null) headers.Authorization = 'Bearer ' + token;
    return worker.fetch(
      new Request('https://hub.example' + path, {
        method,
        headers,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }),
      f.env,
    );
  };

  // Register active repo so router forwards project requests.
  const repoResp = await call('/api/repos/' + encodeURIComponent(PROJECT), 'PATCH', {
    canonical_url: 'https://github.com/org/a',
    purpose: 'Synthetic stats/promote fixture',
    status: 'active',
    aliases: [],
    domains: [],
  });
  assert.equal(repoResp.status, 200, await repoResp.clone().text());

  // Create an open intake.
  const createdResp = await call('/api/intake/tasks', 'POST', {
    key: 'create-open',
    project: PROJECT,
    title: 'Stats and promote smoke',
    text: 'Verify stats and promote routes',
    acceptance_criteria: ['Stats returns 200 and promote sets promoted_at'],
  });
  assert.equal(createdResp.status, 201, await createdResp.clone().text());
  const created = (await jsonBody(createdResp)).intake;
  const fullId: string = created.id;
  const shortId: string = fullId.slice(0, 8);
  assert.equal(created.promoted_at, null);

  // AC1: GET stats works for both short id and full UUID on /api/intake/tasks and /api/intakes.
  for (const prefix of ['/api/intake/tasks/', '/api/intakes/']) {
    for (const ref of [shortId, fullId]) {
      const statsResp = await call(prefix + ref + '/stats');
      assert.equal(statsResp.status, 200, `${prefix}${ref}/stats`);
      const statsBody = await jsonBody(statsResp);
      assert.equal(statsBody.stats.id, fullId);
      assert.equal(statsBody.stats.project, PROJECT);
      assert.equal(statsBody.stats.status, 'open');
      assert.equal(statsBody.stats.promoted_at, null);
      assert.equal(statsBody.stats.attempt_count, 0);
      assert.deepEqual(statsBody.stats.attempts, []);
    }
  }

  // AC2: POST promote works for short id and full UUID on an open intake, sets promoted_at, and persists across restart.
  const showBefore = await jsonBody(await call('/api/intake/tasks/' + shortId));
  const v1 = showBefore.task.control.version;
  const promoteShortResp = await call('/api/intake/tasks/' + shortId + '/promote', 'POST', {
    key: 'promote-short-1',
    expected_version: v1,
  });
  assert.equal(promoteShortResp.status, 200, await promoteShortResp.clone().text());
  const promotedShort = await jsonBody(promoteShortResp);
  assert.equal(promotedShort.intake.id, fullId);
  assert.equal(typeof promotedShort.intake.promoted_at, 'number');
  assert.equal(promotedShort.task.promoted_at, promotedShort.intake.promoted_at);
  const firstPromotedAt = promotedShort.intake.promoted_at;

  // Idempotent replay succeeds with duplicate=true.
  const replayResp = await call('/api/intake/tasks/' + shortId + '/promote', 'POST', {
    key: 'promote-short-1',
    expected_version: v1,
  });
  assert.equal(replayResp.status, 200);
  assert.equal((await jsonBody(replayResp)).duplicate, true);

  // Stale CAS version returns typed 409 version_conflict (never 404).
  const staleResp = await call('/api/intake/tasks/' + fullId + '/promote', 'POST', {
    key: 'promote-stale',
    expected_version: v1,
  });
  assert.equal(staleResp.status, 409);
  assert.equal((await jsonBody(staleResp)).error, 'version_conflict');

  // Re-promoting with full UUID and current version advances promoted_at.
  const v2 = promotedShort.task.control.version;
  const promoteFullResp = await call('/api/intakes/' + fullId + '/promote', 'POST', {
    key: 'promote-full-2',
    expected_version: v2,
  });
  assert.equal(promoteFullResp.status, 200, await promoteFullResp.clone().text());
  const promotedFull = await jsonBody(promoteFullResp);
  assert.ok(promotedFull.intake.promoted_at > firstPromotedAt);

  // Persists across ProjectDO restart and is visible in show + stats.
  f.restartProject(PROJECT);
  const statsAfter = await jsonBody(await call('/api/intake/tasks/' + shortId + '/stats'));
  assert.equal(statsAfter.stats.promoted_at, promotedFull.intake.promoted_at);

  // Wrong HTTP methods return 405, not 404.
  assert.equal((await call('/api/intake/tasks/' + shortId + '/stats', 'POST', { key: 'bad' })).status, 405);
  assert.equal((await call('/api/intake/tasks/' + shortId + '/promote', 'GET')).status, 405);

  // AC2: Closed/archived intake returns typed 409 intake_not_promotable (never 404).
  const archivedResp = await call('/api/intake/tasks/' + fullId, 'POST', {
    key: 'close-intake',
    status: 'done',
    force: true,
    reason: 'done for promote test',
  });
  assert.equal(archivedResp.status, 200);
  const promoteClosedResp = await call('/api/intake/tasks/' + shortId + '/promote', 'POST', {
    key: 'promote-closed',
  });
  assert.equal(promoteClosedResp.status, 409);
  const closedErr = await jsonBody(promoteClosedResp);
  assert.equal(closedErr.error, 'intake_not_promotable');
  assert.match(closedErr.detail, /hub intake update/);
  assert.match(closedErr.detail, /hub sessions start --intake-task-id/);

  // Nonexistent intake still returns 404.
  assert.equal((await call('/api/intake/tasks/deadbeef/stats')).status, 404);
  assert.equal((await call('/api/intake/tasks/deadbeef/promote', 'POST', { key: 'missing' })).status, 404);

  // AC3: Auth is enforced (no bypass).
  assert.equal((await call('/api/intake/tasks/' + shortId + '/stats', 'GET', undefined, null)).status, 401);
  assert.equal((await call('/api/intake/tasks/' + shortId + '/promote', 'POST', { key: 'no-auth' }, null)).status, 401);

  // Read-only operator can read stats but cannot promote.
  assert.equal((await call('/api/intake/tasks/' + shortId + '/stats', 'GET', undefined, readerToken)).status, 200);
  const readerPromote = await call('/api/intake/tasks/' + shortId + '/promote', 'POST', { key: 'reader-promote' }, readerToken);
  assert.equal(readerPromote.status, 403);
  assert.equal((await jsonBody(readerPromote)).missing_scope, 'intake:write');

  // Bound worker session can read stats of its own intake, cannot promote its own intake, and cannot access another intake.
  const workerToken = 'synthetic-worker-' + 'c'.repeat(32);
  f.tokens.set(workerToken, {
    id: 'session:11111111-1111-4111-8111-111111111111',
    kind: 'agent',
    resource_id: fullId,
    generation: 'worker:1:op',
    scopes: ['intake:read', 'intake:append', 'intake:create', 'worker_project:' + PROJECT],
  });
  assert.equal((await call('/api/intake/tasks/' + fullId + '/stats', 'GET', undefined, workerToken)).status, 200);
  const workerPromote = await call('/api/intake/tasks/' + fullId + '/promote', 'POST', { key: 'worker-promote' }, workerToken);
  assert.equal(workerPromote.status, 403);
  assert.equal((await jsonBody(workerPromote)).missing_scope, 'intake:write');

  // AC3: CSRF protection is enforced (cross-origin POST rejected with 403 csrf_refused).
  const csrfResp = await call(
    '/api/intake/tasks/' + shortId + '/promote',
    'POST',
    { key: 'csrf-attempt' },
    adminToken,
    { Origin: 'https://evil.example' },
  );
  assert.equal(csrfResp.status, 403);
  assert.equal((await jsonBody(csrfResp)).error, 'csrf_refused');
});
