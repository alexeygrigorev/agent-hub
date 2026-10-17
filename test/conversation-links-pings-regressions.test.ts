import { expect, it } from 'vitest';
import { env as testBindings, runInDurableObject } from 'cloudflare:test';
import type { Env } from '../src/env';
import { Hub } from '../src/conversation-hub';
import { ConversationsService } from '../src/conversations';
import { ChecksService, validateCheck, type ActionDeps } from '../src/checks';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import type { FollowUpIntent } from '../src/conversation-automation';

const env = testBindings as unknown as Env;
const csrf = 'argon-adversarial-csrf';
const target = 'human:adv-target';

async function within<T>(fn: (h: Hub, state: DurableObjectState) => Promise<T>): Promise<T> {
  const stub = env.HUB.get(env.HUB.idFromName('adv-' + crypto.randomUUID()));
  return await runInDurableObject(stub, async (_h, state) => fn(new Hub(state, env), state));
}

function port(h: Hub, svc = h.conversationsService): ActionDeps {
  return {
    liveness: h.livenessService,
    notify: async () => {},
    message: async () => '',
    launch: async () => '',
    fetch: async () => new Response(null),
    evaluateQuota: () => [],
    conversation: {
      evaluateBacklog: q => svc.evaluateScheduledBacklog(q),
      sendFollowUp: i => svc.postScheduledFollowUp(i),
    },
  };
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

