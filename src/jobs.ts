import { registryNameSql } from './agent_registry';
import { sha256, base64url } from './auth/crypto';
import { getSession } from './auth/session';
import { ensureRootScheduling, isRootSchedulingEnabled } from './root_scheduling';
import { RoleLeases, canonical, roleName, ROLE_JOB_CAPACITY, SUPPORTED_ROLE_PROFILES } from './role_leases';

function sha256Sync(input: string): string {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ]);
  const bytes = new TextEncoder().encode(input);
  const bitLen = bytes.length * 8;
  const padLen = ((bytes.length + 9 + 63) & ~63);
  const padded = new Uint8Array(padLen);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padLen - 8, Math.floor(bitLen / 0x100000000), false);
  view.setUint32(padLen - 4, bitLen >>> 0, false);

  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));

  for (let offset = 0; offset < padLen; offset += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = view.getUint32(offset + i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + temp1) >>> 0;
      d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }

  const out = new Uint8Array(32);
  const outView = new DataView(out.buffer);
  [h0, h1, h2, h3, h4, h5, h6, h7].forEach((v, idx) => outView.setUint32(idx * 4, v, false));
  return base64url(out);
}

export const JOBS_SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  payload JSON NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  offer_host_id TEXT,
  offer_machine_id TEXT,
  offer_expires_at TEXT,
  offer_attempts JSON NOT NULL DEFAULT '[]',
  claim_token_hash TEXT,
  holder_host_id TEXT,
  holder_machine_id TEXT,
  started_at TEXT,
  first_action_at TEXT,
  evidence_digest TEXT,
  native_session TEXT,
  launcher_task_id TEXT,
  lease_epoch INTEGER,
  finished_at TEXT,
  outcome_summary TEXT,
  outcome_evidence JSON
);

CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  holder_job_id TEXT,
  holder_machine_id TEXT,
  holder_host_id TEXT,
  holder_actor TEXT,
  epoch INTEGER NOT NULL DEFAULT 0,
  acquired_at TEXT,
  renewed_at TEXT,
  expires_at TEXT,
  released_at TEXT,
  release_reason TEXT
);

CREATE TABLE IF NOT EXISTS offers (
  job_id TEXT,
  machine_id TEXT,
  offered_at TEXT,
  response TEXT,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_kind TEXT,
  subject TEXT,
  state TEXT,
  detail TEXT,
  at TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource TEXT,
  target TEXT,
  detail TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoff_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lease_name TEXT NOT NULL,
  job_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  session_id TEXT,
  epoch INTEGER NOT NULL,
  summary TEXT NOT NULL,
  evidence_refs JSON,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS root_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  session_id TEXT,
  epoch INTEGER NOT NULL,
  action_name TEXT NOT NULL,
  action_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  result JSON NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  tag TEXT PRIMARY KEY,
  project TEXT,
  role TEXT,
  machine_id TEXT,
  engine TEXT,
  session_id TEXT,
  workspace TEXT,
  state TEXT,
  health_state TEXT,
  detail TEXT,
  lasthealth TEXT,
  health_at TEXT,
  last_seen_at TEXT
);
`;

const CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

export function generateUlid(
  nowMs: number = Date.now(),
  getRandomValues?: (array: Uint32Array) => Uint32Array
): string {
  let timeStr = "";
  let t = Math.max(0, Math.floor(nowMs));
  for (let i = 0; i < 10; i++) {
    const mod = t % 32;
    timeStr = CROCKFORD_BASE32[mod] + timeStr;
    t = Math.floor(t / 32);
  }

  const uints = new Uint32Array(4);
  if (getRandomValues) {
    getRandomValues(uints);
  } else {
    crypto.getRandomValues(uints);
  }

  let randStr = "";
  let buffer = BigInt(uints[0]) | (BigInt(uints[1]) << 32n) | (BigInt(uints[2]) << 64n);
  for (let i = 0; i < 16; i++) {
    const idx = Number(buffer & 31n);
    randStr = CROCKFORD_BASE32[idx] + randStr;
    buffer = buffer >> 5n;
  }

  return timeStr + randStr;
}

export function parseExpiryMs(expiresAt: string | number | null | undefined): number | null {
  if (expiresAt === null || expiresAt === undefined) return null;
  if (typeof expiresAt === 'number') {
    if (!Number.isFinite(expiresAt) || expiresAt <= 0) return null;
    return expiresAt < 1e11 ? expiresAt * 1000 : expiresAt;
  }
  const str = String(expiresAt).trim();
  if (str.length === 0) return null;
  if (str === 'Infinity' || str === '-Infinity' || str === 'NaN') return null;
  if (!str.includes('-') && !str.includes('T')) {
    const n = Number(str);
    if (Number.isFinite(n) && n > 0) return n < 1e11 ? n * 1000 : n;
    return null;
  }
  const parsed = Date.parse(str);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

export function parseEpoch(raw: any): number | null {
  if (raw === undefined || raw === null) return null;
  const n = typeof raw === 'number' ? raw : parseInt(String(raw), 10);
  if (!Number.isFinite(n) || isNaN(n) || n < 0 || !Number.isInteger(n)) return null;
  return n;
}

export function isLeaseExpired(expiresAt: string | number | null | undefined, nowMs: number): boolean {
  const exp = parseExpiryMs(expiresAt);
  if (exp === null) return true;
  return exp <= nowMs;
}

export function safeUtf8Summary(summary: string, maxBytes = 4096): string {
  if (!summary) return '';
  const encoded = new TextEncoder().encode(summary);
  if (encoded.length <= maxBytes) return summary;
  const dec = new TextDecoder('utf-8', { fatal: false, ignoreBOM: false });
  return dec.decode(encoded.subarray(0, maxBytes)).replace(/[\uFFFD]$/, '');
}

export interface JobsDependencies {
  now?: () => Date;
  getRandomValues?: (array: Uint32Array) => Uint32Array;
  rootHosts?: string[];
  busService?: any;
  healthService?: any;
  enableAlarms?: boolean;
  env?: any;
}

export interface Machine {
  id: string;
  name: string;
  hostname: string;
  last_checkin_at: string | null;
  last_heartbeat: string | null;
  drained: number;
  capabilities: string;
  roles?: string;
  state?: string;
}

export class JobsService {
  public roles: RoleLeases;
  private sql: SqlStorage;
  private ctx: DurableObjectState;
  private deps?: JobsDependencies;
  private rawNow: () => Date;
  private now: () => Date;
  private getRandomValues: (array: Uint32Array) => Uint32Array;
  private rootHostsPolicy?: string[];
  private busService?: any;
  private healthService?: any;
  private env?: any;
  private clockSnapshot: Date | null = null;

  constructor(sql: SqlStorage, ctx: DurableObjectState, deps?: JobsDependencies) {
    this.sql = sql;
    this.ctx = ctx;
    this.deps = deps;
    this.rawNow = deps?.now ? deps.now : (() => new Date());
    this.now = () => {
      if (this.clockSnapshot) {
        return this.clockSnapshot;
      }
      return this.rawNow();
    };
    this.getRandomValues = deps?.getRandomValues || ((arr) => crypto.getRandomValues(arr));
    this.rootHostsPolicy = deps?.rootHosts;
    this.busService = deps?.busService;
    this.healthService = deps?.healthService;
    this.env = deps?.env;

    for (const statement of JOBS_SCHEMA_SQL.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }

    const agentCols = ['engine TEXT', 'session_id TEXT', 'workspace TEXT', 'state TEXT', 'health_state TEXT', 'detail TEXT', 'lasthealth TEXT', 'health_at TEXT', 'last_seen_at TEXT'];
    for (const col of agentCols) {
      try {
        this.sql.exec(`ALTER TABLE agents ADD COLUMN ${col}`);
      } catch {}
    }
    try {
      this.sql.exec('ALTER TABLE machines ADD COLUMN state TEXT');
    } catch {}

    this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES ('root', 0)");
    ensureRootScheduling(this.sql);
    this.roles = new RoleLeases(this.sql);
  }

  configureRole(project: string, name: string, config: any, generation: number): any {
    const key = roleName(project, name);
    if (!key) return { error: 'invalid_role_name' };
    return this.ctx.storage.transactionSync(() => this.roles.configure(key, config, generation, this.rawNow().getTime()));
  }

  /** An enrolled allowlisted host requests an offer; claim remains the lease acquisition CAS. */
  offerRoleJob(project: string, name: string, hostId: string, credentialId: string, body: any): any {
    const key = roleName(project, name);
    if (!key || !body || Object.keys(body).some(k => !['generation','expected_epoch','idempotency_key','profile','payload'].includes(k)) ||
        !Number.isSafeInteger(body.generation) || !Number.isSafeInteger(body.expected_epoch) || body.expected_epoch < 0 ||
        typeof body.idempotency_key !== 'string' || !/^\d{4}-\d{2}-\d{2}:[A-Za-z0-9_-]{1,64}$/.test(body.idempotency_key) ||
        typeof body.profile !== 'string' || !SUPPORTED_ROLE_PROFILES.has(body.profile) ||
        !body.payload || typeof body.payload !== 'object' || Array.isArray(body.payload)) return { error: 'invalid_role_job' };
    if (body.profile === 'writer-run') {
      if (Object.keys(body.payload).some(k => k !== 'date') ||
          typeof body.payload.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(body.payload.date) ||
          !Number.isFinite(Date.parse(body.payload.date + 'T00:00:00Z')) ||
          new Date(body.payload.date + 'T00:00:00Z').toISOString().slice(0,10) !== body.payload.date) {
        return { error: 'invalid_role_job' };
      }
    } else {
      if (Object.keys(body.payload).some(k => !/^[A-Za-z0-9_-]{1,64}$/.test(k))) {
        return { error: 'invalid_role_job' };
      }
    }
    return this.ctx.storage.transactionSync(() => {
      const now = this.rawNow(); const ms = now.getTime();
      const role = this.roles.get(key);
      if (!role || !role.enabled || role.generation !== body.generation || !this.roles.clock(key, ms) ||
          !role.profiles.includes(body.profile) ||
          role.bindings[hostId] !== credentialId || !this.roles.host(hostId, credentialId, body.profile, ms)) return { error: 'role_unauthorized' };
      // Expired keys cannot execute again after bounded receipt retention.
      if (!body.idempotency_key.startsWith(now.toISOString().slice(0,10) + ':')) return { error: 'idempotency_key_expired' };
      const idempotency = `${key}:${body.idempotency_key}`;
      const payload = canonical({ ...body.payload, project });
      const existing: any = this.sql.exec('SELECT * FROM jobs WHERE idempotency_key=?', idempotency).toArray()[0];
      if (existing) return existing.role_generation === body.generation && existing.role_expected_epoch === body.expected_epoch &&
        existing.role_host_credential === credentialId && existing.offer_host_id === hostId && existing.payload === payload && existing.kind === body.profile
        ? { job_id: existing.id, duplicate: true } : { error: 'idempotency_conflict' };
      const lease: any = this.sql.exec('SELECT * FROM leases WHERE name=?', key).toArray()[0];
      if (!lease || lease.epoch !== body.expected_epoch || (lease.holder_job_id && !isLeaseExpired(lease.expires_at, ms))) return { error: 'role_conflict' };
      this.roles.prune(ms);
      if (Number(this.sql.exec('SELECT count(*) AS n FROM jobs WHERE role_name IS NOT NULL').one().n) >= ROLE_JOB_CAPACITY) return { error: 'role_job_capacity' };
      const id = this.generateId();
      this.sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_machine_id,offer_expires_at,role_name,role_generation,role_host_credential,role_expected_epoch)
        VALUES(?,?,?,?, 'offered',?,?,?,?,?,?,?,?,?,?)`, id, body.profile, idempotency, payload, now.toISOString(), new Date(ms + 25*60000).toISOString(),
        'machine:' + hostId, hostId, hostId, new Date(ms + 120000).toISOString(), key, role.generation, credentialId, lease.epoch);
      return { job_id: id, duplicate: false };
    });
  }

  private roleCurrent(job: any): boolean {
    if (!job.role_name) return true;
    const now = this.rawNow().getTime();
    if (!this.roles.binding(job, now) || !(Date.parse(job.deadline_at) > now)) return false;
    if (job.status === 'offered') return Date.parse(job.offer_expires_at) > now;
    const lease: any = this.sql.exec('SELECT * FROM leases WHERE name=?', job.role_name).toArray()[0];
    const cred: any = this.sql.exec("SELECT * FROM credentials WHERE kind='job' AND subject=? AND token_hash=?", job.id, job.claim_token_hash).toArray()[0];
    return !!lease && lease.holder_job_id === job.id && lease.epoch === job.lease_epoch && !isLeaseExpired(lease.expires_at, now) &&
      !!cred && !cred.revoked_at && Date.parse(cred.expires_at) > now && cred.machine_id === job.holder_host_id;
  }

  roleJobBinding(job: any): any {
    if (!job.role_name) return undefined;
    return { name: job.role_name, generation: job.role_generation, host_id: job.holder_host_id || job.offer_host_id,
      profile: job.kind, ttl_seconds: this.roles.get(job.role_name)?.ttl_seconds };
  }

  /** The authorization and durable intent on the real job share the existing SQL transaction. */
  async commitRoleExecution(jobId: string, token: string, epoch: number, generation: number): Promise<any> {
    const hash = await sha256(token);
    return this.ctx.storage.transactionSync(() => {
      const job: any = this.sql.exec('SELECT * FROM jobs WHERE id=?', jobId).toArray()[0];
      if (!job?.role_name || job.claim_token_hash !== hash || job.lease_epoch !== epoch || job.role_generation !== generation ||
          !['claimed','running'].includes(job.status) || !this.roleCurrent(job)) return { error: 'unauthorized_or_stale' };
      if (job.role_effect) return { execute: false, status: 'HOLD', reason: 'execution_intent_already_recorded' };
      if (this.hasUnresolvedWriterIntent(job)) return { execute: false, status: 'HOLD', reason: 'previous_execution_unresolved' };
      const unresolved = this.sql.exec(`SELECT id FROM jobs WHERE role_name IS NOT NULL AND role_effect IS NOT NULL
        AND role_finish IS NULL AND kind=? AND payload=? AND id!=? LIMIT 1`, job.kind, job.payload, jobId).toArray()[0];
      if (unresolved) return { execute: false, status: 'HOLD', reason: 'previous_execution_unresolved' };
      this.sql.exec('UPDATE jobs SET role_effect=? WHERE id=? AND role_effect IS NULL', canonical({ action: 'execute', epoch, generation, payload: job.payload }), jobId);
      return { execute: true, status: 'intent_recorded', role: job.role_name, generation, epoch, profile: job.kind, payload: JSON.parse(job.payload) };
    });
  }

  public setBusService(bus: any) {
    this.busService = bus;
  }

  public setHealthService(health: any) {
    this.healthService = health;
  }

  public setClock(now: () => Date) {
    this.rawNow = now;
  }

  private withClockSnapshot<T>(fn: (now: Date) => T): T {
    if (this.clockSnapshot) {
      return fn(this.clockSnapshot);
    }
    this.clockSnapshot = this.rawNow();
    try {
      return fn(this.clockSnapshot);
    } finally {
      this.clockSnapshot = null;
    }
  }

  private async withClockSnapshotAsync<T>(fn: (now: Date) => Promise<T>): Promise<T> {
    if (this.clockSnapshot) {
      return await fn(this.clockSnapshot);
    }
    this.clockSnapshot = this.rawNow();
    try {
      return await fn(this.clockSnapshot);
    } finally {
      this.clockSnapshot = null;
    }
  }

  private scheduleAlarm() {
    const alarmTime = this.getAlarmTime();
    if (alarmTime && this.deps?.enableAlarms && typeof this.ctx?.storage?.setAlarm === 'function') {
      try {
        this.ctx.storage.setAlarm(alarmTime);
      } catch {}
    }
  }

  private logAuditEvent(actor: string, action: string, resource: string, target: string, detail: any, at: string) {
    try {
      const detailStr = typeof detail === 'string' ? safeUtf8Summary(detail, 4096) : safeUtf8Summary(JSON.stringify(detail), 4096);
      this.sql.exec(
        'INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
        actor, action, resource, target, detailStr, at
      );
    } catch {}
  }

  private addHealthEvent(subject_kind: string, subject: string, state: string, detail: string, at: string) {
    try {
      this.sql.exec(
        'INSERT INTO health_events (subject_kind, subject, state, detail, at) VALUES (?, ?, ?, ?, ?)',
        subject_kind, subject, state, safeUtf8Summary(detail, 4096), at
      );
    } catch {}
  }

  private unbiasedSample<T>(items: T[]): T | null {
    if (items.length === 0) return null;
    if (items.length === 1) return items[0];
    const array = new Uint32Array(1);
    let randomValue: number;
    const maxValid = Math.floor(0xFFFFFFFF / items.length) * items.length;
    do {
      this.getRandomValues(array);
      randomValue = array[0];
    } while (randomValue >= maxValid);
    return items[randomValue % items.length];
  }

  private generateId(): string {
    return generateUlid(this.now().getTime(), this.getRandomValues);
  }

  private generateToken(): string {
    const array = new Uint32Array(8);
    this.getRandomValues(array);
    return 'hubt_' + Array.from(array).map(n => n.toString(16).padStart(8, '0')).join('');
  }

  getAlarmTime(): number | null {
    const now = this.now();
    const nowMs = now.getTime();
    const nextTimes: number[] = [];

    // 1. Pending offers
    const offers = this.sql.exec("SELECT offer_expires_at FROM jobs WHERE status = 'offered' AND offer_expires_at IS NOT NULL").toArray();
    for (const o of offers) {
      const t = parseExpiryMs(o.offer_expires_at as string);
      if (t !== null && t > nowMs) nextTimes.push(t);
    }

    // 2. Active leases
    const leases = this.sql.exec("SELECT expires_at FROM leases WHERE holder_job_id IS NOT NULL AND expires_at IS NOT NULL").toArray();
    for (const l of leases) {
      const t = parseExpiryMs(l.expires_at as string);
      if (t !== null && t > nowMs) nextTimes.push(t);
    }

    // 3. Claims awaiting start timeout (5m from acquired_at) - ONLY if job is claimed and NOT YET started
    const claimed = this.sql.exec(
      "SELECT leases.acquired_at FROM leases JOIN jobs ON leases.holder_job_id = jobs.id WHERE jobs.status = 'claimed' AND jobs.started_at IS NULL AND leases.acquired_at IS NOT NULL"
    ).toArray();
    for (const c of claimed) {
      const acq = parseExpiryMs(c.acquired_at as string);
      if (acq !== null) {
        const deadline = acq + 5 * 60000;
        if (deadline > nowMs) nextTimes.push(deadline);
      }
    }

    if (nextTimes.length === 0) return null;
    nextTimes.sort((a, b) => a - b);
    return nextTimes[0];
  }

  processAlarms(): number | null {
    return this.withClockSnapshot((now) => {
      const nowMs = now.getTime();
      const nowIso = now.toISOString();

      this.ctx.storage.transactionSync(() => {
        // 1. Expire offers (root-run)
        const offeredJobs = this.sql.exec("SELECT * FROM jobs WHERE kind = 'root-run' AND status = 'offered' AND offer_expires_at IS NOT NULL").toArray();
        const rootSchedulingEnabled = isRootSchedulingEnabled(this.sql);
        for (const job of offeredJobs) {
          // Root scheduling flag: while disabled, root-run offers are frozen (no expiry,
          // no re-offer); only a job whose deadline has passed is closed.
          if (!rootSchedulingEnabled) {
            this.expireRootRunPastDeadline(job, now);
            continue;
          }
          const exp = parseExpiryMs(job.offer_expires_at as string);
          if (exp !== null && exp <= nowMs) {
            this.addHealthEvent('job', job.id as string, 'failed', 'offer expired', nowIso);
            this.tryOfferNext(job, now);
          }
        }

        // 2. Expire leases (hard deadline or missed renew)
        const activeLeases = this.sql.exec("SELECT * FROM leases WHERE holder_job_id IS NOT NULL").toArray();
        for (const lease of activeLeases) {
          if (isLeaseExpired(lease.expires_at as any, nowMs)) {
            if (lease.name === 'root') {
              const inc = this.evaluateRootIncumbent();
              if (inc.within_grace && inc.heartbeat_fresh && inc.is_healthy_alive) {
                continue;
              }
            }
            const jobId = lease.holder_job_id as string;
            this.sql.exec("UPDATE jobs SET status = 'expired', finished_at = ?, outcome_summary = ? WHERE id = ?", nowIso, 'lease expired', jobId);
            this.addHealthEvent('job', jobId, 'failed', 'lease expired', nowIso);
            this.sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL", nowIso, jobId);
            this.sql.exec(
              "UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, expires_at = NULL, released_at = ?, release_reason = ? WHERE name = ?",
              nowIso, 'lease expired', lease.name as string
            );
          }
        }

        // 3. Claims awaiting first action (5 mins)
        const noStartLeases = this.sql.exec(
          "SELECT leases.* FROM leases JOIN jobs ON leases.holder_job_id = jobs.id WHERE jobs.started_at IS NULL AND jobs.status = 'claimed'"
        ).toArray();
        for (const lease of noStartLeases) {
          const acq = parseExpiryMs(lease.acquired_at as string);
          if (acq !== null && (acq + 5 * 60000) <= nowMs) {
            const jobId = lease.holder_job_id as string;
            this.sql.exec("UPDATE jobs SET status = 'failed', finished_at = ?, outcome_summary = ? WHERE id = ?", nowIso, 'no first action', jobId);
            this.addHealthEvent('job', jobId, 'failed', 'no first action', nowIso);
            this.sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL", nowIso, jobId);
            this.sql.exec(
              "UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, expires_at = NULL, released_at = ?, release_reason = ? WHERE name = ?",
              nowIso, 'no first action', lease.name as string
            );
          }
        }
      });

      return this.getAlarmTime();
    });
  }

  // Closes a pending/offered root-run job whose deadline has passed (status 'expired').
  // Returns true when the job was closed. Runs inside the caller's transaction.
  private expireRootRunPastDeadline(job: any, now: Date): boolean {
    if (job.kind !== 'root-run') return false;
    const deadlineMs = parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime();
    if (Number.isFinite(deadlineMs) && deadlineMs > now.getTime()) return false;
    const nowIso = now.toISOString();
    this.sql.exec(
      "UPDATE jobs SET status = 'expired', finished_at = ?, outcome_summary = ? WHERE id = ? AND status IN ('pending', 'offered')",
      nowIso, 'deadline passed before claim', job.id
    );
    this.addHealthEvent('job', job.id as string, 'failed', 'deadline passed before claim', nowIso);
    return true;
  }

  private tryOfferNext(job: any, now: Date) {
    // Role offers are explicit, host-bound requests. Never route one using root candidate policy.
    if (job.role_name) {
      this.sql.exec("UPDATE jobs SET status='expired',finished_at=? WHERE id=? AND status='offered'", now.toISOString(), job.id);
      return;
    }
    // Root scheduling flag: while disabled, no new offers for root-run jobs.
    // Runs inside the caller's storage transaction, so the check is race-free.
    if (job.kind === 'root-run' && !isRootSchedulingEnabled(this.sql)) return;
    const attempts = JSON.parse(job.offer_attempts as string || '[]');
    if (attempts.length >= 3) {
      this.sql.exec("UPDATE jobs SET status = 'no-host', finished_at = ? WHERE id = ?", now.toISOString(), job.id);
      this.addHealthEvent('job', job.id as string, 'no-host', 'failed to find host', now.toISOString());
      return;
    }

    const candidates = this.getCandidates(now, attempts, job.idempotency_key || job.created_at);
    if (candidates.length === 0) {
      this.sql.exec("UPDATE jobs SET status = 'no-host', finished_at = ? WHERE id = ?", now.toISOString(), job.id);
      this.addHealthEvent('job', job.id as string, 'no-host', 'no candidates available', now.toISOString());
      return;
    }

    // Never offer a root-run job that is already past its deadline: it could not be
    // claimed (ghost offer, e.g. the first alarm after re-enabling a long pause).
    if (this.expireRootRunPastDeadline(job, now)) return;

    const chosen = this.unbiasedSample(candidates);
    if (chosen) {
      attempts.push(chosen.id);
      const offerExpires = new Date(now.getTime() + 120 * 1000).toISOString();
      this.sql.exec(
        "UPDATE jobs SET status = 'offered', offer_host_id = ?, offer_machine_id = ?, offer_expires_at = ?, offer_attempts = ? WHERE id = ?", 
        chosen.id, chosen.id, offerExpires, JSON.stringify(attempts), job.id
      );
      this.sql.exec(
        "INSERT INTO offers (job_id, machine_id, offered_at, response, reason) VALUES (?, ?, ?, ?, ?)",
        job.id, chosen.id, now.toISOString(), 'pending', null
      );
    }
  }

  private getCandidates(now: Date, excludeIds: string[], slotKeyOrDate?: string | Date): Machine[] {
    const machines = this.sql.exec("SELECT * FROM machines").toArray() as unknown as Machine[];
    const candidates: Machine[] = [];
    const nowMs = now.getTime();

    // Immutable slot key used only for idempotency/backoff
    let currentSlotTime = nowMs;
    if (slotKeyOrDate) {
      if (slotKeyOrDate instanceof Date) {
        currentSlotTime = slotKeyOrDate.getTime();
      } else if (typeof slotKeyOrDate === 'string') {
        if (slotKeyOrDate.startsWith('root-run:')) {
          const parsed = new Date(slotKeyOrDate.slice('root-run:'.length)).getTime();
          if (Number.isFinite(parsed)) currentSlotTime = parsed;
        } else {
          const parsed = new Date(slotKeyOrDate).getTime();
          if (Number.isFinite(parsed)) currentSlotTime = parsed;
        }
      }
    }

    for (const m of machines) {
      if (excludeIds.includes(m.id)) continue;
      if (m.drained || m.state === 'drained') continue;

      if (!m.last_checkin_at) continue;
      const checkinTime = new Date(m.last_checkin_at).getTime();
      if (!Number.isFinite(checkinTime)) continue;
      const checkinAge = nowMs - checkinTime;
      if (checkinAge > 180000 || checkinAge < 0) continue;

      if (!this.checkHostRootRunCapability(m)) continue;

      if (!m.last_heartbeat) continue;
      let hb: any;
      try { hb = typeof m.last_heartbeat === 'string' ? JSON.parse(m.last_heartbeat) : m.last_heartbeat; } catch { continue; }
      if (!hb || !hb.gate || hb.gate.launch_allowed !== true) continue;

      // ROOT_HOSTS policy check
      if (this.rootHostsPolicy && this.rootHostsPolicy.length > 0) {
        if (!this.rootHostsPolicy.includes(m.hostname) && !this.rootHostsPolicy.includes(m.id) && !this.rootHostsPolicy.includes(m.name)) {
          continue;
        }
      }

      const lastRuns = this.sql.exec(
        "SELECT status, created_at, idempotency_key FROM jobs WHERE kind = 'root-run' AND (holder_host_id = ? OR holder_machine_id = ?) AND status NOT IN ('pending', 'offered', 'skip') ORDER BY created_at DESC LIMIT 2",
        m.id, m.id
      ).toArray() as any[];
      if (lastRuns.length === 2 &&
          (lastRuns[0].status === 'failed' || lastRuns[0].status === 'expired') &&
          (lastRuns[1].status === 'failed' || lastRuns[1].status === 'expired')) {
        let lastSlotTime = new Date(lastRuns[0].created_at).getTime();
        if (lastRuns[0].idempotency_key && lastRuns[0].idempotency_key.startsWith('root-run:')) {
          const parsed = new Date(lastRuns[0].idempotency_key.slice('root-run:'.length)).getTime();
          if (!isNaN(parsed)) lastSlotTime = parsed;
        }
        const cooldownSlotTime = lastSlotTime + 30 * 60000;
        if (currentSlotTime <= cooldownSlotTime) continue;
      }

      candidates.push(m);
    }
    return candidates;
  }

  tickRootSlot(slotStartUtc: string): { status: string; slot: string } {
    return this.withClockSnapshot((now) => {
      const nowIso = now.toISOString();
      const nowMs = now.getTime();
      let pendingBusError: { toActor: string; idempotencyKey: string; err: any } | null = null;
      let tickOutcome: { status: string; slot: string } = { status: 'ok', slot: slotStartUtc };

      try {
        this.ctx.storage.transactionSync(() => {
        this.processAlarms();

        // Root scheduling flag: a disabled tick creates no job, no offers.
        // Lease/offer expiry processing above still runs so live roots finish normally.
        if (!isRootSchedulingEnabled(this.sql)) {
          tickOutcome = { status: 'disabled', slot: slotStartUtc };
          return;
        }

        const idempotencyKey = `root-run:${slotStartUtc}`;
        const existing = this.sql.exec("SELECT id FROM jobs WHERE idempotency_key = ?", idempotencyKey).toArray();
        if (existing.length > 0) return;

        const slotStartMs = new Date(slotStartUtc).getTime();

        // Late-slot refusal: refuse if slot start is >= 25 minutes in the past
        if (Number.isFinite(slotStartMs) && nowMs >= slotStartMs + 25 * 60000) {
          const jobId = this.generateId();
          const deadlineAt = new Date(slotStartMs + 25 * 60000).toISOString();
          this.sql.exec(
            "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, outcome_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            jobId, 'root-run', idempotencyKey, '{}', 'failed', slotStartUtc, deadlineAt, 'system', 'refused: late slot'
          );
          this.addHealthEvent('job', idempotencyKey, 'failed', 'refused: late slot', nowIso);
          this.logAuditEvent('system', 'refuse', 'jobs/root-run', idempotencyKey, {
            reason: 'late_slot_refusal',
            slot: slotStartUtc
          }, nowIso);
          return;
        }

        // Active root incumbent check per spec §8.1
        const inc = this.evaluateRootIncumbent();
        if (inc.is_healthy_alive) {
          const holderJobId = inc.lease?.holder_job_id || null;
          const rootEpoch = inc.lease?.epoch ?? 0;
          const hostId = inc.host?.id || inc.registered_root?.machine_id || inc.lease?.holder_host_id || 'unknown';
          const checkinAgeS = inc.host?.checkin_age_s ?? 0;
          const skipSummary = `skip: previous root healthy (heartbeat ${checkinAgeS}s old); skipped slot ${slotStartUtc}`;

          const skipJobId = this.generateId();
          this.sql.exec(
            "INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor, outcome_summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            skipJobId, 'root-run', idempotencyKey, '{}', 'skip', slotStartUtc, new Date(slotStartMs + 25 * 60000).toISOString(), 'system', skipSummary
          );
          this.addHealthEvent('job', idempotencyKey, 'skip', 'root still running', nowIso);
          this.logAuditEvent('system', 'skip', 'leases/root', idempotencyKey, {
            reason: 'root still running',
            holder_job_id: holderJobId,
            holder_host_id: hostId,
            epoch: rootEpoch,
            slot: slotStartUtc
          }, nowIso);

          // Record skip handoff note referencing skipped slot start UTC
          const targetNoteJobId = skipJobId;
          const liveJob = holderJobId ? this.sql.exec("SELECT native_session FROM jobs WHERE id = ?", holderJobId).toArray()[0] as any : null;
          this.sql.exec(
            "INSERT INTO handoff_notes (lease_name, job_id, host_id, session_id, epoch, summary, evidence_refs, created_at) VALUES ('root', ?, ?, ?, ?, ?, '[]', ?)",
            targetNoteJobId, hostId || 'host-1', liveJob?.native_session || null, rootEpoch, skipSummary, nowIso
          );

          // Send skip note via bus without using denied 'role:root'
          if (this.busService) {
            const isRecipientValid = (r: string) => {
              if (r === 'role:root' || r.startsWith('role:root')) return false;
              if (typeof this.busService?.validateRecipient === 'function') {
                return this.busService.validateRecipient(r, nowMs);
              }
              return true;
            };

            let toActor: string | null = null;
            if (inc.registered_root) {
              if (isRecipientValid(inc.registered_root.tag)) {
                toActor = inc.registered_root.tag;
              } else if (inc.registered_root.project && isRecipientValid(`${inc.registered_root.project}:${inc.registered_root.tag}`)) {
                toActor = `${inc.registered_root.project}:${inc.registered_root.tag}`;
              }
            }
            if (!toActor && inc.lease?.holder_actor && inc.lease.holder_actor !== 'role:root' && isRecipientValid(inc.lease.holder_actor)) {
              toActor = inc.lease.holder_actor;
            }
            if (!toActor && hostId && hostId !== 'unknown' && isRecipientValid(`machine:${hostId}`)) {
              toActor = `machine:${hostId}`;
            }
            if (!toActor) {
              const rootAgent = this.sql.exec(`SELECT tag, project FROM agents WHERE role = 'root' AND (COALESCE(project,'') = '' OR ${registryNameSql('project')}) AND (state IS NULL OR state != 'retired') LIMIT 1`).toArray()[0] as any;
              if (rootAgent) {
                if (isRecipientValid(rootAgent.tag)) {
                  toActor = rootAgent.tag;
                } else if (rootAgent.project && isRecipientValid(`${rootAgent.project}:${rootAgent.tag}`)) {
                  toActor = `${rootAgent.project}:${rootAgent.tag}`;
                }
              }
            }
            if (toActor) {
              try {
                this.busService.send({
                  kind: 'job',
                  actor: 'system',
                  jobId: holderJobId || skipJobId || undefined,
                  scopes: ['bus:send']
                }, {
                  senderActor: 'system',
                  toActor: toActor,
                  kind: 'note',
                  body: `- ${nowIso} slot ${slotStartUtc} started and skipped: you were healthy (hb ${checkinAgeS}s old).`,
                  idempotencyKey: `skip-note:${slotStartUtc}:${holderJobId || toActor}`,
                  leaseEpoch: rootEpoch,
                  now: nowMs
                });
              } catch (err: any) {
                pendingBusError = { toActor, idempotencyKey, err };
                throw err;
              }
            }
          }
          return;
        }

        const jobId = this.generateId();
        const deadlineAt = new Date(slotStartMs + 25 * 60000).toISOString();
        this.sql.exec("INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          jobId, 'root-run', idempotencyKey, '{}', 'pending', nowIso, deadlineAt, 'system'
        );

        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        this.tryOfferNext(job, now);
      });
      } catch (err: any) {
        if (pendingBusError) {
          this.logAuditEvent('system', 'bus_error', 'leases/root', (pendingBusError as any).idempotencyKey, {
            error: (pendingBusError as any).err?.message || String((pendingBusError as any).err),
            to_actor: (pendingBusError as any).toActor
          }, nowIso);
        }
        throw err;
      }
      return tickOutcome;
    });
  }

  private getLeaseNameForJob(job: any): string {
    if (job.role_name) return job.role_name;
    if (job.kind === 'root-run') return 'root';
    if (job.kind === 'writer-run') {
      let date = '';
      try {
        const p = typeof job.payload === 'string' ? JSON.parse(job.payload) : job.payload;
        if (p?.date) date = p.date;
      } catch {}
      if (!date && job.idempotency_key && job.idempotency_key.startsWith('writer-run:')) {
        date = job.idempotency_key.slice('writer-run:'.length);
      }
      return date ? `writer:${date}` : `job:${job.id}`;
    }
    if (job.kind === 'head-recovery') {
      let project = 'default';
      try {
        const p = typeof job.payload === 'string' ? JSON.parse(job.payload) : job.payload;
        if (p?.project) project = p.project;
      } catch {}
      return `head:${project}`;
    }
    if (job.lease_name) return job.lease_name;
    return `job:${job.id}`;
  }

  /** Call within the claim/effect transaction. Uncertainty outlives lease custody. */
  private hasUnresolvedWriterIntent(job: any): boolean {
    if (job.kind !== 'writer-run') return false;
    const identity = this.getLeaseNameForJob({ ...job, role_name: null });
    if (!identity.startsWith('writer:')) return false;
    // Match the existing writer identity, including legacy dated keys without a
    // payload date. Project, role name, request key and epoch are not identities.
    return !!this.sql.exec(`SELECT id FROM jobs WHERE kind='writer-run' AND id!=?
      AND role_effect IS NOT NULL AND role_finish IS NULL
      AND COALESCE(NULLIF(CASE WHEN json_valid(payload) THEN json_extract(payload,'$.date') END,''),
        CASE WHEN substr(idempotency_key,1,11)='writer-run:' THEN substr(idempotency_key,12) END)=? LIMIT 1`,
      job.id, identity.slice('writer:'.length)).toArray()[0];
  }

  async claimJob(
    jobId: string,
    hostId: string,
    hostFreshGate: boolean,
    _simNow?: Date,
    hostCredentialId?: string
  ): Promise<{ error?: string, reason?: string, job_token?: string, lease_epoch?: number, deadline_at?: string, role_binding?: any }> {
    if (!hostFreshGate) return { error: 'gate_failed', reason: 'caller_gate_false' };

    return this.withClockSnapshotAsync(async (now) => {
      const nowIso = now.toISOString();
      const nowMs = now.getTime();

      const token = this.generateToken();
      const tokenHash = await sha256(token);

      let result: any = null;

      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job || job.status !== 'offered' || (job.offer_host_id !== hostId && job.offer_machine_id !== hostId)) {
          result = { error: 'conflict' }; return;
        }
        if (job.role_name && (job.role_host_credential !== hostCredentialId || !this.roleCurrent(job))) {
          result = { error: 'gate_failed', reason: 'role_unauthorized' }; return;
        }
        // Root scheduling flag: root-run claims (offered jobs included) are refused
        // inside the same claim transaction, so there is no flag-check race.
        if (job.kind === 'root-run' && !isRootSchedulingEnabled(this.sql)) {
          result = { error: 'conflict', reason: 'root_scheduling_disabled' }; return;
        }
        const exp = parseExpiryMs(job.offer_expires_at);
        if (exp === null || !Number.isFinite(exp) || exp <= nowMs) {
          result = { error: 'conflict' }; return;
        }

        const deadlineMs = parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime();
        if (!Number.isFinite(deadlineMs) || deadlineMs <= nowMs) {
          result = { error: 'conflict' }; return;
        }

        // Server revalidate current machine checkin/drain/capability/heartbeat gate in same SQL transaction
        const host = this.sql.exec("SELECT * FROM machines WHERE id = ?", hostId).toArray()[0] as any;
        if (!host) {
          result = { error: 'gate_failed', reason: 'host_not_found' }; return;
        }
        if (host.drained === 1 || host.drained === true || host.state === 'drained') {
          result = { error: 'gate_failed', reason: 'host_drained' }; return;
        }
        if (job.kind === 'root-run' && !this.checkHostRootRunCapability(host)) {
          result = { error: 'gate_failed', reason: 'host_incapable' }; return;
        }

        if (!host.last_checkin_at) {
          result = { error: 'gate_failed', reason: 'missing_checkin' }; return;
        }
        const checkinTime = new Date(host.last_checkin_at).getTime();
        if (!Number.isFinite(checkinTime)) {
          result = { error: 'gate_failed', reason: 'nonfinite_checkin' }; return;
        }
        const checkinAge = nowMs - checkinTime;
        if (checkinAge < 0 || checkinAge > 180000) {
          result = { error: 'gate_failed', reason: checkinAge < 0 ? 'future_checkin' : 'stale_checkin' }; return;
        }

        if (!host.last_heartbeat) {
          result = { error: 'gate_failed', reason: 'missing_heartbeat' }; return;
        }
        let hb: any;
        try {
          hb = typeof host.last_heartbeat === 'string' ? JSON.parse(host.last_heartbeat) : host.last_heartbeat;
        } catch {
          result = { error: 'gate_failed', reason: 'invalid_heartbeat_json' }; return;
        }
        if (!hb || !hb.gate || hb.gate.launch_allowed !== true) {
          result = { error: 'gate_failed', reason: 'gate_launch_not_allowed' }; return;
        }

        // Role custody must not bypass the existing writer:<date> exclusion.
        // Both legacy and role claims consult live leases and durable uncertainty
        // in this CAS, before acquiring custody or minting a job credential.
        if (job.kind === 'writer-run') {
          if (this.hasUnresolvedWriterIntent(job)) {
            result = { error: 'conflict', reason: 'writer_date_busy' }; return;
          }
          const profileLease = this.getLeaseNameForJob({ ...job, role_name: null });
          if (profileLease.startsWith('writer:')) {
            const holders = this.sql.exec(`SELECT j.*,l.expires_at AS profile_expires_at FROM jobs j JOIN leases l ON l.holder_job_id=j.id
              WHERE j.kind='writer-run' AND j.id!=? AND j.status IN ('claimed','running')`, jobId).toArray();
            if (holders.some(h => this.getLeaseNameForJob({ ...h, role_name: null }) === profileLease &&
                !isLeaseExpired(h.profile_expires_at as any, nowMs))) {
              result = { error: 'conflict', reason: 'writer_date_busy' }; return;
            }
          }
        }

        const leaseName = this.getLeaseNameForJob(job);
        this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES (?, 0)", leaseName);

        const currentLease = this.sql.exec("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
        if (job.role_name && (currentLease.epoch !== job.role_expected_epoch || !Number.isSafeInteger(currentLease.epoch) || currentLease.epoch >= Number.MAX_SAFE_INTEGER)) { result = { error: 'conflict' }; return; }
        if (job.kind === 'root-run' || leaseName === 'root') {
          const incumbent = this.evaluateRootIncumbent();
          if (!incumbent.can_takeover) {
            result = { error: 'conflict', reason: incumbent.takeover_denial_reason || 'root incumbent not eligible for takeover' };
            return;
          }
        } else {
          if (currentLease && currentLease.holder_job_id) {
            if (!isLeaseExpired(currentLease.expires_at, nowMs)) {
              result = { error: 'conflict' };
              return;
            }
          }
        }

        const roleTtl = job.role_name ? this.roles.get(job.role_name).ttl_seconds * 1000 : 20 * 60000;
        const leaseExpiresMs = Math.min(nowMs + roleTtl, deadlineMs);
        const currentEpoch = currentLease ? (currentLease.epoch ?? 0) : 0;
        const jobActor = job.kind === 'root-run' ? 'role:root' : (job.actor || 'role:worker');

        const acquire = this.sql.exec(
          "UPDATE leases SET holder_job_id = ?, holder_host_id = ?, holder_machine_id = ?, holder_actor = ?, epoch = epoch + 1, acquired_at = ?, renewed_at = ?, expires_at = ? WHERE name = ? AND epoch = ?",
          jobId, hostId, hostId, jobActor, nowIso, nowIso, new Date(leaseExpiresMs).toISOString(), leaseName, currentEpoch
        );

        if (acquire.rowsWritten !== 1) {
          result = { error: 'conflict' }; return;
        }

        const updatedLease = this.sql.exec("SELECT epoch FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
        const newEpoch = updatedLease.epoch;

        // Fence old incumbent job if one was held
        const oldJobId = currentLease ? currentLease.holder_job_id : null;
        if (oldJobId && oldJobId !== jobId) {
          this.sql.exec(
            "UPDATE jobs SET status = 'fenced', finished_at = ?, outcome_summary = ? WHERE id = ? AND status IN ('claimed', 'running', 'offered')",
            nowIso, `fenced: superseded by takeover epoch ${newEpoch}`, oldJobId
          );
          this.sql.exec(
            "UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL",
            nowIso, oldJobId
          );
          this.addHealthEvent('job', oldJobId, 'fenced', `superseded by takeover epoch ${newEpoch}`, nowIso);
        }

        this.sql.exec(
          "UPDATE jobs SET status = 'claimed', claim_token_hash = ?, holder_host_id = ?, holder_machine_id = ?, lease_epoch = ? WHERE id = ?",
          tokenHash, hostId, hostId, newEpoch, jobId
        );

        // Mint scoped job credential in credentials table
        const credId = this.generateId();
        const jobScopes = job.role_name ? [] : job.kind === 'root-run'
          ? ['lease:root', 'jobs:create', 'bus:send', 'bus:read', 'health:report']
          : ['bus:send', 'bus:read', 'health:report'];

        this.sql.exec(
          "INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'job', ?, ?, ?, ?, ?, ?, 'system', ?, ?)",
          credId, tokenHash, credId, jobId, hostId, jobActor, JSON.stringify(jobScopes), nowIso, new Date(leaseExpiresMs).toISOString()
        );

        result = { job_token: token, lease_epoch: newEpoch, deadline_at: job.deadline_at,
          ...(job.role_name ? { role_binding: this.roleJobBinding(job) } : {}) };
      });

      if (!result?.error) {
        this.scheduleAlarm();
      }

      return result;
    });
  }

  async declineJob(jobId: string, hostId: string, reason: string, hostCredentialId?: string): Promise<{ error?: string }> {
    return this.withClockSnapshot((now) => {
      let result: any = {};
      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (job?.role_name && (job.role_host_credential !== hostCredentialId || !this.roleCurrent(job))) { result = { error: 'unauthorized_or_stale' }; return; }
        if (!job || job.status !== 'offered' || (job.offer_host_id !== hostId && job.offer_machine_id !== hostId)) {
          result = { error: 'conflict' }; return;
        }
        this.addHealthEvent('job', jobId, 'declined', reason.substring(0, 4096), now.toISOString());
        this.tryOfferNext(job, now);
      });
      this.scheduleAlarm();
      return result;
    });
  }

  async validateEpoch(jobId: string, jobTokenPlain: string, epoch: number): Promise<boolean> {
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') return false;
    return this.withClockSnapshotAsync(async (now) => {
      const tokenHash = await sha256(jobTokenPlain);
      let valid = false;
      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) return;
        if (!this.roleCurrent(job)) return;

        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          tokenHash, jobId
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) return;
        const exp = parseExpiryMs(cred.expires_at);
        if (exp === null || exp <= now.getTime()) return;

        const leaseName = this.getLeaseNameForJob(job);
        const lease = this.sql.exec("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
        if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId) return;

        if (!isLeaseExpired(lease.expires_at, now.getTime())) valid = true;
      });
      return valid;
    });
  }

  private resolveLeaseForJob(jobId: string, job: any): any {
    const direct = this.sql.exec("SELECT * FROM leases WHERE holder_job_id = ?", jobId).toArray()[0] as any;
    if (direct) return direct;
    const leaseName = this.getLeaseNameForJob(job);
    return this.sql.exec("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
  }

  async startJob(
    jobId: string,
    jobTokenPlain: string,
    epoch: number,
    evidenceDigest: string,
    nativeSession: string,
    launcherTaskId: string
  ): Promise<{ error?: string }> {
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') {
      return { error: 'unauthorized_or_stale' };
    }
    const isForbiddenPattern = (s: unknown): boolean => {
      if (!s || typeof s !== 'string') return true;
      const lower = s.trim().toLowerCase();
      if (lower.length === 0) return true;
      if (lower.includes('unknown') || lower.includes('none') || lower.includes('null') || lower.includes('undefined')) return true;
      return false;
    };
    return this.withClockSnapshotAsync(async (now) => {
      const tokenHash = await sha256(jobTokenPlain);
      const nowIso = now.toISOString();
      let result: any = {};

      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }
        if (!this.roleCurrent(job)) { result = { error: 'unauthorized_or_stale' }; return; }
        if (job.role_name && !job.role_effect) { result = { error: 'execution_intent_required' }; return; }
        if (job.role_name && job.status === 'running' && job.evidence_digest === evidenceDigest && job.native_session === nativeSession && job.launcher_task_id === launcherTaskId) return;
        if (job.status !== 'claimed') { result = { error: 'invalid_status' }; return; }
        // Root scheduling flag gates root-run START transitions; renew/finish of an
        // already-running root do not pass through here and keep working while disabled.
        if (job.kind === 'root-run' && !isRootSchedulingEnabled(this.sql)) {
          result = { error: 'root_scheduling_disabled' }; return;
        }

        // Check credential validity, non-revocation, and non-expiry
        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          tokenHash, jobId
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }
        const exp = parseExpiryMs(cred.expires_at);
        if (exp === null || exp <= now.getTime()) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        const lease = this.resolveLeaseForJob(jobId, job);
        if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId || isLeaseExpired(lease.expires_at, now.getTime())) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }
        if (lease.acquired_at) {
          const acqMs = parseExpiryMs(lease.acquired_at);
          if (acqMs !== null && (now.getTime() - acqMs) > 5 * 60000) {
            result = { error: 'first_action_window_expired' };
            return;
          }
        }

        if (isForbiddenPattern(evidenceDigest) || !/^(sha256:[0-9a-fA-F]{1,64}|[0-9a-fA-F]{16,64})$/.test(String(evidenceDigest).trim())) {
          result = { error: 'missing_first_action_evidence' };
          return;
        }
        if (isForbiddenPattern(nativeSession)) {
          result = { error: 'missing_native_session' };
          return;
        }
        if (isForbiddenPattern(launcherTaskId)) {
          result = { error: 'missing_launcher_task_id' };
          return;
        }

        this.sql.exec("UPDATE jobs SET status = 'running', started_at = ?, first_action_at = ?, evidence_digest = ?, native_session = ?, launcher_task_id = ? WHERE id = ?",
          nowIso, nowIso, evidenceDigest, nativeSession, launcherTaskId, jobId);

        if (job.kind === 'root-run') {
          this.logAuditEvent('role:root', 'start', 'leases/root', jobId, {
            job_id: jobId,
            epoch,
            host_id: job.holder_host_id || job.holder_machine_id,
            native_session: nativeSession,
            launcher_task_id: launcherTaskId,
            evidence_digest: evidenceDigest
          }, nowIso);
          this.addHealthEvent('job', jobId, 'running', `root run started (epoch ${epoch})`, nowIso);
        }
      });
      this.scheduleAlarm();
      return result;
    });
  }

  async renewJob(jobId: string, jobTokenPlain: string, epoch: number): Promise<{ error?: string }> {
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') {
      return { error: 'unauthorized_or_stale' };
    }
    return this.withClockSnapshotAsync(async (now) => {
      const nowMs = now.getTime();
      const tokenHash = await sha256(jobTokenPlain);
      let result: any = {};

      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }
        if (!this.roleCurrent(job)) { result = { error: 'unauthorized_or_stale' }; return; }

        if (job.status !== 'running' || !job.started_at) {
          result = { error: 'unauthorized_or_stale' }; return;
        }

        const deadlineMs = parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime();
        if (nowMs >= deadlineMs) {
          result = { error: 'deadline_exceeded' }; return;
        }

        // Check credential validity and non-revocation
        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          tokenHash, jobId
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }
        const credExpiresMs = parseExpiryMs(cred.expires_at);
        if (credExpiresMs === null || credExpiresMs <= nowMs) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        const lease = this.resolveLeaseForJob(jobId, job);
        if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId || isLeaseExpired(lease.expires_at, nowMs)) {
          result = { error: 'unauthorized_or_stale' }; return;
        }

        const ttl = job.role_name ? this.roles.get(job.role_name).ttl_seconds * 1000 : 20 * 60000;
        const newExpiresMs = Math.min(nowMs + ttl, deadlineMs);
        const newExpiresIso = new Date(newExpiresMs).toISOString();

        const updateRes = this.sql.exec(
          "UPDATE leases SET expires_at = ?, renewed_at = ? WHERE name = ? AND epoch = ? AND holder_job_id = ?",
          newExpiresIso, now.toISOString(), lease.name, epoch, jobId
        );
        if (updateRes.rowsWritten !== 1) {
          result = { error: 'unauthorized_or_stale' }; return;
        }

        // Update credential expiry
        this.sql.exec("UPDATE credentials SET expires_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL", newExpiresIso, jobId);
      });

      this.scheduleAlarm();
      return result;
    });
  }

  async finishJob(jobId: string, jobTokenPlain: string, epoch: number, status: string, summary: string, evidenceRefs: string[]): Promise<{ error?: string }> {
    const allowedStatuses = new Set(['succeeded', 'finished', 'failed', 'cancelled']);
    if (!allowedStatuses.has(status)) {
      return { error: 'invalid_status' };
    }
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') {
      return { error: 'unauthorized_or_stale' };
    }
    return this.withClockSnapshotAsync(async (now) => {
      const tokenHash = await sha256(jobTokenPlain);
      const nowMs = now.getTime();
      const nowIso = now.toISOString();
      let result: any = {};

      this.ctx.storage.transactionSync(() => {
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }

        if (job.role_name) {
          const ms = this.rawNow().getTime();
          const lease: any = this.sql.exec('SELECT * FROM leases WHERE name=?', job.role_name).toArray()[0];
          const cred: any = this.sql.exec("SELECT * FROM credentials WHERE kind='job' AND subject=? AND token_hash=?", jobId, tokenHash).toArray()[0];
          const receipt = canonical({ status, summary: safeUtf8Summary(summary, 4096), evidenceRefs });
          if (!this.roles.binding(job, ms) || !lease || lease.epoch !== epoch || !cred || cred.revoked_at || !(Date.parse(cred.expires_at) > ms)) {
            result = { error: 'unauthorized_or_stale' }; return;
          }
          if (job.role_finish) { result = job.role_finish === receipt ? {} : { error: 'completion_conflict' }; return; }
          if (!this.roleCurrent(job)) { result = { error: 'unauthorized_or_stale' }; return; }
          if (!Array.isArray(evidenceRefs) || evidenceRefs.length > 16 || evidenceRefs.some(r => typeof r !== 'string' || r.length > 256)) {
            result = { error: 'invalid_evidence' }; return;
          }
        }

        // Terminal status check: cannot finish an already terminal or fenced job
        const terminalStatuses = ['succeeded', 'finished', 'failed', 'expired', 'cancelled', 'fenced'];
        if (terminalStatuses.includes(job.status)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        // Unstarted claimed job cannot finish as succeeded or finished
        if ((status === 'succeeded' || status === 'finished') && (job.status === 'claimed' || !job.started_at || !job.first_action_at)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        // Hardstop check: claimed or running job past hardstop (including grace for root-run) cannot finish normally
        const rootGraceMs = (job.kind === 'root-run') ? 120000 : 0;
        const hardstopWithGraceMs = job.deadline_at ? (parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime()) + rootGraceMs : null;
        if (hardstopWithGraceMs !== null && nowMs >= hardstopWithGraceMs) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        // Check credential validity and non-revocation
        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          tokenHash, jobId
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }
        const credExpiresMs = parseExpiryMs(cred.expires_at);
        if (credExpiresMs === null || credExpiresMs <= nowMs) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        const lease = this.resolveLeaseForJob(jobId, job);
        if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId) { result = { error: 'unauthorized_or_stale' }; return; }
        if (isLeaseExpired(lease.expires_at, nowMs)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        const safeSummary = safeUtf8Summary(summary, 4096);

        const leaseUpdate = this.sql.exec(
          "UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, expires_at = NULL, released_at = ?, release_reason = ? WHERE name = ? AND epoch = ?",
          nowIso, status, lease.name, epoch
        );
        if (leaseUpdate.rowsWritten !== 1) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }

        this.sql.exec("UPDATE jobs SET status = ?, finished_at = ?, outcome_summary = ?, outcome_evidence = ? WHERE id = ?",
          status, nowIso, safeSummary, JSON.stringify(evidenceRefs), jobId);

        if (job.role_name) this.sql.exec('UPDATE jobs SET role_finish=? WHERE id=?', canonical({ status, summary: safeSummary, evidenceRefs }), jobId);

        // Invalidate/revoke job credential
        if (!job.role_name) this.sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL", nowIso, jobId);

        this.addHealthEvent('job', jobId, status, safeSummary, nowIso);

        if (job.kind === 'root-run') {
          this.logAuditEvent('role:root', 'finish', 'leases/root', jobId, {
            job_id: jobId,
            epoch,
            status,
            summary_len: safeSummary.length,
            evidence_refs_count: (evidenceRefs || []).length
          }, nowIso);
        }
      });

      this.scheduleAlarm();

      return result;
    });
  }

  async reconcileTerminalJob(
    jobId: string,
    callerHostId: string,
    callerTokenId: string,
    body: any,
    currentHostAuthority?: () => boolean
  ): Promise<{
    error?: string;
    status_code?: number;
    status?: string;
    reconciled?: boolean;
    job_id?: string;
    epoch?: number;
    generation?: number;
    outcome?: string;
    terminal_case?: string;
    receipt?: any;
    replay?: boolean;
    reason?: string;
  }> {
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return { error: 'invalid_reconciliation', status_code: 400 };
    }
    if (Object.keys(body).some(key => !['epoch', 'generation', 'terminal_case', 'native_session', 'summary', 'evidence_refs'].includes(key)) ||
        (body.summary !== undefined && typeof body.summary !== 'string') ||
        (body.native_session !== undefined && (typeof body.native_session !== 'string' || body.native_session.length === 0 || body.native_session.length > 256)) ||
        (body.evidence_refs !== undefined && (!Array.isArray(body.evidence_refs) || body.evidence_refs.length > 16 ||
          body.evidence_refs.some((ref: any) => typeof ref !== 'string' || ref.length > 256)))) {
      return { error: 'invalid_reconciliation', status_code: 400 };
    }
    const epoch = typeof body.epoch === 'number' && Number.isSafeInteger(body.epoch) && body.epoch > 0 ? body.epoch : null;
    if (epoch === null) {
      return { error: 'invalid_epoch', status_code: 400 };
    }
    const generation = typeof body.generation === 'number' && Number.isSafeInteger(body.generation) && body.generation > 0 ? body.generation : null;
    if (generation === null) {
      return { error: 'invalid_generation', status_code: 400 };
    }

    return this.withClockSnapshot((now) => {
      const nowMs = now.getTime();
      const nowIso = now.toISOString();

      return this.ctx.storage.transactionSync(() => {
        // Reuse the existing current host-family/approval gate after reading the body.
        if (typeof currentHostAuthority !== 'function' || !currentHostAuthority()) {
          return { error: 'unauthorized_or_stale', status_code: 403 };
        }
        const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id=?', callerTokenId).toArray()[0];
        const machine: any = this.sql.exec('SELECT * FROM machines WHERE id=?', callerHostId).toArray()[0];
        let scopes: any;
        try { scopes = JSON.parse(cred?.scopes); } catch { scopes = null; }
        if (!cred || cred.kind !== 'host' || cred.revoked_at || !(Date.parse(cred.expires_at) > nowMs) ||
            cred.machine_id !== callerHostId || !Array.isArray(scopes) || !scopes.includes('host') || scopes.includes('admin') ||
            !machine?.registered_at || machine.drained || ['disabled', 'revoked', 'drained', 'dead'].includes(machine.state)) {
          return { error: 'unauthorized_or_stale', status_code: 403 };
        }

        // 5. Job lookup
        const job: any = this.sql.exec('SELECT * FROM jobs WHERE id = ?', jobId).toArray()[0];
        if (!job) {
          return { error: 'not_found', status_code: 404 };
        }

        // 6. Owning machine verification
        const jobOwningHost = job.holder_host_id || job.holder_machine_id || job.offer_host_id || job.offer_machine_id;
        if (!jobOwningHost || jobOwningHost !== callerHostId) {
          return { error: 'unauthorized_or_stale', status_code: 403 };
        }

        // 7. Role configuration check: role must exist, be enabled, and current role generation must match
        if (!job.role_name) {
          return { error: 'invalid_role_job', status_code: 400 };
        }
        const role = this.roles.get(job.role_name);
        if (!role || !role.enabled) {
          return { error: 'role_unauthorized', status_code: 403 };
        }
        if (role.generation !== job.role_generation) {
          return { error: 'generation_mismatch', status_code: 409 };
        }
        // Current role config must still authorize same owning host
        if (role.bindings[callerHostId] !== job.role_host_credential ||
            machine.credential_id !== job.role_host_credential || !role.profiles.includes(job.kind)) {
          return { error: 'role_unauthorized', status_code: 403 };
        }

        // 8. Expected epoch and generation correlation
        if (epoch !== job.lease_epoch) {
          return { error: 'epoch_mismatch', status_code: 409 };
        }
        if (generation !== job.role_generation) {
          return { error: 'generation_mismatch', status_code: 409 };
        }

        // Lease loss is not a terminal job status; successful history is never reconciled as failure.
        if (!['expired', 'fenced', 'failed', 'cancelled'].includes(job.status)) {
          return { error: 'job_not_terminal', status_code: 409 };
        }

        // Determine terminal case: 'unstarted' vs 'executed'
        let terminalCase = body.terminal_case;
        if (!terminalCase) {
          if (job.role_effect === null && job.started_at === null && job.first_action_at === null && job.native_session === null) {
            terminalCase = 'unstarted';
          } else {
            terminalCase = 'executed';
          }
        }
        if (terminalCase !== 'unstarted' && terminalCase !== 'executed') {
          return { error: 'invalid_reconciliation', status_code: 400 };
        }

        // 10. Validate terminal case prerequisites
        let targetSessionId: string | null = null;
        let nativeRetirement: any = null;
        if (terminalCase === 'unstarted') {
          if (body.native_session !== undefined) return { error: 'invalid_reconciliation', status_code: 400 };
          if (job.role_effect !== null || job.started_at !== null || job.first_action_at !== null || job.native_session !== null) {
            return { error: 'execution_intent_recorded', status_code: 409 };
          }
        } else {
          // Executed case: require real existing full native binding and maintained positive same-incarnation finished proof
          targetSessionId = typeof job.native_session === 'string' && job.native_session.length > 0 &&
            typeof job.launcher_task_id === 'string' && job.launcher_task_id.length > 0 &&
            job.started_at && job.first_action_at ? job.native_session : null;
          if (!targetSessionId) {
            return { status: 'HOLD', reconciled: false, reason: 'missing_native_session', status_code: 409 };
          }
          if (job.native_session && body.native_session && body.native_session !== job.native_session) {
            return { status: 'HOLD', reconciled: false, reason: 'native_session_mismatch', status_code: 409 };
          }

          const sessions = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', targetSessionId).toArray() as any[];
          if (sessions.length !== 1) {
            return { status: 'HOLD', reconciled: false, reason: 'session_not_unique', status_code: 409 };
          }
          const session = sessions[0];
          const jobHost = job.holder_host_id || job.holder_machine_id || job.offer_host_id || job.offer_machine_id;
          if (session.machine_id !== callerHostId || session.machine_id !== jobHost) {
            return { status: 'HOLD', reconciled: false, reason: 'machine_mismatch', status_code: 409 };
          }
          if (session.state !== 'stopped') {
            return { status: 'HOLD', reconciled: false, reason: 'session_not_stopped', status_code: 409 };
          }

          let binding: any;
          try {
            binding = JSON.parse(session.binding_json);
          } catch {
            binding = null;
          }
          if (!binding || typeof binding !== 'object' || binding.provenance !== 'observed_native') {
            return { status: 'HOLD', reconciled: false, reason: 'binding_not_imported', status_code: 409 };
          }

          const expectedTag = 'task-' + job.launcher_task_id;
          if (binding.tag !== expectedTag || binding.native_id !== session.id) {
            return { status: 'HOLD', reconciled: false, reason: 'binding_mismatch', status_code: 409 };
          }

          const marker = binding.retirement;
          if (!marker || typeof marker !== 'object') {
            return { status: 'HOLD', reconciled: false, reason: 'retirement_marker_missing', status_code: 409 };
          }

          // Session binding generation is independent of role generation (normally binding 1 vs role N)
          if (
            marker.native_id !== session.id ||
            marker.native_id !== binding.native_id ||
            marker.tag !== expectedTag ||
            marker.tag !== binding.tag ||
            marker.workspace !== binding.workspace ||
            typeof marker.workspace !== 'string' ||
            marker.workspace.length === 0 ||
            marker.engine !== session.engine ||
            typeof marker.engine !== 'string' ||
            marker.engine.length === 0 ||
            marker.created_at_ms !== binding.native_created_at_ms ||
            !Number.isSafeInteger(marker.created_at_ms) ||
            marker.created_at_ms <= 0 ||
            marker.generation !== binding.generation ||
            !Number.isSafeInteger(marker.generation) || marker.generation <= 0 ||
            marker.cause !== 'finished' ||
            !Number.isSafeInteger(marker.finished_at_ms) ||
            marker.finished_at_ms < marker.created_at_ms || marker.finished_at_ms > nowMs
          ) {
            return { status: 'HOLD', reconciled: false, reason: 'retirement_marker_invalid', status_code: 409 };
          }
          nativeRetirement = {
            native_id: marker.native_id, tag: marker.tag, workspace: marker.workspace, engine: marker.engine,
            created_at_ms: marker.created_at_ms, generation: marker.generation,
            cause: marker.cause, finished_at_ms: marker.finished_at_ms
          };
        }

        // 11. Construct exact canonical failed/inconclusive reconciliation receipt
        const summary = safeUtf8Summary(body.summary || `terminal reconciliation: ${terminalCase}`, 4096);
        const evidenceRefs = body.evidence_refs || [];
        const receiptObj = {
          epoch: job.lease_epoch,
          evidenceRefs,
          generation: job.role_generation,
          job_id: jobId,
          outcome: 'failed',
          original_status: job.status,
          reconciled: true,
          status: 'failed',
          summary,
          terminal_case: terminalCase,
          ...(terminalCase === 'executed' && targetSessionId ? { native_session: targetSessionId, native_retirement: nativeRetirement } : {})
        };
        const receipt = canonical(receiptObj);

        // 12. Idempotency / conflict check
        if (job.role_finish) {
          if (job.role_finish === receipt) {
            return {
              reconciled: true,
              status: 'reconciled',
              job_id: jobId,
              epoch: job.lease_epoch,
              generation: job.role_generation,
              outcome: 'failed',
              terminal_case: terminalCase,
              receipt: receiptObj,
              replay: true
            };
          }
          return { error: 'reconciliation_conflict', status_code: 409 };
        }

        // 13. Persist role_finish without overwriting original expired/fenced job status or effect
        this.sql.exec('UPDATE jobs SET role_finish = ? WHERE id = ?', receipt, jobId);

        // 14. Audit and health event
        this.logAuditEvent('host:' + callerHostId, 'reconcile', 'jobs/' + job.kind, jobId, receiptObj, nowIso);
        this.addHealthEvent('job', jobId, job.status, `role job reconciled as terminal failed (${terminalCase})`, nowIso);

        return {
          reconciled: true,
          status: 'reconciled',
          job_id: jobId,
          epoch: job.lease_epoch,
          generation: job.role_generation,
          outcome: 'failed',
          terminal_case: terminalCase,
          receipt: receiptObj
        };
      });
    });
  }

  async createJob(kind: string, payload: any, actor: string, deadlineMins: number = 25, rootTokenPlain?: string, rootEpoch?: number): Promise<{ jobId?: string, error?: string }> {
    return this.withClockSnapshotAsync(async (now) => {
      const nowMs = now.getTime();
      let jobId = this.generateId();
      const idempotencyKey = `${kind}:${jobId}`;
      const deadlineAt = new Date(nowMs + deadlineMins * 60000).toISOString();
      let result: any = {};
      let verifiedSession: any = null;
      if (actor !== 'role:root' && this.env && rootTokenPlain && typeof rootTokenPlain === 'string' && rootTokenPlain.includes('hub_session=')) {
        try {
          verifiedSession = await getSession(this.env, new Request('http://localhost', { headers: { Cookie: rootTokenPlain } }));
        } catch {}
      }
      let tokenHash = (rootTokenPlain && !verifiedSession) ? await sha256(rootTokenPlain) : undefined;

      this.ctx.storage.transactionSync(() => {
        if (actor === 'role:root') {
          if (!tokenHash || rootEpoch === undefined) { result = { error: 'unauthorized' }; return; }
          const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
          if (!lease || lease.epoch !== rootEpoch || !lease.holder_job_id) { result = { error: 'unauthorized_or_stale' }; return; }
          if (isLeaseExpired(lease.expires_at, nowMs)) { result = { error: 'unauthorized_or_stale' }; return; }
          const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", lease.holder_job_id).toArray()[0] as any;
          if (!job || job.claim_token_hash !== tokenHash || job.status !== 'running' || !job.started_at || !job.first_action_at) {
            result = { error: 'unauthorized_or_stale' };
            return;
          }
          const cred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
            tokenHash, lease.holder_job_id
          ).toArray()[0] as any;
          if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
            result = { error: 'unauthorized_or_stale' };
            return;
          }
          const credExpiresMs = parseExpiryMs(cred.expires_at);
          if (credExpiresMs === null || credExpiresMs <= nowMs) {
            result = { error: 'unauthorized_or_stale' };
            return;
          }
        } else if (verifiedSession && verifiedSession.isAdmin) {
          // Authenticated via verified HMAC admin session cookie
        } else {
          if (!tokenHash || !actor || typeof actor !== 'string') {
            result = { error: 'unauthorized' };
            return;
          }
          const cred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ?",
            tokenHash
          ).toArray()[0] as any;
          if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
            result = { error: 'unauthorized' };
            return;
          }
          const credExpiresMs = parseExpiryMs(cred.expires_at);
          if (credExpiresMs === null || credExpiresMs <= nowMs) {
            result = { error: 'unauthorized' };
            return;
          }
          let scopes: any[] = [];
          try {
            scopes = typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : (Array.isArray(cred.scopes) ? cred.scopes : []);
          } catch {
            result = { error: 'unauthorized' };
            return;
          }
          if (!scopes.includes('jobs:create') && !scopes.includes('admin')) {
            result = { error: 'unauthorized' };
            return;
          }
          const credIdentity = cred.actor || cred.subject;
          const isSystemOrPrincipalCred = cred.actor === 'role:principal' || cred.actor === 'system' || (!cred.actor && (actor === 'role:principal' || actor === 'system'));
          if (!scopes.includes('admin') && credIdentity !== actor && !isSystemOrPrincipalCred) {
            result = { error: 'unauthorized' };
            return;
          }
        }

        this.sql.exec("INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          jobId, kind, idempotencyKey, JSON.stringify(payload), 'pending', now.toISOString(), deadlineAt, actor);

        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        this.tryOfferNext(job, now);
        result = { jobId };
      });

      this.scheduleAlarm();

      return result;
    });
  }

  private checkHostRootRunCapability(machine: any): boolean {
    if (!machine) return false;
    let hasRootCap = false;
    if (machine.capabilities) {
      try {
        const caps = typeof machine.capabilities === 'string' ? JSON.parse(machine.capabilities) : machine.capabilities;
        if (Array.isArray(caps) && (caps.includes('root-run') || caps.includes('root'))) hasRootCap = true;
        if (caps && Array.isArray(caps.jobs) && caps.jobs.includes('root-run')) hasRootCap = true;
        if (caps && (caps['root-run'] || caps['root'])) hasRootCap = true;
      } catch {}
    }
    if (machine.roles) {
      try {
        const roles = typeof machine.roles === 'string' ? JSON.parse(machine.roles) : machine.roles;
        if (Array.isArray(roles) && (roles.includes('root-run') || roles.includes('root'))) hasRootCap = true;
        if (typeof roles === 'string' && (roles === 'root' || roles === 'root-run')) hasRootCap = true;
      } catch {}
    }
    return hasRootCap;
  }

  evaluateRootIncumbent(_callerNowMs?: number): {
    lease: any;
    live_session: any;
    host: any;
    registered_root: any;
    current_root: any;
    incumbent_legacy: any;
    heartbeat_fresh: boolean;
    within_lease: boolean;
    within_hardstop: boolean;
    within_grace?: boolean;
    past_hardstop: boolean;
    past_hardstop_with_grace?: boolean;
    is_healthy_alive: boolean;
    confirmed_stale: boolean;
    transport_unknown: boolean;
    server_now: string;
    can_takeover: boolean;
    takeover_denial_error: string | null;
    takeover_denial_reason: string | null;
  } {
    // Real clock trusted capture; caller argument is inert per contract
    const now = this.now();
    const nowMs = now.getTime();
    const nowIso = now.toISOString();

    let leaseRow = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
    if (!leaseRow) {
      leaseRow = { name: 'root', epoch: 0, holder_job_id: null, holder_host_id: null, holder_machine_id: null, expires_at: null };
    }

    let liveSession: any = null;
    let holderJob: any = null;
    if (leaseRow.holder_job_id) {
      holderJob = this.sql.exec("SELECT * FROM jobs WHERE id = ?", leaseRow.holder_job_id).toArray()[0] as any;
      if (holderJob) {
        liveSession = {
          job_id: holderJob.id,
          status: holderJob.status,
          started_at: holderJob.started_at,
          deadline_at: holderJob.deadline_at,
          session_id: holderJob.native_session || holderJob.session_id || null,
          launcher_task_id: holderJob.launcher_task_id || null,
          evidence_digest: holderJob.evidence_digest || null,
          first_action_at: holderJob.first_action_at || null
        };
      }
    }

    // Query agents table for live appointed root, filtering out retired Win35 rows or any retired rows
    // Multiple registry rows must not hide the fresh appointed Root.
    let regAgent: any = null;
    try {
      const regAgents = this.sql.exec(
        `SELECT tag, project, role, machine_id, engine, session_id, workspace, state, health_state, detail, lasthealth, health_at, last_seen_at
         FROM agents
         WHERE (role = 'root' OR (COALESCE(project,'') = '' AND (tag = 'role:root' OR tag = 'root')))
           AND (state IS NULL OR state != 'retired')
           AND (health_state IS NULL OR health_state != 'retired')
           AND (COALESCE(project,'') = '' OR ${registryNameSql('project')})
           AND tag NOT LIKE '%win35%'
           AND tag NOT LIKE '%retired%'
         ORDER BY
           CASE
             WHEN LOWER(COALESCE(state, health_state, lasthealth, '')) IN ('healthy', 'active', 'working', 'idle') THEN 0
             ELSE 1
           END,
           CASE WHEN last_seen_at IS NOT NULL THEN 0 ELSE 1 END,
           last_seen_at DESC
         LIMIT 1`
      ).toArray() as any[];
      regAgent = regAgents[0] || null;
    } catch {}

    // Canonical agent state: unknown state or health_state takes precedence over other states
    const DEAD_STATES = new Set(['retired', 'dead', 'defunct', 'failed', 'stale', 'stopped', 'offline', 'terminal', 'broken', 'error', 'expired', 'fenced']);
    const ALIVE_STATES = new Set(['healthy', 'active', 'working', 'idle']);
    let canonicalAgentState: string | null = null;
    let isExplicitDeadOrStale = false;
    if (regAgent) {
      const rawState = (regAgent.state || '').toLowerCase();
      const rawHealthState = (regAgent.health_state || '').toLowerCase();
      const rawLastHealth = (regAgent.lasthealth || '').toLowerCase();
      isExplicitDeadOrStale = Boolean(
        (rawState && DEAD_STATES.has(rawState) && (!rawHealthState || DEAD_STATES.has(rawHealthState))) ||
        (!rawState && rawHealthState && DEAD_STATES.has(rawHealthState))
      );
      canonicalAgentState = (rawState || rawHealthState || rawLastHealth || 'unknown').toLowerCase();
      if (rawHealthState === 'unknown' || rawState === 'unknown') {
        canonicalAgentState = 'unknown';
        isExplicitDeadOrStale = false;
      }
      if (this.healthService?.getEffectiveHealth) {
        const eff = this.healthService.getEffectiveHealth(regAgent.tag);
        if (eff && eff.state) {
          const effState = eff.state.toLowerCase();
          if (effState === 'unknown' && isExplicitDeadOrStale && (eff.detail?.inherited_from_host === 'stale' || eff.detail?.inherited_from_host === 'unavailable')) {
            // Preserve explicit dead/stale agent observation when HealthService only reports inherited host staleness/unavailability
          } else {
            canonicalAgentState = effState;
            if (effState === 'unknown') {
              isExplicitDeadOrStale = false;
            }
          }
        }
      }
    }
    const isAgentStateHealthy = canonicalAgentState ? ALIVE_STATES.has(canonicalAgentState) : false;

    const hostId = leaseRow.holder_job_id
      ? (leaseRow.holder_host_id || leaseRow.holder_machine_id || (holderJob ? holderJob.holder_host_id : null))
      : (regAgent ? regAgent.machine_id : (leaseRow.holder_host_id || leaseRow.holder_machine_id));

    let hostRow: any = null;
    let machineCheckinFresh = false;
    let machineTransportKnown = false;

    if (hostId) {
      const machine = this.sql.exec("SELECT * FROM machines WHERE id = ?", hostId).toArray()[0] as any;
      if (machine) {
        let hb = null;
        try { hb = machine.last_heartbeat ? JSON.parse(machine.last_heartbeat) : null; } catch {}
        let checkinAgeS = null;
        if (machine.last_checkin_at) {
          const checkinMs = parseExpiryMs(machine.last_checkin_at);
          if (checkinMs !== null && (nowMs - checkinMs >= 0)) {
            machineTransportKnown = true;
            const ageMs = nowMs - checkinMs;
            checkinAgeS = Math.floor(ageMs / 1000);
            machineCheckinFresh = (ageMs >= 0 && ageMs <= 180000);
          }
        }
        hostRow = {
          id: machine.id,
          name: machine.name,
          hostname: machine.hostname,
          state: machine.state || 'healthy',
          last_checkin_at: machine.last_checkin_at,
          checkin_age_s: checkinAgeS,
          last_heartbeat: hb
        };
      }
    }

    let leaseHeartbeatFresh = false;
    let leaseTransportUnknown = false;

    if (leaseRow.holder_job_id) {
      if (machineTransportKnown) {
        leaseHeartbeatFresh = machineCheckinFresh;
        leaseTransportUnknown = false;
      } else {
        // Leased root but machine has no checkin: transport unknown
        leaseTransportUnknown = true;
        leaseHeartbeatFresh = false;
      }
    }

    let regHeartbeatFresh = false;
    let regTransportUnknown = false;

    if (regAgent) {
      const regHostId = regAgent.machine_id;
      const regMachine = regHostId ? this.sql.exec("SELECT * FROM machines WHERE id = ?", regHostId).toArray()[0] as any : null;
      let regMachineFresh = false;
      let regMachineKnown = false;
      if (regMachine && regMachine.last_checkin_at) {
        const regCheckinMs = parseExpiryMs(regMachine.last_checkin_at);
        if (regCheckinMs !== null && (nowMs - regCheckinMs >= 0)) {
          regMachineKnown = true;
          const regAgeMs = nowMs - regCheckinMs;
          regMachineFresh = (regAgeMs >= 0 && regAgeMs <= 180000);
        }
      }

      const regLastSeenMs = parseExpiryMs(regAgent.last_seen_at || regAgent.health_at);
      const regSeenFinite = regLastSeenMs !== null && (nowMs - regLastSeenMs >= 0);
      const regSeenFresh = regSeenFinite && (nowMs - regLastSeenMs <= 180000);

      if (canonicalAgentState === 'unknown' || (regAgent.health_state && regAgent.health_state.toLowerCase() === 'unknown') || (regAgent.state && regAgent.state.toLowerCase() === 'unknown')) {
        regTransportUnknown = true;
        regHeartbeatFresh = false;
      } else if (!regMachineKnown || !regSeenFinite) {
        regTransportUnknown = true;
        regHeartbeatFresh = false;
      } else if (isExplicitDeadOrStale && !regSeenFresh) {
        // Explicit dead/stale state AND finite stale last_seen_at on known machine -> confirmed stale observation
        regTransportUnknown = false;
        regHeartbeatFresh = false;
      } else if (isExplicitDeadOrStale && regSeenFresh) {
        // Contradicted observation (dead/stale state with fresh last_seen_at) -> fail closed as unknown
        regTransportUnknown = true;
        regHeartbeatFresh = false;
      } else if (!regMachineFresh) {
        // SPEC §9: Active/healthy agent inherits unknown when its host is stale
        regTransportUnknown = true;
        regHeartbeatFresh = false;
      } else if (regSeenFresh) {
        regHeartbeatFresh = true;
        regTransportUnknown = false;
      } else {
        regHeartbeatFresh = false;
        regTransportUnknown = false;
      }
    }

    let heartbeatFresh = false;
    let transportUnknown = false;

    if (leaseRow.holder_job_id && regAgent) {
      transportUnknown = leaseTransportUnknown || regTransportUnknown;
      heartbeatFresh = leaseHeartbeatFresh || regHeartbeatFresh;
    } else if (leaseRow.holder_job_id) {
      transportUnknown = leaseTransportUnknown;
      heartbeatFresh = leaseHeartbeatFresh;
    } else if (regAgent) {
      transportUnknown = regTransportUnknown;
      heartbeatFresh = regHeartbeatFresh;
    }

    if (!liveSession && regAgent) {
      liveSession = {
        role: regAgent.role,
        tag: regAgent.tag,
        session_id: regAgent.session_id || null,
        machine_id: regAgent.machine_id || null,
        health_state: canonicalAgentState,
        state: canonicalAgentState,
        last_seen_at: regAgent.last_seen_at || regAgent.health_at || null,
        is_external_custody: true
      };
    }

    const incumbentLegacy = {
      type: "retired_win35_root",
      status: "retired",
      read_only: true,
      notes: "Win35 root stood down; do not reactivate"
    };

    const withinLease = Boolean(
      leaseRow.holder_job_id &&
      leaseRow.expires_at &&
      !isLeaseExpired(leaseRow.expires_at, nowMs)
    );

    const GRACE_MS = 2 * 60000;
    const deadlineMs = holderJob?.deadline_at ? (parseExpiryMs(holderJob.deadline_at) ?? new Date(holderJob.deadline_at).getTime()) : null;
    const startMs = holderJob?.started_at ? (parseExpiryMs(holderJob.started_at) ?? new Date(holderJob.started_at).getTime()) : (holderJob?.created_at ? (parseExpiryMs(holderJob.created_at) ?? new Date(holderJob.created_at).getTime()) : null);

    const hardstopMs = (deadlineMs !== null && startMs !== null)
      ? Math.min(deadlineMs, startMs + 25 * 60000)
      : (deadlineMs !== null ? deadlineMs : (startMs !== null ? startMs + 25 * 60000 : null));
    const hardstopWithGraceMs = hardstopMs !== null ? hardstopMs + GRACE_MS : null;

    const withinHardstop = Boolean(
      hardstopMs !== null && nowMs < hardstopMs
    );

    const withinGrace = Boolean(
      hardstopMs !== null &&
      hardstopWithGraceMs !== null &&
      nowMs >= hardstopMs &&
      nowMs < hardstopWithGraceMs
    );

    const pastHardstop = Boolean(
      hardstopMs !== null && nowMs >= hardstopMs
    );

    const pastHardstopWithGrace = Boolean(
      hardstopWithGraceMs !== null && nowMs >= hardstopWithGraceMs
    );

    let isJobActive = Boolean(
      holderJob &&
      (holderJob.status === 'claimed' || holderJob.status === 'running')
    );
    if (holderJob && holderJob.status === 'claimed') {
      const acquiredMs = parseExpiryMs(leaseRow.acquired_at) ?? (parseExpiryMs(holderJob.created_at) ?? nowMs);
      if (nowMs - acquiredMs > 300_000) {
        isJobActive = false;
      }
    }

    const isExternalHealthyAlive = Boolean(
      regAgent &&
      regHeartbeatFresh &&
      isAgentStateHealthy
    );

    const isLeasedHealthyAlive = Boolean(
      (withinLease || withinGrace) &&
      leaseHeartbeatFresh &&
      isJobActive
    );

    const isHealthyAlive = Boolean(
      isLeasedHealthyAlive ||
      isExternalHealthyAlive
    );

    // Explicit confirmed stale policy:
    // Unknown transport is UNKNOWN, NOT proof of dead!
    // Stale heartbeat alone while lease is still active (withinLease && isJobActive && !pastHardstop) is NOT proof of Root death!
    let confirmedStale = false;
    if (transportUnknown || canonicalAgentState === 'unknown') {
      confirmedStale = false;
    } else if (isHealthyAlive) {
      confirmedStale = false;
    } else if (leaseRow.holder_job_id && withinLease && !pastHardstop) {
      if (!isJobActive) {
        confirmedStale = true;
      } else if (!heartbeatFresh) {
        transportUnknown = true;
        confirmedStale = false;
      }
    } else if (regAgent) {
      if (canonicalAgentState && DEAD_STATES.has(canonicalAgentState)) {
        confirmedStale = true;
      } else if (canonicalAgentState === 'unknown' || canonicalAgentState === 'unspecified') {
        confirmedStale = false;
      } else if (isAgentStateHealthy) {
        confirmedStale = !heartbeatFresh && !transportUnknown;
      } else {
        confirmedStale = !heartbeatFresh && !transportUnknown;
      }
    } else if (leaseRow.holder_job_id) {
      if (pastHardstopWithGrace || !withinLease) {
        confirmedStale = true;
      }
    }

    // Shared takeover safety policy
    let canTakeover = false;
    let takeoverDenialError: string | null = null;
    let takeoverDenialReason: string | null = null;

    if (transportUnknown || canonicalAgentState === 'unknown') {
      canTakeover = false;
      takeoverDenialError = 'transport_unknown';
      takeoverDenialReason = 'incumbent root or host checkin state is unknown; unknown transport is not proof of death';
    } else if (isHealthyAlive) {
      canTakeover = false;
      takeoverDenialError = 'incumbent_still_healthy';
      takeoverDenialReason = 'incumbent is healthy alive with fresh heartbeat and valid state';
    } else if (leaseRow.holder_job_id || regAgent) {
      if (confirmedStale) {
        canTakeover = true;
      } else {
        canTakeover = false;
        takeoverDenialError = 'incumbent_not_stale';
        takeoverDenialReason = 'incumbent exists and is not confirmed stale';
      }
    } else {
      // No incumbent exists
      canTakeover = true;
    }

    const currentGrokRoot = {
      role: "root",
      tag: regAgent?.tag || "root",
      name: "Cloudflare Root (Grok Bot)",
      appointed_at: "2026-10-11",
      status: "active_appointed_sole_root",
      active_lease_holder: leaseRow.holder_job_id || null,
      active_host: hostId || null,
      session_id: liveSession?.session_id || null,
      machine_id: hostId || null,
      heartbeat_fresh: heartbeatFresh
    };

    return {
      lease: leaseRow,
      live_session: liveSession,
      host: hostRow,
      registered_root: regAgent || null,
      current_root: currentGrokRoot,
      incumbent_legacy: incumbentLegacy,
      heartbeat_fresh: heartbeatFresh,
      within_lease: withinLease,
      within_hardstop: withinHardstop,
      within_grace: withinGrace,
      past_hardstop: pastHardstop,
      past_hardstop_with_grace: pastHardstopWithGrace,
      is_healthy_alive: isHealthyAlive,
      confirmed_stale: confirmedStale,
      transport_unknown: transportUnknown,
      server_now: nowIso,
      can_takeover: canTakeover,
      takeover_denial_error: takeoverDenialError,
      takeover_denial_reason: takeoverDenialReason
    };
  }

  getRootStatus(): {
    lease: any;
    live_session: any;
    host: any;
    registered_root: any;
    current_root: any;
    incumbent_legacy: any;
    heartbeat_fresh: boolean;
    within_lease: boolean;
    within_hardstop: boolean;
    within_grace?: boolean;
    past_hardstop: boolean;
    past_hardstop_with_grace?: boolean;
    is_healthy_alive: boolean;
    confirmed_stale: boolean;
    transport_unknown: boolean;
    server_now: string;
  } {
    const evalResult = this.evaluateRootIncumbent();
    return {
      lease: evalResult.lease,
      live_session: evalResult.live_session,
      host: evalResult.host,
      registered_root: evalResult.registered_root,
      current_root: evalResult.current_root,
      incumbent_legacy: evalResult.incumbent_legacy,
      heartbeat_fresh: evalResult.heartbeat_fresh,
      within_lease: evalResult.within_lease,
      within_hardstop: evalResult.within_hardstop,
      within_grace: evalResult.within_grace,
      past_hardstop: evalResult.past_hardstop,
      past_hardstop_with_grace: evalResult.past_hardstop_with_grace,
      is_healthy_alive: evalResult.is_healthy_alive,
      confirmed_stale: evalResult.confirmed_stale,
      transport_unknown: evalResult.transport_unknown,
      server_now: evalResult.server_now
    };
  }

  async takeoverRoot(
    candidateJobId: string,
    hostId: string,
    expectedEpoch: number,
    reason: string = 'stale_or_hardstop'
  ): Promise<{
    success?: boolean;
    error?: string;
    reason?: string;
    job_token?: string;
    lease_epoch?: number;
    deadline_at?: string;
    current_epoch?: number;
  }> {
    return this.withClockSnapshotAsync(async (now) => {
      const nowIso = now.toISOString();
      const nowMs = now.getTime();

      const token = this.generateToken();
      const tokenHash = await sha256(token);
      let result: any = null;

      this.ctx.storage.transactionSync(() => {
        // Root scheduling flag: takeover acquires the root lease for a new root-run
        // claim and is refused while disabled.
        if (!isRootSchedulingEnabled(this.sql)) {
          result = { error: 'root_scheduling_disabled', reason: 'root scheduling is disabled; takeover refused' };
          return;
        }
        // 1. Fetch current lease
        const currentLease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
        if (!currentLease) {
          result = { error: 'lease_not_found', reason: 'root lease does not exist' };
          return;
        }

        // 2. Atomic CAS check on expected incumbent epoch
        if (currentLease.epoch !== expectedEpoch) {
          result = {
            error: 'cas_failed',
            reason: `expected epoch ${expectedEpoch} but current lease epoch is ${currentLease.epoch}`,
            current_epoch: currentLease.epoch
          };
          return;
        }

        // 3. Verify takeover policy via shared incumbent evaluator
        const evaluation = this.evaluateRootIncumbent();
        if (!evaluation.can_takeover) {
          result = {
            error: evaluation.takeover_denial_error,
            reason: evaluation.takeover_denial_reason,
            current_epoch: currentLease.epoch
          };
          return;
        }

        // 4. Validate candidate job
        const candidateJob = this.sql.exec("SELECT * FROM jobs WHERE id = ?", candidateJobId).toArray()[0] as any;
        if (!candidateJob) {
          result = { error: 'not_found', reason: `candidate job ${candidateJobId} not found` };
          return;
        }

        if (candidateJob.kind !== 'root-run') {
          result = { error: 'invalid_candidate_kind', reason: `candidate job must be kind root-run, got ${candidateJob.kind}` };
          return;
        }

        const validCandidateStatuses = ['offered', 'pending'];
        if (!validCandidateStatuses.includes(candidateJob.status)) {
          result = { error: 'invalid_candidate_status', reason: `candidate job status is ${candidateJob.status}; must be offered or pending` };
          return;
        }

        if (candidateJob.deadline_at && nowMs >= new Date(candidateJob.deadline_at).getTime()) {
          result = { error: 'candidate_expired', reason: 'candidate job deadline expired' };
          return;
        }

        if (candidateJob.status === 'offered') {
          const expectedHost = candidateJob.offer_host_id || candidateJob.offer_machine_id;
          if (expectedHost && expectedHost !== hostId) {
            result = { error: 'host_mismatch', reason: `candidate job offered to ${expectedHost}, not ${hostId}` };
            return;
          }
          if (candidateJob.offer_expires_at) {
            const offerExp = parseExpiryMs(candidateJob.offer_expires_at);
            if (offerExp !== null && offerExp <= nowMs) {
              result = { error: 'offer_expired', reason: 'candidate job offer expired' };
              return;
            }
          }
        }

        // 5. Validate candidate host eligibility
        const candidateMachine = this.sql.exec("SELECT * FROM machines WHERE id = ?", hostId).toArray()[0] as any;
        if (!candidateMachine) {
          result = { error: 'host_not_found', reason: `candidate host ${hostId} not registered in machines` };
          return;
        }
        if (candidateMachine.drained === 1 || candidateMachine.state === 'drained') {
          result = { error: 'host_drained', reason: `candidate host ${hostId} is drained` };
          return;
        }
        if (!this.checkHostRootRunCapability(candidateMachine)) {
          result = { error: 'host_incapable', reason: `candidate host ${hostId} lacks root-run capability` };
          return;
        }

        // Check host checkin freshness (<= 180s && >= 0)
        if (!candidateMachine.last_checkin_at) {
          result = { error: 'host_stale_or_offline', reason: `candidate host ${hostId} has no checkin` };
          return;
        }
        const checkinAgeMs = nowMs - new Date(candidateMachine.last_checkin_at).getTime();
        if (checkinAgeMs < 0 || checkinAgeMs > 180000) {
          result = { error: 'host_stale', reason: `candidate host ${hostId} checkin is stale (${Math.floor(checkinAgeMs / 1000)}s old)` };
          return;
        }

        // Check host gate launch_allowed
        let gateAllowed = false;
        if (candidateMachine.last_heartbeat) {
          try {
            const hb = typeof candidateMachine.last_heartbeat === 'string'
              ? JSON.parse(candidateMachine.last_heartbeat)
              : candidateMachine.last_heartbeat;
            if (hb && hb.gate && hb.gate.launch_allowed === true) {
              gateAllowed = true;
            }
          } catch {}
        }
        if (!gateAllowed) {
          result = { error: 'gate_launch_disallowed', reason: `candidate host ${hostId} gate.launch_allowed is not true` };
          return;
        }

        // Check rootHostsPolicy
        if (this.rootHostsPolicy && this.rootHostsPolicy.length > 0) {
          if (!this.rootHostsPolicy.includes(candidateMachine.hostname) &&
              !this.rootHostsPolicy.includes(candidateMachine.id) &&
              !this.rootHostsPolicy.includes(candidateMachine.name)) {
            result = { error: 'host_not_allowed', reason: `candidate host ${hostId} not permitted by rootHosts policy` };
            return;
          }
        }

        const deadlineMs = candidateJob.deadline_at ? (parseExpiryMs(candidateJob.deadline_at) ?? new Date(candidateJob.deadline_at).getTime()) : (nowMs + 25 * 60000);
        const newEpoch = currentLease.epoch + 1;
        const leaseExpiresMs = Math.min(nowMs + 20 * 60000, deadlineMs);
        const leaseExpiresIso = new Date(leaseExpiresMs).toISOString();

        // 6. Strong atomic CAS update against expectedEpoch
        const updateLease = this.sql.exec(
          "UPDATE leases SET holder_job_id = ?, holder_host_id = ?, holder_machine_id = ?, holder_actor = 'role:root', epoch = epoch + 1, acquired_at = ?, renewed_at = ?, expires_at = ?, release_reason = NULL, released_at = NULL WHERE name = 'root' AND epoch = ?",
          candidateJobId, hostId, hostId, nowIso, nowIso, leaseExpiresIso, expectedEpoch
        );

        if (updateLease.rowsWritten !== 1) {
          result = {
            error: 'cas_failed',
            reason: 'concurrent update race; epoch changed',
            current_epoch: currentLease.epoch
          };
          return;
        }

        // 7. Fence old incumbent job if one was held
        const oldJobId = currentLease.holder_job_id;
        if (oldJobId && oldJobId !== candidateJobId) {
          this.sql.exec(
            "UPDATE jobs SET status = 'fenced', finished_at = ?, outcome_summary = ? WHERE id = ? AND status IN ('claimed', 'running', 'offered')",
            nowIso, `fenced: superseded by takeover epoch ${newEpoch} on host ${hostId}`, oldJobId
          );
          this.sql.exec(
            "UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL",
            nowIso, oldJobId
          );
          this.addHealthEvent('job', oldJobId, 'fenced', `superseded by takeover epoch ${newEpoch}`, nowIso);
        }

        // 8. Update candidate job to claimed with new epoch and token
        this.sql.exec(
          "UPDATE jobs SET status = 'claimed', claim_token_hash = ?, holder_host_id = ?, holder_machine_id = ?, lease_epoch = ? WHERE id = ?",
          tokenHash, hostId, hostId, newEpoch, candidateJobId
        );

        // 9. Mint new credential for new root job
        const credId = this.generateId();
        const jobScopes = ['lease:root', 'jobs:create', 'bus:send', 'bus:read', 'health:report'];
        this.sql.exec(
          "INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'job', ?, ?, ?, ?, 'role:root', ?, 'system', ?, ?)",
          credId, tokenHash, credId, candidateJobId, hostId, JSON.stringify(jobScopes), nowIso, leaseExpiresIso
        );

        // 10. Audit event: takeover
        this.logAuditEvent('host:' + hostId, 'takeover', 'leases/root', candidateJobId, {
          old_job_id: oldJobId || null,
          old_epoch: expectedEpoch,
          new_job_id: candidateJobId,
          new_epoch: newEpoch,
          host_id: hostId,
          reason: safeUtf8Summary(reason, 1024)
        }, nowIso);

        this.addHealthEvent('job', candidateJobId, 'takeover', `atomic CAS takeover succeeded: epoch ${newEpoch}`, nowIso);

        result = {
          success: true,
          job_token: token,
          lease_epoch: newEpoch,
          deadline_at: candidateJob.deadline_at || new Date(deadlineMs).toISOString()
        };
      });

      this.scheduleAlarm();

      return result;
    });
  }

  async verifyRootEpoch(
    jobId: string | null,
    jobTokenPlain: string | null,
    epoch: number,
    isHostSupervisor: boolean = false,
    hostId?: string | null
  ): Promise<{ valid: boolean; status?: string; reason?: string; current_epoch?: number; holder_job_id?: string | null }> {
    return this.withClockSnapshotAsync(async (now) => {
      const tokenHash = (jobTokenPlain && typeof jobTokenPlain === 'string') ? await sha256(jobTokenPlain) : null;
      const nowMs = now.getTime();
      const nowIso = now.toISOString();
      let result: { valid: boolean; status?: string; reason?: string; current_epoch?: number; holder_job_id?: string | null } = { valid: false };

      this.ctx.storage.transactionSync(() => {
        const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
        if (!lease) {
          result = { valid: false, status: 'error', reason: 'no_root_lease' };
          return;
        }

        // Fence check first
        if (lease.epoch !== epoch) {
          this.logAuditEvent('role:root', 'fenced', 'leases/root', jobId || lease.holder_job_id || 'root', {
            job_id: jobId || lease.holder_job_id || null,
            attempted_epoch: epoch,
            current_epoch: lease.epoch,
            current_holder: lease.holder_job_id
          }, nowIso);
          result = {
            valid: false,
            status: 'fenced',
            reason: 'superseded',
            current_epoch: lease.epoch,
            holder_job_id: lease.holder_job_id
          };
          return;
        }

        if (isLeaseExpired(lease.expires_at, nowMs)) {
          result = { valid: false, status: 'expired', reason: 'lease_expired', current_epoch: lease.epoch };
          return;
        }

        const targetJobId = jobId || lease.holder_job_id;
        if (!targetJobId || lease.holder_job_id !== targetJobId) {
          result = { valid: false, status: 'unauthorized', reason: 'job_mismatch', current_epoch: lease.epoch };
          return;
        }

        const holderHost = lease.holder_host_id || lease.holder_machine_id;
        if (hostId && holderHost && holderHost !== hostId) {
          result = { valid: false, status: 'unauthorized', reason: 'host_mismatch', current_epoch: lease.epoch };
          return;
        }

        if (!tokenHash) {
          result = { valid: false, status: 'unauthorized', reason: 'token_required', current_epoch: lease.epoch };
          return;
        }

        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", targetJobId).toArray()[0] as any;
        if (!job) {
          result = { valid: false, status: 'unauthorized', reason: 'job_not_found', current_epoch: lease.epoch };
          return;
        }

        let tokenAuthorized = false;
        if (job.claim_token_hash && job.claim_token_hash === tokenHash) {
          if (job.status !== 'running' || !job.started_at || !job.first_action_at || !job.native_session || !job.evidence_digest) {
            result = { valid: false, status: 'unauthorized', reason: 'job_not_running_or_unstarted', current_epoch: lease.epoch };
            return;
          }
          const cred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
            tokenHash, targetJobId
          ).toArray()[0] as any;
          if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
            result = { valid: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
            return;
          }
          const credExpiresMs = parseExpiryMs(cred.expires_at);
          if (credExpiresMs === null || credExpiresMs <= nowMs) {
            result = { valid: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
            return;
          }
          tokenAuthorized = true;
        } else if (isHostSupervisor) {
          const callerCred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ?",
            tokenHash
          ).toArray()[0] as any;
          if (!callerCred || (callerCred.revoked_at && callerCred.revoked_at.trim().length > 0)) {
            result = { valid: false, status: 'unauthorized', reason: 'unauthorized_token', current_epoch: lease.epoch };
            return;
          }
          const callerExpMs = parseExpiryMs(callerCred.expires_at);
          if (callerExpMs === null || callerExpMs <= nowMs) {
            result = { valid: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
            return;
          }
          let scopes: string[] = [];
          try {
            scopes = typeof callerCred.scopes === 'string' ? JSON.parse(callerCred.scopes) : (Array.isArray(callerCred.scopes) ? callerCred.scopes : []);
          } catch {}
          const isValidHostCred = callerCred.kind === 'host' && scopes.includes('host') && callerCred.machine_id && callerCred.machine_id === holderHost && (!hostId || holderHost === hostId);
          const isValidAdminCred = scopes.includes('admin');
          if (!isValidHostCred && !isValidAdminCred) {
            result = { valid: false, status: 'unauthorized', reason: 'unauthorized_token', current_epoch: lease.epoch };
            return;
          }
          if (job.status !== 'running' || (job.claim_token_hash && (!job.started_at || !job.first_action_at || !job.native_session || !job.evidence_digest))) {
            result = { valid: false, status: 'unauthorized', reason: 'job_not_running_or_unstarted', current_epoch: lease.epoch };
            return;
          }
          if (job.claim_token_hash) {
            const jobCred = this.sql.exec(
              "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
              job.claim_token_hash, targetJobId
            ).toArray()[0] as any;
            if (!jobCred || (jobCred.revoked_at && jobCred.revoked_at.trim().length > 0)) {
              result = { valid: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
              return;
            }
            const jobCredExpMs = parseExpiryMs(jobCred.expires_at);
            if (jobCredExpMs === null || jobCredExpMs <= nowMs) {
              result = { valid: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
              return;
            }
          }
          tokenAuthorized = true;
        }

        if (!tokenAuthorized) {
          result = { valid: false, status: 'unauthorized', reason: 'unauthorized_token', current_epoch: lease.epoch };
          return;
        }

        const rootGraceMs = (job.kind === 'root-run') ? 120000 : 0;
        const hardstopWithGraceMs = job.deadline_at ? (parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime()) + rootGraceMs : null;
        if (hardstopWithGraceMs !== null && nowMs >= hardstopWithGraceMs) {
          result = { valid: false, status: 'expired', reason: 'past_hardstop', current_epoch: lease.epoch };
          return;
        }

        result = { valid: true, status: 'ok', current_epoch: lease.epoch, holder_job_id: lease.holder_job_id };
      });

      return result;
    });
  }

  async commitRootAction(
    jobId: string,
    hostId: string,
    epoch: number,
    action: { type: string; name?: string; payload?: any; idempotency_key?: string },
    sessionId?: string,
    jobTokenPlain?: string | null
  ): Promise<{
    success: boolean;
    status: string;
    committed?: boolean;
    reason?: string;
    current_epoch?: number;
    action_id?: number;
    result?: any;
    duplicate?: boolean;
  }> {
    return this.withClockSnapshotAsync(async (now) => {
      const nowMs = now.getTime();
      const nowIso = now.toISOString();

      const hashedToken = (jobTokenPlain && typeof jobTokenPlain === 'string') ? await sha256(jobTokenPlain) : null;

      const actionName = action?.name || `${action?.type || 'unknown'}:${action?.idempotency_key || 'default'}`;
      const idempotencyKey = action?.idempotency_key || actionName;
      const payloadDigest = await sha256(JSON.stringify(action?.payload || {}));

      let result: {
        success: boolean;
        status: string;
        committed?: boolean;
        reason?: string;
        current_epoch?: number;
        action_id?: number;
        result?: any;
        duplicate?: boolean;
      } = {
        success: false,
        status: 'error'
      };

      this.ctx.storage.transactionSync(() => {
        const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
        if (!lease) {
          result = { success: false, status: 'error', reason: 'no_root_lease' };
          return;
        }

        // 1. Fence check FIRST before replay lookup
        if (lease.epoch !== epoch) {
          this.logAuditEvent('role:root', 'fenced', 'leases/root', jobId || 'root', {
            job_id: jobId,
            attempted_epoch: epoch,
            current_epoch: lease.epoch,
            current_holder: lease.holder_job_id,
            action: actionName,
            action_type: action?.type,
            reason: 'superseded'
          }, nowIso);
          result = {
            success: false,
            status: 'fenced',
            reason: 'superseded',
            current_epoch: lease.epoch
          };
          return;
        }

        // 2. Lease expiry check
        if (isLeaseExpired(lease.expires_at, nowMs)) {
          result = {
            success: false,
            status: 'expired',
            reason: 'lease_expired',
            current_epoch: lease.epoch
          };
          return;
        }

        // 3. Holder binding check: caller host and job must match lease holder
        const holderHost = lease.holder_host_id || lease.holder_machine_id;
        if (lease.holder_job_id !== jobId || (holderHost && holderHost !== hostId)) {
          result = {
            success: false,
            status: 'unauthorized',
            reason: 'holder_binding_mismatch',
            current_epoch: lease.epoch
          };
          return;
        }

        // 4. Job record validation
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job) {
          result = { success: false, status: 'unauthorized', reason: 'job_not_found', current_epoch: lease.epoch };
          return;
        }
        if (job.status !== 'running') {
          result = { success: false, status: 'unauthorized', reason: `job_not_running (status: ${job.status})`, current_epoch: lease.epoch };
          return;
        }
        const rootGraceMs = (job.kind === 'root-run') ? 120000 : 0;
        const hardstopWithGraceMs = job.deadline_at ? (parseExpiryMs(job.deadline_at) ?? new Date(job.deadline_at).getTime()) + rootGraceMs : null;
        if (hardstopWithGraceMs !== null && nowMs >= hardstopWithGraceMs) {
          result = { success: false, status: 'expired', reason: 'past_hardstop', current_epoch: lease.epoch };
          return;
        }
        const jobHost = job.holder_host_id || job.holder_machine_id;
        if (jobHost && jobHost !== hostId) {
          result = { success: false, status: 'unauthorized', reason: 'host_mismatch', current_epoch: lease.epoch };
          return;
        }
        if (!jobTokenPlain || typeof jobTokenPlain !== 'string' || !hashedToken) {
          result = { success: false, status: 'unauthorized', reason: 'token_required', current_epoch: lease.epoch };
          return;
        }

        // Authenticate plaintext token hash against job credential or host/admin supervisor credential (NEVER raw stored hash)
        let credentialAuthorized = false;
        if (job.claim_token_hash && job.claim_token_hash === hashedToken) {
          const cred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
            hashedToken, jobId
          ).toArray()[0] as any;
          if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
            result = { success: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
            return;
          }
          const credExpiresMs = parseExpiryMs(cred.expires_at);
          if (credExpiresMs === null || credExpiresMs <= nowMs) {
            result = { success: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
            return;
          }
          credentialAuthorized = true;
        } else {
          const callerCred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ?",
            hashedToken
          ).toArray()[0] as any;
          if (callerCred) {
            let scopes: string[] = [];
            try {
              scopes = typeof callerCred.scopes === 'string' ? JSON.parse(callerCred.scopes) : (Array.isArray(callerCred.scopes) ? callerCred.scopes : []);
            } catch {}
            const expectedHost = holderHost || jobHost;
            const isValidHostSupervisor = callerCred.kind === 'host' && scopes.includes('host') && callerCred.machine_id && callerCred.machine_id === expectedHost && expectedHost === hostId;
            const isValidAdminSupervisor = scopes.includes('admin');
            if (isValidHostSupervisor || isValidAdminSupervisor) {
              if (callerCred.revoked_at && callerCred.revoked_at.trim().length > 0) {
                result = { success: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
                return;
              }
              const callerExpMs = parseExpiryMs(callerCred.expires_at);
              if (callerExpMs === null || callerExpMs <= nowMs) {
                result = { success: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
                return;
              }
              if (job.claim_token_hash) {
                const jobCred = this.sql.exec(
                  "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
                  job.claim_token_hash, jobId
                ).toArray()[0] as any;
                if (!jobCred || (jobCred.revoked_at && jobCred.revoked_at.trim().length > 0)) {
                  result = { success: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
                  return;
                }
                const jobCredExpMs = parseExpiryMs(jobCred.expires_at);
                if (jobCredExpMs === null || jobCredExpMs <= nowMs) {
                  result = { success: false, status: 'unauthorized', reason: 'credential_expired', current_epoch: lease.epoch };
                  return;
                }
              }
              credentialAuthorized = true;
            }
          }
        }

        if (!credentialAuthorized) {
          result = { success: false, status: 'unauthorized', reason: 'token_mismatch', current_epoch: lease.epoch };
          return;
        }

        if (!sessionId || !job.native_session || sessionId !== job.native_session) {
          result = { success: false, status: 'unauthorized', reason: 'session_mismatch', current_epoch: lease.epoch };
          return;
        }

        // 5. Action type allowlist check: useful actions commit transactionally; unimplemented explicit deny
        const allowedActionTypes = ['bus_dispatch', 'job_transition'];
        if (!action || !action.type || !allowedActionTypes.includes(action.type)) {
          result = {
            success: false,
            status: 'unsupported_type',
            reason: `unsupported action type: ${action?.type}`,
            current_epoch: lease.epoch
          };
          return;
        }

        // 6. Idempotent replay and payload digest conflict check
        const existing = this.sql.exec(
          "SELECT * FROM root_actions WHERE epoch = ? AND idempotency_key = ?",
          epoch, idempotencyKey
        ).toArray()[0] as any;

        if (existing) {
          if (existing.payload_digest !== payloadDigest) {
            result = {
              success: false,
              status: 'conflict',
              reason: 'payload_digest_conflict',
              current_epoch: lease.epoch
            };
            return;
          }
          let parsedResult: any = null;
          try { parsedResult = JSON.parse(existing.result); } catch {}
          result = {
            success: true,
            status: 'committed',
            committed: true,
            current_epoch: epoch,
            action_id: existing.id,
            result: parsedResult,
            duplicate: true
          };
          return;
        }

        // 7. Execute actual useful business mutation at the epoch fence
        let effectResult: any = {};
        if (action.type === 'bus_dispatch') {
          const p = action.payload || {};
          let toActor = p.to_actor;
          if (!toActor && typeof p.to === 'string' && p.to) {
            const mRow = this.sql.exec("SELECT 1 FROM machines WHERE id = ?", p.to).toArray()[0];
            toActor = mRow ? `machine:${p.to}` : p.to;
          }
          const msgKind = p.kind || (p.type === 'directive' ? 'note' : p.type);
          if (!toActor || typeof toActor !== 'string' || !msgKind || typeof msgKind !== 'string' || p.body === undefined) {
            result = { success: false, status: 'invalid_payload', reason: 'missing to_actor, kind, or body in bus_dispatch', current_epoch: epoch };
            return;
          }

          const allowedKinds = new Set(['note', 'handoff', 'reply', 'status', 'claim', 'release']);
          if (!allowedKinds.has(msgKind)) {
            result = { success: false, status: 'invalid_payload', reason: `invalid message kind: ${msgKind}`, current_epoch: epoch };
            return;
          }

          if (toActor === 'role:root' || toActor.startsWith('role:root')) {
            result = { success: false, status: 'invalid_payload', reason: 'cannot send message to role:root', current_epoch: epoch };
            return;
          }

          // Reject retired agents
          let isRetired = false;
          if (toActor.startsWith('role:')) {
            const role = toActor.substring(5);
            const res = this.sql.exec("SELECT 1 FROM agents WHERE role = ? AND (state = 'retired' OR health_state = 'retired')", role).toArray();
            if (res.length > 0) isRetired = true;
          } else if (toActor.includes(':')) {
            const [proj, tag] = toActor.split(':');
            const res = this.sql.exec("SELECT 1 FROM agents WHERE project = ? AND tag = ? AND (state = 'retired' OR health_state = 'retired')", proj, tag).toArray();
            if (res.length > 0) isRetired = true;
          } else {
            const res = this.sql.exec("SELECT 1 FROM agents WHERE tag = ? AND (state = 'retired' OR health_state = 'retired')", toActor).toArray();
            if (res.length > 0) isRetired = true;
          }
          if (isRetired) {
            result = { success: false, status: 'invalid_payload', reason: `recipient is retired: ${toActor}`, current_epoch: epoch };
            return;
          }

          let validRecipient = false;
          if (toActor === 'role:principal' || toActor === 'principal') {
            const activePrincipal = this.sql.exec(
              "SELECT 1 FROM agents WHERE (role = 'principal' OR tag = 'principal') AND COALESCE(state, 'active') != 'retired' AND COALESCE(health_state, 'active') != 'retired'"
            ).toArray();
            validRecipient = activePrincipal.length > 0;
          } else if (toActor.startsWith('role:')) {
            const role = toActor.substring(5);
            const res = this.sql.exec(
              "SELECT 1 FROM agents WHERE role = ? AND COALESCE(state, 'active') != 'retired' AND COALESCE(health_state, 'active') != 'retired'",
              role
            ).toArray();
            validRecipient = res.length > 0;
          } else if (toActor.startsWith('machine:')) {
            const mId = toActor.substring(8);
            const mRow = this.sql.exec("SELECT * FROM machines WHERE id = ? AND COALESCE(drained, 0) = 0", mId).toArray()[0] as any;
            validRecipient = Boolean(mRow && (!mRow.state || mRow.state !== 'drained'));
          } else if (toActor.includes(':')) {
            const [project, tag] = toActor.split(':');
            const res = this.sql.exec(
              "SELECT 1 FROM agents WHERE project = ? AND tag = ? AND COALESCE(state, 'active') != 'retired' AND COALESCE(health_state, 'active') != 'retired'",
              project, tag
            ).toArray();
            validRecipient = res.length > 0;
          } else {
            const agents = this.sql.exec(
              "SELECT project FROM agents WHERE tag = ? AND COALESCE(state, 'active') != 'retired' AND COALESCE(health_state, 'active') != 'retired'",
              toActor
            ).toArray();
            if (agents.length > 0) {
              const projects = new Set(agents.map((a: any) => a.project));
              if (projects.size === 1) validRecipient = true;
            }
          }

          if (!validRecipient) {
            result = { success: false, status: 'invalid_payload', reason: `unknown recipient: ${toActor}`, current_epoch: epoch };
            return;
          }

          if (p.reply_to) {
            const replyMsg = this.sql.exec('SELECT id, thread_id, from_actor, to_actor FROM messages WHERE id = ?', p.reply_to).toArray()[0] as any;
            if (!replyMsg) {
              result = { success: false, status: 'invalid_payload', reason: `reply_to message ${p.reply_to} not found`, current_epoch: epoch };
              return;
            }
            const recipient = this.busService?.canonicalActor(toActor) ?? toActor;
            if (replyMsg.from_actor !== recipient && replyMsg.to_actor !== recipient) {
              result = { success: false, status: 'invalid_payload', reason: 'reply_to recipient mismatch', current_epoch: epoch };
              return;
            }
            if (replyMsg.from_actor !== 'root' && replyMsg.from_actor !== 'role:root' && replyMsg.to_actor !== 'root' && replyMsg.to_actor !== 'role:root') {
              result = { success: false, status: 'invalid_payload', reason: 'reply_to sender mismatch', current_epoch: epoch };
              return;
            }
          }

          const bodyStr = typeof p.body === 'string' ? p.body : JSON.stringify(p.body);
          if (new TextEncoder().encode(bodyStr).length > 16384) {
            result = { success: false, status: 'invalid_payload', reason: 'body_too_large', current_epoch: epoch };
            return;
          }

          let sentMsgId: string;
          let sentThreadId: string;
          if (this.busService && typeof this.busService.send === 'function') {
            try {
              let sent: any;
              try {
                sent = this.busService.send({
                  kind: 'operator',
                  actor: 'role:root',
                  isAdmin: true
                }, {
                  senderActor: 'role:root',
                  toActor,
                  kind: msgKind,
                  body: bodyStr,
                  idempotencyKey: `bus:${epoch}:${idempotencyKey}`,
                  leaseEpoch: epoch,
                  threadId: p.thread_id,
                  replyTo: p.reply_to || undefined,
                  now: nowMs
                });
              } catch (innerErr: any) {
                if (String(innerErr?.message || innerErr).includes('missing_m4_root_authority')) {
                  sent = this.busService.send({
                    kind: 'operator',
                    actor: 'root',
                    isAdmin: true
                  }, {
                    senderActor: 'root',
                    toActor,
                    kind: msgKind,
                    body: bodyStr,
                    idempotencyKey: `bus:${epoch}:${idempotencyKey}`,
                    leaseEpoch: epoch,
                    threadId: p.thread_id,
                    replyTo: p.reply_to || undefined,
                    now: nowMs
                  });
                } else {
                  throw innerErr;
                }
              }
              sentMsgId = sent.id;
              sentThreadId = sent.threadId || sent.thread_id || sent.id;
            } catch (busErr: any) {
              result = { success: false, status: 'invalid_payload', reason: busErr?.message || String(busErr), current_epoch: epoch };
              return;
            }
          } else {
            const msgId = generateUlid(nowMs, this.getRandomValues);
            let threadId = p.thread_id || msgId;
            if (p.reply_to) {
              try {
                const msg = this.sql.exec('SELECT thread_id FROM messages WHERE id = ?', p.reply_to).toArray()[0] as any;
                if (msg?.thread_id) threadId = msg.thread_id;
              } catch {}
            }
            const expiresAt = nowMs + 30 * 24 * 3600 * 1000;
            this.sql.exec(`
              INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, reply_to, body, idempotency_key, lease_epoch, created_at, expires_at)
              VALUES (?, 'role:root', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            `, msgId, toActor, msgKind, threadId, p.reply_to || null, bodyStr, `bus:${epoch}:${idempotencyKey}`, epoch, nowMs, expiresAt);
            sentMsgId = msgId;
            sentThreadId = threadId;
          }
          effectResult = { message_id: sentMsgId, thread_id: sentThreadId };
        } else if (action.type === 'job_transition') {
          const p = action.payload || {};
          if (!p.target_job_id || typeof p.target_job_id !== 'string' || !p.new_status || typeof p.new_status !== 'string') {
            result = { success: false, status: 'invalid_payload', reason: 'missing target_job_id or new_status in job_transition', current_epoch: epoch };
            return;
          }
          const validStatuses = ['succeeded', 'failed', 'cancelled'];
          if (!validStatuses.includes(p.new_status)) {
            result = { success: false, status: 'invalid_payload', reason: `invalid target status: ${p.new_status}`, current_epoch: epoch };
            return;
          }
          const targetJob = this.sql.exec("SELECT * FROM jobs WHERE id = ?", p.target_job_id).toArray()[0] as any;
          if (!targetJob) {
            result = { success: false, status: 'invalid_payload', reason: `target job ${p.target_job_id} not found`, current_epoch: epoch };
            return;
          }
          const terminalStatuses = ['succeeded', 'failed', 'cancelled', 'expired', 'fenced'];
          if (terminalStatuses.includes(targetJob.status)) {
            result = { success: false, status: 'invalid_payload', reason: `target job is already terminal (${targetJob.status})`, current_epoch: epoch };
            return;
          }
          const summary = safeUtf8Summary(p.outcome_summary || '', 4096);
          const evidenceJson = JSON.stringify(p.outcome_evidence || []);
          this.sql.exec(
            "UPDATE jobs SET status = ?, finished_at = ?, outcome_summary = ?, outcome_evidence = ? WHERE id = ?",
            p.new_status, nowIso, summary, evidenceJson, p.target_job_id
          );
          // Release any lease held by target job
          this.sql.exec(
            "UPDATE leases SET holder_job_id = NULL, holder_host_id = NULL, holder_machine_id = NULL, expires_at = NULL, released_at = ?, release_reason = ? WHERE holder_job_id = ?",
            nowIso, p.new_status, p.target_job_id
          );
          // Revoke target job credentials
          this.sql.exec(
            "UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL",
            nowIso, p.target_job_id
          );
          this.addHealthEvent('job', p.target_job_id, p.new_status, `transitioned by root epoch ${epoch}`, nowIso);
          effectResult = { target_job_id: p.target_job_id, previous_status: targetJob.status, new_status: p.new_status };
        }

        // 8. Persist receipt into root_actions
        this.sql.exec(
          `INSERT INTO root_actions (job_id, host_id, session_id, epoch, action_name, action_type, idempotency_key, payload_digest, result, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
          jobId, hostId, sessionId || job.native_session || '', epoch, actionName, action.type, idempotencyKey, payloadDigest, JSON.stringify(effectResult), nowIso
        );
        const insertedRow = this.sql.exec(
          "SELECT id FROM root_actions WHERE epoch = ? AND idempotency_key = ?",
          epoch, idempotencyKey
        ).toArray()[0] as any;
        const actionId = insertedRow ? insertedRow.id : 1;

        // 9. Audit event: action
        this.logAuditEvent('role:root', 'action', 'leases/root', jobId, {
          epoch,
          session_id: sessionId || null,
          host_id: hostId,
          action: actionName,
          action_type: action.type,
          effect_result: effectResult
        }, nowIso);

        result = {
          success: true,
          status: 'committed',
          committed: true,
          current_epoch: epoch,
          action_id: actionId,
          result: effectResult
        };
      });

      return result;
    });
  }

  postHandoffNotes(
    jobId: string,
    hostId: string,
    epoch: number,
    summary: string,
    evidenceRefs: string[] = [],
    sessionId?: string,
    jobTokenPlain?: string | null
  ): { success: boolean; id?: number; duplicate?: boolean; error?: string } {
    return this.withClockSnapshot((now) => {
      const nowMs = now.getTime();
      const nowIso = now.toISOString();
      const safeSummary = safeUtf8Summary(summary || '', 4096);
      const cleanRefs = Array.isArray(evidenceRefs)
        ? evidenceRefs.slice(0, 50).map(r => safeUtf8Summary(String(r), 512))
        : [];
      const evidenceJson = JSON.stringify(cleanRefs);

      let res: any = { success: false };
      this.ctx.storage.transactionSync(() => {
        // 1. Authenticate caller host exists
        const host = this.sql.exec("SELECT * FROM machines WHERE id = ?", hostId).toArray()[0] as any;
        if (!host) {
          res = { success: false, error: 'host_not_found' };
          return;
        }

        // 2. Validate job exists
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!job) {
          res = { success: false, error: 'job_not_found' };
          return;
        }

        // 3. Host binding check
        const boundHost = job.holder_host_id || job.holder_machine_id || job.offer_host_id || job.offer_machine_id;
        if (boundHost && boundHost !== hostId) {
          res = { success: false, error: 'host_mismatch' };
          return;
        }

        // 4. Exact epoch check
        if (job.lease_epoch !== epoch) {
          res = { success: false, error: 'epoch_mismatch' };
          return;
        }

        // 5. Authenticate caller secret (holder job token or bound host/admin credential)
        if (!jobTokenPlain || typeof jobTokenPlain !== 'string' || jobTokenPlain.trim().length === 0) {
          res = { success: false, error: 'token_required' };
          return;
        }
        const tokenHash = sha256Sync(jobTokenPlain);
        let isAuthorizedByJobToken = false;
        let isAuthorizedBySupervisor = false;

        if (job.claim_token_hash && job.claim_token_hash === tokenHash) {
          if (job.status === 'running' || job.status === 'claimed') {
            const cred = this.sql.exec(
              "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
              tokenHash, jobId
            ).toArray()[0] as any;
            if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
              res = { success: false, error: 'revoked_token' };
              return;
            }
            const credExpMs = parseExpiryMs(cred.expires_at);
            if (credExpMs === null || credExpMs <= nowMs) {
              res = { success: false, error: 'credential_expired' };
              return;
            }
          }
          isAuthorizedByJobToken = true;
        } else {
          const callerCred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ?",
            tokenHash
          ).toArray()[0] as any;
          if (callerCred && (!callerCred.revoked_at || callerCred.revoked_at.trim().length === 0)) {
            const callerExpMs = parseExpiryMs(callerCred.expires_at);
            if (callerExpMs !== null && callerExpMs > nowMs) {
              let scopes: string[] = [];
              try {
                scopes = typeof callerCred.scopes === 'string' ? JSON.parse(callerCred.scopes) : (Array.isArray(callerCred.scopes) ? callerCred.scopes : []);
              } catch {}
              const expectedHost = boundHost || hostId;
              const isValidHostCred = callerCred.kind === 'host' && scopes.includes('host') && callerCred.machine_id && callerCred.machine_id === expectedHost && expectedHost === hostId;
              const isValidAdminCred = scopes.includes('admin');
              if (isValidHostCred || isValidAdminCred) {
                isAuthorizedBySupervisor = true;
              }
            }
          }
        }

        if (!isAuthorizedByJobToken && !isAuthorizedBySupervisor) {
          res = { success: false, error: 'unauthorized_token' };
          return;
        }

        // 6. Session binding check: job must be started with native_session and sessionId must match
        if (isAuthorizedByJobToken || job.native_session) {
          if (!job.native_session) {
            res = { success: false, error: 'job_not_started' };
            return;
          }
          if (!sessionId || sessionId !== job.native_session) {
            res = { success: false, error: 'session_mismatch' };
            return;
          }
        }

        // 7. Check existing handoff note for (job_id, epoch) -> prevent overwriting legitimate notes
        const existing = this.sql.exec(
          "SELECT id, host_id, session_id, summary, evidence_refs FROM handoff_notes WHERE lease_name = 'root' AND job_id = ? AND epoch = ?",
          jobId, epoch
        ).toArray()[0] as any;

        const effectiveSessionId = sessionId || job.native_session || null;

        if (existing) {
          const hostMatches = existing.host_id === hostId;
          const sessionMatches = existing.session_id === effectiveSessionId;
          if (hostMatches && sessionMatches) {
            this.sql.exec(
              "UPDATE handoff_notes SET summary = ?, evidence_refs = ?, created_at = ? WHERE id = ?",
              safeSummary, evidenceJson, nowIso, existing.id
            );
            res = { success: true, id: existing.id, duplicate: true };
            return;
          } else {
            res = { success: false, error: 'session_mismatch' };
            return;
          }
        }

        this.sql.exec(
          "INSERT INTO handoff_notes (lease_name, job_id, host_id, session_id, epoch, summary, evidence_refs, created_at) VALUES ('root', ?, ?, ?, ?, ?, ?, ?)",
          jobId, hostId, effectiveSessionId, epoch, safeSummary, evidenceJson, nowIso
        );
        const inserted = this.sql.exec(
          "SELECT id FROM handoff_notes WHERE lease_name = 'root' AND job_id = ? AND epoch = ?",
          jobId, epoch
        ).toArray()[0] as any;
        res = { success: true, id: inserted ? inserted.id : 1, duplicate: false };

        this.logAuditEvent('host:' + hostId, 'handoff', 'leases/root', jobId, {
          job_id: jobId,
          epoch,
          summary_len: safeSummary.length,
          refs_count: cleanRefs.length
        }, nowIso);
      });

      return res;
    });
  }

  getHandoffNotes(limit = 10): any[] {
    const lim = Math.max(1, Math.min(50, limit));
    const rows = this.sql.exec(
      "SELECT * FROM handoff_notes WHERE lease_name = 'root' ORDER BY id DESC LIMIT ?",
      lim
    ).toArray() as any[];

    return rows.map(r => {
      let refs: any[] = [];
      try { refs = r.evidence_refs ? JSON.parse(r.evidence_refs) : []; } catch {}
      return {
        id: r.id,
        lease_name: r.lease_name,
        job_id: r.job_id,
        host_id: r.host_id,
        session_id: r.session_id,
        epoch: r.epoch,
        summary: r.summary,
        evidence_refs: refs,
        created_at: r.created_at
      };
    });
  }
}
