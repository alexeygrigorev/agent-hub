/**
 * Scheduled checks: the generic form of the competition's routine agent. Each check runs on an
 * interval from the Worker cron, evaluates a condition and, when it fails (or always, for a
 * routine), takes one action: open an alert, send a hub inbox message, or start an agent through
 * the normal fleet placement path. Checks are configuration, never shell commands.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { LivenessService } from './liveness';

export const CHECKS_SCHEMA = `
CREATE TABLE IF NOT EXISTS hub_checks (name TEXT PRIMARY KEY, kind TEXT NOT NULL, interval_s INTEGER NOT NULL, params_json TEXT NOT NULL, action_json TEXT NOT NULL, enabled INTEGER NOT NULL, next_due_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT, last_detail TEXT);
CREATE TABLE IF NOT EXISTS hub_check_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, action TEXT);
`;

export const CHECK_KINDS = ['liveness', 'quota', 'http', 'inbox_backlog', 'routine'] as const;
const NAME = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const LABEL = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const TEXT = (v: unknown, max: number) => typeof v === 'string' && v.length <= max && !/[\x00-\x08\x0b-\x1f\x7f]/.test(v);
const plain = (v: any, keys: string[]) => v !== null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).every(k => keys.includes(k));

export type CheckResult = { status: 'pass' | 'fail' | 'error'; detail: string };
export type ActionDeps = {
  liveness: LivenessService;
  notify: (alert: any) => Promise<void>;
  message: (to: string, body: string, key: string) => Promise<string>;
  launch: (name: string, request: any, key: string) => Promise<string>;
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  evaluateQuota: () => any[];
};

/** Validate one check definition; returns a normalized row or an error code. */
export function validateCheck(c: any): { row: any } | { error: string } {
  if (!plain(c, ['name', 'kind', 'interval_s', 'params', 'action', 'enabled'])) return { error: 'invalid_check' };
  if (typeof c.name !== 'string' || !NAME.test(c.name)) return { error: 'invalid_check_name' };
  if (!CHECK_KINDS.includes(c.kind)) return { error: 'invalid_check_kind' };
  if (!Number.isInteger(c.interval_s) || c.interval_s < 300 || c.interval_s > 7 * 86400) return { error: 'invalid_interval' };
  const params = c.params ?? {};
  if (!plain(params, ['url', 'expect_status', 'min_percent', 'actor', 'max_age_s'])) return { error: 'invalid_params' };
  if (c.kind === 'http') {
    let url: URL | null = null; try { url = new URL(params.url); } catch {}
    if (!url || url.protocol !== 'https:' || url.username || url.password || params.url.length > 512) return { error: 'invalid_http_url' };
    if (params.expect_status !== undefined && (!Number.isInteger(params.expect_status) || params.expect_status < 100 || params.expect_status > 599)) return { error: 'invalid_expect_status' };
  }
  if (c.kind === 'quota' && params.min_percent !== undefined && (typeof params.min_percent !== 'number' || params.min_percent < 0 || params.min_percent > 100)) return { error: 'invalid_min_percent' };
  if (c.kind === 'inbox_backlog' && (typeof params.actor !== 'string' || !TEXT(params.actor, 128) || !Number.isInteger(params.max_age_s) || params.max_age_s < 60)) return { error: 'invalid_backlog_params' };
  const action = c.action ?? { type: 'alert' };
  if (!plain(action, ['type', 'to', 'body', 'workspace', 'prompt', 'engine', 'machine', 'mode', 'summary', 'on'])) return { error: 'invalid_action' };
  if (!['alert', 'message', 'launch'].includes(action.type)) return { error: 'invalid_action_type' };
  if (action.on !== undefined && !['fail', 'always'].includes(action.on)) return { error: 'invalid_action_on' };
  if (c.kind === 'routine' && action.type === 'alert') return { error: 'routine_needs_message_or_launch' };
  if (action.type === 'message' && (typeof action.to !== 'string' || !TEXT(action.to, 128) || !TEXT(action.body, 4000) || !action.body)) return { error: 'invalid_message_action' };
  if (action.type === 'launch') {
    if (typeof action.workspace !== 'string' || !LABEL.test(action.workspace) || !TEXT(action.prompt, 16000) || !action.prompt?.trim()) return { error: 'invalid_launch_action' };
    if ((action.engine !== undefined && !LABEL.test(action.engine)) || (action.machine !== undefined && !LABEL.test(action.machine)) || (action.mode !== undefined && action.mode !== 'headless') || (action.summary !== undefined && !TEXT(action.summary, 240))) return { error: 'invalid_launch_action' };
  }
  if (typeof c.enabled !== 'boolean' && c.enabled !== undefined) return { error: 'invalid_enabled' };
  return { row: { name: c.name, kind: c.kind, interval_s: c.interval_s, params, action: { on: c.kind === 'routine' ? 'always' : 'fail', ...action }, enabled: c.enabled !== false } };
}

export const DEFAULT_CHECKS = [
  { name: 'liveness', kind: 'liveness', interval_s: 300, params: {}, action: { type: 'alert' }, enabled: true },
];

export class ChecksService {
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}

  init() {
    for (const stmt of CHECKS_SCHEMA.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    if (!Number(this.sql.exec('SELECT COUNT(*) AS n FROM hub_checks').one().n)) this.replace(DEFAULT_CHECKS.map(c => (validateCheck(c) as any).row));
  }

  replace(rows: any[]) {
    const existing = new Map((this.sql.exec('SELECT name, next_due_at, last_run_at, last_status, last_detail FROM hub_checks').toArray() as any[]).map(r => [r.name, r]));
    this.sql.exec('DELETE FROM hub_checks');
    const at = new Date(this.now()).toISOString();
    for (const r of rows) {
      const prior: any = existing.get(r.name);
      this.sql.exec('INSERT INTO hub_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', r.name, r.kind, r.interval_s, JSON.stringify(r.params), JSON.stringify(r.action), Number(r.enabled),
        prior?.next_due_at ?? at, prior?.last_run_at ?? null, prior?.last_status ?? null, prior?.last_detail ?? null);
    }
  }

  list() {
    return (this.sql.exec('SELECT * FROM hub_checks ORDER BY name').toArray() as any[]).map(r => ({
      name: r.name, kind: r.kind, interval_s: r.interval_s, params: JSON.parse(r.params_json), action: JSON.parse(r.action_json), enabled: !!r.enabled,
      next_due_at: r.next_due_at, last_run_at: r.last_run_at, last_status: r.last_status, last_detail: r.last_detail,
    }));
  }

  runs(limit = 50) {
    return this.sql.exec('SELECT * FROM hub_check_runs ORDER BY id DESC LIMIT ?', limit).toArray();
  }

  /** Run every enabled check whose next_due_at has passed (or one named check when forced). */
  async runDue(deps: ActionDeps, only?: string): Promise<any[]> {
    const now = this.now(); const at = new Date(now).toISOString();
    const due = (this.sql.exec('SELECT * FROM hub_checks WHERE enabled = 1 OR name = ? ORDER BY name', only ?? '').toArray() as any[])
      .filter(r => only ? r.name === only : Date.parse(r.next_due_at) <= now);
    const results: any[] = [];
    for (const row of due) {
      // Claim the slot before any effect so an overlapping cron cannot run the same check twice.
      const slot = Math.floor(now / (row.interval_s * 1000));
      this.sql.exec('UPDATE hub_checks SET next_due_at = ?, last_run_at = ? WHERE name = ?', new Date((slot + 1) * row.interval_s * 1000).toISOString(), at, row.name);
      const check = { ...row, params: JSON.parse(row.params_json), action: JSON.parse(row.action_json) };
      let result: CheckResult; let action: string | null = null;
      try { result = await this.evaluate(check, deps); }
      catch (e: any) { result = { status: 'error', detail: 'check failed: ' + String(e?.message || e).slice(0, 200) }; }
      try { action = await this.act(check, result, deps, `${row.name}-${slot}`); }
      catch (e: any) { action = 'action failed: ' + String(e?.message || e).slice(0, 200); }
      this.sql.exec('UPDATE hub_checks SET last_status = ?, last_detail = ? WHERE name = ?', result.status, result.detail.slice(0, 1000), row.name);
      this.sql.exec('INSERT INTO hub_check_runs (name, at, status, detail, action) VALUES (?, ?, ?, ?, ?)', row.name, at, result.status, result.detail.slice(0, 1000), action);
      results.push({ name: row.name, ...result, action });
    }
    this.sql.exec('DELETE FROM hub_check_runs WHERE id <= (SELECT id FROM hub_check_runs ORDER BY id DESC LIMIT 1 OFFSET 1000)');
    return results;
  }

  private async evaluate(check: any, deps: ActionDeps): Promise<CheckResult> {
    const p = check.params;
    switch (check.kind) {
      case 'liveness': {
        const { subjects } = deps.liveness.evaluate();
        // Liveness keeps one alert per subject; the check itself only summarizes.
        for (const alert of deps.liveness.reconcileAlerts(subjects)) await deps.notify(alert);
        const bad = subjects.filter(s => ['stale', 'offline', 'stalled', 'stuck', 'uncertain', 'missing'].includes(s.state));
        return bad.length ? { status: 'fail', detail: bad.map(s => `${s.kind} ${s.id}: ${s.state}`).join('; ') } : { status: 'pass', detail: `${subjects.length} subjects healthy or idle` };
      }
      case 'quota': {
        const min = p.min_percent ?? 20;
        const low = deps.evaluateQuota().filter(a => a.enabled && (a.status === 'blocked' || (typeof a.remaining_percent === 'number' && a.remaining_percent < min)));
        return low.length ? { status: 'fail', detail: low.map(a => `${a.label}: ${a.status}, ${a.remaining_percent ?? 'unknown'}% left`).join('; ') } : { status: 'pass', detail: `no account below ${min}%` };
      }
      case 'http': {
        const expected = p.expect_status ?? 200;
        const r = await deps.fetch(p.url, { method: 'GET', redirect: 'manual', signal: AbortSignal.timeout(10000) });
        return r.status === expected ? { status: 'pass', detail: `HTTP ${r.status}` } : { status: 'fail', detail: `HTTP ${r.status}, expected ${expected}` };
      }
      case 'inbox_backlog': {
        const cutoff = this.now() - p.max_age_s * 1000;
        const n = Number(this.sql.exec('SELECT COUNT(*) AS n FROM messages WHERE to_actor = ? AND acked_at IS NULL AND created_at < ? AND expires_at > ?', p.actor, cutoff, this.now()).one().n);
        return n ? { status: 'fail', detail: `${n} unacknowledged message(s) for ${p.actor} older than ${p.max_age_s}s` } : { status: 'pass', detail: `inbox of ${p.actor} is current` };
      }
      case 'routine':
        return { status: 'pass', detail: 'routine due' };
    }
    return { status: 'error', detail: 'unknown check kind' };
  }

  private async act(check: any, result: CheckResult, deps: ActionDeps, key: string): Promise<string | null> {
    const action = check.action; const failing = result.status !== 'pass';
    if (action.type === 'alert') {
      if (check.kind === 'liveness') return null; // per-subject alerts were reconciled during evaluation
      const opened = deps.liveness.setAlert('check', check.name, failing ? { state: result.status, detail: result.detail } : null);
      if (opened) { await deps.notify(opened); return 'alert opened'; }
      return failing ? 'alert already open' : null;
    }
    if (action.on !== 'always' && !failing) return null;
    if (action.type === 'message') {
      const body = action.body.replaceAll('{detail}', result.detail).replaceAll('{check}', check.name);
      return 'message ' + await deps.message(action.to, body, key);
    }
    const request: any = { workspace: action.workspace, prompt: action.prompt.replaceAll('{detail}', result.detail).replaceAll('{check}', check.name), summary: action.summary ?? `Scheduled: ${check.name}`, mode: 'headless' };
    if (action.engine) request.engine = action.engine;
    if (action.machine) request.machine = action.machine;
    return 'launch ' + await deps.launch(check.name, request, key);
  }
}
