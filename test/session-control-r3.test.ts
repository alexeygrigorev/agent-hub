import { describe, expect, it } from 'vitest';
import { FIXTURE_INTAKE_ID, seedFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const agent = (tokenId = 'cred-1', actor = 'demo:assistant') => ({ caller: { kind: 'agent', actor, tokenId }, scopes: ['fleet:launch', 'bus:send'] });
const host = (id: string, tokenId = 'host-cred-1') => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id, tokenId }, scopes: ['host'] });

const NATIVE_ID = '22222222-2222-4222-8222-222222222222';
const NATIVE_WS = '/srv/hub/demo';
const NATIVE_CREATED = 1792223000000;
const canonTag = (launchId: string) => 'hub-' + launchId;

/** Synthetic placement only: never starts a live agent. */
function fakeReserve() {
  const calls: any[] = [];
  return { calls, hook: (input: any) => {
    calls.push(input);
    return { value: { id: 'launch-' + calls.length, state: 'offered', machine: input.machine || 'hetzner', engine: input.engine || 'codex' }, status: 201 };
  } };
}

async function fixture(fn: (s: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-r3-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fake = fakeReserve();
    const service = new SessionControlService(hub.sql, (cb) => state.storage.transactionSync(cb), Date.now, { reserve: fake.hook });
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
    await fn(service, hub.sql);
  });
}

/** A chunked body that runs one SQL mutation mid-stream (proven H1 pattern):
 * the authority the handler checked BEFORE the await must be re-verified
 * INSIDE the mutation txn AFTER it. */
function streamedBody(sql: any, payload: any, mutate: () => void) {
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  let step = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (step === 0) { step = 1; controller.enqueue(bytes.slice(0, 8)); return; }
      mutate();
      controller.enqueue(bytes.slice(8));
      controller.close();
    },
  });
}

function streamRequest(sql: any, path: string, payload: any, mutate: () => void) {
  return new Request('https://hub.test' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: streamedBody(sql, payload, mutate),
  });
}

describe('hub session control R3: post-body authority inside the mutation txn', () => {
  it('R3-1 host revoked mid-body cannot bind or adopt a family', async () => fixture(async (s, sql) => {
    const created = await (await s.handle(new Request('https://hub.test/api/sessions',
      { method: 'POST', body: JSON.stringify({ key: 'r3-b1', workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID }) }), agent())).json() as any;
    const sessionId = created.id;
    const launchId = created.fleet_launch_id;
    const at = new Date().toISOString();
    sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES (?, 'test-admin', 'human', NULL, 'fixture', 'fleet-x', '{}', 'hetzner', 'codex', 'a', NULL, 'headless', 'demo', 'g', 'running', ?, NULL, ?, ?)`,
      launchId, NATIVE_ID, at, at);
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
      VALUES ('hetzner', ?, ?, 'codex', 'running', NULL, ?, ?, NULL, NULL, 'a', NULL)`,
      NATIVE_ID, canonTag(launchId), at, NATIVE_WS);
    const res = await s.handle(streamRequest(sql, `/api/sessions/${sessionId}/binding`,
      { native: { native_id: NATIVE_ID, tag: canonTag(launchId), workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } },
      () => { sql.exec(`UPDATE credentials SET revoked_at = ? WHERE id = 'host-cred-1'`, new Date().toISOString()); }), host('hetzner'));
    const data = await res.json() as any;
    expect(res.status, JSON.stringify(data)).toBe(409);
    const row: any = sql.exec('SELECT state, binding_json, host_family FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
    expect(row.state).not.toBe('running');
    expect(row.binding_json).toBeNull();
    expect(row.host_family).toBeNull();
  }));

  it('R3-2 owner revoked mid-body enqueues nothing and stamps no NULL family', async () => fixture(async (s, sql) => {
    const created = await (await s.handle(new Request('https://hub.test/api/sessions',
      { method: 'POST', body: JSON.stringify({ key: 'r3-s1', workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID }) }), agent())).json() as any;
    const sessionId = created.id;
    const launchId = created.fleet_launch_id;
    const at = new Date().toISOString();
    sql.exec(`INSERT OR REPLACE INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, native_id, launcher_task_id, created_at, updated_at)
      VALUES (?, 'test-admin', 'human', NULL, 'fixture', 'fleet-y', '{}', 'hetzner', 'codex', 'a', NULL, 'headless', 'demo', 'g', 'running', ?, NULL, ?, ?)`,
      launchId, NATIVE_ID, at, at);
    sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at)
      VALUES ('hetzner', ?, ?, 'codex', 'running', NULL, ?, ?, NULL, NULL, 'a', NULL)`,
      NATIVE_ID, canonTag(launchId), at, NATIVE_WS);
    const bound = await s.handle(new Request(`https://hub.test/api/sessions/${sessionId}/binding`,
      { method: 'POST', body: JSON.stringify({ native: { native_id: NATIVE_ID, tag: canonTag(launchId), workspace: NATIVE_WS, native_created_at_ms: NATIVE_CREATED } }) }), host('hetzner'));
    expect(bound.status).toBe(200);
    const res = await s.handle(streamRequest(sql, `/api/sessions/${sessionId}/send`,
      { key: 'r3-send-1', body: 'hello after revoke' },
      () => { sql.exec(`UPDATE credentials SET revoked_at = ? WHERE id = 'cred-1'`, new Date().toISOString()); }), agent());
    expect([403, 404]).toContain(res.status);
    const cmds: any[] = sql.exec('SELECT id, actor, owner_family FROM session_control_commands WHERE session_id = ?', sessionId).toArray() as any[];
    expect(cmds.length).toBe(0);
  }));

  it('R3-3 creator revoked mid-body creates no session row', async () => fixture(async (s, sql) => {
    const res = await s.handle(streamRequest(sql, '/api/sessions',
      { key: 'r3-c1', workspace: 'demo', intake_task_id: FIXTURE_INTAKE_ID },
      () => { sql.exec(`UPDATE credentials SET revoked_at = ? WHERE id = 'cred-1'`, new Date().toISOString()); }), agent());
    expect(res.status).toBe(403);
    const rows: any[] = sql.exec('SELECT id FROM session_control_sessions WHERE request_key = ?', 'r3-c1').toArray() as any[];
    expect(rows.length).toBe(0);
  }));
});
