import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { seedFixtureIntake, withFixtureIntake, FIXTURE_INTAKE_ID } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'human:admin-token:fixture', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner' }, scopes: ['host'] };
const req = (path: string, data?: any, method = 'POST') => new Request('https://hub.test' + path,
  { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });

async function fixture(fn: (s: SessionControlService, f: FleetService, sql: any, session: any, launch: string) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('wedged-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const txn = <T>(cb: () => T): T => state.storage.transactionSync(cb);
    const f = new FleetService(hub.sql, txn, Date.now); f.init();
    const launch = crypto.randomUUID();
    const s = new SessionControlService(hub.sql, txn, Date.now, { reserve: () => ({
      status: 201, value: { id: launch, state: 'offered', machine: 'hetzner', engine: 'codex' },
    }) });
    s.init(); seedFixtureIntake(hub.sql);
    const created = await s.handle(req('/api/sessions', { key: crypto.randomUUID(), workspace: 'demo',
      engine: 'codex', engine_override_reason: 'Synthetic fixture.', mode: 'headless', brief: 'Test recovery.' }), admin);
    expect(created.status).toBe(201);
    const session: any = await created.json();
    const at = new Date(Date.now() - 3600000).toISOString();
    hub.sql.exec(`INSERT INTO fleet_launches
      (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,launcher_task_id,created_at,updated_at)
      VALUES (?, 'human:admin-token:fixture','human','fixture',?,'{}','hetzner','codex','fixture','headless','demo','test','starting',?,?,?)`,
      launch, launch, 'hub-' + launch, at, at);
    await fn(s, f, hub.sql, session, launch);
  });
}

describe('wedged claimed launches', () => {
  it('accepts owning host fenced no-start, terminalizes offered session, and permits stop with replay', () => fixture(async (s, f, sql, session, launch) => {
    expect((await s.handle(req(`/api/sessions/${session.id}/stop`, { key: 'too-early' }), admin)).status).toBe(409);
    const receipt = { state: 'failed', confirmed_exit: true, launcher_task_id: 'hub-' + launch, reason: 'launcher_not_started' };
    expect((await f.handle(req(`/api/fleet/launches/${launch}/result`, receipt), host)).status).toBe(200);
    const shown: any = await (await s.handle(req(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json();
    expect(shown.state).toBe('failed');
    const stopped: any = await (await s.handle(req(`/api/sessions/${session.id}/stop`, { key: 'stop-proven' }), admin)).json();
    expect(stopped.state).toBe('cancelled');
    expect(stopped.cancellation.effect).toBe('no_native_start');
    expect((await (await s.handle(req(`/api/sessions/${session.id}/stop`, { key: 'stop-proven' }), admin)).json() as any).replay).toBe(true);
    expect(sql.exec('SELECT state, verification_status FROM fleet_launches WHERE id=?', launch).one()).toMatchObject({ state: 'failed', verification_status: 'not_started' });
    expect(sql.exec('SELECT id FROM session_control_commands').toArray()).toHaveLength(0);
  }));

  it('records a recovered completed result and terminal session without requiring a live native binding', () => fixture(async (s, f, sql, session, launch) => {
    const receipt = { state: 'exited', confirmed_exit: true, launcher_task_id: 'hub-' + launch,
      native_id: '22222222-2222-4222-8222-222222222222', report_status: 'done', report: 'Recovered completion.', artifacts: [] };
    expect((await f.handle(req(`/api/fleet/launches/${launch}/result`, receipt), host)).status).toBe(200);
    const shown: any = await (await s.handle(req(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json();
    expect(shown.state).toBe('stopped');
    expect(shown.binding).toBeNull();
    const history = JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).one().data).history;
    expect(history.some((event: any) => JSON.stringify(event).includes('Recovered completion.'))).toBe(true);
    expect((await f.handle(req(`/api/fleet/launches/${launch}/result`, receipt), host)).status).toBe(200);
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?', FIXTURE_INTAKE_ID).one().data).history).toHaveLength(history.length);
  }));

  it('refuses unproven failures, live native identity, and foreign-host terminal receipts', () => fixture(async (s, f, sql, session, launch) => {
    for (const [status, native] of [['unverified', null], ['not_started', 'real-native']]) {
      sql.exec("UPDATE fleet_launches SET state='failed', verification_status=?, native_id=? WHERE id=?", status, native, launch);
      expect((await s.handle(req(`/api/sessions/${session.id}/stop`, { key: 'unsafe' }), admin)).status).toBe(409);
    }
    expect(sql.exec('SELECT session_id FROM session_control_cancellations').toArray()).toHaveLength(0);
    expect((await f.handle(req(`/api/fleet/launches/${launch}/result`, { state: 'failed', confirmed_exit: true, reason: 'launcher_not_started', launcher_task_id: 'hub-' + launch }),
      { caller: { ...host.caller, machineId: 'foreign' }, scopes: ['host'] })).status).toBe(404);
  }));

  it('does not strand a native claim that wins while stop input is streaming', () => fixture(async (s, f, sql, session, launch) => {
    sql.exec("UPDATE fleet_launches SET state='failed', verification_status='not_started' WHERE id=?", launch);
    await s.handle(req(`/api/sessions/${session.id}`, undefined, 'GET'), admin);
    let controller: ReadableStreamDefaultController;
    const stream = new ReadableStream({ start(c) { controller = c; } });
    const pending = s.handle(new Request(`https://hub.test/api/sessions/${session.id}/stop`, { method: 'POST', body: stream }), admin);
    await Promise.resolve();
    sql.exec("UPDATE fleet_launches SET state='starting', verification_status=NULL WHERE id=?", launch);
    controller!.enqueue(new TextEncoder().encode('{"key":"race"}')); controller!.close();
    expect((await pending).status).toBe(409);
    expect(sql.exec('SELECT session_id FROM session_control_cancellations').toArray()).toHaveLength(0);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch).one().state).toBe('starting');
  }));
});
