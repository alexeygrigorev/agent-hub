import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ControlDO} from '../src/control-do';
import {pickModelRouteDetailed,simulateModelRoutes} from '../src/model-picker';
import {setImmediate as nextTurn} from 'node:timers/promises';
import {spawnSync as pickerAdapterSpawn} from 'node:child_process';
import {fileURLToPath as pickerAdapterFilePath} from 'node:url';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin'],family_id:'operator-family',client_id:'phone'};
function fixture(options:{beforeAccount?:()=>Promise<void>;schemaMode?:string}={}){const database=new DatabaseSync(':memory:'),snapshots=new Map<string,unknown[]>();const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(q:string,...a:any[]){const s=database.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const v=fn();database.exec('COMMIT');return v;}catch(e){database.exec('ROLLBACK');throw e;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 let control:ControlDO;
 const env:any={HUB2_SCHEMA_MIGRATIONS:options.schemaMode,
 CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:(input:string|Request,init?:RequestInit)=>control.fetch(input instanceof Request?input:new Request(input,init))})},
 ACCOUNTS:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>{await options.beforeAccount?.();return Response.json({account:{enabled:true},quota:{remaining:80,captured_at:Date.now(),valid_until:Date.now()+900000},fresh:true,hosts:['host-picker']});}})},
 MACHINES:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(input:string|Request)=>new URL(input instanceof Request?input.url:input).pathname.includes('routing')?Response.json({machine_id:id,connected:true,engines:['codex','claude'],capabilities:['headless']}):Response.json({sessions:snapshots.get(id)||[]})})}};
 control=new ControlDO(state,env);
 const call=(p:string,m='GET',b?:unknown,a:unknown=admin)=>control.fetch(new Request('https://control.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{control,database,call,snapshots,env,restart:()=>{control=new ControlDO(state,env);}};}
const data=async(r:Response)=>await r.json() as any;

function addMachine(database:DatabaseSync,id:string){
 database.prepare('INSERT INTO credentials(actor_id,kind,resource_id,generation,token_hash,scopes_json,family_id,client_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)')
  .run('machine:'+id,'machine',id,'1','token-hash-'+id,'[]','family-'+id,'client-'+id,Date.now());
}

test('global session cap counts fresh live panes across every machine and logs the loud 429',async()=>{
 const f=fixture();await f.call('/api/model-policy');
 for(let i=0;i<51;i++)addMachine(f.database,'host-'+String(i).padStart(3,'0'));
 const now=Date.now();f.snapshots.set('host-050',Array.from({length:50},(_,i)=>({session_id:'live-'+i,native_id:'native-'+i,phase:'running',last_activity_at:now-1000,observed_at:now})));
 const original=console.error;let alert='';console.error=(...args:unknown[])=>{alert=args.map(String).join(' ');};
 let response:Response;
 try{response=await f.call('/_internal/session-cap/check','POST',{operation_id:'op-51',session_id:'session-51'});}
 finally{console.error=original;}
 assert.equal(response!.status,429);
 const body=await data(response!);assert.equal(body.error,'active_session_limit');assert.equal(body.active_count,50);assert.equal(body.limit,50);
 assert.match(body.detail,/50 live active sessions/);assert.match(alert,/ACTIVE_SESSION_LIMIT/);assert.match(alert,/"active_count":50/);
});

test('orphan, dead, exited, idle, and stale DB rows never consume live-session capacity',async()=>{
 const f=fixture();await f.call('/api/model-policy');addMachine(f.database,'host-free');
 const now=Date.now(),rows=Array.from({length:49},(_,i)=>({session_id:'live-'+i,native_id:'native-'+i,phase:'running',last_activity_at:now-1000,observed_at:now}));
 rows.push({session_id:'orphan',native_id:null,phase:'missing',last_activity_at:null,observed_at:now});
 rows.push({session_id:'stale',native_id:'native-stale',phase:'running',last_activity_at:now-7*60*60_000,observed_at:now});
 rows.push({session_id:'exited',native_id:'native-exited',phase:'exited',last_activity_at:now-1000,observed_at:now});
 f.snapshots.set('host-free',rows);
 const response=await f.call('/_internal/session-cap/check','POST',{operation_id:'op-free',session_id:'session-free'});
 assert.equal(response.status,200);const body=await data(response);assert.equal(body.active_count,49);assert.equal(body.starting_count,1);
});

test('named-agent delegation requires current explicit family/client/action grant; revocation removes phone inbox',async()=>{
 const {call}=fixture();assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin'})).status,200);
 const subject={id:'human:reader',kind:'operator',scopes:['intake:read'],family_id:'family-a',client_id:'client1'};
 assert.equal((await call('/_delegate','POST',{as:'orin',action:'read'},subject)).status,403);
 assert.equal((await call('/api/named-agents/orin/grants','PUT',{family_id:'family-a',client_id:'client1',can_read:true,can_send:false,enabled:true},subject)).status,403);
 assert.equal((await call('/api/named-agents/orin/grants','PUT',{family_id:'family-a',client_id:'client1',can_read:true,can_send:false,enabled:true})).status,200);
 const delegated=await data(await call('/_delegate','POST',{as:'agent:orin',action:'read'},subject));assert.equal(delegated.target,'agent:orin');assert.equal(delegated.action,'read');assert.equal(delegated.subject.actor_id,subject.id);
 assert.equal((await call('/_delegate','POST',{as:'orin',action:'send'},subject)).status,403);
 assert.equal((await call('/_delegate','POST',{as:'orin',action:'read'},{...subject,client_id:'different'})).status,403);
 assert.deepEqual((await data(await call('/_mailboxes','POST',{},subject))).owners,['human:reader','agent:orin']);
 await call('/api/named-agents/orin/grants','PUT',{family_id:'family-a',client_id:'client1',can_read:true,can_send:false,enabled:false});
 assert.equal((await call('/_delegate','POST',{as:'orin',action:'read'},subject)).status,403);
 assert.deepEqual((await data(await call('/_mailboxes','POST',{},subject))).owners,['human:reader']);
});
test('directory owner immutable, ACL readdress replaces readers, scoped workers cannot discover another intake',async()=>{
 const {call}=fixture(),route={kind:'message',id:'message-000001',owner:'agent:sender',resource_id:'message-000001',participants:['agent:sender','agent:old']};
 assert.equal((await call('/_routes/register','POST',route)).status,200);
 assert.equal((await call('/_routes/register','POST',{...route,owner:'agent:hijack'})).status,409);
 const old={id:'agent:old',kind:'agent',scopes:[]};assert.equal((await call('/api/routes/message/message-000001','GET',undefined,old)).status,200);
 await call('/_routes/register','POST',{...route,epoch:2,participants:['agent:sender','agent:new']});assert.equal((await call('/api/routes/message/message-000001','GET',undefined,old)).status,404);
 assert.equal((await call('/_routes/register','POST',route)).status,409);
 assert.equal((await call('/api/routes/message/message-000001','GET',undefined,{id:'agent:new',kind:'agent',scopes:[]})).status,200);
 await call('/_routes/register','POST',{kind:'intake',id:'intake-000001',owner:'project1',resource_id:'intake-000001',participants:[]});
 await call('/_routes/register','POST',{kind:'intake',id:'intake-000002',owner:'project1',resource_id:'intake-000002',participants:[]});
 const worker={id:'agent:worker',kind:'agent',scopes:['intake:read'],resource_id:'intake-000001'};
 assert.equal((await call('/api/routes/intake/intake-000002','GET',undefined,worker)).status,404);
 assert.equal((await call('/api/routes/intake/intake-000001','GET',undefined,worker)).status,200);
 assert.equal((await call('/api/routes/intake/intake-','GET',undefined,admin)).status,409);
 const unique=await data(await call('/api/routes/intake/intake-','GET',undefined,worker));assert.equal(unique.route.id,'intake-000001');
});
test('machine re-enrollment rotates credential family and revokes predecessor token',async()=>{
 const {call}=fixture();async function pair(){const p=await data(await call('/api/enrollments/pair','POST',{machine_id:'host1',alias:'test'}));return await data(await call('/api/enrollments/complete','POST',{code:p.pairing_code}));}
 const first=await pair(),verified=await data(await call('/_verify','POST',{token:first.token}));assert(verified.actor.family_id);
 const second=await pair();assert.equal((await call('/_verify','POST',{token:first.token})).status,401);
 const current=await data(await call('/_verify','POST',{token:second.token}));assert.notEqual(current.actor.family_id,verified.actor.family_id);assert.equal(current.actor.resource_id,'host1');
});
test('canonical duplicates cannot point to duplicates or create a registry cycle',async()=>{
 const {call}=fixture();const a='/api/repos/'+encodeURIComponent('owner/a'),b='/api/repos/'+encodeURIComponent('owner/b');
 assert.equal((await call(a,'PATCH',{canonical_url:'https://example.test/a',purpose:'a'})).status,200);
 assert.equal((await call(b,'PATCH',{canonical_url:'https://example.test/b',purpose:'b'})).status,200);
 assert.equal((await call(a,'PATCH',{status:'duplicate',duplicate_of:'owner/b'})).status,200);
 assert.equal((await call(b,'PATCH',{status:'duplicate',duplicate_of:'owner/a'})).status,409);
 await call(a,'PATCH',{status:'active'});const active=await data(await call(a));assert.equal(active.repo.duplicate_of,null);
});
test('named-agent forwarding requires a registered session and keeps stable mailbox address',async()=>{
 const {call}=fixture();assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin',forward_session:'s1'})).status,400);
 await call('/_routes/register','POST',{kind:'session',id:'s1',owner:'machine1',resource_id:'intake1',participants:[]});
 assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin',forward_session:'s1'})).status,200);
 assert.deepEqual(await data(await call('/_recipient','POST',{recipient:'agent:orin'})),{mailbox:'agent:orin',forward_session:'s1'});
});
test('canonical repository cannot be archived while duplicates still depend on its active identity',async()=>{
 const {call}=fixture();const a='/api/repos/'+encodeURIComponent('owner/a'),b='/api/repos/'+encodeURIComponent('owner/b');
 await call(a,'PATCH',{canonical_url:'https://example.test/a',purpose:'a'});await call(b,'PATCH',{canonical_url:'https://example.test/b',purpose:'b'});
 await call(a,'PATCH',{status:'duplicate',duplicate_of:'owner/b'});assert.equal((await call(b,'PATCH',{status:'archived'})).status,409);
 await call(a,'PATCH',{status:'active'});assert.equal((await call(b,'PATCH',{status:'archived'})).status,200);
});
test('recipient resolution refuses unknown/disabled actors and accepts only registered human/session identities',async()=>{
 const {call}=fixture();for(const recipient of ['agent:missing','human:missing','session:missing','arbitrary'])assert.equal((await call('/_recipient','POST',{recipient})).status,404);
 await call('/api/named-agents','POST',{name:'orin',display_name:'Orin',enabled:false});assert.equal((await call('/_recipient','POST',{recipient:'agent:orin'})).status,404);
 assert.equal((await call('/_actors/register','POST',{id:'human:known',kind:'human',enabled:true})).status,200);
 assert.equal((await call('/_recipient','POST',{recipient:'human:known'})).status,200);
 await call('/_actors/register','POST',{id:'human:known',kind:'human',enabled:false});assert.equal((await call('/_recipient','POST',{recipient:'human:known'})).status,404);
 await call('/_routes/register','POST',{kind:'session',id:'known-session',owner:'host',resource_id:'task',participants:[]});assert.equal((await call('/_recipient','POST',{recipient:'session:known-session'})).status,200);
});
test('model policy is explicit bounded operator configuration with immutable CAS replay',async()=>{
 const {call}=fixture(),c={id:'candidate',engine:'codex',model:'configured/model',account_id:'approved-account',weight:2,capabilities:['code'],enabled:true};
 assert.equal((await call('/_routing-policy')).status,404);
 assert.equal((await call('/api/model-policy','PUT',{candidates:[c]},{id:'worker',kind:'agent',scopes:[]})).status,403);
 assert.equal((await call('/api/model-policy','PUT',{candidates:[{...c,weight:0}]})).status,400);
 assert.equal((await call('/api/model-policy','PUT',{candidates:[c,c]})).status,400);
 const first=await data(await call('/api/model-policy','PUT',{expected_version:0,candidates:[c]}));assert.equal(first.version,1);
 assert.equal((await data(await call('/api/model-policy','PUT',{expected_version:1,candidates:[c]}))).unchanged,true);
 assert.equal((await call('/api/model-policy','PUT',{expected_version:0,candidates:[{...c,weight:3}]})).status,409);
 assert.deepEqual((await data(await call('/_routing-policy'))).candidates,[c]);
 assert.equal((await call('/api/model-policy','PUT',{candidates:Array.from({length:33},(_,i)=>({...c,id:'c'+i}))})).status,400);
});
test('canonical project route resource keeps bound intake and related session authorization isolated including prefixes',async()=>{
 const {call}=fixture();
 for(const [id,project] of [['intake-shared-001','org/a'],['intake-shared-002','org/a'],['intake-other-003','org/b']])await call('/_routes/register','POST',{kind:'intake',id,owner:project,resource_id:project,participants:[]});
 for(const [id,intake] of [['session-shared-001','intake-shared-001'],['session-shared-002','intake-shared-002']])await call('/_routes/register','POST',{kind:'session',id,owner:'machine1',resource_id:'org/a',bound_intake_id:intake,participants:[]});
 const worker={id:'agent:bound',kind:'agent',resource_id:'intake-shared-001',scopes:['intake:read']};
 assert.equal((await call('/api/routes/intake/intake-shared-001','GET',undefined,worker)).status,200);
 assert.equal((await call('/api/routes/intake/intake-shared-002','GET',undefined,worker)).status,404);
 assert.equal((await data(await call('/api/routes/intake/intake-shared-','GET',undefined,worker))).route.id,'intake-shared-001');
 assert.equal((await data(await call('/api/routes/session/session-shared-','GET',undefined,worker))).route.id,'session-shared-001');
 assert.equal((await call('/api/routes/session/session-shared-002','GET',undefined,worker)).status,404);
 const reader={id:'agent:reader',kind:'agent',scopes:['project:org/a','intake:read']};
 assert.equal((await call('/api/routes/intake/intake-shared-','GET',undefined,reader)).status,409);
 assert.equal((await call('/api/routes/intake/intake-other-003','GET',undefined,reader)).status,404);
 assert.equal((await call('/api/routes/intake/intake-shared-001','GET',undefined,{...reader,scopes:['intake:read']})).status,404);
 assert.equal((await call('/api/routes/intake/intake-shared-001','GET',undefined,{...reader,scopes:['project:org/a']})).status,404);
 assert.equal((await call('/_routes/register','POST',{kind:'session',id:'session-shared-001',owner:'machine1',resource_id:'org/a',bound_intake_id:'intake-shared-002',participants:[],epoch:2})).status,409);
});


const pickerCandidates=[
 {id:'sol',model:'gpt-6.1-sol',engine:'codex',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'sonnet',model:'sonnet',engine:'claude',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'opus',model:'opus',engine:'claude',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
];
async function configurePicker(f:ReturnType<typeof fixture>){
 await f.call('/api/model-policy');
 addMachine(f.database,'host-picker');
 (f.control as any).db.run('INSERT OR REPLACE INTO model_policy VALUES(1,?,?,?)',1,JSON.stringify(pickerCandidates),'synthetic');
}
function draw(operation:string,model:string|undefined='gpt-6.1-sol'){
 return {project_id:'org/a',operation_id:operation,attempt:0,input:{interactive:false,...(model?{model}:{})}};
}

test('central real draws commit once and operator delivery consumes their occurrences',async()=>{
 const f=fixture();await configurePicker(f);
 let first:any;
 for(let index=0;index<3;index++){
  const response=await f.call('/_internal/picker/draw','POST',draw('operation-'+index));
  assert.equal(response.status,200);const result=await data(response);if(index===0)first=result;
 }
 f.restart();
 assert.deepEqual(await data(await f.call('/_internal/picker/draw','POST',draw('operation-0'))),first);
 assert.equal((await f.call('/_internal/picker/draw','POST',draw('operation-0','sonnet'))).status,409);
 const counters=await data(await f.call('/api/picker-alerts/counters'));
 assert.equal(counters.total_picks,3);assert.equal(counters.occurrence_watermark,1);assert.equal(counters.coverage.available,true);
 const machine={id:'machine:h1',kind:'machine',resource_id:'h1',scopes:['session:report']};
 assert.equal((await f.call('/api/picker-alerts/offers','GET',undefined,machine)).status,403);
 const offers=await data(await f.call('/api/picker-alerts/offers'));
 assert.equal(offers.offers.length,1);const offered=offers.offers[0];assert.equal(offered.recipient,'dev@dtcdev.click');
 assert.equal((await f.call('/api/picker-alerts/1/ack','POST',{lease_token:'a'.repeat(64)})).status,409);
 assert.equal((await f.call('/api/picker-alerts/1/ack','POST',{lease_token:offered.lease_token})).status,200);
 assert.equal((await f.call('/api/picker-alerts/1/ack','POST',{lease_token:offered.lease_token})).status,200);
 assert.equal((await data(await f.call('/api/picker-alerts/offers'))).offers.length,0);
 assert.equal((await f.call('/_internal/picker/record','POST',{})).status,404);
});

test('a delayed healthy actual draw resets the global streak before a subsequent degraded draw',async()=>{
 let block=false,blocked=false,entered!:()=>void,release!:()=>void;
 const started=new Promise<void>(resolve=>{entered=resolve;});
 const gate=new Promise<void>(resolve=>{release=resolve;});
 const f=fixture({beforeAccount:async()=>{if(block&&!blocked){blocked=true;entered();await gate;}}});await configurePicker(f);
 await f.call('/_internal/picker/draw','POST',draw('degraded-1'));
 await f.call('/_internal/picker/draw','POST',draw('degraded-2'));
 block=true;
 const healthy=f.call('/_internal/picker/draw','POST',{...draw('healthy'),project_id:'org/b',input:{interactive:false}});
 await started;
 const degraded=f.call('/_internal/picker/draw','POST',{...draw('degraded-3'),project_id:'org/c'});
 await nextTurn();
 assert.equal((await data(await f.call('/api/picker-alerts/counters'))).total_picks,2);
 release();
 assert.equal((await healthy).status,200);assert.equal((await degraded).status,200);
 const counters=await data(await f.call('/api/picker-alerts/counters'));
 assert.equal(counters.total_picks,4);assert.equal(counters.consecutive_degraded,1);assert.equal(counters.occurrence_watermark,0);
 const receipts=f.database.prepare('SELECT operation_id FROM picker_receipts ORDER BY receipt_seq').all();
 assert.deepEqual(receipts.map(row=>row.operation_id),['degraded-1','degraded-2','healthy','degraded-3']);
});

test('schema four draws continue with explicit unavailable coverage',async()=>{
 const f=fixture({schemaMode:'defer'});await configurePicker(f);
 const response=await data(await f.call('/_internal/picker/draw','POST',draw('bridge')));
 assert.equal(response.route.model,'gpt-6.1-sol');assert.deepEqual(response.coverage,{available:false});
 assert.equal(f.database.prepare('SELECT version FROM hub2_schema').get()!.version,4);
 assert.equal(f.database.prepare("SELECT COUNT(*) AS n FROM sqlite_master WHERE name='picker_draws'").get()!.n,0);
});

test('preview and simulation draws do not advance actual draw counters',async()=>{
 const f=fixture();await configurePicker(f);
 await f.call('/_internal/picker/draw','POST',draw('actual'));
 const before=await data(await f.call('/api/picker-alerts/counters'));
 await pickModelRouteDetailed(f.env,{interactive:false});
 await simulateModelRoutes(f.env,{interactive:false},20);
 assert.deepEqual(await data(await f.call('/api/picker-alerts/counters')),before);
});

test('collector write failures return the real route and reset the interrupted streak before recovery',async()=>{
 const f=fixture();await configurePicker(f);
 await f.call('/_internal/picker/draw','POST',draw('before-gap-1'));
 await f.call('/_internal/picker/draw','POST',draw('before-gap-2'));
 f.database.exec("CREATE TRIGGER reject_draw BEFORE INSERT ON picker_draws BEGIN SELECT RAISE(ABORT,'synthetic collector failure'); END");
 const healthy=await f.call('/_internal/picker/draw','POST',{...draw('unobserved-healthy'),input:{interactive:false}});
 assert.equal(healthy.status,200);
 const actual=await data(healthy);
 assert(actual.route.model);assert.equal(actual.diagnostics.pool.length,3);
 assert.deepEqual(actual.coverage,{available:false,reason:'picker_observer_unavailable'});
 let counters=await data(await f.call('/api/picker-alerts/counters'));
 assert.equal(counters.total_picks,2);assert.equal(counters.coverage.available,false);
 f.database.exec('DROP TRIGGER reject_draw');
 f.database.exec("CREATE TRIGGER reject_gap_reset BEFORE UPDATE ON picker_coverage BEGIN SELECT RAISE(ABORT,'synthetic coverage failure'); END");
 const stillUnavailable=await f.call('/_internal/picker/draw','POST',draw('reset-write-fails'));
 assert.equal(stillUnavailable.status,200);assert.equal((await data(stillUnavailable)).coverage.available,false);
 assert.equal((await data(await f.call('/api/picker-alerts/counters'))).total_picks,2);
 f.database.exec('DROP TRIGGER reject_gap_reset');
 assert.equal((await f.call('/_internal/picker/draw','POST',draw('after-gap'))).status,200);
 counters=await data(await f.call('/api/picker-alerts/counters'));
 assert.equal(counters.total_picks,3);assert.equal(counters.consecutive_degraded,1);
 assert.equal(counters.occurrence_watermark,0);assert.equal(counters.coverage.available,true);
});

test('collector replay read failures are visible and fail open while actual routing errors remain errors',async()=>{
 const f=fixture();await configurePicker(f);
 const db=(f.control as any).db,read=db.all.bind(db);
 db.all=(query:string,...args:unknown[])=>{
  if(query.startsWith('SELECT input_digest,response_json FROM picker_draws'))throw Error('synthetic collector read failure');
  return read(query,...args);
 };
 const response=await f.call('/_internal/picker/draw','POST',draw('unobserved'));
 assert.equal(response.status,200);
 assert.equal((await data(response)).coverage.available,false);
 db.all=read;
 const invalid=await f.call('/_internal/picker/draw','POST',draw('invalid-model','unconfigured-model'));
 assert.equal(invalid.status,503);assert.equal((await data(invalid)).error,'model_policy_unavailable');
 assert.equal((await f.call('/_internal/picker/draw','POST',draw('recovered'))).status,200);
 const counters=await data(await f.call('/api/picker-alerts/counters'));
 assert.equal(counters.total_picks,1);assert.equal(counters.consecutive_degraded,1);
});

test('observer gap recovery changes coverage identity even within the same millisecond',async t=>{
 t.mock.method(Date,'now',()=>1700000000000);
 const f=fixture();await configurePicker(f);
 await f.call('/_internal/picker/draw','POST',draw('before-same-clock-gap'));
 const before=(await data(await f.call('/api/picker-alerts/counters'))).coverage;
 f.database.exec("CREATE TRIGGER reject_same_clock_draw BEFORE INSERT ON picker_draws BEGIN SELECT RAISE(ABORT,'synthetic gap'); END");
 const failed=await f.call('/_internal/picker/draw','POST',draw('same-clock-gap'));
 assert.equal(failed.status,200);assert.equal((await data(failed)).coverage.available,false);
 f.database.exec('DROP TRIGGER reject_same_clock_draw');
 await f.call('/_internal/picker/draw','POST',draw('after-same-clock-gap'));
 const after=(await data(await f.call('/api/picker-alerts/counters'))).coverage;
 assert.equal(after.available,true);assert.equal(after.since,before.since);
 assert.notEqual(after.epoch,before.epoch);
 f.restart();
 const recreated=await data(await f.call('/api/picker-alerts/counters'));
 assert.deepEqual(recreated.coverage,after);assert.equal(recreated.consecutive_degraded,1);
});

test('picker control actual handler and cache produce adapter-compatible committed watermark',async()=>{
 const f=fixture();await configurePicker(f);
 try{
  const initial=await f.call('/api/picker-alerts/counters');
  assert.equal(initial.headers.get('cache-control'),'no-store');
  const before=await data(initial);assert.equal(before.coverage.available,true);
  for(let i=0;i<3;i++){
   assert.equal((await f.call('/_internal/picker/draw','POST',draw('adapter-real-draw-'+i))).status,200);
   const warm=await data(await f.call('/api/picker-alerts/counters'));
   assert.equal(warm.total_picks,i+1); // Cache must invalidate after each actual transaction.
  }
  const response=await f.call('/api/picker-alerts/counters');assert.equal(response.status,200);
  assert.equal(response.headers.get('cache-control'),'no-store');const body=await data(response);
  const script=pickerAdapterFilePath(new URL('../../scripts/hub_alert_evidence.py',import.meta.url));
  const python=`import importlib.util,json,sys
s=importlib.util.spec_from_file_location('sampler',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
e={'mode':'external','source_id':'a'*64,'remote':{'kind':'picker_control_watermark_v1','base_url':'https://synthetic.example','credential_path':'/synthetic/UNREAD.json','source_id':'b'*64,'release_sha':'c'*40}}
v=m.picker_control_observation(json.load(sys.stdin),e,observed_at_ms=int(sys.argv[2]));print(json.dumps(v))`;
  const result=pickerAdapterSpawn('python3',['-I','-S','-B','-c',python,script,String(Date.now())],{input:JSON.stringify(body),encoding:'utf8',timeout:5000,maxBuffer:16384});
  assert.equal(result.status,0,result.stderr);const observed=JSON.parse(result.stdout);
  assert.equal(observed.kind,'picker_control_watermark_observation_v1');assert.equal(observed.occurrence_count,1);
  assert.equal(observed.coverage_epoch,body.coverage.epoch);
  for(const invented of ['complete','nonce','started_total','finished_total','controller_id'])assert.equal(invented in observed,false);
 }finally{f.database.close();}
});

test('picker control handler unavailable coverage and actor refusal remain explicit',async()=>{
 const f=fixture();await configurePicker(f);
 try{
  const machine={id:'machine:synthetic',kind:'machine',resource_id:'synthetic',scopes:['session:report']};
  assert.equal((await f.call('/api/picker-alerts/counters','GET',undefined,machine)).status,403);
  (f.control as any).pickerObserverDirty=true;
  const unavailable=await data(await f.call('/api/picker-alerts/counters'));
  assert.equal(unavailable.coverage.available,false);assert.equal(unavailable.coverage.reason,'picker_observer_unavailable');
  assert.equal(unavailable.recipient,'dev@dtcdev.click');
 }finally{f.database.close();}
});
