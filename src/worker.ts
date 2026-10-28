import { ConversationHub } from './conversation-hub';
import type { Hub as BaseHub } from './hub';

export const Hub = ConversationHub;
// Keep existing callers typed against the inherited public Hub contract.
export type Hub = BaseHub;

export interface Env {
  HUB: DurableObjectNamespace<BaseHub>;
  ASSETS?: Fetcher;
  AUTH_CALLBACK_URL?: string;
  AUTH_CALLBACK_URLS?: string;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    // CloudFront connects using the workers.dev origin and supplies its fixed
    // public hostname. Only configured OAuth callback origins can rewrite it,
    // so device links, logout and CSRF checks all see the browser's origin.
    const forwardedHost = request.headers.get('X-Forwarded-Host');
    if (forwardedHost) {
      const callbacks = [env.AUTH_CALLBACK_URL, ...(env.AUTH_CALLBACK_URLS || '').split(',')];
      for (const callback of callbacks) {
        try {
          const candidate = new URL((callback || '').trim());
          if (candidate.protocol === 'https:' && candidate.host === forwardedHost.trim().toLowerCase()) {
            url.protocol = candidate.protocol;
            url.host = candidate.host;
            request = new Request(url, request);
            break;
          }
        } catch {}
      }
    }
    // CloudFront supplies X-Forwarded-Host for the public hub host. Metrics and
    // quota reject that header as client spoofing, so strip it after the edge
    // has applied an allowlisted origin rewrite (or declined one).
    if (request.headers.has('X-Forwarded-Host')) {
      const headers = new Headers(request.headers);
      headers.delete('X-Forwarded-Host');
      request = new Request(request, { headers });
    }
    // Constant edge liveness only: no authentication, storage or upstream calls.
    // Readiness remains authenticated and exercises the existing Hub path.
    if (url.pathname === '/livez') {
      const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        return new Response(null, { status: 405, headers: { ...headers, Allow: 'GET, HEAD' } });
      }
      return new Response(request.method === 'HEAD' ? null : JSON.stringify({ status: 'ok', component: 'hub-worker' }), {
        headers: { ...headers, 'Content-Type': 'application/json' },
      });
    }
    if (url.pathname === '/healthz' || url.pathname === '/api/healthz') {
      url.pathname = '/api/healthz';
      try {
        const response = await env.HUB.get(env.HUB.idFromName('hub')).fetch(new Request(url, request));
        const headers = new Headers(response.headers);
        headers.set('Cache-Control', 'no-store');
        return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
      } catch {
        // No automatic replay: a reset is unavailable, never evidence of health.
        // Do not expose platform exception details or private configuration.
        return Response.json({ status: 'unavailable', component: 'hub-do' }, {
          status: 503, headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' },
        });
      }
    }

    // /webhooks/ is the public authenticated ingest surface (intake 19ec91c2);
    // it forwards to the DO like /api/ but its responses skip the internal
    // Server-Timing decoration to keep the hot path minimal.
    if (url.pathname === '/mcp' || url.pathname.startsWith('/oauth/') || url.pathname.startsWith('/.well-known/') || url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname.startsWith('/device') || url.pathname.startsWith('/webhooks/')) {
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      const started = Date.now();
      const response = await stub.fetch(request);
      if (!url.pathname.startsWith('/api/') || response.status === 101) return response;
      // Edge-to-DO elapsed time includes queueing, authentication and handler
      // work, excluding the client's TLS/network trip. No payloads are logged.
      const headers = new Headers(response.headers);
      headers.append('Server-Timing', `hub;dur=${Math.max(0, Date.now() - started)}`);
      return new Response(response.body, {status: response.status, statusText: response.statusText, headers});
    }

    // Public CLI installer seam (server routing only; CLI worker owns artifacts).
    // Anonymous READ ONLY for exactly these static assets; nothing else under
    // /cli/ is exposed and no API/static private artifact is opened up.
    if (url.pathname === '/cli/install.sh' || url.pathname === '/cli/install.ps1' || url.pathname === '/cli/hub.pyz' ||
        url.pathname === '/cli/hub.pyz.sha256' || url.pathname === '/cli/manifest.json') {
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET, HEAD' } });
      }
      if (!env.ASSETS) return new Response('Not found', { status: 404 });
      const asset = await env.ASSETS.fetch(new Request(url, request));
      if (!asset.ok) return new Response('Not found', { status: 404 });
      const headers = new Headers(asset.headers);
      headers.set('Cache-Control', 'public, max-age=300');
      headers.set('X-Content-Type-Options', 'nosniff');
      return new Response(asset.body, { status: asset.status, headers });
    }
    if (url.pathname.startsWith('/cli/')) {
      return new Response('Not found', { status: 404 });
    }

    // Sign-in needs these exact immutable styles/scripts/fonts before a session exists.
    const PUBLIC_AUTH_ASSETS = ['/auth.css', '/auth.js', '/vendor/dakit/device-code.js', '/vendor/dakit/dakit.css', '/vendor/dakit/shell.js',
      '/vendor/dakit/fonts/inter-var.woff2', '/vendor/dakit/fonts/ibm-plex-mono-400.woff2', '/vendor/dakit/fonts/ibm-plex-mono-500.woff2'];
    // Explicit private-asset allowlist: every asset is served only after the
    // authenticated-session check below. No arbitrary path, credential file
    // or vendored tool is exposed; the list covers exactly the conversations
    // page, its app code, the shipped vendored dakit bundle files it loads,
    // and the fonts those files reference.
    const PRIVATE_ASSETS = [
      '/', '/index.html',
      '/fleet', '/fleet.html', '/fleet.js', '/fleet.css',
      '/engines', '/engines.html', '/engines.js', '/engines.css',
      '/banked-resets.html', '/banked-resets.js',
      '/conversations', '/conversations.html', '/conversations.js', '/conversations.css',
      '/metrics', '/metrics.html', '/metrics.js', '/metrics-charts.js', '/metrics.css', '/host-health-charts.js', '/tbm.js', '/tbm.css', '/usage-history.js',
      '/usage', '/usage.html',
      '/sessions', '/sessions.html', '/sessions.js', '/sessions.css',
      '/intake', '/intake.html', '/intake.js', '/intake.css', '/intake-overview.js', '/resource-management.js',
      '/vendor/dakit/dakit.css', '/vendor/dakit/shell.js', '/vendor/dakit/split.js', '/vendor/dakit/dialogs.js',
      '/vendor/dakit/fonts/inter-var.woff2',
      '/vendor/dakit/fonts/ibm-plex-mono-400.woff2',
      '/vendor/dakit/fonts/ibm-plex-mono-500.woff2'
    ];
    // Phone-friendly intake detail URLs (/intake/<task-id>) are served by the
    // same static bundle; the client renders the detail from the path and only
    // ever GETs the task (opening a task never starts a session).
    const INTAKE_DETAIL = /^\/intake\/[A-Za-z0-9._:-]{1,128}$/;
    if (!PRIVATE_ASSETS.includes(url.pathname) && !PUBLIC_AUTH_ASSETS.includes(url.pathname) && !INTAKE_DETAIL.test(url.pathname)) {
      return new Response('Not found', { status: 404 });
    }
    const stub = env.HUB.get(env.HUB.idFromName('hub'));
    const authUrl = new URL('/auth/me', request.url);
    const auth = PUBLIC_AUTH_ASSETS.includes(url.pathname) ? null : await stub.fetch(new Request(authUrl, { headers: request.headers }));
    if (!PUBLIC_AUTH_ASSETS.includes(url.pathname) && !auth?.ok) {
      return new Response(null, { status: 303, headers: { Location: '/auth/login', 'Cache-Control': 'private, no-store' } });
    }
    if (!env.ASSETS) return new Response('Not found', { status: 404 });
    if (url.pathname === '/') url.pathname = '/index.html';
    if (url.pathname === '/metrics' || url.pathname === '/usage' || url.pathname === '/usage.html') url.pathname = '/metrics.html';
    if (url.pathname === '/sessions') url.pathname = '/sessions.html';
    if (url.pathname === '/fleet') url.pathname = '/fleet.html';
    if (url.pathname === '/conversations') url.pathname = '/conversations.html';
    if (url.pathname === '/engines') url.pathname = '/engines.html';
    if (url.pathname === '/intake' || INTAKE_DETAIL.test(url.pathname)) url.pathname = '/intake.html';
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
