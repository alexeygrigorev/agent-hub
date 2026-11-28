// Run: node --experimental-transform-types --test hub2/tests/account-credential-renewal.test.ts
// Proactive, expiry-driven Claude credential renewal in AccountDO. Synthetic
// credentials and a synthetic provider only; no live services.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { registerTypeScriptResolveHook } from '../node/compat.ts';
import { Hub2Runtime } from '../node/runtime.ts';

registerTypeScriptResolveHook();
const { AccountDO } = await import('../src/account-do.ts');

const LEAD = 20 * 60_000;
const HOUR = 3600_000;
const KEY = 'c1'.repeat(32);
const TOKEN_URL = 'https://platform.claude.com/v1/oauth/token';
const USAGE_URL = 'https://api.anthropic.com/api/oauth/usage';
const admin = { id: 'human:test', kind: 'operator', scopes: ['operator:admin'] };
const host = { id: 'machine:h1', kind: 'machine', resource_id: 'h1', scopes: [] };

let clock = Date.parse('2026-11-12T20:00:00Z');
const realNow = Date.now;
function useClock(): () => void { Date.now = () => clock; return () => { Date.now = realNow; }; }

function state() {
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
function request(path: string, method = 'GET', body?: unknown, actor: unknown = admin) {
  return new Request('https://hub2.test' + path, { method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(actor) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
}
function claudeFiles(expiresAt: number, access = 'synthetic-access-1', refresh = 'synthetic-refresh-1') {
  return { '.claude/.credentials.json': JSON.stringify({ claudeAiOauth: { accessToken: access, refreshToken: refresh, expiresAt } }) };
}
type Reply = Response | (() => Response | Promise<Response>);
function provider() {
  const calls: { url: string; auth?: string; body?: any }[] = []; const replies: Reply[] = []; let rotated = 0;
  const fetcher = async (url: string, init: any) => {
    calls.push({ url, auth: init?.headers?.Authorization, body: init?.body ? JSON.parse(init.body) : undefined });
    const next = replies.shift();
    if (next) return typeof next === 'function' ? await next() : next;
    if (url === TOKEN_URL) { rotated++; return Response.json({ access_token: `synthetic-access-${rotated + 1}`, refresh_token: `synthetic-refresh-${rotated + 1}`, expires_in: 8 * 3600 }); }
    if (url === USAGE_URL) return Response.json({ five_hour: { utilization: 10 }, seven_day: { utilization: 20 } });
    throw new Error('unexpected synthetic provider URL');
  };
  return { calls, replies, fetcher, tokenCalls: () => calls.filter(c => c.url === TOKEN_URL).length };
}
async function claudeAccount(expiresAt: number, env: any = {}) {
  const s = state(), p = provider();
  const account: any = new AccountDO(s.ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: p.fetcher as any, ...env });
  assert.equal((await account.fetch(request('/api/account', 'PATCH', { provider: 'claude', enabled: true }))).status, 200);
  assert.equal((await account.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 0, files: claudeFiles(expiresAt) }))).status, 200);
  return { ...s, p, account };
}
async function renewal(account: any) { return (await (await account.fetch(request('/api/account/renewal'))).json() as any).renewal; }
async function settled(check: () => boolean) { for (let i = 0; i < 200 && !check(); i++) await new Promise(r => setTimeout(r, 5)); }
const generation = (database: DatabaseSync) => (database.prepare('SELECT generation FROM credentials WHERE id=1').get() as any).generation;

test('credential import arms the alarm at expiry minus lead and the alarm renews through the central CAS', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + 2 * HOUR;
    const { ctx, database, p, account } = await claudeAccount(expiresAt);
    assert.equal(ctx.alarm, expiresAt - LEAD, 'imported credential arms renewal before expiry');
    assert.deepEqual(await renewal(account), { generation: 1, status: 'scheduled', attempts: 0, next_at: expiresAt - LEAD, access_expires_at: expiresAt, reason: null, updated_at: clock });

    clock = expiresAt - LEAD - 1000; await account.alarm();
    assert.equal(p.calls.length, 0, 'an early wake never refreshes');
    assert.equal(generation(database), 1);

    clock = expiresAt - LEAD; await account.alarm();
    assert.equal(p.tokenCalls(), 1);
    assert.deepEqual(p.calls[0].body, { grant_type: 'refresh_token', refresh_token: 'synthetic-refresh-1', client_id: '9d1c250a-e61b-44d9-88ed-5944d1962f5e' });
    assert.equal(generation(database), 2, 'renewal publishes exactly one new generation');
    assert.equal((database.prepare("SELECT state FROM credential_candidates WHERE host_id='hub:central'").get() as any).state, 'published');
    const renewedExpiry = clock + 8 * HOUR, rearm = clock + 6 * HOUR; // bounded re-evaluation horizon
    const view = await renewal(account);
    assert.equal(view.generation, 2); assert.equal(view.status, 'scheduled'); assert.equal(view.attempts, 0);
    assert.equal(view.access_expires_at, renewedExpiry); assert.equal(view.next_at, rearm);
    assert.equal(ctx.alarm, rearm, 'the new generation rearms itself');

    clock = rearm; await account.alarm();
    assert.equal(p.tokenCalls(), 1, 'a horizon wake re-evaluates without refreshing a fresh token');
    assert.equal((await renewal(account)).next_at, renewedExpiry - LEAD); assert.equal(ctx.alarm, renewedExpiry - LEAD);
  } finally { restore(); }
});

test('already-expired credentials renew at the first alarm and credentials without refresh material never schedule', async () => {
  const restore = useClock();
  try {
    const { ctx, database, p, account } = await claudeAccount(clock - 1000);
    assert.equal(ctx.alarm, clock + 100, 'expired access is due immediately (bounded minimum delay)');
    await account.alarm();
    assert.equal(p.tokenCalls(), 1); assert.equal(generation(database), 2);

    const s = state(), q = provider(); const bare: any = new AccountDO(s.ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: q.fetcher as any });
    await bare.fetch(request('/api/account', 'PATCH', { provider: 'claude', enabled: true }));
    const files = { '.claude/.credentials.json': JSON.stringify({ claudeAiOauth: { accessToken: 'synthetic-access-only', expiresAt: clock + HOUR } }) };
    assert.equal((await bare.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 0, files }))).status, 200);
    assert.equal((await renewal(bare)).status, 'no_refresh_token');
    assert.equal(s.ctx.alarm, null); await bare.alarm(); assert.equal(q.calls.length, 0);
  } finally { restore(); }
});

test('existing credentials from a pre-renewal database arm on construction without re-import', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + 3 * HOUR;
    const { ctx, database } = await claudeAccount(expiresAt);
    database.exec('DROP TABLE credential_renewal'); ctx.alarm = null; // the state an upgraded deployment starts from
    const p = provider(); const restarted: any = new AccountDO(ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: p.fetcher as any });
    await settled(() => ctx.alarm !== null);
    assert.equal(ctx.alarm, expiresAt - LEAD, 'construction alone rearms an existing generation');
    assert.equal((await renewal(restarted)).generation, 1);
    assert.equal(p.calls.length, 0, 'arming never contacts the provider');
  } finally { restore(); }
});

test('a live host refresh lease defers renewal with bounded retry; an exact not-started outcome lets it complete', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + HOUR;
    const { ctx, database, p, account } = await claudeAccount(expiresAt);
    await account.fetch(request('/api/account/hosts', 'PUT', { hosts: ['h1'] }));
    clock = expiresAt - LEAD;
    const lease = await (await account.fetch(request('/api/account/refresh-lease', 'POST', { generation: 1 }, host))).json() as any;
    assert.equal(typeof lease.lease, 'string');
    await account.alarm();
    assert.equal(p.calls.length, 0, 'the host owns this generation: no central provider call');
    let view = await renewal(account);
    assert.equal(view.status, 'retrying'); assert.equal(view.attempts, 1); assert.equal(view.reason, 'refresh_in_progress');
    assert.equal(view.next_at, lease.expires_at + 1000, 'retry waits past the live lease');
    assert.equal(ctx.alarm, view.next_at);

    assert.equal((await account.fetch(request('/api/account/refresh-outcome', 'POST', { generation: 1, lease: lease.lease, outcome: 'not-started' }, host))).status, 200);
    clock = view.next_at; await account.alarm();
    assert.equal(p.tokenCalls(), 1); assert.equal(generation(database), 2);
    view = await renewal(account); assert.equal(view.generation, 2); assert.equal(view.attempts, 0);
  } finally { restore(); }
});

test('an expired host lease with no outcome is uncertain: renewal stops instead of replaying the refresh token', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + HOUR;
    const { ctx, p, account } = await claudeAccount(expiresAt);
    await account.fetch(request('/api/account/hosts', 'PUT', { hosts: ['h1'] }));
    const lease = await (await account.fetch(request('/api/account/refresh-lease', 'POST', { generation: 1 }, host))).json() as any;
    clock = Math.max(expiresAt - LEAD, lease.expires_at + 1); await account.alarm();
    assert.equal(p.calls.length, 0);
    const view = await renewal(account);
    assert.equal(view.status, 'uncertain'); assert.equal(view.reason, 'refresh_outcome_uncertain'); assert.equal(view.next_at, null);
    assert.equal(ctx.alarm, null, 'no renewal wake remains for an uncertain generation');
    const alerts = await (await account.fetch(request('/api/account/alerts'))).json() as any;
    assert(alerts.items.some((a: any) => a.kind === 'credential_renewal_uncertain'));
    // The exact owner later proves no request occurred: renewal resumes.
    assert.equal((await account.fetch(request('/api/account/refresh-outcome', 'POST', { generation: 1, lease: lease.lease, outcome: 'not-started' }, host))).status, 200);
    assert.equal((await renewal(account)).status, 'retrying'); assert.equal(ctx.alarm, clock + 100);
    await account.alarm(); assert.equal(p.tokenCalls(), 1);
  } finally { restore(); }
});

test('a central network failure becomes uncertain and is never retried; a definite refusal stops immediately', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + HOUR;
    const a = await claudeAccount(expiresAt);
    a.p.replies.push(() => { throw new Error('synthetic network failure'); });
    clock = expiresAt - LEAD; await a.account.alarm();
    assert.equal(a.p.tokenCalls(), 1);
    assert.equal((await renewal(a.account)).status, 'uncertain');
    clock += 10 * 60_000; await a.account.alarm(); await a.account.alarm();
    assert.equal(a.p.tokenCalls(), 1, 'a possibly-rotated refresh token is never replayed');

    clock = Date.parse('2026-11-12T20:00:00Z');
    const b = await claudeAccount(clock + HOUR);
    b.p.replies.push(Response.json({ error: 'invalid_grant' }, { status: 400 }));
    clock += HOUR - LEAD; await b.account.alarm();
    const view = await renewal(b.account);
    assert.equal(view.status, 'refused'); assert.equal(view.reason, 'provider_refresh_invalid_grant'); assert.equal(view.next_at, null);
    assert.equal(b.ctx.alarm, null);
    clock += HOUR; await b.account.alarm(); assert.equal(b.p.tokenCalls(), 1);
  } finally { restore(); }
});

test('local failures retry with exponential backoff and stop after a bounded number of attempts', async () => {
  const restore = useClock();
  try {
    const env: any = {};
    const { ctx, p, account } = await claudeAccount(clock + HOUR, env);
    (account as any).env.HUB2_CREDENTIAL_KEY = 'invalid-key';
    clock += HOUR - LEAD;
    const delays: number[] = [];
    for (let i = 0; i < 6; i++) {
      const before = clock; await account.alarm();
      const view = await renewal(account);
      assert.equal(view.attempts, i + 1); assert.equal(view.reason, 'credential_decryption_failed');
      if (view.status === 'retrying') { delays.push(view.next_at - before); assert.equal(ctx.alarm, view.next_at); clock = view.next_at; }
      else { assert.equal(view.status, 'exhausted'); assert.equal(i, 5); assert.equal(ctx.alarm, null); }
    }
    assert.deepEqual(delays, [30_000, 60_000, 120_000, 240_000, 300_000]);
    assert.equal(p.calls.length, 0);
    const alerts = await (await account.fetch(request('/api/account/alerts'))).json() as any;
    assert(alerts.items.some((a: any) => a.kind === 'credential_renewal_exhausted'));
    // Explicit operator maintenance re-evaluates a stopped generation.
    (account as any).env.HUB2_CREDENTIAL_KEY = KEY;
    assert.equal((await account.fetch(request('/api/account/maintain', 'POST', {}))).status, 200);
    let view = await renewal(account);
    assert.equal(view.generation, 1); assert.equal(view.status, 'scheduled'); assert.equal(view.attempts, 0); assert.equal(view.next_at, clock);
    assert.equal(ctx.alarm, clock + 100);
    await account.alarm(); assert.equal(p.tokenCalls(), 1, 'the re-armed renewal uses the same central refresh path');
    view = await renewal(account); assert.equal(view.generation, 2); assert.equal(view.status, 'scheduled');
  } finally { restore(); }
});

test('renewal coexists with quota discovery: separate due times, discovery cadence unchanged, discovery sees the renewed token', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + HOUR;
    const { ctx, database, p, account } = await claudeAccount(expiresAt);
    assert.equal((await account.fetch(request('/api/account/discovery', 'PUT', { enabled: true, interval_ms: 6 * HOUR }))).status, 200);
    assert.equal(ctx.alarm, clock + 100, 'a newly enabled discovery is due first');
    clock += 100; await account.alarm();
    assert.deepEqual(p.calls.map(c => c.url), [USAGE_URL]);
    const discoveryNext = clock + 6 * HOUR;
    assert.equal((database.prepare('SELECT next_at FROM discovery').get() as any).next_at, discoveryNext);
    assert.equal(ctx.alarm, expiresAt - LEAD, 'renewal is earlier than the next discovery');

    clock = expiresAt - LEAD; await account.alarm();
    assert.deepEqual(p.calls.map(c => c.url), [USAGE_URL, TOKEN_URL], 'renewal wake does not run discovery');
    assert.equal((database.prepare('SELECT next_at FROM discovery').get() as any).next_at, discoveryNext, 'discovery cadence untouched');
    assert.equal(ctx.alarm, Math.min(discoveryNext, clock + 6 * HOUR));

    // Both due together: renewal runs first, so the probe uses the renewed access token.
    database.prepare('UPDATE credential_renewal SET next_at=?').run(clock); database.prepare('UPDATE discovery SET next_at=?').run(clock);
    (account as any).db.cache.clear();
    const { sealed } = database.prepare('SELECT sealed FROM credentials').get() as any;
    const nearExpiry = await (account as any).seal(claudeFiles(clock + 60_000, 'synthetic-access-2', 'synthetic-refresh-2'), 'claude', 2);
    assert.notEqual(sealed, nearExpiry); database.prepare('UPDATE credentials SET sealed=?').run(nearExpiry); (account as any).db.cache.clear();
    await account.alarm();
    assert.deepEqual(p.calls.slice(2).map(c => c.url), [TOKEN_URL, USAGE_URL]);
    assert.equal(p.calls[3].auth, 'Bearer synthetic-access-3');
    assert.equal((database.prepare('SELECT source FROM quota').get() as any).source, 'provider:claude:generation:3');
  } finally { restore(); }
});

test('renewal diagnostics and alerts are secret-free and scoped; non-Claude accounts are untouched', async () => {
  const restore = useClock();
  try {
    const { p, account } = await claudeAccount(clock + HOUR);
    p.replies.push(Response.json({ error: 'invalid_grant', error_description: 'synthetic-refresh-1 revoked' }, { status: 400 }));
    clock += HOUR - LEAD; await account.alarm();
    const surfaces = [await (await account.fetch(request('/api/account/renewal'))).text(), await (await account.fetch(request('/api/account/alerts'))).text()];
    for (const text of surfaces) for (const secret of ['synthetic-access', 'synthetic-refresh', 'revoked', 'claudeAiOauth']) assert.equal(text.includes(secret), false, secret);
    assert.equal((await account.fetch(request('/api/account/renewal', 'GET', undefined, { id: 'agent:x', kind: 'agent', scopes: [] }))).status, 403);
    assert.equal((await account.fetch(request('/api/account/renewal', 'GET', undefined, host))).status, 403);
    assert.equal((await account.fetch(request('/api/account/renewal', 'GET', undefined, { id: 'reader', kind: 'operator', scopes: ['quota:read'] }))).status, 200);

    const s = state(), q = provider(); const codex: any = new AccountDO(s.ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: q.fetcher as any });
    await codex.fetch(request('/api/account', 'PATCH', { provider: 'codex', enabled: true }));
    await codex.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 0, files: { '.codex/auth.json': JSON.stringify({ tokens: { access_token: 'synthetic', refresh_token: 'synthetic' } }) } }));
    assert.equal(s.ctx.alarm, null); assert.equal(s.database.prepare('SELECT COUNT(*) AS n FROM credential_renewal').get()!.n, 0);
    assert.deepEqual(await (await codex.fetch(request('/api/account/renewal'))).json(), { provider: 'codex', renewal: null });
  } finally { restore(); }
});

test('on the Node runtime a persisted renewal alarm survives restart and renews without any fetch', async () => {
  const restore = useClock();
  const parent = resolve('.local/tmp'); mkdirSync(parent, { recursive: true });
  const dataDir = mkdtempSync(join(parent, 'renewal-runtime-')), name = 'hub-claude', p = provider();
  const options: any = { classes: { ACCOUNTS: AccountDO }, dataDir, env: { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: p.fetcher },
    entry: { fetch: (r: Request, env: any) => env.ACCOUNTS.get(env.ACCOUNTS.idFromName(name)).fetch(r) } };
  let runtime = new Hub2Runtime(options);
  try {
    const expiresAt = clock + 2 * HOUR;
    await runtime.fetch(request('/api/account', 'PATCH', { provider: 'claude', enabled: true }));
    assert.equal((await runtime.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 0, files: claudeFiles(expiresAt) }))).status, 200);
    await runtime.dispose();
    runtime = new Hub2Runtime(options);
    const container: any = runtime.containerOf('ACCOUNTS', name);
    assert.ok(container, 'the persisted renewal alarm restores the account before any request');
    // The first sweep constructs the account, whose init rearms the same
    // generation; the runtime then defers delivery to a later 1s sweep.
    clock = expiresAt - LEAD;
    for (let sweep = 0; sweep < 3; sweep++) { await container.pollAlarm(clock); if (p.tokenCalls() > 0) break; clock += 1000; }
    await container.pollAlarm(clock);
    assert.equal(p.tokenCalls(), 1);
    const db = new DatabaseSync(join(dataDir, container.file), { readOnly: true });
    try {
      assert.equal((db.prepare('SELECT generation FROM credentials').get() as any).generation, 2);
      assert.equal((db.prepare('SELECT due FROM hub2_alarm').get() as any).due, clock + 6 * HOUR, 'handler rearm wins over the runtime retry');
    } finally { db.close(); }
  } finally { await runtime.dispose(); rmSync(dataDir, { recursive: true }); restore(); }
});

// Review B1: a scheduler blocked in unseal for generation 1 must not delete or
// overwrite the alarm a concurrent import published for generation 2.
async function blockedScheduler(blockedGeneration: number) {
  const gate: { release?: () => void; entered?: () => void; armed: boolean } = { armed: false };
  const entered = new Promise<void>(r => { gate.entered = r; });
  const released = new Promise<void>(r => { gate.release = r; });
  const base = (AccountDO as any).prototype.unseal;
  class BlockingAccountDO extends (AccountDO as any) {
    async unseal(value: string, providerName: string, gen: number) {
      if (gate.armed && gen === blockedGeneration) { gate.armed = false; gate.entered!(); await released; }
      return base.call(this, value, providerName, gen);
    }
  }
  const s = state(), p = provider();
  const account: any = new BlockingAccountDO(s.ctx, { HUB2_CREDENTIAL_KEY: KEY, HUB2_PROVIDER_FETCH: p.fetcher as any });
  return { ...s, p, account, gate, entered };
}

test('a scheduler blocked in unseal never deletes or overwrites the newer generation alarm', async () => {
  const restore = useClock();
  try {
    const { ctx, database, account, gate, entered } = await blockedScheduler(1);
    assert.equal((await account.fetch(request('/api/account', 'PATCH', { provider: 'claude', enabled: true }))).status, 200);
    const expiresAt1 = clock + 2 * HOUR;
    assert.equal((await account.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 0, files: claudeFiles(expiresAt1) }))).status, 200);
    assert.equal(ctx.alarm, expiresAt1 - LEAD);

    // Scheduler A re-reads generation 1 (no renewal row) and blocks inside unseal.
    database.prepare('DELETE FROM credential_renewal').run(); (account as any).db.cache?.clear?.();
    gate.armed = true;
    // The alarm path has no outer request rearm to rescue a wrong publish.
    const a = account.alarm();
    await entered;

    // Concurrent import B publishes generation 2 and arms its own alarm.
    const expiresAt2 = clock + 5 * HOUR;
    assert.equal((await account.fetch(request('/api/account/credentials', 'PUT', { expected_generation: 1, files: claudeFiles(expiresAt2, 'synthetic-access-g2', 'synthetic-refresh-g2') }))).status, 200);
    assert.equal(generation(database), 2);
    assert.equal(ctx.alarm, expiresAt2 - LEAD, 'generation 2 armed before A resumes');

    gate.release!(); await a;
    assert.equal(ctx.alarm, expiresAt2 - LEAD, 'A resume recomputes instead of deleting/overwriting the generation 2 alarm');
    const view = await renewal(account);
    assert.equal(view.generation, 2); assert.equal(view.status, 'scheduled'); assert.equal(view.next_at, expiresAt2 - LEAD);
  } finally { restore(); }
});

test('an unexpected renewal read failure keeps a bounded retry alarm rather than reading as no work', async () => {
  const restore = useClock();
  try {
    const { ctx, account } = await claudeAccount(clock + 2 * HOUR);
    (account as any).renewalRow = () => { throw new Error('synthetic storage failure'); };
    await (account as any).schedule();
    assert.equal(ctx.alarm, clock + 5 * 60_000, 'unknown is retried, never deleted');
  } finally { restore(); }
});

test('a successful reschedule clears the stale diagnostic left by an initial unseal failure', async () => {
  const restore = useClock();
  try {
    const expiresAt = clock + 2 * HOUR;
    const { ctx, database, p, account } = await claudeAccount(expiresAt);
    // Initial evaluation fails to unseal: a due-now row records the bounded diagnostic.
    database.prepare('DELETE FROM credential_renewal').run(); (account as any).db.cache?.clear?.();
    (account as any).env.HUB2_CREDENTIAL_KEY = 'invalid-key';
    await (account as any).schedule();
    let view = await renewal(account);
    assert.equal(view.reason, 'credential_decryption_failed'); assert.equal(view.status, 'scheduled');
    assert.equal(view.attempts, 0); assert.equal(view.next_at, clock);

    // The key works again: the alarm unseals, sees a future expiry and only reschedules.
    (account as any).env.HUB2_CREDENTIAL_KEY = KEY;
    clock += 1000; await account.alarm();
    view = await renewal(account);
    assert.equal(view.reason, null, 'the obsolete diagnostic is cleared');
    assert.equal(view.status, 'scheduled'); assert.equal(view.attempts, 0);
    assert.equal(view.generation, 1); assert.equal(view.access_expires_at, expiresAt);
    assert.equal(view.next_at, expiresAt - LEAD); assert.equal(ctx.alarm, expiresAt - LEAD);
    assert.equal(p.tokenCalls(), 0, 'no refresh is performed');
    assert.equal(generation(database), 1);
  } finally { restore(); }
});
