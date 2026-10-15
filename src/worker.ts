import { Hub } from './hub';

export { Hub };

export interface Env {
  HUB: DurableObjectNamespace<Hub>;
  ASSETS?: Fetcher;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/healthz') {
      url.pathname = '/api/healthz';
      return env.HUB.get(env.HUB.idFromName('hub')).fetch(new Request(url, request));
    }

    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname.startsWith('/device')) {
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      return stub.fetch(request);
    }

    if (!['/', '/index.html', '/fleet.html', '/fleet.js', '/fleet.css'].includes(url.pathname)) {
      return new Response('Not found', { status: 404 });
    }
    const stub = env.HUB.get(env.HUB.idFromName('hub'));
    const authUrl = new URL('/auth/me', request.url);
    const auth = await stub.fetch(new Request(authUrl, { headers: request.headers }));
    if (!auth.ok) {
      return new Response(null, { status: 303, headers: { Location: '/auth/login', 'Cache-Control': 'private, no-store' } });
    }
    if (!env.ASSETS) return new Response('Not found', { status: 404 });
    if (url.pathname === '/' || url.pathname === '/index.html') url.pathname = '/fleet.html';
    const asset = await env.ASSETS.fetch(new Request(url, request));
    const headers = new Headers(asset.headers);
    headers.set('Cache-Control', 'private, no-store');
    headers.set('X-Content-Type-Options', 'nosniff');
    headers.set('Referrer-Policy', 'same-origin');
    headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    return new Response(asset.body, { status: asset.status, headers });
  },

  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    const id = env.HUB.idFromName("hub");
    const stub = env.HUB.get(id);
    // The 30-minute cron keeps the existing root slot; every cron also runs due checks.
    if (event.cron !== '*/5 * * * *') await (stub as any).scheduledTick();
    await (stub as any).runChecks();
  }
};
