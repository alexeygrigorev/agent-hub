/** Hub session orchestration (M12e): durable start/send/capture/stop intents over existing fleet placement.
 *
 * Placement itself is never reimplemented here: starts delegate to the existing
 * fleet reserve through an injected hook (wired in src/hub.ts after owner ACK).
 * Without that hook the start route holds honestly instead of launching.
 * Native execution belongs to the sibling host worker; this module only tracks
 * recorded / transported / acked / refused / uncertain command states with
 * correlation + generation binding. See docs/SESSION-CONTROL.md (proposal).
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { redact } from './routing';

export const SESSION_CONTROL_SCHEMA = `
CREATE TABLE IF NOT EXISTS session_control_sessions (id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, fleet_launch_id TEXT, machine_id TEXT, engine TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, binding_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
CREATE TABLE IF NOT EXISTS session_control_commands (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL, correlation TEXT NOT NULL, poll_at TEXT, ack_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, actor, request_key));
`;

type Caller = { kind?: string; actor?: string; isAdmin?: boolean; machineId?: string; tokenId?: string };
type Auth = { caller?: Caller; scopes?: string[]; error?: Response };
type ReserveHook = (input: any, auth: Auth) => { value: any; status: number };

const LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const UUID = /^[a-f0-9-]{36}$/;
const TIERS = new Set(['quick', 'daily', 'deep']);
const COMMANDS = new Set(['send', 'capture', 'stop']);
const ACK_OUTCOMES = new Set(['delivered', 'refused', 'uncertain']);
const HEADERS = { 'Cache-Control': 'private, no-store' };
const BOUND_STATES = new Set(['running']);
const STOPPABLE = new Set(['offered', 'starting', 'running', 'uncertain']);

const label = (x: unknown): x is string => typeof x === 'string' && LABEL.test(x);
const response = (value: unknown, status = 200) => Response.json(value, { status, headers: HEADERS });
const obj = (x: any, keys: string[]) => x !== null && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).every(k => keys.includes(k));
const parsed = (value: any, fallback: any = null) => { try { return JSON.parse(value); } catch { return fallback; } };

function cwdOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 256) return false;
  if (value.startsWith('/') || value.includes('\\') || value.includes(':') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value.split('/').every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
}

async function body(request: Request): Promise<any> {
  if (!request.body) return null;
  const reader = request.body.getReader();
  let size = 0;
  const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 32768) { await reader.cancel(); return null; }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return null; } finally { reader.releaseLock(); }
}

export class SessionControlService {
  constructor(
    private sql: SqlStorage,
    private transaction: <T>(fn: () => T) => T,
    private now: () => number = Date.now,
    private hooks: { reserve?: ReserveHook } = {},
  ) {}

  init() {
    for (const stmt of SESSION_CONTROL_SCHEMA.split(';')) {
      if (stmt.trim()) this.sql.exec(stmt);
    }
  }

  private at() { return new Date(this.now()).toISOString(); }

  private view(row: any) {
    const request = parsed(row.request_json, {});
    return {
      id: row.id, state: row.state, machine: row.machine_id ?? null, engine: row.engine ?? null,
      mode: row.mode, access: request.access?.mode || 'legacy_unspecified',
      fleet_launch_id: row.fleet_launch_id ?? null,
      binding: row.binding_json ? parsed(row.binding_json, null) : null,
      created_at: row.created_at, updated_at: row.updated_at,
    };
  }

  /** Server-internal binding seam for the future hub.ts wiring (post-ACK). Not an HTTP route. */
  recordBinding(sessionId: string, binding: { native_id: string; tag: string; workspace: string }): { ok: true; generation: number } | { ok: false; error: string } {
    return this.transaction(() => {
      const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
      if (!row) return { ok: false as const, error: 'not_found' };
      if (!['offered', 'starting', 'uncertain'].includes(row.state)) return { ok: false as const, error: 'binding_not_allowed' };
      if (typeof binding.native_id !== 'string' || !binding.native_id || typeof binding.tag !== 'string' || !binding.tag ||
          typeof binding.workspace !== 'string' || !binding.workspace) return { ok: false as const, error: 'invalid_binding' };
      const prior = row.binding_json ? parsed(row.binding_json, null) : null;
      if (prior && prior.native_id !== binding.native_id) return { ok: false as const, error: 'binding_conflict' };
      const generation = (prior?.generation ?? 0) + 1;
      this.sql.exec('UPDATE session_control_sessions SET state = ?, binding_json = ?, updated_at = ? WHERE id = ?',
        'running', JSON.stringify({ ...binding, generation }), this.at(), sessionId);
      return { ok: true as const, generation };
    });
  }

  async handle(request: Request, auth: Auth): Promise<Response> {
    if (auth.error) return auth.error;
    const url = new URL(request.url);
    const path = url.pathname;
    const caller = auth.caller;
    if (!caller) return response({ error: 'unauthorized' }, 401);

    const admin = caller.isAdmin === true;
    const writer = admin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch'));
    const host = caller.kind === 'host' && !!caller.machineId && auth.scopes?.includes('host');

    if (path === '/api/sessions' && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['key', 'engine', 'tier', 'machine', 'workspace', 'cwd', 'brief', 'access', 'mode']) ||
          !label(input.key) || !label(input.workspace) ||
          (input.engine !== undefined && !label(input.engine)) ||
          (input.machine !== undefined && !label(input.machine)) ||
          (input.tier !== undefined && !TIERS.has(input.tier)) ||
          (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) ||
          (input.cwd !== undefined && !cwdOk(input.cwd)) ||
          (input.brief !== undefined && (typeof input.brief !== 'string' || !input.brief.trim() || input.brief.length > 8000))) {
        return response({ error: 'invalid_session_request' }, 400);
      }
      const mode = input.mode || 'headless';
      if (mode === 'interactive' && input.brief !== undefined) {
        return response({ error: 'interactive_starts_empty' }, 400);
      }
      if (mode === 'headless' && input.brief !== undefined && !input.brief.trim()) {
        return response({ error: 'invalid_session_request' }, 400);
      }
      const actor = caller.actor || caller.machineId || 'admin';
      const canonical = JSON.stringify({
        engine: input.engine ?? null, tier: input.tier ?? null, machine: input.machine ?? null,
        workspace: input.workspace, cwd: input.cwd ?? null, brief: input.brief ?? null,
        mode, access: input.access ?? null,
      });
      return this.transaction(() => {
        const prior: any = this.sql.exec(
          'SELECT * FROM session_control_sessions WHERE actor = ? AND request_key = ?', actor, input.key).toArray()[0];
        if (prior) {
          if (prior.request_json !== canonical) return response({ error: 'idempotency_conflict' }, 409);
          if (prior.fleet_launch_id) return response({ ...this.view(prior), replay: true });
          // A held intent may be placed once capacity/admission recovers; the fleet
          // reserve stays idempotent on its own derived key, so this cannot double-launch.
        }
        if (!this.hooks.reserve) {
          if (!prior) {
            const id = crypto.randomUUID();
            const at = this.at();
            this.sql.exec(
              'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
              id, actor, input.key, canonical, mode, 'held', at, at);
            const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
            return response({ ...this.view(row), hold: 'no_placement_authority' }, 409);
          }
          return response({ ...this.view(prior), hold: 'no_placement_authority', replay: true }, 409);
        }
        const id = prior?.id || crypto.randomUUID();
        const at = this.at();
        if (!prior) {
          this.sql.exec(
            'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            id, actor, input.key, canonical, mode, 'queued', at, at);
        }
        const reserved = this.hooks.reserve({
          mode, machine: input.machine, engine: input.engine, workspace: input.workspace,
          prompt: input.brief || '', access: input.access,
          idempotency_key: 'session-control-' + id,
        }, auth);
        if (reserved.status >= 300) {
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'held', at, id);
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
          return response({ ...this.view(row), hold: reserved.value }, 409);
        }
        this.sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = ?, engine = ?, state = ?, updated_at = ? WHERE id = ?',
          reserved.value.id, reserved.value.machine ?? null, reserved.value.engine ?? null, reserved.value.state || 'offered', at, id);
        const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
        return response({ ...this.view(row), replay: !!prior }, prior ? 200 : 201);
      });
    }

    if (path === '/api/sessions' && request.method === 'GET') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 50));
      const rows = admin
        ? this.sql.exec('SELECT * FROM session_control_sessions ORDER BY created_at DESC LIMIT ?', limit).toArray()
        : this.sql.exec('SELECT * FROM session_control_sessions WHERE actor = ? ORDER BY created_at DESC LIMIT ?', caller.actor, limit).toArray();
      return response({ sessions: rows.map(r => this.view(r)) });
    }

    const sessionMatch = /^\/api\/sessions\/([a-f0-9-]{36})(\/(send|capture|stop|commands))?$/.exec(path);
    if (sessionMatch) {
      const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionMatch[1]).toArray()[0];
      if (!session || (!admin && session.actor !== caller.actor)) return response({ error: 'not_found' }, 404);
      if (!sessionMatch[2] && request.method === 'GET') return response(this.view(session));
      if (sessionMatch[3] === 'commands' && request.method === 'GET') {
        const commands = this.sql.exec('SELECT * FROM session_control_commands WHERE session_id = ? ORDER BY created_at, id', session.id).toArray()
          .map((c: any) => ({ ...c, payload: parsed(c.payload_json, null), ack: c.ack_json ? parsed(c.ack_json, null) : null }));
        return response({ session_id: session.id, commands });
      }
      if (sessionMatch[3] && request.method === 'POST') {
        if (!writer || (!admin && session.actor !== caller.actor)) return response({ error: 'forbidden' }, 403);
        const kind = sessionMatch[3];
        const input = await body(request);
        if (kind === 'send') {
          if (!obj(input, ['key', 'body']) || !label(input.key) ||
              typeof input.body !== 'string' || !input.body.trim() || input.body.length > 16000) {
            return response({ error: 'invalid_send' }, 400);
          }
          if (!BOUND_STATES.has(session.state)) return response({ error: 'session_not_bound' }, 409);
          return this.transaction(() => this.enqueue(session, kind, actorName(caller), input.key, { body: input.body }));
        }
        if (kind === 'capture') {
          if (!obj(input, ['key', 'max_bytes']) || !label(input.key) ||
              (input.max_bytes !== undefined && (!Number.isInteger(input.max_bytes) || input.max_bytes < 1 || input.max_bytes > 65536))) {
            return response({ error: 'invalid_capture' }, 400);
          }
          if (!BOUND_STATES.has(session.state)) return response({ error: 'session_not_bound' }, 409);
          return this.transaction(() => this.enqueue(session, kind, actorName(caller), input.key, { max_bytes: input.max_bytes ?? 65536 }));
        }
        if (!obj(input, ['key']) || !label(input.key)) return response({ error: 'invalid_stop' }, 400);
        if (!STOPPABLE.has(session.state)) return response({ error: 'session_not_stoppable' }, 409);
        return this.transaction(() => this.enqueue(session, kind, actorName(caller), input.key, {}));
      }
      return response({ error: 'not_found' }, 404);
    }

    if (path === '/api/session-commands/offers' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      return this.transaction(() => {
        const at = this.at();
        const rows = this.sql.exec(
          `SELECT c.* FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
           WHERE s.machine_id = ? AND c.state = 'recorded' ORDER BY c.created_at, c.id LIMIT 20`, caller.machineId!).toArray() as any[];
        const offers = [];
        for (const row of rows) {
          const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
          const binding = session?.binding_json ? parsed(session.binding_json, null) : null;
          if (!binding) continue;
          this.sql.exec("UPDATE session_control_commands SET state = 'transported', poll_at = ?, updated_at = ? WHERE id = ? AND state = 'recorded'", at, at, row.id);
          offers.push({
            command_id: row.id, session_id: row.session_id, kind: row.kind,
            native_tag: binding.tag, workspace: binding.workspace,
            payload: parsed(row.payload_json, null), correlation: row.correlation,
          });
        }
        return response({ offers });
      });
    }

    const ackMatch = /^\/api\/session-commands\/([a-f0-9-]{36})\/ack$/.exec(path);
    if (ackMatch && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['correlation', 'outcome', 'native', 'reason', 'capture', 'confirmed_exit']) ||
          typeof input.correlation !== 'string' || !ACK_OUTCOMES.has(input.outcome) ||
          !obj(input.native || {}, ['native_id', 'tag', 'workspace']) ||
          typeof input.native.native_id !== 'string' || !input.native.native_id ||
          typeof input.native.tag !== 'string' || !input.native.tag ||
          typeof input.native.workspace !== 'string' || !input.native.workspace ||
          (input.reason !== undefined && (typeof input.reason !== 'string' || input.reason.length > 512)) ||
          (input.capture !== undefined && (typeof input.capture !== 'string' || input.capture.length > 65536))) {
        return response({ error: 'invalid_ack' }, 400);
      }
      return this.transaction(() => {
        const row: any = this.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', ackMatch[1]).toArray()[0];
        if (!row || row.state !== 'transported') return response({ error: row ? 'invalid_transition' : 'not_found' }, row ? 409 : 404);
        const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
        if (!session || session.machine_id !== caller.machineId) return response({ error: 'not_found' }, 404);
        if (input.correlation !== row.correlation) return response({ error: 'correlation_mismatch' }, 409);
        const binding = session.binding_json ? parsed(session.binding_json, null) : null;
        if (!binding || input.native.native_id !== binding.native_id || input.native.tag !== binding.tag) {
          return response({ error: 'binding_mismatch' }, 409);
        }
        const at = this.at();
        if (input.outcome === 'delivered' && row.kind === 'stop' && input.confirmed_exit !== true) {
          return response({ error: 'confirmed_exit_required' }, 409);
        }
        const ack = {
          outcome: input.outcome, native: input.native,
          ...(input.reason !== undefined ? { reason: input.reason } : {}),
          ...(input.capture !== undefined ? { capture: redact(input.capture).slice(0, 65536) } : {}),
          ...(input.confirmed_exit !== undefined ? { confirmed_exit: input.confirmed_exit === true } : {}),
        };
        const state = input.outcome === 'delivered' ? 'acked' : input.outcome === 'refused' ? 'refused' : 'uncertain';
        this.sql.exec('UPDATE session_control_commands SET state = ?, ack_json = ?, updated_at = ? WHERE id = ?', state, JSON.stringify(ack), at, row.id);
        if (state === 'acked' && row.kind === 'stop') {
          this.sql.exec("UPDATE session_control_sessions SET state = 'stopped', updated_at = ? WHERE id = ?", at, session.id);
        }
        return response({ command_id: row.id, state });
      });
    }

    return response({ error: 'not_found' }, 404);
  }

  private enqueue(session: any, kind: string, actor: string, key: string, payload: any) {
    const prior: any = this.sql.exec(
      'SELECT * FROM session_control_commands WHERE session_id = ? AND actor = ? AND request_key = ?', session.id, actor, key).toArray()[0];
    if (prior) {
      if (prior.kind !== kind || JSON.stringify(parsed(prior.payload_json, null)) !== JSON.stringify(payload)) {
        return response({ error: 'idempotency_conflict' }, 409);
      }
      return response({ command_id: prior.id, state: prior.state, replay: true });
    }
    const binding = session.binding_json ? parsed(session.binding_json, null) : null;
    const generation = binding?.generation ?? 0;
    const id = crypto.randomUUID();
    const at = this.at();
    const correlation = `cmd:${id}:gen:${generation}`;
    this.sql.exec(
      'INSERT INTO session_control_commands (id, session_id, kind, actor, request_key, payload_json, state, generation, correlation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
      id, session.id, kind, actor, key, JSON.stringify(payload), 'recorded', generation, correlation, at, at);
    return response({ command_id: id, state: 'recorded' }, 201);
  }
}

function actorName(caller: Caller): string {
  return caller.actor || caller.machineId || 'admin';
}
