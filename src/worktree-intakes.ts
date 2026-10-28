/** Destructive worktree cleanup requires a complete, readable intake ledger. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { intakeHasDoneEvidence } from './intake';
import { isIntakeTaskOpen } from './session_lifecycle_registry';

function record(raw: any): any {
  const value = JSON.parse(raw || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid session ledger');
  return value;
}

export type WorktreeIntakeSnapshot = {
  valid: boolean; machines: any[]; projections: any[]; sessions: any[];
  /** Batched isIntakeTaskOpen over the snapshot's own id universe. */
  open: (taskId: unknown) => boolean;
  /** Sessions sharing a native identity on the same hostname's machines. */
  aliasesOf: (nativeId: unknown, hosts: Set<any>) => any[];
};

export function worktreeIntakeSnapshot(sql: SqlStorage): WorktreeIntakeSnapshot {
  try {
    const machines = sql.exec('SELECT id, hostname FROM machines').toArray();
    const projections = sql.exec('SELECT id, links FROM intake_task_projection').toArray().map((row: any) => {
      const links = JSON.parse(row.links);
      if (!Array.isArray(links)) throw new Error('invalid intake link projection');
      return { id: row.id, links };
    });
    const sessions = sql.exec(`SELECT s.id,s.machine_id,s.binding_json,
      CASE WHEN json_valid(s.request_json) THEN json_type(s.request_json)='object' ELSE 0 END AS request_valid,
      CASE WHEN f.request_json IS NULL THEN s.fleet_launch_id IS NULL WHEN json_valid(f.request_json) THEN json_type(f.request_json)='object' ELSE 0 END AS launch_valid,
      json_extract(CASE WHEN json_valid(s.request_json) THEN s.request_json ELSE '{}' END,'$.intake_task_id') AS direct_intake,
      json_extract(CASE WHEN json_valid(f.request_json) THEN f.request_json ELSE '{}' END,'$.intake_task_id') AS launch_intake
      FROM session_control_sessions s LEFT JOIN fleet_launches f ON f.id=s.fleet_launch_id`).toArray().map((row: any) => {
        if (!row.request_valid || !row.launch_valid) throw new Error('invalid session request ledger');
        return { ...row, binding: record(row.binding_json), request: { intake_task_id: row.direct_intake },
          launch: { intake_task_id: row.launch_intake } };
      });
    // One chunked pass evaluates the open/closed verdict for every id in the
    // snapshot's own universe and retains ONLY the boolean: raw task payloads
    // are parsed and released row by row. Retaining (or re-parsing per call)
    // production-scale payloads is a memory and CPU hazard inside the 128 MB
    // isolate, and the resource watchdog renders every session row.
    const ids = new Set<string>();
    for (const projection of projections) ids.add(String(projection.id));
    for (const row of sessions) for (const id of [row.direct_intake, row.launch_intake]) {
      if (typeof id === 'string' && id) ids.add(id);
    }
    const verdicts = new Map<string, boolean>();
    const verdictOf = (task: any): boolean => {
      if (!task || typeof task !== 'object') return true;
      if (task.archived_at != null) return false;
      if (intakeHasDoneEvidence(task)) return false;
      return true;
    };
    const all = [...ids];
    for (let i = 0; i < all.length; i += 90) {
      const chunk = all.slice(i, i + 90);
      for (const row of sql.exec(`SELECT id, data FROM intake_tasks WHERE id IN (${chunk.map(() => '?').join(',')})`, ...chunk).toArray()) {
        if (typeof row.id !== 'string') continue;
        let task: any = null;
        try { task = typeof row.data === 'string' ? JSON.parse(row.data) : row.data; } catch { task = null; }
        verdicts.set(row.id, verdictOf(task));
      }
    }
    const open = (taskId: unknown): boolean => {
      if (typeof taskId !== 'string' || !taskId) return true;
      const cached = verdicts.get(taskId);
      if (cached !== undefined) return cached;
      const fresh = isIntakeTaskOpen(sql, taskId);
      verdicts.set(taskId, fresh);
      return fresh;
    };
    const byNative = new Map<string, any[]>();
    for (const row of sessions) {
      const native = row.binding && row.binding.native_id;
      if (!native) continue;
      const key = String(native);
      const bucket = byNative.get(key);
      if (bucket) bucket.push(row); else byNative.set(key, [row]);
    }
    const aliasesOf = (nativeId: unknown, hosts: Set<any>): any[] => !nativeId ? [] :
      (byNative.get(String(nativeId)) ?? []).filter(row => hosts.has(row.machine_id));
    return { valid: true, machines, projections, sessions, open, aliasesOf };
  } catch {
    return { valid: false, machines: [], projections: [], sessions: [], open: () => true, aliasesOf: () => [] };
  }
}

export function openWorktreeIntake(sql: SqlStorage, session: any, snapshot: WorktreeIntakeSnapshot = worktreeIntakeSnapshot(sql)): string | null {
  if (!snapshot.valid) throw new Error('unreadable intake links');
  if (!session) throw new Error('missing session identity');
  const open = typeof snapshot.open === 'function' ? snapshot.open : (id: unknown) => isIntakeTaskOpen(sql, id);
  const request = record(session.request_json);
  const launch = record(session.launch_request);
  for (const id of [request.intake_task_id, launch.intake_task_id]) {
    if (id !== null && id !== undefined && id !== '' && (typeof id !== 'string' || open(id))) return String(id);
  }
  const binding = record(session.binding_json);
  const machines = snapshot.machines;
  const own = machines.find(row => row.id === session.machine_id);
  const hostname = typeof own?.hostname === 'string' ? own.hostname.trim().toUpperCase() : '';
  const hosts = new Set([session.machine_id, ...machines.filter(row => hostname &&
    typeof row.hostname === 'string' && row.hostname.trim().toUpperCase() === hostname).map(row => row.id)]);
  const aliases = typeof snapshot.aliasesOf === 'function'
    ? snapshot.aliasesOf(binding.native_id, hosts)
    : snapshot.sessions.filter(row => binding.native_id &&
      row.binding.native_id === binding.native_id && hosts.has(row.machine_id));
  const sessionIds = new Set([session.id, ...aliases.map(row => row.id)]);
  for (const alias of aliases) for (const id of [alias.request.intake_task_id, alias.launch.intake_task_id]) {
    if (id !== null && id !== undefined && id !== '' && (typeof id !== 'string' || open(id))) return String(id);
  }
  const projections = snapshot.projections;
  for (const projection of projections) {
    const links = projection.links;
    const linked = links.some((event: any) => event?.kind === 'link' &&
      ((event.hub_session_id && sessionIds.has(event.hub_session_id)) ||
       (binding.native_id && event.native_id === binding.native_id &&
        (!event.host || hosts.has(event.host)))));
    if (linked && open(String(projection.id))) return String(projection.id);
  }
  return null;
}

export function worktreeIntakeProtected(sql: SqlStorage, session: any, snapshot = worktreeIntakeSnapshot(sql)): boolean {
  try { return openWorktreeIntake(sql, session, snapshot) !== null; }
  catch { return true; }
}
