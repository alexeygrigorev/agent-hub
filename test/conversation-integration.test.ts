import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, ConversationHub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import { createSessionCookie, tokenAdminSubject, TOKEN_ADMIN_EMAIL } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

function extractCookiePair(setCookieHeader: string): string {
  return setCookieHeader.split(';')[0];
}

async function makeAdminSession(
  bindings: Env,
  opts: { sub: string; email: string; csrfToken: string; isAdmin?: boolean }
): Promise<{ cookie: string; csrfToken: string }> {
  const raw = await createSessionCookie(bindings, {
    sub: opts.sub,
    email: opts.email,
    isAdmin: opts.isAdmin ?? true,
    csrfToken: opts.csrfToken,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return { cookie: extractCookiePair(raw), csrfToken: opts.csrfToken };
}

async function insertBearerCredential(
  sql: any,
  opts: {
    id: string;
    token: string;
    kind: 'agent' | 'host' | 'job' | 'human';
    actor?: string | null;
    subject: string;
    machineId?: string | null;
    scopes: string[];
    expiresAt?: string;
    revokedAt?: string | null;
  }
) {
  const tokenHash = await sha256(opts.token);
  const nowIso = new Date().toISOString();
  const expiresAt = opts.expiresAt ?? new Date(Date.now() + 3600_000).toISOString();
  sql.exec(
    `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    opts.id,
    opts.kind,
    tokenHash,
    'fam-' + opts.id,
    opts.subject,
    opts.machineId ?? null,
    opts.actor ?? null,
    JSON.stringify(opts.scopes),
    'sub-owner',
    nowIso,
    expiresAt,
    opts.revokedAt ?? null
  );
}

describe('ConversationHub Durable Object Integration (src/conversation-hub.ts)', () => {
  it('preserves Hub export name, BaseHub inheritance, and shared SQLite storage authority', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-hub-meta-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_baseInstance: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      expect(Hub).toBe(ConversationHub);
      expect(Hub.name).toBe('Hub');
      expect(hub).toBeInstanceOf(BaseHub);
      expect(hub.conversationsService).toBeDefined();
      expect(hub.busService).toBeDefined();
      expect(hub.jobsService).toBeDefined();
      expect(hub.fleetService).toBeDefined();

      // Verify conversation schema tables exist alongside base Hub tables in the same SQLite database
      const tables = new Set(
        (hub.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table'").toArray() as any[]).map(
          (r) => r.name
        )
      );
      expect(tables.has('users')).toBe(true);
      expect(tables.has('agents')).toBe(true);
      expect(tables.has('messages')).toBe(true);
      expect(tables.has('conversations')).toBe(true);
      expect(tables.has('conversation_members')).toBe(true);
      expect(tables.has('conversation_messages')).toBe(true);
    });
  });

  it('enforces real session cookie authentication, CSRF verification, and per-human subject isolation through fetch()', async () => {
    const tokenHashHex = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenHashHex,
    } as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-hub-auth-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_baseInstance: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);

      const alice = await makeAdminSession(bindings, {
        sub: 'oidc-alice-001',
        email: 'admin@fake.local',
        csrfToken: 'csrf-alice-secret-token',
      });
      const bob = await makeAdminSession(bindings, {
        sub: 'oidc-bob-002',
        email: 'another@fake.local',
        csrfToken: 'csrf-bob-secret-token',
      });
      const tokenAdminSub = tokenAdminSubject(tokenHashHex);
      const tokenAdmin = await makeAdminSession(bindings, {
        sub: tokenAdminSub,
        email: TOKEN_ADMIN_EMAIL,
        csrfToken: 'csrf-token-admin',
      });

      // Register an agent via inherited BaseHub /api/agents endpoint using Alice's admin session + CSRF
      const regRes = await hub.fetch(
        new Request('https://hub.test/api/agents', {
          method: 'POST',
          headers: {
            Cookie: alice.cookie,
            'X-CSRF-Token': alice.csrfToken,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tag: 'platform-api',
            project: 'agent-hub',
            role: 'implementer',
          }),
        })
      );
      expect(regRes.status).toBe(201);

      // 1. Unauthenticated request is rejected with 401 before body read
      const unauthRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', { method: 'GET' })
      );
      expect(unauthRes.status).toBe(401);

      // 2. Session POST without CSRF header is rejected with 403 csrf_invalid
      const noCsrfRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: alice.cookie,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Should Fail CSRF',
            kind: 'direct',
            members: ['agent-hub:platform-api'],
            idempotency_key: 'ik-no-csrf',
          }),
        })
      );
      expect(noCsrfRes.status).toBe(403);
      expect(((await noCsrfRes.json()) as any).error).toBe('csrf_invalid');

      // 3. Session POST with wrong CSRF header is rejected with 403 csrf_invalid
      const badCsrfRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: alice.cookie,
            'X-CSRF-Token': 'wrong-csrf-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Should Fail CSRF',
            kind: 'direct',
            members: ['agent-hub:platform-api'],
            idempotency_key: 'ik-bad-csrf',
          }),
        })
      );
      expect(badCsrfRes.status).toBe(403);
      expect(((await badCsrfRes.json()) as any).error).toBe('csrf_invalid');

      // 4. Session POST with valid CSRF succeeds and binds created_by to human:oidc-alice-001
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: alice.cookie,
            'X-CSRF-Token': alice.csrfToken,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Alice + Platform API',
            kind: 'direct',
            members: ['agent-hub:platform-api'],
            idempotency_key: 'ik-alice-create-1',
          }),
        })
      );
      expect(createRes.status).toBe(201);
      const created = (await createRes.json()) as any;
      expect(created.conversation.created_by).toBe('human:oidc-alice-001');
      const convId = created.conversation.id;

      // 5. Bob (another admin human) and tokenAdmin cannot see or read Alice's private conversation
      const bobListRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'GET',
          headers: { Cookie: bob.cookie },
        })
      );
      expect(bobListRes.status).toBe(200);
      expect(((await bobListRes.json()) as any).conversations).toHaveLength(0);

      const bobDetailRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}`, {
          method: 'GET',
          headers: { Cookie: bob.cookie },
        })
      );
      expect(bobDetailRes.status).toBe(403);
      expect(((await bobDetailRes.json()) as any).reason).toBe('not_a_member');

      const tokenAdminDetailRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}`, {
          method: 'GET',
          headers: { Cookie: tokenAdmin.cookie },
        })
      );
      expect(tokenAdminDetailRes.status).toBe(403);
      expect(((await tokenAdminDetailRes.json()) as any).reason).toBe('not_a_member');
    });
  });

  it('supports end-to-end agent bearer messaging, read cursors, and rejects host/job/revoked credentials', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-hub-bearer-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_baseInstance: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        `INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)`,
        'sub-owner',
        'admin@fake.local',
        nowIso,
        nowIso
      );
      hub.sql.exec(
        `INSERT INTO agents (tag, project, role, registered_at) VALUES (?, ?, ?, ?)`,
        'worker-1',
        'proj-a',
        'implementer',
        nowIso
      );

      await insertBearerCredential(hub.sql, {
        id: 'cred-agent-1',
        token: 'tok-agent-worker-1',
        kind: 'agent',
        actor: 'worker-1',
        subject: 'worker-1',
        scopes: ['bus:read', 'bus:send'],
      });
      await insertBearerCredential(hub.sql, {
        id: 'cred-host-1',
        token: 'tok-host-1',
        kind: 'host',
        subject: 'hetzner',
        machineId: 'hetzner',
        scopes: ['host', 'bus:read', 'bus:send'],
      });
      await insertBearerCredential(hub.sql, {
        id: 'cred-job-1',
        token: 'tok-job-1',
        kind: 'job',
        actor: 'worker-1',
        subject: 'job-123',
        scopes: ['bus:read', 'bus:send'],
      });
      await insertBearerCredential(hub.sql, {
        id: 'cred-revoked-1',
        token: 'tok-revoked-1',
        kind: 'agent',
        actor: 'worker-1',
        subject: 'worker-1',
        scopes: ['bus:read', 'bus:send'],
        revokedAt: nowIso,
      });

      // Host, job, and revoked credentials must be rejected on /api/conversations
      for (const badToken of ['tok-host-1', 'tok-job-1']) {
        const res = await hub.fetch(
          new Request('https://hub.test/api/conversations', {
            method: 'GET',
            headers: { Authorization: `Bearer ${badToken}` },
          })
        );
        expect(res.status).toBe(403);
      }
      const revokedRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'GET',
          headers: { Authorization: `Bearer tok-revoked-1` },
        })
      );
      expect(revokedRes.status).toBe(401);

      // Agent creates conversation with human:sub-owner
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-agent-worker-1',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Agent Task Thread',
            kind: 'group',
            members: ['human:sub-owner'],
            idempotency_key: 'ik-agent-conv-1',
          }),
        })
      );
      expect(createRes.status).toBe(201);
      const convId = ((await createRes.json()) as any).conversation.id;

      // Agent sends a message; sender_actor is canonicalized to proj-a:worker-1
      const sendRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}/messages`, {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-agent-worker-1',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'M1 integration test message',
            kind: 'message',
            idempotency_key: 'ik-msg-1',
          }),
        })
      );
      expect(sendRes.status).toBe(201);
      const sent = (await sendRes.json()) as any;
      expect(sent.message.seq).toBe(1);
      expect(sent.message.sender_actor).toBe('proj-a:worker-1');

      // Re-instantiate Hub on the same DurableObjectState (simulating DO cold start) and verify persistence
      const coldHub = new Hub(state, bindings);
      const listRes = await coldHub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}/messages`, {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-agent-worker-1' },
        })
      );
      expect(listRes.status).toBe(200);
      const listed = (await listRes.json()) as any;
      expect(listed.messages).toHaveLength(1);
      expect(listed.messages[0].body).toBe('M1 integration test message');
    });
  });

  it('delegates non-conversation fetch routes, scheduledTick (pinned valid & late slots), runChecks, alarm, bound DO RPC, and Worker composition to BaseHub unchanged (F9)', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-hub-delegate-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (baseInstance: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const admin = await makeAdminSession(bindings, {
        sub: 'oidc-admin-delegate',
        email: 'admin@fake.local',
        csrfToken: 'csrf-delegate-token',
      });

      // 1. /api/healthz delegates to BaseHub
      const healthRes = await hub.fetch(
        new Request('https://hub.test/api/healthz', {
          method: 'GET',
          headers: { Cookie: admin.cookie },
        })
      );
      expect(healthRes.status).toBe(200);
      expect(await healthRes.json()).toEqual({ status: 'ok', component: 'hub-do' });

      // 2. Look-alike prefix /api/conversations-other is NOT intercepted by ConversationHub
      const otherRes = await hub.fetch(
        new Request('https://hub.test/api/conversations-other', {
          method: 'GET',
          headers: { Cookie: admin.cookie },
        })
      );
      expect(otherRes.status).toBe(404);

      // 3. Inherited RPC / cron / alarm methods work deterministically on ConversationHub:
      // Pin clock to valid slot (01:05:00Z, 5m into 01:00Z slot) -> status 'ok', matching BaseHub
      const validSlotDate = new Date('2026-10-17T01:05:00.000Z');
      (hub.jobsService as any).now = () => validSlotDate;
      (baseInstance.jobsService as any).now = () => validSlotDate;

      const tickRes = await hub.scheduledTick();
      expect(tickRes.status).toBe('ok');
      const baseValidTick = await baseInstance.scheduledTick();
      expect(baseValidTick.status).toBe('ok');

      // Pin clock to late slot in final 5 minutes of 30m window (01:26:00Z, age 1,560,000ms >= 1,500,000ms) -> status 'refused'
      const lateSlotDate = new Date('2026-10-17T01:26:00.000Z');
      (hub.jobsService as any).now = () => lateSlotDate;
      (baseInstance.jobsService as any).now = () => lateSlotDate;

      const lateTickRes = await hub.scheduledTick();
      const baseLateTickRes = await baseInstance.scheduledTick();
      expect(lateTickRes).toEqual({
        status: 'refused',
        reason: 'late_slot_refused',
        age_ms: 1560000,
        slot: '2026-10-17T01:00:00.000Z',
      });
      expect(lateTickRes).toEqual(baseLateTickRes);

      const checksRes = await hub.runChecks();
      expect(Array.isArray(checksRes)).toBe(true);

      await expect(hub.alarm()).resolves.toBeUndefined();
    });

    // 4. Exercise actual bound DO stub RPC and default Worker entrypoint composition
    const admin = await makeAdminSession(bindings, {
      sub: 'oidc-admin-delegate',
      email: 'admin@fake.local',
      csrfToken: 'csrf-delegate-token',
    });
    const rpcChecks = await (stub as any).runChecks();
    expect(Array.isArray(rpcChecks)).toBe(true);

    const workerMod = await import('../src/worker');
    const routedEnv = {
      ...bindings,
      HUB: {
        idFromName: (name: string) => bindings.HUB.idFromName(name),
        get: () => stub,
      },
    };
    const workerHealth = await workerMod.default.fetch(
      new Request('https://hub.test/healthz', {
        headers: { Cookie: admin.cookie },
      }),
      routedEnv as any,
      {} as ExecutionContext,
    );
    expect(workerHealth.status).toBe(200);
    expect(await workerHealth.json()).toEqual({ status: 'ok', component: 'hub-do' });

    await expect(
      workerMod.default.scheduled(
        { cron: '*/5 * * * *', scheduledTime: Date.now() } as ScheduledEvent,
        routedEnv as any,
        {} as ExecutionContext,
      ),
    ).resolves.toBeUndefined();
  });

  it('enforces shared global cleanup row budget across bus + conversations (F14) and backfills ee6 schema on ConversationHub init (F16)', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(
      bindings.HUB.idFromName('conv-hub-retention-f14-f16-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_baseInstance: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const admin = await makeAdminSession(bindings, {
        sub: 'oidc-admin-ret',
        email: 'admin@fake.local',
        csrfToken: 'csrf-ret-token',
      });

      // Create an archived conversation with 8 messages and 2 members (11 total conversation rows)
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: admin.cookie,
            'X-CSRF-Token': admin.csrfToken,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Archived Cleanup Target',
            kind: 'group',
            members: [],
            idempotency_key: 'ik-ret-conv-1',
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const convId = ((await createRes.json()) as any).conversation.id;

      for (let i = 1; i <= 8; i++) {
        const msgRes = await hub.fetch(
          new Request(`https://hub.test/api/conversations/${convId}/messages`, {
            method: 'POST',
            headers: {
              Cookie: admin.cookie,
              'X-CSRF-Token': admin.csrfToken,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              body: `Archived msg ${i}`,
              idempotency_key: `ik-ret-msg-${i}`,
            }),
          }),
        );
        expect(msgRes.status).toBe(201);
      }

      const archRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}/archive`, {
          method: 'POST',
          headers: {
            Cookie: admin.cookie,
            'X-CSRF-Token': admin.csrfToken,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({}),
        }),
      );
      expect(archRes.status).toBe(200);

      // Backdate archived_at by 35 days so it is expired
      const pastIso = new Date(Date.now() - 35 * 86_400_000).toISOString();
      hub.sql.exec(
        'UPDATE conversations SET archived_at = ? WHERE id = ?',
        pastIso,
        convId,
      );

      // Expired archived conversation is immediately 404 before and during chunked purge
      const detailExpired = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${convId}`, {
          headers: { Cookie: admin.cookie },
        }),
      );
      expect(detailExpired.status).toBe(404);

      // Run chained retentionCleanup with limit=4 -> deletes at most 4 child rows, keeping header until children are gone
      const step1 = hub.busService.retentionCleanup(Date.now(), 4) as any;
      expect(step1.conversationRowsDeleted).toBe(4);
      expect(step1.hasMore).toBe(true);
      expect(
        Number(
          (
            hub.sql
              .exec('SELECT COUNT(*) AS n FROM conversations WHERE id = ?', convId)
              .one() as any
          ).n,
        ),
      ).toBe(1);

      // Drain remaining rows via limit=10
      const step2 = hub.busService.retentionCleanup(Date.now(), 10) as any;
      expect(step2.conversationsDeleted).toBe(1);
      expect(step2.hasMore).toBe(false);
      expect(hub.conversationsService.getStorageUsage()).toEqual({
        conversation_count: 0,
        message_count: 0,
        message_bytes: 0,
        removed_member_count: 0,
      });
    });
  });
});

