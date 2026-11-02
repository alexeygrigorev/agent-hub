import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
const inDO: any = runInDurableObject;
import { IntakeWatchdog } from '../src/intake-watchdog';

it.each(['healthy-bound','queued-intent','healthy-external'])('does not alert idle for %s beside a stalled old session',async scenario=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('recovery-mixed-'+crypto.randomUUID()));
 await inDO(stub,async(h:any)=>{
  const now=Date.now(),at=new Date(now).toISOString(),created=now-7200000;
  const t:any={id:crypto.randomUUID(),title:'Mixed recovery owners',project:'demo',status:'open',created_at:created,history:[],acceptance_criteria:'Synthetic'};
  const add=(id:string,stalled:boolean)=>{
   const binding={native_id:id,tag:id,workspace:'fixture',native_created_at_ms:created};
   h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES(?,'fixture',?,?,'fixture','codex','headless','running',?,?,?)`,id,id,JSON.stringify({intake_task_id:t.id,workspace:'fixture'}),JSON.stringify(binding),at,at);
   h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,workspace,phase,engine,created_at_ms,observed_at,progress_json)
    VALUES('fixture',?,?,'fixture','running','codex',?,?,?)`,id,id,created,at,JSON.stringify({last_progress_at:new Date(stalled?created:now).toISOString()}));
  };
  add('stalled-old',true);
  if(scenario==='healthy-bound')add('healthy-new',false);
  else if(scenario==='queued-intent')h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
    VALUES('queued-new','fixture','queued-new',?,'codex','headless','queued',?,?)`,JSON.stringify({intake_task_id:t.id,workspace:'fixture'}),at,at);
  else{
   t.history.push({kind:'link',native_id:'healthy-external',host:'fixture'});
   h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,workspace,phase,engine,created_at_ms,observed_at,progress_json)
    VALUES('fixture','healthy-external','healthy-external','fixture','running','codex',?,?,?)`,created,at,JSON.stringify({last_progress_at:at}));
  }
  h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)",at);
  const notices:any[]=[];
  const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{notify:async(...args)=>{notices.push(args)}},()=>now);w.init();
  await w.tick();expect(w.project([t])[0]).toMatchObject({state:'active',idle_ms:null,forgotten:false});
  expect(notices.filter(n=>n[0]==='intake_watchdog'&&n[2]!==null)).toHaveLength(0);
 });
});
