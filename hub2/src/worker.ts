import { mcp } from './mcp';
import { publicAuthentication, cookieActor, noStore } from './auth';
import { safeEqual } from './crypto';
import { bodyObject, boundedText, error, HttpError, HUB2_FORWARD_BODY_LIMIT, json } from './http';
import { pickModelRouteDetailed, simulateModelRoutes, type PickInput } from './model-picker';
import { sessionPurpose } from './session-purpose';
import { isLiveActiveSession, type LiveSession } from './session-liveness';
import { qualifiedRepositoryWorkspace, REPOSITORY_IDENTITY_CAPABILITY } from './repository-identity';
import { ARTIFACT_BODY_LIMIT, ARTIFACT_MAX_BYTES, PREREQUISITE_HEADER } from './intake-relations';
import type { Actor, Hub2Env, JsonRecord } from './types';

const CONTROL = 'https://control.hub2.internal';
let devActorRegistration: Promise<boolean> | undefined;

export default {
  async fetch(request: Request, env: Hub2Env): Promise<Response> {
    return router.fetch(request, env);
  },
};
const router = {
  async fetch(request: Request, env: Hub2Env): Promise<Response> {
    try {
      const path = publicPath(request.url);
      if (!path) return error('internal_route_forbidden', 404);
      // Install/update clients have no credential yet. Only the published CLI
      // artifacts and dashboard are public; API routes still authenticate.
      const cliAsset = /^\/cli\/(?:manifest\.json|host-release\.json|hub\.pyz(?:\.sha256)?|(?:host-)?install\.(?:sh|ps1)|hub2-(?:self-update|enroll)\.py|agent-hub2-host\.service|agent-hub2-host-update\.(?:service|timer)|agent_hub_host_v2-2\.0\.0\+g[0-9a-f]{40}-py3-none-any\.whl|websockets-16\.0-py3-none-any\.whl)$/.test(path);
      if (env.ASSETS && ['GET', 'HEAD'].includes(request.method) && (path === '/' || cliAsset))
        return secureStatic(await env.ASSETS.fetch(request));
      const authResponse = await publicAuthentication(request, env);
      if (authResponse) return authResponse;
      const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
      if (path === '/webhooks/oncall' && request.method === 'POST') {
        return control.fetch(await forwardRequest(request, CONTROL + '/_oncall/ingest'));
      }
      if (path === '/api/oncall-alerts/offers' && request.method === 'GET') {
        return control.fetch(await forwardRequest(request, CONTROL + '/_oncall/offers'));
      }
      const oncallDelivery = path.match(/^\/api\/oncall-alerts\/([0-9a-f-]{36})\/(ack|nack)$/i);
      if (oncallDelivery && request.method === 'POST') {
        return control.fetch(await forwardRequest(request,
          CONTROL + '/_oncall/deliveries/' + oncallDelivery[1] + '/' + oncallDelivery[2]));
      }
      if (path === '/api/oncall-alerts/config' && request.method === 'POST') {
        if (!sameOriginMutation(request)) return error('csrf_refused', 403);
        if (env.HUB2_RELEASE_MODE === 'bootstrap') {
          return control.fetch(await forwardRequest(request, CONTROL + '/_oncall/config'));
        }
        const configActor = await authenticate(request, env);
        if (!configActor || !admin(configActor)) return error('forbidden', 403);
        return control.fetch(await forwardRequest(request, CONTROL + '/_oncall/config', configActor));
      }
      if (path === '/health' && request.method === 'GET')
        return json({ ok: true, service: 'hub2' });
      if (path === '/api/enrollments/complete' && request.method === 'POST') {
        if (
          !sameOriginMutation(request) ||
          !request.headers.get('Content-Type')?.toLowerCase().startsWith('application/json')
        )
          return error('csrf_refused', 403);
        return await (
          await env.CONTROL.get(env.CONTROL.idFromName('global'))
        ).fetch(await forwardRequest(request, `${CONTROL}/api/enrollments/complete`));
      }
      if (!sameOriginMutation(request)) return error('csrf_refused', 403);
      let actor = await authenticate(request, env);
      if (!actor) return error('unauthorized', 401);
      // Deployment credentials are a fixed policy capability, never a general API identity.
      if (actor.kind === 'service' && path !== '/api/model-policy') return error('forbidden', 403);

      const safetyMode = safetyModeForRequest(request, path, actor);
      if (safetyMode === false) return error('safety_mode_refused', 403);

      if (path === '/api/auth/me' && request.method === 'GET') {
        if (actor.kind !== 'operator') return error('human_required', 403);
        if (actor.generation === 'dev') return json({
          actor: actor.id, sub: 'dev', kind: 'human', label: actor.client_id,
          scopes: actor.scopes, isAdmin: true,
        }, 200, noStore);
        return control.fetch(await forwardRequest(request, CONTROL + '/_auth/me'));
      }
      if (path === '/api/resource-settings') return resourceSettings(request, env, actor);
      if (path === '/api/quota' && request.method === 'GET')
        return quotaProjection(request, env, actor);
      if (path === '/mcp') return mcp(request, env, (r, e) => router.fetch(r, e), actor);
      if (
        path === '/api/model-policy' ||
        path === '/api/deployment-credentials' ||
        /^\/api\/deployment-credentials\/deployment:[0-9a-f-]+\/revoke$/.test(path) ||
        path === '/api/named-agents' ||
        /^\/api\/named-agents\/[^/]+\/grants$/.test(path) ||
        path === '/api/directory' ||
        path.startsWith('/api/routes/')
      )
        return control.fetch(
          await forwardRequest(request, CONTROL + path + requestUrl(request).search, actor),
        );
      if (path === '/api/engines' || path.startsWith('/api/engines/'))
        return control.fetch(await forwardRequest(request, CONTROL + path + requestUrl(request).search, actor));
      if (path === '/api/mailboxes')
        return control.fetch(
          await forwardRequest(
            new Request(request.url, {
              method: 'POST',
              headers: request.headers,
            }),
            CONTROL + '/_mailboxes',
            actor,
          ),
        );
      if (path === '/api/sessions' && request.method !== 'GET' && request.method !== 'HEAD')
        return request.method === 'POST' ? await startSession(request, env, actor) : error('method_not_allowed', 405);
      if (/^\/api\/picker-alerts\/(?:counters|offers|[1-9][0-9]*\/(?:ack|nack))$/.test(path)) {
        if(!admin(actor))return error('forbidden',403);
        return control.fetch(await forwardRequest(request, CONTROL + path, actor));
      }
      if (path === '/api/model-picker/credential-monitor-evidence') {
        if (!admin(actor)) return error('forbidden', 403);
        if (request.method !== 'GET') return error('method_not_allowed', 405);
        const nonce = new URL(request.url).searchParams.get('nonce');
        if (!nonce || !/^[0-9a-f]{32}$/.test(nonce)) return error('invalid_nonce', 400);
        try {
          if (env.CREDENTIAL_MONITOR_EVIDENCE) return json(await observerBound(
            env.CREDENTIAL_MONITOR_EVIDENCE.snapshot(nonce)), 200, { 'Cache-Control': 'no-store' });
        } catch { try { env.CREDENTIAL_MONITOR_EVIDENCE?.invalidate(); } catch {} }
        return json({kind:'hub_alert_evidence_unavailable_v1',available:false,reason:'coverage_unknown',nonce},
          200, {'Cache-Control':'no-store'});
      }
      if (path === '/api/model-picker/diagnostic-evidence') {
        if (!admin(actor)) return error('forbidden', 403);
        if (request.method !== 'GET') return error('method_not_allowed', 405);
        const url = new URL(request.url), producer = url.searchParams.get('producer'), nonce = url.searchParams.get('nonce');
        if (producer !== 'picker_excluded' && producer !== 'picker_quota_stale') return error('invalid_producer', 400);
        if (!nonce || !/^[0-9a-f]{32}$/.test(nonce)) return error('invalid_nonce', 400);
        const observer = env.PICKER_DIAGNOSTIC_EVIDENCE?.[producer];
        try {
          if (observer) return json(await observerBound(observer.snapshot(nonce)), 200, {'Cache-Control': 'no-store'});
        } catch { try { observer?.invalidate(); } catch {} }
        return json({kind: 'hub_alert_evidence_unavailable_v1', available: false, reason: 'coverage_unknown', nonce},
          200, {'Cache-Control': 'no-store'});
      }
      if (path === '/api/model-picker/pick')
        return request.method === 'POST' ? await dryRunPick(request, env, actor) : error('method_not_allowed', 405);
      if (path === '/api/fleet/routing/simulate')
        return request.method === 'GET' ? await routingSimulation(request, env, actor) : error('method_not_allowed', 405);
      if (path === '/api/sessions/reap')
        return request.method === 'POST' ? await reapSessions(request, env, actor) : error('method_not_allowed', 405);
      if (path === '/api/fleet' || path === '/api/fleet/machines' || path === '/api/sessions')
        return await machineProjection(request, env, actor, path === '/api/sessions');
      if (path === '/api/status') {
        if (!admin(actor)) return error('forbidden', 403);
        const fleet = await machineProjection(
          new Request(new URL('/api/fleet', request.url), {
            headers: request.headers,
          }),
          env,
          actor,
          false,
        );
        if (!fleet.ok) return fleet;
        const view = (await fleet.json()) as { items?: { id: string; connected?: boolean; sessions?: unknown[]; attention?: { session_id?: string; reason?: string }[] }[] };
        const items = view.items ?? [];
        // Hub1-shaped keys the CLI (`hub status`) renders; the Hub2 projection stays under `fleet`.
        return json({
          fleet: view,
          service: 'hub2',
          projection: 'bounded_first_page',
          snapshot_at: new Date().toISOString(),
          revision: 'hub2',
          coverage: {
            sessions: items.reduce((n, m) => n + (m.sessions?.length ?? 0), 0),
            missing_hosts: items.filter(m => m.connected === false).map(m => m.id),
            expected_offline: [],
          },
          needs_attention: items.flatMap(m => (m.attention ?? []).map(a => ({
            severity: 'warning', subject: a.session_id ?? m.id, detail: a.reason ?? 'attention',
          }))),
        });
      }
      if (path === '/api/fleet/routing/self') {
        if (request.method !== 'GET') return error('method_not_allowed', 405);
        if (requestUrl(request).search) return error('unexpected_query', 400);
        return await ownMachineConnection(env, actor);
      }
      if (path === '/api/fleet/routing/machines' && request.method === 'GET')
        return await routingMachines(env, actor);
      if (path === '/api/fleet/routing/policy' && request.method === 'GET')
        return control.fetch(await forwardRequest(request, CONTROL + '/api/model-policy', actor));

      if (path === '/api/enrollments/pair' && request.method === 'POST')
        return control.fetch(await forwardRequest(request, `${CONTROL}${path}`, actor));
      if (path === '/api/repos' || /^\/api\/repos\/[^/]+$/.test(path))
        return control.fetch(
          await forwardRequest(
            request,
            `${CONTROL}${requestUrl(request).pathname}${requestUrl(request).search}`,
            actor,
          ),
        );

      if (path === '/api/intake/rules' || path === '/api/asks' || path.startsWith('/api/asks/') || path === '/api/shared-memory/import')
        return control.fetch(await forwardRequest(request, CONTROL + path + requestUrl(request).search, actor));

      const projectPath =
        path === '/api/claims' ||
        /^\/api\/claims\/[^/]+(?:\/(?:renew|release))?$/.test(path) ||
        path === '/api/schedules' ||
        /^\/api\/schedules\/[^/]+(?:\/(?:pause|resume|cancel|occurrences))?$/.test(path) ||
        path === '/api/intakes' ||
        /^\/api\/intakes\/[^/]+(?:\/(?:start|history|append|events|context|audit|depend|links|artifacts|merge-evidence|merge-state|reevaluate))?$/.test(path) ||
        /^\/api\/intakes\/[^/]+\/artifacts\/[^/]+$/.test(path) ||
        path === '/api/intake/tasks' ||
        /^\/api\/intake\/tasks\/[^/]+(?:\/(?:start|history|append|events|context|audit|depend|links|artifacts|merge-evidence|merge-state|reevaluate))?$/.test(path) ||
        /^\/api\/intake\/tasks\/[^/]+\/artifacts\/[^/]+$/.test(path);
      if (projectPath) {
        const url = requestUrl(request);
        let requested = url.searchParams.get('project');
        if (!requested && request.method === 'POST' && (path === '/api/intakes' || path === '/api/intake/tasks')) {
          // Intake create carries the project in its JSON body (CLI/MCP); accept it like the query form.
          const body = (await request.clone().json().catch(() => null)) as { project?: unknown } | null;
          if (typeof body?.project === 'string' && body.project) requested = body.project;
        }
        const intakeId = path.match(/^\/api\/(?:intakes|intake\/tasks)\/([^/]+)/)?.[1];
        if (!requested && intakeId) {
          const response = await control.fetch(
            await forwardRequest(
              new Request(request.url, { headers: request.headers }),
              CONTROL + '/api/routes/intake/' + encodeURIComponent(intakeId),
              actor,
            ),
          );
          if (!response.ok) return response;
          const { route } = (await response.json()) as {
            route: { id: string; owner: string };
          };
          requested = route.owner;
          url.pathname = url.pathname.replace('/' + intakeId, '/' + route.id);
        }
        if (actor.kind === 'agent' && actor.id.startsWith('session:') && actor.resource_id) {
          const response = await control.fetch(await forwardRequest(new Request(request.url, {headers:request.headers}), CONTROL+'/api/routes/intake/'+encodeURIComponent(actor.resource_id), actor));
          if (!response.ok) return response;
          const {route} = await response.json() as {route:{owner:string}};
          if (requested && requested !== route.owner) return error('project_forbidden',403);
          url.searchParams.set('project',route.owner);
          const internalPath = url.pathname.replace('/api/intake/tasks','/api/intakes');
          const relation = await relationForward(request, env, control, actor, internalPath);
          if (relation instanceof Response) return relation;
          return env.PROJECTS.get(env.PROJECTS.idFromName(route.owner)).fetch(await forwardRequest(request,'https://project.internal'+internalPath+url.search,actor,undefined,undefined,undefined,relation));
        }
        if (!requested && !intakeId && request.method === 'GET' && (path === '/api/intakes' || path === '/api/intake/tasks'))
          return control.fetch(await forwardRequest(request, CONTROL + '/api/intakes' + url.search, actor));
        if (!requested || requested.length > 180)
          return error('project_required', 400, 'pass --project <owner/name> (a repo from `hub repos list`) or ?project=');
        const repoResponse = await control.fetch(
          new Request(`${CONTROL}/api/repos/${encodeURIComponent(requested)}`, {
            headers: { 'x-hub2-actor': JSON.stringify(actor) },
          }),
        );
        if (!repoResponse.ok)
          return error('project_not_found_or_forbidden', repoResponse.status === 403 ? 403 : 404);
        const repo = (await repoResponse.json()) as {
          repo?: { canonical_name?: string; status?: string };
        };
        if (repo.repo?.status !== 'active' || !repo.repo.canonical_name)
          return error('project_inactive', 409);
        url.searchParams.set('project', repo.repo.canonical_name);
        const internalPath = url.pathname.startsWith('/api/intake/tasks')
          ? url.pathname.replace('/api/intake/tasks', '/api/intakes')
          : url.pathname;
        const stub = env.PROJECTS.get(env.PROJECTS.idFromName(repo.repo.canonical_name));
        const relation = await relationForward(request, env, control, actor, internalPath);
        if (relation instanceof Response) return relation;
        return stub.fetch(
          await forwardRequest(
            request,
            `https://project.hub2.internal${internalPath}${url.search}`,
            actor,
            undefined,
            undefined,
            undefined,
            relation,
          ),
        );
      }

      const nativeCommand=path.match(/^\/api\/machines\/([A-Za-z0-9_.-]{1,80})\/native\/([0-9a-f-]{36})\/commands(?:\/([0-9a-f-]{36})(\/ack)?)?$/);
      if(nativeCommand){
        const ack=!!nativeCommand[4];
        if(ack?request.method!=='POST'||actor.kind!=='machine'||actor.resource_id!==nativeCommand[1]||!actor.scopes.includes('session:report')
          :actor.kind!=='operator'||!actor.scopes.includes('operator:admin'))return error('forbidden',403);
        if(safetyMode)return error('forbidden',403);
        const machine=env.MACHINES.get(env.MACHINES.idFromName(nativeCommand[1]));
        return machine.fetch(await forwardRequest(request,'https://machine.hub2.internal/api/native/'+nativeCommand[2]+'/commands'
          +(nativeCommand[3]?'/'+nativeCommand[3]:'')+(ack?'/ack':''),actor));
      }
      const nativeInventory=path.match(/^\/api\/machines\/([A-Za-z0-9_.-]{1,80})\/native-inventory$/);
      if(nativeInventory){
        if(request.method!=='POST')return error('method_not_allowed',405);
        if(actor.kind!=='machine'||actor.resource_id!==nativeInventory[1]||!actor.scopes.includes('machine:heartbeat'))return error('forbidden',403);
        const machine=env.MACHINES.get(env.MACHINES.idFromName(nativeInventory[1]));
        return machine.fetch(await forwardRequest(request,'https://machine.hub2.internal/api/native-inventory',actor));
      }
      const engineUpdateAck=path.match(/^\/api\/machines\/([A-Za-z0-9_.-]{1,80})\/engine-updates\/([0-9a-f-]{36})\/ack$/i);
      if(engineUpdateAck){
        if(request.method!=='POST')return error('method_not_allowed',405);
        if(actor.kind!=='machine'||actor.resource_id!==engineUpdateAck[1])return error('forbidden',403);
        const machine=env.MACHINES.get(env.MACHINES.idFromName(engineUpdateAck[1]));
        return machine.fetch(await forwardRequest(request,'https://machine.hub2.internal/api/engine-updates/'+engineUpdateAck[2]+'/ack',actor));
      }
      if (path === '/api/heartbeat' || path === '/api/offers' || path === '/api/connect') {
        if (actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
        const machine = env.MACHINES.get(env.MACHINES.idFromName(actor.resource_id));
        return machine.fetch(
          await forwardRequest(
            request,
            `https://machine.hub2.internal${path}${requestUrl(request).search}`,
            actor,
            undefined,
            undefined,
            safetyMode || undefined,
          ),
        );
      }
      const session = path.match(
        /^\/api\/sessions\/([^/]+)(?:\/(started|finish|admission|credentials|stop|send|capture|link|commands(?:\/[^/]+\/ack)?))?$/,
      );
      if (session) {
        let machineId =
          actor.kind === 'machine'
            ? actor.resource_id
            : requestUrl(request).searchParams.get('machine_id');
        let sessionPath = path;
        if (!machineId && actor.kind !== 'machine') {
          const response = await control.fetch(
            await forwardRequest(
              new Request(request.url, { headers: request.headers }),
              CONTROL + '/api/routes/session/' + encodeURIComponent(session[1]),
              actor,
            ),
          );
          if (!response.ok) return response;
          const { route } = (await response.json()) as {
            route: { id: string; owner: string };
          };
          machineId = route.owner;
          sessionPath = path.replace('/' + session[1], '/' + route.id);
        }
        if (!machineId || !/^[A-Za-z0-9_.-]{1,80}$/.test(machineId))
          return error('machine_required', 400);
        if (actor.kind === 'machine' && actor.resource_id !== machineId)
          return error('forbidden', 403);
        const machine = env.MACHINES.get(env.MACHINES.idFromName(machineId));
        return machine.fetch(
          await forwardRequest(
            request,
            `https://machine.hub2.internal${sessionPath}${requestUrl(request).search}`,
            actor,
            undefined,
            undefined,
            safetyMode || undefined,
          ),
        );
      }

      const accountPath = path.match(
        /^\/api\/accounts\/([A-Za-z0-9_.-]{1,80})(\/api\/account(?:\/.*)?)$/,
      );
      if (accountPath || path === '/api/account' || path.startsWith('/api/account/')) {
        const accountId = accountPath?.[1] || env.HUB2_DEFAULT_ACCOUNT_ID || 'default';
        if (['shared-zai','zcodex'].includes(accountId)) return error('noncanonical_account',400);
        const target = await forwardRequest(request,'https://account.hub2.internal'+(accountPath?.[2]||path)+requestUrl(request).search,actor);
        target.headers.set('x-hub2-account-id',accountId);
        return env.ACCOUNTS.get(env.ACCOUNTS.idFromName(accountId)).fetch(target);
      }

      if (
        path === '/api/messages' ||
        /^\/api\/messages\/[^/]+(?:\/(?:read|ack|delivered|accept|complete|readdress|status))?$/.test(
          path,
        )
      ) {
        const q = requestUrl(request).searchParams;
        let as = q.get('as');
        if (!as && request.method === 'POST') {
          let b: JsonRecord;
          try {
            b = JSON.parse(await boundedText(request.clone(), HUB2_FORWARD_BODY_LIMIT));
          } catch (caught) {
            if (caught instanceof HttpError) throw caught;
            return error('invalid_json', 400);
          }
          if (typeof b.as === 'string') as = b.as;
        }
        if (as) {
          const delegated = await control.fetch(
            new Request(CONTROL + '/_delegate', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'x-hub2-actor': JSON.stringify(actor),
              },
              body: JSON.stringify({
                target: as,
                action:
                  request.method === 'GET' || /\/(?:read|ack|accept|complete)$/.test(path)
                    ? 'read'
                    : 'send',
                actor_id: actor.id,
                family_id: actor.family_id,
                client_id: actor.client_id || '',
              }),
            }),
          );
          if (!delegated.ok) return delegated;
          const d = (await delegated.json()) as {
            target: string;
            scopes: string[];
          };
          actor = { ...actor, id: d.target, kind: 'agent', scopes: d.scopes };
        }
        let deliverySession: string | undefined;
        const owner = q.get('mailbox') || actor.id;
        if (path === '/api/messages' && request.method === 'POST' && owner !== actor.id) return error('sender_not_authorized',403);
        if (actor.kind === 'machine' && /\/delivered$/.test(path) && request.method === 'POST') {
          if (!actor.resource_id || !actor.scopes.includes('message:delivery'))
            return error('forbidden', 403);
          const body = JSON.parse(await boundedText(request.clone(), HUB2_FORWARD_BODY_LIMIT)) as JsonRecord;
          const id = path.split('/')[3];
          const response = await env.MACHINES.get(env.MACHINES.idFromName(actor.resource_id)).fetch(
            'https://machine.internal/_internal/mail-authorize',
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                message_id: id,
                epoch: body.epoch,
                operation_id: body.operation_id,
                recipient: owner,
                machine_id: actor.resource_id,
              }),
            },
          );
          if (!response.ok) return response;
          const target = (await response.json()) as JsonRecord;
          if (
            target.authorized !== true ||
            target.message_id !== id ||
            target.epoch !== body.epoch ||
            target.operation_id !== body.operation_id ||
            target.recipient !== owner ||
            target.machine_id !== actor.resource_id ||
            typeof target.session_id !== 'string' ||
            !target.session_id ||
            target.session_id.length > 120
          )
            return error('native_delivery_authority_unavailable', 503);
          deliverySession = target.session_id;
          actor = { ...actor, scopes: [...actor.scopes, `mailbox:${owner}`] };
        }
        if (
          owner !== actor.id &&
          !actor.scopes.includes('*') &&
          !actor.scopes.includes('operator:admin') &&
          !actor.scopes.includes(`mailbox:${owner}`) &&
          !deliverySession
        )
          return error('forbidden', 403);
        const mailbox = env.MAILBOXES.get(env.MAILBOXES.idFromName('global'));
        return mailbox.fetch(
          await forwardRequest(
            request,
            `https://mailbox.hub2.internal${path}${requestUrl(request).search}`,
            actor,
            owner,
            deliverySession,
          ),
        );
      }
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      console.error('[hub2] unhandled worker error:', caught);
      return error('internal_error', 500);
    }
  },
};

async function authenticate(request: Request, env: Hub2Env): Promise<Actor | undefined> {
  const authorization = request.headers.get('Authorization') || '';
  const match = authorization.match(/^Bearer ([^\s]{32,256})$/);
  if (!match) return cookieActor(request, env);
  const token = match[1];
  const devToken = env.HUB2_DEV_OPERATOR_TOKEN;
  if (devToken && safeEqual(token, devToken)) {
    devActorRegistration ??= env.CONTROL.get(env.CONTROL.idFromName('global'))
      .fetch(`${CONTROL}/_actors/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: 'human:dev', kind: 'human', enabled: true }),
      })
      .then((response) => response.ok);
    if (!(await devActorRegistration)) return undefined;
    return {
      id: 'human:dev',
      kind: 'operator',
      scopes: ['*'],
      generation: 'dev',
      family_id: 'hub2-dev',
      client_id: 'local',
    };
  }
  const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
  const response = await control.fetch(
    new Request(`${CONTROL}/_verify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    }),
  );
  if (!response.ok) return undefined;
  const body = (await response.json()) as { actor?: Actor };
  return body.actor;
}

function publicPath(input: string): string | undefined {
  let path: string;
  try {
    path = new URL(input).pathname;
  } catch {
    return undefined;
  }
  if (path.includes('\\') || path.includes('\0')) return undefined;
  let decoded = path;
  for (let pass = 0; pass < 3; pass++) {
    try {
      decoded = decodeURIComponent(decoded);
    } catch {
      return undefined;
    }
    if (decoded.split('/').some((segment) => segment.startsWith('_'))) return undefined;
  }
  return path;
}

function requestUrl(request: Request): URL {
  return new URL(request.url);
}

function sameOriginMutation(request: Request): boolean {
  if (['GET', 'HEAD', 'OPTIONS'].includes(request.method)) return true;
  const origin = request.headers.get('Origin');
  if (origin) {
    try {
      if (new URL(origin).origin !== new URL(request.url).origin) return false;
    } catch {
      return false;
    }
  }
  const fetchSite = request.headers.get('Sec-Fetch-Site')?.toLowerCase();
  // Native/API clients do not send Sec-Fetch-Site. When a browser does send
  // it, allow only same-origin requests (Origin is checked above as well).
  return fetchSite === undefined || fetchSite === null || fetchSite === 'same-origin';
}

function secureStatic(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('Referrer-Policy', 'no-referrer');
  headers.set('X-Frame-Options', 'DENY');
  headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
  if (!headers.has('Content-Security-Policy'))
    headers.set(
      'Content-Security-Policy',
      "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    );
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function forwardRequest(
  request: Request,
  target: string,
  actor?: Actor,
  mailbox?: string,
  deliverySession?: string,
  safetyMode?: 'operator_stop',
  options?: ForwardOptions,
): Promise<Request> {
  const headers = new Headers(request.headers);
  for (const key of [...headers.keys()])
    if (key.toLowerCase().startsWith('x-hub2-')) headers.delete(key);
  for (const [key, value] of Object.entries(options?.headers ?? {})) headers.set(key, value);
  if (actor) headers.set('x-hub2-actor', JSON.stringify(actor));
  if (mailbox) headers.set('x-hub2-mailbox', mailbox);
  if (deliverySession) headers.set('x-hub2-delivery-session', deliverySession);
  if (safetyMode === 'operator_stop') headers.set('x-hub2-safety-mode', safetyMode);
  let body: ArrayBuffer | undefined;
  if (!['GET', 'HEAD'].includes(request.method)) {
    const text = await boundedText(request.clone(), options?.bodyLimit ?? HUB2_FORWARD_BODY_LIMIT);
    const encoded = new TextEncoder().encode(text);
    const copied = new Uint8Array(new ArrayBuffer(encoded.byteLength));
    copied.set(encoded);
    body = copied.buffer;
  }
  return new Request(target, {
    method: request.method,
    headers,
    ...(body ? { body } : {}),
  });
}

type ForwardOptions = { bodyLimit?: number; headers?: Record<string, string> };

/**
 * Intake relation routes: artifacts get a PNG-sized body bound; `depend` resolves the
 * prerequisite (`on`, possibly in another project) with the caller's own visibility, refuses
 * cross-project cycles, and hands the canonical id/project to the dependent's project object
 * in a trusted header (client x-hub2-* headers are always stripped).
 */
async function relationForward(request: Request, env: Hub2Env, control: DurableObjectStub, actor: Actor,
  internalPath: string): Promise<ForwardOptions | Response | undefined> {
  const match = internalPath.match(/^\/api\/intakes\/([^/]+)\/(depend|artifacts)(?:\/[^/]+)?$/);
  if (!match || request.method !== 'POST') return undefined;
  if (match[2] === 'artifacts') {
    const length = Number(request.headers.get('Content-Length'));
    if (Number.isFinite(length) && length > ARTIFACT_BODY_LIMIT)
      return error('artifact_too_large', 413, `size check failed: PNG over ${ARTIFACT_MAX_BYTES} bytes (6 MiB limit)`);
    // Cheap scope gate before the router buffers the large body; the project object rechecks per intake.
    if (!['operator:admin', '*', 'intake:write', 'intake:append'].some(scope => actor.scopes.includes(scope)))
      return error('forbidden', 403, 'uploading an artifact needs intake:write, intake:append or operator access');
    return { bodyLimit: ARTIFACT_BODY_LIMIT };
  }
  const dependent = decodeURIComponent(match[1]);
  let body: { on?: unknown; remove?: unknown } | null = null;
  try { body = JSON.parse(await boundedText(request.clone(), 4096)); } catch { body = null; }
  if (!body || typeof body.on !== 'string' || !body.on || body.on.length > 180)
    return error('prerequisite_required', 400, 'pass the prerequisite intake id with --on');
  const remove = body.remove === true;
  const routed = await control.fetch(new Request(`${CONTROL}/api/routes/intake/${encodeURIComponent(body.on)}`,
    { headers: { 'x-hub2-actor': JSON.stringify(actor) } }));
  // Removing never needs the prerequisite to be visible or the graph walk: a stale or cyclic link stays removable.
  if (!routed.ok && remove)
    return { headers: { [PREREQUISITE_HEADER]: JSON.stringify({ id: body.on, project: '' }) } };
  if (!routed.ok) {
    const code = ((await routed.json().catch(() => null)) as { error?: string } | null)?.error;
    if (code === 'ambiguous_prefix') return error('prerequisite_ambiguous', 409, `prerequisite id prefix ${body.on} matches several intakes; pass the full id`);
    if (code === 'prefix_too_short') return error('prerequisite_prefix_too_short', 400, 'prerequisite id prefix must be at least 6 characters');
    return error('prerequisite_not_found', 404, `prerequisite intake ${body.on} does not exist or is not visible to this caller`);
  }
  const { route } = (await routed.json()) as { route: { id: string; owner: string } };
  if (remove) return { headers: { [PREREQUISITE_HEADER]: JSON.stringify({ id: route.id, project: route.owner }) } };
  if (route.id === dependent) return error('dependency_self', 409, 'an intake cannot depend on itself');
  // Bounded walk over the prerequisite's own prerequisites, across projects.
  const seen = new Set<string>(), queue: Array<{ id: string; project: string }> = [{ id: route.id, project: route.owner }];
  while (queue.length) {
    const next = queue.shift()!;
    if (next.id === dependent) return error('dependency_cycle', 409, `${route.id} already depends (directly or transitively) on ${dependent}`);
    if (seen.has(next.id) || !next.project) continue;
    seen.add(next.id);
    if (seen.size > 64) return error('dependency_graph_too_large', 409, 'prerequisite chain exceeds 64 intakes; cycle check could not complete');
    const response = await env.PROJECTS.get(env.PROJECTS.idFromName(next.project))
      .fetch(new Request('https://project.hub2.internal/_internal/prerequisites/' + encodeURIComponent(next.id)));
    if (!response.ok) return error('dependency_check_unavailable', 503, `could not read prerequisites of ${next.id}; cycle check failed closed, retry`);
    const view = (await response.json()) as { depends_on?: Array<{ intake_id: string; project: string }> };
    for (const item of view.depends_on ?? []) queue.push({ id: item.intake_id, project: item.project });
  }
  return { headers: { [PREREQUISITE_HEADER]: JSON.stringify({ id: route.id, project: route.owner }) } };
}

/**
 * POST /api/sessions: `hub sessions start`. Creates a project intake and starts
 * it through the normal model-picker and dispatch path.
 * Every refusal names a reason; nothing here waits on a machine.
 */
async function startSession(request: Request, env: Hub2Env, actor: Actor): Promise<Response> {
  if (!admin(actor) && !actor.scopes.includes('session:start')) return error('forbidden', 403);
  let body: JsonRecord;
  try {
    const parsed = JSON.parse(await boundedText(request, 32_768));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return error('invalid_json_object', 400);
    body = parsed as JsonRecord;
  } catch (caught) {
    if (caught instanceof HttpError) return error(caught.code, caught.status);
    return error('invalid_json', 400);
  }
  const key = body.key;
  if (typeof key !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/.test(key)) return error('idempotency_key_required', 400);
  const purpose = sessionPurpose(body.purpose);
  const text = (field: string): string | undefined => typeof body[field] === 'string' && (body[field] as string).trim() ? (body[field] as string).trim() : undefined;
  const requestedProject = text('repo') ?? text('project');
  const label = text('workspace');
  // `--cwd` is the directory the session must run in (a task worktree). It is the launch workspace;
  // the label only names the project. Dropping it started sessions in the repository's label directory.
  const cwd = text('cwd');
  if (cwd !== undefined && (cwd.length > 1024 || cwd.includes('\0') || !(cwd.startsWith('/') || /^[A-Za-z]:\\/.test(cwd))))
    return error('invalid_cwd', 400, '--cwd must be an absolute path on the target host');
  if (!cwd && !label && !requestedProject && !text('intake_task_id'))
    return error('workspace_required', 400, 'pass --repo owner/name to use that repository workspace, or --workspace <registered label or absolute target-host path>');
  const interactive = body.mode === 'interactive';
  const brief = text('brief');
  const taskRef = text('intake_task_id');
  if (taskRef !== undefined && !/^[A-Za-z0-9._:-]{6,128}$/.test(taskRef)) return error('invalid_intake_task_id', 400);
  if (!interactive && !brief && !taskRef) return error('brief_required', 400, 'headless sessions need --prompt or --brief-file');
  const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
  // An existing intake already knows its project; resolve it (full id or unique prefix) instead of guessing.
  let existingIntake: string | undefined;
  let project = requestedProject ?? env.HUB2_DEFAULT_PROJECT;
  if (taskRef !== undefined) {
    const routed = await control.fetch(new Request(`${CONTROL}/api/routes/intake/${encodeURIComponent(taskRef)}`, { headers: { 'x-hub2-actor': JSON.stringify(actor) } }));
    if (!routed.ok) {
      const code = ((await routed.json().catch(() => ({}))) as JsonRecord).error;
      return error(typeof code === 'string' ? code : 'intake_not_found', routed.status === 403 ? 403 : routed.status === 400 ? 400 : 404, 'intake_task_id did not resolve to a Hub2 intake');
    }
    const { route } = (await routed.json()) as { route: { id: string; owner: string } };
    existingIntake = route.id;
    project = route.owner;
  }
  const labelIsPath = !label || label.startsWith('/') || /^[A-Za-z]:\\/.test(label);
  if (!project && label && !labelIsPath) {
    const byWorkspace = await control.fetch(new Request(`${CONTROL}/api/repos/${encodeURIComponent(label)}`, {
      headers: { 'x-hub2-actor': JSON.stringify(actor) },
    }));
    if (byWorkspace.ok) {
      const match = (await byWorkspace.json()) as { repo?: { canonical_name?: string } };
      project = match.repo?.canonical_name;
    }
  }
  if (!project) {
    const list = await control.fetch(new Request(`${CONTROL}/api/repos?limit=2`, { headers: { 'x-hub2-actor': JSON.stringify(actor) } }));
    const items = list.ok ? ((await list.json()) as { items?: { canonical_name: string }[] }).items ?? [] : [];
    if (items.length !== 1) return error('project_required', 400, 'pass --repo owner/name (no unique default project)');
    project = items[0].canonical_name;
  }
  const repoResponse = await control.fetch(new Request(`${CONTROL}/api/repos/${encodeURIComponent(project)}`, { headers: { 'x-hub2-actor': JSON.stringify(actor) } }));
  let repoBody = repoResponse;
  // First start for a repo the registry has not seen: an operator registers it from its GitHub name
  // (the CLI derives owner/name from the workspace's git origin) instead of failing every new project.
  if (repoResponse.status === 404 && admin(actor) && existingIntake === undefined && /^[A-Za-z0-9_.-]{1,80}\/[A-Za-z0-9_.-]{1,100}$/.test(project)) {
    repoBody = await control.fetch(new Request(`${CONTROL}/api/repos/${encodeURIComponent(project)}`, {
      method: 'PATCH', headers: { 'x-hub2-actor': JSON.stringify(actor), 'Content-Type': 'application/json' },
      body: JSON.stringify({ canonical_url: `https://github.com/${project}`, purpose: 'Auto-registered by hub sessions start', aliases: [] }),
    }));
  }
  if (!repoBody.ok) return error('project_not_found_or_forbidden', repoBody.status === 403 ? 403 : 404, 'register it with: hub repos import / PATCH /api/repos/owner%2Fname');
  const repo = (await repoBody.json()) as { repo?: { canonical_name?: string; canonical_url?: string; status?: string } };
  if (repo.repo?.status !== 'active' || !repo.repo.canonical_name) return error('project_inactive', 409);
  const canonical = repo.repo.canonical_name;
  let workspace = cwd ?? label;
  let requiredCapabilities = Array.isArray(body.needs) ? body.needs : undefined;
  const repoOnly = workspace === undefined && (requestedProject !== undefined || taskRef !== undefined);
  if (repoOnly) {
    // Identity is derived only from the registry's canonical record. Alias and
    // caller spelling never become the placement key or a basename fallback.
    // The 236-bit token binds identity; it grants no access by itself. The host
    // must advertise new-protocol support and reread a unique full origin.
    const qualified = await qualifiedRepositoryWorkspace(repo.repo.canonical_url);
    if (!qualified) return error('repository_identity_unavailable', 409);
    workspace = qualified;
    requiredCapabilities = [...new Set([...(requiredCapabilities || []), REPOSITORY_IDENTITY_CAPABILITY])];
  }
  if (!workspace)
    return error('workspace_required', 400, 'pass --repo owner/name to use that repository workspace, or --workspace <registered label or absolute target-host path>');
  const stub = env.PROJECTS.get(env.PROJECTS.idFromName(canonical));
  const call = async (path: string, payload: JsonRecord): Promise<{ response: Response; data: JsonRecord }> => {
    const response = await stub.fetch(await forwardRequest(
      new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify(payload) }),
      `https://project.hub2.internal${path}?project=${encodeURIComponent(canonical)}`, actor));
    return { response, data: (await response.json().catch(() => ({}))) as JsonRecord };
  };
  const name = text('name');
  const title = (name ?? brief?.split('\n')[0] ?? 'Interactive session').slice(0, 200);
  const intake = existingIntake ? { response: new Response(null, { status: 200 }), data: { id: existingIntake } as JsonRecord } : await call('/api/intakes', {
    key: `${key}:intake`, title,
    text: brief ?? 'Interactive session started with `hub sessions start`; await operator input.',
    acceptance_criteria: ['The session started on a host, did the requested work and ended cleanly.'],
  });
  const intakeId = (intake.data.intake as { id?: string } | undefined)?.id ?? (intake.data.id as string | undefined);
  if (!intake.response.ok || !intakeId) return json({ ...intake.data, error: intake.data.error ?? 'intake_create_failed', stage: 'intake' }, intake.response.ok ? 502 : intake.response.status);
  const createdHere = !existingIntake && intake.response.status === 201;
  const start: JsonRecord = {
    key, workspace, interactive, purpose, with_goal: body.with_goal === true,
    ...(brief ? { prompt: brief } : {}),
    ...(text('machine') ? { machine_id: text('machine') } : {}),
    ...(text('engine') ? { engine: text('engine') } : {}),
    ...(text('model') ? { model: text('model') } : {}),
    ...(text('effort') ? { effort: text('effort') } : {}),
    ...(text('tier') ? { tier: text('tier') } : {}),
    ...(text('engine_override_reason') ? { override_reason: text('engine_override_reason') } : {}),
    ...(requiredCapabilities ? { required_capabilities: requiredCapabilities } : {}),
  };
  const started = await call(`/api/intakes/${encodeURIComponent(intakeId)}/start`, start);
  const session = started.data.session as JsonRecord | undefined;
  if (!started.response.ok || !session?.id) {
    // The intake above exists only to carry this start; a refused start used to leave it open forever.
    // Discard it only when this very request created it (HTTP 201, never an idempotent replay) and the
    // refusal is definitive: 408/409/429 and 5xx are retried with the same key on the same intake.
    // ProjectDO applies the atomic fence (open, no session, no saga, no other event) and reports the state.
    const definitive = [400, 403, 404].includes(started.response.status);
    let discarded = false;
    if (createdHere && definitive) {
      const outcome = await call(`/api/intakes/${encodeURIComponent(intakeId)}/discard`, {
        key: `${key}:refused`, reason: `start refused: ${String(started.data.error ?? 'session_start_failed').slice(0, 80)}`,
      }).catch(() => undefined);
      discarded = outcome?.response.ok === true && outcome.data.discarded === true
        && (outcome.data.intake as JsonRecord | undefined)?.status === 'archived';
    }
    return json({ ...started.data, error: started.data.error ?? 'session_start_failed', stage: 'start', intake_id: intakeId,
      ...(discarded ? { intake_archived: true } : {}) }, started.response.ok ? 502 : started.response.status);
  }
  // 201 = offered to the host; 202 = accepted and dispatching.
  return json({ ...session, project: canonical, intake_id: intakeId }, started.response.status);
}

/** Optional observation never becomes a second dispatch/authorization dependency. */
async function observerBound<T>(work: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([work, new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('observer_unavailable')), 250);
  })]); } finally { if (timer !== undefined) clearTimeout(timer); }
}

/** `POST /api/model-picker/pick`: what the picker would choose right now (nothing is started). */
async function dryRunPick(request: Request, env: Hub2Env, actor: Actor): Promise<Response> {
  if (!admin(actor) && !actor.scopes.includes('session:start')) return error('forbidden', 403);
  let body: JsonRecord = {};
  try {
    const raw = await boundedText(request, 4096);
    if (raw.trim()) body = JSON.parse(raw) as JsonRecord;
  } catch { return error('invalid_json', 400); }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return error('invalid_json_object', 400);
  const text = (field: string): string | undefined => typeof body[field] === 'string' && (body[field] as string).trim() ? (body[field] as string).trim() : undefined;
  // This exact response is already mandatory before the monitor may decide to
  // mail. No extra RPC or local observer bootstrap is inserted into that path.
  let observer = request.headers.get('X-Hub-Alert-Producer') === 'credential_monitor'
    ? env.CREDENTIAL_MONITOR_EVIDENCE : undefined;
  let identity: unknown;
  try { identity = observer?.begin(); }
  catch { try { observer?.invalidate(); } catch {} observer = undefined; }
  const observed = async (decision: 'alert' | 'observation' | null) => {
    if (!observer) return;
    try { await observerBound(observer.finish(identity, decision)); }
    catch { try { observer.invalidate(); } catch {} }
  };
  try {
    const { route, diagnostics } = await pickModelRouteDetailed(env, {
      purpose: sessionPurpose(body.purpose),
      interactive: body.mode === 'interactive', with_goal: body.with_goal === true,
      ...(text('tier') ? { tier: text('tier') } : {}),
      ...(Array.isArray(body.needs) ? { required_capabilities: body.needs as string[] } : {}),
    });
    // Use precisely the existing public criterion, including credential
    // advisories. Observation never filters a primary notification to get zero.
    const excluded = [...diagnostics.excluded,...(diagnostics.advisories??[])].filter(row=>row.reason.startsWith('credential_'));
    await observed(excluded.length ? 'alert' : 'observation');
    return json({ route: [route.engine, route.model, route.effort ?? 'default'].join('/'), engine: route.engine, model: route.model,
      effort: route.effort ?? null, policy_id: route.policy_id, machine_id: route.machine_id, ...diagnostics, excluded });
  } catch (caught) {
    await observed(null);
    if (caught instanceof HttpError) return error(caught.code, caught.status);
    throw caught;
  }
}

async function routingSimulation(request: Request, env: Hub2Env, actor: Actor): Promise<Response> {
  if (!admin(actor) && !actor.scopes.includes('routing:read')) return error('forbidden',403);
  const params=requestUrl(request).searchParams;
  const fields=['n','purpose','tier','mode','machine_id','engine','model','effort','workspace'];
  if([...params.keys()].some(key=>!fields.includes(key)||params.getAll(key).length!==1))return error('invalid_simulation_parameters',400);
  const raw=params.get('n')??'1000';
  if(!/^[1-9][0-9]{0,3}$/.test(raw)||Number(raw)>1000)return error('invalid_simulation_count',400);
  if(params.has('mode')&&!['headless','interactive'].includes(params.get('mode')!))return error('invalid_session_mode',400);
  if(params.has('effort')&&!['low','medium','max'].includes(params.get('effort')!))return error('invalid_effort',400);
  const input:PickInput={interactive:params.get('mode')==='interactive',purpose:sessionPurpose(params.get('purpose')??undefined)};
  for(const key of ['tier','machine_id','engine','model','effort','workspace'] as const){
    const value=params.get(key);
    if(value!==null){
      if(!value.trim()||value.length>(key==='workspace'?2048:120)||/[\x00-\x1f\x7f]/.test(value))return error('invalid_simulation_parameters',400);
      input[key]=value;
    }
  }
  return json(await simulateModelRoutes(env,input,Number(raw)));
}

function safetyModeForRequest(
  request: Request,
  path: string,
  actor: Actor,
): 'operator_stop' | undefined | false {
  const value = requestUrl(request).searchParams.get('safety');
  if (value === null) return undefined;
  const isConnect = path === '/api/connect' && request.method === 'GET';
  const isStopAck = /^\/api\/sessions\/[^/]+\/commands\/[^/]+\/ack$/.test(path) && request.method === 'POST';
  if (
    value !== 'operator_stop' ||
    (!isConnect && !isStopAck) ||
    actor.kind !== 'machine' ||
    !actor.resource_id
  ) return false;
  return 'operator_stop';
}

function admin(actor: Actor): boolean {
  return actor.scopes.includes('*') || actor.scopes.includes('operator:admin');
}
function cursor(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}
function readCursor(value: string | null): any {
  if (!value) return {};
  if (value.length > 2048 || !/^[A-Za-z0-9_-]+$/.test(value))
    throw new HttpError('invalid_cursor', 400);
  try {
    return JSON.parse(atob(value.replace(/-/g, '+').replace(/_/g, '/')));
  } catch {
    throw new HttpError('invalid_cursor', 400);
  }
}
/** The enrolled host can verify its own running daemon without an operator credential. */
async function ownMachineConnection(env: Hub2Env, actor: Actor): Promise<Response> {
  if (actor.kind !== 'machine' || !actor.resource_id || !actor.scopes.includes('machine:offer'))
    return error('forbidden', 403);
  const machine = env.MACHINES.get(env.MACHINES.idFromName(actor.resource_id));
  const response = await machine.fetch('https://routing.internal/_internal/routing-snapshot');
  if (!response.ok) return error('routing_view_unavailable', 503);
  const { connected, host_version, protocol_version, version_mismatch } = await response.json() as JsonRecord;
  return json({ machine_id: actor.resource_id, connected: connected === true, host_version, protocol_version, version_mismatch }, 200, noStore);
}

/** `hub routing-machines`: registered host routing information. */
async function routingMachines(env: Hub2Env, actor: Actor): Promise<Response> {
  if (!admin(actor)) return error('forbidden', 403);
  const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
  const ids:string[]=[];let after='';
  do{
    const directory=await control.fetch(new Request(CONTROL+'/api/directory?kind=machines&limit=50'+(after?'&after='+encodeURIComponent(after):''),{
      headers:{'x-hub2-actor':JSON.stringify(actor)},
    }));
    if(!directory.ok)return directory;
    const page=await directory.json() as {items?:Array<{id:string}>;next?:string|null};
    ids.push(...(page.items||[]).map(machine=>machine.id));
    const next=page.next||'';if(next&&next<=after)return error('machine_directory_invalid_cursor',503);after=next;
  }while(after);
  const now = Date.now();
  const machines = await Promise.all(ids.map(async id => {
    const reply = await env.MACHINES.get(env.MACHINES.idFromName(id)).fetch('https://routing.internal/_internal/routing', {
      headers: { 'x-hub2-actor': JSON.stringify(actor) },
    });
    if (!reply.ok) return { machine_id: id, available: false, reason: 'routing_view_unavailable' };
    return await reply.json() as Record<string, unknown>;
  }));
  return json({ machines, observed_at: now });
}

async function reapSessions(request:Request,env:Hub2Env,actor:Actor):Promise<Response>{
  if(!admin(actor))return error('forbidden',403);
  const body=await bodyObject(request,4096);
  if(body.dry_run!==undefined&&typeof body.dry_run!=='boolean')return error('invalid_reap_request',400);
  const dryRun=body.dry_run===true,control=env.CONTROL.get(env.CONTROL.idFromName('global'));
  const headers={'x-hub2-actor':JSON.stringify(actor)},machines:string[]=[];
  let after='';
  do{
    const path=CONTROL+'/api/directory?kind=machines&limit=50'+(after?'&after='+encodeURIComponent(after):'');
    const response=await control.fetch(new Request(path,{headers}));
    if(!response.ok)return error('machine_directory_unavailable',503);
    const page=await response.json() as {items?:Array<{id:string}>;next?:string|null};
    machines.push(...(page.items||[]).map(row=>row.id));after=page.next||'';
  }while(after);
  const activeCount=async():Promise<number>=>{
    const snapshots=await Promise.all(machines.map(async machineId=>{
      try{
        const response=await env.MACHINES.get(env.MACHINES.idFromName(machineId)).fetch('https://machine.internal/_internal/live-sessions');
        if(!response.ok)return [];
        const view=await response.json() as {sessions?:LiveSession[]};
        return view.sessions||[];
      }catch{return [];}
    }));
    return new Set(snapshots.flat().filter(row=>isLiveActiveSession(row)).map(row=>row.session_id)).size;
  };
  const before=await activeCount(),results:JsonRecord[]=[];
  for(const machineId of machines){
    const response=await env.MACHINES.get(env.MACHINES.idFromName(machineId)).fetch('https://machine.internal/_internal/reap',{
      method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify({dry_run:dryRun}),
    });
    if(!response.ok)return json({error:'session_reap_failed',machine_id:machineId,status:response.status},503);
    results.push(await response.json() as JsonRecord);
  }
  return json({dry_run:dryRun,active_count_before:before,active_count_after:await activeCount(),
    reaped:results.reduce((sum,row)=>sum+(Number(row.reaped)||0),0),
    stop_requested:results.reduce((sum,row)=>sum+(Number(row.stop_requested)||0),0),machines:results});
}

async function machineProjection(
  request: Request,
  env: Hub2Env,
  actor: Actor,
  sessions: boolean,
): Promise<Response> {
  if (!admin(actor)) return error('forbidden', 403);
  const q = new URL(request.url).searchParams,
    limit = Number(q.get('limit') || 25);
  if (!Number.isInteger(limit) || limit < 1 || limit > (sessions ? 100 : 50)) return error('invalid_page', 400);
  const selectedMachine = q.get('machine_id');
  const live = q.get('live') === '1' ? '&live=1' : '';
  if (sessions && selectedMachine) {
    if (!/^[A-Za-z0-9_.-]{1,80}$/.test(selectedMachine)) return error('invalid_machine', 400);
    const response = await env.MACHINES.get(env.MACHINES.idFromName(selectedMachine)).fetch(
      await forwardRequest(
        request,
        'https://machine.internal/api/sessions?limit=' +
          limit +
          '&after=' +
          encodeURIComponent(q.get('after') || q.get('cursor') || '') + live,
        actor,
      ),
    );
    if (!response.ok) return response;
    const page = await response.json() as { items: unknown[]; next: string | null };
    return json({ ...page, sessions: page.items, next_cursor: page.next, complete: page.next === null });
  }
  const position = readCursor(q.get('after') || q.get('cursor'));
  if (
    !position ||
    typeof position !== 'object' ||
    Array.isArray(position) ||
    typeof (position.directory_after ?? '') !== 'string' ||
    typeof (position.session_after ?? '') !== 'string'
  )
    return error('invalid_page', 400);
  const control = env.CONTROL.get(env.CONTROL.idFromName('global')),
    headers = { 'x-hub2-actor': JSON.stringify(actor) };
  const directory = await control.fetch(
    new Request(
      CONTROL +
        '/api/directory?kind=machines&limit=' +
        (sessions ? 1 : Math.min(limit, 10)) +
        '&after=' +
        encodeURIComponent(position.directory_after || ''),
      { headers },
    ),
  );
  if (!directory.ok) return directory;
  const page = (await directory.json()) as {
      items: { id: string }[];
      next: string | null;
    },
    items: unknown[] = [];
  const inventories:JsonRecord[]=[];
  let next: string | null = null;
  if(sessions&&position.session_after&&(page.items.length!==1||position.session_machine!==page.items[0].id))return error('native_inventory_changed',409);
  for (const machine of page.items) {
    const stub = env.MACHINES.get(env.MACHINES.idFromName(machine.id)),
      path = sessions
        ? '/api/sessions?limit=' +
          limit +
          '&after=' +
          encodeURIComponent(position.session_after || '') + live
        : '/_internal/status';
    const r = await stub.fetch(new Request('https://machine.internal' + path, { headers }));
    if (r.status===409) return r;
    if (!r.ok) return error('machine_projection_unavailable', 503);
    const data = (await r.json()) as {
      items?: unknown[];
      next?: string | null;
      machine?: JsonRecord;
      sessions?: unknown[];
      attention?: unknown[];
      native_inventory?: JsonRecord;
    };
    if (sessions) {
      items.push(...(data.items || []));
      inventories.push({machine_id:machine.id,...(data.native_inventory??{complete:false,fresh:false,error:'inventory_unavailable'})});
      next = data.next
        ? cursor({
            directory_after: position.directory_after || '',
            session_after: data.next,
            session_machine: machine.id,
          })
        : page.next
          ? cursor({ directory_after: page.next })
          : null;
    } else {
      if (!data.machine) return error('machine_projection_unavailable', 503);
      items.push({
        ...data.machine,
        sessions: data.sessions,
        attention: data.attention,
      });
    }
  }
  if (!sessions) next = page.next ? cursor({ directory_after: page.next }) : null;
  return json({ items, next, complete: next === null, ...(sessions ? { sessions: items, next_cursor: next, native_inventories:inventories } : {}) });
}


function resourceSettings(request: Request, env: Hub2Env, actor: Actor): Response {
  if (actor.kind !== 'operator' || !admin(actor)) return json({ error: 'forbidden' }, 403, noStore);
  if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405, noStore);
  const configured = env.HUB2_DISK_FLOOR_GB;
  if (configured === undefined)
    return json({ error: 'resource_settings_unconfigured' }, 503, noStore);
  // Use the same fractional GiB range as the legacy placement setting, but
  // require this deployment to configure it explicitly.
  const floor = Number(configured);
  if (!/^[0-9]+(?:\.[0-9]+)?$/.test(configured) || !Number.isFinite(floor) || floor < 1 || floor > 1048576)
    return json({ error: 'invalid_resource_settings' }, 503, noStore);
  return json({ resource_settings: { disk_floor_gb: floor } }, 200, noStore);
}


/** Canonical quota account labels published by `hub quota publish`; unconfigured ones are skipped. */
const QUOTA_ACCOUNT_ENGINES: Record<string, string[]> = {
  claude: ['claude'], codex: ['codex'], 'codex-orin': ['codex'], gemini: ['gemini'],
  grok: ['grok'], copilot: ['copilot'], 'shared-go': ['opencode'],
};

/** Bounded, usage-only view of the accounts referenced by the routing policy. */
async function quotaProjection(request: Request, env: Hub2Env, actor: Actor): Promise<Response> {
  if (!admin(actor)) return error('forbidden', 403);
  const q = new URL(request.url).searchParams;
  if (q.get('view') && q.get('view') !== 'fleet') return error('invalid_view', 400);
  // Account evidence is fleet-wide, with no host attribution in Hub2. Do not
  // silently present fleet evidence as a measurement from one host.
  if (q.has('host_id')) return error('host_quota_unavailable', 400);
  for (const key of ['provider', 'engine', 'account'])
    if (q.has(key) && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(q.get(key)!))
      return error('invalid_filter', 400);
  const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
  const response = await control.fetch(await forwardRequest(request, CONTROL + '/api/model-policy', actor));
  let candidates: { engine: string; account_id: string }[] = [];
  if (response.ok) candidates = (await response.json() as { candidates: typeof candidates }).candidates;
  else if (response.status !== 404) return response;
  if (candidates.length > 32) return error('quota_projection_bound_exceeded', 503);
  const ids = [...new Set([...candidates.map(c => c.account_id), ...Object.keys(QUOTA_ACCOUNT_ENGINES), ...(env.HUB2_DEFAULT_ACCOUNT_ID ? [env.HUB2_DEFAULT_ACCOUNT_ID] : [])])];
  const pools: JsonRecord[] = [];
  for (const id of ids) {
    if (!/^[A-Za-z0-9_.-]{1,80}$/.test(id) || ['shared-zai', 'zcodex'].includes(id))
      return error('invalid_account', 503);
    const engines = [...new Set([...candidates.filter(c => c.account_id === id).map(c => c.engine), ...(QUOTA_ACCOUNT_ENGINES[id] ?? [])])];
    if ((q.has('account') && q.get('account') !== id) || (q.has('engine') && !engines.includes(q.get('engine')!))) continue;
    const r = await env.ACCOUNTS.get(env.ACCOUNTS.idFromName(id)).fetch(
      await forwardRequest(request, 'https://account.internal/api/account/quota', actor),
    );
    if (!r.ok) return error('quota_projection_unavailable', 503);
    const data = await r.json() as {
      account: { provider: string; enabled: number; min_remaining: number } | null;
      quota: { remaining: number; captured_at: number; reset_at: number | null } | null;
      fresh: boolean;
    };
    if (q.has('provider') && q.get('provider') !== data.account?.provider) continue;
    if (!data.account && !data.quota) continue; // well-known account label never configured
    pools.push({
      provider: data.account?.provider || 'unknown', account: id, engines,
      routing: data.account?.enabled ? 'enabled' : 'disabled',
      state: !data.quota ? 'unknown' : !data.fresh ? 'stale' : data.quota.remaining <= (data.account?.min_remaining ?? 0) ? 'exhausted' : 'fresh',
      remaining_percent: data.fresh ? data.quota?.remaining ?? null : null,
      observed_at: data.quota ? new Date(data.quota.captured_at).toISOString() : null,
      reset_at: data.quota?.reset_at ? new Date(data.quota.reset_at).toISOString() : null,
    });
  }
  return json({ pools, projection: 'hub2_accounts', complete: true }, 200, noStore);
}
