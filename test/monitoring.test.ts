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

  it('deduplicates alias pressure and retires legacy duplicate tasks without claiming recovery', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };
    const sample = (alias: boolean) => new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ machine_id: 'host-a', observed_at: new Date(clock.t).toISOString(), filesystems: [
        { mount_point: '/data', device_id: 'same-disk', is_primary: true, total_bytes: 1000, used_bytes: 934, free_bytes: 66, percent: 93.4, status: 'warn' },
        { mount_point: '/tmp', device_id: 'same-disk', is_primary: !alias, primary_mount: alias ? '/data' : null, total_bytes: 1000, used_bytes: 934, free_bytes: 66, percent: 93.4, status: 'warn' },
      ] }),
    });
    expect((await hub.monitoringService.handleObservation(sample(false), auth)).status).toBe(200);
    const oldAlias: any = hub.sql.exec("SELECT * FROM disk_episodes WHERE mount_point='/tmp'").toArray()[0];
    expect(oldAlias.intake_task_id).toBeTruthy();
    clock.t += 60000;
    expect((await hub.monitoringService.handleObservation(sample(true), auth)).status).toBe(200);
    const active = hub.sql.exec('SELECT * FROM disk_episodes WHERE resolved_at IS NULL').toArray() as any[];
    expect(active.map(x => x.mount_point)).toEqual(['/data']);
    const aliasTask: any = hub.sql.exec('SELECT data FROM intake_tasks WHERE id=?', oldAlias.intake_task_id).toArray()[0];
    const task = JSON.parse(aliasTask.data);
    expect(task.status).toBe('done');
    expect(task.current_state).toContain('usage remains 93.4% (warn)');
    expect(task.current_state).toContain('not a space recovery');
    const summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.filesystems).toHaveLength(2);
    expect(summary.needs_attention.map((x: any) => x.subject)).toEqual(['host-a:/data']);
    const version = task.version;
    clock.t += 60000;
    expect((await hub.monitoringService.handleObservation(sample(true), auth)).status).toBe(200);
    const repeated: any = hub.sql.exec('SELECT data FROM intake_tasks WHERE id=?', oldAlias.intake_task_id).toArray()[0];
    expect(JSON.parse(repeated.data).version).toBe(version);
    expect(hub.sql.exec('SELECT id FROM intake_tasks').toArray()).toHaveLength(2);
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

    // Poll 2 (30 min later): Still high (90% warn) - MUST NOT create a second task, and MUST NOT emit duplicate event on unchanged poll
    clock.t += 1800 * 1000;
    const res2 = await hub.monitoringService.handleObservation(makeReq(90.0, 'warn', 450_000_000_000), auth);
    expect(res2.status).toBe(200);

    const secondPollTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    expect(secondPollTasks.length).toBe(1); // Still exactly 1 task! No flood!

    // Verify task history has NO new event on unchanged poll
    const taskData = JSON.parse(secondPollTasks[0].data as string);
    expect(taskData.history.length).toBe(1); // Only original intake!

    // Poll 3 (30 min later): Increases to critical (96%) - severity changed!
    clock.t += 1800 * 1000;
    const res3 = await hub.monitoringService.handleObservation(makeReq(96.0, 'critical', 480_000_000_000), auth);
    expect(res3.status).toBe(200);

    const thirdPollTasks = hub.sql.exec('SELECT * FROM intake_tasks').toArray();
    expect(thirdPollTasks.length).toBe(1); // Still 1 task
    const taskData3 = JSON.parse(thirdPollTasks[0].data as string);
    expect(taskData3.history.length).toBe(2); // Intake + severity change event
    expect(taskData3.history[1].text).toContain('warn to critical');
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
    expect(finalTaskData.history.length).toBe(3); // Intake + severity change + resolution

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

  it('computes percent like df using used/(used+avail) when percent omitted', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };
    // 100 GB total, 80 GB used, 15 GB avail (5 GB reserved for root).
    // Like df: used / (used + avail) = 80 / 95 = 84.2%, NOT used / total (80%).
    const req = new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-a',
        observed_at: new Date(clock.t).toISOString(),
        filesystems: [
          {
            mount_point: '/',
            device_id: 'dev-root',
            is_primary: true,
            total_bytes: 100 * 1024 * 1024 * 1024,
            used_bytes: 80 * 1024 * 1024 * 1024,
            free_bytes: 15 * 1024 * 1024 * 1024, // avail
          },
        ],
      }),
    });
    const res = await hub.monitoringService.handleObservation(req, auth);
    expect(res.status).toBe(200);
    const status = hub.monitoringService.getDiskStatus(clock.t);
    expect(status.machines[0].filesystems[0].percent).toBe(84.2);
  }));

  it('clips one-off cleanup drops and reports insufficient_history instead of bogus negative trend', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };
    const totalBytes = 1000 * 1024 * 1024 * 1024; // 1000 GB

    const postSample = async (usedGB: number) => {
      const usedBytes = usedGB * 1024 * 1024 * 1024;
      const freeBytes = totalBytes - usedBytes;
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
              free_bytes: freeBytes,
              percent,
              status: 'ok',
            },
          ],
        }),
      });
      return hub.monitoringService.handleObservation(req, auth);
    };

    // Initial reading: 1000 GB used
    await postSample(1000);

    // 1 hour later: cleanup dropped usage from 1000 GB to 960 GB (-40 GB drop)
    clock.t += 3600 * 1000;
    await postSample(960);

    // Should NOT report -960 GB/day or -972.7 GB/day; must report insufficient_history honestly!
    let status = hub.monitoringService.getDiskStatus(clock.t);
    let fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('insufficient_history');
    expect(fs.trend.growth_bytes_per_day).toBeNull();

    // 30 minutes later: usage grows slightly from 960 GB to 960.5 GB (+0.5 GB in 30 min)
    // Post-cleanup span is 30m (< 1h minimum window) -> still insufficient_history
    clock.t += 1800 * 1000;
    await postSample(960.5);
    status = hub.monitoringService.getDiskStatus(clock.t);
    fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('insufficient_history');
    expect(fs.trend.growth_bytes_per_day).toBeNull();

    // 1.5 hours later (2 hours after cleanup drop): usage reaches 962 GB (+2 GB in 2h = +24 GB/day)
    // Post-cleanup span is 2h (>= 1h minimum window). The -40 GB cleanup drop is clipped out!
    clock.t += 5400 * 1000;
    await postSample(962);
    status = hub.monitoringService.getDiskStatus(clock.t);
    fs = status.machines[0].filesystems[0];
    expect(fs.trend.state).toBe('calculated');
    expect(fs.trend.growth_bytes_per_day).toBeGreaterThan(0);
    // Calculated over post-drop window (2h): +2 GB / (2/24 days) = 24 GB/day
    const expectedPerDay = Math.round((2 * 1024 * 1024 * 1024) / (2 / 24));
    expect(fs.trend.growth_bytes_per_day).toBe(expectedPerDay);
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

  it('enforces canonical task CAS and preserves history across real IntakeService calls', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };
    const adminAuth = async () => ({ caller: { kind: 'human' as const, actor: 'admin', isAdmin: true }, scopes: ['admin'] });

    // 1. Post disk observation triggering warn episode
    const postObs = (percent: number, status: 'ok' | 'warn' | 'critical', usedBytes: number) =>
      hub.monitoringService.handleObservation(
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
                top_consumers: [{ path: '/data/heavy', size_bytes: usedBytes / 2 }],
              },
            ],
          }),
        }),
        auth,
      );

    await postObs(88.0, 'warn', 440_000_000_000);

    const episode = hub.sql.exec('SELECT * FROM disk_episodes WHERE episode_key = "disk:host-a:/data"').toArray()[0] as any;
    expect(episode.intake_task_id).toBeTruthy();
    const taskId = episode.intake_task_id;

    // 2. Fetch task through real IntakeService GET /api/intake/tasks/:id
    const getReq = new Request(`https://hub.internal/api/intake/tasks/${taskId}`, { method: 'GET' });
    const getRes = await hub.intakeService.handle(getReq, adminAuth);
    expect(getRes.status).toBe(200);
    const getData: any = await getRes.json();
    const initialVersion = getData.task.control.version;
    expect(initialVersion).toBe(1);
    expect(getData.task.payload_data.history[0].kind).toBe('intake');

    // 3. User adds a note to the task through real IntakeService
    const noteReq = new Request(`https://hub.internal/api/intake/tasks/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'user-note-1',
        expected_version: initialVersion,
        current_state: 'Investigating high usage on /data',
      }),
    });
    const noteRes = await hub.intakeService.handle(noteReq, adminAuth);
    expect(noteRes.status).toBe(200);
    const noteData: any = await noteRes.json();
    const versionAfterNote = noteData.task.control.version;
    expect(versionAfterNote).toBe(2);

    // 4. Disk monitor observes severity change: warn -> critical!
    clock.t += 60 * 1000;
    await postObs(96.0, 'critical', 480_000_000_000);

    // Verify task version incremented atomically by monitoring service
    const getRes2 = await hub.intakeService.handle(getReq, adminAuth);
    const getData2: any = await getRes2.json();
    const versionAfterCritical = getData2.task.control.version;
    expect(versionAfterCritical).toBe(3);

    // 5. Stale update attempt using expected_version=2 (before critical escalation) MUST FAIL with 409 version_conflict
    const staleReq = new Request(`https://hub.internal/api/intake/tasks/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'user-stale-attempt',
        expected_version: 2, // Stale! Current version is 3
        current_state: 'Trying to update with stale version',
      }),
    });
    const staleRes = await hub.intakeService.handle(staleReq, adminAuth);
    expect(staleRes.status).toBe(409);
    const staleBody: any = await staleRes.json();
    expect(staleBody.error).toBe('version_conflict');

    // 6. Update with current expected_version=3 succeeds
    const validReq = new Request(`https://hub.internal/api/intake/tasks/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: 'user-valid-attempt',
        expected_version: 3,
        current_state: 'Applied cleanup scripts to /data',
      }),
    });
    const validRes = await hub.intakeService.handle(validReq, adminAuth);
    expect(validRes.status).toBe(200);
    const validBody: any = await validRes.json();
    expect(validBody.task.control.version).toBe(4);

    // 7. Verify history preservation: original intake event (history[0]) is preserved intact!
    const history = validBody.task.payload_data.history;
    expect(history[0].kind).toBe('intake');
    expect(history[0].text).toContain('Disk pressure alert');
  }));

  it('still raises disk intake tasks when many open tasks exist (soft task cap t1038u)', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Seed well above the former hard 256/512 refuse without approaching the safety-net ceiling.
    for (let i = 0; i < 520; i++) {
      const dummyId = `dummy-task-${i.toString().padStart(4, '0')}`;
      hub.sql.exec(
        'INSERT INTO intake_tasks(id, data) VALUES(?, ?)',
        dummyId,
        JSON.stringify({ id: dummyId, project: 'demo', title: `Dummy ${i}`, status: 'open', version: 1, created_at: clock.t, updated_at: clock.t, history: [] }),
      );
    }

    const res1 = await hub.monitoringService.handleObservation(
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
              used_bytes: 450_000_000_000,
              free_bytes: 50_000_000_000,
              percent: 90.0,
              status: 'warn',
            },
          ],
        }),
      }),
      auth,
    );
    expect(res1.status).toBe(200);

    const ep1 = hub.sql.exec('SELECT * FROM disk_episodes WHERE episode_key = "disk:host-a:/data"').toArray()[0] as any;
    expect(ep1).toBeTruthy();
    expect(ep1.state).toBe('warn');
    expect(ep1.intake_task_id).toBeTruthy();
  }));

  it('propagates actual observation timestamps, freshness, and errors in getStatusSummary', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // 1. Missing observations (empty database)
    const summaryEmpty = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryEmpty.categories_entry.freshness).toBe('unknown');
    expect(summaryEmpty.categories_entry.coverage).toBe('unavailable');
    expect(summaryEmpty.categories_entry.observed_at).toBeNull();
    expect(summaryEmpty.categories_entry.reason).toBe('no_disk_observations_recorded');

    // 2. Post observation observed at T_obs
    const obsTime = clock.t;
    const res = await hub.monitoringService.handleObservation(
      new Request('https://hub.internal/api/monitoring/disk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_id: 'host-a',
          observed_at: new Date(obsTime).toISOString(),
          filesystems: [
            {
              mount_point: '/',
              device_id: 'dev-1',
              is_primary: true,
              total_bytes: 100_000_000_000,
              used_bytes: 40_000_000_000,
              free_bytes: 60_000_000_000,
              percent: 40.0,
              status: 'ok',
            },
            {
              mount_point: '/broken',
              device_id: 'dev-2',
              is_primary: true,
              total_bytes: 0,
              used_bytes: 0,
              free_bytes: 0,
              percent: 0.0,
              status: 'error',
              error: 'PermissionDenied',
            },
          ],
        }),
      }),
      auth,
    );
    expect(res.status).toBe(200);

    // Query 100s later (within 180s -> fresh)
    clock.t += 100 * 1000;
    const summaryFresh = hub.monitoringService.getStatusSummary(clock.t);
    // Observed_at MUST be obsTime, NOT current clock.t
    expect(summaryFresh.categories_entry.observed_at).toBe(new Date(obsTime).toISOString());
    // Since /broken has error, coverage must be partial, NOT complete
    expect(summaryFresh.categories_entry.coverage).toBe('partial');
    expect(summaryFresh.categories_entry.freshness).toBe('fresh');
    expect(summaryFresh.filesystems.find((f: any) => f.mount_point === '/broken')?.error).toBe('PermissionDenied');
    // Error filesystem appears in needs_attention
    expect(summaryFresh.needs_attention.some((n: any) => n.subject === 'host-a:/broken')).toBe(true);

    // Query 500s later (age = 600s, between 180s and 900s -> stale)
    clock.t = obsTime + 600 * 1000;
    const summaryStale = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryStale.categories_entry.freshness).toBe('stale');
    expect(summaryStale.categories_entry.coverage).toBe('partial');
    expect(summaryStale.categories_entry.reason).toContain('stale');

    // Query 1200s later (age > 900s -> unavailable / stale)
    clock.t = obsTime + 1200 * 1000;
    const summaryUnavail = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryUnavail.categories_entry.freshness).toBe('stale');
    expect(summaryUnavail.categories_entry.coverage).toBe('partial');
  }));

  it('accepts valid host health observation and evaluates memory pressure alerts', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-health-1');
    const auth = { kind: 'host' as const, machine_id: 'host-health-1', tokenId: 'cred-host-health-1' };

    // 1. Post normal health
    const reqNormal = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-health-1',
        observed_at: iso(10),
        load: { load1: 0.5, load5: 0.8, load15: 1.0 },
        memory: {
          total_bytes: 16_000_000_000,
          available_bytes: 8_000_000_000,
          used_bytes: 8_000_000_000,
          percent: 50.0,
          status: 'ok',
        },
      }),
    });

    const resNormal = await hub.monitoringService.handleHostHealthObservation(reqNormal, auth);
    expect(resNormal.status).toBe(200);

    const getRes = await hub.monitoringService.handleHostHealthGet(
      new Request('https://hub.internal/api/monitoring/health'),
      { caller: { isAdmin: true } },
    );
    expect(getRes.status).toBe(200);
    const getBody: any = await getRes.json();
    expect(getBody.machines).toHaveLength(1);
    expect(getBody.machines[0].machine_id).toBe('host-health-1');
    expect(getBody.machines[0].freshness).toBe('fresh');
    expect(getBody.machines[0].load.load1).toBe(0.5);
    expect(getBody.machines[0].memory.percent).toBe(50.0);

    // 2. Post critical memory pressure (96%) -> opens alert
    const reqCrit = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'host-health-1',
        observed_at: iso(5),
        load: { load1: 4.5, load5: 3.8, load15: 2.0 },
        memory: {
          total_bytes: 16_000_000_000,
          available_bytes: 640_000_000,
          used_bytes: 15_360_000_000,
          percent: 96.0,
          status: 'critical',
        },
      }),
    });

    const resCrit = await hub.monitoringService.handleHostHealthObservation(reqCrit, auth);
    expect(resCrit.status).toBe(200);

    // Verify alert opened in liveness
    const alerts = hub.livenessService.evaluate().subjects;
    const summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.host_health).toHaveLength(1);
    expect(summary.host_health[0].memory.status).toBe('critical');
    expect(summary.needs_attention.some((n: any) => n.subject === 'host-health-1:memory' && n.severity === 'critical')).toBe(true);

    // 3. Reject spoofed machine_id
    const reqSpoof = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'other-host',
        observed_at: iso(5),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50.0, status: 'ok' },
      }),
    });
    const resSpoof = await hub.monitoringService.handleHostHealthObservation(reqSpoof, auth);
    expect(resSpoof.status).toBe(403);
  }));

  it('manages supervisor service health episodes, deduplication, attention and real recovery', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Post healthy supervisor status -> no alert
    const reqHealthy = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(10),
        load: { load1: 0.8, load5: 1.2, load15: 1.0 },
        memory: { total_bytes: 16_000_000_000, available_bytes: 10_000_000_000, used_bytes: 6_000_000_000, percent: 37.5, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'alive',
            status_file_age_s: 42.0,
            observed_at: iso(10),
            reason: 'ok',
          },
        ],
      }),
    });
    const resHealthy = await hub.monitoringService.handleHostHealthObservation(reqHealthy, auth);
    expect(resHealthy.status).toBe(200);

    let summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.host_health).toHaveLength(1);
    expect(summary.host_health[0].services).toHaveLength(1);
    expect(summary.host_health[0].services![0].worker).toBe('alive');
    expect(summary.host_health[0].services![0].status_file_age_s).toBe(52.0);
    expect(summary.needs_attention).toHaveLength(0);

    // Verify GET /api/monitoring/health returns services projection
    const getRes = await hub.monitoringService.handleHostHealthGet(
      new Request('https://hub.internal/api/monitoring/health'),
      { caller: { isAdmin: true } },
    );
    expect(getRes.status).toBe(200);
    const getData: any = await getRes.json();
    expect(getData.machines[0].services).toBeDefined();
    expect(getData.machines[0].services[0].service_id).toBe('supervision');
    expect(getData.machines[0].services[0].worker).toBe('alive');

    // 2. Post stale supervisor status (age 650s > 600s threshold) -> opens single attention episode
    const reqStale = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(8),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 16_000_000_000, available_bytes: 10_000_000_000, used_bytes: 6_000_000_000, percent: 37.5, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'alive',
            status_file_age_s: 650.0,
            observed_at: iso(8),
            reason: 'stale_status_file',
          },
        ],
      }),
    });
    const resStale = await hub.monitoringService.handleHostHealthObservation(reqStale, auth);
    expect(resStale.status).toBe(200);

    // Check alert opened
    const alerts = hub.livenessService.alerts();
    expect(alerts.some((a: any) => a.subject_kind === 'supervisor' && a.subject === 'hetzner:supervision' && a.state === 'stale')).toBe(true);

    // Check episode row created in supervisor_episodes
    const episodes = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodes).toHaveLength(1);
    expect(episodes[0].state).toBe('stale');
    expect(episodes[0].resolved_at).toBeNull();

    // Check needs_attention contains item
    summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.needs_attention.some((n: any) => n.subject === 'hetzner:supervision' && n.severity === 'warn')).toBe(true);

    // 3. Repeated poll with same stale status -> deduplicated, does NOT create duplicate episode
    const reqStale2 = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(6),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 16_000_000_000, available_bytes: 10_000_000_000, used_bytes: 6_000_000_000, percent: 37.5, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'alive',
            status_file_age_s: 660.0,
            observed_at: iso(6),
            reason: 'stale_status_file',
          },
        ],
      }),
    });
    await hub.monitoringService.handleHostHealthObservation(reqStale2, auth);
    const episodesAfter = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodesAfter).toHaveLength(1);

    // 4. Worker broken -> escalates state to broken/critical
    const reqBroken = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(4),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 16_000_000_000, available_bytes: 10_000_000_000, used_bytes: 6_000_000_000, percent: 37.5, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'broken',
            status_file_age_s: 670.0,
            observed_at: iso(4),
            reason: 'worker_broken',
          },
        ],
      }),
    });
    await hub.monitoringService.handleHostHealthObservation(reqBroken, auth);
    summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.needs_attention.some((n: any) => n.subject === 'hetzner:supervision' && n.severity === 'critical')).toBe(true);

    // 5. Recovery -> resolves episode, clears alert, removes from needs_attention
    const reqRecovered = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(2),
        load: { load1: 0.5, load5: 0.8, load15: 0.9 },
        memory: { total_bytes: 16_000_000_000, available_bytes: 10_000_000_000, used_bytes: 6_000_000_000, percent: 37.5, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'alive',
            status_file_age_s: 15.0,
            observed_at: iso(2),
            reason: 'ok',
          },
        ],
      }),
    });
    const resRecovered = await hub.monitoringService.handleHostHealthObservation(reqRecovered, auth);
    expect(resRecovered.status).toBe(200);

    const episodesResolved = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodesResolved).toHaveLength(1);
    expect(episodesResolved[0].resolved_at).not.toBeNull();

    // Alert cleared in liveness
    const alertsRecovered = hub.livenessService.alerts();
    expect(alertsRecovered.some((a: any) => a.subject_kind === 'supervisor' && a.subject === 'hetzner:supervision' && a.resolved_at === null)).toBe(false);

    // needs_attention cleared
    summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.needs_attention.some((n: any) => n.subject === 'hetzner:supervision')).toBe(false);
  }));

  it('rejects invalid supervisor service payloads and handles missing/future data honestly', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Invalid worker state (not alive/broken/unknown)
    const reqBadWorker = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(5),
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [{ service_id: 'supervision', worker: 'running' }],
      }),
    });
    const resBadWorker = await hub.monitoringService.handleHostHealthObservation(reqBadWorker, auth);
    expect(resBadWorker.status).toBe(400);

    // 2. Missing/null status file age is accepted honestly (never healthy zero) and opens alert
    const reqMissing = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(5),
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          {
            service_id: 'supervision',
            worker: 'unknown',
            status_file_age_s: null,
            observed_at: null,
            reason: 'missing_status_file',
          },
        ],
      }),
    });
    const resMissing = await hub.monitoringService.handleHostHealthObservation(reqMissing, auth);
    expect(resMissing.status).toBe(200);

    const summary = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary.host_health[0].services![0].status_file_age_s).toBeNull();
    expect(summary.needs_attention.some((n: any) => n.subject === 'hetzner:supervision')).toBe(true);

    // 3. Backward compatibility: win35 offline or payload without services causes no alert
    enrollHost(hub.sql, 'win35');
    const authWin = { kind: 'host' as const, machine_id: 'win35', tokenId: 'cred-win35' };
    const reqWin = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'win35',
        observed_at: iso(5),
        load: { load1: 0.1, load5: 0.1, load15: 0.1 },
        memory: { total_bytes: 8000, available_bytes: 4000, used_bytes: 4000, percent: 50, status: 'ok' },
      }),
    });
    const resWin = await hub.monitoringService.handleHostHealthObservation(reqWin, authWin);
    expect(resWin.status).toBe(200);

    const summaryWin = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryWin.needs_attention.some((n: any) => n.subject.startsWith('win35'))).toBe(false);
  }));

  it('rejects duplicate service_id, validates future timestamps, and derives effective source age on stale host checkin', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Rejection of duplicate service_id (prevents hiding failures with duplicate entries)
    const reqDup = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(5),
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 10, observed_at: iso(5), reason: 'ok' },
          { service_id: 'supervision', worker: 'broken', status_file_age_s: 10, observed_at: iso(5), reason: 'worker_broken' },
        ],
      }),
    });
    const resDup = await hub.monitoringService.handleHostHealthObservation(reqDup, auth);
    expect(resDup.status).toBe(400);
    const dupBody = (await resDup.json()) as any;
    expect(dupBody.error).toBe('duplicate_service_id');

    // 2. Future service observed_at (> now + 60s) is degraded to unknown / future_observation_timestamp
    const futureIso = new Date(clock.t + 120000).toISOString();
    const reqFuture = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(2),
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 10, observed_at: futureIso, reason: 'ok' },
        ],
      }),
    });
    const resFuture = await hub.monitoringService.handleHostHealthObservation(reqFuture, auth);
    expect(resFuture.status).toBe(200);

    const summaryFuture = hub.monitoringService.getStatusSummary(clock.t);
    const svcFuture = summaryFuture.host_health[0].services![0];
    expect(svcFuture.worker).toBe('unknown');
    expect(svcFuture.status_file_age_s).toBeNull();
    expect(svcFuture.reason).toBe('future_observation_timestamp');
    expect(summaryFuture.needs_attention.some((n: any) => n.subject === 'hetzner:supervision')).toBe(true);

    // 3. Invented healthy state with null timestamp (age 0, 1, or 60) is degraded to unknown / missing_observation_timestamp
    const reqZero = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(2),
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 1.0, observed_at: null, reason: 'ok' },
        ],
      }),
    });
    const resZero = await hub.monitoringService.handleHostHealthObservation(reqZero, auth);
    expect(resZero.status).toBe(200);

    const summaryZero = hub.monitoringService.getStatusSummary(clock.t);
    const svcZero = summaryZero.host_health[0].services![0];
    expect(svcZero.worker).toBe('unknown');
    expect(svcZero.reason).toBe('missing_observation_timestamp');

    // 4. Old source timestamp + reported age 0 on fresh host report:
    // Derives conservative source age from (now - source_observed_at), flagging stale_status_file
    const oldSourceIso = new Date(clock.t - 700000).toISOString(); // 700s old source
    const reqOldSource = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: iso(0), // fresh host report right now
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 0.0, observed_at: oldSourceIso, reason: 'ok' },
        ],
      }),
    });
    const resOldSource = await hub.monitoringService.handleHostHealthObservation(reqOldSource, auth);
    expect(resOldSource.status).toBe(200);

    const summaryOldSource = hub.monitoringService.getStatusSummary(clock.t);
    const svcOldSource = summaryOldSource.host_health[0].services![0];
    expect(svcOldSource.status_file_age_s).toBeGreaterThanOrEqual(700.0);
    expect(svcOldSource.reason).toBe('stale_status_file');
    expect(summaryOldSource.needs_attention.some((n: any) => n.subject === 'hetzner:supervision')).toBe(true);

    // 5. Stale host check-in: effective age increases with elapsed time since observation
    // Record a healthy observation 15 minutes ago (900 seconds)
    const pastObsIso = new Date(clock.t - 900000).toISOString();
    const reqPast = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: pastObsIso,
        load: { load1: 1, load5: 1, load15: 1 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 10.0, observed_at: pastObsIso, reason: 'ok' },
        ],
      }),
    });
    const resPast = await hub.monitoringService.handleHostHealthObservation(reqPast, auth);
    expect(resPast.status).toBe(200);

    // Current query at clock.t (900s elapsed since host observation):
    const healthStatus = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostHetzner = healthStatus.machines.find(m => m.machine_id === 'hetzner');
    expect(hostHetzner).toBeDefined();
    expect(hostHetzner!.freshness).toBe('stale');

    // Effective service age = 10.0 + 900 = 910s (> 600s threshold)
    const svcHetzner = hostHetzner!.services![0];
    expect(svcHetzner.status_file_age_s).toBe(910.0);
    expect(svcHetzner.reason).toBe('stale_status_file');

    // Appears in needs_attention because supervisor is stale
    const summaryStale = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryStale.needs_attention.some((n: any) => n.subject === 'hetzner:supervision')).toBe(true);
  }));

  it('does not resolve durable supervisor episode on delayed healthy report and resolves only on genuine fresh report', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Create broken episode (worker: broken) -> opens alert and episode
    const reqBroken = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'broken', status_file_age_s: 10.0, observed_at: new Date(clock.t).toISOString(), reason: 'worker_broken' },
        ],
      }),
    });
    const resBroken = await hub.monitoringService.handleHostHealthObservation(reqBroken, auth);
    expect(resBroken.status).toBe(200);

    const episodesBroken = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodesBroken).toHaveLength(1);
    expect(episodesBroken[0].state).toBe('broken');
    expect(episodesBroken[0].resolved_at).toBeNull();
    let alerts = hub.livenessService.alerts();
    expect(alerts.some((a: any) => a.subject_kind === 'supervisor' && a.subject === 'hetzner:supervision' && a.state === 'broken' && a.resolved_at === null)).toBe(true);

    // 2. Post delayed "healthy" report: outer report is >600s old (e.g. 700s old) with equally old source timestamp, reported age 0, worker alive, reason ok
    const delayedObsIso = new Date(clock.t - 700000).toISOString();
    const reqDelayed = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: delayedObsIso,
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 0.0, observed_at: delayedObsIso, reason: 'ok' },
        ],
      }),
    });
    const resDelayed = await hub.monitoringService.handleHostHealthObservation(reqDelayed, auth);
    expect(resDelayed.status).toBe(200);

    // Persisted episode and alert MUST remain open! State transitions to stale.
    const episodesAfterDelayed = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodesAfterDelayed).toHaveLength(1);
    expect(episodesAfterDelayed[0].resolved_at).toBeNull();
    expect(episodesAfterDelayed[0].state).toBe('stale');

    alerts = hub.livenessService.alerts();
    expect(alerts.some((a: any) => a.subject_kind === 'supervisor' && a.subject === 'hetzner:supervision' && a.resolved_at === null)).toBe(true);

    // Status GET projects stale
    const healthStatus = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostHetzner = healthStatus.machines.find(m => m.machine_id === 'hetzner');
    expect(hostHetzner!.freshness).toBe('stale');
    expect(hostHetzner!.services![0].reason).toBe('stale_status_file');
    expect(hostHetzner!.services![0].status_file_age_s).toBeGreaterThanOrEqual(700.0);

    // 3. Genuine fresh healthy report arrives (fresh outer, fresh source, worker alive, reason ok)
    const freshObsIso = new Date(clock.t).toISOString();
    const reqFresh = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: freshObsIso,
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        services: [
          { service_id: 'supervision', worker: 'alive', status_file_age_s: 10.0, observed_at: freshObsIso, reason: 'ok' },
        ],
      }),
    });
    const resFresh = await hub.monitoringService.handleHostHealthObservation(reqFresh, auth);
    expect(resFresh.status).toBe(200);

    // Resolves once!
    const episodesResolved = hub.sql.exec("SELECT * FROM supervisor_episodes WHERE episode_key = 'supervisor:hetzner:supervision'").toArray() as any[];
    expect(episodesResolved).toHaveLength(1);
    expect(episodesResolved[0].resolved_at).not.toBeNull();

    alerts = hub.livenessService.alerts();
    expect(alerts.some((a: any) => a.subject_kind === 'supervisor' && a.subject === 'hetzner:supervision' && a.resolved_at === null)).toBe(false);
  }));

  it('accepts host health observation with cloud budget, independently verifies status, and projects into status/liveness', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Post observation with spend exceeding $5.00 cap. Client claims known_within_budget.
    // Server must reject false client status and independently derive over_budget.
    const reqOver = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          known_month_to_date_usd: '6.20',
          coverage_complete: true,
          unknown_services: [],
          budget_status: 'known_within_budget', // Falsified client status
          reason: 'spoofed_ok',
          services: [
            { service: 'cloudflare', month_to_date_usd: '1.20' },
            { service: 'hetzner', month_to_date_usd: '5.00' },
          ],
        },
      }),
    });
    const resOver = await hub.monitoringService.handleHostHealthObservation(reqOver, auth);
    expect(resOver.status).toBe(200);

    // Verify projection in getHostHealthStatus
    const healthStatus1 = hub.monitoringService.getHostHealthStatus(clock.t);
    const host1 = healthStatus1.machines.find(m => m.machine_id === 'hetzner');
    expect(host1?.budget).toBeDefined();
    expect(host1!.budget!.budget_status).toBe('over_budget');
    expect(host1!.budget!.reason).toBe('known_spend_exceeds_budget');
    expect(host1!.budget!.known_month_to_date_usd).toBe('6.20');

    // Verify critical liveness alert
    const alerts1 = hub.livenessService.alerts();
    const budgetAlert1 = alerts1.find((a: any) => a.subject_kind === 'budget' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(budgetAlert1).toBeDefined();
    expect(budgetAlert1!.state).toBe('critical');

    // Verify critical needs_attention item in getStatusSummary
    const summary1 = hub.monitoringService.getStatusSummary(clock.t);
    const attentionBudget1 = summary1.needs_attention.find((n: any) => n.subject === 'hetzner:budget');
    expect(attentionBudget1).toBeDefined();
    expect(attentionBudget1.severity).toBe('critical');
    expect(attentionBudget1.budget_status).toBe('over_budget');

    // 2. Post updated observation within budget cap with authentic source observation time
    const reqWithin = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          observed_at: new Date(clock.t - 30000).toISOString(),
          billing_period: '2026-10',
          coverage_complete: true,
          unknown_services: [],
          services: [
            { service: 'cloudflare', month_to_date_usd: '0.00' },
            { service: 'hetzner', month_to_date_usd: '2.50' },
          ],
        },
      }),
    });
    const resWithin = await hub.monitoringService.handleHostHealthObservation(reqWithin, auth);
    expect(resWithin.status).toBe(200);

    const healthStatus2 = hub.monitoringService.getHostHealthStatus(clock.t);
    const host2 = healthStatus2.machines.find(m => m.machine_id === 'hetzner');
    expect(host2!.budget!.budget_status).toBe('known_within_budget');
    expect(host2!.budget!.known_month_to_date_usd).toBe('2.50');
    expect(host2!.budget!.observed_at).toBeDefined();
    expect(host2!.budget!.billing_period).toBe('2026-10');
    expect(host2!.budget!.source_age_s).toBeGreaterThanOrEqual(30.0);

    // Liveness alert should now be cleared
    const alerts2 = hub.livenessService.alerts();
    const budgetAlert2 = alerts2.find((a: any) => a.subject_kind === 'budget' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(budgetAlert2).toBeUndefined();

    // needs_attention should have no budget entry
    const summary2 = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary2.needs_attention.some((n: any) => n.subject === 'hetzner:budget')).toBe(false);

    // 2b. Config flag alone without source observation time CANNOT establish known_within_budget!
    const reqNoSourceTime = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          coverage_complete: true,
          unknown_services: [],
          services: [
            { service: 'cloudflare', month_to_date_usd: '0.00' },
            { service: 'hetzner', month_to_date_usd: '2.50' },
          ],
        },
      }),
    });
    const resNoSourceTime = await hub.monitoringService.handleHostHealthObservation(reqNoSourceTime, auth);
    expect(resNoSourceTime.status).toBe(200);
    const healthStatusNoTime = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostNoTime = healthStatusNoTime.machines.find(m => m.machine_id === 'hetzner');
    expect(hostNoTime!.budget!.budget_status).toBe('unknown');
    expect(hostNoTime!.budget!.reason).toBe('missing_source_observation_time');

    // 2c. Stale source observation (>30 days) is rejected as unknown
    const staleSourceIso = new Date(clock.t - 31 * 86400 * 1000).toISOString();
    const reqStaleSource = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          observed_at: staleSourceIso,
          coverage_complete: true,
          unknown_services: [],
          services: [
            { service: 'cloudflare', month_to_date_usd: '0.00' },
            { service: 'hetzner', month_to_date_usd: '2.50' },
          ],
        },
      }),
    });
    const resStaleSource = await hub.monitoringService.handleHostHealthObservation(reqStaleSource, auth);
    expect(resStaleSource.status).toBe(200);
    const healthStatusStale = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostStale = healthStatusStale.machines.find(m => m.machine_id === 'hetzner');
    expect(hostStale!.budget!.budget_status).toBe('unknown');
    expect(hostStale!.budget!.reason).toBe('stale_source_observation');

    // 3. Post observation with missing billing credentials / incomplete coverage
    const reqUnknown = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          coverage_complete: false,
          unknown_services: ['aws_cognito'],
          services: [
            { service: 'cloudflare', month_to_date_usd: '0.00' },
          ],
        },
      }),
    });
    const resUnknown = await hub.monitoringService.handleHostHealthObservation(reqUnknown, auth);
    expect(resUnknown.status).toBe(200);

    const healthStatus3 = hub.monitoringService.getHostHealthStatus(clock.t);
    const host3 = healthStatus3.machines.find(m => m.machine_id === 'hetzner');
    expect(host3!.budget!.budget_status).toBe('unknown');
    expect(host3!.budget!.reason).toBe('incomplete_billing_coverage');

    // Liveness alert is warn
    const alerts3 = hub.livenessService.alerts();
    const budgetAlert3 = alerts3.find((a: any) => a.subject_kind === 'budget' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(budgetAlert3).toBeDefined();
    expect(budgetAlert3!.state).toBe('warn');

    // needs_attention has warn entry
    const summary3 = hub.monitoringService.getStatusSummary(clock.t);
    const attentionBudget3 = summary3.needs_attention.find((n: any) => n.subject === 'hetzner:budget');
    expect(attentionBudget3).toBeDefined();
    expect(attentionBudget3.severity).toBe('warn');
  }));

  it('rejects invalid cloud budget payloads and enforces validation bounds', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    const postBudget = async (budgetObj: any) => {
      return hub.monitoringService.handleHostHealthObservation(
        new Request('https://hub.internal/api/monitoring/health', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            machine_id: 'hetzner',
            observed_at: new Date(clock.t).toISOString(),
            load: { load1: 1.0, load5: 1.0, load15: 1.0 },
            memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
            budget: budgetObj,
          }),
        }),
        auth,
      );
    };

    // Rejects boolean amount
    const resBool = await postBudget({
      budget_cap_usd: '5.00',
      services: [{ service: 'cloudflare', month_to_date_usd: true }],
    });
    expect(resBool.status).toBe(400);

    // Rejects negative amount
    const resNeg = await postBudget({
      budget_cap_usd: '5.00',
      services: [{ service: 'cloudflare', month_to_date_usd: -5.0 }],
    });
    expect(resNeg.status).toBe(400);

    // Rejects duplicate service names
    const resDup = await postBudget({
      budget_cap_usd: '5.00',
      services: [
        { service: 'hetzner', month_to_date_usd: '1.00' },
        { service: 'hetzner', month_to_date_usd: '2.00' },
      ],
    });
    expect(resDup.status).toBe(400);

    // Rejects invalid service name
    const resBadName = await postBudget({
      budget_cap_usd: '5.00',
      services: [{ service: 'bad name with spaces!', month_to_date_usd: '1.00' }],
    });
    expect(resBadName.status).toBe(400);

    // Rejects non-numeric budget cap
    const resBadCap = await postBudget({
      budget_cap_usd: 'not-a-number',
      services: [{ service: 'cloudflare', month_to_date_usd: '0.00' }],
    });
    expect(resBadCap.status).toBe(400);

    // Rejects array budget cap ([] and [5])
    const resArrayCapEmpty = await postBudget({
      budget_cap_usd: [],
      services: [{ service: 'cloudflare', month_to_date_usd: '0.00' }],
    });
    expect(resArrayCapEmpty.status).toBe(400);

    const resArrayCapNum = await postBudget({
      budget_cap_usd: [5],
      services: [{ service: 'cloudflare', month_to_date_usd: '0.00' }],
    });
    expect(resArrayCapNum.status).toBe(400);

    // Rejects array month_to_date_usd ([] and [5])
    const resArrayAmtEmpty = await postBudget({
      budget_cap_usd: '5.00',
      services: [{ service: 'cloudflare', month_to_date_usd: [] }],
    });
    expect(resArrayAmtEmpty.status).toBe(400);

    const resArrayAmtNum = await postBudget({
      budget_cap_usd: '5.00',
      services: [{ service: 'cloudflare', month_to_date_usd: [5] }],
    });
    expect(resArrayAmtNum.status).toBe(400);

    // Rejects string coverage_complete ('false')
    const resStringCoverage = await postBudget({
      budget_cap_usd: '5.00',
      coverage_complete: 'false',
      services: [{ service: 'cloudflare', month_to_date_usd: '1.00' }],
    });
    expect(resStringCoverage.status).toBe(400);
  }));

  it('rejects prior billing period and incomplete expected services from claiming within-budget status', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };
    const nowIso = new Date(clock.t).toISOString(); // 2026-10-18...

    // Prior billing period (2026-09) must fail closed as unknown with prior_billing_period
    const reqPrior = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: nowIso,
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          coverage_complete: true,
          billing_period: '2026-09',
          observed_at: nowIso,
          services: [{ service: 'cloudflare', month_to_date_usd: '2.00' }],
        },
      }),
    });
    const resPrior = await hub.monitoringService.handleHostHealthObservation(reqPrior, auth);
    expect(resPrior.status).toBe(200);

    const getRes = await hub.monitoringService.handleHostHealthGet(
      new Request('https://hub.internal/api/monitoring/health?machine_id=hetzner'),
      auth,
    );
    const bodyPrior: any = await getRes.json();
    const machine = bodyPrior.machines[0];
    expect(machine.budget.budget_status).toBe('unknown');
    expect(machine.budget.reason).toBe('prior_billing_period');
    expect(machine.budget.coverage_complete).toBe(false);

    // Missing expected service must fail closed as unknown with missing_expected_services
    const reqMissingExp = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: nowIso,
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          coverage_complete: true,
          expected_services: ['cloudflare', 'hetzner_cloud'],
          observed_at: nowIso,
          services: [{ service: 'cloudflare', month_to_date_usd: '2.00' }],
        },
      }),
    });
    const resMissingExp = await hub.monitoringService.handleHostHealthObservation(reqMissingExp, auth);
    expect(resMissingExp.status).toBe(200);

    const getResExp = await hub.monitoringService.handleHostHealthGet(
      new Request('https://hub.internal/api/monitoring/health?machine_id=hetzner'),
      auth,
    );
    const bodyExp: any = await getResExp.json();
    const machineExp = bodyExp.machines[0];
    expect(machineExp.budget.budget_status).toBe('unknown');
    expect(machineExp.budget.reason).toBe('missing_expected_services');
    expect(machineExp.budget.unknown_services).toContain('hetzner_cloud');
    expect(machineExp.budget.coverage_complete).toBe(false);
  }));

  it('accepts storage hygiene observations, alerts on soft threshold, and recovers cleanly', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Post storage observation exceeding soft limit (150 MiB >= 128 MiB)
    const reqWarn = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        storage: [
          {
            path: '/home/alexey/git/agent-hub/.local',
            used_bytes: 157_286_400, // 150 MiB
            soft_bytes: 134_217_728, // 128 MiB
            hard_bytes: 268_435_456, // 256 MiB
            state: 'warn',
          },
        ],
      }),
    });
    const resWarn = await hub.monitoringService.handleHostHealthObservation(reqWarn, auth);
    expect(resWarn.status).toBe(200);

    // Check host health projection
    const healthStatus1 = hub.monitoringService.getHostHealthStatus(clock.t);
    const host1 = healthStatus1.machines.find(m => m.machine_id === 'hetzner');
    expect(host1?.storage).toHaveLength(1);
    expect(host1!.storage![0].state).toBe('warn');
    expect(host1!.storage![0].policy).toBe('preserve all archives and unresolved evidence; no automatic deletion');

    // Check liveness alert
    const alerts1 = hub.livenessService.alerts();
    const storageAlert1 = alerts1.find((a: any) => a.subject_kind === 'storage' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(storageAlert1).toBeDefined();
    expect(storageAlert1!.state).toBe('warn');

    // Check getStatusSummary
    const summary1 = hub.monitoringService.getStatusSummary(clock.t);
    const attentionStorage1 = summary1.needs_attention.find((n: any) => n.subject === 'hetzner:storage:/home/alexey/git/agent-hub/.local');
    expect(attentionStorage1).toBeDefined();
    expect(attentionStorage1.severity).toBe('warn');

    // 2. Post storage observation under soft threshold (recovery)
    const reqOk = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        storage: [
          {
            path: '/home/alexey/git/agent-hub/.local',
            used_bytes: 52_428_800, // 50 MiB
            soft_bytes: 134_217_728,
            hard_bytes: 268_435_456,
            state: 'ok',
          },
        ],
      }),
    });
    const resOk = await hub.monitoringService.handleHostHealthObservation(reqOk, auth);
    expect(resOk.status).toBe(200);

    // Storage alert cleared
    const alerts2 = hub.livenessService.alerts();
    const storageAlert2 = alerts2.find((a: any) => a.subject_kind === 'storage' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(storageAlert2).toBeUndefined();

    // needs_attention has no storage item
    const summary2 = hub.monitoringService.getStatusSummary(clock.t);
    expect(summary2.needs_attention.some((n: any) => n.subject.startsWith('hetzner:storage'))).toBe(false);

    // 3. Validation bounds: inverted thresholds hard_bytes < soft_bytes fails with 400
    const reqInverted = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        storage: [
          {
            path: '/home/alexey/git/agent-hub/.local',
            used_bytes: 100,
            soft_bytes: 500,
            hard_bytes: 200, // Invalid: hard < soft!
          },
        ],
      }),
    });
    const resInverted = await hub.monitoringService.handleHostHealthObservation(reqInverted, auth);
    expect(resInverted.status).toBe(400);

    // Negative used_bytes fails with 400
    const reqNegative = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        storage: [
          {
            path: '/home/alexey/git/agent-hub/.local',
            used_bytes: -1,
            soft_bytes: 100,
            hard_bytes: 200,
          },
        ],
      }),
    });
    const resNegative = await hub.monitoringService.handleHostHealthObservation(reqNegative, auth);
    expect(resNegative.status).toBe(400);
  }));

  it('evaluates exact decimal arithmetic, clock-advanced budget recomputation, and storage scan truncation alerts', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'hetzner');
    const auth = { kind: 'host' as const, machine_id: 'hetzner', tokenId: 'cred-hetzner' };

    // 1. Exact decimal comparison: 0.10 + 0.20 against cap 0.30 is exactly equal, NOT over budget
    const reqExact = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '0.30',
          observed_at: new Date(clock.t).toISOString(),
          billing_period: '2026-10',
          coverage_complete: true,
          services: [
            { service: 'service_a', month_to_date_usd: '0.10' },
            { service: 'service_b', month_to_date_usd: '0.20' },
          ],
        },
      }),
    });
    const resExact = await hub.monitoringService.handleHostHealthObservation(reqExact, auth);
    expect(resExact.status).toBe(200);
    const healthExact = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostExact = healthExact.machines.find(m => m.machine_id === 'hetzner');
    expect(hostExact!.budget!.budget_status).toBe('known_within_budget');
    expect(hostExact!.budget!.known_month_to_date_usd).toBe('0.30');

    // 1b. Exact decimal comparison: 5.0000000000000001 against cap 5.00 is over_budget
    const reqMicroOver = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          observed_at: new Date(clock.t).toISOString(),
          billing_period: '2026-10',
          coverage_complete: true,
          services: [
            { service: 'service_a', month_to_date_usd: '5.0000000000000001' },
          ],
        },
      }),
    });
    const resMicroOver = await hub.monitoringService.handleHostHealthObservation(reqMicroOver, auth);
    expect(resMicroOver.status).toBe(200);
    const healthMicroOver = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostMicroOver = healthMicroOver.machines.find(m => m.machine_id === 'hetzner');
    expect(hostMicroOver!.budget!.budget_status).toBe('over_budget');
    expect(hostMicroOver!.budget!.reason).toBe('known_spend_exceeds_budget');

    // 2. Projected budget recomputation when advancing clock:
    // First post a fresh within_budget observation
    const reqFresh = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        budget: {
          budget_cap_usd: '5.00',
          observed_at: new Date(clock.t).toISOString(),
          billing_period: '2026-10',
          coverage_complete: true,
          services: [
            { service: 'cloudflare', month_to_date_usd: '2.00' },
          ],
        },
      }),
    });
    await hub.monitoringService.handleHostHealthObservation(reqFresh, auth);
    const healthBefore = hub.monitoringService.getHostHealthStatus(clock.t);
    expect(healthBefore.machines[0].budget!.budget_status).toBe('known_within_budget');

    // Advance clock past freshness threshold (301 seconds)
    clock.t += 301 * 1000;
    const healthAfter = hub.monitoringService.getHostHealthStatus(clock.t);
    const hostAfter = healthAfter.machines[0];
    expect(hostAfter.freshness).toBe('stale');
    expect(hostAfter.budget!.budget_status).toBe('unknown');
    expect(hostAfter.budget!.reason).toBe('host_observation_stale');
    expect(hostAfter.budget!.coverage_complete).toBe(false);

    // needs_attention reflects this
    const summaryAfter = hub.monitoringService.getStatusSummary(clock.t);
    expect(summaryAfter.needs_attention.some((n: any) => n.subject === 'hetzner:budget')).toBe(true);

    // 3. Storage hygiene truncation alert
    const reqTruncated = new Request('https://hub.internal/api/monitoring/health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        machine_id: 'hetzner',
        observed_at: new Date(clock.t).toISOString(),
        load: { load1: 1.0, load5: 1.0, load15: 1.0 },
        memory: { total_bytes: 1000, available_bytes: 500, used_bytes: 500, percent: 50, status: 'ok' },
        storage: [
          {
            path: '/home/alexey/git/agent-hub/.local',
            used_bytes: 1024,
            soft_bytes: 10000,
            hard_bytes: 20000,
            state: 'warn',
            truncated: true,
          },
        ],
      }),
    });
    const resTruncated = await hub.monitoringService.handleHostHealthObservation(reqTruncated, auth);
    expect(resTruncated.status).toBe(200);

    const alertsTruncated = hub.livenessService.alerts();
    const storageAlert = alertsTruncated.find((a: any) => a.subject_kind === 'storage' && a.subject === 'hetzner' && a.resolved_at === null);
    expect(storageAlert).toBeDefined();
    expect(storageAlert!.detail).toContain('Storage scan truncated on hetzner');

    const summaryTruncated = hub.monitoringService.getStatusSummary(clock.t);
    const storageAttention = summaryTruncated.needs_attention.find((n: any) => n.subject.startsWith('hetzner:storage'));
    expect(storageAttention).toBeDefined();
    expect(storageAttention!.detail).toContain('Storage scan truncated');
  }));
});


