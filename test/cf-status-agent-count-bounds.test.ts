import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {RowsMeter} from '../src/rows-meter';
import {fixtureSql} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>void)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('agent-batch-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),at=new Date().toISOString();
 const meter=new RowsMeter(),queries:{sql:string;args:any[]}[]=[],sql=meter.wrap(new Proxy(h.sql,{get(target,key){
  if(key==='exec')return(q:string,...args:any[])=>{queries.push({sql:q,args});return target.exec(q,...args);};
  const value=target[key];return typeof value==='function'?value.bind(target):value;
 }}));
 const status=h.statusService as any,original=status.sql;status.sql=sql;
 try {fn({h,seed,at,meter,queries,read:()=>status.computeAgentCounts(Date.now())});}
 finally{status.sql=original;}
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
function control(seed:any,id:string,native:string|null,at:string,type='headless',state='running'){
 seed.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,session_type,binding_json,created_at,updated_at)
 VALUES(?,'fixture',?,'{}','orphan','codex','headless',?,?,?,?,?)`,id,id,state,type,native===null?null:JSON.stringify({native_id:native}),at,at);
}
describe('batched bounded status agent counts',()=>{
 it('uses constant SQL calls for100 known identities while preserving advisory waiting/probe/terminal matching',()=>fixture(({seed,at,meter,queries,read}:any)=>{
  for(let i=0;i<2100;i++){
   seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('orphan',?,?,'codex','exited',?,'/repo')",'old-'+i,'old-'+i,at);
   control(seed,'old-control-'+i,'old-'+i,at,'headless','stopped');
  }
  for(let i=0;i<100;i++){
   seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('orphan',?,?,'codex','running',?,'/repo')",'native-'+i,'tag-'+i,at);
   control(seed,'control-'+i,'native-'+i,at);
   if(i%4===0)seed.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)','orphan/native-'+i,'orphan','native-'+i,at,'{"waiting_confirmation":true}');
  }
  control(seed,'unbound',null,at);control(seed,'terminal-native','old-0',at);control(seed,'probe',null,at,'probe');
  seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('orphan','noise','PROBE-noise','codex','running',?,'/repo')",at);
  control(seed,'noise-a','noise',at,'headless','stopped');control(seed,'noise-b','noise',at,'headless','stopped');
  const result=meter.runSync('agents',read);
  expect(result).toEqual({running:76,idle:25,waiting:25,waiting_confirmation:25});
  expect(meter.snapshot().agents.calls).toBeLessThanOrEqual(20);expect(meter.snapshot().agents.rows_read).toBeLessThan(2000);
  expect(meter.snapshot().agents.rows_written).toBe(0);
  const alias=queries.find((q:any)=>q.sql.includes('SELECT r.position,c.session_type'));
  expect(alias).toBeTruthy();const plan=seed.exec('EXPLAIN QUERY PLAN '+alias.sql,...alias.args).toArray().map((r:any)=>r.detail).join(' | ');
  expect(plan).toMatch(/SEARCH s USING INDEX intake_queue_session_native/);expect(plan).toMatch(/SEARCH c USING INTEGER PRIMARY KEY/);
  expect(plan).not.toMatch(/SCAN s\b|SCAN c\b|TEMP B-TREE/);
  const presence=queries.find((q:any)=>q.sql.includes('SELECT r.id FROM requested r CROSS JOIN fleet_sessions'));
  const presencePlan=seed.exec('EXPLAIN QUERY PLAN '+presence.sql,...presence.args).toArray().map((r:any)=>r.detail).join(' | ');
  expect(presencePlan).toMatch(/SEARCH f USING COVERING INDEX sqlite_autoindex_fleet_sessions_1/);
  expect(presencePlan).not.toMatch(/SCAN f\b|TEMP B-TREE/);
 }));
 it('retains all-state alias ambiguity and exact observation identity refusal',()=>fixture(({seed,at,read}:any)=>{
  seed.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace) VALUES('orphan','native','tag','codex','running',?,'/repo')",at);
  control(seed,'first','native',at,'headless','stopped');
  expect(read()).toEqual({running:1,idle:0,waiting:0,waiting_confirmation:0});
  seed.exec('INSERT INTO session_lifecycle_observations VALUES(?,?,?,?,?)','orphan/native','foreign','native',at,'{}');
  expect(read()).toMatchObject({running:null,observation_pending:true,observation_reason:'observation_identity_mismatch'});
  seed.exec("UPDATE session_lifecycle_observations SET machine_id='orphan' WHERE session_key='orphan/native'");
  control(seed,'second','native',at,'probe','stopped');
  expect(read()).toMatchObject({running:null,observation_pending:true,observation_reason:'native_control_ambiguous'});
 }));
});
