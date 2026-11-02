import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeWatchdog } from '../src/intake-watchdog';
import { SessionControlService } from '../src/session_control';

const NOW = Date.parse('2026-10-29T08:00:00Z');
const automatic = { caller: { isAdmin: true, actor: 'hub:service:intake-watchdog', kind: 'human' }, scopes: ['admin'] };
const operator = { caller: { isAdmin: true, actor: 'human:admin-token:fixture', kind: 'human' }, scopes: ['admin'] };
const inDO: any = runInDurableObject;

async function fixture(state: string, fn: (h: any, id: string, task: any, service: SessionControlService) => Promise<void>) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('protection-' + crypto.randomUUID()));
  await inDO(stub, async (h: any) => {
    const id = crypto.randomUUID(), native = crypto.randomUUID();
    const task = { id: crypto.randomUUID(), project: 'demo', title: 'Protected recovery', status: 'open',
      created_at: NOW - 7200000, acceptance_criteria: 'Preserve protected work', history: [] };
    const binding = { native_id: native, tag: 'worker', workspace: 'fixture', native_created_at_ms: NOW - 7200000 };
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json,hold_json)
      VALUES(?,'fixture',?,?,'codex','interactive',?,?,?,'fixture',?,?)`, id, id,
      JSON.stringify({ intake_task_id: task.id, workspace: 'fixture' }), state, new Date(NOW - 7200000).toISOString(),
      new Date(NOW - 3600000).toISOString(), state === 'running' ? JSON.stringify(binding) : null,
      state === 'held' ? JSON.stringify({ reason: 'model_unsupported' }) : null);
    if (state === 'running') {
      h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,engine,observed_at,workspace,created_at_ms,progress_json)
        VALUES('fixture',?,'worker','running','codex',?,'fixture',?,?)`, native, new Date(NOW).toISOString(),
        binding.native_created_at_ms, JSON.stringify({ last_progress_at: new Date(NOW - 7200000).toISOString() }));
      h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('fixture',?)", new Date(NOW).toISOString());
    }
    const service = new SessionControlService(h.sql, (fn: any) => fn(), () => NOW); service.init();
    await fn(h, id, task, service);
  });
}
function protect(h: any, id: string) {
  h.sql.exec(`INSERT INTO session_control_protections(session_id,actor,request_key,created_at)
    VALUES(?,'human:fixture','permanent',?)`, id, new Date(NOW).toISOString());
}
function canonicalAlias(h: any, id: string, machine?: string) {
  const row = h.sql.exec('SELECT * FROM session_control_sessions WHERE id=?',id).one();
  const alias = crypto.randomUUID();
  h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at,machine_id,binding_json)
    VALUES(?,'fixture',?,?,'codex','interactive','running',?,?,?,?)`,alias,alias,row.request_json,
    row.created_at,row.updated_at,machine ?? row.machine_id,row.binding_json);
  return alias;
}
function protectedAlias(h: any, id: string, machine?: string) {
  protect(h,canonicalAlias(h,id,machine));
}
function wakeAlias(h: any, id: string, machine?: string, due=NOW+60000) {
  const alias=canonicalAlias(h,id,machine);
  h.sql.exec("UPDATE session_control_sessions SET request_json='{}' WHERE id=?",alias);
  h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
    VALUES(?,?,'fixture','alias-wait',?,?,'continue','pending',?,?)`,crypto.randomUUID(),alias,due,new Date(due).toISOString(),new Date(NOW).toISOString(),new Date(NOW).toISOString());
}

it.each(['running', 'held'])('never automatically stops an already-protected %s session', async state => fixture(state, async (h, id, task) => {
  protect(h, id); const stops: string[] = []; let starts = 0;
  const watchdog = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => [task] } as any, (fn: any) => fn(), {
    stop: async id => { stops.push(id); }, start: async () => { starts++; return { status: 202, body: {} }; },
  }, () => NOW); watchdog.init(); await watchdog.tick();
  expect(stops).toEqual([]); expect(starts).toBe(0);
  expect(h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', id).one().state).toBe(state);
}));

it.each(['running', 'held'])('rechecks protection added while the automatic %s stop body streams', async state => fixture(state, async (h, id, _task, service) => {
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  const request = new Request('https://hub.internal/api/sessions/' + id + '/stop', {
    method: 'POST', body: new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }),
  });
  const pending = service.handle(request, automatic as any);
  protect(h, id);
  stream.enqueue(new TextEncoder().encode(JSON.stringify({ key: 'iw-stall-' + id }))); stream.close();
  const response = await pending;
  expect(response.status).toBe(409); expect(await response.json()).toEqual({ error: 'session_protected' });
  expect(h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', id).one().state).toBe(state);
  expect(h.sql.exec('SELECT id FROM session_control_commands WHERE session_id=?', id).toArray()).toEqual([]);
  expect(h.sql.exec('SELECT session_id FROM session_control_cancellations WHERE session_id=?', id).toArray()).toEqual([]);
}));

it.each(['running', 'held'])('preserves explicit operator authority to stop a protected %s session', async state => fixture(state, async (h, id, _task, service) => {
  protect(h, id);
  const response = await service.handle(new Request('https://hub.internal/api/sessions/' + id + '/stop', {
    method: 'POST', body: JSON.stringify({ key: 'explicit-operator-stop' }),
  }), operator as any);
  expect(response.status).toBe(state === 'running' ? 201 : 200);
  expect(h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', id).one().state).toBe(state === 'running' ? 'running' : 'cancelled');
}));

it('does not bypass a protected native worker through another canonical row', async () => fixture('running', async (h,id,task) => {
  protectedAlias(h,id);const stops: string[] = [];
  const watchdog = new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[task]} as any,(fn:any)=>fn(),{
    stop:async id=>{stops.push(id);},
  },()=>NOW);watchdog.init();await watchdog.tick();
  expect(stops).toEqual([]);
}));

it('rechecks a protected native alias created while the stop body streams', async () => fixture('running', async (h,id,_task,service) => {
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  const pending=service.handle(new Request('https://hub.internal/api/sessions/'+id+'/stop',{
    method:'POST',body:new ReadableStream<Uint8Array>({start(controller){stream=controller;}}),
  }),automatic as any);
  protectedAlias(h,id);
  stream.enqueue(new TextEncoder().encode(JSON.stringify({key:'automatic-alias-stop'})));stream.close();
  const response=await pending;expect(response.status).toBe(409);
  expect(await response.json()).toEqual({error:'session_protected'});
  expect(h.sql.exec('SELECT id FROM session_control_commands WHERE session_id=?',id).toArray()).toEqual([]);
}));

it('does not apply another host\'s protection to this native identity', async () => fixture('running', async (h,id,task) => {
  protectedAlias(h,id,'another-host');const stops:string[]=[];
  const watchdog=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[task]} as any,(fn:any)=>fn(),{
    stop:async id=>{stops.push(id);},
  },()=>NOW);watchdog.init();await watchdog.tick();
  expect(stops).toEqual([id]);
}));

it('honors a future wake on an unlinked canonical alias of the same worker', async () => fixture('running', async (h,id,task) => {
  wakeAlias(h,id);const stops:string[]=[];
  const watchdog=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[task]} as any,(fn:any)=>fn(),{stop:async id=>{stops.push(id);}},()=>NOW);
  watchdog.init();await watchdog.tick();expect(stops).toEqual([]);
  expect(watchdog.project([task])[0]).toMatchObject({state:'active',forgotten:false,idle_ms:null});
}));

it('rechecks an alias wake created while the automatic stop body streams', async () => fixture('running', async (h,id,_task,service) => {
  let stream!:ReadableStreamDefaultController<Uint8Array>;
  const pending=service.handle(new Request('https://hub.internal/api/sessions/'+id+'/stop',{
    method:'POST',body:new ReadableStream<Uint8Array>({start(controller){stream=controller;}}),
  }),automatic as any);
  wakeAlias(h,id);stream.enqueue(new TextEncoder().encode(JSON.stringify({key:'alias-wake-stop'})));stream.close();
  const response=await pending;expect(response.status).toBe(409);
  expect(await response.json()).toEqual({error:'session_waiting_until'});
  expect(h.sql.exec('SELECT id FROM session_control_commands WHERE session_id=?',id).toArray()).toEqual([]);
}));

it.each(['other-host','past-due'])('does not borrow an alias wake that is %s', async scenario => fixture('running', async (h,id,task) => {
  wakeAlias(h,id,scenario==='other-host'?'another-host':undefined,scenario==='past-due'?NOW-1:NOW+60000);const stops:string[]=[];
  const watchdog=new IntakeWatchdog(h.sql,{statusAuthorizedTasks:()=>[task]} as any,(fn:any)=>fn(),{stop:async id=>{stops.push(id);}},()=>NOW);
  watchdog.init();await watchdog.tick();expect(stops).toEqual([id]);
}));
