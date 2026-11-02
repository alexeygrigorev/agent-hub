/** Singleton interactive driver. Native lifecycle and timers belong to existing services. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { DRIVER_PROMPT, DRIVER_PING, DRIVER_PROMPT_VERSION } from './driver-prompt';
import { boundedText } from './mcp';

type Auth = { caller?: { isAdmin?: boolean; actor?: string; kind?: string; tokenId?: string; machineId?: string }; scopes?: string[]; error?: Response };
type Handler = (request: Request, auth: any) => Promise<Response>;
type Row = { id: string; actor: string; request_key: string; config: string;
  session_id: string | null; schedule_id: string | null; phase: string; created_at: string };
const terminal = new Set(['stopped', 'exited', 'failed', 'finished', 'expired', 'cancelled', 'canceled', 'refused']);
const reply = (body: any, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'private, no-store' } });

export class DriverService {
  // Serialize effects across awaits. Persisted phase + stable downstream keys make
  // recovery after eviction safe; an uncertain start never releases the singleton.
  private tail: Promise<unknown> = Promise.resolve();
  constructor(private sql: SqlStorage, private sessions: Handler, private schedules: Handler,
              private now = Date.now) {}
  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS hub_drivers (
      id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL,
      config TEXT NOT NULL, session_id TEXT, schedule_id TEXT, phase TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(actor,request_key))`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS hub_driver_operations (
      actor TEXT NOT NULL, request_key TEXT NOT NULL, input TEXT NOT NULL,
      target_id TEXT, result_id TEXT, PRIMARY KEY(actor,request_key))`);
  }
  private latest(): Row | undefined {
    return this.sql.exec('SELECT * FROM hub_drivers ORDER BY rowid DESC LIMIT 1').toArray()[0] as Row | undefined;
  }
  private get(id: string): Row | undefined {
    return this.sql.exec('SELECT * FROM hub_drivers WHERE id=?', id).toArray()[0] as Row | undefined;
  }
  private async call(handler: Handler, path: string, auth: Auth, body?: any) {
    const res = await handler(new Request('https://hub.internal' + path, {
      method: body === undefined ? 'GET' : 'POST',
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }), auth);
    return { status: res.status, body: await res.json() as any };
  }
  /** Driver effects belong to the operator who created the generation. A
   * different admin may recover it, but must not rebind its session or schedule
   * to their own actor. This internal caller is only made after route admin auth. */
  private rowAuth(auth: Auth, row: Row): Auth {
    return { ...auth, caller: { ...(auth.caller ?? {}), actor: row.actor,
      isAdmin: true, kind: 'human', tokenId: undefined, machineId: undefined }, scopes: ['admin'] };
  }
  private async view(row: Row | undefined, auth: Auth) {
    if (!row) return { driver: null, session: null, schedule: null };
    const session = row.session_id ? await this.call(this.sessions, '/api/sessions/' + row.session_id, auth) : null;
    const schedule = row.schedule_id ? await this.call(this.schedules, '/api/session-schedules/' + row.schedule_id, auth) : null;
    const hasAccessTable = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='driver_access'").toArray().length;
    const access: any = hasAccessTable ? this.sql.exec('SELECT expires_at FROM driver_access WHERE driver_id=? LIMIT 1', row.id).toArray()[0] : null;
    return { driver: { id: row.id, phase: row.phase, config: JSON.parse(row.config),
      session_id: row.session_id, schedule_id: row.schedule_id, created_at: row.created_at,
      prompt_version: DRIVER_PROMPT_VERSION, retry_key: row.request_key },
      session: session?.body ?? null, schedule: schedule?.body ?? null,
      access: { issued: !!access, expires_at: access ? new Date(access.expires_at).toISOString() : null },
      startup_delivery: this.sql.exec("SELECT id,status,command_id,attempts,last_error,delivered_at FROM session_deliveries WHERE source_id=? AND delivery_key='startup'", row.id).toArray()[0] ?? null };
  }
  async handle(request: Request, auth: Auth): Promise<Response> {
    if (auth.error) return auth.error;
    if (!auth.caller?.isAdmin) return reply({ error: 'forbidden' }, 403);
    const path = new URL(request.url).pathname;
    if (request.method === 'GET' && path === '/api/driver') return reply(await this.view(this.latest(), auth));
    const action = /^\/api\/driver\/(start|stop|restart)$/.exec(path)?.[1];
    if (request.method !== 'POST' || !action) return reply({ error: 'not_found' }, 404);
    let input: any;
    try {
      const text = await boundedText(request, 4096);
      input = JSON.parse(text);
    } catch { return reply({ error: 'invalid_driver_request' }, 400); }
    if (!input || typeof input !== 'object' || Array.isArray(input) ||
        Object.keys(input).some(k => !['key', 'engine', 'host', 'workspace', 'force'].includes(k)) ||
        typeof input.key !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(input.key) ||
        (input.engine !== undefined && !['claude', 'codex', 'antigravity', 'zai', 'opencode'].includes(input.engine)) ||
        (input.host !== undefined && (typeof input.host !== 'string' || !input.host || input.host.length > 128)) ||
        (input.workspace !== undefined && (typeof input.workspace !== 'string' || !input.workspace || input.workspace.length > 1024)) ||
        (input.force !== undefined && (typeof input.force !== 'boolean' || action !== 'start')) ||
        (action === 'stop' && Object.keys(input).some(k => k !== 'key'))) return reply({ error: 'invalid_driver_request' }, 400);
    const work = this.tail.then(() => this.mutate(action, input, auth));
    this.tail = work.catch(() => undefined);
    try { return await work; }
    catch { return reply({ error: 'driver_operation_uncertain', retry_key: input.key }, 503); }
  }
  private async stop(row: Row, auth: Auth) {
    auth = this.rowAuth(auth, row);
    if (['rejected', 'stopped'].includes(row.phase) && !row.session_id) {
      this.sql.exec("UPDATE hub_drivers SET phase='stopped' WHERE id=?", row.id);
      return reply({ ...(await this.view(this.get(row.id), auth)), stopped: true });
    }
    // Recover an interrupted start first so neither its session nor its schedule
    // can become orphaned. Stable keys recover downstream effects after a crash.
    if (row.phase === 'starting') {
      const started = await this.start(row, auth);
      if (started.status >= 400) return started;
      row = this.get(row.id)!;
    }
    if (row.schedule_id) {
      const paused = await this.call(this.schedules, '/api/session-schedules/' + row.schedule_id + '/pause', auth, {});
      if (paused.status >= 300) return reply({ error: 'driver_schedule_pause_failed', detail: paused.body }, paused.status);
    }
    this.sql.exec("UPDATE session_deliveries SET status='failed',last_error='driver_stopped',next_attempt_at=NULL WHERE source_id=? AND status='pending'", row.id);
    this.sql.exec("UPDATE hub_drivers SET phase='stopping' WHERE id=?", row.id);
    if (!row.session_id) return reply({ error: 'driver_session_unknown' }, 409);
    const observed = await this.call(this.sessions, '/api/sessions/' + row.session_id, auth);
    if (observed.status !== 200) return reply({ error: 'driver_session_unknown', detail: observed.body }, 409);
    if (terminal.has(observed.body.state)) {
      this.sql.exec("UPDATE hub_drivers SET phase='stopped' WHERE id=?", row.id);
      return reply({ ...(await this.view(this.get(row.id), auth)), stopped: true });
    }
    // The canonical operator stop atomically cancels provably unstarted intents
    // or enqueues a native stop after start authorization. Do not choose that
    // fence from a potentially stale state read here.
    const stopped = await this.call(this.sessions, `/api/sessions/${row.session_id}/stop`, auth, { key: 'driver-stop-' + row.id });
    if (stopped.status >= 400 && stopped.status < 500 && stopped.status !== 409) {
      return reply({ error: 'driver_stop_failed', detail: stopped.body }, stopped.status);
    }
    if (stopped.status >= 300) return reply({ error: 'driver_stop_pending', state: 'pending_stop', pending_stop: true, detail: stopped.body }, 202);
    const after = await this.call(this.sessions, '/api/sessions/' + row.session_id, auth);
    if (after.status === 200 && terminal.has(after.body.state)) {
      this.sql.exec("UPDATE hub_drivers SET phase='stopped' WHERE id=?", row.id);
      return reply({ ...(await this.view(this.get(row.id), auth)), stopped: true });
    }
    return reply({ ...(await this.view(this.get(row.id), auth)), state: 'pending_stop', pending_stop: true, command: stopped.body }, 202);
  }
  private async start(row: Row, auth: Auth): Promise<Response> {
    auth = this.rowAuth(auth, row);
    const config = JSON.parse(row.config);
    const started = await this.call(this.sessions, '/api/sessions', auth, {
      key: 'driver-' + row.id, workspace: config.workspace, mode: 'interactive',
      name: 'hub-driver',
      ...(config.engine ? { engine: config.engine, engine_override_reason: 'Explicit Hub driver engine selection' } : {}),
      ...(config.host ? { machine: config.host } : {}),
    });
    if (typeof started.body.id === 'string') {
      row.session_id = started.body.id;
      this.sql.exec('UPDATE hub_drivers SET session_id=? WHERE id=?', row.session_id, row.id);
    }
    if (started.status >= 400 || terminal.has(String(started.body.state ?? ''))) {
      this.sql.exec("UPDATE hub_drivers SET phase='rejected' WHERE id=?", row.id);
      return reply({ error: 'driver_start_failed', detail: started.body, retry_key: row.request_key },
        started.status >= 400 ? started.status : 503);
    }
    if (!row.session_id) {
      return reply({ error: 'driver_start_failed', detail: started.body, retry_key: row.request_key }, started.status >= 300 ? started.status : 503);
    }
    // Every engine starts at its normal empty interactive prompt. Deliver the
    // standing order through the existing durable, readiness-checked queue.
    // A timer or queued command is never represented as processed startup.
    const at = new Date(this.now()).toISOString();
    this.sql.exec(`INSERT OR IGNORE INTO session_deliveries
      (id,source,source_id,delivery_key,target_label,body,status,attempts,next_attempt_at,created_at,updated_at)
      VALUES (?,'driver',?,'startup',?,?,'pending',0,?,?,?)`,
      crypto.randomUUID(), row.id, row.session_id, DRIVER_PROMPT, at, at, at);
    // A capacity-queued session still owns the singleton and needs its schedule
    // for future activation. Delivery waits for a verified running native binding.
    const schedule = await this.ensureSchedule(row, auth);
    if (!schedule) return reply({ error: 'driver_schedule_failed',
      session: started.body, retry_key: row.request_key }, 503);
    this.sql.exec("UPDATE hub_drivers SET schedule_id=?,phase='active' WHERE id=?", schedule.body.id, row.id);
    return reply({ ...(await this.view(this.get(row.id), auth)), created: true }, 202);
  }
  /** A create response can be lost after the schedule transaction commits. The
   * random generation id makes its name unique; verify every public field
   * before recovering it after name_taken rather than creating another ping. */
  private async ensureSchedule(row: Row, auth: Auth) {
    const name = 'hub-driver-' + row.id;
    const expected = { name, target_label: row.session_id, cron: '*/15 * * * *', tz: 'UTC',
      prompt_id: null, prompt_bytes: DRIVER_PING.length, enabled: true, created_by: row.actor };
    const matches = (body: any) => typeof body?.id === 'string' &&
      Object.entries(expected).every(([key, value]) => body[key] === value);
    const created = await this.call(this.schedules, '/api/session-schedules', auth, {
      name, target_label: row.session_id, cron: '*/15 * * * *', tz: 'UTC', prompt: DRIVER_PING,
    });
    if (matches(created.body)) return created;
    const existing = await this.call(this.schedules, '/api/session-schedules/' + encodeURIComponent(name), auth);
    return existing.status === 200 && matches(existing.body) ? existing : null;
  }
  private async mutate(action: string, input: any, auth: Auth): Promise<Response> {
    const actor = auth.caller!.actor ?? 'admin';
    const canonical = JSON.stringify({ action, engine: input.engine, host: input.host,
      workspace: input.workspace, force: input.force });
    let op: any = this.sql.exec('SELECT * FROM hub_driver_operations WHERE actor=? AND request_key=?', actor, input.key).toArray()[0];
    if (op && op.input !== canonical) return reply({ error: 'idempotency_conflict' }, 409);
    if (!op) {
      op = { target_id: this.latest()?.id ?? null, result_id: null };
      this.sql.exec('INSERT INTO hub_driver_operations (actor,request_key,input,target_id) VALUES (?,?,?,?)',
        actor, input.key, canonical, op.target_id);
    }
    const current = op.target_id ? this.get(op.target_id) : undefined;
    if (action === 'stop') return current ? this.stop(current, auth) : reply({ driver: null, stopped: true });
    if (op.result_id) {
      const prior = this.get(op.result_id)!;
      if (prior.phase === 'starting') return this.start(prior, this.rowAuth(auth, prior));
      return reply({ ...(await this.view(prior, auth)), replay: true });
    }
    let inherited: any = {};
    if (current) {
      inherited = JSON.parse(current.config);
      const observed = current.session_id ? await this.call(this.sessions, '/api/sessions/' + current.session_id, auth) : null;
      const live = !['rejected', 'stopped'].includes(current.phase) && (!observed || observed.status !== 200 || !terminal.has(observed.body.state));
      if (live && action === 'start' && !input.force) {
        if (current.phase === 'stopping') return reply({ error: 'driver_stopping', ...(await this.view(current, auth)) }, 409);
        if (current.phase === 'starting') {
          const recovered = await this.start(current, this.rowAuth(auth, current));
          if (recovered.status >= 300) return recovered;
          this.sql.exec('UPDATE hub_driver_operations SET result_id=? WHERE actor=? AND request_key=?', current.id, actor, input.key);
          return reply({ ...(await this.view(this.get(current.id), auth)), reused: true }, recovered.status);
        }
        this.sql.exec('UPDATE hub_driver_operations SET result_id=? WHERE actor=? AND request_key=?', current.id, actor, input.key);
        return reply({ ...(await this.view(current, auth)), reused: true });
      }
      // Disable the old schedule even after natural session termination.
      const stopped = await this.stop(current, auth);
      if (stopped.status !== 200) return stopped;
    }
    const latest = this.latest();
    if (latest && latest.id !== current?.id && latest.phase !== 'stopped' && latest.phase !== 'rejected') {
      return reply({ error: 'driver_changed', ...(await this.view(latest, auth)) }, 409);
    }
    const config = { workspace: input.workspace ?? (action === 'restart' ? inherited.workspace : undefined) ?? 'agent-hub',
      engine: input.engine ?? (action === 'restart' ? inherited.engine : undefined),
      host: input.host ?? (action === 'restart' ? inherited.host : undefined), force: input.force, action };
    const row: Row = { id: crypto.randomUUID(), actor: auth.caller!.actor ?? 'admin', request_key: input.key,
      config: JSON.stringify(config), session_id: null, schedule_id: null, phase: 'starting', created_at: new Date(this.now()).toISOString() };
    this.sql.exec('INSERT INTO hub_drivers (id,actor,request_key,config,phase,created_at) VALUES (?,?,?,?,?,?)',
      row.id, row.actor, row.request_key, row.config, row.phase, row.created_at);
    this.sql.exec('UPDATE hub_driver_operations SET result_id=? WHERE actor=? AND request_key=?', row.id, actor, input.key);
    return this.start(row, auth);
  }
}
