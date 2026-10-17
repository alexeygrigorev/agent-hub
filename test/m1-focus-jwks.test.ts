import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { doFetch, loginAndGetState, createHelpers, defaultEnv } from './m1-focus-setup';
import { runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/hub';
import worker from '../src/worker';
import { sha256 } from '../src/auth/crypto';
import { createLoginCookie } from '../src/auth/session';

let helpers: any;
let originalFetch = globalThis.fetch;

beforeAll(async () => {
  helpers = await createHelpers();
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('M1 Focus: JWKS', () => {

  it('JWKS: multiple unknownkids do not cause repeated refetch within cooldown', async () => {
    let fetchCount = 0;
    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/.well-known/jwks.json')) {
        fetchCount++;
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
      }
      if (i.toString().includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: 'dummy' }), { headers: { 'content-type': 'application/json' } });
      }
      return oF(i);
    };

    let st = await loginAndGetState();
    let badKidToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    }, { alg: 'RS256', kid: 'UNKNOWN_KID_1' });
    
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: badKidToken }), { headers: { 'content-type': 'application/json' } });
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } }); }
      return oF(i);
    };
    
    await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st.state}`, { headers: { 'Cookie': st.loginCookie }, redirect: 'manual' }));
    let firstFetchCount = fetchCount;
    expect(firstFetchCount).toBeGreaterThanOrEqual(1);

    let st2 = await loginAndGetState();
    let badKidToken2 = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st2.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    }, { alg: 'RS256', kid: 'UNKNOWN_KID_2' });

    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: badKidToken2 }), { headers: { 'content-type': 'application/json' } });
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } }); }
      return oF(i);
    };

    await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st2.state}`, { headers: { 'Cookie': st2.loginCookie }, redirect: 'manual' }));
    
    expect(fetchCount).toBe(firstFetchCount);

    globalThis.fetch = oF;
  });

  it('JWKS: real key rotation after allowed cache expiry succeeds with new matching key', async () => {
    const { vi } = await import('vitest');
    
    vi.useFakeTimers();
    let fetchCount = 0;
    
    let currentKid = 'test-key-1';
    let currentHelpers = await createHelpers();
    let oF = globalThis.fetch;
    
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/.well-known/jwks.json')) {
        fetchCount++;
        return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] }), { headers: { 'content-type': 'application/json' } });
      }
      return oF(i);
    };

    vi.advanceTimersByTime(3600000);
    
    let st = await loginAndGetState();
    let idt = await currentHelpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 7200,
      nonce: st.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    }, { alg: 'RS256', kid: 'test-key-1' });

    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idt }), { headers: { 'content-type': 'application/json' } });
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] }), { headers: { 'content-type': 'application/json' } }); }
      return oF(i);
    };

    let res = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st.state}`, { headers: { 'Cookie': st.loginCookie }, redirect: 'manual' }));
    expect(res.status).toBe(302);

    let newHelpers = await createHelpers();
    currentKid = 'test-key-2';
    currentHelpers = newHelpers;

    vi.advanceTimersByTime(3600000);

    let st2 = await loginAndGetState();
    let idt2 = await newHelpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 7200,
      nonce: st2.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    }, { alg: 'RS256', kid: 'test-key-2' });

    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idt2 }), { headers: { 'content-type': 'application/json' } });
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] }), { headers: { 'content-type': 'application/json' } }); }
      return oF(i);
    };

    let res2 = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st2.state}`, { headers: { 'Cookie': st2.loginCookie }, redirect: 'manual' }));
    expect(res2.status).toBe(302);

    vi.useRealTimers();
    globalThis.fetch = oF;
  });

  it('JWKS: SQLite serialized rows must not contain raw device/access/refresh tokens after positive pairing/rotation; debug route absent', async () => {
    let r = await doFetch(new Request('http://example.com/inspect-db'));
    expect(r.status).toBe(404);

    let r2 = await doFetch(new Request('http://example.com/api/debug'));
    expect(r2.status).toBe(404);

    const codeRes = await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 'test-device', hostname: 'testhost', requested_scopes: ['host'] })
    }));
    const codeData = await codeRes.json() as any;
    const deviceCode = codeData.device_code;
    const userCode = codeData.user_code;

    const { loginCookie, state, nonce } = await loginAndGetState();
    let idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    
    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken }), { headers: { 'content-type': 'application/json' } });
      if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
      return oF(i);
    };

    let loginRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, { headers: { 'Cookie': loginCookie }, redirect: 'manual' }));
    let appCookie = (loginRes.headers.get('Set-Cookie') || '').split(';')[0];

    globalThis.fetch = oF;

    let deviceHtmlRes = await doFetch(new Request(`http://example.com/device?code=${userCode}`, { headers: { 'Cookie': appCookie } }));
    let deviceHtml = await deviceHtmlRes.text();
    // M2b keeps CSRF in memory, fetched from the current authenticated session.
    const meRes = await doFetch(new Request('http://example.com/auth/me', { headers: { Cookie: appCookie } }));
    expect(meRes.status).toBe(200);
    const csrfToken = (await meRes.json() as any).csrf_token;
    expect(csrfToken).toBeTruthy();
    expect(deviceHtml).not.toContain(csrfToken);

    const approveRes = await doFetch(new Request(`http://example.com/api/device/approve/${userCode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Cookie': appCookie, 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ granted_scopes: ['host'] })
    }));

    expect(approveRes.status).toBe(200);
    const tokenRes = await doFetch(new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: deviceCode })
    }));
    expect(tokenRes.status).toBe(200);
    const tokenData = await tokenRes.json() as any;
    expect(tokenData.access_token).toBeDefined();

    const refreshRes = await doFetch(new Request('http://example.com/api/token/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: tokenData.refresh_token })
    }));
    const refreshData = await refreshRes.json() as any;
    expect(refreshData.access_token).toBeDefined();

    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const rows = instance.sql.exec('SELECT * FROM device_pairings').toArray() as any[];
      expect(rows.length).toBeGreaterThan(0);
      let foundUserCode = false;
      for (const row of rows) {
        if (row.user_code === userCode) foundUserCode = true;
        expect(row.user_code).toBeDefined();
        expect(row.device_code).toBeUndefined();
        expect(row.device_code_hash).toBeDefined();
        expect(row.device_code_hash).not.toBe(deviceCode);
      }
      expect(foundUserCode).toBe(true);

      const creds = instance.sql.exec('SELECT * FROM credentials').toArray() as any[];
      expect(creds.length).toBeGreaterThan(0);
      for (const c of creds) {
        expect(c.access_token).toBeUndefined();
        expect(c.refresh_token).toBeUndefined();
        expect(c.token).toBeUndefined();
        expect(c.token_hash).toBeDefined();
        expect(c.refresh_hash).toBeDefined();
      }
    });
  });

  it('JWKS: Empty secret/admin allowlist fail closed on callback', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);

    await runInDurableObject(stub, async (instance) => {
      const origAdmins = instance.env.HUB_ADMINS;
      const origSecret = instance.env.SESSION_SECRET;
      const oF = globalThis.fetch;

      try {
        // 1. Missing admins
        instance.env.HUB_ADMINS = '';
        instance.env.SESSION_SECRET = origSecret;

        const state1 = 's1_' + Math.random();
        const nonce1 = 'n1_' + Math.random();
        const hash1 = await sha256(state1);

        instance.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', hash1, Math.floor(Date.now() / 1000) + 600);
        const loginCookie1 = await createLoginCookie(instance.env, { state: state1, nonce: nonce1, verifier: 'v1', exp: Math.floor(Date.now() / 1000) + 600, returnTo: '/' });

        const idToken1 = await helpers.signJwt({
          iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
          nonce: nonce1, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
        }, { alg: 'RS256', kid: 'test-key-1' });

        globalThis.fetch = async (i) => {
          if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken1 }), { headers: { 'content-type': 'application/json' } });
          if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
          return oF(i);
        };

        let resNoAdmins = await instance.fetch(new Request(`http://example.com/auth/callback?code=fake&state=${state1}`, { headers: { 'Cookie': loginCookie1 }, redirect: 'manual' }));
        expect(resNoAdmins.status).toBe(403);
        expect(instance.sql.exec('SELECT consumed FROM login_states WHERE state_hash = ?', hash1).toArray()[0].consumed).toBe(1);

        globalThis.fetch = oF;

        // 2. Missing secret
        instance.env.HUB_ADMINS = origAdmins;
        instance.env.SESSION_SECRET = origSecret;

        const state2 = 's2_' + Math.random();
        const nonce2 = 'n2_' + Math.random();
        const hash2 = await sha256(state2);

        instance.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', hash2, Math.floor(Date.now() / 1000) + 600);
        const loginCookie2 = await createLoginCookie(instance.env, { state: state2, nonce: nonce2, verifier: 'v2', exp: Math.floor(Date.now() / 1000) + 600, returnTo: '/' });

        instance.env.SESSION_SECRET = '';

        const idToken2 = await helpers.signJwt({
          iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
          nonce: nonce2, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
        }, { alg: 'RS256', kid: 'test-key-1' });

        globalThis.fetch = async (i) => {
          if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken2 }), { headers: { 'content-type': 'application/json' } });
          if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
          return oF(i);
        };

        let resNoSec = await instance.fetch(new Request(`http://example.com/auth/callback?code=fake&state=${state2}`, { headers: { 'Cookie': loginCookie2 }, redirect: 'manual' }));
        expect(resNoSec.status).toBe(400);
        expect(instance.sql.exec('SELECT consumed FROM login_states WHERE state_hash = ?', hash2).toArray()[0].consumed).toBe(0);

        globalThis.fetch = oF;

        // 3. Positive control
        instance.env.HUB_ADMINS = origAdmins;
        instance.env.SESSION_SECRET = origSecret;

        const state3 = 's3_' + Math.random();
        const nonce3 = 'n3_' + Math.random();
        const hash3 = await sha256(state3);

        instance.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', hash3, Math.floor(Date.now() / 1000) + 600);
        const loginCookie3 = await createLoginCookie(instance.env, { state: state3, nonce: nonce3, verifier: 'v3', exp: Math.floor(Date.now() / 1000) + 600, returnTo: '/' });

        const idToken3 = await helpers.signJwt({
          iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
          nonce: nonce3, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
        }, { alg: 'RS256', kid: 'test-key-1' });

        let localFetchCount = 0;
        globalThis.fetch = async (i) => {
          localFetchCount++;
          if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken3 }), { headers: { 'content-type': 'application/json' } });
          if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { headers: { 'content-type': 'application/json' } });
          return oF(i);
        };

        let resPos = await instance.fetch(new Request(`http://example.com/auth/callback?code=fake&state=${state3}`, { headers: { 'Cookie': loginCookie3 }, redirect: 'manual' }));
        expect(resPos.status).toBe(302);
        expect(localFetchCount).toBeGreaterThan(0);
        expect(instance.sql.exec('SELECT consumed FROM login_states WHERE state_hash = ?', hash3).toArray()[0].consumed).toBe(1);
      } finally {
        instance.env.HUB_ADMINS = origAdmins;
        instance.env.SESSION_SECRET = origSecret;
        globalThis.fetch = oF;
      }
    });
  });

});
