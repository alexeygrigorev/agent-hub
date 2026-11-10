import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { ControlDO } from '../src/control-do';
import worker from '../src/worker';

const admin = { id: 'human:admin', kind: 'operator', scopes: ['operator:admin'] };
const machine = { id: 'machine:RMTHZ', kind: 'machine', scopes: ['host'], resource_id: 'RMTHZ' };
const accessFile = JSON.stringify({ claudeAiOauth: { accessToken: 'synthetic-access-token' } });

function fixture() {
  const database = new DatabaseSync(':memory:');
  let generation = 7;
  const acknowledgements: Array<{ machine: string; generation: number }> = [];
  let storedFiles: unknown;
  const hosts = ['RMTHZ', 'LAPTOP-PHA6TCNC', 'DESKTOP-FP4OP26'];
  const account = {
    idFromName: (id: string) => id,
    get: () => ({ fetch: async (input: Request | string, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === '/_internal/routing') return Response.json({ account: { provider: 'claude', enabled: true, min_remaining: 0 },
        credential_generation: generation, credential_status: 'ready', credential_usable: true, fresh: true,
        hosts, quota: { remaining: 80 } });
      if (path === '/api/account/hosts') return Response.json({ items: hosts.map(id => ({ id, ack_generation: generation })) });
      if (path === '/api/account/credentials' && request.method === 'GET')
        return Response.json({ generation, provider: 'claude', files: { '.claude/.credentials.json': accessFile } });
      if (path === '/api/account/credentials' && request.method === 'PUT') {
        const body = await request.json() as any;
        assert.equal(body.expected_generation, generation);
        storedFiles = body.files;
        generation++;
        return Response.json({ generation });
      }
      if (path === '/api/account/credentials' && request.method === 'POST') {
        const body = await request.json() as any;
        acknowledgements.push({ machine: JSON.parse(request.headers.get('x-hub2-actor') || '{}').resource_id, generation: body.generation });
        return Response.json({ acknowledged: true });
      }
      if (path === '/api/account/rotation' && request.method === 'POST') {
        const body = await request.json() as any;
        assert.equal(body.expected_generation, generation);
        storedFiles = body.files;
        generation++;
        return Response.json({ generation });
      }
      return Response.json({ error: 'not_found' }, { status: 404 });
    } }),
  };
  const state: any = { storage: { async setAlarm() {}, async deleteAlarm() {}, sql: {
    exec(query: string, ...args: unknown[]) {
      const statement = database.prepare(query);
      const rows = statement.columns().length ? statement.all(...args) : (statement.run(...args), []);
      return { toArray: () => rows };
    },
  }, transactionSync(fn: () => unknown) { database.exec('BEGIN'); try { const value = fn(); database.exec('COMMIT'); return value; }
    catch (caught) { database.exec('ROLLBACK'); throw caught; } } }, blockConcurrencyWhile: (fn: () => unknown) => Promise.resolve(fn()) };
  const control = new ControlDO(state, { ACCOUNTS: account, HUB2_CREDENTIAL_KEY: 'a'.repeat(64) } as any);
  const call = (path: string, method = 'GET', body?: unknown, actor: unknown = admin) => control.fetch(new Request('https://control.test' + path, {
    method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }));
  return { call, database, acknowledgements, get storedFiles() { return storedFiles; } };
}

test('Hub2 restores sanitized engine listing and central Claude credential login storage', async () => {
  const f = fixture();
  const listingResponse = await f.call('/api/engines');
  assert.equal(listingResponse.status, 200);
  const listing = await listingResponse.json() as any;
  assert.equal(listing.engines[0].label, 'hub-claude');
  assert.equal(listing.credentials[0].generation, 7);
  assert.ok(!JSON.stringify(listing).includes('synthetic-access-token'));
  assert.ok(!('files' in listing.engines[0]));

  const startedResponse = await f.call('/api/engines/credential-logins', 'POST', { provider: 'claude', label: 'hub-claude' });
  assert.equal(startedResponse.status, 201);
  const started = await startedResponse.json() as any;
  assert.match(started.url, /^https:\/\/claude\.com\//);
  const sealed = f.database.prepare('SELECT sealed FROM hub2_engine_credential_logins WHERE id=?').get(started.id) as any;
  assert.ok(typeof sealed.sealed === 'string');
  assert.ok(!sealed.sealed.includes('verifier'));

  const completed = await f.call('/api/engines/credential-logins/' + started.id, 'POST', { files: { '.claude/.credentials.json': accessFile } });
  assert.equal(completed.status, 200);
  assert.deepEqual(await completed.json(), { status: 'configured', label: 'hub-claude', credential_generation: 8 });
  assert.deepEqual(f.storedFiles, { '.claude/.credentials.json': accessFile });
});

test('Hub2 restores managed Claude admission and credential read/writeback', async () => {
  const f = fixture();
  const admission = await f.call('/api/engines/admission', 'POST', { label: 'hub-claude', native: true }, machine);
  assert.equal(admission.status, 200);
  const verdict = await admission.json() as any;
  assert.equal(verdict.launch_allowed, true);
  assert.equal(verdict.profile_supported, true);
  assert.equal(verdict.account_label, 'hub-claude');

  const current = await f.call('/api/engines/hub-claude/credential', 'GET', undefined, machine);
  assert.equal(current.status, 200);
  assert.equal((await current.json() as any).generation, 7);

  const rotated = JSON.stringify({ claudeAiOauth: { accessToken: 'synthetic-rotated-access-token' } });
  const writeback = await f.call('/api/engines/hub-claude/credential', 'POST', {
    base_generation: 7, files: { '.claude/.credentials.json': rotated },
  }, machine);
  assert.equal(writeback.status, 200);
  assert.deepEqual(await writeback.json(), { label: 'hub-claude', generation: 8, status: 'ready',
    files: { '.claude/.credentials.json': rotated }, accepted: true });
});

test('Claude canary pins 2.1.322 to RMTHZ, promotes after green smoke, then serves all hosts', async () => {
  const f = fixture();
  const otherMachine = { id: 'machine:LAPTOP-PHA6TCNC', kind: 'machine', scopes: ['host'], resource_id: 'LAPTOP-PHA6TCNC' };
  const deniedStart = await f.call('/api/engines/update', 'POST', { engine: 'claude', canary: 'RMTHZ' }, otherMachine);
  assert.equal(deniedStart.status, 403);
  const started = await f.call('/api/engines/update', 'POST', { engine: 'claude', canary: 'RMTHZ' }, machine);
  assert.equal(started.status, 202);
  assert.equal((await started.json() as any).target_version, '2.1.322');
  const conflictingCanary = await f.call('/api/engines/update', 'POST', {
    engine: 'claude', canary: 'LAPTOP-PHA6TCNC',
  });
  assert.equal(conflictingCanary.status, 409);
  const canary = await f.call('/api/engines/sync', 'GET', undefined, machine);
  const canarySync = await canary.json() as any;
  const canaryBundle = canarySync.engines[0];
  assert.equal(canaryBundle.version, '2.1.322');
  assert.equal(canarySync.latest_version, '2.1.322');
  const other = await f.call('/api/engines/sync', 'GET', undefined,
    { id: 'machine:LAPTOP-PHA6TCNC', kind: 'machine', scopes: ['host'], resource_id: 'LAPTOP-PHA6TCNC' });
  const oldBundle = (await other.json() as any).engines[0];
  assert.equal(oldBundle.version, '2.1.312');

  const syncReport = await f.call('/api/engines/sync-report', 'POST', { state: 'synced', items: [
    { label: 'hub-claude', revision: canaryBundle.revision, state: 'installed', credential_generation: 7 },
  ] }, machine);
  assert.equal(syncReport.status, 200);
  assert.deepEqual(f.acknowledgements, [{ machine: 'RMTHZ', generation: 7 }]);

  const versionReport = await f.call('/api/engines/version-report', 'POST', {
    engines: { claude: { installed: '2.1.322', latest: '2.1.322', result: 'green' } },
  }, machine);
  assert.equal(versionReport.status, 200);
  const promoted = await f.call('/api/engines/sync', 'GET', undefined,
    { id: 'machine:LAPTOP-PHA6TCNC', kind: 'machine', scopes: ['host'], resource_id: 'LAPTOP-PHA6TCNC' });
  const promotedBundle = (await promoted.json() as any).engines[0];
  assert.equal(promotedBundle.version, '2.1.322');
  assert.equal(typeof promotedBundle.revision, 'string');
  assert.notEqual(promotedBundle.revision, oldBundle.revision);
  assert.equal(promotedBundle.revision, canaryBundle.revision);
});

test('Claude fleet rollout returns to the prior desired version after a host smoke failure', async () => {
  const f = fixture();
  const started = await f.call('/api/engines/update', 'POST', { engine: 'claude', canary: 'RMTHZ' });
  assert.equal(started.status, 202);
  const canary = await f.call('/api/engines/sync', 'GET', undefined, machine);
  const canaryBundle = (await canary.json() as any).engines[0];
  await f.call('/api/engines/version-report', 'POST', {
    engines: { claude: { installed: '2.1.322', latest: '2.1.322', result: 'green' } },
  }, machine);
  const rollout = await f.call('/api/engines/versions', 'GET', undefined, admin);
  assert.equal(((await rollout.json() as any).rollouts[0]).state, 'rolling');

  const failedHost = { id: 'machine:LAPTOP-PHA6TCNC', kind: 'machine', scopes: ['host'], resource_id: 'LAPTOP-PHA6TCNC' };
  const failed = await f.call('/api/engines/version-report', 'POST', {
    engines: { claude: { installed: '2.1.312', latest: '2.1.322', result: 'failed' } },
  }, failedHost);
  assert.equal(failed.status, 200);
  const rolledBack = await f.call('/api/engines/sync', 'GET', undefined, failedHost);
  const bundle = (await rolledBack.json() as any).engines[0];
  assert.equal(bundle.version, '2.1.312');
  assert.notEqual(bundle.revision, canaryBundle.revision);
  const state = await f.call('/api/engines/versions', 'GET', undefined, admin);
  assert.equal(((await state.json() as any).rollouts[0]).state, 'rolled_back');
});

test('Hub2 Worker forwards authenticated engine APIs to ControlDO', async () => {
  const seen: string[] = [];
  const token = 'dev-operator-token-that-is-long-enough-for-hub2';
  const env: any = { HUB2_DEV_OPERATOR_TOKEN: token, CONTROL: { idFromName: (id: string) => id,
    get: () => ({ fetch: async (input: Request | string, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init);
      seen.push(new URL(request.url).pathname);
      return Response.json({ ok: true });
    } }) } };
  const response = await worker.fetch(new Request('https://hub.test/api/engines/sync', {
    headers: { Authorization: 'Bearer ' + token },
  }), env);
  assert.equal(response.status, 200);
  assert.ok(seen.includes('/api/engines/sync'));
});
