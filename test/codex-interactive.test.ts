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
    // Failure is a diagnostic, not native exit: keep it visible without making
    // this still-bound process eligible for a duplicate launch. The projected
    // recovery state routes it to the canonical stop-first path (b527e905).
    expect(watchdog.project([task])[0]).toMatchObject({ forgotten: false, ready: false, state: 'recovering',
      last_session: { state: 'failed', lifecycle_state: 'running', failure_reason: 'codex_reconnect_failed' } });
    expect(watchdog.queue([task])[0]).toMatchObject({ state: 'needs_attention', reason: 'codex_reconnect_failed' });
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

it('fails a Claude session whose /goal was rejected fast, never reporting it as running', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('claude-goal-rejected-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const now = Date.parse('2026-11-02T12:00:00Z'), at = new Date(now).toISOString();
    const tx = <T>(cb: () => T) => state.storage.transactionSync(cb);
    const service = new SessionControlService(hub.sql, tx, () => now); service.init();
    const fleet = new FleetService(hub.sql, tx, () => now); fleet.init();
    const id = '33333333-3333-4333-8333-333333333333';
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('test-host', 'test-host', 'test-host', ?, ?)", at, at);
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'test', '{}', 'test-host', 'claude', 'interactive', 'running', ?, ?, ?)`, id,
      JSON.stringify({ native_id: id, tag: 'hub-test', workspace: '/test', native_created_at_ms: now - 120000 }), at, at);
    const row = { native_id: id, tag: 'hub-test', workspace: '/test', engine: 'claude', phase: 'running', agent_state: 'idle', profile: null, account: null,
      created_at_ms: now - 120000, last_activity_at: at,
      progress: { last_progress_at: new Date(now - 120000).toISOString(), headless: false, agent_alive: null, orphaned: false, cleanup: 'none', terminal_error: 'goal_rejected' } };
    const report = (data: any) => fleet.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions: [data] }) }),
      { caller: { machineId: 'test-host', kind: 'host', actor: 'machine:test-host' }, scopes: ['host'] });
    // The error is accepted only for engine claude with an interactive session.
    expect((await report({ ...row, engine: 'codex' })).status).toBe(400);
    expect((await report({ ...row, progress: { ...row.progress, terminal_error: 'codex_reconnect_failed' } })).status).toBe(400);
    expect((await report(row)).status).toBe(200);
    const view = await (await service.handle(new Request('https://hub.test/api/sessions/' + id),
      { caller: { isAdmin: true, kind: 'human', actor: 'test-admin' }, scopes: ['admin'] })).json();
    expect(view).toMatchObject({ state: 'failed', stalled: false, failure_reason: 'goal_rejected', lifecycle_state: 'running', cleanup_pending: true });
    expect(hub.sql.exec('SELECT state FROM session_control_sessions').toArray()[0].state).toBe('running');
    expect(hub.sql.exec('SELECT * FROM session_control_commands').toArray()).toEqual([]);
    const watchdog = new IntakeWatchdog(hub.sql, {} as any, tx, {}, () => now); watchdog.init();
    const task = { id: 'synthetic-task', title: 'Synthetic', project: 'demo', status: 'open', created_at: now - 60000,
      history: [{ kind: 'link', hub_session_id: id }] };
    expect(watchdog.project([task])[0]).toMatchObject({ forgotten: false, ready: false, state: 'recovering',
      last_session: { state: 'failed', lifecycle_state: 'running', failure_reason: 'goal_rejected' } });
    expect(watchdog.queue([task])[0]).toMatchObject({ state: 'needs_attention', reason: 'goal_rejected' });
  });
});

it('fails a dead zcodex session (phase exiting) fast, holds the engine route, and releases only on probe pass', async () => {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('zcodex-dead-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const now = Date.parse('2026-11-01T03:00:00Z'), at = new Date(now).toISOString();
    const tx = <T>(cb: () => T) => state.storage.transactionSync(cb);
    const service = new SessionControlService(hub.sql, tx, () => now); service.init();
    const fleet = new FleetService(hub.sql, tx, () => now); fleet.init();
    const id = '22222222-2222-4222-8222-222222222222';
    hub.sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at) VALUES ('test-host', 'test-host', 'test-host', ?, ?)", at, at);
    hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, machine_id, engine, mode, state, binding_json, created_at, updated_at)
      VALUES (?, 'test-admin', 'test', '{}', 'test-host', 'zcodex', 'interactive', 'running', ?, ?, ?)`, id,
      JSON.stringify({ native_id: id, tag: 'hub-test', workspace: '/test', native_created_at_ms: now - 120000 }), at, at);
    const row = { native_id: id, tag: 'hub-test', workspace: '/test', engine: 'zcodex', phase: 'exiting', agent_state: null, profile: null, account: null,
      created_at_ms: now - 120000, last_activity_at: at,
      progress: { last_progress_at: new Date(now - 120000).toISOString(), headless: false, agent_alive: false, orphaned: false, cleanup: 'none', terminal_error: 'zcodex_agent_exited' } };
    const host = { caller: { machineId: 'test-host', kind: 'host', actor: 'machine:test-host' }, scopes: ['host'] };
    const report = (data: any) => fleet.handle(new Request('https://hub.test/api/fleet/sessions', { method: 'PUT', body: JSON.stringify({ sessions: [data] }) }), host);
    // The error is accepted only for engine zcodex.
    expect((await report({ ...row, engine: 'codex' })).status).toBe(400);
    expect((await report(row)).status).toBe(200);
    const view = await (await service.handle(new Request('https://hub.test/api/sessions/' + id),
      { caller: { isAdmin: true, kind: 'human', actor: 'test-admin' }, scopes: ['admin'] })).json();
    expect(view).toMatchObject({ state: 'failed', stalled: false, failure_reason: 'zcodex_agent_exited' });
    // Route hold is set by the host and released only by the zcodex probe-pass reason.
    const probe = (body: any) => fleet.handle(new Request('https://hub.test/api/fleet/probe', { method: 'POST', body: JSON.stringify(body) }), host);
    expect((await probe({ engine: 'zcodex', status: 'unavailable', reason: 'zcodex_session_failed' })).status).toBe(200);
    expect((await probe({ engine: 'zcodex', status: 'ok', reason: 'admission_configured' })).status).toBe(200);
    expect(hub.sql.exec("SELECT status FROM fleet_engine_health WHERE engine='zcodex'").one().status).toBe('unavailable');
    expect((await probe({ engine: 'zcodex', status: 'ok', reason: 'zcodex_probe_passed' })).status).toBe(200);
    expect(hub.sql.exec("SELECT status FROM fleet_engine_health WHERE engine='zcodex'").one().status).toBe('ok');
  });
});
