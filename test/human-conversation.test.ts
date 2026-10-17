import { describe, it, expect } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import { createSessionCookie, tokenAdminSubject, TOKEN_ADMIN_EMAIL } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

function extractCookiePair(setCookieHeader: string): string {
  return setCookieHeader.split(';')[0];
}

async function sha256Hex(value: string): Promise<string> {
  const data = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('');
}

async function insertHumanBearerCredential(
  sql: any,
  opts: {
    id: string;
    token: string;
    sub: string;
    email: string;
    scopes?: string[];
    machineId?: string | null;
    createdBySub?: string;
    expiresAt?: string;
    revokedAt?: string | null;
    familyId?: string;
    refreshHash?: string;
  }
) {
  const tokenHash = await sha256(opts.token);
  const nowIso = new Date().toISOString();
  const expiresAt = opts.expiresAt ?? new Date(Date.now() + 3600_000).toISOString();
  sql.exec(
    `INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, revoked_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    opts.id,
    'human',
    tokenHash,
    opts.refreshHash ?? null,
    opts.familyId ?? ('fam-' + opts.id),
    opts.email,
    opts.machineId ?? null,
    'human:' + opts.sub,
    JSON.stringify(opts.scopes ?? ['admin']),
    opts.createdBySub ?? opts.sub,
    nowIso,
    expiresAt,
    opts.revokedAt ?? null
  );
}

describe('Human CLI Conversation Integration (test/human-conversation.test.ts)', () => {
  const tokenAdminRaw = 'valid-token-secret-12345678901234567890';
  let tokenAdminHashHex: string;

  it('1. current allowed Google human bearer: participants, create, list, send, idempotent replay, read cursor, history', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com,other@example.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-google-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      // Seed real users table row
      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      // Seed valid human bearer credential
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-alexey-01',
        token: 'tok-human-alexey-live',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
      });

      // A. GET /api/conversations/participants -> 200, kind human
      const partRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-human-alexey-live' },
        })
      );
      expect(partRes.status).toBe(200);
      const partData = (await partRes.json()) as any;
      expect(partData.self.actor).toBe('human:google-sub-alexey');
      expect(partData.self.kind).toBe('human');
      expect(partData.self.display_name).toBe('alexey@google.com');

      // B. POST /api/conversations -> 201 created
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-human-alexey-live',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Operator Priority Investigation',
            kind: 'group',
            idempotency_key: 'ik-human-conv-1',
          }),
        })
      );
      expect(createRes.status).toBe(201);
      const created = (await createRes.json()) as any;
      const cid = created.conversation.id;
      expect(created.conversation.created_by).toBe('human:google-sub-alexey');

      // C. GET /api/conversations -> 200, includes the conversation
      const listRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-human-alexey-live' },
        })
      );
      expect(listRes.status).toBe(200);
      const listData = (await listRes.json()) as any;
      expect(listData.conversations.some((c: any) => c.id === cid)).toBe(true);

      // D. POST /api/conversations/:id/messages -> 201 send message
      const sendRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/messages`, {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-human-alexey-live',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Hello from human CLI bearer',
            idempotency_key: 'ik-human-msg-1',
          }),
        })
      );
      expect(sendRes.status).toBe(201);
      const msg1 = (await sendRes.json()) as any;
      expect(msg1.message.sender_actor).toBe('human:google-sub-alexey');
      expect(msg1.message.sender_kind).toBe('human');
      expect(msg1.message.seq).toBe(1);

      // E. Idempotent replay of message
      const replayRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/messages`, {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-human-alexey-live',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Hello from human CLI bearer',
            idempotency_key: 'ik-human-msg-1',
          }),
        })
      );
      expect(replayRes.status).toBe(200);
      const replayMsg = (await replayRes.json()) as any;
      expect(replayMsg.message.id).toBe(msg1.message.id);
      expect(replayMsg.replay).toBe(true);

      // F. POST /api/conversations/:id/read -> 200 mark read cursor
      const readRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/read`, {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-human-alexey-live',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ seq: 1 }),
        })
      );
      expect(readRes.status).toBe(200);
      const readData = (await readRes.json()) as any;
      expect(readData.read_seq).toBe(1);
      expect(readData.unread_count).toBe(0);

      // G. GET /api/conversations/:id/messages -> 200 read history
      const histRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/messages`, {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-human-alexey-live' },
        })
      );
      expect(histRes.status).toBe(200);
      const histData = (await histRes.json()) as any;
      expect(histData.messages.length).toBe(1);
      expect(histData.messages[0].body).toBe('Hello from human CLI bearer');
    });
  });

  it('2. bootstrap token admin subject pin: works with configured hash, fails when hash unconfigured or sub pinned differently', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const validSubject = tokenAdminSubject(tokenAdminHashHex);

    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-token-admin-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);

      // Seed valid token admin credential
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-token-admin-1',
        token: 'tok-token-admin-live',
        sub: validSubject,
        email: TOKEN_ADMIN_EMAIL,
      });

      // Seed another credential with wrong/arbitrary sub
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-token-admin-bad',
        token: 'tok-token-admin-bad-sub',
        sub: 'admin-token:arbitrary-forged',
        email: TOKEN_ADMIN_EMAIL,
      });

      // Valid token admin bearer succeeds
      const goodRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-token-admin-live' },
        })
      );
      expect(goodRes.status).toBe(200);
      const goodData = (await goodRes.json()) as any;
      expect(goodData.self.actor).toBe('human:' + validSubject);

      // Forged / mismatching sub fails closed with 401
      const badRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-token-admin-bad-sub' },
        })
      );
      expect(badRes.status).toBe(401);

      // If token admin hash is not configured in env, token admin fails closed
      const unconfiguredEnv = {
        ...bindings,
        HUB_ADMIN_TOKEN_SHA256: undefined,
      } as unknown as Env;
      const unconfHub = new Hub(state, unconfiguredEnv);
      const unconfRes = await unconfHub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-token-admin-live' },
        })
      );
      expect(unconfRes.status).toBe(401);
    });
  });

  it('3. refresh credential: newly issued access token is valid, old revoked token returns 401', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-refresh-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      const oldRefreshSecret = 'refresh-secret-old-12345';
      const oldRefreshHash = await sha256(oldRefreshSecret);

      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-refresh-old',
        token: 'tok-access-old-1',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        familyId: 'fam-refresh-1',
        refreshHash: oldRefreshHash,
      });

      // Old token works initially
      const initRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-access-old-1' },
        })
      );
      expect(initRes.status).toBe(200);

      // Perform refresh via /api/token/refresh
      const refreshRes = await hub.fetch(
        new Request('https://hub.test/api/token/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            grant_type: 'refresh_token',
            refresh_token: oldRefreshSecret,
          }),
        })
      );
      expect(refreshRes.status).toBe(200);
      const refreshData = (await refreshRes.json()) as any;
      const newAccessToken = refreshData.access_token;
      expect(newAccessToken).toBeDefined();

      // New access token works
      const newRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: `Bearer ${newAccessToken}` },
        })
      );
      expect(newRes.status).toBe(200);

      // Old access token was revoked in family rotation -> returns 401
      const oldRevokedRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-access-old-1' },
        })
      );
      expect(oldRevokedRes.status).toBe(401);
    });
  });

  it('4. transaction-fenced user changes: removal, disable (is_admin=0), email mismatch, admin de-allowlisting fail closed', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-fencing-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-fence-test',
        token: 'tok-fence-test',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
      });

      // 1. Initial access passes
      const res1 = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(res1.status).toBe(200);

      // 2. Disable user in users table (is_admin = 0) -> fails closed with 401
      hub.sql.exec("UPDATE users SET is_admin = 0 WHERE sub = 'google-sub-alexey'");
      const resDisabled = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(resDisabled.status).toBe(401);

      // Restore is_admin = 1 -> passes again
      hub.sql.exec("UPDATE users SET is_admin = 1 WHERE sub = 'google-sub-alexey'");
      const resRestored = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(resRestored.status).toBe(200);

      // 3. Change email in users table -> mismatch fails closed with 401
      hub.sql.exec("UPDATE users SET email = 'changed@google.com' WHERE sub = 'google-sub-alexey'");
      const resEmailChanged = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(resEmailChanged.status).toBe(401);

      // Restore email
      hub.sql.exec("UPDATE users SET email = 'alexey@google.com' WHERE sub = 'google-sub-alexey'");

      // 4. Remove user from users table entirely -> fails closed with 401
      hub.sql.exec("DELETE FROM users WHERE sub = 'google-sub-alexey'");
      const resDeleted = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(resDeleted.status).toBe(401);

      // Re-insert user, but de-allowlist from HUB_ADMINS -> fails closed with 401
      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );
      const nonAdminEnv = {
        ...bindings,
        HUB_ADMINS: 'otheradmin@google.com', // alexey removed
      } as Env;
      const nonAdminHub = new Hub(state, nonAdminEnv);
      const resUnallowlisted = await nonAdminHub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-fence-test' },
        })
      );
      expect(resUnallowlisted.status).toBe(401);
    });
  });

  it('5. credential invalidity guards: expired, revoked, malformed scopes, created_by_sub mismatch, machine binding fail closed', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-invalids-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      // A. Expired credential
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-expired',
        token: 'tok-expired',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        expiresAt: new Date(Date.now() - 60_000).toISOString(),
      });
      const expRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-expired' },
        })
      );
      expect(expRes.status).toBe(401);

      // B. Revoked credential
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-revoked',
        token: 'tok-revoked',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        revokedAt: new Date().toISOString(),
      });
      const revRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-revoked' },
        })
      );
      expect(revRes.status).toBe(401);

      // C. Malformed scopes (not exactly ['admin'])
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-bad-scopes-1',
        token: 'tok-bad-scopes-1',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        scopes: ['admin', 'host'],
      });
      const badScopes1 = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-bad-scopes-1' },
        })
      );
      expect(badScopes1.status).toBe(401);

      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-bad-scopes-2',
        token: 'tok-bad-scopes-2',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        scopes: ['bus:read'],
      });
      const badScopes2 = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-bad-scopes-2' },
        })
      );
      expect(badScopes2.status).toBe(401);

      // D. created_by_sub mismatch
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-sub-mismatch',
        token: 'tok-sub-mismatch',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        createdBySub: 'someone-else',
      });
      const subMismatchRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-sub-mismatch' },
        })
      );
      expect(subMismatchRes.status).toBe(401);

      // E. Machine binding on human credential
      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-mach-bound',
        token: 'tok-mach-bound',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
        machineId: 'some-machine-id',
      });
      const machBoundRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-mach-bound' },
        })
      );
      expect(machBoundRes.status).toBe(401);
    });
  });

  it('6. authority separation: cookie mutations require CSRF even with bearer present, bearer cannot issue automation grant', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-authority-sep-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-alexey-auth-sep',
        token: 'tok-alexey-auth-sep',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
      });

      // Create a conversation using bearer
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-alexey-auth-sep',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Separation Thread',
            kind: 'group',
            idempotency_key: 'ik-sep-1',
          }),
        })
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;
      const cid = conv.conversation.id;

      // Create a valid browser cookie session
      const cookieHeader = extractCookiePair(
        await createSessionCookie(bindings, {
          sub: 'google-sub-alexey',
          email: 'alexey@google.com',
          isAdmin: true,
          csrfToken: 'valid-csrf-token-12345',
          exp: Math.floor(Date.now() / 1000) + 3600,
        })
      );

      // Attempt mutating request with Cookie session but NO CSRF token, while also passing Bearer header
      const mixedMutateRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/messages`, {
          method: 'POST',
          headers: {
            Cookie: cookieHeader,
            Authorization: 'Bearer tok-alexey-auth-sep',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            body: 'Should fail due to missing CSRF token on session',
            idempotency_key: 'ik-csrf-bypass-attempt',
          }),
        })
      );
      // Because Cookie session was recognized, CSRF is required!
      expect(mixedMutateRes.status).toBe(403);
      expect(((await mixedMutateRes.json()) as any).error).toBe('csrf_invalid');

      // Attempt to issue an automation grant using human bearer token (no real browser session)
      const autoGrantRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/automation`, {
          method: 'PUT',
          headers: {
            Authorization: 'Bearer tok-alexey-auth-sep',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            check: 'check-1',
            target_actor: 'human:google-sub-alexey',
            body: 'alert message',
            on: 'fail',
            expires_at: Date.now() + 3600_000,
          }),
        })
      );
      // M5 automation grant requires real human browser session + CSRF; bearer must NOT gain issuance!
      expect(autoGrantRes.status).toBe(403);
    });
  });

  it('7. disjoint principals: agent, host, job tokens cannot promote to human or access unauthorized conversation operations', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-disjoint-kinds-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      // Insert host credential
      const hostHash = await sha256('tok-host-1');
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('cred-host-1', 'host', ?, 'fam-host', 'machine-host-1', 'mach-1', 'host:mach-1', '["host"]', 'admin', ?, ?)`,
        hostHash,
        nowIso,
        new Date(Date.now() + 3600_000).toISOString()
      );

      // Insert job credential
      const jobHash = await sha256('tok-job-1');
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('cred-job-1', 'job', ?, 'fam-job', 'job-uuid-1', null, 'job:job-uuid-1', '["job"]', 'admin', ?, ?)`,
        jobHash,
        nowIso,
        new Date(Date.now() + 3600_000).toISOString()
      );

      // Insert agent credential with human-looking actor string or admin claim
      const spoofAgentHash = await sha256('tok-spoof-agent');
      hub.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES ('cred-spoof-agent', 'agent', ?, 'fam-spoof', 'spoof-agent', null, 'human:spoofed-admin', '["bus:send", "bus:read", "admin"]', 'admin', ?, ?)`,
        spoofAgentHash,
        nowIso,
        new Date(Date.now() + 3600_000).toISOString()
      );

      // Host token rejected
      const hostRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-host-1' },
        })
      );
      expect(hostRes.status).toBe(403);
      expect(((await hostRes.json()) as any).error).toBe('forbidden');

      // Job token rejected
      const jobRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-job-1' },
        })
      );
      expect(jobRes.status).toBe(403);
      expect(((await jobRes.json()) as any).error).toBe('forbidden');

      // Spoofed agent token with human actor rejected
      const spoofRes = await hub.fetch(
        new Request('https://hub.test/api/conversations/participants', {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-spoof-agent' },
        })
      );
      expect(spoofRes.status).toBe(403);
      expect(((await spoofRes.json()) as any).reason).toBe('unregistered_agent_principal');
    });
  });

  it('8. conversation links: verified human bearer can attach, list, reverse canonical jobs/launches, and target is redacted if revoked', async () => {
    tokenAdminHashHex = await sha256Hex(tokenAdminRaw);
    const bindings = {
      ...(env as unknown as Env),
      HUB_ADMIN_TOKEN_SHA256: tokenAdminHashHex,
      HUB_ADMINS: 'alexey@google.com',
      SESSION_SECRET: 'test-session-secret-must-be-at-least-32-chars-long!',
    } as Env;

    const stub = bindings.HUB.get(bindings.HUB.idFromName('conv-human-links-' + crypto.randomUUID()));

    await runInDurableObject(stub, async (_inst: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, bindings);
      const nowIso = new Date().toISOString();

      hub.sql.exec(
        'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        'google-sub-alexey',
        'alexey@google.com',
        nowIso,
        nowIso
      );

      await insertHumanBearerCredential(hub.sql, {
        id: 'cred-alexey-links',
        token: 'tok-alexey-links',
        sub: 'google-sub-alexey',
        email: 'alexey@google.com',
      });

      // Seed a routing job
      const jobId = '11111111-1111-4111-8111-111111111111';
      hub.sql.exec(
        `INSERT INTO routing_jobs (id, actor, request_key, request_json, state, created_at, updated_at)
         VALUES (?, 'agent:worker-1', 'req-job-1', '{}', 'running', ?, ?)`,
        jobId,
        nowIso,
        nowIso
      );

      // Create conversation
      const createRes = await hub.fetch(
        new Request('https://hub.test/api/conversations', {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-alexey-links',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            title: 'Links Thread',
            kind: 'group',
            idempotency_key: 'ik-link-test-1',
          }),
        })
      );
      expect(createRes.status).toBe(201);
      const conv = (await createRes.json()) as any;
      const cid = conv.conversation.id;

      // Attach link to routing_job as human bearer
      const attachRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/links`, {
          method: 'POST',
          headers: {
            Authorization: 'Bearer tok-alexey-links',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            kind: 'routing_job',
            ref: jobId,
            idempotency_key: 'ik-attach-job-1',
          }),
        })
      );
      expect(attachRes.status).toBe(201);
      const link = (await attachRes.json()) as any;
      expect(link.target.available).toBe(true);
      expect(link.target.canonical_status).toBe('running');

      // List links as human bearer
      const listLinksRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/links`, {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-alexey-links' },
        })
      );
      expect(listLinksRes.status).toBe(200);
      const listLinksData = (await listLinksRes.json()) as any;
      expect(listLinksData.links.length).toBe(1);
      expect(listLinksData.links[0].target.available).toBe(true);

      // Reverse lookup
      const revRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/links?kind=routing_job&ref=${jobId}`, {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-alexey-links' },
        })
      );
      expect(revRes.status).toBe(200);
      const revData = (await revRes.json()) as any;
      expect(revData.target.available).toBe(true);
      expect(revData.links.length).toBe(1);

      // Now revoke target authorization: if target job is deleted or caller credential revoked, target becomes unavailable
      hub.sql.exec('DELETE FROM routing_jobs WHERE id = ?', jobId);
      const redactedListRes = await hub.fetch(
        new Request(`https://hub.test/api/conversations/${cid}/links`, {
          method: 'GET',
          headers: { Authorization: 'Bearer tok-alexey-links' },
        })
      );
      expect(redactedListRes.status).toBe(200);
      const redactedListData = (await redactedListRes.json()) as any;
      expect(redactedListData.links[0].target.available).toBe(false);
      expect(redactedListData.links[0].target.canonical_status).toBeNull();
    });
  });
});


describe('Human bearer through the deployed Worker entrypoint', () => {
  it('uses the actual bound Hub instance for current Google bearer requests', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hub'));
    const token = 'entrypoint-human-bearer-' + crypto.randomUUID();
    const sub = 'entrypoint-human-' + crypto.randomUUID();
    await runInDurableObject(stub, async (hub: BaseHub) => {
      const now = new Date().toISOString();
      hub.sql.exec('INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, 1, ?, ?)',
        sub, 'admin@fake.local', now, now);
      await insertHumanBearerCredential(hub.sql, {
        id: 'entrypoint-cred-' + sub, token, sub, email: 'admin@fake.local',
      });
    });
    const headers = { Authorization: 'Bearer ' + token };
    const me = await SELF.fetch('https://hub.test/api/auth/me', { headers });
    expect(me.status, await me.text()).toBe(200);
    const participants = await SELF.fetch('https://hub.test/api/conversations/participants', { headers });
    const data = await participants.json() as any;
    expect(participants.status, JSON.stringify(data)).toBe(200);
    expect(data.self.actor).toBe('human:' + sub);
  });
});
