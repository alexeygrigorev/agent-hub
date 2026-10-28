import { describe, it, expect } from 'vitest';
import { env, runInDurableObject, SELF } from 'cloudflare:test';
import { IntakeWatchdog, queuePolicyFrom, WATCHDOG_INTERVAL, QUEUE_STALL_MS, QUEUE_RETRY_LIMIT, QUEUE_RETRY_MAX_MS } from '../src/intake-watchdog';
const NOW = Date.parse('2026-10-28T10:00:00Z');
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('queue-tests'));
const task = (id = 'queue-task'): any => ({ id, project:'demo', title:'Queue task', status:'open', version:1,
  created_at:NOW, updated_at:NOW, acceptance_criteria:'Verify the result', history:[] });
async function fixture(fn: (h:any)=>any) {
  return (runInDurableObject as any)(stub(), async (h:any) => {
    for (const table of ['intake_queue_admissions','intake_queue_budget_meta','intake_queue_budget_alerts','intake_queue_digest','intake_queue_escalations','session_control_wakes','intake_queue_movement','intake_restart_queue','intake_watchdog_meta','session_control_sessions','fleet_sessions','claims','fleet_catalog_seen']) h.sql.exec('DELETE FROM '+table);
    h.sql.exec("UPDATE intake_queue_cursor SET after_id='',forgotten_seen=0");
    h.sql.exec("INSERT OR REPLACE INTO machines(id,name,hostname,registered_at,capabilities) VALUES('queue-host','queue-host','queue-host','2026-10-28T10:00:00Z',?)",JSON.stringify({workspaces:['demo']}));
    return fn(h);
  });
}
function session(h:any, t:any, id:string, state:string, bound=false) {
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
    VALUES(?,'hub:service:intake-watchdog',?,?,'codex','interactive',?,?,?,'queue-host',?)`,id,id,JSON.stringify({intake_task_id:t.id,workspace:'demo',with_goal:true}),state,new Date(NOW).toISOString(),new Date(NOW).toISOString(),bound?JSON.stringify({native_id:'native',tag:'native',workspace:'demo',native_created_at_ms:NOW-3600000}):null);
}
describe('autonomous intake queue',()=>{
  it('continues beyond three productive sessions by resetting the healthy episode',()=>fixture(async h=>{
    const t=task('healthy-episode');let now=NOW,starts=0,notes=0;
    const fresh=()=>h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(now).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      start:async()=>{
        const id='healthy-'+(++starts),native='healthy-native-'+starts;
        session(h,t,id,'running',true);
        h.sql.exec('UPDATE session_control_sessions SET created_at=?,binding_json=? WHERE id=?',new Date(now+starts).toISOString(),JSON.stringify({native_id:native,tag:'native',workspace:'demo',native_created_at_ms:NOW-3600000}),id);
        h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,last_activity_at,agent_state) VALUES('queue-host',?,'native','running',?,'demo',?,?,'working')",native,new Date(now).toISOString(),NOW-3600000,new Date(now).toISOString());
        fresh();return {status:202,body:{id,state:'running'}};
      },note:async()=>{notes++;return true}
    },()=>now);w.init();await w.tick();
    for(let i=1;i<=3;i++){
      now+=WATCHDOG_INTERVAL;fresh();await w.tick();
      expect(w.queue([t])[0].attempts).toBe(0);
      h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?",'healthy-'+i);
      h.sql.exec("UPDATE fleet_sessions SET phase='exited',observed_at=? WHERE native_id=?",new Date(now).toISOString(),'healthy-native-'+i);
      now+=QUEUE_RETRY_MAX_MS;fresh();await w.tick();
    }
    expect(starts).toBe(4);expect(notes).toBe(0);expect(w.queue([t])[0].state).not.toBe('escalated');
  }));
  it('caps same-project held intents and leaves further work visibly queued',()=>fixture(async h=>{
    const tasks=[task('cap-a'),task('cap-b'),task('cap-c')];let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{
      start:async input=>{starts++;const t=tasks.find(t=>t.id===input.intake_task_id)!;session(h,t,'cap-session','held');return {status:202,body:{id:'cap-session',state:'held'}}}
    },()=>NOW);w.init();await w.tick();
    expect(starts).toBe(1);expect(w.queue(tasks).filter(r=>r.reason==='watchdog_project_capacity')).toHaveLength(2);
    expect(w.status().automation).toMatchObject({enabled:true,max_inflight:2,max_per_project:1});
  }));
  it('caps total held intents across projects through indexed owned-session receipts',()=>fixture(async h=>{
    const tasks=['a','b','c'].map(id=>({...task('global-'+id),project:'project-'+id}));let starts=0;
    h.sql.exec('UPDATE machines SET capabilities=? WHERE id=?',JSON.stringify({workspaces:tasks.map(t=>t.project)}),'queue-host');
    for(const t of tasks)h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',t.id,JSON.stringify(t));
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{
      start:async input=>{const t=tasks.find(t=>t.id===input.intake_task_id)!;const id='global-session-'+(++starts);session(h,t,id,'held');return {status:202,body:{id,state:'held'}}}
    },()=>NOW);w.init();await w.tick();
    expect(starts).toBe(2);expect(w.queue(tasks).find(r=>r.id==='global-c')).toMatchObject({state:'queued',reason:'watchdog_global_capacity'});
  }));
  it('retains a lost-reply reservation across watchdog reconstruction',()=>fixture(async h=>{
    const tasks=[task('lost-a'),task('lost-b')];let now=NOW,starts=0;
    const hooks={policy:()=>queuePolicyFrom({HUB_INTAKE_QUEUE_MAX_INFLIGHT:'1'}),start:async()=>{starts++;throw new Error('lost reply')}};
    let w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),hooks,()=>now);w.init();await w.tick();
    expect(starts).toBe(1);expect(w.queue(tasks).find(r=>r.id==='lost-b')?.reason).toBe('watchdog_global_capacity');
    now+=WATCHDOG_INTERVAL;w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),hooks,()=>now);w.init();await w.tick();
    expect(starts).toBe(1);expect(h.sql.exec('SELECT task_id FROM intake_queue_admissions').toArray()).toEqual([{task_id:'lost-a'}]);
  }));
  it.each(['blocked','archived','done','done-note','missing'])('releases an unknown reservation when its task becomes %s',kind=>fixture(async h=>{
    const first=task('retired-a'),next=task('retired-b'),tasks=[first,next];let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{
      policy:()=>queuePolicyFrom({HUB_INTAKE_QUEUE_MAX_INFLIGHT:'1'}),
      start:async input=>{
        starts++;if(input.intake_task_id===first.id)throw new Error('lost reply');
        session(h,next,'after-retirement','queued');return {status:202,body:{id:'after-retirement',state:'queued'}};
      }
    },()=>now);w.init();await w.tick();expect(starts).toBe(1);
    if(kind==='blocked'||kind==='done')first.status=kind;
    if(kind==='archived')first.archived_at=now;
    if(kind==='done-note')first.history=[{kind:'note',text:'DONE commit abcdef1\nlive check: inspect receipt\nresult: all AC confirmed'}];
    if(kind==='missing')tasks.shift();
    now+=WATCHDOG_INTERVAL;await w.tick();
    expect(starts).toBe(2);expect(h.sql.exec('SELECT task_id FROM intake_queue_admissions WHERE task_id=?',first.id).toArray()).toEqual([]);
  }));
  it('keeps an active canonical session counted after retiring its reservation',()=>fixture(async h=>{
    const first=task('active-retired-a'),next=task('active-retired-b'),tasks=[first,next];let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{
      policy:()=>queuePolicyFrom({HUB_INTAKE_QUEUE_MAX_INFLIGHT:'1'}),start:async()=>{starts++;throw new Error('lost reply')}
    },()=>now);w.init();await w.tick();
    session(h,first,'still-active','queued');first.status='blocked';now+=WATCHDOG_INTERVAL;await w.tick();
    expect(starts).toBe(1);expect(w.queue(tasks).find(r=>r.id===next.id)?.reason).toBe('watchdog_global_capacity');
    expect(h.sql.exec('SELECT task_id FROM intake_queue_admissions').toArray()).toEqual([]);
  }));
  it.each([{HUB_INTAKE_QUEUE_ENABLED:'false'},{HUB_INTAKE_QUEUE_ENABLED:'invalid'},{HUB_INTAKE_QUEUE_MAX_INFLIGHT:'0'}])('disables automatic starts and recovery for policy %j',settings=>fixture(async h=>{
    const tasks=[task('disabled-new'),task('disabled-held')];session(h,tasks[1],'disabled-held-session','held');let starts=0,commands=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{policy:()=>queuePolicyFrom(settings),start:async()=>{starts++;return {status:202,body:{}}},command:async()=>{commands++;return {status:202,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(0);expect(commands).toBe(0);expect(w.status().automation.enabled).toBe(false);
    expect(w.queue(tasks).find(r=>r.id==='disabled-new')?.reason).toMatch(/^watchdog_(disabled|configuration_invalid)$/);
  }));
  it('picks up a brand-new AC without a ping or engine override; concurrent ticks create one intent',()=>fixture(async h=>{
    const t=task(), inputs:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async input=>{inputs.push(input);session(h,t,'new','queued');return {status:202,body:{id:'new',state:'queued'}}}},()=>NOW);w.init();
    await Promise.all([w.tick('alarm'),w.tick('alarm')]);
    expect(inputs).toHaveLength(1);expect(inputs[0]).not.toHaveProperty('engine');expect(inputs[0]).toMatchObject({workspace:'demo',intake_task_id:t.id,with_goal:true});
    expect(w.queue([t])[0]).toMatchObject({state:'queued',queue_position:1});expect(t.status).toBe('open');
  }));
  it('restarts a proven failure and caps repeated failures with needs_attention and an intake note',()=>fixture(async h=>{
    const t=task();let now=NOW,starts=0;const notes:string[]=[],alerts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      start:async()=>{const id='attempt-'+(++starts);session(h,t,id,'failed');return {status:202,body:{id,state:'failed'}}},
      note:async(_t,text)=>{notes.push(text);return true},notify:async(...args)=>{alerts.push(args)}
    },()=>now);w.init();
    for(let i=0;i<QUEUE_RETRY_LIMIT+2;i++){await w.tick();now+=QUEUE_RETRY_MAX_MS;}
    expect(starts).toBe(QUEUE_RETRY_LIMIT);expect(w.queue([t])[0].state).toBe('escalated');
    expect(notes.some(n=>n.startsWith('needs_attention:'))).toBe(true);expect(alerts.some(a=>a[0]==='intake_queue')).toBe(true);expect(t.status).toBe('open');
  }));
  it('keeps a held intent and retries canonical admission without duplicate launches',()=>fixture(async h=>{
    const t=task();session(h,t,'held','held');let now=NOW,starts=0,retries=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:500,body:{}}},command:async(id,action)=>{expect(id).toBe('held');expect(action).toBe('retry');retries++;return {status:409,body:{state:'held'}}}},()=>now);w.init();
    for(let i=0;i<5;i++){await w.tick();now+=QUEUE_RETRY_MAX_MS;}
    expect(starts).toBe(0);expect(retries).toBe(QUEUE_RETRY_LIMIT);expect(w.queue([t])[0].state).toBe('escalated');
  }));
  it.each(['running','starting','offered','uncertain'])('never replaces a live bound %s worker until the stop has confirmed native exit',state=>fixture(async h=>{
    const t=task();session(h,t,'stalled',state,true);let now=NOW,starts=0,stops=0;
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,last_activity_at,created_at_ms) VALUES('queue-host','native','native','running',?,'demo',?,?)",new Date(now).toISOString(),new Date(now-QUEUE_STALL_MS-1).toISOString(),NOW-3600000);
    h.sql.exec("UPDATE fleet_sessions SET agent_state='idle',progress_json=?",JSON.stringify({last_progress_at:new Date(now-QUEUE_STALL_MS-1).toISOString()}));
    const fresh=()=>h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(now).toISOString());fresh();
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{observe:async()=>({status:200,body:{stalled:state==='running',stall_reason:'no_native_progress'}}),command:async(_id,action)=>{expect(action).toBe('stop');stops++;return {status:202,body:{}}},start:async()=>{starts++;return {status:202,body:{id:'replacement',state:'queued'}}}},()=>now);w.init();
    await w.tick();expect(stops).toBe(1);expect(starts).toBe(0);
    now+=WATCHDOG_INTERVAL;fresh();await w.tick();expect(starts).toBe(0);
    h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id='stalled'");h.sql.exec("UPDATE fleet_sessions SET phase='exited'");
    now+=QUEUE_RETRY_MAX_MS;fresh();await w.tick();expect(starts).toBe(1);
  }));
  it('shows a terminal failure but stops its live native process before replacement',()=>fixture(async h=>{
    const t=task();session(h,t,'terminal','running',true);let starts=0,stops=0;
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,engine,progress_json) VALUES('queue-host','native','native','running',?,'demo',?,'codex',?)",new Date(NOW).toISOString(),NOW-3600000,JSON.stringify({headless:false,terminal_error:'codex_reconnect_failed',last_progress_at:new Date(NOW).toISOString()}));
    h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      command:async(_id,action)=>{expect(action).toBe('stop');stops++;return {status:202,body:{}}},
      start:async()=>{starts++;return {status:202,body:{}}}
    },()=>NOW);w.init();
    expect(w.project([t])[0]).toMatchObject({ready:false,last_session:{state:'failed',lifecycle_state:'running'}});
    await w.tick();expect(stops).toBe(1);expect(starts).toBe(0);
    expect(w.queue([t])[0]).toMatchObject({state:'needs_attention',reason:'codex_reconnect_failed'});
  }));
  it.each(['waiting','human','working','failed-working','unsampled','bad-sample','host-healthy'])('does not stop a protected or unproven %s session',kind=>fixture(async h=>{
    const t=task();session(h,t,'protected','running',true);let commands=0,starts=0;
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,engine,agent_state,progress_json) VALUES('queue-host','native','native','running',?,'demo',?,'codex',?,?)",new Date(NOW).toISOString(),NOW-3600000,kind.includes('working')?'working':'idle',kind==='unsampled'?null:JSON.stringify({last_progress_at:kind==='bad-sample'?'invalid':new Date(NOW-QUEUE_STALL_MS-1).toISOString()}));
    h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(NOW).toISOString());
    if(kind==='failed-working')h.sql.exec("UPDATE session_control_sessions SET state='failed'");
    if(kind==='human')h.sql.exec("UPDATE session_control_sessions SET actor='human:alexey'");
    if(kind==='waiting')h.sql.exec("INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,intake_task_id,created_at,updated_at) VALUES('protected-wake','protected','owner','pause',?,?,'resume','pending',?,?,?)",NOW+3600000,new Date(NOW+3600000).toISOString(),t.id,new Date(NOW).toISOString(),new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      observe:async()=>({status:200,body:{stalled:kind!=='host-healthy',stall_reason:'no_native_progress'}}),
      command:async()=>{commands++;return {status:202,body:{}}},start:async()=>{starts++;return {status:202,body:{}}}
    },()=>NOW);w.init();await w.tick();expect(commands).toBe(0);expect(starts).toBe(0);
  }));
  it.each(['offered','starting'])('keeps %s work queued until canonical running',state=>fixture(async h=>{
    const t=task();session(h,t,'not-running',state);
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{},()=>NOW);w.init();await w.tick();
    expect(w.queue([t])[0]).toMatchObject({state:'queued',reason:'canonical_'+state,queue_position:1});
    expect(w.queue([t])[0].history.some((e:any)=>e.state==='running'||e.state==='restarted')).toBe(false);
  }));
  it.each(['unbound','unobserved'])('caps read-only %s launch checks without stopping or replacing unknown work',kind=>fixture(async h=>{
    const t=task();session(h,t,'gap','uncertain',kind==='unobserved');let now=NOW+QUEUE_STALL_MS+1,stops=0,starts=0,notes=0,probes=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{
      observe:async()=>{probes++;return {status:200,body:{state:'uncertain'}}},
      command:async()=>{stops++;return {status:202,body:{}}},start:async()=>{starts++;return {status:202,body:{}}},note:async()=>{notes++;return true}
    },()=>now);w.init();
    for(let i=0;i<4;i++){await w.tick();now+=QUEUE_RETRY_MAX_MS;}
    expect(probes).toBe(4);expect(stops).toBe(0);expect(starts).toBe(0);expect(notes).toBe(1);
    expect(w.queue([t])[0]).toMatchObject({state:'escalated',attempts:3,reason:'launch_observation_retry_limit'});
  }));
  it.each(['blocked','pending_approval'])('keeps %s intakes visible without automatic admission',status=>fixture(async h=>{
    const t={...task(),status};let starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(starts).toBe(0);expect(w.queue([t])[0]).toMatchObject({state:'needs_attention',reason:'intake_'+status});
  }));
  it('never retries an unbound held session owned by a human',()=>fixture(async h=>{
    const t=task();session(h,t,'manual-held','held');h.sql.exec("UPDATE session_control_sessions SET actor='human:alexey'");let commands=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{command:async()=>{commands++;return {status:202,body:{}}}},()=>NOW);w.init();await w.tick();
    expect(commands).toBe(0);expect(w.queue([t])[0]).toMatchObject({state:'queued',reason:'canonical_held'});
  }));
  it('backs off transient admission failures instead of exhausting a short outage',()=>fixture(async h=>{
    const t=task();let now=NOW,starts=0;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:503,body:{error:'unavailable'}}}},()=>now);w.init();await w.tick();
    expect(h.sql.exec('SELECT attempts,next_at FROM intake_restart_queue').one()).toEqual({attempts:1,next_at:NOW+15*60000});
    now+=14*60000;await w.tick();expect(starts).toBe(1);
    now+=60000;await w.tick();expect(starts).toBe(2);
    expect(h.sql.exec('SELECT attempts,next_at FROM intake_restart_queue').one()).toEqual({attempts:2,next_at:now+30*60000});
  }));
  it('deduplicates escalation delivery and recovers when a new canonical worker is healthy',()=>fixture(async h=>{
    const t=task();let now=NOW,notes=0;const alerts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{note:async()=>{notes++;return true},notify:async(...a)=>{alerts.push(a)}},()=>now);w.init();
    h.sql.exec("INSERT INTO intake_restart_queue(task_id,state,reason,session_id,attempts,next_at,updated_at) VALUES(?,'escalated','restart_retry_limit','old',3,?,?)",t.id,now,now);
    await w.tick();now+=QUEUE_RETRY_MAX_MS;await w.tick();
    expect(notes).toBe(1);expect(alerts.filter(a=>a[0]==='intake_queue'&&a[2]!==null)).toHaveLength(1);
    session(h,t,'repaired','running',true);
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace,created_at_ms,last_activity_at) VALUES('queue-host','native','native','running',?,'demo',?,?)",new Date(now).toISOString(),NOW-3600000,new Date(now).toISOString());
    h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(now).toISOString());
    now+=WATCHDOG_INTERVAL;await w.tick();expect(w.queue([t])[0]).toMatchObject({state:'running',attempts:0});
    expect(alerts.filter(a=>a[0]==='intake_queue'&&a[2]===null)).toHaveLength(1);
  }));
  it('picks up an intake created while recovery awaits without using the old task snapshot',()=>fixture(async h=>{
    const held=task('held-task'), added=task('arrived-during-recovery');const tasks=[held], inputs:any[]=[];let now=NOW;
    session(h,held,'held-session','held');
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[...tasks]} as any,(f:any)=>f(),{
      policy:()=>queuePolicyFrom({HUB_INTAKE_QUEUE_MAX_PER_PROJECT:'2'}),
      command:async()=>{await Promise.resolve();tasks.push(added);return {status:409,body:{state:'held'}}},
      start:async input=>{inputs.push(input);session(h,added,'new-arrival','queued');return {status:202,body:{id:'new-arrival',state:'queued'}}}
    },()=>now);w.init();
    await expect(w.tick()).resolves.toMatchObject({state:'checked'});
    expect(inputs).toHaveLength(0);now+=WATCHDOG_INTERVAL;await w.tick();
    expect(inputs).toHaveLength(1);expect(inputs[0].intake_task_id).toBe(added.id);
  }));
  it('does not infer death from a failed canonical row while native worker is still live',()=>fixture(h=>{
    const t=task();session(h,t,'failed-but-bound','failed',true);
    h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('queue-host','native','native','running',?,'demo')",new Date(NOW).toISOString());
    h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(NOW).toISOString());
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();expect(w.project([t])[0].ready).toBe(false);
  }));
  it('preserves queue movement timestamps on repeated polling and highlights stuck work',()=>fixture(async h=>{
    const t=task();let now=NOW;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{},()=>now);w.init();await w.tick();
    const first=w.queue([t])[0];now+=QUEUE_STALL_MS+1;await w.tick();const later=w.queue([t])[0];
    expect(later.last_movement_at).toBe(first.last_movement_at);expect(later.stuck).toBe(true);expect(later.history).toHaveLength(first.history.length);
  }));
  it('reports completion evidence honestly and never treats an exit or bare DONE as proof',()=>fixture(h=>{
    const bare=task('bare'),proof=task('proof'),exit=task('exit');bare.history=[{kind:'note',text:'DONE'}];
    proof.history=[{kind:'note',text:'DONE commit abcdef1\nlive check: hub status\nresult: all AC confirmed'}];session(h,exit,'exited','stopped');
    const w=new IntakeWatchdog(h.sql,h.intakeService,(f:any)=>f(),{},()=>NOW);w.init();
    const rows=w.queue([bare,proof,exit]);expect(rows.find(r=>r.id==='bare')?.state).toBe('needs_attention');expect(rows.find(r=>r.id==='proof')).toMatchObject({state:'done',completion_evidence:'reported_live_proof_not_independently_verified'});expect(rows.find(r=>r.id==='exit')?.state).toBe('queued');
  }));
  it('bounds recovery work per alarm and gives untouched due items the leading queue positions',()=>fixture(async h=>{
    const tasks=Array.from({length:10},(_,i)=>task('fair-'+i));let now=NOW;const retried:string[]=[];
    for(const t of tasks)session(h,t,t.id+'-held','held');
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>tasks} as any,(f:any)=>f(),{command:async(id)=>{retried.push(id);return {status:409,body:{state:'held'}}}},()=>now);w.init();
    await w.tick();expect(retried).toHaveLength(8);
    const queue=w.queue(tasks);expect(queue[0]).toMatchObject({id:'fair-8',queue_position:1});expect(queue[1]).toMatchObject({id:'fair-9',queue_position:2});
    now+=WATCHDOG_INTERVAL;await w.tick();expect(retried).toHaveLength(10);expect(new Set(retried).size).toBe(10);
    expect(w.queue(tasks).flatMap(t=>t.history).some(e=>e.state==='restarted')).toBe(false);
  }));
  it('records restarted only after the replacement actually reaches running',()=>fixture(async h=>{
    const t=task();session(h,t,'failed','failed');h.sql.exec("UPDATE session_control_sessions SET created_at=? WHERE id='failed'",new Date(NOW-1000).toISOString());let now=NOW;
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{session(h,t,'replacement','queued');return {status:202,body:{id:'replacement',state:'queued'}}}},()=>now);w.init();
    await w.tick();expect(w.queue([t])[0].history.some((e:any)=>e.state==='restarted')).toBe(false);
    h.sql.exec("UPDATE session_control_sessions SET state='running' WHERE id='replacement'");now+=WATCHDOG_INTERVAL;await w.tick();
    expect(w.queue([t])[0].history.filter((e:any)=>e.state==='restarted')).toHaveLength(1);
    now+=WATCHDOG_INTERVAL;await w.tick();expect(w.queue([t])[0].history.filter((e:any)=>e.state==='restarted')).toHaveLength(1);
  }));
  it('fails closed when one intake exceeds the bounded observation budget',()=>fixture(async h=>{
    const t=task();for(let i=0;i<513;i++)session(h,t,'overflow-'+i,'failed');let starts=0,now=NOW;const alerts:any[]=[];
    const w=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[t]} as any,(f:any)=>f(),{start:async()=>{starts++;return {status:202,body:{}}},notify:async(...args)=>{alerts.push(args)}},()=>now);w.init();
    expect(w.project([t])[0]).toMatchObject({ready:false,state:'unknown',reason:'observation_budget_exceeded'});
    expect(await w.tick()).toMatchObject({observation_limited:true});expect(starts).toBe(0);
    expect(w.status()).toMatchObject({observation_limited:true,observation_reason:'observation_budget_exceeded',overflow_tick_count:1,checked_batch_count:1});
    now+=WATCHDOG_INTERVAL;await w.tick();expect(w.status().overflow_tick_count).toBe(2);
    expect(alerts.filter(a=>a[0]==='intake_queue_budget')).toHaveLength(1);
  }));
  it('paginates authorized queue reads with bounded ordinal cursors',()=>fixture(async h=>{
    h.intakeWatchdog.init();
    h.sql.exec('DELETE FROM intake_tasks');
    for(let i=0;i<65;i++){const t=task('page-'+i);h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',t.id,JSON.stringify(t));}
    const auth=async()=>({caller:{isAdmin:true,actor:'queue-reader',kind:'human'},scopes:['admin']});
    const response=await h.intakeService.handle(new Request('https://hub.test/api/intake/queue'),auth);const first=await response.json();expect(response.status,JSON.stringify(first)).toBe(200);
    expect(first.items).toHaveLength(64);expect(first.next_cursor).toMatch(/^\d+$/);expect(first.queue_position_scope).toBe('authorized_page');
    const next=await(await h.intakeService.handle(new Request('https://hub.test/api/intake/queue?cursor='+first.next_cursor),auth)).json();
    expect(next.items).toHaveLength(1);expect(next.next_cursor).toBe(null);
    expect(new Set([...first.items,...next.items].map((t:any)=>t.id)).size).toBe(65);
    expect((await h.intakeService.handle(new Request('https://hub.test/api/intake/queue?limit=10000'),auth)).status).toBe(400);
  }));
  it('bounds production-size tick reads and rotates through every intake',()=>fixture(async h=>{
    h.sql.exec('DELETE FROM intake_tasks');let now=NOW;
    const intake=h.intakeService;
    h.ctx.storage.transactionSync(()=>{
      for(let i=0;i<764;i++){const t=task('budget-'+String(i).padStart(4,'0'));h.sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',t.id,JSON.stringify(t));}
      for(let i=0;i<5000;i++)h.sql.exec("INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,machine_id,binding_json,created_at,updated_at) VALUES(?,'retained',?,?,'codex','headless','stopped','queue-host',?,?,?)",'retained-'+i,'retained-'+i,JSON.stringify({intake_task_id:i<2292?'budget-'+String(Math.floor(i/3)).padStart(4,'0'):'closed-'+i,brief:'retained history '.repeat(40)}),i<2000?JSON.stringify({native_id:'retained-native-'+i}):null,new Date(NOW).toISOString(),new Date(NOW).toISOString());
      for(let i=0;i<2000;i++)h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('queue-host',?,?,'exited',?,'other')",'retained-native-'+i,'retained-'+i,new Date(NOW).toISOString());
      for(let i=0;i<3000;i++)h.sql.exec("INSERT INTO claims(id,project,scope,holder,created_at,expires_at,work_key,claim_source) VALUES(?,'other','@work/task','owner',?,?,?,'native')",'retained-claim-'+i,NOW,NOW+3600000,'task:closed-'+i);
    });
    h.sql.exec("INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES('queue-host',?)",new Date(NOW).toISOString());
    const plan=h.sql.exec("EXPLAIN QUERY PLAN SELECT t.data FROM intake_task_projection p JOIN intake_tasks t ON t.id=p.id WHERE p.id>? AND json_extract(p.data,'$.status') IN ('open','in_progress','blocked','pending_approval') AND json_extract(p.data,'$.archived_at') IS NULL ORDER BY p.id LIMIT 64",'').toArray();
    expect(JSON.stringify(plan)).toContain('intake_watchdog_open');
    const w=new IntakeWatchdog(h.sql,intake,(f:any)=>f(),{notify:async()=>false},()=>now);w.init();
    const measured=await w.tick();if(!('elapsed_ms' in measured))throw new Error('tick did not execute');
    expect(measured).toMatchObject({state:'checked',open_count:64,batch_limit:64,observed_sessions:192,observed_native:192,observation_limited:false});expect(measured.elapsed_ms).toBeLessThan(250);
    for(let i=0;i<12;i++){now+=WATCHDOG_INTERVAL;await w.tick();}
    expect(h.sql.exec('SELECT COUNT(DISTINCT task_id) AS n FROM intake_queue_movement').one().n).toBe(764);
    return measured;
  }).then((result:any)=>{console.log('QUEUE_BUDGET_TICK',JSON.stringify(result))}),30000);
  it('refuses unauthenticated queue reads',async()=>{const r=await SELF.fetch('https://hub.test/api/intake/queue');expect([401,403]).toContain(r.status)});
});
