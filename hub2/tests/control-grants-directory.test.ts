import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ControlDO} from '../src/control-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin'],family_id:'operator-family',client_id:'phone'};
function fixture(){const database=new DatabaseSync(':memory:'),snapshots=new Map<string,unknown[]>();const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(q:string,...a:any[]){const s=database.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const v=fn();database.exec('COMMIT');return v;}catch(e){database.exec('ROLLBACK');throw e;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 const env:any={MACHINES:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:async()=>Response.json({sessions:snapshots.get(id)||[]})})}};
 const control=new ControlDO(state,env);
 const call=(p:string,m='GET',b?:unknown,a:unknown=admin)=>control.fetch(new Request('https://control.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{control,database,call,snapshots};}
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
