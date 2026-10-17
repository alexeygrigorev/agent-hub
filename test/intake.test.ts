import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie, tokenAdminSubject, TOKEN_ADMIN_EMAIL } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { IntakeService, INTAKE_LIMITS } from '../src/intake';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie: string;
let seq = 0;
async function call(path: string, payload?: any, bearer?: string, extra: any = {}) {
  return SELF.fetch('https://hub.test/api/intake' + path, { method: payload ? 'POST' : 'GET',
    headers: { ...(bearer ? { Authorization: 'Bearer ' + bearer } : { Cookie: cookie, 'x-csrf-token': 'csrf' }), ...extra },
    ...(payload ? { body: JSON.stringify(payload) } : {}) });
}
async function ok(path: string, payload?: any, bearer?: string): Promise<any> {
  const res = await call(path, payload, bearer); const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200); return data;
}
const create = (project = 'demo', key = 'create-' + ++seq) => ok('/tasks', { key, project, title: 'Synthetic task', text: '  ORIGINAL synthetic request\n世界\n', source });
async function seedAgent(actor = 'demo:assistant', family = 'family-' + ++seq): Promise<any> {
  const id = 'credential-' + ++seq, token = 'hubt_fixture_' + seq, refresh = 'hubr_fixture_' + seq;
  const hash = await sha256(token), rh = await sha256(refresh);
  await inDO(stub(), (h: any) => {
    const [project, tag] = actor.split(':');
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
    h.sql.exec('INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES (?,?,?,?)', tag, project, 'fixture-host', '2026-01-01T00:00:00.000Z');
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      id, 'agent', hash, rh, family, actor, 'fixture-host', actor, '["bus:read","bus:send"]', 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
  });
  return { id, token, refresh, family, actor };
}
async function grant(agent: any, scope = 'project', target: string | null = 'demo', access = 'write') {
  return ok('/grants', { key: 'grant-' + ++seq, credential_id: agent.id, scope, target, access, expires_at: Date.now() + 3600000, source });
}
async function append(id: string, version: number, event: any, bearer?: string, key = 'event-' + ++seq) {
  return ok('/tasks/' + id + '/events', { key, expected_version: version, event }, bearer);
}
async function state() { return inDO(stub(), (h: any) => Object.fromEntries(['tasks','rules','grants','receipts','meta'].map(t => [t, h.sql.exec('SELECT * FROM intake_' + t + ' ORDER BY id').toArray()]))); }

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    for (const t of ['tasks','grants','rules','receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
  });
});

describe('private durable intake HTTP/SQLite', () => {

  it('captures a pending voice pointer now, then appends provided transcript immutably', async () => {
    const first = await ok('/tasks', { key: 'voice', project: 'demo', title: 'Voice intake', text: null,
      source: { type: 'voice', reference: 'fixture:voice', timestamp: null }, transcription: 'pending' });
    expect(first.task.history[0]).toMatchObject({ text: null, transcription: 'pending' });
    const second = await append(first.task.id, 1, { kind: 'intake', text: 'Provided authoritative transcript',
      source: { type: 'voice', reference: 'fixture:voice', timestamp: null }, transcription: 'provided' });
    expect(second.task.history[0]).toEqual(first.task.history[0]);
    expect(second.task.history[1].text).toBe('Provided authoritative transcript');
    expect((await call('/tasks', { key: 'badvoice', project: 'demo', title: 'x', text: null, source })).status).toBe(400);
  });
  it('continues ready work only through CAS, refuses active/uncertain/approval-blocked and safely replays', async () => {
    let { task } = await create();
    const p = { key: 'continue', expected_version: 1 };
    const active = await ok('/tasks/' + task.id + '/continue', p);
    expect(active.task.continuation_state).toBe('active');
    expect(active.task.history.at(-1).evidence).toBe('recorded_active_not_started');
    expect(await ok('/tasks/' + task.id + '/continue', p)).toEqual(active);
    expect((await call('/tasks/' + task.id + '/continue', { key: 'duplicate-active', expected_version: 2 })).status).toBe(409);
    for (const kind of ['pending_approval', 'blocker']) {
      task = (await create()).task;
      task = (await append(task.id, 1, { kind, text: 'Unresolved', source })).task;
      expect((await call('/tasks/' + task.id + '/continue', { key: 'blocked-' + kind, expected_version: 2 })).status).toBe(409);
    }
    task = (await create()).task;
    await ok('/tasks/' + task.id, { key: 'uncertain', expected_version: 1, uncertainty: 'Unknown receipt' });
    expect((await call('/tasks/' + task.id + '/continue', { key: 'uncertain-continue', expected_version: 2 })).status).toBe(409);
  });
  it('expired grants and foreign canonical job references fail without revealing existence', async () => {
    const a = await seedAgent(); const g = await grant(a); const { task } = await create();
    const event = { kind: 'link', routing_job_id: 'synthetic-foreign-job', provenance: source };
    const denied = await call('/tasks/' + task.id + '/events', { key: 'foreignjob', expected_version: 1, event }, a.token);
    expect(denied.status).toBe(403); expect(await denied.json()).toEqual({ error: 'canonical_link_requires_operator' });
    await inDO(stub(), (h: any) => { const raw = h.sql.exec('SELECT data FROM intake_grants WHERE id=?', g.grant.id).one(); const grant = JSON.parse(raw.data); grant.expires_at=0; h.sql.exec('UPDATE intake_grants SET data=? WHERE id=?', JSON.stringify(grant), grant.id); });
    expect((await call('/handoff', undefined, a.token)).status).toBe(403);
  });

  it('captures verbatim provenance, filters and appends originals without rewriting', async () => {
    const first = await create(); const id = first.task.id;
    expect(first.task.history[0]).toMatchObject({ text: '  ORIGINAL synthetic request\n世界\n', source, authority: 'untrusted_task_data' });
    expect(first.task.history[0].captured_at).toBeGreaterThan(0);
    await create('other');
    expect((await ok('/tasks?project=demo&status=open')).total).toBe(1);
    const secondSource = { type: 'voice', reference: 'fixture:voice-note', timestamp: '2026-01-01T01:02:03Z' };
    await append(id, 1, { kind: 'intake', text: 'authoritative provided transcript', source: secondSource });
    const shown = await ok('/tasks/' + id + '/history');
    expect(shown.task.history).toHaveLength(2);
    expect(shown.task.history[0]).toEqual(first.task.history[0]);
    expect(shown.task.history[1].source).toEqual(secondSource);
    expect((await call('/tasks/' + id, { key: 'bad', expected_version: 2, history: [] })).status).toBe(400);
  });
  it('uses canonical idempotency and safe lost-response replay before CAS, conflict does not mutate', async () => {
    const payload = { key: 'stable', title: 'Synthetic', project: 'demo', text: 'Original', source };
    const a = await ok('/tasks', payload);
    expect(await ok('/tasks', { source, text: 'Original', project: 'demo', title: 'Synthetic', key: 'stable' })).toEqual(a);
    expect((await call('/tasks', { ...payload, text: 'Different' })).status).toBe(409);
    const change = { key: 'update', expected_version: 1, next_step: 'Review source', current_state: 'Prepared', owner: 'assistant', uncertainty: 'Runtime unknown' };
    const b = await ok('/tasks/' + a.task.id, change);
    await append(a.task.id, 2, { kind: 'note', text: 'Later', source });
    expect(await ok('/tasks/' + a.task.id, change)).toEqual(b);
    expect((await ok('/tasks')).total).toBe(1);
  });
  it('CAS rejects concurrent lost updates and keeps contiguous task history', async () => {
    const { task } = await create();
    const results = await Promise.all(['a','b'].map(key => call('/tasks/' + task.id, { key, expected_version: 1, next_step: key })));
    expect(results.map(r => r.status).sort()).toEqual([200,409]);
    const current = (await ok('/tasks/' + task.id)).task;
    expect(current.version).toBe(2); expect(current.history).toHaveLength(2);
  });
  it('records session/report/commit evidence honestly and preserves closed unresolved obligations', async () => {
    let { task } = await create();
    for (const event of [
      { kind: 'link', native_id: 'synthetic-session', engine: 'codex', tier: 'daily', host: 'fixture-host', hub: 'fixture-hub', provenance: source },
      { kind: 'report', reference: 'fixture:private-report', source },
      { kind: 'commit', reference: 'fixture:commit', source },
      { kind: 'followup', text: 'Ask owner', source },
      { kind: 'blocker', text: 'Needs review', source },
      { kind: 'pending_approval', text: 'Deploy request', source },
      { kind: 'decision', text: 'Use existing router', source },
    ]) task = (await append(task.id, task.version, event)).task;
    expect(task.history[1].evidence).toBe('external_unverified_not_launch_proof');
    await ok('/tasks/' + task.id, { key: 'close', expected_version: task.version, status: 'done' });
    const briefing = await ok('/handoff');
    expect(briefing.total).toBe(1); expect(briefing.items[0].unresolved).toHaveLength(3);
    expect((await ok('/tasks/' + task.id + '/links')).task.history).toHaveLength(9);
  });
  it('requires current session CSRF and supports current token-admin cookie and human admin bearer', async () => {
    expect((await SELF.fetch('https://hub.test/api/intake/tasks')).status).toBe(401);
    expect((await call('/tasks', { key: 'x' }, undefined, { 'x-csrf-token': '' })).status).toBe(403);
    expect((await call('/tasks', { key: 'x' }, undefined, { Origin: 'https://foreign.test' })).status).toBe(403);
    const tokenCookie = (await createSessionCookie(env as any, { sub: tokenAdminSubject('a'.repeat(64)), email: TOKEN_ADMIN_EMAIL, isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
    expect((await call('/handoff', undefined, undefined, { Cookie: tokenCookie })).status).toBe(200);
    const hash = await sha256('hubt_fixture_human');
    await inDO(stub(), (h: any) => h.sql.exec("INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES('human','human',?,'human-family','fixture-human','human:fixture','[\"admin\"]','fixture','2026-01-01',?)", hash, new Date(Date.now()+3600000).toISOString()));
    expect((await call('/handoff', undefined, 'hubt_fixture_human')).status).toBe(200);
  });
  it('ordinary bus access is private by default; explicit project/write, task/read and all grants work', async () => {
    const a = await seedAgent(); const b = await seedAgent('other:assistant');
    const task = (await create()).task; const foreign = (await create('other')).task;
    expect((await call('/handoff', undefined, a.token)).status).toBe(403);
    await grant(a); await grant(b, 'task', foreign.id, 'read');
    expect((await ok('/handoff', undefined, a.token)).items.map((t: any) => t.id)).toEqual([task.id]);
    expect((await call('/tasks/' + foreign.id, undefined, a.token)).status).toBe(404);
    expect((await call('/tasks/' + task.id, { key: 'foreign', expected_version: 1, status: 'done' }, b.token)).status).toBe(404);
    expect((await call('/tasks/' + foreign.id, { key: 'readonly', expected_version: 1, status: 'done' }, b.token)).status).toBe(404);
    expect((await call('/grants', { key: 'self', credential_id: a.id }, a.token)).status).toBe(403);
    await append(task.id, 1, { kind: 'note', text: 'Assistant follow-through', source }, a.token);
    await grant(a, 'all', null, 'read'); expect((await ok('/handoff', undefined, a.token)).total).toBe(2);
  });
  it('fresh grant revocation hides historical receipts and each subsequent snapshot page', async () => {
    const a = await seedAgent(); const g = await grant(a);
    const payload = { key: 'agent-created', project: 'demo', title: 'Synthetic', text: 'Data', source };
    const task = (await ok('/tasks', payload, a.token)).task;
    await ok('/grants/' + g.grant.id + '/revoke', { key: 'revoke', source });
    expect((await call('/tasks', payload, a.token)).status).toBe(403);
    expect((await call('/tasks/' + task.id, undefined, a.token)).status).toBe(403);
    expect((await call('/handoff?revision=1', undefined, a.token)).status).toBe(403);
  });
  it('legitimate refresh preserves family grant/retry while replay revokes family, new family stays denied', async () => {
    const a = await seedAgent(); await grant(a);
    const payload = { key: 'family-retry', project: 'demo', title: 'Synthetic', text: 'Data', source };
    const original = await ok('/tasks', payload, a.token);
    const refresh = await SELF.fetch('https://hub.test/api/token/refresh', { method: 'POST', body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: a.refresh }) });
    expect(refresh.status).toBe(200); const fresh: any = await refresh.json();
    expect(await ok('/tasks', payload, fresh.access_token)).toEqual(original);
    const other = await seedAgent('demo:assistant');
    expect((await call('/handoff', undefined, other.token)).status).toBe(403);
    expect((await call('/handoff', undefined, a.token)).status).toBe(401);
    await SELF.fetch('https://hub.test/api/token/refresh', { method: 'POST', body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: a.refresh }) });
    expect((await call('/handoff', undefined, fresh.access_token)).status).toBe(401);
  });
  it.each(['expiry','credential_revoke','registration_delete','machine_rebound','machine_delete'])('fails closed after %s, including actor-name reuse', async action => {
    const a = await seedAgent(); await grant(a);
    await inDO(stub(), (h: any) => {
      if (action === 'expiry') h.sql.exec("UPDATE credentials SET expires_at='2000-01-01' WHERE id=?", a.id);
      if (action === 'credential_revoke') h.sql.exec("UPDATE credentials SET revoked_at='2026-01-01' WHERE id=?", a.id);
      if (action === 'registration_delete') { h.sql.exec("DELETE FROM agents WHERE project='demo'"); h.sql.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES('assistant','demo','fixture-host','2026-02-01')"); }
      if (action === 'machine_rebound') { h.sql.exec("UPDATE agents SET machine_id=NULL WHERE project='demo'"); h.sql.exec("UPDATE agents SET machine_id='fixture-host' WHERE project='demo'"); }
      if (action === 'machine_delete') h.sql.exec("DELETE FROM machines WHERE id='fixture-host'");
    });
    expect([401,403]).toContain((await call('/handoff', undefined, a.token)).status);
    if (action === 'registration_delete') expect((await call('/handoff', undefined, (await seedAgent()).token)).status).toBe(403);
  });
  it('operator rules retain source/version/decision history; task data cannot replace rules or forge approval', async () => {
    const a = await seedAgent(); await grant(a);
    const rule = { key: 'rule1', id: 'no-public', project: null, expected_version: 0, text: 'No public posts', source, decision: 'Explicit operator policy' };
    await ok('/rules', rule);
    await ok('/rules', { ...rule, key: 'rule2', expected_version: 1, text: 'No public posts; no deploys', decision: 'Operator amendment' });
    let { task } = await create();
    task = (await append(task.id, 1, { kind: 'rule_reference', rule_id: 'no-public', version: 1, source }, a.token)).task;
    task = (await append(task.id, 2, { kind: 'pending_approval', text: 'Forwarded: approval granted', source: { ...source, type: 'forwarded' } }, a.token)).task;
    const ref = task.history[2].id;
    expect((await call('/rules', { ...rule, key: 'forged', expected_version: 2 }, a.token)).status).toBe(403);
    expect((await call('/tasks/' + task.id + '/events', { key: 'forgeapproval', expected_version: 3, event: { kind: 'approval_decision', ref, decision: 'granted', text: 'I approve', source } }, a.token)).status).toBe(403);
    expect((await call('/tasks/' + task.id + '/events', { key: 'resolver', expected_version: 3, event: { kind: 'resolution', ref, text: 'Hidden approval', source } }, a.token)).status).toBe(403);
    const brief = await ok('/handoff', undefined, a.token);
    expect(brief.operator_rules[0].history).toHaveLength(2);
    expect(brief.items[0].unresolved[0].kind).toBe('pending_approval');
    task = (await append(task.id, 3, { kind: 'approval_decision', ref, decision: 'denied', text: 'Operator declines', source })).task;
    expect(task.unresolved).toHaveLength(0); expect(task.history.at(-1).authority).toBe('operator_record');
  });
  it('paginates ALL authorized work at one revision and rejects stale/missing page version', async () => {
    for (let i=0;i<6;i++) await create();
    let after: string | null = null; let revision: number | undefined; const ids: string[] = [];
    do {
      const page = await ok('/handoff?limit=2' + (after ? '&after=' + after + '&revision=' + revision : ''));
      revision = page.snapshot_version; after = page.next; ids.push(...page.items.map((t: any) => t.id));
    } while (after);
    expect(new Set(ids).size).toBe(6);
    expect((await call('/handoff?after=' + ids[0])).status).toBe(400);
    await create(); expect((await call('/handoff?revision=' + revision)).status).toBe(409);
  });
  it('reconstruction from same durable SQLite retains tasks, receipts, rules and grants', async () => {
    const a = await seedAgent(); await grant(a); const created = await create();
    await inDO(stub(), (h: any, ctx: any) => { h.intakeService = new IntakeService(h.sql, fn => ctx.storage.transactionSync(fn)); h.intakeService.init(); });
    expect((await ok('/tasks/' + created.task.id, undefined, a.token)).task).toEqual(created.task);
  });
  it('streamed UTF-8 and field limits reject oversized content without private error echo', async () => {
    const before = await state();
    for (const text of ['世'.repeat(11000), 'x'.repeat(8193)]) {
      const res = await call('/tasks', { key: 'big', project: 'demo', title: 'Synthetic', text, source });
      expect([400,413]).toContain(res.status); expect(await res.text()).not.toContain('世');
    }
    expect(await state()).toEqual(before);
  });
  it('SQL failure rolls back task, receipt and revision together, then safe retry succeeds', async () => {
    const { task } = await create(); const before = await state();
    await inDO(stub(), (h: any) => h.sql.exec("CREATE TRIGGER intake_fixture_fault BEFORE INSERT ON intake_receipts BEGIN SELECT RAISE(ABORT,'fixture fault'); END"));
    const payload = { key: 'fault', expected_version: 1, next_step: 'Synthetic update' };
    expect((await call('/tasks/' + task.id, payload)).status).toBe(500);
    expect(await state()).toEqual(before);
    await inDO(stub(), (h: any) => h.sql.exec('DROP TRIGGER intake_fixture_fault'));
    expect((await ok('/tasks/' + task.id, payload)).task.version).toBe(2);
  });
  it('refuses global/task/count/history caps explicitly and preserves prior work', async () => {
    const { task } = await create();
    await inDO(stub(), (h: any) => {
      const raw = h.sql.exec('SELECT data FROM intake_tasks WHERE id=?', task.id).one();
      const data = JSON.parse(raw.data); data.current_state = 'x'.repeat(INTAKE_LIMITS.taskBytes);
      h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify(data), task.id);
    });
    let before = await state();
    expect((await call('/tasks/' + task.id, { key: 'taskcap', expected_version: 1, next_step: 'x' })).status).toBe(507);
    expect(await state()).toEqual(before);
    await inDO(stub(), (h: any) => {
      h.sql.exec('DELETE FROM intake_tasks');
      for (let i=0;i<INTAKE_LIMITS.tasks;i++) h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-' + i, JSON.stringify({ id: 'fixture-' + i, project: 'demo', status: 'open', history: [] }));
    });
    before = await state(); expect((await call('/tasks', { key: 'countcap', project: 'demo', title: 'x', text: 'x', source })).status).toBe(507); expect(await state()).toEqual(before);
    await inDO(stub(), (h: any) => {
      h.sql.exec('DELETE FROM intake_tasks');
      for (let i=0;i<8;i++) h.sql.exec('INSERT INTO intake_receipts VALUES(?,?)', 'fixture-padding-' + i, JSON.stringify({ id: 'fixture-padding-' + i, padding: 'x'.repeat(INTAKE_LIMITS.totalBytes/8) }));
    });
    before = await state(); expect((await call('/tasks', { key: 'globalcap', project: 'demo', title: 'x', text: 'x', source })).status).toBe(507); expect(await state()).toEqual(before);
  });
});
