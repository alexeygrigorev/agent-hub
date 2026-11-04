import { mcp } from './mcp';
import { publicAuthentication, cookieActor } from './auth';
import { safeEqual } from './crypto';
import { boundedText, error, HttpError, json } from './http';
import type { Actor, Hub2Env, JsonRecord } from './types';

const CONTROL = 'https://control.hub2.internal';
const BODY_LIMIT = 98_304;
let devActorRegistration: Promise<boolean> | undefined;

export default {
  async fetch(request: Request, env: Hub2Env): Promise<Response> {
    try {
      const path = publicPath(request.url);
      if (!path) return error('internal_route_forbidden', 404);
      const authResponse = await publicAuthentication(request, env);
      if (authResponse) return authResponse;
      if (path === '/health' && request.method === 'GET')
        return json({ ok: true, service: 'hub2' });
      // The dashboard is the only public route. Its bearer token is held in
      // memory after load; do not put static HTML behind API authentication.
      if (env.ASSETS && request.method === 'GET' && path === '/')
        return secureStatic(await env.ASSETS.fetch(request));
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

      if (path === '/mcp') return mcp(request, env, (r, e) => this.fetch(r, e), actor);
      const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
      if (
        path === '/api/model-policy' ||
        path === '/api/named-agents' ||
        /^\/api\/named-agents\/[^/]+\/grants$/.test(path) ||
        path === '/api/directory' ||
        path.startsWith('/api/routes/') ||
        path === '/api/shared-credentials' ||
        path.startsWith('/api/shared-credentials/')
      )
        return control.fetch(
          await forwardRequest(request, CONTROL + path + requestUrl(request).search, actor),
        );
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
        return json({
          fleet: await fleet.json(),
          service: 'hub2',
          projection: 'bounded_first_page',
        });
      }

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

      const projectPath =
        path === '/api/claims' ||
        /^\/api\/claims\/[^/]+(?:\/(?:renew|release))?$/.test(path) ||
        path === '/api/schedules' ||
        /^\/api\/schedules\/[^/]+(?:\/(?:pause|resume|cancel|occurrences))?$/.test(path) ||
        path === '/api/intakes' ||
        /^\/api\/intakes\/[^/]+(?:\/(?:start|history|append))?$/.test(path) ||
        path === '/api/intake/tasks' ||
        /^\/api\/intake\/tasks\/[^/]+(?:\/(?:start|history|append))?$/.test(path);
      if (projectPath) {
        const url = requestUrl(request);
        let requested = url.searchParams.get('project');
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
          return env.PROJECTS.get(env.PROJECTS.idFromName(route.owner)).fetch(await forwardRequest(request,'https://project.internal'+internalPath+url.search,actor));
        }
        if (!requested || requested.length > 180) return error('project_required', 400);
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
        return stub.fetch(
          await forwardRequest(
            request,
            `https://project.hub2.internal${internalPath}${url.search}`,
            actor,
          ),
        );
      }

      if (path === '/api/heartbeat' || path === '/api/offers' || path === '/api/connect') {
        if (actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
        const machine = env.MACHINES.get(env.MACHINES.idFromName(actor.resource_id));
        return machine.fetch(
          await forwardRequest(
            request,
            `https://machine.hub2.internal${path}${requestUrl(request).search}`,
            actor,
          ),
        );
      }
      const session = path.match(
        /^\/api\/sessions\/([^/]+)(?:\/(started|finish|admission|credentials|commands(?:\/[^/]+\/ack)?))?$/,
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
            b = JSON.parse(await boundedText(request.clone(), BODY_LIMIT));
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
          const body = JSON.parse(await boundedText(request.clone(), BODY_LIMIT)) as JsonRecord;
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
): Promise<Request> {
  const headers = new Headers(request.headers);
  for (const key of [...headers.keys()])
    if (key.toLowerCase().startsWith('x-hub2-')) headers.delete(key);
  if (actor) headers.set('x-hub2-actor', JSON.stringify(actor));
  if (mailbox) headers.set('x-hub2-mailbox', mailbox);
  if (deliverySession) headers.set('x-hub2-delivery-session', deliverySession);
  let body: ArrayBuffer | undefined;
  if (!['GET', 'HEAD'].includes(request.method)) {
    const text = await boundedText(request.clone(), BODY_LIMIT);
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
async function machineProjection(
  request: Request,
  env: Hub2Env,
  actor: Actor,
  sessions: boolean,
): Promise<Response> {
  if (!admin(actor)) return error('forbidden', 403);
  const q = new URL(request.url).searchParams,
    limit = Number(q.get('limit') || 25);
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) return error('invalid_page', 400);
  const selectedMachine = q.get('machine_id');
  if (sessions && selectedMachine) {
    if (!/^[A-Za-z0-9_.-]{1,80}$/.test(selectedMachine)) return error('invalid_machine', 400);
    return env.MACHINES.get(env.MACHINES.idFromName(selectedMachine)).fetch(
      await forwardRequest(
        request,
        'https://machine.internal/api/sessions?limit=' +
          limit +
          '&after=' +
          encodeURIComponent(q.get('after') || ''),
        actor,
      ),
    );
  }
  const position = readCursor(q.get('after'));
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
  let next: string | null = null;
  for (const machine of page.items) {
    const stub = env.MACHINES.get(env.MACHINES.idFromName(machine.id)),
      path = sessions
        ? '/api/sessions?limit=' +
          limit +
          '&after=' +
          encodeURIComponent(position.session_after || '')
        : '/_internal/status';
    const r = await stub.fetch(new Request('https://machine.internal' + path, { headers }));
    if (!r.ok) return error('machine_projection_unavailable', 503);
    const data = (await r.json()) as {
      items?: unknown[];
      next?: string | null;
      machine?: JsonRecord;
      sessions?: unknown[];
      attention?: unknown[];
    };
    if (sessions) {
      items.push(...(data.items || []));
      next = data.next
        ? cursor({
            directory_after: position.directory_after || '',
            session_after: data.next,
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
  return json({ items, next, complete: next === null });
}
