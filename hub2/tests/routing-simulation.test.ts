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

test('1000 simulated draws use one passive observation set and the committed Argon pool',async()=>{
 const f=fixture();let index=0;
 const result=await simulateModelRoutes(f.env,{interactive:false},1000,()=>((index++)+.5)/1000);
 assert.equal(result.draws,1000);assert.equal(result.policy_version,17);
 assert.equal(Object.values(result.counts).reduce((a,b)=>a+b,0),1000);
 assert.equal(result.pool_degraded,false);
 assert.ok(result.pool.some(row=>row.policy_id==='antigravity-argon-medium'));
 assert.ok(result.pool.every(row=>row.share>.03&&!/astra|fable/i.test(row.model)));
 const luna=result.pool.find(row=>/luna/.test(row.model))!;
 const others=result.pool.filter(row=>row!==luna);
 assert.ok(luna.share<=others.reduce((n,row)=>n+row.share,0)/others.length+1e-12);
 for(const kind of ['accounts','machines']){
  const rows=f.calls.filter(call=>call.kind===kind);
  assert.equal(rows.length,kind==='accounts'?5:1);
  assert.ok(rows.every(call=>call.path==='/_internal/routing-snapshot'&&call.method==='GET'));
 }
 assert.equal(result.advisories.filter(row=>row.reason==='local_login_unverified').length,result.pool.length);
 assert.equal(result.advisories.filter(row=>row.reason==='capability_not_advertised').length,result.pool.length);
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
 assert.deepEqual(diagnostics.excluded,[]);
 assert.equal(diagnostics.advisories.filter(row=>row.reason==='credential_missing').length,diagnostics.pool.length);
 assert.equal(diagnostics.advisories.filter(row=>row.reason==='capability_not_advertised').length,diagnostics.pool.length);
 assert.ok(diagnostics.advisories.every(row=>['credential_missing','capability_not_advertised'].includes(row.reason)));
 const only=f.state.policies.filter((row:any)=>row.id==='codex-luna-max');f.state.policies=only;
 const degraded=await simulateModelRoutes(f.env,{interactive:false},1,()=>0);
 assert.equal(degraded.pool_degraded,true);assert.equal(degraded.counts['codex-luna-max'],1,'degradation does not refuse a start');
});

test('legacy monitor retains all-bad credential diagnostics while passive simulation distinguishes advisories',async()=>{
 const f=fixture();f.state.actor.scopes=['operator:admin'];f.machines.get('h1').capabilities=[];
 const post=()=>worker.fetch(new Request('https://hub.test/api/model-picker/pick',{method:'POST',
  headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({})}),f.env);
 const failed=await post(),old=await failed.json() as any;
 assert.equal(failed.status,200);assert.equal(old.excluded.length,old.pool.length);
 assert.ok(old.excluded.every((row:any)=>row.reason==='credential_missing'));
 const passive=await (await f.get('?n=1')).json() as any;
 assert.deepEqual(passive.excluded,[]);
 assert.equal(passive.advisories.filter((row:any)=>row.reason==='credential_missing').length,passive.pool.length);
 assert.equal(passive.advisories.filter((row:any)=>row.reason==='capability_not_advertised').length,passive.pool.length);
 assert.ok(passive.advisories.every((row:any)=>['credential_missing','capability_not_advertised'].includes(row.reason)));
 f.machines.get('h1').capabilities=flags();
 const local=await (await post()).json() as any;
 assert.deepEqual(local.excluded,[],'configured host-owned logins are not reported as central credential failures');
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
 for(const query of ['?n=0','?n=1001','?n=1.5','?n=01','?n=1&n=2','?unknown=x','?mode=invalid','?effort=high','?purpose=invalid','?model=astra']){
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
