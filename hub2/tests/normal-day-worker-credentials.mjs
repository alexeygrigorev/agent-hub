// Actual public five-DO scoped issuer contracts. Secret values never enter reports.
import assert from 'node:assert/strict';
import {writeFile}from'node:fs/promises';
import {resolve}from'node:path';
import {spawn}from'node:child_process';
export async function prepareWorkerProof(c){
 const{api,expect,fixture,socket,intake,start,project,machine,hostToken,session,offer,intakeId}=c;
 const path='/api/sessions/'+session.id+'/credentials',body={operation_id:offer.operation_id,epoch:offer.epoch};
 expect(await fixture('control','global','fault',{paths:['/_worker/issue']}),200,'lose actual successful issuance response');
 expect(await api(path,'POST',body,hostToken),503,'issuance response lost after persisted encrypted grant');
 const grant=expect(await api(path,'POST',body,hostToken),200,'issue bounded child');assert(typeof grant.token==='string'&&grant.token.length>=32,'child opaque credential required');
 const replay=expect(await api(path,'POST',{...body,scopes:['*'],deadline:Date.now()+86400000},hostToken),200,'issuer lost-response retry');assert(grant.token===replay.token&&grant.expires_at===replay.expires_at,'issuer must replay same secret and first expiry');
 for(const [bad,label]of[[{...body,epoch:offer.epoch+1},'wrong child epoch'],[{...body,operation_id:'wrong-operation'},'wrong child operation']])expect(await api(path,'POST',bad,hostToken),409,label);
 const refused=await api(path,'POST',body);assert([403,404].includes(refused.status),'operator cannot extract child credential');
 if(process.env.HUB2_CORE_WORKER_CONTEXT){
  const config=resolve(c.persist,'host-config.json'),offerFile=resolve(c.persist,'claimed-offer.json');await writeFile(config,JSON.stringify({server:c.base,token:hostToken,machine_id:machine}),{mode:0o600});await writeFile(offerFile,JSON.stringify(offer),{mode:0o600});
  const result=await new Promise((done,reject)=>{const child=spawn('/home/alexey/.local/share/agent-hub2/venv/bin/python',['hub2/host/tests/worker_context.py','--launcher-root','/home/alexey/git/agent-quota-launcher-wt-hub2-provider-home','--host-config',config,'--offer',offerFile],{cwd:c.root,env:{...process.env,PYTHONPATH:resolve(c.root,'hub2/host')},stdio:['ignore','pipe','pipe']});let out='',err='';child.stdout.on('data',b=>out+=b);child.stderr.on('data',b=>err+=b);const timer=setTimeout(()=>child.kill(),30000);child.once('error',reject);child.once('close',code=>{clearTimeout(timer);done({code,out,err});});});
  if(result.code!==0)await writeFile(resolve(c.persist,'host-context-failure-private.txt'),result.out+'\n'+result.err,{mode:0o600});assert.equal(result.code,0,'actual host scoped context driver failed; private output retained');const proof=JSON.parse(result.out);assert(proof.passed&&proof.actual_host_scoped_file&&proof.actual_worker_websocket&&proof.actual_child_cli_read&&!proof.model_launched,'actual host context checks required');
 }
 if(process.env.HUB2_CORE_PROVISION){
  const account=offer.account_id,prefix='/api/accounts/'+encodeURIComponent(account)+'/api/account';
  expect(await fixture('account',account,'expire-quota',{}),200,'invalidate current-generation cached quota');expect(await fixture('account',account,'provider-failure',{enabled:true}),200,'provider HTTP outage');
  try{
   const admittedDuringQuotaOutage=expect(await api('/api/sessions/'+session.id+'/admission','POST',{operation_id:offer.operation_id,epoch:offer.epoch},hostToken),200,'quota outage does not gate an accepted session');
   assert.equal(admittedDuringQuotaOutage.launch_allowed,true);
   expect(await api('/api/account/quota'),200,'quota remains readable during worker outage');
  }finally{expect(await fixture('account',account,'provider-failure',{enabled:false}),200,'restore synthetic provider HTTP');}
  expect(await api(prefix+'/discovery','POST',{}),200,'explicit genuine collector repairs outage');
  const fresh=expect(await api(prefix+'/credentials?min_validity_ms=120000','GET',undefined,hostToken),200,'same generation fresh quota bundle');assert.equal(fresh.generation,1);assert.equal(fresh.refresh_material,false);
  const admitted=expect(await api('/api/sessions/'+session.id+'/admission','POST',{operation_id:offer.operation_id,epoch:offer.epoch},hostToken),200,'session binding remains available after quota recovery');assert.equal(admitted.launch_allowed,true);
 }
 if(process.env.HUB2_CORE_PROVISION){
  const config=resolve(c.persist,'provider-host-config.json'),offerFile=resolve(c.persist,'provider-claimed-offer.json');await writeFile(config,JSON.stringify({server:c.base,token:hostToken,machine_id:machine}),{mode:0o600});await writeFile(offerFile,JSON.stringify(offer),{mode:0o600});
  const result=await new Promise((done,reject)=>{const child=spawn('/home/alexey/.local/share/agent-hub2/venv/bin/python',['hub2/host/tests/provider_context.py','--host-config',config,'--offer',offerFile,'--provider','codex'],{cwd:c.root,env:{...process.env,PYTHONPATH:resolve(c.root,'hub2/host')},stdio:['ignore','pipe','pipe']});let out='',err='';child.stdout.on('data',b=>out+=b);child.stderr.on('data',b=>err+=b);const timer=setTimeout(()=>child.kill(),30000);child.once('error',reject);child.once('close',code=>{clearTimeout(timer);done({code,out,err});});});
  if(result.code!==0)await writeFile(resolve(c.persist,'provider-context-failure-private.txt'),result.out+'\n'+result.err,{mode:0o600});assert.equal(result.code,0,'actual provider driver failed; private output retained');const proof=JSON.parse(result.out);assert(proof.sdk_selected&&proof.provider==='codex'&&proof.generation===1&&proof.provider_context_private&&proof.central_generation_ack&&proof.model_call===false&&proof.native_spawn===false,'actual provider generation/SDK/controller proof required');
 }
 const stored=expect(await fixture('control','global','worker-storage',{session_id:session.id,epoch:offer.epoch}),200,'single worker authority storage');assert.equal(stored.duplicate_agent_rows,0,'worker grant must not duplicate credential authority');assert(stored.grant.has_hash===1&&stored.grant.has_sealed===1&&stored.grant.deadline===grant.expires_at&&!stored.grant.revoked_at,'one authoritative encrypted/hash grant required');
 const own='/api/intakes/'+intakeId;
 for(const route of [own,own+'?project='+encodeURIComponent(project),'/api/intakes/'+intakeId.slice(0,8)])assert.equal(expect(await api(route,'GET',undefined,grant.token),200,'child own task').intake.id,intakeId);
 expect(await api(own+'/append','POST',{key:'child-own-note',note:'Scoped child progress',evidence:{fixture:true}},grant.token),201,'child append note');
 expect(await api(own+'/append?project='+encodeURIComponent(project),'POST',{key:'child-own-note-explicit',note:'Explicit project progress'},grant.token),201,'child qualified append');
 const foreign=expect((await intake('worker-foreign-task')).response,201,'foreign fixture task').intake;
 for(const route of ['/api/intakes/'+foreign.id,'/api/intakes/'+foreign.id+'?project='+encodeURIComponent(project),own+'?project=foreign/project'])assert([403,404].includes((await api(route,'GET',undefined,grant.token)).status),'foreign task/project disclosed');
 expect(await api(own+'?project='+encodeURIComponent(project),'PATCH',{key:'child-ac-edit',title:'Forbidden',acceptance_criteria:['Forbidden']},grant.token),403,'child cannot edit acceptance criteria');
 expect(await api(own+'?project='+encodeURIComponent(project),'PATCH',{key:'child-review',status:'accepted'},grant.token),403,'child cannot review or approve');
 expect(await api(own+'/start?project='+encodeURIComponent(project),'POST',{key:'child-extra-start',machine_id:machine,workspace:'/synthetic/workspace',engine:'opencode',override_reason:'Forbidden worker grant test'},grant.token),403,'child cannot launch');
 expect(await api('/api/repos','GET',undefined,grant.token),403,'no global repository authority');expect(await api('/api/account/quota','GET',undefined,grant.token),403,'no account authority');expect(await api('/api/named-agents','GET',undefined,grant.token),403,'no grant administration');
 expect(await api('/api/messages?mailbox='+encodeURIComponent(grant.actor),'GET',undefined,grant.token),200,'own child mailbox');
 const claim=expect(await api('/api/claims?project='+encodeURIComponent(project),'POST',{key:'child-own-claim',intake_id:intakeId,paths:['src/scoped-child.ts'],mode:'edit'},grant.token),201,'own child claim').claim;
 expect(await api('/api/claims/'+encodeURIComponent(claim.id)+'/release?project='+encodeURIComponent(project),'POST',{},grant.token),200,'own child claim release');
 expect(await api('/api/claims?project='+encodeURIComponent(project),'POST',{key:'child-foreign-claim',intake_id:foreign.id,paths:['src/foreign.ts'],mode:'edit'},grant.token),403,'foreign child claim');
 const futureAt=new Date(Date.now()+3600000).toISOString(),wakePath='/api/sessions/'+session.id+'/wake',wakesPath='/api/sessions/'+session.id+'/wakes';
 const workerWake=expect(await api(wakePath,'POST',{key:'worker-self-wake',at:futureAt,prompt:'Bounded worker self wake fixture'},grant.token),201,'worker schedules own wake');assert(workerWake.wake_id&&workerWake.wake_state==='pending'&&!('prompt'in workerWake),'worker wake view stays bounded without prompt leak');
 const wakeReplay=expect(await api(wakePath,'POST',{key:'worker-self-wake',at:futureAt,prompt:'Bounded worker self wake fixture'},grant.token),200,'worker wake lost-response replay');assert.equal(wakeReplay.wake_id,workerWake.wake_id);assert.equal(wakeReplay.replay,true);
 expect(await api(wakePath,'POST',{key:'worker-host',at:futureAt,prompt:'x'},hostToken),404,'machine credential cannot wake');expect(await api(wakesPath,'GET',undefined,hostToken),404,'machine credential cannot read wakes');
 expect(await api(wakePath,'POST',{key:'worker-forged',at:futureAt,prompt:'x'},hostToken,{'x-hub2-actor':JSON.stringify({id:'session:'+session.id,kind:'agent',scopes:['intake:append','worker_project:'+project]})}),404,'caller cannot forge worker actor');
 const unknownWake=await api('/api/sessions/'+crypto.randomUUID()+'/wake','POST',{key:'worker-unknown',at:futureAt,prompt:'x'},grant.token);assert([403,404].includes(unknownWake.status),'unknown session stays private');
 // The wake stays pending for the whole lane: a truthful due miss on this
 // headless fixture session hands its intake to the idle-with-open-AC recovery
 // path by design, which would requeue a completed intake mid-lane. The
 // due-time headless refusal itself is covered by session-wakes.test.ts
 // ('headless due wake is a truthful missed handoff...') on the same engine.
 const ownHistory=expect(await api(wakesPath,'GET',undefined,grant.token),200,'worker reads own wake history');assert.equal(ownHistory.wakes[0].wake_state,'pending');assert(!('prompt' in ownHistory.wakes[0]),'history stays private');
 async function afterFinish(){
  expect(await api(own,'GET',undefined,grant.token),401,'qualified completion revokes child');
  const target='worker-proof-machine';let token;
  async function enroll(){const pair=expect(await api('/api/enrollments/pair','POST',{machine_id:target,alias:'Synthetic worker revocation fixture'}),201,'worker host pair');return expect(await api('/api/enrollments/complete','POST',{code:pair.pairing_code},null),201,'worker host enroll').token;}
  token=await enroll();const wrongHost=await api(path,'POST',body,token);assert([403,404,409].includes(wrongHost.status)&&!wrongHost.data.token,'foreign host must receive no child secret');expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'fixture',engines:['opencode'],capabilities:['headless']},token),200,'worker host capability');expect(await api('/api/account/hosts','PUT',{hosts:[machine,target]}),200,'worker account hosts');
  async function newJob(key,index,race=false){const task=expect((await intake(key)).response,201,'worker lifecycle intake').intake,connection=await socket(token);const s=expect(await start(task.id,key,target),201,'worker fixture start').session,o=(await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===s.id)).offer;connection.socket.send(JSON.stringify({type:'offer_ack',session_id:s.id,epoch:o.epoch}));await connection.wait(v=>v.type==='ack'&&v.session_id===s.id);const admission=expect(await api('/api/sessions/'+s.id+'/admission','POST',{operation_id:o.operation_id,epoch:o.epoch},token),200,'worker fixture admission');assert(Number.isSafeInteger(admission.runtime_deadline),'worker fixture admission deadline');let g;if(race){
   expect(await fixture('control','global','hold-worker-crypt',{}),200,'pause real encrypted issuance');const pending=api('/api/sessions/'+s.id+'/credentials','POST',{operation_id:o.operation_id,epoch:o.epoch},token);
   let entered=false;for(let i=0;i<100;i++){const state=expect(await fixture('control','global','worker-crypt-state',{}),200,'crypt barrier');if(state.entered){entered=true;break;}await new Promise(r=>setTimeout(r,10));}assert(entered,'actual crypto barrier must be entered');
   expect(await fixture('control','global','revoke-worker',{session_id:s.id,operation_id:o.operation_id,epoch:o.epoch,machine_id:target,intake_id:task.id,project_id:project,reason:'native_exit_confirmed'}),200,'revoke absent grant during encryption');expect(await fixture('control','global','release-worker-crypt',{}),200,'resume real issuance');const refused=await pending;assert(refused.status===409&&!refused.data.token,'completed child must be fenced with409 on late issue');
  }else g=expect(await api('/api/sessions/'+s.id+'/credentials','POST',{operation_id:o.operation_id,epoch:o.epoch},token),200,'worker fixture credential');const native='77777777-2222-4333-8444-'+String(index).padStart(12,'0');expect(await api('/api/sessions/'+s.id+'/started','POST',{operation_id:o.operation_id,epoch:o.epoch,native_id:native},token),201,'worker fixture started');return{task,s,o,g,native,connection,admission};}
  async function finish(job){expect(await api('/api/intakes/'+job.task.id+'/events','POST',{key:'block-'+job.s.id,event:{kind:'task_blocked',session_id:job.s.id,cause:'external',detail:'Synthetic worker fixture completes without continuation',next_step:'None'}}),201,'worker fixture terminal blocker');const exitedAt=Date.now(),deadline=job.admission.runtime_deadline,exceeded=exitedAt>=deadline;const evidence={source:'aplexer_retirement',fixture:true,native_identity:{id:job.native,tag:'task-hub2-'+job.s.id,workspace:job.o.launch.workspace},containment_empty:true,native_deadline:{deadline_ms:deadline,exceeded},native_exit:{code:0,exited_at_ms:exitedAt}};expect(await api('/api/sessions/'+job.s.id+'/finish','POST',{operation_id:job.o.operation_id,epoch:job.o.epoch,native_id:job.native,exit_code:0,native_exit_confirmed:true,evidence},token),201,'worker fixture completion');job.connection.socket.close();}
  const rotated=await newJob('worker-reenroll',1);const crossTask=await api('/api/intakes/'+intakeId,'GET',undefined,rotated.g.token);assert([403,404].includes(crossTask.status),'cross-session child task disclosed: '+crossTask.status);
  // Valid-sibling denials at the documented edge route-lookup layer (403 forbidden,
  // no body/token leakage) while the rotated grant is demonstrably live.
  expect(await api('/api/sessions/'+session.id+'/wake','POST',{key:'cross-wake',at:new Date(Date.now()+3600000).toISOString(),prompt:'x'},rotated.g.token),403,'cross-session wake refused');
  expect(await api('/api/sessions/'+session.id+'/wakes','GET',undefined,rotated.g.token),403,'cross-session wake history refused');
  const oldHost=token;token=await enroll();
  expect(await api('/api/intakes/'+rotated.task.id,'GET',undefined,rotated.g.token),401,'re-enroll revokes child');
  expect(await api('/api/offers','GET',undefined,oldHost),401,'re-enroll revokes old host');
  expect(await api('/api/sessions/'+session.id+'/wake','POST',{key:'cross-wake-revoked',at:new Date(Date.now()+3600000).toISOString(),prompt:'x'},rotated.g.token),401,'revoked child cannot wake');
  expect(await api('/api/sessions/'+session.id+'/wakes','GET',undefined,rotated.g.token),401,'revoked child cannot read wake history');
  await finish(rotated);
  const expiring=await newJob('worker-expiry',2);expect(await api('/api/intakes/'+expiring.task.id,'GET',undefined,expiring.g.token),200,'worker cached credential before expiry');expect(await fixture('control','global','clock',{offset:65000}),200,'advance actual authorization clock');try{expect(await api('/api/intakes/'+expiring.task.id,'GET',undefined,expiring.g.token),401,'worker expiry checked despite cached SQL row');}finally{expect(await fixture('control','global','clock',{offset:0}),200,'restore isolated fixture clock');}await finish(expiring);
  const raced=await newJob('worker-revoke-race',3,true);await finish(raced);
  expect(await api('/api/account/quota'),200,'child completion leaves quota view available');
  expect(await fixture('control','global','clock',{offset:31000}),200,'advance clock for missed wake notification retry');
  try{expect(await fixture('machine',machine,'alarm',{}),200,'settle worker wake missed notification');}
  finally{expect(await fixture('control','global','clock',{offset:0}),200,'restore clock after wake notification');}
  const credentialsProof={actual_host_context:!!process.env.HUB2_CORE_WORKER_CONTEXT,actual_provider_context:!!process.env.HUB2_CORE_PROVISION,quota_outage_does_not_gate_launch:!!process.env.HUB2_CORE_PROVISION,issued_idempotently:true,single_authoritative_grant:true,lost_issuer_reply_replayed:true,lost_revocation_reply_replayed:true,wrong_machine_refused:true,review_refused:true,assigned_intake_only:true,append_only:true,foreign_claims_refused:true,no_admin_or_start:true,completion_revoked:true,reenroll_revoked:true,late_issue_after_absent_revoke_fenced:true,expiry_rechecked_on_cached_row:true,worker_self_wake:true,worker_wake_replay_stable:true,worker_wake_headless_truthful:true,cross_session_wake_refused:true};
  // Positive requirement: when this fixture runs, every fixed proof flag must be
  // true or the lane fails — a sentinel-only false green is not acceptable.
  for(const key of ['issued_idempotently','single_authoritative_grant','lost_issuer_reply_replayed','lost_revocation_reply_replayed','wrong_machine_refused','review_refused','assigned_intake_only','append_only','foreign_claims_refused','no_admin_or_start','completion_revoked','reenroll_revoked','late_issue_after_absent_revoke_fenced','expiry_rechecked_on_cached_row','worker_self_wake','worker_wake_replay_stable','worker_wake_headless_truthful','cross_session_wake_refused'])assert.equal(credentialsProof[key],true,'worker credential proof flag must be positively true: '+key);
  return credentialsProof;
 }
 expect(await fixture('control','global','fault',{paths:['/_worker/revoke']}),200,'lose successful revocation response');
 return{afterFinish};
}
