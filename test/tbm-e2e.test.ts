import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
let cookie: string;
const call = (path: string, body?: any, csrf = true) => SELF.fetch('https://hub.test/api/quota/' + path, {
  method: body === undefined ? 'GET' : 'POST',
  headers: { Cookie: cookie, ...(csrf ? { 'x-csrf-token': 'tbm-csrf' } : {}) },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'tbm-operator', email: 'admin@fake.local',
    isAdmin: true, csrfToken: 'tbm-csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    h.sql.exec('DELETE FROM tbm_quota_pools');
    h.sql.exec('DELETE FROM tbm_banked_tranches');
  });
});

describe('TBM public REST composition', () => {
  it('requires authenticated admin and cookie CSRF for kill-switch changes', async () => {
    expect((await SELF.fetch('https://hub.test/api/quota/pacing')).status).toBe(401);
    expect((await call('kill-switch', { active: true }, false)).status).toBe(403);
    expect(await (await call('kill-switch')).json()).toMatchObject({ active: false });
    cookie = (await createSessionCookie(env as any, { sub: 'viewer', email: 'viewer@fake.local',
      isAdmin: false, csrfToken: 'tbm-csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
    expect([401, 403]).toContain((await call('kill-switch', { active: true })).status);
  });

  it('persists a global kill switch and supports repeated on/off requests', async () => {
    for (const active of [true, true, false]) {
      const response = await call('kill-switch', { active, reason: 'staged drill' });
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ active, reason: 'staged drill' });
      expect(await (await call('kill-switch')).json()).toMatchObject({ active });
    }
    expect((await call('kill-switch', { active: 'false' })).status).toBe(400);
    expect((await call('kill-switch', { active: true, reason: 'x'.repeat(3000) })).status).toBe(413);
  });

  it('exposes stored pacing and banked rows with non-neutral routing factors', async () => {
    await inDO(stub(), (h: any) => {
      const now = Date.now();
      h.sql.exec(`INSERT INTO tbm_quota_pools (pool_id, provider, account, total_capacity_tokens,
        remaining_tokens, headroom_percentage, reset_at_ms, target_burn_rate_tpm, last_updated_ms)
        VALUES ('test:5h', 'go', 'test', 1000000, 900000, 90, ?, 1000, ?)`, now + 3600000, now);
      h.sql.exec(`INSERT INTO tbm_banked_tranches (tranche_id, pool_id, provider, allocated_tokens,
        remaining_tokens, granted_at_ms, expires_at_ms, updated_at_ms)
        VALUES ('test-bank', 'test:5h', 'go', 100000, 100000, ?, ?, ?)`, now, now + 3600000, now);
    });
    const pacing: any = await (await call('pacing')).json();
    expect(pacing.pools).toHaveLength(1);
    expect(pacing.routing_factors.go.pacing).toBeGreaterThan(1);
    expect(pacing.routing_factors.go.banked).toBeGreaterThan(1);
    const banked: any = await (await call('banked')).json();
    expect(banked.tranches[0]).toMatchObject({ tranche_id: 'test-bank', status: 'active' });
    const rewards: any = await (await call('rewards')).json();
    expect(Array.isArray(rewards.events)).toBe(true);
    expect(Array.isArray(rewards.rollups)).toBe(true);
  });
});
