import { describe, it, expect } from 'vitest';
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import worker from '../src/worker';
import { defaultEnv, doFetch } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';

describe('private fleet assets', () => {
  it('protects every dashboard asset and does not trust an arbitrary cookie', async () => {
    for (const path of ['/', '/fleet.html', '/fleet.js', '/fleet.css']) {
      const r = await doFetch(new Request('https://hub.test' + path, { headers: { Cookie: 'hub_session=fake' } }));
      expect(r.status).toBe(303); expect(r.headers.get('Location')).toBe('/auth/login');
    }
  });
  it('serves assets only after a valid admin session and sets private cache and CSP headers', async () => {
    const cookie = await createSessionCookie(defaultEnv, { sub: 'human-test', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test', exp: Math.floor(Date.now() / 1000) + 60 });
    let assetCalls = 0;
    const bindings = { ...defaultEnv, ASSETS: { fetch: async () => { assetCalls++; return new Response('private dashboard'); } } };
    const ctx = createExecutionContext();
    const r = await worker.fetch(new Request('https://hub.test/', { headers: { Cookie: cookie } }), bindings as any, ctx);
    await waitOnExecutionContext(ctx);
    expect(r.status).toBe(200); expect(assetCalls).toBe(1);
    expect(r.headers.get('Cache-Control')).toBe('private, no-store');
    expect(r.headers.get('Content-Security-Policy')).toContain("frame-ancestors 'none'");
  });
  it('requires CSRF and admin scope for account updates even with a valid browser session', async () => {
    const cookie = await createSessionCookie(defaultEnv, { sub: 'human-test', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test', exp: Math.floor(Date.now() / 1000) + 60 });
    const r = await doFetch(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', headers: { Cookie: cookie }, body: JSON.stringify({ accounts: [], routes: [] }) }));
    expect(r.status).toBe(403);
  });
  it('offers narrow spawn scope only through agent pairing, without returning a credential before approval', async () => {
    const start = async (kind: string, scopes: string[]) => doFetch(new Request('https://hub.test/api/device/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_kind: kind, name: 'private-coordinator', requested_scopes: scopes }) }));
    expect((await start('host', ['host', 'bus:send', 'bus:read', 'health:report'])).status).toBe(200);
    expect((await start('host', ['fleet:launch'])).status).toBe(400);
    expect((await start('agent', ['jobs:create'])).status).toBe(400);
    const response = await start('agent', ['bus:send', 'bus:read', 'fleet:launch']);
    expect(response.status).toBe(200);
    const code: any = await response.json(); expect(code.access_token).toBeUndefined();
    const pending = await doFetch(new Request('https://hub.test/api/device/token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: code.device_code }) }));
    const result: any = await pending.json(); expect(result.error).toBe('authorization_pending'); expect(result.access_token).toBeUndefined();
  });
});
