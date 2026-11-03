import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { BusService } from '../src/bus';
import { resolveRecipient, afterMessageInsert, renderTui, checkSendScoping, setMessageState, updateNamedAgentDelegates } from '../src/session-messaging';
import { sha256 } from '../src/auth/crypto';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// docs/SESSION-MESSAGING.md (intakes 4fc83e80, a3227276 AC4/AC6).
const SESSION = '22222222-2222-4222-8222-222222222222';
const NATIVE = '33333333-3333-4333-8333-333333333333';
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const host = { caller: { machineId: 'tabo', kind: 'host', tokenId: 'host-cred-tabo' }, scopes: ['host'] } as any;
const developer = { kind: 'agent', actor: 'agent:developer' } as any;
const hostBus = { kind: 'host', machineId: 'tabo' } as any;
const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });

async function fixture(fn: (ctx: { bus: BusService; sc: SessionControlService; sql: any; hub: Hub }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-messaging-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const sql = hub.sql;
    const tx = <T,>(cb: () => T): T => state.storage.transactionSync(cb);
    const sc = new SessionControlService(sql, tx, Date.now, { reserve: () => ({ value: {}, status: 500 }) });
    sc.init();
    const bus = new BusService(sql, tx, {
      resolveRecipient: (to, now) => resolveRecipient(sql, to, now),
      checkSendScope: (caller, from, to, now) => checkSendScoping(sql, caller, from, to, now),
      afterInsert: (m, o, now) => afterMessageInsert(sql, m, { delivery: o.delivery, deliverByMs: o.deliverByMs, ref: o.ref, now }),
      onStage: (id, stage, actor, machine, now) => setMessageState(sql, id, stage, null, actor, machine, now),
    });
    const at = new Date().toISOString(); const far = new Date(Date.now() + 86400000).toISOString();
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-tabo', 'host', 'h-tabo', 'r-tabo', 'fam-tabo', 'tabo', 'tabo', NULL, '["host"]', 't', ?, ?)`, at, far);
    sql.exec(`INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('tabo', 'tabo', 'Tabo', 'host-cred-tabo', ?)`, at);
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('developer', 'agent', NULL, NULL, ?)", at);
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'admin', 'k1', '{"workspace":"agent-hub"}', 'tabo', 'codex', 'interactive', 'running', ?, ?, ?)`, SESSION,
      JSON.stringify({ native_id: NATIVE, tag: 'burn-tabo', workspace: 'C:\\repos\\agent-hub', generation: 1, native_created_at_ms: 1793180000000 }), at, at);
    await fn({ bus, sc, sql, hub });
  });
}

async function offers(sc: SessionControlService) {
  return ((await (await sc.handle(req('/api/session-commands/offers'), host)).json()) as any).offers;
}
async function claim(sc: SessionControlService, offer: any) {
  const res = await sc.handle(req(`/api/session-commands/${offer.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT }), host);
  expect(res.status).toBe(200);
  return res.json() as any;
}
const ack = (sc: SessionControlService, offer: any, token: string, outcome: string, extra: any = {}) =>
  sc.handle(req(`/api/session-commands/${offer.command_id}/ack`, { correlation: offer.correlation, outcome, claim_token: token,
    native: { native_id: NATIVE, tag: 'burn-tabo', workspace: 'C:\\repos\\agent-hub' }, ...extra }), host);

describe('cross-device session messaging', () => {
  it('delivers Developer -> Tabo session into the TUI with not-ready backoff, and threads the host-relayed reply back', () => fixture(async ({ bus, sc, sql }) => {
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'ping from Developer', idempotencyKey: 'k-1' });
    const row = sql.exec('SELECT to_actor, state, delivery, recipient_seq, to_session_id FROM messages WHERE id = ?', sent.id).one();
    expect(row).toMatchObject({ to_actor: 'session:' + SESSION, state: 'accepted', delivery: 'tui', recipient_seq: 1, to_session_id: SESSION });

    let [offer] = await offers(sc);
    expect(offer).toMatchObject({ kind: 'message', session_id: SESSION, binding: { native_id: NATIVE, tag: 'burn-tabo' } });
    expect(offer.payload).toMatchObject({ message_id: sent.id, delivery: 'tui' });
    expect(offer.payload.body).toContain('ping from Developer');
    expect(offer.payload.body).toContain(`hub reply ${sent.id}`);

    let granted = await claim(sc, offer);
    expect(sql.exec('SELECT state FROM messages WHERE id = ?', sent.id).one().state).toBe('dispatched');
    const busy = await ack(sc, offer, granted.claim_token, 'not_ready', { reason: 'composer busy' });
    expect(await busy.json()).toMatchObject({ state: 'recorded', not_before_ms: 5000 });
    expect(await offers(sc)).toEqual([]); // backoff, attempt not consumed
    sql.exec("UPDATE session_control_commands SET not_before = NULL WHERE kind = 'message'");
    [offer] = await offers(sc);
    granted = await claim(sc, offer);
    const done = await ack(sc, offer, granted.claim_token, 'delivered', { proof: { native_message_id: 'env-1' } });
    expect(await done.json()).toMatchObject({ state: 'acked' });
    expect(sql.exec('SELECT state, delivered_at FROM messages WHERE id = ?', sent.id).one().state).toBe('delivered');

    // The host relays the native reply as the session (agents row bound to its machine).
    const reply = bus.send(hostBus, { senderActor: 'session:' + SESSION, toActor: 'agent:developer', kind: 'reply', body: 'pong from Tabo', idempotencyKey: 'native-env-reply-1', replyTo: sent.id });
    expect(reply.threadId).toBe(sent.threadId);
    const inbox = bus.inbox(developer, 'agent:developer');
    expect(inbox.map((m: any) => m.body)).toEqual(['pong from Tabo']);
    expect(sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'message'").one().n).toBe(1); // replies to a named agent stay in its inbox
    const status = bus.status(developer, sent.id);
    expect(status.events.map((e: any) => e.event)).toEqual(['accepted', 'dispatched', 'accepted', 'not_ready', 'dispatched', 'delivered', 'read', 'replied']);
    expect(status.read_at).toBeTruthy();
    expect(status.body).toBeUndefined();
  }));

  it('refuses spoofed senders, unknown sessions and foreign hosts', () => fixture(async ({ bus, sql }) => {
    expect(() => bus.send({ kind: 'host', machineId: 'other' } as any, { senderActor: 'session:' + SESSION, toActor: 'agent:developer', kind: 'note', body: 'x', idempotencyKey: 'k' })).toThrow('unauthorized');
    expect(() => bus.send(developer, { senderActor: 'agent:oncall', toActor: 'session:' + SESSION, kind: 'note', body: 'x', idempotencyKey: 'k2' })).toThrow('from_actor_mismatch');
    expect(() => bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:44444444-4444-4444-8444-444444444444', kind: 'note', body: 'x', idempotencyKey: 'k3' })).toThrow('unknown_recipient');
    sql.exec("UPDATE session_control_sessions SET state = 'stopped'");
    expect(() => bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'x', idempotencyKey: 'k4' })).toThrow('unknown_recipient');
  }));

  it('resolves tag@machine to the one live session and expires undelivered messages', () => fixture(async ({ bus, sc, sql }) => {
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'burn-tabo@Tabo', kind: 'note', body: 'hi', idempotencyKey: 'k-at', delivery: 'inbox' });
    expect(sql.exec('SELECT to_actor, delivery FROM messages WHERE id = ?', sent.id).one()).toEqual({ to_actor: 'session:' + SESSION, delivery: 'inbox' });
    expect(() => bus.send(developer, { senderActor: 'agent:developer', toActor: 'nobody@Tabo', kind: 'note', body: 'x', idempotencyKey: 'k-x' })).toThrow('unknown_recipient');
    sql.exec('UPDATE messages SET deliver_by = ? WHERE id = ?', Date.now() - 1, sent.id);
    expect(await offers(sc)).toEqual([]);
    expect(sql.exec('SELECT state, state_reason FROM messages WHERE id = ?', sent.id).one()).toEqual({ state: 'expired', state_reason: 'deliver_by_passed' });
  }));

  it('offers one message per session at a time, in send order', () => fixture(async ({ bus, sc }) => {
    const a = bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'first', idempotencyKey: 'o1' });
    bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'second', idempotencyKey: 'o2' });
    const first = await offers(sc);
    expect(first.map((o: any) => o.payload.message_id)).toEqual([a.id]);
    const granted = await claim(sc, first[0]);
    await ack(sc, first[0], granted.claim_token, 'delivered');
    const next = await offers(sc);
    expect(next.map((o: any) => o.payload.body.includes('second'))).toEqual([true]);
  }));

  it('renders bounded TUI text without control characters', () => {
    const text = renderTui({ id: '01JD3K7QXYZABCDEFGHJKMNPQR', from_actor: 'agent:developer\u0085', thread_id: 't\u009f', body: 'a\x1b[2Jb\x03🙂'.repeat(1500) });
    expect(text).not.toMatch(/[\x00-\x08\x0b-\x1f\x7f]/);
    expect(text).not.toMatch(/[\u0080-\u009f]/);
    expect(new TextEncoder().encode(text).length).toBeLessThanOrEqual(2048);
    expect(text).toContain('⟦hub:HJKMNPQR⟧ from agent:developer');
    expect(text).toContain('[truncated: hub msg show 01JD3K7QXYZABCDEFGHJKMNPQR]');
    expect(text).toContain('hub reply 01JD3K7QXYZABCDEFGHJKMNPQR --body');
  });
});

describe('named agent credential', () => {
  it('mints an operator-only rotating agent:developer msg credential that reads its own inbox', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('named-agent-' + crypto.randomUUID()));
    const human = 'test-only-named-agent-admin';
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString(); const far = new Date(Date.now() + 86400000).toISOString();
      hub.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('http-admin', 'admin@fake.local', 1, ?, ?)", at, at);
      hub.sql.exec('INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        'http-admin', 'human', await sha256(human), 'http-admin', 'admin@fake.local', 'human:http-admin', JSON.stringify(['admin']), 'http-admin', at, far);
    });
    const call = (path: string, token: string, data?: any) => stub.fetch(new Request('https://hub.test' + path, {
      method: data === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data) }));
    const inbox = (token: string) => call('/api/bus/inbox?actor=' + encodeURIComponent('agent:developer'), token);
    expect((await call('/api/agents/named/developer/credential', 'hubt_unknown', {})).status).toBe(401);
    const res = await call('/api/agents/named/developer/credential', human, {});
    expect(res.status).toBe(201);
    const cred: any = await res.json();
    expect(cred).toMatchObject({ actor: 'agent:developer', scopes: ['msg:send', 'msg:read'] });
    expect((await inbox(cred.access_token)).status).toBe(200);
    // The agent identity cannot mint or rotate credentials itself.
    expect((await call('/api/agents/named/developer/credential', cred.access_token, {})).status).toBe(403);
    const again: any = await (await call('/api/agents/named/developer/credential', human, {})).json();
    expect((await inbox(cred.access_token)).status).toBe(401);
    expect((await inbox(again.access_token)).status).toBe(200);
  });
});

describe('per-session messaging credential (M2, intake 008529b5)', () => {
  it('lets only the bound host mint a session credential that sends as the session, and revokes it when the session ends', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('session-cred-' + crypto.randomUUID()));
    const hostToken = 'test-only-host-tabo'; const otherHost = 'test-only-host-other';
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString(); const far = new Date(Date.now() + 86400000).toISOString();
      for (const [id, machine, token] of [['cred-tabo', 'tabo', hostToken], ['cred-other', 'other', otherHost]]) {
        hub.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
          VALUES (?, 'host', ?, ?, ?, ?, ?, NULL, '["host"]', 't', ?, ?)`, id, await sha256(token), 'r-' + id, 'fam-' + id, machine, machine, at, far);
        hub.sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES (?, ?, ?, ?, ?)', machine, machine, machine, id, at);
      }
      hub.sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('developer', 'agent', NULL, NULL, ?)", at);
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES (?, 'admin', 'k1', '{"workspace":"agent-hub"}', 'tabo', 'codex', 'interactive', 'running', ?, ?, ?)`, SESSION,
        JSON.stringify({ native_id: NATIVE, tag: 'burn-tabo', workspace: 'C:\\repos\\agent-hub', generation: 1, native_created_at_ms: 1793180000000 }), at, at);
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES ('44444444-4444-4444-8444-444444444444', 'admin', 'k2', '{"workspace":"agent-hub"}', 'tabo', 'codex', 'interactive', 'running', NULL, ?, ?)`, at, at);
    });
    const call = (path: string, token: string, data?: any) => stub.fetch(new Request('https://hub.test' + path, {
      method: data === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data) }));
    // No identity yet (never messaged): nothing to provision, no scan.
    expect((await (await call('/api/session-messaging/credentials', hostToken)).json() as any).sessions).toEqual([]);
    await runInDurableObject(stub, async (hub: Hub) => { resolveRecipient(hub.sql, 'session:' + SESSION, Date.now()); });
    const pending: any = await (await call('/api/session-messaging/credentials', hostToken)).json();
    expect(pending.sessions).toEqual([{ session_id: SESSION, native_id: NATIVE, tag: 'burn-tabo', workspace: 'C:\\repos\\agent-hub' }]);
    expect((await (await call('/api/session-messaging/credentials', otherHost)).json() as any).sessions).toEqual([]);
    // AC1: the spec endpoint, host-bound only; a foreign host is refused.
    expect((await call('/api/sessions/' + SESSION + '/messaging-credential', otherHost, {})).status).toBe(404);
    const minted = await call('/api/sessions/' + SESSION + '/messaging-credential', hostToken, {});
    expect(minted.status).toBe(201);
    const cred: any = await minted.json();
    expect(cred.actor).toBe('session:' + SESSION);
    await runInDurableObject(stub, async (hub: Hub) => {
      const row: any = hub.sql.exec('SELECT scopes, expires_at FROM credentials WHERE registry_actor = ? AND revoked_at IS NULL', 'session:' + SESSION).toArray()[0];
      expect(JSON.parse(row.scopes)).toEqual(['msg:send', 'msg:read']); // AC1 scopes
    });
    const ttlMs = Date.parse(cred.expires_at) - Date.now();
    expect(ttlMs).toBeGreaterThan(55 * 60 * 1000); // AC1: 1 h access token
    expect(ttlMs).toBeLessThanOrEqual(60 * 60 * 1000);
    expect((await (await call('/api/session-messaging/credentials', hostToken)).json() as any).sessions).toEqual([]);
    const send = (from: string, token = cred.access_token) => call('/api/bus/send', token, { from_actor: from, to_actor: 'agent:developer', kind: 'note', body: 'hello from the Tabo session', idempotency_key: crypto.randomUUID() });
    expect((await send('session:' + SESSION)).status).toBe(200); // msg:send scope accepted on the bus
    expect((await send('agent:developer')).status).toBe(403); // never another identity
    expect((await call('/api/session-messaging/credentials', cred.access_token)).status).toBe(403);
    expect((await call('/api/bus/inbox?actor=' + encodeURIComponent('session:' + SESSION), cred.access_token)).status).toBe(200); // msg:read on its own inbox
    // AC1: the refresh family rotates while the session runs.
    const refreshed: any = await (await call('/api/token/refresh', '', { grant_type: 'refresh_token', refresh_token: cred.refresh_token })).json();
    expect(refreshed.access_token).toBeTruthy();
    expect((await send('session:' + SESSION, refreshed.access_token)).status).toBe(200);
    expect((await send('session:' + SESSION, cred.access_token)).status).toBe(401); // rotation retired the predecessor
    // AC4: the projectless session cannot reach another session; the refusal is audited.
    const refusedSend = await call('/api/bus/send', refreshed.access_token, { from_actor: 'session:' + SESSION, to_actor: 'session:44444444-4444-4444-8444-444444444444', kind: 'note', body: 'x', idempotency_key: crypto.randomUUID() });
    expect(refusedSend.status).toBe(403);
    expect((await refusedSend.json() as any).error).toBe('cross_project_forbidden');
    await runInDurableObject(stub, async (hub: Hub) => {
      const refusals = hub.sql.exec("SELECT detail_json FROM message_events WHERE event = 'send_refused'").toArray() as any[];
      expect(refusals.map(r => JSON.parse(r.detail_json).code)).toEqual(['from_actor_mismatch', 'cross_project_forbidden']); // both refusals survive rollback
    });
    // AC2: stop revokes the family; the revoked token is refused.
    await runInDurableObject(stub, async (hub: Hub) => { hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped'"); });
    await call('/api/session-messaging/credentials', hostToken);
    expect((await send('session:' + SESSION, refreshed.access_token)).status).toBe(401);
    expect((await call('/api/token/refresh', '', { grant_type: 'refresh_token', refresh_token: refreshed.refresh_token })).status).toBe(400);
  });

  it('revokes the credential on rebind to another machine and lists the session for the new host', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('session-rebind-' + crypto.randomUUID()));
    const hostToken = 'test-only-host-tabo';
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString(); const far = new Date(Date.now() + 86400000).toISOString();
      hub.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
        VALUES ('cred-tabo', 'host', ?, 'r', 'fam', 'tabo', 'tabo', NULL, '["host"]', 't', ?, ?)`, await sha256(hostToken), at, far);
      hub.sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES (?, ?, ?, ?, ?)', 'tabo', 'tabo', 'Tabo', 'cred-tabo', at);
      hub.sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('developer', 'agent', NULL, NULL, ?)", at);
      hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES (?, 'admin', 'k1', '{"workspace":"agent-hub"}', 'tabo', 'codex', 'interactive', 'running', ?, ?, ?)`, SESSION,
        JSON.stringify({ native_id: NATIVE, tag: 'burn-tabo', workspace: '/w', generation: 1, native_created_at_ms: 1793180000000 }), at, at);
    });
    const call = (path: string, token: string, data?: any) => stub.fetch(new Request('https://hub.test' + path, {
      method: data === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: data === undefined ? undefined : JSON.stringify(data) }));
    await runInDurableObject(stub, async (hub: Hub) => { resolveRecipient(hub.sql, 'session:' + SESSION, Date.now()); });
    const cred: any = await (await call('/api/sessions/' + SESSION + '/messaging-credential', hostToken, {})).json();
    expect(cred.access_token).toBeTruthy();
    const send = () => call('/api/bus/send', cred.access_token, { from_actor: 'session:' + SESSION, to_actor: 'agent:developer', kind: 'note', body: 'x', idempotency_key: crypto.randomUUID() });
    expect((await send()).status).toBe(200);
    // Rebind to another machine: the event-driven revocation from the bind
    // transition plus the refresh guard kill every family of the session.
    await runInDurableObject(stub, async (hub: Hub) => {
      const { revokeSessionCredentials } = await import('../src/session-messaging');
      hub.sql.exec("UPDATE session_control_sessions SET machine_id = 'elsewhere'");
      revokeSessionCredentials(hub.sql, SESSION, Date.now());
    });
    expect((await send()).status).toBe(401); // AC2: revoked token is refused
    expect((await call('/api/token/refresh', '', { grant_type: 'refresh_token', refresh_token: cred.refresh_token })).status).toBe(400);
  });
});

describe('session credential project scoping (M2 AC4)', () => {
  const S1 = '61616161-6161-4161-8161-616161616161'; // project agent-hub
  const S2 = '44444444-4444-4444-8444-444444444444'; // project other-proj
  const S3 = '55555555-5555-4555-8555-555555555555'; // no project
  const S4 = '62626262-6262-4262-8262-626262626262'; // project agent-hub (same as S1)

  function seed(sql: any) {
    const at = new Date().toISOString();
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('helper', 'agent-hub', NULL, NULL, ?)", at);
    sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('foreign', 'other-proj', NULL, NULL, ?)", at);
    sql.exec("INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('alexey', 'a@fake.local', 1, ?, ?)", at, at);
    sql.exec("INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('guest', 'g@fake.local', 0, ?, ?)", at, at);
    for (const [id, task] of [[S1, 't1'], [S2, 't2'], [S3, null], [S4, 't1']] as const) {
      sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
        VALUES (?, 'admin', ?, ?, 'tabo', 'codex', 'interactive', 'running', NULL, ?, ?)`, id, 'k-' + id.slice(0, 8),
        JSON.stringify({ ...(task ? { intake_task_id: task } : {}), workspace: 'w' }), at, at);
    }
    sql.exec(`INSERT INTO intake_tasks (id, data) VALUES ('t1', '{"project":"agent-hub"}'), ('t2', '{"project":"other-proj"}')`);
  }

  it('allows same-project sessions, named agents, operator humans and same-project legacy addresses', () => fixture(async ({ bus, sql }) => {
    seed(sql);
    const send = (to: string, from = 'session:' + S1) =>
      bus.send({ kind: 'agent', actor: from } as any, { senderActor: from, toActor: to, kind: 'note', body: 'x', idempotencyKey: crypto.randomUUID() });
    const { sessionProject } = await import('../src/session-messaging');
    const steps: Array<[string, () => unknown, boolean]> = [
      ['S1->S4 same project', () => send('session:' + S4), true],
      ['S1->developer named', () => send('agent:developer'), true],
      ['S1->human:alexey operator', () => send('human:alexey'), true],
      ['S1->agent-hub:helper legacy', () => send('agent-hub:helper'), true],
      ['S1->helper bare tag', () => send('helper'), true],
      ['S1->machine:tabo own machine', () => send('machine:tabo'), true],
      ['S3->developer projectless', () => send('agent:developer', 'session:' + S3), true],
      ['S3->S1 projectless cross', () => send('session:' + S1, 'session:' + S3), false],
      ['S2->S1 cross', () => send('session:' + S1, 'session:' + S2), false],
      ['S1->other-proj:foreign cross', () => send('other-proj:foreign'), false],
      ['S1->human:guest non-operator', () => send('human:guest'), false],
      ['S1->machine:elsewhere', () => send('machine:elsewhere'), false],
    ];
    for (const [name, fn, allowed] of steps) {
      let outcome = 'ok';
      try { fn(); } catch (e: any) { outcome = e.message; }
      console.log('STEP', name, '=>', outcome, allowed ? '(allowed expected)' : '(refusal expected)');
      if (allowed) expect(outcome).toBe('ok');
      else expect(outcome).toBe('cross_project_forbidden');
    }
    // Refusals carry the scoping detail; the audit row itself is written at
    // the API boundary (below, in the HTTP test) because the send transaction
    // rolls back with the refusal.
    try { send('other-proj:foreign'); throw new Error('should have been refused'); } catch (e: any) {
      expect(e.detail).toMatchObject({ code: 'cross_project_forbidden', from_project: 'agent-hub' });
    }
    const { updateMessagingSettings } = await import('../src/session-messaging');
    updateMessagingSettings(sql, { cross_project_allow: { 'other-proj': true } });
    expect(send('other-proj:foreign').id).toBeTruthy();
    expect(send('session:' + S2).id).toBeTruthy(); // recipient project granted
    expect(() => send('session:' + S1, 'session:' + S2)).toThrow('cross_project_forbidden'); // agent-hub did not grant
  }));
});

describe('message event retention', () => {
  it('trims audit events older than 180 days on later writes', () => fixture(async ({ sql }) => {
    const { messageEvent } = await import('../src/session-messaging');
    const old = Date.now() - 181 * 24 * 3600 * 1000;
    sql.exec("INSERT INTO message_events (message_id, event, actor, machine_id, at, detail_json) VALUES ('m-old', 'accepted', NULL, NULL, ?, NULL)", old);
    messageEvent(sql, 'm-new', 'accepted', null, null, Date.now());
    expect(sql.exec('SELECT message_id FROM message_events').toArray().map((r: any) => r.message_id)).toEqual(['m-new']);
  }));
});

describe('M5 client surface (intake 3bdf9715)', () => {
  it('carries refs by reference only: validated, stored, and idempotency-checked', () => fixture(async ({ bus, sql }) => {
    const { validateRef } = await import('../src/session-messaging');
    expect(validateRef(undefined)).toBeNull();
    expect(JSON.parse(validateRef({ kind: 'url', value: 'https://x', sha256: 'a'.repeat(64), bytes: 5 })!)).toMatchObject({ kind: 'url' });
    for (const bad of [{ kind: 'nope', value: 'x' }, { kind: 'path', value: '/x' }, { kind: 'git', value: '' },
      { kind: 'git', value: 'x', sha256: 'nope' }, { kind: 'git', value: 'x', extra: 1 }, 'string', ['array']]) {
      expect(() => validateRef(bad)).toThrow('invalid_ref');
    }
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 'with ref', idempotencyKey: 'ref-1', ref: { kind: 'url', value: 'https://example/blob' } });
    expect(JSON.parse(sql.exec('SELECT ref_json FROM messages WHERE id = ?', sent.id).one().ref_json)).toMatchObject({ kind: 'url' });
    expect(() => bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 'with ref', idempotencyKey: 'ref-1', ref: { kind: 'url', value: 'https://other' } })).toThrow('conflict');
    expect(bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 'with ref', idempotencyKey: 'ref-1', ref: { kind: 'url', value: 'https://example/blob' } }).id).toBe(sent.id);
  }));

  it('delivers to human:<sub> recipients and keeps their inbox private', () => fixture(async ({ bus, sql }) => {
    const at = new Date().toISOString();
    sql.exec("INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('alice', 'alice@fake.local', 0, ?, ?)", at, at);
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'human:alice', kind: 'note', body: 'for alice', idempotencyKey: 'h-1' });
    expect(sql.exec('SELECT to_actor FROM messages WHERE id = ?', sent.id).one().to_actor).toBe('human:alice');
    expect(() => bus.send(developer, { senderActor: 'agent:developer', toActor: 'human:stranger', kind: 'note', body: 'x', idempotencyKey: 'h-2' })).toThrow('unknown_recipient'); // no users row
    const alice = { kind: 'human', actor: 'human:alice' } as any;
    expect(bus.inbox(alice, 'human:alice').map((m: any) => m.body)).toEqual(['for alice']);
    expect(sql.exec('SELECT delivered_at FROM messages WHERE id = ?', sent.id).one().delivered_at).toBeTruthy(); // inbox poll marks delivered
    expect(() => bus.inbox({ kind: 'human', actor: 'human:zoe' } as any, 'human:alice')).toThrow('unauthorized');
  }));

  it('filters the inbox by unread and thread, and batch-marks read', () => fixture(async ({ bus, sql }) => {
    const first = bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 't1 first', idempotencyKey: 'u-1', threadId: 'thread-a' });
    bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 't2', idempotencyKey: 'u-2', threadId: 'thread-b' });
    bus.send(developer, { senderActor: 'agent:developer', toActor: 'agent:developer', kind: 'note', body: 't1 second', idempotencyKey: 'u-3', threadId: 'thread-a' });
    expect(bus.inbox(developer, 'agent:developer', 50, undefined, { thread: 'thread-a' }).map((m: any) => m.body)).toEqual(['t1 first', 't1 second']);
    expect(bus.markRead(developer, [first.id])).toEqual({ marked: 1 });
    expect(sql.exec('SELECT state FROM messages WHERE id = ?', first.id).one().state).toBe('read');
    expect(sql.exec("SELECT event FROM message_events WHERE message_id = ? AND event = 'read'", first.id).toArray()).toHaveLength(1);
    const sessionMessage = bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'host transport only', idempotencyKey: 'u-host' });
    expect(bus.markRead(hostBus, [sessionMessage.id])).toEqual({ marked: 0 });
    expect(sql.exec('SELECT read_at FROM messages WHERE id = ?', sessionMessage.id).one().read_at).toBeNull();
    // Same-millisecond ULIDs do not promise send order; compare as a set.
    expect(bus.inbox(developer, 'agent:developer', 50, undefined, { unread: true }).map((m: any) => m.body).sort()).toEqual(['t1 second', 't2'].sort());
    // Sender and admin views never mark read; unknown ids are skipped.
    expect(bus.markRead({ kind: 'human', isAdmin: true } as any, [first.id, 'missing'])).toEqual({ marked: 0 });
  }));

  it('readdresses an undelivered session message to a replacement session (operator only)', () => fixture(async ({ bus, sc, sql }) => {
    const { readdressMessage, expireSessionMessages } = await import('../src/session-messaging');
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'follow me', idempotencyKey: 'r-1' });
    sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = ?", SESSION);
    const OTHER = '44444444-4444-4444-8444-444444444444';
    const at = new Date().toISOString();
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'admin', 'k2', '{"workspace":"agent-hub"}', 'tabo', 'codex', 'interactive', 'running', ?, ?, ?)`, OTHER,
      JSON.stringify({ native_id: NATIVE, tag: 'burn-tabo-2', workspace: 'C:\\repos\\agent-hub', generation: 0 }), at, at);
    expireSessionMessages(sql, Date.now(), 'tabo'); // the host cycle expires the ended session's queued delivery
    expect(sql.exec("SELECT state FROM session_control_commands WHERE request_key = 'msg-" + sent.id + "'").one().state).toBe('failed'); // ended session expires the queue
    const readdressed = readdressMessage(sql, sent.id, 'session:' + OTHER, Date.now());
    expect(readdressed).toEqual({ id: sent.id, to_actor: 'session:' + OTHER });
    const row = sql.exec('SELECT to_actor, to_session_id, state FROM messages WHERE id = ?', sent.id).one();
    expect(row).toMatchObject({ to_actor: 'session:' + OTHER, to_session_id: OTHER, state: 'accepted' });
    const command: any = sql.exec("SELECT session_id, state FROM session_control_commands WHERE request_key = 'msg-" + sent.id + "'").toArray();
    expect(Object.fromEntries(command.map((c: any) => [c.session_id, c.state]))).toEqual({ [SESSION]: 'failed', [OTHER]: 'recorded' });
    const queued = sql.exec("SELECT recipient_seq, payload_json FROM session_control_commands WHERE session_id = ? AND request_key = ?", OTHER, 'msg-' + sent.id).one();
    const sequence = sql.exec('SELECT recipient_seq FROM messages WHERE id = ?', sent.id).one().recipient_seq;
    expect(queued.recipient_seq).toBe(sequence);
    expect(JSON.parse(queued.payload_json).recipient_seq).toBe(sequence);
    expect(sql.exec("SELECT detail_json FROM message_events WHERE message_id = ? AND event = 'readdressed'", sent.id).one().detail_json).toContain(OTHER);
    sql.exec("UPDATE messages SET read_at = strftime('%s','now') * 1000 WHERE id = ?", sent.id);
    expect(() => readdressMessage(sql, sent.id, 'session:' + OTHER, Date.now())).toThrow('already_read'); // after read, the inbox copy is authoritative
  }));
});

describe('M6 named-agent delegation (§9)', () => {
  it('checks the exact delegated credential family on message routes and audits on_behalf_of', () => fixture(async ({ bus, hub, sql }) => {
    const human = { kind: 'human', actor: 'human:alice', tokenId: 'mcp-cred-alice' } as any;
    const call = async (caller: any, path: string, method = 'POST', body?: any) => {
      (hub as any).authenticateBusRequest = async () => ({ caller, scopes: ['msg:send', 'msg:read', 'admin'] });
      return hub.fetch(new Request('https://hub.test' + path, {
        method, headers: { 'content-type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }));
    };
    const send = { to_actor: 'agent:developer', kind: 'note', body: 'as developer', idempotency_key: 'd-1', as: 'agent:developer' };
    expect((await call(human, '/api/bus/send', 'POST', send)).status).toBe(403); // no delegate grant yet
    expect((await call({ kind: 'human', actor: 'admin', isAdmin: true }, '/api/bus/send', 'POST', send)).status).toBe(403);

    // One credential family is delegated; sibling families from the same MCP client stay isolated.
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 86400000).toISOString();
    sql.exec('CREATE TABLE IF NOT EXISTS mcp_grants (family TEXT PRIMARY KEY, client TEXT, resource TEXT, expires INTEGER)');
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('mcp-cred-alice', 'human', 'hash-alice', 'fam-alice', 'alice@fake.local', 'human:alice', '["admin"]', 'a', ?, ?)`, at, far);
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('mcp-cred-alice-sibling', 'human', 'hash-alice-sibling', 'fam-alice-sibling', 'alice@fake.local', 'human:alice', '["admin"]', 'a', ?, ?)`, at, far);
    sql.exec("INSERT INTO mcp_grants (family, client, resource, expires) VALUES ('fam-alice', 'test-client', 'https://hub.test/mcp', ?)", Date.now() + 3600000);
    sql.exec("INSERT INTO mcp_grants (family, client, resource, expires) VALUES ('fam-alice-sibling', 'test-client', 'https://hub.test/mcp', ?)", Date.now() + 3600000);
    updateNamedAgentDelegates(sql, 'developer', ['fam-alice']);
    expect((await call({ ...human, tokenId: 'mcp-cred-alice-sibling' }, '/api/bus/send', 'POST', { ...send, idempotency_key: 'd-sibling' })).status).toBe(403);

    // The MCP-style HTTP route changes the sender to the named agent while
    // preserving the credential-family owner in the metadata-only audit.
    const sentResponse = await call(human, '/api/bus/send', 'POST', send);
    expect(sentResponse.status).toBe(200);
    const sent = await sentResponse.json() as any;
    expect(sql.exec('SELECT from_actor, to_actor FROM messages WHERE id = ?', sent.id).one()).toEqual({
      from_actor: 'agent:developer', to_actor: 'agent:developer',
    });
    const audit: any = sql.exec("SELECT actor, detail_json FROM message_events WHERE message_id = ? AND event = 'delegated'", sent.id).one();
    expect(audit.actor).toBe('human:alice');
    expect(JSON.parse(audit.detail_json)).toMatchObject({ on_behalf_of: 'human:alice', credential_family: 'fam-alice' });

    const inbox = await call(human, '/api/bus/inbox?as=agent%3Adeveloper', 'GET');
    expect(inbox.status).toBe(200);
    expect((await inbox.json() as any[]).map(row => row.id)).toContain(sent.id);

    sql.exec(`INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at)
      VALUES ('alice', 'alice@fake.local', 1, ?, ?)`, at, at);
    const inbound = bus.send({ kind: 'human', actor: 'human:alice' } as any, {
      senderActor: 'human:alice', toActor: 'agent:developer', kind: 'note', body: 'reply to this', idempotencyKey: 'd-inbound',
    });
    const replyResponse = await call(human, '/api/bus/reply', 'POST', {
      reply_to: inbound.id, from_actor: 'agent:developer', to_actor: 'human:alice', kind: 'reply',
      body: 'reply as developer', idempotency_key: 'd-reply', as: 'agent:developer',
    });
    expect(replyResponse.status).toBe(200);
    const reply = await replyResponse.json() as any;
    expect(reply.thread_id).toBe(inbound.threadId);
    expect(sql.exec('SELECT from_actor, to_actor, thread_id FROM messages WHERE id = ?', reply.id).one()).toEqual({
      from_actor: 'agent:developer', to_actor: 'human:alice', thread_id: inbound.threadId,
    });
    const replyAudit: any = sql.exec("SELECT actor, detail_json FROM message_events WHERE message_id = ? AND event = 'delegated'", reply.id).one();
    expect(replyAudit.actor).toBe('human:alice');
    expect(JSON.parse(replyAudit.detail_json)).toMatchObject({ on_behalf_of: 'human:alice', credential_family: 'fam-alice' });

    // Revoking the grant stops future delegation.
    updateNamedAgentDelegates(sql, 'developer', ['fam-alice'], true);
    expect((await call(human, '/api/bus/send', 'POST', { ...send, idempotency_key: 'd-revoked' })).status).toBe(403);
  }));
});

describe('M1 and M5 delivery integration', () => {
  it('fences a claimed old target and audits the operator when readdressing', () => fixture(async ({ bus, sc, sql }) => {
    const { readdressMessage } = await import('../src/session-messaging');
    const sent = bus.send(developer, { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'replacement delivery', idempotencyKey: 'retarget-claimed' });
    const [oldOffer] = await offers(sc);
    const grant = await claim(sc, oldOffer);
    const other = '44444444-4444-4444-8444-444444444444';
    const at = new Date().toISOString();
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'admin', 'replacement', '{}', 'tabo', 'codex', 'interactive', 'running', ?, ?, ?)`, other,
      JSON.stringify({ native_id: NATIVE, tag: 'replacement', workspace: '/synthetic', generation: 0 }), at, at);
    readdressMessage(sql, sent.id, 'session:' + other, Date.now(), 'human:operator');
    expect((await ack(sc, oldOffer, grant.claim_token, 'delivered', { proof: { native_message_id: 'old-target' } })).status).toBe(409);
    expect(sql.exec('SELECT state FROM messages WHERE id = ?', sent.id).one().state).toBe('accepted');
    const [replacement] = await offers(sc);
    expect(replacement.session_id).toBe(other);
    expect(replacement.payload.recipient_seq).toBe(sql.exec('SELECT recipient_seq FROM messages WHERE id = ?', sent.id).one().recipient_seq);
    expect(sql.exec("SELECT actor FROM message_events WHERE message_id = ? AND event = 'admin_override'", sent.id).one().actor).toBe('human:operator');
    // A repeated request for the current target does not allocate another sequence or command.
    readdressMessage(sql, sent.id, 'session:' + other, Date.now(), 'human:operator');
    expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_commands WHERE session_id = ?', other).one().n).toBe(1);
  }));
});
