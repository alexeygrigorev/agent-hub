import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {generateTokenPair} from '../src/auth/token';
import {fixtureSql} from './helpers/fixture-budget';
describe('writer intent observation failure through authenticated HTTP',()=>{
 it('returns explicit503 without granting a job token or changing lease/state',async()=>{
  await (runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('writer-intent-http-'+crypto.randomUUID())),async(h:any,state:any)=>{
   await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),now=Date.now(),at=new Date(now).toISOString(),pair=await generateTokenPair();
   seed.exec('UPDATE root_scheduling SET enabled=0 WHERE id=1');
   seed.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,last_heartbeat,capabilities,credential_id) VALUES('host','host','host',?,?,?,?, 'writer-host-credential')",at,at,JSON.stringify({gate:{launch_allowed:true}}),JSON.stringify({cpu_count:8}));
   seed.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,last_used_at)
     VALUES('writer-host-credential','host',?,?,'writer-family','host','host','machine:host','["host"]','fixture',?,?,?)`,pair.accessHash,pair.refreshHash,at,new Date(now+3600000).toISOString(),at);
   for(let i=0;i<129;i++)seed.exec("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor) VALUES(?,'root-run',?,'{','failed',?,?,'fixture')",'retained-'+i,'retained-'+i,at,at);
   seed.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_host_id,offer_expires_at)
     VALUES('current','writer-run','current','{"date":"2026-11-04"}','offered',?,?,'fixture','host',?)`,at,new Date(now+1200000).toISOString(),new Date(now+60000).toISOString());
   const consumed=()=>{const x=h.rowBudget.status();return x.reserved_total-x.available_total;},before=consumed();
   const response=await h.fetch(new Request('https://hub.test/api/jobs/current/claim',{method:'POST',headers:{Authorization:'Bearer '+pair.accessToken,'Content-Type':'application/json'},body:JSON.stringify({gate:true})}));
   expect(response.status).toBe(503);expect(await response.json()).toEqual({error:'writer_intent_observation_pending',observation_pending:true});
   expect(consumed()-before).toBeLessThan(400);
   expect(state.storage.sql.exec("SELECT status,claim_token_hash,lease_epoch FROM jobs WHERE id='current'").one()).toMatchObject({status:'offered',claim_token_hash:null,lease_epoch:null});
   expect(state.storage.sql.exec("SELECT id FROM credentials WHERE kind='job' AND subject='current'").toArray()).toEqual([]);
   expect(state.storage.sql.exec("SELECT name FROM leases WHERE holder_job_id='current'").toArray()).toEqual([]);
   expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
  });
 });
});
