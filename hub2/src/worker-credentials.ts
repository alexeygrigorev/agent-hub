import { MeteredDb } from './db';
import { bodyObject, error, HttpError, json } from './http';
import { randomToken, sha256Hex } from './crypto';
import { decode64, encode64 } from './auth';
import type { Hub2Env } from './types';
export const WORKER_CREDENTIAL_SCHEMA = [
  'CREATE TABLE worker_credentials(session_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,machine_id TEXT NOT NULL,intake_id TEXT NOT NULL,project_id TEXT NOT NULL,deadline INTEGER NOT NULL,sealed_token TEXT NOT NULL,revoked_at INTEGER,PRIMARY KEY(session_id,epoch)) WITHOUT ROWID',
  'CREATE INDEX worker_credentials_machine ON worker_credentials(machine_id,revoked_at,deadline,session_id,epoch)',
];
type Binding = {
  session_id: string;
  epoch: number;
  operation_id: string;
  machine_id: string;
  intake_id: string;
  project_id: string;
  deadline: number;
  machine_generation: string;
};
type Grant = Binding & { sealed_token: string; revoked_at: number | null };
const fields = ['session_id', 'operation_id', 'machine_id', 'intake_id', 'project_id'] as const;
export class WorkerCredentials {
  private key?: Promise<CryptoKey>;
  constructor(
    private ctx: DurableObjectState,
    private db: MeteredDb,
    private env: Hub2Env,
  ) {}
  private actor(b: Binding) {
    return 'session:' + b.session_id;
  }
  private generation(b: Binding) {
    return 'worker:' + b.epoch + ':' + b.operation_id;
  }
  private currentHost(b: Binding): boolean {
    return !!this.db.one(
      'SELECT actor_id FROM credentials WHERE kind=? AND resource_id=? AND generation=? AND revoked_at IS NULL',
      'machine',
      b.machine_id,
      b.machine_generation,
    );
  }
  private same(a: Grant, b: Binding): boolean {
    return a.epoch === b.epoch && fields.every((k) => a[k] === b[k]);
  }
  private async crypt(token: string, b: Binding, open = false): Promise<string> {
    if (!this.key) {
      const raw = this.env.HUB2_CREDENTIAL_KEY;
      if (!raw || !/^[0-9a-f]{64}$/i.test(raw)) throw new HttpError('worker_key_unavailable', 503);
      this.key = crypto.subtle.importKey(
        'raw',
        new Uint8Array(raw.match(/../g)!.map((v) => parseInt(v, 16))),
        'AES-GCM',
        false,
        ['encrypt', 'decrypt'],
      );
    }
    const aad = new TextEncoder().encode(
      JSON.stringify(['hub2-worker-credential', ...fields.map((k) => b[k]), b.epoch, b.deadline]),
    );
    if (open) {
      const [iv, cipher] = token.split('.');
      return new TextDecoder().decode(
        await crypto.subtle.decrypt(
          { name: 'AES-GCM', iv: decode64(iv), additionalData: aad },
          await this.key,
          decode64(cipher),
        ),
      );
    }
    const iv = crypto.getRandomValues(new Uint8Array(12)),
      sealed = await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv, additionalData: aad },
        await this.key,
        new TextEncoder().encode(token),
      );
    return encode64(iv) + '.' + encode64(new Uint8Array(sealed));
  }
  async fetch(request: Request): Promise<Response | undefined> {
    const path = new URL(request.url).pathname;
    if (!['/_worker/issue', '/_worker/revoke'].includes(path)) return;
    if (request.method !== 'POST') return error('method_not_allowed', 405);
    const b = (await bodyObject(request, 4096)) as unknown as Binding;
    if (
      !Number.isSafeInteger(b.epoch) ||
      b.epoch < 1 ||
      fields.some((k) => typeof b[k] !== 'string' || !b[k] || b[k].length > 180)
    )
      return error('invalid_worker_binding', 400);
    if (path === '/_worker/revoke') return this.revoke(b);
    if (
      !Number.isSafeInteger(b.deadline) ||
      b.deadline <= Date.now() ||
      b.deadline > Date.now() + 86400000 ||
      typeof b.machine_generation !== 'string' ||
      b.machine_generation.length > 180
    )
      return error('invalid_worker_deadline', 400);
    const route = this.db.one<{
      owner: string;
      resource_id: string;
      bound_intake_id: string | null;
    }>(
      'SELECT owner,resource_id,bound_intake_id FROM object_routes WHERE kind=? AND id=?',
      'session',
      b.session_id,
    );
    if (
      !route ||
      route.owner !== b.machine_id ||
      route.resource_id !== b.project_id ||
      route.bound_intake_id !== b.intake_id ||
      !this.currentHost(b)
    )
      return error('worker_owner_mismatch', 403);
    let selected = this.db.one<Grant>(
        'SELECT * FROM worker_credentials WHERE session_id=? AND epoch=?',
        b.session_id,
        b.epoch,
      ),
      token: string | undefined;
    if (!selected) {
      token = randomToken();
      const sealed = await this.crypt(token, b),
        hash = await sha256Hex(token);
      selected = this.db.transaction(this.ctx, () => {
        if (!this.currentHost(b)) throw new HttpError('worker_host_revoked', 401);
        const prior = this.db.one<Grant>(
          'SELECT * FROM worker_credentials WHERE session_id=? AND epoch=?',
          b.session_id,
          b.epoch,
        );
        if (prior) return prior;
        const newer = this.db.one<{ epoch: number }>(
          'SELECT epoch FROM worker_credentials WHERE session_id=? ORDER BY epoch DESC LIMIT 1',
          b.session_id,
        );
        if (newer && newer.epoch >= b.epoch) throw new HttpError('stale_worker_epoch', 409);
        this.db.run(
          'UPDATE worker_credentials SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL',
          Date.now(),
          b.session_id,
        );
        this.db.run(
          'INSERT INTO worker_credentials(session_id,epoch,operation_id,machine_id,intake_id,project_id,deadline,sealed_token) VALUES(?,?,?,?,?,?,?,?)',
          b.session_id,
          b.epoch,
          b.operation_id,
          b.machine_id,
          b.intake_id,
          b.project_id,
          b.deadline,
          sealed,
        );
        const actor = this.actor(b),
          scopes = [
            'intake:read',
            'intake:append',
            'claim:read',
            'claim:write',
            'message:read',
            'message:send',
            'mailbox:' + actor,
          ];
        this.db.run(
          `INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET kind=excluded.kind,resource_id=excluded.resource_id,generation=excluded.generation,token_hash=excluded.token_hash,scopes_json=excluded.scopes_json,family_id=excluded.family_id,client_id=excluded.client_id,created_at=excluded.created_at,expires_at=excluded.expires_at,revoked_at=NULL,principal_email=NULL,refresh_hash=NULL,csrf_token=NULL`,
          actor,
          'agent',
          b.intake_id,
          this.generation(b),
          hash,
          JSON.stringify(scopes),
          'worker:' + b.session_id + ':' + b.epoch,
          'native-worker',
          Date.now(),
          b.deadline,
        );
        return { ...b, sealed_token: sealed, revoked_at: null };
      });
      if (selected.sealed_token !== sealed) token = undefined;
    }
    if (!this.same(selected, b) || selected.revoked_at !== null || selected.deadline <= Date.now())
      return error('worker_grant_unavailable', 409);
    token ??= await this.crypt(selected.sealed_token, selected, true);
    if (
      !this.currentHost(b) ||
      this.db.one<Grant>(
        'SELECT * FROM worker_credentials WHERE session_id=? AND epoch=?',
        b.session_id,
        b.epoch,
      )?.revoked_at !== null
    )
      return error('worker_grant_revoked', 401);
    return json({
      token,
      actor: this.actor(b),
      expires_at: selected.deadline,
      ...Object.fromEntries(fields.map((k) => [k, b[k]])),
      epoch: b.epoch,
    },200,{'Cache-Control':'private, no-store'});
  }
  private revoke(b: Binding): Response {
    return this.db.transaction(this.ctx, () => {
      const row = this.db.one<Grant>(
        'SELECT * FROM worker_credentials WHERE session_id=? AND epoch=?',
        b.session_id,
        b.epoch,
      );
      if (!row) {
        // A concurrent issue may be awaiting encryption. Persist the negative
        // result so its publish transaction cannot resurrect a finished child.
        this.db.run('INSERT INTO worker_credentials(session_id,epoch,operation_id,machine_id,intake_id,project_id,deadline,sealed_token,revoked_at) VALUES(?,?,?,?,?,?,0,?,?)', b.session_id,b.epoch,b.operation_id,b.machine_id,b.intake_id,b.project_id,'',Date.now());
        return json({revoked:true,absent:true});
      }
      if (!this.same(row, b)) return error('worker_binding_mismatch', 409);
      this.db.run(
        'UPDATE worker_credentials SET revoked_at=COALESCE(revoked_at,?) WHERE session_id=? AND epoch=?',
        Date.now(),
        b.session_id,
        b.epoch,
      );
      this.db.run(
        'UPDATE credentials SET revoked_at=COALESCE(revoked_at,?) WHERE actor_id=? AND generation=?',
        Date.now(),
        this.actor(b),
        this.generation(b),
      );
      return json({ revoked: true });
    });
  }
  revokeMachine(machine: string): void {
    const rows = this.db.all<Grant>(
      'SELECT * FROM worker_credentials WHERE machine_id=? AND revoked_at IS NULL AND deadline>? ORDER BY deadline,session_id,epoch LIMIT 65',
      machine,
      Date.now(),
    );
    if (rows.length > 64) throw new HttpError('worker_revocation_bound', 503);
    for (const row of rows) this.revoke(row);
  }
}
