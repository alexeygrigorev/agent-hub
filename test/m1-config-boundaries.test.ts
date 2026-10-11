import { createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { defaultEnv } from './m1-focus-setup';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import worker, { Hub } from '../src/worker';
import { createHelpers } from './m1-focus-setup';
import { getLoginCookie, createLoginCookie, createSessionCookie, validSessionSecret } from '../src/auth/session';

describe('M1 Focus: Config Boundaries', () => {
  let helpers: Awaited<ReturnType<typeof createHelpers>>;
  let fetchCount = 0;

  beforeEach(async () => {
    helpers = await createHelpers();
    fetchCount = 0;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Configuration boundaries: Missing/invalid SESSION_SECRET and HUB_ADMINS fail closed', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);

    let oF = globalThis.fetch;
    let localFetchCount = 0;

    await runInDurableObject(stub, async (instance: Hub) => {
      const originalAdmins = instance.env.HUB_ADMINS;
      const originalSecret = instance.env.SESSION_SECRET;

      // Ensure valid original env works (positive control)
      instance.env.HUB_ADMINS = 'admin@fake.local';
      instance.env.SESSION_SECRET = '01234567890123456789012345678901';

      // 1. Positive control for /auth/me
      const validSession = await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-123', exp: Math.floor(Date.now() / 1000) + 3600
      });
      let resMe = await instance.fetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': validSession } }));
      expect(resMe.status).toBe(200);

      // Now test negative cases by mutating instance.env
      const cases = [
        { desc: 'Missing SESSION_SECRET', admins: 'admin@fake.local', secret: undefined },
        { desc: 'Empty SESSION_SECRET', admins: 'admin@fake.local', secret: '' },
        { desc: 'Short SESSION_SECRET (<32)', admins: 'admin@fake.local', secret: 'shortsecret' },
        { desc: 'Placeholder SESSION_SECRET', admins: 'admin@fake.local', secret: '<put_32_character_secret_here>' },
        { desc: 'Missing HUB_ADMINS', admins: undefined, secret: '01234567890123456789012345678901' },
        { desc: 'Empty HUB_ADMINS', admins: '', secret: '01234567890123456789012345678901' },
        { desc: 'Placeholder HUB_ADMINS', admins: '<your_email@example.com>', secret: '01234567890123456789012345678901' },
        { desc: 'Removed admin allowlist with existing valid cookie', admins: 'other@fake.local', secret: '01234567890123456789012345678901' },
      ];

      for (const c of cases) {
        instance.env.HUB_ADMINS = c.admins as any;
        instance.env.SESSION_SECRET = c.secret as any;

        globalThis.fetch = async (req) => {
          localFetchCount++;
          return oF(req);
        };

        localFetchCount = 0;

        if (c.desc !== 'Removed admin allowlist with existing valid cookie') {
          // /auth/login refuses with 503/4xx (never external redirect)
          let resLogin = await instance.fetch(new Request('http://example.com/auth/login'));
          expect(resLogin.status).toBeGreaterThanOrEqual(400);
          expect(resLogin.status).toBeLessThan(600);
          expect(resLogin.status).not.toBe(302);
        }

        // /auth/me unauthorized
        let resMeBad = await instance.fetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': validSession } }));
        expect(resMeBad.status).toBe(401);

        // Record DB state
        const preApproveCount = instance.sql.exec('SELECT count(*) as c FROM device_pairings').toArray()[0].c;

        // POST device approve refuses and NO state writes
        let resApprove = await instance.fetch(new Request('http://example.com/api/device/approve/fakecode', { method: 'POST', headers: { 'Cookie': validSession, 'X-CSRF-Token': 'csrf-123' } }));
        expect(resApprove.status).toBe(401);
        
        const postApproveCount = instance.sql.exec('SELECT count(*) as c FROM device_pairings').toArray()[0].c;
        expect(postApproveCount).toBe(preApproveCount);

        // Check network fetch not invoked
        expect(localFetchCount).toBe(0);
      }

      // Restore
      instance.env.HUB_ADMINS = originalAdmins;
      instance.env.SESSION_SECRET = originalSecret;
    });

    globalThis.fetch = oF;
  });
});
