/** M15c live lifecycle HTTP/SQLite suite (synthetic fixtures only).
 *
 * Exercises the genuinely working seams: init creates the additive tables,
 * GET /api/sessions?view=lifecycle projects canonical joins, host
 * machine-scoped observations land durably, owner-only limit-block
 * decisions persist and feed the projection, cleanup dry-run mutates
 * nothing, and --apply refuses explicitly. No native/Git effects anywhere.
 */
import { describe, expect, it } from 'vitest';
import { closeFixtureIntake, seedFixtureIntake, withFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const stranger = { caller: { kind: 'agent', actor: 'other:assistant', tokenId: 'cred-2' }, scopes: ['fleet:launch', 'bus:send'] };
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });
const unauth = { error: Response.json({ error: 'unauthorized' }, { status: 401 }) } as any;

const NATIVE_ID = '22222222-2222-4222-8222-222222222222';
const NATIVE_WS = '/srv/hub/demo';
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

async function fixture(fn: (s: SessionControlService, sql: any, serviceEnv: Env) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-lifecycle-m15c-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const reserve = fakeReserve();
    const serviceEnv = { ...bindings, HUB_ADMINS: 'admin@fake.local' } as Env;
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: reserve.hook }, { env: serviceEnv });
    service.init();
    seedFixtureIntake(hub.sql);
    hub.sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.history',json(?)) WHERE id='fixture-intake'",
      JSON.stringify([{kind:'note',text:'DONE: synthetic completed cleanup fixture'}]));
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
      VALUES ('host-cred-1', 'host', 'hash-host-cred-1', 'refresh-host-cred-1', 'host-family-1', 'test-host', 'hetzner', NULL, '["host"]', 'test', ?, ?)`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('hetzner', 'hetzner', 'hetzner', 'host-cred-1', ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'demo', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-1', 'agent', 'hash-cred-1', 'refresh-cred-1', 'agent-family-1', 'demo-agent', NULL, 'demo:assistant', '["fleet:launch"]', 'test', ?, ?, 'demo:assistant')`, at, far);
    hub.sql.exec(`INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('assistant', 'other', 'worker', NULL, ?)`, at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('cred-2', 'agent', 'hash-cred-2', 'refresh-cred-2', 'agent-family-2', 'other-agent', NULL, 'other:assistant', '["fleet:launch"]', 'test', ?, ?, 'other:assistant')`, at, far);
    await fn(service, hub.sql, serviceEnv);
  });
}

function ensureFleetLaunch(sql: any, launchId: string, machine = 'hetzner', nativeId: string | null = NATIVE_ID, launchState = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
    VALUES (?, 'demo:assistant', 'agent', NULL, 'fixture', ?, '{"workspace":"demo"}', ?, 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', ?, ?, NULL, ?, ?)`,
    launchId, 'fleet-' + launchId, machine, launchState, nativeId, at, at);
}

function reportFleetSession(sql: any, machine: string, nativeId: string, tag: string, workspace: string, phase = 'running') {
  const at = new Date().toISOString();
  sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
    VALUES (?, ?, ?, 'codex', ?, NULL, ?, ?, NULL, NULL, 'fixture-acct', NULL)`,
    machine, nativeId, tag, phase, at, workspace);
}

async function startAgentSession(s: SessionControlService, key: string) {
  const res = await s.handle(request('/api/sessions', { key, workspace: 'demo' }), agent());
  const data = await res.json() as any;
  expect(res.status, JSON.stringify(data)).toBe(201);
  return data;
}

async function bindAgentSession(s: SessionControlService, sql: any, sessionId: string, launchId: string) {
  ensureFleetLaunch(sql, launchId);
  reportFleetSession(sql, 'hetzner', NATIVE_ID, canonTag(launchId), NATIVE_WS);
  const res = await s.handle(request(`/api/sessions/${sessionId}/binding`, {
    native: { native_id: NATIVE_ID, tag: canonTag(launchId), workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED },
  }), host('hetzner'));
  expect(res.status).toBe(200);
}

async function lifecycle(s: SessionControlService, auth: any, query = '') {
  const res = await s.handle(request('/api/sessions?view=lifecycle' + query, undefined, 'GET'), auth);
  const data = await res.json() as any;
  return { res, data };
}

const human = (tokenId = 'cleanup-human-1') => ({ caller: { kind: 'human', isAdmin: true, actor: 'human:cleanup-owner', tokenId }, scopes: ['admin'] });
function seedHuman(sql: any, id = 'cleanup-human-1', family = 'cleanup-human-family') {
  const at = new Date().toISOString();
  sql.exec('INSERT OR IGNORE INTO users (sub,email,is_admin,first_seen_at,last_seen_at) VALUES (?,?,1,?,?)', 'cleanup-owner', 'admin@fake.local', at, at);
  sql.exec(`INSERT INTO credentials (id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES (?,'human',?,?,'admin@fake.local','human:cleanup-owner','["admin"]','cleanup-owner',?,?)`, id, 'hash-'+id, family, at, new Date(Date.now()+3600000).toISOString());
}
async function humanWorker(s: SessionControlService, sql: any, mode = 'headless') {
  seedHuman(sql);
  const res = await s.handle(request('/api/sessions', {key:'human-worker',workspace:'demo',mode,name:'owned-human-worker'}), human());
  expect(res.status, await res.clone().text()).toBe(201);
  const created = await res.json() as any;
  ensureFleetLaunch(sql, 'launch-1');
  sql.exec("UPDATE fleet_launches SET actor='human:cleanup-owner', requester_kind='human' WHERE id='launch-1'");
  reportFleetSession(sql, 'hetzner', NATIVE_ID, canonTag('launch-1'), NATIVE_WS);
  expect((await s.handle(request(`/api/sessions/${created.id}/binding`, {native:{native_id:NATIVE_ID,tag:canonTag('launch-1'),workspace:NATIVE_WS,native_created_at_ms:NATIVE_CREATED}}), host('hetzner'))).status).toBe(200);
  const at = new Date().toISOString();
  expect((await s.handle(request('/api/sessions/observations', {machine_id:'hetzner',native_id:NATIVE_ID,payload:{report_written:true,report_at:at,screen_done:true,authoritative_no_pending_proof:true}}),host('hetzner'))).status).toBe(201);
  // Worktree-guard: legacy cleanup tests run with the linked intake closed;
  // the open-intake refusal is covered by dedicated tests.
  closeFixtureIntake(sql);
  return created;
}
async function humanPlan(s: SessionControlService, auth = human()) {
  return (await s.handle(request('/api/sessions/cleanup',{dry_run:true}),auth)).json() as Promise<any>;
}

describe('human worker cleanup custody and explicit protection', () => {
  it('distinguishes a human creator from a protected human workload and stamps exact-family commands', async () => fixture(async (s,sql) => {
    const created = await humanWorker(s,sql);
    const original: any = sql.exec('SELECT * FROM session_control_sessions WHERE id=?',created.id).toArray()[0];
    expect(original.owner_family).toBe('cleanup-human-family');
    const plan = await humanPlan(s);
    expect(plan.eligible).toHaveLength(1);
    expect(plan.eligible[0].refs.human_owned).toBe(true);
    expect(plan.eligible[0].refs.protected_session).toBe(false);
    const applied: any = await (await s.handle(request('/api/sessions/cleanup',{apply:true,key:'human-cleanup'}),human())).json();
    expect(applied.enqueued).toHaveLength(1);
    const command: any = sql.exec('SELECT * FROM session_control_commands WHERE id=?',applied.enqueued[0].command_id).toArray()[0];
    expect(command.owner_family).toBe('cleanup-human-family');
    const claim = await s.handle(request(`/api/session-commands/${command.id}/claim`,{correlation:command.correlation,attempt_id:crypto.randomUUID()}),host('hetzner'));
    expect(claim.status,await claim.clone().text()).toBe(200);
    expect((await claim.json() as any).cleanup_tree).toBe(true);
  }));

  it('refuses the same actor from another family and retains immutable creator provenance across refresh', async () => fixture(async (s,sql) => {
    const created = await humanWorker(s,sql);
    seedHuman(sql,'cleanup-other-family','different-family');
    expect((await humanPlan(s,human('cleanup-other-family'))).eligible).toEqual([]);
    const refused: any = await (await s.handle(request('/api/sessions/cleanup',{apply:true,key:'foreign-family'}),human('cleanup-other-family'))).json();
    expect(refused.enqueued).toEqual([]);
    seedHuman(sql,'cleanup-rotated');
    sql.exec('UPDATE credentials SET revoked_at=? WHERE id=?',new Date().toISOString(),'cleanup-human-1');
    // A pre-family-column session still has independently verifiable creator provenance.
    sql.exec('UPDATE session_control_sessions SET owner_family=NULL WHERE id=?',created.id);
    expect((await humanPlan(s,human('cleanup-rotated'))).eligible).toHaveLength(1);
    expect(sql.exec('SELECT owner_token_id FROM session_control_sessions WHERE id=?',created.id).toArray()[0].owner_token_id).toBe('cleanup-human-1');
    const applied:any = await(await s.handle(request('/api/sessions/cleanup',{apply:true,key:'refreshed-family'}),human('cleanup-rotated'))).json();
    const command:any = sql.exec('SELECT * FROM session_control_commands WHERE id=?',applied.enqueued[0].command_id).toArray()[0];
    const claimed = await s.handle(request(`/api/session-commands/${command.id}/claim`,{correlation:command.correlation,attempt_id:crypto.randomUUID()}),host('hetzner'));
    expect(claimed.status,await claimed.clone().text()).toBe(200);
    expect((await claimed.json()as any).cleanup_tree).toBe(true);
    sql.exec('DELETE FROM credentials WHERE id=?','cleanup-human-1');
    expect((await humanPlan(s,human('cleanup-rotated'))).eligible).toEqual([]);
  }));

  it('keeps interactive and contradictory headless workloads protected', async () => {
    for (const change of ['interactive','fleet-mode','native-id','session-type']) await fixture(async (s,sql) => {
      const created = await humanWorker(s,sql,change==='interactive'?'interactive':'headless');
      if(change==='fleet-mode')sql.exec("UPDATE fleet_launches SET mode='interactive' WHERE id='launch-1'");
      if(change==='native-id')sql.exec("UPDATE fleet_launches SET native_id='another-native' WHERE id='launch-1'");
      if(change==='session-type')sql.exec('UPDATE session_control_sessions SET session_type=NULL WHERE id=?',created.id);
      expect((await humanPlan(s)).eligible).toEqual([]);
    });
  });

  it('protects by actual name, replays the receipt and never accepts an observation downgrade', async () => fixture(async (s,sql) => {
    const created = await humanWorker(s,sql);
    const first = await s.handle(request('/api/sessions/owned-human-worker/protect',{key:'alexey-protected'}),human());
    expect(first.status).toBe(200);
    const protection = (await first.json() as any).cleanup_protection;
    expect(protection.permanent).toBe(true);
    const replay: any = await (await s.handle(request(`/api/sessions/${created.id}/protect`,{key:'alexey-protected'}),human())).json();
    expect(replay.replay).toBe(true);expect(replay.cleanup_protection).toEqual(protection);
    expect((await s.handle(request(`/api/sessions/${created.id}/protect`,{key:'other-key'}),human())).status).toBe(409);
    expect((await s.handle(request('/api/sessions/observations',{machine_id:'hetzner',native_id:NATIVE_ID,payload:{protected_session:false}}),host('hetzner'))).status).toBe(400);
    expect((await s.handle(request(`/api/sessions/${created.id}/protect`,{key:'unprotect',protected:false}),human())).status).toBe(400);
    expect((await humanPlan(s)).eligible).toEqual([]);
  }));

  it('rechecks current human authority after body streaming and again before a host claim', async () => {
    for(const change of ['revoke','expire','disable','allowlist'])await fixture(async(s,sql,serviceEnv)=>{
      await humanWorker(s,sql);
      const mutate=()=>{
        if(change==='revoke')sql.exec("UPDATE credentials SET revoked_at=? WHERE id='cleanup-human-1'",new Date().toISOString());
        if(change==='expire')sql.exec("UPDATE credentials SET expires_at=? WHERE id='cleanup-human-1'",new Date(0).toISOString());
        if(change==='disable')sql.exec("UPDATE users SET is_admin=0 WHERE sub='cleanup-owner'");
        if(change==='allowlist')serviceEnv.HUB_ADMINS='other@fake.local';
      };
      let controller!: ReadableStreamDefaultController<Uint8Array>;
      const stream=new ReadableStream<Uint8Array>({start(c){controller=c;}});
      const pending=s.handle(new Request('https://hub.test/api/sessions/cleanup',{method:'POST',body:stream,duplex:'half'} as RequestInit),human());
      await Promise.resolve();mutate();controller.enqueue(new TextEncoder().encode('{"apply":true,"key":"streamed"}'));controller.close();
      expect((await pending).status).toBe(403);
      expect(sql.exec('SELECT COUNT(*) AS n FROM session_control_commands').one().n).toBe(0);
    });
    await fixture(async(s,sql)=>{
      await humanWorker(s,sql);
      const applied: any=await(await s.handle(request('/api/sessions/cleanup',{apply:true,key:'revoke-before-claim'}),human())).json();
      const command:any=sql.exec('SELECT * FROM session_control_commands WHERE id=?',applied.enqueued[0].command_id).toArray()[0];
      sql.exec("UPDATE credentials SET revoked_at=? WHERE id='cleanup-human-1'",new Date().toISOString());
      const claim=await s.handle(request(`/api/session-commands/${command.id}/claim`,{correlation:command.correlation,attempt_id:crypto.randomUUID()}),host('hetzner'));
      expect(claim.status).toBe(409);
      expect((await claim.json()as any).error).toBe('cleanup_not_eligible');
    });
  });

  it('records uncertainty rather than an authorized removal if protection changes after claim', async()=>fixture(async(s,sql)=>{
    const created=await humanWorker(s,sql);
    const applied:any=await(await s.handle(request('/api/sessions/cleanup',{apply:true,key:'claimed-then-protected'}),human())).json();
    const command:any=sql.exec('SELECT * FROM session_control_commands WHERE id=?',applied.enqueued[0].command_id).toArray()[0];
    const claimed:any=await(await s.handle(request(`/api/session-commands/${command.id}/claim`,{correlation:command.correlation,attempt_id:crypto.randomUUID()}),host('hetzner'))).json();
    expect(claimed.cleanup_tree).toBe(true);
    expect((await s.handle(request(`/api/sessions/${created.id}/protect`,{key:'protected-after-claim'}),human())).status).toBe(200);
    const ack=await s.handle(request(`/api/session-commands/${command.id}/ack`,{correlation:command.correlation,claim_token:claimed.claim_token,outcome:'delivered',confirmed_exit:true,native:{native_id:NATIVE_ID,tag:canonTag('launch-1'),workspace:NATIVE_WS},tree:{state:'removed',reason:'host-reported'}}),host('hetzner'));
    expect(ack.status,await ack.clone().text()).toBe(200);
    const stored=JSON.parse(sql.exec('SELECT ack_json FROM session_control_commands WHERE id=?',command.id).toArray()[0].ack_json);
    expect(stored.tree.state).toBe('uncertain');expect(stored.tree.reason).toContain('authority_changed');
  }));
});

describe('m15c lifecycle view', () => {
  it('requires writer auth and scopes non-admin callers to their own actor', async () => fixture(async (s) => {
    expect((await s.handle(request('/api/sessions?view=lifecycle', undefined, 'GET'), unauth)).status).toBe(401);
    const { res, data } = await lifecycle(s, stranger);
    expect(res.status).toBe(200);
    expect(data.total).toBe(0);
    expect(data.groups.unknown).toEqual([]);
  }));

  it('projects a bound session with starter/owner refs and real engine/host', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-active');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const { res, data } = await lifecycle(s, agent());
    expect(res.status).toBe(200);
    expect(data.total).toBe(1);
    const entry = data.groups.active[0] ?? data.groups.unknown[0];
    expect(entry).toBeTruthy();
    expect(entry.refs.starter).toBe('demo:assistant');
    expect(entry.refs.owner).toBe('demo:assistant');
    expect(entry.refs.engine).toBe('codex');
    expect(entry.refs.machine_id).toBe('hetzner');
    expect(entry.decision).toBeNull();
  }));

  it('stores machine-scoped observations and drops JSON owner flags', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-obs');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const key = `hetzner/${NATIVE_ID}`;
    const res = await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID,
      payload: { limit_hit: true, limit_window: '5h', owner: 'intruder', human_owned: true },
    }), host('hetzner'));
    expect(res.status, await res.clone().text()).toBe(201);
    // A host cannot write for another machine; agent writers cannot observe.
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'elsewhere', native_id: NATIVE_ID, payload: {},
    }), host('hetzner'))).status).toBe(404);
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID, payload: {},
    }), agent())).status).toBe(403);
    const { data } = await lifecycle(s, agent());
    const entry = data.groups.limit_blocked.find((e: any) => e.session_key === key);
    expect(entry).toBeTruthy();
    expect(entry.refs.owner).toBe('demo:assistant');
    expect(entry.refs.human_owned).toBe(false);
  }));

  it('records owner-only limit-block decisions durably and refuses others', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-decide');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const key = `hetzner/${NATIVE_ID}`;
    const reset = new Date(Date.now() + 3600000).toISOString();
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID,
      payload: { limit_hit: true, limit_window: '5h', limit_reset_at: reset },
    }), host('hetzner'))).status).toBe(201);
    const decided = await s.handle(request('/api/sessions/decisions', {
      session_key: key, kind: 'resume_on_reset', reason: 'resume on quota reset', engine: 'codex',
    }), agent());
    expect(decided.status, await decided.clone().text()).toBe(201);
    // Strangers and admins never hold another coordinator's ownership.
    expect((await s.handle(request('/api/sessions/decisions', {
      session_key: key, kind: 'drop', reason: 'nope',
    }), stranger)).status).toBe(404);
    expect((await s.handle(request('/api/sessions/decisions', {
      session_key: key, kind: 'drop', reason: 'nope',
    }), admin)).status).toBe(404);
    // The decision persists and feeds the projection.
    const { data } = await lifecycle(s, agent());
    const entry = data.groups.limit_blocked.find((e: any) => e.session_key === key);
    expect(entry?.decision?.kind).toBe('resume_on_reset');
    expect(entry?.decision?.actor).toBe('demo:assistant');
    expect(entry.reasons.join(' ')).toContain('decision:resume_on_reset');
  }));

  it('dry-runs cleanup without mutation and applies via real M12 stop intents', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-done');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const key = `hetzner/${NATIVE_ID}`;
    const now = new Date().toISOString();
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID,
      payload: { report_written: true, report_at: now, screen_done: true, authoritative_no_pending_proof: true },
    }), host('hetzner'))).status).toBe(201);
    // Worktree-guard: run the legacy eligibility flow with the linked intake closed.
    closeFixtureIntake(sql);
    const count = () => Number(sql.exec('SELECT COUNT(*) AS n FROM session_lifecycle_decisions').one().n)
      + Number(sql.exec('SELECT COUNT(*) AS n FROM session_lifecycle_observations').one().n)
      + Number(sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').one().n);
    const before = count();
    const dry = await s.handle(request('/api/sessions/cleanup', { dry_run: true }), agent());
    const plan = await dry.json() as any;
    expect(dry.status).toBe(200);
    expect(plan.dry_run).toBe(true);
    expect(plan.eligible.map((e: any) => e.session_key)).toContain(key);
    expect(count()).toBe(before);
    // M15d REAL apply: enqueues one M12 stop intent for the eligible own
    // session (no second store; the host executes it via claim/ack).
    const applied = await s.handle(request('/api/sessions/cleanup', { apply: true, key: 'm15c-op' }), agent());
    expect(applied.status, await applied.clone().text()).toBe(200);
    const done = await applied.json() as any;
    expect(done.apply).toBe(true);
    expect(done.enqueued.map((e: any) => e.session_key)).toContain(key);
    expect((sql.exec("SELECT COUNT(*) AS n FROM session_control_commands WHERE kind = 'stop'").one() as any).n).toBe(1);
    // Only the M12 stop intent was added: no observation/decision/session rows.
    expect(count()).toBe(before);
  }));

  it('never grants admin another coordinator cleanup and protects human records', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-guard');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    const now = new Date().toISOString();
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: NATIVE_ID,
      payload: { report_written: true, report_at: now, screen_done: true, authoritative_no_pending_proof: true },
    }), host('hetzner'))).status).toBe(201);
    // Admin dry-run: owned by demo:assistant, so nothing is eligible for the admin actor.
    const dry = await s.handle(request('/api/sessions/cleanup', { dry_run: true }), admin);
    const plan = await dry.json() as any;
    expect(dry.status).toBe(200);
    expect(plan.eligible).toEqual([]);
    // Human-owned canonical record: protected from decisions and cleanup.
    const humanKey = 'hetzner/33333333-3333-4333-8333-333333333333';
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
      VALUES ('hetzner', '33333333-3333-4333-8333-333333333333', 'hub-launch-h', 'codex', 'running', NULL, ?, '/srv/h', NULL, NULL, 'fixture-acct', NULL)`, now);
    sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES ('launch-h', 'human:alexey', 'human', NULL, 'fixture', 'fleet-launch-h', '{}', 'hetzner', 'codex', 'fixture-acct', NULL, 'headless', 'demo', 'goal', 'running', '33333333-3333-4333-8333-333333333333', NULL, ?, ?)`, now, now);
    expect((await s.handle(request('/api/sessions/observations', {
      machine_id: 'hetzner', native_id: '33333333-3333-4333-8333-333333333333',
      payload: { limit_hit: true, limit_window: 'weekly' },
    }), host('hetzner'))).status).toBe(201);
    expect((await s.handle(request('/api/sessions/decisions', {
      session_key: humanKey, kind: 'drop', reason: 'protected',
    }), admin)).status).toBe(404);
    const scoped = await s.handle(request('/api/sessions/cleanup?owner=human%3Aalexey', { dry_run: true }), admin);
    const scopedPlan = await scoped.json() as any;
    expect(scopedPlan.eligible).toEqual([]);
  }));

  it('surfaces fleet-observed sessions without canonical launch with truthful unknowns', async () => fixture(async (s, sql) => {    const now = new Date().toISOString();
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
      VALUES ('hetzner', '44444444-4444-4444-8444-444444444444', 'stray', 'codex', 'running', NULL, ?, '/srv/stray', NULL, NULL, 'fixture-acct', NULL)`, now);
    const { data } = await lifecycle(s, admin);
    // Fresh running provenance still classifies active; ownership stays
    // truthfully unknown (never invented, never blocks the projection).
    const entry = data.groups.active.find((e: any) => e.session_key === 'hetzner/44444444-4444-4444-8444-444444444444');
    expect(entry).toBeTruthy();
    expect(entry.refs.owner).toBeNull();
    expect(entry.refs.engine).toBe('codex');
  }));

  it('joins intake task links via canonical hub_session_id', async () => fixture(async (s, sql) => {
    const created = await startAgentSession(s, 'm15c-linked');
    await bindAgentSession(s, sql, created.id, 'launch-1');
    sql.exec('INSERT OR REPLACE INTO intake_tasks (id, data) VALUES (?, ?)', 'task-1', JSON.stringify({
      id: 'task-1',
      history: [
        { kind: 'link', native_id: null, hub_session_id: created.id, routing_job_id: null },
        { kind: 'report', reference: '/synth/report.md' },
      ],
    }));
    const { data } = await lifecycle(s, agent());
    const entry = data.groups.active.find((e: any) => e.session_key === `hetzner/${NATIVE_ID}`);
    expect(entry).toBeTruthy();
    expect(entry.refs.task_refs).toContain('task-1');
    expect(entry.refs.report_refs).toContain('/synth/report.md');
  }));
});
