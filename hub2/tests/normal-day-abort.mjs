// Real public five-DO admitted-abort contract; native receipt is explicitly synthetic.
import assert from 'node:assert/strict';
export async function admittedAbort({api,expect,fixture,socket,intake,start,project,machine,hostToken}){
 const task=expect((await intake('qualified-admitted-abort')).response,201,'abort task').intake,connection=await socket(hostToken);let pending;
 try{
  const session=expect(await start(task.id,'qualified-abort-start'),201,'abort start').session;
  const offer=(await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===session.id)).offer;
  connection.socket.send(JSON.stringify({type:'offer_ack',session_id:session.id,epoch:offer.epoch}));await connection.wait(v=>v.type==='ack'&&v.session_id===session.id);
  const admission=expect(await api('/api/sessions/'+session.id+'/admission','POST',{operation_id:offer.operation_id,epoch:offer.epoch},hostToken),200,'abort admitted before native confirmation');assert(Number.isSafeInteger(admission.runtime_deadline),'abort fixture must carry the immutable deadline');
  const child=expect(await api('/api/sessions/'+session.id+'/credentials','POST',{operation_id:offer.operation_id,epoch:offer.epoch},hostToken),200,'abort scoped credential');
  assert(Number.isSafeInteger(admission.observed_at),'abort admission must expose its observed timestamp');
  const native='99999999-2222-4333-8444-000000000001',evidence={source:'aplexer_retirement',fixture:true,native_identity:{id:native,tag:'task-hub2-'+session.id,workspace:offer.launch.workspace},containment_empty:true,native_deadline:{deadline_ms:admission.runtime_deadline,exceeded:false},native_exit:{code:0,exited_at_ms:admission.observed_at+1}};
  const path='/api/sessions/'+session.id+'/finish',body={operation_id:offer.operation_id,epoch:offer.epoch,native_id:native,exit_code:0,native_exit_confirmed:true,launch_failed:true,evidence};
  for(const[changed,label]of[[{...body,launch_failed:false},'cannot invent normal completion before started'],[{...body,operation_id:'foreign-op'},'abort wrong operation'],[{...body,epoch:offer.epoch+1},'abort wrong epoch']])expect(await api(path,'POST',changed,hostToken),409,label);
  for(const changed of[{...body,native_exit_confirmed:false},{...body,evidence:{...evidence,containment_empty:false}},{...body,evidence:{...evidence,native_identity:{...evidence.native_identity,tag:'wrong-tag'}}}])assert([400,409].includes((await api(path,'POST',changed,hostToken)).status),'abort requires exact qualified native containment proof');
  expect(await fixture('project',project,'hold-finish-response',{}),200,'delay actual successful abort forwarding response');
  pending=api(path,'POST',body,hostToken);
  let entered=false;for(let i=0;i<200;i++){if(expect(await fixture('project',project,'finish-response-state',{}),200,'abort forwarding barrier').entered){entered=true;break;}await new Promise(r=>setTimeout(r,10));}assert(entered,'actual Project abort must commit before delayed response');
  const failed=expect(await api('/api/intakes/'+task.id),200,'failed admitted task').intake;assert.equal(failed.status,'failed');assert.equal(failed.final_evidence.exit_code,0);assert.equal(failed.final_evidence.launch_failed,true);
  const history=expect(await api('/api/intakes/'+task.id+'/history'),200,'abort history');assert.equal(history.events.filter(e=>e.kind==='native_session_started').length,0);
  expect(await api('/api/account/quota'),200,'quota view remains independent of abort');expect(await api('/api/intakes/'+task.id,'GET',undefined,child.token),401,'abort revokes child');
  const retry=expect(await start(task.id,'qualified-abort-retry'),201,'qualified failed task retry').session;assert.notEqual(retry.id,session.id);
  const next=(await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===retry.id)).offer;
  expect(await fixture('project',project,'release-finish-response',{}),200,'release lost abort response');expect(await pending,202,'abort notification remains durable after lost success');pending=undefined;
  expect(await fixture('machine',machine,'due-outbox',{id:'finish:'+offer.operation_id}),200,'old completion retry after new intake assignment');
  const queued=expect(await api('/api/intakes/'+task.id),200,'new retry survives old completion').intake;assert.equal(queued.status,'queued');assert.equal(queued.session_id,retry.id);expect(await api('/api/account/quota'),200,'retry keeps quota view independent');
  expect(await api(path,'POST',body,hostToken),200,'exact abort replay');expect(await api(path,'POST',{...body,operation_id:'wrong-terminal-operation'},hostToken),409,'terminal abort replay exact operation fence');for(const changed of[{...body,launch_failed:false},{...body,evidence:{...evidence,note:'changed proof'}}])expect(await api(path,'POST',changed,hostToken),409,'abort proof conflict');
  connection.socket.send(JSON.stringify({type:'offer_ack',session_id:retry.id,epoch:next.epoch}));await connection.wait(v=>v.type==='ack'&&v.session_id===retry.id);
  const retryAdmission=expect(await api('/api/sessions/'+retry.id+'/admission','POST',{operation_id:next.operation_id,epoch:next.epoch},hostToken),200,'retry admission');assert(Number.isSafeInteger(retryAdmission.runtime_deadline),'retry fixture must carry the immutable deadline');
  const newChild=expect(await api('/api/sessions/'+retry.id+'/credentials','POST',{operation_id:next.operation_id,epoch:next.epoch},hostToken),200,'retry fresh scoped grant');assert.notEqual(newChild.token,child.token);
  const realNative='99999999-2222-4333-8444-000000000002';expect(await api('/api/sessions/'+retry.id+'/started','POST',{operation_id:next.operation_id,epoch:next.epoch,native_id:realNative},hostToken),201,'only retry is marked started');
  assert(Number.isSafeInteger(retryAdmission.observed_at),'retry admission must expose its observed timestamp');
  const finalProof={source:'aplexer_retirement',fixture:true,native_identity:{id:realNative,tag:'task-hub2-'+retry.id,workspace:next.launch.workspace},containment_empty:true,native_deadline:{deadline_ms:retryAdmission.runtime_deadline,exceeded:false},native_exit:{code:0,exited_at_ms:retryAdmission.observed_at+1}};
  expect(await api('/api/sessions/'+retry.id+'/finish','POST',{operation_id:next.operation_id,epoch:next.epoch,native_id:realNative,exit_code:0,native_exit_confirmed:true,evidence:finalProof},hostToken),201,'retry qualified normal finish');
  assert.equal(expect(await api('/api/intakes/'+task.id),200,'retry terminal task').intake.status,'completed-awaiting-review');expect(await api('/api/account/quota'),200,'all abort/retry quota views remain available');expect(await api('/api/intakes/'+task.id,'GET',undefined,newChild.token),401,'retry child revoked');
  return{qualified_admitted_abort:true,actual_exit_zero_kept_failed:true,no_started_event_for_aborted_attempt:true,abort_child_revoked:true,late_old_completion_cannot_clobber_retry:true,exact_replay_and_conflict_fences:true};
 }finally{if(pending){await fixture('project',project,'release-finish-response',{});await pending.catch(()=>{});}connection.socket.close();}
}
