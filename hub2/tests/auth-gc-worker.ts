// Isolated test-only composition; all credentials and expired rows are synthetic.
import worker from '../src/worker';
import {ControlDO as ActualControl} from '../src/control-do';
import {sha256Hex} from '../src/crypto';
export class AuthGcControl extends ActualControl {
 async fetch(r:Request):Promise<Response>{
  const p=new URL(r.url).pathname,self=this as any;
  if(p==='/_fixture/seed'){
   await self.ready;
   return self.sqlReceipts.withScope(async()=>{
    const b=await r.json() as any,now=Date.now(),expired=now-1000;
    self.db.transaction(self.ctx,()=>{
     for(let i=0;i<25;i++){
      self.db.run('INSERT INTO auth_login_states VALUES(?,?,?,?,?,?,?,0)','login-'+i,'browser','nonce','verifier','https://fixture/auth/callback','/',expired);
      self.db.run('INSERT INTO auth_devices VALUES(?,?,?,?,?,?,NULL,NULL)','device-'+i,'user-'+i,'fixture',expired,0,'pending');
      self.db.run('INSERT INTO auth_rate VALUES(?,1,?)','rate-'+i,expired);
     }
    });
    self.db.run('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email) VALUES(?,?,?,?,?,?,?,?,?,?,?)','fixture-credential','operator','human:fixture','fixture-generation',await sha256Hex(b.token),JSON.stringify(['operator:admin']),'fixture-family','hub-cli',now,now+3600000,'fixture@example.test');
    return Response.json({seeded:75});
   });
  }
  if(p==='/_fixture/remaining'){
   await self.ready;
   return self.sqlReceipts.withScope(async()=>Response.json({remaining:['auth_login_states','auth_devices','auth_rate'].map(t=>self.db.one('SELECT COUNT(*) AS n FROM '+t).n)}));
  }
  return super.fetch(r);
 }
}
export default {async fetch(r:Request,env:any):Promise<Response>{
 const u=new URL(r.url),id=u.searchParams.get('case')||'device';
 const control=env.CONTROL.get(env.CONTROL.idFromName(id));
 if(u.pathname==='/_fixture/seed')return control.fetch(new Request('https://control/_fixture/seed',r));
 if(u.pathname==='/_fixture/remaining')return control.fetch('https://control/_fixture/remaining');
 const scoped={...env,CONTROL:{idFromName:()=>id,get:()=>control}};
 return worker.fetch(r,scoped);
}};
