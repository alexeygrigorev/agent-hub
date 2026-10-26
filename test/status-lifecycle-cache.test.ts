import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { buildSessionInputs } from '../src/session_lifecycle_registry';
import { StatusService } from '../src/status';

const inDO: any = runInDurableObject;
const id = '00000000-0000-4000-8000-000000000001';
const launch = '00000000-0000-4000-8000-000000000002';

function seed(h: any) {
  const now = new Date().toISOString();
  h.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES('cache-host','cache-host','cache-host',?)",now);
  h.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES('cache-owner','human','cache-hash','cache-family','owner','human:owner','[]','owner',?,?)`,now,now);
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,session_type,state,binding_json,fleet_launch_id,owner_token_id,owner_family,created_at,updated_at)
    VALUES(?,'human:owner','cache','{}','cache-host','codex','headless','headless','running',?,?,'cache-owner','cache-family',?,?)`,
    id,JSON.stringify({native_id:'cache-native',workspace:'/cache',tag:'cache-tag',native_created_at_ms:Date.now()}),launch,now,now);
  h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
    VALUES(?,'human:owner','human','cache','cache','{}','cache-host','codex','default','headless','/cache','cache','running','cache-native',?,?)`,launch,now,now);
  h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,last_activity_at,workspace)
    VALUES('cache-host','cache-native','cache-tag','codex','running','working',?,?,'/cache')`,now,now);
  h.sql.exec("INSERT INTO intake_tasks VALUES('cache-task',?)",JSON.stringify({id:'cache-task',project:'cache',status:'open',history:[{kind:'link',hub_session_id:id}]}));
  h.sql.exec("INSERT INTO session_lifecycle_observations VALUES('cache-host/cache-native','cache-host','cache-native',?,'{}')",now);
  return Date.parse(now);
}

it('invalidates warmed lifecycle facts for every source write and preserves creator protections',async()=>{
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('cache-writes'));
  await inDO(stub,(h:any)=>{
    seed(h);
    const service:any=h.statusService;
    const ids=new Set([id]);
    const read=()=>service.readLifecycleInputs(ids);
    const normalized=(value:any)=>({inputs:value.inputs,refs:[...value.refs]});
    const fresh=()=>normalized(buildSessionInputs(h.sql,ids));
    expect(read().refs.get('cache-host/cache-native').protected_session).toBe(false);
    h.sql.exec("UPDATE credentials SET family_id='other' WHERE id='cache-owner'");
    expect(read().refs.get('cache-host/cache-native').protected_session).toBe(true);
    h.sql.exec("UPDATE credentials SET family_id='cache-family' WHERE id='cache-owner'");
    expect(read().refs.get('cache-host/cache-native').protected_session).toBe(false);
    const warm=read();
    h.sql.exec("UPDATE credentials SET last_used_at=? WHERE id='cache-owner'",new Date().toISOString());
    expect(read()).toBe(warm);
    h.sql.exec("INSERT INTO session_control_protections(session_id,actor,request_key,created_at) VALUES(?,'human:owner','keep',?)",id,new Date().toISOString());
    expect(read().refs.get('cache-host/cache-native').protected_session).toBe(true);
    const sources=[
      ['session_control_sessions','id',id,'state','stopped'],
      ['fleet_launches','id',launch,'state','failed'],
      ['fleet_sessions','native_id','cache-native','phase','stopped'],
      ['session_control_protections','session_id',id,'request_key','keep updated'],
      ['session_lifecycle_observations','session_key','cache-host/cache-native','payload_json','{"awaiting_alexey":true}'],
      ['intake_task_projection','id','cache-task','links',JSON.stringify([{kind:'link',hub_session_id:id},{kind:'report',reference:'cache-report'}])],
      ['credentials','id','cache-owner','family_id','other-family'],
    ];
    for(const [table,pk,key,column,value] of sources) {
      let before=read();
      h.sql.exec(`UPDATE ${table} SET ${column}=? WHERE ${pk}=?`,value,key);
      expect(read(),table+' UPDATE').not.toBe(before);
      expect(normalized(read())).toEqual(fresh());
      const row=h.sql.exec(`SELECT * FROM ${table} WHERE ${pk}=?`,key).toArray()[0];
      before=read();
      h.sql.exec(`DELETE FROM ${table} WHERE ${pk}=?`,key);
      expect(read(),table+' DELETE').not.toBe(before);
      expect(normalized(read())).toEqual(fresh());
      before=read();
      const columns=Object.keys(row);
      h.sql.exec(`INSERT INTO ${table}(${columns.join(',')}) VALUES(${columns.map(()=>'?').join(',')})`,...columns.map(k=>row[k]));
      expect(read(),table+' INSERT').not.toBe(before);
      expect(normalized(read())).toEqual(fresh());
    }
  });
});

it('preserves last-key brief semantics for legacy duplicate JSON keys',async()=>{
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('cache-duplicate-brief'));
  await inDO(stub,(h:any)=>{
    seed(h);
    h.sql.exec('UPDATE session_control_sessions SET fleet_launch_id=NULL WHERE id=?',id);
    h.sql.exec("UPDATE fleet_launches SET native_id='unrelated' WHERE id=?",launch);
    for(const [raw,expected] of [['{"brief":false,"brief":"yes"}',true],['{"brief":"yes","brief":false}',false]] as const) {
      h.sql.exec('UPDATE session_control_sessions SET request_json=? WHERE id=?',raw,id);
      expect(buildSessionInputs(h.sql,new Set([id])).refs.get('cache-host/cache-native')?.has_brief).toBe(expected);
    }
  });
});

it('keeps decisions and time live, survives rollback ABA, and bypasses absent initialization',async()=>{
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('cache-time'));
  await inDO(stub,(h:any)=>{
    const now=seed(h),service:any=h.statusService,ids=new Set([id]);
    const read=()=>service.readLifecycleInputs(ids);
    const initial=read();
    expect(service.readLifecycleInputs(new Set([id]))).toBe(initial);
    const first=service.lifecycleBySessionId(now,ids).get(id);
    const later=service.lifecycleBySessionId(now+86400000,ids).get(id);
    expect(later).not.toEqual(first);
    expect(read()).toBe(initial);
    h.sql.exec("INSERT INTO session_lifecycle_decisions(session_key,decision_id,kind,actor,reason,created_at) VALUES('cache-host/cache-native','cache-decision','drop','human:owner','new decision',?)",new Date(now).toISOString());
    expect(service.lifecycleBySessionId(now,ids).get(id).decision.kind).toBe('drop');
    expect(read()).toBe(initial);
    expect(()=>h.ctx.storage.transactionSync(()=>{
      h.sql.exec("UPDATE fleet_sessions SET engine='claude' WHERE native_id='cache-native'");
      expect(read().inputs[0].engine).toBe('claude');
      throw new Error('rollback cache read');
    })).toThrow('rollback cache read');
    // No intervening read: a reused monotonic generation could wrongly hit.
    h.sql.exec("UPDATE fleet_sessions SET engine='opencode' WHERE native_id='cache-native'");
    expect(read().inputs[0].engine).toBe('opencode');
    const uncached:any=new StatusService(h.sql,(fn:any)=>fn(),h.intakeService);
    expect(uncached.readLifecycleInputs(ids)).not.toBe(uncached.readLifecycleInputs(ids));
    service.initLifecycleCache();
    expect(read()).not.toBe(initial);
    h.sql.exec('DELETE FROM status_lifecycle_revision');
    const missing=read();
    h.sql.exec("UPDATE fleet_sessions SET engine='codex' WHERE native_id='cache-native'");
    expect(read()).not.toBe(missing);
    expect(read().inputs[0].engine).toBe('codex');
  });
});

it('refreshes cached status control rows and latest observations after writes and rollback',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('status-source-cache'));
 await inDO(stub,(h:any)=>{
  const now=seed(h),service:any=h.statusService;
  const query='SELECT id,state,binding_json FROM session_control_sessions WHERE id=?';
  const read=()=>service.cachedSourceRows(query,id);
  expect(read()).toBe(read());
  h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?",id);
  expect(read()[0].state).toBe('stopped');
  const insert=(observation:string,state:string)=>h.sql.exec('INSERT INTO status_observations(id,machine_id,hub_session_id,kind,observed_at,payload_json,reporter,received_at) VALUES(?,?,?,?,?,?,?,?)',observation,'cache-host',id,'session',new Date(now).toISOString(),JSON.stringify({state}),'cache-host',new Date(now).toISOString());
  const latest=()=>service.latestObservations([id]).get(id+'\nsession');
  expect(latest()).toBeUndefined();insert('source-observation','working');
  expect(latest().payload.state).toBe('working');
  expect(()=>h.ctx.storage.transactionSync(()=>{
   h.sql.exec("UPDATE status_observations SET payload_json='{\"state\":\"failed\"}' WHERE id='source-observation'");
   expect(latest().payload.state).toBe('failed');
   throw new Error('source cache rollback');
  })).toThrow('source cache rollback');
  h.sql.exec("UPDATE session_control_sessions SET state='running' WHERE id=?",id);
  expect(read()[0].state).toBe('running');expect(latest().payload.state).toBe('working');
  h.sql.exec("DELETE FROM status_observations WHERE id='source-observation'");
  expect(latest()).toBeUndefined();
  service.initLifecycleCache();expect(read()).toBe(read());
  h.sql.exec('DELETE FROM status_lifecycle_revision');
  expect(read()).not.toBe(read());
 });
});

it('invalidates cached agent counts on native, control and observation state changes',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('status-count-cache'));
 await inDO(stub,(h:any)=>{
  const now=seed(h),service:any=h.statusService;
  const read=()=>service.computeAgentCounts(now);
  expect(read()).toMatchObject({running:1,waiting:0});
  const exposed=read();exposed.running=999;expect(read().running).toBe(1);
  h.sql.exec("UPDATE session_lifecycle_observations SET payload_json='{\"waiting_confirmation\":true}' WHERE native_id='cache-native'");
  expect(read()).toMatchObject({running:0,idle:1,waiting:1,waiting_confirmation:1});
  expect(()=>h.ctx.storage.transactionSync(()=>{
   h.sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id='cache-native'");
   expect(read().waiting).toBe(0);throw new Error('count rollback');
  })).toThrow('count rollback');
  h.sql.exec("UPDATE session_control_sessions SET session_type='probe' WHERE id=?",id);
  expect(read()).toEqual({running:0,idle:0,waiting:0,waiting_confirmation:0});
  h.sql.exec("UPDATE session_control_sessions SET session_type='headless',binding_json=NULL WHERE id=?",id);
  expect(read()).toMatchObject({running:1,waiting:1});
 });
});

it('retries agent observations after a failed read instead of caching incomplete counts',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('status-count-retry'));
 await inDO(stub,(h:any)=>{
  const now=seed(h),service:any=h.statusService;
  h.sql.exec("UPDATE session_lifecycle_observations SET payload_json='{\"waiting_confirmation\":true}' WHERE native_id='cache-native'");
  const rows=service.rows.bind(service);let failOnce=true;
  service.rows=(query:string,...args:any[])=>{
   if(failOnce && query.includes('FROM session_lifecycle_observations')) {failOnce=false;throw new Error('transient observation read');}
   return rows(query,...args);
  };
  try {
   expect(service.computeAgentCounts(now)).toMatchObject({running:1,waiting:0});
   expect(service.computeAgentCounts(now)).toMatchObject({running:0,waiting:1,waiting_confirmation:1});
  } finally {service.rows=rows;}
 });
});
