import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { ensureTbmControllerSchema, recordSessionOutcomeReward } from '../src/tbm-controller';
import { captureTbmMetrics, readTbmMetrics, recordTbmAdmission } from '../src/tbm-metrics';

const now = Date.UTC(2026, 9, 27, 12);
const inDO: any = runInDurableObject;
const stub = (name: string) => (env as any).HUB.get((env as any).HUB.idFromName(name));
function seed(sql: any, id = 'zai:a') {
  ensureTbmControllerSchema(sql);
  sql.exec(`INSERT INTO tbm_quota_pools (pool_id,provider,account,total_capacity_tokens,remaining_tokens,headroom_percentage,reset_at_ms,last_updated_ms,current_burn_rate_tpm,target_burn_rate_tpm) VALUES (?, 'zai', 'a',1000000,800000,80,?,?,10,20)`, id, now + 3600000, now);
  return sql.exec('SELECT * FROM tbm_quota_pools WHERE pool_id=?', id).toArray()[0];
}
describe('TBM catalog real observations', () => {
  it('exposes every metric in both windows without fabricated zero values', async () => {
    await inDO(stub('catalog-empty'), (hub: any) => {
      seed(hub.sql);
      const data = readTbmMetrics(hub.sql, now);
      expect(Object.keys(data.pools[0].windows)).toEqual(['24h', '7d']);
      const m = data.pools[0].windows['24h'].metrics;
      expect(Object.keys(m)).toHaveLength(13); // paired admission/no-route metric
      for (const v of Object.values(m)) { expect(v.value).toBeNull(); expect(v.reason).toBeTruthy(); }
    });
  });
  it('isolates pools/windows, measures native efficiency and observed deficit, rejects stale or gapped intervals', async () => {
    await inDO(stub('catalog-real'), (hub: any) => {
      const p = seed(hub.sql); seed(hub.sql, 'zai:b');
      recordSessionOutcomeReward(hub.sql, { session_id: 'useful', pool_id: p.pool_id, provider: 'zai', intake_task_id: 'task', outcome: 'done', tokens_burned: 100000, value_weight: 5 }, now - 2 * 86400000);
      captureTbmMetrics(hub.sql, [p], now);
      captureTbmMetrics(hub.sql, [p], now + 30000); // not a new minute
      captureTbmMetrics(hub.sql, [{ ...p, remaining_tokens: 799990, last_updated_ms: now + 60000 }], now + 60000);
      let data = readTbmMetrics(hub.sql, now + 60000);
      const w = data.pools[0].windows;
      expect(w['24h'].metrics.useful_work_per_tokens.value).toBeNull();
      expect(w['7d'].metrics.useful_work_per_tokens.value).toBe(50);
      expect(w['24h'].metrics.burn_rate_vs_pacing_target.value).toBe(.5);
      expect(w['24h'].metrics.idle_wasted_quota_over_time.value).toBe(10);
      expect(w['24h'].coverage.pacing_samples).toBe(1);
      expect(data.pools[1].windows['24h'].metrics.burn_rate_vs_pacing_target.value).toBeNull();
      captureTbmMetrics(hub.sql, [p], now + 3600000);
      data = readTbmMetrics(hub.sql, now + 3600000);
      expect(data.pools[0].windows['24h'].coverage.observed_minutes).toBe(1);
    });
  });
  it('counts real idempotent admission decisions, preserves unassigned requests globally, excludes expiration from bank burn', async () => {
    await inDO(stub('catalog-admissions-bank'), (hub: any) => {
      const p = seed(hub.sql);
      recordTbmAdmission(hub.sql, { request_id: '1', pool_id: p.pool_id, outcome: 'accepted' }, now);
      recordTbmAdmission(hub.sql, { request_id: '1', pool_id: p.pool_id, outcome: 'accepted' }, now);
      recordTbmAdmission(hub.sql, { request_id: '2', pool_id: null, outcome: 'no_eligible_route' }, now);
      recordTbmAdmission(hub.sql, { request_id: 'old', pool_id: p.pool_id, outcome: 'rejected' }, now - 2 * 86400000);
      hub.sql.exec(`INSERT INTO tbm_banked_tranches (tranche_id,pool_id,provider,allocated_tokens,remaining_tokens,granted_at_ms,expires_at_ms,updated_at_ms) VALUES ('bank',?,'zai',1000,1000,?,?,?)`,p.pool_id,now-60000,now+3600000,now);
      captureTbmMetrics(hub.sql, [p], now);
      hub.sql.exec("UPDATE tbm_banked_tranches SET remaining_tokens=800,expired_unused_tokens=100,updated_at_ms=?",now+60000);
      captureTbmMetrics(hub.sql, [p], now+60000);
      const data = readTbmMetrics(hub.sql, now+60000);
      expect(data.global.windows['24h'].metrics.admission_rejects_rate.value).toBe(.5);
      expect(data.global.windows['24h'].unattributed_decisions).toBe(1);
      const w = data.pools[0].windows;
      expect(w['24h'].metrics.admission_rejects_rate.value).toBeNull();
      expect(w['24h'].metrics.admission_rejects_rate.reason).toContain('incomplete_pool_attribution');
      expect(w['7d'].metrics.admission_rejects_rate.value).toBeNull();
      expect(w['24h'].metrics.banked_burn_rate.value).toBeNull();
      expect(w['24h'].metrics.banked_burn_rate.observed_event_throughput.value).toBeCloseTo(100/1440);
      hub.sql.exec('UPDATE tbm_banked_tranches SET updated_at_ms=?',now+120000);
      captureTbmMetrics(hub.sql,[p],now+120000);
      expect(hub.sql.exec('SELECT tokens FROM tbm_banked_consumption').toArray()).toEqual([{tokens:100}]);
      expect(w['24h'].metrics.banked_remaining.value).toBe(800);
      expect(w['24h'].metrics.time_to_expiry.value).toBe(3540);
    });
  });
  it('uses verified outcomes and measured engine tokens; percentage reset quantities retain percent units', async () => {
    await inDO(stub('catalog-verified'), (hub: any) => {
      const p = seed(hub.sql);
      hub.sql.exec(`INSERT INTO session_control_sessions (id,actor,request_key,request_json,engine,mode,state,created_at,updated_at) VALUES ('verified','test','1','{}','zai','headless','stopped',?,?)`,new Date(now-120000).toISOString(),new Date(now).toISOString());
      recordSessionOutcomeReward(hub.sql, { session_id: 'verified', pool_id: p.pool_id, provider: 'zai', outcome: 'done', tokens_burned: 1000 }, now);
      recordSessionOutcomeReward(hub.sql, { session_id: 'failed', pool_id: p.pool_id, provider: 'zai', outcome: 'failed', tokens_burned: 1000 }, now);
      const m = readTbmMetrics(hub.sql,now).pools[0].windows['24h'].metrics;
      expect(m.time_to_useful_outcome.value).toBe(120);
      expect(m.engine_mix.value).toEqual({zai:1});
      hub.sql.exec("UPDATE tbm_quota_pools SET capacity_known=0,quota_unit='percent',remaining_percent=80,total_capacity_tokens=0,remaining_tokens=0");
      hub.sql.exec(`INSERT INTO tbm_pool_resets (pool_id,reset_at_ms,provider,capacity_known,remaining_percent,captured_at_ms) VALUES (?,?,'zai',0,25,?)`,p.pool_id,now-1000,now);
      const reset = readTbmMetrics(hub.sql,now).pools[0].windows['24h'].metrics.leftover_quota_at_planned_reset;
      expect(reset.value).toBe(20);
      expect(reset.unit).toBe('percentage_points');
    });
  });
  it('captures real start decisions without changing start behavior or counting retries/interactive starts', async () => {
    await inDO(stub('catalog-start-hook'), async (hub: any, state: any) => {
      const service = new SessionControlService(hub.sql, (cb: any) => state.storage.transactionSync(cb), () => now,
        { reserve: () => ({ status: 201, value: { id: 'fixture-launch', state: 'offered', machine: 'fixture', engine: 'zai' } }) });
      service.init(); seedFixtureIntake(hub.sql);
      const auth: any = { caller: { isAdmin: true, actor: 'test', kind: 'human' }, scopes: ['admin'] };
      const start = (key: string, mode = 'headless') => service.handle(new Request('https://test/api/sessions', { method: 'POST', body: JSON.stringify(withFixtureIntake('/api/sessions', { key, workspace: 'demo', mode }, 'POST')) }), auth);
      expect((await start('first')).status).toBe(201);
      expect((await start('first')).status).toBe(200);
      expect((await start('human', 'interactive')).status).toBe(201);
      expect(hub.sql.exec('SELECT outcome FROM tbm_admission_events').toArray()).toEqual([{ outcome: 'accepted' }]);
    });
  });
  it('does not count the floor boundary or repeat a probe as additional breaches', async () => {
    await inDO(stub('catalog-floor'), (hub: any) => {
      const p = seed(hub.sql);
      const boundary = { ...p, remaining_tokens: 50000 };
      captureTbmMetrics(hub.sql, [boundary], now);
      captureTbmMetrics(hub.sql, [boundary], now+60000);
      expect(readTbmMetrics(hub.sql,now+60000).pools[0].windows['24h'].metrics.reserve_floor_breaches.value).toEqual({soft:1,hard:0});
      captureTbmMetrics(hub.sql, [{...p, remaining_tokens:49999,last_updated_ms:now+120000}],now+120000);
      captureTbmMetrics(hub.sql, [{...p, remaining_tokens:49998,last_updated_ms:now+180000}],now+180000);
      expect(readTbmMetrics(hub.sql,now+180000).pools[0].windows['24h'].metrics.reserve_floor_breaches.value).toEqual({soft:1,hard:1});
    });
  });
});
