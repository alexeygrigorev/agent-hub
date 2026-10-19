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
    if (url.pathname === '/healthz') {
      url.pathname = '/api/healthz';
      return env.HUB.get(env.HUB.idFromName('hub')).fetch(new Request(url, request));
    }

    if (url.pathname === '/mcp' || url.pathname.startsWith('/oauth/') || url.pathname.startsWith('/.well-known/') || url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/') || url.pathname.startsWith('/device')) {
      const id = env.HUB.idFromName("hub");
      const stub = env.HUB.get(id);
      return stub.fetch(request);
    }

    // Public CLI installer seam (server routing only; CLI worker owns artifacts).
    // Anonymous READ ONLY for exactly these static assets; nothing else under
    // /cli/ is exposed and no API/static private artifact is opened up.
    if (url.pathname === '/cli/install.sh' || url.pathname === '/cli/hub.pyz' ||
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
      '/fleet.html', '/fleet.js', '/fleet.css',
      '/conversations.html', '/conversations.js', '/conversations.css',
      '/metrics', '/metrics.html', '/metrics.js', '/metrics.css',
      '/usage', '/usage.html',
      '/sessions', '/sessions.html', '/sessions.js', '/sessions.css',
      '/vendor/dakit/dakit.css', '/vendor/dakit/shell.js', '/vendor/dakit/split.js', '/vendor/dakit/dialogs.js',
      '/vendor/dakit/fonts/inter-var.woff2',
      '/vendor/dakit/fonts/ibm-plex-mono-400.woff2',
      '/vendor/dakit/fonts/ibm-plex-mono-500.woff2'
    ];
    if (!PRIVATE_ASSETS.includes(url.pathname) && !PUBLIC_AUTH_ASSETS.includes(url.pathname)) {
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
