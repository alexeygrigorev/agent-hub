/** Centrally managed engine bundles. Secrets are encrypted at rest and never returned to UI/list callers. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { Env } from './env';
import { validSessionSecret } from './auth/session';
import type { FleetService } from './fleet';
import { canonicalQuotaProvider } from './quota';

const LABEL = /^[a-z][a-z0-9_-]{0,47}$/;
export const PACKAGES: Record<string, { pkg: string; bin: string; engine: string }> = {
  codex: { pkg: '@openai/codex', bin: 'codex', engine: 'codex' },
  claude: { pkg: '@anthropic-ai/claude-code', bin: 'claude', engine: 'claude' },
  gemini: { pkg: '@google/gemini-cli', bin: 'gemini', engine: 'gemini' },
  go: { pkg: 'opencode-ai', bin: 'opencode', engine: 'opencode' },
  zai: { pkg: 'zcodex', bin: 'zcodex', engine: 'codex' },
};
const ENV = new Set(['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENCODE_API_KEY', 'ZAI_API_KEY', 'ZAI_BASE_URL', 'ZAI_TOKEN']);
const headers = { 'Cache-Control': 'private, no-store', 'Pragma': 'no-cache' };
const reply = (data: unknown, status = 200) => Response.json(data, { status, headers });
const object = (v: any) => v && typeof v === 'object' && !Array.isArray(v);
export function validBundle(v: any): boolean {
  if (!object(v) || Object.keys(v).some(k => !['label', 'provider', 'version', 'files', 'env', 'args', 'auth_method'].includes(k)) || !LABEL.test(v.label) || !Object.hasOwn(PACKAGES, v.provider) || typeof v.version !== 'string' || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(v.version)) return false;
  if (v.provider === 'gemini' && (v.auth_method !== 'machine-login' || Object.keys(v.files || {}).some(k => /oauth|credential|token/i.test(k)) || Object.keys(v.env || {}).length)) return false;
  if (!object(v.files) || Object.keys(v.files).length > 24 || !object(v.env) || Object.keys(v.env).length > 12) return false;
  if (v.provider === 'go' && (Object.keys(v.files).length || Object.keys(v.env).length)) return false;
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
    return { ...JSON.parse(new TextDecoder().decode(data)), revision: row.revision, quota_provider: account?.provider ?? row.provider };
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
      return reply({ logins: this.sql.exec('SELECT id,machine_id,label,status,url,expires_at FROM engine_logins WHERE expires_at > ?', Date.now()).toArray(), engines: rows().map(({ ciphertext, ...meta }) => ({ ...meta, supports_goal: ['codex', 'zai'].includes(meta.provider) })), installations: this.sql.exec('SELECT * FROM engine_installations ORDER BY machine_id,label').toArray() });
    }
    if (path === '/api/engines/sync' && request.method === 'GET') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      try { const engines = await Promise.all(rows().map(r => this.open(r)));
        if (!this.hostAllowed(auth.caller.machineId, auth.caller)) return reply({ error: 'forbidden' }, 403);
        return reply({ engines, logins: this.sql.exec("SELECT id,label,status FROM engine_logins WHERE machine_id=? AND expires_at>? AND status IN ('requested','waiting','submitted')", auth.caller.machineId, Date.now()).toArray() }); }
      catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    }
    if (path === '/api/engines/admission' && request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      const data = await request.json().catch(() => null) as any;
      if (!data || !LABEL.test(data.label)) return reply({ error: 'invalid_label' }, 400);
      const a = this.sql.exec('SELECT * FROM fleet_accounts WHERE label = ?', data.label).toArray()[0];
      const installed = this.sql.exec('SELECT 1 FROM engine_installations i JOIN engine_bundles b ON b.label=i.label AND b.revision=i.revision WHERE i.machine_id=? AND i.label=?', auth.caller.machineId, data.label).toArray().length;
      return reply({ launch_allowed: !!installed && !!a && this.fleet.accountStatus(a).status === 'ok', profile_supported: !!installed, account_label: data.label, observed_at: new Date().toISOString() });
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
      this.sql.exec('DELETE FROM engine_bundles WHERE label=?', label);
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
    const revision = crypto.randomUUID(); let ciphertext: string;
    try { ciphertext = await this.seal(bundle, revision); } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
    return this.tx(() => {
      const managed = this.sql.exec('SELECT 1 FROM engine_bundles WHERE label=?', label).toArray().length;
      const existing: any = this.sql.exec('SELECT * FROM fleet_accounts WHERE label=?', label).toArray()[0];
      // Canonical provider family: `codex-orin` rows are the second Codex
      // account under provider `codex`, never a separate provider.
      const providerFamily = canonicalQuotaProvider(existing?.provider);
      if (existing && providerFamily !== bundle.provider) return reply({ error: 'account_provider_mismatch' }, 409);
      if (!managed && existing) {
        // An explicit recovery may attach credentials to a stale registry pool,
        // but must neither rename the quota pool nor reset its limits.
        if (new URL(request.url).searchParams.get('adopt') !== 'true') return reply({ error: 'unmanaged_account_exists' }, 409);
        const reserved = this.sql.exec("SELECT 1 FROM fleet_launches WHERE account=? AND state IN ('offered','starting','running','uncertain') LIMIT 1", label).toArray().length;
        if (reserved || this.fleet.accountStatus(existing).active > 0) return reply({ error: 'account_active' }, 409);
      }
      if (!managed && rows().length >= 32) return reply({ error: 'bundle_capacity' }, 409);
      this.sql.exec('INSERT OR REPLACE INTO engine_bundles VALUES (?,?,?,?,?)', label, bundle.provider, bundle.version, revision, ciphertext);
      this.sql.exec('DELETE FROM engine_installations WHERE label=?', label);
      this.sql.exec('DELETE FROM fleet_routes WHERE account=?', label);
      this.sql.exec('DELETE FROM fleet_subscriptions WHERE account=?', label);
      if (!existing) this.sql.exec('INSERT INTO fleet_accounts VALUES (?,?,?,?,?)', label, bundle.provider, 1, bundle.provider === 'codex' ? 15 : 0, 1);
      return reply({ status: 'configured', label, revision });
    });
  }
}
