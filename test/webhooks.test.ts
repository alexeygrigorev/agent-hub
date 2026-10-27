import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { renderTemplate, WebhookService } from '../src/webhooks';
import { doFetch } from './m1-focus-setup';

const defaultEnv = env as unknown as Env;
const ADMIN_TOKEN = 'op-token-webhooks-test';
const ADMIN_HEADER = { Authorization: `Bearer ${ADMIN_TOKEN}`, 'Content-Type': 'application/json' };

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function hmacHex(key: string, text: string): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signature = await crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(text));
  return [...new Uint8Array(signature)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// Inbound webhooks -> session delivery (intake 19ec91c2): per-route auth
// (token/HMAC), rate limit, size cap, idempotent durable queue reusing the
// session-control send path with the ack ledger as delivery proof.
describe('webhook routes admin API', () => {
  let stub: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha256Hex(ADMIN_TOKEN) } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { instance.sql.exec(trimmed); } catch (e) {} }
      }
      for (const table of ['webhook_routes', 'webhook_rate_buckets', 'session_deliveries',
        'session_control_sessions', 'session_control_commands', 'audit', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });

  const admin = (method: string, path: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method, headers: ADMIN_HEADER, body: body === undefined ? undefined : JSON.stringify(body),
  }));

  async function createRoute(overrides: Record<string, unknown> = {}) {
    const res = await admin('POST', '/api/webhooks', {
      name: 'oncall-dev', target_label: 'oncall-test', ...overrides,
    });
    expect(res.status).toBe(201);
    const created: any = await res.json();
    expect(created.secret).toMatch(/^whsec_/);
    return created;
  }

  const SESSION_ID = 'a1b2c3d4-0001-4000-8000-000000000001';

  async function seedSession(instance: Hub, name: string, state = 'running') {
    const now = new Date().toISOString();
    instance.sql.exec(
      `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
       VALUES (?, 'human:op', 'k-0001', ?, 'interactive', ?, ?, ?)`,
      SESSION_ID, JSON.stringify({ name }), state, now, now);
  }

  it('creates a route, stores only a hash, never shows the secret again', async () => {
    const created = await createRoute();
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec('SELECT * FROM webhook_routes WHERE id = ?', created.id).toArray()[0];
      expect(row.secret_storage.startsWith('sha256:')).toBe(true);
      expect(row.secret_storage).not.toContain(created.secret);
    });
    const list = await admin('GET', '/api/webhooks');
    const data: any = await list.json();
    expect(data.webhooks).toHaveLength(1);
    expect(JSON.stringify(data.webhooks[0])).not.toContain('secret');
    expect(JSON.stringify(data.webhooks[0])).not.toContain(created.secret);
    const detail = await admin('GET', '/api/webhooks/oncall-dev');
    expect(JSON.stringify(await detail.json() as any)).not.toContain(created.secret);
    const dup = await admin('POST', '/api/webhooks', { name: 'oncall-dev', target_label: 'x' });
    expect(dup.status).toBe(409);
  });

  it('rejects invalid create input', async () => {
    expect((await admin('POST', '/api/webhooks', { name: 'Bad Name', target_label: 'x' })).status).toBe(400);
    expect((await admin('POST', '/api/webhooks', { name: 'ok-name' })).status).toBe(400);
    expect((await admin('POST', '/api/webhooks', { name: 'ok-name2', target_label: 'x', auth_mode: 'magic' })).status).toBe(400);
    expect((await admin('POST', '/api/webhooks', { name: 'ok-name3', target_label: 'x', rate_limit_per_min: 0 })).status).toBe(400);
  });

  it('requires admin authority on the admin API', async () => {
    const res = await doFetch(new Request('http://example.com/api/webhooks', { method: 'GET' }));
    expect(res.status).toBe(401);
  });

  it('deletes a route and audits it', async () => {
    const created = await createRoute({ name: 'doomed' });
    const del = await admin('DELETE', `/api/webhooks/${created.id}`);
    expect((await del.json() as any).deleted).toBe(true);
    const list = await admin('GET', '/api/webhooks');
    expect((await list.json() as any).webhooks).toHaveLength(0);
    await runInDurableObject(stub, async (instance: Hub) => {
      const audits = instance.sql.exec("SELECT action FROM audit WHERE resource = 'webhook'").toArray() as any[];
      expect(audits.map(a => a.action)).toEqual(expect.arrayContaining(['webhook.create', 'webhook.delete']));
    });
  });
});

describe('webhook ingest auth and limits', () => {
  let stub: any;
  let created: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha256Hex(ADMIN_TOKEN) } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { instance.sql.exec(trimmed); } catch (e) {} }
      }
      for (const table of ['webhook_routes', 'webhook_rate_buckets', 'session_deliveries',
        'session_control_sessions', 'session_control_commands', 'audit', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
    const res = await doFetch(new Request('http://example.com/api/webhooks', {
      method: 'POST', headers: ADMIN_HEADER, body: JSON.stringify({ name: 'ingest', target_label: 'nowhere' }),
    }));
    created = await res.json() as any;
  });

  const ingest = (path: string, body: string, headers: Record<string, string> = {}) =>
    doFetch(new Request(`http://example.com${path}`, { method: 'POST', headers, body }));

  it('accepts a valid token and queues a delivery', async () => {
    const res = await ingest(`/webhooks/${created.id}`, '{"hello":"world"}', { 'X-Webhook-Token': created.secret });
    expect(res.status).toBe(202);
    const data: any = await res.json();
    expect(data.replay).toBe(false);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      expect(row.body).toBe('{"hello":"world"}');
      expect(row.status).toBe('pending');
      expect(row.last_error).toBe('target_not_found');
    });
  });

  it('rejects missing and wrong tokens', async () => {
    expect((await ingest(`/webhooks/${created.id}`, 'x')).status).toBe(401);
    expect((await ingest(`/webhooks/${created.id}`, 'x', { 'X-Webhook-Token': 'whsec_wrong' })).status).toBe(401);
  });

  it('unknown and disabled routes are not found; GET is not allowed', async () => {
    expect((await ingest('/webhooks/00000000-0000-0000-0000-000000000000', 'x')).status).toBe(404);
    const get = await doFetch(new Request(`http://example.com/webhooks/${created.id}`));
    expect(get.status).toBe(405);
    await doFetch(new Request(`http://example.com/api/webhooks/${created.id}`, { method: 'DELETE', headers: ADMIN_HEADER }));
    expect((await ingest(`/webhooks/${created.id}`, 'x', { 'X-Webhook-Token': created.secret })).status).toBe(404);
  });

  it('enforces the payload size cap', async () => {
    const small = await doFetch(new Request('http://example.com/api/webhooks', {
      method: 'POST', headers: ADMIN_HEADER,
      body: JSON.stringify({ name: 'tiny', target_label: 'x', max_body_bytes: 1024 }),
    }));
    const tiny: any = await small.json();
    expect((await ingest(`/webhooks/${tiny.id}`, 'a'.repeat(2000), { 'X-Webhook-Token': tiny.secret })).status).toBe(413);
  });

  it('enforces the per-minute rate limit with Retry-After', async () => {
    const res = await doFetch(new Request('http://example.com/api/webhooks', {
      method: 'POST', headers: ADMIN_HEADER,
      body: JSON.stringify({ name: 'throttled', target_label: 'x', rate_limit_per_min: 2 }),
    }));
    const route: any = await res.json();
    expect((await ingest(`/webhooks/${route.id}`, 'a', { 'X-Webhook-Token': route.secret })).status).toBe(202);
    expect((await ingest(`/webhooks/${route.id}`, 'b', { 'X-Webhook-Token': route.secret })).status).toBe(202);
    const third = await ingest(`/webhooks/${route.id}`, 'c', { 'X-Webhook-Token': route.secret });
    expect(third.status).toBe(429);
    expect(Number(third.headers.get('Retry-After'))).toBeGreaterThan(0);
  });

  it('replays the same delivery id without duplicating', async () => {
    const headers = { 'X-Webhook-Token': created.secret, 'X-Delivery-Id': 'dapier-mail-0001' };
    const first = await ingest(`/webhooks/${created.id}`, '{"n":1}', headers);
    const second = await ingest(`/webhooks/${created.id}`, '{"n":1}', headers);
    const a: any = await first.json();
    const b: any = await second.json();
    expect(b.delivery_id).toBe(a.delivery_id);
    expect(b.replay).toBe(true);
    await runInDurableObject(stub, async (instance: Hub) => {
      expect(instance.sql.exec('SELECT COUNT(*) AS n FROM session_deliveries').one().n).toBe(1);
    });
    expect((await ingest(`/webhooks/${created.id}`, 'x', { 'X-Webhook-Token': created.secret, 'X-Delivery-Id': 'bad id!' })).status).toBe(400);
  });

  it('authenticates HMAC signatures and rejects stale or wrong ones', async () => {
    const res = await doFetch(new Request('http://example.com/api/webhooks', {
      method: 'POST', headers: ADMIN_HEADER,
      body: JSON.stringify({ name: 'hmac-route', target_label: 'x', auth_mode: 'hmac' }),
    }));
    const route = await res.json() as any;
    const body = '{"event":"mail"}';
    const ts = String(Math.floor(Date.now() / 1000));
    const sig = 'sha256=' + await hmacHex(route.secret, `${ts}.${body}`);
    const ok = await ingest(`/webhooks/${route.id}`, body, { 'X-Webhook-Timestamp': ts, 'X-Webhook-Signature': sig });
    expect(ok.status).toBe(202);
    const badSig = await ingest(`/webhooks/${route.id}`, body, { 'X-Webhook-Timestamp': ts, 'X-Webhook-Signature': 'sha256=' + '0'.repeat(64) });
    expect(badSig.status).toBe(401);
    const staleTs = String(Math.floor(Date.now() / 1000) - 3600);
    const staleSig = 'sha256=' + await hmacHex(route.secret, `${staleTs}.${body}`);
    const stale = await ingest(`/webhooks/${route.id}`, body, { 'X-Webhook-Timestamp': staleTs, 'X-Webhook-Signature': staleSig });
    expect(stale.status).toBe(401);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec('SELECT secret_storage FROM webhook_routes WHERE id = ?', route.id).toArray()[0];
      expect(row.secret_storage.startsWith('xor:')).toBe(true);
      expect(row.secret_storage).not.toContain(route.secret);
    });
  });
});

describe('webhook delivery pump', () => {
  let stub: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha256Hex(ADMIN_TOKEN) } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { instance.sql.exec(trimmed); } catch (e) {} }
      }
      for (const table of ['webhook_routes', 'webhook_rate_buckets', 'session_deliveries',
        'session_control_sessions', 'session_control_commands', 'audit', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });

  const SESSION_ID = 'b2c3d4e5-0001-4000-8000-000000000001';

  async function setup(overrides: Record<string, unknown> = {}, sessionState = 'running') {
    return runInDurableObject(stub, async (instance: Hub) => {
      const now = new Date().toISOString();
      instance.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
         VALUES (?, 'human:op', 'k1', '{"name":"oncall-test"}', 'interactive', ?, ?, ?)`, SESSION_ID, sessionState, now, now);
      const res = await instance.webhookService.handle(new Request('http://hub.internal/api/webhooks', {
        method: 'POST', body: JSON.stringify({ name: 'dev-mail', target_label: 'oncall-test', ...overrides }),
      }), { caller: { kind: 'human', isAdmin: true, actor: 'human:test' }, scopes: ['admin'] });
      const created: any = await res.json();
      return created;
    });
  }

  const ingestRoute = async (route: any, body: string, headers: Record<string, string> = {}) => {
    const res = await doFetch(new Request(`http://example.com/webhooks/${route.id}`, {
      method: 'POST', headers: { 'X-Webhook-Token': route.secret, ...headers }, body,
    }));
    return { status: res.status, data: await res.json() as any };
  };

  it('delivers through a session-control send command and follows the ack ledger', async () => {
    const route = await setup({ template: 'dev@ alert: {{payload.subject}}' });
    const { data } = await ingestRoute(route, '{"subject":"disk 90%"}');
    expect(data.status).toBe('queued');
    await runInDurableObject(stub, async (instance: Hub) => {
      const delivery: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      expect(delivery.session_id).toBe('b2c3d4e5-0001-4000-8000-000000000001');
      const command: any = instance.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', delivery.command_id).toArray()[0];
      expect(command.kind).toBe('send');
      expect(command.request_key).toBe(`wd-${delivery.id}`);
      expect(JSON.parse(command.payload_json).body).toBe('dev@ alert: disk 90%');
      // Host acks delivered -> the delivery follows the ack ledger.
      instance.sql.exec("UPDATE session_control_commands SET state = 'acked', ack_json = ? WHERE id = ?",
        JSON.stringify({ outcome: 'delivered', native: { native_id: 'n1', tag: 't', workspace: '/w' } }), command.id);
      await instance.webhookService.pump();
      const done: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      expect(done.status).toBe('delivered');
      expect(done.delivered_at).toBeTruthy();
      const audits = instance.sql.exec("SELECT action FROM audit WHERE resource = 'webhook'").toArray() as any[];
      expect(audits.map(a => a.action)).toContain('webhook.deliver');
    });
  });

  it('holds and redelivers when the target session is not bound yet', async () => {
    const route = await setup({}, 'offered');
    const { data } = await ingestRoute(route, 'hello');
    expect(data.status).toBe('pending');
    await runInDurableObject(stub, async (instance: Hub) => {
      const delivery: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      expect(delivery.last_error).toBe('session_not_bound');
      expect(delivery.attempts).toBe(1);
      // The session comes back: due time reached -> queued.
      instance.sql.exec("UPDATE session_control_sessions SET state = 'running' WHERE id = 'b2c3d4e5-0001-4000-8000-000000000001'");
      instance.sql.exec("UPDATE session_deliveries SET next_attempt_at = ? WHERE id = ?", new Date(Date.now() - 1000).toISOString(), delivery.id);
      await instance.webhookService.pump();
      const queued: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', delivery.id).toArray()[0];
      expect(queued.status).toBe('queued');
    });
  });

  it('retries after a refused ack and fails honestly after the bounded attempts', async () => {
    const route = await setup();
    const { data } = await ingestRoute(route, 'hello');
    expect(data.status).toBe('queued');
    await runInDurableObject(stub, async (instance: Hub) => {
      const delivery: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      instance.sql.exec("UPDATE session_control_commands SET state = 'acked', ack_json = ? WHERE id = ?",
        JSON.stringify({ outcome: 'refused', reason: 'target_busy' }), delivery.command_id);
      await instance.webhookService.pump();
      const retried: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', delivery.id).toArray()[0];
      expect(retried.status).toBe('pending');
      expect(retried.attempts).toBe(1);
      expect(retried.last_error).toBe('refused:target_busy');
      // Bring the retry budget to the edge (absence of the target retries
      // longer by design; refusals are bounded), redeliver once, refuse again.
      instance.sql.exec("UPDATE session_deliveries SET attempts = 4, next_attempt_at = ? WHERE id = ?",
        new Date(Date.now() - 1000).toISOString(), delivery.id);
      await instance.webhookService.pump();
      const redelivered: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', delivery.id).toArray()[0];
      expect(redelivered.status).toBe('queued');
      instance.sql.exec("UPDATE session_control_commands SET state = 'acked', ack_json = ? WHERE id = ?",
        JSON.stringify({ outcome: 'refused', reason: 'target_busy' }), redelivered.command_id);
      await instance.webhookService.pump();
      const failed: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', delivery.id).toArray()[0];
      expect(failed.status).toBe('failed');
      const audits = instance.sql.exec("SELECT action FROM audit WHERE resource = 'webhook'").toArray() as any[];
      expect(audits.map(a => a.action)).toContain('webhook.fail');
    });
  });

  it('treats recipient_not_ready as absence: redelivers past the transport budget (AC2, live finding)', async () => {
    const route = await setup();
    const { data } = await ingestRoute(route, 'busy session');
    expect(data.status).toBe('queued');
    await runInDurableObject(stub, async (instance: Hub) => {
      const read = () => instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0] as any;
      const ackFailed = (row: any) => instance.sql.exec("UPDATE session_control_commands SET state = 'acked', ack_json = ? WHERE id = ?",
        JSON.stringify({ outcome: 'failed', reason: 'recipient_not_ready' }), row.command_id);
      // Attempt 1: the live busy session acks its send command failed.
      ackFailed(read());
      await instance.webhookService.pump();
      let row: any = read();
      expect(row.status).toBe('pending');
      expect(row.attempts).toBe(1);
      // Attempts 2-5: each redelivery is refused the same way and must stay
      // pending — the session exists and can come back, so recipient_not_ready
      // rides the absence budget, not the 5-attempt transport budget.
      for (let attempt = 2; attempt <= 5; attempt++) {
        instance.sql.exec('UPDATE session_deliveries SET attempts = ?, next_attempt_at = ? WHERE id = ?',
          attempt - 1, new Date(Date.now() - 1000).toISOString(), data.delivery_id);
        await instance.webhookService.pump();
        row = read();
        expect(row.status).toBe('queued');
        ackFailed(row);
        await instance.webhookService.pump();
        row = read();
        expect(row.status).toBe('pending');
        expect(row.attempts).toBe(attempt);
      }
      const audits = instance.sql.exec("SELECT action FROM audit WHERE resource = 'webhook' AND action = 'webhook.fail'").toArray() as any[];
      expect(audits).toHaveLength(0);
      // The long uncertain variant from the live system is absence too, and
      // the absence budget still terminates honestly at 96 attempts.
      instance.sql.exec('UPDATE session_deliveries SET attempts = 95, next_attempt_at = ? WHERE id = ?',
        new Date(Date.now() - 1000).toISOString(), data.delivery_id);
      await instance.webhookService.pump();
      row = read();
      instance.sql.exec("UPDATE session_control_commands SET state = 'acked', ack_json = ? WHERE id = ?",
        JSON.stringify({ outcome: 'uncertain', reason: 'recipient_not_ready_uncertain: inbox envelope still undelivered; removal unproven' }), row.command_id);
      await instance.webhookService.pump();
      row = read();
      expect(row.status).toBe('failed');
      expect(row.attempts).toBe(96);
    });
  });

  it('fails permanently on an ambiguous target, retries absent targets longer', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const now = new Date().toISOString();
      instance.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
         VALUES ('c3d4e5f6-0001-4000-8000-000000000002', 'human:a', 'k2', '{"name":"dupe"}', 'interactive', 'running', ?, ?)`, now, now);
      instance.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
         VALUES ('d4e5f6a7-0001-4000-8000-000000000003', 'human:b', 'k3', '{"name":"dupe"}', 'interactive', 'running', ?, ?)`, now, now);
      const res = await instance.webhookService.handle(new Request('http://hub.internal/api/webhooks', {
        method: 'POST', body: JSON.stringify({ name: 'ambig', target_label: 'dupe' }),
      }), { caller: { kind: 'human', isAdmin: true, actor: 'human:test' }, scopes: ['admin'] });
      const route = await res.json();
      const test = await instance.webhookService.handle(new Request('http://hub.internal/api/webhooks/ambig/test', {
        method: 'POST', body: JSON.stringify({ payload: 'x' }),
      }), { caller: { kind: 'human', isAdmin: true, actor: 'human:test' }, scopes: ['admin'] });
      expect(test.status).toBe(202);
      const delivery: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE source = ?', 'test').toArray()[0];
      expect(delivery.status).toBe('failed');
      expect(delivery.last_error).toBe('ambiguous_target');
      void route;
    });
  });

  it('fails pending deliveries once their route is deleted', async () => {
    const route = await setup({}, 'offered');
    const { data } = await ingestRoute(route, 'hello');
    expect(data.status).toBe('pending');
    await doFetch(new Request(`http://example.com/api/webhooks/${route.id}`, { method: 'DELETE', headers: ADMIN_HEADER }));
    await runInDurableObject(stub, async (instance: Hub) => {
      await instance.webhookService.pump();
      const delivery: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', data.delivery_id).toArray()[0];
      expect(delivery.status).toBe('failed');
      expect(delivery.last_error).toBe('route_deleted');
    });
  });

  it('supports an operator test injection through the same queue', async () => {
    const route = await setup();
    const res = await doFetch(new Request(`http://example.com/api/webhooks/${route.id}/test`, {
      method: 'POST', headers: ADMIN_HEADER, body: JSON.stringify({ payload: { ping: true } }),
    }));
    const data: any = await res.json();
    expect(res.status).toBe(202);
    expect(data.status).toBe('queued');
    const detail = await doFetch(new Request(`http://example.com/api/webhooks/${route.id}`, { headers: ADMIN_HEADER }));
    const view: any = await detail.json();
    expect(view.deliveries).toHaveLength(1);
    expect(view.deliveries[0].source).toBe('test');
  });
});

describe('template rendering', () => {
  it('defaults to the raw payload', () => {
    expect(renderTemplate(null, '{"a":1}').text).toBe('{"a":1}');
  });
  it('renders fields, pretty JSON and reports missing fields', () => {
    const r = renderTemplate('S: {{payload.subject}}\nFrom: {{payload.from.name}}\nFull: {{payload_json}}\nGone: {{payload.missing}}', '{"subject":"s","from":{"name":"ops"}}');
    expect(r.text).toContain('S: s');
    expect(r.text).toContain('From: ops');
    expect(r.text).toContain('"subject": "s"');
    expect(r.text).toContain('Gone:');
    expect(r.warnings).toContain('missing_field:missing');
  });
  it('drops unknown placeholders and truncates long renders honestly', () => {
    const unknown = renderTemplate('a {{bogus}} b', 'x');
    expect(unknown.text).toBe('a  b');
    expect(unknown.warnings[0]).toContain('unknown_placeholder');
    const long = renderTemplate(null, 'x'.repeat(17000));
    expect(long.text.length).toBe(16000 + '\n[hub: truncated after 16000 characters]'.length);
    expect(long.text.endsWith('[hub: truncated after 16000 characters]')).toBe(true);
  });
  it('extracts array indices', () => {
    const r = renderTemplate('{{payload.items.1}}', '{"items":["a","b"]}');
    expect(r.text).toBe('b');
  });
  it('delivers payload content containing braces verbatim (review finding 3)', () => {
    const r = renderTemplate('Alert {{payload.subject}}: {{payload}}', '{"subject":"ci","body":"vue {{count}} mustache {{payload}} literals"}');
    expect(r.text).toBe('Alert ci: {"subject":"ci","body":"vue {{count}} mustache {{payload}} literals"}');
    expect(r.warnings).toEqual([]);
  });
  it('reports and strips unknown placeholders even when the payload has braces', () => {
    const r = renderTemplate('{{naem}} {{payload}}', '{"text":"has {{braces}} inside"}');
    expect(r.text).toBe(' {"text":"has {{braces}} inside"}');
    expect(r.warnings).toContain('unknown_placeholder:naem');
    expect(r.warnings).not.toContain('unknown_placeholder:braces');
  });
});

describe('webhook pump concurrency guard', () => {
  it('coalesces a second pump pass instead of double-sending (review finding 2)', async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    const stub = defaultEnv.HUB.get(id);
    const SESSION_ID = 'a1b2c3d4-0002-4000-8000-000000000002';
    let sendCalls = 0;
    let gate: ((response: Response) => void) | null = null;
    const result = await runInDurableObject(stub, async (instance: Hub) => {
      const now = new Date().toISOString();
      instance.sql.exec(
        `INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at)
         VALUES (?, 'human:op', 'k-0002', ?, 'interactive', 'running', ?, ?)`,
        SESSION_ID, JSON.stringify({ name: 'pump-target' }), now, now);
      instance.sql.exec(
        `INSERT INTO session_deliveries (id, source, source_id, delivery_key, target_label, body, status, attempts, next_attempt_at, created_at, updated_at)
         VALUES ('wd-conc-1', 'test', 'route-conc', 'k-1', ?, 'hello', 'pending', 0, ?, ?, ?)`,
        SESSION_ID, now, now, now);
      let releaseCount = 0;
      const gated = { handle: async () => {
        sendCalls += 1;
        releaseCount += 1;
        return new Promise<Response>(resolve => { gate = resolve; });
      } };
      const service = new WebhookService(instance.sql, fn => fn(), () => Date.now(), () => gated, null);
      const first = service.pump();
      while (sendCalls === 0) await new Promise(resolve => setTimeout(resolve, 2));
      const second = await service.pump();
      expect(second).toBeUndefined();
      expect(sendCalls).toBe(1);
      expect(releaseCount).toBe(1);
      gate!(new Response(JSON.stringify({ error: 'invalid_body' }), { status: 400 }));
      return await first;
    });
    expect(result).toBeUndefined();
    expect(sendCalls).toBe(1);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec("SELECT * FROM session_deliveries WHERE id = 'wd-conc-1'").toArray()[0];
      expect(row.status).toBe('failed');
      expect(row.attempts).toBe(0); // permanent body failure is never retried
      expect(row.last_error).toBe('send_400:invalid_body');
    });
  });
});
