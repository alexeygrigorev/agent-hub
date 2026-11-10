import {type MeteredDb} from './db';
import {error, json} from './http';

/** Filter in SQL before paginating: sparse matches must not disappear behind a page. */
export function listProjectIntakes(db: MeteredDb, query: URLSearchParams, project: string): Response {
  const limit = Number(query.get('limit') || 25);
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) return error('invalid_limit', 400);
  const status = query.get('status');
  if (status && !['open','queued','running','completed-awaiting-review','failed','needs_attention','archived','accepted'].includes(status)) return error('invalid_status',400);
  let cursor: [number,string] = [Number.MAX_SAFE_INTEGER,'\uffff'];
  if (query.has('cursor')) {
    try { cursor = JSON.parse(query.get('cursor')!) as [number,string]; } catch { return error('invalid_cursor',400); }
    if (!Array.isArray(cursor) || cursor.length !== 2 || !Number.isSafeInteger(cursor[0]) || typeof cursor[1] !== 'string' || cursor[1].length > 80) return error('invalid_cursor',400);
  }
  const search = query.get('q') ?? query.get('search') ?? '';
  if (search.length > 128 || ((query.has('q') || query.has('search')) && !search.trim())) return error('invalid_search',400);
  const where = ['project=?','(updated_at,id)<(?,?)'];
  const args: unknown[] = [project,...cursor];
  if (status) { where.push('status=?'); args.push(status); }
  if (search) {
    where.push("(instr(lower(title),lower(?))>0 OR instr(lower(project),lower(?))>0 OR instr(lower(text),lower(?))>0 OR instr(lower(COALESCE(original_title,'')),lower(?))>0 OR instr(lower(COALESCE(original_text,'')),lower(?))>0 OR EXISTS (SELECT 1 FROM intake_events e WHERE e.task_id=intakes.id AND instr(lower(e.body_json),lower(?))>0))");
    args.push(...Array(6).fill(search));
  }
  const rows = db.all<any>(`SELECT id,project,title,status,assigned_machine_id,assigned_actor_id,session_id,created_at,updated_at,priority FROM intakes WHERE ${where.join(' AND ')} ORDER BY updated_at DESC,id DESC LIMIT ?`,...args,limit+1);
  const items = rows.slice(0,limit), last = items.at(-1);
  return json({items,filtered_page:!!search,next: rows.length>limit && last ? JSON.stringify([last.updated_at,last.id]):null});
}
