import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import worker from '../src/worker';
import {buildRoutingPool,loadRoutingSnapshot,simulateModelRoutes,pickModelRouteDetailed} from '../src/model-picker';
import type {RoutingSnapshot} from '../src/routing-pool';

const policy=JSON.parse(readFileSync(new URL('../../config/model-policy.json',import.meta.url),'utf8'));
const bindings:Record<string,string>={codex:'codex',claude:'claude',go:'shared-go',zai:'zai-central',antigravity:'gemini'};
const flags=(mode='headless')=>Object.entries(bindings).map(([engine,account])=>`credentials:local:${mode}:${engine}:${account}`);
const token='synthetic-routing-observer-token-0000000000000';
function fixture(){
 const now=Date.now(),calls:Array<{kind:string;id:string;path:string;method:string}>=[];
 const accountRows=new Map(Object.values(bindings).map(id=>[id,{account:{enabled:true},credential_usable:false,credential_status:'missing',
  quota:{remaining:50,captured_at:now,valid_until:now+900000},fresh:true,hosts:['h1']} as any]));
 const machines=new Map<string,any>([['h1',{machine_id:'h1',connected:true,os:'linux',engines:Object.keys(bindings),capabilities:flags()}]]);
 const state={policies:structuredClone(policy.candidates),actor:{id:'observer',kind:'operator',scopes:['routing:read']}};
 const ns=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(input:string|Request,init?:RequestInit)=>{
  const req=input instanceof Request?input:new Request(input,init),url=new URL(req.url);
  calls.push({kind,id,path:url.pathname,method:req.method});
  if(kind==='control'){
   if(url.pathname==='/_verify')return Response.json({actor:state.actor});
   if(url.pathname==='/_routing-policy')return Response.json({version:17,candidates:state.policies});
   if(url.pathname==='/api/directory')return Response.json({items:[...machines.keys()].map(id=>({id})),next:null});
  }
  if(kind==='accounts'&&['/_internal/routing','/_internal/routing-snapshot'].includes(url.pathname))return Response.json(accountRows.get(id)??{});
  if(kind==='machines'&&['/_internal/routing','/_internal/routing-snapshot'].includes(url.pathname))return Response.json(machines.get(id)??{});
  throw new Error('unexpected effect '+kind+url.pathname);
 }})});
 const env={CONTROL:ns('control'),ACCOUNTS:ns('accounts'),MACHINES:ns('machines')} as any;
 const get=(query='',method='GET')=>worker.fetch(new Request('https://hub.test/api/fleet/routing/simulate'+query,{method,headers:{Authorization:'Bearer '+token}}),env);
 return{env,get,state,calls,machines,accountRows};
}

function skewedFixture(){
 const f=fixture();
 for(const [id,remaining] of Object.entries({codex:56,claude:76,'shared-go':7,'zai-central':65,gemini:81.45}))
  f.accountRows.get(id).quota.remaining=remaining;
 return f;
}

test('skewed headroom keeps Go above three percent in the real simulation and draw',async()=>{
 const f=skewedFixture();let index=0;
 const result=await simulateModelRoutes(f.env,{interactive:false},1000,()=>((index++)+.5)/1000);
 assert.deepEqual(result.share_floor,{minimum_share:.03,applied:true,reason:null});
 assert.equal(result.pool.length,6);assert.equal(result.pool_degraded,false);
 assert.ok(result.pool.every(row=>row.share>.03));
 assert.ok(Math.abs(result.pool.reduce((sum,row)=>sum+row.share,0)-1)<1e-12);
 const go=result.pool.find(row=>row.policy_id==='go-muse')!;
 assert.ok(Math.abs(go.share-(.03+.82*7/377.9))<1e-12);
 // Six automatic routes: headroom 377.9, residual 1-6*.03=.82; Luna is explicit-only.
 // No zai-central in the Hub picker, even with a healthy zai-central account and an advertised zai engine.
 assert.ok(!result.pool.some(row=>row.engine==='zai'||row.account_id==='zai-central'));
 assert.equal(result.counts['zai-glm-5-3-flash'],undefined);
 assert.ok(result.counts['go-muse']>30);
 // The actual start draw uses the same transformed weights, not just diagnostics.
 const start=await pickModelRouteDetailed(f.env,{interactive:false},()=>result.pool.slice(0,result.pool.findIndex(r=>r.policy_id==='go-muse')).reduce((s,r)=>s+r.share,0)+1e-6);
 assert.equal(start.route.policy_id,'go-muse');
 assert.deepEqual(start.diagnostics.pool,result.pool);
 assert.ok(!result.pool.some(row=>row.policy_id==='codex-luna-max'));
 assert.equal(result.counts['codex-luna-max'],undefined);
});

test('exploration retains the five-percent eligibility boundary and cannot restore a depleted route',async()=>{
 const f=skewedFixture();f.accountRows.get('shared-go').quota.remaining=5;
 const atBoundary=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.ok(atBoundary.pool.some(row=>row.policy_id==='go-muse'&&row.share>.03));
 f.accountRows.get('shared-go').quota.remaining=4.99;
 const below=await simulateModelRoutes(f.env,{interactive:false},1000,()=>.5);
 assert.ok(!below.pool.some(row=>row.policy_id==='go-muse'));
 assert.equal(below.counts['go-muse'],undefined);
 assert.ok(below.excluded.some(row=>row.policy_id==='go-muse'&&row.reason==='quota_below_minimum'));
 assert.ok(below.pool.every(row=>row.share>.03));
});

test('exploration preserves stale-neutral headroom, quota influence and underlying deep multipliers',async()=>{
 const f=skewedFixture();f.accountRows.get('shared-go').fresh=false;
 const stale=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.ok(Math.abs(stale.pool.find(row=>row.policy_id==='go-muse')!.share-(.03+.82*50/420.9))<1e-12);
 assert.ok(stale.warnings.some(row=>row.account_id==='shared-go'&&row.reason==='quota_stale'));
 f.accountRows.get('shared-go').fresh=true;
 const ordinary=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.ok(stale.pool.find(row=>row.policy_id==='go-muse')!.share>ordinary.pool.find(row=>row.policy_id==='go-muse')!.share);
 const deep=await simulateModelRoutes(f.env,{interactive:false,tier:'deep'},1,()=>0);
 assert.ok(deep.pool.every(row=>row.share>.03));
 // Deep tier without an effort prefers the strong high-effort route (intake 10a76faa).
 assert.deepEqual(deep.pool.map(row=>[row.policy_id,row.effort,row.share]),[['claude-opus-high','high',1]]);
 assert.ok(deep.excluded.some(row=>row.policy_id==='antigravity-hydrogen-medium'&&row.reason==='deep_prefers_strong_model'));
 assert.ok(deep.excluded.some(row=>row.policy_id==='claude-opus-medium'&&row.reason==='deep_prefers_high_effort'));
 // Explicit medium keeps the strong pool and the opus/sol deep multiplier: exploration leaves .91.
 const medium=await simulateModelRoutes(f.env,{interactive:false,tier:'deep',effort:'medium'},1,()=>0);
 assert.deepEqual(medium.pool.map(row=>row.policy_id).sort(),['antigravity-argon-medium','claude-opus-medium','codex-sol-medium']);
 assert.ok(Math.abs(medium.pool.find(row=>row.policy_id==='codex-sol-medium')!.share-(.03+.91*168/477.45))<1e-12);
 assert.ok(Math.abs(medium.pool.find(row=>row.policy_id==='claude-opus-medium')!.share-(.03+.91*228/477.45))<1e-12);
 assert.ok(Math.abs(medium.pool.find(row=>row.policy_id==='antigravity-argon-medium')!.share-(.03+.91*81.45/477.45))<1e-12);
});

test('automatic non-Luna weights and candidate shares survive skew and additional hosts',async()=>{
 const f=skewedFixture();
 for(const [id,row] of f.accountRows)row.quota.remaining=id==='codex'?100:5;
 const original=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.equal(original.pool.length,6);
 assert.ok(!original.pool.some(row=>row.policy_id==='codex-luna-max'));
 // Sol100 and five 5-point routes: total125, exploration residual .82.
 for(const row of original.pool)assert.ok(Math.abs(row.share-(.03+.82*(row.policy_id==="codex-sol-medium"?100:5)/125))<1e-12);
 assert.ok(Math.abs(original.pool.reduce((sum,row)=>sum+row.share,0)-1)<1e-12);
 f.machines.set('h2',{machine_id:'h2',connected:true,engines:['codex'],capabilities:[flags()[0]]});
 const expanded=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.deepEqual(expanded.pool,original.pool);
 assert.deepEqual(expanded.share_floor,original.share_floor);
});

test('exploration remains feasible at the existing maximum policy size and for an eight-member pool',async()=>{
 const f=skewedFixture(),go=f.state.policies.find((row:any)=>row.id==='go-muse');
 // Additional distinct policy routes, without changing the production policy.
 for(const size of [8,32]){
  f.state.policies=Array.from({length:size},(_,i)=>({...go,id:`synthetic-route-${i}`,weight:i===0?.001:1}));
  const result=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
  assert.equal(result.pool.length,size);assert.equal(result.share_floor.applied,true);
  assert.ok(result.pool.every(row=>row.share>.03));
  assert.ok(Math.abs(result.pool.reduce((sum,row)=>sum+row.share,0)-1)<1e-12);
 }
});

test('1000 simulated draws use one passive observation set and the committed ordinary Hydrogen pool',async()=>{
 const f=fixture();let index=0;
 const result=await simulateModelRoutes(f.env,{interactive:false},1000,()=>((index++)+.5)/1000);
 assert.equal(result.draws,1000);assert.equal(result.policy_version,17);
 assert.equal(Object.values(result.counts).reduce((a,b)=>a+b,0),1000);
 assert.equal(result.pool_degraded,false);
 assert.ok(!result.pool.some(row=>row.policy_id==='antigravity-argon-medium'));
 assert.ok(result.pool.some(row=>row.policy_id==='antigravity-hydrogen-medium')&&!result.pool.some(row=>row.engine==='antigravity'&&/flash|argon/.test(row.model)));
 assert.ok(result.pool.every(row=>row.share>.03&&!/astra|fable/i.test(row.model)));
 assert.equal(result.pool.length,6);
 assert.ok(!result.pool.some(row=>/luna/i.test(row.model)));
 assert.ok(!result.pool.some(row=>row.engine==='zai'||row.account_id==='zai-central'));
 assert.equal(result.counts['codex-luna-max'],undefined);
 assert.ok(result.pool.every(row=>Math.abs(row.share-1/6)<1e-12));
 for(const kind of ['accounts','machines']){
  const rows=f.calls.filter(call=>call.kind===kind);
  // Five accounts: the four legacy routes plus the committed zai-central route.
  assert.equal(rows.length,kind==='accounts'?5:1);
  assert.ok(rows.every(call=>call.path==='/_internal/routing-snapshot'&&call.method==='GET'));
 }
 assert.equal(result.advisories.filter(row=>row.reason==='local_login_unverified').length,result.pool.length);
 assert.equal(result.advisories.filter(row=>row.reason==='capability_not_advertised').length,result.pool.length);
 // The committed GLM 5.4 zcodex route stays out of the h1 pool (no zcodex engine, unusable credential).
 assert.deepEqual(result.excluded.map(row=>[row.policy_id,row.reason]),[['glm-flash-zcodex','credential_missing']]);
 assert.ok(result.advisories.every(row=>['local_login_unverified','capability_not_advertised'].includes(row.reason)));
});

test('the pure pool uses only its frozen timestamp and identical inputs give identical pools',async()=>{
 const f=fixture(),active=await loadRoutingSnapshot(f.env,{interactive:false}),passive=await loadRoutingSnapshot(f.env,{interactive:false},true);
 passive.observed_at=active.observed_at;
 const prior=Date.now;
 Date.now=()=>{throw new Error('live clock consulted inside pure pool');};
 try{assert.deepEqual(buildRoutingPool(active,{interactive:false}),buildRoutingPool(passive,{interactive:false}));}
 finally{Date.now=prior;}
 const before=JSON.stringify([...active.machines]);
 buildRoutingPool(active,{interactive:false,machine_id:'h1'});
 assert.equal(JSON.stringify([...active.machines]),before,'the builder does not mutate the observation maps');
});

test('local hints bind the account and launch mode; central credential delivery hosts do not hide local logins',async()=>{
 const f=fixture();
 f.state.policies=f.state.policies.filter((row:any)=>['codex-sol-medium','claude-sonnet-medium'].includes(row.id));
 f.state.policies.push({...f.state.policies[0],id:'codex-orin-route',account_id:'codex-orin'});
 f.accountRows.set('codex-orin',{...f.accountRows.get('codex'),credential_usable:false});
 const passive=await loadRoutingSnapshot(f.env,{interactive:false},true);
 const headless=buildRoutingPool(passive,{interactive:false});
 assert.ok(headless.diagnostics.pool.some(row=>row.policy_id==='codex-sol-medium'));
 assert.ok(!headless.diagnostics.pool.some(row=>row.policy_id==='codex-orin-route'));
 assert.ok(headless.diagnostics.excluded.some(row=>row.policy_id==='codex-orin-route'));
 f.accountRows.get('claude').credential_usable=true;
 const interactive=await pickModelRouteDetailed(f.env,{interactive:true},()=>0);
 assert.equal(interactive.route.engine,'claude','a headless hint does not bless interactive Codex');
 f.machines.set('local-only',{machine_id:'local-only',connected:true,engines:['codex'],capabilities:[flags()[0]]});
 f.machines.get('h1').capabilities=[];
 const localChoice=await pickModelRouteDetailed(f.env,{interactive:false,model:'gpt-6.1-sol'},()=>0);
 assert.equal(localChoice.route.machine_id,'local-only');
 assert.ok(localChoice.diagnostics.advisories?.some(row=>row.reason==='local_login_unverified'));
});

test('local hint on another host never blesses a pinned host and duplicate hosts do not multiply model share',async()=>{
 const f=fixture(),original=buildRoutingPool(await loadRoutingSnapshot(f.env,{interactive:false},true),{interactive:false});
 f.machines.set('h2',{machine_id:'h2',connected:true,engines:['codex'],capabilities:[flags()[0]]});
 const expanded=buildRoutingPool(await loadRoutingSnapshot(f.env,{interactive:false},true),{interactive:false});
 assert.deepEqual(expanded.diagnostics.pool,original.diagnostics.pool);
 f.machines.get('h1').capabilities=[];
 f.accountRows.get('claude').credential_usable=true;
 const pinned=buildRoutingPool(await loadRoutingSnapshot(f.env,{interactive:false,machine_id:'h1'},true),{interactive:false,machine_id:'h1'});
 assert.ok(pinned.diagnostics.excluded.some(row=>row.policy_id==='codex-sol-medium'));
 assert.ok(pinned.options.every(row=>row.value.machine_id==='h1'));
});

test('unhealthy observations retained by soft fallback are advisories, never claimed exclusions',async()=>{
 const f=fixture();f.machines.get('h1').capabilities=[];
 const {diagnostics}=buildRoutingPool(await loadRoutingSnapshot(f.env,{interactive:false},true),{interactive:false});
 // Only the committed zcodex route is a claimed exclusion: no host advertises its engine
 // while the six legacy routes survive, so the soft fallback never applies to it.
 assert.deepEqual(diagnostics.excluded.map(row=>[row.policy_id,row.reason]),[['glm-flash-zcodex','engine_not_advertised']]);
 assert.equal(diagnostics.advisories.filter(row=>row.reason==='credential_missing').length,diagnostics.pool.length+1);
 assert.equal(diagnostics.advisories.filter(row=>row.reason==='capability_not_advertised').length,diagnostics.pool.length);
 assert.ok(diagnostics.advisories.every(row=>['credential_missing','capability_not_advertised'].includes(row.reason)));
 const only=f.state.policies.filter((row:any)=>row.id==='codex-luna-max');f.state.policies=only;
 // Soft credential fallback cannot restore a hard-excluded automatic Luna.
 await assert.rejects(simulateModelRoutes(f.env,{interactive:false},1,()=>0),
  (error:any)=>error.code==='model_policy_unavailable'&&error.status===503);
 // Explicit canonical/policy intent still uses the same quota/host safety checks.
 for(const model of ['gpt-6-luna','codex-luna-max']){
  const explicit=await simulateModelRoutes(f.env,{interactive:false,model},1,()=>0);
  assert.equal(explicit.pool_degraded,true);assert.equal(explicit.counts['codex-luna-max'],1);
 }
 f.accountRows.get('codex').quota.remaining=15;
 for(const model of ['gpt-6-luna','codex-luna-max'])await assert.rejects(
  simulateModelRoutes(f.env,{interactive:false,model},1,()=>0),
  (error:any)=>error.code==='model_policy_unavailable'&&error.status===503);
});

test('legacy monitor retains all-bad credential diagnostics while passive simulation distinguishes advisories',async()=>{
 const f=fixture();f.state.actor.scopes=['operator:admin'];f.machines.get('h1').capabilities=[];
 const post=()=>worker.fetch(new Request('https://hub.test/api/model-picker/pick',{method:'POST',
  headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({})}),f.env);
 const failed=await post(),old=await failed.json() as any;
 assert.equal(failed.status,200);
 // The legacy monitor claims every all-bad-credential route as an exclusion,
 // plus the committed zcodex route whose credential is equally unusable.
 assert.equal(old.excluded.filter((row:any)=>row.reason==='credential_missing').length,old.pool.length+1);
 assert.ok(old.excluded.some((row:any)=>row.policy_id==='glm-flash-zcodex'));
 const passive=await (await f.get('?n=1')).json() as any;
 assert.deepEqual(passive.excluded.map((row:any)=>[row.policy_id,row.reason]),[['glm-flash-zcodex','engine_not_advertised']]);
 assert.equal(passive.advisories.filter((row:any)=>row.reason==='credential_missing').length,passive.pool.length+1);
 assert.equal(passive.advisories.filter((row:any)=>row.reason==='capability_not_advertised').length,passive.pool.length);
 assert.ok(passive.advisories.every((row:any)=>['credential_missing','capability_not_advertised'].includes(row.reason)));
 f.machines.get('h1').capabilities=flags();
 const local=await (await post()).json() as any;
 assert.deepEqual(local.excluded.map((row:any)=>[row.policy_id,row.reason]),[['glm-flash-zcodex','credential_missing']]);
 assert.equal(local.advisories.filter((row:any)=>row.reason==='local_login_unverified').length,local.pool.length);
 assert.equal(local.advisories.filter((row:any)=>row.reason==='capability_not_advertised').length,local.pool.length);
 assert.ok(local.advisories.every((row:any)=>['local_login_unverified','capability_not_advertised'].includes(row.reason)));
});

test('HTTP simulation validates parameters, requires routing scope, and performs no business mutation',async()=>{
 const f=fixture(),response=await f.get('?n=10');
 assert.equal(response.status,200);
 const result=await response.json() as any;
 assert.equal(Object.values<number>(result.counts).reduce((a,b)=>a+b,0),10);
 assert.ok(f.calls.filter(call=>call.kind!=='control').every(call=>call.method==='GET'&&call.path==='/_internal/routing-snapshot'));
 f.calls.length=0;
 for(const query of ['?n=0','?n=1001','?n=1.5','?n=01','?n=1&n=2','?unknown=x','?mode=invalid','?effort=xhigh','?purpose=invalid','?model=astra']){
  assert.equal((await f.get(query)).status,400,query);
 }
 assert.ok(f.calls.every(call=>call.kind==='control'),'invalid input never observes accounts or hosts');
 f.calls.length=0;f.state.actor.scopes=['session:start'];
 assert.equal((await f.get('?n=1')).status,403);
 assert.ok(f.calls.every(call=>call.path==='/_verify'));
 assert.equal((await f.get('?n=1','POST')).status,405);
});

test('simulation never uses the active provider-observation route and failures remain typed',async()=>{
 const f=fixture();f.machines.get('h1').connected=false;
 const response=await f.get('?n=1');
 assert.equal(response.status,503);assert.equal((await response.json() as any).error,'no_connected_host');
 assert.ok(!f.calls.some(call=>call.path==='/_internal/routing'));
 await assert.rejects(simulateModelRoutes(f.env,{interactive:false},1001), (e:any)=>e.code==='invalid_simulation_count');
});

test('valid policy identifiers cannot change the simulation count object prototype',async()=>{
 const f=fixture();f.state.policies=[{...f.state.policies[1],id:'__proto__'}];
 const response=await f.get('?n=3'),result=await response.json() as any;
 assert.equal(response.status,200);assert.deepEqual(Object.keys(result.counts),['__proto__']);
 assert.equal(result.counts['__proto__'],3);
});

test('simulation with the real policy draws zero Codex when the account is fresh 6% or stale/unknown',async()=>{
 const f=fixture();
 f.accountRows.get('codex').quota.remaining=6;
 const low=await simulateModelRoutes(f.env,{interactive:false},500,(()=>{let i=0;return()=>((i++)+.5)/500;})());
 assert.ok(low.pool.every(row=>!row.policy_id.startsWith('codex-')));
 assert.ok(!Object.keys(low.counts).some(id=>id.startsWith('codex-')),JSON.stringify(low.counts));
 assert.ok(low.excluded.some(row=>row.policy_id==='codex-sol-medium'&&row.reason==='codex_reserve'));
 assert.ok(Math.abs(low.pool.reduce((sum,row)=>sum+row.share,0)-1)<1e-12);
 f.accountRows.get('codex').quota.remaining=16;
 const ok=await simulateModelRoutes(f.env,{interactive:false},500,(()=>{let i=0;return()=>((i++)+.5)/500;})());
 assert.ok(ok.pool.some(row=>row.policy_id==='codex-sol-medium'));
 f.accountRows.get('codex').fresh=false;
 const stale=await simulateModelRoutes(f.env,{interactive:false},500,(()=>{let i=0;return()=>((i++)+.5)/500;})());
 assert.ok(!Object.keys(stale.counts).some(id=>id.startsWith('codex-')));
 // Explicit Codex with an excluded account is a typed refusal, never a restored route.
 await assert.rejects(()=>simulateModelRoutes(f.env,{interactive:false,engine:'codex'},1,()=>0),(e:any)=>e.code==='model_policy_unavailable');
});
