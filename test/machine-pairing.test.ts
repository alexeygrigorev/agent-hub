import { describe, it, expect, beforeEach } from 'vitest';
import { SELF, env, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';
import { Hub } from '../src/conversation-hub';
const csrf='local-pairing-csrf';
const scopes=['bus:read','bus:send','health:report'];
const stub=()=> (env as any).HUB.get((env as any).HUB.idFromName('hub'));
async function sql(q:string,...args:any[]) { let rows:any[]=[];await runInDurableObject(stub(),(h:Hub)=>{rows=h.sql.exec(q,...args).toArray();});return rows; }
async function human(isAdmin=true) {return (await createSessionCookie(env as any,{sub:'pairing-human',email:'admin@fake.local',isAdmin,csrfToken:csrf,exp:Math.floor(Date.now()/1000)+3600})).split(';')[0];}
async function req(path:string,method='GET',body?:any,auth?:string,csrfHeader:string|null=csrf) {
 const headers:Record<string,string>={};if(auth?.startsWith('hubt_'))headers.Authorization='Bearer '+auth;else if(auth)headers.Cookie=auth;
 if(csrfHeader)headers['X-CSRF-Token']=csrfHeader;
 if(body!==undefined)headers['Content-Type']='application/json';
 return SELF.fetch('https://hub.test'+path,{method,headers,redirect:'manual',body:body===undefined?undefined:JSON.stringify(body)});
}
async function machine(id='pair-node') {await sql("INSERT INTO machines(id,name,hostname,registered_at,credential_id) VALUES (?,?,'local',?,'host-pointer')",id,'Local fixture '+id,new Date().toISOString());}
async function code(actor='pair:worker',extra:any={}) {return req('/api/device/code','POST',{client_kind:'agent',name:actor,actor,requested_scopes:scopes,...extra});}
async function challenge(actor='pair:worker',extra:any={}) {const r=await code(actor,extra);expect(r.status).toBe(200);return await r.json() as any;}
const approve=(p:any,body:any,auth:string,csrfHeader:string|null=csrf)=> req('/api/device/approve/'+p.user_code,'POST',body,auth,csrfHeader);
const poll=(p:any)=>req('/api/device/token','POST',{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:p.device_code});
const refresh=(t:any)=>req('/api/token/refresh','POST',{grant_type:'refresh_token',refresh_token:t.refresh_token});
async function pair(actor='pair:worker',body:any={machine_id:'pair-node'}) {const c=await human(),p=await challenge(actor);expect((await approve(p,body,c)).status).toBe(200);const r=await poll(p);expect(r.status).toBe(200);return await r.json() as any;}
const credential=async(t:any)=>(await sql('SELECT * FROM credentials WHERE token_hash = ?',await sha256(t.access_token)))[0];
const self=(t:any)=>req('/api/conversations/participants','GET',undefined,t.access_token);

describe('public machine-bound agent enrollment through real SELF Worker and bound Hub',()=>{
 beforeEach(async()=>{ for(const table of ['device_pairings','credentials','agents','machines','jobs'])await sql('DELETE FROM '+table); });
 it('issues ordinary-agent identity, exact machine/scopes and immutable F7 actor; preserves host pointer on poll/refresh',async()=>{
  await machine();const p=await challenge();expect((await poll(p)).status).toBe(400);
  const c=await human();expect((await approve(p,{machine_id:'pair-node',expected_actor:'pair:worker'},c)).status).toBe(200);
  const t=await (await poll(p)).json() as any;const row=await credential(t);
  expect(t.machine_id).toBe('pair-node');
  expect(row).toMatchObject({kind:'agent',machine_id:'pair-node',registry_actor:'pair:worker',actor:'pair:worker'});expect(JSON.parse(row.scopes)).toEqual(scopes);
  const participants=await (await self(t)).json() as any;expect(participants.self.actor).toBe('pair:worker');
  expect(participants.participants.find((a:any)=>a.actor==='pair:worker')).toMatchObject({machine_id:'pair-node',native_binding:{project:'pair',tag:'worker',machine_id:'pair-node'}});
  const r=await refresh(t);expect(r.status).toBe(200);const next=await r.json() as any;
  expect(next.machine_id).toBe('pair-node');
  expect(await credential(next)).toMatchObject({kind:'agent',machine_id:'pair-node',registry_actor:'pair:worker',scopes:row.scopes,expires_at:row.expires_at});
  expect((await self(t)).status).toBe(401);expect((await self(next)).status).toBe(200);
  expect((await sql("SELECT credential_id FROM machines WHERE id='pair-node'"))[0].credential_id).toBe('host-pointer');
  expect((await poll(p)).status).toBe(400);expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(400);
 });
 it('preserves machine-less qualified/projectless pairing and inherits trusted existing machine assignment',async()=>{
  for(const actor of ['pair:unbound','unbound']) {const t=await pair(actor,{});expect(t).toHaveProperty('machine_id',null);expect((await credential(t)).machine_id).toBeNull();expect((await self(t)).status).toBe(200);const r=await refresh(t);expect(r.status).toBe(200);expect(await r.json()).toHaveProperty('machine_id',null);}
  await machine();const c=await human();expect((await req('/api/agents','POST',{tag:'registered',project:'pair',machine_id:'pair-node'},c)).status).toBe(201);
  const t=await pair('pair:registered',{});expect((await credential(t)).machine_id).toBe('pair-node');
  const p=await challenge('pair:registered');const html=await (await req('/device?code='+p.user_code,'GET',undefined,c)).text();
  expect(html).toContain('Current registered machine: pair-node');expect(html).toContain('value="pair-node" selected');
 });
 // Replay the values actually rendered by GET, with a real public registry mutation in between.
 for(const change of ['register','replace-project','replace-projectless','delete-project'])it('rejects changed displayed canonical identity after '+change,async()=>{
  await machine();const c=await human(),tag='snapshot';
  const register=(project:string|null)=>req('/api/agents','POST',{tag,...(project?{project}:{}),...(project?{machine_id:'pair-node'}:{})},c);
  if(change==='replace-project'||change==='delete-project')expect((await register('before')).status).toBe(201);
  if(change==='replace-projectless')expect((await register(null)).status).toBe(201);
  const p=await challenge(tag),html=await (await req('/device?code='+p.user_code,'GET',undefined,c)).text();
  const expected=change==='replace-project'||change==='delete-project'?'before:'+tag:tag;
  expect(html).toContain('data-actor="'+expected+'"');
  expect(html).toContain('Project: '+(expected.includes(':')?'before':'(projectless)')+' / Tag: '+tag);
  if(change==='replace-project'||change==='delete-project')expect((await req('/api/agents/before:'+tag,'DELETE',undefined,c)).status).toBe(200);
  if(change==='replace-projectless')expect((await req('/api/agents/'+tag,'DELETE',undefined,c)).status).toBe(200);
  if(change!=='delete-project')expect((await register('after')).status).toBe(201);
  const r=await approve(p,{expected_actor:expected,machine_id:expected.includes(':')?'pair-node':null},c);
  expect(r.status).toBe(400);expect(await r.json()).toEqual({error:'invalid_approval'});
  expect((await sql('SELECT status,actor,machine_id FROM device_pairings WHERE user_code=?',p.user_code))[0]).toEqual({status:'pending',actor:tag,machine_id:null});
  expect(await sql('SELECT * FROM credentials')).toHaveLength(0);
 });
 it('accepts the canonical actor displayed for an unchanged bare request',async()=>{
  await machine();const c=await human();expect((await req('/api/agents','POST',{project:'pair',tag:'canonical',machine_id:'pair-node'},c)).status).toBe(201);
  const p=await challenge('canonical'),html=await (await req('/device?code='+p.user_code,'GET',undefined,c)).text();
  expect(html).toContain('data-actor="pair:canonical"');
  expect((await approve(p,{expected_actor:'pair:canonical',machine_id:'pair-node'},c)).status).toBe(200);
  const t=await (await poll(p)).json() as any;expect(t.machine_id).toBe('pair-node');expect((await credential(t)).registry_actor).toBe('pair:canonical');
 });
 for(const existing of [false,true,'assignment'])it('enforces displayed explicit NULL after concurrent machine assignment, existing='+existing,async()=>{
  await machine();const c=await human();
  if(existing)expect((await req('/api/agents','POST',{project:'pair',tag:'choice'},c)).status).toBe(201);
  const p=await challenge('pair:choice'),html=await (await req('/device?code='+p.user_code,'GET',undefined,c)).text();
  expect(html).toContain('Machine-less (native bridge unavailable)');expect(html).toContain('data-kind="agent" data-machine=""');
  if(existing==='assignment') {const concurrent=await challenge('pair:choice');expect((await approve(concurrent,{machine_id:'pair-node'},c)).status).toBe(200);}
  else {
   if(existing)expect((await req('/api/agents/pair:choice','DELETE',undefined,c)).status).toBe(200);
   expect((await req('/api/agents','POST',{project:'pair',tag:'choice',machine_id:'pair-node'},c)).status).toBe(201);
  }
  const r=await approve(p,{expected_actor:'pair:choice',machine_id:null},c);expect(r.status).toBe(409);expect(await r.json()).toEqual({error:'agent_machine_conflict'});
  expect((await sql('SELECT status FROM device_pairings WHERE user_code=?',p.user_code))[0].status).toBe('pending');
  expect((await sql("SELECT machine_id FROM agents WHERE project='pair' AND tag='choice'"))[0].machine_id).toBe('pair-node');
  expect(await sql('SELECT * FROM credentials')).toHaveLength(0);
 });
 it('allows explicit NULL ordinary agents and rejects host NULL without side effects',async()=>{
  const t=await pair('pair:null-choice',{expected_actor:'pair:null-choice',machine_id:null});expect(t).toHaveProperty('machine_id',null);expect((await self(t)).status).toBe(200);
  const r=await refresh(t);expect(r.status).toBe(200);expect(await r.json()).toHaveProperty('machine_id',null);
  const p=await challenge('host-null',{client_kind:'host',requested_scopes:['host']});
  expect((await approve(p,{machine_id:null},await human())).status).toBe(400);expect(await sql('SELECT * FROM machines')).toHaveLength(0);
  expect((await sql('SELECT status FROM device_pairings WHERE user_code=?',p.user_code))[0].status).toBe('pending');
 });
 it('retains the displayed machine after same-actor delete/re-register, refusing a new assignment',async()=>{
  await machine();await machine('other-node');const c=await human();
  expect((await req('/api/agents','POST',{project:'pair',tag:'swap',machine_id:'pair-node'},c)).status).toBe(201);
  const p=await challenge('pair:swap'),html=await (await req('/device?code='+p.user_code,'GET',undefined,c)).text();
  expect(html).toContain('value="pair-node" selected');expect(html).toContain('data-kind="agent" data-machine="pair-node"');
  expect((await req('/api/agents/pair:swap','DELETE',undefined,c)).status).toBe(200);
  expect((await req('/api/agents','POST',{project:'pair',tag:'swap',machine_id:'other-node'},c)).status).toBe(201);
  expect((await approve(p,{expected_actor:'pair:swap',machine_id:'pair-node'},c)).status).toBe(409);
  expect((await sql('SELECT status FROM device_pairings WHERE user_code=?',p.user_code))[0].status).toBe('pending');
 });
 it('ignores caller machine assertions at token polling and refresh',async()=>{
  await machine();const c=await human(),p=await challenge('pair:metadata');expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);
  const r=await req('/api/device/token','POST',{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:p.device_code,machine_id:'forged'});
  expect(r.status).toBe(200);const t=await r.json() as any;expect(t.machine_id).toBe('pair-node');
  const rotated=await req('/api/token/refresh','POST',{grant_type:'refresh_token',refresh_token:t.refresh_token,machine_id:'forged'});
  expect(rotated.status).toBe(200);const next=await rotated.json() as any;expect(next.machine_id).toBe('pair-node');expect((await credential(next)).machine_id).toBe('pair-node');
 });
 it('does not promote old machine-less credentials when an admin binds their registration',async()=>{
  const old=await pair('pair:upgrade',{});expect((await self(old)).status).toBe(200);
  await machine();const next=await pair('pair:upgrade');expect((await self(next)).status).toBe(200);
  expect((await self(old)).status).toBe(401);expect((await refresh(old)).status).toBe(400);
 });
 it('renders official dakit selector, explicit qualified identity, escaped machines and CSRF',async()=>{
  await machine('node-ui');await sql('UPDATE machines SET name=? WHERE id=?','<script>bad</script>','node-ui');
  const p=await challenge();const r=await req('/device?code='+p.user_code,'GET',undefined,await human());const html=await r.text();
  expect(r.status).toBe(200);for(const text of ['dakit/dakit.css','class="dk-select"','Project: pair / Tag: worker','value="node-ui"','&lt;script&gt;bad&lt;/script&gt;','Machine-less (native bridge unavailable)','/auth.js'])expect(html).toContain(text);
  expect(html).not.toContain('<style'); expect(html).not.toContain(csrf); expect(html).not.toContain(p.device_code);
 });
 it('rejects unauthenticated, non-admin human, agent and host approval plus missing/wrong CSRF',async()=>{
  await machine();const t=await pair();const host=await challenge('host-fixture',{client_kind:'host',requested_scopes:['host']});
  expect((await approve(host,{machine_id:'pair-node'},await human())).status).toBe(200);const ht=await (await poll(host)).json() as any;
  const p=await challenge('pair:other');
  for(const auth of [undefined,t.access_token,ht.access_token])expect((await req('/api/device/approve/'+p.user_code,'POST',{machine_id:'pair-node'},auth)).status).toBe(401);
  expect((await approve(p,{},await human(false))).status).toBe(401);
  for(const x of [null,'wrong'])expect((await approve(p,{},await human(),x)).status).toBe(403);
  expect((await req('/device?code='+p.user_code,'GET',undefined,await human(false))).status).toBe(302);
 });
 it('machine binding confers no host/role/foreign inbox, jobs, root, registry or fleet authority',async()=>{
  await machine();const t=await pair();
  for(const actor of ['machine:pair-node','role:ops','foreign:worker'])expect((await req('/api/bus/inbox?actor='+actor,'GET',undefined,t.access_token)).status).toBe(403);
  for(const [path,method,body] of [['/api/host/poll','POST',{type:'heartbeat'}],['/api/agents','POST',{tag:'forged',machine_id:'pair-node'}],['/api/jobs','GET',undefined],['/api/root/handoff','GET',undefined],['/api/fleet','GET',undefined],['/api/fleet/launch','POST',{machine:'pair-node'}]] as any[])expect((await req(path,method,body,t.access_token)).status).toBeGreaterThanOrEqual(400);
  await sql("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_machine_id) VALUES ('foreign-job','root','foreign-job','{}','offered',?,'2099-01-01T00:00:00Z','admin','pair-node')",new Date().toISOString());
  for(const path of ['/api/jobs/foreign-job','/api/jobs/foreign-job/status'])expect((await req(path,'GET',undefined,t.access_token)).status).toBe(403);
 });

 it('retains all authorization fences with explicitly approved fleet:launch and keeps own heartbeat authority',async()=>{
  await machine();const c=await human(),p=await challenge('pair:fleet',{requested_scopes:[...scopes,'fleet:launch']});
  expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);const t=await (await poll(p)).json() as any;
  await sql("INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_machine_id,holder_machine_id,claim_token_hash) VALUES ('host-job','root','host-job','{}','offered',?,'2099-01-01T00:00:00Z','admin','pair-node','pair-node','unrelated-job-token-hash')",new Date().toISOString());
  expect((await req('/api/agents','POST',{tag:'agent',project:'foreign',role:'ops',machine_id:'pair-node'},c)).status).toBe(201);
  const launch='11111111-1111-4111-8111-111111111111';
  const negatives:any[]=[
   ['/api/jobs/offers','GET'],['/api/host/offers','GET'],['/api/jobs/host-job','GET'],['/api/jobs/host-job/status','GET'],['/api/root/status','GET'],
   ['/api/fleet/offers','GET'],['/api/fleet/claim-policy','GET'],['/api/fleet/accounts','PUT',{accounts:[],routes:[]}],
   ['/api/fleet/sessions','PUT',{sessions:[]}],['/api/fleet/subscriptions','PUT',{}],
   ['/api/fleet/launches/'+launch+'/claim','POST',{}],['/api/fleet/launches/'+launch+'/result','POST',{}],
   ['/api/routing/capabilities','PUT',{}],['/api/routing/clients/forged','POST',{}],
   ['/api/machines/available','GET'],['/api/machines/register','POST',{}],['/api/machines/pair-node/drain','POST',{}],
   ['/api/host/poll','POST',{type:'heartbeat'}],['/api/host/connect','GET'],['/api/liveness','GET'],['/api/checks','PUT',{}],
   ['/api/bus/inbox?actor=machine:pair-node','GET'],['/api/bus/inbox?actor=role:ops','GET'],
  ];
  for(const action of ['claim','decline','start','renew','finish'])negatives.push(['/api/jobs/host-job/'+action,'POST',{epoch:0,status:'completed',evidence_digest:'local',native_session:'local',launcher_task_id:'local'}]);
  for(const [path,method,body] of negatives)expect((await req(path,method,body,t.access_token)).status,path).toBeGreaterThanOrEqual(400);
  expect((await req('/api/heartbeat','POST',{status:'working',summary:'own local heartbeat'},t.access_token)).status).toBe(200);
  expect((await req('/api/heartbeat','POST',{status:'working',actor:'foreign:agent',machine_id:'foreign'},t.access_token)).status).toBe(400);
  expect((await sql("SELECT actor,machine_id FROM agent_heartbeats WHERE actor='pair:fleet'"))[0]).toEqual({actor:'pair:fleet',machine_id:'pair-node'});
  expect((await sql("SELECT status FROM jobs WHERE id='host-job'"))[0].status).toBe('offered');
  expect((await sql("SELECT credential_id FROM machines WHERE id='pair-node'"))[0].credential_id).toBe('host-pointer');
 });
 it('only admin approval can name a registered machine; rejects unknown, empty, projectless, foreign reassignment and client assertions',async()=>{
  await machine();await machine('other-node');const c=await human();
  expect((await code('pair:asserted',{machine_id:'pair-node'})).status).toBe(400);
  expect((await code('pair:duplicates',{requested_scopes:['bus:read','bus:read']})).status).toBe(400);
  for(const scope of ['host','lease:root','jobs:create','admin'])expect((await code('pair:privilege',{requested_scopes:[scope]})).status).toBe(400);
  for(const [actor,id] of [['pair:missing','missing'],['pair:empty',''],['bare','pair-node']]) {const p=await challenge(actor);expect((await approve(p,{machine_id:id},c)).status).toBe(400);expect((await sql('SELECT status FROM device_pairings WHERE user_code=?',p.user_code))[0].status).toBe('pending');}
  await pair();const p=await challenge();expect((await approve(p,{machine_id:'other-node'},c)).status).toBe(409);
  expect((await sql("SELECT machine_id FROM agents WHERE project='pair' AND tag='worker'"))[0].machine_id).toBe('pair-node');
 });
 it('rejects altered approval identity/scopes, duplicates and reserved namespaces; pending rows cannot smuggle host scopes',async()=>{
  await machine();const c=await human(),p=await challenge();
  for(const b of [{actor:'foreign:worker'},{client_kind:'host'},{expected_actor:'foreign:worker'},{granted_scopes:['bus:read','bus:read']},{granted_scopes:['host']},{granted_scopes:['lease:root']},{granted_scopes:['jobs:create']}])expect((await approve(p,{machine_id:'pair-node',...b},c)).status).toBe(400);
  for(const actor of ['root','p:root','machine:worker','role:ops','admin:worker','routing-client:worker'])expect((await code(actor)).status).toBe(400);
  await sql('UPDATE device_pairings SET actor=? WHERE user_code=?','foreign:worker',p.user_code);
  expect((await approve(p,{machine_id:'pair-node',expected_actor:'pair:worker'},c)).status).toBe(400);
  await sql('UPDATE device_pairings SET requested_scopes=? WHERE user_code=?','["host"]',p.user_code);
  expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(400);
 });
 it('expires stale and denied codes and refuses machine removal between approval and token issuance',async()=>{
  await machine();const c=await human();
  const expired=await challenge();await sql('UPDATE device_pairings SET expires_at=? WHERE user_code=?','2000-01-01T00:00:00Z',expired.user_code);
  expect((await approve(expired,{},c)).status).toBe(400);expect((await poll(expired)).status).toBe(400);
  const denied=await challenge();expect((await req('/api/device/deny/'+denied.user_code,'POST',{},c)).status).toBe(200);expect((await poll(denied)).status).toBe(400);
  const p=await challenge();expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);await sql("DELETE FROM machines WHERE id='pair-node'");
  expect((await poll(p)).status).toBe(400);expect((await sql('SELECT * FROM credentials')).length).toBe(0);
 });
 it('rejects credential/registry machine disagreement on real conversation/bus/refresh routes',async()=>{
  await machine();await machine('other-node');const t=await pair();await sql('UPDATE credentials SET machine_id=? WHERE id=?','other-node',(await credential(t)).id);
  expect((await self(t)).status).toBe(403);expect((await req('/api/bus/inbox?actor=pair:worker','GET',undefined,t.access_token)).status).toBe(403);expect((await refresh(t)).status).toBe(400);
 });
 for(const mode of ['machine-delete','registry-delete','registry-move','expiry','revocation'])it('ends bound access and refresh after '+mode,async()=>{
  await machine();const t=await pair();const row=await credential(t);
  if(mode==='machine-delete') {await sql("DELETE FROM machines WHERE id='pair-node'");await machine();}
  if(mode==='registry-delete') {await req('/api/agents/pair:worker','DELETE',undefined,await human());await req('/api/agents','POST',{project:'pair',tag:'worker',machine_id:'pair-node'},await human());}
  if(mode==='registry-move') {await machine('other-node');await sql("UPDATE agents SET machine_id='other-node' WHERE project='pair' AND tag='worker'");await sql("UPDATE agents SET machine_id='pair-node' WHERE project='pair' AND tag='worker'");}
  if(mode==='expiry')await sql('UPDATE credentials SET expires_at=? WHERE id=?','2000-01-01T00:00:00Z',row.id);
  if(mode==='revocation')expect((await req('/api/token/revoke','POST',{id:row.id},await human())).status).toBe(200);
  expect((await self(t)).status).toBe(401);expect((await refresh(t)).status).toBe(400);
 });

 it('revokes an approved code when registration or machine ends before polling, even after recreation',async()=>{
  await machine();const c=await human();
  for(const mode of ['agent','machine']) {
   const actor='pair:pending-'+mode,p=await challenge(actor);expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);
   if(mode==='agent') {expect((await req('/api/agents/'+actor,'DELETE',undefined,c)).status).toBe(200);expect((await req('/api/agents','POST',{project:'pair',tag:'pending-agent',machine_id:'pair-node'},c)).status).toBe(201);}
   else {await sql("DELETE FROM machines WHERE id='pair-node'");await machine();}
   expect((await poll(p)).status).toBe(400);
  }
 });
 it('serializes concurrent conflicting approvals without contradictory machine bindings',async()=>{
  await machine();await machine('other-node');const c=await human(),p=await challenge(),other=await challenge();
  const responses=await Promise.all([approve(p,{machine_id:'pair-node'},c),approve(other,{machine_id:'other-node'},c)]);
  expect(responses.filter(r=>r.status===200)).toHaveLength(1);const winner=responses[0].status===200?p:other;const t=await (await poll(winner)).json() as any;
  expect((await credential(t)).machine_id).toBe((await sql("SELECT machine_id FROM agents WHERE project='pair' AND tag='worker'"))[0].machine_id);
 });
 it('retains scope reduction through refresh and rejects ungranted conversation mutations',async()=>{
  await machine();const t=await pair('pair:limited',{machine_id:'pair-node',granted_scopes:['bus:read']});
  await sql('UPDATE credentials SET scopes=? WHERE id=?','["bus:read"]',(await credential(t)).id);
  const next=await (await refresh(t)).json() as any;expect((await credential(next)).scopes).toBe('["bus:read"]');
  expect((await req('/api/conversations','POST',{kind:'group',title:'deny',participants:[]},next.access_token)).status).toBe(403);
 });
 for(const phase of ['approval','issuance','refresh'])it('rolls back '+phase+' failure and retry cannot create contradictory bindings',async()=>{
  await machine();const c=await human(),p=await challenge();let t:any;
  if(phase!=='approval')expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);
  if(phase==='refresh')t=await (await poll(p)).json();
  const table=phase==='approval'?'device_pairings':'credentials';
  const operation=phase==='approval'?"UPDATE OF status":"INSERT";
  await sql(`CREATE TRIGGER pairing_injected_failure BEFORE ${operation} ON ${table} BEGIN SELECT RAISE(ABORT,'pairing_injected_failure'); END`);
  const result=phase==='approval'?await approve(p,{machine_id:'pair-node'},c):phase==='issuance'?await poll(p):await refresh(t);
  expect(result.status).toBe(500);await sql('DROP TRIGGER pairing_injected_failure');
  if(phase==='approval') {expect((await sql("SELECT * FROM agents WHERE project='pair'")).length).toBe(0);expect((await approve(p,{machine_id:'pair-node'},c)).status).toBe(200);}
  if(phase==='refresh') {expect((await credential(t)).revoked_at).toBeNull();expect((await refresh(t)).status).toBe(200);}
  else {expect((await poll(p)).status).toBe(200);expect((await poll(p)).status).toBe(400);}
  expect((await sql("SELECT COUNT(*) n FROM credentials WHERE kind='agent' AND revoked_at IS NULL"))[0].n).toBe(1);
 });
});
