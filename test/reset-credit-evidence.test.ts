import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { handleResetCreditEvidence } from '../src/reset-credit-evidence';
import { loadProviderPacing } from '../src/pacing';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
let now: number;
const request = (body?: any) => new Request('https://hub.test/api/quota/reset-credits', {
  method: body ? 'POST' : 'GET', ...(body ? { body: JSON.stringify(body) } : {}),
});
const call = (body?: any, at = now): Promise<Response> => inDO(stub(), (h: any) => handleResetCreditEvidence(request(body), h.sql, at));
const input = () => ({ action: 'prepare', account: 'credit-test', credit_id: 'credit-a',
  provider_account_id: 'provider-a', expires_at_ms: now + 3600000, observed_at_ms: now, available_count: 2, provider_used_percent: 90 });
async function prepared() { return (await call(input())).json() as Promise<any>; }
function receipt(e: any, overrides: any = {}) {
  return { action: 'receipt', event_id: e.event_id, receipt: {
    account_id: 'provider-a', credit_id: 'credit-a', idempotency_key: e.decision.idempotency_key,
    outcome: 'reset', before_at_ms: now, after_at_ms: now + 1000, before_count: 2, after_count: 1,
    selected_credit_state: 'absent', sha256: 'a'.repeat(64), after_usage_allowed: true,
    after_windows: [{ bucket: 'codex', window: 'primary', used_percent: 0, reset_at_ms: now + 86400000, window_minutes: 10080 }], ...overrides } };
}

beforeEach(async () => {
  now = Date.now();
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    h.sql.exec('DROP TABLE IF EXISTS tbm_reset_credit_evidence');
    h.sql.exec('DELETE FROM tbm_banked_tranches');
    h.sql.exec('DELETE FROM tbm_quota_pools');
    h.sql.exec("INSERT OR REPLACE INTO fleet_accounts VALUES('credit-test','codex',1,15,1)");
    h.sql.exec("DELETE FROM fleet_engine_health WHERE engine='codex'");
    h.sql.exec('CREATE TABLE IF NOT EXISTS tbm_tick_state (id INTEGER PRIMARY KEY,last_tick_ms INTEGER NOT NULL,summary_json TEXT NOT NULL)');
    h.sql.exec("INSERT OR REPLACE INTO tbm_tick_state VALUES(1,?,'{}')", now);
    h.sql.exec(`INSERT INTO tbm_quota_pools (pool_id,provider,account,total_capacity_tokens,remaining_tokens,
      headroom_percentage,remaining_percent,reset_at_ms,last_updated_ms,capacity_known,quota_unit,cycle_ms)
      VALUES('codex:credit-test:7d','codex','credit-test',0,0,20,20,?,?,0,'percent',604800000)`, now + 86400000, now);
    h.sql.exec(`INSERT INTO tbm_banked_tranches (tranche_id,pool_id,provider,label,allocated_tokens,remaining_tokens,
      granted_at_ms,expires_at_ms,updated_at_ms,quantity_known) VALUES('grant-a','codex:credit-test:7d','codex','Full reset',0,0,?,?,?,0)`, now - 86400000, now + 3600000, now);
  });
});

describe('earned-reset genuine evidence correlation (offline fixtures)', () => {
  it('captures actual stored tick/EDF state with stable server key and explicit attestation boundary', async () => {
    const e = await prepared();
    expect(e.decision_event_id).toBe(e.event_id + ':decision');
    expect(e.decision).toMatchObject({ controller_tick_ms: now, tranche_id: 'grant-a', reason: 'expiring_earned_reset_edf' });
    expect(e.decision.routing_factors.banked).toBeGreaterThan(1);
    expect(e.authority).toBe('operator_attestation_with_hub_state');
    expect(e.ac2_accepted).toBe(false);
    expect(await (await call(input())).json()).toEqual(e);
    expect((await call({ ...input(), account: 'renamed-alias' })).status).toBe(409);
    const rows: any = await (await call()).json();
    expect(rows.events).toHaveLength(1); // same physical credit cannot acquire a new key under another alias
  });
  it('prepares genuine EDF reset need at the reserve boundary without inventing a runnable factor', async () => {
    // rules.tbm_floor_percent overrides codex stored floors to 0 (intake 4f6cf562), so the
    // policy halt boundary for codex pools is depletion; the attested native >=85% used
    // threshold below is provider-side and unchanged.
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET hard_floor_percent=0,remaining_percent=0,headroom_percentage=0'));
    const before = await inDO(stub(), (h: any) => loadProviderPacing(h.sql, now).get('codex:credit-test'));
    expect(before).toMatchObject({ halted: true });
    expect((await call({ ...input(), provider_used_percent: 84 })).status).toBe(400);
    const e: any = await (await call({ ...input(), provider_used_percent: 85 })).json();
    expect(e.decision).toMatchObject({ reset_needed_basis: 'native_quota_at_or_below_reserve', routing_eligibility_unchanged: true });
    expect(e.decision.routing_factors).toEqual(before);
    expect(await inDO(stub(), (h: any) => loadProviderPacing(h.sql, now).get('codex:credit-test'))).toEqual(before);
  });
  it('does not prepare across actual disabled-account, global health or explicit pool kill policies', async () => {
    await inDO(stub(), (h: any) => h.sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE label='credit-test'"));
    expect((await call(input())).status).toBe(409);
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE fleet_accounts SET enabled=1 WHERE label='credit-test'");
      h.sql.exec("INSERT INTO fleet_engine_health(engine,machine_id,status,reason,updated_at) VALUES('codex','','unavailable','engine_auth_expired',?)", new Date(now).toISOString());
    });
    expect((await call(input())).status).toBe(409);
    await inDO(stub(), (h: any) => {
      h.sql.exec("DELETE FROM fleet_engine_health WHERE engine='codex'");
      h.sql.exec('UPDATE tbm_quota_pools SET kill_switch_active=1');
    });
    expect((await call(input())).status).toBe(409);
  });
  it('fails closed for stale provider facts, stale tick and stale ingested quota', async () => {
    expect((await call({ ...input(), observed_at_ms: now - 60001 })).status).toBe(400);
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_tick_state SET last_tick_ms=?', now - 300001));
    expect((await call(input())).status).toBe(409);
    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE tbm_tick_state SET last_tick_ms=?', now);
      h.sql.exec('UPDATE tbm_quota_pools SET last_updated_ms=?', now - 180001);
    });
    expect((await call(input())).status).toBe(409);
  });
  it('refuses unmapped expiry and an emergency global kill switch', async () => {
    expect((await call({ ...input(), expires_at_ms: now + 7200000 })).status).toBe(409);
    await inDO(stub(), (h: any) => h.sql.exec(`INSERT INTO tbm_quota_pools
      (pool_id,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,reset_at_ms,last_updated_ms,kill_switch_active)
      VALUES('*','*','*',1,1,100,?,?,1)`, now + 86400000, now));
    expect((await call(input())).status).toBe(409);
  });
  it('refuses a missing controller tick and invalidates readiness after a global hold', async () => {
    const e = await prepared();
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET last_updated_ms=?', now - 180001));
    const r: any = await (await call()).json();
    expect(r.events[0]).toMatchObject({ event_id: e.event_id, consumption_ready: false });
    await inDO(stub(), (h: any) => {
      h.sql.exec('DELETE FROM tbm_reset_credit_evidence');
      h.sql.exec('DROP TABLE tbm_tick_state');
    });
    expect((await call(input())).status).toBe(409);
  });
  it('binds a receipt to exact account, credit, key and chronology; repeat is idempotent', async () => {
    const e = await prepared();
    for (const change of [{ account_id: 'other' }, { credit_id: 'other' }, { idempotency_key: 'other' },
      { after_at_ms: now - 1 }, { before_at_ms: now - 2000 }]) {
      expect((await call(receipt(e, change))).status).toBe(400);
    }
    const r = receipt(e);
    const recorded: any = await (await call(r)).json();
    expect(recorded.receipt_event_id).toBe(e.event_id + ':receipt');
    expect(recorded.ac2_accepted).toBe(false);
    expect(await (await call(r)).json()).toEqual(recorded);
    expect((await call(receipt(e, { sha256: 'b'.repeat(64) }))).status).toBe(409);
  });
  it('revalidates current policy and EDF state before granting consumption readiness', async () => {
    await prepared();
    const ready = async () => (await (await call()).json() as any).events[0].consumption_ready;
    expect(await ready()).toBe(true);
    await inDO(stub(), (h: any) => h.sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE label='credit-test'"));
    expect(await ready()).toBe(false);
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE fleet_accounts SET enabled=1 WHERE label='credit-test'");
      h.sql.exec('UPDATE tbm_quota_pools SET kill_switch_active=1');
    });
    expect(await ready()).toBe(false);
    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE tbm_quota_pools SET kill_switch_active=0');
      h.sql.exec("UPDATE tbm_banked_tranches SET status='expired'");
    });
    expect(await ready()).toBe(false);
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE tbm_banked_tranches SET status='active'");
      h.sql.exec('UPDATE tbm_tick_state SET last_tick_ms=?', now - 300001);
    });
    expect(await ready()).toBe(false);
  });
  it('preserves no-op provider outcome without fabricating a post-read', async () => {
    const e = await prepared();
    const recorded: any = await (await call(receipt(e, { outcome: 'nothingToReset', after_at_ms: null,
      after_count: null, selected_credit_state: 'unknown', after_windows: [], after_usage_allowed: null }))).json();
    expect(recorded.receipt.outcome).toBe('nothingToReset');
    expect((await call({ action: 'link-work', event_id: e.event_id, routing_job_id: 'job-a' })).status).toBe(409);
  });
  it('does not equate an ordinary quota drop with coupon consumption', async () => {
    const e = await prepared();
    await call(receipt(e, { after_count: 2 }));
    const r = await call({ action: 'link-work', event_id: e.event_id, routing_job_id: 'job-a' });
    expect(await r.json()).toMatchObject({ error: 'coupon_consumption_unproven' });
  });
  it('links existing unforced completed routing, reward and fresh ingestion without changing their facts', async () => {
    const e = await prepared();
    await call(receipt(e));
    await inDO(stub(), (h: any) => {
      const at = new Date(now + 2000).toISOString();
      h.sql.exec(`INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,machine_id,
        engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES('credit-launch','test','agent','work','credit-launch','{}','machine-a','codex','credit-test','headless','repo','private','exited',?,?)`, at, at);
      h.sql.exec(`INSERT INTO routing_jobs(id,actor,request_key,request_json,launch_id,state,created_at,updated_at,selection_json)
        VALUES('job-a','test','credit-job','{}','credit-launch','done',?,?,'{"id":"codex","factors":{"banked":3}}')`, at, at);
      h.sql.exec(`INSERT INTO tbm_reward_events(event_id,timestamp_ms,pool_id,intake_task_id,active_mode,swarm_concurrency,
        useful_work_reward,pacing_error_penalty,floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,
        banked_waste_penalty,net_reward,token_efficiency_eta,work_units)
        VALUES('session:job-a',?,'codex:credit-test:7d','routing:job-a','high',1,10,0,0,0,0,0,10,0,10)`, now + 2000);
    });
    const body = { action: 'link-work', event_id: e.event_id, routing_job_id: 'job-a' };
    expect((await call(body, now + 3000)).status).toBe(409); // ingestion predates reset
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET last_updated_ms=?,remaining_percent=99', now + 2000));
    await inDO(stub(), (h: any) => h.sql.exec("UPDATE fleet_launches SET account='other' WHERE id='credit-launch'"));
    expect((await call(body, now + 3000)).status).toBe(409);
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE fleet_launches SET account='credit-test' WHERE id='credit-launch'");
      h.sql.exec("UPDATE routing_jobs SET request_json=? WHERE id='job-a'", JSON.stringify({ model: 'forced' }));
    });
    expect((await call(body, now + 3000)).status).toBe(409);
    await inDO(stub(), (h: any) => h.sql.exec("UPDATE routing_jobs SET request_json='{}' WHERE id='job-a'"));
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET remaining_percent=100'));
    expect(await (await call(body, now + 3000)).json()).toMatchObject({ error: 'same_window_post_work_drawdown_required' });
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET remaining_percent=99,reset_at_ms=?', now + 90000000));
    expect(await (await call(body, now + 3000)).json()).toMatchObject({ error: 'same_window_post_work_drawdown_required' });
    await inDO(stub(), (h: any) => h.sql.exec('UPDATE tbm_quota_pools SET reset_at_ms=?', now + 86400000));
    const r: any = await (await call(body, now + 3000)).json();
    expect(r.work).toMatchObject({ routing_job_id: 'job-a', reward: { event_id: 'session:job-a', work_units: 10 },
      pool: { remaining_percent: 99 }, quota_drawdown: { used_delta_percentage_points: 1 }, no_starvation_verified: false });
    expect(r.ac2_accepted).toBe(false);
    expect(await (await call(body, now + 4000)).json()).toEqual(r);
  });
  it('mount requires admin and existing cookie CSRF even for an attestation', async () => {
    expect((await SELF.fetch(request())).status).toBe(401);
    const cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local',
      isAdmin: true, csrfToken: 'credit-csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
    expect((await SELF.fetch(new Request('https://hub.test/api/quota/reset-credits', {
      method: 'POST', headers: { Cookie: cookie }, body: JSON.stringify(input()),
    }))).status).toBe(403);
  });
  it('bounds stream size before JSON parsing', async () => {
    const r = await inDO(stub(), (h: any) => handleResetCreditEvidence(new Request('https://hub.test/api/quota/reset-credits', {
      method: 'POST', body: 'x'.repeat(16385),
    }), h.sql, now));
    expect(r.status).toBe(413);
  });
});
