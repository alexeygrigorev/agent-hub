import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { BusService, BUS_SCHEMA_SQL, CallerContext } from '../src/bus';

const defaultEnv = env as unknown as Env;

describe('M3 Bus Service & Schema Tests', () => {
  let stub: any;

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      for (const statement of BUS_SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) {
          try { instance.sql.exec(trimmed); } catch (e) {}
        }
      }

      instance.sql.exec('DELETE FROM messages');
      instance.sql.exec('DELETE FROM claims');
      try { instance.sql.exec('DELETE FROM agents'); } catch (e) {}
      try { instance.sql.exec('DELETE FROM machines'); } catch (e) {}
      try { instance.sql.exec('DELETE FROM credentials'); } catch (e) {}

      instance.sql.exec("CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, name TEXT, hostname TEXT, registered_at TEXT)");
      instance.sql.exec("CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, kind TEXT, token_hash TEXT UNIQUE, scopes JSON, family_id TEXT, subject TEXT, created_by_sub TEXT, created_at TEXT, expires_at TEXT, revoked_at TEXT, machine_id TEXT, actor TEXT, last_used_at TEXT)");

      instance.sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('machine-A', 'Machine A', 'host-a', '2026-01-01T00:00:00Z')");
      instance.sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('machine-B', 'Machine B', 'host-b', '2026-01-01T00:00:00Z')");

      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'machine-A')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1b', 'projA', 'worker', 'machine-A')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-2', 'projB', 'worker', 'machine-B')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('lead-1', 'projA', 'lead', 'machine-A')");
    });
  });

  it('1: BusService.send caller authorization binding (forged sender throws unauthorized)', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      expect(() => bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-2',
        toActor: 'worker-1',
        kind: 'note',
        body: 'forged sender message',
        idempotencyKey: 'forge-key-1',
        now
      })).toThrow('unauthorized');
    });
  });

  it('2: role:root routing fails closed without accepted M4 authority', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      // Sending as role:root must fail closed
      expect(() => bus.send({ actor: 'role:root' }, {
        senderActor: 'role:root',
        toActor: 'worker-1',
        kind: 'note',
        body: 'unauthorized root send',
        idempotencyKey: 'root-send-fail',
        now
      })).toThrow(/missing_m4_root_authority/);

      // Sending to role:root must fail closed (unknown recipient)
      expect(() => bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'role:root',
        kind: 'note',
        body: 'unauthorized to root',
        idempotencyKey: 'to-root-fail',
        now
      })).toThrow('unknown_recipient');

      // Attempting to claim scope as role:root must fail closed
      expect(() => bus.claim({ actor: 'role:root' }, 'projA', 'src', 'role:root', 60000, now)).toThrow(/missing_m4_root_authority/);
    });
  });

  it('3: Machine binding prevents cross-project access for same tag', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const callerMachineA: CallerContext = { machineId: 'machine-A' };
      // machine-A only hosts projA:worker-1, so reading projB:worker-2 or cross-project throws
      expect(() => bus.inbox(callerMachineA, 'projB:worker-1')).toThrow('unauthorized');
      expect(() => bus.inbox(callerMachineA, 'projB:worker-2')).toThrow('unauthorized');
    });
  });

  it('4: role:root inbox and show fail closed', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const callerMachineA: CallerContext = { machineId: 'machine-A' };
      expect(() => bus.inbox(callerMachineA, 'role:root')).toThrow(/missing_m4_root_authority/);
    });
  });

  it('5: validateRecipient fails closed for role:root and unknown recipients', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      expect(bus.validateRecipient('role:root')).toBe(false);
      expect(bus.validateRecipient('role:nonexistent')).toBe(false);
      expect(bus.validateRecipient('machine:unknown-mach')).toBe(false);
      expect(bus.validateRecipient('unknown-tag')).toBe(false);
      expect(bus.validateRecipient('worker-1')).toBe(true);
      expect(bus.validateRecipient('projA:worker-1')).toBe(true);
      expect(bus.validateRecipient('machine:machine-A')).toBe(true);
      expect(bus.validateRecipient('role:lead')).toBe(true);
    });
  });

  it('6: Body size limit 16 KiB strictly enforced', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      const okBody = 'a'.repeat(16384);
      const msg = bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: okBody,
        idempotencyKey: 'ok-size-key',
        now
      });
      expect(msg.id).toBeTruthy();

      const tooLargeBody = 'a'.repeat(16385);
      expect(() => bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: tooLargeBody,
        idempotencyKey: 'oversize-key',
        now
      })).toThrow('body_too_large');
    });
  });

  it('7: Idempotent replay returns original message, conflicting throws conflict', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      const res1 = bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: 'hello world',
        idempotencyKey: 'idemp-1',
        now
      });

      // Identical replay returns same ID
      const res2 = bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: 'hello world',
        idempotencyKey: 'idemp-1',
        now
      });
      expect(res2.id).toBe(res1.id);
      expect(res2.thread_id).toBe(res1.thread_id);

      // Conflicting payload with same idempotency key throws conflict
      expect(() => bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: 'different body',
        idempotencyKey: 'idemp-1',
        now
      })).toThrow('conflict');
    });
  });

  it('8: Scope normalization and traversal rejection', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      expect(() => bus.claim({ actor: 'worker-1' }, 'projA', '../escape/path', 'worker-1', 60000, now)).toThrow('invalid_scope');
      expect(() => bus.claim({ actor: 'worker-1' }, 'projA', 'foo/../../bar', 'worker-1', 60000, now)).toThrow('invalid_scope');

      const claimRes = bus.claim({ actor: 'worker-1' }, 'projA', './src//nested///file.ts', 'worker-1', 60000, now);
      expect(claimRes.id).toBeTruthy();

      const stored = instance.sql.exec('SELECT scope FROM claims WHERE id = ?', claimRes.id).toArray()[0] as any;
      expect(stored.scope).toBe('src/nested/file.ts');
    });
  });

  it('9: Claims overlapping prefix conflicts for different holders, extends for same holder', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      // worker-1 claims src/bus.ts
      const c1 = bus.claim({ actor: 'worker-1' }, 'projA', 'src/bus.ts', 'worker-1', 60000, now);

      // worker-1b attempts to claim overlapping parent src -> conflict
      expect(() => bus.claim({ actor: 'worker-1b' }, 'projA', 'src', 'worker-1b', 60000, now)).toThrow('conflict');

      // worker-1b attempts to claim exact same src/bus.ts -> conflict
      expect(() => bus.claim({ actor: 'worker-1b' }, 'projA', 'src/bus.ts', 'worker-1b', 60000, now)).toThrow('conflict');

      // worker-1 re-claims same scope -> renewal/extension (same claim ID)
      const c2 = bus.claim({ actor: 'worker-1' }, 'projA', 'src/bus.ts', 'worker-1', 120000, now);
      expect(c2.id).toBe(c1.id);

      // worker-1 releases claim
      bus.releaseClaim({ actor: 'worker-1' }, c1.id, now);

      // Now worker-1b can claim src
      const c3 = bus.claim({ actor: 'worker-1b' }, 'projA', 'src', 'worker-1b', 60000, now);
      expect(c3.id).toBeTruthy();
    });
  });

  it('10: Retention cleanup is bounded and reports hasMore correctly', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const past = Date.now() - 500000;

      for (let i = 0; i < 150; i++) {
        instance.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'b', ?, ?, ?)",
          `batch-msg-${i}`, `batch-key-${i}`, past - 1000, past
        );
      }

      const res1 = bus.retentionCleanup(Date.now(), 100);
      expect(res1.messagesDeleted).toBe(100);
      expect(res1.hasMore).toBe(true);

      const res2 = bus.retentionCleanup(Date.now(), 100);
      expect(res2.messagesDeleted).toBe(50);
      expect(res2.hasMore).toBe(false);
    });
  });

  it('11: Stage progression requires recipient authorization and correct stage sequence', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      const msg = bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: 'stage test',
        idempotencyKey: 'stg-key',
        now
      });

      // Sender (worker-1) cannot ack (only recipient worker-2 can)
      expect(() => bus.ack({ actor: 'worker-1' }, msg.id, now)).toThrow('unauthorized');

      // Cannot ack before delivered/read
      expect(() => bus.ack({ actor: 'worker-2' }, msg.id, now)).toThrow('invalid_stage');

      bus.deliver({ actor: 'worker-2' }, msg.id, now);
      bus.read({ actor: 'worker-2' }, msg.id, now);
      bus.ack({ actor: 'worker-2' }, msg.id, now);
      bus.accept({ actor: 'worker-2' }, msg.id, now);
      bus.complete({ actor: 'worker-2' }, msg.id, now);

      const updated = bus.show({ actor: 'worker-2' }, msg.id);
      expect(updated.delivered_at).toBeTruthy();
      expect(updated.read_at).toBeTruthy();
      expect(updated.acked_at).toBeTruthy();
      expect(updated.accepted_at).toBeTruthy();
      expect(updated.completed_at).toBeTruthy();
    });
  });

  it('12: Reply verification requires valid parent message and matching participants', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      const msg = bus.send({ actor: 'worker-1' }, {
        senderActor: 'worker-1',
        toActor: 'worker-2',
        kind: 'note',
        body: 'initial msg',
        idempotencyKey: 'reply-init-key',
        now
      });

      // Third party (worker-1b) cannot reply
      expect(() => bus.send({ actor: 'worker-1b' }, {
        senderActor: 'worker-1b',
        toActor: 'worker-2',
        kind: 'reply',
        body: 'third party intrusion',
        idempotencyKey: '3p-reply-key',
        replyTo: msg.id,
        now
      })).toThrow('invalid_reply_to');

      // Recipient replying to sender succeeds
      const reply = bus.send({ actor: 'worker-2' }, {
        senderActor: 'worker-2',
        toActor: 'worker-1',
        kind: 'reply',
        body: 'acknowledged and finished',
        idempotencyKey: 'legit-reply-key',
        replyTo: msg.id,
        now
      });
      expect(reply.id).toBeTruthy();
      expect(reply.thread_id).toBe(msg.thread_id);
    });
  });

  it('13: getBusActors enforces caller scoping and excludes role:root', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);

      // Agent caller: only own authorized actor
      const agentActors = bus.getBusActors({ actor: 'worker-1' });
      expect(agentActors).toContain('projA:worker-1');
      expect(agentActors).not.toContain('projB:worker-2');
      expect(agentActors).not.toContain('machine:machine-A');
      expect(agentActors).not.toContain('machine:machine-B');
      expect(agentActors).not.toContain('role:root');

      // Host caller: only machine and hosted agents
      const hostActors = bus.getBusActors({ kind: 'host', machineId: 'machine-A' });
      expect(hostActors).toContain('machine:machine-A');
      expect(hostActors).toContain('projA:worker-1');
      expect(hostActors).not.toContain('projB:worker-2');
      expect(hostActors).not.toContain('machine:machine-B');
      expect(hostActors).not.toContain('role:root');

      // Admin caller: all actors, role:root excluded
      const adminActors = bus.getBusActors({ isAdmin: true });
      expect(adminActors).toContain('projA:worker-1');
      expect(adminActors).toContain('projB:worker-2');
      expect(adminActors).toContain('machine:machine-A');
      expect(adminActors).toContain('machine:machine-B');
      expect(adminActors).not.toContain('role:root');

      // role:root caller: fails closed
      expect(() => bus.getBusActors({ actor: 'role:root' })).toThrow(/missing_m4_root_authority/);
    });
  });

  it('14: getBusMessages scopes to caller, derives principal binding, and denies cross-actor access', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();

      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('m-w1-w2', 'worker-1', 'worker-2', 'note', 'th1', 'b1', 'k1', ?, ?)",
        now - 2000, now + 86400000
      );
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('m-w2-w1b', 'worker-2', 'worker-1b', 'note', 'th2', 'b2', 'k2', ?, ?)",
        now - 1000, now + 86400000
      );

      // Agent worker-1 attempting to retrieve worker-2 messages -> unauthorized
      expect(() => bus.getBusMessages({ actor: 'worker-1' }, 'worker-2')).toThrow('unauthorized');

      // Agent worker-1 attempting to retrieve role:root messages -> fails closed
      expect(() => bus.getBusMessages({ actor: 'worker-1' }, 'role:root')).toThrow(/missing_m4_root_authority/);

      // Agent worker-1 retrieving without actor param derives worker-1 principal binding
      const w1Msgs = bus.getBusMessages({ actor: 'worker-1' });
      expect(w1Msgs.some((m: any) => m.id === 'm-w1-w2')).toBe(true);
      expect(w1Msgs.some((m: any) => m.id === 'm-w2-w1b')).toBe(false);

      // Host machine-A retrieving actor on machine-B -> unauthorized
      expect(() => bus.getBusMessages({ kind: 'host', machineId: 'machine-A' }, 'worker-2')).toThrow('unauthorized');

      // Host machine-A retrieving without actor param returns hosted agents (both worker-1 and worker-1b)
      const hostMsgs = bus.getBusMessages({ kind: 'host', machineId: 'machine-A' });
      expect(hostMsgs.some((m: any) => m.id === 'm-w1-w2')).toBe(true);
      expect(hostMsgs.some((m: any) => m.id === 'm-w2-w1b')).toBe(true);

      // Admin query for role:root fails closed
      expect(() => bus.getBusMessages({ isAdmin: true }, 'role:root')).toThrow(/missing_m4_root_authority/);
    });
  });
});
