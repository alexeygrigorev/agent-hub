/** Destructive worktree cleanup requires a complete, readable intake ledger. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { isIntakeTaskOpen } from './session_lifecycle_registry';

function record(raw: any): any {
  const value = JSON.parse(raw || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid session ledger');
  return value;
}

export function worktreeIntakeSnapshot(sql: SqlStorage): { valid: boolean; machines: any[]; projections: any[]; sessions: any[] } {
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
    return { valid: true, machines, projections, sessions };
  } catch { return { valid: false, machines: [], projections: [], sessions: [] }; }
}

export function openWorktreeIntake(sql: SqlStorage, session: any, snapshot = worktreeIntakeSnapshot(sql)): string | null {
  if (!snapshot.valid) throw new Error('unreadable intake links');
  if (!session) throw new Error('missing session identity');
  const request = record(session.request_json);
  const launch = record(session.launch_request);
  for (const id of [request.intake_task_id, launch.intake_task_id]) {
    if (id !== null && id !== undefined && id !== '' && (typeof id !== 'string' || isIntakeTaskOpen(sql, id))) return String(id);
  }
  const binding = record(session.binding_json);
  const machines = snapshot.machines;
  const own = machines.find(row => row.id === session.machine_id);
  const hostname = typeof own?.hostname === 'string' ? own.hostname.trim().toUpperCase() : '';
  const hosts = new Set([session.machine_id, ...machines.filter(row => hostname &&
    typeof row.hostname === 'string' && row.hostname.trim().toUpperCase() === hostname).map(row => row.id)]);
  const aliases = snapshot.sessions.filter(row => binding.native_id &&
    row.binding.native_id === binding.native_id && hosts.has(row.machine_id));
  const sessionIds = new Set([session.id, ...aliases.map(row => row.id)]);
  for (const alias of aliases) for (const id of [alias.request.intake_task_id, alias.launch.intake_task_id]) {
    if (id !== null && id !== undefined && id !== '' && (typeof id !== 'string' || isIntakeTaskOpen(sql, id))) return String(id);
  }
  const projections = snapshot.projections;
  for (const projection of projections) {
    const links = projection.links;
    const linked = links.some((event: any) => event?.kind === 'link' &&
      ((event.hub_session_id && sessionIds.has(event.hub_session_id)) ||
       (binding.native_id && event.native_id === binding.native_id &&
        (!event.host || hosts.has(event.host)))));
    if (linked && isIntakeTaskOpen(sql, String(projection.id))) return String(projection.id);
  }
  return null;
}

export function worktreeIntakeProtected(sql: SqlStorage, session: any, snapshot = worktreeIntakeSnapshot(sql)): boolean {
  try { return openWorktreeIntake(sql, session, snapshot) !== null; }
  catch { return true; }
}
