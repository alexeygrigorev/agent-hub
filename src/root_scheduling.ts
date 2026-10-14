// Root scheduling policy: persisted enable/disable flag for root-run scheduling.
//
// The flag lives in a single-row SQLite table so it is durable across DO restarts
// and readable inside the same storage transaction that creates offers and claims
// (no flag-check race). The safe default is enabled=true, which preserves the
// pre-flag scheduler behavior exactly.
//
// Disabling atomically prevents:
//   - new root-run slot jobs and offers from the scheduler tick,
//   - new offers of existing root-run jobs (fallback/decline paths),
//   - new claims of root-run jobs (including already-offered ones) and takeovers,
//   - root-run START transitions (claimed -> running).
// While disabled, root-run offers are frozen and hidden from host offer lists.
// Jobs already running keep renewing and finishing; the flag never kills a live root.

export interface RootSchedulingState {
  enabled: boolean;
  updated_at: string | null;
}

export const ROOT_SCHEDULING_TABLE_SQL = `
CREATE TABLE IF NOT EXISTS root_scheduling (
  id INTEGER PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT
)`;

export function ensureRootScheduling(sql: SqlStorage, nowIso?: string): void {
  sql.exec(ROOT_SCHEDULING_TABLE_SQL);
  sql.exec(
    'INSERT OR IGNORE INTO root_scheduling (id, enabled, updated_at) VALUES (1, 1, ?)',
    nowIso ?? new Date().toISOString()
  );
}

export function getRootScheduling(sql: SqlStorage): RootSchedulingState {
  const rows = sql.exec('SELECT enabled, updated_at FROM root_scheduling WHERE id = 1').toArray();
  const row = rows[0] as any;
  if (!row) return { enabled: true, updated_at: null };
  return {
    enabled: row.enabled === 1 || row.enabled === true,
    updated_at: (row.updated_at as string) ?? null
  };
}

// Read used inside offer/claim/start transactions. Defaults to enabled when the
// table is missing so an uninitialized database keeps the current behavior.
export function isRootSchedulingEnabled(sql: SqlStorage): boolean {
  const rows = sql.exec('SELECT enabled FROM root_scheduling WHERE id = 1').toArray();
  const row = rows[0] as any;
  if (!row) return true;
  return row.enabled === 1 || row.enabled === true;
}

// Idempotent set: repeated writes of the same value leave state (and updated_at)
// untouched. Callers wrap this in the DO storage transaction for atomicity.
export function setRootScheduling(sql: SqlStorage, enabled: boolean, nowIso: string): RootSchedulingState {
  const current = getRootScheduling(sql);
  if (current.enabled === enabled) return current;
  sql.exec(
    'UPDATE root_scheduling SET enabled = ?, updated_at = ? WHERE id = 1',
    enabled ? 1 : 0,
    nowIso
  );
  return { enabled, updated_at: nowIso };
}
