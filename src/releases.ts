// Orchestrator release slots (intake 2c87c231): every deploy of the Hub Worker
// must consume a slot the orchestrator issued for the exact target (commit SHA,
// or a rollback version id). A slot is single-use and expires; the CI deploy
// job consumes it through POST /api/releases/slots/verify before any deploy
// step runs; main-push CI performs a read-only check before the test job.
//
// Load bounds (lesson from 90336bb5): every statement is an indexed lookup or
// a bounded LIMIT read. The public verify endpoint never writes on a failed
// attempt, only on a successful atomic consume.

const SHA_RE = /^[0-9a-f]{40}$/;
// Slot ids are public capability credentials. Require a random UUID v4 so a
// mistaken short/client-chosen value cannot make a live grant guessable.
const SLOT_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const TARGET_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const CONSUMER_RE = /^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,127}$/;
const NOTE_MAX = 512;
const DEFAULT_TTL_MS = 48 * 3600000;
const MAX_TTL_MS = 14 * 24 * 3600000;
const LIST_LIMIT_DEFAULT = 50;
const LIST_LIMIT_MAX = 200;

const reply = (data: unknown, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'private, no-store' } });
const obj = (x: any, keys: string[]) => x && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).every(k => keys.includes(k));

export async function boundedJson(request: Request, limit = 8192): Promise<any> {
  if (!request.body) return null;
  const reader = request.body.getReader();
  let size = 0;
  const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) { await reader.cancel(); return null; }
      chunks.push(value);
    }
    const all = new Uint8Array(size);
    let pos = 0;
    for (const chunk of chunks) { all.set(chunk, pos); pos += chunk.length; }
    return JSON.parse(new TextDecoder().decode(all));
  } catch { return null; } finally { reader.releaseLock(); }
}

export class ReleaseSlotService {
  constructor(private sql: SqlStorage, private now = Date.now, private transaction: <T>(fn: () => T) => T = fn => fn()) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS release_slots (
      id TEXT PRIMARY KEY,
      target TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'issued',
      issued_by TEXT NOT NULL,
      issued_at INTEGER NOT NULL,
      expires_at INTEGER NOT NULL,
      consumed_at INTEGER,
      consumer TEXT,
      note TEXT
    )`);
    // The partial unique index is both the one-active-slot-per-target guard
    // and the index behind "which slot is live for this SHA".
    this.sql.exec(`CREATE UNIQUE INDEX IF NOT EXISTS release_slots_active_target ON release_slots(target) WHERE status = 'issued'`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS release_slots_status_issued_at ON release_slots(status, issued_at DESC)`);
    this.sql.exec(`CREATE INDEX IF NOT EXISTS release_slots_issued_at ON release_slots(issued_at DESC)`);
  }

  private view(row: any) {
    return {
      id: row.id,
      target: row.target,
      sha: SHA_RE.test(String(row.target)) ? row.target : null,
      status: row.status,
      issued_by: row.issued_by,
      issued_at: row.issued_at,
      expires_at: row.expires_at,
      consumed_at: row.consumed_at ?? null,
      consumer: row.consumer ?? null,
      note: row.note ?? null,
    };
  }

  private at() { return new Date(this.now()).toISOString(); }

  private async requireAdmin(auth: any): Promise<Response | null> {
    if (auth?.error) return auth.error;
    if (!auth?.caller) return reply({ error: 'unauthorized' }, 401);
    if (auth.caller.isAdmin !== true) return reply({ error: 'forbidden', reason: 'admin_required' }, 403);
    return null;
  }

  async handle(request: Request, auth: any): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // Public capability endpoint: knowing the unguessable slot id IS the
    // credential. Push CI checks the grant without consuming it; the manual
    // production deploy consumes it atomically. Failures and check-only
    // requests are read-only indexed lookups.
    if (path === '/api/releases/slots/verify' && request.method === 'POST') {
      const data = await boundedJson(request);
      if (!obj(data, ['slot', 'target', 'actor', 'consume']) || !SLOT_RE.test(String(data.slot)) || !TARGET_RE.test(String(data.target)) ||
          (data.consume !== undefined && typeof data.consume !== 'boolean')) {
        return reply({ error: 'invalid_request' }, 400);
      }
      const consume = data.consume !== false;
      const consumer = data.actor === undefined ? 'ci-deploy'
        : (typeof data.actor === 'string' && CONSUMER_RE.test(data.actor) ? data.actor : null);
      if (consumer === null) return reply({ error: 'invalid_request' }, 400);
      const at = this.now();
      const result = this.transaction(() => {
        const row = this.sql.exec('SELECT * FROM release_slots WHERE id = ?', data.slot).toArray()[0];
        if (!row) return { status: 404, body: { error: 'release_slot_not_found' } };
        if (row.status === 'consumed') return { status: 409, body: { error: 'release_slot_already_consumed', consumed_at: row.consumed_at } };
        if (row.status === 'revoked') return { status: 409, body: { error: 'release_slot_revoked' } };
        if (Number(row.expires_at) <= at) return { status: 409, body: { error: 'release_slot_expired', expires_at: Number(row.expires_at) } };
        if (row.target !== String(data.target)) return { status: 403, body: { error: 'release_slot_target_mismatch' } };
        if (!consume) return { status: 200, body: { ok: true, id: row.id, target: row.target, verified: true, consumed: false } };
        this.sql.exec('UPDATE release_slots SET status = ?, consumed_at = ?, consumer = ? WHERE id = ?', 'consumed', at, consumer, row.id);
        this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
          consumer, 'release_slot_consume', row.id, row.target, '', this.at());
        return { status: 200, body: { ok: true, id: row.id, target: row.target, consumed: true, consumed_at: at } };
      });
      return reply(result.body, result.status);
    }

    if (path === '/api/releases/slots' || path.startsWith('/api/releases/slots/')) {
      const denied = await this.requireAdmin(auth);
      if (denied) return denied;
      const caller = auth.caller;

      if (path === '/api/releases/slots' && request.method === 'POST') {
        const data = await boundedJson(request);
        if (!obj(data, ['sha', 'target', 'id', 'ttl_hours', 'note'])) return reply({ error: 'invalid_body' }, 400);
        let target: string | null = null;
        if (typeof data.sha === 'string') {
          if (data.target !== undefined) return reply({ error: 'invalid_body', reason: 'sha_and_target' }, 400);
          target = data.sha.toLowerCase();
          // A SHA slot id must already be present in the commit trailer. The
          // orchestrator chooses a random UUID before the commit is finalized,
          // then issues it against the resulting exact SHA.
          if (data.id === undefined) return reply({ error: 'release_slot_id_required' }, 400);
        } else if (typeof data.target === 'string') {
          target = SHA_RE.test(data.target.toLowerCase()) ? data.target.toLowerCase() : data.target;
        }
        if (!target || !(SHA_RE.test(target) || TARGET_RE.test(target))) return reply({ error: 'invalid_target' }, 400);
        if (SHA_RE.test(target) && data.id === undefined) return reply({ error: 'release_slot_id_required' }, 400);
        if (data.id !== undefined && !(typeof data.id === 'string' && SLOT_RE.test(data.id))) return reply({ error: 'invalid_slot_id' }, 400);
        if (data.note !== undefined && !(typeof data.note === 'string' && data.note.length <= NOTE_MAX)) return reply({ error: 'invalid_note' }, 400);
        let ttl = DEFAULT_TTL_MS;
        if (data.ttl_hours !== undefined) {
          if (typeof data.ttl_hours !== 'number' || !Number.isFinite(data.ttl_hours) || data.ttl_hours <= 0 || data.ttl_hours * 3600000 > MAX_TTL_MS) {
            return reply({ error: 'invalid_ttl' }, 400);
          }
          ttl = Math.round(data.ttl_hours * 3600000);
        }
        const id = data.id ?? crypto.randomUUID();
        const at = this.now();
        return this.transaction(() => {
          if (this.sql.exec('SELECT id FROM release_slots WHERE id = ?', id).toArray().length) {
            return reply({ error: 'release_slot_exists' }, 409);
          }
          // One live slot per target: an expired predecessor is superseded
          // (marked revoked) in the same transaction so re-issue never needs
          // a separate cleanup call.
          const active = this.sql.exec("SELECT id, expires_at FROM release_slots WHERE target = ? AND status = 'issued'", target).toArray()[0];
          if (active) {
            if (Number(active.expires_at) > at) return reply({ error: 'release_slot_target_active', active_id: active.id }, 409);
            this.sql.exec("UPDATE release_slots SET status = 'revoked', note = COALESCE(note, '') WHERE id = ?", active.id);
          }
          this.sql.exec('INSERT INTO release_slots (id, target, status, issued_by, issued_at, expires_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)',
            id, target, 'issued', caller.actor || 'admin', at, at + ttl, data.note ?? null);
          this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
            caller.actor || 'admin', 'release_slot_issue', id, target, '', this.at());
          return reply({ ...this.view(this.sql.exec('SELECT * FROM release_slots WHERE id = ?', id).toArray()[0]) }, 201);
        });
      }

      if (path === '/api/releases/slots' && request.method === 'GET') {
        const limitRaw = Number(url.searchParams.get('limit') || LIST_LIMIT_DEFAULT);
        const limit = Number.isFinite(limitRaw) ? Math.max(1, Math.min(LIST_LIMIT_MAX, Math.floor(limitRaw))) : LIST_LIMIT_DEFAULT;
        const status = url.searchParams.get('status');
        if (status !== null && !['issued', 'consumed', 'revoked'].includes(status)) return reply({ error: 'invalid_status' }, 400);
        const rows = (status
          ? this.sql.exec('SELECT * FROM release_slots WHERE status = ? ORDER BY issued_at DESC LIMIT ?', status, limit)
          : this.sql.exec('SELECT * FROM release_slots ORDER BY issued_at DESC LIMIT ?', limit)).toArray() as any[];
        return reply({ slots: rows.map(r => this.view(r)), limit });
      }

      const revokeMatch = /^\/api\/releases\/slots\/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/revoke$/i.exec(path);
      if (revokeMatch && request.method === 'POST') {
        const id = revokeMatch[1];
        const at = this.now();
        return this.transaction(() => {
          const row = this.sql.exec('SELECT * FROM release_slots WHERE id = ?', id).toArray()[0];
          if (!row) return reply({ error: 'release_slot_not_found' }, 404);
          if (row.status !== 'issued') return reply({ error: 'release_slot_not_issued', status: row.status }, 409);
          this.sql.exec("UPDATE release_slots SET status = 'revoked' WHERE id = ?", id);
          this.sql.exec('INSERT INTO audit (actor, action, resource, target, detail, at) VALUES (?, ?, ?, ?, ?, ?)',
            caller.actor || 'admin', 'release_slot_revoke', id, row.target, '', this.at());
          return reply(this.view(this.sql.exec('SELECT * FROM release_slots WHERE id = ?', id).toArray()[0]));
        });
      }
      const idMatch = /^\/api\/releases\/slots\/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$/i.exec(path);
      if (idMatch && request.method === 'GET') {
        const id = idMatch[1];
        const row = this.sql.exec('SELECT * FROM release_slots WHERE id = ?', id).toArray()[0];
        if (!row) return reply({ error: 'release_slot_not_found' }, 404);
        return reply(this.view(row));
      }
      return reply({ error: 'not_found' }, 404);
    }
    return reply({ error: 'not_found' }, 404);
  }
}
