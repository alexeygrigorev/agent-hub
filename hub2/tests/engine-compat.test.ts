import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { ControlDO } from '../src/control-do';
import worker from '../src/worker';
import { AccountDO } from '../src/account-do';

const admin = { id: 'human:admin', kind: 'operator', scopes: ['operator:admin'] };
const machine = { id: 'machine:RMTHZ', kind: 'machine', scopes: ['host'], resource_id: 'RMTHZ' };
const accessFile = JSON.stringify({ claudeAiOauth: { accessToken: 'synthetic-access-token' } });

function fixture(managedAccount?: AccountDO) {
  const database = new DatabaseSync(':memory:');
  let generation = 7;
  const acknowledgements: Array<{ machine: string; generation: number }> = [];
  let storedFiles: unknown;
  const hosts = ['RMTHZ', 'LAPTOP-PHA6TCNC', 'DESKTOP-FP4OP26'];
  const account = {
    idFromName: (id: string) => id,
    get: () => ({ fetch: async (input: Request | string, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (managedAccount) return managedAccount.fetch(request);
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

async function maintenanceFixture(options: { expired?: boolean; refuseRefresh?: boolean; refuseQuota?: boolean; missing?: boolean } = {}) {
  const database = new DatabaseSync(':memory:');
  const state: any = { storage: { async setAlarm() {}, async deleteAlarm() {}, sql: {
    exec(query: string, ...args: unknown[]) {
      const statement = database.prepare(query);
      const rows = statement.columns().length ? statement.all(...args) : (statement.run(...args), []);
      return { toArray: () => rows };
    },
  }, transactionSync(fn: () => unknown) { database.exec('BEGIN'); try { const value = fn(); database.exec('COMMIT'); return value; }
    catch (caught) { database.exec('ROLLBACK'); throw caught; } } }, blockConcurrencyWhile: (fn: () => unknown) => Promise.resolve(fn()) };
  const calls: string[] = [];
  const account = new AccountDO(state, { HUB2_CREDENTIAL_KEY: 'a'.repeat(64), HUB2_PROVIDER_FETCH: (async (url: string) => {
    calls.push(url);
    if (url.endsWith('/token')) return options.refuseRefresh
      ? Response.json({ error: 'invalid_grant', secret: 'synthetic-provider-secret' }, { status: 400 })
      : Response.json({ access_token: 'synthetic-new-access', refresh_token: 'synthetic-new-refresh', expires_in: 86400 });
    assert.ok(url.endsWith('/usage'));
    return options.refuseQuota ? Response.json({ secret: 'synthetic-provider-secret' }, { status: 429 })
      : Response.json({ five_hour: { utilization: 20, resets_at: new Date(Date.now() + 3600000).toISOString() },
        seven_day: { utilization: 30, resets_at: new Date(Date.now() + 86400000).toISOString() } });
  }) as any });
  const callAccount = (path: string, method = 'POST', body?: unknown, actor: unknown = admin) => account.fetch(new Request('https://account.test' + path, {
    method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }));
  assert.equal((await callAccount('/api/account', 'PATCH', { provider: 'claude', enabled: true })).status, 200);
  if (!options.missing) assert.equal((await callAccount('/api/account/credentials', 'PUT', { expected_generation: 0,
    files: { '.claude/.credentials.json': JSON.stringify({ claudeAiOauth: { accessToken: 'synthetic-old-access',
      refreshToken: 'synthetic-old-refresh', expiresAt: Date.now() + (options.expired ? -1000 : 86400000) } }) } })).status, 200);
  return { ...fixture(account), calls, callAccount };
}

test('operator maintain refreshes centrally, advances generation, and forces fresh quota without exporting secrets', async () => {
  const f = await maintenanceFixture({ expired: true });
  const response = await f.call('/api/engines/maintain', 'POST', {});
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { results: [{ label: 'hub-claude', generation: 2, status: 'ready', quota: 'observed' }] });
  assert.equal(f.calls.filter(url => url.endsWith('/token')).length, 1);
  assert.equal(f.calls.filter(url => url.endsWith('/usage')).length, 1);
  await f.call('/api/engines/maintain', 'POST', {});
  assert.equal(f.calls.filter(url => url.endsWith('/token')).length, 1);
  assert.equal(f.calls.filter(url => url.endsWith('/usage')).length, 2, 'manual maintenance probes even with cached quota');
});

test('maintain reports expired credentials independently of the restored HTTP route', async () => {
  const f = await maintenanceFixture({ expired: true, refuseRefresh: true });
  const response = await f.call('/api/engines/maintain', 'POST', {});
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { results: [{ label: 'hub-claude', generation: 1, status: 'expired', quota: 'skipped' }] });
  assert.equal(f.calls.length, 1);
});

test('maintain reports missing credentials and redacts provider quota refusal bodies', async () => {
  const missing = await maintenanceFixture({ missing: true });
  const response = await missing.call('/api/engines/maintain', 'POST', {});
  assert.deepEqual(await response.json(), { results: [{ label: 'hub-claude', generation: null, status: 'missing', quota: 'skipped' }] });
  assert.equal(missing.calls.length, 0);
  const refused = await maintenanceFixture({ refuseQuota: true });
  const failure = await refused.call('/api/engines/maintain', 'POST', {});
  assert.equal(failure.status, 200);
  assert.deepEqual(await failure.json(), { results: [{ label: 'hub-claude', generation: 1, status: 'ready', quota: 'failed', reason: 'quota_http_refused' }] });
});

test('maintain requires operator authority at both routing and account boundaries', async () => {
  const f = await maintenanceFixture();
  for (const actor of [machine, { id: 'agent:test', kind: 'agent', scopes: ['quota:write'] }, undefined]) {
    const outsider = actor ?? null;
    assert.equal((await f.call('/api/engines/maintain', 'POST', {}, outsider)).status, 403);
    assert.equal((await f.callAccount('/api/account/maintain', 'POST', {}, outsider)).status, 403);
  }
  assert.equal(f.calls.length, 0);
  assert.equal((await f.call('/api/engines/maintain')).status, 404, 'GET must not trigger maintenance');
});

test('public authenticated engine maintain reaches the real ControlDO and AccountDO', async () => {
  const f = await maintenanceFixture({ expired: true });
  const token = 'dev-operator-token-that-is-long-enough-for-hub2';
  const env: any = { HUB2_DEV_OPERATOR_TOKEN: token, CONTROL: { idFromName: (id: string) => id,
    get: () => ({ fetch: (request: Request) => f.call(new URL(request.url).pathname, request.method, {},
      JSON.parse(request.headers.get('x-hub2-actor')!)) }) } };
  const response = await worker.fetch(new Request('https://hub.test/api/engines/maintain', { method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: '{}' }), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { results: [{ label: 'hub-claude', generation: 2, status: 'ready', quota: 'observed' }] });
});
