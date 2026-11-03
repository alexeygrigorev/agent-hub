import type { SqlStorage } from '@cloudflare/workers-types';
import { engineProvider, normalizeProviderFamily } from './quota_admission';
import { FREE_POOL_PROVIDERS } from './pacing';

const DAY = 24 * 60 * 60 * 1000;
type MetricValue = { value: number | Record<string, number> | null; unit: string; reason?: string };
type WindowMetrics = Record<string, MetricValue>;

const known = (value: number, unit: string): MetricValue => ({ value, unit });
const unknown = (unit: string, reason: string): MetricValue => ({ value: null, unit, reason });
const safeNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

function rows(sql: SqlStorage, query: string, ...values: (string | number | null)[]): any[] {
  return sql.exec(query, ...values).toArray() as any[];
}

function engineMix(sql: SqlStorage, poolId: string, since: number, now: number): MetricValue {
  try {
    const data = rows(sql,
      `SELECT t.engine, SUM(t.total_tokens) AS tokens
       FROM tbm_reward_events r JOIN tbm_session_tokens t ON t.session_id = r.session_id
       WHERE r.pool_id = ? AND r.event_kind = 'session' AND r.timestamp_ms >= ? AND r.timestamp_ms <= ?
         AND r.tokens_known = 1 AND t.total_tokens > 0
       GROUP BY t.engine`, poolId, since, now);
    const totals = { free: 0, paid: 0, grok: 0 };
    let classified = 0;
    for (const row of data) {
      const count = safeNumber(row.tokens) ?? 0;
      const provider = typeof row.engine === 'string' ? normalizeProviderFamily(engineProvider(row.engine)) : null;
      if (provider === 'grok') { totals.grok += count; classified += count; }
      else if (provider && FREE_POOL_PROVIDERS.includes(provider)) { totals.free += count; classified += count; }
      else if (provider) { totals.paid += count; classified += count; }
    }
    if (!classified) return unknown('fraction', 'No token usage with a classifiable engine was recorded in this window.');
    return { value: { free: totals.free / classified, paid: totals.paid / classified, grok: totals.grok / classified }, unit: 'fraction', reason: 'Shares cover only token usage with a recorded, classifiable engine.' };
  } catch {
    return unknown('fraction', 'Engine-tagged token usage is unavailable.');
  }
}

function metricsForPool(sql: SqlStorage, pool: any, now: number, since: number, window: '24h' | '7d'): WindowMetrics {
  const pid = String(pool.pool_id);
  const rangeLabel = window;
  const metrics: WindowMetrics = {};
  // The controller's rollup row is an overwritten latest snapshot, not a complete
  // time series, so window rates are exposed only from their event-level sources.
  metrics.burn_rate_vs_pacing_target = unknown('ratio', `No historical pacing samples are retained for ${rangeLabel}; the current pool ratio is available from /api/quota/pacing.`);

  try {
    const resets = rows(sql,
      `SELECT remaining_tokens, remaining_percent, total_capacity_tokens, capacity_known, hard_floor_percent
       FROM tbm_pool_resets WHERE pool_id = ? AND reset_at_ms >= ? AND reset_at_ms <= ?`, pid, since, now);
    if (!resets.length) {
      metrics.leftover_quota_at_planned_reset = unknown('quota_units', `No reset snapshot was recorded in ${rangeLabel}.`);
    } else if (resets.some(r => Number(r.capacity_known) === 0)) {
      const values = resets.map(r => safeNumber(r.remaining_percent));
      if (values.some(v => v === null)) metrics.leftover_quota_at_planned_reset = unknown('percentage_points', 'A reset snapshot has unknown remaining percentage.');
      else metrics.leftover_quota_at_planned_reset = known(resets.reduce((sum, r) => sum + Math.max(0, Number(r.remaining_percent) - Number(r.hard_floor_percent || 0)), 0), 'percentage_points');
    } else if (resets.some(r => safeNumber(r.remaining_tokens) === null || safeNumber(r.total_capacity_tokens) === null)) {
      metrics.leftover_quota_at_planned_reset = unknown('tokens', 'A reset snapshot has unknown token capacity or remaining amount.');
    } else {
      metrics.leftover_quota_at_planned_reset = known(resets.reduce((sum, r) => sum + Math.max(0, Number(r.remaining_tokens) - Number(r.total_capacity_tokens) * Number(r.hard_floor_percent || 0) / 100), 0), 'tokens');
    }
  } catch {
    metrics.leftover_quota_at_planned_reset = unknown('quota_units', 'Reset snapshot storage is unavailable.');
  }

  metrics.reserve_floor_breaches = unknown('events', `No timestamped floor-breach history is retained for ${rangeLabel}.`);

  try {
    const reward = rows(sql,
      `SELECT SUM(work_units) AS work, SUM(tokens_burned) AS tokens, COUNT(*) AS n
       FROM tbm_reward_events WHERE pool_id = ? AND event_kind = 'session' AND efficiency_known = 1
         AND tokens_known = 1 AND tokens_burned > 0 AND work_units IS NOT NULL
         AND timestamp_ms >= ? AND timestamp_ms <= ?`, pid, since, now)[0] || {};
    const tokens = safeNumber(reward.tokens) ?? 0;
    const work = safeNumber(reward.work) ?? 0;
    metrics.useful_work_per_tokens = tokens > 0
      ? known((work * 1_000_000) / tokens, 'VU/MTok')
      : unknown('VU/MTok', `No verified outcome with measured tokens was recorded in ${rangeLabel}.`);
  } catch {
    metrics.useful_work_per_tokens = unknown('VU/MTok', 'Verified token reward data is unavailable.');
  }

  metrics.idle_wasted_quota_over_time = unknown('quota_units', `Only the current controller deficit is retained; no ${rangeLabel} history is available.`);
  metrics.engine_mix = engineMix(sql, pid, since, now);

  metrics.admission_rejects_rate = unknown('fraction', `Dispatch submission and rejection events are not retained for ${rangeLabel}.`);
  metrics.no_eligible_route_rate = unknown('fraction', `Authoritative no-eligible-route submission events are not retained for ${rangeLabel}.`);

  try {
    const outcome = rows(sql,
      `SELECT AVG((r.timestamp_ms - (julianday(s.created_at) - 2440587.5) * 86400000) / 1000.0) AS seconds, COUNT(*) AS n
       FROM tbm_reward_events r JOIN session_control_sessions s ON s.id = r.session_id
       WHERE r.pool_id = ? AND r.event_kind = 'session' AND r.work_units > 0
         AND r.timestamp_ms >= ? AND r.timestamp_ms <= ? AND s.created_at IS NOT NULL
         AND r.timestamp_ms >= (julianday(s.created_at) - 2440587.5) * 86400000`, pid, since, now)[0] || {};
    const seconds = safeNumber(outcome.seconds);
    metrics.time_to_useful_outcome = seconds !== null && Number(outcome.n) > 0
      ? known(seconds, 'seconds')
      : unknown('seconds', `No verified useful outcome with lifecycle timestamps was recorded in ${rangeLabel}.`);
  } catch {
    metrics.time_to_useful_outcome = unknown('seconds', 'Session lifecycle timestamps are unavailable.');
  }

  try {
    const tranches = rows(sql,
      `SELECT remaining_tokens, quantity_known, expires_at_ms, status, expired_unused_tokens,
              banked_burn_rate_tpm, updated_at_ms
       FROM tbm_banked_tranches WHERE pool_id = ?`, pid);
    const active = tranches.filter(t => t.status === 'active' && Number(t.expires_at_ms) > now);
    const unknownActive = active.some(t => Number(t.quantity_known) === 0 || safeNumber(t.remaining_tokens) === null);
    metrics.banked_remaining = !tranches.length
      ? unknown('tokens', 'No banked tranche inventory has been observed.')
      : unknownActive
      ? unknown('tokens', 'At least one active tranche has unknown token quantity.')
      : known(active.reduce((sum, t) => sum + (safeNumber(t.remaining_tokens) ?? 0), 0), 'tokens');
    const expiries = active.map(t => safeNumber(t.expires_at_ms)).filter((v): v is number => v !== null);
    metrics.time_to_expiry = expiries.length
      ? known(Math.max(0, (Math.min(...expiries) - now) / 1000), 'seconds')
      : unknown('seconds', 'No active tranche has a known expiration time.');
    const expired = tranches.filter(t => Number(t.expires_at_ms) >= since && Number(t.expires_at_ms) <= now);
    metrics.expired_unused = !tranches.length
      ? unknown('tokens', 'No banked tranche history is recorded.')
      : expired.some(t => (t.status === 'expired' || t.status === 'active') &&
        (Number(t.quantity_known) === 0 || safeNumber(t.status === 'expired' ? t.expired_unused_tokens : t.remaining_tokens) === null))
      ? unknown('tokens', 'At least one expired tranche has unknown token quantity.')
      : known(expired.reduce((sum, t) => {
        if (t.status === 'expired') return sum + (safeNumber(t.expired_unused_tokens) ?? 0);
        if (t.status === 'active') return sum + (safeNumber(t.remaining_tokens) ?? 0);
        return sum;
      }, 0), 'tokens');
    const activeRates = active.map(t => safeNumber(t.banked_burn_rate_tpm));
    metrics.banked_burn_rate = activeRates.length && activeRates.every(rate => rate !== null && rate > 0)
      ? known(activeRates.reduce((sum, value) => sum + (value ?? 0), 0), 'tokens/minute')
      : unknown('tokens/minute', active.length ? 'At least one active tranche has no measured banked token burn rate.' : 'No active banked tranche is available in this window.');
  } catch {
    for (const key of ['banked_remaining', 'time_to_expiry', 'expired_unused', 'banked_burn_rate']) {
      const unit = key === 'time_to_expiry' ? 'seconds' : key === 'banked_burn_rate' ? 'tokens/minute' : 'tokens';
      metrics[key] = unknown(unit, 'Banked tranche data is unavailable.');
    }
  }

  return metrics;
}

export function getTbmMetrics(sql: SqlStorage, now = Date.now()): unknown {
  const pools = rows(sql, "SELECT * FROM tbm_quota_pools WHERE pool_id != '*' ORDER BY provider, account, pool_id");
  const windows = [
    { id: '24h' as const, ms: DAY },
    { id: '7d' as const, ms: 7 * DAY },
  ];
  return {
    as_of: new Date(now).toISOString(),
    windows: Object.fromEntries(windows.map(w => [w.id, {
      start: new Date(now - w.ms).toISOString(), end: new Date(now).toISOString(),
      pools: pools.map(pool => ({
        pool_id: pool.pool_id, provider: pool.provider, account: pool.account,
        metrics: metricsForPool(sql, pool, now, now - w.ms, w.id),
      })),
    }])),
  };
}
