import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '11111111-1111-4111-8111-111111111111';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_TAG = 'hub-launch-1';
const NATIVE_CREATED = 1792223000000;
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';

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

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>, reserve?: (input: any) => { value: any; status: number }) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-cap-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now,
      reserve ? { reserve } : {}, {});
    service.init();
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql);
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', 'running', ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, NATIVE_ID, at, at);
}

function reportFleetSession(sql: any, machine: string) {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'codex', 'running', NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    machine, NATIVE_ID, NATIVE_TAG, at, NATIVE_WS);
}

async function startAndBind(s: SessionControlService, sql: any, key: string) {
  const created = await (await s.handle(request('/api/sessions', { key, workspace: 'demo' }), admin)).json() as any;
  ensureFleetLaunch(sql, created.fleet_launch_id || 'launch-1', 'hetzner');
  reportFleetSession(sql, 'hetzner');
  const bound = await s.handle(request(`/api/sessions/${created.id}/binding`,
    { native: { native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }), host('hetzner'));
  expect(bound.status).toBe(200);
  return created;
}

describe('capture screen projection', () => {
  it('defaults to tail and stores explicit mode', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startAndBind(s, sql, 'cap-tail-1');
      const res = await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'c1' }), admin);
      expect(res.status).toBe(201);
      const row: any = sql.exec('SELECT payload_json FROM session_control_commands WHERE session_id = ?', session.id).toArray()[0];
      expect(JSON.parse(row.payload_json)).toMatchObject({ mode: 'tail' });
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(poll.offers[0].payload.mode).toBe('tail');
      expect(poll.offers[0].mode).toBe('tail');
    }, fake.hook);
  });

  it('accepts mode=screen and screen=true, persists across claim replay', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startAndBind(s, sql, 'cap-screen-1');
      const res = await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'cs1', mode: 'screen' }), admin);
      expect(res.status).toBe(201);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const offer = poll.offers[0];
      expect(offer.payload.mode).toBe('screen');
      expect(offer.mode).toBe('screen');
      const claim = await (await s.handle(request(`/api/session-commands/${offer.command_id}/claim`,
        { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'))).json() as any;
      expect(claim.claim_token).toBeTruthy();
      // Same attester replays the same grant without changing the projection.
      const replayRes = await s.handle(request(`/api/session-commands/${offer.command_id}/claim`,
        { correlation: offer.correlation, attempt_id: ATTEMPT }), host('hetzner'));
      expect(replayRes.status).toBe(200);
      const poll2 = await (await s.handle(request(`/api/session-commands/${offer.command_id}/ack`,
        { correlation: offer.correlation, outcome: 'delivered', native: { native_id: NATIVE_ID, tag: NATIVE_TAG, workspace: NATIVE_WS }, claim_token: claim.claim_token, capture: 'screen text' }), host('hetzner'))).json() as any;
      expect(poll2.state).toBe('acked');
      const cmds = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(cmds.commands[0].payload.mode).toBe('screen');
    }, fake.hook);
  });

  it('refuses bad mode/bool and unknown fields before mutation', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startAndBind(s, sql, 'cap-bad-1');
      const before = Number(sql.exec('SELECT COUNT(*) AS n FROM session_control_commands').one().n);
      for (const body of [
        { key: 'b1', mode: 'fullscreen' },
        { key: 'b2', mode: 42 },
        { key: 'b3', screen: 'yes' },
        { key: 'b4', mode: 'screen', screen: false },
        { key: 'b5', mode: 'tail', screen: true },
        { key: 'b6', max_bytes: 10, bogus: 1 },
      ]) {
        const res = await s.handle(request(`/api/sessions/${session.id}/capture`, body), admin);
        expect(res.status, JSON.stringify(body)).toBe(400);
      }
      const after = Number(sql.exec('SELECT COUNT(*) AS n FROM session_control_commands').one().n);
      expect(after).toBe(before);
    }, fake.hook);
  });

  it('rejects idempotency conflict when the same key changes mode', async () => {
    const fake = fakeReserve();
    await fixture(async (s, sql) => {
      const session: any = await startAndBind(s, sql, 'cap-conflict-1');
      expect((await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'k1', mode: 'tail' }), admin)).status).toBe(201);
      expect((await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'k1', mode: 'screen' }), admin)).status).toBe(409);
    }, fake.hook);
  });
});
