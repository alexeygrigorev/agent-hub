import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import worker from '../src/worker';
import { ProjectDO } from '../src/project-do';
import { ARTIFACT_MAX_BYTES } from '../src/intake-relations';

const token = 'synthetic-intake-relations-token-000000000';

function context(): any {
  const sql = new DatabaseSync(':memory:');
  return { id: { toString: () => 'synthetic' }, storage: {
    sql: { exec(q: string, ...a: any[]) { const p = sql.prepare(q); const rows = p.columns().length ? p.all(...a) : (p.run(...a), []); return { toArray: () => rows }; } },
    transactionSync(fn: any) { sql.exec('BEGIN'); try { const r = fn(); sql.exec('COMMIT'); return r; } catch (e) { sql.exec('ROLLBACK'); throw e; } },
    async getAlarm() { return null; }, async setAlarm() {}, async deleteAlarm() {},
    async get() { return undefined; }, async put() {}, async delete() { return false; },
  }, blockConcurrencyWhile: (f: any) => Promise.resolve(f()) };
}

/** Real router + one real ProjectDO per project; control resolves intake routes from the registry. */
function fixture() {
  const routes = new Map<string, { id: string; owner: string }>();
  const control = { idFromName: (n: string) => n, get: () => ({ fetch: async (input: string | Request, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === '/_actors/register') return Response.json({ registered: true });
    if (path === '/_routes/register') {
      const body = await request.json() as any;
      routes.set(body.id, { id: body.id, owner: body.owner });
      return Response.json({ registered: true });
    }
    if (path.startsWith('/api/repos/')) return Response.json({ repo: { canonical_name: decodeURIComponent(path.slice(11)), status: 'active' } });
    if (path.startsWith('/api/routes/intake/')) {
      const route = routes.get(decodeURIComponent(path.slice(19)));
      return route ? Response.json({ route: { kind: 'intake', ...route } }) : Response.json({ error: 'route_not_found' }, { status: 404 });
    }
    return Response.json({ error: 'not_found' }, { status: 404 });
  } }) };
  const objects = new Map<string, ProjectDO>();
  const env: any = { CONTROL: control, HUB2_DEV_OPERATOR_TOKEN: token,
    MACHINES: { idFromName: (n: string) => n, get: () => ({ fetch: async () => Response.json({ ok: true }) }) },
    ACCOUNTS: { idFromName: (n: string) => n, get: () => ({ fetch: async () => Response.json({ ok: true }) }) },
    MAILBOXES: { idFromName: (n: string) => n, get: () => ({ fetch: async () => Response.json({ ok: true }) }) } };
  env.PROJECTS = { idFromName: (n: string) => n, get: (name: string) => {
    if (!objects.has(name)) objects.set(name, new ProjectDO(context(), env));
    return objects.get(name)!;
  } };
  const call = async (path: string, method = 'GET', body?: unknown) => {
    const response = await worker.fetch(new Request('https://hub.test' + path, { method,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', Origin: 'https://hub.test' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) }), env);
    return { status: response.status, body: await response.json() as any };
  };
  const create = async (project: string, key: string) => {
    const created = await call('/api/intake/tasks', 'POST', { key, project, title: 'Task ' + key, text: 'Body ' + key, acceptance_criteria: ['Done'] });
    assert.equal(created.status, 201, JSON.stringify(created.body));
    return created.body.intake.id as string;
  };
  return { call, create, objects, env, routes };
}

function png(size: number): string {
  const bytes = Buffer.alloc(size, 7);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]).copy(bytes);
  return bytes.toString('base64');
}

test('depend adds a cross-project prerequisite visible in show and links; errors name the cause', async () => {
  const f = fixture();
  const field = await f.create('org/field-guide', 'a');
  const track = await f.create('org/research', 'b');
  const other = await f.create('org/research', 'c');
  const shown = await f.call('/api/intake/tasks/' + track);
  const version = shown.body.task.control.version;

  const stale = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-1', expected_version: version - 1, on: field, remove: false });
  assert.equal(stale.status, 409);
  assert.equal(stale.body.error, 'intake_version_conflict');
  assert.match(stale.body.detail, /current task\.control\.version is \d+/);

  const added = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-1', expected_version: version, on: field, remove: false });
  assert.equal(added.status, 201, JSON.stringify(added.body));
  assert.deepEqual(added.body.prerequisite, { intake_id: field, project: 'org/field-guide' });
  const replay = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-1', expected_version: version, on: field, remove: false });
  assert.equal(replay.body.duplicate, true);

  const after = await f.call('/api/intake/tasks/' + track);
  assert.deepEqual(after.body.intake.depends_on.map((d: any) => [d.intake_id, d.project]), [[field, 'org/field-guide']]);
  assert.ok(after.body.task.control.version > version, 'the change bumps the CAS version');
  const links = await f.call(`/api/intake/tasks/${track}/links`);
  assert.equal(links.status, 200);
  assert.equal(links.body.depends_on[0].intake_id, field);

  // The same project: version omitted is allowed (the CLI reads task.control.version itself).
  assert.equal((await f.call(`/api/intakes/${other}/depend`, 'POST', { key: 'dep-2', on: field })).status, 201);

  const missing = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-3', on: '00000000-0000-4000-8000-000000000000' });
  assert.equal(missing.status, 404);
  assert.equal(missing.body.error, 'prerequisite_not_found');
  assert.match(missing.body.detail, /does not exist or is not visible/);
  const self = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-4', on: track });
  assert.equal(self.body.error, 'dependency_self');
  const cycle = await f.call(`/api/intake/tasks/${field}/depend`, 'POST', { key: 'dep-5', on: track });
  assert.equal(cycle.status, 409);
  assert.equal(cycle.body.error, 'dependency_cycle');
  const noOn = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-6' });
  assert.equal(noOn.body.error, 'prerequisite_required');
  const dependentMissing = await f.call('/api/intake/tasks/00000000-0000-4000-8000-00000000abcd/depend', 'POST', { key: 'dep-7', on: field, remove: false });
  assert.equal(dependentMissing.status, 404);
  assert.notEqual(dependentMissing.body.error, 'not_found', 'never a bare not_found');

  const removed = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-8', on: field, remove: true });
  assert.equal(removed.status, 200);
  assert.deepEqual(removed.body.depends_on, []);
  const notThere = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-9', on: field, remove: true });
  assert.equal(notThere.body.error, 'dependency_not_found');
  // A link to an intake the caller can no longer resolve stays removable.
  assert.equal((await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-10', on: field })).status, 201);
  f.routes.delete(field);
  const staleLink = await f.call(`/api/intake/tasks/${track}/depend`, 'POST', { key: 'dep-11', on: field, remove: true });
  assert.equal(staleLink.status, 200, JSON.stringify(staleLink.body));
  assert.deepEqual(staleLink.body.depends_on, []);
});

test('a forged prerequisite header from the client is ignored', async () => {
  const f = fixture();
  const a = await f.create('org/a', 'x'), b = await f.create('org/b', 'y');
  const response = await worker.fetch(new Request(`https://hub.test/api/intake/tasks/${a}/depend`, { method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', Origin: 'https://hub.test',
      'x-hub2-prerequisite': JSON.stringify({ id: 'forged', project: 'org/evil' }) },
    body: JSON.stringify({ key: 'forge', on: b }) }), f.env);
  const body = await response.json() as any;
  assert.equal(response.status, 201, JSON.stringify(body));
  assert.deepEqual(body.prerequisite, { intake_id: b, project: 'org/b' });
});

test('PNG artifacts up to 6 MiB are stored on the intake; each refusal names the failed check', async () => {
  const f = fixture();
  const id = await f.create('org/relay', 'png');
  const data = png(1_855_328);
  const stored = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { key: 'k', name: 'compare-3way', media_type: 'image/png', data_base64: data, sanitized: true });
  assert.equal(stored.status, 201, JSON.stringify(stored.body));
  assert.equal(stored.body.bytes, 1_855_328);
  assert.equal(stored.body.sha256, createHash('sha256').update(Buffer.from(data, 'base64')).digest('hex'));
  const again = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { key: 'k', name: 'compare-3way', media_type: 'image/png', data_base64: data, sanitized: true });
  assert.equal(again.body.duplicate, true);

  const shown = await f.call('/api/intake/tasks/' + id);
  assert.deepEqual(shown.body.intake.artifacts.map((a: any) => [a.name, a.bytes]), [['compare-3way', 1_855_328]]);
  const download = await f.call(`/api/intake/tasks/${id}/artifacts/compare-3way`);
  assert.equal(download.status, 200);
  assert.equal(download.body.data_base64, data);
  const history = await f.call(`/api/intake/tasks/${id}/history`);
  assert.ok(!JSON.stringify(history.body).includes(data.slice(100, 200)), 'pixels never enter history pages');

  const big = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: 'big', media_type: 'image/png', data_base64: png(ARTIFACT_MAX_BYTES + 1), sanitized: true });
  assert.equal(big.status, 413);
  assert.equal(big.body.error, 'artifact_too_large');
  assert.match(big.body.detail, /size check failed/);
  const notPng = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: 'jpg', media_type: 'image/png', data_base64: Buffer.alloc(64, 1).toString('base64'), sanitized: true });
  assert.equal(notPng.body.error, 'invalid_png');
  assert.match(notPng.body.detail, /type check failed: missing PNG signature/);
  const unsanitized = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: 'x', media_type: 'image/png', data_base64: png(100), sanitized: false });
  assert.equal(unsanitized.body.error, 'sanitized_attestation_required');
  const badName = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: '../x', media_type: 'image/png', data_base64: png(100), sanitized: true });
  assert.equal(badName.body.error, 'invalid_artifact_name');
  const conflict = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: 'compare-3way', media_type: 'image/png', data_base64: png(200), sanitized: true });
  assert.equal(conflict.body.error, 'artifact_name_conflict');
  const wrapped = await f.call(`/api/intake/tasks/${id}/artifacts`, 'POST', { name: 'wrapped', media_type: 'image/png', data_base64: png(300).replace(/(.{76})/g, '$1\n'), sanitized: true });
  assert.equal(wrapped.body.error, 'invalid_png');
  assert.match(wrapped.body.detail, /not canonical base64/);
  const absent = await f.call(`/api/intake/tasks/${id}/artifacts/nope`);
  assert.equal(absent.body.error, 'artifact_not_found');
});

test('only operators, project writers or the bound session may upload; readers may download', async () => {
  const objects = new ProjectDO(context(), { CONTROL: { idFromName: (n: string) => n, get: () => ({ fetch: async () => Response.json({ registered: true }) }) } } as any);
  const admin = { id: 'human:admin', kind: 'operator', scopes: ['operator:admin'] };
  const call = async (path: string, method: string, actor: unknown, body?: unknown) => {
    const response = await objects.fetch(new Request('https://p.test' + path, { method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
    return { status: response.status, body: await response.json() as any };
  };
  const created = await call('/api/intakes?project=org/a', 'POST', admin, { key: 'own', title: 'T', text: 'X', acceptance_criteria: ['Y'] });
  const id = created.body.intake.id;
  const payload = { name: 'shot', media_type: 'image/png', data_base64: png(300), sanitized: true };
  const session = { id: 'session:s1', kind: 'agent', resource_id: id, scopes: ['intake:read', 'intake:append'] };
  const stranger = { id: 'session:s2', kind: 'agent', resource_id: 'another', scopes: ['intake:read', 'intake:append'] };
  assert.equal((await call(`/api/intakes/${id}/artifacts`, 'POST', stranger, payload)).status, 403);
  assert.equal((await call(`/api/intakes/${id}/artifacts`, 'POST', session, payload)).status, 201);
  assert.equal((await call(`/api/intakes/${id}/artifacts/shot`, 'GET', session)).status, 200);
  assert.equal((await call(`/api/intakes/${id}/artifacts/shot`, 'GET', stranger)).status, 403);
  // The project object refuses a depend that did not come through the router's prerequisite resolution.
  const unresolved = await call(`/api/intakes/${id}/depend`, 'POST', admin, { key: 'raw', on: 'whatever' });
  assert.equal(unresolved.body.error, 'prerequisite_unresolved');
});
