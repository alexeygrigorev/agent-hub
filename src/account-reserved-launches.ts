import { reservationNativeJoinSql, reservationHoldsCapacity, INFLIGHT_LAUNCH_ID_SQL } from './fleet-capacity-snapshot';

export const ACTIVE_LAUNCH_STATES_SQL = "('offered','starting','running','uncertain')";

export type ReservedLaunchContext = {
  live_ids: string[];
  reservation_cutoff: string;
  complete_catalogs: Map<string, number>;
};

/** Cutoffs and catalogs without the live-machine set: the pure eligibility
 * part shared by accountStatus and the bounded page. */
export type ReservedLaunchEligibility = Pick<ReservedLaunchContext, 'reservation_cutoff' | 'complete_catalogs'>;

export type ReservedLaunchCursor = { state: string; id: string; rowid?: number } | null;

export function parseReservedLaunchCursor(raw: string | null): ReservedLaunchCursor {
  if (raw === null || raw === '') return null;
  if (raw.startsWith('v2:')) {
    const match = /^v2:(offered|running|starting|uncertain):([0-9]+)$/.exec(raw);
    if (!match || !Number.isSafeInteger(Number(match[2]))) throw new Error('invalid_reserved_launch_cursor');
    return { state: match[1],id:'',rowid:Number(match[2]) };
  }
  const sep = raw.indexOf(':');
  if (sep < 1 || sep === raw.length - 1) throw new Error('invalid_reserved_launch_cursor');
  const state = raw.slice(0, sep);
  const id = raw.slice(sep + 1);
  if (!['offered', 'starting', 'running', 'uncertain'].includes(state) || id.length > 128 || /[\x00-\x1f\x7f]/.test(id))
    throw new Error('invalid_reserved_launch_cursor');
  return { state, id };
}

export function encodeReservedLaunchCursor(cursor: ReservedLaunchCursor): string | null {
  return cursor ? cursor.rowid === undefined ? `${cursor.state}:${cursor.id}` : `v2:${cursor.state}:${cursor.rowid}` : null;
}

/** The single reserved-eligibility predicate: observed-live natives release,
 * reservations hold only within TTL and release early on a fresh complete
 * census. accountStatus (snapshot and query branches) and reservedLaunchPage
 * count with it. */
export function isEligibleReservedLaunch(
  row: any,
  eligibility: ReservedLaunchEligibility,
  inFlightMatchedIds?: ReadonlySet<string>,
): boolean {
  return reservationHoldsCapacity(row, eligibility.complete_catalogs, eligibility.reservation_cutoff, inFlightMatchedIds);
}

/**
 * Shared accounting query for accountStatus and the operator's row-level read.
 * It selects at most 100 launch rows, with the same TTL/live/census predicate
 * used for reserved capacity (reservationHoldsCapacity).
 */
export function reservedLaunchPage(
  sql: any,
  account: string,
  context: ReservedLaunchContext,
  limit: number,
  cursor: ReservedLaunchCursor = null,
): { rows: any[]; raw_count: number; next_cursor: string | null } {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('invalid_reserved_launch_limit');
  if (!context.live_ids.length) return { rows: [], raw_count: 0, next_cursor: null };

  let hasControlStore = false;
  try { hasControlStore = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0; }
  catch { /* Fleet-only stores have no session-control binding. */ }
  if (context.live_ids.length > 64) throw new Error('capacity_observation_limited');
  const natives = sql.exec(`SELECT machine_id,native_id FROM fleet_sessions INDEXED BY fleet_session_account_phase
    WHERE account=? AND phase IN ('starting','running','broken','unknown','uncertain') LIMIT 257`,account).toArray() as any[];
  if (natives.length > 256) throw new Error('capacity_observation_limited');
  const live = new Set(context.live_ids);
  const inFlightMatchedIds = new Set<string>();
  for (const native of natives.filter(row=>live.has(String(row.machine_id)))) {
    const match = sql.exec(`SELECT ${INFLIGHT_LAUNCH_ID_SQL} AS inflight_launch_id FROM fleet_sessions s
      WHERE s.machine_id=? AND s.native_id=?`,native.machine_id,native.native_id).toArray()[0] as any;
    if (typeof match?.inflight_launch_id === 'string') inFlightMatchedIds.add(match.inflight_launch_id);
  }
  let after = cursor?.rowid ?? 0;
  if (cursor && cursor.rowid === undefined) {
    const legacy = sql.exec('SELECT rowid AS source_rowid FROM fleet_launches WHERE id=? AND account=? AND state=?',cursor.id,account,cursor.state).toArray()[0] as any;
    if (!legacy) throw new Error('invalid_reserved_launch_cursor');
    after = Number(legacy.source_rowid);
  }
  const candidates: any[] = [];
  let continuation: ReservedLaunchCursor = null;
  for (const state of ['offered','running','starting','uncertain']) {
    if (cursor && state < cursor.state) continue;
    const page = sql.exec(`SELECT rowid AS source_rowid,id,account,machine_id,state,native_id,created_at,updated_at
      FROM fleet_launches INDEXED BY fleet_launch_account_state
      WHERE account=? AND state=? AND rowid>? ORDER BY rowid LIMIT ?`,account,state,cursor?.state === state ? after : 0,limit-candidates.length).toArray() as any[];
    candidates.push(...page);
    if (candidates.length === limit) {
      const last = candidates[candidates.length-1];
      continuation = { state:String(last.state),id:String(last.id),rowid:Number(last.source_rowid) };
      break;
    }
  }
  // Hydrate only the bounded source page. The canonical native-incarnation
  // join and eligibility predicate are unchanged; live/TTL postfilters no
  // longer force a scan through old reservations before LIMIT applies.
  const nativeJoin = reservationNativeJoinSql(sql);
  const rows = candidates.filter(row=>live.has(String(row.machine_id)) && row.updated_at > context.reservation_cutoff)
    .flatMap(row=>sql.exec(`SELECT l.id,l.account,l.machine_id,l.state,l.native_id,l.created_at,l.updated_at,
      s.phase,s.last_activity_at,s.created_at_ms,s.account AS native_account
      FROM fleet_launches l ${nativeJoin} WHERE l.id=?`,row.id).toArray() as any[])
    .filter(row=>isEligibleReservedLaunch(row,context,inFlightMatchedIds))
    .map(row=>({ id:String(row.id),machine:String(row.machine_id),state:String(row.state),native_id:row.native_id ?? null,
      hub_session_id:hasControlStore ? (sql.exec('SELECT id FROM session_control_sessions WHERE fleet_launch_id=? LIMIT 1',row.id).toArray()[0] as any)?.id ?? null : null,
      created_at:row.created_at,updated_at:row.updated_at,phase:row.phase ?? null,last_activity_at:row.last_activity_at ?? null,
      created_at_ms:row.created_at_ms == null ? null : Number(row.created_at_ms) }));
  return { rows,raw_count:candidates.length,next_cursor:encodeReservedLaunchCursor(continuation) };
}

export function countReservedLaunches(sql: any, account: string, context: ReservedLaunchContext): number {
  let cursor: ReservedLaunchCursor = null;
  let count = 0;
  for (let pageNumber=0;pageNumber<10;pageNumber++) {
    const page = reservedLaunchPage(sql, account, context, 100, cursor);
    count += page.rows.length;
    if (!page.next_cursor) return count;
    cursor = parseReservedLaunchCursor(page.next_cursor);
  }
  throw new Error('capacity_observation_limited');
}
