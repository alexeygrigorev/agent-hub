import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { doFetch, defaultEnv, createHelpers } from './m1-focus-setup';
import { Env } from '../src/env';
import worker from '../src/worker';
import { Hub } from '../src/hub';
import { createLoginCookie, resolveCallbackUrl, isAllowedOidcOrigin, allowedCallbackUrls } from '../src/auth/session';
import { createExecutionContext, waitOnExecutionContext, runInDurableObject } from 'cloudflare:test';

let helpers: any;
const originalFetch = globalThis.fetch;

beforeAll(async () => {
  helpers = await createHelpers();
});

afterAll(() => {
  globalThis.fetch = originalFetch;
});

describe('Web Auth Repair & Robustness', () => {

  it('retries a Workspace rejection with fresh state and the signed device destination', async () => {
    const returnTo = '/device?code=ABCD-EFGH';
    const start = await doFetch(new Request('http://example.com/auth/login?return_to=' + encodeURIComponent(returnTo), { redirect: 'manual' }));
    const cookie = start.headers.get('Set-Cookie')!.split(';')[0];
    const state = new URL(start.headers.get('Location')!).searchParams.get('state')!;
    const query = new URLSearchParams({ error: 'invalid_request', state,
      error_description: 'PreSignUp failed with error Sign-in is restricted to a verified account in the approved Workspace domain. <script>private</script>' });
    const failure = await doFetch(new Request('http://example.com/auth/callback?' + query, {
      headers: { Cookie: cookie, Accept: 'text/html' },
    }));
    expect(failure.status).toBe(400);
    expect(failure.headers.get('X-Auth-Error')).toBe('workspace_account_required');
    const html = await failure.text();
    expect(html).toContain('verified @datatalks.club account');
    expect(html).not.toContain('private</script>');
    expect(html).not.toContain('<nav');
    const retry = html.match(/href="([^" ]+)">Try sign-in again/)![1].replaceAll('&amp;', '&');
    expect(retry).toBe('/auth/login?return_to=' + encodeURIComponent(returnTo));
    const again = await doFetch(new Request('http://example.com' + retry, {
      headers: { Cookie: failure.headers.get('Set-Cookie')!.split(';')[0] }, redirect: 'manual',
    }));
    const next = new URL(again.headers.get('Location')!);
    expect(again.status).toBe(302);
    expect(next.searchParams.get('state')).not.toBe(state);
    expect(next.searchParams.get('prompt')).toBe('select_account');
    const freshCookie = again.headers.get('Set-Cookie')!.split(';')[0];
    const { getLoginCookie } = await import('../src/auth/session');
    const data = await getLoginCookie(defaultEnv, new Request('http://example.com', { headers: { Cookie: freshCookie } }), next.searchParams.get('state')!);
    expect(data?.returnTo).toBe(returnTo);
  });

  it('ignores unsigned provider return paths and raw error details', async () => {
    const query = new URLSearchParams({ error: 'invalid_request', return_to: '//evil.example', error_description: 'private-provider-detail' });
    const failure = await doFetch(new Request('http://example.com/auth/callback?' + query, { headers: { Accept: 'text/html' } }));
    const html = await failure.text();
    expect(html).toContain('href="/auth/login">Try sign-in again');
    expect(html).not.toContain('evil.example');
    expect(html).not.toContain('private-provider-detail');
    expect(html).not.toContain('<nav');
  });

  it('keeps device entry, review and results free of dashboard navigation', async () => {
    const { deviceEntry, deviceResult, deviceReview } = await import('../src/ui-pages');
    const pairing = {user_code:'ABCD-EFGH', client_kind:'host', requested_scopes:'[]', expires_at:new Date(Date.now()+600000).toISOString()};
    for (const page of [deviceEntry(''), deviceEntry('admin@fake.local'), deviceResult('expired', ''), deviceReview(pairing, 'admin@fake.local', null, null, [], Date.now(), null)]) {
      expect(page).not.toContain('<nav');
      expect(page).not.toContain('/conversations.html');
    }
  });

  it('concurrent logins in separate tabs retain both states in LoginJar and allow both callbacks to succeed', async () => {
    // Tab 1 initiates login with return_to
    const req1 = new Request('http://example.com/auth/login?return_to=/tab1', { redirect: 'manual' });
    const res1 = await doFetch(req1);
    expect(res1.status).toBe(302);
    const cookie1 = res1.headers.get('Set-Cookie')!.split(';')[0];
    const loc1 = new URL(res1.headers.get('Location')!);
    const state1 = loc1.searchParams.get('state')!;
    const nonce1 = loc1.searchParams.get('nonce')!;

    // Tab 2 initiates login carrying Tab 1's cookie (simulating same browser session)
    const req2 = new Request('http://example.com/auth/login?return_to=/tab2', {
      headers: { Cookie: cookie1 },
      redirect: 'manual',
    });
    const res2 = await doFetch(req2);
    expect(res2.status).toBe(302);
    const cookie2 = res2.headers.get('Set-Cookie')!.split(';')[0];
    const loc2 = new URL(res2.headers.get('Location')!);
    const state2 = loc2.searchParams.get('state')!;
    const nonce2 = loc2.searchParams.get('nonce')!;

    expect(state1).not.toBe(state2);

    const now = Math.floor(Date.now() / 1000);
    const token1 = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: nonce1, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });
    const token2 = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: nonce2, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });

    globalThis.fetch = (async (input: any) => {
      const s = input.toString();
      if (s.includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: token1 }), { status: 200 });
      }
      if (s.includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return originalFetch(input);
    }) as any;

    try {
      // Tab 1 callback arrives with cookie2 (which contains both tab1 and tab2 states)
      const cb1 = await doFetch(new Request(`http://example.com/auth/callback?code=code1&state=${state1}`, {
        headers: { Cookie: cookie2 },
        redirect: 'manual',
      }));
      expect(cb1.status).toBe(302);
      expect(cb1.headers.get('Location')).toBe('/tab1');
      const sessCookie1 = cb1.headers.get('Set-Cookie')!;
      expect(sessCookie1).toContain('hub_session=');

      // Now prepare mock for token2
      globalThis.fetch = (async (input: any) => {
        const s = input.toString();
        if (s.includes('/oauth2/token')) {
          return new Response(JSON.stringify({ id_token: token2 }), { status: 200 });
        }
        if (s.includes('/.well-known/jwks.json')) {
          return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
            status: 200, headers: { 'content-type': 'application/json' },
          });
        }
        return originalFetch(input);
      }) as any;

      // Extract updated hub_login from cb1 Set-Cookie if any, or use cookie2
      const allCookies = cb1.headers.getSetCookie ? cb1.headers.getSetCookie() : [cb1.headers.get('Set-Cookie')!];
      const updatedLoginCookie = allCookies.find(c => c.startsWith('hub_login='))?.split(';')[0] || cookie2;

      // Tab 2 callback arrives
      const cb2 = await doFetch(new Request(`http://example.com/auth/callback?code=code2&state=${state2}`, {
        headers: { Cookie: updatedLoginCookie },
        redirect: 'manual',
      }));
      expect(cb2.status).toBe(302);
      expect(cb2.headers.get('Location')).toBe('/tab2');
      expect(cb2.headers.get('Set-Cookie')).toContain('hub_session=');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('recovers an already authenticated user on stale or replayed callback', async () => {
    // First obtain a valid session cookie
    const loginReq = new Request('http://example.com/auth/login?return_to=/device?code=ABCD-1234', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const loc = new URL(loginRes.headers.get('Location')!);
    const state = loc.searchParams.get('state')!;
    const nonce = loc.searchParams.get('nonce')!;

    const now = Math.floor(Date.now() / 1000);
    const token = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });

    globalThis.fetch = (async (input: any) => {
      const s = input.toString();
      if (s.includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: token }), { status: 200 });
      }
      if (s.includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return originalFetch(input);
    }) as any;

    let sessionCookie: string;
    try {
      const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=ok&state=${state}`, {
        headers: { Cookie: loginCookie },
        redirect: 'manual',
      }));
      expect(cbRes.status).toBe(302);
      sessionCookie = cbRes.headers.get('Set-Cookie')!.split(';')[0];
    } finally {
      globalThis.fetch = originalFetch;
    }

    // Now user revisits callback with invalid or bogus state, but has active hub_sess
    const staleReq = new Request('http://example.com/auth/callback?code=stale&state=bogus_state', {
      headers: { Cookie: sessionCookie },
      redirect: 'manual',
    });
    const staleRes = await doFetch(staleReq);
    // Instead of error page 400, recovers and redirects to /
    expect(staleRes.status).toBe(302);
    expect(staleRes.headers.get('Location')).toBe('/');
  });

  it('renders bounded diagnostic category when provider returns an error (browser HTML)', async () => {
    const res = await doFetch(new Request('http://example.com/auth/callback?error=access_denied&error_description=User+cancelled', {
      headers: { Accept: 'text/html,application/xhtml+xml' },
    }));
    expect(res.status).toBe(400);
    expect(res.headers.get('X-Auth-Error')).toBe('oauth_access_denied');
    const html = await res.text();
    expect(html).toContain('id="auth-diagnostic"');
    expect(html).toContain('oauth_access_denied');
    expect(html).toContain('Provider returned an error: access_denied');
  });

  it('renders bounded diagnostic category as JSON for API callers', async () => {
    const res = await doFetch(new Request('http://example.com/auth/callback?error=access_denied&error_description=User+cancelled', {
      headers: { Accept: 'application/json' },
    }));
    expect(res.status).toBe(400);
    expect(res.headers.get('X-Auth-Error')).toBe('oauth_access_denied');
    const body = await res.json() as any;
    expect(body.category).toBe('oauth_access_denied');
  });

  it('renders missing_cookie diagnostic when state cookie is absent and not authenticated', async () => {
    const res = await doFetch(new Request('http://example.com/auth/callback?code=abc&state=nonexistent_state', {
      headers: { Accept: 'text/html' },
    }));
    expect(res.status).toBe(400);
    expect(res.headers.get('X-Auth-Error')).toBe('missing_cookie');
    const html = await res.text();
    expect(html).toContain('id="auth-diagnostic"');
    expect(html).toContain('missing_cookie');
  });

  it('renders state_not_found diagnostic when state in cookie is not in database', async () => {
    const fakeCookie = await createLoginCookie(defaultEnv, {
      state: 'fake_state_123',
      nonce: 'nonce123',
      verifier: 'verifier123',
      exp: Math.floor(Date.now() / 1000) + 600,
    });
    const res = await doFetch(new Request('http://example.com/auth/callback?code=abc&state=fake_state_123', {
      headers: { Cookie: fakeCookie },
    }));
    expect(res.status).toBe(400);
    expect(res.headers.get('X-Auth-Error')).toBe('state_not_found');
    const html = await res.text();
    expect(html).toContain('state_not_found');
  });

  it('renders state_already_consumed when state was already used (CAS defense)', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const loc = new URL(loginRes.headers.get('Location')!);
    const state = loc.searchParams.get('state')!;
    const nonce = loc.searchParams.get('nonce')!;

    const now = Math.floor(Date.now() / 1000);
    const token = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });

    globalThis.fetch = (async (input: any) => {
      const s = input.toString();
      if (s.includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: token }), { status: 200 });
      }
      if (s.includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return originalFetch(input);
    }) as any;

    try {
      // First use consumes state
      const cb1 = await doFetch(new Request(`http://example.com/auth/callback?code=c1&state=${state}`, {
        headers: { Cookie: loginCookie },
        redirect: 'manual',
      }));
      expect(cb1.status).toBe(302);

      // Replay attempt with same loginCookie (without session cookie to observe diagnostic)
      const cbReplay = await doFetch(new Request(`http://example.com/auth/callback?code=c2&state=${state}`, {
        headers: { Cookie: loginCookie },
      }));
      expect(cbReplay.status).toBe(400);
      expect(cbReplay.headers.get('X-Auth-Error')).toBe('state_already_consumed');
      const html = await cbReplay.text();
      expect(html).toContain('state_already_consumed');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('renders jwt_signature_invalid diagnostic when JWT signature is tampered', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const loc = new URL(loginRes.headers.get('Location')!);
    const state = loc.searchParams.get('state')!;
    const nonce = loc.searchParams.get('nonce')!;

    const now = Math.floor(Date.now() / 1000);
    const badToken = await helpers.signJwtBad({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce, token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });

    globalThis.fetch = (async (input: any) => {
      const s = input.toString();
      if (s.includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: badToken }), { status: 200 });
      }
      if (s.includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return originalFetch(input);
    }) as any;

    try {
      const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=bad&state=${state}`, {
        headers: { Cookie: loginCookie },
      }));
      expect(cbRes.status).toBe(400);
      expect(cbRes.headers.get('X-Auth-Error')).toBe('jwt_signature_invalid');
      const html = await cbRes.text();
      expect(html).toContain('jwt_signature_invalid');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('renders jwt_nonce_mismatch diagnostic when nonce does not match state', async () => {
    const loginReq = new Request('http://example.com/auth/login', { redirect: 'manual' });
    const loginRes = await doFetch(loginReq);
    const loginCookie = loginRes.headers.get('Set-Cookie')!.split(';')[0];
    const loc = new URL(loginRes.headers.get('Location')!);
    const state = loc.searchParams.get('state')!;

    const now = Math.floor(Date.now() / 1000);
    const badNonceToken = await helpers.signJwt({
      iss: 'https://auth.fake.local', aud: 'fake_client_id', exp: now + 3600,
      nonce: 'completely_different_nonce', token_use: 'id', email: 'admin@fake.local', email_verified: true, sub: 'admin-1',
    });

    globalThis.fetch = (async (input: any) => {
      const s = input.toString();
      if (s.includes('/oauth2/token')) {
        return new Response(JSON.stringify({ id_token: badNonceToken }), { status: 200 });
      }
      if (s.includes('/.well-known/jwks.json')) {
        return new Response(JSON.stringify({ keys: [{ ...helpers.jwkPub, kid: 'test-key-1' }] }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }
      return originalFetch(input);
    }) as any;

    try {
      const cbRes = await doFetch(new Request(`http://example.com/auth/callback?code=bad&state=${state}`, {
        headers: { Cookie: loginCookie },
      }));
      expect(cbRes.status).toBe(400);
      expect(cbRes.headers.get('X-Auth-Error')).toBe('jwt_nonce_mismatch');
      const html = await cbRes.text();
      expect(html).toContain('jwt_nonce_mismatch');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  describe('AUTH_CALLBACK_URLS Contract and Origin Resolution', () => {
    it('allowedCallbackUrls parses CSV with whitespace and preserves primary URL', () => {
      const testEnv = {
        AUTH_CALLBACK_URL: 'https://primary.example.com/auth/callback',
        AUTH_CALLBACK_URLS: 'https://primary.example.com/auth/callback, https://secondary.example.com/auth/callback , https://worker.workers.dev/auth/callback',
      } as any;
      const urls = allowedCallbackUrls(testEnv);
      expect(urls).toEqual([
        'https://primary.example.com/auth/callback',
        'https://secondary.example.com/auth/callback',
        'https://worker.workers.dev/auth/callback',
      ]);
    });

    it('resolveCallbackUrl matches caller request origin when listed', () => {
      const testEnv = {
        AUTH_CALLBACK_URL: 'https://primary.example.com/auth/callback',
        AUTH_CALLBACK_URLS: 'https://primary.example.com/auth/callback,https://secondary.example.com/auth/callback',
      } as any;
      const resolved = resolveCallbackUrl(testEnv, 'https://secondary.example.com/auth/login?return_to=/fleet');
      expect(resolved).toBe('https://secondary.example.com/auth/callback');
    });

    it('resolveCallbackUrl falls back to primary when origin is unlisted', () => {
      const testEnv = {
        AUTH_CALLBACK_URL: 'https://primary.example.com/auth/callback',
        AUTH_CALLBACK_URLS: 'https://primary.example.com/auth/callback,https://secondary.example.com/auth/callback',
      } as any;
      const resolved = resolveCallbackUrl(testEnv, 'https://attacker.example.com/auth/login');
      expect(resolved).toBe('https://primary.example.com/auth/callback');
    });

    it('isAllowedOidcOrigin returns true for listed origins and false for others', () => {
      const testEnv = {
        AUTH_CALLBACK_URL: 'https://primary.example.com/auth/callback',
        AUTH_CALLBACK_URLS: 'https://primary.example.com/auth/callback,https://secondary.example.com/auth/callback',
      } as any;
      expect(isAllowedOidcOrigin(testEnv, 'https://primary.example.com/any/path')).toBe(true);
      expect(isAllowedOidcOrigin(testEnv, 'https://secondary.example.com/auth/login')).toBe(true);
      expect(isAllowedOidcOrigin(testEnv, 'https://unauthorized.example.com/auth/login')).toBe(false);
    });

    it('resolveCallbackUrl and isAllowedOidcOrigin honor allowlisted X-Forwarded-Host', () => {
      const testEnv = {
        AUTH_CALLBACK_URL: 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback',
        AUTH_CALLBACK_URLS: 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback,https://hub.dtcdev.click/auth/callback',
      } as any;
      expect(isAllowedOidcOrigin(testEnv, 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/login', 'hub.dtcdev.click')).toBe(true);
      expect(resolveCallbackUrl(testEnv, 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/login', 'hub.dtcdev.click')).toBe('https://hub.dtcdev.click/auth/callback');

      // Unallowlisted host falls back safely
      expect(isAllowedOidcOrigin(testEnv, 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/login', 'evil.dtcdev.click')).toBe(true); // origin is workers.dev which is allowed
      expect(resolveCallbackUrl(testEnv, 'https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/login', 'evil.dtcdev.click')).toBe('https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback');
    });

    it('Hub Durable Object uses X-Forwarded-Host to pick matching callback URL', async () => {
      const id = defaultEnv.HUB.idFromName("hub");
      const stub = defaultEnv.HUB.get(id);

      await runInDurableObject(stub, async (instance: Hub) => {
        const oldEnv = (instance as any).env;
        (instance as any).env = {
          ...oldEnv,
          AUTH_CALLBACK_URL: 'https://workers.dev/auth/callback',
          AUTH_CALLBACK_URLS: 'https://workers.dev/auth/callback,https://hub.dtcdev.click/auth/callback',
        };
      });

      try {
        const req = new Request('https://workers.dev/auth/login', {
          headers: { 'X-Forwarded-Host': 'hub.dtcdev.click' },
          redirect: 'manual',
        });
        const res = await doFetch(req);
        expect(res.status).toBe(302);
        const loc = new URL(res.headers.get('Location')!);
        expect(loc.searchParams.get('redirect_uri')).toBe('https://hub.dtcdev.click/auth/callback');
      } finally {
        await runInDurableObject(stub, async (instance: Hub) => {
          (instance as any).env = defaultEnv;
        });
      }
    });
  });

});
