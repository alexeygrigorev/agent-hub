import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { JobsService } from '../src/jobs';

function createRng(initialSeed = 123456789) {
  let seed = initialSeed;
  return (arr: Uint32Array) => {
    for (let i = 0; i < arr.length; i++) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      arr[i] = seed;
    }
    return arr;
  };
}

describe('M4 Scheduler Contract and Durability Tests', () => {
  it('24h simulation, 48 slots, never concurrent lease, coverage of outcomes', async () => {
    const id = (env as any).HUB.idFromName("test-m4-sim-48slots");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let simulatedTimeMs = new Date('2026-10-11T00:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(simulatedTimeMs),
        getRandomValues: createRng(42),
      });

      // Inject hosts with real structured heartbeat per SPEC §7.2
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host1", "h1", "h1",
        new Date(simulatedTimeMs).toISOString(),
        new Date(simulatedTimeMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
        0,
        JSON.stringify({ jobs: ["root-run"] })
      );
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host2", "h2", "h2",
        new Date(simulatedTimeMs).toISOString(),
        new Date(simulatedTimeMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }),
        0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      let slotsRan = 0;
      let slotsSkipped = 0;

      for (let i = 0; i < 48; i++) {
        const slotStartIso = new Date(simulatedTimeMs).toISOString();
        sql.exec("UPDATE machines SET last_checkin_at = ?", slotStartIso);

        service.tickRootSlot(slotStartIso);

        const job = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${slotStartIso}`).toArray() as any[])[0];
        expect(job).toBeDefined();

        if (job.status === 'skip') {
          slotsSkipped++;
          simulatedTimeMs += 30 * 60000;
          continue;
        }

        slotsRan++;
        expect(job.status).toBe('offered');

        // Claim acquires root lease for 20m
        const claim = await service.claimJob(job.id, job.offer_host_id, true);
        expect(claim.error).toBeUndefined();
        expect(claim.lease_epoch).toBeGreaterThan(0);

        // Start job
        const start = await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'digest-art', 'sess-1', 'task-1');
        expect(start.error).toBeUndefined();

        // 5 minutes in: renew lease
        simulatedTimeMs += 5 * 60000;
        service.processAlarms();
        const renew = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
        expect(renew.error).toBeUndefined();

        // 5 minutes later: finish job
        simulatedTimeMs += 5 * 60000;
        service.processAlarms();
        const finish = await service.finishJob(job.id, claim.job_token!, claim.lease_epoch!, 'succeeded', 'completed', ['log.txt']);
        expect(finish.error).toBeUndefined();

        // Advance to next 30m slot
        simulatedTimeMs = new Date('2026-10-11T00:00:00Z').getTime() + (i + 1) * 30 * 60000;
        service.processAlarms();

        // Root lease is cleanly released
        const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
        expect(lease.holder_job_id).toBeNull();
      }

      expect(slotsRan + slotsSkipped).toBe(48);
      expect(slotsRan).toBe(48);
    });
  });

  it('race one lease epoch + 1: concurrent claims result in exactly one lease and epoch+1', async () => {
    const id = (env as any).HUB.idFromName("test-race-claim");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(100),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "hostA", "hA", "hA",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];

      // Initial root epoch is 0
      const initialLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(initialLease.epoch).toBe(0);

      // Winner claims
      const winnerClaim = await service.claimJob(job.id, "hostA", true);
      expect(winnerClaim.error).toBeUndefined();
      expect(winnerClaim.lease_epoch).toBe(1);

      // Second attempt gets conflict
      const loserClaim = await service.claimJob(job.id, "hostA", true);
      expect(loserClaim.error).toBe('conflict');

      // Stale finish attempt with invalid epoch rejected
      const staleFinish = await service.finishJob(job.id, winnerClaim.job_token!, 999, 'succeeded', 'stale', []);
      expect(staleFinish.error).toBe('unauthorized_or_stale');

      const leaseAfter = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(leaseAfter.holder_job_id).toBe(job.id);
      expect(leaseAfter.epoch).toBe(1);
    });
  });

  it('idempotent UTC slot: duplicate tickRootSlot calls for same slot are no-ops', async () => {
    const id = (env as any).HUB.idFromName("test-idempotent-slot");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(200),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-idemp", "h-idemp", "h-idemp",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      const slotIso = new Date(nowMs).toISOString();
      service.tickRootSlot(slotIso);
      service.tickRootSlot(slotIso);
      service.tickRootSlot(slotIso);

      const jobs = sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${slotIso}`).toArray();
      expect(jobs.length).toBe(1);
    });
  });

  it('120s TTL offer expiry and max 3 fallback: offers expire, fallback to next, then no-host', async () => {
    const id = (env as any).HUB.idFromName("test-ttl-max3");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(300),
      });

      for (let i = 1; i <= 4; i++) {
        sql.exec(
          "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          `host-${i}`, `h${i}`, `h${i}`,
          new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
          JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
          JSON.stringify({ jobs: ["root-run"] })
        );
      }

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job1 = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      expect(job1.status).toBe('offered');
      const firstHost = job1.offer_host_id;

      // 121 seconds later: offer 1 expires (machines stay fresh via regular check-in)
      nowMs += 121 * 1000;
      sql.exec("UPDATE machines SET last_checkin_at = ?", new Date(nowMs).toISOString());
      service.processAlarms();

      const job2 = (sql.exec("SELECT * FROM jobs WHERE id = ?", job1.id).toArray() as any[])[0];
      expect(job2.status).toBe('offered');
      expect(job2.offer_host_id).not.toBe(firstHost);

      // 121 seconds later: offer 2 expires
      nowMs += 121 * 1000;
      sql.exec("UPDATE machines SET last_checkin_at = ?", new Date(nowMs).toISOString());
      service.processAlarms();

      const job3 = (sql.exec("SELECT * FROM jobs WHERE id = ?", job1.id).toArray() as any[])[0];
      expect(job3.status).toBe('offered');

      // 121 seconds later: offer 3 expires -> max 3 reached -> no-host
      nowMs += 121 * 1000;
      service.processAlarms();

      const job4 = (sql.exec("SELECT * FROM jobs WHERE id = ?", job1.id).toArray() as any[])[0];
      expect(job4.status).toBe('no-host');
    });
  });

  it('no-host recorded when no eligible hosts exist', async () => {
    const id = (env as any).HUB.idFromName("test-no-host");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(400),
      });

      // No machines registered
      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      expect(job.status).toBe('no-host');
    });
  });

  it('skipped active: if root lease is active and holder has fresh checkin <= 180s, tickRootSlot skips', async () => {
    const id = (env as any).HUB.idFromName("test-skip-active");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(500),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-active", "h-active", "h-active",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job1 = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job1.id, "host-active", true);
      expect(claim.error).toBeUndefined();

      // Start job
      await service.startJob(job1.id, claim.job_token!, claim.lease_epoch!, 'digest', 'sess', 'task');

      // Next slot at 30m: root lease still held, host checked in 60s ago
      nowMs += 30 * 60000;
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-active'", new Date(nowMs - 60000).toISOString());

      // Extend lease slightly to simulate long root run
      sql.exec("UPDATE leases SET expires_at = ? WHERE name = 'root'", new Date(nowMs + 5 * 60000).toISOString());

      service.tickRootSlot(new Date(nowMs).toISOString());

      const job2 = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${new Date(nowMs).toISOString()}`).toArray() as any[])[0];
      expect(job2.status).toBe('skip');
    });
  });

  it('drain policy and fresh gates: correctly rejects ineligible machines', async () => {
    const id = (env as any).HUB.idFromName("test-drain-gates");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(600),
      });

      // 1. Drained machine
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-drained", "hd", "hd",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 1,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // 2. Stale checkin > 180s
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-stale", "hs", "hs",
        new Date(nowMs - 200000).toISOString(), new Date(nowMs - 200000).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // 3. Future checkin
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-future", "hf", "hf",
        new Date(nowMs + 10000).toISOString(), new Date(nowMs + 10000).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // 4. Missing root-run capability
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-nocap", "hnc", "hnc",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["writer-run"] })
      );

      // 5. Gate false
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-gatefail", "hgf", "hgf",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: false, reason: "quota exhausted" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      expect(job.status).toBe('no-host');
    });
  });

  it('backoff and restore: two failed root runs back off ONLY ONE next slot, restored on following slot', async () => {
    const id = (env as any).HUB.idFromName("test-backoff-restore");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(700),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-recovering", "hr", "hr",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Insert two previous failed runs from earlier slots
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "f-job-1", "root-run", "root-run:t1", "{}", "failed",
        new Date(nowMs - 60 * 60000).toISOString(), new Date(nowMs - 35 * 60000).toISOString(), "system", "host-recovering"
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "f-job-2", "root-run", "root-run:t2", "{}", "failed",
        new Date(nowMs - 30 * 60000).toISOString(), new Date(nowMs - 5 * 60000).toISOString(), "system", "host-recovering"
      );

      // Slot 1 (immediate next slot after failure): host is backed off
      service.tickRootSlot(new Date(nowMs).toISOString());
      const jobSlot1 = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${new Date(nowMs).toISOString()}`).toArray() as any[])[0];
      expect(jobSlot1.status).toBe('no-host');

      // Slot 2 (+30m): backoff of ONLY one next slot has expired; host is restored!
      nowMs += 30 * 60000;
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-recovering'", new Date(nowMs).toISOString());

      service.tickRootSlot(new Date(nowMs).toISOString());
      const jobSlot2 = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${new Date(nowMs).toISOString()}`).toArray() as any[])[0];
      expect(jobSlot2.status).toBe('offered');
      expect(jobSlot2.offer_host_id).toBe('host-recovering');
    });
  });

  it('expiry first action: claimed job without start within 5 minutes is marked failed', async () => {
    const id = (env as any).HUB.idFromName("test-fa-timeout");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(800),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-slow", "hs", "hs",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-slow", true);
      expect(claim.error).toBeUndefined();

      // 5 minutes and 1 second pass without startJob
      nowMs += 5 * 60000 + 1000;
      service.processAlarms();

      const expiredJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(expiredJob.status).toBe('failed');
      expect(expiredJob.outcome_summary).toBe('no first action');

      // Root lease is freed
      const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(lease.holder_job_id).toBeNull();
    });
  });

  it('renew hard deadlines and fencing: enforces epoch/token/deadline cap, prevents stale mutation', async () => {
    const id = (env as any).HUB.idFromName("test-renew-fencing");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(900),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-renew", "hr", "hr",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-renew", true);
      expect(claim.error).toBeUndefined();

      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'digest', 'sess', 'task');

      // 1. Valid renew at 5m
      nowMs += 5 * 60000;
      const renew1 = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
      expect(renew1.error).toBeUndefined();

      // 2. Stale epoch renew fails
      const renewStaleEpoch = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch! + 10);
      expect(renewStaleEpoch.error).toBe('unauthorized_or_stale');

      // 3. Stale token renew fails
      const renewWrongToken = await service.renewJob(job.id, "invalid-token", claim.lease_epoch!);
      expect(renewWrongToken.error).toBe('unauthorized_or_stale');

      // 4. Hard deadline cap: job deadline is 25m from slot start.
      nowMs = new Date(job.deadline_at).getTime();
      const renewPastDeadline = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
      expect(renewPastDeadline.error).toBe('deadline_exceeded');
    });
  });

  it('createJob permissions: root requires valid lease; principal and system allowed; arbitrary rejected', async () => {
    const id = (env as any).HUB.idFromName("test-createjob-perms");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(1000),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-creator", "hc", "hc",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const rootJob = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(rootJob.id, "host-creator", true);

      // 1. role:principal can create job without root lease
      const princJob = await service.createJob("head-recovery", { head: "h1" }, "role:principal");
      expect(princJob.jobId).toBeDefined();

      // 2. system actor can create job without root lease
      const sysJob = await service.createJob("custom", { note: "test" }, "system");
      expect(sysJob.jobId).toBeDefined();

      // 3. Arbitrary non-root actor is unauthorized
      const anonJob = await service.createJob("custom", {}, "attacker");
      expect(anonJob.error).toBe('unauthorized');

      // 4. role:root with valid token and epoch succeeds
      const rootSubJob = await service.createJob("custom", { task: "sub" }, "role:root", 25, claim.job_token!, claim.lease_epoch!);
      expect(rootSubJob.jobId).toBeDefined();

      // 5. role:root with wrong token fails
      const rootBadToken = await service.createJob("custom", {}, "role:root", 25, "bad-token", claim.lease_epoch!);
      expect(rootBadToken.error).toBe('unauthorized_or_stale');

      // 6. role:root after lease expiration fails
      nowMs += 25 * 60000;
      service.processAlarms();
      const rootExpiredJob = await service.createJob("custom", {}, "role:root", 25, claim.job_token!, claim.lease_epoch!);
      expect(rootExpiredJob.error).toBe('unauthorized_or_stale');
    });
  });

  it('declineJob: offered host declines before claim; wrong host decline rejected', async () => {
    const id = (env as any).HUB.idFromName("test-decline-flow");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(1100),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-dec-1", "hd1", "hd1",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-dec-2", "hd2", "hd2",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const offeredHost = job.offer_host_id;
      const otherHost = offeredHost === "host-dec-1" ? "host-dec-2" : "host-dec-1";

      // Wrong host decline rejected with conflict
      const wrongDecline = await service.declineJob(job.id, otherHost, "not my job");
      expect(wrongDecline.error).toBe('conflict');

      // Right host decline succeeds and re-offers to other host without burning epoch
      const rightDecline = await service.declineJob(job.id, offeredHost, "quota exhausted");
      expect(rightDecline.error).toBeUndefined();

      const updatedJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(updatedJob.status).toBe('offered');
      expect(updatedJob.offer_host_id).toBe(otherHost);

      // Root lease epoch was NOT incremented by decline
      const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(lease.epoch).toBe(0);
      expect(lease.holder_job_id).toBeNull();
    });
  });

  it('fallback new checkin: host checking in after initial slot is offered on retry at t+121s', async () => {
    const id = (env as any).HUB.idFromName("test-fallback-new-checkin");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(1200),
      });

      // host-1 registered and checked in at slot creation t=0
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-initial", "hi", "hi",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job1 = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      expect(job1.status).toBe('offered');
      expect(job1.offer_host_id).toBe('host-initial');

      // At t+10s: host-fallback checks in for the first time
      const host2CheckinMs = nowMs + 10 * 1000;
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-fallback", "hf", "hf",
        new Date(host2CheckinMs).toISOString(), new Date(host2CheckinMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // At t+121s: offer 1 expires; tryOfferNext evaluates candidates against current server clock
      // host-fallback has age 121s - 10s = 111s (fresh, positive age)
      nowMs += 121 * 1000;
      service.processAlarms();

      const job2 = (sql.exec("SELECT * FROM jobs WHERE id = ?", job1.id).toArray() as any[])[0];
      expect(job2.status).toBe('offered');
      expect(job2.offer_host_id).toBe('host-fallback');
    });
  });

  it('claimJob regression: server revalidation in same SQL transaction prevents unqualified mutations with exactly zero effects', async () => {
    const id = (env as any).HUB.idFromName("test-claim-server-revalidation");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(1300),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-gate-test", "hgt", "hgt",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      expect(job.status).toBe('offered');
      expect(job.offer_host_id).toBe('host-gate-test');

      const assertZeroEffects = () => {
        const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
        expect(lease?.epoch ?? 0).toBe(0);
        expect(lease?.holder_job_id ?? null).toBeNull();
        const currentJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
        expect(currentJob.status).toBe('offered');
        expect(currentJob.claim_token_hash).toBeNull();
        const creds = sql.exec("SELECT COUNT(*) as c FROM credentials WHERE subject = ?", job.id).toArray() as any[];
        expect(creds[0].c).toBe(0);
      };

      // 1. False caller flag conservatively denies
      const resFalseCaller = await service.claimJob(job.id, "host-gate-test", false);
      expect(resFalseCaller.error).toBe('gate_failed');
      assertZeroEffects();

      // 2. Drained host: true caller flag cannot qualify drained host
      sql.exec("UPDATE machines SET drained = 1 WHERE id = 'host-gate-test'");
      const resDrained = await service.claimJob(job.id, "host-gate-test", true);
      expect(resDrained.error).toBe('gate_failed');
      assertZeroEffects();
      sql.exec("UPDATE machines SET drained = 0 WHERE id = 'host-gate-test'");

      // 3. Stale checkin > 180s: true caller flag cannot qualify stale host
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-gate-test'", new Date(nowMs - 200000).toISOString());
      const resStale = await service.claimJob(job.id, "host-gate-test", true);
      expect(resStale.error).toBe('gate_failed');
      assertZeroEffects();

      // 4. Future checkin (negative age): true caller flag cannot qualify future checkin
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-gate-test'", new Date(nowMs + 60000).toISOString());
      const resFuture = await service.claimJob(job.id, "host-gate-test", true);
      expect(resFuture.error).toBe('gate_failed');
      assertZeroEffects();

      // 5. Nonfinite checkin: true caller flag cannot qualify invalid checkin timestamp
      sql.exec("UPDATE machines SET last_checkin_at = 'not-a-date' WHERE id = 'host-gate-test'");
      const resNonfinite = await service.claimJob(job.id, "host-gate-test", true);
      expect(resNonfinite.error).toBe('gate_failed');
      assertZeroEffects();

      // 6. Unsupported host: lacks root-run capability
      sql.exec(
        "UPDATE machines SET last_checkin_at = ?, capabilities = ? WHERE id = 'host-gate-test'",
        new Date(nowMs).toISOString(), JSON.stringify({ jobs: ["writer-run"] })
      );
      const resUnsupported = await service.claimJob(job.id, "host-gate-test", true);
      expect(resUnsupported.error).toBe('gate_failed');
      assertZeroEffects();

      // 7. Gate false in stored heartbeat
      sql.exec(
        "UPDATE machines SET capabilities = ?, last_heartbeat = ? WHERE id = 'host-gate-test'",
        JSON.stringify({ jobs: ["root-run"] }),
        JSON.stringify({ gate: { launch_allowed: false, reason: "quota exhausted" } })
      );
      const resGateNotAllowed = await service.claimJob(job.id, "host-gate-test", true);
      expect(resGateNotAllowed.error).toBe('gate_failed');
      assertZeroEffects();

      // 8. Stale/expired offer cannot qualify
      sql.exec(
        "UPDATE machines SET last_heartbeat = ? WHERE id = 'host-gate-test'",
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } })
      );
      sql.exec("UPDATE jobs SET offer_expires_at = ? WHERE id = ?", new Date(nowMs - 1000).toISOString(), job.id);
      const resOfferExpired = await service.claimJob(job.id, "host-gate-test", true);
      expect(resOfferExpired.error).toBe('conflict');
      assertZeroEffects();

      // 9. Past deadline cannot qualify
      sql.exec("UPDATE jobs SET offer_expires_at = ?, deadline_at = ? WHERE id = ?",
        new Date(nowMs + 60000).toISOString(), new Date(nowMs - 1000).toISOString(), job.id
      );
      const resDeadlineExpired = await service.claimJob(job.id, "host-gate-test", true);
      expect(resDeadlineExpired.error).toBe('conflict');
      assertZeroEffects();

      // 10. Legitimate fully qualified claim succeeds and mutates state
      sql.exec("UPDATE jobs SET deadline_at = ? WHERE id = ?", new Date(nowMs + 25 * 60000).toISOString(), job.id);
      const validClaim = await service.claimJob(job.id, "host-gate-test", true);
      expect(validClaim.error).toBeUndefined();
      expect(validClaim.lease_epoch).toBe(1);
      expect(validClaim.job_token).toBeDefined();

      const claimedLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(claimedLease.epoch).toBe(1);
      expect(claimedLease.holder_job_id).toBe(job.id);
      expect(claimedLease.holder_host_id).toBe("host-gate-test");

      const claimedJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(claimedJob.status).toBe('claimed');
      expect(claimedJob.lease_epoch).toBe(1);

      const cred = (sql.exec("SELECT * FROM credentials WHERE subject = ?", job.id).toArray() as any[])[0];
      expect(cred).toBeDefined();
      expect(JSON.parse(cred.scopes)).toContain('lease:root');
    });
  });

  it('lease identities: preserves correct root-run, job:<id>, writer and head lease identities', async () => {
    const id = (env as any).HUB.idFromName("test-lease-identities");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-11T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(1400),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-ident", "h-id", "h-id",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true, reason: "ok" } }), 0,
        JSON.stringify({ jobs: ["root-run", "writer-run", "head-recovery"] })
      );

      // 1. Root run job -> lease name 'root'
      service.tickRootSlot(new Date(nowMs).toISOString());
      const rootJob = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const rootClaim = await service.claimJob(rootJob.id, "host-ident", true);
      expect(rootClaim.error).toBeUndefined();
      const rootLease = (sql.exec("SELECT * FROM leases WHERE holder_job_id = ?", rootJob.id).toArray() as any[])[0];
      expect(rootLease.name).toBe('root');

      // 2. Writer job -> lease name 'writer:<date>'
      const writerJobId = "w-job-1";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        writerJobId, "writer-run", "writer-run:2026-10-12", JSON.stringify({ date: "2026-10-12" }),
        "offered", new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(),
        "host-ident", "host-ident", new Date(nowMs + 120000).toISOString(), "system"
      );
      const writerClaim = await service.claimJob(writerJobId, "host-ident", true);
      expect(writerClaim.error).toBeUndefined();
      const writerLease = (sql.exec("SELECT * FROM leases WHERE holder_job_id = ?", writerJobId).toArray() as any[])[0];
      expect(writerLease.name).toBe('writer:2026-10-12');
      const writerValid = await service.validateEpoch(writerJobId, writerClaim.job_token!, writerClaim.lease_epoch!);
      expect(writerValid).toBe(true);

      // 3. Head recovery job -> lease name 'head:<project>'
      const headJobId = "h-job-1";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        headJobId, "head-recovery", "head-rec:core", JSON.stringify({ project: "core" }),
        "offered", new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(),
        "host-ident", "host-ident", new Date(nowMs + 120000).toISOString(), "role:principal"
      );
      const headClaim = await service.claimJob(headJobId, "host-ident", true);
      expect(headClaim.error).toBeUndefined();
      const headLease = (sql.exec("SELECT * FROM leases WHERE holder_job_id = ?", headJobId).toArray() as any[])[0];
      expect(headLease.name).toBe('head:core');
      const headValid = await service.validateEpoch(headJobId, headClaim.job_token!, headClaim.lease_epoch!);
      expect(headValid).toBe(true);

      // 4. Custom job -> lease name 'job:<id>'
      const customJobId = "c-job-1";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        customJobId, "custom", "custom-1", "{}",
        "offered", new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(),
        "host-ident", "host-ident", new Date(nowMs + 120000).toISOString(), "role:worker"
      );
      const customClaim = await service.claimJob(customJobId, "host-ident", true);
      expect(customClaim.error).toBeUndefined();
      const customLease = (sql.exec("SELECT * FROM leases WHERE holder_job_id = ?", customJobId).toArray() as any[])[0];
      expect(customLease.name).toBe(`job:${customJobId}`);
      const customValid = await service.validateEpoch(customJobId, customClaim.job_token!, customClaim.lease_epoch!);
      expect(customValid).toBe(true);
    });
  });
});
