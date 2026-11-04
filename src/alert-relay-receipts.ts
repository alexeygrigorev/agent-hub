/** Alert inbox-ack and onward-relay receipts (intake 1429bf88 AC1-AC3).
 *
 * Alert sends are already receipted (alert_delivery_receipts records that the
 * Hub handed an alert to a target). What was missing is what happened next:
 * AC1 links the inbox bus message back to its alert and records when the
 * alert inbox target read/acknowledged it; AC2 lets the relay owner report
 * onward delivery to Alexey per alert (forward receipt id + timestamp, no
 * message content); AC3 exposes one bounded operator view where an absent
 * relay receipt is explicitly `unconfirmed`, never inferred delivered.
 * Receipt rows carry ids, targets and times only — never alert bodies.
 */
import type { SqlStorage } from '@cloudflare/workers-types';

export const ALERT_RELAY_SCHEMA = `
CREATE TABLE IF NOT EXISTS alert_inbox_links (
  message_id TEXT PRIMARY KEY,
  alert_id INTEGER NOT NULL,
  target TEXT NOT NULL,
  sent_at INTEGER NOT NULL,
  read_at INTEGER,
  acked_at INTEGER
);
CREATE INDEX IF NOT EXISTS alert_inbox_links_alert ON alert_inbox_links(alert_id, sent_at);
CREATE TABLE IF NOT EXISTS alert_relay_receipts (
  alert_id INTEGER PRIMARY KEY,
  relay_ref TEXT NOT NULL,
  relayed_at TEXT NOT NULL,
  reported_by TEXT NOT NULL,
  reported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_relay_receipts_reported ON alert_relay_receipts(reported_at);
`;

export function initAlertRelayReceipts(sql: { exec(query: string, ...values: any[]): unknown }) {
  for (const stmt of ALERT_RELAY_SCHEMA.split(';')) if (stmt.trim()) sql.exec(stmt);
}

/** Called from the Hub alert delivery path right after a successful inbox
 * send. The bus message id is the stable link; a replayed idempotency key
 * returns the same message id, so INSERT OR IGNORE keeps the first link. */
export function linkAlertInboxMessage(sql: SqlStorage, alertId: number, target: string, messageId: string, sentAt: number) {
  if (!Number.isInteger(alertId) || alertId < 1 || !messageId) return;
  sql.exec('INSERT OR IGNORE INTO alert_inbox_links (message_id, alert_id, target, sent_at) VALUES (?, ?, ?, ?)',
    messageId, alertId, target, sentAt);
}

/** Bus onStage hook: when an alert-linked message is read/acknowledged by
 * its recipient, stamp the receipt. Stage times are written once; repeating
 * a stage never moves an earlier receipt. Non-alert messages touch nothing
 * beyond one primary-key lookup. */
export function onMessageStageReceipt(sql: SqlStorage, messageId: string, state: string, now: number) {
  if (state === 'read') {
    sql.exec('UPDATE alert_inbox_links SET read_at = ? WHERE message_id = ? AND read_at IS NULL', now, messageId);
  } else if (state === 'acked') {
    sql.exec('UPDATE alert_inbox_links SET acked_at = ? WHERE message_id = ? AND acked_at IS NULL', now, messageId);
  }
}

const RELAY_REF_MAX = 200;

/** Operator-reported onward relay receipt for one alert (AC2). Holds the
 * forward receipt id and timestamp only; a later report replaces the earlier
 * one, and every write is auditable via the operator route's audit trail. */
export function recordRelayReceipt(sql: SqlStorage, alertId: number, input: { relay_ref?: unknown; relayed_at?: unknown },
  reportedBy: string, now: () => number): { error: string; detail?: string } | { receipt: RelayReceipt } {
  if (!Number.isInteger(alertId) || alertId < 1) return { error: 'invalid_alert_id' };
  const alert = sql.exec('SELECT id FROM liveness_alerts WHERE id = ?', alertId).toArray()[0];
  if (!alert) return { error: 'alert_not_found' };
  const relayRef = typeof input?.relay_ref === 'string' ? input.relay_ref.trim() : '';
  if (!relayRef || relayRef.length > RELAY_REF_MAX || /[\x00-\x1f\x7f]/.test(relayRef)) {
    return { error: 'invalid_relay_ref', detail: `1-${RELAY_REF_MAX} visible chars (forward receipt id)` };
  }
  let relayedAt = new Date(now()).toISOString();
  if (input?.relayed_at !== undefined && input?.relayed_at !== null && input.relayed_at !== '') {
    if (typeof input.relayed_at !== 'string' || Number.isNaN(Date.parse(input.relayed_at))) return { error: 'invalid_relayed_at' };
    relayedAt = new Date(input.relayed_at).toISOString();
  }
  sql.exec(`INSERT INTO alert_relay_receipts (alert_id, relay_ref, relayed_at, reported_by, reported_at) VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(alert_id) DO UPDATE SET relay_ref = excluded.relay_ref, relayed_at = excluded.relayed_at,
      reported_by = excluded.reported_by, reported_at = excluded.reported_at`,
    alertId, relayRef, relayedAt, reportedBy, new Date(now()).toISOString());
  return { receipt: relayReceipt(sql, alertId)! };
}

export type RelayReceipt = { relay_ref: string; relayed_at: string; reported_by: string; reported_at: string; state: 'confirmed' };

function relayReceipt(sql: SqlStorage, alertId: number): RelayReceipt | null {
  const row: any = sql.exec('SELECT * FROM alert_relay_receipts WHERE alert_id = ?', alertId).toArray()[0];
  return row ? { relay_ref: row.relay_ref, relayed_at: row.relayed_at, reported_by: row.reported_by,
    reported_at: row.reported_at, state: 'confirmed' } : null;
}

function parseDelivered(json: unknown): string[] {
  try { const v = JSON.parse(String(json || '[]')); return Array.isArray(v) ? v.map(String) : []; } catch { return []; }
}

/** Bounded operator view (AC3): per alert, where it was delivered, the inbox
 * read/ack receipts, and the onward relay receipt or an explicit
 * `unconfirmed`. Newest alerts first; `limit` is clamped to 500. */
export function alertReceipts(sql: SqlStorage, limit = 100): { alerts: any[]; unconfirmed: number } {
  const lim = Math.min(500, Math.max(1, Math.floor(limit) || 100));
  const alerts = sql.exec('SELECT id, subject_kind, subject, state, opened_at, resolved_at, notified_at, notify_error, delivered_to FROM liveness_alerts ORDER BY id DESC LIMIT ?', lim).toArray() as any[];
  const out: any[] = [];
  let unconfirmed = 0;
  for (const alert of alerts) {
    const inbox = sql.exec('SELECT target, message_id, sent_at, read_at, acked_at FROM alert_inbox_links WHERE alert_id = ? ORDER BY sent_at', alert.id).toArray()
      .map((l: any) => ({ target: l.target, message_id: l.message_id, sent_at: new Date(Number(l.sent_at)).toISOString(),
        read_at: l.read_at ? new Date(Number(l.read_at)).toISOString() : null, acked_at: l.acked_at ? new Date(Number(l.acked_at)).toISOString() : null }));
    const relay = relayReceipt(sql, alert.id);
    if (!relay) unconfirmed += 1;
    out.push({ id: alert.id, subject_kind: alert.subject_kind, subject: alert.subject, state: alert.state,
      opened_at: alert.opened_at, resolved_at: alert.resolved_at, notified_at: alert.notified_at,
      delivered_to: parseDelivered(alert.delivered_to), inbox, relay, relay_state: relay ? 'confirmed' : 'unconfirmed' });
  }
  return { alerts: out, unconfirmed };
}

/** Receipt rows share the liveness alert lifecycle: links and relay receipts
 * whose alert row is gone (resolved and pruned) are removed with it. */
export function pruneAlertReceipts(sql: SqlStorage, now = Date.now(), keepDays = 30) {
  const cutoff = new Date(now - keepDays * 24 * 3600 * 1000).toISOString();
  sql.exec('DELETE FROM alert_inbox_links WHERE alert_id NOT IN (SELECT id FROM liveness_alerts)');
  sql.exec('DELETE FROM alert_relay_receipts WHERE alert_id NOT IN (SELECT id FROM liveness_alerts)');
  sql.exec(`DELETE FROM alert_inbox_links WHERE alert_id IN (
    SELECT id FROM liveness_alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?)`, cutoff);
  sql.exec(`DELETE FROM alert_relay_receipts WHERE alert_id IN (
    SELECT id FROM liveness_alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?)`, cutoff);
}
