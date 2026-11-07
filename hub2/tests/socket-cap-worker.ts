// Test-only socket-capacity fixture; synthetic machine rows, no native process launches.
import base, { CoreControl, CoreMachine, CoreProject, CoreAccount, CoreMailbox } from './core-integration-worker.ts';

export { CoreProject, CoreAccount, CoreMailbox };

const machine = 'socket-cap-machine';

export class CountControl extends CoreControl {
  calls = 0;
  async fetch(r: Request) {
    const path = new URL(r.url).pathname;
    if (path === '/_socket/count') return Response.json({ calls: this.calls });
    if (path === '/_authorize') this.calls += 1;
    return super.fetch(r);
  }
}

type FixtureCtx = {
  storage: {
    sql: { exec(q: string, ...b: unknown[]): { toArray(): unknown[] } };
  };
  getWebSockets(tag?: string): unknown[];
  acceptWebSocket(ws: unknown, tags?: string[]): void;
};

export class SocketMachine extends CoreMachine {
  async fetch(r: Request) {
    const path = new URL(r.url).pathname;
    const ctx = (this as unknown as { ctx: FixtureCtx }).ctx;
    if (path === '/_socket/seed') {
      ctx.storage.sql.exec(
        'INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        'synthetic-pending', 'synthetic-session', machine, 1, 'synthetic-key', 'synthetic-digest', 'stop', null, 'pending', null, 1, 1);
      return Response.json({ seeded: true });
    }
    if (path === '/_socket/queue') return Response.json({ rows: ctx.storage.sql.exec('SELECT * FROM session_commands ORDER BY id').toArray() });
    if (path === '/_socket/state') return Response.json({ open: ctx.getWebSockets('hub2:' + machine).filter((s) => (s as { readyState: number }).readyState === 1).length });
    if (path === '/_socket/legacy') {
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair) as [unknown, { serializeAttachment(v: unknown): void }];
      const current = ctx.getWebSockets('hub2:' + machine).find((s) => (s as { readyState: number }).readyState === 1) as { deserializeAttachment(): Record<string, unknown> };
      const attachment = { ...current.deserializeAttachment() };
      delete attachment.connection_id;
      delete attachment.connected_at;
      server.serializeAttachment(attachment);
      ctx.acceptWebSocket(server, ['hub2:' + machine]);
      return new Response(null, { status: 101, webSocket: client } as ResponseInit);
    }
    if (path === '/_socket/routing') return (this as unknown as { core: { fetch(req: Request): Promise<Response> } }).core.fetch(new Request('https://machine.internal/_internal/routing'));
    return super.fetch(r);
  }
}

type Namespace = { idFromName(n: string): unknown; get(id: unknown): { fetch(req: Request): Promise<Response> } };

export default {
  async fetch(r: Request, env: { CONTROL: Namespace; MACHINES: Namespace; HUB2_DEV_OPERATOR_TOKEN: string }) {
    const path = new URL(r.url).pathname;
    if (!path.startsWith('/_socket/')) return base.fetch(r, env);
    if (r.headers.get('Authorization') !== 'Bearer ' + env.HUB2_DEV_OPERATOR_TOKEN) return new Response(null, { status: 401 });
    const ns = path === '/_socket/count' ? env.CONTROL : env.MACHINES;
    return ns.get(ns.idFromName(path === '/_socket/count' ? 'global' : machine)).fetch(r);
  },
};
