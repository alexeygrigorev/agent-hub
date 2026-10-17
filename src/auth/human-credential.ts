import type { SqlStorage } from '../bus';
import type { Env } from '../env';
import {
  humanAdminAllowed,
  TOKEN_ADMIN_EMAIL,
} from './session';

export interface VerifiedHumanCredential {
  actor: string;
  sub: string;
  email: string;
  expiresAt: string;
  tokenId: string;
}

/**
 * Validates whether a human identity (sub + stored email) is currently authorized.
 * - Google email: must match users table row case-insensitively, user must not be disabled
 *   (is_admin = 1), and email must be in configuredAdmins / HUB_ADMINS / HUB_ADDITIONAL_ADMINS.
 *   Missing, disabled, or unallowlisted users fail closed.
 * - Bootstrap token admin (storedEmail === TOKEN_ADMIN_EMAIL): only valid if HUB_ADMIN_TOKEN_SHA256
 *   is configured and sub matches tokenAdminSubject(hash).
 */
export function isHumanCredentialAllowed(
  sql: SqlStorage,
  env: Env | undefined,
  sub: string,
  storedEmail: string,
): boolean {
  if (!sub || !storedEmail || !env) return false;
  if (storedEmail === TOKEN_ADMIN_EMAIL) {
    return humanAdminAllowed(env, storedEmail, sub);
  }
  try {
    const row = sql
      .exec('SELECT email, is_admin FROM users WHERE sub = ?', sub)
      .toArray()[0] as any;
    if (!row) return false;
    if (!row.is_admin) return false;
    const currentEmail = typeof row.email === 'string' ? row.email : null;
    if (!currentEmail) return false;
    if (currentEmail.toLowerCase() !== storedEmail.toLowerCase()) return false;
    return humanAdminAllowed(env, currentEmail, sub);
  } catch {
    return false;
  }
}

/**
 * Validates a credential row from SQLite to ensure it is a valid, unrevoked, unexpired,
 * non-machine-bound human credential with exact ['admin'] scope and an active allowlisted approver.
 */
export function validateHumanCredentialRecord(
  sql: SqlStorage,
  env: Env | undefined,
  cred: any,
  nowMs: number = Date.now(),
): VerifiedHumanCredential | null {
  if (!cred || typeof cred !== 'object') return null;
  if (cred.kind !== 'human') return null;
  if (cred.revoked_at !== null && cred.revoked_at !== undefined) return null;
  if (cred.machine_id !== null && cred.machine_id !== undefined) return null;

  const expiresMs = Date.parse(cred.expires_at);
  if (!Number.isFinite(expiresMs) || expiresMs <= nowMs) {
    return null;
  }

  let scopes: unknown;
  try {
    scopes =
      typeof cred.scopes === 'string' ? JSON.parse(cred.scopes) : cred.scopes;
  } catch {
    return null;
  }
  if (!Array.isArray(scopes) || scopes.length !== 1 || scopes[0] !== 'admin') {
    return null;
  }

  const actor = typeof cred.actor === 'string' ? cred.actor : '';
  const email = typeof cred.subject === 'string' ? cred.subject : '';
  if (!actor.startsWith('human:')) return null;
  const sub = actor.slice('human:'.length);
  if (!sub || !email) return null;
  if (typeof cred.created_by_sub !== 'string' || cred.created_by_sub !== sub) {
    return null;
  }

  if (!isHumanCredentialAllowed(sql, env, sub, email)) {
    return null;
  }

  return {
    actor,
    sub,
    email,
    expiresAt: cred.expires_at,
    tokenId: String(cred.id),
  };
}
