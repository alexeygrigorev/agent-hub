import { Hub } from './hub';

export { Hub };

export interface Env {
  HUB: DurableObjectNamespace<Hub>;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') {
      if (request.method !== 'GET') {
        return new Response('Method Not Allowed', { status: 405, headers: { 'Allow': 'GET' } });
      }
      return new Response(JSON.stringify({ status: 'ok', component: 'worker' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) {
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      return stub.fetch(request);
    }

    return new Response('Not found', { status: 404 });
  },

  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    const id = env.HUB.idFromName("hub");
    const stub = env.HUB.get(id);
    const req = new Request("http://hub/internal/tick", { method: "POST" });
    await stub.fetch(req);
  }
};
