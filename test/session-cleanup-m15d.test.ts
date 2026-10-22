/** M15d REAL cleanup --apply suite (synthetic fixtures only).
 *
 * The server enqueues genuine M12 stop intents for freshly eligible OWN
 * sessions (no second store/classifier; dry-run stays readonly). The host
 * executes each stop through the existing claim/effect/ack custody and the
 * host removes owned worktrees under the trusted allowlist (covered in
 * host/tests/test_session_cleanup_m15d.py with synthetic repos only).
 * No native/Git effects anywhere in this file.
 */
import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, deriveCleanupSessionKey, CLEANUP_APPLY_ALLOWED_STATES } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const stranger = { caller: { kind: 'agent', actor: 'other:assistant', tokenId: 'cred-2' }, scopes: ['fleet:launch', 'bus:send'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '22222222-2222-4222-8222-222222222222';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const canonTag = (launchId: string) => 'hub-' + launchId;

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

function fakeReserve() {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-cleanup-m15d-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const reserve = fakeReserve();
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: reserve.hook });
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'other', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-2', 'agent', 'hash-cred-2', 'refresh-cred-2', 'agent-family-2', 'other-agent', NULL, 'other:assistant', '["fleet:launch"]', 'test', ?, ?, 'other:assistant')`, at, far);
    await fn(service, hub.sql);
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'demo:assistant', 'agent', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'codex', ?, NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function startAgentSession(s: SessionControlService, key: string) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo' }), agent());
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

async function bindAgentSession(s: SessionControlService, sql: any, sessionId: string, launchId: string, nativeId: string = NATIVE_ID, workspace: string = NATIVE_WS) {
  ensureFleetLaunch(sql, launchId, 'hetzner', nativeId);
  reportFleetSession(sql, 'hetzner', nativeId, canonTag(launchId), workspace);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`, {
    native: { native_id: nativeId, tag: canonTag(launchId), workspace, native_created_at_ms: NATIVE_CREATED },
  }), host('hetzner'));
  expect(res.status).toBe(200);
}

async function finishObservation(s: SessionControlService, nativeId: string = NATIVE_ID) {
  const now = new Date().toISOString();
  const res = await s.handle(request('/api/sessions/observations', {
    machine_id: 'hetzner', native_id: nativeId,
    payload: { report_written: true, report_at: now, screen_done: true, authoritative_no_pending_proof: true },
  }), host('hetzner'));
  expect(res.status).toBe(201);
}

describe('m15d cleanup apply', () => {
  it('enqueues a real M12 stop for an eligible own finished session and replays it', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-done');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    const key = `hetzner/${NATIVE_ID}`;
    const dry = await (await s.handle(request('/api/sessions/cleanup', { dry_run: true }), agent())).json() as any;
    expect(dry.eligible.map((e: any) => e.session_key)).toContain(key);
    const first = await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-one' }), agent());
    expect(first.status, await first.clone().text()).toBe(200);
    const plan = await first.json() as any;
    expect(plan.apply).toBe(true);
    expect(plan.operation_key).toBe('op-one');
    expect(plan.enqueued).toHaveLength(1);
    expect(plan.enqueued[0].session_key).toBe(key);
    expect(plan.enqueued[0].replay).toBe(false);
    const stopped = sql.exec("SELECT * FROM session_control_commands WHERE kind = 'stop'").toArray() as any[];
    expect(stopped).toHaveLength(1);
    // Lost-response retry with the same key replays the SAME command: never duplicates.
    const retry = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-one' }), agent())).json() as any;
    expect(retry.enqueued).toHaveLength(1);
    expect(retry.enqueued[0].command_id).toBe(plan.enqueued[0].command_id);
    expect(retry.enqueued[0].replay).toBe(true);
    expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'stop'").one() as any).n).toBe(1);
    // A second operation with a fresh key still reuses the pending intent.
    const fresh = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-two' }), agent())).json() as any;
    expect(fresh.enqueued[0].command_id).toBe(plan.enqueued[0].command_id);
    expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'stop'").one() as any).n).toBe(1);
  }));

  it('derives bounded collision-free session keys from full operation key and session ID', async () => {
    const k1 = await deriveCleanupSessionKey('m15-cleanup-batch-alpha', 'session-1234');
    const k2 = await deriveCleanupSessionKey('m15-cleanup-batch-beta', 'session-1234');
    const k3 = await deriveCleanupSessionKey('m15-cleanup-batch-alpha', 'session-5678');
    const k1Replay = await deriveCleanupSessionKey('m15-cleanup-batch-alpha', 'session-1234');

    expect(k1).toMatch(/^cu-[a-zA-Z0-9_-]{40,60}$/);
    expect(k1.length).toBeLessThanOrEqual(64);
    expect(k1).toBe(k1Replay);
    expect(k1).not.toBe(k2); // Common 12-char prefix does NOT collide
    expect(k1).not.toBe(k3); // Different session ID does NOT collide
    expect(CLEANUP_APPLY_ALLOWED_STATES.has('stopped')).toBe(true);
  });

  it('allows cleanup --apply for already-stopped sessions while ordinary stop refuses', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-already-stopped');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);

    // Mark the canonical session stopped (e.g. naturally exited / host reported exit)
    sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = ?", created.id);

    // Ordinary stop API must refuse already-stopped sessions
    const ordStop = await s.handle(request(`/api/sessions/${created.id}/stop`, { key: 'ord-stop-1' }), agent());
    expect(ordStop.status).toBe(409);
    const ordBody = await ordStop.json() as any;
    expect(ordBody.error).toBe('session_not_stoppable');

    // Cleanup --apply MUST allow already-stopped sessions and enqueue a cleanup stop command
    const cleanupRes = await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-cleanup-stopped' }), agent());
    expect(cleanupRes.status, await cleanupRes.clone().text()).toBe(200);
    const cleanupPlan = await cleanupRes.json() as any;
    expect(cleanupPlan.apply).toBe(true);
    expect(cleanupPlan.enqueued).toHaveLength(1);
    expect(cleanupPlan.enqueued[0].session_id).toBe(created.id);
    expect(cleanupPlan.enqueued[0].replay).toBe(false);

    // Command must have cleanup_tree authorized
    const stopCmd = sql.exec("SELECT * FROM session_control_commands WHERE session_id = ? AND kind = 'stop'", created.id).toArray()[0] as any;
    expect(stopCmd).toBeTruthy();
    const payload = JSON.parse(stopCmd.payload_json);
    expect(payload.cleanup_tree).toBe(true);
    expect(payload.operation_key).toBe('op-cleanup-stopped');

    // Exact replay of cleanup apply with same key replays same command
    const replayRes = await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-cleanup-stopped' }), agent());
    expect(replayRes.status).toBe(200);
    const replayPlan = await replayRes.json() as any;
    expect(replayPlan.enqueued).toHaveLength(1);
    expect(replayPlan.enqueued[0].command_id).toBe(stopCmd.id);
    expect(replayPlan.enqueued[0].replay).toBe(true);
  }));

  it('refuses admin apply (never another owner) and validates keys', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-admin');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    expect((await s.handle(request('/api/sessions/cleanup', { apply: true }), admin)).status).toBe(404);
    expect((await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'bad key!' }), agent())).status).toBe(400);
    expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'stop'").one() as any).n).toBe(0);
  }));

  it('enqueues nothing for strangers, idle, limit-blocked, or human records', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-idle');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const stale = new Date(Date.now() - 25 * 3600000).toISOString();
    sql.exec('UPDATE fleet_sessions SET last_activity_at = ? WHERE machine_id = ? AND native_id = ?', stale, 'hetzner', NATIVE_ID);
    const idle = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-idle' }), agent())).json() as any;
    expect(idle.enqueued).toEqual([]);
    expect(idle.refused.length).toBeGreaterThan(0);
    const now = new Date().toISOString();
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { limit_hit: true, limit_window: '5h' },
    }), host('hetzner'))).status).toBe(201);
    const limited = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-limit' }), agent())).json() as any;
    expect(limited.enqueued).toEqual([]);
    const foreign = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-other' }), stranger)).json() as any;
    expect(foreign.enqueued).toEqual([]);
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
      VALUES ('hetzner', '33333333-3333-4333-8333-333333333333', 'hub-launch-h', 'codex', 'running', NULL, ?, '/srv/h', NULL, NULL, 'fixture-acct', NULL)`, now);
    sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES ('launch-h', 'human:alexey', 'human', NULL, 'fixture', 'fleet-launch-h', '{}', 'hetzner', 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', 'running', '33333333-3333-4333-8333-333333333333', NULL, ?, ?)`, now, now);
    const human = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-human' }), agent())).json() as any;
    expect(human.enqueued).toEqual([]);
    expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'stop'").one() as any).n).toBe(0);
  }));

  it('exposes the narrow host observation gate without new privilege', async () => fixture(async (s) => {
    expect(s.hostObservationAllowed('hetzner', { kind: 'host', machineId: 'hetzner', tokenId: 'host-cred-1' } as any)).toBe(true);
    expect(s.hostObservationAllowed('elsewhere', { kind: 'host', machineId: 'hetzner', tokenId: 'host-cred-1' } as any)).toBe(false);
    expect(s.hostObservationAllowed('hetzner', { kind: 'agent', actor: 'demo:assistant', tokenId: 'cred-1' } as any)).toBe(false);
  }));
});

describe('m15d stop-bundled tree outcome in the stop ack', () => {
  const N2 = '33333333-3333-4333-8333-333333333333';
  const W2 = '/srv/hub/other';
  const nativeTriple = (nativeId: string = NATIVE_ID, launchId = 'launch-1', workspace: string = NATIVE_WS) =>
    ({ native_id: nativeId, tag: canonTag(launchId), workspace });

  async function enqueueStop(s: SessionControlService, sql: any, key: string, opKey: string,
      launchId = 'launch-1', nativeId: string = NATIVE_ID, workspace: string = NATIVE_WS) {
    const created = await startAgentSession(s, key);
    await bindAgentSession(s, sql, created.id, launchId, nativeId, workspace);
    await finishObservation(s, nativeId);
    const plan = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: opKey }), agent())).json() as any;
    expect(plan.enqueued).toHaveLength(1);
    return { sessionId: created.id, commandId: plan.enqueued[0].command_id };
  }

  async function offerFor(s: SessionControlService, commandId: string) {
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const offer = (poll.offers as any[]).find((o: any) => o.command_id === commandId);
    expect(offer, JSON.stringify(poll)).toBeTruthy();
    return offer;
  }

  async function claimAs(s: SessionControlService, commandId: string, correlation: string) {
    const res = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    const data = await res.json() as any;
    expect(res.status, JSON.stringify(data)).toBe(200);
    return data;
  }

  it('persists a valid owning-host tree outcome on the stop ack and surfaces it via commands', async () => fixture(async (s, sql) => {
    const { sessionId, commandId } = await enqueueStop(s, sql, 'm15d-tree-1', 'op-tree-1');
    const offer = await offerFor(s, commandId);
    const claim = await claimAs(s, commandId, offer.correlation);
    const acked = await s.handle(request(`/api/session-commands/${commandId}/ack`, {
      correlation: offer.correlation, outcome: 'delivered', native: nativeTriple(),
      claim_token: claim.claim_token, confirmed_exit: true,
      tree: { state: 'removed', reason: 'owned worktree removed; branch deleted after fresh ancestry proof' },
    }), host('hetzner'));
    expect(acked.status, await acked.clone().text()).toBe(200);
    const view = await (await s.handle(request(`/api/sessions/${sessionId}`, undefined, 'GET'), admin)).json() as any;
    expect(view.state).toBe('stopped');
    const list = await (await s.handle(request(`/api/sessions/${sessionId}/commands`, undefined, 'GET'), admin)).json() as any;
    const row = (list.commands as any[]).find((c: any) => c.id === commandId);
    expect(row.ack.tree).toEqual({ state: 'removed', reason: 'owned worktree removed; branch deleted after fresh ancestry proof' });
    expect(row.ack.claim_token).toBeUndefined();
  }));

  it('rejects tree on non-stop commands and malformed tree shapes', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-tree-2');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const sent = await s.handle(request(`/api/sessions/${created.id}/send`, { key: 'tree-send-1', body: 'go' }), agent());
    expect(sent.status).toBe(201);
    const cmdId = ((await sent.json()) as any).command_id;
    const offer = await offerFor(s, cmdId);
    const claim = await claimAs(s, cmdId, offer.correlation);
    const base = { correlation: offer.correlation, outcome: 'transported', native: nativeTriple(), claim_token: claim.claim_token };
    // Tree is stop-only.
    expect((await s.handle(request(`/api/session-commands/${cmdId}/ack`, { ...base, tree: { state: 'kept', reason: 'x' } }), host('hetzner'))).status).toBe(400);
    // Malformed shapes fail closed on a stop command too (distinct native identity).
    const { commandId } = await enqueueStop(s, sql, 'm15d-tree-3', 'op-tree-3', 'launch-2', N2, W2);
    const stopOffer = await offerFor(s, commandId);
    const stopClaim = await claimAs(s, commandId, stopOffer.correlation);
    const stopBase = { correlation: stopOffer.correlation, outcome: 'delivered', native: nativeTriple(N2, 'launch-2', W2), claim_token: stopClaim.claim_token, confirmed_exit: true };
    for (const bad of [
      { state: 'removed' }, // missing reason
      { state: 'vaporized', reason: 'x' }, // unknown state
      { state: 'kept', reason: 'x'.repeat(301) }, // oversize reason
      'removed', // not an object
    ]) {
      expect((await s.handle(request(`/api/session-commands/${commandId}/ack`, { ...stopBase, tree: bad }), host('hetzner'))).status, JSON.stringify(bad)).toBe(400);
    }
    // The command is still claimable-acked afterwards: malformed tree never consumed the claim.
    const ok = await s.handle(request(`/api/session-commands/${commandId}/ack`, { ...stopBase, tree: { state: 'kept', reason: 'dirty; listed-only' } }), host('hetzner'));
    expect(ok.status).toBe(200);
  }));

  it('converts the tree to kept on owner drift while still acking the stop', async () => fixture(async (s, sql) => {
    const { sessionId, commandId } = await enqueueStop(s, sql, 'm15d-tree-4', 'op-tree-4');
    const offer = await offerFor(s, commandId);
    const claim = await claimAs(s, commandId, offer.correlation);
    sql.exec("UPDATE session_control_sessions SET actor = 'other:assistant' WHERE id = ?", sessionId);
    const acked = await s.handle(request(`/api/session-commands/${commandId}/ack`, {
      correlation: offer.correlation, outcome: 'delivered', native: nativeTriple(),
      claim_token: claim.claim_token, confirmed_exit: true,
      tree: { state: 'removed', reason: 'host believed it owned this' },
    }), host('hetzner'));
    expect(acked.status, await acked.clone().text()).toBe(200);
    const list = await (await s.handle(request(`/api/sessions/${sessionId}/commands`, undefined, 'GET'), admin)).json() as any;
    const row = (list.commands as any[]).find((c: any) => c.id === commandId);
    expect(row.ack.tree.state).toBe('kept');
    expect(row.ack.tree.reason).toContain('ownership_changed');
  }));
});

describe('m15d cleanup_tree authorization (critical blocker)', () => {
  const N2 = '33333333-3333-4333-8333-333333333333';
  const W2 = '/srv/hub/other';
  const nativeTriple = (nativeId: string = NATIVE_ID, launchId = 'launch-1', workspace: string = NATIVE_WS) =>
    ({ native_id: nativeId, tag: canonTag(launchId), workspace });

  async function offerFor(s: SessionControlService, commandId: string) {
    const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
    const offer = (poll.offers as any[]).find((o: any) => o.command_id === commandId);
    expect(offer, JSON.stringify(poll)).toBeTruthy();
    return offer;
  }

  async function claimAs(s: SessionControlService, commandId: string, correlation: string) {
    const res = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    const data = await res.json() as any;
    expect(res.status, JSON.stringify(data)).toBe(200);
    return data;
  }

  it('exposes cleanup_tree true in offer+claim only for cleanup --apply stops; ordinary stops carry false', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-auth-1');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    // Ordinary stop first: no flag anywhere.
    const stop = await s.handle(request(`/api/sessions/${created.id}/stop`, { key: 'ordinary-1' }), agent());
    expect(stop.status).toBe(201);
    const stopId = ((await stop.json()) as any).command_id;
    const ordinaryOffer = await offerFor(s, stopId);
    expect(ordinaryOffer.cleanup_tree).toBe(false);
    const ordinaryClaim = await claimAs(s, stopId, ordinaryOffer.correlation);
    expect(ordinaryClaim.cleanup_tree).toBe(false);
    // Cleanup --apply enqueues a SECOND flagged stop (never co-opts the ordinary one).
    const plan = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-auth-1' }), agent())).json() as any;
    expect(plan.enqueued).toHaveLength(1);
    expect(plan.enqueued[0].command_id).not.toBe(stopId);
    const cleanupOffer = await offerFor(s, plan.enqueued[0].command_id);
    expect(cleanupOffer.cleanup_tree).toBe(true);
    const cleanupClaim = await claimAs(s, plan.enqueued[0].command_id, cleanupOffer.correlation);
    expect(cleanupClaim.cleanup_tree).toBe(true);
  }));

  it('ordinary stop ack reporting a tree is coerced to kept (never a removal)', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-auth-2');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const stop = await s.handle(request(`/api/sessions/${created.id}/stop`, { key: 'ordinary-2' }), agent());
    const stopId = ((await stop.json()) as any).command_id;
    const offer = await offerFor(s, stopId);
    const claim = await claimAs(s, stopId, offer.correlation);
    expect(claim.cleanup_tree).toBe(false);
    const acked = await s.handle(request(`/api/session-commands/${stopId}/ack`, {
      correlation: offer.correlation, outcome: 'delivered', native: nativeTriple(),
      claim_token: claim.claim_token, confirmed_exit: true,
      tree: { state: 'removed', reason: 'buggy host removed without authorization' },
    }), host('hetzner'));
    expect(acked.status).toBe(200);
    const list = await (await s.handle(request(`/api/sessions/${created.id}/commands`, undefined, 'GET'), admin)).json() as any;
    const row = (list.commands as any[]).find((c: any) => c.id === stopId);
    expect(row.ack.tree.state).toBe('kept');
    expect(row.ack.tree.reason).toContain('not_cleanup_authorized');
  }));

  it('refuses the entire claim when the owner drifted before claim (no token, zero native effect)', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-auth-3');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    const plan = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-auth-3' }), agent())).json() as any;
    expect(plan.enqueued).toHaveLength(1);
    const commandId = plan.enqueued[0].command_id;
    const offer = await offerFor(s, commandId);
    expect(offer.cleanup_tree).toBe(true);
    sql.exec("UPDATE session_control_sessions SET actor = 'other:assistant' WHERE id = ?", created.id);
    const refused = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation: offer.correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    expect(refused.status).toBe(409);
    expect((await refused.json() as any).error).toBe('cleanup_not_eligible');
    // The command stays unclaimed: no holder, no token, still re-offerable, never executed.
    const row: any = sql.exec('SELECT * FROM session_control_commands WHERE id = ?', commandId).toArray()[0];
    expect(row.state).not.toBe('claimed');
    expect(row.holder_machine).toBeNull();
  }));

  it('refuses cleanup claims that turned human-owned or grew obligations before claim', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15d-auth-4');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    const plan = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'op-auth-4' }), agent())).json() as any;
    expect(plan.enqueued).toHaveLength(1);
    const commandId = plan.enqueued[0].command_id;
    const offer = await offerFor(s, commandId);
    // Human drift: the launch is now human-requested.
    sql.exec("UPDATE fleet_launches SET requester_kind = 'human', actor = 'human:alexey' WHERE id = ?", 'launch-1');
    const humanRefused = await s.handle(request(`/api/session-commands/${commandId}/claim`, { correlation: offer.correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    expect(humanRefused.status).toBe(409);
    expect((await humanRefused.json() as any).error).toBe('cleanup_not_eligible');
  }));
});

describe('muse-bind server seam (task-hub- tag + launch native_id)', () => {
  const nativeTriple = (nativeId: string = NATIVE_ID, tag: string, workspace: string = NATIVE_WS) =>
    ({ native_id: nativeId, tag, workspace });

  it('accepts the exact task-hub-<launch.id> tag for a genuine reported native; fuzzy tags still refused', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'muse-bind-1');
    const launchId = (created as any).fleet_launch_id as string;
    expect(launchId).toBeTruthy();
    const taskTag = 'task-hub-' + launchId;
    // Genuine launcher evidence: fleet result reported the native UUID and
    // the native catalog carries the exact task-hub tag + workspace.
    ensureFleetLaunch(sql, launchId, 'hetzner', NATIVE_ID);
    reportFleetSession(sql, 'hetzner', NATIVE_ID, taskTag, NATIVE_WS);
    const ok = await s.handle(request(`/api/sessions/${created.id}/binding`, {
      native: { native_id: NATIVE_ID, tag: taskTag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED },
    }), host('hetzner'));
    expect(ok.status, await ok.clone().text()).toBe(200);
    // Fuzzy/foreign tags are still refused on another session.
    const created2 = await startAgentSession(s, 'muse-bind-2');
    const launchId2 = (created2 as any).fleet_launch_id as string;
    ensureFleetLaunch(sql, launchId2, 'hetzner', null);
    const bad = await s.handle(request(`/api/sessions/${created2.id}/binding`, {
      native: { native_id: NATIVE_ID, tag: 'task-hub-' + launchId2 + '-extra', workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED },
    }), host('hetzner'));
    expect(bad.status).toBe(409);
    expect((await bad.json() as any).error).toBe('fleet_tag_mismatch');
  }));

  it('pending host-bindings expose the canonical launch native_id for exact-UUID binding', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'muse-bind-3');
    const launchId = (created as any).fleet_launch_id as string;
    ensureFleetLaunch(sql, launchId, 'hetzner', NATIVE_ID);
    const poll = await (await s.handle(request('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner'))).json() as any;
    const entry = (poll.sessions as any[]).find((e: any) => e.id === created.id);
    expect(entry, JSON.stringify(poll)).toBeTruthy();
    expect(entry.fleet_launch_id).toBe(launchId);
    expect(entry.native_id).toBe(NATIVE_ID);
  }));
});
