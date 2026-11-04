import {MeteredDb} from './db';
import {sha256Hex} from './crypto';
import {actorFrom,bodyObject,boundedString,error,hasScope,HttpError,json} from './http';
import type {Actor,JsonRecord} from './types';
import {parseCron,cronNext,validTimezone} from './cron';

export const PROJECT_WORK_SCHEMA=[
 'CREATE TABLE work_claims(id TEXT PRIMARY KEY,owner TEXT NOT NULL,intake_id TEXT NOT NULL,digest TEXT NOT NULL,mode TEXT NOT NULL,paths_json TEXT NOT NULL,state TEXT NOT NULL,expires_at INTEGER,expiry_due INTEGER GENERATED ALWAYS AS(expires_at IS NOT NULL) VIRTUAL,created_at INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE INDEX claim_owner ON work_claims(owner,id)',
 'CREATE INDEX claim_expiry ON work_claims(expiry_due,expires_at,id) WHERE expiry_due=1',
 'CREATE TABLE work_locks(path TEXT NOT NULL,claim_id TEXT NOT NULL,PRIMARY KEY(path,claim_id)) WITHOUT ROWID',
 'CREATE TABLE work_schedules(id TEXT PRIMARY KEY,owner TEXT NOT NULL,parent_intake_id TEXT NOT NULL,digest TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,cron TEXT,timezone TEXT NOT NULL,interval_ms INTEGER,state TEXT NOT NULL,overlap TEXT NOT NULL,queued_due INTEGER,next_due INTEGER,due INTEGER GENERATED ALWAYS AS(next_due IS NOT NULL) VIRTUAL,created_at INTEGER NOT NULL) WITHOUT ROWID',
 'CREATE INDEX schedule_due ON work_schedules(due,next_due,id) WHERE due=1',
 'CREATE TABLE schedule_occurrences(id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL,owner TEXT NOT NULL,parent_intake_id TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,due_at INTEGER NOT NULL,state TEXT NOT NULL,next_attempt INTEGER,attempt INTEGER NOT NULL DEFAULT 0,result_json TEXT,due INTEGER GENERATED ALWAYS AS(next_attempt IS NOT NULL) VIRTUAL) WITHOUT ROWID',
 'CREATE INDEX occurrence_due ON schedule_occurrences(due,next_attempt,id) WHERE due=1',
 'CREATE INDEX occurrence_schedule ON schedule_occurrences(schedule_id,due_at,id)',
];
type Claim={id:string;owner:string;intake_id:string;digest:string;mode:string;paths_json:string;state:string;expires_at:number|null;created_at:number};
type Schedule={id:string;owner:string;parent_intake_id:string;digest:string;kind:string;payload_json:string;cron:string|null;timezone:string;interval_ms:number|null;state:string;overlap:string;queued_due:number|null;next_due:number|null};
export type Occurrence={id:string;schedule_id:string;owner:string;parent_intake_id:string;kind:string;payload:JsonRecord;due_at:number};
export type ProjectWorkHooks={
 authorize:(actor:Actor,intakeId:string)=>boolean;
 occurrence:(record:Occurrence)=>Promise<{intake_id:string;session_id?:string}>;
 active:(scheduleId:string)=>Promise<boolean>;
};
const ttl=(v:unknown)=>v===undefined?300000:typeof v==='number'&&Number.isSafeInteger(v)&&v>=10000&&v<=3600000?v:0;
const safePath=(v:unknown):v is string=>typeof v==='string'&&v.length>0&&v.length<=240&&!v.startsWith('/')&&!v.includes('\\')&&!v.includes('\0')&&v.split('/').length<=16&&v.split('/').every(p=>p!==''&&p!=='.'&&p!=='..');
/** Project-local claims and durable recurring occurrence journal. No external side effect is acknowledged before its idempotent sink replies. */
export class ProjectWork{
 constructor(private ctx:DurableObjectState,private db:MeteredDb,private hooks:ProjectWorkHooks){}
 private permitted(actor:Actor|undefined,intake:string,scope:string):actor is Actor{return !!actor&&(hasScope(actor,'operator:admin')||hasScope(actor,scope))&&this.hooks.authorize(actor,intake);}
 private expire(now:number):void{
  const rows=this.db.all<Claim>('SELECT * FROM work_claims WHERE expiry_due=1 AND expires_at<=? ORDER BY expires_at,id LIMIT 16',now);
  for(const r of rows){for(const path of JSON.parse(r.paths_json))this.db.run('DELETE FROM work_locks WHERE path=? AND claim_id=?',path,r.id);this.db.run("UPDATE work_claims SET state='expired',expires_at=NULL WHERE id=?",r.id);}
 }
 async fetch(request:Request):Promise<Response|undefined>{
  const u=new URL(request.url),m=u.pathname.match(/^\/api\/(claims|schedules)(?:\/([^/]+)(?:\/(renew|release|pause|resume|cancel|occurrences))?)?$/);if(!m)return;
  try{
   const actor=actorFrom(request),id=m[2]?decodeURIComponent(m[2]):undefined;
   if(m[1]==='claims')return await this.claims(request,actor,id,m[3]);
   return await this.schedules(request,actor,id,m[3]);
  }catch(e){if(e instanceof HttpError)return error(e.code,e.status);throw e;}
 }
 private async claims(r:Request,a:Actor|undefined,id?:string,action?:string):Promise<Response>{
  if(!id&&r.method==='POST'){
   const b=await bodyObject(r,16384),duration=ttl(b.ttl_ms);
   if(!boundedString(b.key,160)||!boundedString(b.intake_id,180)||!duration||!['read','edit','review','coord'].includes(String(b.mode))||!Array.isArray(b.paths)||!b.paths.length||b.paths.length>16||!b.paths.every(safePath))return error('invalid_claim',400);
   if(!this.permitted(a,b.intake_id,'claim:write'))return error('forbidden',403);
   const paths=[...new Set(b.paths as string[])].sort(),claimId='claim:'+await sha256Hex(a.id+'\0'+b.key),digest=await sha256Hex(JSON.stringify([b.intake_id,b.mode,paths,duration]));
   return this.db.transaction(this.ctx,()=>{
    this.expire(Date.now());const prior=this.db.one<Claim>('SELECT * FROM work_claims WHERE id=?',claimId);if(prior)return prior.digest===digest?json({claim:prior,duplicate:true}):error('claim_key_conflict',409);
    // The finite active projection bounds conflict work even when a caller fills the project.
    if(this.db.all('SELECT id FROM work_claims WHERE expiry_due=1 AND expires_at>? ORDER BY expires_at,id LIMIT 129',Date.now()).length>=128)return error('claim_capacity',409);
    if(b.mode==='edit'||b.mode==='coord')for(const path of paths){
     const ancestors=path.split('/').map((_,i)=>path.split('/').slice(0,i+1).join('/'));
     for(const ancestor of ancestors)if(this.db.one('SELECT l.claim_id FROM work_locks l JOIN work_claims c ON c.id=l.claim_id WHERE l.path=? AND c.owner<>? AND c.expires_at>? LIMIT 1',ancestor,a.id,Date.now()))return error('claim_conflict',409);
     if(this.db.one('SELECT l.claim_id FROM work_locks l JOIN work_claims c ON c.id=l.claim_id WHERE l.path>=? AND l.path<? AND c.owner<>? AND c.expires_at>? LIMIT 1',path+'/',path+'/\uffff',a.id,Date.now()))return error('claim_conflict',409);
    }
    const now=Date.now(),expires=now+duration;
    this.db.run('INSERT INTO work_claims(id,owner,intake_id,digest,mode,paths_json,state,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)',claimId,a.id,b.intake_id,digest,b.mode,JSON.stringify(paths),'active',expires,now);
    if(b.mode==='edit'||b.mode==='coord')for(const path of paths)this.db.run('INSERT INTO work_locks VALUES(?,?)',path,claimId);
    return json({claim:{id:claimId,owner:a.id,intake_id:b.intake_id,mode:b.mode,paths,state:'active',expires_at:expires}},201);
   });
  }
  if(!id&&r.method==='GET'){
   if(!a||(!hasScope(a,'operator:admin')&&!hasScope(a,'claim:read')))return error('forbidden',403);
   const q=new URL(r.url).searchParams,after=q.get('after')||'',limit=Number(q.get('limit')||25);
   if(after.length>180||!Number.isInteger(limit)||limit<1||limit>50)return error('invalid_page',400);
   const rows=hasScope(a,'operator:admin')?this.db.all<Claim>('SELECT * FROM work_claims WHERE id>? ORDER BY id LIMIT ?',after,limit+1):this.db.all<Claim>('SELECT * FROM work_claims WHERE owner=? AND id>? ORDER BY id LIMIT ?',a.id,after,limit+1);
   return json({items:rows.slice(0,limit).filter(v=>this.hooks.authorize(a,v.intake_id)),next:rows.length>limit?rows[limit-1].id:null});
  }
  if(!id)return error('claim_id_required',400);
  const row=this.db.one<Claim>('SELECT * FROM work_claims WHERE id=?',id);if(!row)return error('claim_not_found',404);
  if(!this.permitted(a,row.intake_id,r.method==='GET'?'claim:read':'claim:write')||(a.id!==row.owner&&!hasScope(a,'operator:admin')))return error('forbidden',403);
  if(r.method==='GET')return json({claim:{...row,state:row.expires_at!==null&&row.expires_at<=Date.now()?'expired':row.state}});
  if(r.method!=='POST'||!['renew','release'].includes(action||''))return error('method_not_allowed',405);
  const b=await bodyObject(r,2048),duration=ttl(b.ttl_ms);if(action==='renew'&&!duration)return error('invalid_ttl',400);
  return this.db.transaction(this.ctx,()=>{
   const current=this.db.one<Claim>('SELECT * FROM work_claims WHERE id=?',id);
   if(!current)return error('claim_not_found',404);
   if(action==='release'){
    if(current.state==='active'){for(const path of JSON.parse(current.paths_json))this.db.run('DELETE FROM work_locks WHERE path=? AND claim_id=?',path,id);this.db.run("UPDATE work_claims SET state='released',expires_at=NULL WHERE id=?",id);}
    return json({released:true,id});
   }
   if(current.state!=='active'||current.expires_at===null||current.expires_at<=Date.now())return error('claim_expired',409);
   const expires=Date.now()+duration;this.db.run('UPDATE work_claims SET expires_at=? WHERE id=?',expires,id);return json({id,expires_at:expires});
  });
 }
 private next(s:Pick<Schedule,'cron'|'timezone'|'interval_ms'>,after:number):number|null{return s.cron?cronNext(parseCron(s.cron)!,s.timezone,after):after+s.interval_ms!;}
 private async schedules(r:Request,a:Actor|undefined,id?:string,action?:string):Promise<Response>{
  if(!id&&r.method==='POST'){
   const b=await bodyObject(r,32768),cron=typeof b.cron==='string'?b.cron:null,zone=String(b.timezone||'UTC'),interval=b.interval_ms;
   if(!boundedString(b.key,160)||!boundedString(b.intake_id,180)||!['root','writer','prompt','wake'].includes(String(b.kind))||!b.payload||typeof b.payload!=='object'||Array.isArray(b.payload)||!validTimezone(zone)||zone.length>100||((!!cron)===(interval!==undefined))||(cron&&!parseCron(cron))||(interval!==undefined&&(!Number.isSafeInteger(interval)||Number(interval)<60000||Number(interval)>31536000000))||!['skip','queue','allow'].includes(String(b.overlap||'skip')))return error('invalid_schedule',400);
   if(!this.permitted(a,b.intake_id,'schedule:write'))return error('forbidden',403);
   const sid='schedule:'+await sha256Hex(a.id+'\0'+b.key),payload=JSON.stringify(b.payload),digest=await sha256Hex(JSON.stringify([b.intake_id,b.kind,payload,cron,zone,interval??null,b.overlap||'skip']));
   const prior=this.db.one<Schedule>('SELECT * FROM work_schedules WHERE id=?',sid);if(prior)return prior.digest===digest?json({schedule:prior,duplicate:true}):error('schedule_key_conflict',409);
   const due=this.next({cron,timezone:zone,interval_ms:Number(interval)||null},Date.now());if(due===null)return error('schedule_never_due',400);
   this.db.run('INSERT INTO work_schedules(id,owner,parent_intake_id,digest,kind,payload_json,cron,timezone,interval_ms,state,overlap,next_due,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',sid,a.id,b.intake_id,digest,b.kind,payload,cron,zone,interval??null,'active',b.overlap||'skip',due,Date.now());
   return json({schedule:{id:sid,state:'active',next_due:due}},201);
  }
  const q=new URL(r.url).searchParams,limit=Number(q.get('limit')||25),after=q.get('after')||'';
  if(!Number.isInteger(limit)||limit<1||limit>50||after.length>180)return error('invalid_page',400);
  if(!id){
   if(!a||!hasScope(a,'operator:admin'))return error('forbidden',403);
   if(r.method!=='GET')return error('method_not_allowed',405);
   const rows=this.db.all<Schedule>('SELECT * FROM work_schedules WHERE id>? ORDER BY id LIMIT ?',after,limit+1);return json({items:rows.slice(0,limit),next:rows.length>limit?rows[limit-1].id:null});
  }
  const s=this.db.one<Schedule>('SELECT * FROM work_schedules WHERE id=?',id);if(!s)return error('schedule_not_found',404);
  if(!this.permitted(a,s.parent_intake_id,r.method==='GET'?'schedule:read':'schedule:write')||(a.id!==s.owner&&!hasScope(a,'operator:admin')))return error('forbidden',403);
  if(r.method==='GET'&&action==='occurrences'){
   const rows=this.db.all('SELECT id,due_at,state,result_json FROM schedule_occurrences WHERE schedule_id=? AND due_at>? ORDER BY due_at,id LIMIT ?',id,Number(after)||0,limit+1);
   return json({items:rows.slice(0,limit),next:rows.length>limit?(rows[limit-1] as {due_at:number}).due_at:null});
  }
  if(r.method==='GET'&&!action)return json({schedule:s});
  if(r.method!=='POST'||!['pause','resume','cancel'].includes(action||''))return error('method_not_allowed',405);
  if(s.state==='cancelled')return action==='cancel'?json({cancelled:true,id}):error('schedule_cancelled',409);
  const state=action==='resume'?'active':action==='pause'?'paused':'cancelled',due=state==='active'?this.next(s,Date.now()):null;
  this.db.run('UPDATE work_schedules SET state=?,next_due=?,queued_due=NULL WHERE id=?',state,due,id);
  return json({id,state,next_due:due});
 }
 async alarm(now=Date.now()):Promise<void>{
  this.db.transaction(this.ctx,()=>this.expire(now));
  const schedules=this.db.all<Schedule>('SELECT * FROM work_schedules WHERE due=1 AND next_due<=? ORDER BY next_due,id LIMIT 8',now);
  for(const s of schedules){
   const active=s.overlap!=='allow'?await this.hooks.active(s.id):false;
   this.db.transaction(this.ctx,()=>{
    const current=this.db.one<Schedule>('SELECT * FROM work_schedules WHERE id=?',s.id);if(!current||current.state!=='active'||current.next_due!==s.next_due)return;
    // Coalesce missed ticks; queue retains this single due tick until the prior execution is terminal.
    if(active&&s.overlap==='queue'){this.db.run('UPDATE work_schedules SET queued_due=COALESCE(queued_due,next_due),next_due=? WHERE id=?',now+60000,s.id);return;}
    if(!active){const due=s.queued_due??s.next_due,oid=s.id+':'+due;this.db.run("INSERT OR IGNORE INTO schedule_occurrences(id,schedule_id,owner,parent_intake_id,kind,payload_json,due_at,state,next_attempt) VALUES(?,?,?,?,?,?,?,'pending',?)",oid,s.id,s.owner,s.parent_intake_id,s.kind,s.payload_json,due,now);}
    this.db.run('UPDATE work_schedules SET next_due=?,queued_due=NULL WHERE id=?',this.next(s,now),s.id);
   });
  }
  const pending=this.db.all<{id:string;schedule_id:string;owner:string;parent_intake_id:string;kind:string;payload_json:string;due_at:number;attempt:number}>('SELECT * FROM schedule_occurrences WHERE due=1 AND next_attempt<=? ORDER BY next_attempt,id LIMIT 8',now);
  for(const o of pending){
   try{
    const result=await this.hooks.occurrence({id:o.id,schedule_id:o.schedule_id,owner:o.owner,parent_intake_id:o.parent_intake_id,kind:o.kind,payload:JSON.parse(o.payload_json),due_at:o.due_at});
    if(!boundedString(result.intake_id,180))throw new Error('invalid_occurrence_result');
    this.db.run("UPDATE schedule_occurrences SET state='completed',next_attempt=NULL,result_json=? WHERE id=? AND state='pending'",JSON.stringify(result),o.id);
   }catch{this.db.run('UPDATE schedule_occurrences SET attempt=attempt+1,next_attempt=? WHERE id=? AND state=?',now+Math.min(3600000,1000*2**Math.min(o.attempt,12)),o.id,'pending');}
  }
 }
 nextDeadline():number|null{
  const values=[
   this.db.one<{at:number}>('SELECT expires_at AS at FROM work_claims WHERE expiry_due=1 ORDER BY expires_at,id LIMIT 1')?.at,
   this.db.one<{at:number}>('SELECT next_due AS at FROM work_schedules WHERE due=1 ORDER BY next_due,id LIMIT 1')?.at,
   this.db.one<{at:number}>('SELECT next_attempt AS at FROM schedule_occurrences WHERE due=1 ORDER BY next_attempt,id LIMIT 1')?.at,
  ].filter((v):v is number=>typeof v==='number');return values.length?Math.min(...values):null;
 }
}
