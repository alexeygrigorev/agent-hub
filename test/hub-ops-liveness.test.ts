import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { LivenessService, DEFAULT_THRESHOLDS } from '../src/liveness';
import { FleetService } from '../src/fleet';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const now = Date.parse('2026-10-15T12:00:00Z');
const iso = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();

// Bounded hub-ops reproducer: reuses actual app source + local SQLite only.
// Max 2 selected cases; run with explicit -t filter, ONE guarded invocation.
// Purpose: separate workerd/harness failure (internal error, resource, pipe mask)
// from real assertion failure on current 91b8b6c source (f5efc43 ancestor).

async function fixture(fn: (live: LivenessService, sql: any) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('hubops-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub) => {
    const clock = { t: now };
    new FleetService(hub.sql, fn => fn(), () => clock.t).init();
    const live = new LivenessService(hub.sql, DEFAULT_THRESHOLDS, () => clock.t);
    live.init();
    await fn(live, hub.sql);
  });
}

describe('hub-ops liveness reproducer', () => {
  it('hubops-default-inbox delivers alerts to the grok-bot inbox when HUB_ALERT_TO is unset', async () => {
    const bindings = env as unknown as Env;
    const stub = bindings.HUB.get(bindings.HUB.idFromName('hubops-default-' + crypto.randomUUID()));
    await runInDurableObject(stub, async (hub: Hub) => {
      delete (hub.env as any).HUB_ALERT_TO;
      hub.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('grok-bot', NULL, NULL, NULL)");
      hub.sql.exec(
        "INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES ('m', 'm', 'm', ?, ?, 0)",
        new Date().toISOString(),
        new Date(Date.now() - 3600000).toISOString(),
      );
      await hub.runChecks('liveness');
      const [alert]: any[] = hub.livenessService.alerts();
      expect(alert.notify_error).toBeNull();
      expect(JSON.parse(alert.delivered_to)).toEqual(['inbox:grok-bot']);
    });
  });

  it('hubops-stale-reconcile opens one alert per problem and resolves on recovery', () => fixture((live, sql) => {
    sql.exec(
      'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, drained) VALUES (?, ?, ?, ?, ?, ?)',
      'hetzner', 'hetzner', 'hetzner', iso(86400), iso(300), 0,
    );
    const opened = live.reconcileAlerts(live.evaluate().subjects);
    expect(opened.map(a => [a.subject_kind, a.subject, a.state])).toEqual([['machine', 'hetzner', 'stale']]);
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', iso(10), 'hetzner');
    expect(live.reconcileAlerts(live.evaluate().subjects)).toEqual([]);
    expect(live.alerts()[0].resolved_at).not.toBeNull();
  }));
});
