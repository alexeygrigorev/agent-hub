/**
 * M14/Addendum 6: Disk space monitoring, alert episodes and host ingestion.
 *
 * Provides:
 * - Machine-authenticated host ingestion (rejects spoofed machine IDs)
 * - Linux stdlib statvfs DTO validation (mount, size, used, free, percent, stable identity)
 * - Threshold evaluation (>=85% warn, >=95% critical)
 * - Deduplicated episode tracking for alerts and intake tasks (recovery without task flood)
 * - Measured history and growth per day trend calculation
 * - Bounded storage retention and data sanitization
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { LivenessService } from './liveness';
import type { StatusMachineAuth } from './status';
import type { IntakeService } from './intake';

export const MONITORING_SCHEMA = `
CREATE TABLE IF NOT EXISTS disk_observations (
  id TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  reporter TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disk_obs_machine ON disk_observations(machine_id, observed_at_ms);

CREATE TABLE IF NOT EXISTS disk_episodes (
  episode_key TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL,
  mount_point TEXT NOT NULL,
  state TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  last_updated_at TEXT NOT NULL,
  resolved_at TEXT,
  alert_id INTEGER,
  intake_task_id TEXT,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_disk_episodes_open ON disk_episodes(resolved_at, machine_id);

CREATE TABLE IF NOT EXISTS host_health_observations (
  machine_id TEXT PRIMARY KEY,
  observed_at TEXT NOT NULL,
  observed_at_ms INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  reporter TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_health_obs ON host_health_observations(machine_id, observed_at_ms);
`;

export const DISK_LIMITS = {
  maxPayloadBytes: 32768,
  maxHistoryPerMachine: 200,
  maxGlobalObservations: 1000,
  retentionDays: 14,
  minTrendWindowMs: 3600000, // 1 hour minimum history for trend
  maxTrendWindowMs: 7 * 86400000, // 7 days window for trend
  staleTrendAfterMs: 86400000, // 24 hours stale cutoff
  maxTopConsumers: 20,
  maxPathLength: 256,
};

export const DISK_THRESHOLDS = {
  warnPercent: 85.0,
  criticalPercent: 95.0,
};

const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const CONTROL_RE = /[\u0000-\u001f\u007f\u202e\ufeff]/g;

function fail(code: string, status = 400): never {
  const err: any = new Error(code);
  err.code = code;
  err.status = status;
  throw err;
}

function cleanPath(value: unknown): string {
  if (typeof value !== 'string') fail('invalid_path');
  const s = value.replace(CONTROL_RE, '').trim();
  if (!s || s.length > DISK_LIMITS.maxPathLength || !s.startsWith('/')) {
    fail('invalid_path');
  }
  return s;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let b = bytes;
  let unitIndex = 0;
  while (b >= 1024 && unitIndex < units.length - 1) {
    b /= 1024;
    unitIndex++;
  }
  return `${unitIndex === 0 ? b : b.toFixed(1)} ${units[unitIndex]}`;
}

export type DiskTopConsumer = {
  path: string;
  size_bytes: number;
};

export type DiskFilesystem = {
  mount_point: string;
  device_id: string;
  is_primary: boolean;
  primary_mount?: string | null;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent: number;
  status: 'ok' | 'warn' | 'critical' | 'error';
  top_consumers?: DiskTopConsumer[];
  top_consumers_truncated?: boolean;
  error?: string | null;
};

export type DiskTrend = {
  state: 'insufficient_history' | 'stale' | 'calculated';
  growth_bytes_per_day: number | null;
  growth_pct_per_day: number | null;
  sample_count?: number;
  window_hours?: number;
  reason?: string;
};

export type DiskFilesystemWithTrend = DiskFilesystem & {
  trend: DiskTrend;
};

export type DiskObservationPayload = {
  machine_id: string;
  observed_at: string;
  filesystems: DiskFilesystem[];
  errors?: string[];
};

export type HostLoad = {
  load1: number;
  load5: number;
  load15: number;
};

export type HostMemory = {
  total_bytes: number;
  available_bytes: number;
  used_bytes: number;
  percent: number;
  status: 'ok' | 'warn' | 'critical' | 'unknown';
  error?: string | null;
};

export type HostHealthPayload = {
  machine_id: string;
  observed_at: string;
  load: HostLoad;
  memory: HostMemory;
};

export type HostHealthSummary = {
  machine_id: string;
  observed_at: string;
  freshness: 'fresh' | 'stale' | 'unavailable' | 'unknown';
  load: HostLoad;
  memory: HostMemory;
};

export class MonitoringService {
  constructor(
    private sql: SqlStorage,
    private tx: <T>(fn: () => T) => T,
    private liveness?: LivenessService,
    private notifyAlertFn?: (alert: any) => Promise<void>,
    private intake?: IntakeService,
    public now: () => number = Date.now,
  ) {}

  init(): void {
    for (const stmt of MONITORING_SCHEMA.split(';')) {
      const trimmed = stmt.trim();
      if (trimmed) this.sql.exec(trimmed);
    }
  }

  setLiveness(liveness: LivenessService | undefined): void {
    this.liveness = liveness;
  }

  setNotifyAlert(notifyAlertFn: ((alert: any) => Promise<void>) | undefined): void {
    this.notifyAlertFn = notifyAlertFn;
  }

  /** Validate and ingest a host disk observation. */
  async handleObservation(request: Request, machineAuth: StatusMachineAuth | { error: Response }): Promise<Response> {
    const json = (data: any, status = 200) =>
      Response.json(data, {
        status,
        headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
      });

    try {
      if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
      if ((machineAuth as any).error) {
        const code = ((machineAuth as any).error as Response).status || 401;
        return json({ error: 'unauthorized_or_forbidden' }, code);
      }
      const auth = machineAuth as StatusMachineAuth;

      const text = await request.text();
      if (new TextEncoder().encode(text).length > DISK_LIMITS.maxPayloadBytes) {
        return json({ error: 'payload_too_large' }, 413);
      }

      let body: any = null;
      try {
        body = JSON.parse(text);
      } catch {
        return json({ error: 'malformed_json' }, 400);
      }

      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return json({ error: 'invalid_body' }, 400);
      }

      const validated = this.validatePayload(body, auth);

      const result = this.tx(() => this.recordObservation(validated, auth));
      return json(result, 200);
    } catch (e: any) {
      const code = e?.code || 'internal_error';
      const status = e?.status || 500;
      return Response.json({ error: code }, { status, headers: { 'Cache-Control': 'private, no-store' } });
    }
  }

  /** Read-only GET query for disk status across all machines or for one machine. */
  async handleGet(request: Request, auth: any): Promise<Response> {
    const json = (data: any, status = 200) =>
      Response.json(data, {
        status,
        headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
      });

    try {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      if (auth.error) {
        const code = (auth.error as Response).status || 401;
        return json({ error: 'unauthorized_or_forbidden' }, code);
      }

      const url = new URL(request.url);
      const machineFilter = url.searchParams.get('machine');
      if (machineFilter && !LABEL_RE.test(machineFilter)) {
        return json({ error: 'invalid_machine' }, 400);
      }

      const now = this.now();
      const status = this.getDiskStatus(now, machineFilter || undefined);
      return json(status, 200);
    } catch (e: any) {
      const code = e?.code || 'internal_error';
      const status = e?.status || 500;
      return Response.json({ error: code }, { status, headers: { 'Cache-Control': 'private, no-store' } });
    }
  }

  /** Validate and ingest a host resource health observation (load averages and memory). */
  async handleHostHealthObservation(
    request: Request,
    machineAuth: StatusMachineAuth | { error: Response },
  ): Promise<Response> {
    const json = (data: any, status = 200) =>
      Response.json(data, {
        status,
        headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
      });

    try {
      if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
      if ((machineAuth as any).error) {
        const code = ((machineAuth as any).error as Response).status || 401;
        return json({ error: 'unauthorized_or_forbidden' }, code);
      }
      const auth = machineAuth as StatusMachineAuth;

      const text = await request.text();
      if (new TextEncoder().encode(text).length > 32768) {
        return json({ error: 'payload_too_large' }, 413);
      }

      let body: any = null;
      try {
        body = JSON.parse(text);
      } catch {
        return json({ error: 'malformed_json' }, 400);
      }

      if (!body || typeof body !== 'object' || Array.isArray(body)) {
        return json({ error: 'invalid_body' }, 400);
      }

      const validated = this.validateHostHealthPayload(body, auth);
      const result = this.tx(() => this.recordHostHealthObservation(validated, auth));
      return json(result, 200);
    } catch (e: any) {
      const code = e?.code || 'internal_error';
      const status = e?.status || 500;
      return Response.json({ error: code }, { status, headers: { 'Cache-Control': 'private, no-store' } });
    }
  }

  /** Read-only GET query for host health status across all machines or for one machine. */
  async handleHostHealthGet(request: Request, auth: any): Promise<Response> {
    const json = (data: any, status = 200) =>
      Response.json(data, {
        status,
        headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
      });

    try {
      if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      if (auth.error) {
        const code = (auth.error as Response).status || 401;
        return json({ error: 'unauthorized_or_forbidden' }, code);
      }

      const url = new URL(request.url);
      const machineFilter = url.searchParams.get('machine');
      if (machineFilter && !LABEL_RE.test(machineFilter)) {
        return json({ error: 'invalid_machine' }, 400);
      }

      const now = this.now();
      const status = this.getHostHealthStatus(now, machineFilter || undefined);
      return json(status, 200);
    } catch (e: any) {
      const code = e?.code || 'internal_error';
      const status = e?.status || 500;
      return Response.json({ error: code }, { status, headers: { 'Cache-Control': 'private, no-store' } });
    }
  }

  /** Validate incoming payload against strict schema and caller authority. */
  private validatePayload(body: any, auth: StatusMachineAuth): DiskObservationPayload {
    if (typeof body.machine_id !== 'string' || !LABEL_RE.test(body.machine_id)) {
      fail('invalid_machine_id');
    }

    // Machine-bound write authority: host credentials can only report for their own machine!
    if (auth.kind === 'host' && body.machine_id !== auth.machine_id) {
      fail('observation_forbidden', 403);
    }

    if (typeof body.observed_at !== 'string' || !/Z|[+-]\d{2}:?\d{2}$/.test(body.observed_at)) {
      fail('invalid_observed_at');
    }
    const observedMs = Date.parse(body.observed_at);
    if (!Number.isFinite(observedMs)) fail('invalid_observed_at');
    const now = this.now();
    if (observedMs > now + 60000) fail('future_observation');
    if (observedMs < now - 30 * 86400000) fail('stale_observation');

    if (!Array.isArray(body.filesystems) || body.filesystems.length === 0 || body.filesystems.length > 32) {
      fail('invalid_filesystems');
    }

    const filesystems: DiskFilesystem[] = [];
    const seenMounts = new Set<string>();

    for (const raw of body.filesystems) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) fail('invalid_filesystem_entry');

      const mount = cleanPath(raw.mount_point);
      if (seenMounts.has(mount)) fail('duplicate_mount_point');
      seenMounts.add(mount);

      const deviceId = String(raw.device_id ?? '').replace(CONTROL_RE, '').trim();
      if (!deviceId || deviceId.length > 64) fail('invalid_device_id');

      const isPrimary = Boolean(raw.is_primary);
      const primaryMount = raw.primary_mount ? cleanPath(raw.primary_mount) : null;

      const total = Number(raw.total_bytes);
      const used = Number(raw.used_bytes);
      const free = Number(raw.free_bytes);

      if (!Number.isSafeInteger(total) || total < 0) fail('invalid_total_bytes');
      if (!Number.isSafeInteger(used) || used < 0) fail('invalid_used_bytes');
      if (!Number.isSafeInteger(free) || free < 0) fail('invalid_free_bytes');

      let percent = typeof raw.percent === 'number' ? raw.percent : total > 0 ? (used / total) * 100 : 0;
      if (!Number.isFinite(percent) || percent < 0 || percent > 100) fail('invalid_percent');
      percent = Math.round(percent * 10) / 10;

      // Status derived from threshold: >=95% critical, >=85% warn, else ok; error if error reported
      let computedStatus: 'ok' | 'warn' | 'critical' | 'error' = 'ok';
      if (raw.error || raw.status === 'error') {
        computedStatus = 'error';
      } else if (percent >= DISK_THRESHOLDS.criticalPercent) {
        computedStatus = 'critical';
      } else if (percent >= DISK_THRESHOLDS.warnPercent) {
        computedStatus = 'warn';
      }

      const topConsumers: DiskTopConsumer[] = [];
      if (Array.isArray(raw.top_consumers)) {
        for (const tc of raw.top_consumers.slice(0, DISK_LIMITS.maxTopConsumers)) {
          if (!tc || typeof tc !== 'object') continue;
          const p = cleanPath(tc.path);
          const sz = Number(tc.size_bytes);
          if (Number.isSafeInteger(sz) && sz >= 0) {
            topConsumers.push({ path: p, size_bytes: sz });
          }
        }
      }

      filesystems.push({
        mount_point: mount,
        device_id: deviceId,
        is_primary: isPrimary,
        primary_mount: primaryMount,
        total_bytes: total,
        used_bytes: used,
        free_bytes: free,
        percent,
        status: computedStatus,
        top_consumers: topConsumers,
        top_consumers_truncated: Boolean(raw.top_consumers_truncated),
        error: raw.error ? String(raw.error).slice(0, 128) : null,
      });
    }

    const errors: string[] = [];
    if (Array.isArray(body.errors)) {
      for (const e of body.errors) {
        if (typeof e === 'string') errors.push(e.slice(0, 256));
      }
    }

    return {
      machine_id: body.machine_id,
      observed_at: new Date(observedMs).toISOString(),
      filesystems,
      errors: errors.slice(0, 10),
    };
  }

  /** Record observation, handle alert and task episodes, and enforce storage bounds. */
  private recordObservation(payload: DiskObservationPayload, auth: StatusMachineAuth): any {
    const now = this.now();
    const nowIso = new Date(now).toISOString();
    const id = crypto.randomUUID();
    const observedMs = Date.parse(payload.observed_at);
    const reporter = auth.kind === 'host' ? `host:${auth.tokenId}` : `admin:${auth.actor}`;

    // 1. Store observation
    this.sql.exec(
      'INSERT INTO disk_observations(id, machine_id, observed_at, observed_at_ms, received_at, payload_json, reporter) VALUES(?,?,?,?,?,?,?)',
      id,
      payload.machine_id,
      payload.observed_at,
      observedMs,
      nowIso,
      JSON.stringify(payload),
      reporter,
    );

    // 2. Reconcile episodes for each filesystem
    const episodesSummary: any[] = [];
    for (const fs of payload.filesystems) {
      const episode = this.reconcileFilesystemEpisode(payload.machine_id, fs, now, nowIso);
      if (episode) episodesSummary.push(episode);
    }

    // 3. Enforce storage bounds & retention
    this.pruneStorage(payload.machine_id, now);

    return {
      ok: true,
      id,
      machine_id: payload.machine_id,
      observed_at: payload.observed_at,
      filesystems_count: payload.filesystems.length,
      episodes: episodesSummary,
    };
  }

  /** Deduplicated alert and intake task management per host and filesystem. */
  private reconcileFilesystemEpisode(
    machineId: string,
    fs: DiskFilesystem,
    now: number,
    nowIso: string,
  ): any | null {
    const episodeKey = `disk:${machineId}:${fs.mount_point}`;
    const row: any = this.sql
      .exec('SELECT * FROM disk_episodes WHERE episode_key = ?', episodeKey)
      .toArray()[0];

    const isProblem = fs.status === 'warn' || fs.status === 'critical';

    if (isProblem) {
      let topConsumersText = '';
      if (fs.top_consumers && fs.top_consumers.length) {
        topConsumersText =
          '; Top: ' +
          fs.top_consumers
            .slice(0, 3)
            .map(c => `${c.path} (${formatBytes(c.size_bytes)})`)
            .join(', ');
      }
      const detail = `${fs.mount_point} on ${machineId} is at ${fs.percent}% (${formatBytes(fs.used_bytes)} / ${formatBytes(fs.total_bytes)} used)${topConsumersText}`;

      if (!row || row.resolved_at !== null) {
        // Newly opened episode!
        let alertId: number | null = null;
        if (this.liveness) {
          const alert = this.liveness.setAlert('disk', `${machineId}:${fs.mount_point}`, {
            state: fs.status,
            detail,
          });
          if (alert && typeof alert.id === 'number') {
            alertId = alert.id;
            if (this.notifyAlertFn) {
              this.notifyAlertFn(alert).catch(() => {});
            }
          }
        }

        // Raise intake task for disk pressure (honest capacity refusal if limits reached)
        const taskRes = this.createOrUpdateIntakeTask(null, machineId, fs, detail, now);
        const taskId = taskRes.taskId;

        const detailsJson = JSON.stringify({
          status: fs.status,
          percent: fs.percent,
          used_bytes: fs.used_bytes,
          total_bytes: fs.total_bytes,
          top_consumers: fs.top_consumers || [],
          capacity_refused: !taskId ? (taskRes.error || 'capacity_refused') : undefined,
        });

        this.sql.exec(
          `INSERT INTO disk_episodes(episode_key, machine_id, mount_point, state, opened_at, last_updated_at, resolved_at, alert_id, intake_task_id, details_json)
           VALUES(?,?,?,?,?,?,NULL,?,?,?)
           ON CONFLICT(episode_key) DO UPDATE SET
             state = excluded.state,
             opened_at = excluded.opened_at,
             last_updated_at = excluded.last_updated_at,
             resolved_at = NULL,
             alert_id = excluded.alert_id,
             intake_task_id = excluded.intake_task_id,
             details_json = excluded.details_json`,
          episodeKey,
          machineId,
          fs.mount_point,
          fs.status,
          nowIso,
          nowIso,
          alertId,
          taskId,
          detailsJson,
        );

        return { episode_key: episodeKey, action: 'opened', state: fs.status };
      } else {
        // Existing open episode: update alert state if severity changed
        const stateChanged = row.state !== fs.status;
        const needsTaskRetry = !row.intake_task_id;
        let taskId = row.intake_task_id;

        if (needsTaskRetry) {
          // Retry creating orphan task if capacity was previously refused
          const retryRes = this.createOrUpdateIntakeTask(null, machineId, fs, detail, now);
          if (retryRes.taskId) {
            taskId = retryRes.taskId;
          }
        } else if (stateChanged && taskId) {
          // Record severity escalation or de-escalation on existing task
          this.createOrUpdateIntakeTask(taskId, machineId, fs, detail, now, row.state);
        }
        // If unchanged poll (stateChanged === false && !needsTaskRetry), DO NOT emit event or touch intake task

        if (stateChanged && this.liveness) {
          this.liveness.setAlert('disk', `${machineId}:${fs.mount_point}`, {
            state: fs.status,
            detail,
          });
        }

        const detailsJson = JSON.stringify({
          status: fs.status,
          percent: fs.percent,
          used_bytes: fs.used_bytes,
          total_bytes: fs.total_bytes,
          top_consumers: fs.top_consumers || [],
          capacity_refused: !taskId ? 'capacity_refused' : undefined,
        });

        this.sql.exec(
          'UPDATE disk_episodes SET state = ?, last_updated_at = ?, intake_task_id = ?, details_json = ? WHERE episode_key = ?',
          fs.status,
          nowIso,
          taskId,
          detailsJson,
          episodeKey,
        );

        return { episode_key: episodeKey, action: stateChanged ? 'severity_changed' : 'updated', state: fs.status };
      }
    } else {
      // Normal state (< 85%)
      if (row && row.resolved_at === null) {
        // Episode recovered!
        if (this.liveness) {
          this.liveness.setAlert('disk', `${machineId}:${fs.mount_point}`, null);
        }

        if (row.intake_task_id) {
          this.resolveIntakeTask(row.intake_task_id, machineId, fs, now);
        }

        this.sql.exec(
          'UPDATE disk_episodes SET resolved_at = ?, last_updated_at = ? WHERE episode_key = ?',
          nowIso,
          nowIso,
          episodeKey,
        );

        return { episode_key: episodeKey, action: 'resolved', state: 'ok' };
      }
    }

    return null;
  }

  /** Create or update an intake task for a disk episode without task flood. */
  private createOrUpdateIntakeTask(
    existingTaskId: string | null,
    machineId: string,
    fs: DiskFilesystem,
    detail: string,
    now: number,
    previousState?: string,
  ): { taskId: string | null; error?: string } {
    const topStr = (fs.top_consumers || [])
      .map(c => `  - ${c.path}: ${formatBytes(c.size_bytes)}`)
      .join('\n');
    const text = `Disk pressure alert on host ${machineId}.\nMount: ${fs.mount_point}\nDevice: ${fs.device_id}\nUsed: ${formatBytes(fs.used_bytes)} / ${formatBytes(fs.total_bytes)} (${fs.percent}%)\nStatus: ${fs.status.toUpperCase()}\n\nTop Consumers:\n${topStr || '  (none recorded)'}`;

    if (existingTaskId) {
      if (this.intake) {
        const res = this.intake.serviceRecordEvent(existingTaskId, {
          currentState: detail,
          nextStep: 'Investigate and free space on ' + fs.mount_point,
          event: {
            kind: 'update',
            text: `Disk status changed from ${previousState || 'previous'} to ${fs.status}: ${fs.mount_point} is at ${fs.percent}% (${fs.status})`,
            actor: 'hub:service:disk-monitor',
            sourceRef: `disk_monitor:${machineId}:${fs.mount_point}`,
            authority: 'operator_record',
          },
          now,
        });
        return { taskId: res.ok ? existingTaskId : null, error: res.ok ? undefined : res.error };
      }

      try {
        const row: any = this.sql.exec('SELECT data FROM intake_tasks WHERE id = ?', existingTaskId).toArray()[0];
        if (row) {
          const task = JSON.parse(row.data);
          task.version = (task.version || 1) + 1;
          task.updated_at = now;
          task.current_state = detail;
          task.history = task.history || [];
          if (task.history.length >= 128) {
            const intake0 = task.history[0];
            const remaining = task.history.slice(1);
            const dropIdx = remaining.findIndex((e: any) => typeof e.actor === 'string' && e.actor.startsWith('hub:service:'));
            if (dropIdx >= 0) {
              remaining.splice(dropIdx, 1);
              task.history = [intake0, ...remaining];
            }
          }
          task.history.push({
            id: crypto.randomUUID(),
            kind: 'update',
            actor: 'hub:service:disk-monitor',
            captured_at: now,
            text: `Disk status changed from ${previousState || 'previous'} to ${fs.status}: ${fs.mount_point} is at ${fs.percent}% (${fs.status})`,
            authority: 'operator_record',
          });
          this.sql.exec('UPDATE intake_tasks SET data = ? WHERE id = ?', JSON.stringify(task), existingTaskId);
          this.sql.exec('UPDATE intake_meta SET revision = revision + 1 WHERE id = 1');
          return { taskId: existingTaskId };
        }
      } catch (err: any) {
        return { taskId: null, error: err?.message };
      }
      return { taskId: null, error: 'task_not_found' };
    }

    if (this.intake) {
      const res = this.intake.serviceCreateTask({
        project: 'infrastructure',
        title: `[Disk pressure] ${machineId}: ${fs.mount_point} (${fs.percent}%)`,
        text,
        currentState: detail,
        nextStep: 'Investigate and free space on ' + fs.mount_point,
        sourceRef: `disk_monitor:${machineId}:${fs.mount_point}`,
        actor: 'hub:service:disk-monitor',
        now,
      });
      return { taskId: res.ok ? res.taskId : null, error: res.ok ? undefined : res.error };
    }

    try {
      const count = Number((this.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0] as any)?.n ?? 0);
      if (count >= 256) return { taskId: null, error: 'record_capacity' };

      const taskId = crypto.randomUUID();
      const task = {
        id: taskId,
        project: 'infrastructure',
        title: `[Disk pressure] ${machineId}: ${fs.mount_point} (${fs.percent}%)`,
        status: 'open',
        version: 1,
        created_at: now,
        updated_at: now,
        current_state: detail,
        next_step: 'Investigate and free space on ' + fs.mount_point,
        owner: null,
        uncertainty: null,
        history: [
          {
            id: crypto.randomUUID(),
            kind: 'intake',
            actor: 'hub:service:disk-monitor',
            captured_at: now,
            text,
            source: { type: 'unknown', reference: `disk_monitor:${machineId}:${fs.mount_point}`, timestamp: new Date(now).toISOString() },
            authority: 'operator_record',
          },
        ],
      };
      this.sql.exec('INSERT INTO intake_tasks(id, data) VALUES(?, ?)', taskId, JSON.stringify(task));
      this.sql.exec('UPDATE intake_meta SET revision = revision + 1 WHERE id = 1');
      return { taskId };
    } catch (err: any) {
      return { taskId: null, error: err?.message };
    }
  }

  /** Mark an intake task as resolved on recovery. */
  private resolveIntakeTask(taskId: string, machineId: string, fs: DiskFilesystem, now: number): void {
    if (this.intake) {
      this.intake.serviceRecordEvent(taskId, {
        status: 'done',
        currentState: `Recovered: ${fs.mount_point} is at ${fs.percent}% (< 85%)`,
        nextStep: null,
        event: {
          kind: 'resolution',
          text: `Recovered: ${fs.mount_point} space pressure resolved, now at ${fs.percent}% usage.`,
          actor: 'hub:service:disk-monitor',
          sourceRef: `disk_monitor:${machineId}:${fs.mount_point}`,
          authority: 'operator_record',
        },
        now,
      });
      return;
    }

    try {
      const row: any = this.sql.exec('SELECT data FROM intake_tasks WHERE id = ?', taskId).toArray()[0];
      if (!row) return;
      const task = JSON.parse(row.data);
      task.status = 'done';
      task.version = (task.version || 1) + 1;
      task.updated_at = now;
      task.current_state = `Recovered: ${fs.mount_point} is at ${fs.percent}% (< 85%)`;
      task.next_step = null;
      task.history = task.history || [];
      if (task.history.length >= 128) {
        const intake0 = task.history[0];
        const remaining = task.history.slice(1);
        const dropIdx = remaining.findIndex((e: any) => typeof e.actor === 'string' && e.actor.startsWith('hub:service:'));
        if (dropIdx >= 0) {
          remaining.splice(dropIdx, 1);
          task.history = [intake0, ...remaining];
        }
      }
      task.history.push({
        id: crypto.randomUUID(),
        kind: 'resolution',
        actor: 'hub:service:disk-monitor',
        captured_at: now,
        text: `Recovered: ${fs.mount_point} space pressure resolved, now at ${fs.percent}% usage.`,
        authority: 'operator_record',
      });
      this.sql.exec('UPDATE intake_tasks SET data = ? WHERE id = ?', JSON.stringify(task), taskId);
      this.sql.exec('UPDATE intake_meta SET revision = revision + 1 WHERE id = 1');
    } catch {
      // Best-effort intake resolution
    }
  }

  /** Prune storage to bounded limits. */
  private pruneStorage(machineId: string, now: number): void {
    try {
      const retentionCutoff = now - DISK_LIMITS.retentionDays * 86400000;
      this.sql.exec('DELETE FROM disk_observations WHERE observed_at_ms < ?', retentionCutoff);

      // Per-machine cap
      const machineRows = this.sql
        .exec(
          'SELECT id FROM disk_observations WHERE machine_id = ? ORDER BY observed_at_ms DESC',
          machineId,
        )
        .toArray() as any[];
      if (machineRows.length > DISK_LIMITS.maxHistoryPerMachine) {
        const toDelete = machineRows.slice(DISK_LIMITS.maxHistoryPerMachine).map(r => r.id);
        const placeholders = toDelete.map(() => '?').join(',');
        this.sql.exec(`DELETE FROM disk_observations WHERE id IN (${placeholders})`, ...toDelete);
      }

      // Global cap
      const totalCount = Number(
        (this.sql.exec('SELECT COUNT(*) AS n FROM disk_observations').toArray()[0] as any)?.n ?? 0,
      );
      if (totalCount > DISK_LIMITS.maxGlobalObservations) {
        this.sql.exec(
          `DELETE FROM disk_observations WHERE id IN (
            SELECT id FROM disk_observations ORDER BY observed_at_ms ASC LIMIT ?
          )`,
          totalCount - DISK_LIMITS.maxGlobalObservations,
        );
      }
    } catch {
      // Best-effort storage trim
    }
  }

  /** Compute growth trend per day for a filesystem on a machine. */
  private computeTrend(machineId: string, mountPoint: string, now: number): DiskTrend {
    try {
      const windowStart = now - DISK_LIMITS.maxTrendWindowMs;
      const rows = this.sql
        .exec(
          'SELECT observed_at_ms, payload_json FROM disk_observations WHERE machine_id = ? AND observed_at_ms >= ? ORDER BY observed_at_ms ASC',
          machineId,
          windowStart,
        )
        .toArray() as any[];

      const samples: { timeMs: number; usedBytes: number; totalBytes: number; percent: number }[] = [];

      for (const r of rows) {
        try {
          const payload = JSON.parse(r.payload_json);
          const fs = (payload.filesystems || []).find((f: any) => f.mount_point === mountPoint);
          if (fs && Number.isSafeInteger(fs.used_bytes) && Number.isSafeInteger(fs.total_bytes)) {
            samples.push({
              timeMs: r.observed_at_ms,
              usedBytes: fs.used_bytes,
              totalBytes: fs.total_bytes,
              percent: fs.percent,
            });
          }
        } catch {}
      }

      if (samples.length < 2) {
        return {
          state: 'insufficient_history',
          growth_bytes_per_day: null,
          growth_pct_per_day: null,
          reason: 'insufficient_samples',
        };
      }

      const oldest = samples[0];
      const latest = samples[samples.length - 1];
      const spanMs = latest.timeMs - oldest.timeMs;

      if (spanMs < DISK_LIMITS.minTrendWindowMs) {
        return {
          state: 'insufficient_history',
          growth_bytes_per_day: null,
          growth_pct_per_day: null,
          reason: 'window_less_than_1h',
        };
      }

      if (now - latest.timeMs > DISK_LIMITS.staleTrendAfterMs) {
        return {
          state: 'stale',
          growth_bytes_per_day: null,
          growth_pct_per_day: null,
          reason: 'latest_sample_stale',
        };
      }

      const days = spanMs / 86400000;
      const deltaBytes = latest.usedBytes - oldest.usedBytes;
      const deltaPct = latest.percent - oldest.percent;

      return {
        state: 'calculated',
        growth_bytes_per_day: Math.round(deltaBytes / days),
        growth_pct_per_day: Math.round((deltaPct / days) * 100) / 100,
        sample_count: samples.length,
        window_hours: Math.round((spanMs / 3600000) * 10) / 10,
      };
    } catch {
      return {
        state: 'insufficient_history',
        growth_bytes_per_day: null,
        growth_pct_per_day: null,
        reason: 'calculation_error',
      };
    }
  }

  /** Complete disk status with trends and active episodes. */
  getDiskStatus(now: number = this.now(), machineFilter?: string): any {
    const machinesQuery = machineFilter
      ? this.sql.exec('SELECT DISTINCT machine_id FROM disk_observations WHERE machine_id = ?', machineFilter).toArray()
      : this.sql.exec('SELECT DISTINCT machine_id FROM disk_observations').toArray();

    const machineList: any[] = [];

    for (const mRow of machinesQuery as any[]) {
      const mid = mRow.machine_id;
      const latestRow: any = this.sql
        .exec('SELECT * FROM disk_observations WHERE machine_id = ? ORDER BY observed_at_ms DESC LIMIT 1', mid)
        .toArray()[0];
      if (!latestRow) continue;

      let payload: DiskObservationPayload;
      try {
        payload = JSON.parse(latestRow.payload_json);
      } catch {
        continue;
      }

      const filesystemsWithTrend: DiskFilesystemWithTrend[] = (payload.filesystems || []).map(fs => {
        const trend = fs.is_primary ? this.computeTrend(mid, fs.mount_point, now) : {
          state: 'insufficient_history' as const,
          growth_bytes_per_day: null,
          growth_pct_per_day: null,
          reason: 'secondary_mount_alias',
        };
        return {
          ...fs,
          trend,
        };
      });

      const openEpisodes = this.sql
        .exec(
          'SELECT episode_key, mount_point, state, opened_at, last_updated_at, alert_id, intake_task_id FROM disk_episodes WHERE machine_id = ? AND resolved_at IS NULL',
          mid,
        )
        .toArray();

      const observedMs = latestRow.observed_at_ms;
      const ageMs = now - observedMs;
      const freshness = ageMs <= 180000 ? 'fresh' : ageMs <= 900000 ? 'stale' : 'unavailable';

      machineList.push({
        machine_id: mid,
        observed_at: latestRow.observed_at,
        freshness,
        filesystems: filesystemsWithTrend,
        open_episodes: openEpisodes,
      });
    }

    return {
      snapshot_at: new Date(now).toISOString(),
      machines: machineList,
    };
  }

  private validateHostHealthPayload(body: any, auth: StatusMachineAuth): HostHealthPayload {
    if (typeof body.machine_id !== 'string' || !LABEL_RE.test(body.machine_id)) {
      fail('invalid_machine_id');
    }
    if (auth.kind === 'host' && body.machine_id !== auth.machine_id) {
      fail('observation_forbidden', 403);
    }
    if (typeof body.observed_at !== 'string' || !/Z|[+-]\d{2}:?\d{2}$/.test(body.observed_at)) {
      fail('invalid_observed_at');
    }
    const observedMs = Date.parse(body.observed_at);
    if (!Number.isFinite(observedMs)) fail('invalid_observed_at');
    const now = this.now();
    if (observedMs > now + 60000) fail('future_observation');
    if (observedMs < now - 30 * 86400000) fail('stale_observation');

    if (!body.load || typeof body.load !== 'object' || Array.isArray(body.load)) {
      fail('invalid_load');
    }
    const load1 = Number(body.load.load1);
    const load5 = Number(body.load.load5);
    const load15 = Number(body.load.load15);
    if (!Number.isFinite(load1) || load1 < 0 || !Number.isFinite(load5) || load5 < 0 || !Number.isFinite(load15) || load15 < 0) {
      fail('invalid_load');
    }

    if (!body.memory || typeof body.memory !== 'object' || Array.isArray(body.memory)) {
      fail('invalid_memory');
    }
    const totalBytes = Number(body.memory.total_bytes);
    const availableBytes = Number(body.memory.available_bytes);
    const usedBytes = Number(body.memory.used_bytes);
    const percent = Number(body.memory.percent);
    if (!Number.isFinite(totalBytes) || totalBytes < 0 || !Number.isFinite(availableBytes) || availableBytes < 0 ||
        !Number.isFinite(usedBytes) || usedBytes < 0 || !Number.isFinite(percent) || percent < 0 || percent > 100) {
      fail('invalid_memory');
    }
    const status = body.memory.status;
    if (!['ok', 'warn', 'critical', 'unknown'].includes(status)) {
      fail('invalid_memory_status');
    }

    return {
      machine_id: body.machine_id,
      observed_at: new Date(observedMs).toISOString(),
      load: { load1, load5, load15 },
      memory: {
        total_bytes: Math.floor(totalBytes),
        available_bytes: Math.floor(availableBytes),
        used_bytes: Math.floor(usedBytes),
        percent: Number(percent.toFixed(1)),
        status,
        error: typeof body.memory.error === 'string' ? body.memory.error.slice(0, 128) : null,
      },
    };
  }

  private recordHostHealthObservation(
    payload: HostHealthPayload,
    auth: StatusMachineAuth,
  ): { ok: boolean; machine_id: string; observed_at: string } {
    const reporter = auth.kind === 'host' ? `host:${auth.machine_id}` : `admin:${auth.actor}`;
    const observedMs = Date.parse(payload.observed_at);
    const nowIso = new Date(this.now()).toISOString();

    this.sql.exec(
      `INSERT INTO host_health_observations (machine_id, observed_at, observed_at_ms, received_at, payload_json, reporter)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(machine_id) DO UPDATE SET
         observed_at = excluded.observed_at,
         observed_at_ms = excluded.observed_at_ms,
         received_at = excluded.received_at,
         payload_json = excluded.payload_json,
         reporter = excluded.reporter`,
      payload.machine_id,
      payload.observed_at,
      observedMs,
      nowIso,
      JSON.stringify(payload),
      reporter,
    );

    if (this.liveness) {
      if (payload.memory.status === 'warn' || payload.memory.status === 'critical') {
        this.liveness.setAlert('memory', payload.machine_id, {
          state: payload.memory.status,
          detail: `Host memory pressure on ${payload.machine_id}: ${payload.memory.percent}% used (${formatBytes(payload.memory.used_bytes)} / ${formatBytes(payload.memory.total_bytes)})`,
        });
      } else if (payload.memory.status === 'ok') {
        this.liveness.setAlert('memory', payload.machine_id, null);
      }
    }

    return {
      ok: true,
      machine_id: payload.machine_id,
      observed_at: payload.observed_at,
    };
  }

  getHostHealthStatus(
    now: number = this.now(),
    machineFilter?: string,
  ): { snapshot_at: string; machines: HostHealthSummary[] } {
    let query = 'SELECT machine_id, observed_at, observed_at_ms, payload_json FROM host_health_observations';
    const params: any[] = [];
    if (machineFilter) {
      query += ' WHERE machine_id = ?';
      params.push(machineFilter);
    }
    query += ' ORDER BY machine_id ASC';

    let rows: any[] = [];
    try {
      rows = this.sql.exec(query, ...params).toArray() as any[];
    } catch {
      return { snapshot_at: new Date(now).toISOString(), machines: [] };
    }

    const machines: HostHealthSummary[] = [];
    for (const r of rows) {
      let payload: any = {};
      try {
        payload = JSON.parse(r.payload_json);
      } catch {
        continue;
      }
      const ageMs = now - r.observed_at_ms;
      const freshness: 'fresh' | 'stale' | 'unavailable' =
        ageMs <= 180000 ? 'fresh' : ageMs <= 900000 ? 'stale' : 'unavailable';

      machines.push({
        machine_id: r.machine_id,
        observed_at: r.observed_at,
        freshness,
        load: payload.load ?? { load1: 0, load5: 0, load15: 0 },
        memory: payload.memory ?? { total_bytes: 0, available_bytes: 0, used_bytes: 0, percent: 0, status: 'unknown' },
      });
    }

    return {
      snapshot_at: new Date(now).toISOString(),
      machines,
    };
  }

  /** Condensed summary projection for StatusService (hub status / needs-attention). */
  getStatusSummary(now: number = this.now()): {
    filesystems: any[];
    host_health: HostHealthSummary[];
    needs_attention: any[];
    categories_entry: any;
  } {
    const full = this.getDiskStatus(now);
    const healthStatus = this.getHostHealthStatus(now);
    const filesystems: any[] = [];
    const hostHealth: HostHealthSummary[] = [];
    const needsAttention: any[] = [];

    let hasErrors = false;
    let anyStale = false;
    let anyUnavailable = false;
    let latestObservedMs: number | null = null;
    let latestObservedIso: string | null = null;

    for (const h of healthStatus.machines || []) {
      hostHealth.push(h);
      if (h.freshness === 'stale') anyStale = true;
      if (h.freshness === 'unavailable') anyUnavailable = true;

      const hObservedMs = Date.parse(h.observed_at);
      if (Number.isFinite(hObservedMs) && (latestObservedMs === null || hObservedMs > latestObservedMs)) {
        latestObservedMs = hObservedMs;
        latestObservedIso = h.observed_at;
      }

      if (h.memory?.status === 'warn' || h.memory?.status === 'critical') {
        needsAttention.push({
          severity: h.memory.status,
          subject: `${h.machine_id}:memory`,
          title: `Memory pressure on ${h.machine_id}`,
          detail: `Memory is at ${h.memory.percent}% (${formatBytes(h.memory.used_bytes)} / ${formatBytes(h.memory.total_bytes)})`,
          percent: h.memory.percent,
        });
      }
    }

    for (const m of full.machines || []) {
      if (m.freshness === 'stale') anyStale = true;
      if (m.freshness === 'unavailable') anyUnavailable = true;

      const mObservedMs = Date.parse(m.observed_at);
      if (Number.isFinite(mObservedMs) && (latestObservedMs === null || mObservedMs > latestObservedMs)) {
        latestObservedMs = mObservedMs;
        latestObservedIso = m.observed_at;
      }

      for (const fs of m.filesystems || []) {
        if (fs.error || fs.status === 'error') {
          hasErrors = true;
        }

        filesystems.push({
          machine_id: m.machine_id,
          mount_point: fs.mount_point,
          device_id: fs.device_id,
          is_primary: fs.is_primary,
          total_bytes: fs.total_bytes,
          used_bytes: fs.used_bytes,
          free_bytes: fs.free_bytes,
          percent: fs.percent,
          status: fs.status,
          error: fs.error ?? null,
          observed_at: m.observed_at,
          freshness: m.freshness,
          growth_per_day_bytes: fs.trend?.growth_bytes_per_day ?? null,
          growth_per_day_pct: fs.trend?.growth_pct_per_day ?? null,
          trend_state: fs.trend?.state ?? 'unknown',
          top_consumers: fs.top_consumers || [],
          top_consumers_truncated: fs.top_consumers_truncated ?? false,
        });

        if (fs.status === 'warn' || fs.status === 'critical') {
          needsAttention.push({
            severity: fs.status,
            subject: `${m.machine_id}:${fs.mount_point}`,
            title: `Disk pressure on ${m.machine_id}: ${fs.mount_point}`,
            detail: `${fs.mount_point} is at ${fs.percent}% (${formatBytes(fs.used_bytes)} / ${formatBytes(fs.total_bytes)})`,
            percent: fs.percent,
            top_consumers: fs.top_consumers || [],
          });
        } else if (fs.status === 'error' || fs.error) {
          needsAttention.push({
            severity: 'warn',
            subject: `${m.machine_id}:${fs.mount_point}`,
            title: `Disk monitor error on ${m.machine_id}: ${fs.mount_point}`,
            detail: `Filesystem statvfs check failed: ${fs.error || 'unknown_error'}`,
            percent: 0,
            top_consumers: [],
          });
        }
      }
    }

    const overallFreshness = !filesystems.length
      ? 'unknown'
      : (anyUnavailable || anyStale)
      ? 'stale'
      : 'fresh';

    const coverage = !filesystems.length
      ? 'unavailable'
      : (hasErrors || anyStale || anyUnavailable)
      ? 'partial'
      : 'complete';

    let reason = 'linux_stdlib_statvfs_and_bounded_top_consumers';
    if (!filesystems.length) {
      reason = 'no_disk_observations_recorded';
    } else if (hasErrors && (anyStale || anyUnavailable)) {
      reason = 'filesystem_errors_reported_and_host_stale';
    } else if (hasErrors) {
      reason = 'one_or_more_filesystems_reported_statvfs_errors';
    } else if (anyStale || anyUnavailable) {
      reason = 'one_or_more_hosts_stale_or_unreachable';
    }

    const state = !filesystems.length
      ? 'unknown_not_collected'
      : hasErrors
      ? 'collected_disk_monitoring_with_errors'
      : 'collected_disk_monitoring';

    return {
      filesystems,
      host_health: hostHealth,
      needs_attention: needsAttention,
      categories_entry: {
        state,
        source: 'host_disk_monitor_stdlib_statvfs',
        observed_at: latestObservedIso,
        freshness: overallFreshness,
        coverage,
        reason,
      },
    };
  }
}
