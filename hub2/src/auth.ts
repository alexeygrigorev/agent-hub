import { error } from './http';
import type { Actor, Hub2Env } from './types';
export type AuthEnv = Hub2Env & {
  AUTH_BASE_URL?: string;
  AUTH_CLIENT_ID?: string;
  AUTH_CALLBACK_URL?: string;
  AUTH_ISSUER?: string;
  AUTH_JWKS_URL?: string;
  HUB_ADMINS?: string;
  HUB_ADDITIONAL_ADMINS?: string;
};
export const publicAuthPaths = new Set([
  '/device',
  '/auth/login',
  '/auth/callback',
  '/auth/session',
  '/auth/logout',
  '/api/device/code',
  '/api/device/token',
  '/api/device/approve',
  '/api/token/refresh',
  '/api/token/revoke',
]);
async function controlAuth(request: Request, env: AuthEnv, path: string, includeBody = true): Promise<Response> {
  const headers = new Headers(request.headers);
  for (const k of [...headers.keys()]) if (k.toLowerCase().startsWith('x-hub2-')) headers.delete(k);
  headers.set('x-hub2-public-origin', new URL(request.url).origin);
  return env.CONTROL.get(env.CONTROL.idFromName('global')).fetch(new Request(
    'https://control.internal/_auth/' + path,
    { method: request.method, redirect: 'manual', headers,
      body: includeBody && !['GET', 'HEAD'].includes(request.method) ? request.body : undefined },
  ));
}
export async function publicAuthentication(request: Request, env: AuthEnv): Promise<Response | undefined> {
  const url = new URL(request.url);
  if (publicAuthPaths.has(url.pathname)) return controlAuth(request, env, 'public' + url.pathname + url.search);
}
export async function cookieActor(request: Request, env: AuthEnv): Promise<Actor | undefined> {
  // An invalid bearer cannot fall back to the independently authenticated cookie.
  if (request.headers.has('Authorization')) return;
  const response = await controlAuth(request, env, 'cookie', false);
  return response.ok ? ((await response.json()) as { actor: Actor }).actor : undefined;
}
export function allowlisted(env: AuthEnv, email: string): boolean {
  return [env.HUB_ADMINS, env.HUB_ADDITIONAL_ADMINS]
    .flatMap((v) => (v || '').split(','))
    .map((v) => v.trim().toLowerCase())
    .filter(Boolean)
    .includes(email.toLowerCase());
}
export function cookieValue(r: Request, name: string): string | undefined {
  return r.headers
    .get('Cookie')
    ?.split(';')
    .map((v) => v.trim())
    .find((v) => v.startsWith(name + '='))
    ?.slice(name.length + 1);
}
export function authCookie(name: string, value: string, age: number): string {
  return `${name}=${value}; Path=/; Max-Age=${age}; Secure; HttpOnly; SameSite=Lax`;
}
export const noStore = {
  'Cache-Control': 'private, no-store',
  Pragma: 'no-cache',
};
export function authError(code: string, status = 400): Response {
  const r = error(code, status);
  return new Response(r.body, {
    status,
    headers: { ...Object.fromEntries(r.headers), ...noStore },
  });
}
export async function boundedJson(
  url: string,
  init: RequestInit = {},
  limit = 262144,
): Promise<any> {
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), 5000);
  try {
    const r = await fetch(url, {
      ...init,
      redirect: 'manual',
      signal: controller.signal,
    });
    if (!r.ok) throw new Error('provider_request_refused');
    const reader = r.body?.getReader();
    if (!reader) throw new Error('provider_empty');
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      for (;;) {
        const x = await reader.read();
        if (x.done) break;
        size += x.value.length;
        if (size > limit) throw new Error('provider_body_limit');
        chunks.push(x.value);
      }
    } finally {
      await reader.cancel();
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const c of chunks) {
      bytes.set(c, offset);
      offset += c.length;
    }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes));
  } finally {
    clearTimeout(timer);
  }
}
export function decode64(s: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(s)) throw new Error('invalid_base64');
  return Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
}
export function encode64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/=/g, '')
    .replace(/\+/g, '-')
    .replace(/\//g, '_');
}
export async function pkce(verifier: string): Promise<string> {
  return encode64(
    new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))),
  );
}
export async function identityToken(
  env: AuthEnv,
  token: string,
  nonce: string,
): Promise<{ sub: string; email: string }> {
  if (typeof token !== 'string' || token.length > 16384) throw new Error('invalid_id_token');
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('invalid_id_token');
  const header = JSON.parse(new TextDecoder().decode(decode64(parts[0]))),
    c = JSON.parse(new TextDecoder().decode(decode64(parts[1])));
  if (header.alg !== 'RS256' || typeof header.kid !== 'string')
    throw new Error('invalid_jwt_algorithm');
  if (!env.AUTH_JWKS_URL?.startsWith('https://')) throw new Error('auth_not_configured');
  const jwks = await boundedJson(env.AUTH_JWKS_URL);
  if (!Array.isArray(jwks.keys) || jwks.keys.length > 32) throw new Error('invalid_jwks');
  const key = jwks.keys.find(
    (k: any) =>
      k.kid === header.kid &&
      k.kty === 'RSA' &&
      (!k.alg || k.alg === 'RS256') &&
      (!k.use || k.use === 'sig'),
  );
  if (!key) throw new Error('unknown_signing_key');
  const imported = await crypto.subtle.importKey(
    'jwk',
    key,
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['verify'],
  );
  if (
    !(await crypto.subtle.verify(
      'RSASSA-PKCS1-v1_5',
      imported,
      decode64(parts[2]),
      new TextEncoder().encode(parts[0] + '.' + parts[1]),
    ))
  )
    throw new Error('invalid_jwt_signature');
  const aud = Array.isArray(c.aud) ? c.aud : [c.aud],
    now = Math.floor(Date.now() / 1000);
  if (
    c.iss !== env.AUTH_ISSUER ||
    !aud.includes(env.AUTH_CLIENT_ID) ||
    (aud.length > 1 && c.azp !== env.AUTH_CLIENT_ID) ||
    !aud.every((v: unknown) => typeof v === 'string')
  )
    throw new Error('invalid_jwt_audience_or_issuer');
  if (
    !Number.isSafeInteger(c.exp) ||
    c.exp <= now ||
    c.exp > now + 86400 ||
    c.nonce !== nonce ||
    c.token_use !== 'id' ||
    c.email_verified !== true ||
    typeof c.sub !== 'string' ||
    !c.sub ||
    c.sub.length > 160 ||
    typeof c.email !== 'string' ||
    !allowlisted(env, c.email)
  )
    throw new Error('identity_not_authorized');
  return { sub: c.sub, email: c.email.toLowerCase() };
}

export function verifyKnownCredential(row: any, env: AuthEnv): Actor | undefined {
  if (
    !['operator', 'agent'].includes(row.kind) ||
    typeof row.expires_at !== 'number' ||
    row.expires_at <= Date.now() ||
    row.revoked_at != null ||
    !row.family_id ||
    !row.generation
  )
    return;
  let scopes: string[];
  try {
    scopes = JSON.parse(row.scopes_json);
  } catch {
    return;
  }
  if (!Array.isArray(scopes) || scopes.length > 16 || !scopes.every((s) => typeof s === 'string'))
    return;
  const worker = row.kind === 'agent';
  if (worker) {
    if (
      !/^session:[0-9a-f-]{36}$/i.test(row.actor_id) ||
      typeof row.resource_id !== 'string' ||
      !row.resource_id ||
      row.resource_id.length > 180
    )
      return;
    const allowed = [
      'intake:read',
      'intake:append',
      'claim:read',
      'claim:write',
      'message:read',
      'message:send',
      'mailbox:' + row.actor_id,
    ];
    if (scopes.some((scope) => !allowed.includes(scope))) return;
  } else if (
    typeof row.principal_email !== 'string' ||
    !allowlisted(env, row.principal_email) ||
    typeof row.resource_id !== 'string' ||
    !row.resource_id.startsWith('human:')
  )
    return;
  return {
    id: worker ? row.actor_id : row.resource_id,
    kind: worker ? 'agent' : 'operator',
    resource_id: worker ? row.resource_id : undefined,
    scopes,
    generation: row.generation,
    family_id: row.family_id,
    client_id: row.client_id || '',
  };
}
