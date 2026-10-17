import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import {
  formatHumanActorFromSub,
  parseCanonicalHumanActor,
  isDisjointAgentActor,
} from '../src/conversations';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const testEnv = env as unknown as Env;

async function makeSignedCookie(
  sub: string,
  email = 'admin@fake.local',
  csrfToken = 'csrf-sec-token',
  expOffsetSec = 3600,
): Promise<string> {
  const cookieHeader = await createSessionCookie(testEnv, {
    sub,
    email,
    isAdmin: true,
    csrfToken,
    exp: Math.floor(Date.now() / 1000) + expOffsetSec,
  });
  return cookieHeader.split(';')[0];
}

async function seedBearer(
  sql: any,
  opts: {
    id: string;
    token: string;
    actor: string;
    subject?: string;
    kind?: string;
    scopes?: string[];
    expiresAt?: string;
    revokedAt?: string | null;
  },
): Promise<string> {
  const hash = await sha256(opts.token);
  const nowIso = new Date().toISOString();
  const expiresAt =
    opts.expiresAt ?? new Date(Date.now() + 3600_000).toISOString();
  sql.exec(
    `INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    opts.id,
    opts.kind ?? 'agent',
    hash,
    'fam-' + opts.id,
    opts.subject ?? opts.actor,
    opts.actor,
    JSON.stringify(opts.scopes ?? ['bus:read', 'bus:send']),
    'alice',
    nowIso,
    expiresAt,
    opts.revokedAt ?? null,
  );
  return opts.token;
}

function createBodyGate() {
  let markPulled!: () => void;
  const pulled = new Promise<void>(resolve => {
    markPulled = resolve;
  });
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>(
    {
      start(c) {
        controller = c;
      },
      pull() {
        markPulled();
        return new Promise(() => {});
      },
    },
    { highWaterMark: 0 },
  );
  return {
    stream,
    pulled,
    release(payload: unknown) {
      controller.enqueue(new TextEncoder().encode(JSON.stringify(payload)));
      controller.close();
    },
  };
}

describe('Conversation Security Regressions (Cluster 1: F1, F3, F4)', () => {
  it('F1: keeps agent and human namespaces disjoint across all resolvers, directory listings, and membership checks (including reserved human/role/machine/routing rows)', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f1-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const nowIso = new Date().toISOString();

      // Seed imported/registered rows with reserved project/tag namespaces
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('alice', 'human', 'worker', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('ops', 'role', 'worker', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('node1', 'machine', 'worker', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot1', 'routing-client', 'worker', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('human', 'projA', 'worker', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('valid-worker', 'projA', 'worker', ?)",
        nowIso,
      );

      // Human Alice creates a private group conversation
      const aliceCookie = await makeSignedCookie('alice', 'admin@fake.local');
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: aliceCookie,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Alice Private Owner Conversation',
            members: ['projA:valid-worker'],
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const created = (await createRes.json()) as any;
      expect(created.self_actor).toBe('human:alice');

      // Agent credential with bare alias 'alice' (where agents has project='human', tag='alice')
      // must NOT canonicalize to 'human:alice' or read/write Alice's conversation!
      const tokenBareAlice = await seedBearer(hub.sql, {
        id: 'cred-f1-alice',
        token: 'tok-f1-bare-alice',
        actor: 'alice',
      });

      const spoofRead = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${created.id}`, {
          headers: { Authorization: `Bearer ${tokenBareAlice}` },
        }),
      );
      expect(spoofRead.status).toBe(403);
      expect(await spoofRead.json()).toEqual({
        error: 'forbidden',
        reason: 'unregistered_agent_principal',
      });

      const spoofWrite = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${created.id}/messages`,
          {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${tokenBareAlice}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              body: 'Attempted human impersonation',
              idempotency_key: 'f1-impersonation',
            }),
          },
        ),
      );
      expect(spoofWrite.status).toBe(403);

      // Verify directory listing excludes all reserved-namespace agent rows
      const dirRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          headers: { Cookie: aliceCookie },
        }),
      );
      expect(dirRes.status).toBe(200);
      const dir = (await dirRes.json()) as any;
      const agentActors = dir.participants
        .filter((p: any) => p.kind === 'agent')
        .map((p: any) => p.actor);
      expect(agentActors).toEqual(['projA:valid-worker']);

      // Verify resolver rejects every reserved alias and bare alias for reserved rows
      for (const forbiddenAlias of [
        'alice',
        'human:alice',
        'ops',
        'role:ops',
        'node1',
        'machine:node1',
        'bot1',
        'routing-client:bot1',
        'human',
        'projA:human',
      ]) {
        const resolved =
          hub.conversationsService.identityValidator.resolveParticipant(
            forbiddenAlias,
          );
        if (forbiddenAlias === 'human:alice') {
          expect(resolved?.kind).toBe('human');
        } else {
          expect(resolved).toBeNull();
        }
      }

      for (const reservedActor of [
        'human:alice',
        'role:ops',
        'machine:node1',
        'routing-client:bot1',
        'human',
        'projA:human',
        'admin',
        'root',
        'principal',
        'role:root',
      ]) {
        expect(isDisjointAgentActor(reservedActor)).toBe(false);
      }

      // Defense in depth: even if a corrupted conversation_members row had actor='projA:valid-worker' with kind='human',
      // an agent principal 'projA:valid-worker' (kind='agent') is refused because kind does not match.
      const c2 = hub.conversationsService.createConversation(
        {
          session: {
            sub: 'alice',
            email: 'admin@fake.local',
            isAdmin: true,
            csrfToken: 'csrf-sec-token',
            exp: Math.floor(Date.now() / 1000) + 3600,
          },
          authType: 'session',
          csrfVerified: true,
        },
        { kind: 'group', title: 'Kind Defense In Depth' },
      );
      hub.sql.exec(
        `INSERT INTO conversation_members (conversation_id, actor, kind, role, added_at, added_by, last_read_seq)
         VALUES (?, 'projA:valid-worker', 'human', 'member', ?, 'human:alice', 0)`,
        c2.id,
        nowIso,
      );
      const tokenValidWorker = await seedBearer(hub.sql, {
        id: 'cred-f1-valid',
        token: 'tok-f1-valid-worker',
        actor: 'projA:valid-worker',
      });
      const kindMismatchRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c2.id}`, {
          headers: { Authorization: `Bearer ${tokenValidWorker}` },
        }),
      );
      expect(kindMismatchRes.status).toBe(403);
      expect(await kindMismatchRes.json()).toEqual({
        error: 'forbidden',
        reason: 'not_a_member',
      });
    });
  });

  it('F3: revalidates live credential revocation, expiry, scope reduction, and session expiry after body awaits inside the synchronous transaction', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f3-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let currentMs = Date.now();
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => currentMs;

      const nowIso = new Date(currentMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );

      const aliceCookie = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        120,
      );
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: aliceCookie,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Queued Auth Race Room',
            members: ['proj:bot'],
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;

      // 1. Queued credential revocation while body stream is paused
      const tokRevoke = await seedBearer(hub.sql, {
        id: 'cred-race-revoke',
        token: 'tok-race-revoke',
        actor: 'bot',
      });
      const gate1 = createBodyGate();
      const pendingRevoke = hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${tokRevoke}` },
          body: gate1.stream,
        }),
      );
      await gate1.pulled;
      hub.sql.exec(
        'UPDATE credentials SET revoked_at = ? WHERE id = ?',
        new Date(currentMs).toISOString(),
        'cred-race-revoke',
      );
      gate1.release({
        body: 'Message after revocation',
        idempotency_key: 'race-revoke-1',
      });
      const resRevoke = await pendingRevoke;
      expect(resRevoke.status).toBe(401);

      // 2. Queued credential expiry while body stream is paused
      const tokExpire = await seedBearer(hub.sql, {
        id: 'cred-race-expire',
        token: 'tok-race-expire',
        actor: 'bot',
        expiresAt: new Date(currentMs + 10_000).toISOString(),
      });
      const gate2 = createBodyGate();
      const pendingExpire = hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${tokExpire}` },
          body: gate2.stream,
        }),
      );
      await gate2.pulled;
      currentMs += 20_000; // advance clock past token expiry
      gate2.release({
        body: 'Message after expiry',
        idempotency_key: 'race-expire-1',
      });
      const resExpire = await pendingExpire;
      expect(resExpire.status).toBe(401);

      // 3. Queued credential scope reduction (bus:send removed) while body stream is paused
      const tokScope = await seedBearer(hub.sql, {
        id: 'cred-race-scope',
        token: 'tok-race-scope',
        actor: 'bot',
        expiresAt: new Date(currentMs + 3600_000).toISOString(),
      });
      const gate3 = createBodyGate();
      const pendingScope = hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${tokScope}` },
          body: gate3.stream,
        }),
      );
      await gate3.pulled;
      hub.sql.exec(
        'UPDATE credentials SET scopes = ? WHERE id = ?',
        JSON.stringify(['bus:read']),
        'cred-race-scope',
      );
      gate3.release({
        body: 'Message after scope reduction',
        idempotency_key: 'race-scope-1',
      });
      const resScope = await pendingScope;
      expect(resScope.status).toBe(403);
      expect(await resScope.json()).toEqual({
        error: 'forbidden',
        reason: 'scope_missing',
      });

      // 4. Queued human session expiry while body stream is paused
      const gate4 = createBodyGate();
      const pendingSessionExp = hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: aliceCookie,
            'X-CSRF-Token': 'csrf-sec-token',
          },
          body: gate4.stream,
        }),
      );
      await gate4.pulled;
      currentMs += 300_000; // advance clock past session.exp
      gate4.release({
        body: 'Message after session expiry',
        idempotency_key: 'race-sess-exp-1',
      });
      const resSessionExp = await pendingSessionExp;
      expect(resSessionExp.status).toBe(401);

      // Verify zero messages were inserted across all 4 races
      const msgRows = hub.sql
        .exec(
          'SELECT * FROM conversation_messages WHERE conversation_id = ?',
          conv.id,
        )
        .toArray();
      expect(msgRows).toHaveLength(0);

      // 5. Reads also cannot trust stale auth snapshots after credential revocation
      const staleAuth = {
        caller: {
          kind: 'agent' as const,
          actor: 'proj:bot',
          tokenId: 'cred-race-revoke',
        },
        scopes: ['bus:read', 'bus:send'],
        authType: 'bearer' as const,
        csrfVerified: true,
      };
      expect(() =>
        hub.conversationsService.getConversation(staleAuth, conv.id),
      ).toThrow();
    });
  });

  it('F4: separates injective raw subject encoding from canonical human actor parsing so prefix and whitespace variants never collide', async () => {
    // Unit verification of formatHumanActorFromSub vs parseCanonicalHumanActor
    expect(formatHumanActorFromSub('alice')).toBe('human:alice');
    expect(formatHumanActorFromSub('human:alice')).toBe('human:human:alice');
    expect(formatHumanActorFromSub(' alice')).toBeNull();
    expect(formatHumanActorFromSub('alice ')).toBeNull();
    expect(formatHumanActorFromSub('human: alice')).toBeNull();
    expect(formatHumanActorFromSub('admin')).toBeNull();
    expect(formatHumanActorFromSub('human:admin')).toBeNull();

    expect(parseCanonicalHumanActor('human:alice')).toEqual({
      actor: 'human:alice',
      sub: 'alice',
    });
    expect(parseCanonicalHumanActor('human:human:alice')).toEqual({
      actor: 'human:human:alice',
      sub: 'human:alice',
    });
    expect(parseCanonicalHumanActor('alice')).toBeNull();
    expect(parseCanonicalHumanActor(' human:alice')).toBeNull();
    expect(parseCanonicalHumanActor('human:alice ')).toBeNull();

    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f4-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);

      const cookieAlice = await makeSignedCookie('alice', 'admin@fake.local');
      const cookiePrefixedAlice = await makeSignedCookie(
        'human:alice',
        'another@fake.local',
      );
      const cookieWhitespaceAlice = await makeSignedCookie(
        ' alice',
        'another@fake.local',
      );

      // Subject 'alice' creates a private group conversation
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Alice Exact Subject Conversation',
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;
      expect(conv.self_actor).toBe('human:alice');

      // Distinct verified subject 'human:alice' maps to 'human:human:alice' and gets 403 not_a_member
      const prefixedRead = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Cookie: cookiePrefixedAlice },
        }),
      );
      expect(prefixedRead.status).toBe(403);
      expect(await prefixedRead.json()).toEqual({
        error: 'forbidden',
        reason: 'not_a_member',
      });

      // Subject with whitespace ' alice' fails closed at authentication (403 invalid_session_subject)
      const whitespaceRead = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Cookie: cookieWhitespaceAlice },
        }),
      );
      expect(whitespaceRead.status).toBe(403);
      expect(await whitespaceRead.json()).toEqual({
        error: 'forbidden',
        reason: 'invalid_session_subject',
      });

      // Directory shows both distinct humans ('human:alice' and 'human:human:alice') without collision
      const dirRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          headers: { Cookie: cookiePrefixedAlice },
        }),
      );
      expect(dirRes.status).toBe(200);
      const dir = (await dirRes.json()) as any;
      expect(dir.self.actor).toBe('human:human:alice');
      const humanActors = dir.participants
        .filter((p: any) => p.kind === 'human')
        .map((p: any) => p.actor)
        .sort();
      expect(humanActors).toEqual(['human:alice', 'human:human:alice']);
    });
  });
});

describe('Conversation Security Regressions (Cluster 2: F2, F5, F8)', () => {
  it('F2: revalidates active membership inside createConversation idempotency replay so removed creators cannot read post-removal state or restore authority', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f2-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const cookieAlice = await makeSignedCookie('alice', 'admin@fake.local');
      const cookieBob = await makeSignedCookie('bob', 'another@fake.local');

      // Ensure Bob is registered in users table via an authenticated call
      const bobInit = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          headers: { Cookie: cookieBob },
        }),
      );
      expect(bobInit.status).toBe(200);

      // Alice creates a group conversation with idempotency_key 'f2-create-key'
      const createPayload = {
        kind: 'group',
        title: 'Initial Shared Room',
        members: ['human:bob'],
        idempotency_key: 'f2-create-key',
      };
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(createPayload),
        }),
      );
      expect(createRes.status).toBe(201);
      const created = (await createRes.json()) as any;

      // Active creator replay returns 200 with replay: true
      const activeReplayRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(createPayload),
        }),
      );
      expect(activeReplayRes.status).toBe(200);
      expect(((await activeReplayRes.json()) as any).replay).toBe(true);

      // Alice promotes Bob to owner, then Bob removes Alice
      const promoteRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${created.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:bob', role: 'owner' }),
        }),
      );
      expect(promoteRes.status).toBe(200);

      const removeAliceRes = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${created.id}/members/${encodeURIComponent('human:alice')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieBob,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(removeAliceRes.status).toBe(200);

      // Bob sends a confidential message after Alice's removal
      const secretSend = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${created.id}/messages`,
          {
            method: 'POST',
            headers: {
              Cookie: cookieBob,
              'X-CSRF-Token': 'csrf-sec-token',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              body: 'Post-removal confidential content',
              idempotency_key: 'f2-secret-1',
            }),
          },
        ),
      );
      expect(secretSend.status).toBe(201);

      // Removed creator Alice repeating create with same idempotency_key must get 403 member_removed
      const replayAfterRemoval = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(createPayload),
        }),
      );
      expect(replayAfterRemoval.status).toBe(403);
      const replayBody = (await replayAfterRemoval.json()) as any;
      expect(replayBody).toEqual({
        error: 'forbidden',
        reason: 'member_removed',
      });
      expect(JSON.stringify(replayBody)).not.toContain(
        'Post-removal confidential content',
      );

      // Even with a modified payload, removed creator gets 403 member_removed before payload comparison
      const replayModifiedAfterRemoval = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            ...createPayload,
            title: 'Different Title',
          }),
        }),
      );
      expect(replayModifiedAfterRemoval.status).toBe(403);

      // Verify Alice's membership remains removed in SQL
      const aliceMember = hub.sql
        .exec(
          "SELECT removed_at FROM conversation_members WHERE conversation_id = ? AND actor = 'human:alice'",
          created.id,
        )
        .toArray()[0] as any;
      expect(aliceMember.removed_at).toBeTruthy();
    });
  });

  it('F5: enforces the last-active-owner invariant across all role transitions and preserves existing role when omitted on upsert', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f5-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const nowIso = new Date().toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );
      const tokenBot = await seedBearer(hub.sql, {
        id: 'cred-f5-bot',
        token: 'tok-f5-bot',
        actor: 'proj:bot',
      });
      const cookieAlice = await makeSignedCookie('alice', 'admin@fake.local');

      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Owner Invariant Room',
            members: ['proj:bot'],
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;

      // 1. Sole owner upserting herself with role omitted preserves role='owner'
      const defaultUpsertRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:alice' }),
        }),
      );
      expect(defaultUpsertRes.status).toBe(200);
      expect(((await defaultUpsertRes.json()) as any).member.role).toBe(
        'owner',
      );

      // 2. Sole owner explicitly attempting self-demotion to role='member' is rejected with 409 cannot_remove_last_owner
      const selfDemoteRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:alice', role: 'member' }),
        }),
      );
      expect(selfDemoteRes.status).toBe(409);
      expect(await selfDemoteRes.json()).toEqual({
        error: 'cannot_remove_last_owner',
      });

      // 3. Promoting proj:bot to owner allows Alice to hand off ownership and demote herself
      const promoteBotRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'proj:bot', role: 'owner' }),
        }),
      );
      expect(promoteBotRes.status).toBe(200);
      expect(((await promoteBotRes.json()) as any).member.role).toBe('owner');

      const handoffDemoteRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:alice', role: 'member' }),
        }),
      );
      expect(handoffDemoteRes.status).toBe(200);
      expect(((await handoffDemoteRes.json()) as any).member.role).toBe(
        'member',
      );

      // 4. Now proj:bot is the sole active owner; proj:bot cannot demote itself
      const botSelfDemoteRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${tokenBot}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'proj:bot', role: 'member' }),
        }),
      );
      expect(botSelfDemoteRes.status).toBe(409);
      expect(await botSelfDemoteRes.json()).toEqual({
        error: 'cannot_remove_last_owner',
      });
    });
  });

  it('F8: advances re-added member read cursor to the current conversation watermark so absent-period messages are not unread', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f8-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const nowIso = new Date().toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );
      const tokenBot = await seedBearer(hub.sql, {
        id: 'cred-f8-bot',
        token: 'tok-f8-bot',
        actor: 'proj:bot',
      });
      const cookieAlice = await makeSignedCookie('alice', 'admin@fake.local');

      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Watermark Advancement Room',
            members: ['proj:bot'],
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;

      // Message 1 while bot is active; bot marks read at seq 1
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ body: 'Msg 1', idempotency_key: 'f8-m1' }),
        }),
      );
      const read1 = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/read`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${tokenBot}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ seq: 1 }),
        }),
      );
      expect(read1.status).toBe(200);

      // Remove bot
      const remRes = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv.id}/members/${encodeURIComponent('proj:bot')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(remRes.status).toBe(200);

      // Send messages 2 and 3 while bot is removed
      for (const n of [2, 3]) {
        const res = await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              method: 'POST',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
                'Content-Type': 'application/json',
              },
              body: JSON.stringify({
                body: `Msg ${n}`,
                idempotency_key: `f8-m${n}`,
              }),
            },
          ),
        );
        expect(res.status).toBe(201);
      }

      // Re-add bot: read cursor advances from 1 -> 3 (current watermark)
      const reAddRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'proj:bot' }),
        }),
      );
      expect(reAddRes.status).toBe(201);
      const reAdded = (await reAddRes.json()) as any;
      expect(reAdded.member.last_read_seq).toBe(3);

      // Bot sees read_seq=3 and unread_count=0
      const botConvRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Authorization: `Bearer ${tokenBot}` },
        }),
      );
      expect(botConvRes.status).toBe(200);
      const botConv = (await botConvRes.json()) as any;
      expect(botConv.read_seq).toBe(3);
      expect(botConv.unread_count).toBe(0);

      // Subsequent message 4 increments unread_count to 1
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ body: 'Msg 4', idempotency_key: 'f8-m4' }),
        }),
      );
      const botConvAfter4 = (await (
        await hub.fetch(
          new Request(`https://hub.test/api/conversations/${conv.id}`, {
            headers: { Authorization: `Bearer ${tokenBot}` },
          }),
        )
      ).json()) as any;
      expect(botConvAfter4.last_seq).toBe(4);
      expect(botConvAfter4.read_seq).toBe(3);
      expect(botConvAfter4.unread_count).toBe(1);
    });
  });
});

describe('Conversation Security Regressions (Cluster 3: F6 Bounded Storage & Retention)', () => {
  it('F6: enforces O(1) transactional storage caps, avoids duplicating message body in request_json, preserves seq on failed admission, and prunes expired state via existing scheduledTick/alarm lifecycle', async () => {
    const { BaseHub } = await import('../src/conversation-hub');
    expect(Hub.prototype.scheduledTick).toBe(BaseHub.prototype.scheduledTick);
    expect(Hub.prototype.alarm).toBe(BaseHub.prototype.alarm);

    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f6-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let currentMs = Date.parse('2026-10-17T01:05:00.000Z');
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => currentMs;
      (hub.jobsService as any).now = () => new Date(currentMs);

      // Configure tight Free-plan caps for deterministic verification
      hub.conversationsService.limits = {
        ...hub.conversationsService.limits,
        maxTotalConversations: 2,
        maxMessagesPerConversation: 2,
        maxTotalMessages: 3,
        maxBytesPerConversation: 80,
        maxTotalMessageBytes: 120,
        maxRemovedMembersPerConversation: 1,
        maxTotalRemovedMembers: 1,
        messageRetentionMs: 60_000,
        removedMemberRetentionMs: 60_000,
        archivedConversationRetentionMs: 60_000,
      };

      const nowIso = new Date(currentMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot1', 'proj', 'worker', ?), ('bot2', 'proj', 'worker', ?)",
        nowIso,
        nowIso,
      );
      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );

      // 1. Create conversation 1 (with idempotency key) and conversation 2 (reaching maxTotalConversations = 2)
      const c1Res = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Room 1',
            members: ['proj:bot1', 'proj:bot2'],
            idempotency_key: 'f6-c1',
          }),
        }),
      );
      expect(c1Res.status).toBe(201);
      const c1 = (await c1Res.json()) as any;

      const c2Res = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Room 2',
            members: ['proj:bot1'],
            idempotency_key: 'f6-c2',
          }),
        }),
      );
      expect(c2Res.status).toBe(201);
      const c2 = (await c2Res.json()) as any;

      // 3rd conversation is refused with 409 storage_limit_exceeded
      const c3Res = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Room 3 Overflow',
          }),
        }),
      );
      expect(c3Res.status).toBe(409);
      expect(await c3Res.json()).toEqual({
        error: 'storage_limit_exceeded',
        reason: 'conversation_limit_exceeded',
      });

      // Archiving c2 does NOT bypass maxTotalConversations
      const archiveC2 = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c2.id}/archive`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
          },
        }),
      );
      expect(archiveC2.status).toBe(200);

      const c3AfterArchive = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Room 3 Still Blocked',
          }),
        }),
      );
      expect(c3AfterArchive.status).toBe(409);

      // Idempotent replay of c1 still succeeds with 200 at the cap
      const c1Replay = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Room 1',
            members: ['proj:bot1', 'proj:bot2'],
            idempotency_key: 'f6-c1',
          }),
        }),
      );
      expect(c1Replay.status).toBe(200);

      // 2. Removed-member churn cap (F10): removing bot1 succeeds (removed_member_count = 1),
      // and removing bot2 at cap = 1 also succeeds by evicting the oldest tombstone (bot1)
      // while keeping removed_member_count bounded at 1.
      const remBot1 = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${c1.id}/members/${encodeURIComponent('proj:bot1')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(remBot1.status).toBe(200);

      const remBot2EvictsOldest = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${c1.id}/members/${encodeURIComponent('proj:bot2')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(remBot2EvictsOldest.status).toBe(200);
      expect(hub.conversationsService.getStorageUsage().removed_member_count).toBe(
        1,
      );

      // 3. Message byte & count admission caps, non-duplicated request_json, and seq non-advancement on failure
      const m1Body = 'First retained message (30B)!!'; // 30 bytes
      const m1Res = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: m1Body,
            idempotency_key: 'f6-m1',
          }),
        }),
      );
      expect(m1Res.status).toBe(201);
      const m1 = (await m1Res.json()) as any;
      expect(m1.seq).toBe(1);

      // Verify request_json in SQLite does NOT duplicate the message body text
      const rawMsg1 = hub.sql
        .exec(
          'SELECT body, body_bytes, request_json FROM conversation_messages WHERE id = ?',
          m1.id,
        )
        .toArray()[0] as any;
      expect(rawMsg1.body).toBe(m1Body);
      expect(Number(rawMsg1.body_bytes)).toBe(30);
      expect(rawMsg1.request_json).not.toContain(m1Body);
      expect(JSON.parse(rawMsg1.request_json).body).toBeUndefined();

      // Message exceeding maxBytesPerConversation (30 + 60 > 80) fails with 409 and does NOT advance last_seq
      const mByteOverflow = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'X'.repeat(60),
            idempotency_key: 'f6-m-overflow',
          }),
        }),
      );
      expect(mByteOverflow.status).toBe(409);
      expect(await mByteOverflow.json()).toEqual({
        error: 'storage_limit_exceeded',
        reason: 'conversation_byte_limit_exceeded',
      });

      // Next valid message (20 bytes) at t+90s gets seq = 2 (no sequence gap after failed admission!)
      currentMs += 90_000;
      const m2Res = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Second message (20B)',
            idempotency_key: 'f6-m2',
          }),
        }),
      );
      expect(m2Res.status).toBe(201);
      const m2 = (await m2Res.json()) as any;
      expect(m2.seq).toBe(2);

      // 3rd message in c1 exceeds maxMessagesPerConversation = 2 -> 409
      const m3Blocked = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Third blocked',
            idempotency_key: 'f6-m3',
          }),
        }),
      );
      expect(m3Blocked.status).toBe(409);
      expect(await m3Blocked.json()).toEqual({
        error: 'storage_limit_exceeded',
        reason: 'conversation_message_limit_exceeded',
      });

      // Idempotent retry of m2 still returns 200 at the message cap
      const m2Replay = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Second message (20B)',
            idempotency_key: 'f6-m2',
          }),
        }),
      );
      expect(m2Replay.status).toBe(200);
      expect(((await m2Replay.json()) as any).replay).toBe(true);

      // 4. Trigger inherited scheduledTick: prunes c2 (archived at t0), bot1 removal (at t0), and m1 (at t0),
      // while retaining m2 (created at t0+90s, cutoff is t0+30s).
      const tickRes = (await hub.scheduledTick(
        new Date(currentMs).toISOString(),
      )) as any;
      expect(tickRes.status).toBe('ok');
      expect(tickRes.retention.conversationsDeleted).toBe(1);
      expect(tickRes.retention.removedMembersDeleted).toBe(1);
      expect(tickRes.retention.conversationMessagesDeleted).toBe(1);

      // Verify O(1) usage counters after retention cleanup
      expect(hub.conversationsService.getStorageUsage()).toEqual({
        conversation_count: 1,
        message_count: 1,
        message_bytes: 20,
        removed_member_count: 0,
      });

      // Verify listMessages after pruning m1 (seq 1): oldest_seq=2, oldest_retained_seq=2, has_more_before=false
      const histAfterPrune = (await (
        await hub.fetch(
          new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
            headers: { Cookie: cookieAlice },
          }),
        )
      ).json()) as any;
      expect(histAfterPrune.messages.map((m: any) => m.seq)).toEqual([2]);
      expect(histAfterPrune.last_seq).toBe(2);
      expect(histAfterPrune.oldest_seq).toBe(2);
      expect(histAfterPrune.oldest_retained_seq).toBe(2);
      expect(histAfterPrune.has_more_before).toBe(false);

      // Now that m1 was pruned, sending a new message in c1 succeeds and gets monotonic seq = 3!
      const m3AfterPrune = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${c1.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Third after prune!',
            idempotency_key: 'f6-m3',
          }),
        }),
      );
      expect(m3AfterPrune.status).toBe(201);
      expect(((await m3AfterPrune.json()) as any).seq).toBe(3);
    });
  });
});

describe('Conversation Security Regressions (Cluster 4: F7 Real Registry & F9 Deterministic Slot Clock)', () => {
  it('F7: exercises real SCHEMA_SQL and /api/agents entrypoint without DROP TABLE agents, keeping cross-project same-tag registry status explicit and failing closed on project mismatch', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f7-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const cookieAlice = await makeSignedCookie('alice', 'admin@fake.local');

      // 1. Register 'shared-worker' in 'projA' via real /api/agents endpoint -> 201 Created
      const regProjA = await hub.fetch(
        new Request('https://hub.test/api/agents', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tag: 'shared-worker',
            project: 'projA',
            role: 'worker',
          }),
        }),
      );
      expect(regProjA.status).toBe(201);

      // 2. Registering the same tag 'shared-worker' in 'projB' via real /api/agents succeeds (201) under project/tag uniqueness
      // This uses the real registry migration, without replacement schema.
      const regProjB = await hub.fetch(
        new Request('https://hub.test/api/agents', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tag: 'shared-worker',
            project: 'projB',
            role: 'worker',
          }),
        }),
      );
      expect(regProjB.status).toBe(201);

      // 3. Verify that against real SCHEMA_SQL, a credential or member lookup for 'projB:shared-worker'
      // fails closed (403 unregistered_agent_principal / 404 unknown_participant) and never conflates with 'projA:shared-worker'
      const tokProjA = await seedBearer(hub.sql, {
        id: 'cred-f7-proja',
        token: 'tok-f7-proja',
        actor: 'projA:shared-worker',
      });
      const tokProjBMismatch = await seedBearer(hub.sql, {
        id: 'cred-f7-projb',
        token: 'tok-f7-projb',
        actor: 'projC:shared-worker',
      });

      const createConv = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'direct',
            title: 'ProjA Exclusive Thread',
            members: ['projA:shared-worker'],
          }),
        }),
      );
      expect(createConv.status).toBe(201);
      const conv = (await createConv.json()) as any;

      const readByProjA = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Authorization: `Bearer ${tokProjA}` },
        }),
      );
      expect(readByProjA.status).toBe(200);

      const readByProjBMismatch = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Authorization: `Bearer ${tokProjBMismatch}` },
        }),
      );
      expect(readByProjBMismatch.status).toBe(403);
      expect(await readByProjBMismatch.json()).toEqual({
        error: 'forbidden',
        reason: 'unregistered_agent_principal',
      });

      const createWithProjBMismatch = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'direct',
            title: 'Should Reject Unregistered ProjB',
            members: ['projC:shared-worker'],
          }),
        }),
      );
      expect(createWithProjBMismatch.status).toBe(404);
    });
  });

  it('F9: pins scheduledTick clock for valid-slot ok and late-slot refusal equivalence with BaseHub', async () => {
    const { BaseHub } = await import('../src/conversation-hub');
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f9-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (baseHub: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);

      // Valid slot: 01:05:00Z (5 minutes into 01:00Z window, age = 300,000ms < 1,500,000ms)
      const validTime = new Date('2026-10-17T01:05:00.000Z');
      (hub.jobsService as any).now = () => validTime;
      (baseHub.jobsService as any).now = () => validTime;

      const validHubTick = await hub.scheduledTick();
      const validBaseTick = await baseHub.scheduledTick();
      expect(validHubTick.status).toBe('ok');
      expect(validBaseTick.status).toBe('ok');

      // Late slot: 01:26:00Z (26 minutes into 01:00Z window, age = 1,560,000ms >= 1,500,000ms)
      const lateTime = new Date('2026-10-17T01:26:00.000Z');
      (hub.jobsService as any).now = () => lateTime;
      (baseHub.jobsService as any).now = () => lateTime;

      const lateHubTick = await hub.scheduledTick();
      const lateBaseTick = await baseHub.scheduledTick();
      expect(lateHubTick).toEqual({
        status: 'refused',
        reason: 'late_slot_refused',
        age_ms: 1560000,
        slot: '2026-10-17T01:00:00.000Z',
      });
      expect(lateHubTick).toEqual(lateBaseTick);
    });
  });
});

describe('Conversation Security Regressions (Cluster 5: F10-F16 Retention & Accounting Repairs)', () => {
  it('F10: default 128 per-conversation and global removed-member tombstone caps never block authorized revocation and preserve creator/re-add fences', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f10-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let clockMs = Date.now() - 65 * 24 * 60 * 60 * 1000;
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => (clockMs += 100);

      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const cookieBob = await makeSignedCookie(
        'bob',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );

      // Register bob in users and 130 agents using real SCHEMA_SQL
      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        "INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('bob', 'admin@fake.local', 1, ?, ?)",
        nowIso,
        nowIso,
      );
      for (let i = 1; i <= 130; i++) {
        hub.sql.exec(
          'INSERT INTO agents (tag, project, role, registered_at) VALUES (?, ?, ?, ?)',
          `churn-${i}`,
          'proj',
          'worker',
          nowIso,
        );
      }

      const tokenChurn1 = await seedBearer(hub.sql, {
        id: 'cred-f10-1',
        token: 'tok-f10-churn-1',
        actor: 'proj:churn-1',
      });
      const tokenChurn129 = await seedBearer(hub.sql, {
        id: 'cred-f10-129',
        token: 'tok-f10-churn-129',
        actor: 'proj:churn-129',
      });
      const tokenChurn130 = await seedBearer(hub.sql, {
        id: 'cred-f10-130',
        token: 'tok-f10-churn-130',
        actor: 'proj:churn-130',
      });

      // Create group conversation with default 128 maxRemovedMembersPerConversation
      expect(
        hub.conversationsService.limits.maxRemovedMembersPerConversation,
      ).toBe(128);
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Default 128 Churn Room',
            members: ['human:bob', 'proj:churn-129'],
            idempotency_key: 'f10-create-key',
          }),
        }),
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;

      // Send a message so last_seq = 1
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Before churn',
            idempotency_key: 'f10-msg-1',
          }),
        }),
      );

      // Add and remove 128 members (churn-1 .. churn-128), filling the default 128 tombstone cap
      for (let i = 1; i <= 128; i++) {
        const addR = await hub.fetch(
          new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
            method: 'POST',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ actor: `proj:churn-${i}` }),
          }),
        );
        expect(addR.status).toBe(201);

        const remR = await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/members/${encodeURIComponent(`proj:churn-${i}`)}`,
            {
              method: 'DELETE',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
              },
            },
          ),
        );
        expect(remR.status).toBe(200);
      }

      expect(hub.conversationsService.getStorageUsage().removed_member_count).toBe(
        128,
      );

      // Removing the 129th member (proj:churn-129) at the default 128 cap MUST succeed (200) and revoke access immediately
      const rem129Res = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv.id}/members/${encodeURIComponent('proj:churn-129')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(rem129Res.status).toBe(200);
      expect(((await rem129Res.json()) as any).status).toBe('removed');

      // Tombstone count remains bounded at 128
      const convCounterRow = hub.sql
        .exec(
          'SELECT removed_member_count FROM conversations WHERE id = ?',
          conv.id,
        )
        .toArray()[0] as any;
      expect(Number(convCounterRow.removed_member_count)).toBe(128);
      expect(hub.conversationsService.getStorageUsage().removed_member_count).toBe(
        128,
      );

      // Revoked member proj:churn-129 is immediately denied read and send access
      for (const req of [
        new Request(`https://hub.test/api/conversations/${conv.id}`, {
          headers: { Authorization: `Bearer ${tokenChurn129}` },
        }),
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          headers: { Authorization: `Bearer ${tokenChurn129}` },
        }),
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${tokenChurn129}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Unauthorized after removal at cap',
            idempotency_key: 'f10-unauth',
          }),
        }),
      ]) {
        const res = await hub.fetch(req);
        expect(res.status).toBe(403);
        expect(await res.json()).toEqual({
          error: 'forbidden',
          reason: 'member_removed',
        });
      }

      // Evicted earliest removed member (proj:churn-1) also remains denied read/send access
      const churn1Read = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          headers: { Authorization: `Bearer ${tokenChurn1}` },
        }),
      );
      expect(churn1Read.status).toBe(403);

      // Global removed-member cap: lower maxTotalRemovedMembers to 128 and remove proj:churn-130 in a second conversation
      hub.conversationsService.limits.maxTotalRemovedMembers = 128;
      const conv2Res = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Second Room At Global Cap',
            members: ['proj:churn-130'],
          }),
        }),
      );
      expect(conv2Res.status).toBe(201);
      const conv2 = (await conv2Res.json()) as any;

      const remGlobalCapRes = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv2.id}/members/${encodeURIComponent('proj:churn-130')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(remGlobalCapRes.status).toBe(200);
      expect(hub.conversationsService.getStorageUsage().removed_member_count).toBe(
        128,
      );

      const churn130Read = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv2.id}/messages`, {
          headers: { Authorization: `Bearer ${tokenChurn130}` },
        }),
      );
      expect(churn130Read.status).toBe(403);
      expect(await churn130Read.json()).toEqual({
        error: 'forbidden',
        reason: 'member_removed',
      });

      // Promote Bob to owner, demote and remove creator Alice, then evict Alice's tombstone via cap=1;
      // verify creator replay fence (F2) still returns 403 member_removed!
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:bob', role: 'owner' }),
        }),
      );
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieBob,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'human:alice', role: 'member' }),
        }),
      );
      const remAlice = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv.id}/members/${encodeURIComponent('human:alice')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieBob,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );
      expect(remAlice.status).toBe(200);

      // Force cleanup of all tombstones so Alice's tombstone row is gone
      clockMs += 31 * 24 * 60 * 60 * 1000;
      while (hub.conversationsService.retentionCleanup(clockMs, 100).hasMore) {}

      const aliceReplayAfterPurge = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Default 128 Churn Room',
            members: ['human:bob', 'proj:churn-129'],
            idempotency_key: 'f10-create-key',
          }),
        }),
      );
      expect(aliceReplayAfterPurge.status).toBe(403);
      expect(await aliceReplayAfterPurge.json()).toEqual({
        error: 'forbidden',
        reason: 'member_removed',
      });
    });
  });

  it('F11 & F15: retained-row catch-up pagination never returns empty has_more_after=true, reports gap/resume watermarks, and clears expired content previews', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f11-f15-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let clockMs = Date.now() - 65 * 24 * 60 * 60 * 1000;
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => clockMs;

      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );
      const tokenBot = await seedBearer(hub.sql, {
        id: 'cred-f11-bot',
        token: 'tok-f11-bot',
        actor: 'proj:bot',
      });

      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Pruned Pagination & Preview Room',
            members: ['proj:bot'],
          }),
        }),
      );
      const conv = (await createRes.json()) as any;

      // Send m1 and m2 at t0
      for (const idx of [1, 2]) {
        const res = await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              method: 'POST',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
                'Content-Type': 'application/json',
              },
              body: JSON.stringify({
                body: `Secret expiring message #${idx}`,
                idempotency_key: `f11-m${idx}`,
              }),
            },
          ),
        );
        expect(res.status).toBe(201);
        clockMs += 1000;
      }

      // Advance clock past 30-day message retention so all messages (seq 1..2) are pruned
      clockMs += 31 * 24 * 60 * 60 * 1000;
      hub.conversationsService.retentionCleanup(clockMs, 100);

      // 1. Fully pruned idle conversation: after_seq=0 returns messages=[], has_more_after=false, next_after_seq=2, gap_detected=true
      const emptyCatchupRes = await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv.id}/messages?after_seq=0`,
          {
            headers: { Authorization: `Bearer ${tokenBot}` },
          },
        ),
      );
      expect(emptyCatchupRes.status).toBe(200);
      const emptyCatchup = (await emptyCatchupRes.json()) as any;
      expect(emptyCatchup.messages).toEqual([]);
      expect(emptyCatchup.last_seq).toBe(2);
      expect(emptyCatchup.oldest_retained_seq).toBeNull();
      expect(emptyCatchup.newest_retained_seq).toBeNull();
      expect(emptyCatchup.pruned_through_seq).toBe(2);
      expect(emptyCatchup.resume_after_seq).toBe(2);
      expect(emptyCatchup.gap_detected).toBe(true);
      expect(emptyCatchup.gap_from_seq).toBe(1);
      expect(emptyCatchup.gap_to_seq).toBe(2);
      expect(emptyCatchup.has_more_after).toBe(false);
      expect(emptyCatchup.next_after_seq).toBe(2);
      expect(emptyCatchup.unread_count).toBe(0);

      // F15: Verify expired message preview is cleared in both GET detail and GET list while last_seq=2 is preserved
      const detailAfterPrune = (await (
        await hub.fetch(
          new Request(`https://hub.test/api/conversations/${conv.id}`, {
            headers: { Authorization: `Bearer ${tokenBot}` },
          }),
        )
      ).json()) as any;
      expect(detailAfterPrune.last_seq).toBe(2);
      expect(detailAfterPrune.last_message_preview).toBeNull();
      expect(detailAfterPrune.unread_count).toBe(0);

      const listAfterPrune = (await (
        await hub.fetch(
          new Request('https://hub.test/api/conversations', {
            headers: { Authorization: `Bearer ${tokenBot}` },
          }),
        )
      ).json()) as any;
      expect(listAfterPrune.conversations[0].last_seq).toBe(2);
      expect(listAfterPrune.conversations[0].last_message_preview).toBeNull();
      expect(listAfterPrune.conversations[0].unread_count).toBe(0);

      // 2. Resumed conversation after pruning: send m3, m4, m5 (seq 3, 4, 5)
      for (const idx of [3, 4, 5]) {
        const res = await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              method: 'POST',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
                'Content-Type': 'application/json',
              },
              body: JSON.stringify({
                body: `Retained message #${idx}`,
                idempotency_key: `f11-m${idx}`,
              }),
            },
          ),
        );
        expect(res.status).toBe(201);
        clockMs += 1000;
      }

      // Partially pruned catch-up from after_seq=0 with limit=2: returns [3, 4], has_more_after=true, next_after_seq=4, gap_detected=true (1..2)
      const page1 = (await (
        await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages?after_seq=0&limit=2`,
            {
              headers: { Authorization: `Bearer ${tokenBot}` },
            },
          ),
        )
      ).json()) as any;
      expect(page1.messages.map((m: any) => m.seq)).toEqual([3, 4]);
      expect(page1.oldest_retained_seq).toBe(3);
      expect(page1.newest_retained_seq).toBe(5);
      expect(page1.pruned_through_seq).toBe(2);
      expect(page1.gap_detected).toBe(true);
      expect(page1.gap_from_seq).toBe(1);
      expect(page1.gap_to_seq).toBe(2);
      expect(page1.has_more_after).toBe(true);
      expect(page1.next_after_seq).toBe(4);
      expect(page1.unread_count).toBe(3); // only retained messages 3, 4, 5 count as unread

      // Next page from next_after_seq=4: returns [5], has_more_after=false, next_after_seq=5, gap_detected=false
      const page2 = (await (
        await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages?after_seq=${page1.next_after_seq}&limit=2`,
            {
              headers: { Authorization: `Bearer ${tokenBot}` },
            },
          ),
        )
      ).json()) as any;
      expect(page2.messages.map((m: any) => m.seq)).toEqual([5]);
      expect(page2.gap_detected).toBe(false);
      expect(page2.has_more_after).toBe(false);
      expect(page2.next_after_seq).toBe(5);
    });
  });

  it('F12: replays retained child reply after parent message expires across ID, seq, and combined forms while refusing conflicting retries and new replies to pruned parents', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f12-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let clockMs = Date.now() - 65 * 24 * 60 * 60 * 1000;
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => clockMs;

      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );

      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Reply Idempotency Window Room',
            members: ['proj:bot'],
          }),
        }),
      );
      const conv = (await createRes.json()) as any;

      // Send parent message (seq 1) at day 0
      const parentRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Parent at day 0',
            idempotency_key: 'f12-parent',
          }),
        }),
      );
      expect(parentRes.status).toBe(201);
      const parent = (await parentRes.json()) as any;

      // Advance 20 days; send 3 child replies (by ID, by seq, and by both)
      clockMs += 20 * 24 * 60 * 60 * 1000;
      const childByIdRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by ID',
            idempotency_key: 'f12-child-id',
            reply_to: parent.id,
          }),
        }),
      );
      expect(childByIdRes.status).toBe(201);
      const childById = (await childByIdRes.json()) as any;

      clockMs += 1000;
      const childBySeqRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by seq',
            idempotency_key: 'f12-child-seq',
            reply_to_seq: parent.seq,
          }),
        }),
      );
      expect(childBySeqRes.status).toBe(201);
      const childBySeq = (await childBySeqRes.json()) as any;

      clockMs += 1000;
      const childByBothRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by both',
            idempotency_key: 'f12-child-both',
            reply_to: parent.id,
            reply_to_seq: parent.seq,
          }),
        }),
      );
      expect(childByBothRes.status).toBe(201);
      const childByBoth = (await childByBothRes.json()) as any;

      // Advance 11 more days (day 31): parent is expired (31d > 30d), children are still retained (11d < 30d)
      clockMs += 11 * 24 * 60 * 60 * 1000;
      const cleanup = hub.conversationsService.retentionCleanup(clockMs, 100);
      expect(cleanup.messagesDeleted).toBe(1);

      // 1. Retrying all 3 retained child replies succeeds with 200 and replay=true even though parent row was pruned
      const replayId = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by ID',
            idempotency_key: 'f12-child-id',
            reply_to: parent.id,
          }),
        }),
      );
      expect(replayId.status).toBe(200);
      const replayIdJson = (await replayId.json()) as any;
      expect(replayIdJson.replay).toBe(true);
      expect(replayIdJson.id).toBe(childById.id);
      expect(replayIdJson.reply_to).toBe(parent.id);
      expect(replayIdJson.reply_to_seq).toBe(1);

      const replaySeq = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by seq',
            idempotency_key: 'f12-child-seq',
            reply_to_seq: parent.seq,
          }),
        }),
      );
      expect(replaySeq.status).toBe(200);
      expect(((await replaySeq.json()) as any).id).toBe(childBySeq.id);

      const replayBoth = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Child reply by both',
            idempotency_key: 'f12-child-both',
            reply_to: parent.id,
            reply_to_seq: parent.seq,
          }),
        }),
      );
      expect(replayBoth.status).toBe(200);
      expect(((await replayBoth.json()) as any).id).toBe(childByBoth.id);

      // 2. Conflicting retries with same idempotency_key are refused with 409 idempotency_conflict
      for (const badBody of [
        {
          body: 'Different body',
          idempotency_key: 'f12-child-id',
          reply_to: parent.id,
        },
        {
          body: 'Child reply by ID',
          idempotency_key: 'f12-child-id',
          reply_to: 'different-parent-id',
        },
        {
          body: 'Child reply by ID',
          idempotency_key: 'f12-child-id',
          reply_to_seq: 99,
        },
        {
          body: 'Child reply by ID',
          idempotency_key: 'f12-child-id',
        },
      ]) {
        const conflictRes = await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              method: 'POST',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(badBody),
            },
          ),
        );
        expect(conflictRes.status).toBe(409);
        expect(await conflictRes.json()).toEqual({
          error: 'conflict',
          reason: 'idempotency_conflict',
        });
      }

      // 3. New message referencing the pruned parent fails with 400 invalid_reply_to
      const newToPrunedParent = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Brand new reply to pruned parent',
            idempotency_key: 'f12-new-child',
            reply_to: parent.id,
          }),
        }),
      );
      expect(newToPrunedParent.status).toBe(400);
      expect(await newToPrunedParent.json()).toEqual({
        error: 'invalid_reply_to',
        reason: 'reply_target_not_in_conversation',
      });
    });
  });

  it('F13: enforces consistent join-at-current-watermark for new joins and re-adds before/after tombstone cleanup while keeping history ACL separate', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f13-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let clockMs = Date.now() - 65 * 24 * 60 * 60 * 1000;
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => (clockMs += 100);

      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('readd-bot', 'proj', 'worker', ?), ('late-joiner', 'proj', 'worker', ?)",
        nowIso,
        nowIso,
      );
      const tokenReadd = await seedBearer(hub.sql, {
        id: 'cred-f13-readd',
        token: 'tok-f13-readd',
        actor: 'proj:readd-bot',
      });
      const tokenLate = await seedBearer(hub.sql, {
        id: 'cred-f13-late',
        token: 'tok-f13-late',
        actor: 'proj:late-joiner',
      });

      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'group',
            title: 'Join Watermark Room',
            members: ['proj:readd-bot'],
          }),
        }),
      );
      const conv = (await createRes.json()) as any;

      // Send m1 while readd-bot is active; readd-bot marks read at seq 1
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ body: 'M1', idempotency_key: 'f13-m1' }),
        }),
      );
      await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/read`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${tokenReadd}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ seq: 1 }),
        }),
      );

      // Remove readd-bot at t0
      await hub.fetch(
        new Request(
          `https://hub.test/api/conversations/${conv.id}/members/${encodeURIComponent('proj:readd-bot')}`,
          {
            method: 'DELETE',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
            },
          },
        ),
      );

      // Advance 31 days and prune readd-bot's tombstone via retentionCleanup
      clockMs += 31 * 24 * 60 * 60 * 1000;
      const cleanup = hub.conversationsService.retentionCleanup(clockMs, 100);
      expect(cleanup.removedMembersDeleted).toBe(1);

      // Send m2 and m3 (so last_seq = 3)
      for (const idx of [2, 3]) {
        await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              method: 'POST',
              headers: {
                Cookie: cookieAlice,
                'X-CSRF-Token': 'csrf-sec-token',
                'Content-Type': 'application/json',
              },
              body: JSON.stringify({
                body: `M${idx}`,
                idempotency_key: `f13-m${idx}`,
              }),
            },
          ),
        );
      }

      // 1. Re-adding readd-bot AFTER tombstone cleanup sets last_read_seq = 3 and unread_count = 0 (never resets to 0!)
      const readdRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'proj:readd-bot' }),
        }),
      );
      expect(readdRes.status).toBe(201);
      expect(((await readdRes.json()) as any).member.last_read_seq).toBe(3);

      const readdDetail = (await (
        await hub.fetch(
          new Request(`https://hub.test/api/conversations/${conv.id}`, {
            headers: { Authorization: `Bearer ${tokenReadd}` },
          }),
        )
      ).json()) as any;
      expect(readdDetail.read_seq).toBe(3);
      expect(readdDetail.unread_count).toBe(0);

      // 2. Adding brand-new late-joiner also initializes last_read_seq = 3 (unread_count = 0) while allowing full retained history read
      const lateJoinRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${conv.id}/members`, {
          method: 'POST',
          headers: {
            Cookie: cookieAlice,
            'X-CSRF-Token': 'csrf-sec-token',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ actor: 'proj:late-joiner' }),
        }),
      );
      expect(lateJoinRes.status).toBe(201);
      expect(((await lateJoinRes.json()) as any).member.last_read_seq).toBe(3);

      const lateHistory = (await (
        await hub.fetch(
          new Request(
            `https://hub.test/api/conversations/${conv.id}/messages`,
            {
              headers: { Authorization: `Bearer ${tokenLate}` },
            },
          ),
        )
      ).json()) as any;
      expect(lateHistory.unread_count).toBe(0);
      expect(lateHistory.messages.map((m: any) => m.seq)).toEqual([2, 3]);
    });
  });

  it('F14: enforces global affected-row cleanup budget across messages, members, and headers, keeps expired archived conversations unavailable during chunked purge, and progresses mixed backlogs', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f14-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      let clockMs = Date.now() - 65 * 24 * 60 * 60 * 1000;
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => (clockMs += 10);

      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const nowIso = new Date(clockMs).toISOString();
      hub.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot', 'proj', 'worker', ?)",
        nowIso,
      );

      // Create 2 conversations with 30 messages each and archive both (60 messages + 4 members + 2 headers = 66 SQL rows)
      const convIds: string[] = [];
      for (const cIdx of [1, 2]) {
        const c = hub.conversationsService.createConversation(
          {
            session: {
              sub: 'alice',
              email: 'admin@fake.local',
              isAdmin: true,
              csrfToken: 'csrf',
              exp: Math.floor(clockMs / 1000) + 86400,
            },
            csrfVerified: true,
          },
          {
            kind: 'group',
            title: `Archived Heavy ${cIdx}`,
            members: ['proj:bot'],
          },
        );
        convIds.push(c.id);
        for (let m = 1; m <= 30; m++) {
          hub.conversationsService.sendMessage(
            {
              session: {
                sub: 'alice',
                email: 'admin@fake.local',
                isAdmin: true,
                csrfToken: 'csrf',
                exp: Math.floor(clockMs / 1000) + 86400,
              },
              csrfVerified: true,
            },
            {
              conversationId: c.id,
              body: `Archived msg ${cIdx}-${m}`,
              idempotencyKey: `f14-${cIdx}-${m}`,
            },
          );
        }
        hub.conversationsService.setArchived(
          {
            session: {
              sub: 'alice',
              email: 'admin@fake.local',
              isAdmin: true,
              csrfToken: 'csrf',
              exp: Math.floor(clockMs / 1000) + 86400,
            },
            csrfVerified: true,
          },
          c.id,
          true,
        );
      }

      const countAllRows = () => {
        const c = Number(
          (
            hub.sql
              .exec('SELECT COUNT(*) AS n FROM conversations')
              .toArray()[0] as any
          ).n,
        );
        const m = Number(
          (
            hub.sql
              .exec('SELECT COUNT(*) AS n FROM conversation_messages')
              .toArray()[0] as any
          ).n,
        );
        const cm = Number(
          (
            hub.sql
              .exec('SELECT COUNT(*) AS n FROM conversation_members')
              .toArray()[0] as any
          ).n,
        );
        return { c, m, cm, total: c + m + cm };
      };

      expect(countAllRows()).toEqual({ c: 2, m: 60, cm: 4, total: 66 });

      // Advance past 30-day archive retention TTL
      clockMs += 31 * 24 * 60 * 60 * 1000;

      // Expired archived conversations are immediately unavailable (404 not_found and omitted from list) even before/during chunked cleanup
      for (const cid of convIds) {
        const getRes = await hub.fetch(
          new Request(`https://hub.test/api/conversations/${cid}`, {
            headers: { Cookie: cookieAlice },
          }),
        );
        expect(getRes.status).toBe(404);

        const msgRes = await hub.fetch(
          new Request(`https://hub.test/api/conversations/${cid}/messages`, {
            headers: { Cookie: cookieAlice },
          }),
        );
        expect(msgRes.status).toBe(404);
      }
      const listAll = (await (
        await hub.fetch(
          new Request(
            'https://hub.test/api/conversations?include_archived=true',
            {
              headers: { Cookie: cookieAlice },
            },
          ),
        )
      ).json()) as any;
      expect(listAll.conversations).toHaveLength(0);

      // Run chunked cleanup with batchLimit = 10; each call must delete <= 10 SQL rows and never leave orphaned children
      let batches = 0;
      for (;;) {
        const before = countAllRows();
        const res = hub.conversationsService.retentionCleanup(clockMs, 10);
        const after = countAllRows();
        const actualDeleted = before.total - after.total;
        batches++;

        expect(actualDeleted).toBe(res.rowsDeleted);
        expect(actualDeleted).toBeLessThanOrEqual(10);
        expect(actualDeleted).toBeGreaterThan(0);

        // Invariant: no child message or member row may ever exist without its parent conversation header
        const orphanMsgs = Number(
          (
            hub.sql
              .exec(
                'SELECT COUNT(*) AS n FROM conversation_messages WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_messages.conversation_id)',
              )
              .toArray()[0] as any
          ).n,
        );
        const orphanMembers = Number(
          (
            hub.sql
              .exec(
                'SELECT COUNT(*) AS n FROM conversation_members WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_members.conversation_id)',
              )
              .toArray()[0] as any
          ).n,
        );
        expect(orphanMsgs).toBe(0);
        expect(orphanMembers).toBe(0);

        if (!res.hasMore) break;
      }

      expect(batches).toBe(7); // ceil(66 / 10) = 7 batches
      expect(countAllRows()).toEqual({ c: 0, m: 0, cm: 0, total: 0 });
      expect(hub.conversationsService.getStorageUsage()).toEqual({
        conversation_count: 0,
        message_count: 0,
        message_bytes: 0,
        removed_member_count: 0,
      });

      // Verify mixed bus + conversation backlog progresses both domains within shared limit via busService.retentionCleanup
      for (let i = 1; i <= 6; i++) {
        hub.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'a', 'b', 'note', 'th-1', '{}', ?, ?, ?)",
          `bus-exp-${i}`,
          `bus-idem-${i}`,
          clockMs - 10000,
          clockMs - 1000,
        );
      }
      const mixConv = hub.conversationsService.createConversation(
        {
          session: {
            sub: 'alice',
            email: 'admin@fake.local',
            isAdmin: true,
            csrfToken: 'csrf',
            exp: Math.floor(clockMs / 1000) + 86400,
          },
          csrfVerified: true,
        },
        { kind: 'group', title: 'Mixed Backlog', members: ['proj:bot'] },
      );
      for (let i = 1; i <= 6; i++) {
        hub.conversationsService.sendMessage(
          {
            session: {
              sub: 'alice',
              email: 'admin@fake.local',
              isAdmin: true,
              csrfToken: 'csrf',
              exp: Math.floor(clockMs / 1000) + 86400,
            },
            csrfVerified: true,
          },
          {
            conversationId: mixConv.id,
            body: `Mix msg ${i}`,
            idempotencyKey: `mix-${i}`,
          },
        );
      }
      clockMs += 31 * 24 * 60 * 60 * 1000;

      const mixBatch1 = hub.busService.retentionCleanup(clockMs, 6) as any;
      expect(mixBatch1.messagesDeleted).toBeGreaterThan(0);
      expect(mixBatch1.conversationRowsDeleted).toBeGreaterThan(0);
      expect(
        mixBatch1.messagesDeleted +
          mixBatch1.claimsDeleted +
          mixBatch1.conversationRowsDeleted,
      ).toBeLessThanOrEqual(6);
      expect(mixBatch1.hasMore).toBe(true);
    });
  });

  it('F16: atomically backfills per-conversation and global counters and message body_bytes when upgrading from original ee6 schema', async () => {
    const stub = testEnv.HUB.get(
      testEnv.HUB.idFromName('sec-f16-' + crypto.randomUUID()),
    );

    await runInDurableObject(stub, async (_base: unknown, state: DurableObjectState) => {
      // Drop current conversation tables to simulate a DO initialized with the original ee6d6795 schema
      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_storage_usage');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_messages');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversation_members');
      state.storage.sql.exec('DROP TABLE IF EXISTS conversations');

      // Create original ee6d6795 conversation tables (without counter columns, body_bytes, or conversation_storage_usage)
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

      const t0Iso = '2026-10-17T01:00:00.000Z';
      state.storage.sql.exec(
        "INSERT INTO agents (tag, project, role, registered_at) VALUES ('bot1', 'proj', 'worker', ?), ('bot2', 'proj', 'worker', ?)",
        t0Iso,
        t0Iso,
      );

      // Seed legacy conversation with 2 messages (40 bytes + 35 bytes = 75 bytes) and 1 removed member
      const legBody1 = 'Legacy message one (40 bytes exact!!!!!)'; // 40 bytes
      const legBody2 = 'Legacy message two (35 bytes!!!!)'; // 33 bytes -> let's use exact UTF-8 length
      const legBytes1 = new TextEncoder().encode(legBody1).byteLength;
      const legBytes2 = new TextEncoder().encode(legBody2).byteLength;

      state.storage.sql.exec(
        `INSERT INTO conversations (id, kind, title, created_by, created_at, updated_at, last_seq, last_message_at, last_message_preview)
         VALUES ('legacy-conv-1', 'group', 'Legacy Room', 'human:alice', ?, ?, 2, ?, ?)`,
        t0Iso,
        t0Iso,
        t0Iso,
        legBody2,
      );
      state.storage.sql.exec(
        `INSERT INTO conversation_members (conversation_id, actor, kind, role, added_at, added_by, removed_at, removed_by, last_read_seq)
         VALUES ('legacy-conv-1', 'human:alice', 'human', 'owner', ?, 'human:alice', NULL, NULL, 2),
                ('legacy-conv-1', 'proj:bot1', 'agent', 'member', ?, 'human:alice', NULL, NULL, 1),
                ('legacy-conv-1', 'proj:bot2', 'agent', 'member', ?, 'human:alice', ?, 'human:alice', 1)`,
        t0Iso,
        t0Iso,
        t0Iso,
        t0Iso,
      );
      state.storage.sql.exec(
        `INSERT INTO conversation_messages (id, conversation_id, seq, sender_actor, sender_kind, kind, body, idempotency_key, request_json, created_at)
         VALUES ('leg-m1', 'legacy-conv-1', 1, 'human:alice', 'human', 'message', ?, 'leg-k1', '{}', ?),
                ('leg-m2', 'legacy-conv-1', 2, 'human:alice', 'human', 'message', ?, 'leg-k2', '{}', ?)`,
        legBody1,
        t0Iso,
        legBody2,
        t0Iso,
      );

      // Instantiate Hub -> triggers init() and versioned v2 atomic backfill
      let clockMs = Date.parse('2026-10-17T01:05:00.000Z');
      const hub = new Hub(state, testEnv);
      (hub.conversationsService as any).now = () => clockMs;

      // Verify per-conversation counters and body_bytes were backfilled
      const convRow = hub.sql
        .exec(
          'SELECT retained_message_count, retained_message_bytes, removed_member_count FROM conversations WHERE id = ?',
          'legacy-conv-1',
        )
        .toArray()[0] as any;
      expect(Number(convRow.retained_message_count)).toBe(2);
      expect(Number(convRow.retained_message_bytes)).toBe(
        legBytes1 + legBytes2,
      );
      expect(Number(convRow.removed_member_count)).toBe(1);

      const msgRows = hub.sql
        .exec(
          'SELECT id, body_bytes FROM conversation_messages WHERE conversation_id = ? ORDER BY seq ASC',
          'legacy-conv-1',
        )
        .toArray() as any[];
      expect(Number(msgRows[0].body_bytes)).toBe(legBytes1);
      expect(Number(msgRows[1].body_bytes)).toBe(legBytes2);

      expect(hub.conversationsService.getStorageUsage()).toEqual({
        conversation_count: 1,
        message_count: 2,
        message_bytes: legBytes1 + legBytes2,
        removed_member_count: 1,
      });

      // Verify per-conversation cap enforcement works immediately against the backfilled counters
      hub.conversationsService.limits.maxMessagesPerConversation = 2;
      const cookieAlice = await makeSignedCookie(
        'alice',
        'admin@fake.local',
        'csrf-sec-token',
        7200,
      );
      const blockedByBackfilledCount = await hub.fetch(
        new Request(
          'https://hub.test/api/conversations/legacy-conv-1/messages',
          {
            method: 'POST',
            headers: {
              Cookie: cookieAlice,
              'X-CSRF-Token': 'csrf-sec-token',
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({
              body: 'Third message over cap',
              idempotency_key: 'leg-k3',
            }),
          },
        ),
      );
      expect(blockedByBackfilledCount.status).toBe(409);
      expect(await blockedByBackfilledCount.json()).toEqual({
        error: 'storage_limit_exceeded',
        reason: 'conversation_message_limit_exceeded',
      });

      // Restart idempotence: calling init() again preserves counters and schema_version = 2
      hub.conversationsService.init();
      const usageRow = hub.sql
        .exec('SELECT * FROM conversation_storage_usage WHERE singleton = 1')
        .toArray()[0] as any;
      expect(Number(usageRow.schema_version)).toBe(2);

      // Retention cleanup after 31 days decrements backfilled counters cleanly to 0
      clockMs += 31 * 24 * 60 * 60 * 1000;
      hub.conversationsService.retentionCleanup(clockMs, 100);
      expect(hub.conversationsService.getStorageUsage()).toEqual({
        conversation_count: 1,
        message_count: 0,
        message_bytes: 0,
        removed_member_count: 0,
      });
    });
  });
});




