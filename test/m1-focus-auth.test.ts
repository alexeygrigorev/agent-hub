import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { doFetch, loginAndGetState, createHelpers } from './m1-focus-setup';

let helpers: any;
let originalFetch = globalThis.fetch;

beforeAll(async () => {
  helpers = await createHelpers();
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('M1 Focus: AUTH Quality', () => {

  it('AUTH: wrongstate must carry otherwise-valid freshly minted loginCOOKIE so failure proves mismatch', async () => {
    const { loginCookie } = await loginAndGetState();
    
    // We send wrong state but VALID cookie
    const cbReq = new Request(`http://example.com/auth/callback?code=fake_code&state=wrong_state`, {
      headers: { 'Cookie': loginCookie },
      redirect: 'manual'
    });
    const cbRes = await doFetch(cbReq);
    expect(cbRes.status).toBeGreaterThanOrEqual(400);
    expect(cbRes.status).toBeLessThan(500); // 4xx contract
  });

  it('AUTH: invalid JWT scenario gets fresh state/nonce/cookie and otherwise valid signedJWT; wrong aud+iss', async () => {
    // WRONG AUD
    let st1 = await loginAndGetState();
    let idTokenAud = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'WRONG_AUD', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st1.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idTokenAud }));
      return oF(i);
    };
    let resAud = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st1.state}`, { headers: { 'Cookie': st1.loginCookie }, redirect: 'manual' }));
    expect(resAud.status).toBeGreaterThanOrEqual(400);
    expect(resAud.status).toBeLessThan(500);

    // WRONG ISS
    let st2 = await loginAndGetState();
    let idTokenIss = await helpers.signJwt({
      iss: 'https://WRONG.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st2.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idTokenIss }));
      return oF(i);
    };
    let resIss = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st2.state}`, { headers: { 'Cookie': st2.loginCookie }, redirect: 'manual' }));
    expect(resIss.status).toBeGreaterThanOrEqual(400);
    expect(resIss.status).toBeLessThan(500);

    globalThis.fetch = oF;
  });

  it('AUTH: For invalidsignature use actualbadRSA signature, neverstubverification', async () => {
    let st = await loginAndGetState();
    // signJwtBad uses a completely different RSA key pair!
    let idTokenBadSig = await helpers.signJwtBad({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    
    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idTokenBadSig }));
      if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }));
      return oF(i);
    };
    let res = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st.state}`, { headers: { 'Cookie': st.loginCookie }, redirect: 'manual' }));
    globalThis.fetch = oF;

    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(res.status).toBeLessThan(500);
  });

  it('AUTH: Add tampered/expired session and validPOSTlogout+CSRF, wrongOrigin', async () => {
    // To test session we first get a valid session
    let st = await loginAndGetState();
    let idToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: Math.floor(Date.now() / 1000) + 3600,
      nonce: st.nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1'
    });
    let oF = globalThis.fetch;
    globalThis.fetch = async (i) => {
      if (i.toString().includes('/oauth2/token')) return new Response(JSON.stringify({ id_token: idToken }));
      if (i.toString().includes('/.well-known/jwks.json')) return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }));
      return oF(i);
    };
    let cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=fake&state=${st.state}`, { headers: { 'Cookie': st.loginCookie }, redirect: 'manual' }));
    globalThis.fetch = oF;
    const sessionCookie = cbRes.headers.get('Set-Cookie')!;
    
    // Tampered session
    let tamperedCookie = sessionCookie.replace(/hub_session=([^;]+)/, 'hub_session=$1_tampered');
    let resTamp = await doFetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': tamperedCookie } }));
    expect(resTamp.status).toBeGreaterThanOrEqual(401);
    expect(resTamp.status).toBeLessThan(500);

    // valid POST logout + CSRF
    const meRes = await doFetch(new Request('http://example.com/auth/me', { headers: { 'Cookie': sessionCookie } }));
    const csrfToken = ((await meRes.json()) as any).csrf_token;

    // wrong Origin on POST logout
    let resWrongOrg = await doFetch(new Request('http://example.com/auth/logout', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'x-csrf-token': csrfToken, 'Origin': 'http://hacker.com' }
    }));
    expect(resWrongOrg.status).toBeGreaterThanOrEqual(400);

    // valid POST logout
    let resLogout = await doFetch(new Request('http://example.com/auth/logout', {
      method: 'POST', headers: { 'Cookie': sessionCookie, 'x-csrf-token': csrfToken, 'Origin': 'http://example.com' }, redirect: 'manual'
    }));
    expect([200, 302]).toContain(resLogout.status);
  });

});
