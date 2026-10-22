import { describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { defaultEnv } from './m1-focus-setup';
import type { Hub } from '../src/hub';
import { ensureTbmQuotaSchema } from '../src/quota';
import { modelCandidates, policy } from '../src/model-routing';
import {
  bankedUrgencyMultiplier, baseTargetTpm, edfOrder, grokDemotion, hardFloorTokens, loadProviderPacing, netHeadroom,
  newPidState, pacingAlignment, pacingStep, providerPacing, providerPreference, refreshPacing, resetUrgency, routeScore,
  selectBurnSource, softReserveTokens, taskFit, type PidState, type TbmPoolRow, type TbmTrancheRow,
} from '../src/pacing';

const H = 3600000; const MIN = 60000; const NOW = Date.UTC(2026, 9, 22, 12);
const pool = (o: Partial<TbmPoolRow> = {}): TbmPoolRow => ({
  pool_id: 'zai:default', provider: 'zai', account: 'default', total_capacity_tokens: 10_000_000, remaining_tokens: 6_000_000,
  headroom_percentage: 60, reset_at_ms: NOW + 10 * H, hard_floor_percent: 5, soft_reserve_percent: 20, convexity_exponent: 1.5,
  current_burn_rate_tpm: 0, target_burn_rate_tpm: 0, pacing_ratio: 1, kill_switch_active: 0, ...o,
});
const tranche = (id: string, hours: number, remaining = 1_000_000, o: Partial<TbmTrancheRow> = {}): TbmTrancheRow => ({
  tranche_id: id, pool_id: 'zai:default', provider: 'zai', remaining_tokens: remaining, expires_at_ms: NOW + hours * H,
  status: 'active', banked_burn_rate_tpm: 0, target_banked_burn_rate_tpm: 0, expired_unused_tokens: 0, ...o,
});

describe('safety floors', () => {
  it('hard floor is max(percent, absolute) and capped by the pool', () => {
    expect(hardFloorTokens(pool())).toBe(500_000);
    expect(hardFloorTokens(pool({ total_capacity_tokens: 100_000 }))).toBe(50_000);
    expect(hardFloorTokens(pool({ total_capacity_tokens: 10_000 }))).toBe(10_000);
  });
  it('soft reserve decays convexly from the initial buffer to the hard floor at reset', () => {
    const p = pool({ reset_at_ms: NOW + 168 * H });
    expect(softReserveTokens(p, NOW)).toBeCloseTo(2_000_000);
    expect(softReserveTokens(p, NOW + 168 * H)).toBeCloseTo(500_000);
    // Halfway: R_hard + 1.5M * 0.5^1.5.
    expect(softReserveTokens(p, NOW + 84 * H)).toBeCloseTo(500_000 + 1_500_000 * Math.pow(0.5, 1.5));
  });
});

describe('continuous target burn', () => {
  it('base target is max(0, Q_rem - R_soft) / time_to_reset in tokens/minute', () => {
    const p = pool();
    const expected = (6_000_000 - softReserveTokens(p, NOW)) / (10 * 60);
    expect(baseTargetTpm(p, NOW)).toBeCloseTo(expected);
    expect(baseTargetTpm(pool({ remaining_tokens: 100_000 }), NOW)).toBe(0);
    // Epsilon guard: at/after reset the denominator is 10 s, never zero.
    expect(Number.isFinite(baseTargetTpm(pool({ reset_at_ms: NOW }), NOW))).toBe(true);
  });
  it('first PID step has no history, so target equals the base term', () => {
    const r = pacingStep(pool(), newPidState(), NOW);
    expect(r.targetTpm).toBeCloseTo(r.baseTpm); expect(r.smoothingTpm).toBeCloseTo(0);
  });
  it('a persistent under-burn accumulates deficit and raises the target above base', () => {
    let s: PidState = newPidState(); let r = pacingStep(pool(), s, NOW); s = r.state;
    for (let i = 1; i <= 6; i++) { r = pacingStep(pool(), s, NOW + i * 10 * MIN); s = r.state; }
    expect(r.state.deficitTokens).toBeGreaterThan(0);
    expect(r.state.integral).toBeGreaterThan(0);
    expect(r.targetTpm).toBeGreaterThan(r.baseTpm);
    // Over-burn pulls the target below base.
    const over = pool({ current_burn_rate_tpm: 50_000 });
    let s2 = pacingStep(over, newPidState(), NOW).state; const r2 = pacingStep(over, s2, NOW + 30 * MIN);
    expect(r2.targetTpm).toBeLessThan(r2.baseTpm);
  });
  it('anti-windup freezes the integral while saturated or held, and clamps to the actuator ceiling', () => {
    const s0 = pacingStep(pool(), newPidState(), NOW).state;
    const sat = pacingStep(pool(), s0, NOW + 10 * MIN, { maxBurnTpm: 100 });
    expect(sat.saturated).toBe(true); expect(sat.targetTpm).toBe(100); expect(sat.state.integral).toBe(s0.integral);
    const held = pacingStep(pool(), s0, NOW + 10 * MIN, { heldUntilMs: NOW + H });
    expect(held.held).toBe(true); expect(held.targetTpm).toBe(0); expect(held.state.integral).toBe(s0.integral);
  });
  it('flags the anti-cliff horizon when surplus cannot be drained at 80% of max burn', () => {
    const near = pool({ reset_at_ms: NOW + 20 * MIN, remaining_tokens: 3_000_000 });
    expect(pacingStep(near, newPidState(), NOW, { maxBurnTpm: 50_000 }).antiCliff).toBe(true);
    expect(pacingStep(pool(), newPidState(), NOW, { maxBurnTpm: 50_000 }).antiCliff).toBe(false);
  });
  it('reports pacing ratio = actual / target', () => {
    const r = pacingStep(pool({ current_burn_rate_tpm: 3000 }), newPidState(), NOW);
    expect(r.pacingRatio).toBeCloseTo(3000 / r.targetTpm);
  });
});

describe('banked EDF arbiter', () => {
  it('orders active tranches by earliest expiry and drops expired, exhausted and empty ones', () => {
    const order = edfOrder([tranche('late', 30), tranche('soon', 2), tranche('mid', 10), tranche('gone', -1),
      tranche('empty', 1, 0), tranche('done', 1, 5, { status: 'exhausted' }), tranche('tie-small', 10, 10)], NOW);
    expect(order.map(t => t.tranche_id)).toEqual(['soon', 'mid', 'tie-small', 'late']);
  });
  it('urgency multiplier is 1 outside 24h and rises to 4 at expiry', () => {
    expect(bankedUrgencyMultiplier(NOW + 30 * H, NOW)).toBe(1);
    expect(bankedUrgencyMultiplier(NOW + 24 * H, NOW)).toBe(1);
    expect(bankedUrgencyMultiplier(NOW + 12 * H, NOW)).toBeCloseTo(1.75);
    expect(bankedUrgencyMultiplier(NOW + 1, NOW)).toBeCloseTo(4, 3);
  });
  it('dispatches to the EDF head inside the 6h window, critical inside 4h', () => {
    const r = selectBurnSource(pool(), [tranche('b', 20), tranche('a', 3)], NOW)!;
    expect(r.source).toBe('banked'); expect(r.tranche!.tranche_id).toBe('a'); expect(r.critical).toBe(true);
    const r5 = selectBurnSource(pool(), [tranche('a', 5)], NOW)!;
    expect(r5.source).toBe('banked'); expect(r5.critical).toBe(false);
  });
  it('prefers banked when its urgency exceeds the primary pool, else primary', () => {
    // 5M over 12h (6944 tpm) is slower than primary's 5.5M above R_hard over 10h (9167 tpm).
    expect(selectBurnSource(pool(), [tranche('a', 12, 5_000_000)], NOW)!.source).toBe('primary');
    expect(selectBurnSource(pool(), [tranche('a', 12, 9_000_000)], NOW)!.source).toBe('banked');
  });
  it('never treats banked as a substitute for primary reserves', () => {
    expect(selectBurnSource(pool({ remaining_tokens: 400_000 }), [tranche('a', 1)], NOW)).toBeNull();
    expect(selectBurnSource(pool({ kill_switch_active: 1 }), [tranche('a', 1)], NOW)).toBeNull();
  });
});

describe('routing score factors', () => {
  it('free pool 1.3x, codex 0.9x, others 1.0; grok demoted once by 0.2x', () => {
    expect(providerPreference('zai')).toBe(1.3); expect(providerPreference('go')).toBe(1.3);
    expect(providerPreference('codex')).toBe(0.9); expect(providerPreference('gemini')).toBe(1);
    expect(providerPreference('grok')).toBe(1); expect(grokDemotion('grok')).toBe(0.2); expect(grokDemotion('zai')).toBe(1);
  });
  it('task fit allows down-tiering deep->daily at 0.5 and forbids up-tiering', () => {
    expect(taskFit('daily', 'daily')).toBe(1); expect(taskFit('deep', 'daily')).toBe(0.5);
    expect(taskFit('quick', 'daily')).toBe(0); expect(taskFit('daily', 'deep')).toBe(0);
  });
  it('net headroom is measured above the floor with a 0.05 minimum', () => {
    expect(netHeadroom(60, 20)).toBeCloseTo(0.5); expect(netHeadroom(10, 15)).toBe(0.05); expect(netHeadroom(100, 0)).toBe(1);
  });
  it('reset urgency grows near reset and is suppressed at the floor', () => {
    expect(resetUrgency(1, 30)).toBe(1); expect(resetUrgency(1, 0)).toBeCloseTo(3.5);
    expect(resetUrgency(0.5, 12)).toBeCloseTo(1 + 2.5 * 0.5 * Math.pow(0.5, 1.5));
    expect(resetUrgency(0.05, 0)).toBeCloseTo(1.125);
  });
  it('pacing alignment boosts lagging providers up to 2.5x and damps over-burners to 0.2x', () => {
    expect(pacingAlignment(0, 1000)).toBe(2); expect(pacingAlignment(500, 1000)).toBe(1.5);
    expect(pacingAlignment(1000, 1000)).toBe(1); expect(pacingAlignment(2000, 1000)).toBe(0.5);
    expect(pacingAlignment(100_000, 1000)).toBe(0.2); expect(pacingAlignment(10, 0)).toBe(1);
  });
  it('score is the product of all factors', () => {
    expect(routeScore({ fit: 1, health: 1, preference: 1.3, headroom: 0.5, urgency: 2, pacing: 1.5, grok: 1, banked: 2 })).toBeCloseTo(3.9);
  });
  it('config policy mirrors the Phase 3 constants', () => {
    expect(policy.rules.provider_preference).toBe(1.3); expect(policy.rules.reset_urgency_factor).toBe(2.5);
  });
  it('providerPacing aggregates lag, banked boost and critical preemption per provider', () => {
    const map = providerPacing([pool({ current_burn_rate_tpm: 0, target_burn_rate_tpm: 1000 }),
      pool({ pool_id: 'codex:main', provider: 'codex', current_burn_rate_tpm: 5000, target_burn_rate_tpm: 1000, kill_switch_active: 1 })],
    [tranche('a', 2)], NOW);
    expect(map.get('zai')).toEqual({ pacing: 2, banked: bankedUrgencyMultiplier(NOW + 2 * H, NOW), preempt: true, halted: false });
    expect(map.get('codex')).toMatchObject({ pacing: 0.2, halted: true, preempt: false });
  });
  it('modelCandidates applies the multi-factor weight and drops kill-switched providers', () => {
    const quota = { status: 'ok', observed_at: new Date(NOW - 1000).toISOString(), details: { limit_reached: false },
      windows: { '7d': { present: true, percent_remaining: 80, reset_at: new Date(NOW + 2 * H).toISOString() } } };
    const route = { provider: 'go', engine: 'opencode', profile: null, account: 'g' };
    const ads = [{ engine: 'opencode', profile: null, model: 'opencode-go/muse-spark-1.3-contributor', effort: null, account: 'g' }];
    const [neutral] = modelCandidates({ tier: 'daily' }, route, ads, quota, NOW) as any[];
    const urgency = resetUrgency(0.8, 2);
    expect(neutral.factors).toEqual({ fit: 1, health: 1, preference: 1.3, headroom: 0.8, urgency, pacing: 1, grok: 1, banked: 1 });
    expect(neutral.weight).toBeCloseTo(1.3 * 0.8 * urgency); expect(neutral.preempt).toBe(false);
    const lag = new Map([['go', { pacing: 2, banked: 3, preempt: true, halted: false }]]);
    const [boosted] = modelCandidates({ tier: 'daily' }, route, ads, quota, NOW, lag) as any[];
    expect(boosted.weight).toBeCloseTo(neutral.weight * 6); expect(boosted.preempt).toBe(true);
    expect(modelCandidates({ tier: 'daily' }, route, ads, quota, NOW, new Map([['go', { pacing: 1, banked: 1, preempt: false, halted: true }]]))).toEqual([]);
  });
});

describe('Phase 1 schema integration', () => {
  it('refreshPacing settles tranches and writes targets into existing tbm_* columns only', async () => {
    const stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('pacing-test'));
    await runInDurableObject(stub, async (instance: Hub) => {
      const sql = instance.sql; ensureTbmQuotaSchema(sql);
      const p = pool({ current_burn_rate_tpm: 1000 });
      sql.exec(`INSERT INTO tbm_quota_pools (pool_id, provider, account, total_capacity_tokens, remaining_tokens, headroom_percentage, reset_at_ms, current_burn_rate_tpm, last_updated_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        p.pool_id, p.provider, p.account, p.total_capacity_tokens, p.remaining_tokens, p.headroom_percentage, p.reset_at_ms, p.current_burn_rate_tpm, NOW);
      const ins = (id: string, hours: number, remaining: number) => sql.exec(`INSERT INTO tbm_banked_tranches (tranche_id, pool_id, provider, allocated_tokens, remaining_tokens, granted_at_ms, expires_at_ms, updated_at_ms) VALUES (?, ?, 'zai', ?, ?, ?, ?, ?)`,
        id, p.pool_id, remaining, remaining, NOW - H, NOW + hours * H, NOW);
      ins('dead', -1, 700); ins('empty', 5, 0); ins('live', 3, 1_800_000);
      const states = new Map<string, PidState>();
      const [result] = refreshPacing(sql, NOW, states);
      const row: any = sql.exec('SELECT * FROM tbm_quota_pools').one();
      expect(row.target_burn_rate_tpm).toBeCloseTo(result.targetTpm); expect(row.pacing_ratio).toBeCloseTo(1000 / result.targetTpm);
      expect(states.get(p.pool_id)!.lastAtMs).toBe(NOW);
      const t = Object.fromEntries((sql.exec('SELECT * FROM tbm_banked_tranches').toArray() as any[]).map(r => [r.tranche_id, r]));
      expect(t.dead).toMatchObject({ status: 'expired', remaining_tokens: 0, expired_unused_tokens: 700 });
      expect(t.empty.status).toBe('exhausted');
      expect(t.live.target_banked_burn_rate_tpm).toBeCloseTo(1_800_000 / 180);
      expect(loadProviderPacing(sql, NOW).get('zai')).toMatchObject({ preempt: true });
    });
  });
});
