import { describe, it, expect } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import worker, { Hub } from '../src/worker';
import { ConversationHub, BaseHub } from '../src/conversation-hub';
import { createSessionCookie } from '../src/auth/session';

describe('production Hub composition', () => {
  it('exports the exact conversation class with inherited SQLite, RPC and lifecycle behavior', async () => {
    expect(Hub).toBe(ConversationHub);
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('hub'));
    await runInDurableObject(stub, async (hub: Hub) => {
      expect(hub).toBeInstanceOf(BaseHub);
      expect(hub.sql.exec("SELECT name FROM sqlite_master WHERE type='table'").toArray().map((r: any) => r.name)).toEqual(expect.arrayContaining(['agents', 'messages', 'conversations', 'conversation_messages']));
      for (const method of ['scheduledTick', 'runChecks', 'alarm', 'webSocketMessage', 'webSocketClose', 'webSocketError']) {
        expect((Hub.prototype as any)[method]).toBe((BaseHub.prototype as any)[method]);
      }
    });
    expect(Array.isArray(await stub.runChecks())).toBe(true);
    // A past slot exercises real RPC without admitting any job/launch.
    expect((await stub.scheduledTick('2000-01-01T00:00:00.000Z')).reason).toBe('late_slot_refused');
    await worker.scheduled({ cron: '*/5 * * * *' } as ScheduledEvent, env as any, {} as ExecutionContext);
    await worker.scheduled({ cron: '*/30 * * * *' } as ScheduledEvent, env as any, {} as ExecutionContext);
    await runInDurableObject(stub, async (hub: Hub) => { await hub.alarm(); });
  });

  it('routes real health/auth/bus/fleet/conversation requests and gates private assets', async () => {
    expect((await SELF.fetch('https://hub.test/api/conversations')).status).toBe(401);
    for (const path of ['/', '/conversations.js', '/fleet.html', '/fleet.js', '/fleet.css']) {
      const response = await SELF.fetch('https://hub.test' + path, { redirect: 'manual' });
      expect(response.status).toBe(303);
      expect(response.headers.get('Location')).toBe('/auth/login');
    }
    expect((await SELF.fetch('https://hub.test/.dev.vars')).status).toBe(404);
    const cookie = (await createSessionCookie(env as any, { sub: 'path-admin', email: 'admin@fake.local', isAdmin: true, csrfToken: 'local-csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
    for (const path of ['/healthz', '/api/healthz', '/auth/me', '/api/agents', '/api/conversations']) {
      expect((await SELF.fetch('https://hub.test' + path, { headers: { Cookie: cookie } })).status, path).toBe(200);
    }
  });
});
