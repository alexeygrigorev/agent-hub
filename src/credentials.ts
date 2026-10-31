/**
 * Hub-owned engine credentials (intake 5814e109).
 *
 * Hub is the single source of truth for every managed engine login. The
 * imported bundle is an immutable install snapshot; this store holds the
 * CURRENT credential files per account label with a monotonically increasing
 * generation. Hub refreshes OAuth tokens centrally (one refresh owner per
 * token family), reads quota directly from provider APIs with the stored
 * credential, and hosts materialize the current generation at sync/launch.
 * A host whose engine still rotated a token locally writes it back with a
 * compare-and-set on the generation, so no copy is ever silently stale.
 *
 * Secrets never leave this module except sealed, or to an approved host.
 * Errors and logs carry fixed reason codes only, never provider bodies.
 */
import type { SqlStorage } from '@cloudflare/workers-types';

export type CredentialFiles = Record<string, string>;
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** Rotatable/refreshable credential files per provider, relative to the account HOME. */
export const CREDENTIAL_PATHS: Record<string, string[]> = {
  codex: ['.codex/auth.json'],
  claude: ['.claude/.credentials.json'],
  zai: ['.zcode/v2/credentials.json'],
  gemini: ['.gemini/oauth_creds.json'],
  go: [],
};

export const CODEX_OAUTH = {
  token: 'https://auth.openai.com/oauth/token',
  client: 'app_EMoamEEZ73f0CkXaXp7hrann',
  usage: 'https://chatgpt.com/backend-api/wham/usage',
};
export const CLAUDE_OAUTH = {
  authorize: 'https://claude.com/cai/oauth/authorize',
  token: 'https://platform.claude.com/v1/oauth/token',
  redirect: 'https://platform.claude.com/oauth/code/callback',
  client: '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
  scopes: ['user:profile', 'user:inference', 'user:sessions:claude_code', 'user:mcp_servers', 'user:file_upload'],
  usage: 'https://api.anthropic.com/api/oauth/usage',
  profile: 'https://api.anthropic.com/api/oauth/profile',
};

const DAY = 86400000;
const HOUR = 3600000;
// Codex CLI refreshes on its own once last_refresh is 8 days old; Hub refreshes
// well before so hosts never need to. Claude Code refreshes ~5 minutes before
// expiry; Hub keeps at least three hours of validity on every host copy.
export const CODEX_REFRESH_AGE_MS = 3 * DAY;
export const CODEX_MIN_VALIDITY_MS = 3 * DAY;
export const CLAUDE_MIN_VALIDITY_MS = 3 * HOUR;
const MAX_FILE = 16000;
const TIMEOUT_MS = 15000;

const parse = (text: string | undefined): any => { try { const v = JSON.parse(text ?? ''); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };

/** JWT `exp` in ms, or null. Decodes the public claims only; nothing is verified or logged. */
export function jwtExpiryMs(token: unknown): number | null {
  if (typeof token !== 'string') return null;
  const part = token.split('.')[1];
  if (!part) return null;
  try {
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(part.length / 4) * 4, '='));
    const exp = JSON.parse(json)?.exp;
    return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : null;
  } catch { return null; }
}

/** The credential subset of a bundle's files for its provider. */
export function credentialSubset(provider: string, files: CredentialFiles | undefined): CredentialFiles {
  const out: CredentialFiles = {};
  for (const path of CREDENTIAL_PATHS[provider] ?? []) if (typeof files?.[path] === 'string') out[path] = files[path];
  return out;
}

/**
 * Provider-specific issuance time of a credential set (ms), used to order two
 * divergent copies: the copy whose tokens were issued later is the live one.
 * Null when the provider has no meaningful ordering.
 */
export function credentialFreshness(provider: string, files: CredentialFiles): number | null {
  if (provider === 'codex') {
    const auth = parse(files['.codex/auth.json']);
    const t = Date.parse(auth?.last_refresh);
    return Number.isFinite(t) ? t : null;
  }
  if (provider === 'claude') {
    const exp = parse(files['.claude/.credentials.json'])?.claudeAiOauth?.expiresAt;
    return typeof exp === 'number' && Number.isFinite(exp) ? exp : null;
  }
  return null;
}

/** Access-token expiry (ms) where the provider exposes it. */
export function accessExpiry(provider: string, files: CredentialFiles): number | null {
  if (provider === 'codex') return jwtExpiryMs(parse(files['.codex/auth.json'])?.tokens?.access_token);
  if (provider === 'claude') {
    const exp = parse(files['.claude/.credentials.json'])?.claudeAiOauth?.expiresAt;
    return typeof exp === 'number' && Number.isFinite(exp) ? exp : null;
  }
  return null;
}

export function refreshable(provider: string, files: CredentialFiles): boolean {
  if (provider === 'codex') return typeof parse(files['.codex/auth.json'])?.tokens?.refresh_token === 'string';
  if (provider === 'claude') return typeof parse(files['.claude/.credentials.json'])?.claudeAiOauth?.refreshToken === 'string';
  return false;
}

export function needsRefresh(provider: string, files: CredentialFiles, nowMs: number): boolean {
  if (!refreshable(provider, files)) return false;
  const exp = accessExpiry(provider, files);
  if (provider === 'codex') {
    const fresh = credentialFreshness(provider, files);
    return fresh === null || nowMs - fresh >= CODEX_REFRESH_AGE_MS || exp === null || exp - nowMs <= CODEX_MIN_VALIDITY_MS;
  }
  return exp === null || exp - nowMs <= CLAUDE_MIN_VALIDITY_MS;
}

export type RefreshResult = { ok: true; files: CredentialFiles } | { ok: false; invalid: boolean; reason: string; providerStatus?: number; detail?: string; blocked?: boolean };

const INVALID_GRANT = /invalid_grant|refresh_token_(?:expired|reused|invalidated)|invalid_refresh_token|token_revoked/;

async function postJson(fetchFn: FetchLike, url: string, body: unknown, extra: Record<string, string> = {}) {
  const res = await fetchFn(url, { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...extra },
    body: JSON.stringify(body), signal: AbortSignal.timeout(TIMEOUT_MS) });
  const text = (await res.text()).slice(0, 65536);
  return { status: res.status, json: parse(text), text };
}

/** Classify a failed token response without retaining its body. */
function refusal(status: number, text: string): { ok: false; invalid: boolean; reason: string } {
  // Only an OAuth error document from the provider is a verdict about the
  // credential. An HTML challenge or bare 403 is egress blocking (Cloudflare
  // Worker IPs are bot-screened by some providers): delegate, never invalidate.
  const body = parse(text);
  const oauthError = body && typeof body.error === 'string' ? body.error + ' ' + (typeof body.error_description === 'string' ? body.error_description : '') : null;
  if (oauthError && (status === 400 || status === 401 || status === 403) && INVALID_GRANT.test(oauthError)) return { ok: false, invalid: true, reason: 'refresh_token_rejected' };
  if (oauthError && status === 401) return { ok: false, invalid: true, reason: 'refresh_unauthorized' };
  if (!oauthError && (status === 401 || status === 403 || status === 429)) return { ok: false, invalid: false, reason: 'refresh_blocked' };
  return { ok: false, invalid: false, reason: status >= 500 ? 'refresh_provider_unavailable' : 'refresh_http_' + status };
}

/** Exchange the stored refresh token for new tokens. Never returns provider bodies. */
export async function refreshCredential(provider: string, files: CredentialFiles, fetchFn: FetchLike, nowMs: number): Promise<RefreshResult> {
  try {
    if (provider === 'codex') {
      const auth = parse(files['.codex/auth.json']);
      const r = await postJson(fetchFn, CODEX_OAUTH.token, { client_id: CODEX_OAUTH.client, grant_type: 'refresh_token', refresh_token: auth.tokens.refresh_token, scope: 'openid profile email' });
      if (r.status !== 200 || typeof r.json?.access_token !== 'string') return r.status === 200 ? { ok: false, invalid: false, reason: 'refresh_invalid_response' } : refusal(r.status, r.text);
      const tokens = { ...auth.tokens, access_token: r.json.access_token,
        ...(typeof r.json.id_token === 'string' ? { id_token: r.json.id_token } : {}),
        ...(typeof r.json.refresh_token === 'string' ? { refresh_token: r.json.refresh_token } : {}) };
      return { ok: true, files: { ...files, '.codex/auth.json': JSON.stringify({ ...auth, tokens, last_refresh: new Date(nowMs).toISOString() }, null, 2) } };
    }
    if (provider === 'claude') {
      const creds = parse(files['.claude/.credentials.json']);
      const oauth = creds.claudeAiOauth;
      const scopes = Array.isArray(oauth.scopes) && oauth.scopes.length ? oauth.scopes : CLAUDE_OAUTH.scopes;
      const r = await postJson(fetchFn, CLAUDE_OAUTH.token, { grant_type: 'refresh_token', refresh_token: oauth.refreshToken, client_id: CLAUDE_OAUTH.client, scope: scopes.join(' ') });
      if (r.status !== 200 || typeof r.json?.access_token !== 'string') return r.status === 200 ? { ok: false, invalid: false, reason: 'refresh_invalid_response' } : refusal(r.status, r.text);
      return { ok: true, files: { ...files, '.claude/.credentials.json': JSON.stringify({ ...creds, claudeAiOauth: claudeOauthFrom(r.json, oauth, nowMs) }) } };
    }
    return { ok: false, invalid: false, reason: 'refresh_unsupported' };
  } catch {
    return { ok: false, invalid: false, reason: 'refresh_unreachable' };
  }
}

function claudeOauthFrom(token: any, prior: any, nowMs: number) {
  return { ...prior, accessToken: token.access_token,
    ...(typeof token.refresh_token === 'string' ? { refreshToken: token.refresh_token } : {}),
    ...(typeof token.expires_in === 'number' ? { expiresAt: Math.floor(nowMs + token.expires_in * 1000) } : {}),
    ...(typeof token.scope === 'string' ? { scopes: token.scope.split(/\s+/).filter(Boolean) } : {}) };
}

// ---------- Hub-central Claude login (PKCE, paste-code) ----------

const b64url = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export async function claudeAuthorization(random: (n: number) => Uint8Array = n => crypto.getRandomValues(new Uint8Array(n))) {
  const verifier = b64url(random(32));
  const state = b64url(random(32));
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))));
  const url = new URL(CLAUDE_OAUTH.authorize);
  for (const [k, v] of Object.entries({ code: 'true', client_id: CLAUDE_OAUTH.client, response_type: 'code', redirect_uri: CLAUDE_OAUTH.redirect,
    scope: CLAUDE_OAUTH.scopes.join(' '), code_challenge: challenge, code_challenge_method: 'S256', state })) url.searchParams.set(k, v);
  return { url: url.toString(), verifier, state };
}

/** Exchange a pasted `code#state` for a fresh, Hub-owned Claude credential file. */
export async function claudeExchange(pasted: string, verifier: string, state: string, fetchFn: FetchLike, nowMs: number): Promise<RefreshResult> {
  const [code, returnedState] = pasted.trim().split('#');
  if (!code || (returnedState && returnedState !== state)) return { ok: false, invalid: true, reason: 'login_state_mismatch' };
  try {
    const r = await postJson(fetchFn, CLAUDE_OAUTH.token, { grant_type: 'authorization_code', code, state, redirect_uri: CLAUDE_OAUTH.redirect, client_id: CLAUDE_OAUTH.client, code_verifier: verifier });
    if (r.status !== 200 || typeof r.json?.access_token !== 'string') return r.status === 200 ? { ok: false, invalid: false, reason: 'login_invalid_response', providerStatus: 200, detail: 'invalid_token_response' } : loginRefusal(r.status, r.text);
    const oauth: any = claudeOauthFrom(r.json, { scopes: CLAUDE_OAUTH.scopes }, nowMs);
    try {
      const p = await fetchFn(CLAUDE_OAUTH.profile, { headers: { Authorization: 'Bearer ' + oauth.accessToken, 'anthropic-beta': 'oauth-2025-04-20' }, signal: AbortSignal.timeout(TIMEOUT_MS) });
      const profile = p.ok ? parse((await p.text()).slice(0, 65536)) : null;
      const org = profile?.organization ?? {};
      const sub = org.organization_type === 'claude_max' ? 'max' : org.organization_type === 'claude_pro' ? 'pro' : null;
      if (sub) oauth.subscriptionType = sub;
      if (typeof org.rate_limit_tier === 'string') oauth.rateLimitTier = org.rate_limit_tier;
    } catch {}
    return { ok: true, files: { '.claude/.credentials.json': JSON.stringify({ claudeAiOauth: oauth }) } };
  } catch {
    // Worker egress to the provider failed outright: the code was never seen
    // by the provider, so the CLI host may still exchange it (I8 fallback).
    return { ok: false, invalid: false, reason: 'login_unreachable', detail: 'egress_unreachable', blocked: true };
  }
}

const OAUTH_SLUG = /^[a-z][a-z0-9_.-]{0,63}$/;

/**
 * Classify a refused authorization-code exchange without retaining its body:
 * the provider HTTP status plus a sanitized reason (the OAuth `error` slug, or
 * a fixed label). A bot challenge (no OAuth error document on 401/403/429) is
 * Worker egress blocking, which the CLI host can bypass with its own exchange.
 */
export function loginRefusal(status: number, text: string): { ok: false; invalid: boolean; reason: string; providerStatus: number; detail: string; blocked: boolean } {
  const body = parse(text);
  const oauth = body && typeof body.error === 'string' && OAUTH_SLUG.test(body.error) ? body.error : null;
  const blocked = !oauth && (status === 401 || status === 403 || status === 429);
  const detail = oauth ?? (blocked ? 'egress_blocked' : status >= 500 ? 'provider_unavailable' : 'http_' + status);
  return { ok: false, invalid: !!oauth, reason: 'login_code_rejected', providerStatus: status, detail, blocked };
}

// ---------- Direct provider quota (no host, no quse) ----------

export type QuotaWindow = { percent_remaining: number | null; reset_at: string | null; present: boolean; rolling: boolean };
export type QuotaRoute = { status: 'ok' | 'error' | 'unknown'; error: 'unavailable' | null; observed_at: string | null; details: { limit_reached: boolean | null }; windows: Record<string, QuotaWindow> };
export type QuotaResult = { ok: true; quota: QuotaRoute } | { ok: false; unauthorized: boolean; reason: string };

const pct = (used: unknown) => typeof used === 'number' && Number.isFinite(used) ? Math.max(0, Math.min(100, 100 - used)) : null;
function isoFrom(value: unknown): string | null {
  if (typeof value === 'number' && Number.isFinite(value)) return new Date(value > 1e12 ? value : value * 1000).toISOString();
  if (typeof value === 'string') { const t = Date.parse(value); return Number.isFinite(t) ? new Date(t).toISOString() : null; }
  return null;
}
function spanLabel(seconds: unknown, fallback: string) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return fallback;
  if (seconds === 18000) return '5h';
  if (seconds === 604800) return '7d';
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  return `${seconds}s`;
}

/** Map a ChatGPT `wham/usage` body to the Hub quota route shape. */
export function codexQuotaFrom(body: any, nowMs: number): QuotaRoute {
  const rl = body?.rate_limit && typeof body.rate_limit === 'object' ? body.rate_limit : {};
  const present = [rl.primary_window, rl.secondary_window].filter((w: any) => w && typeof w === 'object');
  const windows: Record<string, QuotaWindow> = { '5h': { percent_remaining: null, reset_at: null, present: false, rolling: false }, '7d': { percent_remaining: null, reset_at: null, present: false, rolling: false } };
  // One active window is the weekly one; with two, the short window comes first.
  present.forEach((w: any, i: number) => {
    const name = spanLabel(w.limit_window_seconds, present.length === 1 || i === 1 ? '7d' : '5h');
    windows[name] = { percent_remaining: pct(w.used_percent), reset_at: isoFrom(w.reset_at), present: true, rolling: false };
  });
  const weekly = windows['7d'];
  const limit = rl.limit_reached === true || (weekly.present && weekly.percent_remaining === 0);
  return { status: 'ok', error: null, observed_at: new Date(nowMs).toISOString(), details: { limit_reached: limit }, windows };
}

/** Map an Anthropic `oauth/usage` body to the Hub quota route shape. */
export function claudeQuotaFrom(body: any, nowMs: number): QuotaRoute {
  const win = (w: any): QuotaWindow => w && typeof w === 'object'
    ? { percent_remaining: pct(w.utilization ?? 0), reset_at: isoFrom(w.resets_at), present: true, rolling: false }
    : { percent_remaining: null, reset_at: null, present: false, rolling: false };
  const windows = { '5h': win(body?.five_hour), '7d': win(body?.seven_day) };
  const limit = [windows['5h'], windows['7d']].some(w => w.present && w.percent_remaining === 0);
  return { status: 'ok', error: null, observed_at: new Date(nowMs).toISOString(), details: { limit_reached: limit }, windows };
}

export async function fetchQuota(provider: string, files: CredentialFiles, fetchFn: FetchLike, nowMs: number): Promise<QuotaResult> {
  try {
    let res: Response;
    if (provider === 'codex') {
      const auth = parse(files['.codex/auth.json']);
      const token = auth?.tokens?.access_token;
      if (typeof token !== 'string') return { ok: false, unauthorized: true, reason: 'credential_missing' };
      res = await fetchFn(CODEX_OAUTH.usage, { redirect: 'manual', headers: { Authorization: 'Bearer ' + token, Accept: 'application/json',
        ...(typeof auth.tokens.account_id === 'string' ? { 'ChatGPT-Account-Id': auth.tokens.account_id } : {}), 'User-Agent': 'codex_cli_rs/0.162.1', originator: 'codex_cli_rs' }, signal: AbortSignal.timeout(TIMEOUT_MS) });
    } else if (provider === 'claude') {
      const token = parse(files['.claude/.credentials.json'])?.claudeAiOauth?.accessToken;
      if (typeof token !== 'string') return { ok: false, unauthorized: true, reason: 'credential_missing' };
      res = await fetchFn(CLAUDE_OAUTH.usage, { redirect: 'manual', headers: { Authorization: 'Bearer ' + token, Accept: 'application/json', 'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'Claude-Code/2.1.198' }, signal: AbortSignal.timeout(TIMEOUT_MS) });
    } else {
      return { ok: false, unauthorized: false, reason: 'quota_unsupported' };
    }
    const text = (await res.text()).slice(0, 131072);
    if (res.status === 401 && parse(text)) return { ok: false, unauthorized: true, reason: 'quota_unauthorized' };
    if (res.status === 401 || res.status === 403 || res.status === 429) return { ok: false, unauthorized: false, reason: 'quota_blocked' };
    if (res.status !== 200) return { ok: false, unauthorized: false, reason: 'quota_http_' + res.status };
    const body = parse(text);
    if (!body) return { ok: false, unauthorized: false, reason: 'quota_invalid_response' };
    return { ok: true, quota: provider === 'codex' ? codexQuotaFrom(body, nowMs) : claudeQuotaFrom(body, nowMs) };
  } catch {
    return { ok: false, unauthorized: false, reason: 'quota_unreachable' };
  }
}

// ---------- Store ----------

export type CredentialRow = { label: string; provider: string; generation: number; ciphertext: string; freshness_ms: number | null;
  access_expires_ms: number | null; status: string; status_reason: string | null; updated_by: string; updated_at: number;
  refresh_attempt_at: number | null; quota_attempt_at: number | null };

export const CREDENTIAL_SCHEMA = [
  'CREATE TABLE IF NOT EXISTS engine_credentials (label TEXT PRIMARY KEY, provider TEXT NOT NULL, generation INTEGER NOT NULL, ciphertext TEXT NOT NULL, freshness_ms INTEGER, access_expires_ms INTEGER, status TEXT NOT NULL, status_reason TEXT, updated_by TEXT NOT NULL, updated_at INTEGER NOT NULL, refresh_attempt_at INTEGER, quota_attempt_at INTEGER)',
  // A host that reported a credential as rejected is never offered that
  // account again until Hub publishes a newer credential generation (I5).
  'CREATE TABLE IF NOT EXISTS engine_credential_rejections (machine_id TEXT NOT NULL, label TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT NOT NULL, observed_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))',
  // When Hub's own egress to a provider is blocked, exactly one host at a time
  // holds the refresh for a label and writes the rotation back by CAS.
  'CREATE TABLE IF NOT EXISTS engine_credential_leases (label TEXT PRIMARY KEY, machine_id TEXT NOT NULL, generation INTEGER NOT NULL, expires_at INTEGER NOT NULL)',
  'CREATE TABLE IF NOT EXISTS engine_credential_logins (id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL, sealed TEXT NOT NULL, expires_at INTEGER NOT NULL)',
];

export function validCredentialFiles(provider: string, files: unknown): files is CredentialFiles {
  if (!files || typeof files !== 'object' || Array.isArray(files)) return false;
  const allowed = new Set(CREDENTIAL_PATHS[provider] ?? []);
  const entries = Object.entries(files as Record<string, unknown>);
  return entries.length > 0 && entries.every(([k, v]) => allowed.has(k) && typeof v === 'string' && v.length > 0 && v.length <= MAX_FILE && !v.includes('\0'));
}

/**
 * A credential set that an engine could actually use: torn, emptied or
 * logged-out files are never accepted as a new generation.
 */
export function structurallyValid(provider: string, files: CredentialFiles): boolean {
  for (const [path, text] of Object.entries(files)) {
    if (path.endsWith('.json') && parse(text) === null) return false;
  }
  if (provider === 'codex') {
    const t = parse(files['.codex/auth.json'])?.tokens;
    return typeof t?.access_token === 'string' && typeof t?.refresh_token === 'string';
  }
  if (provider === 'claude') return typeof parse(files['.claude/.credentials.json'])?.claudeAiOauth?.accessToken === 'string';
  return true;
}

/** Rejected (machine,label) pairs that still apply to the current generation. */
export function rejectedPairs(sql: SqlStorage): Set<string> {
  try {
    return new Set(sql.exec('SELECT r.machine_id, r.label FROM engine_credential_rejections r JOIN engine_credentials c ON c.label=r.label AND c.generation=r.generation').toArray()
      .map((r: any) => r.machine_id + '\u0000' + r.label));
  } catch { return new Set(); }
}

/** Account labels whose Hub-held credential is known invalid (re-auth required). */
export function invalidLabels(sql: SqlStorage): Set<string> {
  try { return new Set(sql.exec("SELECT label FROM engine_credentials WHERE status='invalid'").toArray().map((r: any) => r.label)); }
  catch { return new Set(); }
}

/** Record that `machine` saw the current generation of `label` fail authentication. */
export function recordCredentialRejection(sql: SqlStorage, machine: string, label: string, reason: string, nowMs: number) {
  try {
    const row: any = sql.exec('SELECT generation FROM engine_credentials WHERE label=?', label).toArray()[0];
    if (!row) return;
    sql.exec('INSERT OR REPLACE INTO engine_credential_rejections VALUES (?,?,?,?,?)', machine, label, row.generation, reason, nowMs);
    sql.exec('UPDATE engine_credentials SET refresh_attempt_at=NULL, quota_attempt_at=NULL WHERE label=?', label);
  } catch {}
}
