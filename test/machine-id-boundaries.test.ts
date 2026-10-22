import { describe, it, expect } from 'vitest';
import { FIXTURE_INTAKE_ID, seedFixtureIntake } from './tbm-fixture';
import { env, runInDurableObject } from 'cloudflare:test';
import { isValidMachineId, MACHINE_ID_REGEX } from '../src/machine-id';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import { MonitoringService } from '../src/monitoring';
import { StatusService } from '../src/status';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

describe('Machine-ID Boundary Validation', () => {
  describe('isValidMachineId unit validation', () => {
    it('accepts base64url characters, leading dash, leading underscore, and alphanumeric start', () => {
      // Leading dash
      expect(isValidMachineId('-dash-machine')).toBe(true);
      expect(isValidMachineId('-1234567890')).toBe(true);
      expect(isValidMachineId('-')).toBe(true);

      // Leading underscore
      expect(isValidMachineId('_under_machine')).toBe(true);
      expect(isValidMachineId('_1234567890')).toBe(true);
      expect(isValidMachineId('_')).toBe(true);

      // Alphanumeric start
      expect(isValidMachineId('machine-01')).toBe(true);
      expect(isValidMachineId('hetzner')).toBe(true);
      expect(isValidMachineId('0123456789')).toBe(true);
      expect(isValidMachineId('A')).toBe(true);

      // Genuine base64url 16-byte random strings (22 characters)
      expect(isValidMachineId('-aB3_cD4-eF5_gH6-iJ7_k')).toBe(true);
      expect(isValidMachineId('_aB3-cD4_eF5-gH6_iJ7-k')).toBe(true);
      expect(isValidMachineId('AaB3_cD4-eF5_gH6-iJ7_k')).toBe(true);

      // Exact bounded length: 1 to 64
      expect(isValidMachineId('x'.repeat(1))).toBe(true);
      expect(isValidMachineId('x'.repeat(64))).toBe(true);
      expect(isValidMachineId('-' + 'x'.repeat(63))).toBe(true);
      expect(isValidMachineId('_' + 'x'.repeat(63))).toBe(true);
    });

    it('rejects empty, oversized, whitespace, slashes, control and invalid punctuation characters', () => {
      // Empty
      expect(isValidMachineId('')).toBe(false);

      // Oversized (> 64 chars)
      expect(isValidMachineId('x'.repeat(65))).toBe(false);
      expect(isValidMachineId('-' + 'x'.repeat(64))).toBe(false);

      // Whitespace
      expect(isValidMachineId(' ')).toBe(false);
      expect(isValidMachineId(' -machine')).toBe(false);
      expect(isValidMachineId('-machine ')).toBe(false);
      expect(isValidMachineId('mach ine')).toBe(false);
      expect(isValidMachineId('\t')).toBe(false);
      expect(isValidMachineId('\n')).toBe(false);

      // Disallowed punctuation and delimiters
      expect(isValidMachineId('machine.local')).toBe(false);
      expect(isValidMachineId('machine/01')).toBe(false);
      expect(isValidMachineId('machine\\01')).toBe(false);
      expect(isValidMachineId('machine:01')).toBe(false);
      expect(isValidMachineId('machine@01')).toBe(false);
      expect(isValidMachineId('machine#01')).toBe(false);
      expect(isValidMachineId('machine?01')).toBe(false);

      // Control characters
      expect(isValidMachineId('machine\0')).toBe(false);
      expect(isValidMachineId('machine\x1b')).toBe(false);

      // Non-strings
      expect(isValidMachineId(null)).toBe(false);
      expect(isValidMachineId(undefined)).toBe(false);
      expect(isValidMachineId(123)).toBe(false);
      expect(isValidMachineId({})).toBe(false);
      expect(isValidMachineId([])).toBe(false);
    });
  });

  describe('Fleet Service machine-ID seams', () => {
    const now = Date.now();
    const iso = (delta = 0) => new Date(now + delta).toISOString();
    const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };

    async function withFleet(fn: (s: FleetService, sql: any) => Promise<void>) {
      const bindings = env as unknown as Env;
      const stub = bindings.HUB.get(bindings.HUB.idFromName('fleet-mid-' + crypto.randomUUID()));
      await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
        const service = new FleetService(hub.sql, (cb) => state.storage.transactionSync(cb), () => now);
        service.init();
        const sql = hub.sql;

        // Register machines with leading dash, leading underscore, and alphanumeric
        for (const machine of ['-dash-host', '_under_host', 'alpha-host']) {
          sql.exec(
            'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            machine, machine, machine, iso(), iso(),
            JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, load: [1], gate: { launch_allowed: true, at: iso() } }),
            0,
            JSON.stringify({ engines: ['codex'], fleet_modes: ['headless', 'interactive'], workspaces: ['demo'], cpu_count: 8 })
          );
          sql.exec(
            'INSERT INTO host_quota_snapshots (host_id, schema_version, captured_at, captured_at_ms, received_at, received_at_ms, status, error, routes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            machine, '1.0', iso(), now, iso(), now, 'ok', null,
            JSON.stringify({ codex: { status: 'ok', observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: iso(86400000) } } } })
          );
        }
        await fn(service, sql);
      });
    }

    it('PUT /api/fleet/accounts accepts routes with leading dash and leading underscore machine IDs', async () => withFleet(async (s, sql) => {
      const reg = {
        accounts: [
          { label: 'shared-pool', provider: 'codex', max_concurrency: 4, reserve_percent: 15, enabled: true },
        ],
        routes: [
          { machine_id: '-dash-host', engine: 'codex', account: 'shared-pool', profile: null },
          { machine_id: '_under_host', engine: 'codex', account: 'shared-pool', profile: null },
          { machine_id: 'alpha-host', engine: 'codex', account: 'shared-pool', profile: null },
        ],
      };
      const req = new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(reg) });
      const res = await s.handle(req, admin);
      expect(res.status).toBe(200);

      const savedRoutes = sql.exec('SELECT machine_id, engine, account FROM fleet_routes ORDER BY machine_id').toArray();
      expect(savedRoutes).toEqual([
        { machine_id: '-dash-host', engine: 'codex', account: 'shared-pool' },
        { machine_id: '_under_host', engine: 'codex', account: 'shared-pool' },
        { machine_id: 'alpha-host', engine: 'codex', account: 'shared-pool' },
      ]);
    }));

    it('PUT /api/fleet/accounts refuses punctuation-leading account labels and invalid engines', async () => withFleet(async (s) => {
      // Punctuation-leading account labels remain invalid
      const badAccount1 = {
        accounts: [{ label: '-bad-account', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: 'alpha-host', engine: 'codex', account: '-bad-account', profile: null }],
      };
      const res1 = await s.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(badAccount1) }), admin);
      expect(res1.status).toBe(400);

      const badAccount2 = {
        accounts: [{ label: '_bad_account', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: 'alpha-host', engine: 'codex', account: '_bad_account', profile: null }],
      };
      const res2 = await s.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(badAccount2) }), admin);
      expect(res2.status).toBe(400);

      // Punctuation-leading engine remains invalid
      const badEngine = {
        accounts: [{ label: 'ok-pool', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: '-dash-host', engine: '-codex', account: 'ok-pool', profile: null }],
      };
      const res3 = await s.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(badEngine) }), admin);
      expect(res3.status).toBe(400);
    }));

    it('PUT /api/fleet/accounts refuses unknown or malformed machine IDs', async () => withFleet(async (s) => {
      // Unknown machine ID (not registered in machines table)
      const unknownRoute = {
        accounts: [{ label: 'pool-ok', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: '-not-registered', engine: 'codex', account: 'pool-ok', profile: null }],
      };
      const res1 = await s.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(unknownRoute) }), admin);
      expect(res1.status).toBe(400);

      // Malformed machine ID (slashes, whitespace)
      const malformedRoute = {
        accounts: [{ label: 'pool-ok', provider: 'codex', max_concurrency: 1, reserve_percent: 15, enabled: true }],
        routes: [{ machine_id: 'bad/host', engine: 'codex', account: 'pool-ok', profile: null }],
      };
      const res2 = await s.handle(new Request('https://hub.test/api/fleet/accounts', { method: 'PUT', body: JSON.stringify(malformedRoute) }), admin);
      expect(res2.status).toBe(400);
    }));

    it('FleetService.plan accepts explicit placement for machine IDs with leading dash or underscore', async () => withFleet(async (s) => {
      // Configure routes
      await s.handle(new Request('https://hub.test/api/fleet/accounts', {
        method: 'PUT',
        body: JSON.stringify({
          accounts: [{ label: 'shared-pool', provider: 'codex', max_concurrency: 4, reserve_percent: 15, enabled: true }],
          routes: [
            { machine_id: '-dash-host', engine: 'codex', account: 'shared-pool', profile: null },
            { machine_id: '_under_host', engine: 'codex', account: 'shared-pool', profile: null },
          ],
        }),
      }), admin);

      // Plan specifying machine: '-dash-host'
      const planDash: any = s.plan({
        workspace: 'demo',
        prompt: 'test prompt',
        machine: '-dash-host',
        idempotency_key: crypto.randomUUID(),
      });
      expect(planDash.error).toBeUndefined();
      expect(planDash.selected.machine).toBe('-dash-host');

      // Plan specifying machine: '_under_host'
      const planUnder: any = s.plan({
        workspace: 'demo',
        prompt: 'test prompt',
        machine: '_under_host',
        idempotency_key: crypto.randomUUID(),
      });
      expect(planUnder.error).toBeUndefined();
      expect(planUnder.selected.machine).toBe('_under_host');

      // Plan specifying malformed machine fails with invalid_launch_request
      const planBad: any = s.plan({
        workspace: 'demo',
        prompt: 'test prompt',
        machine: 'bad/machine',
        idempotency_key: crypto.randomUUID(),
      });
      expect(planBad).toEqual({ error: 'invalid_launch_request' });
    }));
  });

  describe('Session Control Service machine-ID seams', () => {
    const now = Date.now();
    const writerAuth = { caller: { isAdmin: true, actor: 'test-writer', kind: 'human' }, scopes: ['writer'] };

    async function withSessionControl(fn: (sc: SessionControlService, hub: Hub) => Promise<void>) {
      const bindings = env as unknown as Env;
      const stub = bindings.HUB.get(bindings.HUB.idFromName('sc-mid-' + crypto.randomUUID()));
      await runInDurableObject(stub, async (hub: Hub) => {
        hub.sessionControlService.init();
        seedFixtureIntake(hub.sql);
        await fn(hub.sessionControlService, hub);
      });
    }

    it('POST /api/sessions accepts explicit machine placement with leading dash or underscore', async () => withSessionControl(async (sc, hub) => {
      // Start intent targeting machine '-dash-host'
      const reqDash = new Request('https://hub.test/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: 'intent-dash-1',
          workspace: 'demo',
          brief: 'session on dash host',
          machine: '-dash-host',
          intake_task_id: FIXTURE_INTAKE_ID,
        }),
      });
      const resDash = await sc.handle(reqDash, writerAuth);
      // Not rejected by input validation (either held because no placement hook or started)
      expect(resDash.status).not.toBe(400);

      // Start intent targeting machine '_under_host'
      const reqUnder = new Request('https://hub.test/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: 'intent-under-1',
          workspace: 'demo',
          brief: 'session on under host',
          machine: '_under_host',
          intake_task_id: FIXTURE_INTAKE_ID,
        }),
      });
      const resUnder = await sc.handle(reqUnder, writerAuth);
      expect(resUnder.status).not.toBe(400);

      // Malformed machine ID rejected with 400 invalid_session_request
      const reqBad = new Request('https://hub.test/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: 'intent-bad-1',
          workspace: 'demo',
          brief: 'session on bad host',
          machine: 'bad/machine',
        }),
      });
      const resBad = await sc.handle(reqBad, writerAuth);
      expect(resBad.status).toBe(400);
      expect(((await resBad.json()) as any).error).toBe('invalid_session_request');

      // Punctuation-leading key remains rejected with 400 invalid_session_request
      const reqBadKey = new Request('https://hub.test/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: '-invalid-key',
          workspace: 'demo',
          brief: 'bad key session',
          machine: '-dash-host',
        }),
      });
      const resBadKey = await sc.handle(reqBadKey, writerAuth);
      expect(resBadKey.status).toBe(400);
      expect(((await resBadKey.json()) as any).error).toBe('invalid_session_request');
    }));
  });

  describe('Monitoring Service machine-ID seams', () => {
    const now = Date.parse('2026-10-18T12:00:00Z');
    const iso = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();

    async function withMonitoring(fn: (mon: MonitoringService, hub: Hub) => Promise<void>) {
      const bindings = env as unknown as Env;
      const stub = bindings.HUB.get(bindings.HUB.idFromName('mon-mid-' + crypto.randomUUID()));
      await runInDurableObject(stub, async (hub: Hub) => {
        (hub.monitoringService as any).now = () => now;
        (hub.statusService as any).now = () => now;
        (hub.livenessService as any).now = () => now;
        await fn(hub.monitoringService, hub);
      });
    }

    it('GET /api/monitoring/disk accepts machine filters with leading dash or underscore and rejects malformed', async () => withMonitoring(async (mon) => {
      const auth = { kind: 'admin' as const, actor: 'admin' };

      const resDash = await mon.handleGet(
        new Request('https://hub.test/api/monitoring/disk?machine=-dash-host', { method: 'GET' }),
        auth
      );
      expect(resDash.status).toBe(200);

      const resUnder = await mon.handleGet(
        new Request('https://hub.test/api/monitoring/disk?machine=_under_host', { method: 'GET' }),
        auth
      );
      expect(resUnder.status).toBe(200);

      const resBad = await mon.handleGet(
        new Request('https://hub.test/api/monitoring/disk?machine=bad/host', { method: 'GET' }),
        auth
      );
      expect(resBad.status).toBe(400);
    }));

    it('POST /api/monitoring/observations/disk accepts observations from machines with leading dash or underscore', async () => withMonitoring(async (mon) => {
      const authDash = { kind: 'host' as const, machine_id: '-dash-host', tokenId: 'cred--dash-host' };
      const resDash = await mon.handleObservation(
        new Request('https://hub.test/api/monitoring/observations/disk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: '-dash-host',
            observed_at: iso(5),
            filesystems: [{
              mount_point: '/var',
              device_id: 'dev-1',
              total_bytes: 100000000000,
              used_bytes: 50000000000,
              free_bytes: 50000000000,
              percent: 50,
            }],
          }),
        }),
        authDash
      );
      expect(resDash.status).toBe(200);

      // Spoofed machine ID rejected with 403
      const resSpoofed = await mon.handleObservation(
        new Request('https://hub.test/api/monitoring/observations/disk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: '_under_host',
            observed_at: iso(5),
            filesystems: [{
              mount_point: '/var',
              device_id: 'dev-1',
              total_bytes: 100000000000,
              used_bytes: 50000000000,
              free_bytes: 50000000000,
              percent: 50,
            }],
          }),
        }),
        authDash
      );
      expect(resSpoofed.status).toBe(403);

      // Malformed machine ID rejected with 400
      const resMalformed = await mon.handleObservation(
        new Request('https://hub.test/api/monitoring/observations/disk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: 'bad/host',
            observed_at: iso(5),
            filesystems: [{
              mount_point: '/var',
              device_id: 'dev-1',
              total_bytes: 100000000000,
              used_bytes: 50000000000,
              free_bytes: 50000000000,
              percent: 50,
            }],
          }),
        }),
        authDash
      );
      expect(resMalformed.status).toBe(400);
    }));

    it('POST /api/monitoring/observations/host-health accepts health observations for leading dash/underscore machines', async () => withMonitoring(async (mon) => {
      const authUnder = { kind: 'host' as const, machine_id: '_under_host', tokenId: 'cred-_under_host' };
      const resUnder = await mon.handleHostHealthObservation(
        new Request('https://hub.test/api/monitoring/observations/host-health', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: '_under_host',
            observed_at: iso(5),
            load: { load1: 1.0, load5: 1.0, load15: 1.0 },
            memory: { total_bytes: 16000000000, available_bytes: 8000000000, used_bytes: 8000000000, percent: 50, status: 'ok' },
          }),
        }),
        authUnder
      );
      expect(resUnder.status).toBe(200);

      // Mismatched machine ID rejected with 403
      const resMismatch = await mon.handleHostHealthObservation(
        new Request('https://hub.test/api/monitoring/observations/host-health', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: '-dash-host',
            observed_at: iso(5),
            load: { load1: 1.0, load5: 1.0, load15: 1.0 },
            memory: { total_bytes: 16000000000, available_bytes: 8000000000, used_bytes: 8000000000, percent: 50, status: 'ok' },
          }),
        }),
        authUnder
      );
      expect(resMismatch.status).toBe(403);
    }));
  });

  describe('Status Service machine-ID seams', () => {
    async function withStatus(fn: (status: StatusService, hub: Hub) => Promise<void>) {
      const bindings = env as unknown as Env;
      const stub = bindings.HUB.get(bindings.HUB.idFromName('status-mid-' + crypto.randomUUID()));
      await runInDurableObject(stub, async (hub: Hub) => {
        await fn(hub.statusService, hub);
      });
    }

    it('GET /api/status accepts host filters with leading dash or underscore and rejects malformed', async () => withStatus(async (st, hub) => {
      const adminAuth = { caller: { kind: 'human', isAdmin: true, actor: 'admin' }, scopes: ['admin'] };

      const authenticate = async () => adminAuth;

      const resDash = await st.handle(new Request('https://hub.test/api/status?host=-dash-host'), authenticate);
      expect(resDash.status).toBe(200);

      const resUnder = await st.handle(new Request('https://hub.test/api/status?host=_under_host'), authenticate);
      expect(resUnder.status).toBe(200);

      const resBad = await st.handle(new Request('https://hub.test/api/status?host=bad/host'), authenticate);
      expect(resBad.status).toBe(400);

      const resWhitespace = await st.handle(new Request('https://hub.test/api/status?host=%20'), authenticate);
      expect(resWhitespace.status).toBe(400);
    }));

    it('POST /api/status/observations rejects spoofed and malformed machine IDs while honoring valid machine IDs', async () => withStatus(async (st) => {
      const hostAuth = { kind: 'host' as const, machine_id: '-dash-host', tokenId: 'cred-1' };

      // Spoofed machine ID rejected with 403
      const resSpoofed = await st.handleObservation(
        new Request('https://hub.test/api/status/observations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: '_different_host',
            native_id: 'native-1',
            kind: 'report',
            observed_at: new Date().toISOString(),
            payload: { headline: 'Test observation report' },
          }),
        }),
        hostAuth
      );
      expect(resSpoofed.status).toBe(403);

      // Malformed machine ID rejected with 400
      const resMalformed = await st.handleObservation(
        new Request('https://hub.test/api/status/observations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: 'bad/machine',
            native_id: 'native-1',
            kind: 'report',
            observed_at: new Date().toISOString(),
            payload: { headline: 'Test observation report' },
          }),
        }),
        hostAuth
      );
      expect(resMalformed.status).toBe(400);
    }));
  });
});
