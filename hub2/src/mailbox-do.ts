import {MeteredDb,initializeSchema} from './db';
import {sha256Hex} from './crypto';
import {actorFrom,bodyObject,boundedString,error,hasScope,HttpError,json} from './http';
import type {Actor,JsonRecord} from './types';
type Mail={id:string;sender:string;recipient:string;body:string;key:string;digest:string;thread_id:string;reply_to:string|null;epoch:number;state:string;visible:number;created_at:number;read_at:number|null;acked_at:number|null;accepted_at:number|null;completed_at:number|null;delivered_at:number|null;native_receipt_json:string|null;attention_json:string|null};
type Native={message_id:string;epoch:number;operation_id:string;recipient:string;body:string;session_id:string|null;machine_id:string|null;state:string;next_attempt:number|null;attempts:number;receipt_json:string|null};
type Claim={message_id:string;epoch:number;operation_id:string;claim_id:string;machine_id:string;session_id:string;state:string;receipt_digest:string|null};
type Move={sender:string;key:string;message_id:string;digest:string;target:string;epoch:number;state:string;next_attempt:number|null;result_json:string|null};
type Env={CONTROL:DurableObjectNamespace;MACHINES:DurableObjectNamespace};
const FIELDS:Record<string,string>={read:'read_at',ack:'acked_at',accept:'accepted_at',complete:'completed_at'};
const SCHEMA=[
 'CREATE TABLE messages(id TEXT PRIMARY KEY,sender TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,thread_id TEXT NOT NULL,reply_to TEXT,epoch INTEGER NOT NULL,state TEXT NOT NULL,visible INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL,read_at INTEGER,acked_at INTEGER,accepted_at INTEGER,completed_at INTEGER,delivered_at INTEGER,native_receipt_json TEXT,attention_json TEXT,UNIQUE(sender,key)) WITHOUT ROWID',
 'CREATE INDEX sender_page ON messages(sender,created_at,id)',
 'CREATE INDEX sender_prefix ON messages(sender,id)',
 'CREATE INDEX recipient_page ON messages(recipient,visible,created_at,id)',
 'CREATE INDEX recipient_prefix ON messages(recipient,visible,id)',
 'CREATE INDEX recipient_state ON messages(recipient,visible,state,created_at,id)',
 'CREATE INDEX recipient_thread ON messages(recipient,visible,thread_id,created_at,id)',
 'CREATE TABLE native_outbox(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,session_id TEXT,machine_id TEXT,state TEXT NOT NULL,next_attempt INTEGER,attempts INTEGER NOT NULL DEFAULT 0,receipt_json TEXT,due INTEGER GENERATED ALWAYS AS(next_attempt IS NOT NULL) VIRTUAL,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
 'CREATE INDEX native_due ON native_outbox(due,next_attempt,message_id,epoch) WHERE due=1',
 "CREATE INDEX native_wake ON native_outbox(recipient,message_id,epoch) WHERE state IN ('unrouted','pending-machine','queued')",
 'CREATE TABLE delivery_claims(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,claim_id TEXT NOT NULL,machine_id TEXT NOT NULL,session_id TEXT NOT NULL,state TEXT NOT NULL,receipt_digest TEXT,native_message_id TEXT,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
 'CREATE UNIQUE INDEX physical_receipt_once ON delivery_claims(native_message_id) WHERE native_message_id IS NOT NULL',
 'CREATE TABLE readdress_operations(sender TEXT NOT NULL,key TEXT NOT NULL,message_id TEXT NOT NULL,digest TEXT NOT NULL,target TEXT NOT NULL,epoch INTEGER NOT NULL,state TEXT NOT NULL,next_attempt INTEGER,result_json TEXT,due INTEGER GENERATED ALWAYS AS(next_attempt IS NOT NULL) VIRTUAL,PRIMARY KEY(sender,key)) WITHOUT ROWID',
 'CREATE INDEX readdress_due ON readdress_operations(due,next_attempt,sender,key) WHERE due=1',
 'CREATE TABLE events(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,stage TEXT NOT NULL,at INTEGER NOT NULL,PRIMARY KEY(message_id,epoch,stage)) WITHOUT ROWID',
];
/** The single global message authority. Owner headers select a view; every row
 * still requires sender/current-recipient authorization. Native effects remain
 * a fenced durable producer, independent of the four human receipt stages. */
export class MailboxDO{
 private db:MeteredDb;private ready:Promise<void>;
 constructor(private ctx:DurableObjectState,private env:Env){this.db=new MeteredDb(ctx.storage.sql);this.ready=initializeSchema(ctx,this.db,1,SCHEMA,[],()=>this.schedule());}
 async fetch(r:Request):Promise<Response>{
  try{
   await this.ready;const p=new URL(r.url).pathname;
   if(p==='/_metrics')return json({counts:this.db.snapshot()});
   if(p==='/_resume-native'&&r.method==='POST')return await this.resume(r);
   if(p.startsWith('/_'))return error('not_found',404);
   const actor=actorFrom(r),owner=r.headers.get('x-hub2-mailbox')||actor?.id;
   if(!actor||!boundedString(owner,180)||!(actor.id===owner||hasScope(actor,'operator:admin')||hasScope(actor,'mailbox:'+owner)))return error('forbidden',403);
   if(actor.kind==='machine'&&(!p.endsWith('/delivered')||r.method!=='POST'))return error('forbidden',403);
   if(p==='/api/messages')return r.method==='POST'?await this.send(r,owner):r.method==='GET'?this.list(r,owner):error('method_not_allowed',405);
   const match=p.match(/^\/api\/messages\/([^/]+)(?:\/(read|ack|accept|complete|status|delivered|readdress))?$/);if(!match)return error('not_found',404);
   const mail=this.resolve(decodeURIComponent(match[1]),owner,actor);if(mail instanceof Response)return mail;
   if(r.method==='GET'&&match[2]&&match[2]!=='status')return error('method_not_allowed',405);
   if(r.method==='GET')return json({message:this.view(mail),...(match[2]==='status'?{events:this.events(mail)}:{})});
   if(r.method!=='POST')return error('method_not_allowed',405);
   if(match[2]==='delivered')return await this.delivered(r,mail.id,owner,actor);
   if(match[2]==='readdress')return await this.readdress(r,mail,owner,actor);
   return match[2]&&FIELDS[match[2]]?await this.receipt(r,mail.id,owner,match[2]):error('not_found',404);
  }catch(e){return e instanceof HttpError?error(e.code,e.status):error('internal_error',500);}
 }
 private resolve(id:string,owner:string,actor:Actor):Mail|Response{
  if(!boundedString(id,120))return error('invalid_message_id',400);
  const exact=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',id),admin=hasScope(actor,'operator:admin');
  if(exact)return admin||exact.sender===owner||exact.recipient===owner&&!!exact.visible?exact:error('message_not_found',404);
  if(id.length<6)return error('prefix_too_short',400);
  const rows=admin?this.db.all<Mail>('SELECT * FROM messages WHERE id>=? AND id<? ORDER BY id LIMIT 2',id,id+'\uffff'):[
   ...this.db.all<Mail>('SELECT * FROM messages WHERE sender=? AND id>=? AND id<? ORDER BY id LIMIT 2',owner,id,id+'\uffff'),
   ...this.db.all<Mail>('SELECT * FROM messages WHERE recipient=? AND visible=1 AND id>=? AND id<? ORDER BY id LIMIT 2',owner,id,id+'\uffff')];
  const unique=[...new Map(rows.map(m=>[m.id,m])).values()];return unique.length===1?unique[0]:error(unique.length?'ambiguous_prefix':'message_not_found',unique.length?409:404);
 }
 private async directory(recipient:string):Promise<{mailbox:string;forward_session?:string|null}>{
  const response=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch(new Request('https://control.internal/_recipient',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient})}));
  if(!response.ok)throw new HttpError('recipient_unknown',response.status===404?404:503);const result=await response.json() as {mailbox:string;forward_session?:string|null};
  if(!boundedString(result.mailbox,180))throw new HttpError('invalid_directory_result',503);return result;
 }
 private async send(r:Request,owner:string):Promise<Response>{
  const b=await bodyObject(r);if(!boundedString(b.recipient,180)||!boundedString(b.body,12000)||!boundedString(b.key,120)||(b.reply_to!==undefined&&!boundedString(b.reply_to,120)))return error('invalid_message',400);
  const digest=await sha256Hex(JSON.stringify([b.recipient,b.body,b.reply_to??null,b.parent_sender??null]));
  const prior=this.db.one<Mail>('SELECT * FROM messages WHERE sender=? AND key=?',owner,b.key);if(prior)return prior.digest===digest?json({message:this.view(prior),replayed:true}):error('idempotency_conflict',409);
  const target=await this.directory(String(b.recipient));let thread=crypto.randomUUID();
  if(b.reply_to){
   const parent=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',b.reply_to);
   if(!boundedString(b.parent_sender,180))return error('parent_sender_required',400);
   if(!parent||parent.sender!==b.parent_sender||![parent.sender,parent.recipient].includes(owner)||![parent.sender,parent.recipient].includes(target.mailbox)||(!parent.visible&&owner!==parent.sender))return error('reply_forbidden',403);thread=parent.thread_id;
  }
  const id=crypto.randomUUID(),now=Date.now();
  const mail=this.db.transaction(this.ctx,()=>{
   const race=this.db.one<Mail>('SELECT * FROM messages WHERE sender=? AND key=?',owner,b.key);if(race){if(race.digest!==digest)throw new HttpError('idempotency_conflict',409);return race;}
   this.db.run('INSERT INTO messages(id,sender,recipient,body,key,digest,thread_id,reply_to,epoch,state,created_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)',id,owner,target.mailbox,b.body,b.key,digest,thread,b.reply_to??null,'received',now);
   this.queue(id,1,target.mailbox,String(b.body),now);return this.db.one<Mail>('SELECT * FROM messages WHERE id=?',id)!;
  });
  await this.schedule();await this.flushNative(mail.id,mail.epoch);await this.schedule();return json({message:this.view(mail)},201);
 }
 private list(r:Request,owner:string):Response{
  const q=new URL(r.url).searchParams,limit=Number(q.get('limit')||25),folder=q.get('folder')||'inbox',state=q.get('state'),thread=q.get('thread');
  if(!Number.isInteger(limit)||limit<1||limit>50||!['inbox','sent'].includes(folder)||state&&!['received','claimed','read','acked','delivered','accepted','completed'].includes(state)||thread&&!boundedString(thread,120))return error('invalid_list_query',400);
  let cursor:[number,string]=[0,''];if(q.has('cursor')){try{cursor=JSON.parse(q.get('cursor')!);}catch{return error('invalid_cursor',400);}}
  if(!Array.isArray(cursor)||cursor.length!==2||!Number.isSafeInteger(cursor[0])||!boundedString(cursor[1]||'empty',120))return error('invalid_cursor',400);
  const extra=folder==='sent'?'':state?' AND state=?':thread?' AND thread_id=?':'',args:unknown[]=folder==='sent'?[]:state?[state]:thread?[thread]:[];
  const rows=this.db.all<Mail>('SELECT * FROM messages WHERE '+(folder==='sent'?'sender=?':'recipient=? AND visible=1')+extra+' AND (created_at,id)>(?,?) ORDER BY created_at,id LIMIT ?',owner,...args,...cursor,limit+1),scanned=rows.slice(0,limit);
  const filtered=folder==='sent'&&!!(state||thread)||!!state&&!!thread,shown=scanned.filter(m=>(!state||m.state===state)&&(!thread||m.thread_id===thread)),last=scanned[scanned.length-1];
  return json({items:shown.map(m=>this.view(m)),filtered_page:filtered,next:rows.length>limit&&last?JSON.stringify([last.created_at,last.id]):null});
 }
 private async receipt(r:Request,id:string,owner:string,stage:string):Promise<Response>{
  const b=await bodyObject(r,2048);if(!Number.isSafeInteger(b.epoch))return error('epoch_required',400);
  return this.db.transaction(this.ctx,()=>{
   const m=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',id);if(!m||m.recipient!==owner||!m.visible)return error('message_not_found',404);if(m.epoch!==b.epoch)return error('stale_route_epoch',409);
   const field=FIELDS[stage];if((m as unknown as JsonRecord)[field])return json({recorded:true,duplicate:true});
   const next=stage==='complete'||m.completed_at?'completed':stage==='accept'||m.accepted_at?'accepted':stage==='ack'||m.acked_at?'acked':'read';
   this.db.run('UPDATE messages SET '+field+'=?,state=? WHERE id=?',Date.now(),next,id);return json({recorded:true});
  });
 }
 private queue(id:string,epoch:number,recipient:string,body:string,now:number):void{this.db.run('INSERT INTO native_outbox(message_id,epoch,operation_id,recipient,body,state,next_attempt) VALUES(?,?,?,?,?,?,?)',id,epoch,'native:'+id+':'+epoch,recipient,body,'pending-target',now);}
 private async delivered(r:Request,id:string,owner:string,actor:Actor):Promise<Response>{
  if(actor.kind!=='machine'||!actor.resource_id||!hasScope(actor,'message:delivery'))return error('native_delivery_forbidden',403);
  const b=await bodyObject(r,8192),session=r.headers.get('x-hub2-delivery-session');
  if(!Number.isSafeInteger(b.epoch)||!boundedString(b.operation_id,120)||!boundedString(session,120)||!['claim','receipt'].includes(String(b.phase)))return error('invalid_native_delivery',400);
  if(b.phase==='claim')return this.db.transaction(this.ctx,()=>{
   const m=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',id);if(!m||m.recipient!==owner||!m.visible)return error('message_not_found',404);if(m.epoch!==b.epoch)return error('stale_route_epoch',409);
   const target=this.db.one<Native>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,b.epoch);
   if(target?.state==='pending-machine')return error('native_handoff_pending',409);
   if(!target||!['queued','pending-ack','acked'].includes(target.state)||target.machine_id!==actor.resource_id||target.session_id!==session)return error('native_target_mismatch',403);
   if(target.operation_id!==b.operation_id)return error('delivery_operation_mismatch',409);
   const old=this.db.one<Claim>('SELECT * FROM delivery_claims WHERE message_id=? AND epoch=?',id,b.epoch);
   if(old)return old.machine_id===actor.resource_id&&old.operation_id===b.operation_id&&old.session_id===session?json({phase:old.state,claim_id:old.claim_id,epoch:old.epoch}):error('delivery_already_claimed',409);
   const claim=crypto.randomUUID();this.db.run("INSERT INTO delivery_claims(message_id,epoch,operation_id,claim_id,machine_id,session_id,state) VALUES(?,?,?,?,?,?,'claimed')",id,b.epoch,b.operation_id,claim,actor.resource_id,session);
   if(m.state==='received')this.db.run("UPDATE messages SET state='claimed' WHERE id=?",id);return json({phase:'claimed',claim_id:claim,epoch:b.epoch},201);
  });
  const proof=b.native_receipt as JsonRecord|undefined;
  if(!boundedString(b.claim_id,120)||!proof||proof.provider!=='aplexer'||!boundedString(proof.message_id,180)||proof.session_id!==session||proof.recipient!==owner)return error('invalid_native_receipt',400);
  const digest=await sha256Hex(JSON.stringify([proof.provider,proof.message_id,session,owner]));
  const result=this.db.transaction(this.ctx,()=>{
   const m=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',id),claim=this.db.one<Claim>('SELECT * FROM delivery_claims WHERE message_id=? AND epoch=?',id,b.epoch);
   if(!m||m.recipient!==owner||!m.visible||m.epoch!==b.epoch)return error('stale_route_epoch',409);
   if(!claim||claim.claim_id!==b.claim_id||claim.machine_id!==actor.resource_id||claim.operation_id!==b.operation_id||claim.session_id!==session)return error('delivery_claim_mismatch',409);
   if(claim.receipt_digest)return claim.receipt_digest===digest?json({recorded:true,duplicate:true}):error('native_receipt_conflict',409);
   if(this.db.one('SELECT message_id FROM delivery_claims WHERE native_message_id=?',proof.message_id))return error('native_receipt_reused',409);
   const normalized={provider:'aplexer',message_id:proof.message_id,session_id:session,recipient:owner},evidence=JSON.stringify({...normalized,machine_id:actor.resource_id});
   this.db.run("UPDATE delivery_claims SET state='delivered',receipt_digest=?,native_message_id=? WHERE message_id=? AND epoch=?",digest,proof.message_id,id,b.epoch);
   this.db.run("UPDATE messages SET delivered_at=?,native_receipt_json=?,state=CASE WHEN state='claimed' THEN 'delivered' ELSE state END WHERE id=?",Date.now(),evidence,id);
   this.db.run("UPDATE native_outbox SET state='pending-ack',receipt_json=?,next_attempt=? WHERE message_id=? AND epoch=?",JSON.stringify(normalized),Date.now(),id,b.epoch);return json({recorded:true});
  });if(result.ok){await this.schedule();await this.flushNative(id,Number(b.epoch));await this.schedule();}return result;
 }
 private async readdress(r:Request,m:Mail,owner:string,actor:Actor):Promise<Response>{
  if(!hasScope(actor,'operator:admin')&&!(hasScope(actor,'message:readdress')&&owner===m.sender))return error('forbidden',403);
  const b=await bodyObject(r,4096);if(!boundedString(b.recipient,180)||!boundedString(b.key,120)||!Number.isSafeInteger(b.epoch))return error('readdress_key_epoch_required',400);
  const digest=await sha256Hex(JSON.stringify([m.id,b.recipient,b.epoch])),old=this.db.one<Move>('SELECT * FROM readdress_operations WHERE sender=? AND key=?',m.sender,b.key);
  if(old){if(old.digest!==digest)return error('idempotency_conflict',409);if(old.result_json)return json(JSON.parse(old.result_json));}
  const target=await this.directory(String(b.recipient));
  const refused=this.db.transaction(this.ctx,()=>{
   const race=this.db.one<Move>('SELECT * FROM readdress_operations WHERE sender=? AND key=?',m.sender,b.key);
   if(race){if(race.digest!==digest)throw new HttpError('idempotency_conflict',409);return undefined;}
   const row=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',m.id)!;
   if(old)return undefined;if(row.epoch!==b.epoch)throw new HttpError('stale_route_epoch',409);
   if(row.read_at||row.acked_at||row.accepted_at||row.completed_at||row.delivered_at||this.db.one('SELECT claim_id FROM delivery_claims WHERE message_id=? AND epoch=?',m.id,b.epoch))return json({error:'delivery_already_started',message:this.view(row)},409);
   if(!row.visible)throw new HttpError('readdress_in_progress',409);
   if(row.recipient===target.mailbox&&!row.attention_json){
    const result={message:this.view(row)};
    this.db.run("INSERT INTO readdress_operations(sender,key,message_id,digest,target,epoch,state,next_attempt,result_json) VALUES(?,?,?,?,?,?,'complete',NULL,?)",m.sender,b.key,m.id,digest,target.mailbox,b.epoch,JSON.stringify(result));
    return json(result);
   }
   this.db.run("INSERT INTO readdress_operations(sender,key,message_id,digest,target,epoch,state,next_attempt) VALUES(?,?,?,?,?,?,'pending',?)",m.sender,b.key,m.id,digest,target.mailbox,b.epoch,Date.now());
   this.db.run("UPDATE messages SET state='revoking',visible=0 WHERE id=?",m.id);
   this.db.run("UPDATE native_outbox SET state=CASE WHEN machine_id IS NULL THEN 'cancelled' ELSE 'pending-cancel' END,next_attempt=CASE WHEN machine_id IS NULL THEN NULL ELSE ? END WHERE message_id=? AND epoch=?",Date.now(),m.id,b.epoch);return undefined;
  });if(refused)return refused;
  await this.schedule();await this.finishMove(m.sender,String(b.key));await this.schedule();
  const result=this.db.one<Move>('SELECT * FROM readdress_operations WHERE sender=? AND key=?',m.sender,b.key)!;return result.result_json?json(JSON.parse(result.result_json)):json({state:'revoking',operation_key:b.key},202);
 }
 private async finishMove(sender:string,key:string):Promise<void>{
  const op=this.db.one<Move>('SELECT * FROM readdress_operations WHERE sender=? AND key=?',sender,key);if(!op||op.state!=='pending')return;
  await this.flushNative(op.message_id,op.epoch);
  const native=this.db.one<Native>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',op.message_id,op.epoch);
  if(native?.state!=='cancelled'){this.db.run('UPDATE readdress_operations SET next_attempt=? WHERE sender=? AND key=? AND state=?',Math.max(Date.now()+1000,native?.next_attempt||0),sender,key,'pending');return;}
  this.db.transaction(this.ctx,()=>{
   const fresh=this.db.one<Move>('SELECT * FROM readdress_operations WHERE sender=? AND key=?',sender,key);if(fresh?.state!=='pending')return;
   const m=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',op.message_id);if(!m||m.epoch!==op.epoch||m.visible)throw new HttpError('readdress_binding_changed',409);
   const next={...m,recipient:op.target,epoch:m.epoch+1,state:'received',visible:1,attention_json:null};
   this.db.run("UPDATE messages SET recipient=?,epoch=epoch+1,state='received',visible=1,attention_json=NULL WHERE id=?",op.target,m.id);
   this.queue(m.id,next.epoch,op.target,m.body,Date.now());this.db.run("INSERT INTO events VALUES(?,?,'readdress',?)",m.id,next.epoch,Date.now());
   this.db.run("UPDATE readdress_operations SET state='complete',next_attempt=NULL,result_json=? WHERE sender=? AND key=?",JSON.stringify({message:this.view(next)}),sender,key);
  });const current=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',op.message_id)!;await this.flushNative(current.id,current.epoch);
 }
 private async resume(r:Request):Promise<Response>{
  const b=await bodyObject(r,4096),after=b.after??'';if(!boundedString(b.owner,180)||typeof after!=='string'||after.length>120)return error('invalid_wake',400);
  const rows=this.db.all<Native>("SELECT * FROM native_outbox WHERE recipient=? AND state IN ('unrouted','pending-machine','queued') AND message_id>? ORDER BY message_id,epoch LIMIT 17",b.owner,after),target=await this.directory(String(b.owner));
  for(const n of rows.slice(0,16)){
   if(n.state==='unrouted'){this.db.run("UPDATE native_outbox SET state='pending-target',next_attempt=? WHERE message_id=? AND epoch=? AND state='unrouted'",Date.now(),n.message_id,n.epoch);continue;}
   const session=String(b.owner).startsWith('session:')?String(b.owner).slice(8):target.forward_session;if(session===n.session_id)continue;
   this.db.transaction(this.ctx,()=>{
    const m=this.db.one<Mail>('SELECT * FROM messages WHERE id=?',n.message_id);if(!m||m.epoch!==n.epoch||!m.visible||this.db.one('SELECT claim_id FROM delivery_claims WHERE message_id=? AND epoch=?',n.message_id,n.epoch))return;
    const attention=JSON.stringify({reason:'forward_target_changed',previous_session:n.session_id,current_session:session??null,repair:'authorized_readdress_same_recipient_or_replacement_message'});
    this.db.run('UPDATE messages SET attention_json=? WHERE id=?',attention,m.id);
    this.db.run("UPDATE native_outbox SET state='attention-pending',receipt_json=?,next_attempt=? WHERE message_id=? AND epoch=? AND state IN ('pending-machine','queued')",attention,Date.now(),m.id,n.epoch);
   });
  }await this.schedule();return json({resumed:Math.min(rows.length,16),next:rows.length>16?rows[15].message_id:null});
 }
 private async flushNative(id:string,epoch:number):Promise<void>{
  let n=this.db.one<Native>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,epoch);if(!n)return;
  try{
   if(n.state==='pending-target'){
    const target=await this.directory(n.recipient),session=n.recipient.startsWith('session:')?n.recipient.slice(8):target.forward_session;
    if(!session){this.db.run("UPDATE native_outbox SET state='unrouted',next_attempt=NULL WHERE message_id=? AND epoch=? AND state='pending-target'",id,epoch);const latest=await this.directory(n.recipient);if(latest.forward_session)this.db.run("UPDATE native_outbox SET state='pending-target',next_attempt=? WHERE message_id=? AND epoch=? AND state='unrouted'",Date.now(),id,epoch);return;}
    const response=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch(new Request('https://control.internal/api/routes/session/'+encodeURIComponent(session),{headers:{'x-hub2-actor':JSON.stringify({id:'service:mail',kind:'operator',scopes:['routing:read']})}}));
    if(!response.ok)throw new Error('native_route_unavailable');const route=await response.json() as {route?:{owner:string}};if(!boundedString(route.route?.owner,180))throw new Error('native_route_invalid');
    this.db.run("UPDATE native_outbox SET state='pending-machine',session_id=?,machine_id=? WHERE message_id=? AND epoch=? AND state='pending-target'",session,route.route.owner,id,epoch);n=this.db.one<Native>('SELECT * FROM native_outbox WHERE message_id=? AND epoch=?',id,epoch)!;
   }
   if(!['pending-machine','pending-ack','pending-cancel','attention-pending'].includes(n.state)||!n.machine_id||!n.session_id)return;
   const state=n.state,path=state==='pending-machine'?'mail':state==='pending-ack'?'mail-ack':'mail-cancel';
   const response=await this.env.MACHINES.get(this.env.MACHINES.idFromName(n.machine_id)).fetch(new Request('https://machine.internal/_internal/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:id,epoch,operation_id:n.operation_id,...(state==='pending-machine'?{recipient:n.recipient,session_id:n.session_id,body:n.body}:state==='pending-ack'?{native_receipt:JSON.parse(n.receipt_json!)}:{})})}));
   if(!response.ok)throw new Error('native_handoff_pending');const proof=await response.json() as JsonRecord;
   if(proof.message_id!==id||proof.epoch!==epoch||proof.operation_id!==n.operation_id||(state==='pending-machine'?proof.queued!==true:state==='pending-ack'?proof.acked!==true:proof.cancelled!==true))throw new Error('native_handoff_mismatch');
   this.db.run('UPDATE native_outbox SET state=?,next_attempt=NULL WHERE message_id=? AND epoch=? AND state=?',state==='pending-machine'?'queued':state==='pending-ack'?'acked':state==='attention-pending'?'attention':'cancelled',id,epoch,state);
  }catch{this.db.run("UPDATE native_outbox SET state=CASE WHEN state='unrouted' THEN 'pending-target' ELSE state END,attempts=attempts+1,next_attempt=? WHERE message_id=? AND epoch=? AND state IN ('pending-target','pending-machine','pending-ack','pending-cancel','unrouted','attention-pending')",Date.now()+Math.min(300000,1000*2**Math.min(n.attempts+1,8)),id,epoch);}
 }
 private events(m:Mail):JsonRecord[]{
  const rows=this.db.all<JsonRecord>('SELECT stage,epoch,at FROM events WHERE message_id=? ORDER BY epoch,stage LIMIT 64',m.id);rows.push({stage:'received',epoch:1,at:m.created_at});
  for(const [stage,field] of Object.entries({...FIELDS,delivered:'delivered_at'})){const at=(m as unknown as JsonRecord)[field];if(typeof at==='number')rows.push({stage,epoch:m.epoch,at});}return rows.sort((a,b)=>Number(a.epoch)-Number(b.epoch)||String(a.stage).localeCompare(String(b.stage)));
 }
 private view(m:Mail):JsonRecord{return{id:m.id,sender:m.sender,recipient:m.recipient,body:m.body,key:m.key,thread_id:m.thread_id,reply_to:m.reply_to,epoch:m.epoch,state:m.state,created_at:m.created_at,read_at:m.read_at,acked_at:m.acked_at,accepted_at:m.accepted_at,completed_at:m.completed_at,delivered_at:m.delivered_at,native_receipt:m.native_receipt_json?JSON.parse(m.native_receipt_json):null,native_attention:m.attention_json?JSON.parse(m.attention_json):null};}
 private async schedule():Promise<void>{
  const a=this.db.one<{at:number}>('SELECT next_attempt AS at FROM native_outbox WHERE due=1 ORDER BY next_attempt,message_id,epoch LIMIT 1'),b=this.db.one<{at:number}>('SELECT next_attempt AS at FROM readdress_operations WHERE due=1 ORDER BY next_attempt,sender,key LIMIT 1'),next=a&&b?Math.min(a.at,b.at):a?.at??b?.at;
  this.db.alarmOperation();if(next!==undefined)await this.ctx.storage.setAlarm(Math.max(Date.now()+1,next));else await this.ctx.storage.deleteAlarm();
 }
 async alarm():Promise<void>{
  await this.ready;for(const n of this.db.all<Native>('SELECT * FROM native_outbox WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,message_id,epoch LIMIT 16',Date.now()))await this.flushNative(n.message_id,n.epoch);
  for(const op of this.db.all<Move>('SELECT * FROM readdress_operations WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,sender,key LIMIT 8',Date.now()))await this.finishMove(op.sender,op.key);await this.schedule();
 }
}

