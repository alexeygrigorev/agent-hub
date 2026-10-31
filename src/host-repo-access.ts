/**
 * Scoped repository enrollment, mounted on the authenticated host routes.
 * No account/key creation or default grant. The route supplies a fresh
 * host-family AND explicitly approved repository-read-scope predicate.
 *
 * Contract: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
 */
type Repository = { alias: string; owner: string; name: string; installationId: number; repositoryId: number };
type Policy = { enabled: true; issuer: string; privateKey: string; repositories: Repository[] };
type Dependencies = {
  fetcher?: typeof fetch;
  clock?: () => number;
  signer?: (policy: Policy, now: number) => Promise<string>;
};
const API_VERSION = '2026-03-10';
const LIMIT = 64 * 1024;
const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };
const reply = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
  Response.json(body, { status, headers: { ...headers, ...extra } });
const forbidden = () => reply({ error: 'forbidden' }, 403);
const unavailable = () => reply({ error: 'repository_access_unavailable' }, 503);
const id = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0;
const origin = (r: Repository) => 'https://github.com/' + r.owner + '/' + r.name + '.git';

function policy(value: unknown): Policy | null {
  try {
    const p = typeof value === 'string' ? JSON.parse(value) : value;
    if (!p || p.enabled !== true || typeof p.issuer !== 'string' ||
        !/^[A-Za-z0-9_.-]{1,128}$/.test(p.issuer) || typeof p.privateKey !== 'string' ||
        !p.privateKey || p.privateKey.length > 16384 || !Array.isArray(p.repositories) ||
        !p.repositories.length || p.repositories.length > 64) return null;
    const aliases = new Set<string>();
    const repositories: Repository[] = [];
    for (const r of p.repositories) {
      if (!r || typeof r.alias !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(r.alias) ||
          aliases.has(r.alias) || typeof r.owner !== 'string' ||
          !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/.test(r.owner) ||
          typeof r.name !== 'string' || !/^[A-Za-z0-9_.-]{1,100}$/.test(r.name) ||
          ['.', '..'].includes(r.name) || !id(r.installationId) || !id(r.repositoryId)) return null;
      aliases.add(r.alias);
      repositories.push({ alias: r.alias, owner: r.owner, name: r.name,
        installationId: r.installationId, repositoryId: r.repositoryId });
    }
    return { enabled: true, issuer: p.issuer, privateKey: p.privateKey, repositories };
  } catch { return null; }
}

function b64(bytes: Uint8Array): string {
  return btoa(Array.from(bytes, v => String.fromCharCode(v)).join(''))
    .replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}
function der(tag: number, value: Uint8Array): Uint8Array {
  const n = value.length;
  const length = n < 128 ? [n] : n < 256 ? [129, n] : [130, n >> 8, n & 255];
  return Uint8Array.from([tag, ...length, ...value]);
}

/** Accept existing PKCS8 or GitHub's RSA/PKCS1 PEM; never creates a key. */
export async function installationJwt(p: Pick<Policy, 'issuer' | 'privateKey'>, now: number): Promise<string> {
  const pem = p.privateKey.replace(/\\n/g, '\n').trim();
  const match = /^-----BEGIN (RSA PRIVATE KEY|PRIVATE KEY)-----\s+([A-Za-z0-9+/=\s]+)-----END \1-----$/.exec(pem);
  if (!match) throw new Error('invalid_app_key');
  let bytes: Uint8Array = Uint8Array.from(atob(match[2].replace(/\s/g, '')), c => c.charCodeAt(0));
  if (match[1] === 'RSA PRIVATE KEY') {
    const algorithm = [48, 13, 6, 9, 42, 134, 72, 134, 247, 13, 1, 1, 1, 5, 0];
    bytes = der(48, Uint8Array.from([2, 1, 0, ...algorithm, ...der(4, bytes)]));
  }
  const key = await crypto.subtle.importKey('pkcs8', bytes.buffer as ArrayBuffer,
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign']);
  const encode = (v: unknown) => b64(new TextEncoder().encode(JSON.stringify(v)));
  const seconds = Math.floor(now / 1000);
  const input = encode({ alg: 'RS256', typ: 'JWT' }) + '.' +
    encode({ iss: p.issuer, iat: seconds - 60, exp: seconds + 480 });
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(input));
  return input + '.' + b64(new Uint8Array(signature));
}

async function boundedJson(response: Response, limit: number): Promise<any> {
  if (!response.body) throw new Error('empty_response');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > limit) { await reader.cancel(); throw new Error('response_too_large'); }
      chunks.push(chunk.value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes));
}

/**
 * Every callback invocation must re-read current presenting credential, host
 * family/approval, expiry and explicit repository-read consent. A cached bool
 * is not a valid production authorize implementation.
 */
export function hostRepositoryAccess(
  readPolicy: () => unknown, authorize: () => boolean, dependencies: Dependencies = {},
) {
  const now = dependencies.clock || Date.now;
  const fetcher = dependencies.fetcher || fetch;
  const signer = dependencies.signer || installationJwt;
  const authorized = () => { try { return authorize() === true; } catch { return false; } };
  const configured = () => { try { return policy(readPolicy()); } catch { return null; } };

  return async (request: Request): Promise<Response> => {
    if (!authorized()) return forbidden();
    const path = new URL(request.url).pathname;
    if (path !== '/api/host/repositories' && path !== '/api/host/repositories/token')
      return reply({ error: 'not_found' }, 404);
    if ((path.endsWith('/token') && request.method !== 'POST') ||
        (!path.endsWith('/token') && request.method !== 'GET'))
      return reply({ error: 'method_not_allowed' }, 405, { Allow: path.endsWith('/token') ? 'POST' : 'GET' });
    const selected = configured();
    if (!selected) return reply({ error: 'repository_access_not_configured' }, 503);
    if (request.method === 'GET') {
      return reply({ repositories: selected.repositories.map(r => ({ alias: r.alias, origin: origin(r) })) });
    }
    let body: any;
    try { body = await boundedJson(new Response(request.body), 2048); }
    catch { return reply({ error: 'invalid_request' }, 400); }
    if (!authorized()) return forbidden();
    if (!body || typeof body !== 'object' || Array.isArray(body) ||
        Object.keys(body).length !== 1 || typeof body.repository !== 'string')
      return reply({ error: 'invalid_request' }, 400);
    const repo = selected.repositories.find(r => r.alias === body.repository);
    if (!repo) return reply({ error: 'repository_not_allowed' }, 403);
    const fingerprint = JSON.stringify(selected);
    const fresh = () => authorized() && JSON.stringify(configured()) === fingerprint;
    if (!fresh()) return forbidden();
    const controller = new AbortController();
    const abort = () => controller.abort();
    request.signal.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(abort, 10000);
    try {
      if (request.signal.aborted) return unavailable();
      const jwt = await signer(selected, now());
      if (!fresh()) return forbidden();
      if (controller.signal.aborted) return unavailable();
      const response = await fetcher(
        'https://api.github.com/app/installations/' + repo.installationId + '/access_tokens', {
          method: 'POST', redirect: 'error', signal: controller.signal,
          headers: { Authorization: 'Bearer ' + jwt, Accept: 'application/vnd.github+json',
            'Content-Type': 'application/json', 'User-Agent': 'Agent-Hub-Host-Enrollment',
            'X-GitHub-Api-Version': API_VERSION },
          body: JSON.stringify({ repository_ids: [repo.repositoryId], permissions: { contents: 'read' } }),
        });
      if (!fresh()) return forbidden();
      if (response.status !== 201) return unavailable();
      const result = await boundedJson(response, LIMIT);
      if (!fresh()) return forbidden();
      const expires = Date.parse(result?.expires_at);
      const permissions = result?.permissions;
      if (typeof result?.token !== 'string' || !/^[^\s\x00-\x1f\x7f]{12,16384}$/.test(result.token) ||
          !Number.isFinite(expires) || expires <= now() || expires > now() + 3660000 ||
          !permissions || permissions.contents !== 'read' ||
          Object.entries(permissions).some(([k, v]) => !['contents', 'metadata'].includes(k) || v !== 'read') ||
          !Array.isArray(result.repositories) || result.repositories.length !== 1 ||
          result.repositories[0]?.id !== repo.repositoryId ||
          (result.repository_selection !== undefined && result.repository_selection !== 'selected'))
        return unavailable();
      return reply({ origin: origin(repo), username: 'x-access-token',
        token: result.token, expires_at: result.expires_at });
    } catch { return authorized() ? unavailable() : forbidden(); }
    finally {
      clearTimeout(timer);
      request.signal.removeEventListener('abort', abort);
      controller.abort();
    }
  };
}
