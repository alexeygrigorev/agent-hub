/** Production composition: retain Hub1's namespace without restoring its dispatcher. */
import worker from './src/worker';
import {boundedText} from './src/http';
import {safeEqual,sha256Hex} from './src/crypto';
import {ProjectDO as BaseProjectDO} from './src/project-do';
export {ControlDO} from './src/control-do';

export {MachineDO} from './src/machine-do';
export {AccountDO} from './src/account-do';
export {MailboxDO} from './src/mailbox-do';
export {BudgetDO} from './src/budget-do';

const reply=(body:unknown,status=200)=>Response.json(body,{status,headers:{'Cache-Control':'no-store'}});
const name=(value:string)=>{if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value))throw Error('identifier');return '"'+value+'"';};
const allowed=(value:string)=>!value.startsWith('sqlite_')&&!value.startsWith('_cf_')&&value!=='backup_rate';
function clean(value:any):any{
 if(typeof value==='bigint')return {__hub_backup_bigint:String(value)};
 if(value instanceof ArrayBuffer||ArrayBuffer.isView(value)){
  const bytes=value instanceof ArrayBuffer?new Uint8Array(value):new Uint8Array(value.buffer,value.byteOffset,value.byteLength);
  let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);
  return {__hub_backup_blob_b64:btoa(binary)};
 }
 if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,clean(v)]));
 return value;
}

/** Existing v1 class/export name intentionally retained. Constructor and alarm never run Hub1 code. */
export class Hub{
 constructor(private ctx:DurableObjectState,private env:any){}
 async alarm():Promise<void>{/* Consume any old pending alarm once, never rearm or dispatch. */}
 async fetch(request:Request):Promise<Response>{
  if(!this.env.HUB1_EXPORT_TOKEN||!safeEqual(request.headers.get('Authorization')||'','Bearer '+this.env.HUB1_EXPORT_TOKEN))
   return reply({error:'unauthorized'},401);
  if(request.method!=='GET'||!this.env.HUB1_EXPORT_GENERATION)return reply({error:'maintenance_read_only'},403);
  try{
   const url=new URL(request.url),sql=this.ctx.storage.sql;
   const tables=sql.exec<{name:string;sql:string}>("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").toArray().filter(t=>allowed(t.name));
   if(tables.length>1024)return reply({error:'maintenance_table_bound'},413);
   const base={snapshot:'hub-do-sqlite',exported_at:new Date().toISOString(),writes_disabled:true,maintenance_generation:this.env.HUB1_EXPORT_GENERATION,api_version:5};
   if(url.pathname==='/api/backup/status'){
    const row_counts=Object.fromEntries(tables.map(t=>[t.name,Number(sql.exec<{n:number}>('SELECT COUNT(*) AS n FROM '+name(t.name)).one().n)]));
    return reply({...base,tables:tables.map(t=>t.name),row_counts,without_rowid:tables.filter(t=>/\bWITHOUT\s+ROWID\b/i.test(t.sql)).map(t=>t.name)});
   }
   if(url.pathname!=='/api/backup/export')return reply({error:'not_found'},404);
   if(url.searchParams.get('schema')==='1'){
    const objects=sql.exec<{name:string;sql:string}>("SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','trigger','view') AND sql IS NOT NULL ORDER BY name").toArray().filter(t=>allowed(t.name));
    if(objects.length>4096)return reply({error:'maintenance_schema_bound'},413);
    return reply({...base,schema:Object.fromEntries(objects.map(t=>[t.name,t.sql]))});
   }
   const table=url.searchParams.get('table'),limit=Number(url.searchParams.get('limit')||100);
   if(!table||!tables.some(t=>t.name===table)||!Number.isSafeInteger(limit)||limit<1||limit>100)return reply({error:'invalid_chunk'},400);
   const ddl=tables.find(t=>t.name===table)!.sql,total=Number(sql.exec<{n:number}>('SELECT COUNT(*) AS n FROM '+name(table)).one().n);
   const after=url.searchParams.get('after'),offset=Number(url.searchParams.get('offset')||0);
   if(after!==null){
    if(/\bWITHOUT\s+ROWID\b/i.test(ddl)||after!=='first'&&!/^-?\d{1,19}$/.test(after))return reply({error:'invalid_cursor'},400);
    if(after!=='first'&&(BigInt(after)<-(2n**63n)||BigInt(after)>2n**63n-1n))return reply({error:'invalid_cursor'},400);
    const rows=sql.exec<any>('SELECT CAST(rowid AS TEXT) AS __hub_backup_rowid,* FROM '+name(table)+(after==='first'?'':' WHERE rowid>CAST(? AS INTEGER)')+' ORDER BY rowid LIMIT ?',...(after==='first'?[]:[after]),limit).toArray();
    const next_after=rows.length?String(rows[rows.length-1].__hub_backup_rowid):null;
    const data=rows.map(row=>{const{__hub_backup_rowid,...rest}=row;return clean(rest);});
    return reply({...base,table,ddl,total,cursor:'rowid',after,next_after,rows:data});
   }
   if(!Number.isSafeInteger(offset)||offset<0)return reply({error:'invalid_cursor'},400);
   const pk=sql.exec<any>('PRAGMA table_info('+name(table)+')').toArray().filter(row=>row.pk>0).sort((a,b)=>a.pk-b.pk).map(row=>name(row.name));
   if(!pk.length)return reply({error:'stable_offset_order_unavailable'},409);
   const rows=sql.exec<any>('SELECT * FROM '+name(table)+' ORDER BY '+pk.join(',')+' LIMIT ? OFFSET ?',limit,offset).toArray().map(clean);
   return reply({...base,table,ddl,total,cursor:'offset',offset,rows});
  }catch{return reply({error:'maintenance_export_failed'},503);}
 }
}
/** Dedicated maintenance authority; disabled after activation. No queue/schedule rows are imported. */
export class ProjectDO extends BaseProjectDO{
 constructor(private importCtx:DurableObjectState,private importEnv:any){super(importCtx,importEnv);}
 async fetch(request:Request):Promise<Response>{
  const url=new URL(request.url);
  if(!url.pathname.startsWith('/_maintenance/'))return super.fetch(request);
  if(this.importEnv.HUB2_RELEASE_MODE!=='maintenance'||!this.importEnv.HUB2_IMPORT_TOKEN||
    !safeEqual(request.headers.get('Authorization')||'','Bearer '+this.importEnv.HUB2_IMPORT_TOKEN))return reply({error:'maintenance_import_forbidden'},403);
  try{
   const db=this.db;
   if(url.pathname==='/_maintenance/task'&&request.method==='POST'){
    const text=await boundedText(request,8*1024*1024);
    const envelope=JSON.parse(text),manifest=JSON.parse(this.importEnv.HUB2_IMPORT_MANIFEST||'null');
    if(!manifest||manifest.version!==1||manifest.live_state_verified!==true||manifest.project_resolution!=='verified-canonical-registry'||
      !Number.isSafeInteger(manifest.task_count)||manifest.task_count<1||manifest.task_count>100000||
      typeof envelope.task_json!=='string'||!Array.isArray(envelope.proof)||envelope.proof.length!==Math.ceil(Math.log2(manifest.task_count)))return reply({error:'import_manifest_required'},403);
    const b=JSON.parse(envelope.task_json),digest=await sha256Hex(envelope.task_json);
    let root=await sha256Hex('leaf:'+digest);
    for(const sibling of envelope.proof){
     if(!/^[a-f0-9]{64}$/.test(sibling.hash)||!['left','right'].includes(sibling.side))return reply({error:'invalid_manifest_proof'},403);
     root=await sha256Hex('node:'+(sibling.side==='left'?sibling.hash+root:root+sibling.hash));
    }
    if(root!==manifest.merkle_root||b.source_sha256!==manifest.source_sha256||b.generation!==manifest.generation||b.import_run_id!==manifest.import_run_id)
     return reply({error:'import_manifest_binding'},403);
    if(b.live_state_verified!==true||!/^[a-f0-9-]{36}$/.test(b.import_run_id)||!/^[a-f0-9]{64}$/.test(b.source_sha256)||typeof b.generation!=='string'||!b.generation||b.generation.length>128||
      b.project!==url.searchParams.get('project')||typeof b.id!=='string'||!b.id||b.id.length>180||typeof b.project!=='string'||!b.project||b.project.length>180||
      b.record?.id!==b.id||typeof b.title!=='string'||typeof b.text!=='string'||!Array.isArray(b.acceptance_criteria)||
      (b.parent_intake_id!==null&&typeof b.parent_intake_id!=='string')||!b.acceptance_criteria.every((v:unknown)=>typeof v==='string')||!['open','accepted','archived','needs_attention'].includes(b.status)||
      ![b.created_at,b.updated_at,b.imported_at].every(Number.isSafeInteger)||!Array.isArray(b.events)||b.events.length>5000||
      b.events.some((e:any)=>!Number.isSafeInteger(e.sequence)||e.sequence<0||!Number.isSafeInteger(e.at)||!e.event||typeof e.event!=='object')||
      new Set(b.events.map((e:any)=>e.sequence)).size!==b.events.length)return reply({error:'invalid_import'},400);
    const prior=db.one<any>('SELECT source_json FROM intakes WHERE id=?',b.id);
    if(prior)return JSON.parse(prior.source_json).import_digest===digest?reply({imported:true,duplicate:true,id:b.id,count:1,events:b.events.length,digest}):reply({error:'import_conflict'},409);
    const route=await this.importEnv.CONTROL.get(this.importEnv.CONTROL.idFromName('global')).fetch('https://control/_routes/register',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'intake',id:b.id,owner:b.project,resource_id:b.project,participants:[]})});
    if(!route.ok)return reply({error:'import_route_pending'},503);
    db.transaction(this.importCtx,()=>{
     if(db.one('SELECT id FROM intakes WHERE id=?',b.id))throw Error('import_race');
     db.run("INSERT INTO intakes(id,project,title,text,acceptance_criteria_json,status,created_at,updated_at,original_title,original_text,source_json,priority,owner_actor_id,route_registered,route_next_action_at,parent_intake_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,2,'operator:migration',1,0,?)",
       b.id,b.project,b.title,b.text,JSON.stringify(b.acceptance_criteria),b.status,b.created_at,b.updated_at,b.title,b.text,
       JSON.stringify({type:'hub1_import',source_sha256:b.source_sha256,generation:b.generation,manifest_root:manifest.merkle_root,import_run_id:b.import_run_id,import_digest:digest,record:b.record,imported_at:b.imported_at}),b.parent_intake_id);
     for(const e of b.events)db.run('INSERT INTO intake_events(task_id,seq,event_key,actor_id,kind,body_json,at) VALUES(?,?,?,?,?,?,?)',
       b.id,e.sequence+1,'hub1:'+b.source_sha256+':'+b.id+':'+e.sequence,
       typeof e.event.actor==='string'?e.event.actor:'legacy:unknown','legacy:'+String(e.event.kind||'unknown').slice(0,80),
       JSON.stringify({legacy_sequence:e.sequence,source_time_unknown:e.source_time_unknown===true,event:e.event}),e.at);
    });
    return reply({imported:true,id:b.id,count:1,events:b.events.length,digest},201);
   }
   if(url.pathname==='/_maintenance/task'&&request.method==='GET'){
    const id=url.searchParams.get('id'),row=db.one<any>('SELECT * FROM intakes WHERE id=?',id||'');
    if(!row)return reply({error:'not_found'},404);
    const source=JSON.parse(row.source_json),events=db.all<any>('SELECT seq,body_json,at FROM intake_events WHERE task_id=? ORDER BY seq LIMIT 5001',id);
    const projection={id:row.id,project:row.project,title:row.title,text:row.text,acceptance_criteria:JSON.parse(row.acceptance_criteria_json),status:row.status,created_at:row.created_at,updated_at:row.updated_at,parent_intake_id:row.parent_intake_id};
    return reply({id:row.id,source_sha256:source.source_sha256,generation:source.generation,manifest_root:source.manifest_root,import_run_id:source.import_run_id,
      projection_sha256:await sha256Hex(JSON.stringify(projection)),record_sha256:await sha256Hex(JSON.stringify(source.record)),
      events_sha256:await sha256Hex(JSON.stringify(events.map((e:any)=>({sequence:e.seq-1,at:e.at,...JSON.parse(e.body_json)})))),
      event_count:events.length,dispatch_enabled:false});
   }
   return reply({error:'not_found'},404);
  }catch{return reply({error:'maintenance_import_refused'},409);}
 }
}
export default{
 async fetch(request:Request,env:any):Promise<Response>{
  const url=new URL(request.url);
  if(url.pathname==='/api/maintenance/task'){
   if(env.HUB2_RELEASE_MODE!=='maintenance'||!env.HUB2_IMPORT_TOKEN||
     !safeEqual(request.headers.get('Authorization')||'','Bearer '+env.HUB2_IMPORT_TOKEN))return reply({error:'maintenance_import_forbidden'},403);
   const project=url.searchParams.get('project');
   if(!project||project.length>180)return reply({error:'project_required'},400);
   const target=new URL(request.url);target.pathname='/_maintenance/task';
   return env.PROJECTS.get(env.PROJECTS.idFromName(project)).fetch(new Request(target,request));
  }
  if(new URL(request.url).pathname.startsWith('/api/backup/'))
   return env.HUB.get(env.HUB.idFromName('hub')).fetch(request);
  if(env.HUB2_RELEASE_MODE!=='active'){
   if(new URL(request.url).pathname==='/health')return reply({status:'maintenance',dispatch_enabled:false});
   return reply({error:'release_maintenance',dispatch_enabled:false},503);
  }
  return worker.fetch(request,env);
 },
 async scheduled(event:ScheduledController,env:any):Promise<void>{
  if(env.HUB2_RELEASE_MODE==='active')await worker.scheduled(event,env);
 },
};
