import worker from './src/worker';
import type { Hub2Env } from './src/types';

type ProductionEnv = Hub2Env & { HUB2_RELEASE_MODE?: string };

export { ControlDO } from './src/control-do';
export { ProjectDO } from './src/project-do';
export { MachineDO } from './src/machine-do';
export { AccountDO } from './src/account-do';
export { MailboxDO } from './src/mailbox-do';
export { BudgetDO } from './src/budget-do';

const unavailable = (health = false) => Response.json(
  health ? { status: 'maintenance', dispatch_enabled: false } : { error: 'release_maintenance', dispatch_enabled: false },
  { status: health ? 200 : 503, headers: { 'Cache-Control': 'no-store' } },
);

const bootstrapPublicMethods: Readonly<Record<string, readonly string[]>> = {
  '/device': ['GET'],
  '/auth/login': ['GET'],
  '/auth/callback': ['GET'],
  '/auth/session': ['GET'],
  '/auth/logout': ['POST'],
  '/api/device/code': ['POST'],
  '/api/device/token': ['POST'],
  '/api/device/approve': ['POST'],
  '/api/token/refresh': ['POST'],
  '/api/token/revoke': ['POST'],
};

function bootstrapRequestAllowed(request: Request): boolean {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  if (bootstrapPublicMethods[path]?.includes(method)) return true;
  if (path === '/api/repos') return method === 'GET';
  const repoPath = path.match(/^\/api\/repos\/([^/]+)$/);
  if (repoPath) {
    try {
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(decodeURIComponent(repoPath[1]))) return false;
      return method === 'GET' || method === 'PATCH';
    } catch { return false; }
  }
  if (path === '/api/model-policy') return method === 'GET' || method === 'PUT';
  if (path === '/api/enrollments/pair' || path === '/api/enrollments/complete') return method === 'POST';
  if (path === '/api/heartbeat') return method === 'POST';
  if (['/api/fleet', '/api/fleet/machines', '/api/sessions', '/api/status', '/api/budget'].includes(path))
    return method === 'GET';

  const accountPath = path.match(/^\/api\/(?:accounts\/[A-Za-z0-9_.-]{1,80}\/api\/)?account(?:\/(credentials|hosts|discovery|quota|resets|alerts))?$/);
  if (!accountPath) return false;
  switch (accountPath[1] || '') {
    case '': return method === 'GET' || method === 'PATCH';
    case 'credentials': return method === 'PUT';
    case 'hosts': return method === 'GET' || method === 'PUT';
    case 'discovery': return method === 'GET' || method === 'PUT' || method === 'POST';
    case 'quota': return method === 'GET';
    case 'resets':
    case 'alerts': return method === 'GET';
    default: return false;
  }
}

function bootstrapResponse(request: Request): Response {
  const health = new URL(request.url).pathname === '/health' && request.method === 'GET';
  return Response.json(
    health ? { status: 'bootstrap', dispatch_enabled: false } : { error: 'release_bootstrap', dispatch_enabled: false },
    { status: health ? 200 : 503, headers: { 'Cache-Control': 'no-store' } },
  );
}

export default {
  fetch(request: Request, env: ProductionEnv): Promise<Response> {
    if (env.HUB2_RELEASE_MODE === 'active') return worker.fetch(request, env);
    if (env.HUB2_RELEASE_MODE === 'bootstrap') {
      if (new URL(request.url).pathname === '/health' && request.method === 'GET') return Promise.resolve(bootstrapResponse(request));
      return bootstrapRequestAllowed(request) ? worker.fetch(request, env) : Promise.resolve(bootstrapResponse(request));
    }
    return Promise.resolve(unavailable(new URL(request.url).pathname === '/health' && request.method === 'GET'));
  },
  async scheduled(event: ScheduledController, env: ProductionEnv): Promise<void> {
    if (env.HUB2_RELEASE_MODE === 'active') await worker.scheduled(event, env);
  },
};
