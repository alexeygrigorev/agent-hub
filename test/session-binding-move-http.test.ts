import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import { FIXTURE_INTAKE_ID, seedFixtureIntake } from './tbm-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const nativeId = '11111111-1111-4111-8111-111111111111';
const sessionId = '22222222-2222-4222-8222-222222222222';
const launchId = '33333333-3333-4333-8333-333333333333';
const oldWorkspace = '/srv/project';
const workspace = '/srv/project-tree';
const now = Date.now();
const created = now - 60000;
const tag = 'hub-' + launchId;
const prior = { native_id: nativeId, tag, workspace: oldWorkspace, native_created_at_ms: created, generation: 1 };
const next = { native_id: nativeId, tag, workspace, native_created_at_ms: created };
const host = { caller: { kind: 'host', machineId: 'host', tokenId: 'host-cred', actor: 'machine:host' }, scopes: ['host'] };
const admin = { caller: { kind: 'human', isAdmin: true, actor: 'operator' }, scopes: ['admin'] };
const request = (path: string, body?: any) => new Request('https://hub.test' + path,
  body === undefined ? { method: 'GET' } : { method: 'POST', body: JSON.stringify(body) });

async function fixture(fn: (service: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName('move-' + crypto.randomUUID())),
    async (hub: Hub, state: DurableObjectState) => {
      const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), () => now);
      service.init();
      const sql = hub.sql;
      seedFixtureIntake(sql);
      const at = new Date(now).toISOString();
      sql.exec(`INSERT INTO credentials (id,kind,token_hash,refresh_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at)
        VALUES ('host-cred','host','host-hash','host-refresh','host-family','host','host','["host"]','operator',?,?)`, at, new Date(now + 86400000).toISOString());
      sql.exec("INSERT INTO machines(id,name,hostname,credential_id,registered_at) VALUES ('host','host','host','host-cred',?)", at);
      sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
        VALUES (?,'operator','human','fixture','move-launch',?,'host','codex','fixture','interactive','demo','fixture','running',?,?,?)`,
        launchId, JSON.stringify({ workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID }), nativeId, at, at);
      sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,host_family,created_at,updated_at)
        VALUES (?,'operator','move-session',?,?,'host','codex','interactive','running',?,'host-family',?,?)`,
        sessionId, JSON.stringify({ workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID }), launchId, JSON.stringify(prior), at, at);
      sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,cwd,created_at_ms)
        VALUES ('host',?,?,'codex','running',?,?,?,?)`, nativeId, tag, at, workspace, workspace, created);
      sql.exec(`INSERT INTO intake_worktrees(id,task_id,machine,path,created_at,updated_at,origin,state)
        VALUES ('tree',?,'host',?,?,?,'hub_started','active')`, FIXTURE_INTAKE_ID, workspace, now, now);
      await fn(service, sql);
    });
}

const repair = (s: SessionControlService, native: any = next, auth: any = host) =>
  s.handle(request(`/api/sessions/${sessionId}/binding`, { native }), auth);
const binding = (sql: any) => JSON.parse(sql.exec('SELECT binding_json FROM session_control_sessions WHERE id=?', sessionId).one().binding_json);

describe('host-authenticated workspace binding repair', () => {
  it('rediscovers a bound move and repairs the same native incarnation', async () => fixture(async (s, sql) => {
    const at = new Date(now).toISOString();
    sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,actor,registry_actor,scopes,created_by_sub,created_at,expires_at)
      VALUES ('messaging','agent','msg-hash','msg-refresh','msg-family','session',?,?, '["msg:send"]','host',?,?)`,
      'session:' + sessionId, 'session:' + sessionId, at, new Date(now + 86400000).toISOString());
    const pending = await s.handle(request('/api/sessions/host-bindings'), host);
    expect(pending.status).toBe(200);
    expect((await pending.json() as any).sessions).toContainEqual(expect.objectContaining({ id: sessionId, native_id: nativeId }));
    const response = await repair(s);
    expect(response.status, await response.clone().text()).toBe(200);
    expect(binding(sql)).toMatchObject({ ...next, generation: 2, reported_by: 'host' });
    expect(sql.exec("SELECT revoked_at FROM credentials WHERE id='messaging'").one().revoked_at).toBe(at);
    expect((await repair(s)).status).toBe(200);
    expect(binding(sql).generation).toBe(2);
    const settled = await s.handle(request('/api/sessions/host-bindings'), host);
    expect((await settled.json() as any).sessions).toEqual([]);
  }));

  it('does not permit operator or internal-seam workspace rebinds', async () => fixture(async (s, sql) => {
    expect((await repair(s, next, admin)).status).toBe(403);
    expect(s.recordBinding(sessionId, next)).toMatchObject({ ok: false });
    expect(binding(sql)).toEqual(prior);
  }));

  it.each([
    "UPDATE intake_worktrees SET task_id='foreign'",
    "UPDATE intake_worktrees SET state='removed'",
    "UPDATE intake_worktrees SET machine='other-host'",
    "UPDATE fleet_sessions SET created_at_ms=created_at_ms+1",
    "UPDATE fleet_sessions SET observed_at='2000-01-01T00:00:00.000Z'",
    "UPDATE fleet_sessions SET engine='shell'",
    "UPDATE fleet_sessions SET phase='exited'",
  ])('refuses unproven move without changing binding: %s', async mutation => fixture(async (s, sql) => {
    sql.exec(mutation);
    const response = await repair(s);
    expect(response.status).toBe(409);
    expect(binding(sql)).toEqual(prior);
  }));

  it('rejects a recycled native incarnation even when host reports it', async () => fixture(async (s, sql) => {
    sql.exec('UPDATE fleet_sessions SET created_at_ms=?', created + 1);
    expect((await repair(s, { ...next, native_created_at_ms: created + 1 })).status).toBe(409);
    expect(binding(sql)).toEqual(prior);
  }));

  const commandId = '44444444-4444-4444-8444-444444444444';
  const correlation = `cmd:${commandId}:gen:1`;
  function seedCommand(sql: any) {
    const at = new Date(now).toISOString();
    sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
      VALUES (?,?,'capture','operator','capture-old','{}','recorded',1,?,?,?)`, commandId, sessionId, correlation, at, at);
  }
  it('fences an unclaimed old offer and reoffers it at the new generation', async () => fixture(async (s, sql) => {
    seedCommand(sql);
    const offered = await s.handle(request('/api/session-commands/offers'), host);
    expect((await offered.json() as any).offers).toContainEqual(expect.objectContaining({ command_id: commandId, generation: 1 }));
    expect((await repair(s)).status).toBe(200);
    const stale = await s.handle(request(`/api/session-commands/${commandId}/claim`, {
      correlation, attempt_id: '55555555-5555-4555-8555-555555555555',
    }), host);
    expect(stale.status).toBe(409);
    const reoffered = await s.handle(request('/api/session-commands/offers'), host);
    expect((await reoffered.json() as any).offers).toContainEqual(expect.objectContaining({ command_id: commandId, generation: 2 }));
  }));
  it('accepts wakes after binding repair and marks delivered only after a native inbox receipt', async () => fixture(async (s, sql) => {
    expect((await repair(s)).status).toBe(200);
    const wakeRes = await s.handle(request(`/api/sessions/${sessionId}/wake`, {
      key: 'wake-after-move',
      at: 'now',
      prompt: 'continue after worktree move',
    }), admin);
    expect(wakeRes.status).toBe(201);
    const wakeBody = await wakeRes.json() as any;
    expect(wakeBody).toMatchObject({
      session_id: sessionId,
      wake_state: 'queued',
      binding: expect.objectContaining({ workspace, generation: 2 }),
    });
    const offered = await s.handle(request('/api/session-commands/offers'), host);
    const offer = (await offered.json() as any).offers.find((candidate: any) => candidate.command_id === wakeBody.command_id);
    expect(offer).toMatchObject({
      command_id: wakeBody.command_id,
      kind: 'send',
      generation: 2,
      binding: expect.objectContaining({
        native_id: nativeId,
        tag,
        workspace,
        generation: 2,
        native_created_at_ms: created,
      }),
    });
    const attemptId = '55555555-5555-4555-8555-555555555555';
    const claimResponse = await s.handle(request(`/api/session-commands/${wakeBody.command_id}/claim`, {
      correlation: offer.correlation,
      attempt_id: attemptId,
    }), host);
    expect(claimResponse.status).toBe(200);
    const claim = await claimResponse.json() as any;
    const receipt = await s.handle(request(`/api/session-commands/${wakeBody.command_id}/status`, {
      claim_token: claim.claim_token,
      status: 'waiting in inbox for recipient',
    }), host);
    expect(receipt.status, await receipt.clone().text()).toBe(200);
    expect(sql.exec('SELECT state, delivered_at FROM session_control_wakes WHERE id=?', wakeBody.wake_id).one())
      .toMatchObject({ state: 'delivered', delivered_at: expect.any(String) });
  }));
  it('preserves existing execution custody instead of rebinding underneath it', async () => fixture(async (s, sql) => {
    seedCommand(sql);
    const claimed = await s.handle(request(`/api/session-commands/${commandId}/claim`, {
      correlation, attempt_id: '55555555-5555-4555-8555-555555555555',
    }), host);
    expect(claimed.status).toBe(200);
    expect((await repair(s)).status).toBe(409);
    expect(binding(sql)).toEqual(prior);
    expect(sql.exec('SELECT state FROM session_control_commands WHERE id=?', commandId).one().state).toBe('claimed');
  }));
});
