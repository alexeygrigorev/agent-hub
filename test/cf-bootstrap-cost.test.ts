import {legacyMessageBindingTrigger} from './fixtures/messages-bind-agents-legacy';
import {fixtureSql,restartFixtureHub} from './helpers/fixture-budget';
import {RowsMeter} from '../src/rows-meter';
import {WriteWatch} from '../src/write-watch';
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
  // Production services share the configured SQL handle and its verified
  // locator records; a new unconfigured proxy would model a different owner.
  const goals=new HostGoals(hub.sql,hub.fleetService);
  hub.meter.runSync('host-goal-repeat',()=>{goals.init();goals.init();});
  const queries=hub.meter.statementTop(4096).filter((row:any)=>row.scope==='host-goal-repeat').map((row:any)=>row.sql);
  expect(queries.some((q:string)=>/sqlite_master.*type\s*=\s*'trigger'/i.test(q))).toBe(false);
  expect(hub.meter.snapshot()['host-goal-repeat'].rows_read).toBeLessThan(20);
  const names=state.storage.sql.exec("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'host_goal_%'").toArray().map((r:any)=>r.name);
  expect(names.sort()).toEqual(['host_goal_launch_running_v2','host_goal_running_insert','host_goal_running_update']);
  expect(hub.rowBudget.status().stopped).toBe(false);
 });
});


it.each([false,true])('measures full uncached initialization including real legacy upgrade=%s',async(legacyUpgrade)=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('existing-bootstrap-'+crypto.randomUUID()));
 await (runInDurableObject as any)(stub,async(hub:any,state:any)=>{
  let startupEnv:any={...env,SCHEMA_OBJECT_ROWID_HINTS:'{}'};
  if(legacyUpgrade){
   const setup=fixtureSql(hub,state);
   setup.exec('DROP TRIGGER messages_bind_agents');setup.exec(legacyMessageBindingTrigger);
   setup.exec("DELETE FROM hub_init_stamps WHERE key='agents_projectless_collision_validation_v1'");
   // Locator hints are untrusted identifiers, never definition receipts.
   // Gather before the measured cold boundary, without warming its SQL handle.
   const rows=state.storage.sql.exec("SELECT rowid,name,type,tbl_name FROM sqlite_master WHERE name IN ('messages_bind_agents','resource_catalog_fleet_sessions_update','fleet_launch_runtime_exit','agents_no_legacy_collision_insert','agents_no_legacy_collision_update','host_goal_running_insert','host_goal_running_update')").toArray();
   startupEnv={...env,SCHEMA_OBJECT_ROWID_HINTS:JSON.stringify(rows)};
  }
  // Preserve physical tables, rows and receipts. Reset the SQL/cache graph;
  // this independent restart boundary gets exactly one strict synthetic day.
  hub.cancelBudgetRefund();
  hub.writes=new WriteWatch();hub.meter=new RowsMeter();
  await restartFixtureHub(hub,startupEnv);
  // Include the outer constructor event's queue/root/alarm completion gate.
  // These are identity stamps, never a budget reset or a second initializer.
  hub.backgroundRootStamp=null;hub.backgroundAlarmStamp=null;
  await hub.budgeted(async()=>undefined,undefined);
  const ledger=hub.rowBudget.status(),totals=Object.values(hub.meter.snapshot()).reduce((sum:any,value:any)=>({
   calls:sum.calls+value.calls,rows_read:sum.rows_read+value.rows_read,rows_written:sum.rows_written+value.rows_written,
  }),{calls:0,rows_read:0,rows_written:0}) as any;
  const guardConsumed=ledger.reserved_total-ledger.available_total;
  console.log(JSON.stringify({evidence:'existing_schema_initialization_cost',legacy_message_upgrade:legacyUpgrade,physical_storage_preserved:true,
   fresh_write_watch:true,fresh_rows_meter:true,services_initialized:!!hub.conversationsService&&!!hub.jobsService,
   native_sql:totals,guard_consumed_total:guardConsumed,ledger,
   strict_two_hour_operation_allowance_exclusive:20000/12,startup_alone_below_two_hour_allowance:guardConsumed<20000/12,
   scope:'full initializeServices including subclass plus outer budgeted queue/root/alarm completion; includes internal index preflight and fixture-day ledger control in guard total',
   caveat:'one existing RowBudget object is retained; real eviction adds one cold KV ledger read. Deferred quiet checkpoint is not in this immediate receipt; startup remains included in the real fixed two-hour meter.',
   top_sql:hub.meter.statementTop(25).map((row:any)=>({scope:row.scope,sql:row.sql.slice(0,200),calls:row.calls,rows_read:row.rows_read,rows_written:row.rows_written})),
  }));
  expect(ledger).toMatchObject({limit_total:20000,stopped:false});
  expect(guardConsumed).toBeGreaterThan(0);expect(guardConsumed).toBeLessThan(20000/12);
  expect(hub.conversationsService).toBeDefined();expect(hub.jobsService).toBeDefined();
  if(legacyUpgrade){
   const drops=hub.meter.statementTop(4096).filter((row:any)=>/^DROP TRIGGER messages_bind_agents$/i.test(row.sql));
   expect(drops).toHaveLength(1);expect(drops[0].calls).toBe(1);
  }
  // Fixture cleanup only: do not leave the production five-minute timer alive
  // after this isolated observation. No unused credit is refunded here.
  hub.cancelBudgetRefund();
 });
});
