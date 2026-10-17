// LOCAL TEST ONLY. Copied into ignored reconstruction alongside immutable d9b4cde
// sources. Never imported from src/ or a production Wrangler entrypoint.
import worker, { Hub as ProductionHub } from '../src/worker';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

export class Hub extends ProductionHub {
  async gapFixture(fixture: any) {
    const now = new Date().toISOString();
    if (fixture.operation === 'expire') {
      this.sql.exec('UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ? AND seq = ?',
        new Date(Date.now() - 31 * 86400000).toISOString(), fixture.cid, fixture.seq);
      return this.conversationsService.retentionCleanup(Date.now(), 10);
    }
    // Keep the inherited production SCHEMA_SQL and all migrations/constraints.
    this.sql.exec('INSERT INTO users (sub,email,is_admin,first_seen_at,last_seen_at) VALUES (?,?,1,?,?)',
      'gap-fixture-human', 'gap-fixture@fake.local', now, now);
    this.sql.exec('INSERT INTO machines (id,name,hostname,registered_at) VALUES (?,?,?,?)',
      'gap-fixture-machine', 'Local fixture only', 'localhost', now);
    for (const entry of fixture.credentials) {
      this.sql.exec('INSERT INTO credentials (id,kind,token_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        entry.id, entry.kind || 'agent', await sha256(entry.token), entry.id + '-family',
        fixture.tag, 'gap-fixture-machine', entry.actor || fixture.project + ':' + fixture.tag,
        JSON.stringify(entry.scopes), 'gap-fixture-human', now,
        new Date(Date.now() + 3600000).toISOString(), entry.revoked ? now : null);
    }
    return { seeded: true };
  }
}

export default {
  ...worker,
  async fetch(request: Request, env: any, ctx: ExecutionContext) {
    if (new URL(request.url).pathname === '/__gap_fixture') {
      if (request.method !== 'POST' || request.headers.get('Authorization') !== 'Bearer ' + env.GAP_FIXTURE_KEY)
        return new Response(null, { status: 404 });
      const fixture = await request.json() as any;
      const stub = env.HUB.get(env.HUB.idFromName('hub'));
      const result = await stub.gapFixture(fixture);
      if (fixture.operation === 'expire') return Response.json(result);
      const cookie = await createSessionCookie(env, { sub: 'gap-fixture-human', email: 'gap-fixture@fake.local',
        isAdmin: true, csrfToken: env.GAP_FIXTURE_KEY, exp: Math.floor(Date.now() / 1000) + 3600 });
      return Response.json({ cookie: cookie.split(';')[0], csrf: env.GAP_FIXTURE_KEY });
    }
    return worker.fetch(request, env, ctx);
  },
};
