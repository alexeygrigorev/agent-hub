import { beforeAll, describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { defaultEnv, doFetch } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { CLI_EXCLUSIONS, MCP_ROUTES, publicTools, toolRequest, dispatchToolRequest } from '../src/mcp-tools';
import cliCommands from './mcp-cli-commands.json';

const origin = 'http://example.com';
const bearer = 'hubt_mcp_test_human';
const csrf = 'mcp-csrf';
const sub = 'mcp-admin';
let cookie: string;
async function inHub(fn: (h: any) => any) { return runInDurableObject(defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub')), fn); }
function rpc(method: string, params?: any, auth = bearer, extra: Record<string, string> = {}) {
  return doFetch(new Request(origin + '/mcp', { method: 'POST', headers: {
    Authorization: 'Bearer ' + auth, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', ...extra,
  }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, ...(params ? { params } : {}) }) }));
}
function form(path: string, params: Record<string, string>, headers = {}) {
  return doFetch(new Request(origin + path, { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/x-www-form-urlencoded', ...headers }, body: new URLSearchParams(params) }));
}
async function register(redirect = 'http://127.0.0.1:4567/callback') {
  const response = await doFetch(new Request(origin + '/oauth/register', { method: 'POST', body: JSON.stringify({ client_name: 'test <client>', redirect_uris: [redirect] }) }));
  expect(response.status).toBe(201);
  return response.json() as Promise<any>;
}
const verifier = 'v'.repeat(64);
async function authorization(client?: any) {
  client ||= await register();
  return { client, params: { client_id: client.client_id, redirect_uri: client.redirect_uris[0], response_type: 'code', code_challenge: await sha256(verifier), code_challenge_method: 'S256', resource: origin + '/mcp', scope: 'admin', state: 'mcp-state' } };
}
async function grant(client?: any) {
  const { client: c, params } = await authorization(client);
  const response = await form('/oauth/authorize', { ...params, csrf, decision: 'allow' }, { Cookie: cookie, Origin: origin });
  expect(response.status).toBe(303);
  const target = new URL(response.headers.get('Location')!);
  expect(target.searchParams.get('state')).toBe('mcp-state');
  return { client: c, code: target.searchParams.get('code')! };
}
async function redeem(g: any, extra = {}) {
  return form('/oauth/token', { client_id: g.client.client_id, code: g.code, redirect_uri: g.client.redirect_uris[0], grant_type: 'authorization_code', code_verifier: verifier, resource: origin + '/mcp', ...extra });
}
beforeAll(async () => {
  const hash = await sha256(bearer);
  await inHub(h => {
    const now = new Date().toISOString(), exp = new Date(Date.now() + 86400000).toISOString();
    h.sql.exec('INSERT INTO users(sub,email,is_admin,first_seen_at,last_seen_at) VALUES(?,?,1,?,?)', sub, 'admin@fake.local', now, now);
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)', 'mcp-test-human', 'human', hash, 'mcp-test-family', 'admin@fake.local', 'human:' + sub, '["admin"]', sub, now, exp);
  });
  cookie = (await createSessionCookie(defaultEnv, { sub, email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
});
describe('Hub MCP protocol and parity', () => {
  it('updates intake with optional key/version and preserves concise append and held diagnostics', async () => {
    const invoke = async (name: string, args: any) => {
      const response: any = await (await rpc('tools/call', { name, arguments: args })).json();
      return JSON.parse(response.result.content[0].text);
    };
    const created = await invoke('intake_create', { payload: { key: 'mcp-convenience-create', project: 'demo', title: 'Fixture', text: 'Fixture', source: { type: 'unknown' } } });
    const id = created.data.task.id;
    const updated = await invoke('intake_update', { id, payload: { owner: 'fixture-owner' } });
    expect(updated.status).toBe(200);
    expect(updated.data.task.control.version).toBe(2);
    expect(updated.data.task.payload_data.owner).toBe('fixture-owner');
    const stale = await invoke('intake_update', { id, payload: { expected_version: 1, owner: 'stale-owner' } });
    expect(stale).toMatchObject({ status: 409, data: { error: 'version_conflict' } });
    expect((await invoke('intake_append', { id, payload: { key: 'mcp-note', expected_version: 2, event: { kind: 'note', text: 'Fixture' } } })).status).toBe(200);
    expect((await invoke('intake_append', { id, payload: { key: 'mcp-session', expected_version: 3, event: { kind: 'session', native_id: 'fixture-native' } } })).status).toBe(200);
    const held = await invoke('intake_assign', { id, payload: { key: 'mcp-assign', expected_version: 4, session: { workspace: 'demo', engine: 'codex', mode: 'headless', brief: 'Synthetic fixture goal' }, provenance: { type: 'unknown' } } });
    expect(held.status, JSON.stringify(held.data)).toBe(200);
    expect(held.data.assigned_session.state).toBe('held');
    expect(held.data.hold).toBeTruthy();
    expect(held.data.assigned_session.hold).toEqual(held.data.hold);
  });
  it('retries only CAS conflicts with a stable key and bounded attempts', async () => {
    for (const code of ['version_conflict', 'idempotency_conflict']) {
      const writes: any[] = [];
      let reads = 0;
      const response = await dispatchToolRequest(toolRequest(origin, 'intake_update', { id: 'fixture', payload: { owner: 'fixture' } }, 'Bearer fixture'), 'intake_update', async r => {
        expect(r.headers.get('Authorization')).toBe('Bearer fixture');
        if (r.method === 'GET') return Response.json({ task: { control: { version: ++reads } } });
        writes.push(await r.json());
        return Response.json({ error: code }, { status: 409 });
      });
      expect(response.status).toBe(409);
      expect(writes.length).toBe(code === 'version_conflict' ? 3 : 1);
      expect(new Set(writes.map(w => w.key)).size).toBe(1);
      expect(writes.map(w => w.expected_version)).toEqual(code === 'version_conflict' ? [1, 2, 3] : [1]);
    }
  });
  it('retains the generated key and original version on an uncertain update without retry', async () => {
    let writes = 0;
    const response = await dispatchToolRequest(toolRequest(origin, 'intake_update', { id: 'fixture', payload: { owner: 'fixture' } }, 'Bearer fixture'), 'intake_update', async r => {
      if (r.method === 'GET') return Response.json({ task: { control: { version: 7 } } });
      writes++;
      throw new Error('private diagnostic');
    });
    expect(response.status).toBe(502);
    expect(await response.json()).toMatchObject({ error: 'intake_update_outcome_unknown', key: expect.stringMatching(/^update-/), expected_version: 7 });
    expect(writes).toBe(1);
  });
  it('requires bearer auth with protected-resource discovery', async () => {
    const r = await rpc('tools/list', undefined, 'invalid');
    expect(r.status).toBe(401);
    expect(r.headers.get('WWW-Authenticate')).toContain('/.well-known/oauth-protected-resource/mcp');
  });
  it('negotiates supported versions and accepts initialized notification', async () => {
    const r: any = await (await rpc('initialize', { protocolVersion: 'future', capabilities: {}, clientInfo: { name: 'test', version: '1' } })).json();
    expect(r.result.protocolVersion).toBe('2025-11-25');
    expect(r.result.capabilities.tools).toBeTruthy();
    const n = await doFetch(new Request(origin + '/mcp', { method: 'POST', headers: { Authorization: 'Bearer ' + bearer, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' }, body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) }));
    expect(n.status).toBe(202);
    expect(await n.text()).toBe('');
  });
  it('rejects cookie authority, foreign origins and bad versions', async () => {
    expect((await rpc('ping', undefined, bearer, { Cookie: cookie })).status).toBe(403);
    expect((await rpc('ping', undefined, bearer, { Origin: 'https://evil.test' })).status).toBe(403);
    expect((await rpc('ping', undefined, bearer, { 'MCP-Protocol-Version': 'unknown' })).status).toBe(400);
  });
  it('returns method and parameter errors', async () => {
    expect(((await (await rpc('unknown')).json()) as any).error.code).toBe(-32601);
    expect(((await (await rpc('tools/call', { name: 'unknown' })).json()) as any).error.code).toBe(-32602);
    expect(((await (await rpc('tools/call', { name: 'sessions_show', arguments: { id: '../fleet' } })).json()) as any).error.code).toBe(-32602);
  });
  it('does not offer SSE or an unbounded request parser', async () => {
    expect((await doFetch(new Request(origin + '/mcp', { headers: { Authorization: 'Bearer ' + bearer } }))).status).toBe(405);
    const r = await doFetch(new Request(origin + '/mcp', { method: 'POST', headers: { Authorization: 'Bearer ' + bearer, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' }, body: 'x'.repeat(32769) }));
    expect(r.status).toBe(413);
  });
  it('marks every mutation destructive, especially send/stop', async () => {
    const result: any = await (await rpc('tools/list')).json();
    expect(result.result.tools.length).toBe(MCP_ROUTES.length);
    for (const name of ['sessions_send', 'sessions_stop', 'bus_send']) expect(result.result.tools.find((t: any) => t.name === name).annotations.destructiveHint).toBe(true);
    expect(new Set(publicTools().map(t => t.name)).size).toBe(MCP_ROUTES.length);
  });
  it('covers EVERY actual CLI leaf, with a documented exclusion when local-only', () => {
    const covered = new Set(MCP_ROUTES.flatMap(r => r.cli));
    for (const command of cliCommands) expect(covered.has(command) || Boolean(CLI_EXCLUSIONS[command]), command).toBe(true);
  });
  it('passes the exact bearer into real API routes and preserves errors', async () => {
    const direct = await doFetch(new Request(origin + '/api/sessions/tree', { headers: { Authorization: 'Bearer ' + bearer } }));
    const mcp: any = await (await rpc('tools/call', { name: 'sessions_tree' })).json();
    expect(JSON.parse(mcp.result.content[0].text)).toEqual({ status: direct.status, data: await direct.json() });
    const missing: any = await (await rpc('tools/call', { name: 'sessions_show', arguments: { id: 'missing' } })).json();
    expect(missing.result.isError).toBe(true);
    expect(JSON.parse(missing.result.content[0].text).status).toBe(404);
  });
  it('cancels an owned held intent through real MCP bearer dispatch with replay', async () => {
    const id = crypto.randomUUID();
    await inHub(h => {
      const now = new Date().toISOString();
      h.sql.exec(`INSERT INTO session_control_sessions
        (id,actor,request_key,request_json,mode,state,owner_token_id,created_at,updated_at)
        VALUES (?,?,'mcp-cancel-fixture','{"workspace":"mcp-fixture"}','interactive','held','mcp-test-human',?,?)`,
        id, 'human:' + sub, now, now);
    });
    const params = {name:'sessions_cancel', arguments:{id,payload:{key:'mcp-cancel-once'}}};
    const first: any = await (await rpc('tools/call', params)).json();
    const data = JSON.parse(first.result.content[0].text);
    expect(first.result.isError).toBe(false);
    expect(data.status).toBe(200);
    expect(data.data.state).toBe('cancelled');
    expect(data.data.cancellation.effect).toBe('no_native_start');
    const second: any = await (await rpc('tools/call', params)).json();
    expect(JSON.parse(second.result.content[0].text).data.replay).toBe(true);
    expect(publicTools().find(t => t.name==='sessions_cancel')?.annotations.destructiveHint).toBe(true);
  });
  it('protects an owned session through the real MCP route with a durable replay', async () => {
    const id = crypto.randomUUID();
    await inHub(h => {
      const now = new Date().toISOString();
      h.sql.exec(`INSERT INTO session_control_sessions (id,actor,request_key,request_json,mode,state,owner_token_id,created_at,updated_at)
        VALUES (?,?,'mcp-protect-fixture','{"workspace":"mcp-fixture"}','interactive','held','mcp-test-human',?,?)`,id,'human:'+sub,now,now);
    });
    const params = {name:'sessions_protect',arguments:{id,payload:{key:'mcp-protect-once'}}};
    const first:any = await(await rpc('tools/call',params)).json();
    expect(first.result.isError).toBe(false);
    const receipt=JSON.parse(first.result.content[0].text).data.cleanup_protection;
    expect(receipt.permanent).toBe(true);
    const replay:any=await(await rpc('tools/call',params)).json();
    expect(JSON.parse(replay.result.content[0].text).data.cleanup_protection).toEqual(receipt);
    expect(JSON.parse(replay.result.content[0].text).data.replay).toBe(true);
  });
  it('legacy launch tools retain their CLI mode semantics', async () => {
    const r = toolRequest(origin, 'start_session', { payload: { workspace: 'x', machine: 'm' } }, 'Bearer x');
    expect((await r.json() as any).mode).toBe('interactive');
    expect((await toolRequest(origin, 'start_agent', { payload: { workspace: 'x' } }, 'Bearer x').json() as any).mode).toBe('headless');
    expect(() => toolRequest(origin, 'start_session', { payload: { mode: 'headless' } }, 'Bearer x')).toThrow('invalid_launch_mode');
  });
  it('path/query/payload validation never permits another origin or route', () => {
    for (const id of ['..', '.', '../x', '%2e%2e', 'https://evil.test', 'x\\y', 'x?y', 'x#z']) expect(() => toolRequest(origin, 'sessions_show', { id }, bearer)).toThrow();
    expect(() => toolRequest(origin, 'fleet', { payload: {} }, bearer)).toThrow();
    expect(() => toolRequest(origin, 'fleet', { query: { x: {} } }, bearer)).toThrow();
    const r = toolRequest(origin, 'claims_release', { id: 'project:src' }, 'Bearer x');
    expect(new URL(r.url).pathname).toBe('/api/bus/claims/project%3Asrc/release');
    expect(r.headers.get('Cookie')).toBeNull();
    expect(new URL(toolRequest(origin, 'claims_release', { id: 'agent-hub:src/mcp.ts' }, 'Bearer x').url).pathname).toBe('/api/bus/claims/agent-hub%3Asrc%2Fmcp.ts/release');
  });
  it('keeps restricted credentials restricted through MCP', async () => {
    const token = 'hubt_mcp_routing', hash = await sha256(token);
    await inHub(h => h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)', 'mcp-routing', 'routing-client', hash, 'mcp-routing', 'MCP routing test', 'routing-client:mcp-test', '["routing:submit"]', sub, new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()));
    const mcp: any = await (await rpc('tools/call', { name: 'sessions_stop', arguments: { id: 'other', payload: { key: 'stop' } } }, token)).json();
    expect(mcp.result.isError).toBe(true);
    expect(JSON.parse(mcp.result.content[0].text).status).toBe(404);
    const create: any = await (await rpc('tools/call', { name: 'sessions_start', arguments: { payload: { key: 'denied', workspace: 'agent-hub' } } }, token)).json();
    expect(JSON.parse(create.result.content[0].text).status).toBe(403);
  });
});
describe('OAuth discovery, DCR, consent, PKCE and credential lifecycle', () => {
  it('publishes separate Hub metadata', async () => {
    const pr: any = await (await doFetch(new Request(origin + '/.well-known/oauth-protected-resource/mcp'))).json();
    expect(pr.resource).toBe(origin + '/mcp');
    expect(pr.authorization_servers).toEqual([origin]);
    const as: any = await (await doFetch(new Request(origin + '/.well-known/oauth-authorization-server'))).json();
    expect(as.code_challenge_methods_supported).toEqual(['S256']);
    expect(as.registration_endpoint).toBe(origin + '/oauth/register');
  });
  it('rejects insecure and malformed redirect metadata', async () => {
    for (const redirect of ['http://evil.test/cb', 'https://user:pass@evil.test/cb', 'https://evil.test/#x', 'javascript:alert(1)']) {
      const r = await doFetch(new Request(origin + '/oauth/register', { method: 'POST', body: JSON.stringify({ redirect_uris: [redirect] }) }));
      expect(r.status).toBe(400);
    }
  });
  it('requires login, exact resource/redirect and PKCE S256 before consent', async () => {
    const { params } = await authorization();
    const r = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams(params), { redirect: 'manual' }));
    expect(r.status).toBe(303);
    expect(r.headers.get('Location')).toContain('/auth/login?return_to=');
    for (const change of [{ redirect_uri: 'https://evil.test' }, { resource: 'https://evil.test/mcp' }, { code_challenge_method: 'plain' }]) {
      expect((await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams({ ...params, ...change })))).status).toBe(400);
    }
  });
  it('escapes consent content and requires explicit CSRF-protected approval', async () => {
    const { params } = await authorization();
    const r = await doFetch(new Request(origin + '/oauth/authorize?' + new URLSearchParams(params), { headers: { Cookie: cookie } }));
    expect(r.status).toBe(200);
    const html = await r.text();
    expect(html).toContain('test &lt;client&gt;');
    // dakit auth shell (not plain HTML)
    expect(html).toContain('/vendor/dakit/dakit.css');
    expect(html).toContain('dk-auth-page');
    expect(html).toContain('Allow connection');
    expect((await form('/oauth/authorize', { ...params, csrf: 'wrong', decision: 'allow' }, { Cookie: cookie, Origin: origin })).status).toBe(403);
    // Cross-origin Origin must still fail even with a valid CSRF token.
    expect((await form('/oauth/authorize', { ...params, csrf, decision: 'allow' }, { Cookie: cookie, Origin: 'https://evil.test' })).status).toBe(403);
    // Missing Origin (common for same-site form POSTs / Cursor) must succeed with valid CSRF.
    const allowedNoOrigin = await form('/oauth/authorize', { ...params, csrf, decision: 'allow' }, { Cookie: cookie });
    expect(allowedNoOrigin.status).toBe(303);
    expect(new URL(allowedNoOrigin.headers.get('Location')!).searchParams.get('code')).toBeTruthy();
    const denied = await form('/oauth/authorize', { ...params, csrf, decision: 'deny' }, { Cookie: cookie, Origin: origin });
    expect(new URL(denied.headers.get('Location')!).searchParams.get('error')).toBe('access_denied');
  });
  it('preserves OAuth return_to through bootstrap admin-token sign-in', async () => {
    const token = 'mcp-bootstrap-private-fixture';
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token)))].map(x => x.toString(16).padStart(2, '0')).join('');
    const { params } = await authorization();
    const next = '/oauth/authorize?' + new URLSearchParams(params);
    await inHub(h => { h.env.HUB_ADMIN_TOKEN_SHA256 = hash; });
    try {
      const login = await doFetch(new Request(origin + '/auth/login?return_to=' + encodeURIComponent(next), { redirect: 'manual' }));
      expect(login.headers.get('Location')).toBe('/auth/token?return_to=' + encodeURIComponent(next));
      const signed = await form('/auth/token', { token, return_to: next }, { Origin: origin });
      expect(signed.status).toBe(303);
      expect(signed.headers.get('Location')).toBe(next);
      const consent = await doFetch(new Request(origin + next, { redirect: 'manual', headers: { Cookie: signed.headers.get('Set-Cookie')!.split(';')[0] } }));
      expect(consent.status).toBe(200);
      const evil = await form('/auth/token', { token, return_to: '//evil.test' }, { Origin: origin });
      expect(evil.headers.get('Location')).toBe('/');
    } finally { await inHub(h => { delete h.env.HUB_ADMIN_TOKEN_SHA256; }); }
  });
  it('binds code to client, verifier, redirect, resource and single redemption', async () => {
    const g = await grant();
    for (const changes of [{ code_verifier: 'b'.repeat(64) }, { redirect_uri: 'http://localhost:9999/cb' }, { resource: origin + '/other' }, { client_id: (await register()).client_id }]) expect((await redeem(g, changes)).status).toBe(400);
    const r = await redeem(g); expect(r.status).toBe(200);
    const t: any = await r.json();
    expect(t.scope).toBe('admin');
    expect((await rpc('ping', undefined, t.access_token)).status).toBe(200);
    expect((await redeem(g)).status).toBe(400);
  });
  it('supports a remote HTTPS DCR client with no localhost dependency', async () => {
    const client = await register('https://remote-client.example/oauth/callback');
    const g = await grant(client);
    const response = await redeem(g); expect(response.status).toBe(200);
    const tokens: any = await response.json();
    const tools: any = await (await rpc('tools/list', undefined, tokens.access_token)).json();
    expect(tools.result.tools.some((t: any) => t.name === 'sessions_start')).toBe(true);
  });
  it('rejects an OAuth token at a different resource origin', async () => {
    const t: any = await (await redeem(await grant())).json();
    const r = await doFetch(new Request('https://other.example/mcp', { method: 'POST', headers: { Authorization: 'Bearer ' + t.access_token, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'ping' }) }));
    expect(r.status).toBe(401);
  });
  it('allows only one concurrent code redemption', async () => {
    const g = await grant();
    const responses = await Promise.all([redeem(g), redeem(g)]);
    expect(responses.map(r => r.status).sort()).toEqual([200, 400]);
  });
  it('expires codes and rechecks the current verified user before issuance', async () => {
    const g = await grant(); const hash = await sha256(g.code);
    await inHub(h => h.sql.exec('UPDATE mcp_codes SET expires=0 WHERE hash=?', hash));
    expect((await redeem(g)).status).toBe(400);
    const second = await grant();
    await inHub(h => h.sql.exec('UPDATE users SET email=? WHERE sub=?', 'removed@fake.local', sub));
    try { expect((await redeem(second)).status).toBe(400); }
    finally { await inHub(h => h.sql.exec('UPDATE users SET email=? WHERE sub=?', 'admin@fake.local', sub)); }
  });
  it('rotates refresh tokens with client binding and revokes the entire family', async () => {
    const g = await grant(); const t: any = await (await redeem(g)).json();
    const refresh = { client_id: g.client.client_id, resource: origin + '/mcp', grant_type: 'refresh_token', refresh_token: t.refresh_token };
    expect((await form('/oauth/token', { ...refresh, client_id: (await register()).client_id })).status).toBe(400);
    const direct = await doFetch(new Request(origin + '/api/token/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: t.refresh_token }) }));
    expect(direct.status).toBe(400);
    const r = await form('/oauth/token', refresh); expect(r.status).toBe(200);
    const rotated: any = await r.json(); expect(rotated.access_token).not.toBe(t.access_token);
    expect((await form('/oauth/token', refresh)).status).toBe(400);
    expect((await rpc('ping', undefined, rotated.access_token)).status).toBe(401);
    const fresh: any = await (await redeem(await grant(g.client))).json();
    expect((await form('/oauth/revoke', { client_id: g.client.client_id, token: fresh.refresh_token })).status).toBe(200);
    expect((await rpc('ping', undefined, fresh.access_token)).status).toBe(401);
  });
});
