/** Centrally managed engine bundles. Secrets are encrypted at rest and never returned to UI/list callers. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { Env } from './env';
import { validSessionSecret } from './auth/session';
import type { FleetService } from './fleet';
import { GOAL_PROVIDERS } from './goal-engines';
import { canonicalQuotaProvider, canonicalAccountLabel, validateQuotaSnapshotPayload } from './quota';
import { syncTbmFromServerAccounts } from './tbm-ingestion';
import { effectiveCheckin } from './registry';
import { bumpEngineEpoch } from './engine-epoch';
import { CLAUDE_OAUTH, CREDENTIAL_PATHS, CREDENTIAL_SCHEMA, accessExpiry, claudeAuthorization, claudeExchange, credentialFreshness, credentialSubset,
  fetchQuota, needsRefresh, refreshCredential, refreshable, structurallyValid, validCredentialFiles, type CredentialFiles, type FetchLike } from './credentials';

const LABEL = /^[a-z][a-z0-9_-]{0,47}$/;
// Hosts check in about every minute (persisted at least every 5 min); silence
// beyond this reads as offline, and the host reconciles on reconnect.
const SYNC_STALE_MS = 10 * 60000;
const MIGRATION_GRACE_MS = 2 * 3600000;
// A Hub-central login waits this long for the pasted code (intake 8ff6b3d8).
export const CREDENTIAL_LOGIN_TTL_MS = 30 * 60000;
// One maintenance pass per isolate: overlapping cron/manual runs would refresh
// the same single-use token twice and raise a false rejection.
let maintaining = false;
export const PACKAGES: Record<string, { pkg: string; bin: string; engine: string }> = {
  codex: { pkg: '@openai/codex', bin: 'codex', engine: 'codex' },
  claude: { pkg: '@anthropic-ai/claude-code', bin: 'claude', engine: 'claude' },
  gemini: { pkg: '@google/gemini-cli', bin: 'gemini', engine: 'gemini' },
  go: { pkg: 'opencode-ai', bin: 'opencode', engine: 'opencode' },
  zai: { pkg: 'zcodex', bin: 'zcodex', engine: 'codex' },
  // Antigravity is a native binary: hosts install the release pinned in
  // host/agent_hub_host/bootstrap_pins.py (sha512-checked), never npm (AC8).
  antigravity: { pkg: 'antigravity-cli', bin: 'agy', engine: 'antigravity' },
};
// The Antigravity harness spends the Google (Gemini) quota pool.
export const accountProvider = (provider: string) => provider === 'antigravity' ? 'gemini' : provider;
// Provider default slot limit for a Hub-central account created by import or
// central login (t1248u). An explicit `max_concurrency` on create wins.
export const PROVIDER_DEFAULT_MAX_CONCURRENCY: Record<string, number> = { codex: 8, claude: 8, gemini: 8, go: 8, zai: 8, antigravity: 8 };
export const providerDefaultMaxConcurrency = (provider: string) => PROVIDER_DEFAULT_MAX_CONCURRENCY[provider] ?? 8;
const IMPLICIT_MAX_MIGRATION = 'managed_account_max_concurrency_default_v1';
const explicitMax = (v: unknown) => v === undefined || v === null ? undefined : Number.isInteger(v) && (v as number) >= 1 && (v as number) <= 100 ? v as number : NaN;
const ENV = new Set(['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENCODE_API_KEY', 'ZAI_API_KEY', 'ZAI_BASE_URL', 'ZAI_TOKEN']);
const headers = { 'Cache-Control': 'private, no-store', 'Pragma': 'no-cache' };
const reply = (data: unknown, status = 200) => Response.json(data, { status, headers });
const object = (v: any) => v && typeof v === 'object' && !Array.isArray(v);
export function validBundle(v: any): boolean {
  if (!object(v) || Object.keys(v).some(k => !['label', 'provider', 'version', 'files', 'env', 'args', 'auth_method'].includes(k)) || !LABEL.test(v.label) || !Object.hasOwn(PACKAGES, v.provider) || typeof v.version !== 'string' || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(v.version)) return false;
  if (v.provider === 'gemini' && (Object.keys(v.env || {}).length || (v.auth_method === 'machine-login'
    ? Object.keys(v.files || {}).some(k => /oauth|credential|token/i.test(k))
    : v.auth_method !== undefined || typeof v.files?.['.gemini/oauth_creds.json'] !== 'string'))) return false;
  if (!object(v.files) || Object.keys(v.files).length > 24 || !object(v.env) || Object.keys(v.env).length > 12) return false;
  // OpenCode: no env secrets; the only file is the Hub-held device-login export.
  if (v.provider === 'go' && (Object.keys(v.env).length || (Object.keys(v.files).length && JSON.stringify(Object.keys(v.files)) !== JSON.stringify(CREDENTIAL_PATHS.go)))) return false;
  // Antigravity: exactly the Hub-held OAuth token file, no env secrets.
  if (v.provider === 'antigravity' && (Object.keys(v.env).length || v.auth_method !== undefined ||
    JSON.stringify(Object.keys(v.files)) !== JSON.stringify(CREDENTIAL_PATHS.antigravity))) return false;
  for (const [path, value] of Object.entries(v.files)) {
    if (path.length > 200 || !path.split('/').every(p => /^[A-Za-z0-9_.-]+$/.test(p) && p !== '.' && p !== '..') || typeof value !== 'string' || value.length > 16000) return false;
  }
  if (Object.entries(v.env).some(([k, val]) => !ENV.has(k) || typeof val !== 'string' || val.length > 8000 || val.includes('\0'))) return false;
  return Array.isArray(v.args) && v.args.length <= 32 && v.args.every((x: any) => typeof x === 'string' && x.length < 1024 && !x.includes('\0')) &&
    // OpenCode is exclusively the subscribed Muse route; GLM belongs to zcodex.
    (v.provider !== 'go' || [JSON.stringify(['--model', 'opencode-go/muse-spark-1.3-contributor']), JSON.stringify(['--model', 'opencode-go/muse-spark-1.3-contributor', '--version'])].includes(JSON.stringify(v.args))) &&
    // Z.ai routes through native zcodex and never routes OpenCode Muse models.
    (v.provider !== 'zai' || !v.args.some((a: string) => a.includes('opencode-go') || a.includes('muse')));
}

export class ProvisioningService {
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private env: Env, private fleet: FleetService,
    private hostAllowed: (machine: string, caller: any) => boolean) {}
  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_bundles (label TEXT PRIMARY KEY, provider TEXT NOT NULL, version TEXT NOT NULL, revision TEXT NOT NULL, ciphertext TEXT NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_logins (id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL, url TEXT, code_ciphertext TEXT, expires_at INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_installations (machine_id TEXT NOT NULL, label TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(machine_id,label))');
    // Per-host sync visibility (AC9): what each host last reported per account.
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_sync_status (machine_id TEXT NOT NULL, label TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL, error TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_sync_hosts (machine_id TEXT PRIMARY KEY, state TEXT NOT NULL, last_sync_at INTEGER NOT NULL)');
    for (const ddl of CREDENTIAL_SCHEMA) this.sql.exec(ddl);
    // A7: the credential generation each host acknowledged installing, per account.
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_credential_acks (machine_id TEXT NOT NULL, label TEXT NOT NULL, generation INTEGER NOT NULL, acked_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL, detail TEXT NOT NULL)');
    this.migrateImplicitMaxConcurrency();
    this.initEngineRollout();
  }
  // ---- Hub-managed engine version rollout (intake 8cc9e31a) ----
  // Desired versions mirror host/agent_hub_host/bootstrap_pins.py ENGINE_PACKAGES
  // plus the reviewed Antigravity pin. zcodex tracks the reviewed codex CLI
  // version (release tag ``zcode-<runtime>-codex-<version>``). Update both
  // places in the same change.
  private rolloutDesired(): Record<string, string> {
    return { codex: '0.166.0', claude: '2.1.312', opencode: '1.19.5', agy: '1.4.10', zcodex: '0.166.0' };
  }
  private rolloutProvider(engine: string): string | null {
    return { codex: 'codex', claude: 'claude', opencode: 'go', agy: 'antigravity', zcodex: 'zai' }[engine] ?? null;
  }
  private initEngineRollout() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_rollout_desired (engine TEXT PRIMARY KEY, version TEXT NOT NULL, latest TEXT, source TEXT NOT NULL, updated_at INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_version_reports (machine_id TEXT NOT NULL, engine TEXT NOT NULL, installed TEXT, latest TEXT, result TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY(machine_id,engine))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS engine_rollouts (id TEXT PRIMARY KEY, engine TEXT NOT NULL, target_version TEXT NOT NULL, previous_json TEXT NOT NULL, canary_machine TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, detail TEXT NOT NULL)');
    const desired = this.rolloutDesired();
    for (const [engine, version] of Object.entries(desired)) {
      const row: any = this.sql.exec('SELECT version FROM engine_rollout_desired WHERE engine=?', engine).toArray()[0];
      if (!row) this.sql.exec('INSERT INTO engine_rollout_desired VALUES (?,?,?,?,?)', engine, version, version, 'bootstrap_pins', this.now());
      else if (row.version !== version) this.sql.exec('UPDATE engine_rollout_desired SET version=?, source=?, updated_at=? WHERE engine=?', version, 'bootstrap_pins', this.now(), engine);
    }
  }
  /**
   * Managed accounts used to be created at an implicit max_concurrency of 1.
   * Once, raise those still at 1 to the provider default and record the
   * labels so the change is visible (`GET /api/engines` account_migrations).
   * Unmanaged registry rows and any later explicit 1 are never touched.
   */
  private migrateImplicitMaxConcurrency() {
    if (this.sql.exec('SELECT 1 FROM engine_migrations WHERE name=?', IMPLICIT_MAX_MIGRATION).toArray().length) return;
    this.tx(() => {
      const rows = this.sql.exec('SELECT a.label, b.provider FROM fleet_accounts a JOIN engine_bundles b ON b.label=a.label WHERE a.max_concurrency=1 ORDER BY a.label').toArray() as any[];
      const migrated = rows.map(r => ({ label: r.label, from: 1, to: providerDefaultMaxConcurrency(r.provider) }));
      for (const m of migrated) this.sql.exec('UPDATE fleet_accounts SET max_concurrency=? WHERE label=? AND max_concurrency=1', m.to, m.label);
      this.sql.exec('INSERT INTO engine_migrations VALUES (?,?,?)', IMPLICIT_MAX_MIGRATION, this.now(), JSON.stringify({ migrated }));
    });
  }
  private now() { return Date.now(); }
  /**
   * One row per registered host and managed account: `synced` (current revision
   * installed), `failed` (host reported an install error for it), `pending`
   * (not yet acknowledged). A host silent for SYNC_STALE_MS is `offline`, and it
   * reconciles on reconnect.
   */
  syncStatus() {
    const now = this.now();
    const engines = this.sql.exec('SELECT label, provider, revision FROM engine_bundles ORDER BY label').toArray() as any[];
    const machines = this.sql.exec('SELECT id, name, last_checkin_at FROM machines WHERE registered_at IS NOT NULL AND revoked_at IS NULL ORDER BY id').toArray() as any[];
    const hosts = new Map((this.sql.exec('SELECT * FROM engine_sync_hosts').toArray() as any[]).map(h => [h.machine_id, h]));
    const installed = new Set((this.sql.exec('SELECT machine_id,label,revision FROM engine_installations').toArray() as any[]).map(i => i.machine_id + '|' + i.label + '|' + i.revision));
    const status = new Map((this.sql.exec('SELECT * FROM engine_sync_status').toArray() as any[]).map(r => [r.machine_id + '|' + r.label, r]));
    const acks = new Map((this.sql.exec('SELECT machine_id,label,generation,acked_at FROM engine_credential_acks').toArray() as any[]).map(r => [r.machine_id + '|' + r.label, r]));
    const current = new Map((this.sql.exec('SELECT label,generation FROM engine_credentials').toArray() as any[]).map(r => [r.label, r.generation]));
    return machines.map(m => {
      const h: any = hosts.get(m.id);
      // Liveness comes from the machine check-in, so sync reports need only be sent on change.
      const seen = Date.parse(effectiveCheckin(m.id, m.last_checkin_at) || '');
      const online = Number.isFinite(seen) && now - seen <= SYNC_STALE_MS;
      return { machine_id: m.id, machine: m.name, last_sync_at: h?.last_sync_at ?? null, host_state: h?.state ?? 'never', online,
        accounts: engines.map(e => {
          const r: any = status.get(m.id + '|' + e.label);
          const state = installed.has(m.id + '|' + e.label + '|' + e.revision) ? 'synced' : r?.revision === e.revision && r.state === 'failed' ? 'failed' : 'pending';
          const ack: any = acks.get(m.id + '|' + e.label);
          return { label: e.label, provider: e.provider, state, ...(state === 'failed' && r.error ? { error: r.error } : {}),
            ...(current.has(e.label) ? { credential_generation: current.get(e.label), acked_generation: ack?.generation ?? null, acked_at: ack?.acked_at ?? null } : {}) };
        }) };
    });
  }
  private credentialRow(label: string): any { return this.sql.exec('SELECT * FROM engine_credentials WHERE label=?', label).toArray()[0]; }
  private async sealCredential(label: string, generation: number, files: CredentialFiles) {
    return this.seal({ label, files }, 'credential:' + generation);
  }
  private async openCredential(row: any): Promise<CredentialFiles> {
    return (await this.open({ label: row.label, revision: 'credential:' + row.generation, ciphertext: row.ciphertext })).files;
  }
  /** Publish a new credential generation (inside the caller's transaction). Clears host rejections of older generations. */
  private writeCredential(label: string, provider: string, generation: number, ciphertext: string, files: CredentialFiles, by: string) {
    this.sql.exec('INSERT OR REPLACE INTO engine_credentials VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', label, provider, generation, ciphertext,
      credentialFreshness(provider, files), accessExpiry(provider, files), 'ok', null, by, this.now(), null,
      this.credentialRow(label)?.quota_attempt_at ?? null);
    this.sql.exec('DELETE FROM engine_credential_rejections WHERE label=? AND generation<?', label, generation);
    bumpEngineEpoch(this.sql, this.now());
  }
  /** Non-secret credential metadata for operators. */
  credentialStatus() {
    return this.sql.exec('SELECT label,provider,generation,freshness_ms,access_expires_ms,status,status_reason,updated_by,updated_at FROM engine_credentials ORDER BY label').toArray();
  }
  private async key() {
    if (!validSessionSecret(this.env)) throw new Error('provisioning_key_unavailable');
    const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(this.env.SESSION_SECRET), 'HKDF', false, ['deriveKey']);
    return crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: new TextEncoder().encode('hub-engine-bundles-v1'), info: new TextEncoder().encode('credentials-at-rest') }, material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  }
  private async seal(value: any, revision: string) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const data = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(value.label + ':' + revision) }, await this.key(), new TextEncoder().encode(JSON.stringify(value)));
    return JSON.stringify({ iv: Array.from(iv), data: Array.from(new Uint8Array(data)) });
  }
  private async open(row: any) {
    const c = JSON.parse(row.ciphertext);
    const data = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: new Uint8Array(c.iv), additionalData: new TextEncoder().encode(row.label + ':' + row.revision) }, await this.key(), new Uint8Array(c.data));
    const account: any = this.sql.exec('SELECT provider FROM fleet_accounts WHERE label=?', row.label).toArray()[0];
    const bundle = JSON.parse(new TextDecoder().decode(data));
    const cred: any = row.revision.startsWith('credential:') ? null : this.credentialRow(row.label);
    if (cred && bundle.files) bundle.files = { ...bundle.files, ...await this.openCredential(cred) };
    return { ...bundle, revision: row.revision, quota_provider: account?.provider ?? row.provider,
      ...(cred ? { credential_generation: cred.generation, credential_paths: CREDENTIAL_PATHS[row.provider] ?? [], credential_status: cred.status } : {}) };
  }
  async handle(request: Request, auth: any): Promise<Response> {
    if (auth.error) return auth.error;
    if (!auth.caller) return reply({ error: 'unauthorized' }, 401);
    this.sql.exec('DELETE FROM engine_logins WHERE expires_at <= ?', Date.now());
    const path = new URL(request.url).pathname;
    const admin = auth.caller.isAdmin === true;
    const host = auth.caller.kind === 'host' && auth.scopes?.includes('host') && auth.caller.machineId && this.hostAllowed(auth.caller.machineId, auth.caller);
    const rows = () => this.sql.exec('SELECT * FROM engine_bundles ORDER BY label').toArray() as any[];
    if (path === '/api/engines' && request.method === 'GET') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      return reply({ logins: this.sql.exec('SELECT id,machine_id,label,status,url,expires_at FROM engine_logins WHERE expires_at > ?', Date.now()).toArray(), engines: rows().map(({ ciphertext, ...meta }) => ({ ...meta, supports_goal: GOAL_PROVIDERS.has(meta.provider) })), installations: this.sql.exec('SELECT * FROM engine_installations ORDER BY machine_id,label').toArray(),
        sync_status: this.syncStatus(),
        credentials: this.credentialStatus(), credential_rejections: this.sql.exec('SELECT machine_id,label,generation,reason,observed_at FROM engine_credential_rejections ORDER BY label,machine_id').toArray(),
        account_migrations: this.sql.exec('SELECT name,applied_at,detail FROM engine_migrations ORDER BY name').toArray().map((m: any) => ({ name: m.name, applied_at: m.applied_at, ...JSON.parse(m.detail) })) });
    }
    if (path === '/api/engines/sync' && request.method === 'GET') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      try { const engines = await Promise.all(rows().map(r => this.open(r)));
        if (!this.hostAllowed(auth.caller.machineId, auth.caller)) return reply({ error: 'forbidden' }, 403);
        for (const e of engines) if (this.grantRefreshLease(e, auth.caller.machineId)) e.refresh_lease = true;
        return reply({ engines, logins: this.sql.exec("SELECT id,label,status FROM engine_logins WHERE machine_id=? AND expires_at>? AND status IN ('requested','waiting','submitted')", auth.caller.machineId, Date.now()).toArray() }); }
      catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    }
    if (path === '/api/engines/admission' && request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      if (!data || !LABEL.test(data.label)) return reply({ error: 'invalid_label' }, 400);
      const a = this.sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', data.label).toArray()[0];
      const installed = this.sql.exec('SELECT 1 FROM engine_installations i JOIN engine_bundles b ON b.label=i.label AND b.revision=i.revision WHERE i.machine_id=? AND i.label=?', auth.caller.machineId, data.label).toArray().length;
      const quotaStatus = a ? this.fleet.accountStatus(a).status : 'unmapped';
      const cred = this.credentialRow(data.label);
      const rejected = cred && this.sql.exec('SELECT 1 FROM engine_credential_rejections WHERE machine_id=? AND label=? AND generation=?', auth.caller.machineId, data.label, cred.generation).toArray().length;
      // Hub-held quota and credential state decide admission; the host runs no quota probe.
      // Native (host-home) accounts have no managed profile; Hub still owns their quota verdict.
      const native = data.native === true;
      const reason = !installed && !native ? 'profile_not_installed' : !a ? 'account_unmapped' : cred?.status === 'invalid' ? 'credential_invalid'
        : rejected ? 'credential_rejected' : quotaStatus === 'blocked' ? 'quota_exhausted' : null;
      return reply({ launch_allowed: !reason, profile_supported: !!installed || (native && !!a), ...(reason ? { reason } : {}),
        ...(quotaStatus === 'unknown' ? { quota_warning: 'quota_probe_unavailable' } : {}),
        ...(cred ? { credential_generation: cred.generation } : {}),
        // Compatibility with installed hosts whose clocks lag Hub slightly. Backdating
        // consumes freshness budget; it never extends the 30-second stale cutoff.
        account_label: data.label, observed_at: new Date(Date.now() - 5_000).toISOString(),
        server_observed_at: new Date().toISOString() });
    }
    if (path === '/api/engines/logins' && request.method === 'POST') {
      if (!admin) return reply({error:'forbidden'},403);
      const data = await request.json().catch(() => null) as any;
      if (!data || !LABEL.test(data.label) || typeof data.machine !== 'string' || !this.sql.exec('SELECT 1 FROM engine_installations i JOIN engine_bundles b ON b.label=i.label AND b.revision=i.revision WHERE i.label=? AND i.machine_id=? AND b.provider=?',data.label,data.machine,'gemini').toArray().length) return reply({error:'invalid_login_target'},400);
      const prior: any = this.sql.exec("SELECT * FROM engine_logins WHERE label=? AND machine_id=? AND expires_at>? AND status IN ('requested','waiting','submitted')",data.label,data.machine,Date.now()).toArray()[0];
      if (prior) return reply({id:prior.id,status:prior.status});
      if (Number(this.sql.exec('SELECT COUNT(*) AS n FROM engine_logins').one().n) >= 128) return reply({error:'login_capacity'},409);
      const id = crypto.randomUUID();
      this.sql.exec("INSERT INTO engine_logins VALUES (?,?,?,'requested',NULL,NULL,?)",id,data.machine,data.label,Date.now()+600000);
      return reply({id,status:'requested'});
    }
    const loginMatch = /^\/api\/engines\/logins\/([a-f0-9-]{36})$/.exec(path);
    if (loginMatch) {
      const row: any = this.sql.exec('SELECT * FROM engine_logins WHERE id=?',loginMatch[1]).toArray()[0];
      if (!row) return reply({error:'not_found'},404);
      if (!(admin || (host && auth.caller.machineId === row.machine_id))) return reply({error:'forbidden'},403);
      if (row.expires_at <= Date.now()) return reply({status:'expired'});
      if (request.method === 'GET') {
        let code;
        if (host && row.code_ciphertext) {
          const opened = await this.open({label:row.label,revision:row.id,ciphertext:row.code_ciphertext});
          if (!this.hostAllowed(auth.caller.machineId,auth.caller)) return reply({error:'forbidden'},403);
          code = opened.code;
        }
        return reply({id:row.id,status:row.status,url:row.url,...(code ? {code} : {})});
      }
      const data = await request.json().catch(() => null) as any;
      if (!data || request.method !== 'POST') return reply({error:'invalid_login_update'},400);
      if (admin && typeof data.code === 'string' && /^[A-Za-z0-9_\/.-]{1,2048}$/.test(data.code) && row.status === 'waiting') {
        const sealed = await this.seal({label:row.label,code:data.code},row.id);
        this.sql.exec("UPDATE engine_logins SET status='submitted',code_ciphertext=? WHERE id=?",sealed,row.id);
        return reply({status:'submitted'});
      }
      if (!host || auth.caller.machineId !== row.machine_id || !['waiting','authenticated','failed'].includes(data.status)) return reply({error:'forbidden'},403);
      let url: string | null = null;
      if (data.status === 'waiting') {
        try { const u = new URL(data.url); if (u.protocol !== 'https:' || u.hostname !== 'accounts.google.com' || u.username || data.url.length>4096) throw new Error(); url=data.url; }
        catch { return reply({error:'invalid_login_url'},400); }
      }
      this.sql.exec('UPDATE engine_logins SET status=?,url=?,code_ciphertext=CASE WHEN ? THEN NULL ELSE code_ciphertext END WHERE id=?',data.status,url,Number(['authenticated','failed'].includes(data.status)),row.id);
      return reply({status:data.status});
    }
    if (path === '/api/engines/maintain' && request.method === 'POST') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      return reply({ results: await this.maintainCredentials() });
    }
    const credentialLogin = await this.handleCredentialLogin(request, path, admin);
    if (credentialLogin) return credentialLogin;
    if (path === '/api/engines/sync-report' && request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      if (!object(data) || !['synced', 'held'].includes(data.state) || !Array.isArray(data.items) || data.items.length > 32) return reply({ error: 'invalid_report' }, 400);
      return this.tx(() => {
        const machine = auth.caller.machineId as string;
        if (!this.hostAllowed(machine, auth.caller)) return reply({ error: 'forbidden' }, 403);
        const now = this.now();
        for (const it of data.items) {
          if (!object(it) || !LABEL.test(it.label) || !['installed', 'failed'].includes(it.state)) continue;
          const row: any = this.sql.exec('SELECT revision FROM engine_bundles WHERE label=?', it.label).toArray()[0];
          if (!row || it.revision !== row.revision) continue;
          // Error is a bounded exception-type name, never provider output.
          const error = it.state === 'failed' && typeof it.error === 'string' && /^[A-Za-z0-9_]{1,64}$/.test(it.error) ? it.error : null;
          // Only failures are stored (synced comes from engine_installations), and only
          // on change: Free-plan Durable Object row writes are a daily budget.
          const prior: any = this.sql.exec('SELECT revision,state,error FROM engine_sync_status WHERE machine_id=? AND label=?', machine, it.label).toArray()[0];
          if (it.state === 'installed') {
            if (Number.isInteger(it.credential_generation) && it.credential_generation >= 0) {
              const ack: any = this.sql.exec('SELECT generation FROM engine_credential_acks WHERE machine_id=? AND label=?', machine, it.label).toArray()[0];
              if (!ack || ack.generation !== it.credential_generation) this.sql.exec('INSERT OR REPLACE INTO engine_credential_acks VALUES (?,?,?,?)', machine, it.label, it.credential_generation, now);
            }
            if (prior) this.sql.exec('DELETE FROM engine_sync_status WHERE machine_id=? AND label=?', machine, it.label);
            continue;
          }
          if (prior && prior.revision === row.revision && prior.state === it.state && (prior.error ?? null) === error) continue;
          this.sql.exec('INSERT OR REPLACE INTO engine_sync_status VALUES (?,?,?,?,?,?)', machine, it.label, row.revision, it.state, error, now);
        }
        this.sql.exec('INSERT OR REPLACE INTO engine_sync_hosts VALUES (?,?,?)', machine, data.state, now);
        return reply({ status: 'recorded' });
      });
    }
    // Hub-managed engine version rollout (intake 8cc9e31a). These paths sort
    // before the generic /api/engines/<label> match below ('versions' and
    // 'update' would otherwise parse as labels).
    if (path === '/api/engines/versions' && request.method === 'GET') {
      if (!admin && !host) return reply({ error: 'forbidden' }, 403);
      const machine = new URL(request.url).searchParams.get('machine') || undefined;
      return reply(this.rolloutVersions(machine ?? (host ? auth.caller.machineId : undefined)));
    }
    if (path === '/api/engines/version-report' && request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      const res: any = await this.reportEngineVersions(auth.caller.machineId, data);
      if (res.error) return reply(res, 400);
      if (data && typeof data.alert === 'object' && data.alert !== null) {
        const detail = typeof data.alert.detail === 'string' ? data.alert.detail.slice(0, 500) : 'engine rollout canary failed';
        this.sql.exec('INSERT OR REPLACE INTO engine_migrations VALUES (?,?,?)', 'engine_rollout_host_alert_v1', this.now(),
          JSON.stringify({ at: this.now(), machine: auth.caller.machineId, detail }));
      }
      return reply(res);
    }
    if (path === '/api/engines/update' && request.method === 'POST') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      if (data && typeof data.alert === 'object') {
        this.sql.exec('INSERT OR REPLACE INTO engine_migrations VALUES (?,?,?)', 'engine_rollout_host_alert_v1', this.now(),
          JSON.stringify({ at: this.now(), detail: typeof data.alert.detail === 'string' ? data.alert.detail.slice(0, 500) : 'engine rollout canary failed' }));
        return reply({ status: 'alert_recorded' });
      }
      const res: any = await this.startEngineRollout(data);
      if (res.error) return reply(res, 400);
      return reply(res);
    }
    const credentialMatch = /^\/api\/engines\/([a-z][a-z0-9_-]{0,47})\/(credential|rejection)$/.exec(path);
    if (credentialMatch) {
      if (!host) return reply({ error: 'forbidden' }, 403);
      return credentialMatch[2] === 'credential' ? this.handleCredential(request, credentialMatch[1], auth.caller)
        : this.handleRejection(request, credentialMatch[1], auth.caller);
    }
    const match = /^\/api\/engines\/([a-z][a-z0-9_-]{0,47})$/.exec(path);
    if (!match) return reply({ error: 'not_found' }, 404);
    const label = match[1];
    if (request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      return this.tx(() => {
        if (!this.hostAllowed(auth.caller.machineId, auth.caller)) return reply({ error: 'forbidden' }, 403);
        const row: any = this.sql.exec('SELECT * FROM engine_bundles WHERE label=?', label).toArray()[0];
        if (!row || !data || data.revision !== row.revision) return reply({ error: 'revision_changed' }, 409);
        this.sql.exec('INSERT OR REPLACE INTO engine_installations VALUES (?,?,?)', auth.caller.machineId, label, row.revision);
        if (row.provider === 'gemini' && data.authenticated !== true) return reply({status:'needs_login',label});
        this.sql.exec('INSERT OR REPLACE INTO fleet_routes VALUES (?,?,?,?)', auth.caller.machineId, PACKAGES[row.provider].engine, label, 'hub-' + label);
        return reply({ status: 'installed', label });
      });
    }
    if (!admin) return reply({ error: 'forbidden' }, 403);
    if (request.method === 'DELETE') return this.tx(() => {
      // Never remove a manually managed account with a colliding label.
      if (!this.sql.exec('SELECT 1 FROM engine_bundles WHERE label=?', label).toArray().length) return reply({ status: 'absent', label });
      this.sql.exec('DELETE FROM fleet_routes WHERE account=?', label);
      this.sql.exec('DELETE FROM fleet_subscriptions WHERE account=?', label);
      this.sql.exec('DELETE FROM engine_logins WHERE label=?', label);
      this.sql.exec('DELETE FROM engine_installations WHERE label=?', label);
      this.sql.exec('DELETE FROM engine_sync_status WHERE label=?', label);
      this.sql.exec('DELETE FROM engine_bundles WHERE label=?', label);
      bumpEngineEpoch(this.sql, this.now());
      this.sql.exec('DELETE FROM engine_credentials WHERE label=?', label);
      this.sql.exec('DELETE FROM engine_credential_rejections WHERE label=?', label);
      this.sql.exec('DELETE FROM fleet_accounts WHERE label=?', label);
      return reply({ status: 'removed', label });
    });
    if (request.method !== 'PUT') return reply({ error: 'method_not_allowed' }, 405);
    // Bound request bytes before JSON parsing; errors never include input.
    const reader = request.body?.getReader(); let size = 0; const chunks: Uint8Array[] = [];
    if (!reader) return reply({ error: 'invalid_bundle' }, 400);
    for (;;) { const part = await reader.read(); if (part.done) break; size += part.value.length; if (size > 32768) { await reader.cancel(); return reply({ error: 'bundle_too_large' }, 413); } chunks.push(part.value); }
    let bundle: any;
    try { const bytes = new Uint8Array(size); let offset = 0; for (const c of chunks) { bytes.set(c, offset); offset += c.length; } bundle = JSON.parse(new TextDecoder().decode(bytes)); } catch { return reply({ error: 'invalid_bundle' }, 400); }
    if (!validBundle(bundle) || bundle.label !== label) return reply({ error: 'invalid_bundle' }, 400);
    const maxParam = new URL(request.url).searchParams.get('max_concurrency');
    const requestedMax = explicitMax(maxParam === null ? undefined : /^\d{1,3}$/.test(maxParam) ? Number(maxParam) : NaN);
    if (Number.isNaN(requestedMax)) return reply({ error: 'invalid_max_concurrency' }, 400);
    const revision = crypto.randomUUID(); let ciphertext: string;
    // The imported credential becomes the next Hub-owned generation; the
    // bundle keeps the install snapshot only (intake 5814e109 I1).
    const credential = credentialSubset(bundle.provider, bundle.files);
    const generation = Number(this.credentialRow(label)?.generation ?? 0) + 1;
    let credentialCipher: string | null = null;
    try {
      ciphertext = await this.seal(bundle, revision);
      if (Object.keys(credential).length) credentialCipher = await this.sealCredential(label, generation, credential);
    } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
    return this.tx(() => {
      const managed = this.sql.exec('SELECT 1 FROM engine_bundles WHERE label=?', label).toArray().length;
      const existing: any = this.sql.exec('SELECT * FROM fleet_accounts WHERE label=?', label).toArray()[0];
      // Canonical provider family: `codex-orin` rows are the second Codex
      // account under provider `codex`, never a separate provider.
      const providerFamily = canonicalQuotaProvider(existing?.provider);
      if (existing && providerFamily !== accountProvider(bundle.provider)) return reply({ error: 'account_provider_mismatch' }, 409);
      // Same quota family is not enough: a managed label never switches harness
      // (gemini <-> antigravity), which would strand its credential under other paths.
      const prior: any = this.sql.exec('SELECT provider FROM engine_bundles WHERE label=?', label).toArray()[0];
      if (prior && prior.provider !== bundle.provider) return reply({ error: 'account_provider_mismatch' }, 409);
      if (!managed && existing) {
        // An explicit recovery may attach credentials to a stale registry pool,
        // but must neither rename the quota pool nor reset its limits.
        if (new URL(request.url).searchParams.get('adopt') !== 'true') return reply({ error: 'unmanaged_account_exists' }, 409);
        const reserved = this.sql.exec("SELECT 1 FROM fleet_launches WHERE account=? AND state IN ('offered','starting','running','uncertain') LIMIT 1", label).toArray().length;
        if (reserved || this.fleet.accountStatus(existing).active > 0) return reply({ error: 'account_active' }, 409);
      }
      if (!managed && rows().length >= 32) return reply({ error: 'bundle_capacity' }, 409);
      this.sql.exec('INSERT OR REPLACE INTO engine_bundles VALUES (?,?,?,?,?)', label, bundle.provider, bundle.version, revision, ciphertext);
      bumpEngineEpoch(this.sql, this.now());
      this.sql.exec('DELETE FROM engine_installations WHERE label=?', label);
      this.sql.exec('DELETE FROM fleet_routes WHERE account=?', label);
      this.sql.exec('DELETE FROM fleet_subscriptions WHERE account=?', label);
      // An existing pool keeps its limits; a new one inherits the provider default.
      if (!existing) this.sql.exec('INSERT INTO fleet_accounts VALUES (?,?,?,?,?)', label, accountProvider(bundle.provider), requestedMax ?? providerDefaultMaxConcurrency(bundle.provider), bundle.provider === 'codex' ? 15 : 0, 1);
      if (credentialCipher) {
        if (Number(this.credentialRow(label)?.generation ?? 0) + 1 !== generation) return reply({ error: 'credential_changed' }, 409);
        this.writeCredential(label, bundle.provider, generation, credentialCipher, credential, 'import');
      } else {
        this.sql.exec('DELETE FROM engine_credentials WHERE label=?', label);
        this.sql.exec('DELETE FROM engine_credential_rejections WHERE label=?', label);
      }
      return reply({ status: 'configured', label, revision, ...(credentialCipher ? { credential_generation: generation } : {}) });
    });
  }

  /** Current credential for launch-time materialization (GET) or a CAS write-back of a host-side rotation (POST). */
  private async handleCredential(request: Request, label: string, caller: any): Promise<Response> {
    const bundle: any = this.sql.exec('SELECT provider FROM engine_bundles WHERE label=?', label).toArray()[0];
    const row = this.credentialRow(label);
    if (!bundle || !row) return reply({ error: 'not_found' }, 404);
    const current = async () => {
      const latest = this.credentialRow(label);
      const files = await this.openCredential(latest);
      if (!this.hostAllowed(caller.machineId, caller)) return reply({ error: 'forbidden' }, 403);
      return reply({ label, generation: latest.generation, status: latest.status, files });
    };
    if (request.method === 'GET') {
      try { return await current(); } catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    }
    if (request.method !== 'POST') return reply({ error: 'method_not_allowed' }, 405);
    const data = await request.json().catch(() => null) as any;
    if (!data || !Number.isInteger(data.base_generation) || !validCredentialFiles(bundle.provider, data.files)) return reply({ error: 'invalid_credential' }, 400);
    let stored: CredentialFiles;
    try { stored = await this.openCredential(row); } catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    const merged = { ...stored, ...data.files };
    const unchanged = Object.keys(data.files).every(k => stored[k] === data.files[k]);
    const incoming = credentialFreshness(bundle.provider, merged);
    // CAS: a rotation based on the current generation wins; a divergent copy
    // wins only when its tokens were provably issued later. Otherwise the
    // host receives Hub's current copy and must adopt it.
    // Never accept an unusable file or a copy provably older than Hub's.
    const older = incoming !== null && row.freshness_ms !== null && incoming < row.freshness_ms;
    const accept = !unchanged && structurallyValid(bundle.provider, merged) && !older && (data.base_generation === row.generation
      || (incoming !== null && row.freshness_ms !== null && incoming > row.freshness_ms));
    if (accept) {
      const generation = row.generation + 1;
      let cipher: string;
      try { cipher = await this.sealCredential(label, generation, merged); } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
      const written = this.tx(() => {
        if (this.credentialRow(label)?.generation !== row.generation) return false;
        this.writeCredential(label, bundle.provider, generation, cipher, merged, 'host:' + caller.machineId);
        return true;
      });
      if (!written) return reply({ error: 'credential_changed' }, 409);
    }
    try { const res = await current(); const body: any = await res.json(); return reply({ ...body, accepted: accept }, res.status); }
    catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
  }

  /** A host reports that a credential generation failed authentication on it (I5). */
  private async handleRejection(request: Request, label: string, caller: any): Promise<Response> {
    if (request.method !== 'POST') return reply({ error: 'method_not_allowed' }, 405);
    const data = await request.json().catch(() => null) as any;
    const row = this.credentialRow(label);
    if (!row) return reply({ error: 'not_found' }, 404);
    if (!data || !Number.isInteger(data.generation) || typeof data.reason !== 'string' || !/^[a-z0-9_]{1,64}$/.test(data.reason)) return reply({ error: 'invalid_rejection' }, 400);
    if (data.generation !== row.generation) return reply({ status: 'superseded', generation: row.generation });
    this.tx(() => {
      this.sql.exec('INSERT OR REPLACE INTO engine_credential_rejections VALUES (?,?,?,?,?)', caller.machineId, label, row.generation, data.reason, this.now());
      // Re-validate centrally on the next maintenance pass instead of waiting for the schedule.
      this.sql.exec('UPDATE engine_credentials SET refresh_attempt_at=NULL, quota_attempt_at=NULL WHERE label=?', label);
    });
    return reply({ status: 'recorded', generation: row.generation });
  }

  /** Hub-central provider login: Hub owns the resulting token family; no host ever logs in. */
  private async handleCredentialLogin(request: Request, path: string, admin: boolean): Promise<Response | null> {
    const m = /^\/api\/engines\/credential-logins(?:\/([a-f0-9-]{36}))?$/.exec(path);
    if (!m) return null;
    if (!admin) return reply({ error: 'forbidden' }, 403);
    if (request.method !== 'POST') return reply({ error: 'method_not_allowed' }, 405);
    this.sql.exec('DELETE FROM engine_credential_logins WHERE expires_at <= ?', this.now());
    const data = await request.json().catch(() => null) as any;
    if (!m[1]) {
      if (!data || data.provider !== 'claude' || !LABEL.test(data.label) || (data.version !== undefined && (typeof data.version !== 'string' || !/^\d+\.\d+\.\d+$/.test(data.version))) || Number.isNaN(explicitMax(data.max_concurrency))) return reply({ error: 'invalid_login' }, 400);
      const bundle: any = this.sql.exec('SELECT provider FROM engine_bundles WHERE label=?', data.label).toArray()[0];
      if (bundle && bundle.provider !== 'claude') return reply({ error: 'account_provider_mismatch' }, 409);
      const id = crypto.randomUUID();
      const { url, verifier, state } = await claudeAuthorization();
      let sealed: string;
      try { sealed = await this.seal({ label: data.label, verifier, state, version: data.version ?? null, adopt: data.adopt === true, max_concurrency: explicitMax(data.max_concurrency) ?? null }, 'login:' + id); }
      catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
      const expiresAt = this.now() + CREDENTIAL_LOGIN_TTL_MS;
      this.sql.exec('INSERT INTO engine_credential_logins VALUES (?,?,?,?,?)', id, data.label, 'claude', sealed, expiresAt);
      return reply({ id, url, expires_in: CREDENTIAL_LOGIN_TTL_MS / 1000, expires_at: new Date(expiresAt).toISOString() });
    }
    const row: any = this.sql.exec('SELECT * FROM engine_credential_logins WHERE id=?', m[1]).toArray()[0];
    // Expired rows were purged above, so a missing row is an expired (or
    // already used) login: tell the operator to start over.
    if (!row) return reply({ error: 'login_expired' }, 410);
    let login: any;
    try { login = await this.open({ label: row.label, revision: 'login:' + row.id, ciphertext: row.sealed }); } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
    const install = (files: CredentialFiles) => this.installCentralCredential(row.label, 'claude', login.version, files, login.adopt === true, explicitMax(login.max_concurrency));
    // Host fallback upload: the CLI host exchanged the code itself because
    // Worker egress to the provider was blocked (like grantRefreshLease).
    if (data && data.files !== undefined) {
      if (!validCredentialFiles('claude', data.files) || !structurallyValid('claude', data.files)) return reply({ error: 'invalid_credential' }, 400);
      this.sql.exec('DELETE FROM engine_credential_logins WHERE id=?', row.id);
      return install(data.files);
    }
    if (!data || typeof data.code !== 'string' || data.code.length > 4096 || !/^[A-Za-z0-9_\-#.~]+$/.test(data.code.trim())) return reply({ error: 'invalid_code' }, 400);
    this.sql.exec('DELETE FROM engine_credential_logins WHERE id=?', row.id);
    const result = await claudeExchange(data.code, login.verifier, login.state, this.fetchFn, this.now());
    if (result.ok) return install(result.files);
    if (result.blocked) {
      // The provider never judged the code: keep the login so the CLI host can
      // exchange it from its own network and upload the credential.
      this.sql.exec('INSERT OR REPLACE INTO engine_credential_logins VALUES (?,?,?,?,?)', row.id, row.label, row.provider, row.sealed, row.expires_at);
      const sanitized = { provider_status: result.providerStatus ?? null, reason: result.detail ?? null };
      if (data.host_fallback !== true) return reply({ error: 'login_exchange_blocked', ...sanitized }, 502);
      return reply({ status: 'host_exchange_required', ...sanitized, expires_at: new Date(row.expires_at).toISOString(),
        exchange: { token_url: CLAUDE_OAUTH.token, profile_url: CLAUDE_OAUTH.profile, client_id: CLAUDE_OAUTH.client, redirect_uri: CLAUDE_OAUTH.redirect,
          scopes: CLAUDE_OAUTH.scopes, state: login.state, code_verifier: login.verifier } }, 202);
    }
    return reply({ error: result.reason, provider_status: result.providerStatus ?? null, reason: result.detail ?? null }, 400);
  }

  /** Store a Hub-minted credential: create the account bundle on first login, else publish a new generation. */
  private async installCentralCredential(label: string, provider: string, version: string | null, files: CredentialFiles, adopt: boolean, maxConcurrency?: number): Promise<Response> {
    const bundleRow: any = this.sql.exec('SELECT * FROM engine_bundles WHERE label=?', label).toArray()[0];
    if (!bundleRow) {
      const bundle = { label, provider, version: version || '2.1.304', files: { '.claude/settings.json': '{}' }, env: {}, args: [] };
      const query = new URLSearchParams({ ...(adopt ? { adopt: 'true' } : {}), ...(maxConcurrency ? { max_concurrency: String(maxConcurrency) } : {}) }).toString();
      const put = new Request('https://hub.internal/api/engines/' + label + (query ? '?' + query : ''), { method: 'PUT', body: JSON.stringify(bundle) });
      const res = await this.handle(put, { caller: { isAdmin: true } });
      if (res.status !== 200) return res;
    }
    const row = this.credentialRow(label);
    const generation = Number(row?.generation ?? 0) + 1;
    let cipher: string;
    try { cipher = await this.sealCredential(label, generation, files); } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
    this.tx(() => this.writeCredential(label, provider, generation, cipher, files, 'hub_login'));
    return reply({ status: 'configured', label, credential_generation: generation });
  }

  /**
   * Delegate a due refresh to this host when Hub's own provider egress is
   * blocked. One lease per label (10 min); the host refreshes and writes back
   * by CAS on this generation, so the token family keeps a single owner.
   */
  private grantRefreshLease(engine: any, machine: string): boolean {
    const row = this.credentialRow(engine.label);
    if (!row || row.status === 'invalid' || row.status_reason !== 'refresh_blocked' || engine.credential_generation !== row.generation) return false;
    if (!needsRefresh(row.provider, credentialSubset(row.provider, engine.files), this.now())) return false;
    return this.tx(() => {
      const lease: any = this.sql.exec('SELECT * FROM engine_credential_leases WHERE label=?', engine.label).toArray()[0];
      if (lease && lease.expires_at > this.now() && lease.generation === row.generation && lease.machine_id !== machine) return false;
      this.sql.exec('INSERT OR REPLACE INTO engine_credential_leases VALUES (?,?,?,?)', engine.label, machine, row.generation, this.now() + 600000);
      return true;
    });
  }

  fetchFn: FetchLike = (input, init) => fetch(input, init);
  private alert: (label: string, problem: { state: string; detail: string } | null) => void = () => {};
  setAlertSink(fn: (label: string, problem: { state: string; detail: string } | null) => void) { this.alert = fn; }

  /**
   * Central maintenance (cron): refresh each OAuth credential before any host
   * engine would, read quota directly from the provider with the stored
   * credential, and mark a credential invalid (alerting once) when the provider
   * rejects it. Returns non-secret per-label outcomes.
   */
  async maintainCredentials(nowMs = this.now()) {
    if (maintaining) return [{ status: 'already_running' }];
    maintaining = true;
    try { return await this.maintainOnce(nowMs); } finally { maintaining = false; }
  }

  private async maintainOnce(nowMs: number) {
    const out: any[] = [];
    await this.migrateCredentials(nowMs);
    const rows = this.sql.exec("SELECT * FROM engine_credentials ORDER BY label").toArray() as any[];
    for (const row of rows.slice(0, 32)) {
      const result: any = { label: row.label, generation: row.generation };
      try {
        let files = await this.openCredential(row);
        let generation = row.generation;
        const publish = async (next: CredentialFiles, by: string) => {
          const gen = generation + 1;
          const cipher = await this.sealCredential(row.label, gen, next);
          const ok = this.tx(() => {
            if (this.credentialRow(row.label)?.generation !== generation) return false;
            this.writeCredential(row.label, row.provider, gen, cipher, next, by);
            return true;
          });
          if (ok) { files = next; generation = gen; return true; }
          // A host wrote back meanwhile. Our refresh consumed the token family's
          // previous refresh token, so our copy is the live one unless theirs is newer.
          const latest = this.credentialRow(row.label);
          const ours = credentialFreshness(row.provider, next);
          if (!latest || ours === null || (latest.freshness_ms !== null && latest.freshness_ms >= ours)) return false;
          generation = latest.generation;
          return publish(next, by);
        };
        const markInvalid = (reason: string) => {
          const latest = this.credentialRow(row.label);
          // A newer generation landed (host write-back): judge that one next pass.
          if (!latest || latest.generation !== generation) { result.status = 'raced'; return; }
          // Two consecutive rejections of the same generation, at least one
          // backoff apart, before the account is withdrawn fleet-wide.
          const strike = typeof latest.status_reason === 'string' && latest.status_reason.startsWith('rejected_once:');
          if (!strike) {
            this.sql.exec('UPDATE engine_credentials SET status_reason=?, refresh_attempt_at=? WHERE label=? AND generation=?', 'rejected_once:' + reason, nowMs, row.label, generation);
            result.status = 'suspect'; result.reason = reason; return;
          }
          this.sql.exec("UPDATE engine_credentials SET status='invalid', status_reason=?, refresh_attempt_at=? WHERE label=? AND generation=?", reason, nowMs, row.label, generation);
          this.alert(row.label, { state: 'reauth_required', detail: `Engine account ${row.label} (${row.provider}) credential was rejected by the provider (${reason}). Hub will not offer it until it is re-authenticated: hub engines login/add --label ${row.label}.` });
          result.status = 'invalid'; result.reason = reason;
        };
        const tryRefresh = async (force: boolean) => {
          if (!refreshable(row.provider, files) || (!force && !needsRefresh(row.provider, files, nowMs))) return 'skipped';
          // Back off transient provider failures for ten minutes.
          if (!force && row.refresh_attempt_at && nowMs - row.refresh_attempt_at < 600000) return 'backoff';
          this.sql.exec('UPDATE engine_credentials SET refresh_attempt_at=? WHERE label=?', nowMs, row.label);
          const r = await refreshCredential(row.provider, files, this.fetchFn, nowMs);
          if (r.ok) return (await publish(r.files, 'hub_refresh')) ? 'refreshed' : 'raced';
          if (r.invalid) { markInvalid(r.reason); return result.status === 'invalid' ? 'invalid' : 'rejected'; }
          this.sql.exec('UPDATE engine_credentials SET status_reason=? WHERE label=?', r.reason, row.label);
          return r.reason;
        };
        if (row.status === 'invalid') { result.status = 'invalid'; out.push(result); continue; }
        // A credential migrated from a pre-existing bundle may be an old
        // snapshot whose live rotation sits on a host. Give hosts time to write
        // it back before Hub refreshes or judges it.
        const grace = row.updated_by === 'migration' && nowMs - row.updated_at < MIGRATION_GRACE_MS;
        if (grace) result.refresh = 'migration_grace';
        else result.refresh = await tryRefresh(false);
        if (result.refresh !== 'invalid' && (!row.quota_attempt_at || nowMs - row.quota_attempt_at >= 240000)) {
          this.sql.exec('UPDATE engine_credentials SET quota_attempt_at=? WHERE label=?', nowMs, row.label);
          let q = await fetchQuota(row.provider, files, this.fetchFn, nowMs);
          if (!q.ok && q.unauthorized && grace) { result.quota = 'migration_grace'; result.generation = generation; out.push(result); continue; }
          if (!q.ok && q.unauthorized && refreshable(row.provider, files)) {
            result.refresh = await tryRefresh(true);
            if (result.refresh === 'refreshed') q = await fetchQuota(row.provider, files, this.fetchFn, nowMs);
          }
          if (q.ok) {
            result.quota = this.storeQuota(row.label, row.provider, q.quota, nowMs) ? 'stored' : 'rejected';
            this.sql.exec("UPDATE engine_credentials SET status_reason=NULL WHERE label=? AND generation=? AND status_reason LIKE 'rejected_once:%'", row.label, generation);
            this.alert(row.label, null);
          }
          else if (q.unauthorized && !result.status) markInvalid(q.reason);
          else result.quota = q.reason;
        }
        result.generation = generation;
      } catch { result.error = 'maintenance_failed'; }
      out.push(result);
    }
    return out;
  }

  /** Adopt credentials of bundles imported before Hub owned them (generation 1, 'migration'). */
  private async migrateCredentials(nowMs: number) {
    const missing = this.sql.exec('SELECT b.* FROM engine_bundles b LEFT JOIN engine_credentials c ON c.label=b.label WHERE c.label IS NULL').toArray() as any[];
    for (const row of missing) {
      if (!(CREDENTIAL_PATHS[row.provider] ?? []).length) continue;
      try {
        const bundle = await this.open(row);
        const files = credentialSubset(row.provider, bundle.files);
        if (!Object.keys(files).length) continue;
        const cipher = await this.sealCredential(row.label, 1, files);
        this.tx(() => {
          if (this.credentialRow(row.label)) return;
          this.writeCredential(row.label, row.provider, 1, cipher, files, 'migration');
          this.sql.exec('UPDATE engine_credentials SET updated_at=? WHERE label=?', nowMs, row.label);
        });
      } catch { /* undecryptable legacy bundle: leave it to the existing path */ }
    }
  }

  /** Persist a Hub-direct provider observation exactly like a server collector snapshot. */
  private storeQuota(label: string, provider: string, quota: any, nowMs: number): boolean {
    const account = canonicalAccountLabel(label);
    const capturedAt = new Date(nowMs).toISOString();
    const family = canonicalQuotaProvider(provider);
    const valid = validateQuotaSnapshotPayload({ schema_version: '1.1', collector_version: 'hub-direct/1.0', captured_at: capturedAt, host_id: 'hub-server', status: 'ok', error: null,
      routes: { [family]: quota } }, 'hub-server', nowMs);
    if (!valid.ok) return false;
    return this.tx(() => {
      const old: any = this.sql.exec('SELECT captured_at, quota_json FROM server_quota_snapshots WHERE account = ?', account).toArray()[0];
      if (old && Date.parse(old.captured_at) >= nowMs) return false;
      const stored = valid.snapshot!.routes[family];
      const deadline = old ? JSON.parse(old.quota_json)?.details?.cooldown_until : null;
      if (stored.details.cooldown_until == null && typeof deadline === 'string' && Date.parse(deadline) > nowMs) stored.details.cooldown_until = deadline;
      this.sql.exec('INSERT OR REPLACE INTO server_quota_snapshots VALUES (?, ?, ?, ?)', account, family, capturedAt, JSON.stringify(stored));
      syncTbmFromServerAccounts(this.sql, [{ account, provider: family, quota: stored }], nowMs);
      return true;
    });
  }

  // ---- Engine rollout state machine (intake 8cc9e31a) ----
  /** Fleet-wide per-host version view (AC5): installed/latest/last result. */
  rolloutVersions(machine?: string) {
    const desired = (this.sql.exec('SELECT engine, version, latest, updated_at FROM engine_rollout_desired ORDER BY engine').toArray() as any[]);
    const reports = (machine
      ? this.sql.exec('SELECT * FROM engine_version_reports WHERE machine_id=? ORDER BY engine', machine)
      : this.sql.exec('SELECT * FROM engine_version_reports ORDER BY machine_id, engine').toArray() as any[]);
    const rollouts = (this.sql.exec('SELECT id, engine, target_version, canary_machine, state, created_at, updated_at, detail FROM engine_rollouts ORDER BY created_at DESC LIMIT 10').toArray() as any[])
      .map((r: any) => ({ ...r, detail: (() => { try { return JSON.parse(r.detail); } catch { return {}; } })() }));
    return { desired: Object.fromEntries(desired.map((d: any) => [d.engine, { version: d.version, latest: d.latest ?? d.version, updated_at: d.updated_at }])), reports, rollouts };
  }

  /** Host check-in of its per-engine versions (AC5 source: a check run on that host). */
  async reportEngineVersions(machineId: string, body: any) {
    const engines = body?.engines;
    if (!object(engines) || Object.keys(engines).length > 8) return { error: 'invalid_report' };
    const now = this.now();
    const applied: string[] = [];
    for (const [engine, row] of Object.entries(engines)) {
      if (!this.rolloutProvider(engine) || !object(row)) continue;
      const installed = typeof (row as any).installed === 'string' && /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test((row as any).installed) ? (row as any).installed : null;
      const latest = typeof (row as any).latest === 'string' && /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test((row as any).latest) ? (row as any).latest : null;
      const result = typeof (row as any).result === 'string' && /^[A-Za-z0-9_]{1,32}$/.test((row as any).result) ? (row as any).result : null;
      this.sql.exec('INSERT OR REPLACE INTO engine_version_reports VALUES (?,?,?,?,?,?)', machineId, engine, installed, latest, result, now);
      applied.push(engine);
      // Canary gate (AC3): the canary host reporting green promotes the
      // rollout; a failure rolls back to the previous versions.
      const pending: any = this.sql.exec("SELECT * FROM engine_rollouts WHERE engine=? AND canary_machine=? AND state='canary_pending' ORDER BY created_at DESC LIMIT 1", engine, machineId).toArray()[0];
      if (pending && (result === 'green' || installed)) {
        if (result === 'green') {
          this.sql.exec("UPDATE engine_rollouts SET state='rolling', updated_at=?, detail=? WHERE id=?", now,
            JSON.stringify({ ...JSON.parse(pending.detail || '{}'), canary: 'green', canary_at: now }), pending.id);
        } else if (result && result !== 'green' && installed !== pending.target_version) {
          await this.rollbackEngineRollout(pending, `canary ${result}`);
        }
      }
    }
    return { status: 'recorded', engines: applied };
  }

  /** Roll one rollout back to its previous bundle versions. Keeps serving the old build. */
  private async rollbackEngineRollout(pending: any, reason: string) {
    const now = this.now();
    try {
      const previous = JSON.parse(pending.previous_json || '{}') as Record<string, string>;
      for (const [label, version] of Object.entries(previous)) {
        const row: any = this.sql.exec('SELECT * FROM engine_bundles WHERE label=?', label).toArray()[0];
        if (!row) continue;
        const bundle = await this.open(row);
        if (bundle.version === version) continue;
        bundle.version = version;
        delete (bundle as any).revision;
        const revision = crypto.randomUUID();
        const ciphertext = await this.seal(bundle, revision);
        this.tx(() => {
          this.sql.exec('UPDATE engine_bundles SET version=?, revision=?, ciphertext=? WHERE label=?', version, revision, ciphertext, label);
          this.sql.exec('DELETE FROM engine_installations WHERE label=?', label);
          this.sql.exec('DELETE FROM fleet_routes WHERE account=?', label);
          this.sql.exec('DELETE FROM fleet_subscriptions WHERE account=?', label);
          bumpEngineEpoch(this.sql, now);
        });
      }
    } catch { /* rollback best-effort; the rollout row still records the failure */ }
    this.sql.exec("UPDATE engine_rollouts SET state='rolled_back', updated_at=?, detail=? WHERE id=?", now,
      JSON.stringify({ reason, rolled_back_at: now }), pending.id);
  }

  /**
   * On-demand rollout (AC1): bump central bundle versions for one engine
   * (default: every supported engine whose bundles lag the desired pin),
   * canary first. Hosts pick the new revision up on sync; the previous
   * revision dir stays until live sessions drain (host-side AC2).
   */
  async startEngineRollout(input: any) {
    const now = this.now();
    const engine = typeof input?.engine === 'string' ? input.engine : null;
    const engines = engine ? [engine] : Object.keys(this.rolloutDesired());
    for (const e of engines) if (!this.rolloutProvider(e)) return { error: 'invalid_engine' };
    const canary = typeof input?.canary === 'string' && input.canary.length >= 1 && input.canary.length <= 64
      ? input.canary : typeof input?.machine === 'string' && input.machine ? input.machine : 'RMTHZ';
    const out: any[] = [];
    for (const e of engines) {
      const provider = this.rolloutProvider(e)!;
      const target = (this.sql.exec('SELECT version FROM engine_rollout_desired WHERE engine=?', e).toArray()[0] as any)?.version
        ?? this.rolloutDesired()[e];
      const rows = this.sql.exec('SELECT label, version FROM engine_bundles WHERE provider=? ORDER BY label', provider).toArray() as any[];
      const lagging = rows.filter(r => r.version !== target);
      const previous: Record<string, string> = Object.fromEntries(rows.map(r => [r.label, r.version]));
      let bumped: string[] = [];
      if (lagging.length && !input?.check_only) {
        for (const r of lagging) {
          const row: any = this.sql.exec('SELECT * FROM engine_bundles WHERE label=?', r.label).toArray()[0];
          if (!row) continue;
          try {
            const bundle = await this.open(row);
            bundle.version = target;
            delete (bundle as any).revision;
            const revision = crypto.randomUUID();
            const ciphertext = await this.seal(bundle, revision);
            this.tx(() => {
              this.sql.exec('UPDATE engine_bundles SET version=?, revision=?, ciphertext=? WHERE label=?', target, revision, ciphertext, r.label);
              this.sql.exec('DELETE FROM engine_installations WHERE label=?', r.label);
              this.sql.exec('DELETE FROM fleet_routes WHERE account=?', r.label);
              this.sql.exec('DELETE FROM fleet_subscriptions WHERE account=?', r.label);
              bumpEngineEpoch(this.sql, now);
            });
            bumped.push(r.label);
          } catch { return { error: 'bundle_decryption_unavailable' }; }
        }
      }
      const open: any = this.sql.exec("SELECT id FROM engine_rollouts WHERE engine=? AND state IN ('canary_pending','rolling') ORDER BY created_at DESC LIMIT 1", e).toArray()[0];
      if (open && !input?.restart) {
        out.push({ engine: e, state: 'already_running', rollout_id: open.id, bumped });
        continue;
      }
      const id = crypto.randomUUID();
      this.sql.exec('INSERT INTO engine_rollouts VALUES (?,?,?,?,?,?,?,?,?)', id, e, target, JSON.stringify(previous), canary,
        lagging.length || bumped.length ? 'canary_pending' : 'done', now, now,
        JSON.stringify({ bumped, requested_machine: input?.machine ?? null }));
      out.push({ engine: e, state: lagging.length || bumped.length ? 'canary_pending' : 'current', rollout_id: id, canary, target_version: target, bumped });
    }
    return { rollouts: out };
  }

  /** Daily scheduled check (AC1): compare upstream latest vs desired pins. */
  async tickEngineRollout(nowMs = this.now(), fetchLatest?: (pkg: string) => Promise<string | null>): Promise<Array<{ subject: string; problem: { state: string; detail: string } | null }>> {
    const last: any = this.sql.exec("SELECT detail FROM engine_migrations WHERE name='engine_rollout_daily_v1'").toArray()[0];
    const lastAt = last ? (() => { try { return JSON.parse(last.detail).at ?? 0; } catch { return 0; } })() : 0;
    if (nowMs - lastAt < 20 * 3600 * 1000) return [];
    const fetchFn = fetchLatest ?? (async (pkg: string) => {
      try {
        const res = await fetch('https://registry.npmjs.org/' + pkg + '/latest', { signal: AbortSignal.timeout(10000) });
        if (!res.ok) return null;
        const data: any = await res.json();
        return typeof data?.version === 'string' && /^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(data.version) ? data.version : null;
      } catch { return null; }
    });
    const pkgFor: Record<string, string | null> = { codex: '@openai/codex', claude: '@anthropic-ai/claude-code', opencode: 'opencode-ai', agy: null, zcodex: null };
    const found: Array<{ subject: string; problem: { state: string; detail: string } | null }> = [];
    for (const [engine, pkg] of Object.entries(pkgFor)) {
      if (!pkg) continue;
      const latest = await fetchFn(pkg);
      if (!latest) continue;
      const row: any = this.sql.exec('SELECT version, latest FROM engine_rollout_desired WHERE engine=?', engine).toArray()[0];
      if (!row) continue;
      if (latest !== row.latest) this.sql.exec('UPDATE engine_rollout_desired SET latest=?, updated_at=? WHERE engine=?', latest, nowMs, engine);
      if (latest !== row.version && latest !== row.latest) {
        found.push({ subject: engine, problem: { state: 'attention',
          detail: `Engine update available: ${engine} ${row.version} -> ${latest}. Run \`hub engines update --engine ${engine}\` (canary on RMTHZ, real smoke, then fleet rollout).` } });
      }
    }
    this.sql.exec('INSERT OR REPLACE INTO engine_migrations VALUES (?,?,?)', 'engine_rollout_daily_v1', nowMs, JSON.stringify({ at: nowMs }));
    return found.slice(0, 1);
  }
}
