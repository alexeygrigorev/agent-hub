/** Small, transactionally maintained read model. Never stores history bodies.
 * Bootstrap scans retained ledgers once; reads never do. */
import type { SqlStorage } from '@cloudflare/workers-types';
type Obj = Record<string, any>;
export function initIntakeExport(sql: SqlStorage) {
  const exists = sql.exec("SELECT 1 FROM sqlite_master WHERE name='intake_export'").toArray().length;
  sql.exec(`CREATE TABLE IF NOT EXISTS intake_export (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
    archived INTEGER NOT NULL, updated_at INTEGER NOT NULL, data TEXT NOT NULL)`);
  sql.exec('CREATE INDEX IF NOT EXISTS intake_export_recent ON intake_export(updated_at DESC,id)');
  sql.exec('CREATE INDEX IF NOT EXISTS intake_export_project_recent ON intake_export(project,updated_at DESC,id)');
  sql.exec('CREATE INDEX IF NOT EXISTS intake_export_status ON intake_export(status,archived,id)');
  sql.exec('CREATE INDEX IF NOT EXISTS intake_export_project ON intake_export(project,id)');
  for (const [name, columns] of [
    ['archive', 'archived,id'], ['status_id', 'status,id'],
    ['project_archive', 'project,archived,id'], ['project_status', 'project,status,id'],
    ['project_status_archive', 'project,status,archived,id'],
  ]) sql.exec(`CREATE INDEX IF NOT EXISTS intake_export_${name} ON intake_export(${columns})`);
  const select = (prefix: string) => {
    const d = prefix + 'data';
    const j = (key: string) => `json_extract(${d},'$.${key}')`;
    return `${prefix}id,${j('project')},${j('status')},CASE WHEN ${j('archived_at')} IS NULL THEN 0 ELSE 1 END,COALESCE(${j('updated_at')},0),
      json_object('id',${prefix}id,'project',${j('project')},'status',${j('status')},'version',${j('version')},
        'title',${j('title')},'owner',${j('owner')},'current_state',${j('current_state')},'next_step',${j('next_step')},
        'created_at',${j('created_at')},'updated_at',${j('updated_at')},'archived_at',${j('archived_at')},
        'last_event_kind',${j('history[#-1].kind')},'last_event_at',${j('history[#-1].captured_at')},
        'last_session_id',(SELECT json_extract(value,'$.hub_session_id') FROM json_each(${d},'$.history')
          WHERE json_extract(value,'$.kind')='link' AND json_extract(value,'$.hub_session_id') IS NOT NULL ORDER BY CAST(key AS INTEGER) DESC LIMIT 1))`;
  };
  if (!exists) sql.exec(`INSERT INTO intake_export SELECT ${select('')} FROM intake_tasks`);
  for (const op of ['INSERT','UPDATE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_export_${op.toLowerCase()} AFTER ${op} ON intake_tasks BEGIN
    INSERT INTO intake_export SELECT ${select('NEW.')} ON CONFLICT(id) DO UPDATE SET
      project=excluded.project,status=excluded.status,archived=excluded.archived,updated_at=excluded.updated_at,data=excluded.data; END`);
  sql.exec('CREATE TRIGGER IF NOT EXISTS intake_export_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_export WHERE id=OLD.id; END');
  const countsExist = sql.exec("SELECT 1 FROM sqlite_master WHERE name='intake_export_counts'").toArray().length;
  sql.exec(`CREATE TABLE IF NOT EXISTS intake_export_counts(project TEXT NOT NULL,status TEXT NOT NULL,archived INTEGER NOT NULL,n INTEGER NOT NULL,
    PRIMARY KEY(project,status,archived))`);
  if (!countsExist) sql.exec('INSERT INTO intake_export_counts SELECT project,status,archived,COUNT(*) FROM intake_export GROUP BY project,status,archived');
  for (const op of ['INSERT','UPDATE','DELETE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_export_count_${op.toLowerCase()} AFTER ${op} ON intake_export BEGIN
    ${op !== 'INSERT' ? 'UPDATE intake_export_counts SET n=n-1 WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_counts WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived AND n=0;' : ''}
    ${op !== 'DELETE' ? 'INSERT INTO intake_export_counts VALUES(NEW.project,NEW.status,NEW.archived,1) ON CONFLICT(project,status,archived) DO UPDATE SET n=n+1;' : ''}
    END`);

  const totalsExist = sql.exec("SELECT 1 FROM sqlite_master WHERE name='intake_export_totals'").toArray().length;
  sql.exec(`CREATE TABLE IF NOT EXISTS intake_export_totals(status TEXT NOT NULL,archived INTEGER NOT NULL,n INTEGER NOT NULL,
    PRIMARY KEY(status,archived))`);
  if (!totalsExist) sql.exec('INSERT INTO intake_export_totals SELECT status,archived,SUM(n) FROM intake_export_counts GROUP BY status,archived');
  for (const op of ['INSERT','UPDATE','DELETE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS intake_export_total_${op.toLowerCase()} AFTER ${op} ON intake_export BEGIN
    ${op !== 'INSERT' ? 'UPDATE intake_export_totals SET n=n-1 WHERE status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_totals WHERE status=OLD.status AND archived=OLD.archived AND n=0;' : ''}
    ${op !== 'DELETE' ? 'INSERT INTO intake_export_totals VALUES(NEW.status,NEW.archived,1) ON CONFLICT(status,archived) DO UPDATE SET n=n+1;' : ''}
    END`);
}
/** Exact project buckets and task primary keys keep both work and binding
 * count bounded. Callers combine bucket parts in memory; no SQL GROUP sort. */
export function intakeExportCounts(sql: SqlStorage, p: Obj, project: string | null): Obj[] {
  const all = p.admin || p.grants.some((g: Obj) => g.scope === 'all');
  if (all && project === null) return sql.exec('SELECT status,archived,n FROM intake_export_totals').toArray() as Obj[];
  const projects: string[] = all ? [project!] : [...new Set<string>(p.grants.filter((g: Obj) =>
    g.scope === 'project' && (project === null || g.target === project)).map((g: Obj) => g.target))];
  const rows: Obj[] = [];
  for (const target of projects) rows.push(...sql.exec('SELECT status,archived,n FROM intake_export_counts WHERE project=?', target).toArray() as Obj[]);
  if (!all) for (const id of new Set<string>(p.grants.filter((g: Obj) => g.scope === 'task').map((g: Obj) => g.target))) {
    const row = sql.exec('SELECT project,status,archived FROM intake_export WHERE id=?', id).toArray()[0] as Obj | undefined;
    if (row && (project === null || row.project === project) && !projects.includes(row.project)) rows.push({ status: row.status, archived: row.archived, n: 1 });
  }
  return rows;
}

/** Each project contributes only five indexed rows; each task is one primary
 * key lookup. Merge at most grants*5 candidates without large IN/UNION binds. */
export function intakeExportRecent(sql: SqlStorage, p: Obj, project: string | null): Obj[] {
  const all = p.admin || p.grants.some((g: Obj) => g.scope === 'all');
  if (all) return sql.exec(`SELECT data FROM intake_export INDEXED BY ${project === null ? 'intake_export_recent' : 'intake_export_project_recent'}${project === null ? '' : ' WHERE project=?'} ORDER BY updated_at DESC,id LIMIT 5`,
    ...(project === null ? [] : [project])).toArray() as Obj[];
  const rows = new Map<string, Obj>();
  for (const target of new Set<string>(p.grants.filter((g: Obj) => g.scope === 'project' && (project === null || g.target === project)).map((g: Obj) => g.target))) {
    for (const row of sql.exec('SELECT id,updated_at,data FROM intake_export INDEXED BY intake_export_project_recent WHERE project=? ORDER BY updated_at DESC,id LIMIT 5', target).toArray() as Obj[]) rows.set(row.id, row);
  }
  for (const id of new Set<string>(p.grants.filter((g: Obj) => g.scope === 'task').map((g: Obj) => g.target))) {
    const row = sql.exec('SELECT id,project,updated_at,data FROM intake_export WHERE id=?', id).toArray()[0] as Obj | undefined;
    if (row && (project === null || row.project === project)) rows.set(row.id, row);
  }
  return [...rows.values()].sort((a, b) => Number(b.updated_at) - Number(a.updated_at) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).slice(0, 5);
}

export type IntakeExportFilter = { project: string | null; statuses: string[] | null; archived: number | null };
export function intakeExportMatches(row: Obj, filter: IntakeExportFilter): boolean {
  return (filter.statuses === null || filter.statuses.includes(row.status)) &&
    (filter.archived === null || Number(row.archived) === filter.archived);
}
/** Every source is an equality-prefix + primary-key range with its own LIMIT.
 * The merge sorts at most (grants * fixed status count * limit) ids; it never
 * scans unrelated projects looking for authorized matches. */
export function intakeExportPageIds(sql: SqlStorage, p: Obj, filter: IntakeExportFilter, after: string | null, limit: number): string[] {
  const all = p.admin || p.grants.some((g: Obj) => g.scope === 'all');
  const projects: (string | null)[] = all ? [filter.project] : [...new Set<string>(p.grants
    .filter((g: Obj) => g.scope === 'project' && (filter.project === null || g.target === filter.project)).map((g: Obj) => g.target))];
  const ids = new Set<string>();
  for (const project of projects) for (const status of filter.statuses ?? [null]) {
    const terms: string[] = []; const args: any[] = [];
    if (project !== null) { terms.push('project=?'); args.push(project); }
    if (status !== null) { terms.push('status=?'); args.push(status); }
    if (filter.archived !== null) { terms.push('archived=?'); args.push(filter.archived); }
    if (after) { terms.push('id>?'); args.push(after); }
    let index: string;
    if (project !== null) index = status !== null ? (filter.archived !== null ? 'project_status_archive' : 'project_status') : (filter.archived !== null ? 'project_archive' : 'project');
    else index = status !== null ? (filter.archived !== null ? 'status' : 'status_id') : (filter.archived !== null ? 'archive' : '');
    const name = index ? 'intake_export_' + index : 'sqlite_autoindex_intake_export_1';
    for (const row of sql.exec(`SELECT id FROM intake_export INDEXED BY ${name}${terms.length ? ' WHERE ' + terms.join(' AND ') : ''} ORDER BY id LIMIT ?`, ...args, limit).toArray() as Obj[]) ids.add(row.id);
  }
  if (!all) for (const id of new Set<string>(p.grants.filter((g: Obj) => g.scope === 'task').map((g: Obj) => g.target))) {
    const row = sql.exec('SELECT id,project,status,archived FROM intake_export WHERE id=?', id).toArray()[0] as Obj | undefined;
    if (row && (!after || row.id > after) && (filter.project === null || row.project === filter.project) && intakeExportMatches(row, filter)) ids.add(row.id);
  }
  return [...ids].sort().slice(0, limit);
}
