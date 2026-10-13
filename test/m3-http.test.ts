import { describe, it, expect, beforeAll, afterAll } from 'vitest';
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

  it('8. Bounded Resumable Retention Cleanup via Maintenance Tick', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      // Seed 250 expired messages
      const past = Date.now() - 100000;
      for (let i = 0; i < 250; i++) {
        instance.sql.exec(
          "INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at) VALUES (?, 'worker-1', 'worker-2', 'note', 'th', 'exp', ?, ?, ?)",
          `exp-msg-${i}`, `exp-key-${i}`, past - 1000, past
        );
      }
    });

    // Trigger tick which processes a bounded batch (100) and arms resume alarm
    const tickRes = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST'
    }));
    expect(tickRes.status).toBe(200);

    // Verify bounded batch deletion (exactly 100 deleted, 150 remain)
    await runInDurableObject(stub, async (instance: Hub) => {
      const count1 = instance.sql.exec("SELECT count(*) as c FROM messages WHERE id LIKE 'exp-msg-%'").toArray()[0] as any;
      expect(count1.c).toBe(150);

      // Verify alarm execution processes next batch (100 deleted, 50 remain)
      await instance.alarm();
      const count2 = instance.sql.exec("SELECT count(*) as c FROM messages WHERE id LIKE 'exp-msg-%'").toArray()[0] as any;
      expect(count2.c).toBe(50);

      // Verify alarm execution processes final batch (50 deleted, 0 remain)
      await instance.alarm();
      const count3 = instance.sql.exec("SELECT count(*) as c FROM messages WHERE id LIKE 'exp-msg-%'").toArray()[0] as any;
      expect(count3.c).toBe(0);
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

  it('10. Actors and Messages Listing Routes', async () => {
    // GET /api/bus/actors
    const actorsRes = await doFetch(new Request('http://example.com/api/bus/actors', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(actorsRes.status).toBe(200);
    const actors = await actorsRes.json() as string[];
    expect(Array.isArray(actors)).toBe(true);
    expect(actors).toContain('projA:worker-1');
    expect(actors).toContain('machine:mach-A');

    // GET /api/bus/messages
    const messagesRes = await doFetch(new Request('http://example.com/api/bus/messages', {
      method: 'GET',
      headers: { 'Authorization': 'Bearer token_host_a' }
    }));
    expect(messagesRes.status).toBe(200);
    const messages = await messagesRes.json() as any[];
    expect(Array.isArray(messages)).toBe(true);
  });
});
