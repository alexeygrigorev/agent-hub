import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { JobsService } from '../src/jobs';
import { BusService } from '../src/bus';

function createRng(initialSeed = 987654321) {
  let seed = initialSeed;
  return (arr: Uint32Array) => {
    for (let i = 0; i < arr.length; i++) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      arr[i] = seed;
    }
    return arr;
  };
}

describe('M4 Lease Semantics, Epoch CAS, and Safety Tests', () => {
  it('two contenders: exactly one epoch CAS succeeds, the other gets conflict, epoch increments by 1', async () => {
    const id = (env as any).HUB.idFromName("test-lease-two-contenders");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T10:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(10),
      });

      // Register two capable candidate hosts
      for (const h of ['host-one', 'host-two']) {
        sql.exec(
          "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          h, h, h,
          new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
          JSON.stringify({ gate: { launch_allowed: true } }), 0,
          JSON.stringify({ jobs: ["root-run"] })
        );
      }

      // Create two candidate jobs for root-run
      const job1Id = "job-cand-1";
      const job2Id = "job-cand-2";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', ?, '{}', 'offered', ?, ?, 'system', ?, ?, ?)",
        job1Id, 'cand:1', new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), 'host-one', 'host-one', new Date(nowMs + 120000).toISOString()
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', ?, '{}', 'offered', ?, ?, 'system', ?, ?, ?)",
        job2Id, 'cand:2', new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), 'host-two', 'host-two', new Date(nowMs + 120000).toISOString()
      );

      // Current root lease starts at epoch 0
      const initLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(initLease.epoch).toBe(0);

      // Contender 1 attempts takeover expecting epoch 0
      const win = await service.takeoverRoot(job1Id, 'host-one', 0, 'initial_takeover');
      expect(win.success).toBe(true);
      expect(win.lease_epoch).toBe(1);

      // Contender 2 attempts takeover expecting epoch 0 (which has now advanced)
      const lose = await service.takeoverRoot(job2Id, 'host-two', 0, 'concurrent_takeover');
      expect(lose.error).toBe('cas_failed');
      expect(lose.current_epoch).toBe(1);

      // Exactly one holder and epoch is 1
      const finalLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(finalLease.epoch).toBe(1);
      expect(finalLease.holder_job_id).toBe(job1Id);
      expect(finalLease.holder_host_id).toBe('host-one');

      // Loser job remains in offered state without burning lease
      const loserJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job2Id).toArray() as any[])[0];
      expect(loserJob.status).toBe('offered');
    });
  });

  it('negative zero-effect: stale epoch rejected across verify, commitAction, renew, finish', async () => {
    const id = (env as any).HUB.idFromName("test-stale-epoch-zero-effect");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T11:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(20),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-stale-test", "hst", "hst",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-stale-test", true);
      expect(claim.lease_epoch).toBe(1);

      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-stale', 'task-stale');

      // 1. verifyRootEpoch with stale epoch 0
      const verifyStale = await service.verifyRootEpoch(job.id, claim.job_token!, 0);
      expect(verifyStale.valid).toBe(false);
      expect(verifyStale.status).toBe('fenced');
      expect(verifyStale.reason).toBe('superseded');

      // 2. commitRootAction with stale epoch 0
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('target-1', 'custom', 'k1', '{}', 'running', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );
      const actionStale = await service.commitRootAction(
        job.id, "host-stale-test", 0,
        { type: 'job_transition', payload: { target_job_id: 'target-1', new_status: 'succeeded' } },
        'sess-stale'
      );
      expect(actionStale.success).toBe(false);
      expect(actionStale.status).toBe('fenced');

      // Verify target job was NOT modified (zero-effect)
      const targetJob = (sql.exec("SELECT * FROM jobs WHERE id = 'target-1'").toArray() as any[])[0];
      expect(targetJob.status).toBe('running');

      // 3. renewJob with stale epoch
      const renewStale = await service.renewJob(job.id, claim.job_token!, 0);
      expect(renewStale.error).toBe('unauthorized_or_stale');

      // 4. finishJob with stale epoch
      const finishStale = await service.finishJob(job.id, claim.job_token!, 0, 'succeeded', 'stale summary', []);
      expect(finishStale.error).toBe('unauthorized_or_stale');

      // Verify root job remains running and lease remains active (zero-effect)
      const rootJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(rootJob.status).toBe('running');
      const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(lease.holder_job_id).toBe(job.id);
      expect(lease.epoch).toBe(1);
    });
  });

  it('negative zero-effect: wrong host binding rejected', async () => {
    const id = (env as any).HUB.idFromName("test-wrong-host-zero-effect");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(30),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "legit-host", "lh", "lh",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "legit-host", true);
      expect(claim.error).toBeUndefined();
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-host', 'task-host');

      // Register rogue host after claim
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "rogue-host", "rh", "rh",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Rogue host attempts verifyRootEpoch
      const verifyWrong = await service.verifyRootEpoch(job.id, claim.job_token!, claim.lease_epoch!, false, "rogue-host");
      expect(verifyWrong.valid).toBe(false);
      expect(verifyWrong.status).toBe('unauthorized');
      expect(verifyWrong.reason).toBe('host_mismatch');

      // Rogue host attempts commitRootAction
      const actionWrong = await service.commitRootAction(
        job.id, "rogue-host", claim.lease_epoch!,
        { type: 'job_transition', payload: { target_job_id: 'any', new_status: 'succeeded' } },
        'sess-host'
      );
      expect(actionWrong.success).toBe(false);
      expect(actionWrong.status).toBe('unauthorized');
      expect(actionWrong.reason).toBe('holder_binding_mismatch');

      // Zero actions written
      const rootActions = sql.exec("SELECT * FROM root_actions").toArray();
      expect(rootActions.length).toBe(0);
    });
  });

  it('negative zero-effect: revoked job credential rejected across all operations', async () => {
    const id = (env as any).HUB.idFromName("test-revoked-token-zero-effect");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T13:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(40),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-rev", "hr", "hr",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-rev", true);
      expect(claim.error).toBeUndefined();
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-rev', 'task-rev');

      // Explicitly revoke credential in credentials table
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ?", new Date(nowMs).toISOString(), job.id);

      // Seed target job for action
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('target-rev', 'custom', 'krev', '{}', 'running', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );

      // 1. verifyRootEpoch fails
      const verifyRev = await service.verifyRootEpoch(job.id, claim.job_token!, claim.lease_epoch!);
      expect(verifyRev.valid).toBe(false);
      expect(verifyRev.status).toBe('unauthorized');
      expect(verifyRev.reason).toBe('revoked_token');

      // 2. commitRootAction fails
      const actionRev = await service.commitRootAction(
        job.id, "host-rev", claim.lease_epoch!,
        { type: 'job_transition', payload: { target_job_id: 'target-rev', new_status: 'succeeded' } },
        'sess-rev',
        claim.job_token!
      );
      expect(actionRev.success).toBe(false);
      expect(actionRev.status).toBe('unauthorized');
      expect(actionRev.reason).toBe('revoked_token');

      // Target job not transitioned (zero-effect)
      const targetJob = (sql.exec("SELECT * FROM jobs WHERE id = 'target-rev'").toArray() as any[])[0];
      expect(targetJob.status).toBe('running');

      // 3. renewJob fails
      const renewRev = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
      expect(renewRev.error).toBe('unauthorized_or_stale');

      // 4. finishJob fails
      const finishRev = await service.finishJob(job.id, claim.job_token!, claim.lease_epoch!, 'succeeded', 'revoked', []);
      expect(finishRev.error).toBe('unauthorized_or_stale');

      // 5. createJob as root fails
      const createRev = await service.createJob("custom", {}, "role:root", 25, claim.job_token!, claim.lease_epoch!);
      expect(createRev.error).toBe('unauthorized_or_stale');
    });
  });

  it('negative zero-effect: expiry and past hardstop reject mutations', async () => {
    const id = (env as any).HUB.idFromName("test-expiry-hardstop");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-12T14:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(50),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-exp", "he", "he",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-exp", true);
      expect(claim.error).toBeUndefined();
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-exp', 'task-exp');

      // Advance clock past 20m lease expiration (but before 25m hardstop)
      nowMs += 21 * 60000;

      // verifyRootEpoch: lease_expired
      const verifyExpired = await service.verifyRootEpoch(job.id, claim.job_token!, claim.lease_epoch!);
      expect(verifyExpired.valid).toBe(false);
      expect(verifyExpired.status).toBe('expired');
      expect(verifyExpired.reason).toBe('lease_expired');

      // commitRootAction: lease_expired
      const actionExpired = await service.commitRootAction(
        job.id, "host-exp", claim.lease_epoch!,
        { type: 'job_transition', payload: { target_job_id: 't', new_status: 'succeeded' } },
        'sess-exp'
      );
      expect(actionExpired.success).toBe(false);
      expect(actionExpired.status).toBe('expired');
      expect(actionExpired.reason).toBe('lease_expired');

      // Advance clock past 25m hardstop
      nowMs = new Date(job.deadline_at).getTime() + 1000;

      // renewJob: deadline_exceeded
      const renewPastDeadline = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
      expect(renewPastDeadline.error).toBe('deadline_exceeded');

      // Candidate expired check in takeoverRoot: candidate job with status 'offered' and expired deadline
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('job-exp-cand', 'root-run', 'exp:cand', '{}', 'offered', ?, ?, 'system')",
        new Date(nowMs - 30 * 60000).toISOString(),
        new Date(nowMs - 5 * 60000).toISOString()
      );
      const candidateTakeover = await service.takeoverRoot('job-exp-cand', "host-exp", claim.lease_epoch!);
      expect(candidateTakeover.error).toBe('candidate_expired');
    });
  });

  it('replay and idempotency: commitRootAction duplicate payload returns duplicate, mismatch returns conflict', async () => {
    const id = (env as any).HUB.idFromName("test-replay-idempotency");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T15:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(60),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-replay", "hrp", "hrp",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-replay", true);
      expect(claim.error).toBeUndefined();
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-replay', 'task-replay');

      // Seed target job
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('target-rep', 'custom', 'krep', '{}', 'running', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );

      // First execution: commits transactionally
      const action1 = await service.commitRootAction(
        job.id, "host-replay", claim.lease_epoch!,
        { type: 'job_transition', idempotency_key: 'act-1', payload: { target_job_id: 'target-rep', new_status: 'succeeded' } },
        'sess-replay',
        claim.job_token!
      );
      expect(action1.success).toBe(true);
      expect(action1.status).toBe('committed');
      expect(action1.duplicate).toBeUndefined();

      // Check target job transitioned
      const targetJob1 = (sql.exec("SELECT * FROM jobs WHERE id = 'target-rep'").toArray() as any[])[0];
      expect(targetJob1.status).toBe('succeeded');

      // Second execution with same payload: returns duplicate
      const action2 = await service.commitRootAction(
        job.id, "host-replay", claim.lease_epoch!,
        { type: 'job_transition', idempotency_key: 'act-1', payload: { target_job_id: 'target-rep', new_status: 'succeeded' } },
        'sess-replay',
        claim.job_token!
      );
      expect(action2.success).toBe(true);
      expect(action2.duplicate).toBe(true);

      // Third execution with different payload: conflict
      const actionConflict = await service.commitRootAction(
        job.id, "host-replay", claim.lease_epoch!,
        { type: 'job_transition', idempotency_key: 'act-1', payload: { target_job_id: 'target-rep', new_status: 'failed' } },
        'sess-replay',
        claim.job_token!
      );
      expect(actionConflict.success).toBe(false);
      expect(actionConflict.status).toBe('conflict');
      expect(actionConflict.reason).toBe('payload_digest_conflict');

      // Exactly 1 row in root_actions table
      const rows = sql.exec("SELECT * FROM root_actions WHERE idempotency_key = 'act-1'").toArray();
      expect(rows.length).toBe(1);

      // Handoff notes idempotency
      const note1 = service.postHandoffNotes(job.id, "host-replay", claim.lease_epoch!, "initial notes", ["ref1"], "sess-replay", claim.job_token!);
      expect(note1.success).toBe(true);
      expect(note1.duplicate).toBe(false);

      const note2 = service.postHandoffNotes(job.id, "host-replay", claim.lease_epoch!, "updated notes", ["ref1", "ref2"], "sess-replay", claim.job_token!);
      expect(note2.success).toBe(true);
      expect(note2.duplicate).toBe(true);

      const notes = service.getHandoffNotes(10);
      expect(notes.length).toBe(1);
      expect(notes[0].summary).toBe("updated notes");
    });
  });

  it('positive transition: claim -> start -> renew -> action -> finish lifecycle with credentials and audit', async () => {
    const id = (env as any).HUB.idFromName("test-positive-lifecycle");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-12T16:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(70),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-lifecycle", "hlc", "hlc",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];

      // 1. Claim
      const claim = await service.claimJob(job.id, "host-lifecycle", true);
      expect(claim.error).toBeUndefined();
      expect(claim.lease_epoch).toBe(1);

      // Scoped credential created with role:root and lease:root
      const credClaimed = (sql.exec("SELECT * FROM credentials WHERE subject = ?", job.id).toArray() as any[])[0];
      expect(credClaimed).toBeDefined();
      expect(credClaimed.actor).toBe('role:root');
      expect(credClaimed.revoked_at).toBeNull();
      expect(JSON.parse(credClaimed.scopes)).toContain('lease:root');

      // 2. Start
      const start = await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'native-sess-001', 'task-001');
      expect(start.error).toBeUndefined();

      const runningJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(runningJob.status).toBe('running');
      expect(runningJob.native_session).toBe('native-sess-001');

      // 3. Renew
      nowMs += 5 * 60000;
      const renew = await service.renewJob(job.id, claim.job_token!, claim.lease_epoch!);
      expect(renew.error).toBeUndefined();

      // 4. Verify epoch valid
      const verify = await service.verifyRootEpoch(job.id, claim.job_token!, claim.lease_epoch!);
      expect(verify.valid).toBe(true);
      expect(verify.status).toBe('ok');

      // 5. Finish
      nowMs += 5 * 60000;
      const finish = await service.finishJob(job.id, claim.job_token!, claim.lease_epoch!, 'succeeded', 'done successfully', ['art1']);
      expect(finish.error).toBeUndefined();

      // Post-finish assertions
      const finishedJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", job.id).toArray() as any[])[0];
      expect(finishedJob.status).toBe('succeeded');
      expect(finishedJob.outcome_summary).toBe('done successfully');

      // Root lease is cleared
      const leaseAfter = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(leaseAfter.holder_job_id).toBeNull();
      expect(leaseAfter.release_reason).toBe('succeeded');

      // Job credential is automatically revoked
      const credAfter = (sql.exec("SELECT * FROM credentials WHERE subject = ?", job.id).toArray() as any[])[0];
      expect(credAfter.revoked_at).not.toBeNull();
    });
  });

  it('root incumbent protection vs confirmed stale takeover and transport unknown', async () => {
    const id = (env as any).HUB.idFromName("test-incumbent-protection");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-12T17:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(80),
      });

      // Register host-live first
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-live", "hl", "hl",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const incumbentJob = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(incumbentJob.id, "host-live", true);
      expect(claim.error).toBeUndefined();
      await service.startJob(incumbentJob.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-live', 'task-live');

      // Register host-contender
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-contender", "hc", "hc",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Candidate job for takeover
      const candJobId = "job-cand-takeover";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', 'cand:to', '{}', 'offered', ?, ?, 'system', 'host-contender', 'host-contender', ?)",
        candJobId, new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), new Date(nowMs + 120000).toISOString()
      );

      // Case 1: Incumbent is live, fresh, healthy -> takeover DENIED
      const statusLive = service.getRootStatus();
      expect(statusLive.is_healthy_alive).toBe(true);
      expect(statusLive.confirmed_stale).toBe(false);

      const denyTakeover = await service.takeoverRoot(candJobId, "host-contender", claim.lease_epoch!);
      expect(denyTakeover.error).toBe('incumbent_still_healthy');

      // Case 2: Transport unknown -> host has no last_checkin_at -> fail closed, takeover DENIED
      sql.exec("UPDATE machines SET last_checkin_at = NULL WHERE id = 'host-live'");
      const statusUnknown = service.getRootStatus();
      expect(statusUnknown.transport_unknown).toBe(true);
      expect(statusUnknown.confirmed_stale).toBe(false);

      const denyTakeoverUnknown = await service.takeoverRoot(candJobId, "host-contender", claim.lease_epoch!);
      expect(denyTakeoverUnknown.error).toBe('transport_unknown');

      // Case 3: Confirmed stale -> checkin > 180s ago, lease expired -> takeover SUCCEEDS
      nowMs += 25 * 60000;
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-live'", new Date(nowMs - 200000).toISOString());
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-contender'", new Date(nowMs).toISOString());

      // Refresh candidate job deadline
      sql.exec("UPDATE jobs SET deadline_at = ?, offer_expires_at = ? WHERE id = ?",
        new Date(nowMs + 20 * 60000).toISOString(), new Date(nowMs + 120000).toISOString(), candJobId);

      const statusStale = service.getRootStatus();
      expect(statusStale.confirmed_stale).toBe(true);

      const succTakeover = await service.takeoverRoot(candJobId, "host-contender", claim.lease_epoch!);
      expect(succTakeover.success).toBe(true);
      expect(succTakeover.lease_epoch).toBe(claim.lease_epoch! + 1);

      // Old incumbent is fenced and its credential revoked
      const fencedJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", incumbentJob.id).toArray() as any[])[0];
      expect(fencedJob.status).toBe('fenced');

      const fencedCred = (sql.exec("SELECT * FROM credentials WHERE subject = ?", incumbentJob.id).toArray() as any[])[0];
      expect(fencedCred.revoked_at).not.toBeNull();
    });
  });

  it('fail-closed candidate validation in takeoverRoot', async () => {
    const id = (env as any).HUB.idFromName("test-takeover-fail-closed");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T18:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(90),
      });

      // 1. Missing candidate job -> not_found
      const toNotFound = await service.takeoverRoot("non-existent-job", "host-any", 0);
      expect(toNotFound.error).toBe('not_found');

      // 2. Candidate job is not kind root-run -> invalid_candidate_kind
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('job-writer', 'writer-run', 'w1', '{}', 'offered', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );
      const toWrongKind = await service.takeoverRoot("job-writer", "host-any", 0);
      expect(toWrongKind.error).toBe('invalid_candidate_kind');

      // 3. Candidate job has terminal status -> invalid_candidate_status
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('job-failed', 'root-run', 'rf1', '{}', 'failed', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );
      const toFailedStatus = await service.takeoverRoot("job-failed", "host-any", 0);
      expect(toFailedStatus.error).toBe('invalid_candidate_status');

      // 4. Candidate host missing from machines table -> host_not_found (fail-closed)
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('job-valid-to', 'root-run', 'rv1', '{}', 'pending', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );
      const toMissingHost = await service.takeoverRoot("job-valid-to", "unregistered-host", 0);
      expect(toMissingHost.error).toBe('host_not_found');

      // 5. Candidate host is drained -> host_drained
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained, capabilities) VALUES ('host-dr', 'dr', 'dr', ?, ?, 1, ?)",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(), JSON.stringify({ jobs: ["root-run"] })
      );
      const toDrainedHost = await service.takeoverRoot("job-valid-to", "host-dr", 0);
      expect(toDrainedHost.error).toBe('host_drained');

      // 6. Candidate host lacks root-run capability -> host_incapable (fail-closed)
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained, capabilities) VALUES ('host-nocaps', 'nc', 'nc', ?, ?, 0, ?)",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(), JSON.stringify({ jobs: ["writer-run"] })
      );
      const toIncapableHost = await service.takeoverRoot("job-valid-to", "host-nocaps", 0);
      expect(toIncapableHost.error).toBe('host_incapable');
    });
  });

  it('commitRootAction bus_dispatch creates durable message in SQLite and enforces payload conflict receipt', async () => {
    const id = (env as any).HUB.idFromName("test-bus-dispatch-action");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T19:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(95),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-bus-act", "hba", "hba",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-bus-act", true);
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-bus', 'task-bus');

      // Register active principal agent for recipient validation
      sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, engine, session_id, workspace, state, health_state, last_seen_at) VALUES ('principal', 'hub', 'principal', 'host-bus-act', 'codex', 'sess-p', '/p', 'active', 'healthy', ?)",
        new Date(nowMs).toISOString()
      );

      // 1. Commit bus_dispatch action
      const actionRes = await service.commitRootAction(
        job.id, "host-bus-act", claim.lease_epoch!,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'note',
            body: 'Status report from root run epoch 1'
          },
          idempotency_key: 'bus-action-1'
        },
        'sess-bus',
        claim.job_token!
      );

      expect(actionRes.success).toBe(true);
      expect(actionRes.status).toBe('committed');
      expect(actionRes.result.message_id).toBeDefined();

      // Verify message is actually stored in SQLite messages table
      const msg = (sql.exec("SELECT * FROM messages WHERE id = ?", actionRes.result.message_id).toArray() as any[])[0];
      expect(msg).toBeDefined();
      expect(msg.from_actor).toBe('role:root');
      expect(msg.to_actor).toBe('role:principal');
      expect(msg.body).toBe('Status report from root run epoch 1');
      expect(msg.lease_epoch).toBe(claim.lease_epoch);

      // Verify root_actions audit record
      const actionRecord = (sql.exec("SELECT * FROM root_actions WHERE idempotency_key = 'bus-action-1'").toArray() as any[])[0];
      expect(actionRecord).toBeDefined();
      expect(actionRecord.epoch).toBe(claim.lease_epoch);

      // 2. Replay with identical payload -> duplicate receipt (success=true, duplicate=true)
      const duplicateRes = await service.commitRootAction(
        job.id, "host-bus-act", claim.lease_epoch!,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'note',
            body: 'Status report from root run epoch 1'
          },
          idempotency_key: 'bus-action-1'
        },
        'sess-bus',
        claim.job_token!
      );
      expect(duplicateRes.success).toBe(true);
      expect(duplicateRes.duplicate).toBe(true);
      expect(duplicateRes.result.message_id).toBe(actionRes.result.message_id);

      // 3. Replay with DIFFERENT payload -> conflict receipt
      const conflictRes = await service.commitRootAction(
        job.id, "host-bus-act", claim.lease_epoch!,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'note',
            body: 'ALTERED payload text'
          },
          idempotency_key: 'bus-action-1'
        },
        'sess-bus',
        claim.job_token!
      );
      expect(conflictRes.success).toBe(false);
      expect(conflictRes.status).toBe('conflict');
      expect(conflictRes.reason).toBe('payload_digest_conflict');

      // 4. Revocation check: revoked job credential prevents any root action
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ?", new Date(nowMs).toISOString(), job.id);
      const revokedRes = await service.commitRootAction(
        job.id, "host-bus-act", claim.lease_epoch!,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'note',
            body: 'Message after revocation'
          },
          idempotency_key: 'bus-action-revoked'
        },
        'sess-bus',
        claim.job_token!
      );
      expect(revokedRes.success).toBe(false);
      expect(revokedRes.status).toBe('unauthorized');
      expect(revokedRes.reason).toBe('revoked_token');
    });
  });

  it('regression F1/F4: commitRootAction rejects raw stored hash bypass, dispatches via BusService with from_actor role:root, and qualifies NULL health_state', async () => {
    const id = (env as any).HUB.idFromName("test-reg-f1-f4");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-14T02:10:00Z').getTime();

      const bus = new BusService(sql);
      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(101),
        busService: bus,
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-f14', 'hf14', 'hf14', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-f14", true);
      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-1', 'task-1');

      // 1. Raw stored hash bypass rejected (including cancel-writer-1)
      const storedHash = (sql.exec("SELECT claim_token_hash FROM jobs WHERE id = ?", job.id).toArray() as any[])[0].claim_token_hash;
      const rawHashAction = await service.commitRootAction(
        job.id, "host-f14", claim.lease_epoch!,
        { type: 'bus_dispatch', idempotency_key: 'raw-hash-1', payload: { to_actor: 'role:principal', kind: 'note', body: 'bypass' } },
        'sess-1',
        storedHash
      );
      expect(rawHashAction.success).toBe(false);
      expect(rawHashAction.status).toBe('unauthorized');

      const rawHashCancelWriter = await service.commitRootAction(
        job.id, "host-f14", claim.lease_epoch!,
        { type: 'job_transition', idempotency_key: 'cancel-writer-1', payload: { target_job_id: 'any', new_status: 'cancelled' } },
        'sess-1',
        storedHash
      );
      expect(rawHashCancelWriter.success).toBe(false);
      expect(rawHashCancelWriter.status).toBe('unauthorized');

      // 2. Dispatch to non-existent role:principal fails
      const dispatchNoPrincipal = await service.commitRootAction(
        job.id, "host-f14", claim.lease_epoch!,
        { type: 'bus_dispatch', idempotency_key: 'no-princ', payload: { to_actor: 'role:principal', kind: 'note', body: 'nobody home' } },
        'sess-1',
        claim.job_token!
      );
      expect(dispatchNoPrincipal.success).toBe(false);

      // 3. Registered agent with state='working' and health_state=NULL is valid and visible in BusService.getBusMessages()
      sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, engine, session_id, workspace, state, health_state, last_seen_at) VALUES ('worker-null-hs', 'demo', 'worker', 'host-f14', 'codex', 'sw', '/w', 'working', NULL, ?)",
        new Date(nowMs).toISOString()
      );
      const dispatchNullHs = await service.commitRootAction(
        job.id, "host-f14", claim.lease_epoch!,
        { type: 'bus_dispatch', idempotency_key: 'null-hs', payload: { to_actor: 'worker-null-hs', kind: 'note', body: 'hello worker' } },
        'sess-1',
        claim.job_token!
      );
      expect(dispatchNullHs.success).toBe(true);
      const visible = bus.getBusMessages({ kind: 'agent', actor: 'worker-null-hs', scopes: ['bus:read'] }, 'worker-null-hs');
      expect(visible.length).toBe(1);
    });
  });

  it('regression F1-F4 contract: postHandoffNotes requires holder credential, tickRootSlot skip note uses unique skipJobId, and stale broken incumbent vs unknown incumbent', async () => {
    const id = (env as any).HUB.idFromName("test-reg-f1-f2-f3-f4");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let nowMs = new Date('2026-10-14T03:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(202),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-1', 'h1', 'h1', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), JSON.stringify({ jobs: ["root-run"] })
      );

      // Slot 1: claim and start
      const slot1Iso = new Date(nowMs).toISOString();
      service.tickRootSlot(slot1Iso);
      const job1 = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${slot1Iso}`).toArray() as any[])[0];
      const claim = await service.claimJob(job1.id, "host-1", true);
      await service.startJob(job1.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 'sess-1', 'task-1');

      // Public getRootStatus fields alone (without holder token) cannot forge handoff notes
      const publicStatus = service.getRootStatus();
      const forgeAttempt = service.postHandoffNotes(
        publicStatus.lease.holder_job_id,
        publicStatus.lease.holder_host_id,
        publicStatus.lease.epoch,
        'FORGED BY PUBLIC STATUS READER WITHOUT CREDENTIAL',
        ['evil.json'],
        publicStatus.live_session?.session_id
      );
      expect(forgeAttempt.success).toBe(false);

      // Advance 10m: next slot is skipped while Root is healthy
      nowMs += 10 * 60000;
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-1'", new Date(nowMs - 20000).toISOString());
      const slot2Iso = new Date(nowMs).toISOString();
      service.tickRootSlot(slot2Iso);
      const job2 = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${slot2Iso}`).toArray() as any[])[0];
      expect(job2.status).toBe('skip');

      // Legitimate holder posts final handoff note with valid holder token -> does NOT collide with skip note!
      const legitNote = service.postHandoffNotes(
        job1.id,
        'host-1',
        claim.lease_epoch!,
        'LEGITIMATE FINAL HANDOFF SUMMARY',
        ['evidence/final.json'],
        'sess-1',
        claim.job_token!
      );
      expect(legitNote.success).toBe(true);
      expect(legitNote.duplicate).toBe(false);

      const allNotes = service.getHandoffNotes(10);
      const skipNote = allNotes.find((n: any) => n.summary.includes('skipped slot'));
      const holderNote = allNotes.find((n: any) => n.summary === 'LEGITIMATE FINAL HANDOFF SUMMARY');
      expect(skipNote).toBeDefined();
      expect(holderNote).toBeDefined();
      expect(skipNote.job_id).toBe(job2.id);
      expect(holderNote.job_id).toBe(job1.id);
    });
  });
});
