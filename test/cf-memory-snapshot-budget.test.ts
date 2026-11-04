import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { MEMORY_PENDING_SCAN_LIMIT, memoryVerdict, ResourceManagement } from '../src/resource-management';

const withHub = (fn: (hub: any, state: any) => void) => (runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('cf-memory-bound-' + crypto.randomUUID())), fn);

function machine(raw: any, id: string, at: string) {
  raw.exec('INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat) VALUES(?,?,?,?,?,?)',
    id, id, id, at, at, JSON.stringify({ mem_avail_mb: 64000, at, launcher_resources: { observed_at: at, active_mem_mb: 0 } }));
}
function launches(raw: any, machine: string, prefix: string, count: number, state: string, at: string) {
  raw.exec(`WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i+1<?)
    INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    SELECT ?||i,'fixture','agent','fixture',?||i,'{}',?,'codex','fixture','headless','/fixture','fixture',?,?,? FROM n`,
    count, prefix, prefix, machine, state, at, at);
}
function readRows(hub: any, scope: string, fn: () => any) {
  const before = hub.meter.snapshot()[scope]?.rows_read ?? 0;
  const value = hub.meter.runSync(scope, fn);
  return { value, rows: (hub.meter.snapshot()[scope]?.rows_read ?? 0) - before };
}

describe('bounded memory visibility and admission', () => {
  it('ignores thousands of terminal launches without scanning their history', () => withHub((hub, state) => {
    const raw = state.storage.sql, now = Date.now(), at = new Date(now).toISOString(), id = 'memory-host';
    machine(raw, id, at);
    launches(raw, id, 'failed-', 5000, 'failed', at);
    launches(raw, id, 'pending-', 4, 'starting', at);
    const measured = readRows(hub, 'memory:terminal-history', () => hub.fleetService.resources.headroom(id));
    console.log('memory headroom with 5000 failed launches rows_read=' + measured.rows);
    expect(measured.rows).toBeLessThan(100);
    expect(measured.value.snapshot_truncated).toBe(false);
    expect(measured.value.reserved_mb).toBe(4 * hub.fleetService.resources.estimate('/fixture', 'codex', 'goal'));
  }));

  it('refuses overflow before sparse ramp eligibility can scan all active launches', () => withHub((hub, state) => {
    const raw = state.storage.sql, now = Date.now(), at = new Date(now).toISOString(), old = new Date(now - 86400000).toISOString();
    machine(raw, 'overflow-host', at);
    // Every old running row fails ramp eligibility. Filtering first would
    // still scan all 3000 before reaching the one eligible tail row.
    launches(raw, 'overflow-host', 'old-running-', 3000, 'running', old);
    launches(raw, 'overflow-host', 'tail-pending-', 1, 'starting', at);
    const measured = readRows(hub, 'memory:sparse-ramps', () => hub.fleetService.resources.headroom('overflow-host'));
    console.log('memory sparse-ramp candidate cap=' + MEMORY_PENDING_SCAN_LIMIT + ' rows_read=' + measured.rows);
    expect(measured.rows).toBeLessThan(1000);
    expect(measured.value).toMatchObject({ snapshot_truncated: true, reserved_mb: null, requested_mb: null, reason: 'resource_snapshot_overflow' });
    expect(memoryVerdict(64000, measured.value.reserved_mb, measured.value.requested_mb, hub.fleetService.resources.config())).toBe(false);
    const view = hub.fleetService.hostResources('overflow-host')[0];
    expect(view.headroom_mb).toBe(null);
    expect(view.headroom_ok).toBe(null);
    expect(view.stale_telemetry).toBe(true);
  }));

  it('calibration seeks the eligible partial index despite sparse measurements', () => withHub((hub, state) => {
    const raw = state.storage.sql, now = Date.now(), at = new Date(now).toISOString(), engine = 'calibration-' + crypto.randomUUID();
    raw.exec(`WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i<4999)
      INSERT INTO session_resource_usage(session_id,machine_id,native_id,project,engine,kind,started_at,finished_at,state,peak_rss_mb,observed_at)
      SELECT 'sparse-'||i,'m','sparse-'||i,'calibration',?,'goal',?,CASE WHEN i%2=0 THEN NULL ELSE ? END,'exited',CASE WHEN i%2=0 THEN 100 ELSE NULL END,? FROM n`, engine, at, at, at);
    raw.exec(`INSERT INTO session_resource_usage(session_id,machine_id,native_id,project,engine,kind,started_at,finished_at,state,peak_rss_mb,observed_at)
      VALUES('measured','m','measured','calibration',?,'goal',?,?,'exited',256,?)`, engine, at, at, at);
    raw.exec("INSERT INTO session_rss_median VALUES('measured',128,?)", at);
    const resources = new ResourceManagement(hub.sql, () => now);
    const measured = readRows(hub, 'memory:sparse-calibration', () => resources.estimate('calibration', engine, 'goal'));
    console.log('memory sparse calibration rows_read=' + measured.rows);
    expect(measured.rows).toBeLessThan(20);
    expect(measured.value).toBe(Math.ceil(128 * resources.config().estimate_multiplier));
  }));
});
