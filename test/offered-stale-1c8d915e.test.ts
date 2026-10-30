/** Intake 1c8d915e: natively started launches stuck as `offered`, and stops
 * on heartbeating hosts failing as stop_host_unreachable.
 *
 * AC1/AC2: a successor native in a same-intake worktree stayed unbound because
 *   the predecessor's retired native never reached a terminal census phase,
 *   although its Fleet launch carried a confirmed exit receipt; and the
 *   canonical row never left `offered` on claim or on the running receipt.
 * AC3: stop on an offered row whose launch is native-running reaches the host
 *   and settles; a host that is polling or checked in recently (in memory) is
 *   never reported as stop_host_unreachable.
 */
import { freshCpu } from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { updateHeartbeatMemoryOnly } from '../src/registry';
import { FIXTURE_INTAKE_ID, seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const START = Date.now();
const NATIVE_ID = '9bb2ae7c-82e3-42e2-a18f-6cb2d00ee188';
const PREDECESSOR_NATIVE = '48fe19dd-439b-41fa-82a6-2285cc7c8c4a';
const NATIVE_CREATED = 1793332119480;
const WORKSPACE = '/srv/hub/wt-cbcadaa1';

function fleetReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}
function sessReq(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}
const registry = () => ({
  accounts: [{ label: 'shared-codex', provider: 'codex', max_concurrency: 2, reserve_percent: 15, enabled: true }],
  routes: [{ machine_id: 'stale-1c8d', engine: 'codex', account: 'shared-codex', profile: null }],
});
const host = { caller: { machineId: 'stale-1c8d', kind: 'host', actor: 'machine:stale-1c8d', tokenId: 'host-stale-1c8d' }, scopes: ['host'] };

type Ctx = { f: FleetService; s: SessionControlService; sql: any; clock: { now: number } };

async function fixture(fn: (ctx: Ctx) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('offered-stale-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: START };
    const iso = (delta = 0) => new Date(clock.now + delta).toISOString();
    const tx = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const f = new FleetService(hub.sql, tx, () => clock.now);
    f.init();
    const s = new SessionControlService(hub.sql, tx, () => clock.now, { reserve: (input, auth) => f.reserve(input, auth) });
    s.init();
    const sql = hub.sql;
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-stale-1c8d', 'host', 'hash-stale-1c8d', 'refresh-stale-1c8d', 'family-stale-1c8d', 'test-host', 'stale-1c8d', '["host"]', 'test', ?, ?)`, iso(), iso(86400000));
    sql.exec('INSERT INTO machines (id, name, hostname, credential_id, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'stale-1c8d', 'stale-1c8d', 'stale-1c8d', 'host-stale-1c8d', iso(), iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'stale-1c8d', '1.0', iso(), clock.now, iso(), clock.now, 'ok', null,
      JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    seedFixtureIntake(sql);
    expect((await f.handle(fleetReq('/accounts', registry(), 'PUT'), admin)).status).toBe(200);
    await fn({ f, s, sql, clock });
  });
}

/** Start a canonical headless session, then claim and natively start it. */
async function startNative(ctx: Ctx, key: string) {
  const { f, s, sql, clock } = ctx;
  const res = await s.handle(sessReq('/api/sessions', { key, workspace: 'demo', machine: 'stale-1c8d', mode: 'headless', brief: 'offered-stale probe' }), admin);
  const created: any = await res.json();
  expect(res.status, JSON.stringify(created)).toBe(201);
  expect(created.state).toBe('offered');
  const launchId = created.fleet_launch_id as string;
  const claim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: new Date(clock.now).toISOString(), account: 'shared-codex', profile_supported: true };
  const claimed = await f.handle(fleetReq('/launches/' + launchId + '/claim', claim), host);
  expect(claimed.status, JSON.stringify(await claimed.clone().json())).toBe(200);
  const state = () => sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', created.id).one().state;
  // A claim alone proves no native yet: the row stays offered (review 603199ea finding 1).
  expect(state()).toBe('offered');
  const running = await f.handle(fleetReq('/launches/' + launchId + '/result', { state: 'running', native_id: NATIVE_ID, launcher_task_id: 'hub-' + launchId }), host);
  expect(running.status).toBe(200);
  expect(state()).toBe('starting');
  return { id: created.id as string, launchId, state };
}

/** Reproduce the incident row: launch running with a native receipt, row still offered. */
function strandOffered(sql: any, sessionId: string, clock: { now: number }) {
  sql.exec("UPDATE session_control_sessions SET state = 'offered', updated_at = ? WHERE id = ?", new Date(clock.now).toISOString(), sessionId);
}

function reportNative(sql: any, nativeId: string, tag: string, phase = 'running') {
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at, created_at_ms)
    VALUES ('stale-1c8d', ?, ?, 'codex', ?, NULL, ?, ?, ?, NULL, 'shared-codex', NULL, ?)`, nativeId, tag, phase, new Date().toISOString(), WORKSPACE, WORKSPACE, NATIVE_CREATED);
}

describe('offered rows with a native receipt (intake 1c8d915e)', () => {
  it('promotes a stranded offered row on the next host-bindings poll, then binds it to running', () => fixture(async ctx => {
    const { s, sql, clock } = ctx;
    const started = await startNative(ctx, 'stranded-offered');
    strandOffered(sql, started.id, clock);
    expect(started.state()).toBe('offered');
    const pending: any = await (await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), host)).json();
    expect(started.state()).toBe('starting');
    expect(pending.sessions).toContainEqual({ id: started.id, fleet_launch_id: started.launchId, intake_task_id: FIXTURE_INTAKE_ID, engine: 'codex', native_id: NATIVE_ID });
    const tag = 'task-hub-' + started.launchId;
    reportNative(sql, NATIVE_ID, tag);
    const bound = await s.handle(sessReq('/api/sessions/' + started.id + '/binding', { native: { native_id: NATIVE_ID, tag, workspace: WORKSPACE, native_created_at_ms: NATIVE_CREATED } }), host);
    expect(bound.status, JSON.stringify(await bound.clone().json())).toBe(200);
    expect(started.state()).toBe('running');
  }));

  it('shows a stranded offered row as starting on the next session read', () => fixture(async ctx => {
    const { s, sql, clock } = ctx;
    const started = await startNative(ctx, 'stranded-show');
    strandOffered(sql, started.id, clock);
    const shown: any = await (await s.handle(new Request('https://hub.test/api/sessions/' + started.id, { method: 'GET' }), admin)).json();
    expect(shown.state).toBe('starting');
    strandOffered(sql, started.id, clock);
    await s.handle(new Request('https://hub.test/api/sessions', { method: 'GET' }), admin);
    expect(started.state()).toBe('starting');
  }));

  it('keeps a claimed launch without a native receipt offered, and stop refuses explicitly', () => fixture(async ctx => {
    // Review 603199ea finding 1: an unbound host stop during pre-spawn setup
    // could confirm "no live native" and strand the native that follows.
    const { f, s, sql, clock } = ctx;
    // The no-start cancellation path only trusts an explicit human operator token.
    const operator = { caller: { isAdmin: true, actor: 'human:admin-token:fixture', kind: 'human' }, scopes: ['admin'] };
    const res = await s.handle(sessReq('/api/sessions', { key: 'claimed-no-native', workspace: 'demo', machine: 'stale-1c8d', mode: 'headless', brief: 'offered-stale probe' }), operator);
    const created: any = await res.json();
    const claim = { gate_allowed: true, model: 'gpt-6.1-sol', observed_at: new Date(clock.now).toISOString(), account: 'shared-codex', profile_supported: true };
    expect((await f.handle(fleetReq('/launches/' + created.fleet_launch_id + '/claim', claim), host)).status).toBe(200);
    await s.handle(new Request('https://hub.test/api/sessions', { method: 'GET' }), admin);
    await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), host);
    await s.handle(new Request('https://hub.test/api/sessions/' + created.id, { method: 'GET' }), admin);
    const state = () => sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', created.id).one().state;
    expect(state()).toBe('offered');
    const stopped = await s.handle(sessReq('/api/sessions/' + created.id + '/stop', { key: 'stop-pre-spawn' }), operator);
    expect(stopped.status).toBe(409);
    expect((await stopped.json() as any).error).toBe('session_not_cancellable');
    expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_commands WHERE session_id = ?', created.id).one().n).toBe(0);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', created.fleet_launch_id).one().state).toBe('starting');
  }));

  it('never promotes an unclaimed offer', () => fixture(async ctx => {
    const { s, sql } = ctx;
    const res = await s.handle(sessReq('/api/sessions', { key: 'unclaimed', workspace: 'demo', machine: 'stale-1c8d', mode: 'headless', brief: 'offered-stale probe' }), admin);
    const created: any = await res.json();
    await s.handle(sessReq('/api/sessions/host-bindings', undefined, 'GET'), host);
    await s.handle(new Request('https://hub.test/api/sessions/' + created.id, { method: 'GET' }), admin);
    expect(sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', created.id).one().state).toBe('offered');
  }));
});

describe('same-intake worktree handoff after a retired predecessor (intake 1c8d915e)', () => {
  function seedPredecessor(sql: any, launchState: string, launchNative: string | null) {
    const at = new Date().toISOString();
    const launchId = '6043ab2e-553c-4e4b-afe6-b369a355e602';
    sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES (?, 'test-admin', 'human', NULL, 'predecessor', 'pred', '{"workspace":"demo"}', 'stale-1c8d', 'codex', 'shared-codex', NULL, 'headless', 'demo', 'goal', ?, ?, ?, ?, ?)`,
      launchId, launchState, launchNative, 'hub-' + launchId, at, at);
    const sessionId = 'f888f320-4931-42c2-ad6b-98825c4c8387';
    const binding = { native_id: PREDECESSOR_NATIVE, tag: 'task-hub-' + launchId, workspace: WORKSPACE, generation: 1, native_created_at_ms: 1793328992784 };
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, mode, engine, state, fleet_launch_id, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'pred', ?, 'stale-1c8d', 'headless', 'codex', 'stopped', ?, ?, ?, ?)`,
      sessionId, JSON.stringify({ workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID }), launchId, JSON.stringify(binding), at, at);
    return { sessionId, launchId };
  }
  const fact = async (s: SessionControlService, id: string) =>
    ((await (await s.handle(sessReq('/api/sessions/worktree-guards', undefined, 'GET'), host)).json() as any).sessions as any[]).find(row => row.session_id === id);

  it('treats the confirmed Fleet exit receipt as native exit when the census never reported one', () => fixture(async ({ s, sql }) => {
    // The incident: the native exited and was retired from aplexer before the
    // census saw a terminal phase, so no terminal fleet_sessions row exists.
    const { sessionId, launchId } = seedPredecessor(sql, 'running', PREDECESSOR_NATIVE);
    expect(await fact(s, sessionId)).toMatchObject({ terminal: true, native_terminal: false });
    sql.exec("UPDATE fleet_launches SET state = 'exited' WHERE id = ?", launchId);
    expect(await fact(s, sessionId)).toMatchObject({ terminal: true, native_terminal: true, native_id: PREDECESSOR_NATIVE });
  }));

  it('accepts the launch receipt for a natively started session that never bound', () => fixture(async ({ s, sql }) => {
    // Review 603199ea finding 2: an unbound session stopped (AC3) or finished
    // before binding must not hold its intake worktree lock forever.
    const { sessionId, launchId } = seedPredecessor(sql, 'running', PREDECESSOR_NATIVE);
    sql.exec('UPDATE session_control_sessions SET binding_json = NULL WHERE id = ?', sessionId);
    expect(await fact(s, sessionId)).toMatchObject({ terminal: true, native_terminal: false });
    sql.exec("UPDATE fleet_launches SET state = 'exited' WHERE id = ?", launchId);
    expect(await fact(s, sessionId)).toMatchObject({ terminal: true, native_terminal: true });
    sql.exec('UPDATE fleet_launches SET native_id = NULL WHERE id = ?', launchId);
    expect((await fact(s, sessionId)).native_terminal).toBe(false);
  }));

  it('refuses receipt proof for another native or a routed task result', () => fixture(async ({ s, sql }) => {
    const { sessionId, launchId } = seedPredecessor(sql, 'exited', '11111111-1111-4111-8111-111111111111');
    expect((await fact(s, sessionId)).native_terminal).toBe(false);
    sql.exec('UPDATE fleet_launches SET native_id = ? WHERE id = ?', PREDECESSOR_NATIVE, launchId);
    expect((await fact(s, sessionId)).native_terminal).toBe(true);
    const at = new Date().toISOString();
    sql.exec(`INSERT INTO routing_jobs (id, actor, request_key, request_json, launch_id, state, created_at, updated_at) VALUES ('routed-1', 'test-admin', 'routed-1', '{}', ?, 'done', ?, ?)`, launchId, at, at);
    expect((await fact(s, sessionId)).native_terminal).toBe(false);
  }));
});

describe('stop on a native-running offered row (intake 1c8d915e AC3)', () => {
  it('delivers to a polling host whose persisted check-in is stale and stops the correlated native', () => fixture(async ctx => {
    const { s, sql, clock } = ctx;
    const started = await startNative(ctx, 'stop-offered');
    strandOffered(sql, started.id, clock);
    // Persisted check-in is only rewritten every five minutes for an unchanged heartbeat.
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'stale-1c8d'", new Date(clock.now - 240000).toISOString());
    const queued = await s.handle(sessReq('/api/sessions/' + started.id + '/stop', { key: 'stop-stranded' }), admin);
    expect(queued.status, JSON.stringify(await queued.clone().json())).toBe(201);
    clock.now += 9000;
    const listing: any = await (await s.handle(sessReq('/api/session-commands/offers', undefined, 'GET'), host)).json();
    const offer = listing.offers.find((o: any) => o.kind === 'stop' && o.session_id === started.id);
    expect(offer, JSON.stringify(listing)).toBeTruthy();
    expect(offer).toMatchObject({ binding: null, fleet_launch_id: started.launchId });
    const claim: any = await (await s.handle(sessReq('/api/session-commands/' + offer.command_id + '/claim', { correlation: offer.correlation, attempt_id: crypto.randomUUID() }), host)).json();
    expect(claim.claim_token).toBeTruthy();
    const ack = await s.handle(sessReq('/api/session-commands/' + offer.command_id + '/ack', { correlation: offer.correlation, claim_token: claim.claim_token,
      outcome: 'delivered', confirmed_exit: true, reason: 'unbound stop: correlated native process stopped',
      native: { native_id: NATIVE_ID, tag: 'task-hub-' + started.launchId, workspace: WORKSPACE } }), host);
    expect(ack.status).toBe(200);
    expect(started.state()).toBe('stopped');
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id = ?', started.launchId).one().state).toBe('exited');
  }));

  it('keeps stop_host_unreachable for a host that stopped checking in', () => fixture(async ctx => {
    const { s, sql, clock } = ctx;
    const started = await startNative(ctx, 'stop-offline');
    sql.exec("UPDATE machines SET drained = 1 WHERE id = 'stale-1c8d'");
    await s.handle(sessReq('/api/sessions/' + started.id + '/stop', { key: 'stop-offline' }), admin);
    clock.now += 9000;
    const body: any = await (await s.handle(new Request('https://hub.test/api/sessions/' + started.id + '/commands', { method: 'GET' }), admin)).json();
    expect(body.commands[0]).toMatchObject({ state: 'failed', ack: { reason: 'stop_host_unreachable' } });
  }));
  // Last: updateHeartbeatMemoryOnly is isolate-global state for this machine id.
  it('uses the in-memory check-in, and names an unclaimed stop on an online host honestly', () => fixture(async ctx => {
    const { s, sql, clock } = ctx;
    const started = await startNative(ctx, 'stop-memory');
    sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'stale-1c8d'", new Date(clock.now - 240000).toISOString());
    updateHeartbeatMemoryOnly('stale-1c8d');
    await s.handle(sessReq('/api/sessions/' + started.id + '/stop', { key: 'stop-memory' }), admin);
    clock.now += 9000;
    const commands = async () => (await (await s.handle(new Request('https://hub.test/api/sessions/' + started.id + '/commands', { method: 'GET' }), admin)).json() as any).commands;
    expect((await commands())[0].state).not.toBe('failed');
    clock.now += 60000;
    const listing: any = await (await s.handle(sessReq('/api/session-commands/offers', undefined, 'GET'), host)).json();
    expect(listing.offers.filter((o: any) => o.kind === 'stop')).toHaveLength(0);
    const expired = (await commands())[0];
    expect(expired.state).toBe('failed');
    expect(expired.ack.reason).toBe('stop_not_claimed');
    expect(started.state()).toBe('starting');
  }));
});
