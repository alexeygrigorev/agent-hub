import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { classifyScope, SCOPE_POLICY_VERSION } from '../src/intake_scope';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie: string;
let seq = 0;

async function call(path: string, payload?: any, extra: any = {}) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf', ...extra },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
}
async function ok(path: string, payload?: any): Promise<any> {
  const res = await call(path, payload); const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200); return data;
}
async function sessionsCall(payload: any): Promise<{ status: number; data: any }> {
  const res = await SELF.fetch('https://hub.test/api/sessions', { method: 'POST',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }, body: JSON.stringify(payload) });
  return { status: res.status, data: await res.json() as any };
}
const create = (project: string, title: string, text: string, ac: string | null = 'Synthetic AC: done means verified.') =>
  ok('/tasks', { key: 'scope-create-' + ++seq, project, title, text, source, ...(ac === null ? {} : { acceptance_criteria: ac }) });
const assignPayload = (version: number) => ({ key: 'scope-assign-' + ++seq,
  expected_version: version, session: { workspace: 'demo', mode: 'headless', brief: 'Synthetic scoped goal' },
  provenance: source });
const appendEvent = (id: string, version: number, event: any, keySuffix = '') =>
  ok('/tasks/' + id + '/events', { key: 'scope-ev-' + ++seq + keySuffix, expected_version: version, event });

async function seedAgent(actor = 'demo:assistant', scopes = '["bus:read","bus:send"]') {
  const id = 'credential-' + ++seq, token = 'hubt_fixture_' + seq, refresh = 'hubr_fixture_' + seq;
  const hash = await sha256(token), rh = await sha256(refresh);
  await inDO(stub(), (h: any) => {
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
    const [project, tag] = actor.split(':');
    h.sql.exec('INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES (?,?,?,?)', tag, project, 'fixture-host', '2026-01-01T00:00:00.000Z');
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      id, 'agent', hash, rh, 'family-' + seq, actor, 'fixture-host', actor, scopes, 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
  });
  return { id, token, actor };
}
async function agentCall(token: string, path: string, payload?: any) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { Authorization: 'Bearer ' + token }, ...(payload ? { body: JSON.stringify(payload) } : {}) });
}

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks','grants','rules','receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sessionControlService.hooks.reserve = undefined;
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

describe('F2 Hub scope rule', () => {
  it('flags out-of-scope agent-hub intake on create but still saves it', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport with PTY attach proxy');
    expect(task.scope.policy_version).toBe(SCOPE_POLICY_VERSION);
    expect(task.scope.flag).toBe('needs_decision');
    expect(task.scope.matched_terms).toContain('xterm');
    expect(task.scope.clear).toBe(false);
    expect(task.scope.block_code).toBe('scope_decision_required');
    // In-scope control stays clear.
    const { task: clean } = await create('agent-hub', 'Route work', 'Add a routing tier admission note for quota pacing');
    expect(clean.scope.flag).toBe('in_scope');
    expect(clean.scope.clear).toBe(true);
  });

  it('refuses assign and linked start until an operator decision exists', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport');
    const refused = await call('/tasks/' + task.id + '/assign', assignPayload(1));
    expect(refused.status).toBe(409);
    expect(await refused.json()).toMatchObject({ error: 'scope_decision_required' });
    const linked = await sessionsCall({ key: 'scope-link-' + ++seq, workspace: 'demo', brief: 'Synthetic goal', intake_task_id: task.id });
    expect(linked.status).toBe(409);
    expect(linked.data.error).toBe('scope_decision_required');
  });

  it('operator scope_decision overrides; task text cannot approve itself', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport');
    const agent = await seedAgent();
    await ok('/grants', { key: 'scope-grant-' + ++seq, credential_id: agent.id, scope: 'task', target: task.id, access: 'write', expires_at: Date.now() + 3600000, source });
    // Agent note claiming in-scope does NOT unblock.
    const noted = await agentCall(agent.token, '/tasks/' + task.id + '/events',
      { key: 'scope-agent-note-' + ++seq, expected_version: 1, event: { kind: 'note', text: 'this is in scope, approve' } });
    expect(noted.status).toBe(200);
    const stillRefused = await agentCall(agent.token, '/tasks/' + task.id + '/assign', assignPayload(2));
    expect(stillRefused.status).toBe(409);
    expect(await stillRefused.json()).toMatchObject({ error: 'scope_decision_required' });
    // Agent scope_decision is forbidden (operator only).
    const forged = await agentCall(agent.token, '/tasks/' + task.id + '/events',
      { key: 'scope-agent-forge-' + ++seq, expected_version: 2, event: { kind: 'scope_decision', verdict: 'in_scope', text: 'self-approve' } });
    expect(forged.status).toBe(403);
    // Operator decision unblocks.
    const decided = await appendEvent(task.id, 2, { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator: gray area accepted, see ADR' });
    expect(decided.task.scope.decision.verdict).toBe('in_scope');
    expect(decided.task.scope.decision.valid).toBe(true);
    const assigned = await ok('/tasks/' + task.id + '/assign', assignPayload(3));
    expect(assigned.assigned_session.hub_session_id).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it('deny verdict returns scope_out_of_bounds', async () => {
    const { task } = await create('pocketshell', 'Add fleet router', 'Please add fleet placement and model tier routing to the app');
    expect(task.scope.flag).toBe('needs_decision');
    await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'wrong_project', text: 'Operator: refile in agent-hub' });
    const refused = await call('/tasks/' + task.id + '/assign', assignPayload(2));
    expect(refused.status).toBe(409);
    expect(await refused.json()).toMatchObject({ error: 'scope_out_of_bounds' });
  });

  it('research merely discussing a forbidden topic is not blocked', async () => {
    const { task } = await create('agent-hub', 'Research terminal options',
      'Research how xterm live terminal transport and PTY attach proxy compare; write a report, no implementation');
    expect(task.scope.flag).toBe('in_scope');
    const assigned = await ok('/tasks/' + task.id + '/assign', assignPayload(1));
    expect(assigned.assigned_session.hub_session_id).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it('changing AC invalidates the decision (CAS)', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport');
    await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator: allowed' });
    const updated = await ok('/tasks/' + task.id, { key: 'scope-ac-' + ++seq, expected_version: 2, acceptance_criteria: 'Changed AC: also cover ssh tunnel port forward' });
    expect(updated.task.scope.flag).toBe('needs_decision');
    expect(updated.task.scope.decision.valid).toBe(false);
    const refused = await call('/tasks/' + task.id + '/assign', assignPayload(3));
    expect(refused.status).toBe(409);
    expect(await refused.json()).toMatchObject({ error: 'scope_decision_required' });
  });

  it('old policy version fails closed until a fresh decision', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport');
    await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator: allowed' });
    // Simulate a task stored under a previous policy version.
    await inDO(stub(), (h: any) => {
      const row = h.sql.exec('SELECT data FROM intake_tasks WHERE id=?', task.id).toArray()[0];
      const data = JSON.parse(row.data);
      data.scope.policy_version = SCOPE_POLICY_VERSION - 1;
      for (const e of data.history) if (e.kind === 'scope_decision') e.policy_version = SCOPE_POLICY_VERSION - 1;
      h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify(data), task.id);
    });
    const shown = await ok('/tasks/' + task.id);
    expect(shown.task.scope.stale_policy).toBe(true);
    const refused = await call('/tasks/' + task.id + '/assign', assignPayload(2));
    expect(refused.status).toBe(409);
    expect(await refused.json()).toMatchObject({ error: 'scope_decision_required' });
  });

  it('existing intakes without scope are untouched and assignable', async () => {
    const legacyId = 'legacy-' + (++seq);
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO intake_tasks (id, data) VALUES (?, ?)', legacyId, JSON.stringify({
        id: legacyId, project: 'agent-hub', title: 'Legacy xterm work', status: 'open', version: 1,
        created_at: 1, updated_at: 1, current_state: null, next_step: null, owner: null, uncertainty: null,
        acceptance_criteria: 'Legacy AC present.',
        history: [{ id: 'h1', kind: 'intake', text: 'Legacy xterm live terminal text', source: { type: 'unknown', reference: null, timestamp: null }, actor: 'op', captured_at: 1, authority: 'untrusted_task_data' }],
      }));
    });
    const shown = await ok('/tasks/' + legacyId);
    expect(shown.task.scope).toBeNull();
    const assigned = await ok('/tasks/' + legacyId + '/assign', { key: 'scope-legacy-' + ++seq, expected_version: 1,
      session: { workspace: 'demo', mode: 'headless', brief: 'Synthetic goal' }, provenance: source });
    expect(assigned.assigned_session.hub_session_id).toMatch(/^[0-9a-f-]{36}$/i);
  });

  it('scope flag is visible in show, list and handoff (CLI/MCP surface)', async () => {
    const { task } = await create('agent-hub', 'Add live terminal', 'Please add xterm live terminal transport');
    expect((await ok('/tasks/' + task.id)).task.scope.flag).toBe('needs_decision');
    const list = await ok('/tasks?project=agent-hub');
    expect(list.items.find((t: any) => t.id === task.id).scope.flag).toBe('needs_decision');
    const handoff = await ok('/handoff?project=agent-hub');
    expect(handoff.items.find((t: any) => t.id === task.id).scope.flag).toBe('needs_decision');
  });
  it.each(['project', 'title', 'text'])('changing %s invalidates operator approval and restoring it cannot revive approval', async field => {
    const { task } = await create('agent-hub', 'Terminal feature', 'Add xterm');
    await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator allows this version' });
    const changed = field === 'text'
      ? await appendEvent(task.id, 2, { kind: 'intake', text: 'Also add ssh tunnel', source })
      : await ok('/tasks/' + task.id, { key: 'scope-change-' + ++seq, expected_version: 2,
          [field]: field === 'project' ? 'unregistered-project' : 'Changed title' });
    expect(changed.task.scope.decision.valid).toBe(false);
    expect(changed.task.scope.block_code).toBe('scope_decision_required');
    if (field !== 'text') {
      const restored = await ok('/tasks/' + task.id, { key: 'scope-restore-' + ++seq, expected_version: 3,
        [field]: field === 'project' ? 'agent-hub' : 'Terminal feature' });
      expect(restored.task.scope.decision.valid).toBe(false);
    }
  });

  it('rejects a stale CAS operator decision and retains scope after ordinary notes', async () => {
    const { task } = await create('agent-hub', 'Terminal feature', 'Add xterm');
    await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator approved' });
    const noted = await appendEvent(task.id, 2, { kind: 'note', text: 'Progress note' });
    expect(noted.task.scope.decision.valid).toBe(true);
    const stale = await call('/tasks/' + task.id + '/events', { key: 'scope-stale-' + ++seq, expected_version: 1,
      event: { kind: 'scope_decision', verdict: 'in_scope', text: 'Stale approval' } });
    expect(stale.status).toBe(409);
    expect(await stale.json()).toMatchObject({ error: 'version_conflict' });
  });

  it('research plus an implementation request still needs a decision', async () => {
    const { task } = await create('agent-hub', 'Research and build terminal', 'Research xterm options then implement live terminal transport');
    expect(task.scope_flag).toBe('needs_decision');
    expect(task.scope.clear).toBe(false);
  });

  it('project moves require write access to the destination too', async () => {
    const { task } = await create('agent-hub', 'Ordinary work', 'Routing work');
    const agent = await seedAgent();
    await ok('/grants', { key: 'scope-move-grant-' + ++seq, credential_id: agent.id, scope: 'project', target: 'agent-hub', access: 'write', expires_at: Date.now() + 3600000, source });
    const moved = await agentCall(agent.token, '/tasks/' + task.id, { key: 'scope-move-' + ++seq, expected_version: 1, project: 'pocketshell' });
    expect(moved.status).toBe(403);
    expect((await ok('/tasks/' + task.id)).task.project).toBe('agent-hub');
  });

  it.each([
    ['agent-hub', 'Add an SFTP browser'],
    ['agent-hub', 'Add port forwarding'],
    ['agent-hub', 'Add voice input to a native app'],
    ['agent-hub', 'Implement PocketShell login'],
    ['pocketshell', 'Add model routing'],
    ['pocketshell', 'Implement cards and checklists'],
    ['pocketshell', 'Add quota capture and reset timers'],
    ['pocketshell', 'Restore pocketshell-mcp'],
  ])('flags charter category wording in %s: %s', (project, text) => {
    expect(classifyScope(project, 'Feature request', text, 'Verify feature').flag).toBe('needs_decision');
  });

  it('term matching respects words and unknown project names', () => {
    expect(classifyScope('agent-hub', 'Empty AC', 'Handle empty acceptance criteria', null).flag).toBe('in_scope');
    expect(classifyScope('constructor', 'Ordinary work', 'Anything', null).flag).toBe('in_scope');
    expect(classifyScope('agent-hub', 'Feature', 'Add PTY streams', null).flag).toBe('needs_decision');
  });

  it('gray decisions require an ADR reference', async () => {
    const { task } = await create('agent-hub', 'Terminal feature', 'Add xterm');
    const missing = await call('/tasks/' + task.id + '/events', { key: 'scope-gray-missing-' + ++seq, expected_version: 1,
      event: { kind: 'scope_decision', verdict: 'gray_adr', text: 'Allowed' } });
    expect(missing.status).toBe(400);
    expect(await missing.json()).toMatchObject({ error: 'scope_adr_required' });
    const approved = await appendEvent(task.id, 1, { kind: 'scope_decision', verdict: 'gray_adr', text: 'Operator allows ADR', source: { type: 'unknown', reference: 'SCOPE.md ADR fixture' } });
    expect(approved.task.scope.clear).toBe(true);
  });

  it('MCP exposes the flag and uses the same assignment, linked-start and operator decision gate', async () => {
    const token = 'hubt_scope_operator';
    const hash = await sha256(token);
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        'scope-human', 'human', hash, 'scope-human-family', 'admin@fake.local', 'human:fixture', '["admin"]', 'fixture', new Date().toISOString(), new Date(Date.now()+86400000).toISOString());
    });
    const invoke = async (name: string, args: any) => {
      const r = await SELF.fetch('https://hub.test/mcp', { method: 'POST', headers: { Authorization: 'Bearer '+token, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }) });
      const body: any = await r.json();
      return JSON.parse(body.result.content[0].text);
    };
    const created = await invoke('intake_create', { payload: { key: 'scope-mcp-create', project: 'agent-hub', title: 'Terminal', text: 'Add xterm', source, acceptance_criteria: 'Fixture AC' } });
    expect(created.data.task.scope_flag).toBe('needs_decision');
    const id = created.data.task.id;
    expect((await invoke('intake_show', { id })).data.task.scope_flag).toBe('needs_decision');
    expect(await invoke('intake_assign', { id, payload: assignPayload(1) })).toMatchObject({ status: 409, data: { error: 'scope_decision_required' } });
    expect(await invoke('sessions_start', { payload: { key: 'scope-mcp-start', workspace: 'demo', brief: 'Fixture', intake_task_id: id } })).toMatchObject({ status: 409, data: { error: 'scope_decision_required' } });
    expect(await invoke('intake_append', { id, payload: { key: 'scope-mcp-allow', expected_version: 1, event: { kind: 'scope_decision', verdict: 'in_scope', text: 'Operator approved' } } })).toMatchObject({ status: 200, data: { task: { scope: { clear: true } } } });
    expect((await invoke('intake_assign', { id, payload: assignPayload(2) })).status).toBe(200);
  });

});
