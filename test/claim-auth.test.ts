import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { sha256 } from '../src/auth/crypto';

const TOKEN = 'claim-operator-test-token-0123456789abcdef';

async function setup() {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('claim-auth-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (instance: Hub) => {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(TOKEN));
    const hash = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: hash } as Env;
  });
  const request = (path: string, method = 'GET', body?: unknown, extra: Record<string, string> = {}) =>
    stub.fetch(new Request('https://hub.example' + path, {
      method, headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json', ...extra },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }));
  return { stub, request };
}

describe('claims operator bearer authentication', () => {
  it('creates, lists and releases a real claim with the existing operator bearer', async () => {
    const { request } = await setup();
    const created = await request('/api/bus/claims', 'POST', {
      project: 'claim-auth', scope: 'src/auth', holder: 'claim-auth:worker', ttl_ms: 60000,
    });
    expect(created.status).toBe(200);
    const { id } = await created.json() as { id: string };
    const listed = await request('/api/bus/claims');
    expect(listed.status).toBe(200);
    expect(await listed.json()).toEqual(expect.arrayContaining([expect.objectContaining({ id, released_at: null })]));
    expect((await request('/api/bus/claims/' + id + '/release', 'POST')).status).toBe(200);
    expect(await (await request('/api/bus/claims')).json()).toEqual([]);
    expect(await (await request('/api/bus/claims?active=false')).json()).toEqual([
      expect.objectContaining({ id, released_at: expect.any(Number) }),
    ]);
  });

  it('rejects wrong bearer and mixed bearer/cookie authority', async () => {
    const { request } = await setup();
    expect((await request('/api/bus/claims', 'GET', undefined, { Authorization: 'Bearer wrong' })).status).toBe(401);
    expect((await request('/api/bus/claims', 'GET', undefined, { Cookie: 'unrelated=value' })).status).toBe(403);
    expect((await request('/api/bus/claims/missing/release', 'POST', undefined, { Cookie: 'unrelated=value' })).status).toBe(403);
  });

  it('does not upgrade a revoked registered credential matching the configured token', async () => {
    const { stub, request } = await setup();
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(`INSERT INTO credentials (id, kind, token_hash, scopes, family_id, subject, created_by_sub, created_at, expires_at, revoked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, 'revoked', 'agent', await sha256(TOKEN), '["bus:read","bus:send"]', 'revoked-family', 'revoked-worker', 'test-admin',
        new Date().toISOString(), new Date(Date.now() + 60000).toISOString(), new Date().toISOString());
    });
    expect((await request('/api/bus/claims')).status).toBe(401);
    expect((await request('/api/bus/claims/missing/release', 'POST')).status).toBe(401);
  });

  it('preserves root fencing and does not add operator access to other bus endpoints', async () => {
    const { request } = await setup();
    expect((await request('/api/bus/claims', 'POST', {
      project: 'claim-auth', scope: 'src/auth', holder: 'role:root', ttl_ms: 60000,
    })).status).toBe(403);
    expect((await request('/api/bus/inbox?actor=claim-auth%3Aworker')).status).toBe(401);
  });
});
