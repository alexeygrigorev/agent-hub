/** Test fixtures are separate synthetic days, never a larger production limit.
 * Use explicitly at case boundaries for shared named Hub instances. Business
 * clocks, SQL wrappers, durable ledger persistence and breaker behavior remain
 * real; the cost-safety regression suites deliberately do not use this helper. */
export async function freshFixtureBudgetDay(hub:any):Promise<void> {
  hub.cancelBudgetRefund?.();
  const budget=hub.rowBudget;
  if(!budget)throw new Error('fixture requires an initialized Hub RowBudget');
  const day=Math.max(Number(budget.status().day),Math.floor(Date.now()/86400000))+1;
  budget.now=()=>day*86400000+1;
  await budget.begin();
  if(budget.status().limit_total!==20000 || budget.status().stopped)throw new Error('fixture budget did not preserve the strict daily limit');
}

/** Synthetic retained-history setup only. Keep measured operations on hub.sql
 * or the real service/HTTP path. Sharing WriteWatch preserves all trigger and
 * cache invalidation effects of raw fixture writes without charging thousands
 * of deliberately fabricated rows to the operation being measured. */
export function fixtureSql(hub:any,state:any=hub.ctx):any {
  return hub.writes.wrap(state.storage.sql);
}

// Same explicit case-boundary operation; alias retained for fixture callers.
export const advanceFixtureBudgetDay=freshFixtureBudgetDay;

/** Component restart fixture: the native binding already supplies the real
 * exported subclass. Rebuild its services after prepayment instead of creating
 * a second live DurableObject on the same state and stacking storage guards.
 * Each independent restart scenario uses its own strict synthetic budget day. */
export async function restartFixtureHub<T>(hub:T, envOverride?:unknown):Promise<T> {
  const instance=hub as any;
  await advanceFixtureBudgetDay(instance);
  await instance.rowBudget.releaseUnused();
  await instance.rowBudget.begin(true);
  instance.observedCatalog?.reset();
  instance.sql=instance.meter.wrap(instance.writes.wrap(instance.rowBudget.wrap(instance.ctx.storage.sql)));
  instance.observedCatalog=new instance.observedCatalog.constructor(instance.sql,instance.writes);
  if(envOverride!==undefined)instance.env=envOverride;
  instance.initializeServices(instance.env);
  instance.initializationComplete=true;
  return hub;
}
