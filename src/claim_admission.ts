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

export type AdmissionOk = { ok: true; access: AccessDeclaration; claim: any | null };
export type AdmissionErr = { ok: false; error: string; status: number };
export type AdmissionResult = AdmissionOk | AdmissionErr;

function object(value: any, keys: string[]): boolean {
  return value !== null && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).every(k => keys.includes(k));
}

export function normalizeScope(scope: unknown): string | null {
  if (typeof scope !== 'string' || !scope || scope.length > 256) return null;
  if (scope.includes('..')) return null;
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
    scopes: access.scopes,
  };
}

export class ClaimAdmission {
  public bus: BusService;
  constructor(private sql: SqlStorage, transaction?: <T>(fn: () => T) => T, private now: () => number = Date.now) {
    this.bus = new BusService(sql, transaction);
  }

  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS fleet_claim_policy (workspace TEXT PRIMARY KEY, project TEXT, require_claim INTEGER NOT NULL)');
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

  private principal(caller: CallerContext | undefined): CallerContext {
    return { ...(caller || {}), isAdmin: false };
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

  launchesBoundToClaim(claimId: string, exceptId?: string): any[] {
    const rows = this.sql.exec(`SELECT id, request_json, state FROM fleet_launches WHERE state IN ${ACTIVE}`).toArray() as any[];
    return rows.filter(row => {
      if (exceptId && row.id === exceptId) return false;
      try {
        const request = JSON.parse(row.request_json || '{}');
        return request.access?.mode === 'edit' && request.access?.claim_id === claimId;
      } catch {
        return false;
      }
    });
  }

  evaluate(caller: CallerContext | undefined, workspace: string, access: AccessDeclaration, phase: AdmissionPhase, opts: { launchId?: string } = {}): AdmissionResult {
    const policy = this.policy(workspace);
    if (access.mode === 'read') {
      return { ok: true, access: boundAccess(access), claim: null };
    }
    if (access.mode === 'legacy_unspecified') {
      if (policy.require_claim) return this.fail('claim_required', 403);
      return { ok: true, access: boundAccess(access), claim: null };
    }

    const claim = this.liveClaim(access.claim_id!);
    if (!claim) return this.fail('claim_inactive', 409);
    if (claim.inactive === 'released' || claim.inactive === 'expired') return this.fail('claim_inactive', 409);

    if (policy.project && claim.project !== policy.project) return this.fail('claim_project_mismatch', 403);
    if (access.project && claim.project !== access.project) return this.fail('claim_project_mismatch', 403);

    for (const scope of access.scopes) {
      if (!claimCoversScope(claim.scope, scope)) return this.fail('claim_scope_mismatch', 403);
    }

    if (phase === 'start') {
      return { ok: true, access: { ...boundAccess(access), project: access.project || claim.project }, claim };
    }

    const principal = this.principal(caller);
    if (isRootRoleAddress(principal.actor) || isRootRoleAddress(claim.holder)) {
      return this.fail('claim_authority_unsupported', 403);
    }
    if (!principal.actor && !(principal.kind === 'host' && principal.machineId)) {
      return this.fail('claim_actor_ambiguous', 403);
    }
    if (!parseAddress(claim.holder)) return this.fail('claim_authority_unsupported', 403);

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

    if (phase === 'reserve' && this.launchesBoundToClaim(claim.id, opts.launchId).length) {
      return this.fail('claim_conflict', 409);
    }
    return { ok: true, access: { ...boundAccess(access), project: access.project || claim.project }, claim };
  }
}
