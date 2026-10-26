import { IntakeWatchdog } from './intake-watchdog';
import { RepoRegistry } from './repos';
import { IntakeService } from './intake';
import { UI_HEADERS, tokenPage, authFailure, deviceEntry, deviceReview, deviceReviewHuman, deviceResult } from './ui-pages';
import { DurableObject } from "cloudflare:workers";
import { SqlStorage } from "@cloudflare/workers-types";
import { Env } from './env';
import { SCHEMA_SQL } from './schema';
import { migrateAgentRegistry, bindAgentCredentials, credentialActor, registryAddress, AGENT_NAME } from './agent_registry';
import { JwksCache, validateIdToken, exchangeCode } from './auth/dtc';
import { createSessionCookie, getSession, clearSessionCookie, createLoginCookie, getLoginCookie, clearLoginCookie, clearLoginState, resolveCallbackUrl, isAllowedOidcOrigin, verifyCsrf, configuredAdmins, validSessionSecret, adminTokenHash, tokenAdminSubject, TOKEN_ADMIN_EMAIL, humanAdminAllowed } from './auth/session';
import { isHumanCredentialAllowed as isHumanCredentialAllowedFn, validateHumanCredentialRecord } from './auth/human-credential';

import { randomString, sha256, generateUserCode, base64url } from './auth/crypto';
import { generateTokenPair, verifyBearerScope } from './auth/token';
import { handleRegistryRoutes, handleWebSocketMessageRegistry, handleWebSocketCloseRegistry, updateHeartbeatMemoryOnly, updateHeartbeat } from './registry';
import { BusService, BUS_SCHEMA_SQL, CallerContext, generateUlid } from './bus';
import {
  formatNativeActor,
  isWorkMessageKind,
  redactClaimData,
  redactForBus,
  taskIdFromWorkKey,
  validateWorkClaimData,
} from './work_claim';
import { JobsService, JOBS_SCHEMA_SQL } from './jobs';
import { roleName } from './role_leases';
import { FleetService } from './fleet';
import { ProvisioningService } from './provisioning';
import { LivenessService, thresholdsFrom, alertText } from './liveness';
import { ChecksService, validateCheck, type ActionDeps } from './checks';
import { approvalAuthorityMatches, assignApprovalAuthority } from './host_enrollment';
import { ensureRootScheduling, getRootScheduling, isRootSchedulingEnabled, setRootScheduling } from './root_scheduling';
import { QuotaStore, dedupeAccountsByCanonical, handleQuotaRoutes } from './quota';
import { handleTbmApi } from './tbm-api';
import { tickTbm } from './tbm-controller';
import { syncStoredTbmSnapshots } from './tbm-ingestion';
import { SessionControlService } from './session_control';
import { StatusService, type StatusMachineAuth } from './status';
import { MetricsStore, handleMetricsRoutes } from './metrics';
import { MonitoringService } from './monitoring';
import { isValidMachineId } from './machine-id';
import { HostHistoryService } from './host-history';
import { HISTORY_RETENTION_MS } from './host-history';
import { TOKEN_SCHEMA_VERSION, TOKEN_SOURCES, tokenEntity, tokenPostAt, validateTokenPayload } from './token-history';
import { placementPressureSubjects } from './host-pressure';

function parseEpoch(raw: any): number | null {
  if (raw === undefined || raw === null || raw === '' || typeof raw === 'boolean') return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) return null;
  return n;
}

async function safeJson(req: Request): Promise<Record<string, any> | null> {
  if (!req.body) return null;
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
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
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const data = JSON.parse(new TextDecoder().decode(bytes));
    return data && typeof data === 'object' && !Array.isArray(data) ? data : null;
  } catch { return null; }
  finally { reader.releaseLock(); }
}

/** Only the two local consent entry points may survive bootstrap token login. */
function safeAuthReturn(value: string | null): string | null {
  if (!value || value.length > 4096 || /[\\\x00-\x20\x7f#]/.test(value)) return null;
  if (/^\/device\?code=[A-Za-z0-9-]{1,32}$/.test(value)) return value;
  if (value.startsWith('/oauth/authorize?')) return value;
  return null;
}

function htmlEscape(str: string): string {
  return String(str).replace(/[&<>'"]/g, 
    tag => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    }[tag] || tag)
  );
}

/** Agents keep exactly the existing ordinary-agent scope ceiling, even on stale pending rows. */
function validAgentPairingScopes(granted: string, requested: string): boolean {
  try {
    const g = JSON.parse(granted), r = JSON.parse(requested);
    const allowed = ['bus:read', 'bus:send', 'health:report', 'fleet:launch'];
    return Array.isArray(g) && g.length > 0 && new Set(g).size === g.length && Array.isArray(r) &&
      r.length > 0 && new Set(r).size === r.length && r.every(s => allowed.includes(s)) &&
      g.every(s => allowed.includes(s) && r.includes(s));
  } catch { return false; }
}

export class Hub extends DurableObject {
  sql: SqlStorage;
  env: Env;
  jwksCache: JwksCache = new JwksCache();
  public intakeService: IntakeService;
  public intakeWatchdog: IntakeWatchdog;
  public busService: BusService;
  public jobsService: JobsService;
  public quotaStore: QuotaStore;
  public statusService: StatusService;
  public metricsStore: MetricsStore;
  public fleetService: FleetService;
  public repoRegistry: RepoRegistry;
  public provisioningService: ProvisioningService;
  public sessionControlService: SessionControlService;
  public livenessService: LivenessService;
  public checksService: ChecksService;
  public monitoringService: MonitoringService;
  public hostHistoryService: HostHistoryService;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.env = env;
    this.sql = ctx.storage.sql;
    this.initDb();

    const txSync = (this.ctx.storage as any).transactionSync ? (fn: any) => (this.ctx.storage as any).transactionSync(fn) : undefined;
    // M12b intake assignment (narrow seam, owned by platform-intake-live):
    // task assignment creates its Hub session through the canonical
    // SessionControlService.handle with the caller's own auth, so placement
    // stays with FleetService and no session identity is invented in intake.
    // The closure resolves sessionControlService at call time (it is
    // constructed just below); intake records only the returned real id.
    this.intakeService = new IntakeService(this.sql, fn => this.ctx.storage.transactionSync(fn), {
      forgotten: tasks => {
        const items = this.intakeWatchdog.project(tasks).filter(t => t.forgotten);
        return { items, forgotten_count: items.length, observed_at: Date.now(), watchdog: this.intakeWatchdog.status() };
      },
      startSession: async (input, auth) => {
        const res = await this.sessionControlService.handle(
          new Request('https://hub.internal/api/sessions', { method: 'POST', body: JSON.stringify(input) }), auth);
        let body: any = null;
        try { body = await res.json(); } catch { body = null; }
        return { status: res.status, body };
      },
    });
    this.intakeService.init();
    this.statusService = new StatusService(this.sql, fn => this.ctx.storage.transactionSync(fn), this.intakeService);
    this.statusService.init();
    this.busService = new BusService(this.sql, txSync);
    this.fleetService = new FleetService(this.sql, fn => this.ctx.storage.transactionSync(fn));
    this.fleetService.init();
    this.repoRegistry = new RepoRegistry(this.sql, fn => this.ctx.storage.transactionSync(fn));
    this.repoRegistry.init();
    // M12e session control (narrow seam, owned by platform-session-api-r2):
    // durable start/send/capture/stop intents over the existing fleet reserve.
    // Placement stays with FleetService; no second dispatcher or consumer here.
    this.sessionControlService = new SessionControlService(this.sql, fn => this.ctx.storage.transactionSync(fn), Date.now, {
      reserve: (input, auth) => {
        const prepared = this.repoRegistry.prepareLaunch(input);
        return prepared.error ? { value: prepared.error, status: 400 } : this.fleetService.reserve(prepared.input, auth);
      },
    }, { env: this.env });
    this.sessionControlService.init();
    this.sql.exec(`CREATE INDEX IF NOT EXISTS session_control_native ON session_control_sessions(machine_id,
      CASE WHEN json_valid(binding_json) THEN CASE WHEN json_type(binding_json, '$.native_id') = 'text'
        THEN json_extract(binding_json, '$.native_id') END END)`);
    this.sql.exec('CREATE INDEX IF NOT EXISTS lifecycle_decision_latest ON session_lifecycle_decisions(session_key,created_at DESC,decision_id DESC)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS status_observation_latest ON status_observations(hub_session_id,kind,observed_at DESC,received_at DESC)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS session_control_fleet_lookup ON session_control_sessions(fleet_launch_id,machine_id)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS fleet_launch_native ON fleet_launches(machine_id,native_id)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS session_control_created ON session_control_sessions(created_at DESC,id DESC)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS session_control_actor_created ON session_control_sessions(actor,created_at DESC,id DESC)');
    this.provisioningService = new ProvisioningService(this.sql, fn => this.ctx.storage.transactionSync(fn), this.env, this.fleetService,
      (machine, caller) => this.sessionControlService.hostObservationAllowed(machine, caller));
    this.provisioningService.init();
    // M14b slice2 (narrow STATUS composition): observation writes are gated
    // by the live SessionControlService.hostObservationAllowed (existing
    // fresh host family/approval authority, owned by the cleanup worker).
    // Feature-detected: while the helper is absent the gate is undefined
    // and StatusService fails every observation write closed.
    {
      const sessionControl: any = this.sessionControlService as any;
      this.statusService.setObservationGate(typeof sessionControl?.hostObservationAllowed === 'function'
        ? (machineId: unknown, caller: any) => sessionControl.hostObservationAllowed(machineId, caller)
        : undefined);
    }
    // M9 metrics (narrow METRICS seam, owned by platform-metrics-live):
    // additive MetricsStore.init DDL only; auth via existing host scopes +
    // injected live hostObservationAllowed gate inside fresh mutation txn.
    this.metricsStore = new MetricsStore(this.sql, this.ctx, this.env);
    {
      const sessionControl: any = this.sessionControlService as any;
      this.metricsStore.setObservationGate(typeof sessionControl?.hostObservationAllowed === 'function'
        ? (machineId: unknown, caller: any) => sessionControl.hostObservationAllowed(machineId, caller)
        : undefined);
    }
    this.livenessService = new LivenessService(this.sql, thresholdsFrom(env));
    this.livenessService.init();
    this.intakeWatchdog = new IntakeWatchdog(this.sql, this.intakeService, fn => this.ctx.storage.transactionSync(fn), {
      start: async input => {
        const r = await this.sessionControlService.handle(new Request('https://hub.internal/api/sessions',
          { method: 'POST', body: JSON.stringify(input) }),
          { caller: { isAdmin: true, actor: 'hub:service:intake-watchdog', kind: 'human' }, scopes: ['admin'] });
        return { status: r.status, body: await r.json() as any };
      },
      notify: async (kind, subject, detail) => {
        if (kind === 'intake_digest' && this.sql.exec('SELECT id FROM liveness_alerts WHERE subject_kind=? AND subject=? LIMIT 1', kind, subject).toArray().length) return false;
        const alert = this.livenessService.setAlert(kind, subject, detail === null ? null : { state: 'attention', detail });
        if (alert) await this.notifyAlert(alert);
        return !!alert;
      },
    });
    this.intakeWatchdog.init();
    this.livenessService.setPressureProjection(() => placementPressureSubjects(this.sql, this.fleetService, Date.now()));
    this.checksService = new ChecksService(this.sql);
    this.checksService.init();
    this.monitoringService = new MonitoringService(
      this.sql,
      fn => this.ctx.storage.transactionSync(fn),
      this.livenessService,
      alert => this.notifyAlert(alert),
      this.intakeService,
    );
    this.monitoringService.init();
    this.monitoringService.setResourceProjection(machine => this.fleetService.hostResources(machine)[0]);
    this.hostHistoryService = new HostHistoryService(this.sql, this.fleetService);
    this.hostHistoryService.init();
    this.statusService.setMonitoringService(this.monitoringService);
    this.statusService.initLifecycleCache();

    const busForJobs = Object.create(this.busService);
    const runInTxn = (this.ctx.storage as any).transactionSync ? (fn: any) => (this.ctx.storage as any).transactionSync(fn) : (fn: any) => fn();
    const sendRoot = (caller: any, opts: any) => {
      return runInTxn(() => {
        opts = { ...opts, toActor: this.busService.canonicalActor(opts.toActor) };
        const now = opts.now ?? Date.now();
        if (new TextEncoder().encode(opts.body).length > 16384) {
          throw new Error("body_too_large");
        }
        if (opts.toActor === 'role:root') {
          throw new Error("unknown_recipient");
        }
        if (!this.busService.validateRecipient(opts.toActor, now)) {
          throw new Error("unknown_recipient");
        }
        const validKinds = new Set(['note', 'handoff', 'reply', 'status', 'claim', 'release']);
        if (!opts.kind || !validKinds.has(opts.kind)) {
          throw new Error("invalid_kind");
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
        let threadId = opts.threadId;
        if (opts.replyTo) {
          const msg = this.sql.exec('SELECT thread_id, from_actor, to_actor FROM messages WHERE id = ?', opts.replyTo).toArray()[0] as any;
          if (!msg) throw new Error("invalid_reply_to");
          if (msg.from_actor !== opts.toActor && msg.to_actor !== opts.toActor) throw new Error("invalid_reply_to");
          if (msg.from_actor !== opts.senderActor && msg.to_actor !== opts.senderActor && msg.from_actor !== 'root' && msg.to_actor !== 'root') throw new Error("invalid_reply_to");
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
    };
    busForJobs.send = (caller: any, opts: any) => {
      if (opts.senderActor === 'role:root') {
        return sendRoot(caller, opts);
      }
      return this.busService.send(caller, opts);
    };
    busForJobs.sendRootMessage = (caller: any, opts: any) => {
      return sendRoot(caller, opts);
    };

    this.jobsService = new JobsService(this.sql, this.ctx, {
      busService: busForJobs,
      enableAlarms: true,
      alarmDeadline: () => this.hostHistoryService.nextSampleAt(),
      env: this.env,
    });
    this.jobsService.setBusService(busForJobs);
    
    // Restore hibernated websockets
    const websockets = this.ctx.getWebSockets();
    for (const ws of websockets) {
      try {
        const attachment: any = ws.deserializeAttachment();
        if (attachment && attachment.machineId) {
          updateHeartbeatMemoryOnly(attachment.machineId);
        }
      } catch (e) { }
    }

    this.quotaStore = new QuotaStore(this.sql, this.ctx, this.env);
  }

  private ensureColumns(table: string, columns: { [col: string]: string }) {
    const existing = new Set<string>();
    const rows = this.sql.exec(`PRAGMA table_info(${table})`).toArray() as any[];
    for (const row of rows) {
      if (row.name) existing.add(row.name.toLowerCase());
    }
    if (existing.size === 0) return;
    for (const [col, colType] of Object.entries(columns)) {
      if (!existing.has(col.toLowerCase())) {
        this.sql.exec(`ALTER TABLE ${table} ADD COLUMN ${col} ${colType}`);
      }
    }
  }

  public initDb() {
    this.ctx.storage.transactionSync(() => {
    // Remove the global tag PK before creating/using the current schema.
    if (this.sql.exec('PRAGMA table_info(agents)').toArray().length) {
      migrateAgentRegistry(this.sql, fn => this.ctx.storage.transactionSync(fn));
    }

    // 2. Execute SCHEMA_SQL
    for (const statement of SCHEMA_SQL.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) {
        this.sql.exec(trimmed);
      }
    }

    // 3. Ensure all M4 columns on existing tables (SPEC §6 and M3 migrations)
    this.ensureColumns('agents', {
      project: 'TEXT',
      role: 'TEXT',
      machine_id: 'TEXT',
      registered_at: 'TEXT',
      team_id: 'TEXT',
      imported_from: 'TEXT',
      engine: 'TEXT',
      session_id: 'TEXT',
      workspace: 'TEXT',
      state: 'TEXT',
      health_state: 'TEXT',
      health_detail: 'JSON',
      detail: 'TEXT',
      lasthealth: 'TEXT',
      health_at: 'TEXT',
      last_seen_at: 'TEXT',
      last_seen: 'TEXT',
      updated_at: 'TEXT',
      pid: 'INTEGER',
      started_at: 'TEXT'
    });

    migrateAgentRegistry(this.sql, fn => this.ctx.storage.transactionSync(fn));
    bindAgentCredentials(this.sql, fn => this.ctx.storage.transactionSync(fn));

    this.ensureColumns('machines', {
      name: 'TEXT',
      hostname: 'TEXT',
      network: "TEXT DEFAULT 'home'",
      os: "TEXT DEFAULT ''",
      arch: "TEXT DEFAULT ''",
      roles: "JSON DEFAULT '[]'",
      capabilities: "JSON DEFAULT '{}'",
      credential_id: 'TEXT',
      registered_at: 'TEXT',
      last_checkin_at: 'TEXT',
      last_heartbeat: 'JSON',
      drained: 'INTEGER DEFAULT 0',
      notes: 'TEXT',
      approval_seq: 'INTEGER'
    });

    // R5 R2: server-owned enrollment authority migration.
    this.ensureColumns('device_pairings', { approved_seq: 'INTEGER' });
    this.ensureColumns('credentials', { approval_seq: 'INTEGER' });

    this.ensureColumns('jobs', {
      deadline_at: 'TEXT',
      actor: 'TEXT',
      offer_host_id: 'TEXT',
      offer_machine_id: 'TEXT',
      offer_expires_at: 'TEXT',
      offer_attempts: "JSON DEFAULT '[]'",
      claim_token_hash: 'TEXT',
      holder_host_id: 'TEXT',
      holder_machine_id: 'TEXT',
      started_at: 'TEXT',
      first_action_at: 'TEXT',
      evidence_digest: 'TEXT',
      native_session: 'TEXT',
      launcher_task_id: 'TEXT',
      lease_epoch: 'INTEGER',
      finished_at: 'TEXT',
      outcome_summary: 'TEXT',
      outcome_evidence: 'JSON'
    });

    this.ensureColumns('leases', {
      holder_job_id: 'TEXT',
      holder_machine_id: 'TEXT',
      holder_host_id: 'TEXT',
      holder_actor: 'TEXT',
      epoch: 'INTEGER DEFAULT 0',
      acquired_at: 'TEXT',
      renewed_at: 'TEXT',
      expires_at: 'TEXT',
      released_at: 'TEXT',
      release_reason: 'TEXT'
    });

    this.ensureColumns('audit', {
      actor: 'TEXT',
      action: 'TEXT',
      resource: 'TEXT',
      target: 'TEXT',
      detail: 'TEXT',
      at: 'TEXT'
    });

    // Work-claim protocol (Phase 1): host-attested native aplexer rows share
    // the existing claims table so activity/admission see them. Nullable so
    // legacy Hub rows keep working; NULL mode/source means a Hub-native edit.
    this.ensureColumns('claims', {
      work_key: 'TEXT',
      claim_mode: 'TEXT',
      claim_source: 'TEXT',
      worktree: 'TEXT',
      session_state: 'TEXT',
      native_claim_id: 'TEXT'
    });

    // If audit was upgraded from SPEC §6 where ts existed instead of at:
    const auditInfo = this.sql.exec("PRAGMA table_info(audit)").toArray() as any[];
    const hasAt = auditInfo.some(c => c.name.toLowerCase() === 'at');
    const hasTs = auditInfo.some(c => c.name.toLowerCase() === 'ts');
    if (hasAt && hasTs) {
      this.sql.exec("UPDATE audit SET at = ts WHERE at IS NULL AND ts IS NOT NULL");
    }

    this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES ('root', 0)");
    ensureRootScheduling(this.sql);
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    
    try {
      if (path === '/api/intake' || path.startsWith('/api/intake/')) {
        return this.intakeService.handle(request, async () => {
          if (request.headers.has('Authorization') && request.headers.has('Cookie')) return { error: Response.json({ error: 'mixed_authority' }, { status: 403 }) };
          const auth = await this.authenticateBusRequest(request);
          // The existing operator token can administer intake through the public CLI.
          // Cookie requests stay on the existing session/CSRF path; no agent scope changes.
          if (auth.error && !request.headers.has('Cookie') && validSessionSecret(this.env)) {
            const header = request.headers.get('Authorization') || '';
            const token = /^Bearer ([^\s]{1,4096})$/.exec(header)?.[1];
            if (token) {
              const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
              const given = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
              // A failed registered credential never upgrades to operator authority.
              if (this.sql.exec('SELECT id FROM credentials WHERE token_hash=?', base64url(digest)).toArray().length) return auth;
              const configured = adminTokenHash(this.env);
              if (configured) {
                let difference = 0;
                for (let i = 0; i < configured.length; i++) difference |= given.charCodeAt(i) ^ configured.charCodeAt(i);
                if (difference === 0) return { caller: { kind: 'human' as const, isAdmin: true,
                  actor: 'human:' + tokenAdminSubject(configured) }, scopes: ['admin'] };
              }
            }
          }
          if (auth.caller?.kind === 'human' && !auth.caller.tokenId) {
            const session = await getSession(this.env, request);
            if (!session) return { error: Response.json({ error: 'unauthorized' }, { status: 401 }) };
            auth.caller.actor = 'human:' + session.sub;
          }
          return auth;
        });
      }
      if (path === '/api/status/observations') {
        const resolved = await this.statusObservationAuth(request);
        if ((resolved as any).error) return (resolved as any).error;
        return this.statusService.handleObservation(request, (resolved as any).auth);
      }
      if (path === '/api/monitoring/history') {
        const auth = await this.authenticateBusRequest(request);
        const response = this.hostHistoryService.handle(request, auth);
        if (response.ok) {
          const alarm = await this.ctx.storage.getAlarm();
          if (alarm === null || alarm > Date.now() + 60000) await this.ctx.storage.setAlarm(Date.now() + 60000);
        }
        return response;
      }
      if (path === '/api/monitoring/tokens') {
        // Native token usage hours (usage-history intake, AC3). Hosts POST
        // measured complete hours; reads go through /api/monitoring/history
        // (kind 'tokens'). Only host credentials (own machine) or admins.
        if (request.method === 'GET') {
          const auth = await this.authenticateBusRequest(request);
          if ((auth as any).error) return (auth as any).error;
          if (!(auth as any).caller) return Response.json({ error: 'unauthorized' }, { status: 401 });
          return Response.json({ schema_version: TOKEN_SCHEMA_VERSION, sources: TOKEN_SOURCES }, { headers: { 'Cache-Control': 'private, no-store' } });
        }
        if (request.method !== 'POST') return Response.json({ error: 'method_not_allowed' }, { status: 405 });
        const auth = await this.authenticateBusRequest(request);
        if ((auth as any).error) return (auth as any).error;
        const caller = (auth as any).caller;
        const machineId = caller?.machineId;
        if (!caller || (caller.kind !== 'host' && !caller.isAdmin) || (!caller.isAdmin && !machineId)) {
          return Response.json({ error: 'forbidden', reason: 'host_credential_required' }, { status: 403 });
        }
        const raw = await safeJson(request);
        if (!raw) return Response.json({ error: 'invalid_body' }, { status: 400 });
        const validated = validateTokenPayload(raw, Date.now(), HISTORY_RETENTION_MS);
        if (!validated.ok || !validated.hours) {
          return Response.json({ error: validated.error, ...(validated.reason ? { reason: validated.reason } : {}) }, { status: validated.status });
        }
        const poster = caller.isAdmin && typeof raw.machine_id === 'string' && isValidMachineId(raw.machine_id) ? raw.machine_id : machineId;
        const nowMs = Date.now();
        for (const hour of validated.hours) {
          const entity = tokenEntity(hour);
          const at = tokenPostAt(this.sql, entity, hour.hour_ms, hour.input_tokens, hour.output_tokens, nowMs, HISTORY_RETENTION_MS);
          this.hostHistoryService.record('tokens', entity, String(poster || ''), at,
            { input_tokens: hour.input_tokens, output_tokens: hour.output_tokens }, 'native_token_usage');
        }
        this.hostHistoryService.flush();
        await this.sampleHostHistory();
        return Response.json({ ok: true, hours: validated.hours.length }, { headers: { 'Cache-Control': 'private, no-store' } });
      }
      if (path === '/api/monitoring/disk') {
        if (request.method === 'POST') {
          const resolved = await this.statusObservationAuth(request);
          if ((resolved as any).error) return (resolved as any).error;
          const response = await this.monitoringService.handleObservation(request, (resolved as any).auth);
          if (response.ok) await this.sampleHostHistory();
          return response;
        }
        if (request.method === 'GET') {
          const auth = await this.authenticateBusRequest(request);
          if (auth.caller?.isAdmin) {
            const session = await getSession(this.env, request);
            if (session) auth.caller.actor = 'human:' + session.sub;
          }
          return this.monitoringService.handleGet(request, auth);
        }
        return Response.json({ error: 'method_not_allowed' }, { status: 405 });
      }
      if (path === '/api/monitoring/health' || path === '/api/monitoring/host') {
        if (request.method === 'POST') {
          const resolved = await this.statusObservationAuth(request);
          if ((resolved as any).error) return (resolved as any).error;
          const response = await this.monitoringService.handleHostHealthObservation(request, (resolved as any).auth);
          if (response.ok) await this.sampleHostHistory();
          return response;
        }
        if (request.method === 'GET') {
          const auth = await this.authenticateBusRequest(request);
          if (auth.caller?.isAdmin) {
            const session = await getSession(this.env, request);
            if (session) auth.caller.actor = 'human:' + session.sub;
          }
          return this.monitoringService.handleHostHealthGet(request, auth);
        }
        return Response.json({ error: 'method_not_allowed' }, { status: 405 });
      }
      if (path === '/api/status') {
        // M14d: GET stays the readonly paginated projection; POST projects
        // the COMPLETE bounded summary and advances the caller's own
        // baseline in the same transaction (one call total). Cookie POSTs
        // pass the existing session CSRF via authenticateBusRequest.
        const authenticate = async () => {
          if (request.headers.has('Authorization') && request.headers.has('Cookie')) return { error: Response.json({ error: 'mixed_authority' }, { status: 403 }) };
          const auth = await this.authenticateBusRequest(request);
          if (auth.error && !request.headers.has('Cookie') && validSessionSecret(this.env)) {
            const header = request.headers.get('Authorization') || '';
            const token = /^Bearer ([^\s]{1,4096})$/.exec(header)?.[1];
            if (token) {
              const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
              const given = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
              if (this.sql.exec('SELECT id FROM credentials WHERE token_hash=?', base64url(digest)).toArray().length) return auth;
              const configured = adminTokenHash(this.env);
              if (configured) {
                let difference = 0;
                for (let i = 0; i < configured.length; i++) difference |= given.charCodeAt(i) ^ configured.charCodeAt(i);
                if (difference === 0) return { caller: { kind: 'human' as const, isAdmin: true,
                  actor: 'human:' + tokenAdminSubject(configured) }, scopes: ['admin'] };
              }
            }
          }
          if (auth.caller?.kind === 'human' && !auth.caller.tokenId) {
            const session = await getSession(this.env, request);
            if (!session) return { error: Response.json({ error: 'unauthorized' }, { status: 401 }) };
            auth.caller.actor = 'human:' + session.sub;
          }
          return auth;
        };
        if (request.method === 'POST') return this.statusService.handlePost(request, authenticate);
        return this.statusService.handle(request, authenticate);
      }
      if (path === '/auth/token') return await this.tokenSignIn(request);
      if (path === '/api/host/poll') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const cred = await verifyBearerScope(this.sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
        if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

        const body = await safeJson(request);
        if (!body || body.type !== 'heartbeat') return Response.json({ error: 'malformed_json' }, { status: 400 });

        updateHeartbeat(cred.machine_id, body, this.sql);

        const nowIso = new Date().toISOString();
        const rows = this.sql.exec(
          `SELECT id, kind, payload, status, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, created_at, actor, role_name, role_generation
           FROM jobs
           WHERE status = 'offered'
             AND (offer_host_id = ? OR offer_machine_id = ?)
             AND (offer_expires_at IS NULL OR offer_expires_at > ?)
           ORDER BY created_at DESC`,
          cred.machine_id, cred.machine_id, nowIso
        ).toArray() as any[];
        // Root scheduling flag: root-run offers are not listed while disabled.
        const visibleRows = isRootSchedulingEnabled(this.sql) ? rows : rows.filter((r: any) => r.kind !== 'root-run');

        const offers = visibleRows.map((r: any) => ({
          id: r.id,
          job_id: r.id,
          kind: r.kind,
          payload: typeof r.payload === 'string' ? JSON.parse(r.payload || '{}') : r.payload,
          status: r.status,
          deadline_at: r.deadline_at,
          offer_expires_at: r.offer_expires_at,
          created_at: r.created_at,
          actor: r.actor,
          ...(r.role_name ? { role_binding: this.jobsService.roleJobBinding(r) } : {})
        }));

        return Response.json({ status: 'ok', server_time: nowIso, offers });
      }

      if (path === '/api/agents' || path.startsWith('/api/agents/')) return this.handleAgentRegistry(request, path);
      if (path === '/api/heartbeat' || path === '/api/liveness' || path === '/api/checks' || path.startsWith('/api/checks/')) {
        return this.handleWatchRoutes(request, path);
      }
      if (path === '/api/engines' || path.startsWith('/api/engines/')) {
        return this.provisioningService.handle(request, await this.authenticateBusRequest(request));
      }
      if (path === '/api/repos' || path.startsWith('/api/repos/')) {
        return this.repoRegistry.handle(request, await this.sessionControlAuth(request));
      }
      if (path === '/api/fleet' || path.startsWith('/api/fleet/')) {
        const auth = await this.sessionControlAuth(request);
        if (auth.caller?.isAdmin) {
          const session = await getSession(this.env, request);
          if (session) auth.caller.actor = 'human:' + session.sub;
        }
        return this.fleetService.handle(request, auth);
      }
      // M12e session control (narrow seam, owned by platform-session-api-r2).
      // Auth mirrors the intake mount exactly: bus credentials first, narrow
      // operator-token fallback for the public CLI, cookie sessions on the
      // existing session/CSRF path. No agent scope changes.
      if (path === '/api/sessions' || path.startsWith('/api/sessions/') ||
          path === '/api/session-commands' || path.startsWith('/api/session-commands/')) {
        return this.sessionControlService.handle(request, await this.sessionControlAuth(request));
      }
      const registryResponse = await handleRegistryRoutes(request, path, this.sql, this.ctx, this.env);
      if (registryResponse) return registryResponse;
      const busResponse = await this.handleBusRoutes(request, path);
      if (busResponse) return busResponse;
      const jobsResponse = await this.handleJobsRoutes(request, path, url);
      if (jobsResponse) return jobsResponse;
      if (['/api/quota/pacing', '/api/quota/banked', '/api/quota/kill-switch', '/api/quota/rewards'].includes(path)) {
        const auth = await this.sessionControlAuth(request);
        if (auth.error) return auth.error;
        if (!auth.caller?.isAdmin) return Response.json({ error: 'forbidden' }, { status: 403 });
        return handleTbmApi(request, this.sql);
      }
      const quotaResponse = await handleQuotaRoutes(request, path, this.quotaStore, url, (req) =>
        this.authenticateBusRequest(req)
      );
      if (quotaResponse) return quotaResponse;
      // M9 metrics (narrow METRICS seam only): route/service/auth injection.
      if (path === '/api/metrics') {
        const metricsResponse = await handleMetricsRoutes(request, path, this.metricsStore, url);
        if (metricsResponse) return metricsResponse;
      }
      if (path === '/api/healthz') {
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        return Response.json({ status: 'ok', component: 'hub-do' });
      }

      if (path === '/auth/login') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        // OIDC only works on origins registered as callbacks; elsewhere use token sign-in.
        const forwardedHost = request.headers.get('X-Forwarded-Host');
        const originAllowed = isAllowedOidcOrigin(this.env, url.toString(), forwardedHost);
        if (validSessionSecret(this.env) && adminTokenHash(this.env) && (configuredAdmins(this.env).length === 0 || !originAllowed)) {
          const next = url.searchParams.get('return_to');
          const location = safeAuthReturn(next) ? '/auth/token?return_to=' + encodeURIComponent(next!) : '/auth/token';
          return new Response(null, { status: 303, headers: { Location: location, 'Cache-Control': 'private, no-store' } });
        }
        if (!validSessionSecret(this.env) || configuredAdmins(this.env).length === 0) return authFailure(request, 'authentication_not_configured', 503, {}, 'authentication_not_configured');
        const state = randomString(32);
        const nonce = randomString(32);
        const verifier = randomString(43);
        const codeChallenge = await sha256(verifier);
        this.sql.exec('DELETE FROM login_states WHERE expires_at <= ?', Math.floor(Date.now() / 1000));
        this.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', await sha256(state), Math.floor(Date.now() / 1000) + 600);
        
        const returnTo = url.searchParams.get('return_to');
        const returnToPath = returnTo && returnTo.startsWith('/') && !returnTo.startsWith('//') && !returnTo.includes('\\') ? returnTo : undefined;
        const callbackUrl = resolveCallbackUrl(this.env, url.toString(), forwardedHost);
        
        const loginCookie = await createLoginCookie(this.env, {
          state, nonce, verifier, exp: Math.floor(Date.now() / 1000) + 600, returnTo: returnToPath, callbackUrl
        }, request.headers.get('Cookie'));

        const authUrl = new URL(`${this.env.AUTH_BASE_URL}/oauth2/authorize`);
        authUrl.search = new URLSearchParams({
          client_id: this.env.AUTH_CLIENT_ID,
          redirect_uri: callbackUrl,
          response_type: 'code',
          scope: 'openid email profile',
          state,
          nonce,
          code_challenge: codeChallenge,
          code_challenge_method: 'S256',
          identity_provider: 'Google',
          prompt: 'select_account'
        }).toString();

        return new Response(null, {
          status: 302,
          headers: {
            'Location': authUrl.toString(),
            'Set-Cookie': loginCookie
          }
        });
      }

      if (path === '/auth/callback') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const query = url.searchParams;
        const oauthError = query.get('error');
        const code = query.get('code');
        const state = query.get('state');

        const loginData = state ? await getLoginCookie(this.env, request, state) : null;

        // Check if provider returned an OAuth error directly
        if (oauthError) {
          const cleanErr = oauthError.replace(/[^a-z0-9_-]/gi, '');
          const clearCookie = state ? await clearLoginState(this.env, request, state) : clearLoginCookie();
          // Classify only the known guard rejection. Never render arbitrary provider text.
          const workspaceDenied = oauthError === 'invalid_request' &&
            (query.get('error_description') || '').includes('Sign-in is restricted to a verified account in the approved Workspace domain');
          return authFailure(request, 'provider_error', 400, { 'Set-Cookie': clearCookie }, workspaceDenied ? 'workspace_account_required' : `oauth_${cleanErr}`, loginData?.returnTo);
        }

        const existingSession = await getSession(this.env, request);

        if (!code || !state) {
          if (existingSession) {
            return new Response(null, { status: 302, headers: { 'Location': '/', 'Cache-Control': 'private, no-store' } });
          }
          const cat = !code && !state ? 'missing_code_and_state' : !code ? 'missing_code' : 'missing_state';
          return authFailure(request, 'Missing code or state', 400, {}, cat, loginData?.returnTo);
        }

        if (!loginData || loginData.state !== state) {
          if (existingSession) {
            return new Response(null, { status: 302, headers: { 'Location': '/', 'Cache-Control': 'private, no-store' } });
          }
          const hasAny = await getLoginCookie(this.env, request);
          const cat = hasAny ? 'state_mismatch' : 'missing_cookie';
          return authFailure(request, 'Invalid state', 400, {}, cat, loginData?.returnTo);
        }

        const stateHash = await sha256(state);
        const stateRows = this.sql.exec('SELECT consumed, expires_at FROM login_states WHERE state_hash = ?', stateHash).toArray() as any[];
        const stateRow = stateRows[0];
        const consume = this.sql.exec('UPDATE login_states SET consumed = 1 WHERE state_hash = ? AND consumed = 0 AND expires_at > ?', stateHash, Math.floor(Date.now() / 1000));
        
        if (consume.rowsWritten !== 1) {
          // Check if caller already has a valid authenticated session (valid sess refresh/replay recovery)
          const existingSession = await getSession(this.env, request);
          if (existingSession) {
            return new Response(null, {
              status: 302,
              headers: {
                'Location': loginData.returnTo || '/',
                'Cache-Control': 'private, no-store'
              }
            });
          }
          const clearCookie = await clearLoginState(this.env, request, state);
          let cat = 'invalid_state';
          if (stateRow) {
            if (stateRow.consumed === 1) cat = 'state_already_consumed';
            else if (stateRow.expires_at <= Math.floor(Date.now() / 1000)) cat = 'state_expired';
          } else {
            cat = 'state_not_found';
          }
          return authFailure(request, 'invalid_state', 400, { 'Set-Cookie': clearCookie }, cat, loginData.returnTo);
        }

        const clearCookie = await clearLoginState(this.env, request, state);
        let claims: { sub: string; email: string; email_verified: boolean };
        try {
          const redirectUri = loginData.callbackUrl || resolveCallbackUrl(this.env, url.toString());
          const idToken = await exchangeCode(this.env, code, loginData.verifier, redirectUri);
          claims = await validateIdToken(this.env, idToken, loginData.nonce, this.jwksCache);
        } catch (err: any) {
          const rawMsg = String(err?.message || '');
          const cat = /^[a-z0-9_:-]+$/i.test(rawMsg) ? rawMsg : 'token_validation_failed';
          return authFailure(request, 'invalid_identity_token', 400, { 'Set-Cookie': clearCookie }, cat, loginData.returnTo);
        }
        if (!configuredAdmins(this.env).includes(claims.email.toLowerCase())) {
          return authFailure(request, 'forbidden', 403, { 'Set-Cookie': clearCookie }, 'forbidden_not_admin', loginData.returnTo);
        }

        const now = new Date().toISOString();
        const existingUser = this.sql.exec('SELECT * FROM users WHERE sub = ?', claims.sub).toArray()[0];
        if (!existingUser) {
          this.sql.exec('INSERT INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)',
            claims.sub, claims.email, 1, now, now);
        } else {
          this.sql.exec('UPDATE users SET last_seen_at = ? WHERE sub = ?', now, claims.sub);
        }

        const csrfToken = randomString(32);
        const sessionCookie = await createSessionCookie(this.env, {
          sub: claims.sub,
          email: claims.email,
          isAdmin: true,
          csrfToken,
          exp: Math.floor(Date.now() / 1000) + 12 * 3600
        });

        const headers = new Headers();
        headers.append('Set-Cookie', sessionCookie);
        headers.append('Set-Cookie', clearCookie);
        headers.append('Location', loginData.returnTo || '/');

        return new Response(null, { status: 302, headers });
      }

      if (path === '/auth/me') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ authenticated: false }, { status: 401 });
        return Response.json({ authenticated: true, email: session.email, csrf_token: session.csrfToken });
      }

      if (path === '/api/auth/me') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const authHeader = request.headers.get('Authorization');
        if (!authHeader) return Response.json({ error: 'unauthorized' }, { status: 401 });
        if (request.headers.has('Cookie')) return Response.json({ error: 'mixed_authority' }, { status: 403 });
        const token = /^Bearer ([^\s]{1,4096})$/.exec(authHeader)?.[1];
        if (!token) return Response.json({ error: 'unauthorized' }, { status: 401 });
        const tokenHash = await sha256(token);
        const cred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL', tokenHash).toArray()[0] as any;
        if (!cred) return Response.json({ error: 'unauthorized' }, { status: 401 });
        const verified = validateHumanCredentialRecord(this.sql, this.env, cred, Date.now());
        if (!verified) return Response.json({ error: 'unauthorized' }, { status: 401 });
        return Response.json({ kind: 'human', actor: verified.actor, email: verified.email, is_admin: true, expires_at: verified.expiresAt });
      }

      if (path === '/api/auth/logout') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (body && Object.keys(body).length !== 0) return Response.json({ error: 'invalid_request' }, { status: 400 });
        if (request.headers.has('Cookie')) return Response.json({ error: 'mixed_authority' }, { status: 403 });
        const authHeader = request.headers.get('Authorization');
        if (!authHeader) return Response.json({ error: 'unauthorized' }, { status: 401 });
        const token = /^Bearer ([^\s]{1,4096})$/.exec(authHeader)?.[1];
        if (!token) return Response.json({ error: 'unauthorized' }, { status: 401 });
        const tokenHash = await sha256(token);
        const cred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ?', tokenHash).toArray()[0] as any;
        if (!cred || cred.kind !== 'human') return Response.json({ error: 'unauthorized' }, { status: 401 });
        // Idempotent: revoking an already-revoked family still succeeds.
        // Only the caller's own human family is touched; host/agent untouched.
        this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', new Date().toISOString(), cred.family_id);
        return Response.json({ status: 'revoked' });
      }

      if (path === '/auth/logout') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const session = await getSession(this.env, request);
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });
        const headers = new Headers();
        headers.append('Set-Cookie', clearSessionCookie());
        const logoutUrl = new URL(`${this.env.AUTH_BASE_URL}/logout`);
        logoutUrl.search = new URLSearchParams({
          client_id: this.env.AUTH_CLIENT_ID,
          logout_uri: url.origin + '/'
        }).toString();
        headers.append('Location', logoutUrl.toString());
        return new Response(null, { status: 302, headers });
      }

      if (path === '/api/device/code') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });
        // Human/operator device mode (CLI `hub auth login`): POST {kind:"human",label}.
        // Identity, email, actor, scopes are never taken from the request; the
        // approving allowlisted admin session binds them at approval time.
        if ((body as any).kind === 'human') {
          if (body.machine_id !== undefined) return Response.json({ error: 'machine_assignment_requires_admin_approval' }, { status: 400 });
          const rawLabel = (body as any).label;
          let label = 'human CLI';
          if (rawLabel !== undefined) {
            if (typeof rawLabel !== 'string' || !rawLabel.trim() || rawLabel.length > 128) {
              return Response.json({ error: 'invalid_request' }, { status: 400 });
            }
            label = rawLabel.trim();
          }
          const deviceCode = randomString(32);
          const deviceCodeHash = await sha256(deviceCode);
          const userCode = generateUserCode();
          let ipCountry: string | null = null;
          const cfCountry = (request as any).cf?.country;
          if (typeof cfCountry === 'string' && /^[A-Z]{2}$/.test(cfCountry)) {
            ipCountry = cfCountry;
          } else if (cfCountry !== undefined && cfCountry !== null) {
            ipCountry = 'Unknown';
          }
          const now = new Date();
          const expiresAt = new Date(now.getTime() + 600 * 1000);
          const pastForFirstPoll = new Date(now.getTime() - 60000);
          this.sql.exec(`
            INSERT INTO device_pairings
            (device_code_hash, user_code, client_kind, name, hostname, ip_country, actor, requested_scopes, status, interval_s, last_poll_at, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          `, deviceCodeHash, userCode, 'human', label, '', ipCountry, null, JSON.stringify(['admin']), 'pending', 5, pastForFirstPoll.toISOString(), now.toISOString(), expiresAt.toISOString());
          return Response.json({
            device_code: deviceCode,
            user_code: userCode,
            verification_uri: url.origin + '/device',
            verification_uri_complete: url.origin + '/device?code=' + encodeURIComponent(userCode),
            expires_in: 600,
            interval: 5
          });
        }
        if (body.machine_id !== undefined) return Response.json({ error: 'machine_assignment_requires_admin_approval' }, { status: 400 });
        if (body.client_kind !== 'host' && body.client_kind !== 'agent') {
          return Response.json({ error: 'invalid_client_kind' }, { status: 400 });
        }
        if (!body.name || typeof body.name !== 'string' || !Array.isArray(body.requested_scopes) || body.requested_scopes.length === 0) {
          return Response.json({ error: 'Invalid payload' }, { status: 400 });
        }
        
        const allowedScopes = ['host', 'bus:send', 'bus:read', 'health:report', 'jobs:create', 'lease:root', 'fleet:launch'];
        for (const scope of body.requested_scopes) {
          if (!allowedScopes.includes(scope)) {
            return Response.json({ error: 'invalid_scope' }, { status: 400 });
          }
        }
        if (body.client_kind === 'host' && body.requested_scopes.includes('fleet:launch')) return Response.json({ error: 'invalid_scope' }, { status: 400 });
        
        if (body.requested_scopes.includes('lease:root') || body.requested_scopes.includes('jobs:create') || (body.client_kind === 'agent' && body.requested_scopes.includes('host'))) return Response.json({ error: 'invalid_scope' }, { status: 400 });
        
        if (body.client_kind === 'agent') {
          const proposedActor = body.actor ?? body.name;
          if (typeof proposedActor !== 'string' || !registryAddress(proposedActor)) {
            return Response.json({ error: 'invalid_request', error_description: 'Agent requires an actor' }, { status: 400 });
          }
          if (proposedActor === 'root' || proposedActor === 'principal' || proposedActor.startsWith('role:')) {
            return Response.json({ error: 'invalid_request', error_description: 'Privileged actor names not allowed' }, { status: 400 });
          }
        }

        if (body.name.length > 128 || !body.name.trim() || (body.hostname !== undefined && (typeof body.hostname !== 'string' || body.hostname.length > 255)) || body.requested_scopes.length > 6 || new Set(body.requested_scopes).size !== body.requested_scopes.length) return Response.json({ error: 'invalid_request' }, { status: 400 });
        const hostname = typeof body.hostname === 'string' ? body.hostname : '';
        const actor = body.client_kind === 'agent' ? (body.actor ?? body.name) : null;

        const deviceCode = randomString(32);
        const deviceCodeHash = await sha256(deviceCode);
        const userCode = generateUserCode();
        
        let ipCountry: string | null = null;
        const cfCountry = (request as any).cf?.country;
        if (typeof cfCountry === 'string' && /^[A-Z]{2}$/.test(cfCountry)) {
          ipCountry = cfCountry;
        } else if (cfCountry !== undefined && cfCountry !== null) {
          ipCountry = 'Unknown';
        }

        const now = new Date();
        const expiresAt = new Date(now.getTime() + 600 * 1000);
        
        const pastForFirstPoll = new Date(now.getTime() - 60000);
        this.sql.exec(`
          INSERT INTO device_pairings
          (device_code_hash, user_code, client_kind, name, hostname, ip_country, actor, requested_scopes, status, interval_s, last_poll_at, created_at, expires_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        `, deviceCodeHash, userCode, body.client_kind, body.name, hostname, ipCountry, actor, JSON.stringify(body.requested_scopes), 'pending', 5, pastForFirstPoll.toISOString(), now.toISOString(), expiresAt.toISOString());

        return Response.json({
          device_code: deviceCode,
          user_code: userCode,
          verification_uri: url.origin + '/device',
          verification_uri_complete: url.origin + '/device?code=' + encodeURIComponent(userCode),
          expires_in: 600,
          interval: 5
        });
      }

      if (path === '/api/device/token') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body || body.grant_type !== 'urn:ietf:params:oauth:grant-type:device_code' || typeof body.device_code !== 'string' || !body.device_code || body.device_code.length > 256) {
          return Response.json({ error: 'unsupported_grant_type' }, { status: 400 });
        }

        const deviceCodeHash = await sha256(body.device_code);
        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE device_code_hash = ?', deviceCodeHash).toArray()[0] as any;
        if (!pairing) return Response.json({ error: 'invalid_grant' }, { status: 400 });

        const now = new Date();
        const expiresAt = new Date(pairing.expires_at);
        if ((!Number.isFinite(expiresAt.getTime()) || now >= expiresAt) && pairing.status !== 'consumed' && pairing.status !== 'denied') {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE device_code_hash = ?', 'expired', deviceCodeHash);
          return Response.json({ error: 'expired_token' }, { status: 400 });
        }

        if (pairing.status === 'pending') {
          const lastPollAt = new Date(pairing.last_poll_at);
          const diffS = (now.getTime() - lastPollAt.getTime()) / 1000;
          if (diffS < pairing.interval_s) {
            const newInterval = pairing.interval_s + 5;
            this.sql.exec('UPDATE device_pairings SET interval_s = ?, last_poll_at = ? WHERE device_code_hash = ?', newInterval, now.toISOString(), deviceCodeHash);
            return Response.json({ error: 'slow_down' }, { status: 400 });
          }
          this.sql.exec('UPDATE device_pairings SET last_poll_at = ? WHERE device_code_hash = ?', now.toISOString(), deviceCodeHash);
          return Response.json({ error: 'authorization_pending' }, { status: 400 });
        }
        
        if (pairing.status === 'denied') return Response.json({ error: 'access_denied' }, { status: 400 });
        if (pairing.status === 'expired' || pairing.status === 'consumed') return Response.json({ error: 'invalid_grant' }, { status: 400 });

        if (pairing.status === 'approved') {
          const tokens = await generateTokenPair();
          const familyId = randomString(16);
          const credId = randomString(16);
          const result = this.ctx.storage.transactionSync(() => {
            const current = this.sql.exec('SELECT * FROM device_pairings WHERE device_code_hash = ?', deviceCodeHash).toArray()[0] as any;
            if (!current || current.status !== 'approved') return null;
            if (!Number.isFinite(Date.parse(current.expires_at)) || Date.parse(current.expires_at) <= Date.now()) {
              this.sql.exec("UPDATE device_pairings SET status = 'expired' WHERE device_code_hash = ?", deviceCodeHash);
              return null;
            }
            // R5 R2: a delayed older approved grant must not displace a newer
            // approved family, whether the machine pointer is currently bound
            // or NULL. The pairing's approval authority must still equal the
            // machine's current witness whenever that witness exists; a
            // superseded witnessed grant and a legacy unwitnessed grant are
            // both denied WITHOUT consuming the code or touching the machine
            // binding, so no newer consent is minted and issuance retries stay
            // idempotent. Only legacy both-NULL bootstrap (no witness anywhere)
            // may pass without equality. A legacy grant with no verifiable
            // witness may bootstrap an unbound machine but must never replace
            // an already bound one. Revalidated here inside the transaction
            // after the awaited token generation above.
            // generateTokenPair performs only cryptographic preparation
            // (random strings + hashes, no storage writes); every durable
            // credential insert, code consumption and machine update below
            // happens only after this fresh post-await validation.
            let issuanceSeq: number | null = null;
            if (current.client_kind === 'host' && current.machine_id) {
              const machine = this.sql.exec('SELECT approval_seq, credential_id FROM machines WHERE id = ?', current.machine_id).toArray()[0] as any;
              if (!machine) return null;
              const bound = typeof machine.credential_id === 'string' && machine.credential_id !== '';
              if (bound) {
                if (current.approved_seq === null || current.approved_seq === undefined) return null;
                if (machine.approval_seq === null || machine.approval_seq === undefined) return null;
                if (current.approved_seq !== machine.approval_seq) return null;
                issuanceSeq = current.approved_seq;
              } else if (machine.approval_seq !== null && machine.approval_seq !== undefined) {
                // Unbound but witnessed: only the current approval authority
                // may bootstrap. A stale witnessed grant and a legacy
                // unwitnessed grant are both denied here; only legacy
                // both-NULL bootstrap falls through below.
                if (current.approved_seq === null || current.approved_seq === undefined) return null;
                if (current.approved_seq !== machine.approval_seq) return null;
                issuanceSeq = current.approved_seq;
              } else if (current.approved_seq !== null && current.approved_seq !== undefined) {
                issuanceSeq = current.approved_seq;
              }
            }
            if (current.client_kind === 'agent' && (!credentialActor(this.sql, { kind: 'agent', registry_actor: current.actor, machine_id: current.machine_id }) ||
                !validAgentPairingScopes(current.granted_scopes, current.requested_scopes))) return null;
            // Human/operator issuance: admin only, bound to the approving
            // allowlisted principal. Never trusts request-supplied identity.
            if (current.client_kind === 'human') {
              let granted: unknown;
              try { granted = typeof current.granted_scopes === 'string' ? JSON.parse(current.granted_scopes) : current.granted_scopes; } catch { return null; }
              if (!Array.isArray(granted) || granted.length !== 1 || granted[0] !== 'admin') return null;
              if (current.machine_id !== null && current.machine_id !== undefined) return null;
              const approverSub = typeof current.approved_by_sub === 'string' ? current.approved_by_sub : '';
              if (!approverSub) return null;
              if (current.actor !== 'human:' + approverSub) return null;
              let approverEmail: string | null = null;
              if (approverSub.startsWith('admin-token:')) {
                const hash = adminTokenHash(this.env);
                if (!hash || tokenAdminSubject(hash) !== approverSub) return null;
                approverEmail = TOKEN_ADMIN_EMAIL;
              } else {
                const userRow = this.sql.exec('SELECT email FROM users WHERE sub = ?', approverSub).toArray()[0] as any;
                const userEmail = typeof userRow?.email === 'string' ? userRow.email : null;
                if (!userEmail || !humanAdminAllowed(this.env, userEmail, approverSub)) return null;
                approverEmail = userEmail;
              }
              const expires = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
              const humanNow = new Date().toISOString();
              this.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, approval_seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, credId, 'human', tokens.accessHash, tokens.refreshHash, familyId, approverEmail, null, 'human:' + approverSub, JSON.stringify(['admin']), approverSub, humanNow, expires, null);
              this.sql.exec("UPDATE device_pairings SET status = 'consumed' WHERE device_code_hash = ?", deviceCodeHash);
              return { expires, scopes: ['admin'] as string[], machineId: null as string | null };
            }
            const expires = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
            this.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, approval_seq)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, credId, current.client_kind, tokens.accessHash, tokens.refreshHash, familyId, current.name, current.machine_id, current.actor, current.granted_scopes, current.approved_by_sub, new Date().toISOString(), expires, issuanceSeq);
            this.sql.exec("UPDATE device_pairings SET status = 'consumed' WHERE device_code_hash = ?", deviceCodeHash);
            if (current.client_kind === 'host' && current.machine_id) this.sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', credId, current.machine_id);
            return { expires, scopes: JSON.parse(current.granted_scopes) as string[], machineId: current.machine_id ?? null };
          });
          if (!result) return Response.json({ error: 'invalid_grant' }, { status: 400 });
          return Response.json({ access_token: tokens.accessToken, refresh_token: tokens.refreshToken, token_type: 'Bearer', expires_in: Math.floor((Date.parse(result.expires) - Date.now()) / 1000), scope: result.scopes.join(' '), machine_id: result.machineId });
        }
      }

      if (path === '/device') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const userCode = url.searchParams.get('code');
        const session = await getSession(this.env, request);
        if (!session) {
          const returnTo = userCode ? `?return_to=${encodeURIComponent('/device?code=' + userCode)}` : '';
          return new Response(null, { status: 302, headers: { 'Location': `/auth/login${returnTo}` } });
        }

        if (!userCode) return new Response(deviceEntry(session.email), {headers:UI_HEADERS});
        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode.toUpperCase()).toArray()[0] as any;
        if (!pairing) return new Response(deviceResult('unknown', session.email), {status:404,headers:UI_HEADERS});
        if (!session.isAdmin) return Response.json({ error: 'forbidden' }, { status: 403 });
        const state = !['consumed', 'denied'].includes(pairing.status) && (!Number.isFinite(Date.parse(pairing.expires_at)) || Date.parse(pairing.expires_at) <= Date.now()) ? 'expired' : pairing.status;
        if (pairing.client_kind === 'human') {
          if (state !== 'pending') return new Response(deviceResult(state, session.email), {headers:UI_HEADERS});
          return new Response(deviceReviewHuman(pairing, session.email, Date.now()), {headers:UI_HEADERS});
        }
        const machines = this.sql.exec('SELECT id, name FROM machines WHERE registered_at IS NOT NULL ORDER BY id').toArray() as any[];
        let address = registryAddress(pairing.actor || '');
        if (address && !address.project) {
          const registered = this.sql.exec('SELECT project FROM agents WHERE tag = ?', address.tag).toArray() as any[];
          if (registered.length === 1 && registered[0].project) address = { ...address, project: registered[0].project };
        }
        const displayedActor = address ? (address.project ? `${address.project}:${address.tag}` : address.tag) : pairing.actor;
        const registeredAgent = address ? this.sql.exec("SELECT machine_id FROM agents WHERE tag = ? AND COALESCE(project,'') = COALESCE(?,'')", address.tag, address.project).toArray()[0] as any : null;
        const registeredMachine = registeredAgent?.machine_id || null;
        const html = state === 'pending'
          ? deviceReview(pairing, session.email, address, registeredMachine, machines, Date.now(), displayedActor)
          : deviceResult(state, session.email);
        return new Response(html, {headers:UI_HEADERS});
      }

      if (path.startsWith('/api/device/approve') || path.startsWith('/api/device/deny') || path.startsWith('/api/device/info')) {
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ error: 'Unauthorized' }, { status: 401 });
        if (!session.isAdmin) return Response.json({ error: 'forbidden' }, { status: 403 });

        const parts = path.split('/');
        const action = parts[3];
        const userCode = parts[4];
        if (!userCode) return Response.json({ error: 'Missing user_code' }, { status: 400 });

        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
        if (!pairing) return Response.json({ error: 'Not found' }, { status: 404 });
        
        if (action === 'info') {
          if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
          return Response.json(pairing);
        }

        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing or invalid' }, { status: 403 });

        if (pairing.status !== 'pending') return Response.json({ error: 'Already processed' }, { status: 400 });
        
        const now = new Date();
        const expiresAt = new Date(pairing.expires_at);
        if (!Number.isFinite(expiresAt.getTime()) || now >= expiresAt) {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE user_code = ?', 'expired', userCode);
          return Response.json({ error: 'Already expired' }, { status: 400 });
        }

        if (action === 'deny') {
          this.sql.exec('UPDATE device_pairings SET status = ? WHERE user_code = ?', 'denied', userCode);
          return Response.json({ status: 'denied' });
        }

        if (action === 'approve') {
          const body = await safeJson(request);
          if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });
          // Human/operator approval: bound ONLY to the approving allowlisted
          // session. Request-supplied identity/scopes/machine are never trusted.
          if (pairing.client_kind === 'human') {
            if (Object.keys(body).some(k => !['granted_scopes', 'expected_actor'].includes(k))) {
              return Response.json({ error: 'invalid_approval' }, { status: 400 });
            }
            if (!humanAdminAllowed(this.env, session.email, session.sub)) {
              return Response.json({ error: 'forbidden' }, { status: 403 });
            }
            const granted = body.granted_scopes ?? ['admin'];
            if (!Array.isArray(granted) || granted.length !== 1 || granted[0] !== 'admin') {
              return Response.json({ error: 'invalid_scopes' }, { status: 400 });
            }
            if (body.expected_actor !== undefined && body.expected_actor !== null) {
              return Response.json({ error: 'invalid_approval' }, { status: 400 });
            }
            return this.ctx.storage.transactionSync(() => {
              const current = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
              if (!current || current.status !== 'pending' || !Number.isFinite(Date.parse(current.expires_at)) || Date.parse(current.expires_at) <= Date.now()) return Response.json({ error: 'invalid_pairing' }, { status: 409 });
              if (current.client_kind !== 'human') return Response.json({ error: 'invalid_pairing' }, { status: 400 });
              if (['name', 'requested_scopes', 'expires_at'].some(k => current[k] !== pairing[k])) return Response.json({ error: 'stale_pairing' }, { status: 409 });
              const humanActor = 'human:' + session.sub;
              this.sql.exec("UPDATE device_pairings SET status = 'approved', approved_by_sub = ?, granted_scopes = ?, machine_id = NULL, actor = ?, approved_seq = NULL WHERE user_code = ?", session.sub, JSON.stringify(['admin']), humanActor, userCode);
              return Response.json({ status: 'approved', machine_id: null });
            });
          }
          if (Object.keys(body).some(k => !['granted_scopes', 'machine_id', 'expected_actor'].includes(k))) return Response.json({ error: 'invalid_approval' }, { status: 400 });
          const granted_scopes = body.granted_scopes ?? JSON.parse(pairing.requested_scopes);
          const parsedGranted = granted_scopes;
          
          if (!Array.isArray(parsedGranted) || parsedGranted.length === 0) return Response.json({ error: 'invalid_scopes' }, { status: 400 });
          if (parsedGranted.includes('admin')) return Response.json({ error: 'Cannot grant admin scope through device flow' }, { status: 400 });
          
          const reqScopes = typeof pairing.requested_scopes === 'string' ? JSON.parse(pairing.requested_scopes) : pairing.requested_scopes;
          const isSubset = parsedGranted.every((s: string) => reqScopes.includes(s));
          if (!isSubset) return Response.json({ error: 'Cannot grant unrequested scopes' }, { status: 400 });

          if (body.machine_id !== undefined && !(body.machine_id === null && pairing.client_kind === 'agent') && (typeof body.machine_id !== 'string' || !body.machine_id || body.machine_id.length > 128)) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
          return this.ctx.storage.transactionSync(() => {
            const current = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
            if (!current || current.status !== 'pending' || !Number.isFinite(Date.parse(current.expires_at)) || Date.parse(current.expires_at) <= Date.now()) return Response.json({ error: 'invalid_pairing' }, { status: 409 });
            if (['client_kind', 'name', 'actor', 'requested_scopes', 'machine_id', 'expires_at'].some(k => current[k] !== pairing[k])) return Response.json({ error: 'stale_pairing' }, { status: 409 });
            if (current.client_kind !== 'host' && current.client_kind !== 'agent') return Response.json({ error: 'invalid_pairing' }, { status: 400 });
            if (current.client_kind === 'agent' && !validAgentPairingScopes(JSON.stringify(parsedGranted), current.requested_scopes)) return Response.json({ error: 'invalid_scopes' }, { status: 400 });
            let machineId: string | null = null;
            if (current.client_kind === 'host') {
              if (body.expected_actor !== undefined && body.expected_actor !== current.actor) return Response.json({ error: 'invalid_approval' }, { status: 400 });
              if (body.machine_id) {
                if (!this.sql.exec('SELECT id FROM machines WHERE id = ?', body.machine_id).toArray()[0]) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
                machineId = body.machine_id;
              } else {
                machineId = randomString(16);
                this.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', machineId, current.name, current.hostname, new Date().toISOString());
              }
            }
            // A paired agent is addressable on the bus by its actor name; an existing entry is kept.
            if (current.client_kind === 'agent') {
              if (typeof current.actor !== 'string' || !current.actor) return Response.json({ error: 'invalid_actor' }, { status: 400 });
              const address = registryAddress(current.actor);
              if (!address) return Response.json({ error: 'invalid_actor' }, { status: 400 });
              const rows = this.sql.exec('SELECT project, tag FROM agents WHERE tag = ?', address.tag).toArray() as any[];
              if (address.project && rows.some(r => !r.project)) return Response.json({ error: 'agent_exists' }, { status: 409 });
              if (!address.project && rows.length > 1) return Response.json({ error: 'ambiguous_agent_tag' }, { status: 409 });
              const project = address.project ?? (rows.length === 1 ? rows[0].project : null);
              const actor = project ? `${project}:${address.tag}` : address.tag;
              if (!registryAddress(actor)) return Response.json({ error: 'invalid_actor' }, { status: 400 });
              if (body.expected_actor !== undefined && body.expected_actor !== actor) return Response.json({ error: 'invalid_approval' }, { status: 400 });
              const existing = this.sql.exec("SELECT machine_id FROM agents WHERE tag = ? AND COALESCE(project,'') = COALESCE(?,'')", address.tag, project).toArray()[0] as any;
              // Omission preserves a trusted existing assignment; explicit admin selection may fill NULL only.
              machineId = body.machine_id !== undefined ? body.machine_id : (existing?.machine_id ?? null);
              if (existing?.machine_id && machineId !== existing.machine_id) return Response.json({ error: 'agent_machine_conflict' }, { status: 409 });
              if (machineId && (!project || !this.sql.exec('SELECT id FROM machines WHERE id = ? AND registered_at IS NOT NULL', machineId).toArray()[0])) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
              this.sql.exec("INSERT INTO agents (tag, project, machine_id, registered_at, imported_from) VALUES (?, ?, ?, ?, 'pairing') ON CONFLICT DO NOTHING", address.tag, project, machineId, new Date().toISOString());
              if (machineId && !existing?.machine_id) this.sql.exec("UPDATE agents SET machine_id = ? WHERE tag = ? AND COALESCE(project,'') = COALESCE(?,'')", machineId, address.tag, project);
              this.sql.exec('UPDATE device_pairings SET actor = ? WHERE user_code = ?', actor, userCode);
            }
            // R5 R2: operator approval originates stable server-owned authority.
            // Each host approval mints a strictly increasing per-machine integer
            // in this same transaction, independent of pairing creation order,
            // issuance/consumption order, client body and clocks. Replay of an
            // approved pairing returns Already processed above; no new seq.
            let approvalSeq: number | null = null;
            if (current.client_kind === 'host' && machineId) {
              approvalSeq = assignApprovalAuthority(this.sql, machineId);
            }
            this.sql.exec("UPDATE device_pairings SET status = 'approved', approved_by_sub = ?, granted_scopes = ?, machine_id = ?, approved_seq = ? WHERE user_code = ?", session.sub, JSON.stringify(parsedGranted), machineId, approvalSeq, userCode);
            return Response.json({ status: 'approved', machine_id: machineId });
          });
        }
      }

      if (path === '/api/token/refresh') return await this.refreshCredential(request);

      if (path === '/api/token/revoke') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const body = await safeJson(request);
        if (!body || (!body.token && !body.id) || (body.token !== undefined && typeof body.token !== 'string') || (body.id !== undefined && typeof body.id !== 'string')) return Response.json({ error: 'missing_token_or_id' }, { status: 400 });

        let familyIdToRevoke = null;

        if (body.token) {
          const authHeader = request.headers.get('Authorization');
          if (!authHeader || !authHeader.startsWith('Bearer ')) return Response.json({ error: 'Unauthorized' }, { status: 401 });
          const bearerHash = await sha256(authHeader.substring(7).trim());
          const ownerCred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL', bearerHash).toArray()[0] as any;
          if (!ownerCred || !Number.isFinite(Date.parse(ownerCred.expires_at)) || Date.parse(ownerCred.expires_at) <= Date.now()) return Response.json({ error: 'Unauthorized' }, { status: 401 });

          const tokenHash = await sha256(body.token);
          const cred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? OR refresh_hash = ?', tokenHash, tokenHash).toArray()[0] as any;
          if (cred && cred.family_id === ownerCred.family_id) {
            familyIdToRevoke = cred.family_id;
          } else if (cred) {
             return Response.json({ error: 'Unauthorized' }, { status: 403 });
          }
        } else if (body.id) {
          const session = await getSession(this.env, request);
          if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });
          if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });
          
          const cred = this.sql.exec('SELECT * FROM credentials WHERE id = ?', body.id).toArray()[0] as any;
          if (cred) familyIdToRevoke = cred.family_id;
        }
        
        if (familyIdToRevoke) {
          this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', new Date().toISOString(), familyIdToRevoke);
        }

        return Response.json({ status: 'revoked' });
      }

      if (path === '/internal/tick') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        const body = await safeJson(request);
        const slotOverride = (body && typeof body === 'object') ? (body.slot || body.slot_start) : undefined;

        // If client provides slot override or Authorization header, strictly enforce admin authorization
        const hasAuthHeader = Boolean(request.headers.get('Authorization'));
        if (slotOverride || hasAuthHeader) {
          if (auth.error || !auth.scopes?.includes('admin')) {
            return Response.json({ error: 'unauthorized', reason: 'admin_required' }, { status: 401 });
          }
        }

        const res = await this.scheduledTick(slotOverride, 'manual');
        if (res.status === 'refused' || res.status === 'rejected') {
          return Response.json(res, { status: 400 });
        }
        return Response.json(res);
      }

      if (path === '/api/cron/retention') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const session = await getSession(this.env, request);
        if (session) {
          if (!session.isAdmin) return Response.json({ error: 'forbidden' }, { status: 403 });
          if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing or invalid' }, { status: 403 });
        } else {
          const authHeader = request.headers.get('Authorization');
          if (!authHeader) {
            return Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } });
          }
          return Response.json({ error: 'forbidden', reason: 'admin_csrf_required' }, { status: 403 });
        }

        // Caller body.now inert: strictly use internal server clock
        const now = Date.now();
        const cleanup = this.busService.retentionCleanup(now, 100);
        if (cleanup.hasMore) {
          await this.ctx.storage.setAlarm(Date.now() + 1000);
        }
        const totalDeleted = (cleanup.messagesDeleted || 0) + (cleanup.claimsDeleted || 0);
        return Response.json({
          status: 'ok',
          deleted: totalDeleted,
          messagesDeleted: cleanup.messagesDeleted,
          claimsDeleted: cleanup.claimsDeleted,
          hasMore: cleanup.hasMore
        });
      }

      if (path === '/api/root/scheduling') {
        if (request.method !== 'GET' && request.method !== 'PUT') {
          return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET, PUT' } });
        }
        const session = await getSession(this.env, request);
        if (session && !session.isAdmin) return Response.json({ error: 'forbidden' }, { status: 403 });

        if (request.method === 'GET') {
          // Read: admin session, or the existing host bearer verified like /api/host/poll
          // (host scope, kind host, unrevoked, unexpired). Identity comes only from the
          // verified credential; nothing in the request names the host.
          if (!session) {
            const hostCred = await verifyBearerScope(this.sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
            if (!hostCred || !hostCred.machine_id) return Response.json({ error: 'unauthorized' }, { status: 401 });
          }
          const state = getRootScheduling(this.sql);
          return Response.json({ enabled: state.enabled, updated_at: state.updated_at, server_time: new Date().toISOString() });
        }

        // Write: admin session plus session CSRF only. Bearer tokens (host/agent/job/root)
        // carry no scheduling write authority.
        if (!session) return Response.json({ error: 'unauthorized' }, { status: 401 });
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing or invalid' }, { status: 403 });
        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });
        const bodyKeys = Object.keys(body);
        if (bodyKeys.length !== 1 || bodyKeys[0] !== 'enabled') {
          return Response.json({ error: 'invalid_body', reason: 'exact body {"enabled": boolean} required' }, { status: 400 });
        }
        if (typeof body.enabled !== 'boolean') {
          return Response.json({ error: 'invalid_enabled', reason: 'enabled must be a boolean' }, { status: 400 });
        }

        const nowIso = new Date().toISOString();
        const previous = getRootScheduling(this.sql);
        const state = this.ctx.storage.transactionSync(() => {
          const next = setRootScheduling(this.sql, body.enabled, nowIso);
          if (next.enabled !== previous.enabled) {
            this.sql.exec(
              'INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
              session.email || session.sub,
              next.enabled ? 'root_scheduling_enable' : 'root_scheduling_disable',
              'root_scheduling',
              'root',
              JSON.stringify({ from: previous.enabled, to: next.enabled }),
              nowIso
            );
          }
          return next;
        });
        return Response.json({ enabled: state.enabled, updated_at: state.updated_at, server_time: new Date().toISOString() });
      }

      return new Response("Not found", { status: 404 });
    } catch (e: any) {
      return Response.json({ error: 'Internal Server Error' }, { status: 500 });
    }
  }

  protected async refreshCredential(request: Request, binding?: { client: string; resource: string }): Promise<Response> {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const body = await safeJson(request);
      if (!body || body.grant_type !== 'refresh_token' || typeof body.refresh_token !== 'string' || !body.refresh_token || body.refresh_token.length > 256) {
        return Response.json({ error: 'invalid_request' }, { status: 400 });
      }

      const refreshHash = await sha256(body.refresh_token);

      // PRE-GENERATE values before any synchronous DB interaction to prevent partial state on await failures
      const { accessToken, refreshToken, accessHash, refreshHash: newRefreshHash } = await generateTokenPair();
      const newId = randomString(16);
      const now = new Date(Date.now()).toISOString();

      let errorResponse = null;
      let scopesResponse = '';
      let expiresAtResponse = '';
      let machineIdResponse: string | null = null;

      this.ctx.storage.transactionSync(() => {
      const cred = this.sql.exec('SELECT * FROM credentials WHERE refresh_hash = ?', refreshHash).toArray()[0] as any;
      const nowMs = Date.parse(now);
      const hasMcp = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_grants'").toArray().length > 0;
      const mcpGrant = cred && hasMcp ? this.sql.exec('SELECT * FROM mcp_grants WHERE family=?', cred.family_id).toArray()[0] as any : null;
      // OAuth refresh is usable only through the client/resource-bound server path.
      // This context is an internal method argument, never an HTTP header or body field.
      if ((mcpGrant && (!binding || mcpGrant.client !== binding.client || mcpGrant.resource !== binding.resource || mcpGrant.expires <= Date.now())) || (binding && !mcpGrant)) {
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        return;
      }
      if (!cred) {
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
      } else if (cred.revoked_at) {
        // Replay or explicitly revoked! Revoke family.
        this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
      } else if (!Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= nowMs) {
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
      } else if (cred.kind === 'agent' && !credentialActor(this.sql, cred)) {
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
      } else if (cred.kind === 'human' && !this.isHumanRefreshAllowed(cred)) {
        // Removed-admin, mismatched identity or non-admin human scope:
        // revoke the family so it cannot refresh back, then fail closed.
        this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
        errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
      } else {
        const res = this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL', now, cred.id);
        if (res.rowsWritten !== 1) {
          this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else {
          this.sql.exec(`
            INSERT INTO credentials
            (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at, registry_actor, approval_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          `, newId, cred.kind, accessHash, newRefreshHash, cred.family_id, cred.subject, cred.machine_id, cred.actor, cred.scopes, cred.created_by_sub, now, cred.expires_at, cred.registry_actor ?? null, cred.approval_seq ?? null);

          // Host-enrollment continuity (from f20; M9 metrics-row migration
          // guarded: those tables exist only after MetricsStore.init):
          // atomically rotate the host machine binding only when it still
          // belongs to the validated predecessor. Stale/foreign/rebound or
          // deleted/re-enrolled families must never overwrite a new
          // registration; a legitimate refresh keeps its binding so its
          // subsequent register calls still pass the owner CAS.
          if (cred.kind === 'host' && typeof cred.machine_id === 'string' && cred.machine_id) {
            const machineUpdate = this.sql.exec(
              'UPDATE machines SET credential_id = ? WHERE id = ? AND credential_id = ?',
              newId, cred.machine_id, cred.id
            );
            // M9 metrics continuity: migrate rows bound to the validated
            // predecessor only when the machine binding actually rotated.
            // Stale/foreign/rebound refreshes mutate nothing.
            if (machineUpdate.rowsWritten === 1) {
              try {
                const hasLatest = this.sql.exec(
                  "SELECT name FROM sqlite_master WHERE type='table' AND name='host_metrics_latest'"
                ).toArray().length > 0;
                if (hasLatest) {
                  this.sql.exec(
                    'UPDATE host_metrics_latest SET credential_id = ? WHERE host_id = ? AND credential_id = ?',
                    newId, cred.machine_id, cred.id
                  );
                }
                const hasRetry = this.sql.exec(
                  "SELECT name FROM sqlite_master WHERE type='table' AND name='host_metrics_idempotency'"
                ).toArray().length > 0;
                if (hasRetry) {
                  this.sql.exec(
                    'UPDATE host_metrics_idempotency SET credential_id = ? WHERE host_id = ? AND credential_id = ?',
                    newId, cred.machine_id, cred.id
                  );
                }
              } catch { /* metrics tables absent or migration contended: auth rotation already committed */ }
            }
          }

          scopesResponse = JSON.parse(cred.scopes).join(' ');
          expiresAtResponse = cred.expires_at;
          machineIdResponse = cred.machine_id ?? null;
        }
      }

      });
      if (errorResponse) return errorResponse;

      return Response.json({
        access_token: accessToken,
        refresh_token: refreshToken,
        token_type: 'Bearer',
        expires_in: Math.max(0, Math.floor((new Date(expiresAtResponse).getTime() - new Date(now).getTime()) / 1000)),
        scope: scopesResponse,
        machine_id: machineIdResponse
      });
  }

  private tokenFailures: number[] = [];

  /** Admin sign-in with a high-entropy token whose SHA-256 is a Worker secret. */
  private async tokenSignIn(request: Request): Promise<Response> {
    const headers = { 'Cache-Control': 'private, no-store', 'Content-Security-Policy': UI_HEADERS['Content-Security-Policy'], 'X-Content-Type-Options': 'nosniff' };
    const hash = adminTokenHash(this.env);
    if (!hash || !validSessionSecret(this.env)) return authFailure(request, 'authentication_not_configured', 503, headers);
    const safeNext = safeAuthReturn;
    let next = safeNext(new URL(request.url).searchParams.get('return_to'));
    const page = (message: string, status = 200) => new Response(tokenPage(message, next, status), {status,headers:UI_HEADERS});
    if (request.method === 'GET') return page('Sign in with the admin token kept on your machine.');
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { ...headers, Allow: 'GET, POST' } });
    const now = Date.now();
    this.tokenFailures = this.tokenFailures.filter(t => now - t < 600000);
    if (this.tokenFailures.length >= 10) return page('Too many failed attempts. Try again in ten minutes.', 429);
    const raw = await request.text();
    const fields = raw.length <= 4096 ? new URLSearchParams(raw) : new URLSearchParams();
    const token = fields.get('token');
    next = safeNext(fields.get('return_to')) ?? next;
    const given = token ? [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token)))].map(b => b.toString(16).padStart(2, '0')).join('') : '';
    let diff = given.length === hash.length ? 0 : 1;
    for (let i = 0; i < hash.length; i++) diff |= (given.charCodeAt(i) || 0) ^ hash.charCodeAt(i);
    if (diff !== 0) { this.tokenFailures.push(now); return page('That token was not accepted.', 401); }
    const cookie = await createSessionCookie(this.env, { sub: tokenAdminSubject(hash), email: TOKEN_ADMIN_EMAIL, isAdmin: true, csrfToken: randomString(32), exp: Math.floor(now / 1000) + 12 * 3600 });
    return new Response(null, { status: 303, headers: { ...headers, Location: next || '/', 'Set-Cookie': cookie } });
  }

  /**
   * Admin registry of bus recipients. Paired agents register themselves on approval; use this for
   * agents reached through a host bridge (for example an aplexer coordinator on a machine).
   */
  private async handleAgentRegistry(request: Request, path: string): Promise<Response> {
    const auth = await this.authenticateBusRequest(request);
    if (auth.error) return auth.error;
    const json = (value: unknown, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'private, no-store' } });
    if (!auth.caller?.isAdmin) return json({ error: 'forbidden' }, 403);
    const NAME = AGENT_NAME;
    if (path === '/api/agents' && request.method === 'GET') return json({ agents: this.sql.exec('SELECT tag, project, role, machine_id, registered_at, imported_from FROM agents ORDER BY tag, project').toArray() });
    if (path === '/api/agents' && request.method === 'POST') {
      const b = await safeJson(request);
      if (!b || Object.keys(b).some(k => !['tag', 'project', 'role', 'machine_id'].includes(k)) || typeof b.tag !== 'string' || !NAME.test(b.tag)) return json({ error: 'invalid_agent' }, 400);
      for (const k of ['project', 'role', 'machine_id']) if (b[k] !== undefined && b[k] !== null && !(k === 'project' && b[k] === '') && (typeof b[k] !== 'string' || !NAME.test(b[k]))) return json({ error: 'invalid_' + k }, 400);
      if (!registryAddress(b.project ? `${b.project}:${b.tag}` : b.tag)) return json({ error: 'invalid_project' }, 400);
      if (typeof b.role === 'string' && /^root/i.test(b.role)) return json({ error: 'root_role_reserved' }, 400);
      if (b.machine_id && !this.sql.exec('SELECT 1 FROM machines WHERE id = ?', b.machine_id).toArray().length) return json({ error: 'unknown_machine' }, 400);
      return this.ctx.storage.transactionSync(() => {
        const project = b.project ?? null;
        // Unqualified legacy creation remains conservative; explicit projects permit same-tag peers.
        const exists = project
          ? this.sql.exec("SELECT 1 FROM agents WHERE (COALESCE(project, '') = ? OR COALESCE(project, '') = '') AND tag = ?", project, b.tag).toArray().length
          : this.sql.exec('SELECT 1 FROM agents WHERE tag = ?', b.tag).toArray().length;
        if (exists) return json({ error: 'agent_exists' }, 409);
        this.sql.exec("INSERT INTO agents (tag, project, role, machine_id, registered_at, imported_from) VALUES (?, ?, ?, ?, ?, 'admin')", b.tag, project, b.role ?? null, b.machine_id ?? null, new Date().toISOString());
        return json({ status: 'registered', tag: b.tag, project, actor: project ? `${project}:${b.tag}` : b.tag }, 201);
      });
    }
    if (path.startsWith('/api/agents/') && request.method === 'DELETE') {
      let raw: string;
      try { raw = decodeURIComponent(path.slice('/api/agents/'.length)); } catch { return json({ error: 'invalid_actor' }, 400); }
      const address = registryAddress(raw);
      if (!address) return json({ error: 'invalid_actor' }, 400);
      await safeJson(request);
      return this.ctx.storage.transactionSync(() => {
        const rows = address.project
          ? this.sql.exec('SELECT project, tag FROM agents WHERE project = ? AND tag = ?', address.project, address.tag).toArray() as any[]
          : this.sql.exec('SELECT project, tag FROM agents WHERE tag = ?', address.tag).toArray() as any[];
        if (!rows.length) return json({ error: 'not_found' }, 404);
        if (rows.length !== 1) return json({ error: 'ambiguous_agent_tag' }, 409);
        this.sql.exec("DELETE FROM agents WHERE COALESCE(project, '') = ? AND tag = ?", rows[0].project ?? '', address.tag);
        return json({ status: 'removed', tag: address.tag, project: rows[0].project });
      });
    }
    return json({ error: 'not_found' }, 404);
  }

  /** Heartbeats, liveness, alerts and scheduled checks. */
  private async handleWatchRoutes(request: Request, path: string): Promise<Response> {
    const auth = await this.authenticateBusRequest(request);
    if (auth.error) return auth.error;
    const caller = auth.caller!; const admin = !!caller.isAdmin;
    const json = (value: unknown, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'private, no-store' } });
    if (path === '/api/heartbeat') {
      if (request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
      // Agents report for themselves only; a host's heartbeat travels through /api/host/poll.
      if (caller.kind !== 'agent' || !caller.actor || !auth.scopes?.includes('health:report')) return json({ error: 'forbidden' }, 403);
      const result = this.livenessService.recordHeartbeat(caller.actor, caller.machineId ?? null, await safeJson(request));
      return 'error' in result ? json(result, 400) : json({ status: 'recorded' });
    }
    if (!admin) return json({ error: 'forbidden' }, 403);
    if (path === '/api/liveness' && request.method === 'GET') {
      // Read one subject's retained history even when newer open alerts fill
      // the default page. A history read never reconciles or resolves alerts.
      const subject = new URL(request.url).searchParams.get('alert_subject');
      if (subject !== null) {
        if (!subject || subject.length > 128 || /[\s\x00-\x1f\x7f]/.test(subject)) return json({ error: 'invalid_alert_subject' }, 400);
        return json({ alerts: this.livenessService.alerts(100, subject) });
      }
      const { thresholds, subjects } = this.livenessService.evaluate();
      return json({ thresholds, subjects, alerts: this.livenessService.alerts() });
    }
    if (path === '/api/checks' && request.method === 'GET') return json({ checks: this.checksService.list(), runs: this.checksService.runs() });
    if (path === '/api/checks' && request.method === 'PUT') {
      const data = await safeJson(request);
      if (!data || !Array.isArray(data.checks) || data.checks.length > 64 || Object.keys(data).some(k => k !== 'checks')) return json({ error: 'invalid_checks' }, 400);
      const rows: any[] = []; const names = new Set<string>();
      for (const c of data.checks) {
        const v = validateCheck(c);
        if ('error' in v) return json({ error: v.error, check: typeof c?.name === 'string' ? c.name.slice(0, 64) : null }, 400);
        if (names.has(v.row.name)) return json({ error: 'duplicate_check', check: v.row.name }, 400);
        names.add(v.row.name); rows.push(v.row);
      }
      this.ctx.storage.transactionSync(() => this.checksService.replace(rows));
      return json({ status: 'configured', checks: this.checksService.list() });
    }
    const run = /^\/api\/checks\/([a-z0-9][a-z0-9_-]{0,39})\/run$/.exec(path);
    if (run && request.method === 'POST') {
      await safeJson(request); // the body is unused; drain it so the runtime does not log a stream error
      if (!this.checksService.list().some(c => c.name === run[1])) return json({ error: 'not_found' }, 404);
      return json({ results: await this.runChecks(run[1]) });
    }
    return json({ error: 'not_found' }, 404);
  }

  /** Deliver a newly opened alert to the configured inbox actors and optional webhook. */
  private async notifyAlert(alert: any) {
    const text = alertText(alert); const errors: string[] = [];
    // Each target is delivered once; a retry only covers targets that have not succeeded yet.
    const delivered = new Set(this.livenessService.deliveredTo(alert));
    const targets = typeof this.env.HUB_ALERT_TO === 'string' ? this.env.HUB_ALERT_TO.split(',').map(t => t.trim()).filter(Boolean) : ['grok-bot'];
    for (const to of targets) {
      if (delivered.has('inbox:' + to)) continue;
      try {
        this.busService.send({ isAdmin: true, actor: 'hub', kind: 'human' }, { senderActor: 'hub', toActor: to, kind: 'note', body: text, idempotencyKey: `alert-${alert.id}-${to}-${Date.now()}` });
        delivered.add('inbox:' + to);
      } catch (e: any) { errors.push(`${to}: ${String(e?.message || e).slice(0, 80)}`); }
    }
    const webhook = typeof this.env.HUB_ALERT_WEBHOOK === 'string' && this.env.HUB_ALERT_WEBHOOK.startsWith('https://') ? this.env.HUB_ALERT_WEBHOOK : null;
    if (webhook && !delivered.has('webhook')) {
      try {
        const r = await fetch(webhook, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, alert: { id: alert.id, subject_kind: alert.subject_kind, subject: alert.subject, state: alert.state, detail: alert.detail, opened_at: alert.opened_at } }), signal: AbortSignal.timeout(10000) });
        if (r.ok) delivered.add('webhook'); else errors.push(`webhook: HTTP ${r.status}`);
      } catch { errors.push('webhook: unreachable'); }
    }
    const anyTarget = targets.length > 0 || !!webhook;
    this.livenessService.markNotified(alert.id, errors.length ? errors.join('; ') : (anyTarget ? null : 'no alert target configured'), [...delivered]);
    // A delivered informational digest is not an ongoing incident. Preserve its
    // receipt/history, but keep it out of the unresolved incident queue. Failed
    // targets stay open for the ordinary retry path.
    if (alert.subject_kind === 'intake_digest' && anyTarget && !errors.length) {
      this.livenessService.setAlert(alert.subject_kind, alert.subject, null);
    }
  }

  protected extraCheckDeps(): Partial<ActionDeps> { return {}; }

  /** Cron and manual entry point for scheduled checks. */
  async runChecks(only?: string) {
    this.runTbmTick();
    this.livenessService.prune();
    const results = await this.checksService.runDue({
      liveness: this.livenessService,
      notify: alert => this.notifyAlert(alert),
      message: async (to, body, key) => this.busService.send({ isAdmin: true, actor: 'hub', kind: 'human' }, { senderActor: 'hub', toActor: to, kind: 'note', body, idempotencyKey: 'check-' + key }).id,
      launch: async (name, input, key) => {
        const response = await this.fleetService.handle(new Request('https://hub.internal/api/fleet/launches', { method: 'POST', body: JSON.stringify({ ...input, idempotency_key: 'check-' + key }) }),
          { caller: { isAdmin: true, actor: 'schedule:' + name, kind: 'schedule' as any }, scopes: ['admin'] });
        const result: any = await response.json();
        if (!response.ok) throw new Error(result.error || `launch refused (${response.status})`);
        return `${result.id} ${result.state} on ${result.machine}`;
      },
      fetch: (url, init) => fetch(url, init),
      evaluateQuota: () => dedupeAccountsByCanonical(this.sql.exec('SELECT * FROM fleet_accounts ORDER BY label').toArray() as any[]).map(a => this.fleetService.accountStatus(a)),
      ...this.extraCheckDeps(),
    }, only);
    // Retry alerts whose delivery failed or had no target yet, at most every 15 minutes each.
    for (const alert of this.livenessService.undelivered(900)) await this.notifyAlert(alert);
    return results;
  }

  async scheduledTick(slotOverride?: string, source = 'scheduled') {
    const now = (this.jobsService as any).now ? (this.jobsService as any).now() : new Date();
    // 1. Bus retention cleanup
    const cleanup = this.busService.retentionCleanup(now.getTime(), 100);

    // 2. Jobs root slot tick
    let slotDate: Date;
    if (slotOverride) {
      const parsed = new Date(slotOverride);
      if (isNaN(parsed.getTime())) {
        return { status: 'rejected', reason: 'invalid_slot_timestamp', error: 'invalid_slot_timestamp' };
      }
      const rawAge = now.getTime() - parsed.getTime();
      if (rawAge >= 1500000) {
        return { status: 'refused', reason: 'late_slot_refused', age_ms: rawAge, slot: parsed.toISOString() };
      }
      if (parsed.getTime() > now.getTime() + 60000) {
        return { status: 'refused', reason: 'future_slot_refused', slot: parsed.toISOString() };
      }
      const slotMinute = Math.floor(parsed.getUTCMinutes() / 30) * 30;
      slotDate = new Date(Date.UTC(
        parsed.getUTCFullYear(),
        parsed.getUTCMonth(),
        parsed.getUTCDate(),
        parsed.getUTCHours(),
        slotMinute,
        0,
        0
      ));
    } else {
      const slotMinute = Math.floor(now.getUTCMinutes() / 30) * 30;
      slotDate = new Date(Date.UTC(
        now.getUTCFullYear(),
        now.getUTCMonth(),
        now.getUTCDate(),
        now.getUTCHours(),
        slotMinute,
        0,
        0
      ));
      const age = now.getTime() - slotDate.getTime();
      if (age >= 1500000) {
        return { status: 'refused', reason: 'late_slot_refused', age_ms: age, slot: slotDate.toISOString() };
      }
    }

    const slotTime = slotDate.getTime();
    const age = now.getTime() - slotTime;
    if (age >= 1500000) {
      return { status: 'refused', reason: 'late_slot_refused', age_ms: age, slot: slotDate.toISOString() };
    }
    if (slotTime > now.getTime() + 60000) {
      return { status: 'refused', reason: 'future_slot_refused', slot: slotDate.toISOString() };
    }

    const intakeWatchdog = await this.intakeWatchdog.tick(source);
    const slotStartIso = slotDate.toISOString();
    const tickResult = this.jobsService.tickRootSlot(slotStartIso);

    // 3. Process alarms & expiry
    const nextAlarm = this.jobsService.processAlarms();

    // 4. Schedule next alarm with bounded free usage guard
    if (cleanup.hasMore) {
      await this.ctx.storage.setAlarm(Date.now() + 1000);
    } else {
      const nextTime = nextAlarm || this.jobsService.getAlarmTime();
      if (nextTime) {
        try {
          await this.ctx.storage.setAlarm(Math.max(Date.now(), Math.min(nextTime, Date.now() + 60000)));
        } catch {}
      }
    }

    return { status: 'ok', retention: cleanup, tick: tickResult, intake_watchdog: intakeWatchdog };
  }

  async sampleHostHistory() {
    try { this.hostHistoryService.sample(); } catch { console.warn('resource_history_sampling_failed'); }
    const alarm = await this.ctx.storage.getAlarm();
    if (alarm === null || alarm > Date.now() + 60000) await this.ctx.storage.setAlarm(Date.now() + 60000);
  }

  /** Durable cadence survives hibernation. Alarm plus the existing five-minute cron own the tick. */
  runTbmTick(nowMs = Date.now()) {
    this.sql.exec('CREATE TABLE IF NOT EXISTS tbm_tick_state (id INTEGER PRIMARY KEY, last_tick_ms INTEGER NOT NULL, summary_json TEXT NOT NULL)');
    const previous: any = this.sql.exec('SELECT last_tick_ms FROM tbm_tick_state WHERE id=1').toArray()[0];
    if (previous && nowMs - previous.last_tick_ms < 240000) return;
    return this.ctx.storage.transactionSync(() => {
      syncStoredTbmSnapshots(this.sql, nowMs);
      const summary = tickTbm(this.sql, nowMs);
      this.sql.exec('INSERT OR REPLACE INTO tbm_tick_state VALUES (1,?,?)', nowMs, JSON.stringify(summary));
      return summary;
    });
  }

  async alarm() {
    const now = (this.jobsService as any).now ? (this.jobsService as any).now() : new Date();
    this.runTbmTick(now.getTime());
    try { this.hostHistoryService.sample(); } catch { console.warn('resource_history_sampling_failed'); }
    const cleanup = this.busService.retentionCleanup(now.getTime(), 100);
    const nextAlarm = this.jobsService.processAlarms();

    if (cleanup.hasMore) {
      await this.ctx.storage.setAlarm(Date.now() + 1000);
    } else {
      try {
        await this.ctx.storage.setAlarm(Math.max(Date.now(), Math.min(nextAlarm || Infinity, Date.now() + 60000)));
      } catch {}
    }
  }

  /** M12e session control auth (narrow seam, owned by platform-session-api-r2):
   * identical to the intake mount: mixed-authority refusal, bus credentials,
   * narrow operator-token fallback, cookie session actor fix. */
  /** M14b slice2 (narrow STATUS composition): resolve observation transport
   * authority BEFORE the mutation transaction. Host transport first: a
   * verified host credential names ONLY its own machine (JSON machine/owner
   * flags never authorize). Otherwise only an admin operator record: the
   * existing admin session (CSRF enforced for this POST inside
   * authenticateBusRequest) or the existing operator token fallback. Agent,
   * job and root bearers carry no observation authority. */
  private async statusObservationAuth(request: Request): Promise<{ auth: StatusMachineAuth } | { error: Response }> {
    if (request.headers.has('Authorization') && request.headers.has('Cookie')) {
      return { error: Response.json({ error: 'mixed_authority' }, { status: 403 }) };
    }
    const hostCred = await verifyBearerScope(this.sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (hostCred && typeof hostCred.machine_id === 'string' && hostCred.machine_id) {
      return { auth: { kind: 'host', machine_id: hostCred.machine_id, tokenId: hostCred.id } };
    }
    const auth = await this.authenticateBusRequest(request);
    if (!auth.error && (auth.caller as any)?.isAdmin) {
      return { auth: { kind: 'admin', actor: (auth.caller as any)?.actor || 'admin' } };
    }
    if (!request.headers.has('Cookie') && validSessionSecret(this.env)) {
      const header = request.headers.get('Authorization') || '';
      const token = /^Bearer ([^\s]{1,4096})$/.exec(header)?.[1];
      if (token) {
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
        const given = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
        // A registered non-admin credential never upgrades: only the
        // configured operator token passes here.
        if (!this.sql.exec('SELECT id FROM credentials WHERE token_hash=?', base64url(digest)).toArray().length) {
          const configured = adminTokenHash(this.env);
          if (configured) {
            let difference = 0;
            for (let i = 0; i < configured.length; i++) difference |= given.charCodeAt(i) ^ configured.charCodeAt(i);
            if (difference === 0) return { auth: { kind: 'admin', actor: 'human:' + tokenAdminSubject(configured) } };
          }
        }
      }
    }
    if (auth.error) return { error: auth.error };
    return { error: Response.json({ error: 'unauthorized_or_forbidden' }, { status: 403 }) };
  }

  private async sessionControlAuth(request: Request) {
    if (request.headers.has('Authorization') && request.headers.has('Cookie')) return { error: Response.json({ error: 'mixed_authority' }, { status: 403 }) };
    const auth = await this.authenticateBusRequest(request);
    // The existing operator token can administer sessions through the public CLI.
    // Cookie requests stay on the existing session/CSRF path; no agent scope changes.
    if (auth.error && !request.headers.has('Cookie') && validSessionSecret(this.env)) {
      const header = request.headers.get('Authorization') || '';
      const token = /^Bearer ([^\s]{1,4096})$/.exec(header)?.[1];
      if (token) {
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
        const given = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
        // A failed registered credential never upgrades to operator authority.
        if (this.sql.exec('SELECT id FROM credentials WHERE token_hash=?', base64url(digest)).toArray().length) return auth;
        const configured = adminTokenHash(this.env);
        if (configured) {
          let difference = 0;
          for (let i = 0; i < configured.length; i++) difference |= given.charCodeAt(i) ^ configured.charCodeAt(i);
          if (difference === 0) return { caller: { kind: 'human' as const, isAdmin: true,
            actor: 'human:' + tokenAdminSubject(configured) }, scopes: ['admin'] };
        }
      }
    }
    if (auth.caller?.kind === 'human' && !auth.caller.tokenId) {
      const session = await getSession(this.env, request);
      if (!session) return { error: Response.json({ error: 'unauthorized' }, { status: 401 }) };
      auth.caller.actor = 'human:' + session.sub;
    }
    return auth;
  }

  /** Human bearer allowlist recheck: current HUB_ADMINS (or the live
   * bootstrap token identity) must still approve the stored approver. Uses
   * the users table for Google emails so removal/failure closes access. */
  protected isHumanCredentialAllowed(sub: string, storedEmail: string): boolean {
    return isHumanCredentialAllowedFn(this.sql, this.env, sub, storedEmail);
  }

  /** Refresh-time human gate: strict admin scope, stable human:<sub> actor,
   * matching created_by_sub, and a currently allowlisted approver. */
  private isHumanRefreshAllowed(cred: any): boolean {
    return validateHumanCredentialRecord(this.sql, this.env, cred, Date.now()) !== null;
  }

  protected async authenticateBusRequest(request: Request): Promise<{ caller?: CallerContext; scopes?: string[]; error?: Response }> {
    const session = await getSession(this.env, request);
    if (session) {
      if (!session.isAdmin) {
        return { error: Response.json({ error: 'forbidden' }, { status: 403 }) };
      }
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        if (!verifyCsrf(request, session)) {
          return { error: Response.json({ error: 'CSRF token missing' }, { status: 403 }) };
        }
      }
      return {
        caller: { isAdmin: true, actor: 'admin', kind: 'human' },
        scopes: ['admin', 'bus:send', 'bus:read', 'host', 'lease:root', 'jobs:create']
      };
    }

    const authHeader = request.headers.get('Authorization');
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    const token = authHeader.substring(7).trim();
    if (!token) {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    const tokenHash = await sha256(token);
    const cred = this.sql.exec('SELECT * FROM credentials WHERE token_hash = ? AND revoked_at IS NULL', tokenHash).toArray()[0] as any;
    if (!cred) {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    const expiresMs = Date.parse(cred.expires_at);
    if (!Number.isFinite(expiresMs) || expiresMs <= Date.now()) {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    let scopes: string[] = [];
    try {
      scopes = typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : cred.scopes;
    } catch {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    if (!Array.isArray(scopes)) {
      return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
    }

    const lastUsedMs = cred.last_used_at ? Date.parse(cred.last_used_at) : 0;
    const nowMs = Date.now();
    if (!lastUsedMs || isNaN(lastUsedMs) || (nowMs - lastUsedMs) >= 300_000) {
      const nowIso = new Date().toISOString();
      this.sql.exec('UPDATE credentials SET last_used_at = ? WHERE id = ?', nowIso, cred.id);
      cred.last_used_at = nowIso;
    }

    const caller: CallerContext = {
      kind: cred.kind,
      tokenId: cred.id,
      tokenHash: cred.token_hash
    };
    if (cred.kind === 'host') {
      caller.machineId = cred.machine_id;
    } else if (cred.kind === 'routing-client') {
      if (!/^routing-client:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(cred.actor || '') ||
        scopes.length !== 1 || scopes[0] !== 'routing:submit') return { error: Response.json({ error: 'forbidden' }, { status: 403 }) };
      caller.actor = cred.actor;
    } else if (cred.kind === 'agent' || cred.kind === 'job') {
      caller.actor = cred.kind === 'agent' ? (credentialActor(this.sql, cred) ?? undefined) : (cred.actor || cred.subject);
      if (cred.kind === 'agent' && !caller.actor) return { error: Response.json({ error: 'forbidden' }, { status: 403 }) };
      if (cred.machine_id) caller.machineId = cred.machine_id;
      if (cred.kind === 'job') {
        caller.jobId = cred.subject;
      }
    } else if (cred.kind === 'human') {
      // Human/operator bearer: admin scope only via an approved allowlisted
      // principal. Identity comes only from the stored credential (actor
      // human:<approver-sub>, subject approver email); request fields never
      // authorize. Removed-admin, revoked and expired families fail closed.
      const verified = validateHumanCredentialRecord(this.sql, this.env, cred, Date.now());
      if (!verified) {
        return { error: Response.json({ error: 'unauthorized' }, { status: 401, headers: { 'WWW-Authenticate': 'Bearer' } }) };
      }
      caller.isAdmin = true;
      caller.actor = verified.actor;
    }

    return { caller, scopes };
  }

  private async parseSafeBusBody(request: Request, allowedKeys: Set<string>, optional: boolean = false, maxBytes: number = 16384): Promise<{ data?: Record<string, any>; error?: Response }> {
    if (!request.body) {
      if (optional) return { data: {} };
      return { error: Response.json({ error: 'missing_body' }, { status: 400 }) };
    }
    const reader = request.body.getReader();
    const chunks: Uint8Array[] = [];
    let totalBytes = 0;
    try {
      for (;;) {
        const part = await reader.read();
        if (part.done) break;
        totalBytes += part.value.byteLength;
        if (totalBytes > maxBytes) {
          await reader.cancel();
          return { error: Response.json({ error: 'body_too_large' }, { status: 413 }) };
        }
        chunks.push(part.value);
      }
    } finally {
      reader.releaseLock();
    }

    if (totalBytes === 0) {
      if (optional) return { data: {} };
      return { error: Response.json({ error: 'missing_body' }, { status: 400 }) };
    }

    const allBytes = new Uint8Array(totalBytes);
    let offset = 0;
    for (const chunk of chunks) {
      allBytes.set(chunk, offset);
      offset += chunk.byteLength;
    }

    let text: string;
    try {
      text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(allBytes);
    } catch {
      return { error: Response.json({ error: 'invalid_utf8' }, { status: 400 }) };
    }

    let parsed: any;
    try {
      parsed = JSON.parse(text);
    } catch {
      return { error: Response.json({ error: 'invalid_json' }, { status: 400 }) };
    }

    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { error: Response.json({ error: 'invalid_json_object' }, { status: 400 }) };
    }

    for (const k of Object.keys(parsed)) {
      if (!allowedKeys.has(k)) {
        return { error: Response.json({ error: 'unknown_fields', field: k }, { status: 400 }) };
      }
    }

    return { data: parsed };
  }

  private async handleBusRoutes(request: Request, path: string): Promise<Response | null> {
    if (!path.startsWith('/api/bus')) return null;

    const url = new URL(request.url);

    // GET /api/bus/actors
    if (path === '/api/bus/actors') {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }
      try {
        return Response.json(this.busService.getBusActors(auth.caller!));
      } catch (e: any) {
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // GET /api/bus/messages
    if (path === '/api/bus/messages' && request.method === 'GET') {
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }
      const actor = url.searchParams.get('actor');
      if (actor && actor.length > 128) {
        return Response.json({ error: 'invalid_actor' }, { status: 400 });
      }
      try {
        return Response.json(this.busService.getBusMessages(auth.caller!, actor));
      } catch (e: any) {
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // POST /api/bus/send
    if (path === '/api/bus/send') {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const allowedKeys = new Set(['from_actor', 'to_actor', 'kind', 'body', 'idempotency_key', 'thread_id', 'reply_to', 'lease_epoch']);
      const parsed = await this.parseSafeBusBody(request, allowedKeys);
      if (parsed.error) return parsed.error;
      const body = parsed.data!;

      if (typeof body.from_actor !== 'string' || !body.from_actor || body.from_actor.length > 128) {
        return Response.json({ error: 'invalid_from_actor' }, { status: 400 });
      }
      if (typeof body.to_actor !== 'string' || !body.to_actor || body.to_actor.length > 128) {
        return Response.json({ error: 'invalid_to_actor' }, { status: 400 });
      }
      if (typeof body.kind !== 'string' || !['note', 'handoff', 'reply', 'status', 'claim', 'release', 'scope_claim', 'sync_reply'].includes(body.kind)) {
        return Response.json({ error: 'invalid_kind' }, { status: 400 });
      }
      if (typeof body.body !== 'string') {
        return Response.json({ error: 'invalid_body' }, { status: 400 });
      }
      if (typeof body.idempotency_key !== 'string' || !body.idempotency_key || body.idempotency_key.length > 128) {
        return Response.json({ error: 'invalid_idempotency_key' }, { status: 400 });
      }
      if (body.thread_id !== undefined && (typeof body.thread_id !== 'string' || body.thread_id.length > 128)) {
        return Response.json({ error: 'invalid_thread_id' }, { status: 400 });
      }
      if (body.reply_to !== undefined && (typeof body.reply_to !== 'string' || body.reply_to.length > 128)) {
        return Response.json({ error: 'invalid_reply_to' }, { status: 400 });
      }
      if (body.lease_epoch !== undefined && (typeof body.lease_epoch !== 'number' || !Number.isInteger(body.lease_epoch) || body.lease_epoch < 0)) {
        return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
      }

      try {
        const result = this.busService.send(auth.caller!, {
          senderActor: body.from_actor,
          toActor: body.to_actor,
          kind: body.kind,
          body: body.body,
          idempotencyKey: body.idempotency_key,
          threadId: body.thread_id,
          replyTo: body.reply_to,
          leaseEpoch: body.lease_epoch,
          now: Date.now()
        });
        return Response.json({ id: result.id, thread_id: result.threadId, threadId: result.threadId });
      } catch (e: any) {
        if (e.message === 'body_too_large') return Response.json({ error: 'body_too_large' }, { status: 413 });
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        if (e.message === 'conflict') return Response.json({ error: 'conflict' }, { status: 409 });
        if (e.message === 'invalid_lease_epoch') return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        if (e.message === 'unknown_recipient') return Response.json({ error: 'unknown_recipient' }, { status: 404 });
        if (e.message === 'invalid_reply_to') return Response.json({ error: 'invalid_reply_to' }, { status: 400 });
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // POST /api/bus/reply
    if (path === '/api/bus/reply') {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const allowedKeys = new Set(['from_actor', 'to_actor', 'kind', 'body', 'idempotency_key', 'thread_id', 'reply_to', 'lease_epoch']);
      const parsed = await this.parseSafeBusBody(request, allowedKeys);
      if (parsed.error) return parsed.error;
      const body = parsed.data!;

      if (!body.reply_to || typeof body.reply_to !== 'string' || body.reply_to.length > 128) {
        return Response.json({ error: 'missing_reply_to' }, { status: 400 });
      }
      if (typeof body.from_actor !== 'string' || !body.from_actor || body.from_actor.length > 128) {
        return Response.json({ error: 'invalid_from_actor' }, { status: 400 });
      }
      if (typeof body.to_actor !== 'string' || !body.to_actor || body.to_actor.length > 128) {
        return Response.json({ error: 'invalid_to_actor' }, { status: 400 });
      }
      if (typeof body.kind !== 'string' || !['note', 'handoff', 'reply', 'status', 'claim', 'release', 'scope_claim', 'sync_reply'].includes(body.kind)) {
        return Response.json({ error: 'invalid_kind' }, { status: 400 });
      }
      if (typeof body.body !== 'string') {
        return Response.json({ error: 'invalid_body' }, { status: 400 });
      }
      if (typeof body.idempotency_key !== 'string' || !body.idempotency_key || body.idempotency_key.length > 128) {
        return Response.json({ error: 'invalid_idempotency_key' }, { status: 400 });
      }

      try {
        const result = this.busService.send(auth.caller!, {
          senderActor: body.from_actor,
          toActor: body.to_actor,
          kind: body.kind,
          body: body.body,
          idempotencyKey: body.idempotency_key,
          threadId: body.thread_id,
          replyTo: body.reply_to,
          leaseEpoch: body.lease_epoch,
          now: Date.now()
        });
        return Response.json({ id: result.id, thread_id: result.threadId, threadId: result.threadId });
      } catch (e: any) {
        if (e.message === 'body_too_large') return Response.json({ error: 'body_too_large' }, { status: 413 });
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        if (e.message === 'conflict') return Response.json({ error: 'conflict' }, { status: 409 });
        if (e.message === 'invalid_lease_epoch') return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        if (e.message === 'invalid_reply_to') return Response.json({ error: 'invalid_reply_to' }, { status: 400 });
        if (e.message === 'unknown_recipient') return Response.json({ error: 'unknown_recipient' }, { status: 404 });
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // GET /api/bus/inbox
    if (path === '/api/bus/inbox') {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      for (const k of url.searchParams.keys()) {
        if (!['actor', 'limit', 'cursor', 'before'].includes(k)) {
          return Response.json({ error: 'unknown_query_param', param: k }, { status: 400 });
        }
      }

      const actor = url.searchParams.get('actor');
      if (!actor) return Response.json({ error: 'missing_actor' }, { status: 400 });

      let limit = 50;
      if (url.searchParams.has('limit')) {
        const rawLimit = url.searchParams.get('limit')!;
        if (!/^\d+$/.test(rawLimit)) return Response.json({ error: 'invalid_limit' }, { status: 400 });
        const parsed = parseInt(rawLimit, 10);
        if (parsed < 1 || parsed > 100) return Response.json({ error: 'invalid_limit' }, { status: 400 });
        limit = parsed;
      }

      const cursor = url.searchParams.get('cursor') || url.searchParams.get('before') || undefined;
      if (cursor && cursor.length > 128) return Response.json({ error: 'invalid_cursor' }, { status: 400 });

      try {
        const messages = this.busService.inbox(auth.caller!, actor, limit, cursor);
        return Response.json(messages);
      } catch (e: any) {
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // POST /api/bus/native-sync — host-attested bridge for native aplexer work.
    // The host daemon observes local `a work join` declarations and `work.*`
    // mailbox envelopes (which cannot hold Hub tokens) and mirrors redacted
    // state here with its host credential. Callers are host (bound to their
    // own machine_id) or admin. Synthetic actors use the design-recommended
    // `native:<machine>:<tag>[<short>]` form; no agents row is required.
    if (path === '/api/bus/native-sync') {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      const caller = auth.caller!;
      const isHost = caller.kind === 'host' && !!caller.machineId;
      if (!caller.isAdmin && !isHost) {
        return Response.json({ error: 'forbidden', reason: 'host_or_admin_only' }, { status: 403 });
      }
      if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const parsed = await this.parseSafeBusBody(request, new Set(['claims', 'messages']), true, 131072);
      if (parsed.error) return parsed.error;
      const body = parsed.data!;
      const claimItems = body.claims === undefined ? [] : body.claims;
      const messageItems = body.messages === undefined ? [] : body.messages;
      if (!Array.isArray(claimItems) || !Array.isArray(messageItems) ||
          claimItems.length > 64 || messageItems.length > 64) {
        return Response.json({ error: 'invalid_native_sync' }, { status: 400 });
      }

      const now = Date.now();
      const errors: Array<{ index: number; kind: string; reason: string }> = [];
      let syncedClaims = 0;
      let releasedClaims = 0;
      let syncedMessages = 0;
      let dedupedMessages = 0;
      const projectLabel = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
      const actorSeg = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

      const holderActor = (holder: any): string | null => {
        if (!holder || typeof holder !== 'object') return null;
        const { machine_id, tag, session_id } = holder;
        if (typeof machine_id !== 'string' || typeof tag !== 'string' || typeof session_id !== 'string') return null;
        if (!actorSeg.test(machine_id) || !actorSeg.test(tag)) return null;
        // A host attests only its own machine's sessions; admins may relay any.
        if (isHost && machine_id !== caller.machineId) return null;
        return formatNativeActor(machine_id, tag, session_id);
      };

      for (let i = 0; i < claimItems.length; i++) {
        const item = claimItems[i];
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
          errors.push({ index: i, kind: 'claim', reason: 'item_must_be_object' });
          continue;
        }
        const data = (item as any).data;
        const invalid = validateWorkClaimData(data);
        if (invalid) {
          errors.push({ index: i, kind: 'claim', reason: `invalid_claim_data:${invalid}`.slice(0, 120) });
          continue;
        }
        if (!projectLabel.test(data.project)) {
          errors.push({ index: i, kind: 'claim', reason: 'invalid_project' });
          continue;
        }
        const actor = holderActor(data.holder);
        if (!actor) {
          errors.push({ index: i, kind: 'claim', reason: 'invalid_holder_machine' });
          continue;
        }
        const clean = redactClaimData(data);
        const remaining = (item as any).remaining_ttl_ms;
        const ttlMs = typeof remaining === 'number' && Number.isFinite(remaining) && remaining >= 0
          ? Math.min(Math.floor(remaining), clean.lease_ttl_sec * 1000)
          : clean.lease_ttl_sec * 1000;
        const expiresAt = now + ttlMs;
        const sessionState = (item as any).session_state;
        const stateValue = sessionState === 'stale' || sessionState === 'exited' ? sessionState : 'running';
        const scopes: string[] = Array.isArray(clean.scopes) && clean.scopes.length > 0 ? clean.scopes : ['*'];
        const event = clean.event as string;

        if (event === 'complete' || event === 'abandon') {
          const live = this.sql.exec(
            "SELECT id FROM claims WHERE native_claim_id = ? AND claim_source = 'native' AND released_at IS NULL",
            clean.claim_id,
          ).toArray() as any[];
          for (const row of live) {
            this.sql.exec('UPDATE claims SET released_at = ? WHERE id = ?', now, row.id);
            releasedClaims++;
          }
          continue;
        }

        // Upsert per scope with stable row ids: refresh live rows, insert
        // missing scopes, drop live scopes the declaration no longer holds.
        const live = this.sql.exec(
          "SELECT id, scope FROM claims WHERE native_claim_id = ? AND claim_source = 'native' AND released_at IS NULL",
          clean.claim_id,
        ).toArray() as any[];
        const liveByScope = new Map(live.map((r: any) => [r.scope, r.id]));
        const wantScopes = new Set(scopes);
        for (const [scope, id] of liveByScope) {
          if (!wantScopes.has(scope)) {
            this.sql.exec('UPDATE claims SET released_at = ? WHERE id = ?', now, id);
            releasedClaims++;
          }
        }
        for (const scope of wantScopes) {
          const redactedScope = String(redactForBus(scope));
          const existingId = liveByScope.get(scope);
          if (existingId) {
            this.sql.exec(
              'UPDATE claims SET project = ?, holder = ?, expires_at = ?, work_key = ?, claim_mode = ?, worktree = ?, session_state = ? WHERE id = ?',
              clean.project, actor, expiresAt, clean.work_key, clean.mode,
              typeof clean.worktree === 'string' ? clean.worktree : null, stateValue, existingId,
            );
          } else {
            this.sql.exec(
              "INSERT INTO claims (id, project, scope, holder, created_at, expires_at, released_at, work_key, claim_mode, claim_source, worktree, session_state, native_claim_id) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'native', ?, ?, ?)",
              generateUlid(now), clean.project, redactedScope, actor, now, expiresAt,
              clean.work_key, clean.mode,
              typeof clean.worktree === 'string' ? String(redactForBus(clean.worktree)) : null,
              stateValue, clean.claim_id,
            );
          }
          syncedClaims++;
        }
      }

      for (let i = 0; i < messageItems.length; i++) {
        const item = messageItems[i];
        if (!item || typeof item !== 'object' || Array.isArray(item)) {
          errors.push({ index: i, kind: 'message', reason: 'item_must_be_object' });
          continue;
        }
        const m = item as any;
        if (typeof m.native_id !== 'string' || !m.native_id || m.native_id.length > 64 || /[\x00-\x1f\x7f\s]/.test(m.native_id)) {
          errors.push({ index: i, kind: 'message', reason: 'invalid_native_id' });
          continue;
        }
        if (!isWorkMessageKind(m.kind)) {
          errors.push({ index: i, kind: 'message', reason: 'invalid_kind' });
          continue;
        }
        if (typeof m.project !== 'string' || !projectLabel.test(m.project)) {
          errors.push({ index: i, kind: 'message', reason: 'invalid_project' });
          continue;
        }
        const actor = holderActor(m.from);
        if (!actor) {
          errors.push({ index: i, kind: 'message', reason: 'invalid_from_machine' });
          continue;
        }
        if (typeof m.body !== 'string' || !m.body || m.body.length > 4000) {
          errors.push({ index: i, kind: 'message', reason: 'invalid_body' });
          continue;
        }
        const toActor = `project:${m.project}`;
        const idempotencyKey = `aplexer-msg:${m.native_id}`;
        const existing = this.sql.exec(
          'SELECT id FROM messages WHERE from_actor = ? AND idempotency_key = ?',
          actor, idempotencyKey,
        ).toArray()[0] as any;
        if (existing) {
          dedupedMessages++;
          continue;
        }
        let createdAt = now;
        if (typeof m.created_at_ms === 'number' && Number.isFinite(m.created_at_ms) && m.created_at_ms > 0 && m.created_at_ms <= now + 60000) {
          createdAt = Math.floor(m.created_at_ms);
        }
        const id = generateUlid(createdAt);
        this.sql.exec(
          'INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, reply_to, body, idempotency_key, lease_epoch, created_at, expires_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?)',
          id, actor, toActor, m.kind, id, String(redactForBus(m.body)).slice(0, 2000),
          idempotencyKey, createdAt, createdAt + 30 * 24 * 3600 * 1000,
        );
        syncedMessages++;
      }

      return Response.json({
        status: 'ok',
        synced_claims: syncedClaims,
        released_claims: releasedClaims,
        synced_messages: syncedMessages,
        deduped_messages: dedupedMessages,
        errors: errors.slice(0, 20),
      });
    }

    // POST & GET /api/bus/claims
    if (path === '/api/bus/claims') {
      if (request.method === 'POST') {
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
          return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
        }

        const allowedKeys = new Set(['project', 'scope', 'holder', 'ttl_ms', 'lease_epoch']);
        const parsed = await this.parseSafeBusBody(request, allowedKeys);
        if (parsed.error) return parsed.error;
        const body = parsed.data!;

        if (typeof body.project !== 'string' || !body.project || body.project.length > 64) {
          return Response.json({ error: 'invalid_project' }, { status: 400 });
        }
        if (typeof body.scope !== 'string' || !body.scope || body.scope.length > 256) {
          return Response.json({ error: 'invalid_scope' }, { status: 400 });
        }
        if (typeof body.holder !== 'string' || !body.holder || body.holder.length > 128) {
          return Response.json({ error: 'invalid_holder' }, { status: 400 });
        }
        if (body.lease_epoch !== undefined && (typeof body.lease_epoch !== 'number' || !Number.isInteger(body.lease_epoch) || body.lease_epoch < 0)) {
          return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        }
        let ttlMs = 3600000;
        if (body.ttl_ms !== undefined) {
          if (typeof body.ttl_ms !== 'number' || !Number.isInteger(body.ttl_ms) || body.ttl_ms < 1) {
            return Response.json({ error: 'invalid_ttl' }, { status: 400 });
          }
          ttlMs = body.ttl_ms;
        }

        try {
          const result = this.busService.claim(auth.caller!, body.project, body.scope, body.holder, ttlMs, Date.now(), body.lease_epoch);
          return Response.json({ id: result.id });
        } catch (e: any) {
          if (e.message === 'invalid_scope') return Response.json({ error: 'invalid_scope' }, { status: 400 });
          if (e.message === 'invalid_ttl') return Response.json({ error: 'invalid_ttl' }, { status: 400 });
          if (e.message === 'invalid_lease_epoch') return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
          if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
            return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
          }
          if (e.message === 'conflict') return Response.json({ error: 'conflict' }, { status: 409 });
          return Response.json({ error: 'Internal Server Error' }, { status: 500 });
        }
      }

      if (request.method === 'GET') {
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
          return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
        }

        const project = url.searchParams.get('project') || undefined;
        const holder = url.searchParams.get('holder') || undefined;
        const active = url.searchParams.get('active') !== 'false';

        try {
          const claims = this.busService.listClaims(auth.caller!, project, holder, active);
          return Response.json(claims);
        } catch (e: any) {
          if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
            return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
          }
          return Response.json({ error: 'Internal Server Error' }, { status: 500 });
        }
      }

      return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET, POST' } });
    }

    // GET /api/bus/activity — read-only "who is already on this" aggregation.
    // No new store and no new protocol: active claims come from the existing
    // claims table (via BusService.listClaims) and recent traffic from the
    // existing messages table (via BusService.getBusMessages). A workspace is
    // resolved to a claim project through the existing fleet_claim_policy
    // table (workspace -> project); nothing is invented when no row matches.
    if (path === '/api/bus/activity') {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      for (const k of url.searchParams.keys()) {
        if (!['workspace', 'project', 'holder', 'actor', 'limit', 'all'].includes(k)) {
          return Response.json({ error: 'unknown_query_param', param: k }, { status: 400 });
        }
      }

      const workspace = url.searchParams.get('workspace') || undefined;
      if (workspace && (workspace.length > 2048 || /[\x00-\x1f\x7f]/.test(workspace))) {
        return Response.json({ error: 'invalid_workspace' }, { status: 400 });
      }
      let project = url.searchParams.get('project') || undefined;
      if (project && (project.length > 64 || !project.trim())) {
        return Response.json({ error: 'invalid_project' }, { status: 400 });
      }
      const holder = url.searchParams.get('holder') || undefined;
      if (holder && holder.length > 128) {
        return Response.json({ error: 'invalid_holder' }, { status: 400 });
      }
      const actor = url.searchParams.get('actor') || undefined;
      if (actor && actor.length > 128) {
        return Response.json({ error: 'invalid_actor' }, { status: 400 });
      }
      let limit = 20;
      if (url.searchParams.has('limit')) {
        const rawLimit = url.searchParams.get('limit')!;
        if (!/^\d+$/.test(rawLimit)) return Response.json({ error: 'invalid_limit' }, { status: 400 });
        const parsed = parseInt(rawLimit, 10);
        if (parsed < 1 || parsed > 100) return Response.json({ error: 'invalid_limit' }, { status: 400 });
        limit = parsed;
      }
      const activeOnly = url.searchParams.get('all') !== 'true';

      try {
        // Workspace -> project through the existing claim-admission policy.
        // An explicit ?project= wins; a workspace with no policy row simply
        // yields no project-scoped claims (never a guessed mapping).
        let claimPolicy: { project: string | null; require_claim: boolean } | null = null;
        if (workspace) {
          try {
            const row: any = this.sql.exec(
              'SELECT project, require_claim FROM fleet_claim_policy WHERE workspace = ?', workspace,
            ).toArray()[0];
            if (row) claimPolicy = { project: row.project ?? null, require_claim: !!row.require_claim };
          } catch { claimPolicy = null; }
        }
        const effectiveProject = project ?? claimPolicy?.project ?? undefined;

        // A workspace query that resolves to no Hub project cannot scope
        // claims to that workspace: return an empty claim set (with an
        // explanatory notice) rather than caller-global or hub-global rows
        // under a workspace heading. Pass ?project= explicitly to scope.
        const unmappedWorkspace = Boolean(workspace && !project && !claimPolicy?.project);
        const rawClaims = unmappedWorkspace
          ? []
          : this.busService.listClaims(auth.caller!, effectiveProject, holder, activeOnly);
        // Presentation enrichment (additive): native bridged rows carry the
        // work_key/mode/source the host attested; legacy Hub rows default to
        // a Hub-native edit. task_id links task:<uuid> keys to intake.
        const nowMs = Date.now();
        const claims = rawClaims.map((c: any) => ({
          ...c,
          source: c.claim_source || 'hub',
          mode: c.claim_mode || 'edit',
          task_id: taskIdFromWorkKey(c.work_key),
          stale: typeof c.expires_at === 'number' ? c.expires_at <= nowMs : false,
        }));
        const messageActor = actor || holder || undefined;
        const scopedMessages = this.busService.getBusMessages(auth.caller!, messageActor);
        // Bridged native work.* envelopes are addressed to project:<project>
        // (no Hub agents row exists for them), so the scoped inbox query
        // above cannot match them: merge them explicitly whenever the scope
        // resolves to a project. Content is redacted coordination data.
        let messages = scopedMessages;
        if (effectiveProject && !unmappedWorkspace) {
          try {
            const nativeRows = this.sql.exec(
              'SELECT * FROM messages WHERE to_actor = ? AND expires_at > ? ORDER BY created_at DESC LIMIT ?',
              `project:${effectiveProject}`, nowMs, limit,
            ).toArray() as any[];
            const seen = new Set(scopedMessages.map((r: any) => r.id));
            messages = [...scopedMessages, ...nativeRows.filter((r: any) => !seen.has(r.id))]
              .sort((a: any, b: any) => (b.created_at || 0) - (a.created_at || 0));
          } catch { messages = scopedMessages; }
        }
        messages = messages.slice(0, limit);
        const activeClaims = claims.filter((c: any) => !c.released_at);
        const notice = unmappedWorkspace
          ? 'no claim policy maps this workspace to a Hub project — claims are not scoped to it; pass ?project= explicitly or configure fleet claim policy'
          : activeClaims.length === 0 && messages.length === 0
            ? 'no active work declarations or recent bus activity in scope'
            : `${activeClaims.length} active work declaration${activeClaims.length === 1 ? '' : 's'}, ` +
              `${messages.length} recent bus message${messages.length === 1 ? '' : 's'} in scope — ` +
              'someone may already be on this; read the rows before starting overlapping work';
        return Response.json({
          workspace: workspace ?? null,
          project: effectiveProject ?? null,
          claim_policy: claimPolicy,
          claims,
          recent_messages: messages,
          notice,
        });
      } catch (e: any) {
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // POST /api/bus/claims/:target/release
    if (path.startsWith('/api/bus/claims/') && path.endsWith('/release')) {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const encodedTarget = path.slice('/api/bus/claims/'.length, -'/release'.length);
      const target = decodeURIComponent(encodedTarget);

      const allowedKeys = new Set(['lease_epoch']);
      const parsed = await this.parseSafeBusBody(request, allowedKeys, true);
      if (parsed.error) return parsed.error;
      let leaseEpoch: number | undefined;
      if (parsed.data?.lease_epoch !== undefined) {
        if (typeof parsed.data.lease_epoch !== 'number' || !Number.isInteger(parsed.data.lease_epoch) || parsed.data.lease_epoch < 0) {
          return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        }
        leaseEpoch = parsed.data.lease_epoch;
      }

      try {
        this.busService.releaseClaim(auth.caller!, target, Date.now(), leaseEpoch);
        return Response.json({ status: 'ok' });
      } catch (e: any) {
        if (e.message === 'invalid_scope') return Response.json({ error: 'invalid_scope' }, { status: 400 });
        if (e.message === 'invalid_lease_epoch') return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // POST /api/bus/messages/:id/(ack|accept|complete)
    const stageMatch = path.match(/^\/api\/bus\/messages\/([^/]+)\/(ack|accept|complete)$/);
    if (stageMatch) {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:send') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const messageId = decodeURIComponent(stageMatch[1]);
      const stage = stageMatch[2] as 'ack' | 'accept' | 'complete';

      const allowedKeys = new Set(['lease_epoch']);
      const parsed = await this.parseSafeBusBody(request, allowedKeys, true);
      if (parsed.error) return parsed.error;
      let leaseEpoch: number | undefined;
      if (parsed.data?.lease_epoch !== undefined) {
        if (typeof parsed.data.lease_epoch !== 'number' || !Number.isInteger(parsed.data.lease_epoch) || parsed.data.lease_epoch < 0) {
          return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        }
        leaseEpoch = parsed.data.lease_epoch;
      }

      try {
        if (stage === 'ack') this.busService.ack(auth.caller!, messageId, Date.now(), leaseEpoch);
        else if (stage === 'accept') this.busService.accept(auth.caller!, messageId, Date.now(), leaseEpoch);
        else if (stage === 'complete') this.busService.complete(auth.caller!, messageId, Date.now(), leaseEpoch);
        return Response.json({ status: 'ok', id: messageId, stage });
      } catch (e: any) {
        if (e.message === 'not_found') return Response.json({ error: 'not_found' }, { status: 404 });
        if (e.message === 'invalid_lease_epoch') return Response.json({ error: 'invalid_lease_epoch' }, { status: 403 });
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        if (e.message === 'invalid_stage') return Response.json({ error: 'invalid_stage' }, { status: 400 });
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    // GET /api/bus/messages/:id
    const showMatch = path.match(/^\/api\/bus\/messages\/([^/]+)$/);
    if (showMatch) {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('bus:read') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const messageId = decodeURIComponent(showMatch[1]);

      try {
        const msg = this.busService.show(auth.caller!, messageId);
        return Response.json(msg);
      } catch (e: any) {
        if (e.message === 'not_found') return Response.json({ error: 'not_found' }, { status: 404 });
        if (e.message === 'unauthorized' || (e.message && e.message.startsWith('missing_m4_root_authority'))) {
          return Response.json({ error: 'forbidden', reason: e.message }, { status: 403 });
        }
        return Response.json({ error: 'Internal Server Error' }, { status: 500 });
      }
    }

    return null;
  }

  public setHealthService(health: any) {
    this.jobsService.setHealthService(health);
  }

  public getEffectiveHealth(tag: string): { state?: string } | null {
    if ((this.jobsService as any).healthService?.getEffectiveHealth) {
      return (this.jobsService as any).healthService.getEffectiveHealth(tag);
    }
    const address = registryAddress(tag);
    if (!address) return { state: 'unknown' };
    const rows = address.project
      ? this.sql.exec('SELECT state, health_state FROM agents WHERE project = ? AND tag = ?', address.project, address.tag).toArray()
      : this.sql.exec('SELECT state, health_state FROM agents WHERE tag = ?', address.tag).toArray();
    if (rows.length > 1) return { state: 'unknown' };
    const agent = rows[0] as any;
    if (agent) {
      return { state: agent.state || agent.health_state || 'unknown' };
    }
    return null;
  }

  private async handleJobsRoutes(request: Request, path: string, url: URL): Promise<Response | null> {
    const roleRoute = path.match(/^\/api\/roles\/([^/]+)\/([^/]+)(?:\/(jobs))?$/);
    if (roleRoute) {
      const [, project, name, jobs] = roleRoute;
      const key = roleName(project, name);
      if (!key) return Response.json({ error: 'invalid_role_name' }, { status: 400 });
      if (!jobs) {
        if (!['GET','PUT'].includes(request.method)) return new Response('Method Not Allowed', { status: 405 });
        // Configuration uses the existing operator session and CSRF, never a bearer grant.
        const session = await getSession(this.env, request);
        if (!session?.isAdmin) return Response.json({ error: 'operator_session_required' }, { status: 403 });
        if (request.method === 'GET') {
          const role = this.jobsService.roles.get(key);
          return Response.json(role ? { name: key, generation: role.generation, config: JSON.parse(role.config),
            lease: this.sql.exec('SELECT * FROM leases WHERE name=?', key).toArray()[0] } : { name: key, configured: false });
        }
        if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });
        const body = await safeJson(request);
        if (!body || Object.keys(body).some(k => !['config','expected_generation'].includes(k))) return Response.json({ error: 'invalid_configuration' }, { status: 400 });
        const result = this.jobsService.configureRole(project, name, body.config, body.expected_generation);
        return Response.json(result, { status: result.error ? 409 : 200 });
      }
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (auth.caller?.kind !== 'host' || !auth.scopes?.includes('host') || !auth.caller.machineId || !auth.caller.tokenId) return Response.json({ error: 'enrolled_host_required' }, { status: 403 });
      const result = this.jobsService.offerRoleJob(project, name, auth.caller.machineId, auth.caller.tokenId, await safeJson(request));
      return Response.json(result, { status: result.error ? 409 : 201 });
    }
    const roleExecution = path.match(/^\/api\/jobs\/([^/]+)\/execute$/);
    if (roleExecution) {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      const body = await safeJson(request);
      if (!body || Object.keys(body).some(k => !['epoch','generation'].includes(k)) || !Number.isSafeInteger(body.epoch) || !Number.isSafeInteger(body.generation)) return Response.json({ error: 'invalid_binding' }, { status: 400 });
      if (auth.caller?.kind !== 'job' || auth.caller.jobId !== roleExecution[1]) return Response.json({ error: 'job_credential_required' }, { status: 403 });
      const token = request.headers.get('Authorization')!.substring(7).trim();
      const result = await this.jobsService.commitRoleExecution(roleExecution[1], token, body.epoch, body.generation);
      return Response.json(result, { status: result.error ? 409 : 200 });
    }
    const isJobsPath = path.startsWith('/api/jobs');
    const isRootPath = path.startsWith('/api/root');
    const isHostOffers = path === '/api/host/offers';

    if (!isJobsPath && !isRootPath && !isHostOffers) return null;

    // 1. GET /api/jobs/offers or GET /api/host/offers
    if (path === '/api/jobs/offers' || path === '/api/host/offers') {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('host') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'host_or_admin_scope_required' }, { status: 403 });
      }

      let hostId = auth.caller?.machineId;
      if (auth.caller?.isAdmin && url.searchParams.get('host_id')) {
        hostId = url.searchParams.get('host_id')!;
      }
      if (!hostId) {
        return Response.json({ error: 'forbidden', reason: 'machine_binding_required' }, { status: 403 });
      }

      const nowIso = new Date().toISOString();
      const rows = this.sql.exec(
        `SELECT id, kind, payload, status, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, created_at, actor, role_name, role_generation
         FROM jobs
         WHERE status = 'offered'
           AND (offer_host_id = ? OR offer_machine_id = ?)
           AND (offer_expires_at IS NULL OR offer_expires_at > ?)
         ORDER BY created_at DESC`,
        hostId, hostId, nowIso
      ).toArray() as any[];
      // Root scheduling flag: root-run offers are not listed while disabled.
      const visibleRows = isRootSchedulingEnabled(this.sql) ? rows : rows.filter((r: any) => r.kind !== 'root-run');

      const offers = visibleRows.map(r => {
        let payload = r.payload;
        try { if (typeof payload === 'string') payload = JSON.parse(payload); } catch {}
        return {
          id: r.id,
          kind: r.kind,
          payload,
          status: r.status,
          deadline_at: r.deadline_at,
          offer_host_id: r.offer_host_id,
          offer_machine_id: r.offer_machine_id,
          offer_expires_at: r.offer_expires_at,
          created_at: r.created_at,
          actor: r.actor,
          ...(r.role_name ? { role_binding: this.jobsService.roleJobBinding(r) } : {})
        };
      });

      return Response.json({ offers });
    }

    // 2. Root endpoints (/api/root/... or /api/jobs/root/...)
    // Root status
    if (path === '/api/root/status' || path === '/api/jobs/root/status') {
      if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('admin') && !auth.scopes?.includes('host') && !auth.scopes?.includes('lease:root') && !auth.scopes?.includes('jobs:create')) {
        return Response.json({ error: 'forbidden', reason: 'insufficient_scope' }, { status: 403 });
      }
      return Response.json(this.jobsService.getRootStatus());
    }

    // Root takeover
    if (path === '/api/root/takeover' || path === '/api/jobs/root/takeover') {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('host') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'host_or_admin_scope_required' }, { status: 403 });
      }

      const body = await safeJson(request);
      if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

      let hostId = auth.caller?.machineId;
      if (auth.caller?.isAdmin && body.host_id) {
        hostId = body.host_id;
      }
      if (!hostId) {
        return Response.json({ error: 'forbidden', reason: 'machine_binding_required' }, { status: 403 });
      }

      const candidateJobId = body.candidate_job_id;
      if (!candidateJobId || typeof candidateJobId !== 'string') {
        return Response.json({ error: 'invalid_candidate_job_id' }, { status: 400 });
      }

      const expectedEpoch = parseEpoch(body.expected_epoch);
      if (expectedEpoch === null) {
        return Response.json({ error: 'invalid_expected_epoch' }, { status: 400 });
      }

      const reason = typeof body.reason === 'string' ? body.reason : 'stale_or_hardstop';
      const res = await this.jobsService.takeoverRoot(candidateJobId, hostId, expectedEpoch, reason);
      if (res.error) {
        return Response.json(res, { status: 409 });
      }
      return Response.json(res);
    }

    // Root verify
    if (path === '/api/root/verify' || path === '/api/jobs/root/verify') {
      if (request.method !== 'GET' && request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET, POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;

      // Scope check: only admin, host, or lease:root
      if (!auth.scopes?.includes('admin') && !auth.scopes?.includes('host') && !auth.scopes?.includes('lease:root')) {
        return Response.json({ valid: false, error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const body = await safeJson(request) || {};
      const jobId = body.job_id || url.searchParams.get('job_id') || auth.caller?.jobId || null;
      const epoch = parseEpoch(body.epoch ?? url.searchParams.get('epoch'));
      if (epoch === null) {
        return Response.json({ valid: false, status: 'error', reason: 'invalid_epoch' }, { status: 400 });
      }

      if (auth.caller?.kind === 'job' && auth.caller?.jobId && jobId && auth.caller.jobId !== jobId) {
        return Response.json({ valid: false, status: 'unauthorized', reason: 'job_token_mismatch' }, { status: 403 });
      }

      // Check the root lease
      const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      if (!lease || lease.epoch !== epoch) {
        return Response.json({ valid: false, status: 'fenced', reason: 'stale_epoch', current_epoch: lease?.epoch ?? 0 }, { status: 409 });
      }

      if (!lease.expires_at || new Date(lease.expires_at).getTime() <= Date.now()) {
        return Response.json({ valid: false, status: 'expired', reason: 'lease_expired', current_epoch: lease.epoch }, { status: 403 });
      }

      const targetJobId = jobId || lease.holder_job_id;
      if (!targetJobId) {
        return Response.json({ valid: false, status: 'unauthorized', reason: 'no_holder_job' }, { status: 403 });
      }

      const jobRow = this.sql.exec("SELECT status, holder_host_id, holder_machine_id, claim_token_hash FROM jobs WHERE id = ?", targetJobId).toArray()[0] as any;
      if (!jobRow || jobRow.status !== 'running') {
        return Response.json({
          valid: false,
          status: 'unauthorized',
          reason: jobRow ? `job_not_running (status: ${jobRow.status})` : 'job_not_found',
          current_epoch: lease.epoch
        }, { status: 403 });
      }

      // Check if job token was revoked
      if (jobRow.claim_token_hash) {
        const cred = this.sql.exec("SELECT revoked_at FROM credentials WHERE token_hash = ?", jobRow.claim_token_hash).toArray()[0] as any;
        if (!cred || (cred.revoked_at && cred.revoked_at.trim().length > 0)) {
          return Response.json({ valid: false, status: 'unauthorized', reason: 'revoked_token', current_epoch: lease.epoch }, { status: 403 });
        }
      }

      if (!auth.caller?.isAdmin && auth.scopes?.includes('host')) {
        if (!auth.caller?.machineId || auth.caller.machineId !== (jobRow.holder_host_id || jobRow.holder_machine_id)) {
          return Response.json({ valid: false, status: 'unauthorized', reason: 'host_mismatch', current_epoch: lease.epoch }, { status: 403 });
        }
      }

      const authHeader = request.headers.get('Authorization');
      const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : null;
      const jobToken = body.job_token || url.searchParams.get('job_token') || bearerToken;

      const isHostSupervisor = auth.scopes?.includes('host') || auth.caller?.isAdmin || false;
      let hostId = auth.caller?.machineId;
      if (auth.caller?.isAdmin && (body.host_id || url.searchParams.get('host_id'))) {
        hostId = body.host_id || url.searchParams.get('host_id');
      }

      const res = await this.jobsService.verifyRootEpoch(jobId, jobToken, epoch, isHostSupervisor, hostId);
      if (!res.valid) {
        return Response.json(res, { status: res.status === 'fenced' ? 409 : 403 });
      }
      return Response.json(res);
    }

    // Root actions (singular & plural)
    if (path === '/api/root/action' || path === '/api/root/actions' || path === '/api/jobs/root/actions') {
      if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;

      // Scope check: must have lease:root, host, or admin
      if (!auth.scopes?.includes('lease:root') && !auth.scopes?.includes('admin') && !auth.scopes?.includes('host')) {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const body = await safeJson(request);
      if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

      const jobId = body.job_id;
      if (!jobId || typeof jobId !== 'string') {
        return Response.json({ error: 'missing_job_id' }, { status: 400 });
      }

      const epoch = parseEpoch(body.epoch ?? body.lease_epoch);
      if (epoch === null) {
        return Response.json({ error: 'invalid_epoch' }, { status: 400 });
      }

      const action = body.action;
      if (!action || typeof action !== 'object') {
        return Response.json({ error: 'missing_action' }, { status: 400 });
      }



      const jobRow = this.sql.exec("SELECT holder_host_id, holder_machine_id, claim_token_hash, status FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
      if (!jobRow) {
        return Response.json({ error: 'not_found', reason: 'job_not_found' }, { status: 404 });
      }

      const authHeader = request.headers.get('Authorization');
      const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : null;
      const jobTokenPlain = typeof body.job_token === 'string' && body.job_token ? body.job_token : bearerToken;

      let hostId: string | null = null;
      if (auth.caller?.isAdmin) {
        hostId = body.host_id || auth.caller?.machineId || jobRow.holder_host_id || jobRow.holder_machine_id || '';
      } else if (auth.scopes?.includes('host')) {
        hostId = auth.caller?.machineId || null;
        if (!hostId || hostId !== (jobRow.holder_host_id || jobRow.holder_machine_id)) {
          return Response.json({ error: 'forbidden', reason: 'host_mismatch' }, { status: 403 });
        }
      } else if (auth.scopes?.includes('lease:root')) {
        if (auth.caller?.jobId && auth.caller.jobId !== jobId) {
          return Response.json({ error: 'forbidden', reason: 'job_mismatch' }, { status: 403 });
        }
        if (!auth.caller?.tokenHash || !jobRow.claim_token_hash || auth.caller.tokenHash !== jobRow.claim_token_hash) {
          return Response.json({ error: 'forbidden', reason: 'token_mismatch' }, { status: 403 });
        }
        hostId = jobRow.holder_host_id || jobRow.holder_machine_id || '';
      } else {
        return Response.json({ error: 'forbidden', reason: 'scope_missing' }, { status: 403 });
      }

      const sessionId = typeof body.session_id === 'string' ? body.session_id : undefined;

      const res = await this.jobsService.commitRootAction(jobId, hostId || '', epoch, action, sessionId, jobTokenPlain);
      if (!res.success) {
        const statusCode = res.status === 'fenced' ? 409 : (res.status === 'unauthorized' ? 403 : 400);
        return Response.json(res, { status: statusCode });
      }
      return Response.json(res);
    }

    // Root handoff
    if ((path === '/api/root/handoff' || path === '/api/jobs/root/handoff') && request.method === 'POST') {
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;

      if (!auth.scopes?.includes('lease:root') && !auth.scopes?.includes('admin') && !auth.scopes?.includes('host')) {
        return Response.json({ error: 'forbidden', reason: 'lease_root_host_or_admin_scope_required' }, { status: 403 });
      }

      const body = await safeJson(request);
      if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

      const jobId = body.job_id;
      if (!jobId || typeof jobId !== 'string') {
        return Response.json({ error: 'missing_job_id' }, { status: 400 });
      }

      const epoch = parseEpoch(body.epoch ?? body.lease_epoch);
      if (epoch === null) {
        return Response.json({ error: 'invalid_epoch' }, { status: 400 });
      }

      const lease = this.sql.exec("SELECT * FROM leases WHERE name = 'root'").toArray()[0] as any;
      if (!lease || lease.epoch !== epoch) {
        return Response.json({ error: 'fenced', reason: 'stale_epoch', current_epoch: lease?.epoch ?? 0 }, { status: 409 });
      }
      if (!lease.holder_job_id || lease.holder_job_id !== jobId) {
        return Response.json({ error: 'forbidden', reason: 'job_not_current_holder' }, { status: 403 });
      }
      if (lease.expires_at && new Date(lease.expires_at).getTime() <= Date.now()) {
        return Response.json({ error: 'fenced', reason: 'lease_expired', current_epoch: lease.epoch }, { status: 409 });
      }

      if (!auth.caller?.isAdmin) {
        if (auth.scopes?.includes('host')) {
          if (!auth.caller?.machineId || auth.caller.machineId !== (lease.holder_host_id || lease.holder_machine_id)) {
            return Response.json({ error: 'forbidden', reason: 'host_mismatch' }, { status: 403 });
          }
        }
        if (auth.scopes?.includes('lease:root') || auth.caller?.kind === 'job') {
          if (auth.caller?.jobId && auth.caller.jobId !== jobId) {
            return Response.json({ error: 'forbidden', reason: 'job_token_mismatch' }, { status: 403 });
          }
        }
      }

      let hostId = auth.caller?.machineId;
      if (auth.caller?.isAdmin && body.host_id) {
        hostId = body.host_id;
      }
      if (!hostId) {
        hostId = lease.holder_host_id || lease.holder_machine_id || '';
      }

      const summary = typeof body.summary === 'string' ? body.summary : '';
      const evidenceRefs = Array.isArray(body.evidence_refs) ? body.evidence_refs : [];
      const sessionId = typeof body.session_id === 'string' ? body.session_id : undefined;
      const authHeader = request.headers.get('Authorization');
      const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : null;
      const callerToken = (typeof body.job_token === 'string' && body.job_token) ? body.job_token : bearerToken;

      const res = await this.jobsService.postHandoffNotes(jobId, hostId || '', epoch, summary, evidenceRefs, sessionId, callerToken);
      if (!res.success) {
        const statusCode = res.error === 'epoch_mismatch' ? 409 : 403;
        return Response.json(res, { status: statusCode });
      }
      return Response.json(res);
    }

    if ((path === '/api/root/handoff' || path === '/api/jobs/root/handoff') && request.method === 'GET') {
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('admin') && !auth.scopes?.includes('host') && !auth.scopes?.includes('lease:root') && !auth.scopes?.includes('jobs:create')) {
        return Response.json({ error: 'forbidden', reason: 'insufficient_scope' }, { status: 403 });
      }
      const limitRaw = url.searchParams.get('limit');
      let limit = 10;
      if (limitRaw !== null) {
        const parsed = parseInt(limitRaw, 10);
        if (!Number.isFinite(parsed) || isNaN(parsed) || parsed.toString() !== limitRaw.trim()) {
          return Response.json({ error: 'invalid_limit' }, { status: 400 });
        }
        limit = Math.max(1, Math.min(100, parsed));
      }
      return Response.json({ notes: this.jobsService.getHandoffNotes(limit) });
    }

    // 3. Jobs action endpoints: /api/jobs/:id/:action
    const jobActionMatch = path.match(/^\/api\/jobs\/([^\/]+)\/([^\/]+)$/);
    if (jobActionMatch) {
      const [, jobId, action] = jobActionMatch;

      // GET /api/jobs/:id/status
      if (action === 'status') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;

        const row = this.sql.exec(
          `SELECT id, kind, idempotency_key, payload, status, created_at, deadline_at, actor,
                  offer_host_id, offer_machine_id, offer_expires_at, offer_attempts,
                  holder_host_id, holder_machine_id, started_at, first_action_at, evidence_digest,
                  native_session, launcher_task_id, lease_epoch, finished_at, outcome_summary, outcome_evidence, role_effect
           FROM jobs WHERE id = ?`,
          jobId
        ).toArray()[0] as any;

        if (!row) return Response.json({ error: 'not_found' }, { status: 404 });

        const isSupervisor = auth.caller?.isAdmin || auth.scopes?.includes('admin') || auth.scopes?.includes('lease:root') || auth.scopes?.includes('jobs:create');
        const isOfferedHost = auth.caller?.kind === 'host' && auth.scopes?.includes('host') && auth.caller.machineId && (row.offer_host_id === auth.caller.machineId || row.offer_machine_id === auth.caller.machineId || row.holder_host_id === auth.caller.machineId || row.holder_machine_id === auth.caller.machineId);
        const isJobCaller = auth.caller?.kind === 'job' && (auth.caller.jobId === jobId || (auth.caller as any).subject === jobId);
        if (!isSupervisor && !isOfferedHost && !isJobCaller) {
          return Response.json({ error: 'forbidden', reason: 'scope_or_binding_mismatch' }, { status: 403 });
        }

        let payload = row.payload;
        try { if (typeof payload === 'string') payload = JSON.parse(payload); } catch {}
        let offerAttempts = row.offer_attempts;
        try { if (typeof offerAttempts === 'string') offerAttempts = JSON.parse(offerAttempts); } catch {}
        let outcomeEvidence = row.outcome_evidence;
        try { if (typeof outcomeEvidence === 'string') outcomeEvidence = JSON.parse(outcomeEvidence); } catch {}

        return Response.json({
          job: {
            ...row,
            payload,
            offer_attempts: offerAttempts,
            outcome_evidence: outcomeEvidence
          }
        });
      }

      // POST /api/jobs/:id/claim
      if (action === 'claim') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (!auth.scopes?.includes('host') && !auth.scopes?.includes('admin')) {
          return Response.json({ error: 'forbidden', reason: 'host_or_admin_scope_required' }, { status: 403 });
        }

        const body = await safeJson(request) || {};
        let hostId = auth.caller?.machineId;
        if (auth.caller?.isAdmin && body.host_id) {
          hostId = body.host_id;
        }
        if (!hostId) {
          return Response.json({ error: 'forbidden', reason: 'machine_binding_required' }, { status: 403 });
        }

        const jobRow = this.sql.exec("SELECT kind, status, offer_host_id, offer_machine_id FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!jobRow) {
          return Response.json({ error: 'not_found' }, { status: 404 });
        }

        const targetHost = jobRow.offer_host_id || jobRow.offer_machine_id;
        if (targetHost && targetHost !== hostId) {
          return Response.json({ error: 'conflict', reason: 'offered_to_another_host' }, { status: 409 });
        }
        if (jobRow.status !== 'offered') {
          return Response.json({ error: 'conflict', reason: 'not_offered' }, { status: 409 });
        }

        if (body.gate === undefined || body.gate === null) {
          return Response.json({ error: 'gate_failed', reason: 'missing_gate' }, { status: 403 });
        }

        if (jobRow.kind === 'root-run') {
          if (typeof body.gate === 'boolean') {
            return Response.json({ error: 'unsupported_native_profile', reason: 'denyunsupportednative: structured gate required for root-run' }, { status: 400 });
          }
          const profileSupported = body.profile_supported ?? body.gate?.profile_supported;
          const nativeRootSupported = body.native_root_supported ?? body.gate?.native_root_supported;
          if (profileSupported === false || nativeRootSupported === false || (profileSupported !== true && nativeRootSupported !== true)) {
            return Response.json({ error: 'unsupported_native_profile', reason: 'denyunsupportednative' }, { status: 400 });
          }
        }

        const gate = typeof body.gate === 'boolean' ? body.gate : Boolean(body.gate?.launch_allowed);
        const res = await this.jobsService.claimJob(jobId, hostId, gate, undefined, auth.caller?.kind === 'host' ? auth.caller.tokenId : undefined);
        if (res.error === 'conflict') {
          return Response.json({ error: 'conflict' }, { status: 409 });
        }
        if (res.error === 'gate_failed') {
          return Response.json({ error: 'gate_failed', reason: res.reason }, { status: 403 });
        }
        if (res.error) {
          return Response.json({ error: res.error, reason: res.reason }, { status: 400 });
        }
        return Response.json(res);
      }

      // POST /api/jobs/:id/start
      if (action === 'start') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;

        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

        const jobRow = this.sql.exec("SELECT kind, holder_host_id, holder_machine_id, claim_token_hash FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!jobRow) {
          return Response.json({ error: 'not_found' }, { status: 404 });
        }

        if (jobRow.kind === 'root-run') {
          if (body.profile_supported === false || body.native_root_supported === false) {
            return Response.json({ error: 'unsupported_native_profile', reason: 'denyunsupportednative: native root profile unsupported' }, { status: 400 });
          }
        }

        const authHeader = request.headers.get('Authorization');
        const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : '';
        const jobToken = body.job_token || bearerToken;

        if (!auth.caller?.isAdmin) {
          if (auth.scopes?.includes('host')) {
            if (!auth.caller?.machineId || auth.caller.machineId !== (jobRow.holder_host_id || jobRow.holder_machine_id)) {
              return Response.json({ error: 'forbidden', reason: 'host_mismatch' }, { status: 403 });
            }
          }
          if (auth.caller?.kind === 'job' && auth.caller.jobId && auth.caller.jobId !== jobId) {
            return Response.json({ error: 'forbidden', reason: 'job_token_subject_mismatch' }, { status: 403 });
          }
          if (body.job_token && bearerToken && body.job_token !== bearerToken) {
            return Response.json({ error: 'forbidden', reason: 'bearer_job_token_mismatch' }, { status: 403 });
          }
        }

        const epoch = parseEpoch(body.epoch ?? body.lease_epoch);
        if (epoch === null) {
          return Response.json({ error: 'invalid_epoch' }, { status: 400 });
        }

        const evidenceDigest = body.evidence_digest || body.evidence || '';
        const nativeSession = body.native_session || body.session_id || '';
        const launcherTaskId = body.launcher_task_id || body.task_id || '';

        if (!evidenceDigest || !nativeSession || !launcherTaskId) {
          return Response.json({ error: 'unqualified_first_action', reason: 'evidence_digest, native_session, and launcher_task_id required' }, { status: 400 });
        }
        if (jobRow.kind === 'root-run') {
          if (evidenceDigest === 'UNKNOWN' || nativeSession === 'UNKNOWN' || launcherTaskId === 'UNKNOWN') {
            return Response.json({ error: 'unqualified_first_action', reason: 'denyunsupportednative: qualification fields cannot be UNKNOWN' }, { status: 400 });
          }
        }

        const res = await this.jobsService.startJob(jobId, jobToken, epoch, evidenceDigest, nativeSession, launcherTaskId);
        if (res.error === 'unauthorized_or_stale') {
          return Response.json({ error: 'unauthorized_or_stale' }, { status: 409 });
        }
        if (res.error === 'root_scheduling_disabled') {
          return Response.json({ error: 'root_scheduling_disabled', reason: 'root scheduling is disabled' }, { status: 409 });
        }
        if (res.error === 'first_action_window_expired') {
          return Response.json({ error: 'first_action_window_expired' }, { status: 409 });
        }
        if (res.error) {
          return Response.json({ error: res.error }, { status: 400 });
        }
        return Response.json({ status: 'running', job_id: jobId, epoch });
      }

      // POST /api/jobs/:id/renew
      if (action === 'renew') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;

        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

        const jobRow = this.sql.exec("SELECT holder_host_id, holder_machine_id FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!jobRow) {
          return Response.json({ error: 'not_found' }, { status: 404 });
        }

        const authHeader = request.headers.get('Authorization');
        const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : '';
        const jobToken = body.job_token || bearerToken;

        if (!auth.caller?.isAdmin) {
          if (auth.scopes?.includes('host')) {
            if (!auth.caller?.machineId || auth.caller.machineId !== (jobRow.holder_host_id || jobRow.holder_machine_id)) {
              return Response.json({ error: 'forbidden', reason: 'host_mismatch' }, { status: 403 });
            }
          }
          if (auth.caller?.kind === 'job' && auth.caller.jobId && auth.caller.jobId !== jobId) {
            return Response.json({ error: 'forbidden', reason: 'job_token_subject_mismatch' }, { status: 403 });
          }
          if (body.job_token && bearerToken && body.job_token !== bearerToken) {
            return Response.json({ error: 'forbidden', reason: 'bearer_job_token_mismatch' }, { status: 403 });
          }
        }

        const epoch = parseEpoch(body.epoch ?? body.lease_epoch);
        if (epoch === null) {
          return Response.json({ error: 'invalid_epoch' }, { status: 400 });
        }

        const res = await this.jobsService.renewJob(jobId, jobToken, epoch);
        if (res.error) {
          return Response.json({ error: res.error }, { status: 409 });
        }
        return Response.json({ status: 'renewed', job_id: jobId, epoch });
      }

      // POST /api/jobs/:id/finish
      if (action === 'finish') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;

        const body = await safeJson(request);
        if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

        const jobRow = this.sql.exec("SELECT holder_host_id, holder_machine_id FROM jobs WHERE id = ?", jobId).toArray()[0] as any;
        if (!jobRow) {
          return Response.json({ error: 'not_found' }, { status: 404 });
        }

        const authHeader = request.headers.get('Authorization');
        const bearerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : '';
        const jobToken = body.job_token || bearerToken;

        if (!auth.caller?.isAdmin) {
          if (auth.scopes?.includes('host')) {
            if (!auth.caller?.machineId || auth.caller.machineId !== (jobRow.holder_host_id || jobRow.holder_machine_id)) {
              return Response.json({ error: 'forbidden', reason: 'host_mismatch' }, { status: 403 });
            }
          }
          if (auth.caller?.kind === 'job' && auth.caller.jobId && auth.caller.jobId !== jobId) {
            return Response.json({ error: 'forbidden', reason: 'job_token_subject_mismatch' }, { status: 403 });
          }
          if (body.job_token && bearerToken && body.job_token !== bearerToken) {
            return Response.json({ error: 'forbidden', reason: 'bearer_job_token_mismatch' }, { status: 403 });
          }
        }

        const epoch = parseEpoch(body.epoch ?? body.lease_epoch);
        if (epoch === null) {
          return Response.json({ error: 'invalid_epoch' }, { status: 400 });
        }

        const status = body.status;
        const summary = typeof body.summary === 'string' ? body.summary : '';
        const evidenceRefs = Array.isArray(body.evidence_refs) ? body.evidence_refs : [];

        const res = await this.jobsService.finishJob(jobId, jobToken, epoch, status, summary, evidenceRefs);
        if (res.error === 'invalid_status') {
          return Response.json({ error: 'invalid_status' }, { status: 400 });
        }
        if (res.error) {
          return Response.json({ error: res.error }, { status: 409 });
        }
        return Response.json({ status: 'finished', job_id: jobId });
      }

      // POST /api/jobs/:id/reconcile
      if (action === 'reconcile') {
        if (request.headers.has('Authorization') && request.headers.has('Cookie')) return Response.json({ error: 'mixed_authority' }, { status: 403 });
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;

        // Fresh verified approved host-family authority for owning machine/job is mandatory;
        // expired job credentials, agents, caller booleans and actor-prefix claims never authorize.
        if (auth.caller?.kind !== 'host' || !auth.scopes?.includes('host') || !auth.caller.machineId || !auth.caller.tokenId) {
          return Response.json({ error: 'forbidden', reason: 'enrolled_host_required' }, { status: 403 });
        }

        const body = await safeJson(request);
        if (!body || typeof body !== 'object' || Array.isArray(body)) {
          return Response.json({ error: 'invalid_reconciliation' }, { status: 400 });
        }

        const res = await this.jobsService.reconcileTerminalJob(jobId, auth.caller.machineId, auth.caller.tokenId, body,
          () => this.sessionControlService.hostObservationAllowed(auth.caller!.machineId, { kind: 'host', machineId: auth.caller!.machineId, tokenId: auth.caller!.tokenId }));
        if (res.error) {
          const code = res.status_code || (res.error === 'not_found' ? 404 : (res.error === 'unauthorized_or_stale' || res.error === 'role_unauthorized' ? 403 : 409));
          return Response.json(res, { status: code });
        }
        if (res.status === 'HOLD' || res.reconciled === false) {
          return Response.json(res, { status: res.status_code || 409 });
        }
        return Response.json(res, { status: 200 });
      }

      // POST /api/jobs/:id/decline
      if (action === 'decline') {
        if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (!auth.scopes?.includes('host') && !auth.scopes?.includes('admin')) {
          return Response.json({ error: 'forbidden', reason: 'host_or_admin_scope_required' }, { status: 403 });
        }

        const body = await safeJson(request) || {};
        let hostId = auth.caller?.machineId;
        if (auth.caller?.isAdmin && body.host_id) {
          hostId = body.host_id;
        }
        if (!hostId) {
          return Response.json({ error: 'forbidden', reason: 'machine_binding_required' }, { status: 403 });
        }

        const reason = typeof body.reason === 'string' ? body.reason : 'declined_by_host';
        const res = await this.jobsService.declineJob(jobId, hostId, reason, auth.caller?.kind === 'host' ? auth.caller.tokenId : undefined);
        if (res.error) {
          return Response.json({ error: res.error }, { status: 409 });
        }
        return Response.json({ status: 'declined', job_id: jobId });
      }
    }

    // Match /api/jobs/:id (GET single job)
    const singleJobMatch = path.match(/^\/api\/jobs\/([^\/]+)$/);
    if (singleJobMatch && request.method === 'GET') {
      const jobId = singleJobMatch[1];
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;

      const row = this.sql.exec(
        `SELECT id, kind, idempotency_key, payload, status, created_at, deadline_at, actor,
                offer_host_id, offer_machine_id, offer_expires_at, offer_attempts,
                holder_host_id, holder_machine_id, started_at, first_action_at, evidence_digest,
                native_session, launcher_task_id, lease_epoch, finished_at, outcome_summary, outcome_evidence
         FROM jobs WHERE id = ?`,
        jobId
      ).toArray()[0] as any;

      if (!row) return Response.json({ error: 'not_found' }, { status: 404 });

      const isSupervisor = auth.caller?.isAdmin || auth.scopes?.includes('admin') || auth.scopes?.includes('lease:root') || auth.scopes?.includes('jobs:create');
      const isOfferedHost = auth.caller?.kind === 'host' && auth.scopes?.includes('host') && auth.caller.machineId && (row.offer_host_id === auth.caller.machineId || row.offer_machine_id === auth.caller.machineId || row.holder_host_id === auth.caller.machineId || row.holder_machine_id === auth.caller.machineId);
      const isJobCaller = auth.caller?.kind === 'job' && (auth.caller.jobId === jobId || (auth.caller as any).subject === jobId);
      if (!isSupervisor && !isOfferedHost && !isJobCaller) {
        return Response.json({ error: 'forbidden', reason: 'scope_or_binding_mismatch' }, { status: 403 });
      }

      let payload = row.payload;
      try { if (typeof payload === 'string') payload = JSON.parse(payload); } catch {}
      let offerAttempts = row.offer_attempts;
      try { if (typeof offerAttempts === 'string') offerAttempts = JSON.parse(offerAttempts); } catch {}
      let outcomeEvidence = row.outcome_evidence;
      try { if (typeof outcomeEvidence === 'string') outcomeEvidence = JSON.parse(outcomeEvidence); } catch {}

      return Response.json({
        job: {
          ...row,
          payload,
          offer_attempts: offerAttempts,
          outcome_evidence: outcomeEvidence
        }
      });
    }

    // GET /api/jobs (list jobs)
    if (path === '/api/jobs' && request.method === 'GET') {
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('admin') && !auth.scopes?.includes('host') && !auth.scopes?.includes('lease:root') && !auth.scopes?.includes('jobs:create')) {
        return Response.json({ error: 'forbidden', reason: 'insufficient_scope' }, { status: 403 });
      }

      const statusFilter = url.searchParams.get('status');
      const kindFilter = url.searchParams.get('kind');
      const limitRaw = url.searchParams.get('limit');
      let limit = 50;
      if (limitRaw !== null) {
        const parsed = parseInt(limitRaw, 10);
        if (!Number.isFinite(parsed) || isNaN(parsed) || parsed.toString() !== limitRaw.trim()) {
          return Response.json({ error: 'invalid_limit' }, { status: 400 });
        }
        limit = Math.max(1, Math.min(100, parsed));
      }

      let query = `SELECT id, kind, idempotency_key, payload, status, created_at, deadline_at, actor,
                          offer_host_id, offer_machine_id, offer_expires_at,
                          holder_host_id, holder_machine_id, started_at, first_action_at, evidence_digest,
                          native_session, launcher_task_id, lease_epoch, finished_at, outcome_summary
                   FROM jobs WHERE 1=1`;
      const params: any[] = [];

      // Host isolation: non-admin host caller only sees jobs offered to or held by this host
      if (!auth.caller?.isAdmin && auth.scopes?.includes('host')) {
        const hostId = auth.caller?.machineId;
        if (hostId) {
          query += ' AND (offer_host_id = ? OR offer_machine_id = ? OR holder_host_id = ? OR holder_machine_id = ?)';
          params.push(hostId, hostId, hostId, hostId);
        } else {
          return Response.json({ jobs: [] });
        }
      } else if (!auth.caller?.isAdmin && auth.scopes?.includes('lease:root')) {
        if (auth.caller?.jobId) {
          query += ' AND id = ?';
          params.push(auth.caller.jobId);
        } else {
          return Response.json({ jobs: [] });
        }
      }

      if (statusFilter) {
        query += ' AND status = ?';
        params.push(statusFilter);
      }
      if (kindFilter) {
        query += ' AND kind = ?';
        params.push(kindFilter);
      }
      query += ' ORDER BY created_at DESC LIMIT ?';
      params.push(limit);

      const rows = this.sql.exec(query, ...params).toArray() as any[];
      const jobs = rows.map(r => {
        let payload = r.payload;
        try { if (typeof payload === 'string') payload = JSON.parse(payload); } catch {}
        return { ...r, payload };
      });

      return Response.json({ jobs });
    }

    // POST /api/jobs (create job)
    if (path === '/api/jobs' && request.method === 'POST') {
      const auth = await this.authenticateBusRequest(request);
      if (auth.error) return auth.error;
      if (!auth.scopes?.includes('jobs:create') && !auth.scopes?.includes('admin')) {
        return Response.json({ error: 'forbidden', reason: 'jobs:create_scope_required' }, { status: 403 });
      }

      const body = await safeJson(request);
      if (!body) return Response.json({ error: 'malformed_json' }, { status: 400 });

      const kind = body.kind;
      if (!kind || typeof kind !== 'string') {
        return Response.json({ error: 'missing_kind' }, { status: 400 });
      }

      // Check idempotency_key
      if (body.idempotency_key && typeof body.idempotency_key === 'string') {
        const existing = this.sql.exec("SELECT id FROM jobs WHERE idempotency_key = ?", body.idempotency_key).toArray()[0] as any;
        if (existing) {
          return Response.json({ jobId: existing.id, job_id: existing.id }, { status: 201 });
        }
      }

      if (body.deadline_mins !== undefined) {
        if (!Number.isFinite(body.deadline_mins) || body.deadline_mins <= 0) {
          return Response.json({ error: 'invalid_deadline_mins' }, { status: 400 });
        }
      }
      const deadlineMins = typeof body.deadline_mins === 'number' ? body.deadline_mins : 25;

      const hasEpochField = ('epoch' in body) || ('lease_epoch' in body) || ('root_epoch' in body);
      let rootEpoch: number | undefined;
      if (hasEpochField) {
        const epochRaw = body.epoch !== undefined ? body.epoch : (body.lease_epoch !== undefined ? body.lease_epoch : body.root_epoch);
        const parsed = parseEpoch(epochRaw);
        if (parsed === null) {
          return Response.json({ error: 'invalid_epoch' }, { status: 400 });
        }
        rootEpoch = parsed;
      }

      if (rootEpoch !== undefined) {
        const lease = this.sql.exec("SELECT epoch, holder_job_id FROM leases WHERE name = 'root'").toArray()[0] as any;
        const currentEpoch = lease ? Number(lease.epoch) : 0;
        if (rootEpoch !== currentEpoch) {
          return Response.json({ error: 'fenced', status: 'fenced', reason: 'stale_root_epoch', current_epoch: currentEpoch }, { status: 409 });
        }
        if (lease?.holder_job_id) {
          const rootJob = this.sql.exec("SELECT status FROM jobs WHERE id = ?", lease.holder_job_id).toArray()[0] as any;
          if (rootJob && rootJob.status !== 'running') {
            return Response.json({ error: 'unauthorized', reason: `root_job_not_running (status: ${rootJob.status})` }, { status: 403 });
          }
        }
      }

      // Check active root incumbent if attempting to create competing root-run job
      if (kind === 'root-run') {
        const incumbent = this.jobsService.evaluateRootIncumbent();
        if (incumbent && (incumbent.is_healthy_alive || incumbent.transport_unknown)) {
          return Response.json({ error: 'conflict', reason: 'active_root_incumbent', incumbent }, { status: 409 });
        }
      }

      const payload = body.payload ?? {};
      const actor = auth.caller?.actor || (auth.caller?.isAdmin ? 'role:principal' : 'system');
      
      const authHeader = request.headers.get('Authorization');
      const cookieHeader = request.headers.get('Cookie') || undefined;
      const rootTokenPlain = body.root_token || body.job_token || (authHeader && authHeader.startsWith('Bearer ') ? authHeader.substring(7).trim() : undefined) || cookieHeader;

      const res = await this.jobsService.createJob(kind, payload, actor, deadlineMins, rootTokenPlain, rootEpoch);
      if (res.error) {
        const isStale = res.error === 'stale_root_epoch' || res.error === 'fenced' || res.error === 'conflict' || res.error.includes('epoch');
        return Response.json({ error: res.error }, { status: isStale ? 409 : 403 });
      }

      if (body.idempotency_key && typeof body.idempotency_key === 'string' && res.jobId) {
        this.sql.exec("UPDATE jobs SET idempotency_key = ? WHERE id = ?", body.idempotency_key, res.jobId);
      }

      return Response.json({ jobId: res.jobId, job_id: res.jobId }, { status: 201 });
    }

    return null;
  }

  webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    handleWebSocketMessageRegistry(ws, message, this.sql);
  }

  webSocketClose(ws: WebSocket, code: number, reason: string, wasClean: boolean) {
    handleWebSocketCloseRegistry(ws, this.sql);
  }

  webSocketError(ws: WebSocket, error: any) {
    handleWebSocketCloseRegistry(ws, this.sql);
  }
}
