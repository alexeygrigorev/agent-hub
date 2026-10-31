import { describe, expect, it, vi } from 'vitest';
import { hostRepositoryAccess, installationJwt } from '../src/host-repo-access';

const now = 1793180000000;
const makePolicy = () => ({ enabled: true, issuer: 'Iv1.test-app', privateKey: 'synthetic-fixture-only',
  repositories: [{ alias: 'hub', owner: 'example-org', name: 'hub',
    installationId: 12, repositoryId: 34 }] });
const token = 'synthetic-installation-token';
const result = () => ({ token, expires_at: new Date(now + 3600000).toISOString(),
  permissions: { contents: 'read', metadata: 'read' }, repositories: [{ id: 34 }],
  repository_selection: 'selected' });
const request = (body: any = { repository: 'hub' }) =>
  new Request('https://hub.test/api/host/repositories/token',
    { method: 'POST', body: JSON.stringify(body) });
const signer = async () => 'synthetic.jwt.signature';
const fixture = (getPolicy: () => unknown = makePolicy, authorize = () => true,
  fetcher = vi.fn(async () => Response.json(result(), { status: 201 }))) => ({
    fetcher, handler: hostRepositoryAccess(getPolicy, authorize, { fetcher, signer, clock: () => now }),
  });

describe('prepared host repository access capability', () => {
  it('fails closed before disclosure or upstream activity without current host consent', async () => {
    const f = fixture(makePolicy, () => false);
    const response = await f.handler(request());
    expect(response.status).toBe(403);
    expect(f.fetcher).not.toHaveBeenCalled();
    expect(await response.text()).not.toContain(token);
  });
  it('is disabled without explicit valid operator configuration', async () => {
    for (const p of [null, {}, { ...makePolicy(), enabled: false },
      { ...makePolicy(), repositories: [{ ...makePolicy().repositories[0], name: '../escape' }] }]) {
      const f = fixture(() => p);
      expect((await f.handler(request())).status).toBe(503);
      expect(f.fetcher).not.toHaveBeenCalled();
    }
  });
  it('lists only safe metadata with no credential disclosure or caching', async () => {
    const f = fixture();
    const response = await f.handler(new Request('https://hub.test/api/host/repositories'));
    expect(response.headers.get('Cache-Control')).toBe('no-store');
    expect(await response.json()).toEqual({ repositories: [
      { alias: 'hub', origin: 'https://github.com/example-org/hub.git' },
    ] });
    expect(f.fetcher).not.toHaveBeenCalled();
  });
  it('requests one server-owned repository and read-only contents permissions', async () => {
    const f = fixture();
    const response = await f.handler(request());
    expect(response.status).toBe(200);
    const [url, options] = f.fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.github.com/app/installations/12/access_tokens');
    expect(options.redirect).toBe('error');
    expect(JSON.parse(options.body as string)).toEqual({
      repository_ids: [34], permissions: { contents: 'read' },
    });
    expect(new Headers(options.headers).get('X-GitHub-Api-Version')).toBe('2026-03-10');
    expect(await response.json()).toEqual({ origin: 'https://github.com/example-org/hub.git',
      username: 'x-access-token', token, expires_at: result().expires_at });
    expect(response.headers.get('Cache-Control')).toBe('no-store');
  });
  it('refuses caller-selected installation, URL, permissions or unknown repository', async () => {
    const f = fixture();
    for (const body of [{ repository: 'hub', installationId: 99 },
      { repository: 'hub', origin: 'https://attacker.test' },
      { repository: 'hub', permissions: { contents: 'write' } }, { repository: 'unknown' }])
      expect((await f.handler(request(body))).status).toBeGreaterThanOrEqual(400);
    expect(f.fetcher).not.toHaveBeenCalled();
  });
  it('does not deliver a token if approval is revoked during the upstream await', async () => {
    let allowed = true;
    const fetcher = vi.fn(async () => { allowed = false; return Response.json(result(), { status: 201 }); });
    const f = fixture(makePolicy, () => allowed, fetcher);
    const response = await f.handler(request());
    expect(response.status).toBe(403);
    expect(await response.text()).not.toContain(token);
  });
  it('does not deliver a token after operator policy changes during signing', async () => {
    let p = makePolicy();
    const fetcher = vi.fn(async () => Response.json(result(), { status: 201 }));
    const handler = hostRepositoryAccess(() => p, () => true, { fetcher, clock: () => now,
      signer: async () => { p = { ...p, enabled: false }; return 'synthetic.jwt.signature'; } });
    expect((await handler(request())).status).toBe(403);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('rejects expired, overbroad or wrong-repository responses without reflecting secrets', async () => {
    const responses = [
      { ...result(), expires_at: new Date(now).toISOString() },
      { ...result(), expires_at: new Date(now + 7200000).toISOString() },
      { ...result(), permissions: { contents: 'write' } },
      { ...result(), permissions: { contents: 'read', issues: 'write' } },
      { ...result(), repositories: [{ id: 99 }] },
      { ...result(), repositories: [{ id: 34 }, { id: 99 }] },
      { ...result(), repository_selection: 'all' },
    ];
    for (const body of responses) {
      const f = fixture(makePolicy, () => true, vi.fn(async () => Response.json(body, { status: 201 })));
      const response = await f.handler(request());
      expect(response.status).toBe(503);
      expect(await response.text()).not.toContain(token);
    }
  });
  it('bounds response sizes and sanitizes upstream errors', async () => {
    for (const body of [new Response('private-upstream-detail', { status: 403 }),
      new Response('x'.repeat(65537), { status: 201 }),
      new Response('{truncated', { status: 201 })]) {
      const f = fixture(makePolicy, () => true, vi.fn(async () => body));
      const response = await f.handler(request());
      expect(response.status).toBe(503);
      expect(await response.text()).toBe('{"error":"repository_access_unavailable"}');
    }
  });
  it('aborts upstream I/O after its deadline and clears the timer', async () => {
    vi.useFakeTimers();
    try {
      let started!: () => void;
      const ready = new Promise<void>(resolve => { started = resolve; });
      const fetcher: typeof fetch = async (_input, init) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('synthetic timeout')), { once: true });
        started();
      });
      const handler = hostRepositoryAccess(makePolicy, () => true, { fetcher, signer, clock: () => now });
      const pending = handler(request());
      await ready;
      await vi.advanceTimersByTimeAsync(10000);
      expect((await pending).status).toBe(503);
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });
  it('signs RS256 with existing synthetic PKCS8 and PKCS1 keys, with bounded expiry', async () => {
    const keys = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5',
      modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true, ['sign', 'verify']) as CryptoKeyPair;
    const exported = await crypto.subtle.exportKey('pkcs8', keys.privateKey);
    if (!(exported instanceof ArrayBuffer)) throw new Error('unexpected synthetic key export');
    const bytes = new Uint8Array(exported);
    // Extract the PKCS1 octet string from WebCrypto's PKCS8 fixture without
    // using the production wrapper; verification uses the original public key.
    const element = (offset: number) => {
      let start = offset + 1;
      let length = bytes[start++];
      if (length & 128) {
        const count = length & 127;
        length = 0;
        for (let i = 0; i < count; i++) length = length * 256 + bytes[start++];
      }
      return { tag: bytes[offset], start, end: start + length };
    };
    const outer = element(0);
    const version = element(outer.start);
    const algorithm = element(version.end);
    const octet = element(algorithm.end);
    expect(octet.tag).toBe(4);
    const pem = (kind: string, content: Uint8Array) => '-----BEGIN ' + kind + '-----\n' +
      btoa(Array.from(content, x => String.fromCharCode(x)).join('')) + '\n-----END ' + kind + '-----';
    const fixtures = [pem('PRIVATE KEY', bytes),
      pem('RSA PRIVATE KEY', bytes.slice(octet.start, octet.end))];
    const decode = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
    for (const privateKey of fixtures) {
      const jwt = await installationJwt({ issuer: 'Iv1.test-app', privateKey }, now);
      const [header, payload, signature] = jwt.split('.');
      expect(JSON.parse(new TextDecoder().decode(decode(header))).alg).toBe('RS256');
      expect(JSON.parse(new TextDecoder().decode(decode(payload)))).toEqual({
        iss: 'Iv1.test-app', iat: now / 1000 - 60, exp: now / 1000 + 480,
      });
      expect(await crypto.subtle.verify('RSASSA-PKCS1-v1_5', keys.publicKey,
        decode(signature), new TextEncoder().encode(header + '.' + payload))).toBe(true);
    }
  });
});
