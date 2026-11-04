// Real local workerd/SQLite slice. No provider requests or native process launch.
// Quota and native evidence are explicitly synthetic fixtures; live host proof is separate.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {WebSocket} from 'ws';
import {resolve} from 'node:path';
import {mkdtemp,readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const root=process.env.HUB2_CORE_ROOT||resolve(import.meta.dirname,'../..');
const entry=process.env.HUB2_CORE_ENTRY||resolve(root,'hub2/tests/core-integration-worker.ts');
const reportPath=process.env.HUB2_CORE_REPORT;
const names={control:'CoreControl',project:'CoreProject',machine:'CoreMachine',account:'CoreAccount',mailbox:'CoreMailbox'};
const token='synthetic-core-operator-token-000000000000000000',project='synthetic/core',machine='synthetic-machine',account='synthetic-account';
const sourceFiles=['worker','project-do','machine-do','control-do','account-do','mailbox-do','db','types','auth','control-auth','worker-credentials','credential-crypto','read-cache','mcp','routing','project-work','cron'];
async function hashes(){return Object.fromEntries(await Promise.all(sourceFiles.map(async n=>[n,createHash('sha256').update(await readFile(resolve(root,`hub2/src/${n}.ts`))).digest('hex')])));}
const sourceBefore=await hashes();
const bundle=await build({entryPoints:[entry],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
await mkdir(resolve(root,'.local/tmp'),{recursive:true});const persist=await mkdtemp(resolve(root,'.local/tmp/core-integration-'));
const workspace=process.env.HUB2_CORE_WORKER_CONTEXT||process.env.HUB2_CORE_PROVISION?resolve(persist,'workspace'):'/synthetic/workspace';if(process.env.HUB2_CORE_WORKER_CONTEXT||process.env.HUB2_CORE_PROVISION)await mkdir(workspace,{recursive:true,mode:0o700});
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{name:'hub2-core-native-fixture',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',durableObjects:Object.fromEntries(Object.entries(names).map(([k,className])=>[({control:'CONTROL',project:'PROJECTS',machine:'MACHINES',account:'ACCOUNTS',mailbox:'MAILBOXES'})[k],{className,useSQLite:true}])),bindings:{HUB2_FIXTURE_CODEX_PROVIDER:process.env.HUB2_CORE_PROVISION?'yes':'',HUB2_DEV_OPERATOR_TOKEN:token,HUB2_DEFAULT_ACCOUNT_ID:account,HUB2_ENGINE_ACCOUNTS:JSON.stringify({opencode:account}),HUB2_CREDENTIAL_KEY:'11'.repeat(32),HUB2_FIXTURE_ASSET_HTML:await readFile(resolve(root,'hub2/public/index.html'),'utf8')}}]});
let mf=new Miniflare(options),base,hostToken,liveSocket,completedIntakeId;const results=[],measurements={};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function api(path,method='GET',body,auth=token,extra={}){
 const response=await fetch(base+path,{method,headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+auth}:{}),...extra},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const text=await response.text();let data;try{data=JSON.parse(text);}catch{data={error:'non_json_response'};}
 return {status:response.status,data};
}
function expect(result,status,label){assert.equal(result.status,status,`${label}: HTTP ${result.status}, error=${result.data.error||result.data.reason||''}`);return result.data;}
const fixture=(kind,id,action='state',body)=>api(`/_fixture/${kind}/${encodeURIComponent(id)}/${action}`,body===undefined?'GET':'POST',body);
async function snapshots(){const out={};for(const [kind,id] of [['control','global'],['project',project],['machine',machine],['account',account],...(process.env.HUB2_CORE_PUBLIC_MAIL?[['mailbox','global']]:[])] ){const name=kind==='mailbox'?kind+':'+id:kind;out[name]=expect(await fixture(kind,id),200,kind+' state');assert.equal(out[name].counts.measurement_valid,true,kind+' invalid native meter');}return out;}
function delta(before,after){return Object.fromEntries(Object.keys(before).map(k=>[k,{reads:after[k].counts.rows_read-before[k].counts.rows_read,writes:after[k].counts.rows_written-before[k].counts.rows_written,alarm_calls:after[k].counts.alarm_operations-before[k].counts.alarm_operations}]));}
async function test(name,fn){try{await fn();results.push({name,passed:true});}catch(e){results.push({name,passed:false,error:String(e.message),stack:String(e.stack).split('\n').slice(0,6).join('\n')});}}
async function intake(key,title='Synthetic core lifecycle'){
 const body={title,text:'Explicitly synthetic workerd integration fixture; no native model is launched.',acceptance_criteria:['Synthetic fixture output is received and remains awaiting independent review.'],key};
 return {body,response:await api('/api/intakes?project='+encodeURIComponent(project),'POST',body)};
}
async function start(id,key,targetMachine=machine,engine='opencode'){return api(`/api/intakes/${id}/start?project=${encodeURIComponent(project)}`,'POST',{machine_id:targetMachine,workspace,engine,override_reason:'Deterministic synthetic integration fixture requests explicit engine and host',interactive:false,timeout_seconds:60,key,...(engine==='shell'?{reason:'Explicit synthetic transport-security test, no process launch'}:{})});}
async function socket(auth=hostToken){
 const frames=[],socket=new WebSocket(base.replace(/^http/,'ws')+'/api/connect',{headers:{Authorization:'Bearer '+auth}});
 socket.on('message',raw=>{try{frames.push(JSON.parse(raw.toString()));}catch{}});
 await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('websocket connection timeout')),5000);socket.once('open',()=>{clearTimeout(timer);resolve();});socket.once('error',e=>{clearTimeout(timer);reject(e);});});
 return {socket,frames,async wait(predicate){const until=Date.now()+5000;while(Date.now()<until){const match=frames.find(predicate);if(match)return match;await sleep(10);}throw new Error('expected WebSocket frame absent: '+JSON.stringify(frames.map(v=>v.type)));}};
}
try{
 base=(await mf.ready).origin;
 await test('unauthenticated browser can load the real Connect page',async()=>{const response=await fetch(base+'/');assert.equal(response.status,200,'public assets must precede API authentication');assert((await response.text()).includes('<title>Hub 2'));});
 await test('public auth/internal path/CSRF refusals',async()=>{
  expect(await api('/health','GET',undefined,null),200,'health');
  expect(await api('/api/repos','GET',undefined,null,{'x-hub2-actor':JSON.stringify({id:'forged',kind:'operator',scopes:['*']})}),401,'forged actor');
  expect(await api('/_internal/reserve','POST',{}),404,'private path');
  expect(await api('/api/repos','PATCH',{},token,{Origin:'https://cross-site.invalid'}),403,'CSRF');
 });
 await test('missing native row counters invalidate measurement instead of fabricating zero',async()=>{const data=expect(await fixture('control','global','meter-invalid'),200,'invalid meter fixture');assert.equal(data.counts.measurement_valid,false);assert.equal(data.counts.measurement_errors,1);});
 await test('real one-use machine enrollment and heartbeat',async()=>{
  expect(await api('/api/repos/'+encodeURIComponent(project),'PATCH',{canonical_url:'https://github.com/synthetic/core',purpose:'Synthetic native integration fixture',status:'active',aliases:['synthetic-alias'],domains:[]}),200,'register canonical project');
  const pair=expect(await api('/api/enrollments/pair','POST',{machine_id:machine,alias:'Synthetic machine'}),201,'pair machine');
  const enrolled=expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),201,'complete pairing');hostToken=enrolled.token;assert.equal(typeof hostToken,'string');
  expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),410,'one-use pairing');
  expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'synthetic',labels:['fixture'],engines:['opencode','shell'],capabilities:['headless','interactive']},hostToken),200,'heartbeat');
  expect(await api('/api/account','PATCH',{provider:process.env.HUB2_CORE_PROVISION?'codex':'synthetic-fixture',enabled:true,capacity:2,min_remaining:10}),200,'configure synthetic account');
  if(process.env.HUB2_CORE_PROVISION){const access='fixture.'+Buffer.from(JSON.stringify({exp:Math.floor(Date.now()/1000)+864000})).toString('base64url')+'.signature';expect(await api('/api/account/credentials','PUT',{expected_generation:0,files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:access,refresh_token:'synthetic-native-fixture',account_id:'fixture-id'},last_refresh:new Date().toISOString()})}}),200,'encrypted synthetic provider fixture');expect(await api('/api/account/discovery','POST',{}),200,'actual provider collector over synthetic HTTP');}
  else expect(await api('/api/account/quota','PUT',{remaining:80,captured_at:Date.now(),valid_until:Date.now()+300000,source:'synthetic-native-integration-fixture'}),200,'synthetic quota');
  expect(await api('/api/account/hosts','PUT',{hosts:[machine]}),200,'approve synthetic account host');
  expect(await api('/api/model-policy','PUT',{expected_version:0,candidates:[{id:'synthetic-model',engine:'opencode',model:'synthetic-test-model',account_id:account,weight:1,capabilities:[],enabled:true}]}),200,'configure actual synthetic model policy');
  expect(await api('/api/account','PATCH',{provider:'forbidden',enabled:true,capacity:1},hostToken,{'x-hub2-actor':JSON.stringify({id:'forged',kind:'operator',scopes:['*']})}),403,'machine cannot edit account');
 });
 await test('intake idempotency and acceptance criteria guard',async()=>{
  const first=await intake('intake-dedup');const created=expect(first.response,201,'create intake');
  const replay=expect(await api('/api/intakes?project='+encodeURIComponent(project),'POST',first.body),200,'replay intake');assert.equal(replay.intake.id,created.intake.id);
  expect(await api('/api/intakes?project='+encodeURIComponent(project),'POST',{...first.body,text:'changed under same key'}),409,'intake digest conflict');
  expect(await api('/api/intakes?project='+encodeURIComponent(project),'POST',{...first.body,key:'empty-ac',acceptance_criteria:[]}),400,'empty AC');
 });
 await test('reserve/activate/offer/admission/started/finish through real edge',async()=>{
  assert(hostToken,'enrollment prerequisite failed');const before=await snapshots();
  const created=expect((await intake('intake-main')).response,201,'lifecycle intake').intake;
  liveSocket=await socket();
  const started=expect(await start(created.id,'start-main'),201,'start lifecycle').session;assert.equal(typeof started.id,'string');assert.equal(started.state,'offered');
  const offered=await liveSocket.wait(v=>v.type==='session.offer'&&v.offer.session_id===started.id);const offer=offered.offer;
  assert.equal(offer.operation_id,started.attempt_operation_id);assert.equal(offer.epoch,started.epoch);assert(offer.acceptance_criteria.length);
  const again=expect(await start(created.id,'start-main'),200,'start retry');assert.equal(again.session.id,started.id);
  const q=expect(await api('/api/account/quota'),200,'account reserved');assert.equal(q.account.occupied,1);
  const bad={epoch:offer.epoch+1,operation_id:offer.operation_id};expect(await api(`/api/sessions/${started.id}/admission`,'POST',bad,hostToken),409,'stale epoch');
  liveSocket.socket.send(JSON.stringify({type:'offer_ack',session_id:started.id,epoch:offer.epoch}));await liveSocket.wait(v=>v.type==='ack'&&v.session_id===started.id);
  const native='11111111-2222-4333-8444-555555555555';
  expect(await api(`/api/sessions/${started.id}/started`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch,native_id:native},hostToken),409,'started before admission');
  const admission=expect(await api(`/api/sessions/${started.id}/admission`,'POST',{epoch:offer.epoch,operation_id:offer.operation_id},hostToken),200,'host admission');assert.equal(admission.launch_allowed,true);assert.equal(admission.epoch,offer.epoch);
  expect(await api(`/api/sessions/${started.id}/started`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch+1,native_id:native},hostToken),409,'stale started receipt');
  let workerCheck;if(process.env.HUB2_CORE_WORKER){const {prepareWorkerProof}=await import('./normal-day-worker-credentials.mjs');workerCheck=await prepareWorkerProof({api,expect,fixture,socket,intake,start,project,machine,hostToken,session:started,offer,intakeId:created.id,root,persist,base});}
  expect(await fixture('project',project,'fault',{paths:['/_internal/started']}),200,'lose successful started forwarding response');
  expect(await api(`/api/sessions/${started.id}/started`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch,native_id:native},hostToken),202,'started durable pending response');
  expect(await fixture('machine',machine,'due-outbox',{id:'started:'+offer.operation_id}),200,'retry started durable journal');
  expect(await api(`/api/sessions/${started.id}/started`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch,native_id:native},hostToken),200,'started retry');
  const live=expect(await api(`/api/intakes/${created.id}?project=${encodeURIComponent(project)}`),200,'running intake');assert.equal(live.intake.status,'running');
  if(process.env.HUB2_CORE_PUBLIC_MAIL){const {publicNativeMail}=await import('./normal-day-public-mail.mjs');measurements.public_mail=await publicNativeMail({api,expect,connection:liveSocket,session:started,hostToken,intakeId:created.id});}
  const evidence={source:'aplexer_retirement',fixture:true,artifact:'synthetic-artifact.txt',independent_acceptance:false,native_identity:{id:native,tag:'task-hub2-'+started.id,workspace:offer.launch.workspace},containment_empty:true,native_exit:{code:0}};
  const finish={operation_id:offer.operation_id,epoch:offer.epoch,native_id:native,exit_code:0,evidence,native_exit_confirmed:true};
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',{...finish,native_exit_confirmed:false},hostToken),400,'unproved exit');
  const contradicted=await api(`/api/sessions/${started.id}/finish`,'POST',{...finish,evidence:{...evidence,native_exit:{code:1}}},hostToken);assert([400,409].includes(contradicted.status),'contradictory native exit evidence accepted');
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',{...finish,epoch:finish.epoch+1},hostToken),409,'stale finish epoch');
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',{...finish,operation_id:'stale-attempt'},hostToken),409,'stale finish operation');
  assert.equal(expect(await api('/api/account/quota'),200,'unqualified exit retains reservation').account.occupied,1);
  expect(await fixture('project',project,'fault',{paths:['/_internal/finish']}),200,'lose successful finish forwarding response');
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',finish,hostToken),202,'finish durable pending response');
  expect(await fixture('machine',machine,'due-outbox',{id:'finish:'+offer.operation_id}),200,'retry finish durable journal');
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',finish,hostToken),200,'finish retry');
  if(workerCheck)expect(await fixture('machine',machine,'due-outbox',{id:'finish:'+offer.operation_id}),200,'retry second independent lost Project finish response after revoked worker replay');
  expect(await api(`/api/sessions/${started.id}/finish`,'POST',{...finish,exit_code:1},hostToken),409,'finish conflict');
  if(workerCheck)measurements.worker_credentials=await workerCheck.afterFinish();
  const final=expect(await api(`/api/intakes/${created.id}?project=${encodeURIComponent(project)}`),200,'final intake');assert.equal(final.intake.status,'completed-awaiting-review');assert.notEqual(final.intake.status,'done');completedIntakeId=created.id;
  assert.equal(expect(await api('/api/account/quota'),200,'account released').account.occupied,0,'native completion must release exact reservation');
  const offers=expect(await api('/api/offers','GET',undefined,hostToken),200,'no finished offer');assert.equal(offers.offers.length,0);
  liveSocket.socket.close();liveSocket=null;
  const after=await snapshots();measurements.complete_flow=delta(before,after);measurements.quiescent=after;const total=Object.values(measurements.complete_flow).reduce((n,v)=>n+v.reads+v.writes,0);assert(total<2000,'warm core flow exceeds2000row guard');
  measurements.machine_deadlines=expect(await fixture('machine',machine,'machine-deadlines'),200,'exact machine pending work diagnostics');
  assert.equal(after.project.alarm,null,'finished project has no pending alarm');assert.equal(after.machine.alarm,null,'finished machine has no pending alarm');assert.equal(after.account.alarm,null,'released account has no pending alarm');
  const quietBefore=await snapshots();await sleep(120);const quietAfter=await snapshots();for(const k of Object.keys(quietBefore)){assert.equal(quietAfter[k].counts.rows_read,quietBefore[k].counts.rows_read,k+' idle reads');assert.equal(quietAfter[k].counts.rows_written,quietBefore[k].counts.rows_written,k+' idle writes');}
 });
 await test('lost reserve and activation responses replay one operation and epoch',async()=>{
  const target='synthetic-fault-machine';
  const pair=expect(await api('/api/enrollments/pair','POST',{machine_id:target,alias:'Synthetic lost-response fixture'}),201,'fault pairing');
  const machineToken=expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),201,'fault enrollment').token;
  expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'synthetic',labels:['fixture']},machineToken),200,'fault heartbeat');
  expect(await fixture('machine',target,'fault',{paths:['/_internal/reserve','/_internal/activate']}),200,'arm lost responses');
  const created=expect((await intake('intake-fault')).response,201,'fault intake').intake;
  const first=expect(await start(created.id,'start-fault',target,'shell'),202,'lost reserve response').session;
  expect(await fixture('project',project,'due-saga',{operation_id:first.operation_id}),200,'retry after reserve');
  expect(await fixture('project',project,'due-saga',{operation_id:first.operation_id}),200,'retry after activation');
  const replay=expect(await start(created.id,'start-fault',target,'shell'),200,'stable replay').session;assert.equal(replay.id,first.id);
  const list=expect(await api('/api/offers','GET',undefined,machineToken),200,'one native offer');assert.equal(list.offers.length,1);assert.equal(list.offers[0].session_id,first.id);assert.equal(list.offers[0].epoch,1,'retry allocated a second epoch');
  measurements.lost_response_machine=expect(await fixture('machine',target),200,'fault state');
 });
 await test('expired unclaimed offer rejects stale native admission after alarm replay',async()=>{
  const target='synthetic-expiry-machine';
  const pair=expect(await api('/api/enrollments/pair','POST',{machine_id:target,alias:'Synthetic expiry fixture'}),201,'expiry pairing');
  const expiryToken=expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),201,'expiry enrollment').token;
  expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'synthetic',labels:['fixture']},expiryToken),200,'expiry heartbeat');
  const created=expect((await intake('intake-expiry')).response,201,'expiry intake').intake;
  const session=expect(await start(created.id,'start-expiry',target,'shell'),201,'expiry start').session;
  expect(await fixture('machine',target,'expire-offer',{id:session.id}),200,'run real expiry alarm');
  expect(await fixture('machine',target,'alarm',{}),200,'duplicate alarm');
  const result=await api(`/api/sessions/${session.id}/admission`,'POST',{epoch:session.epoch,operation_id:session.attempt_operation_id},expiryToken);
  assert([404,409,410].includes(result.status),'expired offer became launch-authorized: HTTP'+result.status);
 });
 await test('machine credential rotation fences its existing WebSocket',async()=>{
  const target='synthetic-rotation-machine';
  const pair=expect(await api('/api/enrollments/pair','POST',{machine_id:target,alias:'Synthetic rotation fixture'}),201,'rotation pairing');
  const old=expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),201,'old token').token;
  expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'synthetic',labels:['fixture']},old),200,'rotation heartbeat');
  const connection=await socket(old);
  try{
   const created=expect((await intake('intake-rotation')).response,201,'rotation intake').intake;
   const session=expect(await start(created.id,'start-rotation',target,'shell'),201,'rotation start').session;
   await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===session.id);
   const nextPair=expect(await api('/api/enrollments/pair','POST',{machine_id:target,alias:'Rotated fixture'}),201,'re-pair');
   expect(await api('/api/enrollments/complete','POST',{code:nextPair.pairing_code},null),201,'rotate credential');
   expect(await api('/api/offers','GET',undefined,old),401,'old HTTP credential');
   try{connection.socket.send(JSON.stringify({type:'offer_ack',session_id:session.id,epoch:session.epoch}));}catch{}
   await sleep(150);assert(!connection.frames.some(v=>v.type==='ack'&&v.session_id===session.id),'revoked WebSocket acknowledged a launch offer');
  }finally{connection.socket.close();}
 });
 await test('native cursor cost remains bounded with retained intake history',async()=>{
  const before=await snapshots();expect(await api('/api/intakes?project='+encodeURIComponent(project)+'&limit=1'),200,'small intake page');const small=await snapshots();
  for(let i=0;i<100;i++)expect((await intake('history-'+i,'Synthetic retained history '+i)).response,201,'history fixture');
  const largeBefore=await snapshots();expect(await api('/api/intakes?project='+encodeURIComponent(project)+'&limit=1'),200,'large intake page');const largeAfter=await snapshots();
  const smallRead=small.project.counts.rows_read-before.project.counts.rows_read,largeRead=largeAfter.project.counts.rows_read-largeBefore.project.counts.rows_read;
  assert(largeRead<=smallRead+4,JSON.stringify({smallRead,largeRead}));measurements.intake_page={small_reads:smallRead,large_reads:largeRead};
 });
 await test('pending saga recovers when alarm persistence fails after state commit',async()=>{
  const created=expect((await intake('intake-alarm-interruption')).response,201,'alarm interruption intake').intake;
  expect(await fixture('project',project,'fail-next-alarm',{}),200,'arm alarm failure');
  const interrupted=await start(created.id,'start-alarm-interruption',machine,'shell');
  if(liveSocket){liveSocket.socket.close();liveSocket=null;}await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
  let restored=expect(await api(`/api/intakes/${created.id}?project=${encodeURIComponent(project)}`),200,'interrupted saga survived restart');
  let projection=expect(await fixture('project',project),200,'restarted deadline');
  // A recovered already-due alarm may complete between two HTTP reads. Re-read
  // the durable saga after the observation so a terminal transition is valid.
  if(projection.alarm===null){restored=expect(await api(`/api/intakes/${created.id}?project=${encodeURIComponent(project)}`),200,'post-alarm saga');projection=expect(await fixture('project',project),200,'post-alarm deadline');}
  measurements.alarm_interruption={start_http:interrupted.status,saga:restored.intake.dispatch,alarm:projection.alarm,counts:projection.counts};
  if(typeof restored.intake.dispatch?.next_action_at==='number')assert.notEqual(projection.alarm,null,'durable pending saga has no alarm after restart: '+JSON.stringify(measurements.alarm_interruption));
 });
 await test('persistent restart preserves auth and terminal outcome',async()=>{
  if(liveSocket){liveSocket.socket.close();liveSocket=null;}await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
  expect(await api('/api/offers','GET',undefined,hostToken),200,'credential survives restart');
  assert(completedIntakeId,'completed lifecycle prerequisite did not pass');const restored=expect(await api(`/api/intakes/${completedIntakeId}?project=${encodeURIComponent(project)}`),200,'restored terminal intake');assert.equal(restored.intake.status,'completed-awaiting-review');assert.equal(restored.intake.final_evidence.exit_code,0);
  const snap=await snapshots();for(const v of Object.values(snap))assert.equal(v.counts.measurement_valid,true);measurements.restart=snap;
 });
}finally{
 if(liveSocket)liveSocket.socket.close();await mf.dispose();
 const sourceAfter=await hashes();const sourceStable=JSON.stringify(sourceBefore)===JSON.stringify(sourceAfter);
 const report={passed:results.every(r=>r.passed)&&sourceStable,engine:'native workerd SQLite',source_stable:sourceStable,source_hashes:sourceAfter,results,measurements,note:'Quota/native receipts are synthetic fixtures, not provider or actual-process proof. Local test-only alarm inspection is outside production call counts. Whole-Hub daily budget is not proven by this slice.'};
 if(reportPath)await writeFile(reportPath,JSON.stringify(report,null,2));console.log(JSON.stringify(report));if(!report.passed)process.exitCode=1;
}
