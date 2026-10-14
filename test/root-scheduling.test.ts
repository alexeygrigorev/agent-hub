import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { doFetch } from './m1-focus-setup';
import { sha256 } from '../src/auth/crypto';
import { createSessionCookie } from '../src/auth/session';
import { Hub } from '../src/hub';
import { JobsService, JOBS_SCHEMA_SQL } from '../src/jobs';
import { SCHEMA_SQL } from '../src/schema';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { ensureRootScheduling, getRootScheduling, setRootScheduling, isRootSchedulingEnabled } from '../src/root_scheduling';

// Fixed clock five minutes into a fixed slot. Tick, offer and deadline logic run on
// the JobsService clock, so these tests never depend on the wall-clock minute (the
// tick refuses slots aged >= 25 min). The slot is far in the future so a DO alarm
// armed by a tick (never earlier than the wall clock) cannot act on it mid-test.
const FIXED_SLOT = '2099-01-05T10:30:00.000Z';
const FIXED_NOW = new Date(Date.parse(FIXED_SLOT) + 5 * 60 * 1000);
const MINUTE_MS = 60 * 1000;

async function withFixedClock<T>(instance: any, at: Date, fn: () => Promise<T>): Promise<T> {
  instance.jobsService.setClock(() => new Date(at.getTime()));
  try {
    return await fn();
  } finally {
    instance.jobsService.setClock(() => new Date());
  }
}

// Candidate selection needs a check-in within 180 s of the scheduler clock.
function freshenHosts(sql: any, at: Date) {
  sql.exec('UPDATE machines SET last_checkin_at = ?', at.toISOString());
}

function tickReq(slot: string): Request {
  return new Request('http://example.com/internal/tick', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer token_admin', 'Content-Type': 'application/json' },
    body: JSON.stringify({ slot })
  });
}

async function adminCookie(e: any): Promise<string> {
  return createSessionCookie(e, {
    sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
    exp: Math.floor(Date.now() / 1000) + 3600
  });
}

async function getStub(name: string = 'hub') {
  const id = (env as any).HUB.idFromName(name);
  return (env as any).HUB.get(id);
}

async function seedBase(sql: any) {
  for (const statement of SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) { try { sql.exec(trimmed); } catch (e) {} }
  }
  for (const statement of BUS_SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) { try { sql.exec(trimmed); } catch (e) {} }
  }
  for (const statement of JOBS_SCHEMA_SQL.split(';')) {
    const trimmed = statement.trim();
    if (trimmed) { try { sql.exec(trimmed); } catch (e) {} }
  }

  try { sql.exec('DELETE FROM jobs'); } catch (e) {}
  try { sql.exec('DELETE FROM offers'); } catch (e) {}
  try { sql.exec('DELETE FROM audit'); } catch (e) {}
  try { sql.exec('DELETE FROM machines'); } catch (e) {}
  try { sql.exec('DELETE FROM credentials'); } catch (e) {}
  try { sql.exec('DELETE FROM root_scheduling'); } catch (e) {}
  try { sql.exec("UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, holder_actor = NULL, epoch = 0, expires_at = NULL, released_at = NULL, release_reason = NULL WHERE name = 'root'"); } catch (e) {}
  ensureRootScheduling(sql);

  const now = new Date().toISOString();
  const future = new Date(Date.now() + 86400000).toISOString();

  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    'host-alpha', 'Host Alpha', 'alpha.local', now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' } }),
    0,
    JSON.stringify({ jobs: ['root-run'] })
  );
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    'host-beta', 'Host Beta', 'beta.local', now, now,
    JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' } }),
    0,
    JSON.stringify({ jobs: ['root-run'] })
  );

  const tokenHostAlpha = await sha256('token_host_alpha');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_alpha', 'host', tokenHostAlpha, 'fam_alpha', 'host-alpha', 'host-alpha', JSON.stringify(['host']), 'admin-1', now, future
  );
  const tokenHostBeta = await sha256('token_host_beta');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_host_beta', 'host', tokenHostBeta, 'fam_beta', 'host-beta', 'host-beta', JSON.stringify(['host']), 'admin-1', now, future
  );
  const tokenAdmin = await sha256('token_admin');
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, family_id, subject, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    'cred_admin', 'human', tokenAdmin, 'fam_admin', 'admin@fake.local', JSON.stringify(['admin', 'jobs:create', 'bus:read', 'bus:send']), 'admin-1', now, future
  );
}

async function seedOfferedRootJob(sql: any, jobId: string, hostId: string, idempotencyKey: string) {
  const now = new Date().toISOString();
  const future = new Date(Date.now() + 600000).toISOString();
  sql.exec(
    `INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    jobId, 'root-run', idempotencyKey, JSON.stringify({}), 'offered', now, future, 'role:root',
    hostId, hostId, future
  );
}

const CLAIM_BODY = JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' }, profile_supported: true, native_root_supported: true });

function claimReq(jobId: string, token: string): Request {
  return new Request(`http://example.com/api/jobs/${jobId}/claim`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: CLAIM_BODY
  });
}

const EVIDENCE = 'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';

function startReq(jobId: string, token: string, epoch: number): Request {
  return new Request(`http://example.com/api/jobs/${jobId}/start`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ epoch, evidence_digest: EVIDENCE, launcher_task_id: 'task-1', native_session: 'sess-1' })
  });
}

async function adminGetScheduling(sessionCookie: string): Promise<Response> {
  return doFetch(new Request('http://example.com/api/root/scheduling', { headers: { 'Cookie': sessionCookie } }));
}

async function adminPutScheduling(sessionCookie: string, body: any, csrf: string | null = 'csrf-test-token'): Promise<Response> {
  const headers: Record<string, string> = { 'Cookie': sessionCookie, 'Content-Type': 'application/json' };
  if (csrf !== null) headers['X-CSRF-Token'] = csrf;
  const bodyArg = body === null ? undefined : (typeof body === 'string' ? body : JSON.stringify(body));
  return doFetch(new Request('http://example.com/api/root/scheduling', { method: 'PUT', headers, body: bodyArg }));
}

const UTC_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;

describe('Root scheduling flag: auth and body validation', () => {
  it('default is enabled and readback requires an admin session (bearer and non-admin rejected)', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      const sessionCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
      const nonAdminCookie = await createSessionCookie(instance.env, {
        sub: 'user-1', email: 'user@fake.local', isAdmin: false, csrfToken: 'csrf-u',
        exp: Math.floor(Date.now() / 1000) + 3600
      });

      // No session at all -> 401
      const resAnon = await doFetch(new Request('http://example.com/api/root/scheduling'));
      expect(resAnon.status).toBe(401);

      // Bearer tokens that are not a verified host credential are rejected: a legacy
      // non-hubt token and a human-admin bearer (host read access is tested below)
      const resHostBearer = await doFetch(new Request('http://example.com/api/root/scheduling', {
        headers: { 'Authorization': 'Bearer token_host_alpha' }
      }));
      expect(resHostBearer.status).toBe(401);
      const resAdminBearer = await doFetch(new Request('http://example.com/api/root/scheduling', {
        headers: { 'Authorization': 'Bearer token_admin' }
      }));
      expect(resAdminBearer.status).toBe(401);

      // Non-admin session cookie fails session validation (getSession enforces the
      // admin allowlist), so it is rejected like any unauthenticated request -> 401
      const resNonAdmin = await adminGetScheduling(nonAdminCookie);
      expect(resNonAdmin.status).toBe(401);

      // Admin session -> 200 with safe default enabled
      const res = await adminGetScheduling(sessionCookie);
      expect(res.status).toBe(200);
      const data = await res.json() as any;
      expect(data.enabled).toBe(true);
      expect(typeof data.updated_at).toBe('string');
      expect(UTC_ISO.test(data.updated_at)).toBe(true);

      // Method restriction
      const resPost = await doFetch(new Request('http://example.com/api/root/scheduling', {
        method: 'POST', headers: { 'Cookie': sessionCookie }, body: '{}'
      }));
      expect(resPost.status).toBe(405);
    });
  });

  it('PUT requires session CSRF and a strict {"enabled": boolean} body', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      const sessionCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });

      // Missing CSRF -> 403
      const resNoCsrf = await adminPutScheduling(sessionCookie, { enabled: false }, null);
      expect(resNoCsrf.status).toBe(403);
      const noCsrfData = await resNoCsrf.json() as any;
      expect(noCsrfData.error).toContain('CSRF');

      // Wrong CSRF -> 403
      const resBadCsrf = await adminPutScheduling(sessionCookie, { enabled: false }, 'wrong-token');
      expect(resBadCsrf.status).toBe(403);

      // enabled with wrong type -> 400
      const resWrongType = await adminPutScheduling(sessionCookie, { enabled: 'yes' });
      expect(resWrongType.status).toBe(400);
      expect(((await resWrongType.json()) as any).error).toBe('invalid_enabled');

      // Extra fields rejected -> 400
      const resExtra = await adminPutScheduling(sessionCookie, { enabled: false, force: true, actor: 'root' });
      expect(resExtra.status).toBe(400);
      expect(((await resExtra.json()) as any).error).toBe('invalid_body');

      // Empty object -> 400
      const resEmpty = await adminPutScheduling(sessionCookie, {});
      expect(resEmpty.status).toBe(400);

      // Malformed JSON -> 400
      const resBadJson = await adminPutScheduling(sessionCookie, '{not-json');
      expect(resBadJson.status).toBe(400);
      expect(((await resBadJson.json()) as any).error).toBe('malformed_json');

      // Missing body -> 400
      const resNoBody = await adminPutScheduling(sessionCookie, null);
      expect(resNoBody.status).toBe(400);

      // Nothing changed so far
      const before = await adminGetScheduling(sessionCookie);
      expect(((await before.json()) as any).enabled).toBe(true);

      // Valid disable -> 200
      const res = await adminPutScheduling(sessionCookie, { enabled: false });
      expect(res.status).toBe(200);
      const data = await res.json() as any;
      expect(data.enabled).toBe(false);
      expect(UTC_ISO.test(data.updated_at)).toBe(true);

      const readback = await adminGetScheduling(sessionCookie);
      const readbackData = await readback.json() as any;
      expect(readbackData.enabled).toBe(false);
      expect(readbackData.updated_at).toBe(data.updated_at);
    });
  });
});

describe('Root scheduling flag: scheduler and claim gating', () => {
  it('disabled tick creates no root-run job and no offers; re-enable resumes the same slot (fixed clock)', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      const sessionCookie = await adminCookie(instance.env);

      await withFixedClock(instance, FIXED_NOW, async () => {
        freshenHosts(sql, FIXED_NOW);

        // Disable via the API
        const disableRes = await adminPutScheduling(sessionCookie, { enabled: false });
        expect(disableRes.status).toBe(200);

        // Tick the fixed slot while disabled
        const tickRes = await instance.fetch(tickReq(FIXED_SLOT));
        expect(tickRes.status).toBe(200);
        const tickData = await tickRes.json() as any;
        expect(tickData.status).toBe('ok');
        expect(tickData.tick).toEqual({ status: 'disabled', slot: FIXED_SLOT });
        expect(sql.exec("SELECT id FROM jobs WHERE kind = 'root-run'").toArray().length).toBe(0);
        expect(sql.exec('SELECT * FROM offers').toArray().length).toBe(0);

        // Re-enable and tick the same slot: scheduling resumes
        const enableRes = await adminPutScheduling(sessionCookie, { enabled: true });
        expect(enableRes.status).toBe(200);

        const tick2Res = await instance.fetch(tickReq(FIXED_SLOT));
        expect(tick2Res.status).toBe(200);
        expect(((await tick2Res.json()) as any).tick.status).toBe('ok');
        const jobs = sql.exec("SELECT id, status, deadline_at FROM jobs WHERE kind = 'root-run' AND idempotency_key = ?", `root-run:${FIXED_SLOT}`).toArray();
        expect(jobs.length).toBe(1);
        expect(jobs[0].status).toBe('offered');
        expect(jobs[0].deadline_at).toBe(new Date(Date.parse(FIXED_SLOT) + 25 * 60 * 1000).toISOString());
        expect(sql.exec('SELECT * FROM offers').toArray().length).toBe(1);

        // Duplicate tick stays idempotent
        const tick3Res = await instance.fetch(tickReq(FIXED_SLOT));
        expect(tick3Res.status).toBe(200);
        expect(sql.exec("SELECT id FROM jobs WHERE kind = 'root-run' AND idempotency_key = ?", `root-run:${FIXED_SLOT}`).toArray().length).toBe(1);
      });
    });
  });

  it('tick slot-age boundary is decided by the injected clock, not the wall clock', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      setRootScheduling(sql, false, FIXED_NOW.toISOString());

      // 24:59 into the slot: accepted (disabled tick, 200)
      const justInTime = new Date(Date.parse(FIXED_SLOT) + 25 * 60 * 1000 - 1000);
      await withFixedClock(instance, justInTime, async () => {
        const res = await instance.fetch(tickReq(FIXED_SLOT));
        expect(res.status).toBe(200);
        expect(((await res.json()) as any).tick.status).toBe('disabled');
      });

      // 25:00 into the slot: the existing late-slot refusal (400) applies
      const late = new Date(Date.parse(FIXED_SLOT) + 25 * 60 * 1000);
      await withFixedClock(instance, late, async () => {
        const res = await instance.fetch(tickReq(FIXED_SLOT));
        expect(res.status).toBe(400);
        expect(((await res.json()) as any).reason).toBe('late_slot_refused');
      });
    });
  });

  it('disable refuses new claims of offered root-run jobs; enable restores claiming', async () => {
    const stub = await getStub();
    const sessionCookie = await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      await seedOfferedRootJob(instance.ctx.storage.sql, 'job-dis-1', 'host-alpha', 'root-run:dis-1');
      return await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });

    const disableRes = await adminPutScheduling(sessionCookie, { enabled: false });
    expect(disableRes.status).toBe(200);

    // Claim of the offered job is refused while disabled
    const claimRes = await doFetch(claimReq('job-dis-1', 'token_host_alpha'));
    expect(claimRes.status).toBe(409);
    const claimData = await claimRes.json() as any;
    expect(claimData.error).toBe('conflict');

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      expect(lease.holder_job_id).toBeNull();
      expect(lease.epoch).toBe(0);
    });

    // Enable restores claiming of the still-offered job
    const enableRes = await adminPutScheduling(sessionCookie, { enabled: true });
    expect(enableRes.status).toBe(200);

    const claim2Res = await doFetch(claimReq('job-dis-1', 'token_host_alpha'));
    expect(claim2Res.status).toBe(200);
    const claim2Data = await claim2Res.json() as any;
    expect(claim2Data.job_token).toBeDefined();
    expect(claim2Data.lease_epoch).toBe(1);
  });

  it('START transitions are gated while disabled; a running root keeps renewing and finishing', async () => {
    const stub = await getStub();
    const sessionCookie = await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      await seedOfferedRootJob(instance.ctx.storage.sql, 'job-gate-1', 'host-alpha', 'root-run:gate-1');
      return await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });

    // Claim while enabled (single root lease: this job holds it, epoch 1, not started)
    const claim = await doFetch(claimReq('job-gate-1', 'token_host_alpha'));
    expect(claim.status).toBe(200);
    const claimData = await claim.json() as any;
    expect(claimData.lease_epoch).toBe(1);

    // Disable, then try to START: refused
    const disableRes = await adminPutScheduling(sessionCookie, { enabled: false });
    expect(disableRes.status).toBe(200);

    const startBlocked = await doFetch(startReq('job-gate-1', claimData.job_token, claimData.lease_epoch));
    expect(startBlocked.status).toBe(409);
    expect(((await startBlocked.json()) as any).error).toBe('root_scheduling_disabled');

    // Enable restores the START transition
    const enableRes = await adminPutScheduling(sessionCookie, { enabled: true });
    expect(enableRes.status).toBe(200);
    const startOk = await doFetch(startReq('job-gate-1', claimData.job_token, claimData.lease_epoch));
    expect(startOk.status).toBe(200);

    // Disable again while the root is RUNNING: not killed - renew and finish work
    const disable2 = await adminPutScheduling(sessionCookie, { enabled: false });
    expect(disable2.status).toBe(200);

    const statusRes = await doFetch(new Request('http://example.com/api/jobs/job-gate-1/status', {
      headers: { 'Authorization': 'Bearer token_admin' }
    }));
    expect(statusRes.status).toBe(200);
    expect(((await statusRes.json()) as any).job.status).toBe('running');

    const renewRes = await doFetch(new Request('http://example.com/api/jobs/job-gate-1/renew', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${claimData.job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: claimData.lease_epoch })
    }));
    expect(renewRes.status).toBe(200);

    const finishRes = await doFetch(new Request('http://example.com/api/jobs/job-gate-1/finish', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${claimData.job_token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ epoch: claimData.lease_epoch, status: 'succeeded', summary: 'finished while scheduling disabled', evidence_refs: [] })
    }));
    expect(finishRes.status).toBe(200);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const job = sql.exec("SELECT status FROM jobs WHERE id = 'job-gate-1'").toArray()[0] as any;
      expect(job.status).toBe('succeeded');
      const lease = sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      expect(lease.holder_job_id).toBeNull();
    });
  });
});

describe('Root scheduling flag: idempotence, durability, races', () => {
  it('repeated stop/start is idempotent and durable across DO restart', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      const sessionCookie = await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });

      // First disable
      const d1 = await adminPutScheduling(sessionCookie, { enabled: false });
      expect(d1.status).toBe(200);
      const d1Data = await d1.json() as any;

      // Repeated disable: idempotent, updated_at untouched
      const d2 = await adminPutScheduling(sessionCookie, { enabled: false });
      expect(d2.status).toBe(200);
      const d2Data = await d2.json() as any;
      expect(d2Data.updated_at).toBe(d1Data.updated_at);

      // Enable, repeat
      const e1 = await adminPutScheduling(sessionCookie, { enabled: true });
      expect(e1.status).toBe(200);
      const e1Data = await e1.json() as any;
      expect(e1Data.enabled).toBe(true);
      expect(e1Data.updated_at).not.toBe(d1Data.updated_at);

      const e2 = await adminPutScheduling(sessionCookie, { enabled: true });
      expect(e2.status).toBe(200);
      const e2Data = await e2.json() as any;
      expect(e2Data.updated_at).toBe(e1Data.updated_at);

      // Exactly one state row
      const rows = instance.ctx.storage.sql.exec('SELECT id FROM root_scheduling').toArray();
      expect(rows.length).toBe(1);

      // Disable again for the restart check
      const d3 = await adminPutScheduling(sessionCookie, { enabled: false });
      expect(d3.status).toBe(200);

      // Simulate restart: a fresh Hub instance over the same storage must see the persisted flag
      const hub2 = new Hub(instance.ctx, instance.env);
      const res = await hub2.fetch(new Request('http://example.com/api/root/scheduling', { headers: { 'Cookie': sessionCookie } }));
      expect(res.status).toBe(200);
      expect(((await res.json()) as any).enabled).toBe(false);

      // A fresh JobsService also honors the persisted flag
      await seedOfferedRootJob(instance.ctx.storage.sql, 'job-after-restart', 'host-alpha', 'root-run:after-restart');
      const svc = new JobsService(instance.ctx.storage.sql, instance.ctx);
      const claim = await svc.claimJob('job-after-restart', 'host-alpha', true);
      expect(claim.error).toBe('conflict');
      expect(claim.reason).toBe('root_scheduling_disabled');
    });
  });

  it('two-host claim race: exactly one winner when enabled; both refused when disabled', async () => {
    const stub = await getStub();
    const sessionCookie = await (runInDurableObject as any)(stub, async (instance: any) => {
      await seedBase(instance.ctx.storage.sql);
      await seedOfferedRootJob(instance.ctx.storage.sql, 'job-race-a', 'host-alpha', 'root-run:race-a');
      await seedOfferedRootJob(instance.ctx.storage.sql, 'job-race-b', 'host-beta', 'root-run:race-b');
      return await createSessionCookie(instance.env, {
        sub: 'admin-1', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf-test-token',
        exp: Math.floor(Date.now() / 1000) + 3600
      });
    });

    // Enabled: exactly one of the two racing claims wins, epoch +1 exactly once
    const [resA, resB] = await Promise.all([
      doFetch(claimReq('job-race-a', 'token_host_alpha')),
      doFetch(claimReq('job-race-b', 'token_host_beta'))
    ]);
    const statuses = [resA.status, resB.status].sort();
    expect(statuses).toEqual([200, 409]);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const lease = instance.ctx.storage.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      expect(lease.epoch).toBe(1);
      expect(['job-race-a', 'job-race-b']).toContain(lease.holder_job_id);
    });

    // Disabled: release the lease, offer two fresh jobs, both claims refused, epoch untouched
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      sql.exec("UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, holder_actor = NULL, expires_at = NULL, released_at = ? WHERE name = 'root'", new Date().toISOString());
      setRootScheduling(sql, false, new Date().toISOString());
      await seedOfferedRootJob(sql, 'job-race-a2', 'host-alpha', 'root-run:race-a2');
      await seedOfferedRootJob(sql, 'job-race-b2', 'host-beta', 'root-run:race-b2');
    });

    const [resA2, resB2] = await Promise.all([
      doFetch(claimReq('job-race-a2', 'token_host_alpha')),
      doFetch(claimReq('job-race-b2', 'token_host_beta'))
    ]);
    expect(resA2.status).toBe(409);
    expect(resB2.status).toBe(409);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const lease = sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      expect(lease.epoch).toBe(1);
      expect(lease.holder_job_id).toBeNull();
      for (const jobId of ['job-race-a2', 'job-race-b2']) {
        const job = sql.exec('SELECT status FROM jobs WHERE id = ?', jobId).toArray()[0] as any;
        expect(job.status).toBe('offered');
      }
    });
  });

  it('takeover claim path is refused while disabled and works when enabled', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedOfferedRootJob(sql, 'job-take-1', 'host-alpha', 'root-run:take-1');

      setRootScheduling(sql, false, new Date().toISOString());
      const denied = await instance.jobsService.takeoverRoot('job-take-1', 'host-alpha', 0, 'test while disabled');
      expect(denied.error).toBe('root_scheduling_disabled');

      setRootScheduling(sql, true, new Date().toISOString());
      const ok = await instance.jobsService.takeoverRoot('job-take-1', 'host-alpha', 0, 'test while enabled');
      expect(ok.success).toBe(true);
      expect(ok.lease_epoch).toBe(1);
    });
  });

  it('module unit: idempotent set, missing row falls back to enabled', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);

      expect(isRootSchedulingEnabled(sql)).toBe(true);
      const initial = getRootScheduling(sql);
      expect(initial.enabled).toBe(true);
      expect(typeof initial.updated_at).toBe('string');

      const t1 = new Date().toISOString();
      const s1 = setRootScheduling(sql, false, t1);
      expect(s1).toEqual({ enabled: false, updated_at: t1 });

      // Same value: no write, updated_at unchanged
      const s2 = setRootScheduling(sql, false, new Date(Date.now() + 5000).toISOString());
      expect(s2.updated_at).toBe(t1);

      expect(isRootSchedulingEnabled(sql)).toBe(false);

      // Missing row defaults to enabled (safe default = current behavior)
      sql.exec('DELETE FROM root_scheduling');
      expect(isRootSchedulingEnabled(sql)).toBe(true);
      expect(getRootScheduling(sql)).toEqual({ enabled: true, updated_at: null });
    });
  });
});

const HOST_TOKEN_ALPHA = 'hubt_hostalphareadtoken0001';

// Host bearer credentials in the real device-login format (hubt_ prefix), verified
// by the same helper as /api/host/poll. Only the first row is a valid host reader.
async function seedHostBearers(sql: any) {
  const now = new Date().toISOString();
  const future = new Date(Date.now() + 86400000).toISOString();
  const past = new Date(Date.now() - 60000).toISOString();
  const rows: Array<[string, string, string, string | null, string[], string, string | null]> = [
    ['cred_hubt_alpha', HOST_TOKEN_ALPHA, 'host', 'host-alpha', ['host'], future, null],
    ['cred_hubt_revoked', 'hubt_hostrevokedtoken0001', 'host', 'host-alpha', ['host'], future, now],
    ['cred_hubt_expired', 'hubt_hostexpiredtoken0001', 'host', 'host-alpha', ['host'], past, null],
    ['cred_hubt_agent', 'hubt_agentwithhostscope0001', 'agent', 'host-alpha', ['host'], future, null],
    ['cred_hubt_hostadmin', 'hubt_hostwithadminscope0001', 'host', 'host-alpha', ['host', 'admin'], future, null],
    ['cred_hubt_nomachine', 'hubt_hostwithoutmachine0001', 'host', null, ['host'], future, null],
    ['cred_hubt_noscope', 'hubt_hostwithoutscope00001', 'host', 'host-alpha', ['bus:read'], future, null]
  ];
  for (const [id, token, kind, machineId, scopes, expiresAt, revokedAt] of rows) {
    sql.exec(
      "INSERT INTO credentials (id, kind, token_hash, family_id, subject, machine_id, scopes, created_by_sub, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      id, kind, await sha256(token), `fam_${id}`, machineId ?? 'no-machine', machineId, JSON.stringify(scopes), 'admin-1', now, expiresAt, revokedAt
    );
  }
}

function hostGetScheduling(token: string, query: string = ''): Promise<Response> {
  return doFetch(new Request(`http://example.com/api/root/scheduling${query}`, { headers: { 'Authorization': `Bearer ${token}` } }));
}

describe('Root scheduling flag: host read interface', () => {
  it('GET with a verified host bearer returns enabled/updated_at/server_time (UTC) and tracks admin changes', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedHostBearers(sql);
      const sessionCookie = await adminCookie(instance.env);

      const res = await hostGetScheduling(HOST_TOKEN_ALPHA);
      expect(res.status).toBe(200);
      const data = await res.json() as any;
      expect(Object.keys(data).sort()).toEqual(['enabled', 'server_time', 'updated_at']);
      expect(data.enabled).toBe(true);
      expect(UTC_ISO.test(data.updated_at)).toBe(true);
      expect(UTC_ISO.test(data.server_time)).toBe(true);

      const put = await adminPutScheduling(sessionCookie, { enabled: false });
      expect(put.status).toBe(200);
      const putData = await put.json() as any;

      const res2 = await hostGetScheduling(HOST_TOKEN_ALPHA);
      expect(res2.status).toBe(200);
      const data2 = await res2.json() as any;
      expect(data2.enabled).toBe(false);
      expect(data2.updated_at).toBe(putData.updated_at);

      // A caller-supplied host id is not an identity input
      const res3 = await hostGetScheduling(HOST_TOKEN_ALPHA, '?host_id=host-beta&machine_id=host-beta');
      expect(res3.status).toBe(200);
      expect(((await res3.json()) as any).enabled).toBe(false);

      const enable = await adminPutScheduling(sessionCookie, { enabled: true });
      expect(enable.status).toBe(200);
      const res4 = await hostGetScheduling(HOST_TOKEN_ALPHA);
      expect(((await res4.json()) as any).enabled).toBe(true);

      // Host reads write no audit rows; only the two admin changes are audited
      const audit = sql.exec("SELECT actor, action FROM audit WHERE resource = 'root_scheduling' ORDER BY id").toArray();
      expect(audit.map((a: any) => a.action)).toEqual(['root_scheduling_disable', 'root_scheduling_enable']);
      expect(audit.every((a: any) => a.actor === 'admin@fake.local')).toBe(true);
    });
  });

  it('GET denies unknown, stale, wrong-kind, wrong-scope, admin-scoped and machine-less bearer tokens', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedHostBearers(sql);

      const denied = [
        'hubt_unknowntoken000000001',   // never issued
        'hubt_hostrevokedtoken0001',    // revoked
        'hubt_hostexpiredtoken0001',    // expired
        'hubt_agentwithhostscope0001',  // kind agent, even with host scope
        'hubt_hostwithadminscope0001',  // admin-scoped bearer is never a host reader
        'hubt_hostwithoutmachine0001',  // no machine binding
        'hubt_hostwithoutscope00001',   // host kind without host scope
        'token_host_alpha'              // legacy non-hubt token format
      ];
      for (const token of denied) {
        const res = await hostGetScheduling(token, '?host_id=host-alpha');
        expect(res.status, token).toBe(401);
        expect(((await res.json()) as any).error, token).toBe('unauthorized');
      }

      const basic = await doFetch(new Request('http://example.com/api/root/scheduling', { headers: { 'Authorization': 'Basic aG9zdDpob3N0' } }));
      expect(basic.status).toBe(401);
      const empty = await doFetch(new Request('http://example.com/api/root/scheduling', { headers: { 'Authorization': 'Bearer ' } }));
      expect(empty.status).toBe(401);

      // A valid token goes stale on revocation: the next read is denied
      expect((await hostGetScheduling(HOST_TOKEN_ALPHA)).status).toBe(200);
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'cred_hubt_alpha'", new Date().toISOString());
      expect((await hostGetScheduling(HOST_TOKEN_ALPHA)).status).toBe(401);
    });
  });

  it('PUT with a host bearer is rejected and changes nothing', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedHostBearers(sql);
      const before = getRootScheduling(sql);

      for (const body of [{ enabled: false }, { enabled: false, host_id: 'host-alpha' }]) {
        const res = await doFetch(new Request('http://example.com/api/root/scheduling', {
          method: 'PUT',
          headers: { 'Authorization': `Bearer ${HOST_TOKEN_ALPHA}`, 'X-CSRF-Token': 'csrf-test-token', 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        }));
        expect(res.status).toBe(401);
      }

      expect(getRootScheduling(sql)).toEqual(before);
      expect(sql.exec("SELECT id FROM audit WHERE resource = 'root_scheduling'").toArray().length).toBe(0);
    });
  });
});

describe('Root scheduling flag: deadline expiry and offer visibility', () => {
  for (const mode of ['enable-then-alarms', 'alarms-while-disabled'] as const) {
    it(`re-enable after a long pause leaves no ghost offer (${mode})`, async () => {
      const stub = await getStub();
      await (runInDurableObject as any)(stub, async (instance: any) => {
        const sql = instance.ctx.storage.sql;
        await seedBase(sql);
        const sessionCookie = await adminCookie(instance.env);

        // Slot job offered at slot+5m (offer expires +7m, deadline +25m)
        const job = await withFixedClock(instance, FIXED_NOW, async () => {
          freshenHosts(sql, FIXED_NOW);
          const tick = await instance.fetch(tickReq(FIXED_SLOT));
          expect(tick.status).toBe(200);
          return sql.exec("SELECT * FROM jobs WHERE kind = 'root-run' AND idempotency_key = ?", `root-run:${FIXED_SLOT}`).toArray()[0] as any;
        });
        expect(job.status).toBe('offered');
        const offeredHost = job.offer_host_id;

        expect((await adminPutScheduling(sessionCookie, { enabled: false })).status).toBe(200);

        // Offer expired, deadline not yet: frozen while disabled (no expiry, no re-offer)
        const midPause = new Date(Date.parse(FIXED_SLOT) + 10 * MINUTE_MS);
        await withFixedClock(instance, midPause, async () => {
          freshenHosts(sql, midPause);
          instance.jobsService.processAlarms();
        });
        const frozen = sql.exec('SELECT status, offer_host_id FROM jobs WHERE id = ?', job.id).toArray()[0] as any;
        expect(frozen).toEqual({ status: 'offered', offer_host_id: offeredHost });
        expect(sql.exec('SELECT job_id FROM offers').toArray().length).toBe(1);

        // Three hours later the slot deadline is long past
        const later = new Date(Date.parse(FIXED_SLOT) + 3 * 60 * MINUTE_MS);
        await withFixedClock(instance, later, async () => {
          freshenHosts(sql, later);
          if (mode === 'alarms-while-disabled') {
            instance.jobsService.processAlarms();
            expect((sql.exec('SELECT status FROM jobs WHERE id = ?', job.id).toArray()[0] as any).status).toBe('expired');
          }
          expect((await adminPutScheduling(sessionCookie, { enabled: true })).status).toBe(200);
          instance.jobsService.processAlarms();

          const closed = sql.exec('SELECT status, offer_host_id, outcome_summary, finished_at FROM jobs WHERE id = ?', job.id).toArray()[0] as any;
          expect(closed.status).toBe('expired');
          expect(closed.offer_host_id).toBe(offeredHost);
          expect(closed.outcome_summary).toBe('deadline passed before claim');
          expect(closed.finished_at).toBe(later.toISOString());
          expect(sql.exec('SELECT job_id FROM offers').toArray().length).toBe(1);
          expect(sql.exec("SELECT id FROM health_events WHERE subject = ? AND detail = 'deadline passed before claim'", job.id).toArray().length).toBe(1);

          // Not listed to any host and not claimable
          for (const token of ['token_host_alpha', 'token_host_beta']) {
            const list = await doFetch(new Request('http://example.com/api/jobs/offers', { headers: { 'Authorization': `Bearer ${token}` } }));
            expect(list.status).toBe(200);
            expect(((await list.json()) as any).offers.map((o: any) => o.id)).not.toContain(job.id);
          }
          const claim = await doFetch(claimReq(job.id, offeredHost === 'host-alpha' ? 'token_host_alpha' : 'token_host_beta'));
          expect(claim.status).toBe(409);
        });

        // Scheduling resumes normally for a current slot
        const slot2 = new Date(Date.parse(FIXED_SLOT) + 3 * 60 * MINUTE_MS).toISOString();
        const slot2Now = new Date(Date.parse(slot2) + 5 * MINUTE_MS);
        await withFixedClock(instance, slot2Now, async () => {
          freshenHosts(sql, slot2Now);
          const tick = await instance.fetch(tickReq(slot2));
          expect(tick.status).toBe(200);
          expect(((await tick.json()) as any).tick.status).toBe('ok');
        });
        const next = sql.exec("SELECT status FROM jobs WHERE idempotency_key = ?", `root-run:${slot2}`).toArray()[0] as any;
        expect(next.status).toBe('offered');
        expect(sql.exec('SELECT job_id FROM offers').toArray().length).toBe(2);
      });
    });
  }

  it('decline of a past-deadline root-run offer closes the job instead of re-offering', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedOfferedRootJob(sql, 'job-decl-live', 'host-alpha', 'root-run:decl-live');
      await seedOfferedRootJob(sql, 'job-decl-past', 'host-alpha', 'root-run:decl-past');
      sql.exec("UPDATE jobs SET deadline_at = ? WHERE id = 'job-decl-past'", new Date(Date.now() - MINUTE_MS).toISOString());

      // Control: a live job is re-offered on decline
      expect(await instance.jobsService.declineJob('job-decl-live', 'host-alpha', 'busy')).toEqual({});
      expect((sql.exec("SELECT status FROM jobs WHERE id = 'job-decl-live'").toArray()[0] as any).status).toBe('offered');
      expect(sql.exec("SELECT job_id FROM offers WHERE job_id = 'job-decl-live'").toArray().length).toBe(1);

      // Past deadline: closed, no new offer row
      expect(await instance.jobsService.declineJob('job-decl-past', 'host-alpha', 'busy')).toEqual({});
      const past = sql.exec("SELECT status, outcome_summary FROM jobs WHERE id = 'job-decl-past'").toArray()[0] as any;
      expect(past).toEqual({ status: 'expired', outcome_summary: 'deadline passed before claim' });
      expect(sql.exec("SELECT job_id FROM offers WHERE job_id = 'job-decl-past'").toArray().length).toBe(0);
    });
  });

  it('root-run offers are hidden from host offer lists while disabled and listed again after enable', async () => {
    const stub = await getStub();
    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      await seedBase(sql);
      await seedHostBearers(sql);
      await seedOfferedRootJob(sql, 'job-vis-1', 'host-alpha', 'root-run:vis-1');
      const sessionCookie = await adminCookie(instance.env);

      const listed = async (): Promise<{ offers: string[]; poll: string[] }> => {
        const offersRes = await doFetch(new Request('http://example.com/api/jobs/offers', { headers: { 'Authorization': 'Bearer token_host_alpha' } }));
        expect(offersRes.status).toBe(200);
        const pollRes = await doFetch(new Request('http://example.com/api/host/poll', {
          method: 'POST',
          headers: { 'Authorization': `Bearer ${HOST_TOKEN_ALPHA}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ type: 'heartbeat', gate: { launch_allowed: true, reason: 'ok' } })
        }));
        expect(pollRes.status).toBe(200);
        return {
          offers: ((await offersRes.json()) as any).offers.map((o: any) => o.id),
          poll: ((await pollRes.json()) as any).offers.map((o: any) => o.id)
        };
      };

      expect(await listed()).toEqual({ offers: ['job-vis-1'], poll: ['job-vis-1'] });

      expect((await adminPutScheduling(sessionCookie, { enabled: false })).status).toBe(200);
      expect(await listed()).toEqual({ offers: [], poll: [] });
      expect((sql.exec("SELECT status FROM jobs WHERE id = 'job-vis-1'").toArray()[0] as any).status).toBe('offered');

      expect((await adminPutScheduling(sessionCookie, { enabled: true })).status).toBe(200);
      expect(await listed()).toEqual({ offers: ['job-vis-1'], poll: ['job-vis-1'] });

      // Restore the seeded heartbeat (the poll above rewrote it) and claim normally
      sql.exec('UPDATE machines SET last_heartbeat = ?, last_checkin_at = ?', JSON.stringify({ gate: { launch_allowed: true, reason: 'ok' } }), new Date().toISOString());
      const claim = await doFetch(claimReq('job-vis-1', 'token_host_alpha'));
      expect(claim.status).toBe(200);
    });
  });
});
