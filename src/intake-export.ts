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

}
/** Parameterized authorization identical to IntakeService.allowed, including
 * union/overlap grants. Never count a task twice or reveal denied ids. */
export function intakeExportScope(p: Obj): { where: string; args: any[] } {
  if (p.admin || p.grants.some((g: Obj) => g.scope === 'all')) return { where: '1', args: [] };
  const args: any[] = [];
  const terms = p.grants.filter((g: Obj) => ['task','project'].includes(g.scope)).map((g: Obj) => {
    args.push(g.target); return `${g.scope === 'task' ? 'id' : 'project'}=?`;
  });
  return { where: '(' + (terms.join(' OR ') || '0') + ')', args };
}

/** All/project totals come from tiny write-maintained counters. Task-only
 * grants use exact primary-key lookups, excluding overlapping project grants. */
export function intakeExportCounts(sql: SqlStorage, p: Obj, project: string | null): Obj[] {
  const all = p.admin || p.grants.some((g: Obj) => g.scope === 'all');
  const projects: string[] = all ? [] : [...new Set<string>(p.grants.filter((g: Obj) => g.scope === 'project').map((g: Obj) => g.target))];
  const tasks: string[] = all ? [] : [...new Set<string>(p.grants.filter((g: Obj) => g.scope === 'task').map((g: Obj) => g.target))];
  const terms: string[] = []; const args: any[] = [];
  if (project !== null) { terms.push('project=?'); args.push(project); }
  if (!all) { terms.push(projects.length ? `project IN (${projects.map(() => '?').join(',')})` : '0'); args.push(...projects); }
  const rows = sql.exec(`SELECT status,archived,SUM(n) AS n FROM intake_export_counts${terms.length ? ' WHERE ' + terms.join(' AND ') : ''} GROUP BY status,archived`, ...args).toArray() as Obj[];
  if (tasks.length) {
    const taskTerms = [`id IN (${tasks.map(() => '?').join(',')})`]; const taskArgs: any[] = [...tasks];
    if (project !== null) { taskTerms.push('project=?'); taskArgs.push(project); }
    if (projects.length) { taskTerms.push(`project NOT IN (${projects.map(() => '?').join(',')})`); taskArgs.push(...projects); }
    rows.push(...sql.exec(`SELECT status,archived,COUNT(*) AS n FROM intake_export WHERE ${taskTerms.join(' AND ')} GROUP BY status,archived`, ...taskArgs).toArray() as Obj[]);
  }
  return rows;
}

/** Each grant contributes at most five indexed candidates. The final sort is
 * bounded by grant count, even for sparse authorization on a large ledger. */
export function intakeExportRecent(sql: SqlStorage, p: Obj, project: string | null): Obj[] {
  const all = p.admin || p.grants.some((g: Obj) => g.scope === 'all');
  if (all) return sql.exec(`SELECT data FROM intake_export${project === null ? '' : ' WHERE project=?'} ORDER BY updated_at DESC,id LIMIT 5`,
    ...(project === null ? [] : [project])).toArray() as Obj[];
  const projects = [...new Set<string>(p.grants.filter((g: Obj) => g.scope === 'project' && (project === null || g.target === project)).map((g: Obj) => g.target))];
  const tasks = [...new Set<string>(p.grants.filter((g: Obj) => g.scope === 'task').map((g: Obj) => g.target))];
  const parts: string[] = []; const args: any[] = [];
  for (const target of projects) {
    parts.push('SELECT id,updated_at,data FROM (SELECT id,updated_at,data FROM intake_export WHERE project=? ORDER BY updated_at DESC,id LIMIT 5)'); args.push(target);
  }
  if (tasks.length) {
    const terms = [`id IN (${tasks.map(() => '?').join(',')})`]; args.push(...tasks);
    if (project !== null) { terms.push('project=?'); args.push(project); }
    if (projects.length) { terms.push(`project NOT IN (${projects.map(() => '?').join(',')})`); args.push(...projects); }
    parts.push(`SELECT id,updated_at,data FROM (SELECT id,updated_at,data FROM intake_export WHERE ${terms.join(' AND ')} ORDER BY updated_at DESC,id LIMIT 5)`);
  }
  return parts.length ? sql.exec(`SELECT data FROM (${parts.join(' UNION ALL ')}) ORDER BY updated_at DESC,id LIMIT 5`, ...args).toArray() as Obj[] : [];
}
