import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ControlDO} from '../src/control-do';
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin'],family_id:'operator-family',client_id:'phone'};
function fixture(options:{schemaMode?:string}={}){const database=new DatabaseSync(':memory:');const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(q:string,...a:any[]){const s=database.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const v=fn();database.exec('COMMIT');return v;}catch(e){database.exec('ROLLBACK');throw e;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
 let control:ControlDO;
 const env:any={HUB2_SCHEMA_MIGRATIONS:options.schemaMode,
  CONTROL:{idFromName:(id:string)=>id,get:()=>({fetch:(input:string|Request,init?:RequestInit)=>control.fetch(input instanceof Request?input:new Request(input,init))})}};
 control=new ControlDO(state,env);
 const call=(p:string,m='GET',b?:unknown,a:unknown=admin)=>control.fetch(new Request('https://control.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));return{control,database,call,env,restart:()=>{control=new ControlDO(state,env);}};
}
const data=async(r:Response)=>await r.json() as any;
const workerSession=(over:Record<string,unknown>={})=>({id:'session:11111111-1111-4111-8111-111111111111',kind:'agent',scopes:['message:send','routing:read'],resource_id:'intake-1',family_id:'worker:11111111-1111-4111-8111-111111111111:1',client_id:'native-worker',...over});

test('quota evidence export: admin writes, any actor reads a stable below-threshold shape',async()=>{
 const {call,database}=fixture();await call('/api/model-policy');
 const now=Date.now();
 // An expired window is recorded but reads back stale and never below_threshold.
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.5,source:'grok-replica',captured_at:now-60000,valid_for_ms:1000})).status,200);
 const stale=await data(await call('/api/quota/grokbot'));
 assert.equal(stale.fresh,false);assert.equal(stale.state,'stale');assert.equal(stale.below_threshold,false);
 // Fresh evidence replaces it; any authenticated actor may read (machines and workers included).
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.7,source:'grok-replica',captured_at:now})).status,200);
 for(const reader of [admin,workerSession(),{id:'machine:desk',kind:'machine',resource_id:'desk',scopes:['machine:heartbeat']}]){
  const view=await data(await call('/api/quota/grokbot','GET',undefined,reader));
  assert.equal(view.account,'grokbot');assert.equal(view.provider,'grok');
  assert.equal(view.remaining_percent,0.7);assert.equal(view.threshold_percent,1);
  assert.equal(view.below_threshold,true);assert.equal(view.fresh,true);assert.equal(view.state,'exhausted');
  assert.equal(view.source,'grok-replica');assert(view.observed_at);
 }
 // Above threshold and custom threshold: below_threshold false.
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:4,threshold:1,source:'grok-replica',captured_at:now+10})).status,200);
 assert.equal((await data(await call('/api/quota/grokbot'))).below_threshold,false);
 assert.equal((await data(await call('/api/quota/grokbot'))).state,'fresh');
 // Replayed/older evidence is refused.
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:3,source:'grok-replica',captured_at:now-120000})).status,409);
 // Unknown account reports a stable unknown shape, never below_threshold.
 const unknown=await data(await call('/api/quota/never-reported'));
 assert.equal(unknown.state,'unknown');assert.equal(unknown.below_threshold,false);assert.equal(unknown.remaining_percent,null);
 // Validation and authorization.
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.7,captured_at:now})).status,400);
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:101,source:'x',captured_at:now})).status,400);
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.7,source:'x',captured_at:now,valid_for_ms:0})).status,400);
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.7,source:'x',captured_at:now},workerSession())).status,403);
 assert.equal((await call('/api/quota/grokbot','DELETE')).status,405);
 // Persistence survives a DO restart; schema is at version 8.
 assert.equal(database.prepare('SELECT version FROM hub2_schema').get()!.version,8);
});
test('unauthenticated quota reads are refused before any evidence is revealed',async()=>{
 const {control}=fixture();
 const response=await control.fetch(new Request('https://control.test/api/quota/grokbot',{method:'GET'}));
 assert.equal(response.status,401);
});
test('sticky claim: grant-gated worker claim sets forward_session and bumps the mailbox wake',async()=>{
 const {call,database}=fixture();await call('/api/model-policy');
 assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin',machine:'DESKTOP-FP4OP26'})).status,200);
 const pinCheck=await data(await call('/api/named-agents'));
 assert.equal(pinCheck.items[0].machine,'DESKTOP-FP4OP26');
 await call('/_routes/register','POST',{kind:'session',id:'11111111-1111-4111-8111-111111111111',owner:'DESKTOP-FP4OP26',resource_id:'org/a',bound_intake_id:'intake-1',participants:[]});
 // Without a grant the typed refusal carries the exact family to grant.
 const refused=await call('/api/named-agents/orin/claim','POST',{},workerSession());
 assert.equal(refused.status,403);
 const refusal=await data(refused);assert.equal(refusal.error,'forward_grant_required');
 assert.equal(refusal.family_id,workerSession().family_id);
 // Non-worker actors can never claim.
 assert.equal((await call('/api/named-agents/orin/claim','POST',{},admin)).status,403);
 assert.equal((await call('/api/named-agents/orin/claim','POST',{},workerSession({id:'human:ops',kind:'operator',resource_id:'human:ops'}))).status,403);
 assert.equal((await call('/api/named-agents/missing/claim','POST',{},workerSession())).status,404);
 // Grant the family: claim succeeds, pins forward and wakes pending mail.
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{family_id:workerSession().family_id})).status,200);
 const claimed=await data(await call('/api/named-agents/orin/claim','POST',{},workerSession()));
 assert.equal(claimed.agent.forward_session,'11111111-1111-4111-8111-111111111111');
 assert.equal(claimed.agent.machine,'DESKTOP-FP4OP26');
 assert.deepEqual(await data(await call('/_recipient','POST',{recipient:'agent:orin'})),{mailbox:'agent:orin',forward_session:'11111111-1111-4111-8111-111111111111'});
 assert(database.prepare('SELECT 1 FROM mailbox_wakes WHERE owner=?').get('agent:orin'));
 // A session on another machine is refused by the pin (after its family is granted).
 const otherFamily='worker:22222222-2222-4222-8222-222222222222:1';
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{family_id:otherFamily})).status,200);
 await call('/_routes/register','POST',{kind:'session',id:'22222222-2222-4222-8222-222222222222',owner:'hetzner-aplexer',resource_id:'org/a',participants:[]});
 const wrong=await data(await call('/api/named-agents/orin/claim','POST',{},workerSession({id:'session:22222222-2222-4222-8222-222222222222',family_id:otherFamily})));
 assert.equal(wrong.error,'sticky_machine_mismatch');assert.equal(wrong.required_machine,'DESKTOP-FP4OP26');
 // Pin override: a session matching the new pin claims successfully.
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{machine:'hetzner-aplexer'})).status,200);
 assert.equal((await call('/api/named-agents/orin/claim','POST',{},workerSession({id:'session:22222222-2222-4222-8222-222222222222',family_id:otherFamily}))).status,200);
 // Revocation restores the gate; unsetting the pin clears it.
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{family_id:'worker:22222222-2222-4222-8222-222222222222:1',revoke:true})).status,200);
 assert.equal((await call('/api/named-agents/orin/claim','POST',{},workerSession({id:'session:22222222-2222-4222-8222-222222222222',family_id:'worker:22222222-2222-4222-8222-222222222222:1'}))).status,403);
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{machine:null})).status,200);
 const cleared=await data(await call('/api/named-agents'));
 assert.equal(cleared.items[0].machine,null);
 // Validation: sticky config needs at least one field.
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{})).status,400);
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{machine:'bad machine!'})).status,400);
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{machine:'x'},workerSession())).status,403);
});
test('schema defer mode keeps the new surfaces typed as deferred',async()=>{
 const {call}=fixture({schemaMode:'defer'});await call('/api/model-policy');
 assert.equal((await call('/api/quota/grokbot','GET')).status,503);
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:1,source:'x',captured_at:Date.now()})).status,503);
 assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin',machine:'m1'})).status,503);
 assert.equal((await call('/api/named-agents/orin/claim','POST',{},workerSession())).status,503);
 assert.equal((await call('/api/named-agents/orin/sticky','POST',{machine:'m1'})).status,503);
 // Pre-existing behavior continues on the bridged schema.
 assert.equal((await call('/api/named-agents','POST',{name:'orin',display_name:'Orin'})).status,200);
});
test('worker forwards quota and sticky routes to Control with real authentication',async t=>{
 const f=fixture();t.after(async()=>f.database.close());
 const {default:worker}=await import('../src/worker');
 const token='synthetic-quota-operator-'+'x'.repeat(32);
 const env={...f.env,HUB2_DEV_OPERATOR_TOKEN:token};
 const call=(path:string,method='GET',body?:unknown)=>worker.fetch(new Request('http://127.0.0.1'+path,{method,headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},...(body===undefined?{}:{body:JSON.stringify(body)})}),env);
 assert.equal((await call('/api/quota/grokbot','PUT',{remaining:0.7,source:'grok-replica',captured_at:Date.now()})).status,200);
 const view=await data(await call('/api/quota/grokbot'));assert.equal(view.below_threshold,true);
 // Forwarded sticky claim reaches Control: a non-worker caller gets Control's
 // typed worker_session_required refusal, not a worker-side 404.
 const claim=await call('/api/named-agents/missing/claim','POST');
 assert.equal(claim.status,403);
 const claimBody=await data(claim);assert.equal(claimBody.error,'worker_session_required');
});
