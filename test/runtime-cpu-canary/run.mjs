// Measurement-only. A fsynced fresh-name journal fences every cleanup.
import assert from 'node:assert/strict';
import {randomBytes,createHash}from'node:crypto';
import {readFile,writeFile,mkdir,open}from'node:fs/promises';
import {resolve}from'node:path';
import {execFile}from'node:child_process';
import {promisify}from'node:util';
const execute=promisify(execFile),root=process.cwd(),out=resolve(root,'.local/runtime-cpu-canary');
await mkdir(out,{recursive:true});
const account=process.env.CLOUDFLARE_ACCOUNT_ID,token=process.env.CLOUDFLARE_API_TOKEN,run=process.env.GITHUB_RUN_ID,source=process.env.GITHUB_SHA;
assert(account&&token&&/^\d+$/.test(run||''),'existing CI Cloudflare auth and run id required');
const digest=value=>createHash('sha256').update(value).digest('hex');
const journalPath=resolve(out,'journal.json'),proofPath=resolve(out,'proof.json'),config=resolve(out,'wrangler.json');
async function durableJson(path,value){const f=await open(path,'w',0o600);try{await f.writeFile(JSON.stringify(value,null,2)+'\n');await f.sync();}finally{await f.close();}}
async function api(path,init={},timeout=5000){
 const response=await fetch('https://api.cloudflare.com/client/v4/accounts/'+account+path,{...init,headers:{Authorization:'Bearer '+token,...init.headers},signal:AbortSignal.timeout(Math.max(1,timeout))});
 const text=await response.text();let body;try{body=JSON.parse(text);}catch{body={};}
 return{status:response.status,body};
}
function validateJournal(j){
 assert(j.creation_allowed===true||j.creation_allowed===false,'invalid creation authorization');
 assert(j.run_id===run&&j.source_commit===source&&j.account_sha256===digest(account),'journal owner/account/source mismatch');
 assert(new RegExp('^hub2-cpu-proof-'+run+'-[a-f0-9]{6}$').test(j.worker),'fresh-name fence');
 if(j.tail_worker!==undefined)assert(j.tail_worker===j.worker+'-tail','tail name fence');
}
async function cleanup(j){
 validateJournal(j);if(!j.creation_allowed)return{passed:true,skipped:'preexisting/inaccessible resource was never authorized'};
 const deadline=Date.now()+70000,attempts=[],names=j.tail_worker?[j.tail_worker,j.worker]:[j.worker];
 let passed=true;
 for(const name of names){
  let absentConfirmed=false;
  for(let n=0;n<3&&Date.now()<deadline;n++){
   try{
    const removed=await api('/workers/scripts/'+name+'?force=true',{method:'DELETE'},Math.min(5000,deadline-Date.now()));
    const absent=await api('/workers/scripts/'+name+'/settings',{},Math.min(5000,deadline-Date.now()));
    attempts.push({worker:name,delete_status:removed.status,absent_status:absent.status});
    if(absent.status===404){absentConfirmed=true;break;}
   }catch{attempts.push({worker:name,transport_error:true});}
   await new Promise(r=>setTimeout(r,1000));
  }
  if(!absentConfirmed)passed=false;
 }
 if(passed)return{passed:true,attempts,only_fresh_name:true};
 return{passed:false,attempts,only_fresh_name:true};
}
if(process.argv.includes('--prepare')){
 const name='hub2-cpu-proof-'+run+'-'+randomBytes(3).toString('hex');
 const j={worker:name,tail_worker:name+'-tail',run_id:run,source_commit:source,account_sha256:digest(account),creation_allowed:false};
 await durableJson(journalPath,j);
 for(const worker of[name,j.tail_worker]){const before=await api('/workers/scripts/'+worker+'/settings');assert.equal(before.status,404,'refuse existing or inaccessible Worker');}
 await durableJson(config,{name,main:resolve(root,'test/runtime-cpu-canary/probe.mjs'),account_id:account,compatibility_date:'2026-11-01',workers_dev:true,tail_consumers:[{service:j.tail_worker}],limits:{cpu_ms:500,subrequests:16},vars:{SOURCE_SHA:source},durable_objects:{bindings:[{name:'PROBE',class_name:'CPUProbe'}]},migrations:[{tag:'synthetic-v1',new_sqlite_classes:['CPUProbe']}]});
 const tailBase={name:j.tail_worker,main:resolve(root,'test/runtime-cpu-canary/tail.mjs'),account_id:account,compatibility_date:'2026-11-01',workers_dev:false,limits:{cpu_ms:500,subrequests:8},vars:{PRODUCER_NAME:name,SOURCE_SHA:source}};
 await durableJson(resolve(out,'tail-base.json'),tailBase);
 await durableJson(resolve(out,'tail-final.json'),{...tailBase,durable_objects:{bindings:[{name:'PROBE',class_name:'CPUProbe',script_name:name}]}});
 await durableJson(resolve(out,'private-secrets.json'),{CANARY_KEY:randomBytes(32).toString('hex')});
 j.config_sha256=digest(await readFile(config));j.probe_sha256=digest(await readFile('test/runtime-cpu-canary/probe.mjs'));j.tail_source_sha256=digest(await readFile('test/runtime-cpu-canary/tail.mjs'));j.tail_config_hashes={};for(const file of['tail-base.json','tail-final.json'])j.tail_config_hashes[file]=digest(await readFile(resolve(out,file)));j.creation_allowed=true;await durableJson(journalPath,j); // Uploaded BEFORE deploy.
 console.log('Prepared fresh, undeployed CPU canary journal');process.exit(0);
}
const j=JSON.parse(await readFile(journalPath,'utf8'));validateJournal(j);
if(process.argv.includes('--cleanup-only')){
 const result=await cleanup(j);await durableJson(resolve(out,'cleanup-proof.json'),{worker:j.worker,source_commit:source,cleanup:result});
 console.log(JSON.stringify({worker:j.worker,cleanup:result}));if(!result.passed)process.exitCode=1;
}else{
 const proof={passed:false,measurement_complete:false,bound_candidate_accepted:false,worker:j.worker,tail_worker:j.tail_worker,source_commit:source,cpu_limit_ms:500,probe_source_sha256:digest(await readFile('test/runtime-cpu-canary/probe.mjs')),production_hub_mutated:false,model_or_provider_credentials_used:false,maximum_burns:4,all_handler_cpu_proven:false,provider_cpu_time_ms_verified:false,proof_scope:'Provider CPU/Wall measurement in isolated synthetic resources. Configuration allows bursts; no strict-500-ms bound, Hub2 workflow or billing acceptance.'};
 const deadline=Date.now()+150000,remaining=()=>{const n=deadline-Date.now();assert(n>0,'bounded canary deadline');return n;};
 const wrangler=resolve(root,'node_modules/wrangler/bin/wrangler.js');
 async function cli(args){
  try{const r=await execute(process.execPath,[wrangler,...args],{cwd:root,env:process.env,timeout:Math.min(90000,remaining()),maxBuffer:4*1024*1024});await writeFile(resolve(out,'last-cli-private.log'),r.stdout+r.stderr,{mode:0o600});}
  catch(error){await writeFile(resolve(out,'last-cli-private.log'),String(error.stdout||'')+String(error.stderr||''),{mode:0o600});throw new Error('wrangler command failed: '+args[0]+' exit '+error.code);}
 }
 let ws;
 try{
  assert(j.creation_allowed);
  const c=JSON.parse(await readFile(config,'utf8'));
  assert(c.name===j.worker&&c.account_id===account&&c.main===resolve(root,'test/runtime-cpu-canary/probe.mjs')&&c.limits?.cpu_ms===500&&!c.routes&&!c.route,'config/journal mismatch');
  assert(digest(await readFile(config))===j.config_sha256,'canary config changed after prepare');
  assert(proof.probe_source_sha256===j.probe_sha256,'canary source changed after prepare');
  assert(digest(await readFile('test/runtime-cpu-canary/tail.mjs'))===j.tail_source_sha256,'tail source changed after prepare');
  for(const file of['tail-base.json','tail-final.json']){
   const path=resolve(out,file),tail=JSON.parse(await readFile(path,'utf8'));
   assert(tail.name===j.tail_worker&&tail.account_id===account&&tail.main===resolve(root,'test/runtime-cpu-canary/tail.mjs')&&tail.workers_dev===false&&tail.limits?.cpu_ms===500&&!tail.routes&&!tail.route,'tail config/journal mismatch');
   assert(digest(await readFile(path))===j.tail_config_hashes[file],'tail config changed after prepare');
  }
  await cli(['deploy','--config',resolve(out,'tail-base.json'),'--tag',source.slice(0,12)]);
  await cli(['deploy','--config',config,'--tag',source.slice(0,12),'--message','CPU runtime measurement source_commit='+source]);
  await cli(['deploy','--config',resolve(out,'tail-final.json'),'--tag',source.slice(0,12)]);
  await cli(['secret','bulk',resolve(out,'private-secrets.json'),'--config',config]);
  const settings=await api('/workers/scripts/'+j.worker+'/settings');assert.equal(settings.status,200);assert.equal(settings.body.result?.limits?.cpu_ms,500);
  proof.provider_limits=settings.body.result.limits;
  const subdomain=await api('/workers/subdomain');assert.equal(subdomain.status,200);
  const base='https://'+j.worker+'.'+subdomain.body.result.subdomain+'.workers.dev';proof.url=base;
  const key=JSON.parse(await readFile(resolve(out,'private-secrets.json'),'utf8')).CANARY_KEY;
  async function request(path){const started=performance.now();const response=await fetch(base+path,{headers:{Authorization:'Bearer '+key,'User-Agent':'hub2-cpu-runtime/'+source},signal:AbortSignal.timeout(Math.min(8000,remaining()))});return{status:response.status,text:await response.text(),round_trip_ms:Math.ceil(performance.now()-started)};}
  const observation=result=>{let body;try{body=JSON.parse(result?.text||'');}catch{}return{status:result?.status,round_trip_ms:result?.round_trip_ms,cpu_signature:/1102|(?<![A-Za-z])CPU(?![A-Za-z])[^".]{0,80}(?:limit|exceed)|resource limits?/i.test(result?.text||''),completed:body?.completed===true,error:body?.probe_error??body?.error??null,secret_bound:body?.secret_bound,source_matches:body?.source===source,edge_error_codes:[...String(result?.text||'').matchAll(/\b(10\d\d|11\d\d)\b/g)].map(m=>m[1]).slice(0,4)};};
  let smoke;
  for(let i=0;i<6;i++){try{smoke=await request('/smoke');if(smoke.status===200)break;}catch{}remaining();await new Promise(r=>setTimeout(r,1000));}
  proof.smoke_observation=observation(smoke);
  const secretsReadback=await api('/workers/scripts/'+j.worker+'/secrets');proof.secret_binding_readback={status:secretsReadback.status,names:Array.isArray(secretsReadback.body.result)?secretsReadback.body.result.map(v=>v.name):[]};
  assert.equal(smoke?.status,200);assert.equal(JSON.parse(smoke.text).source,source);assert.equal((await request('/do-smoke')).status,200);
  const workerBurn=await request('/worker-burn'),doBurn=await request('/do-burn');

  proof.http_probe_observations={worker:observation(workerBurn),durable_object:observation(doBurn)};
  proof.http_burns={worker_status:workerBurn.status,worker_cpu_limit_observed:observation(workerBurn).cpu_signature,do_status:doBurn.status,do_cpu_limit_observed:observation(doBurn).cpu_signature};
  const {default:WebSocket}=await import('ws');
  ws=new WebSocket(base.replace('https:','wss:')+'/ws',{headers:{Authorization:'Bearer '+key,'User-Agent':'hub2-cpu-runtime/'+source}});
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('WebSocket open timeout')),Math.min(10000,remaining()));ws.once('open',()=>{clearTimeout(timer);resolve();});ws.once('error',e=>{clearTimeout(timer);reject(e);});});
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('WebSocket smoke timeout')),Math.min(5000,remaining()));ws.once('message',data=>{clearTimeout(timer);try{assert.equal(JSON.parse(String(data)).ok,true);resolve();}catch(e){reject(e);}});ws.send('smoke');});
  ws.send('burn');await new Promise(r=>setTimeout(r,2000));
  let state=JSON.parse((await request('/do-status')).text);assert.equal(state['burn:ws'],1);proof.ws_burn_completed=state['ws:completed']===1;
  ws.close(1000,'probe complete');ws.terminate();ws=undefined;
  assert.equal((await request('/do-arm')).status,200);
  for(let i=0;i<20;i++){remaining();await new Promise(r=>setTimeout(r,2000));const result=await request('/do-status');if(result.status!==200)continue;state=JSON.parse(result.text);if(state['alarm:recovered']||state['alarm:completed'])break;}
  assert.equal(state['burn:worker'],1);assert.equal(state['burn:do'],1);assert.equal(state['alarm:started'],1);proof.alarm_burn_completed=state['alarm:completed']===1;proof.alarm_recovered=state['alarm:recovered']===1;
  proof.persisted_probe_state=state;proof.alarm_ws_observations='Completion/interruption is recorded without inferring CPU time. Provider trace fields below are authoritative measurements; no strict-limit acceptance.';
  assert.equal((await request('/smoke')).status,200);assert.equal((await request('/do-smoke')).status,200);
  for(let i=0;i<12;i++){state=JSON.parse((await request('/do-status')).text);if((state.tail||[]).some(v=>v.kind==='worker_http'&&v.path==='/worker-burn')&&(state.tail||[]).some(v=>v.kind==='do_http'&&v.path==='/burn'))break;await new Promise(r=>setTimeout(r,1000));}
  proof.provider_trace_records=state.tail||[];
  proof.provider_cpu_time_ms_verified=proof.provider_trace_records.some(v=>v.kind==='worker_http'&&v.path==='/worker-burn')&&proof.provider_trace_records.some(v=>v.kind==='do_http'&&v.path==='/burn');
  assert(proof.provider_cpu_time_ms_verified,'provider CPU trace records required, no JS clock substitute');
  proof.successful_over_limit=proof.provider_trace_records.filter(v=>v.outcome==='ok'&&v.cpu_ms>500);
  proof.measurement_complete=true;proof.passed=true;

 }catch(error){proof.error=String(error?.message||error);process.exitCode=1;}
 finally{
  ws?.terminate();
  try{proof.cleanup=await cleanup(j);if(!proof.cleanup.passed){proof.passed=false;process.exitCode=1;}}catch{proof.cleanup={passed:false,unexpected_cleanup_error:true};proof.passed=false;process.exitCode=1;}
  await durableJson(proofPath,proof);console.log(JSON.stringify(proof));
 }
}
