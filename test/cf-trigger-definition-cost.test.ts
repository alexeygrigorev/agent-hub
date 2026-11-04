import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {bindAgentCredentials} from '../src/agent_registry';
import {schemaSqlEquivalent} from '../src/schema-sql-equivalent';

describe('definition-verified constructor trigger upgrades',()=>{
 it('preserves quoted SQL values while comparing optional CREATE syntax',()=>{
  expect(schemaSqlEquivalent("CREATE TRIGGER IF NOT EXISTS t AFTER INSERT ON x BEGIN SELECT 'Case  Sensitive'; END;","create trigger t after insert on x begin select 'Case  Sensitive'; end")).toBe(true);
  expect(schemaSqlEquivalent("CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 'A B'; END","CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 'ab'; END")).toBe(false);
  expect(schemaSqlEquivalent("CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT '/* literal */'; END","CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT ''; END")).toBe(false);
 });
 it('keeps current live definitions and replaces modified definitions exactly once',async()=>{
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('trigger-definitions-'+crypto.randomUUID()));
  await (runInDurableObject as any)(stub,async(h:any,state:any)=>{
   await h.rowBudget.begin(true);const day=h.rowBudget.status().day,queries:string[]=[];
   const original=h.sql;
   const sql=new Proxy(original,{get(target,key){if(key==='exec')return(q:string,...args:any[])=>{queries.push(q);return target.exec(q,...args);};const value=target[key];return typeof value==='function'?value.bind(target):value;}});
   const resources=h.fleetService.resources,resourceSql=resources.sql;resources.sql=sql;
   const initialize=()=>{bindAgentCredentials(sql,(fn:any)=>state.storage.transactionSync(fn));resources.initCatalogCache();};
   try{
    initialize();initialize();
    const drops=()=>queries.filter(q=>/^DROP TRIGGER (?:IF EXISTS )?(messages_bind_agents|resource_catalog_fleet_sessions_update)$/i.test(q));
    expect(drops()).toEqual([]);
    expect(queries.filter(q=>q.includes('ORDER BY rowid LIMIT 1025'))).toHaveLength(1);
    // Actual old/incorrect definitions, not a receipt or a trigger name,
    // determine whether a replacement is required.
    for(const [name,table] of [['messages_bind_agents','messages'],['resource_catalog_fleet_sessions_update','fleet_sessions']]){
     original.exec(`DROP TRIGGER ${name}`);
     original.exec(`CREATE TRIGGER ${name} AFTER INSERT ON ${table} BEGIN SELECT 'obsolete'; END`);
    }
    initialize();expect(drops().sort()).toEqual(['DROP TRIGGER messages_bind_agents','DROP TRIGGER resource_catalog_fleet_sessions_update']);
    initialize();expect(drops()).toHaveLength(2);
    const definitions=state.storage.sql.exec("SELECT name,sql FROM sqlite_master WHERE name IN ('messages_bind_agents','resource_catalog_fleet_sessions_update')").toArray();
    expect(definitions).toHaveLength(2);expect(definitions.every((row:any)=>!row.sql.includes("SELECT 'obsolete'"))).toBe(true);
    expect(queries.filter(q=>q.includes('ORDER BY rowid LIMIT 1025')).length).toBeLessThanOrEqual(2);
    expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
   }finally{resources.sql=resourceSql;}
  });
 });
});
