import { beforeAll, describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { defaultEnv, doFetch } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const origin = 'http://example.com';
const sub = 'mcp-compat-admin';
const csrf = 'mcp-compat-csrf';
let cookie: string;
function inHub(fn: (h: any) => any) { return runInDurableObject(defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub')), fn); }
function form(path: string, params: Record<string, string>, headers: Record<string, string> = {}) {
  return doFetch(new Request(origin + path, { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/x-www-form-urlencoded', ...headers }, body: new URLSearchParams(params) }));
}
async function register(body: any) {
  return doFetch(new Request(origin + '/oauth/register', { method: 'POST', body: JSON.stringify(body) }));
}
const verifier = 'w'.repeat(64);

beforeAll(async () => {
  await inHub((h: any) => {
    h.sql.exec('DELETE FROM mcp_rate WHERE bucket LIKE ?', 'register:%');
    const now = new Date().toISOString(), exp = new Date(Date.now() + 86400000).toISOString();
    h.sql.exec('INSERT OR IGNORE INTO users(sub,email,is_admin,first_seen_at,last_seen_at) VALUES(?,?,1,?,?)', sub, 'admin@fake.local', now, now);
  });
  cookie = (await createSessionCookie(defaultEnv, { sub, email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
});

describe('MCP OAuth client compatibility (b2f0943b)', () => {
  it('1: DCR accepts cursor://, claude://, vscode:// and multi-URI arrays', async () => {
    const r = await register({ client_name: 'cursor', redirect_uris: ['cursor://anysphere.cursor-mcp/oauth/callback', 'http://localhost:4555/callback'] });
    expect(r.status).toBe(201);
    for (const redirect of ['claude://oauth/callback', 'vscode://publisher.extension/oauth/callback', 'https://app.example/cb', 'http://127.0.0.1:8080/cb', 'http://[::1]:8080/cb']) {
      const res = await register({ redirect_uris: [redirect] });
      expect(res.status, redirect).toBe(201);
    }
  });
  it('1: DCR still rejects dangerous and non-loopback http schemes', async () => {
    for (const redirect of ['javascript:alert(1)', 'data:text/plain,hi', 'file:///etc/passwd', 'http://evil.test/cb', 'https://user:pass@evil.test/cb', 'https://evil.test/#x']) {
      const res = await register({ redirect_uris: [redirect] });
      expect(res.status, redirect).toBe(400);
    }
  });
  it('2: resource defaults and normalizes on authorize and token', async () => {
    const client: any = await (await register({ redirect_uris: ['http://127.0.0.1:4666/cb'] })).json();
    const base = { client_id: client.client_id, redirect_uri: client.redirect_uris[0], response_type: 'code', code_challenge: await sha256(verifier), code_challenge_method: 'S256', state: 's' };
    // Missing resource on authorize: login redirect preserves flow, then consent renders.
    const login = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams(base), { redirect: 'manual' }));
    expect(login.status).toBe(303);
    for (const resource of [origin + '/mcp', origin, origin + '/', origin + '/mcp/']) {
      const page = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams({ ...base, resource }), { headers: { Cookie: cookie } }));
      expect(page.status, resource).toBe(200);
    }
    // Missing resource on authorize POST still issues a code bound to canonical /mcp.
    const allow = await form('/oauth/authorize', { ...base, csrf, decision: 'allow' }, { Cookie: cookie, Origin: origin });
    expect(allow.status).toBe(303);
    const target = new URL(allow.headers.get('Location')!);
    expect(target.searchParams.get('code')).toBeTruthy();
    const code = target.searchParams.get('code')!;
    // Missing resource on token defaults to canonical and redeems.
    const token = await form('/oauth/token', { client_id: client.client_id, code, redirect_uri: client.redirect_uris[0], grant_type: 'authorization_code', code_verifier: verifier }, { Origin: origin } as any);
    // form() sets content-type; resource omitted on purpose.
    expect(token.status).toBe(200);
    // Wrong resource still rejected.
    const bad = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams({ ...base, resource: 'https://evil.test/mcp' }), { headers: { Cookie: cookie } }));
    expect(bad.status).toBe(400);
  });
  it('3: missing and standard scopes map to the admin grant', async () => {
    for (const scope of [undefined, 'admin', 'openid', 'openid email profile', 'offline_access', 'claudeai', 'mcp', 'openid email profile offline_access claudeai mcp', 'unknown_future_scope']) {
      const body: any = { redirect_uris: ['http://127.0.0.1:4777/cb'] };
      if (scope !== undefined) body.scope = scope;
      const res = await register(body);
      expect(res.status, String(scope)).toBe(201);
      expect(((await res.json()) as any).scope).toBe('admin');
    }
    const client: any = await (await register({ redirect_uris: ['http://127.0.0.1:4888/cb'] })).json();
    const base = { client_id: client.client_id, redirect_uri: client.redirect_uris[0], response_type: 'code', code_challenge: await sha256(verifier), code_challenge_method: 'S256', resource: origin + '/mcp' };
    for (const scope of ['openid email profile', 'claudeai', 'unknown_future_scope']) {
      const page = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams({ ...base, scope }), { headers: { Cookie: cookie } }));
      expect(page.status, scope).toBe(200);
    }
  });
  it('4: RFC 9207 iss + openid-configuration alias', async () => {
    const as: any = await (await doFetch(new Request(origin + '/.well-known/oauth-authorization-server'))).json();
    expect(as.authorization_response_iss_parameter_supported).toBe(true);
    expect(as.issuer).toBe(origin);
    const oidc: any = await (await doFetch(new Request(origin + '/.well-known/openid-configuration'))).json();
    expect(oidc).toEqual(as);
    const client: any = await (await register({ redirect_uris: ['http://127.0.0.1:4999/cb'] })).json();
    const params = { client_id: client.client_id, redirect_uri: client.redirect_uris[0], response_type: 'code', code_challenge: await sha256(verifier), code_challenge_method: 'S256', resource: origin + '/mcp', scope: 'openid', state: 's', csrf, decision: 'allow' };
    const allow = await form('/oauth/authorize', params, { Cookie: cookie, Origin: origin });
    expect(allow.status).toBe(303);
    const target = new URL(allow.headers.get('Location')!);
    expect(target.searchParams.get('iss')).toBe(origin);
    expect(target.searchParams.get('code')).toBeTruthy();
  });
  it('5: DCR clients do not expire while in use', async () => {
    const client: any = await (await register({ redirect_uris: ['http://127.0.0.1:5001/cb'] })).json();
    const before: any = await inHub((h: any) => h.sql.exec('SELECT expires FROM mcp_clients WHERE id=?', client.client_id).toArray()[0]);
    expect(before.expires - Date.now()).toBeGreaterThan(90 * 86400000);
    // Sliding renewal: fake age, then authorize + token push expiry forward.
    await inHub((h: any) => h.sql.exec('UPDATE mcp_clients SET expires=? WHERE id=?', Date.now() + 1000, client.client_id));
    const params = { client_id: client.client_id, redirect_uri: client.redirect_uris[0], response_type: 'code', code_challenge: await sha256(verifier), code_challenge_method: 'S256', resource: origin + '/mcp', state: 's', csrf, decision: 'allow' };
    const allow = await form('/oauth/authorize', params, { Cookie: cookie, Origin: origin });
    expect(allow.status).toBe(303);
    const afterAuth: any = await inHub((h: any) => h.sql.exec('SELECT expires FROM mcp_clients WHERE id=?', client.client_id).toArray()[0]);
    expect(afterAuth.expires - Date.now()).toBeGreaterThan(30 * 86400000);
    const code = new URL(allow.headers.get('Location')!).searchParams.get('code')!;
    const token = await form('/oauth/token', { client_id: client.client_id, code, redirect_uri: client.redirect_uris[0], grant_type: 'authorization_code', code_verifier: verifier, resource: origin + '/mcp' });
    expect(token.status).toBe(200);
    const afterToken: any = await inHub((h: any) => h.sql.exec('SELECT expires FROM mcp_clients WHERE id=?', client.client_id).toArray()[0]);
    expect(afterToken.expires - Date.now()).toBeGreaterThan(30 * 86400000);
  });
});
