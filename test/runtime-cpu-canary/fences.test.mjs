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
 else{assert.equal(observed.length,2);assert.equal(observed[0].method,'DELETE');assert(observed[0].url.endsWith('/workers/scripts/hub2-cpu-proof-123-a1b2c3?force=true'));assert.equal(observed[1].method,'GET');}
 count++;
}
await check({worker:'agent-hub'},false);await check({worker:'hub2-cpu-proof-124-a1b2c3'},false);await check({account_sha256:'wrong'},false);await check({source_commit:'wrong'},false);await check({creation_allowed:1},false);await check({creation_allowed:false},true);await check({},true);
const probe=await import(new URL('../../test/runtime-cpu-canary/probe.mjs',import.meta.url));
const forbidden=await probe.default.fetch(new Request('https://synthetic/smoke'),{PROBE:{get(){throw Error('must not access namespace');}}});assert.equal(forbidden.status,403);count++;
console.log(JSON.stringify({passed:true,tests:count,all_network_stubbed:true,destructive_targets_only_synthetic:true}));
