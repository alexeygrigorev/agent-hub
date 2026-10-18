import { generateUlid, type SqlStorage, type CallerContext } from './bus';
import { IntakeService } from './intake';
import { ConversationFollowThroughService } from './conversation-followthrough';
import {
  ConversationDomainError,
  ConversationsService,
  DEFAULT_PAGE_LIMIT,
  MAX_PAGE_LIMIT,
  type ConversationAuthInput,
  type VerifiedConversationPrincipal,
} from './conversations';

export const CONVERSATION_LINKS_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS conversation_links (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  idempotency_key TEXT,
  UNIQUE(conversation_id, kind, ref),
  UNIQUE(conversation_id, created_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_conversation_links_conv_created
  ON conversation_links(conversation_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_links_kind_ref
  ON conversation_links(kind, ref, id);
`;

export const LINK_KINDS = [
  'routing_job',
  'fleet_launch',
  'review',
  'outcome',
  'intake_task',
] as const;
export type ConversationLinkKind = (typeof LINK_KINDS)[number];

export const DEFAULT_MAX_LINKS_PER_CONVERSATION = 32;
export const DEFAULT_MAX_TOTAL_LINKS = 4_096;
export const JOB_STUCK_AFTER_MS = 1_800_000;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SAFE_KEY_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const JOB_TERMINAL = new Set(['done', 'accepted', 'failed', 'cancelled']);
const JOB_ACTIVE = new Set(['queued', 'launched', 'running']);
const JOB_KNOWN = new Set([...JOB_TERMINAL, ...JOB_ACTIVE,
  'completed-awaiting-review', 'waiting-on-human', 'stuck']);
const LAUNCH_KNOWN = new Set(['offered', 'starting', 'running', 'uncertain', 'exited', 'failed']);
const LAUNCH_TERMINAL = new Set(['exited', 'failed']);

export interface ConversationLinkLimits {
  maxLinksPerConversation: number;
  maxTotalLinks: number;
}

export interface ConversationLinkTargetView {
  available: boolean;
  canonical_status: string | null;
  terminal: boolean;
  complete: boolean;
}

function isLinkKind(value: unknown): value is ConversationLinkKind {
  return (
    typeof value === 'string' &&
    (LINK_KINDS as readonly string[]).includes(value)
  );
}

function requireRef(kind: ConversationLinkKind, ref: unknown): string {
  if (typeof ref !== 'string') {
    throw new ConversationDomainError('invalid_ref', 400);
  }
  if (kind === 'intake_task') {
    if (!UUID_RE.test(ref) && !SAFE_KEY_RE.test(ref)) {
      throw new ConversationDomainError('invalid_ref', 400);
    }
    return ref;
  }
  if (!UUID_RE.test(ref)) {
    throw new ConversationDomainError('invalid_ref', 400);
  }
  return ref;
}

function parseLimit(raw: unknown, fallback = DEFAULT_PAGE_LIMIT): number {
  const limit = raw === undefined || raw === null ? fallback : raw;
  if (
    typeof limit !== 'number' ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > MAX_PAGE_LIMIT
  ) {
    throw new ConversationDomainError('invalid_limit', 400);
  }
  return limit;
}

function parseCursor(raw: string | null | undefined): {
  createdAt: string;
  id: string;
} | null {
  if (raw === undefined || raw === null || raw === '') return null;
  if (typeof raw !== 'string' || raw.length > 160 || !raw.includes('|')) {
    throw new ConversationDomainError('invalid_cursor', 400);
  }
  const splitAt = raw.indexOf('|');
  const tsPart = raw.slice(0, splitAt);
  const idPart = raw.slice(splitAt + 1);
  if (!tsPart || !idPart || !Number.isFinite(Date.parse(tsPart))) {
    throw new ConversationDomainError('invalid_cursor', 400);
  }
  return { createdAt: tsPart, id: idPart };
}

function targetUnavailable(): ConversationLinkTargetView {
  return {
    available: false,
    canonical_status: null,
    terminal: false,
    complete: false,
  };
}

function jobStatusView(
  status: string,
  kind: ConversationLinkKind,
): ConversationLinkTargetView {
  const terminal =
    ['accepted', 'failed', 'cancelled'].includes(status) ||
    (status === 'done' && kind !== 'review');
  return {
    available: true,
    canonical_status: JOB_KNOWN.has(status) ? status : null,
    terminal,
    complete: status === 'accepted' || (kind !== 'review' && status === 'done'),
  };
}

function launchStatusView(
  state: string,
): ConversationLinkTargetView {
  const terminal = LAUNCH_TERMINAL.has(state);
  return {
    available: true,
    canonical_status: LAUNCH_KNOWN.has(state) ? state : null,
    terminal,
    complete: state === 'exited',
  };
}

function intakeTaskStatusView(task: any): ConversationLinkTargetView {
  const status = String(task.status ?? 'open');
  const terminal = status === 'done' || status === 'cancelled';
  const complete = status === 'done';
  return {
    available: true,
    canonical_status: status,
    terminal,
    complete,
  };
}

/**
 * Join-table service for M4 conversation ↔ canonical job/launch references.
 * Owns `conversation_links`. Inspects `routing_jobs` / `fleet_launches` read-only.
 */
export class ConversationLinksService {
  public readonly sql: SqlStorage;
  private readonly txSync: <T>(fn: () => T) => T;
  private readonly conversations: ConversationsService;
  private readonly now: () => number;
  public limits: ConversationLinkLimits;
  public readonly followThrough: ConversationFollowThroughService;

  constructor(
    sql: SqlStorage,
    conversations: ConversationsService,
    txSync?: <T>(fn: () => T) => T,
    options?: {
      now?: () => number;
      limits?: Partial<ConversationLinkLimits>;
    },
  ) {
    this.sql = sql;
    this.conversations = conversations;
    this.txSync = txSync ?? (<T>(fn: () => T): T => fn());
    this.now = options?.now ?? (() => conversations.getNow());
    this.limits = {
      maxLinksPerConversation:
        options?.limits?.maxLinksPerConversation ??
        DEFAULT_MAX_LINKS_PER_CONVERSATION,
      maxTotalLinks: options?.limits?.maxTotalLinks ?? DEFAULT_MAX_TOTAL_LINKS,
    };
    this.followThrough = new ConversationFollowThroughService(
      this.sql,
      this.now,
      (sql: any) => new IntakeService(sql),
    );
  }

  public init(): void {
    this.txSync(() => {
      for (const stmt of CONVERSATION_LINKS_SCHEMA_SQL.split(';')) {
        const trimmed = stmt.trim();
        if (trimmed) this.sql.exec(trimmed);
      }
    });
  }

  public attach(
    auth: ConversationAuthInput,
    conversationId: string,
    input: { kind?: unknown; ref?: unknown; idempotencyKey?: unknown },
  ): Record<string, unknown> {
    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireWriteScope: true,
        isMutatingHttp: true,
      });
      this.conversations.assertActiveMember(conversationId, principal);
      if (!isLinkKind(input.kind)) {
        throw new ConversationDomainError('invalid_kind', 400);
      }
      const kind = input.kind;
      const ref = requireRef(kind, input.ref);
      let idempotencyKey: string | null = null;
      if (input.idempotencyKey !== undefined && input.idempotencyKey !== null) {
        if (
          typeof input.idempotencyKey !== 'string' ||
          !SAFE_KEY_RE.test(input.idempotencyKey)
        ) {
          throw new ConversationDomainError('invalid_idempotency_key', 400);
        }
        idempotencyKey = input.idempotencyKey;
      }

      const visible = this.inspectTarget(kind, ref, principal);
      if (!visible.row) {
        throw new ConversationDomainError('not_found', 404);
      }

      if (idempotencyKey) {
        const priorKey = this.sql
          .exec(
            `SELECT * FROM conversation_links
             WHERE conversation_id = ? AND created_by = ? AND idempotency_key = ?`,
            conversationId,
            principal.actor,
            idempotencyKey,
          )
          .toArray()[0] as any;
        if (priorKey) {
          if (priorKey.kind !== kind || priorKey.ref !== ref) {
            throw new ConversationDomainError('idempotency_conflict', 409);
          }
          return this.formatLink(priorKey, principal, true);
        }
      }

      const priorRef = this.sql
        .exec(
          `SELECT * FROM conversation_links
           WHERE conversation_id = ? AND kind = ? AND ref = ?`,
          conversationId,
          kind,
          ref,
        )
        .toArray()[0] as any;
      if (priorRef) {
        return this.formatLink(priorRef, principal, true);
      }

      const perConv = Number(
        this.sql
          .exec(
            'SELECT COUNT(*) AS n FROM conversation_links WHERE conversation_id = ?',
            conversationId,
          )
          .toArray()[0]?.n ?? 0,
      );
      if (perConv >= this.limits.maxLinksPerConversation) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'conversation_link_limit_exceeded',
        );
      }
      const total = Number(
        this.sql
          .exec('SELECT COUNT(*) AS n FROM conversation_links')
          .toArray()[0]?.n ?? 0,
      );
      if (total >= this.limits.maxTotalLinks) {
        throw new ConversationDomainError(
          'storage_limit_exceeded',
          409,
          'total_link_limit_exceeded',
        );
      }

      const id = generateUlid(this.now());
      const createdAt = new Date(this.now()).toISOString();
      this.sql.exec(
        `INSERT INTO conversation_links
         (id, conversation_id, kind, ref, created_by, created_at, idempotency_key)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
        id,
        conversationId,
        kind,
        ref,
        principal.actor,
        createdAt,
        idempotencyKey,
      );
      const row = this.sql
        .exec('SELECT * FROM conversation_links WHERE id = ?', id)
        .toArray()[0] as any;
      return this.formatLink(row, principal, false);
    });
  }

  public list(
    auth: ConversationAuthInput,
    conversationId: string,
    options?: { limit?: number; cursor?: string | null },
  ): Record<string, unknown> {
    const limit = parseLimit(options?.limit);
    const cursor = parseCursor(options?.cursor ?? null);
    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireReadScope: true,
      });
      this.conversations.assertActiveMember(conversationId, principal);

      let query = `
        SELECT * FROM conversation_links
        WHERE conversation_id = ?
      `;
      const params: unknown[] = [conversationId];
      if (cursor) {
        query +=
          ' AND (created_at < ? OR (created_at = ? AND id < ?))';
        params.push(cursor.createdAt, cursor.createdAt, cursor.id);
      }
      query += ' ORDER BY created_at DESC, id DESC LIMIT ?';
      params.push(limit + 1);

      const rows = this.sql.exec(query, ...params).toArray() as any[];
      const hasMore = rows.length > limit;
      const page = hasMore ? rows.slice(0, limit) : rows;
      const last = page[page.length - 1];
      return {
        conversation_id: conversationId,
        links: page.map(row => this.formatLink(row, principal, false)),
        next_cursor:
          hasMore && last ? `${last.created_at}|${last.id}` : null,
      };
    });
  }

  public getLink(
    auth: ConversationAuthInput,
    conversationId: string,
    linkId: string,
  ): Record<string, unknown> {
    this.requireLinkId(linkId);
    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireReadScope: true,
      });
      this.conversations.assertActiveMember(conversationId, principal);
      const row = this.sql
        .exec(
          'SELECT * FROM conversation_links WHERE id = ? AND conversation_id = ?',
          linkId,
          conversationId,
        )
        .toArray()[0] as any;
      if (!row) throw new ConversationDomainError('not_found', 404);
      return this.formatLink(row, principal, false);
    });
  }

  public remove(
    auth: ConversationAuthInput,
    conversationId: string,
    linkId: string,
  ): Record<string, unknown> {
    this.requireLinkId(linkId);
    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireWriteScope: true,
        isMutatingHttp: true,
      });
      this.conversations.assertActiveMember(conversationId, principal);
      const row = this.sql
        .exec(
          'SELECT * FROM conversation_links WHERE id = ? AND conversation_id = ?',
          linkId,
          conversationId,
        )
        .toArray()[0] as any;
      if (!row) throw new ConversationDomainError('not_found', 404);
      this.sql.exec(
        'DELETE FROM conversation_links WHERE id = ? AND conversation_id = ?',
        linkId,
        conversationId,
      );
      return {
        conversation_id: conversationId,
        id: row.id,
        removed: true,
      };
    });
  }

  public reverse(
    auth: ConversationAuthInput,
    options: {
      kind?: string | null;
      ref?: string | null;
      limit?: number;
      cursor?: string | null;
    },
  ): Record<string, unknown> {
    if (!isLinkKind(options.kind)) {
      throw new ConversationDomainError('invalid_kind', 400);
    }
    const kind = options.kind;
    const ref = requireRef(kind, options.ref);
    const limit = parseLimit(options.limit);
    const cursor = parseCursor(options.cursor ?? null);

    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireReadScope: true,
      });
      const visible = this.inspectTarget(kind, ref, principal);
      if (!visible.row) {
        throw new ConversationDomainError('not_found', 404);
      }

      let query = `
        SELECT l.*
        FROM conversation_links l
        JOIN conversation_members m
          ON m.conversation_id = l.conversation_id
         AND m.actor = ?
         AND m.kind = ?
         AND m.removed_at IS NULL
        JOIN conversations c ON c.id = l.conversation_id
        WHERE l.kind = ? AND l.ref = ?
          AND (c.archived_at IS NULL OR c.archived_at > ?)
      `;
      const params: unknown[] = [
        principal.actor,
        principal.kind,
        kind,
        ref,
        new Date(this.now() - this.conversations.limits.archivedConversationRetentionMs).toISOString(),
      ];
      if (cursor) {
        query +=
          ' AND (l.created_at < ? OR (l.created_at = ? AND l.id < ?))';
        params.push(cursor.createdAt, cursor.createdAt, cursor.id);
      }
      query += ' ORDER BY l.created_at DESC, l.id DESC LIMIT ?';
      params.push(limit + 1);

      const rows = this.sql.exec(query, ...params).toArray() as any[];
      const hasMore = rows.length > limit;
      const page = hasMore ? rows.slice(0, limit) : rows;
      const last = page[page.length - 1];
      return {
        kind,
        ref,
        target: visible.view,
        links: page.map(row => this.formatLink(row, principal, false)),
        next_cursor:
          hasMore && last ? `${last.created_at}|${last.id}` : null,
      };
    });
  }

  public hasExpiredRows(_nowMs: number = this.now()): boolean {
    return (
      this.sql
        .exec(
          `SELECT 1 FROM conversation_links l
           WHERE NOT EXISTS (
             SELECT 1 FROM conversations c WHERE c.id = l.conversation_id
           )
           LIMIT 1`,
        )
        .toArray().length > 0
    );
  }

  public hasRowsForConversation(conversationId: string): boolean {
    return (
      this.sql
        .exec(
          'SELECT 1 FROM conversation_links WHERE conversation_id = ? LIMIT 1',
          conversationId,
        )
        .toArray().length > 0
    );
  }

  public deleteChunkForConversation(
    conversationId: string,
    limit: number,
  ): number {
    const step = Math.max(0, Math.floor(limit));
    if (step <= 0) return 0;
    const chunk = this.sql
      .exec(
        `SELECT id FROM conversation_links
         WHERE conversation_id = ?
         ORDER BY id ASC
         LIMIT ?`,
        conversationId,
        step,
      )
      .toArray() as any[];
    if (chunk.length === 0) return 0;
    const maxId = String(chunk[chunk.length - 1].id);
    this.sql.exec(
      'DELETE FROM conversation_links WHERE conversation_id = ? AND id <= ?',
      conversationId,
      maxId,
    );
    return chunk.length;
  }

  public retentionCleanup(
    _nowMs: number = this.now(),
    limit: number = 100,
  ): { rowsDeleted: number; hasMore: boolean } {
    const remaining = Math.max(1, Math.min(500, Math.floor(limit)));
    const chunk = this.sql
      .exec(
        `SELECT l.id AS id
         FROM conversation_links l
         WHERE NOT EXISTS (
           SELECT 1 FROM conversations c WHERE c.id = l.conversation_id
         )
         ORDER BY l.id ASC
         LIMIT ?`,
        remaining,
      )
      .toArray() as any[];
    if (chunk.length === 0) {
      return { rowsDeleted: 0, hasMore: false };
    }
    const maxId = String(chunk[chunk.length - 1].id);
    this.sql.exec(
      `DELETE FROM conversation_links
       WHERE id <= ?
         AND NOT EXISTS (
           SELECT 1 FROM conversations c WHERE c.id = conversation_links.conversation_id
         )`,
      maxId,
    );
    return {
      rowsDeleted: chunk.length,
      hasMore: this.hasExpiredRows(),
    };
  }

  private requireLinkId(linkId: string): void {
    if (
      typeof linkId !== 'string' ||
      !linkId ||
      linkId.length > 128 ||
      /[\x00-\x1f\x7f]/.test(linkId)
    ) {
      throw new ConversationDomainError('not_found', 404);
    }
  }

  private isAdminHuman(principal: VerifiedConversationPrincipal): boolean {
    return (
      principal.kind === 'human' &&
      (principal.authType === 'session' || principal.authType === 'bearer')
    );
  }

  private isRoutingClient(principal: VerifiedConversationPrincipal): boolean {
    return (
      principal.kind === 'agent' &&
      principal.scopes.includes('routing:submit')
    );
  }

  private isFleetEditor(principal: VerifiedConversationPrincipal): boolean {
    return (
      principal.kind === 'agent' &&
      principal.scopes.includes('bus:send') &&
      principal.scopes.includes('fleet:launch')
    );
  }

  private canInspectJob(
    principal: VerifiedConversationPrincipal,
    job: any,
  ): boolean {
    if (!job) return false;
    if (this.isAdminHuman(principal)) return true;
    const client = this.isRoutingClient(principal) || this.isFleetEditor(principal);
    return client && job.actor === principal.actor;
  }

  private canInspectLaunch(
    principal: VerifiedConversationPrincipal,
    launch: any,
  ): boolean {
    if (!launch) return false;
    return this.isAdminHuman(principal);
  }

  private readJob(id: string): any | null {
    try {
      return (
        (this.sql
          .exec('SELECT * FROM routing_jobs WHERE id = ?', id)
          .toArray()[0] as any) ?? null
      );
    } catch {
      return null;
    }
  }

  private readLaunch(id: string): any | null {
    try {
      return (
        (this.sql
          .exec('SELECT * FROM fleet_launches WHERE id = ?', id)
          .toArray()[0] as any) ?? null
      );
    } catch {
      return null;
    }
  }

  private deriveJobStatus(job: any): string {
    let status = String(job.state ?? '');
    const launch = job.launch_id ? this.readLaunch(job.launch_id) : null;
    if (
      JOB_ACTIVE.has(status) &&
      launch && LAUNCH_KNOWN.has(String(launch.state))
    ) {
      status =
        launch.state === 'exited'
          ? 'done'
          : launch.state === 'failed'
            ? 'failed'
            : launch.state === 'uncertain'
              ? 'stuck'
              : launch.state === 'running'
                ? 'running'
                : 'queued';
      if (!LAUNCH_TERMINAL.has(String(launch.state))) {
        const launchUpdated = Date.parse(String(launch.updated_at));
        const jobUpdated = Date.parse(String(job.updated_at));
        const newest = Math.max(
          Number.isFinite(launchUpdated) ? launchUpdated : 0,
          Number.isFinite(jobUpdated) ? jobUpdated : 0,
        );
        if (this.now() - newest > JOB_STUCK_AFTER_MS) status = 'stuck';
      }
    }
    return status;
  }

  private readIntakeTask(id: string): any | null {
    try {
      const row: any = this.sql
        .exec('SELECT data FROM intake_tasks WHERE id = ?', id)
        .toArray()[0];
      return row?.data ? JSON.parse(row.data) : null;
    } catch {
      return null;
    }
  }

  private canInspectIntakeTask(
    principal: VerifiedConversationPrincipal,
    task: any,
  ): boolean {
    if (!task) return false;
    const intake = new IntakeService(this.sql as any);
    const isAdmin = Boolean(
      principal.isAdmin ||
      (principal.kind === 'human' && (principal.scopes.includes('admin') || principal.authType === 'session')),
    );
    const scopes = isAdmin && !principal.scopes.includes('admin') ? [...principal.scopes, 'admin'] : principal.scopes;
    const caller: CallerContext = {
      actor: principal.actor,
      kind: principal.kind,
      isAdmin,
      scopes,
      tokenId: principal.tokenId,
      tokenHash: principal.tokenHash,
    };
    try {
      const p = intake.resolvePrincipal({ caller, scopes });
      return intake.isTaskAllowed(p, task, false);
    } catch {
      return false;
    }
  }

  private inspectTarget(
    kind: ConversationLinkKind,
    ref: string,
    principal: VerifiedConversationPrincipal,
  ): { row: any | null; view: ConversationLinkTargetView } {
    if (kind === 'fleet_launch') {
      const launch = this.readLaunch(ref);
      if (!launch || !this.canInspectLaunch(principal, launch)) {
        return { row: null, view: targetUnavailable() };
      }
      return { row: launch, view: launchStatusView(String(launch.state)) };
    }
    if (kind === 'intake_task') {
      const task = this.readIntakeTask(ref);
      if (!task || !this.canInspectIntakeTask(principal, task)) {
        return { row: null, view: targetUnavailable() };
      }
      return { row: task, view: intakeTaskStatusView(task) };
    }
    const job = this.readJob(ref);
    if (!job || !this.canInspectJob(principal, job)) {
      return { row: null, view: targetUnavailable() };
    }
    return { row: job, view: jobStatusView(this.deriveJobStatus(job), kind) };
  }

  private formatLink(
    row: any,
    principal: VerifiedConversationPrincipal,
    replay: boolean,
  ): Record<string, unknown> {
    const kind = row.kind as ConversationLinkKind;
    const inspected = this.inspectTarget(kind, String(row.ref), principal);
    const target = inspected.row ? inspected.view : targetUnavailable();
    const followThrough = this.followThrough.deriveLinkFacts(
      row,
      inspected.row,
      principal,
    );
    return {
      id: row.id,
      conversation_id: row.conversation_id,
      kind: row.kind,
      ref: row.ref,
      created_at: row.created_at,
      created_by: row.created_by,
      replay,
      target,
      follow_through: followThrough,
    };
  }

  public getFollowThrough(
    auth: ConversationAuthInput,
    conversationId: string,
    linkId?: string,
  ): Record<string, unknown> {
    return this.txSync(() => {
      const principal = this.conversations.verifyPrincipal(auth, {
        requireReadScope: true,
      });
      this.conversations.assertActiveMember(conversationId, principal);
      if (linkId) {
        this.requireLinkId(linkId);
        const row = this.sql
          .exec(
            'SELECT * FROM conversation_links WHERE id = ? AND conversation_id = ?',
            linkId,
            conversationId,
          )
          .toArray()[0] as any;
        if (!row) throw new ConversationDomainError('not_found', 404);
        const inspected = this.inspectTarget(row.kind, String(row.ref), principal);
        if (!inspected.row) {
          throw new ConversationDomainError('not_found', 404);
        }
        return this.followThrough.deriveLinkFacts(row, inspected.row, principal) as any;
      }

      const rows = this.sql
        .exec(
          'SELECT * FROM conversation_links WHERE conversation_id = ? ORDER BY created_at DESC',
          conversationId,
        )
        .toArray() as any[];

      const items = rows
        .map(row => {
          const inspected = this.inspectTarget(row.kind, String(row.ref), principal);
          return inspected.row
            ? this.followThrough.deriveLinkFacts(row, inspected.row, principal)
            : null;
        })
        .filter(Boolean);

      return {
        conversation_id: conversationId,
        links: items,
        follow_through: items,
      };
    });
  }
}
