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
  sql.exec('CREATE INDEX IF NOT EXISTS message_events_at ON message_events(at)');
  // Session credential lookups go by registry_actor; never scan credentials.
  sql.exec('CREATE INDEX IF NOT EXISTS credentials_registry_actor ON credentials(registry_actor)');
  const commandCols = new Set(sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='session_control_commands'").toArray().length
    ? sql.exec('PRAGMA table_info(session_control_commands)').toArray().map((c: any) => String(c.name)) : []);
  if (commandCols.size && !commandCols.has('not_before')) sql.exec('ALTER TABLE session_control_commands ADD COLUMN not_before TEXT');
  // Operator messaging configuration (docs/SESSION-MESSAGING.md §8): today only
  // cross_project_allow, the per-recipient-project grant for session senders.
  sql.exec('CREATE TABLE IF NOT EXISTS messaging_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)');
}

export type MessagingSettings = { cross_project_allow?: Record<string, boolean> };

export function messagingSettings(sql: SqlStorage): MessagingSettings {
  const row: any = sql.exec('SELECT data FROM messaging_settings WHERE id = 1').toArray()[0];
  try { const data = JSON.parse(row?.data || '{}'); return data && typeof data === 'object' ? data : {}; } catch { return {}; }
}

export function updateMessagingSettings(sql: SqlStorage, patch: MessagingSettings): MessagingSettings {
  const merged = { ...messagingSettings(sql), ...patch };
  sql.exec('INSERT INTO messaging_settings (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data', JSON.stringify(merged));
  return merged;
}

/** The recipient project explicitly grants cross-project sends from sessions. */
export function crossProjectAllowed(sql: SqlStorage, project: string | null): boolean {
  if (!project) return false;
  return messagingSettings(sql).cross_project_allow?.[project] === true;
}

const EVENT_RETENTION_MS = 180 * 24 * 3600 * 1000;

export function messageEvent(sql: SqlStorage, messageId: string, event: string, actor: string | null, machineId: string | null, now: number, detail?: Record<string, unknown>): void {
  // Bounded retention (180 days, metadata only): trim a small slice per write.
  sql.exec('DELETE FROM message_events WHERE id IN (SELECT id FROM message_events WHERE at < ? ORDER BY at LIMIT 20)', now - EVENT_RETENTION_MS);
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

/** A project-scoping refusal (M2 AC4). Auditing happens at the API boundary
 * (hub.ts route catch), OUTSIDE the rolled-back send transaction — an audit
 * row written inside the transaction would be rolled back with the refusal. */
export class ScopingRefusal extends RecipientError {
  constructor(public detail: Record<string, unknown>) { super('cross_project_forbidden'); }
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

/** A session's project comes from its linked intake task; null when unlinked
 * or projectless (session_control derives it the same way). */
export function sessionProject(sql: SqlStorage, sessionId: string): string | null {
  const row: any = sql.exec('SELECT request_json FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
  let req: any = null;
  try { req = JSON.parse(row?.request_json || '{}'); } catch { return null; }
  const taskId = typeof req?.intake_task_id === 'string' && req.intake_task_id ? req.intake_task_id : null;
  if (!taskId) return null;
  try {
    const task: any = sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0];
    const data = JSON.parse(task?.data || '{}');
    return typeof data?.project === 'string' && data.project ? data.project : null;
  } catch { return null; }
}

function isOperatorHuman(sql: SqlStorage, sub: string): boolean {
  try { return sql.exec('SELECT 1 FROM users WHERE sub = ? AND is_admin = 1', sub).toArray().length > 0; } catch { return false; }
}

/** The projects a legacy address (tag, project:tag, role:<r>) can belong to;
 * empty when it resolves to nothing (normal validation refuses it later). */
function legacyAddressProjects(sql: SqlStorage, toActor: string): string[] {
  const at = toActor.indexOf(':');
  if (toActor.startsWith('role:')) {
    return (sql.exec('SELECT DISTINCT project FROM agents WHERE role = ?', toActor.slice(5)).toArray() as any[]).map(r => r.project ?? null);
  }
  if (at < 0) {
    return (sql.exec('SELECT DISTINCT project FROM agents WHERE tag = ?', toActor).toArray() as any[]).map(r => r.project ?? null);
  }
  const project = toActor.slice(0, at);
  return sql.exec('SELECT 1 FROM agents WHERE project = ? AND tag = ?', project, toActor.slice(at + 1)).toArray().length ? [project] : [];
}

/** Project scoping for session credentials (docs/SESSION-MESSAGING.md §8,
 * intake 008529b5 AC4). A session may send to sessions of its own project,
 * named agents, operator humans, its own machine and legacy addresses of its
 * project; everything else is refused as cross_project_forbidden unless the
 * recipient project grants it in messaging settings. Admins and the bound
 * host keep their existing authority. Refusals are audited. */
export function checkSendScoping(sql: SqlStorage, caller: { kind?: string; isAdmin?: boolean; actor?: string }, senderActor: string, toActor: string, now: number): void {
  const sessionId = sessionIdOf(senderActor);
  if (!sessionId || caller.isAdmin || caller.kind === 'host') return;
  const refuse = (detail: Record<string, unknown>) => {
    throw new ScopingRefusal({ to: toActor, code: 'cross_project_forbidden', from_project: detail.from_project ?? null, ...detail });
  };
  const fromProject = sessionProject(sql, sessionId);
  const ownMachine: string | null = (sql.exec('SELECT machine_id FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0] as any)?.machine_id ?? null;

  const toSessionId = sessionIdOf(toActor);
  if (toSessionId) {
    const toProject = sessionProject(sql, toSessionId);
    if (toProject !== null && toProject === fromProject) return;
    if (crossProjectAllowed(sql, toProject)) return;
    refuse({ recipient: 'session', from_project: fromProject, to_project: toProject });
  }
  if (toActor.startsWith(NAMED_AGENT_PROJECT + ':')) return;
  if (toActor.startsWith('human:')) {
    if (isOperatorHuman(sql, toActor.slice('human:'.length))) return;
    refuse({ recipient: 'human', from_project: fromProject });
  }
  if (toActor.startsWith('machine:')) {
    if (toActor.slice('machine:'.length) === ownMachine) return;
    refuse({ recipient: 'machine', from_project: fromProject });
  }
  // Legacy bus addresses: same project only.
  const projects = legacyAddressProjects(sql, toActor).filter((p): p is string => p !== null);
  if (fromProject !== null && projects.includes(fromProject)) return;
  if (projects.some(p => crossProjectAllowed(sql, p))) return;
  refuse({ recipient: 'legacy', from_project: fromProject, to_project: projects.length === 1 ? projects[0] : projects });
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

const CREDENTIAL_TTL_MS = 3600 * 1000; // 1 h access token (docs/SESSION-MESSAGING.md §8)

/** Bound, running sessions on this machine that already have a messaging
 * identity (they were messaged, or an operator created it) and whose
 * credential family is gone (revoked with a stop or a rebind). The family is
 * refresh-bound to the live session, so no time-based re-mint happens while
 * it runs. Bounded: driven by the small session-identity set, indexed
 * lookups only. */
export function sessionsNeedingCredentials(sql: SqlStorage, machineId: string, now: number): { session_id: string; native_id: string; tag: string; workspace: string }[] {
  const identities = sql.exec('SELECT tag FROM agents WHERE project = ? AND machine_id = ? LIMIT 200', SESSION_PROJECT, machineId).toArray() as any[];
  const out = [];
  for (const identity of identities) {
    const row: any = sql.exec("SELECT id, binding_json FROM session_control_sessions WHERE id = ? AND machine_id = ? AND state = 'running' AND binding_json IS NOT NULL", identity.tag, machineId).toArray()[0];
    if (!row) continue;
    let binding: any = null;
    try { binding = JSON.parse(row.binding_json); } catch {}
    if (!binding?.native_id || !binding?.tag || !binding?.workspace) continue;
    const live = sql.exec("SELECT 1 FROM credentials WHERE registry_actor = ? AND kind = 'agent' AND machine_id = ? AND revoked_at IS NULL LIMIT 1",
      `${SESSION_PROJECT}:${row.id}`, machineId).toArray().length;
    if (!live) out.push({ session_id: String(row.id), native_id: String(binding.native_id), tag: String(binding.tag), workspace: String(binding.workspace) });
    if (out.length >= 10) break;
  }
  return out;
}

/** Mint the session's own bus-only credential for its bound host: actor
 * session:<uuid>, scopes msg:send msg:read, 1 h access with a refresh family
 * that lives exactly as long as the session on this machine (hub.ts refresh
 * guard). Older families of the same session identity are revoked. */
export function mintSessionCredential(sql: SqlStorage, sessionId: string, machineId: string, tokens: { accessHash: string; refreshHash: string }, now: number): { actor: string; expires_at: string } | null {
  const session: any = sql.exec("SELECT id, machine_id FROM session_control_sessions WHERE id = ? AND state = 'running' AND binding_json IS NOT NULL", sessionId).toArray()[0];
  if (!session || session.machine_id !== machineId) return null;
  const actor = ensureSessionIdentity(sql, sessionId, now);
  const at = new Date(now).toISOString();
  sql.exec("UPDATE credentials SET revoked_at = ? WHERE kind = 'agent' AND registry_actor = ? AND revoked_at IS NULL", at, actor);
  const id = crypto.randomUUID();
  const expires = new Date(now + CREDENTIAL_TTL_MS).toISOString();
  sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor)
    VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, id, tokens.accessHash, tokens.refreshHash, id, actor, machineId, actor,
    JSON.stringify(['msg:send', 'msg:read']), 'host:' + machineId, at, expires, actor);
  return { actor, expires_at: expires };
}

/** AC2: a session's messaging credential stops working the moment the session
 * stops or rebinds — called from the session-control stop/bind transitions
 * (the host-poll sweep in revokeEndedSessionCredentials is the backstop). */
export function revokeSessionCredentials(sql: SqlStorage, sessionId: string, now: number): void {
  sql.exec("UPDATE credentials SET revoked_at = ? WHERE kind = 'agent' AND registry_actor = ? AND revoked_at IS NULL",
    new Date(now).toISOString(), `${SESSION_PROJECT}:${sessionId}`);
}

/** Refresh-guard for session credentials (hub.ts refreshCredential): the
 * family may rotate only while the session is still running on the machine
 * the credential was minted for. */
export function sessionRefreshAllowed(sql: SqlStorage, registryActor: string | null, machineId: string | null): boolean {
  const sessionId = registryActor ? sessionIdOf(registryActor) : null;
  if (!sessionId) return false;
  const session: any = sql.exec("SELECT machine_id FROM session_control_sessions WHERE id = ? AND state = 'running'", sessionId).toArray()[0];
  return Boolean(session) && String(session.machine_id ?? '') === String(machineId ?? '');
}

/** A stopped or ended session's messaging identity stops working: bounded
 * by the session-identity rows, indexed revocation by registry_actor. */
export function revokeEndedSessionCredentials(sql: SqlStorage, now: number): void {
  const at = new Date(now).toISOString();
  const ended = sql.exec(`SELECT a.tag FROM agents a LEFT JOIN session_control_sessions s ON s.id = a.tag
    WHERE a.project = ? AND (s.id IS NULL OR s.state <> 'running') LIMIT 100`, SESSION_PROJECT).toArray() as any[];
  for (const row of ended) {
    sql.exec("UPDATE credentials SET revoked_at = ? WHERE registry_actor = ? AND kind = 'agent' AND revoked_at IS NULL", at, `${SESSION_PROJECT}:${row.tag}`);
  }
}
