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
    if (force && now >= this.lastFetchAt && now - this.lastFetchAt < 10000) {
      return this.keys;
    }
    if (this.pendingPromise) {
      return this.pendingPromise;
    }
    
    this.pendingPromise = (async () => {
      const abortCtrl = new AbortController();
      const timeout = setTimeout(() => abortCtrl.abort(), 5000);
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      
      try {
        const res = await fetch(env.AUTH_JWKS_URL, { 
          headers: { accept: 'application/json' },
          signal: abortCtrl.signal 
        });
        if (!res.ok) throw new Error('JWKS fetch failed');
        
        const contentLength = res.headers.get('content-length');
        if (contentLength && parseInt(contentLength, 10) > 256 * 1024) {
          throw new Error('JWKS payload too large');
        }

        const contentType = res.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            throw new Error('JWKS content type not application/json');
        }

        if (!res.body) {
           throw new Error('JWKS body empty');
        }

        reader = res.body.getReader();
        const chunks: Uint8Array[] = [];
        let totalBytes = 0;
        
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value) {
            totalBytes += value.length;
            if (totalBytes > 256 * 1024) {
              throw new Error('JWKS stream exceeded 256KiB');
            }
            chunks.push(value);
          }
        }
        
        const merged = new Uint8Array(totalBytes);
        let offset = 0;
        for (const chunk of chunks) {
          merged.set(chunk, offset);
          offset += chunk.length;
        }
        
        let text: string;
        try {
          text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(merged);
        } catch (e) {
          throw new Error('JWKS payload bad UTF-8');
        }
        
        let data: any;
        try {
          data = JSON.parse(text);
        } catch (e) {
          throw new Error('JWKS payload invalid JSON');
        }
        
        if (!data || typeof data !== 'object' || !Array.isArray(data.keys) || data.keys.length > 32) {
            throw new Error('Invalid JWKS');
        }
        
        for (const k of data.keys) {
            if (!k || typeof k !== 'object') throw new Error('Invalid JWKS');
            if (k.kty !== 'RSA') throw new Error('Invalid JWKS');
            if (k.alg && k.alg !== 'RS256') throw new Error('Invalid JWKS');
            if (k.use && k.use !== 'sig') throw new Error('Invalid JWKS');
            if (typeof k.kid !== 'string' || !k.kid) throw new Error('Invalid JWKS');
            if (typeof k.n !== 'string' || typeof k.e !== 'string') throw new Error('Invalid JWKS');
        }

        this.keys = data.keys;
        const completeNow = Date.now();
        this.lastFetchAt = completeNow;
        this.expiresAt = completeNow + 300000; // 5 minutes
        return this.keys;
      } catch (err) {
        this.lastFetchAt = Date.now();
        throw err;
      } finally {
        clearTimeout(timeout);
        this.pendingPromise = null;
        if (reader) {
          reader.cancel().catch(() => {});
        }
      }
    })();
    return this.pendingPromise;
  }
}

export async function validateIdToken(env: Env, token: string, expectedNonce: string, jwksCache: JwksCache): Promise<{ sub: string, email: string, email_verified: boolean }> {
  const parts = token.split('.');
  if (typeof token !== 'string' || token.length > 16384 || parts.length !== 3) throw new Error('jwt_format_invalid');

  let header: any;
  let claims: any;
  try {
    header = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[0])));
    claims = JSON.parse(new TextDecoder().decode(decodeBase64url(parts[1])));
  } catch {
    throw new Error('jwt_payload_invalid');
  }

  if (header.alg !== 'RS256' || typeof header.kid !== 'string' || !header.kid || header.kid.length > 256) throw new Error('jwt_header_invalid');

  let keys: Jwk[];
  try {
    keys = await jwksCache.getKeys(env);
  } catch {
    throw new Error('jwks_fetch_failed');
  }
  let key = keys.find(k => k.kid === header.kid);
  if (!key) {
    try {
      keys = await jwksCache.getKeys(env, true);
      key = keys.find(k => k.kid === header.kid);
    } catch {
      throw new Error('jwks_fetch_failed');
    }
  }
  if (!key) throw new Error('signing_key_not_found');

  const valid = await verifyRs256(key as JsonWebKey, `${parts[0]}.${parts[1]}`, parts[2]);
  if (!valid) throw new Error('jwt_signature_invalid');

  const now = Math.floor(Date.now() / 1000);
  if (claims.iss !== env.AUTH_ISSUER) throw new Error('jwt_issuer_mismatch');
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.every((v: unknown) => typeof v === 'string') || (aud.length > 1 && claims.azp !== env.AUTH_CLIENT_ID)) throw new Error('jwt_audience_mismatch');
  if (!aud.includes(env.AUTH_CLIENT_ID)) throw new Error('jwt_audience_mismatch');
  if (typeof claims.exp !== 'number' || !Number.isFinite(claims.exp) || claims.exp <= now) throw new Error('jwt_expired');
  if (claims.nonce !== expectedNonce) throw new Error('jwt_nonce_mismatch');
  if (claims.token_use !== 'id') throw new Error('jwt_token_use_invalid');
  if (claims.email_verified !== true) throw new Error('jwt_email_not_verified');
  if (typeof claims.email !== 'string' || !claims.email) throw new Error('jwt_missing_email');
  if (typeof claims.sub !== 'string' || !claims.sub) throw new Error('jwt_missing_sub');

  return { sub: claims.sub, email: claims.email, email_verified: true };
}

export async function exchangeCode(env: Env, code: string, verifier: string, redirectUri?: string): Promise<string> {
  const params = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: env.AUTH_CLIENT_ID,
    code,
    redirect_uri: redirectUri || env.AUTH_CALLBACK_URL,
    code_verifier: verifier
  });
  const res = await fetch(`${env.AUTH_BASE_URL}/oauth2/token`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded', accept: 'application/json' },
    body: params.toString()
  });
  if (!res.ok) {
    let errCode = '';
    try {
      const data = await res.json() as any;
      if (data && typeof data.error === 'string') {
        errCode = data.error.replace(/[^a-z0-9_-]/gi, '');
      }
    } catch {
      try {
        const text = await res.text();
        const m = text.match(/"error"\s*:\s*"([a-zA-Z0-9_-]+)"/);
        if (m) errCode = m[1];
      } catch {}
    }
    const suffix = errCode ? `_${errCode}` : '';
    throw new Error(`exchange_failed_${res.status}${suffix}`);
  }
  const data = await res.json() as { id_token?: string };
  if (!data.id_token) throw new Error('exchange_missing_id_token');
  return data.id_token;
}

