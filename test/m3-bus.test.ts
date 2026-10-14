import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { BusService, BUS_SCHEMA_SQL, CallerContext, parseAddress } from '../src/bus';
import { SCHEMA_SQL } from '../src/schema';
import { sha256 } from '../src/auth/crypto';
import { doFetch } from './m1-focus-setup';

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

// Corrections over the real wire path (Worker -> Hub DO -> BusService -> SQLite). No test here
// writes delivered_at/read_at/acked_at directly; expires_at is the only clock fixture set in SQL.
describe('M3 bus corrections: recipient ACK, addresses, aliases, expiry', () => {
  let stub: any;

  async function seedCred(sql: any, id: string, token: string, kind: string, machineId: string, actor: string | null, scopes: string[]) {
    const now = new Date().toISOString();
    const future = new Date(Date.now() + 86400000).toISOString();
    sql.exec(
      "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      id, kind, await sha256(token), `fam_${id}`, actor || machineId, actor, machineId, JSON.stringify(scopes), 'admin-1', now, future
    );
  }

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      for (const schema of [SCHEMA_SQL, BUS_SCHEMA_SQL]) {
        for (const statement of schema.split(';')) {
          const trimmed = statement.trim();
          if (trimmed) {
            try { instance.sql.exec(trimmed); } catch (e) {}
          }
        }
      }
      for (const table of ['messages', 'claims', 'agents', 'machines', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
      const now = new Date().toISOString();
      for (const m of ['mach-A', 'mach-B']) {
        instance.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', m, m, m, now);
      }
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'mach-A')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-2', 'projB', 'worker', 'mach-B')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('principal-1', 'projA', 'principal', 'mach-A')");
      await seedCred(instance.sql, 'c_host_a', 'tok_host_a', 'host', 'mach-A', null, ['host', 'bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_host_b', 'tok_host_b', 'host', 'mach-B', null, ['host', 'bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_w1', 'tok_w1', 'agent', 'mach-A', 'worker-1', ['bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_w2q', 'tok_w2q', 'agent', 'mach-B', 'projB:worker-2', ['bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_p1', 'tok_p1', 'agent', 'mach-A', 'principal-1', ['bus:send', 'bus:read']);
    });
  });

  const http = (method: string, path: string, token: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method,
    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));

  const send = (token: string, from: string, to: string, key: string, body = 'hello') =>
    http('POST', '/api/bus/send', token, { from_actor: from, to_actor: to, kind: 'note', body, idempotency_key: key });

  const row = (id: string) => runInDurableObject(stub, async (instance: Hub) =>
    instance.sql.exec('SELECT * FROM messages WHERE id = ?', id).toArray()[0] as any);

  it('15: GAP-1 recipient inbox marks delivered and HTTP ACK succeeds without SQL forging', async () => {
    const sent = await send('tok_w1', 'worker-1', 'worker-2', 'g1-key');
    expect(sent.status).toBe(200);
    const { id } = await sent.json() as any;

    // Sender ACK and a foreign host's inbox are refused and do not mark anything
    expect((await http('POST', `/api/bus/messages/${id}/ack`, 'tok_w1')).status).toBe(403);
    expect((await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_a')).status).toBe(403);
    expect((await http('GET', `/api/bus/messages/${id}`, 'tok_w1')).status).toBe(200);
    let r = await row(id);
    expect(r.delivered_at).toBeNull();
    expect(r.read_at).toBeNull();

    // Before any recipient read, ACK is still out of order
    expect((await http('POST', `/api/bus/messages/${id}/ack`, 'tok_host_b')).status).toBe(400);

    // Recipient host bridge polls the inbox: message is delivered
    const inboxRes = await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_b');
    expect(inboxRes.status).toBe(200);
    const inbox = await inboxRes.json() as any[];
    expect(inbox.map(m => m.id)).toEqual([id]);
    expect(inbox[0].delivered_at).toBeTruthy();
    r = await row(id);
    expect(r.delivered_at).toBe(inbox[0].delivered_at);
    expect(r.read_at).toBeNull();

    // A second poll keeps the first delivery time
    const again = await (await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_b')).json() as any[];
    expect(again[0].delivered_at).toBe(r.delivered_at);

    const ack = await http('POST', `/api/bus/messages/${id}/ack`, 'tok_host_b');
    expect(ack.status).toBe(200);
    expect((await row(id)).acked_at).toBeTruthy();
    expect((await http('POST', `/api/bus/messages/${id}/accept`, 'tok_host_b')).status).toBe(200);
    expect((await http('POST', `/api/bus/messages/${id}/complete`, 'tok_host_b')).status).toBe(200);
  });

  it('16: GAP-1 recipient show marks delivered+read; sender and admin views do not', async () => {
    const { id } = await (await send('tok_w1', 'worker-1', 'worker-2', 'g1-show')).json() as any;
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = instance.busService;
      bus.show({ isAdmin: true }, id);
      bus.inbox({ isAdmin: true }, 'worker-2');
      bus.show({ actor: 'worker-1' }, id);
    });
    let r = await row(id);
    expect(r.delivered_at).toBeNull();
    expect(r.read_at).toBeNull();

    const shown = await http('GET', `/api/bus/messages/${id}`, 'tok_w2q');
    expect(shown.status).toBe(200);
    const msg = await shown.json() as any;
    expect(msg.delivered_at).toBeTruthy();
    expect(msg.read_at).toBeTruthy();
    r = await row(id);
    expect(r.read_at).toBe(msg.read_at);
    expect((await http('POST', `/api/bus/messages/${id}/ack`, 'tok_w2q')).status).toBe(200);
  });

  it('17: GAP-4 malformed and ambiguous addresses are rejected and never stored', async () => {
    for (const to of ['projB:worker-2:extra', 'projB:worker-2:', 'projB::worker-2', ':worker-2', 'worker-2:', ' worker-2', 'worker 2', 'role:', 'machine:', 'role:worker', 'role:root', 'role:rootx']) {
      const res = await send('tok_w1', 'worker-1', to, `bad-${to}`);
      expect([to, res.status]).toEqual([to, 404]);
    }
    const stored = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec('SELECT COUNT(*) AS n FROM messages').toArray()[0] as any);
    expect(stored.n).toBe(0);

    // Unique role resolves; machine and both agent forms stay valid
    for (const to of ['role:principal', 'machine:mach-B', 'worker-2', 'projB:worker-2']) {
      expect([to, (await send('tok_w1', 'worker-1', to, `ok-${to}`)).status]).toEqual([to, 200]);
    }

    // Malformed actor in inbox is refused, not matched against stored strings
    expect((await http('GET', `/api/bus/inbox?actor=${encodeURIComponent('projB:worker-2:extra')}`, 'tok_host_b')).status).toBe(403);
  });

  it('18: GAP-4 inbox and ACK are consistent across tag, project:tag and unique role aliases', async () => {
    const q = await (await send('tok_w1', 'worker-1', 'projB:worker-2', 'alias-q')).json() as any;
    const t = await (await send('tok_w1', 'worker-1', 'worker-2', 'alias-t')).json() as any;

    // Bare-tag inbox (host) and qualified-actor inbox (agent) see both messages
    const viaTag = await (await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_b')).json() as any[];
    expect(viaTag.map(m => m.id).sort()).toEqual([q.id, t.id].sort());
    const viaAgent = await (await http('GET', `/api/bus/inbox?actor=${encodeURIComponent('projB:worker-2')}`, 'tok_w2q')).json() as any[];
    expect(viaAgent.map(m => m.id).sort()).toEqual([q.id, t.id].sort());

    // The qualified-actor credential may ACK the bare-tag message and vice versa for the host
    expect((await http('POST', `/api/bus/messages/${t.id}/ack`, 'tok_w2q')).status).toBe(200);
    expect((await http('POST', `/api/bus/messages/${q.id}/ack`, 'tok_host_b')).status).toBe(200);

    // role:principal is principal-1's inbox; another agent cannot read or ACK it
    const r = await (await send('tok_w1', 'worker-1', 'role:principal', 'alias-role')).json() as any;
    expect((await http('GET', `/api/bus/inbox?actor=${encodeURIComponent('role:principal')}`, 'tok_w1')).status).toBe(403);
    expect((await http('POST', `/api/bus/messages/${r.id}/ack`, 'tok_w1')).status).toBe(403);
    const p1 = await (await http('GET', '/api/bus/inbox?actor=principal-1', 'tok_p1')).json() as any[];
    expect(p1.map(m => m.id)).toEqual([r.id]);
    expect((await http('POST', `/api/bus/messages/${r.id}/ack`, 'tok_p1')).status).toBe(200);

    // Once the role is shared it is no longer an alias of principal-1 and no longer a valid address
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('principal-2', 'projB', 'principal', 'mach-B')");
    });
    expect((await send('tok_w1', 'worker-1', 'role:principal', 'alias-role-2')).status).toBe(404);
    const p1After = await (await http('GET', '/api/bus/inbox?actor=principal-1', 'tok_p1')).json() as any[];
    expect(p1After).toEqual([]);
  });

  it('19: GAP-9 expired messages are not served and their idempotency key is reusable', async () => {
    const first = await (await send('tok_w1', 'worker-1', 'worker-2', 'exp-key', 'same body')).json() as any;
    // Replay inside the window returns the original id
    expect((await (await send('tok_w1', 'worker-1', 'worker-2', 'exp-key', 'same body')).json() as any).id).toBe(first.id);

    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE messages SET expires_at = ? WHERE id = ?', Date.now() - 1, first.id);
    });
    expect(await (await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_b')).json()).toEqual([]);
    expect((await http('GET', `/api/bus/messages/${first.id}`, 'tok_host_b')).status).toBe(404);
    expect((await http('POST', `/api/bus/messages/${first.id}/ack`, 'tok_host_b')).status).toBe(404);
    expect((await http('POST', '/api/bus/reply', 'tok_host_b', {
      from_actor: 'worker-2', to_actor: 'worker-1', kind: 'reply', body: 'late', idempotency_key: 'late-reply', reply_to: first.id,
    })).status).toBe(400);

    // Same key after expiry but before retention purge: a new message, same as after a purge
    const second = await (await send('tok_w1', 'worker-1', 'worker-2', 'exp-key', 'same body')).json() as any;
    expect(second.id).not.toBe(first.id);
    const rows = await runInDurableObject(stub, async (instance: Hub) =>
      instance.sql.exec("SELECT id FROM messages WHERE idempotency_key = 'exp-key'").toArray() as any[]);
    expect(rows.map(x => x.id)).toEqual([second.id]);
  });

  it('20: retention purge boundary matches the read boundary (expires_at <= now)', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const now = Date.now();
      instance.sql.exec("INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('edge', 'worker-1', 'worker-2', 'note', 'th', 'b', 'edge-k', ?, ?)", now - 10, now);
      instance.sql.exec("INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('live', 'worker-1', 'worker-2', 'note', 'th', 'b', 'live-k', ?, ?)", now - 10, now + 60000);
      expect(bus.retentionCleanup(now, 100).messagesDeleted).toBe(1);
      expect(instance.sql.exec('SELECT id FROM messages').toArray().map((m: any) => m.id)).toEqual(['live']);
    });
  });

  it('21: parseAddress grammar and service-level role/sender checks', async () => {
    expect(parseAddress('worker-1')).toEqual({ kind: 'tag', tag: 'worker-1' });
    expect(parseAddress('projA:worker-1')).toEqual({ kind: 'qualified', project: 'projA', tag: 'worker-1' });
    expect(parseAddress('role:principal')).toEqual({ kind: 'role', role: 'principal' });
    expect(parseAddress('machine:mach-A')).toEqual({ kind: 'machine', machineId: 'mach-A' });
    for (const bad of ['', 'a:b:c', 'a:', ':a', 'a::b', ' a', 'a\tb', 'x'.repeat(129), null, 7]) {
      expect(parseAddress(bad)).toBeNull();
    }
    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      expect(bus.validateRecipient('role:worker')).toBe(false); // two workers: undefined fan-out
      expect(bus.validateRecipient('role:principal')).toBe(true);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-A' }, 'role:principal')).toBe(true);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-B' }, 'role:principal')).toBe(false);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-A' }, 'role:worker')).toBe(false);
      expect(bus.isAuthorizedForActor({ isAdmin: true }, 'a:b:c')).toBe(false);
      expect(() => bus.send({ isAdmin: true }, {
        senderActor: 'projA:worker-1:x', toActor: 'worker-2', kind: 'note', body: 'b', idempotencyKey: 'bad-sender',
      })).toThrow('unauthorized');
    });
  });

  it('22: Root send path keeps idempotent dedup and still refuses role:root', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      const rootBus = (instance.jobsService as any).busService;
      const opts = { senderActor: 'role:root', toActor: 'role:principal', kind: 'note', body: 'skip', idempotencyKey: 'root-dedup', leaseEpoch: 3 };
      const a = rootBus.sendRootMessage({ actor: 'role:root', isAdmin: true }, opts);
      const b = rootBus.sendRootMessage({ actor: 'role:root', isAdmin: true }, opts);
      expect(b.id).toBe(a.id);
      expect(() => rootBus.sendRootMessage({ actor: 'role:root', isAdmin: true }, { ...opts, idempotencyKey: 'root-2', toActor: 'role:root' })).toThrow('unknown_recipient');
      expect(() => rootBus.sendRootMessage({ actor: 'role:root', isAdmin: true }, { ...opts, idempotencyKey: 'root-3', toActor: 'p:t:x' })).toThrow('unknown_recipient');
      expect(instance.sql.exec("SELECT COUNT(*) AS n FROM messages WHERE from_actor = 'role:root'").toArray()[0]).toEqual({ n: 1 });
    });
  });

  const reply = (token: string, replyTo: string, from: string, to: string, key: string) =>
    http('POST', '/api/bus/reply', token, { from_actor: from, to_actor: to, kind: 'reply', body: `re ${key}`, idempotency_key: key, reply_to: replyTo });

  const count = (where: string, ...params: any[]) => runInDurableObject(stub, async (instance: Hub) =>
    (instance.sql.exec(`SELECT COUNT(*) AS n FROM ${where}`, ...params).toArray()[0] as any).n as number);

  it('23: F1 reply_to participants compare by canonical project+tag, so any alias form may reply', async () => {
    const m = await (await send('tok_w1', 'worker-1', 'projB:worker-2', 'f1-m')).json() as any;
    const r = await (await send('tok_w1', 'projA:worker-1', 'role:principal', 'f1-r')).json() as any;

    // Recipient replies in the other alias form of itself and of the original sender
    for (const [token, from, to, key] of [
      ['tok_host_b', 'worker-2', 'worker-1', 'f1-a'],
      ['tok_w2q', 'projB:worker-2', 'projA:worker-1', 'f1-b'],
      ['tok_host_b', 'worker-2', 'projA:worker-1', 'f1-c'],
    ]) {
      const res = await reply(token, m.id, from, to, key);
      expect([key, res.status]).toEqual([key, 200]);
      expect((await res.json() as any).thread_id).toBe(m.thread_id);
    }
    // role:principal is principal-1's unique alias while principal-1 is the only holder
    expect((await reply('tok_p1', r.id, 'principal-1', 'worker-1', 'f1-d')).status).toBe(200);
    expect((await reply('tok_host_a', r.id, 'projA:principal-1', 'projA:worker-1', 'f1-e')).status).toBe(200);

    // A third agent, or a reply addressed to a non-party, is still refused and stores nothing
    expect((await reply('tok_p1', m.id, 'principal-1', 'worker-1', 'f1-x1')).status).toBe(400);
    expect((await reply('tok_host_b', m.id, 'worker-2', 'principal-1', 'f1-x2')).status).toBe(400);
    expect((await reply('tok_host_b', m.id, 'worker-2', 'machine:mach-A', 'f1-x3')).status).toBe(400);
    expect(await count('messages WHERE reply_to = ?', m.id)).toBe(3);
    expect(await count('messages WHERE reply_to = ?', r.id)).toBe(2);

    // Once role:principal is shared it is no longer principal-1's alias: replying as that role fails
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('principal-2', 'projB', 'principal', 'mach-B')");
    });
    expect((await reply('tok_w1', r.id, 'worker-1', 'role:principal', 'f1-x4')).status).toBe(404);
    expect((await reply('tok_w1', r.id, 'worker-1', 'principal-1', 'f1-x5')).status).toBe(400);
  });

  it('24: F5 stages advance only by the recipient (no admin override) and a stage time never moves', async () => {
    const { id } = await (await send('tok_w1', 'worker-1', 'worker-2', 'f5-key')).json() as any;
    const admin: CallerContext = { isAdmin: true, kind: 'session' };
    await runInDurableObject(stub, async (instance: Hub) => {
      expect(() => instance.busService.deliver(admin, id)).toThrow('unauthorized');
    });
    expect((await http('GET', '/api/bus/inbox?actor=worker-2', 'tok_host_b')).status).toBe(200);

    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = instance.busService;
      for (const stage of ['ack', 'accept', 'complete'] as const) {
        expect(() => bus[stage](admin, id)).toThrow('unauthorized');
        expect(() => bus[stage]({ isAdmin: true, actor: 'worker-1' }, id)).toThrow('unauthorized');
      }
    });
    let r = await row(id);
    expect([r.acked_at, r.accepted_at, r.completed_at]).toEqual([null, null, null]);

    expect((await http('POST', `/api/bus/messages/${id}/ack`, 'tok_host_b')).status).toBe(200);
    const ackedAt = (await row(id)).acked_at;
    expect(ackedAt).toBeTruthy();

    // Repeat ACK over HTTP and at a later explicit time: 200 / no-op, acked_at unchanged
    expect((await http('POST', `/api/bus/messages/${id}/ack`, 'tok_w2q')).status).toBe(200);
    await runInDurableObject(stub, async (instance: Hub) => {
      const recipient: CallerContext = { kind: 'host', machineId: 'mach-B' };
      instance.busService.ack(recipient, id, ackedAt + 60000);
      instance.busService.accept(recipient, id, ackedAt + 1000);
      instance.busService.accept(recipient, id, ackedAt + 90000);
      instance.busService.complete(recipient, id, ackedAt + 2000);
      instance.busService.complete(recipient, id, ackedAt + 99000);
    });
    r = await row(id);
    expect(r.acked_at).toBe(ackedAt);
    expect(r.accepted_at).toBe(ackedAt + 1000);
    expect(r.completed_at).toBe(ackedAt + 2000);
  });

  it('25: F6 root role refusal is case-insensitive and covers root-prefixed roles', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('boss-1', 'projA', 'ROOT', 'mach-A')");
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('boss-2', 'projB', 'rootish', 'mach-B')");
    });
    for (const to of ['role:ROOT', 'role:Root', 'role:rootish', 'role:ROOTISH', 'ROLE:root', 'Role:Root']) {
      expect([to, (await send('tok_w1', 'worker-1', to, `f6-${to}`)).status]).toEqual([to, 404]);
    }
    expect(await count('messages')).toBe(0);
    expect((await http('GET', `/api/bus/inbox?actor=${encodeURIComponent('role:ROOT')}`, 'tok_host_a')).status).toBe(403);
    expect((await http('GET', `/api/bus/inbox?actor=${encodeURIComponent('role:rootish')}`, 'tok_host_b')).status).toBe(403);
    expect((await http('POST', '/api/bus/claims', 'tok_host_a', { project: 'projA', scope: 'src', holder: 'role:ROOT' })).status).toBe(403);
    expect(await count('claims')).toBe(0);

    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      for (const a of ['role:ROOT', 'role:rootish', 'ROLE:root']) {
        expect(bus.validateRecipient(a)).toBe(false);
        expect(bus.isAuthorizedForActor({ isAdmin: true }, a)).toBe(false);
        expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-A' }, a)).toBe(false);
        expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-B' }, a)).toBe(false);
        expect(() => bus.send({ isAdmin: true }, { senderActor: a, toActor: 'worker-2', kind: 'note', body: 'b', idempotencyKey: `s-${a}` })).toThrow(/missing_m4_root_authority/);
        expect(() => bus.inbox({ isAdmin: true }, a)).toThrow(/missing_m4_root_authority/);
        expect(() => bus.claim({ isAdmin: true }, 'projA', 'src', a, 60000)).toThrow(/missing_m4_root_authority/);
        expect(() => bus.listClaims({ actor: a })).toThrow(/missing_m4_root_authority/);
        expect(() => bus.getBusActors({ actor: a })).toThrow(/missing_m4_root_authority/);
      }
      // A root-role agent's own tag inbox never gains a role:* alias
      expect(bus.inbox({ kind: 'host', machineId: 'mach-B' }, 'boss-2')).toEqual([]);
    });
    expect(await count('messages')).toBe(0);
  });

  it('26: F4 claim holders and caller actors follow the address grammar; nothing malformed is stored', async () => {
    for (const holder of ['projA:worker-1:extra', 'projA:worker-1:', ':worker-1', 'projA::worker-1', 'worker-1 ', 'role:worker', 'role:principal', 'machine:mach-A', 'unknown-agent', 'projB:worker-2']) {
      const res = await http('POST', '/api/bus/claims', 'tok_host_a', { project: 'projA', scope: 'src/bus.ts', holder });
      expect([holder, res.status]).toEqual([holder, 403]);
    }
    expect(await count('claims')).toBe(0);

    // The real holder is not blocked by a stored malformed twin
    const ok = await http('POST', '/api/bus/claims', 'tok_w1', { project: 'projA', scope: 'src/bus.ts', holder: 'projA:worker-1' });
    expect(ok.status).toBe(200);
    const { id } = await ok.json() as any;

    await runInDurableObject(stub, async (instance: Hub) => {
      const bus = new BusService(instance.sql);
      const bad: CallerContext = { actor: 'projA:worker-1:x' };
      expect(() => bus.claim(bad, 'projA', 'docs', 'worker-1', 60000)).toThrow('unauthorized');
      expect(() => bus.claim(bad, 'projA', 'docs', 'projA:worker-1:x', 60000)).toThrow('unauthorized');
      expect(() => bus.claim({ isAdmin: true }, 'projA', 'docs', 'projA:worker-1:x', 60000)).toThrow('unauthorized');
      expect(() => bus.releaseClaim(bad, id)).toThrow('unauthorized');
      expect(bus.listClaims(bad)).toEqual([]);
      expect(() => bus.listClaims(bad, 'projA')).toThrow('unauthorized');
      expect(bus.isAuthorizedForActor(bad, 'projA:worker-1')).toBe(false);
      expect(bus.isAuthorizedForActor(bad, 'projA:worker-1:x')).toBe(false);
      expect(bus.getBusActors(bad)).toEqual([]);
    });
    expect(await count('claims')).toBe(1);
    expect((await http('POST', `/api/bus/claims/${id}/release`, 'tok_host_a')).status).toBe(200);
  });

  it('27: F7 duplicate tag across projects: bare tag is ambiguous, qualified forms stay separate identities', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      // The Hub schema keys agents by tag; the bus schema does not, so pin the service over a no-PK table
      instance.sql.exec('DROP TABLE IF EXISTS bus_agents_dup');
      instance.sql.exec('CREATE TABLE bus_agents_dup (tag TEXT NOT NULL, project TEXT NOT NULL, role TEXT, machine_id TEXT, registered_at TEXT)');
      for (const [tag, project, role, mach] of [['dup', 'projA', 'dup-a', 'mach-A'], ['dup', 'projB', 'dup-b', 'mach-B'], ['worker-1', 'projA', 'worker', 'mach-A']]) {
        instance.sql.exec('INSERT INTO bus_agents_dup (tag, project, role, machine_id) VALUES (?, ?, ?, ?)', tag, project, role, mach);
      }
      const sql = { exec: (q: string, ...p: any[]) => instance.sql.exec(q.replace(/\bagents\b/g, 'bus_agents_dup'), ...p) };
      const bus = new BusService(sql as any);
      const a: CallerContext = { actor: 'projA:dup' };
      const b: CallerContext = { actor: 'projB:dup' };

      expect(bus.validateRecipient('dup')).toBe(false);
      expect(bus.validateRecipient('projA:dup')).toBe(true);
      expect(bus.validateRecipient('projB:dup')).toBe(true);
      expect(bus.validateRecipient('role:dup-b')).toBe(true);
      expect(bus.isAuthorizedForActor(a, 'projB:dup')).toBe(false);
      expect(bus.isAuthorizedForActor(a, 'dup')).toBe(false);
      expect(bus.isAuthorizedForActor(a, 'role:dup-b')).toBe(false);
      expect(bus.isAuthorizedForActor(b, 'role:dup-b')).toBe(true);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-A' }, 'projB:dup')).toBe(false);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-B' }, 'projB:dup')).toBe(true);
      expect(bus.isAuthorizedForActor({ kind: 'host', machineId: 'mach-B' }, 'dup')).toBe(false);

      const w1: CallerContext = { actor: 'worker-1' };
      expect(() => bus.send(w1, { senderActor: 'worker-1', toActor: 'dup', kind: 'note', body: 'b', idempotencyKey: 'd0' })).toThrow('unknown_recipient');
      const m = bus.send(w1, { senderActor: 'worker-1', toActor: 'projB:dup', kind: 'note', body: 'b', idempotencyKey: 'd1' });
      expect(() => bus.inbox(a, 'projB:dup')).toThrow('unauthorized');
      expect(bus.inbox(a, 'projA:dup')).toEqual([]);
      expect(bus.inbox(b, 'role:dup-b').map((x: any) => x.id)).toEqual([m.id]);

      // Only projB:dup (in any of its own alias forms) may reply; projA:dup and the bare tag may not
      expect(() => bus.send(a, { senderActor: 'projA:dup', toActor: 'worker-1', kind: 'reply', body: 'r', idempotencyKey: 'd2', replyTo: m.id })).toThrow('invalid_reply_to');
      expect(() => bus.send(b, { senderActor: 'dup', toActor: 'worker-1', kind: 'reply', body: 'r', idempotencyKey: 'd3', replyTo: m.id })).toThrow('unauthorized');
      expect(bus.send(b, { senderActor: 'role:dup-b', toActor: 'projA:worker-1', kind: 'reply', body: 'r', idempotencyKey: 'd4', replyTo: m.id }).thread_id).toBe(m.thread_id);

      // A bare ambiguous holder is refused for agent and host callers alike
      expect(() => bus.claim(b, 'projB', 'src', 'dup', 60000)).toThrow('unauthorized');
      expect(() => bus.claim({ kind: 'host', machineId: 'mach-B' }, 'projB', 'src', 'dup', 60000)).toThrow('unauthorized');
      expect(() => bus.claim(a, 'projB', 'src', 'projB:dup', 60000)).toThrow('unauthorized');
      expect(bus.claim(b, 'projB', 'src', 'projB:dup', 60000).id).toBeTruthy();
      expect(bus.listClaims(a)).toEqual([]);
      expect(bus.listClaims(b).map((c: any) => c.holder)).toEqual(['projB:dup']);
      instance.sql.exec('DROP TABLE bus_agents_dup');
    });
  });
});
