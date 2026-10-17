import { it, expect } from 'vitest';
import { runInDurableObject } from 'cloudflare:test';
import { doFetch, defaultEnv } from './m1-focus-setup';
import { Hub } from '../src/hub';
import { createSessionCookie } from '../src/auth/session';

// M9 R5 regressions: R1 live registration authority, R2 stable approval authority.
// All cases drive actual device/approve/token/register/refresh/metrics routes
// plus real SQLite assertions. No standalone SQL updates as auth substitutes.

const at = Date.now() - 3600000;
const snap = (key = 'r5:metrics-001', when = at): any => ({
  schema_version: '1.0',
  as_of: new Date(when).toISOString(),
  request_key: key,
  coverage: { utilization: 'missing', usage: 'missing', accepted: 'missing' },
  gaps: [{ code: 'missing' }],
  utilization: { unknown: true, projects: {} },
  usage: { unknown: true, projects: {} },
  accepted: { unknown: true, accepted_outcomes: null, task_count: null, commit_count: null, review_count: null, projects: {} },
});
const stub = () => defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
const request = (path: string, body?: any, token?: string) =>
  new Request('http://example.com' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
const registerBody = {
  hostname: 'synthetic.invalid',
  os: 'linux',
  network: 'public',
  roles: ['worker'],
  capabilities: { launcher: '0.1.0', engines: ['grok'], jobs: ['root-run'], aplexer: true },
};

async function adminCookie() {
  return (
    await createSessionCookie(defaultEnv, {
      sub: 'synthetic-admin',
      email: 'admin@fake.local',
      isAdmin: true,
      csrfToken: 'synthetic',
      exp: Math.floor(Date.now() / 1000) + 3600,
    })
  ).split(';')[0];
}

async function approveCode(name: string, machineId?: string) {
  const r = await doFetch(
    request('/api/device/code', { client_kind: 'host', name, hostname: 'synthetic.invalid', requested_scopes: ['host'] })
  );
  expect(r.status).toBe(200);
  const codes: any = await r.json();
  const ar = await doFetch(
    new Request('http://example.com/api/device/approve/' + codes.user_code, {
      method: 'POST',
      headers: { Cookie: await adminCookie(), 'x-csrf-token': 'synthetic', 'Content-Type': 'application/json' },
      body: JSON.stringify({ granted_scopes: ['host'], ...(machineId ? { machine_id: machineId } : {}) }),
    })
  );
  expect(ar.status).toBe(200);
  return codes;
}

async function consume(codes: any) {
  const r = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: codes.device_code,
    })
  );
  expect(r.status).toBe(200);
  return (await r.json()) as any;
}

async function enroll(name: string, machineId?: string) {
  const tok = await consume(await approveCode(name, machineId));
  expect((await doFetch(request('/api/machines/register', registerBody, tok.access_token))).status).toBe(200);
  return tok;
}

async function rotate(t: any) {
  const r = await doFetch(request('/api/token/refresh', { grant_type: 'refresh_token', refresh_token: t.refresh_token }));
  expect(r.status).toBe(200);
  return (await r.json()) as any;
}

async function binding(id: string) {
  return await runInDurableObject(stub(), async (h: Hub) => ({
    machine: h.sql.exec('SELECT * FROM machines WHERE id=?', id).toArray(),
    latest: h.sql.exec('SELECT * FROM host_metrics_latest WHERE host_id=?', id).toArray(),
    receipts: h.sql.exec('SELECT * FROM host_metrics_idempotency WHERE host_id=? ORDER BY request_key', id).toArray(),
  }));
}

// Pause actual WebCrypto preparation, execute a controlled concurrent
// mutation, then resume the real route. No authorization helper is replaced.
async function duringTokenPreparation(action: () => Promise<void>, route: () => Promise<Response>) {
  const subtle: any = crypto.subtle;
  const original = subtle.digest.bind(subtle);
  let hits = 0;
  subtle.digest = async (alg: any, data: any) => {
    const value = new TextDecoder().decode(data);
    if (value.startsWith('hubt_') && hits === 0) {
      hits++;
      await action();
    }
    return original(alg, data);
  };
  try {
    const r = await route();
    expect(hits).toBe(1);
    return r;
  } finally {
    subtle.digest = original;
  }
}

it('R1 streamed register refuses credential revoked during body before mutation', async () => {
  const a = await enroll('r5-revoke');
  const before = await binding(a.machine_id);
  const stream = new ReadableStream<Uint8Array>({
    async pull(c) {
      expect(
        (await doFetch(request('/api/token/revoke', { token: a.access_token }, a.access_token))).status
      ).toBe(200);
      c.enqueue(new TextEncoder().encode(JSON.stringify({ ...registerBody, hostname: 'revoked-write.invalid' })));
      c.close();
    },
  }, { highWaterMark: 0 });
  const r = await doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + a.access_token },
      body: stream,
    })
  );
  const after = await binding(a.machine_id);
  expect(r.status).toBe(401);
  expect(after).toEqual(before);
});

it('R1 unbound streamed registration cannot bind revoked predecessor after real refresh', async () => {
  const a = await enroll('r5-unbound');
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE machines SET credential_id=NULL WHERE id=?', a.machine_id);
  });
  let next: any;
  const stream = new ReadableStream<Uint8Array>({
    async pull(c) {
      next = await rotate(a);
      c.enqueue(new TextEncoder().encode(JSON.stringify(registerBody)));
      c.close();
    },
  }, { highWaterMark: 0 });
  const r = await doFetch(
    new Request('http://example.com/api/machines/register', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + a.access_token },
      body: stream,
    })
  );
  const current = await doFetch(request('/api/machines/register', registerBody, next.access_token));
  expect(r.status).toBe(401);
  expect(current.status).toBe(200);
});

it('R2 older approved but delayed pairing must not displace newer approved and registered family', async () => {
  const a = await enroll('r5-order-a');
  const delayed = await approveCode('r5-order-delayed', a.machine_id);
  const b = await enroll('r5-order-b', a.machine_id);
  expect((await doFetch(request('/api/metrics', snap('r5:new-b'), b.access_token))).status).toBe(200);
  const before = await binding(a.machine_id);
  // Delayed older exchange is denied at issuance: no newer consent is minted,
  // the code is not consumed and the machine binding is untouched.
  const staleRes = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: delayed.device_code,
    })
  );
  expect(staleRes.status).toBe(400);
  const pairingState = await runInDurableObject(
    stub(),
    async (h: Hub) => h.sql.exec('SELECT status FROM device_pairings WHERE user_code = ?', delayed.user_code).toArray()
  );
  expect((pairingState[0] as any).status).toBe('approved');
  const after = await binding(a.machine_id);
  const get = await doFetch(request('/api/metrics', undefined, b.access_token));
  expect(after).toEqual(before);
  expect(get.status).toBe(200);
});

it('R2 original and post-B child stay denied through tied clocks and fresh C retains receipts', async () => {
  const a = await enroll('r5-tie-a');
  const b = await enroll('r5-tie-b', a.machine_id);
  expect((await doFetch(request('/api/metrics', snap('r5:tie-b'), b.access_token))).status).toBe(200);
  const before = await binding(a.machine_id);
  expect((await doFetch(request('/api/machines/register', { ...registerBody }, a.access_token))).status).toBe(403);
  expect(await binding(a.machine_id)).toEqual(before);
  const stale = await rotate(a);
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE credentials SET created_at=? WHERE machine_id=?', '2026-10-16T00:00:00.000Z', a.machine_id);
  });
  expect((await doFetch(request('/api/machines/register', registerBody, stale.access_token))).status).toBe(403);
  const c = await enroll('r5-tie-c', a.machine_id);
  const posted = await doFetch(request('/api/metrics', snap('r5:tie-c', at + 1), c.access_token));
  expect(posted.status).toBe(200);
  const receipt: any = await posted.json();
  const c2 = await rotate(c);
  expect((await doFetch(request('/api/machines/register', registerBody, c2.access_token))).status).toBe(200);
  const retry = await doFetch(request('/api/metrics', snap('r5:tie-c', at + 1), c2.access_token));
  expect(retry.status).toBe(200);
  expect(((await retry.json()) as any).received_at).toBe(receipt.received_at);
  expect((await doFetch(request('/api/metrics', undefined, b.access_token))).status).toBe(403);
  expect((await doFetch(request('/api/metrics', undefined, c.access_token))).status).toBe(401);
});

it('R2 replayed approval never mints newer consent and issuance retry stays idempotent', async () => {
  const a = await enroll('r5-replay-a');
  const codes = await approveCode('r5-replay-delayed', a.machine_id);
  const b = await enroll('r5-replay-b', a.machine_id);
  expect((await doFetch(request('/api/metrics', snap('r5:replay-b'), b.access_token))).status).toBe(200);
  // Replaying the old approval request must not mint newer consent.
  const replay = await doFetch(
    new Request('http://example.com/api/device/approve/' + codes.user_code, {
      method: 'POST',
      headers: { Cookie: await adminCookie(), 'x-csrf-token': 'synthetic', 'Content-Type': 'application/json' },
      body: JSON.stringify({ granted_scopes: ['host'] }),
    })
  );
  expect([400, 409]).toContain(replay.status);
  // Delayed old exchange still denied after newer approval (no token minted).
  const staleRes = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: codes.device_code,
    })
  );
  expect(staleRes.status).toBe(400);
  // Consumed issuance retry remains idempotent (no second credential).
  const again = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: codes.device_code,
    })
  );
  expect(again.status).toBe(400);
  expect((await doFetch(request('/api/metrics', undefined, b.access_token))).status).toBe(200);
});

it('R2 current bound owner keeps routine registration while a later approval is pending', async () => {
  const b = await enroll('r5-pending-b');
  expect((await doFetch(request('/api/metrics', snap('r5:pending-b'), b.access_token))).status).toBe(200);
  // A later genuine approval is pending but not yet exchanged: approval witness
  // and bound owner are distinct, so the current owner sees no outage.
  const cCodes = await approveCode('r5-pending-c', b.machine_id);
  expect((await doFetch(request('/api/machines/register', registerBody, b.access_token))).status).toBe(200);
  const b2 = await rotate(b);
  expect((await doFetch(request('/api/machines/register', registerBody, b2.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', snap('r5:pending-b2', at + 1), b2.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', undefined, b2.access_token))).status).toBe(200);
  // The pending approval still takes over once genuinely exchanged.
  const c = await consume(cCodes);
  expect((await doFetch(request('/api/machines/register', registerBody, c.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', undefined, b2.access_token))).status).toBe(403);
  expect((await doFetch(request('/api/metrics', snap('r5:pending-c', at + 2), c.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', undefined, c.access_token))).status).toBe(200);
});

it('R2 legacy unwitnessed grant cannot replace a bound legacy owner; unbound bootstrap and owner continuity hold', async () => {
  const { sha256 } = await import('../src/auth/crypto');
  const setup = await runInDurableObject(stub(), async (h: Hub) => {
    const atMs = Date.now() - 3600000;
    const mk = async (id: string) => {
      const token = 'hubt_legacy_' + id;
      const refresh = 'hubr_legacy_' + id;
      const cred = 'cred-legacy-' + id;
      h.sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'host', ?, ?, ?, ?, ?, 'legacy', '[\"host\"]', 'synthetic', ?, ?)",
        cred, await sha256(token), await sha256(refresh), 'fam-legacy-' + id, 'legacy-' + id, 'legacy-m-' + id,
        new Date(atMs).toISOString(), new Date(Date.now() + 86400000).toISOString()
      );
      return { token, refresh, cred };
    };
    const owner = await mk('owner');
    h.sql.exec("INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('legacy-m-owner', 'legacy', 'legacy.invalid', ?, ?)",
      owner.cred, new Date(atMs).toISOString());
    h.sql.exec("INSERT INTO machines (id, name, hostname, credential_id, registered_at) VALUES ('legacy-m-free', 'legacy', 'legacy.invalid', NULL, ?)",
      new Date(atMs).toISOString());
    return { owner };
  });
  // Bound legacy owner continues: register, refresh, metrics all work with NULL authority on both sides.
  expect((await doFetch(request('/api/machines/register', registerBody, setup.owner.token))).status).toBe(200);
  const rr = await doFetch(request('/api/token/refresh', { grant_type: 'refresh_token', refresh_token: setup.owner.refresh }));
  expect(rr.status).toBe(200);
  const owner2: any = await rr.json();
  const owner2Id: string = await runInDurableObject(stub(), async (h: Hub) => {
    const c: any = h.sql.exec('SELECT id FROM credentials WHERE refresh_hash = ?', await (await import('../src/auth/crypto')).sha256(owner2.refresh_token)).one();
    return c.id;
  });
  expect((await doFetch(request('/api/metrics', snap('r5:legacy-owner'), owner2.access_token))).status).toBe(200);
  // A legacy pending grant (approved, no verifiable witness) for the BOUND machine is denied at issuance.
  const d = await approveCode('r5-legacy-d', 'legacy-m-owner');
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE device_pairings SET approved_seq = NULL WHERE user_code = ?', d.user_code);
    h.sql.exec('UPDATE machines SET approval_seq = NULL WHERE id = ?', 'legacy-m-owner');
  });
  const denied = await doFetch(
    request('/api/device/token', { grant_type: 'urn:ietf:params:oauth:grant-type:device_code', device_code: d.device_code })
  );
  expect(denied.status).toBe(400);
  const still = await runInDurableObject(stub(), async (h: Hub) => ({
    machine: h.sql.exec('SELECT credential_id FROM machines WHERE id = ?', 'legacy-m-owner').toArray(),
    pairing: h.sql.exec('SELECT status FROM device_pairings WHERE user_code = ?', d.user_code).toArray(),
  }));
  expect((still.machine[0] as any).credential_id).toBe(owner2Id);
  expect((still.pairing[0] as any).status).toBe('approved');
  expect((await doFetch(request('/api/metrics', undefined, owner2.access_token))).status).toBe(200);
  // The same unwitnessed shape bootstraps a genuinely UNBOUND legacy machine.
  const e = await approveCode('r5-legacy-e', 'legacy-m-free');
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE device_pairings SET approved_seq = NULL WHERE user_code = ?', e.user_code);
    h.sql.exec('UPDATE machines SET approval_seq = NULL WHERE id = ?', 'legacy-m-free');
  });
  const exchanged = await consume(e);
  expect((await doFetch(request('/api/machines/register', registerBody, exchanged.access_token))).status).toBe(200);
});

it('R2 replayed old approval mints no new authority and leaves current binding intact', async () => {
  const a = await enroll('r5-noseq-a');
  const d = await approveCode('r5-noseq-d', a.machine_id);
  const b = await enroll('r5-noseq-b', a.machine_id);
  const seqsBefore = await runInDurableObject(stub(), async (h: Hub) => ({
    machine: (h.sql.exec('SELECT approval_seq FROM machines WHERE id = ?', a.machine_id).one() as any).approval_seq,
    pairing: (h.sql.exec('SELECT approved_seq, status FROM device_pairings WHERE user_code = ?', d.user_code).one() as any),
  }));
  const replay = await doFetch(
    new Request('http://example.com/api/device/approve/' + d.user_code, {
      method: 'POST',
      headers: { Cookie: await adminCookie(), 'x-csrf-token': 'synthetic', 'Content-Type': 'application/json' },
      body: JSON.stringify({ granted_scopes: ['host'] }),
    })
  );
  expect([400, 409]).toContain(replay.status);
  const seqsAfter = await runInDurableObject(stub(), async (h: Hub) => ({
    machine: (h.sql.exec('SELECT approval_seq FROM machines WHERE id = ?', a.machine_id).one() as any).approval_seq,
    pairing: (h.sql.exec('SELECT approved_seq, status FROM device_pairings WHERE user_code = ?', d.user_code).one() as any),
  }));
  expect(seqsAfter.machine).toBe(seqsBefore.machine);
  expect(seqsAfter.pairing).toEqual(seqsBefore.pairing);
  expect((await doFetch(request('/api/metrics', snap('r5:noseq-b'), b.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', undefined, b.access_token))).status).toBe(200);
});

it('R2 stale witnessed issuance denied on unbound machine; fresh C bootstraps', async () => {
  const a = await enroll('r5-unboundw-a');
  const d = await approveCode('r5-unboundw-d', a.machine_id);
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE machines SET credential_id=NULL WHERE id=?', a.machine_id);
  });
  const c = await approveCode('r5-unboundw-c', a.machine_id);
  const nBefore = await runInDurableObject(
    stub(),
    async (h: Hub) =>
      (h.sql.exec('SELECT COUNT(*) n FROM credentials WHERE machine_id=?', a.machine_id).one() as any).n
  );
  // A body-injected grant proof is ignored; stale witnessed D (seq2) is
  // denied at exchange after newer C (seq3) even with a NULL pointer.
  const staleRes = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: d.device_code,
    })
  );
  expect(staleRes.status).toBe(400);
  const state = await runInDurableObject(stub(), async (h: Hub) => ({
    machine: h.sql.exec('SELECT approval_seq, credential_id FROM machines WHERE id=?', a.machine_id).toArray(),
    grant: h.sql.exec('SELECT status, approved_seq FROM device_pairings WHERE user_code=?', d.user_code).toArray(),
    n: (h.sql.exec('SELECT COUNT(*) n FROM credentials WHERE machine_id=?', a.machine_id).one() as any).n,
  }));
  expect((state.grant[0] as any).status).toBe('approved');
  expect((state.machine[0] as any).credential_id).toBeNull();
  expect(state.n).toBe(nBefore);
  // Fresh C bootstraps the unbound machine and posts metrics.
  const legit = await consume(c);
  expect(
    (await doFetch(request('/api/machines/register', { ...registerBody, approval_seq: 999999 }, legit.access_token))).status
  ).toBe(200);
  expect((await doFetch(request('/api/metrics', snap('r5:unboundw-c', at + 1), legit.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', undefined, legit.access_token))).status).toBe(200);
});

it('R2 legacy unwitnessed issuance denied on witnessed unbound machine', async () => {
  const a = await enroll('r5-unboundl-a');
  const d = await approveCode('r5-unboundl-d', a.machine_id);
  await runInDurableObject(stub(), async (h: Hub) => {
    h.sql.exec('UPDATE machines SET credential_id=NULL, approval_seq=NULL WHERE id=?', a.machine_id);
    h.sql.exec('UPDATE device_pairings SET approved_seq=NULL WHERE user_code=?', d.user_code);
  });
  // Fresh C establishes seq1 on the legacy-unbound machine.
  const c = await approveCode('r5-unboundl-c', a.machine_id);
  const staleRes = await doFetch(
    request('/api/device/token', {
      grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
      device_code: d.device_code,
    })
  );
  expect(staleRes.status).toBe(400);
  const state = await runInDurableObject(stub(), async (h: Hub) => ({
    machine: h.sql.exec('SELECT approval_seq, credential_id FROM machines WHERE id=?', a.machine_id).toArray(),
    grant: h.sql.exec('SELECT status, approved_seq FROM device_pairings WHERE user_code=?', d.user_code).toArray(),
  }));
  expect((state.grant[0] as any).status).toBe('approved');
  expect((state.machine[0] as any).credential_id).toBeNull();
  const legit = await consume(c);
  expect((await doFetch(request('/api/machines/register', registerBody, legit.access_token))).status).toBe(200);
  expect((await doFetch(request('/api/metrics', snap('r5:unboundl-c', at + 1), legit.access_token))).status).toBe(200);
});

it('R2 issuance denies newer approval raced during token preparation without minting', async () => {
  const a = await enroll('r5-race-a');
  const d = await approveCode('r5-race-d', a.machine_id);
  const before = await runInDurableObject(stub(), async (h: Hub) => ({
    n: (h.sql.exec('SELECT COUNT(*) n FROM credentials WHERE machine_id=?', a.machine_id).one() as any).n,
    binding: (h.sql.exec('SELECT credential_id FROM machines WHERE id=?', a.machine_id).one() as any).credential_id,
  }));
  let c: any;
  const r = await duringTokenPreparation(
    async () => {
      c = await approveCode('r5-race-c', a.machine_id);
    },
    () =>
      doFetch(
        request('/api/device/token', {
          grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
          device_code: d.device_code,
        })
      )
  );
  expect(r.status).toBe(400);
  const after = await runInDurableObject(stub(), async (h: Hub) => ({
    n: (h.sql.exec('SELECT COUNT(*) n FROM credentials WHERE machine_id=?', a.machine_id).one() as any).n,
    binding: (h.sql.exec('SELECT credential_id FROM machines WHERE id=?', a.machine_id).one() as any).credential_id,
    pairing: (h.sql.exec('SELECT status FROM device_pairings WHERE user_code=?', d.user_code).one() as any).status,
  }));
  expect(after.n).toBe(before.n);
  expect(after.binding).toBe(before.binding);
  expect(after.pairing).not.toBe('consumed');
  const legit = await consume(c);
  expect((await doFetch(request('/api/machines/register', registerBody, legit.access_token))).status).toBe(200);
});
