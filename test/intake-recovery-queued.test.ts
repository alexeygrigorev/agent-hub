import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeWatchdog, queuePolicyFrom } from '../src/intake-watchdog';
import { SessionControlService } from '../src/session_control';
const inDO: any = runInDurableObject;
const admin: any = { caller: { isAdmin: true, actor: 'hub:service:intake-watchdog', kind: 'human' }, scopes: ['admin'] };

async function fixture(fn: (c: any) => Promise<void>) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('queued-recovery-' + crypto.randomUUID()));
  await inDO(stub, async (h: any, storage: any) => {
    const now = Date.now(), at = new Date(now).toISOString(), old = new Date(now - 3600000).toISOString();
    const id = crypto.randomUUID(), launch = crypto.randomUUID();
    const task = { id: crypto.randomUUID(), project: 'demo', title: 'Queued resource retry', status: 'open',
      created_at: now - 3600000, acceptance_criteria: 'Recover a resource refusal on an eligible engine', history: [] };
    const request = JSON.stringify({ engine: 'codex', mode: 'interactive', with_goal: true, intake_task_id: task.id, workspace: 'fixture' });
    // Fleet's proven no-start resource refusal creates this new queued row
    // and keeps its rejection in fleet_memory_holds, not control.hold_json.
    h.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'fixture','human','resource retry',?,?,'','','','interactive','fixture','test','queued',?,?)`, launch, launch, request, old, at);
    h.sql.exec(`INSERT INTO fleet_memory_holds(launch_id,since,rejected_json,refused) VALUES(?,?,?,1)`, launch, old,
      JSON.stringify([{ machine: 'fixture', engine: 'codex', reason: 'launcher_memory_reserve' }]));
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,fleet_launch_id,created_at,updated_at)
      VALUES(?,'fixture',?,?,'codex','interactive','queued',?,?,?)`, id, id, request, launch, old, at);
    const tx = (fn: any) => storage.storage.transactionSync(fn);
    const sessions = new SessionControlService(h.sql, tx, () => now); sessions.init();
    const c: any = { h, id, launch, task, now, at, sessions, stops: [], starts: [], beforeStop: null };
    c.watchdog = new IntakeWatchdog(h.sql, { statusAuthorizedTasks: () => [task] } as any, tx, {
      policy: () => queuePolicyFrom({ HUB_INTAKE_QUEUE_ENABLED: 'true', HUB_INTAKE_QUEUE_PROJECTS: '*' }),
      stop: async (sessionId, key) => {
        let stream!: ReadableStreamDefaultController<Uint8Array>;
        const pending = sessions.handle(new Request('https://hub.internal/api/sessions/' + sessionId + '/stop', {
          method: 'POST', body: new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }),
        }), admin);
        if (c.beforeStop) c.beforeStop();
        stream.enqueue(new TextEncoder().encode(JSON.stringify({ key }))); stream.close();
        const response = await pending; c.stops.push({ status: response.status, body: await response.json() });
      },
      start: async input => { c.starts.push(input); return { status: 202, body: { id: 'replacement', state: 'queued' } }; },
    }, () => now); c.watchdog.init(); await fn(c);
  });
}
function protect(c: any) {
  c.h.sql.exec(`INSERT INTO session_control_protections(session_id,actor,request_key,created_at) VALUES(?,'human:fixture','protect',?)`, c.id, c.at);
}
function wait(c: any) {
  c.h.sql.exec(`INSERT INTO session_control_wakes(id,session_id,actor,request_key,wake_at_ms,wake_at,prompt,state,created_at,updated_at)
    VALUES(?,?,'fixture','wait',?,?,'continue','pending',?,?)`, crypto.randomUUID(), c.id, c.now + 60000,
    new Date(c.now + 60000).toISOString(), c.at, c.at);
}

it('cancels a proven no-start resource queue through canonical control and selects another engine', async () => fixture(async c => {
  expect(c.watchdog.project([c.task])[0]).toMatchObject({ state: 'recovering', reason: 'recoverable_admission_hold' });
  await c.watchdog.tick(); expect(c.stops).toMatchObject([{ status: 200 }]);
  expect(c.h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', c.id).one().state).toBe('cancelled');
  expect(c.h.sql.exec('SELECT state FROM fleet_launches WHERE id=?', c.launch).one().state).toBe('cancelled');
  expect(c.starts).toHaveLength(1); expect(c.starts[0].engine).toBe('opencode');
}));

it.each(['ordinary-queue', 'other-retry', 'offered', 'starting', 'native', 'launcher'])('preserves %s rather than treating it as a cancellable resource queue', async scenario => fixture(async c => {
  if (scenario === 'ordinary-queue') c.h.sql.exec('DELETE FROM fleet_memory_holds WHERE launch_id=?', c.launch);
  if (scenario === 'other-retry') c.h.sql.exec('UPDATE fleet_memory_holds SET launch_id=? WHERE launch_id=?', crypto.randomUUID(), c.launch);
  if (['offered', 'starting'].includes(scenario)) c.h.sql.exec('UPDATE fleet_launches SET state=? WHERE id=?', scenario, c.launch);
  if (scenario === 'native') c.h.sql.exec("UPDATE fleet_launches SET native_id='unknown-native' WHERE id=?", c.launch);
  if (scenario === 'launcher') c.h.sql.exec("UPDATE fleet_launches SET launcher_task_id='unknown-task' WHERE id=?", c.launch);
  await c.watchdog.tick(); expect(c.stops).toEqual([]); expect(c.starts).toEqual([]);
  expect(c.h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', c.id).one().state).toBe('queued');
}));

it.each(['protection', 'future-wake'])('preserves a resource queue with pre-existing %s', async scenario => fixture(async c => {
  scenario === 'protection' ? protect(c) : wait(c);
  await c.watchdog.tick(); expect(c.stops).toEqual([]); expect(c.starts).toEqual([]);
}));

it.each(['protection', 'future-wake', 'promotion', 'claim'])('rechecks %s while the queued-resource stop body streams', async scenario => fixture(async c => {
  c.beforeStop = () => {
    if (scenario === 'protection') protect(c);
    if (scenario === 'future-wake') wait(c);
    if (scenario === 'promotion') c.h.sql.exec("UPDATE fleet_launches SET state='offered' WHERE id=?", c.launch);
    if (scenario === 'claim') c.h.sql.exec("UPDATE fleet_launches SET state='starting',launcher_task_id='host-claimed' WHERE id=?", c.launch);
  };
  await c.watchdog.tick();
  const error = scenario === 'protection' ? 'session_protected' : scenario === 'future-wake' ? 'session_waiting_until' : 'session_admission_in_progress';
  expect(c.stops).toMatchObject([{ status: 409, body: { error } }]); expect(c.starts).toEqual([]);
  expect(c.h.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', c.id).one().state).toBe('queued');
  expect(c.h.sql.exec('SELECT session_id FROM session_control_cancellations WHERE session_id=?', c.id).toArray()).toEqual([]);
}));
