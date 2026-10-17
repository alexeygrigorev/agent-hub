import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { formatBytes, DISK_THRESHOLDS } from '../src/monitoring';

const now = Date.parse('2026-10-17T18:00:00Z');
const iso = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();

async function fixture(fn: (hub: Hub, clock: { t: number }) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('monitoring-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const clock = { t: now };
    (hub.monitoringService as any).now = () => clock.t;
    (hub.statusService as any).now = () => clock.t;
    (hub.livenessService as any).now = () => clock.t;
    await fn(hub, clock);
  });
}

function enrollHost(sql: any, machineId: string, token: string = 'test-token-' + machineId) {
  const tokenHash = 'hash-' + token;
  sql.exec(
    "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?, ?, ?, ?, ?, 0)",
    machineId, machineId, machineId, iso(86400), iso(10)
  );
  sql.exec(
    "INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'host', ?, NULL, ?, ?, ?, NULL, '[\"host\"]', 'admin', ?, ?)",
    'cred-' + machineId, tokenHash, 'fam-' + machineId, machineId, machineId, iso(86400), new Date(now + 86400000 * 30).toISOString()
  );
}

describe('Disk Monitoring Service', () => {
  it('formats bytes correctly', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1024 * 1024 * 100)).toBe('100.0 MB');
    expect(formatBytes(1024 * 1024 * 1024 * 437)).toBe('437.0 GB');
  });

  it('rejects spoofed machine IDs for host credentials', () => fixture(async (hub) => {
    enrollHost(hub.sql, 'host-a');

    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };
    const req = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-b', // Spoofed!
        observed_at: iso(10),
        filesystems: [
          {
            mount_point: '/',
            device_id: '123',
            is_primary: true,
            total_bytes: 100000,
            used_bytes: 50000,
            free_bytes: 50000,
            percent: 50.0,
            status: 'ok',
          },
        ],
      }),
    });

    const res = await hub.monitoringService.handleObservation(req, auth);
    expect(res.status).toBe(403);
    const body: any = await res.json();
    expect(body.error).toBe('observation_forbidden');
  }));

  it('accepts valid observation and evaluates threshold states (85% warn / 95% critical)', () => fixture(async (hub) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    const req = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-a',
        observed_at: iso(10),
        filesystems: [
          {
            mount_point: '/',
            device_id: 'dev-root',
            is_primary: true,
            total_bytes: 500_000_000_000,
            used_bytes: 400_000_000_000,
            free_bytes: 100_000_000_000,
            percent: 80.0,
            status: 'ok',
          },
          {
            mount_point: '/home',
            device_id: 'dev-root',
            is_primary: false,
            primary_mount: '/',
            total_bytes: 500_000_000_000,
            used_bytes: 400_000_000_000,
            free_bytes: 100_000_000_000,
            percent: 80.0,
            status: 'ok',
          },
          {
            mount_point: '/data',
            device_id: 'dev-data',
            is_primary: true,
            total_bytes: 500_000_000_000,
            used_bytes: 440_000_000_000,
            free_bytes: 60_000_000_000,
            percent: 88.0, // >=85%: warn
            status: 'warn',
            top_consumers: [
              { path: '/data/heavy-project', size_bytes: 100_000_000_000 },
            ],
          },
          {
            mount_point: '/tmp',
            device_id: 'dev-tmp',
            is_primary: true,
            total_bytes: 100_000_000_000,
            used_bytes: 96_000_000_000,
            free_bytes: 4_000_000_000,
            percent: 96.0, // >=95%: critical
            status: 'critical',
            top_consumers: [
              { path: '/tmp/massive.log', size_bytes: 50_000_000_000 },
            ],
          },
        ],
      }),
    });

    const res = await hub.monitoringService.handleObservation(req, auth);
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.ok).toBe(true);
    expect(body.filesystems_count).toBe(4);

    // Verify episodes opened
    const episodes = hub.sql.exec('SELECT * FROM disk_episodes WHERE machine_id = "host-a"').toArray() as any[];
    expect(episodes.length).toBe(2); // /data (warn) and /tmp (critical)

    const dataEp = episodes.find(e => e.mount_point === '/data');
    expect(dataEp.state).toBe('warn');
    expect(dataEp.resolved_at).toBeNull();
    expect(dataEp.intake_task_id).toBeTruthy();

    const tmpEp = episodes.find(e => e.mount_point === '/tmp');
    expect(tmpEp.state).toBe('critical');
    expect(tmpEp.resolved_at).toBeNull();
    expect(tmpEp.intake_task_id).toBeTruthy();

    // Verify liveness alerts created
    const alerts = hub.livenessService.alerts();
    const diskAlerts = alerts.filter((a: any) => a.subject_kind === 'disk');
    expect(diskAlerts.length).toBe(2);
    expect(diskAlerts.find((a: any) => a.subject === 'host-a:/data')?.state).toBe('warn');
    expect(diskAlerts.find((a: any) => a.subject === 'host-a:/tmp')?.state).toBe('critical');

    // Verify intake tasks created
    const tasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray() as any[];
    expect(tasks.length).toBe(2);
  }));

  it('deduplicates episodes and recovers without creating task floods', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Poll 1: /data exceeds 85% warn threshold
    clock.t = now - 7200 * 1000; // 2 hours ago
    const makeReq = (percent: number, status: 'ok' | 'warn' | 'critical', usedBytes: number) =>
      new Request('https://hub.internal/api/monitoring/disk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_id: 'host-a',
          observed_at: new Date(clock.t).toISOString(),
          filesystems: [
            {
              mount_point: '/data',
              device_id: 'dev-data',
              is_primary: true,
              total_bytes: 500_000_000_000,
              used_bytes: usedBytes,
              free_bytes: 500_000_000_000 - usedBytes,
              percent,
              status,
              top_consumers: [{ path: '/data/scratch', size_bytes: usedBytes / 2 }],
            },
          ],
        }),
      });

    const res1 = await hub.monitoringService.handleObservation(makeReq(88.0, 'warn', 440_000_000_000), auth);
    expect(res1.status).toBe(200);

    const initialTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    expect(initialTasks.length).toBe(1);

    // Poll 2 (30 min later): Still high (90% warn) - MUST NOT create a second task
    clock.t += 1800 * 1000;
    const res2 = await hub.monitoringService.handleObservation(makeReq(90.0, 'warn', 450_000_000_000), auth);
    expect(res2.status).toBe(200);

    const secondPollTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    expect(secondPollTasks.length).toBe(1); // Still exactly 1 task! No flood!

    // Verify task history has update
    const taskData = JSON.parse(secondPollTasks[0].data as string);
    expect(taskData.history.length).toBe(2); // intake + update

    // Poll 3 (30 min later): Increases to critical (96%) - updates state on same task
    clock.t += 1800 * 1000;
    const res3 = await hub.monitoringService.handleObservation(makeReq(96.0, 'critical', 480_000_000_000), auth);
    expect(res3.status).toBe(200);

    const thirdPollTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    expect(thirdPollTasks.length).toBe(1); // Still 1 task
    const alertCritical = hub.livenessService.alerts().find((a: any) => a.subject === 'host-a:/data');
    expect(alertCritical?.state).toBe('critical');

    // Poll 4 (30 min later): Recovers to normal (70%)
    clock.t += 1800 * 1000;
    const res4 = await hub.monitoringService.handleObservation(makeReq(70.0, 'ok', 350_000_000_000), auth);
    expect(res4.status).toBe(200);

    // Verify alert is resolved
    const alertResolved = hub.livenessService.alerts().find((a: any) => a.subject === 'host-a:/data');
    expect(alertResolved?.resolved_at).toBeTruthy();

    // Verify intake task is resolved (status = 'done')
    const finalTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    const finalTaskData = JSON.parse(finalTasks[0].data as string);
    expect(finalTaskData.status).toBe('done');
    expect(finalTaskData.current_state).toContain('Recovered');

    // Verify episode is resolved
    const episode = hub.sql.exec('SELECT * FROM disk_episodes WHERE episode_key = "disk:host-a:/data"').toArray()[0] as any;
    expect(episode.resolved_at).toBeTruthy();
  }));

  it('calculates measured growth per day trend with sufficient valid samples and marks insufficient/stale as unknown', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Single sample -> insufficient_history
    clock.t = now - 7200 * 1000;
    const postSample = async (usedGb: number) => {
      const usedBytes = usedGb * 1024 * 1024 * 1024;
      const totalBytes = 500 * 1024 * 1024 * 1024;
      const percent = Math.round((usedBytes / totalBytes) * 1000) / 10;
      const req = new Request('https://hub.internal/api/monitoring/disk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_id: 'host-a',
          observed_at: new Date(clock.t).toISOString(),
          filesystems: [
            {
              mount_point: '/data',
              device_id: 'dev-data',
              is_primary: true,
              total_bytes: totalBytes,
              used_bytes: usedBytes,
              free_bytes: totalBytes - usedBytes,
              percent,
              status: percent >= 85 ? 'warn' : 'ok',
            },
          ],
        }),
      });
      return hub.monitoringService.handleObservation(req, auth);
    };

    await postSample(300); // 300 GB

    let status = hub.monitoringService.getDiskStatus(clock.t);
    let fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('insufficient_history');
    expect(fs.trend.growth_bytes_per_day).toBeNull();

    // Sample 2 hours later: +20 GB growth over 2 hours (0.0833 days)
    clock.t += 2 * 3600 * 1000;
    await postSample(320); // 320 GB (+20GB in 2h = 240 GB/day)

    status = hub.monitoringService.getDiskStatus(clock.t);
    fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('calculated');
    expect(fs.trend.growth_bytes_per_day).toBeGreaterThan(0);
    expect(fs.trend.growth_pct_per_day).toBeGreaterThan(0);
    expect(fs.trend.sample_count).toBe(2);

    // If query time is > 24 hours after last sample, trend state is stale
    const staleTime = clock.t + 25 * 3600 * 1000;
    status = hub.monitoringService.getDiskStatus(staleTime);
    fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('stale');
    expect(fs.trend.growth_bytes_per_day).toBeNull();
  }));

  it('projects disk monitoring and needs_attention into StatusService /api/status output', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Post observation with /data at 92% (warn)
    const req = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-a',
        observed_at: new Date(clock.t).toISOString(),
        filesystems: [
          {
            mount_point: '/',
            device_id: 'dev-1',
            is_primary: true,
            total_bytes: 400_000_000_000,
            used_bytes: 200_000_000_000,
            free_bytes: 200_000_000_000,
            percent: 50.0,
            status: 'ok',
          },
          {
            mount_point: '/data',
            device_id: 'dev-2',
            is_primary: true,
            total_bytes: 500_000_000_000,
            used_bytes: 460_000_000_000,
            free_bytes: 40_000_000_000,
            percent: 92.0,
            status: 'warn',
            top_consumers: [{ path: '/data/overflow', size_bytes: 100_000_000_000 }],
          },
        ],
      }),
    });
    await hub.monitoringService.handleObservation(req, auth);

    // Query status GET /api/status as admin
    const statusReq = new Request('https://hub.internal/api/status', {
      method: 'GET',
    });
    const statusRes = await hub.statusService.handle(statusReq, async () => ({
      caller: { kind: 'human', actor: 'admin', isAdmin: true },
      scopes: ['admin'],
    }));

    expect(statusRes.status).toBe(200);
    const statusData: any = await statusRes.json();

    // Verify disk items in status
    expect(statusData.disk).toBeDefined();
    expect(statusData.disk.length).toBe(2);

    // Verify needs_attention contains /data warn
    expect(statusData.needs_attention).toBeDefined();
    expect(statusData.needs_attention.length).toBe(1);
    expect(statusData.needs_attention[0].severity).toBe('warn');
    expect(statusData.needs_attention[0].subject).toBe('host-a:/data');
    expect(statusData.needs_attention[0].percent).toBe(92.0);

    // Verify categories includes disk_monitoring
    expect(statusData.categories.disk_monitoring).toBeDefined();
    expect(statusData.categories.disk_monitoring.state).toBe('collected_disk_monitoring');
  }));

  it('rejects invalid or oversized payloads', () => fixture(async (hub) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Oversized body (> 32 KiB)
    const giantConsumers = Array.from({ length: 500 }, (_, i) => ({
      path: `/data/test/very/long/nested/path/to/some/file_${i}`,
      size_bytes: 1000,
    }));
    const overReq = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-a',
        observed_at: iso(10),
        filesystems: [
          {
            mount_point: '/data',
            device_id: 'dev-1',
            is_primary: true,
            total_bytes: 1000,
            used_bytes: 500,
            free_bytes: 500,
            percent: 50.0,
            status: 'ok',
            top_consumers: giantConsumers,
          },
        ],
      }),
    });
    const overRes = await hub.monitoringService.handleObservation(overReq, auth);
    expect(overRes.status).toBe(413);

    // Duplicate mount points rejected
    const dupReq = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-a',
        observed_at: iso(10),
        filesystems: [
          { mount_point: '/', device_id: '1', is_primary: true, total_bytes: 10, used_bytes: 5, free_bytes: 5, percent: 50, status: 'ok' },
          { mount_point: '/', device_id: '1', is_primary: false, total_bytes: 10, used_bytes: 5, free_bytes: 5, percent: 50, status: 'ok' },
        ],
      }),
    });
    const dupRes = await hub.monitoringService.handleObservation(dupReq, auth);
    expect(dupRes.status).toBe(400);
  }));
});
