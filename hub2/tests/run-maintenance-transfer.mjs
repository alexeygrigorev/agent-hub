// Actual local workerd SQL/HTTP import/export. All data and authorities are synthetic.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {resolve} from 'node:path';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import {gunzipSync} from 'node:zlib';
import {exportSnapshot,privateJson,requestJson} from '../../scripts/hub2-maintenance-export.mjs';
import {importReconcile,merkleManifest} from '../../scripts/hub2-import-reconcile.mjs';
const root=resolve(import.meta.dirname,'../..'),admin='synthetic-maintenance-admin-000000000000000000',exportToken='synthetic-export-authority-00000000000000000',importToken='synthetic-import-authority-00000000000000000';
const sourceFiles=['hub2/production-worker.ts','scripts/hub2-maintenance-export.mjs','scripts/hub2-import-reconcile.mjs'];
const hash=async path=>createHash('sha256').update(await readFile(resolve(root,path))).digest('hex');
const before=await Promise.all(sourceFiles.map(hash));
const bundle=await build({entryPoints:[resolve(root,'hub2/tests/maintenance-transfer-worker.ts')],bundle:true,write:false,platform:'browser',format:'esm',external:['cloudflare:workers','node:async_hooks']});
await mkdir(resolve(root,'.local/tmp'),{recursive:true});
const temp=await mkdtemp(resolve(root,'.local/tmp/maintenance-transfer-')),persist=resolve(temp,'state'),runId=randomUUID();
const classes={HUB:'LegacyHub',CONTROL:'ControlDO',PROJECTS:'ProjectDO',MACHINES:'MachineDO',ACCOUNTS:'AccountDO',MAILBOXES:'MailboxDO',BUDGET:'BudgetDO'};
const options=(pinnedManifest='')=>convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{name:'maintenance-transfer-fixture',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_als'],durableObjects:Object.fromEntries(Object.entries(classes).map(([key,className])=>[key,{className,useSQLite:true}])),bindings:{HUB2_RELEASE_MODE:'maintenance',HUB1_EXPORT_TOKEN:exportToken,HUB2_IMPORT_TOKEN:importToken,HUB1_EXPORT_GENERATION:'synthetic-generation',HUB2_IMPORT_MANIFEST:pinnedManifest,HUB2_CREDENTIAL_KEY:'91'.repeat(32),HUB2_DEV_OPERATOR_TOKEN:admin,HUB2_DEFAULT_ACCOUNT_ID:'synthetic-account'}}]});
let mf=new Miniflare(options()),base=(await mf.ready).origin;
const at=1790000000000;
const indexSequence=(record,index)=>record._history_order?.[record.history[index].id]??index;
const originals=['11111111-2222-4333-8444-555555555551','11111111-2222-4333-8444-555555555552'].map((id,index)=>({id,project:'synthetic/project',title:index?'Recreated task':'Original task',status:index?'open':'done',version:3,created_at:new Date(at).toISOString(),updated_at:new Date(at+1000).toISOString(),acceptance_criteria:'First AC\nSecond AC',superseded_by:index?null:'11111111-2222-4333-8444-555555555552',history:[{id:id+'-ask',kind:'intake',text:'Verbatim original ask '+index,actor:'synthetic-user',captured_at:new Date(at).toISOString()},{id:id+'-note',kind:'note',text:'retained note',actor:'synthetic-user',captured_at:new Date(at+500).toISOString()}]}));
const archiveEvent={id:'synthetic-archive-note',kind:'note',text:'Verbatim archived history',actor:'synthetic-user',captured_at:new Date(at+250).toISOString()};
originals[0].history_archive={count:1,href:'synthetic-history'};
originals[0]._history_order=Object.fromEntries(originals[0].history.map((e,i)=>[e.id,i*2]));
originals[0]._history_next_seq=3;originals[1].history[0].parent_intake_id=originals[0].id;
try{
 assert.equal((await fetch(base+'/api/repos')).status,503,'new work blocked in maintenance');
 assert.equal((await fetch(base+'/api/backup/status')).status,401,'backup authority required');
 await requestJson({server:base,token:admin},'/api/maintenance/task?project=synthetic/project',{method:'POST',body:{}}).then(()=>assert.fail('wrong authority'),e=>assert.match(e.message,/403/));
 const seeded=await fetch(base+'/_fixture/seed',{method:'POST',headers:{Authorization:'Bearer '+admin,'Content-Type':'application/json'},body:JSON.stringify({records:originals,archives:[{id:'synthetic-archive-row',task_id:originals[0].id,seq:1,event:archiveEvent}]})});assert.equal(seeded.status,200);
 let mutation,mutateOnPage=true;const nativeFetch=globalThis.fetch;
 globalThis.fetch=async(input,init)=>{
  const response=await nativeFetch(input,init),url=new URL(typeof input==='string'?input:input.url);
  if(mutateOnPage&&url.pathname==='/api/backup/export'&&url.searchParams.get('table')==='intake_task_projection'&&url.searchParams.get('offset')==='0'){
   mutateOnPage=false;
   const changed=await nativeFetch(base+'/_fixture/mutate-same-count',{headers:{Authorization:'Bearer '+admin}});
   assert.equal(changed.status,200);mutation=await changed.json();
  }
  return response;
 };
 let exported;
 try{exported=await exportSnapshot({server:base,token:exportToken},resolve(temp,'export'),{chunkRows:1});}finally{globalThis.fetch=nativeFetch;}
 assert.deepEqual(mutation,{before:2,after:2,content_changed:true},'same-count source mutation injected during pagination');
 assert.equal(exported.total_rows,5);assert.equal(Object.keys(exported.tables).length,4);
 assert.equal(exported.row_counts_stable,true);assert.equal(exported.live_state_verified,false);
 assert.equal(exported.consistency,'unverified_row_counts_only');assert.equal(exported.server_reported_writes_disabled,true);
 const raw=JSON.parse(gunzipSync(await readFile(resolve(temp,'export/export.json.gz'))));assert.equal(raw.tables.intake_tasks.length,2);
 assert.equal(raw.tables.intake_task_projection[0].links,'[]','export captured pre-mutation first page, proving a mixed snapshot can keep row counts stable');
 const tasks=originals.map(record=>({import_run_id:runId,source_sha256:exported.sha256,generation:'synthetic-generation',id:record.id,project:record.project,parent_intake_id:record.history[0].parent_intake_id||null,live_state_verified:true,record,title:record.title,text:record.history[0].text,acceptance_criteria:[record.acceptance_criteria],status:record.status==='done'?'accepted':'open',created_at:at,updated_at:at+1000,imported_at:Date.now(),events:record.history.map((event,sequence)=>({sequence:indexSequence(record,sequence),event,at:Date.parse(event.captured_at),source_time_unknown:false}))}));
 tasks[0].events.push({sequence:1,event:archiveEvent,at:at+250,source_time_unknown:false});
 const plan=resolve(temp,'tasks.ndjson');await writeFile(plan,tasks.map(t=>JSON.stringify(t)).join('\n')+'\n',{mode:0o600});
 const commitment=merkleManifest(tasks.map(t=>JSON.stringify(t)));
 const manifestObject={version:1,import_run_id:runId,source_sha256:exported.sha256,generation:'synthetic-generation',plan_sha256:await hash(plan),task_count:2,live_state_verified:true,queue_effects_enabled:false,schedule_effects_enabled:false,...commitment};
 const manifest=resolve(temp,'manifest.json');await privateJson(manifest,manifestObject);
 const pinned={...Object.fromEntries(['version','source_sha256','generation','import_run_id','task_count','merkle_root'].map(k=>[k,manifestObject[k]])),
  live_state_verified:manifestObject.live_state_verified,project_resolution:'verified-canonical-registry'};
 const config={server:base,token:importToken};
 const unverified=resolve(temp,'unverified-manifest.json');await privateJson(unverified,{...manifestObject,live_state_verified:false,project_resolution:'verified-canonical-registry'});
 await assert.rejects(()=>importReconcile({server:'https://never-contact.invalid',token:importToken},plan,unverified,resolve(temp,'must-not-create-unverified')),/historical_plan_remote_import_forbidden/);
 const unresolved=resolve(temp,'unresolved-project-manifest.json');await privateJson(unresolved,{...manifestObject,live_state_verified:true,project_resolution:'literal-source-awaits-canonical-registry-reconciliation'});
 await assert.rejects(()=>importReconcile({server:'https://never-contact.invalid',token:importToken},plan,unresolved,resolve(temp,'must-not-create-unresolved')),/canonical_registry_resolution_required/);
 for(const badPin of [{...pinned,live_state_verified:false},{...pinned,project_resolution:'literal-source-awaits-canonical-registry-reconciliation'}]){
  await mf.dispose();mf=new Miniflare(options(JSON.stringify(badPin)));base=(await mf.ready).origin;
  await assert.rejects(()=>requestJson({server:base,token:importToken},'/api/maintenance/task?project=synthetic/project',{method:'POST',body:{task_json:JSON.stringify(tasks[0]),proof:commitment.proofs[0]}}),/403/,'server rejects an unverified or noncanonical pinned import manifest');
 }
 await mf.dispose();mf=new Miniflare(options(JSON.stringify(pinned)));base=(await mf.ready).origin;config.server=base;
 const registry=await fetch(base+'/_fixture/public/api/repos/'+encodeURIComponent('synthetic/project'),{method:'PATCH',headers:{Authorization:'Bearer '+admin,'Content-Type':'application/json'},body:JSON.stringify({canonical_url:'https://github.com/synthetic/project',purpose:'Synthetic transfer registry prerequisite',status:'active',aliases:[]})});
 assert.equal(registry.status,200,'actual canonical registry setup: '+JSON.stringify(await registry.clone().json()));
 const historicalManifest=resolve(temp,'historical.json');await privateJson(historicalManifest,{...manifestObject,live_state_verified:false});
 await assert.rejects(()=>importReconcile({server:'https://never-contact.invalid',token:importToken},plan,historicalManifest,resolve(temp,'must-not-create')),/historical_plan_remote_import_forbidden/);
 const result=await importReconcile(config,plan,manifest,resolve(temp,'import-first'));assert.equal(result.task_count,2);
 await importReconcile(config,plan,manifest,resolve(temp,'import-replay'));
 await importReconcile(config,plan,manifest,resolve(temp,'verify-only'),{verifyOnly:true});
 await assert.rejects(()=>requestJson(config,'/api/maintenance/task?project=synthetic/project',{method:'POST',body:{task_json:JSON.stringify({...tasks[0],title:'conflicting rewrite'}),proof:commitment.proofs[0]}}),/403/);
 await assert.rejects(()=>requestJson(config,'/api/maintenance/task?project=foreign/project',{method:'POST',body:{task_json:JSON.stringify(tasks[0]),proof:commitment.proofs[0]}}),/400/);
 await assert.rejects(()=>requestJson(config,'/api/maintenance/task?project=synthetic/project',{method:'POST',body:{task_json:JSON.stringify(tasks[0]),proof:commitment.proofs[0].map(v=>({...v,hash:'0'.repeat(64)}))}}),/403/);
 const publicGet=await fetch(base+'/_fixture/public/api/intakes/'+tasks[0].id+'?project=synthetic%2Fproject',{headers:{Authorization:'Bearer '+admin}});
 assert.equal(publicGet.status,200,'actual public Hub2 intake lookup resolves original ID: '+JSON.stringify(await publicGet.clone().json()));
 const history=await fetch(base+'/_fixture/public/api/intakes/'+tasks[0].id+'/history?project=synthetic%2Fproject',{headers:{Authorization:'Bearer '+admin}});
 assert.equal(history.status,200);assert.equal((await history.json()).events.length,3);
 const state=await(await fetch(base+'/_fixture/check',{headers:{Authorization:'Bearer '+admin}})).json();assert.deepEqual(state,{intakes:2,sagas:0,events:5,alarm:null});
 assert.deepEqual(await Promise.all(sourceFiles.map(hash)),before,'source stable');
 console.log(JSON.stringify({passed:true,exported_tables:4,original_ids:2,events:5,idempotent_replay:true,per_id_hashes:true,public_history:true,conflict_refused:true,wrong_project_refused:true,dispatch_sagas:0,project_alarm:null,source_hashes:Object.fromEntries(sourceFiles.map((p,i)=>[p,before[i]]))}));
}finally{await mf.dispose();await rm(temp,{recursive:true,force:true});}
