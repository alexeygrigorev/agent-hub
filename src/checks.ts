/**
 * Scheduled checks: the generic form of the competition's routine agent. Each check runs on an
 * interval from the Worker cron, evaluates a condition and, when it fails (or always, for a
 * routine), takes one action: open an alert, send a hub inbox message, or start an agent through
 * the normal fleet placement path. Checks are configuration, never shell commands.
 */
import type { ConversationCheckPort, FollowUpIntent } from './conversation-automation';
import { CHECK_SLOT_SCHEMA } from './conversation-automation';
import { registryAddress } from './agent_registry';
import type { SqlStorage } from '@cloudflare/workers-types';
import type { LivenessService } from './liveness';

export const CHECKS_SCHEMA = `
CREATE TABLE IF NOT EXISTS hub_checks (name TEXT PRIMARY KEY, kind TEXT NOT NULL, interval_s INTEGER NOT NULL, params_json TEXT NOT NULL, action_json TEXT NOT NULL, enabled INTEGER NOT NULL, next_due_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT, last_detail TEXT);
CREATE TABLE IF NOT EXISTS hub_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hub_check_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, action TEXT);
` + CHECK_SLOT_SCHEMA;

export const CHECK_KINDS = ['liveness', 'quota', 'http', 'inbox_backlog', 'conversation_backlog', 'routine'] as const;
// 40 characters keeps check-<name>-<slot> within the 64-character fleet idempotency key.
const NAME = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const LABEL = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const TEXT = (v: unknown, max: number) => typeof v === 'string' && v.length <= max && !/[\x00-\x08\x0b-\x1f\x7f]/.test(v);
const plain = (v: any, keys: string[]) => v !== null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).every(k => keys.includes(k));
const jsonBytes = (v: unknown) => new TextEncoder().encode(JSON.stringify(v)).byteLength;
const MAX_CHECK_BYTES = 128 * 1024;
const MAX_RESULT_BYTES = 64 * 1024;
const MAX_INTENT_BYTES = 64 * 1024;

const safeId = (v: unknown): v is string => typeof v === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(v);
const canonicalTarget = (v: unknown): v is string => typeof v === 'string' && (/^human:[A-Za-z0-9._:-]{1,120}$/.test(v) || registryAddress(v) !== null);

export type CheckResult = { status: 'pass' | 'fail' | 'error'; detail: string };
export type ActionDeps = {
  conversation?: ConversationCheckPort;
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
  if (!plain(params, ['url', 'expect_status', 'min_percent', 'actor', 'max_age_s', 'conversation_id'])) return { error: 'invalid_params' };
  if (c.kind === 'http') {
    let url: URL | null = null; try { url = new URL(params.url); } catch {}
    if (!url || url.protocol !== 'https:' || url.username || url.password || params.url.length > 512) return { error: 'invalid_http_url' };
    if (params.expect_status !== undefined && (!Number.isInteger(params.expect_status) || params.expect_status < 100 || params.expect_status > 599)) return { error: 'invalid_expect_status' };
  }
  if (c.kind === 'quota' && params.min_percent !== undefined && (typeof params.min_percent !== 'number' || params.min_percent < 0 || params.min_percent > 100)) return { error: 'invalid_min_percent' };
  if (c.kind === 'inbox_backlog' && (typeof params.actor !== 'string' || !TEXT(params.actor, 128) || !Number.isInteger(params.max_age_s) || params.max_age_s < 60)) return { error: 'invalid_backlog_params' };
  if (c.kind === 'conversation_backlog') {
    if (!canonicalTarget(params.actor) || !Number.isSafeInteger(params.max_age_s) || params.max_age_s < 60 || params.max_age_s > 604800) return { error: 'invalid_backlog_params' };
    if (params.conversation_id !== undefined && !safeId(params.conversation_id)) return { error: 'invalid_conversation_id' };
  } else if (params.conversation_id !== undefined) return { error: 'invalid_params' };
  const action = c.action ?? { type: 'alert' };
  if (!plain(action, ['type', 'to', 'body', 'workspace', 'prompt', 'engine', 'machine', 'mode', 'summary', 'on', 'conversation_id', 'grant_id'])) return { error: 'invalid_action' };
  if (!['alert', 'message', 'launch'].includes(action.type)) return { error: 'invalid_action_type' };
  if (action.on !== undefined && !['fail', 'always'].includes(action.on)) return { error: 'invalid_action_on' };
  if (c.kind === 'routine' && action.type === 'alert') return { error: 'routine_needs_message_or_launch' };
  if (action.type === 'message' && (typeof action.to !== 'string' || !TEXT(action.to, 128) || !TEXT(action.body, 4000) || !action.body)) return { error: 'invalid_message_action' };
  if (action.conversation_id !== undefined || action.grant_id !== undefined) {
    if (action.type !== 'message' || !safeId(action.conversation_id) || !safeId(action.grant_id) || !canonicalTarget(action.to)) return { error: 'invalid_conversation_action' };
    if (c.kind === 'conversation_backlog' && (params.actor !== action.to || (params.conversation_id && params.conversation_id !== action.conversation_id))) return { error: 'conversation_target_mismatch' };
  }
  if (action.type === 'launch') {
    if (typeof action.workspace !== 'string' || !LABEL.test(action.workspace) || !TEXT(action.prompt, 16000) || !action.prompt?.trim()) return { error: 'invalid_launch_action' };
    if ((action.engine !== undefined && !LABEL.test(action.engine)) || (action.machine !== undefined && !LABEL.test(action.machine)) || (action.mode !== undefined && action.mode !== 'headless') || (action.summary !== undefined && !TEXT(action.summary, 240))) return { error: 'invalid_launch_action' };
  }
  if (typeof c.enabled !== 'boolean' && c.enabled !== undefined) return { error: 'invalid_enabled' };
  const row = { name: c.name, kind: c.kind, interval_s: c.interval_s, params, action: { on: c.kind === 'routine' ? 'always' : 'fail', ...action }, enabled: c.enabled !== false };
  if (jsonBytes(row) > MAX_CHECK_BYTES) return { error: 'check_too_large' };
  return { row };
}

export const DEFAULT_CHECKS = [
  { name: 'liveness', kind: 'liveness', interval_s: 300, params: {}, action: { type: 'alert' }, enabled: true },
];

export class ChecksService {
  constructor(private sql: SqlStorage, private now: () => number = Date.now) {}

  init() {
    for (const stmt of CHECKS_SCHEMA.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    // Seed defaults once; an operator who later removes every check keeps an empty list.
    if (!this.sql.exec("SELECT 1 FROM hub_meta WHERE key = 'checks_seeded'").toArray().length) {
      if (!Number(this.sql.exec('SELECT COUNT(*) AS n FROM hub_checks').one().n)) this.replace(DEFAULT_CHECKS.map(c => (validateCheck(c) as any).row));
      this.sql.exec("INSERT INTO hub_meta (key, value) VALUES ('checks_seeded', ?)", new Date(this.now()).toISOString());
    }
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

  /** The latest durable slot per name survives replacement and restarts. SQL
   * statements before each await are atomic in the single Durable Object. A CAS
   * token fences an evaluator whose bounded lease expired; message effects also
   * have transactional idempotency and a retained receipt in ConversationsService. */
  async runDue(deps: ActionDeps, only?: string): Promise<any[]> {
    const results: any[] = [];
    const names = (this.sql.exec('SELECT name FROM hub_checks WHERE enabled = 1 OR name = ? ORDER BY name', only ?? '').toArray()).map(r => String(r.name));
    for (const name of names) {
      if (only && name !== only) continue;
      const now = this.now(); const at = new Date(now).toISOString();
      // Re-read after preceding awaited checks: never execute stale due snapshots.
      const row = this.sql.exec('SELECT * FROM hub_checks WHERE name = ?', name).toArray()[0] as any;
      if (!row || (!row.enabled && only !== name)) continue;
      let slot = this.sql.exec('SELECT * FROM hub_check_slots WHERE name = ?', name).toArray()[0] as any;
      if (slot?.state === 'done' && now < slot.next_at) {
        if (only) results.push({ name, ...JSON.parse(slot.result_json), action: slot.action, replay: true });
        continue;
      }
      if (slot && slot.state !== 'done' && Number(slot.lease_until) > now) continue;
      const token = crypto.randomUUID();
      if (!slot || slot.state === 'done') {
        if (!only && Date.parse(row.next_due_at) > now) continue;
        if (!slot && Number(this.sql.exec('SELECT COUNT(*) AS n FROM hub_check_slots').one().n) >= 1000) throw new Error('check_slot_capacity');
        const n = Math.floor(now / (row.interval_s * 1000));
        const nextAt = (n + 1) * row.interval_s * 1000;
        // Freeze the definition, without duplicating the SQL JSON columns or
        // mutable status metadata. Existing R2 reservations are never rewritten.
        const check = { name, kind: row.kind, interval_s: row.interval_s, params: JSON.parse(row.params_json), action: JSON.parse(row.action_json) };
        if (jsonBytes(check) > MAX_CHECK_BYTES) {
          this.sql.exec("UPDATE hub_checks SET last_status='error', last_detail='check_too_large' WHERE name=?", name);
          results.push({ name, status: 'error', detail: 'check_too_large', action: null });
          continue;
        }
        this.sql.exec(`INSERT INTO hub_check_slots VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'evaluating', ?, ?)
          ON CONFLICT(name) DO UPDATE SET slot_key=excluded.slot_key, next_at=excluded.next_at, check_json=excluded.check_json, result_json=NULL, intent_json=NULL, action=NULL, state='evaluating', token=excluded.token, lease_until=excluded.lease_until`,
          name, `${name}-${check.action.conversation_id ? crypto.randomUUID() : n}`, nextAt, JSON.stringify(check), token, now + 30000);
        this.sql.exec('UPDATE hub_checks SET next_due_at = ?, last_run_at = ? WHERE name = ?', new Date(nextAt).toISOString(), at, name);
      } else {
        this.sql.exec('UPDATE hub_check_slots SET token = ?, lease_until = ? WHERE name = ? AND (token IS NULL OR lease_until <= ?)', token, now + 30000, name, now);
      }
      slot = this.sql.exec('SELECT * FROM hub_check_slots WHERE name = ?', name).toArray()[0] as any;
      if (slot.token !== token) continue;
      const check = JSON.parse(slot.check_json);
      let result: CheckResult; let action: string | null = null;
      let intent: FollowUpIntent | null = slot.intent_json ? JSON.parse(slot.intent_json) : null;
      if (slot.result_json) result = JSON.parse(slot.result_json);
      else {
        try { result = await this.evaluate(check, deps); }
        catch (e: unknown) { result = { status: 'error', detail: 'check failed: ' + errorText(e) }; }
        if (jsonBytes(result) > MAX_RESULT_BYTES) result = { status: 'error', detail: 'check_result_too_large' };
        if (check.action.conversation_id && result.status !== 'error' && (check.action.on === 'always' || result.status === 'fail')) {
          intent = { actor: check.action.to, checkName: name, conversationId: check.action.conversation_id,
            grantId: check.action.grant_id, bodyTemplate: check.action.body, on: check.action.on,
            body: check.action.body.replaceAll('{detail}', result.detail).replaceAll('{check}', name), idempotencyKey: 'check-' + slot.slot_key };
          // Enforce the existing rendered-body limit before durable intent, not
          // only at the writer after an oversized payload has become immortal.
          if (intent.body.length > 8000 || jsonBytes(intent) > MAX_INTENT_BYTES) {
            result = { status: 'error', detail: 'followup_intent_too_large' };
            intent = null;
          }
        }
        this.sql.exec('UPDATE hub_check_slots SET result_json = ?, intent_json = ?, state = ? WHERE name = ? AND token = ?', JSON.stringify(result), intent ? JSON.stringify(intent) : null, 'dispatch', name, token);
      }
      // Replacements cannot substitute the in-flight payload, and a stale evaluator
      // cannot dispatch after another runner took its slot.
      if (!this.sql.exec('SELECT 1 FROM hub_check_slots WHERE name = ? AND token = ?', name, token).toArray().length) continue;
      let retry = false;
      try {
        if (intent) {
          if (!deps.conversation) throw new Error('conversation_unavailable');
          const id = await deps.conversation.sendFollowUp(intent);
          if (typeof id !== 'string' || !id) throw new Error('followup_missing_id');
          action = 'conversation_message ' + id + ' (Hub stored; native delivery unconfirmed)';
        } else if (!check.action.conversation_id) action = await this.act(check, result, deps, slot.slot_key);
      } catch (e: unknown) {
        action = 'action failed: ' + errorText(e);
        retry = !!intent;
      }
      if (!this.sql.exec('SELECT 1 FROM hub_check_slots WHERE name = ? AND token = ?', name, token).toArray().length) continue;
      this.sql.exec('UPDATE hub_check_slots SET state = ?, action = ?, token = NULL, lease_until = 0 WHERE name = ? AND token = ?', retry ? 'retry' : 'done', action, name, token);
      // Unknown dispatch is never healthy, but its original evaluation stays frozen
      // in the slot so reconciliation can recover the first-payload receipt.
      const reported: CheckResult = retry ? { status: 'error', detail: 'conversation dispatch unresolved; ' + result.detail } : result;
      this.sql.exec('UPDATE hub_checks SET last_status = ?, last_detail = ? WHERE name = ?', reported.status, reported.detail.slice(0, 1000), name);
      this.sql.exec('INSERT INTO hub_check_runs (name, at, status, detail, action) VALUES (?, ?, ?, ?, ?)', name, at, reported.status, reported.detail.slice(0, 1000), action);
      results.push({ name, ...reported, action });
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
      case 'conversation_backlog': {
        if (!deps.conversation) return { status: 'error', detail: 'conversation_unavailable' };
        const result = deps.conversation.evaluateBacklog({ actor: p.actor, checkName: check.name, maxAgeS: p.max_age_s, conversationId: p.conversation_id });
        if (!result || !['pass', 'fail', 'error'].includes(result.status) || typeof result.detail !== 'string') return { status: 'error', detail: 'invalid_conversation_response' };
        return result;
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

function errorText(error: unknown): string { return String(error instanceof Error ? error.message : error).slice(0, 200); }
