import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { NodeSqlStorage, NodeDurableStorage } from '../node/sqlite-storage.ts';
import { ProjectDO } from '../src/project-do';
import { ControlDO } from '../src/control-do';

const admin = { id: 'human:disk-owner', kind: 'operator', scopes: ['operator:admin'] };
const candidates = [
  { id: 'sol-route', engine: 'codex', model: 'gpt-6.1-sol', effort: 'medium', account_id: 'codex-private', weight: 1, capabilities: [], enabled: true },
];

test('disk_below_floor launch refusal queues/backpressures intake instead of failing and resumes via alarm', async () => {
  const parent = resolve('.local/tmp');
  mkdirSync(parent, { recursive: true });
  const dir = mkdtempSync(join(parent, 'disk-backpressure-'));
  const controlSql = new NodeSqlStorage(join(dir, 'control.sqlite'));
  const sql = new NodeSqlStorage(join(dir, 'project.sqlite'));
  const offers: any[] = [];
  let control: ControlDO;
  const namespace = (handle: (id: string, req: Request) => Promise<Response>) => ({
    idFromName: (id: string) => id,
    get: (id: string) => ({ fetch: (input: string | Request, init?: RequestInit) => handle(id, input instanceof Request ? input : new Request(input, init)) }),
  });
  const env: any = {
    CONTROL: namespace(async (_id, r) => {
      const path = new URL(r.url).pathname;
      if (path === '/api/directory') return Response.json({ items: [{ id: 'm1' }], next: null });
      if (path === '/_routing-policy') return Response.json({ version: 1, candidates });
      if (path === '/_internal/picker/draw') return control.fetch(r);
      return Response.json({ registered: true, accepted: true });
    }),
    MACHINES: namespace(async (id, r) => {
      const path = new URL(r.url).pathname;
      if (path === '/_internal/routing') return Response.json({ machine_id: id, connected: true, engines: ['codex'], capabilities: ['headless'] });
      const body = await r.json() as any;
      if (path === '/_internal/reserve') return Response.json({ state: 'staged', epoch: body.attempt + 1 });
      if (path === '/_internal/activate') { offers.push(body); return Response.json({ state: 'offered', epoch: body.attempt + 1 }); }
      return Response.json({ ok: true });
    }),
    ACCOUNTS: namespace(async () => Response.json({
      account: { enabled: true, min_remaining: 0 }, credential_usable: true, credential_status: 'ready',
      quota: { remaining: 80, captured_at: Date.now(), valid_until: Date.now() + 900000 }, fresh: true, hosts: ['m1'],
    })),
    MAILBOXES: namespace(async () => Response.json({ message: { id: 'm-1' } }, { status: 201 })),
  };
  control = new ControlDO({ storage: new NodeDurableStorage(controlSql), blockConcurrencyWhile: (fn: any) => Promise.resolve().then(fn) } as any, env);
  const project = new ProjectDO({ id: { toString: () => 'PROJECTS/org/demo' }, storage: new NodeDurableStorage(sql), blockConcurrencyWhile: (fn: any) => Promise.resolve().then(fn) } as any, env);

  try {
    const create = await project.fetch(new Request('https://project.internal/api/intakes?project=org/demo', {
      method: 'POST',
      headers: { 'x-hub2-actor': JSON.stringify(admin), 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: 'create-1', title: 'Disk backpressure test', text: 'Verify queue on low disk', acceptance_criteria: ['AC1'], code_task: false }),
    }));
    assert.equal(create.status, 201);
    const { intake } = await create.json() as any;

    const start = await project.fetch(new Request(`https://project.internal/api/intakes/${intake.id}/start`, {
      method: 'POST',
      headers: { 'x-hub2-actor': JSON.stringify(admin), 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: 'start-1', workspace: '/srv/demo', prompt: 'run', machine_id: 'm1', engine: 'codex', model: 'gpt-6.1-sol' }),
    }));
    assert.ok(start.status === 201 || start.status === 202);
    if (offers.length === 0) {
      sql.exec('UPDATE dispatch_sagas SET next_action_at=? WHERE intake_id=?', Date.now() - 1000, intake.id);
      await project.alarm();
    }
    assert.equal(offers.length, 1);

    const saga = sql.exec('SELECT * FROM dispatch_sagas WHERE intake_id=?', intake.id).toArray()[0] as any;
    const stopRes = await project.fetch(new Request('https://project.internal/_internal/reaped', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        operation_id: saga.operation_id,
        attempt_operation_id: saga.attempt_operation_id,
        session_id: saga.session_id,
        machine_id: 'm1',
        epoch: saga.epoch,
        reason: 'host_launch_refused',
        failure_reason: 'disk_below_floor',
        launch_failed: true,
        reaped: true,
        native_id: null,
        observed_at: Date.now(),
      }),
    }));
    assert.equal(stopRes.status, 200, await stopRes.clone().text());
    const stopJson = await stopRes.json() as any;
    assert.equal(stopJson.queued, true);
    assert.equal(stopJson.backpressure, true);

    const after = sql.exec('SELECT status FROM intakes WHERE id=?', intake.id).toArray()[0] as any;
    assert.equal(after.status, 'queued');
    const sagaAfter = sql.exec('SELECT state, attempt, due, last_reason FROM dispatch_sagas WHERE operation_id=?', saga.operation_id).toArray()[0] as any;
    assert.equal(sagaAfter.state, 'pending_machine');
    assert.equal(sagaAfter.attempt, 1);
    assert.equal(sagaAfter.due, 1);
    assert.equal(sagaAfter.last_reason, 'disk_below_floor');

    sql.exec('UPDATE dispatch_sagas SET next_action_at=? WHERE operation_id=?', Date.now() - 1000, saga.operation_id);
    await project.alarm();
    assert.equal(offers.length, 2);
    assert.equal(offers[1].operation_id, `${saga.operation_id}:1`);
  } finally {
    sql.close();
    controlSql.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
