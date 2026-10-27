import type { SqlStorage } from '@cloudflare/workers-types';

export type GrokMode = 'minimal' | 'disabled' | 'normal';
export const grokMode = (value: unknown): value is GrokMode => ['minimal', 'disabled', 'normal'].includes(value as string);
export function ensureGrokPolicy(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS grok_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL)`);
  sql.exec(`CREATE TABLE IF NOT EXISTS grok_policy_audit (id TEXT PRIMARY KEY, previous_mode TEXT NOT NULL, mode TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)`);
}
/** Stored preferences never grant execution authority. Alexey t1621u disables
 * all Grok routes, including asks and reseller models; core model policy owns
 * that admission fence independently of these operator preference records. */
export function readGrokPolicy(sql: SqlStorage) {
  ensureGrokPolicy(sql);
  const row: any = sql.exec('SELECT mode, reason, actor, updated_at FROM grok_policy WHERE singleton=1').toArray()[0];
  const mode: GrokMode = grokMode(row?.mode) ? row.mode : 'disabled';
  return { mode, effective_mode: 'disabled', factor: 0, coding_allowed: false, ask_last_resort: false,
    enabled: false, policy_reason: 'model_disabled_by_policy',
    reason: row?.reason ?? null, actor: row?.actor ?? null, updated_at: row?.updated_at ?? null };
}
