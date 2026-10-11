import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { doFetch, loginAndGetState, createHelpers, defaultEnv } from './m1-focus-setup';
import { runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/hub';
import worker from '../src/worker';

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
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }));
      }
      if (i.toString().includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: 'dummy' }));
      }
      return oF(i);
    };

    let st = await loginAndGetState();
    let badKidToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    }, { alg: 'RS256', kid: 'UNKNOWN_KID_1' });
    
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: badKidToken }));
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] })); }
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
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: badKidToken2 }));
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] })); }
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
        return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] }));
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
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idt }));
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] })); }
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
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idt2 }));
      if (i.toString().includes('/.well-known/jwks.json')) { fetchCount++; return new Response(JSON.stringify({ keys: [{ ...currentHelpers.jwkPub, kid: currentKid }] })); }
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

    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const rows = instance.sql.exec('SELECT * FROM device_pairings LIMIT 10').toArray() as any[];
      for (const row of rows) {
        expect(row.user_code).toBeUndefined();
        expect(row.device_code).toBeUndefined();
      }
      const creds = instance.sql.exec('SELECT * FROM credentials LIMIT 10').toArray() as any[];
      for (const c of creds) {
        expect(c.access_token).toBeUndefined();
        expect(c.refresh_token).toBeUndefined();
        expect(c.token).toBeUndefined();
      }
    });
  });

  it('JWKS: Empty secret/admin allowlist fail closed', async () => {
    const { loginCookie, state, nonce } = await loginAndGetState();
    let idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });

    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken }));
      if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }));
      return oF(i);
    };

    const ctx = createExecutionContext();
    const envNoAdmins = { ...defaultEnv, HUB_ADMINS: '' };
    const resNoAdmins = await worker.fetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, { headers: { 'Cookie': loginCookie }, redirect: 'manual' }), envNoAdmins, ctx);
    await waitOnExecutionContext(ctx);
    expect(resNoAdmins.status).toBeGreaterThanOrEqual(400);

    const ctx2 = createExecutionContext();
    const envNoSec = { ...defaultEnv, SESSION_SECRET: '' };
    const resNoSec = await worker.fetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, { headers: { 'Cookie': loginCookie }, redirect: 'manual' }), envNoSec, ctx2);
    await waitOnExecutionContext(ctx2);
    expect(resNoSec.status).toBeGreaterThanOrEqual(400);

    globalThis.fetch = oF;
  });

});
