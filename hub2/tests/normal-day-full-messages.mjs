// Canonical normal-day message ledger:100operator reads+400root/writer message calls.
import assert from 'node:assert/strict';
export async function fullMessages({api,expect,roleApi,connection,session,hostToken,family}){
 const sender='agent:normal-root',recipient='agent:normal-recipient',actor='human:normal-day-user';
 for(const[name,forward]of[['normal-root',null],['normal-recipient',session.id]])expect(await api('/api/named-agents','POST',{name,display_name:name,forward_session:forward}),200,'normal named identity');
 for(const[id,send]of[[sender,true],[recipient,false]])expect(await api('/api/named-agents/'+encodeURIComponent(id)+'/grants','PUT',{family_id:family,client_id:'normal-day-cli',can_send:send,can_read:true,enabled:true}),200,'actual caller family delegation');
 const ids=[];
 for(let i=0;i<100;i++){
  const sent=expect(await roleApi('root_writer','/api/messages?as='+encodeURIComponent(sender),'POST',{recipient,body:'Synthetic canonical native message '+i,key:'normal-message-'+i}),201,'normal message send').message;ids.push(sent.id);
  const frame=await connection.wait(v=>v.type==='mailbox.deliver'&&v.message.id===sent.id),message=frame.message;
  const path='/api/messages/'+sent.id+'/delivered?mailbox='+encodeURIComponent(recipient),claim=expect(await api(path,'POST',{phase:'claim',epoch:message.epoch,operation_id:message.operation_id},hostToken),201,'normal native claim');
  expect(await api(path,'POST',{phase:'receipt',epoch:message.epoch,operation_id:message.operation_id,claim_id:claim.claim_id,native_receipt:{provider:'aplexer',message_id:'88888888-2222-4333-8444-'+String(i).padStart(12,'0'),session_id:session.id,recipient}},hostToken),200,'normal native receipt');
  expect(await roleApi('operator','/api/messages/'+sent.id+'/read?mailbox='+encodeURIComponent(recipient),'POST',{epoch:message.epoch}),200,'phone read');
  for(const stage of['ack','accept','complete'])expect(await roleApi('root_writer','/api/messages/'+sent.id+'/'+stage+'?as='+encodeURIComponent(recipient),'POST',{epoch:message.epoch}),200,'normal '+stage);
 }
 for(const id of ids.slice(0,40)){const status=expect(await roleApi('root_writer','/api/messages/'+id+'/status?as='+encodeURIComponent(sender)),200,'actual status view');assert(status.message.completed_at,'completed message status must be real');}
 return{messages:100,native_receipts:100,phone_reads:100,root_writer_message_calls:400,actual_status_views:40,principal:actor,delegation_family:family};
}
