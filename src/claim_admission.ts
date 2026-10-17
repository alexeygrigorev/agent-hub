import { credentialActor } from './agent_registry';
/** Ownership-aware launch admission over the existing /api/bus/claims table. */
import { BusService, parseAddress, isRootRoleAddress, type CallerContext } from './bus';
import type { SqlStorage } from '@cloudflare/workers-types';

const ACTIVE = "('offered','starting','running','uncertain')";
const LABEL = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const CLAIM_ID = /^[0-9A-HJKMNP-TV-Z]{26}$/i;

export type AccessMode = 'read' | 'edit' | 'legacy_unspecified';

export type AccessDeclaration = {
  mode: AccessMode;
  claim_id: string | null;
  project: string | null;
  scopes: string[];
};

export type AdmissionPhase = 'enqueue' | 'reserve' | 'start';
export type AdmissionCaller = Omit<CallerContext, 'kind'> & { kind?: string };

/** Server-derived references only. No bearer, token hash, or caller-supplied authority. */
export type EditAuthority = {
  actor: string;
  credential_id: string;
  permission: 'fleet:launch' | 'routing:submit';
  claim: { id: string; project: string; scope: string; holder: string; created_at: number };
};
export type AdmissionOk = { ok: true; access: AccessDeclaration; authority: EditAuthority | null };
export type AdmissionErr = { ok: false; error: string; status: number };
export type AdmissionResult = AdmissionOk | AdmissionErr;

function object(value: any, keys: string[]): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => keys.includes(k));
}

export function normalizeScope(scope: unknown): string | null {
  if (typeof scope !== 'string' || !scope || scope.length > 256) return null;
  // Claims use slash-separated relative prefixes. Reject alternate host path syntax.
  if (scope.includes('..') || scope.startsWith('/') || /[\\:\x00-\x1f\x7f]/.test(scope)) return null;
  const normalized = scope.split('/').filter(p => p && p !== '.').join('/');
  return normalized || null;
}

export function claimCoversScope(claimScope: string, requested: string): boolean {
  return requested === claimScope || requested.startsWith(claimScope + '/');
}

export function boundAccess(access: AccessDeclaration): AccessDeclaration {
  return {
    mode: access.mode,
    claim_id: access.claim_id,
    project: access.project,
    scopes: [...access.scopes],
  };
}

export class ClaimAdmission {
  public bus: BusService;
  private offerCursor = '';
  constructor(private sql: SqlStorage, private transaction: <T>(fn: () => T) => T = fn => fn(), private now: () => number = Date.now) {
    this.bus = new BusService(sql, transaction);
  }

  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS fleet_claim_policy (workspace TEXT PRIMARY KEY, project TEXT, require_claim INTEGER NOT NULL)');
  }

  /** Called inside the shared transaction; only offered work has no start authorization. */
  retireInvalidOffer(row: any): string | null {
    if (row.state !== 'offered') return null;
    const reason = this.heldReason(row);
    if (!reason) return null;
    const at = new Date(this.now()).toISOString();
    this.sql.exec("UPDATE fleet_launches SET state = 'failed', verification_status = 'not_started', exit_reason = ?, finished_at = ?, updated_at = ? WHERE id = ? AND state = 'offered'", reason, at, at, row.id);
    this.sql.exec("UPDATE routing_jobs SET state = 'failed', report = ?, updated_at = ? WHERE launch_id = ?", 'Not started: ' + reason, at, row.id);
    return reason;
  }

  /** Bounded circular keyset sweep, without a second lock registry. */
  reconcileOffers() {
    const cursor = this.transaction(() => {
      const rows = this.sql.exec("SELECT * FROM fleet_launches WHERE state = 'offered' AND id > ? ORDER BY id LIMIT 100", this.offerCursor).toArray() as any[];
      if (rows.length < 100) rows.push(...this.sql.exec("SELECT * FROM fleet_launches WHERE state = 'offered' AND id <= ? ORDER BY id LIMIT ?", this.offerCursor, 100 - rows.length).toArray());
      for (const row of rows) this.retireInvalidOffer(row);
      return rows.at(-1)?.id || '';
    });
    this.offerCursor = cursor;
  }

  policy(workspace: string): { project: string | null; require_claim: boolean } {
    const row: any = this.sql.exec('SELECT project, require_claim FROM fleet_claim_policy WHERE workspace = ?', workspace).toArray()[0];
    return { project: row?.project || null, require_claim: !!row?.require_claim };
  }

  listPolicy() {
    return (this.sql.exec('SELECT workspace, project, require_claim FROM fleet_claim_policy ORDER BY workspace').toArray() as any[]).map(r => ({
      workspace: r.workspace, project: r.project, require_claim: !!r.require_claim,
    }));
  }

  replacePolicy(workspaces: { workspace: string; project: string | null; require_claim: boolean }[]) {
    this.sql.exec('DELETE FROM fleet_claim_policy');
    for (const row of workspaces) {
      this.sql.exec('INSERT INTO fleet_claim_policy VALUES (?, ?, ?)', row.workspace, row.project, Number(row.require_claim));
    }
  }

  parseDeclaration(raw: unknown): { access: AccessDeclaration } | { error: string } {
    if (raw === undefined || raw === null) {
      return { access: { mode: 'legacy_unspecified', claim_id: null, project: null, scopes: [] } };
    }
    if (!object(raw, ['mode', 'claim_id', 'project', 'scopes'])) return { error: 'invalid_access' };
    const body = raw as any;
    if (body.mode !== 'read' && body.mode !== 'edit' && body.mode !== 'legacy_unspecified') return { error: 'invalid_access' };
    if (body.mode === 'read' || body.mode === 'legacy_unspecified') {
      if (body.claim_id != null || body.project != null || (body.scopes !== undefined && !(Array.isArray(body.scopes) && body.scopes.length === 0))) {
        return { error: 'invalid_access' };
      }
      return { access: { mode: body.mode, claim_id: null, project: null, scopes: [] } };
    }
    if (typeof body.claim_id !== 'string' || !CLAIM_ID.test(body.claim_id)) return { error: 'invalid_access' };
    if (body.project !== undefined && body.project !== null && (typeof body.project !== 'string' || !LABEL.test(body.project))) return { error: 'invalid_access' };
    if (!Array.isArray(body.scopes) || !body.scopes.length || body.scopes.length > 32) return { error: 'invalid_access' };
    const scopes: string[] = [];
    for (const item of body.scopes) {
      const scope = normalizeScope(item);
      if (!scope) return { error: 'invalid_access' };
      if (!scopes.includes(scope)) scopes.push(scope);
    }
    return { access: { mode: 'edit', claim_id: body.claim_id, project: body.project ?? null, scopes } };
  }

  private fail(error: string, status: number): AdmissionErr {
    return { ok: false, error, status };
  }

  private liveClaim(id: string): any | null {
    const row: any = this.sql.exec('SELECT id, project, scope, holder, created_at, expires_at, released_at FROM claims WHERE id = ?', id).toArray()[0];
    if (!row) return null;
    if (row.released_at != null) return { ...row, inactive: 'released' };
    if (!(row.expires_at > this.now())) return { ...row, inactive: 'expired' };
    return row;
  }

  private conflictingLaunches(claim: EditAuthority['claim'], exceptId?: string): any[] {
    const rows = this.sql.exec(`SELECT id, request_json, state FROM fleet_launches WHERE state IN ${ACTIVE}`).toArray() as any[];
    return rows.filter(row => {
      if (exceptId && row.id === exceptId) return false;
      try {
        const request = JSON.parse(row.request_json || '{}');
        if (request.access?.mode !== 'edit') return false;
        if (request.access.claim_id === claim.id) return true;
        // Retained lifecycle facts protect old effects even after the bus lease is gone.
        const old = request.edit_authority?.claim;
        if (old) return old.project === claim.project && (claimCoversScope(old.scope, claim.scope) || claimCoversScope(claim.scope, old.scope));
        // Unbound edit rows from an older version cannot safely prove disjointness.
        return true;
      } catch {
        return true;
      }
    });
  }

  evaluate(caller: AdmissionCaller | undefined, workspace: string, access: AccessDeclaration, phase: AdmissionPhase,
    opts: { launchId?: string; authority?: EditAuthority; permission?: EditAuthority['permission'] } = {}): AdmissionResult {
    const policy = this.policy(workspace);
    if (access.mode === 'read') {
      return { ok: true, access: boundAccess(access), authority: null };
    }
    if (access.mode === 'legacy_unspecified') {
      if (policy.require_claim) return this.fail('claim_required', 403);
      return { ok: true, access: boundAccess(access), authority: null };
    }

    const claim = this.liveClaim(access.claim_id!);
    if (!claim) return this.fail('claim_inactive', 409);
    if (claim.inactive === 'released' || claim.inactive === 'expired') return this.fail('claim_inactive', 409);

    if (!policy.project) return this.fail('claim_workspace_unconfigured', 403);
    if (claim.project !== policy.project) return this.fail('claim_project_mismatch', 403);
    if (access.project && claim.project !== access.project) return this.fail('claim_project_mismatch', 403);

    for (const scope of access.scopes) {
      if (!claimCoversScope(claim.scope, scope)) return this.fail('claim_scope_mismatch', 403);
    }

    if (caller?.isAdmin || caller?.kind !== 'agent' || !caller.tokenId || caller.isRevoked) return this.fail('claim_authority_unsupported', 403);
    const principal: CallerContext = { ...caller, kind: 'agent', isAdmin: false };
    if (isRootRoleAddress(principal.actor) || isRootRoleAddress(claim.holder)) {
      return this.fail('claim_authority_unsupported', 403);
    }
    if (!principal.actor) {
      return this.fail('claim_actor_ambiguous', 403);
    }
    if (!parseAddress(claim.holder)) return this.fail('claim_authority_unsupported', 403);

    const credential: any = this.sql.exec('SELECT kind, actor, subject, registry_actor, machine_id, scopes, revoked_at, expires_at FROM credentials WHERE id = ?', principal.tokenId).toArray()[0];
    const permission = opts.authority?.permission || opts.permission || 'fleet:launch';
    if (!['fleet:launch', 'routing:submit'].includes(permission)) return this.fail('claim_authority_unsupported', 403);
    let scopes: string[] = [];
    try { scopes = JSON.parse(credential?.scopes); } catch { /* fail closed below */ }
    if (!credential || credential.kind !== 'agent' || credentialActor(this.sql as any, credential) !== principal.actor || credential.revoked_at != null ||
      !Number.isFinite(Date.parse(credential.expires_at)) || Date.parse(credential.expires_at) <= this.now() || !Array.isArray(scopes) ||
      !scopes.includes('bus:send') || !scopes.includes(permission)) return this.fail('claim_authority_unsupported', 403);

    const authority: EditAuthority = { actor: principal.actor, credential_id: principal.tokenId!, permission,
      claim: { id: claim.id, project: claim.project, scope: claim.scope, holder: claim.holder, created_at: claim.created_at } };
    if (opts.authority && JSON.stringify(opts.authority) !== JSON.stringify(authority)) return this.fail('claim_authority_changed', 409);

    try {
      this.bus.validateClaimProject(principal, claim.project, claim.holder, this.now());
    } catch (err: any) {
      const message = String(err?.message || err);
      if (message.startsWith('missing_m4_root_authority')) return this.fail('claim_authority_unsupported', 403);
      if (!principal.actor) return this.fail('claim_actor_ambiguous', 403);
      return this.fail('claim_holder_mismatch', 403);
    }
    if (!this.bus.isAuthorizedForActor(principal, claim.holder, this.now())) {
      return this.fail('claim_holder_mismatch', 403);
    }

    if (this.conflictingLaunches(authority.claim, opts.launchId).length) {
      return this.fail('claim_conflict', 409);
    }
    return { ok: true, access: boundAccess(access), authority };
  }

  /** Inspect retained authority without dropping capacity or guessing process termination. */
  heldReason(row: any): string | null {
    try {
      const request = JSON.parse(row.request_json);
      const declared = this.parseDeclaration(request.access);
      if ('error' in declared) return 'claim_authority_unsupported';
      if (declared.access.mode === 'edit' && !request.edit_authority) return 'claim_authority_unsupported';
      const authority: EditAuthority | undefined = request.edit_authority;
      const check = this.evaluate(authority ? { kind: 'agent', actor: authority.actor, tokenId: authority.credential_id } : undefined, row.workspace, declared.access, 'start', { authority, launchId: row.id });
      return check.ok ? null : check.error;
    } catch { return 'claim_authority_unsupported'; }
  }
}

/** Compare immutable client intent independently of the server-derived binding. */
export function sameIntent(stored: string, canonical: string): boolean {
  try {
    const request = JSON.parse(stored);
    delete request.edit_authority;
    request.access ??= { mode: 'legacy_unspecified', claim_id: null, project: null, scopes: [] };
    return JSON.stringify(request) === canonical;
  } catch { return false; }
}

/** An exact retry cannot transfer the retained edit authority to a replacement credential. */
export function sameEditCaller(stored: string, caller: AdmissionCaller): boolean {
  try {
    const request = JSON.parse(stored);
    return request.access?.mode !== 'edit' || (!!request.edit_authority && !!caller.tokenId && !!caller.actor && !caller.isAdmin && caller.kind === 'agent' &&
      request.edit_authority?.credential_id === caller.tokenId && request.edit_authority?.actor === caller.actor);
  } catch { return false; }
}
