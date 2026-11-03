import { describe, expect, it } from 'vitest';
import { getTbmMetrics } from '../src/tbm-metrics';

const pool = { pool_id: 'go:test:5h', provider: 'go', account: 'test' };

function fakeSql(options: { tranches?: any[]; fail?: 'engine' | 'banked' } = {}) {
  return {
    exec(query: string) {
      if (query.includes('FROM tbm_quota_pools')) return { toArray: () => [pool] };
      if (query.includes('FROM tbm_banked_tranches')) {
        if (options.fail === 'banked') throw new Error('unavailable');
        return { toArray: () => options.tranches ?? [] };
      }
      if (query.includes('FROM tbm_reward_events r JOIN tbm_session_tokens')) {
        if (options.fail === 'engine') throw new Error('unavailable');
        return { toArray: () => [] };
      }
      return { toArray: () => [] };
    },
  } as any;
}

function firstPoolMetrics(sql: any) {
  const result: any = getTbmMetrics(sql, 1_800_000_000_000);
  return result.windows['24h'].pools[0].metrics;
}

describe('TBM metrics units and unknown values', () => {
  it('uses fraction for an engine mix with no classifiable data', () => {
    expect(firstPoolMetrics(fakeSql()).engine_mix).toMatchObject({
      value: null,
      unit: 'fraction',
    });
  });

  it('keeps banked burn rate unknown if any active tranche has no measured rate', () => {
    const metrics = firstPoolMetrics(fakeSql({ tranches: [
      { status: 'active', expires_at_ms: 1_800_000_001_000, quantity_known: 1, remaining_tokens: 10, banked_burn_rate_tpm: 50 },
      { status: 'active', expires_at_ms: 1_800_000_001_000, quantity_known: 1, remaining_tokens: 10, banked_burn_rate_tpm: 0 },
    ] }));
    expect(metrics.banked_burn_rate).toMatchObject({
      value: null,
      unit: 'tokens/minute',
    });
  });

  it('preserves established units when engine or banked storage queries fail', () => {
    expect(firstPoolMetrics(fakeSql({ fail: 'engine' })).engine_mix.unit).toBe('fraction');
    expect(firstPoolMetrics(fakeSql({ fail: 'banked' })).banked_burn_rate.unit).toBe('tokens/minute');
  });
});
