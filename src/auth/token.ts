import { randomString, sha256 } from './crypto';

export interface TokenPair {
  accessToken: string;
  refreshToken: string;
  accessHash: string;
  refreshHash: string;
}

export async function generateTokenPair(): Promise<TokenPair> {
  const accessToken = 'hubt_' + randomString(32);
  const refreshToken = 'hubr_' + randomString(32);
  return {
    accessToken,
    refreshToken,
    accessHash: await sha256(accessToken),
    refreshHash: await sha256(refreshToken)
  };
}

export async function verifyBearerScope(
  sql: SqlStorage, 
  authHeader: string | null, 
  requiredScope: string,
  optional?: { kind?: string, machineId?: string, actor?: string }
): Promise<any | null> {
  if (!authHeader || !/^Bearer hubt_[A-Za-z0-9_-]+$/i.test(authHeader)) return null;
  const token = authHeader.substring(7).trim();
  const tokenHash = await sha256(token);
  
  const cred = sql.exec('SELECT * FROM credentials WHERE token_hash = ?', tokenHash).toArray()[0] as any;
  if (!cred || cred.revoked_at || !Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= Date.now()) return null;
  
  let scopes: unknown;
  try { scopes = typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : cred.scopes; } catch { return null; }
  if (!Array.isArray(scopes) || scopes.includes('admin') || !scopes.includes(requiredScope)) return null;

  if (optional) {
    if (optional.kind && cred.kind !== optional.kind) return null;
    if (optional.machineId && cred.machine_id !== optional.machineId) return null;
    if (optional.actor && cred.actor !== optional.actor) return null;
  }
  
  sql.exec('UPDATE credentials SET last_used_at = ? WHERE id = ?', new Date().toISOString(), cred.id);
  
  return cred;
}
