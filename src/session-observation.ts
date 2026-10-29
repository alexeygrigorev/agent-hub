import type { SqlStorage } from '@cloudflare/workers-types';

export function observationModel(value: unknown): string | null {
  return typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_./:-]{0,159}$/.test(value) ? value : null;
}

/** Publish identity and lifecycle without inventing an owner or a lock receipt.
 * Only the authenticated host census calls this helper. Operator adoption is
 * a separate transition with an exact native/repository receipt. */
export function insertSessionObservation(sql: SqlStorage, machine: string, native: any,
  state: string, family: string | null | undefined, now: number): void {
  const at = new Date(now).toISOString();
  const created = Number.isSafeInteger(native.created_at_ms) && native.created_at_ms > 0
    && Number.isFinite(new Date(native.created_at_ms).getTime()) ? native.created_at_ms : null;
  const request = { provenance: 'observed_native', origin: 'aplexer', observation_only: true,
    workspace: native.workspace, cwd: native.cwd ?? null, engine: native.engine ?? null,
    tag: native.tag, model: observationModel(native.model), created_at_ms: created };
  const binding = { native_id: native.native_id, tag: native.tag, workspace: native.workspace,
    native_created_at_ms: created, generation: 1, provenance: 'observed_native', observed_at: at };
  sql.exec(`INSERT INTO session_control_sessions
    (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,
     parent_id,parent_source,session_type,host_family,created_at,updated_at)
    VALUES (?,'unknown',?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    native.native_id, 'native-' + native.native_id, JSON.stringify(request), machine,
    native.engine ?? null, 'interactive', state, JSON.stringify(binding), native.parent_id || null,
    native.parent_id ? 'inferred' : 'unknown_external', 'aplexer', family ?? null,
    created ? new Date(created).toISOString() : at, at);
}

/** Tags are lookup aliases only. Filter ownership before testing ambiguity. */
export function observedHubTagMatches(sql: SqlStorage, ref: string, owns: (row: any) => boolean): any[] {
  if (!/^[a-f0-9][a-f0-9-]{7,35}$/i.test(ref)) return [];
  return (sql.exec(`SELECT * FROM session_control_sessions
    WHERE json_extract(request_json,'$.provenance')='observed_native'
      AND json_extract(binding_json,'$.tag') LIKE ?`, 'hub-' + ref.toLowerCase() + '%').toArray() as any[])
    .filter(row => {
      const tag = JSON.parse(row.binding_json).tag;
      return /^hub-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(tag) && owns(row);
    });
}

/** Reconcile linkage as evidence only. Scan retained history once per census,
 * releasing each parsed task before reading the next; never once per native. */
export function observedIntakeCandidates(sql: SqlStorage, machine: string, rows: any[]): Map<string, string> {
  const candidates = new Map<string, Set<string>>();
  if (!rows.length) return new Map();
  const ids = new Map<string, any[]>(), tags = new Map<string, any[]>(), paths = new Map<string, any[]>();
  const add = (index: Map<string, any[]>, key: string, row: any) => {
    if (key) index.set(key, [...(index.get(key) ?? []), row]);
  };
  const mark = (targets: any[] | undefined, task: string, host?: string) => {
    for (const row of targets ?? []) {
      if (host && host !== row.machine_id) continue;
      const matches = candidates.get(row.id) ?? new Set<string>();
      matches.add(task); candidates.set(row.id, matches);
    }
  };
  for (const row of rows) {
    const binding = JSON.parse(row.binding_json);
    add(ids, row.id, row);
    if (binding.native_id !== row.id) add(ids, binding.native_id, row);
    add(tags, binding.tag, row); add(paths, binding.workspace, row);
  }
  const knownTasks = new Set<string>();
  for (const stored of sql.exec('SELECT id, data FROM intake_tasks')) {
    let task: any;
    try { task = JSON.parse(String(stored.data)); } catch { continue; }
    if (!task || typeof task !== 'object') continue;
    knownTasks.add(String(stored.id));
    if (!Array.isArray(task.history)) continue;
    for (const event of task.history) {
      if (!event || typeof event !== 'object') continue;
      mark(ids.get(event.hub_session_id), String(stored.id));
      mark(ids.get(event.native_id), String(stored.id), event.host);
      if (event.kind !== 'note' || typeof event.text !== 'string' || !/^START(?:ED)?\b/i.test(event.text.trim())) continue;
      for (const token of event.text.split(/[^A-Za-z0-9_.:/~-]+/)) {
        mark(ids.get(token), String(stored.id));
        mark(tags.get(token), String(stored.id));
        if (token.includes(':')) mark(tags.get(token.slice(token.lastIndexOf(':') + 1)), String(stored.id));
      }
    }
  }
  const hasWorktrees = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_worktrees'").toArray().length;
  if (hasWorktrees) {
    for (const tree of sql.exec("SELECT path,task_id FROM intake_worktrees WHERE machine=? AND state='active'", machine)) {
      if (knownTasks.has(String(tree.task_id))) mark(paths.get(String(tree.path)), String(tree.task_id));
    }
  }
  return new Map([...candidates].filter(([, tasks]) => tasks.size === 1).map(([id, tasks]) => [id, [...tasks][0]]));
}
