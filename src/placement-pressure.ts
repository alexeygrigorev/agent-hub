import type { SqlStorage } from '@cloudflare/workers-types';
import { RowBudgetExceeded } from './row-budget';

export const PLACEMENT_SAMPLE_LIMIT = 128;
export const PLACEMENT_PRESSURE_REASONS = new Set(['launcher_memory_reserve', 'account_capacity', 'account_concurrency', 'machine_capacity', 'cpu_headroom', 'cpu_pressure', 'memory_pressure', 'host_oom', 'host_runtime_choking', 'host_load_high']);

type SourceFlags = { queued: boolean; rejections: boolean; failures: boolean };
export type PlacementPressureSnapshot = {
  // Counts describe only the bounded sample when its flag is set. Public
  // projections must show unknown or an explicitly labelled lower bound.
  queuedTotal: number; queuedHeld: number; recentRejects: number;
  failedTotal: number; failedByMachine: Map<string, number>;
  truncated: SourceFlags; unknown: SourceFlags;
};

/** LIMIT applies to indexed candidates BEFORE parsing diagnostic JSON. Never
 * count or postfilter the retained launch/rejection history inside SQLite. */
export function readPlacementPressure(sql: SqlStorage, now: number): PlacementPressureSnapshot {
  const out: PlacementPressureSnapshot = { queuedTotal: 0, queuedHeld: 0, recentRejects: 0, failedTotal: 0,
    failedByMachine: new Map(), truncated: { queued: false, rejections: false, failures: false },
    unknown: { queued: false, rejections: false, failures: false } };
  const take = PLACEMENT_SAMPLE_LIMIT + 1;
  const failure = (source: keyof SourceFlags, error: unknown) => {
    if (error instanceof RowBudgetExceeded) throw error;
    out.unknown[source] = true;
  };
  try {
    const rows = sql.exec("SELECT created_at FROM fleet_launches INDEXED BY fleet_launch_expiry WHERE state='queued' ORDER BY created_at LIMIT ?", take).toArray() as any[];
    out.truncated.queued = rows.length > PLACEMENT_SAMPLE_LIMIT;
    const sample = rows.slice(0, PLACEMENT_SAMPLE_LIMIT);
    out.queuedTotal = sample.length;
    const heldBefore = new Date(now - 5 * 60_000).toISOString();
    out.queuedHeld = sample.filter(row => String(row.created_at) <= heldBefore).length;
  } catch (error) { failure('queued', error); }
  try {
    const rows = sql.exec('SELECT created_at_ms FROM placement_rejections INDEXED BY idx_placement_rejections_recent WHERE created_at_ms>=? ORDER BY created_at_ms DESC LIMIT ?', now - 86_400_000, take).toArray();
    out.truncated.rejections = rows.length > PLACEMENT_SAMPLE_LIMIT;
    out.recentRejects = Math.min(rows.length, PLACEMENT_SAMPLE_LIMIT);
  } catch (error) { failure('rejections', error); }
  try {
    const rows = sql.exec("SELECT state,machine_id,exit_reason,launch_error_json FROM fleet_launches INDEXED BY fleet_launch_updated WHERE updated_at>=? ORDER BY updated_at DESC LIMIT ?", new Date(now - 86_400_000).toISOString(), take).toArray() as any[];
    out.truncated.failures = rows.length > PLACEMENT_SAMPLE_LIMIT;
    for (const row of rows.slice(0, PLACEMENT_SAMPLE_LIMIT)) {
      if (row.state !== 'failed') continue;
      const reasons = [row.exit_reason];
      try { reasons.push(JSON.parse(row.launch_error_json || 'null')?.reason); } catch { /* malformed diagnostics carry no reason */ }
      if (!reasons.some(reason => PLACEMENT_PRESSURE_REASONS.has(reason))) continue;
      out.failedTotal++;
      if (row.machine_id) out.failedByMachine.set(String(row.machine_id), (out.failedByMachine.get(String(row.machine_id)) ?? 0) + 1);
    }
  } catch (error) { failure('failures', error); }
  return out;
}
