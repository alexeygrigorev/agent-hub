import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';

it('initializes an empty exported Hub within the strict combined daily budget', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('bootstrap-cost-' + crypto.randomUUID()));
  await (runInDurableObject as any)(stub, async (hub: any) => {
    console.log('bootstrap_budget', JSON.stringify(hub.rowBudget.status()));
    console.log('bootstrap_statements', JSON.stringify(hub.meter.statementTop(15).map((row: any) => ({
      scope: row.scope, sql: row.sql.slice(0, 200), calls: row.calls, rows_read: row.rows_read, rows_written: row.rows_written,
    }))));
    expect(hub.rowBudget.status().stopped).toBe(false);
    expect(hub.conversationsService).toBeDefined();
    expect(hub.jobsService).toBeDefined();
  });
});
