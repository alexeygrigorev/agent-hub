import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = { caller: { kind: 'agent', actor: 'demo:assistant', tokenId: 'cred-1' }, scopes: ['fleet:launch', 'bus:send'] };
const stranger = { caller: { kind: 'agent', actor: 'other:assistant', tokenId: 'cred-2' }, scopes: ['fleet:launch', 'bus:send'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const unauth = { error: Response.json({ error: 'unauthorized' }, { status: 401 }) } as any;

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}

/** Synthetic placement only: never starts a live agent. */
function fakeReserve(fail?: any) {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    if (fail) return { value: fail, status: 409 };
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService) => Promise<void>, reserve?: (input: any) => { value: any; status: number }) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now,
      reserve ? { reserve } : {});
    service.init();
    await fn(service);
  });
}

async function startOk(s: SessionControlService, extra: any = {}, auth: any = admin, key = crypto.randomUUID()) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo', ...extra }), auth);
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

describe('hub session control durable queue', () => {
  it('rejects unauthenticated and unscoped callers', async () => fixture(async (s) => {
    expect((await s.handle(request('/api/sessions', { key: 'k', workspace: 'demo' }), unauth)).status).toBe(401);
    expect((await s.handle(request('/api/sessions', { key: 'k', workspace: 'demo' }), host('hetzner'))).status).toBe(403);
    const scoped = await s.handle(request('/api/sessions', undefined, 'GET'), stranger);
    expect(scoped.status).toBe(200);
    expect((await scoped.json() as any).sessions).toEqual([]);
  }));

  it('validates start inputs without touching placement', async () => fixture(async (s) => {
    for (const payload of [
      { key: 'k1' }, // missing workspace
      { key: 'k2', workspace: 'demo', tier: 'ultra' },
      { key: 'k3', workspace: 'demo', cwd: '../escape' },
      { key: 'k4', workspace: 'demo', cwd: '/absolute' },
      { key: 'k5', workspace: 'demo', mode: 'interactive', brief: 'inject' },
      { key: 'k6', workspace: 'demo', brief: '' },
    ]) {
      expect((await s.handle(request('/api/sessions', payload), admin)).status, JSON.stringify(payload)).toBe(400);
    }
  }));

  it('holds honestly when placement is not wired', async () => fixture(async (s) => {
    const first = await s.handle(request('/api/sessions', { key: 'hold-1', workspace: 'demo' }), admin);
    expect(first.status).toBe(409);
    expect((await first.json() as any).hold).toBe('no_placement_authority');
    const replay = await s.handle(request('/api/sessions', { key: 'hold-1', workspace: 'demo' }), admin);
    expect(replay.status).toBe(409);
  }));

  it('starts through placement, replays same key, conflicts on changed intent', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const first: any = await startOk(s, { engine: 'codex', machine: 'hetzner' }, admin, 'start-1');
      expect(first.state).toBe('offered');
      expect(first.machine).toBe('hetzner');
      expect(fake.calls.length).toBe(1);
      const replay = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'codex', machine: 'hetzner' }), admin);
      expect(replay.status).toBe(200);
      expect((await replay.json() as any).replay).toBe(true);
      expect(fake.calls.length).toBe(1);
      const conflict = await s.handle(request('/api/sessions', { key: 'start-1', workspace: 'demo', engine: 'claude' }), admin);
      expect(conflict.status).toBe(409);
    }, fake.hook);
  });

  it('records held intent on placement failure without launching twice', async () => {
    const fake = fakeReserve({ error: 'no_eligible_route', rejected: [] });
    await fixture(async (s) => {
      const first = await s.handle(request('/api/sessions', { key: 'held-1', workspace: 'demo' }), admin);
      expect(first.status).toBe(409);
      expect((await first.json() as any).hold.error).toBe('no_eligible_route');
      const retry = await s.handle(request('/api/sessions', { key: 'held-1', workspace: 'demo' }), admin);
      expect(retry.status).toBe(409);
      expect(fake.calls.length).toBe(2);
    }, fake.hook);
  });

  it('refuses send/capture on unbound sessions', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, {}, admin, 'unbound-1');
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'a', body: 'hi' }), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'b' }), admin)).status).toBe(409);
    }, fake.hook);
  });

  it('enqueues send with idempotent keys and rejects conflicts', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, {}, admin, 'bound-1');
      expect(s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' })).toMatchObject({ ok: true });
      const first = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'prompt one' }), admin);
      expect(first.status).toBe(201);
      const replay = await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'prompt one' }), admin);
      expect(replay.status).toBe(200);
      expect((await replay.json() as any).replay).toBe(true);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'send-1', body: 'other' }), admin)).status).toBe(409);
      expect((await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'x', body: '' }), admin)).status).toBe(400);
    }, fake.hook);
  });

  it('isolates sessions and commands by owner', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, {}, agent, 'owner-1');
      expect((await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), stranger)).status).toBe(404);
      expect((await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), stranger)).status).toBe(404);
    }, fake.hook);
  });

  it('polls per-machine offers once and requires transport before ack', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'poll-1');
      s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' });
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'p1', body: 'go' }), admin);
      const foreign = await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('win35'));
      expect((await foreign.json() as any).offers).toEqual([]);
      const offers = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(offers.offers.length).toBe(1);
      expect(offers.offers[0].native_tag).toBe('hub-tag-1');
      const again = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      expect(again.offers).toEqual([]);
    }, fake.hook);
  });

  it('acks only with matching correlation and binding; stop needs confirmed exit', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'ack-1');
      s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' });
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 's1', body: 'go' }), admin);
      await s.handle(request(`/api/sessions/${session.id}/stop`, { key: 't1' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const send = poll.offers.find((o: any) => o.kind === 'send');
      const stop = poll.offers.find((o: any) => o.kind === 'stop');
      const native = { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' };
      const ackUrl = (id: string) => `/api/session-commands/${id}/ack`;
      expect((await s.handle(request(ackUrl(send.command_id), { correlation: 'wrong', outcome: 'delivered', native }), host('hetzner'))).status).toBe(409);
      expect((await s.handle(request(ackUrl(send.command_id), { correlation: send.correlation, outcome: 'delivered', native: { ...native, tag: 'other' } }), host('hetzner'))).status).toBe(409);
      expect((await s.handle(request(ackUrl(send.command_id), { correlation: send.correlation, outcome: 'delivered', native }), host('win35'))).status).toBe(404);
      expect((await s.handle(request(ackUrl(send.command_id), { correlation: send.correlation, outcome: 'delivered', native }), host('hetzner'))).status).toBe(200);
      expect((await s.handle(request(ackUrl(stop.command_id), { correlation: stop.correlation, outcome: 'delivered', native }), host('hetzner'))).status).toBe(409);
      const stopped = await s.handle(request(ackUrl(stop.command_id), { correlation: stop.correlation, outcome: 'delivered', native, confirmed_exit: true }), host('hetzner'));
      expect(stopped.status).toBe(200);
      const view = await (await s.handle(request(`/api/sessions/${session.id}`, undefined, 'GET'), admin)).json() as any;
      expect(view.state).toBe('stopped');
    }, fake.hook);
  });

  it('retains refused and uncertain commands without retry', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'uncertain-1');
      s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' });
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'u1', body: 'go' }), admin);
      await s.handle(request(`/api/sessions/${session.id}/send`, { key: 'u2', body: 'go' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const native = { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' };
      const [first, second] = poll.offers;
      expect((await s.handle(request(`/api/session-commands/${first.command_id}/ack`,
        { correlation: first.correlation, outcome: 'refused', native, reason: 'busy_composer' }), host('hetzner'))).status).toBe(200);
      expect((await s.handle(request(`/api/session-commands/${second.command_id}/ack`,
        { correlation: second.correlation, outcome: 'uncertain', native, reason: 'timeout' }), host('hetzner'))).status).toBe(200);
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(list.commands.map((c: any) => c.state).sort()).toEqual(['refused', 'uncertain']);
    }, fake.hook);
  });

  it('redacts capture text before storage', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, { machine: 'hetzner' }, admin, 'redact-1');
      s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' });
      await s.handle(request(`/api/sessions/${session.id}/capture`, { key: 'c1' }), admin);
      const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
      const cmd = poll.offers[0];
      const native = { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' };
      await s.handle(request(`/api/session-commands/${cmd.command_id}/ack`,
        { correlation: cmd.correlation, outcome: 'delivered', native, capture: 'leaked sk-abcdef0123456789abcdef' }), host('hetzner'));
      const list = await (await s.handle(request(`/api/sessions/${session.id}/commands`, undefined, 'GET'), admin)).json() as any;
      expect(JSON.stringify(list.commands[0].ack)).not.toContain('sk-abcdef');
    }, fake.hook);
  });

  it('refuses conflicting native bindings on tag reuse', async () => {
    const fake = fakeReserve();
    await fixture(async (s) => {
      const session: any = await startOk(s, {}, admin, 'rebind-1');
      expect(s.recordBinding(session.id, { native_id: 'native-1', tag: 'hub-tag-1', workspace: 'demo' })).toMatchObject({ ok: true });
      const second: any = await startOk(s, {}, admin, 'rebind-2');
      expect(s.recordBinding(second.id, { native_id: 'native-2', tag: 'hub-tag-1', workspace: 'demo' })).toMatchObject({ ok: true });
      // Same session rebound to a different native id is a conflict, never silent adoption.
      const direct = s.recordBinding(session.id, { native_id: 'native-9', tag: 'hub-tag-1', workspace: 'demo' });
      expect(direct).toMatchObject({ ok: false, error: 'binding_not_allowed' });
    }, fake.hook);
  });
});
