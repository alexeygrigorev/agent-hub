/** Maintained Hub2 Claude OAuth login. Legacy callers use compatibility exports. */
type CredentialFiles = Record<string, string>;
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;
type RefreshResult = { ok: true; files: CredentialFiles } | { ok: false; invalid: boolean; reason: string; providerStatus?: number; detail?: string; blocked?: boolean };

export const CLAUDE_OAUTH = {
  authorize: 'https://claude.com/cai/oauth/authorize',
  token: 'https://platform.claude.com/v1/oauth/token',
  redirect: 'https://platform.claude.com/oauth/code/callback',
  client: '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
  scopes: ['user:profile', 'user:inference', 'user:sessions:claude_code', 'user:mcp_servers', 'user:file_upload'],
  usage: 'https://api.anthropic.com/api/oauth/usage',
  profile: 'https://api.anthropic.com/api/oauth/profile',
};

const TIMEOUT_MS = 15000;
const parse = (text: string | undefined): any => { try { const v = JSON.parse(text ?? ''); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };

async function postJson(fetchFn: FetchLike, url: string, body: unknown, extra: Record<string, string> = {}) {
  const res = await fetchFn(url, { method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...extra },
    body: JSON.stringify(body), signal: AbortSignal.timeout(TIMEOUT_MS) });
  const text = (await res.text()).slice(0, 65536);
  return { status: res.status, json: parse(text), text };
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

