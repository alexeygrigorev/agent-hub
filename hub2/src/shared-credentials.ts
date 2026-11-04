import {credentialKey, sealCredential, openCredential} from './credential-crypto';
import {MeteredDb} from './db';
import {actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json} from './http';
import {decode64, encode64} from './auth';
import type {Actor, Hub2Env, JsonRecord} from './types';

/** Central authority for Hub-shared host credentials (intakes 54d5b703 + 0043dadb).
 *
 * Host-level credential kinds Hub distributes to every enrolled machine,
 * strictly apart from operator/human/worker credentials and from per-session
 * worker bindings: `gh` (GitHub CLI token + git https helper) and `git-ssh`
 * (one shared, additive git SSH keypair). Isolation rules:
 * - each kind owns an exact file namespace (`gh/...`, `git-ssh/...`); kinds are
 *   never merged with each other or with any other credential surface;
 * - the shared key is additive: hosts materialize only inside Hub-owned
 *   directories and never touch a machine's own keys;
 * - errors carry fixed reason codes; content never appears in acks, status or
 *   this module's errors, and non-admin callers never receive it.
 *
 * Machine actors use the pairing-granted `machine:heartbeat` scope (present on
 * every enrolled machine, so no re-pairing is needed); publishes, status and
 * GitHub registration require `operator:admin`.
 */
export const SHARED_CREDENTIAL_SCHEMA = [
  'CREATE TABLE shared_credentials(kind TEXT PRIMARY KEY,generation INTEGER NOT NULL,sealed TEXT NOT NULL,status TEXT NOT NULL,status_reason TEXT,updated_by TEXT NOT NULL,updated_at INTEGER NOT NULL) WITHOUT ROWID',
  'CREATE TABLE shared_credential_acks(machine_id TEXT NOT NULL,kind TEXT NOT NULL,generation INTEGER NOT NULL,gh_auth_json TEXT,acked_at INTEGER NOT NULL,PRIMARY KEY(machine_id,kind)) WITHOUT ROWID',
  'CREATE TABLE shared_credential_rejections(machine_id TEXT NOT NULL,kind TEXT NOT NULL,generation INTEGER NOT NULL,reason TEXT NOT NULL,observed_at INTEGER NOT NULL,PRIMARY KEY(machine_id,kind)) WITHOUT ROWID',
];

/** v1 -> v2: introduce the shared-credential tables on already-initialized DOs. */
export const SHARED_CREDENTIAL_MIGRATIONS: string[][] = [
  SHARED_CREDENTIAL_SCHEMA.map(statement => statement.replace(/^CREATE TABLE /, 'CREATE TABLE IF NOT EXISTS ')),
];

export type SharedCredentialKind = 'gh' | 'git-ssh';
export const SHARED_CREDENTIAL_KINDS: SharedCredentialKind[] = ['gh', 'git-ssh'];

/** Exact file allowlist per kind; anything else is refused (kind isolation). */
export const SHARED_CREDENTIAL_PATHS: Record<SharedCredentialKind, string[]> = {
  gh: ['gh/hosts.yml'],
  'git-ssh': ['git-ssh/id_ed25519', 'git-ssh/id_ed25519.pub'],
};

const MAX_FILE = 16000;
const TIMEOUT_MS = 15000;
const GITHUB_API = 'https://api.github.com';
const KEY_TITLE = 'agent-hub-shared-git';
const MACHINE_SCOPE = 'machine:heartbeat';

type Files = Record<string, string>;
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;
type SealedRow = {kind: SharedCredentialKind; generation: number; sealed: string; status: string; status_reason: string | null; updated_by: string; updated_at: number};

export function isSharedCredentialKind(value: unknown): value is SharedCredentialKind {
  return typeof value === 'string' && (SHARED_CREDENTIAL_KINDS as string[]).includes(value);
}

/** Path-allowlisted, size-bounded, NUL-free files for one shared kind. */
export function validSharedFiles(kind: SharedCredentialKind, files: unknown): files is Files {
  if (!files || typeof files !== 'object' || Array.isArray(files)) return false;
  const allowed = new Set(SHARED_CREDENTIAL_PATHS[kind]);
  const entries = Object.entries(files as Record<string, unknown>);
  return entries.length > 0 && entries.every(([k, v]) => allowed.has(k)
    && typeof v === 'string' && v.length > 0 && v.length <= MAX_FILE && !v.includes('\0'));
}

/** A credential set a host could actually use: a gh https login for github.com, or a complete OpenSSH ed25519 pair. */
export function structurallyValidShared(kind: SharedCredentialKind, files: Files): boolean {
  if (kind === 'gh') {
    const hosts = files['gh/hosts.yml'];
    return /(^|\n)github\.com:\s*(\n|$)/.test(hosts) && /(^|\n)\s*oauth_token:\s*\S+/.test(hosts)
      && /(^|\n)\s*git_protocol:\s*https\s*(\n|$)/.test(hosts);
  }
  const priv = files['git-ssh/id_ed25519'];
  const pub = files['git-ssh/id_ed25519.pub'];
  return priv.startsWith('-----BEGIN OPENSSH PRIVATE KEY-----') && priv.trimEnd().endsWith('-----END OPENSSH PRIVATE KEY-----')
    && /^ssh-ed25519 [A-Za-z0-9+/=]+ \S{0,251}$/.test(pub.trim());
}

/** The gh OAuth token from the shared hosts.yml, for GitHub API calls only. Never logged. */
export function ghToken(files: Files): string | null {
  const match = /(^|\n)\s*oauth_token:\s*(\S+)/.exec(files['gh/hosts.yml'] ?? '');
  return match ? match[2] : null;
}

/** The public key blob (second field of the .pub line) — safe, non-secret. */
export function publicKeyBlob(files: Files): string | null {
  const match = /^ssh-ed25519 ([A-Za-z0-9+/=]+)/.exec((files['git-ssh/id_ed25519.pub'] ?? '').trim());
  return match ? match[1] : null;
}

export type GithubRegisterResult =
  | {ok: true; already_registered: boolean; key_id: number | null}
  | {ok: false; reason: 'gh_credential_missing' | 'key_missing' | 'github_unauthorized' | 'github_blocked' | `github_http_${number}` | 'github_invalid_response' | 'github_unreachable'};

/** AC7 (54d5b703): register the shared public key on GitHub with the shared gh
 * token. Idempotent by key blob; the result carries a verdict and the provider
 * key id only — never tokens or key text. */
export async function githubRegisterPublicKey(files: Record<SharedCredentialKind, Files>, fetchFn: FetchLike): Promise<GithubRegisterResult> {
  const token = ghToken(files.gh ?? {});
  const blob = publicKeyBlob(files['git-ssh'] ?? {});
  if (typeof token !== 'string' || !token) return {ok: false, reason: 'gh_credential_missing'};
  if (!blob) return {ok: false, reason: 'key_missing'};
  const headers = {Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'agent-hub-shared-credentials/1'};
  try {
    const list = await fetchFn(GITHUB_API + '/user/keys?per_page=100', {headers, signal: AbortSignal.timeout(TIMEOUT_MS)});
    if (list.status === 401 || list.status === 403) return {ok: false, reason: 'github_unauthorized'};
    if (list.status !== 200) return {ok: false, reason: `github_http_${list.status}` as Extract<GithubRegisterResult, {ok: false}>['reason']};
    let keys: unknown = null;
    try { keys = JSON.parse((await list.text()).slice(0, 262144)); } catch { keys = null; }
    const existing = Array.isArray(keys)
      ? keys.find((k: JsonRecord) => !!k && typeof k.key === 'string' && String(k.key).trim().split(/\s+/)[1] === blob && Number.isInteger(k.id))
      : null;
    if (existing) return {ok: true, already_registered: true, key_id: existing.id as number};
    const created = await fetchFn(GITHUB_API + '/user/keys', {method: 'POST',
      headers: {...headers, 'Content-Type': 'application/json'}, body: JSON.stringify({title: KEY_TITLE, key: 'ssh-ed25519 ' + blob}),
      signal: AbortSignal.timeout(TIMEOUT_MS)});
    if (created.status === 401 || created.status === 403 || created.status === 422) {
      return {ok: false, reason: created.status === 422 ? 'github_invalid_response' : 'github_unauthorized'};
    }
    if (created.status !== 201) return {ok: false, reason: `github_http_${created.status}` as Extract<GithubRegisterResult, {ok: false}>['reason']};
    let body: JsonRecord | null = null;
    try { body = JSON.parse((await created.text()).slice(0, 65536)) as JsonRecord; } catch { body = null; }
    return {ok: true, already_registered: false, key_id: Number.isInteger(body?.id) ? body!.id as number : null};
  } catch {
    return {ok: false, reason: 'github_unreachable'};
  }
}

/** Bounded, non-secret gh auth status a host may report (AC3). */
export type GhAuthReport = {installed: boolean; logged_in: boolean; account: string | null; scopes: string[] | null; checked_at: string};

export function validGhAuthReport(value: unknown): value is GhAuthReport {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const report = value as JsonRecord;
  if (typeof report.installed !== 'boolean' || typeof report.logged_in !== 'boolean') return false;
  if (report.account !== null && (typeof report.account !== 'string' || !/^[A-Za-z0-9-]{1,100}$/.test(report.account))) return false;
  if (report.scopes !== null && (!Array.isArray(report.scopes) || report.scopes.length > 16
    || !report.scopes.every(scope => typeof scope === 'string' && /^[a-z0-9:_-]{1,64}$/.test(scope)))) return false;
  return typeof report.checked_at === 'string' && report.checked_at.length <= 32 && !Number.isNaN(Date.parse(report.checked_at));
}

export class SharedCredentials {
  private key?: Promise<CryptoKey>;
  constructor(
    private ctx: DurableObjectState,
    private db: MeteredDb,
    private env: Hub2Env,
    private readonly fetchFn: FetchLike = (input, init) => fetch(input, init),
  ) {}

  /** Routes under /api/shared-credentials; undefined when the path is not ours. */
  async fetch(request: Request): Promise<Response | undefined> {
    const path = new URL(request.url).pathname;
    if (path !== '/api/shared-credentials' && !path.startsWith('/api/shared-credentials/')) return undefined;
    const actor = actorFrom(request);
    if (!actor) return error('forbidden', 403);
    const admin = hasScope(actor, 'operator:admin');

    if (path === '/api/shared-credentials' && request.method === 'GET') return this.distribute(actor);
    if (path === '/api/shared-credentials/status' && request.method === 'GET') {
      if (!admin) return error('forbidden', 403);
      return json({
        credentials: this.db.all('SELECT kind,generation,status,status_reason,updated_by,updated_at FROM shared_credentials ORDER BY kind'),
        acks: this.db.all('SELECT machine_id,kind,generation,gh_auth_json,acked_at FROM shared_credential_acks ORDER BY machine_id,kind')
          .map((row: JsonRecord) => {
            try { return {...row, gh_auth: row.gh_auth_json === null ? null : JSON.parse(String(row.gh_auth_json))}; }
            catch { return {...row, gh_auth: null}; }
          }),
        rejections: this.db.all('SELECT machine_id,kind,generation,reason,observed_at FROM shared_credential_rejections ORDER BY kind,machine_id'),
      });
    }
    const actionMatch = path.match(/^\/api\/shared-credentials\/(gh|git-ssh)\/(ack|rejection)$/);
    if (actionMatch) return actionMatch[2] === 'ack' ? this.ack(request, actor, actionMatch[1] as SharedCredentialKind) : this.reject(request, actor, actionMatch[1] as SharedCredentialKind);
    if (path === '/api/shared-credentials/git-ssh/register-github' && request.method === 'POST') {
      if (!admin) return error('forbidden', 403);
      return this.registerGithub();
    }
    const kindMatch = path.match(/^\/api\/shared-credentials\/(gh|git-ssh)$/);
    if (kindMatch && request.method === 'POST') {
      if (!admin) return error('forbidden', 403);
      return this.publish(request, kindMatch[1] as SharedCredentialKind);
    }
    return error('not_found', 404);
  }

  private machine(actor: Actor): string | undefined {
    return actor.kind === 'machine' && actor.resource_id && hasScope(actor, MACHINE_SCOPE) ? actor.resource_id : undefined;
  }

  private async crypt(kind: SharedCredentialKind, generation: number, files: Files, open = false): Promise<string> {
    if (!this.key) {
      const raw = this.env.HUB2_CREDENTIAL_KEY;
      if (!raw || !/^[0-9a-f]{64}$/i.test(raw)) throw new HttpError('credential_key_unavailable', 503);
      this.key = credentialKey(raw);
    }
    const aad = new TextEncoder().encode(JSON.stringify(['hub2-shared-credential', kind, generation]));
    if (open) {
      const [iv, cipher] = files['sealed'].split('.');
      return new TextDecoder().decode(await openCredential(await this.key, decode64(iv), decode64(cipher), aad));
    }
    const sealed = await sealCredential(await this.key, new TextEncoder().encode(JSON.stringify({files})), aad);
    return encode64(sealed.iv) + '.' + encode64(sealed.data);
  }

  private row(kind: SharedCredentialKind): SealedRow | undefined {
    return this.db.one<SealedRow>('SELECT * FROM shared_credentials WHERE kind=?', kind) as SealedRow | undefined;
  }

  private async files(kind: SharedCredentialKind, row: SealedRow): Promise<Files> {
    return (JSON.parse(await this.crypt(kind, row.generation, {sealed: row.sealed}, true)) as {files: Files}).files;
  }

  /** Host fetch: current generations with files, over the machine-authenticated channel. */
  private async distribute(actor: Actor): Promise<Response> {
    if (!this.machine(actor)) return error('forbidden', 403);
    const kinds: JsonRecord = {};
    try {
      for (const kind of SHARED_CREDENTIAL_KINDS) {
        const row = this.row(kind);
        if (row) kinds[kind] = {generation: row.generation, status: row.status, files: await this.files(kind, row)};
      }
    } catch (caught) {
      if (caught instanceof HttpError) throw caught;
      throw new HttpError('credential_key_unavailable', 503);
    }
    return json({kinds}, 200, {'Cache-Control': 'private, no-store'});
  }

  /** Publish the next generation (operator). Content is never echoed back. */
  private async publish(request: Request, kind: SharedCredentialKind): Promise<Response> {
    const body = await bodyObject(request, 131072);
    if (!validSharedFiles(kind, body.files) || !structurallyValidShared(kind, body.files as Files)) return error('invalid_credential', 400);
    const prior = this.row(kind);
    const generation = (prior?.generation ?? 0) + 1;
    if (body.base_generation !== undefined
      && (!Number.isSafeInteger(body.base_generation) || (body.base_generation as number) < 0)) return error('invalid_credential', 400);
    if (prior && body.base_generation !== undefined && body.base_generation !== prior.generation) {
      return json({error: 'generation_changed', generation: prior.generation}, 409);
    }
    let sealed: string;
    try { sealed = await this.crypt(kind, generation, body.files as Files); }
    catch (caught) { if (caught instanceof HttpError) throw caught; throw new HttpError('credential_key_unavailable', 503); }
    return this.db.transaction(this.ctx, () => {
      const current = this.row(kind);
      if ((current?.generation ?? null) !== (prior?.generation ?? null)) return json({error: 'generation_changed', generation: current?.generation ?? 0}, 409);
      this.db.run('INSERT INTO shared_credentials(kind,generation,sealed,status,status_reason,updated_by,updated_at) VALUES (?,?,?,?,?,?,?) '
        + 'ON CONFLICT(kind) DO UPDATE SET generation=excluded.generation,sealed=excluded.sealed,status=\'ok\',status_reason=NULL,updated_by=excluded.updated_by,updated_at=excluded.updated_at',
      kind, generation, sealed, 'ok', null, actorFrom(request)!.id, Date.now());
      // A new generation supersedes every rejection of the kind.
      this.db.run('DELETE FROM shared_credential_rejections WHERE kind=? AND generation<=?', kind, generation);
      return json({status: 'published', kind, generation});
    });
  }

  /** Host acknowledges a materialized generation and reports gh auth status (AC2/AC3). */
  private async ack(request: Request, actor: Actor, kind: SharedCredentialKind): Promise<Response> {
    const machine = this.machine(actor);
    if (!machine) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (!Number.isSafeInteger(body.generation) || (body.generation as number) < 1
      || (body.gh_auth !== undefined && !validGhAuthReport(body.gh_auth))) return error('invalid_ack', 400);
    if (!this.row(kind)) return error('not_found', 404);
    this.db.transaction(this.ctx, () => {
      this.db.run('INSERT INTO shared_credential_acks(machine_id,kind,generation,gh_auth_json,acked_at) VALUES (?,?,?,?,?) '
        + 'ON CONFLICT(machine_id,kind) DO UPDATE SET generation=excluded.generation,gh_auth_json=excluded.gh_auth_json,acked_at=excluded.acked_at',
      machine, kind, body.generation, body.gh_auth === undefined ? null : JSON.stringify(body.gh_auth), Date.now());
    });
    return json({status: 'acked', kind, generation: body.generation, current_generation: this.row(kind)!.generation});
  }

  /** A host reports the current generation of a shared credential failed on it (AC4). */
  private async reject(request: Request, actor: Actor, kind: SharedCredentialKind): Promise<Response> {
    const machine = this.machine(actor);
    if (!machine) return error('forbidden', 403);
    if (request.method !== 'POST') return error('method_not_allowed', 405);
    const body = await bodyObject(request, 4096);
    const row = this.row(kind);
    if (!row) return error('not_found', 404);
    if (!Number.isSafeInteger(body.generation) || typeof body.reason !== 'string' || !/^[a-z0-9_]{1,64}$/.test(body.reason)) {
      return error('invalid_rejection', 400);
    }
    if (body.generation !== row.generation) return json({status: 'superseded', generation: row.generation});
    this.db.transaction(this.ctx, () => {
      this.db.run('INSERT INTO shared_credential_rejections(machine_id,kind,generation,reason,observed_at) VALUES (?,?,?,?,?) '
        + 'ON CONFLICT(machine_id,kind) DO UPDATE SET generation=excluded.generation,reason=excluded.reason,observed_at=excluded.observed_at',
      machine, kind, row.generation, body.reason, Date.now());
    });
    return json({status: 'recorded', generation: row.generation});
  }

  /** AC7: register the stored public key on GitHub using the stored gh token. */
  private async registerGithub(): Promise<Response> {
    const ghRow = this.row('gh');
    const keyRow = this.row('git-ssh');
    if (!ghRow || !keyRow) return error('not_found', 404);
    let files: Record<SharedCredentialKind, Files>;
    try {
      files = {gh: await this.files('gh', ghRow), 'git-ssh': await this.files('git-ssh', keyRow)};
    } catch (caught) {
      if (caught instanceof HttpError) throw caught;
      throw new HttpError('credential_key_unavailable', 503);
    }
    const result = await githubRegisterPublicKey(files, this.fetchFn);
    if (!result.ok) {
      this.db.run('UPDATE shared_credentials SET status_reason=? WHERE kind=? AND status=\'ok\'', result.reason, 'git-ssh');
      return error(result.reason, result.reason === 'github_unreachable' ? 502 : 400);
    }
    this.db.run('UPDATE shared_credentials SET status_reason=NULL WHERE kind=?', 'git-ssh');
    return json({status: 'registered', already_registered: result.already_registered, ...(result.key_id !== null ? {key_id: result.key_id} : {})});
  }
}
