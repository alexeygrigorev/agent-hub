import { DurableObject } from "cloudflare:workers";
import { SqlStorage } from "@cloudflare/workers-types";
import { Env } from './env';
import { SCHEMA_SQL } from './schema';
import { JwksCache, validateIdToken, exchangeCode } from './auth/dtc';
import { createSessionCookie, getSession, clearSessionCookie, createLoginCookie, getLoginCookie, clearLoginCookie, verifyCsrf, configuredAdmins, validSessionSecret, adminTokenHash, tokenAdminSubject, TOKEN_ADMIN_EMAIL } from './auth/session';
import { randomString, sha256, generateUserCode } from './auth/crypto';
import { generateTokenPair, verifyBearerScope } from './auth/token';
import { handleRegistryRoutes, handleWebSocketMessageRegistry, handleWebSocketCloseRegistry, updateHeartbeatMemoryOnly, updateHeartbeat } from './registry';
import { BusService, BUS_SCHEMA_SQL, CallerContext, generateUlid } from './bus';
import { JobsService, JOBS_SCHEMA_SQL } from './jobs';
import { FleetService } from './fleet';
import { LivenessService, thresholdsFrom, alertText } from './liveness';
import { ChecksService, validateCheck } from './checks';
import { ensureRootScheduling, getRootScheduling, isRootSchedulingEnabled, setRootScheduling } from './root_scheduling';
import { QuotaStore, handleQuotaRoutes } from './quota';

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

export class Hub extends DurableObject {
  sql: SqlStorage;
  env: Env;
  jwksCache: JwksCache = new JwksCache();
  public busService: BusService;
  public jobsService: JobsService;
  public quotaStore: QuotaStore;
  public fleetService: FleetService;
  public livenessService: LivenessService;
  public checksService: ChecksService;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.env = env;
    this.sql = ctx.storage.sql;
    this.initDb();

    const txSync = (this.ctx.storage as any).transactionSync ? (fn: any) => (this.ctx.storage as any).transactionSync(fn) : undefined;
    this.busService = new BusService(this.sql, txSync);
    this.fleetService = new FleetService(this.sql, fn => this.ctx.storage.transactionSync(fn));
    this.fleetService.init();
    this.livenessService = new LivenessService(this.sql, thresholdsFrom(env));
    this.livenessService.init();
    this.checksService = new ChecksService(this.sql);
    this.checksService.init();

    const busForJobs = Object.create(this.busService);
    const runInTxn = (this.ctx.storage as any).transactionSync ? (fn: any) => (this.ctx.storage as any).transactionSync(fn) : (fn: any) => fn();
    const sendRoot = (caller: any, opts: any) => {
      return runInTxn(() => {
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

    this.busService.getBusMessages = (caller: CallerContext, actor?: string | null, now: number = Date.now()): any[] => {
      if (caller.isRevoked) {
        throw new Error("unauthorized");
      }

      if (caller.actor === 'role:root' || actor === 'role:root') {
        throw new Error("missing_m4_root_authority: root role routing has no accepted root authority in M3");
      }

      if (caller.isAdmin) {
        if (actor) {
          return this.sql.exec(
            'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
            actor, actor
          ).toArray();
        }
        return this.sql.exec('SELECT * FROM messages WHERE to_actor != "role:root" ORDER BY created_at DESC LIMIT 100').toArray();
      }

      if (caller.actor) {
        const effectiveActor = actor || caller.actor;
        if (!this.busService.isAuthorizedForActor(caller, effectiveActor, now)) {
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
            'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
            aliases[0], aliases[0]
          ).toArray();
        } else {
          return this.sql.exec(
            'SELECT * FROM messages WHERE (to_actor IN (?, ?) OR from_actor IN (?, ?)) AND to_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
            aliases[0], aliases[1], aliases[0], aliases[1]
          ).toArray();
        }
      }

      if (caller.kind === 'host' || (!caller.actor && caller.machineId)) {
        if (!caller.machineId) throw new Error("unauthorized");

        if (actor) {
          if (!this.busService.isAuthorizedForActor(caller, actor, now)) {
            throw new Error("unauthorized");
          }
          return this.sql.exec(
            'SELECT * FROM messages WHERE (to_actor = ? OR from_actor = ?) AND to_actor != "role:root" ORDER BY created_at DESC LIMIT 100',
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
        const sqlQuery = `SELECT * FROM messages WHERE (to_actor IN (${placeholders}) OR from_actor IN (${placeholders})) AND to_actor != "role:root" ORDER BY created_at DESC LIMIT 100`;
        return this.sql.exec(sqlQuery, ...allowedList, ...allowedList).toArray();
      }

      throw new Error("unauthorized");
    };

    this.jobsService = new JobsService(this.sql, this.ctx, {
      busService: busForJobs,
      enableAlarms: true,
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
    // 1. If agents exists from M3 BUS_SCHEMA_SQL (tag has no PK or project is NOT NULL):
    // Recreate agents table to match M4 schema non-destructively
    const agentTableInfo = this.sql.exec("PRAGMA table_info(agents)").toArray() as any[];
    if (agentTableInfo.length > 0) {
      const tagCol = agentTableInfo.find(c => c.name.toLowerCase() === 'tag');
      const projCol = agentTableInfo.find(c => c.name.toLowerCase() === 'project');
      if (tagCol && (tagCol.pk === 0 || (projCol && projCol.notnull === 1))) {
        this.sql.exec(`
          CREATE TABLE agents_m4 (
            tag TEXT PRIMARY KEY,
            project TEXT,
            role TEXT,
            machine_id TEXT,
            registered_at TEXT,
            engine TEXT,
            session_id TEXT,
            workspace TEXT,
            team_id TEXT,
            imported_from TEXT,
            state TEXT,
            health_state TEXT,
            health_detail JSON,
            detail TEXT,
            lasthealth TEXT,
            health_at TEXT,
            last_seen_at TEXT,
            last_seen TEXT,
            updated_at TEXT,
            pid INTEGER,
            started_at TEXT
          )
        `);
        const colNames = agentTableInfo.map(c => c.name);
        const m4Cols = ['tag', 'project', 'role', 'machine_id', 'registered_at', 'engine', 'session_id', 'workspace', 'team_id', 'imported_from', 'state', 'health_state', 'health_detail', 'detail', 'lasthealth', 'health_at', 'last_seen_at', 'last_seen', 'updated_at', 'pid', 'started_at'];
        const commonCols = colNames.filter(c => m4Cols.includes(c));
        const selectCols = commonCols.join(', ');
        this.sql.exec(`INSERT OR REPLACE INTO agents_m4 (${selectCols}) SELECT ${selectCols} FROM agents`);
        this.sql.exec("DROP TABLE agents");
        this.sql.exec("ALTER TABLE agents_m4 RENAME TO agents");
        this.sql.exec("CREATE INDEX IF NOT EXISTS idx_agents_project_tag ON agents(project, tag)");
      }
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

    this.ensureColumns('machines', {
      name: 'TEXT',
      hostname: 'TEXT',
      network: "TEXT DEFAULT 'home'",
      os: "TEXT DEFAULT ''",
      roles: "JSON DEFAULT '[]'",
      capabilities: "JSON DEFAULT '{}'",
      credential_id: 'TEXT',
      registered_at: 'TEXT',
      last_checkin_at: 'TEXT',
      last_heartbeat: 'JSON',
      drained: 'INTEGER DEFAULT 0',
      notes: 'TEXT'
    });

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

    // If audit was upgraded from SPEC §6 where ts existed instead of at:
    const auditInfo = this.sql.exec("PRAGMA table_info(audit)").toArray() as any[];
    const hasAt = auditInfo.some(c => c.name.toLowerCase() === 'at');
    const hasTs = auditInfo.some(c => c.name.toLowerCase() === 'ts');
    if (hasAt && hasTs) {
      this.sql.exec("UPDATE audit SET at = ts WHERE at IS NULL AND ts IS NOT NULL");
    }

    this.sql.exec("INSERT OR IGNORE INTO leases (name, epoch) VALUES ('root', 0)");
    ensureRootScheduling(this.sql);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    
    try {
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
          `SELECT id, kind, payload, status, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, created_at, actor
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
          actor: r.actor
        }));

        return Response.json({ status: 'ok', server_time: nowIso, offers });
      }

      if (path === '/api/heartbeat' || path === '/api/liveness' || path === '/api/checks' || path.startsWith('/api/checks/')) {
        return this.handleWatchRoutes(request, path);
      }
      if (path === '/api/fleet' || path.startsWith('/api/fleet/')) {
        const auth = await this.authenticateBusRequest(request);
        if (auth.caller?.isAdmin) {
          const session = await getSession(this.env, request);
          if (session) auth.caller.actor = 'human:' + session.sub;
        }
        return this.fleetService.handle(request, auth);
      }
      const registryResponse = await handleRegistryRoutes(request, path, this.sql, this.ctx, this.env);
      if (registryResponse) return registryResponse;
      const busResponse = await this.handleBusRoutes(request, path);
      if (busResponse) return busResponse;
      const jobsResponse = await this.handleJobsRoutes(request, path, url);
      if (jobsResponse) return jobsResponse;
      const quotaResponse = await handleQuotaRoutes(request, path, this.quotaStore, url);
      if (quotaResponse) return quotaResponse;
      if (path === '/api/healthz') {
        const auth = await this.authenticateBusRequest(request);
        if (auth.error) return auth.error;
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        return Response.json({ status: 'ok', component: 'hub-do' });
      }

      if (path === '/auth/login') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        // OIDC only works on the origin registered as the callback; elsewhere use token sign-in.
        let oidcOrigin = ''; try { oidcOrigin = new URL(this.env.AUTH_CALLBACK_URL).origin; } catch {}
        if (validSessionSecret(this.env) && adminTokenHash(this.env) && (configuredAdmins(this.env).length === 0 || oidcOrigin !== url.origin)) {
          const next = url.searchParams.get('return_to');
          const location = next && /^\/device\?code=[A-Za-z0-9-]{1,32}$/.test(next) ? '/auth/token?return_to=' + encodeURIComponent(next) : '/auth/token';
          return new Response(null, { status: 303, headers: { Location: location, 'Cache-Control': 'private, no-store' } });
        }
        if (!validSessionSecret(this.env) || configuredAdmins(this.env).length === 0) return Response.json({ error: 'authentication_not_configured' }, { status: 503 });
        const state = randomString(32);
        const nonce = randomString(32);
        const verifier = randomString(43);
        const codeChallenge = await sha256(verifier);
        this.sql.exec('DELETE FROM login_states WHERE expires_at <= ?', Math.floor(Date.now() / 1000));
        this.sql.exec('INSERT INTO login_states (state_hash, expires_at) VALUES (?, ?)', await sha256(state), Math.floor(Date.now() / 1000) + 600);
        
        const returnTo = url.searchParams.get('return_to');
        const returnToPath = returnTo && returnTo.startsWith('/device?code=') ? returnTo : undefined;
        
        const loginCookie = await createLoginCookie(this.env, {
          state, nonce, verifier, exp: Math.floor(Date.now() / 1000) + 600, returnTo: returnToPath
        });

        const authUrl = new URL(`${this.env.AUTH_BASE_URL}/oauth2/authorize`);
        authUrl.search = new URLSearchParams({
          client_id: this.env.AUTH_CLIENT_ID,
          redirect_uri: this.env.AUTH_CALLBACK_URL,
          response_type: 'code',
          scope: 'openid email profile',
          state,
          nonce,
          code_challenge: codeChallenge,
          code_challenge_method: 'S256',
          identity_provider: 'Google'
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
        const code = query.get('code');
        const state = query.get('state');
        if (!code || !state) return Response.json({ error: 'Missing code or state' }, { status: 400 });

        const loginData = await getLoginCookie(this.env, request);
        if (!loginData || loginData.state !== state) {
          return Response.json({ error: 'Invalid state' }, { status: 400 });
        }

        const stateHash = await sha256(state);
        const consume = this.sql.exec('UPDATE login_states SET consumed = 1 WHERE state_hash = ? AND consumed = 0 AND expires_at > ?', stateHash, Math.floor(Date.now() / 1000));
        if (consume.rowsWritten !== 1) return Response.json({ error: 'invalid_state' }, { status: 400, headers: { 'Set-Cookie': clearLoginCookie() } });
        let claims: { sub: string; email: string; email_verified: boolean };
        try {
          const idToken = await exchangeCode(this.env, code, loginData.verifier);
          claims = await validateIdToken(this.env, idToken, loginData.nonce, this.jwksCache);
        } catch {
          return Response.json({ error: 'invalid_identity_token' }, { status: 400, headers: { 'Set-Cookie': clearLoginCookie() } });
        }
        if (!configuredAdmins(this.env).includes(claims.email.toLowerCase())) {
          return Response.json({ error: 'forbidden' }, { status: 403, headers: { 'Set-Cookie': clearLoginCookie() } });
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
        headers.append('Set-Cookie', clearLoginCookie());
        headers.append('Location', loginData.returnTo || '/');

        return new Response(null, { status: 302, headers });
      }

      if (path === '/auth/me') {
        if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ authenticated: false }, { status: 401 });
        return Response.json({ authenticated: true, email: session.email, csrf_token: session.csrfToken });
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
          if (typeof proposedActor !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(proposedActor)) {
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
        if (now >= expiresAt && pairing.status !== 'consumed' && pairing.status !== 'denied') {
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
            if (Date.parse(current.expires_at) <= Date.now()) {
              this.sql.exec("UPDATE device_pairings SET status = 'expired' WHERE device_code_hash = ?", deviceCodeHash);
              return null;
            }
            const expires = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
            this.sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, credId, current.client_kind, tokens.accessHash, tokens.refreshHash, familyId, current.name, current.machine_id, current.actor, current.granted_scopes, current.approved_by_sub, new Date().toISOString(), expires);
            this.sql.exec("UPDATE device_pairings SET status = 'consumed' WHERE device_code_hash = ?", deviceCodeHash);
            if (current.machine_id) this.sql.exec('UPDATE machines SET credential_id = ? WHERE id = ?', credId, current.machine_id);
            return { expires, scopes: JSON.parse(current.granted_scopes) as string[] };
          });
          if (!result) return Response.json({ error: 'invalid_grant' }, { status: 400 });
          return Response.json({ access_token: tokens.accessToken, refresh_token: tokens.refreshToken, token_type: 'Bearer', expires_in: Math.floor((Date.parse(result.expires) - Date.now()) / 1000), scope: result.scopes.join(' ') });
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

        if (!userCode) return new Response('<!doctype html><html><head><title>Pair a device</title></head><body><h1>Pair a device</h1><form method=GET action=/device><label>Device code <input name=code required maxlength=9 autocomplete=off></label><button>Continue</button></form></body></html>', { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
        const pairing = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode.toUpperCase()).toArray()[0] as any;
        if (!pairing) {
          return new Response('Not found or expired', { status: 404 });
        }

        const safeKind = htmlEscape(pairing.client_kind);
        const safeName = htmlEscape(pairing.name);
        const safeHostname = htmlEscape(pairing.hostname);
        const safeCountry = pairing.ip_country ? htmlEscape(pairing.ip_country) : 'Unknown';
        const safeActor = pairing.actor ? htmlEscape(pairing.actor) : '';
        const safeScopes = htmlEscape(pairing.requested_scopes);
        const safeUserCode = htmlEscape(userCode);
        const safeCsrf = htmlEscape(session.csrfToken);

        const html = `<!DOCTYPE html>
<html>
<head><title>Approve Device</title></head>
<body>
  <h1>Approve Device</h1>
  <p>Kind: ${safeKind}</p>
  <p>Name: ${safeName}</p>
  <p>Hostname: ${safeHostname}</p>
  <p>Country: ${safeCountry}</p>
  ${safeActor ? `<p>Actor: ${safeActor}</p>` : ''}
  <p>Requested Scopes: ${safeScopes}</p>
  <form method="POST" action="/api/device/approve/${safeUserCode}" id="approveForm">
    <input type="hidden" name="csrf_token" value="${safeCsrf}">
    ${safeKind === 'host' ? `<label>Machine ID (optional): <input type="text" name="machine_id"></label><br>` : ''}
    <label>Granted Scopes: <input type="text" name="granted_scopes" value="${htmlEscape(typeof pairing.requested_scopes === 'string' ? pairing.requested_scopes : JSON.stringify(pairing.requested_scopes))}"></label><br>
    <button type="button" onclick="submitApprove()">Approve</button>
  </form>
  <form method="POST" action="/api/device/deny/${safeUserCode}" id="denyForm">
    <input type="hidden" name="csrf_token" value="${safeCsrf}">
    <button type="button" onclick="submitDeny()">Deny</button>
  </form>
  <script>
    async function submitApprove() {
      const form = document.getElementById('approveForm');
      const data = { granted_scopes: JSON.parse(form.granted_scopes.value) };
      if (form.machine_id && form.machine_id.value) data.machine_id = form.machine_id.value;
      const res = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': form.csrf_token.value },
        body: JSON.stringify(data)
      });
      if (res.ok) alert('Approved'); else alert('Error');
    }
    async function submitDeny() {
      const form = document.getElementById('denyForm');
      const res = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': form.csrf_token.value },
      });
      if (res.ok) alert('Denied'); else alert('Error');
    }
  </script>
</body>
</html>`;
        return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
      }

      if (path.startsWith('/api/device/approve') || path.startsWith('/api/device/deny') || path.startsWith('/api/device/info')) {
        const session = await getSession(this.env, request);
        if (!session) return Response.json({ error: 'Unauthorized' }, { status: 401 });

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
        if (now >= expiresAt) {
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
          const granted_scopes = body.granted_scopes || JSON.parse(pairing.requested_scopes);
          const parsedGranted = granted_scopes;
          
          if (!Array.isArray(parsedGranted) || parsedGranted.length === 0) return Response.json({ error: 'invalid_scopes' }, { status: 400 });
          if (parsedGranted.includes('admin')) return Response.json({ error: 'Cannot grant admin scope through device flow' }, { status: 400 });
          
          const reqScopes = typeof pairing.requested_scopes === 'string' ? JSON.parse(pairing.requested_scopes) : pairing.requested_scopes;
          const isSubset = parsedGranted.every((s: string) => reqScopes.includes(s));
          if (!isSubset) return Response.json({ error: 'Cannot grant unrequested scopes' }, { status: 400 });

          if (body.machine_id !== undefined && (typeof body.machine_id !== 'string' || body.machine_id.length > 128 || pairing.client_kind !== 'host')) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
          return this.ctx.storage.transactionSync(() => {
            const current = this.sql.exec('SELECT * FROM device_pairings WHERE user_code = ?', userCode).toArray()[0] as any;
            if (!current || current.status !== 'pending' || Date.parse(current.expires_at) <= Date.now()) return Response.json({ error: 'invalid_pairing' }, { status: 409 });
            let machineId: string | null = null;
            if (current.client_kind === 'host') {
              if (body.machine_id) {
                if (!this.sql.exec('SELECT id FROM machines WHERE id = ?', body.machine_id).toArray()[0]) return Response.json({ error: 'invalid_machine_id' }, { status: 400 });
                machineId = body.machine_id;
              } else {
                machineId = randomString(16);
                this.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', machineId, current.name, current.hostname, new Date().toISOString());
              }
            }
            this.sql.exec("UPDATE device_pairings SET status = 'approved', approved_by_sub = ?, granted_scopes = ?, machine_id = ? WHERE user_code = ?", session.sub, JSON.stringify(parsedGranted), machineId, userCode);
            return Response.json({ status: 'approved', machine_id: machineId });
          });
        }
      }

      if (path === '/api/token/refresh') {
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
        
        this.ctx.storage.transactionSync(() => {
        const cred = this.sql.exec('SELECT * FROM credentials WHERE refresh_hash = ?', refreshHash).toArray()[0] as any;
        const nowMs = Date.parse(now);
        if (!cred) {
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else if (cred.revoked_at) {
          // Replay or explicitly revoked! Revoke family.
          this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else if (!Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= nowMs) {
          errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
        } else {
          const res = this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL', now, cred.id);
          if (res.rowsWritten !== 1) {
            this.sql.exec('UPDATE credentials SET revoked_at = ? WHERE family_id = ? AND revoked_at IS NULL', now, cred.family_id);
            errorResponse = Response.json({ error: 'invalid_grant' }, { status: 400 });
          } else {
            this.sql.exec(`
              INSERT INTO credentials
              (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes, created_by_sub, created_at, expires_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            `, newId, cred.kind, accessHash, newRefreshHash, cred.family_id, cred.subject, cred.machine_id, cred.actor, cred.scopes, cred.created_by_sub, now, cred.expires_at);

            scopesResponse = JSON.parse(cred.scopes).join(' ');
            expiresAtResponse = cred.expires_at;
          }
        }

        });
        if (errorResponse) return errorResponse;

        return Response.json({
          access_token: accessToken,
          refresh_token: refreshToken,
          token_type: 'Bearer',
          expires_in: Math.max(0, Math.floor((new Date(expiresAtResponse).getTime() - new Date(now).getTime()) / 1000)),
          scope: scopesResponse
        });
      }

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

        const res = await this.scheduledTick(slotOverride);
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

  private tokenFailures: number[] = [];

  /** Admin sign-in with a high-entropy token whose SHA-256 is a Worker secret. */
  private async tokenSignIn(request: Request): Promise<Response> {
    const headers = { 'Cache-Control': 'private, no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'", 'X-Content-Type-Options': 'nosniff' };
    const hash = adminTokenHash(this.env);
    if (!hash || !validSessionSecret(this.env)) return Response.json({ error: 'authentication_not_configured' }, { status: 503, headers });
    const safeNext = (value: string | null) => value && /^\/device\?code=[A-Za-z0-9-]{1,32}$/.test(value) ? value : null;
    let next = safeNext(new URL(request.url).searchParams.get('return_to'));
    const page = (message: string, status = 200) => new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Hub sign-in</title>
<style>body{font-family:system-ui,sans-serif;background:#f3f5f6;color:#172735;margin:0}main{max-width:420px;margin:12vh auto;padding:24px;background:#fff;border:1px solid #d5dde1;border-radius:6px}input,button{font:inherit;padding:10px;width:100%;box-sizing:border-box;margin-top:8px}button{background:#164c65;color:#fff;border:0;border-radius:3px}p{color:#425b69}</style></head>
<body><main><h1>Agent Hub</h1><p>${message}</p><form method="post" action="/auth/token"><input type="hidden" name="return_to" value="${next ? next.replace(/[^A-Za-z0-9\/?=-]/g, '') : ''}"><label>Admin token<input name="token" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form></main></body></html>`, { status, headers: { ...headers, 'Content-Type': 'text/html; charset=utf-8' } });
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
    const targets = typeof this.env.HUB_ALERT_TO === 'string' ? this.env.HUB_ALERT_TO.split(',').map(t => t.trim()).filter(Boolean) : [];
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
  }

  /** Cron and manual entry point for scheduled checks. */
  async runChecks(only?: string) {
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
      evaluateQuota: () => this.sql.exec('SELECT * FROM fleet_accounts ORDER BY label').toArray().map(a => this.fleetService.accountStatus(a)),
    }, only);
    // Retry alerts whose delivery failed or had no target yet, at most every 15 minutes each.
    for (const alert of this.livenessService.undelivered(900)) await this.notifyAlert(alert);
    return results;
  }

  async scheduledTick(slotOverride?: string) {
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
          await this.ctx.storage.setAlarm(Math.max(Date.now(), nextTime));
        } catch {}
      }
    }

    return { status: 'ok', retention: cleanup, tick: tickResult };
  }

  async alarm() {
    const now = (this.jobsService as any).now ? (this.jobsService as any).now() : new Date();
    const cleanup = this.busService.retentionCleanup(now.getTime(), 100);
    const nextAlarm = this.jobsService.processAlarms();

    if (cleanup.hasMore) {
      await this.ctx.storage.setAlarm(Date.now() + 1000);
    } else if (nextAlarm) {
      try {
        await this.ctx.storage.setAlarm(Math.max(Date.now(), nextAlarm));
      } catch {}
    }
  }

  private async authenticateBusRequest(request: Request): Promise<{ caller?: CallerContext; scopes?: string[]; error?: Response }> {
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
    } else if (cred.kind === 'agent' || cred.kind === 'job') {
      caller.actor = cred.actor || cred.subject;
      if (cred.machine_id) caller.machineId = cred.machine_id;
      if (cred.kind === 'job') {
        caller.jobId = cred.subject;
      }
    } else if (cred.kind === 'human') {
      if (scopes.includes('admin')) caller.isAdmin = true;
      caller.actor = cred.actor || cred.subject;
    }

    return { caller, scopes };
  }

  private async parseSafeBusBody(request: Request, allowedKeys: Set<string>, optional: boolean = false): Promise<{ data?: Record<string, any>; error?: Response }> {
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
        if (totalBytes > 16384) {
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
    const agent = this.sql.exec("SELECT state, health_state FROM agents WHERE tag = ?", tag).toArray()[0] as any;
    if (agent) {
      return { state: agent.state || agent.health_state || 'unknown' };
    }
    return null;
  }

  private async handleJobsRoutes(request: Request, path: string, url: URL): Promise<Response | null> {
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
        `SELECT id, kind, payload, status, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, created_at, actor
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
          actor: r.actor
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
                  native_session, launcher_task_id, lease_epoch, finished_at, outcome_summary, outcome_evidence
           FROM jobs WHERE id = ?`,
          jobId
        ).toArray()[0] as any;

        if (!row) return Response.json({ error: 'not_found' }, { status: 404 });

        const isSupervisor = auth.caller?.isAdmin || auth.scopes?.includes('admin') || auth.scopes?.includes('lease:root') || auth.scopes?.includes('jobs:create');
        const isOfferedHost = auth.caller?.machineId && (row.offer_host_id === auth.caller.machineId || row.offer_machine_id === auth.caller.machineId || row.holder_host_id === auth.caller.machineId || row.holder_machine_id === auth.caller.machineId);
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
        const res = await this.jobsService.claimJob(jobId, hostId, gate);
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
        const res = await this.jobsService.declineJob(jobId, hostId, reason);
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
      const isOfferedHost = auth.caller?.machineId && (row.offer_host_id === auth.caller.machineId || row.offer_machine_id === auth.caller.machineId || row.holder_host_id === auth.caller.machineId || row.holder_machine_id === auth.caller.machineId);
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
