import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie, tokenAdminSubject, TOKEN_ADMIN_EMAIL } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { IntakeService, INTAKE_LIMITS, INTAKE_EVENT_KINDS } from '../src/intake';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const operatorHash = async (value: string) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, '0')).join('');
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
const create = (project = 'demo', key = 'create-' + ++seq) => ok('/tasks', { key, project, title: 'Synthetic task', text: '  ORIGINAL synthetic request\n世界\n', source,
  acceptance_criteria: 'Synthetic acceptance criteria: review posted with findings addressed.' });
async function seedAgent(actor = 'demo:assistant', family = 'family-' + ++seq, scopes = '["bus:read","bus:send"]'): Promise<any> {
  const id = 'credential-' + ++seq, token = 'hubt_fixture_' + seq, refresh = 'hubr_fixture_' + seq;
  const hash = await sha256(token), rh = await sha256(refresh);
  await inDO(stub(), (h: any) => {
    const [project, tag] = actor.split(':');
    h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
    h.sql.exec('INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES (?,?,?,?)', tag, project, 'fixture-host', '2026-01-01T00:00:00.000Z');
    h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
      id, 'agent', hash, rh, family, actor, 'fixture-host', actor, scopes, 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
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
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks','grants','rules','receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials'); h.sql.exec('DELETE FROM agents');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

describe('private durable intake HTTP/SQLite', () => {
  it.each([1, 25, 26, 100, 101, 1000, "999999999999999999999999999999999999"])('accepts limit %s and reports the effective clamped limit', async limit => {
    const result = await ok('/tasks?limit=' + limit);
    expect(result.effective_limit).toBe(Math.min(Number(limit), 100));
  });

  it('reports precise errors for invalid and nonpositive limits', async () => {
    for (const limit of ['', 'abc', '1.5', 'NaN', '0x10', '1e2']) {
      const response = await call('/tasks?limit=' + encodeURIComponent(limit));
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ error: 'invalid_limit', field: 'limit', min: 1, max: 100 });
    }
    for (const limit of ['0', '-1']) {
      const response = await call('/tasks?limit=' + limit);
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ error: 'limit_out_of_range', field: 'limit', min: 1, max: 100 });
    }
  });

  it('returns 100 items per page with a revision-pinned remainder', async () => {
    await inDO(stub(), (h: any) => {
      for (let i = 0; i < 101; i++) {
        h.sql.exec('INSERT INTO intake_tasks (id, data) VALUES (?, ?)', 'limit-' + String(i).padStart(3, '0'), JSON.stringify({
          id: 'limit-' + String(i).padStart(3, '0'), project: 'limit-fixture', status: 'open', version: 1,
          title: 'Fixture', history: [], created_at: 1, updated_at: 1,
        }));
      }
    });
    const first = await ok('/tasks?project=limit-fixture&limit=100');
    expect(first.items).toHaveLength(100);
    expect(first.total).toBe(101);
    const last = await ok('/tasks?project=limit-fixture&limit=100&after=' + first.next + '&revision=' + first.snapshot_version);
    expect(last.items).toHaveLength(1);
    expect(last.next).toBeNull();
  });


  it('returns field byte limits and every supported event kind without echoing input', async () => {
    for (const [field, max] of [['text', 8192], ['title', 256], ['acceptance_criteria', 8192]] as const) {
      const res = await call('/tasks', { key: 'invalid-' + field, project: 'demo', title: 'Title', text: 'Body', source,
        [field]: '界'.repeat(Math.floor(max / 3) + 1) });
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({ error: 'invalid_field', field, max_bytes: max });
    }
    const { task } = await create();
    const res = await call('/tasks/' + task.id + '/events', { key: 'invalid-kind', expected_version: 1, event: { kind: 'update' } });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: 'invalid_event_kind', valid_kinds: INTAKE_EVENT_KINDS });
    expect(INTAKE_EVENT_KINDS).toContain('session');
    expect(INTAKE_EVENT_KINDS).toContain('obligation_response');
    expect((await ok('/tasks/' + task.id)).task.control.version).toBe(1);

    // Short prefix lookup on /tasks/:id
    const prefixLookup = await ok('/tasks/' + task.id.slice(0, 8));
    expect(prefixLookup.task.id).toBe(task.id);

    // Append accepts { note: { text, source } } and string source
    const noteRes = await ok('/tasks/' + task.id + '/events', {
      key: 'append-note-shape',
      expected_version: 1,
      note: { text: 'Appended note content', source: 'triage-note' },
    });
    expect(noteRes.task.control.version).toBe(2);
    expect(noteRes.task.payload_data.history[1]).toMatchObject({
      kind: 'note',
      text: 'Appended note content',
      source: { type: 'unknown', reference: 'triage-note', timestamp: null },
    });

    // Append accepts { event: { note: { text, source } } } wrapped shape
    const wrappedNoteRes = await ok('/tasks/' + task.id + '/events', {
      key: 'append-wrapped-note',
      expected_version: 2,
      event: { note: { text: 'Wrapped note content', source: 'chat' } },
    });
    expect(wrappedNoteRes.task.control.version).toBe(3);
    expect(wrappedNoteRes.task.payload_data.history[2]).toMatchObject({
      kind: 'note',
      text: 'Wrapped note content',
      source: { type: 'chat', reference: null, timestamp: null },
    });
  });

  it('records and replays new intake beyond the former 4 MiB backfill budget', async () => {
    await inDO(stub(), (h: any) => {
      for (let i = 0; i < 20; i++) h.sql.exec('INSERT INTO intake_receipts VALUES(?,?)', 'retained-' + i, JSON.stringify({ id: 'retained-' + i, padding: 'x'.repeat(256 * 1024), captured_at: Date.now() }));
    });
    const input = { key: 'after-backfill', project: 'demo', title: 'Keep accepting intake', text: 'Original request after retained history exceeds 4 MiB', source };
    const first = await ok('/tasks', input);
    expect((await ok('/tasks', input)).task.id).toBe(first.task.id);
    expect(await inDO(stub(), (h: any) => h.sql.exec("SELECT COUNT(*) AS n FROM intake_receipts WHERE id LIKE 'retained-%'").one().n)).toBe(20);
  });

  it('captures a pending voice pointer now, then appends provided transcript immutably', async () => {
    const first = await ok('/tasks', { key: 'voice', project: 'demo', title: 'Voice intake', text: null,
      source: { type: 'voice', reference: 'fixture:voice', timestamp: null }, transcription: 'pending' });
    expect(first.task.payload_data.history[0]).toMatchObject({ text: null, transcription: 'pending' });
    expect(first.task.control.continuation_state).toBe('uncertain');
    expect((await call('/tasks/' + first.task.id + '/continue', { key: 'pending-voice', expected_version: 1 })).status).toBe(409);
    const second = await append(first.task.id, 1, { kind: 'intake', text: 'Provided authoritative transcript',
      source: { type: 'voice', reference: 'fixture:voice', timestamp: null }, transcription: 'provided' });
    expect(second.task.payload_data.history[0]).toEqual(first.task.payload_data.history[0]);
    expect(second.task.payload_data.history[1].text).toBe('Provided authoritative transcript');
    expect((await call('/tasks', { key: 'badvoice', project: 'demo', title: 'x', text: null, source })).status).toBe(400);
  });
  it('continues ready work only through CAS, refuses active/uncertain/approval-blocked and safely replays', async () => {
    let { task } = await create();
    const p = { key: 'continue', expected_version: 1 };
    const active = await ok('/tasks/' + task.id + '/continue', p);
    expect(active.task.control.continuation_state).toBe('active');
    expect(active.task.payload_data.history.at(-1).evidence).toBe('recorded_active_not_started');
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


  it('keeps immutable per-event retry keys and refuses history exhaustion without truncation', async () => {
    const created = await create('demo', 'immutable-intake-key');
    expect(created.task.payload_data.history[0].intake_key).toBe('immutable-intake-key');
    await inDO(stub(), (h: any) => {
      const data = JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?', created.task.id).one().data);
      while (data.history.length < INTAKE_LIMITS.events) data.history.push({ id: 'fixture-event-' + data.history.length, kind: 'note', text: 'Synthetic bounded history', source });
      h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify(data), created.task.id);
    });
    const before = await state();
    expect((await call('/tasks/' + created.task.id, { key: 'history-cap', expected_version: 1, next_step: 'Do not truncate' })).status).toBe(507);
    expect(await state()).toEqual(before);
    expect((await ok('/tasks/' + created.task.id)).task.payload_data.history).toHaveLength(INTAKE_LIMITS.events);
  });
  it('fences pagination when one of several grants expires without a data revision change', async () => {
    const a = await seedAgent(); const g = await grant(a); await ok('/grants', { key: 'longer-all', credential_id: a.id, scope: 'all', target: null, access: 'read', expires_at: Date.now()+7200000, source });
    await create(); await create();
    const first = await ok('/handoff?limit=1', undefined, a.token);
    expect((await call('/handoff?after=' + first.next + '&revision=' + first.snapshot_version, undefined, a.token)).status).toBe(400);
    await inDO(stub(), (h: any) => { const grant = JSON.parse(h.sql.exec('SELECT data FROM intake_grants WHERE id=?', g.grant.id).one().data); grant.expires_at=0; h.sql.exec('UPDATE intake_grants SET data=? WHERE id=?', JSON.stringify(grant), g.grant.id); });
    expect((await call('/handoff?after=' + first.next + '&revision=' + first.snapshot_version + '&authorization_valid_until=' + first.authorization_valid_until, undefined, a.token)).status).toBe(409);
  });
  it.each(['grants','rules','receipts'])('refuses bounded %s count without deleting old records', async table => {
    const a = await seedAgent();
    const count = table === 'grants' ? INTAKE_LIMITS.grants : table === 'rules' ? INTAKE_LIMITS.rules : INTAKE_LIMITS.receipts;
    await inDO(stub(), (h: any) => { for (let i=0;i<count;i++) h.sql.exec('INSERT INTO intake_' + table + ' VALUES(?,?)', 'fixture-' + i, JSON.stringify({ id: 'fixture-' + i, captured_at: Date.now() })); });
    const before = await state();
    const path = table === 'receipts' ? '/tasks' : '/' + table;
    const payload = table === 'grants' ? { key: 'cap', credential_id: a.id, scope: 'all', target: null, access: 'read', expires_at: Date.now()+60000, source } :
      table === 'rules' ? { key: 'cap', id: 'rule-cap', expected_version: 0, project: null, text: 'Synthetic', decision: 'Operator rule', source } :
      { key: 'cap', project: 'demo', title: 'Synthetic', text: 'Synthetic', source };
    expect((await call(path, payload)).status).toBe(507);
    expect(await state()).toEqual(before);
  });
  it('accepts bounded streamed UTF-8 chunks and rejects malformed byte sequences atomically', async () => {
    const bytes = new TextEncoder().encode(JSON.stringify({ key: 'stream', project: 'demo', title: 'Synthetic', text: '世界', source }));
    const stream = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close(); } });
    const response = await SELF.fetch('https://hub.test/api/intake/tasks', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }, body: stream });
    expect(response.status).toBe(200);
    const before = await state();
    const invalid = await SELF.fetch('https://hub.test/api/intake/tasks', { method: 'POST', headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }, body: new Uint8Array([0xff,0xfe]) });
    expect(invalid.status).toBe(400); expect(await state()).toEqual(before);
  });


  it('rechecks live SQL credentials before mutation even when the authentication callback is stale', async () => {
    const a = await seedAgent(); await grant(a); const hash = await sha256(a.token);
    const before = await state();
    const status = await inDO(stub(), async (h: any) => {
      let checks = 0;
      const auth = { caller: { kind: 'agent', tokenId: a.id, tokenHash: hash, actor: a.actor }, scopes: ['bus:read', 'bus:send'] };
      const request = new Request('https://hub.test/api/intake/tasks', { method: 'POST', body: JSON.stringify({ key: 'stale-caller', project: 'demo', title: 'Synthetic', text: 'Synthetic', source }) });
      const response = await h.intakeService.handle(request, async () => {
        if (++checks === 2) h.sql.exec("UPDATE credentials SET revoked_at='2026-01-01' WHERE id=?", a.id);
        return auth;
      });
      return response.status;
    });
    expect(status).toBe(403);
    expect(await state()).toEqual(before);
  });
  it('rule CAS conflicts roll back and rule scope cannot be changed', async () => {
    const rule = { key: 'rule-first', id: 'synthetic-rule', project: 'demo', expected_version: 0, text: 'Operator rule', source, decision: 'Explicit policy' };
    await ok('/rules', rule); const before = await state();
    expect((await call('/rules', { ...rule, key: 'stale-rule', expected_version: 0 })).status).toBe(409);
    expect((await call('/rules', { ...rule, key: 'scope-change', expected_version: 1, project: 'other' })).status).toBe(400);
    expect(await state()).toEqual(before);
  });


  it('rename-away-and-back cannot revive a former grant for an identity string', async () => {
    const a = await seedAgent(); await grant(a); await create();
    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE agents SET tag='renamed' WHERE project='demo' AND tag='assistant'");
      h.sql.exec("UPDATE agents SET tag='assistant' WHERE project='demo' AND tag='renamed'");
    });
    expect((await call('/handoff', undefined, a.token)).status).toBe(403);
  });
  it('human admin bearer refresh preserves lost-response idempotency identity', async () => {
    const hash = await sha256('hubt_fixture_admin_refresh'), rh = await sha256('hubr_fixture_admin_refresh');
    await inDO(stub(), (h: any) => h.sql.exec("INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES('human-refresh','human',?,?,'fixture-admin-family','admin@fake.local','human:fixture','[\"admin\"]','fixture','2026-01-01',?)", hash, rh, new Date(Date.now()+3600000).toISOString()));
    const payload = { key: 'admin-retry', project: 'demo', title: 'Synthetic', text: 'Original', source };
    const first = await ok('/tasks', payload, 'hubt_fixture_admin_refresh');
    const refresh = await SELF.fetch('https://hub.test/api/token/refresh', { method: 'POST', body: JSON.stringify({ grant_type: 'refresh_token', refresh_token: 'hubr_fixture_admin_refresh' }) });
    expect(refresh.status).toBe(200); const fresh: any = await refresh.json();
    expect(await ok('/tasks', payload, fresh.access_token)).toEqual(first);
    expect((await ok('/tasks')).total).toBe(1);
  });


  it('uses the actual configured operator token as CLI bearer, rotates closed, and cannot bypass cookie CSRF', async () => {
    const token = 'synthetic_operator_token_0123456789_abcdefgh';
    const hash = await operatorHash(token);
    await inDO(stub(), (h: any) => { h.env.HUB_ADMIN_TOKEN_SHA256 = hash; });
    const created = await ok('/tasks', { key: 'operator-token-create', project: 'demo', title: 'Synthetic', text: 'Operator bearer', source }, token);
    expect((await ok('/handoff', undefined, token)).total).toBe(1);
    const agent = await seedAgent();
    await ok('/grants', { key: 'operator-token-grant', credential_id: agent.id, scope: 'all', target: null, access: 'read', expires_at: Date.now()+60000, source }, token);
    await ok('/rules', { key: 'operator-token-rule', id: 'token-rule', expected_version: 0, project: null, text: 'Operator rule', decision: 'Explicit operator policy', source }, token);
    expect((await ok('/handoff', undefined, agent.token)).operator_rules).toHaveLength(1);
    expect((await call('/tasks', { key: 'crossorigin', project: 'demo', title: 'x', text: 'x', source }, token, { Origin: 'https://foreign.test' })).status).toBe(403);
    expect((await call('/tasks', { key: 'csrf-bypass', project: 'demo', title: 'x', text: 'x', source }, token, { Cookie: cookie })).status).toBe(403);
    expect((await call('/handoff', undefined, token + '-wrong')).status).toBe(401);
    const nextHash = await operatorHash('synthetic_rotated_operator_token');
    await inDO(stub(), (h: any) => { h.env.HUB_ADMIN_TOKEN_SHA256 = nextHash; });
    expect((await call('/tasks/' + created.task.id, undefined, token)).status).toBe(401);
    expect((await call('/tasks', { key: 'operator-token-create', project: 'demo', title: 'Synthetic', text: 'Operator bearer', source }, token)).status).toBe(401);
  });
  it('a fresh granted assistant uses only handoff to advance ready work while retaining approval and active custody', async () => {
    const ready = (await create()).task, blocked = (await create()).task, active = (await create()).task;
    await append(blocked.id, 1, { kind: 'pending_approval', text: 'Operator decision pending', source });
    await ok('/tasks/' + active.id + '/continue', { key: 'existing-active', expected_version: 1 });
    const assistant = await seedAgent(); await grant(assistant, 'all', null, 'write');
    const briefing = await ok('/handoff', undefined, assistant.token);
    const next = briefing.items.find((t: any) => t.control.continuation_state === 'ready');
    expect(next.id).toBe(ready.id);
    await ok('/tasks/' + next.id + '/continue', { key: 'fresh-continue', expected_version: next.control.version }, assistant.token);
    const pending = briefing.items.find((t: any) => t.control.continuation_state === 'approval_blocked');
    expect((await call('/tasks/' + pending.id + '/continue', { key: 'approval-bypass', expected_version: pending.control.version }, assistant.token)).status).toBe(409);
    const running = briefing.items.find((t: any) => t.id === active.id);
    expect((await call('/tasks/' + running.id + '/continue', { key: 'duplicate-active-fresh', expected_version: running.control.version }, assistant.token)).status).toBe(409);
    const fresh = await ok('/handoff', undefined, assistant.token);
    expect(fresh.items.find((t: any) => t.id === blocked.id).unresolved).toHaveLength(1);
    expect(fresh.total).toBe(3);
  });


  it('operator bearer fails closed for absent/malformed configuration and failed registered credentials', async () => {
    const token = 'synthetic_operator_no_configuration';
    for (const value of [undefined, 'malformed-configuration']) {
      await inDO(stub(), (h: any) => { h.env.HUB_ADMIN_TOKEN_SHA256 = value; });
      expect((await call('/handoff', undefined, token)).status).toBe(401);
    }
    const a = await seedAgent(); const hash = await operatorHash(a.token);
    await inDO(stub(), (h: any) => { h.env.HUB_ADMIN_TOKEN_SHA256=hash; h.sql.exec("UPDATE credentials SET revoked_at='2026-01-01' WHERE id=?", a.id); });
    expect((await call('/handoff', undefined, a.token)).status).toBe(401);
    expect((await call('/handoff', undefined, a.token, { Cookie: cookie, 'x-csrf-token': 'csrf' })).status).toBe(403);
  });


  it('operator canonical routing references require a real job and never claim verified launch evidence', async () => {
    const { task } = await create();
    const event = { kind: 'link', routing_job_id: 'fixture-routing-job', native_id: 'claimed-external-session', provenance: source };
    expect((await call('/tasks/' + task.id + '/events', { key: 'missing-route', expected_version: 1, event })).status).toBe(400);
    await inDO(stub(), (h: any) => h.sql.exec("INSERT INTO routing_jobs(id,actor,request_key,request_json,state,created_at,updated_at) VALUES('fixture-routing-job','fixture-client','fixture-key','{}','queued','2026-01-01','2026-01-01')"));
    const linked = await append(task.id, 1, event);
    expect(linked.task.payload_data.history.at(-1)).toMatchObject({ routing_job_id: 'fixture-routing-job', evidence: 'external_unverified_not_launch_proof' });
    expect((await call('/tasks/' + task.id + '/events', { key: 'forged-verification', expected_version: 2, event: { ...event, evidence: 'hub_launch_verified' } })).status).toBe(400);
  });

  it('accepts the documented note envelope with provenance, replay and CAS guards', async () => {
    const { task } = await create();
    const path = '/tasks/' + task.id + '/events';
    const origin = { type: 'chat', reference: 't753u', timestamp: '2026-10-20T13:26:00+02:00' };
    const payload = { key: 'note-envelope', expected_version: 1, note: { text: 'И в этом случае тоже', source: origin } };
    const first = await ok(path, payload);
    expect(first.task.control.version).toBe(2);
    expect(first.task.payload_data.history.at(-1)).toMatchObject({ kind: 'note', text: payload.note.text, source: origin });
    expect(await ok(path, payload)).toEqual(first);
    expect((await call(path, { ...payload, note: { text: 'Changed' } })).status).toBe(409);
    expect((await call(path, { ...payload, key: 'stale-note' })).status).toBe(409);
    const second = await ok(path, { key: 'note-no-source', expected_version: 2, note: { text: 'No source' } });
    expect(second.task.payload_data.history.at(-1)).toMatchObject({ kind: 'note', source });
    expect(second.task.control.version).toBe(3);
    for (const [extra, field] of [
      [{ typo: {} }, 'typo'],
      [{ note: { text: 'Invalid', typo: true } }, 'typo'],
      [{ note: { text: 'Invalid', source: { type: 'chat', typo: true } } }, 'typo'],
      [{ note: { text: 'Invalid', kind: 'approval_decision' } }, 'kind'],
    ] as const) {
      const response = await call(path, { key: 'bad-note', expected_version: 3, ...extra });
      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: 'unknown_field', field });
    }
    for (const note of [null, [], {}, { text: '' }, { text: 'Invalid', source: null }]) {
      expect((await call(path, { key: 'invalid-note', expected_version: 3, note })).status).toBe(400);
    }
    const ambiguous = await call(path, { key: 'ambiguous-note', expected_version: 3,
      note: { text: 'Note' }, event: { kind: 'note', text: 'Other' } });
    expect(ambiguous.status).toBe(400);
    expect(await ambiguous.json()).toEqual({ error: 'invalid_field', field: 'note' });
    expect((await ok('/tasks/' + task.id)).task.control.version).toBe(3);
  });

  it('accepts note string and top-level text shorthands; missing note/event is event_or_note_required', async () => {
    const task = (await create()).task;
    const path = '/tasks/' + task.id + '/events';
    const asString = await ok(path, { key: 'note-string', expected_version: 1, note: 'String shorthand note' });
    expect(asString.task.payload_data.history.at(-1)).toMatchObject({ kind: 'note', text: 'String shorthand note' });
    const asText = await ok(path, { key: 'note-text', expected_version: 2, text: 'Top-level text note' });
    expect(asText.task.payload_data.history.at(-1)).toMatchObject({ kind: 'note', text: 'Top-level text note' });
    const missing = await call(path, { key: 'note-missing', expected_version: 3 });
    expect(missing.status).toBe(400);
    expect(await missing.json()).toEqual({ error: 'event_or_note_required' });
  });

  it('accepts concise notes and session aliases without relaxing link checks', async () => {
    const { task } = await create();
    const note = await append(task.id, 1, { kind: 'note', text: 'Concise note' });
    expect(note.task.payload_data.history.at(-1)).toMatchObject({ kind: 'note', text: 'Concise note', source });
    const linked = await append(task.id, 2, { kind: 'session', native_id: 'fixture-native' });
    expect(linked.task.payload_data.history.at(-1)).toMatchObject({ kind: 'link', native_id: 'fixture-native', provenance: source, evidence: 'external_unverified_not_launch_proof' });
    expect((await call('/tasks/' + task.id + '/events', { key: 'unknown-session', expected_version: 3,
      event: { kind: 'session', hub_session_id: '12345678-1234-4234-8234-123456789012' } })).status).toBe(404);
    expect((await call('/tasks/' + task.id + '/events', { key: 'invalid-source', expected_version: 3,
      event: { kind: 'note', text: 'Invalid', source: null } })).status).toBe(400);
  });

  it('assigns a real Hub session through the canonical session API and projects it live in handoff', async () => {
    const { task } = await create();
    const payload = { key: 'assign-live', expected_version: 1,
      session: { workspace: 'demo', engine: 'codex', mode: 'headless', brief: 'Synthetic assigned goal' },
      provenance: source, next_step: 'Watch Hub binding' };
    const assigned = await ok('/tasks/' + task.id + '/assign', payload);
    const sessionId = assigned.assigned_session.hub_session_id;
    expect(sessionId).toMatch(/^[0-9a-f-]{36}$/i);
    expect(assigned.assigned_session.linkage).toBe('hub_session');
    expect(assigned.assigned_session.state).toBe('held');
    expect(assigned.hold).toBeTruthy();
    expect(assigned.assigned_session.hold).toEqual(assigned.hold);
    const heldRead = await ok('/tasks/' + task.id);
    expect(heldRead.task.assigned_sessions[0].hold).toEqual(assigned.hold);
    expect(assigned.task.control.version).toBe(3);
    expect(assigned.task.payload_data.next_step).toBe('Watch Hub binding');
    const link = assigned.task.payload_data.history.find((e: any) => e.kind === 'link');
    expect(link).toMatchObject({ hub_session_id: sessionId, evidence: 'hub_launch_verified' });
    // The same operator controls the real session through hub sessions.
    const sessions = async () => ((await (await SELF.fetch('https://hub.test/api/sessions?limit=50',
      { headers: { Cookie: cookie, 'x-csrf-token': 'csrf' } })).json()) as any).sessions.map((s: any) => s.id);
    expect(await sessions()).toContain(sessionId);
    // Handoff projects the live canonical state, never caller assertions.
    const entry = (await ok('/handoff')).items.find((t: any) => t.id === task.id);
    expect(entry.assigned_sessions[0]).toMatchObject({ hub_session_id: sessionId, linkage: 'hub_session' });
    expect(typeof entry.assigned_sessions[0].state).toBe('string');
    // A lost-response retry replays the identical response without launching again.
    expect(await ok('/tasks/' + task.id + '/assign', payload)).toEqual(assigned);
    expect(await sessions()).toContain(sessionId);
    // A changed payload under the same key conflicts without mutation.
    expect((await call('/tasks/' + task.id + '/assign', { ...payload, next_step: 'Different' })).status).toBe(409);
    // A second assignment is refused while the first session is still live.
    expect((await call('/tasks/' + task.id + '/assign', { ...payload, key: 'assign-second',
      expected_version: assigned.task.control.version, session: { workspace: 'demo' } })).status).toBe(409);
    // A forged session identity fails closed without an oracle.
    expect((await call('/tasks/' + task.id + '/events', { key: 'forged-link', expected_version: 3,
      event: { kind: 'link', hub_session_id: '12345678-1234-4234-8234-123456789012', provenance: source } })).status).toBe(404);
  });
  it('agents assign only through their own canonical session; foreign sessions fail closed', async () => {
    const agent = await seedAgent('demo:assistant', 'family-' + ++seq, '["bus:read","bus:send","fleet:launch"]');
    await grant(agent, 'project', 'demo', 'write');
    const { task } = await create();
    const payload = { key: 'agent-assign', expected_version: 1,
      session: { workspace: 'demo', brief: 'Synthetic agent goal' }, provenance: source };
    const assigned = await ok('/tasks/' + task.id + '/assign', payload, agent.token);
    const sessionId = assigned.assigned_session.hub_session_id;
    expect(sessionId).toMatch(/^[0-9a-f-]{36}$/i);
    // The same agent credential controls its session through hub sessions.
    const listed = await SELF.fetch('https://hub.test/api/sessions?limit=50',
      { headers: { Authorization: 'Bearer ' + agent.token } });
    expect(listed.status).toBe(200);
    expect((((await listed.json()) as any).sessions as any[]).map(s => s.id)).toContain(sessionId);
    // An operator-owned session cannot be linked by this agent: no oracle.
    const foreign = (await ok('/tasks', { key: 'foreign-task', project: 'demo', title: 'Synthetic', text: 'x', source,
      acceptance_criteria: 'Synthetic acceptance criteria.' })).task;
    const foreignSession = (await ok('/tasks/' + foreign.id + '/assign', { key: 'foreign-assign',
      expected_version: 1, session: { workspace: 'demo', brief: 'Synthetic foreign goal' }, provenance: source })).assigned_session.hub_session_id;
    expect((await call('/tasks/' + task.id + '/events', { key: 'foreign-link', expected_version: 2,
      event: { kind: 'link', hub_session_id: foreignSession, provenance: source } }, agent.token)).status).toBe(404);
    // Without fleet:launch the canonical service refuses, so assignment fails.
    const plain = await seedAgent('demo:assistant');
    await grant(plain, 'project', 'demo', 'write');
    const unscoped = (await ok('/tasks', { key: 'unscoped-task', project: 'demo', title: 'Synthetic', text: 'x', source,
      acceptance_criteria: 'Synthetic acceptance criteria.' })).task;
    expect((await call('/tasks/' + unscoped.id + '/assign', { key: 'no-launch', expected_version: 1,
      session: { workspace: 'demo' }, provenance: source }, plain.token)).status).toBe(403);
  });
  it('carries session.parent_id to canonical session service, preserving lineage, idempotency, and failure boundaries', async () => {
    const parentTask = (await create()).task;
    const parentRes = await SELF.fetch('https://hub.test/api/sessions', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf' },
      body: JSON.stringify({ key: 'parent-session-' + ++seq, workspace: 'demo', mode: 'headless', brief: 'Parent session', intake_task_id: parentTask.id })
    });
    expect([201, 409]).toContain(parentRes.status);
    const parentId = ((await parentRes.json()) as any).id;
    expect(parentId).toMatch(/^[0-9a-f-]{36}$/i);

    const { task } = await create();
    const childPayload = {
      key: 'assign-with-parent-' + ++seq,
      expected_version: 1,
      session: {
        workspace: 'demo',
        engine: 'codex',
        mode: 'headless',
        brief: 'Child assigned goal',
        parent_id: parentId
      },
      provenance: source,
      next_step: 'Child running'
    };
    const childAssigned = await ok('/tasks/' + task.id + '/assign', childPayload);
    const childSessionId = childAssigned.assigned_session.hub_session_id;
    expect(childSessionId).toMatch(/^[0-9a-f-]{36}$/i);

    await inDO(stub(), (h: any) => {
      const row = h.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', childSessionId).toArray()[0];
      expect(row.parent_id).toBe(parentId);
      expect(row.parent_source).toBe('explicit_parent');
    });
    const childShowRes = await SELF.fetch('https://hub.test/api/sessions/' + childSessionId, {
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf' }
    });
    expect(childShowRes.status).toBe(200);
    const childShow = (await childShowRes.json()) as any;
    expect(childShow.parent_id).toBe(parentId);
    expect(childShow.parent_source).toBe('explicit_parent');

    expect(await ok('/tasks/' + task.id + '/assign', childPayload)).toEqual(childAssigned);

    const conflicting = { ...childPayload, session: { ...childPayload.session, parent_id: '11111111-2222-3333-4444-555555555555' } };
    expect((await call('/tasks/' + task.id + '/assign', conflicting)).status).toBe(409);

    const { task: taskUnknown } = await create();
    const unknownRes = await call('/tasks/' + taskUnknown.id + '/assign', {
      key: 'assign-unknown-parent-' + ++seq,
      expected_version: 1,
      session: { workspace: 'demo', parent_id: '00000000-0000-0000-0000-000000000000' },
      provenance: source
    });
    expect(unknownRes.status).toBe(404);
    expect(await unknownRes.json()).toEqual({ error: 'parent_session_not_found' });
    const taskUnknownAfter = (await ok('/tasks/' + taskUnknown.id)).task;
    expect(taskUnknownAfter.control.version).toBe(1);
    expect(taskUnknownAfter.payload_data.history.some((e: any) => e.kind === 'link')).toBe(false);

    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE session_control_sessions SET parent_id = ? WHERE id = ?', parentId, parentId);
    });
    const { task: taskCycle } = await create();
    const cycleRes = await call('/tasks/' + taskCycle.id + '/assign', {
      key: 'assign-cycle-parent-' + ++seq,
      expected_version: 1,
      session: { workspace: 'demo', parent_id: parentId },
      provenance: source
    });
    expect(cycleRes.status).toBe(400);
    expect(await cycleRes.json()).toEqual({ error: 'parent_cycle_detected' });
    const taskCycleAfter = (await ok('/tasks/' + taskCycle.id)).task;
    expect(taskCycleAfter.control.version).toBe(1);
    expect(taskCycleAfter.payload_data.history.some((e: any) => e.kind === 'link')).toBe(false);

    await inDO(stub(), (h: any) => {
      h.sql.exec('UPDATE session_control_sessions SET parent_id = NULL WHERE id = ?', parentId);
    });
    const agent = await seedAgent('demo:assistant', 'family-' + ++seq, '["bus:read","bus:send","fleet:launch"]');
    await grant(agent, 'project', 'demo', 'write');
    const { task: taskForeign } = await create();
    const foreignRes = await call('/tasks/' + taskForeign.id + '/assign', {
      key: 'assign-foreign-parent-' + ++seq,
      expected_version: 1,
      session: { workspace: 'demo', parent_id: parentId },
      provenance: source
    }, agent.token);
    expect(foreignRes.status).toBe(403);
    expect(await foreignRes.json()).toEqual({ error: 'parent_forbidden' });
    const taskForeignAfter = (await ok('/tasks/' + taskForeign.id)).task;
    expect(taskForeignAfter.control.version).toBe(1);
    expect(taskForeignAfter.payload_data.history.some((e: any) => e.kind === 'link')).toBe(false);

    const { task: taskNoParent } = await create();
    const noParentAssigned = await ok('/tasks/' + taskNoParent.id + '/assign', {
      key: 'assign-no-parent-' + ++seq,
      expected_version: 1,
      session: { workspace: 'demo', mode: 'headless', brief: 'Synthetic no-parent goal' },
      provenance: source
    });
    await inDO(stub(), (h: any) => {
      const row = h.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', noParentAssigned.assigned_session.hub_session_id).toArray()[0];
      expect(row.parent_id).toBeNull();
      expect(row.parent_source).toBe('unknown_external');
    });
  });
  it('captures verbatim provenance, filters and appends originals without rewriting', async () => {
    const first = await create(); const id = first.task.id;
    expect(first.task.payload_data.history[0]).toMatchObject({ text: '  ORIGINAL synthetic request\n世界\n', source, authority: 'untrusted_task_data' });
    expect(first.task.payload_data.history[0].captured_at).toBeGreaterThan(0);
    await create('other');
    expect((await ok('/tasks?project=demo&status=open')).total).toBe(1);
    const secondSource = { type: 'voice', reference: 'fixture:voice-note', timestamp: '2026-01-01T01:02:03Z' };
    await append(id, 1, { kind: 'intake', text: 'authoritative provided transcript', source: secondSource });
    const shown = await ok('/tasks/' + id + '/history');
    expect(shown.task.payload_data.history).toHaveLength(2);
    expect(shown.task.payload_data.history[0]).toEqual(first.task.payload_data.history[0]);
    expect(shown.task.payload_data.history[1].source).toEqual(secondSource);
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
    expect(current.version).toBe(2); expect(current.payload_data.history).toHaveLength(2);
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
    expect(task.payload_data.history[1].evidence).toBe('external_unverified_not_launch_proof');
    task = (await append(task.id, task.version, { kind: 'note', text: 'DONE commit abc1234\nLive check: hub intake show\nResult: HTTP 200' })).task;
    await ok('/tasks/' + task.id, { key: 'close', expected_version: task.version, status: 'done' });
    const briefing = await ok('/handoff');
    expect(briefing.total).toBe(0);
    expect((await ok('/tasks/' + task.id + '/links')).task.payload_data.history).toHaveLength(10);
  });
  it('requires current session CSRF and supports current token-admin cookie and human admin bearer', async () => {
    expect((await SELF.fetch('https://hub.test/api/intake/tasks')).status).toBe(401);
    expect((await call('/tasks', { key: 'x' }, undefined, { 'x-csrf-token': '' })).status).toBe(403);
    expect((await call('/tasks', { key: 'x' }, undefined, { Origin: 'https://foreign.test' })).status).toBe(403);
    const tokenCookie = (await createSessionCookie(env as any, { sub: tokenAdminSubject('a'.repeat(64)), email: TOKEN_ADMIN_EMAIL, isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
    expect((await call('/handoff', undefined, undefined, { Cookie: tokenCookie })).status).toBe(200);
    const hash = await sha256('hubt_fixture_human');
    await inDO(stub(), (h: any) => h.sql.exec("INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES('human','human',?,'human-family','admin@fake.local','human:fixture','[\"admin\"]','fixture','2026-01-01',?)", hash, new Date(Date.now()+3600000).toISOString()));
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
  it('discovers active grantees for operator and excludes revoked/expired/unbound credentials', async () => {
    const a = await seedAgent('demo:assistant');
    const b = await seedAgent('demo:assistant');
    const c = await seedAgent('other:worker');

    const res = await ok('/grantees?actor=demo:assistant');
    expect(res.actor).toBe('demo:assistant');
    expect(res.total).toBe(2);
    expect(res.items.map((i: any) => i.credential_id).sort()).toEqual([a.id, b.id].sort());

    const item = res.items[0];
    expect(item).toHaveProperty('credential_id');
    expect(item).toHaveProperty('actor', 'demo:assistant');
    expect(item).toHaveProperty('family_id');
    expect(item).toHaveProperty('project', 'demo');
    expect(item).toHaveProperty('machine_id', 'fixture-host');
    expect(item).toHaveProperty('scopes');
    expect(item).toHaveProperty('created_at');
    expect(item).toHaveProperty('expires_at');
    expect(item).toHaveProperty('registered_at');
    expect(item).toHaveProperty('incarnation');
    expect(item).not.toHaveProperty('token_hash');
    expect(item).not.toHaveProperty('refresh_hash');
    expect(item).not.toHaveProperty('token');
    expect(item).not.toHaveProperty('secret');

    expect((await call('/grantees?actor=demo:assistant', undefined, a.token)).status).toBe(403);

    const unauth = await SELF.fetch('https://hub.test/api/intake/grantees?actor=demo:assistant');
    expect([401, 403]).toContain(unauth.status);

    expect((await call('/grantees')).status).toBe(400);
    expect((await call('/grantees?actor=')).status).toBe(400);
    expect((await call('/grantees?actor=bad%20actor')).status).toBe(400);

    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE credentials SET revoked_at='2026-01-01' WHERE id=?", b.id);
    });
    const afterRevoke = await ok('/grantees?actor=demo:assistant');
    expect(afterRevoke.total).toBe(1);
    expect(afterRevoke.items[0].credential_id).toBe(a.id);

    await inDO(stub(), (h: any) => {
      h.sql.exec("UPDATE credentials SET expires_at='2020-01-01' WHERE id=?", a.id);
    });
    const afterExpire = await ok('/grantees?actor=demo:assistant');
    expect(afterExpire.total).toBe(0);
    expect(afterExpire.items).toEqual([]);

    const specialId = '_cred-special-' + ++seq;
    await inDO(stub(), async (h: any) => {
      const hash = await sha256('token-special');
      const rh = await sha256('refresh-special');
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        specialId, 'agent', hash, rh, 'family-special', 'demo:assistant', 'fixture-host', 'demo:assistant', '["bus:read"]', 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
    });
    const specialGrant = await ok('/grants', { key: 'grant-special', credential_id: specialId, scope: 'project', target: 'demo', access: 'read', expires_at: Date.now() + 3600000, source });
    expect(specialGrant.grant.actor).toBe('demo:assistant');
    const allGrants = await ok('/grants');
    expect(allGrants.items.find((x: any) => x.id === specialGrant.grant.id).credential_id).toBe(specialId);
  });
  it('discovers active grantees for operator, ignoring expired-row noise', async () => {
    const activeAgent = await seedAgent('demo:assistant', 'family-active');
    await inDO(stub(), async (h: any) => {
      for (let i = 0; i < 30; i++) {
        const hash = await sha256('token-expired-' + i);
        const rh = await sha256('refresh-expired-' + i);
        h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
          'expired-cred-' + i, 'agent', hash, rh, 'family-expired-' + i, 'demo:assistant', 'fixture-host', 'demo:assistant', '["bus:read"]', 'fixture-operator',
          '2026-01-10T00:00:00.000Z', '2020-01-01T00:00:00.000Z');
      }
    });
    const res = await ok('/grantees?actor=demo:assistant');
    expect(res.total).toBe(1);
    expect(res.items).toHaveLength(1);
    expect(res.items[0].credential_id).toBe(activeAgent.id);
    expect(res.items[0].family_id).toBe('family-active');
  });
  it('rejects over-capacity active families with 507 record_capacity', async () => {
    await inDO(stub(), async (h: any) => {
      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
      h.sql.exec("INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES ('cap-assistant','demo','fixture-host','2026-01-01T00:00:00.000Z')");
      for (let i = 0; i < INTAKE_LIMITS.grants + 1; i++) {
        const hash = await sha256('token-cap-' + i);
        const rh = await sha256('refresh-cap-' + i);
        h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,registry_actor,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
          'cap-cred-' + i, 'agent', hash, rh, 'family-cap-' + i, 'demo:cap-assistant', 'fixture-host', 'demo:cap-assistant', 'demo:cap-assistant', '["bus:read"]', 'fixture-operator',
          '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
      }
    });
    const res = await call('/grantees?actor=demo:cap-assistant');
    expect(res.status).toBe(507);
  });
  it('rejects candidate overflow from many valid rows within the same family with 507 record_capacity', async () => {
    await inDO(stub(), async (h: any) => {
      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
      h.sql.exec("INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES ('samefam-assistant','demo','fixture-host','2026-01-01T00:00:00.000Z')");
      for (let i = 0; i < INTAKE_LIMITS.grants + 1; i++) {
        const hash = await sha256('token-samefam-' + i);
        const rh = await sha256('refresh-samefam-' + i);
        h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,registry_actor,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
          'samefam-cred-' + i, 'agent', hash, rh, 'family-same', 'demo:samefam-assistant', 'fixture-host', 'demo:samefam-assistant', 'demo:samefam-assistant', '["bus:read"]', 'fixture-operator',
          '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
      }
    });
    const res = await call('/grantees?actor=demo:samefam-assistant');
    expect(res.status).toBe(507);

    const resFamily = await call('/grantees?actor=demo:samefam-assistant&family=family-same');
    expect(resFamily.status).toBe(507);

    const resOther = await ok('/grantees?actor=demo:samefam-assistant&family=other-family');
    expect(resOther.total).toBe(0);
    expect(resOther.items).toEqual([]);
  });
  it('discovers active grantees with migrated registry_actor or bare legacy bindings', async () => {
    const hash1 = await sha256('token-migrated-1');
    const rh1 = await sha256('refresh-migrated-1');
    const hash2 = await sha256('token-migrated-2');
    const rh2 = await sha256('refresh-migrated-2');
    await inDO(stub(), (h: any) => {
      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at) VALUES ('fixture-host','fixture','fixture','2026-01-01T00:00:00.000Z')");
      h.sql.exec("INSERT OR IGNORE INTO agents(tag,project,machine_id,registered_at) VALUES ('migworker','demo','fixture-host','2026-01-01T00:00:00.000Z')");
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,registry_actor,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        'cred-migrated-1', 'agent', hash1, rh1, 'family-mig-1', 'demo:migworker', 'fixture-host', 'demo:migworker', 'demo:migworker', '["bus:read"]', 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,registry_actor,actor,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        'cred-migrated-2', 'agent', hash2, rh2, 'family-mig-2', 'migworker', 'fixture-host', 'demo:migworker', 'migworker', '["bus:read"]', 'fixture-operator', '2026-01-02T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
    });
    const res = await ok('/grantees?actor=demo:migworker');
    expect(res.total).toBe(2);
    const ids = res.items.map((i: any) => i.credential_id).sort();
    expect(ids).toEqual(['cred-migrated-1', 'cred-migrated-2'].sort());
  });
  it('denies grantee discovery to already-granted agents, hosts, and jobs', async () => {
    const a = await seedAgent();
    await grant(a);
    expect((await call('/grantees?actor=demo:assistant', undefined, a.token)).status).toBe(403);

    const hostToken = 'hubh_fixture_host';
    const hostHash = await sha256(hostToken);
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,machine_id,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
        'cred-host', 'host', hostHash, 'family-host', 'fixture-host', 'fixture-host', '["fleet:host"]', 'operator', '2026-01-01T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
    });
    expect((await call('/grantees?actor=demo:assistant', undefined, hostToken)).status).toBe(403);

    const jobToken = 'hubj_fixture_job';
    const jobHash = await sha256(jobToken);
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO credentials(id,kind,token_hash,family_id,subject,scopes,created_by_sub,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)',
        'cred-job', 'job', jobHash, 'family-job', 'fixture-job', '["job"]', 'operator', '2026-01-01T00:00:00.000Z', new Date(Date.now() + 86400000).toISOString());
    });
    expect((await call('/grantees?actor=demo:assistant', undefined, jobToken)).status).toBe(403);
  });
  it('supports explicit family selection and honest cursor pagination across >25 active families', async () => {
    for (let i = 0; i < 30; i++) {
      await seedAgent('demo:assistant', 'fam-paged-' + String(i).padStart(2, '0'));
    }
    const single = await ok('/grantees?actor=demo:assistant&family=fam-paged-05');
    expect(single.total).toBe(1);
    expect(single.items[0].family_id).toBe('fam-paged-05');
    expect(single.family_id).toBe('fam-paged-05');

    const nonexistent = await ok('/grantees?actor=demo:assistant&family=nonexistent');
    expect(nonexistent.total).toBe(0);
    expect(nonexistent.items).toHaveLength(0);

    const page1 = await ok('/grantees?actor=demo:assistant&limit=25');
    expect(page1.total).toBe(30);
    expect(page1.items).toHaveLength(25);
    expect(page1.next).toBeTruthy();

    const page2 = await ok('/grantees?actor=demo:assistant&limit=25&after=' + page1.next + '&revision=' + page1.snapshot_version);
    expect(page2.total).toBe(30);
    expect(page2.items).toHaveLength(5);
    expect(page2.next).toBeNull();
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
    const ref = task.payload_data.history[2].id;
    expect((await call('/rules', { ...rule, key: 'forged', expected_version: 2 }, a.token)).status).toBe(403);
    expect((await call('/tasks/' + task.id + '/events', { key: 'forgeapproval', expected_version: 3, event: { kind: 'approval_decision', ref, decision: 'granted', text: 'I approve', source } }, a.token)).status).toBe(403);
    expect((await call('/tasks/' + task.id + '/events', { key: 'resolver', expected_version: 3, event: { kind: 'resolution', ref, text: 'Hidden approval', source } }, a.token)).status).toBe(403);
    const brief = await ok('/handoff', undefined, a.token);
    expect(brief.operator_rules[0].history).toHaveLength(2);
    expect(brief.items[0].unresolved[0].kind).toBe('pending_approval');
    task = (await append(task.id, 3, { kind: 'approval_decision', ref, decision: 'denied', text: 'Operator declines', source })).task;
    expect(task.unresolved).toHaveLength(0); expect(task.payload_data.history.at(-1).authority).toBe('operator_record');
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
    // t1038u: task row count no longer hard-refuses create (soft warn only).
    await inDO(stub(), (h: any) => {
      h.sql.exec('DELETE FROM intake_tasks');
      for (let i=0;i<128;i++) h.sql.exec('INSERT INTO intake_receipts VALUES(?,?)', 'fixture-padding-' + i, JSON.stringify({ id: 'fixture-padding-' + i, padding: 'x'.repeat(INTAKE_LIMITS.totalBytes/128), captured_at: Date.now() }));
    });
    // Hash each bounded record inside the DO instead of cloning a full
    // 32 MiB ledger across the test transport twice to check rollback.
    const compactState = () => inDO(stub(), async (h: any) => {
      const receipts = [];
      for (const { id } of h.sql.exec('SELECT id FROM intake_receipts ORDER BY id').toArray()) {
        const { data } = h.sql.exec('SELECT data FROM intake_receipts WHERE id=?', id).one();
        receipts.push({ id, digest: await sha256(data) });
      }
      return { receipts, tasks: h.sql.exec('SELECT * FROM intake_tasks ORDER BY id').toArray(), meta: h.sql.exec('SELECT * FROM intake_meta').toArray() };
    });
    before = await compactState(); expect((await call('/tasks', { key: 'globalcap', project: 'demo', title: 'x', text: 'x', source })).status).toBe(507); expect(await compactState()).toEqual(before);
  });
  it('archives legacy terminal tasks in batches without deleting payloads', async () => {
    const stale = Date.now() - 20 * 86400000;
    const seed = 300;
    await inDO(stub(), (h: any) => {
      for (let i = 0; i < seed; i++) h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-' + i, JSON.stringify({ id: 'fixture-' + i, project: 'demo', status: 'done', updated_at: stale, history: [] }));
    });
    const created = await ok('/tasks', { key: 'after-prune', project: 'demo', title: 'Synthetic', text: 'Synthetic', source });
    expect(created.task.id).toBeTruthy();
    expect(await inDO(stub(), (h: any) => h.sql.exec("SELECT COUNT(*) AS n FROM intake_tasks WHERE id LIKE 'fixture-%'").one().n)).toBe(seed);
  });
  it('retains both old and fresh terminal payloads during archival', async () => {
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-aged', JSON.stringify({ id: 'fixture-aged', project: 'demo', status: 'done', updated_at: Date.now() - 120_000, history: [] }));
      h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-fresh', JSON.stringify({ id: 'fixture-fresh', project: 'demo', status: 'cancelled', updated_at: Date.now() - 30_000, history: [] }));
    });
    await ok('/tasks', { key: 'retention-boundary', project: 'demo', title: 'Synthetic', text: 'Synthetic', source });
    expect(await inDO(stub(), (h: any) => h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks WHERE id=?', 'fixture-aged').one().n)).toBe(1);
    expect(await inDO(stub(), (h: any) => h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks WHERE id=?', 'fixture-fresh').one().n)).toBe(1);
  });
  it('keeps terminal tasks within retention but still admits create (soft task cap)', async () => {
    const seed = 120;
    await inDO(stub(), (h: any) => {
      for (let i = 0; i < seed; i++) h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-' + i, JSON.stringify({ id: 'fixture-' + i, project: 'demo', status: 'done', updated_at: Date.now() - 30_000, history: [] }));
    });
    const created = await ok('/tasks', { key: 'cap-still', project: 'demo', title: 'Synthetic', text: 'Synthetic', source });
    expect(created.task.id).toBeTruthy();
    expect(await inDO(stub(), (h: any) => h.sql.exec("SELECT COUNT(*) AS n FROM intake_tasks WHERE id LIKE 'fixture-%'").one().n)).toBe(seed);
  });

  it('emits intake_open_warning on list/create when open non-terminal count exceeds 100', async () => {
    await inDO(stub(), (h: any) => {
      h.sql.exec('DELETE FROM intake_tasks');
      for (let i = 0; i < 101; i++) {
        h.sql.exec('INSERT INTO intake_tasks VALUES(?,?)', 'fixture-open-' + i, JSON.stringify({
          id: 'fixture-open-' + i, project: 'demo', title: 'Open ' + i, status: 'open', version: 1,
          created_at: Date.now(), updated_at: Date.now(), history: [],
        }));
      }
    });
    const listed = await ok('/tasks?status=open');
    expect(listed.open_non_terminal_count).toBeGreaterThan(100);
    expect(listed.intake_open_warning?.code).toBe('intake_open_high');
    expect(listed.intake_open_warning?.action).toBe('review_done_evidence');
    expect(listed.intake_open_warning.items).toHaveLength(101);
    expect(listed.intake_open_warning.items[0]).toMatchObject({ id: expect.any(String), title: expect.any(String), age_ms: expect.any(Number) });
    const created = await ok('/tasks', { key: 'warn-create', project: 'demo', title: 'Synthetic', text: 'Synthetic', source });
    expect(created.task.id).toBeTruthy();
    expect(created.intake_open_warning?.code).toBe('intake_open_high');
  });
  it('prunes stale and legacy replay receipts on create while fresh receipts keep replaying', async () => {
    const payload = { key: 'receipt-replay', project: 'demo', title: 'Synthetic', text: 'Original', source };
    const first = await ok('/tasks', payload);
    await inDO(stub(), (h: any) => {
      h.sql.exec('INSERT INTO intake_receipts VALUES(?,?)', 'stale-receipt', JSON.stringify({ id: 'stale-receipt', fingerprint: 'x', response: {}, captured_at: Date.now() - 20 * 86400000 }));
      h.sql.exec('INSERT INTO intake_receipts VALUES(?,?)', 'legacy-receipt', JSON.stringify({ id: 'legacy-receipt', fingerprint: 'x', response: {} }));
    });
    expect((await ok('/tasks', { key: 'after-receipt-prune', project: 'demo', title: 'Synthetic', text: 'Synthetic', source })).task.id).toBeTruthy();
    expect(await inDO(stub(), (h: any) => h.sql.exec("SELECT COUNT(*) AS n FROM intake_receipts WHERE id IN ('stale-receipt','legacy-receipt')").one().n)).toBe(0);
    expect(await ok('/tasks', payload)).toEqual(first);
  });
});


describe('archive retention, evidence gates and durable audit', () => {
  const done = { kind: 'note', text: 'DONE commit abc1234\nLive check: curl https://fixture.test/health\nResult: HTTP 200 healthy' };
  it('refuses missing or incomplete evidence, cancels only with explicit reason and disposition', async () => {
    let { task } = await create();
    for (const text of ['', 'DONE abc1234', 'DONE\nLive check: curl /health\nResult: OK', 'DONE abc1234\nLive check: curl /health']) {
      if (text) task = (await append(task.id, task.version, { kind: 'note', text })).task;
      const res = await call('/tasks/' + task.id, { key: 'bad-close-' + ++seq, expected_version: task.version, status: 'done' });
      expect(res.status).toBe(409); expect(await res.json()).toMatchObject({ error: 'done_evidence_required' });
    }
    const denied = await call('/tasks/' + task.id, { key: 'bad-cancel', expected_version: task.version, status: 'cancelled', reason: 'old' });
    expect(denied.status).toBe(409);
    task = (await ok('/tasks/' + task.id, { key: 'cancel-explicit', expected_version: task.version, status: 'cancelled', reason: 'dropped by Alexey: no longer requested' })).task;
    expect(task.archived_at).toEqual(expect.any(Number));
    expect((await ok('/tasks/' + task.id + '/audit')).items).toEqual(expect.arrayContaining([expect.objectContaining({ kind: 'cancel', from_status: 'open', to_status: 'cancelled', reason: 'dropped by Alexey: no longer requested' })]));
  });
  it('preserves closed payload/history after five minutes; supports archive filters, notes, reopen and fresh evidence', async () => {
    let { task } = await create();
    task = (await append(task.id, task.version, done)).task;
    const close = { key: 'archive-close', expected_version: task.version, status: 'done' };
    task = (await ok('/tasks/' + task.id, close)).task;
    expect(await ok('/tasks/' + task.id, close)).toHaveProperty('task.id', task.id);
    await inDO(stub(), (h: any) => h.sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.updated_at',?) WHERE id=?", Date.now() - 301000, task.id));
    await create(); // concurrent prune/archive activity triggered through normal write
    const shown = (await ok('/tasks/' + task.id.slice(0, 8))).task;
    expect(shown.payload_data.history).toEqual(task.payload_data.history);
    expect(shown.archived_at).toEqual(task.archived_at);
    expect((await ok('/tasks')).items.map((t: any) => t.id)).not.toContain(task.id);
    for (const filter of ['all=true', 'archived=true', 'status=done'])
      expect((await ok('/tasks?' + filter)).items.map((t: any) => t.id)).toContain(task.id);
    const events = (await ok('/tasks/' + task.id + '/audit')).items;
    expect(events.filter((e: any) => e.kind === 'close')).toHaveLength(1);
    expect(events.map((e: any) => e.kind)).toEqual(['close', 'archive']);
    expect(events.find((e: any) => e.kind === 'close')).toMatchObject({ actor: 'human:operator', from_status: 'open', to_status: 'done', captured_at: expect.any(Number), reason: done.text });
    task = (await append(task.id.slice(0, 8), task.version, { kind: 'note', text: 'Post-close observation retained' })).task;
    task = (await ok('/tasks/' + task.id.slice(0, 8), { key: 'reopen', expected_version: task.version, status: 'open', reason: 'additional work requested' })).task;
    expect(task.archived_at).toBeNull();
    expect((await call('/tasks/' + task.id, { key: 'stale-evidence', expected_version: task.version, status: 'done' })).status).toBe(409);
    expect((await ok('/tasks/' + task.id + '/audit')).items[0].kind).toBe('reopen');
  });
  it('list/show/update/history agree for every visible full id and prefix during parallel archive passes', async () => {
    const first = await create(), second = await create();
    const closed = await append(second.task.id, 1, done);
    await ok('/tasks/' + second.task.id, { key: 'agree-close', expected_version: closed.task.version, status: 'done' });
    for (const query of ['', '?all=true', '?archived=true']) {
      const list = await ok('/tasks' + query);
      for (const entry of list.items) {
        await Promise.all([create('other'), ...[entry.id, entry.id.slice(0, 8)].map(async id => {
          expect((await ok('/tasks/' + id)).task.id).toBe(entry.id);
          expect((await ok('/tasks/' + id + '/history')).task.payload_data.history).toEqual(entry.payload_data.history);
        })]);
        const updated = await ok('/tasks/' + entry.id.slice(0, 8), { key: 'agree-update-' + ++seq, expected_version: entry.version, next_step: 'verified' });
        expect(updated.task.id).toBe(entry.id);
      }
    }
    expect((await ok('/tasks/' + first.task.id)).task.status).toBe('open');
  });
  it('audit is append-only, survives row loss, and does not disclose other projects', async () => {
    let { task } = await create('secret');
    task = (await append(task.id, 1, done)).task;
    task = (await ok('/tasks/' + task.id, { key: 'audit-close', expected_version: task.version, status: 'done' })).task;
    const agent = await seedAgent(); await grant(agent);
    expect((await call('/tasks/' + task.id + '/audit', undefined, agent.token)).status).toBe(404);
    expect((await ok('/audit', undefined, agent.token)).items.every((e: any) => e.project === 'demo')).toBe(true);
    await inDO(stub(), (h: any) => {
      expect(() => h.sql.exec('UPDATE intake_audit SET reason=? WHERE task_id=?', 'tamper', task.id)).toThrow();
      expect(() => h.sql.exec('DELETE FROM intake_audit WHERE task_id=?', task.id)).toThrow();
      h.sql.exec('DELETE FROM intake_tasks WHERE id=?', task.id); // disposable fixture only
    });
    const events = (await ok('/tasks/' + task.id.slice(0, 8) + '/audit')).items;
    expect(events.map((e: any) => e.kind)).toEqual(['delete', 'close', 'archive']);
  });
});
