import { env, createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';
import { base64url, sha256 } from '../src/auth/crypto';
import worker from '../src/worker';
import { Env } from '../src/env';

export const defaultEnv = {
  ...env,
  AUTH_BASE_URL: 'https://auth.fake.local',
  AUTH_CLIENT_ID: 'fake_client_id',
  AUTH_CALLBACK_URL: 'http://localhost:19130/auth/callback',
  AUTH_ISSUER: 'https://auth.fake.local',
  AUTH_JWKS_URL: 'https://auth.fake.local/.well-known/jwks.json',
  HUB_ADMINS: 'admin@fake.local,another@fake.local',
  SESSION_SECRET: 'super_secret_session_key_32_chars_long_for_tests'
} as unknown as Env;

export async function doFetch(req: Request) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(req, defaultEnv, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

export async function createHelpers() {
  const testKeyPair = (await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true, ['sign', 'verify']
  )) as CryptoKeyPair;
  
  const badKeyPair = (await crypto.subtle.generateKey(
    { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true, ['sign', 'verify']
  )) as CryptoKeyPair;

  const jwkPub = await crypto.subtle.exportKey('jwk', testKeyPair.publicKey) as JsonWebKey;

  async function signJwtWithKey(payload: any, privateKey: CryptoKey, header: any = { alg: 'RS256', kid: 'test-key-1' }) {
    const enc = new TextEncoder();
    const headerB64 = base64url(enc.encode(JSON.stringify(header)));
    const payloadB64 = base64url(enc.encode(JSON.stringify(payload)));
    const data = enc.encode(`${headerB64}.${payloadB64}`);
    
    if (header.alg === 'none') {
      return `${headerB64}.${payloadB64}.`;
    }
    
    const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', privateKey, data);
    const sigB64 = base64url(signature);
    return `${headerB64}.${payloadB64}.${sigB64}`;
  }

  return { testKeyPair, badKeyPair, jwkPub, signJwt: (p: any, h?: any) => signJwtWithKey(p, testKeyPair.privateKey, h), signJwtBad: (p: any, h?: any) => signJwtWithKey(p, badKeyPair.privateKey, h) };
}

export async function loginAndGetState() {
  const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
  const loginRes = await doFetch(loginReq);
  const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
  const location = new URL(loginRes.headers.get('Location')!);
  const state = location.searchParams.get('state')!;
  const nonce = location.searchParams.get('nonce')!;
  return { loginCookie, state, nonce };
}

export async function approveDevice(user_code: string, scopes: string[]) {
  const { loginCookie, state, nonce } = await loginAndGetState();
  const helpers = await createHelpers();
  const now = Math.floor(Date.now() / 1000);
  const idToken = await helpers.signJwt({
    iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
    nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
  });
  
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input: RequestInfo | URL) => {
    if (input.toString().includes('/oauth2/token')) {
      return new Response(JSON.stringify({ id_token: idToken }), { status: 200 });
    }
    if (input.toString().includes('/.well-known/jwks.json')) {
      return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return originalFetch(input);
  };
  
  const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${state}`, {
    headers: { 'Cookie': loginCookie }, redirect: 'manual'
  }));
  const sessionCookie = cbRes.headers.get('Set-Cookie')!;
  globalThis.fetch = originalFetch;

  const csrfReq = new Request('http://example.com/auth/me', { headers: { 'Cookie': sessionCookie } });
  const meRes = await doFetch(csrfReq);
  const csrfToken = ((await meRes.json()) as any).csrf_token;

  const approveReq = new Request(`http://example.com/api/device/approve/${user_code}`, {
    method: 'POST',
    headers: { 'Cookie': sessionCookie, 'Content-Type': 'application/json', 'x-csrf-token': csrfToken },
    body: JSON.stringify({ granted_scopes: scopes })
  });
  return await doFetch(approveReq);
}
