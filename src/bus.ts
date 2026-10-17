import { registryAddress, registryRowSql } from './agent_registry';
export interface SqlStorageCursor {
  toArray(): any[];
  [Symbol.iterator](): IterableIterator<any>;
}

export interface SqlStorage {
  exec(query: string, ...cursorValues: any[]): SqlStorageCursor;
}

export interface CallerContext {
  actor?: string;
  machineId?: string;
  isAdmin?: boolean;
  kind?: 'host' | 'agent' | 'job' | 'human' | 'session' | 'bearer' | 'routing-client';
  tokenId?: string;
  tokenHash?: string;
  jobId?: string;
  isRevoked?: boolean;
  scopes?: string[];
  project?: string;
}

export function parseUtcExpiry(val: unknown): number | null {
  if (typeof val === 'number') {
    return Number.isFinite(val) ? Math.floor(val) : null;
  }
  if (typeof val === 'string') {
    const trimmed = val.trim();
    if (!trimmed) return null;
    if (/^\d+(\.\d+)?$/.test(trimmed)) {
      const n = Number(trimmed);
      return Number.isFinite(n) ? Math.floor(n) : null;
    }
    const ms = Date.parse(trimmed);
    return Number.isFinite(ms) ? ms : null;
  }
  return null;
}

export interface SendOpts {
  senderActor: string;
  toActor: string;
  kind: string;
  body: string;
  idempotencyKey: string;
  replyTo?: string;
  threadId?: string;
  leaseEpoch?: number;
  now?: number;
}

export const BUS_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  from_actor TEXT NOT NULL,
  to_actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  reply_to TEXT,
  body TEXT NOT NULL,
  idempotency_key TEXT,
  lease_epoch INTEGER,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  delivered_at INTEGER,
  read_at INTEGER,
  acked_at INTEGER,
  accepted_at INTEGER,
  completed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_from_idempotency ON messages(from_actor, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_messages_to_actor ON messages(to_actor, id);
CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  scope TEXT NOT NULL,
  holder TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  released_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project);

CREATE TABLE IF NOT EXISTS agents (
  tag TEXT NOT NULL,
  project TEXT NOT NULL,
  role TEXT,
  machine_id TEXT,
  registered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agents_tag ON agents(tag);
CREATE INDEX IF NOT EXISTS idx_agents_project_tag ON agents(project, tag);
`;

export type BusAddress =
  | { kind: 'tag'; tag: string }
  | { kind: 'qualified'; project: string; tag: string }
  | { kind: 'role'; role: string }
  | { kind: 'machine'; machineId: string };

// One address segment: non-empty, no ':' separator, no whitespace or control characters.
const ADDRESS_SEGMENT = /^[^\s:\x00-\x1f\x7f]+$/;

/**
 * Parse a bus actor address (SPEC §6): `tag`, `project:tag`, `role:<role>` or `machine:<id>`.
 * Anything else (empty segments, more than one ':', whitespace) is null, so a malformed address is
 * rejected instead of being stored under a to_actor that no inbox can match.
 */
export function parseAddress(address: unknown): BusAddress | null {
  if (typeof address !== 'string' || address.length === 0 || address.length > 128) return null;
  const parts = address.split(':');
  if (!parts.every(p => ADDRESS_SEGMENT.test(p))) return null;
  if (parts.length === 1) return { kind: 'tag', tag: parts[0] };
  if (parts.length !== 2) return null;
  if (parts[0] === 'role') return { kind: 'role', role: parts[1] };
  if (parts[0] === 'machine') return { kind: 'machine', machineId: parts[1] };
  return { kind: 'qualified', project: parts[0], tag: parts[1] };
}

/** One bounded row per cleanup kind; next_index is 0..modulus-1. Created on demand so schema.ts stays untouched. */
export const RETENTION_CLEANUP_CURSOR_SQL = `CREATE TABLE IF NOT EXISTS retention_cleanup_cursor (
  kind TEXT PRIMARY KEY,
  next_index INTEGER NOT NULL
);`;

export type RetentionFairCategory = {
  hasWork: () => boolean;
  spend: (budget: number) => number;
};

function readRetentionCursor(sql: SqlStorage, kind: string, modulus: number): number {
  sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
  const row = sql
    .exec('SELECT next_index FROM retention_cleanup_cursor WHERE kind = ?', kind)
    .toArray()[0] as { next_index?: unknown } | undefined;
  const n = Math.floor(Number(row?.next_index));
  if (!Number.isFinite(n) || modulus <= 0) return 0;
  return ((n % modulus) + modulus) % modulus;
}

function writeRetentionCursor(
  sql: SqlStorage,
  kind: string,
  nextIndex: number,
  modulus: number,
): void {
  sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
  const idx =
    modulus <= 0 ? 0 : ((Math.floor(Number(nextIndex)) % modulus) + modulus) % modulus;
  sql.exec(
    'INSERT OR REPLACE INTO retention_cleanup_cursor (kind, next_index) VALUES (?, ?)',
    kind,
    idx,
  );
}

/**
 * Spend one affected-row budget across categories in durable round-robin order.
 * Budget < eligible count gives 1 row to each of the first `budget` eligible categories.
 * Budget >= eligible count equal-slices then leftover in the same rotated order.
 * Cursor advances past the last category that actually deleted rows, with the caller's transaction.
 */
export function runFairRetentionPass(
  sql: SqlStorage,
  kind: string,
  categories: RetentionFairCategory[],
  totalBudget: number,
): void {
  const modulus = categories.length;
  if (modulus === 0 || totalBudget <= 0) return;
  const start = readRetentionCursor(sql, kind, modulus);
  const rotated: Array<{ cat: RetentionFairCategory; index: number }> = [];
  for (let i = 0; i < modulus; i++) {
    const index = (start + i) % modulus;
    rotated.push({ cat: categories[index], index });
  }
  const eligible = rotated.filter(entry => entry.cat.hasWork());
  if (eligible.length === 0) return;

  let lastDeletedIndex: number | null = null;
  const invoke = (
    entry: { cat: RetentionFairCategory; index: number },
    budget: number,
  ): void => {
    if (budget <= 0) return;
    if (!entry.cat.hasWork()) return;
    const deleted = Number(entry.cat.spend(budget) ?? 0);
    if (deleted > 0) lastDeletedIndex = entry.index;
  };

  if (totalBudget >= eligible.length) {
    const slice = Math.max(1, Math.floor(totalBudget / eligible.length));
    for (const entry of eligible) invoke(entry, slice);
    for (const entry of eligible) invoke(entry, totalBudget);
  } else {
    for (let i = 0; i < totalBudget; i++) invoke(eligible[i], 1);
  }

  if (lastDeletedIndex !== null) {
    writeRetentionCursor(sql, kind, lastDeletedIndex + 1, modulus);
  }
}

const ROOT_AUTHORITY_ERROR = "missing_m4_root_authority: root role routing has no accepted root authority in M3";

/** A root role name in any letter case (`root`, `ROOT`, `rootish`): M3 has no accepted Root authority. */
function isRootRole(role: unknown): boolean {
  return typeof role === 'string' && /^root/i.test(role);
}

/** `role:root…` in any letter case, including the `ROLE:Root` spelling: refused, never routed. */
export function isRootRoleAddress(address: unknown): boolean {
  return typeof address === 'string' && /^role:root/i.test(address);
}

interface AgentIdentity {
  tag: string;
  project: string | null;
  role: string | null;
  machine_id: string | null;
}

const CROCKFORD_BASE32 = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

export function generateUlid(now: number = Date.now()): string {
  let time = Math.floor(now);
  let timeChars = '';
  for (let i = 0; i < 10; i++) {
    timeChars = CROCKFORD_BASE32.charAt(time % 32) + timeChars;
    time = Math.floor(time / 32);
  }

  const randBytes = new Uint8Array(10);
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    crypto.getRandomValues(randBytes);
  } else {
    for (let i = 0; i < 10; i++) {
      randBytes[i] = Math.floor(Math.random() * 256);
    }
  }

  let randChars = '';
  for (let i = 0; i < 16; i++) {
    const bitPos = i * 5;
    const bytePos = Math.floor(bitPos / 8);
    const bitOffset = bitPos % 8;
    let val = (randBytes[bytePos] << 8) | (bytePos + 1 < randBytes.length ? randBytes[bytePos + 1] : 0);
    val = (val >> (11 - bitOffset)) & 31;
    randChars += CROCKFORD_BASE32.charAt(val);
  }

  return timeChars + randChars;
}

export class BusService {
  public sql: SqlStorage;
  public txSync?: <T>(fn: () => T) => T;

  constructor(sql: SqlStorage, txSync?: <T>(fn: () => T) => T) {
    this.sql = sql;
    this.txSync = txSync;
  }

  /**
   * Resolve an agent address to exactly one registered agent, or null when unknown or ambiguous.
   * `role:<role>` resolves only when exactly one non-root agent holds the role: a shared role
   * inbox has no defined fan-out or ACK semantics, so it is refused rather than guessed.
   */
  private resolveAgent(addr: BusAddress | null): AgentIdentity | null {
    if (!addr) return null;
    let rows: any[];
    if (addr.kind === 'tag') {
      rows = this.sql.exec('SELECT tag, project, role, machine_id FROM agents WHERE tag = ?', addr.tag).toArray();
    } else if (addr.kind === 'qualified') {
      rows = this.sql.exec('SELECT tag, project, role, machine_id FROM agents WHERE project = ? AND tag = ?', addr.project, addr.tag).toArray();
    } else if (addr.kind === 'role') {
      if (isRootRole(addr.role)) return null;
      rows = this.sql.exec('SELECT tag, project, role, machine_id FROM agents WHERE role = ?', addr.role).toArray();
    } else {
      return null;
    }
    if (rows.length !== 1 || !registryAddress(rows[0].project ? `${rows[0].project}:${rows[0].tag}` : rows[0].tag)) return null;
    const r = rows[0];
    return { tag: r.tag, project: r.project ?? null, role: r.role ?? null, machine_id: r.machine_id ?? null };
  }

  /** The registered agent behind a `tag` or `project:tag` address; role, machine and malformed forms are null. */
  private resolveAgentActor(actor: unknown): AgentIdentity | null {
    if (typeof actor !== 'string' || !registryAddress(actor)) return null;
    const addr = parseAddress(actor);
    if (!addr || (addr.kind !== 'tag' && addr.kind !== 'qualified')) return null;
    return this.resolveAgent(addr);
  }

  /**
   * Two addresses name the same participant: the same well-formed string, or aliases (tag,
   * project:tag, unique role) of one registered agent compared by canonical project+tag.
   * Root role addresses never match anything.
   */
  private sameParticipant(a: string, b: string): boolean {
    if (isRootRoleAddress(a) || isRootRoleAddress(b)) return false;
    if (a === b) {
      const addr = parseAddress(a);
      if (addr?.kind === 'tag') return this.sql.exec('SELECT 1 FROM agents WHERE tag = ?', addr.tag).toArray().length <= 1;
      return addr !== null;
    }
    const x = this.resolveAgent(parseAddress(a));
    const y = this.resolveAgent(parseAddress(b));
    return Boolean(x && y && x.tag === y.tag && x.project === y.project);
  }

  /** Stored bare endpoints belong to the projectless identity; never promote history
   * into a project created later. Unique request aliases still resolve normally. */
  private sameStoredParticipant(stored: string, actor: string, unbound = false): boolean {
    if (unbound) return false;
    const address = parseAddress(stored);
    if (address?.kind === 'tag' && this.resolveAgentActor(stored)?.project) return false;
    return this.sameParticipant(stored, actor);
  }

  private isAuthorizedForStoredActor(caller: CallerContext, actor: string, now: number, unbound = false): boolean {
    if (unbound && !caller.isAdmin) return false;
    if (caller.isAdmin) return this.isAuthorizedForActor(caller, actor, now);
    const address = parseAddress(actor);
    if (address?.kind === 'tag' && this.resolveAgentActor(actor)?.project) return false;
    return this.isAuthorizedForActor(caller, actor, now);
  }

  /** Every to_actor string that addresses this agent: tag, project:tag and its role when it is the unique holder. */
  private agentAliases(agent: AgentIdentity): string[] {
    const aliases = [agent.project ? `${agent.project}:${agent.tag}` : agent.tag];
    if (agent.role && !isRootRole(agent.role)) {
      const holder = this.resolveAgent({ kind: 'role', role: agent.role });
      if (holder && holder.tag === agent.tag && holder.project === agent.project) aliases.push(`role:${agent.role}`);
    }
    return aliases;
  }

  /** Inbox aliases for an address; a well-formed address that resolves to no agent matches only itself. */
  private recipientAliases(actor: string): string[] {
    const addr = parseAddress(actor);
    if (!addr) return [];
    const agent = this.resolveAgent(addr);
    if (!agent) return [actor];
    return Array.from(new Set(this.agentAliases(agent)));
  }

  public canonicalActor(actor: string): string {
    const agent = this.resolveAgentActor(actor);
    return agent?.project ? `${agent.project}:${agent.tag}` : actor;
  }

  public validateRecipient(actor: string, now: number = Date.now()): boolean {
    // M3 has no accepted root authority: fail closed rather than infer/mint Root
    if (isRootRoleAddress(actor)) return false;

    const addr = parseAddress(actor);
    if (!addr) return false;

    if (addr.kind === 'machine') {
      const res = this.sql.exec('SELECT 1 FROM machines WHERE id = ?', addr.machineId).toArray();
      return res.length > 0;
    }

    return this.resolveAgent(addr) !== null;
  }

  public isAuthorizedForActor(caller: CallerContext, actor: string, now: number = Date.now()): boolean {
    if (caller.isRevoked) return false;

    // Root role routing has no accepted root authority in M3: fail closed in any letter case
    if (isRootRoleAddress(actor) || isRootRoleAddress(caller.actor)) {
      return false;
    }

    const addr = parseAddress(actor);
    if (!addr) return false;

    if (caller.isAdmin) return true;

    // Aliases of one registered agent (tag, project:tag, unique role holder) are one identity;
    // a malformed caller actor matches nothing
    if (caller.actor) return this.sameParticipant(caller.actor, actor);

    if (caller.kind === 'host' || (caller.kind === undefined && !caller.actor && caller.machineId)) {
      if (!caller.machineId) return false;
      if (addr.kind === 'machine') return addr.machineId === caller.machineId;
      const agent = this.resolveAgent(addr);
      return Boolean(agent && agent.machine_id === caller.machineId);
    }

    return false;
  }

  /** The recipient itself (or the host it runs on), as opposed to an admin observer. */
  private isRecipientPrincipal(caller: CallerContext, toActor: string, now: number): boolean {
    return this.isAuthorizedForActor({ ...caller, isAdmin: false }, toActor, now);
  }

  public validateClaimProject(caller: CallerContext, project: string, holder: string, now: number = Date.now(), leaseEpoch?: number): void {
    if (isRootRoleAddress(holder) || isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    // A holder is one agent address, `tag` or `project:tag`, under the message address grammar
    const holderAddr = parseAddress(holder);
    if (!holderAddr || (holderAddr.kind !== 'tag' && holderAddr.kind !== 'qualified')) {
      throw new Error("unauthorized");
    }

    if (caller.isAdmin) return;

    // The holder must name exactly one registered agent: a bare tag shared across projects is ambiguous
    const holderAgent = this.resolveAgent(holderAddr);
    if (!holderAgent || !holderAgent.project || project !== holderAgent.project) {
      throw new Error("unauthorized");
    }

    if (caller.actor) {
      const agent = this.resolveAgentActor(caller.actor);
      if (!agent || agent.tag !== holderAgent.tag || agent.project !== holderAgent.project) {
        throw new Error("unauthorized");
      }
      return;
    }

    if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
      if (!caller.machineId || holderAgent.machine_id !== caller.machineId) {
        throw new Error("unauthorized");
      }
      return;
    }

    throw new Error("unauthorized");
  }

  private runInTxn<T>(fn: () => T): T {
    if (this.txSync) return this.txSync(fn);
    return fn();
  }

  send(caller: CallerContext, opts: SendOpts): { id: string; threadId: string; thread_id: string } {
    return this.runInTxn(() => {
      const now = Date.now(); // internal server clock

      if (new TextEncoder().encode(opts.body).length > 16384) {
        throw new Error("body_too_large");
      }

      if (isRootRoleAddress(opts.senderActor)) {
        throw new Error(ROOT_AUTHORITY_ERROR);
      }

      if (!parseAddress(opts.senderActor)) {
        throw new Error("unauthorized");
      }

      if (!this.isAuthorizedForActor(caller, opts.senderActor, now) && !caller.isAdmin) {
        throw new Error("unauthorized");
      }

      opts = { ...opts, senderActor: this.canonicalActor(opts.senderActor), toActor: this.canonicalActor(opts.toActor) };
      let existing = this.sql.exec(
        'SELECT id, body, to_actor, kind, thread_id, reply_to, lease_epoch, expires_at FROM messages WHERE from_actor = ? AND idempotency_key = ?',
        opts.senderActor,
        opts.idempotencyKey
      ).toArray()[0] as any;

      // The idempotency window is the message retention window: once a message has expired it is
      // gone for every reader, so its key is free again whether or not retention has purged the row.
      if (existing && existing.expires_at <= now) {
        this.sql.exec('DELETE FROM messages WHERE id = ?', existing.id);
        existing = undefined;
      }

      if (existing) {
        const expectedReplyTo = opts.replyTo || null;
        const expectedLeaseEpoch = opts.leaseEpoch ?? null;
        if (
          existing.body !== opts.body ||
          existing.to_actor !== opts.toActor ||
          existing.kind !== opts.kind ||
          existing.reply_to !== expectedReplyTo ||
          existing.lease_epoch !== expectedLeaseEpoch ||
          (opts.threadId !== undefined && existing.thread_id !== opts.threadId)
        ) {
          throw new Error("conflict");
        }
        return { id: existing.id, threadId: existing.thread_id, thread_id: existing.thread_id };
      }

      if (isRootRoleAddress(opts.toActor)) {
        throw new Error("unknown_recipient");
      }

      if (!this.validateRecipient(opts.toActor, now)) {
        throw new Error("unknown_recipient");
      }

      let threadId = opts.threadId;
      if (opts.replyTo) {
        const msg = this.sql.exec('SELECT thread_id, from_actor, to_actor, registry_unbound_from, registry_unbound_to FROM messages WHERE id = ? AND expires_at > ?', opts.replyTo, now).toArray()[0] as any;
        if (!msg) throw new Error("invalid_reply_to");
        // Both reply ends must be parties of the original message in any alias form of the same agent
        const parties = [[msg.from_actor, msg.registry_unbound_from], [msg.to_actor, msg.registry_unbound_to]];
        if (!parties.some(([p, unbound]) => this.sameStoredParticipant(p, opts.toActor, !!unbound))) throw new Error("invalid_reply_to");
        if (!parties.some(([p, unbound]) => this.sameStoredParticipant(p, opts.senderActor, !!unbound))) throw new Error("invalid_reply_to");
        const parentThreadId = msg.thread_id || opts.replyTo;
        if (threadId !== undefined && threadId !== parentThreadId) throw new Error("invalid_reply_to");
        threadId = parentThreadId;
      }

      const id = generateUlid(now);
      threadId = threadId || id;
      const expiresAt = now + 30 * 24 * 3600 * 1000;

      this.sql.exec(`
        INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, reply_to, body, idempotency_key, lease_epoch, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `, id, opts.senderActor, opts.toActor, opts.kind, threadId, opts.replyTo || null, opts.body, opts.idempotencyKey, opts.leaseEpoch ?? null, now, expiresAt);

      return { id, threadId, thread_id: threadId };
    });
  }

  /**
   * Recipient inbox. Matches every alias of the addressed agent (tag, project:tag, unique role),
   * hides expired messages, and marks returned messages delivered when the caller is the recipient
   * (or its host bridge). An admin reading someone else's inbox does not mark anything.
   */
  inbox(caller: CallerContext, actor: string, limit: number = 50, cursor?: string): any[] {
    const now = Date.now();
    if (isRootRoleAddress(actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    if (!this.isAuthorizedForActor(caller, actor, now)) throw new Error("unauthorized");

    if (limit > 100) limit = 100;
    if (limit < 1) limit = 1;

    const aliases = this.recipientAliases(actor);
    if (aliases.length === 0) throw new Error("unauthorized");

    return this.runInTxn(() => {
      let query = `SELECT * FROM messages WHERE to_actor IN (${aliases.map(() => '?').join(',')}) AND expires_at > ?${caller.isAdmin ? '' : ' AND registry_unbound_to = 0'}`;
      const params: any[] = [...aliases, now];
      if (cursor) {
        query += ' AND id > ?';
        params.push(cursor);
      }
      query += ' ORDER BY id ASC LIMIT ?';
      params.push(limit);

      const rows = this.sql.exec(query, ...params).toArray() as any[];
      if (this.isRecipientPrincipal(caller, actor, now)) {
        for (const row of rows) {
          if (row.delivered_at) continue;
          this.sql.exec('UPDATE messages SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL', now, row.id);
          row.delivered_at = now;
        }
      }
      return rows;
    });
  }

  /**
   * Show one message to its sender or recipient. When the recipient (or its host bridge) shows it,
   * the message is marked delivered and read; sender and admin views leave the stages untouched.
   */
  show(caller: CallerContext, messageId: string): any {
    const now = Date.now();
    return this.runInTxn(() => {
      const msg = this.sql.exec('SELECT * FROM messages WHERE id = ? AND expires_at > ?', messageId, now).toArray()[0] as any;
      if (!msg) throw new Error("not_found");
      if (!caller.isAdmin && !this.isAuthorizedForStoredActor(caller, msg.to_actor, now, !!msg.registry_unbound_to) && !this.isAuthorizedForStoredActor(caller, msg.from_actor, now, !!msg.registry_unbound_from)) {
        throw new Error("unauthorized");
      }
      if (this.isAuthorizedForStoredActor({ ...caller, isAdmin: false }, msg.to_actor, now, !!msg.registry_unbound_to)) {
        if (!msg.delivered_at) {
          this.sql.exec('UPDATE messages SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL', now, messageId);
          msg.delivered_at = now;
        }
        if (!msg.read_at) {
          this.sql.exec('UPDATE messages SET read_at = ? WHERE id = ? AND read_at IS NULL', now, messageId);
          msg.read_at = now;
        }
      }
      return msg;
    });
  }

  private updateMessageStage(caller: CallerContext, messageId: string, stage: 'delivered_at' | 'read_at' | 'acked_at' | 'accepted_at' | 'completed_at', now: number = Date.now(), leaseEpoch?: number) {
    const msg = this.sql.exec('SELECT to_actor, registry_unbound_to, delivered_at, read_at, acked_at, accepted_at, completed_at, expires_at FROM messages WHERE id = ?', messageId).toArray()[0] as any;
    if (!msg || msg.expires_at <= Date.now()) throw new Error("not_found");

    if (isRootRoleAddress(msg.to_actor) || isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    // SPEC M3: ACK only by the recipient. Every stage is advanced by the recipient (or the host it
    // runs on); an admin observer has no override, so a stage time is always the recipient's own step.
    if (!this.isAuthorizedForStoredActor({ ...caller, isAdmin: false }, msg.to_actor, now, !!msg.registry_unbound_to)) {
      throw new Error("unauthorized");
    }

    if (stage === 'read_at' && !msg.delivered_at) throw new Error("invalid_stage");
    if (stage === 'acked_at' && !msg.read_at && !msg.delivered_at) throw new Error("invalid_stage");
    if (stage === 'accepted_at' && !msg.acked_at) throw new Error("invalid_stage");
    if (stage === 'completed_at' && !msg.accepted_at) throw new Error("invalid_stage");

    // A stage time is written once; repeating a stage never moves it
    if (msg[stage]) return;

    this.sql.exec(`UPDATE messages SET ${stage} = ? WHERE id = ?`, now, messageId);
  }

  deliver(caller: CallerContext, messageId: string, now: number = Date.now(), leaseEpoch?: number) { this.updateMessageStage(caller, messageId, 'delivered_at', now, leaseEpoch); }
  read(caller: CallerContext, messageId: string, now: number = Date.now(), leaseEpoch?: number) { this.updateMessageStage(caller, messageId, 'read_at', now, leaseEpoch); }
  ack(caller: CallerContext, messageId: string, now: number = Date.now(), leaseEpoch?: number) { this.updateMessageStage(caller, messageId, 'acked_at', now, leaseEpoch); }
  accept(caller: CallerContext, messageId: string, now: number = Date.now(), leaseEpoch?: number) { this.updateMessageStage(caller, messageId, 'accepted_at', now, leaseEpoch); }
  complete(caller: CallerContext, messageId: string, now: number = Date.now(), leaseEpoch?: number) { this.updateMessageStage(caller, messageId, 'completed_at', now, leaseEpoch); }

  claim(caller: CallerContext, project: string, scope: string, holder: string, ttlMs: number, now: number = Date.now(), leaseEpoch?: number): { id: string } {
    const effectiveNow = Date.now(); // internal server clock

    if (ttlMs < 1 || ttlMs > 30 * 24 * 3600 * 1000 || !Number.isSafeInteger(ttlMs)) {
      throw new Error("invalid_ttl");
    }

    if (scope.includes('..')) throw new Error("invalid_scope");
    scope = scope.split('/').filter(p => p && p !== '.').join('/');
    if (!scope) throw new Error("invalid_scope");

    this.validateClaimProject(caller, project, holder, effectiveNow, leaseEpoch);

    const expiresAt = effectiveNow + ttlMs;

    const activeClaims = this.sql.exec('SELECT id, holder, scope FROM claims WHERE project = ? AND released_at IS NULL AND expires_at > ?', project, effectiveNow).toArray() as any[];
    for (const active of activeClaims) {
      const aScope = active.scope;
      if (aScope === scope || scope.startsWith(aScope + '/') || aScope.startsWith(scope + '/')) {
        if (active.holder !== holder) {
          throw new Error("conflict");
        }
      }
    }

    const existing = activeClaims.find(c => c.scope === scope && c.holder === holder);
    if (existing) {
      this.sql.exec('UPDATE claims SET expires_at = ? WHERE id = ?', expiresAt, existing.id);
      return { id: existing.id };
    }

    const id = generateUlid(effectiveNow);
    this.sql.exec('INSERT INTO claims (id, project, scope, holder, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)', id, project, scope, holder, effectiveNow, expiresAt);
    return { id };
  }

  releaseClaim(caller: CallerContext, idOrProjectScope: string, now: number = Date.now(), leaseEpoch?: number) {
    const effectiveNow = Date.now();

    let claim: any;
    if (idOrProjectScope.includes(':')) {
      const parts = idOrProjectScope.split(':');
      const project = parts[0];
      let scope = parts.slice(1).join(':');
      if (scope.includes('..')) throw new Error("invalid_scope");
      scope = scope.split('/').filter(p => p && p !== '.').join('/');
      if (!scope) throw new Error("invalid_scope");
      claim = this.sql.exec('SELECT * FROM claims WHERE project = ? AND scope = ? AND released_at IS NULL ORDER BY created_at DESC LIMIT 1', project, scope).toArray()[0];
    } else {
      claim = this.sql.exec('SELECT * FROM claims WHERE id = ?', idOrProjectScope).toArray()[0];
    }

    if (!claim || claim.released_at) return;

    if (isRootRoleAddress(claim.holder) || isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    if (!caller.isAdmin) {
      if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
        if (!caller.machineId) throw new Error("unauthorized");
        const agent = this.resolveAgentActor(claim.holder);
        if (!agent || agent.machine_id !== caller.machineId || agent.project !== claim.project) {
          throw new Error("unauthorized");
        }
      } else {
        if (!this.isAuthorizedForActor(caller, claim.holder, effectiveNow)) {
          throw new Error("unauthorized");
        }
        if (caller.actor) {
          const agent = this.resolveAgentActor(caller.actor);
          if (!agent || !agent.project || agent.project !== claim.project) {
            throw new Error("unauthorized");
          }
        }
      }
    }

    this.sql.exec('UPDATE claims SET released_at = ? WHERE id = ?', effectiveNow, claim.id);
  }

  listClaims(caller?: CallerContext, project?: string, holder?: string, activeOnly: boolean = true): any[] {
    const now = Date.now();
    let query = 'SELECT id, project, scope, holder, created_at, expires_at, released_at FROM claims WHERE 1=1';
    const params: any[] = [];

    let effectiveProject = project;

    if (caller && isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    if (caller && !caller.isAdmin) {
      if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
        if (caller.machineId) {
          const hostProjects = this.sql.exec('SELECT DISTINCT project FROM agents WHERE machine_id = ?', caller.machineId).toArray().map((r: any) => r.project);
          if (project) {
            if (!hostProjects.includes(project)) {
              throw new Error("unauthorized");
            }
          } else if (hostProjects.length > 0) {
            query += ` AND project IN (${hostProjects.map(() => '?').join(',')})`;
            params.push(...hostProjects);
          } else {
            return [];
          }
        }
      } else if (caller.actor) {
        const agentProj = this.resolveAgentActor(caller.actor)?.project || undefined;

        if (project) {
          if (!agentProj || project !== agentProj) {
            throw new Error("unauthorized");
          }
        } else if (agentProj) {
          effectiveProject = agentProj;
        } else {
          return [];
        }
      }
    }

    if (effectiveProject) {
      query += ' AND project = ?';
      params.push(effectiveProject);
    }
    if (holder) {
      query += ' AND holder = ?';
      params.push(holder);
    }
    if (activeOnly) {
      query += ' AND released_at IS NULL AND expires_at > ?';
      params.push(now);
    }
    query += ' ORDER BY created_at DESC LIMIT 100';
    return this.sql.exec(query, ...params).toArray();
  }

  hasExpiredRows(now: number = Date.now()): boolean {
    if (
      this.sql
        .exec('SELECT 1 FROM messages WHERE expires_at <= ? LIMIT 1', now)
        .toArray().length > 0
    ) {
      return true;
    }
    return (
      this.sql
        .exec('SELECT 1 FROM claims WHERE expires_at < ? LIMIT 1', now)
        .toArray().length > 0
    );
  }

  retentionCleanup(now: number = Date.now(), limit: number = 100): { messagesDeleted: number; claimsDeleted: number; hasMore: boolean } {
    const raw = Math.floor(Number(limit));
    const budget = Number.isFinite(raw) ? Math.max(0, Math.min(500, raw)) : 100;
    if (budget <= 0) {
      return {
        messagesDeleted: 0,
        claimsDeleted: 0,
        hasMore: this.hasExpiredRows(now),
      };
    }

    this.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
    return this.runInTxn(() => {
      let remaining = budget;
      let messagesDeleted = 0;
      let claimsDeleted = 0;

      const deleteExpiredMessages = (stepBudget: number): number => {
        const stepLimit = Math.min(remaining, stepBudget);
        if (stepLimit <= 0) return 0;
        const expiredMessages = this.sql
          .exec(
            'SELECT id FROM messages WHERE expires_at <= ? LIMIT ?',
            now,
            stepLimit,
          )
          .toArray() as { id: string }[];
        for (const row of expiredMessages) {
          this.sql.exec('DELETE FROM messages WHERE id = ?', row.id);
          messagesDeleted++;
          remaining--;
        }
        return expiredMessages.length;
      };

      const deleteExpiredClaims = (stepBudget: number): number => {
        const stepLimit = Math.min(remaining, stepBudget);
        if (stepLimit <= 0) return 0;
        const expiredClaims = this.sql
          .exec(
            'SELECT id FROM claims WHERE expires_at < ? LIMIT ?',
            now,
            stepLimit,
          )
          .toArray() as { id: string }[];
        for (const row of expiredClaims) {
          this.sql.exec('DELETE FROM claims WHERE id = ?', row.id);
          claimsDeleted++;
          remaining--;
        }
        return expiredClaims.length;
      };

      runFairRetentionPass(
        this.sql,
        'bus',
        [
          {
            hasWork: () =>
              this.sql
                .exec('SELECT 1 FROM messages WHERE expires_at <= ? LIMIT 1', now)
                .toArray().length > 0,
            spend: deleteExpiredMessages,
          },
          {
            hasWork: () =>
              this.sql
                .exec('SELECT 1 FROM claims WHERE expires_at < ? LIMIT 1', now)
                .toArray().length > 0,
            spend: deleteExpiredClaims,
          },
        ],
        budget,
      );

      return {
        messagesDeleted,
        claimsDeleted,
        hasMore: this.hasExpiredRows(now),
      };
    });
  }

  getBusActors(caller?: CallerContext): string[] {
    if (!caller) {
      const agentActors = this.sql.exec(`SELECT DISTINCT CASE WHEN COALESCE(project, '') = '' THEN tag ELSE project || ':' || tag END as actor FROM agents WHERE COALESCE(role,'') != "root" AND ${registryRowSql()}`).toArray().map((r: any) => r.actor);
      const machineActors = this.sql.exec('SELECT "machine:" || id as actor FROM machines').toArray().map((r: any) => r.actor);
      return Array.from(new Set([...agentActors, ...machineActors])).filter(a => a !== 'role:root');
    }

    if (isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    if (caller.isAdmin) {
      const agentActors = this.sql.exec(`SELECT DISTINCT CASE WHEN COALESCE(project, '') = '' THEN tag ELSE project || ':' || tag END as actor FROM agents WHERE COALESCE(role,'') != "root" AND ${registryRowSql()}`).toArray().map((r: any) => r.actor);
      const machineActors = this.sql.exec('SELECT "machine:" || id as actor FROM machines').toArray().map((r: any) => r.actor);
      return Array.from(new Set([...agentActors, ...machineActors])).filter(a => a !== 'role:root');
    }

    if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
      if (!caller.machineId) throw new Error("unauthorized");
      const agentActors = this.sql.exec(
        `SELECT DISTINCT CASE WHEN COALESCE(project, '') = '' THEN tag ELSE project || ':' || tag END as actor FROM agents WHERE machine_id = ? AND COALESCE(role,'') != "root" AND ${registryRowSql()}`,
        caller.machineId
      ).toArray().map((r: any) => r.actor);
      const machineActor = `machine:${caller.machineId}`;
      return Array.from(new Set([...agentActors, machineActor])).filter(a => a !== 'role:root');
    }

    if (caller.actor) {
      const agent = this.resolveAgentActor(caller.actor);
      if (agent && agent.project && !isRootRole(agent.role)) {
        return [`${agent.project}:${agent.tag}`];
      }
      return parseAddress(caller.actor) ? [caller.actor] : [];
    }

    throw new Error("unauthorized");
  }

  getBusMessages(caller?: CallerContext, actor?: string | null): any[] {
    const now = Date.now();

    if (!caller) {
      if (actor) {
        return this.sql.exec('SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100', actor, actor).toArray();
      }
      return this.sql.exec('SELECT * FROM messages WHERE to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100').toArray();
    }

    if (isRootRoleAddress(actor) || isRootRoleAddress(caller.actor)) {
      throw new Error(ROOT_AUTHORITY_ERROR);
    }

    if (caller.isAdmin) {
      if (actor) {
        return this.sql.exec(
          'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
          actor, actor
        ).toArray();
      }
      return this.sql.exec('SELECT * FROM messages WHERE to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100').toArray();
    }

    if (caller.actor) {
      const effectiveActor = actor || caller.actor;
      if (!this.isAuthorizedForActor(caller, effectiveActor, now)) {
        throw new Error("unauthorized");
      }

      const aliases = [this.canonicalActor(effectiveActor)];

      if (aliases.length === 1) {
        return this.sql.exec(
          'SELECT * FROM messages WHERE ((registry_unbound_to = 0 AND to_actor = ?) OR (registry_unbound_from = 0 AND from_actor = ?)) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
          aliases[0], aliases[0]
        ).toArray();
      } else {
        return this.sql.exec(
          'SELECT * FROM messages WHERE ((registry_unbound_to = 0 AND to_actor IN (?, ?)) OR (registry_unbound_from = 0 AND from_actor IN (?, ?))) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
          aliases[0], aliases[1], aliases[0], aliases[1]
        ).toArray();
      }
    }

    if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
      if (!caller.machineId) throw new Error("unauthorized");

      if (actor) {
        if (!this.isAuthorizedForActor(caller, actor, now)) {
          throw new Error("unauthorized");
        }
        actor = this.canonicalActor(actor);
        return this.sql.exec(
          'SELECT * FROM messages WHERE ((registry_unbound_to = 0 AND to_actor = ?) OR (registry_unbound_from = 0 AND from_actor = ?)) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
          actor, actor
        ).toArray();
      }

      const machineActor = `machine:${caller.machineId}`;
      const agentRows = this.sql.exec(
        'SELECT project, tag FROM agents WHERE machine_id = ? AND role != "root"',
        caller.machineId
      ).toArray() as any[];
      const allowedActors = new Set<string>([machineActor]);
      for (const row of agentRows) {
        if (!registryAddress(row.project ? `${row.project}:${row.tag}` : row.tag)) continue;
        allowedActors.add(row.project ? `${row.project}:${row.tag}` : row.tag);
      }
      const allowedList = Array.from(allowedActors);
      if (allowedList.length === 0) return [];

      const placeholders = allowedList.map(() => '?').join(',');
      const sqlQuery = `SELECT * FROM messages WHERE ((registry_unbound_to = 0 AND to_actor IN (${placeholders})) OR (registry_unbound_from = 0 AND from_actor IN (${placeholders}))) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100`;
      return this.sql.exec(sqlQuery, ...allowedList, ...allowedList).toArray();
    }

    throw new Error("unauthorized");
  }
}
