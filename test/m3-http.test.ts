import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { pinJobsClock } from './slot-clock';
import { runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv } from './m1-focus-setup';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { BUS_SCHEMA_SQL } from '../src/bus';

let savedFetch = globalThis.fetch;

async function seedTestData(sql: any) {
  for (const statement of SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) {
      try { sql.exec(trimmed); } catch (e) {}
    }
  }
  for (const statement of BUS_SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) {
      try { sql.exec(trimmed); } catch (e) {}
    }
  }

  try { sql.exec('DELETE FROM messages'); } catch (e) {}
  try { sql.exec('DELETE FROM claims'); } catch (e) {}
  try { sql.exec('DELETE FROM agents'); } catch (e) {}
  try { sql.exec('DELETE FROM machines'); } catch (e) {}
  try { sql.exec('DELETE FROM credentials'); } catch (e) {}

  const now = new Date().toISOString();
  const future = new Date(Date.now() + 86400000).toISOString();

  // Seed Machines
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('mach-A', 'Machine A', 'host-a', ?)", now);
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('mach-B', 'Machine B', 'host-b', ?)", now);
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at) VALUES ('mach-C', 'Machine C', 'host-c', ?)", now);

  // Seed Host Credentials
  const tokenHostA = await sha256('token_host_a');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_mach_a', 'host', tokenHostA, 'fam_a', 'host-a', 'mach-A', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  const tokenHostB = await sha256('token_host_b');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_mach_b', 'host', tokenHostB, 'fam_b', 'host-b', 'mach-B', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  // Seed agents
  sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'mach-A')");
  sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-2', 'projB', 'worker', 'mach-B')");
  sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-3', 'projC', 'worker', 'mach-C')");
  sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('principal-1', 'projA', 'principal', 'mach-A')");

  // Seed Scoped Agent Credentials
  const tokenAgent1 = await sha256('token_agent_1');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_agent_1', 'agent', tokenAgent1, 'fam_ag1', 'worker-1', 'worker-1', 'mach-A', JSON.stringify(['bus:send', 'bus:read']), 'admin-1', now, future
  );

  const tokenPrincipal1 = await sha256('token_principal_1');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_prin_1', 'agent', tokenPrincipal1, 'fam_p1', 'principal-1', 'principal-1', 'mach-A', JSON.stringify(['bus:send', 'bus:read']), 'admin-1', now, future
  );

  // Read-only Agent Credential
  const tokenReadOnly = await sha256('token_read_only');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_ro', 'agent', tokenReadOnly, 'fam_ro', 'worker-1', 'worker-1', 'mach-A', JSON.stringify(['bus:read']), 'admin-1', now, future
  );

  // Send-only Agent Credential
  const tokenSendOnly = await sha256('token_send_only');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_so', 'agent', tokenSendOnly, 'fam_so', 'worker-1', 'worker-1', 'mach-A', JSON.stringify(['bus:send']), 'admin-1', now, future
  );

  // Expired Credential
  const tokenExpired = await sha256('token_expired');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_exp', 'agent', tokenExpired, 'fam_exp', 'worker-1', 'worker-1', 'mach-A', JSON.stringify(['bus:send', 'bus:read']), 'admin-1', now, new Date(Date.now() - 1000).toISOString()
  );

  // Revoked Credential
  const tokenRevoked = await sha256('token_revoked');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_rev', 'agent', tokenRevoked, 'fam_rev', 'worker-1', 'worker-1', 'mach-A', JSON.stringify(['bus:send', 'bus:read']), 'admin-1', now, future, now
  );
}

function countSql(sql: any, query: string, ...params: any[]): number {
  try {
    return Number((sql.exec(query, ...params).toArray()[0] as any)?.c ?? 0);
  } catch {
    return 0;
  }
}

function busCategorySnapshot(sql: any, nowMs: number) {
  let liveClaimIds: string[] = [];
  try {
    liveClaimIds = sql
      .exec('SELECT id FROM claims WHERE expires_at >= ? ORDER BY id', nowMs)
      .toArray()
      .map((r: any) => String(r.id));
  } catch {
    liveClaimIds = [];
  }
  return {
    messages: countSql(sql, 'SELECT COUNT(*) AS c FROM messages'),
    expiredMessages: countSql(
      sql,
      'SELECT COUNT(*) AS c FROM messages WHERE expires_at <= ?',
      nowMs,
    ),
    claims: countSql(sql, 'SELECT COUNT(*) AS c FROM claims'),
    expiredClaims: countSql(
      sql,
      'SELECT COUNT(*) AS c FROM claims WHERE expires_at < ?',
      nowMs,
    ),
    liveClaimIds,
  };
}

describe('M3 HTTP: End-to-End Authenticated Bus Routing', () => {
  let stub: any;

  beforeAll(async () => {
    const id = defaultEnv.HUB.idFromName("hub");
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      await seedTestData(instance.sql);
    });
  });

  afterAll(() => {
    globalThis.fetch = savedFetch;
  });

  it('1. Full Flow: Send -> Inbox -> Show -> Ack -> Accept -> Complete -> Reply', async () => {
    // Send message from Host A (delegating for worker-1) to worker-2
    const sendReq = new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_a',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Task instructions for worker 2',
        idempotency_key: 'flow-key-1',
        thread_id: 'thread-flow-1'
      })
    });
    const sendRes = await doFetch(sendReq);
    expect(sendRes.status).toBe(200);
    const sendJson = await sendRes.json() as any;
    expect(sendJson.id).toBeTruthy();
    expect(sendJson.thread_id).toBe('thread-flow-1');
    const messageId = sendJson.id;

    // Identical retry returns original ID + thread
    const retryRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_a',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Task instructions for worker 2',
        idempotency_key: 'flow-key-1',
        thread_id: 'thread-flow-1'
      })
    }));
    expect(retryRes.status).toBe(200);
    const retryJson = await retryRes.json() as any;
    expect(retryJson.id).toBe(messageId);
    expect(retryJson.thread_id).toBe('thread-flow-1');

    // Recipient (worker-2 on mach-B) reads inbox
    const inboxReq = new Request('http://example.com/api/bus/inbox?actor=worker-2', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_b' }
    });
    const inboxRes = await doFetch(inboxReq);
    expect(inboxRes.status).toBe(200);
    const inbox = await inboxRes.json() as any[];
    expect(inbox.length).toBe(1);
    expect(inbox[0].id).toBe(messageId);
    expect(inbox[0].body).toBe('Task instructions for worker 2');

    // Recipient views message via show
    const showReq = new Request(`http://example.com/api/bus/messages/${messageId}`, {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_b' }
    });
    const showRes = await doFetch(showReq);
    expect(showRes.status).toBe(200);
    const msg = await showRes.json() as any;
    expect(msg.id).toBe(messageId);

    // Deliver & read before ack
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec('UPDATE messages SET delivered_at = ?, read_at = ? WHERE id = ?', Date.now(), Date.now(), messageId);
    });

    const ackRes = await doFetch(new Request(`http://example.com/api/bus/messages/${messageId}/ack`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_b' }
    }));
    expect(ackRes.status).toBe(200);
    expect((await ackRes.json() as any).stage).toBe('ack');

    const acceptRes = await doFetch(new Request(`http://example.com/api/bus/messages/${messageId}/accept`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_b' }
    }));
    expect(acceptRes.status).toBe(200);
    expect((await acceptRes.json() as any).stage).toBe('accept');

    const completeRes = await doFetch(new Request(`http://example.com/api/bus/messages/${messageId}/complete`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_b' }
    }));
    expect(completeRes.status).toBe(200);
    expect((await completeRes.json() as any).stage).toBe('complete');

    // Recipient replies back to sender
    const replyReq = new Request('http://example.com/api/bus/reply', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_b',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        reply_to: messageId,
        from_actor: 'worker-2',
        to_actor: 'worker-1',
        kind: 'reply',
        body: 'Work finished successfully',
        idempotency_key: 'flow-reply-key-1'
      })
    });
    const replyRes = await doFetch(replyReq);
    expect(replyRes.status).toBe(200);
    const replyJson = await replyRes.json() as any;
    expect(replyJson.id).toBeTruthy();
    expect(replyJson.thread_id).toBe('thread-flow-1');
  });

  it('2. Idempotency Conflict: Conflicting payload with same idempotency key returns 409', async () => {
    const conflictRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_a',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Conflicting body with existing key flow-key-1',
        idempotency_key: 'flow-key-1'
      })
    }));
    expect(conflictRes.status).toBe(409);
    expect((await conflictRes.json() as any).error).toBe('conflict');
  });

  it('3. Root Role Fails Closed: Send, Inbox, Show, Claims fail closed with 403 / 404', async () => {
    // 1. Host token attempting to write as role:root is rejected (403 forbidden)
    const hostRootSendRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_a',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'role:root',
        to_actor: 'worker-1',
        kind: 'note',
        body: 'Host cannot send as root',
        idempotency_key: 'root-host-send-1'
      })
    }));
    expect(hostRootSendRes.status).toBe(403);
    const hostRootJson = await hostRootSendRes.json() as any;
    expect(hostRootJson.error).toBe('forbidden');

    // 2. Sending to role:root returns 404 unknown_recipient (no accepted root authority in M3)
    const toRootSendRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_agent_1',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'role:root',
        kind: 'note',
        body: 'Message to root must fail closed',
        idempotency_key: 'to-root-key-1'
      })
    }));
    expect(toRootSendRes.status).toBe(404);
    expect((await toRootSendRes.json() as any).error).toBe('unknown_recipient');

    // 3. Attempting to read inbox of role:root -> 403 Forbidden
    const rootInboxRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=role:root', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(rootInboxRes.status).toBe(403);

    // 4. Attempting to claim scope as role:root -> 403 Forbidden
    const rootClaimRes = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: 'src', holder: 'role:root' })
    }));
    expect(rootClaimRes.status).toBe(403);
  });

  it('4. Security & Authorization: Forgery, wrong host delegation, missing scopes, recipient leak', async () => {
    // A. Wrong host delegation: Machine A tries to send from worker-2 (which belongs to Machine B)
    const wrongHostRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_a',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-2',
        to_actor: 'worker-1',
        kind: 'note',
        body: 'Forged sender',
        idempotency_key: 'forgery-1'
      })
    }));
    expect(wrongHostRes.status).toBe(403);

    // B. Body caller forgery: token_agent_1 (bound to worker-1) sets from_actor: worker-2
    const forgedAgentRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_agent_1',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-2',
        to_actor: 'worker-1',
        kind: 'note',
        body: 'Forged agent sender',
        idempotency_key: 'forgery-2'
      })
    }));
    expect(forgedAgentRes.status).toBe(403);

    // C. Scope missing: Read-only token tries to send
    const readOnlySendRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_read_only',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Send attempt with RO token',
        idempotency_key: 'ro-send-1'
      })
    }));
    expect(readOnlySendRes.status).toBe(403);
    expect((await readOnlySendRes.json() as any).reason).toBe('scope_missing');

    // D. Scope missing: Send-only token tries to read inbox
    const sendOnlyInboxRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_send_only' }
    }));
    expect(sendOnlyInboxRes.status).toBe(403);
    expect((await sendOnlyInboxRes.json() as any).reason).toBe('scope_missing');

    // E. Recipient Leak: Machine B tries to read inbox of worker-1 (hosted on Machine A)
    const leakInboxRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_b' }
    }));
    expect(leakInboxRes.status).toBe(403);

    // F. Third-Party Reply Rejection: Machine A tries to reply to message with mismatched to_actor
    const mRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_actor: 'worker-1', to_actor: 'worker-2', kind: 'note', body: 'Confidential msg', idempotency_key: 'm-key-orig' })
    }));
    const mId = (await mRes.json() as any).id;

    const thirdPartyReplyRes = await doFetch(new Request('http://example.com/api/bus/reply', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ reply_to: mId, from_actor: 'worker-1', to_actor: 'worker-3', kind: 'reply', body: 'Leak to worker 3', idempotency_key: '3p-reply' })
    }));
    expect(thirdPartyReplyRes.status).toBe(400);
    expect((await thirdPartyReplyRes.json() as any).error).toBe('invalid_reply_to');

    // G. Expired / Revoked credentials denied 401
    const expiredRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_expired' }
    }));
    expect(expiredRes.status).toBe(401);

    const revokedRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_revoked' }
    }));
    expect(revokedRes.status).toBe(401);
  });

  it('5. Cookie Authentication & CSRF Validation', async () => {
    let adminCookie = '';
    await runInDurableObject(stub, async (instance: Hub) => {
      adminCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'csrf-secret-token-123',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });

    // A. Mutation without CSRF token -> 403 Forbidden
    const noCsrfRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Cookie': adminCookie,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Admin send without CSRF',
        idempotency_key: 'admin-no-csrf'
      })
    }));
    expect(noCsrfRes.status).toBe(403);
    expect((await noCsrfRes.json() as any).error).toBe('CSRF token missing');

    // B. Mutation with valid CSRF token -> 200 OK
    const withCsrfRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: {
        'Cookie': adminCookie,
        'x-csrf-token': 'csrf-secret-token-123',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Admin send with valid CSRF',
        idempotency_key: 'admin-with-csrf'
      })
    }));
    expect(withCsrfRes.status).toBe(200);

    // C. Non-admin cookie session -> 401 Unauthorized
    let nonAdminCookie = '';
    await runInDurableObject(stub, async (instance: Hub) => {
      nonAdminCookie = await createSessionCookie(instance.env, {
        sub: 'user-2',
        email: 'user2@fake.local',
        isAdmin: false,
        csrfToken: 'csrf-user-123',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });
    const nonAdminRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1', {
      method: 'GET',
      headers: { 'Cookie': nonAdminCookie }
    }));
    expect(nonAdminRes.status).toBe(401);
  });

  it('6. Validation Boundaries: 16 KiB body limit, malformed pagination, unknown fields, address validation', async () => {
    // A. 16 KiB limit: total JSON <= 16384 bytes accepted, > 16384 rejected with 413
    const prefix = '{"from_actor":"worker-1","to_actor":"worker-2","kind":"note","body":"';
    const suffix = '","idempotency_key":"size-16k-b"}';
    const overhead = prefix.length + suffix.length;
    const okBody = 'x'.repeat(16384 - overhead);
    const okPayload = prefix + okBody + suffix;
    expect(okPayload.length).toBe(16384);

    const okRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: okPayload
    }));
    expect(okRes.status).toBe(200);

    const overPayload = prefix + okBody + 'y' + suffix;
    expect(overPayload.length).toBe(16385);
    const overRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: overPayload
    }));
    expect(overRes.status).toBe(413);

    // B. Unknown fields rejected
    const unknownFieldRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_actor: 'worker-1', to_actor: 'worker-2', kind: 'note', body: 'hi', idempotency_key: 'uf-1', unauthorized_extra: 123 })
    }));
    expect(unknownFieldRes.status).toBe(400);
    expect((await unknownFieldRes.json() as any).error).toBe('unknown_fields');

    // C. Malformed pagination: non-integer limit, negative limit, > 100 limit, unknown query param
    const badLimitRes1 = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1&limit=abc', {
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(badLimitRes1.status).toBe(400);

    const badLimitRes2 = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1&limit=200', {
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(badLimitRes2.status).toBe(400);

    const unknownQueryRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=worker-1&evil_param=true', {
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(unknownQueryRes.status).toBe(400);

    // D. Address validation: Unknown recipient returns 404
    const unknownRecipientRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_actor: 'worker-1', to_actor: 'non_existent_actor_tag', kind: 'note', body: 'hello', idempotency_key: 'unk-recip' })
    }));
    expect(unknownRecipientRes.status).toBe(404);
    expect((await unknownRecipientRes.json() as any).error).toBe('unknown_recipient');

    // E. Address validation: Mismatched project:tag returns 404
    const ambigRecipientRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a', 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_actor: 'worker-1', to_actor: 'projB:worker-1', kind: 'note', body: 'hello', idempotency_key: 'ambig-recip' })
    }));
    expect(ambigRecipientRes.status).toBe(404);
    expect((await ambigRecipientRes.json() as any).error).toBe('unknown_recipient');
  });

  it('7. Claims Lifecycle: Normalization, Traversal Rejection, Conflict, Idempotency, and Release', async () => {
    // A. Traversal rejection: '../' -> 400
    const traversalRes = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: '../secret/file.ts', holder: 'worker-1' })
    }));
    expect(traversalRes.status).toBe(400);
    expect((await traversalRes.json() as any).error).toBe('invalid_scope');

    // B. Scope normalization: './src//bus.ts' -> 'src/bus.ts'
    const claimRes1 = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: './src//bus.ts', holder: 'worker-1', ttl_ms: 60000 })
    }));
    expect(claimRes1.status).toBe(200);
    const claimId1 = (await claimRes1.json() as any).id;
    expect(claimId1).toBeTruthy();

    // Verify stored scope is normalized
    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT scope FROM claims WHERE id = ?', claimId1).toArray()[0] as any;
      expect(row.scope).toBe('src/bus.ts');
    });

    // C. Cross-project claim denied (403): Worker 2 in projB attempts to claim in projA
    const crossProjClaimRes = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_b', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: 'src', holder: 'worker-2', ttl_ms: 60000 })
    }));
    expect(crossProjClaimRes.status).toBe(403);

    // Overlapping conflict (409): principal-1 (in projA) attempts to claim overlapping 'src' in projA
    const conflictClaimRes = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_principal_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: 'src', holder: 'principal-1', ttl_ms: 60000 })
    }));
    expect(conflictClaimRes.status).toBe(409);

    // D. Idempotent renewal by same holder returns same claim ID
    const renewalRes = await doFetch(new Request('http://example.com/api/bus/claims', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: 'projA', scope: 'src/bus.ts', holder: 'worker-1', ttl_ms: 120000 })
    }));
    expect(renewalRes.status).toBe(200);
    expect((await renewalRes.json() as any).id).toBe(claimId1);

    // E. Release ownership: Non-holder cannot release
    const badReleaseRes = await doFetch(new Request(`http://example.com/api/bus/claims/${claimId1}/release`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_b' }
    }));
    expect(badReleaseRes.status).toBe(403);

    // F. Holder releases via project:scope
    const goodReleaseRes = await doFetch(new Request(`http://example.com/api/bus/claims/${encodeURIComponent('projA:src/bus.ts')}/release`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(goodReleaseRes.status).toBe(200);

    // Verified released in SQLite
    await runInDurableObject(stub, async (instance: Hub) => {
      const row = instance.sql.exec('SELECT released_at FROM claims WHERE id = ?', claimId1).toArray()[0] as any;
      expect(row.released_at).toBeTruthy();
    });

    // G. List claims endpoint
    const listRes = await doFetch(new Request('http://example.com/api/bus/claims?project=projA', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(listRes.status).toBe(200);
  });

  it('8. Bounded Resumable Retention Cleanup via Maintenance Tick and Public Maintenance Route', async () => {
    // 1. Negative tests on public maintenance route POST /api/cron/retention:
    // a. anonymous (no credentials) -> 401
    const anonRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST'
    }));
    expect(anonRes.status).toBe(401);

    // b. host bearer token -> 403
    const hostRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(hostRes.status).toBe(403);

    // c. agent bearer token -> 403
    const agentRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(agentRes.status).toBe(403);

    // d. write-only bearer token -> 403
    const writeOnlyRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_send_only' }
    }));
    expect(writeOnlyRes.status).toBe(403);

    // e. admin session cookie without CSRF -> 403
    let adminCookie = '';
    await runInDurableObject(stub, async (instance: Hub) => {
      adminCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'valid-csrf-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });
    const noCsrfRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST',
      headers: { 'Cookie': adminCookie }
    }));
    expect(noCsrfRes.status).toBe(403);

    // Deterministic slot clocks: 01:05 is 5 minutes into the 01:00 slot (early);
    // 01:25 is 25 minutes in (late). scheduledTick refuses late slots; alarm does not.
    const EARLY_T = Date.parse('2026-10-17T01:05:00Z');
    const LATE_T = Date.parse('2026-10-17T01:25:00Z');
    const freezeJobsClock = async (target: any, when: number) => {
      await pinJobsClock(target, 0);
      await (runInDurableObject as any)(target, async (instance: any) => {
        instance.jobsService.setClock(() => new Date(when));
      });
    };

    // 2. Isolated single-category fixture: exactly 100 message deletions, 150 then 50 then 0.
    const isoStub = defaultEnv.HUB.get(
      defaultEnv.HUB.idFromName('m3-http-ret-iso-' + crypto.randomUUID()),
    );
    await runInDurableObject(isoStub, async (instance: Hub) => {
      await seedTestData(instance.sql);
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('iso-valid-msg-future', 'worker-1', 'worker-2', 'note', 'th', 'valid', 'iso-valid-key-future', ?, ?)",
        EARLY_T,
        EARLY_T + 30 * 24 * 3600 * 1000,
      );
      for (let i = 0; i < 250; i++) {
        instance.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'exp', ?, ?, ?)",
          `iso-exp-msg-${i}`,
          `iso-exp-key-${i}`,
          EARLY_T - 2000,
          EARLY_T - 1000,
        );
      }
      const isoBefore = busCategorySnapshot(instance.sql, EARLY_T);
      expect(isoBefore.expiredMessages).toBe(250);
      expect(isoBefore.expiredClaims).toBe(0);
    });
    await freezeJobsClock(isoStub, EARLY_T);
    const isoTickRes = await isoStub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ now: '2099-01-01T00:00:00Z' }),
    }));
    expect(isoTickRes.status).toBe(200);
    await runInDurableObject(isoStub, async (instance: Hub) => {
      expect(
        instance.sql.exec("SELECT id FROM messages WHERE id = 'iso-valid-msg-future'").toArray(),
      ).toHaveLength(1);
      const isoAfter = busCategorySnapshot(instance.sql, EARLY_T);
      expect(isoAfter.expiredMessages).toBe(150);
      expect(isoAfter.expiredClaims).toBe(0);
      expect(isoAfter.liveClaimIds).toEqual([]);
      expect(
        countSql(instance.sql, "SELECT COUNT(*) AS c FROM messages WHERE id LIKE 'iso-exp-msg-%'"),
      ).toBe(150);
    });
    await freezeJobsClock(isoStub, EARLY_T);
    await runInDurableObject(isoStub, async (instance: Hub) => {
      const coldHub = new Hub((instance as any).ctx, instance.env);
      (coldHub as any).jobsService.setClock(() => new Date(EARLY_T));
      expect(
        countSql(coldHub.sql, "SELECT COUNT(*) AS c FROM messages WHERE id LIKE 'iso-exp-msg-%'"),
      ).toBe(150);
      await coldHub.alarm();
      expect(
        countSql(coldHub.sql, "SELECT COUNT(*) AS c FROM messages WHERE id LIKE 'iso-exp-msg-%'"),
      ).toBe(50);
      await coldHub.alarm();
      expect(
        countSql(coldHub.sql, "SELECT COUNT(*) AS c FROM messages WHERE id LIKE 'iso-exp-msg-%'"),
      ).toBe(0);
      expect(
        coldHub.sql.exec("SELECT id FROM messages WHERE id = 'iso-valid-msg-future'").toArray(),
      ).toHaveLength(1);
    });

    // Late-slot alarm uses the same insertion/expiry/cleanup clock (25 minutes into the slot).
    const lateStub = defaultEnv.HUB.get(
      defaultEnv.HUB.idFromName('m3-http-ret-late-' + crypto.randomUUID()),
    );
    const lateLiveClaim = 'late-genuine-live-claim';
    await runInDurableObject(lateStub, async (instance: Hub) => {
      await seedTestData(instance.sql);
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('late-valid-msg', 'worker-1', 'worker-2', 'note', 'th', 'valid', 'late-valid-key', ?, ?)",
        LATE_T,
        LATE_T + 30 * 24 * 3600 * 1000,
      );
      for (let i = 0; i < 250; i++) {
        instance.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'exp', ?, ?, ?)",
          `late-exp-msg-${i}`,
          `late-exp-key-${i}`,
          LATE_T - 2000,
          LATE_T - 1000,
        );
      }
      instance.sql.exec(
        'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        lateLiveClaim,
        'projA',
        'src/late-live.ts',
        'worker-1',
        LATE_T,
        LATE_T + 120000,
      );
      expect(busCategorySnapshot(instance.sql, LATE_T).expiredMessages).toBe(250);
      expect(busCategorySnapshot(instance.sql, LATE_T).liveClaimIds).toEqual([lateLiveClaim]);
    });
    await freezeJobsClock(lateStub, LATE_T);
    await runInDurableObject(lateStub, async (instance: Hub) => {
      await instance.alarm();
      const lateAfter = busCategorySnapshot(instance.sql, LATE_T);
      expect(lateAfter.expiredMessages).toBe(150);
      expect(lateAfter.liveClaimIds).toEqual([lateLiveClaim]);
      expect(
        instance.sql.exec('SELECT id FROM messages WHERE id = ?', 'late-valid-msg').toArray(),
      ).toHaveLength(1);
    });

    // 3. Mixed named-hub state: unique IDs, snapshot every expired category, exact 100 total.
    const mixPrefix = `mix-exp-msg-${crypto.randomUUID()}-`;
    const mixValidId = `mix-valid-msg-${crypto.randomUUID()}`;
    const genuineLiveClaim = `genuine-live-claim-${crypto.randomUUID()}`;
    let mixBefore = {
      expiredMessages: 0,
      expiredClaims: 0,
      liveClaimIds: [] as string[],
    };
    await freezeJobsClock(stub, EARLY_T);
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'valid', ?, ?, ?)",
        mixValidId,
        `mix-valid-key-${mixValidId}`,
        EARLY_T,
        EARLY_T + 30 * 24 * 3600 * 1000,
      );
      for (let i = 0; i < 250; i++) {
        instance.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'exp', ?, ?, ?)",
          `${mixPrefix}${i}`,
          `${mixPrefix}key-${i}`,
          EARLY_T - 2000,
          EARLY_T - 1000,
        );
      }
      instance.sql.exec(
        'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        genuineLiveClaim,
        'projA',
        `src/genuine-${genuineLiveClaim}.ts`,
        'worker-1',
        EARLY_T,
        EARLY_T + 120000,
      );
      mixBefore = busCategorySnapshot(instance.sql, EARLY_T);
    });
    const mixEligible = mixBefore.expiredMessages + mixBefore.expiredClaims;
    expect(mixEligible).toBeGreaterThanOrEqual(100);
    expect(mixBefore.liveClaimIds).toContain(genuineLiveClaim);
    const tickFutureRes = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ now: '2099-01-01T00:00:00Z' })
    }));
    expect(tickFutureRes.status).toBe(200);
    const tickJson = await tickFutureRes.json() as any;
    await runInDurableObject(stub, async (instance: Hub) => {
      expect(
        instance.sql.exec('SELECT id FROM messages WHERE id = ?', mixValidId).toArray(),
      ).toHaveLength(1);
      const mixAfter = busCategorySnapshot(instance.sql, EARLY_T);
      const deletedMessages = mixBefore.expiredMessages - mixAfter.expiredMessages;
      const deletedClaims = mixBefore.expiredClaims - mixAfter.expiredClaims;
      const totalDeleted = deletedMessages + deletedClaims;
      expect(mixAfter.liveClaimIds).toEqual(mixBefore.liveClaimIds);
      expect(mixAfter.liveClaimIds).toContain(genuineLiveClaim);
      expect(totalDeleted).toBe(100);
      expect(tickJson.retention.messagesDeleted + tickJson.retention.claimsDeleted).toBe(100);
      expect(mixAfter.expiredMessages + mixAfter.expiredClaims).toBe(mixEligible - 100);
      expect(tickJson.retention.hasMore).toBe(mixEligible - 100 > 0);
      const mixRemain = countSql(
        instance.sql,
        'SELECT COUNT(*) AS c FROM messages WHERE id LIKE ?',
        `${mixPrefix}%`,
      );
      expect(mixRemain).toBeGreaterThanOrEqual(0);
      expect(mixRemain).toBeLessThanOrEqual(250);
      expect(mixAfter.expiredMessages).toBeGreaterThanOrEqual(mixRemain);
    });

    // 4. Authorized public maintenance route uses Date.now() (caller body.now inert).
    const publicNow = Date.now();
    const publicLiveClaim = `public-genuine-live-${crypto.randomUUID()}`;
    let publicBefore = {
      expiredMessages: 0,
      expiredClaims: 0,
      liveClaimIds: [] as string[],
    };
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.sql.exec(
        'INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
        publicLiveClaim,
        'projA',
        `src/public-${publicLiveClaim}.ts`,
        'worker-1',
        publicNow,
        publicNow + 120000,
      );
      publicBefore = busCategorySnapshot(instance.sql, publicNow);
    });
    const publicEligible = publicBefore.expiredMessages + publicBefore.expiredClaims;
    expect(publicEligible).toBeGreaterThanOrEqual(100);
    expect(publicBefore.liveClaimIds).toContain(publicLiveClaim);
    const adminMaintainRes = await doFetch(new Request('http://example.com/api/cron/retention', {
      method: 'POST',
      headers: {
        'Cookie': adminCookie,
        'X-CSRF-Token': 'valid-csrf-token',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ now: '2099-01-01T00:00:00Z' }) // caller body.now inert
    }));
    expect(adminMaintainRes.status).toBe(200);
    const maintainData = await adminMaintainRes.json() as any;
    expect(maintainData.status).toBe('ok');
    await runInDurableObject(stub, async (instance: Hub) => {
      expect(
        instance.sql.exec('SELECT id FROM messages WHERE id = ?', mixValidId).toArray(),
      ).toHaveLength(1);
      const publicAfter = busCategorySnapshot(instance.sql, publicNow);
      expect(publicAfter.liveClaimIds).toEqual(publicBefore.liveClaimIds);
      expect(publicAfter.liveClaimIds).toContain(publicLiveClaim);
      const publicDeleted =
        publicBefore.expiredMessages -
        publicAfter.expiredMessages +
        (publicBefore.expiredClaims - publicAfter.expiredClaims);
      expect(publicDeleted).toBe(100);
      expect(
        Number(maintainData.messagesDeleted ?? 0) + Number(maintainData.claimsDeleted ?? 0),
      ).toBe(100);
      expect(maintainData.hasMore).toBe(publicEligible - 100 > 0);
    });
  });

  it('9. Adversarial Same-Host Impersonation: Deny cross-actor send/read/stages/reply/claims, allow canonical project aliases', async () => {
    // 1. Cross-actor send on same machine (mach-A): worker-1 tries to send as principal-1
    const badSendRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_actor: 'principal-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Impersonating principal on same host',
        idempotency_key: 'spoof-send-same-host'
      })
    }));
    expect(badSendRes.status).toBe(403);

    // 2. Legitimate send as self: worker-1 sends as worker-1
    const goodSendRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_actor: 'worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Legitimate self send',
        idempotency_key: 'legit-send-self'
      })
    }));
    expect(goodSendRes.status).toBe(200);

    // 3. Canonical project alias: worker-1 sends as projA:worker-1
    const canonAliasRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_actor: 'projA:worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Canonical alias send',
        idempotency_key: 'canon-alias-send'
      })
    }));
    expect(canonAliasRes.status).toBe(200);

    // 4. Mismatched project alias: worker-1 sends as projB:worker-1
    const badAliasRes = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_agent_1', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_actor: 'projB:worker-1',
        to_actor: 'worker-2',
        kind: 'note',
        body: 'Mismatched project alias send',
        idempotency_key: 'bad-alias-send'
      })
    }));
    expect(badAliasRes.status).toBe(403);

    // 5. Cross-actor inbox read on same machine: worker-1 tries to read inbox of principal-1
    const badInboxRes = await doFetch(new Request('http://example.com/api/bus/inbox?actor=principal-1', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(badInboxRes.status).toBe(403);
  });

  it('10. Actors and Messages Listing Routes: Scoped Authorization and Negative Proofs', async () => {
    // Seed messages across projects:
    // Message 1: projA (worker-1 <-> worker-2)
    // Message 2: projB/C (worker-2 <-> worker-3)
    await runInDurableObject(stub, async (instance: Hub) => {
      const now = Date.now();
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('msg-w1-w2', 'worker-1', 'worker-2', 'note', 'th1', 'hello from w1', 'idemp-w1-w2', ?, ?)",
        now - 2000, now + 86400000
      );
      instance.sql.exec(
        "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES ('msg-w2-w3', 'worker-2', 'worker-3', 'note', 'th2', 'hello from w2', 'idemp-w2-w3', ?, ?)",
        now - 1000, now + 86400000
      );
    });

    // --- GET /api/bus/actors ---
    // A. Host A listing: scoped to host project/machine actors on mach-A
    const hostActorsRes = await doFetch(new Request('http://example.com/api/bus/actors', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(hostActorsRes.status).toBe(200);
    const hostActors = await hostActorsRes.json() as string[];
    expect(Array.isArray(hostActors)).toBe(true);
    expect(hostActors).toContain('projA:worker-1');
    expect(hostActors).toContain('machine:mach-A');
    expect(hostActors).not.toContain('projB:worker-2');
    expect(hostActors).not.toContain('machine:mach-B');
    expect(hostActors).not.toContain('role:root');

    // B. Negative proof: Agent 1 (worker-1) cannot retrieve all actors across projects
    const agentActorsRes = await doFetch(new Request('http://example.com/api/bus/actors', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(agentActorsRes.status).toBe(200);
    const agentActors = await agentActorsRes.json() as string[];
    expect(Array.isArray(agentActors)).toBe(true);
    expect(agentActors).toContain('projA:worker-1');
    expect(agentActors).not.toContain('projB:worker-2'); // Cross-project actor excluded
    expect(agentActors).not.toContain('projC:worker-3'); // Cross-project actor excluded
    expect(agentActors).not.toContain('machine:mach-A');
    expect(agentActors).not.toContain('machine:mach-B');
    expect(agentActors).not.toContain('role:root');

    // C. Admin listing: all actors returned, but role:root remains denied / excluded
    let adminCookie = '';
    await runInDurableObject(stub, async (instance: Hub) => {
      adminCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'admin-csrf',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });
    const adminActorsRes = await doFetch(new Request('http://example.com/api/bus/actors', {
      method: 'GET',
      headers: { 'Cookie': adminCookie }
    }));
    expect(adminActorsRes.status).toBe(200);
    const adminActors = await adminActorsRes.json() as string[];
    expect(adminActors).toContain('projA:worker-1');
    expect(adminActors).toContain('projB:worker-2');
    expect(adminActors).toContain('machine:mach-A');
    expect(adminActors).toContain('machine:mach-B');
    expect(adminActors).not.toContain('role:root'); // role:root remains denied

    // --- GET /api/bus/messages ---
    // D. Negative proof: Agent 1 cannot retrieve actor2 (worker-2) messages
    const badAgentMsgRes = await doFetch(new Request('http://example.com/api/bus/messages?actor=worker-2', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(badAgentMsgRes.status).toBe(403);

    // E. Negative proof: Agent 1 cannot retrieve role:root messages (role:root remains denied)
    const rootAgentMsgRes = await doFetch(new Request('http://example.com/api/bus/messages?actor=role:root', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(rootAgentMsgRes.status).toBe(403);

    // F. Negative proof: Host A cannot retrieve actor on Host B (worker-2)
    const badHostMsgRes = await doFetch(new Request('http://example.com/api/bus/messages?actor=worker-2', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(badHostMsgRes.status).toBe(403);

    // G. Derive current principal binding: Agent 1 lists messages without ?actor=
    // Scoped strictly to worker-1, does not leak worker-2 <-> worker-3 message
    const agentSelfMsgRes = await doFetch(new Request('http://example.com/api/bus/messages', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_agent_1' }
    }));
    expect(agentSelfMsgRes.status).toBe(200);
    const agentMessages = await agentSelfMsgRes.json() as any[];
    expect(Array.isArray(agentMessages)).toBe(true);
    expect(agentMessages.some((m: any) => m.id === 'msg-w1-w2')).toBe(true);
    expect(agentMessages.some((m: any) => m.id === 'msg-w2-w3')).toBe(false);

    // H. Host A lists messages without ?actor=
    // Scoped to actors hosted on mach-A, does not leak worker-2 <-> worker-3 message
    const hostMsgRes = await doFetch(new Request('http://example.com/api/bus/messages', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(hostMsgRes.status).toBe(200);
    const hostMessages = await hostMsgRes.json() as any[];
    expect(Array.isArray(hostMessages)).toBe(true);
    expect(hostMessages.some((m: any) => m.id === 'msg-w1-w2')).toBe(true);
    expect(hostMessages.some((m: any) => m.id === 'msg-w2-w3')).toBe(false);

    // I. Admin queries messages
    const adminMsgRes = await doFetch(new Request('http://example.com/api/bus/messages', {
      method: 'GET',
      headers: { 'Cookie': adminCookie }
    }));
    expect(adminMsgRes.status).toBe(200);
    const adminMessages = await adminMsgRes.json() as any[];
    expect(adminMessages.some((m: any) => m.id === 'msg-w1-w2')).toBe(true);
    expect(adminMessages.some((m: any) => m.id === 'msg-w2-w3')).toBe(true);

    // J. Admin query for role:root fails closed (role:root remains denied)
    const adminRootMsgRes = await doFetch(new Request('http://example.com/api/bus/messages?actor=role:root', {
      method: 'GET',
      headers: { 'Cookie': adminCookie }
    }));
    expect(adminRootMsgRes.status).toBe(403);
  });
});
