import { createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { defaultEnv, createHelpers } from './m1-focus-setup';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import worker, { Hub } from '../src/worker';
import { getLoginCookie, createLoginCookie, createSessionCookie, validSessionSecret } from '../src/auth/session';

describe('M1 Focus: Config Boundaries', () => {
  let helpers: Awaited<ReturnType<typeof createHelpers>>;

  beforeEach(async () => {
    helpers = await createHelpers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const runWithEnv = async (
    desc: string,
    admins: string | undefined,
    secret: string | undefined,
    setupFixture: (instance: Hub) => void,
    testFn: (instance: Hub, validSession: string) => Promise<void>
  ) => {
    it(`Configuration boundary: ${desc}`, async () => {
      const id = defaultEnv.HUB.idFromName("hub");
      const stub = defaultEnv.HUB.get(id);

      let oF = globalThis.fetch;
      let localFetchCount = 0;

      await runInDurableObject(stub, async (instance: Hub) => {
        const originalAdmins = instance.env.HUB_ADMINS;
        const originalSecret = instance.env.SESSION_SECRET;

        // Valid setup first
        instance.env.HUB_ADMINS = 'admin@fake.local';
        instance.env.SESSION_SECRET = '01234567890123456789012345678901';

        const validSession = await createSessionCookie(instance.env, {
          sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-123', exp: Math.floor(Date.now() / 1000) + 3600
        });

        // Seed fixture
        setupFixture(instance);

        try {
          // Mutate env
          instance.env.HUB_ADMINS = admins as any;
          instance.env.SESSION_SECRET = secret as any;

          globalThis.fetch = async (req) => {
            localFetchCount++;
            return oF(req);
          };

          await testFn(instance, validSession);

        } finally {
          instance.env.HUB_ADMINS = originalAdmins;
          instance.env.SESSION_SECRET = originalSecret;
          globalThis.fetch = oF;
        }
      });
    });
  };

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
    runWithEnv(
      c.desc,
      c.admins,
      c.secret,
      (instance) => {
        // Seed legitimate pending pair and actual credential
        instance.sql.exec("DELETE FROM device_pairings");
        instance.sql.exec("DELETE FROM credentials");
        instance.sql.exec("INSERT INTO device_pairings (device_code_hash, user_code, client_kind, name, hostname, requested_scopes, expires_at, status, interval_s, last_poll_at, created_at) VALUES ('hash', 'validcode', 'host', 'name', 'host', '[\"read\"]', ?, 'pending', 5, ?, ?)", new Date(Date.now() + 100000).toISOString(), new Date().toISOString(), new Date().toISOString());
        instance.sql.exec("INSERT INTO credentials (id, kind, family_id, token_hash, refresh_hash, subject, scopes, created_by_sub, created_at, expires_at) VALUES ('cred-1', 'host', 'fam-1', 'thash', 'rhash', 'subj', '[\"read\"]', 'admin', ?, ?)", new Date().toISOString(), new Date(Date.now() + 100000).toISOString());
      },
      async (instance, validSession) => {
        let localFetchCount = 0;
        const oF = globalThis.fetch;
        globalThis.fetch = async (req) => {
          localFetchCount++;
          return oF(req);
        };

        try {
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

          // Record DB state before approve
          const preApprove = instance.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', 'validcode').toArray()[0];

          // POST device approve refuses
          let resApprove = await instance.fetch(new Request('http://example.com/api/device/approve/validcode', { method: 'POST', headers: { 'Cookie': validSession, 'X-CSRF-Token': 'csrf-123' }, body: JSON.stringify({ granted_scopes: ['read'] }) }));
          expect(resApprove.status).toBe(401);
          
          const postApprove = instance.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', 'validcode').toArray()[0];
          expect(postApprove.status).toBe(preApprove.status);
          expect(postApprove.granted_scopes).toBe(preApprove.granted_scopes);

          // Revoke uses actual admin route
          const preRevoke = instance.sql.exec('SELECT * FROM credentials WHERE id = ?', 'cred-1').toArray()[0];
          let resRevoke = await instance.fetch(new Request('http://example.com/api/token/revoke', { method: 'POST', headers: { 'Cookie': validSession, 'X-CSRF-Token': 'csrf-123', 'Content-Type': 'application/json' }, body: JSON.stringify({ id: 'cred-1' }) }));
          expect(resRevoke.status).toBe(401);
          const postRevoke = instance.sql.exec('SELECT * FROM credentials WHERE id = ?', 'cred-1').toArray()[0];
          expect(postRevoke.revoked_at).toBe(preRevoke.revoked_at);

          // Check network fetch not invoked
          expect(localFetchCount).toBe(0);
        } finally {
          globalThis.fetch = oF;
        }
      }
    );
  }

  it('Configuration boundary: Positive control valid configuration works', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let oF = globalThis.fetch;
    let localFetchCount = 0;

    await runInDurableObject(stub, async (instance: Hub) => {
      const originalAdmins = instance.env.HUB_ADMINS;
      const originalSecret = instance.env.SESSION_SECRET;

      try {
        instance.env.HUB_ADMINS = 'admin@fake.local';
        instance.env.SESSION_SECRET = '01234567890123456789012345678901';
        const validSession = await createSessionCookie(instance.env, {
          sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-123', exp: Math.floor(Date.now() / 1000) + 3600
        });

        // Seed
        instance.sql.exec("DELETE FROM device_pairings");
        instance.sql.exec("DELETE FROM credentials");
        instance.sql.exec("INSERT INTO device_pairings (device_code_hash, user_code, client_kind, name, hostname, requested_scopes, expires_at, status, interval_s, last_poll_at, created_at) VALUES ('hash', 'validcode', 'host', 'name', 'host', '[\"read\"]', ?, 'pending', 5, ?, ?)", new Date(Date.now() + 100000).toISOString(), new Date().toISOString(), new Date().toISOString());
        instance.sql.exec("INSERT INTO credentials (id, kind, family_id, token_hash, refresh_hash, subject, scopes, created_by_sub, created_at, expires_at) VALUES ('cred-1', 'host', 'fam-1', 'thash', 'rhash', 'subj', '[\"read\"]', 'admin', ?, ?)", new Date().toISOString(), new Date(Date.now() + 100000).toISOString());

        globalThis.fetch = async (req) => {
          localFetchCount++;
          return oF(req);
        };

        // /auth/me
        let resMe = await instance.fetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': validSession } }));
        expect(resMe.status).toBe(200);

        // Approve
        let resApprove = await instance.fetch(new Request('http://example.com/api/device/approve/validcode', { method: 'POST', headers: { 'Cookie': validSession, 'X-CSRF-Token': 'csrf-123' }, body: JSON.stringify({ granted_scopes: ['read'] }) }));
        expect(resApprove.status).toBe(200);
        const postApprove = instance.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', 'validcode').toArray()[0] as any;
        expect(postApprove.status).toBe('approved');

        // Revoke
        let resRevoke = await instance.fetch(new Request('http://example.com/api/token/revoke', { method: 'POST', headers: { 'Cookie': validSession, 'X-CSRF-Token': 'csrf-123', 'Content-Type': 'application/json' }, body: JSON.stringify({ id: 'cred-1' }) }));
        expect(resRevoke.status).toBe(200);
        const postRevoke = instance.sql.exec('SELECT * FROM credentials WHERE id = ?', 'cred-1').toArray()[0] as any;
        expect(postRevoke.revoked_at).not.toBeNull();
      } finally {
        instance.env.HUB_ADMINS = originalAdmins;
        instance.env.SESSION_SECRET = originalSecret;
        globalThis.fetch = oF;
      }
    });
  });

  it('Configuration boundary: Test current auth callback empty secret independently', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let oF = globalThis.fetch;
    let localFetchCount = 0;

    await runInDurableObject(stub, async (instance: Hub) => {
      const originalAdmins = instance.env.HUB_ADMINS;
      const originalSecret = instance.env.SESSION_SECRET;

      try {
        instance.env.HUB_ADMINS = 'admin@fake.local';
        instance.env.SESSION_SECRET = '01234567890123456789012345678901';

        globalThis.fetch = async (req) => {
          localFetchCount++;
          if (req.url === 'https://cloudflare.com/oauth2/token') {
             return new Response(JSON.stringify({ id_token: 'valid.id.token' }), { status: 200, headers: {'Content-Type': 'application/json'} });
          }
          return oF(req);
        };

        const loginCookie = await createLoginCookie(instance.env, {
          nonce: 'n1', state: 's1', pkce_verifier: 'p1', exp: Math.floor(Date.now() / 1000) + 600, return_to: '/'
        });

        // Mutate
        instance.env.SESSION_SECRET = '';

        // It must fail or 503 instead of issuing session because the secret is empty
        let cbRes = await instance.fetch(new Request('http://example.com/auth/callback?state=s1&code=c1', { headers: { 'Cookie': loginCookie } }));
        expect(cbRes.status).toBeGreaterThanOrEqual(400);

      } finally {
        instance.env.HUB_ADMINS = originalAdmins;
        instance.env.SESSION_SECRET = originalSecret;
        globalThis.fetch = oF;
      }
    });
  });
  
  it('Configuration boundary: Test current auth callback empty admin independently', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let oF = globalThis.fetch;
    let localFetchCount = 0;

    await runInDurableObject(stub, async (instance: Hub) => {
      const originalAdmins = instance.env.HUB_ADMINS;
      const originalSecret = instance.env.SESSION_SECRET;

      try {
        instance.env.HUB_ADMINS = '';
        instance.env.SESSION_SECRET = '01234567890123456789012345678901';

        globalThis.fetch = async (req) => {
          localFetchCount++;
          if (req.url === 'https://cloudflare.com/oauth2/token') {
             return new Response(JSON.stringify({ id_token: await helpers.createSignedJWT({ sub: 'u1', email: 'user@fake.local', aud: 'aud', iss: 'iss' }) }), { status: 200, headers: {'Content-Type': 'application/json'} });
          }
          if (req.url.includes('/certs')) {
             return new Response(JSON.stringify({ keys: [helpers.jwks] }), { status: 200, headers: {'Content-Type': 'application/json'} });
          }
          return oF(req);
        };

        const loginCookie = await createLoginCookie(instance.env, {
          nonce: 'n1', state: 's2', pkce_verifier: 'p2', exp: Math.floor(Date.now() / 1000) + 600, return_to: '/'
        });

        // It must reject if the admin list is empty or the user is not in the list (403 usually)
        let cbRes = await instance.fetch(new Request('http://example.com/auth/callback?state=s2&code=c2', { headers: { 'Cookie': loginCookie } }));
        expect(cbRes.status).toBeGreaterThanOrEqual(400);

      } finally {
        instance.env.HUB_ADMINS = originalAdmins;
        instance.env.SESSION_SECRET = originalSecret;
        globalThis.fetch = oF;
      }
    });
  });

});
