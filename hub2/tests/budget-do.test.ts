import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { BudgetDO, costForUnits, METRICS, type Coverage, type Metric, type Units } from '../src/budget-do';

function state() {
  const database = new DatabaseSync(':memory:');
  const ctx: any = {
    storage: {
      sql: { exec(query: string, ...args: unknown[]) {
        const statement = database.prepare(query);
        if (statement.columns().length) {
          const rows = statement.all(...args as any[]);
          return { rowsRead: rows.length, rowsWritten: 0, toArray: () => rows };
        }
        const result = statement.run(...args as any[]);
        return { rowsRead: 0, rowsWritten: Number(result.changes), toArray: () => [] };
      } },
      transactionSync<T>(fn: () => T): T {
        database.exec('BEGIN');
        try { const result = fn(); database.exec('COMMIT'); return result; }
        catch (e) { database.exec('ROLLBACK'); throw e; }
      },
    },
    blockConcurrencyWhile<T>(fn: () => T | Promise<T>): Promise<T> { return Promise.resolve(fn()); },
  };
  return { ctx, database };
}

const admin = { id: 'operator:budget-test', kind: 'operator', scopes: ['operator:admin', 'budget:read', 'budget:override'] };
const request = (path: string, method = 'GET', body?: unknown, actor?: unknown) => new Request(`https://budget.test${path}`, {
  method,
  headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(actor === undefined ? {} : { 'x-hub2-actor': JSON.stringify(actor) }) },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const units = (values: Partial<Record<Metric, number>> = {}): Units => Object.fromEntries(METRICS.map(k => [k, values[k] ?? 0])) as Units;
const coverage = (upperBound: Metric[] = []): Coverage => Object.fromEntries(METRICS.map(k => [k, upperBound.includes(k) ? 'upper_bound' : 'exact'])) as Coverage;
async function body(response: Response): Promise<any> { return await response.json(); }
async function reserve(budget: BudgetDO, id: string, values: Units, kind = 'test_work', emergency = false) {
  return budget.fetch(request('/_internal/reserve', 'POST', { id, kind, estimated_units: values, coverage: coverage(), ...(emergency ? { emergency: true } : {}) }));
}
async function settle(budget: BudgetDO, id: string, values: Units) {
  return budget.fetch(request('/_internal/settle', 'POST', { id, actual_units: values, coverage: coverage() }));
}
async function status(budget: BudgetDO) { return body(await budget.fetch(request('/api/budget', 'GET', undefined, admin))); }

test('paid rates use marginal units and include every configured cost dimension', () => {
  const estimate = costForUnits(units({
    worker_requests: 1_000_000, worker_cpu_ms: 1_000_000, do_requests: 1_000_000,
    do_ws_messages: 20_000_000, sqlite_rows_read: 1_000_000, sqlite_rows_written: 1_000_000,
    sqlite_byte_days: 1_073_741_824 * 30, kv_reads: 1_000_000, kv_writes: 1_000_000,
    kv_deletes: 1_000_000, kv_lists: 1_000_000, kv_byte_days: 1_073_741_824 * 30,
  }));
  assert.equal(estimate.costs_pusd.worker, '320000000000');
  assert.equal(estimate.costs_pusd.durable_objects, '300000000000');
  assert.equal(estimate.costs_pusd.sqlite, '1201000000000');
  assert.equal(estimate.costs_pusd.workers_kv, '16000000000000');
  assert.equal(units().background_invocations, 0);
});

test('unknown dimensions fail before a reservation is created', async () => {
  const s = state(), budget = new BudgetDO(s.ctx);
  const refused = await budget.fetch(request('/_internal/reserve', 'POST', {
    id: 'unknown-1', kind: 'fanout', estimated_units: units(), coverage: { worker_requests: 'exact' },
  }));
  assert.equal(refused.status, 503);
  assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_reservations').get() as any).n, 0);
  const health = await status(budget);
  assert.equal(health.health, 'degraded');
  assert.equal(health.provider.state, 'unknown');
  assert.equal(health.health_events.length, METRICS.length - 1);
  assert.equal((await reserve(budget, 'blocked-after-unknown', units({ worker_requests: 1 }))).status, 503);
  assert.equal((await body(await reserve(budget, 'safety-after-unknown', units({ worker_requests: 1 }), 'native_exit', true))).allowed, true);
  await settle(budget, 'safety-after-unknown', units({ worker_requests: 1 }));
  const afterRecovery = await status(budget);
  assert.equal(afterRecovery.health, 'degraded');
  assert.equal(afterRecovery.coverage.worker_requests, 'unknown');
  assert.equal(afterRecovery.health_events.length, METRICS.length - 1);
});

test('settlement replay includes coverage classification, not only numeric units', async () => {
  const s = state(), budget = new BudgetDO(s.ctx), measured = units({ worker_requests: 1 });
  await reserve(budget, 'coverage-replay', measured);
  const upper = coverage(['worker_requests']);
  assert.equal((await budget.fetch(request('/_internal/settle', 'POST', { id: 'coverage-replay', actual_units: measured, coverage: upper }))).status, 200);
  assert.equal((await budget.fetch(request('/_internal/settle', 'POST', { id: 'coverage-replay', actual_units: measured, coverage: coverage() }))).status, 409);
  assert.equal((await body(await budget.fetch(request('/_internal/settle', 'POST', { id: 'coverage-replay', actual_units: measured, coverage: upper })))).replay, true);
});

test('strictly greater than three cents alerts once and survives object restart', async () => {
  const s = state(); let now = Date.parse('2026-11-04T00:10:00.000Z');
  let budget = new BudgetDO(s.ctx, {}, () => now);
  const exact = units({ worker_requests: 100_000 });
  assert.equal((await body(await reserve(budget, 'exact', exact))).allowed, true);
  await settle(budget, 'exact', exact);
  assert.equal((await status(budget)).alerts.filter((a: any) => a.kind === 'threshold').length, 0);
  const extra = units({ worker_requests: 1 });
  assert.equal((await body(await reserve(budget, 'crossing', extra))).allowed, true);
  const first = await settle(budget, 'crossing', extra);
  assert.equal((await body(first)).replay, false);
  assert.equal((await body(await settle(budget, 'crossing', extra))).replay, true);
  assert.equal((await status(budget)).alerts.filter((a: any) => a.kind === 'threshold').length, 1);

  budget = new BudgetDO(s.ctx, {}, () => now);
  const restarted = await status(budget);
  assert.equal(restarted.day, '2026-11-04');
  assert.equal(restarted.alerts.filter((a: any) => a.kind === 'threshold').length, 1);
  assert.equal(restarted.settled_usd, '0.030000300000');
});

test('UTC rollover isolates daily ceilings without deleting the previous ledger', async () => {
  const s = state(); let now = Date.parse('2026-11-04T23:59:00.000Z');
  const budget = new BudgetDO(s.ctx, {}, () => now), one = units({ worker_requests: 1 });
  await reserve(budget, 'before-midnight', one); await settle(budget, 'before-midnight', one);
  now = Date.parse('2026-11-05T00:00:01.000Z');
  const next = await status(budget);
  assert.equal(next.day, '2026-11-05');
  assert.equal(next.settled_usd, '0.000000000000');
  assert.equal(next.health, 'degraded');
  assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_days').get() as any).n, 1);
});

test('daily ceiling denies before fanout, returns one retry boundary, and leaves recovery reserve', async () => {
  const s = state(), now = Date.parse('2026-11-04T12:00:00.000Z');
  const budget = new BudgetDO(s.ctx, {}, () => now);
  const nearCeiling = units({ worker_requests: 300_000 });
  assert.equal((await body(await reserve(budget, 'bulk-1', nearCeiling))).allowed, true);
  const denied = await budget.fetch(request('/_internal/reserve', 'POST', { id: 'bulk-2', kind: 'fanout', estimated_units: units({ worker_requests: 1 }), coverage: coverage() }));
  assert.equal(denied.status, 429);
  const refusal = await body(denied);
  assert.equal(refusal.reason, 'daily_ceiling');
  assert.equal(refusal.retry_at, Date.parse('2026-11-05T00:00:00.000Z'));
  const retry = await budget.fetch(request('/_internal/reserve', 'POST', { id: 'bulk-2', kind: 'fanout', estimated_units: units({ worker_requests: 1 }), coverage: coverage() }));
  assert.equal((await body(retry)).retry_at, refusal.retry_at);
  assert.equal((await body(await reserve(budget, 'exit-1', units({ worker_requests: 30_000 }), 'native_exit', true))).allowed, true);
  assert.equal((await reserve(budget, 'fake-emergency', units({ worker_requests: 1 }), 'fanout', true)).status, 400);
  await settle(budget, 'bulk-1', nearCeiling);
  await settle(budget, 'exit-1', units({ worker_requests: 30_000 }));
  const report = await status(budget);
  assert.equal(report.settled_usd, '0.099000000000');
  assert.equal(report.hard_stopped, false);
});

test('proved pre-effect cancellation releases only that hold; uncertain work has no timeout release', async () => {
  const s = state(), budget = new BudgetDO(s.ctx), estimate = units({ do_requests: 10 });
  await reserve(budget, 'cancel-me', estimate);
  assert.equal((await budget.fetch(request('/_internal/cancel', 'POST', { id: 'cancel-me', effect_started: true, reason: 'uncertain' }))).status, 409);
  assert.equal((await budget.fetch(request('/_internal/cancel', 'POST', {
    id: 'cancel-me', effect_started: false, reason: 'preflight refused', evidence: { source: 'pre_effect_refusal', id: 'cancel-me', checked_at: Date.now() },
  }))).status, 200);
  assert.equal((await status(budget)).reserved_usd, '0.000000000000');
  assert.equal((await budget.fetch(request('/_internal/reserve', 'POST', { id: 'cancel-me', kind: 'test_work', estimated_units: estimate, coverage: coverage() }))).status, 409);
});

test('forecast alert is separate, durable, deduped, and its claim retries with a stable ID', async () => {
  const s = state(); let now = Date.parse('2026-11-04T12:00:00.000Z');
  let budget = new BudgetDO(s.ctx, {}, () => now);
  const spend = units({ worker_requests: 50_001 });
  await reserve(budget, 'forecast-op', spend); await settle(budget, 'forecast-op', spend);
  assert.equal((await status(budget)).alerts.filter((a: any) => a.kind === 'forecast').length, 1);
  const id = 'hub:2026-11-04:forecast';
  const claim = await body(await budget.fetch(request(`/_internal/alerts/${id}/claim`, 'POST', { worker_id: 'existing-alert-worker' })));
  assert.equal(claim.alert.id, id);
  assert.equal((await budget.fetch(request(`/_internal/alerts/${id}/ack`, 'POST', { claim_token: 'wrong' }))).status, 409);
  const fail = await body(await budget.fetch(request(`/_internal/alerts/${id}/fail`, 'POST', { claim_token: claim.claim_token })));
  now = fail.retry_at;
  budget = new BudgetDO(s.ctx, {}, () => now);
  const retry = await body(await budget.fetch(request(`/_internal/alerts/${id}/claim`, 'POST', { worker_id: 'existing-alert-worker' })));
  assert.equal(retry.alert.id, id);
  assert.equal(retry.alert.attempts, 2);
  assert.equal((await budget.fetch(request(`/_internal/alerts/${id}/ack`, 'POST', { claim_token: retry.claim_token }))).status, 200);
  assert.equal((await body(await budget.fetch(request(`/_internal/alerts/${id}/ack`, 'POST', { claim_token: 'lost-ack-replay' })))).replay, true);
});

test('a settlement over its reserved estimate opens the circuit but permits the safety reserve', async () => {
  const s = state(), budget = new BudgetDO(s.ctx), reserved = units({ worker_requests: 100_000 });
  await reserve(budget, 'underestimate', reserved);
  const actual = units({ worker_requests: 100_001 });
  const settled = await body(await settle(budget, 'underestimate', actual));
  assert.equal(settled.overrun, true);
  assert.equal((await status(budget)).hard_stopped, true);
  assert.equal((await reserve(budget, 'denied-after-overrun', units({ worker_requests: 1 }))).status, 429);
  assert.equal((await body(await reserve(budget, 'safety-after-overrun', units({ worker_requests: 10 }), 'native_exit', true))).allowed, true);
});

test('operator override is day-scoped and provider reconciliation remains explicit', async () => {
  const s = state(), now = Date.parse('2026-11-04T12:00:00.000Z');
  const budget = new BudgetDO(s.ctx, {}, () => now);
  assert.equal((await budget.fetch(request('/api/budget', 'GET', undefined, { id: 'agent:x', kind: 'agent', scopes: [] }))).status, 403);
  const override = await body(await budget.fetch(request('/api/budget/override', 'POST', { day: '2026-11-04', ceiling_pusd: '200000000000', reason: 'bounded rollout observation' }, admin)));
  assert.equal(override.daily_ceiling_usd, '0.200000000000');
  const measured = units({ worker_requests: 1, do_gb_ms: 10 });
  await budget.fetch(request('/_internal/reserve', 'POST', { id: 'metric-ready', kind: 'test', estimated_units: measured, coverage: coverage(['do_gb_ms']) }));
  await budget.fetch(request('/_internal/settle', 'POST', { id: 'metric-ready', actual_units: measured, coverage: coverage(['do_gb_ms']) }));
  const rec = await budget.fetch(request('/_internal/reconcile', 'POST', {
    day: '2026-11-04', source: 'cloudflare_account_usage_v2', observed_at: now, reported_through: now - 30_000,
    provider_total_pusd: '1000000000', hub_attributed_pusd: '500000000', unattributed_pusd: '500000000',
    coverage: { worker: true, durable_objects: true, sqlite: true, workers_kv: true },
  }));
  assert.equal((await body(rec)).complete, true);
  const report = await status(budget);
  assert.equal(report.health, 'healthy_estimated');
  assert.equal(report.rate_basis, 'marginal_paid_estimate_from_exact_or_upper_bound_units');
  assert.equal(report.coverage.do_gb_ms, 'upper_bound');
  assert.equal(report.provider.lag_seconds, 30);
  assert.equal(report.provider.unattributed_usd, '0.000500000000');
  assert.equal((await budget.fetch(request('/_internal/reconcile', 'POST', {
    day: '2026-11-04', source: 'older', observed_at: now - 60_000, reported_through: now - 90_000,
    provider_total_pusd: '1000000000', hub_attributed_pusd: '500000000', unattributed_pusd: '500000000',
    coverage: { worker: true, durable_objects: true, sqlite: true, workers_kv: true },
  }))).status, 409);
});

test('denied unique request flood creates no per-request ledger rows or day rewrites',async()=>{
 const s=state(),budget=new BudgetDO(s.ctx,{},()=>Date.parse('2026-11-04T12:00:00Z'));
 await reserve(budget,'fill-flood-fixture',units({worker_requests:300000}));
 const before=(s.database.prepare('SELECT COUNT(*) AS n FROM budget_reservations').get() as any).n;
 const beforeWrites=(s.database.prepare('SELECT total_changes() AS n').get() as any).n;
 for(let i=0;i<100;i++)assert.equal((await reserve(budget,'denied-'+i,units({worker_requests:1}))).status,429);
 assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_reservations').get() as any).n,before);
 assert.equal((s.database.prepare('SELECT total_changes() AS n').get() as any).n,beforeWrites);
});

test('explicit audited operator recovery closes an overrun breaker but cannot clear unknown metric health',async()=>{
 const s=state(),now=Date.parse('2026-11-04T12:00:00Z'),budget=new BudgetDO(s.ctx,{},()=>now);
 await reserve(budget,'overrun-recovery',units({worker_requests:1}));
 assert.equal((await budget.fetch(request('/_internal/settle','POST',{id:'overrun-recovery',actual_units:units({worker_requests:100}),coverage:coverage()}))).status,200);
 const override={day:'2026-11-04',ceiling_pusd:'100000000000',reason:'Corrected envelope source; explicitly resume controlled work'};
 assert.equal((await budget.fetch(request('/api/budget/override','POST',override,admin))).status,200);
 assert.equal((await reserve(budget,'still-stopped',units({worker_requests:1}))).status,429);
 assert.equal((await budget.fetch(request('/api/budget/override','POST',{...override,clear_hard_stop:true},admin))).status,200);
 assert.equal((await reserve(budget,'resumed',units({worker_requests:1}))).status,200);
 assert((s.database.prepare('SELECT reason FROM budget_override_audit ORDER BY id DESC LIMIT 1').get() as any).reason.includes('explicit breaker recovery'));
 await budget.fetch(request('/_internal/reserve','POST',{id:'unknown-recovery',kind:'test',estimated_units:{},coverage:{}}));
 assert.equal((await budget.fetch(request('/api/budget/override','POST',{...override,clear_hard_stop:true},admin))).status,503);
});

test('recovery ceiling and priced reservation are atomic, authorized and replay bound',async()=>{
 const s=state(),budget=new BudgetDO(s.ctx,{},()=>Date.parse('2026-11-04T12:00:00Z'));
 const proposal={day:'2026-11-04',ceiling_pusd:'200000000000',reason:'Bounded atomic operator recovery'};
 const attempt={id:'atomic-recovery',kind:'budget_recovery',emergency:true,override:proposal,estimated_units:units({sqlite_rows_written:300000}),coverage:coverage()};
 assert.equal((await budget.fetch(request('/_internal/reserve','POST',attempt))).status,403);
 assert.equal((await budget.fetch(request('/_internal/reserve','POST',attempt,admin))).status,409);
 assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_override_audit').get() as any).n,0,'unfundable action rolls back audit and override');
 assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_overrides').get() as any).n,0);
 const valid={...attempt,estimated_units:units({worker_requests:1})};
 assert.equal((await budget.fetch(request('/_internal/reserve','POST',valid,admin))).status,200);
 assert.equal((await budget.fetch(request('/_internal/reserve','POST',valid,admin))).status,200);
 assert.equal((s.database.prepare('SELECT COUNT(*) AS n FROM budget_override_audit').get() as any).n,1);
 assert.equal((await budget.fetch(request('/_internal/reserve','POST',{...valid,override:{...proposal,reason:'Changed replay'}},admin))).status,409);
});

test('breaker recovery rejects missing and invalid stored metric coverage',async()=>{
 for(const damaged of [{},{worker_requests:'not-a-coverage'}]){
  const s=state(),budget=new BudgetDO(s.ctx,{},()=>Date.parse('2026-11-04T12:00:00Z'));
  await reserve(budget,'damaged-coverage',units({worker_requests:1}));
  await budget.fetch(request('/_internal/settle','POST',{id:'damaged-coverage',actual_units:units({worker_requests:2}),coverage:coverage()}));
  // Deliberately corrupt disposable synthetic metadata; production has no raw-SQL endpoint.
  (budget as any).db.run('UPDATE budget_days SET coverage_json=?',JSON.stringify(damaged));
  const result=await budget.fetch(request('/api/budget/override','POST',{day:'2026-11-04',ceiling_pusd:'200000000000',reason:'Must refuse incomplete metadata',clear_hard_stop:true},admin));
  assert.equal(result.status,503);
  assert.equal((await status(budget)).hard_stopped,true);
 }
});
