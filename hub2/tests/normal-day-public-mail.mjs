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
 const path='/api/messages/'+id+'/delivered?mailbox='+encodeURIComponent(recipient),claimBody={phase:'claim',epoch,operation_id:message.operation_id};
 expect(await api('/api/messages/'+id+'/read?mailbox='+encodeURIComponent(recipient),'POST',{epoch}),200,'phone read before native claim');
 const bad=await api(path,'POST',{...claimBody,operation_id:'forged-operation'},hostToken,{'x-hub2-delivery-session':session.id});assert([403,404,409].includes(bad.status),'untrusted delivery header/operation accepted');
 const claim=expect(await api(path,'POST',claimBody,hostToken),201,'public native claim uses server queue authority');assert.equal(claim.epoch,epoch);
 const payload={phase:'receipt',epoch,operation_id:message.operation_id,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'22222222-3333-4444-8555-666666666666',session_id:session.id,recipient,machine_id:'caller-forged-machine'}};
 expect(await api(path,'POST',payload,hostToken),200,'public native receipt');expect(await api(path,'POST',payload,hostToken),200,'public native receipt replay');
 const status=expect(await api('/api/messages/'+id+'/status'),200,'public sender native stage');assert(status.message.delivered_at);assert(status.message.read_at,'phone read must survive native claim');assert.notEqual(status.message.native_receipt.machine_id,'caller-forged-machine');
 for(const stage of ['ack','accept','complete'])expect(await api('/api/messages/'+id+'/'+stage+'?mailbox='+encodeURIComponent(recipient),'POST',{epoch}),200,'public '+stage);
 const done=expect(await api('/api/messages/'+id+'/status'),200,'public complete status');assert(done.message.completed_at);
 return{id,epoch,public_native_claim:true,receipt:true,read_before_native_preserved:true};
}
