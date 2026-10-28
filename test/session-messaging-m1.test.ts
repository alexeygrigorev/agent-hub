import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { BusService, parseAddress } from '../src/bus';
import { resolveRecipient, afterMessageInsert, renderTui, checkSendScoping, expireSessionMessages, revokeSessionCredentials, setMessageState, messageEvent } from '../src/session-messaging';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
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
      onStage: (id, stage, actor, machine, now) => setMessageState(sql, id, stage, null, actor, machine, now),
      afterInsert: (m, o, now) => afterMessageInsert(sql, m, { delivery: o.delivery, deliverByMs: o.deliverByMs, ref: o.ref, now }),
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

const send = (bus: BusService, key = crypto.randomUUID(), extra: any = {}) => bus.send(developer,
  { senderActor: 'agent:developer', toActor: 'session:' + SESSION, kind: 'note', body: 'M1 fixture', idempotencyKey: key, ...extra });

describe('M1 ordering, states and deadlines', () => {
  it('parses explicit address kinds and refuses ambiguous tag@machine with candidates', () => fixture(async ({ bus, sql }) => {
    expect(parseAddress('session:' + SESSION)).toEqual({ kind: 'session', sessionId: SESSION });
    expect(parseAddress('session:wrong')).toBeNull();
    expect(parseAddress('agent:developer')).toEqual({ kind: 'agent', name: 'developer' });
    expect(parseAddress('human:me')).toEqual({ kind: 'human', sub: 'me' });
    expect(parseAddress('human:admin-token:fixture')).toEqual({ kind: 'human', sub: 'admin-token:fixture' });
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      SELECT '44444444-4444-4444-8444-444444444444', actor, 'duplicate', request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at FROM session_control_sessions WHERE id = ?`, SESSION);
    try { send(bus, 'ambiguous', { toActor: 'burn-tabo@Tabo' }); throw new Error('expected refusal'); }
    catch (e: any) {
      expect(e.message).toBe('ambiguous_recipient');
      expect(e.candidates).toEqual(expect.arrayContaining(['session:' + SESSION, 'session:44444444-4444-4444-8444-444444444444']));
    }
    sql.exec("UPDATE named_agents SET disabled_at = '2026-01-01' WHERE name = 'developer'");
    expect(() => send(bus, 'disabled', { toActor: 'agent:developer' })).toThrow('unknown_recipient');
  }));

  it('validates refs in UTF-8 bytes, deadlines and idempotent delivery options', () => fixture(async ({ bus, sql }) => {
    const ref = { kind: 'path', value: '/evidence/result.txt', machine: 'tabo', bytes: 1024 };
    const sent = send(bus, 'ref', { ref, delivery: 'inbox', deliverByMs: 7 * 86400000 });
    expect(JSON.parse(sql.exec('SELECT ref_json FROM messages WHERE id = ?', sent.id).one().ref_json)).toEqual(ref);
    expect(send(bus, 'ref', { ref, delivery: 'inbox', deliverByMs: 7 * 86400000 }).id).toBe(sent.id);
    expect(() => send(bus, 'ref', { ref: { kind: 'url', value: 'https://example.test' } })).toThrow('conflict');
    expect(() => send(bus, 'ref', { ref, delivery: 'tui' })).toThrow('conflict');
    for (const invalid of [{ kind: 'blob', value: 'x' }, { kind: 'path', value: '/tmp' }, { kind: 'url', value: 'é'.repeat(1020) }, { kind: 'git', value: 'x', bytes: -1 }]) {
      expect(() => send(bus, crypto.randomUUID(), { ref: invalid })).toThrow('invalid_ref');
    }
    expect(() => send(bus, 'deadline', { deliverByMs: 7 * 86400000 + 1 })).toThrow('invalid_deliver_by');
    expect(() => send(bus, 'body', { body: 'é'.repeat(8193) })).toThrow('body_too_large');
  }));

  it('aligns session-control send to exactly 16384 UTF-8 bytes', () => fixture(async ({ sc }) => {
    const auth = { caller: { actor: 'admin', isAdmin: true }, scopes: ['admin'] } as any;
    expect((await sc.handle(req(`/api/sessions/${SESSION}/send`, { key: 'size-ok', body: 'é'.repeat(8192), require_ready: false }), auth)).status).toBe(201);
    expect((await sc.handle(req(`/api/sessions/${SESSION}/send`, { key: 'size-bad', body: 'é'.repeat(8193), require_ready: false }), auth)).status).toBe(400);
  }));
  it('serializes by sequence even when command creation time contradicts send order', () => fixture(async ({ bus, sc, sql }) => {
    const first = send(bus);
    const second = send(bus);
    sql.exec("UPDATE session_control_commands SET created_at = '2000-01-01' WHERE recipient_seq = 2");
    expect((await offers(sc)).map((o: any) => o.payload.message_id)).toEqual([first.id]);
    const row = sql.exec('SELECT * FROM session_control_commands WHERE recipient_seq = 2').one();
    const early = await sc.handle(req(`/api/session-commands/${row.id}/claim`, { correlation: row.correlation, attempt_id: ATTEMPT }), host);
    expect(early.status).toBe(409);
    expect((await early.json() as any).error).toBe('earlier_message_pending');
    expect(sql.exec('SELECT recipient_seq FROM messages WHERE id = ?', second.id).one().recipient_seq).toBe(2);
  }));

  it('backs off 5/15/30/60 seconds without consuming attempts and refuses early direct claims', () => fixture(async ({ bus, sc, sql }) => {
    const sent = send(bus);
    for (const delay of [5000, 15000, 30000, 60000, 60000]) {
      const [offer] = await offers(sc);
      const granted = await claim(sc, offer);
      const result = await ack(sc, offer, granted.claim_token, 'not_ready');
      expect(await result.json()).toMatchObject({ state: 'recorded', not_before_ms: delay });
      expect(sql.exec('SELECT attempts FROM session_control_commands WHERE id = ?', offer.command_id).one().attempts).toBe(0);
      const early = await sc.handle(req(`/api/session-commands/${offer.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT }), host);
      expect(early.status).toBe(409);
      expect(await offers(sc)).toEqual([]);
      sql.exec("UPDATE session_control_commands SET not_before = NULL WHERE kind = 'message'");
    }
    const [offer] = await offers(sc);
    const granted = await claim(sc, offer);
    expect((await ack(sc, offer, granted.claim_token, 'delivered')).status).toBe(200);
    expect(bus.status(developer, sent.id).state).toBe('delivered');
  }));

  it('retries nonterminal refusal, but session_ended creates only one status per thread', () => fixture(async ({ bus, sc, sql }) => {
    const sent = send(bus);
    send(bus, crypto.randomUUID(), { threadId: sent.threadId });
    let [offer] = await offers(sc);
    let granted = await claim(sc, offer);
    expect(await (await ack(sc, offer, granted.claim_token, 'refused', { reason: 'binding_pending' })).json()).toMatchObject({ state: 'recorded' });
    expect(bus.status(developer, sent.id).events.some((e: any) => e.event === 'delivery_refused')).toBe(true);
    sql.exec("UPDATE session_control_commands SET not_before = NULL WHERE kind = 'message'");
    [offer] = await offers(sc);
    granted = await claim(sc, offer);
    await ack(sc, offer, granted.claim_token, 'refused', { reason: 'session_ended' });
    sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = ?", SESSION);
    revokeSessionCredentials(sql, SESSION, Date.now());
    expireSessionMessages(sql, Date.now());
    const statuses = sql.exec("SELECT * FROM messages WHERE from_actor = 'system:hub'").toArray();
    expect(statuses).toHaveLength(1);
    expect(statuses[0]).toMatchObject({ to_actor: 'agent:developer', thread_id: sent.threadId, kind: 'status' });
    expect(sql.exec("SELECT state_reason FROM messages WHERE to_session_id = ?", SESSION).toArray().map((r: any) => r.state_reason)).toEqual(['session_ended', 'session_ended']);
  }));

  it('never marks a session delivered on host polling; expired claims become uncertain without retry exhaustion', () => fixture(async ({ bus, sc, sql }) => {
    const sent = send(bus);
    expect(bus.inbox(hostBus, 'session:' + SESSION)[0].delivered_at).toBeNull();
    expect(bus.show(hostBus, sent.id).read_at).toBeNull();
    send(bus);
    const [offer] = await offers(sc);
    await claim(sc, offer);
    sql.exec("UPDATE session_control_commands SET attempts = 99, claim_expires_at = '2000-01-01' WHERE id = ?", offer.command_id);
    expect(await offers(sc)).toEqual([]);
    expect(bus.status(developer, sent.id).state).toBe('uncertain');
    expect(sql.exec('SELECT state FROM session_control_commands WHERE id = ?', offer.command_id).one().state).toBe('uncertain');
    sql.exec('UPDATE messages SET deliver_by = ? WHERE id = ?', Date.now() - 1, sent.id);
    expireSessionMessages(sql, Date.now()); // works without a host poll
    expect(bus.status(developer, sent.id)).toMatchObject({ state: 'expired', state_reason: 'deliver_by_passed' });
    expect(await offers(sc)).toHaveLength(1); // next message unblocked
  }));

  it('expires transported and claimed messages offline, with metadata-only audit', () => fixture(async ({ bus, sc, sql }) => {
    const sent = send(bus, 'offline', { body: 'PRIVATE BODY MUST NOT ENTER AUDIT' });
    await offers(sc);
    sql.exec('UPDATE messages SET deliver_by = ? WHERE id = ?', Date.now() - 1, sent.id);
    expireSessionMessages(sql, Date.now());
    const status = bus.status(developer, sent.id);
    expect(status.state).toBe('expired');
    expect(JSON.stringify(status.events)).not.toContain('PRIVATE BODY');
    expect(sql.exec('SELECT state FROM session_control_commands').one().state).toBe('failed');
  }));

  it('cursors use sequence and recipient stages audit forward-only transitions', () => fixture(async ({ bus, sql }) => {
    const one = send(bus, 'a', { toActor: 'agent:developer' });
    const two = send(bus, 'b', { toActor: 'agent:developer' });
    expect(bus.inbox(developer, 'agent:developer', 1).map((r: any) => r.id)).toEqual([one.id]);
    expect(bus.inbox(developer, 'agent:developer', 10, '1').map((r: any) => r.id)).toEqual([two.id]);
    bus.show(developer, one.id);
    bus.ack(developer, one.id);
    bus.accept(developer, one.id);
    bus.complete(developer, one.id);
    const status = bus.status(developer, one.id);
    expect(status.state).toBe('completed');
    expect(status.events.map((r: any) => r.event)).toEqual(['accepted', 'delivered', 'read', 'acked', 'accepted_work', 'completed']);
    setMessageState(sql, one.id, 'dispatched', null, null, null, Date.now());
    expect(bus.status(developer, one.id).state).toBe('completed');
  }));

  it('rolls back message, cursor, event and delivery command together on send failure', () => fixture(async ({ bus, sql }) => {
    const original = bus.hooks!.afterInsert;
    bus.hooks!.afterInsert = (...args) => { original(...args); throw new Error('injected failure'); };
    expect(() => send(bus, 'rollback')).toThrow('injected failure');
    for (const table of ['messages', 'recipient_cursors', 'message_events', 'session_control_commands']) {
      expect(sql.exec(`SELECT COUNT(*) AS n FROM ${table}`).one().n).toBe(0);
    }
  }));

  it('bounds escaped metadata without storing invalid JSON', () => fixture(async ({ bus, sql }) => {
    const sent = send(bus, 'escaped-metadata');
    messageEvent(sql, sent.id, 'delivery_refused', null, 'tabo', Date.now(), { reason: '\u0000'.repeat(512) });
    const status = bus.status(developer, sent.id);
    expect(status.events.at(-1).detail).toEqual({ truncated: true });
    const json = sql.exec('SELECT detail_json FROM message_events ORDER BY id DESC LIMIT 1').one().detail_json;
    expect(new TextEncoder().encode(json).length).toBeLessThanOrEqual(2048);
    sql.exec("INSERT INTO message_events (message_id,event,at,detail_json) VALUES (?, 'legacy_detail', ?, ?)", sent.id, Date.now(), '{"broken');
    expect(bus.status(developer, sent.id).events.at(-1).detail).toEqual({ invalid_json: true });
  }));

  it('retains authorized status metadata for 180 days after body deletion', () => fixture(async ({ bus, sql }) => {
    const sent = send(bus, 'retained');
    sql.exec('DELETE FROM messages WHERE id = ?', sent.id);
    expect(bus.status(developer, sent.id)).toMatchObject({ body_expired: true, from_actor: 'agent:developer', to_actor: 'session:' + SESSION });
    expect(() => bus.status({ actor: 'outsider', kind: 'agent' }, sent.id)).toThrow('unauthorized');
    sql.exec('UPDATE message_events SET at = ?', Date.now() - 181 * 86400000);
    expect(() => bus.status(developer, sent.id)).toThrow('not_found');
  }));

  it('records the source session and reconciles uncertainty only on the original host fence', () => fixture(async ({ bus, sc, sql }) => {
    const sent = send(bus);
    const [offer] = await offers(sc);
    const granted = await claim(sc, offer);
    await ack(sc, offer, granted.claim_token, 'uncertain');
    expect(bus.status(developer, sent.id).state).toBe('uncertain');
    expect((await ack(sc, offer, 'wrong-token', 'delivered')).status).toBe(409);
    expect((await ack(sc, offer, granted.claim_token, 'delivered', { proof: { native_message_id: 'marker-confirmed' } })).status).toBe(200);
    const reply = bus.send(hostBus, { senderActor: 'session:' + SESSION, toActor: 'agent:developer', kind: 'reply', body: 'reply', replyTo: sent.id, idempotencyKey: 'source-session' });
    expect(sql.exec('SELECT from_session_id, delivery FROM messages WHERE id = ?', reply.id).one()).toEqual({ from_session_id: SESSION, delivery: 'inbox' });
    expect(bus.status(developer, sent.id).events.some((e: any) => e.event === 'command_refused')).toBe(true);
  }));
});

describe('M1 HTTP sender and human addressing', () => {
  it('derives a human sender, resolves human:me, refuses spoofing and audits a legacy admin override', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('m1-http-' + crypto.randomUUID()));
    const token = 'test-only-m1-human-token';
    await runInDurableObject(stub, async (hub: Hub) => {
      const at = new Date().toISOString();
      hub.sql.exec("INSERT INTO users (sub,email,is_admin,first_seen_at,last_seen_at) VALUES ('m1-human','admin@fake.local',1,?,?)", at, at);
      hub.sql.exec(`INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
        VALUES ('m1-human','human',?,'m1-human','admin@fake.local','human:m1-human','["admin"]','m1-human',?,?)`, await sha256(token), at, new Date(Date.now() + 86400000).toISOString());
      hub.sql.exec("INSERT INTO agents (tag,project,registered_at) VALUES ('helper','m1',?)", at);
    });
    const call = (path: string, data?: any) => stub.fetch(new Request('https://hub.test' + path, { method: data ? 'POST' : 'GET', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: data ? JSON.stringify(data) : undefined }));
    const body = { to_actor: 'human:me', kind: 'note', body: 'body remains private', idempotency_key: 'm1-human', ref: { kind: 'git', value: 'abc123' } };
    const sent = await call('/api/bus/send', body);
    expect(sent.status).toBe(200);
    for (let i = 0; i < 5; i++) {
      const anonymous = await stub.fetch(new Request('https://hub.test/api/bus/inbox?actor=anything'));
      expect(anonymous.status).toBe(401);
    }
    await runInDurableObject(stub, async (hub: Hub) => {
      expect(hub.sql.exec("SELECT COUNT(*) AS n FROM message_events WHERE event='auth_refused' AND actor IS NULL").one().n).toBe(0);
    });

    const id = (await sent.json() as any).id;
    const status = await (await call('/api/bus/messages/' + id + '/status')).json() as any;
    expect(status).toMatchObject({ from_actor: 'human:m1-human', to_actor: 'human:m1-human', recipient_seq: 1 });
    expect(status.body).toBeUndefined();
    expect((await call('/api/bus/send', { ...body, from_actor: 'agent:developer', idempotency_key: 'spoof' })).status).toBe(403);
    expect((await call('/api/bus/send', { ...body, ref: { kind: 'path', value: '/missing-machine' }, idempotency_key: 'bad-ref' })).status).toBe(400);
    expect((await call('/api/bus/send', { ...body, to_actor: 'm1:helper', from_actor: 'm1:helper', idempotency_key: 'override' })).status).toBe(200);
    expect((await call('/api/bus/inbox?actor=human:me')).status).toBe(200);
    expect((await call('/api/bus/send', { ...body, body: '\u0000'.repeat(16384), idempotency_key: 'full-body' })).status).toBe(200);
    expect((await call('/api/bus/send', { ...body, body: 'é'.repeat(8193), idempotency_key: 'oversize' })).status).toBe(413);
    const cookie = (await createSessionCookie(env as any, { sub: 'm1-human', email: 'admin@fake.local', isAdmin: true, csrfToken: 'm1-cookie', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
    const cookieSend = await stub.fetch(new Request('https://hub.test/api/bus/send', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': 'm1-cookie', 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, idempotency_key: 'cookie-human' }) }));
    expect(cookieSend.status).toBe(200);
    await runInDurableObject(stub, async (hub: Hub) => {
      const rows = hub.sql.exec("SELECT event,detail_json FROM message_events WHERE event IN ('send_refused','admin_override') ORDER BY id").toArray();
      expect(rows.map((r: any) => r.event)).toEqual(['send_refused', 'admin_override']);
      expect(JSON.stringify(rows)).not.toContain('body remains private');
    });
  });
});

