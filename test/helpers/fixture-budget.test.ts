import { expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { advanceFixtureBudgetDay,fixtureSql } from './fixture-budget';

it('isolates synthetic setup while retaining the real20k breaker and guarded measured reads',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('fixture-budget-proof-'+crypto.randomUUID()));
 await (runInDurableObject as any)(stub,async(hub:any,state:any)=>{
  const originalDay=hub.rowBudget.status().day;
  await advanceFixtureBudgetDay(hub);
  expect(hub.rowBudget.status().day).toBeGreaterThan(originalDay);
  expect(hub.rowBudget.status().limit_total).toBe(20000);
  const available=hub.rowBudget.status().available_total,stamp=hub.writes.stamp('fixture_history');
  const seed=fixtureSql(hub,state);
  seed.exec('CREATE TABLE fixture_history(id INTEGER PRIMARY KEY)');
  seed.exec('WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000) INSERT INTO fixture_history SELECT i FROM n');
  expect(hub.rowBudget.status().available_total).toBe(available);
  expect(hub.writes.stamp('fixture_history')).not.toBe(stamp);
  expect(hub.sql.exec('SELECT id FROM fixture_history ORDER BY id LIMIT 1').one().id).toBe(1);
  expect(hub.rowBudget.status().available_total).toBeLessThan(available);
  expect(hub.rowBudget.status().limit_total).toBe(20000);
  // A synthetic native counter over the daily ceiling must still close the
  // REAL guard; the helper neither replaces it nor intercepts its assertions.
  expect(()=>hub.rowBudget.wrap({exec(){return{rowsRead:20001,rowsWritten:0};}}).exec('SELECT bounded_fixture_counter')).toThrow('do_daily_row_budget_exceeded');
  await hub.rowBudget.finish();
  expect(()=>hub.sql.exec('SELECT id FROM fixture_history LIMIT 1')).toThrow('do_daily_row_budget_exceeded');
 });
});
