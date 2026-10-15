import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const TOKEN = 'test-admin-token-with-plenty-of-entropy-0123456789';
async function hex(value: string) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function hub(hash: string | undefined) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('token-auth-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (instance: Hub) => { instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: hash } as Env; });
  return stub;
}
const signIn = (stub: any, token: string) => stub.fetch(new Request('https://hub.example/auth/token', { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ token }).toString() }));
const cookieOf = (r: Response) => (r.headers.get('Set-Cookie') || '').split(';')[0];

describe('admin token sign-in', () => {
  it('is unavailable until a token hash is configured', async () => {
    const stub = await hub(undefined);
    expect((await stub.fetch(new Request('https://hub.example/auth/token'))).status).toBe(503);
    expect((await signIn(stub, TOKEN)).status).toBe(503);
  });

  it('rejects a wrong token and signs in with the right one', async () => {
    const stub = await hub(await hex(TOKEN));
    const form = await stub.fetch(new Request('https://hub.example/auth/token'));
    expect(form.status).toBe(200);
    expect(form.headers.get('Content-Security-Policy')).toContain("default-src 'none'");
    const wrong = await signIn(stub, TOKEN + 'x');
    expect(wrong.status).toBe(401); expect(wrong.headers.get('Set-Cookie')).toBeNull();
    const right = await signIn(stub, TOKEN);
    expect(right.status).toBe(303); expect(right.headers.get('Location')).toBe('/');
    const cookie = cookieOf(right);
    expect(cookie).toMatch(/^hub_session=/);
    const me = await stub.fetch(new Request('https://hub.example/auth/me', { headers: { Cookie: cookie } }));
    expect(me.status).toBe(200);
    expect(((await me.json()) as any).email).toBe('admin-token@hub.local');
    expect((await stub.fetch(new Request('https://hub.example/api/liveness', { headers: { Cookie: cookie } }))).status).toBe(200);
  });

  it('ends token sessions when the token is rotated', async () => {
    const stub = await hub(await hex(TOKEN));
    const cookie = cookieOf(await signIn(stub, TOKEN));
    await runInDurableObject(stub, async (instance: Hub) => { instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await hex('rotated-token-value-0123456789abcdef') } as Env; });
    expect((await stub.fetch(new Request('https://hub.example/auth/me', { headers: { Cookie: cookie } }))).status).toBe(401);
  });

  it('rate-limits repeated failures', async () => {
    const stub = await hub(await hex(TOKEN));
    for (let i = 0; i < 10; i++) expect((await signIn(stub, 'guess-' + i)).status).toBe(401);
    expect((await signIn(stub, TOKEN)).status).toBe(429);
  });

  it('sends browser sign-in to the token form on an origin that is not the OIDC callback', async () => {
    const stub = await hub(await hex(TOKEN));
    const r = await stub.fetch(new Request('https://hub.example/auth/login', { redirect: 'manual' }));
    expect(r.status).toBe(303); expect(r.headers.get('Location')).toBe('/auth/token');
    const device = await stub.fetch(new Request('https://hub.example/auth/login?return_to=' + encodeURIComponent('/device?code=ABCD-1234'), { redirect: 'manual' }));
    expect(device.headers.get('Location')).toBe('/auth/token?return_to=' + encodeURIComponent('/device?code=ABCD-1234'));
    const back = await stub.fetch(new Request('https://hub.example/auth/token', { method: 'POST', redirect: 'manual', body: new URLSearchParams({ token: TOKEN, return_to: '/device?code=ABCD-1234' }).toString() }));
    expect(back.headers.get('Location')).toBe('/device?code=ABCD-1234');
    const offsite = await stub.fetch(new Request('https://hub.example/auth/token', { method: 'POST', redirect: 'manual', body: new URLSearchParams({ token: TOKEN, return_to: 'https://evil.example/' }).toString() }));
    expect(offsite.headers.get('Location')).toBe('/');
  });
});
