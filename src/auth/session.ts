import { Env } from '../env';
import { signHmac, verifyHmac, base64url, decodeBase64url } from './crypto';

export function configuredAdmins(env: Env): string[] {
  return typeof env.HUB_ADMINS === 'string' ? env.HUB_ADMINS.split(',').map(s => s.trim().toLowerCase()).filter(s => s && !s.includes('<')) : [];
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
    if (typeof data.exp !== 'number' || !Number.isFinite(data.exp) || data.exp <= Math.floor(Date.now() / 1000) || typeof data.sub !== 'string' || !data.sub || typeof data.email !== 'string' || data.isAdmin !== true || typeof data.csrfToken !== 'string' || !data.csrfToken || !configuredAdmins(env).includes(data.email.toLowerCase())) {
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
}

export async function createLoginCookie(env: Env, data: LoginData): Promise<string> {
  if (!validSessionSecret(env)) throw new Error('Authentication is not configured');
  const payloadStr = JSON.stringify(data);
  const payload = base64url(new TextEncoder().encode(payloadStr));
  const signature = await signHmac(env.SESSION_SECRET, payload);
  const cookieValue = `${payload}.${signature}`;
  return `${LOGIN_COOKIE_NAME}=${cookieValue}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`;
}

export async function getLoginCookie(env: Env, request: Request): Promise<LoginData | null> {
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
    const data = JSON.parse(new TextDecoder().decode(decodeBase64url(payload))) as LoginData;
    if (typeof data.exp !== 'number' || !Number.isFinite(data.exp) || data.exp <= Math.floor(Date.now() / 1000) || ![data.state, data.nonce, data.verifier].every(v => typeof v === 'string' && v.length > 0)) return null;
    return data;
  } catch {
    return null;
  }
}

export function clearLoginCookie(): string {
  return `${LOGIN_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}
