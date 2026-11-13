// Run: node --experimental-transform-types --test hub2/tests/account-passive-credential.test.ts
// Passive approved-host retrieval of the current Claude generation:
// AccountDO ?passive_generation=<N> and ControlDO GET /api/engines/hub-claude/passive-credential.
// Synthetic credentials and a synthetic provider only; no live services.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { registerTypeScriptResolveHook } from '../node/compat.ts';

registerTypeScriptResolveHook();
const { AccountDO } = await import('../src/account-do.ts');
const { ControlDO } = await import('../src/control-do.ts');

const KEY = 'c1'.repeat(32);
const admin = { id: 'human:test', kind: 'operator', scopes: ['operator:admin'] };
const host = { id: 'machine:h1', kind: 'machine', resource_id: 'h1', scopes: [] };
const other = { id: 'machine:h2', kind: 'machine', resource_id: 'h2', scopes: [] };
const ACCESS = 'synthetic-access-1', REFRESH = 'synthetic-refresh-1';
const PATH = '.claude/.credentials.json';
const ROUTE = '/api/engines/hub-claude/passive-credential';

function sqlState() {
  const database = new DatabaseSync(':memory:');
  const ctx: any = { storage: { sql: { exec(query: string, ...args: unknown[]) {
    const statement = database.prepare(query); let rows: unknown[] = [], rowsRead = 0, rowsWritten = 0;
    if (statement.columns().length) { rows = statement.all(...args as any); rowsRead = rows.length; }
    else { const result = statement.run(...args as any); rowsWritten = Number(result.changes); }
    return { toArray: () => rows, rowsRead, rowsWritten };
  } }, transactionSync<T>(fn: () => T): T { database.exec('BEGIN'); try { const r = fn(); database.exec('COMMIT'); return r; } catch (e) { database.exec('ROLLBACK'); throw e; } },
  async setAlarm(at: number) { ctx.alarm = at; }, async deleteAlarm() { ctx.alarm = null; } },
  blockConcurrencyWhile(fn: () => unknown) { return Promise.resolve(fn()); }, alarm: null };
  return { ctx, database };
}
const claudeFiles = (expiresAt: number) => ({ [PATH]: JSON.stringify({ claudeAiOauth: { accessToken: ACCESS, refreshToken: REFRESH, expiresAt } }) });
const totalChanges = (db: DatabaseSync) => (db.prepare('SELECT total_changes() AS n').get() as any).n as number;

function req(path: string, method = 'GET', body?: unknown, actor: unknown = admin) {
  return new Request('https://hub2.test' + path, { method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
}

// A real AccountDO whose first unseal for a chosen generation can be held open.
function holdOpen() {
  const gate: { armed: boolean; release?: () => void; entered?: () => void } = { armed: false };
  const entered = new Promise<void>(r => { gate.entered = r; });
  const released = new Promise<void>(r => { gate.release = r; });
  const base = (AccountDO as any).prototype.unseal;
  class Blocking extends (AccountDO as any) {
    async unseal(value: string, providerName: string, generation: number) {
      if (gate.armed) { gate.armed = false; gate.entered!(); await released; }
      return base.call(this, value, providerName, generation);
    }
  }
  return { gate, entered, Blocking };
}

async function accountFixture(options: { expired?: boolean; ctor?: any } = {}) {
  const s = sqlState(); const providerCalls: string[] = [];
  const Ctor = options.ctor ?? AccountDO;
  const account: any = new Ctor(s.ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: (async (url: string) => {
    providerCalls.push(url); throw new Error('passive retrieval must never call the provider');
  }) as any });
  const call = (path: string, method = 'GET', body?: unknown, actor: unknown = admin) => account.fetch(req(path, method, body, actor));
  assert.equal((await call('/api/account', 'PATCH', { provider: 'claude', enabled: true })).status, 200);
  assert.equal((await call('/api/account/credentials', 'PUT', { expected_generation: 0,
    files: claudeFiles(Date.now() + (options.expired ? -1000 : 86_400_000)) })).status, 200);
  assert.equal((await call('/api/account/hosts', 'PUT', { hosts: ['h1'] })).status, 200);
  return { ...s, account, call, providerCalls, passive: (g: unknown, extra = '', actor: unknown = host) =>
    call(`/api/account/credentials?passive_generation=${g}${extra}`, 'GET', undefined, actor) };
}

test('passive retrieval returns the exact current generation refresh-stripped with no writes, ack, provider call or alarm change', async () => {
  const f = await accountFixture();
  const alarm = f.ctx.alarm, changes = totalChanges(f.database);
  const response = await f.passive(1);
  assert.equal(response.status, 200);
  const text = await response.text(), body = JSON.parse(text);
  assert.equal(body.generation, 1); assert.equal(body.provider, 'claude'); assert.equal(body.passive, true);
  assert.equal(body.refresh_material, false); assert.equal(body.static_key, false);
  const auth = JSON.parse(body.files[PATH]).claudeAiOauth;
  assert.equal(auth.accessToken, ACCESS); assert.equal(auth.refreshToken, '', 'refresh token stripped');
  assert.ok(!text.includes(REFRESH), 'no refresh material anywhere in the response');
  assert.ok(body.access_expires_at > Date.now());
  assert.equal(totalChanges(f.database), changes, 'read-only: zero database writes');
  assert.equal(f.ctx.alarm, alarm, 'renewal alarm untouched');
  assert.equal((f.database.prepare("SELECT ack_generation FROM approved_hosts WHERE id='h1'").get() as any).ack_generation ?? null, null, 'never acknowledges');
  assert.equal(f.providerCalls.length, 0);
});

test('passive retrieval refuses, with the maintained codes, every unsafe state and never returns material', async () => {
  const f = await accountFixture();
  const refusal = async (response: Response, status: number, code: string) => {
    const text = await response.text(); assert.equal(response.status, status, text);
    assert.equal(JSON.parse(text).error, code); assert.ok(!text.includes(ACCESS) && !text.includes(REFRESH), 'no material in a refusal');
  };
  await refusal(await f.passive(1, '', other), 403, 'host_not_approved');
  await refusal(await f.passive(1, '', { id: 'agent:x', kind: 'agent', scopes: [] }), 403, 'host_not_approved');
  await refusal(await f.passive(2), 409, 'generation_conflict');
  for (const bad of ['0', '1e3', 'abc', '9007199254740993', '']) await refusal(await f.passive(bad), 400, 'invalid_passive_generation');
  for (const extra of ['&min_validity_ms=0', '&metadata=1', '&expected_generation=1', '&passive_generation=1'])
    await refusal(await f.passive(1, extra), 400, 'invalid_passive_generation');

  await refusal(await f.passive(1, '&refresh_lease=x'), 409, 'refresh_permit_mismatch');

  // The existing observation and metadata modes are untouched: still no material.
  const observed = await (await f.call('/api/account/credentials?expected_generation=1', 'GET', undefined, host)).text();
  assert.deepEqual(JSON.parse(observed), { generation: 1, provider: 'claude', status: 'current' });
  assert.ok(!(await (await f.call('/api/account/credentials?metadata=1', 'GET', undefined, host)).text()).includes(ACCESS));

  // Rejected generation, live lease and unresolved candidate.
  assert.equal((await f.call('/api/account/refresh-lease', 'POST', { generation: 1 }, host)).status, 200);
  await refusal(await f.passive(1), 409, 'refresh_in_progress');
  f.database.prepare('UPDATE credentials SET refresh_owner=NULL,refresh_until=0,refresh_lease=NULL').run();
  f.database.prepare("INSERT INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES('c1','hub:central',1,'',1,'pending')").run(); f.account.db.cache?.clear?.();
  await refusal(await f.passive(1), 503, 'refresh_outcome_uncertain');
  f.database.prepare('DELETE FROM credential_candidates').run(); f.account.db.cache?.clear?.();
  assert.equal((await f.call('/api/account/credential-rejection', 'POST', { generation: 1, reason: 'provider_unauthorized' }, host)).status, 200);
  await refusal(await f.passive(1), 409, 'credential_generation_rejected');
  assert.equal(f.providerCalls.length, 0);
});

test('passive retrieval fails closed on an expired access token and never refreshes it', async () => {
  const f = await accountFixture({ expired: true });
  const changes = totalChanges(f.database);
  const response = await f.passive(1);
  assert.equal(response.status, 409); assert.equal((await response.json() as any).error, 'access_lifetime_insufficient');
  assert.equal(f.providerCalls.length, 0, 'an expired token is not refreshed passively');
  assert.equal(totalChanges(f.database), changes);
});

test('passive retrieval rechecks after the awaited unseal: new generation, lease or revoked approval never yields old material', async () => {
  for (const change of ['generation', 'lease', 'revoked'] as const) {
    const held = holdOpen();
    const f = await accountFixture({ ctor: held.Blocking });
    held.gate.armed = true;
    const pending = f.passive(1);
    await held.entered;
    if (change === 'generation') assert.equal((await f.call('/api/account/credentials', 'PUT', { expected_generation: 1,
      files: claudeFiles(Date.now() + 86_400_000) })).status, 200);
    else if (change === 'lease') assert.equal((await f.call('/api/account/refresh-lease', 'POST', { generation: 1 }, host)).status, 200);
    else assert.equal((await f.call('/api/account/hosts', 'PUT', { hosts: ['h2'] })).status, 200);
    held.gate.release!();
    const response = await pending, text = await response.text();
    assert.deepEqual([response.status, JSON.parse(text).error],
      change === 'generation' ? [409, 'generation_conflict'] : change === 'lease' ? [409, 'refresh_in_progress'] : [403, 'host_not_approved'], change);
    assert.ok(!text.includes(ACCESS), change + ': stale material never returned');
  }
});

// --- ControlDO route -----------------------------------------------------------------

async function controlFixture(options: { expired?: boolean; afterPassive?: (account: any, a: ReturnType<typeof sqlState>) => Promise<void> | void } = {}) {
  const a = await accountFixture({ expired: options.expired });
  const stub = { fetch: async (input: Request) => {
    const response = await a.account.fetch(input);
    if (options.afterPassive && new URL(input.url).searchParams.has('passive_generation')) await options.afterPassive(a.account, a);
    return response;
  } };
  const c = sqlState();
  const control: any = new ControlDO(c.ctx, { ACCOUNTS: { idFromName: (id: string) => id, get: () => stub }, HUB2_CREDENTIAL_KEY: KEY } as any);
  const call = (path: string, method = 'GET', body?: unknown, actor: unknown = host) => control.fetch(new Request('https://control.test' + path, {
    method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
  return { ...a, control, controlDatabase: c.database, call };
}

test('Control passive route returns revision-fenced refresh-stripped material with no refresh, lease or ack', async () => {
  const f = await controlFixture();
  const revision = ((await (await f.call('/api/engines', 'GET', undefined, admin)).json()) as any).engines[0].revision;
  const changes = totalChanges(f.database), calls = f.providerCalls.length;
  const response = await f.call(ROUTE);
  assert.equal(response.status, 200);
  const text = await response.text(), body = JSON.parse(text);
  assert.deepEqual(Object.keys(body).sort(), ['files', 'generation', 'label', 'revision', 'status']);
  assert.deepEqual([body.label, body.generation, body.revision, body.status], ['hub-claude', 1, revision, 'current']);
  assert.deepEqual(Object.keys(body.files), [PATH]);
  assert.equal(JSON.parse(body.files[PATH]).claudeAiOauth.refreshToken, '');
  assert.ok(!text.includes(REFRESH));
  assert.equal(totalChanges(f.database), changes, 'account database unchanged');
  assert.equal((f.database.prepare("SELECT ack_generation FROM approved_hosts WHERE id='h1'").get() as any).ack_generation ?? null, null);
  assert.equal(f.providerCalls.length, calls, 'the route made no provider call');
});

test('Control passive route is machine-only, approved-host-only, exact-shaped and maps account refusals', async () => {
  const f = await controlFixture();
  for (const actor of [admin, { id: 'agent:t', kind: 'agent', scopes: ['quota:write'] }, null])
    assert.equal((await f.call(ROUTE, 'GET', undefined, actor)).status, 403);
  assert.equal((await f.call(ROUTE, 'GET', undefined, other)).status, 403, 'unapproved host');
  assert.equal((await f.call(ROUTE + '?generation=1')).status, 400, 'no extra query');
  assert.equal((await f.call(ROUTE, 'POST', {})).status, 404);
  const withBody = new Request('https://control.test' + ROUTE, { method: 'GET', headers: { 'x-hub2-actor': JSON.stringify(host) } });
  Object.defineProperty(withBody, 'text', { value: async () => '{}' });
  assert.equal((await f.control.fetch(withBody)).status, 400, 'no body');

  const calls = f.providerCalls.length;
  assert.equal((await f.call(ROUTE)).status, 200, 'baseline is current');
  // Account refusals surface through the Control route unchanged.
  const lease = await f.account.fetch(req('/api/account/refresh-lease', 'POST', { generation: 1 }, host));
  assert.equal(lease.status, 200);
  const busy = await f.call(ROUTE);
  assert.deepEqual([busy.status, (await busy.json() as any).error], [409, 'refresh_in_progress']);
  f.database.prepare('UPDATE credentials SET refresh_owner=NULL,refresh_until=0,refresh_lease=NULL').run();
  f.account.db.cache?.clear?.();
  f.database.prepare("INSERT INTO credential_candidates(id,host_id,base_generation,sealed,observed_at,state) VALUES('c1','hub:central',1,'',1,'pending')").run();
  f.account.db.cache?.clear?.();
  const uncertain = await f.call(ROUTE);
  assert.deepEqual([uncertain.status, (await uncertain.json() as any).error], [503, 'refresh_outcome_uncertain']);
  f.database.prepare('DELETE FROM credential_candidates').run();
  assert.equal((await f.account.fetch(req('/api/account/credential-rejection', 'POST', { generation: 1, reason: 'provider_unauthorized' }, host))).status, 200);
  const rejected = await f.call(ROUTE);
  assert.deepEqual([rejected.status, (await rejected.json() as any).error], [409, 'credential_generation_rejected']);
  assert.equal(f.providerCalls.length, calls);
});

test('Control passive route fails closed on an expired credential without any provider call', async () => {
  const f = await controlFixture({ expired: true });
  const response = await f.call(ROUTE);
  assert.equal(response.status, 409);
  assert.ok(!(await response.text()).includes(ACCESS));
  assert.equal(f.providerCalls.length, 0);
});

test('Control passive route never answers when the generation or the desired revision moves across its awaits', async () => {
  const moved = await controlFixture({ afterPassive: async (account) => {
    assert.equal((await account.fetch(req('/api/account/credentials', 'PUT', { expected_generation: 1, files: claudeFiles(Date.now() + 86_400_000) }))).status, 200);
  } });
  const generation = await moved.call(ROUTE), generationText = await generation.text();
  assert.deepEqual([generation.status, JSON.parse(generationText).error], [409, 'generation_conflict']);
  assert.ok(!generationText.includes(ACCESS));

  // Move the desired Claude version while the account request is in flight.
  const rolled = await controlFixture();
  const original = rolled.control.accountRequest.bind(rolled.control);
  rolled.control.accountRequest = async (path: string, ...rest: unknown[]) => {
    const response = await original(path, ...rest);
    if (path.includes('passive_generation'))
    { rolled.controlDatabase.prepare("INSERT OR REPLACE INTO hub2_engine_rollout(engine,desired_version,latest,state,updated_at) VALUES('claude','0.0.1-synthetic','0.0.1-synthetic','stable',?)").run(Date.now());
      rolled.control.db.cache?.clear?.(); }
    return response;
  };
  const changed = await rolled.call(ROUTE), changedText = await changed.text();
  assert.deepEqual([changed.status, JSON.parse(changedText).error], [409, 'revision_changed']);
  assert.ok(!changedText.includes(ACCESS));
});

test('Control passive route rechecks the rollout after the final metadata await, immediately before responding', async () => {
  const f = await controlFixture();
  const original = f.control.accountRequest.bind(f.control);
  let metadataCalls = 0, release!: () => void, entered!: () => void;
  const held = new Promise<void>(r => { release = r; }), reached = new Promise<void>(r => { entered = r; });
  f.control.accountRequest = async (path: string, ...rest: unknown[]) => {
    const response = await original(path, ...rest);
    // The first metadata read captures the generation; the second is the final fence. Hold that one open.
    if (path.includes('metadata=1') && ++metadataCalls === 2) { entered(); await held; }
    return response;
  };
  const pending = f.call(ROUTE);
  await reached;
  // Generation is unchanged and the earlier revision check already passed; now move the desired version.
  f.controlDatabase.prepare("INSERT OR REPLACE INTO hub2_engine_rollout(engine,desired_version,latest,state,updated_at) VALUES('claude','0.0.2-synthetic','0.0.2-synthetic','stable',?)").run(Date.now());
  f.control.db.cache?.clear?.();
  release();
  const response = await pending, text = await response.text();
  assert.deepEqual([response.status, JSON.parse(text).error], [409, 'revision_changed']);
  assert.ok(!text.includes(ACCESS), 'old-revision material is never returned');
  assert.equal(metadataCalls, 2);
});
