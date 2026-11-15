/** Shared retained credential-refresh contract. No storage/database dependency.
 * Legacy credentials reexports this same implementation; Hub2 tests do not boot
 * the retired root worker to retain its refresh and OAuth export contracts.
 */
import {CLAUDE_OAUTH} from '../src/claude-oauth';
export {CLAUDE_OAUTH, claudeAuthorization, claudeExchange, loginRefusal} from '../src/claude-oauth';

export type CredentialFiles = Record<string, string>;
export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export const CODEX_OAUTH = {
  token: 'https://auth.openai.com/oauth/token',
  client: 'app_EMoamEEZ73f0CkXaXp7hrann',
  usage: 'https://chatgpt.com/backend-api/wham/usage',
  resetCredits: 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits',
};
export const TIMEOUT_MS = 15000;

export const parse = (text: string | undefined): any => { try { const v = JSON.parse(text ?? ''); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };

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
