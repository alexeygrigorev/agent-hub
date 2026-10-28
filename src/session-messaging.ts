/** Cross-device session messaging (docs/SESSION-MESSAGING.md; intakes 4fc83e80,
 * a3227276 AC4/AC6).
 *
 * Sessions and named agents are ordinary agent-registry identities, so the
 * existing bus (threads, reply_to, idempotency, recipient stages, host
 * authority over agents bound to its machine) carries every message:
 *
 * - `session:<hub-session-uuid>`: a registry row (project `session`, tag =
 *   Hub session id, machine = the bound host), created on first use.
 * - `<tag>@<machine>`: resolved at send time to exactly one live session.
 * - `agent:<name>`: an operator-created named agent (Developer, oncall...).
 *
 * A message to a session additionally becomes a session-control command of
 * kind `message`; the bound host delivers it through the native mailbox and,
 * for `delivery=tui`, the native deliver guard (at most once into the TUI).
 */
import type { SqlStorage } from '@cloudflare/workers-types';

export const SESSION_PROJECT = 'session';
export const NAMED_AGENT_PROJECT = 'agent';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const LIVE_SESSION_STATES = ['queued', 'held', 'offered', 'starting', 'running', 'uncertain'];
export const DELIVERIES = new Set(['tui', 'inbox']);
export const DEFAULT_DELIVER_BY_MS = 24 * 3600 * 1000;
export const NOT_READY_BACKOFF_MS = [5000, 15000, 30000, 60000];
const TUI_RENDER_BYTES = 2048;

export function initSessionMessaging(sql: SqlStorage): void {
  const cols = new Set(sql.exec('PRAGMA table_info(messages)').toArray().map((c: any) => String(c.name)));
  for (const [col, type] of [['delivery', 'TEXT'], ['state', 'TEXT'], ['state_reason', 'TEXT'], ['deliver_by', 'INTEGER'], ['to_session_id', 'TEXT'], ['recipient_seq', 'INTEGER']] as const) {
    if (!cols.has(col)) sql.exec(`ALTER TABLE messages ADD COLUMN ${col} ${type}`);
  }
  sql.exec('CREATE TABLE IF NOT EXISTS recipient_cursors (actor TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)');
  // Metadata only, never bodies; retained longer than bodies (180 days).
  sql.exec(`CREATE TABLE IF NOT EXISTS message_events (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,
    event TEXT NOT NULL, actor TEXT, machine_id TEXT, at INTEGER NOT NULL, detail_json TEXT)`);
  sql.exec('CREATE INDEX IF NOT EXISTS message_events_message ON message_events(message_id, id)');
  const commandCols = new Set(sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_commands'").toArray().length
    ? sql.exec('PRAGMA table_info(session_control_commands)').toArray().map((c: any) => String(c.name)) : []);
  if (commandCols.size && !commandCols.has('not_before')) sql.exec('ALTER TABLE session_control_commands ADD COLUMN not_before TEXT');
}

export function messageEvent(sql: SqlStorage, messageId: string, event: string, actor: string | null, machineId: string | null, now: number, detail?: Record<string, unknown>): void {
  sql.exec('INSERT INTO message_events (message_id, event, actor, machine_id, at, detail_json) VALUES (?, ?, ?, ?, ?, ?)',
    messageId, event, actor, machineId, now, detail ? JSON.stringify(detail).slice(0, 2048) : null);
}

/** Forward-only message state with its audit event. */
export function setMessageState(sql: SqlStorage, messageId: string, state: string, reason: string | null, actor: string | null, machineId: string | null, now: number, detail?: Record<string, unknown>): void {
  sql.exec('UPDATE messages SET state = ?, state_reason = ? WHERE id = ?', state, reason, messageId);
  if (state === 'delivered') sql.exec('UPDATE messages SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?', now, messageId);
  messageEvent(sql, messageId, state, actor, machineId, now, { ...(reason ? { reason } : {}), ...(detail || {}) });
}

export class RecipientError extends Error {
  constructor(code: string, public candidates?: string[]) { super(code); }
}

function liveSession(sql: SqlStorage, id: string): any {
  return sql.exec(`SELECT id, machine_id, state, binding_json FROM session_control_sessions WHERE id = ? AND state IN (${LIVE_SESSION_STATES.map(() => '?').join(',')})`,
    id, ...LIVE_SESSION_STATES).toArray()[0];
}

/** Ensure the registry identity of a live Hub session and return its address.
 * The row follows rebinds so host authority always matches the bound host. */
export function ensureSessionIdentity(sql: SqlStorage, sessionId: string, now: number): string {
  const session = liveSession(sql, sessionId);
  if (!session) throw new RecipientError('unknown_recipient');
  const row: any = sql.exec('SELECT machine_id FROM agents WHERE project = ? AND tag = ?', SESSION_PROJECT, sessionId).toArray()[0];
  if (!row) {
    sql.exec('INSERT INTO agents (tag, project, role, machine_id, registered_at) VALUES (?, ?, NULL, ?, ?)',
      sessionId, SESSION_PROJECT, session.machine_id ?? null, new Date(now).toISOString());
  } else if ((row.machine_id ?? null) !== (session.machine_id ?? null)) {
    sql.exec('UPDATE agents SET machine_id = ? WHERE project = ? AND tag = ?', session.machine_id ?? null, SESSION_PROJECT, sessionId);
  }
  return `${SESSION_PROJECT}:${sessionId}`;
}

/** Resolve the convenience and session address forms to a canonical actor;
 * every other address passes through unchanged for the bus to validate. */
export function resolveRecipient(sql: SqlStorage, toActor: string, now: number): string {
  if (toActor.startsWith(SESSION_PROJECT + ':')) {
    const id = toActor.slice(SESSION_PROJECT.length + 1).toLowerCase();
    if (!UUID.test(id)) throw new RecipientError('unknown_recipient');
    return ensureSessionIdentity(sql, id, now);
  }
  const at = toActor.lastIndexOf('@');
  if (at <= 0 || toActor.includes(':')) return toActor;
  const tag = toActor.slice(0, at), machineRef = toActor.slice(at + 1);
  const machines = (sql.exec('SELECT id FROM machines WHERE id = ? OR lower(name) = lower(?) OR lower(hostname) = lower(?)', machineRef, machineRef, machineRef).toArray() as any[]).map(m => String(m.id));
  if (!machines.length) throw new RecipientError('unknown_recipient');
  const rows = (sql.exec(`SELECT id, machine_id, binding_json FROM session_control_sessions WHERE state IN (${LIVE_SESSION_STATES.map(() => '?').join(',')})`, ...LIVE_SESSION_STATES).toArray() as any[])
    .filter(s => machines.includes(String(s.machine_id)) && (() => { try { return JSON.parse(s.binding_json || '{}').tag === tag; } catch { return false; } })());
  if (!rows.length) throw new RecipientError('unknown_recipient');
  if (rows.length > 1) throw new RecipientError('ambiguous_recipient', rows.map(r => `${SESSION_PROJECT}:${r.id}`));
  return ensureSessionIdentity(sql, String(rows[0].id), now);
}

export function sessionIdOf(actor: string): string | null {
  if (!actor.startsWith(SESSION_PROJECT + ':')) return null;
  const id = actor.slice(SESSION_PROJECT.length + 1);
  return UUID.test(id) ? id : null;
}

/** Text typed into the TUI: bounded, control characters stripped, with the
 * marker used for uncertain-delivery resolution and the reply instruction. */
export function renderTui(msg: { id: string; from_actor: string; thread_id: string; body: string }): string {
  const clean = (s: string) => s.replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
  const head = `⟦hub:${msg.id.slice(-8)}⟧ from ${clean(msg.from_actor)} (thread ${msg.thread_id}): `;
  const tail = `\n[Reply: a message reply <this inbox message id> "text", or hub reply ${msg.id} --body "text"]`;
  const enc = new TextEncoder();
  let body = clean(msg.body);
  const budget = TUI_RENDER_BYTES - enc.encode(head + tail).length - 64;
  if (enc.encode(body).length > budget) {
    while (enc.encode(body).length > budget) body = body.slice(0, Math.floor(body.length * 0.9));
    body += `\n[truncated: hub show ${msg.id}]`;
  }
  return head + body + tail;
}

/** Assign the per-recipient sequence, the initial state, and for session
 * recipients the delivery command. Runs inside the bus send transaction. */
export function afterMessageInsert(sql: SqlStorage, msg: any, opts: { delivery?: string; deliverByMs?: number; now: number }): void {
  const prior: any = sql.exec('SELECT last_seq FROM recipient_cursors WHERE actor = ?', msg.to_actor).toArray()[0];
  const seq = (prior ? Number(prior.last_seq) : 0) + 1;
  sql.exec('INSERT INTO recipient_cursors (actor, last_seq) VALUES (?, ?) ON CONFLICT(actor) DO UPDATE SET last_seq = excluded.last_seq', msg.to_actor, seq);
  const sessionId = sessionIdOf(msg.to_actor);
  const named = msg.from_actor.startsWith(NAMED_AGENT_PROJECT + ':') || msg.from_actor.startsWith('human:') || msg.from_actor === 'admin';
  const delivery = sessionId ? (opts.delivery ?? (named ? 'tui' : 'inbox')) : null;
  const deliverBy = sessionId ? opts.now + Math.min(opts.deliverByMs ?? DEFAULT_DELIVER_BY_MS, 7 * 24 * 3600 * 1000) : null;
  sql.exec('UPDATE messages SET recipient_seq = ?, state = ?, delivery = ?, deliver_by = ?, to_session_id = ? WHERE id = ?',
    seq, 'accepted', delivery, deliverBy, sessionId, msg.id);
  messageEvent(sql, msg.id, 'accepted', msg.from_actor, null, opts.now, { to: msg.to_actor, seq, ...(delivery ? { delivery } : {}) });
  if (msg.reply_to) {
    const parent: any = sql.exec('SELECT id, to_actor, read_at FROM messages WHERE id = ?', msg.reply_to).toArray()[0];
    if (parent && parent.to_actor === msg.from_actor) {
      // A reply implies the recipient read the original.
      if (!parent.read_at) sql.exec('UPDATE messages SET delivered_at = COALESCE(delivered_at, ?), read_at = ? WHERE id = ?', opts.now, opts.now, parent.id);
      messageEvent(sql, parent.id, 'replied', msg.from_actor, null, opts.now, { reply_id: msg.id });
    }
  }
  if (!sessionId) return;
  const session: any = sql.exec('SELECT id, binding_json FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
  const binding = (() => { try { return JSON.parse(session?.binding_json || 'null'); } catch { return null; } })();
  const generation = binding?.generation ?? 0;
  const id = crypto.randomUUID();
  const at = new Date(opts.now).toISOString();
  sql.exec(
    'INSERT INTO session_control_commands (id, session_id, kind, actor, request_key, payload_json, state, generation, correlation, owner_token_id, owner_family, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
    id, sessionId, 'message', msg.from_actor, 'msg-' + msg.id,
    JSON.stringify({ message_id: msg.id, delivery, from_actor: msg.from_actor, body: renderTui(msg) }), 'recorded', generation, `cmd:${id}:gen:${generation}`,
    null, null, 0, at, at);
}

/** Backoff for a not-ready TUI: 5 s, 15 s, 30 s, then every 60 s. */
export function notReadyDelay(count: number): number {
  return NOT_READY_BACKOFF_MS[Math.min(count, NOT_READY_BACKOFF_MS.length - 1)];
}

/** Messages past deliver_by, or whose session ended, stop being offered:
 * the native inbox copy (if any) and the Hub inbox stay authoritative. */
export function expireSessionMessages(sql: SqlStorage, now: number, machineId: string): void {
  const rows = sql.exec(`SELECT c.id, c.payload_json, s.state AS session_state FROM session_control_commands c
    JOIN session_control_sessions s ON s.id = c.session_id
    WHERE s.machine_id = ? AND c.kind = 'message' AND c.state = 'recorded' LIMIT 100`, machineId).toArray() as any[];
  const at = new Date(now).toISOString();
  for (const row of rows) {
    let messageId: string | null = null;
    try { messageId = JSON.parse(row.payload_json)?.message_id ?? null; } catch {}
    const msg: any = messageId ? sql.exec('SELECT deliver_by FROM messages WHERE id = ?', messageId).toArray()[0] : null;
    const ended = !LIVE_SESSION_STATES.includes(row.session_state);
    const expired = !msg || (msg.deliver_by != null && Number(msg.deliver_by) <= now);
    if (!ended && !expired) continue;
    const reason = ended ? 'session_ended' : 'deliver_by_passed';
    sql.exec("UPDATE session_control_commands SET state = 'failed', ack_json = ?, updated_at = ? WHERE id = ? AND state = 'recorded'",
      JSON.stringify({ outcome: 'failed', reason }), at, row.id);
    if (messageId && msg) setMessageState(sql, messageId, ended ? 'undeliverable' : 'expired', reason, null, machineId, now);
  }
}
