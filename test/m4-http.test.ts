import { describe, it, expect, beforeAll } from 'vitest';
import { pinJobsClock } from './slot-clock';
import { runInDurableObject, env } from 'cloudflare:test';
import { doFetch } from './m1-focus-setup';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { JOBS_SCHEMA_SQL } from '../src/jobs';

async function seedTestDb(sql: any) {
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
  for (const statement of JOBS_SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) {
      try { sql.exec(trimmed); } catch (e) {}
    }
  }

  sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES ('root', 0)");

  try { sql.exec('DELETE FROM jobs'); } catch (e) {}
  try { sql.exec('DELETE FROM offers'); } catch (e) {}
  try { sql.exec('DELETE FROM audit'); } catch (e) {}
  try { sql.exec('DELETE FROM root_actions'); } catch (e) {}
  try { sql.exec('DELETE FROM handoff_notes'); } catch (e) {}
  try { sql.exec('DELETE FROM machines'); } catch (e) {}
  try { sql.exec('DELETE FROM credentials'); } catch (e) {}
  try { sql.exec("UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, epoch = 0, expires_at = NULL WHERE name = 'root'"); } catch (e) {}

  const now = new Date().toISOString();
  const future = new Date(Date.now() + 86400000).toISOString();

  // Seed two capable machines
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-alpha", "Host Alpha", "alpha.local", now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
    0,
    JSON.stringify({ jobs: ["root-run", "writer-run"] })
  );

  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-beta", "Host Beta", "beta.local", now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
    0,
    JSON.stringify({ jobs: ["root-run"] })
  );

  // Machine with failed gate
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-failing-gate", "Host Failing", "fail.local", now, now,
    JSON.stringify({ gate: { launch_allowed: false, reason: "disk_pressure" } }),
    0,
    JSON.stringify({ jobs: ["root-run"] })
  );

  // Seed Host Credentials
  const tokenHostAlpha = await sha256('token_host_alpha');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_alpha', 'host', tokenHostAlpha, 'fam_alpha', 'host-alpha', 'host-alpha', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  const tokenHostBeta = await sha256('token_host_beta');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_beta', 'host', tokenHostBeta, 'fam_beta', 'host-beta', 'host-beta', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  const tokenHostFail = await sha256('token_host_fail');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_fail', 'host', tokenHostFail, 'fam_fail', 'host-failing-gate', 'host-failing-gate', JSON.stringify(['host']), 'admin-1', now, future
  );

  // Admin credential
  const tokenAdmin = await sha256('token_admin');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_admin', 'human', tokenAdmin, 'fam_admin', 'admin@example.com', JSON.stringify(['admin', 'jobs:create', 'bus:read', 'bus:send']), 'admin-1', now, future
  );

  // Normal agent without host scope
  const tokenAgentNoHost = await sha256('token_no_host');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_no_host', 'agent', tokenAgentNoHost, 'fam_nh', 'worker-1', 'worker-1', 'host-alpha', JSON.stringify(['bus:read', 'bus:send']), 'admin-1', now, future
  );
}

describe('M4 HTTP Endpoints and Worker/Hub Routing', () => {
  beforeAll(async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);
    await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedTestDb(instance.ctx.storage.sql);
    });
  });

  it('unauthenticated calls to jobs endpoints return 401', async () => {
    const resOffers = await doFetch(new Request('http://example.com/api/jobs/offers'));
    expect(resOffers.status).toBe(401);

    const resJobs = await doFetch(new Request('http://example.com/api/jobs'));
    expect(resJobs.status).toBe(401);

    const resClaim = await doFetch(new Request('http://example.com/api/jobs/job-1/claim', { method: 'POST' }));
    expect(resClaim.status).toBe(401);
  });

  it('host offers endpoint enforces scope, machine binding, and does not leak tokens', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    // Insert an offered job for host-alpha and an offered job for host-beta
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const now = new Date().toISOString();
      const future = new Date(Date.now() + 600000).toISOString();
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at, claim_token_hash)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-alpha-1', 'root-run', 'root-run:test-alpha-1', JSON.stringify({ step: 'init' }), 'offered', now, future, 'role:root',
        'host-alpha', 'host-alpha', future, 'super-secret-token-hash-must-not-leak'
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at, claim_token_hash)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-beta-1', 'root-run', 'root-run:test-beta-1', JSON.stringify({ step: 'init' }), 'offered', now, future, 'role:root',
        'host-beta', 'host-beta', future, 'another-secret-token-hash'
      );
    });

    // 1. Caller lacking host scope gets 403
    const resNoHost = await doFetch(new Request('http://example.com/api/jobs/offers', {
      headers: { 'Authorization': 'Bearer token_no_host' }
    }));
    expect(resNoHost.status).toBe(403);

    // 2. Host Alpha retrieves its offers
    const resAlpha = await doFetch(new Request('http://example.com/api/jobs/offers', {
      headers: { 'Authorization': 'Bearer token_host_alpha' }
    }));
    expect(resAlpha.status).toBe(200);
    const dataAlpha = await resAlpha.json() as any;
    expect(dataAlpha.offers).toBeDefined();
    expect(Array.isArray(dataAlpha.offers)).toBe(true);
    expect(dataAlpha.offers.length).toBe(1);
    expect(dataAlpha.offers[0].id).toBe('job-alpha-1');

    // SECURITY CHECK: Verify token hash is never leaked
    expect((dataAlpha.offers[0] as any).claim_token_hash).toBeUndefined();
    expect(JSON.stringify(dataAlpha)).not.toContain('super-secret-token-hash');

    // 3. /api/host/offers alias behaves identically
    const resHostAlias = await doFetch(new Request('http://example.com/api/host/offers', {
      headers: { 'Authorization': 'Bearer token_host_alpha' }
    }));
    expect(resHostAlias.status).toBe(200);
    const dataHostAlias = await resHostAlias.json() as any;
    expect(dataHostAlias.offers.length).toBe(1);
    expect(dataHostAlias.offers[0].id).toBe('job-alpha-1');
  });

  it('job claim enforces machine binding, server gate check, and returns tokens', async () => {
    // 1. Host Beta attempts to claim Host Alpha's offered job -> 409 conflict
    const resCrossClaim = await doFetch(new Request('http://example.com/api/jobs/job-alpha-1/claim', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_beta',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ gate: true })
    }));
    expect(resCrossClaim.status).toBe(409);

    // 2. Host Alpha claims its offered job with gate = true
    const resClaim = await doFetch(new Request('http://example.com/api/jobs/job-alpha-1/claim', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_alpha',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
    }));
    expect(resClaim.status).toBe(200);
    const claimData = await resClaim.json() as any;
    expect(claimData.job_token).toBeDefined();
    expect(claimData.lease_epoch).toBe(1);
    expect(claimData.deadline_at).toBeDefined();

    // 3. Second claim on already-claimed job returns 409 conflict
    const resClaimAgain = await doFetch(new Request('http://example.com/api/jobs/job-alpha-1/claim', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_alpha',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
    }));
    expect(resClaimAgain.status).toBe(409);
  });

  it('claim fails closed when host gate is failing', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const now = new Date().toISOString();
      const future = new Date(Date.now() + 600000).toISOString();
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-fail-gate', 'root-run', 'root-run:fail-gate', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-failing-gate', 'host-failing-gate', future
      );
    });

    const res = await doFetch(new Request('http://example.com/api/jobs/job-fail-gate/claim', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_host_fail',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
    }));
    expect(res.status).toBe(403);
    const data = await res.json() as any;
    expect(data.error).toBe('gate_failed');
  });

  it('job lifecycle: start, renew, and finish via authenticated HTTP', async () => {
    // Insert a new offered job for host-beta
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const now = new Date().toISOString();
      const future = new Date(Date.now() + 600000).toISOString();
      // Ensure lease is unheld
      instance.ctx.storage.sql.exec("UPDATE leases SET holder_job_id = NULL, expires_at = NULL WHERE name = 'root'");
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-lifecycle-1', 'root-run', 'root-run:lifecycle-1', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-beta', 'host-beta', future
      );
    });

    // Claim
    const claimRes = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/claim', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_beta', 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
    }));
    expect(claimRes.status).toBe(200);
    const { job_token, lease_epoch } = await claimRes.json() as any;
    expect(job_token).toBeDefined();

    // Start with missing first-action evidence returns 400
    const startMissingEvidence = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/start', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: lease_epoch, launcher_task_id: 'task-1', native_session: 'sess-1' })
    }));
    expect(startMissingEvidence.status).toBe(400);

    // Start with wrong epoch returns 409 (fenced before payload format check)
    const startWrongEpoch = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/start', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: 9999, evidence_digest: 'art-dig', launcher_task_id: 'task-1', native_session: 'sess-1' })
    }));
    expect(startWrongEpoch.status).toBe(409);

    // Start with valid epoch but non-hex evidence_digest ('art-dig' or 'sha256:valid-ev') returns 400
    for (const invalidDigest of ['art-dig', 'sha256:valid-ev']) {
      const startInvalidDigest = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/start', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ epoch: lease_epoch, evidence_digest: invalidDigest, launcher_task_id: 'task-1', native_session: 'sess-1' })
      }));
      expect(startInvalidDigest.status).toBe(400);
    }

    // Valid start with cryptographic hex digest
    const startRes = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/start', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        epoch: lease_epoch,
        evidence_digest: 'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        launcher_task_id: 'task-1',
        native_session: 'sess-1'
      })
    }));
    expect(startRes.status).toBe(200);
    const startData = await startRes.json() as any;
    expect(startData.status).toBe('running');

    // Renew
    const renewRes = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/renew', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: lease_epoch })
    }));
    expect(renewRes.status).toBe(200);
    const renewData = await renewRes.json() as any;
    expect(renewData.status).toBe('renewed');

    // Finish with invalid status returns 400
    const finishInvalid = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/finish', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: lease_epoch, status: 'invalid_status_type' })
    }));
    expect(finishInvalid.status).toBe(400);

    // Valid finish
    const finishRes = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/finish', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: lease_epoch, status: 'succeeded', summary: 'Run finished cleanly', evidence_refs: ['out.log'] })
    }));
    expect(finishRes.status).toBe(200);

    // Verify lease is released
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(lease.holder_job_id).toBeNull();
    });
  });

  it('job decline triggers offer decline and permits fallback', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const now = new Date().toISOString();
      const future = new Date(Date.now() + 600000).toISOString();
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-decline-1', 'root-run', 'root-run:decline-1', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-alpha', 'host-alpha', future
      );
    });

    // Decline by wrong host returns 409
    const declineWrong = await doFetch(new Request('http://example.com/api/jobs/job-decline-1/decline', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_beta', 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'cannot run' })
    }));
    expect(declineWrong.status).toBe(409);

    // Decline by correct host returns 200
    const declineRes = await doFetch(new Request('http://example.com/api/jobs/job-decline-1/decline', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'busy with other task' })
    }));
    expect(declineRes.status).toBe(200);
  });

  it('job listing and single status do not leak tokens', async () => {
    const resList = await doFetch(new Request('http://example.com/api/jobs?limit=10', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resList.status).toBe(200);
    const listData = await resList.json() as any;
    expect(listData.jobs).toBeDefined();
    for (const j of listData.jobs) {
      expect(j.claim_token_hash).toBeUndefined();
    }

    const resSingle = await doFetch(new Request('http://example.com/api/jobs/job-lifecycle-1/status', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resSingle.status).toBe(200);
    const singleData = await resSingle.json() as any;
    expect(singleData.job).toBeDefined();
    expect(singleData.job.claim_token_hash).toBeUndefined();
  });

  it('job creation via POST /api/jobs enforces jobs:create scope', async () => {
    // 1. Without jobs:create scope -> 403
    const resForbidden = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_no_host', 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'custom', payload: { foo: 'bar' } })
    }));
    expect(resForbidden.status).toBe(403);

    // 2. Admin has jobs:create scope -> 201 Created
    const resCreated = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'custom', payload: { action: 'deploy-check' } })
    }));
    expect(resCreated.status).toBe(201);
    const createdData = await resCreated.json() as any;
    expect(createdData.job_id).toBeDefined();
  });

  it('root endpoints status, verify, actions, and handoff', async () => {
    // Root status
    const resStatus = await doFetch(new Request('http://example.com/api/root/status', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resStatus.status).toBe(200);
    const statusData = await resStatus.json() as any;
    expect(statusData.lease).toBeDefined();
    const currentEpoch = statusData.lease.epoch;

    // When lease is not actively held, verify returns 403 expired
    const resVerifyUnheld = await doFetch(new Request(`http://example.com/api/root/verify?epoch=${currentEpoch}`, {
      headers: { 'Authorization': 'Bearer token_host_alpha' }
    }));
    expect(resVerifyUnheld.status).toBe(403);
    const unheldData = await resVerifyUnheld.json() as any;
    expect(unheldData.status).toBe('expired');

    // Stale epoch returns 409 fenced
    const resVerifyStale = await doFetch(new Request(`http://example.com/api/root/verify?epoch=${currentEpoch + 99}`, {
      headers: { 'Authorization': 'Bearer token_host_alpha' }
    }));
    expect(resVerifyStale.status).toBe(409);
    const staleData = await resVerifyStale.json() as any;
    expect(staleData.status).toBe('fenced');

    // When an active root lease is held by host-alpha:
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);
    const future = new Date(Date.now() + 600000).toISOString();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        "UPDATE leases SET holder_job_id = 'job-verify-active', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = epoch + 1, expires_at = ? WHERE name = 'root'",
        future
      );
      instance.ctx.storage.sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, lease_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        'job-verify-active', 'root-run', 'root-run:verify-act', JSON.stringify({}), 'running', new Date().toISOString(), future, 'role:root', 'host-alpha', 'host-alpha', currentEpoch + 1
      );
    });

    const resVerifyActive = await doFetch(new Request(`http://example.com/api/root/verify?epoch=${currentEpoch + 1}&job_id=job-verify-active`, {
      headers: { 'Authorization': 'Bearer token_host_alpha' }
    }));
    expect(resVerifyActive.status).toBe(200);
    const activeData = await resVerifyActive.json() as any;
    expect(activeData.valid).toBe(true);
    expect(activeData.current_epoch).toBe(currentEpoch + 1);

    // Handoff notes POST and GET
    const resHandoff = await doFetch(new Request('http://example.com/api/root/handoff', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: 'job-verify-active',
        epoch: currentEpoch + 1,
        summary: 'Root cycle complete, healthy handoff',
        evidence_refs: ['cycle.log']
      })
    }));
    expect(resHandoff.status).toBe(200);

    const resHandoffGet = await doFetch(new Request('http://example.com/api/root/handoff?limit=5', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resHandoffGet.status).toBe(200);
    const handoffData = await resHandoffGet.json() as any;
    expect(handoffData.notes).toBeDefined();
    expect(handoffData.notes.length).toBeGreaterThan(0);
    expect(handoffData.notes[0].summary).toBe('Root cycle complete, healthy handoff');
  });

  it('POST /internal/tick executes scheduledTick idempotently', async () => {
    // Public worker.fetch blocks /internal/ routing
    const resPublic = await doFetch(new Request('http://example.com/internal/tick', { method: 'POST' }));
    expect(resPublic.status).toBe(404);

    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const resGet = await stub.fetch(new Request('http://example.com/internal/tick', { method: 'GET' }));
    expect(resGet.status).toBe(405);

    const at = await pinJobsClock(stub);
    const validSlot = new Date(Math.floor(at.getTime() / 1800000) * 1800000).toISOString();
    const resTick1 = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_admin',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ slot: validSlot })
    }));
    expect(resTick1.status).toBe(200);
    const tickData1 = await resTick1.json() as any;
    expect(tickData1.status).toBe('ok');

    // Duplicate tick for same slot is idempotent
    const resTick2 = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_admin',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ slot: validSlot })
    }));
    expect(resTick2.status).toBe(200);
    const tickData2 = await resTick2.json() as any;
    expect(tickData2.status).toBe('ok');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const rows = instance.ctx.storage.sql.exec("SELECT id FROM jobs WHERE idempotency_key = ?", `root-run:${validSlot}`).toArray();
      expect(rows.length).toBe(1);
    });
  });

  it('POST /api/host/poll returns pending offers for polling host', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const now = new Date().toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    const tokenPollAlpha = await sha256('hubt_poll_alpha');
    const tokenPollBeta = await sha256('hubt_poll_beta');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        'cred_poll_alpha', 'host', tokenPollAlpha, 'fam_pa', 'host-alpha', 'host-alpha', JSON.stringify(['host']), 'admin-1', now, future
      );
      instance.ctx.storage.sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        'cred_poll_beta', 'host', tokenPollBeta, 'fam_pb', 'host-beta', 'host-beta', JSON.stringify(['host']), 'admin-1', now, future
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-poll-test', 'root-run', 'root-run:poll-1', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-alpha', 'host-alpha', future
      );
    });

    // Polling with host-alpha returns the offer
    const resAlpha = await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer hubt_poll_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'heartbeat', gate: { launch_allowed: true } })
    }));
    expect(resAlpha.status).toBe(200);
    const alphaData = await resAlpha.json() as any;
    expect(alphaData.offers).toBeDefined();
    expect(alphaData.offers.some((o: any) => o.id === 'job-poll-test')).toBe(true);

    // Polling with host-beta does not return alpha's offer
    const resBeta = await doFetch(new Request('http://example.com/api/host/poll', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer hubt_poll_beta', 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'heartbeat', gate: { launch_allowed: true } })
    }));
    expect(resBeta.status).toBe(200);
    const betaData = await resBeta.json() as any;
    expect(betaData.offers).toBeDefined();
    expect(betaData.offers.some((o: any) => o.id === 'job-poll-test')).toBe(false);
  });

  it('POST /api/root/action (singular) and /actions (plural) bus_dispatch and job_transition', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const now = new Date().toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        "UPDATE leases SET holder_job_id = 'job-action-holder', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = 42, expires_at = ? WHERE name = 'root'",
        future
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, lease_epoch, native_session)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-action-holder', 'root-run', 'root-run:act-hold', JSON.stringify({}), 'running', now, future, 'role:root', 'host-alpha', 'host-alpha', 42, 'session-act-42'
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-transition-target', 'sub-task', 'sub:1', JSON.stringify({}), 'pending', now, future, 'system'
      );
    });

    // 1. Singular POST /api/root/action with bus_dispatch
    const resBusDispatch = await doFetch(new Request('http://example.com/api/root/action', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: 'job-action-holder',
        epoch: 42,
        session_id: 'session-act-42',
        action: {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'machine:host-alpha',
            kind: 'note',
            body: { message: 'hello from root authority' }
          }
        }
      })
    }));
    const busData = await resBusDispatch.json() as any;
    expect(resBusDispatch.status).toBe(200);
    expect(busData.status).toBe('committed');

    // Verify message was stored in DB with from_actor = 'role:root'
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const msgs = instance.ctx.storage.sql.exec("SELECT * FROM messages WHERE from_actor = 'role:root' AND to_actor = 'machine:host-alpha'").toArray();
      expect(msgs.length).toBeGreaterThan(0);
    });

    // 2. Plural POST /api/root/actions with job_transition
    const resTransition = await doFetch(new Request('http://example.com/api/root/actions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: 'job-action-holder',
        epoch: 42,
        session_id: 'session-act-42',
        action: {
          type: 'job_transition',
          payload: {
            target_job_id: 'job-transition-target',
            new_status: 'succeeded',
            outcome_summary: 'sub-task successfully completed'
          }
        }
      })
    }));
    expect(resTransition.status).toBe(200);

    // Verify target job was transitioned in DB
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const targetJob = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'job-transition-target'").toArray()[0];
      expect(targetJob.status).toBe('succeeded');
      expect(targetJob.outcome_summary).toBe('sub-task successfully completed');
    });
  });

  it('session cookie CSRF protection on mutating job/root endpoints', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    let sessionCookie = '';
    await (runInDurableObject as any)(stub, async (instance: any) => {
      sessionCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1',
        email: 'admin@fake.local',
        isAdmin: true,
        csrfToken: 'valid-csrf-token-xyz',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });

    // 1. Missing CSRF token -> 403
    const resNoCsrf = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: {
        'Cookie': sessionCookie,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ kind: 'test-job', idempotency_key: 'csrf-no-token', payload: {} })
    }));
    expect(resNoCsrf.status).toBe(403);
    const noCsrfData = await resNoCsrf.json() as any;
    expect(noCsrfData.error).toContain('CSRF');

    // 2. Invalid CSRF token -> 403
    const resBadCsrf = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: {
        'Cookie': sessionCookie,
        'X-CSRF-Token': 'wrong-token',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ kind: 'test-job', idempotency_key: 'csrf-bad-token', payload: {} })
    }));
    expect(resBadCsrf.status).toBe(403);
    const badCsrfData = await resBadCsrf.json() as any;
    expect(badCsrfData.error).toContain('CSRF');

    // 3. Valid CSRF token -> 201
    const resValidCsrf = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: {
        'Cookie': sessionCookie,
        'X-CSRF-Token': 'valid-csrf-token-xyz',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ kind: 'test-job', idempotency_key: 'csrf-valid-token', payload: {} })
    }));
    expect(resValidCsrf.status).toBe(201);
  });

  it('denyunsupportednative: fail-closed gate and unsupported profile rejection', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const now = new Date().toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        "UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, epoch = 100, expires_at = NULL WHERE name = 'root'"
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-deny-native', 'root-run', 'root-run:deny-1', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-alpha', 'host-alpha', future
      );
    });

    // 1. Missing gate fails closed -> 403
    const resNoGate = await doFetch(new Request('http://example.com/api/jobs/job-deny-native/claim', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    }));
    expect(resNoGate.status).toBe(403);

    // 2. profile_supported: false rejected -> 400 denyunsupportednative
    const resUnsupProfile = await doFetch(new Request('http://example.com/api/jobs/job-deny-native/claim', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate: true, profile_supported: false })
    }));
    expect(resUnsupProfile.status).toBe(400);
    const unsupData = await resUnsupProfile.json() as any;
    expect(unsupData.reason).toContain('denyunsupportednative');

    // 3. Claim succeeds with valid gate
    const resClaim = await doFetch(new Request('http://example.com/api/jobs/job-deny-native/claim', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_alpha', 'Content-Type': 'application/json' },
      body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true })
    }));
    expect(resClaim.status).toBe(200);
    const claimData = await resClaim.json() as any;
    const jobToken = claimData.job_token;

    // 4. Start rejects profile_supported: false -> 400
    const resStartUnsup = await doFetch(new Request('http://example.com/api/jobs/job-deny-native/start', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${jobToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        epoch: claimData.lease_epoch,
        profile_supported: false,
        first_action: { pid: 1234, evidence_digest: 'sha256:abc' }
      })
    }));
    expect(resStartUnsup.status).toBe(400);

    // 5. Start rejects empty first-action evidence -> 400
    const resStartNoEvidence = await doFetch(new Request('http://example.com/api/jobs/job-deny-native/start', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${jobToken}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        epoch: claimData.lease_epoch,
        profile_supported: true,
        first_action: {}
      })
    }));
    expect(resStartNoEvidence.status).toBe(400);
  });

  it('epoch validation and non-coercion across job/root endpoints', async () => {
    // 1. POST /api/jobs with epoch = null, false, "" rejected with 400
    for (const invalidEpoch of [null, false, ""]) {
      const res = await doFetch(new Request('http://example.com/api/jobs', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'root-run',
          idempotency_key: `bad-epoch-${String(invalidEpoch)}`,
          payload: {},
          epoch: invalidEpoch
        })
      }));
      expect(res.status).toBe(400);
    }

    // 2. POST /api/jobs with non-finite or negative deadline_mins rejected with 400
    const resNegDeadline = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'generic-job',
        idempotency_key: 'bad-deadline-neg',
        payload: {},
        deadline_mins: -10
      })
    }));
    expect(resNegDeadline.status).toBe(400);

    // 3. POST /api/jobs with stale root epoch returns 409 fenced
    const resStaleEpoch = await doFetch(new Request('http://example.com/api/jobs', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind: 'root-run',
        idempotency_key: 'stale-epoch-create',
        payload: {},
        epoch: 999
      })
    }));
    expect(resStaleEpoch.status).toBe(409);
    const staleData = await resStaleEpoch.json() as any;
    expect(staleData.status).toBe('fenced');
  });

  it('internal tick validates clock inputs and refuses late/future slots', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    // 0. Unauthenticated tick with slot override -> 401
    const resUnauth = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot: new Date().toISOString() })
    }));
    expect(resUnauth.status).toBe(401);

    // 1. Malformed date string -> 400
    const resMalformed = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_admin',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ slot: 'invalid-not-a-date' })
    }));
    expect(resMalformed.status).toBe(400);

    // 2. Late slot (>= 25 min in the past) -> refused
    const lateSlot = new Date(Date.now() - 30 * 60 * 1000).toISOString();
    const resLate = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_admin',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ slot: lateSlot })
    }));
    expect(resLate.status).toBe(400);
    const lateData = await resLate.json() as any;
    expect(lateData.reason).toBe('late_slot_refused');

    // 3. Future slot (> 60s in the future) -> refused 400
    const futureSlot = new Date(Date.now() + 120 * 1000).toISOString();
    const resFuture = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer token_admin',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ slot: futureSlot })
    }));
    expect(resFuture.status).toBe(400);
    const futureData = await resFuture.json() as any;
    expect(futureData.reason).toBe('future_slot_refused');
  });

  it('root verify method restriction and handoff low-privilege rejection', async () => {
    // 1. PUT and DELETE to /api/root/verify return 405 Method Not Allowed
    const resPut = await doFetch(new Request('http://example.com/api/root/verify?epoch=0', {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resPut.status).toBe(405);

    const resDel = await doFetch(new Request('http://example.com/api/root/verify?epoch=0', {
      method: 'DELETE',
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resDel.status).toBe(405);

    // 2. POST /api/root/handoff by an unprivileged agent (no host/lease:root/admin scope) returns 403
    const resHandoffLowPriv = await doFetch(new Request('http://example.com/api/root/handoff', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_no_host', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: 'job-any',
        epoch: 0,
        summary: 'illegal takeover'
      })
    }));
    expect(resHandoffLowPriv.status).toBe(403);
  });
});
