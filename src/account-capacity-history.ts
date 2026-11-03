const SAMPLE_INTERVAL_MS = 15 * 60_000;
export const ACCOUNT_CAPACITY_HISTORY_OBSERVATION_LIMIT = 128;
export class AccountCapacityHistoryObservationLimit extends Error {
  constructor(){super('account_capacity_history_observation_limited');this.name='AccountCapacityHistoryObservationLimit';}
}
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
  // This legacy/manual sampler refuses an incomplete retained population.
  // MAX(timestamp) across account-major storage and COUNT(*) both walk history.
  const retained=sql.exec(`SELECT account,observed_at_ms FROM fleet_account_capacity_history
    ORDER BY account,observed_at_ms LIMIT 129`).toArray() as any[];
  if(retained.length>ACCOUNT_CAPACITY_HISTORY_OBSERVATION_LIMIT)throw new AccountCapacityHistoryObservationLimit();
  const latest=retained.reduce((n,row)=>Math.max(n,Number(row.observed_at_ms)),Number.NEGATIVE_INFINITY);
  if(Number.isFinite(latest) && nowMs-latest<SAMPLE_INTERVAL_MS)return 0;
  const accounts=getAccounts();
  if(accounts.length>ACCOUNT_CAPACITY_HISTORY_OBSERVATION_LIMIT)throw new AccountCapacityHistoryObservationLimit();
  let recorded=0,totalRows=retained.length;
  for (const account of accounts) {
    if (typeof account?.label !== 'string' || !account.label ||
        !Number.isInteger(account.active) || account.active < 0 ||
        !Number.isInteger(account.reserved) || account.reserved < 0 ||
        !Number.isInteger(account.max_concurrency) || account.max_concurrency < 0 ||
        (account.native_live !== null && account.native_live !== undefined &&
          (!Number.isInteger(account.native_live) || account.native_live < 0))) continue;
    if (totalRows >= ACCOUNT_CAPACITY_HISTORY_MAX_TOTAL_ROWS) break;
    const accountRows=retained.filter(row=>row.account===account.label).length;
    if (accountRows >= ACCOUNT_CAPACITY_HISTORY_MAX_ROWS) continue;
    const inserted = sql.exec(`INSERT OR IGNORE INTO fleet_account_capacity_history
      (account, observed_at_ms, active, reserved, native_live, concurrency_limit)
      VALUES (?, ?, ?, ?, ?, ?) RETURNING account`, account.label, nowMs, account.active, account.reserved,
      account.native_live ?? null, account.max_concurrency).toArray().length;
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
    WHERE account = ? AND observed_at_ms >= ? AND observed_at_ms < ?
    ORDER BY observed_at_ms LIMIT ?`, account, afterMs===null?fromMs:Math.max(fromMs,afterMs+1), toMs, limit).toArray() as any[];
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
