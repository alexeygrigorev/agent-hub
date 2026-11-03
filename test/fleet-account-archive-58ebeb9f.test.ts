import { beforeEach, describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { BankedResets } from '../src/banked-resets';
import type { Hub } from '../src/hub';
import { defaultEnv, doFetch } from './m1-focus-setup';
import { freshCpu } from './cpu-fixture';

const stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
const csrf = 'fleet-account-archive-test';
let cookie = '';
const at = (delta = 0) => new Date(Date.now() + delta).toISOString();
const observation = { status: 'ok', error: null, observed_at: at(-100), details: { limit_reached: false },
  windows: { weekly: { present: true, percent_remaining: 75, reset_at: at(3600000), rolling: false, error: null } } };

beforeEach(async () => {
  cookie = (await createSessionCookie(defaultEnv, { sub: 'archive-admin', email: 'admin@fake.local',
    isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await runInDurableObject(stub, (hub: Hub) => {
    hub.fleetService.init();
    hub.sql.exec("DELETE FROM audit WHERE action = 'fleet_account_archived' AND target = 'dummy-sync-5814'");
    hub.sql.exec('DELETE FROM fleet_account_archives');
    hub.sql.exec('DELETE FROM fleet_subscriptions');
    hub.sql.exec('DELETE FROM server_quota_snapshots');
    hub.sql.exec('DELETE FROM fleet_routes');
    hub.sql.exec('DELETE FROM fleet_accounts');
    hub.sql.exec('DELETE FROM fleet_launches');
    hub.sql.exec("DELETE FROM tbm_controller_state WHERE pool_id LIKE 'codex:%'");
    hub.sql.exec("DELETE FROM tbm_reward_events WHERE pool_id LIKE 'codex:%'");
    hub.sql.exec("DELETE FROM tbm_metrics_rollup WHERE pool_id LIKE 'codex:%'");
    hub.sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:%'");
    hub.sql.exec("DELETE FROM tbm_quota_pools WHERE account IN ('shared-codex','dummy-sync-5814')");
    hub.sql.exec('DELETE FROM resource_history');
    hub.sql.exec('DELETE FROM resource_history_entities');
    hub.sql.exec('DELETE FROM resource_history_meta');
    new BankedResets(hub.sql, hub.livenessService, () => Date.now());
    hub.sql.exec('DELETE FROM banked_reset_grants WHERE account IN (?,?)', 'shared-codex', 'dummy-sync-5814');
    hub.sql.exec('DELETE FROM banked_reset_accounts WHERE account IN (?,?)', 'shared-codex', 'dummy-sync-5814');
    hub.sql.exec('DELETE FROM banked_reset_notices');
    if (hub.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tbm_reset_credit_evidence'").toArray().length)
      hub.sql.exec("DELETE FROM tbm_reset_credit_evidence WHERE account='dummy-sync-5814'");
    const machine = `archive-${crypto.randomUUID().replaceAll('-', '')}`;
    const heartbeat = JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(at()),
      gate: { launch_allowed: true, at: at() } });
    hub.sql.exec(`INSERT INTO machines
      (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, machine, machine, machine, 'Linux', 'amd64', at(), at(), heartbeat, 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }));
    for (const [account, profile] of [['shared-codex', 'hub-shared-codex'], ['dummy-sync-5814', 'hub-dummy-sync-5814']]) {
      hub.sql.exec('INSERT INTO fleet_accounts VALUES (?, ?, 4, 0, 1)', account, 'codex');
      hub.sql.exec('INSERT INTO fleet_routes VALUES (?, ?, ?, ?)', machine, 'codex', account, profile);
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES (?, ?, ?, ?)', account, 'codex', at(), JSON.stringify(observation));
    }
    // Historical machine data is retained by archive; no active view or route may expose it.
    hub.sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', machine, 'dummy-sync-5814', 'codex', 'pro', at(), JSON.stringify(observation));
    const nowMs = Date.now();
    for (const account of ['shared-codex', 'dummy-sync-5814']) {
      const poolId = `codex:${account}:weekly`;
      hub.sql.exec(`INSERT INTO tbm_quota_pools
        (pool_id,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,reset_at_ms,
         cycle_ms,window_type,remaining_percent,last_updated_ms) VALUES (?, 'codex', ?, 1000, 750, 75, ?, 604800000, 'weekly', 75, ?)`,
        poolId, account, nowMs + 86400000, nowMs);
      hub.sql.exec('INSERT INTO tbm_controller_state(pool_id,updated_at_ms) VALUES (?,?)', poolId, nowMs);
      hub.sql.exec(`INSERT INTO tbm_banked_tranches
        (tranche_id,pool_id,provider,label,allocated_tokens,remaining_tokens,granted_at_ms,expires_at_ms,updated_at_ms)
        VALUES (?,?, 'codex','test grant',100,100,?,?,?)`, `grant:${account}`, poolId, nowMs, nowMs + 86400000, nowMs);
      hub.sql.exec(`INSERT INTO tbm_reward_events
        (event_id,timestamp_ms,pool_id,intake_task_id,active_mode,useful_work_reward,pacing_error_penalty,
         floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,banked_waste_penalty,net_reward,token_efficiency_eta)
        VALUES (?,?,?,'fixture','high',1,0,0,0,0,0,1,1)`, `reward:${account}`, nowMs, poolId);
      hub.sql.exec(`INSERT INTO tbm_metrics_rollup
        (rollup_id,window_type,start_time_ms,end_time_ms,pool_id,avg_burn_rate_vs_target,total_tokens_burned,
         useful_work_units,token_efficiency_eta,engine_mix_free_share,engine_mix_paid_share,engine_mix_grok_share,
         admission_rejection_rate,no_eligible_route_rate,avg_time_to_outcome_seconds)
        VALUES (?,'1h',?,?,?,1,1,1,1,1,0,0,0,0,1)`, `rollup:${account}`, nowMs - 3600000, nowMs, poolId);
      hub.sql.exec(`INSERT INTO banked_reset_grants
        (id,account,provider,label,expires_at,source,active,observed_at,recorded_by)
        VALUES (?,?, 'codex','fixture grant',?,'quota',1,?,'fixture')`, `banked:${account}`, account, nowMs + 86400000, nowMs);
      hub.sql.exec("INSERT INTO banked_reset_accounts VALUES (?,?,'quota')", account, nowMs);
      hub.sql.exec('INSERT INTO banked_reset_notices VALUES (?,?,?,?)', `banked:${account}`, 'early', `notice:${account}`, nowMs);
    }
  });
});

async function getJson(path: string) {
  const response = await doFetch(new Request(`http://example.com${path}`, { headers: { Cookie: cookie } }));
  return { response, body: await response.json() as any };
}

describe('audited fleet account archive (intake 58ebeb9f)', () => {
  it('hides dummy-sync-5814 from views and routing while retaining the audit trail', async () => {
    const archived = await doFetch(new Request('http://example.com/api/fleet/accounts/dummy-sync-5814/archive', {
      method: 'POST', headers: { Cookie: cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ reason: 'Remove test account created by account-sync test 5814e109' }),
    }));
    expect(archived.status).toBe(200);
    expect(await archived.json()).toMatchObject({ status: 'archived', account: 'dummy-sync-5814', changed: true, removed_routes: 1 });

    const fleet = await getJson('/api/fleet');
    expect(fleet.response.status).toBe(200);
    expect(fleet.body.accounts.map((row: any) => row.label)).not.toContain('dummy-sync-5814');
    expect(fleet.body.routes.map((row: any) => row.account)).not.toContain('dummy-sync-5814');

    const quota = await getJson('/api/quota?view=fleet');
    expect(quota.response.status).toBe(200);
    expect(quota.body.pools.map((row: any) => row.account)).not.toContain('dummy-sync-5814');

    const capacityHistory = await getJson('/api/fleet/accounts/dummy-sync-5814/capacity-history?limit=1');
    expect(capacityHistory.response.status).toBe(404);
    const limits = await getJson('/api/fleet/limits');
    expect(limits.response.status).toBe(200);
    expect(JSON.stringify(limits.body)).not.toContain('dummy-sync-5814');

    const pacing = await getJson('/api/quota/pacing');
    expect(pacing.response.status).toBe(200);
    expect(pacing.body.pools.map((row: any) => row.account)).toEqual(['shared-codex']);
    expect(pacing.body.controller.map((row: any) => row.pool_id)).not.toContain('codex:dummy-sync-5814:weekly');
    expect(Object.keys(pacing.body.routing_factors)).not.toContain('codex:dummy-sync-5814');
    const banked = await getJson('/api/quota/banked');
    expect(JSON.stringify(banked.body)).not.toContain('dummy-sync-5814');
    const rewards = await getJson('/api/quota/rewards');
    expect(JSON.stringify(rewards.body)).not.toContain('dummy-sync-5814');
    const resets = await getJson('/api/quota/resets');
    expect(JSON.stringify(resets.body)).not.toContain('dummy-sync-5814');
    await getJson('/api/quota/reset-credits'); // creates the evidence schema on this isolated DO
    await runInDurableObject(stub, (hub: Hub) => {
      hub.sql.exec('INSERT INTO tbm_reset_credit_evidence VALUES(?,?,?,?,?,NULL,NULL,?)', 'earned-reset:archived',
        'dummy-sync-5814', 'fixture-credit', 'fixture-provider-account', JSON.stringify({ account: 'dummy-sync-5814' }), Date.now());
      const atMs = Date.now() - 1000;
      hub.sql.exec('INSERT OR REPLACE INTO resource_history VALUES (?,?)', Math.floor(atMs / 60000) * 60000, JSON.stringify([
        { kind: 'quota', entity: 'dummy-sync-5814', machine_id: '', at: atMs, values: { remaining_percent: 75 }, source: 'fixture' },
        { kind: 'quota', entity: 'shared-codex', machine_id: '', at: atMs, values: { remaining_percent: 75 }, source: 'fixture' },
      ]));
      expect(hub.quotaStore.getWorkerEnginesView().engines.map((row: any) => row.account)).not.toContain('dummy-sync-5814');
    });
    const resetCredits = await getJson('/api/quota/reset-credits');
    expect(JSON.stringify(resetCredits.body)).not.toContain('dummy-sync-5814');
    const history = await getJson('/api/monitoring/history?range=24h');
    expect(history.response.status).toBe(200);
    expect(history.body.series.map((row: any) => row.entity)).not.toContain('dummy-sync-5814');

    const restore = await doFetch(new Request('http://example.com/api/fleet/accounts', {
      method: 'PUT', headers: { Cookie: cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ accounts: [{ label: 'dummy-sync-5814', provider: 'codex', max_concurrency: 4,
        reserve_percent: 15, enabled: true }], routes: [] }),
    }));
    expect(restore.status).toBe(409);
    expect(await restore.json()).toMatchObject({ error: 'account_archived' });

    await runInDurableObject(stub, (hub: Hub) => {
      const audit = hub.sql.exec("SELECT detail FROM audit WHERE action = 'fleet_account_archived' AND target = 'dummy-sync-5814'").toArray() as any[];
      expect(audit).toHaveLength(1);
      expect(JSON.parse(audit[0].detail).reason).toContain('5814e109');
      expect(hub.sql.exec("SELECT account FROM fleet_routes WHERE account = 'dummy-sync-5814'").toArray()).toHaveLength(0);
      expect(hub.sql.exec("SELECT account FROM fleet_subscriptions WHERE account = 'dummy-sync-5814'").toArray()).toHaveLength(1);
      const plan: any = hub.fleetService.plan({ workspace: 'demo', prompt: 'work', idempotency_key: crypto.randomUUID(), engine: 'codex' });
      expect(plan.candidates.map((candidate: any) => candidate.account)).not.toContain('dummy-sync-5814');
      expect(plan.selected?.account).toBe('shared-codex');
    });
  });

  it('requires an admin and a non-empty audit reason', async () => {
    const forbidden = await doFetch(new Request('http://example.com/api/fleet/accounts/dummy-sync-5814/archive', {
      method: 'POST', headers: { Cookie: '' , 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ reason: 'clean up test account' }),
    }));
    expect(forbidden.status).toBe(401);
    const invalid = await doFetch(new Request('http://example.com/api/fleet/accounts/dummy-sync-5814/archive', {
      method: 'POST', headers: { Cookie: cookie, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ reason: ' ' }),
    }));
    expect(invalid.status).toBe(400);
  });
});
