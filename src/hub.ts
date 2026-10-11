import { DurableObject } from "cloudflare:workers";

export interface Env {
}

export class Hub extends DurableObject {
  sql: SqlStorage;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.initDb();
  }

  private initDb() {
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS health_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_kind TEXT,
        subject TEXT,
        state TEXT,
        detail TEXT,
        at TEXT
      )
    `);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/api/healthz') {
      if (request.method !== 'GET') {
        return new Response('Method Not Allowed', { status: 405, headers: { 'Allow': 'GET' } });
      }
      return new Response(JSON.stringify({ status: 'ok', component: 'hub-do' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (url.pathname === '/internal/tick') {
      return new Response("Tick processed");
    }

    return new Response("Hub DO Not Found", { status: 404 });
  }
}
