import {readFileSync} from 'node:fs';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pickModelRoute, pickModelRouteDetailed, weightedChoice, buildRoutingPool } from '../src/model-picker.ts';
import type { RoutingSnapshot } from '../src/routing-pool.ts';

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
   return Response.json({account:{enabled:true},quota:id in quota?{remaining:quota[id],captured_at:Date.now(),valid_until:Date.now()+900000}:null,fresh:true,hosts:['h1'],credential_usable:credentialUsable});
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
 assert.equal(route.quota_remaining,null);
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
  if(kind==='accounts')return Response.json({account:{enabled:true},quota:id in quota?{remaining:quota[id],captured_at:Date.now(),valid_until:Date.now()+900000}:null,fresh:true,hosts:['h1'],credential_usable:usable[id]===true,credential_status:usable[id]?'ready':'expired'});
  return Response.json({machine_id:'h1',connected:true,engines});
 }})});
 return {CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines')} as any;
}

const checkedWorkspace='/work/checked';
const missingMarker=(workspace=checkedWorkspace,mode='h',observed=Math.floor(Date.now()/1000))=>
 `auth:missing:l:${mode}:claude:${createHash('sha256').update(JSON.stringify(['hub2-local-auth-context-v1',workspace])).digest('base64url')}:${observed}`;

test('actual picker excludes known-missing LOCAL Claude despite installed engine, stale hint or healthy central account',async()=>{
 for(const centralReady of [false,true]){
  const env=poolEnv({codex:true,claude:centralReady},{codex:80,claude:80},['claude','codex'],pool.filter(row=>['codex-sol-medium','claude-sonnet-medium','claude-opus-medium'].includes(row.id)));
  env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({machine_id:'h1',connected:true,engines:['claude','codex'],
   capabilities:['headless','effort:claude','effort:codex','credentials:local:headless:claude:claude',
    missingMarker()]})})};
  const picked=await pickModelRouteDetailed(env,{interactive:false,workspace:checkedWorkspace},()=>0.9);
  assert.equal(picked.route.engine,'codex');
  assert.deepEqual(picked.diagnostics.pool.map(row=>row.policy_id),['codex-sol-medium']);
  assert.equal(picked.diagnostics.excluded.filter(row=>row.reason==='local_credentials_missing').length,2);
  const explicit=await pickModelRouteDetailed(env,{interactive:false,model:'sonnet',workspace:checkedWorkspace},()=>0);
  assert.equal(explicit.route.engine,'claude','negative observation must not create an explicit start gate');
  assert(explicit.diagnostics.advisories?.some(row=>row.reason==='local_credentials_missing'));
 }
});

test('actual Python heartbeat with empty local auth composes with the normal model picker',{skip:process.platform!=='linux'},async()=>{
 const {spawnSync}=await import('node:child_process'),{fileURLToPath}=await import('node:url');
 const {mkdtempSync,mkdirSync,rmSync}=await import('node:fs'),{join}=await import('node:path');
 const temp=fileURLToPath(new URL('../../.local/tmp/',import.meta.url));mkdirSync(temp,{recursive:true});
 const home=mkdtempSync(join(temp,'hub-local-auth-'));
 try{
  const script=`import sys,os,json,pathlib,types
sys.path.insert(0,sys.argv[1])
from agent_hub_host.agent import Bridge
import agent_hub_host.agent as agent_module
from agent_hub_host.local_auth_readiness import local_claude_missing_contexts
from agent_hub_host.native_launch import NativeLauncher
home=pathlib.Path(sys.argv[2]);binary=home/'bin';binary.mkdir(mode=0o700)
for name in ('claude','codex'):
 path=binary/name;path.write_bytes(bytes([127,69,76,70])+bytes(60));path.chmod(0o700)
(home/'.claude').mkdir(mode=0o700)
credentials=home/'.claude/.credentials.json';credentials.write_text('{"claudeAiOauth":{"accessToken":"","refreshToken":""}}');credentials.chmod(0o600)
os.environ.clear();os.environ.update(HOME=str(home),PATH=str(binary))
workspace=home/'project-测试';workspace.mkdir()
agent_module.local_claude_missing_contexts=lambda contexts,modes,environment:local_claude_missing_contexts(contexts,modes,environment,filesystem_root=home)
bridge=Bridge.__new__(Bridge);bridge.roots=[workspace];bridge.engines={'claude','codex'};bridge.launcher=NativeLauncher({'private_provider_homes':False})
def heartbeat(labels,engines,capabilities):print(json.dumps({'machine_id':'h1','connected':True,'engines':engines,'capabilities':capabilities,'workspace':str(workspace)}))
bridge.client=types.SimpleNamespace(heartbeat=heartbeat)
bridge.heartbeat([],include_native_sessions=False)
(workspace/'.claude').mkdir(mode=0o700)
settings=workspace/'.claude/settings.json';settings.write_text('{"apiKeyHelper":"synthetic-never-executed"}');settings.chmod(0o600)
bridge.heartbeat([],include_native_sessions=False)
settings.unlink()
config=home/'.config/aplexer/config.toml';config.parent.mkdir(parents=True,mode=0o700)
config.write_text('[engines.shell]'+chr(10)+'command=["/bin/sh"]'+chr(10)+'[engines.shell.env]'+chr(10)+'CLAUDE_CONFIG_DIR="/synthetic/alternate-login"'+chr(10));config.chmod(0o600)
bridge.heartbeat([],include_native_sessions=False)
`;
  const child=spawnSync('python3',['-I','-c',script,fileURLToPath(new URL('../host',import.meta.url)),home],{encoding:'utf8',timeout:10000});
  assert.equal(child.status,0,child.stderr);
  const [machine,projectHelperMachine,outerOverrideMachine]=child.stdout.trim().split('\n').map(row=>JSON.parse(row));
  assert(machine.engines.includes('claude'));
  const expectedMarker=missingMarker(machine.workspace);
  assert(machine.capabilities.some((flag:string)=>flag.startsWith(expectedMarker.slice(0,expectedMarker.lastIndexOf(':')+1))));
  assert(machine.capabilities.includes('credentials:local:headless:claude:claude'));
  assert(!projectHelperMachine.capabilities.some((flag:string)=>flag.startsWith('auth:missing:')));
  const env=poolEnv({codex:true,claude:true},{codex:80,claude:80},['claude','codex'],pool.filter(row=>['codex-sol-medium','claude-sonnet-medium','claude-opus-medium'].includes(row.id)));
  env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json(machine)})};
  const result=await pickModelRouteDetailed(env,{interactive:false,workspace:machine.workspace},()=>0.99);
  assert.equal(result.route.engine,'codex');assert(result.diagnostics.pool.every(row=>row.engine!=='claude'));
  const explicit=await pickModelRouteDetailed(env,{interactive:false,model:'opus',workspace:machine.workspace},()=>0);
  assert.equal(explicit.route.engine,'claude');
  env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json(projectHelperMachine)})};
  const alternative=await pickModelRouteDetailed(env,{interactive:false,workspace:machine.workspace},()=>0.99);
  assert(alternative.diagnostics.pool.some(row=>row.engine==='claude'),'project helper must retain Claude among healthy alternatives');
  assert(!outerOverrideMachine.capabilities.some((flag:string)=>flag.startsWith('auth:missing:')));
  env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json(outerOverrideMachine)})};
  for(const interactive of [false,true]){
   const outer=await pickModelRouteDetailed(env,{interactive,workspace:machine.workspace},()=>0.99);
   assert(outer.diagnostics.pool.some(row=>row.engine==='claude'),'outer shell config override stays unknown in both modes');
  }
 }finally{rmSync(home,{recursive:true,force:true});}
});

test('local negative is exact mode/account scoped; unknown and old hosts retain unverified behavior',async()=>{
 for(const extra of [[],[missingMarker(checkedWorkspace,'i')],[missingMarker('/other')],
  [missingMarker(checkedWorkspace,'h',Math.floor(Date.now()/1000)-61)],
  [missingMarker(checkedWorkspace,'h',Math.floor(Date.now()/1000)+10)],
  [missingMarker().replace(':claude:',':other:')],['credentials:missing:local:headless:claude:claude']]){
  const env=poolEnv({codex:true,claude:false},{codex:80,claude:80},['claude','codex'],pool.filter(row=>['codex-sol-medium','claude-sonnet-medium'].includes(row.id)));
  env.MACHINES={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({machine_id:'h1',connected:true,engines:['claude','codex'],
   capabilities:['headless','effort:claude','effort:codex','credentials:local:headless:claude:claude',...extra]})})};
  const result=await pickModelRouteDetailed(env,{interactive:false,workspace:checkedWorkspace},()=>0.99);
  assert(result.diagnostics.pool.some(row=>row.engine==='claude'));
  assert(result.diagnostics.advisories?.some(row=>row.reason==='local_login_unverified'));
 }
});

test('known missing local home cannot borrow central readiness but another approved host can serve Claude',async()=>{
 const env=poolEnv({claude:true},{claude:80},['claude'],pool.filter(row=>row.id==='claude-sonnet-medium'));
 env.ACCOUNTS={idFromName:(id:string)=>id,get:()=>({fetch:async()=>Response.json({account:{enabled:true},hosts:['h1','h2'],credential_usable:true})})};
 env.MACHINES={idFromName:(id:string)=>id,get:(id:string)=>({fetch:async()=>Response.json({machine_id:id,connected:true,engines:['claude'],
  capabilities:['headless','effort:claude',...(id==='h1'?[missingMarker()]:[])]})})};
 for(const draw of [0,0.99])assert.equal((await pickModelRoute(env,{interactive:false,workspace:checkedWorkspace},()=>draw)).machine_id,'h2');
 const explicit=await pickModelRouteDetailed(env,{interactive:false,model:'sonnet',machine_id:'h1',workspace:checkedWorkspace},()=>0);
 assert.equal(explicit.route.machine_id,'h1');assert(explicit.diagnostics.advisories?.some(row=>row.reason==='local_credentials_missing'));
});

test('scoped negatives never apply to absent, label, alternate or unknown workspace contexts',()=>{
 const now=Date.now();
 const snapshot={policy:{version:1,candidates:pool.filter(row=>['codex-sol-medium','claude-sonnet-medium'].includes(row.id))},
  accounts:new Map(['codex','claude'].map(id=>[id,{account:{enabled:true},credential_usable:true}])),
  machines:new Map([['h1',{connected:true,engines:['claude','codex'],capabilities:['headless','effort:claude','effort:codex',missingMarker()]}]]),observed_at:now};
 for(const workspace of [undefined,'checked','/work/checked/','/work/cold']){
  const result=buildRoutingPool(snapshot,{interactive:false,workspace});
  assert(result.diagnostics.pool.some(row=>row.engine==='claude'));
 }
});

function observationEnv(codex:any,claude:any){
 const env=poolEnv(healthy,left,['codex','claude'],pool.filter(row=>['codex-sol-medium','claude-sonnet-medium'].includes(row.id)));
 env.ACCOUNTS={idFromName:(id:string)=>id,get:(id:string)=>({fetch:async()=>Response.json({
  account:{enabled:true},credential_usable:true,hosts:['h1'],...(id==='codex'?codex:claude),
 })})};
 return env;
}
function observed(remaining:number,now=Date.now()){
 return {fresh:true,quota:{remaining,captured_at:now,valid_until:now+900000}};
}

test('fresh quota affects weight, but expired quota never preserves stale preference',async()=>{
 const now=Date.now(),other=observed(50,now);
 const fresh=await pickModelRouteDetailed(observationEnv(observed(100,now),other),{interactive:false},()=>0);
 assert.ok(Math.abs(fresh.diagnostics.pool[0].share-(.03+.94*2/3))<1e-9);
 assert.deepEqual(fresh.diagnostics.warnings,[]);
 const stale=await pickModelRouteDetailed(observationEnv({fresh:true,quota:{remaining:100,captured_at:now-31*60000,valid_until:now+60000}},other),{interactive:false},()=>0);
 assert.equal(stale.diagnostics.pool[0].share,0.5);
 assert.equal(stale.route.quota_remaining,null,'neutral weight is not a measured balance');
 assert.equal(stale.diagnostics.warnings?.[0].reason,'quota_stale');
});

test('missing, invalidated, expired and malformed quota use equal neutral weights without refusing starts',async()=>{
 const now=Date.now(),fresh=observed(50,now);
 const cases=[{}, {fresh:true}, {fresh:true,quota:null}, {...observed(1,now),fresh:false},
  {quota:observed(1,now).quota},
  {fresh:true,quota:{remaining:1,captured_at:now-30000,valid_until:now-1}},
  {fresh:true,quota:{remaining:99,captured_at:now+60000,valid_until:now+900000}},
  {fresh:true,quota:{remaining:99,captured_at:now-30*60000,valid_until:now+60000}},
  {fresh:true,quota:{remaining:99,captured_at:now,valid_until:now+3600001}},
  ...[-1,101,'99',true].map(remaining=>({fresh:true,quota:{remaining,captured_at:now,valid_until:now+900000}})),
  {fresh:true,quota:{remaining:99,captured_at:now+0.5,valid_until:now+900000}},
  {fresh:true,quota:{remaining:99,captured_at:now,valid_until:now+900000.5}},
 ];
 for(const row of cases){
  const result=await pickModelRouteDetailed(observationEnv(row,fresh),{interactive:false},()=>0);
  assert.equal(result.route.quota_remaining,null,JSON.stringify(row));
  assert.equal(result.diagnostics.pool[0].share,0.5,JSON.stringify(row));
  assert.deepEqual(result.diagnostics.warnings?.map(x=>[x.account_id,x.reason]),[['codex','quota_stale']]);
 }
 const allUnknown=await pickModelRouteDetailed(observationEnv({},{}),{interactive:false},()=>0.9);
 assert.equal(allUnknown.route.engine,'claude');
 assert.equal(allUnknown.diagnostics.pool.length,2);
 assert.equal(allUnknown.diagnostics.warnings?.length,2);
});

test('quota freshness expires at the earlier provider validity or thirty-minute limit',async()=>{
 const now=Date.now();
 const near=await pickModelRouteDetailed(observationEnv({fresh:true,quota:{remaining:80,captured_at:now-29*60000,valid_until:now+60000}},observed(50,now)),{interactive:false},()=>0);
 assert.equal(near.route.quota_remaining,80);
 const providerExpired=await pickModelRouteDetailed(observationEnv({fresh:false,quota:{remaining:80,captured_at:now-60000,valid_until:now+60000}},observed(50,now)),{interactive:false},()=>0);
 assert.equal(providerExpired.route.quota_remaining,null);
});
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


function capabilitySnapshot(capabilities: string[] | undefined) {
 const now=Date.now();
 return {
  policy:{version:17,candidates:pool.filter(row=>['codex-sol-medium','claude-opus-medium'].includes(row.id))},
  accounts:new Map(['codex','claude'].map(id=>[id,{account:{enabled:true},credential_usable:true,
   hosts:['h1'],...observed(80,now)}])),
  machines:new Map([['h1',{connected:true,engines:['codex','claude'],capabilities}]]),observed_at:now,
 };
}

test('goal capability preference spans candidates and the actual weighted draw',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','effort:codex','effort:claude','goal:codex','goal:zai']);
 const result=buildRoutingPool(snapshot,{interactive:true,with_goal:true,effort:'medium',machine_id:'h1'});
 assert.deepEqual(result.diagnostics.pool.map(row=>row.policy_id),['codex-sol-medium']);
 assert.equal(result.diagnostics.pool[0].share,1);
 assert.deepEqual(result.options.map(row=>[row.value.engine,row.value.machine_id]),[['codex','h1']]);
 for(const draw of [0,0.5,0.999999])assert.equal(weightedChoice(result.options,()=>draw).engine,'codex');
 assert.ok(result.diagnostics.excluded.some(row=>row.policy_id==='claude-opus-medium'&&row.reason==='goal_not_advertised'));
 assert.deepEqual(result.options[0].value.required_capabilities,['interactive','goal']);
});

test('unknown workspace labels do not mask observed goal capability or explicit needs',()=>{
 for(const required_capabilities of [[],['goal:codex']]){
  const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','effort:codex','effort:claude','goal:codex']);
  const result=buildRoutingPool(snapshot,{interactive:true,with_goal:true,workspace:'agent-hub',required_capabilities});
  assert.deepEqual(result.options.map(row=>row.value.engine),['codex']);
  for(const draw of [0,0.5,0.999999])assert.equal(weightedChoice(result.options,()=>draw).engine,'codex');
  assert.ok(result.diagnostics.excluded.some(row=>row.policy_id==='claude-opus-medium'&&row.reason==='goal_not_advertised'));
 }
});

test('workspace hints only narrow hosts that survived goal capability selection',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','effort:codex','effort:claude','goal:codex']);
 for(const account of snapshot.accounts.values())account.hosts.push('h2');
 snapshot.machines.set('h2',{connected:true,engines:['claude'],capabilities:['interactive:claude','effort:claude','workspace:agent-hub']});
 const input={interactive:true,with_goal:true,workspace:'agent-hub'};
 assert.deepEqual(buildRoutingPool(snapshot,input).options.map(row=>[row.value.engine,row.value.machine_id]),[['codex','h1']]);
 snapshot.machines.set('h2',{connected:true,engines:['codex','claude'],capabilities:['interactive:codex','effort:codex','goal:codex','workspace:agent-hub']});
 assert.deepEqual(buildRoutingPool(snapshot,input).options.map(row=>[row.value.engine,row.value.machine_id]),[['codex','h2']]);
});

test('ordinary starts retain model diversity when both engines advertise launch capabilities',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','effort:codex','effort:claude','goal:codex']);
 const result=buildRoutingPool(snapshot,{interactive:true,effort:'medium'});
 assert.deepEqual(result.diagnostics.pool.map(row=>row.policy_id),['codex-sol-medium','claude-opus-medium']);
 assert.deepEqual(result.diagnostics.pool.map(row=>row.share),[0.5,0.5]);
 assert.equal(weightedChoice(result.options,()=>0).engine,'codex');
 assert.equal(weightedChoice(result.options,()=>0.999999).engine,'claude');
});

test('missing or universally incompatible capability observations remain advisory without --with-goal',()=>{
 for(const capabilities of [undefined,[],['interactive:codex','interactive:claude','effort:codex','effort:claude']]){
  const result=buildRoutingPool(capabilitySnapshot(capabilities),{interactive:true,required_capabilities:['goal']});
  assert.equal(result.options.length,2);
  assert.deepEqual(result.diagnostics.excluded,[]);
  assert.deepEqual(result.diagnostics.advisories?.filter(row=>row.reason==='capability_not_advertised').map(row=>row.policy_id),
   ['codex-sol-medium','claude-opus-medium']);
 }
});

test('--with-goal never places an engine whose host lacks goal:<engine> (intake baab076a)',()=>{
 for(const capabilities of [undefined,[],['interactive:codex','interactive:claude','effort:codex','effort:claude'],
  ['goal','interactive:claude','effort:claude']]){
  assert.throws(()=>buildRoutingPool(capabilitySnapshot(capabilities),{interactive:true,with_goal:true}),
   (caught:any)=>caught.code==='goal_capability_unavailable'&&caught.status===503);
 }
});

test('--with-goal on the live hetzner-aplexer advertisement only draws goal-capable engines',()=>{
 const now=Date.now();
 const live=['credentials:local:headless:antigravity:gemini','credentials:local:headless:claude:claude','credentials:local:headless:codex:codex',
  'credentials:local:headless:go:shared-go','credentials:local:interactive:antigravity:gemini','credentials:local:interactive:claude:claude',
  'credentials:local:interactive:codex:codex','credentials:local:interactive:go:shared-go','effort:antigravity','effort:claude','effort:codex',
  'effort:zai','goal:codex','goal:zai','headless','interactive:antigravity','interactive:claude','interactive:codex','interactive:go','interactive:zai'];
 const policy={version:5,candidates:[
  {id:'codex-sol-medium',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
  {id:'claude-opus-medium',engine:'claude',model:'opus',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
  {id:'claude-sonnet-medium',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
  {id:'go-muse',engine:'go',model:'opencode-go/muse-spark-1.3-contributor',account_id:'shared-go',weight:1,capabilities:[],enabled:true},
  {id:'zai-glm-flash',engine:'zai',model:'glm-5.3-flash',account_id:'zai',weight:1,capabilities:[],enabled:true},
  {id:'antigravity-argon-medium',engine:'antigravity',model:'gemini-4-argon-medium',effort:'medium',account_id:'gemini',weight:1,capabilities:[],enabled:true},
 ]};
 const accounts=new Map(['codex','claude','shared-go','zai','gemini'].map(id=>[id,{account:{enabled:true},credential_usable:true,hosts:['hetzner-aplexer'],...observed(80,now)}]));
 // Claude has the most headroom; a quota-only preference must still not revive it for a goal start.
 accounts.get('codex')!.quota!.remaining=1;
 const machines=new Map([['hetzner-aplexer',{connected:true,os:'linux',engines:['antigravity','claude','codex','go','shell','zai'],capabilities:live}]]);
 for(const input of [{interactive:true,with_goal:true,tier:'deep',workspace:'agent-hub'},
  {interactive:true,with_goal:true,tier:'deep',workspace:'agent-hub',machine_id:'hetzner-aplexer'}]){
  const result=buildRoutingPool({policy,accounts,machines,observed_at:now},input);
  const engines=new Set(result.options.map(row=>row.value.engine));
  assert.ok([...engines].every(engine=>live.includes(`goal:${engine}`)),[...engines].join(','));
  assert.ok(engines.has('codex'),JSON.stringify(result.diagnostics));
  for(const draw of [0,0.25,0.5,0.75,0.999999])assert.ok(['codex','zai'].includes(weightedChoice(result.options,()=>draw).engine));
  for(const id of ['claude-opus-medium','claude-sonnet-medium','go-muse','antigravity-argon-medium'])
   assert.ok(result.diagnostics.excluded.some(row=>row.policy_id===id&&row.reason==='goal_not_advertised'),id);
  assert.ok(result.options.every(row=>row.value.machine_id==='hetzner-aplexer'));
 }
 // Without --with-goal the same host keeps every interactive engine.
 assert.ok(buildRoutingPool({policy,accounts,machines,observed_at:now},{interactive:true,tier:'deep'}).options.some(row=>row.value.engine==='claude'));
});

test('advertising goal:claude admits Opus and Sonnet to the real weighted goal draw without an engine override',()=>{
 const now=Date.now(),host='hetzner-aplexer';
 const candidates=[
  {id:'codex-sol-medium',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
  {id:'claude-opus-medium',engine:'claude',model:'opus',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
  {id:'claude-sonnet-medium',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
  {id:'zai-glm-flash',engine:'zai',model:'glm-5.3-flash',account_id:'zai-central',weight:1,capabilities:[],enabled:true},
 ];
 const capabilities=['headless','interactive:codex','interactive:claude','interactive:zai',
  'effort:codex','effort:claude','effort:zai','goal:codex','goal:zai'];
 const snapshot={policy:{version:5,candidates},observed_at:now,
  accounts:new Map(['codex','claude','zai-central'].map(id=>[id,{account:{enabled:true},credential_usable:true,hosts:[host],...observed(80,now)}])),
  machines:new Map([[host,{connected:true,os:'linux',engines:['codex','claude','zai'],capabilities}]]),
 };
 const input={interactive:true,with_goal:true,machine_id:host,tier:'deep'};
 assert.deepEqual(buildRoutingPool(snapshot,input).options.map(row=>row.value.engine).sort(),['codex','zai']);
 capabilities.push('goal:claude');
 const result=buildRoutingPool(snapshot,input);
 assert.deepEqual(result.options.map(row=>row.value.model).sort(),['glm-5.3-flash','gpt-6.1-sol','opus','sonnet']);
 let cumulative=0;
 const total=result.options.reduce((sum,row)=>sum+row.weight,0);
 const drawn=new Set<string>();
 for(const option of result.options){
  const picked=weightedChoice(result.options,()=>(cumulative+option.weight/2)/total);
  cumulative+=option.weight;
  assert.equal(picked.model,option.value.model);
  assert.equal(picked.machine_id,host);
  assert.deepEqual(picked.required_capabilities,['interactive','goal']);
  drawn.add(picked.model);
 }
 assert.ok(drawn.has('opus')&&drawn.has('sonnet'));
 assert.equal(result.diagnostics.excluded.filter(row=>row.reason==='goal_not_advertised').length,0);
});

test('explicit unsupported engine/model stays explicit with advisory capability observations',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','effort:codex','effort:claude','goal:codex']);
 for(const explicit of [{engine:'claude'},{model:'claude-opus-medium'}]){
  const result=buildRoutingPool(snapshot,{interactive:true,with_goal:true,...explicit});
  assert.equal(result.options.length,1);
  assert.equal(result.options[0].value.engine,'claude');
  assert.equal(result.options[0].value.override_reason,'explicit_route');
  assert.deepEqual(result.diagnostics.excluded,[]);
  assert.ok(result.diagnostics.advisories?.some(row=>row.reason==='capability_not_advertised'));
 }
});

test('capability preference removes unsupported hosts within a surviving candidate before weighting',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','goal:codex','effort:codex']);
 snapshot.accounts.get('codex')!.hosts.push('h2');
 snapshot.machines.set('h2',{connected:true,engines:['codex','claude'],capabilities:['interactive:codex','effort:codex']});
 const result=buildRoutingPool(snapshot,{interactive:true,with_goal:true});
 assert.deepEqual(result.options.map(row=>[row.value.policy_id,row.value.machine_id]),[['codex-sol-medium','h1']]);
 assert.equal(result.diagnostics.pool[0].share,1);
});


test('effective policy effort needs same-host support alongside goal, even without an effort override',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','interactive:claude','goal:codex','goal:claude','effort:codex']);
 const supported=buildRoutingPool(snapshot,{interactive:true,with_goal:true});
 assert.deepEqual(supported.diagnostics.pool.map(row=>row.policy_id),['codex-sol-medium']);
 snapshot.machines.get('h1')!.capabilities=['interactive:codex','interactive:claude','goal:codex','goal:claude'];
 const advisory=buildRoutingPool(snapshot,{interactive:true,with_goal:true});
 assert.equal(advisory.options.length,2,'no effort observations must not refuse the launch');
 assert.equal(advisory.diagnostics.excluded.length,0);
});

test('capability preference cannot revive a candidate excluded by fresh quota observations',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','headless','effort:codex','effort:claude']);
 snapshot.accounts.get('codex')!.quota!.remaining=1;
 const result=buildRoutingPool(snapshot,{interactive:true});
 assert.deepEqual(result.options.map(row=>row.value.engine),['claude']);
 assert.ok(result.diagnostics.excluded.some(row=>row.policy_id==='codex-sol-medium'&&row.reason==='quota_below_minimum'));
 assert.ok(result.diagnostics.advisories?.some(row=>row.policy_id==='claude-opus-medium'&&row.reason==='capability_not_advertised'));
});

test('--with-goal keeps a low-quota goal-capable engine instead of an incapable one',()=>{
 const snapshot=capabilitySnapshot(['interactive:codex','goal:codex','effort:codex']);
 snapshot.accounts.get('codex')!.quota!.remaining=1;
 const result=buildRoutingPool(snapshot,{interactive:true,with_goal:true});
 assert.deepEqual(result.options.map(row=>row.value.engine),['codex']);
 assert.ok(result.diagnostics.excluded.some(row=>row.policy_id==='claude-opus-medium'&&row.reason==='goal_not_advertised'));
});

function managedClaudeSnapshot(ready=true,approved=['h1','h2']){
 const now=Date.now(),workspace='/synthetic/central-worker';
 const context=createHash('sha256').update(JSON.stringify(['hub2-local-auth-context-v1',workspace])).digest('base64url');
 const caps=['headless','effort:claude','managed:hub-claude:opus:medium:v1','credentials:local:headless:claude:claude'];
 return{workspace,snapshot:{observed_at:now,
  policy:{version:1,candidates:[{id:'claude-opus',engine:'claude',model:'opus',effort:'medium',account_id:'claude',weight:1,enabled:true,capabilities:[]}]},
  accounts:new Map([['claude',{account:{enabled:true},credential_usable:ready,credential_status:ready?'ready':'unavailable',hosts:approved,
    fresh:true,quota:{remaining:80,captured_at:now,valid_until:now+600000}}]]),
  machines:new Map([['h1',{machine_id:'h1',connected:true,last_seen:now,engines:['claude'],
    capabilities:[...caps,`auth:missing:l:h:claude:${context}:${Math.floor(now/1000)}`]}],
    ['h2',{machine_id:'h2',connected:true,last_seen:now,engines:['claude'],capabilities:[...caps]}]])}};
}

test('explicit managed Claude uses central readiness and approved hosts, not local missing markers',()=>{
 const {snapshot,workspace}=managedClaudeSnapshot();
 const input={interactive:false,workspace,engine:'claude',model:'opus',effort:'medium',required_capabilities:['managed:hub-claude:opus:medium:v1']};
 const native=buildRoutingPool(snapshot,input);
 assert.deepEqual(native.options.map(row=>row.value.machine_id),['h2'],'capability alone cannot opt out of local auth evidence');
 const managed=buildRoutingPool(snapshot,{...input,managed_credential_label:'hub-claude'});
 assert.deepEqual(managed.options.map(row=>row.value.machine_id).sort(),['h1','h2']);
 assert(!managed.diagnostics.advisories?.some(row=>['local_credentials_missing','local_login_unverified','central_credentials_unavailable'].includes(row.reason)));
 // This feature does not redefine the existing <3-model degraded-pool rule.
 assert.equal(managed.diagnostics.pool_degraded,true);
});

test('explicit managed Claude cannot borrow a local login when central auth or approval is absent',()=>{
 for(const [ready,approved,reason] of [[false,['h1','h2'],'central_credentials_unavailable'],[true,['h1'],'central_host_not_approved']] as const){
  const {snapshot,workspace}=managedClaudeSnapshot(ready,[...approved]);
  const input={interactive:false,workspace,engine:'claude',model:'opus',effort:'medium',machine_id:'h2',required_capabilities:['managed:hub-claude:opus:medium:v1']};
  const native=buildRoutingPool(snapshot,input);
  assert(!native.diagnostics.advisories?.some(row=>row.reason===reason));
  const managed=buildRoutingPool(snapshot,{...input,managed_credential_label:'hub-claude'});
  assert(managed.diagnostics.advisories?.some(row=>row.reason===reason));
  assert(!managed.diagnostics.advisories?.some(row=>row.reason==='local_login_unverified'));
 }
});

test('managed picker mode validates exact selector, Opus/medium/headless and canonical account',()=>{
 const {snapshot,workspace}=managedClaudeSnapshot();
 const input={interactive:false,workspace,engine:'claude',model:'opus',effort:'medium',managed_credential_label:'hub-claude' as const};
 for(const changed of [{managed_credential_label:'other'},{model:'sonnet'},{engine:'codex'},{effort:'max'},{interactive:true},{with_goal:true}])
  assert.throws(()=>buildRoutingPool(snapshot,{...input,...changed} as any),/invalid_managed_credential_route/);
 snapshot.policy.candidates[0].account_id='foreign-claude';
 assert.throws(()=>buildRoutingPool(snapshot,input),/model_policy_unavailable/);
});

const hydrogenPolicy = JSON.parse(readFileSync(new URL('../../config/model-policy.json', import.meta.url), 'utf8'));
function hydrogenFixture(): RoutingSnapshot {
 const now=Date.now();
 const candidates=[...hydrogenPolicy.candidates,{id:'zai-glm-flash',engine:'zai',model:'glm-5.3-flash',account_id:'zai-central',weight:1_000_000,capabilities:[],enabled:true}];
 return {policy:{version:6,candidates},observed_at:now,
  accounts:new Map(candidates.map((row:any)=>[row.account_id,{account:{enabled:true},credential_usable:true,fresh:true,hosts:['h1'],quota:{remaining:80,captured_at:now,valid_until:now+900000}}])),
  machines:new Map([['h1',{connected:true,engines:['codex','claude','antigravity','go','zai'],capabilities:['headless','interactive','effort:codex','effort:claude','effort:antigravity']} ]])};
}
test('ordinary default/fix/implement in both modes and quick/daily excludes Argon and stale Flash',()=>{
 for(const purpose of [undefined,'fix','implement'] as const)for(const interactive of [false,true])for(const tier of [undefined,'quick','daily']){
  const result=buildRoutingPool(hydrogenFixture(),{interactive,purpose,tier});
  const models=result.options.map(row=>row.value.model);
  assert(models.includes('gemini-4-hydrogen-medium'));
  assert(models.includes('gpt-6.1-sol'));
  assert(!models.some(model=>/argon|flash/.test(model)));
  for(const draw of [0,.25,.5,.75,.999999])assert(!/argon|flash/.test(weightedChoice(result.options,()=>draw).model));
 }
});
test('review/think/deep and explicit Sol/Fable/Argon preserve existing selection',()=>{
 for(const input of [{purpose:'review' as const},{purpose:'think' as const},{tier:'deep'}]){
  const models=buildRoutingPool(hydrogenFixture(),{interactive:false,...input}).options.map(row=>row.value.model);
  assert(models.includes('gemini-4-argon-medium'));
 }
 for(const model of ['gemini-4-argon-medium','antigravity-argon-medium','gpt-6.1-sol','fable']){
  const result=buildRoutingPool(hydrogenFixture(),{interactive:false,model});
  assert(result.options.every(row=>row.value.model===model||row.value.policy_id===model));
 }
});
test('an Antigravity-only ordinary automatic pool selects Hydrogen, never Argon fallback',()=>{
 const snapshot=hydrogenFixture();
 const result=buildRoutingPool(snapshot,{interactive:false,engine:'antigravity'});
 assert(result.options.length>0);
 assert(result.options.every(row=>row.value.model==='gemini-4-hydrogen-medium'));
 snapshot.policy.candidates=snapshot.policy.candidates.filter(row=>row.model!=='gemini-4-hydrogen-medium');
 assert.throws(()=>buildRoutingPool(snapshot,{interactive:false,engine:'antigravity'}),(e:any)=>e.code==='model_policy_unavailable');
 assert.equal(buildRoutingPool(snapshot,{interactive:false,engine:'antigravity',model:'gemini-4-argon-medium'}).options[0].value.model,'gemini-4-argon-medium');
});
