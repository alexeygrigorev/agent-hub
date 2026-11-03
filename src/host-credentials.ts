/**
 * Hub-owned host credentials that are not engine accounts (intake 54d5b703):
 * the GitHub CLI token, the one shared git SSH key and the commit identity.
 * Hub is the only place they are created or changed; every host materializes
 * the current generation on its heartbeat, so nothing is ever copied between
 * machines by hand. Secrets are AES-GCM sealed at rest and leave this module
 * only to an approved host. Responses, logs and errors carry fixed reason
 * codes, never token or key material.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { Env } from './env';
import { validSessionSecret } from './auth/session';

export const HOST_CREDENTIAL_KINDS = ['gh_token', 'git_ssh_key', 'git_identity'] as const;
export type HostCredentialKind = typeof HOST_CREDENTIAL_KINDS[number];
type Payload = Record<string, string>;
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

const MAX_FIELD = 16000;
const GH_TOKEN = /^(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})$/;
const SSH_PUBLIC = /^ssh-ed25519 [A-Za-z0-9+/]{43,}={0,2}(?: [^\r\n]{0,128})?$/;
const SSH_PRIVATE = /^-----BEGIN OPENSSH PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END OPENSSH PRIVATE KEY-----\r?\n?$/;
const IDENT_NAME = /^[^\r\n<>\0]{1,100}$/;
const IDENT_EMAIL = /^[^\s<>@\0]{1,100}@[^\s<>@\0]{1,150}$/;
const GITHUB_API = 'https://api.github.com';
const TIMEOUT_MS = 15000;
const STATUS_STALE_MS = 15 * 60000;

const reply = (body: unknown, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
const object = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown, max = MAX_FIELD): v is string => typeof v === 'string' && v.length > 0 && v.length <= max && !v.includes('\0');
const isKind = (v: string): v is HostCredentialKind => (HOST_CREDENTIAL_KINDS as readonly string[]).includes(v);
/** Who wrote a credential, for the admin overview only; bounded, never secret material. */
const actorSlug = (caller: any): string => {
  const a = caller?.actor;
  return typeof a === 'string' && /^[A-Za-z0-9_: .@-]{1,100}$/.test(a) ? a : (caller?.isAdmin === true ? 'admin' : 'unknown');
};

/** The exact payload a kind accepts; anything else is refused whole. */
export function validPayload(kind: HostCredentialKind, value: unknown): value is Payload {
  if (!object(value)) return false;
  const keys = Object.keys(value).sort().join(',');
  if (kind === 'gh_token') return keys === 'token' && typeof value.token === 'string' && GH_TOKEN.test(value.token);
  if (kind === 'git_ssh_key') return keys === 'private_key,public_key' && text(value.private_key) && SSH_PRIVATE.test(value.private_key)
    && text(value.public_key, 2000) && SSH_PUBLIC.test(value.public_key);
  return keys === 'email,name' && text(value.name, 100) && IDENT_NAME.test(value.name) && text(value.email, 256) && IDENT_EMAIL.test(value.email);
}

/** SHA256:<base64> fingerprint of an OpenSSH public key line, as `ssh-keygen -l` prints it. */
export async function sshFingerprint(publicKey: string): Promise<string | null> {
  try {
    const blob = publicKey.split(' ')[1];
    const raw = Uint8Array.from(atob(blob), c => c.charCodeAt(0));
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', raw));
    return 'SHA256:' + btoa(String.fromCharCode(...digest)).replace(/=+$/, '');
  } catch { return null; }
}

/** What a host reports about its own state; every field is bounded and non-secret. */
export function sanitizeStatus(value: unknown) {
  if (!object(value)) return null;
  const slug = (v: unknown, max = 64) => typeof v === 'string' && /^[A-Za-z0-9_.@:-]{1,64}$/.test(v) && v.length <= max ? v : null;
  const gh = object(value.gh) ? value.gh : {};
  const ssh = object(value.ssh) ? value.ssh : {};
  const scopes = Array.isArray(gh.scopes) ? gh.scopes.map((s: unknown) => slug(s)).filter((s: string | null): s is string => !!s).slice(0, 32) : [];
  const generations: Record<string, number> = {};
  if (object(value.generations)) for (const k of HOST_CREDENTIAL_KINDS) {
    const g = value.generations[k];
    if (Number.isInteger(g) && g >= 0 && g < 1e9) generations[k] = g;
  }
  return {
    gh: { logged_in: gh.logged_in === true, account: slug(gh.account), scopes, error: slug(gh.error) },
    ssh: { authenticated: ssh.authenticated === true, account: slug(ssh.account), key_fingerprint: typeof ssh.key_fingerprint === 'string' && /^SHA256:[A-Za-z0-9+/]{43}$/.test(ssh.key_fingerprint) ? ssh.key_fingerprint : null, error: slug(ssh.error) },
    identity_applied: value.identity_applied === true,
    generations,
  };
}

export class HostCredentialService {
  constructor(private sql: SqlStorage, private tx: <T>(fn: () => T) => T, private env: Env,
    private hostAllowed: (machine: string, caller: any) => boolean, private fetchFn: FetchLike = (u, i) => fetch(u, i),
    private now: () => number = () => Date.now()) {}

  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS host_credentials (kind TEXT PRIMARY KEY, generation INTEGER NOT NULL, ciphertext TEXT NOT NULL, meta TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS host_credential_status (machine_id TEXT PRIMARY KEY, report TEXT NOT NULL, reported_at INTEGER NOT NULL, resync_requested_at INTEGER)');
  }

  private row(kind: string): any { return this.sql.exec('SELECT * FROM host_credentials WHERE kind=?', kind).toArray()[0]; }

  private async key() {
    if (!validSessionSecret(this.env)) throw new Error('provisioning_key_unavailable');
    const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(this.env.SESSION_SECRET), 'HKDF', false, ['deriveKey']);
    return crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: new TextEncoder().encode('hub-engine-bundles-v1'), info: new TextEncoder().encode('host-credentials-at-rest') },
      material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  }
  private async seal(kind: string, generation: number, payload: Payload) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const data = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: new TextEncoder().encode(kind + ':' + generation) }, await this.key(), new TextEncoder().encode(JSON.stringify(payload)));
    return JSON.stringify({ iv: Array.from(iv), data: Array.from(new Uint8Array(data)) });
  }
  private async open(row: any): Promise<Payload> {
    const c = JSON.parse(row.ciphertext);
    const data = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: new Uint8Array(c.iv), additionalData: new TextEncoder().encode(row.kind + ':' + row.generation) }, await this.key(), new Uint8Array(c.data));
    return JSON.parse(new TextDecoder().decode(data));
  }

  /** Non-secret metadata per kind plus the last status every machine reported. */
  overview() {
    const now = this.now();
    const kinds = (this.sql.exec('SELECT kind,generation,meta,updated_by,updated_at FROM host_credentials ORDER BY kind').toArray() as any[])
      .map(r => ({ kind: r.kind, generation: r.generation, updated_by: r.updated_by, updated_at: r.updated_at, ...JSON.parse(r.meta) }));
    const machines = (this.sql.exec('SELECT m.id, m.name, s.report, s.reported_at, s.resync_requested_at FROM machines m LEFT JOIN host_credential_status s ON s.machine_id=m.id WHERE m.registered_at IS NOT NULL AND m.revoked_at IS NULL ORDER BY m.id').toArray() as any[])
      .map(m => {
        const report = m.report ? JSON.parse(m.report) : null;
        return { machine_id: m.id, machine: m.name, reported_at: m.reported_at ?? null, stale: m.reported_at == null || now - m.reported_at > STATUS_STALE_MS,
          resync_pending: m.resync_requested_at != null, gh: report?.gh ?? null, ssh: report?.ssh ?? null, identity_applied: report?.identity_applied ?? null, generations: report?.generations ?? {} };
      });
    return { kinds, machines };
  }

  async handle(request: Request, auth: any): Promise<Response> {
    if (auth.error) return auth.error;
    if (!auth.caller) return reply({ error: 'unauthorized' }, 401);
    const url = new URL(request.url);
    const path = url.pathname;
    const admin = auth.caller.isAdmin === true;
    const host = auth.caller.kind === 'host' && auth.scopes?.includes('host') && !!auth.caller.machineId && this.hostAllowed(auth.caller.machineId, auth.caller);
    if (path === '/api/host-credentials' && request.method === 'GET') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      return reply(this.overview());
    }
    if (path === '/api/host-credentials/sync' && request.method === 'GET') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      return this.handleSync(url, auth.caller.machineId);
    }
    if (path === '/api/host-credentials/status' && request.method === 'POST') {
      if (!host) return reply({ error: 'forbidden' }, 403);
      return this.handleStatus(request, auth.caller.machineId);
    }
    if (path === '/api/host-credentials/register-key' && request.method === 'POST') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      return this.registerKey();
    }
    if (path === '/api/host-credentials/resync' && request.method === 'POST') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      this.sql.exec('UPDATE host_credential_status SET resync_requested_at=?', this.now());
      return reply({ status: 'requested' });
    }
    const m = /^\/api\/host-credentials\/([a-z_]{1,32})$/.exec(path);
    if (!m || !isKind(m[1])) return reply({ error: 'not_found' }, 404);
    if (request.method === 'PUT') {
      if (!admin) return reply({ error: 'forbidden' }, 403);
      return this.handlePut(request, m[1], auth.caller);
    }
    return reply({ error: 'method_not_allowed' }, 405);
  }

  private async handlePut(request: Request, kind: HostCredentialKind, caller: any): Promise<Response> {
    const data = await request.json().catch(() => null) as any;
    if (!validPayload(kind, data?.payload)) return reply({ error: 'invalid_credential' }, 400);
    const payload = data.payload as Payload;
    const prior = this.row(kind);
    const generation = Number(prior?.generation ?? 0) + 1;
    const meta: Record<string, unknown> = {};
    if (kind === 'git_ssh_key') {
      meta.public_key = payload.public_key;
      meta.fingerprint = await sshFingerprint(payload.public_key);
      meta.registered = false;
    } else if (kind === 'git_identity') {
      meta.name = payload.name; meta.email = payload.email;
    } else {
      // Verify the token and record who it is and what it can do. A refusal
      // by GitHub stores nothing; an unreachable GitHub is not a verdict.
      const who = await this.githubWhoami(payload.token);
      if (who.ok === false && who.invalid) return reply({ error: 'token_rejected_by_github' }, 422);
      if (who.ok) { meta.account = who.account; meta.scopes = who.scopes; }
    }
    let cipher: string;
    try { cipher = await this.seal(kind, generation, payload); } catch { return reply({ error: 'provisioning_key_unavailable' }, 503); }
    const written = this.tx(() => {
      if (Number(this.row(kind)?.generation ?? 0) + 1 !== generation) return false;
      this.sql.exec('INSERT OR REPLACE INTO host_credentials VALUES (?,?,?,?,?,?)', kind, generation, cipher, JSON.stringify(meta),
        actorSlug(caller), this.now());
      // A new generation must reach every host promptly.
      this.sql.exec('UPDATE host_credential_status SET resync_requested_at=?', this.now());
      return true;
    });
    if (!written) return reply({ error: 'credential_changed' }, 409);
    return reply({ status: 'stored', kind, generation, ...meta });
  }

  private async githubWhoami(token: string): Promise<{ ok: true; account: string; scopes: string[] } | { ok: false; invalid: boolean }> {
    try {
      const res = await this.fetchFn(GITHUB_API + '/user', { redirect: 'manual', headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'User-Agent': 'agent-hub', 'X-GitHub-Api-Version': '2022-11-28' }, signal: AbortSignal.timeout(TIMEOUT_MS) });
      if (res.status === 401) return { ok: false, invalid: true };
      if (res.status !== 200) return { ok: false, invalid: false };
      const body: any = await res.json().catch(() => null);
      const scopes = (res.headers.get('x-oauth-scopes') ?? '').split(',').map(s => s.trim()).filter(Boolean);
      return typeof body?.login === 'string' && /^[A-Za-z0-9-]{1,39}$/.test(body.login) ? { ok: true, account: body.login, scopes } : { ok: false, invalid: false };
    } catch { return { ok: false, invalid: false }; }
  }

  /**
   * Hosts send the generations they hold. Only changed kinds (or everything
   * after a Hub-requested re-sync) come back, so steady-state heartbeats carry
   * no secrets and write no rows.
   */
  private async handleSync(url: URL, machine: string): Promise<Response> {
    const have: Record<string, number> = {};
    for (const part of (url.searchParams.get('have') ?? '').split(',')) {
      const [k, g] = part.split(':');
      if (k && isKind(k) && /^\d{1,9}$/.test(g ?? '')) have[k] = Number(g);
    }
    const status: any = this.sql.exec('SELECT resync_requested_at FROM host_credential_status WHERE machine_id=?', machine).toArray()[0];
    const force = status?.resync_requested_at != null;
    const generations: Record<string, number> = {};
    const credentials: Record<string, { generation: number; payload: Payload; registered?: boolean }> = {};
    try {
      for (const r of this.sql.exec('SELECT * FROM host_credentials ORDER BY kind').toArray() as any[]) {
        generations[r.kind] = r.generation;
        if (force || have[r.kind] !== r.generation) {
          // GitHub does not accept the shared key until register-key succeeds;
          // hosts must not install it before that (it would override their
          // working github.com identity with a key GitHub refuses).
          const registered = r.kind === 'git_ssh_key' ? JSON.parse(r.meta).registered === true : undefined;
          credentials[r.kind] = { generation: r.generation, payload: await this.open(r), ...(registered === undefined ? {} : { registered }) };
        }
      }
    } catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    if (force) this.sql.exec('UPDATE host_credential_status SET resync_requested_at=NULL WHERE machine_id=?', machine);
    return reply({ generations, credentials, forced: force });
  }

  /** A host reports gh/ssh state. A missing login makes Hub schedule a re-sync for that host (AC4). */
  private async handleStatus(request: Request, machine: string): Promise<Response> {
    const report = sanitizeStatus(await request.json().catch(() => null));
    if (!report) return reply({ error: 'invalid_report' }, 400);
    const keyRow = this.row('git_ssh_key');
    const keyUsable = !!keyRow && JSON.parse(keyRow.meta).registered === true;
    const ghMissing = !report.gh.logged_in && !!this.row('gh_token');
    const sshMissing = !report.ssh.authenticated && keyUsable;
    const body = JSON.stringify(report);
    const prior: any = this.sql.exec('SELECT report, resync_requested_at FROM host_credential_status WHERE machine_id=?', machine).toArray()[0];
    const resync = ghMissing || sshMissing;
    // Write only on change: Free-plan Durable Object row writes are a daily budget.
    if (!prior || prior.report !== body || (resync && prior.resync_requested_at == null)) {
      this.sql.exec('INSERT OR REPLACE INTO host_credential_status VALUES (?,?,?,?)', machine, body, this.now(), resync ? this.now() : prior?.resync_requested_at ?? null);
    }
    return reply({ status: 'recorded', resync: resync || prior?.resync_requested_at != null });
  }

  /** Register the shared public key on the account that owns the stored gh token (AC7). Hub does it; nobody is asked. */
  async registerKey(): Promise<Response> {
    const keyRow = this.row('git_ssh_key'); const tokenRow = this.row('gh_token');
    if (!keyRow || !tokenRow) return reply({ error: 'credential_missing' }, 409);
    let key: Payload; let token: Payload;
    try { key = await this.open(keyRow); token = await this.open(tokenRow); } catch { return reply({ error: 'bundle_decryption_unavailable' }, 503); }
    const headers = { Authorization: 'Bearer ' + token.token, Accept: 'application/vnd.github+json', 'User-Agent': 'agent-hub', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' };
    let outcome = 'github_unreachable';
    try {
      const res = await this.fetchFn(GITHUB_API + '/user/keys', { method: 'POST', redirect: 'manual', headers, body: JSON.stringify({ title: 'agent-hub shared git key', key: key.public_key }), signal: AbortSignal.timeout(TIMEOUT_MS) });
      const body: any = await res.json().catch(() => null);
      const already = res.status === 422 && JSON.stringify(body?.errors ?? '').includes('already in use');
      if (res.status === 201 || already) outcome = 'registered';
      else if (res.status === 401) outcome = 'token_rejected';
      // GitHub answers 404 (not 403) when the token lacks admin:public_key/write:public_key.
      else if (res.status === 403 || res.status === 404) outcome = 'missing_scope_admin_public_key';
      else outcome = 'github_http_' + res.status;
    } catch {}
    if (outcome === 'registered') {
      const meta = { ...JSON.parse(keyRow.meta), registered: true, registered_at: this.now() };
      this.sql.exec('UPDATE host_credentials SET meta=? WHERE kind=?', JSON.stringify(meta), 'git_ssh_key');
      // Hosts already hold this generation, so a plain poll would not deliver
      // it; force one sync so they install the now-accepted key.
      this.sql.exec('UPDATE host_credential_status SET resync_requested_at=?', this.now());
    }
    return reply({ status: outcome, fingerprint: JSON.parse(keyRow.meta).fingerprint ?? null }, outcome === 'registered' ? 200 : 502);
  }
}
