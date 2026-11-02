import { describe, expect, it } from 'vitest';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { StatusService } from '../src/status';
import { IntakeService } from '../src/intake';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '22222222-2222-4222-8222-222222222222';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const canonTag = (launchId: string) => 'hub-' + launchId;
const bound = (tag: string) => ({ native_id: NATIVE_ID, tag, workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED });
const ATTEMPT = 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa';

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

async function fixture(fn: (s: SessionControlService, sql: any, hub: Hub, state: DurableObjectState) => Promise<void>) {
  const fake = fakeReserve();
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-wake-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fake.hook, wakeMissed: (taskId, wakeId) => hub.intakeWatchdog.enqueueWakeRecovery(taskId, wakeId) }, {});
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    await fn(service, hub.sql, hub, state);
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'test-admin', 'human', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'claude', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'claude', ?, NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function startAndBind(s: SessionControlService, sql: any, key: string) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', machine: 'hetzner' }), admin);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  const launchId = data.fleet_launch_id || 'launch-1';
  ensureFleetLaunch(sql, launchId, data.machine || 'hetzner');
  reportFleetSession(sql, data.machine || 'hetzner', NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const bres = await s.handle(request(`/api/sessions/${data.id}/binding`, { native: { ...bound(canonTag(launchId)), evidence: 'fixture' } }), host(data.machine || 'hetzner'));
  const bdata = await bres.json() as any;
  expect(bres.status, JSON.stringify(bdata)).toBe(200);
  return bdata.id ? { ...bdata, id: data.id } : data;
}


const mk = (s: SessionControlService, id: string, data: any) => s.handle(request(`/api/sessions/${id}/ping`, data), admin);

describe('session pings (external loop)', () => {
  it('creates a ping, shows it on the session and delivers a send command on each due tick', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-1');
      const res = await mk(s, sess.id, { every_seconds: 900, prompt: 'check goal' });
      expect(res.status).toBe(201);
      const created: any = await res.json();
      expect(created.ping).toMatchObject({ state: 'active', every_seconds: 900 });
      const shown: any = await (await s.handle(request(`/api/sessions/${sess.id}`, undefined, 'GET'), admin)).json();
      expect(shown.ping).toMatchObject({ id: created.ping.id, every_seconds: 900 });
      expect(s.processPings()).toMatchObject({ delivered: 0 });
      sql.exec('UPDATE session_control_pings SET next_at_ms=? WHERE id=?', Date.now() - 1, created.ping.id);
      expect(s.processPings()).toMatchObject({ delivered: 1 });
      const cmd: any = sql.exec("SELECT * FROM session_control_commands WHERE session_id=? AND request_key LIKE 'ping-%'", sess.id).toArray()[0];
      expect(cmd.kind).toBe('send');
      expect(JSON.parse(cmd.payload_json)).toEqual({ body: 'check goal' });
      const row: any = sql.exec('SELECT * FROM session_control_pings WHERE id=?', created.ping.id).toArray()[0];
      expect(row.delivered_count).toBe(1);
      expect(row.next_at_ms).toBeGreaterThan(Date.now() + 800000);
      expect(s.nextWakeTime()).toBe(row.next_at_ms);
    });
  });

  it('coalesces ticks while the previous delivery is unfinished and resumes after the ack', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-2');
      const created: any = await (await mk(s, sess.id, { every_seconds: 60, prompt: 'tick' })).json();
      const due = () => sql.exec('UPDATE session_control_pings SET next_at_ms=? WHERE id=?', Date.now() - 1, created.ping.id);
      due(); s.processPings();
      due(); expect(s.processPings()).toMatchObject({ delivered: 0, skipped: 1 });
      due(); s.processPings();
      expect(sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE request_key LIKE 'ping-%'").toArray()[0].n).toBe(1);
      sql.exec("UPDATE session_control_commands SET state='acked' WHERE request_key LIKE 'ping-%'");
      due(); expect(s.processPings()).toMatchObject({ delivered: 1 });
      const row: any = sql.exec('SELECT * FROM session_control_pings WHERE id=?', created.ping.id).toArray()[0];
      expect(row).toMatchObject({ delivered_count: 2, skipped_count: 2 });
    });
  });

  it('records a failed delivery as an alert and attention item, then clears it on a later ack', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-3');
      const created: any = await (await mk(s, sess.id, { every_seconds: 60, prompt: 'tick' })).json();
      sql.exec('UPDATE session_control_pings SET next_at_ms=? WHERE id=?', Date.now() - 1, created.ping.id);
      s.processPings();
      sql.exec("UPDATE session_control_commands SET state='failed' WHERE request_key LIKE 'ping-%'");
      expect(s.processPings()).toMatchObject({ failed: 1 });
      expect(sql.exec("SELECT kind FROM session_control_wake_alerts WHERE wake_id=?", created.ping.id).toArray()[0].kind).toBe('ping_failed');
      expect(s.attention().some((a: any) => a.source === 'session_ping' && a.session_id === sess.id)).toBe(true);
      const pings: any = await (await s.handle(request(`/api/sessions/${sess.id}/pings`, undefined, 'GET'), admin)).json();
      expect(pings.active).toMatchObject({ failure_count: 1, last_error: 'ping_delivery_failed' });
      expect(pings.alerts).toHaveLength(1);
    });
  });

  it('can be cleared by the owner and replaces an earlier ping', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-4');
      const first: any = await (await mk(s, sess.id, { every_seconds: 60, prompt: 'a' })).json();
      const second: any = await (await mk(s, sess.id, { every_seconds: 120, prompt: 'b' })).json();
      expect(second.replaced).toBe(first.ping.id);
      const cleared: any = await (await s.handle(request(`/api/sessions/${sess.id}/ping-clear`, { reason: 'done' }), admin)).json();
      expect(cleared).toMatchObject({ cleared: true, ping_id: second.ping.id });
      expect(s.nextWakeTime()).toBeNull();
      const again: any = await (await s.handle(request(`/api/sessions/${sess.id}/ping-clear`, {}), admin)).json();
      expect(again.cleared).toBe(false);
    });
  });

  it('ends automatically when the session ends', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-5');
      const created: any = await (await mk(s, sess.id, { every_seconds: 60, prompt: 'tick' })).json();
      sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sess.id);
      expect(s.processPings()).toMatchObject({ ended: 1, delivered: 0 });
      expect(sql.exec('SELECT state, ended_reason FROM session_control_pings WHERE id=?', created.ping.id).toArray()[0]).toEqual({ state: 'ended', ended_reason: 'session_stopped' });
    });
  });

  it('rejects out-of-range intervals, empty prompts and unknown keys', async () => {
    await fixture(async (s, sql) => {
      const sess = await startAndBind(s, sql, 'ping-6');
      for (const bad of [{ every_seconds: 30, prompt: 'x' }, { every_seconds: 700000, prompt: 'x' }, { every_seconds: 60, prompt: '  ' }, { every_seconds: 60, prompt: 'x', extra: 1 }, { prompt: 'x' }]) {
        expect((await mk(s, sess.id, bad)).status).toBe(400);
      }
    });
  });
});
