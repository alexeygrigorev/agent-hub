import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

/** M12e session repair regressions (F3/F4/F5) against real Worker/SQLite.
 *
 * Each case reproduces a confirmed review finding on the seed contract and
 * asserts the repaired behavior. No live agents, no Fleet consumer: launch
 * rows are modeled fixture setup (the legitimate consumer report state),
 * while every binding/claim/ack under test goes through the real routes.
 */
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '11111111-1111-4111-8111-111111111111';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const goodProof = (tag: string) => ({ native_message_id: 'nmsg-1', reply_to_native_id: 'nreply-1',
  reply_digest: 'a'.repeat(64), sender_native_id: NATIVE_ID, sender_tag: tag,
  sender_workspace: NATIVE_WS, sender_generation: 1, sender_created_at_ms: NATIVE_CREATED });

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

function fakeReserve() {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'claude' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-repair-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fake = fakeReserve();
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fake.hook });
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql);
  });
}

async function startOk(s: SessionControlService, extra: any = {}, auth: any = admin, key = crypto.randomUUID()) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', ...extra }), auth);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'claude', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'claude', ?, NULL, ?, ?, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function bindOk(s: SessionControlService, sql: any, sessionId: string, launchId: string, machine = 'hetzner') {
  ensureFleetLaunch(sql, launchId, machine);
  reportFleetSession(sql, machine, NATIVE_ID, 'hub-' + launchId, NATIVE_WS);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`,
    { native: { native_id: NATIVE_ID, tag: 'hub-' + launchId, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }), host(machine));
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}

async function claimAs(s: SessionControlService, commandId: string, correlation: string, machine = 'hetzner', attemptId = ATTEMPT, tokenId = 'host-cred-1') {
  const res = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation, attempt_id: attemptId }), host(machine, tokenId));
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}

describe('F3 live family and machine authorization', () => {
  it('foreign host family sees no offers and transports nothing', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-2', 'host', 'hash-host-cred-2', 'refresh-host-cred-2', 'host-family-2', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f3-foreign-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'f1', body: 'private prompt body' }), admin);
    // Foreign (newly re-enrolled) family: no payload, no state mutation.
    const foreign = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner', 'host-cred-2'))).json() as any;
    expect(foreign.offers).toEqual([]);
    const row: any = sql.exec(`SELECT state FROM session_control_commands`).toArray()[0];
    expect(row.state).toBe('recorded');
    // Current family still sees and transports exactly once.
    const own = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    expect(own.offers.length).toBe(1);
    expect(own.offers[0].payload).toEqual({ body: 'private prompt body' });
  }));

  it('superseded family cannot hijack the first binding', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-new', 'host', 'hash-host-cred-new', 'refresh-host-cred-new', 'host-family-new', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    // Machine re-enrolled: the registry now points at the new family.
    sql.exec(`UPDATE machines SET credential_id = 'host-cred-new' WHERE id = 'hetzner'`);
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f3-hijack-1');
    ensureFleetLaunch(sql, session.fleet_launch_id, 'hetzner');
    reportFleetSession(sql, 'hetzner', NATIVE_ID, 'hub-' + session.fleet_launch_id, NATIVE_WS);
    const tag = 'hub-' + session.fleet_launch_id;
    const stale = await s.handle(request(`/api/sessions/${session.id}/binding`,
      { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
      host('hetzner', 'host-cred-1'));
    expect(stale.status).toBe(409);
    expect((await stale.json() as any).error).toBe('host_credential_changed');
    // The current enrolled family binds fine.
    const fresh = await s.handle(request(`/api/sessions/${session.id}/binding`,
      { native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }),
      host('hetzner', 'host-cred-new'));
    expect(fresh.status).toBe(200);
    // The superseded family is now locked out of offers too.
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'h1', body: 'go' }), admin);
    const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner', 'host-cred-1'))).json() as any;
    expect(offers.offers).toEqual([]);
  }));

  it('deleted machine offers nothing and binds nothing', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f3-deleted-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'd1', body: 'go' }), admin);
    sql.exec(`DELETE FROM machines WHERE id = 'hetzner'`);
    const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    expect(offers.offers).toEqual([]);
    const bind = await s.handle(request(`/api/sessions/${session.id}/binding`,
      { native: { native_id: NATIVE_ID, tag: 'hub-' + session.fleet_launch_id, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }), host('hetzner'));
    expect(bind.status).toBe(404);
    const poll: any = sql.exec(`SELECT state FROM session_control_commands`).toArray()[0];
    expect(poll.state).toBe('recorded');
  }));

  it('revoked owner token fails closed while same-family refresh keeps working', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    sql.exec(`INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
    for (const [id, family] of [['cred-A', 'family-1'], ['cred-B', 'family-1'], ['cred-C', 'family-2']]) {
      sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
        VALUES (?, 'agent', ?, ?, ?, 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?)`,
        id, 'hash-' + id, 'refresh-' + id, family, at, far);
    }
    const created: any = await startOk(s, {}, agent('cred-A'), 'f3-owner-1');
    // Revocation is honored live on the very next read (no token-id shortcut).
    sql.exec(`UPDATE credentials SET revoked_at = ? WHERE id = 'cred-A'`, new Date().toISOString());
    expect((await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), agent('cred-A'))).status).toBe(404);
    // Same-family refresh (genuine authority) keeps working and adopts the id.
    expect((await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), agent('cred-B'))).status).toBe(200);
    // Delete + reregister (new family, same actor) stays a different owner.
    expect((await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), agent('cred-C'))).status).toBe(404);
  }));

  it('claim holder change of family fails the ack without a new effect', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-2', 'host', 'hash-host-cred-2', 'refresh-host-cred-2', 'host-family-2', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f3-claimfam-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'cf1', body: 'go' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const claim = await claimAs(s, poll.offers[0].command_id, poll.offers[0].correlation);
    // Re-enrollment moves the machine to a new family mid-lease.
    sql.exec(`UPDATE machines SET credential_id = 'host-cred-2' WHERE id = 'hetzner'`);
    const tag = 'hub-' + session.fleet_launch_id;
    const late = await s.handle(request(`/api/session-commands/${poll.offers[0].command_id}/ack`,
      { correlation: poll.offers[0].correlation, outcome: 'transported',
        native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS },
        claim_token: claim.claim_token }), host('hetzner'));
    expect(late.status).toBe(409);
    expect((await late.json() as any).error).toBe('host_credential_changed');
  }));
});

describe('F4 idempotent ack reconciliation', () => {
  it('identical delivered re-submit after commit succeeds without a new effect', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f4-idem-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    const tag = 'hub-' + session.fleet_launch_id;
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'i1', body: 'go' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const offer = poll.offers[0];
    const claim = await claimAs(s, offer.command_id, offer.correlation);
    const ackUrl = `/api/session-commands/${offer.command_id}/ack`;
    const body = { correlation: offer.correlation, outcome: 'delivered',
      native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS },
      claim_token: claim.claim_token, proof: goodProof(tag) };
    expect((await s.handle(request(ackUrl, body), host('hetzner'))).status).toBe(200);
    // Lost ack response: the IDENTICAL proven ack replays success.
    const replay = await s.handle(request(ackUrl, body), host('hetzner'));
    expect(replay.status).toBe(200);
    expect((await replay.json() as any).state).toBe('acked');
    // A different reply envelope for the finished command is not a new
    // outcome: the command is acked, so only identical resubmits succeed.
    const other = await s.handle(request(ackUrl,
      { ...body, proof: { ...goodProof(tag), reply_to_native_id: 'nreply-2' } }), host('hetzner'));
    expect(other.status).toBe(409);
    expect((await other.json() as any).error).toBe('claim_required');
  }));

  it('identical transported re-submit succeeds; uncertain stays terminal', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f4-trans-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    const tag = 'hub-' + session.fleet_launch_id;
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 't1', body: 'go' }), admin);
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 't2', body: 'go again' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const [first, second] = poll.offers;
    const c1 = await claimAs(s, first.command_id, first.correlation);
    const body = { correlation: first.correlation, outcome: 'transported',
      native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS }, claim_token: c1.claim_token };
    expect((await s.handle(request(`/api/session-commands/${first.command_id}/ack`, body), host('hetzner'))).status).toBe(200);
    const replay = await s.handle(request(`/api/session-commands/${first.command_id}/ack`, body), host('hetzner'));
    expect(replay.status).toBe(200);
    expect((await replay.json() as any).state).toBe('transported');
    // Uncertain is terminal for everything EXCEPT the narrow late-reply
    // path: the identical report replays idempotently, and the SAME attempt
    // with a valid fresh delivered proof still reconciles (F1 late-reply).
    // Anything else — including a proof-less new outcome — stays rejected.
    const c2 = await claimAs(s, second.command_id, second.correlation);
    const uBody = { correlation: second.correlation, outcome: 'uncertain',
      native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS }, claim_token: c2.claim_token, reason: 'timeout' };
    expect((await s.handle(request(`/api/session-commands/${second.command_id}/ack`, uBody), host('hetzner'))).status).toBe(200);
    const same = await s.handle(request(`/api/session-commands/${second.command_id}/ack`, uBody), host('hetzner'));
    expect(same.status).toBe(200);
    expect((await same.json() as any).state).toBe('uncertain');
    const late = await s.handle(request(`/api/session-commands/${second.command_id}/ack`,
      { ...uBody, outcome: 'delivered', proof: goodProof(tag) }), host('hetzner'));
    expect(late.status).toBe(200);
    expect((await late.json() as any).state).toBe('acked');
    const proofLess = await s.handle(request(`/api/session-commands/${second.command_id}/ack`,
      { ...uBody, outcome: 'transported' }), host('hetzner'));
    expect(proofLess.status).toBe(409);
  }));
});

describe('F1 late-reply reconciliation after lease expiry', () => {
  it('recipient reply after the execution lease lapses still acks without a new effect', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f1-late-1');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    const tag = 'hub-' + session.fleet_launch_id;
    await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'l1', body: 'go' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const offer = poll.offers[0];
    const claim = await claimAs(s, offer.command_id, offer.correlation);
    // The execution lease lapses with no reply yet: the command goes
    // back to the queue for reconciliation under the SAME execution fence.
    sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
      new Date(Date.now() - 1000).toISOString(), offer.command_id);
    await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'));
    const row: any = sql.exec('SELECT state, attempts, claim_token, holder_attempt FROM session_control_commands WHERE id = ?', offer.command_id).toArray()[0];
    expect(row.state).toBe('transported');
    expect(row.attempts).toBe(2);
    expect(row.claim_token).toBe(claim.claim_token);
    expect(row.holder_attempt).toBe(ATTEMPT);
    // Minutes later the recipient reply arrives: the SAME attempt (claim
    // token + correlation) with a valid fresh proof reconciles to acked.
    // No new effect exists anywhere on this path (ack is record-only).
    const ackUrl = `/api/session-commands/${offer.command_id}/ack`;
    const late = await s.handle(request(ackUrl, { correlation: offer.correlation, outcome: 'delivered',
      native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS },
      claim_token: claim.claim_token, proof: goodProof(tag) }), host('hetzner'));
    expect(late.status, JSON.stringify(await late.json())).toBe(200);
    const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
    expect(list.commands[0].state).toBe('acked');
    expect(list.commands[0].ack.proof.reply_to_native_id).toBe('nreply-1');
    // Claiming is NOT reopened by the late reply.
    const reclaim = await s.handle(request(`/api/session-commands/${offer.command_id}/claim`,
      { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'));
    expect(reclaim.status).toBe(409);
  }));

  it('proof-less outcomes need renewed custody after expiry; foreign token cannot late-ack', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f1-late-2');
    await bindOk(s, sql, session.id, session.fleet_launch_id);
    const tag = 'hub-' + session.fleet_launch_id;
    await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'c1' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const offer = poll.offers[0];
    const claim = await claimAs(s, offer.command_id, offer.correlation);
    sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
      new Date(Date.now() - 1000).toISOString(), offer.command_id);
    await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'));
    const ackUrl = `/api/session-commands/${offer.command_id}/ack`;
    // Capture has no reply proof: it needs renewed custody, not a late send proof.
    const late = await s.handle(request(ackUrl, { correlation: offer.correlation, outcome: 'delivered',
      native: { native_id: NATIVE_ID, tag, workspace: NATIVE_WS },
      claim_token: claim.claim_token, capture: 'tail' }), host('hetzner'));
    expect(late.status).toBe(409);
    expect((await late.json() as any).error).toBe('command_uncertain');
  }));
});

describe('F5 canonical fleet launch reconciliation', () => {
  it('rejects unstarted, terminal, missing, and inconsistent launches', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f5-matrix-1');
    const lid = session.fleet_launch_id as string;
    const tag = 'hub-' + lid;
    const attempt = (native: any, machine = 'hetzner') => s.handle(
      request(`/api/sessions/${session.id}/binding`, { native }), host(machine));
    const shape = (over: any = {}) => ({ native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED, ...over });
    // Missing launch row.
    expect((await (await attempt(shape())).json() as any).error).toBe('missing_fleet_launch');
    // Unstarted (offered) launch with an arbitrary tag.
    ensureFleetLaunch(sql, lid, 'hetzner', null, 'offered');
    const offeredBad = await attempt({ native_id: NATIVE_ID, tag: 'totally-unrelated-tag',
      workspace: '/tmp/unrelated-foreign-workspace', native_created_at_ms: NATIVE_CREATED });
    expect(offeredBad.status).toBe(409);
    expect((await offeredBad.json() as any).error).toBe('fleet_launch_state');
    // Terminal failures.
    for (const state of ['failed', 'exited', 'uncertain']) {
      ensureFleetLaunch(sql, lid, 'hetzner', null, state);
      const res = await attempt(shape());
      expect(res.status, state).toBe(409);
      expect((await res.json() as any).error).toBe('fleet_launch_state');
    }
    // Foreign machine.
    ensureFleetLaunch(sql, lid, 'win35', null, 'running');
    const foreign = await attempt(shape());
    expect(foreign.status).toBe(409);
    expect((await foreign.json() as any).error).toBe('fleet_machine_mismatch');
    // Wrong tag on a live launch.
    ensureFleetLaunch(sql, lid, 'hetzner', NATIVE_ID, 'running');
    const badTag = await attempt(shape({ tag: 'totally-unrelated-tag' }));
    expect(badTag.status).toBe(409);
    expect((await badTag.json() as any).error).toBe('fleet_tag_mismatch');
    // Launch-reported native inconsistency.
    reportFleetSession(sql, 'hetzner', '22222222-2222-4222-8222-222222222222', tag, NATIVE_WS);
    const badNative = await attempt(shape({ native_id: '22222222-2222-4222-8222-222222222222' }));
    expect(badNative.status).toBe(409);
    expect((await badNative.json() as any).error).toBe('fleet_native_mismatch');
    // Unreported UUID: the launch row alone never proves the native identity.
    ensureFleetLaunch(sql, lid, 'hetzner', null, 'running');
    const unreported = await attempt(shape({ native_id: '44444444-4444-4444-8444-444444444444' }));
    expect(unreported.status).toBe(409);
    expect((await unreported.json() as any).error).toBe('fleet_native_unreported');
    // Sessions-reported tag/workspace inconsistency.
    reportFleetSession(sql, 'hetzner', NATIVE_ID, 'other-tag', NATIVE_WS);
    const badReportedTag = await attempt(shape());
    expect(badReportedTag.status).toBe(409);
    expect((await badReportedTag.json() as any).error).toBe('fleet_native_mismatch');
    reportFleetSession(sql, 'hetzner', NATIVE_ID, tag, '/tmp/elsewhere');
    const badReportedWs = await attempt(shape());
    expect(badReportedWs.status).toBe(409);
    expect((await badReportedWs.json() as any).error).toBe('fleet_native_mismatch');
    // Terminal native phase in the sessions report.
    reportFleetSession(sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS, 'exited');
    const terminalNative = await attempt(shape());
    expect(terminalNative.status).toBe(409);
    expect((await terminalNative.json() as any).error).toBe('fleet_native_terminal');
    // Live launch, canonical tag, reported native agrees.
    ensureFleetLaunch(sql, lid, 'hetzner', NATIVE_ID, 'running');
    reportFleetSession(sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
    expect((await attempt(shape())).status).toBe(200);
  }));

  it('accepts starting and unreported-native live launches with the canonical tag', async () => fixture(async (s, sql) => {
    const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'f5-live-1');
    const lid = session.fleet_launch_id as string;
    const tag = 'hub-' + lid;
    const shape = { native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED };
    ensureFleetLaunch(sql, lid, 'hetzner', null, 'starting');
    reportFleetSession(sql, 'hetzner', NATIVE_ID, tag, NATIVE_WS);
    expect((await s.handle(request(`/api/sessions/${session.id}/binding`, { native: shape }), host('hetzner'))).status).toBe(200);
    const session2: any = await startOk(s, { machine: 'hetzner' }, admin, 'f5-live-2');
    const lid2 = session2.fleet_launch_id as string;
    ensureFleetLaunch(sql, lid2, 'hetzner', null, 'running');
    reportFleetSession(sql, 'hetzner', '33333333-3333-4333-8333-333333333333', 'hub-' + lid2, NATIVE_WS);
    expect((await s.handle(request(`/api/sessions/${session2.id}/binding`,
      { native: { ...shape, native_id: '33333333-3333-4333-8333-333333333333', tag: 'hub-' + lid2 } }), host('hetzner'))).status).toBe(200);
  }));
});
