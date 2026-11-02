import type { SqlStorage } from '@cloudflare/workers-types';
import { canonicalQuotaProvider } from './quota';
import { effectiveCheckin } from './registry';

type Row = Record<string, any>;
export const CAPACITY_LIMITS = { hosts: 64, accounts: 128, routes: 256, launches: 256, natives: 256, engines: 16 };
export const CAPACITY_MACHINES_SQL = `SELECT m.id,m.last_checkin_at,m.last_heartbeat,m.capabilities,r.max_agents
  FROM machines m INDEXED BY fleet_queue_live_machines
  LEFT JOIN routing_machines r ON r.machine_id=m.id
  WHERE m.drained=0 AND m.revoked_at IS NULL AND m.last_checkin_at>=? AND m.last_checkin_at<=?
  ORDER BY m.last_checkin_at,m.id LIMIT 65`;
export type FleetCapacitySnapshot = {
  now: number; definitions: Row[]; machines: Row[]; routes: Row[]; launches: Row[]; natives: Row[];
  completeCatalogs: Map<string, number>; familyAccounts: Map<string, Set<string>>;
};
export const capacityFamilyKey = (machine: string, provider: string) => JSON.stringify([machine, provider]);
export const RESERVATION_TTL_MS = 15 * 60 * 1000;
export const LIVE_NATIVE_PHASES = new Set(['starting', 'running', 'broken', 'unknown', 'uncertain']);
export const NATIVE_IN_FLIGHT_SQL = `(
  l.native_id IS NULL AND l.state IN ('starting','uncertain') AND
  l.engine = s.engine AND l.account = s.account AND l.profile IS s.profile AND
  CASE WHEN json_valid(l.request_json) THEN
    CASE WHEN json_extract(l.request_json, '$.cwd') IS NULL OR json_extract(l.request_json, '$.cwd') = '.'
      THEN l.workspace
      WHEN json_extract(l.request_json, '$.cwd') LIKE '/%' OR json_extract(l.request_json, '$.cwd') GLOB '[A-Za-z]:[/\\]*'
      THEN json_extract(l.request_json, '$.cwd')
      ELSE rtrim(l.workspace, '/') || '/' || json_extract(l.request_json, '$.cwd') END
    END = s.workspace AND
  (SELECT COUNT(*) FROM (SELECT 1 FROM fleet_sessions spawned WHERE spawned.machine_id = s.machine_id
   AND spawned.tag = s.tag AND spawned.engine = s.engine AND spawned.account = s.account
   AND spawned.profile IS s.profile AND spawned.workspace = s.workspace
   AND spawned.phase IN ('starting','running','broken','unknown','uncertain') LIMIT 2)) = 1 AND
  ((l.mode = 'headless' AND s.tag = 'task-hub-' || l.id) OR
   (l.mode = 'interactive' AND s.tag = 'hub-' || l.id)))`;
export const INFLIGHT_LAUNCH_ID_SQL = `CASE WHEN NOT EXISTS (SELECT 1 FROM fleet_launches l
  WHERE l.machine_id=s.machine_id AND l.native_id=s.native_id AND l.state IN ('offered','starting','running','uncertain'))
  THEN (SELECT l.id FROM fleet_launches l
  WHERE l.id = CASE WHEN substr(s.tag,1,9)='task-hub-' THEN substr(s.tag,10)
    WHEN substr(s.tag,1,4)='hub-' THEN substr(s.tag,5) END
  AND l.machine_id=s.machine_id AND ${NATIVE_IN_FLIGHT_SQL}) END`;

/** Match the exact historical native, including an incarnation-checked control
 * binding when a launch shed its native_id. Both ordinary and snapshot reads
 * use this join. Two matching bindings are already ambiguous; bound that probe
 * before aggregating, even if a launch has retained many duplicate controls. */
export function reservationNativeJoinSql(sql: SqlStorage): string {
  let boundNativeId = 'NULL';
  let boundNativeCreatedAt = 'NULL';
  let hasControlStore = false;
  try {
    hasControlStore = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
  } catch { /* No usable control store: keep the physical reservation. */ }
  if (hasControlStore) {
    const bindings = `SELECT c.binding_json FROM session_control_sessions c
      WHERE c.machine_id=l.machine_id AND c.fleet_launch_id=l.id AND c.engine=l.engine
      AND c.state IN ('running','uncertain') AND c.binding_json IS NOT NULL LIMIT 2`;
    boundNativeId = `(SELECT CASE WHEN COUNT(*)=1 THEN MAX(
      CASE WHEN json_valid(c.binding_json) THEN
        CASE WHEN json_type(c.binding_json,'$.native_id')='text' AND json_type(c.binding_json,'$.native_created_at_ms')='integer'
          THEN json_extract(c.binding_json,'$.native_id') END END) END FROM (${bindings}) c)`;
    boundNativeCreatedAt = `(SELECT CASE WHEN COUNT(*)=1 THEN MAX(
      CASE WHEN json_valid(c.binding_json) THEN
        CASE WHEN json_type(c.binding_json,'$.native_created_at_ms')='integer'
          THEN json_extract(c.binding_json,'$.native_created_at_ms') END END) END FROM (${bindings}) c)`;
  }
  return `LEFT JOIN fleet_sessions s ON s.machine_id=l.machine_id
    AND s.native_id=COALESCE(NULLIF(l.native_id,''),${boundNativeId})
    AND (NULLIF(l.native_id,'') IS NOT NULL OR
      (s.engine=l.engine AND s.account=l.account AND s.created_at_ms=${boundNativeCreatedAt}))`;
}

/** Absence releases physical capacity only with a fresh complete census.
 * A running launch is releasable only when its direct native_id has no row;
 * an uncertain launch's direct native_id lacks incarnation proof and requires
 * its exact control-binding fallback above. */
export function unobservedReservationReleased(row: Row, completeCatalogs: Map<string, number>): boolean {
  const censusAt = completeCatalogs.get(String(row.machine_id));
  const exactNativeMissing = row.state === 'running' && row.phase == null
    && typeof row.native_id === 'string' && row.native_id.length > 0;
  return ['running','uncertain'].includes(row.state) && (row.phase==='unobserved' || exactNativeMissing)
    && censusAt !== undefined
    && (row.state!=='uncertain' || row.native_id==null || row.native_id==='');
}

/** Active concurrency is derived from observed live native sessions; a launch
 * reservation holds an additional slot only while no matching live native is
 * observed and the reservation is within its bounded TTL. */
export function reservationHoldsCapacity(
  row: Row,
  completeCatalogs: Map<string, number>,
  reservationCutoff: string,
  inFlightMatchedIds?: ReadonlySet<string>,
): boolean {
  if (!['offered', 'starting', 'running', 'uncertain'].includes(String(row.state))) return false;
  if (inFlightMatchedIds?.has(String(row.id))) return false;
  if (typeof row.phase === 'string') {
    if (LIVE_NATIVE_PHASES.has(row.phase) && (row.native_account == null || row.native_account === row.account)) return false;
    if (row.phase !== 'unobserved' && !LIVE_NATIVE_PHASES.has(row.phase)) return false;
  }
  if (typeof row.updated_at !== 'string' || row.updated_at <= reservationCutoff) return false;
  return !unobservedReservationReleased(row, completeCatalogs);
}

/** No maintenance, global scans or aggregate counts. Each indexed key batch has
 * at most 50 keys, leaving room for fixed bindings under SQLite's 100 limit.
 * Stop at the first overflow; no account/resource projection sees a prefix. */
export function readFleetCapacitySnapshot(sql: SqlStorage, now: number, unreservedPredicate: string): FleetCapacitySnapshot {
  const bounded = (query: string, limit: number, ...args: any[]): Row[] => {
    const rows = sql.exec(query, ...args).toArray() as Row[];
    if (rows.length > limit) throw new Error('capacity_observation_limited');
    return rows;
  };
  const machines = bounded(CAPACITY_MACHINES_SQL, CAPACITY_LIMITS.hosts,
    // Unchanged heartbeats persist at least every five minutes. Include their
    // storage lag, then use the same effective in-memory check-in as Fleet.
    new Date(now - 360000).toISOString(), new Date(now + 60000).toISOString())
    .map((m): Row => ({ ...m, last_checkin_at: effectiveCheckin(m.id, m.last_checkin_at) }))
    .filter(m => { const at = Date.parse(m.last_checkin_at || ''); return Number.isFinite(at) && at <= now + 60000 && now - at <= 180000; });
  const definitions = bounded('SELECT * FROM fleet_accounts ORDER BY label LIMIT 129', CAPACITY_LIMITS.accounts);
  const keyed = (keys: string[], limit: number, query: (marks: string, remaining: number) => string, ...fixed: any[]) => {
    const out: Row[] = [];
    const size = Math.min(50, 100 - fixed.length);
    if (size < 1) throw new Error('capacity_binding_budget');
    for (let i = 0; i < keys.length; i += size) {
      const batch = keys.slice(i, i + size);
      out.push(...sql.exec(query(batch.map(() => '?').join(','), limit - out.length + 1), ...fixed, ...batch).toArray() as Row[]);
      if (out.length > limit) throw new Error('capacity_observation_limited');
    }
    return out;
  };
  // Include disabled definitions too: the legacy quota fallback counts all
  // registered accounts in a host/provider family, not only eligible ones.
  const routes = keyed(definitions.map(a => a.label), CAPACITY_LIMITS.routes,
    (marks, n) => `SELECT * FROM fleet_routes WHERE account IN (${marks}) LIMIT ${n}`);
  const ids = machines.map(m => m.id);
  const launches = keyed(ids, CAPACITY_LIMITS.launches, (marks, n) => `SELECT l.id,l.account,l.machine_id,l.native_id,l.engine,l.state,l.updated_at,s.phase,s.last_activity_at,s.account AS native_account
    FROM fleet_launches l ${reservationNativeJoinSql(sql)}
    WHERE l.machine_id IN (${marks}) AND l.state IN ('queued','offered','starting','running','uncertain') LIMIT ${n}`);
  if (new Set(launches.map(l => l.engine)).size > CAPACITY_LIMITS.engines) throw new Error('capacity_observation_limited');
  const natives = keyed(ids, CAPACITY_LIMITS.natives, (marks, n) => `SELECT machine_id,native_id,account,phase,last_activity_at
    FROM fleet_sessions WHERE machine_id IN (${marks}) AND phase IN ('starting','running','broken','unknown','uncertain') LIMIT ${n}`);
  // Only after BOTH observation budgets succeeded may incarnation correlation
  // run, and only when an unbound starting/uncertain launch exists to correlate.
  const hasInFlight = launches.some(l => !l.native_id && (l.state === 'starting' || l.state === 'uncertain'));
  if (hasInFlight) {
    for (const native of natives) {
      native.inflight_launch_id = sql.exec(`SELECT ${INFLIGHT_LAUNCH_ID_SQL} AS inflight_launch_id
        FROM fleet_sessions s WHERE s.machine_id=? AND s.native_id=?`, native.machine_id, native.native_id).one().inflight_launch_id;
    }
  }
  const seen = keyed(ids, CAPACITY_LIMITS.hosts, (marks, n) => `SELECT machine_id,observed_at FROM fleet_catalog_census
    WHERE complete=1 AND observed_at>=? AND observed_at<=? AND machine_id IN (${marks}) LIMIT ${n}`,
    new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString());
  const byLabel = new Map(definitions.map(a => [a.label, a]));
  const familyAccounts = new Map<string, Set<string>>();
  for (const r of routes) {
    const a = byLabel.get(r.account)!;
    const key = capacityFamilyKey(r.machine_id, canonicalQuotaProvider(a.provider));
    if (!familyAccounts.has(key)) familyAccounts.set(key, new Set());
    familyAccounts.get(key)!.add(r.account);
  }
  return { now, definitions, machines, routes, launches, natives,
    completeCatalogs: new Map(seen.map(r => [r.machine_id, Date.parse(r.observed_at)])), familyAccounts };
}
