import { DurableObject } from "cloudflare:workers";
import { Env } from './env';
import { SCHEMA_SQL } from './schema';
import { JwksCache, validateIdToken, exchangeCode } from './auth/dtc';
import { createSessionCookie, getSession, clearSessionCookie, createLoginCookie, getLoginCookie, clearLoginCookie, verifyCsrf, configuredAdmins, validSessionSecret } from './auth/session';
import { randomString, sha256, generateUserCode } from './auth/crypto';
import { generateTokenPair, verifyBearerScope } from './auth/token';
import { handleRegistryRoutes, handleWebSocketMessageRegistry, handleWebSocketCloseRegistry, updateHeartbeatMemoryOnly, SafeSqlStorage } from './registry';

async function safeJson(req: Request): Promise<Record<string, any> | null> {
  if (!req.body) return null;
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 32768) { await reader.cancel(); return null; }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const data = JSON.parse(new TextDecoder().decode(bytes));
    return data && typeof data === 'object' && !Array.isArray(data) ? data : null;
  } catch { return null; }
  finally { reader.releaseLock(); }
}

function htmlEscape(str: string): string {
  return String(str).replace(/[&<>'"]/g, 
    tag => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    }[tag] || tag)
  );
}

export class Hub extends DurableObject {
  sql: SafeSqlStorage;
  env: Env;
  jwksCache: JwksCache = new JwksCache();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.env = env;
    this.sql = ctx.storage.sql as unknown as SafeSqlStorage;
    this.initDb();
    
    // Restore hibernated websockets
    const websockets = this.ctx.getWebSockets();
    for (const ws of websockets) {
      try {
        const attachment: any = ws.deserializeAttachment();
        if (attachment && attachment.machineId) {
          updateHeartbeatMemoryOnly(attachment.machineId);
        }
      } catch (e) { }
    }
  }

  private initDb() {
    for (const statement of SCHEMA_SQL.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) {
        this.sql.exec(trimmed);
      }
    }
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    
    try {
      const registryResponse = await handleRegistryRoutes(request, path, this.sql, this.ctx, this.env);
      if (registryResponse) return registryResponse;
      if (path === '/api/healthz') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        return Response.json({ status: 'ok', component: 'hub-do' });
      }

      if (path === '/auth/login') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        if (!validSessionSecret(this.env) || configuredAdmins(this.env).length === 0) return Response.json({ error: 'authentication_not_configured' }, { status: 503 });
        const state = randomString(32);
        const nonce = randomString(32);
        const verifier = randomString(43);
        const codeChallenge = await sha256(verifier);
        this.sql.exec('DELETE FROM login_states WHERE expires_at <= ?', Math.floor(Date.now() / 1000));
        this.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', await sha256(state), Math.floor(Date.now() / 1000) + 600);
        
        const returnTo = url.searchParams.get('return_to');
        const returnToPath = returnTo && returnTo.startsWith('/device?code=') ? returnTo : undefined;
        
        const loginCookie = await createLoginCookie(this.env, {
          state, nonce, verifier, exp: Math.floor(Date.now() / 1000) + 600, returnTo: returnToPath
        });

        const authUrl = new URL(`${this.env.AUTH_BASE_URL}/oauth2/authorize`);
        authUrl.search = new URLSearchParams({
          client_id: this.env.AUTH_CLIENT_ID,
          redirect_uri: this.env.AUTH_CALLBACK_URL,
          response_type: 'code',
          scope: 'openid email profile',
          state,
          nonce,
          code_challenge: codeChallenge,
          code_challenge_method: 'S256',
          identity_provider: 'Google'
        }).toString();

        return new Response(null, {
          status: 302,
          headers: {
            'Location': authUrl.toString(),
            'Set-Cookie': loginCookie
          }
        });
      }

      if (path === '/auth/callback') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const query = url.searchParams;
        const code = query.get('code');
        const state = query.get('state');
        if (!code || !state) return Response.json({ error: 'Missing code or state' }, { status: 400 });

        const loginData = await getLoginCookie(this.env, request);
        if (!loginData || loginData.state !== state) {
          return Response.json({ error: 'Invalid state' }, { status: 400 });
        }

        const stateHash = await sha256(state);
        const consume = this.sql.exec('UPDATE login_states SET consumed = 1 WHERE state_hash = ? AND consumed = 0 AND expires_at > ?', stateHash, Math.floor(Date.now() / 1000));
        if (consume.rowsWritten !== 1) return Response.json({ error: 'invalid_state' }, { status: 400, headers: { 'Set-Cookie': clearLoginCookie() } });
        let claims: { sub: string; email: string; email_verified: boolean };
        try {
          const idToken = await exchangeCode(this.env, code, loginData.verifier);
          claims = await validateIdToken(this.env, idToken, loginData.nonce, this.jwksCache);
        } catch {
          return Response.json({ error: 'invalid_identity_token' }, { status: 400, headers: { 'Set-Cookie': clearLoginCookie() } });
        }
        if (!configuredAdmins(this.env).includes(claims.email.toLowerCase())) {
          return Response.json({ error: 'forbidden' }, { status: 403, headers: { 'Set-Cookie': clearLoginCookie() } });
        }

        const now = new Date().toISOString();
        const existingUser = this.sql.exec('SELECT * FROM users WHERE sub = ?', claims.sub).toArray()[0];
        if (!existingUser) {
          this.sql.exec('INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)',
            claims.sub, claims.email, 1, now, now);
        } else {
          this.sql.exec('UPDATE users SET last_seen_at = ? WHERE sub = ?', now, claims.sub);
        }

        const csrfToken = randomString(32);
        const sessionCookie = await createSessionCookie(this.env, {
          sub: claims.sub,
          email: claims.email,
          isAdmin: true,
          csrfToken,
          exp: Math.floor(Date.now() / 1000) + 12 * 3600
        });

        const headers = new Headers();
        headers.append('Set-Cookie', sessionCookie);
        headers.append('Set-Cookie', clearLoginCookie());
        headers.append('Location', loginData.returnTo || '/');

        return new Response(null, { status: 302, headers });
      }

      if (path === '/auth/me') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ authenticated: false }, { status: 401 });
        return Response.json({ authenticated: true, email: session.email, csrf_token: session.csrfToken });
      }

      if (path === '/auth/logout') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const session = await getSession(this.env, request);
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });
        const headers = new Headers();
        headers.append('Set-Cookie', clearSessionCookie());
        const logoutUrl = new URL(`${this.env.AUTH_BASE_URL}/logout`);
        logoutUrl.search = new URLSearchParams({
          client_id: this.env.AUTH_CLIENT_ID,
          logout_uri: url.origin + '/'
        }).toString();
        headers.append('Location', logoutUrl.toString());
        return new Response(null, { status: 302, headers });
      }

      if (path === '/api/device/code') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });
        if (body.client_kind !== 'host' && body.client_kind !== 'agent') {
          return Response.json({ error: 'invalid_client_kind' }, { status: 400 });
        }
        if (!body.name || typeof body.name !== 'string' || !Array.isArray(body.requested_scopes) || body.requested_scopes.length === 0) {
          return Response.json({ error: 'Invalid payload' }, { status: 400 });
        }
        
        const allowedScopes = ['host', 'bus:send', 'bus:read', 'health:report', 'jobs:create', 'lease:root'];
        for (const scope of body.requested_scopes) {
          if (!allowedScopes.includes(scope)) {
            return Response.json({ error: 'invalid_scope' }, { status: 400 });
          }
        }
        
        if (body.requested_scopes.includes('lease:root') || body.requested_scopes.includes('jobs:create') || (body.client_kind === 'agent' && body.requested_scopes.includes('host'))) return Response.json({ error: 'invalid_scope' }, { status: 400 });
        
        if (body.client_kind === 'agent') {
          const proposedActor = body.actor ?? body.name;
          if (typeof proposedActor !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(proposedActor)) {
            return Response.json({ error: 'invalid_request', error_description: 'Agent requires an actor' }, { status: 400 });
          }
          if (proposedActor === 'root' || proposedActor === 'principal' || proposedActor.startsWith('role:')) {
            return Response.json({ error: 'invalid_request', error_description: 'Privileged actor names not allowed' }, { status: 400 });
          }
        }

        if (body.name.length > 128 || !body.name.trim() || (body.hostname !== undefined && (typeof body.hostname !== 'string' || body.hostname.length > 255)) || body.requested_scopes.length > 6 || new Set(body.requested_scopes).size !== body.requested_scopes.length) return Response.json({ error: 'invalid_request' }, { status: 400 });
        const hostname = typeof body.hostname === 'string' ? body.hostname : '';
        const actor = body.client_kind === 'agent' ? (body.actor ?? body.name) : null;

        const deviceCode = randomString(32);
        const deviceCodeHash = await sha256(deviceCode);
        const userCode = generateUserCode();
        
        let ipCountry: string | null = null;
        const cfCountry = (request as any).cf?.country;
        if (typeof cfCountry === 'string' && /^[A-Z]{2}$/.test(cfCountry)) {
          ipCountry = cfCountry;
        } else if (cfCountry !== undefined && cfCountry !== null) {
          ipCountry = 'Unknown';
        }

        const now = new Date();
        const expiresAt = new Date(now.getTime() + 600 * 1000);
        
        const pastForFirstPoll = new Date(now.getTime() - 60000);
        this.sql.exec(`
          INSERT INTO device_pairings
          (device_code_hash, user_code, client_kind, name, hostname, ip_country, actor, requested_scopes, status, interval_s, last_poll_at, created_at, expires_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `, deviceCodeHash, userCode, body.client_kind, body.name, hostname, ipCountry, actor, JSON.stringify(body.requested_scopes), 'pending', 5, pastForFirstPoll.toISOString(), now.toISOString(), expiresAt.toISOString());

        return Response.json({
          device_code: deviceCode,
          user_code: userCode,
          verification_uri: url.origin + '/device',
          verification_uri_complete: url.origin + '/device?code=' + encodeURIComponent(userCode),
          expires_in: 600,
          interval: 5
        });
      }

      if (path === '/api/device/token') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body || body.grant_type !== 'urn:ietf:params:oauth:grant-type:device_code' || typeof body.device_code !== 'string' || !body.device_code || body.device_code.length > 256) {
          return Response.json({ error: 'unsupported_grant_type' }, { status: 400 });
        }

        const deviceCodeHash = await sha256(body.device_code);
        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE device_code_hash = ?', deviceCodeHash).toArray()[0] as any;
        if (!pairing) return Response.json({ error: 'invalid_grant' }, { status: 400 });

        const now = new Date();
        const expiresAt = new Date(pairing.expires_at);
        if (now >= expiresAt && pairing.status !== 'consumed' && pairing.status !== 'denied') {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE device_code_hash = ?', 'expired', deviceCodeHash);
          return Response.json({ error: 'expired_token' }, { status: 400 });
        }

        if (pairing.status === 'pending') {
          const lastPollAt = new Date(pairing.last_poll_at);
          const diffS = (now.getTime() - lastPollAt.getTime()) / 1000;
          if (diffS < pairing.interval_s) {
            const newInterval = pairing.interval_s + 5;
            this.sql.exec('UPDATE device_pairings SET interval_s = ?, last_poll_at = ? WHERE device_code_hash = ?', newInterval, now.toISOString(), deviceCodeHash);
            return Response.json({ error: 'slow_down' }, { status: 400 });
          }
          this.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE device_code_hash = ?', now.toISOString(), deviceCodeHash);
          return Response.json({ error: 'authorization_pending' }, { status: 400 });
        }
        
        if (pairing.status === 'denied') return Response.json({ error: 'access_denied' }, { status: 400 });
        if (pairing.status === 'expired' || pairing.status === 'consumed') return Response.json({ error: 'invalid_grant' }, { status: 400 });

        if (pairing.status === 'approved') {
          const tokens = await generateTokenPair();
          const familyId = randomString(16);
          const credId = randomString(16);
          const result = this.ctx.storage.transactionSync(() => {
            const current = this.sql.exec('SELECT * FROM device_pairings WHERE device_code_hash = ?', deviceCodeHash).toArray()[0] as any;
            if (!current || current.status !== 'approved') return null;
            if (Date.parse(current.expires_at) <= Date.now()) {
              this.sql.exec("UPDATE device_pairings SET status = 'expired' WHERE device_code_hash = ?", deviceCodeHash);
              return null;
            }
            const expires = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
            this.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, credId, current.client_kind, tokens.accessHash, tokens.refreshHash, familyId, current.name, current.machine_id, current.actor, current.granted_scopes, current.approved_by_sub, new Date().toISOString(), expires);
            this.sql.exec("UPDATE device_pairings SET status = 'consumed' WHERE device_code_hash = ?", deviceCodeHash);
            if (current.machine_id) this.sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', credId, current.machine_id);
            return { expires, scopes: JSON.parse(current.granted_scopes) as string[] };
          });
          if (!result) return Response.json({ error: 'invalid_grant' }, { status: 400 });
          return Response.json({ access_token: tokens.accessToken, refresh_token: tokens.refreshToken, token_type: 'Bearer', expires_in: Math.floor((Date.parse(result.expires) - Date.now()) / 1000), scope: result.scopes.join(' ') });
        }
      }

      if (path === '/device') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const userCode = url.searchParams.get('code');
        const session = await getSession(this.env, request);
        if (!session) {
          const returnTo = userCode ? `?return_to=${encodeURIComponent('/device?code=' + userCode)}` : '';
          return new Response(null, { status: 302, headers: { 'Location': `/auth/login${returnTo}` } });
        }

        if (!userCode) return new Response('<!doctype html><html><head><title>Pair a device</title></head><body><h1>Pair a device</h1><form method=GET action=/device><label>Device code <input name=code required maxlength=9 autocomplete=off></label><button>Continue</button></form></body></html>', { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode.toUpperCase()).toArray()[0] as any;
        if (!pairing) {
          return new Response('Not found or expired', { status: 404 });
        }

        const safeKind = htmlEscape(pairing.client_kind);
        const safeName = htmlEscape(pairing.name);
        const safeHostname = htmlEscape(pairing.hostname);
        const safeCountry = pairing.ip_country ? htmlEscape(pairing.ip_country) : 'Unknown';
        const safeActor = pairing.actor ? htmlEscape(pairing.actor) : '';
        const safeScopes = htmlEscape(pairing.requested_scopes);
        const safeUserCode = htmlEscape(userCode);
        const safeCsrf = htmlEscape(session.csrfToken);

        const html = `<!DOCTYPE html>
<html>
<head><title>Approve Device</title></head>
<body>
  <h1>Approve Device</h1>
  <p>Kind: ${safeKind}</p>
  <p>Name: ${safeName}</p>
  <p>Hostname: ${safeHostname}</p>
  <p>Country: ${safeCountry}</p>
  ${safeActor ? `<p>Actor: ${safeActor}</p>` : ''}
  <p>Requested Scopes: ${safeScopes}</p>
  <form method="POST" action="/api/device/approve/${safeUserCode}" id="approveForm">
    <input type="hidden" name="csrf_token" value="${safeCsrf}">
    ${safeKind === 'host' ? `<label>Machine ID (optional): <input type="text" name="machine_id"></label><br>` : ''}
    <label>Granted Scopes: <input type="text" name="granted_scopes" value="${htmlEscape(typeof pairing.requested_scopes === 'string' ? pairing.requested_scopes : JSON.stringify(pairing.requested_scopes))}"></label><br>
    <button type="button" onclick="submitApprove()">Approve</button>
  </form>
  <form method="POST" action="/api/device/deny/${safeUserCode}" id="denyForm">
    <input type="hidden" name="csrf_token" value="${safeCsrf}">
    <button type="button" onclick="submitDeny()">Deny</button>
  </form>
  <script>
    async function submitApprove() {
      const form = document.getElementById('approveForm');
      const data = { granted_scopes: JSON.parse(form.granted_scopes.value) };
      if (form.machine_id && form.machine_id.value) data.machine_id = form.machine_id.value;
      const res = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': form.csrf_token.value },
        body: JSON.stringify(data)
      });
      if (res.ok) alert('Approved'); else alert('Error');
    }
    async function submitDeny() {
      const form = document.getElementById('denyForm');
      const res = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': form.csrf_token.value },
      });
      if (res.ok) alert('Denied'); else alert('Error');
    }
  </script>
</body>
</html>`;
        return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
      }

      if (path.startsWith('/api/device/approve') || path.startsWith('/api/device/deny') || path.startsWith('/api/device/info')) {
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ error: 'Unauthorized' }, { status: 401 });

        const parts = path.split('/');
        const action = parts[3];
        const userCode = parts[4];
        if (!userCode) return Response.json({ error: 'Missing user_code' }, { status: 400 });

        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
        if (!pairing) return Response.json({ error: 'Not found' }, { status: 404 });
        
        if (action === 'info') {
          if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
          return Response.json(pairing);
        }

        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing or invalid' }, { status: 403 });

        if (pairing.status !== 'pending') return Response.json({ error: 'Already processed' }, { status: 400 });
        
        const now = new Date();
        const expiresAt = new Date(pairing.expires_at);
        if (now >= expiresAt) {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE user_code = ?', 'expired', userCode);
          return Response.json({ error: 'Already expired' }, { status: 400 });
        }

        if (action === 'deny') {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE user_code = ?', 'denied', userCode);
          return Response.json({ status: 'denied' });
        }

        if (action === 'approve') {
          const body = await safeJson(request);
          if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });
          const granted_scopes = body.granted_scopes || JSON.parse(pairing.requested_scopes);
          const parsedGranted = granted_scopes;
          
          if (!Array.isArray(parsedGranted) || parsedGranted.length === 0) return Response.json({ error: 'invalid_scopes' }, { status: 400 });
          if (parsedGranted.includes('admin')) return Response.json({ error: 'Cannot grant admin scope through device flow' }, { status: 400 });
          
          const reqScopes = typeof pairing.requested_scopes === 'string' ? JSON.parse(pairing.requested_scopes) : pairing.requested_scopes;
          const isSubset = parsedGranted.every((s: string) => reqScopes.includes(s));
          if (!isSubset) return Response.json({ error: 'Cannot grant unrequested scopes' }, { status: 400 });

          if (body.machine_id !== undefined && (typeof body.machine_id !== 'string' || body.machine_id.length > 128 || pairing.client_kind !== 'host')) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
          return this.ctx.storage.transactionSync(() => {
            const current = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
            if (!current || current.status !== 'pending' || Date.parse(current.expires_at) <= Date.now()) return Response.json({ error: 'invalid_pairing' }, { status: 409 });
            let machineId: string | null = null;
            if (current.client_kind === 'host') {
              if (body.machine_id) {
                if (!this.sql.exec('SELECT id FROM machines WHERE id = ?', body.machine_id).toArray()[0]) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
                machineId = body.machine_id;
              } else {
                machineId = randomString(16);
                this.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', machineId, current.name, current.hostname, new Date().toISOString());
              }
            }
            this.sql.exec("UPDATE device_pairings SET status = 'approved', approved_by_sub = ?, granted_scopes = ?, machine_id = ? WHERE user_code = ?", session.sub, JSON.stringify(parsedGranted), machineId, userCode);
            return Response.json({ status: 'approved', machine_id: machineId });
          });
        }
      }

      if (path === '/api/token/refresh') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body || body.grant_type !== 'refresh_token' || typeof body.refresh_token !== 'string' || !body.refresh_token || body.refresh_token.length > 256) {
          return Response.json({ error: 'invalid_request' }, { status: 400 });
        }

        const refreshHash = await sha256(body.refresh_token);
        
        // PRE-GENERATE values before any synchronous DB interaction to prevent partial state on await failures
        const { accessToken, refreshToken, accessHash, refreshHash: newRefreshHash } = await generateTokenPair();
        const newId = randomString(16);
        const now = new Date(Date.now()).toISOString();

        let errorResponse = null;
        let scopesResponse = '';
        let expiresAtResponse = '';
        
        this.ctx.storage.transactionSync(() => {
        const cred = this.sql.exec('SELECT * FROM credentials WHERE refresh_hash = ?', refreshHash).toArray()[0] as any;
        const nowMs = Date.parse(now);
        if (!cred) {
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else if (cred.revoked_at) {
          // Replay or explicitly revoked! Revoke family.
          this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else if (!Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= nowMs) {
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else {
          const res = this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL', now, cred.id);
          if (res.rowsWritten !== 1) {
            this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
            errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
          } else {
            this.sql.exec(`
              INSERT INTO credentials
              (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            `, newId, cred.kind, accessHash, newRefreshHash, cred.family_id, cred.subject, cred.machine_id, cred.actor, cred.scopes, cred.created_by_sub, now, cred.expires_at);

            scopesResponse = JSON.parse(cred.scopes).join(' ');
            expiresAtResponse = cred.expires_at;
          }
        }

        });
        if (errorResponse) return errorResponse;

        return Response.json({
          access_token: accessToken,
          refresh_token: refreshToken,
          token_type: 'Bearer',
          expires_in: Math.max(0, Math.floor((new Date(expiresAtResponse).getTime() - new Date(now).getTime()) / 1000)),
          scope: scopesResponse
        });
      }

      if (path === '/api/token/revoke') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body || (!body.token && !body.id) || (body.token !== undefined && typeof body.token !== 'string') || (body.id !== undefined && typeof body.id !== 'string')) return Response.json({ error: 'missing_token_or_id' }, { status: 400 });

        let familyIdToRevoke = null;

        if (body.token) {
          const authHeader = request.headers.get('Authorization');
          if (!authHeader || !authHeader.startsWith('Bearer ')) return Response.json({ error: 'Unauthorized' }, { status: 401 });
          const bearerHash = await sha256(authHeader.substring(7).trim());
          const ownerCred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL', bearerHash).toArray()[0] as any;
          if (!ownerCred || !Number.isFinite(Date.parse(ownerCred.expires_at)) || Date.parse(ownerCred.expires_at) <= Date.now()) return Response.json({ error: 'Unauthorized' }, { status: 401 });

          const tokenHash = await sha256(body.token);
          const cred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? OR refresh_hash = ?', tokenHash, tokenHash).toArray()[0] as any;
          if (cred && cred.family_id === ownerCred.family_id) {
            familyIdToRevoke = cred.family_id;
          } else if (cred) {
             return Response.json({ error: 'Unauthorized' }, { status: 403 });
          }
        } else if (body.id) {
          const session = await getSession(this.env, request);
          if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });
          if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });
          
          const cred = this.sql.exec('SELECT * FROM credentials WHERE id = ?', body.id).toArray()[0] as any;
          if (cred) familyIdToRevoke = cred.family_id;
        }
        
        if (familyIdToRevoke) {
          this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', new Date().toISOString(), familyIdToRevoke);
        }

        return Response.json({ status: 'revoked' });
      }

      if (path === '/internal/tick') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        return new Response("Tick processed");
      }

      return new Response("Not found", { status: 404 });
    } catch (e: any) {
      return Response.json({ error: 'Internal Server Error' }, { status: 500 });
    }
  }

  webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    handleWebSocketMessageRegistry(ws, message, this.sql);
  }

  webSocketClose(ws: WebSocket, code: number, reason: string, wasClean: boolean) {
    handleWebSocketCloseRegistry(ws, this.sql);
  }

  webSocketError(ws: WebSocket, error: any) {
    handleWebSocketCloseRegistry(ws, this.sql);
  }
}
