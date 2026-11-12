import {bodyObject,boundedString,error,json} from './http';
import type {MeteredDb} from './db';
import type {JsonRecord} from './types';

type Append=(id:string,key:string,actor:string,kind:string,body:JsonRecord,at:number)=>void;

/** Machine-authenticated receipts: retain diagnostics without replaying lifecycle
 * transitions or consuming task recovery attempts. These are internal DO routes.
 */
export async function terminalReceipt(request:Request,db:MeteredDb,transaction:<T>(fn:()=>T)=>T,
  append:Append,context:(id:string)=>JsonRecord):Promise<Response>{
  const body=await bodyObject(request,16_384),stopped=new URL(request.url).pathname==='/_internal/stopped-finish';
  if(!boundedString(body.session_id,180)||!boundedString(body.intake_id,180)||!boundedString(body.machine_id,180)
    ||!boundedString(body.operation_id,180)||!boundedString(body.attempt_operation_id,180)||!Number.isSafeInteger(body.epoch))
    return error('invalid_terminal_receipt',400);
  const saga=db.one<{intake_id:string;machine_id:string;operation_id:string;attempt_operation_id:string;epoch:number;native_id:string|null}>(
    'SELECT * FROM dispatch_sagas WHERE session_id=?',body.session_id);
  if(!saga||saga.intake_id!==body.intake_id||saga.machine_id!==body.machine_id||saga.operation_id!==body.operation_id
    ||saga.attempt_operation_id!==body.attempt_operation_id||saga.epoch!==body.epoch)return error('terminal_receipt_binding_mismatch',409);
  if(stopped){
    if(body.state!=='stopped'||body.native_exit_confirmed!==true||typeof body.native_id!=='string'
      ||saga.native_id!==body.native_id||typeof body.evidence_digest!=='string'||!/^[0-9a-f]{64}$/.test(body.evidence_digest))
      return error('invalid_stopped_finish',409);
  }else if(!['finish_dead_letter','launch_expired_unconfirmed'].includes(String(body.kind))
    ||!boundedString(body.reason,180)||!Number.isInteger(body.status)||body.recipient!=='dev@dtcdev.click')return error('invalid_finish_alert',400);
  const intakeId=saga.intake_id,key=`${stopped?'stopped-finish':String(body.kind)}:${body.attempt_operation_id}:${body.epoch}`;
  return transaction(()=>{
    if(db.one('SELECT seq FROM intake_events WHERE event_key=?',key))return json({ok:true,duplicate:true});
    const now=Date.now(),actor='machine:'+saga.machine_id;
    if(stopped){
      append(intakeId,key,actor,'session_finish_after_stop',body,now);
      const intake=db.one<{final_evidence_json:string|null}>('SELECT final_evidence_json FROM intakes WHERE id=?',intakeId);
      const previous=intake?.final_evidence_json?JSON.parse(intake.final_evidence_json):{};
      db.run('UPDATE intakes SET final_evidence_json=?,updated_at=? WHERE id=?',JSON.stringify({...previous,finish_after_stop:body}),now,intakeId);
    }else{
      append(intakeId,key,actor,'developer_alert_raised',body,now);
      append(intakeId,key+':owner',actor,'intake_context_updated',{
        ...context(intakeId),owner:'dev@dtcdev.click',
        current_state:`Host terminal receipt needs investigation (${body.kind}: ${body.reason}; intake ${intakeId}; session ${body.session_id}).`,
        next_step:'Developer: inspect the host journal receipt and intake evidence.'},now);
      db.run('UPDATE intakes SET updated_at=? WHERE id=?',now,intakeId);
    }
    return json({ok:true});
  });
}
