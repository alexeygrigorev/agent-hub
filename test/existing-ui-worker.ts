// Local-only entrypoint. Fixtures create a human and machines, never agent credentials.
import worker, {Hub as ProductionHub} from '../src/worker';
import {createSessionCookie} from '../src/auth/session';
export class Hub extends ProductionHub {
  async uiFixture(action: string, code?: string, tokenHash?: string) {
    if (action === 'auth-off' || action === 'auth-on') { this.env = {...this.env,HUB_ADMIN_TOKEN_SHA256:action === 'auth-on' ? tokenHash : undefined}; return; }
    if (action === 'phone-session') {
      // F7 phone pass: a stand-in running session row so the real capture/send
      // enqueue path (recorded ack states) can be driven from a phone-width
      // page. No host, placement or execution is involved.
      const id = crypto.randomUUID();
      const now = new Date().toISOString();
      this.sql.exec('INSERT OR REPLACE INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,parent_id,parent_source,session_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        id, 'human:ui-human', 'phone-fixture-' + id.slice(0, 8), JSON.stringify({ workspace: 'demo' }), null, 'ui-machine', 'codex', 'interactive', 'running', null, null, null, 'headless', now, now);
      return id;
    }
    if (action === 'auth-off' || action === 'auth-on') { this.env = {...this.env,HUB_ADMIN_TOKEN_SHA256:action === 'auth-on' ? tokenHash : undefined}; return; }
    if (action === 'expire-soon') { this.sql.exec('UPDATE device_pairings SET expires_at=? WHERE user_code=?',new Date(Date.now()+1500).toISOString(),code!); return; }
    if (action === 'expire') { this.sql.exec('UPDATE device_pairings SET expires_at=? WHERE user_code=?',new Date(Date.now()-1000).toISOString(),code!); return; }
    if (action !== 'seed') return;
    const now = new Date().toISOString();
    this.sql.exec("INSERT OR IGNORE INTO users(sub,email,is_admin,first_seen_at,last_seen_at) VALUES ('ui-human','admin@fake.local',1,?,?)",now,now);
    for (const [id,name] of [['ui-machine','Local workstation'],['ui-other','Other workstation']]) {
      this.sql.exec('INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities) VALUES (?,?,?,?,?,?,?)',id,name,'localhost',now,now,JSON.stringify({mem_avail_mb:32000,disk_free_gb:80,load:[1],gate:{launch_allowed:true,at:now}}),JSON.stringify({engines:['codex'],fleet_modes:['headless','interactive'],workspaces:['demo'],cpu_count:8}));
    }
  }
}
export default {
  ...worker,
  async fetch(request: Request, env: any, ctx: ExecutionContext) {
    if (new URL(request.url).pathname === '/__ui_fixture') {
      if(request.method !== 'POST' || request.headers.get('Authorization') !== 'Bearer '+env.LOCAL_FIXTURE_KEY) return new Response(null,{status:404});
      const body = await request.json() as any;
      const created = await env.HUB.get(env.HUB.idFromName('hub')).uiFixture(body.action,body.code,env.HUB_ADMIN_TOKEN_SHA256);
      const cookie = await createSessionCookie(env,{sub:'ui-human',email:'admin@fake.local',isAdmin:true,csrfToken:env.LOCAL_FIXTURE_KEY,exp:Math.floor(Date.now()/1000)+(body.action === 'expired-session' ? -60 : 3600)});
      return Response.json({cookie:cookie.split(';')[0], ...(typeof created === 'string' ? { created } : {})});
    }
    return worker.fetch(request,env,ctx);
  }
};
