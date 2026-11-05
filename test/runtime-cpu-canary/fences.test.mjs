import assert from'node:assert/strict';import{mkdtemp,writeFile,readFile,mkdir}from'node:fs/promises';import{resolve}from'node:path';import{createHash}from'node:crypto';import{spawnSync}from'node:child_process';
const root=process.cwd(),script=resolve('test/runtime-cpu-canary/run.mjs'),base=resolve('.local/runtime-canary-fence-test'),hash=createHash('sha256').update('synthetic-account').digest('hex');
await mkdir(base,{recursive:true});
const preload=resolve(base,'fake-network.mjs');
await writeFile(preload,"import{writeFileSync}from'node:fs';const calls=[];globalThis.fetch=async(url,init={})=>{calls.push({url:String(url),method:init.method||'GET'});writeFileSync(process.env.CALLS,JSON.stringify(calls));return new Response(JSON.stringify({success:true,result:{}}),{status:init.method==='DELETE'?200:404});};");
let count=0;
async function check(change,allowed){
 const dir=await mkdtemp(resolve(base,'case-'));await mkdir(resolve(dir,'.local/runtime-cpu-canary'),{recursive:true});const calls=resolve(dir,'calls.json');
 const j={worker:'hub2-cpu-proof-123-a1b2c3',run_id:'123',source_commit:'synthetic-sha',account_sha256:hash,creation_allowed:true,...change};
 await writeFile(resolve(dir,'.local/runtime-cpu-canary/journal.json'),JSON.stringify(j));
 const result=spawnSync(process.execPath,['--import',preload,script,'--cleanup-only'],{cwd:dir,env:{...process.env,CLOUDFLARE_ACCOUNT_ID:'synthetic-account',CLOUDFLARE_API_TOKEN:'synthetic-token',GITHUB_RUN_ID:'123',GITHUB_SHA:'synthetic-sha',CALLS:calls},encoding:'utf8',timeout:10000});
 assert.equal(result.status,allowed?0:1,result.stderr);let observed=[];try{observed=JSON.parse(await readFile(calls,'utf8'));}catch{}
 if(!allowed||change.creation_allowed===false)assert.deepEqual(observed,[]);
 else{const names=change.tail_worker?[change.tail_worker,j.worker]:[j.worker];assert.equal(observed.length,names.length*2);for(let i=0;i<names.length;i++){assert.equal(observed[i*2].method,'DELETE');assert(observed[i*2].url.endsWith('/workers/scripts/'+names[i]+'?force=true'));assert.equal(observed[i*2+1].method,'GET');}}
 count++;
}
await check({worker:'agent-hub'},false);await check({tail_worker:'agent-hub'},false);await check({worker:'hub2-cpu-proof-124-a1b2c3'},false);await check({account_sha256:'wrong'},false);await check({source_commit:'wrong'},false);await check({creation_allowed:1},false);await check({creation_allowed:false},true);await check({},true);await check({tail_worker:'hub2-cpu-proof-123-a1b2c3-tail'},true);
const probe=await import(new URL('../../test/runtime-cpu-canary/probe.mjs',import.meta.url));
const forbidden=await probe.default.fetch(new Request('https://synthetic/smoke'),{PROBE:{get(){throw Error('must not access namespace');}}});assert.equal(forbidden.status,403);count++;
const tail=await import(new URL('../../test/runtime-cpu-canary/tail.mjs',import.meta.url));
const calls=[],receiver={idFromName:x=>x,get:()=>({fetch:async(url,init)=>{calls.push({url,body:JSON.parse(init.body)});return new Response(null,{status:204});}})};
const trace={scriptName:'synthetic-producer',executionModel:'stateless',event:{request:{url:'https://synthetic/worker-burn',headers:{Authorization:'PRIVATE-SYNTHETIC'}}},logs:[{message:['PRIVATE-SYNTHETIC']}],cpuTime:1500,wallTime:1600,outcome:'ok'};
await tail.default.tail([trace,{...trace,event:{request:{url:'https://synthetic/metrics'}}},{...trace,scriptName:'foreign-producer'}],{PRODUCER_NAME:'synthetic-producer',PROBE:receiver});
assert.equal(calls.length,1);assert.equal(calls[0].body.records.length,1);assert.deepEqual(Object.keys(calls[0].body.records[0]).sort(),['kind','path','outcome','cpu_ms','wall_ms'].sort());assert(!JSON.stringify(calls).includes('PRIVATE-SYNTHETIC'));count++;
calls.length=0;await tail.default.tail([{...trace,event:{request:{url:'https://synthetic/metrics'}}}],{PRODUCER_NAME:'synthetic-producer',PROBE:receiver});assert.equal(calls.length,0);count++;
console.log(JSON.stringify({passed:true,tests:count,all_network_stubbed:true,destructive_targets_only_synthetic:true}));
