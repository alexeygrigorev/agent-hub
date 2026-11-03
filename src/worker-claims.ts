/** Work claims for renewable intake-scoped worker credentials (intake 6022786b).
 * A worker may list claims of its intake's project and create/renew/release
 * claims held by its own worker actor there. Nothing else: no other project,
 * holder or claim. BusService keeps normalization, conflicts and root fences;
 * its admin path is used only after every operation is confined here.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { BusService, CallerContext } from './bus';
import { workerAccessContext, workerClaimRoute } from './worker-access';

const LIVE_SESSION = new Set(['starting', 'running']);
const MAX_WORKER_CLAIM_TTL_MS = 24 * 3600 * 1000;

/** The live worker's claim identity, re-read from the credential and session rows. */
export function workerClaimContext(sql: SqlStorage, tokenId: string | undefined, now = Date.now()):
  { actor: string; project: string; expiresAt: number } | null {
  const worker = workerAccessContext(sql, tokenId, now);
  if (!worker || typeof worker.actor !== 'string' || !LIVE_SESSION.has(worker.session_state)) return null;
  const project = worker.task_project;
  if (typeof project !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(project)) return null;
  let status: unknown;
  try { status = JSON.parse((sql.exec('SELECT data FROM intake_tasks WHERE id=?', worker.task_id).toArray()[0] as any)?.data).status; } catch { return null; }
  if (status === 'done' || status === 'cancelled') return null;
  return { actor: worker.actor, project, expiresAt: worker.expires_at };
}

const reply = (data: unknown, status = 200) => Response.json(data, { status, headers: { 'Cache-Control': 'private, no-store' } });

/** Call only for an authenticated caller whose credential carries worker:own-intake.
 * `body` is the already size-bounded parsed request body (empty for GET). */
export function handleWorkerClaimRequest(request: Request, sql: SqlStorage, bus: BusService,
  caller: CallerContext, body: Record<string, any>, releaseNativeClaim?: (target: string) => Response): Response {
  const url = new URL(request.url), path = url.pathname;
  if (!workerClaimRoute(path, request.method)) return reply({ error: 'worker_scope_forbidden' }, 403);
  // Checked after the body was read: no await separates this from the mutation.
  const now = Date.now();
  const context = workerClaimContext(sql, caller.tokenId, now);
  if (!context) return reply({ error: 'worker_scope_forbidden' }, 403);
  const forbidden = () => reply({ error: 'worker_claim_forbidden' }, 403);
  const tag = context.actor.slice(context.actor.indexOf(':') + 1);
  const admin: CallerContext = { isAdmin: true, kind: 'agent', actor: context.actor };
  try {
    if (path === '/api/bus/claims' && request.method === 'GET') {
      const project = url.searchParams.get('project');
      if (project && project !== context.project) return forbidden();
      return reply(bus.listClaims(admin, context.project, url.searchParams.get('holder') || undefined, url.searchParams.get('active') !== 'false'));
    }
    if (path === '/api/bus/claims') {
      if (body.project !== context.project || ![context.actor, tag].includes(body.holder)) return forbidden();
      if (typeof body.scope !== 'string' || !body.scope || body.scope.length > 256) return reply({ error: 'invalid_scope' }, 400);
      if (body.lease_epoch !== undefined) return forbidden();
      const ttl = body.ttl_ms === undefined ? 3600000 : body.ttl_ms;
      if (typeof ttl !== 'number' || !Number.isSafeInteger(ttl) || ttl < 1) return reply({ error: 'invalid_ttl' }, 400);
      // A worker claim never outlives the worker's own access window.
      const bounded = Math.max(1, Math.min(ttl, MAX_WORKER_CLAIM_TTL_MS, context.expiresAt - now));
      return reply(bus.claim(admin, context.project, body.scope, context.actor, bounded, now));
    }
    if (body.lease_epoch !== undefined) return forbidden();
    let target: string;
    try { target = decodeURIComponent(path.slice('/api/bus/claims/'.length, -'/release'.length)); }
    catch { return reply({ error: 'invalid_target' }, 400); }
    let row: any;
    if (target.includes(':')) {
      const colon = target.indexOf(':');
      if (target.slice(0, colon) !== context.project) return forbidden();
      let scope = target.slice(colon + 1);
      if (scope.includes('..')) return reply({ error: 'invalid_scope' }, 400);
      scope = scope.split('/').filter(part => part && part !== '.').join('/');
      if (!scope) return reply({ error: 'invalid_scope' }, 400);
      row = sql.exec('SELECT id,project,holder,claim_source FROM claims WHERE project=? AND scope=? AND released_at IS NULL ORDER BY created_at DESC LIMIT 1', context.project, scope).toArray()[0];
    } else {
      row = sql.exec('SELECT id,project,holder,claim_source FROM claims WHERE id=?', target).toArray()[0];
    }
    if (!row) return forbidden();
    if (row && row.project !== context.project) return forbidden();
    if (row && row.holder !== context.actor) {
      // Preserve host-attested native-session claims. WorkerAccess rechecks the
      // full native incarnation, workspace, holder and live worker credential.
      if (row.claim_source === 'native' && releaseNativeClaim) return releaseNativeClaim(target);
      return forbidden();
    }
    if (row) bus.releaseClaim(admin, row.id, now);
    return reply({ status: 'ok' });
  } catch (error: any) {
    if (['invalid_scope', 'invalid_ttl'].includes(error?.message)) return reply({ error: error.message }, 400);
    if (error?.message === 'conflict') return reply({ error: 'conflict' }, 409);
    if (error?.message === 'unauthorized' || error?.message?.startsWith?.('missing_m4_root_authority')) return forbidden();
    return reply({ error: 'Internal Server Error' }, 500);
  }
}
