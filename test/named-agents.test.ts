import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';
import {
  afterMessageInsert,
  authorizedNamedAgent,
  namedForwardSession,
  setNamedAgentForward,
  updateNamedAgentDelegates,
} from '../src/session-messaging';
import { handleNamedAgentRequest } from '../src/named-agents';

const SESSION = '22222222-2222-4222-8222-222222222222';
const OTHER_SESSION = '44444444-4444-4444-8444-444444444444';

async function fixture(fn: (ctx: { sql: any; call: (path: string, body?: any, caller?: any) => Promise<Response>; route: (path: string, body?: any, caller?: any) => Promise<Response>; hub: Hub }) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('named-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const sql = hub.sql;
    const call = (path: string, body?: any, caller: any = { isAdmin: true, actor: 'human:operator' }) =>
      handleNamedAgentRequest(sql, f => state.storage.transactionSync(f), new Request('https://hub.test/api/agents/named/' + path,
        { method: body === undefined ? 'GET' : 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) }), caller)
        .then(response => response ?? new Response(null, { status: 404 }));
    const route = (path: string, body?: any, caller: any = { isAdmin: true, actor: 'human:operator' }) => {
      (hub as any).authenticateBusRequest = async () => ({ caller, scopes: ['admin'] });
      return hub.fetch(new Request('https://hub.test/api/agents/named/' + path,
        { method: body === undefined ? 'GET' : 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) }));
    };
    await fn({ sql, call, route, hub });
  });
}

describe('named agent lifecycle', () => {
  it('does not mint a duplicate when a legacy named-agent credential is still active', () => fixture(async ({ sql, call }) => {
    const now = new Date().toISOString();
    const future = new Date(Date.now() + 86400000).toISOString();
    sql.exec("INSERT INTO agents (tag, project, registered_at) VALUES ('developer', 'agent', ?)", now);
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('legacy-agent-token', 'agent', 'legacy-hash', 'legacy-family', 'agent:developer', 'agent:developer',
        '["bus:send","bus:read"]', 'operator', ?, ?, 'agent:developer')`, now, future);
    expect((await call('developer/create', {})).status).toBe(409);
    expect(await (await call('developer')).json()).toMatchObject({ actor: 'agent:developer', credential_family: 'legacy-family' });
  }));

  it('creates a separate messaging-only credential once and restricts administration to operators', () => fixture(async ({ sql, call, route }) => {
    expect((await route('developer/create', {}, { actor: 'agent:other', kind: 'agent' })).status).toBe(403);
    const created = await route('developer/create', {});
    expect(created.status).toBe(201);
    const cred: any = await created.json();
    expect(cred.actor).toBe('agent:developer');
    expect(cred.scopes).toEqual(['msg:send', 'msg:read']);
    const stored: any = sql.exec('SELECT * FROM credentials WHERE actor = ?', 'agent:developer').one();
    expect(stored.token_hash).not.toBe(cred.access_token);
    expect(stored.registry_actor).toBe('agent:developer');
    expect(JSON.parse(stored.scopes)).toEqual(['msg:send', 'msg:read']);
    expect((await call('developer/create', {})).status).toBe(409);
    expect(sql.exec('SELECT COUNT(*) AS n FROM credentials WHERE actor = ?', 'agent:developer').one().n).toBe(1);
    const shown: any = await (await route('developer')).json();
    expect(shown).toMatchObject({ actor: 'agent:developer', display: 'developer', forward_session_id: null, delegates: [] });
    expect(JSON.stringify(shown)).not.toContain(cred.access_token);
    expect((await call('root/create', {})).status).toBe(400);
  }));

  it('authorizes explicit credential families and keeps MCP client siblings isolated', () => fixture(async ({ sql, call }) => {
    await call('developer/create', {});
    const now = new Date().toISOString();
    const future = new Date(Date.now() + 86400000).toISOString();
    sql.exec('CREATE TABLE IF NOT EXISTS mcp_grants (family TEXT PRIMARY KEY, client TEXT, resource TEXT, expires INTEGER)');
    for (const [family, client, revoked] of [['family-a', 'selected', null], ['family-b', 'other', null], ['family-old', 'selected', now]]) {
      sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
        VALUES (?, 'human', ?, ?, 'operator', 'human:operator', '["admin"]', 'operator', ?, ?, ?)`, family, family, family, now, future, revoked);
      sql.exec('INSERT INTO mcp_grants VALUES (?, ?, ?, ?)', family, client, 'https://hub.test/mcp', Date.now() + 86400000);
    }
    expect((await call('developer/delegate', { mcp_client: 'missing' })).status).toBe(404);
    const granted: any = await (await call('developer/delegate', { mcp_client: 'selected' })).json();
    expect(granted.delegates).toEqual(['family-a']);
    expect(authorizedNamedAgent(sql, 'agent:developer', 'family-a')).toBe(true);
    expect(authorizedNamedAgent(sql, 'agent:developer', 'family-b')).toBe(false);
    expect(authorizedNamedAgent(sql, 'agent:developer', undefined)).toBe(false);
    expect(authorizedNamedAgent(sql, 'agent:oncall', 'family-a')).toBe(false);
    sql.exec('UPDATE credentials SET id = ? WHERE family_id = ?', 'rotated-token-id', 'family-a');
    expect(authorizedNamedAgent(sql, 'agent:developer', 'family-a')).toBe(true);
    updateNamedAgentDelegates(sql, 'developer', ['family-a'], true);
    expect(authorizedNamedAgent(sql, 'agent:developer', 'family-a')).toBe(false);
    expect((await call('developer/delegate', { family: 'family-b', mcp_client: 'selected' })).status).toBe(400);
  }));

  it('forwards stable coordinator messages to the current session and delegates only that session family', () => fixture(async ({ sql, call }) => {
    expect((await (await call('orchestrator-main')).json() as any).actor).toBe('agent:orchestrator-main');
    expect((await (await call('oncall')).json() as any).actor).toBe('agent:oncall');
    expect(sql.exec("SELECT tag FROM agents WHERE project = 'agent'").toArray()).toEqual([]);
    const now = new Date().toISOString();
    for (const [id, key] of [[SESSION, 'forward-1'], [OTHER_SESSION, 'forward-2']]) {
      sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, engine, mode, state, created_at, updated_at)
        VALUES (?, 'admin', ?, '{}', 'codex', 'interactive', 'running', ?, ?)`, id, key, now, now);
    }
    expect((await call('oncall/forward', { to: 'session:' + SESSION })).status).toBe(200);
    expect(namedForwardSession(sql, 'agent:oncall')).toBe(SESSION);
    expect(sql.exec("SELECT tag FROM agents WHERE project = 'session' AND tag = ?", SESSION).one().tag).toBe(SESSION);

    const at = Date.now();
    const future = new Date(at + 86400000).toISOString();
    sql.exec(`INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
      VALUES ('session-token', 'agent', 'session-hash', 'session-family', ?, ?, '["msg:send","msg:read"]', 'operator', ?, ?, ?)`,
      'session:' + SESSION, 'session:' + SESSION, now, future, 'session:' + SESSION);
    expect(authorizedNamedAgent(sql, 'agent:oncall', 'session-family')).toBe(true);

    const messageId = '01JD3K7QXYZABCDEFGHJKMNPQR';
    sql.exec(`INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at)
      VALUES (?, 'agent:developer', 'agent:oncall', 'note', 'thread-1', 'check status', 'named-forward-1', ?, ?)`,
      messageId, at, at + 86400000);
    afterMessageInsert(sql, { id: messageId, from_actor: 'agent:developer', to_actor: 'agent:oncall', kind: 'note',
      thread_id: 'thread-1', reply_to: null, body: 'check status' }, { now: at });
    const row: any = sql.exec('SELECT to_actor, delivery, to_session_id FROM messages WHERE id = ?', messageId).one();
    expect(row).toEqual({ to_actor: 'agent:oncall', delivery: 'tui', to_session_id: SESSION });
    const command: any = sql.exec("SELECT payload_json FROM session_control_commands WHERE kind = 'message' AND session_id = ?", SESSION).one();
    expect(JSON.parse(command.payload_json).body).toContain('from agent:developer');
    expect(JSON.parse(command.payload_json).body).toContain(`hub msg reply ${messageId} --as agent:oncall`);

    setNamedAgentForward(sql, 'oncall', OTHER_SESSION);
    expect(authorizedNamedAgent(sql, 'agent:oncall', 'session-family')).toBe(false);
    expect(namedForwardSession(sql, 'agent:oncall')).toBe(OTHER_SESSION);
    await call('oncall/forward', { to: null });
    expect(namedForwardSession(sql, 'agent:oncall')).toBeNull();
  }));

  it('updates a named agent forward when an operator starts a matching session', () => fixture(async ({ sql, hub }) => {
    const now = new Date().toISOString();
    sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, engine, mode, state, created_at, updated_at)
      VALUES (?, 'human:operator', 'start-oncall', '{"name":"orchestrator-main"}', 'codex', 'interactive', 'running', ?, ?)`, SESSION, now, now);
    (hub as any).sessionControlAuth = async () => ({ caller: { kind: 'human', actor: 'human:operator', isAdmin: true }, scopes: ['admin'] });
    (hub as any).sessionControlService.handle = async () => Response.json({ id: SESSION }, { status: 201 });
    const response = await hub.fetch(new Request('https://hub.test/api/sessions', {
      method: 'POST', body: JSON.stringify({ name: 'orchestrator-main', key: 'start-oncall' }),
    }));
    expect(response.status).toBe(201);
    expect(namedForwardSession(sql, 'agent:orchestrator-main')).toBe(SESSION);
  }));
});
