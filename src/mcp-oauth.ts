import type { Env } from './env';
import { getSession, humanAdminAllowed } from './auth/session';
import { randomString, sha256 } from './auth/crypto';
import { generateTokenPair } from './auth/token';
import { boundedText, json, NO_STORE } from './mcp';
import { mcpConnectPage, mcpConsentHeaders } from './ui-pages';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS mcp_clients (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_codes (hash TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL, challenge TEXT NOT NULL, resource TEXT NOT NULL, sub TEXT NOT NULL, email TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_grants (family TEXT PRIMARY KEY, client TEXT NOT NULL, resource TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mcp_rate (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL);
`;
const esc = (s: string) => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
// OAuth client compatibility (intake b2f0943b): Cursor/Claude/VSCode custom schemes.
const CUSTOM_SCHEMES = new Set(['cursor:', 'claude:', 'vscode:']);
// DCR clients do not expire while in use: long TTL + sliding renewal on authorize/token.
const CLIENT_TTL_MS = 10 * 365 * 86400000;
function redirectUri(value: unknown): value is string {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const u = new URL(value);
    if (u.username || u.password || u.hash) return false;
    if (u.protocol === 'https:') return true;
    if (u.protocol === 'http:' && ['127.0.0.1', '[::1]', 'localhost'].includes(u.hostname)) return true;
    if (CUSTOM_SCHEMES.has(u.protocol)) return true;
    return false;
  } catch { return false; }
}
// Unknown scopes are ignored (RFC 6749: server may grant a subset); every
// accepted scope maps to the existing admin grant.
function scopeAcceptable(scope: unknown): boolean {
  if (scope === null || scope === undefined || scope === '') return true;
  if (typeof scope !== 'string' || scope.length > 2048) return false;
  // All standard OIDC/MCP scopes (admin, openid, email, profile,
  // offline_access, claudeai, mcp) are accepted; any unknown scope tokens are
  // ignored so the flow continues with the admin grant.
  return true;
}
function normalizeResource(raw: string | null, canonical: string): string | null {
  if (!raw) return canonical;
  if (raw === canonical) return canonical;
  const origin = canonical.replace(/\/mcp$/, '');
  if (raw === origin || raw === origin + '/' || raw === canonical + '/') return canonical;
  return null;
}
export class McpOAuth {
  constructor(private sql: SqlStorage, private env: Env, private tx: <T>(fn: () => T) => T, private refresh: (r: Request, client: string, resource: string) => Promise<Response>) { sql.exec(SCHEMA); }
  acceptsResource(tokenId: string, resource: string): boolean {
    const row = this.sql.exec('SELECT g.resource,g.expires FROM mcp_grants g JOIN credentials c ON c.family_id=g.family WHERE c.id=?', tokenId).toArray()[0] as any;
    // Existing CLI credentials keep their original permissions. OAuth-issued
    // credentials additionally require their explicitly consented MCP audience.
    return !row || (row.resource === resource && row.expires > Date.now());
  }
  private allowed(sub: string, email: string) {
    if (!humanAdminAllowed(this.env, email, sub)) return false;
    if (sub.startsWith('admin-token:')) return true;
    const user = this.sql.exec('SELECT email FROM users WHERE sub=?', sub).toArray()[0] as any;
    return typeof user?.email === 'string' && user.email.toLowerCase() === email.toLowerCase();
  }
  private rate(request: Request, kind: string, limit: number) {
    const now = Date.now();
    this.sql.exec('DELETE FROM mcp_rate WHERE expires<=?', now);
    const bucket = `${kind}:${request.headers.get('CF-Connecting-IP') || 'local'}:${Math.floor(now / 3600000)}`;
    this.sql.exec('INSERT INTO mcp_rate(bucket,count,expires) VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', bucket, now + 3600000);
    return Number(this.sql.exec('SELECT count FROM mcp_rate WHERE bucket=?', bucket).one().count) <= limit;
  }
  private client(id: string) {
    const row = this.sql.exec('SELECT metadata FROM mcp_clients WHERE id=? AND expires>?', id, Date.now()).toArray()[0] as any;
    return row ? JSON.parse(row.metadata) : null;
  }
  async handle(request: Request): Promise<Response> {
    let response: Response;
    try { response = await this.route(request); }
    catch { response = json({ error: 'server_error' }, 500); }
    if (new URL(request.url).pathname !== '/oauth/authorize') {
      const headers = new Headers(response.headers);
      headers.set('Access-Control-Allow-Origin', '*');
      return new Response(response.body, { status: response.status, headers });
    }
    return response;
  }
  private async route(request: Request): Promise<Response> {
    const url = new URL(request.url), origin = url.origin, resource = origin + '/mcp';
    const path = url.pathname;
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: {
      ...NO_STORE, 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization, MCP-Protocol-Version',
    } });
    if (path.startsWith('/.well-known/')) {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      if (path === '/.well-known/oauth-protected-resource' || path === '/.well-known/oauth-protected-resource/mcp') return json({
        resource, authorization_servers: [origin], scopes_supported: ['admin'], bearer_methods_supported: ['header'], resource_name: 'Agent Hub',
      }, 200, { 'Access-Control-Allow-Origin': '*' });
      if (path === '/.well-known/oauth-authorization-server' || path === '/.well-known/openid-configuration') return json({ issuer: origin, authorization_endpoint: origin + '/oauth/authorize',
        token_endpoint: origin + '/oauth/token', registration_endpoint: origin + '/oauth/register', revocation_endpoint: origin + '/oauth/revoke',
        response_types_supported: ['code'], grant_types_supported: ['authorization_code', 'refresh_token'], token_endpoint_auth_methods_supported: ['none'],
        code_challenge_methods_supported: ['S256'], scopes_supported: ['admin'], client_id_metadata_document_supported: false,
        authorization_response_iss_parameter_supported: true,
      }, 200, { 'Access-Control-Allow-Origin': '*' });
      return json({ error: 'not_found' }, 404);
    }
    if (path === '/oauth/register' && request.method === 'POST') return this.register(request);
    if (path === '/oauth/authorize' && ['GET', 'POST'].includes(request.method)) return this.authorize(request, resource);
    if (path === '/oauth/token' && request.method === 'POST') return this.token(request, resource);
    if (path === '/oauth/revoke' && request.method === 'POST') return this.revoke(request, resource);
    return json({ error: 'not_found' }, 404);
  }
  private async register(request: Request) {
    if (!this.rate(request, 'register', 30)) return json({ error: 'rate_limited' }, 429);
    let body: any;
    try { body = JSON.parse(await boundedText(request, 8192)); } catch { return json({ error: 'invalid_client_metadata' }, 400); }
    if (!body || !Array.isArray(body.redirect_uris) || body.redirect_uris.length < 1 || body.redirect_uris.length > 8 || !body.redirect_uris.every(redirectUri) ||
        (body.token_endpoint_auth_method && body.token_endpoint_auth_method !== 'none') ||
        (body.grant_types && (!Array.isArray(body.grant_types) || body.grant_types.some((g: unknown) => !['authorization_code', 'refresh_token'].includes(g as string)))) ||
        (body.response_types && JSON.stringify(body.response_types) !== '["code"]') ||
        !scopeAcceptable(body.scope) || (body.client_name !== undefined && (typeof body.client_name !== 'string' || body.client_name.length > 120))) return json({ error: 'invalid_client_metadata' }, 400);
    this.sql.exec('DELETE FROM mcp_clients WHERE expires<=?', Date.now());
    if (Number(this.sql.exec('SELECT COUNT(*) AS n FROM mcp_clients').one().n) >= 4096) return json({ error: 'temporarily_unavailable' }, 503);
    const id = randomString(24);
    const metadata = { client_id: id, client_name: body.client_name || 'MCP client', redirect_uris: body.redirect_uris,
      token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'], scope: 'admin' };
    this.sql.exec('INSERT INTO mcp_clients VALUES(?,?,?)', id, JSON.stringify(metadata), Date.now() + CLIENT_TTL_MS);
    return json(metadata, 201, { 'Access-Control-Allow-Origin': '*' });
  }
  private touch(id: string) {
    this.sql.exec('UPDATE mcp_clients SET expires=? WHERE id=?', Date.now() + CLIENT_TTL_MS, id);
  }
  private async authorize(request: Request, resource: string) {
    const url = new URL(request.url);
    let params: URLSearchParams;
    try { params = request.method === 'POST' ? new URLSearchParams(await boundedText(request, 8192)) : url.searchParams; }
    catch { return json({ error: 'invalid_request' }, 400); }
    const id = params.get('client_id') || '', redirect = params.get('redirect_uri') || '', challenge = params.get('code_challenge') || '';
    const effectiveResource = normalizeResource(params.get('resource'), resource);
    const client = this.client(id);
    // Validate exact redirect BEFORE sign-in or any redirect back to a client.
    if (!client || !client.redirect_uris.includes(redirect) || params.get('response_type') !== 'code' ||
        params.get('code_challenge_method') !== 'S256' || !/^[A-Za-z0-9_-]{43}$/.test(challenge) ||
        !effectiveResource || !scopeAcceptable(params.get('scope')) || (params.get('state') || '').length > 2048) return json({ error: 'invalid_request' }, 400);
    this.touch(id);
    const session = await getSession(this.env, request);
    if (!session) {
      if (request.method !== 'GET') return json({ error: 'unauthorized' }, 401);
      return new Response(null, { status: 303, headers: { ...NO_STORE, Location: '/auth/login?return_to=' + encodeURIComponent(url.pathname + url.search) } });
    }
    if (!this.allowed(session.sub, session.email)) return json({ error: 'access_denied' }, 403);
    if (request.method === 'GET') {
      const fields = ['client_id', 'redirect_uri', 'response_type', 'code_challenge', 'code_challenge_method', 'resource', 'scope', 'state'];
      const hidden = fields.map(k => `<input type="hidden" name="${k}" value="${esc(k === 'resource' ? (effectiveResource || '') : (params.get(k) || ''))}">`).join('');
      // Same dakit auth shell as device pairing / token sign-in (not plain HTML).
      const html = mcpConnectPage({
        email: session.email,
        clientName: client.client_name,
        redirectOrigin: new URL(redirect).origin,
        hiddenFields: hidden,
        csrfToken: session.csrfToken,
      });
      return new Response(html, { headers: { ...NO_STORE, ...mcpConsentHeaders(redirect) } });
    }
    // CSRF: require the consent form token. Origin may be missing (some same-site
    // POSTs) or the literal "null" (Chromium/Safari when Referrer-Policy was
    // no-referrer, and some Cursor/mobile WebViews). Reject only a real
    // cross-site Origin that does not match this request's public origin.
    const originHeader = request.headers.get('Origin');
    const originOk = !originHeader || originHeader === 'null' || originHeader === url.origin;
    if (!originOk || params.get('csrf') !== session.csrfToken) {
      return json({ error: 'invalid_csrf' }, 403);
    }
    const target = new URL(redirect);
    // RFC 9207: issuer identification for mix-up protection (ChatGPT uses this).
    target.searchParams.set('iss', url.origin);
    if (params.has('state')) target.searchParams.set('state', params.get('state')!);
    if (params.get('decision') !== 'allow') target.searchParams.set('error', 'access_denied');
    else {
      if (!this.rate(request, 'authorize', 120)) return json({ error: 'rate_limited' }, 429);
      const code = randomString(32), hash = await sha256(code);
      // Identity and client are rechecked after crypto awaits, before durable issuance.
      const current = await getSession(this.env, request);
      if (!current || !this.allowed(current.sub, current.email) || !this.client(id)) return json({ error: 'access_denied' }, 403);
      this.sql.exec('DELETE FROM mcp_codes WHERE expires<=?', Date.now());
      this.sql.exec('INSERT INTO mcp_codes VALUES(?,?,?,?,?,?,?,?)', hash, id, redirect, challenge, effectiveResource, current.sub, current.email, Date.now() + 60000);
      this.touch(id);
      target.searchParams.set('code', code);
    }
    return new Response(null, { status: 303, headers: { ...NO_STORE, Location: target.toString() } });
  }
  private async form(request: Request) {
    if (!request.headers.get('Content-Type')?.startsWith('application/x-www-form-urlencoded')) throw new Error('invalid_request');
    const body = new URLSearchParams(await boundedText(request, 8192));
    if ([...body.keys()].some(k => body.getAll(k).length !== 1)) throw new Error('invalid_request');
    return body;
  }
  private async token(request: Request, resource: string) {
    if (!this.rate(request, 'token', 300)) return json({ error: 'rate_limited' }, 429);
    let p: URLSearchParams;
    try { p = await this.form(request); } catch { return json({ error: 'invalid_request' }, 400); }
    const client = p.get('client_id') || '';
    const effectiveResource = normalizeResource(p.get('resource'), resource);
    if (!this.client(client) || !effectiveResource) return json({ error: 'invalid_target' }, 400);
    this.touch(client);
    if (p.get('grant_type') === 'refresh_token') {
      const hash = await sha256(p.get('refresh_token') || '');
      const cred = this.sql.exec('SELECT * FROM credentials WHERE refresh_hash=?', hash).toArray()[0] as any;
      const grant = cred && this.sql.exec('SELECT * FROM mcp_grants WHERE family=? AND client=? AND resource=? AND expires>?', cred.family_id, client, effectiveResource, Date.now()).toArray()[0];
      if (!grant) return json({ error: 'invalid_grant' }, 400);
      // Existing Hub rotation checks current allowlist, absolute lifetime and replay.
      const result = await this.refresh(new Request(new URL('/api/token/refresh', request.url), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: p.get('refresh_token') }) }), client, effectiveResource);
      return new Response(result.body, { status: result.status, headers: { ...NO_STORE, 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
    }
    if (p.get('grant_type') !== 'authorization_code') return json({ error: 'unsupported_grant_type' }, 400);
    const verifier = p.get('code_verifier') || '';
    if (!/^[A-Za-z0-9._~-]{43,128}$/.test(verifier)) return json({ error: 'invalid_grant' }, 400);
    const hash = await sha256(p.get('code') || ''), challenge = await sha256(verifier);
    const tokens = await generateTokenPair();
    const outcome = this.tx(() => {
      const code = this.sql.exec('SELECT * FROM mcp_codes WHERE hash=?', hash).toArray()[0] as any;
      if (!code || code.expires <= Date.now() || code.client !== client || code.redirect !== p.get('redirect_uri') || code.resource !== effectiveResource || code.challenge !== challenge || !this.allowed(code.sub, code.email) || !this.client(client)) return false;
      const now = new Date().toISOString(), expiry = new Date(Date.now() + 30 * 86400000).toISOString(), family = randomString(16);
      this.sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,
        randomString(16), 'human', tokens.accessHash, tokens.refreshHash, family, code.email, null, 'human:' + code.sub, '["admin"]', code.sub, now, expiry);
      this.sql.exec('INSERT INTO mcp_grants VALUES(?,?,?,?)', family, client, effectiveResource, Date.parse(expiry));
      this.sql.exec('DELETE FROM mcp_codes WHERE hash=?', hash);
      return true;
    });
    if (!outcome) return json({ error: 'invalid_grant' }, 400);
    return json({ access_token: tokens.accessToken, refresh_token: tokens.refreshToken, token_type: 'Bearer', expires_in: 30 * 86400, scope: 'admin' }, 200, { 'Access-Control-Allow-Origin': '*' });
  }
  private async revoke(request: Request, resource: string) {
    let p: URLSearchParams;
    try { p = await this.form(request); } catch { return json({ error: 'invalid_request' }, 400); }
    const effectiveResource = normalizeResource(p.get('resource'), resource) || resource;
    const hash = await sha256(p.get('token') || '');
    const cred = this.sql.exec('SELECT family_id FROM credentials WHERE token_hash=? OR refresh_hash=?', hash, hash).toArray()[0] as any;
    if (cred) this.tx(() => {
      if (this.sql.exec('SELECT family FROM mcp_grants WHERE family=? AND client=? AND resource=?', cred.family_id, p.get('client_id') || '', effectiveResource).toArray().length)
        this.sql.exec('UPDATE credentials SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL', new Date().toISOString(), cred.family_id);
    });
    return json({}, 200, { 'Access-Control-Allow-Origin': '*' });
  }
}
