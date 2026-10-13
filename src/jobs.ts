import { sha256 } from './auth/crypto';

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
  if (expiresAt === null || expiresAt === undefined || expiresAt === '') return null;
  if (typeof expiresAt === 'number') {
    return expiresAt < 1e11 ? expiresAt * 1000 : expiresAt;
  }
  const str = String(expiresAt).trim();
  if (!str.includes('-') && !str.includes('T')) {
    const n = Number(str);
    if (!isNaN(n)) return n < 1e11 ? n * 1000 : n;
  }
  const parsed = Date.parse(str);
  return isNaN(parsed) ? null : parsed;
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
  private sql: SqlStorage;
  private ctx: DurableObjectState;
  private deps?: JobsDependencies;
  private now: () => Date;
  private getRandomValues: (array: Uint32Array) => Uint32Array;
  private rootHostsPolicy?: string[];
  private busService?: any;
  private healthService?: any;

  constructor(sql: SqlStorage, ctx: DurableObjectState, deps?: JobsDependencies) {
    this.sql = sql;
    this.ctx = ctx;
    this.deps = deps;
    this.now = deps?.now ? deps.now : (() => new Date());
    this.getRandomValues = deps?.getRandomValues || ((arr) => crypto.getRandomValues(arr));
    this.rootHostsPolicy = deps?.rootHosts;
    this.busService = deps?.busService;
    this.healthService = deps?.healthService;

    for (const statement of JOBS_SCHEMA_SQL.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }

    this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES ('root', 0)");
  }

  public setBusService(bus: any) {
    this.busService = bus;
  }

  public setHealthService(health: any) {
    this.healthService = health;
  }

  public setClock(now: () => Date) {
    this.now = now;
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
    return Array.from(array).map(n => n.toString(16).padStart(8, '0')).join('');
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
    const now = this.now();
    const nowMs = now.getTime();
    const nowIso = now.toISOString();

    this.ctx.storage.transactionSync(() => {
      // 1. Expire offers (root-run)
      const offeredJobs = this.sql.exec("SELECT * FROM jobs WHERE kind = 'root-run' AND status = 'offered' AND offer_expires_at IS NOT NULL").toArray();
      for (const job of offeredJobs) {
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
  }

  private tryOfferNext(job: any, now: Date) {
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

  tickRootSlot(slotStartUtc: string) {
    const now = this.now();
    const nowIso = now.toISOString();

    this.ctx.storage.transactionSync(() => {
      this.processAlarms();

      const idempotencyKey = `root-run:${slotStartUtc}`;
      const existing = this.sql.exec("SELECT id FROM jobs WHERE idempotency_key = ?", idempotencyKey).toArray();
      if (existing.length > 0) return;

      const rootLease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      if (rootLease && rootLease.holder_job_id) {
        if (!isLeaseExpired(rootLease.expires_at, now.getTime())) {
          const hostId = rootLease.holder_host_id || rootLease.holder_machine_id;
          const host = this.sql.exec("SELECT last_checkin_at FROM machines WHERE id = ?", hostId).toArray()[0] as any;
          if (host && host.last_checkin_at) {
             const age = now.getTime() - new Date(host.last_checkin_at).getTime();
             if (age <= 180000 && age >= 0) {
                this.sql.exec("INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  this.generateId(), 'root-run', idempotencyKey, '{}', 'skip', slotStartUtc, new Date(new Date(slotStartUtc).getTime() + 25*60000).toISOString(), 'system'
                );
                this.addHealthEvent('job', idempotencyKey, 'skip', 'root still running', nowIso);
                this.logAuditEvent('system', 'skip', 'leases/root', idempotencyKey, {
                  reason: 'root still running',
                  holder_job_id: rootLease.holder_job_id,
                  holder_host_id: hostId,
                  epoch: rootLease.epoch,
                  slot: slotStartUtc
                }, nowIso);
                return;
             }
          }
        }
      }

      const jobId = this.generateId();
      const deadlineAt = new Date(new Date(slotStartUtc).getTime() + 25 * 60000).toISOString();
      this.sql.exec("INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        jobId, 'root-run', idempotencyKey, '{}', 'pending', nowIso, deadlineAt, 'system'
      );

      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      this.tryOfferNext(job, now);
    });
  }

  private getLeaseNameForJob(job: any): string {
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

  async claimJob(
    jobId: string,
    hostId: string,
    hostFreshGate: boolean,
    _simNow?: Date
  ): Promise<{ error?: string, reason?: string, job_token?: string, lease_epoch?: number, deadline_at?: string }> {
    if (!hostFreshGate) return { error: 'gate_failed', reason: 'caller_gate_false' };

    // Real clock trusted capture; caller simNow is inert per contract
    const now = this.now();
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

      const leaseName = this.getLeaseNameForJob(job);
      this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES (?, 0)", leaseName);

      const currentLease = this.sql.exec("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
      if (currentLease && currentLease.holder_job_id) {
        if (!isLeaseExpired(currentLease.expires_at, nowMs)) {
          result = { error: 'conflict' };
          return;
        }
      }

      const leaseExpiresMs = Math.min(nowMs + 20 * 60000, deadlineMs);
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

      this.sql.exec(
        "UPDATE jobs SET status = 'claimed', claim_token_hash = ?, holder_host_id = ?, holder_machine_id = ?, lease_epoch = ? WHERE id = ?",
        tokenHash, hostId, hostId, newEpoch, jobId
      );

      // Mint scoped job credential in credentials table
      const credId = this.generateId();
      const jobScopes = job.kind === 'root-run'
        ? ['lease:root', 'jobs:create', 'bus:send', 'bus:read', 'health:report']
        : ['bus:send', 'bus:read', 'health:report'];

      this.sql.exec(
        "INSERT OR REPLACE INTO credentials (id, kind, token_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at) VALUES (?, 'job', ?, ?, ?, ?, ?, ?, 'system', ?, ?)",
        credId, tokenHash, credId, jobId, hostId, jobActor, JSON.stringify(jobScopes), nowIso, new Date(leaseExpiresMs).toISOString()
      );

      result = { job_token: token, lease_epoch: newEpoch, deadline_at: job.deadline_at };
    });

    if (!result?.error) {
      this.scheduleAlarm();
    }

    return result;
  }

  async declineJob(jobId: string, hostId: string, reason: string): Promise<{ error?: string }> {
    let result: any = {};
    const now = this.now();
    this.ctx.storage.transactionSync(() => {
      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!job || job.status !== 'offered' || (job.offer_host_id !== hostId && job.offer_machine_id !== hostId)) {
        result = { error: 'conflict' }; return;
      }
      this.addHealthEvent('job', jobId, 'declined', reason.substring(0, 4096), now.toISOString());
      this.tryOfferNext(job, now);
    });
    this.scheduleAlarm();
    return result;
  }

  async validateEpoch(jobId: string, jobTokenPlain: string, epoch: number): Promise<boolean> {
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') return false;
    const tokenHash = await sha256(jobTokenPlain);
    let valid = false;
    this.ctx.storage.transactionSync(() => {
      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) return;

      const cred = this.sql.exec(
        "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
        tokenHash, jobId
      ).toArray()[0] as any;
      if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) return;

      const leaseName = this.getLeaseNameForJob(job);
      const lease = this.sql.exec("SELECT * FROM leases WHERE name = ?", leaseName).toArray()[0] as any;
      if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId) return;

      const now = this.now();
      if (!isLeaseExpired(lease.expires_at, now.getTime())) valid = true;
    });
    return valid;
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
    if (!evidenceDigest || typeof evidenceDigest !== 'string' || evidenceDigest.trim().length === 0) {
      return { error: 'missing_first_action_evidence' };
    }
    if (!nativeSession || typeof nativeSession !== 'string' || nativeSession.trim().length === 0) {
      return { error: 'missing_native_session' };
    }
    if (!launcherTaskId || typeof launcherTaskId !== 'string' || launcherTaskId.trim().length === 0) {
      return { error: 'missing_launcher_task_id' };
    }
    const tokenHash = await sha256(jobTokenPlain);
    const now = this.now();
    const nowIso = now.toISOString();
    let result: any = {};

    this.ctx.storage.transactionSync(() => {
      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }
      if (job.status !== 'claimed') { result = { error: 'invalid_status' }; return; }

      // Check credential validity and non-revocation
      const cred = this.sql.exec(
        "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
        tokenHash, jobId
      ).toArray()[0] as any;
      if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
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
  }

  async renewJob(jobId: string, jobTokenPlain: string, epoch: number): Promise<{ error?: string }> {
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') {
      return { error: 'unauthorized_or_stale' };
    }
    const tokenHash = await sha256(jobTokenPlain);
    const now = this.now();
    let result: any = {};

    this.ctx.storage.transactionSync(() => {
      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }

      const validStatuses = ['claimed', 'running'];
      if (!validStatuses.includes(job.status)) {
        result = { error: 'unauthorized_or_stale' }; return;
      }

      const deadlineMs = new Date(job.deadline_at).getTime();
      if (now.getTime() >= deadlineMs) {
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

      const lease = this.resolveLeaseForJob(jobId, job);
      if (!lease || lease.epoch !== epoch || lease.holder_job_id !== jobId || isLeaseExpired(lease.expires_at, now.getTime())) {
        result = { error: 'unauthorized_or_stale' }; return;
      }

      const newExpiresMs = Math.min(now.getTime() + 20 * 60000, deadlineMs);
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
  }

  async finishJob(jobId: string, jobTokenPlain: string, epoch: number, status: string, summary: string, evidenceRefs: string[]): Promise<{ error?: string }> {
    const allowedStatuses = new Set(['succeeded', 'finished', 'failed', 'cancelled']);
    if (!allowedStatuses.has(status)) {
      return { error: 'invalid_status' };
    }
    if (!jobTokenPlain || typeof jobTokenPlain !== 'string') {
      return { error: 'unauthorized_or_stale' };
    }
    const tokenHash = await sha256(jobTokenPlain);
    const now = this.now();
    const nowMs = now.getTime();
    const nowIso = now.toISOString();
    let result: any = {};

    this.ctx.storage.transactionSync(() => {
      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!job || job.claim_token_hash !== tokenHash || job.lease_epoch !== epoch) { result = { error: 'unauthorized_or_stale' }; return; }

      // Terminal status check: cannot finish an already terminal or fenced job
      const terminalStatuses = ['succeeded', 'finished', 'failed', 'expired', 'cancelled', 'fenced'];
      if (terminalStatuses.includes(job.status)) {
        result = { error: 'unauthorized_or_stale' };
        return;
      }

      // Hardstop check: claimed or running job past hardstop cannot finish normally
      if (job.deadline_at && nowMs >= new Date(job.deadline_at).getTime()) {
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

      // Invalidate/revoke job credential
      this.sql.exec("UPDATE credentials SET revoked_at = ? WHERE subject = ? AND kind = 'job' AND revoked_at IS NULL", nowIso, jobId);

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
  }

  async createJob(kind: string, payload: any, actor: string, deadlineMins: number = 25, rootTokenPlain?: string, rootEpoch?: number): Promise<{ jobId?: string, error?: string }> {
    const now = this.now();
    let jobId = this.generateId();
    const idempotencyKey = `${kind}:${jobId}`;
    const deadlineAt = new Date(now.getTime() + deadlineMins * 60000).toISOString();
    let result: any = {};
    let tokenHash = rootTokenPlain ? await sha256(rootTokenPlain) : undefined;

    this.ctx.storage.transactionSync(() => {
      if (actor === 'role:root') {
        if (!tokenHash || rootEpoch === undefined) { result = { error: 'unauthorized' }; return; }
        const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
        if (!lease || lease.epoch !== rootEpoch || !lease.holder_job_id) { result = { error: 'unauthorized_or_stale' }; return; }
        if (isLeaseExpired(lease.expires_at, now.getTime())) { result = { error: 'unauthorized_or_stale' }; return; }
        const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", lease.holder_job_id).toArray()[0] as any;
        if (!job || job.claim_token_hash !== tokenHash || job.status === 'expired' || job.status === 'failed') { result = { error: 'unauthorized_or_stale' }; return; }
        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          tokenHash, lease.holder_job_id
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          result = { error: 'unauthorized_or_stale' };
          return;
        }
      } else if (actor === 'role:principal' || actor === 'principal' || actor === 'system' || actor === 'role:admin' || actor === 'admin' || actor.startsWith('role:admin') || actor.includes('@')) {
        // Trusted callers with jobs:create scope or admin sessions are allowed without root lease
      } else {
        result = { error: 'unauthorized' }; return;
      }

      this.sql.exec("INSERT INTO jobs (id, kind, idempotency_key, payload, status, created_at, deadline_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        jobId, kind, idempotencyKey, JSON.stringify(payload), 'pending', now.toISOString(), deadlineAt, actor);

      const job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      this.tryOfferNext(job, now);
      result = { jobId };
    });

    this.scheduleAlarm();

    return result;
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
    past_hardstop: boolean;
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
         WHERE (role = 'root' OR tag = 'role:root' OR tag = 'root')
           AND (state IS NULL OR state != 'retired')
           AND (health_state IS NULL OR health_state != 'retired')
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

    // Canonical agent state: state column takes precedence over health_state
    let canonicalAgentState = (regAgent?.state || regAgent?.health_state || regAgent?.lasthealth || 'healthy').toLowerCase();
    if (regAgent && this.healthService?.getEffectiveHealth) {
      const eff = this.healthService.getEffectiveHealth(regAgent.tag);
      if (eff && eff.state) {
        canonicalAgentState = eff.state.toLowerCase();
      }
    }
    const ALIVE_STATES = new Set(['healthy', 'active', 'working', 'idle']);
    const isAgentStateHealthy = ALIVE_STATES.has(canonicalAgentState);

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
          machineTransportKnown = true;
          const ageMs = nowMs - new Date(machine.last_checkin_at).getTime();
          checkinAgeS = Math.floor(ageMs / 1000);
          machineCheckinFresh = (ageMs >= 0 && ageMs <= 180000);
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

    let heartbeatFresh = false;
    let transportUnknown = false;

    if (leaseRow.holder_job_id) {
      if (machineTransportKnown) {
        heartbeatFresh = machineCheckinFresh;
        transportUnknown = false;
      } else {
        // Leased root but machine has no checkin: transport unknown
        transportUnknown = true;
        heartbeatFresh = false;
      }
    } else if (regAgent) {
      let regFresh = false;
      let regKnown = false;
      if (regAgent.last_seen_at) {
        regKnown = true;
        const regAgeMs = nowMs - new Date(regAgent.last_seen_at).getTime();
        regFresh = (regAgeMs >= 0 && regAgeMs <= 180000);
      }
      if (machineTransportKnown || regKnown) {
        transportUnknown = false;
        heartbeatFresh = machineCheckinFresh || regFresh;
      } else {
        transportUnknown = true;
        heartbeatFresh = false;
      }
    } else {
      // No incumbent at all
      transportUnknown = false;
      heartbeatFresh = false;
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

    const withinHardstop = Boolean(
      holderJob &&
      holderJob.deadline_at &&
      nowMs < new Date(holderJob.deadline_at).getTime()
    );

    const pastHardstop = Boolean(
      holderJob &&
      holderJob.deadline_at &&
      nowMs >= new Date(holderJob.deadline_at).getTime()
    );

    const isJobActive = Boolean(
      holderJob &&
      (holderJob.status === 'claimed' || holderJob.status === 'running')
    );

    const isExternalHealthyAlive = Boolean(
      !leaseRow.holder_job_id &&
      regAgent &&
      heartbeatFresh &&
      isAgentStateHealthy
    );

    const isHealthyAlive = Boolean(
      (withinLease && withinHardstop && heartbeatFresh && isJobActive) ||
      isExternalHealthyAlive
    );

    // Explicit confirmed stale policy:
    // Unknown transport is UNKNOWN, NOT proof of dead!
    let confirmedStale = false;
    if (transportUnknown) {
      confirmedStale = false;
    } else if (leaseRow.holder_job_id) {
      if (pastHardstop) {
        confirmedStale = true;
      } else if (!heartbeatFresh && (!withinLease || isLeaseExpired(leaseRow.expires_at, nowMs))) {
        confirmedStale = true;
      } else if (!heartbeatFresh && !isJobActive) {
        confirmedStale = true;
      } else if (!heartbeatFresh) {
        confirmedStale = true;
      }
    } else if (regAgent) {
      if (!heartbeatFresh || !isAgentStateHealthy) {
        confirmedStale = true;
      }
    }

    // Shared takeover safety policy
    let canTakeover = false;
    let takeoverDenialError: string | null = null;
    let takeoverDenialReason: string | null = null;

    if (isHealthyAlive) {
      canTakeover = false;
      takeoverDenialError = 'incumbent_still_healthy';
      takeoverDenialReason = 'incumbent is healthy alive with fresh heartbeat and valid state';
    } else if (transportUnknown) {
      canTakeover = false;
      takeoverDenialError = 'transport_unknown';
      takeoverDenialReason = 'incumbent transport is unknown; unknown transport is not proof of death';
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
      past_hardstop: pastHardstop,
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
    past_hardstop: boolean;
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
      past_hardstop: evalResult.past_hardstop,
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
    const now = this.now();
    const nowIso = now.toISOString();
    const nowMs = now.getTime();

    const token = this.generateToken();
    const tokenHash = await sha256(token);
    let result: any = null;

    this.ctx.storage.transactionSync(() => {
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
        if (candidateJob.offer_machine_id && candidateJob.offer_machine_id !== hostId) {
          result = { error: 'host_mismatch', reason: `candidate job offered to ${candidateJob.offer_machine_id}, not ${hostId}` };
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

      const newEpoch = currentLease.epoch + 1;
      const deadlineMs = candidateJob.deadline_at ? new Date(candidateJob.deadline_at).getTime() : (nowMs + 25 * 60000);
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
  }

  async verifyRootEpoch(
    jobId: string | null,
    jobTokenPlain: string | null,
    epoch: number,
    isHostSupervisor: boolean = false,
    hostId?: string | null
  ): Promise<{ valid: boolean; status?: string; reason?: string; current_epoch?: number; holder_job_id?: string | null }> {
    const tokenHash = jobTokenPlain ? await sha256(jobTokenPlain) : null;
    const now = this.now();
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
        this.logAuditEvent('role:root', 'fenced', 'leases/root', jobId || 'root', {
          job_id: jobId,
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

      if (jobId && lease.holder_job_id !== jobId) {
        result = { valid: false, status: 'unauthorized', reason: 'job_mismatch', current_epoch: lease.epoch };
        return;
      }

      if (hostId && lease.holder_host_id && lease.holder_host_id !== hostId) {
        result = { valid: false, status: 'unauthorized', reason: 'host_mismatch', current_epoch: lease.epoch };
        return;
      }

      let job: any = null;
      if (lease.holder_job_id) {
        job = this.sql.exec("SELECT * FROM jobs WHERE id = ?", lease.holder_job_id).toArray()[0] as any;
      }

      if (!isHostSupervisor && job) {
        if (!tokenHash || job.claim_token_hash !== tokenHash) {
          result = { valid: false, status: 'unauthorized', reason: 'unauthorized_token', current_epoch: lease.epoch };
          return;
        }
        if (tokenHash) {
          const cred = this.sql.exec(
            "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
            tokenHash, jobId || lease.holder_job_id
          ).toArray()[0] as any;
          if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
            result = { valid: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
            return;
          }
        }
      }

      if (job && job.deadline_at && nowMs >= new Date(job.deadline_at).getTime()) {
        result = { valid: false, status: 'expired', reason: 'past_hardstop', current_epoch: lease.epoch };
        return;
      }

      result = { valid: true, status: 'ok', current_epoch: lease.epoch, holder_job_id: lease.holder_job_id };
    });

    return result;
  }

  async commitRootAction(
    jobId: string,
    hostId: string,
    epoch: number,
    action: { type: string; name?: string; payload?: any; idempotency_key?: string },
    sessionId?: string,
    tokenHash?: string | null
  ): Promise<{
    success: boolean;
    status: string;
    reason?: string;
    current_epoch?: number;
    action_id?: number;
    result?: any;
    duplicate?: boolean;
  }> {
    const now = this.now();
    const nowMs = now.getTime();
    const nowIso = now.toISOString();

    const actionName = action?.name || `${action?.type || 'unknown'}:${action?.idempotency_key || 'default'}`;
    const idempotencyKey = action?.idempotency_key || actionName;
    const payloadDigest = await sha256(JSON.stringify(action?.payload || {}));

    let result: {
      success: boolean;
      status: string;
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
      if (lease.holder_job_id !== jobId || (lease.holder_host_id && lease.holder_host_id !== hostId)) {
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
      if (job.deadline_at && nowMs >= new Date(job.deadline_at).getTime()) {
        result = { success: false, status: 'expired', reason: 'past_hardstop', current_epoch: lease.epoch };
        return;
      }
      if (job.holder_host_id && job.holder_host_id !== hostId) {
        result = { success: false, status: 'unauthorized', reason: 'host_mismatch', current_epoch: lease.epoch };
        return;
      }
      if (job.claim_token_hash) {
        if (tokenHash && job.claim_token_hash !== tokenHash) {
          result = { success: false, status: 'unauthorized', reason: 'token_mismatch', current_epoch: lease.epoch };
          return;
        }
        const cred = this.sql.exec(
          "SELECT * FROM credentials WHERE token_hash = ? AND subject = ?",
          job.claim_token_hash, jobId
        ).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          result = { success: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch };
          return;
        }
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
        if (!p.to_actor || typeof p.to_actor !== 'string' || !p.kind || typeof p.kind !== 'string' || p.body === undefined) {
          result = { success: false, status: 'invalid_payload', reason: 'missing to_actor, kind, or body in bus_dispatch', current_epoch: epoch };
          return;
        }
        if (!this.busService) {
          result = { success: false, status: 'error', reason: 'bus_service_unavailable', current_epoch: epoch };
          return;
        }
        try {
          const bodyStr = typeof p.body === 'string' ? p.body : JSON.stringify(p.body);
          const callerCtx = {
            kind: 'job' as const,
            actor: 'role:root',
            jobId: jobId,
            tokenHash: job.claim_token_hash,
            machineId: hostId,
            scopes: ['lease:root', 'bus:send', 'bus:read']
          };
          const sendRes = this.busService.send(callerCtx, {
            senderActor: 'role:root',
            toActor: p.to_actor,
            kind: p.kind,
            body: bodyStr,
            threadId: p.thread_id,
            replyTo: p.reply_to,
            leaseEpoch: epoch,
            now: nowMs,
            idempotencyKey: `bus:${epoch}:${idempotencyKey}`
          });
          effectResult = { message_id: sendRes.id, thread_id: sendRes.threadId };
        } catch (busErr: any) {
          result = { success: false, status: 'invalid_payload', reason: busErr.message, current_epoch: epoch };
          return;
        }
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
        const summary = safeUtf8Summary(p.outcome_summary || '', 4096);
        const evidenceJson = JSON.stringify(p.outcome_evidence || []);
        this.sql.exec(
          "UPDATE jobs SET status = ?, finished_at = ?, outcome_summary = ?, outcome_evidence = ? WHERE id = ?",
          p.new_status, nowIso, summary, evidenceJson, p.target_job_id
        );
        this.addHealthEvent('job', p.target_job_id, p.new_status, `transitioned by root epoch ${epoch}`, nowIso);
        effectResult = { target_job_id: p.target_job_id, previous_status: targetJob.status, new_status: p.new_status };
      }

      // 8. Persist receipt into root_actions
      const ins = this.sql.exec(
        `INSERT INTO root_actions (job_id, host_id, session_id, epoch, action_name, action_type, idempotency_key, payload_digest, result, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        jobId, hostId, sessionId || job.native_session || '', epoch, actionName, action.type, idempotencyKey, payloadDigest, JSON.stringify(effectResult), nowIso
      );
      const actionId = ins.rowsWritten ? 1 : 0;

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
        current_epoch: epoch,
        action_id: actionId,
        result: effectResult
      };
    });

    return result;
  }

  postHandoffNotes(
    jobId: string,
    hostId: string,
    epoch: number,
    summary: string,
    evidenceRefs: string[] = [],
    sessionId?: string
  ): { success: boolean; id?: number; duplicate?: boolean } {
    const nowIso = this.now().toISOString();
    const safeSummary = safeUtf8Summary(summary || '', 4096);
    const cleanRefs = Array.isArray(evidenceRefs)
      ? evidenceRefs.slice(0, 50).map(r => safeUtf8Summary(String(r), 512))
      : [];
    const evidenceJson = JSON.stringify(cleanRefs);

    let res: any = { success: true };
    this.ctx.storage.transactionSync(() => {
      // Check idempotency for (job_id, epoch)
      const existing = this.sql.exec(
        "SELECT id FROM handoff_notes WHERE lease_name = 'root' AND job_id = ? AND epoch = ?",
        jobId, epoch
      ).toArray()[0] as any;

      if (existing) {
        this.sql.exec(
          "UPDATE handoff_notes SET summary = ?, evidence_refs = ?, session_id = ? WHERE id = ?",
          safeSummary, evidenceJson, sessionId || null, existing.id
        );
        res = { success: true, id: existing.id, duplicate: true };
      } else {
        const ins = this.sql.exec(
          "INSERT INTO handoff_notes (lease_name, job_id, host_id, session_id, epoch, summary, evidence_refs, created_at) VALUES ('root', ?, ?, ?, ?, ?, ?, ?)",
          jobId, hostId, sessionId || null, epoch, safeSummary, evidenceJson, nowIso
        );
        res = { success: true, id: ins.rowsWritten ? 1 : 0, duplicate: false };
      }

      this.logAuditEvent('host:' + hostId, 'handoff', 'leases/root', jobId, {
        job_id: jobId,
        epoch,
        summary_len: safeSummary.length,
        refs_count: cleanRefs.length
      }, nowIso);
    });

    return res;
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
