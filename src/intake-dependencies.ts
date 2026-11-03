import type { SqlStorage } from '@cloudflare/workers-types';
import { intakeHasDoneEvidence } from './intake';

type Graph = { depends_on: string[]; blocked_by: string[]; dependencies: Obj[] };
type Obj = Record<string, any>;
const CHUNK = 64;
const rowsOf = (sql: SqlStorage, query: string, ...args: any[]): Obj[] => sql.exec(query, ...args).toArray() as Obj[];

/** Directed intake prerequisites. All mutations run inside the intake transaction.
 * Reads are batched (two indexed probes per 64 tasks, never one query per task) and a failed
 * lookup throws: callers that gate a start must treat the throw as "blocked", never as "free". */
export class IntakeDependencies {
  constructor(private sql: SqlStorage) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_dependencies (
      task_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
      created_at INTEGER NOT NULL, actor TEXT NOT NULL,
      PRIMARY KEY(task_id, dependency_id), CHECK(task_id <> dependency_id))`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_dependencies_reverse ON intake_dependencies(dependency_id, task_id)');
    // Parents currently held by their graph: the watchdog reconciles only these (no corpus scan).
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_dependency_blocked(task_id TEXT PRIMARY KEY, since INTEGER NOT NULL)');
    // One shared prerequisite per blocker key (same blocker for several parents -> one intake).
    this.sql.exec('CREATE TABLE IF NOT EXISTS intake_blocker_keys(blocker_key TEXT PRIMARY KEY, prerequisite_id TEXT NOT NULL, created_at INTEGER NOT NULL)');
  }

  add(taskId: string, dependencyId: string, actor: string) {
    if (taskId === dependencyId) throw new Error('dependency_cycle');
    const cycle = this.sql.exec(`WITH RECURSIVE reachable(id) AS (
      SELECT dependency_id FROM intake_dependencies WHERE task_id=?
      UNION SELECT d.dependency_id FROM intake_dependencies d JOIN reachable r ON d.task_id=r.id
    ) SELECT 1 FROM reachable WHERE id=? LIMIT 1`, dependencyId, taskId).toArray();
    if (cycle.length) throw new Error('dependency_cycle');
    this.sql.exec('INSERT OR IGNORE INTO intake_dependencies VALUES(?,?,?,?)', taskId, dependencyId, Date.now(), actor);
  }

  remove(taskId: string, dependencyId: string) {
    this.sql.exec('DELETE FROM intake_dependencies WHERE task_id=? AND dependency_id=?', taskId, dependencyId);
  }

  /** Graphs for many tasks in O(ceil(n/64)) queries. Tasks without edges map to an empty graph. */
  viewMany(ids: string[]): Map<string, Graph> {
    const out = new Map<string, Graph>();
    const unique = [...new Set(ids)];
    for (let i = 0; i < unique.length; i += CHUNK) {
      const part = unique.slice(i, i + CHUNK);
      const edges = rowsOf(this.sql, `SELECT task_id, dependency_id FROM intake_dependencies WHERE task_id IN (${part.map(() => '?').join(',')}) ORDER BY task_id, dependency_id`, ...part);
      const depIds = [...new Set(edges.map(e => String(e.dependency_id)))];
      const tasks = new Map<string, Obj>();
      for (let j = 0; j < depIds.length; j += CHUNK) {
        const dep = depIds.slice(j, j + CHUNK);
        for (const r of rowsOf(this.sql, `SELECT id, data FROM intake_tasks WHERE id IN (${dep.map(() => '?').join(',')})`, ...dep)) tasks.set(String(r.id), JSON.parse(String(r.data)));
      }
      for (const id of part) out.set(id, { depends_on: [], blocked_by: [], dependencies: [] });
      for (const e of edges) {
        const task = tasks.get(String(e.dependency_id)) ?? null;
        const graph = out.get(String(e.task_id))!;
        const item = { id: String(e.dependency_id), title: task?.title ?? null, project: task?.project ?? null, status: task?.status ?? 'missing',
          blocker_followup: task?.created_by?.blocker_followup === true,
          resolved: !!task && task.status !== 'cancelled' && (task.status === 'done' || intakeHasDoneEvidence(task)) };
        graph.dependencies.push(item);
        graph.depends_on.push(item.id);
        if (!item.resolved) graph.blocked_by.push(item.id);
      }
    }
    return out;
  }

  view(taskId: string): Graph { return this.viewMany([taskId]).get(taskId)!; }

  /** Parents that list this task as a prerequisite (reverse-index probe, bounded). */
  dependentsOf(taskId: string): string[] {
    return rowsOf(this.sql, 'SELECT task_id FROM intake_dependencies WHERE dependency_id=? ORDER BY task_id LIMIT 65', taskId).map(r => String(r.task_id));
  }

  markBlocked(taskId: string, now: number) { this.sql.exec('INSERT OR IGNORE INTO intake_dependency_blocked(task_id,since) VALUES(?,?)', taskId, now); }
  clearBlocked(taskId: string) { this.sql.exec('DELETE FROM intake_dependency_blocked WHERE task_id=?', taskId); }
  blockedParents(limit = 64): string[] {
    return rowsOf(this.sql, 'SELECT task_id FROM intake_dependency_blocked ORDER BY since, task_id LIMIT ?', limit).map(r => String(r.task_id));
  }

  /** Waiting parents (blocked by an unresolved prerequisite) with the blockers' titles, for the web list. */
  waiting(limit = 100): Obj[] {
    const ids = this.blockedParents(limit);
    const graphs = this.viewMany(ids);
    const parents = new Map<string, Obj>();
    for (let i = 0; i < ids.length; i += CHUNK) {
      const part = ids.slice(i, i + CHUNK);
      for (const r of rowsOf(this.sql, `SELECT id, json_extract(data,'$.title') AS title, json_extract(data,'$.project') AS project FROM intake_tasks WHERE id IN (${part.map(() => '?').join(',')})`, ...part)) parents.set(String(r.id), r);
    }
    return ids.map(id => ({ id, title: parents.get(id)?.title ?? null, project: parents.get(id)?.project ?? null,
      blocked_by: graphs.get(id)!.blocked_by, dependencies: graphs.get(id)!.dependencies.filter(d => !d.resolved) }));
  }

  prerequisiteForBlocker(blockerKey: string): string | null {
    const r = rowsOf(this.sql, 'SELECT prerequisite_id FROM intake_blocker_keys WHERE blocker_key=?', blockerKey)[0];
    return r ? String(r.prerequisite_id) : null;
  }
  rememberBlocker(blockerKey: string, prerequisiteId: string, now: number) {
    this.sql.exec(`INSERT INTO intake_blocker_keys(blocker_key,prerequisite_id,created_at) VALUES(?,?,?)
      ON CONFLICT(blocker_key) DO UPDATE SET prerequisite_id=excluded.prerequisite_id,created_at=excluded.created_at`, blockerKey, prerequisiteId, now);
  }
}

/** Stable, machine-independent key for a placement hold: the same blocker on several parents shares one prerequisite. */
export function blockerKeyFromHold(hold: Obj | null | undefined): string | null {
  if (!hold || typeof hold.error !== 'string') return null;
  const reasons = [...new Set((Array.isArray(hold.rejected) ? hold.rejected : []).map((r: Obj) => String(r?.reason ?? '')).filter(Boolean))].sort();
  return ('hold:' + hold.error + (reasons.length ? ':' + reasons.join('+') : '')).replace(/[^A-Za-z0-9._:+-]/g, '_').slice(0, 160);
}
