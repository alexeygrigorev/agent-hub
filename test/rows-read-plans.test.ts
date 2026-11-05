/**
 * Hot-path query plans and write budgets (intake e8a22f4c AC2/AC4, 8081a05a AC(c)).
 * Every hot query must use an index: a full base-table scan fails CI.
 * Per-alarm-step and per-route write budgets fail CI when exceeded, so the
 * green Cloudflare product (DO rows written ~$1/M) cannot regress silently.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';

const NOW = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();

const withHub = (fn: (hub: Hub) => Promise<void>) => runInDurableObject(
  (env as any).HUB.get((env as any).HUB.idFromName('rows-plans-' + crypto.randomUUID())), async (hub: Hub) => { await fn(hub); });

const plan = (hub: Hub, q: string, ...b: any[]) =>
  (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...b).toArray() as any[]).map(r => String(r.detail)).join(' | ');

/** Full base-table scan in a plan, ignoring index-backed SEARCH/SCAN ... USING. */
const fullScan = (detail: string, table: string) =>
  detail.split(' | ').some(part => new RegExp(`^SCAN ${table}(\\s|$)`, '').test(part) && !/USING/.test(part));

describe('hot-path query plans use indexes, never full scans', () => {
  it('intake-session link lookups use the link primary key', () => withHub(async hub => {
    const p = plan(hub, 'SELECT DISTINCT task_id FROM intake_session_links WHERE session_key IN (?, ?)', 'a', 'b');
    expect(fullScan(p, 'intake_session_links')).toBe(false);
    expect(p).toMatch(/USING/i);
  }));

  it('the live session list uses the partial live index', () => withHub(async hub => {
    // Production forces the index via liveFrom(); the planner alone picks
    // session_control_state + a temp B-tree. Lock in that the forced index
    // exists and covers the ORDER BY (no temp sort, no scan).
    const p = plan(hub, `SELECT * FROM session_control_sessions INDEXED BY session_control_live_created WHERE state IN ('queued','held','offered','starting','running','uncertain') ORDER BY created_at DESC, id DESC LIMIT 51`);
    expect(p).toContain('session_control_live_created');
    expect(p).not.toMatch(/TEMP B-TREE/i);
    expect(fullScan(p, 'session_control_sessions')).toBe(false);
  }));

  it('native catalog pair lookups use the fleet_sessions primary key', () => withHub(async hub => {
    const p = plan(hub, 'SELECT * FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', 'm', 'n');
    expect(fullScan(p, 'fleet_sessions')).toBe(false);
    expect(p).toMatch(/USING/i);
  }));

  it('native catalog freshness uses timestamp indexes for cold-start markers', () => withHub(async hub => {
    hub.fleetService.init();
    const seen = plan(hub, 'SELECT machine_id,observed_at FROM fleet_catalog_seen WHERE observed_at>=? AND observed_at<=?', iso(NOW - 180000), iso(NOW + 60000));
    const complete = plan(hub, 'SELECT machine_id,observed_at FROM fleet_catalog_census WHERE complete=1 AND observed_at>=? AND observed_at<=?', iso(NOW - 180000), iso(NOW + 60000));
    expect(seen).toContain('fleet_catalog_seen_fresh');
    expect(complete).toContain('fleet_catalog_census_fresh');
    expect(fullScan(seen, 'fleet_catalog_seen')).toBe(false);
    expect(fullScan(complete, 'fleet_catalog_census')).toBe(false);
  }));

  it('scheduled intake open-work selection uses the partial status index and task primary key', () => withHub(async hub => {
    hub.intakeService.init();
    const p = plan(hub, `SELECT t.data FROM intake_status_summary s INDEXED BY intake_status_summary_open
      JOIN intake_tasks t ON t.id=s.id
      WHERE s.archived_at IS NULL AND s.status IN ('open','in_progress','blocked','pending_approval') ORDER BY s.id`);
    expect(p).toContain('intake_status_summary_open');
    expect(p).toMatch(/SEARCH t USING/i);
    expect(fullScan(p, 'intake_status_summary')).toBe(false);
    expect(fullScan(p, 'intake_tasks')).toBe(false);
  }));

  it('audit decisions are fetched by run index, not by retained-log scan', () => withHub(async hub => {
    hub.sessionAudit.init();
    const p = plan(hub, 'SELECT id FROM session_audit_decisions WHERE run_id = ? ORDER BY id', 1);
    expect(p).toContain('session_audit_decisions_run');
    expect(fullScan(p, 'session_audit_decisions')).toBe(false);
  }));

  it('worker credential existence uses the session-family index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT 1 FROM worker_access w JOIN credentials c ON c.family_id = w.family_id WHERE w.session_id = ? AND c.revoked_at IS NULL LIMIT 1', 's');
    expect(p).toContain('worker_access_session_family');
  }));

  it('message expiry probes use the expiry index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT 1 FROM messages WHERE expires_at <= ? LIMIT 1', NOW);
    expect(p).toContain('messages_expires_at');
  }));

  it('due-wake probes use the due index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT id FROM session_control_wakes WHERE state = ? AND wake_at_ms <= ? ORDER BY wake_at_ms, created_at, id LIMIT 10', 'pending', NOW);
    expect(p).toContain('session_control_wakes');
    expect(fullScan(p, 'session_control_wakes')).toBe(false);
  }));

  it('quota history point lookups use the account/provider/time index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT * FROM usage_quota_history WHERE account = ? AND provider = ? AND observed_at_ms >= ? ORDER BY observed_at_ms LIMIT 100', 'a', 'codex', NOW - 3600000);
    expect(p).toContain('usage_quota_history_account_provider_time');
  }));

  it('launcher memory-reserve scans stay off the failed-launch history', () => withHub(async hub => {
    const p = plan(hub, "SELECT id, updated_at, finished_at, machine_id, native_id, verification_status FROM fleet_launches WHERE state = 'failed' AND exit_reason = 'launcher_memory_reserve' AND updated_at >= ?", iso(Date.now() - 86400000));
    expect(p).toContain('fleet_launch_failed_reason');
    expect(fullScan(p, 'fleet_launches')).toBe(false);
  }));

  it('message inserts bind agents with indexed lookups, not registry scans', () => withHub(async hub => {
    for (let i = 0; i < 200; i++) hub.sql.exec("INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES(?,'p',NULL,NULL,?)", 'probe-' + i, iso(0));
    const cursor: any = hub.sql.exec("INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?)",
      'bind-probe', 'bot', 'grok-bot', 'note', 't', 'b', 'bind-key', Date.now() - 1000, Date.now() + 86400000, 'accepted');
    // The bind trigger used a CASE over stored columns and scanned the whole
    // registry per insert (~8 rows/agent, ~9k live); the sargable form reads
    // a handful of index entries regardless of registry size.
    expect(Number(cursor.rowsRead)).toBeLessThan(150);
  }));

  it('cleanup protections use the session primary key', () => withHub(async hub => {
    const p = plan(hub, 'SELECT request_key, created_at FROM session_control_protections WHERE session_id = ?', 's');
    expect(fullScan(p, 'session_control_protections')).toBe(false);
    expect(p).toMatch(/USING/i);
  }));
});

/** Rows written per scope by `fn`, from the meter's persisted hour ring. */
async function measureWrites(hub: Hub, fn: () => Promise<unknown>): Promise<Record<string, number>> {
  const read = (): Record<string, number> => {
    hub.meter.flush(hub.sql);
    return Object.fromEntries((hub.sql.exec('SELECT scope, SUM(rows_written) AS n FROM rows_read_stats GROUP BY scope').toArray() as any[]).map(r => [r.scope, Number(r.n)]));
  };
  const before = read();
  await fn();
  const after = read();
  const out: Record<string, number> = {};
  for (const [scope, n] of Object.entries(after)) if (scope !== '__total__' && scope !== 'meter:flush' && n - (before[scope] ?? 0) > 0) out[scope] = n - (before[scope] ?? 0);
  return out;
}

describe('per-step rows-written budgets', () => {
  it('a minute alarm writes almost nothing when nothing is due', () => withHub(async hub => {
    await hub.alarm(); // first alarm: runs once-only work and the 5-minute cadence steps
    const writes = await measureWrites(hub, () => hub.alarm());
    console.log('alarm writes by scope', JSON.stringify(writes));
    expect(writes['alarm:idle_work'] ?? 0).toBe(0); // at most every 5 minutes
    const total = Object.values(writes).reduce((a, b) => a + b, 0);
    expect(total).toBeLessThan(500);
  }));
});
