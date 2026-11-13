import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

registerHooks({resolve(specifier,context,next){
 if(specifier.startsWith('.')&&context.parentURL&&!specifier.endsWith('.ts')){
  const candidate=new URL(specifier+'.ts',context.parentURL);
  if(existsSync(fileURLToPath(candidate)))return next(candidate.href,context);
 }
 return next(specifier,context);
}});
const {MachineDO}=await import('../src/machine-do.ts');

const machineActor={id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
const operator={'x-hub2-actor':JSON.stringify({id:'operator',kind:'operator',scopes:['operator:admin']})};
const header={'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)};

function fixture(){
 const database=new DatabaseSync(':memory:'),events:Array<{path:string;body:any}>=[],sockets:any[]=[];
 let alarm:number|null=null;
 const ctx:any={id:{toString:()=> 'machine-h1'},storage:{sql:{exec(query:string,...args:unknown[]){
  const statement=database.prepare(query);let rows:unknown[]=[],rowsRead=0,rowsWritten=0;
  if(statement.columns().length){rows=statement.all(...args as any);rowsRead=rows.length;}
  else{const result=statement.run(...args as any);rowsWritten=Number(result.changes);}
  return{toArray:()=>rows,rowsRead,rowsWritten};
 }},async getAlarm(){return alarm;},async setAlarm(value:number){alarm=value;},async deleteAlarm(){alarm=null;},transactionSync<T>(fn:()=>T):T{database.exec('BEGIN');try{const result=fn();database.exec('COMMIT');return result;}catch(error){database.exec('ROLLBACK');throw error;}}},
  blockConcurrencyWhile(fn:()=>unknown){return Promise.resolve(fn());},getWebSockets(){return sockets;}};
 const env:any={
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({authorized:true})})},
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({})})},
  PROJECTS:{idFromName:(id:string)=>id,get:()=>({fetch:async(request:Request)=>{
   const path=new URL(request.url).pathname;events.push({path,body:await request.json()});return Response.json({ok:true});
  }})},
 };
 const machine=new MachineDO(ctx,env);
 const call=(path:string,method='GET',body?:unknown,headers:Record<string,string>={})=>machine.fetch(new Request('https://machine.test'+path,{method,headers:{...header,...headers},...(body===undefined?{}:{body:JSON.stringify(body)})}));
 return{database,machine,call,events,sockets};
}

async function startOffered(f:ReturnType<typeof fixture>,sessionId:string){
 assert.equal((await f.call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]})).status,200);
 const reservation=await f.call('/_internal/reserve','POST',{operation_id:'op-'+sessionId,dispatch_id:'dispatch-'+sessionId,attempt:0,
  session_id:sessionId,project_id:'project-test',intake_id:'intake-'+sessionId,machine_id:'h1',request_digest:'digest-'+sessionId,
  title:'test session',acceptance_criteria:['test'],account_id:'codex',launch:{workspace:'relay',engine:'codex',model:'gpt-6.1-sol',prompt:'test work',interactive:false,timeout_seconds:1800}});
 assert.equal(reservation.status,201);const reserved=await reservation.json() as any;
 assert.equal((await f.call('/_internal/activate','POST',{operation_id:'op-'+sessionId,session_id:sessionId,epoch:reserved.epoch})).status,201);
 return reserved.epoch as number;
}

function hostSocket(f:ReturnType<typeof fixture>){
 const socket={readyState:1,deserializeAttachment:()=>({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,connection_id:'host-start-diagnostics'}),send:()=>{}};
 f.sockets.push(socket);return socket;
}

async function view(f:ReturnType<typeof fixture>,sessionId:string){
 const response=await f.call('/api/sessions/'+sessionId,'GET',undefined,operator);
 assert.equal(response.status,200);return (await response.json() as any).session;
}

test('host.error refusal persists a sanitized attempt-bound diagnostic in the failed session projection',async()=>{
 const f=fixture(),sessionId='host-diagnostic-refusal',epoch=await startOffered(f,sessionId),socket=hostSocket(f);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,epoch,stage:'host_launch',reason:'workspace_unavailable',
  detail:'Requested workspace unavailable. Try the enrolled workspace. '+'x'.repeat(400)}));
 const stored=JSON.parse((f.database.prepare('SELECT evidence_json FROM sessions WHERE id=?').get(sessionId) as any).evidence_json);
 assert.match(stored._hub_host_start_diagnostic.detail,/Requested workspace unavailable/);
 assert.equal(stored._hub_host_start_diagnostic.attempt,0);
 assert.equal(stored._hub_host_start_diagnostic.detail.length,240);
 const session=await view(f,sessionId);
 assert.equal(session.state,'failed');assert.equal(session.epoch,epoch);
 assert.deepEqual(session.attention.reason,'workspace_unavailable');
 assert.equal(session.attention.stage,'host_launch');
 assert.match(session.attention.detail,/Requested workspace unavailable/);
 assert.match(session.attention.detail,/Try the enrolled workspace/);
 assert.doesNotMatch(session.attention.detail,/super-secret-token|\u001b|\n/);
 assert.ok(session.attention.detail.length<=240);
 assert.equal(session.evidence,null,'internal diagnostic envelope is exposed only through attention');
 assert.equal(f.database.prepare('PRAGMA table_info(attention)').all().length,5,'diagnostics need no schema migration');
});

for(const [label,detail] of [
 ['authorization header split by NUL','Authorization: Bearer\u0000 synthetic-private-token'],
 ['secret assignment with colon','AWS_SECRET_ACCESS_KEY: synthetic-private-token'],
 ['double quoted secret key','"AWS_SECRET_ACCESS_KEY": "synthetic-private-token"'],
 ['double quoted access token','"access_token": "synthetic-private-token"'],
 ['single quoted access token',"'access_token': 'synthetic-private-token'"],
 ['backtick quoted access token','`access_token`: `synthetic-private-token`'],
 ['private key', '"private_key": "synthetic-private-token"'],
 ['spaced API key', '"api key": "synthetic-private-token"'],
 ['padded access token', '"access_token ": "synthetic-private-token"'],
 ['URL token userinfo', 'https://synthetic-private-token@example.test/repo.git'],
 ['unquoted private key', 'private_key: synthetic-private-token'],
 ['unquoted spaced API key', 'api key: synthetic-private-token'],
] as const){
 test(`host.error rejects ${label} detail from storage and session projection`,async()=>{
  const f=fixture(),sessionId='host-diagnostic-unsafe-'+label.replace(/[^a-z]+/gi,'-'),epoch=await startOffered(f,sessionId),socket=hostSocket(f);
  await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
   operation_id:'op-'+sessionId,epoch,stage:'host_launch',reason:'workspace_unavailable',detail}));
  const stored=JSON.parse((f.database.prepare('SELECT evidence_json FROM sessions WHERE id=?').get(sessionId) as any).evidence_json);
  const diagnostic=stored._hub_host_start_diagnostic;
  assert.equal(diagnostic.reason,'workspace_unavailable');assert.equal(diagnostic.stage,'host_launch');
  assert.equal('detail' in diagnostic,false);
  assert.doesNotMatch(JSON.stringify(stored),/synthetic-private-token/);
  const session=await view(f,sessionId);
  assert.equal(session.state,'failed');assert.equal(session.epoch,epoch);
  assert.equal(session.attention.reason,'workspace_unavailable');assert.equal(session.attention.stage,'host_launch');
  assert.equal('detail' in session.attention,false);
  assert.doesNotMatch(JSON.stringify(session),/synthetic-private-token/);
 });
}

test('host.error refuses stale attempts and epochs without changing current attention or failure state',async()=>{
 const f=fixture(),sessionId='host-diagnostic-fence',epoch=await startOffered(f,sessionId),socket=hostSocket(f);
 const common={type:'host.error',session_id:sessionId,stage:'host_launch',reason:'workspace_unavailable',detail:'stale diagnostic'};
 await f.machine.webSocketMessage(socket as any,JSON.stringify({...common,operation_id:'old-operation',epoch}));
 await f.machine.webSocketMessage(socket as any,JSON.stringify({...common,operation_id:'op-'+sessionId,epoch:epoch+1}));
 let session=await view(f,sessionId);
 assert.equal(session.state,'offered');assert.equal(session.attention,null);assert.equal(session.evidence,null);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({...common,operation_id:'op-'+sessionId,epoch}));
 session=await view(f,sessionId);
 assert.equal(session.state,'failed');assert.equal(session.attention.reason,'workspace_unavailable');
 assert.equal(session.attention.detail,'stale diagnostic');
});

test('legacy host.error without stage or detail keeps its terminal refusal reason',async()=>{
 const f=fixture(),sessionId='host-diagnostic-legacy',epoch=await startOffered(f,sessionId),socket=hostSocket(f);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,reason:'workspace_unavailable'}));
 const session=await view(f,sessionId);
 assert.equal(session.state,'failed');assert.equal(session.epoch,epoch);
 assert.equal(session.attention.reason,'workspace_unavailable');
 assert.equal('stage' in session.attention,false);assert.equal('detail' in session.attention,false);
});

test('malformed host launch stage remains diagnostic-only and cannot fail an offer',async()=>{
 const f=fixture(),sessionId='host-diagnostic-bad-stage';await startOffered(f,sessionId);const socket=hostSocket(f);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,stage:'host_launch\u001b[31m',reason:'workspace_unavailable',detail:{raw:'not text'}}));
 const session=await view(f,sessionId);
 assert.equal(session.state,'offered');assert.equal(session.attention.reason,'workspace_unavailable');
 assert.equal('stage' in session.attention,false);assert.equal('detail' in session.attention,false);
});

test('raw stderr and structured config are not projected as host detail',async()=>{
 const f=fixture(),sessionId='host-diagnostic-raw-output',epoch=await startOffered(f,sessionId),socket=hostSocket(f);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,epoch,stage:'host_launch',reason:'workspace_unavailable',
  detail:'raw stderr: Traceback private-test-secret'}));
 const session=await view(f,sessionId);
 assert.equal(session.state,'failed');assert.equal(session.epoch,epoch);
 assert.equal(session.attention.reason,'workspace_unavailable');assert.equal(session.attention.stage,'host_launch');
 assert.equal('detail' in session.attention,false);
});

test('host diagnostics do not rewrite digest-bound native exit evidence',async()=>{
 const f=fixture(),sessionId='host-diagnostic-native-proof',epoch=await startOffered(f,sessionId),socket=hostSocket(f);
 const proof={operation_id:'op-'+sessionId,epoch,native_id:'11111111-2222-4333-8444-555555555555',
  evidence:{native_exit_confirmed:true,native_exit:{code:0},native_identity:{provider:'aplexer'}}};
 const evidenceJson=JSON.stringify(proof),digest='a'.repeat(64);
 f.database.prepare("UPDATE sessions SET state='stopped',native_id=?,evidence_json=?,evidence_digest=? WHERE id=?")
  .run(proof.native_id,evidenceJson,digest,sessionId);
 await f.machine.webSocketMessage(socket as any,JSON.stringify({type:'host.error',session_id:sessionId,
  operation_id:'op-'+sessionId,epoch,stage:'host_launch',reason:'workspace_unavailable',detail:'late host error'}));
 const stored=f.database.prepare('SELECT evidence_json,evidence_digest FROM sessions WHERE id=?').get(sessionId) as any;
 assert.equal(stored.evidence_json,evidenceJson);assert.equal(stored.evidence_digest,digest);
 const session=await view(f,sessionId);
 assert.equal(session.state,'stopped');assert.deepEqual(session.evidence,proof);
 assert.equal(session.attention.reason,'workspace_unavailable');
 assert.equal('stage' in session.attention,false);assert.equal('detail' in session.attention,false);
});
