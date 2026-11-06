import {build} from 'esbuild';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const bundle=await build({entryPoints:['hub2/src/resource-manager.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const {admitLaunch,advanceDispatch,releaseAccountCapacity}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function setup(){
 const policy={version:7,candidates:[{id:'configured-model',engine:'engine',model:'actual-model',account_id:'account',weight:2,capabilities:[],enabled:true}]};
 const machine={machine_id:'machine',available:true,capacity:1,occupied:0,engines:['engine'],capabilities:['headless'],resources:{mem_available_mb:42000,cpu_load_pct:12.5,observed_at:Date.now()}};
 const account={account:{enabled:true,capacity:2,occupied:0,min_remaining:10},quota:{remaining:70,valid_until:Date.now()+300000,source:'provider-observed'},fresh:true,hosts:['machine']};
 const calls=[];
 const ns=(kind,fn)=>({idFromName:id=>id,get:id=>({fetch:async url=>{calls.push([kind,id,new URL(url).pathname]);return Response.json(fn(new URL(url)));}})});
 const env={CONTROL:ns('control',url=>url.pathname==='/_routing-policy'?policy:{items:[{id:'machine'}],next:null}),MACHINES:ns('machine',()=>machine),ACCOUNTS:ns('account',()=>account)};
 return {env,policy,machine,account,calls};
}
const input={operation_id:'stable-operation',interactive:false};
test('fresh quota selects account/model/machine plan deterministically',async()=>{
 const f=setup(),a=await admitLaunch(f.env,input),b=await admitLaunch(f.env,input);
 assert.equal(a.ok,true);assert.equal(b.ok,true);
 if(!a.ok||!b.ok)return;
 assert.equal(a.plan.model,'actual-model');assert.equal(a.plan.account_id,'account');assert.equal(a.plan.machine_id,b.plan.machine_id);
 assert.equal(a.plan.policy_version,7);assert.equal(a.plan.weight,1.2);assert.equal(a.plan.quota_remaining,70);
 assert.deepEqual(f.calls.slice(0,4).map(v=>v[2]),['/_routing-policy','/api/directory','/_internal/routing','/_internal/routing']);
});
test('unknown, stale, exhausted and saturated quota become typed holds, never fallback capacity',async()=>{
 const cases=[
  {change:(f:any)=>{f.account.quota=null;},code:'quota_unknown'},
  {change:(f:any)=>{f.account.fresh=false;},code:'quota_stale'},
  {change:(f:any)=>{f.account.quota.valid_until=Date.now()-1;},code:'quota_stale'},
  {change:(f:any)=>{f.account.quota.remaining=10;},code:'quota_exhausted'},
  {change:(f:any)=>{f.account.quota.remaining=Number.NaN;},code:'quota_unknown'},
  {change:(f:any)=>{f.account.account.occupied=2;},code:'account_saturated'},
  {change:(f:any)=>{f.account.account.enabled=false;},code:'quota_unknown'}];
 for(const {change,code} of cases){
  const f=setup();change(f);
  const outcome=await admitLaunch(f.env,input);
  assert.equal(outcome.ok,false,code);
  if(outcome.ok)continue;
  assert.equal(outcome.code,code,code);
  assert.equal(typeof outcome.hold.retry_at==='number'||outcome.hold.retry_at===null,true);
 }
});
test('exhausted quota holds until the known reset instead of the refresh backoff',async()=>{
 const f=setup(),reset=Date.now()+90*60_000;f.account.quota.remaining=5;f.account.quota.reset_at=reset;
 const outcome=await admitLaunch(f.env,input);
 assert.equal(outcome.ok,false);
 if(!outcome.ok&&outcome.code==='quota_exhausted')assert.equal(outcome.hold.retry_at,reset);
});
test('stale host resource snapshot holds admission, missing snapshot fails closed',async()=>{
 const f=setup();f.machine.resources={mem_available_mb:42000,observed_at:Date.now()-500_000};
 let outcome=await admitLaunch(f.env,input);
 assert.equal(outcome.ok,false);
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_stale');
 const g=setup();g.machine.resources={mem_available_mb:42000,observed_at:Date.now()};
 assert.equal((await admitLaunch(g.env,input)).ok,true,'a fresh positive snapshot admits');
 const m=setup();delete m.machine.resources;
 outcome=await admitLaunch(m.env,input);
 assert.equal(outcome.ok,false,'a machine without any snapshot must fail closed');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_unknown');
 const z=setup();z.machine.resources={mem_available_mb:0,observed_at:Date.now()};
 outcome=await admitLaunch(z.env,input);
 assert.equal(outcome.ok,false,'a non-positive snapshot is unknown, never capacity');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_unknown');
 const fut=setup();fut.machine.resources={mem_available_mb:42000,observed_at:Date.now()+60_000};
 outcome=await admitLaunch(fut.env,input);
 assert.equal(outcome.ok,false,'a future-dated snapshot is unverified, never trusted');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_stale');
 const skew=setup();skew.machine.resources={mem_available_mb:42000,observed_at:Date.now()+1_000};
 assert.equal((await admitLaunch(skew.env,input)).ok,true,'sub-tolerance clock skew admits');
 const bad=setup();bad.machine.resources={mem_available_mb:42000,observed_at:Number.NaN};
 outcome=await admitLaunch(bad.env,input);
 assert.equal(outcome.ok,false,'a malformed timestamp is unknown');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_unknown');
 const cpu=setup();cpu.machine.resources={mem_available_mb:42000,cpu_load_pct:96,observed_at:Date.now()};
 outcome=await admitLaunch(cpu.env,input);
 assert.equal(outcome.ok,false,'a saturated CPU refuses admission');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_saturated');
 const invalid=setup();invalid.machine.resources={mem_available_mb:42000,cpu_load_pct:180,observed_at:Date.now()};
 outcome=await admitLaunch(invalid.env,input);
 assert.equal(outcome.ok,false,'an out-of-range CPU reading is unknown');
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_unknown');
 const broken=setup();broken.machine.capacity=0;
 outcome=await admitLaunch(broken.env,input);
 assert.equal(outcome.ok,false,'a degenerate capacity field is machine_unavailable, never a division artifact');
 if(!outcome.ok)assert.equal(outcome.code,'machine_unavailable');
});
test('300-second snapshot TTL admits at 299 seconds and refuses after expiry',async()=>{
 const fresh=setup();fresh.machine.resources={mem_available_mb:42000,observed_at:Date.now()-299_000};
 assert.equal((await admitLaunch(fresh.env,input)).ok,true,'freshness margin admits');
 const stale=setup();stale.machine.resources={mem_available_mb:42000,observed_at:Date.now()-301_000};
 const outcome=await admitLaunch(stale.env,input);
 assert.equal(outcome.ok,false);if(!outcome.ok)assert.equal(outcome.code,'host_resources_stale');
});
test('goal preflight and override reason cannot bypass real capabilities or host approval',async()=>{
 let f=setup();
 await assert.rejects(admitLaunch(f.env,{...input,with_goal:true}),e=>e.code==='goal_requires_interactive');
 const goalRefused=await admitLaunch(f.env,{...input,interactive:true,with_goal:true});
 assert.equal(goalRefused.ok,false);
 if(!goalRefused.ok)assert.equal(goalRefused.code,'capability_unavailable');
 await assert.rejects(admitLaunch(f.env,{...input,model:'actual-model'}),e=>e.code==='override_reason_required');
 const selected=await admitLaunch(f.env,{...input,model:'actual-model',override_reason:'reproduce model-specific failure'});
 assert.equal(selected.ok,true);
 if(selected.ok)assert.equal(selected.plan.override_reason,'reproduce model-specific failure');
 f=setup();f.account.hosts=[];
 const refused=await admitLaunch(f.env,input);
 assert.equal(refused.ok,false);
 if(!refused.ok)assert.equal(refused.code,'machine_unavailable');
});
test('weighted quota distribution changes from observed headroom',async()=>{
 const f=setup();f.policy.candidates.push({...f.policy.candidates[0],id:'second',model:'second-model',weight:6});
 let first=0;
 for(let i=0;i<200;i++){const outcome=await admitLaunch(f.env,{...input,operation_id:String(i)});if(outcome.ok&&outcome.plan.model==='actual-model')first++;}
 assert.ok(first>25&&first<80,`configured 1:3 weights produced ${first}/200 first choices`);
});
test('machine-unavailable input returns a typed hold instead of a bare error',async()=>{
 const f=setup();f.policy.candidates[0].engine='absent-engine';
 const outcome=await admitLaunch(f.env,input);
 assert.equal(outcome.ok,false);
 if(!outcome.ok)assert.equal(outcome.code,'capability_unavailable');
});

test('ResourceManager orchestrates machine reserve, account confirm and offer through the authoritative owners',async()=>{
 const f=setup();
 const calls:string[]=[];const bodies:any[]=[];
 const machineFetch=async(url:any,init?:RequestInit)=>{calls.push(new URL(url).pathname);bodies.push(init?.body?JSON.parse(String(init.body)):null);
  const p=new URL(url).pathname;
  if(p.endsWith('/reserve'))return new Response(JSON.stringify({state:'staged',epoch:4}),{status:201});
  if(p.endsWith('/activate'))return new Response(JSON.stringify({state:'offered'}));
  return new Response('{}');};
 const accountFetch=async(url:any,init?:RequestInit)=>{calls.push(new URL(url).pathname);bodies.push(init?.body?JSON.parse(String(init.body)):null);
  const p=new URL(url).pathname;
  if(p.endsWith('/reserve'))return new Response(JSON.stringify({reservation:{state:'offered'}}),{status:200});
  return new Response(JSON.stringify({reservation:{state:'confirmed',epoch:4}}));};
 const env:any={...f.env,MACHINES:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:machineFetch})},ACCOUNTS:{idFromName:(id:string)=>id,get:(id:string)=>({fetch:accountFetch})}};
 const intent={state:'pending_machine' as const,operation_id:'op-1',attempt_operation_id:'op-1:0',dispatch_id:'op-1',attempt:0,
  session_id:'s1',machine_id:'machine',account_id:'account',epoch:null,request_digest:'d',project_id:'org/a',intake_id:'i1',
  title:'t',acceptance_criteria:['a'],launch:{engine:'engine'}};
 const staged=await advanceDispatch(env,intent);
 assert.equal(staged.ok,true);
 if(!staged.ok||staged.state!=='staged')return assert.fail('expected staged');
 assert.equal(staged.epoch,4);
 assert.equal(calls[0],'/_internal/reserve');
 assert.equal(bodies[0].dispatch_id,'op-1');assert.equal(bodies[0].account_id,'account');assert.deepEqual(bodies[0].launch,{engine:'engine'});
 const confirmed=await advanceDispatch(env,{...intent,state:'staged',epoch:4});
 assert.equal(confirmed.ok,true);assert.ok(confirmed.ok&&confirmed.state==='account_confirmed');
 const reserveBody=bodies.find((b)=>b&&b.ttl_ms===600000);
 assert.equal(reserveBody.epoch,4);assert.equal(reserveBody.operation_id,'op-1:0');
 const confirmBody=bodies.filter((b)=>b&&b.epoch===4&&b.operation_id==='op-1:0').length;
 assert.ok(confirmBody>=2,'reserve and confirm both target the attempt operation');
 const offered=await advanceDispatch(env,{...intent,state:'account_confirmed',epoch:4});
 assert.equal(offered.ok,true);assert.ok(offered.ok&&offered.state==='offered');
 assert.equal(calls[calls.length-1],'/_internal/activate');
});
test('orchestration refusals carry typed reasons and account release requires qualified proof echo',async()=>{
 const f=setup();
 const env:any={...f.env,
  MACHINES:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>new Response(JSON.stringify({reason:'machine_capacity'}),{status:409})})},
  ACCOUNTS:{idFromName:(id:string)=>id,get:()=>({fetch:async()=>new Response('{}')})}};
 const refused=await advanceDispatch(env,{state:'pending_machine',operation_id:'op',attempt_operation_id:'op:0',dispatch_id:'op',attempt:0,
  session_id:'s',machine_id:'machine',account_id:'account',epoch:null,request_digest:'d',project_id:'p',intake_id:'i',title:'t',acceptance_criteria:[],launch:{}});
 assert.equal(refused.ok,false);
 if(!refused.ok)assert.equal(refused.reason,'machine_capacity');
 let releaseBody:any=null;
 const releaseAccount={fetch:async(input:string|Request,init?:RequestInit)=>{
   const request=input instanceof Request?input:new Request(input,init);
   releaseBody=await request.json();
   // Model the authoritative owner: release only against qualified proof.
   if(releaseBody.native_exit_confirmed!==true)return new Response(JSON.stringify({error:'unqualified_release_proof'}),{status:409});
   return new Response(JSON.stringify({reservation:{operation_id:'op:0',session_id:'s',machine_id:'m',epoch:3,state:'released'}}));}};
 const budgetMetrics=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
 const unitMap=Object.fromEntries(budgetMetrics.map((key)=>[key,key==='do_requests'?64:10]));
 const envelopes=JSON.stringify({version:1,...Object.fromEntries(['normal','safety'].map((kind)=>[kind,{units:unitMap,max_do_calls:16,max_wall_ms:60000,provenance:'synthetic-release-envelope'}]))});
 const renv:any={CONTROL:f.env.CONTROL,MACHINES:f.env.MACHINES,PROJECTS:f.env.PROJECTS,MAILBOXES:f.env.MAILBOXES,
  BUDGET:{idFromName:(id:string)=>id,get:()=>({fetch:async(input:string|Request,init?:RequestInit)=>{
   void (input instanceof Request?input:new Request(input,init));
   return new Response(JSON.stringify({allowed:true}));}})},
  ACCOUNTS:{idFromName:(id:string)=>id,get:()=>releaseAccount},HUB2_BUDGET_ENVELOPES:envelopes};
 const release=await releaseAccountCapacity(renv,{account_id:'account',attempt_operation_id:'op:0',session_id:'s',machine_id:'m',epoch:3},{native_exit_confirmed:true});
 assert.equal(release.released,true);
 assert.deepEqual(releaseBody,{operation_id:'op:0',session_id:'s',machine_id:'m',epoch:3,native_exit_confirmed:true});
 const mismatch=await releaseAccountCapacity(renv,{account_id:'account',attempt_operation_id:'op:0',session_id:'s',machine_id:'m',epoch:3},{unclaimed_proof:{x:1}});
 assert.equal(mismatch.released,false,'identity mismatch must not count as released');
});
