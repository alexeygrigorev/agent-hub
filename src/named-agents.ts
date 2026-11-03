/** Operator lifecycle for stable named messaging identities. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { CallerContext } from './bus';
import { registryAddress } from './agent_registry';
import { generateTokenPair } from './auth/token';
import {
  authorizedNamedAgent,
  ensureSessionIdentity,
  listDelegates,
  namedForwardSession,
  setNamedAgentForward,
  updateNamedAgentDelegates,
} from './session-messaging';

export { authorizedNamedAgent, namedForwardSession };

const FAMILY = /^[A-Za-z0-9._:-]{1,256}$/;
const SESSION = /^session:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;

/** Called after ordinary HTTP authentication. Tokens appear only in create's
 * no-store response so the CLI can atomically save them to a private profile. */
export async function handleNamedAgentRequest(
  sql: SqlStorage,
  transaction: <T>(f: () => T) => T,
  request: Request,
  caller: CallerContext,
): Promise<Response | null> {
  const match = /^\/api\/agents\/named\/([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:\/(create|delegate|forward))?$/.exec(new URL(request.url).pathname);
  if (!match) return null;
  const json = (value: unknown, status = 200) => Response.json(value, { status, headers: { 'Cache-Control': 'private, no-store' } });
  if (!caller.isAdmin) return json({ error: 'forbidden' }, 403);
  const [, name, action] = match;
  if (!registryAddress('agent:' + name)) return json({ error: 'invalid_agent' }, 400);
  const actor = 'agent:' + name;
  const get = () => sql.exec('SELECT * FROM named_agents WHERE name = ?', name).toArray()[0] as any;
  const activeCredentialFamily = () => {
    const row: any = sql.exec(`SELECT family_id FROM credentials WHERE registry_actor = ? AND kind = 'agent'
      AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at DESC LIMIT 1`, actor, new Date().toISOString()).toArray()[0];
    return row?.family_id == null ? null : String(row.family_id);
  };

  if (!action && request.method === 'GET') {
    const row = get();
    if (!row || row.disabled_at) return json({ error: 'not_found' }, 404);
    let delegates: string[] = [];
    try {
      const parsed = JSON.parse(row.delegates_json || '[]');
      if (Array.isArray(parsed)) delegates = parsed.filter((family: unknown): family is string => typeof family === 'string');
    } catch { /* malformed legacy data is shown as an empty grant list */ }
    return json({ actor, name, project: row.project ?? null, display: row.display, credential_family: row.credential_family ?? activeCredentialFamily(),
      delegates, forward_session_id: row.forward_session_id ?? null, created_by: row.created_by, created_at: row.created_at,
      disabled_at: row.disabled_at ?? null });
  }

  if (!action || request.method !== 'POST') return json({ error: 'method_not_allowed' }, 405);
  let body: any;
  try {
    const raw = await request.text();
    if (raw.length > 4096) throw new Error();
    body = JSON.parse(raw);
  } catch { return json({ error: 'invalid_body' }, 400); }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return json({ error: 'invalid_body' }, 400);

  if (action === 'create') {
    if (Object.keys(body).some(key => !['display', 'project'].includes(key)) ||
        (body.display !== undefined && (typeof body.display !== 'string' || !body.display.trim() || body.display.length > 128)) ||
        (body.project !== undefined && (typeof body.project !== 'string' || !registryAddress(body.project)))) {
      return json({ error: 'invalid_body' }, 400);
    }
    const tokens = await generateTokenPair();
    return transaction(() => {
      // A retry must never rotate the stable identity or revoke its existing token.
      const prior = get();
      if (prior?.credential_family || prior?.disabled_at || activeCredentialFamily()) return json({ error: 'agent_exists' }, 409);
      const now = new Date().toISOString();
      sql.exec("INSERT OR IGNORE INTO agents (tag, project, registered_at) VALUES (?, 'agent', ?)", name, now);
      const registry = get();
      if (registry?.credential_family || registry?.disabled_at || activeCredentialFamily()) return json({ error: 'agent_exists' }, 409);
      sql.exec(`INSERT INTO named_agents (name, project, display, credential_family, created_by, created_at)
        VALUES (?, ?, ?, NULL, ?, ?) ON CONFLICT(name) DO UPDATE SET project=COALESCE(excluded.project, named_agents.project),
        display=excluded.display WHERE named_agents.credential_family IS NULL AND named_agents.disabled_at IS NULL`,
        name, body.project ?? null, body.display?.trim() ?? registry?.display ?? name, caller.actor ?? 'admin', now);
      const family = crypto.randomUUID();
      const expires = new Date(Date.now() + 30 * 86400000).toISOString();
      const scopes = ['msg:send', 'msg:read'];
      sql.exec(`INSERT INTO credentials (id, kind, token_hash, refresh_hash, family_id, subject, machine_id, actor, scopes,
        created_by_sub, created_at, expires_at, registry_actor)
        VALUES (?, 'agent', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)`,
        family, tokens.accessHash, tokens.refreshHash, family, actor, actor, JSON.stringify(scopes),
        caller.actor ?? 'admin', now, expires, actor);
      sql.exec('UPDATE named_agents SET credential_family = ? WHERE name = ?', family, name);
      return json({ actor, access_token: tokens.accessToken, refresh_token: tokens.refreshToken, expires_at: expires, scopes }, 201);
    });
  }

  return transaction(() => {
    const row = get();
    if (!row || row.disabled_at) return json({ error: 'not_found' }, 404);
    if (action === 'forward') {
      if (Object.keys(body).some(key => key !== 'to') ||
          !(body.to === null || (typeof body.to === 'string' && SESSION.test(body.to)))) return json({ error: 'invalid_session' }, 400);
      const id = body.to === null ? null : body.to.slice('session:'.length).toLowerCase();
      try { setNamedAgentForward(sql, name, id, Date.now()); }
      catch (error: any) { if (error.message === 'unknown_recipient') return json({ error: 'unknown_recipient' }, 404); throw error; }
      return json({ actor, forward_session_id: id });
    }

    const keys = ['family', 'mcp_client', 'session'].filter(key => Object.prototype.hasOwnProperty.call(body, key));
    if (Object.keys(body).some(key => !['family', 'mcp_client', 'session', 'revoke'].includes(key)) ||
        (body.revoke !== undefined && typeof body.revoke !== 'boolean') || keys.length !== 1 ||
        typeof body[keys[0]] !== 'string' || !FAMILY.test(body[keys[0]])) return json({ error: 'invalid_delegate' }, 400);
    const now = Date.now();
    const nowIso = new Date(now).toISOString();
    let families: string[];
    if (body.mcp_client) {
      const hasMcpGrants = sql.exec("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_grants'").toArray().length > 0;
      if (!hasMcpGrants) return json({ error: 'no_live_credential_family' }, 404);
      const query = body.revoke
        ? `SELECT DISTINCT c.family_id FROM mcp_grants g JOIN credentials c ON c.family_id = g.family WHERE g.client = ? AND c.kind = 'human'`
        : `SELECT DISTINCT c.family_id FROM mcp_grants g JOIN credentials c ON c.family_id = g.family
           WHERE g.client = ? AND g.expires > ? AND c.kind = 'human' AND c.revoked_at IS NULL AND c.expires_at > ?`;
      const rows = body.revoke
        ? sql.exec(query, body.mcp_client).toArray() as any[]
        : sql.exec(query, body.mcp_client, now, nowIso).toArray() as any[];
      families = rows.map(item => String(item.family_id));
    } else if (body.session) {
      const session = SESSION.exec(body.session.startsWith('session:') ? body.session : 'session:' + body.session);
      if (!session) return json({ error: 'invalid_session' }, 400);
      const registryActor = 'session:' + session[1].toLowerCase();
      const query = body.revoke
        ? "SELECT DISTINCT family_id FROM credentials WHERE registry_actor = ? AND kind = 'agent'"
        : "SELECT DISTINCT family_id FROM credentials WHERE registry_actor = ? AND kind = 'agent' AND revoked_at IS NULL AND expires_at > ?";
      const rows = body.revoke ? sql.exec(query, registryActor).toArray() as any[] : sql.exec(query, registryActor, nowIso).toArray() as any[];
      families = rows.map(item => String(item.family_id));
    } else {
      const family = String(body.family);
      const live = sql.exec("SELECT 1 FROM credentials WHERE family_id = ? AND revoked_at IS NULL AND expires_at > ? LIMIT 1", family, nowIso).toArray().length > 0;
      families = body.revoke || live ? [family] : [];
    }
    if (!families.length && !body.revoke) return json({ error: 'no_live_credential_family' }, 404);
    let delegates: string[];
    try { delegates = updateNamedAgentDelegates(sql, name, families, body.revoke === true); }
    catch (error: any) {
      if (error.message === 'unknown_agent') return json({ error: 'not_found' }, 404);
      if (error.message === 'too_many_delegates') return json({ error: 'too_many_delegates' }, 400);
      throw error;
    }
    return json({ actor, delegates });
  });
}
