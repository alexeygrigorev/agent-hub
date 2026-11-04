import {HostGoals} from '../src/host-goals';
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
    // These migration columns belong in modern CREATE definitions. SQLite
    // ADD COLUMN re-reads the full catalog even when its source table is empty.
    const unnecessary=hub.meter.statementTop(4096).filter((row:any)=>
      /^ALTER TABLE \S+ ADD COLUMN\b/i.test(row.sql));
    expect(unnecessary).toEqual([]);
  });
});

it('reinitializes host goal trigger wiring without scanning the schema for trigger existence',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('host-goal-init-'+crypto.randomUUID()));
 await (runInDurableObject as any)(stub,(hub:any,state:any)=>{
  const queries:string[]=[];
  const sql=new Proxy(hub.sql,{get(target,key){if(key==='exec')return(q:string,...args:any[])=>{queries.push(q);return target.exec(q,...args);};const v=target[key];return typeof v==='function'?v.bind(target):v;}});
  const goals=new HostGoals(sql,hub.fleetService);
  hub.meter.runSync('host-goal-repeat',()=>{goals.init();goals.init();});
  expect(queries.some(q=>/sqlite_master.*type\s*=\s*'trigger'/i.test(q))).toBe(false);
  expect(hub.meter.snapshot()['host-goal-repeat'].rows_read).toBeLessThan(20);
  const names=state.storage.sql.exec("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'host_goal_%'").toArray().map((r:any)=>r.name);
  expect(names.sort()).toEqual(['host_goal_launch_running_v2','host_goal_running_insert_v2','host_goal_running_update_v2']);
  expect(hub.rowBudget.status().stopped).toBe(false);
 });
});
