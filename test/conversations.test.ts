import { describe, it, expect, beforeEach } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { SCHEMA_SQL } from '../src/schema';
import {
  ConversationsService,
  CONVERSATIONS_SCHEMA_SQL,
  authenticateConversationRequest,
  createSqlIdentityValidator,
  IdentityValidator,
} from '../src/conversations';
import {
  createSessionCookie,
  TOKEN_ADMIN_EMAIL,
  tokenAdminSubject,
} from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const testEnv = env as unknown as Env;

describe('M1 Conversations Domain Service & HTTP Route Contract', () => {
  let stub: DurableObjectStub<Hub>;
  let clockMs = 1792189200000;

  const humanAlice = {
    caller: { actor: 'human:user-alice', kind: 'human' as const, isAdmin: true },
    scopes: ['bus:read', 'bus:send'],
    authType: 'session' as const,
    csrfVerified: true,
  };

  const humanBob = {
    caller: { actor: 'human:user-bob', kind: 'human' as const, isAdmin: true },
    scopes: ['bus:read', 'bus:send'],
    authType: 'session' as const,
    csrfVerified: true,
  };

  const agentWorker1 = {
    caller: { actor: 'projA:worker-1', kind: 'agent' as const },
    scopes: ['bus:read', 'bus:send'],
    authType: 'bearer' as const,
    csrfVerified: true,
  };

  const agentWorker2 = {
    caller: { actor: 'projB:worker-2', kind: 'agent' as const },
    scopes: ['bus:read', 'bus:send'],
    authType: 'bearer' as const,
    csrfVerified: true,
  };

  function makeReq(
    path: string,
    options?: {
      method?: string;
      body?: unknown;
      headers?: Record<string, string>;
    },
  ): Request {
    const method =
      options?.method ?? (options?.body !== undefined ? 'POST' : 'GET');
    const headers: Record<string, string> = {
      ...(options?.headers ?? {}),
    };
    let rawBody: string | undefined;
    if (options?.body !== undefined) {
      rawBody =
        typeof options.body === 'string'
          ? options.body
          : JSON.stringify(options.body);
      if (!headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
      }
    }
    return new Request(`https://hub.test${path}`, {
      method,
      headers,
      body: rawBody,
    });
  }

  beforeEach(async () => {
    clockMs = 1792189200000;
    const id = testEnv.HUB.idFromName('conv-test-' + crypto.randomUUID());
    stub = testEnv.HUB.get(id);

    await runInDurableObject(stub, async (hub: Hub) => {
      for (const stmt of SCHEMA_SQL.split(';')) {
        if (stmt.trim()) hub.sql.exec(stmt);
      }
      for (const stmt of CONVERSATIONS_SCHEMA_SQL.split(';')) {
        if (stmt.trim()) hub.sql.exec(stmt);
      }

      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'user-alice',
        'admin@fake.local',
        nowIso,
        nowIso,
      );
      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'user-bob',
        'another@fake.local',
        nowIso,
        nowIso,
      );

      hub.sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at) VALUES ('machine-A', 'Machine A', 'host-a', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at) VALUES ('machine-B', 'Machine B', 'host-b', ?)",
        nowIso,
      );

      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, registered_at, workspace, session_id) VALUES ('worker-1', 'projA', 'implementer', 'machine-A', ?, '/workspace/projA', '11111111-1111-1111-1111-111111111111')",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, registered_at, workspace, session_id) VALUES ('worker-2', 'projB', 'reviewer', 'machine-B', ?, '/workspace/projB', '22222222-2222-2222-2222-222222222222')",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('lead-1', 'projA', 'head', 'machine-A', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES ('root-agent', 'projA', 'root', 'machine-A', ?)",
        nowIso,
      );
    });
  });

  it('1: rejects unauthenticated, collapsed admin, host, job, routing-only, root, and human-bearer principals before content access', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Unauthenticated -> 401
      const unauthRes = await svc.handle(makeReq('/api/conversations'), {});
      expect(unauthRes.status).toBe(401);

      // Revoked -> 401
      const revokedRes = await svc.handle(makeReq('/api/conversations'), {
        caller: {
          actor: 'projA:worker-1',
          kind: 'agent',
          isRevoked: true,
        },
        scopes: ['bus:read', 'bus:send'],
      });
      expect(revokedRes.status).toBe(401);

      // Collapsed 'admin' from legacy authenticateBusRequest -> 403 collapsed_admin_identity_refused
      const collapsedAdminRes = await svc.handle(
        makeReq('/api/conversations'),
        {
          caller: { isAdmin: true, actor: 'admin', kind: 'human' },
          scopes: ['admin', 'bus:read', 'bus:send'],
          csrfVerified: true,
        },
      );
      expect(collapsedAdminRes.status).toBe(403);
      expect(await collapsedAdminRes.json()).toEqual({
        error: 'forbidden',
        reason: 'collapsed_admin_identity_refused',
      });

      // Host credential -> 403 invalid_principal_kind
      const hostRes = await svc.handle(makeReq('/api/conversations'), {
        caller: { kind: 'host', machineId: 'machine-A' },
        scopes: ['host', 'bus:read', 'bus:send'],
      });
      expect(hostRes.status).toBe(403);
      expect(await hostRes.json()).toEqual({
        error: 'forbidden',
        reason: 'invalid_principal_kind',
      });

      // Job credential -> 403 invalid_principal_kind
      const jobRes = await svc.handle(makeReq('/api/conversations'), {
        caller: { kind: 'job', actor: 'job-123', jobId: 'job-123' },
        scopes: ['lease:root', 'bus:read', 'bus:send'],
      });
      expect(jobRes.status).toBe(403);
      expect(await jobRes.json()).toEqual({
        error: 'forbidden',
        reason: 'invalid_principal_kind',
      });

      // Routing-only client credential -> 403 invalid_principal_kind
      const routingClientRes = await svc.handle(
        makeReq('/api/conversations'),
        {
          caller: { kind: 'agent', actor: 'routing-client:grok-bot' },
          scopes: ['routing:submit'],
        },
      );
      expect(routingClientRes.status).toBe(403);
      expect(await routingClientRes.json()).toEqual({
        error: 'forbidden',
        reason: 'invalid_principal_kind',
      });

      // Agent with routing:submit scope only -> 403 invalid_principal_kind
      const routingScopeOnlyRes = await svc.handle(
        makeReq('/api/conversations'),
        {
          caller: { kind: 'agent', actor: 'projA:worker-1' },
          scopes: ['routing:submit'],
        },
      );
      expect(routingScopeOnlyRes.status).toBe(403);

      // role:root -> 403 invalid_principal_kind
      const rootRoleRes = await svc.handle(makeReq('/api/conversations'), {
        caller: { kind: 'agent', actor: 'role:root' },
        scopes: ['bus:read', 'bus:send'],
      });
      expect(rootRoleRes.status).toBe(403);

      // Human bearer credential -> 403 invalid_principal_kind
      const humanBearerRes = await svc.handle(makeReq('/api/conversations'), {
        caller: {
          kind: 'human',
          actor: 'human:user-alice',
          tokenId: 'cred-1',
        },
        authType: 'bearer',
        scopes: ['admin', 'bus:read', 'bus:send'],
      });
      expect(humanBearerRes.status).toBe(403);

      // Unregistered agent -> 403 unregistered_agent_principal
      const unknownAgentRes = await svc.handle(makeReq('/api/conversations'), {
        caller: { kind: 'agent', actor: 'projA:unknown-worker' },
        scopes: ['bus:read', 'bus:send'],
      });
      expect(unknownAgentRes.status).toBe(403);
      expect(await unknownAgentRes.json()).toEqual({
        error: 'forbidden',
        reason: 'unregistered_agent_principal',
      });

      // Read-only agent cannot create conversation -> 403 scope_missing
      const readOnlyAgentWrite = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'direct',
            title: 'Test',
            members: ['human:user-alice'],
          },
        }),
        {
          caller: { kind: 'agent', actor: 'projA:worker-1' },
          scopes: ['bus:read'],
        },
      );
      expect(readOnlyAgentWrite.status).toBe(403);
      expect(await readOnlyAgentWrite.json()).toEqual({
        error: 'forbidden',
        reason: 'scope_missing',
      });
    });
  });

  it('2: enforces CSRF disposition on session principals and authenticates session/token-admin/agent via authenticateConversationRequest', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Mutating request with csrfVerified: false is rejected before reading malformed body
      const csrfFail = await svc.handle(
        makeReq('/api/conversations', {
          method: 'POST',
          body: '{not-valid-json',
        }),
        {
          ...humanAlice,
          csrfVerified: false,
        },
      );
      expect(csrfFail.status).toBe(403);
      expect(await csrfFail.json()).toEqual({
        error: 'csrf_invalid',
        reason: 'csrf_token_missing_or_invalid',
      });

      // End-to-end session cookie + verifyCsrf helper via authenticateConversationRequest
      const expSec = Math.floor(Date.now() / 1000) + 3600;
      const cookieHeader = await createSessionCookie(testEnv, {
        sub: 'user-alice',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'csrf-secret-123',
        exp: expSec,
      });
      const cookiePair = cookieHeader.split(';')[0];

      // GET with session cookie (no CSRF header needed) and spoofed X-Actor header
      const getReq = makeReq('/api/conversations/participants', {
        headers: {
          Cookie: cookiePair,
          'X-Actor': 'human:spoofed-actor',
          'X-User': 'human:spoofed-user',
        },
      });
      const getAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        getReq,
      );
      const getRes = await svc.handle(getReq, getAuth);
      expect(getRes.status).toBe(200);
      const getJson = (await getRes.json()) as any;
      expect(getJson.self.actor).toBe('human:user-alice');

      // POST with session cookie but missing X-CSRF-Token -> 403 csrf_invalid
      const postNoCsrf = makeReq('/api/conversations', {
        headers: { Cookie: cookiePair },
        body: {
          kind: 'group',
          title: 'CSRF Test',
          members: ['projA:worker-1'],
        },
      });
      const postNoCsrfAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        postNoCsrf,
      );
      const postNoCsrfRes = await svc.handle(postNoCsrf, postNoCsrfAuth);
      expect(postNoCsrfRes.status).toBe(403);
      expect((await postNoCsrfRes.json() as any).error).toBe('csrf_invalid');

      // POST with cross-origin Origin header -> 403 csrf_invalid
      const postBadOrigin = makeReq('/api/conversations', {
        headers: {
          Cookie: cookiePair,
          'X-CSRF-Token': 'csrf-secret-123',
          Origin: 'https://evil.example',
        },
        body: {
          kind: 'group',
          title: 'CSRF Test',
          members: ['projA:worker-1'],
        },
      });
      const postBadOriginAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        postBadOrigin,
      );
      expect((await svc.handle(postBadOrigin, postBadOriginAuth)).status).toBe(
        403,
      );

      // POST with valid X-CSRF-Token and matching Origin -> 201 Created
      const postOk = makeReq('/api/conversations', {
        headers: {
          Cookie: cookiePair,
          'X-CSRF-Token': 'csrf-secret-123',
          Origin: 'https://hub.test',
        },
        body: {
          kind: 'group',
          title: 'CSRF Verified Group',
          members: ['projA:worker-1'],
        },
      });
      const postOkAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        postOk,
      );
      const postOkRes = await svc.handle(postOk, postOkAuth);
      expect(postOkRes.status).toBe(201);
      const created = (await postOkRes.json()) as any;
      expect(created.created_by).toBe('human:user-alice');

      // Token-admin session shares one explicit deterministic operator identity
      const tokenHash = 'a'.repeat(64);
      const envWithToken: Env = {
        ...testEnv,
        HUB_ADMIN_TOKEN_SHA256: tokenHash,
      };
      const opSub = tokenAdminSubject(tokenHash);
      const opCookie = (
        await createSessionCookie(envWithToken, {
          sub: opSub,
          email: TOKEN_ADMIN_EMAIL,
          isAdmin: true,
          csrfToken: 'op-csrf',
          exp: expSec,
        })
      ).split(';')[0];

      const opReq = makeReq('/api/conversations/participants', {
        headers: { Cookie: opCookie },
      });
      const opAuth = await authenticateConversationRequest(
        envWithToken,
        hub.sql,
        opReq,
      );
      const opRes = await svc.handle(opReq, opAuth);
      expect(opRes.status).toBe(200);
      const opData = (await opRes.json()) as any;
      expect(opData.self.actor).toBe(`human:${opSub}`);

      // Agent bearer token authentication via authenticateConversationRequest
      const rawAgentToken = 'hubt_agent_worker_1_secret_token_value';
      const nowIso = new Date().toISOString();
      const futureIso = new Date(Date.now() + 86400000).toISOString();
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at)
         VALUES ('cred-w1', 'agent', ?, 'fam-1', 'worker-1', 'projA:worker-1', 'machine-A', '["bus:read","bus:send"]', 'user-alice', ?, ?)`,
        await sha256(rawAgentToken),
        nowIso,
        futureIso,
      );

      const agentReq = makeReq(`/api/conversations/${created.id}`, {
        headers: { Authorization: `Bearer ${rawAgentToken}` },
      });
      const agentAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        agentReq,
      );
      const agentRes = await svc.handle(agentReq, agentAuth);
      expect(agentRes.status).toBe(200);
      const agentConv = (await agentRes.json()) as any;
      expect(agentConv.self_actor).toBe('projA:worker-1');
    });
  });

  it('3: tests real SCHEMA_SQL /api/agents contract, fails closed on cross-project identity mismatch, and isolates multi-project same-tag validators (F7)', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      // 1. Verify real SCHEMA_SQL + actual BaseHub /api/agents entrypoint (no DROP TABLE agents):
      // Registering 'shared-tag' in 'projA' succeeds (201); registering 'shared-tag' in 'projB'
      // succeeds against the migrated project/tag uniqueness constraint.
      const expSec = Math.floor(Date.now() / 1000) + 3600;
      const adminCookie = (
        await createSessionCookie(testEnv, {
          sub: 'user-alice',
          email: 'admin@fake.local',
          isAdmin: true,
          csrfToken: 'f7-csrf',
          exp: expSec,
        })
      ).split(';')[0];

      const regProjA = await hub.fetch(
        new Request('https://hub.example/api/agents', {
          method: 'POST',
          headers: {
            Cookie: adminCookie,
            'X-CSRF-Token': 'f7-csrf',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tag: 'shared-tag',
            project: 'projA',
            role: 'worker',
          }),
        }),
      );
      expect(regProjA.status).toBe(201);

      const regProjBOnMigratedSchema = await hub.fetch(
        new Request('https://hub.example/api/agents', {
          method: 'POST',
          headers: {
            Cookie: adminCookie,
            'X-CSRF-Token': 'f7-csrf',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tag: 'shared-tag',
            project: 'projB',
            role: 'worker',
          }),
        }),
      );
      // Same tag in two real projects is supported by the production schema.
      expect(regProjBOnMigratedSchema.status).toBe(201);

      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Against real SCHEMA_SQL where 'shared-tag' belongs to 'projA', any reference or credential
      // for 'projB:shared-tag' MUST fail closed (never conflating with 'projA:shared-tag')
      expect(
        svc.identityValidator.resolveParticipant('projA:shared-tag')?.actor,
      ).toBe('projA:shared-tag');
      expect(
        svc.identityValidator.resolveParticipant('projB:shared-tag')?.actor,
      ).toBe('projB:shared-tag');
      expect(svc.identityValidator.resolveParticipant('projC:shared-tag')).toBeNull();

      const convRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'direct',
            title: 'ProjA Only',
            members: ['projA:shared-tag'],
          },
        }),
        humanAlice,
      );
      expect(convRes.status).toBe(201);
      const conv = (await convRes.json()) as any;
      expect(conv.members.map((m: any) => m.actor)).toEqual([
        'human:user-alice',
        'projA:shared-tag',
      ]);

      // Caller claiming 'projB:shared-tag' fails closed with 403 unregistered_agent_principal
      const projBCaller = await svc.handle(
        makeReq(`/api/conversations/${conv.id}`),
        {
          caller: { actor: 'projC:shared-tag', kind: 'agent' },
          scopes: ['bus:read', 'bus:send'],
        },
      );
      expect(projBCaller.status).toBe(403);
      expect(await projBCaller.json()).toEqual({
        error: 'forbidden',
        reason: 'unregistered_agent_principal',
      });

      // 2. When an IdentityValidator exposes two projects sharing the same tag ('projA:shared-tag' and 'projB:shared-tag'),
      // bare 'shared-tag' is rejected as ambiguous while project-qualified actors remain strictly isolated.
      const baseValidator = svc.identityValidator;
      const multiProjectValidator = {
        resolveParticipant(rawActor: string, caller?: any) {
          if (rawActor === 'shared-tag') return null; // ambiguous bare tag
          if (rawActor === 'projB:shared-tag') {
            return {
              actor: 'projB:shared-tag',
              kind: 'agent' as const,
              display_name: 'projB:shared-tag',
              project: 'projB',
              role: 'worker',
              machine_id: 'machine-B',
              native_binding: {
                tag: 'shared-tag',
                project: 'projB',
                machine_id: 'machine-B',
                workspace: null,
                session_id: null,
              },
            };
          }
          return baseValidator.resolveParticipant(rawActor, caller);
        },
        listParticipants(caller?: any) {
          return [
            ...baseValidator.listParticipants(caller),
            {
              actor: 'projB:shared-tag',
              kind: 'agent' as const,
              display_name: 'projB:shared-tag',
              project: 'projB',
              role: 'worker',
              machine_id: 'machine-B',
              native_binding: {
                tag: 'shared-tag',
                project: 'projB',
                machine_id: 'machine-B',
                workspace: null,
                session_id: null,
              },
            },
          ];
        },
      };
      svc.identityValidator = multiProjectValidator;

      // Adding ambiguous bare 'shared-tag' fails with 404 unknown_participant
      const ambigCreate = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'direct',
            title: 'Ambiguous Direct',
            members: ['shared-tag'],
          },
        }),
        humanAlice,
      );
      expect(ambigCreate.status).toBe(404);

      // Calling as bare 'shared-tag' fails closed with 403 unregistered_agent_principal
      const ambigCaller = await svc.handle(makeReq('/api/conversations'), {
        caller: { actor: 'shared-tag', kind: 'agent' },
        scopes: ['bus:read', 'bus:send'],
      });
      expect(ambigCaller.status).toBe(403);

      // projA:shared-tag can read conv, while registered projB:shared-tag is refused with 403 not_a_member
      const projARead = await svc.handle(
        makeReq(`/api/conversations/${conv.id}`),
        {
          caller: { actor: 'projA:shared-tag', kind: 'agent' },
          scopes: ['bus:read', 'bus:send'],
        },
      );
      expect(projARead.status).toBe(200);

      const projBRead = await svc.handle(
        makeReq(`/api/conversations/${conv.id}`),
        {
          caller: { actor: 'projB:shared-tag', kind: 'agent' },
          scopes: ['bus:read', 'bus:send'],
        },
      );
      expect(projBRead.status).toBe(403);
      expect(await projBRead.json()).toEqual({
        error: 'forbidden',
        reason: 'not_a_member',
      });
    });
  });

  it('4: prevents cross-human admin snooping and enforces strict membership isolation', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Alice creates a direct conversation with projA:worker-1
      const createRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'direct',
            title: 'Alice & Worker-1 Private',
            members: ['worker-1'],
          },
        }),
        humanAlice,
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;

      // Alice sends a message
      const sendRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: {
            body: 'Confidential task details',
            idempotency_key: 'alice-msg-1',
          },
        }),
        humanAlice,
      );
      expect(sendRes.status).toBe(201);

      // Bob (another authenticated admin session!) lists his conversations -> empty!
      const bobListRes = await svc.handle(
        makeReq('/api/conversations'),
        humanBob,
      );
      expect(bobListRes.status).toBe(200);
      const bobList = (await bobListRes.json()) as any;
      expect(bobList.conversations).toHaveLength(0);

      // Bob cannot read Alice's conversation metadata, messages, members, or send/markRead
      for (const req of [
        makeReq(`/api/conversations/${conv.id}`),
        makeReq(`/api/conversations/${conv.id}/messages`),
        makeReq(`/api/conversations/${conv.id}/members`),
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: { body: 'Intrusion', idempotency_key: 'bob-1' },
        }),
        makeReq(`/api/conversations/${conv.id}/read`, { body: { seq: 1 } }),
      ]) {
        const res = await svc.handle(req, humanBob);
        expect(res.status).toBe(403);
        expect(await res.json()).toEqual({
          error: 'forbidden',
          reason: 'not_a_member',
        });
      }

      // Non-member agent worker-2 also gets 403 not_a_member
      const w2Res = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`),
        agentWorker2,
      );
      expect(w2Res.status).toBe(403);
    });
  });

  it('5: refuses sender impersonation in JSON and validates membership targets via injected validator', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      const conv = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Security Check',
        members: ['projA:worker-1'],
      });

      // Attempting to supply sender override fields in POST /messages returns 400 sender_override_forbidden
      for (const field of [
        'from',
        'from_actor',
        'sender',
        'sender_actor',
        'author',
      ]) {
        const res = await svc.handle(
          makeReq(`/api/conversations/${conv.id}/messages`, {
            body: {
              body: 'Spoof attempt',
              idempotency_key: 'spoof-' + field,
              [field]: 'human:user-bob',
            },
          }),
          agentWorker1,
        );
        expect(res.status).toBe(400);
        expect(await res.json()).toEqual({
          error: 'sender_override_forbidden',
        });
      }

      // bridge_ref never changes sender_actor from the verified caller
      const bridgeMsgRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: {
            body: 'Relayed from native mailbox',
            idempotency_key: 'bridge-msg-1',
            bridge_ref: 'native-uuid-1234',
          },
        }),
        agentWorker1,
      );
      expect(bridgeMsgRes.status).toBe(201);
      const bridgeMsg = (await bridgeMsgRes.json()) as any;
      expect(bridgeMsg.sender_actor).toBe('projA:worker-1');
      expect(bridgeMsg.sender_kind).toBe('agent');
      expect(bridgeMsg.bridge_ref).toBe('native-uuid-1234');

      // Fabricated human or unregistered agent in createConversation rolls back atomically
      const badHumanRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Fabricated User Test',
            members: ['projA:worker-1', 'human:invented-person'],
          },
        }),
        humanAlice,
      );
      expect(badHumanRes.status).toBe(404);
      expect((await badHumanRes.json() as any).error).toBe(
        'unknown_participant',
      );

      // Root role agent cannot be added as a participant
      const rootMemberRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Root Member Test',
            members: ['root-agent'],
          },
        }),
        humanAlice,
      );
      expect(rootMemberRes.status).toBe(404);

      // Custom injected IdentityValidator is honored
      const customValidator: IdentityValidator = {
        resolveParticipant(raw) {
          if (raw === 'custom:bot-alpha') {
            return {
              actor: 'custom:bot-alpha',
              kind: 'agent',
              display_name: 'Bot Alpha',
              native_binding: null,
            };
          }
          return createSqlIdentityValidator(hub.sql).resolveParticipant(raw);
        },
        listParticipants() {
          return [];
        },
      };
      const customSvc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { identityValidator: customValidator },
      );
      const customConv = customSvc.createConversation(humanAlice, {
        kind: 'direct',
        title: 'Custom Validator Direct',
        members: ['custom:bot-alpha'],
      });
      expect(customConv.members.map((m: any) => m.actor)).toContain(
        'custom:bot-alpha',
      );
    });
  });

  it('6: enforces owner authority on add/remove membership and revokes removed member read/send access', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Alice creates group conversation with worker-1
      const conv = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Team Room',
        members: ['worker-1'],
      });
      expect(conv.my_role).toBe('owner');

      // Non-owner worker-1 cannot add worker-2 or remove Alice -> 403 owner_required
      const nonOwnerAdd = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/members`, {
          body: { actor: 'projB:worker-2' },
        }),
        agentWorker1,
      );
      expect(nonOwnerAdd.status).toBe(403);
      expect(await nonOwnerAdd.json()).toEqual({
        error: 'forbidden',
        reason: 'owner_required',
      });

      const nonOwnerRemove = await svc.handle(
        makeReq(
          `/api/conversations/${conv.id}/members/${encodeURIComponent('human:user-alice')}`,
          { method: 'DELETE' },
        ),
        agentWorker1,
      );
      expect(nonOwnerRemove.status).toBe(403);

      // Owner Alice cannot remove herself when she is the sole owner -> 409 cannot_remove_last_owner
      const soleOwnerRemove = await svc.handle(
        makeReq(
          `/api/conversations/${conv.id}/members/${encodeURIComponent('human:user-alice')}`,
          { method: 'DELETE' },
        ),
        humanAlice,
      );
      expect(soleOwnerRemove.status).toBe(409);
      expect((await soleOwnerRemove.json() as any).error).toBe(
        'cannot_remove_last_owner',
      );

      // Owner Alice adds worker-2
      const addRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/members`, {
          body: { actor: 'worker-2' },
        }),
        humanAlice,
      );
      expect(addRes.status).toBe(201);
      const addedJson = (await addRes.json()) as any;
      expect(addedJson.member.actor).toBe('projB:worker-2');

      // worker-2 sends a message and advances its read cursor to seq 1
      const w2Send = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: {
            body: 'Hello from worker-2',
            idempotency_key: 'w2-hello',
          },
        }),
        agentWorker2,
      );
      expect(w2Send.status).toBe(201);

      // Owner Alice removes worker-2
      const remRes = await svc.handle(
        makeReq(
          `/api/conversations/${conv.id}/members/${encodeURIComponent('projB:worker-2')}`,
          { method: 'DELETE' },
        ),
        humanAlice,
      );
      expect(remRes.status).toBe(200);
      expect((await remRes.json() as any).status).toBe('removed');

      // Removed member worker-2 immediately loses list, get, history, send, and markRead access
      const w2List = (await (
        await svc.handle(makeReq('/api/conversations'), agentWorker2)
      ).json()) as any;
      expect(w2List.conversations).toHaveLength(0);

      for (const req of [
        makeReq(`/api/conversations/${conv.id}`),
        makeReq(`/api/conversations/${conv.id}/messages`),
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: { body: 'After removal', idempotency_key: 'w2-after' },
        }),
        makeReq(`/api/conversations/${conv.id}/read`, { body: { seq: 1 } }),
      ]) {
        const res = await svc.handle(req, agentWorker2);
        expect(res.status).toBe(403);
        expect(await res.json()).toEqual({
          error: 'forbidden',
          reason: 'member_removed',
        });
      }

      // Active member can inspect removed members with ?include_removed=true
      const membersWithRemoved = (await (
        await svc.handle(
          makeReq(`/api/conversations/${conv.id}/members?include_removed=true`),
          humanAlice,
        )
      ).json()) as any;
      const removedEntry = membersWithRemoved.members.find(
        (m: any) => m.actor === 'projB:worker-2',
      );
      expect(removedEntry.removed_by).toBe('human:user-alice');
      expect(removedEntry.removed_at).toBeTruthy();

      // Messages sent while worker-2 is absent advance conversation watermark to seq 3
      for (const idx of [2, 3]) {
        const absentMsgRes = await svc.handle(
          makeReq(`/api/conversations/${conv.id}/messages`, {
            body: {
              body: `Absent period message ${idx}`,
              idempotency_key: `absent-msg-${idx}`,
            },
          }),
          humanAlice,
        );
        expect(absentMsgRes.status).toBe(201);
      }

      // Re-adding worker-2 restores access and advances last_read_seq to current watermark (3)
      const reAddRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/members`, {
          body: { actor: 'projB:worker-2' },
        }),
        humanAlice,
      );
      expect(reAddRes.status).toBe(201);
      const reAdded = (await reAddRes.json()) as any;
      expect(reAdded.member.removed_at).toBeNull();
      expect(reAdded.member.last_read_seq).toBe(3);

      const w2ConvAfterReAdd = (await (
        await svc.handle(makeReq(`/api/conversations/${conv.id}`), agentWorker2)
      ).json()) as any;
      expect(w2ConvAfterReAdd.read_seq).toBe(3);
      expect(w2ConvAfterReAdd.unread_count).toBe(0);
    });
  });

  it('7: guarantees per-conversation monotonic sequences, pagination (after_seq/before_seq), idempotency, and same-conversation reply validation', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => (clockMs += 1000) },
      );
      svc.init();

      // Idempotent conversation create
      const c1Res = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Pagination & Retry Room',
            members: ['projA:worker-1', 'projB:worker-2'],
            idempotency_key: 'conv-create-key-1',
          },
        }),
        humanAlice,
      );
      expect(c1Res.status).toBe(201);
      const c1 = (await c1Res.json()) as any;
      expect(c1.replay).toBe(false);

      // Identical replay returns 200 and same conversation ID
      const c1ReplayRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Pagination & Retry Room',
            members: ['worker-2', 'worker-1'],
            idempotency_key: 'conv-create-key-1',
          },
        }),
        humanAlice,
      );
      expect(c1ReplayRes.status).toBe(200);
      const c1Replay = (await c1ReplayRes.json()) as any;
      expect(c1Replay.id).toBe(c1.id);
      expect(c1Replay.replay).toBe(true);

      // Conflicting payload with same idempotency_key returns 409 conflict
      const c1ConflictRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Different Title',
            members: ['projA:worker-1', 'projB:worker-2'],
            idempotency_key: 'conv-create-key-1',
          },
        }),
        humanAlice,
      );
      expect(c1ConflictRes.status).toBe(409);

      // Second independent conversation has its own sequence starting at 1
      const c2 = svc.createConversation(humanAlice, {
        kind: 'direct',
        title: 'Other Conversation',
        members: ['projA:worker-1'],
      });
      const c2Msg = svc.sendMessage(humanAlice, {
        conversationId: c2.id,
        body: 'Message in conversation 2',
        idempotencyKey: 'c2-msg-1',
      });
      expect(c2Msg.seq).toBe(1);

      // Send 5 messages in c1
      const sentIds: string[] = [];
      for (let i = 1; i <= 5; i++) {
        const sender = i % 2 === 1 ? humanAlice : agentWorker1;
        const res = await svc.handle(
          makeReq(`/api/conversations/${c1.id}/messages`, {
            body: {
              body: `Message #${i}`,
              idempotency_key: `c1-msg-${i}`,
              ...(i > 1 ? { reply_to: sentIds[i - 2] } : {}),
            },
          }),
          sender,
        );
        expect(res.status).toBe(201);
        const msg = (await res.json()) as any;
        expect(msg.seq).toBe(i);
        if (i > 1) {
          expect(msg.reply_to).toBe(sentIds[i - 2]);
          expect(msg.reply_to_seq).toBe(i - 1);
        }
        sentIds.push(msg.id);
      }

      // Idempotent message retry returns 200 with replay: true and does not advance last_seq
      const retryRes = await svc.handle(
        makeReq(`/api/conversations/${c1.id}/messages`, {
          body: {
            body: 'Message #5',
            idempotency_key: 'c1-msg-5',
            reply_to: sentIds[3],
          },
        }),
        humanAlice,
      );
      expect(retryRes.status).toBe(200);
      const retryMsg = (await retryRes.json()) as any;
      expect(retryMsg.id).toBe(sentIds[4]);
      expect(retryMsg.seq).toBe(5);
      expect(retryMsg.replay).toBe(true);

      // Conflicting message payload with same idempotency_key returns 409 conflict
      const conflictMsgRes = await svc.handle(
        makeReq(`/api/conversations/${c1.id}/messages`, {
          body: {
            body: 'Tampered Message #5',
            idempotency_key: 'c1-msg-5',
            reply_to: sentIds[3],
          },
        }),
        humanAlice,
      );
      expect(conflictMsgRes.status).toBe(409);

      // Reply referencing a message in a DIFFERENT conversation (c2Msg.id) is refused with 400 invalid_reply_to
      const crossConvReply = await svc.handle(
        makeReq(`/api/conversations/${c1.id}/messages`, {
          body: {
            body: 'Cross conversation reply',
            idempotency_key: 'cross-reply-1',
            reply_to: c2Msg.id,
          },
        }),
        humanAlice,
      );
      expect(crossConvReply.status).toBe(400);
      expect((await crossConvReply.json() as any).error).toBe(
        'invalid_reply_to',
      );

      // Forward pagination / reconnect via after_seq
      const page1 = (await (
        await svc.handle(
          makeReq(`/api/conversations/${c1.id}/messages?after_seq=0&limit=2`),
          agentWorker2,
        )
      ).json()) as any;
      expect(page1.messages.map((m: any) => m.seq)).toEqual([1, 2]);
      expect(page1.has_more_after).toBe(true);
      expect(page1.next_after_seq).toBe(2);
      // Reading messages does NOT implicitly advance worker-2's read cursor
      expect(page1.my_read_seq).toBe(0);
      expect(page1.unread_count).toBe(5);

      const page2 = (await (
        await svc.handle(
          makeReq(
            `/api/conversations/${c1.id}/messages?after_seq=${page1.next_after_seq}&limit=2`,
          ),
          agentWorker2,
        )
      ).json()) as any;
      expect(page2.messages.map((m: any) => m.seq)).toEqual([3, 4]);
      expect(page2.has_more_after).toBe(true);

      // Backward pagination ("load older") via before_seq
      const olderPage = (await (
        await svc.handle(
          makeReq(`/api/conversations/${c1.id}/messages?before_seq=4&limit=2`),
          agentWorker2,
        )
      ).json()) as any;
      expect(olderPage.messages.map((m: any) => m.seq)).toEqual([2, 3]);
      expect(olderPage.has_more_before).toBe(true);
      expect(olderPage.prev_before_seq).toBe(2);
    });
  });

  it('8: persists monotonic per-member read cursors and enforces explicit archive/unarchive semantics', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => (clockMs += 1000) },
      );
      svc.init();

      const conv = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Read Cursor & Archive Test',
        members: ['projA:worker-1'],
      });

      const m1 = svc.sendMessage(humanAlice, {
        conversationId: conv.id,
        body: 'First',
        idempotencyKey: 'rc-1',
      });
      const m2 = svc.sendMessage(humanAlice, {
        conversationId: conv.id,
        body: 'Second',
        idempotencyKey: 'rc-2',
      });
      const m3 = svc.sendMessage(humanAlice, {
        conversationId: conv.id,
        body: 'Third',
        idempotencyKey: 'rc-3',
      });
      expect(m3.seq).toBe(3);

      // Sender (Alice) has my_read_seq = 3, unread_count = 0
      const aliceView = svc.getConversation(humanAlice, conv.id);
      expect(aliceView.my_read_seq).toBe(3);
      expect(aliceView.unread_count).toBe(0);

      // Recipient (worker-1) has my_read_seq = 0, unread_count = 3
      const w1ViewBefore = svc.getConversation(agentWorker1, conv.id);
      expect(w1ViewBefore.my_read_seq).toBe(0);
      expect(w1ViewBefore.unread_count).toBe(3);

      // Worker-1 marks read up to message m2 by message_id (seq 2)
      const read2Res = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/read`, {
          body: { message_id: m2.id },
        }),
        agentWorker1,
      );
      expect(read2Res.status).toBe(200);
      const read2 = (await read2Res.json()) as any;
      expect(read2.read_seq).toBe(2);
      expect(read2.unread_count).toBe(1);

      // Monotonic cursor: attempting to move read_seq backward to 1 keeps read_seq at 2
      const readBackRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/read`, {
          body: { seq: 1 },
        }),
        agentWorker1,
      );
      expect(readBackRes.status).toBe(200);
      expect(((await readBackRes.json()) as any).read_seq).toBe(2);

      // Out-of-range seq (> last_seq or negative) returns 400 invalid_read_seq
      const readAheadRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/read`, {
          body: { seq: 99 },
        }),
        agentWorker1,
      );
      expect(readAheadRes.status).toBe(400);
      expect(((await readAheadRes.json()) as any).error).toBe(
        'invalid_read_seq',
      );

      // Archive semantics: non-owner cannot archive
      const nonOwnerArchive = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/archive`, { body: {} }),
        agentWorker1,
      );
      expect(nonOwnerArchive.status).toBe(403);

      // Owner archives conversation
      const archiveRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/archive`, { body: {} }),
        humanAlice,
      );
      expect(archiveRes.status).toBe(200);
      const archivedConv = (await archiveRes.json()) as any;
      expect(archivedConv.archived_at).toBeTruthy();
      expect(archivedConv.archived_by).toBe('human:user-alice');

      // Default list excludes archived conversations; include_archived=true and state=archived include it
      const activeList = svc.listConversations(agentWorker1);
      expect(activeList.conversations).toHaveLength(0);
      const allList = svc.listConversations(agentWorker1, {
        includeArchived: true,
      });
      expect(allList.conversations).toHaveLength(1);
      const archivedOnlyList = svc.listConversations(agentWorker1, {
        state: 'archived',
      });
      expect(archivedOnlyList.conversations).toHaveLength(1);

      // Active members can still read history and advance read cursor while archived
      const historyWhileArchived = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`),
        agentWorker1,
      );
      expect(historyWhileArchived.status).toBe(200);
      expect(((await historyWhileArchived.json()) as any).messages).toHaveLength(
        3,
      );

      const readWhileArchived = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/read`, { body: { seq: 3 } }),
        agentWorker1,
      );
      expect(readWhileArchived.status).toBe(200);
      expect(((await readWhileArchived.json()) as any).unread_count).toBe(0);

      // Sending messages or changing membership while archived returns 409 conversation_archived
      const sendWhileArchived = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: { body: 'Blocked while archived', idempotency_key: 'arch-1' },
        }),
        humanAlice,
      );
      expect(sendWhileArchived.status).toBe(409);
      expect(((await sendWhileArchived.json()) as any).error).toBe(
        'conversation_archived',
      );

      const addMemberWhileArchived = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/members`, {
          body: { actor: 'projB:worker-2' },
        }),
        humanAlice,
      );
      expect(addMemberWhileArchived.status).toBe(409);

      // Owner unarchives conversation -> sending works again and continues at seq 4
      const unarchiveRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/unarchive`, { body: {} }),
        humanAlice,
      );
      expect(unarchiveRes.status).toBe(200);
      expect(((await unarchiveRes.json()) as any).archived_at).toBeNull();

      const m4 = svc.sendMessage(agentWorker1, {
        conversationId: conv.id,
        body: 'Resumed after unarchive',
        idempotencyKey: 'rc-4',
      });
      expect(m4.seq).toBe(4);
      expect(m1.seq).toBe(1);
    });
  });

  it('9: enforces input bounds on body size, title, pagination limits, direct member counts, and rolls back failed transactions atomically', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => clockMs },
      );
      svc.init();

      // Invalid direct member counts (1 or 3 members) -> 400 invalid_direct_members
      expect(
        (
          await svc.handle(
            makeReq('/api/conversations', {
              body: { kind: 'direct', title: 'Solo Direct', members: [] },
            }),
            humanAlice,
          )
        ).status,
      ).toBe(400);

      expect(
        (
          await svc.handle(
            makeReq('/api/conversations', {
              body: {
                kind: 'direct',
                title: 'Three Direct',
                members: ['projA:worker-1', 'projB:worker-2'],
              },
            }),
            humanAlice,
          )
        ).status,
      ).toBe(400);

      const conv = svc.createConversation(humanAlice, {
        kind: 'direct',
        title: 'Valid Direct',
        members: ['projA:worker-1'],
      });

      // Direct conversation cannot add or remove members -> 409 direct_conversation_member_limit
      expect(
        (
          await svc.handle(
            makeReq(`/api/conversations/${conv.id}/members`, {
              body: { actor: 'projB:worker-2' },
            }),
            humanAlice,
          )
        ).status,
      ).toBe(409);

      // Message body > 16 KiB -> 413 body_too_large
      const over16kRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: {
            body: 'x'.repeat(16385),
            idempotency_key: 'oversize-16k',
          },
        }),
        humanAlice,
      );
      expect(over16kRes.status).toBe(413);

      // Request JSON > 32 KiB -> 413 body_too_large
      const over32kRes = await svc.handle(
        makeReq(`/api/conversations/${conv.id}/messages`, {
          body: JSON.stringify({
            body: 'y'.repeat(33000),
            idempotency_key: 'oversize-32k',
          }),
        }),
        humanAlice,
      );
      expect(over32kRes.status).toBe(413);

      // Invalid pagination parameters (both after_seq and before_seq, or limit > 100) -> 400
      expect(
        (
          await svc.handle(
            makeReq(
              `/api/conversations/${conv.id}/messages?after_seq=0&before_seq=2`,
            ),
            humanAlice,
          )
        ).status,
      ).toBe(400);
      expect(
        (
          await svc.handle(
            makeReq(`/api/conversations/${conv.id}/messages?limit=101`),
            humanAlice,
          )
        ).status,
      ).toBe(400);

      // Atomic rollback verification if updating conversations fails during sendMessage
      hub.sql.exec(
        "CREATE TRIGGER fail_conv_update BEFORE UPDATE OF last_seq ON conversations BEGIN SELECT RAISE(ABORT, 'simulated_db_failure'); END",
      );
      await expect(
        svc.handle(
          makeReq(`/api/conversations/${conv.id}/messages`, {
            body: {
              body: 'Should roll back completely',
              idempotency_key: 'rollback-key-1',
            },
          }),
          humanAlice,
        ),
      ).rejects.toThrow();
      hub.sql.exec('DROP TRIGGER fail_conv_update');

      const msgCount = hub.sql
        .exec(
          'SELECT COUNT(*) AS n FROM conversation_messages WHERE conversation_id = ?',
          conv.id,
        )
        .one() as any;
      expect(Number(msgCount.n)).toBe(0);
    });
  });

  it('10: supports conversation list cursor pagination, enforces 64-member group cap, and rejects expired/revoked bearer tokens', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        { now: () => (clockMs += 1000) },
      );
      svc.init();

      // Create 3 conversations at increasing timestamps
      const cA = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Conv A',
        members: ['projA:worker-1'],
      });
      const cB = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Conv B',
        members: ['projA:worker-1'],
      });
      const cC = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Conv C',
        members: ['projA:worker-1'],
      });

      // Sending a message in Conv A bumps its updated_at to the newest
      svc.sendMessage(agentWorker1, {
        conversationId: cA.id,
        body: 'Bump Conv A',
        idempotencyKey: 'bump-a',
      });

      const page1Res = await svc.handle(
        makeReq('/api/conversations?limit=2'),
        humanAlice,
      );
      expect(page1Res.status).toBe(200);
      const page1 = (await page1Res.json()) as any;
      expect(page1.conversations.map((c: any) => c.id)).toEqual([cA.id, cC.id]);
      expect(page1.next_cursor).toBeTruthy();

      const page2Res = await svc.handle(
        makeReq(
          `/api/conversations?limit=2&cursor=${encodeURIComponent(page1.next_cursor)}`,
        ),
        humanAlice,
      );
      expect(page2Res.status).toBe(200);
      const page2 = (await page2Res.json()) as any;
      expect(page2.conversations.map((c: any) => c.id)).toEqual([cB.id]);
      expect(page2.next_cursor).toBeNull();

      // Register 64 agents and test MAX_CONVERSATION_MEMBERS (64) limit
      const extraAgents: string[] = [];
      for (let i = 1; i <= 64; i++) {
        const tag = `scale-worker-${i}`;
        hub.sql.exec(
          'INSERT INTO agents (tag, project, role, machine_id) VALUES (?, ?, ?, ?)',
          tag,
          'projA',
          'worker',
          'machine-A',
        );
        extraAgents.push(`projA:${tag}`);
      }

      // Alice + 63 agents = 64 members (succeeds)
      const maxConvRes = await svc.handle(
        makeReq('/api/conversations', {
          body: {
            kind: 'group',
            title: 'Max 64 Group',
            members: extraAgents.slice(0, 63),
          },
        }),
        humanAlice,
      );
      expect(maxConvRes.status).toBe(201);
      const maxConv = (await maxConvRes.json()) as any;
      expect(maxConv.member_count).toBe(64);

      // Adding a 65th member fails with 409 member_limit_exceeded
      const overMaxAdd = await svc.handle(
        makeReq(`/api/conversations/${maxConv.id}/members`, {
          body: { actor: extraAgents[63] },
        }),
        humanAlice,
      );
      expect(overMaxAdd.status).toBe(409);
      expect(((await overMaxAdd.json()) as any).error).toBe(
        'member_limit_exceeded',
      );

      // Removing a member via POST /members/remove frees a slot
      const postRemoveRes = await svc.handle(
        makeReq(`/api/conversations/${maxConv.id}/members/remove`, {
          body: { actor: extraAgents[0] },
        }),
        humanAlice,
      );
      expect(postRemoveRes.status).toBe(200);

      // Expired and revoked bearer tokens fail with 401 via authenticateConversationRequest
      const expiredToken = 'hubt_expired_agent_token_00000000000';
      const revokedToken = 'hubt_revoked_agent_token_00000000000';
      const pastIso = new Date(clockMs - 60000).toISOString();
      const futureIso = new Date(clockMs + 86400000).toISOString();

      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('cred-exp', 'agent', ?, 'fam-e', 'worker-1', 'projA:worker-1', '["bus:read"]', 'user-alice', ?, ?)`,
        await sha256(expiredToken),
        pastIso,
        pastIso,
      );
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
         VALUES ('cred-rev', 'agent', ?, 'fam-r', 'worker-1', 'projA:worker-1', '["bus:read"]', 'user-alice', ?, ?, ?)`,
        await sha256(revokedToken),
        pastIso,
        futureIso,
        pastIso,
      );

      const expAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        makeReq('/api/conversations', {
          headers: { Authorization: `Bearer ${expiredToken}` },
        }),
        () => clockMs,
      );
      expect(
        (await svc.handle(makeReq('/api/conversations'), expAuth)).status,
      ).toBe(401);

      const revAuth = await authenticateConversationRequest(
        testEnv,
        hub.sql,
        makeReq('/api/conversations', {
          headers: { Authorization: `Bearer ${revokedToken}` },
        }),
        () => clockMs,
      );
      expect(
        (await svc.handle(makeReq('/api/conversations'), revAuth)).status,
      ).toBe(401);
    });
  });

  it('11: enforces F10 tombstone eviction on removal, F11/F15 catch-up pagination & preview clearing, F12 child reply replay after parent pruning, and F13 join-at-current-watermark', async () => {
    await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
      const svc = new ConversationsService(
        hub.sql,
        fn => state.storage.transactionSync(fn),
        {
          now: () => clockMs,
          limits: {
            messageRetentionMs: 10_000,
            removedMemberRetentionMs: 10_000,
            archivedConversationRetentionMs: 10_000,
          },
        },
      );
      svc.init();

      const conv = svc.createConversation(humanAlice, {
        kind: 'group',
        title: 'Retention & Pagination Unit Suite',
        members: ['projA:worker-1'],
      });

      // Send m1 (parent) at t0 and m2 (child reply) at t0 + 8000
      const m1 = svc.sendMessage(humanAlice, {
        conversationId: conv.id,
        body: 'Parent message body to be pruned',
        idempotencyKey: 'unit-m1',
      });
      clockMs += 8_000;
      const m2 = svc.sendMessage(agentWorker1, {
        conversationId: conv.id,
        body: 'Child reply retained after m1 expires',
        idempotencyKey: 'unit-m2',
        replyTo: m1.id,
        replyToSeq: m1.seq,
      });
      expect(m2.seq).toBe(2);

      // Advance clock by 5000ms so m1 is 13000ms old (> 10000ms) while m2 is 5000ms old
      clockMs += 5_000;
      const cleanup1 = svc.retentionCleanup(clockMs, 100);
      expect(cleanup1.messagesDeleted).toBe(1);

      // F12: Exact retries of m2 by ID, seq, or both succeed with replay: true even though m1 is pruned
      expect(
        svc.sendMessage(agentWorker1, {
          conversationId: conv.id,
          body: 'Child reply retained after m1 expires',
          idempotencyKey: 'unit-m2',
          replyTo: m1.id,
        }).replay,
      ).toBe(true);
      expect(
        svc.sendMessage(agentWorker1, {
          conversationId: conv.id,
          body: 'Child reply retained after m1 expires',
          idempotencyKey: 'unit-m2',
          replyToSeq: 1,
        }).replay,
      ).toBe(true);
      expect(
        svc.sendMessage(agentWorker1, {
          conversationId: conv.id,
          body: 'Child reply retained after m1 expires',
          idempotencyKey: 'unit-m2',
          replyTo: m1.id,
          replyToSeq: 1,
        }).replay,
      ).toBe(true);

      // F11: Polling with afterSeq=0 detects gap [1..1] and returns m2 with has_more_after=false
      const page = svc.listMessages(humanAlice, conv.id, {
        afterSeq: 0,
        limit: 10,
      });
      expect(page.messages.map((m: any) => m.seq)).toEqual([2]);
      expect(page.gap_detected).toBe(true);
      expect(page.gap_from_seq).toBe(1);
      expect(page.gap_to_seq).toBe(1);
      expect(page.pruned_through_seq).toBe(1);
      expect(page.has_more_after).toBe(false);
      expect(page.next_after_seq).toBe(2);

      // F13: Adding worker-2 now sets its last_read_seq to current watermark (2), unread_count = 0
      const addedW2 = svc.addMember(humanAlice, conv.id, 'projB:worker-2');
      expect(addedW2.member.last_read_seq).toBe(2);
      expect(svc.getConversation(agentWorker2, conv.id).unread_count).toBe(0);

      // Remove worker-2, prune its tombstone, and re-add -> still joins at current watermark (2), never resets to 0
      svc.removeMember(humanAlice, conv.id, 'projB:worker-2');
      clockMs += 15_000;
      const cleanup2 = svc.retentionCleanup(clockMs, 100);
      expect(cleanup2.membersDeleted).toBe(1);
      expect(cleanup2.messagesDeleted).toBe(1); // m2 also expired

      // F15: With all messages pruned, last_message_preview is cleared to null while last_seq remains 2
      const detailAfterAllPruned = svc.getConversation(humanAlice, conv.id);
      expect(detailAfterAllPruned.last_seq).toBe(2);
      expect(detailAfterAllPruned.last_message_preview).toBeNull();

      const readdedW2 = svc.addMember(humanAlice, conv.id, 'projB:worker-2');
      expect(readdedW2.member.last_read_seq).toBe(2);
    });
  });
});


