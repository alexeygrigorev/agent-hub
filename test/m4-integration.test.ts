import { describe, it, expect, beforeEach } from 'vitest';
import { pinJobsClock } from './slot-clock';
import { runInDurableObject, env } from 'cloudflare:test';
import { doFetch } from './m1-focus-setup';
import { sha256 } from '../src/auth/crypto';
import { SCHEMA_SQL } from '../src/schema';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { JOBS_SCHEMA_SQL } from '../src/jobs';

async function resetAndSeedIntegrationDb(sql: any) {
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
  try { sql.exec('DELETE FROM agents'); } catch (e) {}
  try { sql.exec('DELETE FROM messages'); } catch (e) {}
  try { sql.exec("UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, epoch = 0, expires_at = NULL WHERE name = 'root'"); } catch (e) {}

  const now = new Date().toISOString();
  const future = new Date(Date.now() + 86400000).toISOString();

  // Seed two capable machines
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-alpha", "Host Alpha", "alpha.local", now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
    0,
    JSON.stringify({ jobs: ["root-run"] })
  );

  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-beta", "Host Beta", "beta.local", now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
    0,
    JSON.stringify({ jobs: ["root-run"] })
  );

  // Seed machine with unknown transport (no checkin, no heartbeat)
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    "host-unknown", "Host Unknown", "unknown.local", now, null,
    null,
    0,
    JSON.stringify({ jobs: ["root-run"] })
  );

  // Seed Host Credentials
  const tokenHostAlpha = await sha256('hubt_host_alpha');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_alpha', 'host', tokenHostAlpha, 'fam_alpha', 'host-alpha', 'host-alpha', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  const tokenHostBeta = await sha256('token_host_beta');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_beta', 'host', tokenHostBeta, 'fam_beta', 'host-beta', 'host-beta', JSON.stringify(['host', 'bus:send', 'bus:read']), 'admin-1', now, future
  );

  // Admin Credential
  const tokenAdmin = await sha256('token_admin');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_admin', 'human', tokenAdmin, 'fam_admin', 'admin@example.com', JSON.stringify(['admin', 'jobs:create', 'bus:read', 'bus:send']), 'admin-1', now, future
  );
}

describe('M4 Six Root Scenarios & System Compatibility Integration Tests', () => {
  beforeEach(async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);
    await (runInDurableObject as any)(stub, async (instance: any) => {
      await resetAndSeedIntegrationDb(instance.ctx.storage.sql);
    });
  });

  // Scenario 1: Races
  it('Scenario 1 (races): concurrent claim race over HTTP results in exactly one winner and one conflict', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const now = new Date().toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    // Setup two offered root-run jobs offered to host-alpha and host-beta respectively
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-race-root-alpha', 'root-run', 'root-run:race-alpha', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-alpha', 'host-alpha', future
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-race-root-beta', 'root-run', 'root-run:race-beta', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-beta', 'host-beta', future
      );
    });

    // Host Alpha and Host Beta both attempt to claim simultaneously
    // Each host claims its own offered job; they race to acquire the single root lease
    const [resAlpha, resBeta] = await Promise.all([
      doFetch(new Request('http://example.com/api/jobs/job-race-root-alpha/claim', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer hubt_host_alpha', 'Content-Type': 'application/json' },
        body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
      })),
      doFetch(new Request('http://example.com/api/jobs/job-race-root-beta/claim', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer token_host_beta', 'Content-Type': 'application/json' },
        body: JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true })
      }))
    ]);

    // Exactly one must succeed (200), and one must fail with 409 conflict
    const statuses = [resAlpha.status, resBeta.status].sort();
    expect(statuses).toEqual([200, 409]);

    const winnerRes = resAlpha.status === 200 ? resAlpha : resBeta;
    const winnerData = await winnerRes.json() as any;
    expect(winnerData.job_token).toBeDefined();
    expect(winnerData.lease_epoch).toBe(1);

    // Verify exactly one lease holder in SQLite with epoch 1
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(['job-race-root-alpha', 'job-race-root-beta']).toContain(lease.holder_job_id);
      expect(['host-alpha', 'host-beta']).toContain(lease.holder_host_id);
      expect(lease.epoch).toBe(1);
    });
  });

  // Scenario 2: Healthy Incumbent Protection
  it('Scenario 2 (healthy): active healthy root incumbent cannot be taken over', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const now = new Date().toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    await (runInDurableObject as any)(stub, async (instance: any) => {
      // Set active root lease for host-alpha
      instance.ctx.storage.sql.exec(
        `UPDATE leases SET holder_job_id = 'job-healthy-root', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = 1, acquired_at = ?, renewed_at = ?, expires_at = ? WHERE name = 'root'`,
        now, now, future
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-healthy-root', 'root-run', 'root-run:healthy-1', JSON.stringify({}), 'running', now, future, 'role:root',
        'host-alpha', 'host-alpha', 1, now
      );
      // Register agent as working/healthy
      instance.ctx.storage.sql.exec(
        `INSERT INTO agents (tag, project, role, machine_id, state, health_state, last_seen_at)
         VALUES ('root', 'system', 'root', 'host-alpha', 'working', 'healthy', ?)`,
        now
      );
      // Candidate job for host-beta
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-candidate-beta', 'root-run', 'root-run:cand-beta', JSON.stringify({}), 'offered', now, future, 'role:root',
        'host-beta', 'host-beta', future
      );
    });

    // Check root status reports healthy
    const resStatus = await doFetch(new Request('http://example.com/api/root/status', {
      headers: { 'Authorization': 'Bearer token_host_beta' }
    }));
    expect(resStatus.status).toBe(200);
    const statusData = await resStatus.json() as any;
    expect(statusData.heartbeat_fresh).toBe(true);
    expect(statusData.is_healthy_alive).toBe(true);
    expect(statusData.within_lease).toBe(true);

    // Host Beta attempts takeover of healthy Root -> 409 Conflict
    const resTakeover = await doFetch(new Request('http://example.com/api/root/takeover', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_beta', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candidate_job_id: 'job-candidate-beta',
        expected_epoch: 1,
        reason: 'attempted_preemption'
      })
    }));
    expect(resTakeover.status).toBe(409);
    const takeoverData = await resTakeover.json() as any;
    expect(takeoverData.error).toBe('incumbent_still_healthy');

    // Lease remains safely held by host-alpha
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(lease.holder_job_id).toBe('job-healthy-root');
      expect(lease.epoch).toBe(1);
    });
  });

  // Scenario 3: Stale Incumbent Takeover
  it('Scenario 3 (stale): confirmed stale root incumbent is atomically taken over via CAS', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    // 10 minutes ago
    const pastTime = new Date(Date.now() - 600000).toISOString();
    const expiredTime = new Date(Date.now() - 60000).toISOString();
    const future = new Date(Date.now() + 600000).toISOString();

    await (runInDurableObject as any)(stub, async (instance: any) => {
      // Host Alpha checkin is stale (10m ago)
      instance.ctx.storage.sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-alpha'", pastTime);

      // Stale root lease with expired deadline
      instance.ctx.storage.sql.exec(
        `UPDATE leases SET holder_job_id = 'job-stale-root', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = 1, acquired_at = ?, renewed_at = ?, expires_at = ? WHERE name = 'root'`,
        pastTime, pastTime, expiredTime
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-stale-root', 'root-run', 'root-run:stale-1', JSON.stringify({}), 'running', pastTime, expiredTime, 'role:root',
        'host-alpha', 'host-alpha', 1, pastTime
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO agents (tag, project, role, machine_id, state, health_state, last_seen_at)
         VALUES ('root', 'system', 'root', 'host-alpha', 'broken', 'stale', ?)`,
        pastTime
      );

      // Candidate job for host-beta
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-takeover-beta', 'root-run', 'root-run:to-beta', JSON.stringify({}), 'offered', new Date().toISOString(), future, 'role:root',
        'host-beta', 'host-beta', future
      );
    });

    // Verify root status confirms stale
    const resStatus = await doFetch(new Request('http://example.com/api/root/status', {
      headers: { 'Authorization': 'Bearer token_host_beta' }
    }));
    expect(resStatus.status).toBe(200);
    const statusData = await resStatus.json() as any;
    expect(statusData.confirmed_stale).toBe(true);

    // Host Beta performs atomic takeover
    const resTakeover = await doFetch(new Request('http://example.com/api/root/takeover', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_beta', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candidate_job_id: 'job-takeover-beta',
        expected_epoch: 1,
        reason: 'stale_incumbent_checkin_expired'
      })
    }));
    expect(resTakeover.status).toBe(200);
    const takeoverData = await resTakeover.json() as any;
    expect(takeoverData.success).toBe(true);
    expect(takeoverData.lease_epoch).toBe(2);
    expect(takeoverData.job_token).toBeDefined();

    // Verify DB updated: new holder is host-beta, epoch 2, old job marked fenced
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(lease.holder_job_id).toBe('job-takeover-beta');
      expect(lease.holder_host_id).toBe('host-beta');
      expect(lease.epoch).toBe(2);

      const oldJob = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'job-stale-root'").toArray()[0];
      expect(oldJob.status).toBe('fenced');
    });
  });

  // Scenario 4: Expiry
  it('Scenario 4 (expiry): lease expiration marks job expired and rejects subsequent actions', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const pastTime = new Date(Date.now() - 3600000).toISOString();
    const expiredTime = new Date(Date.now() - 1000).toISOString();

    const tokenJobExpired = await sha256('token_job_expired');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        `UPDATE leases SET holder_job_id = 'job-expired-1', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = 1, acquired_at = ?, renewed_at = ?, expires_at = ? WHERE name = 'root'`,
        pastTime, pastTime, expiredTime
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, claim_token_hash, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-expired-1', 'root-run', 'root-run:exp-1', JSON.stringify({}), 'running', pastTime, expiredTime, 'role:root',
        'host-alpha', 'host-alpha', tokenJobExpired, 1, pastTime
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES (?, 'job', ?, ?, ?, ?, ?, ?, 'system', ?, ?)`,
        'cred_job_exp', tokenJobExpired, 'fam_j_exp', 'job-expired-1', 'host-alpha', 'role:root', JSON.stringify(['lease:root', 'jobs:create']), pastTime, expiredTime
      );
    });

    // Run alarm / scheduledTick to process expirations
    await pinJobsClock(stub);
    const resTick = await stub.fetch(new Request('http://example.com/internal/tick', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resTick.status).toBe(200);

    // Verify in DB that job was marked expired and lease cleared
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(lease.holder_job_id).toBeNull();

      const job = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'job-expired-1'").toArray()[0];
      expect(job.status).toBe('expired');
    });

    // Subsequent renew attempt fails (credential revoked/expired)
    const resRenew = await doFetch(new Request('http://example.com/api/jobs/job-expired-1/renew', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_job_expired', 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: 1, job_token: 'token_job_expired' })
    }));
    expect([401, 409]).toContain(resRenew.status);
  });

  // Scenario 5: Unknown Transport Fails Closed
  it('Scenario 5 (unknown): machine with missing/unknown transport cannot perform takeover', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const tokenUnknown = await sha256('token_host_unknown');
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        'cred_unknown', 'host', tokenUnknown, 'fam_unk', 'host-unknown', 'host-unknown', JSON.stringify(['host']), 'admin-1', new Date().toISOString(), new Date(Date.now() + 86400000).toISOString()
      );
    });

    // Check root status reports transport unknown when host has no checkin
    const resStatus = await doFetch(new Request('http://example.com/api/root/status', {
      headers: { 'Authorization': 'Bearer token_host_unknown' }
    }));
    expect(resStatus.status).toBe(200);

    // Host Unknown attempts takeover -> fails closed with 409
    const resTakeover = await doFetch(new Request('http://example.com/api/root/takeover', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_host_unknown', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candidate_job_id: 'job-any',
        expected_epoch: 0
      })
    }));
    expect(resTakeover.status).toBe(409);
  });

  // Scenario 6: Old-Epoch Action Rejection
  it('Scenario 6 (old-epoch action): actions with superseded epoch are rejected with 409 fenced and zero effects', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    const future = new Date(Date.now() + 600000).toISOString();
    const tokenAlphaRoot = await sha256('token_alpha_root');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      // Lease is currently at epoch 2, held by host-beta
      instance.ctx.storage.sql.exec(
        `UPDATE leases SET holder_job_id = 'job-beta-active', holder_host_id = 'host-beta', holder_machine_id = 'host-beta', epoch = 2, expires_at = ? WHERE name = 'root'`,
        future
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-beta-active', 'root-run', 'root-run:beta-act', JSON.stringify({}), 'running', new Date().toISOString(), future, 'role:root',
        'host-beta', 'host-beta', 2, new Date().toISOString()
      );

      // Old job from epoch 1 (superseded)
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, claim_token_hash, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-alpha-old', 'root-run', 'root-run:alpha-old', JSON.stringify({}), 'fenced', new Date().toISOString(), future, 'role:root',
        'host-alpha', 'host-alpha', tokenAlphaRoot, 1, new Date().toISOString()
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
         VALUES (?, 'job', ?, ?, ?, ?, ?, ?, 'system', ?, ?)`,
        'cred_alpha_old', tokenAlphaRoot, 'fam_old', 'job-alpha-old', 'host-alpha', 'role:root', JSON.stringify(['lease:root', 'jobs:create']), new Date().toISOString(), future
      );
    });

    // Attempt action with old epoch 1 -> 409 fenced
    const resAction = await doFetch(new Request('http://example.com/api/root/actions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_alpha_root', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_id: 'job-alpha-old',
        epoch: 1,
        action: {
          type: 'bus_dispatch',
          name: 'stale-dispatch',
          idempotency_key: 'stale-key-1'
        }
      })
    }));
    expect(resAction.status).toBe(409);
    const actionData = await resAction.json() as any;
    expect(actionData.status).toBe('fenced');
    expect(actionData.reason).toBe('superseded');
    expect(actionData.current_epoch).toBe(2);

    // Verify ZERO rows in root_actions table for this stale attempt
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const actions = instance.ctx.storage.sql.exec("SELECT * FROM root_actions WHERE job_id = 'job-alpha-old'").toArray();
      expect(actions.length).toBe(0);
    });
  });

  // Scenario 7: Initial and M2/M3 Backward Compatibility
  it('Scenario 7 (compatibility): worker healthz, DO healthz, registry and bus operate concurrently', async () => {
    // 1. Worker healthz
    const resWorkerHealth = await doFetch(new Request('http://example.com/healthz', { headers: { Authorization: 'Bearer hubt_host_alpha' } }));
    expect(resWorkerHealth.status).toBe(200);
    const workerHealth = await resWorkerHealth.json() as any;
    expect(workerHealth.status).toBe('ok');
    expect(workerHealth.component).toBe('hub-do');

    // 2. Hub DO healthz
    const resDoHealth = await doFetch(new Request('http://example.com/api/healthz', { headers: { Authorization: 'Bearer hubt_host_alpha' } }));
    expect(resDoHealth.status).toBe(200);
    const doHealth = await resDoHealth.json() as any;
    expect(doHealth.status).toBe('ok');
    expect(doHealth.component).toBe('hub-do');

    // 3. Registry available machines endpoint (requires host scope)
    const resMachines = await doFetch(new Request('http://example.com/api/machines/available', {
      headers: { 'Authorization': 'Bearer hubt_host_alpha' }
    }));
    expect(resMachines.status).toBe(200);
    const machinesData = await resMachines.json() as any;
    expect(machinesData.machines).toBeDefined();

    // 4. Bus send and receive
    const resSend = await doFetch(new Request('http://example.com/api/bus/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_actor: 'admin',
        to_actor: 'machine:host-alpha',
        kind: 'note',
        body: 'system check message',
        idempotency_key: 'sys-chk-1'
      })
    }));
    expect(resSend.status).toBe(200);
    const sendData = await resSend.json() as any;
    expect(sendData.id).toBeDefined();

    // Read messages for host-alpha (as admin or host)
    const resMessages = await doFetch(new Request('http://example.com/api/bus/messages?actor=machine:host-alpha', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(resMessages.status).toBe(200);
    const msgData = await resMessages.json() as any[];
    expect(Array.isArray(msgData)).toBe(true);
    expect(msgData.some((m: any) => m.id === sendData.id)).toBe(true);
  });

  // Scenario 8: DO Alarm Offer Timeout Retry to no-host and 5m First-Action Expiry
  it('Scenario 8 (offer timeout retry & first-action 5m expiry)', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    // Part A: Offer retry when candidates exhausted transitions to no-host
    const pastTime = new Date(Date.now() - 300000).toISOString();
    const expiredOfferTime = new Date(Date.now() - 1000).toISOString();

    await (runInDurableObject as any)(stub, async (instance: any) => {
      // Create a job offered to host-alpha, where attempts already include all capable hosts
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at, offer_attempts)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-offer-exhausted', 'root-run', 'root-run:off-ex', JSON.stringify({}), 'offered', pastTime, pastTime, 'role:root',
        'host-alpha', 'host-alpha', expiredOfferTime, JSON.stringify(['host-alpha', 'host-beta'])
      );
    });

    // Run alarm on the DO to process offer expiration
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.jobsService.processAlarms();
      const job = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'job-offer-exhausted'").toArray()[0];
      expect(job.status).toBe('no-host');
    });

    // Part B: 5-minute first-action timeout transitions claimed job to failed
    const sixMinsAgo = new Date(Date.now() - 360000).toISOString();
    const tokenNoStart = await sha256('token_no_start');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec(
        `UPDATE leases SET holder_job_id = 'job-no-first-act', holder_host_id = 'host-alpha', holder_machine_id = 'host-alpha', epoch = 10, acquired_at = ?, expires_at = ? WHERE name = 'root'`,
        sixMinsAgo, new Date(Date.now() + 600000).toISOString()
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, claim_token_hash, lease_epoch, started_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        'job-no-first-act', 'root-run', 'root-run:no-start-1', JSON.stringify({}), 'claimed', sixMinsAgo, new Date(Date.now() + 600000).toISOString(), 'role:root',
        'host-alpha', 'host-alpha', tokenNoStart, 10, null
      );
    });

    // Run alarm to process first-action timeout
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.jobsService.processAlarms();

      const job = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'job-no-first-act'").toArray()[0];
      expect(job.status).toBe('failed');
      expect(job.outcome_summary).toBe('no first action');

      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0];
      expect(lease.holder_job_id).toBeNull();
      expect(lease.release_reason).toBe('no first action');
    });
  });

  // Scenario 9: 24-hour Cron Simulation (48 slots)
  it('Scenario 9 (24-hour simulation): 48 slots across 24h never produce two concurrent leases', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      // Clear all existing jobs and reset lease
      instance.ctx.storage.sql.exec('DELETE FROM jobs');
      instance.ctx.storage.sql.exec("UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, epoch = 0, expires_at = NULL WHERE name = 'root'");

      const baseMs = Date.parse('2026-10-15T00:00:00.000Z');

      for (let slot = 0; slot < 48; slot++) {
        const slotTime = new Date(baseMs + slot * 1800000);
        const slotIso = slotTime.toISOString();

        // Advance jobsService trusted clock across the 48 slots
        instance.jobsService.setClock(() => slotTime);

        // 1. Tick slot
        instance.jobsService.tickRootSlot(slotIso);

        // 2. Query the job created for this slot
        const jobsForSlot = instance.ctx.storage.sql.exec(
          "SELECT * FROM jobs WHERE idempotency_key = ?",
          `root-run:${slotIso}`
        ).toArray() as any[];
        expect(jobsForSlot.length).toBe(1);
        const job = jobsForSlot[0];

        // 3. Check lease state: never more than one holder
        const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
        expect(lease).toBeDefined();

        if (job.status === 'offered') {
          // Simulate successful claim, start, finish on host-alpha
          const hostId = job.offer_host_id || 'host-alpha';
          const claimRes = await instance.jobsService.claimJob(job.id, hostId, true);
          expect(claimRes.job_token).toBeDefined();

          // Start job via startJob before finishing
          const startRes = await instance.jobsService.startJob(
            job.id,
            claimRes.job_token,
            claimRes.lease_epoch,
            'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
            'session-sim-1',
            'task-sim-1'
          );
          expect(startRes.error).toBeUndefined();

          // Lease is held by this job
          const heldLease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
          expect(heldLease.holder_job_id).toBe(job.id);

          // Finish job
          await instance.jobsService.finishJob(
            job.id,
            claimRes.job_token,
            claimRes.lease_epoch,
            'succeeded',
            `Slot ${slot} completed successfully`
          );

          // After finish, lease holder is released
          const releasedLease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
          expect(releasedLease.holder_job_id).toBeNull();
        } else if (job.status === 'skip') {
          // If skipped, lease was held by incumbent
          expect(lease.holder_job_id).not.toBeNull();
        }

        // Idempotency check: duplicate tick for same slot produces no duplicate job
        instance.jobsService.tickRootSlot(slotIso);
        const duplicateJobs = instance.ctx.storage.sql.exec(
          "SELECT * FROM jobs WHERE idempotency_key = ?",
          `root-run:${slotIso}`
        ).toArray();
        expect(duplicateJobs.length).toBe(1);
      }

      // Final verification: 48 jobs created in total
      const totalJobs = instance.ctx.storage.sql.exec("SELECT COUNT(*) as cnt FROM jobs").toArray()[0] as any;
      expect(totalJobs.cnt).toBe(48);
    });
  });

  // Scenario 10: Non-destructive M3 -> M4 Schema Upgrade
  it('Scenario 10 (non-destructive M3 to M4 schema upgrade): existing data preserved with new columns accessible', async () => {
    const id = (env as any).HUB.idFromName("hub");
    const stub = (env as any).HUB.get(id);

    // Part A: Upgrade from real M3 BUS_SCHEMA_SQL (agents with project NOT NULL and no PK on tag)
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec('DROP TABLE IF EXISTS agents');
      for (const stmt of BUS_SCHEMA_SQL.split(';').map((s: string) => s.trim()).filter(Boolean)) {
        instance.ctx.storage.sql.exec(stmt);
      }
      // Seed pre-existing M3 agent with project
      const now = new Date().toISOString();
      instance.ctx.storage.sql.exec(
        `INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES (?, ?, ?, ?, ?)`,
        'm3-old-agent', 'm3-proj', 'worker', 'host-alpha', now
      );

      // Re-run Hub migration sequence on real M3 table
      instance.initDb();

      // Verify M3 agent is preserved
      const oldAgent = instance.ctx.storage.sql.exec("SELECT * FROM agents WHERE tag = 'm3-old-agent'").toArray()[0] as any;
      expect(oldAgent.tag).toBe('m3-old-agent');
      expect(oldAgent.project).toBe('m3-proj');

      // Verify inserting an agent WITHOUT project succeeds (project is nullable in M4)
      instance.ctx.storage.sql.exec(
        `INSERT INTO agents (tag, role, machine_id, registered_at, state, updated_at) VALUES ('m3-agent-no-proj', 'worker', 'host-1', ?, 'idle', ?)`,
        now, now
      );
      const newAgent = instance.ctx.storage.sql.exec("SELECT * FROM agents WHERE tag = 'm3-agent-no-proj'").toArray()[0] as any;
      expect(newAgent.tag).toBe('m3-agent-no-proj');

      // Verify agents.tag is now PRIMARY KEY
      const agentCols = instance.ctx.storage.sql.exec("PRAGMA table_info(agents)").toArray();
      const tagCol = agentCols.find((c: any) => c.name === 'tag');
      expect(tagCol.pk).toBeGreaterThan(0);
    });

    // Part B: Upgrade from SPEC §6 tables (jobs, leases, audit before M4 columns)
    await (runInDurableObject as any)(stub, async (instance: any) => {
      instance.ctx.storage.sql.exec('DROP TABLE IF EXISTS jobs');
      instance.ctx.storage.sql.exec('DROP TABLE IF EXISTS leases');
      instance.ctx.storage.sql.exec('DROP TABLE IF EXISTS audit');
      instance.ctx.storage.sql.exec(`
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          status TEXT NOT NULL,
          idempotency_key TEXT UNIQUE,
          offered_host_id TEXT,
          claimed_host_id TEXT,
          claim_token_hash TEXT,
          payload TEXT,
          result TEXT,
          error TEXT,
          created_at TEXT NOT NULL,
          offered_at TEXT,
          claimed_at TEXT,
          started_at TEXT,
          heartbeat_at TEXT,
          expires_at TEXT,
          finished_at TEXT
        )
      `);
      instance.ctx.storage.sql.exec(`
        CREATE TABLE leases (
          name TEXT PRIMARY KEY,
          holder_job_id TEXT,
          epoch INTEGER NOT NULL DEFAULT 0,
          acquired_at TEXT,
          heartbeat_at TEXT,
          expires_at TEXT
        )
      `);
      instance.ctx.storage.sql.exec(`
        CREATE TABLE audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          detail TEXT
        )
      `);

      // Seed pre-existing records
      const now = new Date().toISOString();
      instance.ctx.storage.sql.exec(
        `INSERT INTO jobs (id, kind, status, created_at) VALUES ('spec6-job-1', 'generic', 'completed', ?)`,
        now
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO leases (name, epoch) VALUES ('root', 0)`
      );
      instance.ctx.storage.sql.exec(
        `INSERT INTO audit (ts, actor, action) VALUES (?, 'system', 'test_init')`,
        now
      );

      // Re-run Hub migration sequence on SPEC §6 tables
      instance.initDb();

      // Verify all M4 extra columns exist on jobs, leases, audit
      const jobCols = instance.ctx.storage.sql.exec("PRAGMA table_info(jobs)").toArray().map((c: any) => c.name);
      expect(jobCols).toContain('offer_attempts');
      expect(jobCols).toContain('outcome_summary');
      expect(jobCols).toContain('outcome_evidence');
      expect(jobCols).toContain('deadline_at');

      const leaseCols = instance.ctx.storage.sql.exec("PRAGMA table_info(leases)").toArray().map((c: any) => c.name);
      expect(leaseCols).toContain('holder_host_id');
      expect(leaseCols).toContain('holder_machine_id');
      expect(leaseCols).toContain('holder_actor');
      expect(leaseCols).toContain('renewed_at');
      expect(leaseCols).toContain('released_at');
      expect(leaseCols).toContain('release_reason');

      const auditCols = instance.ctx.storage.sql.exec("PRAGMA table_info(audit)").toArray().map((c: any) => c.name);
      expect(auditCols).toContain('resource');
      expect(auditCols).toContain('target');
      expect(auditCols).toContain('at');

      // Verify pre-existing data preserved
      const oldJob = instance.ctx.storage.sql.exec("SELECT * FROM jobs WHERE id = 'spec6-job-1'").toArray()[0] as any;
      expect(oldJob.id).toBe('spec6-job-1');
      const oldAudit = instance.ctx.storage.sql.exec("SELECT * FROM audit WHERE action = 'test_init'").toArray()[0] as any;
      expect(oldAudit.actor).toBe('system');
    });

    // Part C: Verify GET /api/jobs succeeds without 500 error after upgrade
    const hostToken = 'hubt_host_alpha';
    const listRes = await doFetch(new Request('http://example.com/api/jobs', {
      headers: { 'Authorization': `Bearer ${hostToken}` }
    }));
    expect(listRes.status).toBe(200);
    const listBody = await listRes.json() as any;
    expect(Array.isArray(listBody.jobs)).toBe(true);
  });
});
