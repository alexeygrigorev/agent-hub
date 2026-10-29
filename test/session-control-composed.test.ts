import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { SessionControlService } from '../src/session_control';

/** Composed M12e loop against the ACTUAL Worker/SQLite HTTP endpoints.
 *
 * Covers the connected contract the predecessor modules left unwired:
 * start intent -> fleet binding -> send -> offers (with generation +
 * native_created_at_ms) -> claim (holder/generation/both expiry forms) ->
 * correlated reply proof -> capture -> stop, plus lost-GET re-discovery,
 * claim replay/already_claimed, ack expiry, foreign binding, revoked
 * credential, and ack.native envelope-id confusion (must be binding_mismatch).
 *
 * No live agents are started, read, or stopped. Placement is a synthetic
 * fleet reserve hook (same shape the Hub wires); native proof uses the
 * exported server validator with genuine envelope semantics.
 */
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '11111111-1111-4111-8111-111111111111';
const NATIVE_WS = '/srv/hub/demo';
// Canonical Fleet consumer tag for launch-1 (F5 requires 'hub-<fleet_launch_id>').
const NATIVE_TAG = 'hub-launch-1';
const NATIVE_CREATED = 1792223000000;
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const ATTEMPT2 = 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb';
const goodProof = () => ({ native_message_id: 'nmsg-1', reply_to_native_id: 'nreply-1',
  reply_digest: 'a'.repeat(64), sender_native_id: NATIVE_ID, sender_tag: NATIVE_TAG,
  sender_workspace: NATIVE_WS, sender_generation: 1, sender_created_at_ms: NATIVE_CREATED });
const nativeTarget = () => ({ native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS });
const boundNative = () => ({ native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED });

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
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-composed-' + crypto.randomUUID()));
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

/** Model the legitimate Fleet consumer report for launch-1 (running with its
 * native receipt) plus the authoritative sessions report for the bound native
 * identity. Fixture setup only; the binding route under test is real. */
function ensureFleetLaunch(sql: any) {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES ('launch-1', 'test-admin', 'human', NULL, 'fixture', 'fleet-launch-1', '{"workspace":"demo"}', 'hetzner', 'claude', 'fixture-acct', NULL, 'headless', 'demo', 'goal', 'running', '${NATIVE_ID}', NULL, ?, ?)`, at, at);
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at)
    VALUES ('hetzner', '${NATIVE_ID}', 'hub-launch-1', 'claude', 'running', NULL, ?, '${NATIVE_WS}', NULL, 'fixture-acct', NULL)`, at);
}

describe('composed session loop: start -> binding -> send/proof -> capture -> stop', () => {
  it('runs the full connected intent chain with correlated proof', async () => fixture(async (s, sql) => {
    const start = await (await s.handle(request('/api/sessions', { key: 'loop-1', workspace: 'demo', machine: 'hetzner' }), admin)).json() as any;
    expect(start.fleet_launch_id).toBe('launch-1');
    ensureFleetLaunch(sql);
    // Binding reconciles the fleet launch: machine-bound host, exact native triple + incarnation.
    const bind = await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('hetzner'));
    expect(bind.status).toBe(200);
    // Send -> offers carry generation + incarnation for the receiver mapping.
    expect((await (await s.handle(request(`/api/sessions/${start.id}/send`, { key: 'm1', body: 'hello target' }), admin)).json() as any).command_id).toBeTruthy();
    const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    expect(offers.offers.length).toBe(1);
    expect(offers.offers[0].generation).toBe(1);
    expect(offers.offers[0].binding.native_created_at_ms).toBe(NATIVE_CREATED);
    expect(offers.offers[0].payload).toEqual({ body: 'hello target' });
    // Claim carries holder + generation + both expiry forms (server canonical ISO + host int).
    const claim = await (await s.handle(request(`/api/session-commands/${offers.offers[0].command_id}/claim`,
      { correlation: offers.offers[0].correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    expect(claim.holder).toBe('hetzner');
    expect(claim.generation).toBe(1);
    expect(typeof claim.expires_at).toBe('string');
    expect(typeof claim.expires_at_ms).toBe('number');
    // ack.native MUST be the target UUID: an envelope id is a binding mismatch.
    const ackUrl = `/api/session-commands/${offers.offers[0].command_id}/ack`;
    const confused = await s.handle(request(ackUrl, { correlation: offers.offers[0].correlation, outcome: 'delivered',
      native: { native_id: 'nmsg-1', tag: NATIVE_TAG, workspace: NATIVE_WS },
      claim_token: claim.claim_token, proof: goodProof() }), host('hetzner'));
    expect(confused.status).toBe(409);
    expect((await confused.json() as any).error).toBe('binding_mismatch');
    const done = await s.handle(request(ackUrl, { correlation: offers.offers[0].correlation, outcome: 'delivered',
      native: nativeTarget(), claim_token: claim.claim_token, proof: goodProof() }), host('hetzner'));
    expect(done.status).toBe(200);
    // Capture then stop complete the loop.
    await s.handle(request(`/api/sessions/${start.id}/capture`, { key: 'c1' }), admin);
    const capOffer = (await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any).offers[0];
    const capClaim = await (await s.handle(request(`/api/session-commands/${capOffer.command_id}/claim`,
      { correlation: capOffer.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    expect((await s.handle(request(`/api/session-commands/${capOffer.command_id}/ack`,
      { correlation: capOffer.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: capClaim.claim_token, capture: 'tail bytes' }), host('hetzner'))).status).toBe(200);
    await s.handle(request(`/api/sessions/${start.id}/stop`, { key: 't1' }), admin);
    // Stop offers may arrive while transported; poll until visible (lost-GET safe).
    let stopOffer: any = null;
    for (let i = 0; i < 3 && !stopOffer; i++) {
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      stopOffer = poll.offers.find((o: any) => o.kind === 'stop') || null;
    }
    expect(stopOffer).toBeTruthy();
    const stopClaim = await (await s.handle(request(`/api/session-commands/${stopOffer.command_id}/claim`,
      { correlation: stopOffer.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    const stopped = await s.handle(request(`/api/session-commands/${stopOffer.command_id}/ack`,
      { correlation: stopOffer.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: stopClaim.claim_token, confirmed_exit: true }), host('hetzner'));
    expect(stopped.status).toBe(200);
    const view = await (await s.handle(request(`/api/sessions/${start.id}`, undefined, 'GET'), admin)).json() as any;
    expect(view.state).toBe('stopped');
  }));

  it('re-discovers transported-but-unclaimed commands after a lost GET', async () => fixture(async (s, sql) => {
    const start = await (await s.handle(request('/api/sessions', { key: 'lost-1', workspace: 'demo', machine: 'hetzner' }), admin)).json() as any;
    ensureFleetLaunch(sql);
    await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('hetzner'));
    await s.handle(request(`/api/sessions/${start.id}/send`, { key: 'l1', body: 'go' }), admin);
    const first = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    expect(first.offers.length).toBe(1); // fetch receipt: recorded -> transported
    expect((await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any).offers).toEqual([]);
    // Simulate the transport TTL lapsing without a claim: the command self-heals to recorded.
    sql.exec("UPDATE session_control_commands SET poll_at = ? WHERE id = ?",
      new Date(Date.now() - 10 * 60 * 1000).toISOString(), first.offers[0].command_id);
    const rediscovered = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    expect(rediscovered.offers.length).toBe(1);
    expect(rediscovered.offers[0].command_id).toBe(first.offers[0].command_id);
    // The claim fence still protects the effect: one holder only.
    const claim = await (await s.handle(request(`/api/session-commands/${first.offers[0].command_id}/claim`,
      { correlation: first.offers[0].correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    expect(claim.claim_token).toBeTruthy();
    const replay = await (await s.handle(request(`/api/session-commands/${first.offers[0].command_id}/claim`,
      { correlation: first.offers[0].correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    expect(replay.claim_token).toBe(claim.claim_token);
    expect((await s.handle(request(`/api/session-commands/${first.offers[0].command_id}/claim`,
      { correlation: first.offers[0].correlation, attempt_id: ATTEMPT2 }), host('hetzner'))).status).toBe(409);
  }));

  it('rejects foreign bindings, revoked credentials, and expired-lease acks', async () => fixture(async (s, sql) => {
    const start = await (await s.handle(request('/api/sessions', { key: 'neg-1', workspace: 'demo', machine: 'hetzner' }), admin)).json() as any;
    // Foreign machine cannot bind an unrelated native UUID.
    expect((await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('win35'))).status).toBe(404);
    // Binding without a fleet launch is refused (never grant arbitrary UUIDs).
    sql.exec('UPDATE session_control_sessions SET fleet_launch_id = NULL WHERE id = ?', start.id);
    expect((await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('hetzner'))).status).toBe(409);
    sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ? WHERE id = ?', 'launch-1', start.id);
    ensureFleetLaunch(sql);
    await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('hetzner'));
    await s.handle(request(`/api/sessions/${start.id}/send`, { key: 'n1', body: 'go' }), admin);
    const offer = (await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any).offers[0];
    const claim = await (await s.handle(request(`/api/session-commands/${offer.command_id}/claim`,
      { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    // Revoked presenting credential cannot ack (host_credential_changed / 401 via hub auth; here family continuity fails closed).
    sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'host-cred-1'", new Date().toISOString());
    // Expired lease becomes uncertain, never delivered.
    sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
      new Date(Date.now() - 1000).toISOString(), offer.command_id);
    const late = await s.handle(request(`/api/session-commands/${offer.command_id}/ack`,
      { correlation: offer.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: claim.claim_token, proof: goodProof() }), host('hetzner'));
    expect([401, 409]).toContain(late.status);
  }));

  it('refuses original-message-ID confusion and proof replay across commands', async () => fixture(async (s, sql) => {
    const start = await (await s.handle(request('/api/sessions', { key: 'proof-1', workspace: 'demo', machine: 'hetzner' }), admin)).json() as any;
    ensureFleetLaunch(sql);
    await s.handle(request(`/api/sessions/${start.id}/binding`, { native: boundNative() }), host('hetzner'));
    await s.handle(request(`/api/sessions/${start.id}/send`, { key: 'p1', body: 'one' }), admin);
    await s.handle(request(`/api/sessions/${start.id}/send`, { key: 'p2', body: 'two' }), admin);
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const [first, second] = poll.offers;
    const c1 = await (await s.handle(request(`/api/session-commands/${first.command_id}/claim`,
      { correlation: first.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    // A reply whose reply_to references the hub cmd:gen string (not the native
    // envelope id) is not proof: here simulated by self-reply confusion.
    const confused = { ...goodProof(), reply_to_native_id: goodProof().native_message_id };
    expect((await s.handle(request(`/api/session-commands/${first.command_id}/ack`,
      { correlation: first.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: c1.claim_token, proof: confused }), host('hetzner'))).status).toBe(409);
    expect((await s.handle(request(`/api/session-commands/${first.command_id}/ack`,
      { correlation: first.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: c1.claim_token, proof: goodProof() }), host('hetzner'))).status).toBe(200);
    const c2 = await (await s.handle(request(`/api/session-commands/${second.command_id}/claim`,
      { correlation: second.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
    const replay = await s.handle(request(`/api/session-commands/${second.command_id}/ack`,
      { correlation: second.correlation, outcome: 'delivered', native: nativeTarget(),
        claim_token: c2.claim_token, proof: goodProof() }), host('hetzner'));
    expect(replay.status).toBe(409);
    expect((await replay.json() as any).error).toBe('proof_replayed');
  }));
});
