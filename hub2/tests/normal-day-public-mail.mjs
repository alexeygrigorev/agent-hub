// Public edge + actual Control/Project/Machine/Account/Mailbox producer contract.
import assert from 'node:assert/strict';
export async function publicNativeMail({api,expect,connection,session,hostToken,intakeId}){
 const resolved=expect(await api('/api/intakes/'+intakeId.slice(0,8)),200,'actual intake prefix route without project');assert.equal(resolved.intake.id,intakeId);
 const routed=expect(await api('/api/sessions/'+session.id),200,'actual session route without machine');assert.equal(routed.session.id,session.id);
 const recipient='agent:public-core-recipient';
 expect(await api('/api/named-agents','POST',{name:'public-core-recipient',display_name:'Synthetic public native recipient',forward_session:session.id}),200,'create real named forward');
 const send=expect(await api('/api/messages','POST',{recipient,body:'Synthetic native-mail effect contract',key:'public-native-mail'}),201,'public sender');
 const id=send.message.id,epoch=send.message.epoch;
 const frame=await connection.wait(v=>v.type==='mailbox.deliver'&&v.message.id===id);const message=frame.message;
 assert.equal(message.recipient,recipient);assert.equal(message.session_id,session.id);assert.equal(message.epoch,epoch);
 const outsider='agent:public-core-outsider';expect(await api('/api/named-agents','POST',{name:'public-core-outsider',display_name:'Synthetic partition outsider'}),200,'create outsider');expect(await api('/api/named-agents/'+encodeURIComponent(outsider)+'/grants','PUT',{family_id:'hub2-dev',client_id:'local',can_send:true,can_read:true,enabled:true}),200,'grant actual outsider identity');
 const outsiderQuery='?as='+encodeURIComponent(outsider);for(const target of[id,id.slice(0,8)])expect(await api('/api/messages/'+target+outsiderQuery),404,'global authority foreign message/prefix opaque');assert(!expect(await api('/api/messages'+outsiderQuery),200,'bounded outsider inbox').items.some(m=>m.id===id),'global inbox must not leak another actor');
 const refusedRead=await api('/api/messages/'+id+'/read'+outsiderQuery,'POST',{epoch});assert([403,404].includes(refusedRead.status),'outsider cannot mutate foreign message');
 expect(await api('/api/messages?mailbox='+encodeURIComponent(outsider),'POST',{recipient:'human:dev',body:'Forbidden owner spoof',key:'owner-spoof'}),403,'operator mailbox query cannot spoof sender without grant-as');
 const foreignReply=await api('/api/messages'+outsiderQuery,'POST',{recipient:'human:dev',body:'Forbidden thread participant',key:'foreign-thread-reply',reply_to:id,parent_sender:'human:dev'});assert([403,404].includes(foreignReply.status),'nonparticipant cannot reply to another actor thread');
 const partition=expect(await api('/api/messages'+outsiderQuery,'POST',{recipient:'human:dev',body:'Distinct sender same key',key:'public-native-mail'}),201,'same key belongs to different sender');assert.notEqual(partition.message.id,id);assert.equal(partition.message.sender,outsider);
 const concurrent=await Promise.all([api('/api/messages','POST',{recipient:outsider,body:'Human partition',key:'concurrent-partition'}),api('/api/messages'+outsiderQuery,'POST',{recipient:'human:dev',body:'Agent partition',key:'concurrent-partition'})]);assert.equal(expect(concurrent[0],201,'concurrent human sender').message.sender,'human:dev');assert.equal(expect(concurrent[1],201,'concurrent agent sender').message.sender,outsider);
 const path='/api/messages/'+id+'/delivered?mailbox='+encodeURIComponent(recipient),claimBody={phase:'claim',epoch,operation_id:message.operation_id};
 expect(await api('/api/messages/'+id+'/read?mailbox='+encodeURIComponent(recipient),'POST',{epoch}),200,'phone read before native claim');
 const bad=await api(path,'POST',{...claimBody,operation_id:'forged-operation'},hostToken,{'x-hub2-delivery-session':session.id});assert([403,404,409].includes(bad.status),'untrusted delivery header/operation accepted');
 const claim=expect(await api(path,'POST',claimBody,hostToken),201,'public native claim uses server queue authority');assert.equal(claim.epoch,epoch);
 const payload={phase:'receipt',epoch,operation_id:message.operation_id,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'22222222-3333-4444-8555-666666666666',session_id:session.id,recipient,machine_id:'caller-forged-machine'}};
 expect(await api(path,'POST',payload,hostToken),200,'public native receipt');expect(await api(path,'POST',payload,hostToken),200,'public native receipt replay');
 const status=expect(await api('/api/messages/'+id+'/status'),200,'public sender native stage');assert(status.message.delivered_at);assert(status.message.read_at,'phone read must survive native claim');assert.notEqual(status.message.native_receipt.machine_id,'caller-forged-machine');
 for(const stage of ['ack','accept','complete'])expect(await api('/api/messages/'+id+'/'+stage+'?mailbox='+encodeURIComponent(recipient),'POST',{epoch}),200,'public '+stage);
 const done=expect(await api('/api/messages/'+id+'/status'),200,'public complete status');assert(done.message.completed_at);
 return{id,epoch,public_native_claim:true,receipt:true,read_before_native_preserved:true,global_actor_partition:true,composite_sender_key:true,concurrent_owner_isolation:true};
}
