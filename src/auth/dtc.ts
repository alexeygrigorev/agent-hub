import { Env } from '../env';
import { verifyRs256, decodeBase64url } from './crypto';

interface Jwk {
  kid?: string;
  alg?: string;
  use?: string;
  kty?: string;
  n?: string;
  e?: string;
}

export class JwksCache {
  private keys: Jwk[] = [];
  private expiresAt: number = 0;
  private pendingPromise: Promise<Jwk[]> | null = null;
  private lastFetchAt: number = 0;

  async getKeys(env: Env, force = false): Promise<Jwk[]> {
    const now = Date.now();
    if (!force && this.expiresAt > now) {
      return this.keys;
    }
    if (force && now - this.lastFetchAt < 10000) {
      return this.keys;
    }
    if (this.pendingPromise) {
      return this.pendingPromise;
    }
    
    this.lastFetchAt = now; // Set immediately to enforce cooldown even on failure
    
    this.pendingPromise = (async () => {
      try {
        const res = await fetch(env.AUTH_JWKS_URL, { headers: { accept: 'application/json' } });
        if (!res.ok) throw new Error('JWKS fetch failed');
        const text = await res.text();
        if (text.length > 256 * 1024) throw new Error('JWKS payload too large');
        const data = JSON.parse(text) as { keys: Jwk[] };
        if (!Array.isArray(data.keys) || data.keys.length > 32) throw new Error('Invalid JWKS');
        this.keys = data.keys.filter(k => k && k.kty === 'RSA' && (!k.alg || k.alg === 'RS256') && (!k.use || k.use === 'sig') && typeof k.kid === 'string' && typeof k.n === 'string' && typeof k.e === 'string');
        this.lastFetchAt = Date.now();
        this.expiresAt = this.lastFetchAt + 300000; // 5 minutes
        return this.keys;
      } finally {
        this.pendingPromise = null;
      }
    })();
    return this.pendingPromise;
  }
}

export async function validateIdToken(env: Env, token: string, expectedNonce: string, jwksCache: JwksCache): Promise<{ sub: string, email: string, email_verified: boolean }> {
  const parts = token.split('.');
  if (typeof token !== 'string' || token.length > 16384 || parts.length !== 3) throw new Error('Invalid token format');

  const header = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[1])));

  if (header.alg !== 'RS256' || typeof header.kid !== 'string' || !header.kid || header.kid.length > 256) throw new Error('Invalid alg or missing kid');

  let keys = await jwksCache.getKeys(env);
  let key = keys.find(k => k.kid === header.kid);
  if (!key) {
    keys = await jwksCache.getKeys(env, true);
    key = keys.find(k => k.kid === header.kid);
  }
  if (!key) throw new Error('Signing key not found');

  const valid = await verifyRs256(key as JsonWebKey, `${parts[0]}.${parts[1]}`, parts[2]);
  if (!valid) throw new Error('Invalid signature');

  const now = Math.floor(Date.now() / 1000);
  if (claims.iss !== env.AUTH_ISSUER) throw new Error('Invalid issuer');
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.every((v: unknown) => typeof v === 'string') || (aud.length > 1 && claims.azp !== env.AUTH_CLIENT_ID)) throw new Error('Invalid audience');
  if (!aud.includes(env.AUTH_CLIENT_ID)) throw new Error('Invalid audience');
  if (typeof claims.exp !== 'number' || !Number.isFinite(claims.exp) || claims.exp <= now) throw new Error('Token expired');
  if (claims.nonce !== expectedNonce) throw new Error('Invalid nonce');
  if (claims.token_use !== 'id') throw new Error('Invalid token_use');
  if (claims.email_verified !== true) throw new Error('Email not verified');
  if (typeof claims.email !== 'string' || !claims.email) throw new Error('Missing email');
  if (typeof claims.sub !== 'string' || !claims.sub) throw new Error('Missing sub');

  return { sub: claims.sub, email: claims.email, email_verified: true };
}

export async function exchangeCode(env: Env, code: string, verifier: string): Promise<string> {
  const params = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: env.AUTH_CLIENT_ID,
    code,
    redirect_uri: env.AUTH_CALLBACK_URL,
    code_verifier: verifier
  });
  const res = await fetch(`${env.AUTH_BASE_URL}/oauth2/token`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
    body: params.toString()
  });
  if (!res.ok) throw new Error('Token exchange failed');
  const data = await res.json() as { id_token?: string };
  if (!data.id_token) throw new Error('Missing id_token');
  return data.id_token;
}
