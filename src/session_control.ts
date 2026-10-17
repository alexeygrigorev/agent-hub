/** Hub session orchestration (M12e): durable start/send/capture/stop intents over existing fleet placement.
 *
 * Placement itself is never reimplemented here: starts delegate to the existing
 * fleet reserve through an injected hook (the Hub wires the real
 * `FleetService.reserve`; tests inject a synthetic hook). Without that hook the
 * start route holds honestly instead of launching. Native execution belongs to
 * the sibling host worker; this module only tracks durable command custody:
 *
 *   recorded -> transported (host poll fetch receipt; NOT execution permission)
 *     -> claimed (atomic per-command execution fence, BEFORE any native effect)
 *     -> acked | transported | refused | uncertain
 *
 * Concurrent host processes and poll retries cannot execute the same command
 * twice: only the claim holder (machine + claim token, unexpired) may ack, an
 * expired or lost-effect attempt becomes `uncertain` (never blindly reissued),
 * and a transport mark alone never authorizes an effect. SEND delivery
 * (`acked`) additionally requires a correlated receiver proof validated by
 * `validateSendProof` — host assertion, PTY bytes, or inbox persistence alone
 * are reported as `recorded` / `transported` / `uncertain`, never as processed.
 * See docs/SESSION-CONTROL.md.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { credentialActor } from './agent_registry';
import { redact } from './routing';
import { taskTier } from './model-routing';
import { approvalAuthorityMatches } from './host_enrollment';

export const SESSION_CONTROL_SCHEMA = `
CREATE TABLE IF NOT EXISTS session_control_sessions (id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, fleet_launch_id TEXT, machine_id TEXT, engine TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, binding_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
CREATE TABLE IF NOT EXISTS session_control_commands (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL, correlation TEXT NOT NULL, poll_at TEXT, ack_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, actor, request_key));
CREATE TABLE IF NOT EXISTS session_control_proofs (reply_envelope_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL);
`;

type Caller = { kind?: string; actor?: string; isAdmin?: boolean; machineId?: string; tokenId?: string };
type Auth = { caller?: Caller; scopes?: string[]; error?: Response };
type ReserveHook = (input: any, auth: Auth) => { value: any; status: number };
export type SessionControlOptions = { maxPendingCommands?: number; maxSessionCommands?: number; claimTtlMs?: number };

const LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const UUID_RE = /^[a-f0-9-]{36}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const COMMANDS = new Set(['send', 'capture', 'stop']);
const ACK_OUTCOMES = new Set(['delivered', 'transported', 'refused', 'uncertain']);
const HEADERS = { 'Cache-Control': 'private, no-store' };
const BOUND_STATES = new Set(['running']);
const STOPPABLE = new Set(['offered', 'starting', 'running', 'uncertain']);
const BINDABLE = new Set(['offered', 'starting', 'uncertain', 'running']);
const CLAIMABLE = new Set(['recorded', 'transported']);

const label = (x: unknown): x is string => typeof x === 'string' && LABEL.test(x);
const response = (value: unknown, status = 200) => Response.json(value, { status, headers: HEADERS });
const obj = (x: any, keys: string[]) => x !== null && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).every(k => keys.includes(k));
const parsed = (value: any, fallback: any = null) => { try { return JSON.parse(value); } catch { return fallback; } };

function cwdOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 256) return false;
  if (value.startsWith('/') || value.includes('\\') || value.includes(':') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value.split('/').every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
}

function absoluteWorkspaceOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 512) return false;
  if (!value.startsWith('/') || value.includes('\\') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value.split('/').every(seg => seg !== '..');
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

/** Host-verified native reply provenance required before a SEND is marked delivered.
 *
 * This is deliberately NOT cryptographic target authentication: the hub cannot
 * verify native envelopes server-side. The host retrieves the native log /
 * envelope itself, verifies the reply references the delivered message
 * (correlation/digest), and attests the provenance below. The hub checks
 * shape, exact sender binding (UUID + tag + workspace + generation +
 * native_created_at), reply linkage, and global envelope uniqueness — a bare
 * object echoing the public correlation with a non-empty id is NOT proof and
 * any absent proof keeps the command at recorded / transported / uncertain.
 */
export type SendProof = {
  native_message_id: string;
  reply_to_native_id: string;
  reply_digest: string;
  sender_native_id: string;
  sender_tag: string;
  sender_workspace: string;
  sender_generation: number;
  sender_created_at_ms: number;
};

export type SendProofExpectation = {
  native_id: string;
  tag: string;
  workspace: string;
  generation: number;
  native_created_at_ms: number;
};

const HEX64 = /^[0-9a-f]{64}$/i;

export function validateSendProof(proof: any, expected: SendProofExpectation): { ok: true } | { ok: false; error: string } {
  const fail = (error: string) => ({ ok: false as const, error });
  if (!proof || typeof proof !== 'object' || Array.isArray(proof)) return fail('proof_required');
  const keys = ['native_message_id', 'reply_to_native_id', 'reply_digest', 'sender_native_id',
    'sender_tag', 'sender_workspace', 'sender_generation', 'sender_created_at_ms'];
  if (!obj(proof, keys)) return fail('invalid_proof_shape');
  if (typeof proof.native_message_id !== 'string' || !proof.native_message_id || proof.native_message_id.length > 256 ||
      typeof proof.reply_to_native_id !== 'string' || !proof.reply_to_native_id || proof.reply_to_native_id.length > 256 ||
      typeof proof.reply_digest !== 'string' || !HEX64.test(proof.reply_digest)) return fail('invalid_proof_shape');
  // A message is never its own reply; the reply envelope must reference the
  // delivered message envelope as retrieved from the native log by the host.
  if (proof.reply_to_native_id === proof.native_message_id) return fail('proof_reply_mismatch');
  if (typeof proof.sender_native_id !== 'string' || !UUID_V4.test(proof.sender_native_id) ||
      typeof proof.sender_tag !== 'string' || !proof.sender_tag ||
      typeof proof.sender_workspace !== 'string' || !proof.sender_workspace ||
      !Number.isInteger(proof.sender_generation) ||
      !Number.isInteger(proof.sender_created_at_ms) || proof.sender_created_at_ms <= 0) return fail('invalid_proof_shape');
  if (proof.sender_native_id !== expected.native_id || proof.sender_tag !== expected.tag ||
      proof.sender_workspace !== expected.workspace) return fail('proof_sender_mismatch');
  if (proof.sender_generation !== expected.generation ||
      proof.sender_created_at_ms !== expected.native_created_at_ms) return fail('proof_generation_stale');
  return { ok: true };
}

export class SessionControlService {
  private maxPending: number;
  private maxPerSession: number;
  private claimTtlMs: number;
  constructor(
    private sql: SqlStorage,
    private transaction: <T>(fn: () => T) => T,
    private now: () => number = Date.now,
    private hooks: { reserve?: ReserveHook } = {},
    options: SessionControlOptions = {},
  ) {
    this.maxPending = options.maxPendingCommands ?? 2000;
    this.maxPerSession = options.maxSessionCommands ?? 200;
    this.claimTtlMs = options.claimTtlMs ?? 300000;
  }

  init() {
    for (const stmt of SESSION_CONTROL_SCHEMA.split(';')) {
      if (stmt.trim()) this.sql.exec(stmt);
    }
    // Additive migration for tables created by the earlier reserve-only draft.
    const sessionCols = new Set(this.sql.exec('PRAGMA table_info(session_control_sessions)').toArray().map((c: any) => String(c.name)));
    for (const [col, type] of [['owner_token_id', 'TEXT'], ['owner_family', 'TEXT'], ['host_family', 'TEXT']] as const) {
      if (!sessionCols.has(col)) this.sql.exec(`ALTER TABLE session_control_sessions ADD COLUMN ${col} ${type}`);
    }
    const commandCols = new Set(this.sql.exec('PRAGMA table_info(session_control_commands)').toArray().map((c: any) => String(c.name)));
    for (const [col, type] of [['owner_token_id', 'TEXT'], ['owner_family', 'TEXT'], ['holder_machine', 'TEXT'], ['claim_token', 'TEXT'], ['claim_expires_at', 'TEXT'], ['holder_attempt', 'TEXT'], ['attempts', 'INTEGER DEFAULT 0']] as const) {
      if (!commandCols.has(col)) this.sql.exec(`ALTER TABLE session_control_commands ADD COLUMN ${col} ${type}`);
    }
  }

  private at() { return new Date(this.now()).toISOString(); }

  /** Current credential family of the presenting token, or null when the
   * credential row is gone, revoked, or expired. The hub already rejected
   * revoked/expired presenting credentials before this service runs; this is
   * the continuity half: refresh keeps the family, delete/reregister changes it. */
  private presentingFamily(caller: Caller): string | null {
    if (!caller.tokenId) return null;
    try {
      const row: any = this.sql.exec('SELECT family_id, revoked_at, expires_at FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
      if (!row || row.revoked_at) return null;
      const expires = Date.parse(row.expires_at);
      if (!Number.isFinite(expires) || expires <= this.now()) return null;
      return typeof row.family_id === 'string' && row.family_id ? row.family_id : null;
    } catch { return null; }
  }

  /** Fresh ownership authorization on every read/write/replay. Admins bypass;
   * everyone else must present the owning actor with a LIVE credential in the
   * owning family (refresh continuity), verified inside the current
   * transaction AFTER any awaited request body. A matching token id alone
   * never authorizes: revoked/expired/rebound credentials fail closed, and
   * agent callers must additionally resolve a live registry incarnation via
   * credentialActor. Anything else renders as not_found (never an oracle). */
  private agentLive(caller: Caller): boolean {
    if (caller.kind !== 'agent') return true;
    if (!caller.tokenId) return false;
    const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
    if (!cred || cred.revoked_at) return false;
    const expires = Date.parse(cred.expires_at);
    if (!Number.isFinite(expires) || expires <= this.now()) return false;
    return credentialActor(this.sql, cred) !== null;
  }

  private ownerOk(row: { actor: string; owner_token_id?: string | null; owner_family?: string | null }, caller: Caller): boolean {
    if (caller.isAdmin === true) return true;
    if (!caller.actor || row.actor !== caller.actor) return false;
    const family = this.presentingFamily(caller);
    if (!family) return false;
    if (row.owner_token_id && caller.tokenId && row.owner_token_id === caller.tokenId) {
      return this.agentLive(caller);
    }
    if (family && row.owner_family && family === row.owner_family) {
      // Refresh continuity: adopt the rotated token id for future checks.
      const table = (row as any).session_id !== undefined ? 'session_control_commands' : 'session_control_sessions';
      try { this.sql.exec(`UPDATE ${table} SET owner_token_id = ? WHERE id = ?`, caller.tokenId, (row as any).id); } catch { /* bookkeeping only */ }
      return this.agentLive(caller);
    }
    return false;
  }

  /** Host machine binding plus credential-family continuity for poll/claim/ack/binding.
   *
   * Beyond the adopted session host_family, the CURRENT machine registry is
   * consulted live on every call: the machine row must still exist (a deleted
   * machine binds nothing), and when its enrolled credential resolves to a
   * known family, the presenter must be in that family (a superseded pre-bind
   * family cannot hijack the first binding; refresh rotations keep the family
   * so genuine authority keeps working). */
  private hostOk(session: { machine_id?: string | null; host_family?: string | null }, caller: Caller): { ok: true } | { ok: false; error: string } {
    if (!session.machine_id || session.machine_id !== caller.machineId) return { ok: false, error: 'not_found' };
    if (caller.isAdmin === true) return { ok: true };
    const family = this.presentingFamily(caller);
    if (!family) return { ok: false, error: 'credential_changed' };
    try {
      const m: any = this.sql.exec(
        'SELECT m.id, m.credential_id, m.approval_seq, c.family_id AS machine_family FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?',
        session.machine_id).toArray()[0];
      if (!m) return { ok: false, error: 'not_found' };
      const credential: any = this.sql.exec('SELECT approval_seq FROM credentials WHERE id = ?', caller.tokenId!).toArray()[0];
      if (!m.credential_id || !credential || !approvalAuthorityMatches(credential.approval_seq, m.approval_seq)) return { ok: false, error: 'host_credential_changed' };
      if (m.machine_family && m.machine_family !== family) return { ok: false, error: 'host_credential_changed' };
    } catch {
      return { ok: false, error: 'credential_changed' };
    }
    if (session.host_family && session.host_family !== family) return { ok: false, error: 'host_credential_changed' };
    return { ok: true };
  }

  private adoptHostFamily(sessionId: string, caller: Caller) {
    if (caller.isAdmin === true || !caller.tokenId) return;
    const family = this.presentingFamily(caller);
    if (!family) return;
    try { this.sql.exec('UPDATE session_control_sessions SET host_family = COALESCE(host_family, ?) WHERE id = ?', family, sessionId); } catch { /* bookkeeping only */ }
  }

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

  /** Cursor pagination over (created_at, id): complete pages, no silent truncation. */
  private cursorClause(cursor: unknown, params: any[], desc: boolean): string {
    if (typeof cursor !== 'string' || !cursor) return '';
    const sep = cursor.indexOf('|');
    if (sep < 0) return '';
    const [at, id] = [cursor.slice(0, sep), cursor.slice(sep + 1)];
    if (!at || !UUID_RE.test(id)) return '';
    params.push(at, at, id);
    const op = desc ? '<' : '>';
    return `AND (created_at ${op} ? OR (created_at = ? AND id ${op} ?))`;
  }

  /** Server-internal binding seam (used by tests and future hub wiring after
   * the fleet claim/result half). The HTTP binding route below is the host path. */
  recordBinding(sessionId: string, binding: { native_id: string; tag: string; workspace: string; native_created_at_ms: number }): { ok: true; generation: number } | { ok: false; error: string } {
    return this.transaction(() => this.applyBinding(sessionId, binding, null));
  }

  private applyBinding(sessionId: string, binding: { native_id: string; tag: string; workspace: string; native_created_at_ms?: number; evidence?: unknown }, reporter: string | null): { ok: true; generation: number } | { ok: false; error: string } {
    const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
    if (!row) return { ok: false as const, error: 'not_found' };
    if (!BINDABLE.has(row.state)) return { ok: false as const, error: 'binding_not_allowed' };
    // Canonical assigned launch must exist before any native binding is
    // accepted: Fleet owns start, so a session without a fleet_launch_id has
    // no assigned machine/workspace/launch to reconcile. Never grant control
    // of an arbitrary unrelated native UUID just because a host reports it.
    if (!row.fleet_launch_id) return { ok: false as const, error: 'binding_not_allowed' };
    if (typeof binding.native_id !== 'string' || !UUID_V4.test(binding.native_id) ||
        typeof binding.tag !== 'string' || !binding.tag || binding.tag.startsWith('-') ||
        !absoluteWorkspaceOk(binding.workspace)) return { ok: false as const, error: 'invalid_binding' };
    // Native incarnation evidence is mandatory: UUID + tag + workspace +
    // generation alone is hub-side only without the native created_at.
    if (!Number.isInteger(binding.native_created_at_ms) || (binding.native_created_at_ms as number) <= 0 ||
        (binding.native_created_at_ms as number) > this.now() + 60000) {
      return { ok: false as const, error: 'invalid_binding' };
    }
    const prior = row.binding_json ? parsed(row.binding_json, null) : null;
    // Exact UUID + tag + workspace reconciliation: a different native id is a
    // conflict, never a silent adoption (protects tag reuse across generations).
    if (prior && (prior.native_id !== binding.native_id || prior.tag !== binding.tag || prior.workspace !== binding.workspace)) {
      return { ok: false as const, error: 'binding_conflict' };
    }
    // Canonical Fleet reconciliation (read-only; Fleet owns the launch
    // lifecycle, this service never writes fleet rows). The assigned launch
    // must exist on the session's machine in a live post-start state, the
    // tag must be the canonical Fleet consumer tag for that launch
    // ('hub-<fleet_launch_id>', the exact argv the existing Dispatcher
    // starts), and an already-reported native id must match. Unstarted
    // (offered), failed/exited/uncertain, foreign-machine, or inconsistent
    // launches are refused. The binding workspace (absolute native path) is
    // NOT compared to the Fleet workspace label: they live in different
    // domains (hub label vs. canonical native path); machine + tag + native
    // id + state carry the reconciliation instead.
    const launch: any = this.sql.exec(
      'SELECT id, machine_id, workspace, state, native_id FROM fleet_launches WHERE id = ?',
      row.fleet_launch_id).toArray()[0];
    if (!launch) return { ok: false as const, error: 'missing_fleet_launch' };
    if (launch.machine_id !== row.machine_id) return { ok: false as const, error: 'fleet_machine_mismatch' };
    if (launch.state !== 'starting' && launch.state !== 'running') {
      return { ok: false as const, error: 'fleet_launch_state' };
    }
    if (binding.tag !== 'hub-' + launch.id) return { ok: false as const, error: 'fleet_tag_mismatch' };
    if (launch.native_id && launch.native_id !== binding.native_id) {
      return { ok: false as const, error: 'fleet_native_mismatch' };
    }
    // Authoritative native identity: the existing Fleet sessions report must
    // already carry this exact native session (machine + UUID + tag +
    // workspace, non-terminal phase). Binding before the Fleet consumer
    // reports the native identity is refused — the launch row alone cannot
    // prove the tag/workspace mapping, and an arbitrary workspace must never
    // be adopted on a launch id plus an unreported UUID.
    const reported: any = this.sql.exec(
      'SELECT tag, workspace, phase, engine, cwd FROM fleet_sessions WHERE machine_id = ? AND native_id = ?',
      row.machine_id, binding.native_id).toArray()[0];
    if (!reported) return { ok: false as const, error: 'fleet_native_unreported' };
    if (reported.tag !== binding.tag || reported.workspace !== binding.workspace) {
      return { ok: false as const, error: 'fleet_native_mismatch' };
    }
    if (reported.phase === 'exited' || reported.phase === 'dead' ||
        reported.phase === 'forgotten' || reported.phase === 'pruned') {
      return { ok: false as const, error: 'fleet_native_terminal' };
    }
    // Expected engine enforcement (vs canonical Fleet report AND actual
    // native catalog, both host-reported): a known mismatch hard-rejects with
    // no mutation — a real SHELL row never binds a requested codex session.
    // Sessions created without an engine keep the legacy behavior.
    if (row.engine && reported.engine !== row.engine) {
      return { ok: false as const, error: 'fleet_engine_mismatch' };
    }
    // Requested cwd verification against the reported actual catalog cwd: the
    // relative request (workspace-root-relative) must match the reported
    // absolute path exactly or as its trailing segments. Unreported catalog
    // cwd holds honestly — the hub never claims an executed cwd it cannot see.
    const requestedCwd: unknown = parsed(row.request_json, {}).cwd;
    if (typeof requestedCwd === 'string' && requestedCwd) {
      const seen = typeof reported.cwd === 'string' ? reported.cwd : null;
      const want = requestedCwd.replace(/\/+$/, '');
      if (!seen || !(seen === want || seen.endsWith('/' + want))) {
        return { ok: false as const, error: 'fleet_cwd_mismatch' };
      }
    }
    const generation = (prior?.generation ?? 0) + 1;
    this.sql.exec('UPDATE session_control_sessions SET state = ?, binding_json = ?, updated_at = ? WHERE id = ?',
      'running', JSON.stringify({ native_id: binding.native_id, tag: binding.tag, workspace: binding.workspace, generation,
        native_created_at_ms: binding.native_created_at_ms,
        ...(reporter ? { reported_by: reporter } : {}), reported_at: this.at() }), this.at(), sessionId);
    return { ok: true as const, generation };
  }

  private claimExpired(row: any): boolean {
    if (!row.claim_expires_at) return false;
    const at = Date.parse(row.claim_expires_at);
    return !Number.isFinite(at) || at <= this.now();
  }

  private markUncertain(row: any, reason: string) {
    this.sql.exec("UPDATE session_control_commands SET state = 'uncertain', ack_json = ?, updated_at = ? WHERE id = ?",
      JSON.stringify({ outcome: 'uncertain', reason }), this.at(), row.id);
  }

  /** Late-reply reconciliation for an uncertain SEND (the single post-terminal
   * transition). Real recipients ordinarily reply minutes after the execution
   * lease lapses; discarding those replies would strand normal delayed
   * delivery. This path records the proven outcome WITHOUT any new effect
   * and WITHOUT re-opening claiming (claim on uncertain still 409s):
   *
   * - same command, same attempt (claim token + correlation match the fence),
   *   same executor machine, fresh live host authority;
   * - outcome `delivered` with a valid, still-unused reply proof against the
   *   CURRENT binding (stale generations fail closed as usual).
   *
   * Capture/stop and proof-less outcomes stay terminal: without a reply
   * proof there is nothing to reconcile. Returns the success response, or
   * null when this ack is not an eligible late reply.
   */
  private lateReplyAck(row: any, input: any, caller: Caller): Response | null {
    if (row.kind !== 'send' || input.outcome !== 'delivered') return null;
    if (typeof input.correlation !== 'string' || input.correlation !== row.correlation) return null;
    if (typeof input.claim_token !== 'string' || !input.claim_token ||
        input.claim_token !== row.claim_token) return null;
    const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
    if (!session || session.machine_id !== caller.machineId) return null;
    const gate = this.hostOk(session, caller);
    if (!gate.ok) return null;
    if (row.holder_machine !== caller.machineId) return null;
    const binding = session.binding_json ? parsed(session.binding_json, null) : null;
    if (!binding || binding.native_created_at_ms === undefined || binding.native_created_at_ms === null) return null;
    if (input.native.native_id !== binding.native_id || input.native.tag !== binding.tag ||
        input.native.workspace !== binding.workspace) return null;
    const proof = validateSendProof(input.proof, { native_id: binding.native_id, tag: binding.tag,
      workspace: binding.workspace, generation: binding.generation ?? 0,
      native_created_at_ms: binding.native_created_at_ms });
    if (!proof.ok) return null;
    // Proof freshness was established by the handler's cross-state dedup
    // before reaching this branch (same-command duplicates replay success,
    // cross-command reuse is proof_replayed); reaching here means fresh.
    const at = this.at();
    const ack: any = {
      outcome: 'delivered', native: input.native, claim_attempt: row.attempts ?? 1,
      claim_token: input.claim_token,
      ...(row.holder_attempt ? { holder_attempt: row.holder_attempt } : {}),
      ...(typeof input.reason === 'string' ? { reason: input.reason } : {}),
      proof: input.proof, late_reconciled: true,
    };
    this.sql.exec('UPDATE session_control_commands SET state = ?, ack_json = ?, updated_at = ? WHERE id = ?',
      'acked', JSON.stringify(ack), at, row.id);
    this.sql.exec('INSERT OR IGNORE INTO session_control_proofs (reply_envelope_id, command_id, session_id, created_at) VALUES (?, ?, ?, ?)',
      input.proof.reply_to_native_id, row.id, row.session_id, at);
    return response({ command_id: row.id, state: 'acked' });
  }

  /** Idempotent lost-ACK reconciliation: a re-submit that is byte-identical in
   * the fields that define the recorded outcome (claim token, correlation via
   * caller, outcome, native triple, proof envelope identity, capture bytes,
   * confirmed exit) replays success WITHOUT repeating any native effect.
   * Anything else keeps the existing terminal errors. */
  private sameAck(row: any, input: any): boolean {
    const stored = row.ack_json ? parsed(row.ack_json, null) : null;
    if (!stored || typeof stored !== 'object') return false;
    if (stored.claim_token !== input.claim_token || stored.outcome !== input.outcome) return false;
    const a = stored.native || {};
    const b = input.native || {};
    if (a.native_id !== b.native_id || a.tag !== b.tag || a.workspace !== b.workspace) return false;
    if ((stored.confirmed_exit === true) !== (input.confirmed_exit === true)) return false;
    if (stored.proof || input.proof) {
      const p = stored.proof || {};
      const q = input.proof || {};
      if (p.native_message_id !== q.native_message_id ||
          p.reply_to_native_id !== q.reply_to_native_id ||
          p.reply_digest !== q.reply_digest) return false;
    }
    if ((stored.capture ?? null) !== (input.capture ?? null)) {
      // Capture bytes are server-redacted before storage, so compare against
      // the redacted form of the resubmitted bytes.
      if (redact(String(input.capture ?? '')).slice(0, 65536) !== (stored.capture ?? null)) return false;
    }
    return true;
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

    if (path === '/api/sessions/host-bindings' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const rows = this.sql.exec("SELECT s.* FROM session_control_sessions s JOIN fleet_launches f ON f.id = s.fleet_launch_id WHERE s.machine_id = ? AND s.binding_json IS NULL AND s.state IN ('offered','starting','uncertain') AND f.state IN ('starting','running') LIMIT 100", caller.machineId!).toArray() as any[];
      return response({ sessions: rows.filter(row => this.hostOk(row, caller).ok).map(row => ({ id: row.id, fleet_launch_id: row.fleet_launch_id, engine: row.engine })) });
    }

    if (path === '/api/sessions' && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['key', 'engine', 'tier', 'machine', 'workspace', 'cwd', 'brief', 'access', 'mode']) ||
          !label(input.key) || !label(input.workspace) ||
          (input.engine !== undefined && !label(input.engine)) ||
          (input.machine !== undefined && !label(input.machine)) ||
          (input.cwd !== undefined && !cwdOk(input.cwd)) ||
          (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) ||
          (input.brief !== undefined && (typeof input.brief !== 'string' || !input.brief.trim() || input.brief.length > 8000))) {
        return response({ error: 'invalid_session_request' }, 400);
      }
      // Tier resolves through the EXISTING model-routing table inside fleet
      // plan (configured tier membership filters eligible engine/profile
      // routes; the existing score order still decides). Unknown tiers are
      // rejected here; a tier with no eligible route becomes an honest held
      // session, never a silent upgrade.
      if (input.tier !== undefined && taskTier({ tier: input.tier }) === null) {
        return response({ error: 'invalid_tier', detail: 'unknown tier; use configured tiers/aliases' }, 400);
      }
      // cwd is consumed by the existing fleet/host start adapter: validated
      // here as a workspace-relative confined path, carried through the fleet
      // reserve into the launch offer, resolved by the host against
      // allowlisted canonical roots with symlink confinement, and verified
      // against the actual native catalog at binding. The hub never claims an
      // executed cwd without that verification.
      if (input.cwd !== undefined && !cwdOk(input.cwd)) {
        return response({ error: 'invalid_cwd', detail: 'cwd must be a workspace-relative confined path' }, 400);
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
        engine: input.engine ?? null, machine: input.machine ?? null,
        workspace: input.workspace, cwd: input.cwd ?? null, brief: input.brief ?? null,
        mode, access: input.access ?? null,
      });
      return this.transaction(() => {
        const prior: any = this.sql.exec(
          'SELECT * FROM session_control_sessions WHERE actor = ? AND request_key = ?', actor, input.key).toArray()[0];
        if (prior) {
          if (!this.ownerOk(prior, caller)) return response({ error: 'not_found' }, 404);
          if (prior.request_json !== canonical) return response({ error: 'idempotency_conflict' }, 409);
          if (prior.fleet_launch_id) return response({ ...this.view(prior), replay: true });
          // A held intent may be placed once capacity/admission recovers; the fleet
          // reserve stays idempotent on its own derived key, so this cannot double-launch.
        }
        // Post-body liveness inside the mutation txn: a credential revoked,
        // expired, or re-enrolled while the request body streamed must not
        // create a session row (or stamp a NULL family). Admins bypass; every
        // other creator needs a live agent incarnation in a live family.
        let creatorFamily: string | null = null;
        if (!admin) {
          if (!this.agentLive(caller)) return response({ error: 'forbidden' }, 403);
          creatorFamily = this.presentingFamily(caller);
          if (!creatorFamily) return response({ error: 'forbidden' }, 403);
        }
        if (!this.hooks.reserve) {
          if (!prior) {
            const id = crypto.randomUUID();
            const at = this.at();
            this.sql.exec(
              'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, owner_token_id, owner_family, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
              id, actor, input.key, canonical, mode, 'held', caller.tokenId ?? null, creatorFamily, at, at);
            const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
            return response({ ...this.view(row), hold: 'no_placement_authority' }, 409);
          }
          return response({ ...this.view(prior), hold: 'no_placement_authority', replay: true }, 409);
        }
        const id = prior?.id || crypto.randomUUID();
        const at = this.at();
        if (!prior) {
          this.sql.exec(
            'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, owner_token_id, owner_family, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            id, actor, input.key, canonical, mode, 'queued', caller.tokenId ?? null, creatorFamily, at, at);
        }
      // engine / machine / tier / workspace / cwd / mode / prompt reach the
      // existing fleet reserve as REAL placement constraints (exact route
      // match + tier-membership filter + M6/M8 gates). Nothing is silently
      // discarded: unknown tiers/cwds are rejected above, and a tier with no
      // eligible route becomes an honest held session.
      const reserved = this.hooks.reserve({
        mode, machine: input.machine, engine: input.engine, tier: input.tier,
        workspace: input.workspace, cwd: input.cwd ?? null,
        prompt: input.brief || '', access: input.access,
        idempotency_key: 'session-control-' + id,
      }, auth);
        if (reserved.status === 400) {
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'refused', at, id);
          return response(reserved.value, 400);
        }
        if (reserved.status >= 300) {
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'held', at, id);
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
          return response({ ...this.view(row), hold: reserved.value, requested: { engine: input.engine ?? null, machine: input.machine ?? null, workspace: input.workspace } }, 409);
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
      const cursor = url.searchParams.get('cursor');
      const params: any[] = [];
      const cursorSql = this.cursorClause(cursor, params, true);
      const rows = (admin
        ? this.sql.exec(`SELECT * FROM session_control_sessions WHERE 1 = 1 ${cursorSql} ORDER BY created_at DESC, id DESC LIMIT ?`, ...params, limit + 1).toArray()
        : this.sql.exec(`SELECT * FROM session_control_sessions WHERE actor = ? ${cursorSql} ORDER BY created_at DESC, id DESC LIMIT ?`, caller.actor, ...params, limit + 1).toArray()
      ).filter((r: any) => this.ownerOk(r, caller));
      const page = rows.slice(0, limit);
      const next = rows.length > limit ? `${(page[page.length - 1] as any).created_at}|${(page[page.length - 1] as any).id}` : null;
      return response({ sessions: page.map(r => this.view(r)), next_cursor: next });
    }

    const sessionMatch = /^\/api\/sessions\/([a-f0-9-]{36})(\/(send|capture|stop|commands|binding))?$/.exec(path);
    if (sessionMatch) {
      const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionMatch[1]).toArray()[0];
      const sub = sessionMatch[3];
      if (sub === 'binding') {
        // Host-only native binding report: the Fleet result -> native binding
        // reconciliation path. The fleet offer was already claimed through the
        // existing fleet flow; this binds the exact observed native session.
        if (request.method !== 'POST') return response({ error: 'not_found' }, 404);
        if (!host) return response({ error: 'forbidden' }, 403);
        if (!session) return response({ error: 'not_found' }, 404);
        const gate = this.hostOk(session, caller);
        if (!gate.ok) return response({ error: gate.error }, gate.error === 'not_found' ? 404 : 409);
        const input = await body(request);
        if (!obj(input, ['native']) || !obj(input.native || {}, ['native_id', 'tag', 'workspace', 'native_created_at_ms', 'evidence'])) {
          return response({ error: 'invalid_binding' }, 400);
        }
        return this.transaction(() => {
          // Post-body host authority inside the mutation txn: a host
          // credential revoked or superseded (machine re-enrolled to a new
          // family) while the body streamed must not bind or adopt a family.
          // hostOk re-reads the live machine registry + presenting family; a
          // NULL stamp is never a valid adoption.
          const current: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          const fresh = this.hostOk(current || {}, caller);
          if (!fresh.ok) return response({ error: fresh.error }, fresh.error === 'not_found' ? 404 : 409);
          const applied = this.applyBinding(session.id, input.native, caller.machineId!);
          if (!applied.ok) {
            return response({ error: applied.error },
              applied.error === 'not_found' ? 404 : applied.error === 'invalid_binding' ? 400 : 409);
          }
          this.adoptHostFamily(session.id, caller);
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          return response({ ...this.view(row), generation: applied.generation });
        });
      }
      if (!session || !this.ownerOk(session, caller)) return response({ error: 'not_found' }, 404);
      if (!sub && request.method === 'GET') return response(this.view(session));
      if (sub === 'commands' && request.method === 'GET') {
        const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 50));
        const params: any[] = [session.id];
        const cursorSql = this.cursorClause(url.searchParams.get('cursor'), params, false);
        const rows = this.sql.exec(`SELECT * FROM session_control_commands WHERE session_id = ? ${cursorSql} ORDER BY created_at, id LIMIT ?`, ...params, limit + 1).toArray() as any[];
        const page = rows.slice(0, limit);
        const next = rows.length > limit ? `${page[page.length - 1].created_at}|${page[page.length - 1].id}` : null;
        return response({ session_id: session.id,
          commands: page.map((c: any) => {
            const { claim_token, holder_attempt, owner_token_id, owner_family, ack_json, ...visible } = c;
            const ack = ack_json ? parsed(ack_json, null) : null;
            if (ack) { delete ack.claim_token; delete ack.holder_attempt; }
            return { ...visible, payload: parsed(c.payload_json, null), ack };
          }),
          next_cursor: next });
      }
      if (sub && ['send', 'capture', 'stop'].includes(sub) && request.method === 'POST') {
        if (!writer || !this.ownerOk(session, caller)) return response({ error: 'forbidden' }, 403);
        const kind = sub;
        const input = await body(request);
        // Every mutation below runs inside one synchronous transaction that
        // re-reads the live session row AND the live caller authority after
        // the awaited body: revocation/expiry/rebound/reenroll — or a
        // concurrent stop/delete — during streaming fails closed with no
        // row, no NULL family stamp, and no state-machine violation.
        const freshEnqueue = (payload: any, allowed: Set<string>, err: string): Response => {
          return this.transaction(() => {
            const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
            if (!live || !this.ownerOk(live, caller)) return response({ error: 'not_found' }, 404);
            if (!allowed.has(live.state)) return response({ error: err }, 409);
            return this.enqueue(live, kind, actorName(caller), caller, input.key, payload);
          });
        };
        if (kind === 'send') {
          if (!obj(input, ['key', 'body']) || !label(input.key) ||
              typeof input.body !== 'string' || !input.body.trim() || input.body.length > 16000) {
            return response({ error: 'invalid_send' }, 400);
          }
          return freshEnqueue({ body: input.body }, BOUND_STATES, 'session_not_bound');
        }
        if (kind === 'capture') {
          if (!obj(input, ['key', 'max_bytes']) || !label(input.key) ||
              (input.max_bytes !== undefined && (!Number.isInteger(input.max_bytes) || input.max_bytes < 1 || input.max_bytes > 65536))) {
            return response({ error: 'invalid_capture' }, 400);
          }
          return freshEnqueue({ max_bytes: input.max_bytes ?? 65536 }, BOUND_STATES, 'session_not_bound');
        }
        if (!obj(input, ['key']) || !label(input.key)) return response({ error: 'invalid_stop' }, 400);
        return freshEnqueue({}, STOPPABLE, 'session_not_stoppable');
      }
      return response({ error: 'not_found' }, 404);
    }

    if (path === '/api/session-commands/offers' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      return this.transaction(() => {
        const at = this.at();
        // Expiry sweep: a claim whose lease lapsed is a lost-effect attempt.
        // Uncertain here, never re-offered, never blindly reissued. Only the
        // CURRENT bound family may age its own machine's claims: foreign or
        // superseded families neither read payloads nor mutate transport here.
        for (const stale of this.sql.exec(
          `SELECT c.id, c.claim_expires_at, s.host_family, s.machine_id FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
           WHERE s.machine_id = ? AND c.state = 'claimed' AND c.holder_machine = ?`, caller.machineId!, caller.machineId!).toArray() as any[]) {
          const session = { machine_id: stale.machine_id, host_family: stale.host_family };
          if (!this.hostOk(session, caller).ok) continue;
          const full: any = this.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', stale.id).toArray()[0];
          if (full && this.claimExpired(full)) this.markUncertain(full, 'claim_expired');
        }
        // A fetch receipt without a claim authorizes nothing: stale unclaimed
        // fetches revert to recorded so a lost poll response self-heals. Rows
        // already acked (ack_json set) are never resurrected.
        const cutoff = new Date(this.now() - this.claimTtlMs).toISOString();
        this.sql.exec(
          `UPDATE session_control_commands SET state = 'recorded', poll_at = NULL, updated_at = ?
           WHERE state = 'transported' AND ack_json IS NULL AND holder_machine IS NULL
           AND poll_at IS NOT NULL AND poll_at < ?
           AND session_id IN (SELECT id FROM session_control_sessions WHERE machine_id = ?)`,
          at, cutoff, caller.machineId!);
        const rows = this.sql.exec(
          `SELECT c.* FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
           WHERE s.machine_id = ? AND c.state = 'recorded' ORDER BY c.created_at, c.id LIMIT 20`, caller.machineId!).toArray() as any[];
        const offers = [];
        for (const row of rows) {
          const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
          if (!session || session.machine_id !== caller.machineId) continue;
          // Host-family authorization BEFORE any payload disclosure or state
          // mutation: a foreign or newly re-enrolled family for this machine
          // sees nothing and transports nothing; a deleted machine offers
          // nothing.
          if (!this.hostOk(session, caller).ok) continue;
          const binding = session?.binding_json ? parsed(session.binding_json, null) : null;
          const requestJson = parsed(session.request_json, {});
          // Fetch receipt only: marking transported never authorizes an effect.
          if (row.state === 'recorded') {
            this.sql.exec("UPDATE session_control_commands SET state = 'transported', poll_at = ?, updated_at = ? WHERE id = ? AND state = 'recorded'", at, at, row.id);
          }
          offers.push({
            command_id: row.id, session_id: row.session_id, kind: row.kind,
            generation: row.generation,
            session_state: session.state, fleet_launch_id: session.fleet_launch_id ?? null,
            // Expected session engine for host-side masquerade refusal: the
            // receiver qualifies the live catalog row against it.
            engine: session.engine ?? null,
            // Native send/capture targets need the exact bound tag + incarnation;
            // unbound sessions (stop on offered) carry binding null + fleet correlation.
            // native_created_at_ms is mandatory for host generation provenance:
            // the receiver refuses bound offers without it rather than guessing.
            binding: binding ? { native_id: binding.native_id, tag: binding.tag, workspace: binding.workspace, generation: binding.generation, native_created_at_ms: binding.native_created_at_ms ?? null } : null,
            native_tag: binding?.tag ?? null, workspace: requestJson.workspace ?? null,
            // Reserved for the future start adapter that consumes cwd; the hub
            // rejects non-root cwd today and never claims an executed cwd.
            cwd: requestJson.cwd ?? null,
            payload: parsed(row.payload_json, null), correlation: row.correlation,
          });
          if (offers.length >= 20) break;
        }
        return response({ offers });
      });
    }

    const claimMatch = /^\/api\/session-commands\/([a-f0-9-]{36})\/claim$/.exec(path);
    if (claimMatch && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      // attempt_id is the host's own journaled execution-attempt nonce (one per
      // command per local mutex). It lets the SAME attester reconcile a lost
      // claim reply without ever authorizing a SECOND executor.
      if (!obj(input, ['correlation', 'attempt_id']) || typeof input.correlation !== 'string' || !input.correlation ||
          typeof input.attempt_id !== 'string' || !UUID_RE.test(input.attempt_id)) {
        return response({ error: 'invalid_claim' }, 400);
      }
      return this.transaction(() => {
        const row: any = this.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', claimMatch[1]).toArray()[0];
        if (!row) return response({ error: 'not_found' }, 404);
        const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
        if (!session) return response({ error: 'not_found' }, 404);
        const gate = this.hostOk(session, caller);
        if (!gate.ok) return response({ error: gate.error }, gate.error === 'not_found' ? 404 : 409);
        if (input.correlation !== row.correlation) return response({ error: 'correlation_mismatch' }, 409);
        if (row.state === 'claimed') {
          if (this.claimExpired(row)) {
            this.markUncertain(row, 'claim_expired');
            return response({ error: 'claim_expired' }, 409);
          }
          if (row.holder_machine !== caller.machineId) return response({ error: 'claim_conflict' }, 409);
          // Same attester (journaled attempt) replays; any other attempt on
          // this machine is refused: one claim authorizes exactly one effect.
          // The grant carries holder + generation + both expiry forms so the
          // host receiver can feed verify_server_claim without inferring field
          // names: expires_at (ISO, canonical) and expires_at_ms (int, host clock).
          if (row.holder_attempt === input.attempt_id) {
            return response({ command_id: row.id, claim_token: row.claim_token, holder: row.holder_machine, generation: row.generation, expires_at: row.claim_expires_at, expires_at_ms: Date.parse(row.claim_expires_at), attempt: row.attempts ?? 1, replay: true });
          }
          return response({ error: 'already_claimed' }, 409);
        }
        if (!CLAIMABLE.has(row.state)) return response({ error: row.state === 'uncertain' ? 'command_uncertain' : 'invalid_transition' }, 409);
        const token = crypto.randomUUID();
        const expires = new Date(this.now() + this.claimTtlMs).toISOString();
        const attempt = (row.attempts ?? 0) + 1;
        const written = this.sql.exec(
          "UPDATE session_control_commands SET state = 'claimed', holder_machine = ?, claim_token = ?, claim_expires_at = ?, holder_attempt = ?, attempts = ?, updated_at = ? WHERE id = ? AND state IN ('recorded', 'transported')",
          caller.machineId, token, expires, input.attempt_id, attempt, this.at(), row.id);
        if (written.rowsWritten !== 1) return response({ error: 'claim_conflict' }, 409);
        this.adoptHostFamily(session.id, caller);
        return response({ command_id: row.id, claim_token: token, holder: caller.machineId, generation: row.generation, expires_at: expires, expires_at_ms: Date.parse(expires), attempt });
      });
    }

    const ackMatch = /^\/api\/session-commands\/([a-f0-9-]{36})\/ack$/.exec(path);
    if (ackMatch && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['correlation', 'outcome', 'native', 'reason', 'capture', 'confirmed_exit', 'claim_token', 'proof']) ||
          typeof input.correlation !== 'string' || !ACK_OUTCOMES.has(input.outcome) ||
          typeof input.claim_token !== 'string' || !input.claim_token ||
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
        if (!row) return response({ error: 'not_found' }, 404);
        const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', row.session_id).toArray()[0];
        if (!session || session.machine_id !== caller.machineId) return response({ error: 'not_found' }, 404);
        const gate = this.hostOk(session, caller);
        if (!gate.ok) return response({ error: gate.error }, gate.error === 'not_found' ? 404 : 409);
        // Each native reply envelope confirms at most one command, in every
        // state: a duplicate proof for the SAME command is idempotent success
        // (concurrent or lost-ack re-submit, no new effect), while reuse
        // across commands is replay.
        if (row.kind === 'send' && input.outcome === 'delivered' && input.proof &&
            typeof input.proof.reply_to_native_id === 'string' && input.proof.reply_to_native_id) {
          const seen: any = this.sql.exec('SELECT command_id FROM session_control_proofs WHERE reply_envelope_id = ?',
            input.proof.reply_to_native_id).toArray()[0];
          if (seen) {
            if (seen.command_id === row.id) return response({ command_id: row.id, state: 'acked' });
            return response({ error: 'proof_replayed' }, 409);
          }
        }
        // The execution fence: only the live claim holder may report an outcome.
        // A re-submit identical to the already-recorded ack (lost-ack response)
        // replays success without any new effect; anything else on a
        // non-claimed row keeps the existing terminal errors, except for one
        // narrow path below: late-reply reconciliation on uncertain sends.
        if (row.state !== 'claimed') {
          if (this.sameAck(row, input)) return response({ command_id: row.id, state: row.state });
          if (row.state === 'uncertain') {
            const late = this.lateReplyAck(row, input, caller);
            if (late) return late;
            return response({ error: 'command_uncertain' }, 409);
          }
          return response({ error: 'claim_required' }, 409);
        }
        if (row.holder_machine !== caller.machineId || row.claim_token !== input.claim_token) {
          return response({ error: 'claim_conflict' }, 409);
        }
        if (this.claimExpired(row)) {
          // Post-effect lease expiry: the effect may have happened. A send
          // carrying a valid delivered proof on the SAME live claim reconciles
          // through lateReplyAck (no new effect, no re-claim) regardless of
          // whether the offers sweep has marked the row uncertain yet — the
          // verdict must not depend on sweep ordering. Anything else stays
          // uncertain, never silently reissued or marked delivered.
          if (row.kind === 'send' && input.outcome === 'delivered') {
            const late = this.lateReplyAck(row, input, caller);
            if (late) return late;
          }
          this.markUncertain(row, 'claim_expired_before_ack');
          return response({ error: 'claim_expired' }, 409);
        }
        if (input.correlation !== row.correlation) return response({ error: 'correlation_mismatch' }, 409);
        const binding = session.binding_json ? parsed(session.binding_json, null) : null;
        if (binding) {
          if (input.native.native_id !== binding.native_id || input.native.tag !== binding.tag ||
              input.native.workspace !== binding.workspace) {
            return response({ error: 'binding_mismatch' }, 409);
          }
        }
        const at = this.at();
        if (input.outcome === 'delivered' && row.kind === 'stop' && input.confirmed_exit !== true) {
          return response({ error: 'confirmed_exit_required' }, 409);
        }
        if (input.outcome === 'delivered' && row.kind === 'send') {
          // Host-verified native reply provenance, not host assertion: the
          // host retrieved the native envelope itself and verified the reply
          // references the delivered message. The hub checks shape, exact
          // sender binding, and envelope uniqueness — never crypto identity.
          if (!binding || binding.native_created_at_ms === undefined || binding.native_created_at_ms === null) {
            return response({ error: 'native_proof_unavailable' }, 409);
          }
          const proof = validateSendProof(input.proof, { native_id: binding.native_id, tag: binding.tag,
            workspace: binding.workspace, generation: binding.generation ?? 0,
            native_created_at_ms: binding.native_created_at_ms });
          if (!proof.ok) return response({ error: proof.error }, 409);
          // Proof uniqueness was established at the top of this handler for
          // every state; reaching here means this reply envelope is fresh.
        }
        const ack: any = {
          outcome: input.outcome, native: input.native, claim_attempt: row.attempts ?? 1,
          // The claim token binds this recorded outcome to exactly one
          // authorized attempt, so an identical lost-ack re-submit is
          // recognizable without repeating the native effect.
          claim_token: input.claim_token,
          ...(row.holder_attempt ? { holder_attempt: row.holder_attempt } : {}),
          ...(input.reason !== undefined ? { reason: input.reason } : {}),
          ...(input.capture !== undefined ? { capture: redact(input.capture).slice(0, 65536), capture_truncated: input.capture.length >= 65536 } : {}),
          ...(input.confirmed_exit !== undefined ? { confirmed_exit: input.confirmed_exit === true } : {}),
          ...(input.proof !== undefined ? { proof: input.proof } : {}),
        };
        const state = input.outcome === 'delivered' ? 'acked'
          : input.outcome === 'transported' ? 'transported'
          : input.outcome === 'refused' ? 'refused' : 'uncertain';
        this.sql.exec('UPDATE session_control_commands SET state = ?, ack_json = ?, updated_at = ? WHERE id = ?', state, JSON.stringify(ack), at, row.id);
        if (state === 'acked' && row.kind === 'send' && input.proof) {
          this.sql.exec('INSERT INTO session_control_proofs (reply_envelope_id, command_id, session_id, created_at) VALUES (?, ?, ?, ?)',
            input.proof.reply_to_native_id, row.id, row.session_id, at);
        }
        if (state === 'acked' && row.kind === 'stop') {
          this.sql.exec("UPDATE session_control_sessions SET state = 'stopped', updated_at = ? WHERE id = ?", at, session.id);
          this.sql.exec("UPDATE fleet_launches SET state = 'exited', finished_at = ?, updated_at = ?, verification_status = 'verified' WHERE id = ? AND machine_id = ? AND native_id = ?", at, at, session.fleet_launch_id, session.machine_id, input.native.native_id);
        }
        return response({ command_id: row.id, state });
      });
    }

    return response({ error: 'not_found' }, 404);
  }

  private enqueue(session: any, kind: string, actor: string, caller: Caller, key: string, payload: any) {
    // Post-body ownership inside the mutation txn: a credential revoked,
    // expired, rebound, or re-enrolled while the body streamed must not
    // enqueue or stamp a NULL family. ownerOk re-verifies the live
    // credential/agent incarnation and owning family; admins bypass.
    if (!this.ownerOk(session, caller)) return response({ error: 'not_found' }, 404);
    // True caps: refuse new work without evicting pending work.
    const pending = Number(this.sql.exec(
      "SELECT COUNT(*) AS n FROM session_control_commands WHERE state IN ('recorded', 'transported', 'claimed')").one().n);
    if (pending >= this.maxPending) return response({ error: 'queue_full' }, 429);
    const perSession = Number(this.sql.exec(
      "SELECT COUNT(*) AS n FROM session_control_commands WHERE session_id = ? AND state IN ('recorded', 'transported', 'claimed')", session.id).one().n);
    if (perSession >= this.maxPerSession) return response({ error: 'session_queue_full' }, 429);
    const prior: any = this.sql.exec(
      'SELECT * FROM session_control_commands WHERE session_id = ? AND actor = ? AND request_key = ?', session.id, actor, key).toArray()[0];
    if (prior) {
      if (!this.ownerOk(prior, caller)) return response({ error: 'not_found' }, 404);
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
      'INSERT INTO session_control_commands (id, session_id, kind, actor, request_key, payload_json, state, generation, correlation, owner_token_id, owner_family, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
      id, session.id, kind, actor, key, JSON.stringify(payload), 'recorded', generation, correlation,
      caller.tokenId ?? null, this.presentingFamily(caller), 0, at, at);
    return response({ command_id: id, state: 'recorded' }, 201);
  }
}

function actorName(caller: Caller): string {
  return caller.actor || caller.machineId || 'admin';
}
