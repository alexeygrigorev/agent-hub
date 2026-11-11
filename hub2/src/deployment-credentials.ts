import { allowlisted, verifyKnownCredential } from './auth';
import { randomToken, sha256Hex } from './crypto';
import { MeteredDb } from './db';
import { actorFrom, bodyObject, error, hasScope, json } from './http';
import { DEPLOYMENT_LABEL, DEPLOYMENT_POLICY_SCOPES, DEPLOYMENT_REQUEST_ID, isDeploymentActor } from './types';
import type { Actor, Hub2Env, JsonRecord } from './types';

const NO_STORE = { 'Cache-Control': 'no-store' };
const PREFIX = '/api/deployment-credentials';
const PAGE_SIZE = 32;
const MAX_ACTIVE = 32;
type Row = Record<string, any>;

/** Hash-only, independently revocable policy capability; no refresh credential. */
export class DeploymentCredentials {
  constructor(private readonly state: DurableObjectState, private readonly db: MeteredDb, private readonly env: Hub2Env) {}

  private verified(row: Row | undefined): Actor | undefined {
    if (!row || row.kind !== 'service' || row.resource_id !== 'model-policy'
        || row.revoked_at !== null || row.expires_at !== null || row.refresh_hash !== null || row.csrf_token !== null
        || typeof row.token_hash !== 'string' || !/^[0-9a-f]{64}$/.test(row.token_hash)
        || typeof row.principal_email !== 'string' || !allowlisted(this.env, row.principal_email)
        || !Number.isSafeInteger(row.created_at) || row.created_at < 0) return;
    let scopes: unknown;
    try { scopes = JSON.parse(row.scopes_json); } catch { return; }
    const actor = {id: row.actor_id, kind: 'service', resource_id: row.resource_id,
      generation: row.generation, family_id: row.family_id, client_id: row.client_id, scopes};
    return isDeploymentActor(actor) ? actor : undefined;
  }

  verifyHash(hash: string): Actor | undefined {
    return this.verified(this.db.one<Row>('SELECT * FROM credentials WHERE token_hash=? AND kind=?', hash, 'service'));
  }

  authorize(body: JsonRecord): Response {
    if (typeof body.actor_id !== 'string' || typeof body.generation !== 'string') return error('invalid_authorization_check', 400);
    const actor = this.verified(this.db.one<Row>('SELECT * FROM credentials WHERE actor_id=? AND kind=?', body.actor_id, 'service'));
    if (!actor || actor.generation !== body.generation) return error('credential_revoked', 401);
    if (body.scope !== undefined && (typeof body.scope !== 'string' || !hasScope(actor, body.scope))) return error('forbidden', 403);
    return json({current: true, actor}, 200, NO_STORE);
  }

  private issuer(request: Request): Row | undefined {
    const actor = actorFrom(request);
    if (!actor || actor.kind !== 'operator' || !hasScope(actor, 'operator:admin')
        || !actor.generation || !actor.family_id) return;
    // Resolve current authority, not a caller's claimed email/scopes or a stale
    // projection. Both authenticated browser and bearer admins resolve here.
    const row = this.db.one<Row>('SELECT * FROM credentials WHERE resource_id=? AND generation=? AND family_id=? AND kind=?',
      actor.id, actor.generation, actor.family_id, 'operator');
    const current = row && verifyKnownCredential(row, this.env);
    return current && hasScope(current, 'operator:admin') ? row : undefined;
  }

  private view(row: Row) {
    return {id: row.actor_id, request_id: row.actor_id.slice(11), kind: 'service', purpose: 'model-policy',
      label: row.client_id, scopes: [...DEPLOYMENT_POLICY_SCOPES], expires_at: null,
      created_at: row.created_at, revoked: row.revoked_at !== null};
  }

  async fetch(request: Request): Promise<Response | undefined> {
    const url = new URL(request.url);
    if (url.pathname !== PREFIX && !url.pathname.startsWith(PREFIX + '/')) return;
    const issuer = this.issuer(request);
    if (!issuer) return error('forbidden', 403);
    if (url.pathname === PREFIX && request.method === 'GET') {
      const after = url.searchParams.get('after') || '';
      if ([...url.searchParams.keys()].some(key => key !== 'after')
          || after && (!after.startsWith('deployment:') || !DEPLOYMENT_REQUEST_ID.test(after.slice(11))))
        return error('invalid_deployment_credential_cursor', 400);
      const rows = this.db.all<Row>('SELECT actor_id,client_id,created_at,revoked_at FROM credentials WHERE actor_id>? AND actor_id<? AND kind=? ORDER BY actor_id LIMIT 33',
        after || 'deployment:', 'deployment;', 'service');
      return json({items: rows.slice(0, PAGE_SIZE).map(row => this.view(row)),
        next: rows.length > PAGE_SIZE ? rows[PAGE_SIZE - 1].actor_id : null}, 200, NO_STORE);
    }
    if (url.pathname === PREFIX && request.method === 'POST') {
      const body = await bodyObject(request, 2048);
      if (Object.keys(body).some(key => !['request_id','label'].includes(key))
          || typeof body.request_id !== 'string' || !DEPLOYMENT_REQUEST_ID.test(body.request_id)
          || typeof body.label !== 'string' || !DEPLOYMENT_LABEL.test(body.label))
        return error('invalid_deployment_credential', 400);
      const id = 'deployment:' + body.request_id, token = randomToken(), hash = await sha256Hex(token);
      return this.db.transaction(this.state, () => {
        const currentIssuer = this.issuer(request);
        if (!currentIssuer) return error('forbidden', 403);
        const prior = this.db.one<Row>('SELECT * FROM credentials WHERE actor_id=?', id);
        if (prior) return json({error: prior.kind === 'service' && prior.client_id === body.label && prior.principal_email === currentIssuer.principal_email
          ? 'deployment_credential_already_issued' : 'deployment_credential_request_conflict', id}, 409, NO_STORE);
        const active = this.db.all('SELECT actor_id FROM credentials WHERE actor_id>? AND actor_id<? AND kind=? AND revoked_at IS NULL LIMIT 32',
          'deployment:', 'deployment;', 'service');
        if (active.length >= MAX_ACTIVE) return error('deployment_credential_capacity', 409);
        this.db.run('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,refresh_hash,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)',
          id, 'service', 'model-policy', crypto.randomUUID(), hash, JSON.stringify(DEPLOYMENT_POLICY_SCOPES),
          crypto.randomUUID(), body.label, Date.now(), currentIssuer.principal_email);
        // The sole plaintext disclosure is this authenticated creation response.
        // A lost response requires revoking this deterministic ID and a new ID.
        return json({credential: this.view(this.db.one<Row>('SELECT * FROM credentials WHERE actor_id=?', id)!), access_token: token, token_type: 'Bearer'}, 201, NO_STORE);
      });
    }
    const revoke = url.pathname.match(/^\/api\/deployment-credentials\/(deployment:([0-9a-f-]+))\/revoke$/);
    if (revoke && request.method === 'POST' && DEPLOYMENT_REQUEST_ID.test(revoke[2])) {
      if (Object.keys(await bodyObject(request, 2048)).length) return error('invalid_deployment_credential_revoke', 400);
      return this.db.transaction(this.state, () => {
        if (!this.issuer(request)) return error('forbidden', 403);
        const row = this.db.one<Row>('SELECT * FROM credentials WHERE actor_id=? AND kind=?', revoke[1], 'service');
        if (!row) return error('deployment_credential_not_found', 404);
        this.db.run('UPDATE credentials SET revoked_at=COALESCE(revoked_at,?) WHERE actor_id=? AND kind=?', Date.now(), revoke[1], 'service');
        return json({id: revoke[1], revoked: true, replay: row.revoked_at !== null}, 200, NO_STORE);
      });
    }
    return error('not_found', 404);
  }
}
