import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import { doFetch, defaultEnv, createHelpers } from './m1-focus-setup';

let helpers: any;
let originalFetch = globalThis.fetch;

beforeAll(async () => {
  helpers = await createHelpers();
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('M1 Focus: DEVICE RETURN TO', () => {

  it('Anonymous /device?code=... redirects to /auth/login with return_to, and callback returns to /device?code=...', async () => {
    // 1. Generate user_code
    const { user_code } = await (await doFetch(new Request('http://example.com/api/device/code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_kind: 'host', name: 'returntester', requested_scopes: ['host'] })
    }))).json() as any;

    // 2. Anonymous GET /device?code=...
    const devPageRes = await doFetch(new Request(`http://example.com/device?code=${user_code}`, { redirect: 'manual' }));
    expect(devPageRes.status).toBe(302);
    const location = devPageRes.headers.get('Location')!;
    expect(location).toContain('/auth/login');
    expect(location).toContain('return_to=' + encodeURIComponent(`/device?code=${user_code}`));

    // 3. Follow redirect to /auth/login
    const loginReq = new Request('http://example.com' + location, { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    expect(loginRes.status).toBe(302);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const authLocation = new URL(loginRes.headers.get('Location')!);
    const state = authLocation.searchParams.get('state')!;
    const nonce = authLocation.searchParams.get('nonce')!;

    // 4. Simulate OIDC callback
    const now = Math.floor(Date.now() / 1000);
    const idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    
    globalThis.fetch = async (input: RequestInfo | URL) => {
      if (input.toString().includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
      }
      if (input.toString().includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { status: 200 });
      }
      return originalFetch(input);
    };

    // 5. Callback should redirect to the original /device?code=...
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    globalThis.fetch = originalFetch;

    expect(cbRes.status).toBe(302);
    expect(cbRes.headers.get('Location')).toBe(`/device?code=${user_code}`);
  });

  it('Normal login without return_to goes to /', async () => {
    // 1. GET /auth/login directly
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    expect(loginRes.status).toBe(302);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const authLocation = new URL(loginRes.headers.get('Location')!);
    const state = authLocation.searchParams.get('state')!;
    const nonce = authLocation.searchParams.get('nonce')!;

    const now = Math.floor(Date.now() / 1000);
    const idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    
    globalThis.fetch = async (input: RequestInfo | URL) => {
      if (input.toString().includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
      }
      if (input.toString().includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { status: 200 });
      }
      return originalFetch(input);
    };

    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    globalThis.fetch = originalFetch;

    expect(cbRes.status).toBe(302);
    expect(cbRes.headers.get('Location')).toBe('/');
  });

});
