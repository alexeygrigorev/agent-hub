import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { freshCpu } from './cpu-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();
const intent = () => ({ machine: 'host', workspace: 'demo', prompt: 'capacity regression', idempotency_key: crypto.randomUUID() });

async function fixture(check: (fleet: FleetService, sql: any, heartbeat: any) => Promise<void>, engine = 'codex') {
  const provider = engine === 'opencode' ? 'go' : 'codex';
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('machine-capacity-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
    fleet.init();
    const sql = hub.sql;
    const heartbeat = { mem_avail_mb: 37000, disk_free_gb: 80, load: [100, 100, 100], cpu_usage: freshCpu(iso(), 8, 8) };
    sql.exec('INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,last_heartbeat,drained,capabilities) VALUES (?,?,?,?,?,?,?,?)',
      'host', 'host', 'host', iso(), iso(), JSON.stringify(heartbeat), 0,
      JSON.stringify({ engines: [engine], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 }));
    sql.exec('INSERT INTO routing_machines (machine_id,needs_json,max_agents,runners_json,observed_at) VALUES (?,?,?,?,?)', 'host', '[]', 1, '[]', iso());
    sql.exec('INSERT INTO host_quota_snapshots (host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,error,routes_json) VALUES (?,?,?,?,?,?,?,?,?)',
      'host', '1.0', iso(), now, iso(), now, 'ok', null,
      JSON.stringify({ [provider]: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    const configured = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify({
      accounts: [{ label: 'test-account', provider, max_concurrency: 100, reserve_percent: 15, enabled: true }],
      routes: [{ machine_id: 'host', engine, account: 'test-account', profile: null }],
    }) }), admin);
    expect(configured.status).toBe(200);
    for (let i = 0; i < 110; i++) {
      sql.exec('INSERT INTO fleet_sessions (machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,account,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        'host', 'native-' + i, 'old-' + i, engine, 'running', i < 60 ? 'idle' : 'working', iso(), 'demo', null, iso(-7200000));
    }
    await check(fleet, sql, heartbeat);
  });
}

describe('machine admission uses resource evidence instead of a session ceiling', () => {
  it.each(['codex', 'opencode'])('places %s above max_agents with idle and stalled catalog rows and pending launches', async engine => fixture(async (fleet, sql) => {
    // A fresh observation of productive work must remain intact, too.
    sql.exec("UPDATE fleet_sessions SET last_activity_at=? WHERE native_id='native-109'", iso());
    expect(fleet.plan(intent()).selected?.machine).toBe('host');
    for (let i = 0; i < 3; i++) {
      const response = await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin);
      expect(response.status).toBe(201);
      expect(await response.json()).toMatchObject({ state: 'offered', machine: 'host' });
    }
    expect(fleet.plan(intent()).rejected).not.toContainEqual(expect.objectContaining({ reason: 'machine_capacity' }));
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_sessions').one().n).toBe(110);
    expect(sql.exec("SELECT phase,agent_state,last_activity_at FROM fleet_sessions WHERE native_id='native-109'").one())
      .toMatchObject({ phase: 'running', agent_state: 'working', last_activity_at: iso() });
  }, engine));

  it('promotes a held launch after pressure clears despite an unchanged native census', async () => fixture(async (fleet, sql, heartbeat) => {
    heartbeat.cpu_usage.psi_full_avg10 = 99;
    sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?', JSON.stringify(heartbeat), 'host');
    const held = await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin);
    expect(held.status).toBe(202);
    const launch: any = await held.json();
    expect(launch).toMatchObject({ state: 'queued' });
    heartbeat.cpu_usage.psi_full_avg10 = 0;
    sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?', JSON.stringify(heartbeat), 'host');
    const offered: any = await (await fleet.handle(new Request('https://hub.test/api/fleet/offers'),
      { caller: { machineId: 'host', kind: 'host' }, scopes: ['host'] })).json();
    expect(offered.offers).toContainEqual(expect.objectContaining({ id: launch.id }));
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?', launch.id).one().state).toBe('offered');
    expect(sql.exec('SELECT COUNT(*) AS n FROM fleet_sessions').one().n).toBe(110);
  }));

  it.each([
    ['CPU full pressure', 'cpu_pressure', { psi_full_avg10: 99 }],
    ['memory full pressure', 'memory_pressure', { memory_psi_full_avg10: 99 }],
    ['host OOM', 'host_oom', { host_oom_kills_delta: 1 }],
  ])('holds for %s even when count limits are ignored', async (_label, reason, cpu) => fixture(async (fleet, sql, heartbeat) => {
    heartbeat.cpu_usage = { ...heartbeat.cpu_usage, ...cpu };
    sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?', JSON.stringify(heartbeat), 'host');
    const plan = fleet.plan(intent());
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ reason }));
  }));

  it.each(['engine_runtime_timeout', 'engine_signal_exit'])('holds after a recent %s', async reason => fixture(async (fleet, sql) => {
    const response = await fleet.handle(new Request('https://hub.test/api/fleet/launches', { method: 'POST', body: JSON.stringify(intent()) }), admin);
    expect(response.status).toBe(201);
    const launch: any = await response.json();
    sql.exec("UPDATE fleet_launches SET state='failed',exit_reason=?,updated_at=? WHERE id=?", reason, iso(), launch.id);
    const plan = fleet.plan(intent());
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ reason: 'host_runtime_choking' }));
  }));

  it.each([2047, 2500])('reserves expected task memory above the floor with %i MB available', async available => fixture(async (fleet, sql, heartbeat) => {
    heartbeat.mem_avail_mb = available;
    sql.exec('UPDATE machines SET last_heartbeat=? WHERE id=?', JSON.stringify(heartbeat), 'host');
    const plan = fleet.plan(intent());
    expect(plan.selected).toBeNull();
    expect(plan.rejected).toContainEqual(expect.objectContaining({ reason: 'launcher_memory_reserve' }));
  }));
});
