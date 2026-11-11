/** Operator commands on exact native identities. No managed-session lifecycle effects. */
import {MeteredDb} from './db';
import {sha256Hex} from './crypto';
import {actorFrom,bodyObject,error,HttpError,json} from './http';
import {nativeReturncode} from './native-exit';
import {MachineNativeInventory} from './machine-native-inventory';
import type {JsonRecord} from './types';

export const NATIVE_COMMAND_SCHEMA=[
 `CREATE TABLE native_commands(id TEXT PRIMARY KEY,machine_id TEXT NOT NULL,native_id TEXT NOT NULL,
  request_key TEXT NOT NULL,request_digest TEXT NOT NULL,kind TEXT NOT NULL,identity_json TEXT NOT NULL,
  mode TEXT,state TEXT NOT NULL,result_json TEXT,created_at INTEGER NOT NULL,deadline_ms INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,UNIQUE(native_id,request_key)) WITHOUT ROWID`,
 'CREATE INDEX native_command_pending ON native_commands(state,deadline_ms,created_at,id)',
];
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
type Row={id:string;machine_id:string;native_id:string;request_key:string;request_digest:string;kind:string;
 identity_json:string;mode:string|null;state:string;result_json:string|null;created_at:number;deadline_ms:number;updated_at:number};
type Submission={operation_id:string;created_at_ms:number;deadline_ms:number;text:string;request_sha256:string};
function sendText(value:unknown):value is string{return typeof value==='string'&&!!value.trim()
 &&new TextEncoder().encode(value).length<=8192&&!/[\uD800-\uDFFF]/u.test(value)&&!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(value);}
function intent(row:Row){const stored=JSON.parse(row.identity_json);return row.kind==='send'?stored:{identity:stored};}
function admin(request:Request){const a=actorFrom(request);return a?.kind==='operator'&&a.scopes.includes('operator:admin');}
export class MachineNativeCommands {
 constructor(private db:MeteredDb,private ctx:DurableObjectState,private inventory:MachineNativeInventory){}
 private managed(id:string){return !!this.db.one('SELECT id FROM sessions WHERE native_id=?',id);}
 private registered(){return this.db.one<{machine_id:string}>('SELECT machine_id FROM machines LIMIT 1')?.machine_id;}
 private publicView(row:Row):JsonRecord{
  const result=row.result_json?JSON.parse(row.result_json):null;
  return {id:row.id,command_id:row.id,target_kind:'native',machine_id:row.machine_id,native_id:row.native_id,
   kind:row.kind,state:row.state,created_at:row.created_at,deadline_ms:row.deadline_ms,
   ...(result?{ack:{...result,...(row.kind==='send'?{consumed:null}:{}),...(row.kind==='capture'&&typeof result.output==='string'?{capture:result.output}:{}),
    confirmed_exit:row.kind==='stop'&&row.state==='acked'&&result.stopped===true}}:{})};
 }
 envelope(row:Row):JsonRecord{return {id:row.id,target_kind:'native',protocol:1,machine_id:row.machine_id,
  native_id:row.native_id,kind:row.kind,identity:intent(row).identity,deadline_ms:row.deadline_ms,
  ...(row.kind==='send'?{submission:intent(row).submission}:{}),
  ...(row.mode?{mode:row.mode}:{})};}
 async handle(request:Request,nativeId:string,commandId?:string,ack=false):Promise<Response>{
  if(!UUID.test(nativeId)||commandId!==undefined&&!UUID.test(commandId))return error('invalid_native_target',400);
  if(this.db.schemaVersion<9)return error('schema_migration_deferred',503);
  if(ack)return request.method==='POST'?this.ack(request,nativeId,commandId!):error('method_not_allowed',405);
  if(!admin(request))return error('forbidden',403);
  if(request.method==='GET'){
   if(commandId){const row=this.db.one<Row>('SELECT * FROM native_commands WHERE id=? AND native_id=?',commandId,nativeId);
    return row?json({command:this.publicView(row)}):error('command_not_found',404);}
   return json({commands:this.db.all<Row>('SELECT * FROM native_commands WHERE native_id=? ORDER BY created_at DESC,id LIMIT 100',nativeId).map(r=>this.publicView(r))});
  }
  if(request.method!=='POST'||commandId)return error('method_not_allowed',405);
  const b=await bodyObject(request,65536),kind=b.kind,key=request.headers.get('Idempotency-Key')||b.key;
  if(kind!=='stop'&&kind!=='capture'&&kind!=='send')return error('invalid_command_kind',400);
  if(Object.keys(b).some(k=>!['kind','key','mode',...(kind==='send'?['text']:[])].includes(k))||b.mode!==undefined&&(kind!=='capture'||b.mode!=='screen'))return error('invalid_native_command',400);
  if(typeof key!=='string'||!/^[A-Za-z0-9_.:-]{1,120}$/.test(key))return error('idempotency_key_required',400);
  if(kind==='send'&&!sendText(b.text))return error('invalid_native_send_text',400);
  const digest=await sha256Hex(JSON.stringify({native_id:nativeId,kind,mode:b.mode??null,...(kind==='send'?{text:b.text}:{})}));
  // Prepare the immutable native operation outside the synchronous SQL block;
  // recheck its exact census binding inside the transaction after hashing.
  let prepared:{identity:JsonRecord;submission:Submission}|undefined;
  if(kind==='send'&&!this.db.one('SELECT id FROM native_commands WHERE native_id=? AND request_key=?',nativeId,key)){
   if(this.managed(nativeId))throw new HttpError('native_target_managed',409);
   if(this.inventory.status().fresh!==true)throw new HttpError('native_inventory_unavailable',409);
   const target=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',nativeId);
   if(!target)throw new HttpError('native_target_not_found',404);
   const observed=JSON.parse(target.row_json) as JsonRecord;
   if(!Number.isSafeInteger(observed.created_at_ms)||Number(observed.created_at_ms)<=0
    ||observed.phase!=='running'||observed.state!=='running'||observed.worker_alive!==true)throw new HttpError('native_target_not_running',409);
   const operation=crypto.randomUUID(),deadline=Date.now()+60000;
   const identity={id:nativeId,workspace:observed.workspace,tag:observed.tag};
   const request_sha256=await sha256Hex(JSON.stringify([nativeId,operation,identity.tag,identity.workspace,observed.created_at_ms,deadline,b.text]));
   prepared={identity,submission:{operation_id:operation,created_at_ms:Number(observed.created_at_ms),deadline_ms:deadline,text:b.text as string,request_sha256}};
  }
  const outcome=this.db.transaction(this.ctx,()=>{
   const prior=this.db.one<Row>('SELECT * FROM native_commands WHERE native_id=? AND request_key=?',nativeId,key);
   if(prior){if(prior.request_digest!==digest)throw new HttpError('idempotency_conflict',409);return {row:prior,replayed:true};}
   if(this.managed(nativeId))throw new HttpError('native_target_managed',409);
   if(this.inventory.status().fresh!==true)throw new HttpError('native_inventory_unavailable',409);
   const target=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',nativeId);
   const machine=this.registered();if(!target||!machine)throw new HttpError('native_target_not_found',404);
   const identity=JSON.parse(target.row_json) as JsonRecord;
   if(kind==='send'&&(!prepared||prepared.identity.workspace!==identity.workspace||prepared.identity.tag!==identity.tag
    ||prepared.submission.created_at_ms!==identity.created_at_ms||identity.phase!=='running'||identity.state!=='running'||identity.worker_alive!==true))throw new HttpError('native_target_identity_changed',409);
   const id=prepared?.submission.operation_id??crypto.randomUUID(),now=Date.now(),deadline=prepared?.submission.deadline_ms??now+60000;
   this.db.run(`INSERT INTO native_commands VALUES(?,?,?,?,?,?,?,?, 'pending',NULL,?,?,?)`,id,machine,nativeId,key,digest,kind,
    JSON.stringify(prepared??{id:nativeId,workspace:identity.workspace,tag:identity.tag}),b.mode??null,now,deadline,now);
   return {row:this.db.one<Row>('SELECT * FROM native_commands WHERE id=?',id)!,replayed:false};
  });
  return json({command:this.publicView(outcome.row),command_id:outcome.row.id,state:outcome.row.state,replayed:outcome.replayed},outcome.replayed?200:201);
 }
 expire(now=Date.now()){
  if(this.db.schemaVersion<9)return;
  // A target can become managed after enqueue. Cancel its independent intent
  // before socket delivery; the host repeats the journal ownership check.
  this.db.run(`UPDATE native_commands SET state='failed',result_json=?,updated_at=? WHERE state='pending'
   AND EXISTS(SELECT 1 FROM sessions s WHERE s.native_id=native_commands.native_id)`,
   JSON.stringify({error:'native_target_managed',outcome:'unconfirmed'}),now);
  this.db.run(`UPDATE native_commands SET state='failed',result_json=?,updated_at=? WHERE state='pending' AND deadline_ms<=?`,
   JSON.stringify({error:'native_command_deadline',outcome:'unconfirmed'}),now,now);
 }
 next():JsonRecord|undefined{
  if(this.db.schemaVersion<9)return;
  this.expire();
  const row=this.db.one<Row>("SELECT * FROM native_commands WHERE state='pending' ORDER BY CASE kind WHEN 'stop' THEN 0 ELSE 1 END,created_at,id LIMIT 1");
  if(row?.kind==='send'){
   const planned=intent(row),current=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',row.native_id);
   const observed=current?JSON.parse(current.row_json):null;
   const failure=this.inventory.status().fresh!==true?'native_inventory_unavailable'
    :!observed||observed.id!==row.native_id||observed.tag!==planned.identity.tag||observed.workspace!==planned.identity.workspace
     ||observed.created_at_ms!==planned.submission.created_at_ms?'native_target_identity_changed'
    :observed.phase!=='running'||observed.state!=='running'||observed.worker_alive!==true?'native_target_not_running':null;
   if(failure){this.db.run("UPDATE native_commands SET state='failed',result_json=?,updated_at=? WHERE id=? AND state='pending'",
    JSON.stringify({error:failure,outcome:'unconfirmed',consumed:null}),Date.now(),row.id);return;}
  }
  return row?this.envelope(row):undefined;
 }
 private async ack(request:Request,nativeId:string,id:string):Promise<Response>{
  const actor=actorFrom(request);
  if(actor?.kind!=='machine'||!actor.scopes.includes('session:report')||actor.resource_id!==this.registered()
   ||request.headers.has('x-hub2-safety-mode'))return error('forbidden',403);
  // Identity bounds include 8KiB workspace + 512B tag; Python ASCII JSON can expand them sixfold.
  const b=await bodyObject(request,65536),r=b.result as JsonRecord;
  if(!r||typeof r!=='object'||Array.isArray(r)||new TextEncoder().encode(JSON.stringify(r)).length>60000)return error('invalid_native_ack',400);
  return this.db.transaction(this.ctx,()=>{
   const row=this.db.one<Row>('SELECT * FROM native_commands WHERE id=? AND native_id=? AND machine_id=?',id,nativeId,actor.resource_id!);
   if(!row)return error('stale_command',409);
   const planned=intent(row),expected=planned.identity as JsonRecord;
   if(b.deadline_ms!==row.deadline_ms||b.native_id!==nativeId)return error('stale_command',409);
   // Only whitelisted output is retained; raw native metadata never enters receipts.
   let result:JsonRecord;
   if(row.kind==='stop'&&r.stopped===true){
    const proof=r.evidence as JsonRecord;
    if(!proof||(proof.source!=='aplexer_status'&&proof.source!=='aplexer_retirement')||proof.native_id!==nativeId
     ||proof.workspace!==expected.workspace||proof.tag!==expected.tag||proof.containment_empty!==true
     ||proof.phase!=='exited'||nativeReturncode(proof.exit)===undefined
     ||!Number.isSafeInteger((proof.exit as JsonRecord)?.exited_at_ms)||Number((proof.exit as JsonRecord).exited_at_ms)<=0
     ||Number((proof.exit as JsonRecord).exited_at_ms)>Number(proof.observed_at_ms)+30000||!Number.isSafeInteger(proof.observed_at_ms)
     ||Number(proof.observed_at_ms)<row.created_at||Number(proof.observed_at_ms)>row.deadline_ms
     ||Number(proof.observed_at_ms)>Date.now()+30000)return error('native_exit_evidence_required',400);
    result={stopped:true,evidence:{source:proof.source,native_id:nativeId,workspace:expected.workspace,tag:expected.tag,
     containment_empty:true,phase:'exited',exit:{code:(proof.exit as JsonRecord).code,
      ...((proof.exit as JsonRecord).signal!==undefined?{signal:(proof.exit as JsonRecord).signal}:{}),
      exited_at_ms:(proof.exit as JsonRecord).exited_at_ms},observed_at_ms:proof.observed_at_ms}};
   }else if(row.kind==='capture'&&typeof r.output==='string'){
    if(new TextEncoder().encode(r.output).length>4096||/[\x00-\x08\x0b-\x1f\x7f-\x9f]/.test(r.output))return error('invalid_capture_output',400);
    result={output:r.output};
   }else if(row.kind==='send'&&r.submission){
    const receipt=r.submission as JsonRecord,submission=planned.submission as Submission;
    if(!receipt||typeof receipt!=='object'||Array.isArray(receipt)||receipt.version!==1||receipt.session_id!==nativeId
     ||receipt.operation_id!==row.id||receipt.request_sha256!==submission.request_sha256||receipt.deadline_ms!==row.deadline_ms
     ||receipt.consumed!==null||typeof receipt.reason!=='string'||!/^[a-z_]{1,80}$/.test(receipt.reason))return error('native_send_receipt_unverified',400);
    const verified=receipt.status==='submitted_verified'&&receipt.mutation===true&&receipt.landed===true&&receipt.submitted===true&&receipt.reason==='submission_observed';
    const refused=receipt.status==='refused'&&receipt.mutation===false&&receipt.landed===false&&receipt.submitted===false;
    const uncertain=receipt.status==='uncertain'&&[true,false,null].includes(receipt.mutation as boolean|null)
     &&[true,false,null,undefined].includes(receipt.landed as boolean|null)&&[true,false,null,undefined].includes(receipt.submitted as boolean|null);
    if(!verified&&!refused&&!uncertain)return error('native_send_receipt_unverified',400);
    const proof={version:1,session_id:nativeId,operation_id:row.id,request_sha256:submission.request_sha256,deadline_ms:row.deadline_ms,
     status:receipt.status,mutation:receipt.mutation,landed:receipt.landed??null,submitted:receipt.submitted??null,consumed:null,reason:receipt.reason,
     ...(receipt.replayed===true?{replayed:true}:{})};
    result={submission:proof,submitted:verified,consumed:null,outcome:verified?'submitted_verified':'unconfirmed',
     ...(!verified?{error:refused?'native_send_refused':'native_send_uncertain'}:{})};
   }else{
    if(typeof r.error!=='string'||!/^[a-z_]{1,80}$/.test(r.error))return error('invalid_native_ack',400);
    result={error:r.error,outcome:'unconfirmed',...(row.kind==='stop'?{stopped:false}:row.kind==='send'?{consumed:null,submitted:false}:{})};
   }
   const serialized=JSON.stringify(result);
   if(row.state!=='pending')return row.result_json===serialized?json({acknowledged:true,duplicate:true}):error('command_ack_conflict',409);
   if(Date.now()>=row.deadline_ms)return error('native_command_deadline',409);
   if(this.managed(nativeId))return error('native_target_managed',409);
   this.db.run("UPDATE native_commands SET state=?,result_json=?,updated_at=? WHERE id=? AND state='pending'",
    result.error?'failed':'acked',serialized,Date.now(),id);
   return json({acknowledged:true});
  });
 }
}
