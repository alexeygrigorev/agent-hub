import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { evaluateTbmAdmission } from '../src/quota_admission';
import { loadPools, loadProviderPacing } from '../src/pacing';

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
  it('exposes per-pool 24h/7d catalog values and keeps unsupported observations unknown with a reason', async () => {
    const now = Date.now();
    await inDO(stub(), (h: any) => {
      h.sql.exec("DELETE FROM tbm_reward_events WHERE event_id = 'session:one'");
      h.sql.exec("DELETE FROM tbm_session_tokens WHERE session_id = 'one'");
      h.sql.exec("DELETE FROM session_control_sessions WHERE id = 'one'");
      h.sql.exec(`INSERT INTO tbm_quota_pools
        (pool_id, provider, account, total_capacity_tokens, remaining_tokens, headroom_percentage,
         reset_at_ms, last_updated_ms, capacity_known, quota_unit)
        VALUES ('go:metrics:5h', 'go', 'metrics', 1000000, 700000, 70, ?, ?, 1, 'tokens')`, now + 3600000, now);
      h.sql.exec(`INSERT INTO tbm_reward_events
        (event_id, timestamp_ms, pool_id, intake_task_id, active_mode, swarm_concurrency,
         useful_work_reward, pacing_error_penalty, floor_breach_penalty, engine_mix_penalty,
         waste_at_reset_penalty, banked_waste_penalty, net_reward, token_efficiency_eta,
         tokens_known, efficiency_known, tokens_burned, session_id, event_kind, work_units)
        VALUES ('session:one', ?, 'go:metrics:5h', 'intake-one', 'high', 1,
         2, 0, 0, 0, 0, 0, 2, 20, 1, 1, 100000, 'one', 'session', 2)`, now - 1000);
      h.sql.exec(`INSERT INTO tbm_session_tokens (session_id, engine, total_tokens, observed_at_ms)
        VALUES ('one', 'opencode', 100000, ?)`, now - 1000);
      h.sql.exec(`INSERT INTO session_control_sessions
        (id, actor, request_key, request_json, engine, mode, state, created_at, updated_at)
        VALUES ('one', 'test', 'one', '{}', 'opencode', 'headless', 'stopped', ?, ?)`,
        new Date(now - 60000).toISOString(), new Date(now - 1000).toISOString());
      h.sql.exec(`INSERT INTO tbm_banked_tranches
        (tranche_id, pool_id, provider, allocated_tokens, remaining_tokens, granted_at_ms,
         expires_at_ms, status, banked_burn_rate_tpm, target_banked_burn_rate_tpm,
         expired_unused_tokens, updated_at_ms, quantity_known)
        VALUES ('tranche-one', 'go:metrics:5h', 'go', 500, 300, ?, ?, 'active', 0, 0, 0, ?, 1)`, now - 1000, now + 7200000, now);
    });
    const response = await call('metrics');
    expect(response.status).toBe(200);
    const data: any = await response.json();
    for (const window of ['24h', '7d']) {
      const pool = data.windows[window].pools.find((p: any) => p.pool_id === 'go:metrics:5h');
      expect(pool).toBeTruthy();
      expect(pool.metrics.useful_work_per_tokens.value).toBe(20);
      expect(pool.metrics.engine_mix.value).toEqual({ free: 1, paid: 0, grok: 0 });
      expect(pool.metrics.banked_remaining.value).toBe(300);
      expect(pool.metrics.time_to_expiry.value).toBeGreaterThan(0);
      expect(pool.metrics.burn_rate_vs_pacing_target.value).toBeNull();
      expect(pool.metrics.burn_rate_vs_pacing_target.reason).toContain('No historical pacing samples');
    }
  });

  it('scheduled controller persists state and real ingestion triggers floors', async () => {
    const now = Date.now();
    const response = await call('server', { captured_at: new Date(now - 1000).toISOString(), accounts: [{
      account: 'staged-low', provider: 'go', quota: { status: 'ok', error: null,
        observed_at: new Date(now - 1000).toISOString(), details: { limit_reached: false }, windows: {
          '5h': { present: true, rolling: false, percent_remaining: 3, reset_at: new Date(now + 3600000).toISOString() },
        } },
    }] });
    expect(response.status).toBe(200);
    await inDO(stub(), async (h: any) => {
      h.sql.exec('DROP TABLE IF EXISTS tbm_tick_state');
      await h.alarm();
      const state: any = h.sql.exec("SELECT * FROM tbm_controller_state WHERE pool_id='go:staged-low:5h'").one();
      expect(state.last_at_ms).toBeGreaterThanOrEqual(now);
      const pool = loadPools(h.sql).find(p => p.account === 'staged-low')!;
      expect(pool.last_updated_ms).toBe(now - 1000); // controller time never falsifies quota observation age
      expect(evaluateTbmAdmission({ pools: [pool], provider: 'go', nowMs: now })).toMatchObject({ ok: false, reason: 'hard_floor' });
      expect(loadProviderPacing(h.sql, now).get('go')?.halted).toBe(true);
      h.runTbmTick(state.last_at_ms + 60000);
      expect(h.sql.exec('SELECT last_tick_ms FROM tbm_tick_state WHERE id=1').one().last_tick_ms).toBe(state.last_at_ms);
      h.runTbmTick(state.last_at_ms + 240000);
      expect(h.sql.exec('SELECT last_tick_ms FROM tbm_tick_state WHERE id=1').one().last_tick_ms).toBe(state.last_at_ms + 240000);
    });
  });
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
