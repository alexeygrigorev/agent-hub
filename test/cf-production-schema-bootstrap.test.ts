import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {WriteWatch} from '../src/write-watch';
import {fixtureSql,restartFixtureHub} from './helpers/fixture-budget';
import {deployedUserSchema,deployedSchemaObjectCount,deployedUserObjectCount} from './fixtures/production-schema-t2609u';

const reserved=(name:string)=>/^sqlite_/i.test(name)||/^_+cf/i.test(name);
const quote=(name:string)=>'"'+name.replaceAll('"','""')+'"';
const observedOldReceipts=['resource_request_projection:session_control_sessions','resource_request_projection:fleet_launches',
  'lifecycle_request_projection','named_agents:agents'];
const storageKinds=['audit','grants','note_archive','receipts','rules','tasks'];
const earlyTriggerNames=new Set(['resource_request_session_control_sessions_insert','resource_request_fleet_launches_insert',
  'lifecycle_request_insert','session_observation_intake_insert','intake_projection_insert','intake_storage_tasks_insert',
  'intake_export_insert','intake_export_count_insert','intake_export_total_insert','named_agent_registry_insert']);
const hintNames=['messages_bind_agents','resource_catalog_fleet_sessions_update','fleet_launch_runtime_exit',
  'agents_no_legacy_collision_insert','agents_no_legacy_collision_update','host_goal_running_insert','host_goal_running_update'];
const retained=1001;

describe('first restoration from the actual deployed schema',()=>{
 it('charges all new schema work and the real old-trigger replacement inside the strict startup allowance',async()=>{
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('deployed-schema-first-restore-'+crypto.randomUUID()));
  await (runInDurableObject as any)(stub,async(h:any,state:any)=>{
   h.cancelBudgetRefund();
   const setup=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString();
   const current=setup.exec('SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY rowid LIMIT 2049').toArray() as any[];
   expect(current.length).toBeLessThanOrEqual(2048);
   const protectedBefore=current.filter(row=>reserved(row.tbl_name)||(row.type==='table'&&reserved(row.name)));
   const owned=current.filter(row=>row.sql!==null&&!reserved(row.name)&&!reserved(row.tbl_name));
   expect(deployedUserSchema.every(row=>!reserved(row.name)&&!reserved(row.table))).toBe(true);
   expect(deployedUserSchema.some(row=>row.name==='intake_status_summary')).toBe(false);
   expect(deployedUserSchema.some(row=>row.name==='intake_summary_migration')).toBe(false);
   expect(deployedUserSchema.some(row=>row.name==='tbm_maintenance_cursor')).toBe(false);
   // Only this isolated workerd namespace's application schema is replaced.
   // All reserved Cloudflare/KV/SQLite objects remain outside the DROP list.
   state.storage.transactionSync(()=>{
    setup.exec('PRAGMA defer_foreign_keys=ON');
    for(const type of ['trigger','view','index','table'])for(const row of [...owned].reverse())if(row.type===type)
      setup.exec('DROP '+type.toUpperCase()+' '+quote(row.name));
    for(const type of ['table','view','index'])for(const row of deployedUserSchema)if(row.type===type)setup.exec(row.sql);
    // Existing OLD receipt rows are sanitized semantic fixture states from
    // bounded production metadata reads. No NEW migration is marked done.
    for(const key of observedOldReceipts)setup.exec('INSERT INTO hub_init_stamps VALUES(?,?)',key,now);
    setup.exec('INSERT INTO disk_trend_projection_meta VALUES(1)');
    setup.exec('INSERT INTO root_scheduling VALUES(1,0,?)',at);
    for(const key of ['checks_seeded','burn_check_seeded'])setup.exec('INSERT INTO hub_meta VALUES(?,?)',key,at);
    setup.exec("INSERT INTO engine_migrations VALUES('managed_account_max_concurrency_default_v1',?,?)",now,JSON.stringify({migrated:[]}));
    setup.exec('INSERT INTO conversation_storage_usage(singleton,schema_version) VALUES(1,2)');
    setup.exec("INSERT INTO session_observation_intake_meta VALUES(1,'',1,0)");
    setup.exec('INSERT INTO session_observation_intake_schema VALUES(1,2)');
    setup.exec('INSERT INTO intake_export_migration VALUES(1,NULL,1)');
    for(const kind of storageKinds)setup.exec('INSERT INTO intake_storage_totals VALUES(?,0,0,0,0,0)',kind);
    // Install only old write-through projection/accounting triggers while
    // fabricating retained history. Other runtime triggers are installed
    // afterwards, so this setup does not invent live queued work.
    for(const row of deployedUserSchema)if(row.type==='trigger'&&earlyTriggerNames.has(row.name))setup.exec(row.sql);
    for(let i=0;i<retained;i++){
     const id='legacy-'+String(i).padStart(4,'0');
     setup.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at,registry_unbound_from,registry_unbound_to)
       VALUES(?,'fixture','fixture','note','fixture','synthetic history',?,0,1,1,1)`,id,id);
     setup.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
       VALUES(?,'fixture','human','synthetic',?,'{}','fixture-host','codex','fixture','headless','/fixture','','stopped',?,?)`,id,id,at,at);
     setup.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,mode,state,created_at,updated_at)
       VALUES(?,'fixture',?,'{}','headless','failed',?,?)`,id,id,at,at);
     setup.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace)
       VALUES('fixture-host',?,?,'codex','exited',?,'/fixture')`,id,id,at);
     setup.exec("INSERT INTO agents(project,tag) VALUES('fixture',?)",id);
     setup.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,scopes,created_by_sub,created_at,expires_at,revoked_at)
       VALUES(?,'host',?,?,'fixture','["host"]','fixture',?,?,?)`,id,(i+1).toString(16).padStart(64,'0'),id,at,at,at);
     setup.exec(`INSERT INTO tbm_reward_events(event_id,timestamp_ms,pool_id,intake_task_id,active_mode,useful_work_reward,pacing_error_penalty,floor_breach_penalty,engine_mix_penalty,waste_at_reset_penalty,banked_waste_penalty,net_reward,token_efficiency_eta)
       VALUES(?,0,'fixture',?,'high',0,0,0,0,0,0,0,0)`,id,id);
     setup.exec('INSERT INTO resource_resident_samples VALUES(?,0,?)',id,at);
     const data=JSON.stringify({id,project:'fixture',status:'done',title:'synthetic historical task',history:[],created_at:0,updated_at:0,version:1});
     setup.exec('INSERT INTO intake_tasks VALUES(?,?)',id,data);
    }
    // Known small retained sources also pay their real first-deploy index
    // work; in particular OLD disk history lacks the current time index.
    for(let i=0;i<200;i++)setup.exec(`INSERT INTO disk_observations(id,machine_id,observed_at,observed_at_ms,received_at,payload_json,reporter)
      VALUES(?,'fixture-host',?,?,?,'{"filesystems":[]}','fixture')`,'fixture-disk-'+i,at,now,at);
    for(let i=0;i<17;i++)setup.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor)
      VALUES(?,'writer',?,'{}','failed',?,?,'fixture')`,'fixture-job-'+i,'fixture-job-'+i,at,at);
    for(const name of ['root','daily-writer'])setup.exec('INSERT INTO leases(name,epoch,released_at) VALUES(?,0,?)',name,at);
    for(const row of deployedUserSchema)if(row.type==='trigger'&&!earlyTriggerNames.has(row.name))setup.exec(row.sql);
   });
   const protectedAfter=setup.exec('SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY rowid LIMIT 2049').toArray()
     .filter((row:any)=>reserved(row.tbl_name)||(row.type==='table'&&reserved(row.name)));
   expect(protectedAfter).toEqual(protectedBefore);
   // Verify the old receipt boundaries match synthetic source projections.
   for(const table of ['messages','fleet_launches','fleet_sessions','session_control_sessions','agents','credentials','tbm_reward_events',
     'resource_resident_samples','intake_tasks','intake_task_projection','session_observation_intakes','lifecycle_request_projection'])
     expect(setup.exec('SELECT 1 FROM '+table+' LIMIT 1002').toArray()).toHaveLength(retained);
   expect(setup.exec("SELECT count FROM intake_storage_totals WHERE kind='tasks'").one().count).toBe(retained);
   const hints=setup.exec(`SELECT rowid,name,type,tbl_name FROM sqlite_master WHERE name IN (${hintNames.map(()=>'?').join(',')})`,...hintNames).toArray();
   expect(hints).toHaveLength(hintNames.length);
   const oldCatalog=setup.exec('SELECT name,tbl_name FROM sqlite_master ORDER BY rowid LIMIT 1025').toArray();
   const oldObjects=oldCatalog.length;
   expect(oldCatalog.filter((row:any)=>!reserved(row.tbl_name))).toHaveLength(deployedUserObjectCount);
   expect(oldObjects).toBe(deployedUserObjectCount+protectedBefore.length);
   // All synthetic history/replayed OLD schema work ends above. The fresh
   // initializer below pays EVERY new table/index/trigger and source probe.
   h.writes=new WriteWatch();h.meter=new RowsMeter();
   const nativeMeter=new RowsMeter();
   let failure:unknown;
   try{
    await restartFixtureHub(h,{...env,SCHEMA_OBJECT_ROWID_HINTS:JSON.stringify(hints)},nativeMeter);
    h.backgroundRootStamp=null;h.backgroundAlarmStamp=null;
    await h.budgeted(async()=>undefined,undefined);
   }catch(error){failure=error;}
   const ledger=h.rowBudget.status(),cost=ledger.reserved_total-ledger.available_total;
   const native=Object.values(h.meter.snapshot()).reduce((sum:any,row:any)=>({calls:sum.calls+row.calls,rows_read:sum.rows_read+row.rows_read,rows_written:sum.rows_written+row.rows_written}),{calls:0,rows_read:0,rows_written:0}) as any;
   console.log(JSON.stringify({evidence:'actual_deployed_schema_first_restoration',captured_production_objects:deployedSchemaObjectCount,native_old_objects:oldObjects,
    protected_platform_objects:protectedBefore.length,deployed_user_objects:deployedUserObjectCount,deployed_ddl:deployedUserSchema.length,
    retained_per_large_source:retained,retained_small_sources:{disk_observations:200,jobs:17,leases:2},guard_consumed_total:cost,native_sql:native,ledger,
    failure:failure?{name:(failure as any)?.name,code:(failure as any)?.code}:null,
    top_sql:h.meter.statementTop(30),
    all_guarded_native_sql:nativeMeter.snapshot(),all_guarded_native_top_sql:nativeMeter.statementTop(60),
    scope:'actual OLD schema plus observed OLD receipts; full current initializer/subclass and outer queue/root/alarm gates; one strict20kday'}));
   h.cancelBudgetRefund();
   if(failure)throw failure;
   expect(ledger).toMatchObject({limit_total:20000,stopped:false});expect(cost).toBeGreaterThan(0);expect(cost).toBeLessThan(20000/12);
   expect(h.conversationsService).toBeDefined();expect(h.jobsService).toBeDefined();
   expect(h.meter.statementTop(4096).filter((row:any)=>/^DROP TRIGGER messages_bind_agents$/i.test(row.sql))).toHaveLength(1);
  });
 });
});
