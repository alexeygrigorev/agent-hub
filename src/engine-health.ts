/** The engine-health predicate placement uses, shared with the routing views.
 *
 * plan() (src/fleet.ts) blocks a route when an unavailable fleet_engine_health
 * row matches the route's engine on either scope: the route's machine or the
 * global ''/NULL machine_id. The hold reason engine_tool_permission_refused
 * blocks headless work only — interactive launches ignore it — and retriable
 * holds pass through after the retry cooldown (one retrial may pass; a
 * failure re-marks the row with a fresh updated_at). Routing/status views
 * must answer with this same predicate, exposing reason and the affected
 * mode per runner, instead of a bare engine-global lookup (intake 056a4131
 * AC2). */
import { ENGINE_HEALTH_RETRY_MS, RETRIABLE_ENGINE_HOLDS } from './fleet';

/** The full stored row shape (fleet_engine_health). */
export interface EngineHealthRow { engine: string; machine_id?: string | null; status: string; reason: string; updated_at: string }

/** Rows plan() would consult for one (machine, engine): machine-scoped plus global. */
export function healthRowsFor<T extends Pick<EngineHealthRow, 'engine' | 'machine_id'>>(rows: T[], engine: string, machineId: string): T[] {
  return rows.filter(r => r.engine === engine && (r.machine_id === machineId || r.machine_id === '' || r.machine_id == null));
}

/** The row plan()'s find() settles on for this launch mode (same ordering: input order).
 * Missing/unparseable updated_at never passes the cooldown (Date.parse -> NaN), like plan(). */
export function blockingHealthRow<T extends Pick<EngineHealthRow, 'reason' | 'updated_at'>>(rows: T[], mode: string, now: number): T | undefined {
  return rows.find(h => (mode === 'headless' || h.reason !== 'engine_tool_permission_refused') &&
    !(RETRIABLE_ENGINE_HOLDS.has(h.reason) && now - Date.parse(h.updated_at) >= ENGINE_HEALTH_RETRY_MS));
}

/** Engines whose (machine, global) rows block this launch mode on one machine. */
export function blockedEngines<T extends Pick<EngineHealthRow, 'engine' | 'machine_id' | 'reason' | 'updated_at'>>(rows: T[], machineId: string, mode: string, now: number): Set<string> {
  const blocked = new Set<string>();
  for (const r of rows) {
    if (!r.engine || blocked.has(r.engine)) continue;
    if (blockingHealthRow(healthRowsFor(rows, r.engine, machineId), mode, now)) blocked.add(r.engine);
  }
  return blocked;
}

export type RunnerHealthMode = 'headless' | 'interactive' | 'all';

/** Display verdict for one advertised runner: plan()'s predicate per launch mode.
 * available/health go false/0 only when every launch mode is blocked; a
 * partial hold stays placeable but its reason and affected mode are exposed
 * ('all' when every launch mode is blocked). */
export function runnerHealth(rows: Pick<EngineHealthRow, 'reason' | 'updated_at'>[], now: number): { available: boolean; health: 0 | 1; reason: string | null; mode: RunnerHealthMode | null } {
  const headless = blockingHealthRow(rows, 'headless', now);
  const interactive = blockingHealthRow(rows, 'interactive', now);
  const fullyBlocked = Boolean(headless && interactive);
  const blocked = headless ?? interactive;
  const mode: RunnerHealthMode | null = headless && interactive ? 'all' : headless ? 'headless' : interactive ? 'interactive' : null;
  return { available: !fullyBlocked, health: fullyBlocked ? 0 : 1, reason: blocked?.reason ?? null, mode };
}
