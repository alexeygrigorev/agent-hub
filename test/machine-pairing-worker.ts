// Local-only HTTP fixture. The agent credential is always issued by public device routes.
import worker, { Hub as ProductionHub } from '../src/worker';
import { createSessionCookie } from '../src/auth/session';
export class Hub extends ProductionHub {
 async pairingFixture(action?:string,code?:string) {
  if(action==='expire') {
   this.sql.exec("UPDATE device_pairings SET expires_at='2000-01-01T00:00:00Z' WHERE user_code=?",code!);return;
  }
  const now=new Date().toISOString();
  this.sql.exec("INSERT INTO users(sub,email,is_admin,first_seen_at,last_seen_at) VALUES ('pair-human','admin@fake.local',1,?,?)",now,now);
  this.sql.exec("INSERT INTO machines(id,name,hostname,registered_at) VALUES ('pair-local-machine','Local fixture','localhost',?)",now);
 }
}
export default {
 ...worker,
 async fetch(request:Request,env:any,ctx:ExecutionContext) {
  if(new URL(request.url).pathname==='/__pairing_fixture') {
   if(request.method!=='POST'||request.headers.get('Authorization')!=='Bearer '+env.LOCAL_FIXTURE_KEY)return new Response(null,{status:404});
   const body:any=await request.json().catch(()=>({}));
   await env.HUB.get(env.HUB.idFromName('hub')).pairingFixture(body.action,body.code);
   const cookie=await createSessionCookie(env,{sub:'pair-human',email:'admin@fake.local',isAdmin:true,csrfToken:env.LOCAL_FIXTURE_KEY,exp:Math.floor(Date.now()/1000)+3600});
   return Response.json({cookie:cookie.split(';')[0]});
  }
  return worker.fetch(request,env,ctx);
 }
};
