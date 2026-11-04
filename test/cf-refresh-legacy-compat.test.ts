import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {generateTokenPair} from '../src/auth/token';
import {fixtureSql,restartFixtureHub} from './helpers/fixture-budget';
const fixture=(fn:(x:any)=>Promise<void>)=>(runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('legacy-refresh-'+crypto.randomUUID())),async(h:any,state:any)=>{
 const seed=fixtureSql(h,state),now=new Date().toISOString();
 seed.exec('ALTER TABLE credentials DROP COLUMN refresh_rotated_at');
 seed.exec("INSERT INTO users VALUES('legacy-admin','admin@fake.local',1,?,?)",now,now);
 for(let i=0;i<1001;i++)seed.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,scopes,created_by_sub,created_at,expires_at,revoked_at)
  VALUES(?,'host',?,?,'noise','[]','noise',?,?,?)`,'noise-'+i,'noise-hash-'+i,'noise-family-'+i,now,now,now);
 await restartFixtureHub(h);await h.rowBudget.begin(true);const day=h.rowBudget.status().day;
 expect(seed.exec('PRAGMA table_info(credentials)').toArray().some((row:any)=>row.name==='refresh_rotated_at')).toBe(false);
 const mint=async(id:string)=>{
  const pair=await generateTokenPair();seed.exec(`INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
   VALUES(?,'human',?,?,?,'admin@fake.local','human:legacy-admin','["admin"]','legacy-admin',?,?)`,id,pair.accessHash,pair.refreshHash,id,now,new Date(Date.now()+3600000).toISOString());return pair;
 };
 const refresh=(token:string)=>h.fetch(new Request('https://hub/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:token})}));
 await fn({h,state,seed,mint,refresh});
 expect(h.rowBudget.status()).toMatchObject({day,limit_total:20000,stopped:false});
});
describe('legacy credentials retain human refresh replay safety without a constructor ALTER',()=>{
 it('rotates once, replays the same pair only within grace, and preserves family revocation and scopes',()=>fixture(async({h,seed,mint,refresh}:any)=>{
  const old=await mint('human-family');const response=await refresh(old.refreshToken);expect(response.status).toBe(200);
  const first:any=await response.json();expect(first.scope).toBe('admin');expect(first.machine_id).toBeNull();
  const replay=await refresh(old.refreshToken);expect(replay.status).toBe(200);const second:any=await replay.json();
  expect(second.access_token).toBe(first.access_token);expect(second.refresh_token).toBe(first.refresh_token);
  expect(seed.exec("SELECT id FROM credentials WHERE family_id='human-family'").toArray()).toHaveLength(2);
  expect(seed.exec("SELECT rotated_at FROM credential_refresh_rotations WHERE id='human-family'").one().rotated_at).toBeTruthy();
  seed.exec("UPDATE credential_refresh_rotations SET rotated_at=? WHERE id='human-family'",new Date(Date.now()-31000).toISOString());
  expect((await refresh(old.refreshToken)).status).toBe(400);
  expect(seed.exec("SELECT id FROM credentials WHERE family_id='human-family' AND revoked_at IS NULL").toArray()).toHaveLength(0);
  const denied=await mint('non-admin-scope');seed.exec("UPDATE credentials SET scopes='[\"host\"]' WHERE id='non-admin-scope'");
  expect((await refresh(denied.refreshToken)).status).toBe(400);
  expect(seed.exec("SELECT id FROM credential_refresh_rotations WHERE id='non-admin-scope'").toArray()).toHaveLength(0);
  const explicitlyRevoked=await mint('explicit-revocation');seed.exec("UPDATE credentials SET revoked_at=? WHERE id='explicit-revocation'",new Date().toISOString());
  expect((await refresh(explicitlyRevoked.refreshToken)).status).toBe(400);
  expect(h.meter.statementTop(4096).some((row:any)=>/ALTER TABLE credentials ADD COLUMN refresh_rotated_at/i.test(row.sql))).toBe(false);
 }));
 it('rolls back predecessor CAS, sidecar receipt and successor together before a safe retry',()=>fixture(async({h,state,seed,mint,refresh}:any)=>{
  const pair=await mint('rollback-family'),transaction=state.storage.transactionSync;
  state.storage.transactionSync=(fn:any)=>transaction.call(state.storage,()=>{const result=fn();throw new Error('refresh-rollback');return result;});
  try{expect((await refresh(pair.refreshToken)).status).toBe(500);}finally{state.storage.transactionSync=transaction;}
  expect(seed.exec("SELECT revoked_at FROM credentials WHERE id='rollback-family'").one().revoked_at).toBeNull();
  expect(seed.exec("SELECT id FROM credentials WHERE family_id='rollback-family'").toArray()).toHaveLength(1);
  expect(seed.exec("SELECT name FROM sqlite_master WHERE name='credential_refresh_rotations'").toArray()).toHaveLength(0);
  expect((await refresh(pair.refreshToken)).status).toBe(200);
  expect(seed.exec("SELECT id FROM credential_refresh_rotations WHERE id='rollback-family'").toArray()).toHaveLength(1);
 }));
});
