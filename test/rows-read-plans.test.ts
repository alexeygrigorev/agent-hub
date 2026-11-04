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

  it('worker credential enumeration uses the deployed registry-actor index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT id,family_id,revoked_at,kind,machine_id FROM credentials INDEXED BY credentials_registry_actor WHERE registry_actor=? LIMIT ?', 'native:session',129);
    expect(p).toContain('credentials_registry_actor');
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

  it('quota replay bounds exact-time candidates using the deployed time index', () => withHub(async hub => {
    const p = plan(hub, 'SELECT account,provider,quota_json FROM usage_quota_history INDEXED BY usage_quota_history_time WHERE observed_at_ms=? LIMIT 129', NOW);
    expect(p).toContain('usage_quota_history_time');
  }));

  it('launcher memory-reserve scans stay off the failed-launch history', () => withHub(async hub => {
    const p = plan(hub, "SELECT id,updated_at,finished_at,machine_id,native_id,verification_status,exit_reason,state FROM fleet_launches INDEXED BY fleet_launch_updated WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 129", iso(Date.now() - 86400000));
    expect(p).toContain('fleet_launch_updated');
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
