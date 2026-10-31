import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ProvisioningService } from '../src/provisioning';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
const admin = { caller: { isAdmin: true, kind: 'human', actor: 'goal-test' }, scopes: ['admin'] };
it('validates and carries interactive goal intent through session reservation and replay', async () => {
  const e = env as unknown as Env;
  await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub, state) => {
    const calls: any[] = [];
    const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), Date.now, { reserve: input => {
      calls.push(input); return { value: { id: 'goal-launch', state: 'offered', machine: 'hetzner', engine: 'codex' }, status: 201 };
    } });
    service.init();
    const input = { key: 'goal', workspace: 'demo', mode: 'interactive', with_goal: true, brief: 'Fix binding' };
    const send = (data: any) => service.handle(new Request('https://hub.test/api/sessions', { method: 'POST', body: JSON.stringify(data) }), admin);
    expect((await send(input)).status).toBe(201);
    expect(calls[0]).toMatchObject({ mode: 'interactive', with_goal: true, prompt: 'Fix binding' });
    expect((await send({ ...input, mode: 'headless' })).status).toBe(400);
    expect((await send({ ...input, brief: undefined })).status).toBe(400);
    expect((await send({ ...input, with_goal: false })).status).toBe(400);
    expect((await send({ ...input, with_goal: undefined })).status).toBe(400);
  });
});
it('advertises goal capability and filters non-Codex routes without requiring a machine pin', async () => {
  const e = env as unknown as Env;
  await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub, state) => {
    const s = new FleetService(hub.sql, cb => state.storage.transactionSync(cb)); s.init();
    for (const provider of ['codex', 'codex-orin', 'zai', 'claude', 'gemini', 'go']) {
      // Canonical provider model: a legacy `codex-orin` row reads as provider
      // `codex`, so both Codex accounts advertise goal support.
      expect(s.accountStatus({ label: provider, provider, max_concurrency: 1, reserve_percent: 15, enabled: true }).supports_goal).toBe(['codex', 'codex-orin', 'zai', 'claude'].includes(provider));
      expect(s.accountStatus({ label: provider, provider, max_concurrency: 1, reserve_percent: 15, enabled: true }).provider).toBe(provider === 'codex-orin' ? 'codex' : provider);
    }
    hub.sql.exec("INSERT INTO fleet_routes VALUES ('hetzner','claude','claude',NULL)");
    hub.sql.exec("INSERT INTO fleet_routes VALUES ('hetzner','gemini','gemini',NULL)");
    const p = s.plan({ mode: 'interactive', with_goal: true, workspace: 'demo', prompt: 'Fix binding' });
    expect(p.error).toBeUndefined();
    // Claude is goal-capable but this host advertised no goal route for it.
    expect(p.rejected).toContainEqual({ machine: 'hetzner', engine: 'claude', reason: 'goal_unsupported' });
    expect(p.rejected).toContainEqual({ machine: 'hetzner', engine: 'gemini', reason: 'goal_unsupported' });
    expect(s.plan({ mode: 'headless', with_goal: true, workspace: 'demo', prompt: 'Fix' }).error).toBe('invalid_launch_request');
    expect(s.plan({ mode: 'interactive', with_goal: true, workspace: 'demo', prompt: 42 }).error).toBe('invalid_launch_request');
  });
});

it('advertises supports_goal in hub engines list without exposing credentials', async () => {
  const e = env as unknown as Env;
  await runInDurableObject(e.HUB.get(e.HUB.idFromName(crypto.randomUUID())), async (hub: Hub, state) => {
    const service = new ProvisioningService(hub.sql, cb => state.storage.transactionSync(cb), {} as any, hub.fleetService, () => true);
    service.init();
    for (const provider of ['codex', 'zai', 'claude', 'gemini', 'go']) {
      hub.sql.exec('INSERT INTO engine_bundles VALUES (?,?,?,?,?)', provider, provider, '1', 'revision', 'private-marker');
    }
    const response = await service.handle(new Request('https://hub.test/api/engines'), admin);
    const data = await response.json() as any;
    for (const engine of data.engines) expect(engine.supports_goal).toBe(['codex', 'zai', 'claude'].includes(engine.provider));
    expect(JSON.stringify(data)).not.toContain('private-marker');
  });
});
