import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { FleetService } from '../src/fleet';
import { IntakeWatchdog } from '../src/intake-watchdog';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

it('reports a dead Codex TUI as failed while retaining lifecycle and capacity until native exit', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('codex-reconnect-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const now = Date.parse('2026-10-26T17:00:00Z'), at = new Date(now).toISOString();
    const tx = <T>(cb: () => T) => state.storage.transactionSync(cb);
    const service = new SessionControlService(hub.sql, tx, () => now); service.init();
    const fleet = new FleetService(hub.sql, tx, () => now); fleet.init();
    const id = '11111111-1111-4111-8111-111111111111';
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('test-host', 'test-host', 'test-host', ?, ?)", at, at);
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'test', '{}', 'test-host', 'codex', 'interactive', 'running', ?, ?, ?)`, id,
      JSON.stringify({ native_id: id, tag: 'hub-test', workspace: '/test', native_created_at_ms: now - 120000 }), at, at);
    const row = { native_id: id, tag: 'hub-test', workspace: '/test', engine: 'codex', phase: 'running', agent_state: 'idle', profile: null, account: null,
      created_at_ms: now - 120000, last_activity_at: at,
      progress: { last_progress_at: new Date(now - 120000).toISOString(), headless: false, agent_alive: null, orphaned: false, cleanup: 'none', terminal_error: 'codex_reconnect_failed' } };
    const report = (data: any) => fleet.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions: [data] }) }),
      { caller: { machineId: 'test-host', kind: 'host', actor: 'machine:test-host' }, scopes: ['host'] });
    expect((await report({ ...row, progress: { ...row.progress, terminal_error: 'arbitrary private log' } })).status).toBe(400);
    expect((await report(row)).status).toBe(200);
    const view = await (await service.handle(new Request('https://hub.test/api/sessions/' + id),
      { caller: { isAdmin: true, kind: 'human', actor: 'test-admin' }, scopes: ['admin'] })).json();
    expect(view).toMatchObject({ state: 'failed', stalled: false, failure_reason: 'codex_reconnect_failed', lifecycle_state: 'running', cleanup_pending: true });
    expect(hub.sql.exec('SELECT state FROM session_control_sessions').toArray()[0].state).toBe('running');
    expect(hub.sql.exec('SELECT * FROM session_control_commands').toArray()).toEqual([]);
    const watchdog = new IntakeWatchdog(hub.sql, {} as any, tx, {}, () => now); watchdog.init();
    const task = { id: 'synthetic-task', title: 'Synthetic', project: 'demo', status: 'open', created_at: now - 60000,
      history: [{ kind: 'link', hub_session_id: id }] };
    expect(watchdog.project([task])[0]).toMatchObject({ forgotten: true, reason: 'only_failed_sessions', last_session: { state: 'failed' } });
    for (const phase of ['running', 'starting', 'broken']) {
      expect((await report({ ...row, phase })).status).toBe(200);
      const absent = await fleet.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions: [] }) }),
        { caller: { machineId: 'test-host', kind: 'host', actor: 'machine:test-host' }, scopes: ['host'] });
      expect(absent.status).toBe(200);
      expect(hub.sql.exec('SELECT phase FROM fleet_sessions').toArray()[0].phase).toBe('unobserved');
      expect(watchdog.project([task])[0]).toMatchObject({ forgotten: false, state: 'unknown' });
    }
    const missingView = await (await service.handle(new Request('https://hub.test/api/sessions/' + id),
      { caller: { isAdmin: true, kind: 'human', actor: 'test-admin' }, scopes: ['admin'] })).json();
    expect(missingView).not.toMatchObject({ state: 'failed' });
    expect((await report({ ...row, phase: 'active' })).status).toBe(200);
    expect(watchdog.project([task])[0].forgotten).toBe(false);
    expect((await report(row)).status).toBe(200);
    // A retained error from another native incarnation cannot authorize restart.
    hub.sql.exec('UPDATE fleet_sessions SET created_at_ms = created_at_ms + 1');
    expect(watchdog.project([task])[0]).toMatchObject({ forgotten: false, state: 'active' });
    // Host loss cannot turn an otherwise valid binding into restart authority.
    hub.sql.exec('UPDATE fleet_sessions SET created_at_ms = created_at_ms - 1');
    hub.sql.exec('DELETE FROM fleet_catalog_seen');
    hub.sql.exec("UPDATE machines SET last_checkin_at = '2000-01-01T00:00:00Z'");
    expect(watchdog.project([task])[0]).toMatchObject({ forgotten: false, state: 'unknown' });
  });
});
