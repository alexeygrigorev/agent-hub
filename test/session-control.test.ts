import { seedLegacyObservedSessions } from './observed-native-fixture';
import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
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
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

/** Synthetic placement only: never starts a live agent. */
function fakeReserve(fail?: any) {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    if (fail) return { value: fail, status: fail.status || 409 };
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'claude' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>, reserve?: (input: any) => { value: any; status: number }, options?: any) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now,
      reserve ? { reserve } : {}, options || {});
    service.init();
    seedFixtureIntake(hub.sql);
    // Presenting host credential row so family continuity is exercised honestly,
    // plus its live machine registry row (hostOk consults machines on every
    // host call; a deleted machine binds nothing).
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    // Live agent registry incarnation for the default test caller: the outer
    // hub auth would have rejected a dead credential before the service runs,
    // and creation now re-verifies liveness inside the mutation txn.
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
    await fn(service, hub.sql);
  });
}

/** Model the legitimate Fleet consumer report for one launch (claimed and
 * reported running with its native receipt), which F5 binding reconciles
 * against. Fixture setup only: the binding route under test stays real. */
function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'claude', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

/** Model the existing Fleet sessions report for one native session (the
 * authoritative native identity the F5 binding reconciles workspace/tag/
 * engine/cwd against. Fixture setup only. */
function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running', cwd: string | null = null) {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'claude', ?, NULL, ?, ?, ?, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace, cwd);
}

async function startOk(s: SessionControlService, extra: any = {}, auth: any = admin, key = crypto.randomUUID()) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', ...extra }), auth);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

async function bindHttp(s: SessionControlService, sql: any, sessionId: string, machine = 'hetzner', extra: any = {}) {
  const session: any = sql.exec('SELECT fleet_launch_id, machine_id, request_json FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
  const launchId = session?.fleet_launch_id || 'launch-1';
  ensureFleetLaunch(sql, launchId, session?.machine_id || machine);
  // Model a current host report: the requested cwd (when any) is present in
  // the catalog row, so binding reconciliation can verify it.
  let cwd: string | null = null;
  try { cwd = JSON.parse(session?.request_json || '{}').cwd ?? null; } catch { cwd = null; }
  reportFleetSession(sql, session?.machine_id || machine, NATIVE_ID, canonTag(launchId), NATIVE_WS, 'running', cwd);
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

describe('failed launch diagnostics', () => {
  it('labels a previously bound legacy death as runtime, never dispatch', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    ensureFleetLaunch(sql, 'failed-runtime', 'hetzner', null, 'failed');
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, binding_json, created_at, updated_at) VALUES ('12121212-1212-4212-8212-121212121212', 'test-admin', 'runtime-key', '{}', 'failed-runtime', 'hetzner', 'headless', 'failed', ?, ?, ?)", JSON.stringify(bound()), at, at);
    const res = await s.handle(request('/api/sessions/12121212-1212-4212-8212-121212121212', undefined, 'GET'), admin);
    const view: any = await res.json();
    expect(view.launch_error).toMatchObject({ reason: 'engine_exited_without_diagnostic', stage: 'runtime' });
  }));

  // Intake 14453011 (launch 984fce21 ran ~33 min with activity, then failed
  // as launcher_failed_without_diagnostic at dispatch_route): the host
  // running receipt reached the fleet launch row (native_id set) even when
  // the session binding POST was missed, so started=true and the fallback
  // must be runtime engine_exited, never a dispatch-stage launcher failure.
  it('labels a mid-run death with launch native_id but no binding as runtime, never dispatch', async () => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    ensureFleetLaunch(sql, 'failed-midrun', 'hetzner', NATIVE_ID, 'failed');
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('34343434-3434-4343-8343-343434343434', 'test-admin', 'midrun-key', '{}', 'failed-midrun', 'hetzner', 'headless', 'failed', ?, ?)", at, at);
    const res = await s.handle(request('/api/sessions/34343434-3434-4343-8343-343434343434', undefined, 'GET'), admin);
    const view: any = await res.json();
    expect(view.launch_error).toMatchObject({ reason: 'engine_exited_without_diagnostic', stage: 'runtime' });
    expect(view.reason).toBe('engine_exited_without_diagnostic');
  }));

  it.each(['admission_unconfigured', 'launcher_not_started', null, 'secret=/private/token'])('projects a safe failure reason for %s', async reason => fixture(async (s, sql) => {
    const at = new Date().toISOString();
    ensureFleetLaunch(sql, 'failed-launch', 'hetzner', null, 'failed');
    sql.exec("UPDATE fleet_launches SET exit_reason = ? WHERE id = 'failed-launch'", reason);
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, mode, state, created_at, updated_at) VALUES ('12121212-1212-4212-8212-121212121212', 'test-admin', '12121212-1212-4212-8212-121212121212', '{}', 'failed-launch', 'hetzner', 'headless', 'failed', ?, ?)", at, at);
    const response = await s.handle(request('/api/sessions/12121212-1212-4212-8212-121212121212', undefined, 'GET'), admin);
    expect(response.status).toBe(200);
    const result: any = await response.json();
    expect(result.state).toBe('failed');
    expect(result.reason).toBe(reason && /^[a-z][a-z_]{0,63}$/.test(reason) ? reason : 'launcher_failed_without_diagnostic');
    expect(result.message.length).toBeGreaterThan(10);
    expect(result.launch_error.reason).toBe(result.reason);
    expect(JSON.stringify(result)).not.toContain('/private/token');
  }));
});

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
      { key: 'k2', workspace: 'demo', tier: 'ultra' }, // unknown tier
      { key: 'k3', workspace: 'demo', cwd: '../escape' },
      { key: 'k4', workspace: 'demo', cwd: '/absolute' },
      { key: 'k5', workspace: 'demo', mode: 'interactive', brief: 'inject' },
      { key: 'k6', workspace: 'demo', brief: '' },
    ]) {
      expect((await s.handle(request('/api/sessions', payload), admin)).status, JSON.stringify(payload)).toBe(400);
    }
    // Known tiers and confined relative cwds are placement constraints, not
    // rejections: they reach the fleet reserve (R3 D transport seam).
    const fake = fakeReserve();
    await fixture(async (s2) => {
      const tiered = await s2.handle(request('/api/sessions', { key: 'k2b', workspace: 'demo', tier: 'quick' }), admin);
      expect(tiered.status).toBe(201);
      const cwd = await s2.handle(request('/api/sessions', { key: 'k3b', workspace: 'demo', cwd: 'demo/project' }), admin);
      expect(cwd.status).toBe(201);
      expect(fake.calls.length).toBe(2);
      expect(fake.calls[0].tier).toBe('quick');
      expect(fake.calls[1].cwd).toBe('demo/project');
    }, fake.hook);
  }));

  it('sends tier through to fleet and rejects unknown tiers before placement', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const res = await s.handle(request('/api/sessions', { key: 'tier-1', workspace: 'demo', tier: 'nope' }), admin);
      expect(res.status).toBe(400);
      expect(fake.calls.length).toBe(0);
      const ok = await s.handle(request('/api/sessions', { key: 'tier-2', workspace: 'demo', tier: 'daily' }), admin);
      expect(ok.status).toBe(201);
      expect(fake.calls.length).toBe(1);
      expect(fake.calls[0].tier).toBe('daily');
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
      const first: any = await startOk(s, { engine: 'codex', engine_override_reason: 'Synthetic override for placement replay test.', machine: 'hetzner' }, admin, 'start-1');
      expect(first.state).toBe('offered');
      expect(first.machine).toBe('hetzner');
      expect(fake.calls.length).toBe(1);
      const replay = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'codex', engine_override_reason: 'Synthetic override for placement replay test.', machine: 'hetzner' }), admin);
      expect(replay.status).toBe(200);
      expect((await replay.json() as any).replay).toBe(true);
      expect(fake.calls.length).toBe(1);
      const conflict = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'claude', engine_override_reason: 'Synthetic override for conflict test.', machine: 'hetzner' }), admin);
      expect(conflict.status).toBe(409);
    }, fake.hook);
  });

  it('passes engine/machine/workspace/tier/cwd as real placement constraints', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      await startOk(s, { engine: 'codex', engine_override_reason: 'Synthetic override for placement constraints test.', machine: 'hetzner', brief: 'goal', tier: 'daily', cwd: 'demo/project' }, admin, 'constraints-1');
      expect(fake.calls.length).toBe(1);
      const sent = fake.calls[0];
      expect(sent.engine).toBe('codex');
      expect(sent.machine).toBe('hetzner');
      expect(sent.workspace).toBe('demo');
      expect(sent.prompt).toBe('goal');
      expect(sent.tier).toBe('daily');
      expect(sent.cwd).toBe('demo/project');
      // Malformed cwd is still rejected BEFORE any reservation.
      expect((await s.handle(request('/api/sessions', { key: 'cwd-x', workspace: 'demo', cwd: '../escape' }), admin)).status).toBe(400);
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

  it('supersedes and expires stale unplaced holds so retries cannot pile up junk', async () => {
    const fake = fakeReserve({ error: 'no_eligible_route', rejected: [] });
    await fixture(async (s, sql) => {
      const first = await s.handle(request('/api/sessions', { key: 'junk-1', workspace: 'demo', name: 'probe-a' }), admin);
      expect(first.status).toBe(409);
      const second = await s.handle(request('/api/sessions', { key: 'junk-2', workspace: 'demo', name: 'probe-a' }), admin);
      expect(second.status).toBe(409);
      // An aged unplaced hold for the same actor expires even under a different name.
      const old = Date.now() - 49 * 3600000;
      sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, owner_token_id, owner_family, parent_id, parent_source, session_type, created_at, updated_at)
        VALUES ('junk-aged', 'test-admin', 'junk-aged', '{"workspace":"demo","name":"aged"}', 'headless', 'held', NULL, NULL, NULL, 'unknown_external', 'headless', ?, ?)`, old, old);
      const third = await s.handle(request('/api/sessions', { key: 'junk-3', workspace: 'demo', name: 'probe-a' }), admin);
      expect(third.status).toBe(409);
      const state = (key: string) => sql.exec('SELECT state FROM session_control_sessions WHERE request_key = ?', key).toArray()[0]?.state;
      expect(state('junk-1')).toBe('cancelled');
      expect(state('junk-2')).toBe('cancelled');
      expect(state('junk-aged')).toBe('cancelled');
      // The newest intent stays held (and replayable); nothing placed is touched.
      expect(sql.exec("SELECT COUNT(*) AS n FROM session_control_sessions WHERE state = 'held' AND actor = 'test-admin'").toArray()[0].n).toBe(1);
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
      // resolves to project demo / tag assistant. OR IGNORE: the shared
      // fixture already seeds this live incarnation.
      sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
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

  it('carries requested cwd into host offers (null when unset)', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner', cwd: 'demo/project' }, admin, 'cwd-1');
      expect(fake.calls[0].cwd).toBe('demo/project');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'c1', body: 'go' }), admin);
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers.length).toBe(1);
      expect(offers.offers[0].cwd).toBe('demo/project');
      expect(offers.offers[0].workspace).toBe('demo');
      const plain: any = await startOk(s, { machine: 'hetzner' }, admin, 'cwd-2');
      expect(fake.calls[1].cwd).toBe(null);
      await bindHttp(s, sql, plain.id);
      await s.handle(request(`/api/sessions/${plain.id}/send`, { key: 'c2', body: 'go' }), admin);
      // The first command already transported on the earlier poll, so only
      // the new recorded command is offered; the session row still shows both.
      const offers2 = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers2.offers.length).toBe(1);
      expect(offers2.offers[0].cwd).toBe(null);
      const cmds = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(cmds.commands.length).toBe(1);
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

  it('reconciles valid late delivered proofs independent of sweep order, stays uncertain otherwise', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'expiry-1');
      await bindHttp(s, sql, session.id);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'e1', body: 'go' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer = poll.offers[0];
      const claim = await claimAs(s, offer.command_id, offer.correlation);
      // Let the lease lapse BEFORE the offers sweep marks the row uncertain,
      // then ack with a valid delivered proof: lateReplyAck reconciles to
      // acked (no new effect, no re-claim) instead of 409 claim_expired.
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
        new Date(Date.now() - 1000).toISOString(), offer.command_id);
      const late = await s.handle(request(`/api/session-commands/${offer.command_id}/ack`,
        { correlation: offer.correlation, outcome: 'delivered', native: native(), claim_token: claim.claim_token,
          proof: goodProof() }),
        host('hetzner'));
      expect(late.status, JSON.stringify(await late.clone().json())).toBe(200);
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(list.commands[0].state).toBe('acked');
      // Acked commands are never re-offered, and the fence never re-opens.
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers).toEqual([]);
      expect((await s.handle(request(`/api/session-commands/${offer.command_id}/claim`,
        { correlation: offer.correlation, attempt_id: ATTEMPT2 }), host('hetzner'))).status).toBe(409);
      // A second send proves the negative half: proof-less expiry stays uncertain.
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'e2', body: 'go again' }), admin);
      const poll2 = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer2 = poll2.offers[0];
      const claim2 = await claimAs(s, offer2.command_id, offer2.correlation);
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?',
        new Date(Date.now() - 1000).toISOString(), offer2.command_id);
      const late2 = await s.handle(request(`/api/session-commands/${offer2.command_id}/ack`,
        { correlation: offer2.correlation, outcome: 'transported', native: native(), claim_token: claim2.claim_token }),
        host('hetzner'));
      expect(late2.status).toBe(409);
      expect((await late2.json() as any).error).toBe('claim_expired');
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

describe('absolute workspace identifiers and friendly names', () => {
  const ABS = '/srv/hub/demo';
  it('accepts an explicitly configured absolute workspace and passes it to placement', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const res = await s.handle(request('/api/sessions', { key: 'abs-1', workspace: ABS }), admin);
      const data = await res.json() as any;
      expect(res.status, JSON.stringify(data)).toBe(201);
      expect(fake.calls[0].workspace).toBe(ABS);
    }, fake.hook);
  });
  it('refuses malformed absolute workspaces before any placement effect', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const bad = [
        '/srv/../escape', '/srv//double', '/srv/./dot', '/srv/hub/demo/',
        '/', '/srv/hub/de mo', '/srv/hub/demo\x00x',
        'relative/path', '',
      ];
      let i = 0;
      for (const workspace of bad) {
        const res = await s.handle(request('/api/sessions', { key: 'badws' + (i++), workspace }), admin);
        expect(res.status, JSON.stringify(workspace)).toBe(400);
      }
      expect(fake.calls.length).toBe(0);
    }, fake.hook);
  });
  it('stores the friendly name, projects it in list/show, and replays idempotently', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const created = await startOk(s, { workspace: 'demo', name: 'friendly' }, admin, 'name-1');
      expect(created.name).toBe('friendly');
      const show = await (await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), admin)).json() as any;
      expect(show.name).toBe('friendly');
      const list = await (await s.handle(request('/api/sessions', undefined, 'GET'), admin)).json() as any;
      expect(list.sessions[0].name).toBe('friendly');
      const replay = await (await s.handle(request('/api/sessions', { key: 'name-1', workspace: 'demo', name: 'friendly' }), admin)).json() as any;
      expect(replay.id).toBe(created.id);
      expect(replay.replay).toBe(true);
      const conflict = await s.handle(request('/api/sessions', { key: 'name-1', workspace: 'demo', name: 'other' }), admin);
      expect(conflict.status).toBe(409);
      const bad = await s.handle(request('/api/sessions', { key: 'name-bad', workspace: 'demo', name: '../escape' }), admin);
      expect(bad.status).toBe(400);
    }, fake.hook);
  });
  it('resolves an unambiguous actor-scoped name alias and refuses ambiguous/foreign/tag lookups', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const first = await startOk(s, { workspace: 'demo', name: 'mine' }, admin, 'alias-1');
      // Exact match resolves show and send (after binding) by name.
      const show = await s.handle(request('/api/sessions/mine', undefined, 'GET'), admin);
      expect(show.status).toBe(200);
      expect(((await show.json()) as any).id).toBe(first.id);
      await bindHttp(s, sql, first.id);
      const send = await s.handle(request('/api/sessions/mine/send', { key: 'alias-send', body: 'hello' }), admin);
      expect(send.status).toBe(201);
      // A native tag is never a name: no adoption through tag guessing.
      expect((await s.handle(request('/api/sessions/' + canonTag(first.fleet_launch_id), undefined, 'GET'), admin)).status).toBe(404);
      // A foreign actor's name never resolves.
      expect((await s.handle(request('/api/sessions/mine', undefined, 'GET'), stranger)).status).toBe(404);
      // A second same-name session makes the alias ambiguous: refused, UUID still works.
      const second = await startOk(s, { workspace: 'demo', name: 'mine' }, admin, 'alias-2');
      expect(second.id).not.toBe(first.id);
      expect((await s.handle(request('/api/sessions/mine', undefined, 'GET'), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${first.id}`, undefined, 'GET'), admin)).status).toBe(200);
      expect((await s.handle(request('/api/sessions/mine/send', { key: 'alias-send-2', body: 'hi' }), admin)).status).toBe(409);
    }, fake.hook);
  });

  describe('observed native session adoption and qualified aliases', () => {
    const NATIVE_DATAOPS_ID = '021d9370-9b3d-4e5a-b59b-256334c08c75';
    const NATIVE_CRM_ID = '60d244c2-02d0-4587-a82b-e172af4cfd0d';
    const SAMPLES = [
      {
        native_id: NATIVE_DATAOPS_ID,
        tag: 'batching',
        workspace: '/home/alexey/git/dataops',
        engine: 'argon',
        phase: 'running',
        cwd: '/home/alexey/git/dataops',
        created_at_ms: 1792257043571,
      },
      {
        native_id: NATIVE_CRM_ID,
        tag: 'crm-redesign',
        workspace: '/home/alexey/git',
        engine: 'codex',
        phase: 'running',
        cwd: '/home/alexey/git',
        created_at_ms: 1792238491217,
      },
    ];

    it('projects a broken worker as uncertain and never as a confirmed stop', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', [{ ...SAMPLES[0], phase: 'broken' }], Date.now());
        const row = sql.exec('SELECT state FROM session_control_sessions WHERE id=?', SAMPLES[0].native_id).one();
        expect(row.state).toBe('uncertain');
      });
    });

    it('preserves legacy adopted native identity, machine and engine across deduplicated polls', async () => {
      await fixture(async (s, sql) => {
        const firstAdopt = seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());
        expect(firstAdopt.adopted).toBe(2);
        expect(firstAdopt.updated).toBe(0);

        // Subsequent poll deduplicates: zero new rows, zero unnecessary writes
        const secondAdopt = seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());
        expect(secondAdopt.adopted).toBe(0);
        expect(secondAdopt.updated).toBe(0);

        // GET /api/sessions lists adopted sessions with correct projection
        const res = await s.handle(request('/api/sessions', undefined, 'GET'), admin);
        expect(res.status).toBe(200);
        const data = await res.json() as any;
        expect(data.sessions.length).toBe(2);

        const dataops = data.sessions.find((r: any) => r.id === NATIVE_DATAOPS_ID);
        expect(dataops).toBeDefined();
        expect(dataops.provenance).toBe('observed_native');
        expect(dataops.controllability).toBe('controllable');
        expect(dataops.workspace).toBe('/home/alexey/git/dataops');
        expect(dataops.qualified_alias).toBe('dataops:batching');
        expect(dataops.engine).toBe('argon');
        expect(dataops.machine).toBe('hetzner');
        expect(dataops).toMatchObject({ state: 'stalled', lifecycle_state: 'running', stalled: true });
        expect(dataops.created_at).toBe(new Date(1792257043571).toISOString());

        const crm = data.sessions.find((r: any) => r.id === NATIVE_CRM_ID);
        expect(crm).toBeDefined();
        expect(crm.provenance).toBe('observed_native');
        expect(crm.controllability).toBe('controllable');
        expect(crm.workspace).toBe('/home/alexey/git');
        expect(crm.qualified_alias).toBe('git:crm-redesign');
        expect(crm.engine).toBe('codex');
        expect(crm.machine).toBe('hetzner');
        expect(crm).toMatchObject({ state: 'stalled', lifecycle_state: 'running', stalled: true });
        expect(crm.created_at).toBe(new Date(1792238491217).toISOString());
      });
    });

    it('resolves sessions by exact qualified alias and canonical native UUID', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Literal colon
        const r1 = await s.handle(request('/api/sessions/dataops:batching', undefined, 'GET'), admin);
        expect(r1.status).toBe(200);
        expect(((await r1.json()) as any).id).toBe(NATIVE_DATAOPS_ID);

        // URL-encoded colon %3A
        const r1Encoded = await s.handle(request('/api/sessions/dataops%3Abatching', undefined, 'GET'), admin);
        expect(r1Encoded.status).toBe(200);
        expect(((await r1Encoded.json()) as any).id).toBe(NATIVE_DATAOPS_ID);

        // git:crm-redesign
        const r2 = await s.handle(request('/api/sessions/git:crm-redesign', undefined, 'GET'), admin);
        expect(r2.status).toBe(200);
        expect(((await r2.json()) as any).id).toBe(NATIVE_CRM_ID);

        // Canonical UUID lookup
        const rUuid = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), admin);
        expect(rUuid.status).toBe(200);
        expect(((await rUuid.json()) as any).id).toBe(NATIVE_DATAOPS_ID);

        // Non-existent alias
        const r404 = await s.handle(request('/api/sessions/dataops:nonexistent', undefined, 'GET'), admin);
        expect(r404.status).toBe(404);
      });
    });

    it('refuses collisions / reused tags with 409 ambiguous_name (no latest-wins)', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Insert a second session with the same workspace basename and tag (e.g. historical / alternative workspace)
        const COLLIDING_ID = '33333333-3333-4333-8333-333333333333';
        const collidingSample = [
          {
            native_id: COLLIDING_ID,
            tag: 'batching',
            workspace: '/srv/other/dataops',
            engine: 'argon',
            phase: 'running',
            created_at_ms: 1792258000000,
          },
        ];
        seedLegacyObservedSessions(sql, 'hetzner', collidingSample, Date.now());

        // Alias resolution refuses ambiguous name honestly
        const res = await s.handle(request('/api/sessions/dataops:batching', undefined, 'GET'), admin);
        expect(res.status).toBe(409);
        expect(((await res.json()) as any).error).toBe('ambiguous_name');

        // Direct canonical UUIDs still work unambiguously
        const r1 = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), admin);
        expect(r1.status).toBe(200);
        const r2 = await s.handle(request(`/api/sessions/${COLLIDING_ID}`, undefined, 'GET'), admin);
        expect(r2.status).toBe(200);
      });
    });

    it('enforces admin human control and denies foreign agent access with 404/403', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Foreign agent cannot see adopted sessions in list
        const agentList = await s.handle(request('/api/sessions', undefined, 'GET'), stranger);
        expect(agentList.status).toBe(200);
        expect(((await agentList.json()) as any).sessions).toEqual([]);

        // Foreign agent cannot resolve by alias
        const agentAlias = await s.handle(request('/api/sessions/dataops:batching', undefined, 'GET'), stranger);
        expect(agentAlias.status).toBe(404);

        // Foreign agent cannot resolve by UUID
        const agentUuid = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), stranger);
        expect(agentUuid.status).toBe(404);

        // Foreign agent cannot enqueue control commands
        const agentSend = await s.handle(request('/api/sessions/dataops:batching/send', { key: 'ag-1', body: 'hi' }), stranger);
        expect([403, 404]).toContain(agentSend.status);
      });
    });

    it('allows send, capture, and stop on adopted session via qualified alias or UUID', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Send via alias
        const sendRes = await s.handle(request('/api/sessions/dataops:batching/send', { key: 'send-1', body: 'execute dataops' }), admin);
        expect(sendRes.status).toBe(201);
        const sendData = await sendRes.json() as any;
        expect(sendData.command_id).toBeDefined();
        expect(sendData.state).toBe('recorded');

        // Capture via alias
        const capRes = await s.handle(request('/api/sessions/dataops:batching/capture', { key: 'cap-1', max_bytes: 1024 }), admin);
        expect(capRes.status).toBe(201);

        // Host receives the offer for the adopted session
        const offersRes = await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'));
        expect(offersRes.status).toBe(200);
        const offersData = await offersRes.json() as any;
        expect(offersData.offers.length).toBeGreaterThanOrEqual(1);
        const sendOffer = offersData.offers.find((o: any) => o.command_id === sendData.command_id);
        expect(sendOffer).toBeDefined();
        expect(sendOffer.binding.native_id).toBe(NATIVE_DATAOPS_ID);
        expect(sendOffer.binding.tag).toBe('batching');
        expect(sendOffer.binding.workspace).toBe('/home/alexey/git/dataops');

        // Stop via alias
        const stopRes = await s.handle(request('/api/sessions/dataops:batching/stop', { key: 'stop-1' }), admin);
        expect(stopRes.status).toBe(201);
      });
    });

    it('preserves absence honestly without deleting or stopping missing sessions', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Next poll only reports crm-redesign (dataops is absent from host poll)
        const nextPoll = [SAMPLES[1]];
        seedLegacyObservedSessions(sql, 'hetzner', nextPoll, Date.now());

        // dataops is NOT deleted, and NOT marked stopped (absence alone is not exit proof)
        const show = await s.handle(request('/api/sessions/dataops:batching', undefined, 'GET'), admin);
        expect(show.status).toBe(200);
        const row = await show.json() as any;
        expect(row).toMatchObject({ state: 'stalled', lifecycle_state: 'running', stalled: true });
      });
    });

    it('protects observed native sessions from automated cleanup', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Mark session as stopped in DB to test eligibility filter
        sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = ?", NATIVE_DATAOPS_ID);

        // Admin apply is refused (admins cannot apply cleanup across foreign sessions)
        const adminCleanup = await s.handle(request('/api/sessions/cleanup', { apply: true }), admin);
        expect(adminCleanup.status).toBe(404);

        // Agent apply does not enqueue observed native sessions
        const agentCleanup = await s.handle(request('/api/sessions/cleanup', { apply: true }), agent('cred-1'));
        expect(agentCleanup.status).toBe(200);
        const cleanupData = await agentCleanup.json() as any;
        expect(cleanupData.enqueued.length).toBe(0);

        // The observed session is intact
        const show = await s.handle(request('/api/sessions/dataops:batching', undefined, 'GET'), admin);
        expect(show.status).toBe(200);
      });
    });

    it('keeps normal Hub-started bindings untouched when adopting native catalog', async () => {
      const fake = fakeReserve();
      await fixture(async (s, sql) => {
        // Start a normal Hub session
        const hubSession = await startOk(s, { workspace: 'demo', name: 'hub-active' }, admin, 'hub-k1');
        expect(hubSession.fleet_launch_id).toBeDefined();

        // Native sessions report includes both new native session and normal sessions
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Hub session retains its fleet_launch_id, name, and canonical_hub provenance
        const checkHub = await s.handle(request(`/api/sessions/${hubSession.id}`, undefined, 'GET'), admin);
        expect(checkHub.status).toBe(200);
        const hubData = await checkHub.json() as any;
        expect(hubData.fleet_launch_id).toBe(hubSession.fleet_launch_id);
        expect(hubData.name).toBe('hub-active');
        expect(hubData.provenance).toBe('canonical_hub');
      }, fake.hook);
    });

    it('validates lineage parent_id, prevents cycles, and projects session_type, parent_id, parent_source, qualified_name', async () => {
      const fake = fakeReserve();
      await fixture(async (s) => {
        // 1. Root session (no parent)
        const root = await startOk(s, { workspace: 'demo', name: 'coordinator' }, admin, 'lineage-root');
        expect(root.parent_id).toBeNull();
        expect(root.parent_source).toBe('unknown_external');
        expect(root.session_type).toBe('headless');
        expect(root.qualified_name).toBe('coordinator');

        // 2. Child session with parent_id
        const childRes = await s.handle(request('/api/sessions', {
          key: 'lineage-child',
          workspace: 'demo',
          name: 'worker-1',
          parent_id: root.id,
        }), admin);
        expect(childRes.status).toBe(201);
        const child = await childRes.json() as any;
        expect(child.parent_id).toBe(root.id);
        expect(child.parent_source).toBe('explicit_parent');
        expect(child.session_type).toBe('headless');

        // 3. Unknown parent -> 404
        const missingRes = await s.handle(request('/api/sessions', {
          key: 'lineage-missing',
          workspace: 'demo',
          parent_id: 'non-existent-uuid',
        }), admin);
        expect(missingRes.status).toBe(404);
        const missingData = await missingRes.json() as any;
        expect(missingData.error).toBe('parent_session_not_found');

        // 4. Non-admin cannot forge parent across unowned sessions -> 403
        const agentAuth = { caller: { kind: 'agent', actor: 'agent:foreign', tokenId: 'tok-foreign' }, scopes: ['fleet:launch'] };
        // (agentLive will check tok-foreign; since tok-foreign is not in credentials table, agent caller gets 403)
        const forgeRes = await s.handle(request('/api/sessions', {
          key: 'lineage-forge',
          workspace: 'demo',
          parent_id: root.id,
        }), agentAuth);
        expect(forgeRes.status).toBe(403);
      }, fake.hook);
    });

    it('serves lineage tree at /api/sessions/tree with hierarchical children', async () => {
      const fake = fakeReserve();
      await fixture(async (s) => {
        const root = await startOk(s, { workspace: 'demo', name: 'root-agent' }, admin, 'tree-root');
        const child1 = await (await s.handle(request('/api/sessions', {
          key: 'tree-c1',
          workspace: 'demo',
          name: 'worker-sub-1',
          parent_id: root.id,
        }), admin)).json() as any;
        const child2 = await (await s.handle(request('/api/sessions', {
          key: 'tree-c2',
          workspace: 'demo',
          name: 'worker-sub-2',
          parent_id: root.id,
        }), admin)).json() as any;

        const treeRes = await s.handle(request('/api/sessions/tree', undefined, 'GET'), admin);
        expect(treeRes.status).toBe(200);
        const treeData = await treeRes.json() as any;
        expect(treeData.total).toBe(3);
        expect(treeData.tree.length).toBe(1);
        expect(treeData.tree[0].id).toBe(root.id);
        expect(treeData.tree[0].children.length).toBe(2);
        const childIds = treeData.tree[0].children.map((c: any) => c.id);
        expect(childIds).toContain(child1.id);
        expect(childIds).toContain(child2.id);
      }, fake.hook);
    });

    it('provides machine-scoped imported bindings read endpoint with pagination', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Unauthenticated call rejected
        expect((await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), unauth)).status).toBe(401);
        // Non-host caller rejected
        expect((await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), stranger)).status).toBe(403);

        // Host on machine 'hetzner' reads imported bindings
        const res = await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), host('hetzner'));
        expect(res.status).toBe(200);
        const data = await res.json() as any;
        expect(data.sessions.length).toBe(2);
        expect(data.sessions.map((x: any) => x.id).sort()).toEqual([NATIVE_CRM_ID, NATIVE_DATAOPS_ID].sort());
        const d0 = data.sessions.find((x: any) => x.id === NATIVE_DATAOPS_ID);
        expect(d0.binding.native_id).toBe(NATIVE_DATAOPS_ID);
        expect(d0.binding.tag).toBe('batching');
        expect(d0.binding.workspace).toBe('/home/alexey/git/dataops');
        expect(d0.binding.native_created_at_ms).toBe(1792257043571);
        expect(d0.binding.provenance).toBe('observed_native');
        expect(d0.engine).toBe('argon');

        // Host on foreign machine 'win35' sees 0 sessions (no cross-machine leak)
        const at = new Date().toISOString();
        const far = new Date(Date.now() + 30 * 86400000).toISOString();
        sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
          VALUES ('host-cred-win35', 'host', 'hash-win35', 'refresh-win35', 'host-family-win35', 'test-win35', 'win35', NULL, '["host"]', 'test', ?, ?)`, at, far);
        sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('win35', 'win35', 'win35', 'host-cred-win35', ?)`, at);

        const foreignRes = await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), host('win35', 'host-cred-win35'));
        expect(foreignRes.status).toBe(200);
        expect((await foreignRes.json() as any).sessions).toEqual([]);

        // Default GET /api/sessions/host-bindings returns pending fleet starts in sessions only (no imported array)
        const defaultRes = await s.handle(request('/api/sessions/host-bindings', undefined, 'GET'), host('hetzner'));
        expect(defaultRes.status).toBe(200);
        const defaultData = await defaultRes.json() as any;
        expect(defaultData.sessions).toEqual([]); // No pending fleet starts
        expect(defaultData.imported).toBeUndefined();

        // Bounded pagination: limit=1
        const page1Res = await s.handle(request('/api/sessions/host-bindings?kind=imported&limit=1', undefined, 'GET'), host('hetzner'));
        const page1 = await page1Res.json() as any;
        expect(page1.sessions.length).toBe(1);
        expect(page1.next_cursor).toBeTruthy();

        const page2Res = await s.handle(request(`/api/sessions/host-bindings?kind=imported&limit=1&cursor=${encodeURIComponent(page1.next_cursor)}`, undefined, 'GET'), host('hetzner'));
        const page2 = await page2Res.json() as any;
        expect(page2.sessions.length).toBe(1);
        expect(page2.sessions[0].id).not.toBe(page1.sessions[0].id);
      });
    });

    it('positively retires imported session from exact authentic tombstone evidence and replays idempotently', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        // Verify initial state
        const initial = await (await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), admin)).json() as any;
        expect(initial).toMatchObject({ state: 'stalled', lifecycle_state: 'running', stalled: true });
        expect(initial.controllability).toBe('controllable');

        const validPayload = {
          native: {
            native_id: NATIVE_DATAOPS_ID,
            tag: 'batching',
            workspace: '/home/alexey/git/dataops',
            created_at_ms: 1792257043571,
            engine: 'argon',
          },
          generation: 1,
          finished_at_ms: 1792257100000,
          cause: 'finished',
          state: 'stopped',
        };

        // 1. Foreign machine caller rejected with 404 (not_found: machine isolation)
        const at = new Date().toISOString();
        const far = new Date(Date.now() + 30 * 86400000).toISOString();
        sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
          VALUES ('host-cred-win35', 'host', 'hash-win35', 'refresh-win35', 'host-family-win35', 'test-win35', 'win35', NULL, '["host"]', 'test', ?, ?)`, at, far);
        sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('win35', 'win35', 'win35', 'host-cred-win35', ?)`, at);

        const foreignRetire = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, validPayload), host('win35', 'host-cred-win35'));
        expect(foreignRetire.status).toBe(404);

        // Admin caller rejected with 403 (host kind only, no admin fallback)
        const adminRetire = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, validPayload), admin);
        expect(adminRetire.status).toBe(403);

        // Removed aliases return 404
        expect((await s.handle(request('/api/sessions/host-imported-bindings', undefined, 'GET'), host('hetzner'))).status).toBe(404);
        expect((await s.handle(request('/api/sessions/retirement', validPayload), host('hetzner'))).status).toBe(404);
        expect((await s.handle(request('/api/sessions/host-retirement', validPayload), host('hetzner'))).status).toBe(404);
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/terminal`, validPayload), host('hetzner'))).status).toBe(404);
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/result`, validPayload), host('hetzner'))).status).toBe(404);

        // 2. Mismatched tuple checks:
        // Wrong tag -> 409
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, native: { ...validPayload.native, tag: 'wrong-tag' } }), host('hetzner'))).status).toBe(409);
        // Wrong workspace -> 409
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, native: { ...validPayload.native, workspace: '/wrong/path' } }), host('hetzner'))).status).toBe(409);
        // Wrong incarnation (created_at_ms) -> 409
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, native: { ...validPayload.native, created_at_ms: 9999999 } }), host('hetzner'))).status).toBe(409);
        // Wrong engine -> 409
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, native: { ...validPayload.native, engine: 'codex' } }), host('hetzner'))).status).toBe(409);
        // Wrong generation -> 409
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, generation: 2 }), host('hetzner'))).status).toBe(409);

        // 3. Evidence / timestamp validation:
        // Cause is not finished -> 400
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, cause: 'killed' }), host('hetzner'))).status).toBe(400);
        // Finished predates creation -> 400
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, finished_at_ms: 1792257000000 }), host('hetzner'))).status).toBe(400);
        // Unknown extra field -> 400
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, unexpected_extra: true }), host('hetzner'))).status).toBe(400);
        expect((await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, tombstone: {} }), host('hetzner'))).status).toBe(400);

        // Negative attempts preserve running custody; these historical
        // samples remain stalled in the inactivity display.
        const stillRunning = await (await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), admin)).json() as any;
        expect(stillRunning).toMatchObject({ state: 'stalled', lifecycle_state: 'running', stalled: true });

        // 4. Valid positive retirement succeeds
        const retireRes = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, validPayload), host('hetzner'));
        expect(retireRes.status).toBe(200);
        const retireData = await retireRes.json() as any;
        expect(retireData.id).toBe(NATIVE_DATAOPS_ID);
        expect(retireData.state).toBe('stopped');
        expect(retireData.replay).toBe(false);

        // Session in database is now stopped and historical_read_only
        const stopped = await (await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}`, undefined, 'GET'), admin)).json() as any;
        expect(stopped.state).toBe('stopped');
        expect(stopped.controllability).toBe('historical_read_only');

        // 5. Idempotent replay: sending the exact same retirement payload again returns 200 with replay: true
        const replayRes = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, validPayload), host('hetzner'));
        expect(replayRes.status).toBe(200);
        const replayData = await replayRes.json() as any;
        expect(replayData.id).toBe(NATIVE_DATAOPS_ID);
        expect(replayData.state).toBe('stopped');
        expect(replayData.replay).toBe(true);

        // Conflicting replays with altered timestamp or cause are refused with 409
        const conflictTimeRes = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, finished_at_ms: validPayload.finished_at_ms + 5000 }), host('hetzner'));
        expect(conflictTimeRes.status).toBe(409);
        expect((await conflictTimeRes.json() as any).error).toBe('retirement_conflict');

        const conflictGenRes = await s.handle(request(`/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, { ...validPayload, generation: 2 }), host('hetzner'));
        expect(conflictGenRes.status).toBe(409);

        // Session does not appear in subsequent GET host-bindings?kind=imported (since it is stopped)
        const updatedList = await (await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), host('hetzner'))).json() as any;
        expect(updatedList.sessions.map((x: any) => x.id)).toEqual([NATIVE_CRM_ID]);
      });
    });

    it('rechecks binding, generation, and host authority inside mutation txn after streamed body yields', async () => {
      await fixture(async (s, sql) => {
        seedLegacyObservedSessions(sql, 'hetzner', SAMPLES, Date.now());

        const validPayload = {
          native: {
            native_id: NATIVE_DATAOPS_ID,
            tag: 'batching',
            workspace: '/home/alexey/git/dataops',
            created_at_ms: 1792257043571,
            engine: 'argon',
          },
          generation: 1,
          finished_at_ms: 1792257100000,
          cause: 'finished',
          state: 'stopped',
        };

        // Case A: Generation mutated while body streamed
        let controller1!: ReadableStreamDefaultController<Uint8Array>;
        const stream1 = new ReadableStream<Uint8Array>({ start(v) { controller1 = v; } });
        const pending1 = s.handle(new Request(`https://hub.test/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, {
          method: 'POST',
          body: stream1,
        }), host('hetzner'));

        await Promise.resolve();
        // Mutate stored generation in database before body resolves
        const currentB = JSON.parse(sql.exec('SELECT binding_json FROM session_control_sessions WHERE id = ?', NATIVE_DATAOPS_ID).toArray()[0].binding_json);
        currentB.generation = 2;
        sql.exec('UPDATE session_control_sessions SET binding_json = ? WHERE id = ?', JSON.stringify(currentB), NATIVE_DATAOPS_ID);

        controller1.enqueue(new TextEncoder().encode(JSON.stringify(validPayload)));
        controller1.close();

        const res1 = await pending1;
        expect(res1.status).toBe(409);
        expect((await res1.json() as any).error).toBe('generation_mismatch');

        // Replacement / live session stays running
        const row1 = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', NATIVE_DATAOPS_ID).toArray()[0];
        expect(row1.state).toBe('running');

        // Case B: Host credential revoked while body streamed
        currentB.generation = 1;
        sql.exec('UPDATE session_control_sessions SET binding_json = ? WHERE id = ?', JSON.stringify(currentB), NATIVE_DATAOPS_ID);

        let controller2!: ReadableStreamDefaultController<Uint8Array>;
        const stream2 = new ReadableStream<Uint8Array>({ start(v) { controller2 = v; } });
        const pending2 = s.handle(new Request(`https://hub.test/api/sessions/${NATIVE_DATAOPS_ID}/retirement`, {
          method: 'POST',
          body: stream2,
        }), host('hetzner'));

        await Promise.resolve();
        // Revoke host credential
        sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'host-cred-1'", new Date().toISOString());

        controller2.enqueue(new TextEncoder().encode(JSON.stringify(validPayload)));
        controller2.close();

        const res2 = await pending2;
        expect(res2.status).toBe(409);
        expect((await res2.json() as any).error).toBe('credential_changed');

        // Session stays live
        const row2 = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', NATIVE_DATAOPS_ID).toArray()[0];
        expect(row2.state).toBe('running');
      });
    });

    it('computes pagination continuation across raw rows even when a page is filtered empty', async () => {
      await fixture(async (s, sql) => {
        // Create 3 sessions on hetzner:
        // Session 1: valid observed_native
        // Session 2: invalid binding (incomplete, non-observed_native provenance)
        // Session 3: valid observed_native
        const at = new Date(1792257000000).toISOString();
        const id1 = '00000000-0000-4000-8000-000000000001';
        const id2 = '00000000-0000-4000-8000-000000000002';
        const id3 = '00000000-0000-4000-8000-000000000003';

        sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
          VALUES (?, 'unknown', 'k1', '{}', 'hetzner', 'codex', 'headless', 'running', ?, '2026-10-18T10:00:00.000Z', '2026-10-18T10:00:00.000Z')`,
          id1, JSON.stringify({ native_id: id1, tag: 't1', workspace: '/ws/1', native_created_at_ms: 1000, generation: 1, provenance: 'observed_native' }));

        // Incomplete/non-observed row that will be filtered out by hostOk or provenance
        sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
          VALUES (?, 'unknown', 'k2', '{}', 'hetzner', 'codex', 'headless', 'running', ?, '2026-10-18T10:00:01.000Z', '2026-10-18T10:00:01.000Z')`,
          id2, JSON.stringify({ native_id: id2, tag: 't2', workspace: '/ws/2', provenance: 'unsupported_external' }));

        sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
          VALUES (?, 'unknown', 'k3', '{}', 'hetzner', 'codex', 'headless', 'running', ?, '2026-10-18T10:00:02.000Z', '2026-10-18T10:00:02.000Z')`,
          id3, JSON.stringify({ native_id: id3, tag: 't3', workspace: '/ws/3', native_created_at_ms: 3000, generation: 1, provenance: 'observed_native' }));

        // Limit=1: Page 1 returns Session 1 with cursor pointing to Session 1
        const p1 = await (await s.handle(request('/api/sessions/host-bindings?kind=imported&limit=1', undefined, 'GET'), host('hetzner'))).json() as any;
        expect(p1.sessions.length).toBe(1);
        expect(p1.sessions[0].id).toBe(id1);
        expect(p1.next_cursor).toBeTruthy();

        // Page 2: with cursor=p1.next_cursor & limit=1. Raw scanned row is id2, which is filtered out.
        // Sessions array is empty, BUT next_cursor is returned!
        const p2 = await (await s.handle(request(`/api/sessions/host-bindings?kind=imported&limit=1&cursor=${encodeURIComponent(p1.next_cursor)}`, undefined, 'GET'), host('hetzner'))).json() as any;
        expect(p2.sessions.length).toBe(0);
        expect(p2.next_cursor).toBeTruthy();

        // Page 3: with cursor=p2.next_cursor & limit=1. Returns Session 3!
        const p3 = await (await s.handle(request(`/api/sessions/host-bindings?kind=imported&limit=1&cursor=${encodeURIComponent(p2.next_cursor)}`, undefined, 'GET'), host('hetzner'))).json() as any;
        expect(p3.sessions.length).toBe(1);
        expect(p3.sessions[0].id).toBe(id3);
      });
    });

    it('rejects incomplete stored bindings and protects legacy unknown sessions', async () => {
      await fixture(async (s, sql) => {
        const id = '00000000-0000-4000-8000-000000000099';
        // Incomplete stored binding: missing generation and native_created_at_ms
        sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
          VALUES (?, 'unknown', 'legacy-key', '{}', 'hetzner', 'codex', 'headless', 'running', ?, '2026-10-18T10:00:00.000Z', '2026-10-18T10:00:00.000Z')`,
          id, JSON.stringify({ native_id: id, tag: 'legacy', workspace: '/legacy', provenance: 'observed_native' }));

        // Not returned in GET imported host-bindings
        const list = await (await s.handle(request('/api/sessions/host-bindings?kind=imported', undefined, 'GET'), host('hetzner'))).json() as any;
        expect(list.sessions.map((x: any) => x.id)).not.toContain(id);

        // POST retirement returns 409 stored_binding_incomplete
        const payload = {
          native: { native_id: id, tag: 'legacy', workspace: '/legacy', created_at_ms: 1000, engine: 'codex' },
          generation: 1,
          finished_at_ms: 2000,
          cause: 'finished',
          state: 'stopped',
        };
        const res = await s.handle(request(`/api/sessions/${id}/retirement`, payload), host('hetzner'));
        expect(res.status).toBe(409);
        expect((await res.json() as any).error).toBe('stored_binding_incomplete');

        // Session remains untouched
        const row = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', id).toArray()[0];
        expect(row.state).toBe('running');
      });
    });

    it('refuses to retire Hub-started sessions through the imported retirement route', async () => {
      const fake = fakeReserve();
      await fixture(async (s, sql) => {
        const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'hub-started-1');
        await bindHttp(s, sql, session.id);

        const hubPayload = {
          native: {
            native_id: NATIVE_ID,
            tag: canonTag('launch-1'),
            workspace: NATIVE_WS,
            created_at_ms: NATIVE_CREATED,
            engine: 'codex',
          },
          generation: 1,
          finished_at_ms: NATIVE_CREATED + 50000,
          cause: 'finished',
          state: 'stopped',
        };

        const res = await s.handle(request(`/api/sessions/${session.id}/retirement`, hubPayload), host('hetzner'));
        expect(res.status).toBe(409);
        expect((await res.json() as any).error).toBe('session_not_imported');

        // Hub session state is untouched
        const view = await (await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json() as any;
        expect(view.state).toBe('running');
      }, fake.hook);
    });
  });
});

describe('held placement recovery', () => {
  it('retries the original legacy intent after capacity recovers without duplicating it', async () => {
    let available = false;
    const placed = fakeReserve();
    await fixture(async (s, sql) => {
      const input = { key: 'recover-held', workspace: 'demo', engine: 'codex', engine_override_reason: 'Synthetic override for held recovery test.' };
      const held = await (await s.handle(request('/api/sessions', input), admin)).json() as any;
      expect(held.state).toBe('held');
      const saved = sql.exec('SELECT request_json FROM session_control_sessions WHERE id = ?', held.id).toArray()[0];
      const legacy = JSON.parse(saved.request_json); delete legacy.parent_id;
      sql.exec('UPDATE session_control_sessions SET request_json = ? WHERE id = ?', JSON.stringify(legacy), held.id);
      available = true;
      const retry = await s.handle(request(`/api/sessions/${held.id}/retry`, {}), admin);
      const recovered = await retry.json() as any;
      expect(recovered.id).toBe(held.id);
      expect(recovered.state).toBe('offered');
      expect(sql.exec('SELECT id FROM session_control_sessions').toArray()).toHaveLength(1);
      expect(placed.calls).toHaveLength(1);
      expect((await s.handle(request('/api/sessions', { ...input, workspace: 'changed' }), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${held.id}/retry`, {}), admin)).status).toBe(409);
      expect(placed.calls).toHaveLength(1);
    }, input => available ? placed.hook(input) : { status: 409, value: { error: 'account_capacity' } });
  });
  it('refuses another actor and refuses payload changes', async () => {
    await fixture(async s => {
      const held = await (await s.handle(request('/api/sessions', { key: 'held-private', workspace: 'demo' }), admin)).json() as any;
      expect((await s.handle(request(`/api/sessions/${held.id}/retry`, {}), stranger)).status).toBe(404);
      expect((await s.handle(request(`/api/sessions/${held.id}/retry`, { workspace: 'other' }), admin)).status).toBe(400);
    }, () => ({ status: 409, value: { error: 'account_capacity' } }));
  });
});

describe('unbound Fleet recovery', () => {
  it('reaps old null-machine intents from terminal Fleet receipts without retiring uncertain starts', () => fixture(async (s, sql) => {
    for (const [fleetState, expected] of [['failed', 'failed'], ['expired', 'failed'], ['cancelled', 'cancelled'], ['exited', 'stopped'], ['uncertain', 'queued']]) {
      const start = await startOk(s, {}, admin);
      ensureFleetLaunch(sql, start.fleet_launch_id, 'hetzner', null, fleetState);
      sql.exec("UPDATE session_control_sessions SET state = 'queued', machine_id = NULL, engine = NULL WHERE id = ?", start.id);
      const list = await (await s.handle(request('/api/sessions', undefined, 'GET'), admin)).json() as any;
      expect(list.sessions.find((r: any) => r.id === start.id).state).toBe(expected);
      expect((await (await s.handle(request('/api/sessions/' + start.id, undefined, 'GET'), admin)).json() as any).state).toBe(fleetState === 'uncertain' ? 'uncertain' : expected);
    }
  }, fakeReserve().hook));

  it('resolves a Fleet UUID to the owned canonical session for control without granting foreign access', () => fixture(async (s, sql) => {
    const start = await startOk(s, {}, agent());
    const launchId = crypto.randomUUID();
    sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ? WHERE id = ?', launchId, start.id);
    ensureFleetLaunch(sql, launchId);
    const shown = await (await s.handle(request('/api/sessions/' + launchId, undefined, 'GET'), agent())).json() as any;
    expect(shown.id).toBe(start.id);
    reportFleetSession(sql, 'hetzner', NATIVE_ID, canonTag(launchId), NATIVE_WS);
    expect(s.recordBinding(start.id, bound({ tag: canonTag(launchId) })).ok).toBe(true);
    const stopped = await s.handle(request('/api/sessions/' + launchId + '/stop', { key: 'stop-by-launch' }), agent());
    expect(stopped.status).toBe(201);
    expect(sql.exec('SELECT session_id FROM session_control_commands').one().session_id).toBe(start.id);
    expect((await s.handle(request('/api/sessions/' + launchId, undefined, 'GET'), stranger)).status).toBe(404);
    expect((await s.handle(request('/api/sessions/' + launchId + '/binding', bound(canonTag(launchId))), host('hetzner'))).status).toBe(404);
  }, fakeReserve().hook));

  it('cancels an unclaimed offer atomically and replays the receipt', () => fixture(async (s, sql) => {
    const start = await startOk(s, {}, agent());
    ensureFleetLaunch(sql, start.fleet_launch_id, 'hetzner', null, 'offered');
    const path = '/api/sessions/' + start.id + '/cancel';
    const result = await (await s.handle(request(path, { key: 'cancel-offer' }), agent())).json() as any;
    expect(result.state).toBe('cancelled');
    expect(result.cancellation.effect).toBe('no_native_start');
    expect(sql.exec('SELECT state, verification_status FROM fleet_launches WHERE id = ?', start.fleet_launch_id).one()).toMatchObject({ state: 'cancelled', verification_status: 'not_started' });
    expect((await (await s.handle(request(path, { key: 'cancel-offer' }), agent())).json() as any).replay).toBe(true);
  }, fakeReserve().hook));

  it('refuses a cancellation when a claim wins while the body is streaming', () => fixture(async (s, sql) => {
    const start = await startOk(s, {}, agent());
    ensureFleetLaunch(sql, start.fleet_launch_id, 'hetzner', null, 'offered');
    let controller: ReadableStreamDefaultController;
    const stream = new ReadableStream({ start(c) { controller = c; } });
    const pending = s.handle(new Request('https://hub.test/api/sessions/' + start.id + '/cancel', { method: 'POST', body: stream }), agent());
    await Promise.resolve();
    sql.exec("UPDATE fleet_launches SET state = 'starting' WHERE id = ?", start.fleet_launch_id);
    controller!.enqueue(new TextEncoder().encode('{"key":"racing-claim"}')); controller!.close();
    expect((await pending).status).toBe(409);
    expect(sql.exec('SELECT * FROM session_control_cancellations').toArray()).toHaveLength(0);
  }, fakeReserve().hook));
});

describe('owned held intent cancellation', () => {
  const start = { key: 'held-cancel-start', name: 'held-fixture', workspace: 'demo', engine: 'codex', engine_override_reason: 'Synthetic override for held cancel test.', mode: 'headless', brief: 'synthetic cancellation fixture' };

  it('stops a never-started held intent by atomic cancellation and replays safely', async () => {
    const reserve = fakeReserve({ status: 409, error: 'no_eligible_route', rejected: [] });
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      const path = '/api/sessions/' + held.id + '/stop';
      const value = await (await s.handle(request(path, { key: 'stop-held' }), agent())).json() as any;
      expect(value.state).toBe('cancelled');
      expect(value.cancellation.effect).toBe('no_native_start');
      expect((await (await s.handle(request(path, { key: 'stop-held' }), agent())).json() as any).replay).toBe(true);
      expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(0);
      expect((await s.handle(request('/api/sessions/' + held.id + '/retry', {}), agent())).status).toBe(409);
    }, reserve.hook);
  });

  it('allows an explicit human operator to stop another owner held intent while cancel stays owner-only', async () => {
    const reserve = fakeReserve({ status: 409, error: 'no_eligible_route', rejected: [] });
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      const operator = { caller: { kind: 'human', isAdmin: true, actor: 'human:admin-token:synthetic-operator' }, scopes: ['admin'] };
      const path = '/api/sessions/' + held.id;
      expect((await s.handle(request(path + '/cancel', { key: 'cancel-other' }), operator)).status).toBe(404);
      expect((await s.handle(request(path + '/stop', { key: 'stop-other' }), stranger)).status).toBe(404);
      expect((await s.handle(request(path + '/stop', { key: 'stop-other' }), admin)).status).toBe(404);
      const value = await (await s.handle(request(path + '/stop', { key: 'stop-other' }), operator)).json() as any;
      expect(value.state).toBe('cancelled');
      expect(value.cancellation.effect).toBe('no_native_start');
      expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(0);
    }, reserve.hook);
  });

  it('records no-start cancellation, replays it, and fences both original start and retry', async () => {
    const reserve = fakeReserve({ status: 409, error: 'no_capacity' });
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      expect(held.state).toBe('held');
      const path = '/api/sessions/' + held.id + '/cancel';
      const named = '/api/sessions/held-fixture/cancel';
      const first = await s.handle(request(named, { key: 'cancel-once' }), agent());
      const value = await first.json() as any;
      expect(first.status).toBe(200);
      expect(value.state).toBe('cancelled');
      expect(value.cancellation.effect).toBe('no_native_start');
      expect(value.binding).toBeNull();
      expect(value.fleet_launch_id).toBeNull();
      const replay = await (await s.handle(request(path, { key: 'cancel-once' }), agent())).json() as any;
      expect(replay.cancellation).toEqual(value.cancellation);
      expect(replay.replay).toBe(true);
      expect((await s.handle(request(path, { key: 'different' }), agent())).status).toBe(409);
      const original = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      expect(original.state).toBe('cancelled');
      expect((await s.handle(request('/api/sessions/' + held.id + '/retry', {}), agent())).status).toBe(409);
      expect(reserve.calls.length).toBe(1);
      expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(0);
    }, reserve.hook);
  });

  it('refuses Fleet placement, native binding, commands, and non-held states', async () => {
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      const path = '/api/sessions/' + held.id + '/cancel';
      for (const patch of [
        { field: 'fleet_launch_id', value: 'real-placement-id' },
        { field: 'binding_json', value: JSON.stringify(bound()) },
        { field: 'state', value: 'uncertain' },
      ]) {
        sql.exec(`UPDATE session_control_sessions SET ${patch.field} = ? WHERE id = ?`, patch.value, held.id);
        expect((await s.handle(request(path, { key: 'cancel' }), agent())).status).toBe(409);
        sql.exec(`UPDATE session_control_sessions SET ${patch.field} = ? WHERE id = ?`, patch.field === 'state' ? 'held' : null, held.id);
      }
      const at = new Date().toISOString();
      sql.exec("INSERT INTO session_control_commands (id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at) VALUES ('existing',?,'stop','demo:assistant','existing','{}','uncertain',0,'existing',?,?)", held.id, at, at);
      expect((await s.handle(request(path, { key: 'cancel' }), agent())).status).toBe(409);
      expect(sql.exec('SELECT * FROM session_control_cancellations').toArray()).toHaveLength(0);
    });
  });

  it('requires the current owning actor, including for admins and replay', async () => {
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      const path = '/api/sessions/' + held.id + '/cancel';
      expect((await s.handle(request(path, { key: 'cancel' }), admin)).status).toBe(404);
      expect((await s.handle(request(path, { key: 'cancel' }), stranger)).status).toBe(404);
      expect((await s.handle(request(path, { key: 'cancel', native_id: NATIVE_ID }), agent())).status).toBe(400);
      expect((await s.handle(request(path, { key: 'cancel' }), agent())).status).toBe(200);
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred-1'", new Date().toISOString());
      expect((await s.handle(request(path, { key: 'cancel' }), agent())).status).toBe(404);
    });
  });

  it('rechecks placement after a streamed cancellation body yields', async () => {
    let capacity = false;
    let placements = 0;
    const reserve = () => capacity
      ? { value: { id: 'placed-after-body-yield', state: 'offered', machine: 'hetzner', engine: 'codex' }, status: (++placements, 201) }
      : { value: { error: 'no_capacity' }, status: 409 };
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      let controller!: ReadableStreamDefaultController<Uint8Array>;
      const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
      const cancellation = s.handle(new Request('https://hub.test/api/sessions/' + held.id + '/cancel', { method: 'POST', body: stream }), agent());
      await Promise.resolve();
      capacity = true;
      expect((await s.handle(request('/api/sessions/' + held.id + '/retry', {}), agent())).status).toBe(200);
      controller.enqueue(new TextEncoder().encode('{"key":"cancel-after-yield"}'));
      controller.close();
      expect((await cancellation).status).toBe(409);
      expect(placements).toBe(1);
      expect(sql.exec('SELECT * FROM session_control_cancellations').toArray()).toHaveLength(0);
    }, reserve);
  });

  it('rechecks revoked credentials after a streamed cancellation body yields', async () => {
    await fixture(async (s, sql) => {
      const held = await (await s.handle(request('/api/sessions', start), agent())).json() as any;
      let controller!: ReadableStreamDefaultController<Uint8Array>;
      const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
      const cancellation = s.handle(new Request('https://hub.test/api/sessions/' + held.id + '/cancel', { method: 'POST', body: stream }), agent());
      await Promise.resolve();
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred-1'", new Date().toISOString());
      controller.enqueue(new TextEncoder().encode('{"key":"revoked-after-yield"}'));
      controller.close();
      expect((await cancellation).status).toBe(404);
      expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', held.id).toArray()[0].state).toBe('held');
    });
  });
});


describe('held cancellation current human bearer authority', () => {
  const auth = { caller: { kind: 'human', isAdmin: true, actor: 'human:cancel-human', tokenId: 'human-cancel-cred' }, scopes: ['admin'] };
  async function seed(sql: any) {
    const now = new Date().toISOString();
    sql.exec('INSERT INTO users (sub,email,is_admin,first_seen_at,last_seen_at) VALUES (?, ?, 1, ?, ?)', 'cancel-human', 'admin@fake.local', now, now);
    sql.exec(`INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
      VALUES ('human-cancel-cred','human','human-cancel-hash','human-cancel-family','admin@fake.local','human:cancel-human','["admin"]','cancel-human',?,?)`, now, new Date(Date.now()+3600000).toISOString());
  }
  it('accepts a current human bearer and refuses a collapsed browser admin identity', async () => {
    await fixture(async (s, sql) => {
      await seed(sql);
      const held = await (await s.handle(request('/api/sessions', {key:'human-start',workspace:'demo',mode:'interactive'}), auth)).json() as any;
      expect((await s.handle(request('/api/sessions/'+held.id+'/cancel',{key:'human-cancel'}), auth)).status).toBe(200);
      const browserHeld = await (await s.handle(request('/api/sessions', {key:'browser-start',workspace:'demo',mode:'interactive'}), admin)).json() as any;
      expect((await s.handle(request('/api/sessions/'+browserHeld.id+'/cancel',{key:'browser-cancel'}), admin)).status).toBe(404);
    }, undefined, {env: env as unknown as Env});
  });
  it('refuses human revocation, expiry, disabled user, changed email and removed allowlist after body streaming', async () => {
    for (const change of ['revoke','expire','disable','email','allowlist']) {
      const bindings = {...env, HUB_ADMINS: 'admin@fake.local'} as unknown as Env;
      await fixture(async (s, sql) => {
        await seed(sql);
        const held = await (await s.handle(request('/api/sessions', {key:'human-start',workspace:'demo',mode:'interactive'}), auth)).json() as any;
        let controller!: ReadableStreamDefaultController<Uint8Array>;
        const stream = new ReadableStream<Uint8Array>({start(value) {controller=value;}});
        const pending = s.handle(new Request('https://hub.test/api/sessions/'+held.id+'/cancel', {method:'POST',body:stream}), auth);
        await Promise.resolve();
        if (change==='revoke') sql.exec("UPDATE credentials SET revoked_at=? WHERE id='human-cancel-cred'", new Date().toISOString());
        if (change==='expire') sql.exec("UPDATE credentials SET expires_at=? WHERE id='human-cancel-cred'", new Date(0).toISOString());
        if (change==='disable') sql.exec("UPDATE users SET is_admin=0 WHERE sub='cancel-human'");
        if (change==='email') sql.exec("UPDATE users SET email='changed@example.com' WHERE sub='cancel-human'");
        if (change==='allowlist') { bindings.HUB_ADMINS=''; bindings.HUB_ADDITIONAL_ADMINS=''; }
        controller.enqueue(new TextEncoder().encode('{"key":"human-cancel"}'));controller.close();
        expect((await pending).status, change).toBe(404);
        expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?', held.id).toArray()[0].state).toBe('held');
      }, undefined, {env:bindings});
    }
  });
});


describe('held cancellation current agent binding and scope', () => {
  it('refuses scope removal after body streaming', async () => {
    for (const change of ['scopes']) {
      await fixture(async (s, sql) => {
        const held = await (await s.handle(request('/api/sessions', {key:'agent-held',workspace:'demo',mode:'interactive'}), agent())).json() as any;
        let controller!: ReadableStreamDefaultController<Uint8Array>;
        const stream = new ReadableStream<Uint8Array>({start(value) {controller=value;}});
        const pending = s.handle(new Request('https://hub.test/api/sessions/'+held.id+'/cancel', {method:'POST',body:stream}), agent());
        await Promise.resolve();
        if (change==='scopes') sql.exec("UPDATE credentials SET scopes='[]' WHERE id='cred-1'");
        controller.enqueue(new TextEncoder().encode('{"key":"agent-cancel"}'));controller.close();
        expect((await pending).status,change).toBe(404);
        expect(sql.exec('SELECT state FROM session_control_sessions WHERE id=?',held.id).toArray()[0].state).toBe('held');
      });
    }
  });
});

describe('bounded command custody recovery', () => {
  it('requeues expired claims, preserves the execution fence, and fails after three leases even without a claimant', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      const cmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'retry', body: 'go' }), admin)).json();
      const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const offer = poll.offers[0];
      const first = await claimAs(s, cmd.command_id, offer.correlation);
      expect(Date.parse(first.expires_at) - Date.now()).toBeLessThanOrEqual(60000);
      const expire = () => sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?', new Date(Date.now() - 1).toISOString(), cmd.command_id);
      const commands = async () => (await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any).commands;
      expire();
      const requeued = (await commands())[0];
      expect(requeued.state).toBe('recorded');
      expect(requeued.attempts).toBe(2);
      const next: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(next.offers[0].command_id).toBe(cmd.command_id);
      const rival = await s.handle(request(`/api/session-commands/${cmd.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT2 }), host('hetzner'));
      expect((await rival.json() as any).error).toBe('retry_requires_original_attempt');
      const second = await claimAs(s, cmd.command_id, offer.correlation);
      expect(second.claim_token).toBe(first.claim_token);
      expect(second.attempt).toBe(2);
      // Host polling also recovers a lease; a dead original claimant cannot
      // strand its retained retry in transported forever.
      expire();
      const retryPoll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(retryPoll.offers[0].command_id).toBe(cmd.command_id);
      expect((await commands())[0].attempts).toBe(3);
      expire();
      const failed = (await commands())[0];
      expect(failed.state).toBe('failed');
      expect(failed.ack.reason).toBe('claim_expired_retry_exhausted');
      expect((await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any).offers).toEqual([]);
      expect((await s.handle(request(`/api/session-commands/${cmd.command_id}/claim`, { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'))).status).toBe(409);
    }, fakeReserve().hook);
  });

  it('accepts a proven send within 60 seconds and fails unsupported headless input explicitly', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      for (const [key, outcome, reason, expected] of [
        ['supported', 'delivered', 'verified reply', 'acked'],
        ['exec', 'refused', 'send_unsupported_headless', 'failed'],
      ]) {
        const started = Date.now();
        await s.handle(request(`/api/sessions/${session.id}/send`, { key, body: 'go' }), admin);
        const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
        const offer = poll.offers[0];
        expect(offer.mode).toBe('headless');
        const grant = await claimAs(s, offer.command_id, offer.correlation);
        const ack = await s.handle(request(`/api/session-commands/${offer.command_id}/ack`, {
          correlation: offer.correlation, claim_token: grant.claim_token, native: native(), outcome, reason,
          ...(outcome === 'delivered' ? { proof: goodProof() } : {}),
        }), host('hetzner'));
        expect(ack.status).toBe(200);
        expect((await ack.json() as any).state).toBe(expected);
        expect(Date.now() - started).toBeLessThan(60000);
      }
    }, fakeReserve().hook);
  });

  it('reports inbox status for undelivered sends and fails expired claims with recipient_not_ready', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      const cmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'inbox-status', body: 'hello recipient' }), admin)).json();
      const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const offer = poll.offers[0];
      const claim = await claimAs(s, cmd.command_id, offer.correlation);

      // Host reports the send was recorded into aplexer inbox, waiting for recipient
      const statusRes = await s.handle(request(`/api/session-commands/${cmd.command_id}/status`, {
        claim_token: claim.claim_token,
        status: 'waiting in inbox for recipient',
        reason: 'aplexer inbox recorded; recipient has not acknowledged',
      }), host('hetzner'));
      expect(statusRes.status).toBe(200);

      // Commands listing projects "waiting in inbox for recipient" instead of claimed
      const commandsList: any = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json();
      const listed = commandsList.commands.find((c: any) => c.id === cmd.command_id);
      expect(listed.state).toBe('waiting in inbox for recipient');
      expect(listed.host_status).toBe('waiting in inbox for recipient');
      expect(listed.reason).toBe('aplexer inbox recorded; recipient has not acknowledged');

      // When the claim expires while stored in inbox: fail with recipient_not_ready (no re-queue)
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?', new Date(Date.now() - 1000).toISOString(), cmd.command_id);
      // Trigger sweep via offers polling
      const sweepOffers: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(sweepOffers.offers).toEqual([]); // not re-offered

      const afterExpiry: any = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json();
      const expiredCmd = afterExpiry.commands.find((c: any) => c.id === cmd.command_id);
      expect(expiredCmd.state).toBe('failed');
      expect(expiredCmd.ack.outcome).toBe('failed');
      expect(expiredCmd.ack.reason).toBe('recipient_not_ready');
    }, fakeReserve().hook);
  });

  it('expires a delivered-awaiting-reply claim to uncertain and promotes the late correlated reply to acked', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      const cmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'await-reply', body: 'hello recipient' }), admin)).json();
      const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const offer = poll.offers[0];
      const claim = await claimAs(s, cmd.command_id, offer.correlation);

      // Host reports the effect already landed; only the reply proof is pending.
      const statusRes = await s.handle(request(`/api/session-commands/${cmd.command_id}/status`, {
        claim_token: claim.claim_token,
        status: 'delivered; awaiting recipient reply',
        reason: 'pane/inbox delivered; correlated reply proof pending',
      }), host('hetzner'));
      expect(statusRes.status).toBe(200);

      // Claim expiry must NOT take the absence-class recipient_not_ready path:
      // the reply is still in flight, so uncertain keeps lateReplyAck available.
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?', new Date(Date.now() - 1000).toISOString(), cmd.command_id);
      const sweepOffers: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(sweepOffers.offers).toEqual([]); // not re-offered, never re-delivered

      const afterExpiry: any = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json();
      const expiredCmd = afterExpiry.commands.find((c: any) => c.id === cmd.command_id);
      expect(expiredCmd.state).toBe('uncertain');
      expect(expiredCmd.ack.outcome).toBe('uncertain');
      expect(expiredCmd.ack.reason).toBe('recipient_reply_pending');

      // The late correlated reply promotes the uncertain send to acked.
      const late = await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`, {
        correlation: offer.correlation, claim_token: claim.claim_token,
        native: native(), outcome: 'delivered',
        reason: 'correlated native reply verified in message log', proof: goodProof(),
      }), host('hetzner'));
      expect(late.status).toBe(200);
      expect((await late.json() as any).state).toBe('acked');
    }, fakeReserve().hook);
  });

  it('warns or refuses sends when target has no recent reported state', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);

      // Default send warns if no reported state exists in fleet_sessions
      const sendWarn = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'warn-send', body: 'hi' }), admin);
      expect(sendWarn.status).toBe(201);
      const warnData: any = await sendWarn.json();
      expect(warnData.warning).toBe('target_no_reported_state');

      // Send with require_ready: true refuses when no reported state exists
      const sendRefuse = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'refuse-send', body: 'hi', require_ready: true }), admin);
      expect(sendRefuse.status).toBe(409);
      const refuseData: any = await sendRefuse.json();
      expect(refuseData.error).toBe('target_not_ready');

      // Once state is reported in fleet_sessions, send succeeds without warning
      sql.exec("UPDATE fleet_sessions SET agent_state = 'idle' WHERE native_id = ?", NATIVE_ID);
      const sendOk = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'ok-send', body: 'hi', require_ready: true }), admin);
      expect(sendOk.status).toBe(201);
      const okData: any = await sendOk.json();
      expect(okData.warning).toBeUndefined();

      // AC4 recency: a stale report warns/refuses again (observed_at > 5 min old)
      sql.exec("UPDATE fleet_sessions SET observed_at = ? WHERE native_id = ?", new Date(Date.now() - 10 * 60 * 1000).toISOString(), NATIVE_ID);
      const sendStaleWarn = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'stale-warn', body: 'hi' }), admin);
      expect(sendStaleWarn.status).toBe(201);
      expect(((await sendStaleWarn.json()) as any).warning).toBe('target_no_reported_state');
      const sendStaleRefuse = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'stale-refuse', body: 'hi', require_ready: true }), admin);
      expect(sendStaleRefuse.status).toBe(409);
      expect(((await sendStaleRefuse.json()) as any).error).toBe('target_not_ready');
    }, fakeReserve().hook);
  });

  it('accepts fenced failed inbox receipt and replays idempotent failure', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      sql.exec("UPDATE fleet_sessions SET agent_state = 'idle' WHERE native_id = ?", NATIVE_ID);
      const cmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'fail-ack', body: 'hi' }), admin)).json();
      const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const offer = poll.offers[0];
      const claim = await claimAs(s, cmd.command_id, offer.correlation);
      // Host reports inbox waiting, then reports proven cancellation as failed.
      const statusRes = await s.handle(request(`/api/session-commands/${cmd.command_id}/status`, {
        claim_token: claim.claim_token, status: 'waiting in inbox for recipient', reason: 'inbox recorded',
      }), host('hetzner'));
      expect(statusRes.status).toBe(200);
      const ackRes = await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`, {
        correlation: offer.correlation, claim_token: claim.claim_token,
        native: native(canonTag('launch-1')), outcome: 'failed', reason: 'recipient_not_ready',
      }), host('hetzner'));
      expect(ackRes.status).toBe(200);
      expect(((await ackRes.json()) as any).state).toBe('failed');
      // Idempotent retry with the same correlation + reason replays success.
      const ackRetry = await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`, {
        correlation: offer.correlation, claim_token: claim.claim_token,
        native: native(canonTag('launch-1')), outcome: 'failed', reason: 'recipient_not_ready',
      }), host('hetzner'));
      expect(ackRetry.status).toBe(200);
      expect(((await ackRetry.json()) as any).state).toBe('failed');
    }, fakeReserve().hook);
  });

  it('accepts fenced terminal failure after claim expiry instead of burning leases', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      sql.exec("UPDATE fleet_sessions SET agent_state = 'idle' WHERE native_id = ?", NATIVE_ID);
      const cmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'fenced-expiry', body: 'hi' }), admin)).json();
      const poll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const offer = poll.offers[0];
      const claim = await claimAs(s, cmd.command_id, offer.correlation);
      await s.handle(request(`/api/session-commands/${cmd.command_id}/status`, {
        claim_token: claim.claim_token, status: 'waiting in inbox for recipient', reason: 'inbox recorded',
      }), host('hetzner'));
      // Burn the lease, then deliver the fenced terminal receipt on the same claim.
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?', new Date(Date.now() - 1000).toISOString(), cmd.command_id);
      const ackRes = await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`, {
        correlation: offer.correlation, claim_token: claim.claim_token,
        native: native(canonTag('launch-1')), outcome: 'failed', reason: 'recipient_not_ready',
      }), host('hetzner'));
      expect(ackRes.status).toBe(200);
      expect(((await ackRes.json()) as any).state).toBe('failed');
      // No retry: the command is terminal failed, not re-offered.
      const sweep: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      expect(sweep.offers).toEqual([]);
    }, fakeReserve().hook);
  });

  it('accepts a canonical absolute worktree --cwd path alongside a workspace label', async () => {
    const reserve = fakeReserve();
    await fixture(async (s) => {
      const res = await s.handle(request('/api/sessions', {
        key: 'abs-cwd-wt-1',
        workspace: 'ai-shipping-labs',
        cwd: '/data/agents/ai-shipping-labs/worktrees/old-email-cleanup-cec7cfc5',
        mode: 'interactive',
        with_goal: true,
        brief: 'echo ok',
      }), admin);
      expect(res.status).toBe(201);
      expect(reserve.calls[0].cwd).toBe('/data/agents/ai-shipping-labs/worktrees/old-email-cleanup-cec7cfc5');
      const bad = await s.handle(request('/api/sessions', {
        key: 'abs-cwd-wt-bad',
        workspace: 'ai-shipping-labs',
        cwd: '/data/agents/../escape',
        mode: 'interactive',
        with_goal: true,
        brief: 'echo ok',
      }), admin);
      expect(bad.status).toBe(400);
    }, reserve.hook);
  });

  it('surfaces hold reason and rejected[] on held sessions in sessions_show and auto-promotes transient resource holds once capacity recovers', async () => {
    let available = false;
    const placed = fakeReserve();
    const rejectedList = [
      { machine: 'hetzner', engine: 'codex', reason: 'resource_limit' },
      { machine: 'hetzner', engine: 'grok', reason: 'provider_disabled' },
    ];
    await fixture(async (s, sql) => {
      const created = await (await s.handle(request('/api/sessions', {
        key: 'auto-reconcile-held-1',
        name: 'drain-test-1',
        workspace: 'demo',
        mode: 'interactive',
      }), admin)).json() as any;
      expect(created.state).toBe('held');
      expect(created.reason).toBe('resource_limit');
      expect(created.rejected).toEqual(rejectedList);

      // Polling GET /api/sessions/:id while still blocked preserves reason and rejected[].
      const shownWhileHeld = await (await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), admin)).json() as any;
      expect(shownWhileHeld.state).toBe('held');
      expect(shownWhileHeld.reason).toBe('resource_limit');
      expect(shownWhileHeld.rejected).toEqual(rejectedList);

      // A second held session whose intake task already has a newer linked session is auto-cancelled with superseded_by_newer_session.
      sql.exec('CREATE TABLE IF NOT EXISTS intake_tasks (id TEXT PRIMARY KEY, data TEXT)');
      sql.exec(
        'INSERT INTO intake_tasks (id, data) VALUES (?, ?)',
        'd8a3a82d-6710-4db4-ab2b-394251ebd2d4',
        JSON.stringify({
          id: 'd8a3a82d-6710-4db4-ab2b-394251ebd2d4',
          status: 'open',
          project: 'ai-shipping-labs',
          acceptance_criteria: '1. Verify fix',
          history: [],
        }),
      );
      const superseded = await (await s.handle(request('/api/sessions', {
        key: 'auto-reconcile-held-2',
        name: 'drain-superseded-1',
        workspace: 'demo',
        mode: 'interactive',
        intake_task_id: 'd8a3a82d-6710-4db4-ab2b-394251ebd2d4',
      }), admin)).json() as any;
      expect(superseded.state).toBe('held');
      sql.exec(
        'UPDATE intake_tasks SET data = ? WHERE id = ?',
        JSON.stringify({
          id: 'd8a3a82d-6710-4db4-ab2b-394251ebd2d4',
          status: 'open',
          project: 'ai-shipping-labs',
          acceptance_criteria: '1. Verify fix',
          history: [
            { kind: 'link', hub_session_id: superseded.id, assignment_hold: { error: 'no_eligible_route', rejected: rejectedList } },
            { kind: 'link', hub_session_id: '543cc23e-49c6-432c-b80f-65538c594d90' },
          ],
        }),
        'd8a3a82d-6710-4db4-ab2b-394251ebd2d4',
      );

      // Once real capacity exists, polling automatically re-evaluates: promotes active held session and cancels superseded held session.
      available = true;
      const shownAfterRecovery = await (await s.handle(request(`/api/sessions/${created.id}`, undefined, 'GET'), admin)).json() as any;
      expect(shownAfterRecovery.state).toBe('offered');
      expect(shownAfterRecovery.fleet_launch_id).toBe('launch-1');

      const shownSuperseded = await (await s.handle(request(`/api/sessions/${superseded.id}`, undefined, 'GET'), admin)).json() as any;
      expect(shownSuperseded.state).toBe('cancelled');
      expect(shownSuperseded.reason).toBe('superseded_by_newer_session');
    }, input => available ? placed.hook(input) : { status: 409, value: { error: 'no_eligible_route', rejected: rejectedList } });
  });

  it('accepts a 64 KiB capture ack whose JSON body exceeds 32 KiB and promotes an expired inbox-waiting send on late delivery proof', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      sql.exec("UPDATE fleet_sessions SET agent_state = 'idle' WHERE native_id = ?", NATIVE_ID);

      // 1. 64 KiB capture ack with newlines/quotes so serialized JSON body exceeds 32 KiB (>70 KiB)
      const capCmd: any = await (await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'cap-64k', max_bytes: 65536 }), admin)).json();
      const capPoll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const capOffer = capPoll.offers.find((o: any) => o.command_id === capCmd.command_id);
      const capClaim = await claimAs(s, capCmd.command_id, capOffer.correlation);
      const bigCapture = ('line "quoted"\n').repeat(4681) + 'tail'; // 65,538 chars -> trim to 65,536
      const exactCapture = bigCapture.slice(0, 65536);
      const capAckRes = await s.handle(request(`/api/session-commands/${capCmd.command_id}/ack`, {
        correlation: capOffer.correlation,
        claim_token: capClaim.claim_token,
        native: native(),
        outcome: 'delivered',
        capture: exactCapture,
      }), host('hetzner'));
      expect(capAckRes.status).toBe(200);
      expect(((await capAckRes.json()) as any).state).toBe('acked');
      const capListed: any = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json();
      const capRow = capListed.commands.find((c: any) => c.id === capCmd.command_id);
      expect(capRow.state).toBe('acked');
      expect(capRow.ack.capture.length).toBe(65536);

      // 2. An inbox-waiting send expired by server-side expireClaim (no host claim_token in ack_json)
      //    is promoted to acked when the host later submits a valid delivered proof on the same claim_token.
      const sendCmd: any = await (await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'late-headless-send', body: 'hello headless' }), admin)).json();
      const sendPoll: any = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json();
      const sendOffer = sendPoll.offers.find((o: any) => o.command_id === sendCmd.command_id);
      const sendClaim = await claimAs(s, sendCmd.command_id, sendOffer.correlation);
      await s.handle(request(`/api/session-commands/${sendCmd.command_id}/status`, {
        claim_token: sendClaim.claim_token,
        status: 'waiting in inbox for recipient',
        reason: 'aplexer inbox recorded; recipient has not acknowledged',
      }), host('hetzner'));
      sql.exec('UPDATE session_control_commands SET claim_expires_at = ? WHERE id = ?', new Date(Date.now() - 1000).toISOString(), sendCmd.command_id);
      await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'));

      const afterSweep: any = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json();
      expect(afterSweep.commands.find((c: any) => c.id === sendCmd.command_id).state).toBe('failed');

      const lateAck = await s.handle(request(`/api/session-commands/${sendCmd.command_id}/ack`, {
        correlation: sendOffer.correlation,
        claim_token: sendClaim.claim_token,
        native: native(),
        outcome: 'delivered',
        reason: 'recipient acknowledged native inbox message at turn',
        proof: goodProof(),
      }), host('hetzner'));
      expect(lateAck.status).toBe(200);
      expect(((await lateAck.json()) as any).state).toBe('acked');
    }, fakeReserve().hook);
  });

  it('rejects send to a headless codex session at send time and hints next-turn delivery for other headless engines (repro db717cb7)', async () => {
    await fixture(async (s, sql) => {
      const session: any = await startOk(s, { machine: 'hetzner' });
      await bindHttp(s, sql, session.id);
      sql.exec("UPDATE fleet_sessions SET agent_state = 'working' WHERE native_id = ?", NATIVE_ID);

      // Hook-capable headless engine: queued for its next turn, with a hint.
      const queued = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'next-turn', body: 'cancel the booking' }), admin);
      expect(queued.status).toBe(201);
      const queuedData: any = await queued.json();
      expect(queuedData.state).toBe('recorded');
      expect(queuedData.delivery).toBe('next_turn');

      // Headless codex/zcodex (exec) never reads input: refused at send time, nothing recorded.
      for (const engine of ['codex', 'zcodex']) {
        sql.exec('UPDATE session_control_sessions SET engine = ? WHERE id = ?', engine, session.id);
        const refused = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'exec-' + engine, body: 'cancel the booking' }), admin);
        expect(refused.status).toBe(409);
        const refusedData: any = await refused.json();
        expect(refusedData.error).toBe('send_unsupported_headless');
        expect(refusedData.message).toContain('accept no input');
        const rows = sql.exec("SELECT id FROM session_control_commands WHERE session_id = ? AND request_key = ?", session.id, 'exec-' + engine).toArray();
        expect(rows.length).toBe(0);
      }

      // Interactive sessions of any engine keep the normal path without the hint.
      sql.exec("UPDATE session_control_sessions SET engine = 'codex', mode = 'interactive' WHERE id = ?", session.id);
      const interactive = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'interactive', body: 'hello' }), admin);
      expect(interactive.status).toBe(201);
      expect(((await interactive.json()) as any).delivery).toBeUndefined();
    }, fakeReserve().hook);
  });
});
