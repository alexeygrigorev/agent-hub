import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
let cookie: string;

describe('no casual direct engine picker + waiting-idle + headless auto-cleanup', () => {
  beforeEach(async () => {
    cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  });

  it('Session control start accepts engine_override_reason and validates length', async () => {
    // 0. Engine without reason is rejected with 400 engine_reason_required
    const noReasonRes = await SELF.fetch('https://hub.test/api/sessions', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'override-no-reason-1',
        workspace: 'demo',
        engine: 'codex',
      }),
    });
    expect(noReasonRes.status).toBe(400);
    const noReasonData: any = await noReasonRes.json();
    expect(noReasonData.error).toBe('engine_reason_required');

    // 1. Oversized reason is rejected with 400 invalid_session_request
    const badRes = await SELF.fetch('https://hub.test/api/sessions', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'override-bad-1',
        workspace: 'demo',
        engine: 'codex',
        engine_override_reason: 'x'.repeat(501),
      }),
    });
    expect(badRes.status).toBe(400);
    const badData: any = await badRes.json();
    expect(badData.error).toBe('invalid_session_request');

    // 2. Valid engine_override_reason passes the reason gate (not 400 engine_reason_required)
    const validRes = await SELF.fetch('https://hub.test/api/sessions', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'override-valid-1',
        workspace: 'demo',
        engine: 'codex',
        engine_override_reason: 'Legitimate model override explanation',
      }),
    });
    // It passes the reason gate (error is not engine_reason_required; headless
    // without intake then fails with intake_task_id_required, not a schema error)
    const validData: any = await validRes.json();
    expect(validData.error).not.toBe('engine_reason_required');
    expect(validData.error).not.toBe('invalid_session_request');
  });

  it('Status API digest surfaces running, idle, and waiting agent counts with probe exclusion', async () => {
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('m-test','m','m','2026-01-01T00:00:00.000Z',?,0)", now);

      // Clean existing fleet_sessions and session_control_sessions
      h.sql.exec('DELETE FROM fleet_sessions');
      h.sql.exec('DELETE FROM session_control_sessions');
      try { h.sql.exec('DELETE FROM session_lifecycle_observations'); } catch {}

      // 1. Regular running agent
      h.sql.exec(
        "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace) VALUES ('m-test', 'nat-1', 'task-1', 'codex', 'running', 'running', ?, '/ws/1')",
        now
      );

      // 2. Idle agent (resting)
      h.sql.exec(
        "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace) VALUES ('m-test', 'nat-2', 'task-2', 'codex', 'running', 'idle', ?, '/ws/2')",
        now
      );

      // 3. Waiting agent (blocked on interactive question / confirmation)
      h.sql.exec(
        "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace) VALUES ('m-test', 'nat-3', 'task-3', 'codex', 'running', 'waiting', ?, '/ws/3')",
        now
      );
      try {
        h.sql.exec(
          "INSERT INTO session_lifecycle_observations (session_key, machine_id, native_id, observed_at, payload_json) VALUES ('m-test/nat-3', 'm-test', 'nat-3', ?, ?)",
          now,
          JSON.stringify({ waiting_confirmation: true })
        );
      } catch {}

      // 4. Ping/probe session: MUST NOT be counted as an agent
      h.sql.exec(
        "INSERT INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace) VALUES ('m-test', 'nat-probe', 'ping-test', 'probe', 'running', 'running', ?, '/ws/probe')",
        now
      );
    });

    const res = await SELF.fetch('https://hub.test/api/status', {
      method: 'POST',
      headers: { Cookie: cookie, 'x-csrf-token': 'csrf', 'Content-Type': 'application/json' },
      body: '{}',
    });
    expect(res.status).toBe(200);
    const data: any = await res.json();

    expect(data.agents).toBeDefined();
    expect(data.agents.running).toBe(1); // Only nat-1
    expect(data.agents.waiting).toBe(1); // nat-3
    expect(data.agents.waiting_confirmation).toBe(1); // nat-3
    expect(data.agents.idle).toBe(2); // nat-2 (resting) + nat-3 (waiting-idle)
    // Probe was NOT counted
    expect(data.summary.agents).toEqual(data.agents);
  });

  it('Headless finish auto-stops session_control_sessions on task result terminal finish', async () => {
    let launchId = '';
    let jobId = '';
    await inDO(stub(), (h: any) => {
      const now = new Date().toISOString();
      launchId = 'launch-auto-stop-' + Math.random().toString(36).slice(2, 8);
      jobId = crypto.randomUUID();
      const machineId = 'm-test';

      h.sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,drained) VALUES ('m-test','m','m','2026-01-01T00:00:00.000Z',?,0)", now);

      // Create fleet launch
      h.sql.exec(
        "INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'user', 'agent', 'test', ?, '{}', ?, 'codex', 'acc1', 'headless', 'demo', 'goal', 'running', ?, ?)",
        launchId, 'rk-' + launchId, machineId, now, now
      );

      // Create routing job
      h.sql.exec(
        "INSERT INTO routing_jobs (id, actor, request_key, request_json, launch_id, state, report, artifacts_json, created_at, updated_at) VALUES (?, 'user', 'rk-job', ?, ?, 'running', '', '[]', ?, ?)",
        jobId, JSON.stringify({ tasks: [{ prompt: 'goal', approved_action: 'action' }], access: { mode: 'edit' }, workspace: 'demo' }), launchId, now, now
      );

      // Create linked session_control_sessions in running state
      h.sql.exec(
        "INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, session_type, created_at, updated_at) VALUES ('sc-test-1', 'user', 'rk-1', '{}', ?, ?, 'codex', 'headless', 'running', 'headless', ?, ?)",
        launchId, machineId, now, now
      );
    });

    // Post terminal task result from host
    const res = await inDO(stub(), async (h: any) => {
      const hostAuth = { caller: { kind: 'host', machineId: 'm-test', actor: 'm-test' }, scopes: ['host'] };
      const req = new Request(`https://hub.test/api/fleet/tasks/${jobId}/result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status: 'done',
          report: 'Finished task successfully',
          artifacts: [],
          confirmed_exit: true,
        }),
      });
      return h.fleetService.routing.handle(req, hostAuth);
    });
    expect(res.status).toBe(200);

    // Verify session_control_sessions was auto-stopped
    await inDO(stub(), (h: any) => {
      const sess = h.sql.exec("SELECT state FROM session_control_sessions WHERE id = 'sc-test-1'").toArray()[0];
      expect(sess.state).toBe('stopped');
    });
  });
});
