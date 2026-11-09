import { MeteredDb } from './db';
import { randomToken, sha256Hex } from './crypto';
import { bodyObject, boundedText, hasScope, json } from './http';
import {
  allowlisted,
  authCookie,
  authError,
  boundedJson,
  cookieValue,
  identityToken,
  noStore,
  pkce,
  verifyKnownCredential,
  type AuthEnv,
} from './auth';
import type { Actor } from './types';
export const CONTROL_AUTH_SCHEMA = [
  'CREATE TABLE auth_login_states(hash TEXT PRIMARY KEY,browser_hash TEXT NOT NULL,nonce TEXT NOT NULL,verifier TEXT NOT NULL,redirect TEXT NOT NULL,return_to TEXT NOT NULL,expires_at INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID',
  'CREATE INDEX auth_login_expiry ON auth_login_states(expires_at,hash)',
  'CREATE TABLE auth_devices(hash TEXT PRIMARY KEY,user_hash TEXT NOT NULL UNIQUE,client_id TEXT NOT NULL,expires_at INTEGER NOT NULL,next_poll INTEGER NOT NULL,state TEXT NOT NULL,principal_id TEXT,principal_email TEXT) WITHOUT ROWID',
  'CREATE INDEX auth_device_expiry ON auth_devices(expires_at,hash)',
  'CREATE TABLE auth_rate(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL,expires_at INTEGER NOT NULL) WITHOUT ROWID',
  'CREATE INDEX auth_rate_expiry ON auth_rate(expires_at,bucket)',
];
type Credential = Record<string, any>;
export class ControlAuth {
  private lastCleanup = 0;
  constructor(
    private ctx: DurableObjectState,
    private db: MeteredDb,
    private env: AuthEnv,
  ) {}
  private origin(r: Request): string {
    return r.headers.get('x-hub2-public-origin') || '';
  }
  private sameOrigin(r: Request): boolean {
    const origin = r.headers.get('Origin'),
      site = r.headers.get('Sec-Fetch-Site');
    return (!origin || origin === this.origin(r)) && (!site || site === 'same-origin');
  }
  private async cookie(
    r: Request,
    mutation = true,
  ): Promise<{ row: Credential; actor: Actor } | undefined> {
    const token = cookieValue(r, 'hub2_session');
    if (!token || token.length > 256) return;
    const row = this.db.one<Credential>(
      'SELECT * FROM credentials WHERE token_hash=?',
      await sha256Hex(token),
    );
    if (
      !row ||
      row.kind !== 'operator' ||
      row.client_id !== 'browser' ||
      typeof row.csrf_token !== 'string'
    )
      return;
    const actor = verifyKnownCredential(row, this.env);
    if (!actor) return;
    if (
      mutation &&
      !['GET', 'HEAD'].includes(r.method) &&
      (!this.sameOrigin(r) || r.headers.get('x-csrf-token') !== row.csrf_token)
    )
      return;
    return { row, actor };
  }
  private async rate(r: Request, kind: string, limit = 60): Promise<boolean> {
    const now = Date.now(),
      bucket =
        kind +
        ':' +
        (await sha256Hex(r.headers.get('CF-Connecting-IP') || 'local')) +
        ':' +
        Math.floor(now / 3600000);
    return this.db.transaction(this.ctx, () => {
      this.db.run(
        'INSERT INTO auth_rate VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1',
        bucket,
        now + 3600000,
      );
      return (
        (this.db.one<{ count: number }>('SELECT count FROM auth_rate WHERE bucket=?', bucket)
          ?.count || 0) <= limit
      );
    });
  }
  private cleanup(): void {
    const now = Date.now();
    if (now - this.lastCleanup < 60000) return;
    // Each expired transport table is indexed and each purge is capped. There is
    // no periodic alarm: enabled authentication requests pay their actual cost.
    this.db.transaction(this.ctx, () => {
      for (const [table, key] of [
        ['auth_login_states', 'hash'],
        ['auth_devices', 'hash'],
        ['auth_rate', 'bucket'],
      ]) {
        const rows = this.db.all<Record<string, string>>(
          `SELECT ${key} FROM ${table} WHERE expires_at<=? ORDER BY expires_at,${key} LIMIT 25`,
          now,
        );
        for (const row of rows) this.db.run(`DELETE FROM ${table} WHERE ${key}=?`, row[key]);
      }
    });
    this.lastCleanup = now;
  }
  async fetch(r: Request): Promise<Response | undefined> {
    const path = new URL(r.url).pathname;
    if (!path.startsWith('/_auth/')) return;
    try {
      if (path === '/_auth/me' && r.method === 'GET') {
        // Resolve the credential itself so profile data never comes from caller
        // headers or a stale actor projection. A supplied bearer never falls
        // back to a browser cookie.
        let auth: { row: Credential; actor: Actor } | undefined;
        if (r.headers.has('Authorization')) {
          const token = r.headers.get('Authorization')?.match(/^Bearer ([^\s]{32,256})$/)?.[1];
          const row = token && this.db.one<Credential>(
            'SELECT * FROM credentials WHERE token_hash=?', await sha256Hex(token),
          );
          const actor = row && verifyKnownCredential(row, this.env);
          if (row && actor) auth = { row, actor };
        } else auth = await this.cookie(r);
        if (!auth) return authError('invalid_credentials', 401);
        if (auth.actor.kind !== 'operator') return authError('human_required', 403);
        return json({
          actor: auth.actor.id,
          kind: 'human',
          sub: auth.actor.id.slice('human:'.length),
          email: auth.row.principal_email,
          label: auth.actor.client_id,
          scopes: auth.actor.scopes,
          isAdmin: hasScope(auth.actor, 'operator:admin'),
        }, 200, noStore);
      }
      if (path === '/_auth/cookie') {
        const auth = await this.cookie(r);
        return auth ? json({ actor: auth.actor }) : authError('invalid_session', 401);
      }
      if (path === '/_auth/authorize' && r.method === 'POST') {
        const b = await bodyObject(r, 4096);
        if (typeof b.actor_id !== 'string' || typeof b.generation !== 'string')
          return authError('invalid_authority', 401);
        const row = this.db.one<Credential>(
          'SELECT * FROM credentials WHERE resource_id=? AND generation=? AND revoked_at IS NULL',
          b.actor_id,
          b.generation,
        );
        const actor = row && verifyKnownCredential(row, this.env);
        if (!actor) return authError('credential_revoked', 401);
        if (
          b.scope !== undefined &&
          (typeof b.scope !== 'string' || b.scope.length > 100 || !hasScope(actor, b.scope))
        )
          return authError('forbidden', 403);
        return json({ current: true, actor });
      }
      const publicPath = path.slice('/_auth/public'.length);
      if (!path.startsWith('/_auth/public/')) return authError('auth_route_not_found', 404);
      this.cleanup();
      if (!['GET', 'POST'].includes(r.method)) return authError('method_not_allowed', 405);
      if (r.method === 'POST' && !this.sameOrigin(r)) return authError('csrf_refused', 403);
      if (publicPath === '/device' && r.method === 'GET') return await this.devicePage(r);
      if (publicPath === '/auth/login' && r.method === 'GET') return await this.login(r);
      if (publicPath === '/auth/callback' && r.method === 'GET') return await this.callback(r);
      if (publicPath === '/auth/session' && r.method === 'GET') {
        const auth = await this.cookie(r);
        return auth
          ? json({ actor: auth.actor, csrf_token: auth.row.csrf_token }, 200, noStore)
          : authError('unauthorized', 401);
      }
      if (publicPath === '/auth/logout' && r.method === 'POST') {
        const auth = await this.cookie(r);
        if (!auth) return authError('csrf_or_session_refused', 403);
        this.db.run(
          'UPDATE credentials SET revoked_at=? WHERE actor_id=?',
          Date.now(),
          auth.row.actor_id,
        );
        return json({ ok: true }, 200, {
          ...noStore,
          'Set-Cookie': authCookie('hub2_session', '', 0),
        });
      }
      if (publicPath === '/api/device/code' && r.method === 'POST') return await this.deviceCode(r);
      if (publicPath === '/api/device/approve' && r.method === 'POST')
        return await this.deviceApprove(r);
      if (publicPath === '/api/device/token' && r.method === 'POST')
        return await this.deviceToken(r);
      if (publicPath === '/api/token/refresh' && r.method === 'POST') return await this.refresh(r);
      if (publicPath === '/api/token/revoke' && r.method === 'POST') return await this.revoke(r);
      return authError('auth_route_not_found', 404);
    } catch {
      return authError('authentication_failed', 400);
    }
  }
  private async login(r: Request): Promise<Response> {
    if (
      !this.env.AUTH_BASE_URL?.startsWith('https://') ||
      !this.env.AUTH_CALLBACK_URL?.startsWith('https://') ||
      !this.env.AUTH_CLIENT_ID ||
      !this.env.AUTH_ISSUER ||
      !this.env.AUTH_JWKS_URL
    )
      return authError('authentication_not_configured', 503);
    if (new URL(this.env.AUTH_CALLBACK_URL).origin !== this.origin(r))
      return authError('unregistered_callback_origin', 403);
    if (!(await this.rate(r, 'login'))) return authError('rate_limited', 429);
    const state = randomToken(),
      browser = randomToken(),
      nonce = randomToken(),
      verifier = randomToken(),
      now = Date.now(),
      returnTo = new URL(r.url).searchParams.get('return_to') || '/';
    if (!returnTo.startsWith('/') || returnTo.length > 2048) return authError('invalid_return_to');
    let decoded = returnTo;
    for (let i=0;i<3;i++) {
      if (decoded.startsWith('//') || /[\u0000-\u001f\u007f\\]/.test(decoded)) return authError('invalid_return_to');
      const next = decodeURIComponent(decoded); if (next === decoded) break; decoded = next;
    }
    const destination = new URL(returnTo,this.origin(r));
    if (destination.origin !== this.origin(r)) return authError('invalid_return_to');
    const safeReturn = destination.pathname+destination.search+destination.hash;
    this.db.run(
      'INSERT INTO auth_login_states(hash,browser_hash,nonce,verifier,redirect,return_to,expires_at) VALUES(?,?,?,?,?,?,?)',
      await sha256Hex(state),
      await sha256Hex(browser),
      nonce,
      verifier,
      this.env.AUTH_CALLBACK_URL,
      safeReturn,
      now + 600000,
    );
    const url = new URL(this.env.AUTH_BASE_URL + '/oauth2/authorize');
    url.search = new URLSearchParams({
      client_id: this.env.AUTH_CLIENT_ID,
      redirect_uri: this.env.AUTH_CALLBACK_URL,
      response_type: 'code',
      scope: 'openid email profile',
      state,
      nonce,
      code_challenge: await pkce(verifier),
      code_challenge_method: 'S256',
      identity_provider: 'Google',
      prompt: 'select_account',
    }).toString();
    return new Response(null, {
      status: 302,
      headers: {
        ...noStore,
        Location: url.toString(),
        'Set-Cookie': authCookie('hub2_login', browser, 600),
      },
    });
  }
  private async callback(r: Request): Promise<Response> {
    const url = new URL(r.url),
      state = url.searchParams.get('state'),
      code = url.searchParams.get('code'),
      browser = cookieValue(r, 'hub2_login');
    if (
      !state ||
      state.length > 256 ||
      !code ||
      code.length > 4096 ||
      !browser ||
      new URL(this.env.AUTH_CALLBACK_URL || 'https://invalid').origin !== this.origin(r)
    )
      return authError('invalid_login_state');
    const hash = await sha256Hex(state),
      browserHash = await sha256Hex(browser);
    const row = this.db.transaction(this.ctx, () => {
      const x = this.db.one<any>('SELECT * FROM auth_login_states WHERE hash=?', hash);
      if (!x || x.consumed || x.expires_at <= Date.now() || x.browser_hash !== browserHash) return;
      this.db.run('UPDATE auth_login_states SET consumed=1 WHERE hash=? AND consumed=0', hash);
      return x;
    });
    if (!row) return authError('login_state_expired_or_consumed');
    const tokens = await boundedJson(
      this.env.AUTH_BASE_URL + '/oauth2/token',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          client_id: this.env.AUTH_CLIENT_ID!,
          code,
          redirect_uri: row.redirect,
          code_verifier: row.verifier,
        }).toString(),
      },
      32768,
    );
    const identity = await identityToken(this.env, tokens.id_token, row.nonce),
      issued = await this.preparedIssue('human:' + identity.sub, identity.email, 'browser', true);
    this.db.transaction(this.ctx, issued.persist);
    const headers = new Headers({ ...noStore, Location: row.return_to });
    headers.append('Set-Cookie', authCookie('hub2_session', issued.tokens.access_token, 43200));
    headers.append('Set-Cookie', authCookie('hub2_login', '', 0));
    return new Response(null, { status: 303, headers });
  }
  private async deviceCode(r: Request): Promise<Response> {
    if (!(await this.rate(r, 'device', 30))) return authError('rate_limited', 429);
    const b = await bodyObject(r, 4096),
      client = typeof b.client_id === 'string' ? b.client_id : 'hub-cli';
    if (!/^[A-Za-z0-9_.:-]{1,100}$/.test(client)) return authError('invalid_client');
    const code = randomToken(),
      user = randomToken()
        .replace(/[^A-Za-z0-9]/g, '')
        .slice(0, 10)
        .toUpperCase(),
      now = Date.now();
    this.db.run(
      'INSERT INTO auth_devices VALUES(?,?,?,?,?,?,NULL,NULL)',
      await sha256Hex(code),
      await sha256Hex(user),
      client,
      now + 600000,
      now,
      'pending',
    );
    return json(
      {
        device_code: code,
        user_code: user,
        verification_uri: this.origin(r) + '/device',
        verification_uri_complete: this.origin(r) + '/device?user_code=' + encodeURIComponent(user),
        expires_in: 600,
        interval: 5,
      },
      200,
      noStore,
    );
  }
  private escapeHtml(v: string): string {
    return v.replace(
      /[&<>"']/g,
      (c) =>
        ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        })[c]!,
    );
  }
  private deviceFormError(message: string, status: number): Response {
    return new Response(
      '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Approval failed</title><main><h1>Approval failed</h1><p>' +
        this.escapeHtml(message) +
        '</p><p>No access was granted. Run <code>hub auth login</code> for a fresh code and approve only the code shown in your terminal.</p></main></html>',
      {
        status,
        headers: {
          ...noStore,
          'Content-Type': 'text/html; charset=utf-8',
          'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
        },
      },
    );
  }
  private async devicePage(r: Request): Promise<Response> {
    const auth = await this.cookie(r);
    if (!auth)
      return new Response(null, {
        status: 303,
        headers: {
          ...noStore,
          Location:
            '/auth/login?return_to=' + encodeURIComponent('/device' + new URL(r.url).search),
        },
      });
    const escape = (v: string) =>
      v.replace(
        /[&<>"']/g,
        (c) =>
          ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
          })[c]!,
      );
    const params = new URL(r.url).searchParams;
    // Prefill from the current user_code link; fall back to the legacy code
    // parameter so older CLI output and bookmarks still populate the field.
    // An unknown or missing code stays visible as an empty field and every
    // approve path below fails loudly instead of authorizing anything.
    const code = params.get('user_code') || params.get('code') || '';
    return new Response(
      '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorize Hub CLI</title><main><h1>Authorize Hub CLI</h1><p>Signed in as ' +
        escape(auth.row.principal_email) +
        '. Approve only a code from the CLI you started.</p><form method="post" action="/api/device/approve"><input type="hidden" name="csrf_token" value="' +
        escape(auth.row.csrf_token) +
        '"><label>Device code <input name="user_code" required maxlength="40" autocomplete="off" value="' +
        escape(code) +
        '"></label><button>Approve CLI access</button></form></main></html>',
      {
        headers: {
          ...noStore,
          'Content-Type': 'text/html; charset=utf-8',
          'Content-Security-Policy':
            "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        },
      },
    );
  }
  private async deviceApprove(r: Request): Promise<Response> {
    let b: any;
    const form = r.headers.get('Content-Type')?.startsWith('application/x-www-form-urlencoded');
    if (form) b = Object.fromEntries(new URLSearchParams(await boundedText(r, 4096)));
    else b = await bodyObject(r, 4096);
    // Browser form posts must see a visible failure page, never a bare JSON
    // error and never a false "authorized" success.
    const fail = (code: string, status: number, message: string): Response => {
      if (form) return this.deviceFormError(message, status);
      return authError(code, status);
    };
    const auth = await this.cookie(r, false);
    if (
      !auth ||
      !this.sameOrigin(r) ||
      (form ? b.csrf_token : r.headers.get('x-csrf-token')) !== auth.row.csrf_token
    )
      return authError('csrf_or_session_refused', 403);
    if (b.user_code === undefined || b.user_code === null || typeof b.user_code !== 'string')
      return fail('missing_user_code', 400, 'No device code was submitted. Enter the code shown in your terminal.');
    if (b.user_code.length > 40)
      return fail('invalid_user_code', 400, 'That device code is not valid. Enter the code shown in your terminal.');
    // Pasted codes may carry surrounding whitespace; matching stays exact
    // after trimming. An empty code is rejected loudly instead of consuming
    // or authorizing anything.
    const normalized = b.user_code.trim().toUpperCase();
    if (!normalized)
      return fail('invalid_user_code', 400, 'The device code is empty. Enter the code shown in your terminal.');
    const hash = await sha256Hex(normalized),
      row = this.db.one<any>('SELECT * FROM auth_devices WHERE user_hash=?', hash);
    if (!row)
      return fail('unknown_or_expired_code', 404, 'This code was not found. It may be mistyped, expired, or already used. Run hub auth login for a new code.');
    if (row.expires_at <= Date.now() || row.state !== 'pending')
      return fail('expired_or_used_code', 409, 'This code already expired or was already used. Run hub auth login for a new code.');
    this.db.run(
      "UPDATE auth_devices SET state='approved',principal_id=?,principal_email=? WHERE user_hash=? AND state='pending'",
      auth.actor.id,
      auth.row.principal_email,
      hash,
    );
    if (form)
      return new Response(
        '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CLI authorized</title><p>CLI authorized. Return to the CLI.</p></html>',
        {
          headers: {
            ...noStore,
            'Content-Type': 'text/html; charset=utf-8',
            'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
          },
        },
      );
    return json({ approved: true }, 200, noStore);
  }
  private async deviceToken(r: Request): Promise<Response> {
    if (!(await this.rate(r, 'poll', 800))) return authError('rate_limited', 429);
    const b = await bodyObject(r, 4096);
    if (typeof b.device_code !== 'string' || b.device_code.length > 256)
      return authError('invalid_device_code');
    const hash = await sha256Hex(b.device_code),
      row = this.db.one<any>('SELECT * FROM auth_devices WHERE hash=?', hash),
      now = Date.now();
    if (!row || row.expires_at <= now || row.state === 'consumed')
      return authError('expired_token');
    if (row.next_poll > now) return authError('slow_down', 429);
    this.db.run('UPDATE auth_devices SET next_poll=? WHERE hash=?', now + 5000, hash);
    if (row.state !== 'approved') return authError('authorization_pending', 428);
    if (!allowlisted(this.env, row.principal_email))
      return authError('principal_not_authorized', 403);
    // Hashes prepared before transaction; consume and insert credential atomically below.
    const issued = await this.preparedIssue(row.principal_id, row.principal_email, row.client_id);
    const ok = this.db.transaction(this.ctx, () => {
      const current = this.db.one<any>('SELECT state FROM auth_devices WHERE hash=?', hash);
      if (current?.state !== 'approved') return false;
      this.db.run("UPDATE auth_devices SET state='consumed' WHERE hash=?", hash);
      issued.persist();
      return true;
    });
    return ok ? json(issued.tokens, 200, noStore) : authError('expired_token');
  }
  private async preparedIssue(
    principal: string,
    email: string,
    client: string,
    browser = false,
  ): Promise<{ tokens: any; persist: () => void }> {
    if (!allowlisted(this.env, email)) throw new Error('principal_not_authorized');
    const token = randomToken(),
      refresh = randomToken(),
      csrf = randomToken(),
      hash = await sha256Hex(token),
      rh = await sha256Hex(refresh),
      id = 'credential:' + crypto.randomUUID(),
      family = crypto.randomUUID(),
      generation = crypto.randomUUID(),
      now = Date.now();
    return {
      tokens: {
        access_token: token,
        refresh_token: refresh,
        token_type: 'Bearer',
        expires_in: browser ? 43200 : 3600,
        csrf_token: csrf,
        family_id: family,
      },
      persist: () => {
        if (browser) this.db.run('INSERT INTO actor_directory(id,kind,enabled) VALUES(?,?,1) ON CONFLICT(id) DO UPDATE SET enabled=1', principal, 'human');
        this.db.run(
          'INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,refresh_hash,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
          id,
          'operator',
          principal,
          generation,
          hash,
          JSON.stringify(['operator:admin']),
          family,
          client,
          now,
          now + (browser ? 43200000 : 3600000),
          email,
          rh,
          csrf,
        );
      },
    };
  }
  private async refresh(r: Request): Promise<Response> {
    if (!(await this.rate(r, 'refresh', 120))) return authError('rate_limited', 429);
    const b = await bodyObject(r, 4096);
    if (typeof b.refresh_token !== 'string' || b.refresh_token.length > 256)
      return authError('invalid_refresh');
    const oldHash = await sha256Hex(b.refresh_token),
      row = this.db.one<Credential>('SELECT * FROM credentials WHERE refresh_hash=?', oldHash);
    if (
      !row ||
      row.kind !== 'operator' ||
      row.revoked_at != null ||
      row.created_at + 30 * 86400000 <= Date.now() ||
      !allowlisted(this.env, row.principal_email)
    )
      return authError('invalid_refresh', 401);
    const token = randomToken(),
      refresh = randomToken(),
      hash = await sha256Hex(token),
      rh = await sha256Hex(refresh),
      generation = crypto.randomUUID();
    const ok = this.db.transaction(this.ctx, () => {
      const current = this.db.one<any>(
        'SELECT refresh_hash,revoked_at FROM credentials WHERE actor_id=?',
        row.actor_id,
      );
      if (current?.refresh_hash !== oldHash || current.revoked_at != null) return false;
      this.db.run(
        'UPDATE credentials SET token_hash=?,refresh_hash=?,generation=?,expires_at=? WHERE actor_id=?',
        hash,
        rh,
        generation,
        Date.now() + 3600000,
        row.actor_id,
      );
      return true;
    });
    return ok
      ? json(
          {
            access_token: token,
            refresh_token: refresh,
            token_type: 'Bearer',
            expires_in: 3600,
          },
          200,
          noStore,
        )
      : authError('refresh_replayed', 401);
  }
  private async revoke(r: Request): Promise<Response> {
    const token = r.headers.get('Authorization')?.match(/^Bearer ([^\s]{32,256})$/)?.[1];
    if (!token) return authError('invalid_credentials', 401);
    const row = this.db.one<Credential>(
      'SELECT * FROM credentials WHERE token_hash=?',
      await sha256Hex(token),
    );
    if (!row || row.kind !== 'operator' || !verifyKnownCredential(row, this.env))
      return authError('invalid_credentials', 401);
    this.db.run('UPDATE credentials SET revoked_at=? WHERE actor_id=?', Date.now(), row.actor_id);
    return json({ revoked: true }, 200, noStore);
  }
}
