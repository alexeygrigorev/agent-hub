import {build} from 'esbuild';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const bundle=await build({entryPoints:['hub2/src/resource-manager.ts'],bundle:true,write:false,format:'esm',platform:'neutral'});
const {admitLaunch}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function setup(){
 const policy={version:7,candidates:[{id:'configured-model',engine:'engine',model:'actual-model',account_id:'account',weight:2,capabilities:[],enabled:true}]};
 const machine={machine_id:'machine',available:true,capacity:1,occupied:0,engines:['engine'],capabilities:['headless']};
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
test('stale host resource snapshot holds admission, absent snapshot stays backward compatible',async()=>{
 const f=setup();f.machine.resources={fresh:false,observed_at:Date.now()};
 let outcome=await admitLaunch(f.env,input);
 assert.equal(outcome.ok,false);
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_stale');
 const g=setup();g.machine.resources={fresh:true,observed_at:Date.now()-500_000};
 outcome=await admitLaunch(g.env,input);
 assert.equal(outcome.ok,false);
 if(!outcome.ok)assert.equal(outcome.code,'host_resources_stale');
 const h=setup();
 assert.equal((await admitLaunch(h.env,input)).ok,true,'machines without a resources block admit exactly as before');
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
