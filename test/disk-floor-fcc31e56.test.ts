import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { floorStatus, type DiskFloor } from '../src/monitoring';

// Intake fcc31e56 AC6: a floor-managed filesystem alerts only when free space is
// still below the floor after a completed automatic cleanup pass.
const now = Date.parse('2026-10-30T00:00:00Z');
const GIB = 1024 ** 3;
const TOTAL = 436 * GIB;

const floor = (over: Partial<DiskFloor> = {}): DiskFloor => ({
  floor_bytes: 100 * GIB, target_bytes: 200 * GIB, cleanup_at: new Date(now - 60000).toISOString(),
  free_after_cleanup_bytes: 120 * GIB, freed_bytes: 30 * GIB, removed: 12, dry_run: false, ...over,
});

async function fixture(fn: (hub: Hub, clock: { t: number }) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('disk-floor-test-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const clock = { t: now };
    (hub.monitoringService as any).now = () => clock.t;
    (hub.livenessService as any).now = () => clock.t;
    await fn(hub, clock);
  });
}

function enrollHost(sql: any, machineId: string) {
  const at = new Date(now - 86400000).toISOString();
  sql.exec("INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?, ?, ?, ?, ?, 0)",
    machineId, machineId, machineId, at, at);
  sql.exec("INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'host', ?, NULL, ?, ?, ?, NULL, '[\"host\"]', 'admin', ?, ?)",
    'cred-' + machineId, 'hash-' + machineId, 'fam-' + machineId, machineId, machineId, at, new Date(now + 86400000 * 30).toISOString());
}

describe('disk floor status (fcc31e56)', () => {
  it('is ok above the floor regardless of percent', () => {
    expect(floorStatus(150 * GIB, 66, floor(), now)).toBe('ok');
    expect(floorStatus(101 * GIB, 97, floor(), now)).toBe('ok');
  });

  it('is critical only when the last completed pass ended below the floor', () => {
    expect(floorStatus(90 * GIB, 79, floor({ free_after_cleanup_bytes: 90 * GIB }), now)).toBe('critical');
    // Dropped below after a pass that ended above it: the next pass runs first.
    expect(floorStatus(90 * GIB, 79, floor({ free_after_cleanup_bytes: 120 * GIB }), now)).toBe('ok');
  });

  it('stays quiet before a cleanup pass, including severe percent pressure', () => {
    const pending = floor({ cleanup_at: null, free_after_cleanup_bytes: null });
    expect(floorStatus(90 * GIB, 79, pending, now)).toBe('ok');
    expect(floorStatus(10 * GIB, 97, pending, now)).toBe('ok');
  });

  it('does not alert for a dry-run or for a cleanup that ended above the floor', () => {
    expect(floorStatus(90 * GIB, 79, floor({ dry_run: true, free_after_cleanup_bytes: 80 * GIB }), now)).toBe('ok');
    expect(floorStatus(90 * GIB, 79, floor({ free_after_cleanup_bytes: 120 * GIB }), now)).toBe('ok');
  });

  it('opens no informational alert at 90% and alerts once when cleanup cannot restore the floor', () => fixture(async (hub, clock) => {
    enrollHost(hub.sql, 'rmthz');
    const auth = { kind: 'host' as const, machine_id: 'rmthz', tokenId: 'cred-rmthz' };
    const post = (free: number, fl: DiskFloor | undefined) => hub.monitoringService.handleObservation(new Request('https://hub.internal/api/monitoring/disk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ machine_id: 'rmthz', observed_at: new Date(clock.t).toISOString(), filesystems: [{
        mount_point: '/', device_id: 'nvme0', is_primary: true, total_bytes: TOTAL, used_bytes: TOTAL - free,
        free_bytes: free, percent: Math.round(((TOTAL - free) / TOTAL) * 1000) / 10, status: 'warn', ...(fl ? { floor: fl } : {}),
      }] }),
    }), auth);
    const diskAlerts = () => hub.livenessService.alerts().filter((a: any) => a.subject_kind === 'disk' && !a.resolved_at);

    // 90% used but 130G free after cleanup: above the floor, nothing to tell Alexey.
    expect((await post(130 * GIB, floor({ free_after_cleanup_bytes: 130 * GIB }))).status).toBe(200);
    expect(diskAlerts()).toHaveLength(0);
    expect(hub.sql.exec('SELECT * FROM intake_tasks').toArray()).toHaveLength(0);

    // Before the first real cleanup pass, even severe pressure is not a mail.
    clock.t += 60000;
    await post(10 * GIB, floor({ cleanup_at: null, free_after_cleanup_bytes: null }));
    expect(diskAlerts()).toHaveLength(0);

    // Without floor data the same filesystem keeps the legacy percent alert.
    clock.t += 60000;
    await post(40 * GIB, undefined);
    expect(diskAlerts()).toHaveLength(1);
    clock.t += 60000;
    await post(130 * GIB, floor({ cleanup_at: new Date(clock.t).toISOString(), free_after_cleanup_bytes: 130 * GIB }));
    expect(diskAlerts()).toHaveLength(0);

    // Below the floor, cleanup pending/just above: still quiet.
    clock.t += 60000;
    await post(95 * GIB, floor({ cleanup_at: new Date(clock.t - 30000).toISOString(), free_after_cleanup_bytes: 105 * GIB }));
    expect(diskAlerts()).toHaveLength(0);

    // A completed pass that could not restore the floor: one real alert with evidence.
    clock.t += 60000;
    const res = await post(80 * GIB, floor({ cleanup_at: new Date(clock.t - 10000).toISOString(), free_after_cleanup_bytes: 80 * GIB }));
    expect(res.status).toBe(200);
    const open = diskAlerts();
    expect(open).toHaveLength(1);
    expect(open[0].state).toBe('critical');
    expect(open[0].detail).toContain('below the 100.0 GB floor after automatic cleanup');

    // Recovered by later cleanup: alert resolves.
    clock.t += 60000;
    await post(210 * GIB, floor({ cleanup_at: new Date(clock.t).toISOString(), free_after_cleanup_bytes: 210 * GIB }));
    expect(diskAlerts()).toHaveLength(0);
  }));

  it('rejects malformed floor data', () => fixture(async hub => {
    enrollHost(hub.sql, 'rmthz');
    const auth = { kind: 'host' as const, machine_id: 'rmthz', tokenId: 'cred-rmthz' };
    for (const bad of [{ floor_bytes: -1, target_bytes: 1 }, { floor_bytes: 10, target_bytes: 5 }, 'x', { floor_bytes: 10, target_bytes: 20, cleanup_at: 'nope' }]) {
      const res = await hub.monitoringService.handleObservation(new Request('https://hub.internal/api/monitoring/disk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_id: 'rmthz', observed_at: new Date(now).toISOString(), filesystems: [{
          mount_point: '/', device_id: 'nvme0', is_primary: true, total_bytes: 100, used_bytes: 50, free_bytes: 50, percent: 50, floor: bad }] }),
      }), auth);
      expect(res.status).toBe(400);
    }
  }));
});
