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
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
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
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
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

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
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

async function bindAgentSession(s: SessionControlService, sql: any, sessionId: string, launchId: string) {
  ensureFleetLaunch(sql, launchId);
  reportFleetSession(sql, 'hetzner', NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`, {
    native: { native_id: NATIVE_ID, tag: canonTag(launchId), workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED },
  }), host('hetzner'));
  expect(res.status).toBe(200);
}

async function finishObservation(s: SessionControlService) {
  const now = new Date().toISOString();
  const res = await s.handle(request('/api/sessions/observations', {
    machine_id: 'hetzner', native_id: NATIVE_ID,
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
