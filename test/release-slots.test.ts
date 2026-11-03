import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { ReleaseSlotService } from '../src/releases';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

// Intake 2c87c231: single-use, SHA-bound orchestrator release slots consumed
// by the CI deploy job. AC3 matrix: valid / missing / reused / wrong-SHA,
// plus expiry, revoke, authority and bounded-list behavior.
let now = 1_800_000_000_000;
const admin = { caller: { isAdmin: true, actor: 'orchestrator-main', kind: 'human' }, scopes: ['admin'] };
const agent = { caller: { actor: 'agent:a', kind: 'agent' }, scopes: ['bus:send'] };
const req = (path: string, data?: any, method = data === undefined ? 'GET' : 'POST') => new Request('https://hub.test' + path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
const SHA_A = 'a'.repeat(40);
const SHA_B = 'b'.repeat(40);
const slot = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`;
const SLOT_A = slot(1);
const SLOT_B = slot(2);
const SLOT_C = slot(3);
const SLOT_D = slot(4);
const SLOT_E = slot(5);
const SLOT_MISSING = slot(9999);

async function fixture(fn: (r: ReleaseSlotService, hub: Hub) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('release-slots-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const r = new ReleaseSlotService(hub.sql, () => now, cb => state.storage.transactionSync(cb));
    r.init();
    await fn(r, hub);
  });
}

const issue = (r: ReleaseSlotService, body: any, auth: any = admin) => r.handle(req('/api/releases/slots', body), auth);
// The public verify endpoint must work without any caller authority; the
// harness passes the same refused auth a real anonymous request produces.
const verify = (r: ReleaseSlotService, body: any) => r.handle(req('/api/releases/slots/verify', body), { error: Response.json({ error: 'unauthorized' }, { status: 401 }) });

describe('release slots (intake 2c87c231)', () => {
  it('AC3 valid: admin issues a slot for an exact SHA and one verify consumes it', async () => fixture(async (r, hub) => {
    expect((await issue(r, { sha: SHA_A }, agent)).status).toBe(403);
    expect((await issue(r, { sha: SHA_A }, { error: Response.json({ error: 'unauthorized' }, { status: 401 }) })).status).toBe(401);
    expect((await issue(r, { sha: 'abc' })).status).toBe(400);
    expect(((await issue(r, { sha: SHA_A })).status)).toBe(400); // SHA grants need a preselected trailer id.
    expect((await issue(r, { sha: SHA_A, extra: 1 })).status).toBe(400);
    expect((await issue(r, { sha: SHA_A, ttl_hours: 10000 })).status).toBe(400);
    expect((await issue(r, { sha: SHA_A, id: 'guessable' })).status).toBe(400);

    const issued = await issue(r, { sha: SHA_A.toUpperCase(), id: SLOT_A, note: 'bootstrap deploy', ttl_hours: 1 });
    expect(issued.status).toBe(201);
    const record: any = await issued.json();
    expect(record).toMatchObject({ id: SLOT_A, sha: SHA_A, target: SHA_A, status: 'issued', issued_by: 'orchestrator-main' });

    // Wrong-SHA verify refuses without consuming; the slot stays issued.
    const mismatch = await verify(r, { slot: SLOT_A, target: SHA_B });
    expect(mismatch.status).toBe(403);
    expect(((await mismatch.json()) as any).error).toBe('release_slot_target_mismatch');
    expect(((await (await r.handle(req('/api/releases/slots/' + SLOT_A), admin)).json()) as any).status).toBe('issued');

    // Push CI validates the grant but leaves it available for the later deploy.
    const checked = await verify(r, { slot: SLOT_A, target: SHA_A, consume: false });
    expect(checked.status).toBe(200);
    expect(await checked.json()).toMatchObject({ ok: true, verified: true, consumed: false, id: SLOT_A });
    expect(((await (await r.handle(req('/api/releases/slots/' + SLOT_A), admin)).json()) as any).status).toBe('issued');

    // Valid verify consumes exactly once.
    const ok = await verify(r, { slot: SLOT_A, target: SHA_A });
    expect(ok.status).toBe(200);
    expect(await ok.json()).toMatchObject({ ok: true, id: SLOT_A, target: SHA_A, consumed_at: now });

    // Reused slot refuses with the single-use reason.
    const reused = await verify(r, { slot: SLOT_A, target: SHA_A });
    expect(reused.status).toBe(409);
    expect(((await reused.json()) as any).error).toBe('release_slot_already_consumed');

    // Exactly one consume audit row: the mismatch attempt wrote nothing.
    const consumeRows = hub.sql.exec("SELECT COUNT(*) AS n FROM audit WHERE action = 'release_slot_consume'").toArray() as any[];
    expect(consumeRows[0].n).toBe(1);
  }));

  it('AC3 missing: unknown slot refuses; issue conflicts are bounded and explicit', async () => fixture(async r => {
    const missing = await verify(r, { slot: SLOT_MISSING, target: SHA_A });
    expect(missing.status).toBe(404);
    expect(((await missing.json()) as any).error).toBe('release_slot_not_found');

    expect((await verify(r, { slot: 'short', target: SHA_A })).status).toBe(400);
    expect((await verify(r, { slot: SLOT_A, target: '../etc' })).status).toBe(400);

    const first: any = await (await issue(r, { sha: SHA_A, id: SLOT_A })).json();
    expect(first.status).toBe('issued');
    // A second live slot for the same SHA is refused while one is active.
    const conflict = await issue(r, { sha: SHA_A, id: SLOT_B });
    expect(conflict.status).toBe(409);
    expect(((await conflict.json()) as any).error).toBe('release_slot_target_active');
    // Same id twice is refused.
    expect((await issue(r, { sha: SHA_B, id: SLOT_A })).status).toBe(409);
    // Revoking frees the target for a fresh slot; non-admins cannot revoke.
    expect((await r.handle(req('/api/releases/slots/' + SLOT_A + '/revoke', {}), admin)).status).toBe(200);
    expect((await issue(r, { sha: SHA_A, id: SLOT_C })).status).toBe(201);
    expect((await r.handle(req('/api/releases/slots/' + SLOT_C + '/revoke', {}), agent)).status).toBe(403);
    expect((await r.handle(req('/api/releases/slots/' + SLOT_MISSING + '/revoke', {}), admin)).status).toBe(404);
  }));

  it('expiry: an expired slot refuses and frees the target for re-issue', async () => fixture(async r => {
    const issued: any = await (await issue(r, { sha: SHA_A, id: SLOT_D, ttl_hours: 1 })).json();
    now += 2 * 3600000;
    const expired = await verify(r, { slot: SLOT_D, target: SHA_A });
    expect(expired.status).toBe(409);
    expect(((await expired.json()) as any).error).toBe('release_slot_expired');
    // Expired predecessors are superseded in-transaction; no cleanup call.
    expect((await issue(r, { sha: SHA_A, id: SLOT_E })).status).toBe(201);
    expect(issued.expires_at).toBeLessThan(now);
  }));

  it('list is bounded and filterable', async () => fixture(async r => {
    for (let i = 0; i < 7; i++) {
      const issued = await issue(r, { sha: i.toString(16).repeat(40), id: slot(100 + i) });
      expect(issued.status).toBe(201);
    }
    const listed: any = await (await r.handle(req('/api/releases/slots?limit=3'), admin)).json();
    expect(listed.slots.length).toBe(3);
    expect(listed.limit).toBe(3);
    await verify(r, { slot: listed.slots[0].id, target: listed.slots[0].target });
    const consumedOnly: any = await (await r.handle(req('/api/releases/slots?status=consumed'), admin)).json();
    expect(consumedOnly.slots.length).toBe(1);
    expect((await r.handle(req('/api/releases/slots?status=bogus'), admin)).status).toBe(400);
    expect((await r.handle(req('/api/releases/slots'), agent)).status).toBe(403);
  }));
});
