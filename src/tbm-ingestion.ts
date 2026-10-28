import { initUsageStore, recordUsageQuota } from './usage-store';
/** TBM projections of the existing account quota observations. Unknown tokens stay unknown. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { canonicalQuotaProvider, canonicalAccountLabel, snapshotRouteForAccount } from './quota';
const HOUR = 3600000, DAY = 24 * HOUR;
export const tbmPoolId = (provider: string, account: string, window: string) => `${provider}:${account}:${window}`;
export const WINDOW_CYCLE_MS: Record<string, number> = {
  '5h': 5 * HOUR, five_hour: 5 * HOUR, '5hour': 5 * HOUR, hourly: HOUR, '1h': HOUR,
  daily: DAY, day: DAY, weekly: 7 * DAY, week: 7 * DAY, '7d': 7 * DAY, '30d': 30 * DAY,
};
export function inferCycleMs(name: string, previous: number | null, reset: number | null): number | null {
  if (previous !== null && reset !== null && reset > previous && reset - previous <= 366 * DAY) return reset - previous;
  if ((name === 'monthly' || name === 'month') && reset !== null) {
    const d = new Date(reset);
    const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 0)).getUTCDate();
    return reset - Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, Math.min(d.getUTCDate(), lastDay),
      d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds());
  }
  return WINDOW_CYCLE_MS[name] ?? null;
}
const nonnegative = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0;
export function resolveTokenCapacity(w: any): { known: boolean; total: number | null; remaining: number | null } {
  const unknown = { known: false, total: null, remaining: null };
  if (w?.unit !== 'tokens') return unknown;
  const total = nonnegative(w.spend_limit) && w.spend_limit > 0 ? w.spend_limit :
    nonnegative(w.spend_used) && nonnegative(w.credits_remaining) ? w.spend_used + w.credits_remaining : null;
  const remaining = nonnegative(w.credits_remaining) ? w.credits_remaining :
    total !== null && nonnegative(w.spend_used) ? Math.max(0, total - w.spend_used) : null;
  return { known: total !== null && total > 0 && remaining !== null,
    total, remaining: remaining !== null && total !== null ? Math.min(total, remaining) : remaining };
}

/** A provider-wide snapshot must never be copied onto several independent accounts. */
export function accountsForHostProvider(sql: SqlStorage, host: string, provider: string): string[] {
  const canonical = canonicalQuotaProvider(provider);
  const eligible = (rows: any[]) => rows.filter(row => snapshotRouteForAccount(row.label, row.provider, { [provider]: true }) === true);
  const rows = eligible(sql.exec(`SELECT DISTINCT a.label,a.provider FROM fleet_routes r JOIN fleet_accounts a
    ON r.account=a.label WHERE r.machine_id=? ORDER BY a.label`, host).toArray());
  if (rows.length === 1) return [canonicalAccountLabel(rows[0].label)];
  if (rows.length > 1) return [];
  const accounts = eligible(sql.exec('SELECT label,provider FROM fleet_accounts ORDER BY label').toArray());
  return accounts.length === 1 ? [canonicalAccountLabel(accounts[0].label)] : accounts.length === 0
    && canonical === provider ? [provider] : [];

}
export interface TbmSyncCounts { pools: number; tranches: number }
interface WindowIngest { provider: string; account: string; windowName: string; window: any }
export function syncWindowsIntoTbm(sql: SqlStorage, windows: WindowIngest[], at: number): TbmSyncCounts {
  const counts = { pools: 0, tranches: 0 };
  for (const { provider, account, windowName, window: w } of windows) {
    if (w?.present !== true || w.error === 'unavailable') continue;
    const reset = typeof w.reset_at === 'string' ? Date.parse(w.reset_at) : NaN;
    const cap = resolveTokenCapacity(w);
    const pct = nonnegative(w.percent_remaining) && w.percent_remaining <= 100 ? w.percent_remaining :
      cap.known ? cap.remaining! / cap.total! * 100 : null;
    if (!Number.isFinite(reset) || reset <= 0 || pct === null) continue;
    const id = tbmPoolId(provider, account, windowName);
    const old: any = sql.exec('SELECT * FROM tbm_quota_pools WHERE pool_id=?', id).toArray()[0];
    if (old && old.last_updated_ms >= at) continue;
    const correction = old && at < old.reset_at_ms;
    const cycle = (old?.reset_at_ms === reset || correction) && old?.cycle_ms > 0 ? old.cycle_ms
      : inferCycleMs(windowName, old?.reset_at_ms <= at ? old.reset_at_ms : null, reset);
    const unit = cap.known ? 'tokens' : 'percent';
    let burn = 0;
    if (old && old.reset_at_ms === reset && at > old.last_updated_ms) {
      const prior = cap.known && old.capacity_known === 1 ? old.remaining_tokens : old.remaining_percent;
      const next = cap.known ? cap.remaining : pct;
      if (nonnegative(prior) && next !== null) burn = Math.max(0, prior - next) / ((at - old.last_updated_ms) / 60000);
    }
    sql.exec(`INSERT INTO tbm_quota_pools (pool_id,provider,account,total_capacity_tokens,remaining_tokens,
      headroom_percentage,reset_at_ms,last_updated_ms,cycle_ms,window_type,remaining_percent,capacity_known,quota_unit,current_burn_rate_tpm)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pool_id) DO UPDATE SET
      total_capacity_tokens=excluded.total_capacity_tokens,remaining_tokens=excluded.remaining_tokens,
      headroom_percentage=excluded.headroom_percentage,reset_at_ms=excluded.reset_at_ms,last_updated_ms=excluded.last_updated_ms,
      cycle_ms=excluded.cycle_ms,window_type=excluded.window_type,remaining_percent=excluded.remaining_percent,
      capacity_known=excluded.capacity_known,quota_unit=excluded.quota_unit,current_burn_rate_tpm=excluded.current_burn_rate_tpm`,
      id,provider,account,cap.known ? cap.total! : 0,cap.known ? cap.remaining! : 0,pct,reset,at,cycle,windowName,pct,cap.known ? 1 : 0,unit,burn);
    counts.pools++;
  }
  return counts;
}

/** A banked full-reset grant has a real deadline but does not report a token quantity. */
function syncBanked(sql: SqlStorage, provider: string, account: string, quota: any, at: number): number {
  const grants = quota?.details?.banked_resets;
  if (!Array.isArray(grants)) return 0;
  const pools: any[] = sql.exec('SELECT pool_id FROM tbm_quota_pools WHERE provider=? AND account=? ORDER BY cycle_ms DESC,pool_id', provider,account).toArray();
  if (!pools.length) return 0;
  const occurrences = new Map<number, number>();
  let count = 0;
  const ids: string[] = [];
  for (const grant of [...grants].sort((a,b) => String(a.expires_at).localeCompare(String(b.expires_at)))) {
    const expires = Date.parse(grant.expires_at);
    if (!Number.isFinite(expires)) continue;
    const index = occurrences.get(expires) ?? 0;
    occurrences.set(expires,index+1);
    const id = `bank:${provider}:${account}:${expires}:${index}`;
    ids.push(id);
    const state = expires <= at ? 'expired' : grant.available === true ? 'active' : 'exhausted';
    sql.exec(`INSERT INTO tbm_banked_tranches (tranche_id,pool_id,provider,label,allocated_tokens,remaining_tokens,
      granted_at_ms,expires_at_ms,status,updated_at_ms,quantity_known) VALUES (?,?,?,?,0,0,?,?,?,?,0)
      ON CONFLICT(tranche_id) DO UPDATE SET status=CASE
        WHEN tbm_banked_tranches.status='active' AND tbm_banked_tranches.expires_at_ms<=excluded.updated_at_ms THEN 'active'
        WHEN tbm_banked_tranches.status='expired' THEN 'expired' ELSE excluded.status END,updated_at_ms=excluded.updated_at_ms
      WHERE excluded.updated_at_ms > tbm_banked_tranches.updated_at_ms`,
      id,pools[0].pool_id,provider,grant.label,at,expires,state,at);
    count++;
  }
  // An explicit complete grant list can mark a consumed grant; omission means unknown.
  const existing: any[] = sql.exec("SELECT tranche_id FROM tbm_banked_tranches WHERE pool_id=? AND quantity_known=0 AND status='active' AND expires_at_ms>? AND updated_at_ms<?",pools[0].pool_id,at,at).toArray();
  for (const row of existing) if (!ids.includes(row.tranche_id)) sql.exec("UPDATE tbm_banked_tranches SET status='exhausted',updated_at_ms=? WHERE tranche_id=?",at,row.tranche_id);
  return count;
}
export function syncTbmFromServerAccounts(sql: SqlStorage, accounts: {account: string; provider: string; quota: any}[], at: number): TbmSyncCounts {
  const result = { pools: 0, tranches: 0 };
  for (const row of accounts) {
    const provider = canonicalQuotaProvider(row.provider), account = canonicalAccountLabel(row.account);
    recordUsageQuota(sql,account,provider,row.quota,at);
    // Read back the shared persisted measurement. Never feed TBM a private
    // latest-only copy that the dashboard cannot see.
    const stored:any=sql.exec('SELECT quota_json FROM usage_quota_history WHERE account=? AND provider=? AND observed_at_ms=? LIMIT 1',account,provider,at).toArray()[0];
    const quota=stored ? JSON.parse(stored.quota_json) : null;
    if (quota?.status !== 'ok') continue;
    const windows = Object.entries(quota.windows ?? {}).map(([windowName,window]) => ({provider,account,windowName,window}));
    const added = syncWindowsIntoTbm(sql,windows,at);
    result.pools += added.pools;
    result.tranches += syncBanked(sql,provider,account,quota,at);
  }
  return result;
}
export function syncTbmFromHostSnapshot(sql: SqlStorage, host: string, routes: Record<string, any>, at: number): TbmSyncCounts {
  return syncTbmFromServerAccounts(sql,Object.entries(routes).flatMap(([provider,quota]) =>
    accountsForHostProvider(sql,host,provider).map(account => ({provider,account,quota}))),at);
}

/** Bootstrap and reconcile existing authoritative per-account observations; do not refresh their age. */
export function syncStoredTbmSnapshots(sql: SqlStorage, now: number): void {
  initUsageStore(sql);
  const rows: any[] = sql.exec(`SELECT account,provider,captured_at AS observed_at,quota_json FROM server_quota_snapshots
    UNION ALL SELECT account,service AS provider,observed_at,quota_json FROM fleet_subscriptions
    ORDER BY observed_at ASC`).toArray();
  for (const row of rows) {
    const at = Date.parse(row.observed_at);
    if (!Number.isFinite(at) || at > now || now-at >= 15*60000) continue;
    let quota: any;
    try { quota = JSON.parse(row.quota_json); } catch { continue; }
    recordUsageQuota(sql,canonicalAccountLabel(row.account),canonicalQuotaProvider(row.provider),quota,at);
  }
  const history:any[]=sql.exec('SELECT account,provider,observed_at_ms,quota_json FROM usage_quota_history WHERE observed_at_ms>=? AND observed_at_ms<=? ORDER BY observed_at_ms',now-15*60000,now).toArray();
  for(const row of history) syncTbmFromServerAccounts(sql,[{account:row.account,provider:row.provider,quota:JSON.parse(row.quota_json)}],row.observed_at_ms);
}
