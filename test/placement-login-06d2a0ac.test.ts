import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, isCapacityOnlyHold } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

// Intake 06d2a0ac: auto-placement chose the logged-out test account
// dummy-sync-5814 (status unknown, no quota windows) because every real Codex
// account was exhausted and an unknown verdict only warned.
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { machineId: 'rmthz', kind: 'host', actor: 'machine:rmthz' }, scopes: ['host'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const intent = (extra = {}) => ({ workspace: 'demo', prompt: 'work', idempotency_key: crypto.randomUUID(), ...extra });

async function fixture(fn: (service: FleetService, sql: any) => Promise<void>, opts: { exhausted?: boolean; routeRejections?: any[] } = {}) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('placement-login-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    service.init();
    const sql = hub.sql;
    const hb = JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } });
    sql.exec(`INSERT INTO machines (id, name, hostname, os, arch, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      'rmthz', 'rmthz', 'rmthz', 'Linux', 'amd64', iso(), iso(), hb, 0,
      JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8,
        ...(opts.routeRejections ? { fleet_route_rejections: opts.routeRejections } : {}) }));
    sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('shared-codex', 'codex', 4, 15, 1)`);
    sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('dummy-sync-5814', 'codex', 4, 15, 1)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('rmthz', 'codex', 'shared-codex', NULL)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('rmthz', 'codex', 'dummy-sync-5814', 'hub-dummy-sync-5814')`);
    // shared-codex is observed by the Hub server probe (exhausted or healthy),
    // as in production; the dummy's named profile has no observation at all.
    sql.exec('CREATE TABLE IF NOT EXISTS server_quota_snapshots (account TEXT PRIMARY KEY, provider TEXT NOT NULL, captured_at TEXT NOT NULL, quota_json TEXT NOT NULL)');
    sql.exec('INSERT INTO server_quota_snapshots (account, provider, captured_at, quota_json) VALUES (?, ?, ?, ?)', 'shared-codex', 'codex', iso(),
      JSON.stringify({ status: 'ok', observed_at: iso(), details: { limit_reached: !!opts.exhausted },
        windows: { weekly: { present: true, percent_remaining: opts.exhausted ? 0 : 80, reset_at: iso(86400000) } } }));
    await fn(service, sql);
  });
}

describe('placement never picks unknown or logged-out accounts (intake 06d2a0ac)', () => {
  it('AC1 regression: real Codex exhausted + dummy-sync-5814 unknown -> the dummy is rejected, not selected', async () => fixture(async (service) => {
    const plan: any = service.plan(intent({ engine: 'codex' }));
    expect(plan.selected?.account).not.toBe('dummy-sync-5814');
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'rmthz', engine: 'codex', reason: 'account_unknown' }));
    expect(plan.rejected).toContainEqual(expect.objectContaining({ reason: 'account_blocked' }));
  }, { exhausted: true }));

  it('AC1: with a healthy real account the dummy never wins either', async () => fixture(async (service) => {
    for (let i = 0; i < 5; i++) {
      const plan: any = service.plan(intent());
      expect(plan.selected?.account).toBe('shared-codex');
      expect(plan.candidates.map((c: any) => c.account)).not.toContain('dummy-sync-5814');
    }
  }));

  it('keeps the fail-open projection rule when no account in the fleet has a verdict (probe outage)', async () => fixture(async (service, sql) => {
    sql.exec('DELETE FROM server_quota_snapshots');
    const plan: any = service.plan(intent());
    expect(plan.selected).not.toBeNull();
    expect(plan.rejected.map((r: any) => r.reason)).not.toContain('account_unknown');
  }));

  it('a disabled account carries no verdict: a probe outage with it disabled still fails open (review fb40dd01)', async () => fixture(async (service, sql) => {
    sql.exec('DELETE FROM server_quota_snapshots');
    sql.exec(`INSERT INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('codex-off', 'codex', 4, 15, 0)`);
    sql.exec(`INSERT INTO fleet_routes (machine_id, engine, account, profile) VALUES ('rmthz', 'codex', 'codex-off', 'hub-codex-off')`);
    const plan: any = service.plan(intent());
    expect(plan.selected).not.toBeNull();
    expect(plan.rejected.map((r: any) => r.reason)).not.toContain('account_unknown');
  }));

  it('the not_logged_in route hold is a primary-key read, never a launch-history scan (review fb40dd01)', async () => fixture(async (_service, sql) => {
    const plan = sql.exec("EXPLAIN QUERY PLAN SELECT 1 FROM fleet_login_holds WHERE machine_id = ? AND engine = ? AND account = ? AND profile = ? AND held_at_ms > ?",
      'rmthz', 'codex', 'dummy-sync-5814', 'hub-dummy-sync-5814', 0).toArray().map((r: any) => String(r.detail)).join(' | ');
    expect(plan).not.toMatch(/\bSCAN\b/);
    expect(plan).toMatch(/SEARCH fleet_login_holds USING PRIMARY KEY|USING INDEX sqlite_autoindex_fleet_login_holds/);
  }));

  it('an explicit --account pin is the operator override', async () => fixture(async (service) => {
    const plan: any = service.plan(intent({ account: 'dummy-sync-5814' }));
    expect(plan.selected?.account).toBe('dummy-sync-5814');
  }, { exhausted: true }));

  it('AC1/AC2: a host not_logged_in route report excludes exactly that profile', async () => fixture(async (service) => {
    const plan: any = service.plan(intent({ account: 'dummy-sync-5814' }));
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ machine: 'rmthz', reason: 'not_logged_in' }));
    expect((service.plan(intent()) as any).selected?.account).toBe('shared-codex');
  }, { routeRejections: ['headless', 'interactive'].map(mode => ({ engine: 'codex', account: 'dummy-sync-5814', profile: 'hub-dummy-sync-5814', mode, reason: 'not_logged_in', observed_at: iso() })) }));

  it('account_unknown and not_logged_in never turn a capacity hold into a hard reject', () => {
    expect(isCapacityOnlyHold([{ machine: 'm', engine: 'codex', reason: 'account_concurrency' }, { machine: 'm', engine: 'codex', reason: 'account_unknown' }, { machine: 'm', engine: 'codex', reason: 'not_logged_in' }])).toBe(true);
    expect(isCapacityOnlyHold([{ machine: 'm', engine: 'codex', reason: 'account_unknown' }])).toBe(false);
  });

  it('AC2: a not_logged_in dispatch refusal fails that launch and re-places the intent, holding the route', async () => fixture(async (service, sql) => {
    const id = crypto.randomUUID();
    sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at)
      VALUES (?, 'admin', 'human', 's', ?, ?, 'rmthz', 'codex', 'dummy-sync-5814', 'hub-dummy-sync-5814', 'interactive', 'demo', '', 'offered', ?, ?)`,
      id, id, JSON.stringify({ mode: 'interactive', workspace: 'demo' }), iso(), iso());
    const diagnostic = { reason: 'not_logged_in', message: 'The engine profile on this host is not logged in (no credential).', stage: 'dispatch_route', error_type: 'ValueError' };
    const res = await service.handle(new Request('https://hub.test/api/fleet/launches/' + id + '/reject', { method: 'POST', body: JSON.stringify({ reason: 'not_logged_in', diagnostic }) }), host);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.id).not.toBe(id);
    expect(sql.exec('SELECT state, exit_reason FROM fleet_launches WHERE id = ?', id).one()).toMatchObject({ state: 'failed', exit_reason: 'not_logged_in' });
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_login_holds WHERE machine_id = ? AND account = ?', 'rmthz', 'dummy-sync-5814').one().n).toBe(1);
    const next: any = sql.exec('SELECT state, account FROM fleet_launches WHERE id = ?', body.id).one();
    expect(next.account).not.toBe('dummy-sync-5814');
    const pinned: any = service.plan(intent({ account: 'dummy-sync-5814' }));
    expect(pinned.rejected).toContainEqual(expect.objectContaining({ reason: 'not_logged_in' }));
  }));

  it('AC3: an operator disables and re-enables an account reversibly; nothing is deleted', async () => fixture(async (service, sql) => {
    const toggle = (enabled: boolean, auth: any = admin) => service.handle(new Request('https://hub.test/api/fleet/accounts/dummy-sync-5814',
      { method: 'PATCH', body: JSON.stringify({ enabled, reason: 'test account' }) }), auth);
    expect((await toggle(false, host)).status).toBe(403);
    const off = await toggle(false);
    expect(await off.json()).toMatchObject({ status: 'disabled', account: 'dummy-sync-5814', changed: true });
    expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_routes WHERE account = 'dummy-sync-5814'").one().n).toBe(1);
    const fleet: any = await (await service.handle(new Request('https://hub.test/api/fleet'), admin)).json();
    const dummy = fleet.accounts.find((a: any) => a.label === 'dummy-sync-5814');
    expect(dummy).toMatchObject({ enabled: false, status: 'blocked' });
    expect(dummy.placement_reasons).toContain('account_disabled');
    const pinned: any = service.plan(intent({ account: 'dummy-sync-5814' }));
    expect(pinned.selected).toBeNull();
    expect(pinned.rejected).toContainEqual(expect.objectContaining({ reason: 'account_blocked' }));
    expect(await (await toggle(true)).json()).toMatchObject({ status: 'enabled', changed: true });
    expect((service.plan(intent({ account: 'dummy-sync-5814' })) as any).selected?.account).toBe('dummy-sync-5814');
    expect((await service.handle(new Request('https://hub.test/api/fleet/accounts/nope', { method: 'PATCH', body: JSON.stringify({ enabled: false }) }), admin)).status).toBe(404);
    expect((await service.handle(new Request('https://hub.test/api/fleet/accounts/dummy-sync-5814', { method: 'PATCH', body: JSON.stringify({ enabled: 'no' }) }), admin)).status).toBe(400);
  }));
});
