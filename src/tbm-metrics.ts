import type { SqlStorage } from '@cloudflare/workers-types';
import { hardFloorTokens, softReserveTokens, poolEffectiveRemaining, isPercentPool, type TbmPoolRow } from './pacing';

const DAY = 86400000;
const MINUTE = 60000;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
const mean = (xs: number[]) => xs.length ? sum(xs) / xs.length : null;
const metric = (value: unknown, unit: string, source: string, reason: string | null = null) => ({ value, unit, source, reason: value === null ? reason ?? 'no_observations' : null });

export function ensureTbmMetricsSchema(sql: SqlStorage) {
  sql.exec(`CREATE TABLE IF NOT EXISTS tbm_catalog_samples (
    pool_id TEXT NOT NULL, observed_at_ms INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(pool_id, observed_at_ms))`);
  sql.exec(`CREATE TABLE IF NOT EXISTS tbm_admission_events (
    request_id TEXT PRIMARY KEY, pool_id TEXT, outcome TEXT NOT NULL CHECK(outcome IN ('accepted','rejected','no_eligible_route','queued')),
    timestamp_ms INTEGER NOT NULL)`);
  sql.exec('CREATE INDEX IF NOT EXISTS idx_tbm_admission_time ON tbm_admission_events(timestamp_ms,pool_id)');
  sql.exec(`CREATE TABLE IF NOT EXISTS tbm_banked_consumption (
    id INTEGER PRIMARY KEY, tranche_id TEXT NOT NULL, pool_id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, tokens REAL NOT NULL)`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS trg_tbm_banked_consumption AFTER UPDATE OF remaining_tokens ON tbm_banked_tranches
    WHEN OLD.quantity_known=1 AND NEW.quantity_known=1 AND OLD.allocated_tokens=NEW.allocated_tokens
      AND NEW.remaining_tokens<OLD.remaining_tokens AND NEW.expired_unused_tokens>=OLD.expired_unused_tokens
      AND OLD.remaining_tokens-NEW.remaining_tokens-(NEW.expired_unused_tokens-OLD.expired_unused_tokens)>0
    BEGIN INSERT INTO tbm_banked_consumption(tranche_id,pool_id,timestamp_ms,tokens)
      VALUES(NEW.tranche_id,NEW.pool_id,NEW.updated_at_ms,
      OLD.remaining_tokens-NEW.remaining_tokens-(NEW.expired_unused_tokens-OLD.expired_unused_tokens)); END`);
  sql.exec('CREATE INDEX IF NOT EXISTS idx_tbm_banked_consumption_pool_time ON tbm_banked_consumption(pool_id,timestamp_ms)');
}

/** Call at the completed admission decision, after authentication/validation. Never logs payloads. */
export function recordTbmAdmission(sql: SqlStorage, input: { request_id: string; pool_id: string | null; outcome: 'accepted' | 'rejected' | 'no_eligible_route' | 'queued' }, now: number) {
  ensureTbmMetricsSchema(sql);
  sql.exec('INSERT OR IGNORE INTO tbm_admission_events VALUES (?,?,?,?)', input.request_id, input.pool_id, input.outcome, now);
}

function admissionMetrics(events: any[], scope: string, complete = true) {
  return {
    admission_rejects_rate: metric(complete && events.length ? events.filter(e => e.outcome === 'rejected' || e.outcome === 'no_eligible_route').length / events.length : null, 'ratio', `tbm_admission_events: ${scope} completed decisions`, complete ? 'no_recorded_admission_decisions' : 'incomplete_pool_attribution_see_global'),
    no_eligible_route_rate: metric(complete && events.length ? events.filter(e => e.outcome === 'no_eligible_route').length / events.length : null, 'ratio', `tbm_admission_events: ${scope} completed decisions`, complete ? 'no_recorded_admission_decisions' : 'incomplete_pool_attribution_see_global'),
  };
}

/** One observation per pool per minute; never backfill synthetic history. */
export function captureTbmMetrics(sql: SqlStorage, pools: TbmPoolRow[], now: number) {
  ensureTbmMetricsSchema(sql);
  for (const pool of pools) {
    if (pool.pool_id === '*') continue;
    const latest: any = sql.exec('SELECT observed_at_ms,data FROM tbm_catalog_samples WHERE pool_id=? ORDER BY observed_at_ms DESC LIMIT 1', pool.pool_id).toArray()[0];
    if (latest && now - latest.observed_at_ms < MINUTE) continue;
    // An unchanged/stale provider probe is not another measured interval.
    const fresh = finite(pool.last_updated_ms) && now - pool.last_updated_ms < 20 * MINUTE && pool.last_updated_ms <= now;
    const rem = poolEffectiveRemaining(pool);
    const prior = latest ? JSON.parse(latest.data) : null;
    const deltaMinutes = prior && finite(prior.probe_at) && finite(pool.last_updated_ms) ? (pool.last_updated_ms - prior.probe_at) / MINUTE : 0;
    const measured = fresh && prior?.fresh && prior.reset === pool.reset_at_ms && prior.percent === isPercentPool(pool) && deltaMinutes > 0 && deltaMinutes <= 20 && finite(prior.remaining) && finite(rem) && rem <= prior.remaining;
    const data = {
      probe_at: pool.last_updated_ms, remaining: finite(rem) ? rem : null,
      measured_minutes: measured ? deltaMinutes : 0,
      fresh, reset: pool.reset_at_ms, percent: isPercentPool(pool),
      actual: measured ? (prior.remaining - rem) / deltaMinutes : null,
      target: fresh ? pool.target_burn_rate_tpm : null,
      soft: fresh && finite(rem) && prior?.probe_at !== pool.last_updated_ms ? Number(rem < softReserveTokens(pool, now)) : null,
      hard: fresh && finite(rem) && prior?.probe_at !== pool.last_updated_ms ? Number(rem < hardFloorTokens(pool)) : null,
    };
    sql.exec('INSERT OR IGNORE INTO tbm_catalog_samples VALUES (?,?,?)', pool.pool_id, now, JSON.stringify(data));
  }
  sql.exec('DELETE FROM tbm_catalog_samples WHERE observed_at_ms < ?', now - 7 * DAY - 5 * MINUTE);
}

const rows = (sql: SqlStorage, table: string, query: string, ...args: (string | number)[]): any[] => {
  if (!sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table).toArray().length) return [];
  return sql.exec(query, ...args).toArray();
};

/** Catalog reads use measured records, never the legacy rollup zero defaults. */
export function readTbmMetrics(sql: SqlStorage, now: number) {
  const pools = sql.exec("SELECT * FROM tbm_quota_pools WHERE pool_id != '*' ORDER BY pool_id").toArray() as unknown as TbmPoolRow[];
  const admissions = rows(sql, 'tbm_admission_events', 'SELECT pool_id,outcome,timestamp_ms FROM tbm_admission_events WHERE timestamp_ms>=? AND timestamp_ms<=?', now - 7 * DAY, now);
  return { as_of: new Date(now).toISOString(), windows: ['24h', '7d'],
    global: { pool_id: 'Fleet (including unattributed)', windows: Object.fromEntries(([['24h', DAY], ['7d', 7 * DAY]] as const).map(([window, duration]) => [window, {
      metrics: admissionMetrics(admissions.filter(e => e.timestamp_ms >= now - duration), 'all pools including unrouted requests'),
      unattributed_decisions: admissions.filter(e => e.timestamp_ms >= now - duration && e.pool_id === null).length,
    }])) },
    pools: pools.map(pool => {
    const tranches = rows(sql, 'tbm_banked_tranches', 'SELECT * FROM tbm_banked_tranches WHERE pool_id=?', pool.pool_id);
    const active = tranches.filter(t => t.status === 'active' && t.expires_at_ms > now);
    const bankKnown = active.every(t => t.quantity_known !== 0);
    return { pool_id: pool.pool_id, provider: pool.provider, account: pool.account,
      windows: Object.fromEntries(([['24h', DAY], ['7d', 7 * DAY]] as const).map(([window, duration]) => {
        const start = now - duration;
        // Aggregate in SQLite: retained history must not become a JS array of every probe.
        const probes = `WITH probes AS (SELECT observed_at_ms,
          json_extract(data,'$.fresh') AS fresh, json_extract(data,'$.actual') AS actual,
          json_extract(data,'$.target') AS target, json_extract(data,'$.probe_at') AS probe_at,
          json_extract(data,'$.measured_minutes') AS measured_minutes,
          json_extract(data,'$.soft') AS soft, json_extract(data,'$.hard') AS hard
          FROM tbm_catalog_samples WHERE pool_id=? AND observed_at_ms>=? AND observed_at_ms<=?)`;
        const pacing = rows(sql, 'tbm_catalog_samples', `${probes}
          SELECT COUNT(CASE WHEN fresh=1 AND actual IS NOT NULL AND target IS NOT NULL THEN 1 END) AS samples,
            AVG(CASE WHEN fresh=1 AND actual IS NOT NULL AND target>0 THEN actual*1.0/target END) AS ratio,
            SUM(CASE WHEN fresh=1 AND actual IS NOT NULL AND target IS NOT NULL AND measured_minutes>0
              AND probe_at-measured_minutes*60000>=? THEN MAX(0,target-actual)*measured_minutes END) AS idle,
            SUM(CASE WHEN fresh=1 AND actual IS NOT NULL AND target IS NOT NULL AND measured_minutes>0
              AND probe_at-measured_minutes*60000>=? THEN measured_minutes END) AS minutes FROM probes`,
          pool.pool_id, start, now, start, start)[0];
        const floors = rows(sql, 'tbm_catalog_samples', `${probes}
          SELECT COUNT(*) AS probes,
            SUM(CASE WHEN soft=1 AND COALESCE(previous_soft,0)=0 THEN 1 ELSE 0 END) AS soft,
            SUM(CASE WHEN hard=1 AND COALESCE(previous_hard,0)=0 THEN 1 ELSE 0 END) AS hard
          FROM (SELECT soft,hard,LAG(soft) OVER(ORDER BY observed_at_ms) AS previous_soft,
            LAG(hard) OVER(ORDER BY observed_at_ms) AS previous_hard FROM probes
            WHERE soft IS NOT NULL AND hard IS NOT NULL)`, pool.pool_id, start, now)[0];
        const minutes = Number(pacing?.minutes) || 0;
        const rewards = rows(sql, 'tbm_reward_events', "SELECT * FROM tbm_reward_events WHERE pool_id=? AND event_kind='session' AND timestamp_ms>=? AND timestamp_ms<=?", pool.pool_id, start, now);
        const measured = rewards.filter(r => r.tokens_known === 1 && finite(r.tokens_burned) && r.tokens_burned > 0 && finite(r.work_units));
        const tokens = sum(measured.map(r => r.tokens_burned));
        const resets = rows(sql, 'tbm_pool_resets', 'SELECT * FROM tbm_pool_resets WHERE pool_id=? AND reset_at_ms>=? AND reset_at_ms<=?', pool.pool_id, start, now);
        const percent = isPercentPool(pool);
        const resetKnown = resets.length > 0 && resets.every(r => percent ? finite(r.remaining_percent) : r.capacity_known === 1);
        const leftover = resetKnown ? sum(resets.map(r => Math.max(0, percent ? r.remaining_percent - r.hard_floor_percent : r.remaining_tokens - hardFloorTokens({ ...pool, total_capacity_tokens: r.total_capacity_tokens, hard_floor_percent: r.hard_floor_percent })))) : null;
        const sessions = rows(sql, 'session_control_sessions', 'SELECT id,engine,created_at FROM session_control_sessions WHERE id IN (SELECT session_id FROM tbm_reward_events WHERE pool_id=? AND timestamp_ms>=? AND timestamp_ms<=?)', pool.pool_id, start, now);
        const bySession = new Map(sessions.map(s => [s.id, s]));
        const durations: number[] = [];
        const engineTokens: Record<string, number> = {};
        for (const r of rewards) {
          const s = bySession.get(r.session_id);
          if (s && r.work_units > 0) {
            const seconds = (r.timestamp_ms - Date.parse(s.created_at)) / 1000;
            if (finite(seconds) && seconds >= 0) durations.push(seconds);
          }
          if (s?.engine && r.tokens_known === 1 && finite(r.tokens_burned) && r.tokens_burned > 0) engineTokens[s.engine] = (engineTokens[s.engine] ?? 0) + r.tokens_burned;
        }
        const mixTotal = sum(Object.values(engineTokens));
        const bankConsumption = rows(sql, 'tbm_banked_consumption', 'SELECT tokens,timestamp_ms FROM tbm_banked_consumption WHERE pool_id=? AND timestamp_ms>=? AND timestamp_ms<=?', pool.pool_id, start, now);
        const expired = tranches.filter(t => t.expires_at_ms >= start && t.expires_at_ms <= now);
        const expiredKnown = expired.length > 0 && expired.every(t => t.status === 'expired' && t.quantity_known !== 0);
        return [window, { start_time_ms: start, end_time_ms: now,
          coverage: { floor_probes: Number(floors?.probes) || 0, banked_consumption_events: bankConsumption.length, pacing_samples: Number(pacing?.samples) || 0, observed_minutes: minutes, reward_events: rewards.length, measured_token_events: measured.length, reset_events: resets.length },
          metrics: {
            burn_rate_vs_pacing_target: metric(pacing?.ratio ?? null, 'ratio', 'tbm_catalog_samples: mean observed actual/target'),
            leftover_quota_at_planned_reset: metric(leftover, percent ? 'percentage_points' : 'tokens', 'tbm_pool_resets: sum remaining above hard reserve', 'no_measured_reset_quantity'),
            reserve_floor_breaches: metric(floors?.probes ? { soft: floors.soft, hard: floors.hard } : null, 'observed_episodes', 'tbm_catalog_samples: distinct provider probes strictly below floor; first observed breach starts one episode; gaps unmeasured'),
            useful_work_per_tokens: metric(tokens > 0 ? sum(measured.map(r => r.work_units)) * 1000000 / tokens : null, 'work_units/million_tokens', 'tbm_reward_events: matched native tokens and verified work', 'no_measured_native_tokens'),
            idle_wasted_quota_over_time: metric(minutes > 0 ? pacing.idle : null, percent ? 'percentage_points' : 'tokens', 'tbm_catalog_samples: integral positive target-actual over observed intervals', 'insufficient_contiguous_samples'),
            engine_mix: metric(mixTotal > 0 ? Object.fromEntries(Object.entries(engineTokens).map(([engine, n]) => [engine, n / mixTotal])) : null, 'token_share_by_engine', 'tbm_reward_events joined session_control_sessions; measured-token subset', 'no_attributed_native_tokens'),
            ...admissionMetrics(admissions.filter(e => e.pool_id === pool.pool_id && e.timestamp_ms >= start), 'attributed pool only; unrouted requests are global', !admissions.some(e => e.timestamp_ms >= start && e.pool_id === null)),
            time_to_useful_outcome: metric(mean(durations), 'seconds', 'verified reward timestamp minus session creation', 'no_verified_outcome_with_start_time'),
            banked_remaining: metric(active.length && bankKnown ? sum(active.map(t => t.remaining_tokens)) : null, 'tokens', 'tbm_banked_tranches: current active stock (not window sum)', active.length ? 'banked_quantity_unknown' : 'no_active_tranches'),
            time_to_expiry: metric(active.length ? Math.min(...active.map(t => (t.expires_at_ms - now) / 1000)) : null, 'seconds', 'tbm_banked_tranches: current nearest active deadline', 'no_active_tranches'),
            expired_unused: metric(expiredKnown ? sum(expired.map(t => t.expired_unused_tokens)) : null, 'tokens', 'tbm_banked_tranches: expirations in window', 'no_settled_measured_expirations'),
            banked_burn_rate: {
              ...metric(null, 'tokens/minute', 'native banked consumption timestamps', 'quantity_decrement_observation_does_not_prove_consumption_time'),
              observed_event_throughput: {
                value: bankConsumption.length ? sum(bankConsumption.map(r => r.tokens)) / (duration / MINUTE) : null,
                unit: 'observed_tokens/window_minute', basis: 'quantity decrements recorded in this window, not native consumption time',
                event_count: bankConsumption.length, unobserved_intervals: 'unknown',
              },
            },
          } }];
      })) };
  }) };
}
