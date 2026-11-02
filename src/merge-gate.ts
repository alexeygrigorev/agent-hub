/** Merge gate (intake 2e00e01f, docs/MERGE-GATE.md): an intake with code work is
 * done only when its commits are reachable from origin/main AND the deploy for
 * that commit is green. Hub never runs git: hosts (and `hub intake merge-check`)
 * report evidence, Hub stores it per intake, gates the close on it, and an
 * indexed due-queue (state, due_at; no table scans) finds work that sits
 * unmerged with no activity for 60 minutes so it can be requeued. */
import type { SqlStorage } from '@cloudflare/workers-types';

type Obj = Record<string, any>;

export const MERGE_IDLE_MS = 60 * 60000;
export const MERGE_PROBE_MS = 15 * 60000;
export const MERGE_MAX_REQUEUES = 3;
export const MERGE_BATCH = 8;
export const MERGE_STATES = ['tracking', 'unmerged', 'requeued', 'blocked', 'merged', 'closed'] as const;
const DEPLOY_OK = new Set(['success', 'skipped', 'not_required']);
const DEPLOY_STATES = ['success', 'skipped', 'not_required', 'failure', 'pending', 'missing', 'unknown'];
const SHA = /^[0-9a-f]{7,40}$/i;
const MAX_COMMITS = 64;

export class MergeGateError extends Error {
  constructor(public code: string, public status = 400, public detail?: Obj) { super(code); }
}
const fail = (code: string, status = 400, detail?: Obj): never => { throw new MergeGateError(code, status, detail); };
const parsed = (text: unknown, fallback: any) => { try { return typeof text === 'string' ? JSON.parse(text) : fallback; } catch { return fallback; } };
const clip = (v: unknown, n: number): string | null => typeof v === 'string' && v.trim() ? v.trim().slice(0, n) : null;

export class MergeGate {
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS intake_merge_state (
      task_id TEXT PRIMARY KEY, project TEXT, state TEXT NOT NULL, machine TEXT, path TEXT, branch TEXT, head TEXT,
      unmerged INTEGER, commits_json TEXT NOT NULL DEFAULT '[]', deploy_state TEXT, deploy_json TEXT, evidence_at INTEGER,
      activity_at INTEGER NOT NULL, due_at INTEGER, probe_at INTEGER, flagged_at INTEGER, requeues INTEGER NOT NULL DEFAULT 0,
      requeue_at INTEGER, finish_session_id TEXT, blocker_id TEXT, updated_at INTEGER NOT NULL)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS intake_merge_state_due ON intake_merge_state(state, due_at)');
  }

  private row(taskId: string): Obj | undefined {
    return this.sql.exec('SELECT * FROM intake_merge_state WHERE task_id = ?', taskId).toArray()[0] as Obj | undefined;
  }

  view(r: Obj | undefined | null): Obj | null {
    if (!r) return null;
    const problems = this.problems(r);
    return { task_id: r.task_id, state: r.state, machine: r.machine ?? null, path: r.path ?? null, branch: r.branch ?? null,
      head: r.head ?? null, unmerged: r.unmerged ?? null, commits: parsed(r.commits_json, []), deploy: { state: r.deploy_state ?? null, ...parsed(r.deploy_json, {}) },
      evidence_at: r.evidence_at ?? null, activity_at: r.activity_at, flagged_at: r.flagged_at ?? null, requeues: r.requeues,
      requeue_at: r.requeue_at ?? null, finish_session_id: r.finish_session_id ?? null, blocker_id: r.blocker_id ?? null,
      next_check_at: r.due_at ?? null, problems };
  }

  /** What still stands between this intake and "merged and deployed". */
  private problems(r: Obj): string[] {
    const out: string[] = [];
    const commits: Obj[] = parsed(r.commits_json, []);
    const missing = commits.filter(c => c.in_main !== true).map(c => String(c.sha).slice(0, 12));
    if (missing.length) out.push(`commits not in origin/main: ${missing.join(', ')}`);
    else if (r.unmerged > 0) out.push(`${r.unmerged} commit(s) on the branch are not in origin/main`);
    else if (r.unmerged == null && !commits.length) out.push('merge state unknown (run hub intake merge-check)');
    if (!DEPLOY_OK.has(r.deploy_state)) out.push(`deploy not green: ${r.deploy_state ?? 'unverified'} (run hub intake merge-check)`);
    return out;
  }

  /** Close gate. Not applicable to intakes that never had tracked code work. */
  check(taskId: string): { applicable: boolean; ok: boolean; problems: string[]; state: string | null; commits: Obj[] } {
    const r = this.row(taskId);
    if (!r || r.state === 'closed') return { applicable: false, ok: true, problems: [], state: r?.state ?? null, commits: [] };
    const problems = this.problems(r);
    return { applicable: true, ok: problems.length === 0, problems, state: r.state, commits: parsed(r.commits_json, []) };
  }

  get(taskId: string): Obj | null { return this.view(this.row(taskId)); }

  /** Newest first; bounded by the partial state index, never a table scan. */
  unmerged(limit = 50): Obj[] {
    return (this.sql.exec(`SELECT * FROM intake_merge_state WHERE state IN ('unmerged','requeued','blocked') ORDER BY due_at DESC LIMIT ?`,
      Math.min(Math.max(limit, 1), 200)).toArray() as Obj[]).map(r => this.view(r)!);
  }
  counts(): Obj {
    const out: Obj = { unmerged: 0, requeued: 0, blocked: 0, tracking: 0 };
    for (const r of this.sql.exec(`SELECT state, COUNT(*) AS n FROM intake_merge_state WHERE state IN ('tracking','unmerged','requeued','blocked') GROUP BY state`).toArray() as Obj[]) out[r.state] = r.n;
    out.total_unmerged = out.unmerged + out.requeued + out.blocked;
    return out;
  }

  private settle(r: Obj, now: number) {
    const ok = this.problems(r).length === 0;
    const state = ok ? 'merged' : r.state === 'merged' || r.state === 'closed' ? 'tracking' : r.state;
    this.sql.exec('UPDATE intake_merge_state SET state = ?, due_at = ?, updated_at = ? WHERE task_id = ?',
      state, ok ? null : (r.due_at ?? now + MERGE_IDLE_MS), now, r.task_id);
  }

  /** A worktree was recorded for the intake: start tracking its branch head. */
  noteWorktree(taskId: string, project: string | null, w: { machine: string; path: string; branch?: string | null }) {
    const now = this.now();
    const r = this.row(taskId);
    if (!r) {
      this.sql.exec(`INSERT INTO intake_merge_state (task_id, project, state, machine, path, branch, activity_at, due_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)`, taskId, project, 'tracking', w.machine, w.path, w.branch ?? null, now, now + MERGE_IDLE_MS, now);
      return;
    }
    this.sql.exec(`UPDATE intake_merge_state SET machine = ?, path = ?, branch = COALESCE(?, branch), activity_at = ?, updated_at = ?,
      state = CASE WHEN state IN ('merged','closed') THEN 'tracking' ELSE state END,
      due_at = CASE WHEN state IN ('merged','closed') OR due_at IS NULL THEN ? ELSE due_at END WHERE task_id = ?`,
      w.machine, w.path, w.branch ?? null, now, now, now + MERGE_IDLE_MS, taskId);
  }

  /** Host worktree status result: the branch head and its unmerged count. */
  noteStatus(taskId: string, status: Obj) {
    const r = this.row(taskId);
    if (!r || !status) return;
    const now = this.now();
    const unmerged = Number.isInteger(status.unmerged) ? status.unmerged : status.merged === true ? 0 : r.unmerged;
    this.sql.exec('UPDATE intake_merge_state SET head = COALESCE(?, head), branch = COALESCE(?, branch), unmerged = ?, evidence_at = ?, updated_at = ? WHERE task_id = ?',
      clip(status.head, 64), clip(status.branch, 255), unmerged ?? null, now, now, taskId);
    this.settle(this.row(taskId)!, now);
  }

  /** Explicit evidence from `hub intake merge-check` (git ancestry + deploy run). */
  submit(taskId: string, project: string | null, b: Obj, actor: string, opts: { operator?: boolean } = {}): Obj {
    const allowed = ['key', 'head', 'branch', 'unmerged', 'commits', 'deploy', 'machine', 'path', 'idle'];
    if (b.idle !== undefined && (b.idle !== true || !opts.operator)) fail(b.idle === true ? 'operator_required' : 'invalid_field', b.idle === true ? 403 : 400);
    if (Object.keys(b).some(k => !allowed.includes(k))) fail('invalid_field');
    const commits: Obj[] = [];
    if (b.commits !== undefined) {
      if (!Array.isArray(b.commits) || b.commits.length > MAX_COMMITS) fail('invalid_commits');
      for (const c of b.commits) {
        if (!c || typeof c.sha !== 'string' || !SHA.test(c.sha) || typeof c.in_main !== 'boolean') fail('invalid_commits');
        commits.push({ sha: c.sha.toLowerCase(), in_main: c.in_main, ...(clip(c.subject, 160) ? { subject: clip(c.subject, 160) } : {}) });
      }
    }
    let deployState: string | null = null; let deployJson: Obj = {};
    if (b.deploy !== undefined) {
      if (!b.deploy || typeof b.deploy !== 'object' || !DEPLOY_STATES.includes(b.deploy.state)) fail('invalid_deploy');
      deployState = b.deploy.state;
      deployJson = { ...(clip(b.deploy.sha, 64) ? { sha: clip(b.deploy.sha, 64) } : {}), ...(clip(b.deploy.run, 300) ? { run: clip(b.deploy.run, 300) } : {}) };
    }
    if (b.unmerged !== undefined && !(Number.isInteger(b.unmerged) && b.unmerged >= 0 && b.unmerged < 1e6)) fail('invalid_unmerged');
    if (b.head !== undefined && !(typeof b.head === 'string' && SHA.test(b.head))) fail('invalid_head');
    const now = this.now();
    if (!this.row(taskId)) {
      this.sql.exec('INSERT INTO intake_merge_state (task_id, project, state, activity_at, due_at, updated_at) VALUES (?,?,?,?,?,?)',
        taskId, project, 'tracking', now, now + MERGE_IDLE_MS, now);
    }
    this.sql.exec(`UPDATE intake_merge_state SET head = COALESCE(?, head), branch = COALESCE(?, branch), machine = COALESCE(?, machine),
      path = COALESCE(?, path), unmerged = COALESCE(?, unmerged), commits_json = ?, deploy_state = COALESCE(?, deploy_state),
      deploy_json = CASE WHEN ? IS NULL THEN deploy_json ELSE ? END, evidence_at = ?, activity_at = ?, updated_at = ? WHERE task_id = ?`,
      b.head?.toLowerCase() ?? null, clip(b.branch, 255), clip(b.machine, 64), clip(b.path, 1024), b.unmerged ?? null,
      JSON.stringify(b.commits !== undefined ? commits : parsed(this.row(taskId)!.commits_json, [])),
      deployState, deployState, JSON.stringify(deployJson), now, now, now, taskId);
    // A report listing commits supersedes the cherry count.
    if (commits.length && b.unmerged === undefined) this.sql.exec('UPDATE intake_merge_state SET unmerged = ? WHERE task_id = ?', commits.filter(c => !c.in_main).length, taskId);
    // Backfill (operator): the work was already idle, so the 60 minute clock is spent.
    if (b.idle === true) this.sql.exec('UPDATE intake_merge_state SET activity_at = ?, due_at = CASE WHEN state IN (\'merged\',\'closed\') THEN due_at ELSE ? END WHERE task_id = ?', now - MERGE_IDLE_MS, now, taskId);
    this.settle(this.row(taskId)!, now);
    void actor;
    return this.get(taskId)!;
  }

  /** The intake was closed (gate passed or forced) or cancelled: stop watching. */
  closed(taskId: string) {
    this.sql.exec("UPDATE intake_merge_state SET state = CASE WHEN state = 'merged' THEN state ELSE 'closed' END, due_at = NULL, updated_at = ? WHERE task_id = ?", this.now(), taskId);
  }
  reopened(taskId: string) {
    const now = this.now();
    this.sql.exec("UPDATE intake_merge_state SET state = 'tracking', due_at = ?, activity_at = ?, updated_at = ? WHERE task_id = ? AND state IN ('merged','closed')",
      now + MERGE_IDLE_MS, now, now, taskId);
  }

  /** Due rows (indexed on state, due_at) for the watcher tick. */
  due(now = this.now()): Obj[] {
    return this.sql.exec(`SELECT * FROM intake_merge_state WHERE state IN ('tracking','requeued','blocked') AND due_at IS NOT NULL AND due_at <= ?
      ORDER BY due_at LIMIT ?`, now, MERGE_BATCH).toArray() as Obj[];
  }
  defer(taskId: string, at: number, extra: Obj = {}) {
    const sets = Object.keys(extra).map(k => `${k} = ?`).join(', ');
    this.sql.exec(`UPDATE intake_merge_state SET due_at = ?, updated_at = ?${sets ? ', ' + sets : ''} WHERE task_id = ?`, at, this.now(), ...Object.values(extra), taskId);
  }
  setState(taskId: string, state: string, extra: Obj = {}) {
    const sets = Object.keys(extra).map(k => `${k} = ?`).join(', ');
    this.sql.exec(`UPDATE intake_merge_state SET state = ?, updated_at = ?${sets ? ', ' + sets : ''} WHERE task_id = ?`, state, this.now(), ...Object.values(extra), taskId);
  }
  raw(taskId: string): Obj | undefined { return this.row(taskId); }
}

export type MergeGateHooks = {
  task: (id: string) => Obj | undefined;
  sessionStates: (ids: string[]) => string[];
  refresh: (taskId: string) => boolean;
  note: (task: Obj, text: string, key: string) => void;
  start: (input: Obj) => Promise<{ status: number; body: any }>;
  blocker: (task: Obj, title: string, text: string, key: string) => Promise<string | null>;
  blockerOpen: (id: string) => boolean;
  sessionIds: (taskId: string) => string[];
  alert: (subject: string, detail: string | null) => Promise<void>;
};
const LIVE = new Set(['queued', 'held', 'offered', 'starting', 'running', 'uncertain']);

export function finishBrief(task: Obj, r: Obj, problems: string[]): string {
  return `Finish-merge for Hub intake ${task.id} (${task.title ?? ''}). Hub flagged it unmerged: ${problems.join('; ')}. ` +
    `The work is in ${r.path ?? 'its intake worktree'} on branch ${r.branch ?? '?'} (host ${r.machine ?? '?'}). ` +
    `Do not redo the feature. 1) Rebase on fresh origin/main and resolve conflicts. 2) Run the FULL local test suite (scripts/test-full-local.sh). ` +
    `3) Get an independent review of the exact version (any model); if the reviewer fails with engine_auth_expired or similar, switch to another engine/account and open a prerequisite with hub intake blocker ${task.id}. ` +
    `4) Push to main yourself as soon as tests pass; never wait for orchestrator permission. 5) Watch CI and the Deploy workflow until green and fix failures yourself. ` +
    `6) Run hub intake merge-check ${task.id} so Hub records the evidence, then append the DONE note. If blocked (tests failing, auth expired, no capacity), run hub intake blocker ${task.id} --title ... --acceptance-criteria ... and stop.`;
}

/** One watcher pass: flags unmerged intakes idle >= 60 min and requeues them. */
export async function mergeGateTick(gate: MergeGate, hooks: MergeGateHooks, now = Date.now()): Promise<Obj> {
  const out = { checked: 0, flagged: 0, requeued: 0, blocked: 0, merged: 0, refreshed: 0 };
  for (const r of gate.due(now)) {
    out.checked++;
    const task = hooks.task(r.task_id);
    if (!task || ['done', 'cancelled'].includes(task.status)) { gate.closed(r.task_id); continue; }
    const check = gate.check(r.task_id);
    if (check.ok) { gate.setState(r.task_id, 'merged', { due_at: null }); out.merged++; continue; }
    if (r.state === 'blocked') {
      if (r.blocker_id && hooks.blockerOpen(r.blocker_id)) { gate.defer(r.task_id, now + MERGE_IDLE_MS); continue; }
      gate.setState(r.task_id, 'tracking', { due_at: now, requeues: Math.max(0, MERGE_MAX_REQUEUES - 1), blocker_id: null });
      continue;
    }
    const sessions = [...new Set([...hooks.sessionIds(r.task_id), ...(r.finish_session_id ? [r.finish_session_id] : [])])];
    if (hooks.sessionStates(sessions).some(s => LIVE.has(s))) { gate.defer(r.task_id, now + MERGE_IDLE_MS, { activity_at: now }); continue; }
    const last = r.activity_at;
    if (now - last < MERGE_IDLE_MS) { gate.defer(r.task_id, last + MERGE_IDLE_MS); continue; }
    // Stale or never-reported merge state: ask the host once, then fail closed.
    if (r.state === 'tracking' && (r.evidence_at == null || r.evidence_at < last) && !(r.probe_at && r.probe_at > last)) {
      if (hooks.refresh(r.task_id)) { out.refreshed++; gate.defer(r.task_id, now + MERGE_PROBE_MS, { probe_at: now }); continue; }
    }
    const fresh = gate.check(r.task_id);
    if (fresh.ok) { gate.setState(r.task_id, 'merged', { due_at: null }); out.merged++; continue; }
    if (r.state === 'tracking') {
      gate.setState(r.task_id, 'unmerged', { flagged_at: now });
      hooks.note(task, `UNMERGED: no session activity for ${Math.round((now - last) / 60000)} min and the work is not merged and deployed (${fresh.problems.join('; ')}). Hub is requeuing a finish-merge session.`, 'mg-flag-' + now);
      out.flagged++;
    }
    if (r.requeues >= MERGE_MAX_REQUEUES) {
      const title = `Unblock merge of intake ${task.id.slice(0, 8)}: ${String(task.title ?? '').slice(0, 120)}`;
      const text = `Hub requeued a finish-merge session ${r.requeues} times without the work reaching origin/main and a green deploy (${fresh.problems.join('; ')}). Find and fix the blocker, then run hub intake merge-check ${task.id}.`;
      let id: string | null = null;
      try { id = await hooks.blocker(task, title, text, 'mg-blocker-' + task.id); } catch { id = null; }
      gate.setState(r.task_id, 'blocked', { blocker_id: id, due_at: now + MERGE_IDLE_MS });
      await hooks.alert(`unmerged-${task.id}`, `Intake ${task.id} unmerged after ${r.requeues} finish-merge attempts; ${id ? 'prerequisite ' + id : 'prerequisite creation failed'}.`);
      out.blocked++;
      continue;
    }
    const key = `mg-${task.id.slice(0, 8)}-${r.requeues + 1}`;
    let result: { status: number; body: any };
    try {
      result = await hooks.start({ key, intake_task_id: task.id, mode: 'interactive', with_goal: true, workspace: task.project,
        ...(r.path ? { cwd: r.path } : {}), ...(r.machine ? { machine: r.machine } : {}), brief: finishBrief(task, r, fresh.problems) });
    } catch { result = { status: 502, body: { error: 'admission_outcome_unknown' } }; }
    const id = typeof result.body?.id === 'string' ? result.body.id : null;
    gate.setState(r.task_id, 'requeued', { requeues: r.requeues + 1, requeue_at: now, finish_session_id: id ?? r.finish_session_id,
      due_at: now + (id ? MERGE_IDLE_MS : MERGE_PROBE_MS) });
    hooks.note(task, id ? `Finish-merge session ${id} started (attempt ${r.requeues + 1}).` : `Finish-merge start refused (${String(result.body?.error ?? result.status).slice(0, 80)}); retrying.`, key);
    out.requeued++;
  }
  return out;
}
