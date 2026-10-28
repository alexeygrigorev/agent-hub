/** Shared, sanitized quota collection journal. UI history and TBM consume this
 * store; their chart/pacing tables are projections, never separate collectors. */
import type { SqlStorage } from '@cloudflare/workers-types';
export const USAGE_RETENTION_MS = 32 * 86400000;
export function initUsageStore(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS usage_quota_history (
    account TEXT NOT NULL, engine TEXT NOT NULL, provider TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL, quota_json TEXT NOT NULL,
    PRIMARY KEY(account,engine,provider,observed_at_ms))`);
  sql.exec('CREATE INDEX IF NOT EXISTS usage_quota_history_time ON usage_quota_history(observed_at_ms)');
}
export function recordUsageQuota(sql: SqlStorage, account: string, provider: string, quota: any, at: number) {
  initUsageStore(sql);
  if (!Number.isSafeInteger(at) || at < 0 || !quota || typeof quota !== 'object') return;
  const fields = ['percent_remaining','reset_at','present','rolling','error','credits_remaining','spend_used','spend_limit','unit'];
  const windows = Object.fromEntries(Object.entries(quota.windows || {}).slice(0,32).filter(([,w])=>w && typeof w==='object')
    .map(([name,w]:any)=>[name,Object.fromEntries(fields.filter(k=>Object.hasOwn(w,k)).map(k=>[k,w[k]]))]));
  const details = Object.fromEntries(['limit_reached','stale','credits_remaining','spend_used','spend_limit','unit','banked_resets']
    .filter(k=>Object.hasOwn(quota.details || {},k)).map(k=>[k,quota.details[k]]));
  const payload=JSON.stringify({status:quota.status,error:quota.error,windows,details,observed_at:new Date(at).toISOString()});
  let engines: string[]=[];
  try { engines=sql.exec('SELECT DISTINCT engine FROM fleet_routes WHERE account=?',account).toArray().map((r:any)=>r.engine==='zcodex'?'codex':String(r.engine)); } catch { /* schema bootstrap */ }
  if (!engines.length) engines=[provider==='zai'?'codex':provider==='go'?'opencode':provider];
  for(const engine of new Set(engines)) sql.exec(`INSERT INTO usage_quota_history VALUES(?,?,?,?,?) ON CONFLICT(account,engine,provider,observed_at_ms) DO UPDATE SET quota_json=excluded.quota_json WHERE usage_quota_history.quota_json<>excluded.quota_json`,account,engine,provider,at,payload);
  sql.exec('DELETE FROM usage_quota_history WHERE observed_at_ms<?',at-USAGE_RETENTION_MS);
}
