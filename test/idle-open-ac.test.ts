import { describe, expect, it } from 'vitest';
import { insertSessionObservation, observedHubTagMatches } from '../src/session-observation';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, adoptObservedNativeSessions } from '../src/session_control';
import { historyReferencesSession, linkedIntake, openWorkPreventsIdleReap } from '../src/intake-session';
import { recordIntakeSessionLink } from '../src/intake';
import { queueOperatorAdoptions, pendingOperatorAdoptions, completeOperatorAdoption } from '../src/session-adoption';
import { IntakeWorktrees } from '../src/intake-worktrees';
import { ResourceManagement } from '../src/resource-management';
import { IdleWorkService } from '../src/idle-work';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const id = '11111111-1111-4111-8111-111111111111';
const taskId = '22222222-2222-4222-8222-222222222222';
const at = Date.parse('2026-10-27T07:00:00Z');
const auth = { caller: { isAdmin: true, kind: 'human', actor: 'operator' }, scopes: ['admin'] };
async function fixture(fn: (f: any) => Promise<void>) {
  const b = env as unknown as Env;
  await runInDurableObject(b.HUB.get(b.HUB.idFromName('idle-ac-' + crypto.randomUUID())), async (hub: Hub, state: DurableObjectState) => {
    const sql = hub.sql, clock = { now: at }, tx = <T>(f: () => T) => state.storage.transactionSync(f);
    const sc = new SessionControlService(sql, tx, () => clock.now); sc.init();
    const task = { id: taskId, project: 'agent-hub', title: 'test work', status: 'open', version: 1,
      acceptance_criteria: 'AC1 prove work\nAC2 retain evidence', history: [{kind:'note',text:'Last operator note'}], created_at: at, updated_at: at };
    sql.exec('INSERT INTO intake_tasks VALUES(?,?)', taskId, JSON.stringify(task));
    sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('idle-host','idle-host','idle-host',?,?)", new Date(at).toISOString(), new Date(at).toISOString());
    sql.exec('INSERT INTO fleet_catalog_seen(machine_id,observed_at) VALUES(?,?)', 'idle-host', new Date(at).toISOString());
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES(?,'operator','test',?,'idle-host','codex','interactive','running',?,?,?)`, id,
      JSON.stringify({ intake_task_id: taskId, workspace: '/test', with_goal: true }),
      JSON.stringify({ native_id: id, tag: 'test-goal', workspace: '/test', native_created_at_ms: at - 3600000 }),
      new Date(at - 3600000).toISOString(), new Date(at).toISOString());
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,last_activity_at,created_at_ms,progress_json)
      VALUES('idle-host',?,'test-goal','codex','running','idle',?,'/test',?,?,?)`, id, new Date(at).toISOString(),
      new Date(at - 3600000).toISOString(), at - 3600000,
      JSON.stringify({ last_progress_at: new Date(at - 3600000).toISOString(), goal_status: 'blocked' }));
    const view = async () => await (await sc.handle(new Request('https://hub/api/sessions/' + id), auth)).json() as any;
    const row = () => sql.exec('SELECT * FROM session_control_sessions WHERE id=?', id).toArray()[0];
    await fn({ sql, clock, tx, sc, task, view, row });
  });
}

describe('idle with open acceptance criteria', () => {
  it('recognizes the 95c1862c START-note shape, not RCA references or tag substrings', () => {
    const binding = { native_id: id, tag: 'test-goal' };
    const text = `START: Started via aplexer BACKUP: interactive Codex session hetzner:/test:test-goal (aplexer id ${id}, engine codex default GPT-6.1-Sol), /goal sent and active ('Pursuing goal').`;
    expect(historyReferencesSession({history:[{kind:'note',text}]}, {id:'canonical'}, binding)).toBe(true);
    expect(historyReferencesSession({history:[{kind:'note',text:'RCA: ' + id}]}, {id:'canonical'}, binding)).toBe(false);
    expect(historyReferencesSession({history:[{kind:'note',text:'START: test-goal-extra'}]}, {}, binding)).toBe(false);
  });
  it('publishes a census identity with no owner or lock and resolves only visible unambiguous tag aliases', async () => fixture(async ({sql,sc}: any) => {
    const native = {native_id:'55555555-5555-4555-8555-555555555555',
      tag:'hub-988b8d49-cacf-4a5e-900c-a214a6402808',workspace:'/test-native',engine:'zcodex',
      model:'glm-5.3-flash',created_at_ms:at-1000,owner:'operator',worktree_locked:true};
    insertSessionObservation(sql,'idle-host',native,'running','host-family',at);
    const observation = sql.exec('SELECT * FROM session_control_sessions WHERE id=?',native.native_id).toArray()[0];
    expect(observation).toMatchObject({actor:'unknown',owner_token_id:null,owner_family:null,machine_id:'idle-host',state:'running'});
    expect(JSON.parse(observation.request_json)).toMatchObject({observation_only:true,model:'glm-5.3-flash'});
    expect(JSON.parse(observation.binding_json).adoption_lock).toBeUndefined();
    expect(observedHubTagMatches(sql,'988b8d49',()=>true).map(r=>r.id)).toEqual([native.native_id]);
    expect(observedHubTagMatches(sql,'988b8d49',()=>false)).toEqual([]);
    for (const ref of ['988b8d49','988b8d49-cacf-4a5e-900c-a214a6402808']) {
      const result = await sc.handle(new Request('https://hub/api/sessions/' + ref), auth);
      expect(result.status).toBe(200);
      expect(await result.json()).toMatchObject({id:native.native_id,model:'glm-5.3-flash'});
    }
    insertSessionObservation(sql,'idle-host',{...native,native_id:'66666666-6666-4666-8666-666666666666'},'running','host-family',at);
    expect(observedHubTagMatches(sql,'988b8d49',()=>true)).toHaveLength(2);
    expect((await sc.handle(new Request('https://hub/api/sessions/988b8d49'),auth)).status).toBe(409);
  }));
  it('reconciles exact census incarnations and ignores foreign or replaced identities', async () => fixture(async ({sql}: any) => {
    const native = {native_id:'77777777-7777-4777-8777-777777777777',tag:'manual-worker',workspace:'/manual',engine:'zcodex',created_at_ms:at-1000,phase:'running'};
    expect(adoptObservedNativeSessions(sql,'idle-host',[native],at).adopted).toBe(1);
    const read=()=>sql.exec('SELECT state FROM session_control_sessions WHERE id=?',native.native_id).toArray()[0].state;
    for (const [machine,change] of [['foreign-host',{}],['idle-host',{created_at_ms:at}],['idle-host',{workspace:'/elsewhere'}]] as const) {
      adoptObservedNativeSessions(sql,machine,[{...native,...change,phase:'exited',confirmed_exit:true}],at+1000);
      expect(read()).toBe('running');
    }
    adoptObservedNativeSessions(sql,'idle-host',[{...native,phase:'exited',confirmed_exit:false}],at+1000);
    expect(read()).toBe('uncertain');
    adoptObservedNativeSessions(sql,'idle-host',[{...native,phase:'exited',confirmed_exit:true}],at+2000);
    expect(read()).toBe('stopped');
    adoptObservedNativeSessions(sql,'idle-host',[native],at+3000);
    expect(read()).toBe('stopped');
  }));
  it('links late START notes and exact registered workspaces in one census history scan', async () => fixture(async ({sql,task}: any) => {
    new IntakeWorktrees(sql,()=>at).init();
    const natives = ['started','registered','unrelated'].map(tag=>({native_id:crypto.randomUUID(),tag,workspace:'/'+tag,engine:'codex',created_at_ms:at-1000,phase:'running'}));
    task.history.push({kind:'note',text:`START: native ${natives[0].native_id}`},
      {kind:'note',text:`RCA: reference ${natives[2].native_id}`});
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(task),taskId);
    sql.exec(`INSERT INTO intake_worktrees(id,task_id,machine,path,created_at,updated_at,origin,state)
      VALUES(?,?,'idle-host','/registered',?,?,'operator','active')`,crypto.randomUUID(),taskId,at,at);
    const reads: string[]=[];
    const counted={exec(query:string,...args:any[]){reads.push(query);return sql.exec(query,...args);}};
    expect(adoptObservedNativeSessions(counted as any,'idle-host',natives,at).adopted).toBe(3);
    const intake=(native:any)=>JSON.parse(sql.exec('SELECT request_json FROM session_control_sessions WHERE id=?',native.native_id).one().request_json).intake_task_id;
    expect(intake(natives[0])).toBe(taskId);
    expect(intake(natives[1])).toBe(taskId);
    expect(intake(natives[2])).toBeUndefined();
    expect(reads.filter(q=>q.includes('AS events FROM intake_tasks WHERE id>'))).toHaveLength(1);
    reads.length = 0;
    adoptObservedNativeSessions(counted as any,'idle-host',natives,at+1000);
    expect(reads.some(q=>q.includes('FROM intake_tasks'))).toBe(false);
  }));
  it('backfills large ledgers in bounded pages and observes late START notes without native changes', async () => fixture(async ({sql,task}: any) => {
    const native={native_id:crypto.randomUUID(),tag:'late-manual',workspace:'/late-manual',engine:'codex',created_at_ms:at-1000,phase:'running'};
    for (let n=0;n<120;n++) {
      const otherId=`history-${String(n).padStart(3,'0')}`;
      sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',otherId,JSON.stringify({...task,id:otherId,history:[]}));
    }
    const reads:string[]=[];
    const counted={exec(query:string,...args:any[]){reads.push(query);return sql.exec(query,...args);}};
    adoptObservedNativeSessions(counted as any,'idle-host',[native],at);
    expect(sql.exec('SELECT count(*) AS n FROM session_observation_intakes').one().n).toBe(100);
    expect(sql.exec('SELECT complete FROM session_observation_intake_meta').one().complete).toBe(0);
    adoptObservedNativeSessions(counted as any,'idle-host',[native],at+1000);
    expect(sql.exec('SELECT complete FROM session_observation_intake_meta').one().complete).toBe(1);
    expect(reads.filter(q=>q.includes('AS events FROM intake_tasks WHERE id>'))).toHaveLength(2);
    const revision=sql.exec('SELECT updated_at FROM session_control_sessions WHERE id=?',native.native_id).one().updated_at;
    task.history.push({kind:'note',text:`START: native ${native.native_id}`});
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(task),taskId);
    adoptObservedNativeSessions(counted as any,'idle-host',[native],at+2000);
    const row=sql.exec('SELECT request_json,updated_at FROM session_control_sessions WHERE id=?',native.native_id).one();
    expect(JSON.parse(row.request_json).intake_task_id).toBe(taskId);
    expect(row.updated_at).toBe(revision);
    reads.length=0;
    adoptObservedNativeSessions(counted as any,'idle-host',[native],at+3000);
    expect(reads.some(q=>q.includes('FROM intake_tasks'))).toBe(false);
  }));
  it('keeps ambiguous intake evidence and foreign-host native links unassigned', async () => fixture(async ({sql,task}: any) => {
    const native={native_id:crypto.randomUUID(),tag:'ambiguous',workspace:'/ambiguous',engine:'codex',created_at_ms:at-1000,phase:'running'};
    const otherId=crypto.randomUUID();
    for (const target of [taskId,otherId]) {
      const body={...task,id:target,history:[{kind:'note',text:`START: native ${native.native_id}`}]};
      sql.exec('INSERT OR REPLACE INTO intake_tasks(id,data) VALUES(?,?)',target,JSON.stringify(body));
    }
    adoptObservedNativeSessions(sql,'idle-host',[native],at);
    const row=()=>sql.exec('SELECT * FROM session_control_sessions WHERE id=?',native.native_id).one();
    expect(JSON.parse(row().request_json).intake_task_id).toBeUndefined();
    sql.exec('DELETE FROM intake_tasks WHERE id=?',otherId);
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify({...task,history:[{kind:'link',native_id:native.native_id,host:'foreign-host'}]}),taskId);
    adoptObservedNativeSessions(sql,'idle-host',[native],at+1000);
    expect(JSON.parse(row().request_json).intake_task_id).toBeUndefined();
  }));
  it('upgrades a visibility-only census row only after an exact operator host receipt', async () => fixture(async ({sql,row}: any) => {
    const current = row();
    const binding = { ...JSON.parse(current.binding_json), provenance: 'observed_native' };
    sql.exec("UPDATE session_control_sessions SET actor='unknown',request_json=?,binding_json=? WHERE id=?",
      JSON.stringify({provenance:'observed_native',observation_only:true,workspace:'/test'}), JSON.stringify(binding), id);
    const native = {native_id:id,tag:binding.tag,workspace:binding.workspace,engine:'codex',created_at_ms:binding.native_created_at_ms};
    expect(queueOperatorAdoptions(sql,'idle-host',[native],'operator',null,at).queued).toBe(1);
    expect(row().actor).toBe('unknown');
    const adoption = pendingOperatorAdoptions(sql,'idle-host',at)[0];
    const receipt = {id:adoption.id,native,lock_verified:true,worktree:false};
    expect(completeOperatorAdoption(sql,'idle-host',{...receipt,lock_verified:false},at,'host-family').ok).toBe(false);
    expect(completeOperatorAdoption(sql,'idle-host',{...receipt,native:{...native,created_at_ms:1}},at,'host-family').ok).toBe(false);
    expect(row().actor).toBe('unknown');
    expect(completeOperatorAdoption(sql,'idle-host',receipt,at,'host-family').ok).toBe(true);
    expect(row().actor).toBe('operator');
    expect(JSON.parse(row().request_json).observation_only).toBe(false);
    expect(JSON.parse(row().binding_json).adoption_intent_id).toBe(adoption.id);
    expect(completeOperatorAdoption(sql,'idle-host',receipt,at,'host-family')).toMatchObject({ok:true,replay:true});
  }));
  it('persists late START-note linkage for an adopted native session', async () => fixture(async ({sql,task}: any) => {
    const nativeId='44444444-4444-4444-8444-444444444444';
    const native={native_id:nativeId,tag:'backup-goal',workspace:'/backup',engine:'codex',phase:'running',created_at_ms:at-10000};
    // Visibility comes first; explicit ownership still requires the host receipt.
    expect(adoptObservedNativeSessions(sql,'idle-host',[native],at,'host-family').adopted).toBe(1);
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,workspace,engine,phase,observed_at,created_at_ms)
      VALUES('idle-host',?,'backup-goal','/backup','codex','running',?,?)`,nativeId,new Date(at).toISOString(),native.created_at_ms);
    expect(queueOperatorAdoptions(sql,'idle-host',[native],'operator',null,at).queued).toBe(1);
    const adoption=pendingOperatorAdoptions(sql,'idle-host',at)[0];
    expect(completeOperatorAdoption(sql,'idle-host',{id:adoption.id,native,lock_verified:true,worktree:false},at,'host-family').ok).toBe(true);
    let row=sql.exec('SELECT * FROM session_control_sessions WHERE id=?',nativeId).toArray()[0];
    expect(JSON.parse(row.request_json).intake_task_id).toBeFalsy();
    task.history.push({kind:'note',text:`START: via aplexer BACKUP: hetzner:/backup:backup-goal (aplexer id ${nativeId}, engine codex), /goal sent and active.`});
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(task),taskId);
    adoptObservedNativeSessions(sql,'idle-host',[native],at+1000,'host-family');
    row=sql.exec('SELECT * FROM session_control_sessions WHERE id=?',nativeId).toArray()[0];
    expect(JSON.parse(row.request_json).intake_task_id).toBe(taskId);
    const saved=JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?',taskId).toArray()[0].data);
    expect(saved.history.filter((e:any)=>e.kind==='link'&&e.hub_session_id===nativeId)).toHaveLength(1);
    adoptObservedNativeSessions(sql,'idle-host',[native],at+2000,'host-family');
    expect(JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?',taskId).toArray()[0].data).history.length).toBe(saved.history.length);
  }));
  it('projects distinct open-work state with attention identity and progress', async () => fixture(async ({sc,view}: any) => {
    expect(await view()).toMatchObject({state:'idle_with_open_ac',intake_id:taskId,lifecycle_state:'running',goal_status:'blocked'});
    expect(sc.attention()[0]).toMatchObject({session_id:id,intake_id:taskId,idle_for:3600,last_nudge:null});
  }));
  it('skips retained intake bodies and full warning projections for healthy attention reads', async () => fixture(async ({sql,tx}: any) => {
    sql.exec("UPDATE fleet_sessions SET progress_json=?",JSON.stringify({last_progress_at:new Date(at).toISOString(),goal_status:'active'}));
    const reads: string[] = [];
    const counted = { exec(query: string, ...args: any[]) { reads.push(query); return sql.exec(query,...args); } };
    const sc = new SessionControlService(counted as any,tx,()=>at);
    expect(sc.attention()).toEqual([]);
    expect(reads.some(q=>q.includes('SELECT data FROM intake_tasks'))).toBe(false);
    expect(reads.some(q=>q.includes('session_warning_events') || q.includes('launch_error_json'))).toBe(false);
  }));
  it('does not call finished work idle_with_open_ac; DONE permits idle reap', async () => fixture(async ({sql,task,row,view}: any) => {
    expect(openWorkPreventsIdleReap(sql,row())).toBe(true);
    task.history.push({kind:'note',text:'DONE: commit abc123; Live check: test; Result: pass'});
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?',JSON.stringify(task),taskId);
    expect((await view()).state).toBe('stalled');
    expect(openWorkPreventsIdleReap(sql,row())).toBe(false);
  }));
  it('protects retained history links after launch metadata is absent', async () => fixture(async ({sql,row}: any) => {
    expect(recordIntakeSessionLink(sql,row(),at)).toBe(true);
    sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE id=?",id);
    expect(linkedIntake(sql,row()).id).toBe(taskId);
    expect(openWorkPreventsIdleReap(sql,row())).toBe(true);
    expect(new ResourceManagement(sql,()=>at).sessions('idle-host')[0].idle_reap_allowed).toBe(false);
  }));
  it('protects every canonical alias of a native executor with open work', async () => fixture(async ({sql}: any) => {
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      SELECT '33333333-3333-4333-8333-333333333333',actor,'alias',request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at FROM session_control_sessions WHERE id=?`,id);
    sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE id=?",id);
    const sessions=new ResourceManagement(sql,()=>at).sessions('idle-host');
    expect(sessions.length).toBeGreaterThan(0);
    expect(sessions.every(s=>s.idle_reap_allowed===false)).toBe(true);
  }));
  it('fails protected when retained intake links are ambiguous', async () => fixture(async ({sql,task,row}: any) => {
    recordIntakeSessionLink(sql,row(),at);
    const second={...task,id:'55555555-5555-4555-8555-555555555555',history:[{kind:'link',hub_session_id:id}]};
    sql.exec('INSERT INTO intake_tasks VALUES(?,?)',second.id,JSON.stringify(second));
    sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE id=?",id);
    expect(linkedIntake(sql,row())).toBeNull();
    expect(openWorkPreventsIdleReap(sql,row())).toBe(true);
  }));
  it('exposes scheduled waiting without nudging, and classifies missed wake with fresh progress', async () => fixture(async ({sql,clock,tx,sc,view}: any) => {
    const wakeResponse = await sc.handle(new Request('https://hub/api/sessions/' + id + '/wake', {
      method:'POST', body:JSON.stringify({key:'idle-wake',at:new Date(at+3600000).toISOString(),prompt:'resume'})
    }), auth);
    expect(wakeResponse.status).toBe(201);
    expect(await view()).toMatchObject({state:'waiting_until',waiting_until:new Date(at+3600000).toISOString()});
    const idle = new IdleWorkService(sql,tx,sc,{},()=>clock.now); idle.init();
    await idle.tick(); expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(0);
    sql.exec("UPDATE session_control_wakes SET state='missed'");
    sql.exec("DELETE FROM session_stalls");
    sql.exec("UPDATE fleet_sessions SET progress_json=?",JSON.stringify({last_progress_at:new Date(at).toISOString(),goal_status:'active'}));
    expect(await view()).toMatchObject({state:'idle_with_open_ac',waiting_until:null,missed_wake:true});
  }));
  it('manual REST linkage is operator-only, persists and refuses conflicting relinks', async () => fixture(async ({sql,sc,row}: any) => {
    sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE id=?",id);
    const req = () => new Request('https://hub/api/sessions/' + id + '/link',{method:'POST',body:JSON.stringify({key:'manual-link',intake_task_id:taskId})});
    expect([403,404]).toContain((await sc.handle(req(),{caller:{kind:'agent',actor:'operator'},scopes:['fleet:launch']})).status);
    expect((await sc.handle(req(),auth)).status).toBe(200);
    expect(linkedIntake(sql,row()).id).toBe(taskId);
    expect(JSON.parse(row().request_json).intake_task_id).toBe(taskId);
  }));
  it('fresh census cannot authorize recovery with missing or cached old progress sampling', async () => fixture(async ({sql,clock,tx,sc}:any) => {
    const idle=new IdleWorkService(sql,tx,sc,{},()=>clock.now);idle.init();
    await idle.tick();
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(0);
    sql.exec("UPDATE fleet_sessions SET progress_json=json_set(progress_json,'$.sampled_at',?)",new Date(at-180001).toISOString());
    await idle.tick();
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(0);
    sql.exec("UPDATE fleet_sessions SET progress_json=json_set(progress_json,'$.sampled_at',?)",new Date(at).toISOString());
    await idle.tick();
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(1);
  }));
  it('detects one episode note; sends durable nudge with full AC and resume; rate limits', async () => fixture(async ({sql,clock,tx,sc}: any) => {
    sql.exec("UPDATE fleet_sessions SET progress_json=json_set(progress_json,'$.last_progress_at',?)",new Date(at).toISOString());
    const idle = new IdleWorkService(sql,tx,sc,{},()=>clock.now); idle.init();
    const tick = async (minutes: number) => { clock.now=at+minutes*60000; sql.exec('UPDATE fleet_catalog_seen SET observed_at=?',new Date(clock.now).toISOString()); sql.exec("UPDATE fleet_sessions SET progress_json=json_set(COALESCE(progress_json,'{}'),'$.sampled_at',?) WHERE native_id=?",new Date(clock.now).toISOString(),id); await idle.tick(); };
    await tick(0); await tick(29);
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(0);
    await tick(30); await tick(31);
    const commands=sql.exec('SELECT * FROM session_control_commands').toArray();
    expect(commands).toHaveLength(1);
    expect(JSON.parse(commands[0].payload_json).body).toContain('/goal resume');
    expect(JSON.parse(commands[0].payload_json).body).toContain('AC2 retain evidence');
    await tick(60); expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(1);
    await tick(90); expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(2);
    const task=JSON.parse(sql.exec('SELECT data FROM intake_tasks WHERE id=?',taskId).toArray()[0].data);
    expect(task.history.filter((e:any)=>e.text?.startsWith('Hub detected'))).toHaveLength(1);
  }));
  it.each([false,true])('retries an interrupted nudge next tick with the original key (legacy=%s)', async (legacy) => fixture(async ({sql,clock,tx,sc}: any) => {
    const idle = new IdleWorkService(sql,tx,sc,{},()=>clock.now); idle.init();
    const handle = sc.handle.bind(sc), keys: string[] = [];
    let interrupted = false;
    sc.handle = async (request: Request, auth: any) => {
      if (request.url.endsWith('/send')) {
        keys.push((await request.clone().json() as any).key);
        const response = await handle(request,auth);
        if (!interrupted) { interrupted=true; throw new Error('lost response after enqueue'); }
        return response;
      }
      return handle(request,auth);
    };
    const tick = async (minutes: number) => {
      clock.now=at+minutes*60000;
      sql.exec('UPDATE fleet_catalog_seen SET observed_at=?',new Date(clock.now).toISOString());
      sql.exec("UPDATE fleet_sessions SET progress_json=json_set(progress_json,'$.sampled_at',?)",new Date(clock.now).toISOString());
      await idle.tick();
    };
    await tick(0);
    const first = sql.exec('SELECT * FROM session_idle_episodes WHERE session_id=?',id).toArray()[0];
    expect(JSON.parse(first.nudges_json)[0].command_id).toBeUndefined();
    expect(typeof JSON.parse(first.nudges_json)[0].body).toBe('string');
    if (legacy) {
      const pending = JSON.parse(first.nudges_json); delete pending[0].body;
      sql.exec('UPDATE session_idle_episodes SET nudges_json=? WHERE session_id=?',JSON.stringify(pending),id);
    }
    await tick(1);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
    const episode = sql.exec('SELECT * FROM session_idle_episodes WHERE session_id=?',id).toArray()[0];
    expect(JSON.parse(episode.nudges_json)[0].command_id).toBeTruthy();
    expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(1);
    await tick(2);
    expect(keys).toHaveLength(2);
  }));
  it('replaces after two unsuccessful nudges and stops old only after fresh bound replacement runs', async () => fixture(async ({sql,clock,tx,sc}: any) => {
    sql.exec("UPDATE fleet_sessions SET progress_json=json_set(progress_json,'$.last_progress_at',?)",new Date(at).toISOString());
    const idle = new IdleWorkService(sql,tx,sc,{},()=>clock.now); idle.init();
    const tick = async (minutes: number) => { clock.now=at+minutes*60000; sql.exec('UPDATE fleet_catalog_seen SET observed_at=?',new Date(clock.now).toISOString()); sql.exec("UPDATE fleet_sessions SET progress_json=json_set(COALESCE(progress_json,'{}'),'$.sampled_at',?) WHERE native_id=?",new Date(clock.now).toISOString(),id); await idle.tick(); };
    await tick(0); await tick(30); await tick(60); await tick(90); await tick(120);
    const episode=sql.exec('SELECT * FROM session_idle_episodes WHERE session_id=?',id).toArray()[0];
    expect(episode.replacement_id).toBeTruthy();
    const old=sql.exec('SELECT * FROM session_control_sessions WHERE id=?',id).toArray()[0];
    expect(openWorkPreventsIdleReap(sql,old,clock.now)).toBe(true);
    const replacement=sql.exec('SELECT * FROM session_control_sessions WHERE id=?',episode.replacement_id).toArray()[0];
    expect(replacement.parent_id).toBe(id);
    expect(JSON.parse(replacement.request_json)).toMatchObject({intake_task_id:taskId,mode:'interactive',with_goal:true});
    expect(JSON.parse(replacement.request_json).brief).toContain('AC2 retain evidence');
    expect(sql.exec("SELECT * FROM session_control_commands WHERE kind='stop'").toArray()).toHaveLength(0);
    const nativeId='33333333-3333-4333-8333-333333333333';
    sql.exec("UPDATE session_control_sessions SET state='running',machine_id='idle-host',binding_json=? WHERE id=?",
      JSON.stringify({native_id:nativeId,tag:'replacement',workspace:'/test',generation:1,native_created_at_ms:clock.now}),replacement.id);
    sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,agent_state,observed_at,workspace,created_at_ms)
      VALUES('idle-host',?,'replacement','codex','running','working',?,'/test',?)`,nativeId,new Date(clock.now).toISOString(),clock.now);
    await tick(121);
    expect(openWorkPreventsIdleReap(sql,old,clock.now)).toBe(false);
    const stops=sql.exec("SELECT * FROM session_control_commands WHERE kind='stop'").toArray();
    expect(stops).toHaveLength(1); expect(stops[0].session_id).toBe(id);
    await tick(122); expect(sql.exec("SELECT * FROM session_control_commands WHERE kind='stop'").toArray()).toHaveLength(1);
  }));
  it('delegates managed replacement only after two shared failed nudges and skips observed owners', async () => fixture(async ({sql,clock,tx,sc}:any) => {
    const idle=new IdleWorkService(sql,tx,sc,{},()=>clock.now); idle.init();
    let mode:'skip'|'managed'='skip'; const calls:string[]=[];
    idle.setRecoveryOwner({disposition:()=>({mode}),replaceOwned:async(session,key)=>{calls.push(session+':'+key);return true;}});
    const tick=async(minutes:number)=>{clock.now=at+minutes*60000;sql.exec('UPDATE fleet_catalog_seen SET observed_at=?',new Date(clock.now).toISOString());sql.exec("UPDATE fleet_sessions SET progress_json=json_set(COALESCE(progress_json,'{}'),'$.sampled_at',?) WHERE native_id=?",new Date(clock.now).toISOString(),id);await idle.tick();};
    await tick(0); expect(sql.exec('SELECT * FROM session_control_commands').toArray()).toHaveLength(0);
    mode='managed'; await tick(1); await tick(31); expect(calls).toHaveLength(0);
    await tick(61); await tick(91); expect(calls).toHaveLength(1);
    expect(sql.exec('SELECT * FROM session_control_sessions').toArray()).toHaveLength(1);
    expect(sql.exec("SELECT * FROM session_control_commands WHERE kind='stop'").toArray()).toHaveLength(0);
  }));
});
