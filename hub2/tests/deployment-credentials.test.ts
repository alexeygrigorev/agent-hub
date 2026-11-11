import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {ControlDO} from '../src/control-do';
import worker from '../src/worker';
import {sha256Hex} from '../src/crypto';
import {actorFrom} from '../src/http';
import {DEPLOYMENT_POLICY_SCOPES} from '../src/types';
import {installCompatGlobals} from '../node/compat';
installCompatGlobals();

const candidate={id:'sol',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true};
const adminToken='synthetic-deployment-admin-'+'a'.repeat(40);
const browserToken='synthetic-deployment-browser-'+'d'.repeat(40);
const adminRefresh='synthetic-deployment-refresh-'+'b'.repeat(40);
const csrf='synthetic-deployment-csrf-'+'c'.repeat(40);
const email='deployment-admin@example.invalid';
const protocol='model-policy-recovery-v1';
const parsed=async(response:Response)=>await response.json() as any;

async function fixture(){
  const database=new DatabaseSync(':memory:');
  const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(query:string,...args:any[]){
    const statement=database.prepare(query);
    if(statement.columns().length){const rows=statement.all(...args);return{toArray:()=>rows,rowsRead:rows.length,rowsWritten:0};}
    const result=statement.run(...args);return{toArray:()=>[],rowsRead:Number(result.changes),rowsWritten:Number(result.changes)};
  }},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
  let otherCalls=0;
  const env:any={HUB_ADMINS:email};
  const control=new ControlDO(state,env);
  env.CONTROL={idFromName:(name:string)=>name,get:()=>({fetch:(input:Request|string,init?:RequestInit)=>control.fetch(new Request(input,init))})};
  for(const binding of ['PROJECTS','MACHINES','ACCOUNTS','MAILBOXES'])env[binding]={idFromName:(name:string)=>name,get:()=>({fetch:()=>{otherCalls++;throw new Error('unexpected service dispatch');}})};
  await control.fetch(new Request('https://control.test/api/model-policy'));
  const family=crypto.randomUUID(),generation=crypto.randomUUID();
  database.prepare('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,refresh_hash,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)')
    .run('credential:admin','operator','human:admin',generation,await sha256Hex(adminToken),'["operator:admin"]',family,'admin-cli',Date.now(),Date.now()+3600000,email,await sha256Hex(adminRefresh),csrf);
  database.prepare('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at,expires_at,principal_email,refresh_hash,csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)')
    .run('credential:browser','operator','human:admin',crypto.randomUUID(),await sha256Hex(browserToken),'["operator:admin"]',crypto.randomUUID(),'browser',Date.now(),Date.now()+43200000,email,null,csrf);
  async function call(path:string,method='GET',body?:unknown,token:string|null=adminToken,extra:Record<string,string>={}){
    const headers=new Headers({'Content-Type':'application/json',...extra});
    if(token!==null)headers.set('Authorization','Bearer '+token);
    return worker.fetch(new Request('https://hub.test'+path,{method,headers,...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
  }
  async function issue(request_id=crypto.randomUUID(),label='vm-policy-deploy'){
    const response=await call('/api/deployment-credentials','POST',{request_id,label});
    assert.equal(response.status,201,JSON.stringify(await response.clone().json()));
    return await parsed(response);
  }
  return {database,state,env,control,reopen:()=>new ControlDO(state,env),call,issue,otherCalls:()=>otherCalls};
}

test('admin bearer issues one hash-only fixed-purpose service credential through public API',async t=>{
  const f=await fixture();t.after(()=>f.database.close());
  const requestId=crypto.randomUUID(),issued=await f.issue(requestId);
  assert.equal(issued.credential.id,'deployment:'+requestId);
  assert.equal(issued.credential.request_id,requestId);
  assert.equal(issued.credential.kind,'service');assert.equal(issued.credential.purpose,'model-policy');
  assert.equal(issued.credential.expires_at,null);assert.equal(issued.token_type,'Bearer');
  assert.deepEqual(issued.credential.scopes,[...DEPLOYMENT_POLICY_SCOPES]);
  assert.match(issued.access_token,/^[0-9a-f]{64}$/);
  const row=f.database.prepare('SELECT * FROM credentials WHERE actor_id=?').get(issued.credential.id) as any;
  assert.equal(row.kind,'service');assert.equal(row.resource_id,'model-policy');
  assert.equal(row.token_hash,await sha256Hex(issued.access_token));
  assert.equal(row.refresh_hash,null);assert.equal(row.csrf_token,null);assert.equal(row.expires_at,null);
  assert.ok(!JSON.stringify(row).includes(issued.access_token));
  const parent=f.database.prepare('SELECT family_id,generation FROM credentials WHERE actor_id=?').get('credential:admin') as any;
  assert.notEqual(row.family_id,parent.family_id);assert.notEqual(row.generation,parent.generation);
  const listing=await parsed(await f.call('/api/deployment-credentials'));
  assert.equal(listing.items.length,1);assert.equal(listing.next,null);
  assert.ok(!JSON.stringify(listing).includes(issued.access_token));assert.ok(!JSON.stringify(listing).includes(row.token_hash));
});

test('service supports normal policy CAS and recovery without acquiring other API authority',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const issued=await f.issue(),token=issued.access_token;
  assert.equal((await f.call('/api/model-policy','GET',undefined,token)).status,404);
  const validate={protocol,action:'validate_recovery',expected_version:0,candidates:[candidate]};
  assert.equal((await f.call('/api/model-policy','POST',validate,token)).status,200);
  const applied=await parsed(await f.call('/api/model-policy','POST',{...validate,action:'apply_recoverable'},token));
  assert.equal(applied.version,1);
  assert.equal((await f.call('/api/model-policy','PUT',{candidates:[candidate],expected_version:0},token)).status,409);
  assert.equal((await f.call('/api/model-policy','PUT',{candidates:[],restore_unconfigured:true,expected_version:1},token)).status,200);
  assert.deepEqual(await parsed(await f.call('/api/model-policy','GET',undefined,token)),{error:'model_policy_unconfigured',version:2});
  for(const [path,method] of [['/api/sessions','POST'],['/api/deployment-credentials','POST'],['/api/deployment-credentials','GET'],
      ['/api/deployment-credentials/'+issued.credential.id+'/revoke','POST'],['/api/account/credentials','GET'],['/api/auth/me','GET'],
      ['/api/connect','GET'],['/mcp','POST'],['/_routing-policy','GET']]){
    const response=await f.call(path,method,method==='POST'?{}:undefined,token);
    assert.ok([403,404].includes(response.status),path+': '+response.status);
  }
  assert.equal(f.otherCalls(),0);
  const refresh=await f.call('/api/token/refresh','POST',{refresh_token:token},token);
  assert.equal(refresh.status,401);
  assert.equal((await f.call('/api/model-policy','GET',undefined,token)).status,404);
});

test('service survives ordinary admin refresh and service revocation immediately denies reads and writes',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const issued=await f.issue();
  const refreshed=await f.call('/api/token/refresh','POST',{refresh_token:adminRefresh});
  assert.equal(refreshed.status,200);const current=await parsed(refreshed);
  assert.equal((await f.call('/api/model-policy','GET',undefined,issued.access_token)).status,404);
  assert.equal((await f.call('/api/deployment-credentials','GET')).status,401);
  const revoke='/api/deployment-credentials/'+issued.credential.id+'/revoke';
  assert.equal((await f.call(revoke,'POST',{},current.access_token)).status,200);
  assert.equal((await parsed(await f.call(revoke,'POST',{},current.access_token))).replay,true);
  assert.equal((await f.call('/api/model-policy','GET',undefined,issued.access_token)).status,401);
  assert.equal((await f.call('/api/model-policy','PUT',{candidates:[candidate],expected_version:0},issued.access_token)).status,401);
  assert.equal((await f.control.fetch(new Request('https://control.test/_authorize',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({actor_id:issued.credential.id,generation:(f.database.prepare('SELECT generation FROM credentials WHERE actor_id=?').get(issued.credential.id) as any).generation,scope:'model-policy:read'})}))).status,401);
});

test('lost issuance retries disclose no secret, preserve intent and permit deterministic cleanup',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const requestId=crypto.randomUUID(),issued=await f.issue(requestId);
  const retry=await f.call('/api/deployment-credentials','POST',{request_id:requestId,label:'vm-policy-deploy'});
  assert.equal(retry.status,409);assert.match(retry.headers.get('Cache-Control')||'',/no-store/);
  const result=await parsed(retry);assert.deepEqual(result,{error:'deployment_credential_already_issued',id:issued.credential.id});
  const conflict=await f.call('/api/deployment-credentials','POST',{request_id:requestId,label:'other-label'});
  assert.equal(conflict.status,409);assert.equal((await parsed(conflict)).error,'deployment_credential_request_conflict');
  await f.call('/api/deployment-credentials/'+issued.credential.id+'/revoke','POST',{});
  assert.equal((await f.call('/api/deployment-credentials','POST',{request_id:requestId,label:'vm-policy-deploy'})).status,409);
  const next=await f.issue();assert.notEqual(next.credential.id,issued.credential.id);
  assert.equal((await f.call('/api/model-policy','GET',undefined,issued.access_token)).status,401);
  assert.equal((await f.call('/api/model-policy','GET',undefined,next.access_token)).status,404);
});

test('concurrent identical issuance has one successful plaintext disclosure',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const request_id=crypto.randomUUID();
  const responses=await Promise.all([1,2].map(()=>f.call('/api/deployment-credentials','POST',{request_id,label:'vm-policy-deploy'})));
  assert.deepEqual(responses.map(response=>response.status).sort(),[201,409]);
  const bodies=await Promise.all(responses.map(parsed));assert.equal(bodies.filter(body=>body.access_token).length,1);
  assert.equal((f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n,1);
});

test('admin cookie issuance preserves CSRF and cannot be replaced by an actor header',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const body={request_id:crypto.randomUUID(),label:'cookie-deploy'};
  assert.equal((await f.call('/api/deployment-credentials','POST',body,null,{Cookie:'hub2_session='+browserToken})).status,401);
  assert.equal((await f.call('/api/deployment-credentials','POST',body,null,{Cookie:'hub2_session='+browserToken,'x-csrf-token':csrf,Origin:'https://evil.test'})).status,403);
  assert.equal((await f.call('/api/deployment-credentials','POST',body,null,{Cookie:'hub2_session='+browserToken,'x-csrf-token':csrf,Origin:'https://hub.test'})).status,201);
  const spoof={'x-hub2-actor':JSON.stringify({id:'human:admin',kind:'operator',scopes:['operator:admin']})};
  assert.equal((await f.call('/api/deployment-credentials','POST',{request_id:crypto.randomUUID(),label:'spoof'},null,spoof)).status,401);
  const service=await f.issue();
  assert.equal((await f.call('/api/deployment-credentials','POST',{request_id:crypto.randomUUID(),label:'spoof'},service.access_token,spoof)).status,403);
});

test('malformed issuance, extra scopes and alternate purpose never create credentials',async t=>{
  const f=await fixture();t.after(()=>f.database.close());
  for(const bad of [{request_id:'not-a-uuid',label:'vm'}, {request_id:crypto.randomUUID(),label:'x'.repeat(81)},
    {request_id:crypto.randomUUID(),label:'vm\nsecret'}, {request_id:crypto.randomUUID(),label:'vm',scopes:['operator:admin']},
    {request_id:crypto.randomUUID(),label:'vm',purpose:'sessions'}, {request_id:crypto.randomUUID(),label:'vm',principal_email:email}])
    assert.equal((await f.call('/api/deployment-credentials','POST',bad)).status,400);
  assert.equal((f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n,0);
});

test('invalid stored scope/purpose and removed owner allowlist fail through the ordinary verifier',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const issued=await f.issue();
  const original=f.database.prepare('SELECT * FROM credentials WHERE actor_id=?').get(issued.credential.id) as any;
  for(const [column,value] of [['scopes_json','["model-policy:read","model-policy:write","operator:admin"]'],['scopes_json','["*"]'],
      ['resource_id','synthetic-resource'],['refresh_hash','not-null'],['expires_at',Date.now()+3600000],['principal_email','outsider@example.invalid']]){
    f.database.prepare('UPDATE credentials SET '+column+'=? WHERE actor_id=?').run(value,issued.credential.id);
    // Reopen after raw fixture mutation, preserving real verifier semantics.
    const response=await f.reopen().fetch(new Request('https://control.test/_verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:issued.access_token})}));
    assert.equal(response.status,401,column);
    f.database.prepare('UPDATE credentials SET '+column+'=? WHERE actor_id=?').run(original[column],issued.credential.id);
  }
  f.env.HUB_ADMINS='different@example.invalid';
  assert.equal((await f.reopen().fetch(new Request('https://control.test/_verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:issued.access_token})}))).status,401);
});

test('service actor parser rejects malformed identity, additional authority and caller fields',()=>{
  const valid={id:'deployment:'+crypto.randomUUID(),kind:'service',resource_id:'model-policy',generation:crypto.randomUUID(),family_id:crypto.randomUUID(),client_id:'vm-policy',scopes:[...DEPLOYMENT_POLICY_SCOPES]};
  for(const patch of [{resource_id:'other'}, {scopes:['operator:admin']}, {scopes:[...valid.scopes,'*']}, {id:'human:admin'}, {issuer:'human:admin'}, {generation:'fake'}])
    assert.equal(actorFrom(new Request('https://control.test',{headers:{'x-hub2-actor':JSON.stringify({...valid,...patch})}})),undefined);
  assert.deepEqual(actorFrom(new Request('https://control.test',{headers:{'x-hub2-actor':JSON.stringify(valid)}})),valid);
});

test('service authority persists across Control reopen and human access-token expiry',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const issued=await f.issue();
  const realNow=Date.now;
  try{
    Date.now=()=>realNow()+366*86400000;
    const response=await f.reopen().fetch(new Request('https://control.test/_verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:issued.access_token})}));
    assert.equal(response.status,200);
    assert.deepEqual((await parsed(response)).actor.scopes,[...DEPLOYMENT_POLICY_SCOPES]);
    assert.equal((await f.call('/api/deployment-credentials','GET')).status,401,'expired human token cannot administer service credentials');
  }finally{Date.now=realNow;}
});

test('active capacity is bounded and revoked tombstones remain paged without restoring old IDs',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const issued=[];
  for(let i=0;i<32;i++)issued.push(await f.issue());
  assert.equal((await f.call('/api/deployment-credentials','POST',{request_id:crypto.randomUUID(),label:'over-capacity'})).status,409);
  await f.call('/api/deployment-credentials/'+issued[0].credential.id+'/revoke','POST',{});
  issued.push(await f.issue());
  const page=await parsed(await f.call('/api/deployment-credentials'));
  assert.equal(page.items.length,32);assert.ok(page.next);
  const last=await parsed(await f.call('/api/deployment-credentials?after='+encodeURIComponent(page.next)));
  assert.equal(last.items.length,1);assert.equal(last.next,null);
  assert.equal(new Set([...page.items,...last.items].map(row=>row.id)).size,33);
  assert.equal([...page.items,...last.items].filter(row=>row.revoked).length,1);
  assert.equal((await f.call('/api/deployment-credentials?after=invalid')).status,400);
  assert.equal((await f.call('/api/deployment-credentials?limit=100000')).status,400);
  const rows=f.database.prepare("SELECT * FROM credentials WHERE kind='service'").all();
  for(const credential of issued)assert.ok(!JSON.stringify(rows).includes(credential.access_token));
});

test('issuance failure after SQL insert rolls back the hash and allows the same request intent',async t=>{
  const f=await fixture();t.after(()=>f.database.close());const request_id=crypto.randomUUID();
  const execute=f.state.storage.sql.exec;
  f.state.storage.sql.exec=(query:string,...args:unknown[])=>{
    const result=execute(query,...args);
    if(query.startsWith('INSERT INTO credentials(')&&args[1]==='service')throw new Error('injected deployment insert failure');
    return result;
  };
  const failed=await f.call('/api/deployment-credentials','POST',{request_id,label:'vm-policy-deploy'});
  assert.equal(failed.status,500);
  assert.equal((f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n,0);
  f.state.storage.sql.exec=execute;
  const retried=await f.issue(request_id);
  assert.equal(retried.credential.id,'deployment:'+request_id);
  assert.equal((f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n,1);
});

test('admin revocation during issuance hashing prevents committing a new service credential',async t=>{
  const f=await fixture();t.after(()=>f.database.close());
  const digest=crypto.subtle.digest.bind(crypto.subtle);
  let entered!:()=>void,release!:()=>void,paused=false;
  const hashing=new Promise<void>(resolve=>{entered=resolve;});
  const resume=new Promise<void>(resolve=>{release=resolve;});
  (crypto.subtle as any).digest=async(algorithm:any,data:any)=>{
    if(!paused&&/^[0-9a-f]{64}$/.test(new TextDecoder().decode(data))){paused=true;entered();await resume;}
    return digest(algorithm,data);
  };
  let pending:Promise<Response>|undefined;
  try{
    pending=f.call('/api/deployment-credentials','POST',{request_id:crypto.randomUUID(),label:'revoked-during-issue'});
    await hashing;
    assert.equal((await f.call('/api/token/revoke','POST',{})).status,200);
    release();
    assert.equal((await pending).status,403);
    assert.equal((f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n,0);
  }finally{
    release();
    (crypto.subtle as any).digest=digest;
    if(pending)await pending;
  }
});

for(const action of ['issue','revoke'])test('admin revocation while '+action+' request body is pending prevents mutation',async t=>{
  const f=await fixture();t.after(()=>f.database.close());
  const existing=action==='revoke'?await f.issue():undefined;
  const verified=await parsed(await f.control.fetch(new Request('https://control.test/_verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:adminToken})})));
  let reading!:()=>void,controller!:ReadableStreamDefaultController<Uint8Array>;
  const requested=new Promise<void>(resolve=>{reading=resolve;});
  // A real Control namespace request with its previously verified actor. A
  // zero-size queue ensures pull proves bodyObject was reached after issuer().
  const stream=new ReadableStream<Uint8Array>({start(value){controller=value;},pull(){reading();}},{highWaterMark:0});
  const path=existing?'/api/deployment-credentials/'+existing.credential.id+'/revoke':'/api/deployment-credentials';
  const pending=f.control.fetch(new Request('https://control.test'+path,{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(verified.actor)},body:stream as any}));
  try{
    await requested;
    assert.equal((await f.call('/api/token/revoke','POST',{})).status,200);
    controller.enqueue(new TextEncoder().encode(JSON.stringify(existing?{}:{request_id:crypto.randomUUID(),label:'revoked-body'})));
    controller.close();
    const response=await pending;assert.equal(response.status,403);
    assert.deepEqual(await parsed(response),{error:'forbidden'});
    const count=(f.database.prepare("SELECT count(*) AS n FROM credentials WHERE kind='service'").get() as any).n;
    assert.equal(count,existing?1:0);
    if(existing){
      assert.equal((f.database.prepare('SELECT revoked_at FROM credentials WHERE actor_id=?').get(existing.credential.id) as any).revoked_at,null);
      assert.equal((await f.call('/api/model-policy','GET',undefined,existing.access_token)).status,404);
    }
  }finally{
    try{controller.error(new Error('fixture cleanup'));}catch{}
    await pending;
  }
});
