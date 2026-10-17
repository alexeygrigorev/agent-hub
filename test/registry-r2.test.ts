import { describe, it, expect } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { Hub } from '../src/conversation-hub';
import { SCHEMA_SQL } from '../src/schema';
import { bindAgentCredentials, registryAddress, validRegistryName, registryNameSql } from '../src/agent_registry';
import type { Env } from '../src/env';

const bindings = env as unknown as Env;
const stub = () => bindings.HUB.get(bindings.HUB.idFromName('hub'));
const csrf = 'r2-local-csrf';
async function admin() { return (await createSessionCookie(bindings, { sub:'r2-owner', email:'admin@fake.local', isAdmin:true, csrfToken:csrf, exp:Math.floor(Date.now()/1000)+3600 })).split(';')[0]; }
async function req(path:string, method='GET', body?:any, auth?:string) {
  const headers:Record<string,string> = {};
  if (auth?.startsWith('hubt_')) headers.Authorization='Bearer '+auth;
  else if(auth) { headers.Cookie=auth; headers['X-CSRF-Token']=csrf; }
  if(body!==undefined)headers['Content-Type']='application/json';
  return SELF.fetch('https://hub.test'+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
}
async function rows(q:string,...params:any[]) { let out:any[]=[]; await runInDurableObject(stub() as any,(hub:Hub)=>{out=hub.sql.exec(q,...params).toArray();});return out; }
const register = (cookie:string, tag:string, project?:string|null) => req('/api/agents','POST',{tag,project},cookie);
async function cred(actor:string, kind='agent', scopes=['bus:read','bus:send']) {
  const token='hubt_'+crypto.randomUUID().replaceAll('-','');
  await rows(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)`,token,kind,await sha256(token),token,actor,actor,JSON.stringify(scopes),'r2-owner',new Date().toISOString(),new Date(Date.now()+3600000).toISOString());return token;
}
async function code(actor:string) { return req('/api/device/code','POST',{client_kind:'agent',name:'local-test',actor,requested_scopes:['bus:read','bus:send','fleet:launch']}); }
async function pair(cookie:string,actor:string) {
  const c=await code(actor);expect(c.status).toBe(200);const p=await c.json() as any;
  const a=await req('/api/device/approve/'+p.user_code,'POST',{},cookie);expect(a.status).toBe(200);
  const t=await req('/api/device/token','POST',{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:p.device_code});expect(t.status).toBe(200);return await t.json() as any;
}
const send=(auth:string,from:string,to:string,key=crypto.randomUUID())=>req('/api/bus/send','POST',{from_actor:from,to_actor:to,body:'local private message',kind:'note',idempotency_key:key},auth);

describe('R2 independent-review counterexamples through SELF Worker and bound Hub',()=>{
  for(const project of [null,'']) it('R1 rejects both projectless collision orders and preserves legacy identity '+JSON.stringify(project),async()=>{
    const c=await admin();const tag=project===null?'legacy-null':'legacy-empty';expect((await register(c,tag,project)).status).toBe(201);
    const tokens=await pair(c,tag);
    expect((await register(c,tag,'pA')).status).toBe(409);
    const challenge=await (await code('pA:'+tag)).json() as any;
    expect((await req('/api/device/approve/'+challenge.user_code,'POST',{},c)).status).toBe(409);
    expect((await rows('SELECT status FROM device_pairings WHERE user_code = ?',challenge.user_code))[0].status).toBe('pending');
    expect((await req('/api/conversations','GET',undefined,tokens.access_token)).status).toBe(200);
    expect((await req('/api/bus/inbox?actor='+tag,'GET',undefined,tokens.access_token)).status).toBe(200);
    await expect(rows("INSERT INTO agents(tag,project) VALUES (?,'pA')",tag)).rejects.toThrow('agent_exists');
    expect((await req('/api/agents/'+tag,'DELETE',undefined,c)).status).toBe(200);
    expect((await register(c,tag,'pA')).status).toBe(201);expect((await register(c,tag,'pB')).status).toBe(201);
    expect((await register(c,tag,project)).status).toBe(409);
    expect((await req('/api/agents/'+tag,'DELETE',undefined,c)).status).toBe(409);
  });

  it('R2 rejects reserved tag AND project in registration, device code, approval, TS and SQL',async()=>{
    const c=await admin();const names=['human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub','ROOT-child'];
    for(const name of names) for(const n of [name,name.toUpperCase()]) {
      for(const actor of [n,'pA:'+n,n+':worker']) {
        expect(registryAddress(actor)).toBeNull();expect((await code(actor)).status).toBe(400);
      }
      expect((await register(c,n)).status).toBe(400);
      expect((await register(c,n,'pA')).status).toBe(400);
      expect((await register(c,'worker',n)).status).toBe(400);
      expect((await rows('SELECT '+registryNameSql('?')+' AS valid',...Array(5).fill(n)))[0].valid).toBe(0);
      expect(validRegistryName(n)).toBe(false);
    }
    const challenge=await (await code('good-worker')).json() as any;
    await rows('UPDATE device_pairings SET actor = ? WHERE user_code = ?','pA:root',challenge.user_code);
    expect((await req('/api/device/approve/'+challenge.user_code,'POST',{},c)).status).toBe(400);
    expect((await rows('SELECT status FROM device_pairings WHERE user_code = ?',challenge.user_code))[0].status).toBe('pending');
  });

  it('R2 imported reserved rows cannot impersonate host/role, canonicalize admin/system, or enter root selection',async()=>{
    const c=await admin();expect((await register(c,'receiver','pA')).status).toBe(201);
    await rows("INSERT INTO machines(id,name,hostname,registered_at) VALUES ('node1','local','local',?)",new Date().toISOString());
    for(const [project,tag,role] of [['machine','node1','root'],['role','ops','worker'],['pA','admin','worker'],['pA','system','worker'],['pA','root','worker']])
      await rows('INSERT INTO agents(tag,project,role,state,health_state) VALUES (?,?,?,\'active\',\'healthy\')',tag,project,role);
    const importedChallenge=await (await code('node1')).json() as any;
    expect((await req('/api/device/approve/'+importedChallenge.user_code,'POST',{},c)).status).toBe(400);
    const m=await cred('node1'),role=await cred('ops'),fakeAdmin=await cred('pA:admin'),fakeSystem=await cred('pA:system');
    const message=await send(c,'admin','machine:node1');expect(message.status).toBe(200);const id=(await message.json() as any).id;
    for(const [token,actor] of [[m,'machine:node1'],[role,'role:ops'],[fakeAdmin,'admin'],[fakeSystem,'system']]) {
      expect((await req('/api/bus/inbox?actor='+actor,'GET',undefined,token)).status).toBe(403);
      expect((await req('/api/bus/messages/'+id,'GET',undefined,token)).status).toBe(403);
      expect((await send(token,actor,'pA:receiver')).status).toBe(403);
      expect((await req('/api/conversations','GET',undefined,token)).status).toBe(403);
    }
    for(const actor of ['admin','system','root','principal','hub']) {
      await rows(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES (?,?,?,'note',?,'local','local',?,?)`,actor,actor,'pA:receiver',actor,Date.now(),Date.now()+60000);
      expect((await rows('SELECT from_actor FROM messages WHERE id = ?',actor))[0].from_actor).toBe(actor);
    }
    await runInDurableObject(stub() as any,(hub:Hub)=>{
      for(const a of ['admin','system','root','role:ops','machine:node1'])expect(hub.busService.canonicalActor(a)).toBe(a);
      expect(hub.jobsService.evaluateRootIncumbent().registered_root).toBeNull();
    });
    const receiver=await cred('pA:receiver');
    expect((await req('/api/bus/inbox?actor=role:root','GET',undefined,receiver)).status).toBe(403);
    expect((await send(receiver,'pA:receiver','role:root')).status).toBeGreaterThanOrEqual(400);
  });

  it('R3 unknown/ambiguous/qualified-unregistered issuance binds immutable denial, never promoting',async()=>{
    const c=await admin();await register(c,'ambig','pA');await register(c,'ambig','pB');
    const tokens=[await cred('ambig'),await cred('future'),await cred('pA:future-qualified')];
    for(const t of tokens)expect((await rows('SELECT registry_actor FROM credentials WHERE id = ?',t))[0].registry_actor).toBe('');
    await req('/api/agents/pA:ambig','DELETE',undefined,c);await req('/api/agents/pB:ambig','DELETE',undefined,c);
    await register(c,'ambig');await register(c,'future');await register(c,'future-qualified','pA');
    for(const t of tokens) {
      expect((await req('/api/conversations','GET',undefined,t)).status).toBe(403);
      expect((await req('/api/bus/messages','GET',undefined,t)).status).toBe(403);
      await expect(rows('UPDATE credentials SET registry_actor = ? WHERE id = ?','future',t)).rejects.toThrow('immutable_agent_binding');
    }
  });

  it('R3 deletion revokes exact bound families, cannot revive on same/different-project registration or refresh',async()=>{
    const c=await admin();await register(c,'lifecycle','pA');
    const bare=await cred('lifecycle');const a=await pair(c,'pA:lifecycle');
    await register(c,'lifecycle','pB');const b=await pair(c,'pB:lifecycle');
    expect((await req('/api/agents/pA:lifecycle','DELETE',undefined,c)).status).toBe(200);
    for(const token of [bare,a.access_token]) {
      expect((await req('/api/bus/messages','GET',undefined,token)).status).toBe(401);
      expect((await req('/api/conversations','GET',undefined,token)).status).toBe(401);
    }
    expect((await req('/api/token/refresh','POST',{grant_type:'refresh_token',refresh_token:a.refresh_token})).status).toBe(400);
    expect((await req('/api/conversations','GET',undefined,b.access_token)).status).toBe(200);
    await register(c,'lifecycle','pA');
    for(const token of [bare,a.access_token])expect((await req('/api/conversations','GET',undefined,token)).status).toBe(401);
    // Direct SQL deletion obeys the same lifetime fence.
    await rows("DELETE FROM agents WHERE project='pB' AND tag='lifecycle'");
    expect((await req('/api/bus/messages','GET',undefined,b.access_token)).status).toBe(401);
  });

  it('R3 registry/credential metadata edits cannot reinterpret immutable actor',async()=>{
    const c=await admin();await register(c,'movable','pA');const t=await cred('movable');
    await rows('UPDATE credentials SET actor = ?, subject = ? WHERE id = ?','pB:movable','pB:movable',t);
    expect((await req('/api/conversations','GET',undefined,t)).status).toBe(200);
    await rows("UPDATE agents SET project='pB' WHERE tag='movable'");
    expect((await req('/api/conversations','GET',undefined,t)).status).toBe(403);
    expect((await req('/api/bus/messages','GET',undefined,t)).status).toBe(403);
    expect((await rows('SELECT registry_actor FROM credentials WHERE id = ?',t))[0].registry_actor).toBe('pA:movable');
  });

  it('R4 accepts migrated bare binding in real HTTP claims/admission, not raw actor or reassignment',async()=>{
    const c=await admin();await register(c,'m6worker','m6proj');
    // Genuine old credentials schema, with issuance after the existing registration.
    const t='hubt_'+crypto.randomUUID().replaceAll('-','');const hash=await sha256(t);
    await runInDurableObject(stub() as any,(hub:Hub,state)=>{
      hub.sql.exec('DROP TABLE credentials');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS credentials ('),SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS device_pairings')));
      hub.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,'agent',?,?,'m6worker','m6worker','["bus:read","bus:send","fleet:launch"]','local',?,?)`,t,hash,t,new Date(Date.now()+1000).toISOString(),new Date(Date.now()+3600000).toISOString());
      new Hub(state,bindings);
    });
    expect((await rows('SELECT actor,registry_actor FROM credentials WHERE id=?',t))[0]).toEqual({actor:'m6worker',registry_actor:'m6proj:m6worker'});
    const claim=await req('/api/bus/claims','POST',{project:'m6proj',scope:'src',holder:'m6proj:m6worker',ttl_ms:60000},t);expect(claim.status).toBe(200);const id=(await claim.json() as any).id;
    expect((await req('/api/fleet/claim-policy','PUT',{workspaces:[{workspace:'demo',project:'m6proj',require_claim:true}]},c)).status).toBe(200);
    const input={workspace:'demo',prompt:'Local input only',approved_action:'Edit declared source',idempotency_key:'r2-admission',access:{mode:'edit',claim_id:id,project:'m6proj',scopes:['src/a.ts']}};
    const accepted=await req('/api/fleet/tasks','POST',input,t);expect(accepted.status).toBe(201);
    const job=(await accepted.json() as any).id;
    const stored=(await rows('SELECT request_json FROM routing_jobs WHERE id=?',job))[0];
    expect(JSON.parse(stored.request_json).edit_authority.actor).toBe('m6proj:m6worker');
    await rows("UPDATE agents SET project='other' WHERE tag='m6worker'");
    expect((await req('/api/fleet/tasks','POST',{...input,idempotency_key:'different'},t)).status).toBe(403);
  });

  it('R3 migration snapshots only provable registered credentials and never adopts pre-registration bare history',async()=>{
    const c=await admin();
    await rows("INSERT INTO agents(tag,project,registered_at) VALUES ('proven-legacy',NULL,'2020-01-01'),('late-history','historyProject','2026-01-01'),('legacy-ambig','pA','2020-01-01'),('legacy-ambig','pB','2020-01-01'),('pre-registration','lateProject','2026-01-01')");
    const actors=['proven-legacy','legacy-ambig','unknown-legacy','pre-registration'];
    const tokens=actors.map(a=>'hubt_legacy_'+a);
    const hashes=await Promise.all(tokens.map(sha256));
    await runInDurableObject(stub() as any,(hub:Hub,state)=>{
      hub.sql.exec('DROP TABLE credentials');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS credentials ('),SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS device_pairings')));
      for(let i=0;i<actors.length;i++)hub.sql.exec(`INSERT INTO credentials(id,kind,token_hash,family_id,subject,actor,scopes,created_by_sub,created_at,expires_at) VALUES (?,'agent',?,?,?,?,'["bus:read","bus:send"]','local','2021-01-01',?)`,tokens[i],hashes[i],tokens[i],actors[i],actors[i],new Date(Date.now()+3600000).toISOString());
      hub.sql.exec('DROP TABLE messages');
      hub.sql.exec(SCHEMA_SQL.slice(SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS messages ('),SCHEMA_SQL.indexOf('CREATE TABLE IF NOT EXISTS claims (')));
      hub.sql.exec(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES ('foreign-old-history','admin','late-history','note','legacy-thread','foreign private','legacy-key',?,?)`,Date.parse('2021-01-01'),Date.now()+3600000);
      new Hub(state,bindings);
      expect(hub.sql.exec('SELECT id,registry_actor FROM credentials ORDER BY id').toArray().map((x:any)=>x.registry_actor).sort()).toEqual(['','','','proven-legacy']);
      const before=hub.sql.exec('SELECT * FROM credentials ORDER BY id').toArray();
      new Hub(state,bindings);
      expect(hub.sql.exec('SELECT * FROM credentials ORDER BY id').toArray()).toEqual(before);
    });
    expect((await req('/api/conversations','GET',undefined,tokens[0])).status).toBe(200);
    for(const token of tokens.slice(1))expect((await req('/api/conversations','GET',undefined,token)).status).toBe(403);
    const current=await cred('historyProject:late-history');
    expect((await req('/api/bus/messages/foreign-old-history','GET',undefined,current)).status).toBe(403);
    expect((await req('/api/bus/messages/foreign-old-history/ack','POST',{},current)).status).toBe(403);
    expect(JSON.stringify(await (await req('/api/bus/inbox?actor=late-history','GET',undefined,current)).json())).not.toContain('foreign-old-history');
    expect(JSON.stringify(await (await req('/api/bus/messages','GET',undefined,current)).json())).not.toContain('foreign-old-history');
    const adminRead=await req('/api/bus/messages/foreign-old-history','GET',undefined,c);expect(adminRead.status).toBe(200);
    expect(await adminRead.json()).toMatchObject({to_actor:'late-history',body:'foreign private',registry_unbound_to:1});
    await req('/api/agents/historyProject:late-history','DELETE',undefined,c);await register(c,'late-history');const projectless=await cred('late-history');
    expect((await req('/api/bus/messages/foreign-old-history','GET',undefined,projectless)).status).toBe(403);
  });

  it('R2/R4 routing issuer uses a separate scoped principal without ordinary-agent, host, bus or edit authority',async()=>{
    const c=await admin();
    const issued=await req('/api/fleet/routing/clients/browser','POST',{},c);expect(issued.status).toBe(201);
    const token=(await issued.json() as any).token;
    expect((await rows('SELECT kind,scopes,registry_actor FROM credentials WHERE token_hash=?',await sha256(token)))[0]).toEqual({kind:'routing-client',scopes:'["routing:submit"]',registry_actor:null});
    const input={workspace:'tasks',prompt:'Local read fixture',approved_action:'Read fixture only',needs:[],idempotency_key:'routing-read',access:{mode:'read'}};
    expect((await req('/api/fleet/tasks','POST',input,token)).status).toBe(201);
    for(const path of ['/api/bus/messages','/api/bus/inbox?actor=machine:node1','/api/conversations','/api/agents'])expect((await req(path,'GET',undefined,token)).status).toBe(403);
    expect((await req('/api/fleet/launches','POST',{...input,idempotency_key:'routing-launch'},token)).status).toBe(403);
    expect((await req('/api/fleet/tasks','POST',{...input,idempotency_key:'routing-edit',access:{mode:'edit',claim_id:'missing',scopes:['src']}},token)).status).toBeGreaterThanOrEqual(400);
    const ordinary=await cred('routing-client:browser','agent',['routing:submit']);
    expect((await req('/api/fleet/tasks','POST',{...input,idempotency_key:'impersonated'},ordinary)).status).toBe(403);
    await rows('UPDATE credentials SET scopes=? WHERE token_hash=?','["routing:submit","bus:read","host","fleet:launch"]',await sha256(token));
    expect((await req('/api/bus/messages','GET',undefined,token)).status).toBe(403);
    expect((await req('/api/fleet/tasks','POST',{...input,idempotency_key:'overscoped'},token)).status).toBe(403);
  });

  it('R5 invalid registry authorization is 403 while revoked, expired and invalid credentials stay 401',async()=>{
    const c=await admin();await register(c,'status-worker','statusProject');
    const invalid=await cred('missing-status-worker'),revoked=await cred('statusProject:status-worker'),expired=await cred('statusProject:status-worker');
    await rows('UPDATE credentials SET revoked_at=? WHERE id=?',new Date().toISOString(),revoked);
    await rows("UPDATE credentials SET expires_at='2020-01-01' WHERE id=?",expired);
    expect((await req('/api/conversations','GET',undefined,invalid)).status).toBe(403);
    for(const token of [revoked,expired,'hubt_never_issued'])expect((await req('/api/conversations','GET',undefined,token)).status).toBe(401);
  });

  it('R5 root replies canonicalize recipient aliases while wrong-recipient and stale-epoch fences remain',async()=>{
    const c=await admin();await register(c,'solo-worker','pA');await register(c,'other','pA');
    const token=await cred('job-1','job',['bus:send','lease:root']);const hash=await sha256(token);
    await rows(`INSERT INTO machines(id,name,hostname,registered_at) VALUES ('m1','local','local',?)`,new Date().toISOString());
    await rows(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,started_at,deadline_at,actor,holder_host_id,native_session,claim_token_hash)
      VALUES ('job-1','root-run','slot-1','{}','running',?,?,?,'system','m1','sess-1',?)`,new Date().toISOString(),new Date().toISOString(),new Date(Date.now()+600000).toISOString(),hash);
    await rows(`INSERT OR REPLACE INTO leases(name,epoch,holder_job_id,holder_host_id,holder_machine_id,acquired_at,expires_at) VALUES ('root',1,'job-1','m1','m1',?,?)`,new Date().toISOString(),new Date(Date.now()+600000).toISOString());
    await rows(`INSERT INTO messages(id,from_actor,to_actor,kind,thread_id,body,idempotency_key,created_at,expires_at) VALUES ('root-parent','solo-worker','root','note','root-parent','local','parent-key',?,?)`,Date.now(),Date.now()+600000);
    expect((await rows("SELECT from_actor,to_actor FROM messages WHERE id='root-parent'"))[0]).toEqual({from_actor:'pA:solo-worker',to_actor:'root'});
    const action=(to:string,epoch=1)=>req('/api/root/actions','POST',{job_id:'job-1',epoch,session_id:'sess-1',action:{type:'bus_dispatch',name:'reply',idempotency_key:crypto.randomUUID(),payload:{to_actor:to,kind:'reply',body:'local reply',reply_to:'root-parent'}}},token);
    expect((await action('other')).status).toBe(400);
    expect((await action('solo-worker',0)).status).toBe(409);
    const ok=await action('solo-worker');expect(ok.status).toBe(200);expect((await ok.json() as any).success).toBe(true);
    const protectedRows=await rows("SELECT from_actor,to_actor,lease_epoch FROM messages WHERE from_actor='role:root'");
    expect(protectedRows).toHaveLength(1);expect(protectedRows[0]).toMatchObject({to_actor:'pA:solo-worker',lease_epoch:1});
  });
});
