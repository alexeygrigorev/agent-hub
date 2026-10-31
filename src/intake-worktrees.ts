/** Intake worktree registry, resume plan, close gate and host remove/status
 * commands (intake 4b161918, docs/INTAKE-WORKTREES.md).
 *
 * Hub only records and queues. Every git effect runs on the owning host
 * through the host agent; unknown host state stays null, never invented.
 * Nothing here deletes automatically (auto-GC belongs to intake 1f1f1877). */
import type { SqlStorage } from '@cloudflare/workers-types';

type Obj = Record<string, any>;

const ORIGINS = new Set(['hub_created', 'hub_resumed', 'hub_recreated', 'session_cwd', 'registered']);
const OBSERVE_ACTIONS: Record<string, string> = { created: 'hub_created', reused: 'hub_resumed', recreated: 'hub_recreated' };
const CLAIM_TIMEOUT_MS = 15 * 60000;
const MAX_ROWS_PER_TASK = 32;
const MAX_SESSIONS_PER_ROW = 64;
const MAX_PENDING_PER_MACHINE = 200;
const STATUS_KEYS = ['present', 'dirty', 'dirty_count', 'unpushed', 'unmerged', 'merged', 'head', 'branch', 'checked_at', 'error'];

export class WorktreeError extends Error {
  constructor(public code: string, public status = 400, public detail?: Obj) { super(code); }
}
function fail(code: string, status = 400, detail?: Obj): never { throw new WorktreeError(code, status, detail); }

const parsed = (text: unknown, fallback: any) => {
  if (typeof text !== 'string') return fallback;
  try { return JSON.parse(text); } catch { return fallback; }
};

/** Hub-side path guard, mirrored by the host guard (which also resolves
 * symlinks and $HOME): absolute, normalized, a direct child of a `git`
 * directory, basename containing `-wt-`, and never anything under `/prod`. */
export function worktreePathAllowed(path: unknown): path is string {
  if (typeof path !== 'string' || path.length < 3 || path.length > 1024) return false;
  if (!path.startsWith('/') || /[\0-\x1f\x7f]/.test(path) || path.includes('/prod')) return false;
  const parts = path.split('/');
  if (parts.some((p, i) => i > 0 && (p === '' || p === '.' || p === '..'))) return false;
  const base = parts[parts.length - 1];
  return parts.length >= 3 && parts[parts.length - 2] === 'git' && base.includes('-wt-');
}

/** A path naming an intake worktree anywhere (the tree itself, a subdirectory, a
 * relative segment): detached starts (79458f2d) must never run inside one. */
export function namesWorktree(path: unknown): boolean {
  return typeof path === 'string' && path.split(/[\\/]/).some(seg => seg.includes('-wt-'));
}

function branchOk(branch: unknown): branch is string {
  return typeof branch === 'string' && branch.length > 0 && branch.length <= 255 && /^[A-Za-z0-9._\/-]+$/.test(branch)
    && !branch.startsWith('-') && !branch.includes('..');
}

function cleanStatus(raw: any): Obj | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const out: Obj = {};
  for (const k of STATUS_KEYS) {
    const v = raw[k];
    if (['present', 'dirty', 'merged'].includes(k)) out[k] = typeof v === 'boolean' ? v : null;
    else if (['dirty_count', 'unpushed', 'unmerged'].includes(k)) out[k] = Number.isInteger(v) && v >= 0 && v < 1e7 ? v : null;
    else if (k === 'checked_at') out[k] = (typeof v === 'string' && v.length <= 64) || (Number.isInteger(v) && v > 0) ? v : null;
    else out[k] = typeof v === 'string' && v ? v.slice(0, k === 'error' ? 512 : 255) : null;
  }
  return out;
}

const shortReason = (v: unknown, fallback: string) => typeof v === 'string' && v.trim() ? v.trim().slice(0, 1024) : fallback;

export class IntakeWorktrees {
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_worktrees (
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, machine TEXT NOT NULL, path TEXT NOT NULL, branch TEXT,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, session_ids TEXT NOT NULL DEFAULT '[]',
      origin TEXT NOT NULL, state TEXT NOT NULL, status_json TEXT, status_at INTEGER, last_remove_json TEXT)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_worktrees_task ON intake_worktrees(task_id, created_at)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_worktrees_path ON intake_worktrees(machine, path)');
    // No sharing, enforced by storage too: one ACTIVE owner per (machine, path).
    this.sql.exec("CREATE UNIQUE INDEX IF NOT EXISTS intake_worktrees_active_owner ON intake_worktrees(machine, path) WHERE state = 'active'");
    this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_worktree_commands (
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worktree_id TEXT NOT NULL, machine TEXT NOT NULL, kind TEXT NOT NULL,
      state TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER, finished_at INTEGER, result_json TEXT)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_worktree_commands_machine ON intake_worktree_commands(machine, state, created_at)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_worktree_commands_worktree ON intake_worktree_commands(worktree_id, created_at)');
  }

  private rows(taskId: string): Obj[] {
    return this.sql.exec('SELECT * FROM intake_worktrees WHERE task_id = ? ORDER BY created_at, id', taskId).toArray() as Obj[];
  }
  private row(id: string): Obj | undefined {
    return this.sql.exec('SELECT * FROM intake_worktrees WHERE id = ?', id).toArray()[0] as Obj | undefined;
  }
  /** Active owner of (machine, path); machine null means any machine. */
  ownerOf(machine: string | null, path: string): Obj | undefined {
    return (machine
      ? this.sql.exec("SELECT * FROM intake_worktrees WHERE machine = ? AND path = ? AND state = 'active'", machine, path)
      : this.sql.exec("SELECT * FROM intake_worktrees WHERE path = ? AND state = 'active'", path)).toArray()[0] as Obj | undefined;
  }

  private commandView(c: Obj): Obj {
    return { id: c.id, kind: c.kind, state: c.state, worktree_id: c.worktree_id, task_id: c.task_id, machine: c.machine,
      actor: c.actor, created_at: c.created_at, claimed_at: c.claimed_at ?? null, finished_at: c.finished_at ?? null,
      result: parsed(c.result_json, null) };
  }
  view(r: Obj): Obj {
    const pending = (this.sql.exec("SELECT * FROM intake_worktree_commands WHERE worktree_id = ? AND state IN ('queued','claimed') ORDER BY created_at",
      r.id).toArray() as Obj[]).map(c => this.commandView(c));
    return { id: r.id, task_id: r.task_id, machine: r.machine, path: r.path, branch: r.branch ?? null, created_at: r.created_at,
      updated_at: r.updated_at, session_ids: parsed(r.session_ids, []), origin: r.origin, state: r.state,
      status: parsed(r.status_json, null), status_at: r.status_at ?? null, last_remove: parsed(r.last_remove_json, null), pending };
  }
  list(taskId: string): Obj[] { return this.rows(taskId).map(r => this.view(r)); }

  /** Close gate: every non-removed worktree must be known gone and merged. */
  gate(taskId: string): { ok: boolean; blocking: Obj[] } {
    const blocking: Obj[] = [];
    for (const r of this.rows(taskId)) {
      if (r.state !== 'active') continue;
      const s = parsed(r.status_json, null);
      const problems: string[] = [];
      if (!s) problems.push('status unknown (run hub worktree refresh)');
      else {
        if (s.present === true) problems.push('still present');
        else if (s.present !== false) problems.push('presence unknown');
        if (s.dirty === true) problems.push(s.dirty_count ? `dirty (${s.dirty_count} uncommitted changes)` : 'dirty');
        if (s.merged !== true) {
          if (s.unpushed) problems.push(`unpushed commits ${s.unpushed}`);
          if (s.unmerged) problems.push(`unmerged commits ${s.unmerged}`);
          else if (!s.unpushed) problems.push('merge state unknown');
        }
      }
      if (problems.length) blocking.push({ worktree_id: r.id, machine: r.machine, path: r.path, branch: r.branch ?? null,
        status_at: r.status_at ?? null, problems });
    }
    return { ok: blocking.length === 0, blocking };
  }

  /** Upsert this task's ownership of (machine, path). Refuses when another
   * intake actively owns it. Returns the row and whether it changed state. */
  record(taskId: string, input: { machine: string; path: string; branch?: string | null; session_id?: string | null; origin: string }): { row: Obj; fresh: boolean } {
    if (!worktreePathAllowed(input.path)) fail('worktree_path_refused', 400, { path: input.path,
      reason: 'only absolute ~/git/<name>-wt-<x> paths are recorded; never /prod' });
    if (typeof input.machine !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(input.machine)) fail('invalid_machine');
    if (input.branch != null && !branchOk(input.branch)) fail('invalid_branch');
    if (!ORIGINS.has(input.origin)) fail('invalid_origin');
    const now = this.now();
    const owner = this.ownerOf(input.machine, input.path);
    if (owner && owner.task_id !== taskId) fail('worktree_owned_by_other_intake', 409, { path: input.path, machine: input.machine, owner_task_id: owner.task_id });
    let row = owner ?? (this.sql.exec('SELECT * FROM intake_worktrees WHERE task_id = ? AND machine = ? AND path = ? ORDER BY updated_at DESC LIMIT 1',
      taskId, input.machine, input.path).toArray()[0] as Obj | undefined);
    let fresh = false;
    if (!row) {
      if (this.rows(taskId).length >= MAX_ROWS_PER_TASK) fail('worktree_capacity', 507);
      const id = crypto.randomUUID();
      this.sql.exec('INSERT INTO intake_worktrees (id, task_id, machine, path, branch, created_at, updated_at, session_ids, origin, state) VALUES (?,?,?,?,?,?,?,?,?,?)',
        id, taskId, input.machine, input.path, input.branch ?? null, now, now, '[]', input.origin, 'active');
      row = this.row(id)!; fresh = true;
    } else if (row.state !== 'active' || input.origin === 'hub_recreated') {
      // A removed/gone path used again, or the host recreated a missing one: active, status unknown.
      this.sql.exec("UPDATE intake_worktrees SET state = 'active', origin = ?, status_json = NULL, status_at = NULL, updated_at = ? WHERE id = ?",
        input.origin, now, row.id);
      fresh = true;
    }
    if (input.branch && input.branch !== row.branch) this.sql.exec('UPDATE intake_worktrees SET branch = ?, updated_at = ? WHERE id = ?', input.branch, now, row.id);
    if (input.session_id) {
      const ids: string[] = parsed(this.row(row.id)!.session_ids, []);
      if (!ids.includes(input.session_id) && ids.length < MAX_SESSIONS_PER_ROW) {
        ids.push(input.session_id);
        this.sql.exec('UPDATE intake_worktrees SET session_ids = ?, updated_at = ? WHERE id = ?', JSON.stringify(ids), now, row.id);
      }
    }
    return { row: this.row(row.id)!, fresh };
  }

  /** State of this intake's newest row for a path (any machine); null when never recorded. */
  lastStateFor(taskId: string, path: string): string | null {
    const r = this.sql.exec('SELECT state FROM intake_worktrees WHERE task_id = ? AND path = ? ORDER BY updated_at DESC LIMIT 1', taskId, path).toArray()[0] as Obj | undefined;
    return r ? String(r.state) : null;
  }

  /** Session start: refuse another intake's worktree; pin to the recorded host. */
  startGuard(taskId: string | null | undefined, request: { machine?: string | null; workspace?: string | null; cwd?: string | null; detached?: boolean }):
    { error?: Obj; machine?: string } {
    if (!taskId) return {}; // unlinked (operator/debug) starts are not intake work
    for (const candidate of [request.cwd, request.workspace]) {
      if (!worktreePathAllowed(candidate)) continue;
      const owner = this.ownerOf(request.machine ?? null, candidate);
      if (owner && owner.task_id !== taskId) return { error: { error: 'worktree_owned_by_other_intake', path: candidate,
        owner_task_id: owner.task_id, machine: owner.machine,
        detail: 'Two intakes never share a worktree; start this intake in its own worktree.' } };
    }
    // Detached starts (79458f2d) never use the intake's worktree, so they are not pinned to its host.
    if (request.detached || request.machine || request.cwd) return {};
    const pinned = this.sql.exec("SELECT machine FROM intake_worktrees WHERE task_id = ? AND state = 'active' ORDER BY updated_at DESC LIMIT 1",
      taskId).toArray()[0] as Obj | undefined;
    return pinned ? { machine: String(pinned.machine) } : {};
  }

  private launchContext(machine: string, launchId: unknown): { session: Obj; request: Obj } | null {
    if (typeof launchId !== 'string' || !/^[0-9a-f-]{36}$/.test(launchId)) fail('invalid_launch_id');
    const launch = this.sql.exec('SELECT machine_id FROM fleet_launches WHERE id = ?', launchId).toArray()[0] as Obj | undefined;
    if (!launch || launch.machine_id !== machine) fail('not_found', 404);
    const session = this.sql.exec('SELECT * FROM session_control_sessions WHERE fleet_launch_id = ? ORDER BY created_at DESC LIMIT 1', launchId).toArray()[0] as Obj | undefined;
    if (!session) return null;
    return { session, request: parsed(session.request_json, {}) };
  }

  /** Host asks which worktree an offered launch must use. */
  plan(machine: string, body: Obj): { plan: Obj; events: { taskId: string; event: Obj }[] } {
    const ctx = this.launchContext(machine, body?.launch_id);
    const taskId = ctx?.request?.intake_task_id;
    if (!ctx || typeof taskId !== 'string' || !taskId) return { plan: { mode: 'none' }, events: [] };
    const request = ctx.request;
    // Intake 79458f2d: a detached start runs in its workspace root and never
    // resumes, creates or locks the linked intake's worktree.
    if (request.intake_worktree === 'detached') return { plan: { mode: 'none', task_id: taskId, detached: true }, events: [] };
    for (const candidate of [request.cwd, request.workspace]) {
      if (!worktreePathAllowed(candidate)) continue;
      // The start named its own worktree: record it, launch as requested.
      try {
        const { row, fresh } = this.record(taskId, { machine, path: candidate, session_id: ctx.session.id, origin: 'session_cwd' });
        return { plan: { mode: 'none', task_id: taskId, worktree_id: row.id, recorded: candidate },
          events: fresh ? [{ taskId, event: { kind: 'worktree', action: 'recorded', worktree_id: row.id, machine, path: candidate, hub_session_id: ctx.session.id } }] : [] };
      } catch (e) {
        if (e instanceof WorktreeError) return { plan: { mode: 'none', task_id: taskId, refused: e.code }, events: [] };
        throw e;
      }
    }
    if (typeof request.cwd === 'string' && request.cwd) return { plan: { mode: 'none', task_id: taskId }, events: [] };
    const recorded = this.sql.exec("SELECT * FROM intake_worktrees WHERE task_id = ? AND machine = ? AND state = 'active' ORDER BY updated_at DESC LIMIT 1",
      taskId, machine).toArray()[0] as Obj | undefined;
    if (recorded) return { plan: { mode: 'resume', task_id: taskId, worktree_id: recorded.id, path: recorded.path, branch: recorded.branch ?? null }, events: [] };
    return { plan: { mode: 'create', task_id: taskId, suffix: 'wt-' + taskId.replace(/[^0-9a-f]/gi, '').slice(0, 8).toLowerCase() }, events: [] };
  }

  /** Host reports what it did for a launch's worktree. */
  observe(machine: string, body: Obj): { result: Obj; events: { taskId: string; event: Obj }[] } {
    const ctx = this.launchContext(machine, body?.launch_id);
    const taskId = ctx?.request?.intake_task_id;
    if (!ctx || typeof taskId !== 'string' || !taskId) return { result: { recorded: false, reason: 'no_intake' }, events: [] };
    const action = String(body?.action ?? '');
    if (action === 'failed') {
      const error = shortReason(body?.error, 'unknown error');
      return { result: { recorded: false, action }, events: [{ taskId, event: { kind: 'worktree', action: 'create_failed', machine,
        path: typeof body?.path === 'string' ? body.path.slice(0, 1024) : null, error, hub_session_id: ctx.session.id } }] };
    }
    const origin = OBSERVE_ACTIONS[action];
    if (!origin) fail('invalid_action');
    try {
      const { row, fresh } = this.record(taskId, { machine, path: body.path, branch: body.branch ?? null, session_id: ctx.session.id, origin });
      const events = fresh || action !== 'reused'
        ? [{ taskId, event: { kind: 'worktree', action, worktree_id: row.id, machine, path: row.path, branch: row.branch ?? null, hub_session_id: ctx.session.id } }]
        : [];
      return { result: { recorded: true, worktree: this.view(row) }, events };
    } catch (e) {
      if (!(e instanceof WorktreeError)) throw e;
      return { result: { recorded: false, error: e.code, ...(e.detail ?? {}) }, events: [{ taskId, event: { kind: 'worktree', action: 'record_refused',
        machine, path: typeof body?.path === 'string' ? body.path.slice(0, 1024) : null, error: e.code, hub_session_id: ctx.session.id } }] };
    }
  }

  /** Live/held Hub sessions bound to a worktree row (recorded ids or the same requested path). */
  private liveSessions(r: Obj): Obj[] {
    const ids: string[] = parsed(r.session_ids, []);
    const out: Obj[] = [];
    for (const s of this.sql.exec(`SELECT id, state, machine_id, request_json FROM session_control_sessions
        WHERE state IN ('queued','held','offered','starting','running','uncertain')
          AND (json_extract(request_json,'$.intake_task_id') = ? OR json_extract(request_json,'$.cwd') = ? OR json_extract(request_json,'$.workspace') = ?)`,
      r.task_id, r.path, r.path).toArray() as Obj[]) {
      const req = parsed(s.request_json, {});
      const samePath = req.cwd === r.path || req.workspace === r.path;
      // A live session of the same intake on the same host resumes in this worktree.
      const sameIntakeHost = req.intake_task_id === r.task_id && req.intake_worktree !== 'detached' && (!s.machine_id || s.machine_id === r.machine);
      if (ids.includes(s.id) || samePath || sameIntakeHost) out.push({ id: s.id, state: s.state });
    }
    return out;
  }

  private pending(worktreeId: string, kind: string): Obj | undefined {
    return this.sql.exec("SELECT * FROM intake_worktree_commands WHERE worktree_id = ? AND kind = ? AND state IN ('queued','claimed') LIMIT 1",
      worktreeId, kind).toArray()[0] as Obj | undefined;
  }

  /** Queue `status` or `remove` for the task's worktrees (one id, or all active). */
  enqueue(taskId: string, kind: 'status' | 'remove', worktreeId: string | null | undefined, actor: string):
    { commands: Obj[]; refused: Obj[]; events: Obj[] } {
    const now = this.now();
    let targets = this.rows(taskId);
    if (worktreeId) {
      targets = targets.filter(r => r.id === worktreeId);
      if (!targets.length) fail('unknown_worktree', 404);
    } else targets = targets.filter(r => r.state === 'active');
    if (!targets.length) fail('no_active_worktree', 404);
    const commands: Obj[] = []; const refused: Obj[] = []; const events: Obj[] = [];
    for (const r of targets) {
      if (kind === 'remove') {
        let reason: string | null = null;
        if (r.state !== 'active') reason = `already ${r.state}`;
        else if (!worktreePathAllowed(r.path)) reason = 'path guard: only ~/git/<name>-wt-<x>, never /prod';
        else {
          const live = this.liveSessions(r);
          if (live.length) reason = 'live session bound: ' + live.map(s => `${s.id} (${s.state})`).join(', ');
        }
        if (reason) {
          const lastRemove = { result: 'refused', reason, at: now, actor, by: 'hub' };
          this.sql.exec('UPDATE intake_worktrees SET last_remove_json = ?, updated_at = ? WHERE id = ?', JSON.stringify(lastRemove), now, r.id);
          refused.push({ worktree_id: r.id, path: r.path, machine: r.machine, result: 'refused', reason });
          events.push({ kind: 'worktree', action: 'remove', result: 'refused', reason, worktree_id: r.id, machine: r.machine, path: r.path });
          continue;
        }
      }
      const existing = this.pending(r.id, kind);
      if (existing) { commands.push(this.commandView(existing)); continue; }
      const queued = (this.sql.exec("SELECT COUNT(*) AS n FROM intake_worktree_commands WHERE machine = ? AND state IN ('queued','claimed')", r.machine).toArray()[0] as Obj).n;
      if (queued >= MAX_PENDING_PER_MACHINE) fail('worktree_command_capacity', 507);
      const id = crypto.randomUUID();
      this.sql.exec('INSERT INTO intake_worktree_commands (id, task_id, worktree_id, machine, kind, state, actor, created_at) VALUES (?,?,?,?,?,?,?,?)',
        id, taskId, r.id, r.machine, kind, 'queued', actor, now);
      commands.push(this.commandView(this.sql.exec('SELECT * FROM intake_worktree_commands WHERE id = ?', id).toArray()[0] as Obj));
    }
    return { commands, refused, events };
  }

  /** Worktree rows linked to a Hub session (for `hub worktree remove <session>`). */
  bySession(sessionId: string): { taskId: string; worktreeIds: string[] } | null {
    const session = this.sql.exec('SELECT request_json FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0] as Obj | undefined;
    const rows = (this.sql.exec("SELECT r.id, r.task_id FROM intake_worktrees r, json_each(r.session_ids) j WHERE j.value = ?", sessionId).toArray() as Obj[]);
    if (rows.length) return { taskId: String(rows[0].task_id), worktreeIds: rows.filter(r => r.task_id === rows[0].task_id).map(r => String(r.id)) };
    const taskId = parsed(session?.request_json, {}).intake_task_id;
    return typeof taskId === 'string' && taskId ? { taskId, worktreeIds: [] } : null;
  }

  command(id: string): Obj | undefined {
    const c = this.sql.exec('SELECT * FROM intake_worktree_commands WHERE id = ?', id).toArray()[0] as Obj | undefined;
    return c ? this.commandView(c) : undefined;
  }

  /** Host poll: claim queued commands (re-offer stale claims). */
  hostCommands(machine: string): Obj[] {
    const now = this.now();
    const rows = this.sql.exec(`SELECT c.*, w.path, w.branch FROM intake_worktree_commands c JOIN intake_worktrees w ON w.id = c.worktree_id
      WHERE c.machine = ? AND (c.state = 'queued' OR (c.state = 'claimed' AND c.claimed_at < ?)) ORDER BY c.created_at LIMIT 10`,
      machine, now - CLAIM_TIMEOUT_MS).toArray() as Obj[];
    for (const c of rows) this.sql.exec("UPDATE intake_worktree_commands SET state = 'claimed', claimed_at = ? WHERE id = ?", now, c.id);
    return rows.map(c => ({ id: c.id, kind: c.kind, worktree_id: c.worktree_id, task_id: c.task_id, path: c.path, branch: c.branch ?? null }));
  }

  /** Host result for one command. Returns history events for the task. */
  hostResult(machine: string, id: string, body: Obj): { command: Obj; events: { taskId: string; event: Obj }[] } {
    const c = this.sql.exec('SELECT * FROM intake_worktree_commands WHERE id = ? AND machine = ?', id, machine).toArray()[0] as Obj | undefined;
    if (!c) fail('not_found', 404);
    if (c.state === 'done' || c.state === 'failed') return { command: this.commandView(c), events: [] }; // replay
    const outcome = String(body?.outcome ?? '');
    if (!['ok', 'refused', 'error'].includes(outcome)) fail('invalid_outcome');
    const now = this.now();
    const status = cleanStatus(body?.status);
    const reason = outcome === 'ok' ? null : shortReason(body?.reason, outcome);
    const result: Obj = { outcome, reason, status, removed: body?.removed === true, branch_deleted: body?.branch_deleted === true,
      ...(typeof body?.branch_reason === 'string' ? { branch_reason: body.branch_reason.slice(0, 512) } : {}) };
    const r = this.row(c.worktree_id);
    const events: { taskId: string; event: Obj }[] = [];
    if (r) {
      if (status) {
        const gone = status.present === false && status.merged === true && r.state === 'active';
        this.sql.exec('UPDATE intake_worktrees SET status_json = ?, status_at = ?, state = ?, updated_at = ? WHERE id = ?',
          JSON.stringify(status), now, gone ? 'gone' : r.state, now, r.id);
      }
      if (c.kind === 'remove') {
        const removed = outcome === 'ok' && result.removed;
        const lastRemove = { result: removed ? 'ok' : outcome === 'ok' ? 'error' : outcome, reason: removed ? null : (reason ?? 'not removed'),
          at: now, actor: c.actor, by: 'host', branch_deleted: result.branch_deleted, ...(result.branch_reason ? { branch_reason: result.branch_reason } : {}) };
        this.sql.exec('UPDATE intake_worktrees SET last_remove_json = ?, updated_at = ? WHERE id = ?', JSON.stringify(lastRemove), now, r.id);
        if (removed) {
          // Removal ran only after the host verified clean + merged; record that.
          const finalStatus = { ...(status ?? {}), present: false, merged: true, dirty: false, checked_at: status?.checked_at ?? new Date(now).toISOString() };
          this.sql.exec("UPDATE intake_worktrees SET state = 'removed', status_json = ?, status_at = ?, updated_at = ? WHERE id = ?",
            JSON.stringify(cleanStatus(finalStatus)), now, now, r.id);
        }
        events.push({ taskId: c.task_id, event: { kind: 'worktree', action: 'remove', result: lastRemove.result, reason: lastRemove.reason,
          branch_deleted: result.branch_deleted, worktree_id: r.id, machine, path: r.path, command_id: c.id, requested_by: c.actor } });
      }
    }
    this.sql.exec('UPDATE intake_worktree_commands SET state = ?, finished_at = ?, result_json = ? WHERE id = ?',
      outcome === 'error' ? 'failed' : 'done', now, JSON.stringify(result), c.id);
    return { command: this.command(c.id)!, events };
  }
}
