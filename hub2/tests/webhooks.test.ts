import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NodeSqlStorage } from '../node/sqlite-storage';
import { MeteredDb } from '../src/db';
import { Hub2Webhooks, HUB2_WEBHOOK_SCHEMA } from '../src/webhooks';
import type { Hub2Env } from '../src/types';
import { eligibleCandidates, type PickInput } from '../src/routing-pool';

function fixture() {
  const db = new MeteredDb(new NodeSqlStorage(':memory:') as unknown as import('@cloudflare/workers-types').SqlStorage);
  for (const statement of HUB2_WEBHOOK_SCHEMA) db.run(statement);
  let alarm: number | null = null;
  const ctx = { storage: { getAlarm: async () => alarm, setAlarm: async (at: number) => { alarm = at; }, transactionSync: (fn: () => unknown) => fn() } } as unknown as DurableObjectState;
  const calls: { body: Record<string, unknown>; draft: boolean | undefined; actor: unknown }[] = [];
  let available = true;
  let intake: Record<string, unknown> = { status: 'running' };
  const env = { PROJECTS: { idFromName: (name: string) => name, get: () => ({ fetch: async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith('/stats')) return Response.json({ stats: { first_session_started_at: 1795600000000 } });
    if (path.endsWith('/context')) return Response.json({ events: [{ kind: 'worker_append', body: { evidence: { kind: 'recording_preparation_drafts', delivery_id: db.one<{ id: string }>('SELECT id FROM session_deliveries')?.id, artifacts: [{ kind: 'video_chapters', path: '.local/drafts/chapters.md', sha256: 'a'.repeat(64) }, { kind: 'workshops_raw_summary', path: '.local/drafts/README.md', sha256: 'b'.repeat(64) }] } } }] });
    return Response.json({ intake });
  } }) } } as unknown as Hub2Env;
  const make = () => new Hub2Webhooks(ctx, db, env, async (request, _env, actor, draft) => {
    const body = await request.json() as Record<string, unknown>;
    calls.push({ body, draft, actor });
    // Exercise the actual selector contract: headless starts cannot request
    // the interactive-only built-in goal mode. A permissive start mock hid it.
    const candidates = eligibleCandidates({ version: 1, candidates: [{ id: 'claude-opus-high', engine: 'claude', model: 'opus', effort: 'high', account_id: 'claude', weight: 1, capabilities: ['headless', 'effort'], enabled: true }] },
      { ...body, interactive: body.mode === 'interactive', with_goal: body.with_goal === true } as PickInput);
    assert.equal(candidates.length, 1);
    if (!available) return Response.json({ error: 'no_available_host' }, { status: 503 });
    return Response.json({ id: 'session-1', intake_id: 'intake-1' }, { status: 202 });
  });
  let service = make();
  const admin = { id: 'human:test', kind: 'operator', scopes: ['operator:admin'] };
  const call = (path: string, method = 'GET', body?: unknown, headers = {}) => service.route(new Request('https://hub.test' + path, {
    method, headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(admin), ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }));
  return { db, calls, call, restart() { service = make(); }, service: () => service, setAvailable(value: boolean) { available = value; }, setIntake(value: Record<string, unknown>) { intake = value; } };
}
const launch = { project: 'AI-Shipping-Labs/website', workspace: '/work/drafts', machine: 'hetzner-aplexer' };
const create = async (f: ReturnType<typeof fixture>, extra = {}) => {
  const response = await f.call('/api/webhooks', 'POST', { name: 'aisl-drafts', launch, template: 'Prepare drafts only from {{payload.source_url}}', ...extra });
  assert.equal(response?.status, 201);
  return await response!.json() as { id: string; secret: string };
};

test('restored legacy authentication, durable queue and renderer launch one fixed draft job across retry/restart', async () => {
  const f = fixture(), route = await create(f);
  const headers = { 'X-Webhook-Token': route.secret, 'X-Delivery-Id': 'recording-69-transcript-1' };
  const payload = { source_url: 'https://aishippinglabs.com/api/events/example/preparation-source', machine: 'evil', brief: 'publish now' };
  assert.equal((await f.call('/webhooks/' + route.id, 'POST', payload))?.status, 401);
  const first = await f.call('/webhooks/' + route.id, 'POST', payload, headers);
  assert.equal(first?.status, 202);
  const receipt = await first!.json() as { delivery_id: string; status: string };
  assert.equal(receipt.status, 'queued');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].body.machine, 'hetzner-aplexer');
  assert.ok(String(f.calls[0].body.brief).startsWith('Prepare drafts only from ' + payload.source_url));
  assert.ok(String(f.calls[0].body.brief).includes(receipt.delivery_id));
  assert.equal(f.calls[0].draft, true);
  assert.equal(f.calls[0].body.purpose, 'implement');
  assert.equal(f.calls[0].body.model, 'opus');
  f.restart();
  const replay = await f.call('/webhooks/' + route.id, 'POST', payload, headers);
  assert.equal((await replay!.json() as { delivery_id: string }).delivery_id, receipt.delivery_id);
  assert.equal(f.calls.length, 1);
  const path = '/webhooks/' + route.id + '/deliveries/' + receipt.delivery_id;
  assert.equal((await f.call(path))?.status, 401);
  const running = await (await f.call(path, 'GET', undefined, { 'X-Webhook-Token': route.secret }))!.json() as Record<string, unknown>;
  assert.equal(running.status, 'running');
  assert.equal(running.started_at, '2026-11-25T09:46:40.000Z');
  f.setIntake({ status: 'accepted', updated_at: 1795600060000, dispatch: { native_id: 'native-1', updated_at: 1795600000000 }, artifacts: [{ name: 'chapters.md' }, { name: 'workshop.md' }] });
  const done = await (await f.call(path, 'GET', undefined, { 'X-Webhook-Token': route.secret }))!.json() as Record<string, unknown>;
  assert.equal(done.status, 'finished');
  assert.equal(done.finished_at, '2026-11-25T09:47:40.000Z');
  assert.equal((done.artifacts as unknown[]).length, 2);
});

test('unavailable host retries the same durable start id after restart; pending is never success', async () => {
  const f = fixture(), route = await create(f);
  f.setAvailable(false);
  const response = await f.call('/webhooks/' + route.id, 'POST', { source_url: 'https://example.test/source' }, { 'X-Webhook-Token': route.secret, 'X-Delivery-Id': 'retry-1' });
  const receipt = await response!.json() as { delivery_id: string; status: string };
  assert.equal(receipt.status, 'pending');
  assert.equal(f.calls.length, 1);
  f.restart(); f.setAvailable(true);
  f.db.run("UPDATE session_deliveries SET next_attempt_at='2000-01-01T00:00:00.000Z' WHERE id=?", receipt.delivery_id);
  await f.service().pump();
  assert.equal(f.calls.length, 2);
  assert.equal(f.db.one<{ status: string }>('SELECT status FROM session_deliveries WHERE id=?', receipt.delivery_id)?.status, 'queued');
  assert.equal(f.calls[0].body.name, f.calls[1].body.name);
});

test('management refuses nonadmin, invalid target and signing modes that Hub2 cannot safely restore', async () => {
  const f = fixture();
  assert.equal((await f.call('/api/webhooks', 'POST', { name: 'bad', launch }, { 'x-hub2-actor': JSON.stringify({ id: 'session:test', kind: 'agent', scopes: ['intake:write'] }) }))?.status, 403);
  assert.equal((await f.call('/api/webhooks', 'POST', { name: 'bad', launch: { ...launch, workspace: 'relative' } }))?.status, 400);
  assert.equal((await f.call('/api/webhooks', 'POST', { name: 'bad', launch, auth_mode: 'hmac' }))?.status, 400);
  const route = await create(f, { rate_limit_per_min: 1, max_body_bytes: 1024 });
  const listing = await (await f.call('/api/webhooks'))!.json();
  assert.equal(JSON.stringify(listing).includes(route.secret), false);
  assert.equal((await f.call('/webhooks/' + route.id, 'POST', { text: 'x'.repeat(2000) }, { 'X-Webhook-Token': route.secret }))?.status, 413);
});

test('actual Hub2 session-start bridge preserves a fixed target, durable key and draft-only intake classification', async () => {
  const { startSession } = await import('../src/worker');
  const calls: { path: string; body: Record<string, unknown>; actor: unknown }[] = [];
  const env = {
    CONTROL: { idFromName: (name: string) => name, get: () => ({ fetch: async () => Response.json({ repo: { canonical_name: launch.project, canonical_url: 'https://github.com/' + launch.project, status: 'active' } }) }) },
    PROJECTS: { idFromName: (name: string) => name, get: () => ({ fetch: async (request: Request) => {
      const path = new URL(request.url).pathname;
      calls.push({ path, body: await request.json() as Record<string, unknown>, actor: JSON.parse(request.headers.get('x-hub2-actor')!) });
      if (path.endsWith('/start')) return Response.json({ session: { id: 'session-started' } }, { status: 202 });
      return Response.json({ id: 'intake-created' }, { status: 201 });
    } }) },
  } as unknown as Hub2Env;
  const actor = { id: 'webhook:route', kind: 'agent' as const, scopes: ['session:start', 'intake:write', 'intake:read', 'project:' + launch.project] };
  const response = await startSession(new Request('https://hub.test/api/sessions', { method: 'POST', headers: { 'Idempotency-Key': 'webhook-fixed-delivery' },
    body: JSON.stringify({ ...launch, purpose: 'implement', model: 'opus', engine: 'claude', effort: 'high', brief: 'Prepare local drafts only.' }) }), env, actor, true);
  assert.equal(response.status, 202);
  assert.equal(calls[0].body.code_task, false);
  assert.equal(calls[0].body.key, 'webhook-fixed-delivery:intake');
  assert.equal(calls[1].body.key, 'webhook-fixed-delivery');
  assert.equal(calls[1].body.machine_id, launch.machine);
  assert.equal(calls[1].body.model, 'opus');
  assert.deepEqual(calls[1].actor, actor);
});

test('webhook dispatch passes actual Control repository authorization with only its configured project scope', async () => {
  const { ControlDO } = await import('../src/control-do');
  const { NodeDurableStorage } = await import('../node/sqlite-storage');
  const sql = new NodeSqlStorage(':memory:');
  const state = { storage: new NodeDurableStorage(sql), blockConcurrencyWhile: (fn: () => Promise<unknown>) => fn() } as unknown as DurableObjectState;
  const projectCalls: Record<string, unknown>[] = [];
  let control: InstanceType<typeof ControlDO>;
  const env = {
    CONTROL: { idFromName: (name: string) => name, get: () => ({ fetch: (request: Request) => control.fetch(request) }) },
    PROJECTS: { idFromName: (name: string) => name, get: () => ({ fetch: async (request: Request) => {
      projectCalls.push(await request.json() as Record<string, unknown>);
      if (new URL(request.url).pathname.endsWith('/start')) return Response.json({ session: { id: 'session-real-control' } }, { status: 202 });
      return Response.json({ id: 'intake-real-control' }, { status: 201 });
    } }) },
  } as unknown as Hub2Env;
  control = new ControlDO(state, env);
  const adminHeaders = { 'x-hub2-actor': JSON.stringify({ id: 'human:test', kind: 'operator', scopes: ['operator:admin'] }) };
  const repo = await control.fetch(new Request('https://control.test/api/repos/' + encodeURIComponent(launch.project), { method: 'PATCH', headers: adminHeaders,
    body: JSON.stringify({ canonical_url: 'https://github.com/' + launch.project, purpose: 'Draft job source' }) }));
  assert.equal(repo.status, 200);
  const created = await control.fetch(new Request('https://control.test/api/webhooks', { method: 'POST', headers: adminHeaders,
    body: JSON.stringify({ name: 'control-auth-drafts', launch, template: 'Prepare local drafts from {{payload.source_url}}' }) }));
  assert.equal(created.status, 201);
  const route = await created.json() as { id: string; secret: string };
  const accepted = await control.fetch(new Request('https://control.test/webhooks/' + route.id, { method: 'POST',
    headers: { 'X-Webhook-Token': route.secret, 'X-Delivery-Id': 'actual-control-1' }, body: JSON.stringify({ source_url: 'https://aishippinglabs.com/api/events/example/preparation-source' }) }));
  assert.equal(accepted.status, 202);
  const receipt = await accepted.json() as { status: string };
  assert.equal(receipt.status, 'queued');
  assert.equal(projectCalls.length, 2);
  assert.equal(projectCalls[0].code_task, false);
  sql.close();
});
