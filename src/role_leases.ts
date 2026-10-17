import type { SqlStorage } from '@cloudflare/workers-types';

const NAME = /^[a-z][a-z0-9-]{0,47}$/;
const RESERVED = new Set(['root', 'principal', 'role', 'host', 'job', 'human', 'system', 'admin']);
export const ROLE_CAPACITY = 64;
export const ROLE_JOB_CAPACITY = 1024;
export const ROLE_RETENTION_MS = 3 * 86400000;

export function roleName(project: unknown, name: unknown): string | null {
  if (typeof project !== 'string' || typeof name !== 'string' || !NAME.test(project) || !NAME.test(name) ||
      RESERVED.has(project) || RESERVED.has(name)) return null;
  return `automation:${project}:${name}`;
}

/** Bounded canonical JSON: input keys are validated separately at each API boundary. */
export function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}

export class RoleLeases {
  constructor(private sql: SqlStorage) {
    sql.exec(`CREATE TABLE IF NOT EXISTS automation_roles (
      name TEXT PRIMARY KEY, generation INTEGER NOT NULL, config TEXT NOT NULL,
      last_clock INTEGER NOT NULL DEFAULT 0)`);
    const columns = ['role_name TEXT', 'role_generation INTEGER', 'role_host_credential TEXT',
      'role_expected_epoch INTEGER', 'role_effect TEXT', 'role_finish TEXT'];
    const existing = new Set(sql.exec('PRAGMA table_info(jobs)').toArray().map((r: any) => r.name));
    for (const col of columns) if (!existing.has(col.split(' ')[0])) sql.exec(`ALTER TABLE jobs ADD COLUMN ${col}`);
    sql.exec('CREATE INDEX IF NOT EXISTS jobs_role_retention ON jobs(role_name, finished_at)');
    sql.exec('CREATE INDEX IF NOT EXISTS jobs_role_credential ON jobs(role_host_credential, status)');
    sql.exec('CREATE INDEX IF NOT EXISTS jobs_role_pending_profile ON jobs(kind,payload) WHERE role_name IS NOT NULL AND role_effect IS NOT NULL AND role_finish IS NULL');
  }

  get(name: string): any {
    const row: any = this.sql.exec('SELECT * FROM automation_roles WHERE name = ?', name).toArray()[0];
    return row ? { ...row, ...JSON.parse(row.config) } : null;
  }

  /** Call only within the JobsService mutation transaction. Persisted across DO restarts. */
  clock(name: string, now: number): boolean {
    const row = this.get(name);
    if (!row || !Number.isSafeInteger(now) || now < row.last_clock) return false;
    this.sql.exec('UPDATE automation_roles SET last_clock = ? WHERE name = ?', now, name);
    return true;
  }

  host(hostId: string, credentialId: string, kind: string, now: number): any | null {
    const host: any = this.sql.exec('SELECT * FROM machines WHERE id = ?', hostId).toArray()[0];
    const cred: any = this.sql.exec('SELECT * FROM credentials WHERE id = ?', credentialId).toArray()[0];
    if (!host || !host.registered_at || host.credential_id !== credentialId || host.drained ||
        ['disabled', 'revoked', 'drained', 'dead'].includes(host.state) || !cred || cred.kind !== 'host' ||
        cred.machine_id !== hostId || cred.revoked_at || !(Date.parse(cred.expires_at) > now)) return null;
    try {
      const scopes = JSON.parse(cred.scopes);
      const caps = JSON.parse(host.capabilities);
      const heartbeat = JSON.parse(host.last_heartbeat);
      const age = now - Date.parse(host.last_checkin_at);
      if (!Array.isArray(scopes) || !scopes.includes('host') || scopes.includes('admin') ||
          !Array.isArray(caps.jobs) || !caps.jobs.includes(kind) || !Number.isFinite(age) ||
          age < 0 || age > 180000 || heartbeat?.gate?.launch_allowed !== true) return null;
    } catch { return null; }
    return { host, cred };
  }

  configure(name: string, input: any, expectedGeneration: number, now: number): any {
    if (!input || Object.keys(input).some(k => !['enabled', 'profiles', 'actions', 'hosts', 'ttl_seconds'].includes(k)) ||
        typeof input.enabled !== 'boolean' || !Array.isArray(input.profiles) || input.profiles.length !== 1 || input.profiles[0] !== 'writer-run' ||
        !Array.isArray(input.actions) || input.actions.length !== 1 || input.actions[0] !== 'execute' ||
        !Array.isArray(input.hosts) || input.hosts.length < 1 || input.hosts.length > 16 ||
        new Set(input.hosts).size !== input.hosts.length || input.hosts.some((h: any) => typeof h !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(h)) ||
        !Number.isInteger(input.ttl_seconds) || input.ttl_seconds < 60 || input.ttl_seconds > 1200 ||
        !Number.isSafeInteger(expectedGeneration) || expectedGeneration < 0) return { error: 'invalid_configuration' };
    const old = this.get(name);
    if ((old?.generation ?? 0) !== expectedGeneration) return { error: 'configuration_conflict' };
    const lease: any = this.sql.exec('SELECT epoch FROM leases WHERE name=?', name).toArray()[0];
    if ((old && (!Number.isSafeInteger(old.generation) || old.generation >= Number.MAX_SAFE_INTEGER)) ||
        (lease && (!Number.isSafeInteger(lease.epoch) || lease.epoch >= Number.MAX_SAFE_INTEGER))) return { error: 'epoch_capacity' };
    if (old && !this.clock(name, now)) return { error: 'clock_rollback' };
    if (!old && Number(this.sql.exec('SELECT count(*) AS n FROM automation_roles').one().n) >= ROLE_CAPACITY) return { error: 'role_capacity' };
    // Snapshot the current enrolled credential, never infer custody from tags or projects.
    const bindings: Record<string, string> = {};
    for (const id of input.hosts) {
      const host: any = this.sql.exec('SELECT credential_id FROM machines WHERE id = ?', id).toArray()[0];
      if (input.enabled && (!host || !this.host(id, host.credential_id, 'writer-run', now))) return { error: 'host_ineligible' };
      bindings[id] = host?.credential_id ?? '';
    }
    const generation = (old?.generation ?? 0) + 1;
    const config = { ...input, bindings };
    this.sql.exec('INSERT INTO automation_roles(name,generation,config,last_clock) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET generation=excluded.generation,config=excluded.config,last_clock=excluded.last_clock', name, generation, canonical(config), now);
    this.sql.exec('INSERT OR IGNORE INTO leases(name,epoch) VALUES(?,0)', name);
    // Config mutation is an immediate epoch fence, including disable and same-value reconfiguration.
    this.sql.exec("UPDATE jobs SET status='fenced', finished_at=?, outcome_summary='role configuration changed' WHERE role_name=? AND status IN ('offered','claimed','running','pending')", new Date(now).toISOString(), name);
    this.sql.exec("UPDATE credentials SET revoked_at=? WHERE kind='job' AND subject IN (SELECT id FROM jobs WHERE role_name=?) AND revoked_at IS NULL", new Date(now).toISOString(), name);
    this.sql.exec("UPDATE leases SET epoch=epoch+1,holder_job_id=NULL,holder_host_id=NULL,holder_machine_id=NULL,holder_actor=NULL,expires_at=NULL,released_at=?,release_reason='configuration changed' WHERE name=?", new Date(now).toISOString(), name);
    return { generation, epoch: this.sql.exec('SELECT epoch FROM leases WHERE name=?', name).one().epoch, enabled: input.enabled };
  }

  /** Source admission remains with existing profiles/M6; this grants no actor or filesystem identity. */
  binding(job: any, now: number): any | null {
    const role = this.get(job.role_name);
    if (!role || !role.enabled || role.generation !== job.role_generation || !this.clock(job.role_name, now) ||
        !role.profiles.includes(job.kind) || !role.actions.includes('execute')) return null;
    const hostId = job.holder_host_id || job.offer_host_id;
    if (role.bindings[hostId] !== job.role_host_credential || !this.host(hostId, job.role_host_credential, job.kind, now)) return null;
    const payload = JSON.parse(job.payload);
    if (payload.project !== job.role_name.split(':')[1]) return null;
    return role;
  }

  prune(now: number) {
    // Retire abandoned offers on bounded admission work, without a new timer.
    this.sql.exec(`UPDATE jobs SET status='expired',finished_at=offer_expires_at
      WHERE id IN (SELECT id FROM jobs WHERE role_name IS NOT NULL AND status='offered'
        AND offer_expires_at<=? LIMIT 64)`, new Date(now).toISOString());
    // Pending external effects stay forever (bounded by admission capacity), even if fenced.
    const cutoff = new Date(now - ROLE_RETENTION_MS).toISOString();
    const rows = this.sql.exec("SELECT id FROM jobs WHERE role_name IS NOT NULL AND finished_at < ? AND (role_effect IS NULL OR role_finish IS NOT NULL) LIMIT 64", cutoff).toArray();
    for (const row of rows) {
      this.sql.exec("DELETE FROM credentials WHERE kind='job' AND subject=?", row.id);
      this.sql.exec('DELETE FROM offers WHERE job_id=?', row.id);
      this.sql.exec('DELETE FROM health_events WHERE subject_kind=\'job\' AND subject=?', row.id);
      this.sql.exec('DELETE FROM jobs WHERE id=?', row.id);
    }
  }
}
