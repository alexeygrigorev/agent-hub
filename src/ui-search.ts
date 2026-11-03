import type { SqlStorage } from '@cloudflare/workers-types';

type Row = Record<string, any>;
type Kind = 'sessions' | 'tasks' | 'machines';
const KINDS: Kind[] = ['sessions', 'tasks', 'machines'];
const ACTIVE_SESSIONS = ['offered', 'queued', 'held', 'starting', 'running', 'uncertain'];
const ACTIVE_TASKS = ['open', 'in_progress', 'blocked', 'pending_approval'];
const MAX_QUERY = 100;
const MAX_LIMIT = 20;
const SCAN_PER_KIND = 100;
const STATUSES = new Set([
  ...ACTIVE_SESSIONS, 'stopped', 'failed', 'cancelled', 'exited', 'completed',
  ...ACTIVE_TASKS, 'done', 'closed', 'archived', 'drained', 'stale', 'available',
]);

export type UiSearchInput = {
  query: string;
  scope: 'active' | 'all';
  kinds: Kind[];
  project: string | null;
  host: string | null;
  statuses: string[];
  limit: number;
  after: Record<Kind, string>;
};

export type UiSearchResult = {
  id: string;
  kind: Kind;
  title: string;
  status: string;
  context: string;
  href: string;
};

export type UiSearchPage = {
  query: string;
  scope: 'active' | 'all';
  results: UiSearchResult[];
  next_cursor: string | null;
  partial: boolean;
  scanned: number;
};

function json(data: unknown, status = 200) {
  return Response.json(data, { status, headers: {
    'Cache-Control': 'private, no-store',
    'X-Content-Type-Options': 'nosniff',
  } });
}

function cursorEncode(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function cursorDecode(value: string): unknown {
  if (value.length > 2048 || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('invalid_cursor');
  const normalized = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4);
  const binary = atob(normalized);
  const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export function parseUiSearchInput(url: URL): UiSearchInput | { error: string } {
  if (url.search.length > 4096) return { error: 'invalid_request' };
  const qValues = url.searchParams.getAll('q');
  const query = (qValues[0] ?? '').trim();
  if (qValues.length !== 1 || query.length < 2 || query.length > MAX_QUERY || /[\u0000-\u001f\u007f]/.test(query)) return { error: 'invalid_query' };

  const rawScope = url.searchParams.get('scope') ?? 'active';
  if (!['active', 'all'].includes(rawScope)) return { error: 'invalid_scope' };
  const rawKinds = url.searchParams.getAll('type');
  if (rawKinds.length > KINDS.length) return { error: 'invalid_type' };
  const kinds = rawKinds.length ? [...new Set(rawKinds as Kind[])] : [...KINDS];
  if (!kinds.length || kinds.some(kind => !KINDS.includes(kind))) return { error: 'invalid_type' };
  const rawStatuses = url.searchParams.getAll('status').flatMap(value => value.split(','));
  const statuses = [...new Set(rawStatuses.filter(Boolean).map(s => s.toLowerCase()))];
  if (statuses.length > 8 || statuses.some(status => status.length > 32 || !STATUSES.has(status))) return { error: 'invalid_status' };
  const project = url.searchParams.get('project');
  const host = url.searchParams.get('host');
  if ((project && project.length > 128) || (host && host.length > 128)) return { error: 'invalid_filter' };
  const rawLimit = url.searchParams.get('limit');
  const limit = rawLimit === null ? 12 : Number(rawLimit);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) return { error: 'invalid_limit' };

  let after: Record<Kind, string> = { sessions: '', tasks: '', machines: '' };
  const cursorValues = url.searchParams.getAll('cursor');
  if (cursorValues.length > 1) return { error: 'invalid_cursor' };
  if (cursorValues[0]) {
    try {
      const cursor: any = cursorDecode(cursorValues[0]);
      if (!cursor || cursor.v !== 1 || cursor.q !== query || cursor.scope !== rawScope ||
          cursor.project !== (project || null) || cursor.host !== (host || null) ||
          JSON.stringify(cursor.kinds) !== JSON.stringify(kinds) ||
          JSON.stringify(cursor.statuses) !== JSON.stringify(statuses) ||
          !cursor.after || KINDS.some(kind => typeof cursor.after[kind] !== 'string' || cursor.after[kind].length > 256)) {
        return { error: 'invalid_cursor' };
      }
      after = cursor.after;
    } catch { return { error: 'invalid_cursor' }; }
  }
  return { query, scope: rawScope as 'active' | 'all', kinds, project: project || null, host: host || null, statuses, limit, after };
}

const contains = (haystack: unknown, needle: string) => String(haystack ?? '').toLocaleLowerCase().includes(needle.toLocaleLowerCase());
const cleanTitle = (value: unknown, fallback: string) => {
  if (typeof value !== 'string') return fallback;
  const title = value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 140);
  return title || fallback;
};
const isActiveSession = (state: string) => ACTIVE_SESSIONS.includes(state);
const isActiveTask = (status: string) => ACTIVE_TASKS.includes(status);

function matchFacet(status: string, requested: string[]) {
  return !requested.length || requested.includes(status.toLowerCase());
}

function cursorFor(input: UiSearchInput, after: Record<Kind, string>) {
  return cursorEncode({ v: 1, q: input.query, scope: input.scope, kinds: input.kinds,
    project: input.project, host: input.host, statuses: input.statuses, after });
}

/**
 * Global operator search. Each request reads at most 100 rows per entity kind,
 * ordered by the entity primary key. Cursors are scan positions (not offsets),
 * so a miss in an early page cannot be mistaken for a complete search.
 * Only display-safe projections are returned; prompts, notes, capabilities,
 * credentials and raw request JSON never leave the worker.
 */
export function searchUiRecords(sql: SqlStorage, input: UiSearchInput, now = Date.now()): UiSearchPage {
  const q = input.query.toLocaleLowerCase();
  const results: UiSearchResult[] = [];
  const after = { ...input.after };
  let scanned = 0;
  let partial = false;

  if (input.kinds.includes('sessions')) {
    const clauses = ['id > ?']; const args: any[] = [after.sessions];
    if (input.scope === 'active') {
      clauses.push(`(state IN ('queued','held','offered') OR (state IN ('starting','running','uncertain') AND EXISTS (
        SELECT 1 FROM fleet_sessions f WHERE f.machine_id = session_control_sessions.machine_id
        AND f.native_id = json_extract(session_control_sessions.binding_json, '$.native_id') AND f.observed_at >= ?)))`);
      args.push(new Date(now - 10 * 60 * 1000).toISOString());
    }
    if (input.statuses.length) { clauses.push(`state IN (${input.statuses.map(() => '?').join(',')})`); args.push(...input.statuses); }
    if (input.host) { clauses.push('machine_id = ?'); args.push(input.host); }
    if (input.project) {
      clauses.push(`(json_extract(request_json,'$.project') = ? OR EXISTS (
        SELECT 1 FROM intake_export ie WHERE ie.id = json_extract(session_control_sessions.request_json,'$.intake_task_id') AND ie.project = ?))`);
      args.push(input.project, input.project);
    }
    const rows = sql.exec(`SELECT id,actor,state,machine_id,engine,request_json,binding_json,created_at FROM session_control_sessions WHERE ${clauses.join(' AND ')} ORDER BY id LIMIT ?`, ...args, SCAN_PER_KIND + 1).toArray() as Row[];
    partial ||= rows.length > SCAN_PER_KIND;
    const page = rows.slice(0, SCAN_PER_KIND); scanned += page.length;
    for (const row of page) {
      after.sessions = String(row.id);
      let request: Row = {}, binding: Row = {};
      try { request = JSON.parse(row.request_json || '{}'); } catch {}
      try { binding = JSON.parse(row.binding_json || '{}'); } catch {}
      let linkedTaskTitle = '', linkedTaskProject = '';
      if (typeof request.intake_task_id === 'string' && request.intake_task_id.length <= 128) {
        try {
          const linked = sql.exec('SELECT project,data FROM intake_export WHERE id = ?', request.intake_task_id).toArray()[0] as Row | undefined;
          if (linked) { linkedTaskTitle = JSON.parse(linked.data || '{}').title || ''; linkedTaskProject = String(linked.project || ''); }
        } catch { /* The task index may be absent while its migration starts. */ }
      }
      const title = cleanTitle(request.name ?? request.title ?? linkedTaskTitle, `Session ${String(row.id).slice(0, 8)}`);
      const aliases = [row.id, binding.native_id, binding.session_id, binding.tag, request.alias, request.name, request.title, linkedTaskTitle];
      if (!aliases.some(value => contains(value, q))) continue;
      const status = String(row.state || 'unknown');
      if (!matchFacet(status, input.statuses)) continue;
      const matchingAlias = [binding.native_id, binding.session_id, binding.tag, request.alias].find(value => value && value !== row.id && contains(value, q));
      const context = [linkedTaskTitle && linkedTaskTitle !== title ? linkedTaskTitle : null, matchingAlias || null, linkedTaskProject || null, row.engine, row.machine_id ? `host ${row.machine_id}` : null].filter(Boolean).join(' · ');
      results.push({ id: String(row.id), kind: 'sessions', title, status, context, href: `/sessions.html?session=${encodeURIComponent(row.id)}` });
    }
  }

  if (input.kinds.includes('tasks')) {
    const clauses = ['id > ?']; const args: any[] = [after.tasks];
    if (input.scope === 'active') { clauses.push(`status IN (${ACTIVE_TASKS.map(() => '?').join(',')})`, 'archived = 0'); args.push(...ACTIVE_TASKS); }
    if (input.statuses.length) { clauses.push(`status IN (${input.statuses.map(() => '?').join(',')})`); args.push(...input.statuses); }
    if (input.project) { clauses.push('project = ?'); args.push(input.project); }
    if (input.host) {
      clauses.push(`EXISTS (SELECT 1 FROM session_control_sessions s WHERE s.machine_id = ?
        AND json_extract(s.request_json,'$.intake_task_id') = intake_export.id)`);
      args.push(input.host);
    }
    const ready = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_export'").toArray().length > 0;
    const rows = ready ? sql.exec(`SELECT id,project,status,archived,data FROM intake_export WHERE ${clauses.join(' AND ')} ORDER BY id LIMIT ?`, ...args, SCAN_PER_KIND + 1).toArray() as Row[] : [];
    if (!ready) partial = true;
    partial ||= rows.length > SCAN_PER_KIND;
    const page = rows.slice(0, SCAN_PER_KIND); scanned += page.length;
    for (const row of page) {
      after.tasks = String(row.id);
      let data: Row = {};
      try { data = JSON.parse(row.data || '{}'); } catch {}
      if (![row.id, data.title, row.project].some(value => contains(value, q))) continue;
      const status = String(row.status || 'unknown');
      results.push({ id: String(row.id), kind: 'tasks', title: cleanTitle(data.title, `Task ${String(row.id).slice(0, 8)}`), status,
        context: String(row.project || ''), href: `/intake/${encodeURIComponent(row.id)}` });
    }
  }

  if (input.kinds.includes('machines')) {
    const clauses = ['revoked_at IS NULL', 'id > ?']; const args: any[] = [after.machines];
    if (input.host) { clauses.push('id = ?'); args.push(input.host); }
    const rows = sql.exec(`SELECT id,name,hostname,drained,last_checkin_at FROM machines WHERE ${clauses.join(' AND ')} ORDER BY id LIMIT ?`, ...args, SCAN_PER_KIND + 1).toArray() as Row[];
    partial ||= rows.length > SCAN_PER_KIND;
    const page = rows.slice(0, SCAN_PER_KIND); scanned += page.length;
    for (const row of page) {
      after.machines = String(row.id);
      const title = cleanTitle(row.name, cleanTitle(row.hostname, `Machine ${String(row.id).slice(0, 8)}`));
      if (![row.id, row.name, row.hostname].some(value => contains(value, q))) continue;
      const lastSeen = row.last_checkin_at ? Date.parse(row.last_checkin_at) : 0;
      const fresh = Number.isFinite(lastSeen) && now - lastSeen <= 15 * 60 * 1000;
      const status = row.drained ? 'drained' : fresh ? 'available' : 'stale';
      if (input.scope === 'active' && (!fresh || row.drained)) continue;
      if (!matchFacet(status, input.statuses)) continue;
      results.push({ id: String(row.id), kind: 'machines', title, status,
        context: row.hostname && row.hostname !== title ? String(row.hostname).slice(0, 100) : '',
        href: `/machines.html?machine=${encodeURIComponent(row.id)}` });
    }
  }

  const fair: UiSearchResult[] = [];
  for (let index = 0; fair.length < input.limit; index++) {
    let added = false;
    for (const kind of KINDS) {
      const result = results.filter(item => item.kind === kind)[index];
      if (result && fair.length < input.limit) { fair.push(result); added = true; }
    }
    if (!added) break;
  }
  const next = partial ? cursorFor(input, after) : null;
  return { query: input.query, scope: input.scope, results: fair, next_cursor: next, partial, scanned };
}

export function handleUiSearch(request: Request, sql: SqlStorage, isAdmin: boolean, advanceIndex?: () => boolean): Response {
  if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
  if (!isAdmin) return json({ error: 'forbidden' }, 403);
  const input = parseUiSearchInput(new URL(request.url));
  if ('error' in input) return json({ error: input.error }, 400);
  try {
    const exportReady = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_export'").toArray().length > 0;
    let exportMigration = exportReady
      ? sql.exec('SELECT done FROM intake_export_migration WHERE id=1').toArray()[0] as Row | undefined
      : undefined;
    if (exportMigration && Number(exportMigration.done) !== 1 && advanceIndex) {
      const done = advanceIndex();
      exportMigration = { done: done ? 1 : 0 };
    } else if (exportReady && !exportMigration && advanceIndex) {
      const done = advanceIndex();
      exportMigration = { done: done ? 1 : 0 };
    }
    if (!exportMigration || Number(exportMigration.done) !== 1) return json({ error: 'search_indexing', partial: true, retry_after: 1 }, 503);
    return json(searchUiRecords(sql, input));
  }
  catch { return json({ error: 'search_unavailable', partial: true }, 503); }
}
