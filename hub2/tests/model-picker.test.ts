import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pickModelRoute, weightedChoice } from '../src/model-picker.ts';

const candidates=[
 {id:'codex',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'claude',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'luna',engine:'codex',model:'gpt-6-luna',effort:'max',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'sol-high',engine:'codex',model:'gpt-6.1-sol',effort:'max',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'astra-high',engine:'codex',model:'gpt-6-astra',effort:'max',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'argon-high',engine:'gemini',model:'gemini-4-argon',effort:'max',account_id:'gemini',weight:100,capabilities:[],enabled:true},
 {id:'luna-medium',engine:'codex',model:'gpt-6-luna',effort:'medium',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'zai-central',engine:'zcodex',model:'glm-flash',account_id:'zai-central',weight:100,capabilities:[],enabled:true},
 {id:'haiku',engine:'claude',model:'haiku',effort:'medium',account_id:'claude',weight:100,capabilities:[],enabled:true},
 {id:'claude-sonnet-high',engine:'claude',model:'sonnet',effort:'max',account_id:'claude',weight:100,capabilities:[],enabled:true},
 {id:'claude-fable-5-2',engine:'claude',model:'fable',effort:'medium',account_id:'claude',weight:100,capabilities:[],enabled:true},
];

function envFor(quota:Record<string,number>,credentialUsable=false){
 const requests:string[]=[];
 const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(url:string)=>{
  const parsed=new URL(url);requests.push(kind+':'+parsed.pathname+parsed.search);
  if(kind==='control'&&parsed.pathname==='/_routing-policy')return Response.json({version:1,candidates});
  if(kind==='control'&&parsed.pathname==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});
  if(kind==='accounts'){
   return Response.json({account:{enabled:true},quota:{remaining:quota[id]??0},hosts:['h1'],credential_usable:credentialUsable});
  }
  if(kind==='machines')return Response.json({machine_id:'h1',connected:true,engines:['codex','claude','zcodex']});
  return Response.json({}, {status:404});
 }})});
 return {env:{CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines')} as any,requests};
}

test('weighted choice follows quota headroom and can select a non-default engine',async()=>{
 const {env,requests}=envFor({codex:10,claude:90},false);
 const route=await pickModelRoute(env,{interactive:true},()=>0.5);
 assert.equal(route.policy_id,'claude');
 assert.equal(route.engine,'claude');
 assert.equal(route.effort,'medium');
 assert.ok(requests.some(value=>value.includes('/_routing-policy')));
});

test('unhealthy credentials and missing quota do not become placement refusals',async()=>{
 const {env}=envFor({},false);
 const route=await pickModelRoute(env,{interactive:true,model:'gpt-6.1-sol',override_reason:'test explicit model'},()=>0);
 assert.equal(route.policy_id,'codex');
 assert.equal(route.quota_remaining,0);
});

test('Fable is excluded from automatic picks and available only when explicitly pinned',async()=>{
 const {env}=envFor({codex:10,claude:90},false);
 const automatic=await pickModelRoute(env,{interactive:true},()=>0.999999);
 assert.notEqual(automatic.policy_id,'claude-fable-5-2');
 const explicit=await pickModelRoute(env,{interactive:true,model:'claude-fable-5-2',override_reason:'Explicit request for Claude Fable 5.2'},()=>0);
 assert.equal(explicit.policy_id,'claude-fable-5-2');
 assert.equal(explicit.model,'fable');
 assert.equal(explicit.effort,'medium');
});

test('fallback can exclude failed candidates while enforcing model policy',async()=>{
 const {env}=envFor({codex:10,claude:90},false);
 const route=await pickModelRoute(env,{interactive:true,model:'gpt-6.1-sol',override_reason:'test explicit model',exclude_policy_ids:['claude']},()=>0);
 assert.equal(route.policy_id,'codex');
 assert.equal(route.model,'gpt-6.1-sol');
 assert.notEqual(route.policy_id,'zai-central');
 assert.notEqual(route.policy_id,'haiku');
 assert.notEqual(route.policy_id,'sol-high');
 assert.notEqual(route.policy_id,'astra-high');
 assert.notEqual(route.policy_id,'argon-high');
 assert.notEqual(route.policy_id,'luna-medium');
 assert.notEqual(route.policy_id,'claude-sonnet-high');
});

test('weightedChoice honors weights deterministically',()=>{
 assert.equal(weightedChoice([{value:'small',weight:1},{value:'large',weight:3}],()=>0.9),'large');
});

// Production-shaped pool (config/model-policy.json): per-account credential state and quota.
const pool=[
 {id:'codex-luna-max',engine:'codex',model:'gpt-6-luna',effort:'max',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'codex-sol-medium',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'codex-astra-medium',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'claude-sonnet-medium',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'claude-opus-medium',engine:'claude',model:'opus',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'go-muse',engine:'go',model:'opencode-go/muse-spark-1.3-contributor',account_id:'shared-go',weight:1,capabilities:[],enabled:true},
 {id:'zai-glm-flash',engine:'zai',model:'glm-5.3-flash',account_id:'zai-central',weight:1,capabilities:[],enabled:true},
 {id:'gemini-argon-medium',engine:'antigravity',model:'gemini-4-argon-medium',account_id:'gemini',weight:1,capabilities:[],enabled:true},
];
function poolEnv(usable:Record<string,boolean>,quota:Record<string,number>,engines=['claude','codex','go','zai']){
 const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(url:string)=>{
  const parsed=new URL(url);
  if(kind==='control'&&parsed.pathname==='/_routing-policy')return Response.json({version:3,candidates:pool});
  if(kind==='control')return Response.json({items:[{id:'h1'}],next:null});
  if(kind==='accounts')return Response.json({account:{enabled:true},quota:id in quota?{remaining:quota[id]}:null,hosts:['h1'],credential_usable:usable[id]===true,credential_status:usable[id]?'ready':'expired'});
  return Response.json({machine_id:'h1',connected:true,engines});
 }})});
 return {CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines')} as any;
}
const healthy={codex:true,claude:true,'shared-go':true,'zai-central':true,gemini:true};
const left={codex:95,claude:87,'shared-go':40,'zai-central':73,gemini:90};

test('pool spreads across engines, keeps astra/unadvertised engines out and caps luna',async()=>{
 const env=poolEnv(healthy,left);
 const seen=new Map<string,number>();
 for(let i=0;i<200;i++){
  const route=await pickModelRoute(env,{interactive:false},()=>(i+0.5)/200);
  seen.set(route.policy_id,(seen.get(route.policy_id)||0)+1);
 }
 assert.ok(seen.size>=5,[...seen.keys()].join());
 assert.ok(!seen.has('codex-astra-medium'));
 assert.ok(!seen.has('gemini-argon-medium'),'engine no host advertises is not picked');
 assert.ok((seen.get('codex-luna-max')||0)<=(seen.get('codex-sol-medium')||0)*1.2+1,'luna never dominates');
});

test('expired credentials are excluded from every pick',async()=>{
 const env=poolEnv({...healthy,claude:false},left);
 for(let i=0;i<50;i++){
  const route=await pickModelRoute(env,{interactive:false},()=>(i+0.5)/50);
  assert.notEqual(route.engine,'claude');
 }
});

test('deep tier biases toward opus and sol; astra needs an explicit model and reason',async()=>{
 const env=poolEnv(healthy,left);
 const count=async(tier?:string)=>{let n=0;for(let i=0;i<300;i++){const r=await pickModelRoute(env,{interactive:false,...(tier?{tier}:{})},()=>(i+0.5)/300);if(r.model==='opus'||r.model==='gpt-6.1-sol')n++;}return n;};
 assert.ok(await count('deep')>await count());
 const astra=await pickModelRoute(env,{interactive:false,model:'gpt-6-astra',override_reason:'operator asked for astra'},()=>0);
 assert.equal(astra.policy_id,'codex-astra-medium');
});

test('detailed pick reports the pool shares and credential exclusions',async()=>{
 const {pickModelRouteDetailed}=await import('../src/model-picker.ts');
 const {diagnostics}=await pickModelRouteDetailed(poolEnv({...healthy,claude:false},left),{interactive:false},()=>0.5);
 assert.deepEqual(diagnostics.excluded.map(row=>row.policy_id).sort(),['claude-opus-medium','claude-sonnet-medium']);
 assert.ok(Math.abs(diagnostics.pool.reduce((sum,row)=>sum+row.share,0)-1)<1e-9);
});


test('disconnected hosts are a typed refusal, including explicit host requests',async()=>{
 const {env}=envFor({codex:90},true);
 env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({machine_id:'h1',connected:false,last_seen:Date.now()-120_000,engines:['codex']})})};
 await assert.rejects(pickModelRoute(env,{interactive:false}), (error:any)=>error.code==='no_connected_host'&&error.status===503&&JSON.parse(error.detail).machines[0].age_ms>=120_000);
 await assert.rejects(pickModelRoute(env,{interactive:false,machine_id:'h1'}), (error:any)=>error.code==='no_connected_host');
});

test('an explicit connected route does not require an override explanation',async()=>{
 const {env}=envFor({codex:90},true);
 const route=await pickModelRoute(env,{interactive:false,model:'gpt-6.1-sol'},()=>0);
 assert.equal(route.model,'gpt-6.1-sol');assert.equal(route.override_reason,'explicit_route');
});
