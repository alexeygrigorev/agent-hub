import { Env } from '../env';
import { signHmac, verifyHmac, base64url, decodeBase64url } from './crypto';

export function configuredAdmins(env: Env): string[] {
  const split = (v: unknown) => typeof v === 'string' ? v.split(',').map(s => s.trim().toLowerCase()).filter(s => s && !s.includes('<')) : [];
  // Additive allowlist: HUB_ADMINS plus HUB_ADDITIONAL_ADMINS (e.g. Orin).
  return [...split(env.HUB_ADMINS), ...split((env as any).HUB_ADDITIONAL_ADMINS)];
}

/** Sessions created by the admin-token sign-in use this identity instead of an OIDC email. */
export const TOKEN_ADMIN_EMAIL = 'admin-token@hub.local';

/** SHA-256 hex of the admin sign-in token, or null when token sign-in is not configured. */
export function adminTokenHash(env: Env): string | null {
  const value = typeof env.HUB_ADMIN_TOKEN_SHA256 === 'string' ? env.HUB_ADMIN_TOKEN_SHA256.trim().toLowerCase() : '';
  return /^[0-9a-f]{64}$/.test(value) ? value : null;
}

/** Token sessions are bound to the configured hash, so rotating the token ends them. */
export function tokenAdminSubject(hash: string): string {
  return 'admin-token:' + hash.slice(0, 16);
}

function sessionAdminAllowed(env: Env, data: SessionData): boolean {
  if (data.email === TOKEN_ADMIN_EMAIL) {
    const hash = adminTokenHash(env);
    return !!hash && data.sub === tokenAdminSubject(hash);
  }
  return configuredAdmins(env).includes(data.email.toLowerCase());
}

/** Human/operator bearer allowlist: Google allowlisted email or the legacy
 * bootstrap admin-token identity. Never trusts request-supplied fields. */
export function humanAdminAllowed(env: Env, email: string, sub: string): boolean {
  if (typeof email !== 'string' || typeof sub !== 'string' || !email || !sub) return false;
  if (email === TOKEN_ADMIN_EMAIL) {
    const hash = adminTokenHash(env);
    return !!hash && sub === tokenAdminSubject(hash);
  }
  return configuredAdmins(env).includes(email.toLowerCase());
}

export function validSessionSecret(env: Env): boolean {
  return typeof env.SESSION_SECRET === 'string' && env.SESSION_SECRET.length >= 32 && !env.SESSION_SECRET.includes('<');
}

const COOKIE_NAME = 'hub_session';
const COOKIE_LIFETIME = 12 * 3600; // 12 hours

export interface SessionData {
  sub: string;
  email: string;
  isAdmin: boolean;
  csrfToken: string;
  exp: number;
}

export async function createSessionCookie(env: Env, data: SessionData): Promise<string> {
  if (!validSessionSecret(env)) throw new Error('Authentication is not configured');
  const payloadStr = JSON.stringify(data);
  const payload = base64url(new TextEncoder().encode(payloadStr));
  const signature = await signHmac(env.SESSION_SECRET, payload);
  const cookieValue = `${payload}.${signature}`;
  return `${COOKIE_NAME}=${cookieValue}; Path=/; Max-Age=${COOKIE_LIFETIME}; HttpOnly; Secure; SameSite=Lax`;
}

export function clearSessionCookie(): string {
  return `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}

export async function getSession(env: Env, request: Request): Promise<SessionData | null> {
  if (!validSessionSecret(env)) return null;
  const cookieHeader = request.headers.get('Cookie');
  if (!cookieHeader) return null;
  const match = cookieHeader.match(new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([^;]+)`));
  if (!match) return null;
  const token = match[1];
  const parts = token.split('.');
  if (parts.length !== 2) return null;
  const [payload, signature] = parts;
  
  if (!(await verifyHmac(env.SESSION_SECRET, payload, signature))) {
    return null;
  }
  
  try {
    const payloadBytes = decodeBase64url(payload);
    const data = JSON.parse(new TextDecoder().decode(payloadBytes)) as SessionData;
    if (typeof data.exp !== 'number' || !Number.isFinite(data.exp) || data.exp <= Math.floor(Date.now() / 1000) || typeof data.sub !== 'string' || !data.sub || typeof data.email !== 'string' || data.isAdmin !== true || typeof data.csrfToken !== 'string' || !data.csrfToken || !sessionAdminAllowed(env, data)) {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

export function verifyCsrf(request: Request, session: SessionData | null): boolean {
  if (request.method === 'GET' || request.method === 'HEAD' || request.method === 'OPTIONS') {
    return true;
  }
  const origin = request.headers.get('Origin');
  if (origin && origin !== new URL(request.url).origin) {
    return false;
  }
  const csrfHeader = request.headers.get('x-csrf-token');
  if (!csrfHeader || !session || csrfHeader !== session.csrfToken) return false;
  return true;
}

const LOGIN_COOKIE_NAME = 'hub_login';

export interface LoginData {
  state: string;
  nonce: string;
  verifier: string;
  exp: number;
  returnTo?: string;
  callbackUrl?: string;
}

interface LoginJar {
  states: LoginData[];
}

function parseValidLoginItem(item: any, now: number): LoginData | null {
  if (!item || typeof item !== 'object') return null;
  if (typeof item.exp !== 'number' || !Number.isFinite(item.exp) || item.exp <= now) return null;
  if (![item.state, item.nonce, item.verifier].every((v: unknown) => typeof v === 'string' && v.length > 0)) return null;
  const res: LoginData = {
    state: item.state,
    nonce: item.nonce,
    verifier: item.verifier,
    exp: item.exp,
  };
  if (typeof item.returnTo === 'string' && item.returnTo) res.returnTo = item.returnTo;
  if (typeof item.callbackUrl === 'string' && item.callbackUrl) res.callbackUrl = item.callbackUrl;
  return res;
}

export async function createLoginCookie(env: Env, data: LoginData, existingCookieHeader?: string | null): Promise<string> {
  if (!validSessionSecret(env)) throw new Error('Authentication is not configured');
  const now = Math.floor(Date.now() / 1000);
  const pendingStates: LoginData[] = [];

  if (existingCookieHeader) {
    const match = existingCookieHeader.match(new RegExp(`(?:^|;\\s*)${LOGIN_COOKIE_NAME}=([^;]+)`));
    if (match) {
      const parts = match[1].split('.');
      if (parts.length === 2 && await verifyHmac(env.SESSION_SECRET, parts[0], parts[1])) {
        try {
          const raw = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[0])));
          if (Array.isArray(raw?.states)) {
            for (const item of raw.states) {
              const valid = parseValidLoginItem(item, now);
              if (valid && valid.state !== data.state) pendingStates.push(valid);
            }
          } else {
            const valid = parseValidLoginItem(raw, now);
            if (valid && valid.state !== data.state) pendingStates.push(valid);
          }
        } catch {}
      }
    }
  }

  // Keep up to 4 most recent prior unexpired states plus the new one
  const pruned = pendingStates.slice(-4);
  pruned.push(data);

  const payloadStr = JSON.stringify({ states: pruned });
  const payload = base64url(new TextEncoder().encode(payloadStr));
  const signature = await signHmac(env.SESSION_SECRET, payload);
  const cookieValue = `${payload}.${signature}`;
  return `${LOGIN_COOKIE_NAME}=${cookieValue}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`;
}

export async function getLoginCookie(env: Env, request: Request, expectedState?: string): Promise<LoginData | null> {
  if (!validSessionSecret(env)) return null;
  const cookieHeader = request.headers.get('Cookie');
  if (!cookieHeader) return null;
  const match = cookieHeader.match(new RegExp(`(?:^|;\\s*)${LOGIN_COOKIE_NAME}=([^;]+)`));
  if (!match) return null;
  const token = match[1];
  const parts = token.split('.');
  if (parts.length !== 2) return null;
  const [payload, signature] = parts;
  
  if (!(await verifyHmac(env.SESSION_SECRET, payload, signature))) return null;
  
  try {
    const now = Math.floor(Date.now() / 1000);
    const raw = JSON.parse(new TextDecoder().decode(decodeBase64url(payload)));
    if (Array.isArray(raw?.states)) {
      const validStates = raw.states.map((item: any) => parseValidLoginItem(item, now)).filter((s: LoginData | null): s is LoginData => s !== null);
      if (expectedState) {
        return validStates.find((s: LoginData) => s.state === expectedState) || null;
      }
      return validStates[validStates.length - 1] || null;
    }
    const single = parseValidLoginItem(raw, now);
    if (!single) return null;
    if (expectedState && single.state !== expectedState) return null;
    return single;
  } catch {
    return null;
  }
}

export async function clearLoginState(env: Env, request: Request, consumedState?: string): Promise<string> {
  if (!consumedState || !validSessionSecret(env)) return clearLoginCookie();
  const cookieHeader = request.headers.get('Cookie');
  if (!cookieHeader) return clearLoginCookie();
  const match = cookieHeader.match(new RegExp(`(?:^|;\\s*)${LOGIN_COOKIE_NAME}=([^;]+)`));
  if (!match) return clearLoginCookie();
  const token = match[1];
  const parts = token.split('.');
  if (parts.length !== 2) return clearLoginCookie();
  if (!(await verifyHmac(env.SESSION_SECRET, parts[0], parts[1]))) return clearLoginCookie();

  try {
    const now = Math.floor(Date.now() / 1000);
    const raw = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[0])));
    if (Array.isArray(raw?.states)) {
      const remaining = raw.states
        .map((item: any) => parseValidLoginItem(item, now))
        .filter((s: LoginData | null): s is LoginData => s !== null && s.state !== consumedState);
      if (remaining.length > 0) {
        const payloadStr = JSON.stringify({ states: remaining });
        const payload = base64url(new TextEncoder().encode(payloadStr));
        const signature = await signHmac(env.SESSION_SECRET, payload);
        const cookieValue = `${payload}.${signature}`;
        return `${LOGIN_COOKIE_NAME}=${cookieValue}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`;
      }
    }
  } catch {}
  return clearLoginCookie();
}

export function clearLoginCookie(): string {
  return `${LOGIN_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}

export function allowedCallbackUrls(env: Env): string[] {
  const urls: string[] = [];
  if (typeof env.AUTH_CALLBACK_URL === 'string' && env.AUTH_CALLBACK_URL) {
    urls.push(env.AUTH_CALLBACK_URL);
  }
  const rawList = (env as any).AUTH_CALLBACK_URLS;
  if (typeof rawList === 'string') {
    for (const item of rawList.split(',')) {
      const trimmed = item.trim();
      if (trimmed && !urls.includes(trimmed)) {
        urls.push(trimmed);
      }
    }
  }
  return urls;
}

export function resolveCallbackUrl(env: Env, requestUrl: string, forwardedHost?: string | null): string {
  const allowed = allowedCallbackUrls(env);
  if (allowed.length === 0) return env.AUTH_CALLBACK_URL || '';

  if (forwardedHost) {
    const cleanHost = forwardedHost.trim().split(',')[0].trim().toLowerCase();
    for (const candidate of allowed) {
      try {
        if (new URL(candidate).host.toLowerCase() === cleanHost) {
          return candidate;
        }
      } catch {}
    }
  }

  try {
    const reqOrigin = new URL(requestUrl).origin;
    for (const candidate of allowed) {
      if (new URL(candidate).origin === reqOrigin) {
        return candidate;
      }
    }
  } catch {}
  return allowed[0];
}

export function isAllowedOidcOrigin(env: Env, requestUrl: string, forwardedHost?: string | null): boolean {
  const allowed = allowedCallbackUrls(env);
  if (forwardedHost) {
    const cleanHost = forwardedHost.trim().split(',')[0].trim().toLowerCase();
    if (allowed.some(c => {
      try { return new URL(c).host.toLowerCase() === cleanHost; } catch { return false; }
    })) {
      return true;
    }
  }
  try {
    const reqOrigin = new URL(requestUrl).origin;
    return allowed.some(candidate => {
      try { return new URL(candidate).origin === reqOrigin; } catch { return false; }
    });
  } catch {
    return false;
  }
}

