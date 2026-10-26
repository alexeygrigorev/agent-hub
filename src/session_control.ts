import { registeredRepoLabel } from './repos';
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
 * expired custody is retried with the same fence and bounded to three leases,
 * and a transport mark alone never authorizes an effect. SEND delivery
 * (`acked`) additionally requires a correlated receiver proof validated by
 * `validateSendProof` — host assertion, PTY bytes, or inbox persistence alone
 * are reported as `recorded` / `transported` / `uncertain`, never as processed.
 * See docs/SESSION-CONTROL.md.
 */
import { recordIntakeSessionLink, IntakeSessionLinkError } from './intake';
import { ResourceManagement } from './resource-management';
import { classifySessionStall, initSessionStalls, nativeObservationFresh, nativeProgress, orphanFailure, stallThreshold } from './session_stall';
import { STOP_DELIVERY_MS, expireUnclaimedStops, refreshObservedHostFamily, visibleSessionPrefixMatches } from './session_stop';
import type { SqlStorage } from '@cloudflare/workers-types';
import { credentialActor } from './agent_registry';
import { redact } from './routing';
import { getScopeBlock } from './intake_scope';
import { taskTier } from './model-routing';
import { engineProvider, evaluateTbmAdmission, normalizeProviderFamily, type TbmPoolRow } from './quota_admission';
import { ensureTbmQuotaSchema } from './quota';
import { ensureTbmControllerSchema, recordSessionOutcomeReward } from './tbm-controller';
import { approvalAuthorityMatches } from './host_enrollment';
import { sha256 } from './auth/crypto';
import { validateHumanCredentialRecord } from './auth/human-credential';
import { isValidMachineId } from './machine-id';
import type { Env } from './env';
import {
  buildLifecycleView,
  classifyKey,
  collectCleanupEligible,
  initLifecycleTables,
  planCleanupDryRun,
  recordDecision,
  recordObservation,
} from './session_lifecycle_registry';

export const SESSION_CONTROL_SCHEMA = `
CREATE TABLE IF NOT EXISTS session_control_sessions (id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, fleet_launch_id TEXT, machine_id TEXT, engine TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, binding_json TEXT, parent_id TEXT, parent_source TEXT, session_type TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
CREATE TABLE IF NOT EXISTS session_control_commands (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL, correlation TEXT NOT NULL, poll_at TEXT, ack_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, actor, request_key));
CREATE TABLE IF NOT EXISTS session_control_cancellations (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_control_protections (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_warning_events (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, until_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS session_warning_events_session ON session_warning_events(session_id, created_at);
CREATE TABLE IF NOT EXISTS session_control_proofs (reply_envelope_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL);
`;

type Caller = { kind?: string; actor?: string; isAdmin?: boolean; machineId?: string; tokenId?: string };
type Auth = { caller?: Caller; scopes?: string[]; error?: Response };
type ReserveHook = (input: any, auth: Auth) => { value: any; status: number };
export type SessionControlOptions = { maxPendingCommands?: number; maxSessionCommands?: number; claimTtlMs?: number; env?: Env };

const LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const UUID_RE = /^[a-f0-9-]{36}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const COMMANDS = new Set(['send', 'capture', 'stop']);
const ACK_OUTCOMES = new Set(['delivered', 'transported', 'refused', 'uncertain', 'failed']);
// Pre-send readiness: a target counts as recently reported only when it has a
// non-empty agent_state AND a fresh observed_at. fleet_sessions.observed_at is
// the last report time; 5 minutes matches host heartbeat staleness (180s)
// with polling slack. Stale or future timestamps warn/refuse honestly.
const RECENT_STATE_MS = 5 * 60 * 1000;
const HEADERS = { 'Cache-Control': 'private, no-store' };
const BOUND_STATES = new Set(['running']);
const STOPPABLE = new Set(['offered', 'starting', 'running', 'uncertain']);
export const CLEANUP_APPLY_ALLOWED_STATES = new Set([...STOPPABLE, 'stopped']);
const BINDABLE = new Set(['offered', 'starting', 'uncertain', 'running']);
const CLAIMABLE = new Set(['recorded', 'transported']);

export async function deriveCleanupSessionKey(operationKey: string, sessionId: string): Promise<string> {
  const digest = await sha256(`cu:${operationKey}:${sessionId}`);
  return `cu-${digest}`;
}

const label = (x: unknown): x is string => typeof x === 'string' && LABEL.test(x);
const response = (value: unknown, status = 200) => Response.json(value, { status, headers: HEADERS });
const obj = (x: any, keys: string[]) => x !== null && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).every(k => keys.includes(k));
const parsed = (value: any, fallback: any = null) => { try { return JSON.parse(value); } catch { return fallback; } };

function cwdOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 256) return false;
  if (value.startsWith('/') || value.includes('\\') || value.includes(':') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value.split('/').every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
}

const WINDOWS_RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
  'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
  'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
]);

function absoluteWorkspaceOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 512) return false;
  if (/[\x00-\x1f\x7f]/.test(value)) return false;
  if (value.startsWith('/')) {
    if (value.includes('\\')) return false;
    const parts = value.split('/');
    if (parts[0] !== '' || parts.length < 2) return false;
    return parts.slice(1).every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
  }
  if (/^[A-Za-z]:[\\/]/.test(value)) {
    if (value.includes('\\\\') || value.includes('//') || value.includes('\\/') || value.includes('/\\')) return false;
    if (value.endsWith('\\') || value.endsWith('/')) return false;
    const tail = value.slice(3);
    if (!tail) return false;
    const parts = tail.split(/[\\/]/);
    for (const seg of parts) {
      if (!seg || seg === '.' || seg === '..' || seg.endsWith(' ') || seg.endsWith('.')) return false;
      if (/[\x00-\x1f\x7f<>"|?*:]/.test(seg)) return false;
      const base = seg.split('.')[0].toUpperCase();
      if (WINDOWS_RESERVED.has(base)) return false;
    }
    return true;
  }
  return false;
}

/** A workspace identifier is either a configured label (legacy, unchanged)
 * or an explicitly allowed absolute POSIX path (bounded canonical form
 * above; exact-match against host-advertised capabilities plus the trusted
 * host settings mapping still required downstream). */
function workspaceOk(value: unknown): value is string {
  return label(value) || absoluteWorkspaceOk(value) || (typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$/.test(value));
}

/** Friendly session names: stored in request_json, shown in list/view, never
 * identity or authority. URL-safe, bounded, exact-match only. */
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const nameOk = (x: unknown): x is string => typeof x === 'string' && NAME_RE.test(x);

export function workspaceBasename(ws: string): string {
  if (typeof ws !== 'string' || !ws) return '';
  const trimmed = ws.replace(/\/+$/, '');
  const idx = trimmed.lastIndexOf('/');
  return idx >= 0 ? trimmed.slice(idx + 1) : trimmed;
}

export function qualifiedNativeAlias(workspace: string, tag: string): string | null {
  if (!workspace || !tag) return null;
  const base = workspaceBasename(workspace);
  if (!base || !tag) return null;
  return `${base}:${tag}`;
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
    private options: SessionControlOptions = {},
  ) {
    this.maxPending = options.maxPendingCommands ?? 2000;
    this.maxPerSession = options.maxSessionCommands ?? 200;
    this.claimTtlMs = options.claimTtlMs ?? 60000;
  }

  init() {
    initSessionStalls(this.sql);
    for (const stmt of SESSION_CONTROL_SCHEMA.split(';')) {
      if (stmt.trim()) this.sql.exec(stmt);
    }
    // Additive migration for tables created by the earlier reserve-only draft.
    const sessionCols = new Set(this.sql.exec('PRAGMA table_info(session_control_sessions)').toArray().map((c: any) => String(c.name)));
    for (const [col, type] of [['owner_token_id', 'TEXT'], ['owner_family', 'TEXT'], ['host_family', 'TEXT'], ['parent_id', 'TEXT'], ['parent_source', 'TEXT'], ['session_type', 'TEXT']] as const) {
      if (!sessionCols.has(col)) this.sql.exec(`ALTER TABLE session_control_sessions ADD COLUMN ${col} ${type}`);
    }
    const commandCols = new Set(this.sql.exec('PRAGMA table_info(session_control_commands)').toArray().map((c: any) => String(c.name)));
    for (const [col, type] of [['owner_token_id', 'TEXT'], ['owner_family', 'TEXT'], ['holder_machine', 'TEXT'], ['claim_token', 'TEXT'], ['claim_expires_at', 'TEXT'], ['holder_attempt', 'TEXT'], ['attempts', 'INTEGER DEFAULT 0'], ['host_status', 'TEXT'], ['host_reason', 'TEXT']] as const) {
      if (!commandCols.has(col)) this.sql.exec(`ALTER TABLE session_control_commands ADD COLUMN ${col} ${type}`);
    }
    // M15c lifecycle: additive observations/decisions tables. Server-owned
    // DDL (single authority: src/session_lifecycle.ts schema string); the
    // host keeps no DDL copy. No ALTERs to existing tables here.
    initLifecycleTables(this.sql);
    try { ensureTbmControllerSchema(this.sql); } catch {}
    // Repair this day's pre-fix starts from canonical requests, once per id.
    // Run each repair atomically; historical manual links are left untouched.
    const hasIntake = this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_tasks'").toArray().length;
    if (hasIntake) {
      const today = this.at().slice(0, 10);
      const sessions = this.sql.exec("SELECT * FROM session_control_sessions WHERE created_at >= ? AND json_extract(request_json, '$.intake_task_id') IS NOT NULL ORDER BY created_at,id", today).toArray();
      for (const row of sessions) {
        try { this.transaction(() => recordIntakeSessionLink(this.sql, row, this.now())); }
        catch { console.warn('intake_session_link_backfill_pending'); }
      }
    }
  }

  private at() { return new Date(this.now()).toISOString(); }

  /** TBM admission over tbm_quota_pools; any read failure fails closed. Uses per-pool cycle_ms/window_type from ingestion. */
  private tbmGate(engine: string | undefined) {
    try {
      ensureTbmControllerSchema(this.sql);
      const pools = this.sql.exec('SELECT * FROM tbm_quota_pools').toArray() as unknown as TbmPoolRow[];
      const provider = engine ? engineProvider(engine) : null;
      const accounts = [...new Set(pools.filter(p => provider !== null &&
        normalizeProviderFamily(p.provider) === normalizeProviderFamily(provider)).map(p => p.account ?? 'default'))];
      const results = accounts.map(account => evaluateTbmAdmission({ pools, provider, account, nowMs: this.now() }));
      return results.find(result => result.ok) ?? results[0] ?? evaluateTbmAdmission({ pools, provider, nowMs: this.now() });
    } catch {
      return { ok: false as const, reason: 'malformed_pool' as const, pool_id: null, detail: 'tbm_read_failed' };
    }
  }

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

  private cancelOwnerLive(row: any, caller: Caller, operatorStop = false): boolean {
    // Explicit human operators already may stop any bound session. Apply the
    // same authority to a provably unstarted intent, without broadening the
    // owner-only /cancel endpoint or trusting a collapsed browser actor.
    if (operatorStop && caller.isAdmin === true && caller.kind === 'human') {
      if (typeof caller.actor === 'string' && caller.actor.startsWith('human:admin-token:')) return true;
      if (caller.tokenId) {
        const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
        if (validateHumanCredentialRecord(this.sql, this.options.env, cred, this.now())?.actor === caller.actor) return true;
      }
    }
    if (row.actor !== actorName(caller) || !this.ownerOk(row, caller)) return false;
    // Legacy admin bearers have a stable actor but no credentials-table row.
    // Exact ownership still applies; browser's shared actor=admin cannot cancel.
    if (caller.isAdmin && caller.kind === 'human' && typeof caller.actor === 'string' && caller.actor.startsWith('human:admin-token:')) return true;
    if (caller.kind === 'human') {
      // Browser bus auth currently collapses users to actor=admin and cannot
      // establish which human owns a legacy intent. Only a current, explicitly
      // bound human bearer can authorize this new no-start cancellation path.
      if (!caller.tokenId) return false;
      const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
      return validateHumanCredentialRecord(this.sql, this.options.env, cred, this.now())?.actor === caller.actor;
    }
    if (caller.kind !== 'agent' || !caller.tokenId) return false;
    const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
    const scopes = parsed(cred?.scopes, null);
    return cred?.kind === 'agent' && !cred.revoked_at && Date.parse(cred.expires_at) > this.now() &&
      Array.isArray(scopes) && scopes.includes('fleet:launch') && credentialActor(this.sql, cred) === caller.actor;
  }

  /** Cleanup authority never inherits the general admin bypass. A current
   * credential family and immutable creator provenance identify custody. */
  private cleanupCredential(cred: any): { cred: any; actor: string; family: string; sub?: string; email?: string } | null {
    if (!cred || typeof cred.family_id !== 'string' || !cred.family_id) return null;
    if (cred.kind === 'human') {
      const human = validateHumanCredentialRecord(this.sql, this.options.env, cred, this.now());
      return human ? { cred, actor: human.actor, family: cred.family_id, sub: human.sub, email: human.email } : null;
    }
    if (cred.kind !== 'agent' || cred.revoked_at || !(Date.parse(cred.expires_at) > this.now())) return null;
    const scopes = parsed(cred.scopes, null);
    const actor = credentialActor(this.sql, cred);
    return actor && Array.isArray(scopes) && scopes.includes('fleet:launch')
      ? { cred, actor, family: cred.family_id } : null;
  }

  private cleanupOwnerCredential(session: any, live: NonNullable<ReturnType<SessionControlService['cleanupCredential']>>): boolean {
    if (!session || session.actor !== live.actor) return false;
    if (live.cred.kind === 'agent') {
      return session.owner_family ? session.owner_family === live.family : session.owner_token_id === live.cred.id;
    }
    if (!session.owner_token_id) return false;
    const creator: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', session.owner_token_id).toArray()[0];
    // The original token may be revoked by ordinary refresh. Its immutable
    // provenance remains evidence; the CURRENT family member is validated
    // independently above. Missing or contradictory provenance fails closed.
    return !!creator && creator.kind === 'human' && creator.actor === session.actor &&
      creator.created_by_sub === live.sub && typeof creator.subject === 'string' &&
      creator.subject.toLowerCase() === live.email?.toLowerCase() &&
      creator.family_id === live.family && (!session.owner_family || session.owner_family === live.family);
  }

  private cleanupOwnerOk(session: any, caller: Caller): boolean {
    if (!caller.tokenId || !caller.actor || session?.actor !== caller.actor) return false;
    const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
    const live = this.cleanupCredential(cred);
    return !!live && live.actor === caller.actor && live.cred.kind === caller.kind && this.cleanupOwnerCredential(session, live);
  }

  private cleanupCallerLive(caller: Caller): boolean {
    if (!caller.tokenId) return false;
    const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', caller.tokenId).toArray()[0];
    const live = this.cleanupCredential(cred);
    return !!live && live.actor === caller.actor && cred.kind === caller.kind;
  }

  private cleanupCommandOwnerLive(row: any, session: any): boolean {
    if (!row.owner_family || row.actor !== session?.actor) return false;
    for (const cred of this.sql.exec('SELECT * FROM credentials WHERE family_id = ?', row.owner_family).toArray() as any[]) {
      const live = this.cleanupCredential(cred);
      if (live && live.actor === row.actor && this.cleanupOwnerCredential(session, live)) return true;
    }
    return false;
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
      if (!m.credential_id || !credential) return { ok: false, error: 'host_credential_changed' };
      const isBoundOwner = m.credential_id === caller.tokenId;
      if (!isBoundOwner && !approvalAuthorityMatches(credential.approval_seq, m.approval_seq)) return { ok: false, error: 'host_credential_changed' };
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

  /** M15d coordination contract for the parallel status worker (narrow).
   *
   * The status worker injects this via Hub into its status observation
   * service to gate host observation writes; it owns src/hub.ts and must
   * still validate host scope + canonical task/session machine itself.
   * Uses the existing fresh host family/approval authority (no duplicate
   * checker, no new privilege). Cleanup logic never trusts JSON ownership
   * from host observations — this gate only authorizes transport. */
  hostObservationAllowed(machineId: unknown, caller: Caller): boolean {
    if (caller.kind !== 'host') return false;
    return this.hostOk({ machine_id: machineId as string }, caller).ok;
  }

  /** M15c: live host authority for machine-scoped observation writes (no
   * session row involved): the machine row must still exist and the
   * presenter must be in the machine's current credential family. */
  private lifecycleHostOk(caller: Caller): boolean {
    if (!caller.machineId || !caller.tokenId) return false;
    const family = this.presentingFamily(caller);
    if (!family) return false;
    try {
      const m: any = this.sql.exec(
        'SELECT m.id, m.credential_id, c.family_id AS machine_family FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?',
        caller.machineId).toArray()[0];
      if (!m) return false;
      if (m.machine_family && m.machine_family !== family) return false;
      return true;
    } catch {
      return false;
    }
  }

  /** M15c: resolve the canonical refs for a lifecycle session key without
   * trusting any payload field. Returns null for unknown keys. */
  private lifecycleRefsFor(sessionKey: unknown): { session_id: string | null; fleet_launch_id: string | null } | null {
    if (typeof sessionKey !== 'string') return null;
    const sep = sessionKey.indexOf('/');
    if (sep < 0) return null;
    const machine_id = sessionKey.slice(0, sep);
    const native_id = sessionKey.slice(sep + 1);
    if (!machine_id || !native_id) return null;
    try {
      for (const sc of this.sql.exec('SELECT id, binding_json, fleet_launch_id FROM session_control_sessions WHERE machine_id = ?', machine_id).toArray() as any[]) {
        try {
          const binding = sc.binding_json ? JSON.parse(sc.binding_json) : null;
          if (binding?.native_id === native_id) {
            return { session_id: sc.id, fleet_launch_id: sc.fleet_launch_id ?? null };
          }
        } catch { /* corrupt binding: not this session */ }
      }
      const launch: any = this.sql.exec(
        'SELECT id FROM fleet_launches WHERE machine_id = ? AND native_id = ?', machine_id, native_id).toArray()[0];
      if (launch) return { session_id: null, fleet_launch_id: launch.id };
      const fleet: any = this.sql.exec(
        'SELECT machine_id FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', machine_id, native_id).toArray()[0];
      if (fleet) return { session_id: null, fleet_launch_id: null };
    } catch {
      return null;
    }
    return null;
  }

  /** Server-owned cleanup-tree marker: true ONLY when the stored command
   * payload carries the explicit cleanup flag written by the cleanup --apply
   * route inside the mutation txn. Ordinary stop/send/capture routes always
   * enqueue fixed payloads without this flag, so they can never set it. The
   * request_key prefix is caller-controlled and is NEVER read here. */
  private cleanupStored(row: any): boolean {
    if (!row || row.kind !== 'stop') return false;
    const payload = row.payload_json ? parsed(row.payload_json, null) : null;
    return !!payload && payload.cleanup_tree === true;
  }

  /** Re-evaluate M15 cleanup eligibility at claim time (before any native
   * effect): stored flag + same owner (session.actor === command.actor) +
   * live binding incarnation + still finished/superseded with zero obligations
   * and not human-owned. Anything else yields false (stop still claimable,
   * tree never authorized). No JSON owner proof is ever trusted: ownership is
   * the live session/command rows plus the M15 canonical joins. */
  private cleanupAuthorized(row: any, session: any): boolean {
    if (!this.cleanupStored(row)) return false;
    if (!session || session.actor !== row.actor) return false;
    if (!this.cleanupCommandOwnerLive(row, session)) return false;
    const binding = session.binding_json ? parsed(session.binding_json, null) : null;
    if (!binding || typeof binding.native_id !== 'string' || !binding.native_id) return false;
    if (!session.machine_id) return false;
    try {
      const live = classifyKey(this.sql, `${session.machine_id}/${binding.native_id}`, this.now());
      if (!live) return false;
      if (live.refs?.protected_session) return false;
      if (live.group !== 'finished_stale') return false;
      if (!['finished', 'superseded'].includes((live as any).state)) return false;
      if (((live as any).obligations?.length ?? 0) > 0) return false;
      if (live.refs?.owner !== row.actor) return false;
      return true;
    } catch {
      return false;
    }
  }

  private warningView(row: any, threshold = 10800) {
    const binding = parsed(row.binding_json, {});
    const intent = parsed(row.request_json, {});
    let native: any = null;
    if (row.machine_id && binding?.native_id) {
      native = this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', row.machine_id, binding.native_id).toArray()[0];
    }
    const now = this.now();
    const started = binding?.native_created_at_ms ?? native?.created_at_ms ?? Date.parse(row.created_at);
    const outputAt = nativeProgress(native) ?? NaN;
    const lastActivity = Number.isFinite(outputAt) ? Math.max(started, outputAt) : started;
    // Imported native sessions can be linked after launch through intake.
    let intakeId = intent.intake_task_id ?? null;
    if (!intakeId) {
      const linked: any = this.sql.exec(`SELECT i.id FROM intake_tasks i, json_each(i.data, '$.history') e
        WHERE json_extract(e.value, '$.kind') = 'link' AND
        (json_extract(e.value, '$.hub_session_id') = ? OR
         (json_extract(e.value, '$.native_id') = ? AND json_extract(e.value, '$.routing_job_id') IS NOT NULL)) LIMIT 1`, row.id, binding?.native_id ?? '').toArray()[0];
      intakeId = linked?.id ?? null;
    }
    const runtime = Math.max(0, (now - started) / 1000);
    const idleFor = Math.max(0, (now - lastActivity) / 1000);
    const snooze: any = this.sql.exec('SELECT * FROM session_warning_events WHERE session_id = ? ORDER BY rowid DESC LIMIT 1', row.id).toArray()[0];
    const longRunning = row.state === 'running' && runtime > threshold;
    return {
      runtime, idle_for: idleFor, last_activity_at: new Date(lastActivity).toISOString(),
      long_running: longRunning, activity_state: idleFor > stallThreshold(this.options.env) ? 'idle' : 'active',
      warning_threshold: threshold, warning_snoozed_until: snooze?.until_at ?? null,
      warning_active: longRunning && !(snooze && Date.parse(snooze.until_at) > now),
      intake_id: intakeId,
      snooze_command: `hub sessions ack ${row.id} --snooze ${threshold}s`,
    };
  }

  private view(row: any, threshold: number | null = null) {
    const request = parsed(row.request_json, {});
    const protection: any = this.sql.exec('SELECT request_key, created_at FROM session_control_protections WHERE session_id = ?', row.id).toArray()[0];
    const binding = row.binding_json ? parsed(row.binding_json, null) : null;
    const observed: any = binding?.native_id && row.machine_id
      ? this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', row.machine_id, binding.native_id).toArray()[0] : null;
    const same = observed && observed.tag === binding?.tag && observed.workspace === binding?.workspace &&
      (!observed.created_at_ms || !binding?.native_created_at_ms || observed.created_at_ms === binding.native_created_at_ms);
    const native = same ? observed : binding?.native_id && row.machine_id ? {
      machine_id: row.machine_id, native_id: binding.native_id, phase: 'unobserved',
      created_at_ms: binding.native_created_at_ms ?? Date.parse(row.created_at), observed_at: row.updated_at,
    } : null;
    const stall = classifySessionStall(this.sql, native, this.now(), stallThreshold(this.options.env), nativeObservationFresh(this.sql, native, this.now()));
    const isObserved = (row.actor === 'unknown' || request.provenance === 'observed_native') && !row.fleet_launch_id;
    const provenance = isObserved ? 'observed_native' : 'canonical_hub';
    const controllability = isObserved
      ? (['running', 'active', 'starting'].includes(row.state) ? 'controllable' : 'historical_read_only')
      : row.state === 'cancelled' ? 'historical_read_only' : 'controllable';
    const workspace = binding?.workspace || request?.workspace || null;
    const tag = binding?.tag || request?.tag || null;
    const qualified_alias = workspace && tag ? qualifiedNativeAlias(workspace, tag) : null;
    const session_type = row.session_type || (row.mode === 'interactive' || row.interactive ? 'aplexer' : (isObserved ? 'aplexer' : 'headless'));
    const parent_id = row.parent_id || null;
    const parent_source = row.parent_source || (row.parent_id ? 'explicit_parent' : 'unknown_external');
    let launchError: any = null;
    let modelSelection: any = null;
    let expectedCpu: any = null;
    let modelReason: string | null = null;
    let memoryRejected: any[] = [];
    if (row.fleet_launch_id) {
      try {
        const launch: any = this.sql.exec('SELECT launch_error_json, model_selection_json, exit_reason, native_id,request_json,workspace FROM fleet_launches WHERE id = ?', row.fleet_launch_id).toArray()[0];
        modelSelection = parsed(launch?.model_selection_json);
        const cpuTable=this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='resource_cpu_starts'").toArray().length;
        if(cpuTable) {
          const intent=parsed(launch?.request_json);
          expectedCpu=this.sql.exec('SELECT expected_cpu_cores,expected_cpu_source FROM resource_cpu_starts WHERE launch_id=?',row.fleet_launch_id).toArray()[0]
            ?? new ResourceManagement(this.sql,this.now).cpuEstimate(intent.project || launch?.workspace || request.workspace || null,intent.task_kind || request.task_kind || 'goal');
        }
        modelReason = launch?.exit_reason === 'model_unsupported' ? 'model_unsupported' : null;
        launchError = launch?.launch_error_json ? parsed(launch.launch_error_json, null) : null;
        // Rejections before claim and legacy terminal receipts have no diagnostic
        // DTO. Preserve their bounded reason without publishing arbitrary logs.
        if (!launchError && row.state === 'failed') {
          const started = !!binding?.native_id || !!launch?.native_id;
          const reason = typeof launch?.exit_reason === 'string' && /^[a-z][a-z_]{0,63}$/.test(launch.exit_reason)
            ? launch.exit_reason : started ? 'engine_exited_without_diagnostic' : 'launcher_failed_without_diagnostic';
          const messages: Record<string, string> = {
            engine_exited_without_diagnostic: 'The engine started and later exited; the legacy receipt did not preserve its runtime cause.',
            admission_unconfigured: 'The host has no fresh admission adapter configured for this engine. Routing is held until it is configured and probed.',
            admission_refused: 'The host fresh admission gate refused this launch.',
            workspace_unavailable: 'The requested workspace is unavailable on this host.',
            model_unsupported: 'The requested model is unsupported by the configured host adapter.',
            dispatch_refused: 'The host refused the dispatch before native start.',
            launcher_not_started: 'The maintained launcher did not start a native session.',
          };
          launchError = { reason, message: messages[reason] || 'The launch failed without a structured diagnostic; inspect the host launcher receipt.', stage: started ? 'runtime' : 'dispatch_route' };
        }
        if (row.state === 'queued' || row.state === 'held') {
          const hold: any = this.sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id = ?', row.fleet_launch_id).toArray()[0];
          memoryRejected = hold ? parsed(hold.rejected_json, []) : [];
        }
      } catch {} // Fleet is optional in isolated session-control installations.
    }
    const qualified_name = qualified_alias || (typeof request.name === 'string' ? request.name : row.id);
    return {
      ...(threshold === null ? {} : this.warningView(row, threshold)),
      id: row.id, state: row.mode === 'headless' && launchError?.reason === 'engine_tool_permission_refused' ? 'failed' :
        row.mode === 'headless' && row.state === 'stopped' && orphanFailure(native) ? 'failed' :
        row.mode === 'headless' && row.state === 'running' && ['exited', 'dead'].includes(native?.phase) ? 'failed' :
        row.state === 'running' && stall.stalled ? 'stalled' : row.state,
      stalled: row.state === 'running' && stall.stalled,
      stall_reason: orphanFailure(native) ? 'agent_exited_children_running' : stall.reason, stalled_since: stall.since,
      stall_threshold: stallThreshold(this.options.env), lifecycle_state: row.state,
      ...(row.mode === 'headless' && launchError?.reason === 'engine_tool_permission_refused' ? { cleanup_pending: !['failed', 'stopped'].includes(row.state) } : {}),
      machine: row.machine_id ?? null, engine: row.engine ?? null,
      model_id: modelSelection?.id ?? null, model: modelSelection?.model ?? null,
      expected_cpu_cores:expectedCpu?.expected_cpu_cores ?? memoryRejected.find(r=>r.cpu)?.cpu?.expected_cpu_cores ?? null,
      expected_cpu_source:expectedCpu?.expected_cpu_source ?? memoryRejected.find(r=>r.cpu)?.cpu?.expected_cpu_source ?? null,
      mode: row.mode, access: request.access?.mode || 'legacy_unspecified',
      // Friendly name from the stored start intent (null when unnamed). The
      // name is display only: control routes stay Hub-UUID addressed, with an
      // actor-scoped exact-name alias where explicitly resolved below.
      name: typeof request.name === 'string' ? request.name : null,
      fleet_launch_id: row.fleet_launch_id ?? null,
      ...(modelReason ? { reason: modelReason } : {}),
      ...(memoryRejected.length ? { reason: memoryRejected[0]?.reason ?? 'placement_unknown', rejected: memoryRejected } : {}),
      ...(launchError ? { reason: launchError.reason, message: launchError.message, launch_error: launchError } : {}),
      cleanup_protection: protection ? { key: protection.request_key, created_at: protection.created_at, permanent: true } : null,
      binding,
      provenance,
      controllability,
      workspace,
      qualified_alias,
      qualified_name,
      session_type,
      parent_id,
      parent_source,
      created_at: row.created_at, updated_at: row.updated_at,
    };
  }

  /** Actor-scoped exact name alias (read/control convenience only).
   *
   * Names are not identity: resolution matches the stored start-intent name
   * exactly within the caller's own actor scope (admins resolve within their
   * own actor too, never across owners), and never matches native tags.
   * Zero matches render as not_found (never an oracle); more than one
   * renders as ambiguous_name (409, refused) — the caller must use the Hub
   * UUID. The exact Hub UUID stays the canonical control address. */
  private resolveSessionByName(name: string, caller: Caller): { ok: true; session: any } | { ok: false; error: string; status: number } {
    if (!nameOk(name)) return { ok: false, error: 'not_found', status: 404 };
    const scope = caller.actor || caller.machineId || 'admin';
    let rows: any[];
    try {
      rows = caller.isAdmin === true
        ? this.sql.exec('SELECT * FROM session_control_sessions').toArray() as any[]
        : this.sql.exec('SELECT * FROM session_control_sessions WHERE actor = ?', scope).toArray() as any[];
    } catch {
      return { ok: false, error: 'not_found', status: 404 };
    }
    const matches = rows.filter(r => {
      if (!this.ownerOk(r, caller)) return false;
      const request = parsed(r.request_json, {});
      return request.name === name;
    });
    if (matches.length === 0) return { ok: false, error: 'not_found', status: 404 };
    if (matches.length > 1) return { ok: false, error: 'ambiguous_name', status: 409 };
    return { ok: true, session: matches[0] };
  }

  /** Qualified native alias (<workspace_basename>:<tag>).
   *
   * Exact match only; collisions / reused tags are refused honestly with
   * 409 ambiguous_name (no latest-wins). Admins can resolve observed native
   * sessions; foreign agent callers cannot resolve unowned sessions (fail closed
   * with 404).
   */
  private resolveSessionByQualifiedAlias(alias: string, caller: Caller): { ok: true; session: any } | { ok: false; error: string; status: number } {
    const colon = alias.indexOf(':');
    if (colon <= 0 || colon === alias.length - 1) return { ok: false, error: 'not_found', status: 404 };
    const base = alias.slice(0, colon);
    const tag = alias.slice(colon + 1);

    let rows: any[];
    try {
      rows = (this.sql.exec('SELECT * FROM session_control_sessions').toArray() as any[]).filter(r => this.ownerOk(r, caller));
    } catch {
      return { ok: false, error: 'not_found', status: 404 };
    }

    const matches = rows.filter(r => {
      const binding = r.binding_json ? parsed(r.binding_json, null) : null;
      const request = parsed(r.request_json, {});
      const ws = binding?.workspace || request?.workspace;
      const t = binding?.tag || request?.tag;
      if (!ws || !t) return false;
      const b = workspaceBasename(ws);
      return b === base && t === tag;
    });

    if (matches.length === 0) return { ok: false, error: 'not_found', status: 404 };
    if (matches.length > 1) return { ok: false, error: 'ambiguous_name', status: 409 };
    return { ok: true, session: matches[0] };
  }

  /** Adopt a Fleet-side promotion of a capacity-queued intent (no new placement).
   *
   * When the linked fleet launch advances, the session follows its assignment
   * even if no client observed offered. Native running/terminal transitions
   * stay with the binding/result flows, which carry the incarnation evidence
   * this sync must never invent. */
  private syncFleetPlacement(sessionId: string): any | null {
    reconcileUnboundFleetSessions(this.sql, this.now());
    const session: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0];
    if (!session || !session.fleet_launch_id) return session;
    if (session.state !== 'queued' && session.state !== 'held') return session;
    let launch: any = null;
    try { launch = this.sql.exec('SELECT id, machine_id, engine, state FROM fleet_launches WHERE id = ?', session.fleet_launch_id).toArray()[0]; } catch { return session; }
    if (!launch || !['offered', 'starting', 'running', 'uncertain'].includes(launch.state)) return session;
    // Fleet may claim and start between session polls. Assignment is safe to
    // adopt in every live launch state, but running still requires the exact
    // native binding flow; never infer execution solely from this row.
    const assignedState = launch.state === 'running' ? 'starting' : launch.state;
    try {
      this.sql.exec('UPDATE session_control_sessions SET machine_id = ?, engine = ?, state = ?, updated_at = ? WHERE id = ? AND state = ?',
        launch.machine_id ?? null, launch.engine ?? null, assignedState, this.at(), sessionId, session.state);
    } catch { return session; }
    return this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', sessionId).toArray()[0] ?? session;
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
    // tag must be the canonical Fleet consumer tag for that launch:
    // 'hub-<fleet_launch_id>' (interactive dispatcher argv) or the exact
    // 'task-hub-<fleet_launch_id>' (maintained launcher aplexer backend
    // run_tag_for('hub-<id>') for canonical headless opencode). No fuzzy
    // matching: any other tag is refused, and the exact tag plus the
    // launch native UUID/result, machine, engine, workspace, and incarnation
    // checks below still bind the exact native row (never tag-only).
    // Unstarted (offered), failed/exited/uncertain, foreign-machine, or inconsistent
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
    if (binding.tag !== 'hub-' + launch.id && binding.tag !== 'task-hub-' + launch.id) return { ok: false as const, error: 'fleet_tag_mismatch' };
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
    if (!row.claim_expires_at) return true;
    const at = Date.parse(row.claim_expires_at);
    return !Number.isFinite(at) || at <= this.now();
  }

  /** Retry custody of the SAME journaled effect, never authorize a new executor.
   * Retain token/attempt so a restarted host can only reconcile its original
   * journal. Missing journals on a different attester fail closed.
   */
  private expireClaim(row: any) {
    const storedInInbox = row.host_status === 'waiting in inbox for recipient';
    if (storedInInbox) {
      this.sql.exec("UPDATE session_control_commands SET state = 'failed', ack_json = ?, poll_at = NULL, updated_at = ? WHERE id = ? AND state IN ('claimed','recorded','transported')",
        JSON.stringify({ outcome: 'failed', reason: 'recipient_not_ready' }), this.at(), row.id);
      return;
    }
    const attempts = row.attempts ?? 1;
    const exhausted = attempts >= 3;
    this.sql.exec("UPDATE session_control_commands SET state = ?, ack_json = ?, poll_at = NULL, attempts = ?, claim_expires_at = ?, updated_at = ? WHERE id = ? AND state IN ('claimed','recorded','transported')",
      exhausted ? 'failed' : 'recorded',
      exhausted ? JSON.stringify({ outcome: 'failed', reason: 'claim_expired_retry_exhausted' }) : null,
      exhausted ? attempts : attempts + 1, new Date(this.now() + this.claimTtlMs).toISOString(), this.at(), row.id);
  }

  private sweepSessionClaims(sessionId: string) {
    expireUnclaimedStops(this.sql, this.now(), sessionId);
    const rows = this.sql.exec("SELECT * FROM session_control_commands WHERE session_id = ? AND state IN ('claimed','recorded','transported') AND claim_token IS NOT NULL AND ack_json IS NULL AND (claim_expires_at IS NULL OR claim_expires_at <= ?) LIMIT 100",
      sessionId, this.at()).toArray() as any[];
    for (const row of rows) this.expireClaim(row);
  }

  private markUncertain(row: any, reason: string) {
    this.sql.exec("UPDATE session_control_commands SET state = 'uncertain', ack_json = ?, updated_at = ? WHERE id = ?",
      JSON.stringify({ outcome: 'uncertain', reason }), this.at(), row.id);
  }

  /** Late-reply reconciliation for an uncertain SEND (the single post-terminal
   * transition), also accepted during same-fence retry custody. Real recipients
   * ordinarily reply after the execution
   * lease lapses; discarding those replies would strand normal delayed
   * delivery. This path records the proven outcome WITHOUT any new effect
   * and WITHOUT re-opening claiming (claim on uncertain still 409s):
   *
   * - same command, same attempt (claim token + correlation match the fence),
   *   same executor machine, fresh live host authority;
   * - outcome `delivered` with a valid, still-unused reply proof against the
   *   CURRENT binding (stale generations fail closed as usual).
   *
   * Capture/stop and proof-less outcomes cannot use this late proof path. Returns the success response, or
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

    const thresholdRaw = url.searchParams.get('threshold');
    const threshold = thresholdRaw === null ? 10800 : Number(thresholdRaw);
    if (!Number.isSafeInteger(threshold) || threshold <= 0 || threshold > 31536000) return response({ error: 'invalid_threshold' }, 400);
    const admin = caller.isAdmin === true;
    const writer = admin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch'));
    const host = caller.kind === 'host' && !!caller.machineId && auth.scopes?.includes('host');

    if (path === '/api/sessions/host-bindings' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);

      if (url.searchParams.get('kind') === 'imported') {
        const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 100));
        const cursor = url.searchParams.get('cursor');
        const params: any[] = [caller.machineId!];
        const cursorSql = this.cursorClause(cursor, params, false);
        const rawRows = this.sql.exec(
          `SELECT * FROM session_control_sessions WHERE machine_id = ? AND fleet_launch_id IS NULL AND binding_json IS NOT NULL AND state IN ('running', 'starting') ${cursorSql} ORDER BY created_at, id LIMIT ?`,
          ...params, limit + 1
        ).toArray() as any[];

        const hasMore = rawRows.length > limit;
        const rawPage = hasMore ? rawRows.slice(0, limit) : rawRows;
        const nextCursor = hasMore ? `${rawPage[rawPage.length - 1].created_at}|${rawPage[rawPage.length - 1].id}` : null;

        const imported: any[] = [];
        for (const row of rawPage) {
          if (!this.hostOk(row, caller).ok) continue;
          const b = parsed(row.binding_json, null);
          if (!b || typeof b !== 'object') continue;
          if (b.provenance !== 'observed_native') continue;
          if (typeof b.native_id !== 'string' || !UUID_RE.test(b.native_id) || b.native_id !== row.id) continue;
          if (typeof b.tag !== 'string' || !b.tag) continue;
          if (typeof b.workspace !== 'string' || !b.workspace) continue;
          if (typeof b.generation !== 'number' || !Number.isInteger(b.generation) || b.generation <= 0) continue;
          if (typeof b.native_created_at_ms !== 'number' || !Number.isInteger(b.native_created_at_ms) || b.native_created_at_ms <= 0) continue;
          const engine = row.engine || b.engine;
          if (typeof engine !== 'string' || !engine) continue;

          imported.push({
            id: row.id,
            engine: engine,
            state: row.state,
            session_type: row.session_type,
            machine: row.machine_id,
            mode: row.mode,
            binding: {
              native_id: b.native_id,
              tag: b.tag,
              workspace: b.workspace,
              generation: b.generation,
              native_created_at_ms: b.native_created_at_ms,
              provenance: b.provenance,
              adopted_at: b.adopted_at ?? row.created_at,
            },
          });
        }

        return response({ sessions: imported, next_cursor: nextCursor });
      }

      // Default GET /api/sessions/host-bindings: pending Fleet starts only
      // Reconcile queued assignments here as well as in client polling. A
      // headless start must bind even when no client observed the brief
      // offered state before the host claimed it.
      const promoted = this.sql.exec("SELECT s.id, s.host_family, f.machine_id FROM session_control_sessions s JOIN fleet_launches f ON f.id = s.fleet_launch_id WHERE f.machine_id = ? AND s.binding_json IS NULL AND s.state IN ('queued','held') AND f.state IN ('starting','running') LIMIT 100", caller.machineId!).toArray() as any[];
      for (const row of promoted) if (this.hostOk(row, caller).ok) this.syncFleetPlacement(row.id);
      const rows = this.sql.exec("SELECT s.*, f.native_id AS launch_native_id FROM session_control_sessions s JOIN fleet_launches f ON f.id = s.fleet_launch_id WHERE s.machine_id = ? AND s.binding_json IS NULL AND s.state IN ('offered','starting','uncertain') AND f.state IN ('starting','running') LIMIT 100", caller.machineId!).toArray() as any[];
      return response({
        sessions: rows.filter(row => this.hostOk(row, caller).ok).map(row => ({ id: row.id, fleet_launch_id: row.fleet_launch_id, engine: row.engine, native_id: row.launch_native_id ?? null })),
      });
    }

    if (path === '/api/sessions' && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      // CLI historically used --intake-task while the API field is intake_task_id.
      // Accept the short alias when it does not conflict, so MCP/CLI parity does
      // not surface as a spurious invalid_session_request for otherwise valid starts.
      if (typeof input.intake_task === 'string') {
        const m = /\/intake\/([A-Za-z0-9._:-]{6,128})/.exec(input.intake_task);
        if (m) input.intake_task = m[1];
      }
      if (typeof input.intake_task_id === 'string') {
        const m = /\/intake\/([A-Za-z0-9._:-]{6,128})/.exec(input.intake_task_id);
        if (m) input.intake_task_id = m[1];
      }
      if (Object.prototype.hasOwnProperty.call(input, 'intake_task')) {
        if (input.intake_task_id !== undefined && input.intake_task_id !== input.intake_task) {
          return response({ error: 'invalid_session_request', field: 'intake_task', detail: 'conflicts_with_intake_task_id' }, 400);
        }
        input.intake_task_id = input.intake_task;
        delete input.intake_task;
      }
      // null / blank intake_task_id means "missing" so the TBM gate can return
      // intake_task_id_required instead of a generic invalid_session_request.
      if (input.intake_task_id === null || input.intake_task_id === '') delete input.intake_task_id;
      if (!obj(input, ['key', 'engine', 'engine_override_reason', 'tier', 'machine', 'workspace', 'cwd', 'brief', 'access', 'mode', 'name', 'parent_id', 'intake_task_id', 'with_goal', 'task_kind'])) {
        const allowed = new Set(['key', 'engine', 'engine_override_reason', 'tier', 'machine', 'workspace', 'cwd', 'brief', 'access', 'mode', 'name', 'parent_id', 'intake_task_id', 'with_goal', 'task_kind']);
        const field = input && typeof input === 'object' && !Array.isArray(input)
          ? Object.keys(input).find(k => !allowed.has(k)) : undefined;
        return response({ error: 'invalid_session_request', ...(field ? { field, detail: 'unknown_field' } : { detail: 'invalid_object' }) }, 400);
      }
      if (input.task_kind !== undefined && !['implement','review','ask','goal'].includes(input.task_kind)) return response({error:'invalid_session_request',field:'task_kind',detail:'invalid_task_kind'},400);
      if (!label(input.key)) return response({ error: 'invalid_session_request', field: 'key', detail: 'invalid_key' }, 400);
      if (!workspaceOk(input.workspace) && !registeredRepoLabel(this.sql, input.workspace)) return response({ error: 'invalid_session_request', field: 'workspace', detail: 'invalid_workspace' }, 400);
      if (input.engine !== undefined && !label(input.engine)) return response({ error: 'invalid_session_request', field: 'engine', detail: 'invalid_engine' }, 400);
      if (input.engine_override_reason !== undefined && (typeof input.engine_override_reason !== 'string' || input.engine_override_reason.length > 500)) {
        return response({ error: 'invalid_session_request', field: 'engine_override_reason', detail: 'invalid_reason' }, 400);
      }
      // No casual engine picker (fresh-start gate is inside the txn after the
      // prior lookup so legacy engine rows without a stored reason can still
      // replay/retry under their original key).
      if (input.machine !== undefined && !isValidMachineId(input.machine)) return response({ error: 'invalid_session_request', field: 'machine', detail: 'invalid_machine' }, 400);
      if (input.name !== undefined && !nameOk(input.name)) return response({ error: 'invalid_session_request', field: 'name', detail: 'invalid_name' }, 400);
      if (input.cwd !== undefined && !cwdOk(input.cwd)) return response({ error: 'invalid_session_request', field: 'cwd', detail: 'invalid_cwd' }, 400);
      if (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) {
        return response({ error: 'invalid_session_request', field: 'mode', detail: 'invalid_mode' }, 400);
      }
      if (input.parent_id !== undefined && (typeof input.parent_id !== 'string' || !input.parent_id.trim() || input.parent_id.length > 256)) {
        return response({ error: 'invalid_session_request', field: 'parent_id', detail: 'invalid_parent_id' }, 400);
      }
      if (input.intake_task_id !== undefined && (typeof input.intake_task_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(input.intake_task_id))) {
        return response({ error: 'invalid_session_request', field: 'intake_task_id', detail: 'invalid_intake_task_id' }, 400);
      }
      if (input.brief !== undefined && (typeof input.brief !== 'string' || !input.brief.trim() || input.brief.length > 8000)) {
        return response({ error: 'invalid_session_request', field: 'brief', detail: 'invalid_brief' }, 400);
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
      if (input.with_goal !== undefined && (input.with_goal !== true || mode !== 'interactive' || !input.brief?.trim())) {
        return response({ error: 'interactive_goal_required' }, 400);
      }
      if (mode === 'interactive' && input.brief !== undefined && !input.with_goal) {
        return response({ error: 'interactive_starts_empty' }, 400);
      }
      if (mode === 'headless' && input.brief !== undefined && !input.brief.trim()) {
        return response({ error: 'invalid_session_request', field: 'brief', detail: 'empty_brief' }, 400);
      }
      const actor = caller.actor || caller.machineId || 'admin';
      try { return this.transaction(() => {
        const finish = (row: any, extra: any = {}, status = 200) => {
          recordIntakeSessionLink(this.sql, row, this.now(), extra.hold);
          return response({ ...this.view(row), ...extra }, status);
        };
        let parentId: string | null = null;
        let parentSource = 'unknown_external';
        if (input.parent_id) {
          const trimmedParent = input.parent_id.trim();
          let parentRow: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', trimmedParent).toArray()[0];
          if (!parentRow) {
            for (const r of this.sql.exec('SELECT * FROM session_control_sessions').toArray() as any[]) {
              const b = parsed(r.binding_json, null);
              if (b?.native_id === trimmedParent) {
                parentRow = r;
                break;
              }
            }
          }
          if (!parentRow) {
            return response({ error: 'parent_session_not_found' }, 404);
          }
          if (!admin && !this.ownerOk(parentRow, caller)) {
            return response({ error: 'parent_forbidden' }, 403);
          }
          let curr = parentRow;
          const visited = new Set<string>();
          while (curr && curr.parent_id) {
            if (visited.has(curr.parent_id)) {
              return response({ error: 'parent_cycle_detected' }, 400);
            }
            visited.add(curr.parent_id);
            curr = this.sql.exec('SELECT id, parent_id FROM session_control_sessions WHERE id = ?', curr.parent_id).toArray()[0];
          }
          parentId = parentRow.id;
          parentSource = 'explicit_parent';
        }
        const sessionType = mode === 'interactive' ? 'aplexer' : 'headless';
        const canonical = JSON.stringify({
          ...(input.task_kind !== undefined ? {task_kind:input.task_kind} : {}),
          engine: input.engine ?? null, engine_override_reason: (input as any).engine_override_reason ?? null,
          machine: input.machine ?? null,
          workspace: input.workspace, cwd: input.cwd ?? null, brief: input.brief ?? null,
          ...(input.with_goal === true ? { with_goal: true } : {}),
          mode, access: input.access ?? null, name: input.name ?? null,
          parent_id: parentId ?? null, intake_task_id: input.intake_task_id ?? null,
        });

        const prior: any = this.sql.exec(
          'SELECT * FROM session_control_sessions WHERE actor = ? AND request_key = ?', actor, input.key).toArray()[0];
        if (prior) {
          if (!this.ownerOk(prior, caller)) return response({ error: 'not_found' }, 404);
          // Before lineage shipped, canonical starts omitted parent_id entirely.
          const priorInput = parsed(prior.request_json, {});
          if (!Object.prototype.hasOwnProperty.call(priorInput, 'parent_id')) priorInput.parent_id = null;
          if (!Object.prototype.hasOwnProperty.call(priorInput, 'intake_task_id')) priorInput.intake_task_id = null;
          if (!Object.prototype.hasOwnProperty.call(priorInput, 'engine_override_reason')) priorInput.engine_override_reason = null;
          // Order-insensitive idempotency: legacy rows may carry the same
          // fields in a different key order (e.g. backfilled defaults).
          const norm = (o: any) => JSON.stringify(o, Object.keys(o).sort());
          if (norm(priorInput) !== norm(parsed(canonical, {}))) return response({ error: 'idempotency_conflict' }, 409);
          // A Fleet-side promotion (queued -> offered) is adopted before the
          // replay below, so polling the same key observes placement.
          const live = this.syncFleetPlacement(prior.id) ?? prior;
          if (live.fleet_launch_id || live.state === 'cancelled') return finish(live, { replay: true });
          // A held intent may be placed once capacity/admission recovers; the fleet
          // reserve stays idempotent on its own derived key, so this cannot double-launch.
        }
        // No casual engine picker: fresh starts with an explicit engine must
        // carry a non-empty explanation. Replays under an existing key skip
        // this so legacy engine rows without a stored reason can still
        // replay/retry; a different reason under the same key is a 409 above.
        if (!prior && input.engine !== undefined && (typeof input.engine_override_reason !== 'string' || !input.engine_override_reason.trim())) {
          return response({ error: 'engine_reason_required', field: 'engine_override_reason', detail: 'Engine override requires an explanation why this specific model/engine must be chosen (use engine_override_reason). Casual use should prefer auto/policy without engine.' }, 400);
        }
        // Start gate (server-side, before any Fleet placement or session row):
        // a start linked to an intake task requires non-empty acceptance
        // criteria on that task. Missing or whitespace-only values fail closed
        // with acceptance_criteria_required; an unknown task id fails closed
        // with unknown_intake_task. Starts without intake_task_id are
        // unlinked and unaffected — link intake-backed work explicitly.
        // TBM Phase 2: every non-interactive (headless) start must be linked to
        // an open intake task; interactive sessions are never gated.
        if (mode !== 'interactive' && !input.intake_task_id) {
          return response({ error: 'intake_task_id_required' }, 400);
        }
        if (input.intake_task_id) {
          let row: any = this.sql.exec('SELECT id, data FROM intake_tasks WHERE id = ?', input.intake_task_id).toArray()[0];
          if (!row && input.intake_task_id.length >= 6) {
            const candidates: any[] = this.sql.exec('SELECT id, data FROM intake_tasks WHERE id LIKE ? || "%" LIMIT 2', input.intake_task_id).toArray();
            if (candidates.length === 1) {
              row = candidates[0];
              input.intake_task_id = row.id;
            } else if (candidates.length > 1) {
              return response({ error: 'ambiguous_intake_task' }, 409);
            }
          }
          if (!row) return response({ error: 'unknown_intake_task' }, 404);
          let data: any;
          try {
            data = JSON.parse(row.data);
          } catch {
            return response({ error: 'unknown_intake_task' }, 404);
          }
          const ac = data ? data.acceptance_criteria : undefined;
          const taskStatus = data?.status;
          if (taskStatus === 'done' || taskStatus === 'cancelled') return response({ error: 'intake_task_closed' }, 409);
          if (typeof ac !== 'string' || !ac.trim()) return response({ error: 'acceptance_criteria_required' }, 400);
          const scopeBlock = getScopeBlock(data);
          if (scopeBlock) return response({ error: scopeBlock }, 409);
        }
        // TBM Phase 2: kill-switch + hard floor + dynamic soft reserve for
        // autonomous starts (fail closed; interactive starts unaffected).
        if (mode !== 'interactive') {
          const gate = this.tbmGate(input.engine);
          if (!gate.ok) {
            const status = gate.reason === 'kill_switch_active' ? 423 : 409;
            return response({ error: 'tbm_admission_denied', reason: gate.reason, pool_id: gate.pool_id, detail: gate.detail ?? null }, status);
          }
        }
        // Held-row hygiene in the same mutation txn: an unplaced held row is a parked
        // intent, not a session, so retries with fresh keys must not pile it up
        // unbounded. Supersede this actor's older unplaced holds for the same
        // workspace+name and expire unplaced holds past 48h. Rows that already
        // placed (fleet_launch_id or binding) are never touched, and the intent
        // currently being placed (prior) is excluded so a same-key retry can
        // still re-reserve.
        const hygieneAt = this.at();
        const staleBefore = new Date(Date.parse(hygieneAt) - 48 * 3600000).toISOString();
        this.sql.exec(
          `UPDATE session_control_sessions SET state = 'cancelled', updated_at = ?
           WHERE actor = ? AND state = 'held' AND fleet_launch_id IS NULL AND binding_json IS NULL AND id <> ?
             AND json_extract(request_json, '$.workspace') = ? AND json_extract(request_json, '$.name') = ?`,
          hygieneAt, actor, prior?.id ?? '', input.workspace, input.name ?? null);
        this.sql.exec(
          `UPDATE session_control_sessions SET state = 'cancelled', updated_at = ?
           WHERE actor = ? AND state = 'held' AND fleet_launch_id IS NULL AND binding_json IS NULL AND id <> ? AND created_at < ?`,
          hygieneAt, actor, prior?.id ?? '', staleBefore);
        // Fleet-linked capacity-queue hygiene: supersede this actor's older
        // queued parks for the same workspace+name and expire queued parks
        // past 48h, releasing their fleet waitlist row while still queued.
        // Placed rows (a binding, or a fleet launch beyond queued) are never
        // touched, and the intent being placed (prior) is excluded.
        const queuedParks: any[] = (() => {
          try {
            return this.sql.exec(
              `SELECT id, fleet_launch_id FROM session_control_sessions WHERE actor = ? AND state = 'queued' AND binding_json IS NULL AND id <> ?
                 AND (created_at < ? OR (json_extract(request_json, '$.workspace') = ? AND json_extract(request_json, '$.name') = ?))`,
              actor, prior?.id ?? '', staleBefore, input.workspace, input.name ?? null).toArray() as any[];
          } catch { return []; }
        })();
        for (const park of queuedParks) {
          this.sql.exec(`UPDATE session_control_sessions SET state = 'cancelled', updated_at = ? WHERE id = ? AND state = 'queued' AND binding_json IS NULL`, hygieneAt, park.id);
          if (park.fleet_launch_id) {
            try { this.sql.exec(`UPDATE fleet_launches SET state = 'cancelled', updated_at = ? WHERE id = ? AND state = 'queued'`, hygieneAt, park.fleet_launch_id); } catch { /* fleet table absent in narrow fixtures */ }
          }
        }
        // Post-body liveness inside the mutation txn: a credential revoked,
        // expired, or re-enrolled while the request body streamed must not
        // create a session row (or stamp a NULL family). Admins bypass; every
        // other creator needs a live agent incarnation in a live family.
        let creatorFamily: string | null = null;
        if (admin && caller.kind === 'human' && caller.tokenId) {
          if (!this.cleanupCallerLive(caller)) return response({ error: 'forbidden' }, 403);
          creatorFamily = this.presentingFamily(caller);
          if (!creatorFamily) return response({ error: 'forbidden' }, 403);
        } else if (!admin) {
          if (!this.agentLive(caller)) return response({ error: 'forbidden' }, 403);
          creatorFamily = this.presentingFamily(caller);
          if (!creatorFamily) return response({ error: 'forbidden' }, 403);
        }
        if (!this.hooks.reserve) {
          if (!prior) {
            const id = crypto.randomUUID();
            const at = this.at();
            this.sql.exec(
              'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, owner_token_id, owner_family, parent_id, parent_source, session_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
              id, actor, input.key, canonical, mode, 'held', caller.tokenId ?? null, creatorFamily, parentId, parentSource, sessionType, at, at);
            const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
            return finish(row, { hold: 'no_placement_authority' }, 409);
          }
          return finish(prior, { hold: 'no_placement_authority', replay: true }, 409);
        }
        const id = prior?.id || crypto.randomUUID();
        const at = this.at();
        if (!prior) {
          this.sql.exec(
            'INSERT INTO session_control_sessions (id, actor, request_key, request_json, mode, state, owner_token_id, owner_family, parent_id, parent_source, session_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            id, actor, input.key, canonical, mode, 'queued', caller.tokenId ?? null, creatorFamily, parentId, parentSource, sessionType, at, at);
        }
      // engine / machine / tier / workspace / cwd / mode / prompt reach the
      // existing fleet reserve as REAL placement constraints (exact route
      // match + tier-membership filter + M6/M8 gates). Nothing is silently
      // discarded: unknown tiers/cwds are rejected above, and a tier with no
      // eligible route becomes an honest held session.
      const reserved = this.hooks.reserve({
        ...(input.task_kind !== undefined ? {task_kind:input.task_kind} : {}),
        ...(input.intake_task_id ? {project:parsed(this.sql.exec('SELECT data FROM intake_tasks WHERE id=?',input.intake_task_id).toArray()[0]?.data,{}).project} : {}),
        mode, machine: input.machine, engine: input.engine, tier: input.tier,
        workspace: input.workspace, cwd: input.cwd ?? null,
        prompt: input.brief || '', access: input.access, ...(input.with_goal === true ? { with_goal: true } : {}),
        idempotency_key: 'session-control-' + id,
      }, auth);
        if (reserved.status === 400) {
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'refused', at, id);
          const refused: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
          recordIntakeSessionLink(this.sql, refused, this.now());
          try { if (mode !== 'interactive') recordSessionOutcomeReward(this.sql, { session_id: id, intake_task_id: input.intake_task_id ?? null, engine: input.engine ?? null, outcome: 'refused' }, this.now()); } catch {}
          return response(reserved.value, 400);
        }
        // Capacity-only placement parks a waitable queued intent (202) linked
        // to the fleet waitlist row; the fleet drain auto-places it when a
        // slot frees and polls adopt the promotion. All other failures hold.
        if (reserved.status === 202 && reserved.value && typeof reserved.value.id === 'string' && reserved.value.state === 'queued') {
          this.sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = NULL, engine = ?, state = ?, updated_at = ? WHERE id = ?',
            reserved.value.id, typeof reserved.value.engine === 'string' && reserved.value.engine ? reserved.value.engine : (input.engine ?? null), 'queued', at, id);
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
          return finish(row, { queued: true, queue_position: reserved.value.queue_position ?? null, rejected: reserved.value.rejected ?? null, requested: { engine: input.engine ?? null, machine: input.machine ?? null, workspace: input.workspace } }, 202);
        }
        if (reserved.status >= 300) {
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'held', at, id);
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
          return finish(row, { hold: reserved.value, requested: { engine: input.engine ?? null, machine: input.machine ?? null, workspace: input.workspace } }, 409);
        }
        this.sql.exec('UPDATE session_control_sessions SET fleet_launch_id = ?, machine_id = ?, engine = ?, state = ?, updated_at = ? WHERE id = ?',
          reserved.value.id, reserved.value.machine ?? null, reserved.value.engine ?? null, reserved.value.state || 'offered', at, id);
        const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', id).toArray()[0];
        return finish(row, { replay: !!prior }, prior ? 200 : 201);
      }); } catch (e) {
        if (e instanceof IntakeSessionLinkError) return response({ error: e.code }, 507);
        throw e;
      }
    }

    if ((path === '/api/sessions/tree' || (path === '/api/sessions' && (url.searchParams.get('view') === 'tree' || url.searchParams.get('tree') === 'true'))) && request.method === 'GET') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const allRows = (admin
        ? this.sql.exec('SELECT * FROM session_control_sessions ORDER BY created_at ASC, id ASC').toArray()
        : this.sql.exec('SELECT * FROM session_control_sessions WHERE actor = ? ORDER BY created_at ASC, id ASC', caller.actor).toArray()
      ).filter((r: any) => this.ownerOk(r, caller));

      const views = allRows.map((r: any) => ({ ...this.view(r), children: [] }));
      const map = new Map<string, any>();
      for (const v of views) {
        map.set(v.id, v);
        if (v.binding?.native_id) map.set(v.binding.native_id, v);
      }
      const roots: any[] = [];
      for (const v of views) {
        if (v.parent_id && map.has(v.parent_id) && map.get(v.parent_id) !== v) {
          map.get(v.parent_id).children.push(v);
        } else {
          roots.push(v);
        }
      }
      return response({ tree: roots, total: views.length });
    }

    if (path === '/api/sessions/warnings' && request.method === 'GET') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      this.transaction(() => reconcileUnboundFleetSessions(this.sql, this.now()));
      const rows = (admin
        ? this.sql.exec("SELECT * FROM session_control_sessions WHERE state = 'running' ORDER BY created_at, id").toArray()
        : this.sql.exec("SELECT * FROM session_control_sessions WHERE state = 'running' AND actor = ? ORDER BY created_at, id", caller.actor).toArray()) as any[];
      const warnings = rows.filter(row => this.ownerOk(row, caller)).map(row => this.view(row, threshold)).filter(row => row.warning_active);
      return response({ warnings });
    }

    if (path === '/api/sessions' && request.method === 'GET') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      this.transaction(() => reconcileUnboundFleetSessions(this.sql, this.now()));
      // M15c lifecycle: additive grouped needs-action projection over the
      // canonical stores (never a second registry). Scope is authentication,
      // never a payload flag: non-admin callers always see their own actor.
      if (url.searchParams.get('view') === 'lifecycle') {
        const scopeParam = (name: string): string | null => {
          const value = url.searchParams.get(name);
          if (value == null || value === '') return null;
          if (value.length > 128) return null;
          return value;
        };
        const scope = admin
          ? { owner: scopeParam('owner'), host: scopeParam('host'), task: scopeParam('task') }
          : { owner: caller.actor ?? null, host: scopeParam('host'), task: scopeParam('task') };
        const limit = Math.min(50, Math.max(1, Number(url.searchParams.get('limit')) || 50));
        return response(buildLifecycleView(this.sql, scope, {
          limit, cursor: url.searchParams.get('cursor'), now_ms: this.now(),
        }));
      }
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
      return response({ sessions: page.map(r => this.view(r, threshold)), next_cursor: next });
    }

    // M15c lifecycle durable seams (narrow; command auth/effect code untouched).
    if (path === '/api/sessions/observations' && request.method === 'POST') {
      if (!host && !admin) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!input || typeof input !== 'object') return response({ error: 'invalid_observation' }, 400);
      // Machine scope is authentication: a host writes only its own machine.
      if (host && !admin && input.machine_id !== caller.machineId) {
        return response({ error: 'not_found' }, 404);
      }
      return this.transaction(() => {
        if (host && !admin && !this.lifecycleHostOk(caller)) {
          return response({ error: 'host_credential_changed' }, 409);
        }
        const stored = recordObservation(this.sql, this.now(), {
          machine_id: (input as any).machine_id,
          native_id: (input as any).native_id,
          observed_at: (input as any).observed_at,
          payload: (input as any).payload ?? input,
        });
        if (!stored.ok) return response({ error: stored.error }, stored.status);
        return response(stored, 201);
      });
    }

    if (path === '/api/sessions/decisions' && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      // Admin privilege is never another coordinator's ownership: admins
      // cannot record decisions for sessions they do not own (never oracle).
      if (admin) return response({ error: 'not_found' }, 404);
      const input = await body(request);
      if (!input || typeof input !== 'object') return response({ error: 'invalid_decision' }, 400);
      return this.transaction(() => {
        if (!this.agentLive(caller)) return response({ error: 'forbidden' }, 403);
        const key = (input as any).session_key;
        let ownerVerified = false;
        try {
          const refs = this.lifecycleRefsFor(key);
          if (refs?.session_id) {
            const sc: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', refs.session_id).toArray()[0];
            ownerVerified = !!sc && this.ownerOk(sc, caller) && sc.actor === caller.actor;
          } else if (refs?.fleet_launch_id) {
            const launch: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ?', refs.fleet_launch_id).toArray()[0];
            ownerVerified = !!launch && !!caller.actor && launch.actor === caller.actor;
          }
        } catch {
          ownerVerified = false;
        }
        const decided = recordDecision(this.sql, this.now(),
          { actor: caller.actor, isAdmin: false }, ownerVerified, {
            session_key: (input as any).session_key,
            kind: (input as any).kind,
            reason: (input as any).reason,
            engine: (input as any).engine,
            target: (input as any).target,
          });
        if (!decided.ok) return response({ error: decided.error }, decided.status);
        return response({ decision: decided.decision }, 201);
      });
    }

    if (path === '/api/sessions/cleanup' && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      const scopeParam = (name: string): string | null => {
        const value = url.searchParams.get(name);
        if (value == null || value === '') return null;
        return value.length > 128 ? null : value;
      };
      // M15d REAL apply: enqueue M12 stop intents for freshly eligible OWN
      // sessions only. Exact authenticated current-owner credential/family/
      // incarnation is required before any enqueue (inside the txn, after the
      // awaited body); admin privilege is never another coordinator's
      // ownership (admin apply is never an oracle). Human/Alexey sessions are
      // permanently protected. Only confirmed finished/superseded with zero
      // obligations are eligible — unknown/idle/limit alone never qualify.
      // The host executes each enqueued stop through the existing
      // claim/effect/ack custody with containment-confirmed exit; a missing
      // catalog entry is never exit proof. Worktree/branch removal runs
      // host-side afterwards under the trusted cleanup-root allowlist.
      // Partial results and uncertainty stay durable: an already-pending
      // stop intent replays (never duplicates), and every kept target
      // reports its actual reason. No pretend success anywhere.
      if (input && (input as any).apply === true) {
        if (admin && !caller.tokenId) return response({ error: 'not_found' }, 404);
        const rawKey = (input as any).key;
        if (rawKey !== undefined && (typeof rawKey !== 'string' || !label(rawKey))) {
          return response({ error: 'invalid_cleanup_key' }, 400);
        }
        const operationKey = typeof rawKey === 'string' ? rawKey : crypto.randomUUID();
        const scope = { owner: caller.actor ?? null, host: scopeParam('host'), task: scopeParam('task') };
        const callerOwner = caller.actor ?? '';
        const candidateSessionIds = new Set<string>();
        try {
          const preEligible = collectCleanupEligible(this.sql, scope, callerOwner, this.now());
          for (const cand of preEligible.eligible) {
            if (cand.refs.session_id) candidateSessionIds.add(cand.refs.session_id);
          }
        } catch {}
        try {
          const ownSessions = this.sql.exec('SELECT id FROM session_control_sessions WHERE actor = ?', callerOwner).toArray() as any[];
          for (const row of ownSessions) {
            if (row.id) candidateSessionIds.add(String(row.id));
          }
        } catch {}
        const keyMap = new Map<string, string>();
        for (const sid of candidateSessionIds) {
          keyMap.set(sid, await deriveCleanupSessionKey(operationKey, sid));
        }
        return this.transaction(() => {
          if (!this.cleanupCallerLive(caller)) return response({ error: 'forbidden' }, 403);
          const collected = collectCleanupEligible(this.sql, scope, callerOwner, this.now());
          const enqueued: { session_key: string; session_id: string; command_id: string; state: string; replay: boolean }[] = [];
          const refused = [...collected.ineligible];
          for (const target of collected.eligible) {
            if (target.refs.protected_session) {
              refused.push({ key: target.session_key, reasons: ['human/Alexey session permanently protected'] });
              continue;
            }
            let live: any = null;
            let ownerVerified = false;
            try {
              if (target.refs.session_id) {
                live = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', target.refs.session_id).toArray()[0] ?? null;
                ownerVerified = !!live && this.cleanupOwnerOk(live, caller);
              } else if (target.refs.fleet_launch_id) {
                const launch: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ?', target.refs.fleet_launch_id).toArray()[0];
                ownerVerified = !!launch && !!caller.actor && launch.actor === caller.actor;
                if (ownerVerified) {
                  refused.push({ key: target.session_key, reasons: ['no session-control intent to stop (fleet-observed only); listed, never acted'] });
                  continue;
                }
              }
            } catch {
              ownerVerified = false;
              live = null;
            }
            if (!live || !ownerVerified) {
              refused.push({ key: target.session_key, reasons: ['not owned by caller (admin is not ownership)'] });
              continue;
            }
            const req = parsed(live.request_json, {});
            if (live.actor === 'unknown' || req.provenance === 'observed_native') {
              refused.push({ key: target.session_key, reasons: ['observed_native session protected from automated cleanup'] });
              continue;
            }
            const binding = live.binding_json ? parsed(live.binding_json, null) : null;
            if (!binding || binding.native_id !== target.refs.native_id) {
              refused.push({ key: target.session_key, reasons: ['binding_mismatch: native incarnation drifted; listed, never acted'] });
              continue;
            }
            if (!CLEANUP_APPLY_ALLOWED_STATES.has(live.state)) {
              refused.push({ key: target.session_key, reasons: [`session_not_stoppable:${live.state}`] });
              continue;
            }
            let pending: any = null;
            try {
              pending = this.sql.exec(
                "SELECT * FROM session_control_commands WHERE session_id = ? AND kind = 'stop' AND state IN ('recorded','transported','claimed') ORDER BY created_at DESC LIMIT 1", live.id).toArray()[0] ?? null;
            } catch {
              pending = null;
            }
            if (pending) {
              // Never co-opt an ordinary stop as cleanup: only a stored
              // cleanup command replays here. An ordinary pending stop is
              // left alone and a fresh cleanup stop is enqueued below (the
              // host runs the tree ONLY for the flagged command).
              if (this.cleanupStored(pending)) {
                enqueued.push({ session_key: target.session_key, session_id: live.id, command_id: pending.id, state: pending.state, replay: true });
                continue;
              }
            }
            const pendingCount = Number(this.sql.exec(
              "SELECT COUNT(*) AS n FROM session_control_commands WHERE state IN ('recorded', 'transported', 'claimed')").one().n);
            if (pendingCount >= this.maxPending) {
              refused.push({ key: target.session_key, reasons: ['queue_full: reconciled via commands listing, do not blind-retry'] });
              continue;
            }
            const perSession = Number(this.sql.exec(
              "SELECT COUNT(*) AS n FROM session_control_commands WHERE session_id = ? AND state IN ('recorded', 'transported', 'claimed')", live.id).one().n);
            if (perSession >= this.maxPerSession) {
              refused.push({ key: target.session_key, reasons: ['session_queue_full: reconciled via commands listing, do not blind-retry'] });
              continue;
            }
            const sessionKey = keyMap.get(live.id);
            if (!sessionKey || !sessionKey.startsWith('cu-') || sessionKey.length > 64) {
              refused.push({ key: target.session_key, reasons: ['session_key_derivation_failed'] });
              continue;
            }
            // Server-owned cleanup-tree authorization: persisted ONLY here
            // (authenticated eligible cleanup --apply). Ordinary stop routes
            // enqueue fixed {} payloads and can never set this flag; the
            // cu- key prefix is caller-visible only and never authorizes.
            this.enqueue(live, 'stop', actorName(caller), caller, sessionKey, { cleanup_tree: true, operation_key: operationKey });
            let row: any = null;
            try {
              row = this.sql.exec('SELECT * FROM session_control_commands WHERE session_id = ? AND actor = ? AND request_key = ?', live.id, actorName(caller), sessionKey).toArray()[0] ?? null;
            } catch {
              row = null;
            }
            if (!row) {
              refused.push({ key: target.session_key, reasons: ['enqueue_failed: reconciled via commands listing, do not blind-retry'] });
              continue;
            }
            enqueued.push({ session_key: target.session_key, session_id: live.id, command_id: row.id, state: row.state, replay: false });
          }
          return response({ apply: true, dry_run: false, operation_key: operationKey, enqueued, refused, total: collected.total });
        });
      }
      const scope = admin
        ? { owner: scopeParam('owner'), host: scopeParam('host'), task: scopeParam('task') }
        : { owner: caller.actor ?? null, host: scopeParam('host'), task: scopeParam('task') };
      return this.transaction(() => {
        const plan = planCleanupDryRun(this.sql, scope, caller.actor ?? '', this.now());
        plan.eligible = plan.eligible.filter(target => {
          const row: any = target.refs.session_id ? this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', target.refs.session_id).toArray()[0] : null;
          if (row && this.cleanupOwnerOk(row, caller)) return true;
          plan.ineligible.push({ key: target.session_key, reasons: ['not owned by current credential family'] });
          return false;
        });
        return response(plan);
      });
    }

    if (path === '/api/sessions/host-adoption' && request.method === 'POST') {
      if (!host && !admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!data || typeof data !== 'object' || !Array.isArray(data.sessions)) {
        return response({ error: 'invalid_adoption_request' }, 400);
      }
      const targetMachine = host ? caller.machineId! : (data.machine_id || caller.machineId);
      if (!targetMachine) return response({ error: 'missing_machine_id' }, 400);
      return this.transaction(() => {
        if (host) {
          const gate = this.hostOk({ machine_id: targetMachine }, caller);
          if (!gate.ok) return response({ error: gate.error }, 409);
        }
        const result = adoptObservedNativeSessions(this.sql, targetMachine, data.sessions, this.now(), this.presentingFamily(caller));
        return response({ status: 'ok', ...result });
      });
    }

    const sessionMatch = /^\/api\/sessions\/([^/]{1,128})(\/(send|capture|stop|commands|binding|retry|cancel|protect|retirement|ack|events))?$/.exec(path);
    if (sessionMatch) {
      const rawRef = decodeURIComponent(sessionMatch[1]);
      const sub = sessionMatch[3];
      let session: any = null;
      if (UUID_RE.test(rawRef)) {
        session = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', rawRef).toArray()[0] ?? null;
        // Fleet launch UUIDs are also returned to operators. Resolve them to
        // their canonical control session, with the same ownership checks.
        // Hosts must keep using the exact session UUID for binding/retirement.
        if (!session && sub !== 'binding' && sub !== 'retirement') {
          const matches = (this.sql.exec('SELECT * FROM session_control_sessions WHERE fleet_launch_id = ?', rawRef).toArray() as any[])
            .filter(row => this.ownerOk(row, caller));
          if (matches.length > 1) return response({ error: 'ambiguous_name' }, 409);
          session = matches[0] ?? null;
        }
        if (!session) {
          const rows = this.sql.exec('SELECT * FROM session_control_sessions WHERE binding_json IS NOT NULL').toArray() as any[];
          for (const r of rows) {
            try {
              const b = JSON.parse(r.binding_json);
              if (b?.native_id === rawRef) {
                session = r;
                break;
              }
            } catch {}
          }
        }
      } else if (/^[a-f0-9][a-f0-9-]{7,34}$/i.test(rawRef) && sub !== 'binding' && sub !== 'retirement') {
        const matches = visibleSessionPrefixMatches(this.sql, rawRef, row => this.ownerOk(row, caller));
        if (matches.length > 1) return response({ error: 'ambiguous_session_prefix',
          matches: matches.map(row => ({ id: row.id, name: parsed(row.request_json, {}).name ?? null })) }, 409);
        session = matches[0] ?? null;
        if (!session) {
          const resolved = this.resolveSessionByName(rawRef, caller);
          if (!resolved.ok) return response({ error: resolved.error }, resolved.status);
          session = resolved.session;
        }
      } else if (rawRef.includes(':')) {
        if (sub === 'binding' || sub === 'retirement') return response({ error: 'not_found' }, 404);
        if ((sub === undefined && request.method !== 'GET') ||
            (sub !== undefined && !(['send', 'capture', 'stop', 'cancel', 'protect', 'ack'].includes(sub) ? request.method === 'POST' : request.method === 'GET'))) {
          return response({ error: 'not_found' }, 404);
        }
        const resolved = this.resolveSessionByQualifiedAlias(rawRef, caller);
        if (!resolved.ok) return response({ error: resolved.error }, resolved.status);
        session = resolved.session;
      } else {
        // Friendly-name alias: actor-scoped exact match only (never a native
        // tag, never a foreign session, never latest-wins). Ambiguous names
        // are refused with 409; unknown names render as not_found. The host
        // binding path stays strict Hub UUID (no alias).
        if (sub === 'binding' || sub === 'retirement') return response({ error: 'not_found' }, 404);
        if ((sub === undefined && request.method !== 'GET') ||
            (sub !== undefined && !(['send', 'capture', 'stop', 'cancel', 'protect', 'ack'].includes(sub) ? request.method === 'POST' : request.method === 'GET'))) {
          return response({ error: 'not_found' }, 404);
        }
        const resolved = this.resolveSessionByName(rawRef, caller);
        if (!resolved.ok) return response({ error: resolved.error }, resolved.status);
        session = resolved.session;
      }
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
      if (sub === 'retirement') {
        if (request.method !== 'POST') return response({ error: 'not_found' }, 404);
        // Retirement principal must be authenticated host kind+hostscope+ownmachine, using existing authoritative hostOk family/approval policy; no separate admin-only fallback.
        if (!host || caller.kind !== 'host' || !caller.machineId) return response({ error: 'forbidden' }, 403);
        if (!session) return response({ error: 'not_found' }, 404);
        if (session.machine_id !== caller.machineId) return response({ error: 'not_found' }, 404);
        const gate = this.hostOk(session, caller);
        if (!gate.ok) return response({ error: gate.error }, gate.error === 'not_found' ? 404 : 409);

        // Do not settle Hub-started rows here; normal observer owns those.
        if (session.fleet_launch_id) return response({ error: 'session_not_imported' }, 409);
        const currentBinding = parsed(session.binding_json, null);
        if (!currentBinding || typeof currentBinding !== 'object' || currentBinding.provenance !== 'observed_native') {
          return response({ error: 'session_not_imported' }, 409);
        }

        // Require KNOWN complete stored binding and supplied full tuple/generation:
        // known integerpositive creation/incarnation and generation, nonempty engine, exact comparisons.
        if (typeof currentBinding.native_id !== 'string' || !UUID_RE.test(currentBinding.native_id) || currentBinding.native_id !== session.id ||
            typeof currentBinding.tag !== 'string' || !currentBinding.tag ||
            typeof currentBinding.workspace !== 'string' || !currentBinding.workspace ||
            typeof currentBinding.native_created_at_ms !== 'number' || !Number.isSafeInteger(currentBinding.native_created_at_ms) || currentBinding.native_created_at_ms <= 0 ||
            typeof currentBinding.generation !== 'number' || !Number.isSafeInteger(currentBinding.generation) || currentBinding.generation <= 0 ||
            (typeof session.engine !== 'string' || !session.engine)) {
          return response({ error: 'stored_binding_incomplete' }, 409);
        }

        const input = await body(request);
        if (!input || typeof input !== 'object' || Array.isArray(input)) {
          return response({ error: 'invalid_retirement' }, 400);
        }

        // Canonical payload exactly native{native_id,tag,workspace,created_at_ms,engine}, generation, finished_at_ms, cause:'finished', state:'stopped'. Reject unsupported/unknown/missing fields.
        const requiredTopKeys = ['cause', 'finished_at_ms', 'generation', 'native', 'state'];
        const inputKeys = Object.keys(input);
        if (inputKeys.length !== requiredTopKeys.length || !requiredTopKeys.every(k => k in input)) {
          return response({ error: 'invalid_retirement' }, 400);
        }

        if (!input.native || typeof input.native !== 'object' || Array.isArray(input.native)) {
          return response({ error: 'invalid_retirement' }, 400);
        }
        const requiredNativeKeys = ['created_at_ms', 'engine', 'native_id', 'tag', 'workspace'];
        const nativeKeys = Object.keys(input.native);
        if (nativeKeys.length !== requiredNativeKeys.length || !requiredNativeKeys.every(k => k in input.native)) {
          return response({ error: 'invalid_retirement' }, 400);
        }

        if (typeof input.native.native_id !== 'string' || !UUID_RE.test(input.native.native_id) || input.native.native_id !== rawRef) {
          return response({ error: 'native_id_mismatch' }, 409);
        }
        if (input.native.native_id !== session.id || input.native.native_id !== currentBinding.native_id) {
          return response({ error: 'native_id_mismatch' }, 409);
        }
        if (typeof input.native.tag !== 'string' || !input.native.tag || input.native.tag !== currentBinding.tag) {
          return response({ error: 'tag_mismatch' }, 409);
        }
        if (typeof input.native.workspace !== 'string' || !input.native.workspace || input.native.workspace !== currentBinding.workspace) {
          return response({ error: 'workspace_mismatch' }, 409);
        }
        if (typeof input.native.created_at_ms !== 'number' || !Number.isSafeInteger(input.native.created_at_ms) || input.native.created_at_ms <= 0) {
          return response({ error: 'invalid_creation_timestamp' }, 400);
        }
        if (input.native.created_at_ms !== currentBinding.native_created_at_ms) {
          return response({ error: 'created_at_mismatch' }, 409);
        }
        if (typeof input.native.engine !== 'string' || !input.native.engine || input.native.engine !== session.engine) {
          return response({ error: 'engine_mismatch' }, 409);
        }

        if (typeof input.generation !== 'number' || !Number.isSafeInteger(input.generation) || input.generation <= 0) {
          return response({ error: 'invalid_generation' }, 400);
        }
        if (input.generation !== currentBinding.generation) {
          return response({ error: 'generation_mismatch' }, 409);
        }

        if (input.cause !== 'finished') {
          return response({ error: 'invalid_retirement_cause' }, 400);
        }

        if (typeof input.finished_at_ms !== 'number' || !Number.isSafeInteger(input.finished_at_ms) || input.finished_at_ms <= 0) {
          return response({ error: 'invalid_finished_timestamp' }, 400);
        }
        if (input.finished_at_ms < input.native.created_at_ms) {
          return response({ error: 'finished_predates_creation' }, 400);
        }
        if (input.finished_at_ms > this.now() + 60000) {
          return response({ error: 'future_finished_timestamp' }, 400);
        }

        if (input.state !== 'stopped') {
          return response({ error: 'invalid_state' }, 400);
        }

        return this.transaction(() => {
          const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          if (!live) return response({ error: 'not_found' }, 404);
          if (live.machine_id !== caller.machineId) return response({ error: 'not_found' }, 404);
          const fresh = this.hostOk(live, caller);
          if (!fresh.ok) return response({ error: fresh.error }, fresh.error === 'not_found' ? 404 : 409);
          if (live.fleet_launch_id) return response({ error: 'session_not_imported' }, 409);

          const liveBinding = parsed(live.binding_json, null);
          if (!liveBinding || typeof liveBinding !== 'object' || liveBinding.provenance !== 'observed_native') {
            return response({ error: 'session_not_imported' }, 409);
          }

          if (typeof liveBinding.native_id !== 'string' || !UUID_RE.test(liveBinding.native_id) || liveBinding.native_id !== live.id ||
              typeof liveBinding.tag !== 'string' || !liveBinding.tag ||
              typeof liveBinding.workspace !== 'string' || !liveBinding.workspace ||
              typeof liveBinding.native_created_at_ms !== 'number' || !Number.isSafeInteger(liveBinding.native_created_at_ms) || liveBinding.native_created_at_ms <= 0 ||
              typeof liveBinding.generation !== 'number' || !Number.isSafeInteger(liveBinding.generation) || liveBinding.generation <= 0 ||
              (typeof live.engine !== 'string' || !live.engine)) {
            return response({ error: 'stored_binding_incomplete' }, 409);
          }

          // In same synchronous effect transaction revalidate CURRENT machine/family/imported provenance/fleet absence/full identity/generation and allowed physical state before any terminal write
          if (input.native.native_id !== live.id || input.native.native_id !== liveBinding.native_id) {
            return response({ error: 'native_id_mismatch' }, 409);
          }
          if (input.native.tag !== liveBinding.tag) {
            return response({ error: 'tag_mismatch' }, 409);
          }
          if (input.native.workspace !== liveBinding.workspace) {
            return response({ error: 'workspace_mismatch' }, 409);
          }
          if (input.native.created_at_ms !== liveBinding.native_created_at_ms) {
            return response({ error: 'created_at_mismatch' }, 409);
          }
          if (input.native.engine !== live.engine) {
            return response({ error: 'engine_mismatch' }, 409);
          }
          if (input.generation !== liveBinding.generation) {
            return response({ error: 'generation_mismatch' }, 409);
          }

          // Replays exact/idempotent, no repeated controls/effects.
          // Persist bounded retirement marker in existing binding metadata so only SAME tuple/gen/cause/finishedtimestamp replays idempotently, and a conflicting replay does not claim success or overwrite terminal history.
          if (live.state === 'stopped') {
            const marker = liveBinding.retirement;
            if (!marker || typeof marker !== 'object') {
              return response({ error: 'retirement_conflict' }, 409);
            }
            const sameReplay =
              marker.native_id === input.native.native_id &&
              marker.tag === input.native.tag &&
              marker.workspace === input.native.workspace &&
              marker.created_at_ms === input.native.created_at_ms &&
              marker.engine === input.native.engine &&
              marker.generation === input.generation &&
              marker.cause === input.cause &&
              marker.finished_at_ms === input.finished_at_ms;
            if (!sameReplay) {
              return response({ error: 'retirement_conflict' }, 409);
            }
            return response({ ...this.view(live), id: live.id, state: 'stopped', replay: true });
          }

          if (!STOPPABLE.has(live.state)) {
            return response({ error: 'session_not_stoppable' }, 409);
          }

          const at = new Date(this.now()).toISOString();
          const updatedBinding = {
            ...liveBinding,
            retirement: {
              native_id: input.native.native_id,
              tag: input.native.tag,
              workspace: input.native.workspace,
              created_at_ms: input.native.created_at_ms,
              engine: input.native.engine,
              generation: input.generation,
              cause: input.cause,
              finished_at_ms: input.finished_at_ms,
              retired_at: at,
            },
          };

          this.sql.exec(
            "UPDATE session_control_sessions SET state = 'stopped', binding_json = ?, updated_at = ? WHERE id = ?",
            JSON.stringify(updatedBinding), at, live.id
          );
          try {
            const reqObj = parsed(live.request_json, {});
            recordSessionOutcomeReward(this.sql, {
              session_id: live.id,
              intake_task_id: reqObj?.intake_task_id ?? null,
              engine: live.engine ?? input.native?.engine ?? null,
              outcome: 'stopped',
            }, this.now());
          } catch {}
          const updatedRow: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', live.id).toArray()[0];
          return response({ ...this.view(updatedRow), id: live.id, state: 'stopped', replay: false });
        });
      }
      if (!session || !this.ownerOk(session, caller)) return response({ error: 'not_found' }, 404);
      // Polling a capacity-queued intent adopts any Fleet-side promotion, so
      // the wait is observable without another placement call.
      if (!sub && request.method === 'GET') return this.transaction(() => response(this.view(this.syncFleetPlacement(session.id) ?? session, threshold)));
      if (sub === 'events' && request.method === 'GET') {
        return response({ events: this.sql.exec('SELECT * FROM session_warning_events WHERE session_id = ? ORDER BY created_at, id', session.id).toArray() });
      }
      if (sub === 'ack' && request.method === 'POST') {
        const input = await body(request);
        const seconds = input?.snooze ?? threshold;
        if (!obj(input, ['snooze']) || !Number.isSafeInteger(seconds) || seconds <= 0 || seconds > 31536000) return response({ error: 'invalid_snooze' }, 400);
        return this.transaction(() => {
          const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          if (!writer || !live || !this.ownerOk(live, caller)) return response({ error: 'not_found' }, 404);
          const event = { id: crypto.randomUUID(), session_id: live.id, kind: 'warning_ack', actor: actorName(caller), created_at: this.at(), until_at: new Date(this.now() + seconds * 1000).toISOString() };
          this.sql.exec('INSERT INTO session_warning_events (id, session_id, kind, actor, created_at, until_at) VALUES (?, ?, ?, ?, ?, ?)', event.id, event.session_id, event.kind, event.actor, event.created_at, event.until_at);
          return response({ ...this.view(live, threshold), event });
        });
      }
      if (sub === 'protect' && request.method === 'POST') {
        const input = await body(request);
        if (!obj(input, ['key']) || !label(input.key)) return response({ error: 'invalid_protection' }, 400);
        return this.transaction(() => {
          const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          if (!writer || !live || !this.cleanupOwnerOk(live, caller)) return response({ error: 'not_found' }, 404);
          const prior: any = this.sql.exec('SELECT * FROM session_control_protections WHERE session_id = ?', live.id).toArray()[0];
          if (prior) {
            if (prior.actor !== caller.actor || prior.request_key !== input.key) return response({ error: 'idempotency_conflict' }, 409);
            return response({ ...this.view(live), replay: true });
          }
          this.sql.exec('INSERT INTO session_control_protections (session_id, actor, request_key, created_at) VALUES (?, ?, ?, ?)', live.id, caller.actor!, input.key, this.at());
          return response({ ...this.view(live), replay: false });
        });
      }
      // A stop of an unstarted intent is a cancellation, with the same atomic
      // claim fence and no fabricated native-exit receipt. Bound stops keep
      // using host command acknowledgement below.
      if ((sub === 'cancel' || (sub === 'stop' && !session.binding_json &&
          ['held', 'queued', 'offered', 'cancelled'].includes(session.state))) && request.method === 'POST') {
        const input = await body(request);
        if (!obj(input, ['key']) || !label(input.key)) return response({ error: 'invalid_cancel' }, 400);
        return this.transaction(() => {
          // Body streaming may yield to a retry or credential revocation. Fresh
          // state and ownership are checked in the same synchronous transaction
          // as the receipt. Fleet reserve is synchronous in the start txn too:
          // either placement wins and this refuses, or cancellation fences all
          // later starts/retries before reserve can be called.
          const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
          if (!writer || !live || !this.cancelOwnerLive(live, caller, sub === 'stop')) {
            return response({ error: 'not_found' }, 404);
          }
          const prior: any = this.sql.exec('SELECT * FROM session_control_cancellations WHERE session_id = ?', live.id).toArray()[0];
          if (prior) {
            if (prior.actor !== actorName(caller) || prior.request_key !== input.key) return response({ error: 'idempotency_conflict' }, 409);
            return response({ ...this.view(live), cancellation: { key: prior.request_key, created_at: prior.created_at, effect: 'no_native_start' }, replay: true });
          }
          const pending = this.sql.exec('SELECT id FROM session_control_commands WHERE session_id = ? LIMIT 1', live.id).toArray();
          const original = parsed(live.request_json, {});
          // Queued and offered launches have no start authorization. Cancel
          // atomically against claim; never treat missing binding as no spawn.
          const queuedFleet = ['queued', 'held', 'offered'].includes(live.state) && live.fleet_launch_id && !live.binding_json && !pending.length && original.provenance !== 'observed_native'
            ? (() => { try { return this.sql.exec('SELECT state, native_id, launcher_task_id FROM fleet_launches WHERE id = ?', live.fleet_launch_id).toArray()[0] ?? null; } catch { return null; } })()
            : null;
          if (queuedFleet && (!['queued', 'offered'].includes(String(queuedFleet.state)) || queuedFleet.native_id || queuedFleet.launcher_task_id)) return response({ error: 'session_not_cancellable' }, 409);
          if ((live.state !== 'held' || live.fleet_launch_id || live.binding_json || pending.length || original.provenance === 'observed_native') && !queuedFleet) {
            return response({ error: 'session_not_cancellable' }, 409);
          }
          const at = this.at();
          if (queuedFleet) {
            let released = false;
            try {
              const written: any = this.sql.exec(`UPDATE fleet_launches SET state = 'cancelled', verification_status = 'not_started', exit_reason = 'session_cancelled', finished_at = ?, updated_at = ? WHERE id = ? AND state IN ('queued','offered') AND native_id IS NULL AND launcher_task_id IS NULL`, at, at, live.fleet_launch_id);
              released = written?.rowsWritten === 1;
            } catch { released = false; }
            // A winning host claim must never be stranded behind a
            // cancelled control session.
            if (!released) return response({ error: 'session_not_cancellable' }, 409);
          }
          this.sql.exec('INSERT INTO session_control_cancellations (session_id, actor, request_key, created_at) VALUES (?, ?, ?, ?)', live.id, actorName(caller), input.key, at);
          this.sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?', 'cancelled', at, live.id);
          try {
            if (live.mode !== 'interactive') {
              recordSessionOutcomeReward(this.sql, {
                session_id: live.id,
                intake_task_id: original?.intake_task_id ?? null,
                engine: live.engine ?? original?.engine ?? null,
                outcome: 'cancelled',
              }, this.now());
            }
          } catch {}
          const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', live.id).toArray()[0];
          return response({ ...this.view(row), cancellation: { key: input.key, created_at: at, effect: 'no_native_start' }, replay: false });
        });
      }
      if (sub === 'retry' && request.method === 'POST') {
        const input = await body(request);
        if (!obj(input, [])) return response({ error: 'invalid_retry' }, 400);
        // Reuse the original actor/key/payload; this cannot create a second
        // intent or bypass the placement/admission/liveness checks below.
        if (!writer || actorName(caller) !== session.actor) return response({ error: 'forbidden' }, 403);
        // A capacity-queued intent linked to the fleet waitlist reports any
        // Fleet-side promotion; otherwise it stays queued (the waitlist, not
        // a blind client retry, owns placement). Anything already placed or
        // terminal keeps the existing refusal.
        if (session.fleet_launch_id) {
          if (session.state !== 'queued' && session.state !== 'held') return response({ error: 'session_not_held' }, 409);
          return this.transaction(() => {
            const live = this.syncFleetPlacement(session.id) ?? session;
            return response(this.view(live));
          });
        }
        if (session.state !== 'held') return response({ error: 'session_not_held' }, 409);
        const original = parsed(session.request_json, {});
        const replay = Object.fromEntries(Object.entries(original).filter(([, value]) => value !== null));
        return this.handle(new Request('https://hub.internal/api/sessions', {
          method: 'POST', body: JSON.stringify({ ...replay, key: session.request_key }),
        }), auth);
      }

      if (sub === 'commands' && request.method === 'GET') {
        this.transaction(() => this.sweepSessionClaims(session.id));
        const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 50));
        const params: any[] = [session.id];
        const cursorSql = this.cursorClause(url.searchParams.get('cursor'), params, false);
        const rows = this.sql.exec(`SELECT * FROM session_control_commands WHERE session_id = ? ${cursorSql} ORDER BY created_at, id LIMIT ?`, ...params, limit + 1).toArray() as any[];
        const page = rows.slice(0, limit);
        const next = rows.length > limit ? `${page[page.length - 1].created_at}|${page[page.length - 1].id}` : null;
        return response({ session_id: session.id,
          commands: page.map((c: any) => {
            const { claim_token, holder_attempt, owner_token_id, owner_family, ack_json, host_status, host_reason, ...visible } = c;
            const ack = ack_json ? parsed(ack_json, null) : null;
            if (ack) { delete ack.claim_token; delete ack.holder_attempt; }
            const state = (c.state === 'claimed' && host_status) ? host_status : c.state;
            const reason = host_reason || ack?.reason || null;
            return {
              ...visible,
              state,
              ...(host_status ? { host_status } : {}),
              ...(reason ? { reason } : {}),
              payload: parsed(c.payload_json, null),
              ack
            };
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
        const freshEnqueue = (payload: any, allowed: Set<string>, err: string, warning?: string): Response => {
          return this.transaction(() => {
            const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
            if (!live || !this.ownerOk(live, caller)) return response({ error: 'not_found' }, 404);
            if (!allowed.has(live.state)) return response({ error: err }, 409);
            return this.enqueue(live, kind, actorName(caller), caller, input.key, payload, warning);
          });
        };
        if (kind === 'send') {
          if (!obj(input, ['key', 'body', 'require_ready']) || !label(input.key) ||
              typeof input.body !== 'string' || !input.body.trim() || input.body.length > 16000) {
            return response({ error: 'invalid_send' }, 400);
          }
          return this.transaction(() => {
            const live: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', session.id).toArray()[0];
            if (!live || !this.ownerOk(live, caller)) return response({ error: 'not_found' }, 404);
            if (!BOUND_STATES.has(live.state)) return response({ error: 'session_not_bound' }, 409);
            const binding = live.binding_json ? parsed(live.binding_json, null) : null;
            const reported: any = (live.machine_id && binding?.native_id) ? this.sql.exec(
              'SELECT agent_state, observed_at FROM fleet_sessions WHERE machine_id = ? AND native_id = ?',
              live.machine_id, binding.native_id
            ).toArray()[0] : null;

            // Recency (AC4): agent_state alone is not enough — a stale report
            // must warn/refuse. observed_at must parse, not be in the future,
            // and be within RECENT_STATE_MS of now.
            const observedMs = typeof reported?.observed_at === 'string' ? Date.parse(reported.observed_at) : NaN;
            const nowMs = this.now();
            const hasReported = Boolean(reported?.agent_state) && Number.isFinite(observedMs) &&
              observedMs <= nowMs + 60000 && nowMs - observedMs <= RECENT_STATE_MS;
            if (!hasReported && input.require_ready === true) {
              return response({ error: 'target_not_ready', message: 'target has no recent reported state' }, 409);
            }
            const warning = !hasReported ? 'target_no_reported_state' : undefined;
            return this.enqueue(live, kind, actorName(caller), caller, input.key, { body: input.body }, warning);
          });
        }
        if (kind === 'capture') {
          // Screen projection: `mode` ('tail'|'screen') and `screen` bool are
          // explicit shorthands for the same projection. Both are validated
          // strictly before any mutation; unknown fields are refused. The
          // stored payload always carries explicit `mode` (default 'tail') so
          // a lost ack/replay can never change the capture projection.
          if (!obj(input, ['key', 'max_bytes', 'mode', 'screen']) || !label(input.key) ||
              (input.max_bytes !== undefined && (!Number.isInteger(input.max_bytes) || input.max_bytes < 1 || input.max_bytes > 65536)) ||
              (input.mode !== undefined && input.mode !== 'tail' && input.mode !== 'screen') ||
              (input.screen !== undefined && typeof input.screen !== 'boolean')) {
            return response({ error: 'invalid_capture' }, 400);
          }
          if (input.mode !== undefined && input.screen !== undefined &&
              ((input.screen === true) !== (input.mode === 'screen'))) {
            return response({ error: 'invalid_capture' }, 400);
          }
          const mode = input.mode ?? (input.screen === true ? 'screen' : 'tail');
          return freshEnqueue({ max_bytes: input.max_bytes ?? 65536, mode }, BOUND_STATES, 'session_not_bound');
        }
        if (!obj(input, ['key']) || !label(input.key)) return response({ error: 'invalid_stop' }, 400);
        return freshEnqueue({}, new Set([...STOPPABLE, 'stopped']), 'session_not_stoppable');
      }
      return response({ error: 'not_found' }, 404);
    }

    if (path === '/api/session-commands/offers' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      return this.transaction(() => {
        const gate = this.hostOk({ machine_id: caller.machineId }, caller);
        if (!gate.ok) return response({ offers: [] });
        const at = this.at();
        expireUnclaimedStops(this.sql, this.now(), undefined, caller.machineId!);
        // Retry expired custody with the original execution fence. Only the
        // CURRENT bound family may age its own machine's claims: foreign or
        // superseded families neither read payloads nor mutate transport here.
        for (const stale of this.sql.exec(
          `SELECT c.id, c.claim_expires_at, s.host_family, s.machine_id FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
           WHERE s.machine_id = ? AND c.state IN ('claimed','recorded','transported') AND c.claim_token IS NOT NULL AND c.ack_json IS NULL AND c.holder_machine = ? AND (c.claim_expires_at IS NULL OR c.claim_expires_at <= ?) LIMIT 100`, caller.machineId!, caller.machineId!, at).toArray() as any[]) {
          const session = { machine_id: stale.machine_id, host_family: stale.host_family };
          if (!this.hostOk(session, caller).ok) continue;
          const full: any = this.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', stale.id).toArray()[0];
          if (full) this.expireClaim(full);
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
           WHERE s.machine_id = ? AND c.state = 'recorded'
           AND (? = 1 OR s.host_family IS NULL OR s.host_family = ?)
           ORDER BY CASE WHEN c.kind = 'stop' THEN 0 ELSE 1 END, c.created_at, c.id LIMIT 20`,
          caller.machineId!, caller.isAdmin === true ? 1 : 0, this.presentingFamily(caller)).toArray() as any[];
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
          const storedPayload: any = parsed(row.payload_json, null);
          // Capture projection persistence: the stored payload always carries
          // explicit `mode` (default 'tail' for legacy rows). The top-level
          // `mode` mirrors it so a lost ack/replay reads the same projection
          // from either field; other kinds carry no mode.
          const captureMode = row.kind === 'capture'
            ? (storedPayload?.mode === 'screen' ? 'screen' : 'tail')
            : null;
          offers.push({
            command_id: row.id, session_id: row.session_id, kind: row.kind,
            generation: row.generation,
            session_state: session.state, fleet_launch_id: session.fleet_launch_id ?? null,
            // Expected session engine for host-side masquerade refusal: the
            // receiver qualifies the live catalog row against it.
            engine: session.engine ?? null,
            mode: session.mode,
            // Server-owned cleanup-tree authorization (canonical offer
            // exposure): true ONLY for stop commands whose stored payload was
            // written by the cleanup --apply route. Ordinary stops carry
            // false; the key prefix is never read. The claim re-evaluates
            // eligibility and is the fence; this offer flag is visibility.
            cleanup_tree: this.cleanupStored(row),
            // Native send/capture targets need the exact bound tag + incarnation;
            // unbound sessions (stop on offered) carry binding null + fleet correlation.
            // native_created_at_ms is mandatory for host generation provenance:
            // the receiver refuses bound offers without it rather than guessing.
            binding: binding ? { native_id: binding.native_id, tag: binding.tag, workspace: binding.workspace, generation: binding.generation, native_created_at_ms: binding.native_created_at_ms ?? null } : null,
            native_tag: binding?.tag ?? null, workspace: requestJson.workspace ?? null,
            // Reserved for the future start adapter that consumes cwd; the hub
            // rejects non-root cwd today and never claims an executed cwd.
            cwd: requestJson.cwd ?? null,
            ...(captureMode !== null ? { mode: captureMode } : {}),
            payload: storedPayload, correlation: row.correlation,
          });
          if (offers.length >= 20) break;
        }
        return response({ offers });
      });
    }

    const statusMatch = /^\/api\/session-commands\/([a-f0-9-]{36})\/(status|progress)$/.exec(path);
    if (statusMatch && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['claim_token', 'status', 'reason']) || typeof input.claim_token !== 'string') {
        return response({ error: 'invalid_status' }, 400);
      }
      return this.transaction(() => {
        const row: any = this.sql.exec('SELECT * FROM session_control_commands WHERE id = ?', statusMatch[1]).toArray()[0];
        if (!row) return response({ error: 'not_found' }, 404);
        if (row.holder_machine !== caller.machineId || row.claim_token !== input.claim_token) {
          return response({ error: 'claim_conflict' }, 409);
        }
        const status = typeof input.status === 'string' ? input.status : 'waiting in inbox for recipient';
        const reason = typeof input.reason === 'string' ? input.reason.slice(0, 512) : null;
        this.sql.exec(
          'UPDATE session_control_commands SET host_status = ?, host_reason = ?, updated_at = ? WHERE id = ?',
          status, reason, this.at(), row.id
        );
        return response({ ok: true, command_id: row.id, status, reason });
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
        if (row.kind === 'stop' && !row.claim_token && Date.parse(row.created_at) <= this.now() - STOP_DELIVERY_MS) {
          expireUnclaimedStops(this.sql, this.now(), session.id);
          return response({ error: 'stop_host_unreachable' }, 409);
        }
        // Stored-cleanup fence: a cleanup --apply stop whose owner,
        // incarnation, or M15 eligibility drifted since enqueue MUST NOT
        // execute at all (neither tree nor native stop). Refuse the entire
        // claim; ordinary stops are unaffected and stay claimable. This sits
        // before the claimed-replay branch so even a replayed attempt cannot
        // authorize a stale cleanup (the host journal already prevents a
        // second native effect for a consumed attempt; a never-executed
        // attempt stays unexecuted).
        if (this.cleanupStored(row) && !this.cleanupAuthorized(row, session)) {
          return response({ error: 'cleanup_not_eligible' }, 409);
        }
        if (row.state === 'claimed') {
          if (this.claimExpired(row)) {
            this.expireClaim(row);
            return response({ error: 'claim_expired' }, 409);
          }
          if (row.holder_machine !== caller.machineId) return response({ error: 'claim_conflict' }, 409);
          // Same attester (journaled attempt) replays; any other attempt on
          // this machine is refused: one claim authorizes exactly one effect.
          // The grant carries holder + generation + both expiry forms so the
          // host receiver can feed verify_server_claim without inferring field
          // names: expires_at (ISO, canonical) and expires_at_ms (int, host clock).
          // cleanup_tree is the re-evaluated server-owned tree authorization
          // (owner/incarnation/M15 eligibility NOW, not at enqueue); the host
          // runs the tree ONLY when this is true plus a confirmed stop.
          if (row.holder_attempt === input.attempt_id) {
            return response({ command_id: row.id, claim_token: row.claim_token, holder: row.holder_machine, generation: row.generation, expires_at: row.claim_expires_at, expires_at_ms: Date.parse(row.claim_expires_at), attempt: row.attempts ?? 1, replay: true, cleanup_tree: this.cleanupAuthorized(row, session) });
          }
          return response({ error: 'already_claimed' }, 409);
        }
        if (!CLAIMABLE.has(row.state)) return response({ error: row.state === 'uncertain' ? 'command_uncertain' : 'invalid_transition' }, 409);
        if (row.claim_token && this.claimExpired(row)) {
          this.expireClaim(row);
          return response({ error: 'claim_expired' }, 409);
        }
        // A retry keeps the original execution fence. A different local
        // journal/attester must never repeat an effect whose outcome is lost.
        if (row.claim_token && (row.holder_machine !== caller.machineId || row.holder_attempt !== input.attempt_id)) {
          return response({ error: 'retry_requires_original_attempt' }, 409);
        }
        const token = row.claim_token || crypto.randomUUID();
        const expires = new Date(this.now() + this.claimTtlMs).toISOString();
        const attempt = row.claim_token ? row.attempts : (row.attempts ?? 0) + 1;
        const written = this.sql.exec(
          "UPDATE session_control_commands SET state = 'claimed', holder_machine = ?, claim_token = ?, claim_expires_at = ?, holder_attempt = ?, attempts = ?, updated_at = ? WHERE id = ? AND state IN ('recorded', 'transported')",
          caller.machineId, token, expires, input.attempt_id, attempt, this.at(), row.id);
        if (written.rowsWritten !== 1) return response({ error: 'claim_conflict' }, 409);
        this.adoptHostFamily(session.id, caller);
        return response({ command_id: row.id, claim_token: token, holder: caller.machineId, generation: row.generation, expires_at: expires, expires_at_ms: Date.parse(expires), attempt, cleanup_tree: this.cleanupAuthorized(row, session) });
      });
    }

    const ackMatch = /^\/api\/session-commands\/([a-f0-9-]{36})\/ack$/.exec(path);
    if (ackMatch && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      if (!obj(input, ['correlation', 'outcome', 'native', 'reason', 'capture', 'confirmed_exit', 'claim_token', 'proof', 'tree']) ||
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
          // Idempotent terminal failure (AC3): the server may have already
          // marked an inbox-waiting send failed/recipient_not_ready via
          // expireClaim after the host POSTed pending status. A host retry
          // with the same correlation + reason replays success (no new
          // effect) instead of 409 claim_required. expireClaim acks carry no
          // claim_token, so sameAck alone cannot match them.
          if (row.state === 'failed' && input.outcome === 'failed' && input.correlation === row.correlation) {
            const stored = row.ack_json ? parsed(row.ack_json, null) : null;
            if (stored && stored.outcome === 'failed' && stored.reason === input.reason) {
              return response({ command_id: row.id, state: row.state });
            }
          }
          if (row.state === 'uncertain' || (CLAIMABLE.has(row.state) && row.claim_token)) {
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
          // verdict must not depend on sweep ordering. A fenced terminal
          // failure receipt (failed/recipient_not_ready, or uncertain with an
          // inbox reason) on the SAME claim + correlation is accepted as
          // terminal instead of burning another lease — this is the host path
          // that reports an undelivered inbox envelope as failed only on
          // positive cancellation proof, else uncertain. Anything else stays
          // uncertain, never silently reissued or marked delivered.
          if (row.kind === 'send' && input.outcome === 'delivered') {
            const late = this.lateReplyAck(row, input, caller);
            if (late) return late;
          }
          if (row.kind === 'send' && (input.outcome === 'failed' || input.outcome === 'uncertain') &&
              input.correlation === row.correlation) {
            const binding = session.binding_json ? parsed(session.binding_json, null) : null;
            if (!binding || (input.native.native_id === binding.native_id && input.native.tag === binding.tag &&
                input.native.workspace === binding.workspace)) {
              const at = this.at();
              const fenced: any = {
                outcome: input.outcome, native: input.native, claim_attempt: row.attempts ?? 1,
                claim_token: input.claim_token,
                ...(row.holder_attempt ? { holder_attempt: row.holder_attempt } : {}),
                ...(typeof input.reason === 'string' ? { reason: input.reason } : {}),
              };
              const fencedState = input.outcome === 'failed' ? 'failed' : 'uncertain';
              this.sql.exec('UPDATE session_control_commands SET state = ?, ack_json = ?, updated_at = ? WHERE id = ?',
                fencedState, JSON.stringify(fenced), at, row.id);
              return response({ command_id: row.id, state: fencedState });
            }
          }
          this.expireClaim(row);
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
        // M15d owning-host tree outcome (STOP-bundled): the host reports what
        // IT did on the OWNING machine after the confirmed stop — derived from
        // the canonical binding workspace + HOST-config roots + live native
        // identity + discovered registered branch, never from CLI paths. The
        // server shape-validates and persists it in ack_json (surfaced via the
        // existing commands listing for CLI remote-observe); the stop verdict
        // itself never depends on it. Ownership is rechecked at this durable
        // ack: a drifted session owner converts the tree to kept
        // (ownership_changed), never a removal. Only a stored cleanup
        // command (cleanup --apply) may report a tree: an ordinary stop
        // reporting one is coerced to kept (not_cleanup_authorized).
        const TREE_STATES = new Set(['removed', 'kept', 'uncertain']);
        let tree: { state: string; reason: string } | null = null;
        if (input.tree !== undefined) {
          if (row.kind !== 'stop' || !obj(input.tree, ['state', 'reason']) ||
              typeof input.tree.state !== 'string' || !TREE_STATES.has(input.tree.state) ||
              typeof input.tree.reason !== 'string' || input.tree.reason.length > 300) {
            return response({ error: 'invalid_ack' }, 400);
          }
          tree = { state: input.tree.state, reason: input.tree.reason };
          if (!this.cleanupStored(row)) {
            tree = { state: 'kept', reason: `not_cleanup_authorized: ordinary stop never authorizes a tree (host reported ${input.tree.state})`.slice(0, 300) };
          } else if (session.actor !== row.actor) {
            tree = { state: 'kept', reason: `ownership_changed: session owner drifted since enqueue; listed-only (host reported ${input.tree.state})`.slice(0, 300) };
          } else if (!this.cleanupAuthorized(row, session)) {
            // Revocation/protection after a claim cannot undo an effect.
            // Preserve uncertainty if a host reports removal after authority
            // changed; never label that report a confirmed authorized cleanup.
            tree = { state: input.tree.state === 'removed' ? 'uncertain' : 'kept', reason: `cleanup_authority_changed: host reported ${input.tree.state}` };
          }
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
          ...(tree ? { tree } : {}),
        };
        const state = input.outcome === 'delivered' ? 'acked'
          : input.outcome === 'transported' ? 'transported'
          : input.outcome === 'failed' ? 'failed'
          : input.outcome === 'refused' ? (row.kind === 'send' && input.reason === 'send_unsupported_headless' ? 'failed' : 'refused') : 'uncertain';
        this.sql.exec('UPDATE session_control_commands SET state = ?, ack_json = ?, updated_at = ? WHERE id = ?', state, JSON.stringify(ack), at, row.id);
        if (state === 'acked' && row.kind === 'send' && input.proof) {
          this.sql.exec('INSERT INTO session_control_proofs (reply_envelope_id, command_id, session_id, created_at) VALUES (?, ?, ?, ?)',
            input.proof.reply_to_native_id, row.id, row.session_id, at);
        }
        if (state === 'acked' && row.kind === 'stop') {
          this.sql.exec("UPDATE session_control_sessions SET state = 'stopped', updated_at = ? WHERE id = ?", at, session.id);
          // The control binding can survive a lost launcher receipt while
          // Fleet.native_id remains unknown. The exact bound exit proof above
          // settles that linked reservation too; a conflicting native stays held.
          this.sql.exec("UPDATE fleet_launches SET state = 'exited', finished_at = ?, updated_at = ?, verification_status = 'verified' WHERE id = ? AND machine_id = ? AND (native_id = ? OR native_id IS NULL)", at, at, session.fleet_launch_id, session.machine_id, input.native.native_id);
          try {
            if (session.mode !== 'interactive') {
              const reqObj = parsed(session.request_json, {});
              recordSessionOutcomeReward(this.sql, {
                session_id: session.id,
                intake_task_id: reqObj?.intake_task_id ?? null,
                engine: session.engine ?? input.native?.engine ?? null,
                outcome: 'stopped',
              }, this.now());
            }
          } catch {}
        }
        return response({ command_id: row.id, state });
      });
    }

    return response({ error: 'not_found' }, 404);
  }

  private enqueue(session: any, kind: string, actor: string, caller: Caller, key: string, payload: any, warning?: string) {
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
    return response({ command_id: id, state: 'recorded', ...(warning ? { warning } : {}) }, 201);
  }
}

function actorName(caller: Caller): string {
  return caller.actor || caller.machineId || 'admin';
}

/** Adopt host-attested observed native sessions into canonical session control.
 *
 * Machine-bound credentials; unknown custody stays unknown; admin device
 * human authorization controls observed sessions. Normal Hub-started bindings
 * stay completely untouched. Absent records are NOT deleted or stopped.
 */
/** Repair legacy unbound intents from authoritative Fleet terminal receipts.
 * A null session machine is expected for a waitlisted launch. Match by launch
 * identity, never by that stale assignment. Bounded, convergent, and safe to
 * repeat: live/uncertain launches and bound sessions are never reaped. */
export function reconcileUnboundFleetSessions(sql: SqlStorage, nowMs: number): void {
  if (sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('fleet_launches','session_control_sessions')").toArray().length !== 2) return;
  const rows = sql.exec(`SELECT s.id, f.state FROM session_control_sessions s
    JOIN fleet_launches f ON f.id = s.fleet_launch_id
    WHERE s.binding_json IS NULL AND s.state IN ('queued','held','offered','starting','running','uncertain')
    AND f.state IN ('failed','exited','expired','cancelled') LIMIT 100`).toArray() as any[];
  for (const row of rows) {
    const nextState = row.state === 'exited' ? 'stopped' : row.state === 'expired' ? 'failed' : row.state;
    sql.exec('UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?',
      nextState, new Date(nowMs).toISOString(), row.id);
    try {
      const sRow: any = sql.exec('SELECT id, engine, mode, request_json FROM session_control_sessions WHERE id = ?', row.id).toArray()[0];
      if (sRow && sRow.mode !== 'interactive') {
        const reqObj = parsed(sRow.request_json, {});
        recordSessionOutcomeReward(sql, {
          session_id: sRow.id,
          intake_task_id: reqObj?.intake_task_id ?? null,
          engine: sRow.engine ?? reqObj?.engine ?? null,
          outcome: nextState as any,
        }, nowMs);
      }
    } catch {}
  }
}

export function adoptObservedNativeSessions(
  sql: SqlStorage,
  machineId: string,
  sessions: any[],
  nowMs: number,
  hostFamily?: string | null,
): { adopted: number; updated: number } {
  const hasTable = sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length > 0;
  if (!hasTable || !Array.isArray(sessions) || !machineId) return { adopted: 0, updated: 0 };

  let resolvedFamily = hostFamily;
  if (!resolvedFamily) {
    try {
      const m: any = sql.exec('SELECT c.family_id FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?', machineId).toArray()[0];
      if (m?.family_id) resolvedFamily = m.family_id;
    } catch {}
  }

  const TERMINAL_PHASES = new Set(['exited', 'dead', 'pruned', 'forgotten']);
  let adopted = 0;
  let updated = 0;
  const isoNow = new Date(nowMs).toISOString();

  for (const s of sessions) {
    if (!s || typeof s !== 'object') continue;
    const nativeId = s.native_id;
    if (typeof nativeId !== 'string' || !UUID_RE.test(nativeId)) continue;
    const tag = typeof s.tag === 'string' ? s.tag : '';
    const workspace = typeof s.workspace === 'string' ? s.workspace : '';
    if (!tag || !workspace) continue;

    // Validate native createdAt field
    let createdAtMs: number | null = null;
    if (typeof s.created_at_ms === 'number' && Number.isInteger(s.created_at_ms) && s.created_at_ms > 0) {
      createdAtMs = s.created_at_ms;
    }

    // Do not adopt if this native session is already bound to an active Fleet launch
    try {
      const hasLaunch = sql.exec('SELECT id FROM fleet_launches WHERE machine_id = ? AND native_id = ?', machineId, nativeId).toArray().length > 0;
      if (hasLaunch) continue;
    } catch {}

    // Check if an existing Hub-started session (with fleet_launch_id) manages this native_id
    const existing: any = sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', nativeId).toArray()[0];
    if (existing && existing.fleet_launch_id) {
      // Hub-started intent with exact nativeId: keep untouched
      continue;
    }

    // Check if any Hub-started session has binding_json pointing to this nativeId
    let hubManaged = false;
    try {
      for (const row of sql.exec('SELECT id, binding_json FROM session_control_sessions WHERE fleet_launch_id IS NOT NULL AND machine_id = ?', machineId).toArray() as any[]) {
        try {
          const b = JSON.parse(row.binding_json || '{}');
          if (b.native_id === nativeId) {
            hubManaged = true;
            break;
          }
        } catch {}
      }
    } catch {}
    if (hubManaged) continue;

    const verifiedTerminal = TERMINAL_PHASES.has(s.phase) && s.confirmed_exit === true;
    const state = verifiedTerminal ? 'stopped' : (s.phase === 'starting' ? 'starting' : s.phase === 'running' ? 'running' : 'uncertain');
    const createdAtIso = createdAtMs ? new Date(createdAtMs).toISOString() : isoNow;
    const requestName = typeof s.name === 'string' && s.name ? s.name : null;
    const requestOwner = typeof s.owner === 'string' && s.owner ? s.owner : null;
    const actor = requestOwner || 'unknown';

    const requestPayload = {
      provenance: 'observed_native',
      origin: 'aplexer',
      workspace,
      cwd: typeof s.cwd === 'string' ? s.cwd : null,
      engine: typeof s.engine === 'string' ? s.engine : null,
      tag,
      name: requestName,
      owner: requestOwner,
      created_at_ms: createdAtMs,
    };

    const bindingPayload = {
      native_id: nativeId,
      tag,
      workspace,
      generation: 1,
      native_created_at_ms: createdAtMs,
      provenance: 'observed_native',
      adopted_at: isoNow,
    };

    const sessionType = (s.session_type === 'headless' || s.mode === 'headless') ? 'headless' : 'aplexer';
    const parentId = typeof s.parent_id === 'string' && s.parent_id ? s.parent_id : null;
    const parentSource = parentId ? 'inferred' : 'unknown_external';

    if (!existing) {
      // Insert new adopted session
      const requestKey = 'native-' + nativeId;
      sql.exec(
        'INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, binding_json, parent_id, parent_source, session_type, owner_token_id, owner_family, host_family, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        nativeId,
        actor,
        requestKey,
        JSON.stringify(requestPayload),
        null,
        machineId,
        typeof s.engine === 'string' ? s.engine : null,
        sessionType === 'headless' ? 'headless' : 'interactive',
        state,
        JSON.stringify(bindingPayload),
        parentId,
        parentSource,
        sessionType,
        null,
        null,
        resolvedFamily || null,
        createdAtIso,
        isoNow,
      );
      adopted++;
    } else {
      refreshObservedHostFamily(sql, machineId, existing, s, resolvedFamily);
      // Update existing adopted session if state or phase changed
      let shouldUpdate = false;
      let nextState = existing.state;
      if (verifiedTerminal && existing.state !== 'stopped') {
        nextState = 'stopped';
        shouldUpdate = true;
      } else if (!verifiedTerminal && existing.state !== 'stopped' && existing.state !== state) {
        nextState = state;
        shouldUpdate = true;
      }
      if (shouldUpdate) {
        sql.exec(
          'UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE id = ?',
          nextState,
          isoNow,
          nativeId,
        );
        updated++;
      }
      if (parentId && !existing.parent_id) {
        sql.exec('UPDATE session_control_sessions SET parent_id = ?, parent_source = ? WHERE id = ?', parentId, parentSource, nativeId);
      }
    }
  }

  return { adopted, updated };
}
