// Local-only entrypoint. Never imported by src/worker.ts or production Wrangler.
// Seeds a human admin session and enrolled machine only. Agent tokens come from
// public device routes; this fixture never inserts credentials.
import worker, { Hub as ProductionHub } from '../src/worker';
import { createSessionCookie } from '../src/auth/session';

export class Hub extends ProductionHub {
  async expireLocalMessage(cid: string, seq: number) {
    this.sql.exec('UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ? AND seq = ?', new Date(Date.now() - 31 * 86400000).toISOString(), cid, seq);
    return this.conversationsService.retentionCleanup(Date.now(), 10);
  }

  async seedLocalFixture(fixture: { tag: string; project: string }) {
    const now = new Date().toISOString();
    this.sql.exec('INSERT INTO users (sub,email,is_admin,first_seen_at,last_seen_at) VALUES (?,?,1,?,?)', 'cint-human', 'cint@fake.local', now, now);
    this.sql.exec('INSERT INTO machines (id,name,hostname,registered_at) VALUES (?,?,?,?)', 'cint-machine', 'Local fixture', 'localhost', now);
    return { seeded: true, machine_id: 'cint-machine' };
  }
}

export default {
  ...worker,
  async fetch(request: Request, env: any, ctx: ExecutionContext) {
    const path = new URL(request.url).pathname;
    if (path === '/__local_fixture' || path === '/__local_fixture/expire') {
      if (request.method !== 'POST' || request.headers.get('Authorization') !== 'Bearer ' + env.LOCAL_FIXTURE_KEY) return new Response(null, { status: 404 });
      const fixture = await request.json() as any;
      const stub = env.HUB.get(env.HUB.idFromName('hub'));
      if (path.endsWith('/expire')) return Response.json(await stub.expireLocalMessage(fixture.cid, fixture.seq));
      await stub.seedLocalFixture(fixture);
      const cookie = await createSessionCookie(env, { sub: 'cint-human', email: 'cint@fake.local', isAdmin: true, csrfToken: env.LOCAL_FIXTURE_KEY, exp: Math.floor(Date.now() / 1000) + 3600 });
      return Response.json({ cookie: cookie.split(';')[0], csrf: env.LOCAL_FIXTURE_KEY, machine_id: 'cint-machine' });
    }
    return worker.fetch(request, env, ctx);
  },
};
