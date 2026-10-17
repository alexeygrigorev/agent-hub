/** Generic account registry and placement. Provider credentials never enter this service. */
import type { SqlStorage } from '@cloudflare/workers-types';
import { RoutingService } from './routing';
import { ClaimAdmission, sameIntent, sameEditCaller, type EditAuthority, type AdmissionCaller } from './claim_admission';
import { effectiveCheckin } from './registry';
import { taskTier, policy } from './model-routing';
import { evaluateQuotaAdmission } from './quota_admission';

export const FLEET_SCHEMA = `
CREATE TABLE IF NOT EXISTS fleet_accounts (label TEXT PRIMARY KEY, provider TEXT NOT NULL, max_concurrency INTEGER NOT NULL, reserve_percent REAL NOT NULL, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fleet_routes (machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, PRIMARY KEY(machine_id, engine, account));
CREATE TABLE IF NOT EXISTS fleet_subscriptions (machine_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL, plan TEXT, observed_at TEXT NOT NULL, quota_json TEXT NOT NULL, PRIMARY KEY(machine_id, account));
CREATE TABLE IF NOT EXISTS fleet_sessions (machine_id TEXT NOT NULL, native_id TEXT NOT NULL, tag TEXT NOT NULL, engine TEXT, phase TEXT NOT NULL, agent_state TEXT, observed_at TEXT NOT NULL, workspace TEXT NOT NULL, profile TEXT, account TEXT, last_activity_at TEXT, cwd TEXT, PRIMARY KEY(machine_id, native_id));
CREATE TABLE IF NOT EXISTS fleet_launches (id TEXT PRIMARY KEY, actor TEXT NOT NULL, requester_kind TEXT NOT NULL, parent_id TEXT, summary TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, mode TEXT NOT NULL, workspace TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT, launcher_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(actor, request_key));
`;
type Auth = { caller?: AdmissionCaller; scopes?: string[]; error?: Response };
const LABEL = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const PROVIDERS = new Set(['codex', 'claude', 'grok', 'gemini', 'zai', 'go', 'copilot']);
const ACTIVE = "('offered','starting','running','uncertain')";
const HEADROOM: Record<string, number> = { codex: 15, grok: 5 };
/** Engines whose providers the canonical model policy disables. New grok sessions hold here. */
function disabledEngines(): Set<string> {
  const rules = ((policy as any).rules || {}) as any;
  const disabled = new Set<string>(Array.isArray(rules.disabled_providers) ? rules.disabled_providers : []);
  return new Set<string>((((policy as any).models || []) as any[]).filter(m => m && disabled.has(m.provider)).map(m => String(m.engine)));
}
const HEADERS = { 'Cache-Control': 'private, no-store' };
function response(value: unknown, status = 200) { return Response.json(value, { status, headers: HEADERS }); }
function label(value: unknown): value is string { return typeof value === 'string' && LABEL.test(value); }
/** Hub-side shape check for a requested working directory relative to the
 * workspace root: relative, confined (no traversal), bounded segments. The
 * host resolves it against allowlisted canonical roots with symlink
 * confinement before any native effect. */
function relativeCwdOk(value: unknown): value is string {
  if (typeof value !== 'string' || !value || value.length > 256) return false;
  if (value.startsWith('/') || value.includes('\\') || value.includes(':') || /[\x00-\x1f\x7f]/.test(value)) return false;
  return value === '.' || value.split('/').every(seg => seg.length > 0 && seg !== '.' && seg !== '..' && /^[A-Za-z0-9._-]{1,64}$/.test(seg));
}
function object(value: any, keys: string[]): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => keys.includes(k));
}
function parsed(value: any, fallback: any = {}) { try { return JSON.parse(value || '{}'); } catch { return fallback; } }
const NATIVE_AMOUNTS: Record<string, number> = { credits_remaining: 1e12, spend_used: 1e15, spend_limit: 1e15 };
const NATIVE_UNITS = new Set(['percent', 'credits', 'usd_cents', 'requests', 'tokens']);
const PROFILE_LABEL = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$/;
const COLLECTOR_VERSION = /^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,63}$/;
const UTC_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$/;
function nativeAmount(value: any, cap: number): boolean {
  return value === null || value === undefined || (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= cap);
}
function sanitizedQuota(raw: any): any {
  const topKeys = ['status', 'observed_at', 'details', 'windows'];
  const hasCv = !!raw && typeof raw === 'object' && !Array.isArray(raw) && (raw as any).collector_version !== undefined;
  if (!object(raw, hasCv ? [...topKeys, 'collector_version'] : topKeys) || !['ok', 'unknown', 'error'].includes(raw.status) || !object(raw.details, Object.keys(raw.details)) || !object(raw.windows, Object.keys(raw.windows || {})) || Object.keys(raw.windows).length > 32) return null;
  if (raw.collector_version !== undefined && raw.collector_version !== null && (typeof raw.collector_version !== 'string' || !COLLECTOR_VERSION.test(raw.collector_version))) return null;
  const details: any = {};
  for (const [key, value] of Object.entries(raw.details)) {
    if (['limit_reached', 'has_grok_code_access', 'stale'].includes(key)) {
      if (value !== null && typeof value !== 'boolean') return null;
    } else if (key in NATIVE_AMOUNTS) {
      if (!nativeAmount(value, NATIVE_AMOUNTS[key])) return null;
    } else if (key === 'unit') {
      if (value !== null && (typeof value !== 'string' || !NATIVE_UNITS.has(value))) return null;
    } else if (key === 'profile') {
      if (value !== null && (typeof value !== 'string' || !PROFILE_LABEL.test(value))) return null;
    } else if (key === 'cooldown_until') {
      if (value !== null && (typeof value !== 'string' || value.length > 64 || !UTC_ISO.test(value) || !Number.isFinite(Date.parse(value)))) return null;
    } else return null;
    details[key] = value;
  }
  const windows: any = {};
  for (const [key, w] of Object.entries(raw.windows) as any) {
    if (!label(key) || !object(w, Object.keys(w)) || typeof w.present !== 'boolean' || (w.percent_remaining !== null && (typeof w.percent_remaining !== 'number' || !Number.isFinite(w.percent_remaining) || w.percent_remaining < 0 || w.percent_remaining > 100)) || (w.reset_at !== null && (typeof w.reset_at !== 'string' || !Number.isFinite(Date.parse(w.reset_at)))) || (w.rolling !== undefined && typeof w.rolling !== 'boolean') || (w.error !== undefined && w.error !== null && w.error !== 'unavailable')) return null;
    for (const nk of Object.keys(NATIVE_AMOUNTS)) if (!nativeAmount(w[nk], NATIVE_AMOUNTS[nk])) return null;
    if (w.unit !== undefined && w.unit !== null && (typeof w.unit !== 'string' || !NATIVE_UNITS.has(w.unit))) return null;
    if (w.profile !== undefined && w.profile !== null && (typeof w.profile !== 'string' || !PROFILE_LABEL.test(w.profile))) return null;
    for (const k of Object.keys(w)) if (!['present', 'percent_remaining', 'reset_at', 'rolling', 'error', ...Object.keys(NATIVE_AMOUNTS), 'unit', 'profile'].includes(k)) return null;
    const nativesNull = w.credits_remaining == null && w.spend_used == null && w.spend_limit == null && w.unit == null && w.profile == null;
    if (!w.present && (w.percent_remaining !== null || w.reset_at !== null || !nativesNull)) return null;
    windows[key] = { present: w.present, percent_remaining: w.percent_remaining, reset_at: w.reset_at, rolling: w.rolling === true, error: w.error === 'unavailable' ? 'unavailable' : null,
      credits_remaining: w.credits_remaining ?? null, spend_used: w.spend_used ?? null, spend_limit: w.spend_limit ?? null, unit: w.unit ?? null, profile: w.profile ?? null };
  }
  if (raw.observed_at !== null && (typeof raw.observed_at !== 'string' || !Number.isFinite(Date.parse(raw.observed_at)))) return null;
  return { status: raw.status, observed_at: raw.observed_at, details, windows, collector_version: raw.collector_version ?? null };
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
    const launchColumns = this.sql.exec('PRAGMA table_info(fleet_launches)').toArray().map((c: any) => c.name);
    for (const name of ['verification_status', 'exit_reason', 'finished_at']) {
      if (!launchColumns.includes(name)) this.sql.exec(`ALTER TABLE fleet_launches ADD COLUMN ${name} TEXT`);
    }
    this.sql.exec("CREATE INDEX IF NOT EXISTS fleet_offered ON fleet_launches(id) WHERE state = 'offered'");
    const columns = this.sql.exec('PRAGMA table_info(fleet_sessions)').toArray().map((c: any) => c.name);
    if (!columns.includes('last_activity_at')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN last_activity_at TEXT');
    if (!columns.includes('cwd')) this.sql.exec('ALTER TABLE fleet_sessions ADD COLUMN cwd TEXT');
  }

  private observationProvenance(account: any, route: any): { quota: any; subscriptionObservedAt: string | null } | null {
    const subscription: any = this.sql.exec('SELECT * FROM fleet_subscriptions WHERE machine_id = ? AND account = ?', route.machine_id, account.label).toArray()[0];
    if (subscription) {
      if (subscription.service !== account.provider || !fresh(subscription.observed_at, this.now(), 1200000)) return null;
      const quota = parsed(subscription.quota_json);
      return fresh(quota.observed_at, this.now(), 1200000) ? { quota, subscriptionObservedAt: subscription.observed_at } : null;
    }
    // A default CLI reading cannot describe a named profile or two accounts on one service.
    if (route.profile !== null) return null;
    const count = Number(this.sql.exec('SELECT COUNT(DISTINCT r.account) AS n FROM fleet_routes r JOIN fleet_accounts a ON r.account = a.label WHERE r.machine_id = ? AND a.provider = ?', route.machine_id, account.provider).one().n);
    const snapshot: any = this.sql.exec('SELECT * FROM host_quota_snapshots WHERE host_id = ?', route.machine_id).toArray()[0];
    if (count !== 1 || !snapshot || !fresh(snapshot.captured_at, this.now(), 1200000)) return null;
    const quota = parsed(snapshot.routes_json)[account.provider];
    if (!quota || !fresh(quota.observed_at, this.now(), 1200000)) return null;
    return { quota: snapshot.status === 'ok' ? quota : { ...quota, status: 'unknown' }, subscriptionObservedAt: snapshot.captured_at };
  }

  private observation(account: any, route: any) {
    return this.observationProvenance(account, route)?.quota ?? null;
  }

  accountStatus(account: any) {
    this.admission.reconcileOffers();
    const routes = this.sql.exec('SELECT machine_id, profile FROM fleet_routes WHERE account = ?', account.label).toArray();
    let remaining: number | null = null; let observed = 0; let unknown = false; let blocked = !account.enabled;
    const reserves = (((policy as any).rules as any).reserve_percent || {}) as Record<string, number>;
    for (const route of routes) {
      const prov = this.observationProvenance(account, route);
      if (!prov) continue;
      observed++;
      const adm = evaluateQuotaAdmission({ provider: account.provider, quota: prov.quota, subscriptionObservedAt: prov.subscriptionObservedAt, nowMs: this.now(), reserves });
      if (!adm.ok) {
        if (adm.reason === 'limit_reached') blocked = true;
        else if (adm.reason === 'reserve') {
          if (adm.remaining !== null) remaining = remaining === null ? adm.remaining : Math.min(remaining, adm.remaining);
        } else unknown = true;
        continue;
      }
      remaining = remaining === null ? adm.remaining! : Math.min(remaining, adm.remaining!);
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
    if (!object(input, ['mode', 'machine', 'engine', 'tier', 'account', 'workspace', 'cwd', 'prompt', 'idempotency_key', 'parent_id', 'summary', 'access', 'approved_action']) ||
      !label(input.workspace) || (input.mode !== undefined && !['headless', 'interactive'].includes(input.mode)) ||
      (input.account !== undefined && !label(input.account)) || (input.machine !== undefined && !label(input.machine)) || (input.engine !== undefined && !label(input.engine)) ||
      // Optional tier/cwd arrive as null (not just undefined) from the
      // session-control reserve handoff when the client omitted them; null
      // means absent, never a malformed value.
      (input.tier !== undefined && input.tier !== null && (typeof input.tier !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(input.tier))) ||
      (input.cwd !== undefined && input.cwd !== null && !relativeCwdOk(input.cwd)) ||
      (input.mode === 'interactive' && !input.machine) ||
      (input.parent_id !== undefined && input.parent_id !== null && (typeof input.parent_id !== 'string' || !/^[a-f0-9-]{36}$/.test(input.parent_id))) ||
      (input.summary !== undefined && (typeof input.summary !== 'string' || input.summary.length > 240 || /[\x00-\x1f\x7f]/.test(input.summary))) ||
      (input.prompt !== undefined && (typeof input.prompt !== 'string' || input.prompt.length > 16000)) ||
      (input.approved_action !== undefined && (typeof input.approved_action !== 'string' || !input.approved_action.trim() || input.approved_action.length > 12000)) ||
      (input.mode !== 'interactive' && !input.prompt?.trim())) return { error: 'invalid_launch_request' };
    // Tier resolves through the EXISTING model-routing table (aliases +
    // tier membership), never new ranking: candidates are filtered to routes
    // whose (engine, profile) pair is a configured member of the tier, while
    // the existing score order still decides placement. An explicit engine
    // outside the tier simply matches nothing (honest no_eligible_route).
    let tierPairs: Set<string> | null = null;
    if (input.tier !== undefined) {
      const tier = taskTier({ tier: input.tier });
      if (!tier) return { error: 'invalid_launch_request' };
      const members: string[] = (policy.tiers as any)[tier] || [];
      tierPairs = new Set(
        policy.models
          .filter(m => members.includes(m.id))
          .map(m => m.engine + '|' + (m.profile ?? '')),
      );
    }
    const request: any = { mode: input.mode || 'headless', machine: input.machine || null, engine: input.engine || null, workspace: input.workspace, cwd: input.cwd ?? null, prompt: input.prompt || '', parent_id: input.parent_id || null, summary: input.summary || (input.mode === 'interactive' ? 'Interactive session' : 'Agent task') };
    if (input.account !== undefined) request.account = input.account;
    if (input.approved_action !== undefined) request.approved_action = input.approved_action;
    if (input.tier !== undefined) request.tier = input.tier;
    const rejected: { machine: string; engine: string; reason: string }[] = []; const candidates: any[] = [];
    const accounts = new Map(this.sql.exec('SELECT * FROM fleet_accounts').toArray().map(a => [a.label, this.accountStatus(a)]));
    const definitions = new Map(this.sql.exec('SELECT * FROM fleet_accounts').toArray().map(a => [a.label, a]));
    for (const route of this.sql.exec('SELECT * FROM fleet_routes ORDER BY machine_id, engine').toArray() as any[]) {
      if ((request.account && route.account !== request.account) || (request.machine && route.machine_id !== request.machine) || (request.engine && route.engine !== request.engine)) continue;
      if (tierPairs && !tierPairs.has(route.engine + '|' + (route.profile ?? ''))) continue;
      // A disabled execution engine holds all new candidate planning, whatever the account label.
      if (disabledEngines().has(route.engine)) { rejected.push({ machine: route.machine_id, engine: route.engine, reason: 'provider_disabled' }); continue; }
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
      this.admission.reconcileOffers();
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
      if (prior) return sameIntent(prior.request_json, canonical) && sameEditCaller(prior.request_json, caller) ? result({ id: prior.id, state: prior.state, machine: prior.machine_id, engine: prior.engine, account: prior.account, mode: prior.mode, access: JSON.parse(prior.request_json).access?.mode || 'legacy_unspecified', replay: true }) : result({ error: 'idempotency_conflict' }, 409);
      // A disabled execution engine refuses every new reservation, after valid replay.
      if (typeof plan.request.engine === 'string' && plan.request.engine && disabledEngines().has(plan.request.engine)) {
        return result({ error: 'provider_disabled', access: parsed.access.mode }, 409);
      }
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
        this.admission.reconcileOffers();
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
      return this.transaction(() => {
        this.admission.reconcileOffers();
        const offers: any[] = [];
        for (const row of this.sql.exec("SELECT * FROM fleet_launches WHERE machine_id = ? AND state = 'offered' ORDER BY created_at, id LIMIT 100", caller.machineId!)) {
          if (this.admission.retireInvalidOffer(row)) continue;
          offers.push({ id: row.id, engine: row.engine, account: row.account, profile: row.profile, mode: row.mode, workspace: row.workspace, cwd: parsed(row.request_json).cwd ?? null, prompt: row.prompt, access: parsed(row.request_json).access?.mode || 'legacy_unspecified' });
          if (offers.length === 20) break;
        }
        return response({ offers: this.routing.enrichOffers(offers) });
      });
    }
    if (path === '/api/fleet/sessions' && request.method === 'PUT') {
      if (!host) return response({ error: 'forbidden' }, 403);
      const data = await body(request);
      if (!object(data, ['sessions']) || !Array.isArray(data.sessions) || data.sessions.length > 200) return response({ error: 'invalid_sessions' }, 400);
      const ids = new Set();
      for (const s of data.sessions) {
        if (!object(s, ['native_id', 'tag', 'engine', 'phase', 'agent_state', 'workspace', 'cwd', 'profile', 'account', 'last_activity_at']) || (s.last_activity_at !== undefined && s.last_activity_at !== null && (typeof s.last_activity_at !== 'string' || !fresh(s.last_activity_at, this.now() + 60000, 366 * 86400000))) || typeof s.native_id !== 'string' || !/^[a-zA-Z0-9-]{1,160}$/.test(s.native_id) || typeof s.tag !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$/.test(s.tag) || (s.engine !== null && !label(s.engine)) || !label(s.phase) || (s.agent_state !== null && !label(s.agent_state)) || typeof s.workspace !== 'string' || s.workspace.length > 2048 || /[\x00-\x1f\x7f]/.test(s.workspace) || (s.cwd !== undefined && s.cwd !== null && (typeof s.cwd !== 'string' || s.cwd.length > 2048 || /[\x00-\x1f\x7f]/.test(s.cwd))) || (s.profile !== null && !label(s.profile)) || (s.account !== null && !label(s.account)) || ids.has(s.native_id)) return response({ error: 'invalid_session' }, 400);
        ids.add(s.native_id);
      }
      return this.transaction(() => {
        // Write only rows that changed: hosts report every minute and Durable Object row writes
        // are metered. observed_at therefore means "last changed"; host freshness comes from heartbeats.
        const fields = ['tag', 'engine', 'phase', 'agent_state', 'workspace', 'cwd', 'profile', 'account', 'last_activity_at'];
        const existing = new Map((this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id = ?', caller.machineId!).toArray() as any[]).map(r => [r.native_id, r]));
        let written = 0;
        for (const s of data.sessions) {
          const prior = existing.get(s.native_id); existing.delete(s.native_id);
          const row = { ...s, last_activity_at: s.last_activity_at ?? null, cwd: s.cwd ?? null };
          if (prior && fields.every(f => (prior[f] ?? null) === (row[f] ?? null))) continue;
          this.sql.exec('INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, profile, account, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', caller.machineId!, s.native_id, s.tag, s.engine, s.phase, s.agent_state, new Date(this.now()).toISOString(), s.workspace, row.cwd, s.profile, s.account, row.last_activity_at);
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
        if (!mapped.length || !quota || (quota.observed_at !== null && Date.parse(quota.observed_at) > this.now()) || (quota.status === 'ok' && !fresh(quota.observed_at, this.now(), 1200000))) return response({ error: 'subscription_unmapped_or_invalid', reason: !mapped.length ? 'unmapped' : !quota ? 'invalid_quota' : 'invalid_timestamp' }, 400);
        clean.push({ ...sub, quota });
      }
      return this.transaction(() => {
        const prior = new Map((this.sql.exec('SELECT account, quota_json FROM fleet_subscriptions WHERE machine_id = ?', caller.machineId!).toArray() as any[]).map(r => [r.account, r.quota_json]));
        this.sql.exec('DELETE FROM fleet_subscriptions WHERE machine_id = ?', caller.machineId!);
        for (const s of clean) {
          // A newer report that omits a cooldown deadline must not erase an unexpired known hold.
          if (s.quota.details?.cooldown_until == null) {
            try {
              const oldQ = JSON.parse(prior.get(s.account) || 'null');
              const oldCd = oldQ?.details?.cooldown_until;
              if (typeof oldCd === 'string' && Number.isFinite(Date.parse(oldCd)) && Date.parse(oldCd) > this.now()) {
                s.quota.details.cooldown_until = oldCd;
              }
            } catch { /* keep the field absent */ }
          }
          this.sql.exec('INSERT INTO fleet_subscriptions VALUES (?, ?, ?, ?, ?, ?)', caller.machineId!, s.account, s.service, s.plan, new Date(this.now()).toISOString(), JSON.stringify(s.quota));
        }
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
          // Pre-hold offers on a now-disabled engine are held, never launched.
          if (disabledEngines().has(row.engine)) return response({ error: 'provider_disabled' }, 409);
          const held = this.admission.retireInvalidOffer(row);
          if (held) return response({ error: held }, ['claim_inactive', 'claim_conflict', 'claim_authority_changed'].includes(held) ? 409 : 403);
          if (!object(data, ['gate_allowed', 'observed_at', 'account', 'profile_supported']) || data.gate_allowed !== true || data.profile_supported !== true || data.account !== row.account || !fresh(data.observed_at, this.now(), 30000)) return response({ error: 'fresh_admission_required' }, 409);
          const account: any = this.sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', row.account).toArray()[0];
          if (!account || this.accountStatus(account).status !== 'ok' || !this.observation(account, row)) return response({ error: 'account_unavailable' }, 409);
          // Re-verify the exact launch route quota inside the atomic claim: no manual override bypass.
          const claimRoute: any = this.sql.exec('SELECT machine_id, profile FROM fleet_routes WHERE machine_id = ? AND account = ?', row.machine_id, row.account).toArray()[0];
          const claimProv = claimRoute ? this.observationProvenance(account, claimRoute) : null;
          const claimAdm = claimProv ? evaluateQuotaAdmission({ provider: account.provider, quota: claimProv.quota, subscriptionObservedAt: claimProv.subscriptionObservedAt, nowMs: this.now(), reserves: ((((policy as any).rules as any).reserve_percent || {}) as Record<string, number>) }) : null;
          if (!claimAdm || !claimAdm.ok) return response({ error: 'quota_unavailable', reason: claimAdm ? claimAdm.reason : 'no_quota_evidence' }, 409);
          // Capacity reads reconcile other offers and time may advance during synchronous checks.
          // Never revive a row retired by that sweep; check authority again at the final write.
          const current: any = this.sql.exec('SELECT * FROM fleet_launches WHERE id = ?', row.id).one();
          const finalHold = current.state === 'offered' ? this.admission.retireInvalidOffer(current) : current.exit_reason || 'already_claimed';
          if (finalHold) return response({ error: finalHold }, ['claim_inactive', 'claim_conflict', 'claim_authority_changed', 'already_claimed'].includes(finalHold) ? 409 : 403);
          this.sql.exec("UPDATE fleet_launches SET state = 'starting', updated_at = ? WHERE id = ? AND state = 'offered'", new Date(this.now()).toISOString(), row.id);
          return response({ ...row, state: 'starting' });
        }
        if (!object(data, ['state', 'native_id', 'launcher_task_id', 'confirmed_exit']) || !['running', 'uncertain', 'failed', 'exited'].includes(data.state) || !['starting', 'running', 'uncertain'].includes(row.state)) return response({ error: 'invalid_transition' }, 409);
        if (['exited', 'failed'].includes(data.state) && this.sql.exec('SELECT id FROM routing_jobs WHERE launch_id = ?', row.id).toArray().length) return response({ error: 'task_report_required' }, 409);
        if (['exited', 'failed'].includes(data.state) && data.confirmed_exit !== true) return response({ error: 'confirmed_exit_required' }, 409);
        if (data.state === 'running' && (typeof data.native_id !== 'string' || data.native_id.length < 1 || data.native_id.length > 160 || (row.mode === 'headless' && !label(data.launcher_task_id)))) return response({ error: 'native_receipt_required' }, 409);
        if ((data.native_id !== undefined && (typeof data.native_id !== 'string' || data.native_id.length > 160)) || (data.launcher_task_id !== undefined && !label(data.launcher_task_id))) return response({ error: 'invalid_receipt' }, 400);
        this.sql.exec('UPDATE fleet_launches SET state = ?, native_id = COALESCE(?, native_id), launcher_task_id = COALESCE(?, launcher_task_id), updated_at = ? WHERE id = ?', data.state, data.native_id || null, data.launcher_task_id || null, new Date(this.now()).toISOString(), row.id);
        // A confirmed terminal host result must also close its canonical Hub
        // session. Otherwise intake keeps an active assignment forever after
        // a failed start, preventing recovery despite released Fleet capacity.
        // Fleet can run without the optional session-control tables in tests.
        if (['failed', 'exited'].includes(data.state) && this.sql.exec("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'session_control_sessions'").toArray().length) {
          this.sql.exec("UPDATE session_control_sessions SET state = ?, updated_at = ? WHERE fleet_launch_id = ? AND machine_id = ? AND state IN ('queued', 'held', 'offered', 'starting', 'running', 'uncertain')", data.state === 'failed' ? 'failed' : 'stopped', new Date(this.now()).toISOString(), row.id, caller.machineId!);
        }
        return response({ id: row.id, state: data.state });
      });
    }
    return response({ error: 'not_found' }, 404);
  }
}
