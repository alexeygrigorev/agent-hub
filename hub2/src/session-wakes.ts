/** One-shot session wakes. KV records are additive to schema10 and survive rollback.
 * Active records are the queue itself, not a separately committed pointer/index.
 * Terminal archives precede active deletion; replay always prefers the archive.
 */
import {sha256Hex} from './crypto';
import {HttpError} from './http';
import type {JsonRecord} from './types';
export const WAKE_ACK_MS=600_000;
const ACTIVE='session-wake-active/',ARCHIVE='session-wake-history/';
export type WakeSession={id:string;operation_id:string;epoch:number;native_id:string|null;state:string;project_id:string;intake_id:string;machine_id:string;interactive:boolean;workspace:string};
export type WakeRecord=WakeSession&{id:string;session_id:string;owner:string;request_key:string;prompt:string;wake_at_ms:number;created_at:number;updated_at:number;state:string;command_id:string;native_birth:number|null;delivered_at:number|null;acked_at:number|null;reason:string|null;missed_at:number|null;notified:boolean;next_check:number};
type Hooks={session:(id:string)=>WakeSession|undefined;enqueue:(w:WakeRecord)=>boolean;command:(w:WakeRecord)=>{state:string;result?:JsonRecord;updated_at:number}|undefined;missed:(w:WakeRecord)=>Promise<void>};
export class SessionWakes{
 private tail:Promise<unknown>=Promise.resolve();private active=new Map<string,WakeRecord>();
 constructor(private storage:DurableObjectStorage,private hooks:Hooks){}
 private serial<T>(fn:()=>Promise<T>):Promise<T>{const p=this.tail.catch(()=>{}).then(fn);this.tail=p;return p;}
 private key(w:WakeRecord){return w.session_id+'/'+w.id;}
 private async read(key:string){return await this.storage.get<WakeRecord>(ARCHIVE+key)??await this.storage.get<WakeRecord>(ACTIVE+key);}
 async load(){
  if(typeof this.storage.list!=='function')return; // old non-wake unit storage adapters
  const loaded=new Map<string,WakeRecord>();let startAfter:string|undefined;
  for(;;){
   const rows=await this.storage.list<WakeRecord>({prefix:ACTIVE,limit:200,startAfter});
   for(const [key,row] of rows){
    const archive=await this.storage.get<WakeRecord>(ARCHIVE+key.slice(ACTIVE.length));
    if(archive){if(this.ackHold(archive,Date.now()))loaded.set(archive.id,{...archive,next_check:this.deadline(archive)});else await this.storage.delete(key);}else loaded.set(row.id,row);
    startAfter=key;
   }
   if(rows.size<200)break;
  }
  this.active=loaded;
 }
 async refresh(){return this.serial(()=>this.load());}
 private async save(w:WakeRecord){
  const terminal=['acked','missed'].includes(w.state)&&w.notified;
  if(terminal){await this.storage.put(ARCHIVE+this.key(w),w);if(this.ackHold(w,Date.now())){w.next_check=this.deadline(w);await this.storage.put(ACTIVE+this.key(w),w);this.active.set(w.id,w);}else{await this.storage.delete(ACTIVE+this.key(w));this.active.delete(w.id);}}
  else{await this.storage.put(ACTIVE+this.key(w),w);this.active.set(w.id,w);}
 }
 private same(w:WakeRecord,s:WakeSession|undefined){return !!s&&s.operation_id===w.operation_id&&s.epoch===w.epoch&&s.machine_id===w.machine_id&&s.project_id===w.project_id&&s.intake_id===w.intake_id&&(!w.native_id||s.native_id===w.native_id);}
 private eligible(w:WakeRecord){const s=this.hooks.session(w.session_id);return this.same(w,s)&&!!s&&['running','claimed','offered'].includes(s.state);}
 private deadline(w:WakeRecord){return(w.delivered_at??w.wake_at_ms)+WAKE_ACK_MS;}
 private ackHold(w:WakeRecord,now:number){return w.state==='acked'&&w.missed_at===null&&w.delivered_at!==null&&w.acked_at!==null&&w.acked_at<=this.deadline(w)&&now<this.deadline(w)&&this.eligible(w);}
 holdsIdle(session:string,now:number){return [...this.active.values()].some(w=>w.session_id===session&&this.eligible(w)&&now<this.deadline(w)&&(['pending','queued','delivered'].includes(w.state)||this.ackHold(w,now)));}
 async create(s:WakeSession,owner:string,key:string,at:unknown,prompt:unknown){return this.serial(async()=>{
  if(typeof key!=='string'||!/^[A-Za-z0-9_.:-]{1,120}$/.test(key)||typeof prompt!=='string'||!prompt.trim()||prompt.trim().length>8000||new TextEncoder().encode(prompt.trim()).length>8192||typeof at!=='string')throw new HttpError('invalid_wake',400);
  const now=Date.now(),isNow=at.trim().toLowerCase()==='now',due=isNow?now:Date.parse(at.trim());
  if(!isNow&&!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(at.trim())||!Number.isSafeInteger(due))throw new HttpError('invalid_wake_at',400);
  if(due>now+365*86400_000)throw new HttpError('wake_at_too_far',400);
  const id=await sha256Hex(JSON.stringify([s.id,owner,key])),prior=await this.read(s.id+'/'+id);
  if(prior){if(prior.prompt!==prompt.trim()||!isNow&&prior.wake_at_ms!==due)throw new HttpError('idempotency_conflict',409);return{wake:prior,replay:true};}
  const current=this.hooks.session(s.id);if(!this.same({...s,session_id:s.id} as WakeRecord,current)||!['running','claimed','offered'].includes(current!.state))throw new HttpError('session_not_wakeable',409);
  await this.load();
  const pending=[...this.active.values()].filter(w=>!['acked','missed'].includes(w.state));
  if(pending.length>=2000||pending.filter(w=>w.session_id===s.id).length>=200)throw new HttpError('wake_queue_full',429); // existing legacy bounds
  if(!this.same({...s,session_id:s.id} as WakeRecord,this.hooks.session(s.id)))throw new HttpError('stale_wake',409);
  const w:WakeRecord={...s,id,session_id:s.id,owner,request_key:key,prompt:prompt.trim(),wake_at_ms:due,created_at:now,updated_at:now,state:'pending',command_id:crypto.randomUUID(),native_birth:null,delivered_at:null,acked_at:null,reason:null,missed_at:null,notified:false,next_check:due};
  await this.save(w);return{wake:w,replay:false};
 });}
 async sweep(){return this.serial(async()=>{await this.load();for(const saved of [...this.active.values()]){
  let w=structuredClone(saved);const now=Date.now();
  const current=this.hooks.session(w.session_id);
  if(w.state==='pending'&&(!this.same(w,current)||!current||!['running','claimed','offered'].includes(current.state)))w=this.miss(w,'wake_session_gone',now);
  if(w.state==='pending'&&w.next_check<=now){
   if(!this.same(w,current)||!current||!['running','claimed','offered'].includes(current.state)){w=this.miss(w,'wake_session_gone',now);}
   else if(now>=w.wake_at_ms+WAKE_ACK_MS)w=this.miss(w,'wake_delivery_deadline_exceeded',now);
   else if(!current.interactive)w=this.miss(w,'headless_send_refused',now);
   else if(current.state==='running'&&current.native_id){
    if(w.native_id===null){w.native_id=current.native_id;w.workspace=current.workspace;}
    // Binding and deterministic command id are durable BEFORE any native dispatch.
    await this.save(w);
    if(!this.same(w,this.hooks.session(w.session_id)))w=this.miss(w,'stale_wake',Date.now());
    else if(this.hooks.enqueue(w)){w.state='queued';w.next_check=now+1000;}
    else w.next_check=Math.min(w.wake_at_ms+WAKE_ACK_MS,now+1000);
   }else w.next_check=Math.min(w.wake_at_ms+WAKE_ACK_MS,now+1000);
  }
  if(w.state==='queued'||w.state==='delivered'){
   const command=this.hooks.command(w),result=command?.result;
   const inbox=result?.wake_delivered_at_ms;
   if(w.delivered_at===null&&typeof inbox==='number'&&inbox<=w.wake_at_ms+WAKE_ACK_MS){w.delivered_at=inbox;w.state='delivered';}
   const deadline=w.delivered_at===null?w.wake_at_ms+WAKE_ACK_MS:w.delivered_at+WAKE_ACK_MS;
   if(command?.state==='acked'&&result?.wake_proof){
    const delivered=typeof result.delivered_at_ms==='number'?result.delivered_at_ms:command.updated_at;
    if(delivered>w.wake_at_ms+WAKE_ACK_MS||command.updated_at>delivered+WAKE_ACK_MS)w=this.miss(w,'wake_ack_after_10m',now);
    w.delivered_at=delivered;w.acked_at=command.updated_at;w.state='acked';
   }else if(!this.same(w,current)||!current||!['running','claimed','offered'].includes(current.state))w=this.miss(w,'wake_session_gone',now);
   else if(command?.state==='failed')w=this.miss(w,'wake_native_delivery_failed',now);
   else if(now>=deadline)w=this.miss(w,w.delivered_at===null?'wake_delivery_receipt_deadline_exceeded':'wake_not_acked_within_10m',now);
   else w.next_check=Math.min(deadline,now+1000);
  }
  if(w.missed_at!==null&&!w.notified){await this.hooks.missed(w);w.notified=true;}
  if(w.state==='acked'&&w.missed_at===null&&!w.notified){await this.hooks.missed(w);w.notified=true;}
  await this.save(w);
 }});}
 private miss(w:WakeRecord,reason:string,now:number){return{...w,state:'missed',reason,missed_at:w.missed_at??now,updated_at:now,next_check:now+1000};}
 due(){let due:number|null=null;for(const w of this.active.values()){const n=this.eligible(w)?w.next_check:Date.now();if(due===null||n<due)due=n;}return due;}
 waiting(session:string){const rows=[...this.active.values()].filter(w=>w.session_id===session&&w.state==='pending'&&w.wake_at_ms>Date.now()&&this.eligible(w)).sort((a,b)=>a.wake_at_ms-b.wake_at_ms);return rows[0]?{waiting_until:new Date(rows[0].wake_at_ms).toISOString(),wake_id:rows[0].id}:{};}
 async lookup(session:string,id:string){return this.read(session+'/'+id);}
 async reconcile(session:string,id:string){return this.serial(async()=>{
  const stored=await this.read(session+'/'+id);if(!stored)return;
  const w=structuredClone(stored),c=this.hooks.command(w);if(c?.state!=='acked'||!c.result?.wake_proof)return;
  const delivered=typeof c.result.delivered_at_ms==='number'?c.result.delivered_at_ms:c.updated_at;
  if(delivered>w.wake_at_ms+WAKE_ACK_MS||c.updated_at>delivered+WAKE_ACK_MS){w.missed_at??=Date.now();w.reason??='wake_ack_after_10m';w.notified=false;}
  w.delivered_at=delivered;w.acked_at=c.updated_at;w.state='acked';w.updated_at=Date.now();
  if(stored.state!=='acked'||!w.notified){await this.hooks.missed(w);w.notified=true;}
  await this.save(w);
 });}
 async history(session:string){const prefix=ARCHIVE+session+'/';const archived=await this.storage.list<WakeRecord>({prefix,limit:100});const all=new Map([...archived.values()].map(w=>[w.id,w]));for(const w of this.active.values())if(w.session_id===session)all.set(w.id,w);return [...all.values()].sort((a,b)=>a.wake_at_ms-b.wake_at_ms).slice(0,100).map(w=>this.view(w));}
 view(w:WakeRecord){return{wake_id:w.id,session_id:w.session_id,wake_at:new Date(w.wake_at_ms).toISOString(),wake_state:w.state,command_id:w.state==='pending'?null:w.command_id,reason:w.reason,missed_at:w.missed_at===null?null:new Date(w.missed_at).toISOString(),delivered_at:w.delivered_at===null?null:new Date(w.delivered_at).toISOString(),acked_at:w.acked_at===null?null:new Date(w.acked_at).toISOString()};}
}
/** Same accepted native-delivery shape as legacy validateSendProof; no processing claim. */
export function wakeProof(proof:unknown,binding:JsonRecord):proof is JsonRecord{
 if(binding.transport!==undefined||!proof||typeof proof!=='object'||Array.isArray(proof))return false;const p=proof as JsonRecord;
 return Object.keys(p).sort().join(',')==='kind,native_message_id,sender_created_at_ms,sender_generation,sender_native_id,sender_tag,sender_workspace,status'
  &&p.kind==='native_delivery'&&['submitted','already-submitted','recipient-acked'].includes(String(p.status))
  &&typeof p.native_message_id==='string'&&/^[0-9a-f-]{36}$/i.test(p.native_message_id)
  &&p.sender_native_id===binding.native_id&&p.sender_tag===binding.tag&&p.sender_workspace===binding.workspace
  &&p.sender_generation===binding.epoch&&p.sender_created_at_ms===binding.created_at_ms
  &&p.native_message_id===binding.native_message_id;
}

/** Service control uses the existing identity-bound strict native receipt.
 * No mailbox ID is invented; native operation_id is this command UUID.
 */
export async function strictWakeProof(proof:unknown,binding:JsonRecord,commandId:string,text:string):Promise<boolean>{
 if(!['strict-submit:v1','strict-inbox:v1'].includes(String(binding.transport))||!proof||typeof proof!=='object'||Array.isArray(proof))return false;
 const p=proof as JsonRecord;
 const inbox=binding.transport==='strict-inbox:v1';
 if(Object.keys(p).sort().join(',')!==(inbox?'body_sha256,envelope_id,epoch,kind,operation_id,receipt':'epoch,kind,operation_id,receipt')||p.kind!==(inbox?'strict_inbox_notice_v1':'strict_submit_v1')||p.epoch!==binding.epoch||p.operation_id!==binding.operation_id)return false;
 if(inbox&&(typeof p.envelope_id!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(p.envelope_id)||p.body_sha256!==await sha256Hex(text)))return false;
 const notice=inbox?'Read inbox message '+p.envelope_id:text;
 const r=p.receipt;if(!r||typeof r!=='object'||Array.isArray(r))return false;const v=r as JsonRecord;
 const keys=Object.keys(v).sort().join(',');
 if(keys!=='consumed,deadline_ms,landed,mutation,operation_id,reason,request_sha256,session_id,status,submitted,version'
  &&keys!=='consumed,deadline_ms,landed,mutation,operation_id,reason,replayed,request_sha256,session_id,status,submitted,version')return false;
 if(v.replayed!==undefined&&typeof v.replayed!=='boolean')return false;
 if(v.version!==1||v.session_id!==binding.native_id||v.operation_id!==commandId||v.deadline_ms!==binding.deadline_ms
  ||v.status!=='submitted_verified'||v.mutation!==true||v.landed!==true||v.submitted!==true||v.consumed!==null||v.reason!=='submission_observed')return false;
 const expected=await sha256Hex(JSON.stringify([binding.native_id,commandId,binding.tag,binding.workspace,binding.created_at_ms,binding.deadline_ms,notice]));
 return v.request_sha256===expected;
}
