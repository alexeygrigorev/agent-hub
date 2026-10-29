/**
 * Token Burning Machine AC2 / AC3 / AC6 backend controller:
 * - Persisted PID pacing controller called by exported `tickTbm(sql, nowMs)`
 * - Reset boundary waste penalty ledger (`tbm_pool_resets` + SQLite trigger)
 * - Idempotent terminal session outcome rewards with real native token counts
 *   when available (unknown stays unknown; delayed native tokens upgrade in-place)
 * - Multi-window rollups (`tbm_metrics_rollup`) and routing efficiency feedback
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { ensureTbmQuotaSchema } from './quota';
import {
  engineProvider,
  isPercentOnlyTbmPool,
  normalizeProviderFamily,
  resolveTbmPoolCycleMs,
  withPolicyTbmFloors,
} from './quota_admission';
import {
  FREE_POOL_PROVIDERS,
  hardFloorTokens,
  isDeadlineOnlyTranche,
  isPercentPool,
  loadActiveTranches,
  loadPools,
  loadProviderPacing,
  newPidState,
  pacingStep,
  poolEffectiveRemaining,
  softReserveTokens,
  trancheUrgencyTpm,
  type PidState,
  type ProviderPacing,
  type TbmPoolRow,
} from './pacing';

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const WASTE_AT_RESET_LAMBDA = 500.0;
const BANKED_WASTE_LAMBDA = 250.0;
const HARD_FLOOR_KAPPA = 1000.0;
const SOFT_FLOOR_KAPPA = 50.0;

export const TBM_CONTROLLER_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS tbm_controller_state (
  pool_id TEXT PRIMARY KEY,
  deficit_tokens REAL NOT NULL DEFAULT 0.0,
  integral REAL NOT NULL DEFAULT 0.0,
  last_rate_error REAL NOT NULL DEFAULT 0.0,
  last_at_ms INTEGER,
  last_remaining REAL,
  last_reset_at_ms INTEGER,
  last_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,
  updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tbm_pool_resets (
  pool_id TEXT NOT NULL,
  reset_at_ms INTEGER NOT NULL,
  provider TEXT NOT NULL,
  account TEXT NOT NULL DEFAULT 'default',
  total_capacity_tokens INTEGER NOT NULL DEFAULT 0,
  remaining_tokens INTEGER NOT NULL DEFAULT 0,
  headroom_percentage REAL NOT NULL DEFAULT 0.0,
  remaining_percent REAL,
  capacity_known INTEGER NOT NULL DEFAULT 1,
  hard_floor_percent REAL NOT NULL DEFAULT 5.0,
  captured_at_ms INTEGER NOT NULL,
  rewarded INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (pool_id, reset_at_ms)
);
CREATE TABLE IF NOT EXISTS tbm_session_tokens (
  session_id TEXT PRIMARY KEY,
  native_id TEXT,
  tag TEXT,
  engine TEXT,
  account TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  observed_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tbm_session_tokens_native ON tbm_session_tokens(native_id);
CREATE INDEX IF NOT EXISTS idx_tbm_session_tokens_tag ON tbm_session_tokens(tag);
`;

const TBM_RESET_TRIGGER_SQL = `
CREATE TRIGGER IF NOT EXISTS trg_tbm_pool_reset_capture_v3
AFTER UPDATE OF reset_at_ms ON tbm_quota_pools
WHEN NEW.reset_at_ms > OLD.reset_at_ms AND OLD.pool_id != '*' AND OLD.reset_at_ms > 0 AND NEW.last_updated_ms >= OLD.reset_at_ms
AND NOT EXISTS (SELECT 1 FROM tbm_pool_resets WHERE pool_id=OLD.pool_id AND reset_at_ms=OLD.reset_at_ms)
BEGIN
  INSERT OR IGNORE INTO tbm_pool_resets (
    pool_id, reset_at_ms, provider, account,
    total_capacity_tokens, remaining_tokens, headroom_percentage,
    remaining_percent, capacity_known, hard_floor_percent, captured_at_ms, rewarded
  ) VALUES (
    OLD.pool_id, OLD.reset_at_ms, OLD.provider, OLD.account,
    OLD.total_capacity_tokens, OLD.remaining_tokens, OLD.headroom_percentage,
    OLD.remaining_percent, OLD.capacity_known, OLD.hard_floor_percent, NEW.last_updated_ms, 0
  );
END;
`;

function hasTable(sql: SqlStorage, name: string): boolean {
  try {
    return sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", name).toArray().length > 0;
  } catch {
    return false;
  }
}

function tableColumns(sql: SqlStorage, table: string): Set<string> {
  try {
    return new Set(sql.exec(`PRAGMA table_info(${table})`).toArray().map((c: any) => String(c.name)));
  } catch {
    return new Set();
  }
}

/** Idempotent schema & trigger initialization for the TBM controller. */
export function ensureTbmControllerSchema(sql: SqlStorage): void {
  ensureTbmQuotaSchema(sql);
  for (const stmt of TBM_CONTROLLER_SCHEMA_SQL.split(';')) {
    const trimmed = stmt.trim();
    if (trimmed) sql.exec(trimmed);
  }
  const poolCols = tableColumns(sql, 'tbm_quota_pools');
  for (const [col, def] of [
    ['cycle_ms', 'INTEGER'],
    ['window_type', 'TEXT'],
    ['remaining_percent', 'REAL'],
    ['capacity_known', 'INTEGER NOT NULL DEFAULT 1'],
    ['quota_unit', 'TEXT'],
  ] as const) {
    if (!poolCols.has(col)) {
      try { sql.exec(`ALTER TABLE tbm_quota_pools ADD COLUMN ${col} ${def}`); } catch {}
    }
  }
  const trancheCols = tableColumns(sql, 'tbm_banked_tranches');
  for (const [col, def] of [
    ['quantity_known', 'INTEGER NOT NULL DEFAULT 1'],
    ['account', 'TEXT'],
  ] as const) {
    if (!trancheCols.has(col)) {
      try { sql.exec(`ALTER TABLE tbm_banked_tranches ADD COLUMN ${col} ${def}`); } catch {}
    }
  }
  const rewardCols = tableColumns(sql, 'tbm_reward_events');
  for (const [col, def] of [
    ['tokens_known', 'INTEGER NOT NULL DEFAULT 0'],
    ['efficiency_known', 'INTEGER NOT NULL DEFAULT 0'],
    ['tokens_burned', 'INTEGER'],
    ['work_units', 'REAL'],
    ['session_id', 'TEXT'],
    ['event_kind', "TEXT NOT NULL DEFAULT 'session'"],
  ] as const) {
    if (!rewardCols.has(col)) {
      try { sql.exec(`ALTER TABLE tbm_reward_events ADD COLUMN ${col} ${def}`); } catch {}
    }
  }
  const rollupCols = tableColumns(sql, 'tbm_metrics_rollup');
  for (const [col, def] of [
    ['tokens_known', 'INTEGER NOT NULL DEFAULT 0'],
    ['efficiency_known', 'INTEGER NOT NULL DEFAULT 0'],
  ] as const) {
    if (!rollupCols.has(col)) {
      try { sql.exec(`ALTER TABLE tbm_metrics_rollup ADD COLUMN ${col} ${def}`); } catch {}
    }
  }
  try {
    sql.exec('DROP TRIGGER IF EXISTS trg_tbm_pool_reset_capture');
    sql.exec('DROP TRIGGER IF EXISTS trg_tbm_pool_reset_capture_v2');
    sql.exec(TBM_RESET_TRIGGER_SQL);
    sql.exec('CREATE INDEX IF NOT EXISTS idx_tbm_reward_pool_time ON tbm_reward_events(pool_id,timestamp_ms)');
    sql.exec('DROP INDEX IF EXISTS idx_tbm_binding_native');
    if (hasTable(sql, 'session_control_sessions')) sql.exec("CREATE INDEX IF NOT EXISTS idx_tbm_binding_native_valid ON session_control_sessions(CASE WHEN json_valid(binding_json) THEN json_extract(binding_json, '$.native_id') END)");
  } catch {}
}

export interface SessionTokensInput {
  session_id: string;
  native_id?: string | null;
  tag?: string | null;
  engine?: string | null;
  account?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  observed_at_ms?: number;
}

/** Record or update measured native token usage for a session and upgrade any pending reward row. */
export function recordSessionTokens(sql: SqlStorage, input: SessionTokensInput, nowMs = Date.now()): void {
  const inTok = typeof input.input_tokens === 'number' && Number.isFinite(input.input_tokens) && input.input_tokens >= 0
    ? Math.floor(input.input_tokens)
    : 0;
  const outTok = typeof input.output_tokens === 'number' && Number.isFinite(input.output_tokens) && input.output_tokens >= 0
    ? Math.floor(input.output_tokens)
    : 0;
  const totTok = typeof input.total_tokens === 'number' && Number.isFinite(input.total_tokens) && input.total_tokens >= 0
    ? Math.floor(input.total_tokens)
    : inTok + outTok;
  const atMs = typeof input.observed_at_ms === 'number' && Number.isFinite(input.observed_at_ms) ? input.observed_at_ms : nowMs;
  const prior: any = hasTable(sql, 'tbm_session_tokens') ? sql.exec('SELECT * FROM tbm_session_tokens WHERE session_id = ?', input.session_id).toArray()[0] : null;
  if (prior && prior.input_tokens === inTok && prior.output_tokens === outTok && prior.total_tokens === totTok
      && (!input.native_id || prior.native_id === input.native_id)
      && (!input.tag || prior.tag === input.tag) && (!input.engine || prior.engine === input.engine)
      && (!input.account || prior.account === input.account)) return;
  ensureTbmControllerSchema(sql);
  sql.exec(
    `INSERT INTO tbm_session_tokens (session_id, native_id, tag, engine, account, input_tokens, output_tokens, total_tokens, observed_at_ms)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(session_id) DO UPDATE SET
       native_id = COALESCE(excluded.native_id, tbm_session_tokens.native_id),
       tag = COALESCE(excluded.tag, tbm_session_tokens.tag),
       engine = COALESCE(excluded.engine, tbm_session_tokens.engine),
       account = COALESCE(excluded.account, tbm_session_tokens.account),
       input_tokens = excluded.input_tokens,
       output_tokens = excluded.output_tokens,
       total_tokens = excluded.total_tokens,
       observed_at_ms = excluded.observed_at_ms`,
    input.session_id,
    input.native_id ?? null,
    input.tag ?? null,
    input.engine ?? null,
    input.account ?? null,
    inTok,
    outTok,
    totTok,
    atMs
  );
  // Native telemetry may arrive under a native key before the Hub session ends.
  upgradePendingSessionRewards(sql, nowMs, input.session_id);
  if (input.native_id && hasTable(sql, 'session_control_sessions')) {
    const linked = sql.exec("SELECT id FROM session_control_sessions WHERE CASE WHEN json_valid(binding_json) THEN json_extract(binding_json, '$.native_id') END = ? AND machine_id || ':' || ? = ?", input.native_id,input.native_id,input.session_id).toArray();
    for (const row of linked) if (row.id !== input.session_id) upgradePendingSessionRewards(sql, nowMs, String(row.id));
  }
  if (input.native_id && hasTable(sql, 'routing_jobs') && hasTable(sql, 'fleet_launches')) {
    const jobs = sql.exec(`SELECT j.id FROM routing_jobs j JOIN fleet_launches l ON l.id=j.launch_id
      WHERE l.native_id=? AND l.machine_id || ':' || l.native_id=?`, input.native_id,input.session_id).toArray();
    for (const row of jobs) upgradePendingSessionRewards(sql,nowMs,String(row.id));
  }
}

export interface SessionOutcomeInput {
  session_id: string;
  intake_task_id?: string | null;
  engine?: string | null;
  provider?: string | null;
  account?: string | null;
  pool_id?: string | null;
  outcome: 'done' | 'stopped' | 'failed' | 'cancelled' | 'refused' | 'stuck';
  active_mode?: 'high' | 'super-think' | 'mini-swarm' | null;
  swarm_concurrency?: number | null;
  /** Real native token count when known; null/undefined means unknown (never fabricated). */
  tokens_burned?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  /** Explicit verified outcome evidence required for non-done outcomes to earn positive work reward. */
  verified_outcome?: boolean | null;
  verification_fidelity?: number | null;
  value_weight?: number | null;
  timestamp_ms?: number;
}

function normalizeActiveMode(mode: unknown): 'high' | 'super-think' | 'mini-swarm' {
  if (mode === 'super-think' || mode === 'mini-swarm') return mode;
  return 'high';
}

function parseJsonSafe(raw: unknown): any {
  if (typeof raw !== 'string' || !raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

/** Resolve real native token counts for a session if available; returns null when unknown. */
export function resolveNativeSessionTokens(
  sql: SqlStorage,
  sessionId: string,
  explicitTokens?: number | null,
  explicitIn?: number | null,
  explicitOut?: number | null
): number | null {
  if (typeof explicitTokens === 'number' && Number.isFinite(explicitTokens) && explicitTokens >= 0) {
    return Math.floor(explicitTokens);
  }
  if (
    (typeof explicitIn === 'number' && Number.isFinite(explicitIn) && explicitIn >= 0) ||
    (typeof explicitOut === 'number' && Number.isFinite(explicitOut) && explicitOut >= 0)
  ) {
    return Math.floor((explicitIn ?? 0) + (explicitOut ?? 0));
  }
  if (hasTable(sql, 'tbm_session_tokens')) {
    const direct = sql.exec('SELECT total_tokens FROM tbm_session_tokens WHERE session_id = ?', sessionId).toArray()[0] as any;
    if (direct && typeof direct.total_tokens === 'number' && Number.isFinite(direct.total_tokens) && direct.total_tokens >= 0) {
      return Math.floor(direct.total_tokens);
    }
  }
  if (hasTable(sql, 'routing_jobs') && hasTable(sql, 'fleet_launches') && hasTable(sql, 'tbm_session_tokens')) {
    const measured: any = sql.exec(`SELECT t.total_tokens FROM routing_jobs j
      JOIN fleet_launches l ON l.id=j.launch_id
      JOIN tbm_session_tokens t ON t.session_id=l.machine_id || ':' || l.native_id AND t.native_id=l.native_id
      WHERE j.id=? LIMIT 1`, sessionId).toArray()[0];
    if (measured && typeof measured.total_tokens === 'number' && measured.total_tokens >= 0) return Math.floor(measured.total_tokens);
  }
  if (hasTable(sql, 'session_control_sessions')) {
    const sRow = sql.exec('SELECT id, fleet_launch_id, machine_id, binding_json, request_json FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0] as any;
    if (sRow) {
      const binding = parseJsonSafe(sRow.binding_json);
      const ret = binding?.retirement;
      for (const holder of [ret, binding]) {
        if (!holder || typeof holder !== 'object') continue;
        if (typeof holder.tokens_burned === 'number' && Number.isFinite(holder.tokens_burned) && holder.tokens_burned >= 0) {
          return Math.floor(holder.tokens_burned);
        }
        if (typeof holder.total_tokens === 'number' && Number.isFinite(holder.total_tokens) && holder.total_tokens >= 0) {
          return Math.floor(holder.total_tokens);
        }
        if (typeof holder.input_tokens === 'number' && typeof holder.output_tokens === 'number') {
          const sum = holder.input_tokens + holder.output_tokens;
          if (Number.isFinite(sum) && sum >= 0) return Math.floor(sum);
        }
      }
      const nativeId = binding?.native_id;
      const tag = binding?.tag;
      if (hasTable(sql, 'tbm_session_tokens') && (nativeId || tag)) {
        const byNative = nativeId
          ? (sql.exec('SELECT total_tokens FROM tbm_session_tokens WHERE native_id = ? AND session_id = ? LIMIT 1', nativeId, `${sRow.machine_id}:${nativeId}`).toArray()[0] as any)
          : null;
        if (byNative && typeof byNative.total_tokens === 'number' && byNative.total_tokens >= 0) {
          return Math.floor(byNative.total_tokens);
        }

      }
      if (hasTable(sql, 'session_control_commands')) {
        const cmds = sql.exec("SELECT ack_json FROM session_control_commands WHERE session_id = ? AND ack_json IS NOT NULL ORDER BY updated_at DESC LIMIT 5", sessionId).toArray() as any[];
        for (const c of cmds) {
          const ack = parseJsonSafe(c.ack_json);
          const nat = ack?.native;
          for (const h of [ack, nat]) {
            if (!h || typeof h !== 'object') continue;
            if (typeof h.tokens_burned === 'number' && Number.isFinite(h.tokens_burned) && h.tokens_burned >= 0) {
              return Math.floor(h.tokens_burned);
            }
            if (typeof h.total_tokens === 'number' && Number.isFinite(h.total_tokens) && h.total_tokens >= 0) {
              return Math.floor(h.total_tokens);
            }
            if (typeof h.input_tokens === 'number' && typeof h.output_tokens === 'number') {
              const sum = h.input_tokens + h.output_tokens;
              if (Number.isFinite(sum) && sum >= 0) return Math.floor(sum);
            }
          }
        }
      }
    }
  }
  return null;
}

function resolvePoolForOutcome(
  sql: SqlStorage,
  explicitPoolId: string | null | undefined,
  provider: string | null | undefined,
  account: string | null | undefined
): { poolId: string; pool: TbmPoolRow | null; provider: string } {
  // Floor-breach penalties must use the effective floors (operator overrides applied),
  // not the stored defaults a configured zero floor replaces.
  const pools = loadPools(sql, false).map(p => withPolicyTbmFloors(p));
  if (explicitPoolId && explicitPoolId !== '*') {
    const exact = pools.find(p => p.pool_id === explicitPoolId) ?? null;
    if (exact) return { poolId: exact.pool_id, pool: exact, provider: exact.provider };
  }
  const prov = provider || 'unknown';
  const fam = normalizeProviderFamily(prov) || prov;
  const matching = pools.filter(p => p.provider === prov || normalizeProviderFamily(p.provider) === fam);
  if (account) {
    const acctMatch = matching.find(p => p.account === account);
    if (acctMatch) return { poolId: acctMatch.pool_id, pool: acctMatch, provider: acctMatch.provider };
  }
  if (matching.length > 0) {
    return { poolId: matching[0].pool_id, pool: matching[0], provider: matching[0].provider };
  }
  const fallbackPoolId = account ? `${prov}:${account}` : `${prov}:default`;
  return { poolId: explicitPoolId || fallbackPoolId, pool: null, provider: prov };
}

export interface RewardEventRow {
  event_id: string;
  timestamp_ms: number;
  pool_id: string;
  intake_task_id: string;
  active_mode: 'high' | 'super-think' | 'mini-swarm';
  swarm_concurrency: number;
  useful_work_reward: number;
  pacing_error_penalty: number;
  floor_breach_penalty: number;
  engine_mix_penalty: number;
  waste_at_reset_penalty: number;
  banked_waste_penalty: number;
  net_reward: number;
  token_efficiency_eta: number;
  tokens_known: number;
  efficiency_known: number;
  tokens_burned: number | null;
  session_id: string | null;
  event_kind: string;
}

/**
 * Write or idempotently upgrade a terminal session outcome reward event.
 * - Accepted/verified outcome yields positive `useful_work_reward`.
 * - Failed / cancelled / refused / stuck yields 0 `useful_work_reward`.
 * - Real native token counts compute `token_efficiency_eta` (VU/MTok) and set `tokens_known = 1`.
 * - Unknown native token counts stay `tokens_known = 0, tokens_burned = null` with NO fabricated
 *   token count and NO efficiency penalty; if native token usage arrives later, calling this
 *   or `tickTbm` upgrades the row in-place to `tokens_known = 1`.
 */
export function recordSessionOutcomeReward(
  sql: SqlStorage,
  input: SessionOutcomeInput,
  nowMs = Date.now()
): RewardEventRow {
  ensureTbmControllerSchema(sql);
  const eventId = `session:${input.session_id}`;
  const existing = sql.exec('SELECT * FROM tbm_reward_events WHERE event_id = ?', eventId).toArray()[0] as any;
  const resolvedTokens = resolveNativeSessionTokens(
    sql,
    input.session_id,
    input.tokens_burned,
    input.input_tokens,
    input.output_tokens
  ) ?? existing?.tokens_burned ?? null;

  const prov = input.provider ?? (input.engine ? engineProvider(input.engine) : null) ?? 'unknown';
  const { poolId, pool, provider } = resolvePoolForOutcome(sql, input.pool_id ?? existing?.pool_id, prov, input.account);
  // Manual stop / exit0 alone is not acceptance: require outcome === 'done', explicit verified_outcome,
  // explicit positive verification_fidelity, or a linked accepted routing_jobs row (state = 'done').
  let acceptedJob = input.outcome === 'done' || input.verified_outcome === true;
  if (!acceptedJob && hasTable(sql, 'session_control_sessions') && hasTable(sql, 'routing_jobs')) {
    try {
      const linkedDone = sql.exec(
        `SELECT j.state FROM session_control_sessions s
         JOIN routing_jobs j ON j.launch_id = s.fleet_launch_id
         WHERE s.id = ? AND j.state = 'done' LIMIT 1`,
        input.session_id
      ).toArray()[0];
      if (linkedDone) acceptedJob = true;
    } catch {}
  }
  const hasExplicitFidelity = typeof input.verification_fidelity === 'number' && Number.isFinite(input.verification_fidelity) && input.verification_fidelity > 0;
  const verifiedUseful = acceptedJob || hasExplicitFidelity;
  const fidelity = hasExplicitFidelity
    ? Math.max(0, Math.min(1, input.verification_fidelity as number))
    : (verifiedUseful ? 1.0 : 0.0);
  const weight = typeof input.value_weight === 'number' && Number.isFinite(input.value_weight) && input.value_weight > 0
    ? input.value_weight
    : 10.0;
  const previousWork = existing ? (existing.work_units ?? Math.max(0, existing.useful_work_reward - Math.min(5, existing.token_efficiency_eta / 50 * 2))) : 0;
  const workUnits = Math.max(previousWork, verifiedUseful ? weight * fidelity : 0);
  if (existing && existing.tokens_burned === resolvedTokens && previousWork === workUnits) return existing as RewardEventRow;

  const tokensKnown = resolvedTokens !== null && resolvedTokens > 0 ? 1 : 0;
  const efficiencyKnown = tokensKnown === 1 ? 1 : 0;
  const tokensBurned = resolvedTokens !== null ? resolvedTokens : null;
  // Token efficiency eta in VU/MTok when native tokens are known; 0 when unknown (never penalized, flagged by efficiency_known=0).
  const eta = efficiencyKnown && tokensBurned && tokensBurned > 0
    ? (workUnits * 1_000_000) / tokensBurned
    : 0.0;
  // Useful work reward scales with verified work and (when tokens known) a bounded efficiency bonus.
  const efficiencyBonus = efficiencyKnown && eta > 0 ? Math.min(5.0, (eta / 50.0) * 2.0) : 0.0;
  const usefulWorkReward = workUnits + efficiencyBonus;

  // Pacing error penalty (deadband [0.85, 1.15] has 0 penalty).
  let pacingErrorPenalty = 0.0;
  let floorBreachPenalty = 0.0;
  if (pool) {
    const rho = Number.isFinite(pool.pacing_ratio) ? pool.pacing_ratio : 1.0;
    if (rho < 0.85) pacingErrorPenalty = 10.0 * Math.pow(0.85 - rho, 2);
    else if (rho > 1.15) pacingErrorPenalty = 10.0 * Math.pow(rho - 1.15, 2);

    const rem = poolEffectiveRemaining(pool);
    const hard = hardFloorTokens(pool);
    const soft = softReserveTokens(pool, nowMs);
    const denom = isPercentPool(pool) ? 100 : Math.max(1, pool.total_capacity_tokens);
    if (rem < hard) {
      floorBreachPenalty = HARD_FLOOR_KAPPA + SOFT_FLOOR_KAPPA * ((soft - rem) / denom);
    } else if (rem < soft) {
      floorBreachPenalty = SOFT_FLOOR_KAPPA * ((soft - rem) / denom);
    }
  }

  // Engine mix penalty: Grok carries a small mix penalty; free pool (zai/go) has 0.
  const fam = normalizeProviderFamily(provider) || provider;
  const engineMixPenalty = existing?.engine_mix_penalty ?? (fam === 'grok' ? 5.0 : (FREE_POOL_PROVIDERS.includes(fam) ? 0.0 : 0.5));
  if (existing) { pacingErrorPenalty = existing.pacing_error_penalty; floorBreachPenalty = existing.floor_breach_penalty; }
  const wasteAtResetPenalty = 0.0;
  const bankedWastePenalty = 0.0;
  const netReward = usefulWorkReward - pacingErrorPenalty - floorBreachPenalty - engineMixPenalty;

  const activeMode = normalizeActiveMode(input.active_mode);
  const concurrency = typeof input.swarm_concurrency === 'number' && Number.isInteger(input.swarm_concurrency) && input.swarm_concurrency >= 1
    ? input.swarm_concurrency
    : 1;
  const intakeTaskId = (typeof input.intake_task_id === 'string' && input.intake_task_id.trim())
    ? input.intake_task_id.trim()
    : (existing?.intake_task_id || 'unlinked');
  const tsMs = typeof input.timestamp_ms === 'number' && Number.isFinite(input.timestamp_ms)
    ? input.timestamp_ms
    : (existing?.timestamp_ms ?? nowMs);

  if (existing) {
    sql.exec(
      `UPDATE tbm_reward_events
       SET useful_work_reward = ?, pacing_error_penalty = ?, floor_breach_penalty = ?,
           engine_mix_penalty = ?, net_reward = ?, token_efficiency_eta = ?,
           tokens_known = ?, efficiency_known = ?, tokens_burned = ?, work_units = ?
       WHERE event_id = ?`,
      usefulWorkReward,
      pacingErrorPenalty,
      floorBreachPenalty,
      engineMixPenalty,
      netReward,
      eta,
      tokensKnown,
      efficiencyKnown,
      tokensBurned,
      workUnits,
      eventId
    );
  } else {
    sql.exec(
      `INSERT INTO tbm_reward_events (
        event_id, timestamp_ms, pool_id, intake_task_id, active_mode, swarm_concurrency,
        useful_work_reward, pacing_error_penalty, floor_breach_penalty, engine_mix_penalty,
        waste_at_reset_penalty, banked_waste_penalty, net_reward, token_efficiency_eta,
        tokens_known, efficiency_known, tokens_burned, session_id, work_units, event_kind
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'session')`,
      eventId,
      tsMs,
      poolId,
      intakeTaskId,
      activeMode,
      concurrency,
      usefulWorkReward,
      pacingErrorPenalty,
      floorBreachPenalty,
      engineMixPenalty,
      wasteAtResetPenalty,
      bankedWastePenalty,
      netReward,
      eta,
      tokensKnown,
      efficiencyKnown,
      tokensBurned,
      input.session_id,
      workUnits
    );
  }
  return sql.exec('SELECT * FROM tbm_reward_events WHERE event_id = ?', eventId).one() as unknown as RewardEventRow;
}

/** Upgrade any session reward events with tokens_known = 0 if native tokens have arrived. */
export function upgradePendingSessionRewards(sql: SqlStorage, nowMs = Date.now(), filterSessionId?: string): number {
  if (!hasTable(sql, 'tbm_reward_events')) return 0;
  const rows = filterSessionId
    ? (sql.exec("SELECT * FROM tbm_reward_events WHERE event_id = ?", `session:${filterSessionId}`).toArray() as any[])
    : (sql.exec("SELECT * FROM tbm_reward_events WHERE event_id LIKE 'session:%' ORDER BY timestamp_ms DESC LIMIT 100").toArray() as any[]);
  let upgraded = 0;
  for (const r of rows) {
    const sid = r.session_id || String(r.event_id).replace(/^session:/, '');
    const tokens = resolveNativeSessionTokens(sql, sid);
    if (tokens !== null && tokens > 0 && tokens !== r.tokens_burned) {
      const outcome: SessionOutcomeInput['outcome'] = 'failed'; // Preserve recorded work; telemetry alone cannot assert acceptance.
      recordSessionOutcomeReward(sql, {
        session_id: sid,
        intake_task_id: r.intake_task_id,
        pool_id: r.pool_id,
        outcome,
        active_mode: r.active_mode,
        swarm_concurrency: r.swarm_concurrency,
        tokens_burned: tokens,
        timestamp_ms: Number(r.timestamp_ms),
      }, nowMs);
      upgraded++;
    }
  }
  return upgraded;
}

/** Sweep terminal session_control_sessions and routing_jobs so every terminal outcome has a reward row. */
export function sweepTerminalSessionRewards(sql: SqlStorage, nowMs = Date.now()): { written: number; upgraded: number } {
  let written = 0;
  if (hasTable(sql, 'session_control_sessions')) {
    const rows = sql.exec(
      `SELECT s.id, s.engine, s.mode, s.state, s.request_json, s.binding_json, s.fleet_launch_id, s.updated_at
       FROM session_control_sessions s
       LEFT JOIN tbm_reward_events r ON r.event_id = ('session:' || s.id)
       WHERE s.state IN ('stopped', 'failed', 'cancelled', 'refused')
         AND r.event_id IS NULL
         AND (s.mode != 'interactive' OR json_extract(s.request_json, '$.intake_task_id') IS NOT NULL)
       ORDER BY s.updated_at DESC LIMIT 100`
    ).toArray() as any[];
    for (const s of rows) {
      const req = parseJsonSafe(s.request_json) || {};
      const intakeId = typeof req.intake_task_id === 'string' && req.intake_task_id ? req.intake_task_id : null;
      if (s.mode === 'interactive' && !intakeId) continue;
      let account: string | null = req.account ?? null;
      let engine: string | null = s.engine ?? req.engine ?? null;
      let jobStatus: string | null = null;
      if (s.fleet_launch_id) {
        try {
          const launch = sql.exec('SELECT account, engine, state FROM fleet_launches WHERE id = ?', s.fleet_launch_id).toArray()[0] as any;
          if (launch) {
            if (!account && launch.account) account = launch.account;
            if (!engine && launch.engine) engine = launch.engine;
          }
        } catch {}
        if (hasTable(sql, 'routing_jobs')) {
          try {
            const job = sql.exec('SELECT state FROM routing_jobs WHERE launch_id = ?', s.fleet_launch_id).toArray()[0] as any;
            if (job && typeof job.state === 'string') jobStatus = job.state;
          } catch {}
        }
      }
      const outcome: SessionOutcomeInput['outcome'] =
        jobStatus === 'done' ? 'done'
        : jobStatus === 'failed' ? 'failed'
        : (s.state as SessionOutcomeInput['outcome']);
      const ts = Date.parse(s.updated_at);
      recordSessionOutcomeReward(sql, {
        session_id: s.id,
        intake_task_id: intakeId ?? 'unlinked',
        engine,
        account,
        outcome,
        active_mode: normalizeActiveMode(req.tbm_mode ?? req.active_mode),
        swarm_concurrency: typeof req.max_workers === 'number' ? req.max_workers : 1,
        timestamp_ms: Number.isFinite(ts) ? ts : nowMs,
      }, nowMs);
      written++;
    }
  }
  const upgraded = upgradePendingSessionRewards(sql, nowMs);
  return { written, upgraded };
}

function processResetPenalties(sql: SqlStorage, pools: TbmPoolRow[], nowMs: number): number {
  let processed = 0;
  // Capture any pool whose reset_at_ms has passed at/before nowMs.
  for (const p of pools) {
    if (p.pool_id === '*' || !(p.reset_at_ms > 0) || p.reset_at_ms > nowMs) continue;
    sql.exec(
      `INSERT OR IGNORE INTO tbm_pool_resets (
        pool_id, reset_at_ms, provider, account,
        total_capacity_tokens, remaining_tokens, headroom_percentage,
        remaining_percent, capacity_known, hard_floor_percent, captured_at_ms, rewarded
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`,
      p.pool_id,
      p.reset_at_ms,
      p.provider,
      p.account || 'default',
      p.total_capacity_tokens,
      p.remaining_tokens,
      p.headroom_percentage,
      p.remaining_percent ?? p.headroom_percentage,
      isPercentOnlyTbmPool(p) ? 0 : 1,
      typeof p.hard_floor_percent === 'number' && Number.isFinite(p.hard_floor_percent) && p.hard_floor_percent >= 0 && p.hard_floor_percent <= 100
        ? p.hard_floor_percent
        : 5,
      nowMs
    );
  }
  // Only observed expired boundaries or the ingestion UPDATE trigger prove rollover.
  // PID watermarks can predate a corrected future deadline and are not expiry evidence.

  const unrewarded = sql.exec('SELECT * FROM tbm_pool_resets WHERE rewarded = 0 AND reset_at_ms <= ? ORDER BY reset_at_ms ASC', nowMs).toArray() as any[];
  for (const r of unrewarded) {
    const eventId = `reset:${r.pool_id}:${r.reset_at_ms}`;
    const pctOnly = Number(r.capacity_known) === 0 || Number(r.total_capacity_tokens) <= 0;
    // Snapshot honors an explicit 0 floor: waste is everything left at the boundary,
    // never a default 5% allowance the deployed policy replaced.
    const hardPct = typeof r.hard_floor_percent === 'number' && Number.isFinite(r.hard_floor_percent) && r.hard_floor_percent >= 0 && r.hard_floor_percent <= 100
      ? r.hard_floor_percent
      : 5;
    let leftoverFraction = 0;
    let leftoverTokens: number | null = null;
    if (pctOnly) {
      const remPct = typeof r.remaining_percent === 'number' && Number.isFinite(r.remaining_percent)
        ? r.remaining_percent
        : (typeof r.headroom_percentage === 'number' && Number.isFinite(r.headroom_percentage) ? r.headroom_percentage : 0);
      leftoverFraction = Math.max(0, (remPct - hardPct) / 100);
      leftoverTokens = null;
    } else {
      const total = Number(r.total_capacity_tokens);
      const rem = Number(r.remaining_tokens);
      const hard = hardPct === 0 ? 0 : Math.min(total, Math.max(total * (hardPct / 100), 50_000));
      const leftover = Math.max(0, rem - hard);
      leftoverFraction = total > 0 ? leftover / total : 0;
      leftoverTokens = Math.round(leftover);
    }
    const wastePenalty = WASTE_AT_RESET_LAMBDA * Math.pow(leftoverFraction, 2);
    sql.exec(
      `INSERT OR IGNORE INTO tbm_reward_events (
        event_id, timestamp_ms, pool_id, intake_task_id, active_mode, swarm_concurrency,
        useful_work_reward, pacing_error_penalty, floor_breach_penalty, engine_mix_penalty,
        waste_at_reset_penalty, banked_waste_penalty, net_reward, token_efficiency_eta,
        tokens_known, tokens_burned, session_id, event_kind
      ) VALUES (?, ?, ?, 'system:reset', 'high', 1, 0, 0, 0, 0, ?, 0, ?, 0, ?, ?, NULL, 'reset')`,
      eventId,
      Number(r.reset_at_ms) || nowMs,
      r.pool_id,
      wastePenalty,
      -wastePenalty,
      pctOnly ? 0 : 1,
      leftoverTokens
    );
    sql.exec('UPDATE tbm_pool_resets SET rewarded = 1 WHERE pool_id = ? AND reset_at_ms = ?', r.pool_id, r.reset_at_ms);
    processed++;
  }
  return processed;
}

function settleTranchesAndPenalties(sql: SqlStorage, nowMs: number): number {
  if (!hasTable(sql, 'tbm_banked_tranches')) return 0;
  const expiring = sql.exec(
    "SELECT * FROM tbm_banked_tranches WHERE status = 'active' AND expires_at_ms <= ?",
    nowMs
  ).toArray() as any[];
  for (const t of expiring) {
    const rem = Number(t.remaining_tokens) || 0;
    const alloc = Math.max(1, Number(t.allocated_tokens) || rem || 1);
    const deadlineOnly = isDeadlineOnlyTranche(t);
    const lostFraction = deadlineOnly ? 1.0 : Math.max(0, rem / alloc);
    if (rem > 0 || deadlineOnly) {
      const penalty = BANKED_WASTE_LAMBDA * lostFraction;
      const eventId = `banked-expire:${t.tranche_id}`;
      sql.exec(
        `INSERT OR IGNORE INTO tbm_reward_events (
          event_id, timestamp_ms, pool_id, intake_task_id, active_mode, swarm_concurrency,
          useful_work_reward, pacing_error_penalty, floor_breach_penalty, engine_mix_penalty,
          waste_at_reset_penalty, banked_waste_penalty, net_reward, token_efficiency_eta,
          tokens_known, tokens_burned, session_id, event_kind
        ) VALUES (?, ?, ?, 'system:banked-expiry', 'high', 1, 0, 0, 0, 0, 0, ?, ?, 0, ?, ?, NULL, 'banked_expiry')`,
        eventId,
        Number(t.expires_at_ms) || nowMs,
        t.pool_id,
        penalty,
        -penalty,
        deadlineOnly ? 0 : 1,
        deadlineOnly ? null : rem
      );
    }
  }
  const trancheCols = tableColumns(sql, 'tbm_banked_tranches');
  const exhaustCond = trancheCols.has('quantity_known')
    ? "status = 'active' AND remaining_tokens <= 0 AND COALESCE(quantity_known, 1) != 0"
    : "status = 'active' AND remaining_tokens <= 0";
  sql.exec(
    "UPDATE tbm_banked_tranches SET status = 'expired', expired_unused_tokens = expired_unused_tokens + remaining_tokens, remaining_tokens = 0, target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE status = 'active' AND expires_at_ms <= ?",
    nowMs,
    nowMs
  );
  sql.exec(
    `UPDATE tbm_banked_tranches SET status = 'exhausted', target_banked_burn_rate_tpm = 0, updated_at_ms = ? WHERE ${exhaustCond}`,
    nowMs
  );
  const active = loadActiveTranches(sql, nowMs);
  for (const t of active) {
    sql.exec(
      'UPDATE tbm_banked_tranches SET target_banked_burn_rate_tpm = ?, updated_at_ms = ? WHERE tranche_id = ?',
      trancheUrgencyTpm(t, nowMs),
      nowMs,
      t.tranche_id
    );
  }
  return expiring.length + active.length;
}

function writePoolRollups(sql: SqlStorage, pools: TbmPoolRow[], nowMs: number): number {
  if (!hasTable(sql, 'tbm_metrics_rollup')) return 0;
  let written = 0;
  const windows: { type: '1h' | '24h' | 'reset_window'; durationMs: number }[] = [
    { type: '1h', durationMs: HOUR },
    { type: '24h', durationMs: DAY },
    { type: 'reset_window', durationMs: 0 },
  ];

  // Fleet-wide engine mix & session stats over 24h.
  let freeShare = 1.0;
  let grokShare = 0.0;
  let rejectRate = 0.0;
  let noRouteRate = 0.0;
  let avgOutcomeSec = 0.0;
  if (hasTable(sql, 'session_control_sessions')) {
    try {
      const sinceIso = new Date(nowMs - DAY).toISOString();
      const sess = sql.exec(
        "SELECT engine, state, created_at, updated_at FROM session_control_sessions WHERE created_at >= ? AND mode != 'interactive' ORDER BY created_at DESC LIMIT 200",
        sinceIso
      ).toArray() as any[];
      if (sess.length > 0) {
        let freeCnt = 0;
        let grokCnt = 0;
        let heldOrRefused = 0;
        let durSum = 0;
        let durCnt = 0;
        for (const s of sess) {
          const prov = s.engine ? engineProvider(s.engine) : null;
          if (prov && FREE_POOL_PROVIDERS.includes(prov)) freeCnt++;
          if (prov === 'grok') grokCnt++;
          if (s.state === 'held' || s.state === 'refused') heldOrRefused++;
          if (s.state === 'stopped' || s.state === 'failed') {
            const d = (Date.parse(s.updated_at) - Date.parse(s.created_at)) / 1000;
            if (Number.isFinite(d) && d >= 0) { durSum += d; durCnt++; }
          }
        }
        freeShare = freeCnt / sess.length;
        grokShare = grokCnt / sess.length;
        rejectRate = heldOrRefused / sess.length;
        noRouteRate = sess.filter(s => s.state === 'held').length / sess.length;
        avgOutcomeSec = durCnt > 0 ? durSum / durCnt : 0;
      }
    } catch {}
  }

  for (const pool of pools) {
    if (pool.pool_id === '*') continue;
    const rem = poolEffectiveRemaining(pool);
    const hard = hardFloorTokens(pool);
    const soft = softReserveTokens(pool, nowMs);
    const hardBreach = rem <= hard ? 1 : 0;
    const softBreach = rem <= soft ? 1 : 0;
    const st = sql.exec('SELECT deficit_tokens FROM tbm_controller_state WHERE pool_id = ?', pool.pool_id).toArray()[0] as any;
    const idleWasted = !isPercentPool(pool) && st && typeof st.deficit_tokens === 'number'
      ? Math.max(0, Math.round(st.deficit_tokens))
      : 0;
    const bankedRows = sql.exec('SELECT allocated_tokens, remaining_tokens, expired_unused_tokens FROM tbm_banked_tranches WHERE pool_id = ?', pool.pool_id).toArray() as any[];
    let bankedBurned = 0;
    let bankedExpired = 0;
    for (const b of bankedRows) {
      const alloc = Number(b.allocated_tokens) || 0;
      const bRem = Number(b.remaining_tokens) || 0;
      const bExp = Number(b.expired_unused_tokens) || 0;
      bankedBurned += Math.max(0, alloc - bRem - bExp);
      bankedExpired += Math.max(0, bExp);
    }

    for (const w of windows) {
      if (w.type === 'reset_window' && !resolveTbmPoolCycleMs(pool)) continue;
      const cycleMs = w.type === 'reset_window'
        ? (resolveTbmPoolCycleMs(pool) ?? w.durationMs)
        : w.durationMs;
      const startMs = w.type === 'reset_window' && pool.reset_at_ms > cycleMs
        ? pool.reset_at_ms - cycleMs
        : nowMs - cycleMs;
      const endMs = nowMs;
      const rewards = sql.exec(
        `SELECT SUM(CASE WHEN tokens_known = 1 AND tokens_burned IS NOT NULL AND event_kind = 'session' THEN tokens_burned ELSE 0 END) AS tok,
                SUM(CASE WHEN event_kind = 'session' AND work_units > 0 THEN work_units ELSE 0 END) AS work,
                SUM(CASE WHEN event_kind = 'session' AND tokens_known = 1 AND tokens_burned > 0 THEN work_units ELSE 0 END) AS measured_work
         FROM tbm_reward_events
         WHERE pool_id = ? AND timestamp_ms >= ? AND timestamp_ms <= ?`,
        pool.pool_id,
        startMs,
        endMs
      ).toArray()[0] as any;
      const resets = sql.exec(
        `SELECT SUM(CASE WHEN capacity_known = 1 AND remaining_tokens > 0 THEN remaining_tokens ELSE 0 END) AS leftover
         FROM tbm_pool_resets
         WHERE pool_id = ? AND reset_at_ms >= ? AND reset_at_ms <= ?`,
        pool.pool_id,
        startMs,
        endMs
      ).toArray()[0] as any;
      const totalTokens = Math.max(0, Math.round(Number(rewards?.tok) || 0));
      const usefulWork = Math.max(0, Number(rewards?.work) || 0);
      const tokensKnown = totalTokens > 0 ? 1 : 0;
      const efficiencyKnown = totalTokens > 0 ? 1 : 0;
      const eta = totalTokens > 0 ? ((Number(rewards?.measured_work) || 0) * 1_000_000) / totalTokens : 0;
      const leftoverAtReset = Math.max(0, Math.round(Number(resets?.leftover) || 0));
      const rollupId = `${pool.pool_id}:${w.type}`;
      sql.exec(
        `INSERT INTO tbm_metrics_rollup (
          rollup_id, window_type, start_time_ms, end_time_ms, pool_id,
          avg_burn_rate_vs_target, leftover_quota_at_reset,
          soft_floor_breach_count, hard_floor_breach_count,
          total_tokens_burned, useful_work_units, token_efficiency_eta,
          idle_wasted_quota, engine_mix_free_share, engine_mix_paid_share,
          engine_mix_grok_share, admission_rejection_rate, no_eligible_route_rate,
          avg_time_to_outcome_seconds, banked_tokens_burned, banked_tokens_expired_unused,
          tokens_known, efficiency_known
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rollup_id) DO UPDATE SET
          start_time_ms = excluded.start_time_ms,
          end_time_ms = excluded.end_time_ms,
          avg_burn_rate_vs_target = excluded.avg_burn_rate_vs_target,
          leftover_quota_at_reset = excluded.leftover_quota_at_reset,
          soft_floor_breach_count = excluded.soft_floor_breach_count,
          hard_floor_breach_count = excluded.hard_floor_breach_count,
          total_tokens_burned = excluded.total_tokens_burned,
          useful_work_units = excluded.useful_work_units,
          token_efficiency_eta = excluded.token_efficiency_eta,
          idle_wasted_quota = excluded.idle_wasted_quota,
          engine_mix_free_share = excluded.engine_mix_free_share,
          engine_mix_paid_share = 0.0,
          engine_mix_grok_share = excluded.engine_mix_grok_share,
          admission_rejection_rate = excluded.admission_rejection_rate,
          no_eligible_route_rate = excluded.no_eligible_route_rate,
          avg_time_to_outcome_seconds = excluded.avg_time_to_outcome_seconds,
          banked_tokens_burned = excluded.banked_tokens_burned,
          banked_tokens_expired_unused = excluded.banked_tokens_expired_unused,
          tokens_known = excluded.tokens_known,
          efficiency_known = excluded.efficiency_known`,
        rollupId,
        w.type,
        startMs,
        endMs,
        pool.pool_id,
        Number.isFinite(pool.pacing_ratio) ? pool.pacing_ratio : 1.0,
        leftoverAtReset,
        softBreach,
        hardBreach,
        totalTokens,
        usefulWork,
        eta,
        idleWasted,
        freeShare,
        grokShare,
        rejectRate,
        noRouteRate,
        avgOutcomeSec,
        bankedBurned,
        bankedExpired,
        tokensKnown,
        efficiencyKnown
      );
      written++;
    }
  }
  return written;
}

export interface TbmTickSummary {
  as_of: string;
  now_ms: number;
  pools_updated: number;
  tranches_updated: number;
  resets_processed: number;
  session_rewards_written: number;
  session_rewards_upgraded: number;
  rollups_written: number;
  routing_factors: Record<string, ProviderPacing>;
}

/**
 * Main persisted TBM controller tick (called by Hub alarm / cron / tests).
 */
export function tickTbm(sql: SqlStorage, nowMs = Date.now()): TbmTickSummary {
  ensureTbmControllerSchema(sql);
  // Effective floors: the tick's targets, reset snapshots and waste penalties must
  // match what admission and routing compute under rules.tbm_floor_percent overrides.
  const initialPools = loadPools(sql, false).map(p => withPolicyTbmFloors(p));
  const resetsProcessed = processResetPenalties(sql, initialPools, nowMs);
  const tranchesUpdated = settleTranchesAndPenalties(sql, nowMs);

  let poolsUpdated = 0;
  for (const pool of initialPools) {
    if (pool.pool_id === '*') continue;
    const saved = sql.exec('SELECT * FROM tbm_controller_state WHERE pool_id = ?', pool.pool_id).toArray()[0] as any;
    const currentRem = poolEffectiveRemaining(pool);
    const sameCycle = saved && Number(saved.last_reset_at_ms) === Number(pool.reset_at_ms);
    const prevPid: PidState = sameCycle
      ? {
          deficitTokens: Number(saved.deficit_tokens) || 0,
          integral: Number(saved.integral) || 0,
          lastRateError: Number(saved.last_rate_error) || 0,
          lastAtMs: typeof saved.last_at_ms === 'number' ? saved.last_at_ms : null,
        }
      : newPidState();

    // Estimate observed burn rate from remaining quota delta across ticks when not externally driven.
    let observedBurnTpm = Number(pool.current_burn_rate_tpm) || 0;
    if (sameCycle && typeof saved.last_at_ms === 'number' && nowMs > saved.last_at_ms && typeof saved.last_remaining === 'number') {
      const dtMin = (nowMs - saved.last_at_ms) / MINUTE;
      const consumed = Math.max(0, saved.last_remaining - currentRem);
      if (dtMin > 0 && consumed > 0) {
        const instantTpm = consumed / dtMin;
        const priorTpm = Number(saved.last_burn_rate_tpm) || observedBurnTpm;
        observedBurnTpm = priorTpm > 0 ? 0.5 * instantTpm + 0.5 * priorTpm : instantTpm;
      }
    }

    const windowMs = resolveTbmPoolCycleMs(pool) ?? undefined;
    const poolForStep: TbmPoolRow = { ...pool, current_burn_rate_tpm: observedBurnTpm };
    const step = pacingStep(poolForStep, prevPid, nowMs, windowMs ? { windowMs } : {});

    sql.exec(
      `INSERT INTO tbm_controller_state (
        pool_id, deficit_tokens, integral, last_rate_error, last_at_ms,
        last_remaining, last_reset_at_ms, last_burn_rate_tpm, updated_at_ms
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(pool_id) DO UPDATE SET
        deficit_tokens = excluded.deficit_tokens,
        integral = excluded.integral,
        last_rate_error = excluded.last_rate_error,
        last_at_ms = excluded.last_at_ms,
        last_remaining = excluded.last_remaining,
        last_reset_at_ms = excluded.last_reset_at_ms,
        last_burn_rate_tpm = excluded.last_burn_rate_tpm,
        updated_at_ms = excluded.updated_at_ms`,
      pool.pool_id,
      step.state.deficitTokens,
      step.state.integral,
      step.state.lastRateError,
      step.state.lastAtMs,
      currentRem,
      pool.reset_at_ms,
      observedBurnTpm,
      nowMs
    );
    sql.exec(
      'UPDATE tbm_quota_pools SET current_burn_rate_tpm = ?, target_burn_rate_tpm = ?, pacing_ratio = ? WHERE pool_id = ?',
      observedBurnTpm,
      step.targetTpm,
      step.pacingRatio,
      pool.pool_id
    );
    poolsUpdated++;
  }

  const sessionRewards = sweepTerminalSessionRewards(sql, nowMs);
  const updatedPools = loadPools(sql, false).map(p => withPolicyTbmFloors(p));
  const rollupsWritten = writePoolRollups(sql, updatedPools, nowMs);
  const routingFactors = Object.fromEntries(loadProviderPacing(sql, nowMs));

  return {
    as_of: new Date(nowMs).toISOString(),
    now_ms: nowMs,
    pools_updated: poolsUpdated,
    tranches_updated: tranchesUpdated,
    resets_processed: resetsProcessed,
    session_rewards_written: sessionRewards.written,
    session_rewards_upgraded: sessionRewards.upgraded,
    rollups_written: rollupsWritten,
    routing_factors: routingFactors,
  };
}
