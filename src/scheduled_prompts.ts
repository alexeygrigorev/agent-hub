/** Hub prompt library + session schedules (intake 19ec91c2 AC8-AC10).
 *
 * Prompt library: versioned prompts addressable by id or slug (`slug@3` pins a
 * version). Every update appends an immutable version; renders substitute
 * `{{var.name}}` variables.
 *
 * Session schedules: a cron expression plus a prompt delivered into any
 * interactive session (claude, codex, zcodex, opencode) through the SAME
 * durable delivery queue as inbound webhooks (`session_deliveries`): queued
 * while the target is busy or down, redelivered when it returns, idempotent per
 * (schedule, fire time). Missed occurrences while Hub was unreachable
 * collapse into one catch-up fire (no backfill storm); audit rows carry ids
 * and statuses, never prompt bodies.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { parseCron, cronNext, validTimezone } from './launch_schedules';
import { renderTemplate } from './webhooks';

export const PROMPT_SCHEMA = `
CREATE TABLE IF NOT EXISTS hub_prompts (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  title TEXT,
  current_version INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hub_prompt_versions (
  prompt_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  note TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (prompt_id, version)
);
CREATE TABLE IF NOT EXISTS session_schedules (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  target_label TEXT NOT NULL,
  cron TEXT NOT NULL,
  tz TEXT NOT NULL DEFAULT 'UTC',
  prompt_text TEXT,
  prompt_id TEXT,
  prompt_vars TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  next_due_ms INTEGER,
  last_fire_ms INTEGER,
  last_fire_at TEXT
);
`;

const HEADERS = { 'Cache-Control': 'private, no-store' };
const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;
const PROMPT_MAX = 16000;

type Auth = { caller?: { isAdmin?: boolean; actor?: string }; scopes?: string[]; error?: Response };

function json(data: any, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', ...HEADERS } });
}
function parsed(value: string | null | undefined, fallback: any): any {
  if (!value) return fallback;
  try { return JSON.parse(value); } catch { return fallback; }
}
function validVars(vars: unknown): vars is Record<string, string> {
  return !!vars && typeof vars === 'object' && !Array.isArray(vars)
    && JSON.stringify(vars).length <= 4096
    && Object.entries(vars as object).every(([k, v]) => /^[A-Za-z0-9_-]{1,64}$/.test(k) && typeof v === 'string');
}

export type ResolvedPrompt = { id: string; slug: string; version: number; body: string };

export class PromptLibraryService {
  constructor(private sql: SqlStorage, private txn: (fn: () => any) => any, private now: () => number) {}

  init() {
    for (const statement of PROMPT_SCHEMA.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }
  }

  private at(): string { return new Date(this.now()).toISOString(); }

  private audit(action: string, actor: string, target: string, detail: Record<string, unknown>) {
    try {
      this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
        actor, action, 'prompt', target, JSON.stringify(detail), this.at());
    } catch { /* best-effort */ }
  }

  /** `ref` is an id or slug, optionally `@<version>` to pin. */
  resolve(ref: string): ResolvedPrompt | null {
    if (typeof ref !== 'string' || !ref) return null;
    let key = ref;
    let pinned: number | null = null;
    const at = ref.lastIndexOf('@');
    if (at > 0) {
      const version = Number(ref.slice(at + 1));
      if (!Number.isInteger(version) || version < 1) return null;
      key = ref.slice(0, at);
      pinned = version;
    }
    const prompt: any = this.sql.exec('SELECT * FROM hub_prompts WHERE id = ? OR slug = ?', key, key).toArray()[0];
    if (!prompt) return null;
    const version = pinned ?? Number(prompt.current_version);
    const row: any = this.sql.exec('SELECT body FROM hub_prompt_versions WHERE prompt_id = ? AND version = ?', prompt.id, version).toArray()[0];
    if (!row) return null;
    return { id: prompt.id, slug: prompt.slug, version, body: row.body };
  }

  render(ref: string, vars: Record<string, string>): { text: string; version: number; slug: string; warnings: string[] } | null {
    const prompt = this.resolve(ref);
    if (!prompt) return null;
    // No payload here: payload placeholders are removed rather than leaked.
    const { text, warnings } = renderTemplate(prompt.body, '', vars);
    return { text, version: prompt.version, slug: prompt.slug, warnings };
  }

  private summary(row: any) {
    return { id: row.id, slug: row.slug, title: row.title, current_version: row.current_version,
      created_by: row.created_by, created_at: row.created_at, updated_at: row.updated_at };
  }

  async handle(request: Request, auth: Auth): Promise<Response> {
    const caller = auth.caller;
    if (!caller?.isAdmin) return auth.error || json({ error: 'forbidden' }, 403);
    const actor = caller.actor || 'admin';
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === '/api/prompts' && request.method === 'GET') {
      const rows = this.sql.exec('SELECT * FROM hub_prompts ORDER BY slug LIMIT 500').toArray() as any[];
      return json({ prompts: rows.map(r => this.summary(r)) });
    }
    if (path === '/api/prompts' && request.method === 'POST') {
      let input: any;
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return this.create(input, actor);
    }
    const match = /^\/api\/prompts\/([^/]+)(?:\/(render|versions))?$/.exec(path);
    if (!match) return json({ error: 'not_found' }, 404);
    const ref = decodeURIComponent(match[1]);
    const prompt: any = this.sql.exec('SELECT * FROM hub_prompts WHERE id = ? OR slug = ?', ref.split('@')[0], ref.split('@')[0]).toArray()[0];
    if (!prompt) return json({ error: 'not_found' }, 404);
    if (match[2] === 'render' && request.method === 'POST') {
      let input: any = {};
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      const vars = input?.vars ?? {};
      if (!validVars(vars)) return json({ error: 'invalid_vars' }, 400);
      const out = this.render(ref, vars);
      return out ? json(out) : json({ error: 'not_found' }, 404);
    }
    if (match[2] === 'versions' && request.method === 'GET') {
      const rows = this.sql.exec('SELECT version, note, created_by, created_at, LENGTH(body) AS bytes FROM hub_prompt_versions WHERE prompt_id = ? ORDER BY version DESC', prompt.id).toArray();
      return json({ ...this.summary(prompt), versions: rows });
    }
    if (request.method === 'GET') {
      const resolved = this.resolve(ref);
      if (!resolved) return json({ error: 'not_found' }, 404);
      return json({ ...this.summary(prompt), version: resolved.version, body: resolved.body });
    }
    if (request.method === 'PUT' || request.method === 'PATCH') {
      let input: any;
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return this.update(prompt, input, actor);
    }
    if (request.method === 'DELETE') {
      const users = this.sql.exec('SELECT name FROM session_schedules WHERE prompt_id = ? OR prompt_id = ? OR prompt_id LIKE ? OR prompt_id LIKE ?',
        prompt.id, prompt.slug, prompt.id + '@%', prompt.slug + '@%').toArray() as any[];
      const routes = this.sql.exec('SELECT name FROM webhook_routes WHERE prompt_id = ? OR prompt_id = ? OR prompt_id LIKE ? OR prompt_id LIKE ?',
        prompt.id, prompt.slug, prompt.id + '@%', prompt.slug + '@%').toArray() as any[];
      if (users.length || routes.length) {
        return json({ error: 'prompt_in_use', schedules: users.map(u => u.name), webhooks: routes.map(u => u.name) }, 409);
      }
      return this.txn(() => {
        this.sql.exec('DELETE FROM hub_prompt_versions WHERE prompt_id = ?', prompt.id);
        this.sql.exec('DELETE FROM hub_prompts WHERE id = ?', prompt.id);
        this.audit('prompt.delete', actor, prompt.slug, { prompt_id: prompt.id });
        return json({ deleted: true, prompt_id: prompt.id });
      });
    }
    return json({ error: 'method_not_allowed' }, 405);
  }

  private create(input: any, actor: string): Response {
    if (!input || typeof input !== 'object') return json({ error: 'invalid_request' }, 400);
    const slug = typeof input.slug === 'string' ? input.slug : '';
    if (!SLUG_RE.test(slug)) return json({ error: 'invalid_slug', detail: 'lowercase slug, 2-64 chars' }, 400);
    const body = input.body;
    if (typeof body !== 'string' || !body.trim() || body.length > PROMPT_MAX) return json({ error: 'invalid_body', detail: `1-${PROMPT_MAX} chars` }, 400);
    const title = typeof input.title === 'string' ? input.title.slice(0, 200) : null;
    return this.txn(() => {
      if (this.sql.exec('SELECT id FROM hub_prompts WHERE slug = ?', slug).toArray().length) return json({ error: 'slug_taken' }, 409);
      const at = this.at();
      const id = crypto.randomUUID();
      this.sql.exec('INSERT INTO hub_prompts (id, slug, title, current_version, created_by, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)',
        id, slug, title, actor, at, at);
      this.sql.exec('INSERT INTO hub_prompt_versions (prompt_id, version, body, note, created_by, created_at) VALUES (?, 1, ?, ?, ?, ?)',
        id, body, typeof input.note === 'string' ? input.note.slice(0, 500) : null, actor, at);
      this.audit('prompt.create', actor, slug, { prompt_id: id, version: 1, bytes: body.length });
      return json({ id, slug, title, current_version: 1, created_at: at }, 201);
    });
  }

  private update(prompt: any, input: any, actor: string): Response {
    if (!input || typeof input !== 'object') return json({ error: 'invalid_request' }, 400);
    const hasBody = input.body !== undefined;
    if (hasBody && (typeof input.body !== 'string' || !input.body.trim() || input.body.length > PROMPT_MAX)) return json({ error: 'invalid_body' }, 400);
    if (input.title !== undefined && input.title !== null && typeof input.title !== 'string') return json({ error: 'invalid_title' }, 400);
    if (!hasBody && input.title === undefined) return json({ error: 'nothing_to_update' }, 400);
    return this.txn(() => {
      const at = this.at();
      let version = Number(prompt.current_version);
      if (hasBody) {
        version += 1;
        this.sql.exec('INSERT INTO hub_prompt_versions (prompt_id, version, body, note, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)',
          prompt.id, version, input.body, typeof input.note === 'string' ? input.note.slice(0, 500) : null, actor, at);
      }
      this.sql.exec('UPDATE hub_prompts SET current_version = ?, title = ?, updated_at = ? WHERE id = ?',
        version, input.title === undefined ? prompt.title : (input.title === null ? null : input.title.slice(0, 200)), at, prompt.id);
      this.audit('prompt.update', actor, prompt.slug, { prompt_id: prompt.id, version });
      return json({ id: prompt.id, slug: prompt.slug, current_version: version, updated_at: at });
    });
  }
}

type Enqueue = (source: string, sourceId: string, deliveryKey: string, target: string, body: string) => Promise<{ id: string; status: string; replayed: boolean }>;

export class SessionSchedulesService {
  constructor(private sql: SqlStorage, private txn: (fn: () => any) => any, private now: () => number,
              private prompts: PromptLibraryService, private enqueue: Enqueue) {}

  private at(): string { return new Date(this.now()).toISOString(); }

  private audit(action: string, actor: string, target: string, detail: Record<string, unknown>) {
    try {
      this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
        actor, action, 'session_schedule', target, JSON.stringify(detail), this.at());
    } catch { /* best-effort */ }
  }

  private pub(row: any) {
    return {
      id: row.id, name: row.name, target_label: row.target_label, cron: row.cron, tz: row.tz,
      prompt_id: row.prompt_id ?? null, prompt_vars: parsed(row.prompt_vars, null),
      prompt_bytes: row.prompt_text ? row.prompt_text.length : null,
      enabled: Boolean(row.enabled), created_by: row.created_by, created_at: row.created_at, updated_at: row.updated_at,
      next_due_at: row.next_due_ms ? new Date(Number(row.next_due_ms)).toISOString() : null,
      last_fire_at: row.last_fire_at ?? null,
    };
  }

  private nextDue(row: { cron: string; tz: string }, afterMs: number): number | null {
    const cron = parseCron(row.cron);
    return cron ? cronNext(cron, row.tz, afterMs) : null;
  }

  private deliveries(id: string, limit = 50) {
    return (this.sql.exec(
      'SELECT id, source, delivery_key, target_label, status, session_id, command_id, attempts, next_attempt_at, last_error, created_at, delivered_at, LENGTH(body) AS body_bytes FROM session_deliveries WHERE source_id = ? ORDER BY created_at DESC, id DESC LIMIT ?',
      id, limit).toArray());
  }

  /** Fire every schedule whose occurrence is due. One fire per tick per
   * schedule (missed occurrences collapse); the delivery key is the
   * occurrence time, so a replayed tick can never double-send. */
  async tick(): Promise<number> {
    const nowMs = this.now();
    const due = this.sql.exec('SELECT * FROM session_schedules WHERE enabled = 1 AND next_due_ms IS NOT NULL AND next_due_ms <= ? LIMIT 50', nowMs).toArray() as any[];
    let fired = 0;
    for (const row of due) {
      const next = this.nextDue(row, nowMs);
      const advanced = this.txn(() => {
        const fresh: any = this.sql.exec('SELECT next_due_ms FROM session_schedules WHERE id = ? AND enabled = 1', row.id).toArray()[0];
        if (!fresh || Number(fresh.next_due_ms) !== Number(row.next_due_ms)) return false;
        this.sql.exec('UPDATE session_schedules SET next_due_ms = ?, last_fire_ms = ?, last_fire_at = ?, updated_at = ? WHERE id = ?',
          next, row.next_due_ms, this.at(), this.at(), row.id);
        return true;
      });
      if (!advanced) continue;
      await this.fire(row, Number(row.next_due_ms), 'schedule');
      fired += 1;
    }
    return fired;
  }

  private async fire(row: any, occurrenceMs: number, source: string, keyOverride?: string) {
    let text: string | null = null;
    let error: string | null = null;
    if (row.prompt_id) {
      const rendered = this.prompts.render(row.prompt_id, parsed(row.prompt_vars, {}));
      if (rendered) text = rendered.text; else error = 'prompt_unavailable';
    } else {
      text = renderTemplate(row.prompt_text, '', parsed(row.prompt_vars, {})).text;
    }
    if (error || !text) {
      this.audit('schedule.fire_failed', 'hub:schedules', row.name, { schedule_id: row.id, error: error || 'empty_prompt' });
      return null;
    }
    const key = keyOverride ?? 'fire-' + new Date(occurrenceMs).toISOString();
    const result = await this.enqueue(source, row.id, key, row.target_label, text);
    this.audit('schedule.fire', 'hub:schedules', row.name, { schedule_id: row.id, delivery_id: result.id, key, replayed: result.replayed });
    return result;
  }

  async handle(request: Request, auth: Auth): Promise<Response> {
    const caller = auth.caller;
    if (!caller?.isAdmin) return auth.error || json({ error: 'forbidden' }, 403);
    const actor = caller.actor || 'admin';
    const path = new URL(request.url).pathname;
    if (path === '/api/session-schedules' && request.method === 'GET') {
      const rows = this.sql.exec('SELECT * FROM session_schedules ORDER BY name LIMIT 200').toArray() as any[];
      return json({ schedules: rows.map(r => this.pub(r)) });
    }
    if (path === '/api/session-schedules' && request.method === 'POST') {
      let input: any;
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return this.create(input, actor);
    }
    if (path === '/api/session-schedules/deliveries' && request.method === 'GET') {
      const rows = this.sql.exec(
        `SELECT d.id, d.source_id, s.name AS schedule, d.status, d.target_label, d.attempts, d.last_error, d.created_at, d.delivered_at
         FROM session_deliveries d LEFT JOIN session_schedules s ON s.id = d.source_id WHERE d.source IN ('schedule','schedule-test')
         ORDER BY d.created_at DESC LIMIT 100`).toArray();
      return json({ deliveries: rows });
    }
    const match = /^\/api\/session-schedules\/([^/]+)(?:\/(pause|resume|fire|deliveries))?$/.exec(path);
    if (!match) return json({ error: 'not_found' }, 404);
    const ref = decodeURIComponent(match[1]);
    const row: any = this.sql.exec('SELECT * FROM session_schedules WHERE id = ? OR name = ?', ref, ref).toArray()[0];
    if (!row) return json({ error: 'not_found' }, 404);
    const action = match[2];
    if (action === 'deliveries' && request.method === 'GET') return json({ deliveries: this.deliveries(row.id) });
    if ((action === 'pause' || action === 'resume') && request.method === 'POST') {
      const enabled = action === 'resume';
      const next = enabled ? this.nextDue(row, this.now()) : null; // no backfill on resume
      this.sql.exec('UPDATE session_schedules SET enabled = ?, next_due_ms = ?, updated_at = ? WHERE id = ?', enabled ? 1 : 0, next, this.at(), row.id);
      this.audit('schedule.' + action, actor, row.name, { schedule_id: row.id });
      return json(this.pub(this.sql.exec('SELECT * FROM session_schedules WHERE id = ?', row.id).toArray()[0]));
    }
    if (action === 'fire' && request.method === 'POST') {
      let input: any = {};
      try { input = await request.json(); } catch { input = {}; }
      const key = typeof input?.delivery_key === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(input.delivery_key)
        ? input.delivery_key : 'manual-' + crypto.randomUUID();
      const result = await this.fire(row, this.now(), 'schedule-test', key);
      return result ? json({ delivery_id: result.id, status: result.status, replay: result.replayed }, 202) : json({ error: 'prompt_unavailable' }, 409);
    }
    if (!action && request.method === 'GET') {
      const audits = this.sql.exec("SELECT actor, action, target, detail, at FROM audit WHERE resource = 'session_schedule' AND target = ? ORDER BY at DESC, id DESC LIMIT 20", row.name).toArray();
      return json({ ...this.pub(row), deliveries: this.deliveries(row.id, 20), audit: audits });
    }
    if (!action && request.method === 'DELETE') {
      this.txn(() => {
        this.sql.exec('DELETE FROM session_schedules WHERE id = ?', row.id);
        this.audit('schedule.delete', actor, row.name, { schedule_id: row.id });
      });
      return json({ deleted: true, schedule_id: row.id });
    }
    return json({ error: 'method_not_allowed' }, 405);
  }

  private create(input: any, actor: string): Response {
    if (!input || typeof input !== 'object') return json({ error: 'invalid_request' }, 400);
    const name = typeof input.name === 'string' ? input.name : '';
    if (!SLUG_RE.test(name)) return json({ error: 'invalid_name', detail: 'lowercase slug, 2-64 chars' }, 400);
    const target = typeof input.target_label === 'string' ? input.target_label.trim() : '';
    if (!target || target.length > 200) return json({ error: 'invalid_target_label' }, 400);
    const cronExpr = typeof input.cron === 'string' ? input.cron.trim() : '';
    if (!parseCron(cronExpr)) return json({ error: 'invalid_cron', detail: 'five-field cron: minute hour dom month dow' }, 400);
    const tz = typeof input.tz === 'string' && input.tz ? input.tz : 'UTC';
    if (!validTimezone(tz)) return json({ error: 'invalid_tz' }, 400);
    const promptText = input.prompt === undefined || input.prompt === null ? null : input.prompt;
    const promptId = input.prompt_id === undefined || input.prompt_id === null ? null : input.prompt_id;
    if ((promptText === null) === (promptId === null)) return json({ error: 'prompt_or_prompt_id_required', detail: 'exactly one of prompt, prompt_id' }, 400);
    if (promptText !== null && (typeof promptText !== 'string' || !promptText.trim() || promptText.length > PROMPT_MAX)) return json({ error: 'invalid_prompt' }, 400);
    if (promptId !== null && (typeof promptId !== 'string' || !this.prompts.resolve(promptId))) return json({ error: 'prompt_not_found' }, 400);
    const vars = input.prompt_vars === undefined || input.prompt_vars === null ? null : input.prompt_vars;
    if (vars !== null && !validVars(vars)) return json({ error: 'invalid_prompt_vars' }, 400);
    const enabled = input.enabled === undefined ? true : Boolean(input.enabled);
    return this.txn(() => {
      if (this.sql.exec('SELECT id FROM session_schedules WHERE name = ?', name).toArray().length) return json({ error: 'name_taken' }, 409);
      const at = this.at();
      const id = crypto.randomUUID();
      const next = enabled ? this.nextDue({ cron: cronExpr, tz }, this.now()) : null;
      this.sql.exec(
        `INSERT INTO session_schedules (id, name, target_label, cron, tz, prompt_text, prompt_id, prompt_vars, enabled, created_by, created_at, updated_at, next_due_ms)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        id, name, target, cronExpr, tz, promptText, promptId, vars === null ? null : JSON.stringify(vars), enabled ? 1 : 0, actor, at, at, next);
      this.audit('schedule.create', actor, name, { schedule_id: id, cron: cronExpr, tz, target_label: target, prompt_id: promptId });
      return json(this.pub(this.sql.exec('SELECT * FROM session_schedules WHERE id = ?', id).toArray()[0]), 201);
    });
  }
}
