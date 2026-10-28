import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const id = '11111111-1111-4111-8111-111111111111';
const base = Date.parse('2026-10-24T12:00:00Z');
async function fixture(fn: (s: SessionControlService, sql: any, clock: { now: number }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('long-running-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const clock = { now: base };
    const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), () => clock.now);
    service.init();
    const started = new Date(base - 3 * 3600000).toISOString();
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'test', ?, 'test-host', 'codex', 'headless', 'running', ?, ?, ?)`, id,
      JSON.stringify({ name: 'long-session', intake_task_id: 'test-intake' }),
      JSON.stringify({ native_id: id, tag: 'long-session', workspace: '/test', native_created_at_ms: base - 3 * 3600000 }), started, started);
    hub.sql.exec(`INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, observed_at, workspace, last_activity_at) VALUES ('test-host', ?, 'long-session', 'codex', 'running', ?, '/test', ?)`, id, new Date(base).toISOString(), new Date(base - 3600000).toISOString());
    await fn(service, hub.sql, clock);
  });
}
function req(path = '', body?: any) {
  return new Request('https://hub.test/api/sessions' + path, body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) });
}
async function json(s: SessionControlService, path = '') { return await (await s.handle(req(path), admin)).json() as any; }

describe('CLI long-running warning projection and durable snooze', () => {
  it('uses strict thresholds, native creation and output activity, excludes terminal sessions', async () => {
    await fixture(async (s, sql, clock) => {
      let view = await json(s, '/' + id);
      expect(view).toMatchObject({ long_running: false, runtime: 10800, idle_for: 3600, activity_state: 'idle', intake_id: 'test-intake' });
      clock.now += 1;
      view = await json(s, '/' + id);
      expect(view).toMatchObject({ long_running: true, activity_state: 'idle', warning_active: true });
      expect((await json(s)).sessions[0].long_running).toBe(true);
      expect((await json(s, '/warnings')).warnings[0].id).toBe(id);
      sql.exec('UPDATE fleet_sessions SET last_activity_at = ?', new Date(clock.now).toISOString());
      expect(await json(s, '/' + id)).toMatchObject({ idle_for: 0, activity_state: 'active' });
      expect((await json(s, '/' + id + '?threshold=14400')).long_running).toBe(false);
      sql.exec("UPDATE session_control_sessions SET state = 'stopped'");
      expect((await json(s, '/warnings')).warnings).toEqual([]);
      expect((await json(s, '/' + id)).long_running).toBe(false);
    });
  });
  it('persists actor/time/until, hides until expiry, never creates stop commands or deletes a session', async () => {
    await fixture(async (s, sql, clock) => {
      clock.now += 3600000;
      const before = sql.exec('SELECT * FROM session_control_sessions').toArray();
      let ack = await (await s.handle(req('/' + id + '/ack', {}), admin)).json() as any;
      expect(ack).toMatchObject({ long_running: true, warning_active: false, event: { kind: 'warning_ack', actor: 'test-admin', created_at: new Date(clock.now).toISOString(), until_at: new Date(clock.now + 10800000).toISOString() } });
      expect((await json(s, '/warnings')).warnings).toEqual([]);
      clock.now += 10800000 - 1;
      expect((await json(s, '/warnings')).warnings).toEqual([]);
      clock.now += 1;
      expect((await json(s, '/warnings')).warnings).toHaveLength(1);
      ack = await (await s.handle(req('/' + id + '/ack?threshold=60', {}), admin)).json() as any;
      expect(Date.parse(ack.event.until_at) - clock.now).toBe(60000);
      const custom = await (await s.handle(req('/' + id + '/ack', { snooze: 120 }), admin)).json() as any;
      expect(Date.parse(custom.event.until_at) - clock.now).toBe(120000);
      expect((await json(s, '/' + id + '/events')).events).toHaveLength(3);
      expect(sql.exec('SELECT * FROM session_control_sessions').toArray()).toEqual(before);
      expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toEqual([]);
    });
  });
  it('rejects invalid durations and unauthorized reads/writes', async () => {
    await fixture(async (s) => {
      for (const val of [0, -1, 1.5, '3h', 31536001]) {
        expect((await s.handle(req('/' + id + '/ack', { snooze: val }), admin)).status).toBe(400);
      }
      expect((await s.handle(req('/warnings?threshold=no'), admin)).status).toBe(400);
      const foreign = { caller: { kind: 'agent', actor: 'other' }, scopes: ['fleet:launch'] };
      expect((await s.handle(req('/' + id + '/ack', {}), foreign)).status).toBe(404);
      expect(await (await s.handle(req('/warnings'), foreign)).json()).toEqual({ warnings: [], wake_alerts: [] });
      expect((await s.handle(req('/warnings'), {})).status).toBe(401);
    });
  });
});
