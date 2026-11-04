import { MeteredDb, initializeSchema } from './db';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';
import type { Actor, JsonRecord } from './types';
type NativeOutbox={message_id:string;epoch:number;operation_id:string;recipient:string;body:string;session_id:string|null;machine_id:string|null;state:string;next_attempt:number|null;attempts:number;receipt_json:string|null};
type DeliveryClaim={message_id:string;epoch:number;operation_id:string;claim_id:string;machine_id:string;session_id:string;receipt_digest:string|null;receipt_json:string|null;state:string};
type Receipt={message_id:string;stage:string;epoch:number;sender:string;recipient:string;attempts:number;state:string};
type MailEnv={MAILBOXES:DurableObjectNamespace;CONTROL:DurableObjectNamespace;MACHINES:DurableObjectNamespace};
type Mail={id:string;sender:string;recipient:string;body:string;key:string;digest:string;thread_id:string;reply_to:string|null;epoch:number;state:string;created_at:number;read_at:number|null;acked_at:number|null;accepted_at:number|null;completed_at:number|null;delivered_at:number|null;native_receipt_json:string|null;attention_json:string|null;target:string|null;readdress_key:string|null};
const SCHEMA=[
 'CREATE TABLE native_outbox(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,session_id TEXT,machine_id TEXT,state TEXT NOT NULL,next_attempt INTEGER,attempts INTEGER NOT NULL DEFAULT 0,receipt_json TEXT,due INTEGER GENERATED ALWAYS AS (next_attempt IS NOT NULL) VIRTUAL,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
 'CREATE INDEX native_outbox_due ON native_outbox(due,next_attempt,message_id,epoch) WHERE due=1',
 "CREATE INDEX native_unrouted ON native_outbox(message_id,epoch) WHERE state IN ('unrouted','pending-machine','queued')",

 'CREATE TABLE delivery_claims(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,claim_id TEXT NOT NULL,machine_id TEXT NOT NULL,session_id TEXT NOT NULL,receipt_digest TEXT,receipt_json TEXT,native_message_id TEXT,state TEXT NOT NULL,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
 'CREATE UNIQUE INDEX native_receipt_once ON delivery_claims(machine_id,native_message_id) WHERE native_message_id IS NOT NULL',
 'CREATE TABLE readdress_operations(key TEXT PRIMARY KEY,message_id TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT) WITHOUT ROWID',

 'CREATE TABLE identity(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT NOT NULL)',
 `CREATE TABLE outbox(id TEXT PRIMARY KEY,sender TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,key TEXT NOT NULL UNIQUE,digest TEXT NOT NULL,thread_id TEXT NOT NULL,reply_to TEXT,epoch INTEGER NOT NULL,state TEXT NOT NULL,created_at INTEGER NOT NULL,read_at INTEGER,acked_at INTEGER,accepted_at INTEGER,completed_at INTEGER,delivered_at INTEGER,native_receipt_json TEXT,attention_json TEXT,target TEXT,readdress_key TEXT,next_attempt INTEGER,attempts INTEGER NOT NULL DEFAULT 0,due INTEGER GENERATED ALWAYS AS (next_attempt IS NOT NULL) VIRTUAL) WITHOUT ROWID`,
 'CREATE INDEX outbox_due ON outbox(due,next_attempt,id) WHERE due=1',
 `CREATE TABLE inbox(id TEXT PRIMARY KEY,sender TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,thread_id TEXT NOT NULL,reply_to TEXT,epoch INTEGER NOT NULL,state TEXT NOT NULL,created_at INTEGER NOT NULL,read_at INTEGER,acked_at INTEGER,accepted_at INTEGER,completed_at INTEGER,delivered_at INTEGER,native_receipt_json TEXT,attention_json TEXT,visible INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID`,
 'CREATE INDEX inbox_page ON inbox(visible,created_at,id)',
 'CREATE INDEX inbox_state_page ON inbox(visible,state,created_at,id)',
 'CREATE INDEX inbox_thread_page ON inbox(visible,thread_id,created_at,id)',
 'CREATE TABLE receipts(message_id TEXT NOT NULL,stage TEXT NOT NULL,epoch INTEGER NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,state TEXT NOT NULL,next_attempt INTEGER,created_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due INTEGER GENERATED ALWAYS AS (next_attempt IS NOT NULL) VIRTUAL,PRIMARY KEY(message_id,stage,epoch)) WITHOUT ROWID',
 'CREATE INDEX receipt_due ON receipts(due,next_attempt,message_id,stage,epoch) WHERE due=1',
 'CREATE TABLE fences(id TEXT PRIMARY KEY,epoch INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE TABLE events(message_id TEXT NOT NULL,stage TEXT NOT NULL,epoch INTEGER NOT NULL,at INTEGER NOT NULL,PRIMARY KEY(message_id,stage,epoch)) WITHOUT ROWID',
];
/** Sender owns deduplication globally across destinations. Recipient projections are epoch fenced. */
export class MailboxDO{
 private readonly db:MeteredDb;
 private boundOwner?:string;
 constructor(private readonly ctx:DurableObjectState,private readonly env:MailEnv){this.db=new MeteredDb(ctx.storage.sql);initializeSchema(ctx,this.db,1,SCHEMA);ctx.blockConcurrencyWhile(async()=>{await this.schedule(false);});}
 async fetch(r:Request):Promise<Response>{
  try{
   const p=new URL(r.url).pathname;
   if(p==='/_metrics')return json({counts:this.db.snapshot()});
   // Namespace-only contracts: the edge MUST reject every /_ path.
   if(p.startsWith('/_')&&r.method==='POST')return await this.internal(p,r);
   const a=actorFrom(r),owner=r.headers.get('x-hub2-mailbox')||a?.id;
   if(!a||!owner||!this.authorized(a,owner))return error('forbidden',403);
   this.bind(owner);
   if(p==='/api/messages'&&r.method==='POST')return await this.send(r,owner);
   if(p==='/api/messages'&&r.method==='GET')return this.list(r);
   const m=p.match(/^\/api\/messages\/([^/]+)(?:\/(read|ack|readdress|delivered|accept|complete|status))?$/);
   if(!m)return error('not_found',404);
   const id=decodeURIComponent(m[1]);
   if(m[2]==='status'&&r.method==='GET'){
    const row=this.db.one<Mail>('SELECT * FROM inbox WHERE id=? AND visible=1',id)||this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',id);
    return row?json({message:this.view(row),events:this.stageEvents(row)}):error('message_not_found',404);
   }
   if(!m[2]&&r.method==='GET'){
    const row=this.db.one<Mail>('SELECT * FROM inbox WHERE id=? AND state<>?',id,'revoked')||this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',id);
    return row?json({message:this.view(row)}):error('message_not_found',404);
   }
   if(r.method!=='POST')return error('not_found',404);
   if(m[2]==='readdress')return await this.readdress(r,id,a);
   if(m[2]==='delivered')return await this.nativeDelivery(r,id,owner,a);
   if(m[2]==='read'||m[2]==='ack'||m[2]==='accept'||m[2]==='complete'){
    const b=await bodyObject(r,4096);if(!Number.isSafeInteger(b.epoch))return error('expected_epoch_required',400);return await this.receipt(id,m[2],owner,Number(b.epoch));
   }
   return error('not_found',404);
  }catch(e){return e instanceof HttpError?error(e.code,e.status):error('internal_error',500);}
 }
 private authorized(a:Actor,owner:string):boolean{return a.id===owner||hasScope(a,'operator:admin')||hasScope(a,`mailbox:${owner}`);}
 private bind(owner:string):void{
  if(!boundedString(owner,180))throw new HttpError('invalid_owner',400);
  if(this.boundOwner){if(this.boundOwner!==owner)throw new HttpError('mailbox_owner_mismatch',403);return;}
  const row=this.db.one<{owner:string}>('SELECT owner FROM identity WHERE id=1');
  if(row&&row.owner!==owner)throw new HttpError('mailbox_owner_mismatch',403);
  if(!row)this.db.run('INSERT INTO identity(id,owner) VALUES(1,?)',owner);this.boundOwner=owner;
 }
 private async send(r:Request,owner:string):Promise<Response>{
  const b=await bodyObject(r);
  if(!boundedString(b.recipient,180)||!boundedString(b.body,12000)||!boundedString(b.key,120)||(b.reply_to!==undefined&&!boundedString(b.reply_to,120)))return error('invalid_message',400);
  const digest=await sha256Hex(JSON.stringify([b.recipient,b.body,b.reply_to??null,b.parent_sender??null]));
  let thread:string=crypto.randomUUID();
  const existing=this.db.one<Mail>('SELECT * FROM outbox WHERE key=?',b.key);
  if(existing){if(existing.digest!==digest)return error('idempotency_conflict',409);if(['pending','pending-directory'].includes(existing.state))await this.deliver(existing);return json({message:this.view(this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',existing.id)!),replayed:true});}
  b.recipient=await this.resolveRecipient(String(b.recipient));
  if(b.reply_to){
   if(!boundedString(b.parent_sender,180))return error('parent_sender_required',400);
   const response=await this.call(b.parent_sender,'/_participant',{id:b.reply_to,participant:owner,recipient:b.recipient});
   if(!response.ok)return error('reply_forbidden',403);thread=(await response.json() as {thread_id:string}).thread_id;
  }
  const id=crypto.randomUUID(),now=Date.now();
  const mail=this.db.transaction(this.ctx,()=>{
   const prior=this.db.one<Mail>('SELECT * FROM outbox WHERE key=?',b.key);
   if(prior){if(prior.digest!==digest)throw new HttpError('idempotency_conflict',409);return prior;}
   this.db.run('INSERT INTO outbox(id,sender,recipient,body,key,digest,thread_id,reply_to,epoch,state,created_at,next_attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',id,owner,b.recipient,b.body,b.key,digest,thread,b.reply_to??null,1,'pending-directory',now,now);
   return this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',id)!;
  });
  await this.schedule();await this.deliver(mail);await this.schedule();
  return json({message:this.view(this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',mail.id)!)},201);
 }
 private list(r:Request):Response{
  const q=new URL(r.url).searchParams,limit=Number(q.get('limit')||25);
  if(!Number.isInteger(limit)||limit<1||limit>50)return error('invalid_limit',400);
  let cursor:[number,string]=[0,''];
  if(q.has('cursor')){try{cursor=JSON.parse(q.get('cursor')!);}catch{return error('invalid_cursor',400);}}
  if(!Array.isArray(cursor)||cursor.length!==2||!Number.isSafeInteger(cursor[0])||typeof cursor[1]!=='string')return error('invalid_cursor',400);
  const state=q.get('state'),thread=q.get('thread');

  if(state&&!['received','claimed','read','acked','delivered','accepted','completed'].includes(state))return error('invalid_state',400);
  // Combined filtering walks one bounded indexed state page and returns a
  // continuation even when no thread match occurs; it never scans a whole thread.
  const combined=!!state&&!!thread;
  const extra=combined||!thread&&state?'state=? AND ':thread?'thread_id=? AND ':'';
  const args:unknown[]=combined||!thread&&state?[state]:thread?[thread]:[];
  const rows=this.db.all<Mail>(`SELECT * FROM inbox WHERE visible=1 AND ${extra}(created_at,id)>(?,?) ORDER BY created_at,id LIMIT ?`,...args,...cursor,limit+1);
  const scanned=rows.slice(0,limit),shown=combined?scanned.filter(m=>m.thread_id===thread):scanned,last=scanned[scanned.length-1];
  return json({items:shown.map(m=>this.view(m)),next:rows.length>limit?JSON.stringify([last.created_at,last.id]):null,filtered_page:combined});
 }
 private async deliver(m:Mail):Promise<void>{
  if(!['pending','pending-directory'].includes(m.state))return;
  try{
   if(m.state==='pending-directory'){
    const registration=await this.control('/_routes/register',{kind:'message',id:m.id,owner:m.sender,resource_id:m.id,participants:[m.sender,m.recipient],epoch:m.epoch});
    if(!registration.ok)throw new Error('directory_registration_pending');
    this.db.run('UPDATE outbox SET state=? WHERE id=? AND epoch=? AND state=?','pending',m.id,m.epoch,'pending-directory');
    const current=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',m.id);if(!current||current.epoch!==m.epoch||current.state!=='pending')return;m=current;
   }
   const response=await this.call(m.recipient,'/_receive',{mail:m});if(!response.ok)throw new Error('delivery_refused');
   this.db.run('UPDATE outbox SET state=?,next_attempt=NULL WHERE id=? AND epoch=? AND state=?','received',m.id,m.epoch,'pending');this.event(m.id,'transported',m.epoch);
  }catch{this.defer(m.id,m.epoch);}
 }
 private async internal(p:string,r:Request):Promise<Response>{
  const b=await bodyObject(r);
  if(p==='/_resume-native'){
   const after=typeof b.after==='string'?b.after:'';
   const rows=this.db.all<NativeOutbox>("SELECT * FROM native_outbox WHERE state IN ('unrouted','pending-machine','queued') AND message_id>? ORDER BY message_id,epoch LIMIT 17",after);
   for(const row of rows.slice(0,16)){
    if(row.state==='unrouted'){this.db.run("UPDATE native_outbox SET state='pending-target',next_attempt=? WHERE message_id=? AND epoch=? AND state='unrouted'",Date.now(),row.message_id,row.epoch);continue;}
    const response=await this.control('/_recipient',{recipient:row.recipient});if(!response.ok)throw new HttpError('directory_unavailable',503);
    const target=await response.json() as {forward_session?:string|null};if(target.forward_session===row.session_id)continue;
    this.db.transaction(this.ctx,()=>{
     if(this.db.one('SELECT claim_id FROM delivery_claims WHERE message_id=? AND epoch=?',row.message_id,row.epoch))return;
     const attention=JSON.stringify({reason:'forward_target_changed',previous_session:row.session_id,current_session:target.forward_session??null,repair:'authorized_readdress_same_recipient_or_replacement_message'});
     this.db.run("UPDATE inbox SET attention_json=? WHERE id=? AND epoch=?",attention,row.message_id,row.epoch);
     this.db.run("UPDATE native_outbox SET state='attention-pending',receipt_json=?,next_attempt=? WHERE message_id=? AND epoch=? AND state IN ('pending-machine','queued')",attention,Date.now(),row.message_id,row.epoch);
    });
   }
   await this.schedule();return json({resumed:Math.min(rows.length,16),next:rows.length>16?rows[15].message_id:null});
  }
  if(p==='/_receive'){
   const m=b.mail as Mail;
   if(!m||!boundedString(m.id,120)||!boundedString(m.sender,180)||!boundedString(m.recipient,180)||!boundedString(m.body,12000)||!boundedString(m.digest,128)||!boundedString(m.key,120)||!boundedString(m.thread_id,120)||!Number.isSafeInteger(m.created_at)||!Number.isInteger(m.epoch)||m.epoch<1)return error('invalid_envelope',400);
   this.bind(m.recipient);
   const result=this.db.transaction(this.ctx,()=>{
    const fence=this.db.one<{epoch:number}>('SELECT epoch FROM fences WHERE id=?',m.id);if(fence&&m.epoch<=fence.epoch)return error('route_fenced',409);
    const prior=this.db.one<Mail>('SELECT * FROM inbox WHERE id=?',m.id);
    if(prior){if(prior.digest!==m.digest||prior.epoch>m.epoch)return error('route_conflict',409);if(prior.epoch===m.epoch)return prior.state==='revoked'?error('route_fenced',409):json({received:true});if(prior.state!=='revoked')return error('route_active',409);}
    this.db.run(`INSERT INTO inbox(id,sender,recipient,body,key,digest,thread_id,reply_to,epoch,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET recipient=excluded.recipient,epoch=excluded.epoch,state=excluded.state,read_at=NULL,acked_at=NULL,accepted_at=NULL,completed_at=NULL,delivered_at=NULL,native_receipt_json=NULL,attention_json=NULL,visible=1`,m.id,m.sender,m.recipient,m.body,m.key,m.digest,m.thread_id,m.reply_to,m.epoch,'received',m.created_at);
    this.event(m.id,'received',m.epoch);
    this.db.run('INSERT OR IGNORE INTO native_outbox(message_id,epoch,operation_id,recipient,body,state,next_attempt) VALUES(?,?,?,?,?,?,?)',m.id,m.epoch,'native:'+m.id+':'+m.epoch,m.recipient,m.body,'pending-target',Date.now());
    return json({received:true});
   });if(result.ok){await this.schedule();await this.flushNative(m.id,m.epoch);await this.schedule();}return result;
  }
  if(p==='/_participant'){
   const m=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',b.id);
   return m&&[m.sender,m.recipient].includes(String(b.participant))&&[m.sender,m.recipient].includes(String(b.recipient))?json({thread_id:m.thread_id}):error('forbidden',403);
  }
  if(p==='/_revoke'){
   if(!boundedString(b.id,120)||!Number.isInteger(b.epoch))return error('invalid_route',400);
   const result=this.db.transaction(this.ctx,()=>{
    const m=this.db.one<Mail>('SELECT * FROM inbox WHERE id=?',b.id);
    if(m&&(m.epoch!==b.epoch||['claimed','delivered','read','acked','accepted','completed'].includes(m.state)))return error('delivery_already_started',409);
    this.db.run('INSERT INTO fences(id,epoch) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET epoch=MAX(epoch,excluded.epoch)',b.id,b.epoch);
    if(m){this.db.run('UPDATE inbox SET state=?,visible=0 WHERE id=?','revoked',m.id);this.event(m.id,'revoked',m.epoch);
     this.db.run("UPDATE native_outbox SET state=CASE WHEN machine_id IS NULL THEN 'cancelled' ELSE 'pending-cancel' END,next_attempt=CASE WHEN machine_id IS NULL THEN NULL ELSE ? END WHERE message_id=? AND epoch=?",Date.now(),m.id,m.epoch);}
    return json({revoked:true});
   });if(result.ok){await this.schedule();if(typeof b.id==='string'&&typeof b.epoch==='number')await this.flushNative(b.id,b.epoch);await this.schedule();}return result;
  }
  if(p==='/_attention'){
   const m=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',b.id);
   if(!m||m.epoch!==b.epoch||m.recipient!==b.recipient)return error('stale_route_epoch',409);
   const detail=b.attention as JsonRecord;
   if(!detail||detail.reason!=='forward_target_changed'||!boundedString(detail.previous_session,120)||(detail.current_session!==null&&!boundedString(detail.current_session,120)))return error('invalid_attention',400);
   this.db.run('UPDATE outbox SET attention_json=? WHERE id=? AND epoch=?',JSON.stringify(detail),m.id,m.epoch);return json({recorded:true});
  }
  if(p==='/_receipt'){
   const m=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',b.id);
   if(!m||m.recipient!==b.recipient||m.epoch!==b.epoch||m.state==='revoking'||!['read','ack','delivered','accept','complete'].includes(String(b.stage)))return error('receipt_refused',409);
   if(b.stage==='delivered'){
    const proof=b.native_receipt as Record<string,unknown>|undefined;if(!proof||proof.provider!=='aplexer'||!boundedString(proof.message_id,180)||!boundedString(proof.session_id,120)||proof.recipient!==m.recipient||!boundedString(proof.machine_id,120))return error('native_delivery_evidence_required',409);
    this.db.run('UPDATE outbox SET delivered_at=COALESCE(delivered_at,?),native_receipt_json=COALESCE(native_receipt_json,?) WHERE id=? AND delivered_at IS NULL',Date.now(),JSON.stringify(proof),m.id);
   }
   if(b.stage!=='delivered'){const field=stageField(String(b.stage));this.db.run(`UPDATE outbox SET ${field}=COALESCE(${field},?) WHERE id=? AND ${field} IS NULL`,Date.now(),m.id);}
   return json({recorded:true});
  }
  return error('not_found',404);
 }
 private async nativeDelivery(request:Request,id:string,owner:string,actor:Actor):Promise<Response>{
  if(actor.kind!=='machine'||!actor.resource_id||!hasScope(actor,'message:delivery'))return error('native_delivery_forbidden',403);
  const b=await bodyObject(request,8192),session=request.headers.get('x-hub2-delivery-session');
  if(owner.startsWith('session:')&&owner.slice(8)!==session)return error('delivery_session_mismatch',403);
  if(!Number.isSafeInteger(b.epoch)||!boundedString(b.operation_id,120)||!boundedString(session,120)||!['claim','receipt'].includes(String(b.phase)))return error('invalid_native_delivery',400);
  if(b.phase==='claim')return this.db.transaction(this.ctx,()=>{
   const mail=this.db.one<Mail>('SELECT * FROM inbox WHERE id=? AND visible=1',id);if(!mail)return error('message_not_found',404);if(mail.epoch!==b.epoch)return error('stale_route_epoch',409);
   const target=this.db.one<NativeOutbox>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,b.epoch);
   if(target?.state==='pending-machine')return error('native_handoff_pending',409);
   if(!target||!['queued','pending-ack','acked'].includes(target.state)||target.machine_id!==actor.resource_id||target.session_id!==session)return error('native_target_mismatch',403);
   if(target.operation_id!==b.operation_id)return error('delivery_operation_mismatch',409);

   const prior=this.db.one<DeliveryClaim>('SELECT * FROM delivery_claims WHERE message_id=? AND epoch=?',id,b.epoch);
   if(prior)return prior.machine_id===actor.resource_id&&prior.operation_id===b.operation_id&&prior.session_id===session?json({phase:prior.state,claim_id:prior.claim_id,epoch:prior.epoch}):error('delivery_already_claimed',409);
   if(!['received','read','acked','accepted','completed'].includes(mail.state))return error('delivery_not_claimable',409);
   const claim=crypto.randomUUID();this.db.run('INSERT INTO delivery_claims(message_id,epoch,operation_id,claim_id,machine_id,session_id,state) VALUES(?,?,?,?,?,?,?)',id,b.epoch,b.operation_id,claim,actor.resource_id,session,'claimed');if(mail.state==='received')this.db.run('UPDATE inbox SET state=? WHERE id=?','claimed',id);return json({phase:'claimed',claim_id:claim,epoch:b.epoch},201);
  });
  const receipt=b.native_receipt as Record<string,unknown>|undefined;
  if(!boundedString(b.claim_id,120)||!receipt||receipt.provider!=='aplexer'||!boundedString(receipt.message_id,180)||receipt.session_id!==session||receipt.recipient!==owner)return error('invalid_native_receipt',400);
  const digest=await sha256Hex(JSON.stringify([receipt.provider,receipt.message_id,receipt.session_id,receipt.recipient]));
  let notice:Receipt|undefined;
  const result=this.db.transaction(this.ctx,()=>{
   const mail=this.db.one<Mail>('SELECT * FROM inbox WHERE id=? AND visible=1',id),claim=this.db.one<DeliveryClaim>('SELECT * FROM delivery_claims WHERE message_id=? AND epoch=?',id,b.epoch);
   if(!mail||mail.epoch!==b.epoch)return error('stale_route_epoch',409);
   if(!claim||claim.claim_id!==b.claim_id||claim.machine_id!==actor.resource_id||claim.operation_id!==b.operation_id||claim.session_id!==session)return error('delivery_claim_mismatch',409);
   if(claim.receipt_digest&&claim.receipt_digest!==digest)return error('native_receipt_conflict',409);
   if(!claim.receipt_digest){
    const reused=this.db.one<{message_id:string;epoch:number}>('SELECT message_id,epoch FROM delivery_claims WHERE machine_id=? AND native_message_id=?',actor.resource_id,receipt.message_id);if(reused&&(reused.message_id!==id||reused.epoch!==b.epoch))return error('native_receipt_reused',409);
    const evidence=JSON.stringify({provider:'aplexer',message_id:receipt.message_id,session_id:session,recipient:owner,machine_id:actor.resource_id});
    this.db.run('UPDATE delivery_claims SET state=?,receipt_digest=?,receipt_json=?,native_message_id=? WHERE message_id=? AND epoch=?','delivered',digest,evidence,receipt.message_id,id,b.epoch);
    this.db.run("UPDATE inbox SET state=CASE WHEN state='claimed' THEN 'delivered' ELSE state END,delivered_at=COALESCE(delivered_at,?),native_receipt_json=COALESCE(native_receipt_json,?) WHERE id=?",Date.now(),evidence,id);
   }
   this.db.run("UPDATE native_outbox SET state='pending-ack',receipt_json=?,next_attempt=? WHERE message_id=? AND epoch=? AND state IN ('queued','pending-ack')",JSON.stringify({provider:'aplexer',message_id:receipt.message_id,session_id:session,recipient:owner}),Date.now(),id,b.epoch);
   if(!claim.receipt_digest){
    this.db.run('INSERT INTO receipts(message_id,stage,epoch,sender,recipient,state,next_attempt,created_at) VALUES(?,?,?,?,?,?,?,?)',id,'delivered',b.epoch,mail.sender,owner,'pending',Date.now(),Date.now());
    notice={message_id:id,epoch:Number(b.epoch),stage:'delivered',sender:mail.sender,recipient:owner,attempts:0,state:'pending'};
   }else notice=this.db.one<Receipt>('SELECT * FROM receipts WHERE message_id=? AND stage=? AND epoch=?',id,'delivered',b.epoch);
   return json({verified:true});
  });
  if(!result.ok)return result;
  await this.schedule();
  const forwarded=notice?.state==='recorded'||!!notice&&await this.flushReceipt(notice,{provider:'aplexer',message_id:receipt.message_id,session_id:session,recipient:owner,machine_id:actor.resource_id});
  await this.flushNative(id,Number(b.epoch));await this.schedule();return forwarded?json({phase:'delivered',verified:true}):error('receipt_pending',503);
 }

 private async receipt(id:string,stage:'read'|'ack'|'accept'|'complete',owner:string,epoch:number):Promise<Response>{
  const receipt=this.db.transaction(this.ctx,()=>{
   const row=this.db.one<Mail>('SELECT * FROM inbox WHERE id=?',id);if(!row||row.state==='revoked')throw new HttpError('message_not_found',404);
   if(row.epoch!==epoch)throw new HttpError('stale_route_epoch',409);
   const prior=this.db.one<Receipt>('SELECT * FROM receipts WHERE message_id=? AND stage=? AND epoch=?',id,stage,epoch);if(prior)return prior;
   if(row.state==='received')this.db.run('UPDATE inbox SET state=? WHERE id=?','read',id);
   this.db.run('INSERT INTO receipts(message_id,stage,epoch,sender,recipient,state,next_attempt,created_at) VALUES(?,?,?,?,?,?,?,?)',id,stage,epoch,row.sender,owner,'pending',Date.now(),Date.now());
   return {message_id:id,stage,epoch,sender:row.sender,recipient:owner,attempts:0,state:'pending'};
  });
  if(receipt.state==='recorded')return json({recorded:true,replayed:true});await this.schedule();
  const recorded=await this.flushReceipt(receipt);await this.schedule();return recorded?json({recorded:true}):error('receipt_pending',503);
 }
 private async flushReceipt(r:Receipt,proof?:JsonRecord):Promise<boolean>{
  try{
   const claim=r.stage==='delivered'&&!proof?this.db.one<{receipt_json:string}>('SELECT receipt_json FROM delivery_claims WHERE message_id=? AND epoch=? AND state=?',r.message_id,r.epoch,'delivered'):undefined;
   const response=await this.call(r.sender,'/_receipt',{id:r.message_id,epoch:r.epoch,recipient:r.recipient,stage:r.stage,...(proof?{native_receipt:proof}:claim?.receipt_json?{native_receipt:JSON.parse(claim.receipt_json)}:{})});if(!response.ok)throw new Error('receipt_pending');
   this.db.transaction(this.ctx,()=>{
    if(r.stage!=='delivered'){const field=stageField(r.stage);const state=r.stage==='complete'?"'completed'":r.stage==='accept'?"CASE WHEN completed_at IS NOT NULL THEN 'completed' ELSE 'accepted' END":r.stage==='read'?"CASE WHEN completed_at IS NOT NULL THEN 'completed' WHEN accepted_at IS NOT NULL THEN 'accepted' WHEN acked_at IS NOT NULL THEN 'acked' ELSE 'read' END":"CASE WHEN completed_at IS NOT NULL THEN 'completed' WHEN accepted_at IS NOT NULL THEN 'accepted' ELSE 'acked' END";
     this.db.run(`UPDATE inbox SET ${field}=COALESCE(${field},?),state=${state} WHERE id=? AND epoch=? AND state<>'revoked' AND ${field} IS NULL`,Date.now(),r.message_id,r.epoch);}
    this.db.run("UPDATE receipts SET state=?,next_attempt=NULL WHERE message_id=? AND stage=? AND epoch=? AND state='pending'",'recorded',r.message_id,r.stage,r.epoch);
   });return true;
  }catch{this.db.run("UPDATE receipts SET attempts=attempts+1,next_attempt=? WHERE message_id=? AND stage=? AND epoch=? AND state='pending'",Date.now()+Math.min(300000,1000*2**Math.min(r.attempts+1,8)),r.message_id,r.stage,r.epoch);return false;}
 }
 private async readdress(r:Request,id:string,a:Actor):Promise<Response>{
  if(!hasScope(a,'operator:admin'))return error('forbidden',403);
  const b=await bodyObject(r,4096);if(!boundedString(b.recipient,180)||!boundedString(b.key,120)||!Number.isSafeInteger(b.epoch))return error('readdress_key_epoch_required',400);
  const digest=await sha256Hex(JSON.stringify([id,b.recipient,b.epoch]));
  const replay=this.db.one<{digest:string;state:string;result_json:string|null}>('SELECT digest,state,result_json FROM readdress_operations WHERE key=?',b.key);
  if(replay){if(replay.digest!==digest)return error('idempotency_conflict',409);if(replay.state!=='pending')return json(JSON.parse(replay.result_json!),replay.state==='refused'?409:200);}
  b.recipient=await this.resolveRecipient(String(b.recipient));
  const operation=this.db.transaction(this.ctx,()=>{
   const prior=this.db.one<{message_id:string;digest:string;state:string;result_json:string|null}>('SELECT * FROM readdress_operations WHERE key=?',b.key);
   if(prior){if(prior.message_id!==id||prior.digest!==digest)throw new HttpError('idempotency_conflict',409);if(prior.state!=='pending')return prior;}
   const row=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',id);if(!row)throw new HttpError('message_not_found',404);
   if(row.epoch!==b.epoch)throw new HttpError('stale_route_epoch',409);
   if(row.read_at||row.acked_at||row.accepted_at||row.completed_at)throw new HttpError('delivery_already_started',409);
   if(row.state==='revoking'&&row.readdress_key!==b.key)throw new HttpError('readdress_in_progress',409);
   this.db.run('INSERT OR IGNORE INTO readdress_operations(key,message_id,digest,state) VALUES(?,?,?,?)',b.key,id,digest,'pending');
   if(row.recipient===b.recipient&&!row.attention_json){const result=JSON.stringify({message:this.view(row)});this.db.run('UPDATE readdress_operations SET state=?,result_json=? WHERE key=?','complete',result,b.key);return {state:'complete',result_json:result};}
   this.db.run('UPDATE outbox SET state=?,target=?,readdress_key=?,next_attempt=? WHERE id=?','revoking',b.recipient,b.key,Date.now(),id);return {state:'pending',result_json:null};
  });
  if(operation.state!=='pending')return json(JSON.parse(operation.result_json!),operation.state==='refused'?409:200);
  await this.schedule();const row=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',id)!;await this.finishReaddress(row);await this.schedule();
  const result=this.db.one<{state:string;result_json:string|null}>('SELECT state,result_json FROM readdress_operations WHERE key=?',b.key)!;
  return result.result_json?json(JSON.parse(result.result_json),result.state==='refused'?409:200):json({state:'revoking',operation_key:b.key},202);
 }
 private async finishReaddress(m:Mail):Promise<boolean|undefined>{
  if(!m.target||!m.readdress_key)return false;
  try{
   const response=await this.call(m.recipient,'/_revoke',{id:m.id,epoch:m.epoch});
   if(!response.ok){if(response.status===409){this.db.transaction(this.ctx,()=>{this.db.run('UPDATE outbox SET state=?,target=NULL,readdress_key=NULL,next_attempt=NULL WHERE id=? AND epoch=? AND state=?','received',m.id,m.epoch,'revoking');this.db.run('UPDATE readdress_operations SET state=?,result_json=? WHERE key=?','refused',JSON.stringify({error:'delivery_already_started',message:this.view({...m,state:'received'})}),m.readdress_key);});return false;}throw new Error('revocation_pending');}
   const next=this.db.transaction(this.ctx,()=>{
    const row=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',m.id);if(!row||row.epoch!==m.epoch||row.state!=='revoking'||row.readdress_key!==m.readdress_key)return undefined;
    this.db.run('UPDATE outbox SET recipient=?,epoch=epoch+1,state=?,attention_json=NULL,target=NULL,readdress_key=NULL,next_attempt=? WHERE id=?',m.target,'pending-directory',Date.now(),m.id);this.event(m.id,'readdress',m.epoch+1);const result=this.db.one<Mail>('SELECT * FROM outbox WHERE id=?',m.id)!;
    this.db.run('UPDATE readdress_operations SET state=?,result_json=? WHERE key=?','complete',JSON.stringify({message:this.view(result)}),m.readdress_key);return result;
   });if(next)await this.deliver(next);return true;
  }catch{this.defer(m.id,m.epoch);}
 }

 private defer(id:string,epoch:number):void{
  const row=this.db.one<{attempts:number}>('SELECT attempts FROM outbox WHERE id=? AND epoch=?',id,epoch);if(!row)return;const n=row.attempts+1;
  this.db.run('UPDATE outbox SET attempts=?,next_attempt=? WHERE id=? AND epoch=?',n,Date.now()+Math.min(300000,1000*2**Math.min(n,8)),id,epoch);
 }
 private event(id:string,stage:string,epoch:number):void{this.db.run('INSERT OR IGNORE INTO events(message_id,stage,epoch,at) VALUES(?,?,?,?)',id,stage,epoch,Date.now());}
 private stageEvents(m:Mail):unknown[]{
  const events=this.db.all<{stage:string;epoch:number;at:number}>('SELECT stage,epoch,at FROM events WHERE message_id=? ORDER BY epoch,stage LIMIT 50',m.id);
  for(const [stage,field] of Object.entries({delivered:'delivered_at',read:'read_at',ack:'acked_at',accept:'accepted_at',complete:'completed_at'})){
   const at=(m as unknown as Record<string,unknown>)[field];if(typeof at==='number')events.push({stage,epoch:m.epoch,at});
  }return events.sort((a,b)=>a.epoch-b.epoch||a.stage.localeCompare(b.stage));
 }
 private view(m:Mail):JsonRecord{return {id:m.id,sender:m.sender,recipient:m.recipient,body:m.body,key:m.key,thread_id:m.thread_id,reply_to:m.reply_to,epoch:m.epoch,state:m.state,created_at:m.created_at,read_at:m.read_at,acked_at:m.acked_at,accepted_at:m.accepted_at,completed_at:m.completed_at,delivered_at:m.delivered_at,native_receipt:m.native_receipt_json?JSON.parse(m.native_receipt_json):null,native_attention:m.attention_json?JSON.parse(m.attention_json):null};}
 private async flushNative(id:string,epoch:number):Promise<void>{
  let row=this.db.one<NativeOutbox>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,epoch);if(!row)return;
  try{
   if(row.state==='pending-target'){
    const recipient=await this.control('/_recipient',{recipient:row.recipient});if(!recipient.ok)throw new Error('native_recipient_unavailable');
    const target=await recipient.json() as {forward_session?:string|null};const session=row.recipient.startsWith('session:')?row.recipient.slice(8):target.forward_session;
    if(!session){
     this.db.run("UPDATE native_outbox SET state='unrouted',next_attempt=NULL WHERE message_id=? AND epoch=? AND state='pending-target'",id,epoch);
     // Publish the idle state before rechecking directory custody. A concurrent
     // forward update either sees this row in its durable wake or is seen here.
     const latest=await this.control('/_recipient',{recipient:row.recipient});
     if(!latest.ok)throw new Error('native_directory_recheck_failed');
     const current=await latest.json() as {forward_session?:string|null};
     if(current.forward_session)this.db.run("UPDATE native_outbox SET state='pending-target',next_attempt=? WHERE message_id=? AND epoch=? AND state='unrouted'",Date.now(),id,epoch);
     return;
    }
    const route=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch(new Request('https://control.internal/api/routes/session/'+encodeURIComponent(session),{headers:{'x-hub2-actor':JSON.stringify({id:'service:mailbox',kind:'operator',scopes:['routing:read']})}}));
    if(!route.ok)throw new Error('native_route_unavailable');const routing=await route.json() as {route?:{owner?:string}};if(!boundedString(routing.route?.owner,180))throw new Error('native_route_invalid');
    this.db.run("UPDATE native_outbox SET state='pending-machine',session_id=?,machine_id=? WHERE message_id=? AND epoch=? AND state='pending-target'",session,routing.route.owner,id,epoch);
    row=this.db.one<NativeOutbox>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,epoch)!;
   }
   if(row.state==='attention-pending'){
    const mail=this.db.one<Mail>('SELECT * FROM inbox WHERE id=?',id);
    if(!mail)throw new Error('attention_mail_missing');
    const response=await this.call(mail.sender,'/_attention',{id,epoch,recipient:row.recipient,attention:JSON.parse(row.receipt_json!)});
    if(!response.ok)throw new Error('attention_pending');
    // The source transaction fenced claims before attention. Release the old
    // unclaimed queue so its first-pending entry cannot block later messages.
    if(!row.machine_id)throw new Error('attention_target_missing');
    const cancelled=await this.env.MACHINES.get(this.env.MACHINES.idFromName(row.machine_id)).fetch(new Request('https://machine.internal/_internal/mail-cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:id,epoch,operation_id:row.operation_id})}));
    if(!cancelled.ok)throw new Error('attention_cancel_pending');
    const proof=await cancelled.json() as Record<string,unknown>;
    if(proof.cancelled!==true||proof.message_id!==id||proof.epoch!==epoch||proof.operation_id!==row.operation_id)throw new Error('attention_cancel_mismatch');
    this.db.run("UPDATE native_outbox SET state='attention',next_attempt=NULL WHERE message_id=? AND epoch=? AND state='attention-pending'",id,epoch);return;
   }
   if(!['pending-machine','pending-ack','pending-cancel'].includes(row.state)||!row.machine_id||!row.session_id)return;
   const state=row.state,path=state==='pending-machine'?'/mail':state==='pending-ack'?'/mail-ack':'/mail-cancel';
   const response=await this.env.MACHINES.get(this.env.MACHINES.idFromName(row.machine_id)).fetch(new Request('https://machine.internal/_internal'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:id,epoch,operation_id:row.operation_id,...(state==='pending-machine'?{recipient:row.recipient,session_id:row.session_id,body:row.body}:state==='pending-ack'?{native_receipt:JSON.parse(row.receipt_json!)}:{})})}));
   if(!response.ok)throw new Error('native_machine_handoff_pending');
   const accepted=await response.json() as Record<string,unknown>;
   if(accepted.message_id!==id||accepted.epoch!==epoch||accepted.operation_id!==row.operation_id||(state==='pending-machine'?accepted.queued!==true:state==='pending-ack'?accepted.acked!==true:accepted.cancelled!==true))throw new Error('native_machine_receipt_mismatch');
   this.db.run('UPDATE native_outbox SET state=?,next_attempt=NULL WHERE message_id=? AND epoch=? AND state=?',state==='pending-machine'?'queued':state==='pending-ack'?'acked':'cancelled',id,epoch,state);
  }catch{
   this.db.run("UPDATE native_outbox SET state=CASE WHEN state='unrouted' THEN 'pending-target' ELSE state END,attempts=attempts+1,next_attempt=? WHERE message_id=? AND epoch=? AND state IN (?,?,?,?,?,?)",Date.now()+Math.min(300000,1000*2**Math.min(row.attempts+1,8)),id,epoch,'pending-target','pending-machine','pending-ack','pending-cancel','unrouted','attention-pending');
  }
 }

 private control(path:string,body:JsonRecord):Promise<Response>{return this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch(new Request('https://control.internal'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}));}
 private async resolveRecipient(recipient:string):Promise<string>{
  const response=await this.control('/_recipient',{recipient});if(!response.ok)throw new HttpError(response.status===404?'recipient_unknown':'recipient_lookup_unavailable',response.status===404?404:503);
  const value=await response.json() as {mailbox?:unknown};if(!boundedString(value.mailbox,180))throw new HttpError('recipient_lookup_invalid',503);return value.mailbox;
 }
 private call(owner:string,path:string,body:JsonRecord):Promise<Response>{return this.env.MAILBOXES.get(this.env.MAILBOXES.idFromName(owner)).fetch(new Request(`https://mailbox.internal${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}));}
 private async schedule(clearEmpty=true):Promise<void>{
  const due:number[]=[];
  const outbox=this.db.one<{next_attempt:number}>('SELECT next_attempt FROM outbox WHERE due=1 ORDER BY next_attempt,id LIMIT 1');if(outbox)due.push(outbox.next_attempt);
  const receipt=this.db.one<{next_attempt:number}>('SELECT next_attempt FROM receipts WHERE due=1 ORDER BY next_attempt,message_id,stage,epoch LIMIT 1');if(receipt)due.push(receipt.next_attempt);
  const native=this.db.one<{next_attempt:number}>('SELECT next_attempt FROM native_outbox WHERE due=1 ORDER BY next_attempt,message_id,epoch LIMIT 1');if(native)due.push(native.next_attempt);
  if(due.length){this.db.alarmOperation();await this.ctx.storage.setAlarm(Math.max(Date.now()+100,Math.min(...due)));}else if(clearEmpty){this.db.alarmOperation();await this.ctx.storage.deleteAlarm();}
 }
 async alarm():Promise<void>{
  for(const m of this.db.all<Mail>('SELECT * FROM outbox WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,id LIMIT 16',Date.now())){if(m.state==='revoking')await this.finishReaddress(m);else await this.deliver(m);}
  for(const r of this.db.all<Receipt>('SELECT * FROM receipts WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,message_id,stage,epoch LIMIT 16',Date.now()))await this.flushReceipt(r);
  for(const n of this.db.all<NativeOutbox>('SELECT * FROM native_outbox WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,message_id,epoch LIMIT 16',Date.now()))await this.flushNative(n.message_id,n.epoch);
  await this.schedule();
 }

}

function stageField(stage:string):string{return ({read:'read_at',ack:'acked_at',accept:'accepted_at',complete:'completed_at'} as Record<string,string>)[stage];}
