import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import type { Hub } from '../src/hub';
import { modelCandidates } from '../src/model-routing';
import { evaluateTbmAdmission, isPercentOnlyTbmPool } from '../src/quota_admission';
import {
  hardFloorTokens,
  loadProviderPacing,
  loadProviderEfficiencies,
  pacingFor,
  selectBurnSource,
  softReserveTokens,
  type TbmPoolRow,
  type TbmTrancheRow,
} from '../src/pacing';
import {
  ensureTbmControllerSchema,
  recordSessionOutcomeReward,
  recordSessionTokens,
  tickTbm,
} from '../src/tbm-controller';

const inDO: any = runInDurableObject;
const stub = (name = 'hub') => (env as any).HUB.get((env as any).HUB.idFromName(name));
const HOUR = 3_600_000;
const MIN = 60_000;
const DAY = 24 * HOUR;
const NOW = Date.UTC(2026, 9, 26, 2, 0, 0);

describe('TBM percentage-only pools and cycle_ms (AC2 / AC6)', () => {
  it('evaluates percentage-only pools with 5% hard and 20%->5% convex soft floors over cycle_ms without 50k token floor', () => {
    const pctPool: TbmPoolRow = {
      pool_id: 'claude:acct-a:5h',
      provider: 'claude',
      account: 'acct-a',
      total_capacity_tokens: 0,
      remaining_tokens: 0,
      headroom_percentage: 80,
      remaining_percent: 80,
      capacity_known: 0,
      quota_unit: 'percent',
      reset_at_ms: NOW + 5 * HOUR,
      cycle_ms: 5 * HOUR,
      window_type: '5h',
      hard_floor_percent: 5,
      soft_reserve_percent: 20,
      convexity_exponent: 1.5,
      current_burn_rate_tpm: 0,
      target_burn_rate_tpm: 0,
      pacing_ratio: 1,
      kill_switch_active: 0,
    };
    expect(isPercentOnlyTbmPool(pctPool)).toBe(true);
    expect(hardFloorTokens(pctPool)).toBe(5);
    expect(softReserveTokens(pctPool, NOW)).toBeCloseTo(20);
    expect(softReserveTokens(pctPool, NOW + 2.5 * HOUR)).toBeCloseTo(5 + 15 * Math.pow(0.5, 1.5));
    expect(softReserveTokens(pctPool, NOW + 5 * HOUR)).toBeCloseTo(5);

    // 80% remaining is well above 20% initial soft reserve -> admitted.
    expect(evaluateTbmAdmission({ pools: [pctPool], provider: 'claude', nowMs: NOW }).ok).toBe(true);

    // 14% remaining at start of 5h cycle is below 20% soft reserve -> rejected with soft_reserve.
    const lowStart = { ...pctPool, headroom_percentage: 14, remaining_percent: 14 };
    expect(evaluateTbmAdmission({ pools: [lowStart], provider: 'claude', nowMs: NOW })).toMatchObject({
      ok: false,
      reason: 'soft_reserve',
      pool_id: 'claude:acct-a:5h',
      remaining: 14,
    });

    // 14% remaining with 30m left in the 5h cycle (soft reserve decayed to ~5.47%) -> admitted!
    expect(evaluateTbmAdmission({ pools: [lowStart], provider: 'claude', nowMs: NOW + 4.5 * HOUR }).ok).toBe(true);

    // 4.5% remaining -> rejected with hard_floor even right before reset.
    const belowHard = { ...pctPool, headroom_percentage: 4.5, remaining_percent: 4.5 };
    expect(evaluateTbmAdmission({ pools: [belowHard], provider: 'claude', nowMs: NOW + 4.9 * HOUR })).toMatchObject({
      ok: false,
      reason: 'hard_floor',
      pool_id: 'claude:acct-a:5h',
      r_hard: 5,
      remaining: 4.5,
    });
  });

  it('supports deadline-only banked tranches (quantity_known=0) without premature exhaustion', () => {
    const pool: TbmPoolRow = {
      pool_id: 'zai:acct-1:5h',
      provider: 'zai',
      account: 'acct-1',
      total_capacity_tokens: 0,
      remaining_tokens: 0,
      headroom_percentage: 75,
      remaining_percent: 75,
      capacity_known: 0,
      quota_unit: 'percent',
      reset_at_ms: NOW + 5 * HOUR,
      cycle_ms: 5 * HOUR,
      hard_floor_percent: 5,
      soft_reserve_percent: 20,
      convexity_exponent: 1.5,
      current_burn_rate_tpm: 0,
      target_burn_rate_tpm: 0,
      pacing_ratio: 1,
      kill_switch_active: 0,
    };
    const deadlineTranche: TbmTrancheRow = {
      tranche_id: 'banked-deadline-1',
      pool_id: 'zai:acct-1:5h',
      provider: 'zai',
      account: 'acct-1',
      allocated_tokens: 0,
      remaining_tokens: 0,
      quantity_known: 0,
      expires_at_ms: NOW + 2 * HOUR,
      status: 'active',
      banked_burn_rate_tpm: 0,
      target_banked_burn_rate_tpm: 0,
      expired_unused_tokens: 0,
    };
    const choice = selectBurnSource(pool, [deadlineTranche], NOW);
    expect(choice).not.toBeNull();
    expect(choice!.source).toBe('banked');
    expect(choice!.critical).toBe(true);
    expect(choice!.omega).toBeGreaterThan(3);
  });
});

describe('Persisted PID controller, reset ledger, rewards, rollups & routing feedback (AC2 / AC3)', () => {
  it('persists PID state across tickTbm calls, captures pre-reset leftover via trigger when ingestion advances reset_at_ms, and settles banked expiry penalties', async () => {
    await inDO(stub('tbm-ctrl-1'), async (h: Hub) => {
      const sql = h.sql;
      ensureTbmControllerSchema(sql);

      // Seed a token-known pool and a percent-only pool plus an expiring banked tranche.
      sql.exec(
        `INSERT INTO tbm_quota_pools (
          pool_id, provider, account, total_capacity_tokens, remaining_tokens,
          headroom_percentage, remaining_percent, capacity_known, quota_unit,
          reset_at_ms, cycle_ms, window_type, current_burn_rate_tpm, last_updated_ms
        ) VALUES
          ('zai:shared:5h', 'zai', 'shared', 1000000, 800000, 80, 80, 1, 'tokens', ?, ?, '5h', 0, ?),
          ('claude:main:7d', 'claude', 'main', 0, 0, 72, 72, 0, 'percent', ?, ?, '7d', 0, ?)`,
        NOW + 5 * HOUR, 5 * HOUR, NOW,
        NOW + 7 * DAY, 7 * DAY, NOW
      );
      sql.exec(
        `INSERT INTO tbm_banked_tranches (
          tranche_id, pool_id, provider, account, allocated_tokens, remaining_tokens,
          granted_at_ms, expires_at_ms, status, quantity_known, updated_at_ms
        ) VALUES
          ('tr-exp-1', 'zai:shared:5h', 'zai', 'shared', 200000, 50000, ?, ?, 'active', 1, ?),
          ('tr-deadline-1', 'claude:main:7d', 'claude', 'main', 0, 0, ?, ?, 'active', 0, ?)`,
        NOW - 10 * HOUR, NOW - MIN, NOW,
        NOW - HOUR, NOW + 2 * HOUR, NOW
      );

      // Tick 1 at NOW: initializes persisted PID state, expires tr-exp-1 with banked_waste_penalty,
      // and keeps deadline-only tr-deadline-1 active with target_banked_burn_rate_tpm = 0 while boosting routing urgency.
      const t1 = tickTbm(sql, NOW);
      expect(t1.pools_updated).toBe(2);
      const deadlineRow = sql.exec("SELECT status, target_banked_burn_rate_tpm FROM tbm_banked_tranches WHERE tranche_id = 'tr-deadline-1'").one() as any;
      expect(deadlineRow.status).toBe('active');
      expect(deadlineRow.target_banked_burn_rate_tpm).toBe(0);
      expect(t1.routing_factors['claude:main']?.banked).toBeGreaterThan(1);
      expect(t1.routing_factors['claude:main']?.preempt).toBe(true);
      const state1 = sql.exec("SELECT * FROM tbm_controller_state WHERE pool_id = 'zai:shared:5h'").one() as any;
      expect(state1.last_at_ms).toBe(NOW);
      expect(state1.deficit_tokens).toBe(0);

      const bankedPenalty = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id = 'banked-expire:tr-exp-1'").one() as any;
      expect(bankedPenalty.banked_waste_penalty).toBeCloseTo(250 * (50_000 / 200_000));
      expect(bankedPenalty.net_reward).toBeCloseTo(-62.5);

      // Tick 2 at NOW + 10m with no burn: PID deficit and integral grow in SQLite and raise target_burn_rate_tpm above base.
      const poolAfter1 = sql.exec("SELECT target_burn_rate_tpm FROM tbm_quota_pools WHERE pool_id = 'zai:shared:5h'").one() as any;
      tickTbm(sql, NOW + 10 * MIN);
      const state2 = sql.exec("SELECT * FROM tbm_controller_state WHERE pool_id = 'zai:shared:5h'").one() as any;
      const poolAfter2 = sql.exec("SELECT target_burn_rate_tpm FROM tbm_quota_pools WHERE pool_id = 'zai:shared:5h'").one() as any;
      expect(state2.deficit_tokens).toBeGreaterThan(0);
      expect(state2.integral).toBeGreaterThan(0);
      expect(poolAfter2.target_burn_rate_tpm).toBeGreaterThan(poolAfter1.target_burn_rate_tpm);

      // Simulate ingestion overwriting reset_at_ms to next cycle (reset turnover) with 650,000 tokens still unspent!
      const oldResetMs = NOW + 5 * HOUR;
      const newResetMs = NOW + 10 * HOUR;
      sql.exec(
        "UPDATE tbm_quota_pools SET remaining_tokens = 650000, headroom_percentage = 65, remaining_percent = 65 WHERE pool_id = 'zai:shared:5h'"
      );
      // Ingestion replaces reset_at_ms and refills remaining_tokens to 1,000,000 BEFORE tickTbm runs:
      sql.exec(
        "UPDATE tbm_quota_pools SET remaining_tokens = 1000000, headroom_percentage = 100, remaining_percent = 100, reset_at_ms = ?, last_updated_ms = ? WHERE pool_id = 'zai:shared:5h'",
        newResetMs,
        oldResetMs + 1000
      );

      // Tick 3 processes the captured reset boundary from tbm_pool_resets and resets PID state for the new cycle!
      const t3 = tickTbm(sql, oldResetMs + 2000);
      expect(t3.resets_processed).toBe(1);
      const resetEvent = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id = ?", `reset:zai:shared:5h:${oldResetMs}`).one() as any;
      // Leftover above 50k hard floor is 600k / 1M = 0.6 -> penalty = 500 * 0.6^2 = 180.
      expect(resetEvent.waste_at_reset_penalty).toBeCloseTo(180);
      expect(resetEvent.tokens_burned).toBe(600_000);
      expect(resetEvent.tokens_known).toBe(1);

      // Re-running tickTbm does not duplicate the reset penalty.
      const t4 = tickTbm(sql, oldResetMs + 3000);
      expect(t4.resets_processed).toBe(0);
    });
  });

  it('writes idempotent session outcome rewards, keeps unknown tokens honest, upgrades when delayed native tokens arrive, and feeds efficiency back into routing', async () => {
    await inDO(stub('tbm-ctrl-2'), async (h: Hub) => {
      const sql = h.sql;
      ensureTbmControllerSchema(sql);

      sql.exec(
        `INSERT INTO tbm_quota_pools (
          pool_id, provider, account, total_capacity_tokens, remaining_tokens,
          headroom_percentage, remaining_percent, capacity_known, quota_unit,
          reset_at_ms, cycle_ms, window_type, current_burn_rate_tpm, target_burn_rate_tpm, last_updated_ms
        ) VALUES
          ('codex:acct-fast:5h', 'codex', 'acct-fast', 0, 0, 80, 80, 0, 'percent', ?, ?, '5h', 0.2, 0.2, ?),
          ('codex:acct-slow:5h', 'codex', 'acct-slow', 0, 0, 80, 80, 0, 'percent', ?, ?, '5h', 0.2, 0.2, ?)`,
        NOW + 5 * HOUR, 5 * HOUR, NOW,
        NOW + 5 * HOUR, 5 * HOUR, NOW
      );

      // 0. Manual stop without acceptance evidence writes reward row with useful_work_reward = 0.
      const manualStop = recordSessionOutcomeReward(sql, {
        session_id: 'sess-manual-stop',
        intake_task_id: 'intake-1',
        engine: 'codex',
        account: 'acct-fast',
        outcome: 'stopped',
      }, NOW);
      expect(manualStop.useful_work_reward).toBe(0);
      expect(manualStop.tokens_known).toBe(0);
      expect(manualStop.efficiency_known).toBe(0);

      // 1. Accepted session finishes before rollout token collector has reported native tokens -> tokens_known = 0, efficiency_known = 0.
      const initial = recordSessionOutcomeReward(sql, {
        session_id: 'sess-delayed-1',
        intake_task_id: 'intake-1',
        engine: 'codex',
        account: 'acct-fast',
        outcome: 'done',
      }, NOW);
      expect(initial.tokens_known).toBe(0);
      expect(initial.efficiency_known).toBe(0);
      expect(initial.tokens_burned).toBeNull();
      expect(initial.token_efficiency_eta).toBe(0);
      expect(initial.useful_work_reward).toBeGreaterThan(0);

      // Calling again while tokens are still unknown is idempotent.
      const replayUnknown = recordSessionOutcomeReward(sql, {
        session_id: 'sess-delayed-1',
        intake_task_id: 'intake-1',
        engine: 'codex',
        account: 'acct-fast',
        outcome: 'done',
      }, NOW + 1000);
      expect(replayUnknown.tokens_known).toBe(0);

      // 2. Delayed native token usage arrives for sess-delayed-1 (100k tokens -> high efficiency 100 VU/MTok).
      recordSessionTokens(sql, {
        session_id: 'sess-delayed-1',
        engine: 'codex',
        account: 'acct-fast',
        input_tokens: 60_000,
        output_tokens: 40_000,
        observed_at_ms: NOW + 5000,
      }, NOW + 5000);

      const upgraded = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id = 'session:sess-delayed-1'").one() as any;
      expect(upgraded.tokens_known).toBe(1);
      expect(upgraded.efficiency_known).toBe(1);
      expect(upgraded.tokens_burned).toBe(100_000);
      expect(upgraded.token_efficiency_eta).toBeCloseTo(100); // 10 VU / 0.1 MTok = 100 VU/MTok

      // 3. Record a low-efficiency session on acct-slow (2M tokens for 10 VU = 5 VU/MTok).
      recordSessionOutcomeReward(sql, {
        session_id: 'sess-slow-1',
        intake_task_id: 'intake-1',
        engine: 'codex',
        account: 'acct-slow',
        outcome: 'done',
        tokens_burned: 2_000_000,
      }, NOW + 6000);

      tickTbm(sql, NOW + 10_000);
      const rollups = sql.exec("SELECT * FROM tbm_metrics_rollup WHERE pool_id = 'codex:acct-fast:5h'").toArray() as any[];
      expect(rollups.length).toBeGreaterThanOrEqual(3);

      // 4. Verify routing factors & modelCandidates reflect per-account efficiency and per-account pacing!
      const pacingMap = loadProviderPacing(sql, NOW + 10_000);
      const fastPacing = pacingFor(pacingMap, 'codex', 'acct-fast');
      const slowPacing = pacingFor(pacingMap, 'codex', 'acct-slow');
      expect(fastPacing.efficiency).toBeGreaterThan(1.0);
      expect(slowPacing.efficiency).toBeLessThan(1.0);
      expect(fastPacing.efficiency!).toBeGreaterThan(slowPacing.efficiency!);

      const quota = {
        status: 'ok',
        observed_at: new Date(NOW).toISOString(),
        details: { limit_reached: false },
        windows: { '5h': { present: true, percent_remaining: 80, reset_at: new Date(NOW + 5 * HOUR).toISOString() } },
      };
      const adsFast = [{ engine: 'codex', profile: null, model: 'gpt-6-astra', effort: 'high', account: 'acct-fast' }];
      const adsSlow = [{ engine: 'codex', profile: null, model: 'gpt-6-astra', effort: 'high', account: 'acct-slow' }];
      const [candFast] = modelCandidates({ tier: 'deep', preferred_runner: 'codex' }, { provider: 'codex', engine: 'codex', profile: null, account: 'acct-fast' }, adsFast, quota, NOW + 10_000, pacingMap);
      const [candSlow] = modelCandidates({ tier: 'deep', preferred_runner: 'codex' }, { provider: 'codex', engine: 'codex', profile: null, account: 'acct-slow' }, adsSlow, quota, NOW + 10_000, pacingMap);
      expect(candFast.weight).toBeGreaterThan(candSlow.weight);
    });
  });
});

describe('Staged low-quota floor and kill-switch end-to-end scenario (AC6)', () => {
  let cookie: string;
  let seq = 0;

  async function api(path: string, payload?: any) {
    const res = await SELF.fetch('https://hub.test' + path, {
      method: payload ? 'POST' : 'GET',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf' },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
    });
    return { status: res.status, data: (await res.json()) as any };
  }

  beforeEach(async () => {
    cookie = (
      await createSessionCookie(env as any, {
        sub: 'operator',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'csrf',
        exp: Math.floor(Date.now() / 1000) + 3600,
      })
    ).split(';')[0];
    await inDO(stub(), (h: any) => {
      h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
      ensureTbmControllerSchema(h.sql);
      for (const t of ['tasks', 'grants', 'rules', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
      h.sql.exec('UPDATE intake_meta SET revision=0');
      h.sql.exec('DELETE FROM tbm_quota_pools');
      h.sql.exec('DELETE FROM tbm_banked_tranches');
      h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
    });
  });

  it('enforces soft reserve, hard floor, and kill-switch on percentage-only pools while preserving interactive sessions', async () => {
    const taskRes = await api('/api/intake/tasks', {
      key: 'tbm-ac6-task-' + ++seq,
      project: 'demo',
      title: 'AC6 low-quota drill',
      text: 'Verify floors and kill-switch on percent-only pools',
      source: { type: 'unknown', reference: null, timestamp: null },
      acceptance_criteria: 'Floors and kill-switch trigger on real data.',
    });
    expect(taskRes.status).toBe(200);
    const intakeId = taskRes.data.task.id;

    const now = Date.now();
    // Stage 1: Percent-only pool at 12% early in 5h cycle -> denied by soft_reserve (20% initial soft reserve).
    await inDO(stub(), (h: any) => {
      h.sql.exec(
        `INSERT OR REPLACE INTO tbm_quota_pools (
          pool_id, provider, account, total_capacity_tokens, remaining_tokens,
          headroom_percentage, remaining_percent, capacity_known, quota_unit,
          reset_at_ms, cycle_ms, window_type, kill_switch_active, last_updated_ms
        ) VALUES ('claude:default:5h', 'claude', 'default', 0, 0, 12, 12, 0, 'percent', ?, ?, '5h', 0, ?)`,
        now + 5 * HOUR,
        5 * HOUR,
        now
      );
    });
    const stage1 = await api('/api/sessions', {
      key: 'tbm-ac6-s1-' + ++seq,
      workspace: 'demo',
      mode: 'headless',
      brief: 'Stage 1',
      intake_task_id: intakeId,
      engine: 'claude',
      engine_override_reason: 'Select Claude to exercise its live percentage pool floor.',
    });
    expect(stage1.status).toBe(409);
    expect(stage1.data).toMatchObject({
      error: 'tbm_admission_denied',
      reason: 'soft_reserve',
      pool_id: 'claude:default:5h',
    });

    // Stage 2: Same 12% remaining when only 20m remain before 5h reset -> soft reserve has decayed near 5%, so admitted!
    await inDO(stub(), (h: any) => {
      h.sql.exec(
        "UPDATE tbm_quota_pools SET reset_at_ms = ?, last_updated_ms = ? WHERE pool_id = 'claude:default:5h'",
        now + 20 * MIN,
        now
      );
    });
    const stage2 = await api('/api/sessions', {
      key: 'tbm-ac6-s2-' + ++seq,
      workspace: 'demo',
      mode: 'headless',
      brief: 'Stage 2',
      intake_task_id: intakeId,
      engine: 'claude',
      engine_override_reason: 'Select Claude to exercise its live percentage pool floor.',
    });
    expect(stage2.data.error).not.toBe('tbm_admission_denied');

    // Stage 3: Pool drops to 4.8% (below 5% hard floor) -> denied with hard_floor even 20m before reset.
    await inDO(stub(), (h: any) => {
      h.sql.exec(
        "UPDATE tbm_quota_pools SET headroom_percentage = 4.8, remaining_percent = 4.8, last_updated_ms = ? WHERE pool_id = 'claude:default:5h'",
        now
      );
    });
    const stage3 = await api('/api/sessions', {
      key: 'tbm-ac6-s3-' + ++seq,
      workspace: 'demo',
      mode: 'headless',
      brief: 'Stage 3',
      intake_task_id: intakeId,
      engine: 'claude',
      engine_override_reason: 'Select Claude to exercise its live percentage pool floor.',
    });
    expect(stage3.status).toBe(409);
    expect(stage3.data).toMatchObject({
      error: 'tbm_admission_denied',
      reason: 'hard_floor',
      pool_id: 'claude:default:5h',
    });

    // Stage 4: Global kill-switch on '*' blocks autonomous starts with 423, while interactive sessions pass.
    await inDO(stub(), (h: any) => {
      h.sql.exec(
        `INSERT OR REPLACE INTO tbm_quota_pools (
          pool_id, provider, account, total_capacity_tokens, remaining_tokens,
          headroom_percentage, reset_at_ms, kill_switch_active, kill_switch_reason, last_updated_ms
        ) VALUES ('*', '*', '*', 1, 1, 100, ?, 1, 'emergency_stop', ?)`,
        now + HOUR,
        now
      );
    });
    const stage4Auto = await api('/api/sessions', {
      key: 'tbm-ac6-s4a-' + ++seq,
      workspace: 'demo',
      mode: 'headless',
      brief: 'Stage 4 auto',
      intake_task_id: intakeId,
    });
    expect(stage4Auto.status).toBe(423);
    expect(stage4Auto.data).toMatchObject({
      error: 'tbm_admission_denied',
      reason: 'kill_switch_active',
      pool_id: '*',
      detail: 'emergency_stop',
    });

    const stage4Interactive = await api('/api/sessions', {
      key: 'tbm-ac6-s4i-' + ++seq,
      workspace: 'demo',
      mode: 'interactive',
    });
    expect(stage4Interactive.data.error).not.toBe('tbm_admission_denied');
  });
});


describe('TBM late telemetry preserves accepted work', () => {
  it('preserves custom work and original penalties across cumulative token updates and accepts a later verified result', async () => {
    await inDO(stub('late-work'), (h: any) => {
      const sql = h.sql;
      ensureTbmControllerSchema(sql);
      const initial: any = recordSessionOutcomeReward(sql, {
        session_id: 'custom', outcome: 'done', value_weight: 40, verification_fidelity: 0.5,
      }, NOW);
      expect(initial.work_units).toBe(20);
      recordSessionTokens(sql, { session_id: 'custom', total_tokens: 100000 }, NOW + MIN);
      const first: any = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id='session:custom'").one();
      expect(first.work_units).toBe(20);
      expect(first.token_efficiency_eta).toBe(200);
      recordSessionTokens(sql, { session_id: 'custom', total_tokens: 200000 }, NOW + 2*MIN);
      const final: any = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id='session:custom'").one();
      expect(final.work_units).toBe(20);
      expect(final.token_efficiency_eta).toBe(100);
      expect(final.engine_mix_penalty).toBe(initial.engine_mix_penalty);
      expect(final.timestamp_ms).toBe(NOW);
      recordSessionOutcomeReward(sql, { session_id: 'accepted-late', outcome: 'stopped', tokens_burned: 100000 }, NOW);
      const accepted: any = recordSessionOutcomeReward(sql, { session_id: 'accepted-late', outcome: 'done', tokens_burned: 100000 }, NOW + MIN);
      expect(accepted.work_units).toBe(10);
      expect(accepted.token_efficiency_eta).toBe(100);
    });
  });
});


it('does not penalize future deadline corrections or contaminate efficiency with unused quota', async () => {
  await inDO(stub('reset-corrections'), (h: any) => {
    const sql = h.sql;
    ensureTbmControllerSchema(sql);
    sql.exec(`INSERT INTO tbm_quota_pools (pool_id,provider,account,total_capacity_tokens,remaining_tokens,
      headroom_percentage,reset_at_ms,last_updated_ms,cycle_ms) VALUES ('codex:correction:5h','codex','correction',1000000,800000,80,?,?,?)`, NOW+HOUR,NOW,5*HOUR);
    recordSessionOutcomeReward(sql, {session_id:'accepted',outcome:'done',tokens_burned:100000,pool_id:'codex:correction:5h'}, NOW);
    const before = loadProviderEfficiencies(sql,NOW).get('codex:correction');
    tickTbm(sql, NOW);
    sql.exec("UPDATE tbm_quota_pools SET reset_at_ms=?,last_updated_ms=? WHERE pool_id='codex:correction:5h'", NOW+2*HOUR, NOW+MIN);
    tickTbm(sql,NOW+HOUR+MIN);
    expect(sql.exec("SELECT * FROM tbm_reward_events WHERE event_kind='reset'").toArray()).toHaveLength(0);
    tickTbm(sql,NOW+2*HOUR+MIN);
    expect(sql.exec("SELECT * FROM tbm_reward_events WHERE event_kind='reset'").toArray()).toHaveLength(1);
    expect(loadProviderEfficiencies(sql,NOW+2*HOUR+MIN).get('codex:correction')).toBe(before);
  });
});

it('updates an older reward directly even after more than 100 newer outcomes', async () => {
  await inDO(stub('older-token-final'), (h: any) => {
    const sql = h.sql;
    recordSessionOutcomeReward(sql, {session_id:'older',outcome:'done'}, NOW);
    for(let i=0;i<101;i++) sql.exec(`INSERT INTO tbm_reward_events
      (event_id,timestamp_ms,pool_id,intake_task_id,active_mode,swarm_concurrency,useful_work_reward,
      pacing_error_penalty,floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,banked_waste_penalty,net_reward,token_efficiency_eta,session_id)
      SELECT ?,?,pool_id,intake_task_id,active_mode,swarm_concurrency,useful_work_reward,
      pacing_error_penalty,floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,banked_waste_penalty,net_reward,token_efficiency_eta,?
      FROM tbm_reward_events WHERE event_id='session:older'`, 'session:newer-'+i,NOW+i+1,'newer-'+i);
    recordSessionTokens(sql, {session_id:'older',total_tokens:12345}, NOW+MIN);
    expect(sql.exec("SELECT tokens_burned FROM tbm_reward_events WHERE event_id='session:older'").one().tokens_burned).toBe(12345);
  });
});


it('links routed task rewards to measured tokens on the exact launch machine', async () => {
  await inDO(stub('routed-native'), (h: any) => {
    const sql = h.sql;
    sql.exec(`INSERT INTO fleet_launches
      (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      VALUES ('route-launch','a','agent','review','route-key','{}','host','codex','acct','headless','demo','review','exited','native',?,?)`,new Date(NOW).toISOString(),new Date(NOW).toISOString());
    sql.exec(`INSERT INTO routing_jobs (id,actor,request_key,request_json,launch_id,state,created_at,updated_at)
      VALUES ('route-job','a','job-key','{}','route-launch','done',?,?)`,new Date(NOW).toISOString(),new Date(NOW).toISOString());
    recordSessionTokens(sql,{session_id:'foreign:native',native_id:'native',total_tokens:999999},NOW);
    const unknown: any = recordSessionOutcomeReward(sql,{session_id:'route-job',outcome:'done'},NOW);
    expect(unknown.tokens_known).toBe(0);
    recordSessionTokens(sql,{session_id:'host:native',native_id:'native',total_tokens:100000},NOW+MIN);
    const first: any = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id='session:route-job'").one();
    expect(first.tokens_burned).toBe(100000);
    expect(first.token_efficiency_eta).toBe(100);
    recordSessionTokens(sql,{session_id:'host:native',native_id:'native',total_tokens:200000},NOW+2*MIN);
    const final: any = sql.exec("SELECT * FROM tbm_reward_events WHERE event_id='session:route-job'").one();
    expect(final.tokens_burned).toBe(200000);
    expect(final.token_efficiency_eta).toBe(50);
  });
});

it('reports total accepted work while measuring efficiency only on the known-cost cohort', async () => {
  await inDO(stub('rollup-cohort'), (h: any) => {
    const sql = h.sql;
    sql.exec(`INSERT INTO tbm_quota_pools (pool_id,provider,account,total_capacity_tokens,remaining_tokens,
      headroom_percentage,reset_at_ms,last_updated_ms,cycle_ms) VALUES ('codex:cohort:5h','codex','cohort',1000000,800000,80,?,?,?)`,NOW+5*HOUR,NOW,5*HOUR);
    recordSessionOutcomeReward(sql,{session_id:'known',outcome:'done',pool_id:'codex:cohort:5h',tokens_burned:100000},NOW);
    recordSessionOutcomeReward(sql,{session_id:'unknown',outcome:'done',pool_id:'codex:cohort:5h'},NOW);
    tickTbm(sql,NOW+MIN);
    const row: any = sql.exec("SELECT * FROM tbm_metrics_rollup WHERE pool_id='codex:cohort:5h' AND window_type='1h'").one();
    expect(row.useful_work_units).toBe(20);
    expect(row.total_tokens_burned).toBe(100000);
    expect(row.token_efficiency_eta).toBe(100);
  });
});
