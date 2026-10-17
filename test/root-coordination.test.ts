import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { JobsService } from '../src/jobs';
import { sha256 } from '../src/auth/crypto';

function createRng(initialSeed = 135792468) {
  let seed = initialSeed;
  return (arr: Uint32Array) => {
    for (let i = 0; i < arr.length; i++) {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      arr[i] = seed;
    }
    return arr;
  };
}

describe('Root Coordination Acceptance Criteria (M4 / SPEC §8.1 / Issue 6 - Six Scenarios)', () => {
  it('1. Root startup fetches Hub current root lease and live session; read-only legacy Win35 representation is retired', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-1");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T10:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(101),
      });

      // Register host
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "host-1", "h1", "h1",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), 0,
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Seed active lease and running root-run job
      sql.exec(
        "INSERT OR REPLACE INTO leases (name, epoch, holder_job_id, holder_host_id, holder_machine_id, holder_actor, acquired_at, expires_at) VALUES ('root', 1, 'job-active-1', 'host-1', 'host-1', 'role:root', ?, ?)",
        new Date(nowMs).toISOString(),
        new Date(nowMs + 20 * 60000).toISOString()
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, started_at, native_session, launcher_task_id, evidence_digest, lease_epoch) VALUES ('job-active-1', 'root-run', 'root:slot1', '{}', 'running', ?, ?, 'role:root', 'host-1', 'host-1', ?, 'sess-1', 'task-1', 'digest-1', 1)",
        new Date(nowMs).toISOString(),
        new Date(nowMs + 25 * 60000).toISOString(),
        new Date(nowMs).toISOString()
      );

      // Seed retired legacy Win35 agent record in agents table
      sql.exec(
        "INSERT OR REPLACE INTO agents (tag, project, role, machine_id, engine, session_id, workspace, state, health_state) VALUES ('root-win35', 'legacy', 'root', 'win-vm', 'codex', 'sess-win', '/workspace', 'retired', 'retired')"
      );

      // Fetch root status via service
      const status = service.getRootStatus();

      expect(status.lease).toBeDefined();
      expect(status.lease.name).toBe('root');
      expect(status.lease.epoch).toBe(1);
      expect(status.lease.holder_job_id).toBe('job-active-1');
      expect(status.lease.holder_host_id).toBe('host-1');

      expect(status.live_session).toBeDefined();
      expect(status.live_session.job_id).toBe('job-active-1');
      expect(status.live_session.session_id).toBe('sess-1');
      expect(status.live_session.launcher_task_id).toBe('task-1');
      expect(status.live_session.evidence_digest).toBe('digest-1');

      // Retired Win35 representation is present, marked retired, read-only
      expect(status.incumbent_legacy).toBeDefined();
      expect(status.incumbent_legacy.status).toBe('retired');
      expect(status.incumbent_legacy.read_only).toBe(true);

      // Fresh status flags
      expect(status.is_healthy_alive).toBe(true);
      expect(status.confirmed_stale).toBe(false);
      expect(status.transport_unknown).toBe(false);
    });
  });

  it('2. Healthy alive incumbent (heartbeat fresh <= 180s within hardstop) prevents contender takeover', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-2");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T11:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(102),
      });

      // Register incumbent host and contender host
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-incumbent', 'hi', 'hi', ?, ?, ?, 0, ?)",
        new Date(nowMs - 30000).toISOString(),
        new Date(nowMs - 30000).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-contender', 'hc', 'hc', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(),
        new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Incumbent holding lease, heartbeat 30s ago (< 180s), within 25m hardstop
      sql.exec(
        "INSERT OR REPLACE INTO leases (name, epoch, holder_job_id, holder_host_id, holder_machine_id, holder_actor, acquired_at, expires_at) VALUES ('root', 1, 'job-incumbent', 'host-incumbent', 'host-incumbent', 'role:root', ?, ?)",
        new Date(nowMs - 30000).toISOString(),
        new Date(nowMs + 15 * 60000).toISOString()
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, holder_machine_id, started_at, native_session, lease_epoch) VALUES ('job-incumbent', 'root-run', 'root:inc', '{}', 'running', ?, ?, 'role:root', 'host-incumbent', 'host-incumbent', ?, 'sess-inc', 1)",
        new Date(nowMs - 30000).toISOString(),
        new Date(nowMs + 20 * 60000).toISOString(),
        new Date(nowMs - 30000).toISOString()
      );

      // Candidate job for contender
      const candJobId = "job-contender-1";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', 'root:c1', '{}', 'offered', ?, ?, 'role:root', 'host-contender', 'host-contender', ?)",
        candJobId,
        new Date(nowMs).toISOString(),
        new Date(nowMs + 25 * 60000).toISOString(),
        new Date(nowMs + 120000).toISOString()
      );

      // Verify status: incumbent is healthy alive
      const status = service.getRootStatus();
      expect(status.is_healthy_alive).toBe(true);
      expect(status.confirmed_stale).toBe(false);

      // Contender attempts takeover -> denied: incumbent_still_healthy
      const takeoverRes = await service.takeoverRoot(candJobId, "host-contender", 1);
      expect(takeoverRes.error).toBe('incumbent_still_healthy');
      expect(takeoverRes.reason).toContain('incumbent is healthy alive');

      // Zero side-effects: lease is still held by incumbent at epoch 1
      const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(lease.epoch).toBe(1);
      expect(lease.holder_job_id).toBe('job-incumbent');
      expect(lease.holder_host_id).toBe('host-incumbent');

      // Candidate job remains in offered state
      const candJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", candJobId).toArray() as any[])[0];
      expect(candJob.status).toBe('offered');
    });
  });

  it('3. Stale heartbeat (> 180s) or past hardstop allows atomic CAS takeover; concurrent contenders: exactly one acts', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-3");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T12:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(103),
      });

      // Incumbent with heartbeat 300s ago (> 180s stale) and expired lease
      const pastHb = new Date(nowMs - 300000).toISOString();
      const pastExp = new Date(nowMs - 10000).toISOString();
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-stale-inc', 'hsi', 'hsi', ?, ?, ?, 0, ?)",
        pastHb, pastHb, JSON.stringify({ gate: { launch_allowed: true } }), JSON.stringify({ jobs: ["root-run"] })
      );
      sql.exec(
        "INSERT OR REPLACE INTO leases (name, epoch, holder_job_id, holder_host_id, holder_machine_id, holder_actor, acquired_at, expires_at) VALUES ('root', 1, 'job-stale-inc', 'host-stale-inc', 'host-stale-inc', 'role:root', ?, ?)",
        pastHb, pastExp
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, started_at, native_session, lease_epoch) VALUES ('job-stale-inc', 'root-run', 'root:stale', '{}', 'running', ?, ?, 'role:root', 'host-stale-inc', ?, 'sess-stale', 1)",
        pastHb, new Date(nowMs + 600000).toISOString(), pastHb
      );
      // Credential for incumbent
      sql.exec(
        "INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES ('cred-stale', 'job', 'hash-stale', 'fam-stale', 'job-stale-inc', 'host-stale-inc', 'role:root', '[]', 'system', ?, ?)",
        pastHb, pastExp
      );

      // Register two capable contender hosts
      for (const h of ['host-A', 'host-B']) {
        sql.exec(
          "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          h, h, h,
          new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
          JSON.stringify({ gate: { launch_allowed: true } }), 0,
          JSON.stringify({ jobs: ["root-run"] })
        );
      }

      // Seed candidate jobs for both contenders
      const jobA = "job-winner-A";
      const jobB = "job-loser-B";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', 'cand:A', '{}', 'offered', ?, ?, 'role:root', 'host-A', 'host-A', ?)",
        jobA, new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), new Date(nowMs + 120000).toISOString()
      );
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', 'cand:B', '{}', 'offered', ?, ?, 'role:root', 'host-B', 'host-B', ?)",
        jobB, new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), new Date(nowMs + 120000).toISOString()
      );

      // Verify confirmed stale
      const status = service.getRootStatus();
      expect(status.confirmed_stale).toBe(true);

      // Contender A attempts CAS takeover expecting epoch 1 -> succeeds
      const takeoverA = await service.takeoverRoot(jobA, "host-A", 1, "takeover_stale_heartbeat");
      expect(takeoverA.success).toBe(true);
      expect(takeoverA.lease_epoch).toBe(2);
      expect(takeoverA.job_token).toBeDefined();

      // Concurrent contender B attempts CAS takeover expecting stale epoch 1 -> cas_failed
      const takeoverB = await service.takeoverRoot(jobB, "host-B", 1, "concurrent_takeover_attempt");
      expect(takeoverB.error).toBe('cas_failed');
      expect(takeoverB.current_epoch).toBe(2);

      // Exactly one holder: host-A at epoch 2
      const finalLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(finalLease.epoch).toBe(2);
      expect(finalLease.holder_job_id).toBe(jobA);
      expect(finalLease.holder_host_id).toBe('host-A');

      // Old incumbent job was marked fenced in SQLite
      const fencedJob = (sql.exec("SELECT * FROM jobs WHERE id = 'job-stale-inc'").toArray() as any[])[0];
      expect(fencedJob.status).toBe('fenced');
      expect(fencedJob.outcome_summary).toContain('fenced: superseded by takeover epoch 2');

      // Old incumbent credential was revoked in SQLite
      const revokedCred = (sql.exec("SELECT * FROM credentials WHERE subject = 'job-stale-inc'").toArray() as any[])[0];
      expect(revokedCred.revoked_at).not.toBeNull();

      // Loser job remains in offered state without side-effects
      const loserJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", jobB).toArray() as any[])[0];
      expect(loserJob.status).toBe('offered');
    });
  });

  it('4. EVERY Root authority action carries epoch validated SERVER side; superseded epoch is fenced pre-action', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-4");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T13:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(104),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-root-act', 'hra', 'hra', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(), new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }), JSON.stringify({ jobs: ["root-run"] })
      );

      service.tickRootSlot(new Date(nowMs).toISOString());
      const job = (sql.exec("SELECT * FROM jobs WHERE kind = 'root-run'").toArray() as any[])[0];
      const claim = await service.claimJob(job.id, "host-root-act", true);
      const currentEpoch = claim.lease_epoch!; // e.g. 1
      await service.startJob(job.id, claim.job_token!, currentEpoch, 'sha256:0123456789abcdef', 'sess-root-act', 'task-root-act');

      // Advance lease epoch externally to simulate takeover/supersession
      const newEpoch = currentEpoch + 1;
      sql.exec("UPDATE leases SET epoch = ? WHERE name = 'root'", newEpoch);

      // 1. verifyRootEpoch with stale epoch -> fenced pre-action
      const verifyStale = await service.verifyRootEpoch(job.id, claim.job_token!, currentEpoch);
      expect(verifyStale.valid).toBe(false);
      expect(verifyStale.status).toBe('fenced');
      expect(verifyStale.reason).toBe('superseded');
      expect(verifyStale.current_epoch).toBe(newEpoch);

      // 2. commitRootAction with stale epoch -> fenced with ZERO side-effects
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES ('target-job-1', 'custom', 'tgt:1', '{}', 'running', ?, ?, 'system')",
        new Date(nowMs).toISOString(), new Date(nowMs + 60000).toISOString()
      );
      const actionStale = await service.commitRootAction(
        job.id, "host-root-act", currentEpoch,
        {
          type: 'job_transition',
          payload: { target_job_id: 'target-job-1', new_status: 'succeeded' }
        },
        'sess-root-act'
      );
      expect(actionStale.success).toBe(false);
      expect(actionStale.status).toBe('fenced');
      expect(actionStale.current_epoch).toBe(newEpoch);

      // Verify target job was NOT modified
      const targetUnchanged = (sql.exec("SELECT status FROM jobs WHERE id = 'target-job-1'").toArray() as any[])[0];
      expect(targetUnchanged.status).toBe('running');

      // Restore epoch to match current lease for valid operations
      sql.exec("UPDATE leases SET epoch = ? WHERE name = 'root'", currentEpoch);

      // 3. Valid commitRootAction: job_transition executes real status update
      const actionValid = await service.commitRootAction(
        job.id, "host-root-act", currentEpoch,
        {
          type: 'job_transition',
          payload: { target_job_id: 'target-job-1', new_status: 'succeeded' }
        },
        'sess-root-act',
        claim.job_token!
      );
      expect(actionValid.success).toBe(true);
      expect(actionValid.status).toBe('committed');
      expect(actionValid.result.new_status).toBe('succeeded');

      const targetModified = (sql.exec("SELECT status FROM jobs WHERE id = 'target-job-1'").toArray() as any[])[0];
      expect(targetModified.status).toBe('succeeded');

      // 4. Valid commitRootAction: bus_dispatch executes real message insert into SQLite messages table
      sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, engine, session_id, workspace, state, health_state, last_seen_at) VALUES ('principal', 'hub', 'principal', 'host-root-act', 'codex', 'sess-p', '/p', 'active', 'healthy', ?)",
        new Date(nowMs).toISOString()
      );
      const busDispatch = await service.commitRootAction(
        job.id, "host-root-act", currentEpoch,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'status',
            body: 'Epoch fence verified'
          },
          idempotency_key: 'bus-epoch-action-1'
        },
        'sess-root-act',
        claim.job_token!
      );
      expect(busDispatch.success).toBe(true);
      expect(busDispatch.status).toBe('committed');
      expect(busDispatch.result.message_id).toBeDefined();

      const dispatchedMsg = (sql.exec("SELECT * FROM messages WHERE id = ?", busDispatch.result.message_id).toArray() as any[])[0];
      expect(dispatchedMsg).toBeDefined();
      expect(dispatchedMsg.from_actor).toBe('role:root');
      expect(dispatchedMsg.to_actor).toBe('role:principal');
      expect(dispatchedMsg.lease_epoch).toBe(currentEpoch);

      // 5. Payload conflict receipt on duplicate idempotency key with altered payload
      const conflictAction = await service.commitRootAction(
        job.id, "host-root-act", currentEpoch,
        {
          type: 'bus_dispatch',
          payload: {
            to_actor: 'role:principal',
            kind: 'status',
            body: 'TAMPERED body text'
          },
          idempotency_key: 'bus-epoch-action-1'
        },
        'sess-root-act',
        claim.job_token!
      );
      expect(conflictAction.success).toBe(false);
      expect(conflictAction.status).toBe('conflict');
      expect(conflictAction.reason).toBe('payload_digest_conflict');
    });
  });

  it('5. Audit events recorded and shown in timeline; inert handoff notes separate from authority', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-5");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T14:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(105),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-1', 'h1', 'h1', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(),
        new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );
      const oldRootToken = 'hubt_old_root_secret_token_scenario_5';
      const oldRootHash = await sha256(oldRootToken);
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, holder_host_id, native_session, lease_epoch, claim_token_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        'job-old-root', 'root-run', 'root-run:old', '{}', 'failed', new Date(nowMs).toISOString(), new Date(nowMs + 25 * 60000).toISOString(), 'system', 'host-1', 'sess-old', 1, oldRootHash
      );

      // Post handoff note with holder credential
      const post1 = service.postHandoffNotes(
        'job-old-root',
        'host-1',
        1,
        'Stopped before action critical_push due to epoch supersede',
        ['/local/evidence/trace.log'],
        'sess-old',
        oldRootToken
      );
      expect(post1.success).toBe(true);
      expect(post1.duplicate).toBe(false);

      // Replay with updated summary updates existing note (idempotent on job_id, epoch)
      const post2 = service.postHandoffNotes(
        'job-old-root',
        'host-1',
        1,
        'Updated note: epoch supersede acknowledged',
        ['/local/evidence/trace.log', '/local/evidence/fenced.json'],
        'sess-old',
        oldRootToken
      );
      expect(post2.success).toBe(true);
      expect(post2.duplicate).toBe(true);

      // Retrieve notes
      const notes = service.getHandoffNotes(5);
      expect(notes.length).toBe(1);
      expect(notes[0].job_id).toBe('job-old-root');
      expect(notes[0].epoch).toBe(1);
      expect(notes[0].summary).toContain('Updated note');
      expect(notes[0].evidence_refs).toEqual(['/local/evidence/trace.log', '/local/evidence/fenced.json']);

      // Handoff notes are inert: posting a handoff note does not mutate or grant any lease
      const rootLease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(rootLease.holder_job_id).toBeNull();

      // Check audit table records
      const auditEntries = sql.exec("SELECT * FROM audit WHERE target = 'job-old-root'").toArray() as any[];
      expect(auditEntries.length).toBeGreaterThan(0);
      const handoffAudit = auditEntries.find((e: any) => e.action === 'handoff');
      expect(handoffAudit).toBeDefined();
    });
  });

  it('6. Server clock only: public endpoint denies untrusted client body.now forgery; late start spans 30-min boundary without dual run', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-6");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      let serverNowMs = new Date('2026-10-12T15:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(serverNowMs),
        getRandomValues: createRng(106),
      });

      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-clock-test', 'hct', 'hct', ?, ?, ?, 0, ?)",
        new Date(serverNowMs).toISOString(),
        new Date(serverNowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );

      // 1. Tick slot and offer job
      const slotIso = new Date(serverNowMs).toISOString();
      service.tickRootSlot(slotIso);
      const job = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${slotIso}`).toArray() as any[])[0];
      expect(job.status).toBe('offered');

      // Client passes fake future _simNow where offer would be expired
      const fakeFutureDate = new Date(serverNowMs + 999999);
      const claim = await service.claimJob(job.id, "host-clock-test", true, fakeFutureDate);
      // Succeeded because caller simNow is inert; server trusted clock is used
      expect(claim.error).toBeUndefined();
      expect(claim.lease_epoch).toBe(1);

      await service.startJob(job.id, claim.job_token!, claim.lease_epoch!, 'sha256:0123456789abcdef', 'sess-clock', 'task-clock');

      // 2. Late start spans 30-minute boundary without dual run
      // Advance clock by 15 minutes (boundary crossed, active root still healthy)
      serverNowMs += 15 * 60000;
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-clock-test'", new Date(serverNowMs).toISOString());

      // Slot tick for next calendar slot
      const nextSlotIso = new Date(serverNowMs).toISOString();
      service.tickRootSlot(nextSlotIso);

      // Active root is healthy -> slot tick skips scheduling new root, avoids dual run
      const skipJob = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${nextSlotIso}`).toArray() as any[])[0];
      expect(skipJob).toBeDefined();
      expect(skipJob.status).toBe('skip');
      expect(skipJob.outcome_summary).toContain('previous root healthy');

      // Only one active root lease holder exists throughout
      const lease = (sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray() as any[])[0];
      expect(lease.holder_job_id).toBe(job.id);
      expect(lease.epoch).toBe(1);

      // 3. Late-slot refusal: a tick arriving >= 25 minutes after slot start is refused
      const ancientSlotIso = new Date(serverNowMs - 30 * 60000).toISOString();
      service.tickRootSlot(ancientSlotIso);
      const lateJob = (sql.exec("SELECT * FROM jobs WHERE idempotency_key = ?", `root-run:${ancientSlotIso}`).toArray() as any[])[0];
      expect(lateJob).toBeDefined();
      expect(lateJob.status).toBe('failed');
      expect(lateJob.outcome_summary).toBe('refused: late slot');
    });
  });

  it('7. Registered Root in unknown state/health_state or on stale host blocks takeover (transport_unknown)', async () => {
    const id = (env as any).HUB.idFromName("test-root-coord-scenario-7");
    const stub = (env as any).HUB.get(id);

    await (runInDurableObject as any)(stub, async (instance: any) => {
      const sql = instance.ctx.storage.sql;
      const nowMs = new Date('2026-10-12T16:00:00Z').getTime();

      const service = new JobsService(sql, instance.ctx, {
        now: () => new Date(nowMs),
        getRandomValues: createRng(107),
      });

      // Machine is registered but stale (>180s without checkin)
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-stale-root', 'hsr', 'hsr', ?, ?, ?, 0, ?)",
        new Date(nowMs - 300000).toISOString(),
        new Date(nowMs - 300000).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );
      sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES ('host-contender-7', 'hc7', 'hc7', ?, ?, ?, 0, ?)",
        new Date(nowMs).toISOString(),
        new Date(nowMs).toISOString(),
        JSON.stringify({ gate: { launch_allowed: true } }),
        JSON.stringify({ jobs: ["root-run"] })
      );

      // Seed registered root agent with running state on stale host
      sql.exec(
        "INSERT INTO agents (tag, project, role, machine_id, engine, session_id, workspace, state, health_state) VALUES ('root-agent-stale', 'control', 'root', 'host-stale-root', 'codex', 'sess-stale-root', '/ws', 'running', 'healthy')"
      );

      // Seed expired lease at epoch 1
      sql.exec(
        "INSERT OR REPLACE INTO leases (name, epoch, holder_job_id, holder_host_id, holder_machine_id, holder_actor, acquired_at, expires_at) VALUES ('root', 1, 'job-stale-inc', 'host-stale-root', 'host-stale-root', 'role:root', ?, ?)",
        new Date(nowMs - 300000).toISOString(),
        new Date(nowMs - 100000).toISOString()
      );

      // Candidate job for contender
      const candJobId = "job-contender-7";
      sql.exec(
        "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, offer_host_id, offer_machine_id, offer_expires_at) VALUES (?, 'root-run', 'root:c7', '{}', 'offered', ?, ?, 'role:root', 'host-contender-7', 'host-contender-7', ?)",
        candJobId,
        new Date(nowMs).toISOString(),
        new Date(nowMs + 25 * 60000).toISOString(),
        new Date(nowMs + 120000).toISOString()
      );

      // Status check: running root agent on stale host -> transport_unknown = true, confirmed_stale = false
      const status1 = service.getRootStatus();
      expect(status1.transport_unknown).toBe(true);
      expect(status1.confirmed_stale).toBe(false);

      // Takeover must be denied with transport_unknown
      const res1 = await service.takeoverRoot(candJobId, "host-contender-7", 1);
      expect(res1.error).toBe('transport_unknown');
      expect(res1.job_token).toBeUndefined();

      // Now test with agent state explicitly 'unknown' on fresh host
      sql.exec("UPDATE machines SET last_checkin_at = ? WHERE id = 'host-stale-root'", new Date(nowMs).toISOString());
      sql.exec("UPDATE agents SET state = 'unknown', health_state = 'unknown' WHERE tag = 'root-agent-stale'");

      const status2 = service.getRootStatus();
      expect(status2.transport_unknown).toBe(true);
      expect(status2.confirmed_stale).toBe(false);

      const res2 = await service.takeoverRoot(candJobId, "host-contender-7", 1);
      expect(res2.error).toBe('transport_unknown');
      expect(res2.job_token).toBeUndefined();

      // Candidate job remains untouched
      const candJob = (sql.exec("SELECT * FROM jobs WHERE id = ?", candJobId).toArray() as any[])[0];
      expect(candJob.status).toBe('offered');
    });
  });
});
