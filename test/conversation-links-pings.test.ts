import { expect, it } from 'vitest';
import { env as testBindings, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import { Hub } from '../src/conversation-hub';
import { ConversationsService, authenticateConversationRequest } from '../src/conversations';
import { ChecksService, type ActionDeps } from '../src/checks';
import { RETENTION_CLEANUP_CURSOR_SQL } from '../src/bus';
import { createSessionCookie } from '../src/auth/session';
import type { FollowUpIntent } from '../src/conversation-automation';

const env = testBindings as unknown as Env;
const csrf = 'composed-local-fixture';
const target = 'human:compose-target';
const tables = ['messages', 'claims', 'conversations', 'conversation_messages',
  'conversation_members', 'conversation_links', 'hub_check_slots',
  'conversation_automation_receipts', 'conversation_automation_grants'] as const;
type Http = (path: string, method?: string, body?: unknown, cookie?: string) => Promise<Response>;

async function within(fn: (h: Hub, state: DurableObjectState) => Promise<void>) {
  const stub = env.HUB.get(env.HUB.idFromName('compose-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (_h, state) => fn(new Hub(state, env), state));
}
async function data(response: Response, status = 200): Promise<any> {
  expect(response.status).toBe(status);
  return response.json();
}
function count(h: Hub, table: string, where = '', ...params: any[]) {
  return Number(h.sql.exec(`SELECT count(*) n FROM ${table} ${where}`, ...params).toArray()[0].n);
}
function counts(h: Hub) {
  return Object.fromEntries(tables.map(t => [t, count(h, t)]));
}
function total(c: Record<string, number>) { return Object.values(c).reduce((a, b) => a + b, 0); }
function snapshot(h: Hub) {
  return Object.fromEntries([...tables, 'hub_checks', 'conversation_storage_usage', 'retention_cleanup_cursor']
    .map(t => [t, h.sql.exec(`SELECT * FROM ${t} ORDER BY rowid`).toArray()]));
}
function assertAccounting(before: Record<string, number>, h: Hub, result: any, limit: number) {
  const after = counts(h), actual = total(before) - total(after);
  expect(actual).toBe(result.messagesDeleted + result.claimsDeleted + result.conversationRowsDeleted);
  expect(result.conversationRowsDeleted).toBe(result.conversationsDeleted + result.conversationMessagesDeleted +
    result.conversationMembersDeleted + result.conversationLinksDeleted + result.automationRowsDeleted);
  expect(actual).toBeLessThanOrEqual(limit);
  expect(actual).toBeGreaterThanOrEqual(0);
  expect(result.conversationLinksDeleted).toBe(before.conversation_links - after.conversation_links);
  expect(result.automationRowsDeleted).toBe(before.hub_check_slots + before.conversation_automation_receipts +
    before.conversation_automation_grants - after.hub_check_slots - after.conversation_automation_receipts - after.conversation_automation_grants);
  return after;
}
function port(h: Hub, svc = h.conversationsService): ActionDeps {
  return { liveness: h.livenessService, notify: async () => {}, message: async () => '',
    launch: async () => '', fetch: async () => new Response(null), evaluateQuota: () => [],
    conversation: { evaluateBacklog: q => svc.evaluateScheduledBacklog(q), sendFollowUp: i => svc.postScheduledFollowUp(i) } };
}
async function fixture(h: Hub) {
  const t = Date.now();
  const cookies = await Promise.all(['compose-owner', 'compose-target', 'compose-stranger'].map(async sub =>
    (await createSessionCookie(env, { sub, email: 'admin@fake.local', isAdmin: true,
      csrfToken: csrf, exp: Math.floor(t / 1000) + 86400 })).split(';')[0]));
  const [owner, recipient, stranger] = cookies;
  const http: Http = (path, method = 'GET', body, cookie = owner) => h.fetch(new Request('https://hub.test' + path, {
    method, headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
  for (const c of cookies) await data(await http('/api/conversations/participants', 'GET', undefined, c));
  const conv = await data(await http('/api/conversations', 'POST', { kind: 'group', title: 'Links and follow-ups', members: [target] }), 201);
  await data(await http(`/api/conversations/${conv.id}/messages`, 'POST', { body: 'Unread source', idempotency_key: 'source' }), 201);
  const ref = crypto.randomUUID(), at = new Date(t).toISOString();
  h.sql.exec(`INSERT INTO routing_jobs (id,actor,request_key,request_json,state,created_at,updated_at)
    VALUES (?,'synthetic:job-owner',?,'{}','queued',?,?)`, ref, ref, at, at);
  const link = await data(await http(`/api/conversations/${conv.id}/links`, 'POST', { kind: 'routing_job', ref, idempotency_key: 'job' }), 201);
  const grant = await data(await http(`/api/conversations/${conv.id}/automation`, 'PUT', {
    check: 'compose', target_actor: target, body: 'Follow-up {detail}', on: 'always', expires_at: t + 3600000,
  }));
  const definition = { name: 'compose', kind: 'routine', interval_s: 300, params: {},
    action: { type: 'message', to: target, body: 'Follow-up {detail}', on: 'always', conversation_id: conv.id, grant_id: grant.grant_id } };
  await data(await http('/api/checks', 'PUT', { checks: [definition] }));
  return { t, cid: conv.id, ref, link, grant, http, owner, recipient, stranger };
}
function seedOrphanLink(h: Hub, id: string) {
  h.sql.exec(`INSERT INTO conversation_links VALUES (?, 'missing-parent', 'routing_job', ?, 'human:compose-owner', '2000-01-01', NULL)`, id, crypto.randomUUID());
}
function seedRetiredAutomation(h: Hub, suffix: string) {
  h.sql.exec(`INSERT INTO hub_check_slots VALUES (?, ?, 0, '{"action":{}}', NULL, NULL, NULL, 'done', NULL, 0)`, 'removed-' + suffix, 'generation-' + suffix);
  h.sql.exec(`INSERT INTO conversation_automation_receipts VALUES (?, '{}', 'old-message', 'missing-parent', 0)`, 'receipt-' + suffix);
  h.sql.exec(`INSERT INTO conversation_automation_grants VALUES (?, 'missing-parent', ?, ?, NULL, 'compose-owner', 0, 'old', 'always', NULL)`, 'grant-' + suffix, 'removed-' + suffix, target);
}

it('keeps public linked follow-ups current, system-authored and unread while preserving target tombstones and reverse privacy', async () => {
  await within(async h => {
    const f = await fixture(h);
    const watermarks = h.sql.exec('SELECT actor,last_read_seq FROM conversation_members ORDER BY actor').toArray();
    expect((await data(await f.http('/api/checks/compose/run', 'POST', {}))).results[0].action).toMatch(/^conversation_message /);
    seedOrphanLink(h, 'orphan'); seedRetiredAutomation(h, 'positive');
    const before = counts(h), cleanup = h.busService.retentionCleanup(f.t, 20) as any;
    assertAccounting(before, h, cleanup, 20);
    expect(cleanup.conversationLinksDeleted).toBe(1);
    expect(cleanup.automationRowsDeleted).toBeGreaterThan(0);
    expect((await new ChecksService(h.sql, () => f.t + 305000).runDue(port(h), 'compose'))[0].action).toMatch(/^conversation_message /);
    expect(h.sql.exec('SELECT actor,last_read_seq FROM conversation_members ORDER BY actor').toArray()).toEqual(watermarks);
    const page = await data(await f.http(`/api/conversations/${f.cid}/messages`, 'GET', undefined, f.recipient));
    expect(page.my_read_seq).toBe(0); expect(page.unread_count).toBe(3);
    expect(page.messages.slice(1).map((m: any) => [m.sender_actor, m.sender_kind, m.kind])).toEqual([
      ['system:conversation-checks', 'system', 'note'], ['system:conversation-checks', 'system', 'note'],
    ]);
    const reverse = `/api/conversations/links?kind=routing_job&ref=${f.ref}&limit=1`;
    expect((await data(await f.http(reverse))).links.map((l: any) => l.conversation_id)).toEqual([f.cid]);
    expect((await data(await f.http(reverse, 'GET', undefined, f.stranger))).links).toEqual([]);
    expect((await f.http(`/api/conversations/${f.cid}/links`, 'GET', undefined, f.stranger)).status).toBe(403);
    const foreign = await f.http(`/api/conversations/${f.cid}/automation`, 'PUT', { check: 'compose' }, f.recipient);
    expect(foreign.status).toBe(403);
    h.sql.exec('DELETE FROM routing_jobs WHERE id=?', f.ref);
    const tombstone = await data(await f.http(`/api/conversations/${f.cid}/links/${f.link.id}`));
    expect(tombstone.target).toMatchObject({ available: false, canonical_status: null, complete: false, terminal: false });
    expect((await f.http(reverse)).status).toBe(404);
    expect((await f.http(`/api/conversations/${f.cid}/links`, 'POST', { kind: 'routing_job', ref: f.ref, idempotency_key: 'job' })).status).toBe(404);
    expect(count(h, 'conversation_messages')).toBe(3);
  });
});

for (const limit of [1, 2]) it(`gives every mixed category a fair bounded turn under replenishment at limit=${limit}`, async () => {
  await within(async h => {
    const f = await fixture(h);
    const archived = await data(await f.http('/api/conversations', 'POST', { kind: 'group', title: 'Expired parent' }), 201);
    h.sql.exec("UPDATE conversations SET archived_at='2000-01-01' WHERE id=?", archived.id);
    await data(await f.http(`/api/conversations/${f.cid}/members`, 'POST', { actor: 'human:compose-stranger' }), 201);
    await data(await f.http(`/api/conversations/${f.cid}/members/human:compose-stranger`, 'DELETE'));
    h.sql.exec("UPDATE conversation_members SET removed_at='2000-01-01' WHERE removed_at IS NOT NULL");
    for (let i = 0; i < 16; i++) {
      seedRetiredAutomation(h, String(i)); seedOrphanLink(h, 'orphan-' + i);
      h.sql.exec("INSERT INTO messages (id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES (?,'a','b','note','synthetic','{}',?,0,1)", 'bus-' + i, 'bus-' + i);
      h.sql.exec("INSERT INTO claims (id,project,scope,holder,created_at,expires_at) VALUES (?,'synthetic',?,'holder',0,1)", 'claim-' + i, 'scope-' + i);
    }
    const seen = new Set<string>();
    for (let i = 0; i < 36; i++) {
      await data(await f.http(`/api/conversations/${f.cid}/messages`, 'POST', { body: 'Replenished expired', idempotency_key: 'replenish-' + i }), 201);
      h.sql.exec("UPDATE conversation_messages SET created_at='2000-01-01' WHERE conversation_id=?", f.cid);
      seedOrphanLink(h, 'replenished-' + i);
      const before = counts(h), result = h.busService.retentionCleanup(f.t, limit) as any;
      const after = assertAccounting(before, h, result, limit);
      for (const table of tables) if (before[table] > after[table]) seen.add(table);
      if (result.removedMembersDeleted) seen.add('removed_member');
      expect(result.hasMore).toBe(true);
    }
    expect(seen).toEqual(new Set([...tables, 'removed_member']));
    expect(count(h, 'conversation_links', 'WHERE id=?', f.link.id)).toBe(1);
    expect(count(h, 'conversation_automation_grants', 'WHERE id=?', f.grant.grant_id)).toBe(1);
  });
});

for (const limit of [1, 2]) it(`drains an expired linked parent and automation children with truthful limit=${limit} accounting`, async () => {
  await within(async h => {
    const f = await fixture(h);
    expect((await new ChecksService(h.sql, () => f.t).runDue(port(h), 'compose'))[0].action).toMatch(/^conversation_message /);
    const old = JSON.parse(String(h.sql.exec("SELECT intent_json FROM hub_check_slots WHERE name='compose'").toArray()[0].intent_json));
    await data(await f.http(`/api/conversations/${f.cid}/archive`, 'POST', {}));
    h.sql.exec("UPDATE conversations SET archived_at='2000-01-01' WHERE id=?", f.cid);
    let links = 0, automation = 0, removed = 0;
    for (let i = 0; i < 30; i++) {
      const before = counts(h), result = h.busService.retentionCleanup(f.t, limit) as any;
      assertAccounting(before, h, result, limit);
      links += result.conversationLinksDeleted; automation += result.automationRowsDeleted; removed += result.conversationsDeleted;
      expect(h.sql.exec(`SELECT id FROM conversation_links l WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=l.conversation_id)`).toArray()).toEqual([]);
      if (!count(h, 'conversations')) {
        expect(count(h, 'conversation_automation_grants')).toBe(0);
        expect(count(h, 'conversation_automation_receipts')).toBe(0);
      }
      if (!result.hasMore) break;
    }
    expect({ links, automation, removed }).toEqual({ links: 1, automation: 2, removed: 1 });
    expect(count(h, 'conversation_messages') + count(h, 'conversation_members')).toBe(0);
    expect(() => h.conversationsService.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
  });
});

it('rolls back every mixed child, authority update, counter and cursor after a late SQL abort, then retries once', async () => {
  await within(async h => {
    const f = await fixture(h);
    const p = port(h);
    p.conversation!.sendFollowUp = i => { h.conversationsService.postScheduledFollowUp(i); throw Error('lost response'); };
    expect((await new ChecksService(h.sql, () => f.t).runDue(p, 'compose'))[0].status).toBe('error');
    seedOrphanLink(h, 'orphan'); seedRetiredAutomation(h, 'rollback');
    h.sql.exec("INSERT INTO messages (id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES ('bus','a','b','note','th','{}','bus',0,1)");
    await data(await f.http(`/api/conversations/${f.cid}/archive`, 'POST', {}));
    h.sql.exec("UPDATE conversations SET archived_at='2000-01-01' WHERE id=?", f.cid);
    h.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    h.sql.exec("INSERT OR REPLACE INTO retention_cleanup_cursor VALUES ('conv',6)");
    // All child tables must already be empty when the final header trigger runs.
    // Abort this late to exercise previous bus/conv cursor and slot UPDATE rollback.
    h.sql.exec(`CREATE TRIGGER abort_composed_header BEFORE DELETE ON conversations BEGIN
      SELECT CASE WHEN (SELECT count(*) FROM conversation_links) != 0
        OR (SELECT count(*) FROM conversation_automation_grants) != 0
        OR (SELECT count(*) FROM conversation_automation_receipts) != 0
        THEN RAISE(ABORT,'premature parent delete') ELSE RAISE(ABORT,'late composed abort') END; END`);
    const before = snapshot(h);
    expect(() => h.busService.retentionCleanup(f.t, 100)).toThrow('late composed abort');
    expect(snapshot(h)).toEqual(before);
    h.sql.exec('DROP TRIGGER abort_composed_header');
    const prior = counts(h), result = h.busService.retentionCleanup(f.t, 100) as any;
    assertAccounting(prior, h, result, 100);
    expect(result.conversationsDeleted).toBe(1);
    expect(result.conversationLinksDeleted).toBe(2);
    expect(result.automationRowsDeleted).toBe(5);
    expect(result.hasMore).toBe(false);
    const again = h.busService.retentionCleanup(f.t, 100) as any;
    expect(again.conversationRowsDeleted + again.messagesDeleted + again.claimsDeleted).toBe(0);
  });
});

for (const accepted of [false, true]) it(`link cleanup/restart preserves accepted=${accepted} unresolved frozen intent and converges exactly once`, async () => {
  await within(async (h, state) => {
    const f = await fixture(h); let old!: FollowUpIntent;
    const p = port(h);
    p.conversation!.sendFollowUp = i => { old = i; if (accepted) h.conversationsService.postScheduledFollowUp(i); throw Error('lost response'); };
    expect((await new ChecksService(h.sql, () => f.t).runDue(p, 'compose'))[0].status).toBe('error');
    const slot = h.sql.exec("SELECT * FROM hub_check_slots WHERE name='compose'").toArray()[0];
    const receipts = h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray();
    for (let i = 0; i < 5; i++) seedOrphanLink(h, 'orphan-' + i);
    // Remove the canonical link target, not the conversation or grant authority.
    h.sql.exec('DELETE FROM routing_jobs WHERE id=?', f.ref);
    for (let i = 0; i < 5; i++) {
      const before = counts(h), r = h.busService.retentionCleanup(f.t, 1) as any;
      assertAccounting(before, h, r, 1); expect(r.conversationLinksDeleted).toBe(1);
      expect(r.automationRowsDeleted).toBe(0);
    }
    expect(h.sql.exec("SELECT * FROM hub_check_slots WHERE name='compose'").toArray()).toEqual([slot]);
    expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual(receipts);
    const service = new ConversationsService(h.sql, fn => state.storage.transactionSync(fn), { now: () => f.t + 305000 });
    service.init(); service.init();
    expect(JSON.parse(String(h.sql.exec("SELECT intent_json FROM hub_check_slots WHERE name='compose'").toArray()[0].intent_json))).toEqual(old);
    expect((await new ChecksService(h.sql, () => f.t + 305000).runDue(port(h, service), 'compose'))[0].action).toMatch(/^conversation_message /);
    expect(count(h, 'conversation_messages')).toBe(2);
    const page = await data(await f.http(`/api/conversations/${f.cid}/messages`, 'GET', undefined, f.recipient));
    expect(page.my_read_seq).toBe(0); expect(page.messages[1].sender_actor).toBe('system:conversation-checks');
    expect((await data(await f.http(`/api/conversations/${f.cid}/links/${f.link.id}`))).target.available).toBe(false);
    const before = counts(h), r = h.busService.retentionCleanup(f.t + 305000, 2) as any;
    assertAccounting(before, h, r, 2); expect(r.automationRowsDeleted).toBe(1);
    expect(() => service.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
    expect(count(h, 'conversation_messages')).toBe(2);
  });
});

it('initializes both standalone handlers and membership trigger before HTTP routes and bounded startup cleanup', async () => {
  await within(async (h, state) => {
    for (const row of h.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'conversation%' OR name='hub_check_slots')").toArray()) h.sql.exec(`DROP TABLE ${row.name}`);
    const svc = new ConversationsService(h.sql, fn => state.storage.transactionSync(fn));
    svc.init(); svc.init();
    for (const table of ['conversation_links', 'conversation_automation_grants', 'conversation_automation_receipts', 'hub_check_slots']) {
      expect(h.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table).toArray()).toHaveLength(1);
    }
    expect(h.sql.exec("SELECT name FROM sqlite_master WHERE type='trigger' AND name='conversation_automation_remove_member'").toArray()).toHaveLength(1);
    const f = await fixture(h);
    const http: Http = async (path, method = 'GET', body, cookie = f.owner) => {
      const req = new Request('https://hub.test' + path, { method, headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
      return svc.handle(req, await authenticateConversationRequest(env, h.sql, req));
    };
    expect((await data(await http(`/api/conversations/${f.cid}/links`))).links).toHaveLength(1);
    // Re-init performs bounded orphan cleanup, sharing authority with M5 rows.
    for (let i = 0; i < 105; i++) seedOrphanLink(h, 'startup-' + i);
    seedRetiredAutomation(h, 'startup');
    const before = counts(h); svc.init();
    const deleted = total(before) - total(counts(h));
    expect(deleted).toBeLessThanOrEqual(100); expect(deleted).toBeGreaterThan(0);
    expect(svc.hasExpiredRows()).toBe(true);
    for (let i = 0; i < 20 && svc.hasExpiredRows(); i++) {
      const previous = counts(h), r = svc.retentionCleanup(Date.now(), 2);
      expect(total(previous) - total(counts(h))).toBe(r.rowsDeleted);
      expect(r.rowsDeleted).toBeLessThanOrEqual(2);
    }
    expect(svc.hasExpiredRows()).toBe(false);
    const settled = snapshot(h); svc.init(); svc.init(); expect(snapshot(h)).toEqual(settled);
    await data(await http(`/api/conversations/${f.cid}/members/${target}`, 'DELETE'));
    expect(h.sql.exec('SELECT revoked_at FROM conversation_automation_grants WHERE id=?', f.grant.grant_id).toArray()[0].revoked_at).not.toBeNull();
    expect((await data(await http(`/api/conversations/${f.cid}/links/${f.link.id}`))).target.available).toBe(true);
  });
});
