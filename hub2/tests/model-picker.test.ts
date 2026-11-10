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
 {id:'codex-astra-medium',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex',weight:1,capabilities:['review-only'],enabled:true},
 {id:'claude-sonnet-medium',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'claude-opus-medium',engine:'claude',model:'opus',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'go-muse',engine:'go',model:'opencode-go/muse-spark-1.3-contributor',account_id:'shared-go',weight:1,capabilities:[],enabled:true},
 {id:'zai-glm-flash',engine:'zai',model:'glm-5.3-flash',account_id:'zai-central',weight:1,capabilities:[],enabled:true},
 {id:'gemini-argon-medium',engine:'antigravity',model:'gemini-4-argon-medium',account_id:'gemini',weight:1,capabilities:[],enabled:true},
];
function poolEnv(usable:Record<string,boolean>,quota:Record<string,number>,engines=['claude','codex','go','zai'],policy=pool){
 const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(url:string)=>{
  const parsed=new URL(url);
  if(kind==='control'&&parsed.pathname==='/_routing-policy')return Response.json({version:3,candidates:policy});
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

test('deep tier biases toward opus and sol; astra needs an explicit review purpose',async()=>{
 const env=poolEnv(healthy,left);
 const count=async(tier?:string)=>{let n=0;for(let i=0;i<300;i++){const r=await pickModelRoute(env,{interactive:false,...(tier?{tier}:{})},()=>(i+0.5)/300);if(r.model==='opus'||r.model==='gpt-6.1-sol')n++;}return n;};
 assert.ok(await count('deep')>await count());
 const astra=await pickModelRoute(env,{interactive:false,model:'gpt-6-astra',purpose:'review',override_reason:'operator asked for astra'},()=>0);
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

test('10000 seeded implementation draws never select Astra, including policy-id aliases',async()=>{
 const injected=[...pool,
  {id:'large-review-route',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex',weight:1000,capabilities:['review-only'],enabled:true},
  {id:'ASTRA-alias',engine:'codex',model:'synthetic-review',effort:'medium',account_id:'codex',weight:1000,capabilities:['review-only'],enabled:true},
 ];
 const env=poolEnv(healthy,left,undefined,injected);
 let seed=0x4a713;
 const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/0x100000000;};
 for(let i=0;i<10000;i++){
  const purpose=i%3===0?undefined:i%3===1?'fix':'implement';
  const route=await pickModelRoute(env,{interactive:false,purpose},random);
  assert.doesNotMatch(route.policy_id+' '+route.model,/astra/i);
  assert.notEqual(route.policy_id,'large-review-route');
 }
});

test('explicit Astra model or policy aliases reject implementation and allow review/think',async()=>{
 const injected=[...pool,{id:'review-route',engine:'codex',model:'gpt-6-astra',effort:'medium',account_id:'codex',weight:1000,capabilities:['review-only'],enabled:true}];
 const env=poolEnv(healthy,left,undefined,injected);
 for(const model of ['gpt-6-astra','codex-astra-medium','review-route']){
  for(const purpose of [undefined,'fix','implement'] as const){
   await assert.rejects(pickModelRoute(env,{interactive:false,model,purpose,override_reason:'explicit test'}),
    (e:any)=>e.code==='astra_not_for_implementation'&&e.status===400);
  }
  for(const purpose of ['review','think'] as const){
   const route=await pickModelRoute(env,{interactive:false,model,purpose,override_reason:'explicit test'},()=>0);
   assert.match(route.model,/astra/i);
  }
 }
});

test('invalid purposes fail with typed errors rather than weakening Astra protection',async()=>{
 const env=poolEnv(healthy,left);
 for(const purpose of [null,'Review','',{},'fix '] as any[])
  await assert.rejects(pickModelRoute(env,{interactive:false,purpose}), (e:any)=>e.code==='invalid_purpose'&&e.status===400);
});

function fleetEnv(hosts:Record<string,{os?:string;connected?:boolean}>){
 const policy=[{id:'codex',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true}];
 const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(url:string)=>{
  const parsed=new URL(url);
  if(kind==='control'&&parsed.pathname==='/_routing-policy')return Response.json({version:1,candidates:policy});
  if(kind==='control'&&parsed.pathname==='/api/directory')return Response.json({items:Object.keys(hosts).map(id=>({id})),next:null});
  if(kind==='accounts')return Response.json({account:{enabled:true},quota:{remaining:80},hosts:[],credential_usable:true});
  if(kind==='machines')return Response.json({machine_id:id,connected:hosts[id]?.connected??true,engines:['codex'],...(hosts[id]?.os?{os:hosts[id].os}:{})});
  return Response.json({}, {status:404});
 }})});
 return {CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines')} as any;
}

test('an absolute workspace path is placed only on hosts of its path family',async()=>{
 const env=fleetEnv({'win-desktop':{os:'windows'},'linux-box':{os:'linux'}});
 for(const draw of [0,0.25,0.5,0.75,0.999999]){
  assert.equal((await pickModelRoute(env,{interactive:false,workspace:'/home/user/work/2026-11-10-task'},()=>draw)).machine_id,'linux-box');
  assert.equal((await pickModelRoute(env,{interactive:false,workspace:'C:\\Users\\user\\work\\task'},()=>draw)).machine_id,'win-desktop');
 }
 const byLabel=new Set<string>();
 for(const draw of [0,0.999999])byLabel.add((await pickModelRoute(env,{interactive:false,workspace:'agent-hub'},()=>draw)).machine_id);
 assert.deepEqual([...byLabel].sort(),['linux-box','win-desktop'],'a label says nothing about the host');
});

test('workspace path narrowing never refuses a start and never overrides an explicit host',async()=>{
 const onlyWindows=fleetEnv({'win-desktop':{os:'windows'}});
 assert.equal((await pickModelRoute(onlyWindows,{interactive:false,workspace:'/home/user/work/task'},()=>0)).machine_id,'win-desktop');
 const mixed=fleetEnv({'win-desktop':{os:'windows'},'linux-box':{os:'linux'},'unknown-os':{}});
 const explicit=await pickModelRoute(mixed,{interactive:false,workspace:'/home/user/work/task',machine_id:'win-desktop'},()=>0);
 assert.equal(explicit.machine_id,'win-desktop');
 const seen=new Set<string>();
 for(const draw of [0,0.4,0.6,0.999999])seen.add((await pickModelRoute(mixed,{interactive:false,workspace:'/home/user/work/task'},()=>draw)).machine_id);
 assert.deepEqual([...seen].sort(),['linux-box','unknown-os'],'a host with unknown OS stays eligible');
});
