import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { readPlacementPressure, PLACEMENT_SAMPLE_LIMIT } from '../src/placement-pressure';

const NOW = Date.parse('2026-11-03T21:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();
const admin: any = { caller: { kind: 'human', actor: 'test', isAdmin: true }, scopes: ['admin'] };

function measured(sql: any) {
  const cursors: any[] = [];
  const statements: string[] = [];
  return { sql: { exec(query: string, ...bindings: any[]) {
    const c = sql.exec(query, ...bindings); cursors.push(c); statements.push(query); return c;
  } } as any, rows: () => cursors.reduce((n, c) => n + Number(c.rowsRead), 0), statements };
}

function launch(sql: any, id: string, state: string, updated: string, reason: string | null) {
  sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at,exit_reason)
    VALUES(?,'test','human','test',?,'{}','host','codex','test','headless','/w','x',?,?,?,?)`, id, id, state, updated, updated, reason);
}

const fixture = (fn: (hub: any, sql: any) => Promise<void>) => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('cf-pressure-' + crypto.randomUUID()));
  return (runInDurableObject as any)(stub, (hub: any, state: any) => fn(hub, state.storage.sql));
};

describe('placement pressure observes bounded indexed candidates', () => {
  it('reports partial counts honestly and reads at most a fixed sample despite retained failures', () => fixture(async (hub, raw) => {
    const recent = iso(NOW - 60_000), old = iso(NOW - 3 * 86_400_000);
    for (let i = 0; i < 1200; i++) {
      launch(raw, 'old-' + i, 'failed', old, 'launcher_memory_reserve');
      launch(raw, 'recent-' + i, 'failed', recent, i % 2 ? 'launcher_memory_reserve' : 'unrelated');
      raw.exec("INSERT INTO placement_rejections VALUES(?,?,?,'test','[]','test')", 'reject-' + i, recent, NOW - 60_000);
    }
    for (let i = 0; i < 200; i++) launch(raw, 'queued-' + i, 'queued', iso(NOW - 600_000), null);
    const m = measured(raw);
    const sample = readPlacementPressure(m.sql, NOW);
    expect(sample.queuedTotal).toBe(PLACEMENT_SAMPLE_LIMIT);
    expect(sample.queuedHeld).toBe(PLACEMENT_SAMPLE_LIMIT);
    expect(sample.truncated).toEqual({ queued: true, rejections: true, failures: true });
    expect(sample.unknown).toEqual({ queued: false, rejections: false, failures: false });
    expect(m.rows()).toBeLessThanOrEqual((PLACEMENT_SAMPLE_LIMIT + 1) * 5);
    expect(m.statements.every(q => q.includes('LIMIT ?') && q.includes('INDEXED BY'))).toBe(true);
    const fleet = new FleetService(raw, (fn: any) => fn(), () => NOW);
    const overview = fleet.placementOverview();
    expect(overview.queued_total).toBeNull();
    expect(overview.recent_rejects).toBeNull();
    expect(overview.recent_failed).toBeNull();
    expect(overview.observed_lower_bounds.queued_total).toBe(PLACEMENT_SAMPLE_LIMIT);
    const monitoring = (hub.monitoringService as any).readPlacementPressure(NOW);
    expect(monitoring.queuedTotal).toBe(sample.queuedTotal);
    expect(monitoring.truncated).toEqual(sample.truncated);
  }));

  it('bounds before filtering state/reason, even when the latest rows contain no placement failures', () => fixture(async (_hub, raw) => {
    for (let i = 0; i < 1500; i++) launch(raw, 'terminal-' + i, i % 2 ? 'finished' : 'failed', iso(NOW - 1000), 'unrelated');
    const m = measured(raw);
    const sample = readPlacementPressure(m.sql, NOW);
    expect(sample.failedTotal).toBe(0);
    expect(sample.truncated.failures).toBe(true);
    expect(m.rows()).toBeLessThanOrEqual((PLACEMENT_SAMPLE_LIMIT + 1) * 2);
  }));

  it('pages sessions without an exact retained-table count', () => fixture(async (_hub, raw) => {
    for (let i = 0; i < 1600; i++) raw.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('host',?,?,'exited',?,'/w')`, 'n-' + i, 'tag-' + i, iso(NOW));
    const m = measured(raw);
    const fleet = new FleetService(m.sql, (fn: any) => fn(), () => NOW);
    const response = await fleet.handle(new Request('https://hub/api/fleet?sections=sessions&sessions_limit=10'), admin);
    const body: any = await response.json();
    expect(body.sessions).toHaveLength(10);
    expect(body.sessions_total).toBeNull();
    expect(body.sessions_total_unknown).toBe(true);
    expect(body.sessions_truncated).toBe(true);
    expect(m.statements.some(q => /COUNT\(\*\).*fleet_sessions/i.test(q))).toBe(false);
    expect(m.rows()).toBeLessThan(100);
  }));

  it('keeps exact counts for fully observed small samples and marks storage failures unknown', () => fixture(async (_hub, raw) => {
    launch(raw, 'small-failed', 'failed', iso(NOW - 1000), 'account_capacity');
    launch(raw, 'small-queued', 'queued', iso(NOW - 600_000), null);
    const small = readPlacementPressure(raw, NOW);
    expect(small.failedTotal).toBe(1);
    expect(small.queuedHeld).toBe(1);
    expect(Object.values(small.truncated).some(Boolean)).toBe(false);
    const unavailable = readPlacementPressure({ exec() { throw new Error('unavailable'); } } as any, NOW);
    expect(unavailable.unknown).toEqual({ queued: true, rejections: true, failures: true });
  }));
});
