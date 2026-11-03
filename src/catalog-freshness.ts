import type { SqlStorage } from '@cloudflare/workers-types';

type CatalogReport = { at: number; complete: boolean };
const recentReports = new WeakMap<object, Map<string, CatalogReport>>();

/** A successful census request is fresh evidence for this live DO instance.
 * The durable row remains a cold-start fallback and changes only when the
 * host first reports or changes its completeness verdict. */
export function noteCatalogReport(sql: SqlStorage, machine: string, complete: boolean, now: number): void {
  let reports = recentReports.get(sql as object);
  if (!reports) recentReports.set(sql as object, reports = new Map());
  if (reports.size >= 128 && !reports.has(machine)) reports.clear();
  reports.set(machine, { at: now, complete });
}

export function clearCatalogReports(sql: SqlStorage): void {
  recentReports.delete(sql as object);
}

function overlayRecent(sql: SqlStorage, rows: Map<string, number>, now: number, completeOnly: boolean): Map<string, number> {
  const reports = recentReports.get(sql as object);
  if (!reports) return rows;
  const min = now - 180000, max = now + 60000;
  for (const [machine, report] of reports) {
    if (report.at >= min && report.at <= max && (!completeOnly || report.complete)) rows.set(machine, report.at);
    else if (completeOnly && !report.complete) rows.delete(machine);
  }
  return rows;
}

/** A session row's observed_at is its last change, not the last full census.
 * Separate bounded evidence lets unchanged live sessions remain observable.
 */
export function freshCatalogs(sql: SqlStorage, now: number): Map<string, number> {
  try {
    const rows = new Map((sql.exec(`SELECT machine_id,observed_at FROM fleet_catalog_seen
      WHERE observed_at>=? AND observed_at<=?`,
      new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString()).toArray() as any[])
      .map(row => [String(row.machine_id), Date.parse(row.observed_at)]));
    return overlayRecent(sql, rows, now, false);
  } catch { return new Map(); }
}

/** Latest census freshness that is safe for capacity decisions.
 *
 * fleet_catalog_seen tracks whether a host ever sent a report. This separate
 * row records whether it covered every live or ambiguous native; an incomplete
 * newer census invalidates an older complete observation. Successful reports
 * refresh only the live DO's in-memory timestamp; durable rows change on first
 * report or a completeness transition.
 */
export function freshCompleteCatalogs(sql: SqlStorage, now: number, machineIds?: readonly string[]): Map<string, number> {
  try {
    const scope = machineIds?.length ? ` AND machine_id IN (${machineIds.map(() => '?').join(',')})` : '';
    if (machineIds && machineIds.length === 0) return new Map();
    const rows = new Map((sql.exec(`SELECT machine_id,observed_at FROM fleet_catalog_census
      WHERE complete=1 AND observed_at>=? AND observed_at<=?${scope}`,
      new Date(now - 180000).toISOString(), new Date(now + 60000).toISOString(), ...(machineIds ?? [])).toArray() as any[])
      .map(row => [String(row.machine_id), Date.parse(row.observed_at)]));
    const fresh = overlayRecent(sql, rows, now, true);
    return machineIds ? new Map([...fresh].filter(([machine]) => machineIds.includes(machine))) : fresh;
  } catch { return new Map(); }
}
