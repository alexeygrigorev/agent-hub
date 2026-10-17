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

  it('handles honest capacity refusals and recovers orphan tasks when slots free', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'host-a');
    const auth = { kind: 'host' as const, machine_id: 'host-a', tokenId: 'cred-host-a' };

    // Fill intake_tasks up to capacity (256 tasks)
    for (let i = 0; i < 256; i++) {
      const dummyId = `dummy-task-${i.toString().padStart(4, '0')}`;
      hub.sql.exec(
        'INSERT INTO intake_tasks(id, data) VALUES(?, ?)',
        dummyId,
        JSON.stringify({ id: dummyId, project: 'demo', title: `Dummy ${i}`, status: 'open', version: 1, created_at: clock.t, updated_at: clock.t, history: [] }),
      );
    }

    // Post disk pressure observation with /data at 90% (warn)
    const postObs = () =>
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

    const res1 = await postObs();
    expect(res1.status).toBe(200);

    // Episode should be open, but intake_task_id is null due to honest capacity refusal
    const ep1 = hub.sql.exec('SELECT * FROM disk_episodes WHERE episode_key = "disk:host-a:/data"').toArray()[0] as any;
    expect(ep1).toBeTruthy();
    expect(ep1.state).toBe('warn');
    expect(ep1.intake_task_id).toBeNull();
    expect(ep1.details_json).toContain('capacity_refused');

    // Free up 1 slot in intake_tasks
    hub.sql.exec('DELETE FROM intake_tasks WHERE id = "dummy-task-0000"');

    // Next poll arrives (60s later)
    clock.t += 60 * 1000;
    const res2 = await postObs();
    expect(res2.status).toBe(200);

    // Episode now has a recovered intake task (no permanent orphan!)
    const ep2 = hub.sql.exec('SELECT * FROM disk_episodes WHERE episode_key = "disk:host-a:/data"').toArray()[0] as any;
    expect(ep2.intake_task_id).toBeTruthy();
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
});
