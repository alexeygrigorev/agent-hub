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

/** Compact evidence projection, updated on task writes. Historical backfill is
 * keyset-paged and never copies full task bodies into the census handler. */
export function observationIntakeRevision(sql: SqlStorage, machine: string): string {
  sql.exec(`CREATE TABLE IF NOT EXISTS session_observation_intake_meta
    (id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT NOT NULL, complete INTEGER NOT NULL, revision INTEGER NOT NULL)`);
  sql.exec("INSERT OR IGNORE INTO session_observation_intake_meta VALUES(1,'',0,0)");
  sql.exec('CREATE TABLE IF NOT EXISTS session_observation_intakes(task_id TEXT PRIMARY KEY, events TEXT NOT NULL)');
  const events = (data: string) => `(SELECT json_group_array(json_object(
    'hub_session_id',json_extract(e.value,'$.hub_session_id'),
    'native_id',json_extract(e.value,'$.native_id'),'host',json_extract(e.value,'$.host'),
    'kind',json_extract(e.value,'$.kind'),'text',CASE WHEN json_extract(e.value,'$.kind')='note'
      THEN json_extract(e.value,'$.text') ELSE NULL END))
    FROM json_each(${data},'$.history') e WHERE json_extract(e.value,'$.hub_session_id') IS NOT NULL
      OR json_extract(e.value,'$.native_id') IS NOT NULL
      OR (json_extract(e.value,'$.kind')='note' AND upper(ltrim(json_extract(e.value,'$.text'))) LIKE 'START%'))`;
  for (const op of ['INSERT','UPDATE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS session_observation_intake_${op.toLowerCase()}
    AFTER ${op} ON intake_tasks BEGIN
      INSERT INTO session_observation_intakes(task_id,events) VALUES(NEW.id,${events('NEW.data')})
        ON CONFLICT(task_id) DO UPDATE SET events=excluded.events;
      UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1;
    END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS session_observation_intake_delete AFTER DELETE ON intake_tasks BEGIN
    DELETE FROM session_observation_intakes WHERE task_id=OLD.id;
    UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1; END`);
  let meta: any = sql.exec('SELECT * FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  if (!meta.complete) {
    const batch = sql.exec(`SELECT id,${events('data')} AS events FROM intake_tasks WHERE id>? ORDER BY id LIMIT 100`, meta.cursor).toArray();
    for (const task of batch) sql.exec(`INSERT INTO session_observation_intakes(task_id,events) VALUES(?,?)
      ON CONFLICT(task_id) DO UPDATE SET events=excluded.events`, task.id, task.events);
    sql.exec('UPDATE session_observation_intake_meta SET cursor=?,complete=?,revision=revision+1 WHERE id=1',
      batch.length ? batch[batch.length-1].id : meta.cursor, batch.length < 100 ? 1 : 0);
    meta = sql.exec('SELECT * FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  }
  const hasWorktrees = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_worktrees'").toArray().length;
  const trees: any = hasWorktrees ? sql.exec("SELECT count(*) AS n,max(updated_at) AS at FROM intake_worktrees WHERE machine=? AND state='active'",machine).toArray()[0] : {};
  return `${meta.revision}/${meta.complete}/${trees.n ?? 0}/${trees.at ?? ''}`;
}

/** Exact token matching remains in JS; SQL only prefilters compact evidence.
 * Ambiguity and incomplete backfill fail closed rather than choosing a task. */
export function observedIntakeCandidates(sql: SqlStorage, machine: string, rows: any[]): Map<string, string> {
  const links = new Map<string, string>();
  if (!rows.length) return links;
  const meta: any = sql.exec('SELECT complete FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  if (!meta?.complete) return links;
  const hasWorktrees = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_worktrees'").toArray().length;
  for (const row of rows) {
    const binding = JSON.parse(row.binding_json);
    const ids = new Set([row.id, binding.native_id].filter(Boolean));
    const matches = new Set<string>();
    const candidates = sql.exec(`SELECT task_id,events FROM session_observation_intakes
      WHERE instr(events,?)>0 OR instr(events,?)>0 OR instr(events,?)>0 LIMIT 101`,
      row.id, binding.native_id || row.id, binding.tag || row.id).toArray();
    if (candidates.length > 100) continue;
    for (const task of candidates) {
      const events = JSON.parse(String(task.events));
      for (const event of events) {
        if (ids.has(event.hub_session_id) || (ids.has(event.native_id) && (!event.host || event.host === machine))) {
          matches.add(String(task.task_id)); break;
        }
        if (event.kind !== 'note' || typeof event.text !== 'string' || !/^START(?:ED)?\b/i.test(event.text.trim())) continue;
        const tokens: string[] = event.text.split(/[^A-Za-z0-9_.:/~-]+/);
        if (tokens.some(token => ids.has(token) || token === binding.tag || token.endsWith(':' + binding.tag))) {
          matches.add(String(task.task_id)); break;
        }
      }
    }
    if (hasWorktrees) for (const tree of sql.exec(`SELECT task_id FROM intake_worktrees w
      WHERE machine=? AND path=? AND state='active' AND EXISTS(SELECT 1 FROM session_observation_intakes i WHERE i.task_id=w.task_id)`,machine,binding.workspace)) {
      matches.add(String(tree.task_id));
    }
    if (matches.size === 1) links.set(row.id,[...matches][0]);
  }
  return links;
}
