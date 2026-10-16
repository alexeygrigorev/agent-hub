/** Generic account registry and placement. Provider credentials never enter this service. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { RoutingService } from './routing';
import { ClaimAdmission, sameIntent, type EditAuthority, type AdmissionCaller } from './claim_admission';
import { effectiveCheckin } from './registry';

export const FLEET_SCHEMA = `
CREATE TABLE IF NOT EXISTS fleet_accounts (label TEXT PRIMARY KEY, provider TEXT NOT NULL, max_concurrency INTEGER NOT NULL, reserve_percent REAL NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_routes (machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, PRIMARY KEY(machine_id, engine, account));
CREATE TABLE IF NOT EXISTS fleet_subscriptions (machine_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL, plan TEXT, observed_at TEXT NOT NULL, quota_json TEXT NOT NULL, PRIMARY KEY(machine_id, account));
CREATE TABLE IF NOT EXISTS fleet_sessions (machine_id TEXT NOT NULL, native_id TEXT NOT NULL, tag TEXT NOT NULL, engine TEXT, phase TEXT NOT NULL, agent_state TEXT, observed_at TEXT NOT NULL, workspace TEXT NOT NULL, profile TEXT, account TEXT, last_activity_at TEXT, PRIMARY KEY(machine_id, native_id));
CREATE TABLE IF NOT EXISTS fleet_launches (id TEXT PRIMARY KEY, actor TEXT NOT NULL, requester_kind TEXT NOT NULL, parent_id TEXT, summary TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, mode TEXT NOT NULL, workspace TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT, launcher_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
`;
type Auth = { caller?: AdmissionCaller; scopes?: string[]; error?: Response };
const LABEL = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const PROVIDERS = new Set(['codex', 'claude', 'grok', 'gemini', 'zai', 'go', 'copilot']);
const ACTIVE = "('offered','starting','running','uncertain')";
const HEADROOM: Record<string, number> = { codex: 15, grok: 5 };
const HEADERS = { 'Cache-Control': 'private, no-store' };
function response(value: unknown, status = 200) { return Response.json(value, { status, headers: HEADERS }); }
function label(value: unknown): value is string { return typeof value === 'string' && LABEL.test(value); }
function object(value: any, keys: string[]): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => keys.includes(k));
}
function parsed(value: any, fallback: any = {}) { try { return JSON.parse(value || '{}'); } catch { return fallback; } }
function sanitizedQuota(raw: any): any {
  if (!object(raw, ['status', 'observed_at', 'details', 'windows']) || !['ok', 'unknown', 'error'].includes(raw.status) || !object(raw.details, ['limit_reached', 'has_grok_code_access', 'stale']) || !object(raw.windows, Object.keys(raw.windows || {})) || Object.keys(raw.windows).length > 32) return null;
  const details: any = {};
  for (const [key, value] of Object.entries(raw.details)) { if (value !== null && typeof value !== 'boolean') return null; details[key] = value; }
  const windows: any = {};
  for (const [key, w] of Object.entries(raw.windows) as any) {
    if (!label(key) || !object(w, ['present', 'percent_remaining', 'reset_at', 'rolling']) || typeof w.present !== 'boolean' || (w.percent_remaining !== null && (typeof w.percent_remaining !== 'number' || !Number.isFinite(w.percent_remaining) || w.percent_remaining < 0 || w.percent_remaining > 100)) || (w.reset_at !== null && (typeof w.reset_at !== 'string' || !Number.isFinite(Date.parse(w.reset_at)))) || (w.rolling !== undefined && typeof w.rolling !== 'boolean')) return null;
    if (!w.present && (w.percent_remaining !== null || w.reset_at !== null)) return null;
    windows[key] = { present: w.present, percent_remaining: w.percent_remaining, reset_at: w.reset_at, rolling: w.rolling === true };
  }
  if (raw.observed_at !== null && (typeof raw.observed_at !== 'string' || !Number.isFinite(Date.parse(raw.observed_at)))) return null;
  return { status: raw.status, observed_at: raw.observed_at, details, windows };
}
function fresh(value: any, now: number, ttl: number) {
  const at = typeof value === 'string' && /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? Date.parse(value) : NaN;
  return Number.isFinite(at) && at <= now && now - at <= ttl;
}
async function body(request: Request): Promise<any> {
  if (!request.body) return null;
  const reader = request.body.getReader(); let size = 0; const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.byteLength;
      if (size > 32768) { await reader.cancel(); return null; }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch { return null; } finally { reader.releaseLock(); }
}

export class FleetService {
  public routing: RoutingService;
  public admission: ClaimAdmission;
  constructor(private sql: SqlStorage, private transaction: <T>(fn: () => T) => T, private now: () => number = Date.now) {
    this.routing = new RoutingService(sql, this, now, transaction);
    this.admission = new ClaimAdmission(sql, transaction, now);
  }
  init() {
    this.routing.init();
    this.admission.init();
    for (const stmt of FLEET_SCHEMA.split(';')) if (stmt.trim()) this.sql.exec(stmt);
    const columns = this.sql.exec('PRAGMA table_info(fleet_sessions)').toArray().map((c: any) => c.name);
    if (!columns.includes('last_activity_at')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN last_activity_at TEXT');
  }

  private observation(account: any, route: any) {
    const subscription: any = this.sql.exec('SELECT * FROM fleet_subscriptions WHERE machine_id = ? AND account = ?', route.machine_id, account.label).toArray()[0];
    if (subscription) {
      if (subscription.service !== account.provider || !fresh(subscription.observed_at, this.now(), 1200000)) return null;
      const quota = parsed(subscription.quota_json);
      return fresh(quota.observed_at, this.now(), 1200000) ? quota : null;
    }
    // A default CLI reading cannot describe a named profile or two accounts on one service.
    if (route.profile !== null) return null;
    const count = Number(this.sql.exec('SELECT COUNT(DISTINCT r.account) AS n FROM fleet_routes r JOIN fleet_accounts a ON r.account = a.label WHERE r.machine_id = ? AND a.provider = ?', route.machine_id, account.provider).one().n);
    const snapshot: any = this.sql.exec('SELECT * FROM host_quota_snapshots WHERE host_id = ?', route.machine_id).toArray()[0];
    if (count !== 1 || !snapshot || !fresh(snapshot.captured_at, this.now(), 1200000)) return null;
    const quota = parsed(snapshot.routes_json)[account.provider];
    if (!quota || !fresh(quota.observed_at, this.now(), 1200000)) return null;
    return snapshot.status === 'ok' ? quota : { ...quota, status: 'unknown' };
  }

  accountStatus(account: any) {
    const routes = this.sql.exec('SELECT machine_id, profile FROM fleet_routes WHERE account = ?', account.label).toArray();
    let remaining: number | null = null; let observed = 0; let unknown = false; let blocked = !account.enabled;
    for (const route of routes) {
      const quota = this.observation(account, route);
      if (!quota) continue;
      observed++;
      if (quota.status !== 'ok' || quota.details?.stale === true) { unknown = true; continue; }
      if (quota.details?.limit_reached === true) blocked = true;
      if (quota.details?.limit_reached !== false || (account.provider === 'grok' && quota.details?.has_grok_code_access !== true)) unknown = true;
      let windows = 0;
      for (const win of Object.values(quota.windows || {}) as any[]) {
        if (win.present === false) continue;
        windows++;
        if (win.present !== true || typeof win.percent_remaining !== 'number' || !Number.isFinite(win.percent_remaining) || win.percent_remaining < 0 || win.percent_remaining > 100 || !fresh(win.reset_at, this.now() + 366 * 86400000, 366 * 86400000)) { unknown = true; continue; }
        remaining = remaining === null ? win.percent_remaining : Math.min(remaining, win.percent_remaining);
      }
      if (!windows) unknown = true;
    }
    if (remaining !== null && remaining <= account.reserve_percent) blocked = true;
    const reserved = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_launches WHERE account = ? AND state IN ${ACTIVE}`, account.label).one().n);
    const observedSessions = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_sessions s WHERE s.account = ? AND s.phase IN ('starting','running') AND NOT EXISTS (SELECT 1 FROM fleet_launches l WHERE l.machine_id = s.machine_id AND l.native_id = s.native_id AND l.state IN ${ACTIVE})`, account.label).one().n);
    const active = reserved + observedSessions;
    return { label: account.label, provider: account.provider, max_concurrency: account.max_concurrency, reserve_percent: account.reserve_percent, enabled: !!account.enabled,
      remaining_percent: remaining, status: blocked ? 'blocked' : (!observed || unknown || remaining === null) ? 'unknown' : 'ok', active, available_slots: Math.max(0, Number(account.max_concurrency) - active),
      machines: routes.map(r => r.machine_id) };
  }

  plan(input: any) {
    if (!object(input, ['mode', 'machine', 'engine', 'account', 'workspace', 'prompt', 'idempotency_key', 'parent_id', 'summary', 'access', 'approved_action']) ||
      !label(input.workspace) || (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) ||
      (input.account !== undefined && !label(input.account)) || (input.machine !== undefined && !label(input.machine)) || (input.engine !== undefined && !label(input.engine)) ||
      (input.mode === 'interactive' && !input.machine) ||
      (input.parent_id !== undefined && input.parent_id !== null && (typeof input.parent_id !== 'string' || !/^[a-f0-9-]{36}$/.test(input.parent_id))) ||
      (input.summary !== undefined && (typeof input.summary !== 'string' || input.summary.length > 240 || /[\x00-\x1f\x7f]/.test(input.summary))) ||
      (input.prompt !== undefined && (typeof input.prompt !== 'string' || input.prompt.length > 16000)) ||
      (input.approved_action !== undefined && (typeof input.approved_action !== 'string' || !input.approved_action.trim() || input.approved_action.length > 12000)) ||
      (input.mode !== 'interactive' && !input.prompt?.trim())) return { error: 'invalid_launch_request' };
    const request: any = { mode: input.mode || 'headless', machine: input.machine || null, engine: input.engine || null, workspace: input.workspace, prompt: input.prompt || '', parent_id: input.parent_id || null, summary: input.summary || (input.mode === 'interactive' ? 'Interactive session' : 'Agent task') };
    if (input.account !== undefined) request.account = input.account;
    if (input.approved_action !== undefined) request.approved_action = input.approved_action;
    const rejected: { machine: string; engine: string; reason: string }[] = []; const candidates: any[] = [];
    const accounts = new Map(this.sql.exec('SELECT * FROM fleet_accounts').toArray().map(a => [a.label, this.accountStatus(a)]));
    const definitions = new Map(this.sql.exec('SELECT * FROM fleet_accounts').toArray().map(a => [a.label, a]));
    for (const route of this.sql.exec('SELECT * FROM fleet_routes ORDER BY machine_id, engine').toArray() as any[]) {
      if ((request.account && route.account !== request.account) || (request.machine && route.machine_id !== request.machine) || (request.engine && route.engine !== request.engine)) continue;
      const machine: any = this.sql.exec('SELECT * FROM machines WHERE id = ?', route.machine_id).toArray()[0];
      const account = accounts.get(route.account); const caps = parsed(machine?.capabilities); const hb = parsed(machine?.last_heartbeat);
      let reason = '';
      if (!machine || machine.drained || !fresh(effectiveCheckin(machine.id, machine.last_checkin_at), this.now(), 180000)) reason = 'machine_unavailable';
      else if (!Array.isArray(caps.engines) || !caps.engines.includes(route.engine) || !Array.isArray(caps.fleet_modes) || !caps.fleet_modes.includes(request.mode) || !Array.isArray(caps.workspaces) || !caps.workspaces.includes(request.workspace)) reason = 'capability_unavailable';
      else if (hb.gate?.launch_allowed !== true || !fresh(hb.gate?.at, this.now(), 300000)) reason = 'admission_unknown';
      else if (!Number.isFinite(hb.mem_avail_mb) || hb.mem_avail_mb < 10240 + 1500 || !Number.isFinite(hb.disk_free_gb) || hb.disk_free_gb < 50.5) reason = 'resource_limit';
      else if (!Array.isArray(hb.load) || !Number.isFinite(hb.load[0]) || hb.load[0] < 0 || !Number.isFinite(caps.cpu_count) || caps.cpu_count < 1) reason = 'load_unknown';
      else if (!account || account.status !== 'ok') reason = 'account_' + (account?.status || 'unmapped');
      else if (!this.observation(definitions.get(route.account), route)) reason = 'machine_account_quota_unknown';
      else if (account.available_slots < 1) reason = 'account_capacity';
      if (reason) { rejected.push({ machine: route.machine_id, engine: route.engine, reason }); continue; }
      const pending = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_launches WHERE machine_id = ? AND state IN ${ACTIVE}`, route.machine_id).one().n);
      const routingInstalled = this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'routing_machines'").toArray().length;
      const limit: any = routingInstalled ? this.sql.exec('SELECT max_agents FROM routing_machines WHERE machine_id = ?', route.machine_id).toArray()[0] : null;
      const observedSessions = Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_sessions s WHERE s.machine_id = ? AND s.phase IN ('starting','running') AND NOT EXISTS (SELECT 1 FROM fleet_launches l WHERE l.machine_id = s.machine_id AND l.native_id = s.native_id AND l.state IN ${ACTIVE})`, route.machine_id).one().n);
      if (limit && pending + observedSessions >= limit.max_agents) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'machine_capacity' }); continue; }

      const score = (account!.remaining_percent! / 100) / (1 + hb.load[0] / caps.cpu_count + pending);
      candidates.push({ machine: route.machine_id, engine: route.engine, account: route.account, profile: route.profile, score });
    }
    candidates.sort((a, b) => b.score - a.score || a.machine.localeCompare(b.machine) || a.engine.localeCompare(b.engine));
    return { request, selected: candidates[0] || null, candidates, rejected };
  }

  /** All final placement gates, authority reads and capacity writes share this transaction. */
  reserve(input: any, auth: Auth, expectedAuthority?: EditAuthority, preview = false): { value: any; status: number } {
    const result = (value: any, status = 200) => ({ value, status });
    const caller = auth.caller;
    if (!caller || !(caller.isAdmin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch')))) return result({ error: 'forbidden' }, 403);
    return this.transaction(() => {
      const plan = this.plan(input);
      if ('error' in plan) return result(plan, 400);
      const parsed = this.admission.parseDeclaration(input?.access);
      if ('error' in parsed) return result({ error: parsed.error }, 400);
      if (parsed.access.mode === 'edit' && !input.approved_action?.trim()) return result({ error: 'approved_action_required' }, 400);
      const bound = { ...plan.request, access: parsed.access };
      if (preview) {
        const preview = this.admission.evaluate(caller, plan.request.workspace, parsed.access, 'enqueue');
        if (!preview.ok) return result({ error: preview.error, access: parsed.access.mode }, preview.status);
        return result({ ...plan, access: parsed.access.mode });
      }
      if (!label(input.idempotency_key)) return result({ error: 'idempotency_key_required' }, 400);
      const actor = caller.actor || caller.machineId || 'admin'; const canonical = JSON.stringify(bound);
      const prior: any = this.sql.exec('SELECT * FROM fleet_launches WHERE actor = ? AND request_key = ?', actor, input.idempotency_key).toArray()[0];
      if (prior) return sameIntent(prior.request_json, canonical) ? result({ id: prior.id, state: prior.state, machine: prior.machine_id, engine: prior.engine, account: prior.account, mode: prior.mode, access: JSON.parse(prior.request_json).access?.mode || 'legacy_unspecified', replay: true }) : result({ error: 'idempotency_conflict' }, 409);
      if (plan.request.parent_id) {
        const parent: any = this.sql.exec('SELECT actor, state FROM fleet_launches WHERE id = ?', plan.request.parent_id).toArray()[0];
        if (!parent) return result({ error: 'parent_not_found' }, 400);
        if (parent.actor !== actor) return result({ error: 'parent_not_owned' }, 403);
        if (!['starting', 'running'].includes(parent.state)) return result({ error: 'parent_not_active' }, 409);
      }
      const admitted = this.admission.evaluate(caller, plan.request.workspace, parsed.access, 'reserve', { authority: expectedAuthority });
      if (!admitted.ok) return result({ error: admitted.error, access: parsed.access.mode }, admitted.status);
      if (!plan.selected) return result({ error: 'no_eligible_route', rejected: plan.rejected, access: parsed.access.mode }, 409);
      const id = crypto.randomUUID(); const at = new Date(this.now()).toISOString(); const selected = plan.selected;
      this.sql.exec('INSERT INTO fleet_launches (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', id, actor, caller.kind || 'unknown', plan.request.parent_id, plan.request.summary, input.idempotency_key, JSON.stringify({ ...bound, ...(admitted.authority ? { edit_authority: admitted.authority } : {}) }), selected.machine, selected.engine, selected.account, selected.profile, plan.request.mode, plan.request.workspace, plan.request.prompt, 'offered', at, at);
      return result({ id, state: 'offered', machine: selected.machine, engine: selected.engine, account: selected.account, mode: plan.request.mode, access: admitted.access.mode }, 201);
    });
  }

  async handle(request: Request, auth: Auth): Promise<Response> {
    if (auth.error) return auth.error;
    const path = new URL(request.url).pathname; const caller = auth.caller;
    if (path === '/api/fleet/tasks' || path.startsWith('/api/fleet/tasks/') || path.startsWith('/api/fleet/routing/')) return this.routing.handle(request, auth);
    if (!caller) return response({ error: 'unauthorized' }, 401);
    const admin = !!caller.isAdmin; const host = caller.kind === 'host' && !!caller.machineId && auth.scopes?.includes('host');
    const writer = admin || (caller.kind === 'agent' && auth.scopes?.includes('fleet:launch'));
    if (path === '/api/fleet' && request.method === 'GET') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      return response({ accounts: this.sql.exec('SELECT * FROM fleet_accounts ORDER BY label').toArray().map(a => this.accountStatus(a)), routes: this.sql.exec('SELECT * FROM fleet_routes ORDER BY machine_id, engine').toArray(),
        machines: this.sql.exec('SELECT id, name, hostname, last_checkin_at, drained FROM machines ORDER BY id').toArray().map((m: any) => ({ ...m, last_checkin_at: effectiveCheckin(m.id, m.last_checkin_at) })),
        agents: this.sql.exec('SELECT tag, project, role, machine_id, engine, session_id, health_state, health_at FROM agents').toArray(),
        sessions: this.sql.exec('SELECT * FROM fleet_sessions ORDER BY machine_id, tag').toArray(),
        subscriptions: this.sql.exec('SELECT * FROM fleet_subscriptions ORDER BY machine_id, service, account').toArray().map((s: any) => ({ machine_id: s.machine_id, account: s.account, service: s.service, plan: s.plan, observed_at: s.observed_at, stale: !fresh(s.observed_at, this.now(), 1200000), quota: parsed(s.quota_json) })),
        launches: this.sql.exec('SELECT * FROM fleet_launches ORDER BY created_at DESC LIMIT 200').toArray().map((row: any) => { const { request_json, request_key, prompt, ...view } = row; return { ...view, access: parsed(request_json).access?.mode || 'legacy_unspecified', admission_hold: ['offered', 'starting', 'running', 'uncertain'].includes(row.state) ? this.admission.heldReason(row) : null }; }) });
    }
    if (path === '/api/fleet/claim-policy' && request.method === 'GET') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      return response({ workspaces: this.admission.listPolicy() });
    }
    if (path === '/api/fleet/claim-policy' && request.method === 'PUT') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['workspaces']) || !Array.isArray(data.workspaces) || data.workspaces.length > 128) return response({ error: 'invalid_claim_policy' }, 400);
      const names = new Set<string>();
      const rows: { workspace: string; project: string | null; require_claim: boolean }[] = [];
      for (const item of data.workspaces) {
        if (!object(item, ['workspace', 'project', 'require_claim']) || !label(item.workspace) || (item.project !== null && !label(item.project)) || typeof item.require_claim !== 'boolean' || names.has(item.workspace)) return response({ error: 'invalid_claim_policy' }, 400);
        names.add(item.workspace);
        rows.push({ workspace: item.workspace, project: item.project, require_claim: item.require_claim });
      }
      return this.transaction(() => { this.admission.replacePolicy(rows); return response({ status: 'configured', workspaces: this.admission.listPolicy() }); });
    }
    if (path === '/api/fleet/accounts' && request.method === 'PUT') {
      if (!admin) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['accounts', 'routes']) || !Array.isArray(data.accounts) || !Array.isArray(data.routes) || data.accounts.length > 128 || data.routes.length > 512) return response({ error: 'invalid_registry' }, 400);
      const labels = new Set(); const keys = new Set();
      for (const a of data.accounts) {
        if (!object(a, ['label', 'provider', 'max_concurrency', 'reserve_percent', 'enabled']) || !label(a.label) || !PROVIDERS.has(a.provider) || !Number.isInteger(a.max_concurrency) || a.max_concurrency < 1 || a.max_concurrency > 100 || !Number.isFinite(a.reserve_percent) || a.reserve_percent < (HEADROOM[a.provider] || 0) || a.reserve_percent > 100 || typeof a.enabled !== 'boolean' || labels.has(a.label)) return response({ error: 'invalid_account' }, 400);
        labels.add(a.label);
      }
      for (const r of data.routes) {
        if (!object(r, ['machine_id', 'engine', 'account', 'profile'])) return response({ error: 'invalid_route' }, 400);
        const key = r.machine_id + ':' + r.engine + ':' + r.account;
        if (!object(r, ['machine_id', 'engine', 'account', 'profile']) || !label(r.machine_id) || !label(r.engine) || !labels.has(r.account) || (r.profile !== null && !label(r.profile)) || keys.has(key) || !this.sql.exec('SELECT id FROM machines WHERE id = ?', r.machine_id).toArray().length) return response({ error: 'invalid_route' }, 400);
        keys.add(key);
      }
      return this.transaction(() => {
        if (Number(this.sql.exec(`SELECT COUNT(*) AS n FROM fleet_launches WHERE state IN ${ACTIVE}`).one().n)) return response({ error: 'active_launches_hold_registry' }, 409);
        this.sql.exec('DELETE FROM fleet_subscriptions');
        this.sql.exec('DELETE FROM fleet_routes'); this.sql.exec('DELETE FROM fleet_accounts');
        for (const a of data.accounts) this.sql.exec('INSERT INTO fleet_accounts VALUES (?, ?, ?, ?, ?)', a.label, a.provider, a.max_concurrency, a.reserve_percent, Number(a.enabled));
        for (const r of data.routes) this.sql.exec('INSERT INTO fleet_routes VALUES (?, ?, ?, ?)', r.machine_id, r.engine, r.account, r.profile);
        return response({ status: 'configured' });
      });
    }
    if (['/api/fleet/plan', '/api/fleet/launches'].includes(path) && request.method === 'POST') {
      if (!writer) return response({ error: 'forbidden' }, 403);
      const input = await body(request);
      const reserved = this.reserve(input, auth, undefined, path.endsWith('/plan'));
      return response(reserved.value, reserved.status);
    }

    if (path === '/api/fleet/offers' && request.method === 'GET') {
      if (!host) return response({ error: 'forbidden' }, 403);
      await this.routing.dispatch();
      const offers: any[] = [];
      // Held effects keep capacity, but must not hide later eligible offers on other slots.
      for (const row of this.sql.exec("SELECT * FROM fleet_launches WHERE machine_id = ? AND state = 'offered' ORDER BY created_at, id", caller.machineId!)) {
        if (this.admission.heldReason(row)) continue;
        offers.push({ id: row.id, engine: row.engine, account: row.account, profile: row.profile, mode: row.mode, workspace: row.workspace, prompt: row.prompt, access: parsed(row.request_json).access?.mode || 'legacy_unspecified' });
        if (offers.length === 20) break;
      }
      return response({ offers: this.routing.enrichOffers(offers) });
    }
    if (path === '/api/fleet/sessions' && request.method === 'PUT') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['sessions']) || !Array.isArray(data.sessions) || data.sessions.length > 200) return response({ error: 'invalid_sessions' }, 400);
      const ids = new Set();
      for (const s of data.sessions) {
        if (!object(s, ['native_id', 'tag', 'engine', 'phase', 'agent_state', 'workspace', 'profile', 'account', 'last_activity_at']) || (s.last_activity_at !== undefined && s.last_activity_at !== null && (typeof s.last_activity_at !== 'string' || !fresh(s.last_activity_at, this.now() + 60000, 366 * 86400000))) || typeof s.native_id !== 'string' || !/^[a-zA-Z0-9-]{1,160}$/.test(s.native_id) || typeof s.tag !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$/.test(s.tag) || (s.engine !== null && !label(s.engine)) || !label(s.phase) || (s.agent_state !== null && !label(s.agent_state)) || typeof s.workspace !== 'string' || s.workspace.length > 2048 || /[\x00-\x1f\x7f]/.test(s.workspace) || (s.profile !== null && !label(s.profile)) || (s.account !== null && !label(s.account)) || ids.has(s.native_id)) return response({ error: 'invalid_session' }, 400);
        ids.add(s.native_id);
      }
      return this.transaction(() => {
        // Write only rows that changed: hosts report every minute and Durable Object row writes
        // are metered. observed_at therefore means "last changed"; host freshness comes from heartbeats.
        const fields = ['tag', 'engine', 'phase', 'agent_state', 'workspace', 'profile', 'account', 'last_activity_at'];
        const existing = new Map((this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ?', caller.machineId!).toArray() as any[]).map(r => [r.native_id, r]));
        let written = 0;
        for (const s of data.sessions) {
          const prior = existing.get(s.native_id); existing.delete(s.native_id);
          const row = { ...s, last_activity_at: s.last_activity_at ?? null };
          if (prior && fields.every(f => (prior[f] ?? null) === (row[f] ?? null))) continue;
          this.sql.exec('INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, profile, account, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', caller.machineId!, s.native_id, s.tag, s.engine, s.phase, s.agent_state, new Date(this.now()).toISOString(), s.workspace, s.profile, s.account, row.last_activity_at);
          written++;
        }
        for (const gone of existing.keys()) { this.sql.exec('DELETE FROM fleet_sessions WHERE machine_id = ? AND native_id = ?', caller.machineId!, gone); written++; }
        return response({ status: 'reported', written });
      });
    }
    if (path === '/api/fleet/subscriptions' && request.method === 'PUT') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['subscriptions']) || !Array.isArray(data.subscriptions) || data.subscriptions.length > 128) return response({ error: 'invalid_subscriptions' }, 400);
      const clean: any[] = []; const labels = new Set();
      for (const sub of data.subscriptions) {
        if (!object(sub, ['service', 'account', 'plan', 'quota']) || !label(sub.account) || !PROVIDERS.has(sub.service) || (sub.plan !== null && (typeof sub.plan !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9 ._+-]{0,63}$/.test(sub.plan))) || labels.has(sub.account)) return response({ error: 'invalid_subscription' }, 400);
        labels.add(sub.account);
        const mapped = this.sql.exec('SELECT a.label FROM fleet_accounts a JOIN fleet_routes r ON a.label = r.account WHERE r.machine_id = ? AND a.label = ? AND a.provider = ?', caller.machineId!, sub.account, sub.service).toArray();
        const quota = sanitizedQuota(sub.quota);
        if (!mapped.length || !quota || (quota.observed_at !== null && Date.parse(quota.observed_at) > this.now()) || (quota.status === 'ok' && !fresh(quota.observed_at, this.now(), 1200000))) return response({ error: 'subscription_unmapped_or_invalid' }, 400);
        clean.push({ ...sub, quota });
      }
      return this.transaction(() => {
        this.sql.exec('DELETE FROM fleet_subscriptions WHERE machine_id = ?', caller.machineId!);
        for (const s of clean) this.sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', caller.machineId!, s.account, s.service, s.plan, new Date(this.now()).toISOString(), JSON.stringify(s.quota));
        return response({ status: 'reported' });
      });
    }
    const match = /^\/api\/fleet\/launches\/([a-f0-9-]{36})\/(claim|result)$/.exec(path);
    if (match && request.method === 'POST') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      return this.transaction(() => {
        const row: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ? AND machine_id = ?', match[1], caller.machineId!).toArray()[0];
        if (!row) return response({ error: 'not_found' }, 404);
        if (match[2] === 'claim') {
          if (row.state !== 'offered') return response({ error: 'already_claimed' }, 409);
          if (!object(data, ['gate_allowed', 'observed_at', 'account', 'profile_supported']) || data.gate_allowed !== true || data.profile_supported !== true || data.account !== row.account || !fresh(data.observed_at, this.now(), 30000)) return response({ error: 'fresh_admission_required' }, 409);
          const account: any = this.sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', row.account).toArray()[0];
          if (!account || this.accountStatus(account).status !== 'ok' || !this.observation(account, row)) return response({ error: 'account_unavailable' }, 409);
          const bound = parsed(row.request_json);
          const declared = this.admission.parseDeclaration(bound.access);
          if ('error' in declared) return response({ error: 'claim_inactive', access: 'legacy_unspecified' }, 409);
          if (declared.access.mode === 'edit') {
            if (!bound.edit_authority) return response({ error: 'claim_authority_unsupported' }, 403);
            const admitted = this.admission.evaluate({ kind: 'agent', actor: bound.edit_authority?.actor, tokenId: bound.edit_authority?.credential_id }, row.workspace, declared.access, 'start', { launchId: row.id, authority: bound.edit_authority });
            if (!admitted.ok) return response({ error: admitted.error, access: declared.access.mode }, admitted.status);
          }
          this.sql.exec("UPDATE fleet_launches SET state = 'starting', updated_at = ? WHERE id = ?", new Date(this.now()).toISOString(), row.id);
          return response({ ...row, state: 'starting' });
        }
        if (!object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit']) || !['running', 'uncertain', 'failed', 'exited'].includes(data.state) || !['starting', 'running', 'uncertain'].includes(row.state)) return response({ error: 'invalid_transition' }, 409);
        if (['exited', 'failed'].includes(data.state) && this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) return response({ error: 'task_report_required' }, 409);
        if (['exited', 'failed'].includes(data.state) && data.confirmed_exit !== true) return response({ error: 'confirmed_exit_required' }, 409);
        if (data.state === 'running' && (typeof data.native_id !== 'string' || data.native_id.length < 1 || data.native_id.length > 160 || (row.mode === 'headless' && !label(data.launcher_task_id)))) return response({ error: 'native_receipt_required' }, 409);
        if ((data.native_id !== undefined && (typeof data.native_id !== 'string' || data.native_id.length > 160)) || (data.launcher_task_id !== undefined && !label(data.launcher_task_id))) return response({ error: 'invalid_receipt' }, 400);
        this.sql.exec('UPDATE fleet_launches SET state = ?, native_id = COALESCE(?, native_id), launcher_task_id = COALESCE(?, launcher_task_id), updated_at = ? WHERE id = ?', data.state, data.native_id || null, data.launcher_task_id || null, new Date(this.now()).toISOString(), row.id);
        return response({ id: row.id, state: data.state });
      });
    }
    return response({ error: 'not_found' }, 404);
  }
}
