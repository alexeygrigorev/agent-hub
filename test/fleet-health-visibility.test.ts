import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { LivenessService, DEFAULT_THRESHOLDS } from '../src/liveness';
import { placementPressureSubjects } from '../src/host-pressure';
import { MonitoringService } from '../src/monitoring';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const now = Date.parse('2026-10-24T16:00:00Z');
const iso = (msDelta: number) => new Date(now + msDelta).toISOString();
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };

async function fixture(fn: (ctx: { fleet: FleetService; live: LivenessService; monitoring: MonitoringService; sql: any }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-health-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const tx = <T>(fn: () => T): T => state.storage.transactionSync(fn);
    const clock = () => now;
    const fleet = new FleetService(hub.sql, tx, clock);
    fleet.init();
    const live = new LivenessService(hub.sql, DEFAULT_THRESHOLDS, clock);
    live.init();
    live.setPressureProjection(() => placementPressureSubjects(hub.sql, fleet, now));
    const monitoring = new MonitoringService(hub.sql, tx, live, undefined, undefined, clock);
    monitoring.init();
    monitoring.setResourceProjection(machine => fleet.hostResources(machine)[0]);
    await fn({ fleet, live, monitoring, sql: hub.sql });
  });
}

function machine(sql: any, id: string, hostname: string, checkinMs: number | null, drained = 0, caps: any = null, hb: any = null) {
  sql.exec(
    'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained, capabilities, last_heartbeat) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
    id, id, hostname, iso(-86400000), checkinMs === null ? null : new Date(checkinMs).toISOString(), drained,
    JSON.stringify(caps ?? { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }),
    hb === null ? null : JSON.stringify(hb),
  );
}

describe('fleet health visibility + machine aliasing', () => {
  it('maps stale hetzner sessions to the live urgent-hetzner host with real state', () => fixture(async ({ live, sql }) => {
    machine(sql, 'hetzner', 'RMTHZ', now - 220 * 3600000);
    machine(sql, 'urgent-hetzner', 'rmthz', now);
    sql.exec(
      "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at) VALUES ('hetzner', 'n-1', 'worker-1', 'codex', 'running', 'working', ?, '/repo', NULL, NULL, ?)",
      iso(0), iso(0),
    );
    const { subjects } = live.evaluate();
    const session = subjects.find((s) => s.kind === 'session' && s.id === 'urgent-hetzner/worker-1');
    expect(session).toBeTruthy();
    expect(session!.state).toBe('working');
    expect(session!.resolved_machine_id).toBe('urgent-hetzner');
    expect(session!.alias_of).toBe('urgent-hetzner');
    const stale = subjects.find((s) => s.kind === 'machine' && s.id === 'hetzner');
    expect(stale!.alias_of).toBe('urgent-hetzner');
    expect(stale!.detail).toContain('alias of urgent-hetzner');
    // No records are deleted by aliasing.
    expect(sql.exec("SELECT COUNT(*) AS n FROM machines").one().n).toBe(2);
    expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_sessions").one().n).toBe(1);
  }));

  it('projects launcher reservation numbers with floor and headroom', () => fixture(async ({ fleet, sql }) => {
    const hb = {
      mem_avail_mb: 3000, disk_free_gb: 80, load: [1],
      gate: { launch_allowed: true, at: iso(0) },
      launcher_resources: { active_mem_mb: 29000, observed_at: iso(0) },
    };
    machine(sql, 'hetzner', 'hetzner', now, 0,
      { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8, launcher_resources: true }, hb);
    const [res] = fleet.hostResources('hetzner');
    expect(res.mem_avail_mb).toBe(3000);
    // Launcher static reservations are not double-counted against live MemAvailable.
    expect(res.reserved_mb).toBe(0);
    expect(res.floor_mb).toBe(2048);
    expect(res.next_slot_mb).toBe(1500);
    // 3000 - 0*1500 - 1500 = 1500 < 2048 floor.
    expect(res.headroom_mb).toBe(1500);
    expect(res.headroom_ok).toBe(false);
    expect(res.stale_telemetry).toBe(false);
  }));

  it('marks stale launcher telemetry instead of projecting phantom headroom', () => fixture(async ({ fleet, sql }) => {
    const hb = {
      mem_avail_mb: 32000, disk_free_gb: 80, load: [1],
      gate: { launch_allowed: true, at: iso(0) },
      launcher_resources: { active_mem_mb: 0, observed_at: iso(-200000) },
    };
    machine(sql, 'hetzner', 'hetzner', now, 0,
      { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8, launcher_resources: true }, hb);
    const [res] = fleet.hostResources('hetzner');
    expect(res.stale_telemetry).toBe(true);
    expect(res.headroom_ok).toBeNull();
  }));

  it('splits account slots into counted reservations, orphans and live natives', () => fixture(async ({ fleet, sql }) => {
    machine(sql, 'hetzner', 'hetzner', now);
    sql.exec("INSERT INTO fleet_accounts VALUES ('shared-codex', 'codex', 8, 15, 1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('hetzner', 'codex', 'shared-codex', NULL)");
    const quota = { status: 'ok', observed_at: iso(0), details: { limit_reached: false }, windows: { '5h': { present: true, percent_remaining: 80, reset_at: iso(86400000) }, '7d': { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    sql.exec("INSERT INTO fleet_subscriptions VALUES ('hetzner', 'shared-codex', 'codex', 'pro', ?, ?)", iso(0), JSON.stringify(quota));
    // One launch reservation with a live native binding.
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES ('a0000000-0000-4000-8000-000000000001', 'admin', 'human', NULL, 't', 'k1', '{}', 'hetzner', 'codex', 'shared-codex', NULL, 'headless', 'demo', 'goal', 'running', 'n-live', ?, ?)",
      iso(-60000), iso(-1000),
    );
    sql.exec(
      "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at) VALUES ('hetzner', 'n-live', 'task-hub-x', 'codex', 'running', 'working', ?, 'demo', NULL, 'shared-codex', ?)",
      iso(0), iso(0),
    );
    // One orphan observed session (no Hub launch) holding a slot.
    sql.exec(
      "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at) VALUES ('hetzner', 'n-orphan', 'parked', 'codex', 'running', 'idle', ?, 'demo', NULL, 'shared-codex', ?)",
      iso(0), iso(0),
    );
    sql.exec("INSERT INTO fleet_catalog_seen VALUES ('hetzner', ?)", iso(0));
    const status: any = fleet.accountStatus({ label: 'shared-codex', provider: 'codex', max_concurrency: 8, reserve_percent: 15, enabled: 1 });
    expect(status.active).toBe(status.reserved + status.observed_sessions);
    expect(status.reserved).toBe(1);
    expect(status.observed_sessions).toBe(1);
    expect(status.native_live).toBe(2);
    expect(status.available_slots).toBe(6);
  }));

  it('raises a stuck alert subject for launches held queued over 5 minutes, with numbers', () => fixture(async ({ live, sql }) => {
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES ('b0000000-0000-4000-8000-000000000001', 'admin', 'human', NULL, 'held work', 'kq', '{}', '', '', '', NULL, 'headless', 'demo', 'goal', 'queued', NULL, ?, ?)",
      iso(-10 * 60000), iso(-10 * 60000),
    );
    const subjects = live.evaluate().subjects.filter((s) => s.kind === 'launch' && s.id === 'placement-queue:b0000000-0000-4000-8000-000000000001');
    expect(subjects.length).toBe(1);
    expect(subjects[0].state).toBe('stuck');
    expect(subjects[0].detail).toContain('held queued');
    // A fresh queue (<5 min) stays silent.
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES ('b0000000-0000-4000-8000-000000000002', 'admin', 'human', NULL, 'fresh wait', 'kq2', '{}', '', '', '', NULL, 'headless', 'demo', 'goal', 'queued', NULL, ?, ?)",
      iso(-60000), iso(-60000),
    );
    const fresh = live.evaluate().subjects.filter((s) => s.kind === 'launch' && s.id === 'b0000000-0000-4000-8000-000000000002');
    expect(fresh.length).toBe(0);
  }));

  it('surfaces recent machine_capacity failures as stuck subjects', () => fixture(async ({ live, sql }) => {
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at, exit_reason, launch_error_json) VALUES ('c0000000-0000-4000-8000-000000000001', 'admin', 'human', NULL, 'mem work', 'kf', '{}', 'hetzner', 'codex', 'shared-codex', NULL, 'headless', 'demo', 'goal', 'failed', NULL, 'hub-c0000000-0000-4000-8000-000000000001', ?, ?, 'machine_capacity', ?)",
      iso(-3600000), iso(-600000), JSON.stringify({ reason: 'machine_capacity', message: 'below floor', error_type: 'Memory', stage: 'dispatch_headless' }),
    );
    const subjects = live.evaluate().subjects.filter((s) => s.id === 'placement-failed:c0000000-0000-4000-8000-000000000001');
    expect(subjects.length).toBe(1);
    expect(subjects[0].state).toBe('stuck');
    expect(subjects[0].detail).toContain('machine_capacity');
  }));

  it('records hard rejections naming memory/capacity and alerts with numbers', () => fixture(async ({ fleet, live, sql }) => {
    machine(sql, 'm1', 'm1', now, 0, { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8 }, {mem_avail_mb: 32000, disk_free_gb: 100, load: [1], gate: {launch_allowed: true, at: iso(0)}});
    machine(sql, 'm2', 'm2', now, 0, { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['other'], cpu_count: 8 });
    for (const m of ['m1', 'm2']) {
      sql.exec('INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', m, '1.0', iso(0), now, iso(0), now, 'ok', null,
        JSON.stringify({ codex: { status: 'ok', observed_at: iso(0), details: { limit_reached: false }, windows: { '5h': { present: true, percent_remaining: 80, reset_at: iso(86400000) }, '7d': { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } }));
    }
    sql.exec("INSERT INTO fleet_accounts VALUES ('a1', 'codex', 1, 15, 1)");
    sql.exec("INSERT INTO fleet_routes VALUES ('m1', 'codex', 'a1', NULL)");
    sql.exec("INSERT INTO fleet_routes VALUES ('m2', 'codex', 'a1', NULL)");
    // Fill the single slot so m1 rejects with account_capacity while m2
    // rejects with a hard capability reason: mixed failure stays a 409.
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES ('d0000000-0000-4000-8000-000000000001', 'admin', 'human', NULL, 'holder', 'kh', '{}', 'm1', 'codex', 'a1', NULL, 'headless', 'demo', 'goal', 'running', 'n-hold', ?, ?)",
      iso(-60000), iso(-1000),
    );
    const res = fleet.reserve({ workspace: 'demo', prompt: 'goal reached', idempotency_key: 'pressure-probe-1' }, admin);
    expect(res.status).toBe(409);
    expect(JSON.stringify(res.value.rejected)).toContain('account_capacity');
    const count = Number(sql.exec('SELECT COUNT(*) AS n FROM placement_rejections').one().n);
    expect(count).toBe(1);
    const subjects = live.evaluate().subjects.filter((s) => String(s.id).startsWith('placement-reject:'));
    expect(subjects.length).toBe(1);
    expect(subjects[0].state).toBe('stuck');
    expect(subjects[0].detail).toContain('account_capacity');
  }));

  it('degrades host health while placement pressure exists and shows reservation numbers', () => fixture(async ({ monitoring, sql }) => {
    machine(sql, 'hetzner', 'hetzner', now, 0,
      { engines: ['codex'], fleet_modes: ['headless'], workspaces: ['demo'], cpu_count: 8, launcher_resources: true },
      { mem_avail_mb: 3000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso(0) }, launcher_resources: { active_mem_mb: 29000, observed_at: iso(0) } });
    const payload = { load: { load1: 0.5, load5: 0.4, load15: 0.3 }, memory: { total_bytes: 64 * 1024 ** 3, available_bytes: 32 * 1024 ** 3, used_bytes: 32 * 1024 ** 3, percent: 50, status: 'ok' } };
    sql.exec('INSERT INTO host_health_observations (machine_id, observed_at, observed_at_ms, received_at, payload_json, reporter) VALUES (?, ?, ?, ?, ?, ?)',
      'hetzner', iso(0), now, iso(0), JSON.stringify(payload), 'host:test');
    // Healthy baseline first.
    let status = monitoring.getHostHealthStatus(now);
    expect(status.machines[0].memory.status).toBe('ok');
    // Then a launch held queued for memory/capacity degrades the host.
    sql.exec(
      "INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES ('e0000000-0000-4000-8000-000000000001', 'admin', 'human', NULL, 'held work', 'ke', '{}', '', '', '', NULL, 'headless', 'demo', 'goal', 'queued', NULL, ?, ?)",
      iso(-10 * 60000), iso(-10 * 60000),
    );
    status = monitoring.getHostHealthStatus(now);
    expect(status.machines[0].memory.status).toBe('warn');
    expect(status.machines[0].placement!.reason).toContain('held queued');
    expect(status.machines[0].reservation!.reserved_mb).toBe(0);
    expect(status.machines[0].reservation!.floor_mb).toBe(2048);
    expect(status.machines[0].reservation!.headroom_mb).toBe(1500);
    const summary = monitoring.getStatusSummary(now);
    expect(summary.needs_attention.some((n: any) => String(n.subject).endsWith(':placement') && String(n.detail).includes('held queued'))).toBe(true);
  }));

  it('exposes aliases, host resources and placement on /api/fleet', () => fixture(async ({ fleet }) => {
    const res = await fleet.handle(new Request('https://hub.test/api/fleet'), admin);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.machine_aliases).toBeTruthy();
    expect(body.host_resources).toBeTruthy();
    expect(body.placement).toBeTruthy();
    expect(Array.isArray(body.sessions)).toBe(true);
  }));
  it('degrades visibility immediately for a new capacity hold while delaying ordinary hold alerts until five minutes', () => fixture(async ({ monitoring, live, sql }) => {
    machine(sql,'live','live',now);
    sql.exec('INSERT INTO host_health_observations VALUES(?,?,?,?,?,?)','live',iso(0),now,iso(0),JSON.stringify({memory:{total_bytes:64000,available_bytes:32000,used_bytes:32000,percent:50,status:'ok'},load:{load1:1,load5:1,load15:1}}),'test');
    sql.exec("INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at) VALUES('fresh-hold','admin','human','held','fresh-key','{}','','','','headless','demo','goal','queued',?,?)",iso(-60000),iso(-60000));
    const host=monitoring.getHostHealthStatus(now).machines[0];
    expect(host.memory.status).toBe('warn');
    expect(host.placement?.reason).toContain('held queued');
    expect(live.evaluate().subjects.some(s=>s.id==='placement-queue:fresh-hold')).toBe(false);
  }));

});

 it('persists complete census freshness without rewriting unchanged session rows or every heartbeat', () => fixture(async ({sql}) => {
   let tick=now; const fleet=new FleetService(sql, fn=>fn(), ()=>tick);
   machine(sql,'census-host','census-host',now);
   const caller={caller:{machineId:'census-host',kind:'host',actor:'machine:census-host'},scopes:['host']};
   const payload={sessions:[{native_id:'n1',tag:'worker',engine:'codex',phase:'running',agent_state:null,workspace:'demo',profile:null,account:null}]};
   const send=()=>fleet.handle(new Request('https://hub/api/fleet/sessions',{method:'PUT',body:JSON.stringify(payload)}),caller);
   expect((await send()).status).toBe(200);
   const first=sql.exec("SELECT observed_at FROM fleet_catalog_seen WHERE machine_id='census-host'").one().observed_at;
   tick+=30000; expect((await (await send()).json() as any).written).toBe(0);
   expect(sql.exec("SELECT observed_at FROM fleet_catalog_seen WHERE machine_id='census-host'").one().observed_at).toBe(first);
   tick+=60000; expect((await (await send()).json() as any).written).toBe(0);
   expect(sql.exec("SELECT observed_at FROM fleet_catalog_seen WHERE machine_id='census-host'").one().observed_at).toBe(iso(90000));
   expect(sql.exec("SELECT observed_at FROM fleet_sessions WHERE machine_id='census-host'").one().observed_at).toBe(first);
 }));

 it('keeps absent native observations unknown rather than asserting exit', () => fixture(async ({live,sql}) => {
   machine(sql,'absence-host','absence-host',now);
   for(const phase of ['unobserved','unknown','uncertain','starting','exiting']) sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('absence-host',?,?,?,?,'demo')",phase,phase,phase,iso(-86400000));
   sql.exec("INSERT INTO fleet_catalog_seen VALUES('absence-host',?)",iso(0));
   const subjects=live.evaluate().subjects.filter(s=>s.kind==='session' && s.machine_id==='absence-host');
   for(const phase of ['unobserved','unknown','uncertain','starting','exiting']) expect(subjects.find(s=>s.id.endsWith('/'+phase))?.state).toBe(['unobserved','unknown'].includes(phase)?'unknown':phase);
 }));
