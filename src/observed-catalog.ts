import type { SqlStorage } from '@cloudflare/workers-types';
import { WriteWatch } from './write-watch';

/**
 * Write-through catalog of observed native sessions (`fleet_sessions`).
 *
 * This table is the hottest read source in the Hub: hosts census every few
 * seconds, and the resource watchdog, liveness evaluation and the offers path
 * all re-read a machine's rows per call. A generation check cannot help -
 * the census writes themselves would invalidate per poll. Instead the census
 * PUT reports the full row it just wrote, so the map IS the table content:
 * every reader sees exactly the durable rows without reading them, and a DO
 * restart lazily reloads each machine once.
 *
 * Every mutation path must go through note/notePhase (census PUT changed
 * rows, watchdog reap phase flips, census unobserved flips). Tests pin this:
 * a write through any other path would serve a stale catalog until restart.
 */
export class ObservedCatalog {
  private byMachine = new Map<string, Map<string, any>>();
  private loadedAll = false;
  private loadedMachinesStamp: string | null = null;
  private machineIds: string[] = [];
  private machineIdsStamp: string | null = null;
  private machineIdsLoaded = false;

  constructor(private sql: SqlStorage, private writes?: WriteWatch) {}

  private copy(row: any): any { return { ...row }; }

  /** Lazy load: one bounded scan per machine (or all machines once). */
  private ensure(machine?: string) {
    if (machine) {
      if (!this.byMachine.has(machine)) {
        const rows = this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ?', machine).toArray() as any[];
        const map = new Map<string, any>();
        for (const row of rows) map.set(String(row.native_id), this.copy(row));
        this.byMachine.set(machine, map);
      }
      return;
    }
    const machinesStamp = this.writes?.stamp('machines') ?? null;
    if (this.loadedAll && machinesStamp === this.loadedMachinesStamp) return;
    if (this.writes) {
      if (!this.machineIdsLoaded || machinesStamp !== this.machineIdsStamp) {
        this.machineIds = (this.sql.exec('SELECT id FROM machines').toArray() as any[]).map(row => String(row.id));
        this.machineIdsStamp = machinesStamp;
        this.machineIdsLoaded = true;
      }
    } else if (!this.machineIdsLoaded) {
      this.machineIds = (this.sql.exec('SELECT id FROM machines').toArray() as any[]).map(row => String(row.id));
      this.machineIdsLoaded = true;
    }
    // Hydrate each machine through the fleet_sessions machine_id index; never
    // scan the full retained session catalog on a cold DO or a machine change.
    for (const id of this.machineIds) this.ensure(id);
    this.loadedAll = true;
    this.loadedMachinesStamp = machinesStamp;
  }

  /** Upsert the durable rows the census just wrote (full row shape). */
  note(machine: string, rows: any[]): void {
    let map = this.byMachine.get(machine);
    if (this.loadedAll || map) {
      if (!map) this.byMachine.set(machine, map = new Map());
      for (const row of rows) map.set(String(row.native_id), this.copy(row));
    }
    // Not loaded yet: nothing to patch; the lazy load reads the table.
  }

  /** Patch one column flip (reap exit, census unobserved). */
  notePhase(machine: string, nativeId: string, phase: string, observedAt: string): void {
    const map = this.byMachine.get(machine);
    const row = map?.get(String(nativeId));
    if (row) { row.phase = phase; row.observed_at = observedAt; }
  }

  /** The durable rows of one machine (or all machines), fresh without reads. */
  rows(machine?: string): any[] {
    this.ensure(machine);
    if (machine) return [...(this.byMachine.get(machine)?.values() ?? [])].map(row => this.copy(row));
    this.ensure();
    return [...this.byMachine.values()].flatMap(map => [...map.values()].map(row => this.copy(row)));
  }

  /** native_key -> row, keyed like the previous per-rebuild SQL map. */
  byNative(machine?: string): Map<string, any> {
    const out = new Map<string, any>();
    for (const row of this.rows(machine)) out.set(JSON.stringify([row.machine_id, row.native_id]), row);
    return out;
  }

  /** native_key -> last_activity_at, the per-read activity overlay. */
  activity(machine?: string): Map<string, any> {
    const out = new Map<string, any>();
    for (const row of this.rows(machine)) out.set(JSON.stringify([row.machine_id, row.native_id]), row.last_activity_at);
    return out;
  }

  /** Durable rows for a raw SQL consumer that expects `SELECT *` order. */
  all(): any[] { return this.rows(); }

  /** Drop cached rows (backup restore swapped the database). */
  reset(): void {
    this.byMachine.clear(); this.loadedAll = false; this.loadedMachinesStamp = null;
    this.machineIds = []; this.machineIdsStamp = null; this.machineIdsLoaded = false;
  }
}
