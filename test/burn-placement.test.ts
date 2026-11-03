import { describe, expect, it, vi } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

// Burn-aware placement (intake ebfe9d85 AC1/AC4): fleet plan() and the
// offer re-placement favour quota that would otherwise expire unused.
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const H = 3600000;
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const intent = (extra: any = {}) => ({ workspace: 'demo', prompt: 'burn regression', idempotency_key: crypto.randomUUID(), ...extra });
const weekly = (pct: number, hoursLeft: number) => ({ status: 'ok', observed_at: iso(), details: { limit_reached: false },
  windows: { '7d': { present: true, percent_remaining: pct, reset_at: iso(hoursLeft * H) } } });

async function fixture(check: (fleet: FleetService, sql: any, setQuota: (claude: any, codex: any) => void) => Promise<void>) {
  const random = vi.spyOn(Math, 'random').mockReturnValue(0);
  try {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('burn-placement-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      fleet.init();
      const sql = hub.sql;
      const heartbeat = { mem_avail_mb: 37000, disk_free_gb: 80, load: [1, 1, 1], cpu_usage: freshCpu(iso(), 8, 1) };
      sql.exec('INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES (?,?,?,?,?,?,?,?)',
        'host', 'host', 'host', iso(), iso(), JSON.stringify(heartbeat), 0,
        JSON.stringify({ engines: ['claude', 'codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
      sql.exec('INSERT INTO routing_machines (machine_id,needs_json,max_agents,runners_json,observed_at) VALUES (?,?,?,?,?)', 'host', '[]', 4, '[]', iso());
      const setQuota = (claude: any, codex: any) => {
        sql.exec('DELETE FROM host_quota_snapshots');
        sql.exec('INSERT INTO host_quota_snapshots (host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES (?,?,?,?,?,?,?,?,?)',
          'host', '1.0', iso(), now, iso(), now, 'ok', null, JSON.stringify({ claude, codex }));
      };
      const configured = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
        accounts: [
          { label: 'shared-claude', provider: 'claude', max_concurrency: 100, reserve_percent: 0, enabled: true },
          { label: 'shared-codex', provider: 'codex', max_concurrency: 100, reserve_percent: 0, enabled: true },
        ],
        routes: [
          { machine_id: 'host', engine: 'claude', account: 'shared-claude', profile: null },
          { machine_id: 'host', engine: 'codex', account: 'shared-codex', profile: null },
        ],
      }) }), admin);
      expect(configured.status).toBe(200);
      await check(fleet, sql, setQuota);
    });
  } finally {
    random.mockRestore();
  }
}

const rebalance = async (fleet: FleetService, apply: boolean) =>
  (await fleet.handle(new Request('https://hub.test/api/fleet/burn/rebalance', { method: 'POST', body: JSON.stringify({ apply }) }), admin)).json() as Promise<any>;

describe('burn-aware fleet placement', () => {
  it('lands policy-driven work on the account whose quota would expire unused', () => fixture(async (fleet, _sql, setQuota) => {
    // Synthetic AC example: an expiring account competes with an early-cycle account.
    setQuota(weekly(95, 28), weekly(79, 162));
    const plan = fleet.plan(intent());
    expect(plan.selected).toMatchObject({ account: 'shared-claude', engine: 'claude' });
    expect(plan.selected.burn_weight).toBeGreaterThan(3);
    expect(plan.selected.burn.waste_alert).toBe(true);
    const codex = plan.candidates!.find((c: any) => c.account === 'shared-codex');
    expect(plan.selected.score).toBeGreaterThan(2 * codex.score);
    // A reasoned deep request can use Opus at medium effort.
    expect(fleet.plan(intent({ tier: 'deep', engine_override_reason: 'This task explicitly requires deep reasoning.' })).selected)
      .toMatchObject({ account: 'shared-claude', selection: expect.objectContaining({ id: 'opus', effort: 'medium' }) });
    // Account status exposes the forecast for the dashboard and alert.
    const status = fleet.accountStatus({ label: 'shared-claude', provider: 'claude', max_concurrency: 100, reserve_percent: 0, enabled: 1 });
    expect(status.burn_forecast).toMatchObject({ window: '7d', waste_alert: true });
    expect(status.burn_forecast!.projected_unused_percent).toBeGreaterThan(90);
  }));

  it('keeps remaining quota as the tiebreak when nothing is expiring', () => fixture(async (fleet, _sql, setQuota) => {
    setQuota(weekly(10, 24), weekly(79, 162)); // Claude nearly spent, Codex fresh
    expect(fleet.plan(intent()).selected).toMatchObject({ account: 'shared-codex' });
  }));

  it('moves unclaimed offers to under-used accounts and reports the count (AC4)', () => fixture(async (fleet, sql, setQuota) => {
    setQuota(weekly(10, 24), weekly(79, 162));
    const free: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin)).json();
    const pinned: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent({ engine: 'codex' })) }), admin)).json();
    expect(free).toMatchObject({ state: 'offered', account: 'shared-codex' });
    expect(pinned).toMatchObject({ state: 'offered', account: 'shared-codex' });
    // Claude's week turns out to be idle and its reset is close.
    setQuota(weekly(95, 28), weekly(79, 162));
    const dry = await rebalance(fleet, false);
    expect(dry).toMatchObject({ applied: false, examined: 2, moved_count: 1 });
    expect(dry.moved[0]).toMatchObject({ id: free.id, from: { account: 'shared-codex' }, to: { account: 'shared-claude', engine: 'claude' } });
    expect(dry.kept).toContainEqual(expect.objectContaining({ id: pinned.id, reason: 'already_best' }));
    expect(sql.exec('SELECT account FROM fleet_launches WHERE id=?', free.id).one().account).toBe('shared-codex');
    const applied = await rebalance(fleet, true);
    expect(applied).toMatchObject({ applied: true, moved_count: 1 });
    expect(sql.exec('SELECT state, engine, account FROM fleet_launches WHERE id=?', free.id).one()).toMatchObject({ state: 'offered', engine: 'claude', account: 'shared-claude' });
    expect(JSON.parse(sql.exec('SELECT model_selection_json FROM fleet_launches WHERE id=?', free.id).one().model_selection_json as string).provider).toBe('claude');
    // The explicit engine pin stays binding.
    expect(sql.exec('SELECT account FROM fleet_launches WHERE id=?', pinned.id).one().account).toBe('shared-codex');
    // A second pass is a no-op.
    expect((await rebalance(fleet, true)).moved_count).toBe(0);
  }));

  it('never moves a claimed offer and rejects malformed or non-admin requests', () => fixture(async (fleet, sql, setQuota) => {
    setQuota(weekly(10, 24), weekly(79, 162));
    const launch: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin)).json();
    sql.exec("UPDATE fleet_launches SET claim_key='k-1' WHERE id=?", launch.id);
    setQuota(weekly(95, 28), weekly(79, 162));
    expect(await rebalance(fleet, true)).toMatchObject({ examined: 0, moved_count: 0 });
    expect(sql.exec('SELECT account FROM fleet_launches WHERE id=?', launch.id).one().account).toBe('shared-codex');
    const bad = await fleet.handle(new Request('https://hub.test/api/fleet/burn/rebalance', { method: 'POST', body: JSON.stringify({ apply: 'yes' }) }), admin);
    expect(bad.status).toBe(400);
    const agent = await fleet.handle(new Request('https://hub.test/api/fleet/burn/rebalance', { method: 'POST', body: JSON.stringify({ apply: true }) }),
      { caller: { kind: 'agent', actor: 'worker' }, scopes: ['fleet:launch'] } as any);
    expect(agent.status).toBe(403);
  }));

  it('preserves admission holds and native/host bindings when rebalancing', () => fixture(async (fleet, sql, setQuota) => {
    setQuota(weekly(10, 24), weekly(79, 162));
    const offers: any[] = [];
    for (let i = 0; i < 4; i++) {
      offers.push(await (await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin)).json());
    }
    sql.exec("UPDATE fleet_launches SET native_id='already-native' WHERE id=?", offers[0].id);
    sql.exec("UPDATE fleet_launches SET launcher_task_id='already-launched' WHERE id=?", offers[1].id);
    sql.exec("INSERT INTO session_control_sessions (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,host_family,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      crypto.randomUUID(), 'test-admin', crypto.randomUUID(), '{}', offers[2].id, 'host', 'codex', 'headless', 'offered', 'host-bound', iso(), iso());
    const heldReason = fleet.admission.heldReason.bind(fleet.admission);
    fleet.admission.heldReason = (row: any) => row.id === offers[3].id ? 'claim_inactive' : heldReason(row);
    setQuota(weekly(95, 28), weekly(79, 162));
    const result = await rebalance(fleet, true);
    expect(result.moved_count).toBe(0);
    expect(result.kept).toContainEqual(expect.objectContaining({ id: offers[2].id, reason: 'session_bound' }));
    expect(result.kept).toContainEqual(expect.objectContaining({ id: offers[3].id, reason: 'admission_held' }));
    for (const offer of offers) expect(sql.exec('SELECT account FROM fleet_launches WHERE id=?', offer.id).one().account).toBe('shared-codex');
  }));

  it('rejects an old host claim after moving an unclaimed offer', () => fixture(async (fleet, _sql, setQuota) => {
    setQuota(weekly(10, 24), weekly(79, 162));
    const offer: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin)).json();
    setQuota(weekly(95, 28), weekly(79, 162));
    expect((await rebalance(fleet, true)).moved_count).toBe(1);
    const reply = await fleet.handle(new Request(`https://hub.test/api/fleet/launches/${offer.id}/claim`, { method: 'POST', body: JSON.stringify({
      gate_allowed: true, observed_at: iso(), account: 'shared-codex', profile_supported: true, model: 'gpt-6.1-sol',
    }) }), { caller: { kind: 'host', machineId: 'host' }, scopes: ['host'] } as any);
    expect(reply.status).toBe(409);
    expect(await reply.json()).toMatchObject({ error: 'fresh_admission_required' });
  }));
});
