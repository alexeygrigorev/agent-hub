/** Hub inbound webhooks -> session delivery (intake 19ec91c2).
 *
 * Operator-configured routes map a public `POST /webhooks/<route id>` endpoint
 * to a target Hub session (friendly label, qualified alias or UUID). Inbound
 * payloads are authenticated per route (bearer token or HMAC over
 * `<ts>.<body>`), rate-limited through durable per-minute buckets, and
 * size-capped before any parsing.
 *
 * Delivery reuses the existing verified session-control send path (durable
 * commands + ack ledger) instead of a new transport: the queue resolves the
 * target session at delivery time and enqueues a `send` command. While the
 * target is missing or unbound the delivery stays pending and redelivers with
 * bounded backoff; idempotency is on (source_id, delivery_key) for the queue
 * and on attempt-scoped request keys for the send command, so hub retries and
 * host transport retries can never double-send into a session.
 *
 * Secret storage: token-mode secrets are stored as SHA-256 hashes. HMAC-mode
 * signing keys must be recoverable to verify signatures, so they are stored
 * XOR-wrapped with an HMAC(SESSION_SECRET, route nonce) pad — ciphertext at
 * rest, plaintext only in memory during verification. Secrets are returned
 * exactly once at creation; audit rows carry ids and statuses, never payload
 * bodies or secrets.
 */
import { sha256 } from './auth/crypto';
import type { SqlStorage } from '@cloudflare/workers-types';

export const WEBHOOK_SCHEMA = `
CREATE TABLE IF NOT EXISTS webhook_routes (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  target_label TEXT NOT NULL,
  template TEXT,
  auth_mode TEXT NOT NULL DEFAULT 'token',
  secret_storage TEXT NOT NULL,
  rate_limit_per_min INTEGER NOT NULL DEFAULT 30,
  max_body_bytes INTEGER NOT NULL DEFAULT 65536,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_rate_buckets (
  webhook_id TEXT NOT NULL,
  bucket INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (webhook_id, bucket)
);
CREATE TABLE IF NOT EXISTS session_deliveries (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  delivery_key TEXT NOT NULL,
  target_label TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  session_id TEXT,
  command_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  delivered_at TEXT,
  UNIQUE(source_id, delivery_key)
);
CREATE INDEX IF NOT EXISTS session_deliveries_due ON session_deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS session_deliveries_recent ON session_deliveries(source_id, created_at);
`;

const HEADERS = { 'Cache-Control': 'private, no-store' };
const MAX_SEND_BODY = 16000; // session-control send body limit; renders truncate honestly beyond it
const MAX_ATTEMPTS = 5;             // transport/refusal failures
const ABSENCE_MAX_ATTEMPTS = 96;    // target absent/unbound: ~24h at the 15-min backoff cap
const BACKOFF_MS = [60_000, 120_000, 240_000, 480_000, 900_000];
const HMAC_TOLERANCE_MS = 5 * 60 * 1000;
const RATE_PRUNE_MS = 2 * 60_000;
const NAME_RE = /^[a-z0-9][a-z0-9-]{1,63}$/;
const DELIVERY_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// BOUND_STATES mirrors session-control: only bound, running sessions accept sends.
const BOUND_STATES = new Set(['running']);

type Caller = { kind?: string; actor?: string; isAdmin?: boolean; machineId?: string; tokenId?: string };
type Auth = { caller?: Caller; scopes?: string[]; error?: Response };
type Json = { status: number; json?: any };

function json(data: any, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', ...HEADERS, ...headers } });
}
function parsed(value: string | null | undefined, fallback: any): any {
  if (!value) return fallback;
  try { return JSON.parse(value); } catch { return fallback; }
}
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}
async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function hmacBytes(key: string, text: string): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(text)));
}
function toHex(bytes: Uint8Array): string {
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function hmacHex(key: string, text: string): Promise<string> {
  return toHex(await hmacBytes(key, text));
}
function randomToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return 'whsec_' + btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Render a stored template against the raw request body.
 * Placeholders: `{{payload}}` raw body, `{{payload_json}}` pretty JSON when
 * parseable, `{{payload.<dot.path>}}` extracted value. Unknown placeholders
 * are removed from the template BEFORE substitution and reported, so a typo
 * can never silently leak markup into a live session prompt — and payload
 * content that itself contains braces is delivered verbatim. Output is
 * capped at the session send limit. */
export function renderTemplate(template: string | null, bodyText: string): { text: string; warnings: string[] } {
  const warnings: string[] = [];
  const KNOWN = /^payload(_json|(\.[A-Za-z0-9_.-]+))?$/;
  let parsedBody: any;
  let parseTried = false;
  const tryParse = () => {
    if (!parseTried) { parsedBody = parsed(bodyText, undefined); parseTried = true; }
    return parsedBody;
  };
  let text = (template ?? '{{payload}}').replace(/\{\{\s*([^{}]*)\s*\}\}/g, (whole, inner: string) => {
    if (KNOWN.test(inner)) return whole;
    warnings.push(`unknown_placeholder:${inner}`);
    return '';
  });
  text = text.replace(/\{\{\s*payload(_json|(\.[A-Za-z0-9_.-]+))?\s*\}\}/g, (_match, whole: string, dotPath?: string) => {
    if (whole === '_json') {
      const value = tryParse();
      return value === undefined ? bodyText : JSON.stringify(value, null, 2);
    }
    if (!dotPath) return bodyText;
    const value = dotPath.slice(1).split('.').reduce<any>((node, key) => {
      if (node === null || node === undefined) return undefined;
      if (Array.isArray(node) && /^\d+$/.test(key)) return node[Number(key)];
      if (typeof node === 'object') return node[key];
      return undefined;
    }, tryParse());
    if (value === undefined) {
      warnings.push(`missing_field:${dotPath.slice(1)}`);
      return '';
    }
    return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  });
  if (text.length > MAX_SEND_BODY) text = text.slice(0, MAX_SEND_BODY) + '\n[hub: truncated after 16000 characters]';
  return { text, warnings };
}

export type WebhookOptions = { maxBatch?: number };

export class WebhookService {
  private sql: SqlStorage;
  private txn: (fn: () => any) => any;
  private now: () => number;
  private maxBatch: number;
  private sessionSecret: string | null;
  /** Resolved lazily at call time (Hub wires the real SessionControlService). */
  private sessionControl: () => { handle: (request: Request, auth: Auth) => Promise<Response> } | null;

  constructor(sql: SqlStorage, txn: (fn: () => any) => any, now: () => number,
              sessionControl: () => { handle: (request: Request, auth: Auth) => Promise<Response> } | null,
              sessionSecret: string | null, options: WebhookOptions = {}) {
    this.sql = sql;
    this.txn = txn;
    this.now = now;
    this.maxBatch = options.maxBatch ?? 20;
    this.sessionSecret = sessionSecret;
    this.sessionControl = sessionControl;
  }

  init() {
    for (const statement of WEBHOOK_SCHEMA.split(';')) {
      const trimmed = statement.trim();
      if (trimmed) this.sql.exec(trimmed);
    }
  }

  private at(): string { return new Date(this.now()).toISOString(); }

  private audit(action: string, actor: string, target: string, detail: Record<string, unknown>) {
    try {
      this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
        actor, action, 'webhook', target, JSON.stringify(detail), this.at());
    } catch { /* audit is best-effort; delivery state stays authoritative */ }
  }

  // ------------------------------------------------------- secret storage

  /** Deterministic counter-mode pad from the server session secret: block n
   * is the raw 32 HMAC bytes of SESSION_SECRET over "whroute:<id>:<n>", so
   * any key length wraps without key reuse. */
  private async pad(routeId: string, length: number): Promise<Uint8Array> {
    const out = new Uint8Array(length);
    for (let block = 0; block * 32 < length; block++) {
      const bytes = await hmacBytes(this.sessionSecret!, `whroute:${routeId}:${block}`);
      out.set(bytes.slice(0, Math.min(32, length - block * 32)), block * 32);
    }
    return out;
  }

  private async wrapHmacKey(routeId: string, secret: string): Promise<string | null> {
    if (!this.sessionSecret) return null;
    const keyBytes = new TextEncoder().encode(secret);
    const padBytes = await this.pad(routeId, keyBytes.length);
    const wrapped = keyBytes.map((b, i) => b ^ padBytes[i]);
    let binary = '';
    for (const byte of wrapped) binary += String.fromCharCode(byte);
    return 'xor:' + btoa(binary);
  }

  private async unwrapHmacKey(routeId: string, stored: string): Promise<string | null> {
    if (!this.sessionSecret || !stored.startsWith('xor:')) return null;
    let binary = stored.slice(4).replace(/-/g, '+').replace(/_/g, '/');
    while (binary.length % 4) binary += '=';
    const raw = atob(binary);
    const padBytes = await this.pad(routeId, raw.length);
    const keyBytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) keyBytes[i] = raw.charCodeAt(i) ^ padBytes[i];
    const key = new TextDecoder().decode(keyBytes);
    return key.length ? key : null;
  }

  // ---------------------------------------------------------------- ingest

  /** Public ingest: POST /webhooks/<route id>. No caller identity; the route
   * secret (token or HMAC) is the authentication. Size cap and rate limit run
   * before auth so unauthenticated floods cannot turn the DO into free
   * verification work. */
  async handleIngest(request: Request, path: string): Promise<Response> {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405, headers: { ...HEADERS, Allow: 'POST' } });
    }
    const id = path.split('/').pop() || '';
    const route: any = this.sql.exec('SELECT * FROM webhook_routes WHERE id = ?', id).toArray()[0];
    if (!route) return json({ error: 'not_found' }, 404);
    if (!route.enabled) return json({ error: 'route_disabled' }, 404);

    const maxBytes = Number(route.max_body_bytes) || 65536;
    const declared = Number(request.headers.get('Content-Length') || '0');
    if (declared > maxBytes) return json({ error: 'payload_too_large', max_body_bytes: maxBytes }, 413);

    const retryAfter = this.rateLimit(route.id, Number(route.rate_limit_per_min) || 30, this.now());
    if (retryAfter !== null) {
      return json({ error: 'rate_limited', retry_after_s: retryAfter }, 429, { 'Retry-After': String(retryAfter) });
    }

    let bodyText: string;
    try { bodyText = await request.text(); } catch { return json({ error: 'malformed_body' }, 400); }
    if (bodyText.length > maxBytes) return json({ error: 'payload_too_large', max_body_bytes: maxBytes }, 413);

    const authError = await this.verifyRouteAuth(request, route, bodyText);
    if (authError) return authError;

    const headerKey = request.headers.get('X-Delivery-Id') || request.headers.get('Idempotency-Key');
    let deliveryKey: string | null = null;
    if (headerKey) {
      if (!DELIVERY_KEY_RE.test(headerKey)) return json({ error: 'invalid_delivery_id' }, 400);
      deliveryKey = headerKey;
      const replay: any = this.sql.exec(
        'SELECT * FROM session_deliveries WHERE source_id = ? AND delivery_key = ?', route.id, deliveryKey).toArray()[0];
      if (replay) return json({ delivery_id: replay.id, status: replay.status, replay: true });
    }
    if (!deliveryKey) deliveryKey = 'd-' + crypto.randomUUID();

    const { text, warnings } = renderTemplate(route.template, bodyText);
    const insert = this.txn(() => {
      // Re-check inside the transaction: a concurrent retry with the same
      // delivery id must attach to the original, never duplicate.
      const existing: any = this.sql.exec(
        'SELECT * FROM session_deliveries WHERE source_id = ? AND delivery_key = ?', route.id, deliveryKey).toArray()[0];
      if (existing) return { row: existing, replayed: true };
      const at = this.at();
      const deliveryId = crypto.randomUUID();
      this.sql.exec(
        `INSERT INTO session_deliveries (id, source, source_id, delivery_key, target_label, body, status, attempts, next_attempt_at, created_at, updated_at)
         VALUES (?, 'webhook', ?, ?, ?, ?, 'pending', 0, ?, ?, ?)`,
        deliveryId, route.id, deliveryKey, route.target_label, text, at, at, at);
      this.audit('webhook.receive', 'webhook:' + route.name, route.name,
        { delivery_id: deliveryId, bytes: bodyText.length, warnings: warnings.length ? warnings : undefined });
      return { row: this.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', deliveryId).toArray()[0], replayed: false };
    });
    if (insert.replayed) return json({ delivery_id: insert.row.id, status: insert.row.status, replay: true });

    // Fast path: attempt delivery inline so live targets receive the payload
    // immediately; failures stay in the durable queue for the alarm pump.
    try { await this.pump(); } catch { /* queue handles it on the next tick */ }
    const row: any = this.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', insert.row.id).toArray()[0];
    return json({ delivery_id: row.id, status: row.status, replay: false }, 202);
  }

  private rateLimit(routeId: string, limit: number, nowMs: number): number | null {
    return this.txn(() => {
      const bucket = Math.floor(nowMs / 60_000);
      const cutoff = Math.floor((nowMs - RATE_PRUNE_MS) / 60_000);
      this.sql.exec('DELETE FROM webhook_rate_buckets WHERE webhook_id = ? AND bucket < ?', routeId, cutoff);
      const row: any = this.sql.exec('SELECT count FROM webhook_rate_buckets WHERE webhook_id = ? AND bucket = ?', routeId, bucket).toArray()[0];
      const count = Number(row?.count || 0);
      if (count >= limit) return Math.max(1, 60 - Math.floor((nowMs % 60_000) / 1000));
      this.sql.exec(
        'INSERT INTO webhook_rate_buckets (webhook_id, bucket, count) VALUES (?, ?, 1) ON CONFLICT (webhook_id, bucket) DO UPDATE SET count = count + 1',
        routeId, bucket);
      return null;
    });
  }

  private async verifyRouteAuth(request: Request, route: any, bodyText: string): Promise<Response | null> {
    const stored = String(route.secret_storage || '');
    if (route.auth_mode === 'hmac') {
      const key = await this.unwrapHmacKey(route.id, stored);
      if (!key) return json({ error: 'server_config' }, 503);
      const timestamp = request.headers.get('X-Webhook-Timestamp') || '';
      const signature = request.headers.get('X-Webhook-Signature') || '';
      const tsMs = Number(timestamp) * 1000;
      if (!/^\d+$/.test(timestamp) || !Number.isFinite(tsMs) || Math.abs(this.now() - tsMs) > HMAC_TOLERANCE_MS) {
        return json({ error: 'unauthorized', detail: 'stale_or_missing_timestamp' }, 401);
      }
      const expected = await hmacHex(key, `${timestamp}.${bodyText}`);
      if (!signature.startsWith('sha256=') || !constantTimeEqual(signature.slice(7), expected)) {
        return json({ error: 'unauthorized', detail: 'bad_signature' }, 401);
      }
      return null;
    }
    if (!stored.startsWith('sha256:')) return json({ error: 'server_config' }, 503);
    const given = request.headers.get('X-Webhook-Token') || '';
    if (!given) return json({ error: 'unauthorized', detail: 'missing_token' }, 401);
    const givenHash = await sha256Hex(given);
    if (!constantTimeEqual(givenHash, stored.slice(7))) return json({ error: 'unauthorized', detail: 'bad_token' }, 401);
    return null;
  }

  // ------------------------------------------------------------- admin API

  async handle(request: Request, auth: Auth): Promise<Response> {
    const caller = auth.caller;
    if (!caller?.isAdmin) return auth.error || json({ error: 'forbidden' }, 403);
    const url = new URL(request.url);
    const path = url.pathname;
    const actor = caller.actor || 'admin';

    if (path === '/api/webhooks' && request.method === 'POST') {
      let input: any;
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return this.create(input, actor);
    }
    if (path === '/api/webhooks' && request.method === 'GET') {
      const rows = this.sql.exec('SELECT * FROM webhook_routes ORDER BY created_at DESC LIMIT 200').toArray() as any[];
      return json({ webhooks: rows.map(row => this.publicRoute(row)) });
    }
    const match = /^\/api\/webhooks\/([^/]+)(\/(deliveries|test))?$/.exec(path);
    if (!match) return json({ error: 'not_found' }, 404);
    const route: any = this.sql.exec('SELECT * FROM webhook_routes WHERE id = ? OR name = ?', match[1], match[1]).toArray()[0];
    if (!route) return json({ error: 'not_found' }, 404);
    if (match[3] === 'test' && request.method === 'POST') {
      let input: any = {};
      try { input = await request.json(); } catch { return json({ error: 'malformed_json' }, 400); }
      return await this.enqueueTest(route, input, actor);
    }
    if (match[3] === 'deliveries' && request.method === 'GET') {
      const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit')) || 50));
      const rows = this.sql.exec(
        'SELECT * FROM session_deliveries WHERE source_id = ? ORDER BY created_at DESC, id DESC LIMIT ?', route.id, limit).toArray() as any[];
      return json({ deliveries: rows.map(row => this.publicDelivery(row)) });
    }
    if (request.method === 'GET') {
      const deliveries = this.sql.exec(
        'SELECT * FROM session_deliveries WHERE source_id = ? ORDER BY created_at DESC, id DESC LIMIT 20', route.id).toArray() as any[];
      const audits = this.sql.exec(
        "SELECT actor, action, target, detail, at FROM audit WHERE resource = 'webhook' AND target = ? ORDER BY at DESC, id DESC LIMIT 20", route.name).toArray();
      return json({ ...this.publicRoute(route), deliveries: deliveries.map(row => this.publicDelivery(row)), audit: audits });
    }
    if (request.method === 'DELETE') {
      return this.txn(() => {
        this.sql.exec('DELETE FROM webhook_routes WHERE id = ?', route.id);
        this.sql.exec('DELETE FROM webhook_rate_buckets WHERE webhook_id = ?', route.id);
        this.audit('webhook.delete', actor, route.name, { webhook_id: route.id });
        return json({ deleted: true, webhook_id: route.id });
      });
    }
    return json({ error: 'method_not_allowed' }, 405);
  }

  private async create(input: any, actor: string): Promise<Response> {
    if (!input || typeof input !== 'object') return json({ error: 'invalid_request' }, 400);
    const name = typeof input.name === 'string' ? input.name : '';
    const targetLabel = typeof input.target_label === 'string' ? input.target_label.trim() : '';
    if (!NAME_RE.test(name)) return json({ error: 'invalid_name', detail: 'lowercase slug, 2-64 chars' }, 400);
    if (!targetLabel || targetLabel.length > 200) return json({ error: 'invalid_target_label' }, 400);
    const template = input.template === undefined || input.template === null ? null : input.template;
    if (template !== null && (typeof template !== 'string' || template.length > 4096)) return json({ error: 'invalid_template' }, 400);
    const authMode = input.auth_mode === undefined ? 'token' : input.auth_mode;
    if (authMode !== 'token' && authMode !== 'hmac') return json({ error: 'invalid_auth_mode' }, 400);
    if (authMode === 'hmac' && !this.sessionSecret) return json({ error: 'hmac_unavailable', detail: 'server session secret not configured' }, 503);
    const rate = input.rate_limit_per_min === undefined ? 30 : Number(input.rate_limit_per_min);
    if (!Number.isInteger(rate) || rate < 1 || rate > 600) return json({ error: 'invalid_rate_limit' }, 400);
    const maxBody = input.max_body_bytes === undefined ? 65536 : Number(input.max_body_bytes);
    if (!Number.isInteger(maxBody) || maxBody < 1024 || maxBody > 262144) return json({ error: 'invalid_max_body_bytes' }, 400);
    const enabled = input.enabled === undefined ? true : Boolean(input.enabled);

    const secret = randomToken();
    const id = crypto.randomUUID();
    const secretStorage = authMode === 'token' ? 'sha256:' + await sha256Hex(secret) : await this.wrapHmacKey(id, secret);
    if (!secretStorage) return json({ error: 'hmac_unavailable' }, 503);

    return this.txn(() => {
      if (this.sql.exec('SELECT id FROM webhook_routes WHERE name = ?', name).toArray().length) {
        return json({ error: 'name_taken' }, 409);
      }
      const at = this.at();
      this.sql.exec(
        `INSERT INTO webhook_routes (id, name, target_label, template, auth_mode, secret_storage, rate_limit_per_min, max_body_bytes, enabled, created_by, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        id, name, targetLabel, template, authMode, secretStorage, rate, maxBody, enabled ? 1 : 0, actor, at, at);
      this.audit('webhook.create', actor, name, { webhook_id: id, auth_mode: authMode, target_label: targetLabel });
      // The secret appears exactly once, in this create response; every other
      // surface (list/show/UI) reports routes without secret material.
      return json({
        id, name, target_label: targetLabel, url_path: '/webhooks/' + id,
        auth_mode: authMode, rate_limit_per_min: rate, max_body_bytes: maxBody,
        enabled, created_at: at, secret,
        secret_note: 'shown once; store it now (private 0600 file, never logs/reports)',
      }, 201);
    });
  }

  private publicRoute(row: any) {
    return {
      id: row.id, name: row.name, target_label: row.target_label, template: row.template,
      auth_mode: row.auth_mode, rate_limit_per_min: row.rate_limit_per_min,
      max_body_bytes: row.max_body_bytes, enabled: Boolean(row.enabled),
      url_path: '/webhooks/' + row.id, created_at: row.created_at, updated_at: row.updated_at,
    };
  }

  private publicDelivery(row: any) {
    return {
      id: row.id, source: row.source, delivery_key: row.delivery_key, target_label: row.target_label,
      status: row.status, session_id: row.session_id, command_id: row.command_id,
      attempts: row.attempts, next_attempt_at: row.next_attempt_at, last_error: row.last_error,
      body_bytes: (row.body || '').length, created_at: row.created_at, updated_at: row.updated_at,
      delivered_at: row.delivered_at,
    };
  }

  private async enqueueTest(route: any, input: any, actor: string): Promise<Response> {
    const payload = input?.payload;
    let bodyText: string;
    if (payload === undefined || payload === null) {
      bodyText = JSON.stringify({ test: true, webhook: route.name, at: this.at() });
    } else if (typeof payload === 'string') {
      bodyText = payload;
    } else {
      try { bodyText = JSON.stringify(payload); } catch { return json({ error: 'invalid_payload' }, 400); }
    }
    if (bodyText.length > Number(route.max_body_bytes)) return json({ error: 'payload_too_large' }, 413);
    const deliveryKey = typeof input?.delivery_key === 'string' && DELIVERY_KEY_RE.test(input.delivery_key)
      ? input.delivery_key : 'test-' + crypto.randomUUID();
    const { text, warnings } = renderTemplate(route.template, bodyText);
    const result = this.txn(() => {
      const existing: any = this.sql.exec(
        'SELECT * FROM session_deliveries WHERE source_id = ? AND delivery_key = ?', route.id, deliveryKey).toArray()[0];
      if (existing) return { delivery: existing, replayed: true };
      const at = this.at();
      const deliveryId = crypto.randomUUID();
      this.sql.exec(
        `INSERT INTO session_deliveries (id, source, source_id, delivery_key, target_label, body, status, attempts, next_attempt_at, created_at, updated_at)
         VALUES (?, 'test', ?, ?, ?, ?, 'pending', 0, ?, ?, ?)`,
        deliveryId, route.id, deliveryKey, route.target_label, text, at, at, at);
      this.audit('webhook.test', actor, route.name, { delivery_id: deliveryId });
      return { delivery: this.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', deliveryId).toArray()[0], replayed: false };
    });
    try { await this.pump(); } catch { /* queue handles it */ }
    const fresh: any = this.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', result.delivery.id).toArray()[0];
    return json({
      delivery_id: fresh.id, status: fresh.status, replay: result.replayed,
      render_warnings: warnings.length ? warnings : undefined,
    }, 202);
  }

  // -------------------------------------------------------------- delivery

  /** Resolve the route target at delivery time. Order: exact Hub UUID, exact
   * friendly name, qualified native alias (<workspace basename>:<tag>).
   * Operator-configured routes resolve across owners (admin scope); zero
   * matches are retryable (the session may not exist yet), ambiguity is a
   * permanent configuration error. */
  private resolveTarget(label: string): { session: any } | { error: string; permanent: boolean } {
    if (UUID_RE.test(label)) {
      const row = this.sql.exec('SELECT * FROM session_control_sessions WHERE id = ?', label).toArray()[0];
      if (row) return { session: row };
    }
    const rows = this.sql.exec('SELECT * FROM session_control_sessions').toArray() as any[];
    const byName = rows.filter(r => parsed(r.request_json, {}).name === label);
    if (byName.length === 1) return { session: byName[0] };
    if (byName.length > 1) return { error: 'ambiguous_target', permanent: true };
    const colon = label.indexOf(':');
    if (colon > 0 && colon < label.length - 1) {
      const base = label.slice(0, colon);
      const tag = label.slice(colon + 1);
      const byAlias = rows.filter(r => {
        const binding = r.binding_json ? parsed(r.binding_json, null) : null;
        const request = parsed(r.request_json, {});
        const ws = binding?.workspace || request?.workspace;
        const t = binding?.tag || request?.tag;
        if (!ws || !t) return false;
        const basename = String(ws).replace(/\/+$/, '').split('/').pop() || '';
        return basename === base && t === tag;
      });
      if (byAlias.length === 1) return { session: byAlias[0] };
      if (byAlias.length > 1) return { error: 'ambiguous_target', permanent: true };
    }
    return { error: 'target_not_found', permanent: false };
  }

  private async sendToSession(sessionId: string, key: string, body: string): Promise<Json> {
    const service = this.sessionControl();
    if (!service) return { status: 503 };
    const request = new Request('https://hub.internal/api/sessions/' + sessionId + '/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, body, require_ready: false }),
    });
    const caller: Caller = { kind: 'human', isAdmin: true, actor: 'hub:webhooks' };
    const response = await service.handle(request, { caller, scopes: ['admin'] });
    let payload: any = null;
    try { payload = await response.json(); } catch { /* non-JSON */ }
    return { status: response.status, json: payload };
  }

  private backoff(attempts: number): string {
    return new Date(this.now() + BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)]).toISOString();
  }

  /** Status-gated mutation: only applies when the row is still in an expected
   * state, so a stale snapshot can never move a delivery backwards. Must run
   * inside the caller's transaction — DO storage is serialized, so the
   * read-check-write sequence is atomic within it. */
  private transition(deliveryId: string, fields: Record<string, unknown>, expectStatus?: string | string[]): boolean {
    if (expectStatus) {
      const expected = Array.isArray(expectStatus) ? expectStatus : [expectStatus];
      const row: any = this.sql.exec('SELECT status FROM session_deliveries WHERE id = ?', deliveryId).toArray()[0];
      if (!row || !expected.includes(String(row.status))) return false;
    }
    const sets = Object.keys(fields).map(key => `${key} = ?`).join(', ');
    this.sql.exec(
      `UPDATE session_deliveries SET ${sets}, updated_at = ? WHERE id = ?`,
      ...Object.values(fields), this.at(), deliveryId);
    return true;
  }

  /** Absence of the target (not yet created, unbound, queue backpressure) is
   * retryable far longer than transport/refusal failures, so a webhook that
   * arrives while the session is down is still delivered when it returns. */
  private absenceError(error: string): boolean {
    if (['target_not_found', 'session_not_bound', 'queue_full', 'session_queue_full', 'command_lost', 'send_transport_error'].includes(error)) return true;
    return /^send_(404|409|429|503):/.test(error) || /^send_(404|409|429|503)$/.test(error);
  }

  private retryOrFailed(delivery: any, error: string) {
    const maxAttempts = this.absenceError(error) ? ABSENCE_MAX_ATTEMPTS : MAX_ATTEMPTS;
    this.txn(() => {
      const attempts = Number(delivery.attempts || 0) + 1;
      const applied = attempts >= maxAttempts
        ? this.transition(delivery.id, { status: 'failed', attempts, last_error: error, next_attempt_at: null }, delivery.status)
        : this.transition(delivery.id, { status: 'pending', attempts, last_error: error, next_attempt_at: this.backoff(attempts) }, delivery.status);
      if (applied && attempts >= maxAttempts) {
        this.audit('webhook.fail', 'webhook', delivery.target_label, { delivery_id: delivery.id, error, attempts });
      }
    });
  }

  /** Coalescing entry point: ingest, enqueueTest, the DO alarm and the cron
   * tick all call pump(). Passes must never interleave at the sendToSession
   * await points — two concurrent passes could select the same pending row
   * and enqueue two send commands for one delivery. Instead of early-return
   * and forget, a request that lands during an active pass sets pumpAgain so
   * the active pass drains once more and the fresh row is picked up. */
  private pumping = false;
  private pumpAgain = false;

  async pump(): Promise<number | undefined> {
    if (this.pumping) {
      this.pumpAgain = true;
      return undefined;
    }
    this.pumping = true;
    let nextDue: number | undefined;
    try {
      do {
        this.pumpAgain = false;
        nextDue = await this.pumpOnce();
      } while (this.pumpAgain);
    } finally {
      this.pumping = false;
    }
    return nextDue;
  }

  /** One bounded pass over the durable queue. Every mutation is a small
   * transaction on idempotent keys, gated on the row's current status so a
   * stale snapshot can never move a delivery backwards. Returns the next due
   * time (epoch ms) for the alarm. */
  private async pumpOnce(): Promise<number | undefined> {
    const nowMs = this.now();
    let nextDue: number | undefined;

    // 1. Queued deliveries: follow the send command's ack ledger.
    const queued = this.sql.exec(
      "SELECT * FROM session_deliveries WHERE status = 'queued' ORDER BY updated_at LIMIT ?", this.maxBatch).toArray() as any[];
    for (const delivery of queued) {
      const command: any = delivery.command_id
        ? this.sql.exec('SELECT state, ack_json FROM session_control_commands WHERE id = ?', delivery.command_id).toArray()[0]
        : null;
      if (!command) {
        // Command row vanished (retention/cleanup): redeliver with a fresh attempt.
        this.retryOrFailed(delivery, 'command_lost');
        continue;
      }
      const ack = parsed(command.ack_json, null);
      if (ack?.outcome === 'delivered') {
        const applied = this.txn(() => this.transition(delivery.id, {
          status: 'delivered', delivered_at: this.at(), last_error: null,
        }, 'queued'));
        if (applied) {
          this.audit('webhook.deliver', 'webhook', delivery.target_label,
            { delivery_id: delivery.id, session_id: delivery.session_id });
        }
        continue;
      }
      if (ack && ['refused', 'failed', 'uncertain'].includes(ack.outcome)) {
        this.retryOrFailed(delivery, ack.reason ? `${ack.outcome}:${ack.reason}` : ack.outcome);
        continue;
      }
      // Still recorded/transported/claimed: wait for the host ack ledger.
    }

    // 2. Pending deliveries due now: resolve target and enqueue a send command.
    const due = this.sql.exec(
      `SELECT * FROM session_deliveries WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
       ORDER BY next_attempt_at LIMIT ?`, this.at(), this.maxBatch).toArray() as any[];
    for (const delivery of due) {
      const resolved = this.resolveTarget(delivery.target_label);
      if ('error' in resolved) {
        if (resolved.permanent) {
          this.txn(() => this.transition(delivery.id, { status: 'failed', last_error: resolved.error, next_attempt_at: null }, 'pending'));
          this.audit('webhook.fail', 'webhook', delivery.target_label, { delivery_id: delivery.id, error: resolved.error });
        } else {
          this.retryOrFailed(delivery, resolved.error);
        }
        continue;
      }
      const session = resolved.session;
      if (!BOUND_STATES.has(session.state)) {
        this.retryOrFailed(delivery, 'session_not_bound');
        continue;
      }
      const attemptKey = delivery.attempts > 0 ? `wd-${delivery.id}-${delivery.attempts}` : `wd-${delivery.id}`;
      let result: Json;
      try {
        result = await this.sendToSession(session.id, attemptKey, delivery.body);
      } catch {
        this.retryOrFailed(delivery, 'send_transport_error');
        continue;
      }
      if (result.status === 200 || result.status === 201) {
        this.txn(() => this.transition(delivery.id, {
          status: 'queued', session_id: session.id, command_id: result.json?.command_id ?? null, last_error: null,
        }, 'pending'));
        continue;
      }
      const error = result.json?.error ? `send_${result.status}:${result.json.error}` : `send_${result.status}`;
      if (result.status === 400) {
        // 400 validates the rendered body itself: retrying cannot fix it.
        this.txn(() => this.transition(delivery.id, { status: 'failed', last_error: error, next_attempt_at: null }, 'pending'));
        this.audit('webhook.fail', 'webhook', delivery.target_label, { delivery_id: delivery.id, error });
        continue;
      }
      this.retryOrFailed(delivery, error);
    }

    // 3. Pending/queued deliveries whose route was deleted: fail them honestly.
    for (const delivery of (this.sql.exec(
      `SELECT d.id, d.target_label FROM session_deliveries d LEFT JOIN webhook_routes r ON r.id = d.source_id
       WHERE d.status IN ('pending','queued') AND d.source = 'webhook' AND r.id IS NULL LIMIT ?`, this.maxBatch).toArray() as any[])) {
      this.txn(() => this.transition(delivery.id, { status: 'failed', last_error: 'route_deleted', next_attempt_at: null }, ['pending', 'queued']));
    }

    const next: any = this.sql.exec(
      "SELECT MIN(next_attempt_at) AS due FROM session_deliveries WHERE status = 'pending'").toArray()[0];
    if (next?.due) {
      const dueMs = Date.parse(next.due);
      if (Number.isFinite(dueMs)) nextDue = Math.max(nowMs, dueMs);
    }
    return nextDue;
  }
}
