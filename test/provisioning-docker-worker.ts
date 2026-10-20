// Local-only fixture entrypoint; never referenced by production wrangler.jsonc.
import worker, { Hub as ProductionHub } from '../src/worker';
import { createSessionCookie } from '../src/auth/session';
export class Hub extends ProductionHub {
  async fixtureCookie() {
    const at = new Date().toISOString();
    this.sql.exec("INSERT OR IGNORE INTO users VALUES ('docker-admin','admin@fake.local',1,?,?)", at, at);
    const cookie = await createSessionCookie(this.env, { sub: 'docker-admin', email: 'admin@fake.local', isAdmin: true, csrfToken: 'docker-csrf', exp: Math.floor(Date.now()/1000) + 3600 });
    return cookie.split(';')[0];
  }
}
export default {
  ...worker,
  async fetch(request: Request, env: any, ctx: ExecutionContext) {
    if (new URL(request.url).pathname === '/__docker_fixture') {
      if (request.method !== 'POST' || request.headers.get('Authorization') !== 'Bearer local-docker-fixture-only') return new Response(null, {status: 404});
      return Response.json({ cookie: await env.HUB.get(env.HUB.idFromName('hub')).fixtureCookie() });
    }
    return worker.fetch(request, env, ctx);
  }
};
