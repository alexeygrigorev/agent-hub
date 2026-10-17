import { expect, it } from 'vitest';
import { env as testBindings, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import { Hub } from '../src/conversation-hub';
import { ConversationsService } from '../src/conversations';
import { ChecksService } from '../src/checks';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { RETENTION_CLEANUP_CURSOR_SQL } from '../src/bus';

const env = testBindings as unknown as Env;
const csrf = 'argon-adversarial-csrf';
const target = 'human:adv-target';

async function within<T>(fn: (h: Hub, state: DurableObjectState) => Promise<T>): Promise<T> {
  const stub = env.HUB.get(env.HUB.idFromName('adv-' + crypto.randomUUID()));
  return await runInDurableObject(stub, async (_h, state) => fn(new Hub(state, env), state));
}

it('F1 drains more automation rows than archived children with a one-row allowance', async () => {
  await within(async h => {
    const t = Date.now();
    const ownerCookie = (await createSessionCookie(env, {
      sub: 'f1-owner',
      email: 'admin@fake.local',
      isAdmin: true,
      csrfToken: csrf,
      exp: Math.floor(t / 1000) + 86400,
    })).split(';')[0];
    const targetCookie = (await createSessionCookie(env, {
      sub: 'adv-target',
      email: 'admin@fake.local',
      isAdmin: true,
      csrfToken: csrf,
      exp: Math.floor(t / 1000) + 86400,
    })).split(';')[0];
    const http = (path: string, method = 'GET', body?: unknown, cookie = ownerCookie) =>
      h.fetch(new Request('https://hub.test' + path, {
        method,
        headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      }));
    await http('/api/conversations/participants', 'GET', undefined, ownerCookie);
    await http('/api/conversations/participants', 'GET', undefined, targetCookie);

    const conv = (await (await http('/api/conversations', 'POST', {
      kind: 'group',
      title: 'F1 multi-grant conversation',
      members: [target],
    })).json()) as any;

    for (const checkName of ['check-a', 'check-b', 'check-c', 'check-d']) {
      const gRes = await http(`/api/conversations/${conv.id}/automation`, 'PUT', {
        check: checkName,
        target_actor: target,
        body: 'Ping {detail}',
        on: 'always',
        expires_at: t + 3600000,
      });
      expect(gRes.status).toBe(200);
    }

    await http(`/api/conversations/${conv.id}/archive`, 'POST', {});
    h.sql.exec("UPDATE conversations SET archived_at='2000-01-01' WHERE id=?", conv.id);

    // More authority rows than children must still converge with one-row turns.
    let deleted = 0;
    for (let i = 0; i < 16; i++) {
      const r = h.conversationsService.retentionCleanup(t, 1);
      expect(r.rowsDeleted).toBeLessThanOrEqual(1);
      deleted += r.rowsDeleted;
      if (!r.hasMore) break;
    }
    expect(deleted).toBe(7); // four grants, two members, one header
    expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
    expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([]);
    expect(h.sql.exec('SELECT id FROM conversation_automation_grants').toArray()).toEqual([]);
  });
});

it('F2 skips held receipt mismatches before LIMIT and reports only actionable work', async () => {
  await within(async h => {
    const t = Date.now();
    // Slot 'a-held' has an inconsistent receipt (held intentionally at src/conversations.ts:1423)
    h.sql.exec(
      `INSERT INTO hub_check_slots VALUES ('a-held', 'a-held-key', 0, '{"action":{"conversation_id":"c-held","grant_id":"g-missing"}}', NULL, '{"idempotencyKey":"check-a-held-key","body":"expected"}', NULL, 'retry', NULL, 0)`,
    );
    h.sql.exec(
      `INSERT INTO conversation_automation_receipts VALUES ('check-a-held-key', '{"idempotencyKey":"check-a-held-key","body":"TAMPERED"}', 'msg-held', 'c-held', 0)`,
    );
    // Slot 'b-orphan' is a normal retireable orphan slot
    h.sql.exec(
      `INSERT INTO hub_check_slots VALUES ('b-orphan', 'b-orphan-key', 0, '{"action":{}}', NULL, NULL, NULL, 'done', NULL, 0)`,
    );

    const held = h.sql.exec("SELECT * FROM hub_check_slots WHERE name='a-held'").toArray();
    const receipts = h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray();
    const r = h.conversationsService.retentionCleanup(t, 1);
    expect(r.rowsDeleted).toBe(1);
    expect(r.hasMore).toBe(false);
    expect(h.sql.exec("SELECT * FROM hub_check_slots WHERE name='b-orphan'").toArray()).toEqual([]);
    expect(h.sql.exec("SELECT * FROM hub_check_slots WHERE name='a-held'").toArray()).toEqual(held);
    expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual(receipts);
    expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
    expect(h.conversationsService.retentionCleanup(t, 1).rowsDeleted).toBe(0);
  });
});


const trackedTables = ['messages', 'claims', 'conversations', 'conversation_messages',
  'conversation_members', 'conversation_links', 'hub_check_slots',
  'conversation_automation_receipts', 'conversation_automation_grants'];
function snapshot(h: Hub) {
  return Object.fromEntries([...trackedTables, 'hub_checks', 'retention_cleanup_cursor', 'conversation_storage_usage']
    .map(table => [table, h.sql.exec(`SELECT * FROM ${table} ORDER BY rowid`).toArray()]));
}
function rowCount(h: Hub) {
  return trackedTables.reduce((n, table) => n + Number(h.sql.exec(`SELECT count(*) n FROM ${table}`).toArray()[0].n), 0);
}
function accountedCleanup(h: Hub, t: number, limit: number, outer: boolean) {
  const before = rowCount(h);
  const result = outer ? h.busService.retentionCleanup(t, limit) as any : h.conversationsService.retentionCleanup(t, limit);
  const deleted = outer ? result.messagesDeleted + result.claimsDeleted + result.conversationRowsDeleted : result.rowsDeleted;
  expect(before - rowCount(h)).toBe(deleted);
  expect(deleted).toBeLessThanOrEqual(limit);
  expect(deleted).toBeGreaterThanOrEqual(0);
  return { result, deleted };
}
async function httpFixture(h: Hub) {
  const t = Date.now();
  const cookie = (await createSessionCookie(env, { sub: 'r2-owner', email: 'admin@fake.local',
    isAdmin: true, csrfToken: csrf, exp: Math.floor(t / 1000) + 86400 })).split(';')[0];
  const http = async (path: string, method = 'GET', body?: unknown) => {
    const r = await h.fetch(new Request('https://hub.test' + path, { method,
      headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) }));
    expect(r.status).toBe(method === 'POST' && path === '/api/conversations' ? 201 : 200);
    return r.json() as Promise<any>;
  };
  await http('/api/conversations/participants');
  const parent = async (title: string, archivedAt = '2000-01-01') => {
    const c = await http('/api/conversations', 'POST', { kind: 'group', title });
    await http(`/api/conversations/${c.id}/archive`, 'POST', {});
    h.sql.exec('UPDATE conversations SET archived_at=? WHERE id=?', archivedAt, c.id);
    return c.id as string;
  };
  return { t, http, parent };
}
function seedSlot(h: Hub, name: string, check: unknown, intent: string | null, state = 'retry') {
  h.sql.exec(`INSERT INTO hub_check_slots VALUES (?, ?, 0, ?, NULL, ?, NULL, ?, 'frozen-token', 0)`,
    name, name + '-key', JSON.stringify(check), intent, state);
}
function seedReceipt(h: Hub, name: string, cid: string, request: string) {
  h.sql.exec('INSERT INTO conversation_automation_receipts VALUES (?, ?, ?, ?, 0)',
    'check-' + name + '-key', request, 'accepted-' + name, cid);
}

for (const outer of [false, true]) for (const limit of [1, 2]) {
  it(`F1 drains an automation-heavy parent with measured budget=${limit}, outer=${outer}`, async () => {
    await within(async h => {
      const { t, parent } = await httpFixture(h);
      const cid = await parent('Many grants');
      for (let i = 0; i < 12; i++) h.sql.exec(`INSERT INTO conversation_automation_grants
        VALUES (?, ?, ?, 'human:r2-owner', NULL, 'r2-owner', 0, 'old', 'always', NULL)`, 'grant-' + i, cid, 'old-' + i);
      let total = 0;
      for (let i = 0; i < 40; i++) {
        const { result, deleted } = accountedCleanup(h, t, limit, outer);
        total += deleted;
        expect(deleted).toBeGreaterThan(0);
        if (!result.hasMore) break;
      }
      expect(total).toBe(14); // 12 grants + owner membership + header
      expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
      expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([]);
    });
  });
}

for (const limit of [1, 2]) it(`F1 skips a drained held parent while mixed work advances at limit=${limit}`, async () => {
  await within(async (h, state) => {
    const { t, parent } = await httpFixture(h);
    const heldId = await parent('Older held parent');
    const laterId = await parent('Later eligible parent', '2001-01-01');
    const intent = JSON.stringify({ conversationId: heldId, body: 'frozen' });
    seedSlot(h, 'a-held-parent', { action: { conversation_id: heldId, grant_id: 'absent' } }, intent);
    seedReceipt(h, 'a-held-parent', heldId, JSON.stringify({ conversationId: heldId, body: 'different' }));
    const slots = h.sql.exec('SELECT * FROM hub_check_slots').toArray();
    const receipts = h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray();
    // One archived member and link remain deletable despite the held effect.
    h.sql.exec(`INSERT INTO conversation_links VALUES ('child-link', ?, 'routing_job', 'gone', 'human:r2-owner', '2000-01-01', NULL)`, heldId);
    for (let i = 0; i < 12; i++) {
      h.sql.exec(`INSERT INTO hub_check_slots VALUES (?, ?, 0, '{"action":{}}', NULL, NULL, NULL, 'done', NULL, 0)`, 'z-orphan-' + i, 'z-key-' + i);
      h.sql.exec(`INSERT INTO conversation_links VALUES (?, 'missing', 'routing_job', ?, 'human:r2-owner', '2000-01-01', NULL)`, 'orphan-link-' + i, 'gone-' + i);
      h.sql.exec(`INSERT INTO messages (id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
        VALUES (?,'a','b','note','th','{}',?,0,1)`, 'bus-' + i, 'bus-' + i);
    }
    h.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    h.sql.exec(`INSERT OR REPLACE INTO retention_cleanup_cursor VALUES ('conv',3),('hub',1)`);
    const observed = new Set<string>();
    for (let i = 0; i < 80; i++) {
      const before = Object.fromEntries(trackedTables.map(table => [table, h.sql.exec(`SELECT count(*) n FROM ${table}`).toArray()[0].n]));
      const { result, deleted } = accountedCleanup(h, t, limit, true);
      expect(deleted).toBeGreaterThan(0);
      for (const table of trackedTables) if (Number(before[table]) > Number(h.sql.exec(`SELECT count(*) n FROM ${table}`).toArray()[0].n)) observed.add(table);
      if (!result.hasMore) break;
    }
    expect(observed).toEqual(new Set(['conversation_members', 'conversation_links', 'conversations', 'hub_check_slots', 'messages']));
    expect(h.sql.exec('SELECT id FROM conversations ORDER BY id').toArray()).toEqual([{ id: heldId }]);
    expect(h.sql.exec('SELECT id FROM conversations WHERE id=?', laterId).toArray()).toEqual([]);
    expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()).toEqual(slots);
    expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual(receipts);
    expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
    const settled = snapshot(h);
    const restart = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t });
    restart.init(); restart.init();
    expect(restart.retentionCleanup(t, limit)).toMatchObject({ rowsDeleted: 0, hasMore: false });
    expect(snapshot(h)).toEqual(settled);
    // Only resolving the exact receipt permits reconciliation; no manual slot closure.
    h.sql.exec('UPDATE conversation_automation_receipts SET request_json=?', intent);
    for (let i = 0; i < 10 && restart.hasExpiredRows(t); i++) expect(restart.retentionCleanup(t, limit).rowsDeleted).toBeLessThanOrEqual(limit);
    expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([]);
    expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual([]);
    expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()).toEqual([]);
  });
});

for (const limit of [1, 2]) it(`F1 preserves a frozen-intent-only parent across config replacement/restart at limit=${limit}`, async () => {
  await within(async (h, state) => {
    const { t, parent } = await httpFixture(h);
    const heldId = await parent('Frozen intent parent');
    await parent('Eligible later parent', '2001-01-01');
    const checks = new ChecksService(h.sql, () => t);
    const definition = { enabled: true, name: 'intent-only', kind: 'routine', interval_s: 300, params: {}, action: { type: 'alert', on: 'fail' } };
    checks.replace([definition]);
    seedSlot(h, 'intent-only', { action: {} }, JSON.stringify({ conversationId: heldId, body: 'original frozen intent' }));
    const frozen = h.sql.exec('SELECT * FROM hub_check_slots').toArray();
    for (let i = 0; i < 10; i++) {
      const { result } = accountedCleanup(h, t, limit, true);
      if (!result.hasMore) break;
    }
    expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([{ id: heldId }]);
    expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
    checks.replace([{ ...definition, interval_s: 600 }]);
    const restart = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t });
    restart.init();
    expect(restart.retentionCleanup(t, limit)).toMatchObject({ rowsDeleted: 0, hasMore: false });
    expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()).toEqual(frozen);
    // Explicit check removal allows normal retirement to fence the unknown effect.
    checks.replace([]);
    for (let i = 0; i < 10 && restart.hasExpiredRows(t); i++) expect(restart.retentionCleanup(t, limit).rowsDeleted).toBeLessThanOrEqual(limit);
    expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([]);
    expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()).toEqual([]);
  });
});

for (const slotState of ['retry', 'done']) for (const frozen of [null, '{malformed', '{"body":"original"}']) {
  it(`F2 preserves ${slotState} mismatched receipt/intent bytes (${frozen}) before orphan LIMIT`, async () => {
    await within(async (h, state) => {
      const t = Date.now();
      seedSlot(h, 'a-held', { action: {} }, frozen, slotState);
      seedReceipt(h, 'a-held', 'missing-parent', '{malformed receipt');
      seedSlot(h, 'z-valid', { action: {} }, null, 'done');
      const held = h.sql.exec("SELECT * FROM hub_check_slots WHERE name='a-held'").toArray();
      const receipts = h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray();
      expect(accountedCleanup(h, t, 1, true).deleted).toBe(1);
      expect(h.conversationsService.hasExpiredRows(t)).toBe(false);
      const service = new ConversationsService(h.sql, f => state.storage.transactionSync(f), { now: () => t });
      service.init(); service.init();
      expect(service.retentionCleanup(t, 2)).toMatchObject({ rowsDeleted: 0, hasMore: false });
      expect(h.sql.exec('SELECT * FROM hub_check_slots').toArray()).toEqual(held);
      expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual(receipts);
    });
  });
}

it('rolls back new eligibility paths, mixed counters and both cursors after a late header abort', async () => {
  await within(async h => {
    const { t, parent } = await httpFixture(h);
    const heldId = await parent('Held older');
    const victimId = await parent('Eligible later', '2001-01-01');
    seedSlot(h, 'a-held', { action: { conversation_id: heldId } }, '{"body":"frozen"}');
    seedReceipt(h, 'a-held', heldId, '{"body":"mismatch"}');
    seedSlot(h, 'z-eligible', { action: {} }, null, 'done');
    h.sql.exec(`INSERT INTO conversation_links VALUES ('orphan', 'absent', 'routing_job', 'gone', 'human:r2-owner', '2000-01-01', NULL)`);
    h.sql.exec(`INSERT INTO messages (id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at)
      VALUES ('bus','a','b','note','th','{}','bus',0,1)`);
    h.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    h.sql.exec(`INSERT OR REPLACE INTO retention_cleanup_cursor VALUES ('conv',0),('hub',0)`);
    h.sql.exec(`CREATE TRIGGER abort_r2_header BEFORE DELETE ON conversations WHEN OLD.id='${victimId}' BEGIN
      SELECT CASE WHEN EXISTS (SELECT 1 FROM hub_check_slots WHERE name='z-eligible')
        OR EXISTS (SELECT 1 FROM conversation_members WHERE conversation_id=OLD.id)
        OR EXISTS (SELECT 1 FROM messages WHERE id='bus') THEN RAISE(ABORT,'not late')
        ELSE RAISE(ABORT,'late r2 abort') END; END`);
    const before = snapshot(h);
    expect(() => h.busService.retentionCleanup(t, 100)).toThrow('late r2 abort');
    expect(snapshot(h)).toEqual(before);
    h.sql.exec('DROP TRIGGER abort_r2_header');
    const { result, deleted } = accountedCleanup(h, t, 100, true);
    expect(deleted).toBe(6); // bus + eligible slot + orphan link + two members + header
    expect(result.hasMore).toBe(false);
    expect(h.sql.exec('SELECT id FROM conversations').toArray()).toEqual([{ id: heldId }]);
    expect(h.sql.exec('SELECT name FROM hub_check_slots').toArray()).toEqual([{ name: 'a-held' }]);
    expect(h.sql.exec('SELECT * FROM conversation_automation_receipts').toArray()).toEqual(before.conversation_automation_receipts);
  });
});
