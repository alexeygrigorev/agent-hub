/** Worktree-guard server regression tests (intake a0f01448, RCA 95c1862c).
 *
 * AC1: fleet offers carry hub_session_id + intake_task_id for the host lock.
 * AC2: cleanup dry-run/apply and claims refuse sessions linked to an open
 *   intake; claims carry linked_intake_open.
 * AC4: a worktree_missing observation on a live session with an open linked
 *   intake fires the needs_attention alert hook and the intake-note hook.
 * AC5: a cwd_path_recreated observation projects as cwd_stale_path_recreated.
 */
import { describe, expect, it } from 'vitest';
import { closeFixtureIntake, FIXTURE_INTAKE_ID, seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, adoptObservedNativeSessions } from '../src/session_control';
import { isIntakeTaskOpen } from '../src/session_lifecycle_registry';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '44444444-4444-4444-8444-444444444444';
const NATIVE_WS = '/srv/hub/guard';
const NATIVE_CREATED = 1792223000000;
const canonTag = (launchId: string) => 'hub-' + launchId;

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)) });
}

function fakeReserve() {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

type Hooks = { alerts: any[]; notes: any[]; failNote?: boolean };

async function fixture(fn: (s: SessionControlService, sql: any, hooks: Hooks) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-worktree-guard-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const hooks: Hooks = { alerts: [], notes: [] };
    const reserve = fakeReserve();
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now,
      { reserve: reserve.hook },
      { env: bindings as unknown as Env,
        worktreeGuard: {
          setAlert: (kind: string, subject: string, problem: any) => { hooks.alerts.push({ kind, subject, problem }); },
          appendIntakeNote: (taskId: string, text: string) => { if (hooks.failNote) throw new Error('synthetic note failure'); hooks.notes.push({ taskId, text }); },
        } });
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
    await fn(service, hub.sql, hooks);
  });
}

async function startAgentSession(s: SessionControlService, key: string) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo' }), agent());
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

function ensureFleetLaunch(sql: any, launchId: string, nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'demo:assistant', 'agent', NULL, 'fixture', ?, '{"workspace":"demo"}', 'hetzner', 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES ('hetzner', ?, ?, 'codex', ?, NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    nativeId, tag, phase, at, workspace);
  sql.exec('UPDATE fleet_sessions SET created_at_ms=? WHERE machine_id=? AND native_id=?', NATIVE_CREATED, 'hetzner', nativeId);
}

async function bindAgentSession(s: SessionControlService, sql: any, sessionId: string, launchId: string) {
  ensureFleetLaunch(sql, launchId);
  reportFleetSession(sql, NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`, {
    native: { native_id: NATIVE_ID, tag: canonTag(launchId), workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED },
  }), host('hetzner'));
  expect(res.status).toBe(200);
}

async function finishObservation(s: SessionControlService) {
  const now = new Date().toISOString();
  const res = await s.handle(request('/api/sessions/observations', {
    machine_id: 'hetzner', native_id: NATIVE_ID,
    payload: { report_written: true, report_at: now, screen_done: true, authoritative_no_pending_proof: true },
  }), host('hetzner'));
  expect(res.status).toBe(201);
}

async function offerFor(s: SessionControlService, commandId: string) {
  const poll = await (await s.handle(request('/api/session-commands/offers', undefined, 'GET'), host('hetzner'))).json() as any;
  const offer = (poll.offers as any[]).find((o: any) => o.command_id === commandId);
  expect(offer, JSON.stringify(poll)).toBeTruthy();
  return offer;
}

describe('worktree guard (intake a0f01448)', () => {
  it('fails closed on ledger errors or corrupt link projections', async () => fixture(async (s, sql) => {
    const check = (SessionControlService.prototype as any).linkedOpenIntakeForSession;
    expect(() => check.call({ sql: { exec: () => { throw new Error('ledger unavailable'); } } }, { id: 'guard' }))
      .toThrow();
    sql.exec("INSERT INTO intake_task_projection(id,data,links) VALUES ('corrupt','{}','broken')");
    expect(() => check.call(s, { id: 'guard' })).toThrow();
    expect((s as any).linkedIntakeProtected({ id: 'guard' })).toBe(true);
  }));

  it('protects a linked open intake beyond the former projection scan limit', async () => fixture(async (s, sql) => {
    for (let i = 0; i < 510; i++) {
      sql.exec('INSERT INTO intake_task_projection(id,data,links) VALUES (?, ?, ?)', 'unrelated-' + i, '{}', '[]');
    }
    const lateId = 'guard-late-intake';
    sql.exec('INSERT INTO intake_tasks(id,data) VALUES (?, ?)', lateId,
      JSON.stringify({ id: lateId, status: 'open', history: [] }));
    sql.exec('INSERT OR REPLACE INTO intake_task_projection(id,data,links) VALUES (?, ?, ?)', lateId, '{}',
      JSON.stringify([{ kind: 'link', hub_session_id: 'guard-late' }]));
    expect((s as any).linkedOpenIntakeForSession({ id: 'guard-late' })).toBe(lateId);
  }));

  it('keeps status-only done/cancelled intakes protected until DONE evidence or archive', async () => fixture(async (_s, sql) => {
    for (const status of ['done', 'cancelled']) {
      sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.status',?) WHERE id=?", status, FIXTURE_INTAKE_ID);
      expect(isIntakeTaskOpen(sql, FIXTURE_INTAKE_ID)).toBe(true);
    }
    closeFixtureIntake(sql);
    expect(isIntakeTaskOpen(sql, FIXTURE_INTAKE_ID)).toBe(false);
    seedFixtureIntake(sql);
    sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.archived_at',?) WHERE id=?", new Date().toISOString(), FIXTURE_INTAKE_ID);
    expect(isIntakeTaskOpen(sql, FIXTURE_INTAKE_ID)).toBe(false);
  }));

  it('refuses cleanup while the linked intake is open and allows it after DONE (AC2)', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'guard-open-intake');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    await finishObservation(s);
    // Linked intake is open: dry-run ineligible with the guard reason.
    const dry = await (await s.handle(request('/api/sessions/cleanup', { dry_run: true }), agent())).json() as any;
    expect(dry.eligible).toEqual([]);
    expect(dry.ineligible.map((e: any) => e.reasons.join(' ')).join(' ')).toContain('linked open intake');
    // Apply enqueues nothing while open.
    const applied = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'guard-open' }), agent())).json() as any;
    expect(applied.enqueued).toEqual([]);
    // Ordinary stop still works and its claim reports the open intake.
    const stop = await s.handle(request(`/api/sessions/${created.id}/stop`, { key: 'guard-stop' }), agent());
    const stopId = ((await stop.json()) as any).command_id;
    const offer = await offerFor(s, stopId);
    const claim = await s.handle(request(`/api/session-commands/${stopId}/claim`,
      { correlation: offer.correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    expect(claim.status).toBe(200);
    const claimData = await claim.json() as any;
    expect(claimData.cleanup_tree).toBe(false);
    expect(claimData.linked_intake_open).toBe(true);
    // DONE note with live proof re-opens the gate.
    sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?)) WHERE id=?",
      JSON.stringify([{ id: 'n1', kind: 'note',
        text: 'DONE abc1234\nlive check: hub sessions show guard-open-intake\nresult: cleanup refused while open' }]),
      FIXTURE_INTAKE_ID);
    const dry2 = await (await s.handle(request('/api/sessions/cleanup', { dry_run: true }), agent())).json() as any;
    expect(dry2.eligible.map((e: any) => e.session_key)).toContain(`hetzner/${NATIVE_ID}`);
    const applied2 = await (await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'guard-done' }), agent())).json() as any;
    expect(applied2.enqueued).toHaveLength(1);
    const offer2 = await offerFor(s, applied2.enqueued[0].command_id);
    const claim2 = await s.handle(request(`/api/session-commands/${applied2.enqueued[0].command_id}/claim`,
      { correlation: offer2.correlation, attempt_id: crypto.randomUUID() }), host('hetzner'));
    expect(claim2.status).toBe(200);
    const claimData2 = await claim2.json() as any;
    expect(claimData2.cleanup_tree).toBe(true);
    expect(claimData2.linked_intake_open).toBe(false);
  }));

  it('alerts and notes the linked intake when a live worktree disappears (AC4)', async () => fixture(async (s, sql, hooks) => {
    const created = await startAgentSession(s, 'guard-self-removal');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const res = await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'));
    expect(res.status).toBe(201);
    expect(hooks.alerts).toHaveLength(1);
    expect(hooks.alerts[0]).toMatchObject({ kind: 'session', subject: `hetzner/${NATIVE_ID}` });
    expect(hooks.alerts[0].problem.state).toBe('worktree_removed');
    expect(hooks.notes).toHaveLength(1);
    expect(hooks.notes[0].taskId).toBe(FIXTURE_INTAKE_ID);
    expect(hooks.notes[0].text).toContain('95c1862c');
    // A recovery observation resolves the alert without a second note.
    const res2 = await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: false },
    }), host('hetzner'));
    expect(res2.status).toBe(201);
    expect(hooks.alerts).toHaveLength(2);
    expect(hooks.alerts[1].problem).toBeNull();
    expect(hooks.notes).toHaveLength(1);
    // Non-live sessions never alert: terminal phase, same observation.
    sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE machine_id='hetzner' AND native_id=?", NATIVE_ID);
    const res3 = await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'));
    expect(res3.status).toBe(201);
    expect(hooks.alerts).toHaveLength(2);
    expect(hooks.notes).toHaveLength(1);
  }));

  it('retries missing bindings and hook failures, acknowledges each episode once', async () => fixture(async (s, sql, hooks) => {
    const created = await startAgentSession(s, 'guard-durable-retry');
    const missing = () => s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'));
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(false);
    await bindAgentSession(s, sql, created.id, 'launch-1');
    hooks.failNote = true;
    await expect(missing()).rejects.toThrow('synthetic note failure');
    expect(sql.exec('SELECT * FROM session_worktree_episodes').toArray()).toHaveLength(0);
    hooks.failNote = false;
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect(hooks.notes).toHaveLength(1);
    await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { cwd_path_recreated: true },
    }), host('hetzner'));
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect(hooks.notes).toHaveLength(2);
    expect(hooks.notes[1].text).toContain('episode 2');
  }));

  it('delivers missing-worktree effects before binding from the exact canonical launch', async () => fixture(async (s, sql, hooks) => {
    const created = await startAgentSession(s, 'guard-before-binding');
    ensureFleetLaunch(sql, 'launch-1', null);
    reportFleetSession(sql, NATIVE_ID, canonTag('launch-1'), NATIVE_WS);
    const result = await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'));
    expect((await result.json() as any).worktree_followup_complete).toBe(true);
    expect(hooks.notes).toHaveLength(1);
    expect(hooks.alerts).toHaveLength(1);
    expect(sql.exec('SELECT binding_json FROM session_control_sessions WHERE id=?',created.id).toArray()[0].binding_json).toBeNull();
  }));

  it('adds a late linked intake note without duplicating an acknowledged episode', async () => fixture(async (s, sql, hooks) => {
    const created = await startAgentSession(s, 'guard-late-link-episode');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    closeFixtureIntake(sql);
    const missing = () => s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'));
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect(hooks.notes).toHaveLength(0);
    const task={id:'late-episode-task',status:'open',history:[]};
    sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',task.id,JSON.stringify(task));
    sql.exec('INSERT OR REPLACE INTO intake_task_projection(id,data,links) VALUES(?,?,?)',task.id,JSON.stringify(task),
      JSON.stringify([{kind:'link',hub_session_id:created.id}]));
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect((await (await missing()).json() as any).worktree_followup_complete).toBe(true);
    expect(hooks.notes).toHaveLength(1);
    expect(hooks.notes[0].taskId).toBe(task.id);
  }));

  it('provides fresh host-scoped terminal and intake protection facts', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'guard-host-facts');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const facts = async () => {
      const res = await s.handle(request('/api/sessions/worktree-guards', undefined, 'GET'), host('hetzner'));
      expect(res.status).toBe(200);
      return (await res.json() as any).sessions;
    };
    expect((await facts())[0]).toMatchObject({ session_id: created.id, native_id: NATIVE_ID,
      native_created_at_ms: NATIVE_CREATED, terminal: false, linked_intake_open: true });
    sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", created.id);
    sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE machine_id='hetzner' AND native_id=?", NATIVE_ID);
    closeFixtureIntake(sql);
    const stopped = (await facts())[0];
    expect(stopped).toMatchObject({ terminal: true, native_terminal: true, linked_intake_open: false, lock_release_permitted: true });
    // Closing an intake cannot unlock the predecessor's retained lock while
    // a successor still runs on that same intake (d31eba1f).
    const successor = crypto.randomUUID();
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,mode,state,created_at,updated_at)
      VALUES(?,'demo:assistant',?,?,'hetzner','interactive','starting',?,?)`, successor,successor,
      JSON.stringify({intake_task_id:stopped.intake_task_id}),new Date().toISOString(),new Date().toISOString());
    expect((await facts()).find((f:any)=>f.session_id===created.id).lock_release_permitted).toBe(false);
    sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?",successor);
    // A terminal label without native exit/no-start proof cannot release.
    expect((await facts()).find((f:any)=>f.session_id===created.id).lock_release_permitted).toBe(false);
    const successorNative = '55555555-5555-4555-8555-555555555555';
    const successorBinding = {native_id:successorNative, tag:canonTag('successor-proof'),
      workspace:NATIVE_WS, native_created_at_ms:NATIVE_CREATED, generation:1};
    ensureFleetLaunch(sql,'successor-proof',successorNative);
    reportFleetSession(sql,successorNative,successorBinding.tag,NATIVE_WS);
    sql.exec("UPDATE session_control_sessions SET fleet_launch_id='successor-proof', owner_token_id='cred-1', binding_json=? WHERE id=?",JSON.stringify(successorBinding),successor);
    expect((await facts()).find((f:any)=>f.session_id===created.id).lock_release_permitted).toBe(false);
    sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id=?",successorNative);
    expect((await facts()).find((f:any)=>f.session_id===created.id).lock_release_permitted).toBe(true);
    // A genuinely never-started successor is also safe with positive no-start evidence.
    ensureFleetLaunch(sql,'successor-proof',null,'refused');
    sql.exec("UPDATE session_control_sessions SET binding_json=NULL,state='refused' WHERE id=?",successor);
    expect((await facts()).find((f:any)=>f.session_id===created.id).lock_release_permitted).toBe(true);
    expect((await s.handle(request('/api/sessions/worktree-guards', undefined, 'GET'), agent())).status).toBe(403);
  }));

  it('releases a terminal session whose pruned native is only unobserved in the census', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'guard-unobserved-release');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    sql.exec("UPDATE session_control_sessions SET state='failed' WHERE id=?", created.id);
    sql.exec("UPDATE fleet_sessions SET phase='unobserved' WHERE machine_id='hetzner' AND native_id=?", NATIVE_ID);
    closeFixtureIntake(sql);
    const facts = async () => {
      const res = await s.handle(request('/api/sessions/worktree-guards', undefined, 'GET'), host('hetzner'));
      expect(res.status).toBe(200);
      return (await res.json() as any).sessions.find((row: any) => row.session_id === created.id);
    };
    // Pruned after a dispatch-false-failure finish: census says not live.
    expect(await facts()).toMatchObject({ terminal: true, native_terminal: true, linked_intake_open: false });
    // 'uncertain' is an unconfirmed terminal label, not absence evidence.
    sql.exec("UPDATE fleet_sessions SET phase='uncertain' WHERE machine_id='hetzner' AND native_id=?", NATIVE_ID);
    expect(await facts()).toMatchObject({ terminal: true, native_terminal: false });
  }));

  it('keeps a non-terminal session held even when its census incarnation is unobserved', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'guard-unobserved-live-hold');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    sql.exec("UPDATE fleet_sessions SET phase='unobserved' WHERE machine_id='hetzner' AND native_id=?", NATIVE_ID);
    closeFixtureIntake(sql);
    const res = await s.handle(request('/api/sessions/worktree-guards', undefined, 'GET'), host('hetzner'));
    const fact = (await res.json() as any).sessions.find((row: any) => row.session_id === created.id);
    expect(fact).toMatchObject({ terminal: false, native_terminal: true, linked_intake_open: false });
  }));

  it('reuses a proven stopped unbound predecessor without releasing its lock for a live successor', async () => fixture(async (s,sql) => {
    const previous = await startAgentSession(s,'guard-stopped-unbound');
    ensureFleetLaunch(sql,'launch-1',null,'uncertain');
    sql.exec("UPDATE session_control_sessions SET state='uncertain' WHERE id=?",previous.id);
    const stop = await s.handle(request('/api/sessions/'+previous.id+'/stop',{key:'stop-unbound-proof'}),admin);
    expect((await stop.json() as any).state).toBe('acked');
    // Repeated idempotent stops must retain unbound exit proof even after
    // the original receipt leaves the bounded twenty-receipt lookup.
    sql.exec("UPDATE session_control_commands SET updated_at='2000-01-01T00:00:00.000Z' WHERE session_id=? AND kind='stop'", previous.id);
    for (let i = 0; i < 25; i++) {
      const replay = await s.handle(request('/api/sessions/'+previous.id+'/stop',{key:'stop-unbound-repeat-'+i}),admin);
      expect((await replay.json() as any).state).toBe('acked');
    }
    closeFixtureIntake(sql);
    const facts = async () => (await (await s.handle(request('/api/sessions/worktree-guards',undefined,'GET'),host('hetzner'))).json() as any).sessions;
    const old = (await facts()).find((f:any)=>f.session_id===previous.id);
    expect(old).toMatchObject({native_id:null,terminal:true,native_terminal:true,lock_release_permitted:true});
    const successor = crypto.randomUUID(), at = new Date().toISOString();
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,mode,state,created_at,updated_at)
      VALUES(?,'demo:assistant',?,?,'hetzner','interactive','starting',?,?)`,successor,successor,
      JSON.stringify({intake_task_id:old.intake_task_id}),at,at);
    expect((await facts()).find((f:any)=>f.session_id===previous.id)).toMatchObject({native_terminal:true,lock_release_permitted:false});
    sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?",successor);
    expect((await facts()).find((f:any)=>f.session_id===previous.id)).toMatchObject({native_terminal:true,lock_release_permitted:false});
  }));

  it('projects a recreated worktree path distinctly (AC5)', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'guard-recreated');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    // A plain worktree_missing observation projects as missing.
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: { worktree_missing: true },
    }), host('hetzner'))).status).toBe(201);
    // The recreated-path observation is accepted and wins the projection.
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID,
      payload: { worktree_missing: true, cwd_path_recreated: true },
    }), host('hetzner'))).status).toBe(201);
    const view = await (await s.handle(request('/api/sessions?view=lifecycle', undefined, 'GET'), admin)).json() as any;
    const rows = [...(view.groups.unknown as any[]), ...(view.groups.active as any[])];
    const row = rows.find((e: any) => e.session_key === `hetzner/${NATIVE_ID}`);
    expect(row).toBeTruthy();
    expect(row.state).toBe('cwd_stale_path_recreated');
  }));
});


describe('observed-native adoption requires verified custody and a host lock', () => {
  it('publishes unknown census identity without trusting ownership and lock labels', async () => {
    await fixture(async (_service, sql) => {
      const native = { native_id: NATIVE_ID, tag: 'hub-spoofed', workspace: NATIVE_WS,
        engine: 'codex', phase: 'running', created_at_ms: NATIVE_CREATED,
        owner: 'demo:assistant', protected: false, worktree_locked: true };
      expect(adoptObservedNativeSessions(sql, 'hetzner', [native], Date.now()).adopted).toBe(1);
      expect(adoptObservedNativeSessions(sql, 'hetzner', [native], Date.now()).adopted).toBe(0);
      const observation: any = sql.exec('SELECT * FROM session_control_sessions WHERE id=?', NATIVE_ID).toArray()[0];
      expect(observation).toMatchObject({actor:'unknown',owner_token_id:null,owner_family:null});
      expect(JSON.parse(observation.request_json).observation_only).toBe(true);
      expect(JSON.parse(observation.binding_json).adoption_lock).toBeUndefined();

    });
  });

  it('does not let the explicit adoption API fabricate a canonical binding without a host receipt', async () => {
    await fixture(async (service, sql) => {
      const res = await service.handle(request('/api/sessions/host-adoption', { machine_id: 'hetzner',
        sessions: [{ native_id: NATIVE_ID, tag: 'manual-worker', workspace: NATIVE_WS,
          engine: 'codex', phase: 'running', created_at_ms: NATIVE_CREATED }] }), admin);
      expect(res.status).toBe(202);
      expect((await res.json() as any).adopted).toBe(0);
      expect(sql.exec('SELECT id FROM session_control_sessions WHERE id=?', NATIVE_ID).toArray()).toHaveLength(0);
    });
  });
});


describe('explicit operator adoption host receipt', () => {
  it('queues custody, rejects wrong host/incarnation, and binds only after the exact host lock receipt', async () => {
    await fixture(async (service, sql) => {
      reportFleetSession(sql, NATIVE_ID, 'manual-worker', NATIVE_WS);
      const native = { native_id: NATIVE_ID, tag: 'manual-worker', workspace: NATIVE_WS,
        engine: 'codex', created_at_ms: NATIVE_CREATED };
      const queued = await service.handle(request('/api/sessions/host-adoption', { machine_id: 'hetzner', sessions: [native] }), admin);
      expect(queued.status).toBe(202);
      expect((await queued.json() as any).queued).toBe(1);
      expect(sql.exec('SELECT id FROM session_control_sessions WHERE id=?', NATIVE_ID).toArray()).toHaveLength(0);
      const pending = await service.handle(request('/api/sessions/host-adoption', undefined, 'GET'), host('hetzner'));
      const intent = (await pending.json() as any).sessions[0];
      expect(intent.native.native_id).toBe(NATIVE_ID);
      const receipt = { id: intent.id, native, lock_verified: true, worktree: true,
        lock_path: NATIVE_WS, repository: { common_dir: '/srv/hub/repo/.git', device: 1, inode: 2 } };
      const agentReceipt = await service.handle(request('/api/sessions/host-adoption/receipt', receipt), agent());
      expect(agentReceipt.status).toBe(403);
      const wrongHost = await service.handle(request('/api/sessions/host-adoption/receipt', receipt), host('foreign'));
      expect(wrongHost.status).toBe(409);
      const wrongNative = await service.handle(request('/api/sessions/host-adoption/receipt', { ...receipt, native: { ...native, created_at_ms: 1 } }), host('hetzner'));
      expect(wrongNative.status).toBe(409);
      const noLock = await service.handle(request('/api/sessions/host-adoption/receipt', { ...receipt, lock_verified: false }), host('hetzner'));
      expect(noLock.status).toBe(409);
      const completed = await service.handle(request('/api/sessions/host-adoption/receipt', receipt), host('hetzner'));
      expect(completed.status).toBe(200);
      const row = sql.exec('SELECT * FROM session_control_sessions WHERE id=?', NATIVE_ID).one();
      expect(row.actor).toBe('test-admin');
      expect(JSON.parse(row.binding_json).adoption_intent_id).toBe(intent.id);
      const repeat = await service.handle(request('/api/sessions/host-adoption/receipt', receipt), host('hetzner'));
      expect((await repeat.json() as any).replay).toBe(true);
      const guards = await service.handle(request('/api/sessions/worktree-guards', undefined, 'GET'), host('hetzner'));
      const fact = (await guards.json() as any).sessions.find((s: any) => s.session_id === NATIVE_ID);
      expect(fact.engine).toBe('codex');
      expect(fact.lock_release_permitted).toBe(true);
    });
  });

  it('retains catalog accounting but refuses stale evidence and host-only custody claims', async () => {
    await fixture(async (service, sql) => {
      reportFleetSession(sql, NATIVE_ID, 'manual-worker', NATIVE_WS);
      const native = { native_id: NATIVE_ID, tag: 'manual-worker', workspace: NATIVE_WS, engine: 'codex', created_at_ms: NATIVE_CREATED };
      const hostIntent = await service.handle(request('/api/sessions/host-adoption', { sessions: [native] }), host('hetzner'));
      expect(hostIntent.status).toBe(403);
      sql.exec("UPDATE fleet_sessions SET observed_at='2000-01-01T00:00:00.000Z' WHERE native_id=?", NATIVE_ID);
      const stale = await service.handle(request('/api/sessions/host-adoption', { machine_id: 'hetzner', sessions: [native] }), admin);
      expect((await stale.json() as any).deferred).toBe(1);
      expect(sql.exec('SELECT native_id FROM fleet_sessions WHERE native_id=?', NATIVE_ID).toArray()).toHaveLength(1);
      expect(sql.exec('SELECT id FROM session_adoption_intents').toArray()).toHaveLength(0);
    });
  });
});


describe('adoption custody expiry and legacy protection', () => {
  it('withdraws revoked custody before host effects and preserves an existing canonical session', async () => {
    await fixture(async (service, sql) => {
      reportFleetSession(sql, NATIVE_ID, 'manual-worker', NATIVE_WS);
      const native = { native_id: NATIVE_ID, tag: 'manual-worker', workspace: NATIVE_WS, engine: 'codex', created_at_ms: NATIVE_CREATED };
      const operator = { caller: { ...admin.caller, tokenId: 'cred-1' }, scopes: ['admin'] };
      await service.handle(request('/api/sessions/host-adoption', { machine_id: 'hetzner', sessions: [native] }), operator);
      const session = await startAgentSession(service, 'keep-existing-canonical');
      sql.exec('UPDATE credentials SET revoked_at=? WHERE id=?', new Date().toISOString(), 'cred-1');
      const pending = await service.handle(request('/api/sessions/host-adoption', undefined, 'GET'), host('hetzner'));
      expect((await pending.json() as any).sessions).toEqual([]);
      const prior = sql.exec('SELECT * FROM session_control_sessions WHERE id=?', session.id).one();
      const existingRequest = await service.handle(request('/api/sessions/host-adoption', { machine_id: 'hetzner', sessions: [{ ...native, native_id: session.id }] }), admin);
      expect((await existingRequest.json() as any).existing).toBe(1);
      expect(sql.exec('SELECT * FROM session_control_sessions WHERE id=?', session.id).one()).toEqual(prior);
    });
  });
});
