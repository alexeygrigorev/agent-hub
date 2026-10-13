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
  kind?: 'host' | 'agent' | 'job' | 'human' | 'session' | 'bearer';
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

  public validateRecipient(actor: string, now: number = Date.now()): boolean {
    if (actor === 'role:root' || actor.startsWith('role:root')) {
      // M3 has no accepted root authority: fail closed rather than infer/mint Root
      return false;
    }

    if (actor.startsWith('role:')) {
      const role = actor.substring(5);
      const res = this.sql.exec('SELECT 1 FROM agents WHERE role = ?', role).toArray();
      return res.length > 0;
    }

    if (actor.startsWith('machine:')) {
      const machineId = actor.substring(8);
      const res = this.sql.exec('SELECT 1 FROM machines WHERE id = ?', machineId).toArray();
      return res.length > 0;
    }

    if (actor.includes(':')) {
      const [project, tag] = actor.split(':');
      const res = this.sql.exec('SELECT 1 FROM agents WHERE project = ? AND tag = ?', project, tag).toArray();
      return res.length > 0;
    }

    const agents = this.sql.exec('SELECT project FROM agents WHERE tag = ?', actor).toArray();
    if (agents.length > 0) {
      const projects = new Set(agents.map((a: any) => a.project));
      if (projects.size === 1) return true;
    }

    return false;
  }

  public isAuthorizedForActor(caller: CallerContext, actor: string, now: number = Date.now()): boolean {
    if (caller.isRevoked) return false;

    // Root role routing has no accepted root authority in M3: fail closed
    if (actor === 'role:root' || caller.actor === 'role:root') {
      return false;
    }

    if (caller.isAdmin) return true;

    if (caller.actor) {
      if (caller.actor === actor) return true;

      if (caller.actor.includes(':') && !actor.includes(':')) {
        const [cProj, cTag] = caller.actor.split(':');
        if (cTag === actor) {
          const agents = this.sql.exec('SELECT project FROM agents WHERE tag = ?', actor).toArray();
          if (agents.length === 1 && agents[0].project === cProj) return true;
        }
      } else if (!caller.actor.includes(':') && actor.includes(':')) {
        const [aProj, aTag] = actor.split(':');
        if (caller.actor === aTag) {
          const agents = this.sql.exec('SELECT project FROM agents WHERE tag = ?', aTag).toArray();
          if (agents.length === 1 && agents[0].project === aProj) return true;
        }
      }

      return false;
    }

    if (caller.kind === 'host' || (caller.kind === undefined && !caller.actor && caller.machineId)) {
      if (caller.machineId) {
        if (actor === `machine:${caller.machineId}`) return true;
        if (actor.startsWith('machine:')) return false;

        if (actor.includes(':') && !actor.startsWith('role:') && !actor.startsWith('machine:')) {
          const [project, tag] = actor.split(':');
          const res = this.sql.exec('SELECT 1 FROM agents WHERE project = ? AND tag = ? AND machine_id = ?', project, tag, caller.machineId).toArray();
          if (res.length > 0) return true;
        } else if (!actor.startsWith('role:') && !actor.startsWith('machine:')) {
          const agents = this.sql.exec('SELECT project, machine_id FROM agents WHERE tag = ?', actor).toArray();
          if (agents.length > 0) {
            const projects = new Set(agents.map((a: any) => a.project));
            if (projects.size === 1 && agents.some((a: any) => a.machine_id === caller.machineId)) return true;
          }
        }

        if (actor.startsWith('role:')) {
          const role = actor.substring(5);
          if (role === 'root') return false; // Fail closed for root
          const res = this.sql.exec('SELECT 1 FROM agents WHERE role = ? AND machine_id = ?', role, caller.machineId).toArray();
          if (res.length > 0) return true;
        }
      }
    }

    return false;
  }

  public validateClaimProject(caller: CallerContext, project: string, holder: string, now: number = Date.now(), leaseEpoch?: number): void {
    if (caller.isAdmin) return;

    if (holder === 'role:root' || caller.actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
    }

    if (caller.actor) {
      let regProj: string | undefined;
      let regTag: string | undefined;

      if (caller.actor.includes(':')) {
        const [cProj, cTag] = caller.actor.split(':');
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE project = ? AND tag = ?', cProj, cTag).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
          regTag = rows[0].tag;
        }
      } else {
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE tag = ?', caller.actor).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
          regTag = rows[0].tag;
        }
      }

      if (!regProj || !regTag) {
        throw new Error("unauthorized");
      }

      if (project !== regProj) {
        throw new Error("unauthorized");
      }

      const validHolders = new Set([regTag, `${regProj}:${regTag}`]);
      if (!validHolders.has(holder)) {
        throw new Error("unauthorized");
      }
      return;
    }

    if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
      if (!caller.machineId) throw new Error("unauthorized");

      let regProj: string | undefined;
      if (holder.includes(':')) {
        const [hProj, hTag] = holder.split(':');
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE project = ? AND tag = ? AND machine_id = ?', hProj, hTag, caller.machineId).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
        }
      } else {
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE tag = ? AND machine_id = ?', holder, caller.machineId).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
        }
      }

      if (!regProj || project !== regProj) {
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

      if (opts.senderActor === 'role:root') {
        throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
      }

      if (!this.isAuthorizedForActor(caller, opts.senderActor, now) && !caller.isAdmin) {
        throw new Error("unauthorized");
      }

      const existing = this.sql.exec(
        'SELECT id, body, to_actor, kind, thread_id, reply_to, lease_epoch FROM messages WHERE from_actor = ? AND idempotency_key = ?',
        opts.senderActor,
        opts.idempotencyKey
      ).toArray()[0] as any;

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

      if (opts.toActor === 'role:root') {
        throw new Error("unknown_recipient");
      }

      if (!this.validateRecipient(opts.toActor, now)) {
        throw new Error("unknown_recipient");
      }

      let threadId = opts.threadId;
      if (opts.replyTo) {
        const msg = this.sql.exec('SELECT thread_id, from_actor, to_actor FROM messages WHERE id = ?', opts.replyTo).toArray()[0] as any;
        if (!msg) throw new Error("invalid_reply_to");
        if (msg.from_actor !== opts.toActor && msg.to_actor !== opts.toActor) throw new Error("invalid_reply_to");
        if (msg.from_actor !== opts.senderActor && msg.to_actor !== opts.senderActor) throw new Error("invalid_reply_to");
        threadId = threadId || msg.thread_id || opts.replyTo;
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

  inbox(caller: CallerContext, actor: string, limit: number = 50, cursor?: string): any[] {
    const now = Date.now();
    if (actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
    }

    if (!this.isAuthorizedForActor(caller, actor, now)) throw new Error("unauthorized");

    if (limit > 100) limit = 100;
    if (limit < 1) limit = 1;

    let query = 'SELECT * FROM messages WHERE to_actor = ?';
    const params: any[] = [actor];
    if (cursor) {
      query += ' AND id > ?';
      params.push(cursor);
    }
    query += ' ORDER BY id ASC LIMIT ?';
    params.push(limit);

    return this.sql.exec(query, ...params).toArray();
  }

  show(caller: CallerContext, messageId: string): any {
    const now = Date.now();
    const msg = this.sql.exec('SELECT * FROM messages WHERE id = ?', messageId).toArray()[0] as any;
    if (!msg) throw new Error("not_found");
    if (!caller.isAdmin && !this.isAuthorizedForActor(caller, msg.to_actor, now) && !this.isAuthorizedForActor(caller, msg.from_actor, now)) {
      throw new Error("unauthorized");
    }
    return msg;
  }

  private updateMessageStage(caller: CallerContext, messageId: string, stage: 'delivered_at' | 'read_at' | 'acked_at' | 'accepted_at' | 'completed_at', now: number = Date.now(), leaseEpoch?: number) {
    const msg = this.sql.exec('SELECT to_actor, delivered_at, read_at, acked_at, accepted_at, completed_at FROM messages WHERE id = ?', messageId).toArray()[0] as any;
    if (!msg) throw new Error("not_found");

    if (msg.to_actor === 'role:root' || caller.actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
    }

    // Recipient-only stage advancement
    if (!caller.isAdmin && !this.isAuthorizedForActor(caller, msg.to_actor, now)) {
      throw new Error("unauthorized");
    }

    if (stage === 'read_at' && !msg.delivered_at) throw new Error("invalid_stage");
    if (stage === 'acked_at' && !msg.read_at && !msg.delivered_at) throw new Error("invalid_stage");
    if (stage === 'accepted_at' && !msg.acked_at) throw new Error("invalid_stage");
    if (stage === 'completed_at' && !msg.accepted_at) throw new Error("invalid_stage");

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

    if (claim.holder === 'role:root' || caller.actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
    }

    if (!caller.isAdmin) {
      if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
        if (!caller.machineId) throw new Error("unauthorized");
        const rows = this.sql.exec('SELECT project FROM agents WHERE (tag = ? OR project || ":" || tag = ?) AND machine_id = ?', claim.holder, claim.holder, caller.machineId).toArray() as any[];
        if (!rows.length || rows[0].project !== claim.project) {
          throw new Error("unauthorized");
        }
      } else {
        if (!this.isAuthorizedForActor(caller, claim.holder, effectiveNow)) {
          throw new Error("unauthorized");
        }
        if (caller.actor) {
          let agentProj: string | undefined;
          if (caller.actor.includes(':')) {
            const [p, t] = caller.actor.split(':');
            const rows = this.sql.exec('SELECT project FROM agents WHERE project = ? AND tag = ?', p, t).toArray() as any[];
            if (rows.length === 1) agentProj = rows[0].project;
          } else {
            const rows = this.sql.exec('SELECT project FROM agents WHERE tag = ?', caller.actor).toArray() as any[];
            if (rows.length === 1) agentProj = rows[0].project;
          }
          if (!agentProj || agentProj !== claim.project) {
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

    if (caller && !caller.isAdmin && caller.actor !== 'role:root') {
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
        let agentProj: string | undefined;
        if (caller.actor.includes(':')) {
          const [p, t] = caller.actor.split(':');
          const rows = this.sql.exec('SELECT project FROM agents WHERE project = ? AND tag = ?', p, t).toArray() as any[];
          if (rows.length === 1) agentProj = rows[0].project;
        } else {
          const rows = this.sql.exec('SELECT project FROM agents WHERE tag = ?', caller.actor).toArray() as any[];
          if (rows.length === 1) agentProj = rows[0].project;
        }

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

  retentionCleanup(now: number = Date.now(), limit: number = 100): { messagesDeleted: number; claimsDeleted: number; hasMore: boolean } {
    const expiredMessages = this.sql.exec(
      'SELECT id FROM messages WHERE expires_at < ? LIMIT ?',
      now,
      limit
    ).toArray() as { id: string }[];
    let messagesDeleted = 0;
    for (const row of expiredMessages) {
      this.sql.exec('DELETE FROM messages WHERE id = ?', row.id);
      messagesDeleted++;
    }

    const expiredClaims = this.sql.exec(
      'SELECT id FROM claims WHERE expires_at < ? LIMIT ?',
      now,
      limit
    ).toArray() as { id: string }[];
    let claimsDeleted = 0;
    for (const row of expiredClaims) {
      this.sql.exec('DELETE FROM claims WHERE id = ?', row.id);
      claimsDeleted++;
    }

    const hasMore = expiredMessages.length === limit || expiredClaims.length === limit;
    return { messagesDeleted, claimsDeleted, hasMore };
  }

  getBusActors(caller?: CallerContext): string[] {
    if (!caller) {
      const agentActors = this.sql.exec('SELECT DISTINCT project || ":" || tag as actor FROM agents WHERE role != "root"').toArray().map((r: any) => r.actor);
      const machineActors = this.sql.exec('SELECT "machine:" || id as actor FROM machines').toArray().map((r: any) => r.actor);
      return Array.from(new Set([...agentActors, ...machineActors])).filter(a => a !== 'role:root');
    }

    if (caller.actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
    }

    if (caller.isAdmin) {
      const agentActors = this.sql.exec('SELECT DISTINCT project || ":" || tag as actor FROM agents WHERE role != "root"').toArray().map((r: any) => r.actor);
      const machineActors = this.sql.exec('SELECT "machine:" || id as actor FROM machines').toArray().map((r: any) => r.actor);
      return Array.from(new Set([...agentActors, ...machineActors])).filter(a => a !== 'role:root');
    }

    if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
      if (!caller.machineId) throw new Error("unauthorized");
      const agentActors = this.sql.exec(
        'SELECT DISTINCT project || ":" || tag as actor FROM agents WHERE machine_id = ? AND role != "root"',
        caller.machineId
      ).toArray().map((r: any) => r.actor);
      const machineActor = `machine:${caller.machineId}`;
      return Array.from(new Set([...agentActors, machineActor])).filter(a => a !== 'role:root');
    }

    if (caller.actor) {
      let regProj: string | undefined;
      let regTag: string | undefined;

      if (caller.actor.includes(':')) {
        const [cProj, cTag] = caller.actor.split(':');
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE project = ? AND tag = ? AND role != "root"', cProj, cTag).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
          regTag = rows[0].tag;
        }
      } else {
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE tag = ? AND role != "root"', caller.actor).toArray() as any[];
        if (rows.length === 1) {
          regProj = rows[0].project;
          regTag = rows[0].tag;
        }
      }

      if (regProj && regTag) {
        return [`${regProj}:${regTag}`, regTag];
      }
      return [caller.actor];
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

    if (actor === 'role:root' || caller.actor === 'role:root') {
      throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
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

      let aliases = [effectiveActor];
      if (effectiveActor.includes(':')) {
        const [cProj, cTag] = effectiveActor.split(':');
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE project = ? AND tag = ? AND role != "root"', cProj, cTag).toArray() as any[];
        if (rows.length === 1) aliases.push(rows[0].tag);
      } else {
        const rows = this.sql.exec('SELECT project, tag FROM agents WHERE tag = ? AND role != "root"', effectiveActor).toArray() as any[];
        if (rows.length === 1) aliases.push(`${rows[0].project}:${rows[0].tag}`);
      }
      aliases = Array.from(new Set(aliases));

      if (aliases.length === 1) {
        return this.sql.exec(
          'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
          aliases[0], aliases[0]
        ).toArray();
      } else {
        return this.sql.exec(
          'SELECT * FROM messages WHERE (to_actor IN (?, ?) OR from_actor IN (?, ?)) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
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
        return this.sql.exec(
          'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
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
        allowedActors.add(`${row.project}:${row.tag}`);
        allowedActors.add(row.tag);
      }
      const allowedList = Array.from(allowedActors);
      if (allowedList.length === 0) return [];

      const placeholders = allowedList.map(() => '?').join(',');
      const sqlQuery = `SELECT * FROM messages WHERE (to_actor IN (${placeholders}) OR from_actor IN (${placeholders})) AND to_actor != "role:root" AND from_actor != "role:root" ORDER BY created_at DESC LIMIT 100`;
      return this.sql.exec(sqlQuery, ...allowedList, ...allowedList).toArray();
    }

    throw new Error("unauthorized");
  }
}
