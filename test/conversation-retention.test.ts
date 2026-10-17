import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import { ConversationsService } from '../src/conversations';
import { BusService, RETENTION_CLEANUP_CURSOR_SQL } from '../src/bus';
import type { Env } from '../src/env';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';

const testEnv = env as unknown as Env;

function count(sql: any, query: string, ...params: any[]): number {
  return Number((sql.exec(query, ...params).toArray()[0] as any)?.n ?? 0);
}

function tableCounts(sql: any) {
  return {
    busMessages: count(sql, 'SELECT COUNT(*) AS n FROM messages'),
    busClaims: count(sql, 'SELECT COUNT(*) AS n FROM claims'),
    convMessages: count(sql, 'SELECT COUNT(*) AS n FROM conversation_messages'),
    convMembers: count(sql, 'SELECT COUNT(*) AS n FROM conversation_members'),
    conversations: count(sql, 'SELECT COUNT(*) AS n FROM conversations'),
  };
}

function totalRows(sql: any): number {
  const c = tableCounts(sql);
  return (
    c.busMessages +
    c.busClaims +
    c.convMessages +
    c.convMembers +
    c.conversations
  );
}

function insertExpiredBusMessage(sql: any, id: string, now: number): void {
  sql.exec(
    "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'a', 'b', 'note', 'th-ret', '{}', ?, ?, ?)",
    id,
    `idem-${id}`,
    now - 10_000,
    now - 1_000,
  );
}

function insertExpiredClaim(sql: any, id: string, now: number): void {
  sql.exec(
    'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
    id,
    'proj',
    `src/${id}.ts`,
    'holder-1',
    now - 10_000,
    now - 1,
  );
}

async function freshHub(
  fn: (hub: Hub, state: DurableObjectState) => Promise<void> | void,
): Promise<void> {
  const stub = testEnv.HUB.get(
    testEnv.HUB.idFromName('ret-' + crypto.randomUUID()),
  );
  await runInDurableObject(stub, async (_base: BaseHub, state: DurableObjectState) => {
    const hub = new Hub(state, testEnv);
    hub.sql.exec(
      "INSERT INTO agents (tag, project, role) VALUES ('bot', 'proj', 'worker')",
    );
    await fn(hub, state);
  });
}

describe('F14 shared bus/claim/conversation cleanup budget', () => {
  it('mixed 50 messages + 50 claims + expired conversation data honors limit 1/2/3/100', async () => {
    await freshHub(async hub => {
      let clockMs = 1_792_189_200_000;
      (hub.conversationsService as any).now = () => clockMs;
      hub.conversationsService.limits.messageRetentionMs = 1000;

      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(clockMs / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Mixed budget',
        members: ['proj:bot'],
      });
      hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'Expired conversation row',
        idempotencyKey: 'mix-exp-1',
      });

      for (let i = 0; i < 50; i++) {
        insertExpiredBusMessage(hub.sql, `bus-m-${i}`, clockMs);
        insertExpiredClaim(hub.sql, `bus-c-${i}`, clockMs);
      }

      clockMs += 2000;
      expect(Hub.prototype.scheduledTick).toBe(BaseHub.prototype.scheduledTick);
      expect(Hub.prototype.alarm).toBe(BaseHub.prototype.alarm);

      const before = tableCounts(hub.sql);
      expect(before.busMessages).toBe(50);
      expect(before.busClaims).toBe(50);
      expect(before.convMessages).toBe(1);

      const run = (limit: number) => {
        const start = totalRows(hub.sql);
        const res = hub.busService.retentionCleanup(clockMs, limit) as any;
        const deleted = start - totalRows(hub.sql);
        const reported =
          Number(res.messagesDeleted ?? 0) +
          Number(res.claimsDeleted ?? 0) +
          Number(res.conversationRowsDeleted ?? 0);
        expect(deleted).toBe(reported);
        expect(deleted).toBeLessThanOrEqual(limit);
        expect(deleted).toBeGreaterThan(0);
        return { res, deleted };
      };

      const one = run(1);
      expect(one.deleted).toBe(1);
      expect(one.res.hasMore).toBe(true);

      const two = run(2);
      expect(two.deleted).toBeLessThanOrEqual(2);
      expect(two.res.hasMore).toBe(true);

      const three = run(3);
      expect(three.deleted).toBeLessThanOrEqual(3);
      expect(three.res.hasMore).toBe(true);

      const hundred = run(100);
      expect(hundred.deleted).toBeLessThanOrEqual(100);
      const remaining =
        tableCounts(hub.sql).busMessages +
        tableCounts(hub.sql).busClaims +
        tableCounts(hub.sql).convMessages;
      if (remaining > 0) {
        expect(hundred.res.hasMore).toBe(true);
      }

      let guard = 0;
      while (
        hub.busService.hasExpiredRows(clockMs) ||
        hub.conversationsService.hasExpiredRows(clockMs)
      ) {
        const step = run(1);
        expect(step.deleted).toBe(1);
        guard++;
        expect(guard).toBeLessThan(120);
      }
      expect(tableCounts(hub.sql).busMessages).toBe(0);
      expect(tableCounts(hub.sql).busClaims).toBe(0);
      expect(tableCounts(hub.sql).convMessages).toBe(0);
    });
  });

  it('bus-only and conversation-only backlogs stay inside the shared budget', async () => {
    await freshHub(async hub => {
      const now = Date.now();
      for (let i = 0; i < 8; i++) {
        insertExpiredBusMessage(hub.sql, `only-m-${i}`, now);
        insertExpiredClaim(hub.sql, `only-c-${i}`, now);
      }
      const busOnly = hub.busService.retentionCleanup(now, 5) as any;
      expect(
        Number(busOnly.messagesDeleted) + Number(busOnly.claimsDeleted),
      ).toBe(5);
      expect(Number(busOnly.conversationRowsDeleted ?? 0)).toBe(0);
      expect(
        tableCounts(hub.sql).busMessages + tableCounts(hub.sql).busClaims,
      ).toBe(11);
      expect(busOnly.hasMore).toBe(true);
      while (hub.busService.hasExpiredRows(now)) {
        hub.busService.retentionCleanup(now, 100);
      }

      let clockMs = now;
      (hub.conversationsService as any).now = () => clockMs;
      hub.conversationsService.limits.messageRetentionMs = 1000;
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(clockMs / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Conv only',
      });
      for (let i = 0; i < 6; i++) {
        hub.conversationsService.sendMessage(human, {
          conversationId: conv.id,
          body: `c${i}`,
          idempotencyKey: `c-${i}`,
        });
      }
      clockMs += 2000;
      const convOnly = hub.busService.retentionCleanup(clockMs, 4) as any;
      expect(Number(convOnly.messagesDeleted ?? 0)).toBe(0);
      expect(Number(convOnly.claimsDeleted ?? 0)).toBe(0);
      expect(Number(convOnly.conversationRowsDeleted)).toBe(4);
      expect(convOnly.hasMore).toBe(true);
      expect(tableCounts(hub.sql).convMessages).toBe(2);
    });
  });

  it('odd small limits and many archived/removed member rows stay resumable', async () => {
    await freshHub(async hub => {
      let clockMs = 1_792_189_300_000;
      (hub.conversationsService as any).now = () => clockMs;
      hub.conversationsService.limits.archivedConversationRetentionMs = 1000;
      hub.conversationsService.limits.removedMemberRetentionMs = 1000;
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(clockMs / 1000) + 86400,
        },
        csrfVerified: true,
      };
      for (let i = 0; i < 6; i++) {
        hub.sql.exec(
          'INSERT INTO agents (tag, project, role) VALUES (?, ?, ?)',
          `m${i}`,
          'proj',
          'worker',
        );
      }
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Archive members',
        members: ['proj:m0', 'proj:m1', 'proj:m2', 'proj:m3', 'proj:m4', 'proj:m5'],
      });
      for (let i = 0; i < 5; i++) {
        hub.conversationsService.sendMessage(human, {
          conversationId: conv.id,
          body: `arch ${i}`,
          idempotencyKey: `arch-${i}`,
        });
      }
      for (let i = 0; i < 6; i++) {
        hub.conversationsService.removeMember(human, conv.id, `proj:m${i}`);
      }
      hub.conversationsService.setArchived(human, conv.id, true);
      clockMs += 2000;

      const step = hub.busService.retentionCleanup(clockMs, 3) as any;
      expect(Number(step.conversationRowsDeleted)).toBeLessThanOrEqual(3);
      expect(step.hasMore).toBe(true);
      expect(
        count(
          hub.sql,
          'SELECT COUNT(*) AS n FROM conversation_messages WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_messages.conversation_id)',
        ),
      ).toBe(0);

      let batches = 0;
      while (hub.conversationsService.hasExpiredRows(clockMs)) {
        const res = hub.busService.retentionCleanup(clockMs, 3) as any;
        expect(
          Number(res.messagesDeleted ?? 0) +
            Number(res.claimsDeleted ?? 0) +
            Number(res.conversationRowsDeleted ?? 0),
        ).toBeLessThanOrEqual(3);
        batches++;
        expect(batches).toBeLessThan(40);
      }
      expect(tableCounts(hub.sql).conversations).toBe(0);
      expect(tableCounts(hub.sql).convMessages).toBe(0);
      expect(tableCounts(hub.sql).convMembers).toBe(0);
    });
  });
});

describe('F14 durable fairness under replenishment', () => {
  const T = Date.parse('2026-10-17T01:05:00Z');

  function humanAt(clockMs: number) {
    return {
      session: {
        sub: 'alice',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'csrf-ret-token',
        exp: Math.floor(clockMs / 1000) + 86400,
      },
      csrfVerified: true,
    };
  }

  function pinRetention(hub: Hub, clockMs: number): void {
    (hub.conversationsService as any).now = () => clockMs;
    hub.conversationsService.limits.messageRetentionMs = 1;
    hub.conversationsService.limits.archivedConversationRetentionMs = 1;
    hub.conversationsService.limits.removedMemberRetentionMs = 1;
  }

  function cursorRows(sql: any) {
    const present = sql
      .exec(
        "SELECT 1 AS n FROM sqlite_master WHERE type = 'table' AND name = 'retention_cleanup_cursor'",
      )
      .toArray();
    if (present.length === 0) return [];
    return sql
      .exec(
        'SELECT kind, next_index FROM retention_cleanup_cursor ORDER BY kind',
      )
      .toArray();
  }

  function fiveTableCounts(sql: any): number[] {
    const c = tableCounts(sql);
    return [
      c.busMessages,
      c.busClaims,
      c.conversations,
      c.convMessages,
      c.convMembers,
    ];
  }

  function retentionState(hub: Hub) {
    const sql = hub.sql;
    let leases: unknown[] = [];
    try {
      leases = sql
        .exec(
          'SELECT id, holder, expires_at FROM leases ORDER BY id',
        )
        .toArray();
    } catch {
      leases = [];
    }
    return {
      tables: tableCounts(sql),
      five: fiveTableCounts(sql),
      cursors: cursorRows(sql),
      usage: hub.conversationsService.getStorageUsage(),
      members: sql
        .exec(
          'SELECT conversation_id, actor, role, removed_at, last_read_seq, last_read_at FROM conversation_members ORDER BY conversation_id, actor',
        )
        .toArray(),
      claims: sql
        .exec(
          'SELECT id, project, scope, holder, expires_at FROM claims ORDER BY id',
        )
        .toArray(),
      messages: sql.exec('SELECT id FROM messages ORDER BY id').toArray(),
      convMessages: sql
        .exec(
          'SELECT id, conversation_id, seq, body FROM conversation_messages ORDER BY id',
        )
        .toArray(),
      conversations: sql
        .exec(
          'SELECT id, last_seq, retained_message_count, last_message_preview FROM conversations ORDER BY id',
        )
        .toArray(),
      leases,
    };
  }

  function pinHubCursor(sql: any, nextIndex: number): void {
    sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    sql.exec(
      "INSERT OR REPLACE INTO retention_cleanup_cursor (kind, next_index) VALUES ('hub', ?)",
      nextIndex,
    );
  }

  it.each([1, 2, 3, 5])(
    'composed replenished bus messages do not starve claims or conversation rows at limit %i',
    async limit => {
      await freshHub(async (initial, state) => {
        let hub = initial;
        pinRetention(hub, T);
        const conv = hub.conversationsService.createConversation(humanAt(T), {
          kind: 'group',
          title: 'Fair composed',
          members: ['proj:bot'],
        });
        hub.conversationsService.sendMessage(humanAt(T), {
          conversationId: conv.id,
          body: 'old',
          idempotencyKey: 'old',
        });
        insertExpiredBusMessage(hub.sql, 'seed-msg', T);
        insertExpiredClaim(hub.sql, 'seed-claim', T);

        let claimsDeleted = 0;
        let conversationRowsDeleted = 0;
        for (let i = 0; i < 12; i++) {
          hub = new Hub(state, testEnv);
          pinRetention(hub, T);
          for (let j = 0; j < limit; j++) {
            insertExpiredBusMessage(hub.sql, `replenish-msg-${i}-${j}`, T);
          }
          hub.conversationsService.sendMessage(humanAt(T), {
            conversationId: conv.id,
            body: `replenish${i}`,
            idempotencyKey: `replenish${i}`,
          });
          const before = totalRows(hub.sql);
          const res = hub.busService.retentionCleanup(T + 2, limit) as any;
          const deleted = before - totalRows(hub.sql);
          expect(deleted).toBeLessThanOrEqual(limit);
          expect(deleted).toBe(
            Number(res.messagesDeleted ?? 0) +
              Number(res.claimsDeleted ?? 0) +
              Number(res.conversationRowsDeleted ?? 0),
          );
          expect(res.hasMore).toBe(true);
          claimsDeleted += Number(res.claimsDeleted ?? 0);
          conversationRowsDeleted += Number(res.conversationRowsDeleted ?? 0);
        }
        expect(claimsDeleted).toBeGreaterThan(0);
        expect(conversationRowsDeleted).toBeGreaterThan(0);
      });
    },
  );

  it('standalone BusService limit 1 deletes an expired claim under message replenishment across reconstructed instances', async () => {
    await freshHub(async (hub, state) => {
      insertExpiredBusMessage(hub.sql, 'base-msg', T);
      insertExpiredClaim(hub.sql, 'base-claim', T);
      let claimsDeleted = 0;
      for (let i = 0; i < 12; i++) {
        insertExpiredBusMessage(hub.sql, `base-replenish-${i}`, T);
        const bus = new BusService(hub.sql, fn =>
          state.storage.transactionSync(fn),
        );
        const before = tableCounts(hub.sql);
        const res = bus.retentionCleanup(T + 2, 1);
        expect(
          Number(res.messagesDeleted) + Number(res.claimsDeleted),
        ).toBeLessThanOrEqual(1);
        expect(
          before.busMessages +
            before.busClaims -
            tableCounts(hub.sql).busMessages -
            tableCounts(hub.sql).busClaims,
        ).toBe(Number(res.messagesDeleted) + Number(res.claimsDeleted));
        expect(res.hasMore).toBe(true);
        claimsDeleted += res.claimsDeleted;
      }
      expect(claimsDeleted).toBeGreaterThan(0);
    });
  });

  it.each([1, 2])(
    'archived replenishment does not starve tombstones or ordinary messages at limit %i',
    async limit => {
      await freshHub(async (initial, state) => {
        let hub = initial;
        pinRetention(hub, T);
        const target = hub.conversationsService.createConversation(humanAt(T), {
          kind: 'group',
          title: 'Target live',
          members: ['proj:bot'],
        });
        hub.conversationsService.sendMessage(humanAt(T), {
          conversationId: target.id,
          body: 'target',
          idempotencyKey: 'target',
        });
        hub.conversationsService.removeMember(
          humanAt(T),
          target.id,
          'proj:bot',
        );
        hub.sql.exec(
          'UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?',
          new Date(T - 100).toISOString(),
          target.id,
        );

        let removed = 0;
        for (let i = 0; i < 12; i++) {
          hub = new Hub(state, testEnv);
          pinRetention(hub, T);
          const archived = hub.conversationsService.createConversation(
            humanAt(T),
            { kind: 'group', title: `Arch ${i}` },
          );
          hub.conversationsService.sendMessage(humanAt(T), {
            conversationId: archived.id,
            body: 'archive',
            idempotencyKey: `archive-${i}`,
          });
          hub.conversationsService.setArchived(humanAt(T), archived.id, true);
          const before = totalRows(hub.sql);
          const res = hub.busService.retentionCleanup(T + 2, limit) as any;
          expect(before - totalRows(hub.sql)).toBeLessThanOrEqual(limit);
          expect(res.hasMore).toBe(true);
          removed += Number(res.removedMembersDeleted ?? 0);
        }
        expect(removed).toBeGreaterThan(0);
        expect(
          count(
            hub.sql,
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conversation_id = ?',
            target.id,
          ),
        ).toBe(0);
      });
    },
  );

  it('an empty category that becomes eligible is served within two reconstructed calls', async () => {
    await freshHub(async (hub, state) => {
      insertExpiredBusMessage(hub.sql, 'only-msg-a', T);
      insertExpiredBusMessage(hub.sql, 'only-msg-b', T);
      const first = new BusService(hub.sql, fn =>
        state.storage.transactionSync(fn),
      );
      expect(first.retentionCleanup(T + 2, 1).messagesDeleted).toBe(1);
      insertExpiredClaim(hub.sql, 'late-claim', T);
      let claimsDeleted = 0;
      for (let i = 0; i < 2; i++) {
        const bus = new BusService(hub.sql, fn =>
          state.storage.transactionSync(fn),
        );
        claimsDeleted += bus.retentionCleanup(T + 2, 1).claimsDeleted;
      }
      expect(claimsDeleted).toBeGreaterThan(0);
    });
  });

  it('odd and large limits honor the exact deletion budget and truthful hasMore', async () => {
    await freshHub(async hub => {
      pinRetention(hub, T);
      const conv = hub.conversationsService.createConversation(humanAt(T), {
        kind: 'group',
        title: 'Budget',
        members: ['proj:bot'],
      });
      for (let i = 0; i < 8; i++) {
        hub.conversationsService.sendMessage(humanAt(T), {
          conversationId: conv.id,
          body: `m${i}`,
          idempotencyKey: `m${i}`,
        });
        insertExpiredBusMessage(hub.sql, `odd-m-${i}`, T);
        insertExpiredClaim(hub.sql, `odd-c-${i}`, T);
      }
      for (const limit of [1, 2, 3, 5, 100]) {
        if (
          !hub.busService.hasExpiredRows(T + 2) &&
          !hub.conversationsService.hasExpiredRows(T + 2)
        ) {
          break;
        }
        const before = totalRows(hub.sql);
        const res = hub.busService.retentionCleanup(T + 2, limit) as any;
        const deleted = before - totalRows(hub.sql);
        const reported =
          Number(res.messagesDeleted ?? 0) +
          Number(res.claimsDeleted ?? 0) +
          Number(res.conversationRowsDeleted ?? 0);
        expect(deleted).toBe(reported);
        expect(deleted).toBeLessThanOrEqual(limit);
        expect(deleted).toBeGreaterThan(0);
        const still =
          hub.busService.hasExpiredRows(T + 2) ||
          hub.conversationsService.hasExpiredRows(T + 2);
        expect(res.hasMore).toBe(still);
      }
      const zero = new BusService(hub.sql).retentionCleanup(T + 2, 0);
      expect(zero.messagesDeleted).toBe(0);
      expect(zero.claimsDeleted).toBe(0);
      expect(zero.hasMore).toBe(
        hub.busService.hasExpiredRows(T + 2) ||
          hub.conversationsService.hasExpiredRows(T + 2),
      );
    });
  });

  it('injected cleanup rollback leaves rows, counters, and fairness cursor unchanged', async () => {
    await freshHub(async (hub, state) => {
      pinRetention(hub, T);
      const conv = hub.conversationsService.createConversation(humanAt(T), {
        kind: 'group',
        title: 'Rollback',
      });
      hub.conversationsService.sendMessage(humanAt(T), {
        conversationId: conv.id,
        body: 'rollback',
        idempotencyKey: 'rollback',
      });
      insertExpiredBusMessage(hub.sql, 'rb-msg', T);
      insertExpiredClaim(hub.sql, 'rb-claim', T);
      const usage = hub.conversationsService.getStorageUsage();
      const before = tableCounts(hub.sql);
      const cursors = cursorRows(hub.sql);
      const members = hub.sql
        .exec('SELECT * FROM conversation_members ORDER BY actor')
        .toArray();

      const busProxy = {
        exec(q: string, ...args: any[]) {
          if (typeof q === 'string' && q.includes('DELETE FROM claims')) {
            throw new Error('injected bus cleanup rollback');
          }
          return hub.sql.exec(q, ...args);
        },
      };
      const bus = new BusService(busProxy as any, fn =>
        state.storage.transactionSync(fn),
      );
      expect(() => bus.retentionCleanup(T + 2, 100)).toThrow(
        'injected bus cleanup rollback',
      );
      expect(tableCounts(hub.sql)).toEqual(before);
      expect(cursorRows(hub.sql)).toEqual(cursors);

      const convProxy = {
        exec(q: string, ...args: any[]) {
          if (
            typeof q === 'string' &&
            q.includes('UPDATE conversation_storage_usage') &&
            q.includes('message_count')
          ) {
            throw new Error('injected conversation cleanup rollback');
          }
          return hub.sql.exec(q, ...args);
        },
      };
      const convSvc = new ConversationsService(
        convProxy as any,
        fn => state.storage.transactionSync(fn),
        { now: () => T, limits: { messageRetentionMs: 1 } },
      );
      expect(() => convSvc.retentionCleanup(T + 2, 100)).toThrow(
        'injected conversation cleanup rollback',
      );
      expect(tableCounts(hub.sql)).toEqual(before);
      expect(hub.conversationsService.getStorageUsage()).toEqual(usage);
      expect(cursorRows(hub.sql)).toEqual(cursors);
      expect(
        hub.sql.exec('SELECT * FROM conversation_members ORDER BY actor').toArray(),
      ).toEqual(members);
    });
  });

  it('hub cursor INSERT abort rolls back composed deletions, child cursors, usage, read, and lease', async () => {
    await freshHub(async (initial, state) => {
      let hub = initial;
      pinRetention(hub, T);
      insertExpiredBusMessage(hub.sql, 'hub-abort-msg', T);
      insertExpiredClaim(hub.sql, 'hub-abort-claim', T);
      hub.sql.exec(
        'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        'hub-abort-live',
        'proj',
        'src/hub-abort-live.ts',
        'holder-1',
        T,
        T + 120000,
      );
      pinHubCursor(hub.sql, 0);
      const before = retentionState(hub);
      expect(before.five).toEqual([1, 2, 0, 0, 0]);

      hub.sql.exec(`
        CREATE TRIGGER abort_hub_cursor BEFORE INSERT ON retention_cleanup_cursor
        WHEN NEW.kind = 'hub'
        BEGIN SELECT RAISE(ABORT, 'injected cursor write failure'); END;
      `);
      expect(() => hub.busService.retentionCleanup(T + 2, 1)).toThrow(
        /injected cursor write failure/,
      );
      expect(retentionState(hub)).toEqual(before);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'hub-abort-live'").toArray(),
      ).toHaveLength(1);

      hub.sql.exec('DROP TRIGGER IF EXISTS abort_hub_cursor');
      hub = new Hub(state, testEnv);
      pinRetention(hub, T);
      const retry = hub.busService.retentionCleanup(T + 2, 1) as any;
      expect(
        Number(retry.messagesDeleted ?? 0) +
          Number(retry.claimsDeleted ?? 0) +
          Number(retry.conversationRowsDeleted ?? 0),
      ).toBe(1);
      expect(retry.hasMore).toBe(true);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'hub-abort-live'").toArray(),
      ).toHaveLength(1);

      let claimsDeleted = Number(retry.claimsDeleted ?? 0);
      for (let i = 0; i < 4; i++) {
        hub = new Hub(state, testEnv);
        pinRetention(hub, T);
        const step = hub.busService.retentionCleanup(T + 2, 1) as any;
        claimsDeleted += Number(step.claimsDeleted ?? 0);
      }
      expect(claimsDeleted).toBeGreaterThan(0);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'hub-abort-live'").toArray(),
      ).toHaveLength(1);
    });
  });

  it('conversation_messages DELETE abort rolls back earlier bus work on direct cleanup and POST /api/cron/retention', async () => {
    await freshHub(async (initial, state) => {
      let hub = initial;
      pinRetention(hub, T);
      const conv = hub.conversationsService.createConversation(humanAt(T), {
        kind: 'group',
        title: 'Atomic composed',
        members: ['proj:bot'],
      });
      hub.conversationsService.sendMessage(humanAt(T), {
        conversationId: conv.id,
        body: 'expire-me',
        idempotencyKey: 'expire-me',
      });
      hub.sql.exec(
        'UPDATE conversation_members SET last_read_seq = 1, last_read_at = ? WHERE conversation_id = ?',
        new Date(T).toISOString(),
        conv.id,
      );
      insertExpiredBusMessage(hub.sql, 'del-abort-msg', T);
      insertExpiredClaim(hub.sql, 'del-abort-claim', T);
      hub.sql.exec(
        'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        'del-abort-live',
        'proj',
        'src/del-abort-live.ts',
        'holder-1',
        T,
        T + 120000,
      );
      pinHubCursor(hub.sql, 0);
      const before = retentionState(hub);
      expect(before.five).toEqual([1, 2, 1, 1, 2]);

      hub.sql.exec(`
        CREATE TRIGGER abort_conv_msg_delete BEFORE DELETE ON conversation_messages
        BEGIN SELECT RAISE(ABORT, 'injected conversation message delete failure'); END;
      `);
      expect(() => hub.busService.retentionCleanup(T + 2, 100)).toThrow(
        /injected conversation message delete failure/,
      );
      expect(retentionState(hub)).toEqual(before);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'del-abort-live'").toArray(),
      ).toHaveLength(1);

      const cookie = await createSessionCookie(testEnv, {
        sub: 'alice',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'csrf-ret-token',
        exp: Math.floor(Date.now() / 1000) + 3600,
      });
      let httpStatus: number | null = null;
      let httpError: string | null = null;
      try {
        const res = await hub.fetch(
          new Request('http://example.com/api/cron/retention', {
            method: 'POST',
            headers: {
              Cookie: cookie,
              'X-CSRF-Token': 'csrf-ret-token',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ now: '2099-01-01T00:00:00Z' }),
          }),
        );
        httpStatus = res.status;
      } catch (err: any) {
        httpError = String(err?.message ?? err);
      }
      expect(
        httpStatus === 500 ||
          /injected conversation message delete failure/i.test(httpError ?? ''),
      ).toBe(true);
      expect(retentionState(hub)).toEqual(before);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'del-abort-live'").toArray(),
      ).toHaveLength(1);

      hub.sql.exec('DROP TRIGGER IF EXISTS abort_conv_msg_delete');
      hub = new Hub(state, testEnv);
      pinRetention(hub, T);
      const retry = hub.busService.retentionCleanup(T + 2, 100) as any;
      const deleted =
        Number(retry.messagesDeleted ?? 0) +
        Number(retry.claimsDeleted ?? 0) +
        Number(retry.conversationRowsDeleted ?? 0);
      expect(deleted).toBeGreaterThan(0);
      expect(deleted).toBeLessThanOrEqual(100);
      expect(fiveTableCounts(hub.sql)[0]).toBe(0);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'del-abort-claim'").toArray(),
      ).toHaveLength(0);
      expect(
        hub.sql.exec("SELECT id FROM claims WHERE id = 'del-abort-live'").toArray(),
      ).toHaveLength(1);
      expect(
        hub.sql
          .exec('SELECT id FROM conversation_messages WHERE conversation_id = ?', conv.id)
          .toArray(),
      ).toHaveLength(0);
      expect(retry.hasMore).toBe(
        hub.busService.hasExpiredRows(T + 2) ||
          hub.conversationsService.hasExpiredRows(T + 2),
      );
    });
  });
});

describe('F11 retained unread counts and truthful gap semantics', () => {
  it('same-millisecond ID ordering can prune an interior seq and unread counts retained rows', async () => {
    await freshHub(async hub => {
      const t = 1_792_189_400_000;
      (hub.conversationsService as any).now = () => t;
      hub.conversationsService.limits.messageRetentionMs = 1000;
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(t / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const agent = {
        caller: { actor: 'proj:bot', kind: 'agent' as const },
        scopes: ['bus:read', 'bus:send'],
        authType: 'bearer' as const,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Interior prune',
        members: ['proj:bot'],
      });
      hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'one',
        idempotencyKey: 'g1',
      });
      hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'two',
        idempotencyKey: 'g2',
      });
      hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'three',
        idempotencyKey: 'g3',
      });

      const rows = hub.sql
        .exec(
          'SELECT id, seq FROM conversation_messages WHERE conversation_id = ? ORDER BY seq ASC',
          conv.id,
        )
        .toArray() as { id: string; seq: number }[];
      expect(rows.map(r => r.seq)).toEqual([1, 2, 3]);
      const iso = new Date(t).toISOString();
      hub.sql.exec(
        'UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?',
        iso,
        conv.id,
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'tmp-seq-1',
        rows[0].id,
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'tmp-seq-2',
        rows[1].id,
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'tmp-seq-3',
        rows[2].id,
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'aaa-seq-2',
        'tmp-seq-2',
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'bbb-seq-3',
        'tmp-seq-3',
      );
      hub.sql.exec(
        'UPDATE conversation_messages SET id = ? WHERE id = ?',
        'ccc-seq-1',
        'tmp-seq-1',
      );

      const later = t + 2000;
      (hub.conversationsService as any).now = () => later;
      const cleanup = hub.conversationsService.retentionCleanup(later, 1);
      expect(cleanup.messagesDeleted).toBe(1);
      const retained = hub.sql
        .exec(
          'SELECT seq FROM conversation_messages WHERE conversation_id = ? ORDER BY seq ASC',
          conv.id,
        )
        .toArray()
        .map((r: any) => Number(r.seq));
      expect(retained).toEqual([1, 3]);

      const token = 'synthetic-local-test-bearer';
      const hash = await sha256(token);
      hub.sql.exec(
        'INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        'ret-bot-cred',
        'agent',
        hash,
        'ret-bot-fam',
        'bot',
        'proj:bot',
        JSON.stringify(['bus:read', 'bus:send']),
        'alice',
        new Date(t).toISOString(),
        new Date(t + 3600_000).toISOString(),
      );
      const detail = (await (
        await hub.fetch(
          new Request(`https://hub.test/api/conversations/${conv.id}`, {
            headers: { Authorization: `Bearer ${token}` },
          }),
        )
      ).json()) as any;
      const list = (await (
        await hub.fetch(
          new Request('https://hub.test/api/conversations', {
            headers: { Authorization: `Bearer ${token}` },
          }),
        )
      ).json()) as any;
      const page = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 0,
      });
      expect(detail.unread_count).toBe(2);
      expect(list.conversations[0].unread_count).toBe(2);
      expect(page.unread_count).toBe(2);
      expect(page.messages.map((m: any) => m.seq)).toEqual([1, 3]);
      expect(page.pruned_through_seq).toBe(0);
      expect(page.resume_after_seq).toBe(0);
      expect(page.gap_detected).toBe(true);
      expect(page.gap_from_seq).toBe(2);
      expect(page.gap_to_seq).toBe(2);
      expect(page.last_seq).toBe(3);

      const read1 = hub.conversationsService.markRead(agent, conv.id, {
        seq: 1,
      });
      expect(read1.read_seq).toBe(1);
      expect(read1.unread_count).toBe(1);
      const afterRead = hub.conversationsService.getConversation(
        agent,
        conv.id,
      );
      const afterList = hub.conversationsService.listConversations(agent);
      const afterPage = hub.conversationsService.listMessages(agent, conv.id);
      expect(afterRead.unread_count).toBe(1);
      expect(afterList.conversations[0].unread_count).toBe(1);
      expect(afterPage.unread_count).toBe(1);

      const catchUp = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 1,
      });
      expect(catchUp.messages.map((m: any) => m.seq)).toEqual([3]);
      expect(catchUp.gap_detected).toBe(true);
      expect(catchUp.gap_from_seq).toBe(2);
      expect(catchUp.gap_to_seq).toBe(2);
    });
  });

  it('64 same-timestamp service IDs keep unread equal to retained unseen rows', async () => {
    await freshHub(async hub => {
      const t = 1_792_189_500_000;
      (hub.conversationsService as any).now = () => t;
      hub.conversationsService.limits.messageRetentionMs = 1000;
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(t / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Tie IDs',
        members: ['proj:bot'],
      });
      for (let i = 0; i < 64; i++) {
        hub.conversationsService.sendMessage(human, {
          conversationId: conv.id,
          body: `m${i}`,
          idempotencyKey: `tie-${i}`,
        });
      }
      hub.sql.exec(
        'UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?',
        new Date(t).toISOString(),
        conv.id,
      );
      const later = t + 2000;
      (hub.conversationsService as any).now = () => later;
      expect(hub.conversationsService.retentionCleanup(later, 1).messagesDeleted).toBe(
        1,
      );
      const retained = count(
        hub.sql,
        'SELECT COUNT(*) AS n FROM conversation_messages WHERE conversation_id = ?',
        conv.id,
      );
      expect(retained).toBe(63);
      const agent = {
        caller: { actor: 'proj:bot', kind: 'agent' as const },
        scopes: ['bus:read'],
        authType: 'bearer' as const,
      };
      const unread = hub.conversationsService.getConversation(agent, conv.id)
        .unread_count;
      expect(unread).toBe(retained);
      expect(unread).not.toBe(64);
      const page = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 0,
        limit: 100,
      });
      expect(page.unread_count).toBe(retained);
      expect(page.messages).toHaveLength(retained);
    });
  });

  it('prefix prune, all-pruned catch-up, parent-removed child replay, and re-add stay consistent', async () => {
    await freshHub(async hub => {
      let t = 1_792_189_600_000;
      (hub.conversationsService as any).now = () => t;
      hub.conversationsService.limits.messageRetentionMs = 1000;
      hub.conversationsService.limits.removedMemberRetentionMs = 1000;
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(t / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const agent = {
        caller: { actor: 'proj:bot', kind: 'agent' as const },
        scopes: ['bus:read', 'bus:send'],
        authType: 'bearer' as const,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Prefix and replay',
        members: ['proj:bot'],
      });
      const parent = hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'parent',
        idempotencyKey: 'parent',
      });
      t += 900;
      const childInput = {
        conversationId: conv.id,
        body: 'child',
        idempotencyKey: 'child',
        replyTo: parent.id,
      };
      hub.conversationsService.sendMessage(human, childInput);
      t += 101;
      expect(hub.conversationsService.retentionCleanup(t, 1).messagesDeleted).toBe(
        1,
      );
      const afterParent = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 0,
      });
      expect(afterParent.messages.map((m: any) => m.seq)).toEqual([2]);
      expect(afterParent.pruned_through_seq).toBe(1);
      expect(afterParent.gap_detected).toBe(true);
      expect(afterParent.unread_count).toBe(1);
      expect(hub.conversationsService.sendMessage(human, childInput).replay).toBe(
        true,
      );

      hub.conversationsService.removeMember(human, conv.id, 'proj:bot');
      expect(() =>
        hub.conversationsService.getConversation(agent, conv.id),
      ).toThrow('member_removed');
      t += 2000;
      hub.conversationsService.retentionCleanup(t, 100);
      const readd = hub.conversationsService.addMember(
        human,
        conv.id,
        'proj:bot',
      );
      expect(readd.member.last_read_seq).toBe(2);
      expect(hub.conversationsService.getConversation(agent, conv.id).unread_count).toBe(
        0,
      );
      expect(hub.conversationsService.listMessages(agent, conv.id).unread_count).toBe(
        0,
      );

      const more = hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'after readd',
        idempotencyKey: 'after-readd',
      });
      expect(more.seq).toBe(3);
      const page = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 0,
      });
      expect(page.unread_count).toBe(1);
      expect(
        hub.conversationsService.listConversations(agent).conversations[0]
          .unread_count,
      ).toBe(1);
      expect(hub.conversationsService.getConversation(agent, conv.id).unread_count).toBe(
        1,
      );

      t += 2000;
      hub.conversationsService.retentionCleanup(t, 100);
      const empty = hub.conversationsService.listMessages(agent, conv.id, {
        afterSeq: 0,
      });
      expect(empty.messages).toEqual([]);
      expect(empty.unread_count).toBe(0);
      expect(empty.gap_detected).toBe(true);
      expect(empty.pruned_through_seq).toBe(3);
      expect(empty.has_more_after).toBe(false);
    });
  });
});

describe('LOW malformed and unsupported schema_version fail closed', () => {
  it('rejects NaN/non-numeric and version 3 while still upgrading original and v1', async () => {
    await freshHub(async (hub, state) => {
      const human = {
        session: {
          sub: 'alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'csrf-ret-token',
          exp: Math.floor(Date.now() / 1000) + 86400,
        },
        csrfVerified: true,
      };
      const conv = hub.conversationsService.createConversation(human, {
        kind: 'group',
        title: 'Version gate',
      });
      hub.conversationsService.sendMessage(human, {
        conversationId: conv.id,
        body: 'kept',
        idempotencyKey: 'kept',
      });

      hub.sql.exec(
        "UPDATE conversation_storage_usage SET schema_version = 'corrupt', message_count = 0, message_bytes = 0",
      );
      expect(() => hub.conversationsService.init()).toThrow(
        /unsupported_conversation_schema_version|unsupported_schema_version/,
      );

      hub.sql.exec(
        'UPDATE conversation_storage_usage SET schema_version = 3, message_count = 0, message_bytes = 0',
      );
      expect(() => hub.conversationsService.init()).toThrow(
        /unsupported_conversation_schema_version|unsupported_schema_version/,
      );

      hub.sql.exec(
        'UPDATE conversation_storage_usage SET schema_version = 1, message_count = 0, message_bytes = 0',
      );
      hub.conversationsService.limits.maxMessagesPerConversation = 1;
      hub.conversationsService.init();
      expect(
        Number(
          (
            hub.sql
              .exec(
                'SELECT schema_version FROM conversation_storage_usage WHERE singleton = 1',
              )
              .toArray()[0] as any
          ).schema_version,
        ),
      ).toBe(2);
      expect(hub.conversationsService.getStorageUsage().message_count).toBe(1);
      expect(() =>
        hub.conversationsService.sendMessage(human, {
          conversationId: conv.id,
          body: 'over cap',
          idempotencyKey: 'over',
        }),
      ).toThrow('conversation_message_limit_exceeded');

      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_storage_usage');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_messages');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_members');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversations');
      state.storage.sql.exec(`
        CREATE TABLE conversations (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          created_by TEXT NOT NULL,
          idempotency_key TEXT,
          request_json TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          archived_at TEXT,
          archived_by TEXT,
          last_seq INTEGER NOT NULL DEFAULT 0,
          last_message_at TEXT,
          last_message_preview TEXT,
          UNIQUE(created_by, idempotency_key)
        );
        CREATE TABLE conversation_members (
          conversation_id TEXT NOT NULL,
          actor TEXT NOT NULL,
          kind TEXT NOT NULL,
          role TEXT NOT NULL,
          added_at TEXT NOT NULL,
          added_by TEXT NOT NULL,
          removed_at TEXT,
          removed_by TEXT,
          last_read_seq INTEGER NOT NULL DEFAULT 0,
          last_read_at TEXT,
          PRIMARY KEY(conversation_id, actor)
        );
        CREATE TABLE conversation_messages (
          id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          sender_actor TEXT NOT NULL,
          sender_kind TEXT NOT NULL,
          kind TEXT NOT NULL,
          body TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          request_json TEXT NOT NULL,
          reply_to TEXT,
          reply_to_seq INTEGER,
          bridge_ref TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(conversation_id, seq),
          UNIQUE(conversation_id, sender_actor, idempotency_key)
        );
      `);
      const t0 = new Date().toISOString();
      state.storage.sql.exec(
        "INSERT INTO conversations (id, kind, title, created_by, created_at, updated_at, last_seq) VALUES ('legacy', 'group', 'Legacy', 'human:alice', ?, ?, 1)",
        t0,
        t0,
      );
      state.storage.sql.exec(
        "INSERT INTO conversation_members (conversation_id, actor, kind, role, added_at, added_by) VALUES ('legacy', 'human:alice', 'human', 'owner', ?, 'human:alice')",
        t0,
      );
      state.storage.sql.exec(
        "INSERT INTO conversation_messages (id, conversation_id, seq, sender_actor, sender_kind, kind, body, idempotency_key, request_json, created_at) VALUES ('old', 'legacy', 1, 'human:alice', 'human', 'message', 'Synthetic old', 'old', '{}', ?)",
        t0,
      );
      const upgraded = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { limits: { maxMessagesPerConversation: 1 } },
      );
      upgraded.init();
      expect(upgraded.getStorageUsage().message_count).toBe(1);
      expect(() =>
        upgraded.sendMessage(human, {
          conversationId: 'legacy',
          body: 'too many',
          idempotencyKey: 'too-many',
        }),
      ).toThrow('conversation_message_limit_exceeded');
    });
  });
});

describe('M5 automation retirement shares the existing global allowance', () => {
  it('counts actual automation deletions with bus and conversation rows and rolls authority fencing back on failure', async () => {
    await freshHub((hub, state) => {
      const t = 1_792_189_800_000;
      hub.sql.exec(`INSERT INTO conversation_automation_grants VALUES
        ('ret-g','gone','removed','human:alice',NULL,'alice',0,'approved','always',NULL)`);
      hub.sql.exec(`INSERT INTO hub_check_slots VALUES
        ('removed','legacy-key',0,'{"action":{"grant_id":"ret-g"}}','{"status":"pass","detail":"old"}',NULL,NULL,'done',NULL,0)`);
      hub.sql.exec(`INSERT INTO conversation_automation_receipts VALUES ('ret-effect','{}','ret-msg','gone',0)`);
      insertExpiredBusMessage(hub.sql, 'retirement-bus', t);
      insertExpiredClaim(hub.sql, 'retirement-claim', t);
      const snapshot = () => ({
        slots: hub.sql.exec('SELECT * FROM hub_check_slots ORDER BY name').toArray(),
        grants: hub.sql.exec('SELECT * FROM conversation_automation_grants ORDER BY id').toArray(),
        receipts: hub.sql.exec('SELECT * FROM conversation_automation_receipts ORDER BY effect_key').toArray(),
        base: tableCounts(hub.sql),
        cursor: hub.sql.exec('SELECT * FROM retention_cleanup_cursor ORDER BY kind').toArray(),
      });
      hub.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
      const before = snapshot();
      hub.sql.exec(`CREATE TRIGGER abort_automation_retirement BEFORE DELETE ON conversation_automation_receipts BEGIN SELECT RAISE(ABORT, 'injected automation retirement failure'); END`);
      expect(() => hub.busService.retentionCleanup(t, 100)).toThrow(/injected automation retirement failure/);
      expect(snapshot()).toEqual(before);
      hub.sql.exec('DROP TRIGGER abort_automation_retirement');
      const automationCount = () => count(hub.sql, `SELECT
        (SELECT count(*) FROM hub_check_slots)+(SELECT count(*) FROM conversation_automation_grants)+(SELECT count(*) FROM conversation_automation_receipts) n`);
      const all = () => totalRows(hub.sql) + automationCount();
      const observed = new Set<string>();
      for (let i = 0; i < 10; i++) {
        const prev = all();
        const result = hub.busService.retentionCleanup(t, [1, 2, 3][i % 3]) as any;
        const actual = prev - all();
        const reported = result.messagesDeleted + result.claimsDeleted + result.conversationRowsDeleted;
        expect(actual).toBe(reported); expect(actual).toBeLessThanOrEqual([1, 2, 3][i % 3]);
        if (result.automationRowsDeleted) observed.add('automation');
        if (result.messagesDeleted || result.claimsDeleted) observed.add('bus');
        if (!result.hasMore) break;
      }
      expect(observed).toEqual(new Set(['automation', 'bus']));
      expect(automationCount()).toBe(0);
      expect(hub.busService.hasExpiredRows(t) || hub.conversationsService.hasExpiredRows(t)).toBe(false);
      // Restart uses the same persisted authority and fair cursor, not a reset.
      const restarted = new Hub(state, testEnv);
      expect(restarted.conversationsService.hasExpiredRows(t)).toBe(false);
    });
  });
});
