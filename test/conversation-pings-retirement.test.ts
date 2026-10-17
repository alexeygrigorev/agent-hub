import { beforeEach, expect, it } from 'vitest';
import { SELF, env, runInDurableObject } from 'cloudflare:test';
import { Hub } from '../src/conversation-hub';
import { ChecksService, validateCheck, type ActionDeps } from '../src/checks';
import { ConversationsService } from '../src/conversations';
import { createSessionCookie } from '../src/auth/session';
import type { FollowUpIntent } from '../src/conversation-automation';

const csrf = 'retirement-fixture', target = 'human:retire-target';
const inside = <T>(fn: (h: Hub, s: DurableObjectState) => T | Promise<T>) => runInDurableObject((env as any).HUB.get((env as any).HUB.idFromName('hub')), fn);
async function request(path: string, auth: string, method = 'GET', body?: unknown) {
  return SELF.fetch('https://hub.test' + path, { method, headers: { Cookie: auth, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
}
async function json(res: Response, status = 200): Promise<any> { expect(res.status).toBe(status); return res.json(); }
async function fixture() {
  const t = Math.floor(Date.now() / 300000) * 300000 + 60000;
  const cookies = await Promise.all(['retire-owner', 'retire-target'].map(sub => createSessionCookie(env as any, { sub, email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(t / 1000) + 30 * 86400 })));
  const [owner, recipient] = cookies.map(c => c.split(';')[0]);
  for (const auth of [owner, recipient]) await json(await request('/api/conversations/participants', auth));
  const conv = await json(await request('/api/conversations', owner, 'POST', { kind: 'group', title: 'Retirement contract', members: [target] }), 201);
  await json(await request(`/api/conversations/${conv.id}/messages`, owner, 'POST', { body: 'Original unread', idempotency_key: 'original' }), 201);
  const grant = await json(await request(`/api/conversations/${conv.id}/automation`, owner, 'PUT', { check: 'retire', target_actor: target, body: 'Follow-up {detail}', on: 'always', expires_at: Date.now() + 23 * 3600000 }));
  const definition = { name: 'retire', kind: 'routine', interval_s: 300, params: {}, action: { type: 'message', to: target, body: 'Follow-up {detail}', on: 'always', conversation_id: conv.id, grant_id: grant.grant_id } };
  await json(await request('/api/checks', owner, 'PUT', { checks: [definition] }));
  return { t, owner, recipient, cid: conv.id, grant, definition };
}
function deps(h: Hub): ActionDeps {
  return { liveness: h.livenessService, notify: async () => {}, message: async () => '', launch: async () => '', fetch: async () => new Response(null), evaluateQuota: () => [], conversation: { evaluateBacklog: q => h.conversationsService.evaluateScheduledBacklog(q), sendFollowUp: i => h.conversationsService.postScheduledFollowUp(i) } };
}
function scalar(h: Hub, q: string) { return Number(h.sql.exec(q).toArray()[0].n); }
beforeEach(async () => inside(h => {
  Reflect.deleteProperty(h, 'extraCheckDeps');
  for (const table of ['conversation_automation_receipts', 'conversation_automation_grants', 'hub_check_slots', 'hub_checks', 'hub_check_runs', 'conversation_messages', 'conversation_members', 'conversations', 'users']) h.sql.exec('DELETE FROM ' + table);
  h.sql.exec('UPDATE conversation_storage_usage SET conversation_count=0,message_count=0,message_bytes=0,removed_member_count=0');
}));

it('reclaims more than 4096 settled historical receipts and keeps current intervals bounded and unread', async () => {
  const { t, owner, recipient, cid } = await fixture();
  await inside(async h => {
    expect((await new ChecksService(h.sql, () => t).runDue(deps(h), 'retire'))[0].action).toMatch(/^conversation_message /);
    const firstReceipt = h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()[0];
    // Bounded R2 fixture: 4095 completed historical slots whose messages have
    // already expired, using a real effect's serialized request and bindings.
    h.sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<4095)
      INSERT INTO conversation_automation_receipts SELECT 'settled-'||i,json_set(?, '$.idempotencyKey', 'settled-'||i),'old-'||i,?,0 FROM n`, firstReceipt.request_json, cid);
    let retired = 0;
    for (let i = 0; i < 50; i++) {
      const result = h.busService.retentionCleanup(t, 100) as any;
      expect(Number(result.conversationRowsDeleted) + Number(result.messagesDeleted) + Number(result.claimsDeleted)).toBeLessThanOrEqual(100);
      retired += result.automationRowsDeleted;
      if (!result.hasMore) break;
    }
    expect(retired).toBe(4096);
    const watermarks = h.sql.exec('SELECT actor,last_read_seq FROM conversation_members ORDER BY actor').toArray();
    const clock = { t: t + 300000 }; const checks = new ChecksService(h.sql, () => clock.t);
    for (let i = 0; i < 32; i++) {
      expect((await checks.runDue(deps(h)))[0].action).toMatch(/^conversation_message /);
      h.busService.retentionCleanup(clock.t, 5);
      expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(0);
      expect(scalar(h, 'SELECT count(*) n FROM hub_check_slots')).toBe(1);
      expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_grants')).toBe(1);
      expect(h.sql.exec('SELECT actor,last_read_seq FROM conversation_members ORDER BY actor').toArray()).toEqual(watermarks);
      clock.t += 300000;
    }
    expect(scalar(h, 'SELECT sum(length(check_json)+length(coalesce(intent_json,\'\'))+length(coalesce(result_json,\'\'))) n FROM hub_check_slots')).toBeLessThan(5000);
  });
  const page = await json(await request(`/api/conversations/${cid}/messages`, recipient));
  expect(page.my_read_seq).toBe(0); expect(page.unread_count).toBe(34);
  expect(page.messages[0].body).toBe('Original unread');
  expect(page.messages.slice(1).every((m: any) => m.sender_actor === 'system:conversation-checks')).toBe(true);
  expect((await json(await request('/api/checks/retire/run', owner, 'POST', {}))).results[0].action).toMatch(/^conversation_message /);
}, 20000);

it('rejects exact old requests after message and receipt pruning, restart, config reuse and backward clock', async () => {
  const { t, cid, definition } = await fixture();
  await inside(async (h, state) => {
    const checks = new ChecksService(h.sql, () => t);
    await checks.runDue(deps(h), 'retire');
    const slot = h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0];
    const old: FollowUpIntent = JSON.parse(String(slot.intent_json));
    const pruner = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t + 86400000, limits: { messageRetentionMs: 0 } });
    pruner.retentionCleanup(t + 86400000, 100);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(0);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_messages')).toBe(0);
    const service = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t - 300000 }); service.init();
    expect(() => service.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
    checks.replace([]); service.retentionCleanup(t, 10);
    const v = validateCheck(definition); if ('error' in v) throw Error(v.error);
    checks.replace([v.row]);
    expect((await new ChecksService(h.sql, () => t - 300000).runDue(deps(h), 'retire'))[0].action).not.toMatch(/^conversation_message /);
    expect(() => service.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
    expect(scalar(h, 'SELECT count(*) n FROM conversation_messages')).toBe(0);
    expect(old.conversationId).toBe(cid);
  });
});

for (const phase of ['preintent', 'intent', 'accepted'] as const) it(`preserves ${phase} uncertainty across cleanup/restart and the interval boundary`, async () => {
  const { t, cid } = await fixture();
  await inside(async (h, state) => {
    const clock = { t }; const checks = new ChecksService(h.sql, () => clock.t); const port = deps(h);
    let frozen: FollowUpIntent | undefined;
    if (phase === 'preintent') {
      const check = checks.list()[0];
      h.sql.exec("INSERT INTO hub_check_slots VALUES ('retire','legacy-r2-key',?,?,NULL,NULL,NULL,'evaluating','dead',?)", t + 300000, JSON.stringify(check), t + 30000);
    } else {
      port.conversation!.sendFollowUp = i => { frozen = i; if (phase === 'accepted') h.conversationsService.postScheduledFollowUp(i); throw Error('lost response'); };
      expect((await checks.runDue(port, 'retire'))[0].status).toBe('error');
    }
    h.conversationsService.retentionCleanup(t, 100);
    expect(scalar(h, "SELECT count(*) n FROM hub_check_slots WHERE state!='done'")).toBe(1);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(phase === 'accepted' ? 1 : 0);
    clock.t += 305000;
    const service = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => clock.t }); service.init();
    const fresh = deps(h); fresh.conversation!.sendFollowUp = i => service.postScheduledFollowUp(i);
    expect((await new ChecksService(h.sql, () => clock.t).runDue(fresh, 'retire'))[0].action).toMatch(/^conversation_message /);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_messages')).toBe(2);
    if (frozen) expect(JSON.parse(String(h.sql.exec('SELECT intent_json FROM hub_check_slots').toArray()[0].intent_json))).toEqual(frozen);
    h.conversationsService.retentionCleanup(clock.t, 100);
    if (frozen) expect(() => service.postScheduledFollowUp(frozen!)).toThrow('automation_slot_retired');
    expect(scalar(h, `SELECT last_read_seq n FROM conversation_members WHERE conversation_id='${cid}' AND actor='human:retire-target'`)).toBe(0);
  });
});

for (const mutation of ['expire', 'revoke', 'remove', 'archive', 'replace-grant'] as const) {
  for (const accepted of [false, true]) it(`retires ${mutation} with accepted=${accepted}, preserving known effects and never claiming unknown delivery`, async () => {
    const { t, owner, cid } = await fixture();
    let old!: FollowUpIntent;
    await inside(async h => {
      const port = deps(h);
      port.conversation!.sendFollowUp = i => { old = i; if (accepted) h.conversationsService.postScheduledFollowUp(i); throw Error('response lost'); };
      expect((await new ChecksService(h.sql, () => t).runDue(port, 'retire'))[0].status).toBe('error');
    });
    if (mutation === 'revoke') await json(await request(`/api/conversations/${cid}/automation`, owner, 'DELETE', { check: 'retire' }));
    if (mutation === 'remove') await json(await request(`/api/conversations/${cid}/members/${target}`, owner, 'DELETE'));
    if (mutation === 'archive') await json(await request(`/api/conversations/${cid}/archive`, owner, 'POST', {}));
    if (mutation === 'replace-grant') await json(await request(`/api/conversations/${cid}/automation`, owner, 'PUT', { check: 'retire', target_actor: target, body: 'New generation', on: 'always', expires_at: Date.now() + 3600000 }));
    await inside(async (h, state) => {
      if (mutation === 'expire') h.sql.exec('UPDATE conversation_automation_grants SET expires_at=?', t - 1);
      const before = scalar(h, 'SELECT count(*) n FROM conversation_messages');
      const cleanup = h.busService.retentionCleanup(t + 1, 100) as any;
      expect(cleanup.conversationRowsDeleted).toBeLessThanOrEqual(100);
      const slot = h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0];
      expect(slot.state).toBe('done');
      expect(String(slot.action)).toContain(accepted ? 'Hub stored; native delivery unconfirmed' : 'no accepted Hub effect');
      if (!accepted) expect(JSON.parse(String(slot.result_json)).status).toBe('error');
      const fresh = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t - 600000 }); fresh.init();
      expect(() => fresh.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
      expect(scalar(h, 'SELECT count(*) n FROM conversation_messages')).toBe(before);
      expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(0);
    });
  });
}

it('reclaims orphan slot and grant caps with a shared one-row allowance, leaving live progress intact', async () => {
  const { t } = await fixture();
  await inside(async h => {
    h.sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<1000)
      INSERT INTO hub_check_slots SELECT 'orphan-'||i,'old-'||i,0,'{"action":{}}','{"status":"pass","detail":"settled"}',NULL,NULL,'done',NULL,0 FROM n`);
    h.sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<999)
      INSERT INTO conversation_automation_grants SELECT 'expired-'||i,'gone-'||i,'expired','human:retire-target',NULL,'retire-owner',0,'old','always',NULL FROM n`);
    for (let i = 0; i < 6; i++) {
      const before = scalar(h, 'SELECT (SELECT count(*) FROM hub_check_slots)+(SELECT count(*) FROM conversation_automation_grants) n');
      const result = h.busService.retentionCleanup(t, 1) as any;
      const after = scalar(h, 'SELECT (SELECT count(*) FROM hub_check_slots)+(SELECT count(*) FROM conversation_automation_grants) n');
      expect(result.automationRowsDeleted).toBe(1); expect(before - after).toBe(1);
      expect(result.conversationRowsDeleted).toBe(1);
    }
    expect(scalar(h, 'SELECT count(*) n FROM hub_check_slots')).toBeLessThan(1000);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_grants')).toBeLessThan(1000);
    expect((await new ChecksService(h.sql, () => t).runDue(deps(h), 'retire'))[0].action).toMatch(/^conversation_message /);
  });
});

it('fences a delayed send when cleanup removes its check, then permits an explicitly regranted current generation', async () => {
  const { t, owner, cid, definition } = await fixture();
  await inside(async h => {
    const checks = new ChecksService(h.sql, () => t), port = deps(h);
    let enter!: () => void, release!: () => void, old!: FollowUpIntent;
    const entered = new Promise<void>(r => { enter = r; }), gate = new Promise<void>(r => { release = r; });
    port.conversation!.sendFollowUp = async i => { old = i; enter(); await gate; return h.conversationsService.postScheduledFollowUp(i); };
    const pending = checks.runDue(port, 'retire'); await entered;
    expect(await checks.runDue(deps(h), 'retire')).toEqual([]);
    checks.replace([]);
    const cleanup = h.busService.retentionCleanup(t, 1) as any;
    expect(cleanup.conversationRowsDeleted).toBe(1);
    release(); expect(await pending).toEqual([]);
    expect(() => h.conversationsService.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
    expect(scalar(h, 'SELECT count(*) n FROM conversation_messages')).toBe(1);
  });
  const g = await json(await request(`/api/conversations/${cid}/automation`, owner, 'PUT', { check: 'retire', target_actor: target, body: definition.action.body, on: 'always', expires_at: Date.now() + 3600000 }));
  await json(await request('/api/checks', owner, 'PUT', { checks: [{ ...definition, action: { ...definition.action, grant_id: g.grant_id } }] }));
  const result = await json(await request('/api/checks/retire/run', owner, 'POST', {}));
  expect(result.results[0].action).toMatch(/^conversation_message /);
});

it('standalone R2 initialization preserves legacy frozen rows and keys, and closes expired unknown effects without a scheduler table', async () => {
  const { t } = await fixture();
  await inside(async (h, state) => {
    const checks = new ChecksService(h.sql, () => t), port = deps(h);
    port.conversation!.sendFollowUp = () => { throw Error('not yet accepted'); };
    await checks.runDue(port, 'retire');
    const original = h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0];
    h.sql.exec('DROP INDEX idx_check_slot_effect'); h.sql.exec('DROP INDEX idx_check_slot_grant');
    h.sql.exec('ALTER TABLE hub_check_slots RENAME TO legacy_slots');
    h.sql.exec(`CREATE TABLE hub_check_slots (name TEXT PRIMARY KEY, slot_key TEXT NOT NULL, next_at INTEGER NOT NULL, check_json TEXT NOT NULL, result_json TEXT, intent_json TEXT, action TEXT, state TEXT NOT NULL, token TEXT, lease_until INTEGER NOT NULL)`);
    h.sql.exec('INSERT INTO hub_check_slots SELECT * FROM legacy_slots'); h.sql.exec('DROP TABLE legacy_slots');
    h.sql.exec('DROP TABLE hub_checks');
    const service = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t });
    service.init(); expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0]).toEqual(original);
    expect(service.retentionCleanup(t, 1).rowsDeleted).toBe(0);
    h.sql.exec('UPDATE conversation_automation_grants SET expires_at=0');
    for (let i = 0; i < 4; i++) expect(service.retentionCleanup(t, 1).rowsDeleted).toBeLessThanOrEqual(1);
    const slot = h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0];
    expect(slot.state).toBe('done'); expect(JSON.parse(String(slot.result_json)).status).toBe('error');
    expect(() => service.postScheduledFollowUp(JSON.parse(String(original.intent_json)))).toThrow('automation_slot_retired');
    // Restore the public scheduler schema for this shared fixture's later tests.
    checks.init();
  });
});

it('keeps retired generations rejected when a fresh grant rebinds the name to a new CID and recipient', async () => {
  const { t, owner, cid, definition } = await fixture();
  let old!: FollowUpIntent;
  await inside(async h => {
    await new ChecksService(h.sql, () => t).runDue(deps(h), 'retire');
    old = JSON.parse(String(h.sql.exec('SELECT intent_json FROM hub_check_slots').toArray()[0].intent_json));
    h.conversationsService.retentionCleanup(t, 100);
  });
  const other = (await createSessionCookie(env as any, { sub: 'retire-other', email: 'admin@fake.local', isAdmin: true, csrfToken: csrf, exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await json(await request('/api/conversations/participants', other));
  const next = await json(await request('/api/conversations', owner, 'POST', { kind: 'group', title: 'New CID', members: ['human:retire-other'] }), 201);
  const g = await json(await request(`/api/conversations/${next.id}/automation`, owner, 'PUT', { check: 'retire', target_actor: 'human:retire-other', body: definition.action.body, on: 'always', expires_at: Date.now() + 3600000 }));
  await json(await request('/api/checks', owner, 'PUT', { checks: [{ ...definition, action: { ...definition.action, to: 'human:retire-other', conversation_id: next.id, grant_id: g.grant_id } }] }));
  await inside(async h => {
    expect((await new ChecksService(h.sql, () => t + 300000).runDue(deps(h), 'retire'))[0].action).toMatch(/^conversation_message /);
    const key = JSON.parse(String(h.sql.exec('SELECT intent_json FROM hub_check_slots').toArray()[0].intent_json)).idempotencyKey;
    expect(key).not.toBe(old.idempotencyKey);
    for (const intent of [old, { ...old, grantId: g.grant_id, conversationId: next.id, actor: 'human:retire-other' }]) expect(() => h.conversationsService.postScheduledFollowUp(intent)).toThrow('automation_slot_retired');
    expect(h.sql.exec('SELECT id FROM conversation_messages WHERE conversation_id=?', cid).toArray()).toHaveLength(2);
    expect(h.sql.exec('SELECT id FROM conversation_messages WHERE conversation_id=?', next.id).toArray()).toHaveLength(1);
  });
});

it('finishes archived parent cleanup within the shared allowance without leaving orphan automation history', async () => {
  const { t, owner, cid } = await fixture();
  let old!: FollowUpIntent;
  await inside(async h => {
    const port = deps(h);
    port.conversation!.sendFollowUp = i => { old = i; h.conversationsService.postScheduledFollowUp(i); throw Error('caller receipt lost'); };
    expect((await new ChecksService(h.sql, () => t).runDue(port, 'retire'))[0].status).toBe('error');
  });
  await json(await request(`/api/conversations/${cid}/archive`, owner, 'POST', {}));
  await inside((h, state) => {
    const service = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t + 86400000, limits: { archivedConversationRetentionMs: 0, messageRetentionMs: 0 } });
    for (let i = 0; i < 20; i++) {
      const result = service.retentionCleanup(t + 86400000, 1);
      expect(result.rowsDeleted).toBeLessThanOrEqual(1);
      if (!result.hasMore) break;
    }
    expect(h.sql.exec('SELECT id FROM conversations WHERE id=?', cid).toArray()).toHaveLength(0);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_grants')).toBe(0);
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(0);
    expect(() => service.postScheduledFollowUp(old)).toThrow('automation_slot_retired');
    h.checksService.replace([]);
    expect(service.retentionCleanup(t + 86400000, 1).rowsDeleted).toBe(1);
    expect(scalar(h, 'SELECT count(*) n FROM hub_check_slots')).toBe(0);
  });
});

it('rejects an oversized rendered effect before persisting intent and leaves original unread storage unchanged', async () => {
  const { owner, recipient, cid, definition } = await fixture();
  const body = '{detail}'.repeat(400);
  const grant = await json(await request(`/api/conversations/${cid}/automation`, owner, 'PUT', { check: 'retire', target_actor: target, body, on: 'always', expires_at: Date.now() + 3600000 }));
  await json(await request('/api/checks', owner, 'PUT', { checks: [{ ...definition, kind: 'conversation_backlog', params: { actor: target, max_age_s: 60, conversation_id: cid }, action: { ...definition.action, body, grant_id: grant.grant_id } }] }));
  const before = await json(await request(`/api/conversations/${cid}/messages`, recipient));
  const result = await json(await request('/api/checks/retire/run', owner, 'POST', {}));
  expect(result.results[0]).toMatchObject({ status: 'error', detail: 'followup_intent_too_large', action: null });
  expect(await json(await request(`/api/conversations/${cid}/messages`, recipient))).toEqual(before);
  await inside(h => {
    const slot = h.sql.exec('SELECT * FROM hub_check_slots').toArray()[0];
    expect(slot.intent_json).toBe(null); expect(slot.state).toBe('done');
    expect(scalar(h, 'SELECT count(*) n FROM conversation_automation_receipts')).toBe(0);
  });
});
