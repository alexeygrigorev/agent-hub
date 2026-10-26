import { describe, expect, it } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { defaultEnv } from './m1-focus-setup';
import { ensureTbmQuotaSchema } from '../src/quota';
import type { Hub } from '../src/hub';

const stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));

const EXPECTED_TABLES = [
  'tbm_quota_pools',
  'tbm_banked_tranches',
  'tbm_reward_events',
  'tbm_metrics_rollup',
];

const EXPECTED_COLUMNS: Record<string, string[]> = {
  tbm_quota_pools: [
    'pool_id', 'provider', 'account', 'total_capacity_tokens', 'remaining_tokens',
    'headroom_percentage', 'reset_at_ms', 'cycle_ms', 'window_type',
    'remaining_percent', 'capacity_known', 'hard_floor_percent', 'soft_reserve_percent',
    'convexity_exponent', 'current_burn_rate_tpm', 'target_burn_rate_tpm', 'pacing_ratio',
    'kill_switch_active', 'kill_switch_reason', 'kill_switch_updated_at', 'last_updated_ms',
  ],
  tbm_banked_tranches: [
    'tranche_id', 'pool_id', 'provider', 'label', 'allocated_tokens', 'remaining_tokens',
    'granted_at_ms', 'expires_at_ms', 'status', 'banked_burn_rate_tpm',
    'target_banked_burn_rate_tpm', 'expired_unused_tokens', 'updated_at_ms',
  ],
  tbm_reward_events: [
    'event_id', 'timestamp_ms', 'pool_id', 'intake_task_id', 'active_mode',
    'swarm_concurrency', 'useful_work_reward', 'pacing_error_penalty', 'floor_breach_penalty',
    'engine_mix_penalty', 'waste_at_reset_penalty', 'banked_waste_penalty', 'net_reward',
    'token_efficiency_eta',
  ],
  tbm_metrics_rollup: [
    'rollup_id', 'window_type', 'start_time_ms', 'end_time_ms', 'pool_id',
    'avg_burn_rate_vs_target', 'leftover_quota_at_reset', 'soft_floor_breach_count',
    'hard_floor_breach_count', 'total_tokens_burned', 'useful_work_units', 'token_efficiency_eta',
    'idle_wasted_quota', 'engine_mix_free_share', 'engine_mix_paid_share', 'engine_mix_grok_share',
    'admission_rejection_rate', 'no_eligible_route_rate', 'avg_time_to_outcome_seconds',
    'banked_tokens_burned', 'banked_tokens_expired_unused',
  ],
};

const EXPECTED_INDEXES = [
  'idx_pool_provider',
  'idx_banked_edf',
  'idx_reward_timestamp',
  'idx_reward_intake',
  'idx_rollup_query',
];

describe('TBM Phase 1 quota schema migration', () => {
  it('creates all four TBM tables with design columns and indexes on fresh DO init', async () => {
    await runInDurableObject(stub, (hub: Hub) => {
      const tables = new Set(
        hub.sql
          .exec("SELECT name FROM sqlite_master WHERE type = 'table'")
          .toArray()
          .map((r: any) => r.name)
      );
      for (const t of EXPECTED_TABLES) expect(tables.has(t)).toBe(true);

      for (const [table, cols] of Object.entries(EXPECTED_COLUMNS)) {
        const actual = hub.sql
          .exec(`PRAGMA table_info(${table})`)
          .toArray()
          .map((c: any) => c.name);
        expect(actual).toEqual(cols);
      }

      const indexes = new Set(
        hub.sql
          .exec("SELECT name FROM sqlite_master WHERE type = 'index'")
          .toArray()
          .map((r: any) => r.name)
      );
      for (const idx of EXPECTED_INDEXES) expect(indexes.has(idx)).toBe(true);
    });
  });

  it('migration is idempotent and preserves existing rows (upgrade-safe re-run)', async () => {
    await runInDurableObject(stub, (hub: Hub) => {
      ensureTbmQuotaSchema(hub.sql as any);
      hub.sql.exec(
        `INSERT OR REPLACE INTO tbm_quota_pools
         (pool_id, provider, account, total_capacity_tokens, remaining_tokens,
          headroom_percentage, reset_at_ms, last_updated_ms)
         VALUES ('tbm-test-pool', 'zai', 'default', 1000000, 800000, 80.0, 1761000000000, 1760900000000)`
      );
      // Simulate a second upgrade pass over an already-migrated DB.
      ensureTbmQuotaSchema(hub.sql as any);
      hub.fleetService.init();
      const row: any = hub.sql
        .exec('SELECT pool_id, remaining_tokens FROM tbm_quota_pools WHERE pool_id = ?', 'tbm-test-pool')
        .toArray()[0];
      expect(row).toMatchObject({ pool_id: 'tbm-test-pool', remaining_tokens: 800000 });
      hub.sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id = 'tbm-test-pool'");
    });
  });

  it('recreates TBM tables from scratch after drop (pre-migration upgrade path)', async () => {
    await runInDurableObject(stub, (hub: Hub) => {
      for (const t of EXPECTED_TABLES) hub.sql.exec(`DROP TABLE IF EXISTS ${t}`);
      const before = hub.sql
        .exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tbm_%'")
        .toArray();
      expect(before).toHaveLength(0);
      ensureTbmQuotaSchema(hub.sql as any);
      const after = new Set(
        hub.sql
          .exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tbm_%'")
          .toArray()
          .map((r: any) => r.name)
      );
      for (const t of EXPECTED_TABLES) expect(after.has(t)).toBe(true);
    });
  });
});
