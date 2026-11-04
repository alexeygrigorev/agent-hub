import {build} from 'esbuild';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const bundle=await build({entryPoints:['hub2/src/routing.ts'],bundle:true,write:false,format:'esm',platform:'neutral'});
const {selectRoute}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
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
test('actual configured policy and fresh quota select route, without default model',async()=>{
 const f=setup(),a=await selectRoute(f.env,input),b=await selectRoute(f.env,input);
 assert.equal(a.model,'actual-model');assert.equal(a.account_id,'account');assert.equal(a.machine_id,b.machine_id);assert.equal(a.policy_version,7);assert.equal(a.weight,1.2);
 assert.deepEqual(f.calls.slice(0,4).map(v=>v[2]),['/_routing-policy','/api/directory','/_internal/routing','/_internal/routing']);
});
test('unknown, stale, exhausted and occupied quota never become fallback capacity',async()=>{
 for(const change of [f=>f.account.quota=null,f=>f.account.fresh=false,f=>f.account.quota.remaining=10,f=>f.account.account.occupied=2]){
  const f=setup();change(f);await assert.rejects(selectRoute(f.env,input),e=>e.code==='quota_unavailable');
 }
});
test('goal preflight and override reason cannot bypass real capabilities or host approval',async()=>{
 let f=setup();await assert.rejects(selectRoute(f.env,{...input,with_goal:true}),e=>e.code==='goal_requires_interactive');
 await assert.rejects(selectRoute(f.env,{...input,interactive:true,with_goal:true}),e=>e.code==='capability_unavailable');
 await assert.rejects(selectRoute(f.env,{...input,model:'actual-model'}),e=>e.code==='override_reason_required');
 const selected=await selectRoute(f.env,{...input,model:'actual-model',override_reason:'reproduce model-specific failure'});assert.equal(selected.override_reason,'reproduce model-specific failure');
 const alias=await selectRoute(f.env,{...input,model:'configured-model',override_reason:'use configured human route alias'});assert.equal(alias.model,'actual-model');assert.equal(alias.policy_id,'configured-model');
 f=setup();f.account.hosts=[];await assert.rejects(selectRoute(f.env,input),e=>e.code==='machine_unavailable');
});
test('weighted quota distribution changes from observed headroom',async()=>{
 const f=setup();f.policy.candidates.push({...f.policy.candidates[0],id:'second',model:'second-model',weight:6});
 let first=0;
 for(let i=0;i<200;i++)if((await selectRoute(f.env,{...input,operation_id:String(i)})).model==='actual-model')first++;
 assert.ok(first>25&&first<80,`configured 1:3 weights produced ${first}/200 first choices`);
});
