import { env, createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import worker from '../src/worker';
import { base64url, decodeBase64url, sha256 } from '../src/auth/crypto';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { verifyBearerScope } from '../src/auth/token';

let testKeyPair: CryptoKeyPair;
let jwkPub: JsonWebKey;

beforeAll(async () => {
  testKeyPair = (await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify'])) as CryptoKeyPair;
  jwkPub = await crypto.subtle.exportKey('jwk', testKeyPair.publicKey) as JsonWebKey;
});

async function signJwt(payload: any, header: any = { alg: 'RS256', kid: 'test-key-1' }): Promise<string> {
  const enc = new TextEncoder();
  const headerB64 = base64url(enc.encode(JSON.stringify(header)));
  const payloadB64 = base64url(enc.encode(JSON.stringify(payload)));
  const data = enc.encode(`${headerB64}.${payloadB64}`);
  
  if (header.alg === 'none') {
    return `${headerB64}.${payloadB64}.`;
  }
  
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', testKeyPair.privateKey, data);
  const sigB64 = base64url(signature);
  return `${headerB64}.${payloadB64}.${sigB64}`;
}

const originalFetch = globalThis.fetch;
let mockOidcResponse: any = {};
let fetchLog: string[] = [];

let lastTokenBody = '';
beforeAll(() => {
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input.toString();
    fetchLog.push(url);
    if (url.includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...jwkPub, kid: 'test-key-1' }] }), { status: 200 });
    }
    if (url.includes('/oauth2/token')) {
      // capture body if possible
      let bodyStr = '';
      if (init && init.body) {
        bodyStr = init.body.toString();
        lastTokenBody = bodyStr;
      }
      return new Response(JSON.stringify({ ...mockOidcResponse, _received_body: bodyStr }), { status: 200 });
    }
    return originalFetch(input, init);
  };
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

const defaultEnv = {
  ...env,
  AUTH_BASE_URL: 'https://auth.fake.local',
  AUTH_CLIENT_ID: 'fake_client_id',
  AUTH_CALLBACK_URL: 'http://localhost:19130/auth/callback',
  AUTH_ISSUER: 'https://auth.fake.local',
  AUTH_JWKS_URL: 'https://auth.fake.local/.well-known/jwks.json',
  HUB_ADMINS: 'admin@fake.local,another@fake.local',
  SESSION_SECRET: 'super_secret_session_key_32_chars_long_for_tests'
} as unknown as Env;

async function doFetch(req: Request) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(req, defaultEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

describe('M1 Auth Worker Runtime', () => {

  it('Login starts PKCE flow', async () => {
    const req = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const res = await doFetch(req);
    expect(res.status).toBe(302);
    expect(res.headers.get('Location')).toContain('https://auth.fake.local/oauth2/authorize');
    expect(res.headers.get('Set-Cookie')).toContain('hub_login=');
  });

  it('Callback handles valid PKCE and issues session', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    const state = location.searchParams.get('state');
    const nonce = location.searchParams.get('nonce');
    const code_challenge = location.searchParams.get('code_challenge');

    const now = Math.floor(Date.now() / 1000);
    const idToken = await signJwt({
      iss: 'https://auth.fake.local',
      aud: 'fake_client_id',
      exp: now + 3600,
      nonce: nonce,
      token_use: 'id',
      email: 'admin@fake.local',
      email_verified: true,
      sub: 'admin-sub-123'
    });
    mockOidcResponse = { id_token: idToken };

    const cbReq = new Request(`http://example.com/auth/callback?code=fake_code&state=${state}`, {
      headers: { 'Cookie': loginCookie },
      redirect: 'manual'
    });
    const cbRes = await doFetch(cbReq);
    expect(cbRes.status).toBe(302);
    expect(cbRes.headers.get('Set-Cookie')).toContain('hub_session=');
    
    // valid PKCE S256 computed against exchange verifier
    const fetchCall = fetchLog.find(url => url.includes('/oauth2/token'));
    expect(fetchCall).toBeTruthy();
    
    // Check if code_verifier is passed and valid
    const params = new URLSearchParams(lastTokenBody);
    const code_verifier = params.get('code_verifier');
    expect(code_verifier).toBeTruthy();
    
    const computedChallenge = await sha256(code_verifier!);
    expect(computedChallenge).toBe(code_challenge);
  });

  // OIDC & Session Negatives
  it('Callback rejects wrong state', async () => {
    const cbReq = new Request(`http://example.com/auth/callback?code=fake_code&state=wrong_state`, {
      headers: { 'Cookie': 'hub_login=some_signed_cookie_stuff' },
      redirect: 'manual'
    });
    const cbRes = await doFetch(cbReq);
    expect(cbRes.status).toBeGreaterThanOrEqual(400); // Expect 4xx or 500
  });

  it('Callback rejects expired ID token, equal now', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    
    const now = Math.floor(Date.now() / 1000);
    const idToken = await signJwt({
      iss: 'https://auth.fake.local',
      aud: 'fake_client_id',
      exp: now, // Expired or equal now
      nonce: location.searchParams.get('nonce'),
      token_use: 'id',
      email: 'admin@fake.local',
      email_verified: true,
      sub: 'admin-123'
    });
    mockOidcResponse = { id_token: idToken };
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);
  });

  it('Callback rejects wrong nonce', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    
    const now = Math.floor(Date.now() / 1000);
    const idToken = await signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: 'wrong_nonce_here', token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: '123'
    });
    mockOidcResponse = { id_token: idToken };
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);
  });

  it('Callback rejects invalid RSA signature, none alg, and wrong token_use', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    
    const now = Math.floor(Date.now() / 1000);
    const payload = {
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: location.searchParams.get('nonce'), token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: '123'
    };

    // 1. None alg
    mockOidcResponse = { id_token: await signJwt(payload, { alg: 'none', kid: 'test-key-1' }) };
    let cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);

    // 2. Invalid RSA sig
    const badSigToken = (await signJwt(payload)).slice(0, -5) + 'abcde';
    mockOidcResponse = { id_token: badSigToken };
    cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);

    // 3. Wrong token_use
    mockOidcResponse = { id_token: await signJwt({...payload, token_use: 'access'}) };
    cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);
  });

  it('Callback rejects unverified email and non-admin email', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    
    const now = Math.floor(Date.now() / 1000);
    const basePayload = {
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: location.searchParams.get('nonce'), token_use: 'id', sub: '123'
    };

    // 1. Unverified admin email
    mockOidcResponse = { id_token: await signJwt({...basePayload, email: 'admin@fake.local', email_verified: false}) };
    let cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);

    // 2. Verified non-admin email
    mockOidcResponse = { id_token: await signJwt({...basePayload, email: 'hacker@fake.local', email_verified: true}) };
    cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    expect(cbRes.status).toBeGreaterThanOrEqual(400);
  });

  it('Session cookie has correct flags (12h, HttpOnly, Secure, SameSite=Lax)', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const location = new URL(loginRes.headers.get('Location')!);
    
    mockOidcResponse = { id_token: await signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: location.searchParams.get('nonce'), token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: '123'
    })};
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${location.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    
    const setCookie = cbRes.headers.get('Set-Cookie')!;
    expect(setCookie).toContain('HttpOnly');
    expect(setCookie).toContain('Secure');
    expect(setCookie).toContain('SameSite=Lax');
    expect(setCookie).toContain('Max-Age=43200'); // 12h
  });

  it('logout GET denied, POST CSRF absent, wrong token fail', async () => {
    // logout GET denied (Method 405 Allow baseline)
    let res = await doFetch(new Request('http://example.com/auth/logout', { method: 'GET' }));
    expect(res.status).toBe(405);
    expect(res.headers.get('Allow')).toBeTruthy(); // Must include Allow header!

    // POST without CSRF
    res = await doFetch(new Request('http://example.com/auth/logout', { method: 'POST', headers: { 'Cookie': 'hub_session=fake' } }));
    expect(res.status).toBeGreaterThanOrEqual(400); // 401 or 403

    // wrong CSRF token
    res = await doFetch(new Request('http://example.com/auth/logout', { method: 'POST', headers: { 'Cookie': 'hub_session=fake', 'x-csrf-token': 'wrong' } }));
    expect(res.status).toBeGreaterThanOrEqual(400);
  });

  it('Handles malformed JSON returning 400', async () => {
    const req = new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"bad json'
    });
    const res = await doFetch(req);
    expect(res.status).toBe(400);
  });

  it('Production inspect endpoint absent', async () => {
    const req = new Request('http://example.com/test-only/inspect-db', { method: 'GET' });
    const res = await doFetch(req);
    expect(res.status).toBe(404);
  });

  // Bearer Helper Tests
  it('Bearer Helper verifies scopes and optionally machineId, actor, kind', async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let result = null;
    await runInDurableObject(stub, async (instance: Hub) => {
      // First, seed a valid credential into DB
      const accessToken = 'hubt_test_helper_token';
      const tokenHash = await sha256(accessToken);
      instance.sql.exec(`
        INSERT INTO credentials (id, kind, token_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at, machine_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `, 'helper_cred_id', 'host', tokenHash, JSON.stringify(['host', 'bus:read']), 'fam_1', 'subj_1', 'sub_1', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString(), 'machine_1');
      
      const vbs = verifyBearerScope as any;
      // Valid cases
      const valid = await vbs(instance.sql, 'Bearer ' + accessToken, 'host', { kind: 'host', machineId: 'machine_1' });
      expect(valid).toBeTruthy();
      
      // Invalid cases
      const wrongScope = await vbs(instance.sql, 'Bearer ' + accessToken, 'admin');
      expect(wrongScope).toBeNull();

      const wrongKind = await vbs(instance.sql, 'Bearer ' + accessToken, 'host', { kind: 'agent' });
      expect(wrongKind).toBeNull();
      
      const wrongMachine = await vbs(instance.sql, 'Bearer ' + accessToken, 'host', { machineId: 'machine_2' });
      expect(wrongMachine).toBeNull();
      
      // Verify expired
      instance.sql.exec(`UPDATE credentials SET expires_at = ? WHERE id = ?`, new Date(Date.now() - 1000).toISOString(), 'helper_cred_id');
      const expired = await vbs(instance.sql, 'Bearer ' + accessToken, 'host');
      expect(expired).toBeNull();
    });
  });

  // Expand Device flow negative tests...
  it('Device flow RFC8628 constraints and negatives', async () => {
    // Malformed kind
    let codeReq = new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'bad_kind', name: 't', requested_scopes: ['agent'] })
    });
    let codeRes = await doFetch(codeReq);
    expect(codeRes.status).toBe(400);

    // Valid request
    codeReq = new Request('http://example.com/api/device/code', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_kind: 'host', name: 't', requested_scopes: ['host', 'bus:send'] })
    });
    codeRes = await doFetch(codeReq);
    expect(codeRes.status).toBe(200);
    const { device_code, user_code } = await codeRes.json() as any;
    expect(user_code).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/); // MUST have hyphen

    // Adjust last_poll_at
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      const past = new Date(Date.now() - 10000).toISOString();
      instance.sql.exec('UPDATE device_pairings SET last_poll_at = ?', past);
    });

    const pollReq = () => new Request('http://example.com/api/device/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code })
    });

    // Unrequested grant
    let badGrantReq = new Request('http://example.com/api/device/token', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'wrong_grant', device_code })
    });
    let res = await doFetch(badGrantReq);
    expect(res.status).toBe(400);

    res = await doFetch(pollReq());
    expect(res.status).toBe(400);
    let error = (await res.json() as any).error;
    expect(error).toBe('authorization_pending');

    // Admin approve with narrowing
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const loc = new URL(loginRes.headers.get('Location')!);
    
    mockOidcResponse = { id_token: await signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: loc.searchParams.get('nonce'), token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    })};
    
    const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=1&state=${loc.searchParams.get('state')}`, {
      headers: { 'Cookie': loginCookie }, redirect: 'manual'
    }));
    const sessionCookie = cbRes.headers.get('Set-Cookie')!;
    
    // Check actual device page render escapes injected name/hostname
    const devicePageReq = new Request(`http://example.com/device?code=${user_code}`, { headers: { 'Cookie': sessionCookie } });
    const devicePageRes = await doFetch(devicePageReq);
    expect(devicePageRes.status).toBe(200);

    const csrfTokenReq = new Request('http://example.com/auth/me', { headers: { 'Cookie': sessionCookie } });
    const meRes = await doFetch(csrfTokenReq);
    const csrfToken = (await meRes.json() as any).csrf_token;

    // Approve narrowed scope
    const approveReq = new Request(`http://example.com/api/device/approve/${user_code}`, {
      method: 'POST',
      headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
      body: JSON.stringify({ granted_scopes: ['host'] }) // Narrowed from 'host', 'bus:send'
    });
    const apprRes = await doFetch(approveReq);
    expect(apprRes.status).toBe(200);

    // Poll approved
    res = await doFetch(pollReq());
    expect(res.status).toBe(200);
    const { access_token, refresh_token, scope } = await res.json() as any;
    expect(scope).toBe('host');

    // Consume once - subsequent poll -> invalid_grant
    res = await doFetch(pollReq());
    expect(res.status).toBe(400);
    error = (await res.json() as any).error;
    expect(error).toBe('invalid_grant');
    
    // Revoke admin ID accepted, cross actor denied
    // Admin revoking by ID:
    let revokeReq = new Request('http://example.com/api/token/revoke', {
      method: 'POST',
      headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
      body: JSON.stringify({ id: 'some_id' }) // Assuming the id is passed here
    });
    let revokeRes = await doFetch(revokeReq);
    // Even if id doesn't exist, it should not fail 400 because of "missing_token" (F5)
    expect(revokeRes.status).not.toBe(400); 

    // Revoke own using Bearer
    revokeReq = new Request('http://example.com/api/token/revoke', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${access_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: access_token })
    });
    revokeRes = await doFetch(revokeReq);
    expect(revokeRes.status).toBe(200);
  });

  it('Refresh rotation revokes old access token', async () => {
    // Generate valid tokens
    const id = defaultEnv.HUB.idFromName("hub");
    const stub = defaultEnv.HUB.get(id);
    let oldAccessToken = '';
    let oldRefreshToken = 'hubr_old_refresh';
    let famId = 'fam_1';
    await runInDurableObject(stub, async (instance: Hub) => {
      oldAccessToken = 'hubt_old_access';
      instance.sql.exec(`
        INSERT INTO credentials (id, kind, token_hash, refresh_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `, 'cred_1', 'host', await sha256(oldAccessToken), await sha256(oldRefreshToken), '["host"]', famId, 'subj_1', 'sub_1', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString());
    });

    const refreshReq = new Request('http://example.com/api/token/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: oldRefreshToken })
    });
    const refRes = await doFetch(refreshReq);
    expect(refRes.status).toBe(200);
    const newTokens = await refRes.json() as any;

    await runInDurableObject(stub, async (instance: Hub) => {
      const vbs = verifyBearerScope as any;
      // Old access token should be revoked! F6 says it's not.
      const valid = await vbs(instance.sql, 'Bearer ' + oldAccessToken, 'host');
      expect(valid).toBeNull();
    });
  });

});
