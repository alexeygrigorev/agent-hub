import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { accountPlanEndDate, accountPlanEndMs, accountPlanExpired, planEndBurnMode, planEndExpiryMs } from '../src/model-routing';
import { computeEngineDecision, planEndUrgency } from '../src/pacing';
import { tickTbm } from '../src/tbm-controller';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'plan-end-admin', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { machineId: 'plan-host', kind: 'host', actor: 'machine:plan-host' }, scopes: ['host'] };
const request = (path: string, data?: any, method = 'POST') => new Request('https://hub.test/api/fleet' + path, {
  method, body: data === undefined ? undefined : JSON.stringify(data),
});
const iso = (now: number, offset = 0) => new Date(now + offset).toISOString();
const quota = (now: number, remaining: number) => ({ status: 'ok', observed_at: iso(now, -1000),
  details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: remaining,
    reset_at: iso(now, 86400000), rolling: false } } });
const intent = () => ({ workspace: 'demo', prompt: 'private test goal', idempotency_key: crypto.randomUUID() });

async function fleetAt(now: number, accounts: string[], check: (service: FleetService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('plan-end-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    service.init();
    const sql = hub.sql;
    const heartbeat = { mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso(now)),
      gate: { launch_allowed: true, at: iso(now) } };
    sql.exec(`INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities)
      VALUES ('plan-host','plan-host','plan-host',?,?,?,0,?)`, iso(now), iso(now), JSON.stringify(heartbeat),
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec(`INSERT INTO host_quota_snapshots
      (host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json)
      VALUES ('plan-host','1.0',?,?,?,?,'ok',NULL,?)`, iso(now), now, iso(now), now,
      JSON.stringify({ codex: quota(now, 80), 'codex-orin': quota(now, 80) }));
    const config = { accounts: accounts.map(label => ({ label, provider: 'codex', max_concurrency: 8,
      reserve_percent: 0, enabled: true })), routes: accounts.map(account => ({ machine_id: 'plan-host',
      engine: 'codex', account, profile: null })) };
    expect((await service.handle(request('/accounts', config, 'PUT'), admin)).status).toBe(200);
    expect((await service.handle(request('/subscriptions', { subscriptions: accounts.map(account => ({
      service: 'codex', account, plan: 'pro', quota: quota(now, 80),
    })) }, 'PUT'), host)).status).toBe(200);
    tickTbm(sql, now);
    await check(service, sql);
  });
}

describe('Codex plan-end policy', () => {
  it('stores inclusive account end dates and enables reset burn only inside its configured horizon', () => {
    const expires = Date.parse('2026-11-14T00:00:00.000Z');
    expect(accountPlanEndDate('codex-orin')).toBe('2026-11-13');
    expect(accountPlanEndMs('codex-orin')).toBe(expires);
    expect(planEndExpiryMs('2026-02-30')).toBeNull();
    expect(accountPlanExpired('codex-orin', expires - 1)).toBe(false);
    expect(accountPlanExpired('codex-orin', expires)).toBe(true);
    expect(accountPlanExpired('shared-codex', expires)).toBe(false);
    expect(planEndBurnMode('codex-orin', Date.parse('2026-11-03T00:00:00.000Z'))).toBe(expires);
    expect(planEndBurnMode('codex-orin', Date.parse('2026-10-29T00:00:00.000Z'))).toBeNull();
    expect(planEndBurnMode('codex-orin', expires)).toBeNull();
  });

  it('scores burnable backlog on codex-orin ahead of an equal shared Codex account', async () => {
    const now = Date.parse('2026-11-03T12:00:00.000Z');
    await fleetAt(now, ['shared-codex', 'codex-orin'], async service => {
      const plan: any = service.plan(intent());
      expect(plan.selected?.account).toBe('codex-orin');
      expect(plan.selected.decision.factors.plan_end_factor).toBeGreaterThan(1);
      expect(plan.selected.decision_reason).toContain('plan_end=2026-11-13');
    });
  });

  it('keeps interactive routing neutral while expiry-aware work receives the plan-end factor', () => {
    const now = Date.parse('2026-11-03T12:00:00.000Z');
    const base: any = { machine: 'plan-host', engine: 'codex', account: 'codex-orin', provider: 'codex',
      model: { id: 'luna', model: 'gpt-6-luna' }, tier: 'daily', quota: null, headroomPercent: 80, nowMs: now,
      cpuLoad: 0, pendingLaunches: 0, familyLoad: 0, accountActive: 0, accountMaxConcurrency: 8,
      recentAccountFailures: 0, degraded: false, tbmWeight: 1, planEndMs: accountPlanEndMs('codex-orin') };
    const batch = computeEngineDecision(base);
    const interactive = computeEngineDecision({ ...base, expiryForecast: false });
    expect(planEndUrgency(0.8, base.planEndMs, now)).toBeCloseTo(3);
    expect(batch.factors.plan_end_factor).toBeGreaterThan(1);
    expect(interactive.factors.plan_end_factor).toBe(1);
  });

  it('marks codex-orin expired and refuses placement at the first instant after 13 November', async () => {
    const afterEnd = Date.parse('2026-11-14T00:00:00.000Z');
    await fleetAt(afterEnd, ['codex-orin'], async (service, sql) => {
      expect(service.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='codex-orin'").one()))
        .toMatchObject({ status: 'expired', plan_end: '2026-11-13', placeable_slots: 0 });
      const plan: any = service.plan(intent());
      expect(plan.selected).toBeNull();
      expect(plan.rejected).toContainEqual({ machine: 'plan-host', engine: 'codex', reason: 'account_expired' });
    });
  });
});
