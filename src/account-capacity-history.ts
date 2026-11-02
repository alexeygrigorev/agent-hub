const SAMPLE_INTERVAL_MS = 15 * 60_000;
export const ACCOUNT_CAPACITY_HISTORY_MAX_ROWS = 4096;
export const ACCOUNT_CAPACITY_HISTORY_MAX_TOTAL_ROWS = 32768;
export const ACCOUNT_CAPACITY_HISTORY_MAX_WINDOW_MS = 31 * 24 * 60 * 60_000;

export function initAccountCapacityHistory(sql: any): void {
  sql.exec(`CREATE TABLE IF NOT EXISTS fleet_account_capacity_history (
    account TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    active INTEGER NOT NULL,
    reserved INTEGER NOT NULL,
    native_live INTEGER,
    concurrency_limit INTEGER NOT NULL,
    PRIMARY KEY (account, observed_at_ms)
  )`);
  sql.exec('CREATE INDEX IF NOT EXISTS fleet_account_capacity_history_window ON fleet_account_capacity_history(account, observed_at_ms)');
}

/** Record one bounded fleet-wide observation at most every fifteen minutes. */
export function sampleAccountCapacityHistory(
  sql: any,
  nowMs: number,
  getAccounts: () => any[],
): number {
  initAccountCapacityHistory(sql);
  const latest = sql.exec('SELECT MAX(observed_at_ms) AS observed_at_ms FROM fleet_account_capacity_history').one()?.observed_at_ms;
  if (latest !== null && latest !== undefined && Number.isFinite(Number(latest)) && nowMs - Number(latest) < SAMPLE_INTERVAL_MS) return 0;

  let recorded = 0;
  let totalRows = Number(sql.exec('SELECT COUNT(*) AS n FROM fleet_account_capacity_history').one()?.n ?? 0);
  for (const account of getAccounts()) {
    if (typeof account?.label !== 'string' || !account.label ||
        !Number.isInteger(account.active) || account.active < 0 ||
        !Number.isInteger(account.reserved) || account.reserved < 0 ||
        !Number.isInteger(account.max_concurrency) || account.max_concurrency < 0 ||
        (account.native_live !== null && account.native_live !== undefined &&
          (!Number.isInteger(account.native_live) || account.native_live < 0))) continue;
    if (totalRows >= ACCOUNT_CAPACITY_HISTORY_MAX_TOTAL_ROWS) break;
    const accountRows = Number(sql.exec('SELECT COUNT(*) AS n FROM fleet_account_capacity_history WHERE account = ?', account.label).one()?.n ?? 0);
    if (accountRows >= ACCOUNT_CAPACITY_HISTORY_MAX_ROWS) continue;
    const inserted = sql.exec(`INSERT OR IGNORE INTO fleet_account_capacity_history
      (account, observed_at_ms, active, reserved, native_live, concurrency_limit)
      VALUES (?, ?, ?, ?, ?, ?)`, account.label, nowMs, account.active, account.reserved,
      account.native_live ?? null, account.max_concurrency).rowsWritten;
    const added = inserted > 0 ? 1 : 0;
    recorded += added;
    totalRows += added;
  }
  return recorded;
}

export function readAccountCapacityHistory(
  sql: any,
  account: string,
  fromMs: number,
  toMs: number,
  limit: number,
  afterMs: number | null = null,
): { rows: any[]; next_cursor: number | null } {
  if (!account || !Number.isSafeInteger(fromMs) || !Number.isSafeInteger(toMs) || fromMs >= toMs ||
      toMs - fromMs > ACCOUNT_CAPACITY_HISTORY_MAX_WINDOW_MS || !Number.isInteger(limit) || limit < 1 || limit > 100 ||
      (afterMs !== null && (!Number.isSafeInteger(afterMs) || afterMs < fromMs || afterMs >= toMs)))
    throw new Error('invalid_capacity_history_query');
  const exists = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_account_capacity_history'").toArray().length > 0;
  if (!exists) return { rows: [], next_cursor: null };
  const rows = sql.exec(`SELECT observed_at_ms, active, reserved, native_live, concurrency_limit
    FROM fleet_account_capacity_history
    WHERE account = ? AND observed_at_ms >= ? AND observed_at_ms < ? AND (? IS NULL OR observed_at_ms > ?)
    ORDER BY observed_at_ms LIMIT ?`, account, fromMs, toMs, afterMs, afterMs, limit).toArray() as any[];
  const next = rows.length === limit ? Number(rows[rows.length - 1].observed_at_ms) : null;
  return {
    rows: rows.map(row => ({
      observed_at: new Date(Number(row.observed_at_ms)).toISOString(),
      active: Number(row.active),
      reserved: Number(row.reserved),
      native_live: row.native_live === null ? null : Number(row.native_live),
      limit: Number(row.concurrency_limit),
    })),
    next_cursor: next,
  };
}
