import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';
import worker from '../src/worker';
import {ProjectDO} from '../src/project-do';
import {ControlDO} from '../src/control-do';
import {MachineDO} from '../src/machine-do';
import {MailboxDO} from '../src/mailbox-do';

const DEV='dev-token-0123456789abcdef0123456789abcdef';
const adminHeaders={'Content-Type':'application/json','Authorization':'Bearer '+DEV};

function doCtx(name:string){const sql=new DatabaseSync(':memory:');let alarm:any=null;
 return {id:{toString:()=>name},getWebSockets:()=>[],serializeAttachment(){},deserializeAttachment(){return null;},storage:{sql:{exec(q:string,...a:any[]){const p=sql.prepare(q);const rows=p.columns().length?p.all(...a):(p.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:any){sql.exec('BEGIN');try{const r=fn();sql.exec('COMMIT');return r;}catch(e){sql.exec('ROLLBACK');throw e;}},async getAlarm(){return alarm;},async setAlarm(at:number){alarm=at;},async deleteAlarm(){alarm=null;}},blockConcurrencyWhile:(f:any)=>Promise.resolve(f())};}

/** Full five-DO composition behind the real worker fetch, for CLI/MCP compat gates. */
function fixture(){const cache=new Map<string,{fetch:(r:Request)=>Promise<Response>}>();
 const stub=(key:string,make:()=>{fetch:(r:Request)=>Promise<Response>})=>({fetch:async(input:Request|string,init?:RequestInit)=>{const r=typeof input==='string'?new Request(input,init):input;return instance(key,make).fetch(r);}});
 const env:any={CONTROL:{idFromName:(n:string)=>n,get:(n:string)=>stub('control',()=>new ControlDO(doCtx('control'),env))},
  PROJECTS:{idFromName:(n:string)=>n,get:(n:string)=>stub('project:'+n,()=>new ProjectDO(doCtx('project-'+n),env))},
  MACHINES:{idFromName:(n:string)=>n,get:(n:string)=>stub('machine:'+n,()=>new MachineDO(doCtx('machine-'+n),env))},
  ACCOUNTS:{idFromName:(n:string)=>n,get:(n:string)=>({fetch:async()=>Response.json({error:'no_account',status:503},{status:503})})},
  MAILBOXES:{idFromName:(n:string)=>n,get:(n:string)=>stub('mailbox:'+n,()=>new MailboxDO(doCtx('mailbox-'+n),env))},
  HUB2_DEV_OPERATOR_TOKEN:DEV,
  HUB2_CREDENTIAL_KEY:'a'.repeat(64)};
 function instance(key:string,make:()=>{fetch:(r:Request)=>Promise<Response>}){let i=cache.get(key);if(!i){i=make();cache.set(key,i);}return i;}
 const call=async(p:string,m='GET',b?:unknown,headers:Record<string,string>=adminHeaders)=>{
   const h={...headers};
   return worker.fetch(new Request('https://hub2.test'+p,{method:m,headers:h,...(b===undefined?{}:{body:JSON.stringify(b)})}),env);};
 const direct=async(p:string,b?:unknown,m='POST')=>(await instance('control',()=>new ControlDO(doCtx('control'),env)).fetch(new Request('https://control.internal'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify({id:'fixture',kind:'operator',scopes:['*']})},...(b===undefined?{}:{body:JSON.stringify(b)})})));
 return {env,call,direct,get cache(){return cache;}};
}
const body=async(r:Response)=>await r.json() as any;

/** Enroll a machine through the public pairing path and mint a real session
 * worker credential bound to its route (the sanctioned 166af2de/M2 path). */
async function enrollSession(f:ReturnType<typeof fixture>,project:string,intakeId:string,sessionId:string,machineId='m9'){
 const pair=await body(await f.call('/api/enrollments/pair','POST',{machine_id:machineId,alias:machineId}));
 await body(await f.call('/api/enrollments/complete','POST',{code:pair.pairing_code}));
 const controlDb=(f.cache.get('control') as any).db;
 const generation=controlDb.one<{generation:string}>("SELECT generation FROM credentials WHERE kind='machine' AND resource_id=? AND revoked_at IS NULL",machineId)?.generation;
 assert(generation,'machine credential row exists');
 await f.direct('/_routes/register',{kind:'session',id:sessionId,owner:machineId,resource_id:project,participants:['session:'+sessionId],bound_intake_id:intakeId});
 const issued=await body(await f.direct('/_worker/issue',{session_id:sessionId,epoch:1,operation_id:'op-'+sessionId,machine_id:machineId,machine_generation:generation,intake_id:intakeId,project_id:project,deadline:Date.now()+3600000}));
 return issued;
}
async function repo(f:ReturnType<typeof fixture>,name:string){
 const r=await f.call('/api/repos/'+name,'PATCH',{canonical_url:'https://example.test/'+name,purpose:'cli compat fixture'});assert.equal(r.status,200,name+' repo');
}
async function intake(f:ReturnType<typeof fixture>,project:string,key:string,title:string){
 const r=await f.call('/api/intakes?project='+project,'POST',{key,title,text:'Compat body '+key,acceptance_criteria:['Works']});assert.equal(r.status,201);return (await body(r)).intake;
}

test('intake list pages through `after` with churn, search marks filtered_page, short-id show resolves',async()=>{
 const f=fixture();
 await f.direct('/_actors/register',{id:'human:dev',kind:'human',enabled:true});
 await repo(f,'org/a');
 const made=[];for(let i=0;i<5;i++)made.push(await intake(f,'org/a','page-'+i,'Paged task '+i));
 const first=await body(await f.call('/api/intakes?project=org/a&limit=2'));assert.equal(first.items.length,2);assert(first.next);
 const churn=await intake(f,'org/a','churn','Created during walk');
 const seen:any[]=[];let cursor:string|null=first.next,pages=0;
 while(cursor&&pages<20){pages++;const page=await body(await f.call('/api/intakes?project=org/a&limit=2&after='+encodeURIComponent(cursor)));seen.push(...page.items.map((t:any)=>t.id));cursor=page.next;}
 assert.equal(cursor,null);
 // Every intake that existed before the walk plus the churn intake appears
 // exactly once (duplicates between the view page and the ascending walk are
 // expected and deduped by id).
 assert.deepEqual([...new Set(seen)].sort(),[...made.map(v=>v.id),churn.id].sort());
 // Replaying the same cursor returns the same page (stable keyset position).
 const replay=await body(await f.call('/api/intakes?project=org/a&limit=2&after='+encodeURIComponent(first.next)));
 const replayAgain=await body(await f.call('/api/intakes?project=org/a&limit=2&after='+encodeURIComponent(first.next)));
 assert.deepEqual(replay.items.map((t:any)=>t.id),replayAgain.items.map((t:any)=>t.id));
 const search=await body(await f.call('/api/intakes?project=org/a&q=churn'));assert.equal(search.items.length,1);assert.equal(search.filtered_page,true);
 const short=await body(await f.call('/api/intakes/'+made[0].id.slice(0,8)));assert.equal(short.intake.id,made[0].id);
 assert.equal((await f.call('/api/intakes/ffffffff-ffff-ffff-ffff-ffffffffffff')).status,404);
 // Legacy `cursor` param keeps working for existing callers.
 const legacy=await body(await f.call('/api/intakes?project=org/a&limit=2&cursor='+encodeURIComponent(first.next)));
 assert.equal(legacy.items.length,2);
});

test('worker append compat: real session credential shows own intake, appends, foreign denied',async()=>{
 const f=fixture();
 await f.direct('/_actors/register',{id:'human:dev',kind:'human',enabled:true});
 await repo(f,'org/a');
 const own=await intake(f,'org/a','append-own','Append target'),other=await intake(f,'org/a','append-other','Not ours');
 const issued=await enrollSession(f,'org/a',own.id,'aaaaaaaa-1111-4111-8111-111111111111');
 assert.equal(issued.actor,'session:aaaaaaaa-1111-4111-8111-111111111111');
 const sessionHeaders={'Content-Type':'application/json','Authorization':'Bearer '+issued.token};
 assert.equal((await f.call('/api/intake/tasks/'+own.id,'GET',undefined,sessionHeaders)).status,200);
 const append=await f.call('/api/intake/tasks/'+own.id+'/events','POST',{key:'w1',expected_version:7,note:'Progress note'},sessionHeaders);
 assert.equal(append.status,201);
 const hist=await body(await f.call('/api/intakes/'+own.id+'/history'));
 assert(hist.events.some((e:any)=>e.kind==='worker_append'&&e.body.note==='Progress note'));
 assert.equal((await f.call('/api/intake/tasks/'+own.id+'/events','POST',{key:'w2',event:{report:'evidence'}},sessionHeaders)).status,201);
 assert.equal((await f.call('/api/intake/tasks/'+own.id+'/events','POST',{key:'w1',note:'changed'},sessionHeaders)).status,409);
 // Foreign intake is denied at the route directory (404: no existence leak)
 // and would be 403 at the project owner even if the route were known.
 const foreignShow=await f.call('/api/intakes/'+other.id,'GET',undefined,sessionHeaders);
 assert([403,404].includes(foreignShow.status),'foreign show denied');
 const foreignAppend=await f.call('/api/intake/tasks/'+other.id+'/events','POST',{key:'w3',note:'foreign'},sessionHeaders);
 assert([403,404].includes(foreignAppend.status),'foreign append denied');
});

test('bus compat: send/reply/inbox/stages/readdress keep the legacy surface over Hub2 mail',async()=>{
 const f=fixture();
 await f.direct('/_actors/register',{id:'human:dev',kind:'human',enabled:true});
 const sent=await body(await f.call('/api/bus/send','POST',{from_actor:'human:dev',to_actor:'human:dev',kind:'note',body:'compat hello',idempotency_key:'m1'}));
 assert.equal(sent.message.body,'compat hello');
 const shown=await body(await f.call('/api/bus/messages/'+sent.message.id));assert.equal(shown.message.from_actor,'human:dev');assert.equal(shown.message.to_actor,'human:dev');
 const replied=await body(await f.call('/api/bus/reply','POST',{from_actor:'human:dev',body:'a reply',idempotency_key:'m2',reply_to:sent.message.id}));
 assert.equal(replied.message.reply_to,sent.message.id);assert.equal(replied.message.thread_id,sent.message.thread_id);
 for(const bad of [{kind:'alert'},{delivery:'tui'},{ref:{a:1}},{thread_id:'t'},{deliver_by_s:600}])
  assert.equal((await f.call('/api/bus/send','POST',{from_actor:'human:dev',to_actor:'human:dev',body:'x',idempotency_key:'bad-'+JSON.stringify(bad),...bad})).status,400,'refuses '+JSON.stringify(bad));
 const inbox=await body(await f.call('/api/bus/inbox?actor=human:dev&unread=1'));
 assert(inbox.messages.some((m:any)=>m.id===replied.message.id));assert.equal(inbox.actor,'human:dev');assert.equal(inbox.messages[0].from_actor,'human:dev');
 const ack=await body(await f.call('/api/bus/messages/'+replied.message.id+'/ack','POST',{lease_epoch:replied.message.epoch}));assert.equal(ack.recorded,true);
 const batch=await body(await f.call('/api/bus/messages/read','POST',{ids:[replied.message.id]}));assert.equal(batch.marked,1);
 const status=await body(await f.call('/api/bus/messages/'+replied.message.id+'/status'));assert(status.events.some((e:any)=>e.stage==='read'));
 const readdr=await body(await f.call('/api/bus/messages/'+sent.message.id+'/readdress','POST',{to:'human:dev'}));assert.equal(readdr.message.id,sent.message.id);
 const replay=await body(await f.call('/api/bus/send','POST',{from_actor:'human:dev',to_actor:'human:dev',kind:'note',body:'compat hello',idempotency_key:'m1'}));
 assert.equal(replay.replayed,true);assert.equal(replay.message.id,sent.message.id);
});

test('messaging M2/M6: real session credentials are project-scoped; named agents mint, delegate, forward',async()=>{
 const f=fixture();
 await f.direct('/_actors/register',{id:'human:dev',kind:'human',enabled:true});
 await repo(f,'org/a');await repo(f,'org/b');
 const ownA=await intake(f,'org/a','a-intake','Project A task');
 const a='aaaaaaaa-1111-4111-8111-111111111111',b='bbbbbbbb-2222-4222-8222-222222222222',c='cccccccc-3333-4333-8333-333333333333';
 await f.direct('/_routes/register',{kind:'session',id:b,owner:'m9',resource_id:'org/b',participants:['session:'+b]});
 await f.direct('/_routes/register',{kind:'session',id:c,owner:'m9',resource_id:'org/a',participants:['session:'+c]});
 const issued=await enrollSession(f,'org/a',ownA.id,a);
 const sessionHeaders={'Content-Type':'application/json','Authorization':'Bearer '+issued.token};
 const send=async(to:string,key:string)=>f.call('/api/bus/send','POST',{from_actor:'session:'+a,to_actor:to,kind:'note',body:'ping',idempotency_key:key},sessionHeaders);
 assert.equal((await send('session:'+c,'same-project')).status,201);
 assert.equal((await send('human:dev','to-human')).status,201);
 const cross=await send('session:'+b,'cross-project');assert.equal(cross.status,403);assert.equal((await body(cross)).error,'cross_project_forbidden');
 const agent=await body(await f.call('/api/agents/named/developer/create','POST',{display:'Developer'}));
 assert.equal(agent.actor,'agent:developer');assert(typeof agent.access_token==='string'&&agent.access_token.length>=32);
 assert.equal((await send('agent:developer','to-named-agent')).status,201);
 // The minted agent identity authenticates and sends on its own token.
 const agentHeaders={'Content-Type':'application/json','Authorization':'Bearer '+agent.access_token};
 assert.equal((await f.call('/api/bus/send','POST',{from_actor:'agent:developer',to_actor:'human:dev',kind:'note',body:'from developer',idempotency_key:'ag1'},agentHeaders)).status,201);
 // Delegated human inbox: grant the dev family read access, then read as.
 assert.equal((await f.call('/api/agents/named/developer/delegate','POST',{family:'hub2-dev'})).status,200);
 const delegated=await body(await f.call('/api/bus/inbox?actor=agent:developer'));
 assert(delegated.messages.some((m:any)=>m.body==='ping'));
 assert.equal((await f.call('/api/agents/named/developer')).status,200);
 const shown=await body(await f.call('/api/agents/named/developer'));assert.equal(shown.agent.id,'agent:developer');
 assert.equal((await f.call('/api/agents/named/developer/forward','POST',{to:'session:'+c})).status,200);
 const forwarded=await body(await f.call('/api/agents/named/developer'));assert.equal(forwarded.agent.forward_session,c);
 // Refresh keeps the agent family alive for the CLI profile (30-day family).
 // Called on the control auth route directly: the worker's public auth path
 // streams the original body, which only real workerd accepts.
 const refreshed=await body(await f.direct('/_auth/public/api/token/refresh',{refresh_token:agent.refresh_token}));
 assert.equal(refreshed.token_type,'Bearer');assert(typeof refreshed.access_token==='string');
});

test('sessions list and show agree on machine_id plus machine alias; fleet rows carry machine_id (0992f366)',async()=>{
 const f=fixture();
 const md=f.env.MACHINES.get('m9');
 const machineActor={id:'machine:m9',kind:'machine',resource_id:'m9',scopes:['machine:heartbeat','machine:offer','session:report','session:admit','message:delivery']};
 await md.fetch(new Request('https://machine.internal/api/heartbeat',{method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(machineActor)},body:JSON.stringify({os:'linux',arch:'x64',labels:[],engines:[],capabilities:[]})}));
 await md.fetch(new Request('https://machine.internal/_internal/reserve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation_id:'op1',dispatch_id:'d1',session_id:'session-1',project_id:'org/a',intake_id:'i1',machine_id:'m9',request_digest:'dig',title:'T',attempt:0,acceptance_criteria:['A'],launch:{workspace:'/w',engine:'shell',prompt:'p',interactive:false}})}));
 const machine=f.cache.get('machine:m9') as any;
 machine.db.run("UPDATE sessions SET state='running',native_id='n1' WHERE id='session-1'");
 const operator={'x-hub2-actor':JSON.stringify({id:'human:dev',kind:'operator',scopes:['operator:admin']})};
 const listed=await body(await md.fetch(new Request('https://machine.internal/api/sessions?limit=25',{headers:operator})));
 assert.equal(listed.items[0].machine_id,'m9');assert.equal(listed.items[0].machine,'m9');
 const detail=await body(await md.fetch(new Request('https://machine.internal/api/sessions/session-1',{headers:operator})));
 assert.equal(detail.session.machine_id,listed.items[0].machine_id);assert.equal(detail.session.machine,listed.items[0].machine);
 const status=await body(await md.fetch(new Request('https://machine.internal/_internal/status')));
 assert.equal(status.sessions[0].machine_id,'m9');
});

test('MCP parity: intake_list cursor paging and intake_append work through tools/call',async()=>{
 const f=fixture();
 await f.direct('/_actors/register',{id:'human:dev',kind:'human',enabled:true});
 await repo(f,'org/a');
 for(let i=0;i<4;i++)await intake(f,'org/a','mcp-'+i,'Mcp task '+i);
 const call=async(name:string,args:Record<string,unknown>)=>(await body(await f.call('/mcp','POST',{jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:args}}))).result;
 const page1=await call('intake_list',{project:'org/a',limit:2});
 const payload1=JSON.parse(page1.content[0].text);assert.equal(payload1.items.length,2);assert(payload1.next);
 const seen:any[]=[];let cursor:string|null=payload1.next,pages=0;
 while(cursor&&pages<10){pages++;const page=await call('intake_list',{project:'org/a',limit:2,cursor});const payload=JSON.parse(page.content[0].text);seen.push(...payload.items.map((t:any)=>t.id));cursor=payload.next;}
 assert.equal(cursor,null);
 const made=seen.slice();assert.deepEqual([...new Set(seen)].sort(),made.sort());
 const target=await intake(f,'org/a','mcp-append','Append via MCP');
 assert(seen.includes(target.id)===false);
 const appended=await call('intake_append',{id:target.id,key:'mcp-note',note:'Operator note'});
 assert.equal(JSON.parse(appended.content[0].text).appended,true);
});
