/** Shared, sanitized quota collection journal. UI history and TBM consume this
 * store; their chart/pacing tables are projections, never separate collectors. */
import type { SqlStorage } from '@cloudflare/workers-types';
export const USAGE_RETENTION_MS = 32 * 86400000;
const initialized = new WeakSet<object>();
export function initUsageStore(sql: SqlStorage) {
  const identity = sql as object;
  if (initialized.has(identity)) return;
  sql.exec(`CREATE TABLE IF NOT EXISTS usage_quota_history (
    account TEXT NOT NULL, engine TEXT NOT NULL, provider TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL, quota_json TEXT NOT NULL,
    PRIMARY KEY(account,engine,provider,observed_at_ms))`);
  sql.exec('CREATE INDEX IF NOT EXISTS usage_quota_history_time ON usage_quota_history(observed_at_ms)');
  sql.exec('CREATE INDEX IF NOT EXISTS usage_quota_history_account_provider_time ON usage_quota_history(account,provider,observed_at_ms)');
  initialized.add(identity);
}
export function recordUsageQuota(sql: SqlStorage, account: string, provider: string, quota: any, at: number, prune = true) {
  initUsageStore(sql);
  if (!Number.isSafeInteger(at) || at < 0 || !quota || typeof quota !== 'object') return;
  const fields = ['percent_remaining','reset_at','present','rolling','error','credits_remaining','spend_used','spend_limit','unit'];
  const windows = Object.fromEntries(Object.entries(quota.windows || {}).slice(0,32).filter(([,w])=>w && typeof w==='object')
    .map(([name,w]:any)=>[name,Object.fromEntries(fields.filter(k=>Object.hasOwn(w,k)).map(k=>[k,w[k]]))]));
  const details = Object.fromEntries(['limit_reached','stale','credits_remaining','spend_used','spend_limit','unit','banked_resets','banked_resets_error']
    .filter(k=>Object.hasOwn(quota.details || {},k)).map(k=>[k,quota.details[k]]));
  const payload=JSON.stringify({status:quota.status,error:quota.error,windows,details,observed_at:new Date(at).toISOString()});
  let engines: string[]=[];
  try {
    const routes=sql.exec('SELECT engine FROM fleet_routes WHERE account=? LIMIT 129',account).toArray();
    if(routes.length>128)throw new Error('tbm_observation_limit:usage_routes');
    engines=routes.map((r:any)=>r.engine==='zcodex'?'codex':String(r.engine));
  } catch(error) { if(String((error as any)?.message).startsWith('tbm_observation_limit:'))throw error; /* schema bootstrap */ }
  if (!engines.length) engines=[provider==='zai'?'codex':provider==='go'?'opencode':provider];
  for(const engine of new Set(engines)) sql.exec(`INSERT INTO usage_quota_history VALUES(?,?,?,?,?) ON CONFLICT(account,engine,provider,observed_at_ms) DO UPDATE SET quota_json=excluded.quota_json WHERE usage_quota_history.quota_json<>excluded.quota_json`,account,engine,provider,at,payload);
  if(prune) pruneUsageQuota(sql,at);
}
export function pruneUsageQuota(sql: SqlStorage, at: number) {
  sql.exec(`DELETE FROM usage_quota_history WHERE (account,engine,provider,observed_at_ms) IN
    (SELECT account,engine,provider,observed_at_ms FROM usage_quota_history INDEXED BY usage_quota_history_time
     WHERE observed_at_ms<? ORDER BY observed_at_ms LIMIT 100)`,at-USAGE_RETENTION_MS);
}
