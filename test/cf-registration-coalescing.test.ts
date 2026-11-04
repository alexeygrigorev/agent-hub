import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {generateTokenPair} from '../src/auth/token';
import {handleRegistryRoutes} from '../src/registry';
import {fixtureSql} from './helpers/fixture-budget';

const body={hostname:'host.example',os:'linux',arch:'x86_64',network:'home',roles:['worker'],capabilities:{cpu_count:8}};
const registerScope='route:POST /api/machines/register';
function registrationTotals(h:any){
 const pending=h.meter.snapshot()[registerScope]??{calls:0,rows_read:0,rows_written:0};
 const total={calls:pending.calls,rows_read:pending.rows_read,rows_written:pending.rows_written};
 // First HTTP completion flushes constructor AND route totals. Observe only
 // this route's pending + durable counters, including its first response.
 // These are test-observer PK reads, not an unmetered business-query path.
 const hours=new Set<number>([h.registrationFixtureHour,Math.floor(Date.now()/3600000)]);
 expect(hours.size).toBeLessThanOrEqual(2);
 for(const hour of hours){
  const row=h.ctx.storage.sql.exec('SELECT calls,rows_read,rows_written FROM rows_read_stats WHERE slot=? AND scope=? AND hour=?',hour%192,registerScope,hour).toArray()[0];
  if(row){total.calls+=row.calls;total.rows_read+=row.rows_read;total.rows_written+=row.rows_written;}
 }
 return total;
}
const writes=(h:any)=>registrationTotals(h).rows_written;
const consumed=(h:any)=>{const row=h.rowBudget.status();return row.reserved_total-row.available_total;};
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('registration-coalescing-'+crypto.randomUUID())),async(h:any,state:any)=>{
 await h.rowBudget.begin(true);const day=h.rowBudget.status().day,seed=fixtureSql(h,state),pair=await generateTokenPair(),now=Date.now(),at=new Date(now).toISOString();
 seed.exec(`INSERT INTO machines(id,name,hostname,os,arch,network,roles,capabilities,registered_at,credential_id,approval_seq)
  VALUES('host','host',?,?,?,?,?,?,?,'owner',7)`,body.hostname,body.os,body.arch,body.network,JSON.stringify(body.roles),JSON.stringify(body.capabilities),at);
 const credential=(id:string,p:any=pair,family='family',used:string|null=at)=>seed.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,last_used_at,approval_seq)
  VALUES(?,'host',?,?,?,'host','host','machine:host','["host"]','fixture',?,?,?,7)`,id,p.accessHash,p.refreshHash,family,at,new Date(now+3600000).toISOString(),used);
 credential('owner');h.registrationFixtureHour=Math.floor(now/3600000);
 const initialGuard=consumed(h);
 const request=(payload:any=body,token=pair.accessToken)=>new Request('https://hub.test/api/machines/register',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(payload)});
 const post=(payload:any=body,token=pair.accessToken)=>h.fetch(request(payload,token));
 await fn({h,state,seed,pair,credential,request,post,now,at});
 console.log(JSON.stringify({evidence:'registration_native_route_cost',scope:registerScope,native_sql:registrationTotals(h),
  whole_guard_consumed_before:initialGuard,whole_guard_consumed_after:consumed(h),whole_guard_delta:consumed(h)-initialGuard,
  note:'guard includes first HTTP meter flush/KV/control costs; route counters include persisted first-response and pending native SQL'}));
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});

describe('registration writes only changed durable facts',()=>{
 it('accepts 100 identical HTTP registrations with zero route writes and only hourly auth-marker work',()=>fixture(async({h,state,seed,post,now}:any)=>{
  const lease=state.storage.sql.exec("SELECT * FROM leases WHERE name='root'").toArray();
  const before=writes(h);
  for(let i=0;i<100;i++)expect((await post()).status).toBe(200);
  expect(writes(h)-before).toBe(0);
  expect(state.storage.sql.exec("SELECT * FROM leases WHERE name='root'").toArray()).toEqual(lease);
  seed.exec("UPDATE credentials SET last_used_at=? WHERE id='owner'",new Date(now-3600001).toISOString());
  const hour=writes(h);for(let i=0;i<100;i++)expect((await post()).status).toBe(200);
  expect(writes(h)-hour).toBe(1);
 }));
 it('persists real payload and credential changes, and refuses stale/revoked/expired authority after cold reads',()=>fixture(async({h,state,seed,pair,credential,request,post,now,at}:any)=>{
  let payload:any={...body};
  for(const patch of [{hostname:'renamed'},{capabilities:{cpu_count:12}},{network:'public'},{roles:['writer']},{os:'other'},{arch:'arm64'}]){
   payload={...payload,...patch};const before=writes(h);expect((await post(payload)).status).toBe(200);expect(writes(h)).toBeGreaterThan(before);
   const row=state.storage.sql.exec("SELECT * FROM machines WHERE id='host'").one();
   expect(row).toMatchObject({hostname:payload.hostname,os:payload.os,arch:payload.arch,network:payload.network,roles:JSON.stringify(payload.roles),capabilities:JSON.stringify(payload.capabilities)});
  }
  const successor=await generateTokenPair();credential('successor',successor,'new-family');
  seed.exec("UPDATE machines SET credential_id='' WHERE id='host'");
  expect((await post(payload,successor.accessToken)).status).toBe(200);
  expect(state.storage.sql.exec("SELECT credential_id FROM machines WHERE id='host'").one().credential_id).toBe('successor');
  // A fresh SQL wrapper has no registration/read memo. The same durable
  // binding still succeeds without writes; old live families cannot reclaim it.
  const cold=h.meter.wrap(h.rowBudget.wrap(state.storage.sql));
  const before=writes(h);
  expect((await h.meter.run(registerScope,()=>handleRegistryRoutes(request(payload,successor.accessToken),'/api/machines/register',cold,state,env as any)))?.status).toBe(200);
  expect((await post(payload,pair.accessToken)).status).toBe(403);expect(writes(h)-before).toBe(0);
  // Model authoritative external revocation without warming/invalidation of
  // SQL memo state; the final live authority read must still refuse it.
  state.storage.sql.exec("UPDATE credentials SET revoked_at=? WHERE id='successor'",at);
  const revoked=writes(h);expect((await post(payload,successor.accessToken)).status).toBe(401);expect(writes(h)-revoked).toBe(0);
  state.storage.sql.exec("UPDATE credentials SET revoked_at=NULL,token_hash='rotated-proof' WHERE id='successor'");
  const rotated=writes(h);expect((await post(payload,successor.accessToken)).status).toBe(401);expect(writes(h)-rotated).toBe(0);
  seed.exec("UPDATE credentials SET token_hash=?,expires_at=? WHERE id='successor'",successor.accessHash,new Date(now-1).toISOString());
  const expired=writes(h);expect((await post(payload,successor.accessToken)).status).toBe(401);expect(writes(h)-expired).toBe(0);
 }));
 it('does not publish ignored CAS changes or retain success across transaction rollback',()=>fixture(async({h,state,seed,request,post}:any)=>{
  const payload={...body,hostname:'changed'};
  seed.exec("CREATE TRIGGER ignore_registration BEFORE UPDATE OF hostname ON machines BEGIN SELECT RAISE(IGNORE); END");
  const ignored=writes(h);expect((await post(payload)).status).toBe(409);expect(writes(h)-ignored).toBe(0);
  seed.exec('DROP TRIGGER ignore_registration');
  await expect(h.meter.run(registerScope,()=>handleRegistryRoutes(request(payload),'/api/machines/register',h.sql,state,env as any,(fn:any)=>state.storage.transactionSync(()=>{fn();throw new Error('rollback-registration');})))).rejects.toThrow('rollback-registration');
  expect(state.storage.sql.exec("SELECT hostname FROM machines WHERE id='host'").one().hostname).toBe(body.hostname);
  expect((await post(payload)).status).toBe(200);
  expect(state.storage.sql.exec("SELECT hostname FROM machines WHERE id='host'").one().hostname).toBe('changed');
  const unchanged=writes(h);expect((await post(payload)).status).toBe(200);expect(writes(h)-unchanged).toBe(0);
  state.storage.sql.exec("UPDATE machines SET credential_id='new-enrollment' WHERE id='host'");
  const stale=writes(h);expect((await post(payload)).status).toBe(403);expect(writes(h)-stale).toBe(0);
 }));
});
