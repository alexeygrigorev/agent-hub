import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { ProvisioningService } from '../src/provisioning';
import { QuotaStore } from '../src/quota';
import { evaluateLaunchQuota } from '../src/quota_admission';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const refusal = { engine: 'codex', account: 'shared-codex', profile: 'hub-shared-codex',
  mode: 'interactive', reason: 'codex_quota_gate_codex_reserve_required', observed_at: iso() };

async function fixture(refusals: any[], fn: (service: FleetService, sql: any) => void | Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('managed-admission-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    service.init();
    hub.sql.exec('INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      'host', 'host', 'host', iso(), iso(), JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80,
        cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }), 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['interactive', 'headless'], workspaces: ['demo'],
        cpu_count: 8, fleet_route_rejections: refusals }));
    const labels = ['shared-codex', 'codex-orin'];
    const req = new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
      accounts: labels.map(label => ({ label, provider: 'codex', max_concurrency: 10, reserve_percent: 15, enabled: true })),
      routes: labels.map(account => ({ machine_id: 'host', engine: 'codex', account, profile: 'hub-' + account })),
    }) });
    expect((await service.handle(req, admin)).status).toBe(200);
    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false },
      windows: { weekly: { present: true, percent_remaining: 99, reset_at: iso(86400000) } } };
    const subs = new Request('https://hub.test/api/fleet/subscriptions', { method: 'PUT', body: JSON.stringify({
      subscriptions: labels.map(account => ({ service: 'codex', account, plan: 'pro', quota })),
    }) });
    expect((await service.handle(subs, { caller: { machineId: 'host', kind: 'host', actor: 'machine:host' }, scopes: ['host'] })).status).toBe(200);
    await fn(service, hub.sql);
  });
}

describe('quota probe failure placement (b3749cbb)', () => {
  it('admits an installed managed account with unknown quota without waiving installation or disabled-account checks', async () => {
    await fixture([], async (fleet, sql) => {
      const provision = new ProvisioningService(sql, fn => fn(), {} as any, fleet, () => true);
      provision.init();
      sql.exec("DELETE FROM fleet_subscriptions");
      sql.exec("INSERT INTO engine_bundles VALUES ('shared-codex','codex','1','rev','unused')");
      sql.exec("INSERT INTO engine_installations VALUES ('host','shared-codex','rev')");
      const admit = async () => (await provision.handle(new Request('https://hub.test/api/engines/admission', {method:'POST', body: JSON.stringify({label:'shared-codex'})}), {caller:{kind:'host', machineId:'host'},scopes:['host']})).json() as Promise<any>;
      expect((await admit()).launch_allowed).toBe(true);
      sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE label='shared-codex'");
      expect((await admit()).launch_allowed).toBe(false);
      sql.exec("UPDATE fleet_accounts SET enabled=1 WHERE label='shared-codex'");
      sql.exec("DELETE FROM engine_installations");
      expect((await admit()).launch_allowed).toBe(false);
    });
  });
  it('does not let a sibling unknown observation poison a healthy shared-account pool', async () => {
    await fixture([], (service, sql) => {
      sql.exec("INSERT INTO fleet_routes VALUES ('sibling','codex','shared-codex','hub-shared-codex')");
      sql.exec("INSERT INTO fleet_subscriptions VALUES ('sibling','shared-codex','codex',NULL,?,?)", iso(), JSON.stringify({status: 'unknown', observed_at: iso(), details: {limit_reached: null}, windows: {}}));
      const view = new QuotaStore(sql, undefined, undefined, () => now).getFleetView({host_id:null,provider:null,engine:null,account:null}, now);
      const pool = view.body.pools.find((p: any) => p.account === 'shared-codex');
      expect(pool.state).toBe('fresh');
      expect(pool.remaining_percent).toBe(99);
      sql.exec("UPDATE fleet_subscriptions SET quota_json=? WHERE machine_id='sibling'", JSON.stringify({status: 'unknown', observed_at: iso(), details: {limit_reached: true}, windows: {}}));
      expect(new QuotaStore(sql, undefined, undefined, () => now).getFleetView({host_id:null,provider:null,engine:null,account:null}, now).body.pools.find((p: any) => p.account === 'shared-codex').state).toBe('exhausted');
    });
  });
  // A probe that crashed carries no verdict about the credential: fail open.
  it('does not hold healthy routes for quota_check_failed', async () => {
    await fixture([{ ...refusal, reason: 'codex_quota_gate_quota_check_failed' }], service => {
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', prompt: 'test' });
      expect(plan.selected).toBeTruthy();
      expect(plan.candidates?.some((r: any) => r.account === 'shared-codex')).toBe(true);
    });
  });
  // Intake 5814e109 I5: a host that rejected this exact route is never offered it.
  it.each(['quota_unavailable', 'stale_window_evidence'])('holds a route the host rejected with %s', async reason => {
    await fixture([{ ...refusal, reason: 'codex_quota_gate_' + reason }], service => {
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', prompt: 'test' });
      expect(plan.candidates?.some((r: any) => r.account === 'shared-codex')).toBe(false);
      expect(plan.rejected).toContainEqual(expect.objectContaining({ reason: 'codex_quota_gate_' + reason }));
    });
  });
  it('prefers an admitted sibling when a probe crashes without a credential verdict (798f0f8c)', async () => {
    for (const account of ['codex-orin', 'shared-codex']) {
      await fixture([{ ...refusal, account, profile: 'hub-' + account, reason: 'codex_quota_gate_quota_check_failed' }], service => {
        const plan = service.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', prompt: 'test' });
        expect(plan.selected?.account).not.toBe(account);
        expect(plan.candidates?.some((r: any) => r.account === account)).toBe(true);
      });
    }
  });
  it('allows an unknown account with a warning and an unknown balance', async () => {
    await fixture([], (service, sql) => {
      sql.exec('DELETE FROM fleet_subscriptions');
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', prompt: 'test' });
      expect(plan.selected?.quota_warning).toBe('quota_probe_unavailable');
      expect(Number.isFinite(plan.selected?.score)).toBe(true);
      expect(service.accountStatus(sql.exec("SELECT * FROM fleet_accounts LIMIT 1").one()).remaining_percent).toBeNull();
    });
  });
  it('falls back from exhausted Codex to a fresh engine in the same tier and reports every rejected engine', async () => {
    await fixture([], (service, sql) => {
      sql.exec("UPDATE fleet_subscriptions SET quota_json = ?", JSON.stringify({status: 'ok', observed_at: iso(), details: {limit_reached: true}, windows: {}}));
      sql.exec("INSERT INTO fleet_accounts VALUES ('shared-go','go',10,0,1)");
      sql.exec("INSERT INTO fleet_routes VALUES ('host','opencode','shared-go',NULL)");
      sql.exec("INSERT INTO fleet_subscriptions (machine_id,account,service,plan,quota_json,observed_at) VALUES ('host','shared-go','go',NULL,?,?)", JSON.stringify({status: 'ok', observed_at: iso(), details: {limit_reached: false}, windows: {weekly: {present: true, percent_remaining: 60, reset_at: iso(86400000)}}}), iso());
      const caps = JSON.parse(sql.exec("SELECT capabilities FROM machines WHERE id='host'").one().capabilities);
      sql.exec("UPDATE machines SET capabilities=?", JSON.stringify({...caps, workspaces: ['/srv/repos'], engines: ['codex','opencode']}));
      const plan = service.plan({ workspace: 'demo', mode: 'interactive', engine: 'codex', tier: 'daily', prompt: 'test' });
      expect(plan.selected?.engine).toBe('opencode');
      expect(plan.selected?.selection.id).toBe('muse');
      expect(plan.request?.workspace).toBe('/srv/repos/demo');
      expect(plan.request?.engine).toBe('codex');
      expect(plan.rejected).toContainEqual(expect.objectContaining({engine: 'codex', reason: 'account_blocked'}));
      // Deep tier cannot silently degrade to a daily model.
      const deep = service.plan({workspace: 'demo', mode: 'interactive', engine: 'astra', tier: 'deep', prompt: 'test'});
      expect(deep.selected).toBeNull();
      expect(deep.rejected).toContainEqual(expect.objectContaining({engine: 'opencode', reason: 'tier_unavailable'}));
    });
  });
  it('falls back a Codex goal to a fresh same-tier zcodex goal route', async () => {
    await fixture([], (service, sql) => {
      sql.exec("UPDATE fleet_subscriptions SET quota_json=?", JSON.stringify({status:'ok',observed_at:iso(),details:{limit_reached:true},windows:{}}));
      sql.exec("INSERT INTO fleet_accounts VALUES ('shared-zai','zai',10,0,1)");
      sql.exec("INSERT INTO fleet_routes VALUES ('host','zcodex','shared-zai',NULL)");
      sql.exec("INSERT INTO fleet_subscriptions VALUES ('host','shared-zai','zai',NULL,?,?)", iso(), JSON.stringify({status:'ok',observed_at:iso(),details:{limit_reached:false},windows:{weekly:{present:true,percent_remaining:60,reset_at:iso(86400000)}}}));
      const caps=JSON.parse(sql.exec("SELECT capabilities FROM machines WHERE id='host'").one().capabilities);
      sql.exec("UPDATE machines SET capabilities=?", JSON.stringify({...caps,engines:['codex','zcodex'],fleet_goal_routes:[
        {engine:'codex',account:'shared-codex',profile:'hub-shared-codex'},
        {engine:'codex',account:'codex-orin',profile:'hub-codex-orin'},
        {engine:'zcodex',account:'shared-zai',profile:null}]}));
      const plan=service.plan({workspace:'demo',mode:'interactive',with_goal:true,engine:'codex',tier:'daily',prompt:'work'});
      expect(plan.selected?.engine).toBe('zcodex');
      expect(plan.selected?.selection.provider).toBe('zai');
      expect(plan.request?.with_goal).toBe(true);
    });
  });
  it.each([null, {status: 'error', windows: {}, details: {limit_reached: null}}])('warns for failed probes without inventing quota', quota => {
    expect(evaluateLaunchQuota({provider: 'codex', quota, subscriptionObservedAt: null, nowMs: now, reserves: {codex: 0}})).toMatchObject({ok: true, remaining: null, reason: expect.stringContaining('quota_probe_warning:')});
  });
  it.each([{details: {limit_reached: true}}, {windows: {weekly: {present: true, percent_remaining: 0}}}, {details: {cooldown_until: iso(60000)}}])('preserves positive exhaustion and backoff even in a failed probe', quota => {
    expect(evaluateLaunchQuota({provider: 'codex', quota, subscriptionObservedAt: null, nowMs: now, reserves: {codex: 0}}).ok).toBe(false);
  });
});
