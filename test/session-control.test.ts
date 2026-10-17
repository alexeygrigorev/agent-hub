import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, validateSendProof } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const stranger = { caller: { kind: 'agent', actor: 'other:assistant', tokenId: 'cred-2' }, scopes: ['fleet:launch', 'bus:send'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });
const unauth = { error: Response.json({ error: 'unauthorized' }, { status: 401 }) } as any;

const NATIVE_ID = '11111111-1111-4111-8111-111111111111';
const NATIVE_WS = '/srv/hub/demo';
// Canonical Fleet consumer tag for a launch ('hub-<fleet_launch_id>', the
// exact argv the existing Dispatcher starts): F5 binding reconciliation
// requires it, so fixtures use it instead of an arbitrary label.
const NATIVE_TAG = 'hub-launch-1';
const NATIVE_CREATED = 1792223000000;
const native = (tag = NATIVE_TAG) => ({ native_id: NATIVE_ID, tag, workspace: NATIVE_WS });
const bound = (over: any = {}) => ({ native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED, ...over });
const canonTag = (launchId: string) => 'hub-' + launchId;
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';
const ATTEMPT2 = 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb';
const goodProof = () => ({ native_message_id: 'nmsg-1', reply_to_native_id: 'nreply-1',
  reply_digest: 'a'.repeat(64), sender_native_id: NATIVE_ID, sender_tag: NATIVE_TAG,
  sender_workspace: NATIVE_WS, sender_generation: 1, sender_created_at_ms: NATIVE_CREATED });

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}

/** Synthetic placement only: never starts a live agent. */
function fakeReserve(fail?: any) {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    if (fail) return { value: fail, status: fail.status || 409 };
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>, reserve?: (input: any) => { value: any; status: number }, options?: any) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now,
      reserve ? { reserve } : {}, options || {});
    service.init();
    // Presenting host credential row so family continuity is exercised honestly,
    // plus its live machine registry row (hostOk consults machines on every
    // host call; a deleted machine binds nothing).
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql);
  });
}

/** Model the legitimate Fleet consumer report for one launch (claimed and
 * reported running with its native receipt), which F5 binding reconciles
 * against. Fixture setup only: the binding route under test stays real. */
function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

/** Model the existing Fleet sessions report for one native session (the
 * authoritative native identity the F5 binding reconciles workspace/tag
 * against). Fixture setup only. */
function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'codex', ?, NULL, ?, ?, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function startOk(s: SessionControlService, extra: any = {}, auth: any = admin, key = crypto.randomUUID()) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', ...extra }), auth);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

async function bindHttp(s: SessionControlService, sql: any, sessionId: string, machine = 'hetzner', extra: any = {}) {
  const session: any = sql.exec('SELECT fleet_launch_id, machine_id FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
  const launchId = session?.fleet_launch_id || 'launch-1';
  ensureFleetLaunch(sql, launchId, session?.machine_id || machine);
  reportFleetSession(sql, session?.machine_id || machine, NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`, { native: { ...bound({ tag: canonTag(launchId) }), ...extra } }), host(machine));
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(200);
  expect(data.state).toBe('running');
  return data;
}

async function claimAs(s: SessionControlService, commandId: string, correlation: string, machine = 'hetzner', attemptId = ATTEMPT) {
  const res = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation, attempt_id: attemptId }), host(machine));
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}

describe('hub session control durable queue', () => {
  it('rejects unauthenticated and unscoped callers', async () => fixture(async (s) => {
    expect((await s.handle(request('/api/sessions', { key: 'k', workspace: 'demo' }), unauth)).status).toBe(401);
    expect((await s.handle(request('/api/sessions', { key: 'k', workspace: 'demo' }), host('hetzner'))).status).toBe(403);
    const scoped = await s.handle(request('/api/sessions', undefined, 'GET'), stranger);
    expect(scoped.status).toBe(200);
    expect((await scoped.json() as any).sessions).toEqual([]);
  }));

  it('validates start inputs without touching placement', async () => fixture(async (s) => {
    for (const payload of [
      { key: 'k1' }, // missing workspace
      { key: 'k2', workspace: 'demo', tier: 'ultra' },
      { key: 'k2b', workspace: 'demo', tier: 'quick' }, // tier has no fleet consumer
      { key: 'k3', workspace: 'demo', cwd: '../escape' },
      { key: 'k3b', workspace: 'demo', cwd: 'demo/project' }, // cwd has no fleet start consumer
      { key: 'k4', workspace: 'demo', cwd: '/absolute' },
      { key: 'k5', workspace: 'demo', mode: 'interactive', brief: 'inject' },
      { key: 'k6', workspace: 'demo', brief: '' },
    ]) {
      expect((await s.handle(request('/api/sessions', payload), admin)).status, JSON.stringify(payload)).toBe(400);
    }
    const tiered = await s.handle(request('/api/sessions', { key: 'k2b', workspace: 'demo', tier: 'quick' }), admin);
    expect((await tiered.json() as any).error).toBe('unsupported_tier');
    const cwd = await s.handle(request('/api/sessions', { key: 'k3b', workspace: 'demo', cwd: 'demo/project' }), admin);
    expect((await cwd.json() as any).error).toBe('unsupported_cwd');
  }));

  it('rejects tier before placement and never sends it to fleet', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const res = await s.handle(request('/api/sessions', { key: 'tier-1', workspace: 'demo', tier: 'quick' }), admin);
      expect(res.status).toBe(400);
      expect(fake.calls.length).toBe(0);
    }, fake.hook);
  });

  it('holds honestly when placement is not wired', async () => fixture(async (s) => {
    const first = await s.handle(request('/api/sessions', { key: 'hold-1', workspace: 'demo' }), admin);
    expect(first.status).toBe(409);
    expect((await first.json() as any).hold).toBe('no_placement_authority');
    const replay = await s.handle(request('/api/sessions', { key: 'hold-1', workspace: 'demo' }), admin);
    expect(replay.status).toBe(409);
  }));

  it('starts through placement, replays same key, conflicts on changed intent', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const first: any = await startOk(s, { engine: 'codex', machine: 'hetzner' }, admin, 'start-1');
      expect(first.state).toBe('offered');
      expect(first.machine).toBe('hetzner');
      expect(fake.calls.length).toBe(1);
      const replay = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'codex', machine: 'hetzner' }), admin);
      expect(replay.status).toBe(200);
      expect((await replay.json() as any).replay).toBe(true);
      expect(fake.calls.length).toBe(1);
      const conflict = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'claude' }), admin);
      expect(conflict.status).toBe(409);
    }, fake.hook);
  });

  it('passes engine/machine/workspace as real placement constraints, never cwd/tier', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      await startOk(s, { engine: 'codex', machine: 'hetzner', brief: 'goal' }, admin, 'constraints-1');
      expect(fake.calls.length).toBe(1);
      const sent = fake.calls[0];
      expect(sent.engine).toBe('codex');
      expect(sent.machine).toBe('hetzner');
      expect(sent.workspace).toBe('demo');
      expect(sent.prompt).toBe('goal');
      expect(sent).not.toHaveProperty('cwd');
      expect(sent).not.toHaveProperty('tier');
      // Non-root cwd is rejected BEFORE any reservation, not passed through.
      expect((await s.handle(request('/api/sessions', { key: 'cwd-x', workspace: 'demo', cwd: 'demo/project' }), admin)).status).toBe(400);
      expect(fake.calls.length).toBe(1);
    }, fake.hook);
  });

  it('passes fleet client errors through instead of holding', async () => {
    const fake = fakeReserve({ error: 'invalid_launch_request', status: 400 });
    await fixture(async (s, sql) => {
      const res = await s.handle(request('/api/sessions', { key: 'bad-1', workspace: 'demo' }), admin);
      expect(res.status).toBe(400);
      expect((await res.json() as any).error).toBe('invalid_launch_request');
    }, fake.hook);
  });

  it('records held intent on placement failure without launching twice', async () => {
    const fake = fakeReserve({ error: 'no_eligible_route', rejected: [] });
    await fixture(async (s, sql) => {
      const first = await s.handle(request('/api/sessions', { key: 'held-1', workspace: 'demo' }), admin);
      expect(first.status).toBe(409);
      expect((await first.json() as any).hold.error).toBe('no_eligible_route');
      const retry = await s.handle(request('/api/sessions', { key: 'held-1', workspace: 'demo' }), admin);
      expect(retry.status).toBe(409);
      expect(fake.calls.length).toBe(2);
    }, fake.hook);
  });

  it('refuses send/capture on unbound sessions', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, {}, admin, 'unbound-1');
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'a', body: 'hi' }), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'b' }), admin)).status).toBe(409);
    }, fake.hook);
  });

  it('enqueues send with idempotent keys and rejects conflicts', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, {}, admin, 'bound-1');
      ensureFleetLaunch(sql, 'launch-1', 'hetzner');
      reportFleetSession(sql, 'hetzner', NATIVE_ID, 'hub-launch-1', NATIVE_WS);
      expect(s.recordBinding(session.id, bound())).toMatchObject({ ok: true });
      const first = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'prompt one' }), admin);
      expect(first.status).toBe(201);
      const replay = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'prompt one' }), admin);
      expect(replay.status).toBe(200);
      expect((await replay.json() as any).replay).toBe(true);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'other' }), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'x', body: '' }), admin)).status).toBe(400);
    }, fake.hook);
  });

  it('isolates sessions and commands by owner', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, {}, agent(), 'owner-1');
      expect((await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), stranger)).status).toBe(404);
      expect((await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), stranger)).status).toBe(404);
    }, fake.hook);
  });

  it('keeps refresh-family continuity but rejects rebound credentials', async () =>
    fixture(async (s, sql) => {
      const at = new Date().toISOString();
      const far = new Date(Date.now() + 30 * 86400000).toISOString();
      // Genuine registry incarnation FIRST: the agent registry binding for a
      // credential resolves at credential-insert time, so the agents row must
      // predate the credentials (enrollment order). Actor demo:assistant
      // resolves to project demo / tag assistant.
      sql.exec(`INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
      for (const [id, family] of [['cred-A', 'family-1'], ['cred-B', 'family-1'], ['cred-C', 'family-2']]) {
        sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
          VALUES (?, 'agent', ?, ?, ?, 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?)`,
          id, 'hash-' + id, 'refresh-' + id, family, at, far);
      }
      const authA = agent('cred-A');
      const created: any = await startOk(s, {}, authA, 'family-1');
      // Same credential replays.
      const replay = await s.handle(request('/api/sessions', { key: 'family-1', workspace: 'demo' }), authA);
      expect(replay.status).toBe(200);
      // Refresh rotation (same family, new id) keeps ownership and adopts the id.
      const rotated = await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), agent('cred-B'));
      expect(rotated.status).toBe(200);
      // Delete + reregister (new family, same actor) is a different owner: 404.
      expect((await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), agent('cred-C'))).status).toBe(404);
    }, fakeReserve().hook));

  it('binds native sessions over HTTP and reconciles fleet result to running', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'bind-1');
      expect(session.fleet_launch_id).toBe('launch-1');
      const bound: any = await bindHttp(s, sql, session.id);
      expect(bound.generation).toBe(1);
      const view = await (await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json() as any;
      expect(view.state).toBe('running');
      expect(view.binding.native_id).toBe(NATIVE_ID);
      // Foreign machine cannot bind; conflicting native id is never adopted.
      expect((await s.handle(request(`/api/sessions/${session.id}/binding`, { native: native() }), host('win35'))).status).toBe(404);
      const conflict = await s.handle(request(`/api/sessions/${session.id}/binding`,
        { native: { native_id: '22222222-2222-4222-8222-222222222222', tag: NATIVE_TAG, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }), host('hetzner'));
      expect(conflict.status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${session.id}/binding`, { native: { native_id: 'not-a-uuid', tag: NATIVE_TAG, workspace: NATIVE_WS } }), host('hetzner'))).status).toBe(400);
    }, fake.hook);
  });

  it('exposes a null cwd in host offers while rejecting non-root cwd at start', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'cwd-1');
      expect(fake.calls[0]).not.toHaveProperty('cwd');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'c1', body: 'go' }), admin);
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers.length).toBe(1);
      expect(offers.offers[0].cwd).toBe(null);
      expect(offers.offers[0].workspace).toBe('demo');
    }, fake.hook);
  });

  it('polls per-machine offers once and requires transport before ack', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'poll-1');
      ensureFleetLaunch(sql, 'launch-1', 'hetzner');
      reportFleetSession(sql, 'hetzner', NATIVE_ID, 'hub-launch-1', NATIVE_WS);
      s.recordBinding(session.id, bound());
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'p1', body: 'go' }), admin);
      const foreign = await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('win35'));
      expect((await foreign.json() as any).offers).toEqual([]);
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers.length).toBe(1);
      expect(offers.offers[0].native_tag).toBe(NATIVE_TAG);
      const again = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(again.offers).toEqual([]);
    }, fake.hook);
  });

  it('fences execution with atomic claims: no claim, no ack; rivals conflict', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'claim-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'c1', body: 'go' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer = poll.offers[0];
      const ackUrl = `/api/session-commands/${offer.command_id}/ack`;
      // Transport mark alone never authorizes an ack.
      expect((await s.handle(request(ackUrl, { correlation: offer.correlation, outcome: 'transported', native: native(), claim_token: 'bogus' }), host('hetzner'))).status).toBe(409);
      // Rival machine cannot claim; wrong correlation cannot claim.
      expect((await s.handle(request(`/api/session-commands/${offer.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT }), host('win35'))).status).toBe(404);
      expect((await s.handle(request(`/api/session-commands/${offer.command_id}/claim`, { correlation: 'wrong', attempt_id: ATTEMPT }), host('hetzner'))).status).toBe(409);
      const claim = await claimAs(s, offer.command_id, offer.correlation);
      expect(claim.attempt).toBe(1);
      // Same holder replays its own unexpired claim (lost-reply reconciliation).
      const replay = await claimAs(s, offer.command_id, offer.correlation);
      expect(replay.claim_token).toBe(claim.claim_token);
      // A second attempt nonce on the same machine is refused: one claim
      // authorizes exactly one effect, never another executor.
      const rival = await s.handle(request(`/api/session-commands/${offer.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT2 }), host('hetzner'));
      expect(rival.status).toBe(409);
      expect((await rival.json() as any).error).toBe('already_claimed');
      // Wrong token cannot ack even with the right correlation.
      expect((await s.handle(request(ackUrl, { correlation: offer.correlation, outcome: 'transported', native: native(), claim_token: 'bogus' }), host('hetzner'))).status).toBe(409);
      const done = await s.handle(request(ackUrl, { correlation: offer.correlation, outcome: 'transported', native: native(), claim_token: claim.claim_token }), host('hetzner'));
      expect(done.status).toBe(200);
      expect((await done.json() as any).state).toBe('transported');
    }, fake.hook);
  });

  it('turns expired claims uncertain instead of reissuing', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'expiry-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'e1', body: 'go' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer = poll.offers[0];
      const claim = await claimAs(s, offer.command_id, offer.correlation);
      // Let the lease lapse, then ack: the effect may have happened.
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
        new Date(Date.now() - 1000).toISOString(), offer.command_id);
      const late = await s.handle(request(`/api/session-commands/${offer.command_id}/ack`,
        { correlation: offer.correlation, outcome: 'delivered', native: native(), claim_token: claim.claim_token,
          proof: goodProof() }),
        host('hetzner'));
      expect(late.status).toBe(409);
      expect((await late.json() as any).error).toBe('claim_expired');
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(list.commands[0].state).toBe('uncertain');
      // Uncertain commands are never re-offered for blind retry.
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers).toEqual([]);
    }, fake.hook);
  });

  it('requires host-verified native reply provenance for delivered sends', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'proof-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 's1', body: 'go' }), admin);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 's2', body: 'go again' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const [first, second] = poll.offers;
      const c1 = await claimAs(s, first.command_id, first.correlation);
      const ackUrl = (id: string) => `/api/session-commands/${id}/ack`;
      const base = { correlation: first.correlation, outcome: 'delivered', native: native(), claim_token: c1.claim_token };
      // Host assertion without proof is not delivery.
      expect((await s.handle(request(ackUrl(first.command_id), base), host('hetzner'))).status).toBe(409);
      // Foreign sender UUID fails; stale generation/created_at fails; a
      // message is never its own reply; bad digest shape fails.
      const good = goodProof();
      for (const [name, proof] of [
        ['foreign', { ...good, sender_native_id: '33333333-3333-4333-8333-333333333333' }],
        ['stale-gen', { ...good, sender_generation: 0 }],
        ['stale-created', { ...good, sender_created_at_ms: NATIVE_CREATED - 1 }],
        ['self-reply', { ...good, reply_to_native_id: good.native_message_id }],
        ['bad-digest', { ...good, reply_digest: 'zz' }],
      ] as any) {
        expect((await s.handle(request(ackUrl(first.command_id), { ...base, proof }), host('hetzner'))).status, name).toBe(409);
      }
      expect((await s.handle(request(ackUrl(first.command_id), { ...base, proof: good }), host('hetzner'))).status).toBe(200);
      // The same native reply envelope never confirms a second command.
      const c2 = await claimAs(s, second.command_id, second.correlation);
      const replay = await s.handle(request(ackUrl(second.command_id),
        { correlation: second.correlation, outcome: 'delivered', native: native(), claim_token: c2.claim_token, proof: good }), host('hetzner'));
      expect(replay.status).toBe(409);
      expect((await replay.json() as any).error).toBe('proof_replayed');
    }, fake.hook);
  });

  it('acks stop only with confirmed exit and marks the session stopped', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'stop-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/stop`, { key: 't1' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const stop = poll.offers[0];
      const claim = await claimAs(s, stop.command_id, stop.correlation);
      const ackUrl = `/api/session-commands/${stop.command_id}/ack`;
      expect((await s.handle(request(ackUrl, { correlation: stop.correlation, outcome: 'delivered', native: native(), claim_token: claim.claim_token }), host('hetzner'))).status).toBe(409);
      const stopped = await s.handle(request(ackUrl, { correlation: stop.correlation, outcome: 'delivered', native: native(), claim_token: claim.claim_token, confirmed_exit: true }), host('hetzner'));
      expect(stopped.status).toBe(200);
      const view = await (await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json() as any;
      expect(view.state).toBe('stopped');
    }, fake.hook);
  });

  it('retains refused and uncertain commands without retry', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'uncertain-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'u1', body: 'go' }), admin);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'u2', body: 'go' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const [first, second] = poll.offers;
      const c1 = await claimAs(s, first.command_id, first.correlation);
      const c2 = await claimAs(s, second.command_id, second.correlation);
      expect((await s.handle(request(`/api/session-commands/${first.command_id}/ack`,
        { correlation: first.correlation, outcome: 'refused', native: native(), claim_token: c1.claim_token, reason: 'busy_composer' }), host('hetzner'))).status).toBe(200);
      expect((await s.handle(request(`/api/session-commands/${second.command_id}/ack`,
        { correlation: second.correlation, outcome: 'uncertain', native: native(), claim_token: c2.claim_token, reason: 'timeout' }), host('hetzner'))).status).toBe(200);
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(list.commands.map((c: any) => c.state).sort()).toEqual(['refused', 'uncertain']);
    }, fake.hook);
  });

  it('redacts capture text before storage', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'redact-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'c1' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const cmd = poll.offers[0];
      const claim = await claimAs(s, cmd.command_id, cmd.correlation);
      await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`,
        { correlation: cmd.correlation, outcome: 'delivered', native: native(), claim_token: claim.claim_token,
          capture: 'leaked sk-abcdef0123456789abcdef' }), host('hetzner'));
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(JSON.stringify(list.commands[0].ack)).not.toContain('sk-abcdef');
    }, fake.hook);
  });

  it('refuses conflicting native bindings on tag reuse', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, {}, admin, 'rebind-1');
      ensureFleetLaunch(sql, 'launch-1', 'hetzner');
      reportFleetSession(sql, 'hetzner', NATIVE_ID, 'hub-launch-1', NATIVE_WS);
      expect(s.recordBinding(session.id, bound())).toMatchObject({ ok: true });
      const second: any = await startOk(s, {}, admin, 'rebind-2');
      ensureFleetLaunch(sql, 'launch-2', 'hetzner', '22222222-2222-4222-8222-222222222222');
      reportFleetSession(sql, 'hetzner', '22222222-2222-4222-8222-222222222222', 'hub-launch-2', NATIVE_WS);
      expect(s.recordBinding(second.id, bound({ native_id: '22222222-2222-4222-8222-222222222222', tag: 'hub-launch-2' }))).toMatchObject({ ok: true });
      // Same session rebound to a different native id is a conflict, never silent adoption.
      const direct = s.recordBinding(session.id, bound({ native_id: '99999999-9999-4999-8999-999999999999' }));
      expect(direct).toMatchObject({ ok: false, error: 'binding_conflict' });
    }, fake.hook);
  });

  it('paginates session and command lists completely', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      for (const key of ['pg-1', 'pg-2', 'pg-3']) await startOk(s, {}, admin, key);
      const seen: string[] = [];
      let cursor: string | null = null;
      for (;;) {
        const url = '/api/sessions?limit=2' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
        const page = await (await s.handle(request(url, undefined, 'GET'), admin)).json() as any;
        expect(page.sessions.length).toBeLessThanOrEqual(2);
        seen.push(...page.sessions.map((x: any) => x.id));
        if (!page.next_cursor) break;
        cursor = page.next_cursor;
      }
      expect(seen.length).toBe(3);
      expect(new Set(seen).size).toBe(3);
    }, fake.hook);
  });

  it('refuses new work when the queue is full without evicting pending work', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'cap-1');
      await bindHttp(s, sql, session.id);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'q1', body: 'go' }), admin)).status).toBe(201);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'q2', body: 'go' }), admin)).status).toBe(429);
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(list.commands.length).toBe(1);
      expect(list.commands[0].state).toBe('recorded');
    }, fake.hook, { maxPendingCommands: 1, maxSessionCommands: 1 });
  });
});

describe('send proof validator (host-verified native reply provenance)', () => {
  const expected = { native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS, generation: 1, native_created_at_ms: NATIVE_CREATED };
  const good = { native_message_id: 'nmsg-1', reply_to_native_id: 'nreply-1', reply_digest: 'b'.repeat(64),
    sender_native_id: NATIVE_ID, sender_tag: NATIVE_TAG, sender_workspace: NATIVE_WS,
    sender_generation: 1, sender_created_at_ms: NATIVE_CREATED };
  it('accepts the exact native reply provenance', () => {
    expect(validateSendProof(good, expected)).toEqual({ ok: true });
  });
  it('rejects missing, foreign, stale, self-reply, and malformed proofs', () => {
    expect(validateSendProof(undefined, expected)).toMatchObject({ ok: false, error: 'proof_required' });
    expect(validateSendProof({ ...good, sender_native_id: '33333333-3333-4333-8333-333333333333' }, expected))
      .toMatchObject({ ok: false, error: 'proof_sender_mismatch' });
    expect(validateSendProof({ ...good, sender_generation: 0 }, expected))
      .toMatchObject({ ok: false, error: 'proof_generation_stale' });
    expect(validateSendProof({ ...good, sender_created_at_ms: 1 }, expected))
      .toMatchObject({ ok: false, error: 'proof_generation_stale' });
    expect(validateSendProof({ ...good, reply_to_native_id: good.native_message_id }, expected))
      .toMatchObject({ ok: false, error: 'proof_reply_mismatch' });
    expect(validateSendProof({ ...good, reply_digest: 'short' }, expected))
      .toMatchObject({ ok: false, error: 'invalid_proof_shape' });
  });
});
