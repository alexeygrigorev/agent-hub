import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { doFetch } from './m1-focus-setup';
import { sha256 } from '../src/auth/crypto';

const defaultEnv = env as unknown as Env;
const ADMIN_TOKEN = 'op-token-alert-receipts-test';
const ADMIN_HEADERS = { Authorization: `Bearer ${ADMIN_TOKEN}`, 'Content-Type': 'application/json' };

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// Alert inbox-ack and onward-relay receipts (intake 1429bf88 AC1-AC3).
describe('alert relay receipts', () => {
  let stub: any;
  beforeEach(async () => {
    stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha256Hex(ADMIN_TOKEN), HUB_ALERT_WEBHOOK: '' } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { instance.sql.exec(trimmed); } catch (e) {} }
      }
      for (const table of ['alert_inbox_links', 'alert_relay_receipts', 'liveness_alerts', 'alert_delivery_receipts',
        'alert_backfill_suppressed', 'bus_reader_activity', 'bus_auth_pending', 'messages', 'audit', 'credentials', 'agents']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });
  const admin = (method: string, path: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method, headers: ADMIN_HEADERS, body: body === undefined ? undefined : JSON.stringify(body),
  }));

  async function watchedReader(h: any, tag = 'watched', token = 'test-reader-token') {
    const now = new Date().toISOString();
    h.sql.exec('INSERT INTO agents(tag, project, registered_at) VALUES (?, NULL, ?)', tag, now);
    h.sql.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
      VALUES (?, 'agent', ?, ?, ?, ?, ?, '["bus:read","bus:send"]', 'test', ?, ?)`,
      tag, await sha256(token), await sha256('refresh-' + token), tag, tag, tag, now, new Date(Date.now() + 3600000).toISOString());
    const caller = { kind: 'agent', actor: tag, tokenId: tag } as any;
    h.busService.inbox(caller, tag); // Empty polls are reader evidence too.
    return { caller, token };
  }

  async function deliverOneAlert(h: any, to = 'watched') {
    h.env = { ...h.env, HUB_ALERT_TO: to };
    const alert: any = h.livenessService.setAlert('session', 'host/shell', { state: 'stalled', detail: 'no_native_progress fixture detail' });
    await h.notifyAlert(alert);
    return alert;
  }

  it('links the inbox message to the alert and records read/ack receipts without bodies', async () => {
    await runInDurableObject(stub, async (h: Hub) => {
      await watchedReader(h);
      const alert = await deliverOneAlert(h);
      expect(h.sql.exec("SELECT COUNT(*) AS n FROM messages WHERE to_actor='watched'").one().n).toBe(1);
      const link: any = h.sql.exec('SELECT * FROM alert_inbox_links WHERE alert_id = ?', alert.id).toArray()[0];
      expect(link).toBeTruthy();
      expect(link.target).toBe('inbox:watched');
      expect(link.read_at).toBeNull();
      const message: any = h.sql.exec('SELECT id FROM messages WHERE to_actor=? ORDER BY id DESC', 'watched').toArray()[0];
      expect(link.message_id).toBe(message.id);

      h.busService.markRead({ kind: 'agent', actor: 'watched', tokenId: 'watched' }, [message.id]);
      h.busService.ack({ kind: 'agent', actor: 'watched', tokenId: 'watched' }, message.id);
      const stamped: any = h.sql.exec('SELECT * FROM alert_inbox_links WHERE message_id = ?', message.id).toArray()[0];
      expect(stamped.read_at).toBeTruthy();
      expect(stamped.acked_at).toBeTruthy();
    });
    const view: any = await (await admin('GET', '/api/alert-receipts')).json();
    expect(view.alerts).toHaveLength(1);
    expect(view.alerts[0]).toMatchObject({ id: view.alerts[0].id, relay_state: 'unconfirmed', delivered_to: ['inbox:watched'] });
    expect(view.unconfirmed).toBe(1);
    expect(view.alerts[0].inbox[0].read_at).toBeTruthy();
    expect(view.alerts[0].inbox[0].acked_at).toBeTruthy();
    // Receipts carry ids and times, never the alert body.
    expect(JSON.stringify(view)).not.toContain('no_native_progress');
  });

  it('records, replaces and validates onward relay receipts', async () => {
    let alertId = 0;
    await runInDurableObject(stub, async (h: Hub) => {
      await watchedReader(h);
      alertId = (await deliverOneAlert(h)).id;
    });
    expect((await admin('POST', `/api/alert-receipts/${alertId}/relay`, { relay_ref: '' })).status).toBe(400);
    expect((await admin('POST', `/api/alert-receipts/${alertId}/relay`, { relay_ref: 'ok', relayed_at: 'not-a-date' })).status).toBe(400);
    expect((await admin('POST', '/api/alert-receipts/999999/relay', { relay_ref: 'x' })).status).toBe(404);
    const first: any = await (await admin('POST', `/api/alert-receipts/${alertId}/relay`, { relay_ref: 'fwd-abc123', relayed_at: '2026-11-04T10:00:00Z' })).json();
    expect(first.relay).toMatchObject({ relay_ref: 'fwd-abc123', relayed_at: '2026-11-04T10:00:00.000Z', state: 'confirmed' });
    expect(first.relay.reported_by).toBeTruthy();
    const replaced: any = await (await admin('POST', `/api/alert-receipts/${alertId}/relay`, { relay_ref: 'fwd-abc123.2' })).json();
    expect(replaced.relay.relay_ref).toBe('fwd-abc123.2');
    const view: any = await (await admin('GET', '/api/alert-receipts')).json();
    expect(view.alerts[0].relay_state).toBe('confirmed');
    expect(view.unconfirmed).toBe(0);
    expect(view.alerts[0].relay.reported_by).toBe(first.relay.reported_by);
  });

  it('denies worker credentials reads and relay writes', async () => {
    let workerToken = '';
    await runInDurableObject(stub, async (h: Hub) => {
      workerToken = (await watchedReader(h, 'oncall-bot', 'oncall-bot-token')).token;
    });
    const worker = { Authorization: `Bearer ${workerToken}`, 'Content-Type': 'application/json' };
    const deniedRead = await doFetch(new Request('http://example.com/api/alert-receipts', { headers: worker }));
    expect(deniedRead.status).toBe(403);
    const deniedWrite = await doFetch(new Request('http://example.com/api/alert-receipts/1/relay', {
      method: 'POST', headers: worker, body: JSON.stringify({ relay_ref: 'nope' }) }));
    expect(deniedWrite.status).toBe(403);
  });

  it('keeps absent receipts explicitly unconfirmed and prunes receipts with their alert', async () => {
    await runInDurableObject(stub, async (h: Hub) => {
      await watchedReader(h);
      const alert = await deliverOneAlert(h);
      // Unlinked inbox message (never sent through the alert path) stays out of receipts.
      h.sql.exec(`INSERT INTO messages (id, from_actor, to_actor, kind, thread_id, body, idempotency_key, created_at, expires_at)
        VALUES ('m-other', 'hub', 'watched', 'note', 't-other', 'x', 'k-other', ?, ?)`,
        Date.now(), Date.now() + 86400000);
      const { pruneAlertReceipts } = await import('../src/alert-relay-receipts');
      // Orphan rows whose alert row is gone are removed with it.
      h.sql.exec("INSERT INTO alert_inbox_links (message_id, alert_id, target, sent_at) VALUES ('m-gone', 424242, 'inbox:watched', ?)", Date.now());
      h.sql.exec('INSERT INTO alert_relay_receipts (alert_id, relay_ref, relayed_at, reported_by, reported_at) VALUES (?, ?, ?, ?, ?)',
        424242, 'old', '2026-01-01T00:00:00Z', 'admin', new Date().toISOString());
      pruneAlertReceipts(h.sql);
      expect(h.sql.exec('SELECT COUNT(*) AS n FROM alert_inbox_links').one().n).toBe(1);
      expect(h.sql.exec('SELECT COUNT(*) AS n FROM alert_relay_receipts').one().n).toBe(0);
      // Aging the alert out (resolved beyond retention) removes its receipts too.
      h.sql.exec('UPDATE liveness_alerts SET resolved_at = ? WHERE id = ?',
        new Date(Date.now() - 31 * 24 * 3600 * 1000).toISOString(), alert.id);
      pruneAlertReceipts(h.sql);
      expect(h.sql.exec('SELECT COUNT(*) AS n FROM alert_inbox_links').one().n).toBe(0);
    });
    const view: any = await (await admin('GET', '/api/alert-receipts')).json();
    expect(view.unconfirmed).toBe(1);
  });
});
