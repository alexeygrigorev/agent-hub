import type { SqlStorage } from '@cloudflare/workers-types';
import { credentialActor } from './agent_registry';

export function readerActor(sql: SqlStorage, actor: string): string {
  const agents = sql.exec("SELECT tag, project FROM agents WHERE tag=? OR project || ':' || tag=? OR 'role:' || role=?", actor, actor, actor).toArray() as any[];
  if (agents.length !== 1) return actor;
  return agents[0].project ? `${agents[0].project}:${agents[0].tag}` : agents[0].tag;
}

// A credential is not evidence of a reader. Only successful recipient/host
// inbox polls count; operator inspections never renew this lease.
export const READER_FRESH_MS = 15 * 60 * 1000;
export const ALERT_SUBJECT_COOLDOWN_MS = 24 * 60 * 60 * 1000;
export const ALERT_ROUTING_SCHEMA = `
CREATE TABLE IF NOT EXISTS bus_reader_activity (recipient TEXT NOT NULL, credential_id TEXT NOT NULL, last_read_at INTEGER NOT NULL, PRIMARY KEY(recipient, credential_id));
CREATE TABLE IF NOT EXISTS alert_delivery_receipts (subject_kind TEXT NOT NULL, subject TEXT NOT NULL, target TEXT NOT NULL, alert_id INTEGER NOT NULL, sent_at INTEGER NOT NULL, PRIMARY KEY(subject_kind, subject, target));
`;
export function initAlertRouting(sql: { exec(query: string, ...values: any[]): unknown }) {
  for (const stmt of ALERT_ROUTING_SCHEMA.split(';')) if (stmt.trim()) sql.exec(stmt);
}

export function inboxReaderStatus(sql: SqlStorage, actor: string, now = Date.now()) {
  actor = readerActor(sql, actor);
  const readers = sql.exec(`SELECT c.*, r.last_read_at FROM bus_reader_activity r
    JOIN credentials c ON c.id = r.credential_id WHERE r.recipient = ?
    AND c.revoked_at IS NULL AND r.last_read_at >= ? AND r.last_read_at <= ?`, actor, now - READER_FRESH_MS, now).toArray() as any[];
  const active = readers.some(c => {
    if (!(Date.parse(c.expires_at) > now)) return false;
    let scopes: any; try { scopes = JSON.parse(c.scopes); } catch { return false; }
    if (!Array.isArray(scopes) || !scopes.some(s => ['admin', 'bus:read', 'host'].includes(s))) return false;
    if (c.kind === 'agent') return credentialActor(sql, c) === actor;
    if (c.kind === 'host') {
      // Host bridge authority must still refer to the recipient's current host.
      const agents = sql.exec("SELECT machine_id FROM agents WHERE (CASE WHEN COALESCE(project,'')='' THEN tag ELSE project || ':' || tag END) = ?", actor).toArray() as any[];
      return agents.length === 1 && agents[0].machine_id === c.machine_id;
    }
    return c.actor === actor;
  });
  return { actor, active_reader: active, reason: active ? 'active_read_loop' : 'no_active_credential_read_loop' };
}

export function alertDestinations(env: { HUB_ALERT_TO?: string; HUB_ALERT_WEBHOOK?: string }) {
  const targets = [...new Set((env.HUB_ALERT_TO || '').split(',').map(t => t.trim()).filter(Boolean))];
  const actors = targets.filter(t => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(t));
  const invalidActors = actors.length !== targets.length;
  let webhook: string | null = null;
  let invalidWebhook = false;
  if (env.HUB_ALERT_WEBHOOK) {
    try {
      const url = new URL(env.HUB_ALERT_WEBHOOK);
      if (url.protocol !== 'https:' || url.username || url.password || url.hash) invalidWebhook = true;
      else webhook = url.toString();
    } catch { invalidWebhook = true; }
  }
  return { actors, webhook, invalidWebhook, invalidActors };
}
