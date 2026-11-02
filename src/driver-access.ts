/** Narrow coordinator authority, derived from an operator-created live driver.
 * No human credentials, administrator scopes or client supplied role flags.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { credentialActor } from './agent_registry';
import type { TokenPair } from './auth/token';

export const DRIVER_SCOPE = 'driver:orchestrate';
export const DRIVER_SCOPES = ['bus:read', 'fleet:launch', DRIVER_SCOPE] as const;
export const DRIVER_ACCESS_SCHEMA = `CREATE TABLE IF NOT EXISTS driver_access (
  family_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL, driver_id TEXT NOT NULL, expires_at INTEGER NOT NULL)`;
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const WEEK = 7 * 86400000;
const hasTable = (sql: SqlStorage, name: string) => sql.exec(
  "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name).toArray().length > 0;
const first = (sql: SqlStorage, query: string, ...args: any[]): any => sql.exec(query, ...args).toArray()[0];

export type DriverMembership = {
  driver_id: string; session_id: string; launch_id: string; machine_id: string;
  workspace: string; expires_at: number;
};

/** Structural registration is deliberately independent of role liveness. A
 * stale driver offer must still require credentials, then fail strict issuance;
 * it must never fall back to an unscoped interactive launch. */
export function driverLaunchRegistered(sql: SqlStorage, launchId: string): boolean {
  if (!UUID.test(launchId) || !hasTable(sql, 'hub_drivers')) return false;
  return !!first(sql, `SELECT d.id FROM hub_drivers d JOIN session_control_sessions s
    ON s.actor=d.actor AND s.request_key='driver-' || d.id WHERE s.fleet_launch_id=? LIMIT 1`, launchId);
}

/** Also matches the deterministic session key before DriverService records the
 * awaited start response. The host must never receive an unscoped offer in that gap. */
export function driverForLaunch(sql: SqlStorage, launchId: string, now = Date.now()): DriverMembership | null {
  if (!UUID.test(launchId) || !hasTable(sql, 'hub_drivers')) return null;
  const driver = first(sql, 'SELECT * FROM hub_drivers ORDER BY rowid DESC LIMIT 1');
  if (!driver || !['starting', 'active'].includes(driver.phase)) return null;
  const created = Date.parse(driver.created_at);
  if (!Number.isFinite(created) || created > now || created + WEEK <= now) return null;
  const session = first(sql, `SELECT s.*, f.workspace AS launch_workspace, f.machine_id AS launch_machine,
    f.state AS launch_state FROM session_control_sessions s JOIN fleet_launches f ON f.id=s.fleet_launch_id
    WHERE s.fleet_launch_id=? AND s.actor=? AND s.request_key=?`, launchId, driver.actor, 'driver-' + driver.id);
  if (!session || session.mode !== 'interactive' ||
      !['offered', 'starting', 'running', 'uncertain', 'queued', 'held'].includes(session.state) ||
      !['offered', 'starting', 'running', 'uncertain'].includes(session.launch_state) ||
      !session.launch_machine || (session.machine_id && session.machine_id !== session.launch_machine) ||
      (driver.session_id && driver.session_id !== session.id)) return null;
  return { driver_id: driver.id, session_id: session.id, launch_id: launchId,
    machine_id: session.launch_machine, workspace: session.launch_workspace, expires_at: created + WEEK };
}

export type DriverContext = DriverMembership & { actor: string; family_id: string; token_id: string };

/** Re-read on each authorization and inside service effect transactions. Refresh
 * may ignore access-token expiry, but never the role's hard deadline or revocation. */
export function driverAccessContext(sql: SqlStorage, tokenId: string | undefined,
  now = Date.now(), allowExpiredAccess = false): DriverContext | null {
  if (!tokenId || !hasTable(sql, 'driver_access')) return null;
  const credential = first(sql, 'SELECT * FROM credentials WHERE id=?', tokenId);
  if (!credential || credential.kind !== 'agent' || credential.revoked_at) return null;
  const expiry = Date.parse(credential.expires_at);
  if (!Number.isFinite(expiry) || (!allowExpiredAccess && expiry <= now)) return null;
  let scopes: unknown;
  try { scopes = JSON.parse(credential.scopes); } catch { return null; }
  if (!Array.isArray(scopes) || scopes.length !== DRIVER_SCOPES.length ||
      !DRIVER_SCOPES.every(scope => scopes.includes(scope))) return null;
  const access = first(sql, 'SELECT * FROM driver_access WHERE family_id=?', credential.family_id);
  if (!access || !Number.isSafeInteger(access.expires_at) || access.expires_at <= now) return null;
  const membership = driverForLaunch(sql, access.launch_id, now);
  if (!membership || membership.driver_id !== access.driver_id || membership.session_id !== access.session_id ||
      membership.machine_id !== credential.machine_id || access.expires_at > membership.expires_at) return null;
  const actor = credentialActor(sql, credential);
  if (!actor || actor !== 'hub-drivers:driver-' + access.launch_id) return null;
  return { ...membership, expires_at: access.expires_at, actor, family_id: credential.family_id, token_id: tokenId };
}

/** Called only inside the existing host-authenticated issuance transaction,
 * after rechecking hostAllowed. Returns the existing worker profile DTO so the
 * private file transport and managed-HOME discovery require no special case. */
export function issueDriverAccess(sql: SqlStorage, launchId: string, machineId: string,
  tokens: TokenPair, baseUrl: string, now = Date.now()): Record<string, unknown> | null {
  const member = driverForLaunch(sql, launchId, now);
  if (!member || member.machine_id !== machineId) return null;
  sql.exec(DRIVER_ACCESS_SCHEMA);
  const tag = 'driver-' + launchId, actor = 'hub-drivers:' + tag;
  const prior = first(sql, 'SELECT * FROM driver_access WHERE launch_id=?', launchId);
  const launchState = first(sql, 'SELECT state FROM fleet_launches WHERE id=?', launchId)?.state;
  // Initial credentials must be issued before native execution. A host may
  // restore a lost profile on a running driver only after this exact launch
  // already has a driver family bound to the same session.
  if (launchState !== 'starting' && !(launchState === 'running' && prior)) return null;
  if (prior && (prior.driver_id !== member.driver_id || prior.session_id !== member.session_id || prior.expires_at <= now)) return null;
  const registration = first(sql, "SELECT machine_id,role FROM agents WHERE tag=? AND project='hub-drivers'", tag);
  if (registration && (registration.machine_id !== machineId || registration.role !== 'driver')) return null;
  const at = new Date(now).toISOString(), family = prior?.family_id ?? crypto.randomUUID();
  const expires = Math.min(prior?.expires_at ?? member.expires_at, member.expires_at);
  if (!registration) sql.exec('INSERT INTO agents(tag,project,role,machine_id,registered_at) VALUES(?,?,?,?,?)',
    tag, 'hub-drivers', 'driver', machineId, at);
  // Host retries can rotate the pre-start profile, but cannot prolong the role.
  sql.exec('UPDATE credentials SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL', at, family);
  sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES(?,'agent',?,?,?,?,?,?,?,?,?,?)`, crypto.randomUUID(), tokens.accessHash, tokens.refreshHash,
    family, actor, machineId, actor, JSON.stringify(DRIVER_SCOPES), 'host:' + machineId, at,
    new Date(Math.min(now + 3600000, expires)).toISOString());
  sql.exec(`INSERT INTO driver_access(family_id,launch_id,session_id,driver_id,expires_at) VALUES(?,?,?,?,?)
    ON CONFLICT(family_id) DO UPDATE SET expires_at=excluded.expires_at`,
    family, launchId, member.session_id, member.driver_id, expires);
  return { access_token: tokens.accessToken, refresh_token: tokens.refreshToken, client_kind: 'agent',
    worker_launch_id: launchId, worker_expires_at: expires, base_url: baseUrl };
}

/** Positive route containment; payload checks and ordinary service ownership
 * remain mandatory. Global session observation never grants foreign commands. */
export function driverRequestAllowed(sql: SqlStorage, request: Request, tokenId: string, now = Date.now()): boolean {
  const context = driverAccessContext(sql, tokenId, now);
  if (!context) return false;
  const path = new URL(request.url).pathname, method = request.method;
  if (method === 'GET') {
    if (['/api/intake/tasks', '/api/intake/forgotten', '/api/intake/handoff', '/api/intake/queue', '/api/sessions'].includes(path)) return true;
    if (/^\/api\/intake\/tasks\/[A-Za-z0-9._:-]+(?:\/(history|links|context))?$/.test(path)) return true;
    if (/^\/api\/sessions\/[0-9a-f-]{36}$/.test(path)) return true;
  }
  if (method === 'POST') {
    if (path === '/api/intake/tasks' || path === '/api/sessions') return true;
    if (/^\/api\/intake\/tasks\/[A-Za-z0-9._:-]+(?:\/(events|assign))?$/.test(path)) return true;
  }
  const child = /^\/api\/sessions\/([0-9a-f-]{36})\/(commands|send|capture|stop|cancel)$/.exec(path);
  if (!child || !(method === 'GET' && child[2] === 'commands' || method === 'POST' && child[2] !== 'commands')) return false;
  const row = first(sql, 'SELECT actor,parent_id,owner_family FROM session_control_sessions WHERE id=?', child[1]);
  return !!row && row.actor === context.actor && row.parent_id === context.session_id && row.owner_family === context.family_id;
}

/** Intake route authorization alone cannot distinguish an AC edit from close,
 * archive or operator decisions. Apply this inside the intake transaction,
 * including before replaying an idempotent response. Ordinary validation still
 * checks types, version, bounds and evidence on every allowed operation. */
export function driverIntakeMutationAllowed(path: string, payload: any): boolean {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return false;
  const only = (fields: string[]) => Object.keys(payload).every(key => fields.includes(key));
  if (path === '/tasks') return only(['key', 'project', 'title', 'text', 'source', 'transcription', 'acceptance_criteria']) &&
    typeof payload.acceptance_criteria === 'string' && payload.acceptance_criteria.trim().length > 0 &&
    payload.acceptance_criteria.length <= 8000;
  if (/^\/tasks\/[A-Za-z0-9._:-]+\/events$/.test(path)) {
    if (!only(['key', 'expected_version', 'event', 'note', 'text'])) return false;
    const forms = ['event', 'note', 'text'].filter(key => key in payload);
    if (forms.length !== 1) return false;
    if ('event' in payload) return ['note', 'report', 'blocker', 'work_action', 'first_action', 'session', 'link']
      .includes(payload.event?.kind);
    if ('text' in payload) return typeof payload.text === 'string';
    if (typeof payload.note === 'string') return true;
    return !!payload.note && typeof payload.note === 'object' && !Array.isArray(payload.note) &&
      Object.keys(payload.note).every(key => ['text', 'source'].includes(key)) && typeof payload.note.text === 'string';
  }
  if (/^\/tasks\/[A-Za-z0-9._:-]+\/assign$/.test(path)) {
    if (!only(['key', 'expected_version', 'session', 'provenance', 'current_state', 'next_step', 'owner'])) return false;
    const session = payload.session;
    if (session === undefined) return true;
    return !!session && typeof session === 'object' && !Array.isArray(session) &&
      session.mode === 'headless' && !('engine' in session) && !('machine' in session) && !('parent_id' in session);
  }
  if (/^\/tasks\/[A-Za-z0-9._:-]+$/.test(path)) {
    return only(['key', 'expected_version', 'title', 'current_state', 'next_step', 'owner', 'uncertainty']);
  }
  return false;
}
