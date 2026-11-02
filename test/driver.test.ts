import { env, runInDurableObject } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import { DriverService } from '../src/driver';
import { DRIVER_PING, DRIVER_PROMPT } from '../src/driver-prompt';
import { Hub } from '../src/hub';
import { Env } from '../src/env';
import { SessionControlService } from '../src/session_control';

const testEnv = env as unknown as Env;
const admin = { caller: { isAdmin: true, actor: 'driver-test-admin', kind: 'human' }, scopes: ['admin'] };
const uuid = () => crypto.randomUUID();
const req = (path: string, body?: any, method = body === undefined ? 'GET' : 'POST') =>
  new Request('https://hub.test' + path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

async function withDriver(fn: (ctx: { hub: Hub; state: DurableObjectState; service: DriverService; starts: any[];
  sessions: Map<string, any>; authActors: string[];
  sessionHandler: (request: Request, auth: any) => Promise<Response>;
  scheduler: (request: Request, auth: any) => Promise<Response> }) => Promise<void>) {
  const stub = testEnv.HUB.get(testEnv.HUB.idFromName('driver-' + uuid()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const starts: any[] = [];
    const sessions = new Map<string, any>();
    const authActors: string[] = [];
    const byKey = new Map<string, string>();
    const sessionHandler = async (request: Request, auth: any) => {
      const path = new URL(request.url).pathname;
      if (path === '/api/sessions' && request.method === 'POST') {
        const input: any = await request.json();
        starts.push(input);
        authActors.push(auth?.caller?.actor ?? '');
        let id = byKey.get(input.key);
        if (!id) {
          id = uuid();
          byKey.set(input.key, id);
          sessions.set(id, { id, state: 'running', mode: input.mode, request_key: input.key });
        }
        return Response.json(sessions.get(id), { status: 201 });
      }
      const match = /^\/api\/sessions\/([^/]+)(?:\/(stop))?$/.exec(path);
      if (!match) return Response.json({ error: 'not_found' }, { status: 404 });
      const session = sessions.get(match[1]);
      if (!session) return Response.json({ error: 'not_found' }, { status: 404 });
      if (match[2] === 'stop' && request.method === 'POST') {
        session.state = 'stopped';
        return Response.json({ state: session.state });
      }
      if (request.method === 'GET' && !match[2]) return Response.json(session);
      return Response.json({ error: 'method_not_allowed' }, { status: 405 });
    };
    const scheduler = (request: Request, auth: any) => hub.sessionSchedules.handle(request, auth);
    const service = new DriverService(hub.sql,
      (request, auth) => sessionHandler(request, auth), scheduler);
    service.init();
    await fn({ hub, state, service, starts, sessions, authActors, sessionHandler, scheduler });
  });
}

async function start(service: DriverService, key = 'driver-start', extra: any = {}) {
  return service.handle(req('/api/driver/start', { key, ...extra }), admin);
}

describe('singleton Hub driver', () => {
  it('starts interactively, queues the standing order, and reuses the shared 15-minute session schedule', async () => {
    await withDriver(async ({ hub, service, starts }) => {
      const response = await start(service, 'driver-codex', { engine: 'codex', host: 'machine-test' });
      const body: any = await response.json();
      expect(response.status).toBe(202);
      expect(body.driver.phase).toBe('active');
      expect(body.session).toMatchObject({ state: 'running', mode: 'interactive' });
      expect(body.schedule).toMatchObject({
        name: 'hub-driver-' + body.driver.id,
        target_label: body.session.id,
        cron: '*/15 * * * *', tz: 'UTC', prompt_id: null,
        prompt_bytes: DRIVER_PING.length, enabled: true,
        created_by: 'driver-test-admin',
      });
      expect(starts).toHaveLength(1);
      expect(starts[0]).toMatchObject({ mode: 'interactive', engine: 'codex', machine: 'machine-test' });
      expect(starts[0].brief).toBeUndefined();
      expect(starts[0].with_goal).toBeUndefined();
      const delivery: any = hub.sql.exec("SELECT body,status FROM session_deliveries WHERE source='driver' AND delivery_key='startup'").toArray()[0];
      expect(delivery).toEqual({ body: DRIVER_PROMPT, status: 'pending' });

      // The existing session scheduler advances due UTC cron rows and inserts
      // the ping into its durable delivery queue, without starting a new agent.
      hub.sql.exec('UPDATE session_schedules SET next_due_ms=? WHERE id=?', Date.now() - 1000, body.driver.schedule_id);
      expect(await hub.sessionSchedules.tick()).toBe(1);
      const ping: any = hub.sql.exec("SELECT body,target_label,status FROM session_deliveries WHERE source='schedule' AND source_id=?", body.driver.schedule_id).toArray()[0];
      expect(ping).toMatchObject({ body: DRIVER_PING, target_label: body.session.id, status: 'pending' });
    });
  });

  it('serializes starts and reuses one live driver unless force is explicit', async () => {
    await withDriver(async ({ hub, service, starts }) => {
      const results = await Promise.all([
        start(service, 'driver-start-a'),
        start(service, 'driver-start-b'),
      ]);
      const bodies = await Promise.all(results.map(r => r.json() as Promise<any>));
      expect(bodies.some(b => b.reused)).toBe(true);
      expect(starts).toHaveLength(1);
      expect(hub.sql.exec('SELECT id FROM hub_drivers').toArray()).toHaveLength(1);
      expect(hub.sql.exec('SELECT id FROM session_schedules').toArray()).toHaveLength(1);

      const replacement: any = await (await start(service, 'driver-start-forced', { force: true, engine: 'antigravity' })).json();
      expect(replacement.driver.id).not.toBe(bodies[0].driver?.id ?? bodies[1].driver.id);
      expect(starts).toHaveLength(2);
      expect(starts[1]).toMatchObject({ mode: 'interactive', engine: 'antigravity' });
      expect(hub.sql.exec('SELECT enabled FROM session_schedules ORDER BY rowid').toArray().map((r: any) => r.enabled)).toEqual([0, 1]);
    });
  });

  it('recovers a lost schedule-create response by validating the existing generation schedule', async () => {
    await withDriver(async ({ hub, starts }) => {
      let lost = true;
      const scheduler = async (request: Request, auth: any) => {
        const response = await hub.sessionSchedules.handle(request, auth);
        if (lost && request.method === 'POST' && new URL(request.url).pathname === '/api/session-schedules') {
          lost = false;
          return Response.json({ error: 'response_lost' }, { status: 503 });
        }
        return response;
      };
      const service = new DriverService(hub.sql, async request => {
        const path = new URL(request.url).pathname;
        if (path === '/api/sessions' && request.method === 'POST') {
          const input: any = await request.json();
          starts.push(input);
          const id = uuid();
          return Response.json({ id, mode: 'interactive', state: 'running' }, { status: 201 });
        }
        const id = path.slice('/api/sessions/'.length);
        return Response.json({ id, mode: 'interactive', state: 'running' });
      }, scheduler);
      service.init();
      const response = await start(service, 'driver-lost-schedule-response', { engine: 'claude' });
      const body: any = await response.json();
      expect(response.status).toBe(202);
      expect(body.schedule.enabled).toBe(true);
      expect(hub.sql.exec('SELECT id FROM session_schedules').toArray()).toHaveLength(1);
      expect(starts).toHaveLength(1);
    });
  });

  it('retries a transient schedule failure with the same generation and disables the ping on stop', async () => {
    await withDriver(async ({ hub, service, starts }) => {
      let unavailable = true;
      const sessions = new Map<string, any>();
      const scheduler = async (request: Request, auth: any) => {
        if (unavailable && request.method === 'POST' && new URL(request.url).pathname === '/api/session-schedules') {
          unavailable = false;
          return Response.json({ error: 'temporary' }, { status: 503 });
        }
        return hub.sessionSchedules.handle(request, auth);
      };
      const retrying = new DriverService(hub.sql, async request => {
        const path = new URL(request.url).pathname;
        if (path === '/api/sessions' && request.method === 'POST') {
          const input: any = await request.json();
          starts.push(input);
          const existing = hub.sql.exec('SELECT id FROM hub_drivers ORDER BY rowid DESC LIMIT 1').toArray()[0] as any;
          const id = existing ? 'session-' + existing.id : 'unused';
          const session = sessions.get(id) ?? { id, state: 'running', mode: 'interactive' };
          sessions.set(id, session);
          return Response.json(session, { status: 201 });
        }
        const match = /^\/api\/sessions\/([^/]+)(?:\/(stop))?$/.exec(path)!;
        const session = sessions.get(match[1]);
        if (match[2] === 'stop' && session) { session.state = 'stopped'; return Response.json({ state: 'stopped' }); }
        if (request.method === 'GET' && session) return Response.json(session);
        return Response.json({ error: 'not_found' }, { status: 404 });
      }, scheduler);
      retrying.init();

      const first = await start(retrying, 'driver-retry-same-key', { engine: 'codex' });
      expect(first.status).toBe(503);
      const second = await start(retrying, 'driver-retry-same-key', { engine: 'codex' });
      expect(second.status).toBe(202);
      const active: any = await second.json();
      expect(starts).toHaveLength(2); // canonical start replay uses the generation key
      expect(hub.sql.exec('SELECT id FROM hub_drivers').toArray()).toHaveLength(1);
      expect(hub.sql.exec('SELECT id FROM session_schedules').toArray()).toHaveLength(1);

      const stopped: any = await (await retrying.handle(req('/api/driver/stop', { key: 'driver-stop-key' }), admin)).json();
      expect(stopped.stopped).toBe(true);
      const schedule: any = hub.sql.exec('SELECT enabled FROM session_schedules WHERE id=?', active.driver.schedule_id).toArray()[0];
      expect(schedule.enabled).toBe(0);
    });
  });

  it('rejects a real SessionControl refused start without a delivery or schedule', async () => {
    await withDriver(async ({ hub, state }) => {
      const control = new SessionControlService(hub.sql, fn => state.storage.transactionSync(fn), Date.now,
        { reserve: () => ({ value: { error: 'placement_refused' }, status: 400 }) });
      control.init();
      const actual = new DriverService(hub.sql,
        (request, auth) => control.handle(request, auth),
        (request, auth) => hub.sessionSchedules.handle(request, auth));
      actual.init();

      const response = await start(actual, 'driver-real-refusal', { engine: 'codex' });
      const body: any = await response.json();
      expect(response.status).toBe(400);
      expect(body).toMatchObject({ error: 'driver_start_failed', detail: { state: 'refused' } });
      expect(hub.sql.exec('SELECT phase,session_id FROM hub_drivers').toArray()[0]).toMatchObject({ phase: 'rejected' });
      expect(hub.sql.exec("SELECT id FROM session_deliveries WHERE source='driver'").toArray()).toHaveLength(0);
      expect(hub.sql.exec('SELECT id FROM session_schedules').toArray()).toHaveLength(0);
    });
  });

  it('recovers a starting row under its original actor when a different admin retries with a new key', async () => {
    await withDriver(async ({ hub, starts, authActors, sessionHandler }) => {
      let unavailable = true;
      const scheduler = async (request: Request, auth: any) => {
        if (unavailable && request.method === 'POST' && new URL(request.url).pathname === '/api/session-schedules') {
          unavailable = false;
          return Response.json({ error: 'temporary' }, { status: 503 });
        }
        return hub.sessionSchedules.handle(request, auth);
      };
      const recovering = new DriverService(hub.sql, sessionHandler, scheduler);
      recovering.init();

      expect((await start(recovering, 'driver-recovery-a')).status).toBe(503);
      const adminB = { caller: { isAdmin: true, actor: 'driver-test-admin-b', kind: 'human' }, scopes: ['admin'] };
      const recovered = await recovering.handle(req('/api/driver/start', { key: 'driver-recovery-b' }), adminB);
      const body: any = await recovered.json();
      expect(recovered.status).toBe(202);
      expect(body.driver.phase).toBe('active');
      expect(starts).toHaveLength(2); // the canonical generation start is replayed
      expect(authActors).toEqual(['driver-test-admin', 'driver-test-admin']);
      expect(body.schedule.created_by).toBe('driver-test-admin');
      expect(hub.sql.exec('SELECT actor FROM hub_drivers').toArray()[0].actor).toBe('driver-test-admin');
    });
  });

  it('does not report a stopping generation as reusable', async () => {
    await withDriver(async ({ hub, service, starts }) => {
      const first: any = await (await start(service, 'driver-stopping')).json();
      hub.sql.exec("UPDATE hub_drivers SET phase='stopping' WHERE id=?", first.driver.id);
      await hub.sessionSchedules.handle(req('/api/session-schedules/' + first.driver.schedule_id + '/pause', {}), admin);

      const response = await start(service, 'driver-after-stopping');
      expect(response.status).toBe(409);
      expect(await response.json()).toMatchObject({ error: 'driver_stopping' });
      expect(starts).toHaveLength(1);
      expect(hub.sql.exec('SELECT id FROM hub_drivers').toArray()).toHaveLength(1);
    });
  });

  it('allows cookie-style admin stop to cancel an unbound offered session through SessionControlService', async () => {
    await withDriver(async ({ hub, state }) => {
      const control = new SessionControlService(hub.sql, fn => state.storage.transactionSync(fn), Date.now, {
        reserve: (input: any, auth: any) => {
          const id = 'driver-launch-' + uuid();
          const at = new Date().toISOString();
          hub.sql.exec(`INSERT INTO fleet_launches
            (id,actor,requester_kind,parent_id,summary,request_key,request_json,machine_id,engine,account,profile,mode,workspace,prompt,state,native_id,launcher_task_id,created_at,updated_at)
            VALUES(?,?,?,NULL,?,?,?,?,?,?,NULL,?,?,?,?,NULL,NULL,?,?)`,
            id, auth.caller.actor, 'human', 'driver test', input.idempotency_key, JSON.stringify(input), 'machine-test', 'codex', 'fixture',
            'interactive', 'agent-hub', '', 'offered', at, at);
          return { value: { id, state: 'offered', machine: 'machine-test', engine: 'codex' }, status: 201 };
        },
      });
      control.init();
      const actual = new DriverService(hub.sql,
        (request, auth) => control.handle(request, auth),
        (request, auth) => hub.sessionSchedules.handle(request, auth));
      actual.init();

      const created = await start(actual, 'driver-cookie-stop');
      expect(created.status).toBe(202);
      const body: any = await created.json();
      expect(body.session.state).toBe('offered');
      const stopped = await actual.handle(req('/api/driver/stop', { key: 'driver-cookie-stop-command' }), admin);
      expect(stopped.status).toBe(200);
      expect(await stopped.json()).toMatchObject({ stopped: true, session: { state: 'cancelled' } });
    });
  });

  it('keeps the driver standing order explicit about evidence and protected actions', () => {
    for (const phrase of [
      'inspect ALL intakes and sessions', 'Follow pagination to its end',
      'acceptance criteria', 'DONE note', 'Never close or delete intakes',
      'NEEDS_ALEXEY only for production approvals, secrets, deletions or 2FA',
      'Never approve production gates',
    ]) expect(DRIVER_PROMPT).toContain(phrase);
  });
});
