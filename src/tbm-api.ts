import type { SqlStorage } from '@cloudflare/workers-types';
import { loadProviderPacing } from './pacing';

const reply = (data: unknown, status = 200) => Response.json(data, {
  status, headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
});

/** Caller authentication and cookie CSRF checks run in the Hub mount before this handler. */
export async function handleTbmApi(request: Request, sql: SqlStorage, now = Date.now()): Promise<Response> {
  const path = new URL(request.url).pathname;
  const writable = path === '/api/quota/kill-switch';
  if (request.method !== 'GET' && !(writable && request.method === 'POST')) {
    return reply({ error: 'method_not_allowed' }, 405);
  }
  const as_of = new Date(now).toISOString();
  if (path === '/api/quota/pacing') {
    const pools = sql.exec("SELECT * FROM tbm_quota_pools WHERE pool_id != '*' ORDER BY provider, account, pool_id").toArray();
    const tickTable = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='tbm_tick_state'").toArray().length;
    const tick: any = tickTable ? sql.exec('SELECT * FROM tbm_tick_state WHERE id=1').toArray()[0] : null;
    return reply({ as_of, pools, last_tick_ms: tick?.last_tick_ms ?? null,
      controller: sql.exec('SELECT * FROM tbm_controller_state ORDER BY pool_id').toArray(),
      routing_factors: Object.fromEntries(loadProviderPacing(sql, now)) });
  }
  if (path === '/api/quota/banked') {
    return reply({ as_of, tranches: sql.exec('SELECT * FROM tbm_banked_tranches ORDER BY expires_at_ms, tranche_id LIMIT 1000').toArray() });
  }
  if (path === '/api/quota/rewards') {
    return reply({ as_of,
      events: sql.exec('SELECT * FROM tbm_reward_events ORDER BY timestamp_ms DESC LIMIT 100').toArray(),
      rollups: sql.exec('SELECT * FROM tbm_metrics_rollup ORDER BY end_time_ms DESC LIMIT 100').toArray() });
  }
  if (request.method === 'POST') {
    // Bound the stream itself: Content-Length is optional and untrusted.
    const reader = request.body?.getReader();
    if (!reader) return reply({ error: 'invalid_body' }, 400);
    let length = 0;
    const chunks: Uint8Array[] = [];
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > 2048) { await reader.cancel(); return reply({ error: 'body_too_large' }, 413); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    let body: any;
    try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)); }
    catch { return reply({ error: 'invalid_body' }, 400); }
    if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.active !== 'boolean' ||
      Object.keys(body).some(key => !['active', 'reason'].includes(key)) ||
      (body.reason !== undefined && (typeof body.reason !== 'string' || body.reason.length > 280))) {
      return reply({ error: 'invalid_kill_switch' }, 400);
    }
    sql.exec(`INSERT INTO tbm_quota_pools
      (pool_id, provider, account, total_capacity_tokens, remaining_tokens, headroom_percentage,
       reset_at_ms, kill_switch_active, kill_switch_reason, kill_switch_updated_at, last_updated_ms)
      VALUES ('*', '*', '*', 1, 1, 100, ?, ?, ?, ?, ?)
      ON CONFLICT(pool_id) DO UPDATE SET kill_switch_active=excluded.kill_switch_active,
        kill_switch_reason=excluded.kill_switch_reason, kill_switch_updated_at=excluded.kill_switch_updated_at,
        last_updated_ms=excluded.last_updated_ms`, now, body.active ? 1 : 0, body.reason ?? 'operator', as_of, now);
  }
  const row: any = sql.exec("SELECT kill_switch_active, kill_switch_reason, kill_switch_updated_at FROM tbm_quota_pools WHERE pool_id='*'").toArray()[0];
  return reply({ as_of, active: Boolean(row?.kill_switch_active), reason: row?.kill_switch_reason ?? null,
    updated_at: row?.kill_switch_updated_at ?? null });
}
