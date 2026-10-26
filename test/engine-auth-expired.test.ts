import {freshCpu} from './cpu-fixture';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { RoutingService } from '../src/routing';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const host = (id: string) => ({ caller: { machineId: id, kind: 'host', actor: 'machine:' + id }, scopes: ['host'] });
const now = Date.now();
const iso = (delta = 0) => new Date(now + delta).toISOString();

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test/api/fleet' + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
}

function claim(launchId: string, account = 'opencode-main', model?: string) {
  const resolvedModel = model || (account === 'opencode-main' ? 'opencode-go/muse-spark-1.3-contributor' : (account.startsWith('zcodex') ? 'glm-5.3-flash' : 'gpt-6.1-sol'));
  return request(`/launches/${launchId}/claim`, {
    gate_allowed: true,
    observed_at: iso(),
    account,
    profile_supported: true,
    model: resolvedModel,
  });
}

async function fixture(fn: (fleet: FleetService, routing: RoutingService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('auth-expired-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
    const routing = new RoutingService(hub.sql, fleet, () => now, (cb) => state.storage.transactionSync(cb));
    fleet.init();
    routing.init();
    const sql = hub.sql;

    // Set up machine hetzner and desktop
    sql.exec(
      'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      'hetzner',
      'hetzner',
      'hetzner',
      iso(),
      iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
      0,
      JSON.stringify({ engines: ['opencode', 'codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 })
    );
    sql.exec(
      'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
      'desktop',
      'desktop',
      'desktop',
      iso(),
      iso(),
      JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
      0,
      JSON.stringify({ engines: ['opencode', 'codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 })
    );

    const quota = { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } };
    sql.exec(
      'INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'hetzner',
      '1.0',
      iso(),
      now,
      iso(),
      now,
      'ok',
      null,
      JSON.stringify({
        go: quota,
        zai: quota,
      })
    );
    sql.exec(
      'INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
      'desktop',
      '1.0',
      iso(),
      now,
      iso(),
      now,
      'ok',
      null,
      JSON.stringify({
        go: quota,
        zai: quota,
      })
    );

    // Register accounts and routes for daily tier (muse on opencode, glm-flash on zcodex)
    await fleet.handle(
      request(
        '/accounts',
        {
          accounts: [
            { label: 'opencode-main', provider: 'go', max_concurrency: 2, reserve_percent: 15, enabled: true },
            { label: 'zcodex-hetzner', provider: 'zai', max_concurrency: 1, reserve_percent: 15, enabled: true },
            { label: 'zcodex-desktop', provider: 'zai', max_concurrency: 1, reserve_percent: 15, enabled: true },
          ],
          routes: [
            { machine_id: 'hetzner', engine: 'opencode', account: 'opencode-main', profile: null },
            { machine_id: 'hetzner', engine: 'codex', account: 'zcodex-hetzner', profile: 'zcodex' },
            { machine_id: 'desktop', engine: 'codex', account: 'zcodex-desktop', profile: 'zcodex' },
          ],
        },
        'PUT'
      ),
      admin
    );

    // Register subscriptions for provenance
    await fleet.handle(
      request(
        '/subscriptions',
        {
          subscriptions: [
            { service: 'go', account: 'opencode-main', plan: 'pro', quota },
            { service: 'zai', account: 'zcodex-hetzner', plan: 'pro', quota },
          ],
        },
        'PUT'
      ),
      host('hetzner')
    );
    await fleet.handle(
      request(
        '/subscriptions',
        {
          subscriptions: [
            { service: 'zai', account: 'zcodex-desktop', plan: 'pro', quota },
          ],
        },
        'PUT'
      ),
      host('desktop')
    );

    await fn(fleet, routing, sql);
  });
}

describe('engine_auth_expired handling and routing recovery', () => {
  it('classifies launch failure with engine_auth_expired, records health, and deduplicates operator notices', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Create a launch on opencode
      const reserved = fleet.reserve(
        { workspace: 'demo', engine: 'opencode', prompt: 'test goal', idempotency_key: 'ik-1' },
        admin
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;

      // Host claims the launch
      const claimRes = await fleet.handle(claim(launchId, 'opencode-main'), host('hetzner'));
      expect(claimRes.status).toBe(200);

      // Post launch result as failed with engine_auth_expired
      const res = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key has expired. Create a new API key in OpenCode Console.',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );
      expect(res.status).toBe(200);

      // Verify fleet_engine_health record
      const healthRows = sql
        .exec("SELECT * FROM fleet_engine_health WHERE engine = 'opencode'")
        .toArray() as any[];
      expect(healthRows.length).toBe(1);
      expect(healthRows[0].status).toBe('unavailable');
      expect(healthRows[0].reason).toBe('engine_auth_expired');
      expect(healthRows[0].diagnostics_json).toContain('API key has expired');

      // Verify GET /api/fleet/engine-health endpoint
      const healthRes = await fleet.handle(request('/engine-health', undefined, 'GET'), admin);
      expect(healthRes.status).toBe(200);
      const healthData: any = await healthRes.json();
      expect(healthData.engines).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            engine: 'opencode',
            status: 'unavailable',
            reason: 'engine_auth_expired',
          }),
        ])
      );

      // Verify operator notice created
      const notices = sql
        .exec("SELECT * FROM operator_notices WHERE engine = 'opencode'")
        .toArray() as any[];
      expect(notices.length).toBe(1);
      expect(notices[0].engine).toBe('opencode');
      expect(notices[0].message).toContain('OpenCode');
      expect(notices[0].message).toContain('opencode auth login opencode --method device');

      // Posting again should replay cleanly and not create duplicate operator notices
      const res2 = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key has expired. Create a new API key in OpenCode Console.',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );
      expect(res2.status).toBe(200);
      const noticesAfter = sql
        .exec("SELECT * FROM operator_notices WHERE engine = 'opencode'")
        .toArray() as any[];
      expect(noticesAfter.length).toBe(1);
    }));

  it('excludes unavailable engine in plan() and routes to next eligible engine in the daily tier', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Both opencode and zcodex are registered.
      // Initially, plan selects an engine for daily tier:
      const initialPlan = fleet.plan({ workspace: 'demo', tier: 'daily', prompt: 'solve task' });
      expect(initialPlan.selected).toBeTruthy();

      // Mark opencode unavailable
      sql.exec(
        "INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('opencode', 'hetzner', 'unavailable', 'engine_auth_expired', ?)",
        iso()
      );

      // Next plan should reject opencode and select codex (zcodex)
      const planned = fleet.plan({ workspace: 'demo', tier: 'daily', prompt: 'solve task' });
      expect(planned.selected).not.toBeNull();
      expect(planned.selected?.engine).toBe('codex');
      expect(planned.selected?.profile).toBe('zcodex');

      // The rejected list should indicate engine_auth_expired for opencode
      expect(planned.rejected).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            engine: 'opencode',
            reason: 'engine_auth_expired',
          }),
        ])
      );
    }));

  it('re-enables engine automatically when a probe succeeds or a launch succeeds', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Mark opencode unavailable
      sql.exec(
        "INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('opencode', 'hetzner', 'unavailable', 'engine_auth_expired', ?)",
        iso()
      );
      expect(fleet.plan({ workspace: 'demo', tier: 'daily', engine: 'opencode', prompt: 'probe test' }).selected).toBeNull();

      // Submit successful probe
      const probeRes = await fleet.handle(
        request('/probe', { engine: 'opencode', success: true }),
        host('hetzner')
      );
      expect(probeRes.status).toBe(200);

      // Verify health is updated to ok
      const health = sql.exec("SELECT * FROM fleet_engine_health WHERE engine = 'opencode'").toArray() as any[];
      expect(health[0].status).toBe('ok');

      // Now opencode can be planned again
      const planned = fleet.plan({ workspace: 'demo', tier: 'daily', engine: 'opencode', prompt: 'probe test' });
      expect(planned.selected?.engine).toBe('opencode');
    }));

  it('auto-retries failed start once on another eligible engine in the same tier', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Reserve launch for daily tier (automatic engine selection -> opencode)
      const first = fleet.reserve(
        { workspace: 'demo', tier: 'daily', prompt: 'auto retry test', idempotency_key: 'ik-retry-1' },
        admin
      );
      expect(first.status).toBe(201);
      const firstId = first.value.id;
      const orig = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', firstId).toArray() as any[];

      // Host claims the launch
      await fleet.handle(claim(firstId, orig[0].account), host(orig[0].machine_id));

      // Fail the launch with engine_auth_expired
      const res = await fleet.handle(
        request(`/launches/${firstId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + firstId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key has expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host(orig[0].machine_id)
      );
      expect(res.status).toBe(200);

      // Check that the original launch has retried_launch_id populated
      const origAfter = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', firstId).toArray() as any[];
      expect(origAfter[0].retried_launch_id).toBeTruthy();
      const retryId = origAfter[0].retried_launch_id;

      // Check the new launch details
      const retried = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', retryId).toArray() as any[];
      expect(retried.length).toBe(1);
      expect(retried[0].machine_id).not.toBe(orig[0].machine_id); // Switched to healthy machine/engine in tier
      expect(retried[0].state).toBe('offered');

      // Claim and fail the retried launch on whichever machine it was offered
      const retriedMachine = retried[0].machine_id;
      const retriedAccount = retried[0].account;
      await fleet.handle(claim(retryId, retriedAccount), host(retriedMachine));
      const res2 = await fleet.handle(
        request(`/launches/${retryId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + retryId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'Secondary auth expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host(retriedMachine)
      );
      expect(res2.status).toBe(200);
      const retryRow = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', retryId).toArray() as any[];
      expect(retryRow[0].retried_launch_id).toBeNull();
    }));

  it('reflects unavailable engine status in GET /api/routing/machines runners', async () =>
    fixture(async (_fleet, routing, sql) => {
      // Register capabilities for hetzner
      const putRes = await routing.handle(
        new Request('https://hub.test/api/routing/capabilities', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            needs: ['daily'],
            max_agents: 5,
            runners: [
              { engine: 'opencode', profile: null, model: 'opencode-go/muse-spark-1.3-contributor', effort: null, account: 'opencode-main' },
              { engine: 'codex', profile: 'zcodex', model: 'glm-5.3-flash', effort: 'high', account: 'zcodex-main' },
            ],
          }),
        }),
        host('hetzner')
      );
      expect(putRes.status).toBe(200);

      // Mark opencode unavailable
      sql.exec(
        "INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('opencode', 'hetzner', 'unavailable', 'engine_auth_expired', ?)",
        iso()
      );

      // GET /api/routing/machines
      const getRes = await routing.handle(
        new Request('https://hub.test/api/routing/machines', { method: 'GET' }),
        admin
      );
      expect(getRes.status).toBe(200);
      const data: any = await getRes.json();
      expect(data.machines.length).toBeGreaterThan(0);
      const hetzner = data.machines.find((m: any) => m.id === 'hetzner');
      expect(hetzner).toBeDefined();

      const opencodeRunner = hetzner.runners.find((r: any) => r.engine === 'opencode');
      expect(opencodeRunner.available).toBe(false);
      expect(opencodeRunner.health).toBe(0);

      const codexRunner = hetzner.runners.find((r: any) => r.engine === 'codex');
      expect(codexRunner.available).toBe(true);
      expect(codexRunner.health).toBe(1);
    }));

  it('enforces machine boundary isolation on host probes and preserves operator notices until all machines are ok', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Both hetzner and desktop report engine_auth_expired on opencode
      sql.exec(
        "INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('opencode', 'hetzner', 'unavailable', 'engine_auth_expired', ?)",
        iso()
      );
      sql.exec(
        "INSERT INTO fleet_engine_health (engine, machine_id, status, reason, updated_at) VALUES ('opencode', 'desktop', 'unavailable', 'engine_auth_expired', ?)",
        iso()
      );
      sql.exec(
        "INSERT INTO operator_notices (notice_key, engine, message, created_at) VALUES ('engine_auth_expired:opencode', 'opencode', 'API key expired', ?)",
        iso()
      );

      // Host hetzner cannot report probe for desktop
      const spoofRes = await fleet.handle(
        request('/probe', { engine: 'opencode', machine_id: 'desktop', success: true }),
        host('hetzner')
      );
      expect(spoofRes.status).toBe(403);

      // Host hetzner reports probe for hetzner
      const validRes = await fleet.handle(
        request('/probe', { engine: 'opencode', machine_id: 'hetzner', success: true }),
        host('hetzner')
      );
      expect(validRes.status).toBe(200);

      // Hetzner is now ok, but desktop is still unavailable
      const healthHetzner = sql.exec("SELECT status FROM fleet_engine_health WHERE engine = 'opencode' AND machine_id = 'hetzner'").toArray() as any[];
      expect(healthHetzner[0].status).toBe('ok');
      const healthDesktop = sql.exec("SELECT status FROM fleet_engine_health WHERE engine = 'opencode' AND machine_id = 'desktop'").toArray() as any[];
      expect(healthDesktop[0].status).toBe('unavailable');

      // Operator notice must NOT be deleted yet because desktop is still unavailable
      const notices1 = sql.exec("SELECT * FROM operator_notices WHERE engine = 'opencode'").toArray();
      expect(notices1.length).toBe(1);

      // Now desktop reports probe success
      const desktopRes = await fleet.handle(
        request('/probe', { engine: 'opencode', machine_id: 'desktop', success: true }),
        host('desktop')
      );
      expect(desktopRes.status).toBe(200);

      // Both are ok, operator notice is now deleted
      const notices2 = sql.exec("SELECT * FROM operator_notices WHERE engine = 'opencode'").toArray();
      expect(notices2.length).toBe(0);
    }));

  it('refuses automatic retry if original owner credential was revoked', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Set up session_control_sessions table and credentials
      sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'agent', ?, 'fam-1', 'agent-revoked', 'agent-revoked', ?, 'admin', ?, ?)",
        'tok-agent-1',
        'hash-agent-1',
        JSON.stringify(['fleet:launch', 'bus:send']),
        iso(),
        iso(3600000)
      );

      // Create a launch for agent-revoked on opencode
      const reserved = fleet.reserve(
        { workspace: 'demo', engine: 'opencode', tier: 'daily', prompt: 'revoked test', idempotency_key: 'ik-revoked-1' },
        { caller: { kind: 'agent', actor: 'agent-revoked', tokenId: 'tok-agent-1' }, scopes: ['fleet:launch', 'bus:send'] }
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;

      // Create linked session in session_control_sessions
      sql.exec(
        "INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, owner_token_id, created_at, updated_at) VALUES (?, 'agent-revoked', 'sk-revoked', ?, ?, 'hetzner', 'opencode', 'headless', 'offered', 'tok-agent-1', ?, ?)",
        'ses-revoked-1',
        JSON.stringify({ workspace: 'demo', tier: 'daily', brief: 'revoked test' }),
        launchId,
        iso(),
        iso()
      );

      // Revoke the agent token before failure / retry
      sql.exec("UPDATE credentials SET revoked_at = ? WHERE id = 'tok-agent-1'", iso());

      // Claim and report failure with engine_auth_expired
      await fleet.handle(claim(launchId, 'opencode-main'), host('hetzner'));
      const res = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );
      expect(res.status).toBe(200);

      // Verify retry was NOT created and session was marked held or failed
      const launchRow = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];
      expect(launchRow[0].retried_launch_id).toBeNull();

      const sessionRow = sql.exec('SELECT state FROM session_control_sessions WHERE id = ?', 'ses-revoked-1').toArray() as any[];
      expect(['held', 'failed']).toContain(sessionRow[0].state);
    }));

  it('refuses automatic retry if workspace requires claim and original intent had legacy_unspecified access', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Create a launch on opencode before claim policy is imposed
      const reserved = fleet.reserve(
        { workspace: 'demo', engine: 'opencode', tier: 'daily', prompt: 'claim test', idempotency_key: 'ik-claim-1' },
        admin
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;

      // Host claims the launch first
      const claimRes = await fleet.handle(claim(launchId, 'opencode-main'), host('hetzner'));
      expect(claimRes.status).toBe(200);

      // Now configure workspace demo to require claim
      sql.exec("INSERT INTO fleet_claim_policy (workspace, project, require_claim) VALUES ('demo', 'proj-alpha', 1)");

      // Claim and fail with engine_auth_expired
      const res = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );
      expect(res.status).toBe(200);

      // Verify retry was refused because claim is required
      const launchRow = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];
      expect(launchRow[0].retried_launch_id).toBeNull();
    }));

  it('refuses automatic retry if parent launch is no longer active', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Create parent launch
      const parent = fleet.reserve(
        { workspace: 'demo', engine: 'codex', prompt: 'parent goal', idempotency_key: 'ik-parent-1' },
        admin
      );
      expect(parent.status).toBe(201);
      const parentId = parent.value.id;
      // Host claims parent
      await fleet.handle(claim(parentId, parent.value.account), host(parent.value.machine));

      // Create child launch
      const child = fleet.reserve(
        { workspace: 'demo', engine: 'opencode', tier: 'daily', parent_id: parentId, prompt: 'child goal', idempotency_key: 'ik-child-1' },
        admin
      );
      expect(child.status).toBe(201);
      const childId = child.value.id;

      // Parent exits/fails before child fails
      await fleet.handle(
        request(`/launches/${parentId}/result`, {
          state: 'exited',
          native_id: 'nat-parent-1',
          launcher_task_id: 'hub-' + parentId,
          confirmed_exit: true,
        }),
        host(parent.value.machine)
      );

      // Claim and fail child with engine_auth_expired
      await fleet.handle(claim(childId, 'opencode-main'), host('hetzner'));
      const res = await fleet.handle(
        request(`/launches/${childId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + childId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );
      expect(res.status).toBe(200);

      // Verify retry was refused because parent is not active
      const childRow = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', childId).toArray() as any[];
      expect(childRow[0].retried_launch_id).toBeNull();
    }));

  it('preserves interactive mode, with_goal constraint and prompt during retry', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Goal placement requires host-advertised goal routes under the current
      // contract: both Codex-family routes serve goal starts here.
      for (const machine of ['hetzner', 'desktop']) {
        sql.exec('UPDATE machines SET capabilities = ? WHERE id = ?', JSON.stringify({ engines: ['opencode', 'codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8, fleet_goal_routes: [{ engine: 'codex', account: machine === 'hetzner' ? 'zcodex-hetzner' : 'zcodex-desktop', profile: 'zcodex' }] }), machine);
      }
      // Set up session_control_sessions table
      sql.exec(
        "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'agent', ?, 'fam-goal', 'agent-goal', 'agent-goal', ?, 'admin', ?, ?)",
        'tok-agent-goal',
        'hash-agent-goal',
        JSON.stringify(['fleet:launch', 'bus:send']),
        iso(),
        iso(3600000)
      );

      // Start with daily tier, mode: interactive, with_goal: true
      const reserved = fleet.reserve(
        { workspace: 'demo', tier: 'daily', mode: 'interactive', with_goal: true, prompt: 'solve goal thoroughly', idempotency_key: 'ik-goal-1' },
        admin
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;
      const origLaunch = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];
      const origMachine = origLaunch[0].machine_id;
      const origAccount = origLaunch[0].account;

      // Create linked session
      sql.exec(
        "INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, owner_token_id, created_at, updated_at) VALUES (?, 'agent-goal', 'sk-goal', ?, ?, ?, ?, 'interactive', 'offered', 'tok-agent-goal', ?, ?)",
        'ses-goal-1',
        JSON.stringify({ workspace: 'demo', tier: 'daily', mode: 'interactive', with_goal: true, brief: 'solve goal thoroughly' }),
        launchId,
        origMachine,
        origLaunch[0].engine,
        iso(),
        iso()
      );

      // Claim and fail on origMachine
      await fleet.handle(claim(launchId, origAccount), host(origMachine));
      const res = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host(origMachine)
      );
      expect(res.status).toBe(200);

      // Check retried launch
      const orig = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];
      expect(orig[0].retried_launch_id).toBeTruthy();
      const retryId = orig[0].retried_launch_id;

      const retried = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', retryId).toArray() as any[];
      expect(retried[0].mode).toBe('interactive');
      expect(retried[0].prompt).toBe('solve goal thoroughly');
      const retriedReq = JSON.parse(retried[0].request_json);
      expect(retriedReq.with_goal).toBe(true);
    }));

  it('preserves tier during retry even if session request_json omits tier', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Start with daily tier (automatic engine selection -> opencode)
      const reserved = fleet.reserve(
        { workspace: 'demo', tier: 'daily', prompt: 'tier task', idempotency_key: 'ik-tier-1' },
        admin
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;

      const origLaunch = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];

      // Session row omits tier in its request_json (canonical session_control behavior)
      sql.exec(
        "INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at) VALUES (?, 'admin', 'sk-tier-1', ?, ?, ?, ?, 'headless', 'offered', ?, ?)",
        'ses-tier-1',
        JSON.stringify({ workspace: 'demo', brief: 'tier task' }), // NO tier field!
        launchId,
        origLaunch[0].machine_id,
        origLaunch[0].engine,
        iso(),
        iso()
      );

      // Claim and fail on orig machine
      await fleet.handle(claim(launchId, origLaunch[0].account), host(origLaunch[0].machine_id));
      const res = await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host(origLaunch[0].machine_id)
      );
      expect(res.status).toBe(200);

      // Check retried launch recovered tier: 'daily' from orig launch request_json and routed to candidate
      const orig = sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', launchId).toArray() as any[];
      expect(orig[0].retried_launch_id).toBeTruthy();
      const retryId = orig[0].retried_launch_id;

      const retried = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', retryId).toArray() as any[];
      expect(retried[0].machine_id).not.toBe(origLaunch[0].machine_id);
      const retriedReq = JSON.parse(retried[0].request_json);
      expect(retriedReq.tier).toBe('daily');
    }));

  it('records liveness_alerts on engine_auth_expired and resolves it on successful probe', async () =>
    fixture(async (fleet, _routing, sql) => {
      // Ensure liveness_alerts table exists
      sql.exec(`CREATE TABLE IF NOT EXISTS liveness_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, opened_at TEXT NOT NULL, resolved_at TEXT, notified_at TEXT, notify_error TEXT, delivered_to TEXT NOT NULL DEFAULT '[]')`);

      const reserved = fleet.reserve(
        { workspace: 'demo', engine: 'opencode', tier: 'daily', prompt: 'test alert', idempotency_key: 'ik-alert-1' },
        admin
      );
      expect(reserved.status).toBe(201);
      const launchId = reserved.value.id;

      await fleet.handle(claim(launchId, 'opencode-main'), host('hetzner'));
      await fleet.handle(
        request(`/launches/${launchId}/result`, {
          state: 'failed',
          launcher_task_id: 'hub-' + launchId,
          confirmed_exit: true,
          reason: 'engine_auth_expired',
          diagnostic: {
            reason: 'engine_auth_expired',
            message: 'API key expired',
            error_type: 'AuthenticationError',
            stage: 'launcher_run',
          },
        }),
        host('hetzner')
      );

      // Verify liveness alert opened
      const openAlerts = sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind = 'engine_auth_expired' AND subject = 'opencode' AND resolved_at IS NULL").toArray() as any[];
      expect(openAlerts.length).toBe(1);
      expect(openAlerts[0].state).toBe('failing');
      expect(openAlerts[0].detail).toContain('opencode auth login opencode --method device');

      // Now probe succeeds
      const probeRes = await fleet.handle(
        request('/probe', { engine: 'opencode', status: 'ok', machine_id: 'hetzner' }),
        host('hetzner')
      );
      expect(probeRes.status).toBe(200);

      // Verify alert is resolved
      const resolvedAlerts = sql.exec("SELECT * FROM liveness_alerts WHERE subject_kind = 'engine_auth_expired' AND subject = 'opencode' AND resolved_at IS NOT NULL").toArray() as any[];
      expect(resolvedAlerts.length).toBe(1);
    }));
});

describe('reviewer acceptance regressions', () => {
  const authDiagnostic = { reason: 'engine_auth_expired', message: 'The engine authentication failed or API key has expired.', error_type: 'RuntimeError', stage: 'dispatch_headless' };
  it('reviewer preserves explicit engine override by refusing automatic fallback', async () => fixture(async (fleet, _routing, sql) => {
    const launch = fleet.reserve({workspace: 'demo', engine: 'opencode', tier: 'daily', prompt: 'explicit engine test', idempotency_key: 'review-explicit'}, admin);
    expect(launch.status).toBe(201);
    await fleet.handle(claim(launch.value.id), host('hetzner'));
    const result = await fleet.handle(request(`/launches/${launch.value.id}/result`, {state: 'failed', launcher_task_id: 'hub-' + launch.value.id, confirmed_exit: true, reason: 'engine_auth_expired', diagnostic: authDiagnostic}), host('hetzner'));
    expect(result.status).toBe(200);
    expect(sql.exec('SELECT retried_launch_id FROM fleet_launches WHERE id = ?', launch.value.id).toArray()[0].retried_launch_id).toBeNull();
  }));
  it('reviewer marks bound failed session failed when no healthy retry route exists', async () => fixture(async (fleet, _routing, sql) => {
    const launch = fleet.reserve({workspace: 'demo', tier: 'daily', prompt: 'bound failure test', idempotency_key: 'review-bound'}, admin);
    expect(launch.status).toBe(201);
    const original = sql.exec('SELECT * FROM fleet_launches WHERE id = ?', launch.value.id).toArray()[0];
    await fleet.handle(claim(launch.value.id, original.account), host(original.machine_id));
    const running = await fleet.handle(request(`/launches/${launch.value.id}/result`, {state: 'running', native_id: 'native-review', launcher_task_id: 'hub-' + launch.value.id}), host(original.machine_id));
    expect(running.status).toBe(200);
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at) VALUES ('review-session', 'test-admin', 'review-session', ?, ?, ?, ?, 'headless', 'running', ?, ?)", JSON.stringify({workspace:'demo', brief:'bound failure test', access:{mode:'read'}}), launch.value.id, original.machine_id, original.engine, iso(), iso());
    sql.exec('UPDATE fleet_accounts SET enabled = 0');
    const failed = await fleet.handle(request(`/launches/${launch.value.id}/result`, {state:'failed', native_id:'native-review', launcher_task_id:'hub-'+launch.value.id, confirmed_exit:true, diagnostic:authDiagnostic}), host(original.machine_id));
    expect(failed.status).toBe(200);
    expect(sql.exec("SELECT state FROM session_control_sessions WHERE id='review-session'").toArray()[0].state).toBe('failed');
  }));
});


describe('headless permission refusal recovery', () => {
  const diagnostic = { reason: 'engine_tool_permission_refused', message: 'Headless engine refused write or network tools; route held until a successful execution probe.', error_type: 'PermissionError', stage: 'dispatch_headless' };
  it('holds only headless routing and admission refresh cannot clear the refusal', async () => fixture(async (fleet, _routing, sql) => {
    await fleet.handle(request('/probe', {engine:'opencode', status:'unavailable', reason:diagnostic.reason}), host('hetzner'));
    await fleet.handle(request('/probe', {engine:'opencode', status:'ok', reason:'admission_configured'}), host('hetzner'));
    const plan = await (await fleet.handle(request('/plan', {workspace:'demo', tier:'daily', mode:'headless', prompt:'bounded probe'}),admin)).json() as any;
    expect(plan.selected?.engine).not.toBe('opencode');
    expect(plan.rejected).toContainEqual(expect.objectContaining({engine:'opencode', reason:diagnostic.reason}));
    const interactive = await (await fleet.handle(request('/plan', {workspace:'demo', tier:'daily', mode:'interactive', engine:'opencode'}),admin)).json() as any;
    expect(interactive.selected?.engine).toBe('opencode');
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='opencode'").one().status).toBe('unavailable');
  }));
  it('persists the failure, automatically holds the engine and retries daily work once', async () => fixture(async (fleet, _routing, sql) => {
    // Make the initial automatic choice deterministic without an explicit override.
    sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE provider='zai'");
    const launch = fleet.reserve({workspace:'demo', tier:'daily', mode:'headless', prompt:'permission smoke', idempotency_key:'permission-smoke'}, admin);
    expect(launch.status).toBe(201); expect(launch.value.engine).toBe('opencode');
    await fleet.handle(claim(launch.value.id),host('hetzner'));
    sql.exec("UPDATE fleet_accounts SET enabled=1 WHERE provider='zai'");
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, binding_json, host_family, created_at, updated_at) VALUES ('retry-permission', 'test-admin', 'retry-permission', ?, ?, 'hetzner', 'opencode', 'headless', 'running', ?, 'original-host-family', ?, ?)",JSON.stringify({workspace:'demo',tier:'daily',brief:'permission smoke'}),launch.value.id,JSON.stringify({native_id:'exact-owned-native',tag:'old',generation:1}),iso(),iso());
    sql.exec("INSERT INTO session_control_commands (id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at) VALUES ('old-command','retry-permission','capture','test-admin','old-command','{}','recorded',1,'old-command',?,?)",iso(),iso());
    const result = await fleet.handle(request(`/launches/${launch.value.id}/result`, {state:'failed', native_id:'exact-owned-native', launcher_task_id:'hub-'+launch.value.id, confirmed_exit:true, diagnostic}),host('hetzner'));
    expect(result.status).toBe(200);
    const failed = sql.exec('SELECT * FROM fleet_launches WHERE id=?',launch.value.id).one();
    expect(failed.state).toBe('failed'); expect(failed.exit_reason).toBe(diagnostic.reason);
    expect(JSON.parse(failed.launch_error_json)).toEqual(diagnostic);
    expect(failed.retried_launch_id).toBeTruthy();
    const retry = sql.exec('SELECT * FROM fleet_launches WHERE id=?',failed.retried_launch_id).one();
    expect(retry.engine).toBe('codex');
    expect(sql.exec("SELECT fleet_launch_id, binding_json, host_family FROM session_control_sessions WHERE id='retry-permission'").one()).toMatchObject({fleet_launch_id:retry.id,binding_json:null,host_family:null});
    expect(sql.exec("SELECT state FROM session_control_commands WHERE id='old-command'").one().state).toBe('failed');
    expect(sql.exec("SELECT reason FROM fleet_engine_health WHERE engine='opencode'").one().reason).toBe(diagnostic.reason);
  }));
  it('holds a refusal even before native cleanup is confirmed, retaining reservation', async () => fixture(async (fleet, _routing, sql) => {
    const launch = fleet.reserve({workspace:'demo', tier:'daily', mode:'headless', engine:'opencode', prompt:'probe', idempotency_key:'permission-uncertain'},admin);
    await fleet.handle(claim(launch.value.id),host('hetzner'));
    const result = await fleet.handle(request(`/launches/${launch.value.id}/result`, {state:'uncertain', native_id:'exact-owned-native', launcher_task_id:'hub-'+launch.value.id, diagnostic}),host('hetzner'));
    expect(result.status).toBe(200);
    expect(sql.exec('SELECT state FROM fleet_launches WHERE id=?',launch.value.id).one().state).toBe('uncertain');
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='opencode'").one().status).toBe('unavailable');
    sql.exec("INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at) VALUES ('22222222-2222-4222-8222-222222222222', 'test-admin', 'permission-session', ?, ?, 'hetzner', 'opencode', 'headless', 'uncertain', ?, ?)", JSON.stringify({workspace:'demo', brief:'probe'}),launch.value.id,iso(),iso());
    const sessions = new SessionControlService(sql, (cb: any) => cb(), () => now);
    const shown = await sessions.handle(new Request('https://hub.test/api/sessions/22222222-2222-4222-8222-222222222222'),admin);
    expect(shown.status).toBe(200);
    expect(await shown.json()).toMatchObject({state:'failed', lifecycle_state:'uncertain', cleanup_pending:true, reason:diagnostic.reason});
  }));
});


describe('device auth preflight rejection', () => {
  it('fences an unstarted auth failure, holds the route and places same-tier work elsewhere', async () => fixture(async (fleet, _routing, sql) => {
    sql.exec("UPDATE fleet_accounts SET enabled=0 WHERE provider='zai'");
    const launch = fleet.reserve({workspace:'demo', tier:'daily', mode:'headless', prompt:'device auth smoke', idempotency_key:'preflight-auth'}, admin);
    expect(launch.status).toBe(201); expect(launch.value.engine).toBe('opencode');
    sql.exec("UPDATE fleet_accounts SET enabled=1 WHERE provider='zai'");
    const res = await fleet.handle(request(`/launches/${launch.value.id}/reject`, {reason:'engine_auth_expired'}), host('hetzner'));
    expect(res.status).toBe(200);
    const data:any = await res.json();
    expect(data.rejected).toContainEqual(expect.objectContaining({engine:'opencode',reason:'engine_auth_expired'}));
    const retry = sql.exec('SELECT * FROM fleet_launches WHERE id=?',data.id).one();
    expect(retry.engine).toBe('codex'); expect(retry.mode).toBe('headless');
    expect(JSON.parse(retry.request_json).tier).toBe('daily');
    expect(sql.exec('SELECT verification_status FROM fleet_launches WHERE id=?',launch.value.id).one().verification_status).toBe('not_started');
    expect(sql.exec("SELECT reason FROM fleet_engine_health WHERE engine='opencode'").one().reason).toBe('engine_auth_expired');
    // Replaying an old refusal cannot retire a later claim or create extra launches.
    expect((await fleet.handle(request(`/launches/${launch.value.id}/reject`,{reason:'engine_auth_expired'}),host('hetzner'))).status).toBe(409);
  }));
  it('retains an explicit engine request with an auth rejection when alternatives are disallowed', async () => fixture(async (fleet,_routing,sql) => {
    const launch = fleet.reserve({workspace:'demo',engine:'opencode',tier:'daily',mode:'headless',prompt:'explicit',idempotency_key:'preflight-explicit'},admin);
    const res = await fleet.handle(request(`/launches/${launch.value.id}/reject`,{reason:'engine_auth_expired'}),host('hetzner'));
    const data:any = await res.json();
    expect(res.status).toBe(200); expect(data.state).toBe('queued');
    expect(data.rejected).toContainEqual(expect.objectContaining({reason:'engine_auth_expired'}));
    expect(sql.exec('SELECT native_id FROM fleet_launches WHERE id=?',data.id).one().native_id).toBeNull();
  }));
});


describe('device recovery proof boundaries', () => {
  it('clears its auth notice after all auth holds recover but preserves unrelated tool holds', async () => fixture(async (fleet,_routing,sql) => {
    sql.exec("INSERT INTO fleet_engine_health (engine,machine_id,status,reason,updated_at) VALUES ('opencode','hetzner','unavailable','engine_auth_expired',?)",iso());
    sql.exec("INSERT INTO operator_notices (notice_key,engine,message,created_at) VALUES ('engine_auth_expired:opencode','opencode','device login unavailable',?)",iso());
    const probe = () => fleet.handle(request('/probe',{engine:'opencode',status:'ok',reason:'device_auth_verified'}),host('hetzner'));
    expect((await probe()).status).toBe(200);
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='opencode'").one().status).toBe('ok');
    expect(sql.exec("SELECT * FROM operator_notices WHERE engine='opencode'").toArray()).toHaveLength(0);
    sql.exec("UPDATE fleet_engine_health SET status='unavailable',reason='engine_tool_permission_refused' WHERE engine='opencode'");
    await probe();
    expect(sql.exec("SELECT status FROM fleet_engine_health WHERE engine='opencode'").one().status).toBe('unavailable');
  }));
});
