import { AUTOMATION_SCHEMA, AUTOMATION_REVOKE_TRIGGER, AUTOMATION_SENDER, type BacklogQuery, type FollowUpIntent } from './conversation-automation';
import { ConversationLinksService } from './conversation-links';
import { credentialActor, validRegistryName, registryAddress } from './agent_registry';
import {
  SqlStorage,
  CallerContext,
  generateUlid,
  isRootRoleAddress,
  RETENTION_CLEANUP_CURSOR_SQL,
  runFairRetentionPass,
} from './bus';
import { Env } from './env';
import {
  SessionData,
  getSession,
  verifyCsrf,
  TOKEN_ADMIN_EMAIL,
} from './auth/session';
import { sha256 } from './auth/crypto';

// Shared by archived discovery, candidate selection and the final header guard.
// An uncertain effect pins its parent even when only the frozen intent names it.
const ARCHIVED_PARENT_UNBLOCKED_SQL = `
  NOT EXISTS (SELECT 1 FROM conversation_automation_grants g WHERE g.conversation_id=c.id)
  AND NOT EXISTS (SELECT 1 FROM conversation_automation_receipts r WHERE r.conversation_id=c.id)
  AND NOT EXISTS (SELECT 1 FROM hub_check_slots s WHERE s.state!='done' AND (
    json_extract(s.check_json, '$.action.conversation_id')=c.id
    OR CASE WHEN json_valid(s.intent_json) THEN json_extract(s.intent_json, '$.conversationId') END=c.id
  ))`;
const ARCHIVED_PARENT_WORK_SQL = `c.archived_at IS NOT NULL AND c.archived_at<=? AND (
  EXISTS (SELECT 1 FROM conversation_messages m WHERE m.conversation_id=c.id)
  OR EXISTS (SELECT 1 FROM conversation_members m WHERE m.conversation_id=c.id)
  OR EXISTS (SELECT 1 FROM conversation_links l WHERE l.conversation_id=c.id)
  OR (${ARCHIVED_PARENT_UNBLOCKED_SQL})
)`;

export const CONVERSATIONS_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  created_by TEXT NOT NULL,
  idempotency_key TEXT,
  request_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  archived_by TEXT,
  last_seq INTEGER NOT NULL DEFAULT 0,
  last_message_at TEXT,
  last_message_preview TEXT,
  retained_message_count INTEGER NOT NULL DEFAULT 0,
  retained_message_bytes INTEGER NOT NULL DEFAULT 0,
  removed_member_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(created_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_archived ON conversations(archived_at, id);

CREATE TABLE IF NOT EXISTS conversation_members (
  conversation_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  role TEXT NOT NULL,
  added_at TEXT NOT NULL,
  added_by TEXT NOT NULL,
  removed_at TEXT,
  removed_by TEXT,
  last_read_seq INTEGER NOT NULL DEFAULT 0,
  last_read_at TEXT,
  PRIMARY KEY(conversation_id, actor)
);
CREATE INDEX IF NOT EXISTS idx_conversation_members_actor ON conversation_members(actor, removed_at, conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversation_members_removed ON conversation_members(removed_at, conversation_id, actor);

CREATE TABLE IF NOT EXISTS conversation_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  sender_actor TEXT NOT NULL,
  sender_kind TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  body_bytes INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL,
  request_json TEXT NOT NULL,
  reply_to TEXT,
  reply_to_seq INTEGER,
  bridge_ref TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(conversation_id, seq),
  UNIQUE(conversation_id, sender_actor, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_seq ON conversation_messages(conversation_id, seq ASC);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_created ON conversation_messages(created_at, id);

CREATE TABLE IF NOT EXISTS conversation_storage_usage (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  conversation_count INTEGER NOT NULL DEFAULT 0,
  message_count INTEGER NOT NULL DEFAULT 0,
  message_bytes INTEGER NOT NULL DEFAULT 0,
  removed_member_count INTEGER NOT NULL DEFAULT 0,
  schema_version INTEGER NOT NULL DEFAULT 0
);
` + AUTOMATION_SCHEMA;

export const CONVERSATIONS_SCHEMA_VERSION = 2;

export const MAX_HTTP_BODY_BYTES = 32768;
export const MAX_MESSAGE_BODY_BYTES = 16384;
export const MAX_TITLE_CHARS = 120;
export const MAX_CONVERSATION_MEMBERS = 64;
export const MAX_PAGE_LIMIT = 100;
export const DEFAULT_PAGE_LIMIT = 50;

export const DEFAULT_MAX_TOTAL_CONVERSATIONS = 1_000;
export const DEFAULT_MAX_MESSAGES_PER_CONVERSATION = 5_000;
export const DEFAULT_MAX_TOTAL_MESSAGES = 50_000;
export const DEFAULT_MAX_BYTES_PER_CONVERSATION = 8 * 1024 * 1024;
export const DEFAULT_MAX_TOTAL_MESSAGE_BYTES = 64 * 1024 * 1024;
export const DEFAULT_MAX_REMOVED_MEMBERS_PER_CONVERSATION = 128;
export const DEFAULT_MAX_TOTAL_REMOVED_MEMBERS = 2_048;
export const DEFAULT_MESSAGE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
export const DEFAULT_REMOVED_MEMBER_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
export const DEFAULT_ARCHIVED_CONVERSATION_RETENTION_MS =
  30 * 24 * 60 * 60 * 1000;
export const DEFAULT_RETENTION_BATCH_LIMIT = 100;

const SAFE_KEY_REGEX = /^[A-Za-z0-9._:-]{1,128}$/;
const HUMAN_SUB_REGEX = /^[A-Za-z0-9._:-]{1,120}$/;
const AGENT_SEGMENT_REGEX = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const RESERVED_AGENT_NAMESPACE_REGEX =
  /^(human|role|machine|routing-client|admin|root|principal|system|hub|schedule)$/i;
const RESERVED_AGENT_PREFIX_REGEX = /^(human|role|machine|routing-client):/i;
const CONTROL_CHARS_REGEX = /[\x00-\x1f\x7f]/;
const ALLOWED_MESSAGE_KINDS = new Set([
  'message',
  'note',
  'reply',
  'status',
  'handoff',
  'action',
]);
const FORBIDDEN_SENDER_FIELDS = new Set([
  'from',
  'from_actor',
  'sender',
  'sender_actor',
  'author',
]);

const RESPONSE_HEADERS = {
  'Cache-Control': 'private, no-store',
  'X-Content-Type-Options': 'nosniff',
};

function parseSupportedSchemaVersion(raw: unknown): number | null {
  if (raw === null || raw === undefined) return 0;
  let n: number;
  if (typeof raw === 'number') {
    n = raw;
  } else if (typeof raw === 'bigint') {
    if (raw < 0n || raw > BigInt(Number.MAX_SAFE_INTEGER)) return null;
    n = Number(raw);
  } else if (typeof raw === 'string') {
    const trimmed = raw.trim();
    if (!/^[0-9]+$/.test(trimmed)) return null;
    n = Number(trimmed);
  } else {
    return null;
  }
  if (!Number.isInteger(n) || n < 0 || !Number.isFinite(n)) return null;
  return n;
}

function jsonResponse(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: RESPONSE_HEADERS });
}

export class ConversationDomainError extends Error {
  public readonly code: string;
  public readonly status: number;
  public readonly reason?: string;
  public readonly extra?: Record<string, unknown>;

  constructor(
    code: string,
    status: number,
    reason?: string,
    extra?: Record<string, unknown>,
  ) {
    super(reason || code);
    this.name = 'ConversationDomainError';
    this.code = code;
    this.status = status;
    this.reason = reason;
    this.extra = extra;
  }

  toResponse(): Response {
    const body: Record<string, unknown> = { error: this.code };
    if (this.reason && this.reason !== this.code) {
      body.reason = this.reason;
    }
    if (this.extra) {
      Object.assign(body, this.extra);
    }
    return jsonResponse(body, this.status);
  }
}

export interface NativeAgentBinding {
  tag: string;
  project: string | null;
  machine_id: string | null;
  workspace: string | null;
  session_id: string | null;
}

export interface ValidatedParticipant {
  actor: string;
  kind: 'human' | 'agent';
  display_name: string;
  project?: string | null;
  role?: string | null;
  machine_id?: string | null;
  native_binding?: NativeAgentBinding | null;
}

export interface VerifiedConversationPrincipal {
  actor: string;
  kind: 'human' | 'agent';
  authType: 'session' | 'bearer';
  scopes: string[];
  csrfVerified: boolean;
  displayName?: string;
  sessionSub?: string;
  tokenId?: string;
  nativeBinding?: NativeAgentBinding | null;
}

export interface IdentityValidator {
  resolveParticipant(
    rawActor: string,
    callerPrincipal?: VerifiedConversationPrincipal,
  ): ValidatedParticipant | null;
  listParticipants(
    callerPrincipal?: VerifiedConversationPrincipal,
  ): ValidatedParticipant[];
}

export interface ConversationAuthInput {
  caller?: CallerContext;
  session?: SessionData | null;
  scopes?: string[];
  authType?: 'session' | 'bearer';
  csrfVerified?: boolean;
  error?: Response;
}

export interface CreateConversationInput {
  kind: 'direct' | 'group';
  title: string;
  members?: string[];
  idempotencyKey?: string;
}

export interface SendConversationMessageInput {
  conversationId: string;
  body: string;
  idempotencyKey: string;
  kind?: string;
  replyTo?: string;
  replyToSeq?: number;
  bridgeRef?: string;
}

export interface ListMessagesOptions {
  afterSeq?: number;
  beforeSeq?: number;
  limit?: number;
}

export interface ListConversationsOptions {
  state?: 'active' | 'archived' | 'all';
  includeArchived?: boolean;
  limit?: number;
  cursor?: string;
}

export interface ConversationStorageLimits {
  maxTotalConversations: number;
  maxMessagesPerConversation: number;
  maxTotalMessages: number;
  maxBytesPerConversation: number;
  maxTotalMessageBytes: number;
  maxRemovedMembersPerConversation: number;
  maxTotalRemovedMembers: number;
  messageRetentionMs: number;
  removedMemberRetentionMs: number;
  archivedConversationRetentionMs: number;
}

export interface ConversationStorageUsage {
  conversation_count: number;
  message_count: number;
  message_bytes: number;
  removed_member_count: number;
}

export interface ConversationRetentionResult {
  conversationsDeleted: number;
  messagesDeleted: number;
  removedMembersDeleted: number;
  membersDeleted: number;
  automationRowsDeleted: number;
  linksDeleted?: number;
  rowsDeleted: number;
  hasMore: boolean;
}

/** Child join-table handler owned by ConversationLinksService. */
export interface ConversationLinksHandler {
  attach(
    auth: ConversationAuthInput,
    conversationId: string,
    input: { kind?: unknown; ref?: unknown; idempotencyKey?: unknown },
  ): Record<string, unknown>;
  list(
    auth: ConversationAuthInput,
    conversationId: string,
    options?: { limit?: number; cursor?: string | null },
  ): Record<string, unknown>;
  getLink(
    auth: ConversationAuthInput,
    conversationId: string,
    linkId: string,
  ): Record<string, unknown>;
  remove(
    auth: ConversationAuthInput,
    conversationId: string,
    linkId: string,
  ): Record<string, unknown>;
  reverse(
    auth: ConversationAuthInput,
    options: {
      kind?: string | null;
      ref?: string | null;
      limit?: number;
      cursor?: string | null;
    },
  ): Record<string, unknown>;
  hasExpiredRows(nowMs: number): boolean;
  hasRowsForConversation(conversationId: string): boolean;
  deleteChunkForConversation(conversationId: string, limit: number): number;
  retentionCleanup(
    nowMs: number,
    limit: number,
  ): { rowsDeleted: number; hasMore: boolean };
}

/**
 * Encode a raw verified SessionData.sub into a namespaced canonical human actor `human:<sub>`.
 * Injective: never trims whitespace and never strips an existing `human:` prefix, so distinct
 * verified raw subjects (`"alice"` vs `"human:alice"` vs `" alice"`) never collide.
 * Rejects bare `'admin'`, `'root'`, `'principal'`, whitespace, or malformed subjects.
 */
export function formatHumanActorFromSub(sub: unknown): string | null {
  if (typeof sub !== 'string' || !sub) return null;
  if (!HUMAN_SUB_REGEX.test(sub)) return null;
  if (
    /^(admin|root|principal)$/i.test(sub) ||
    /^human:(admin|root|principal)$/i.test(sub)
  ) {
    return null;
  }
  return `human:${sub}`;
}

/**
 * Parse a canonical human actor address (`human:<sub_id>`) into its canonical actor and raw `sub`.
 * Separate from raw-subject encoding: requires an exact leading `human:` prefix and validates the
 * remaining raw subject injectively via `formatHumanActorFromSub`.
 */
export function parseCanonicalHumanActor(
  rawActor: unknown,
): { actor: string; sub: string } | null {
  if (typeof rawActor !== 'string' || !rawActor.startsWith('human:')) {
    return null;
  }
  const sub = rawActor.slice('human:'.length);
  const encoded = formatHumanActorFromSub(sub);
  if (!encoded || encoded !== rawActor) {
    return null;
  }
  return { actor: encoded, sub };
}

export function isValidAgentSegment(seg: unknown): seg is string {
  return validRegistryName(seg);
}

export function isDisjointAgentActor(actor: unknown): actor is string {
  return typeof actor === 'string' && registryAddress(actor) !== null;
}

/**
 * Default SQLite-backed IdentityValidator.
 * Validates membership targets strictly against:
 * 1. Registered agents in `agents` (excluding `root` role and reserved namespaces).
 * 2. Registered humans in `users` (`human:<sub_id>`).
 * 3. The currently authenticated human caller's own verified `SessionData.sub`.
 */
export function createSqlIdentityValidator(sql: SqlStorage): IdentityValidator {
  const toAgentParticipant = (row: any): ValidatedParticipant | null => {
    if (!row || typeof row.tag !== 'string') return null;
    if (typeof row.role === 'string' && /^root/i.test(row.role.trim())) {
      return null;
    }
    const tag = row.tag;
    if (!isValidAgentSegment(tag)) {
      return null;
    }

    let project: string | null = null;
    if (row.project !== null && row.project !== undefined && row.project !== '') {
      if (typeof row.project !== 'string' || !isValidAgentSegment(row.project)) {
        return null;
      }
      project = row.project;
    }

    // Require project-qualified canonical identity whenever project is present;
    // bare tag is used only for projectless agent registrations.
    const canonicalActor = project ? `${project}:${tag}` : tag;
    if (!isDisjointAgentActor(canonicalActor)) {
      return null;
    }

    const registrationMetadata: NativeAgentBinding = {
      tag,
      project,
      machine_id: row.machine_id ?? null,
      workspace: row.workspace ?? null,
      session_id: row.session_id ?? null,
    };

    return {
      actor: canonicalActor,
      kind: 'agent',
      display_name: canonicalActor,
      project,
      role: row.role ?? null,
      machine_id: row.machine_id ?? null,
      native_binding: registrationMetadata,
    };
  };

  const resolveAgentRow = (rawActor: string): ValidatedParticipant | null => {
    if (
      typeof rawActor !== 'string' ||
      !rawActor ||
      rawActor.length > 128 ||
      rawActor !== rawActor.trim() ||
      isRootRoleAddress(rawActor) ||
      RESERVED_AGENT_PREFIX_REGEX.test(rawActor) ||
      RESERVED_AGENT_NAMESPACE_REGEX.test(rawActor)
    ) {
      return null;
    }
    const parts = rawActor.split(':');
    let rows: any[] = [];
    try {
      if (parts.length === 1) {
        if (!isValidAgentSegment(parts[0])) return null;
        rows = sql
          .exec('SELECT * FROM agents WHERE tag = ?', parts[0])
          .toArray();
      } else if (parts.length === 2) {
        if (!isValidAgentSegment(parts[0]) || !isValidAgentSegment(parts[1])) {
          return null;
        }
        rows = sql
          .exec(
            'SELECT * FROM agents WHERE project = ? AND tag = ?',
            parts[0],
            parts[1],
          )
          .toArray();
      } else {
        return null;
      }
    } catch {
      return null;
    }

    if (rows.length !== 1) return null;
    return toAgentParticipant(rows[0]);
  };

  return {
    resolveParticipant(
      rawActor: string,
      callerPrincipal?: VerifiedConversationPrincipal,
    ): ValidatedParticipant | null {
      if (
        typeof rawActor !== 'string' ||
        !rawActor ||
        rawActor.length > 128 ||
        rawActor !== rawActor.trim()
      ) {
        return null;
      }

      if (RESERVED_AGENT_PREFIX_REGEX.test(rawActor)) {
        if (!rawActor.startsWith('human:')) {
          return null;
        }
        const parsedHuman = parseCanonicalHumanActor(rawActor);
        if (!parsedHuman) return null;
        const { actor: canonicalHuman, sub } = parsedHuman;

        try {
          const userRow = sql
            .exec('SELECT sub, email FROM users WHERE sub = ?', sub)
            .toArray()[0] as any;
          if (userRow) {
            return {
              actor: canonicalHuman,
              kind: 'human',
              display_name: userRow.email || canonicalHuman,
              native_binding: null,
            };
          }
        } catch {}

        if (
          callerPrincipal &&
          callerPrincipal.kind === 'human' &&
          callerPrincipal.actor === canonicalHuman
        ) {
          return {
            actor: canonicalHuman,
            kind: 'human',
            display_name: callerPrincipal.displayName || canonicalHuman,
            native_binding: null,
          };
        }

        return null;
      }

      return resolveAgentRow(rawActor);
    },

    listParticipants(
      callerPrincipal?: VerifiedConversationPrincipal,
    ): ValidatedParticipant[] {
      const byActor = new Map<string, ValidatedParticipant>();

      if (callerPrincipal && callerPrincipal.kind === 'human') {
        const parsedSelf = parseCanonicalHumanActor(callerPrincipal.actor);
        if (parsedSelf) {
          byActor.set(parsedSelf.actor, {
            actor: parsedSelf.actor,
            kind: 'human',
            display_name: callerPrincipal.displayName || parsedSelf.actor,
            native_binding: null,
          });
        }
      }

      try {
        const users = sql
          .exec('SELECT sub, email FROM users ORDER BY sub ASC LIMIT 200')
          .toArray() as any[];
        for (const u of users) {
          const actor = formatHumanActorFromSub(u.sub);
          if (!actor) continue;
          byActor.set(actor, {
            actor,
            kind: 'human',
            display_name: u.email || actor,
            native_binding: null,
          });
        }
      } catch {}

      try {
        const agents = sql
          .exec('SELECT * FROM agents ORDER BY tag ASC LIMIT 500')
          .toArray() as any[];
        for (const a of agents) {
          const participant = toAgentParticipant(a);
          if (!participant) continue;
          byActor.set(participant.actor, participant);
        }
      } catch {}

      return Array.from(byActor.values());
    },
  };
}

/**
 * Production route authentication adapter for `/api/conversations`.
 * - Uses `getSession(env, request)` for human admin sessions, binding identity strictly to
 *   `SessionData.sub` (`human:<sub_id>`) and computing `csrfVerified = verifyCsrf(request, session)`.
 *   Token-admin sessions share their deterministic `tokenAdminSubject(hash)` identity.
 * - Uses `credentials` table for `kind = 'agent'` bearer tokens.
 * - Never collapses browser sessions to `'admin'` and never trusts unverified request headers.
 */
export async function authenticateConversationRequest(
  env: Env,
  sql: SqlStorage,
  request: Request,
  now: () => number = Date.now,
): Promise<ConversationAuthInput> {
  const session = await getSession(env, request);
  if (session) {
    if (!session.isAdmin) {
      return { error: jsonResponse({ error: 'forbidden' }, 403) };
    }
    const humanActor = formatHumanActorFromSub(session.sub);
    if (!humanActor) {
      return {
        error: jsonResponse(
          { error: 'forbidden', reason: 'invalid_session_subject' },
          403,
        ),
      };
    }
    const csrfVerified = verifyCsrf(request, session);
    const isMutating = !['GET', 'HEAD', 'OPTIONS'].includes(
      request.method.toUpperCase(),
    );
    if (!isMutating || csrfVerified) {
      const nowIso = new Date(now()).toISOString();
      try {
        const existing = sql
          .exec('SELECT sub FROM users WHERE sub = ?', session.sub)
          .toArray()[0];
        if (!existing) {
          sql.exec(
            'INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)',
            session.sub,
            session.email || TOKEN_ADMIN_EMAIL,
            1,
            nowIso,
            nowIso,
          );
        }
      } catch {}
    }

    return {
      session,
      caller: {
        actor: humanActor,
        kind: 'human',
        isAdmin: true,
        scopes: ['bus:read', 'bus:send'],
      },
      scopes: ['bus:read', 'bus:send'],
      authType: 'session',
      csrfVerified,
    };
  }

  const authHeader = request.headers.get('Authorization');
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return {
      error: new Response(JSON.stringify({ error: 'unauthorized' }), {
        status: 401,
        headers: {
          ...RESPONSE_HEADERS,
          'Content-Type': 'application/json; charset=utf-8',
          'WWW-Authenticate': 'Bearer',
        },
      }),
    };
  }

  const token = authHeader.slice(7).trim();
  if (!token || token.length > 512 || /\s/.test(token)) {
    return {
      error: new Response(JSON.stringify({ error: 'unauthorized' }), {
        status: 401,
        headers: {
          ...RESPONSE_HEADERS,
          'Content-Type': 'application/json; charset=utf-8',
          'WWW-Authenticate': 'Bearer',
        },
      }),
    };
  }

  const tokenHash = await sha256(token);
  const cred = sql
    .exec(
      'SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL',
      tokenHash,
    )
    .toArray()[0] as any;
  if (!cred) {
    return {
      error: new Response(JSON.stringify({ error: 'unauthorized' }), {
        status: 401,
        headers: {
          ...RESPONSE_HEADERS,
          'Content-Type': 'application/json; charset=utf-8',
          'WWW-Authenticate': 'Bearer',
        },
      }),
    };
  }

  const nowMs = now();
  const expiresMs = Date.parse(cred.expires_at);
  if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) {
    return {
      error: new Response(JSON.stringify({ error: 'unauthorized' }), {
        status: 401,
        headers: {
          ...RESPONSE_HEADERS,
          'Content-Type': 'application/json; charset=utf-8',
          'WWW-Authenticate': 'Bearer',
        },
      }),
    };
  }

  let scopes: string[] = [];
  try {
    scopes =
      typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : cred.scopes;
  } catch {
    return { error: jsonResponse({ error: 'unauthorized' }, 401) };
  }
  if (!Array.isArray(scopes)) {
    return { error: jsonResponse({ error: 'unauthorized' }, 401) };
  }

  const lastUsedMs = cred.last_used_at ? Date.parse(cred.last_used_at) : 0;
  if (!lastUsedMs || isNaN(lastUsedMs) || nowMs - lastUsedMs >= 300_000) {
    const nowIso = new Date(nowMs).toISOString();
    try {
      sql.exec('UPDATE credentials SET last_used_at = ? WHERE id = ?', nowIso, cred.id);
    } catch {}
  }

  return {
    caller: {
      kind: cred.kind,
      actor: cred.kind === 'agent' ? credentialActor(sql as any, cred) ?? undefined : cred.actor || cred.subject,
      machineId: cred.machine_id ?? undefined,
      jobId: cred.kind === 'job' ? cred.subject : undefined,
      tokenId: cred.id,
      tokenHash: cred.token_hash,
      scopes,
    },
    scopes,
    authType: 'bearer',
    csrfVerified: true,
  };
}

async function readBoundedJsonBody(
  request: Request,
  optional = false,
): Promise<{ data?: Record<string, any>; error?: Response }> {
  if (!request.body) {
    if (optional) return { data: {} };
    return { error: jsonResponse({ error: 'malformed_json' }, 400) };
  }
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      totalBytes += part.value.byteLength;
      if (totalBytes > MAX_HTTP_BODY_BYTES) {
        await reader.cancel();
        return { error: jsonResponse({ error: 'body_too_large' }, 413) };
      }
      chunks.push(part.value);
    }
  } catch {
    return { error: jsonResponse({ error: 'malformed_json' }, 400) };
  } finally {
    reader.releaseLock();
  }

  if (totalBytes === 0) {
    if (optional) return { data: {} };
    return { error: jsonResponse({ error: 'malformed_json' }, 400) };
  }

  const allBytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    allBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }

  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(
      allBytes,
    );
  } catch {
    return { error: jsonResponse({ error: 'invalid_utf8' }, 400) };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: jsonResponse({ error: 'malformed_json' }, 400) };
  }

  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { error: jsonResponse({ error: 'invalid_json_object' }, 400) };
  }

  return { data: parsed as Record<string, any> };
}

export class ConversationsService {
  public readonly sql: SqlStorage;
  private readonly txSync: <T>(fn: () => T) => T;
  private readonly now: () => number;
  public identityValidator: IdentityValidator;
  public limits: ConversationStorageLimits;
  public linksHandler: ConversationLinksService;

  constructor(
    sql: SqlStorage,
    txSync?: <T>(fn: () => T) => T,
    options?: {
      now?: () => number;
      identityValidator?: IdentityValidator;
      limits?: Partial<ConversationStorageLimits>;
    },
  ) {
    this.sql = sql;
    // Child link calls join this service's current transaction, including when
    // used directly with DurableObjectStorage.transactionSync (no nested BEGIN).
    let depth = 0;
    this.txSync = <T>(fn: () => T): T => {
      if (!txSync || depth > 0) return fn();
      depth++;
      try { return txSync(fn); } finally { depth--; }
    };
    this.now = options?.now ?? Date.now;
    this.identityValidator =
      options?.identityValidator ?? createSqlIdentityValidator(sql);
    this.limits = {
      maxTotalConversations:
        options?.limits?.maxTotalConversations ??
        DEFAULT_MAX_TOTAL_CONVERSATIONS,
      maxMessagesPerConversation:
        options?.limits?.maxMessagesPerConversation ??
        DEFAULT_MAX_MESSAGES_PER_CONVERSATION,
      maxTotalMessages:
        options?.limits?.maxTotalMessages ?? DEFAULT_MAX_TOTAL_MESSAGES,
      maxBytesPerConversation:
        options?.limits?.maxBytesPerConversation ??
        DEFAULT_MAX_BYTES_PER_CONVERSATION,
      maxTotalMessageBytes:
        options?.limits?.maxTotalMessageBytes ?? DEFAULT_MAX_TOTAL_MESSAGE_BYTES,
      maxRemovedMembersPerConversation:
        options?.limits?.maxRemovedMembersPerConversation ??
        DEFAULT_MAX_REMOVED_MEMBERS_PER_CONVERSATION,
      maxTotalRemovedMembers:
        options?.limits?.maxTotalRemovedMembers ??
        DEFAULT_MAX_TOTAL_REMOVED_MEMBERS,
      messageRetentionMs:
        options?.limits?.messageRetentionMs ?? DEFAULT_MESSAGE_RETENTION_MS,
      removedMemberRetentionMs:
        options?.limits?.removedMemberRetentionMs ??
        DEFAULT_REMOVED_MEMBER_RETENTION_MS,
      archivedConversationRetentionMs:
        options?.limits?.archivedConversationRetentionMs ??
        DEFAULT_ARCHIVED_CONVERSATION_RETENTION_MS,
    };
    this.linksHandler = new ConversationLinksService(sql, this, this.txSync);
  }

  public getNow(): number {
    return this.now();
  }

  public init(): void {
    this.runInTxn(() => {
      for (const stmt of CONVERSATIONS_SCHEMA_SQL.split(';')) {
        const trimmed = stmt.trim();
        if (trimmed) {
          this.sql.exec(trimmed);
        }
      }
      this.sql.exec(AUTOMATION_REVOKE_TRIGGER);
      this.linksHandler.init();
      for (const alterStmt of [
        'ALTER TABLE conversations ADD COLUMN retained_message_count INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE conversations ADD COLUMN retained_message_bytes INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE conversations ADD COLUMN removed_member_count INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE conversation_messages ADD COLUMN body_bytes INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE conversation_storage_usage ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 0',
      ]) {
        try {
          this.sql.exec(alterStmt);
        } catch {}
      }

      const usageRow = this.sql
        .exec(
          'SELECT singleton, schema_version FROM conversation_storage_usage WHERE singleton = 1',
        )
        .toArray()[0] as any;
      const currentVersion = usageRow
        ? parseSupportedSchemaVersion(usageRow.schema_version)
        : 0;
      if (
        currentVersion === null ||
        currentVersion > CONVERSATIONS_SCHEMA_VERSION
      ) {
        throw new ConversationDomainError(
          'unsupported_schema_version',
          500,
          'unsupported_conversation_schema_version',
        );
      }

      if (!usageRow || currentVersion < CONVERSATIONS_SCHEMA_VERSION) {
        // Clean up any orphaned child rows without a parent conversation header
        this.sql.exec(
          'DELETE FROM conversation_messages WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_messages.conversation_id)',
        );
        this.sql.exec(
          'DELETE FROM conversation_members WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_members.conversation_id)',
        );

        // Backfill per-message UTF-8 byte lengths for rows migrated from original schema
        this.sql.exec(
          `UPDATE conversation_messages
           SET body_bytes = LENGTH(CAST(body AS BLOB))
           WHERE body_bytes <= 0 AND LENGTH(CAST(body AS BLOB)) > 0`,
        );

        // Reconcile per-conversation message counts, byte totals, removed-member counts, and previews
        this.sql.exec(
          `UPDATE conversations
           SET retained_message_count = COALESCE((
                 SELECT COUNT(*)
                 FROM conversation_messages m
                 WHERE m.conversation_id = conversations.id
               ), 0),
               retained_message_bytes = COALESCE((
                 SELECT SUM(CASE WHEN m.body_bytes > 0 THEN m.body_bytes ELSE LENGTH(CAST(m.body AS BLOB)) END)
                 FROM conversation_messages m
                 WHERE m.conversation_id = conversations.id
               ), 0),
               removed_member_count = COALESCE((
                 SELECT COUNT(*)
                 FROM conversation_members cm
                 WHERE cm.conversation_id = conversations.id AND cm.removed_at IS NOT NULL
               ), 0),
               last_message_preview = (
                 SELECT SUBSTR(TRIM(REPLACE(REPLACE(REPLACE(m.body, CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')), 1, 160)
                 FROM conversation_messages m
                 WHERE m.conversation_id = conversations.id
                 ORDER BY m.seq DESC
                 LIMIT 1
               )`,
        );

        const convCnt = Number(
          (
            this.sql
              .exec('SELECT COUNT(*) AS n FROM conversations')
              .toArray()[0] as any
          )?.n ?? 0,
        );
        const msgStats = this.sql
          .exec(
            'SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN body_bytes > 0 THEN body_bytes ELSE LENGTH(CAST(body AS BLOB)) END), 0) AS b FROM conversation_messages',
          )
          .toArray()[0] as any;
        const remCnt = Number(
          (
            this.sql
              .exec(
                'SELECT COUNT(*) AS n FROM conversation_members WHERE removed_at IS NOT NULL',
              )
              .toArray()[0] as any
          )?.n ?? 0,
        );

        this.sql.exec(
          `INSERT INTO conversation_storage_usage (
            singleton, conversation_count, message_count, message_bytes, removed_member_count, schema_version
          ) VALUES (1, ?, ?, ?, ?, ?)
          ON CONFLICT(singleton) DO UPDATE SET
            conversation_count = excluded.conversation_count,
            message_count = excluded.message_count,
            message_bytes = excluded.message_bytes,
            removed_member_count = excluded.removed_member_count,
            schema_version = excluded.schema_version`,
          convCnt,
          Number(msgStats?.n ?? 0),
          Number(msgStats?.b ?? 0),
          remCnt,
          CONVERSATIONS_SCHEMA_VERSION,
        );
      }
      // A bounded fair pass reconciles pre-existing orphan links on startup.
      // Remaining work stays visible to the ordinary retention driver.
      if (this.linksHandler.hasExpiredRows(this.now())) {
        this.retentionCleanup(this.now(), DEFAULT_RETENTION_BATCH_LIMIT);
      }
    });
  }

  public getStorageUsage(): ConversationStorageUsage {
    const row = this.sql
      .exec('SELECT * FROM conversation_storage_usage WHERE singleton = 1')
      .toArray()[0] as any;
    return {
      conversation_count: Number(row?.conversation_count ?? 0),
      message_count: Number(row?.message_count ?? 0),
      message_bytes: Number(row?.message_bytes ?? 0),
      removed_member_count: Number(row?.removed_member_count ?? 0),
    };
  }

  public assertActiveMember(
    conversationId: string,
    principal: VerifiedConversationPrincipal,
  ): { conversation: any; member: any } {
    return this.requireActiveMembership(
      conversationId,
      principal.actor,
      false,
      principal.kind,
    );
  }

  public hasExpiredRows(nowMs: number = this.now()): boolean {
    if (this.automationRetirementCandidates(nowMs, 1).some(rows => rows.length)) return true;
    if (this.linksHandler?.hasExpiredRows(nowMs)) return true;
    const archivedCutoffIso = new Date(
      nowMs - this.limits.archivedConversationRetentionMs,
    ).toISOString();
    if (
      this.sql
        .exec(
          `SELECT 1 FROM conversations c WHERE ${ARCHIVED_PARENT_WORK_SQL} LIMIT 1`,
          archivedCutoffIso,
        )
        .toArray().length > 0
    ) {
      return true;
    }
    const removedMemberCutoffIso = new Date(
      nowMs - this.limits.removedMemberRetentionMs,
    ).toISOString();
    if (
      this.sql
        .exec(
          'SELECT 1 FROM conversation_members WHERE removed_at IS NOT NULL AND removed_at <= ? LIMIT 1',
          removedMemberCutoffIso,
        )
        .toArray().length > 0
    ) {
      return true;
    }
    const messageCutoffIso = new Date(
      nowMs - this.limits.messageRetentionMs,
    ).toISOString();
    return (
      this.sql
        .exec(
          'SELECT 1 FROM conversation_messages WHERE created_at <= ? LIMIT 1',
          messageCutoffIso,
        )
        .toArray().length > 0
    );
  }

  private refreshConversationPreview(conversationId: string): void {
    const latestMsg = this.sql
      .exec(
        'SELECT body FROM conversation_messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT 1',
        conversationId,
      )
      .toArray()[0] as any;
    const nextPreview =
      latestMsg && typeof latestMsg.body === 'string'
        ? latestMsg.body.replace(/\s+/g, ' ').trim().slice(0, 160)
        : null;
    this.sql.exec(
      'UPDATE conversations SET last_message_preview = ? WHERE id = ?',
      nextPreview,
      conversationId,
    );
  }

  /** Hard table caps bound the indexed anti-joins, including an R2 database.
   * A receipt is pinned by an unresolved slot even if its grant/parent is gone.
   * Standalone service use does not require a configured scheduler table. */
  private automationRetirementCandidates(nowMs: number, limit: number): any[][] {
    const hasChecks = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_checks'").toArray().length > 0;
    const orphan = hasChecks ? 'NOT EXISTS (SELECT 1 FROM hub_checks c WHERE c.name=s.name)' : '0';
    const invalidGrant = `NOT EXISTS (SELECT 1 FROM conversation_automation_grants g
      JOIN conversations c ON c.id=g.conversation_id AND c.archived_at IS NULL
      WHERE g.id=json_extract(s.check_json, '$.action.grant_id') AND g.revoked_at IS NULL AND g.expires_at>?)`;
    const slots = this.sql.exec(`SELECT s.*, ${orphan} AS orphan FROM hub_check_slots s
      WHERE ((${orphan}) OR (s.state!='done' AND json_extract(s.check_json, '$.action.conversation_id') IS NOT NULL AND ${invalidGrant}))
      AND NOT EXISTS (SELECT 1 FROM conversation_automation_receipts r
        WHERE r.effect_key=('check-' || s.slot_key) AND r.request_json IS NOT s.intent_json)
      ORDER BY s.name LIMIT ?`, nowMs, limit).toArray();
    const receipts = this.sql.exec(`SELECT r.effect_key FROM conversation_automation_receipts r
      WHERE NOT EXISTS (SELECT 1 FROM hub_check_slots s WHERE ('check-' || s.slot_key)=r.effect_key AND s.state!='done')
      ORDER BY r.effect_key LIMIT ?`, limit).toArray();
    const grants = this.sql.exec(`SELECT g.id FROM conversation_automation_grants g
      WHERE (g.expires_at<=? OR g.revoked_at IS NOT NULL OR NOT EXISTS
        (SELECT 1 FROM conversations c WHERE c.id=g.conversation_id AND c.archived_at IS NULL))
      AND NOT EXISTS (SELECT 1 FROM hub_check_slots s WHERE json_extract(s.check_json, '$.action.grant_id')=g.id AND s.state!='done')
      ORDER BY g.id LIMIT ?`, nowMs, limit).toArray();
    return [slots, receipts, grants];
  }

  /**
   * Bounded batched retention cleanup for Free-plan storage discipline.
   * Enforces a global affected-row budget (`batchLimit`) across child messages,
   * child members, conversation headers, expired removed-member tombstones, and
   * expired messages while keeping conversation sequence watermarks (`last_seq`)
   * monotonic and recomputing content previews (`last_message_preview`).
   */
  public retentionCleanup(
    nowMs: number = this.now(),
    batchLimit: number = DEFAULT_RETENTION_BATCH_LIMIT,
  ): ConversationRetentionResult {
    const limit = Math.max(1, Math.min(500, Math.floor(batchLimit)));
    this.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    return this.runInTxn(() => {
      let remaining = limit;
      let conversationsDeleted = 0;
      let messagesDeleted = 0;
      let removedMembersDeleted = 0;
      let membersDeleted = 0;
      let automationRowsDeleted = 0;
      let linksDeleted = 0;
      const previewRefreshIds = new Set<string>();

      const archivedCutoffIso = new Date(
        nowMs - this.limits.archivedConversationRetentionMs,
      ).toISOString();
      const removedMemberCutoffIso = new Date(
        nowMs - this.limits.removedMemberRetentionMs,
      ).toISOString();
      const messageCutoffIso = new Date(
        nowMs - this.limits.messageRetentionMs,
      ).toISOString();

      const runArchivedStep = (stepBudget: number): number => {
        const remainingBefore = remaining;
        let stepRemaining = Math.min(remaining, stepBudget);
        if (stepRemaining <= 0) return 0;

        const expiredConvs = this.sql
          .exec(
            `SELECT c.id FROM conversations c
             WHERE ${ARCHIVED_PARENT_WORK_SQL}
             ORDER BY c.archived_at ASC, c.id ASC LIMIT ?`,
            archivedCutoffIso,
            stepRemaining,
          )
          .toArray() as any[];

        for (const conv of expiredConvs) {
          if (stepRemaining <= 0) break;

          // 1a. Delete child messages in indexed chunks
          const msgChunk = this.sql
            .exec(
              `SELECT id, seq, body_bytes, body
               FROM conversation_messages
               WHERE conversation_id = ?
               ORDER BY seq ASC
               LIMIT ?`,
              conv.id,
              stepRemaining,
            )
            .toArray() as any[];

          if (msgChunk.length > 0) {
            let chunkBytes = 0;
            for (const m of msgChunk) {
              chunkBytes +=
                Number(m.body_bytes) > 0
                  ? Number(m.body_bytes)
                  : new TextEncoder().encode(String(m.body ?? '')).byteLength;
            }
            const maxSeq = Number(msgChunk[msgChunk.length - 1].seq);
            this.sql.exec(
              'DELETE FROM conversation_messages WHERE conversation_id = ? AND seq <= ?',
              conv.id,
              maxSeq,
            );
            this.sql.exec(
              `UPDATE conversations
               SET retained_message_count = MAX(0, retained_message_count - ?),
                   retained_message_bytes = MAX(0, retained_message_bytes - ?),
                   last_message_preview = NULL
               WHERE id = ?`,
              msgChunk.length,
              chunkBytes,
              conv.id,
            );
            this.sql.exec(
              `UPDATE conversation_storage_usage
               SET message_count = MAX(0, message_count - ?),
                   message_bytes = MAX(0, message_bytes - ?)
               WHERE singleton = 1`,
              msgChunk.length,
              chunkBytes,
            );
            messagesDeleted += msgChunk.length;
            remaining -= msgChunk.length;
            stepRemaining -= msgChunk.length;
          }

          const stillHasMessages =
            this.sql
              .exec(
                'SELECT 1 FROM conversation_messages WHERE conversation_id = ? LIMIT 1',
                conv.id,
              )
              .toArray().length > 0;
          if (stillHasMessages || stepRemaining <= 0) {
            continue;
          }

          // 1b. Delete child members in indexed chunks
          const memberChunk = this.sql
            .exec(
              `SELECT actor, removed_at
               FROM conversation_members
               WHERE conversation_id = ?
               ORDER BY actor ASC
               LIMIT ?`,
              conv.id,
              stepRemaining,
            )
            .toArray() as any[];

          if (memberChunk.length > 0) {
            let removedInChunk = 0;
            for (const cm of memberChunk) {
              if (cm.removed_at !== null && cm.removed_at !== undefined) {
                removedInChunk++;
              }
            }
            const maxActor = String(memberChunk[memberChunk.length - 1].actor);
            this.sql.exec(
              'DELETE FROM conversation_members WHERE conversation_id = ? AND actor <= ?',
              conv.id,
              maxActor,
            );
            if (removedInChunk > 0) {
              this.sql.exec(
                'UPDATE conversations SET removed_member_count = MAX(0, removed_member_count - ?) WHERE id = ?',
                removedInChunk,
                conv.id,
              );
              this.sql.exec(
                'UPDATE conversation_storage_usage SET removed_member_count = MAX(0, removed_member_count - ?) WHERE singleton = 1',
                removedInChunk,
              );
              removedMembersDeleted += removedInChunk;
            }
            membersDeleted += memberChunk.length;
            remaining -= memberChunk.length;
            stepRemaining -= memberChunk.length;
          }

          const stillHasMembers =
            this.sql
              .exec(
                'SELECT 1 FROM conversation_members WHERE conversation_id = ? LIMIT 1',
                conv.id,
              )
              .toArray().length > 0;
          if (stillHasMembers || stepRemaining <= 0) {
            continue;
          }

          // 1c. Delete child links in indexed chunks (counted toward the same budget)
          if (this.linksHandler && stepRemaining > 0) {
            const linkChunk = this.linksHandler.deleteChunkForConversation(
              conv.id,
              stepRemaining,
            );
            if (linkChunk > 0) {
              linksDeleted += linkChunk;
              remaining -= linkChunk;
              stepRemaining -= linkChunk;
            }
            if (
              this.linksHandler.hasRowsForConversation(conv.id) ||
              stepRemaining <= 0
            ) {
              continue;
            }
          }

          // Automation retires through its fair categories, never a cascading
          // delete. Keep the parent while receipts/grants or uncertain slots
          // still need it; inconsistent frozen effects must remain recoverable.
          if (this.sql.exec(`SELECT 1 FROM conversations c WHERE c.id=?
              AND (${ARCHIVED_PARENT_UNBLOCKED_SQL})`, conv.id).toArray().length === 0) {
            continue;
          }

          // 1d. Delete the header only after bounded child retirement completes.
          this.sql.exec('DELETE FROM conversations WHERE id = ?', conv.id);
          this.sql.exec(
            `UPDATE conversation_storage_usage
             SET conversation_count = MAX(0, conversation_count - 1)
             WHERE singleton = 1`,
          );
          previewRefreshIds.delete(conv.id);
          conversationsDeleted++;
          remaining--;
          stepRemaining--;
        }
        return remainingBefore - remaining;
      };

      const runRemovedMembersStep = (stepBudget: number): number => {
        const remainingBefore = remaining;
        const stepLimit = Math.min(remaining, stepBudget);
        if (stepLimit <= 0) return 0;

        const expiredRemovedMembers = this.sql
          .exec(
            `SELECT conversation_id, actor
             FROM conversation_members
             WHERE removed_at IS NOT NULL AND removed_at <= ?
             ORDER BY removed_at ASC, conversation_id ASC, actor ASC
             LIMIT ?`,
            removedMemberCutoffIso,
            stepLimit,
          )
          .toArray() as any[];

        for (const rm of expiredRemovedMembers) {
          this.sql.exec(
            'DELETE FROM conversation_members WHERE conversation_id = ? AND actor = ? AND removed_at IS NOT NULL',
            rm.conversation_id,
            rm.actor,
          );
          this.sql.exec(
            'UPDATE conversations SET removed_member_count = MAX(0, removed_member_count - 1) WHERE id = ?',
            rm.conversation_id,
          );
          this.sql.exec(
            'UPDATE conversation_storage_usage SET removed_member_count = MAX(0, removed_member_count - 1) WHERE singleton = 1',
          );
          removedMembersDeleted++;
          membersDeleted++;
          remaining--;
        }
        return remainingBefore - remaining;
      };

      const runMessagesStep = (stepBudget: number): number => {
        const remainingBefore = remaining;
        const stepLimit = Math.min(remaining, stepBudget);
        if (stepLimit <= 0) return 0;

        const expiredMessages = this.sql
          .exec(
            `SELECT id, conversation_id, body_bytes, body
             FROM conversation_messages
             WHERE created_at <= ?
             ORDER BY created_at ASC, id ASC
             LIMIT ?`,
            messageCutoffIso,
            stepLimit,
          )
          .toArray() as any[];

        for (const msg of expiredMessages) {
          const bytes =
            Number(msg.body_bytes) > 0
              ? Number(msg.body_bytes)
              : new TextEncoder().encode(String(msg.body ?? '')).byteLength;
          this.sql.exec(
            'DELETE FROM conversation_messages WHERE id = ?',
            msg.id,
          );
          this.sql.exec(
            `UPDATE conversations
             SET retained_message_count = MAX(0, retained_message_count - 1),
                 retained_message_bytes = MAX(0, retained_message_bytes - ?)
             WHERE id = ?`,
            bytes,
            msg.conversation_id,
          );
          this.sql.exec(
            `UPDATE conversation_storage_usage
             SET message_count = MAX(0, message_count - 1),
                 message_bytes = MAX(0, message_bytes - ?)
             WHERE singleton = 1`,
            bytes,
          );
          previewRefreshIds.add(String(msg.conversation_id));
          messagesDeleted++;
          remaining--;
        }
        return remainingBefore - remaining;
      };

      const retireAutomation = (category: number, budget: number): number => {
        const before = remaining;
        const candidates = this.automationRetirementCandidates(nowMs, Math.min(remaining, budget))[category];
        for (const row of candidates) {
          if (category === 0) {
            const receipt = this.sql.exec('SELECT * FROM conversation_automation_receipts WHERE effect_key=?', 'check-' + row.slot_key).toArray()[0];
            // An inconsistent receipt is uncertainty, never permission to discard it.
            if (receipt && receipt.request_json !== row.intent_json) continue;
            const grantId = JSON.parse(row.check_json).action?.grant_id;
            if (row.orphan && grantId) this.sql.exec('UPDATE conversation_automation_grants SET revoked_at=? WHERE id=?', nowMs, grantId);
            const result = receipt && row.result_json ? JSON.parse(row.result_json) : { status: 'error', detail: 'automation slot cancelled; no accepted Hub effect' };
            const action = receipt ? `conversation_message ${receipt.message_id} (Hub stored; native delivery unconfirmed)` : 'automation authority retired; no accepted Hub effect';
            // Closing the slot and recovering its receipt share this transaction.
            // This also fences an evaluator/send that was paused before cleanup.
            this.sql.exec("UPDATE hub_check_slots SET state='done', token=NULL, lease_until=0, result_json=?, action=? WHERE name=?", JSON.stringify(result), action, row.name);
            if (row.orphan) {
              this.sql.exec('DELETE FROM hub_check_slots WHERE name=?', row.name);
              remaining--; automationRowsDeleted++;
            } else if (this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hub_checks'").toArray().length) {
              this.sql.exec('UPDATE hub_checks SET last_status=?, last_detail=? WHERE name=?', result.status, result.detail.slice(0, 1000), row.name);
            }
          } else {
            this.sql.exec(category === 1 ? 'DELETE FROM conversation_automation_receipts WHERE effect_key=?' : 'DELETE FROM conversation_automation_grants WHERE id=?', category === 1 ? row.effect_key : row.id);
            remaining--; automationRowsDeleted++;
          }
        }
        return before - remaining;
      };

      const runOrphanLinksStep = (stepBudget: number): number => {
        const budget = Math.min(remaining, stepBudget);
        if (budget <= 0) return 0;
        const step = this.linksHandler.retentionCleanup(nowMs, budget);
        linksDeleted += step.rowsDeleted;
        remaining -= step.rowsDeleted;
        return step.rowsDeleted;
      };

      runFairRetentionPass(
        this.sql,
        'conv',
        [
          ...[0, 1, 2].map(category => ({
            hasWork: () => this.automationRetirementCandidates(nowMs, 1)[category].length > 0,
            spend: (budget: number) => retireAutomation(category, budget),
          })),
          {
            hasWork: () =>
              this.sql
                .exec(
                  `SELECT 1 FROM conversations c WHERE ${ARCHIVED_PARENT_WORK_SQL} LIMIT 1`,
                  archivedCutoffIso,
                )
                .toArray().length > 0,
            spend: runArchivedStep,
          },
          {
            hasWork: () =>
              this.sql
                .exec(
                  'SELECT 1 FROM conversation_members WHERE removed_at IS NOT NULL AND removed_at <= ? LIMIT 1',
                  removedMemberCutoffIso,
                )
                .toArray().length > 0,
            spend: runRemovedMembersStep,
          },
          {
            hasWork: () =>
              this.sql
                .exec(
                  'SELECT 1 FROM conversation_messages WHERE created_at <= ? LIMIT 1',
                  messageCutoffIso,
                )
                .toArray().length > 0,
            spend: runMessagesStep,
          },
          {
            hasWork: () => this.linksHandler.hasExpiredRows(nowMs),
            spend: runOrphanLinksStep,
          },
        ],
        limit,
      );

      for (const convId of previewRefreshIds) {
        this.refreshConversationPreview(convId);
      }

      const rowsDeleted =
        conversationsDeleted + messagesDeleted + membersDeleted + automationRowsDeleted + linksDeleted;
      const hasMore = this.hasExpiredRows(nowMs);

      return {
        conversationsDeleted,
        messagesDeleted,
        removedMembersDeleted,
        membersDeleted,
        automationRowsDeleted,
        linksDeleted,
        rowsDeleted,
        hasMore,
      };
    });
  }

  private runInTxn<T>(fn: () => T): T {
    return this.txSync(fn);
  }

  private nowIso(): string {
    return new Date(this.now()).toISOString();
  }

  /**
   * Verify and normalize caller identity into a VerifiedConversationPrincipal.
   * Re-reads live credential revocation, expiry, kind, actor, and scopes from SQLite
   * when a credential ID or token hash is present, so queued body reads or stale
   * snapshots cannot bypass revocation/expiry/scope changes.
   */
  public verifyPrincipal(
    auth: ConversationAuthInput | CallerContext | null | undefined,
    options?: {
      requireWriteScope?: boolean;
      requireReadScope?: boolean;
      isMutatingHttp?: boolean;
    },
  ): VerifiedConversationPrincipal {
    if (!auth) {
      throw new ConversationDomainError('unauthorized', 401);
    }

    const normalizedAuth: ConversationAuthInput =
      'caller' in auth || 'session' in auth || 'error' in auth || 'authType' in auth
        ? (auth as ConversationAuthInput)
        : { caller: auth as CallerContext };

    if (normalizedAuth.error) {
      throw new ConversationDomainError(
        'unauthorized',
        normalizedAuth.error.status || 401,
      );
    }

    const session = normalizedAuth.session;
    const caller = normalizedAuth.caller;

    if (!session && !caller) {
      throw new ConversationDomainError('unauthorized', 401);
    }

    if (caller?.isRevoked) {
      throw new ConversationDomainError('unauthorized', 401);
    }

    // 1. Session-backed human principal
    if (session) {
      if (
        session.isAdmin !== true ||
        typeof session.exp !== 'number' ||
        !Number.isFinite(session.exp) ||
        session.exp <= Math.floor(this.now() / 1000)
      ) {
        throw new ConversationDomainError('unauthorized', 401);
      }
      const humanActor = formatHumanActorFromSub(session.sub);
      if (!humanActor) {
        throw new ConversationDomainError(
          'forbidden',
          403,
          'collapsed_admin_identity_refused',
        );
      }
      const csrfVerified = normalizedAuth.csrfVerified === true;
      if (options?.isMutatingHttp && !csrfVerified) {
        throw new ConversationDomainError(
          'csrf_invalid',
          403,
          'csrf_token_missing_or_invalid',
        );
      }
      try {
        const userRow = this.sql
          .exec('SELECT is_admin FROM users WHERE sub = ?', session.sub)
          .toArray()[0] as any;
        if (userRow && !userRow.is_admin) {
          throw new ConversationDomainError('forbidden', 403);
        }
      } catch (err) {
        if (err instanceof ConversationDomainError) throw err;
      }

      return {
        actor: humanActor,
        kind: 'human',
        authType: 'session',
        scopes: ['bus:read', 'bus:send'],
        csrfVerified,
        displayName: session.email || humanActor,
        sessionSub: session.sub,
        nativeBinding: null,
      };
    }

    // 2. CallerContext-backed principal
    let rawActor = caller?.actor;
    let kind = caller?.kind;
    let effectiveScopes = normalizedAuth.scopes ?? caller?.scopes;
    const tokenId = caller?.tokenId;
    const tokenHash = caller?.tokenHash;

    if (
      kind === 'host' ||
      kind === 'job' ||
      (!kind && caller?.machineId && !rawActor)
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'invalid_principal_kind',
      );
    }

    if (
      rawActor === 'admin' ||
      rawActor === 'human:admin' ||
      rawActor === 'human:'
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'collapsed_admin_identity_refused',
      );
    }

    // Human CallerContext (must come from a verified session, never a bearer token)
    if (
      kind === 'human' ||
      kind === 'session' ||
      (typeof rawActor === 'string' && rawActor.startsWith('human:'))
    ) {
      if (normalizedAuth.authType === 'bearer' || tokenId || tokenHash) {
        throw new ConversationDomainError(
          'forbidden',
          403,
          'invalid_principal_kind',
        );
      }
      const parsedHuman = parseCanonicalHumanActor(rawActor);
      if (!parsedHuman) {
        throw new ConversationDomainError(
          'forbidden',
          403,
          'collapsed_admin_identity_refused',
        );
      }
      const csrfVerified = normalizedAuth.csrfVerified === true;
      if (options?.isMutatingHttp && !csrfVerified) {
        throw new ConversationDomainError(
          'csrf_invalid',
          403,
          'csrf_token_missing_or_invalid',
        );
      }
      return {
        actor: parsedHuman.actor,
        kind: 'human',
        authType: 'session',
        scopes: effectiveScopes ?? ['bus:read', 'bus:send'],
        csrfVerified,
        displayName: parsedHuman.actor,
        sessionSub: parsedHuman.sub,
        nativeBinding: null,
      };
    }

    // Revalidate live bearer credential state from SQLite whenever tokenId or tokenHash is bound
    if (tokenId || tokenHash) {
      let liveCred: any = null;
      try {
        if (tokenId && tokenHash) {
          liveCred = this.sql
            .exec(
              'SELECT * FROM credentials WHERE id = ? AND token_hash = ?',
              tokenId,
              tokenHash,
            )
            .toArray()[0];
        } else if (tokenId) {
          liveCred = this.sql
            .exec('SELECT * FROM credentials WHERE id = ?', tokenId)
            .toArray()[0];
        } else if (tokenHash) {
          liveCred = this.sql
            .exec(
              'SELECT * FROM credentials WHERE token_hash = ?',
              tokenHash,
            )
            .toArray()[0];
        }
      } catch {
        liveCred = null;
      }

      if (
        !liveCred ||
        (liveCred.revoked_at !== null && liveCred.revoked_at !== undefined)
      ) {
        throw new ConversationDomainError('unauthorized', 401);
      }

      const expiresMs = Date.parse(liveCred.expires_at);
      if (!Number.isFinite(expiresMs) || expiresMs <= this.now()) {
        throw new ConversationDomainError('unauthorized', 401);
      }

      let liveScopes: string[];
      try {
        liveScopes =
          typeof liveCred.scopes === 'string'
            ? JSON.parse(liveCred.scopes)
            : liveCred.scopes;
      } catch {
        throw new ConversationDomainError('unauthorized', 401);
      }
      if (!Array.isArray(liveScopes)) {
        throw new ConversationDomainError('unauthorized', 401);
      }

      kind = liveCred.kind;
      const boundActor = credentialActor(this.sql as any, liveCred);
      if (liveCred.kind === 'agent' && !boundActor) {
        throw new ConversationDomainError('forbidden', 403, 'unregistered_agent_principal');
      }
      rawActor = boundActor ?? undefined;
      effectiveScopes = liveScopes;
    }

    if (!rawActor || typeof rawActor !== 'string') {
      throw new ConversationDomainError('unauthorized', 401);
    }

    if (
      kind !== undefined &&
      kind !== 'agent' &&
      kind !== 'bearer'
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'invalid_principal_kind',
      );
    }

    if (
      isRootRoleAddress(rawActor) ||
      RESERVED_AGENT_NAMESPACE_REGEX.test(rawActor) ||
      RESERVED_AGENT_PREFIX_REGEX.test(rawActor)
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'invalid_principal_kind',
      );
    }

    if (Array.isArray(effectiveScopes)) {
      if (
        effectiveScopes.includes('routing:submit') &&
        !effectiveScopes.includes('bus:read') &&
        !effectiveScopes.includes('bus:send')
      ) {
        throw new ConversationDomainError(
          'forbidden',
          403,
          'invalid_principal_kind',
        );
      }
      if (
        options?.requireWriteScope &&
        !effectiveScopes.includes('bus:send')
      ) {
        throw new ConversationDomainError('forbidden', 403, 'scope_missing');
      }
      if (
        options?.requireReadScope &&
        !effectiveScopes.includes('bus:read') &&
        !effectiveScopes.includes('bus:send')
      ) {
        throw new ConversationDomainError('forbidden', 403, 'scope_missing');
      }
    } else if (options?.isMutatingHttp !== undefined) {
      throw new ConversationDomainError('forbidden', 403, 'scope_missing');
    }

    const validatedAgent = this.identityValidator.resolveParticipant(rawActor);
    if (
      !validatedAgent ||
      validatedAgent.kind !== 'agent' ||
      !isDisjointAgentActor(validatedAgent.actor)
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'unregistered_agent_principal',
      );
    }

    if (rawActor.includes(':') && validatedAgent.actor !== rawActor) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'unregistered_agent_principal',
      );
    }

    if (
      caller?.project !== undefined &&
      caller.project !== null &&
      validatedAgent.project !== caller.project
    ) {
      throw new ConversationDomainError(
        'forbidden',
        403,
        'unregistered_agent_principal',
      );
    }

    return {
      actor: validatedAgent.actor,
      kind: 'agent',
      authType: 'bearer',
      scopes: effectiveScopes ?? ['bus:read', 'bus:send'],
      csrfVerified: true,
      displayName: validatedAgent.display_name,
      tokenId,
      nativeBinding: validatedAgent.native_binding ?? null,
    };
  }

  private isArchivedConversationExpired(
    convRow: any,
    nowMs: number = this.now(),
  ): boolean {
    if (!convRow || !convRow.archived_at) return false;
    const archivedMs = Date.parse(String(convRow.archived_at));
    if (!Number.isFinite(archivedMs)) return false;
    return archivedMs <= nowMs - this.limits.archivedConversationRetentionMs;
  }

  private getRetainedVisibility(
    conversationId: string,
    lastSeq: number,
    readSeq: number,
  ): {
    oldestRetainedSeq: number | null;
    newestRetainedSeq: number | null;
    prunedThroughSeq: number;
    retainedCount: number;
    unreadCount: number;
  } {
    const boundsRow = this.sql
      .exec(
        `SELECT COUNT(*) AS retained_count,
                MIN(seq) AS min_seq,
                MAX(seq) AS max_seq,
                SUM(CASE WHEN seq > ? THEN 1 ELSE 0 END) AS unread_count
         FROM conversation_messages
         WHERE conversation_id = ?`,
        readSeq,
        conversationId,
      )
      .toArray()[0] as any;
    const retainedCount = Number(boundsRow?.retained_count ?? 0);
    const oldestRetainedSeq =
      retainedCount > 0 &&
      boundsRow?.min_seq !== null &&
      boundsRow?.min_seq !== undefined
        ? Number(boundsRow.min_seq)
        : null;
    const newestRetainedSeq =
      retainedCount > 0 &&
      boundsRow?.max_seq !== null &&
      boundsRow?.max_seq !== undefined
        ? Number(boundsRow.max_seq)
        : null;
    const prunedThroughSeq =
      oldestRetainedSeq !== null
        ? Math.max(0, oldestRetainedSeq - 1)
        : lastSeq;
    return {
      oldestRetainedSeq,
      newestRetainedSeq,
      prunedThroughSeq,
      retainedCount,
      unreadCount: Number(boundsRow?.unread_count ?? 0),
    };
  }

  /**
   * First missing sequence range after `afterSeq` in 1..lastSeq.
   * `pruned_through_seq` remains the contiguous prefix watermark and must not
   * skip a retained sequence; interior/suffix holes are reported separately.
   */
  private getFirstMissingSeqRange(
    conversationId: string,
    afterSeq: number,
    lastSeq: number,
    newestRetainedSeq: number | null,
  ): { from: number; to: number } | null {
    if (lastSeq <= afterSeq) return null;
    const nextRow = this.sql
      .exec(
        'SELECT seq FROM conversation_messages WHERE conversation_id = ? AND seq > ? ORDER BY seq ASC LIMIT 1',
        conversationId,
        afterSeq,
      )
      .toArray()[0] as any;
    if (!nextRow) {
      return { from: afterSeq + 1, to: lastSeq };
    }
    const nextSeq = Number(nextRow.seq);
    if (nextSeq > afterSeq + 1) {
      return { from: afterSeq + 1, to: nextSeq - 1 };
    }
    const holeAfter = this.sql
      .exec(
        `SELECT m.seq AS seq
         FROM conversation_messages m
         WHERE m.conversation_id = ?
           AND m.seq > ?
           AND NOT EXISTS (
             SELECT 1 FROM conversation_messages p
             WHERE p.conversation_id = m.conversation_id AND p.seq = m.seq - 1
           )
         ORDER BY m.seq ASC
         LIMIT 1`,
        conversationId,
        nextSeq,
      )
      .toArray()[0] as any;
    if (holeAfter) {
      const holeAt = Number(holeAfter.seq);
      const prevRow = this.sql
        .exec(
          'SELECT MAX(seq) AS seq FROM conversation_messages WHERE conversation_id = ? AND seq < ?',
          conversationId,
          holeAt,
        )
        .toArray()[0] as any;
      const from = Number(prevRow?.seq ?? afterSeq) + 1;
      if (from <= holeAt - 1) {
        return { from, to: holeAt - 1 };
      }
    }
    if (newestRetainedSeq !== null && newestRetainedSeq < lastSeq) {
      const suffixFrom = Math.max(afterSeq + 1, newestRetainedSeq + 1);
      if (suffixFrom <= lastSeq) {
        return { from: suffixFrom, to: lastSeq };
      }
    }
    return null;
  }

  private getConversationRow(conversationId: string): any {
    if (
      typeof conversationId !== 'string' ||
      !conversationId ||
      conversationId.length > 128
    ) {
      throw new ConversationDomainError('not_found', 404);
    }
    const row = this.sql
      .exec('SELECT * FROM conversations WHERE id = ?', conversationId)
      .toArray()[0] as any;
    if (!row || this.isArchivedConversationExpired(row)) {
      throw new ConversationDomainError('not_found', 404);
    }
    return row;
  }

  private requireActiveMembership(
    conversationId: string,
    actor: string,
    requireOwner = false,
    expectedKind?: 'human' | 'agent',
  ): { conversation: any; member: any } {
    const conversation = this.getConversationRow(conversationId);
    const member = this.sql
      .exec(
        'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ?',
        conversationId,
        actor,
      )
      .toArray()[0] as any;

    if (!member) {
      if (conversation.created_by === actor) {
        throw new ConversationDomainError('forbidden', 403, 'member_removed');
      }
      throw new ConversationDomainError('forbidden', 403, 'not_a_member');
    }
    if (expectedKind && member.kind !== expectedKind) {
      throw new ConversationDomainError('forbidden', 403, 'not_a_member');
    }
    if (member.removed_at !== null && member.removed_at !== undefined) {
      throw new ConversationDomainError('forbidden', 403, 'member_removed');
    }
    if (requireOwner && member.role !== 'owner') {
      throw new ConversationDomainError('forbidden', 403, 'owner_required');
    }
    return { conversation, member };
  }

  private formatMemberRow(
    row: any,
    principal?: VerifiedConversationPrincipal,
  ): Record<string, any> {
    const resolved = this.identityValidator.resolveParticipant(
      row.actor,
      principal,
    );
    return {
      actor: row.actor,
      kind: row.kind,
      role: row.role,
      added_at: row.added_at,
      added_by: row.added_by,
      removed_at: row.removed_at ?? null,
      removed_by: row.removed_by ?? null,
      last_read_seq: Number(row.last_read_seq ?? 0),
      last_read_at: row.last_read_at ?? null,
      display_name: resolved?.display_name ?? row.actor,
      native_binding:
        row.kind === 'agent' ? (resolved?.native_binding ?? null) : null,
    };
  }

  private formatConversationDetail(
    convRow: any,
    myMemberRow: any,
    principal?: VerifiedConversationPrincipal,
    replay?: boolean,
  ): Record<string, any> {
    const activeMembers = (
      this.sql
        .exec(
          'SELECT * FROM conversation_members WHERE conversation_id = ? AND removed_at IS NULL ORDER BY CASE WHEN role = "owner" THEN 0 ELSE 1 END, actor ASC',
          convRow.id,
        )
        .toArray() as any[]
    ).map(m => this.formatMemberRow(m, principal));

    const lastSeq = Number(convRow.last_seq ?? 0);
    const myReadSeq = Number(myMemberRow.last_read_seq ?? 0);
    const { oldestRetainedSeq, unreadCount } = this.getRetainedVisibility(
      convRow.id,
      lastSeq,
      myReadSeq,
    );
    const preview =
      oldestRetainedSeq !== null ? (convRow.last_message_preview ?? null) : null;

    const detail: Record<string, any> = {
      id: convRow.id,
      kind: convRow.kind,
      title: convRow.title,
      name: convRow.title,
      created_by: convRow.created_by,
      created_at: convRow.created_at,
      updated_at: convRow.updated_at,
      archived_at: convRow.archived_at ?? null,
      archived_by: convRow.archived_by ?? null,
      last_seq: lastSeq,
      last_message_at: convRow.last_message_at ?? null,
      last_message_preview: preview,
      self_actor: myMemberRow.actor,
      my_role: myMemberRow.role,
      my_read_seq: myReadSeq,
      read_seq: myReadSeq,
      unread_count: unreadCount,
      member_count: activeMembers.length,
      members: activeMembers,
    };
    if (replay !== undefined) {
      detail.replay = replay;
    }
    return {
      ...detail,
      conversation: { ...detail },
    };
  }

  private formatMessageRow(row: any): Record<string, any> {
    return {
      id: row.id,
      conversation_id: row.conversation_id,
      seq: Number(row.seq),
      sender_actor: row.sender_actor,
      sender_kind: row.sender_kind,
      kind: row.kind,
      body: row.body,
      idempotency_key: row.idempotency_key,
      reply_to: row.reply_to ?? null,
      reply_to_seq:
        row.reply_to_seq !== null && row.reply_to_seq !== undefined
          ? Number(row.reply_to_seq)
          : null,
      bridge_ref: row.bridge_ref ?? null,
      created_at: row.created_at,
    };
  }

  public listParticipants(
    auth: ConversationAuthInput | CallerContext,
  ): {
    self: ValidatedParticipant;
    participants: ValidatedParticipant[];
  } {
    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      const participants = this.identityValidator.listParticipants(principal);
      const selfParticipant: ValidatedParticipant = {
        actor: principal.actor,
        kind: principal.kind,
        display_name: principal.displayName || principal.actor,
        native_binding: principal.nativeBinding ?? null,
      };
      return {
        self: selfParticipant,
        participants,
      };
    });
  }

  public createConversation(
    auth: ConversationAuthInput | CallerContext,
    input: CreateConversationInput,
  ): Record<string, any> {
    if (input.kind !== 'direct' && input.kind !== 'group') {
      throw new ConversationDomainError('invalid_conversation_kind', 400);
    }

    if (typeof input.title !== 'string') {
      throw new ConversationDomainError('invalid_title', 400);
    }
    const title = input.title.trim();
    if (
      !title ||
      title.length > MAX_TITLE_CHARS ||
      CONTROL_CHARS_REGEX.test(title)
    ) {
      throw new ConversationDomainError('invalid_title', 400);
    }

    if (
      input.idempotencyKey !== undefined &&
      (typeof input.idempotencyKey !== 'string' ||
        !SAFE_KEY_REGEX.test(input.idempotencyKey))
    ) {
      throw new ConversationDomainError('invalid_idempotency_key', 400);
    }

    const rawMembers = input.members ?? [];
    if (!Array.isArray(rawMembers)) {
      throw new ConversationDomainError('invalid_members', 400);
    }
    if (rawMembers.length > MAX_CONVERSATION_MEMBERS) {
      throw new ConversationDomainError('member_limit_exceeded', 409);
    }

    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireWriteScope: true });

      const participantsByActor = new Map<string, ValidatedParticipant>();
      participantsByActor.set(principal.actor, {
        actor: principal.actor,
        kind: principal.kind,
        display_name: principal.displayName || principal.actor,
        native_binding: principal.nativeBinding ?? null,
      });

      for (const rawTarget of rawMembers) {
        if (
          typeof rawTarget !== 'string' ||
          !rawTarget ||
          rawTarget !== rawTarget.trim()
        ) {
          throw new ConversationDomainError('invalid_members', 400);
        }
        const validated = this.identityValidator.resolveParticipant(
          rawTarget,
          principal,
        );
        if (!validated) {
          throw new ConversationDomainError(
            'unknown_participant',
            404,
            'unknown_participant',
            { participant: rawTarget },
          );
        }
        participantsByActor.set(validated.actor, validated);
      }

      const distinctParticipants = Array.from(
        participantsByActor.values(),
      ).sort((a, b) => a.actor.localeCompare(b.actor));

      if (input.kind === 'direct' && distinctParticipants.length !== 2) {
        throw new ConversationDomainError(
          'invalid_direct_members',
          400,
          'direct_conversation_requires_two_distinct_participants',
        );
      }

      if (distinctParticipants.length > MAX_CONVERSATION_MEMBERS) {
        throw new ConversationDomainError('member_limit_exceeded', 409);
      }

      const canonicalRequestJson = JSON.stringify({
        kind: input.kind,
        title,
        members: distinctParticipants.map(p => p.actor),
      });

      if (input.idempotencyKey) {
        const existing = this.sql
          .exec(
            'SELECT * FROM conversations WHERE created_by = ? AND idempotency_key = ?',
            principal.actor,
            input.idempotencyKey,
          )
          .toArray()[0] as any;
        if (existing) {
          const { conversation: currentConv, member: myMember } =
            this.requireActiveMembership(
              existing.id,
              principal.actor,
              false,
              principal.kind,
            );
          if (existing.request_json !== canonicalRequestJson) {
            throw new ConversationDomainError(
              'conflict',
              409,
              'idempotency_conflict',
            );
          }
          return this.formatConversationDetail(
            currentConv,
            myMember,
            principal,
            true,
          );
        }
      }

      const usage = this.getStorageUsage();
      if (usage.conversation_count >= this.limits.maxTotalConversations) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'conversation_limit_exceeded',
        );
      }

      const id = generateUlid(this.now());
      const nowIso = this.nowIso();

      this.sql.exec(
        `INSERT INTO conversations (
          id, kind, title, created_by, idempotency_key, request_json,
          created_at, updated_at, archived_at, archived_by, last_seq,
          last_message_at, last_message_preview,
          retained_message_count, retained_message_bytes, removed_member_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, NULL, 0, 0, 0)`,
        id,
        input.kind,
        title,
        principal.actor,
        input.idempotencyKey ?? null,
        canonicalRequestJson,
        nowIso,
        nowIso,
      );
      this.sql.exec(
        'UPDATE conversation_storage_usage SET conversation_count = conversation_count + 1 WHERE singleton = 1',
      );

      for (const participant of distinctParticipants) {
        const role = participant.actor === principal.actor ? 'owner' : 'member';
        this.sql.exec(
          `INSERT INTO conversation_members (
            conversation_id, actor, kind, role, added_at, added_by,
            removed_at, removed_by, last_read_seq, last_read_at
          ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL)`,
          id,
          participant.actor,
          participant.kind,
          role,
          nowIso,
          principal.actor,
        );
      }

      const convRow = this.getConversationRow(id);
      const myMember = this.sql
        .exec(
          'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ?',
          id,
          principal.actor,
        )
        .toArray()[0] as any;
      return this.formatConversationDetail(
        convRow,
        myMember,
        principal,
        false,
      );
    });
  }

  public getConversation(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
  ): Record<string, any> {
    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      const { conversation, member } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        false,
        principal.kind,
      );
      return this.formatConversationDetail(conversation, member, principal);
    });
  }

  public listConversations(
    auth: ConversationAuthInput | CallerContext,
    options?: ListConversationsOptions,
  ): {
    self_actor: string;
    conversations: Record<string, any>[];
    next_cursor: string | null;
  } {
    const limit = options?.limit ?? DEFAULT_PAGE_LIMIT;
    if (
      typeof limit !== 'number' ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > MAX_PAGE_LIMIT
    ) {
      throw new ConversationDomainError('invalid_limit', 400);
    }

    const effectiveState =
      options?.includeArchived === true
        ? 'all'
        : (options?.state ?? 'active');
    if (!['active', 'archived', 'all'].includes(effectiveState)) {
      throw new ConversationDomainError('invalid_state', 400);
    }

    let cursorUpdatedAt: string | null = null;
    let cursorId: string | null = null;
    if (options?.cursor !== undefined && options.cursor !== '') {
      if (
        typeof options.cursor !== 'string' ||
        options.cursor.length > 160 ||
        !options.cursor.includes('|')
      ) {
        throw new ConversationDomainError('invalid_cursor', 400);
      }
      const [tsPart, idPart] = options.cursor.split('|');
      if (!tsPart || !idPart || !Number.isFinite(Date.parse(tsPart))) {
        throw new ConversationDomainError('invalid_cursor', 400);
      }
      cursorUpdatedAt = tsPart;
      cursorId = idPart;
    }

    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      const archivedCutoffIso = new Date(
        this.now() - this.limits.archivedConversationRetentionMs,
      ).toISOString();

      let query = `
        SELECT c.*, m.role AS my_role, m.last_read_seq AS my_read_seq, m.last_read_at AS my_read_at, m.actor AS my_actor
        FROM conversations c
        JOIN conversation_members m ON m.conversation_id = c.id
        WHERE m.actor = ? AND m.kind = ? AND m.removed_at IS NULL
          AND (c.archived_at IS NULL OR c.archived_at > ?)
      `;
      const params: any[] = [
        principal.actor,
        principal.kind,
        archivedCutoffIso,
      ];

      if (effectiveState === 'active') {
        query += ' AND c.archived_at IS NULL';
      } else if (effectiveState === 'archived') {
        query += ' AND c.archived_at IS NOT NULL';
      }

      if (cursorUpdatedAt && cursorId) {
        query += ' AND (c.updated_at < ? OR (c.updated_at = ? AND c.id < ?))';
        params.push(cursorUpdatedAt, cursorUpdatedAt, cursorId);
      }

      query += ' ORDER BY c.updated_at DESC, c.id DESC LIMIT ?';
      params.push(limit + 1);

      const rows = (this.sql.exec(query, ...params).toArray() as any[]).filter(
        r => !this.isArchivedConversationExpired(r),
      );
      const hasMore = rows.length > limit;
      const pageRows = hasMore ? rows.slice(0, limit) : rows;

      const conversations = pageRows.map(row => {
        const detail = this.formatConversationDetail(
          row,
          {
            actor: row.my_actor,
            role: row.my_role,
            last_read_seq: row.my_read_seq,
            last_read_at: row.my_read_at,
          },
          principal,
        );
        const { conversation: _nested, ...summary } = detail;
        return summary;
      });

      const lastRow = pageRows[pageRows.length - 1];
      const nextCursor =
        hasMore && lastRow ? `${lastRow.updated_at}|${lastRow.id}` : null;

      return {
        self_actor: principal.actor,
        conversations,
        next_cursor: nextCursor,
      };
    });
  }

  public listMembers(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    includeRemoved = false,
  ): { conversation_id: string; members: Record<string, any>[] } {
    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      this.requireActiveMembership(
        conversationId,
        principal.actor,
        false,
        principal.kind,
      );

      const query = includeRemoved
        ? 'SELECT * FROM conversation_members WHERE conversation_id = ? ORDER BY CASE WHEN removed_at IS NULL THEN 0 ELSE 1 END, CASE WHEN role = "owner" THEN 0 ELSE 1 END, actor ASC'
        : 'SELECT * FROM conversation_members WHERE conversation_id = ? AND removed_at IS NULL ORDER BY CASE WHEN role = "owner" THEN 0 ELSE 1 END, actor ASC';

      const rows = this.sql.exec(query, conversationId).toArray() as any[];
      return {
        conversation_id: conversationId,
        members: rows.map(r => this.formatMemberRow(r, principal)),
      };
    });
  }

  public addMember(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    targetActorRaw: string,
    role?: 'member' | 'owner',
  ): { conversation_id: string; member: Record<string, any>; created: boolean } {
    if (role !== undefined && role !== 'member' && role !== 'owner') {
      throw new ConversationDomainError('invalid_role', 400);
    }
    if (
      typeof targetActorRaw !== 'string' ||
      !targetActorRaw ||
      targetActorRaw !== targetActorRaw.trim()
    ) {
      throw new ConversationDomainError('invalid_actor', 400);
    }

    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireWriteScope: true });
      const { conversation } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        true, // explicit owner authority required
        principal.kind,
      );

      if (conversation.archived_at) {
        throw new ConversationDomainError('conversation_archived', 409);
      }

      if (conversation.kind === 'direct') {
        throw new ConversationDomainError(
          'direct_conversation_member_limit',
          409,
        );
      }

      const validatedTarget = this.identityValidator.resolveParticipant(
        targetActorRaw,
        principal,
      );
      if (!validatedTarget) {
        throw new ConversationDomainError('unknown_participant', 404);
      }

      const existing = this.sql
        .exec(
          'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ?',
          conversationId,
          validatedTarget.actor,
        )
        .toArray()[0] as any;

      const nowIso = this.nowIso();

      if (existing && existing.removed_at === null) {
        const targetRole: 'member' | 'owner' =
          role ?? (existing.role === 'owner' ? 'owner' : 'member');
        if (existing.role === 'owner' && targetRole !== 'owner') {
          const activeOwners = this.sql
            .exec(
              "SELECT COUNT(*) AS n FROM conversation_members WHERE conversation_id = ? AND removed_at IS NULL AND role = 'owner'",
              conversationId,
            )
            .toArray()[0] as any;
          if (Number(activeOwners?.n ?? 0) <= 1) {
            throw new ConversationDomainError('cannot_remove_last_owner', 409);
          }
        }
        if (existing.role !== targetRole) {
          this.sql.exec(
            'UPDATE conversation_members SET role = ? WHERE conversation_id = ? AND actor = ?',
            targetRole,
            conversationId,
            validatedTarget.actor,
          );
          this.sql.exec(
            'UPDATE conversations SET updated_at = ? WHERE id = ?',
            nowIso,
            conversationId,
          );
        }
        const updated = this.sql
          .exec(
            'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ?',
            conversationId,
            validatedTarget.actor,
          )
          .toArray()[0] as any;
        return {
          conversation_id: conversationId,
          member: this.formatMemberRow(updated, principal),
          created: false,
        };
      }

      const targetRole: 'member' | 'owner' = role ?? 'member';

      const activeCountRow = this.sql
        .exec(
          'SELECT COUNT(*) AS n FROM conversation_members WHERE conversation_id = ? AND removed_at IS NULL',
          conversationId,
        )
        .toArray()[0] as any;
      if (Number(activeCountRow?.n ?? 0) >= MAX_CONVERSATION_MEMBERS) {
        throw new ConversationDomainError('member_limit_exceeded', 409);
      }

      const currentLastSeq = Number(conversation.last_seq ?? 0);

      if (existing) {
        // Restore previously removed member, advancing last_read_seq monotonically
        // to the current conversation watermark so absent-period messages do not appear unread.
        const prevReadSeq = Number(existing.last_read_seq ?? 0);
        const nextReadSeq = Math.max(prevReadSeq, currentLastSeq);
        const nextReadAt =
          nextReadSeq > prevReadSeq
            ? nowIso
            : (existing.last_read_at ?? (nextReadSeq > 0 ? nowIso : null));
        this.sql.exec(
          `UPDATE conversation_members
           SET role = ?, added_at = ?, added_by = ?, removed_at = NULL, removed_by = NULL,
               last_read_seq = ?, last_read_at = ?
           WHERE conversation_id = ? AND actor = ?`,
          targetRole,
          nowIso,
          principal.actor,
          nextReadSeq,
          nextReadAt,
          conversationId,
          validatedTarget.actor,
        );
        this.sql.exec(
          'UPDATE conversations SET updated_at = ?, removed_member_count = MAX(0, removed_member_count - 1) WHERE id = ?',
          nowIso,
          conversationId,
        );
        this.sql.exec(
          'UPDATE conversation_storage_usage SET removed_member_count = MAX(0, removed_member_count - 1) WHERE singleton = 1',
        );
      } else {
        // Consistent join-at-current-watermark rule for all insertions (new joins and
        // re-adds after tombstone cleanup/eviction): prior messages are not marked unread,
        // while full retained history remains readable via listMessages.
        const initialReadSeq = Math.max(0, currentLastSeq);
        const initialReadAt = initialReadSeq > 0 ? nowIso : null;
        this.sql.exec(
          `INSERT INTO conversation_members (
            conversation_id, actor, kind, role, added_at, added_by,
            removed_at, removed_by, last_read_seq, last_read_at
          ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)`,
          conversationId,
          validatedTarget.actor,
          validatedTarget.kind,
          targetRole,
          nowIso,
          principal.actor,
          initialReadSeq,
          initialReadAt,
        );
        this.sql.exec(
          'UPDATE conversations SET updated_at = ? WHERE id = ?',
          nowIso,
          conversationId,
        );
      }

      const memberRow = this.sql
        .exec(
          'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ?',
          conversationId,
          validatedTarget.actor,
        )
        .toArray()[0] as any;

      return {
        conversation_id: conversationId,
        member: this.formatMemberRow(memberRow, principal),
        created: true,
      };
    });
  }

  public removeMember(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    targetActorRaw: string,
  ): {
    conversation_id: string;
    actor: string;
    status: 'removed';
    removed_at: string;
    removed_by: string;
  } {
    if (
      typeof targetActorRaw !== 'string' ||
      !targetActorRaw ||
      targetActorRaw !== targetActorRaw.trim()
    ) {
      throw new ConversationDomainError('invalid_actor', 400);
    }

    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireWriteScope: true });
      const { conversation } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        true, // explicit owner authority required
        principal.kind,
      );

      if (conversation.archived_at) {
        throw new ConversationDomainError('conversation_archived', 409);
      }

      if (conversation.kind === 'direct') {
        throw new ConversationDomainError(
          'direct_conversation_member_limit',
          409,
        );
      }

      const resolvedTarget = this.identityValidator.resolveParticipant(
        targetActorRaw,
        principal,
      );
      const canonicalTarget = resolvedTarget
        ? resolvedTarget.actor
        : targetActorRaw;

      const memberRow = this.sql
        .exec(
          'SELECT * FROM conversation_members WHERE conversation_id = ? AND actor = ? AND removed_at IS NULL',
          conversationId,
          canonicalTarget,
        )
        .toArray()[0] as any;

      if (!memberRow) {
        throw new ConversationDomainError('member_not_found', 404);
      }

      if (memberRow.role === 'owner') {
        const activeOwners = this.sql
          .exec(
            'SELECT COUNT(*) AS n FROM conversation_members WHERE conversation_id = ? AND removed_at IS NULL AND role = "owner"',
            conversationId,
          )
          .toArray()[0] as any;
        if (Number(activeOwners?.n ?? 0) <= 1) {
          throw new ConversationDomainError('cannot_remove_last_owner', 409);
        }
      }

      // F10: Never block authorized member revocation when tombstone storage caps are reached.
      // Evict the oldest removed-member tombstone(s) (preferring non-creator tombstones first)
      // so immediate access revocation always succeeds within bounded tombstone storage.
      const convTombstoneCap = Math.max(
        1,
        Math.floor(this.limits.maxRemovedMembersPerConversation),
      );
      const globalTombstoneCap = Math.max(
        1,
        Math.floor(this.limits.maxTotalRemovedMembers),
      );

      let convRemovedCount = Number(conversation.removed_member_count ?? 0);
      while (convRemovedCount >= convTombstoneCap) {
        const oldestLocal = this.sql
          .exec(
            `SELECT conversation_id, actor
             FROM conversation_members
             WHERE conversation_id = ? AND removed_at IS NOT NULL
             ORDER BY CASE WHEN actor = ? THEN 1 ELSE 0 END ASC, removed_at ASC, actor ASC
             LIMIT 1`,
            conversationId,
            conversation.created_by,
          )
          .toArray()[0] as any;
        if (!oldestLocal) {
          this.sql.exec(
            'UPDATE conversations SET removed_member_count = 0 WHERE id = ?',
            conversationId,
          );
          break;
        }
        this.sql.exec(
          'DELETE FROM conversation_members WHERE conversation_id = ? AND actor = ? AND removed_at IS NOT NULL',
          oldestLocal.conversation_id,
          oldestLocal.actor,
        );
        this.sql.exec(
          'UPDATE conversations SET removed_member_count = MAX(0, removed_member_count - 1) WHERE id = ?',
          oldestLocal.conversation_id,
        );
        this.sql.exec(
          'UPDATE conversation_storage_usage SET removed_member_count = MAX(0, removed_member_count - 1) WHERE singleton = 1',
        );
        convRemovedCount = Math.max(0, convRemovedCount - 1);
      }

      let globalRemovedCount = this.getStorageUsage().removed_member_count;
      while (globalRemovedCount >= globalTombstoneCap) {
        const oldestGlobal = this.sql
          .exec(
            `SELECT cm.conversation_id, cm.actor
             FROM conversation_members cm
             LEFT JOIN conversations c ON c.id = cm.conversation_id
             WHERE cm.removed_at IS NOT NULL
             ORDER BY CASE WHEN c.created_by = cm.actor THEN 1 ELSE 0 END ASC, cm.removed_at ASC, cm.conversation_id ASC, cm.actor ASC
             LIMIT 1`,
          )
          .toArray()[0] as any;
        if (!oldestGlobal) {
          this.sql.exec(
            'UPDATE conversation_storage_usage SET removed_member_count = 0 WHERE singleton = 1',
          );
          break;
        }
        this.sql.exec(
          'DELETE FROM conversation_members WHERE conversation_id = ? AND actor = ? AND removed_at IS NOT NULL',
          oldestGlobal.conversation_id,
          oldestGlobal.actor,
        );
        this.sql.exec(
          'UPDATE conversations SET removed_member_count = MAX(0, removed_member_count - 1) WHERE id = ?',
          oldestGlobal.conversation_id,
        );
        this.sql.exec(
          'UPDATE conversation_storage_usage SET removed_member_count = MAX(0, removed_member_count - 1) WHERE singleton = 1',
        );
        globalRemovedCount = Math.max(0, globalRemovedCount - 1);
      }

      const nowIso = this.nowIso();
      this.sql.exec(
        'UPDATE conversation_members SET removed_at = ?, removed_by = ? WHERE conversation_id = ? AND actor = ?',
        nowIso,
        principal.actor,
        conversationId,
        canonicalTarget,
      );
      this.sql.exec(
        'UPDATE conversations SET updated_at = ?, removed_member_count = removed_member_count + 1 WHERE id = ?',
        nowIso,
        conversationId,
      );
      this.sql.exec(
        'UPDATE conversation_storage_usage SET removed_member_count = removed_member_count + 1 WHERE singleton = 1',
      );

      return {
        conversation_id: conversationId,
        actor: canonicalTarget,
        status: 'removed',
        removed_at: nowIso,
        removed_by: principal.actor,
      };
    });
  }

  public setArchived(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    archived: boolean,
  ): Record<string, any> {
    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireWriteScope: true });
      const { conversation, member } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        true, // explicit owner authority required
        principal.kind,
      );

      const nowIso = this.nowIso();
      if (archived) {
        if (!conversation.archived_at) {
          this.sql.exec(
            'UPDATE conversations SET archived_at = ?, archived_by = ?, updated_at = ? WHERE id = ?',
            nowIso,
            principal.actor,
            nowIso,
            conversationId,
          );
        }
      } else {
        if (conversation.archived_at) {
          this.sql.exec(
            'UPDATE conversations SET archived_at = NULL, archived_by = NULL, updated_at = ? WHERE id = ?',
            nowIso,
            conversationId,
          );
        }
      }

      const updatedConv = this.getConversationRow(conversationId);
      return this.formatConversationDetail(updatedConv, member, principal);
    });
  }

  /** An explicit, bounded delegation from a currently signed-in human owner. */
  public configureAutomation(auth: ConversationAuthInput, cid: string, input: Record<string, unknown>, revoke = false) {
    return this.runInTxn(() => {
      // CallerContext alone is deliberately insufficient to issue delegation.
      if (!auth.session || auth.authType !== 'session' || auth.csrfVerified !== true) {
        throw new ConversationDomainError('forbidden', 403);
      }
      const owner = this.verifyPrincipal(auth, { requireWriteScope: true, isMutatingHttp: true });
      const { conversation } = this.requireActiveMembership(cid, owner.actor, true, 'human');
      const user = this.sql.exec('SELECT is_admin FROM users WHERE sub = ?', owner.sessionSub!).toArray()[0];
      if (!user?.is_admin || conversation.archived_at) throw new ConversationDomainError('forbidden', 403);
      const keys = revoke ? ['check'] : ['check', 'target_actor', 'body', 'on', 'expires_at'];
      if (Object.keys(input).some(k => !keys.includes(k)) || typeof input.check !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,39}$/.test(input.check)) {
        throw new ConversationDomainError('invalid_automation', 400);
      }
      if (revoke) {
        this.sql.exec('UPDATE conversation_automation_grants SET revoked_at = ? WHERE conversation_id = ? AND check_name = ?', this.now(), cid, input.check);
        return { revoked: true };
      }
      if (typeof input.target_actor !== 'string' || typeof input.body !== 'string' || !input.body.trim() || input.body.length > 4000 || /[\x00-\x08\x0b-\x1f\x7f]/.test(input.body) || !['fail', 'always'].includes(String(input.on))) {
        throw new ConversationDomainError('invalid_automation', 400);
      }
      const expires = input.expires_at;
      if (typeof expires !== 'number' || !Number.isSafeInteger(expires) || expires <= this.now() || expires > Math.min(auth.session.exp * 1000, this.now() + 86400000)) {
        throw new ConversationDomainError('invalid_automation_expiry', 400);
      }
      // Resolve at issuance, requiring exact canonical equality. Never promote a bare
      // project alias later, and never use this credential as the automation sender.
      const { member } = this.requireActiveMembership(cid, input.target_actor);
      let credentialId: string | null = null;
      if (member.kind === 'human') {
        this.validateAutomationTarget(input.target_actor, null);
      } else if (member.kind === 'agent' && registryAddress(input.target_actor)) {
        const credentials = this.sql.exec("SELECT id FROM credentials WHERE kind = 'agent' AND registry_actor = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at DESC, id LIMIT 51", input.target_actor, this.nowIso()).toArray();
        if (credentials.length > 50) throw new ConversationDomainError('automation_scan_limit', 409);
        for (const credential of credentials) {
          try { this.validateAutomationTarget(input.target_actor, String(credential.id)); credentialId = String(credential.id); break; }
          catch { /* Try only live, exactly bound read credentials; no caller is synthesized. */ }
        }
        if (!credentialId) throw new ConversationDomainError('automation_target_unavailable', 403);
      } else throw new ConversationDomainError('automation_target_unavailable', 403);
      const prior = this.sql.exec('SELECT id FROM conversation_automation_grants WHERE conversation_id = ? AND check_name = ?', cid, input.check).toArray()[0];
      if (!prior && Number(this.sql.exec('SELECT COUNT(*) AS n FROM conversation_automation_grants').toArray()[0].n) >= 1000) throw new ConversationDomainError('automation_capacity', 409);
      const id = generateUlid(this.now());
      this.sql.exec(`INSERT INTO conversation_automation_grants (id, conversation_id, check_name, target_actor, target_credential_id, owner_sub, expires_at, body_template, action_on)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(conversation_id, check_name) DO UPDATE SET
        id=excluded.id, target_actor=excluded.target_actor, target_credential_id=excluded.target_credential_id,
        owner_sub=excluded.owner_sub, expires_at=excluded.expires_at, body_template=excluded.body_template, action_on=excluded.action_on, revoked_at=NULL`,
        id, cid, input.check, input.target_actor, credentialId, owner.sessionSub!, expires, input.body, input.on as string);
      return { grant_id: id, sender_actor: AUTOMATION_SENDER, expires_at: expires };
    });
  }

  private validateAutomationTarget(actor: string, credentialId: string | null): void {
    if (actor.startsWith('human:') && !credentialId) {
      const sub = parseCanonicalHumanActor(actor)?.sub;
      if (!sub || !this.sql.exec('SELECT 1 FROM users WHERE sub = ? AND is_admin = 1', sub).toArray().length) throw new ConversationDomainError('automation_target_unavailable', 403);
      return;
    }
    if (!credentialId || !registryAddress(actor)) throw new ConversationDomainError('automation_target_unavailable', 403);
    const principal = this.verifyPrincipal({ caller: { tokenId: credentialId, kind: 'agent' }, authType: 'bearer' }, { requireReadScope: true });
    if (principal.actor !== actor || principal.kind !== 'agent') throw new ConversationDomainError('automation_target_unavailable', 403);
  }

  private validateAutomationGrant(grant: any): any {
    if (!grant || grant.revoked_at != null || Number(grant.expires_at) <= this.now()) throw new ConversationDomainError('automation_grant_unavailable', 403);
    const ownerActor = formatHumanActorFromSub(grant.owner_sub);
    if (!ownerActor || !this.sql.exec('SELECT 1 FROM users WHERE sub = ? AND is_admin = 1', grant.owner_sub).toArray().length) throw new ConversationDomainError('automation_owner_unavailable', 403);
    const { conversation } = this.requireActiveMembership(grant.conversation_id, ownerActor, true, 'human');
    if (conversation.archived_at) throw new ConversationDomainError('conversation_archived', 409);
    this.validateAutomationTarget(grant.target_actor, grant.target_credential_id);
    this.requireActiveMembership(grant.conversation_id, grant.target_actor, false, grant.target_credential_id ? 'agent' : 'human');
    return conversation;
  }

  public evaluateScheduledBacklog(query: BacklogQuery): { status: 'pass' | 'fail' | 'error'; detail: string } {
    return this.runInTxn(() => {
      const grants = this.sql.exec(`SELECT * FROM conversation_automation_grants WHERE check_name = ? AND target_actor = ? ${query.conversationId ? 'AND conversation_id = ?' : ''} LIMIT 51`,
        ...[query.checkName, query.actor, ...(query.conversationId ? [query.conversationId] : [])]).toArray() as any[];
      if (!grants.length || grants.length > 50) throw new ConversationDomainError('automation_grant_unavailable', 403);
      let overdue = 0;
      for (const grant of grants) {
        this.validateAutomationGrant(grant);
        overdue += Number(this.sql.exec(`SELECT COUNT(*) AS n FROM conversation_messages msg JOIN conversation_members m ON m.conversation_id = msg.conversation_id AND m.actor = ?
          WHERE msg.conversation_id = ? AND msg.seq > m.last_read_seq AND msg.sender_actor != ? AND msg.created_at < ?`,
          query.actor, grant.conversation_id, query.actor, new Date(this.now() - query.maxAgeS * 1000).toISOString()).toArray()[0].n);
      }
      return { status: overdue ? 'fail' : 'pass', detail: `${overdue} overdue retained unread message(s) in ${grants.length} authorized conversation(s)` };
    });
  }

  /** Records a Hub storage receipt atomically with the message; never a native delivery receipt. */
  public postScheduledFollowUp(intent: FollowUpIntent): string {
    return this.runInTxn(() => {
      const request = JSON.stringify(intent);
      // A committed receipt can be recovered after revocation/retention without
      // authorizing a new effect or returning message contents to an HTTP caller.
      const receipt = this.sql.exec('SELECT * FROM conversation_automation_receipts WHERE effect_key = ?', intent.idempotencyKey).toArray()[0];
      if (receipt) {
        if (receipt.request_json !== request) throw new ConversationDomainError('idempotency_conflict', 409);
        return String(receipt.message_id);
      }
      // Receipt retirement is safe because absence never authorizes a write.
      // Only the scheduler's exact current frozen intent can create an effect.
      // Retired keys cannot become current again: new slots use random generations.
      if (!this.sql.exec(`SELECT 1 FROM hub_check_slots
        WHERE name = ? AND ('check-' || slot_key) = ? AND intent_json = ? AND state != 'done'`,
        intent.checkName, intent.idempotencyKey, request).toArray().length) {
        throw new ConversationDomainError('automation_slot_retired', 409);
      }
      if (Number(this.sql.exec('SELECT COUNT(*) AS n FROM conversation_automation_receipts').toArray()[0].n) >= 4096) throw new ConversationDomainError('automation_capacity', 409);
      const message = this.writeMessage({ conversationId: intent.conversationId, body: intent.body, idempotencyKey: intent.idempotencyKey, kind: 'note' }, () => {
        const grant = this.sql.exec('SELECT * FROM conversation_automation_grants WHERE id = ?', intent.grantId).toArray()[0] as any;
        if (!grant || grant.conversation_id !== intent.conversationId || grant.check_name !== intent.checkName || grant.target_actor !== intent.actor || grant.body_template !== intent.bodyTemplate || grant.action_on !== intent.on) throw new ConversationDomainError('automation_grant_mismatch', 403);
        const conversation = this.validateAutomationGrant(grant);
        if (intent.body.length > 8000) throw new ConversationDomainError('body_too_large', 413);
        return { principal: { actor: AUTOMATION_SENDER, kind: 'system' }, conversation };
      });
      this.sql.exec('INSERT INTO conversation_automation_receipts VALUES (?, ?, ?, ?, ?)', intent.idempotencyKey, request, message.id, intent.conversationId, this.now());
      return String(message.id);
    });
  }

  public sendMessage(
    auth: ConversationAuthInput | CallerContext,
    input: SendConversationMessageInput,
  ): Record<string, any> {
    return this.writeMessage(input, () => {
      const principal = this.verifyPrincipal(auth, { requireWriteScope: true });
      const { conversation } = this.requireActiveMembership(input.conversationId, principal.actor, false, principal.kind);
      return { principal, conversation };
    });
  }

  // Only the ordinary verified-member entry point and the grant-checked automation
  // entry point can reach this writer. No caller-supplied sender override exists.
  private writeMessage(
    input: SendConversationMessageInput,
    authorize: () => { principal: { actor: string; kind: 'human' | 'agent' | 'system' }; conversation: any },
  ): Record<string, any> {
    if (typeof input.body !== 'string') {
      throw new ConversationDomainError('invalid_body', 400);
    }
    const byteLength = new TextEncoder().encode(input.body).byteLength;
    if (byteLength > MAX_MESSAGE_BODY_BYTES) {
      throw new ConversationDomainError('body_too_large', 413);
    }
    if (input.body.trim().length === 0) {
      throw new ConversationDomainError('invalid_body', 400);
    }

    if (
      typeof input.idempotencyKey !== 'string' ||
      !SAFE_KEY_REGEX.test(input.idempotencyKey)
    ) {
      throw new ConversationDomainError('invalid_idempotency_key', 400);
    }

    const kind = input.kind ?? 'message';
    if (typeof kind !== 'string' || !ALLOWED_MESSAGE_KINDS.has(kind)) {
      throw new ConversationDomainError('invalid_kind', 400);
    }

    if (
      input.bridgeRef !== undefined &&
      input.bridgeRef !== null &&
      (typeof input.bridgeRef !== 'string' ||
        !SAFE_KEY_REGEX.test(input.bridgeRef))
    ) {
      throw new ConversationDomainError('invalid_bridge_ref', 400);
    }

    if (
      input.replyTo !== undefined &&
      input.replyTo !== null &&
      (typeof input.replyTo !== 'string' ||
        !input.replyTo ||
        input.replyTo.length > 128)
    ) {
      throw new ConversationDomainError('invalid_reply_to', 400);
    }

    if (
      input.replyToSeq !== undefined &&
      input.replyToSeq !== null &&
      (typeof input.replyToSeq !== 'number' ||
        !Number.isInteger(input.replyToSeq) ||
        input.replyToSeq < 1)
    ) {
      throw new ConversationDomainError('invalid_reply_to', 400);
    }

    return this.runInTxn(() => {
      const { principal, conversation } = authorize();

      if (conversation.archived_at) {
        throw new ConversationDomainError('conversation_archived', 409);
      }

      // F12: Check retained idempotency record by (conversation_id, sender_actor, idempotency_key)
      // BEFORE requiring the parent reply target to still exist in conversation_messages,
      // so a retained reply can be replayed even after its older parent message has expired.
      const existing = this.sql
        .exec(
          'SELECT * FROM conversation_messages WHERE conversation_id = ? AND sender_actor = ? AND idempotency_key = ?',
          input.conversationId,
          principal.actor,
          input.idempotencyKey,
        )
        .toArray()[0] as any;

      if (existing) {
        const existingReplyTo =
          existing.reply_to !== null && existing.reply_to !== undefined
            ? String(existing.reply_to)
            : null;
        const existingReplyToSeq =
          existing.reply_to_seq !== null && existing.reply_to_seq !== undefined
            ? Number(existing.reply_to_seq)
            : null;
        const reqReplyTo =
          input.replyTo !== undefined && input.replyTo !== null
            ? input.replyTo
            : null;
        const reqReplyToSeq =
          input.replyToSeq !== undefined && input.replyToSeq !== null
            ? input.replyToSeq
            : null;

        let replyMatches = false;
        if (reqReplyTo === null && reqReplyToSeq === null) {
          replyMatches = existingReplyTo === null && existingReplyToSeq === null;
        } else if (reqReplyTo !== null && reqReplyToSeq === null) {
          replyMatches =
            existingReplyTo !== null && existingReplyTo === reqReplyTo;
        } else if (reqReplyTo === null && reqReplyToSeq !== null) {
          replyMatches =
            existingReplyToSeq !== null && existingReplyToSeq === reqReplyToSeq;
        } else {
          replyMatches =
            existingReplyTo === reqReplyTo &&
            existingReplyToSeq === reqReplyToSeq;
        }

        const matches =
          existing.body === input.body &&
          existing.kind === kind &&
          (existing.bridge_ref ?? null) === (input.bridgeRef ?? null) &&
          replyMatches;

        if (!matches) {
          throw new ConversationDomainError(
            'conflict',
            409,
            'idempotency_conflict',
          );
        }
        const formatted = this.formatMessageRow(existing);
        return {
          ...formatted,
          replay: true,
          message: formatted,
        };
      }

      // Resolve reply_to / reply_to_seq strictly within the SAME conversation for new messages
      let resolvedReplyToId: string | null = null;
      let resolvedReplyToSeq: number | null = null;

      if (input.replyTo !== undefined && input.replyTo !== null) {
        const parent = this.sql
          .exec(
            'SELECT id, seq FROM conversation_messages WHERE conversation_id = ? AND id = ?',
            input.conversationId,
            input.replyTo,
          )
          .toArray()[0] as any;
        if (!parent) {
          throw new ConversationDomainError(
            'invalid_reply_to',
            400,
            'reply_target_not_in_conversation',
          );
        }
        if (
          input.replyToSeq !== undefined &&
          input.replyToSeq !== null &&
          Number(parent.seq) !== input.replyToSeq
        ) {
          throw new ConversationDomainError(
            'invalid_reply_to',
            400,
            'reply_seq_mismatch',
          );
        }
        resolvedReplyToId = String(parent.id);
        resolvedReplyToSeq = Number(parent.seq);
      } else if (input.replyToSeq !== undefined && input.replyToSeq !== null) {
        const parent = this.sql
          .exec(
            'SELECT id, seq FROM conversation_messages WHERE conversation_id = ? AND seq = ?',
            input.conversationId,
            input.replyToSeq,
          )
          .toArray()[0] as any;
        if (!parent) {
          throw new ConversationDomainError(
            'invalid_reply_to',
            400,
            'reply_target_not_in_conversation',
          );
        }
        resolvedReplyToId = String(parent.id);
        resolvedReplyToSeq = Number(parent.seq);
      }

      // F6: Compact non-duplicated metadata JSON (body is stored once in the `body` column)
      const canonicalMetaPayload = JSON.stringify({
        kind,
        reply_to: resolvedReplyToId,
        reply_to_seq: resolvedReplyToSeq,
        bridge_ref: input.bridgeRef ?? null,
        body_bytes: byteLength,
      });

      // F6: Enforce per-conversation and global message/byte caps in O(1) before advancing last_seq
      const usage = this.getStorageUsage();
      if (
        Number(conversation.retained_message_count ?? 0) >=
        this.limits.maxMessagesPerConversation
      ) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'conversation_message_limit_exceeded',
        );
      }
      if (usage.message_count >= this.limits.maxTotalMessages) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'total_message_limit_exceeded',
        );
      }
      if (
        Number(conversation.retained_message_bytes ?? 0) + byteLength >
        this.limits.maxBytesPerConversation
      ) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'conversation_byte_limit_exceeded',
        );
      }
      if (usage.message_bytes + byteLength > this.limits.maxTotalMessageBytes) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'total_byte_limit_exceeded',
        );
      }

      const nextSeq = Number(conversation.last_seq ?? 0) + 1;
      const msgId = generateUlid(this.now());
      const nowIso = this.nowIso();
      const preview = input.body.replace(/\s+/g, ' ').trim().slice(0, 160);

      this.sql.exec(
        `INSERT INTO conversation_messages (
          id, conversation_id, seq, sender_actor, sender_kind,
          kind, body, body_bytes, idempotency_key, request_json,
          reply_to, reply_to_seq, bridge_ref, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        msgId,
        input.conversationId,
        nextSeq,
        principal.actor,
        principal.kind,
        kind,
        input.body,
        byteLength,
        input.idempotencyKey,
        canonicalMetaPayload,
        resolvedReplyToId,
        resolvedReplyToSeq,
        input.bridgeRef ?? null,
        nowIso,
      );

      this.sql.exec(
        `UPDATE conversations
         SET last_seq = ?, last_message_at = ?, last_message_preview = ?, updated_at = ?,
             retained_message_count = retained_message_count + 1,
             retained_message_bytes = retained_message_bytes + ?
         WHERE id = ?`,
        nextSeq,
        nowIso,
        preview,
        nowIso,
        byteLength,
        input.conversationId,
      );

      this.sql.exec(
        `UPDATE conversation_storage_usage
         SET message_count = message_count + 1,
             message_bytes = message_bytes + ?
         WHERE singleton = 1`,
        byteLength,
      );

      // Monotonically advance sender's own read cursor so own sent message is not unread
      this.sql.exec(
        `UPDATE conversation_members
         SET last_read_seq = CASE WHEN last_read_seq < ? THEN ? ELSE last_read_seq END,
             last_read_at = ?
         WHERE conversation_id = ? AND actor = ?`,
        nextSeq,
        nextSeq,
        nowIso,
        input.conversationId,
        principal.actor,
      );

      const createdRow = this.sql
        .exec('SELECT * FROM conversation_messages WHERE id = ?', msgId)
        .toArray()[0] as any;
      const formatted = this.formatMessageRow(createdRow);
      return {
        ...formatted,
        replay: false,
        message: formatted,
      };
    });
  }

  public listMessages(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    options?: ListMessagesOptions,
  ): Record<string, any> {
    const limit = options?.limit ?? DEFAULT_PAGE_LIMIT;
    if (
      typeof limit !== 'number' ||
      !Number.isInteger(limit) ||
      limit < 1 ||
      limit > MAX_PAGE_LIMIT
    ) {
      throw new ConversationDomainError('invalid_limit', 400);
    }

    const hasAfter =
      options?.afterSeq !== undefined && options.afterSeq !== null;
    const hasBefore =
      options?.beforeSeq !== undefined && options.beforeSeq !== null;

    if (hasAfter && hasBefore) {
      throw new ConversationDomainError('invalid_pagination', 400);
    }

    if (hasAfter) {
      if (
        typeof options!.afterSeq !== 'number' ||
        !Number.isInteger(options!.afterSeq) ||
        options!.afterSeq < 0
      ) {
        throw new ConversationDomainError('invalid_pagination', 400);
      }
    }

    if (hasBefore) {
      if (
        typeof options!.beforeSeq !== 'number' ||
        !Number.isInteger(options!.beforeSeq) ||
        options!.beforeSeq < 1
      ) {
        throw new ConversationDomainError('invalid_pagination', 400);
      }
    }

    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      const { conversation, member } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        false,
        principal.kind,
      );

      const lastSeq = Number(conversation.last_seq ?? 0);
      const myReadSeq = Number(member.last_read_seq ?? 0);

      let rows: any[];
      if (hasAfter) {
        rows = this.sql
          .exec(
            'SELECT * FROM conversation_messages WHERE conversation_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?',
            conversationId,
            options!.afterSeq,
            limit,
          )
          .toArray() as any[];
      } else if (hasBefore) {
        rows = this.sql
          .exec(
            `SELECT * FROM (
               SELECT * FROM conversation_messages
               WHERE conversation_id = ? AND seq < ?
               ORDER BY seq DESC LIMIT ?
             ) ORDER BY seq ASC`,
            conversationId,
            options!.beforeSeq,
            limit,
          )
          .toArray() as any[];
      } else {
        rows = this.sql
          .exec(
            `SELECT * FROM (
               SELECT * FROM conversation_messages
               WHERE conversation_id = ?
               ORDER BY seq DESC LIMIT ?
             ) ORDER BY seq ASC`,
            conversationId,
            limit,
          )
          .toArray() as any[];
      }

      const messages = rows.map(r => this.formatMessageRow(r));
      const oldestSeq = messages.length > 0 ? messages[0].seq : null;
      const newestSeq =
        messages.length > 0 ? messages[messages.length - 1].seq : null;

      const {
        oldestRetainedSeq,
        newestRetainedSeq,
        prunedThroughSeq,
        unreadCount,
      } = this.getRetainedVisibility(conversationId, lastSeq, myReadSeq);

      // F11: Derive has_more_before / has_more_after strictly from retained rows so an
      // empty pruned page never returns has_more_after: true or a non-progressing cursor.
      const hasMoreBefore =
        oldestSeq !== null &&
        oldestRetainedSeq !== null &&
        oldestSeq > oldestRetainedSeq;
      const hasMoreAfter =
        newestSeq !== null &&
        newestRetainedSeq !== null &&
        newestSeq < newestRetainedSeq;

      const nextAfterSeq =
        newestSeq !== null
          ? newestSeq
          : hasAfter
            ? Math.max(options!.afterSeq!, lastSeq)
            : prunedThroughSeq;
      const prevBeforeSeq =
        oldestSeq !== null
          ? oldestSeq
          : hasBefore
            ? oldestRetainedSeq !== null
              ? Math.min(options!.beforeSeq!, oldestRetainedSeq)
              : null
            : null;

      const gapWindowAfter = hasAfter ? options!.afterSeq! : 0;
      const missingRange = this.getFirstMissingSeqRange(
        conversationId,
        gapWindowAfter,
        lastSeq,
        newestRetainedSeq,
      );
      const gapDetected = missingRange !== null;
      const gapFromSeq = missingRange?.from ?? null;
      const gapToSeq = missingRange?.to ?? null;

      return {
        conversation_id: conversationId,
        self_actor: principal.actor,
        messages,
        last_seq: lastSeq,
        my_read_seq: myReadSeq,
        read_seq: myReadSeq,
        unread_count: unreadCount,
        oldest_seq: oldestSeq,
        newest_seq: newestSeq,
        oldest_retained_seq: oldestRetainedSeq,
        newest_retained_seq: newestRetainedSeq,
        pruned_through_seq: prunedThroughSeq,
        resume_after_seq: prunedThroughSeq,
        gap_detected: gapDetected,
        gap_from_seq: gapFromSeq,
        gap_to_seq: gapToSeq,
        has_more_before: hasMoreBefore,
        has_more_after: hasMoreAfter,
        next_after_seq: nextAfterSeq,
        prev_before_seq: prevBeforeSeq,
      };
    });
  }

  public markRead(
    auth: ConversationAuthInput | CallerContext,
    conversationId: string,
    options?: { seq?: number; messageId?: string },
  ): {
    conversation_id: string;
    actor: string;
    read_seq: number;
    last_seq: number;
    unread_count: number;
    last_read_at: string | null;
  } {
    return this.runInTxn(() => {
      const principal = this.verifyPrincipal(auth, { requireReadScope: true });
      const { conversation, member } = this.requireActiveMembership(
        conversationId,
        principal.actor,
        false,
        principal.kind,
      );

      const lastSeq = Number(conversation.last_seq ?? 0);
      let targetSeq = lastSeq;

      if (options?.messageId !== undefined && options.messageId !== null) {
        if (
          typeof options.messageId !== 'string' ||
          !options.messageId ||
          options.messageId.length > 128
        ) {
          throw new ConversationDomainError('invalid_read_seq', 400);
        }
        const msg = this.sql
          .exec(
            'SELECT seq FROM conversation_messages WHERE conversation_id = ? AND id = ?',
            conversationId,
            options.messageId,
          )
          .toArray()[0] as any;
        if (!msg) {
          throw new ConversationDomainError('invalid_read_seq', 400);
        }
        targetSeq = Number(msg.seq);
      }

      if (options?.seq !== undefined && options.seq !== null) {
        if (
          typeof options.seq !== 'number' ||
          !Number.isInteger(options.seq) ||
          options.seq < 0 ||
          options.seq > lastSeq
        ) {
          throw new ConversationDomainError('invalid_read_seq', 400);
        }
        if (
          options.messageId !== undefined &&
          options.messageId !== null &&
          options.seq !== targetSeq
        ) {
          throw new ConversationDomainError('invalid_read_seq', 400);
        }
        targetSeq = options.seq;
      }

      const currentReadSeq = Number(member.last_read_seq ?? 0);
      const newReadSeq = Math.max(currentReadSeq, targetSeq);
      let lastReadAt: string | null = member.last_read_at ?? null;

      if (newReadSeq > currentReadSeq || !lastReadAt) {
        lastReadAt = this.nowIso();
        this.sql.exec(
          'UPDATE conversation_members SET last_read_seq = ?, last_read_at = ? WHERE conversation_id = ? AND actor = ?',
          newReadSeq,
          lastReadAt,
          conversationId,
          principal.actor,
        );
      }

      const { unreadCount } = this.getRetainedVisibility(
        conversationId,
        lastSeq,
        newReadSeq,
      );

      return {
        conversation_id: conversationId,
        actor: principal.actor,
        read_seq: newReadSeq,
        last_seq: lastSeq,
        unread_count: unreadCount,
        last_read_at: lastReadAt,
      };
    });
  }

  /**
   * HTTP route handler for `/api/conversations` and `/api/conversations/*`.
   * Accepts an already-verified principal + CSRF disposition (`ConversationAuthInput`).
   * Never inspects unverified headers for identity.
   */
  public async handle(
    request: Request,
    auth: ConversationAuthInput,
  ): Promise<Response> {
    if (auth?.error) {
      return auth.error;
    }

    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();
    const isMutating = !['GET', 'HEAD', 'OPTIONS'].includes(method);

    // Authenticate and enforce CSRF before any content or body access
    let principal: VerifiedConversationPrincipal;
    try {
      const isReadCursorPost = /^\/api\/conversations\/[^/]+\/read$/.test(path);
      principal = this.verifyPrincipal(auth, {
        isMutatingHttp: isMutating,
        requireWriteScope: isMutating && !isReadCursorPost,
        requireReadScope: !isMutating || isReadCursorPost,
      });
    } catch (err) {
      if (err instanceof ConversationDomainError) {
        return err.toResponse();
      }
      return jsonResponse({ error: 'unauthorized' }, 401);
    }

    try {
      // 1. GET /api/conversations/participants or /api/conversations/identities
      if (
        path === '/api/conversations/participants' ||
        path === '/api/conversations/identities'
      ) {
        if (method !== 'GET') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        for (const k of url.searchParams.keys()) {
          return jsonResponse({ error: 'unknown_query_param', param: k }, 400);
        }
        return jsonResponse(this.listParticipants(auth));
      }

      // Reverse job/launch → member conversations (must precede /:id)
      if (path === '/api/conversations/links') {
        if (method !== 'GET') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        for (const k of url.searchParams.keys()) {
          if (!['kind', 'ref', 'limit', 'cursor'].includes(k)) {
            return jsonResponse({ error: 'unknown_query_param', param: k }, 400);
          }
        }
        if (!this.linksHandler) {
          return jsonResponse({ error: 'not_found' }, 404);
        }
        let reverseLimit = DEFAULT_PAGE_LIMIT;
        if (url.searchParams.has('limit')) {
          const rawLimit = url.searchParams.get('limit')!;
          if (!/^\d+$/.test(rawLimit)) {
            return jsonResponse({ error: 'invalid_limit' }, 400);
          }
          reverseLimit = parseInt(rawLimit, 10);
        }
        return jsonResponse(
          this.linksHandler.reverse(auth, {
            kind: url.searchParams.get('kind'),
            ref: url.searchParams.get('ref'),
            limit: reverseLimit,
            cursor: url.searchParams.get('cursor'),
          }),
        );
      }

      // 2. GET or POST /api/conversations
      if (path === '/api/conversations') {
        if (method === 'GET') {
          for (const k of url.searchParams.keys()) {
            if (
              !['state', 'include_archived', 'limit', 'cursor'].includes(k)
            ) {
              return jsonResponse(
                { error: 'unknown_query_param', param: k },
                400,
              );
            }
          }
          let limit = DEFAULT_PAGE_LIMIT;
          if (url.searchParams.has('limit')) {
            const rawLimit = url.searchParams.get('limit')!;
            if (!/^\d+$/.test(rawLimit)) {
              return jsonResponse({ error: 'invalid_limit' }, 400);
            }
            limit = parseInt(rawLimit, 10);
          }
          const includeArchivedParam = url.searchParams.get('include_archived');
          if (
            includeArchivedParam !== null &&
            includeArchivedParam !== 'true' &&
            includeArchivedParam !== 'false'
          ) {
            return jsonResponse({ error: 'invalid_state' }, 400);
          }
          const stateParam = (url.searchParams.get('state') ?? undefined) as
            | 'active'
            | 'archived'
            | 'all'
            | undefined;
          const cursor = url.searchParams.get('cursor') ?? undefined;

          const result = this.listConversations(auth, {
            state: stateParam,
            includeArchived: includeArchivedParam === 'true',
            limit,
            cursor,
          });
          return jsonResponse(result);
        }

        if (method === 'POST') {
          const parsed = await readBoundedJsonBody(request, false);
          if (parsed.error) return parsed.error;
          const body = parsed.data!;

          const allowedKeys = new Set([
            'kind',
            'title',
            'name',
            'members',
            'participants',
            'idempotency_key',
          ]);
          for (const k of Object.keys(body)) {
            if (FORBIDDEN_SENDER_FIELDS.has(k)) {
              return jsonResponse({ error: 'sender_override_forbidden' }, 400);
            }
            if (!allowedKeys.has(k)) {
              return jsonResponse({ error: 'unknown_fields', field: k }, 400);
            }
          }

          if (
            body.title !== undefined &&
            body.name !== undefined &&
            body.title !== body.name
          ) {
            return jsonResponse({ error: 'invalid_title' }, 400);
          }
          if (
            body.members !== undefined &&
            body.participants !== undefined
          ) {
            return jsonResponse({ error: 'invalid_members' }, 400);
          }

          const title = body.title ?? body.name;
          const members = body.members ?? body.participants ?? [];
          const created = this.createConversation(auth, {
            kind: body.kind,
            title,
            members,
            idempotencyKey: body.idempotency_key,
          });
          return jsonResponse(created, created.replay ? 200 : 201);
        }

        return jsonResponse({ error: 'method_not_allowed' }, 405);
      }

      const automationMatch = /^\/api\/conversations\/([^/]+)\/automation$/.exec(path);
      if (automationMatch) {
        const cid = decodeURIComponent(automationMatch[1]);
        if (method !== 'PUT' && method !== 'DELETE') return jsonResponse({ error: 'method_not_allowed' }, 405);
        const parsed = await readBoundedJsonBody(request, false);
        if (parsed.error) return parsed.error;
        return jsonResponse(this.configureAutomation(auth, cid, parsed.data!, method === 'DELETE'));
      }

      // 3. /api/conversations/:id
      const convMatch = /^\/api\/conversations\/([^/]+)$/.exec(path);
      if (convMatch) {
        const convId = decodeURIComponent(convMatch[1]);
        if (method !== 'GET') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        for (const k of url.searchParams.keys()) {
          return jsonResponse({ error: 'unknown_query_param', param: k }, 400);
        }
        return jsonResponse(this.getConversation(auth, convId));
      }

      // 4. /api/conversations/:id/members
      const membersMatch = /^\/api\/conversations\/([^/]+)\/members$/.exec(path);
      if (membersMatch) {
        const convId = decodeURIComponent(membersMatch[1]);
        if (method === 'GET') {
          for (const k of url.searchParams.keys()) {
            if (k !== 'include_removed') {
              return jsonResponse(
                { error: 'unknown_query_param', param: k },
                400,
              );
            }
          }
          const includeRemoved =
            url.searchParams.get('include_removed') === 'true';
          return jsonResponse(this.listMembers(auth, convId, includeRemoved));
        }

        if (method === 'POST') {
          const parsed = await readBoundedJsonBody(request, false);
          if (parsed.error) return parsed.error;
          const body = parsed.data!;
          const allowedKeys = new Set(['actor', 'member', 'role']);
          for (const k of Object.keys(body)) {
            if (!allowedKeys.has(k)) {
              return jsonResponse({ error: 'unknown_fields', field: k }, 400);
            }
          }
          if (
            body.actor !== undefined &&
            body.member !== undefined &&
            body.actor !== body.member
          ) {
            return jsonResponse({ error: 'invalid_actor' }, 400);
          }
          const targetActor = body.actor ?? body.member;
          const role = body.role;
          const res = this.addMember(auth, convId, targetActor, role);
          return jsonResponse(
            { conversation_id: res.conversation_id, member: res.member },
            res.created ? 201 : 200,
          );
        }

        return jsonResponse({ error: 'method_not_allowed' }, 405);
      }

      // 5. POST /api/conversations/:id/members/remove or DELETE /api/conversations/:id/members/:actor
      const removePostMatch =
        /^\/api\/conversations\/([^/]+)\/members\/remove$/.exec(path);
      if (removePostMatch) {
        const convId = decodeURIComponent(removePostMatch[1]);
        if (method !== 'POST') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        const parsed = await readBoundedJsonBody(request, false);
        if (parsed.error) return parsed.error;
        const body = parsed.data!;
        const allowedKeys = new Set(['actor', 'member']);
        for (const k of Object.keys(body)) {
          if (!allowedKeys.has(k)) {
            return jsonResponse({ error: 'unknown_fields', field: k }, 400);
          }
        }
        const targetActor = body.actor ?? body.member;
        return jsonResponse(this.removeMember(auth, convId, targetActor));
      }

      const memberDeleteMatch =
        /^\/api\/conversations\/([^/]+)\/members\/([^/]+)$/.exec(path);
      if (memberDeleteMatch) {
        const convId = decodeURIComponent(memberDeleteMatch[1]);
        const targetActor = decodeURIComponent(memberDeleteMatch[2]);
        if (method !== 'DELETE') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        const parsed = await readBoundedJsonBody(request, true);
        if (parsed.error) return parsed.error;
        return jsonResponse(this.removeMember(auth, convId, targetActor));
      }

      // 5b. GET/POST /api/conversations/:id/links and GET/DELETE .../links/:linkId
      const linksMatch = /^\/api\/conversations\/([^/]+)\/links$/.exec(path);
      if (linksMatch) {
        const convId = decodeURIComponent(linksMatch[1]);
        if (!this.linksHandler) {
          return jsonResponse({ error: 'not_found' }, 404);
        }
        if (method === 'GET') {
          for (const k of url.searchParams.keys()) {
            if (!['limit', 'cursor'].includes(k)) {
              return jsonResponse(
                { error: 'unknown_query_param', param: k },
                400,
              );
            }
          }
          let limit = DEFAULT_PAGE_LIMIT;
          if (url.searchParams.has('limit')) {
            const rawLimit = url.searchParams.get('limit')!;
            if (!/^\d+$/.test(rawLimit)) {
              return jsonResponse({ error: 'invalid_limit' }, 400);
            }
            limit = parseInt(rawLimit, 10);
          }
          return jsonResponse(
            this.linksHandler.list(auth, convId, {
              limit,
              cursor: url.searchParams.get('cursor'),
            }),
          );
        }
        if (method === 'POST') {
          const parsed = await readBoundedJsonBody(request, false);
          if (parsed.error) return parsed.error;
          const body = parsed.data!;
          const allowedKeys = new Set(['kind', 'ref', 'idempotency_key']);
          for (const k of Object.keys(body)) {
            if (FORBIDDEN_SENDER_FIELDS.has(k)) {
              return jsonResponse({ error: 'sender_override_forbidden' }, 400);
            }
            if (!allowedKeys.has(k)) {
              return jsonResponse({ error: 'unknown_fields', field: k }, 400);
            }
          }
          const created = this.linksHandler.attach(auth, convId, {
            kind: body.kind,
            ref: body.ref,
            idempotencyKey: body.idempotency_key,
          });
          return jsonResponse(created, created.replay ? 200 : 201);
        }
        return jsonResponse({ error: 'method_not_allowed' }, 405);
      }

      const linkItemMatch =
        /^\/api\/conversations\/([^/]+)\/links\/([^/]+)$/.exec(path);
      if (linkItemMatch) {
        const convId = decodeURIComponent(linkItemMatch[1]);
        const linkId = decodeURIComponent(linkItemMatch[2]);
        if (!this.linksHandler) {
          return jsonResponse({ error: 'not_found' }, 404);
        }
        if (method === 'GET') {
          for (const k of url.searchParams.keys()) {
            return jsonResponse({ error: 'unknown_query_param', param: k }, 400);
          }
          return jsonResponse(this.linksHandler.getLink(auth, convId, linkId));
        }
        if (method === 'DELETE') {
          const parsed = await readBoundedJsonBody(request, true);
          if (parsed.error) return parsed.error;
          const body = parsed.data!;
          for (const k of Object.keys(body)) {
            return jsonResponse({ error: 'unknown_fields', field: k }, 400);
          }
          return jsonResponse(this.linksHandler.remove(auth, convId, linkId));
        }
        return jsonResponse({ error: 'method_not_allowed' }, 405);
      }

      // 6. GET or POST /api/conversations/:id/messages
      const messagesMatch = /^\/api\/conversations\/([^/]+)\/messages$/.exec(
        path,
      );
      if (messagesMatch) {
        const convId = decodeURIComponent(messagesMatch[1]);
        if (method === 'GET') {
          for (const k of url.searchParams.keys()) {
            if (
              !['after_seq', 'since_seq', 'before_seq', 'limit'].includes(k)
            ) {
              return jsonResponse(
                { error: 'unknown_query_param', param: k },
                400,
              );
            }
          }

          let limit = DEFAULT_PAGE_LIMIT;
          if (url.searchParams.has('limit')) {
            const rawLimit = url.searchParams.get('limit')!;
            if (!/^\d+$/.test(rawLimit)) {
              return jsonResponse({ error: 'invalid_limit' }, 400);
            }
            limit = parseInt(rawLimit, 10);
          }

          let afterSeq: number | undefined;
          const rawAfter =
            url.searchParams.get('after_seq') ??
            url.searchParams.get('since_seq');
          if (rawAfter !== null) {
            if (!/^\d+$/.test(rawAfter)) {
              return jsonResponse({ error: 'invalid_pagination' }, 400);
            }
            afterSeq = parseInt(rawAfter, 10);
          }

          let beforeSeq: number | undefined;
          const rawBefore = url.searchParams.get('before_seq');
          if (rawBefore !== null) {
            if (!/^\d+$/.test(rawBefore)) {
              return jsonResponse({ error: 'invalid_pagination' }, 400);
            }
            beforeSeq = parseInt(rawBefore, 10);
          }

          return jsonResponse(
            this.listMessages(auth, convId, {
              afterSeq,
              beforeSeq,
              limit,
            }),
          );
        }

        if (method === 'POST') {
          const parsed = await readBoundedJsonBody(request, false);
          if (parsed.error) return parsed.error;
          const body = parsed.data!;

          const allowedKeys = new Set([
            'body',
            'idempotency_key',
            'kind',
            'reply_to',
            'reply_to_seq',
            'bridge_ref',
          ]);
          for (const k of Object.keys(body)) {
            if (FORBIDDEN_SENDER_FIELDS.has(k)) {
              return jsonResponse({ error: 'sender_override_forbidden' }, 400);
            }
            if (!allowedKeys.has(k)) {
              return jsonResponse({ error: 'unknown_fields', field: k }, 400);
            }
          }

          const res = this.sendMessage(auth, {
            conversationId: convId,
            body: body.body,
            idempotencyKey: body.idempotency_key,
            kind: body.kind,
            replyTo: body.reply_to,
            replyToSeq: body.reply_to_seq,
            bridgeRef: body.bridge_ref,
          });
          return jsonResponse(res, res.replay ? 200 : 201);
        }

        return jsonResponse({ error: 'method_not_allowed' }, 405);
      }

      // 7. POST /api/conversations/:id/read
      const readMatch = /^\/api\/conversations\/([^/]+)\/read$/.exec(path);
      if (readMatch) {
        const convId = decodeURIComponent(readMatch[1]);
        if (method !== 'POST') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        const parsed = await readBoundedJsonBody(request, true);
        if (parsed.error) return parsed.error;
        const body = parsed.data!;

        const allowedKeys = new Set(['seq', 'read_seq', 'message_id']);
        for (const k of Object.keys(body)) {
          if (!allowedKeys.has(k)) {
            return jsonResponse({ error: 'unknown_fields', field: k }, 400);
          }
        }
        if (
          body.seq !== undefined &&
          body.read_seq !== undefined &&
          body.seq !== body.read_seq
        ) {
          return jsonResponse({ error: 'invalid_read_seq' }, 400);
        }

        const seq = body.seq ?? body.read_seq;
        return jsonResponse(
          this.markRead(auth, convId, {
            seq,
            messageId: body.message_id,
          }),
        );
      }

      // 8. POST /api/conversations/:id/(archive|unarchive)
      const archiveMatch =
        /^\/api\/conversations\/([^/]+)\/(archive|unarchive)$/.exec(path);
      if (archiveMatch) {
        const convId = decodeURIComponent(archiveMatch[1]);
        const action = archiveMatch[2];
        if (method !== 'POST') {
          return jsonResponse({ error: 'method_not_allowed' }, 405);
        }
        const parsed = await readBoundedJsonBody(request, true);
        if (parsed.error) return parsed.error;
        const body = parsed.data!;
        for (const k of Object.keys(body)) {
          if (k !== 'archived') {
            return jsonResponse({ error: 'unknown_fields', field: k }, 400);
          }
        }
        if (
          body.archived !== undefined &&
          typeof body.archived !== 'boolean'
        ) {
          return jsonResponse({ error: 'invalid_archived' }, 400);
        }
        const targetArchived =
          action === 'unarchive' ? false : (body.archived ?? true);
        return jsonResponse(this.setArchived(auth, convId, targetArchived));
      }

      return jsonResponse({ error: 'not_found' }, 404);
    } catch (err) {
      if (err instanceof ConversationDomainError) {
        return err.toResponse();
      }
      throw err;
    }
  }
}
