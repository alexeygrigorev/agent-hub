export const ACTIVE_LAUNCH_STATES_SQL = "('offered','starting','running','uncertain')";

export type ReservedLaunchContext = {
  live_ids: string[];
  stale_cutoff: string;
  idle_cutoff_ms: number;
  complete_catalogs: Set<string>;
  bound_native_id_sql: string;
  bound_native_created_at_sql: string;
};

export type ReservedLaunchCursor = { state: string; id: string } | null;

export function parseReservedLaunchCursor(raw: string | null): ReservedLaunchCursor {
  if (raw === null || raw === '') return null;
  const sep = raw.indexOf(':');
  if (sep < 1 || sep === raw.length - 1) throw new Error('invalid_reserved_launch_cursor');
  const state = raw.slice(0, sep);
  const id = raw.slice(sep + 1);
  if (!['offered', 'starting', 'running', 'uncertain'].includes(state) || id.length > 128 || /[\x00-\x1f\x7f]/.test(id))
    throw new Error('invalid_reserved_launch_cursor');
  return { state, id };
}

export function encodeReservedLaunchCursor(cursor: ReservedLaunchCursor): string | null {
  return cursor ? `${cursor.state}:${cursor.id}` : null;
}

function isIdle(row: any, context: ReservedLaunchContext): boolean {
  return row.phase === 'running' && context.complete_catalogs.has(String(row.machine_id))
    && typeof row.last_activity_at === 'string' && Number.isFinite(Date.parse(row.last_activity_at))
    && Date.parse(row.last_activity_at) <= context.idle_cutoff_ms;
}

function isUnobserved(row: any, context: ReservedLaunchContext): boolean {
  return ['running', 'uncertain'].includes(row.state) && row.phase === 'unobserved'
    && context.complete_catalogs.has(String(row.machine_id))
    // Uncertain launches may shed a reservation only through an exact
    // session-control binding; a mismatched native row is not terminal proof.
    && (row.state !== 'uncertain' || row.native_id == null || row.native_id === '');
}

/**
 * Shared accounting query for accountStatus and the operator's row-level read.
 * It selects at most 100 launch rows, with the same active/live/stale and
 * catalog-backed idle/unobserved predicate used for reserved capacity.
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
  let launchIndex = false;
  try {
    hasControlStore = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_control_sessions'").toArray().length > 0;
  } catch { /* Fleet-only stores have no session-control binding. */ }
  try {
    launchIndex = sql.exec("SELECT 1 FROM sqlite_master WHERE type='index' AND name='fleet_launch_account_state_id'").toArray().length > 0;
  } catch { /* Some query fixtures skip FleetService.init(). */ }
  const launchIndexHint = launchIndex ? ' INDEXED BY fleet_launch_account_state_id' : '';
  const hubSessionIdSql = hasControlStore
    ? `(SELECT h.id FROM session_control_sessions h WHERE h.fleet_launch_id=l.id ORDER BY h.updated_at DESC LIMIT 1)`
    : 'NULL';
  const whereCursor = cursor ? 'AND (l.state > ? OR (l.state = ? AND l.id > ?))' : '';
  const cursorBindings = cursor ? [cursor.state, cursor.state, cursor.id] : [];
  const rawRows = sql.exec(`SELECT l.id, l.machine_id, l.state, l.native_id, l.created_at, l.updated_at,
      ${hubSessionIdSql} AS hub_session_id, s.phase, s.last_activity_at, s.created_at_ms
    FROM fleet_launches l${launchIndexHint}
    LEFT JOIN fleet_sessions s ON s.machine_id=l.machine_id
      AND s.native_id=COALESCE(NULLIF(l.native_id, ''), ${context.bound_native_id_sql})
      AND (NULLIF(l.native_id, '') IS NOT NULL OR
        (s.engine=l.engine AND s.account=l.account AND s.created_at_ms=${context.bound_native_created_at_sql}))
    WHERE l.account = ? AND l.state IN ${ACTIVE_LAUNCH_STATES_SQL}
      AND EXISTS (SELECT 1 FROM json_each(?) live WHERE live.value=l.machine_id)
      AND (l.state = 'running' OR l.updated_at > ?) ${whereCursor}
    ORDER BY l.state, l.id LIMIT ?`, account, JSON.stringify(context.live_ids), context.stale_cutoff,
    ...cursorBindings, limit).toArray() as any[];
  const rows = rawRows.filter(row => row.state === 'running'
    ? (!isIdle(row, context) && !isUnobserved(row, context))
    : row.state === 'uncertain' ? !isUnobserved(row, context) : true)
    .map(row => ({
      id: String(row.id),
      machine: String(row.machine_id),
      state: String(row.state),
      native_id: row.native_id ?? null,
      hub_session_id: row.hub_session_id ?? null,
      created_at: row.created_at,
      updated_at: row.updated_at,
      phase: row.phase ?? null,
      last_activity_at: row.last_activity_at ?? null,
      created_at_ms: row.created_at_ms === null || row.created_at_ms === undefined ? null : Number(row.created_at_ms),
    }));
  const last = rawRows[rawRows.length - 1];
  const nextCursor = rawRows.length === limit && last
    ? encodeReservedLaunchCursor({ state: String(last.state), id: String(last.id) })
    : null;
  return { rows, raw_count: rawRows.length, next_cursor: nextCursor };
}

export function countReservedLaunches(sql: any, account: string, context: ReservedLaunchContext): number {
  let cursor: ReservedLaunchCursor = null;
  let count = 0;
  while (true) {
    const page = reservedLaunchPage(sql, account, context, 100, cursor);
    count += page.rows.length;
    if (!page.next_cursor) return count;
    cursor = parseReservedLaunchCursor(page.next_cursor);
  }
}
