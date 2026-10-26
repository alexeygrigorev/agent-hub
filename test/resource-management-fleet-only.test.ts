import {describe, expect, it} from 'vitest';
import {env, runInDurableObject} from 'cloudflare:test';
import type {Hub} from '../src/hub';
import type {Env} from '../src/env';
import {FleetService} from '../src/fleet';
import type {ResourceManagement} from '../src/resource-management';

const now=Date.now(), iso=(offset=0)=>new Date(now+offset).toISOString();
async function fixture(check:(resources:ResourceManagement,sql:any)=>void) {
  const bindings=env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('fleet-resource-'+crypto.randomUUID())),async(hub:Hub,state:DurableObjectState)=>{
    const fleet=new FleetService(hub.sql,cb=>state.storage.transactionSync(cb),()=>now);fleet.init();
    check(fleet.resources,hub.sql);
  });
}
function launch(sql:any, extra:any={}) {
  const row={id:'direct',machine:'host',native:'native',state:'running',mode:'headless',request:{project:'demo',tier:'daily',task_kind:'ask'},...extra};
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at,model_selection_json)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,row.id,'operator','human','read-only audit',row.id,JSON.stringify(row.request),row.machine,'opencode','shared-go',row.mode,'/repo','audit',row.state,row.native,iso(-60000),iso(),JSON.stringify({model:'muse-spark-1.3'}));
}
function sample(sessionId='fleet:direct', extra:any={}) {
  return {session_id:sessionId,native_id:'native',current_rss_mb:200,peak_rss_mb:220,peak_pss_mb:240,cpu_seconds:12,runtime_s:60,p95_cpu_cores:.4,observed_at:iso(),...extra};
}
function control(sql:any, shadow=false) {
  const id=shadow ? 'native' : 'control';
  const req=shadow ? {provenance:'observed_native'} : {project:'demo',tier:'daily',task_kind:'ask'};
  const binding={native_id:'native',tag:'task-hub-direct',workspace:'/repo',native_created_at_ms:now-60000,...(shadow ? {provenance:'observed_native'} : {})};
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`,id,'operator',shadow ? 'native-native' : 'control',JSON.stringify(req),shadow ? null : 'direct','host','opencode',shadow ? 'interactive' : 'headless','running',JSON.stringify(binding),iso(-60000),iso());
}

describe('resource accounting for Fleet-only tasks',()=>{
  it('accounts a real native launch without creating a SessionControl intent and preserves its final native join',async()=>fixture((resources,sql)=>{
    launch(sql);
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,last_activity_at,created_at_ms)
      VALUES(?,?,?,?,?,?,?,?,?)`,'host','native','task-hub-direct','opencode','running',iso(),'/repo',iso(),now-60000);
    expect(resources.sessions('host')).toEqual([expect.objectContaining({session_id:'fleet:direct',fleet_launch_id:'direct',native_id:'native',managed:false,project:'demo',tier:'daily',kind:'ask',model:'muse-spark-1.3'})]);
    resources.report('host',{samples:[sample()],reaps:[]});
    expect(resources.headroom('host').reserved_mb).toBe(312); // measured 200 MiB credits the 512 MiB launch ramp
    sql.exec("UPDATE fleet_launches SET state='exited',finished_at=? WHERE id='direct'",iso());
    resources.report('host',{samples:[sample()],reaps:[]});
    expect(resources.usage()).toEqual([expect.objectContaining({session_id:'fleet:direct',fleet_launch_id:'direct',state:'exited',finished_at:iso(),cpu_seconds:12,peak_rss_mb:220,peak_pss_mb:240,p95_cpu_cores:.4,token_join_key:'host:native'})]);
    expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').one().n).toBe(0);
    expect(()=>resources.report('other',{samples:[sample()],reaps:[]})).toThrow('resource_binding_mismatch');
    expect(()=>resources.report('host',{samples:[sample('fleet:direct',{native_id:'different'})],reaps:[]})).toThrow('resource_binding_mismatch');
  }));

  it('does not grant stop authority to Fleet-only entries even with an intake or interactive mode',async()=>fixture((resources,sql)=>{
    launch(sql,{request:{intake_task_id:'task'}});
    expect(resources.sessions('host')[0].managed).toBe(false);
    expect(()=>resources.evidence('host',{id:'evidence',session_id:'fleet:direct',native_id:'native',reason:'idle',output:'saved'})).toThrow('session_not_reapable');
    launch(sql,{id:'interactive',native:'human',mode:'interactive'});
    expect(resources.sessions('host').find(s=>s.native_id==='human')).toMatchObject({mode:'interactive',managed:false,keep:true});
    launch(sql,{id:'unbound',native:null,state:'offered'});
    expect(resources.sessions('host').some(s=>s.fleet_launch_id==='unbound')).toBe(false);
  }));

  it('does not turn time queued before spawn into process runtime',async()=>fixture((resources,sql)=>{
    launch(sql,{state:'exited'});
    sql.exec("UPDATE fleet_launches SET created_at=?,finished_at=? WHERE id='direct'",iso(-86400000),iso());
    expect(resources.sessions('host')[0]).toMatchObject({started_at:null,finished_at:iso()});
    resources.report('host',{samples:[sample()],reaps:[]});
    expect(resources.usage()[0]).toMatchObject({started_at:null,runtime_s:60,cpu_seconds:12,avg_cpu_cores:.2});
  }));

  it('keeps one durable native ledger identity when a real control binding arrives later',async()=>fixture((resources,sql)=>{
    launch(sql);resources.report('host',{samples:[sample()],reaps:[]});
    control(sql);
    expect(resources.sessions('host')).toEqual([expect.objectContaining({session_id:'control',managed:true})]);
    resources.report('host',{samples:[sample('control',{cpu_seconds:20})],reaps:[]});
    // A report already in flight under the Fleet accounting alias stays valid.
    resources.report('host',{samples:[sample('fleet:direct',{cpu_seconds:18})],reaps:[]});
    expect(resources.usage()).toEqual([expect.objectContaining({session_id:'fleet:direct',cpu_seconds:20,token_join_key:'host:native'})]);
    sql.exec('INSERT INTO session_control_protections VALUES(?,?,?,?)','control','human','keep',iso());
    expect(resources.sessions('host')[0]).toMatchObject({managed:false,keep:true});
  }));

  it('upgrades an inferred native shadow with Fleet metadata without authorizing reaping',async()=>fixture((resources,sql)=>{
    control(sql,true);
    resources.report('host',{samples:[sample('native')],reaps:[]});
    launch(sql);
    expect(resources.sessions('host')).toEqual([expect.objectContaining({session_id:'native',managed:false,keep:false,model:'muse-spark-1.3',tier:'daily',kind:'ask'})]);
    resources.report('host',{samples:[sample('fleet:direct')],reaps:[]});
    expect(resources.usage()).toEqual([expect.objectContaining({session_id:'native',token_join_key:'host:native',model:'muse-spark-1.3'})]);
  }));

  it('prioritizes a new direct task within the bounded catalog while accepting reports already in flight',async()=>fixture((resources,sql)=>{
    sql.exec(`WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000)
      INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
      SELECT 'old-'||x,'operator','human','old task','old-'||x,'{}','host','opencode','shared-go','headless','/repo','audit','exited','old-native-'||x,?,? FROM n`,iso(-120000),iso(-60000));
    launch(sql);
    const catalog=resources.sessions('host');expect(catalog).toHaveLength(1000);
    expect(catalog[0]).toMatchObject({session_id:'fleet:direct',state:'running'});
    resources.report('host',{samples:[sample('fleet:old-1000',{native_id:'old-native-1000'})],reaps:[]});
    expect(resources.usage()[0]).toMatchObject({token_join_key:'host:old-native-1000'});
  }));
});
