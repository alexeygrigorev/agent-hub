// Replays an immutable prepared NDJSON plan; no starts, schedules, deletes or automatic archive.
import assert from 'node:assert/strict';
import {createReadStream} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {readConfig,requestJson,privateDirectory,privateJson,shaFile} from './hub2-maintenance-export.mjs';
const digest=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
const textDigest=value=>createHash('sha256').update(value).digest('hex');
export function merkleManifest(lines){
 const digests=lines.map(textDigest),levels=[digests.map(d=>textDigest('leaf:'+d))];
 while(levels.at(-1).length>1){const row=levels.at(-1),next=[];for(let i=0;i<row.length;i+=2)next.push(textDigest('node:'+row[i]+(row[i+1]||row[i])));levels.push(next);}
 const proofs=digests.map((_,index)=>{const proof=[];for(const row of levels.slice(0,-1)){const left=index%2===1;proof.push({side:left?'left':'right',hash:row[left?index-1:Math.min(index+1,row.length-1)]});index=Math.floor(index/2);}return proof;});
 return {merkle_root:levels.at(-1)[0],payload_digests:digests,proofs};
}
async function* planLines(path){
 let pending='';
 for await(const chunk of createReadStream(path,{encoding:'utf8',highWaterMark:65536})){
  pending+=chunk;let newline;
  while((newline=pending.indexOf(String.fromCharCode(10)))!==-1){assert(newline<=8*1024*1024,'plan_line_bound');yield pending.slice(0,newline);pending=pending.slice(newline+1);}
  assert(pending.length<=8*1024*1024,'plan_line_bound');
 }
 if(pending)yield pending;
}
export function expectedTask(task){
 const projection=Object.fromEntries(['id','project','title','text','acceptance_criteria','status','created_at','updated_at','parent_intake_id'].map(k=>[k,task[k]]));
 const events=[...task.events].sort((a,b)=>a.sequence-b.sequence).map(e=>({sequence:e.sequence,at:e.at,legacy_sequence:e.sequence,source_time_unknown:e.source_time_unknown===true,event:e.event}));
 return {projection_sha256:digest(projection),record_sha256:digest(task.record),events_sha256:digest(events),event_count:events.length};
}
export async function importReconcile(config,planPath,manifestPath,output,{verifyOnly=false,archiveManifest,deadline=Date.now()+1200000}={}){
 const manifest=JSON.parse(await readFile(manifestPath,'utf8'));
 assert(manifest.live_state_verified!==true||manifest.project_resolution==='verified-canonical-registry'||['127.0.0.1','localhost','[::1]'].includes(new URL(config.server).hostname),'canonical_registry_resolution_required');
 assert(manifest.live_state_verified===true||['127.0.0.1','localhost','[::1]'].includes(new URL(config.server).hostname),'historical_plan_remote_import_forbidden');
 assert(manifest.version===1&&manifest.queue_effects_enabled===false&&manifest.schedule_effects_enabled===false,'disabled_effects_manifest_required');
 assert.equal(await shaFile(planPath),manifest.plan_sha256,'immutable_plan_checksum');
 await privateDirectory(output);
 const seen=new Set(),checks=[],taskIndex=new Map();let count=0;
 for await(const line of planLines(planPath)){
  assert(Date.now()<deadline&&line.length<=8*1024*1024&&count<100000,'import_execution_bound');
  if(!line.trim())continue;
  const task=JSON.parse(line);assert(task.import_run_id===manifest.import_run_id&&task.source_sha256===manifest.source_sha256&&task.generation===manifest.generation,'source_binding_mismatch');
  assert(!seen.has(task.id),'duplicate_original_id');seen.add(task.id);
  const path='/api/maintenance/task?project='+encodeURIComponent(task.project);
  assert.equal(textDigest(line),manifest.payload_digests[count],'payload_commitment_mismatch');
  if(!verifyOnly){const batch=await requestJson(config,path,{method:'POST',body:{task_json:line,proof:manifest.proofs[count]},deadline});assert(batch.id===task.id&&batch.count===1&&batch.events===task.events.length&&batch.digest===textDigest(line),'batch_digest_or_count_mismatch');}
  const actual=await requestJson(config,path+'&id='+encodeURIComponent(task.id),{deadline}),expected=expectedTask(task);
  assert(actual.manifest_root===manifest.merkle_root&&actual.import_run_id===manifest.import_run_id&&actual.id===task.id&&actual.source_sha256===manifest.source_sha256&&actual.generation===manifest.generation&&actual.dispatch_enabled===false,'actual_id_or_source_mismatch');
  for(const key of Object.keys(expected))assert.equal(actual[key],expected[key],'actual_content_mismatch:'+key);
  checks.push({id:task.id,...expected});taskIndex.set(task.id,{project:task.project,expected});count++;
 }
 assert.equal(count,manifest.task_count,'complete_id_set_required');
 const result={version:1,source_sha256:manifest.source_sha256,generation:manifest.generation,plan_sha256:manifest.plan_sha256,task_count:count,
   per_id_verification:true,queue_effects_enabled:false,schedule_effects_enabled:false,
   archived32_applied:false,other_domains_verified:false,checks_sha256:digest(checks)};
 if(archiveManifest){
  const raw=await readFile(archiveManifest,'utf8'),manifestSha=createHash('sha256').update(raw).digest('hex');
  assert.equal(manifestSha,'98a69e5dd3b64da95e41d0cf23f8dd70bf025c232dde9235e5a41b78e4146e93','authorized_archive_manifest_hash');
  const ids=raw.split(/\r?\n/).map(line=>line.match(/^- \[ \] ([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})/)).filter(Boolean).map(m=>m[1]);
  assert.equal(new Set(ids).size,32,'exact_authorized32_ids');
  const audit={manifest_sha256:manifestSha,candidates:32,per_id_checked:0,unchanged_since_plan:0,missing_source:0,missing_target:0,changed:0,applied:0,semantic_review_required:true};
  for(const id of ids){
   const source=taskIndex.get(id);if(!source){audit.missing_source++;continue;}
   let actual;
   try{actual=await requestJson(config,'/api/maintenance/task?project='+encodeURIComponent(source.project)+'&id='+encodeURIComponent(id),{deadline});}
   catch(error){if(error.message==='transfer_http_404'){audit.missing_target++;continue;}throw error;}
   audit.per_id_checked++;
   if(Object.entries(source.expected).every(([key,value])=>actual[key]===value))audit.unchanged_since_plan++;else audit.changed++;
  }
  result.archive32_audit=audit;
 }
 await privateJson(resolve(output,'reconciliation.json'),result);return result;
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const args=process.argv.slice(2),value=flag=>args[args.indexOf(flag)+1];
 try{for(const flag of ['--config','--plan','--manifest','--output'])assert(args.includes(flag),'required_argument');
 const result=await importReconcile(await readConfig(value('--config')),value('--plan'),value('--manifest'),resolve(value('--output')),{verifyOnly:args.includes('--verify-only'),archiveManifest:args.includes('--archive-manifest')?value('--archive-manifest'):undefined});
 console.log(JSON.stringify(result));
 }catch{console.error('Import/reconciliation refused; source and existing target data are preserved.');process.exitCode=1;}
}
